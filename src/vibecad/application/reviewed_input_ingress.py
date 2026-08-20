"""Trusted host ingress for run-bound reviewed CAD task inputs.

This is an application-only boundary.  A local host supplies exact bytes or a
read-only regular-file descriptor together with a closed input-kind descriptor.
The store derives all media, ontology, family, operation, artifact, and document
facts from that kind; none of those authorities are accepted from a model or MCP
request.

Published catalogs are task/project/base bound, canonical, immutable, and private.
``acquire`` materializes a short-lived run snapshot compatible with the existing
``TaskInputSnapshotProvider`` worker seam.  Closing its lease removes that run
snapshot while the durable catalog remains available for recovery until the host
explicitly discards it.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from vibecad import _file_compat
from vibecad._file_compat import WindowsPathCapability
from vibecad.execution.freecad_reviewed_artifact_host import (
    REVIEWED_ARTIFACT_MANIFEST_NAME,
    TaskInputSnapshotLease,
)
from vibecad.execution.freecad_reviewed_artifact_inputs import (
    MAX_REVIEWED_ARTIFACT_BYTES,
    MAX_REVIEWED_ARTIFACT_TOTAL_BYTES,
    MAX_REVIEWED_ARTIFACTS,
    ReviewedArtifactCatalogRecord,
    ReviewedArtifactCatalogSnapshot,
)
from vibecad.workflow.program import ValidatedProgram

REVIEWED_INPUT_CATALOG_SCHEMA_VERSION = 1
REVIEWED_INPUT_CATALOG_DIRECTORY = "reviewed_inputs"
REVIEWED_INPUT_CATALOG_MANIFEST = "catalog.json"

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_READ_CHUNK_BYTES = 64 * 1024
_MANIFEST_MAX_BYTES = 512 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CATALOG_NAME = re.compile(r"catalog_[0-9a-f]{64}\Z")
_RUN_NAME = re.compile(r"\.run_[0-9a-f]{64}\.[0-9a-f]{32}\.tmp\Z")

_CATALOG_BINDING_DOMAIN = b"vibecad-reviewed-input-binding-v1\0"
_CATALOG_DIGEST_DOMAIN = b"vibecad-reviewed-input-catalog-v1\0"
_ARTIFACT_ID_DOMAIN = b"vibecad-reviewed-input-artifact-v1\0"
_RUN_NAME_DOMAIN = b"vibecad-reviewed-input-run-v1\0"


class ReviewedInputKind(StrEnum):
    """Closed attachment kinds admitted by the reviewed product families."""

    BREP = "brep"
    IGES = "iges"
    STEP = "step"
    PNG = "png"
    JPEG = "jpeg"
    PLANAR_MECHANICAL_VISUAL = "planar_mechanical_visual"


@dataclass(frozen=True, slots=True)
class _KindSpec:
    media_type: str
    role_term_ref_id: str
    schema_term_ref_id: str
    family_id: str
    operation_ids: tuple[str, ...]
    document_prefix: str


_KIND_SPECS: Final = MappingProxyType(
    {
        ReviewedInputKind.BREP: _KindSpec(
            "model/vnd.opencascade.brep",
            "role_part_file_import_artifact",
            "schema_part_brep_artifact_v1",
            "freecad_part_file_import",
            ("brep",),
            "part_file_import",
        ),
        ReviewedInputKind.IGES: _KindSpec(
            "model/iges",
            "role_part_file_import_artifact",
            "schema_part_iges_artifact_v1",
            "freecad_part_file_import",
            ("iges",),
            "part_file_import",
        ),
        ReviewedInputKind.STEP: _KindSpec(
            "model/step",
            "role_part_file_import_artifact",
            "schema_part_step_artifact_v1",
            "freecad_part_file_import",
            ("step",),
            "part_file_import",
        ),
        ReviewedInputKind.PNG: _KindSpec(
            "image/png",
            "role_imageplane_artifact",
            "schema_imageplane_png_artifact_v1",
            "freecad_imageplane",
            ("place_or_edit_image_plane",),
            "imageplane",
        ),
        ReviewedInputKind.JPEG: _KindSpec(
            "image/jpeg",
            "role_imageplane_artifact",
            "schema_imageplane_jpeg_artifact_v1",
            "freecad_imageplane",
            ("place_or_edit_image_plane",),
            "imageplane",
        ),
        ReviewedInputKind.PLANAR_MECHANICAL_VISUAL: _KindSpec(
            "application/vnd.vibecad.visual-feature-graph+json",
            "pm1.role.visual-evidence",
            "vfg.schema.v1",
            "partdesign.planar-mechanical",
            ("add", "reference-profiles", "remove"),
            "planar_mechanical_visual",
        ),
    }
)


class ReviewedInputIngressErrorCode(StrEnum):
    """Stable path-free failures from the host-only ingress boundary."""

    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    AUTHORITY_VIOLATION = "authority_violation"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"
    CLOSED = "closed"
    CLEANUP_FAILED = "cleanup_failed"


class ReviewedInputIngressError(ValueError):
    """Non-reflective host ingress failure."""

    __slots__ = ("code",)

    def __init__(self, code: ReviewedInputIngressErrorCode) -> None:
        if type(code) is not ReviewedInputIngressErrorCode:
            raise TypeError("code must be a ReviewedInputIngressErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: ReviewedInputIngressErrorCode) -> None:
    raise ReviewedInputIngressError(code)


def _identifier(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 128 or _IDENTIFIER.fullmatch(value) is None:
        _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
    return value


def _canonical_json(value: object, *, maximum: int = _MANIFEST_MAX_BYTES) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
    if not raw or len(raw) > maximum:
        _fail(ReviewedInputIngressErrorCode.BUDGET_EXCEEDED)
    return raw


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedReviewedInputDescriptor:
    """Exact trusted attachment metadata; all route facts are derived from ``kind``."""

    kind: ReviewedInputKind
    content_sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if type(self.kind) is not ReviewedInputKind:
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
        object.__setattr__(self, "content_sha256", _digest(self.content_sha256))
        if (
            type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= MAX_REVIEWED_ARTIFACT_BYTES
        ):
            _fail(
                ReviewedInputIngressErrorCode.BUDGET_EXCEEDED
                if type(self.size_bytes) is int and self.size_bytes > MAX_REVIEWED_ARTIFACT_BYTES
                else ReviewedInputIngressErrorCode.INVALID_INPUT
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedReviewedInputBytes:
    """One trusted host attachment carried as exact bytes."""

    descriptor: TrustedReviewedInputDescriptor
    payload: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.descriptor) is not TrustedReviewedInputDescriptor
            or type(self.payload) is not bytes
        ):
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedReviewedInputFileDescriptor:
    """One trusted host attachment carried by a private regular-file FD."""

    descriptor: TrustedReviewedInputDescriptor
    fd: int = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.descriptor) is not TrustedReviewedInputDescriptor
            or type(self.fd) is not int
            or self.fd < 0
        ):
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)


type TrustedReviewedInput = TrustedReviewedInputBytes | TrustedReviewedInputFileDescriptor


def _copy_record(record: ReviewedArtifactCatalogRecord) -> ReviewedArtifactCatalogRecord:
    try:
        return ReviewedArtifactCatalogRecord(
            artifact_id=record.artifact_id,
            content_sha256=record.content_sha256,
            size_bytes=record.size_bytes,
            media_type=record.media_type,
            role_term_ref_id=record.role_term_ref_id,
            schema_term_ref_id=record.schema_term_ref_id,
            document_id=record.document_id,
            family_id=record.family_id,
            operation_ids=record.operation_ids,
            maximum_bytes=record.maximum_bytes,
        )
    except BaseException:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)


def _catalog_body(
    *,
    task_id: str,
    project_id: str,
    base_revision: str,
    records: tuple[ReviewedArtifactCatalogRecord, ...],
) -> dict[str, object]:
    return {
        "base_revision": base_revision,
        "project_id": project_id,
        "records": [record.to_mapping() for record in records],
        "schema_version": REVIEWED_INPUT_CATALOG_SCHEMA_VERSION,
        "task_id": task_id,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class SealedReviewedInputCatalog:
    """Path-free receipt returned to the trusted calling host."""

    task_id: str
    project_id: str
    base_revision: str
    records: tuple[ReviewedArtifactCatalogRecord, ...]
    catalog_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("task_id", "project_id", "base_revision"):
            object.__setattr__(self, name, _identifier(getattr(self, name)))
        if (
            type(self.records) is not tuple
            or not 1 <= len(self.records) <= MAX_REVIEWED_ARTIFACTS
            or any(type(record) is not ReviewedArtifactCatalogRecord for record in self.records)
        ):
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
        records = tuple(
            sorted(
                (_copy_record(record) for record in self.records),
                key=lambda item: item.artifact_id,
            )
        )
        if len({record.artifact_id for record in records}) != len(records):
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
        if sum(record.size_bytes for record in records) > MAX_REVIEWED_ARTIFACT_TOTAL_BYTES:
            _fail(ReviewedInputIngressErrorCode.BUDGET_EXCEEDED)
        body = _catalog_body(
            task_id=self.task_id,
            project_id=self.project_id,
            base_revision=self.base_revision,
            records=records,
        )
        body_raw = _canonical_json(body)
        catalog_sha256 = hashlib.sha256(_CATALOG_DIGEST_DOMAIN + body_raw).hexdigest()
        canonical = _canonical_json(body | {"catalog_sha256": catalog_sha256})
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "catalog_sha256", catalog_sha256)
        object.__setattr__(self, "canonical_bytes", canonical)

    def to_mapping(self) -> dict[str, object]:
        return _catalog_body(
            task_id=self.task_id,
            project_id=self.project_id,
            base_revision=self.base_revision,
            records=self.records,
        ) | {"catalog_sha256": self.catalog_sha256}


@runtime_checkable
class ReviewedInputIngressPort(Protocol):
    """Application-facing host port; intentionally absent from MCP."""

    def seal_reviewed_task_inputs(
        self,
        *,
        task_id: str,
        project_id: str,
        base_revision: str,
        inputs: tuple[TrustedReviewedInput, ...],
    ) -> SealedReviewedInputCatalog: ...

    def discard_reviewed_task_inputs(
        self,
        *,
        task_id: str,
        project_id: str,
        base_revision: str,
    ) -> None: ...


def _directory_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required):
        _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _file_read_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_CLOEXEC"):
        _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode))


def _valid_directory(value: os.stat_result, *, device: int | None = None) -> bool:
    return (
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.geteuid()
        and stat.S_IMODE(value.st_mode) == _DIRECTORY_MODE
        and (device is None or value.st_dev == device)
    )


def _valid_file(value: os.stat_result, *, device: int | None = None) -> bool:
    if sys.platform == "win32":
        return (
            stat.S_ISREG(value.st_mode)
            and value.st_nlink == 1
            and (device is None or value.st_dev == device)
        )
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == _FILE_MODE
        and (device is None or value.st_dev == device)
    )


def _catalog_name(task_id: str, base_revision: str) -> str:
    raw = _canonical_json(
        {"base_revision": _identifier(base_revision), "task_id": _identifier(task_id)},
        maximum=1024,
    )
    return "catalog_" + hashlib.sha256(_CATALOG_BINDING_DOMAIN + raw).hexdigest()


def _artifact_id(kind: ReviewedInputKind, digest: str) -> str:
    return (
        "artifact_"
        + hashlib.sha256(
            _ARTIFACT_ID_DOMAIN + kind.value.encode("ascii") + b"\0" + digest.encode("ascii")
        ).hexdigest()[:32]
    )


def _record(descriptor: TrustedReviewedInputDescriptor) -> ReviewedArtifactCatalogRecord:
    spec = _KIND_SPECS[descriptor.kind]
    return ReviewedArtifactCatalogRecord(
        artifact_id=_artifact_id(descriptor.kind, descriptor.content_sha256),
        content_sha256=descriptor.content_sha256,
        size_bytes=descriptor.size_bytes,
        media_type=spec.media_type,
        role_term_ref_id=spec.role_term_ref_id,
        schema_term_ref_id=spec.schema_term_ref_id,
        document_id=f"{spec.document_prefix}_{descriptor.content_sha256[:32]}",
        family_id=spec.family_id,
        operation_ids=spec.operation_ids,
        maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
    )


def _validate_record_authority(
    record: ReviewedArtifactCatalogRecord,
    payload: bytes,
) -> None:
    selected: ReviewedInputKind | None = None
    for kind, spec in _KIND_SPECS.items():
        if (
            record.media_type == spec.media_type
            and record.role_term_ref_id == spec.role_term_ref_id
            and record.schema_term_ref_id == spec.schema_term_ref_id
            and record.family_id == spec.family_id
            and record.operation_ids == spec.operation_ids
            and record.maximum_bytes == MAX_REVIEWED_ARTIFACT_BYTES
            and record.document_id == f"{spec.document_prefix}_{record.content_sha256[:32]}"
            and record.artifact_id == _artifact_id(kind, record.content_sha256)
        ):
            selected = kind
            break
    if selected is None:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    _validate_media(selected, payload)


def _validate_media(kind: ReviewedInputKind, payload: bytes) -> None:
    try:
        if kind in {ReviewedInputKind.PNG, ReviewedInputKind.JPEG}:
            from vibecad.parametric.freecad_imageplane_rules import (
                validate_imageplane_artifact_payload,
            )

            validate_imageplane_artifact_payload(payload, _KIND_SPECS[kind].media_type)
            return
        if kind is ReviewedInputKind.PLANAR_MECHANICAL_VISUAL:
            from vibecad.intent_rules.planar_mechanical_v1.rule_set import (  # noqa: PLC0415
                analyze_visual_feature_graph,
            )
            from vibecad.visual.feature_graph import (  # noqa: PLC0415
                decode_visual_feature_graph,
                encode_visual_feature_graph,
            )

            graph = decode_visual_feature_graph(payload)
            if (
                not hmac.compare_digest(payload, encode_visual_feature_graph(graph))
                or analyze_visual_feature_graph(graph) is None
            ):
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
            return
    except BaseException:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    if kind is ReviewedInputKind.STEP:
        stripped = payload.lstrip()
        if not stripped.startswith(b"ISO-10303-21;") or b"END-ISO-10303-21;" not in stripped:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    elif kind is ReviewedInputKind.BREP:
        if not (
            payload.startswith(b"DBRep_DrawableShape") or payload.startswith(b"CASCADE Topology V1")
        ):
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    elif kind is ReviewedInputKind.IGES:
        if len(payload) < 80 or payload[72:73] != b"S" or b"T" not in payload[72::80]:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)


def _read_source(source: TrustedReviewedInput) -> tuple[ReviewedArtifactCatalogRecord, bytes]:
    if type(source) is TrustedReviewedInputBytes:
        descriptor = source.descriptor
        payload = source.payload
    elif type(source) is TrustedReviewedInputFileDescriptor:
        descriptor = source.descriptor
        fd = source.fd
        try:
            before = os.fstat(fd)
            _file_compat.require_read_only(fd)
            windows_capability = (
                _file_compat.capture_windows_fd(fd, directory=False)
                if sys.platform == "win32"
                else None
            )
            if (
                not _valid_file(before)
                or before.st_size != descriptor.size_bytes
                or before.st_size > MAX_REVIEWED_ARTIFACT_BYTES
            ):
                _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
            chunks: list[bytes] = []
            offset = 0
            remaining = before.st_size
            while remaining:
                chunk = _file_compat.pread(fd, min(_READ_CHUNK_BYTES, remaining), offset)
                if not chunk:
                    _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
                chunks.append(chunk)
                offset += len(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(fd)
            if (
                _directory_identity(after) != _directory_identity(before)
                or after.st_nlink != before.st_nlink
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns
            ):
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
            if windows_capability is not None:
                current_capability = _file_compat.capture_windows_fd(
                    fd,
                    directory=False,
                    generation_token=windows_capability.generation_token,
                )
                if current_capability != windows_capability:
                    _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        except ReviewedInputIngressError:
            raise
        except (OSError, OverflowError):
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
    else:
        _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
    if len(payload) != descriptor.size_bytes or not hmac.compare_digest(
        hashlib.sha256(payload).hexdigest(), descriptor.content_sha256
    ):
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    _validate_media(descriptor.kind, payload)
    return _record(descriptor), payload


def _write_file(directory_fd: int, name: str, payload: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            _FILE_MODE,
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset : offset + _READ_CHUNK_BYTES])
            if written <= 0:
                _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
            offset += written
        os.fsync(descriptor)
        current = os.fstat(descriptor)
        if not _valid_file(current) or current.st_size != len(payload):
            _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
    except ReviewedInputIngressError:
        raise
    except OSError:
        _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _rename_directory_noreplace(parent_fd: int, source: str, destination: str) -> None:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            operation = library.renameatx_np
            operation.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            arguments = (
                parent_fd,
                source.encode("ascii"),
                parent_fd,
                destination.encode("ascii"),
                4,
            )
        elif sys.platform.startswith("linux"):
            operation = library.renameat2
            operation.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            arguments = (
                parent_fd,
                source.encode("ascii"),
                parent_fd,
                destination.encode("ascii"),
                1,
            )
        else:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        operation.restype = ctypes.c_int
        ctypes.set_errno(0)
        if operation(*arguments) != 0:
            code = ctypes.get_errno() or errno.EIO
            if code in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(code, "destination exists")
            raise OSError(code, "atomic no-replace rename failed")
    except (AttributeError, UnicodeError):
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable") from None


def _remove_flat_directory(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        current = os.fstat(descriptor)
        if not _valid_directory(current) or (
            expected_identity is not None and _directory_identity(current) != expected_identity
        ):
            _fail(ReviewedInputIngressErrorCode.CLEANUP_FAILED)
        for entry in os.listdir(descriptor):
            value = os.stat(entry, dir_fd=descriptor, follow_symlinks=False)
            if not _valid_file(value, device=current.st_dev):
                _fail(ReviewedInputIngressErrorCode.CLEANUP_FAILED)
            os.unlink(entry, dir_fd=descriptor)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        return
    except ReviewedInputIngressError:
        raise
    except OSError:
        _fail(ReviewedInputIngressErrorCode.CLEANUP_FAILED)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _record_from_mapping(value: object) -> ReviewedArtifactCatalogRecord:
    fields = {
        "artifact_id",
        "content_sha256",
        "document_id",
        "family_id",
        "maximum_bytes",
        "media_type",
        "operation_ids",
        "role_term_ref_id",
        "schema_term_ref_id",
        "size_bytes",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or type(value.get("operation_ids")) is not list
    ):
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    try:
        return ReviewedArtifactCatalogRecord(
            artifact_id=value["artifact_id"],
            content_sha256=value["content_sha256"],
            document_id=value["document_id"],
            family_id=value["family_id"],
            maximum_bytes=value["maximum_bytes"],
            media_type=value["media_type"],
            operation_ids=tuple(value["operation_ids"]),
            role_term_ref_id=value["role_term_ref_id"],
            schema_term_ref_id=value["schema_term_ref_id"],
            size_bytes=value["size_bytes"],
        )
    except BaseException:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)


def _decode_catalog(raw: bytes) -> SealedReviewedInputCatalog:
    if type(raw) is not bytes or not raw or len(raw) > _MANIFEST_MAX_BYTES:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    fields = {
        "base_revision",
        "catalog_sha256",
        "project_id",
        "records",
        "schema_version",
        "task_id",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or value.get("schema_version") != REVIEWED_INPUT_CATALOG_SCHEMA_VERSION
        or type(value.get("records")) is not list
    ):
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    try:
        receipt = SealedReviewedInputCatalog(
            task_id=value["task_id"],
            project_id=value["project_id"],
            base_revision=value["base_revision"],
            records=tuple(_record_from_mapping(record) for record in value["records"]),
        )
    except ReviewedInputIngressError:
        raise
    except BaseException:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    if (
        type(value["catalog_sha256"]) is not str
        or not hmac.compare_digest(value["catalog_sha256"], receipt.catalog_sha256)
        or not hmac.compare_digest(raw, receipt.canonical_bytes)
    ):
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    return receipt


def _read_regular(
    directory_fd: int,
    name: str,
    *,
    device: int,
    maximum: int,
) -> bytes:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        descriptor = os.open(name, _file_read_flags(), dir_fd=directory_fd)
        opened = os.fstat(descriptor)
        if (
            not _valid_file(before, device=device)
            or not _valid_file(opened, device=device)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or not 1 <= opened.st_size <= maximum
        ):
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            _directory_identity(after) != _directory_identity(opened)
            or after.st_nlink != opened.st_nlink
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
        ):
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        return b"".join(chunks)
    except ReviewedInputIngressError:
        raise
    except OSError:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _windows_write_file(directory: Path, name: str, payload: bytes) -> None:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
    path = directory / name
    descriptor = -1
    try:
        descriptor = os.open(
            _file_compat.windows_extended_path(path),
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOINHERIT", 0),
            _FILE_MODE,
        )
        _file_compat.set_private_dacl(path)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset : offset + _READ_CHUNK_BYTES])
            if written <= 0:
                _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
            offset += written
        os.fsync(descriptor)
        capability = _file_compat.capture_windows_fd(descriptor, directory=False)
        current = os.fstat(descriptor)
        if current.st_size != len(payload) or (current.st_dev, current.st_ino) != (
            capability.volume,
            capability.file_id,
        ):
            _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
    except ReviewedInputIngressError:
        raise
    except OSError:
        _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _windows_read_regular(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        before = _file_compat.capture_windows_path(path, directory=False)
        descriptor = os.open(
            _file_compat.windows_extended_path(path),
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0),
        )
        _file_compat.require_read_only(descriptor)
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (
            before.volume,
            before.file_id,
        ) or not 1 <= opened.st_size <= maximum:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
            chunks.append(chunk)
            remaining -= len(chunk)
        after = _file_compat.capture_windows_path(
            path,
            directory=False,
            generation_token=before.generation_token,
        )
        current = os.fstat(descriptor)
        if after != before or (current.st_dev, current.st_ino, current.st_size) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        return b"".join(chunks)
    except ReviewedInputIngressError:
        raise
    except OSError:
        _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)


def _windows_remove_flat_directory(
    capability: WindowsPathCapability,
    *,
    expected_names: set[str] | None = None,
) -> None:
    try:
        path = _file_compat.validate_windows_path(capability, directory=True)
        names = {item.name for item in path.iterdir()}
        if expected_names is not None and names != expected_names:
            _fail(ReviewedInputIngressErrorCode.CLEANUP_FAILED)
        for name in names:
            item = path / name
            _file_compat.capture_windows_path(item, directory=False)
            os.unlink(_file_compat.windows_extended_path(item))
        os.rmdir(_file_compat.windows_extended_path(path))
    except FileNotFoundError:
        return
    except ReviewedInputIngressError:
        raise
    except OSError:
        _fail(ReviewedInputIngressErrorCode.CLEANUP_FAILED)


class _WindowsReviewedInputBackend:
    __slots__ = ("_closed", "_creator_pid", "_parent", "_root")

    def __init__(
        self,
        *,
        application_root: Path,
        expected_root_identity: tuple[int, int],
    ) -> None:
        try:
            parent = _file_compat.capture_windows_path(application_root, directory=True)
            if (parent.volume, parent.file_id) != expected_root_identity:
                raise OSError
        except OSError:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        self._parent = parent
        self._root: WindowsPathCapability | None = None
        self._creator_pid = os.getpid()
        self._closed = False

    def require_live(self) -> Path:
        if self._closed or self._creator_pid != os.getpid():
            _fail(ReviewedInputIngressErrorCode.CLOSED)
        try:
            return _file_compat.validate_windows_path(self._parent, directory=True)
        except OSError:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)

    def open_root(self, *, create: bool) -> tuple[Path, WindowsPathCapability]:
        parent = self.require_live()
        path = parent / REVIEWED_INPUT_CATALOG_DIRECTORY
        try:
            if create and not path.exists():
                try:
                    path.mkdir()
                    _file_compat.set_private_dacl(path)
                except FileExistsError:
                    pass
            token = self._root.generation_token if self._root is not None else None
            capability = _file_compat.capture_windows_path(
                path,
                directory=True,
                generation_token=token,
            )
            if capability.volume != self._parent.volume:
                raise OSError
            if self._root is None:
                self._root = capability
            elif capability != self._root:
                raise OSError
            return path, capability
        except FileNotFoundError:
            _fail(ReviewedInputIngressErrorCode.NOT_FOUND)
        except OSError:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)

    def load_catalog(
        self,
        root: Path,
        *,
        name: str,
        task_id: str,
        project_id: str | None,
        base_revision: str,
    ) -> tuple[SealedReviewedInputCatalog, WindowsPathCapability]:
        path = root / name
        try:
            capability = _file_compat.capture_windows_path(path, directory=True)
            names = {item.name for item in path.iterdir()}
            raw = _windows_read_regular(
                path / REVIEWED_INPUT_CATALOG_MANIFEST,
                maximum=_MANIFEST_MAX_BYTES,
            )
            receipt = _decode_catalog(raw)
            if (
                receipt.task_id != task_id
                or receipt.base_revision != base_revision
                or (project_id is not None and receipt.project_id != project_id)
                or names
                != {
                    REVIEWED_INPUT_CATALOG_MANIFEST,
                    *(record.artifact_id for record in receipt.records),
                }
            ):
                _fail(ReviewedInputIngressErrorCode.AUTHORITY_VIOLATION)
            for record in receipt.records:
                payload = _windows_read_regular(
                    path / record.artifact_id,
                    maximum=record.maximum_bytes,
                )
                if len(payload) != record.size_bytes or not hmac.compare_digest(
                    hashlib.sha256(payload).hexdigest(), record.content_sha256
                ):
                    _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
                _validate_record_authority(record, payload)
            _file_compat.validate_windows_path(capability, directory=True)
            return receipt, capability
        except FileNotFoundError:
            _fail(ReviewedInputIngressErrorCode.NOT_FOUND)
        except ReviewedInputIngressError:
            raise
        except OSError:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)

    def seal(
        self,
        *,
        binding: tuple[str, str, str],
        receipt: SealedReviewedInputCatalog,
        loaded: tuple[tuple[ReviewedArtifactCatalogRecord, bytes], ...],
    ) -> SealedReviewedInputCatalog:
        by_id = {record.artifact_id: payload for record, payload in loaded}
        name = _catalog_name(binding[0], binding[2])
        stage_name = f".run_{receipt.catalog_sha256}.{secrets.token_hex(16)}.tmp"
        root, _ = self.open_root(create=True)
        stage = root / stage_name
        stage_capability: WindowsPathCapability | None = None
        try:
            try:
                existing, _ = self.load_catalog(
                    root,
                    name=name,
                    task_id=binding[0],
                    project_id=binding[1],
                    base_revision=binding[2],
                )
            except ReviewedInputIngressError as error:
                if error.code is not ReviewedInputIngressErrorCode.NOT_FOUND:
                    raise
            else:
                if not hmac.compare_digest(existing.catalog_sha256, receipt.catalog_sha256):
                    _fail(ReviewedInputIngressErrorCode.CONFLICT)
                return existing
            stage.mkdir()
            _file_compat.set_private_dacl(stage)
            stage_capability = _file_compat.capture_windows_path(stage, directory=True)
            for record in receipt.records:
                _windows_write_file(stage, record.artifact_id, by_id[record.artifact_id])
            _windows_write_file(stage, REVIEWED_INPUT_CATALOG_MANIFEST, receipt.canonical_bytes)
            try:
                stage.rename(root / name)
            except FileExistsError:
                _windows_remove_flat_directory(stage_capability)
                stage_capability = None
                existing, _ = self.load_catalog(
                    root,
                    name=name,
                    task_id=binding[0],
                    project_id=binding[1],
                    base_revision=binding[2],
                )
                if not hmac.compare_digest(existing.catalog_sha256, receipt.catalog_sha256):
                    _fail(ReviewedInputIngressErrorCode.CONFLICT)
                return existing
            stage_capability = None
            verified, _ = self.load_catalog(
                root,
                name=name,
                task_id=binding[0],
                project_id=binding[1],
                base_revision=binding[2],
            )
            if not hmac.compare_digest(verified.catalog_sha256, receipt.catalog_sha256):
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
            return verified
        except ReviewedInputIngressError:
            if stage_capability is not None:
                with contextlib.suppress(ReviewedInputIngressError):
                    _windows_remove_flat_directory(stage_capability)
            raise
        except OSError:
            if stage_capability is not None:
                with contextlib.suppress(ReviewedInputIngressError):
                    _windows_remove_flat_directory(stage_capability)
            _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)

    def acquire(
        self,
        *,
        task: str,
        project: str,
        base: str,
        run: str,
    ) -> TaskInputSnapshotLease:
        root, root_capability = self.open_root(create=False)
        name = _catalog_name(task, base)
        receipt, _ = self.load_catalog(
            root,
            name=name,
            task_id=task,
            project_id=project,
            base_revision=base,
        )
        snapshot = ReviewedArtifactCatalogSnapshot(
            task_id=task,
            project_id=project,
            base_revision=base,
            run_id=run,
            records=receipt.records,
        )
        run_name = (
            ".run_"
            + hashlib.sha256(
                _RUN_NAME_DOMAIN
                + _canonical_json(
                    {
                        "base_revision": base,
                        "project_id": project,
                        "run_id": run,
                        "task_id": task,
                    },
                    maximum=2048,
                )
            ).hexdigest()
            + f".{secrets.token_hex(16)}.tmp"
        )
        path = root / run_name
        capability: WindowsPathCapability | None = None
        transferred = False
        try:
            path.mkdir()
            _file_compat.set_private_dacl(path)
            capability = _file_compat.capture_windows_path(path, directory=True)
            catalog = root / name
            for record in snapshot.records:
                payload = _windows_read_regular(
                    catalog / record.artifact_id,
                    maximum=record.maximum_bytes,
                )
                _windows_write_file(path, record.artifact_id, payload)
            _windows_write_file(
                path,
                REVIEWED_ARTIFACT_MANIFEST_NAME,
                _canonical_json(snapshot.to_mapping()),
            )
            lease = TaskInputSnapshotLease(
                snapshot=snapshot,
                directory_capability=capability,
                cleanup_parent_capability=root_capability,
                cleanup_name=run_name,
            )
            transferred = True
            return lease
        except ReviewedInputIngressError:
            raise
        except BaseException:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        finally:
            if not transferred and capability is not None:
                with contextlib.suppress(ReviewedInputIngressError):
                    _windows_remove_flat_directory(capability)

    def discard(self, *, task: str, project: str, base: str) -> None:
        try:
            root, _ = self.open_root(create=False)
            _, capability = self.load_catalog(
                root,
                name=_catalog_name(task, base),
                task_id=task,
                project_id=project,
                base_revision=base,
            )
        except ReviewedInputIngressError as error:
            if error.code is ReviewedInputIngressErrorCode.NOT_FOUND:
                return
            raise
        _windows_remove_flat_directory(capability)

    def close(self) -> None:
        self._closed = True


class ReviewedInputCatalogStore:
    """Identity-pinned host catalog and TaskInputSnapshotProvider implementation."""

    __slots__ = (
        "_closed",
        "_creator_pid",
        "_lock",
        "_parent_fd",
        "_parent_identity",
        "_root_identity",
        "_windows_backend",
    )

    def __init__(
        self,
        *,
        application_root: Path,
        expected_root_identity: tuple[int, int],
    ) -> None:
        if (
            type(application_root) is not type(Path("/"))
            or not application_root.is_absolute()
            or type(expected_root_identity) is not tuple
            or len(expected_root_identity) != 2
            or any(type(item) is not int for item in expected_root_identity)
        ):
            raise TypeError("invalid reviewed input store composition")
        self._windows_backend: _WindowsReviewedInputBackend | None = None
        self._creator_pid = os.getpid()
        self._closed = False
        self._lock = threading.RLock()
        if sys.platform == "win32":
            self._windows_backend = _WindowsReviewedInputBackend(
                application_root=application_root,
                expected_root_identity=expected_root_identity,
            )
            self._parent_fd = -1
            self._parent_identity = (0, 0, 0, 0)
            self._root_identity = None
            return
        parent_fd = -1
        try:
            parent_fd = os.open(application_root, _directory_flags())
            parent = os.fstat(parent_fd)
            if (
                not _valid_directory(parent)
                or (parent.st_dev, parent.st_ino) != expected_root_identity
            ):
                raise OSError
        except ReviewedInputIngressError:
            if parent_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(parent_fd)
            raise
        except OSError:
            if parent_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(parent_fd)
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        self._parent_fd = parent_fd
        self._parent_identity = _directory_identity(parent)
        self._root_identity: tuple[int, int, int, int] | None = None

    def _require_live(self) -> None:
        if self._closed or os.getpid() != self._creator_pid:
            _fail(ReviewedInputIngressErrorCode.CLOSED)
        try:
            if _directory_identity(os.fstat(self._parent_fd)) != self._parent_identity:
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
        except OSError:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)

    def _open_root(self, *, create: bool) -> int:
        self._require_live()
        try:
            if create:
                try:
                    os.mkdir(
                        REVIEWED_INPUT_CATALOG_DIRECTORY,
                        _DIRECTORY_MODE,
                        dir_fd=self._parent_fd,
                    )
                except FileExistsError:
                    pass
            descriptor = os.open(
                REVIEWED_INPUT_CATALOG_DIRECTORY,
                _directory_flags(),
                dir_fd=self._parent_fd,
            )
            current = os.fstat(descriptor)
            if not _valid_directory(current, device=self._parent_identity[0]):
                raise OSError
            identity = _directory_identity(current)
            if self._root_identity is None:
                self._root_identity = identity
            elif identity != self._root_identity:
                raise OSError
            return descriptor
        except FileNotFoundError:
            _fail(ReviewedInputIngressErrorCode.NOT_FOUND)
        except OSError:
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)

    def _load_catalog(
        self,
        root_fd: int,
        *,
        name: str,
        task_id: str,
        project_id: str | None,
        base_revision: str,
    ) -> tuple[SealedReviewedInputCatalog, int]:
        descriptor = -1
        try:
            descriptor = os.open(name, _directory_flags(), dir_fd=root_fd)
            directory = os.fstat(descriptor)
            if not _valid_directory(directory, device=os.fstat(root_fd).st_dev):
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
            names = set(os.listdir(descriptor))
            raw = _read_regular(
                descriptor,
                REVIEWED_INPUT_CATALOG_MANIFEST,
                device=directory.st_dev,
                maximum=_MANIFEST_MAX_BYTES,
            )
            receipt = _decode_catalog(raw)
            if (
                receipt.task_id != task_id
                or receipt.base_revision != base_revision
                or (project_id is not None and receipt.project_id != project_id)
                or names
                != {
                    REVIEWED_INPUT_CATALOG_MANIFEST,
                    *(record.artifact_id for record in receipt.records),
                }
            ):
                _fail(ReviewedInputIngressErrorCode.AUTHORITY_VIOLATION)
            for record in receipt.records:
                payload = _read_regular(
                    descriptor,
                    record.artifact_id,
                    device=directory.st_dev,
                    maximum=record.maximum_bytes,
                )
                if len(payload) != record.size_bytes or not hmac.compare_digest(
                    hashlib.sha256(payload).hexdigest(), record.content_sha256
                ):
                    _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
                _validate_record_authority(record, payload)
            return receipt, descriptor
        except FileNotFoundError:
            _fail(ReviewedInputIngressErrorCode.NOT_FOUND)
        except ReviewedInputIngressError:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise
        except OSError:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)

    def seal(
        self,
        *,
        task_id: str,
        project_id: str,
        base_revision: str,
        inputs: tuple[TrustedReviewedInput, ...],
    ) -> SealedReviewedInputCatalog:
        """Seal exact host attachments without accepting paths, labels, or store keys."""

        binding = tuple(_identifier(item) for item in (task_id, project_id, base_revision))
        if (
            type(inputs) is not tuple
            or not 1 <= len(inputs) <= MAX_REVIEWED_ARTIFACTS
            or any(
                type(item) not in {TrustedReviewedInputBytes, TrustedReviewedInputFileDescriptor}
                for item in inputs
            )
        ):
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
        loaded = tuple(_read_source(item) for item in inputs)
        if sum(len(payload) for _, payload in loaded) > MAX_REVIEWED_ARTIFACT_TOTAL_BYTES:
            _fail(ReviewedInputIngressErrorCode.BUDGET_EXCEEDED)
        receipt = SealedReviewedInputCatalog(
            task_id=binding[0],
            project_id=binding[1],
            base_revision=binding[2],
            records=tuple(record for record, _ in loaded),
        )
        if len({record.artifact_id for record in receipt.records}) != len(receipt.records):
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
        by_id = {record.artifact_id: payload for record, payload in loaded}
        name = _catalog_name(binding[0], binding[2])
        stage = f".run_{receipt.catalog_sha256}.{secrets.token_hex(16)}.tmp"
        root_fd = -1
        stage_fd = -1
        with self._lock:
            if self._windows_backend is not None:
                return self._windows_backend.seal(
                    binding=binding,
                    receipt=receipt,
                    loaded=loaded,
                )
            try:
                root_fd = self._open_root(create=True)
                try:
                    existing, existing_fd = self._load_catalog(
                        root_fd,
                        name=name,
                        task_id=binding[0],
                        project_id=binding[1],
                        base_revision=binding[2],
                    )
                except ReviewedInputIngressError as error:
                    if error.code is not ReviewedInputIngressErrorCode.NOT_FOUND:
                        raise
                else:
                    os.close(existing_fd)
                    if not hmac.compare_digest(existing.catalog_sha256, receipt.catalog_sha256):
                        _fail(ReviewedInputIngressErrorCode.CONFLICT)
                    return existing
                os.mkdir(stage, _DIRECTORY_MODE, dir_fd=root_fd)
                stage_fd = os.open(stage, _directory_flags(), dir_fd=root_fd)
                for record in receipt.records:
                    _write_file(stage_fd, record.artifact_id, by_id[record.artifact_id])
                _write_file(stage_fd, REVIEWED_INPUT_CATALOG_MANIFEST, receipt.canonical_bytes)
                os.fsync(stage_fd)
                os.close(stage_fd)
                stage_fd = -1
                try:
                    _rename_directory_noreplace(root_fd, stage, name)
                except FileExistsError:
                    _remove_flat_directory(root_fd, stage)
                    existing, existing_fd = self._load_catalog(
                        root_fd,
                        name=name,
                        task_id=binding[0],
                        project_id=binding[1],
                        base_revision=binding[2],
                    )
                    os.close(existing_fd)
                    if not hmac.compare_digest(existing.catalog_sha256, receipt.catalog_sha256):
                        _fail(ReviewedInputIngressErrorCode.CONFLICT)
                    return existing
                os.fsync(root_fd)
                verified, verified_fd = self._load_catalog(
                    root_fd,
                    name=name,
                    task_id=binding[0],
                    project_id=binding[1],
                    base_revision=binding[2],
                )
                os.close(verified_fd)
                if not hmac.compare_digest(verified.catalog_sha256, receipt.catalog_sha256):
                    _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
                return verified
            except ReviewedInputIngressError:
                if root_fd >= 0:
                    with contextlib.suppress(ReviewedInputIngressError):
                        _remove_flat_directory(root_fd, stage)
                raise
            except OSError:
                if root_fd >= 0:
                    with contextlib.suppress(ReviewedInputIngressError):
                        _remove_flat_directory(root_fd, stage)
                _fail(ReviewedInputIngressErrorCode.STORE_FAILURE)
            finally:
                if stage_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(stage_fd)
                if root_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(root_fd)

    def requires_artifact_snapshot(self, program: ValidatedProgram) -> bool:
        """Preflight only authenticated programs and a verified task/base catalog."""

        if type(program) is not ValidatedProgram:
            _fail(ReviewedInputIngressErrorCode.INVALID_INPUT)
        from vibecad.execution.freecad_reviewed_intent_execution import (
            REVIEWED_IMAGEPLANE_ROUTES,
            REVIEWED_PART_FILE_IMPORT_ROUTES,
            REVIEWED_PLANAR_MECHANICAL_ROUTES,
        )

        artifact_route_identities = {
            (route.operation_id, route.semantic_operation)
            for route in (
                *REVIEWED_PART_FILE_IMPORT_ROUTES,
                *REVIEWED_IMAGEPLANE_ROUTES,
                *REVIEWED_PLANAR_MECHANICAL_ROUTES,
            )
        }
        if not any(
            command.operation == "apply_reviewed_intent"
            and isinstance(intent := command.handler_kwargs.get("intent"), Mapping)
            and (intent.get("operation_id"), intent.get("semantic_operation"))
            in artifact_route_identities
            for command in program.commands
        ):
            return False
        source = program.program
        name = _catalog_name(source.task_id, source.base_revision)
        root_fd = -1
        catalog_fd = -1
        with self._lock:
            if self._windows_backend is not None:
                try:
                    root, _ = self._windows_backend.open_root(create=False)
                    self._windows_backend.load_catalog(
                        root,
                        name=name,
                        task_id=source.task_id,
                        project_id=None,
                        base_revision=source.base_revision,
                    )
                    return True
                except ReviewedInputIngressError as error:
                    if error.code is ReviewedInputIngressErrorCode.NOT_FOUND:
                        return False
                    raise
            try:
                root_fd = self._open_root(create=False)
                _, catalog_fd = self._load_catalog(
                    root_fd,
                    name=name,
                    task_id=source.task_id,
                    project_id=None,
                    base_revision=source.base_revision,
                )
                return True
            except ReviewedInputIngressError as error:
                if error.code is ReviewedInputIngressErrorCode.NOT_FOUND:
                    return False
                raise
            finally:
                if catalog_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(catalog_fd)
                if root_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(root_fd)

    def acquire(
        self,
        *,
        task_id: str,
        project_id: str,
        base_revision: str,
        run_id: str,
    ) -> TaskInputSnapshotLease:
        """Materialize and lease one exact run-bound immutable snapshot."""

        task, project, base, run = tuple(
            _identifier(item) for item in (task_id, project_id, base_revision, run_id)
        )
        name = _catalog_name(task, base)
        root_fd = -1
        catalog_fd = -1
        run_fd = -1
        lease_owns_snapshot = False
        run_name = (
            ".run_"
            + hashlib.sha256(
                _RUN_NAME_DOMAIN
                + _canonical_json(
                    {
                        "base_revision": base,
                        "project_id": project,
                        "run_id": run,
                        "task_id": task,
                    },
                    maximum=2048,
                )
            ).hexdigest()
            + f".{secrets.token_hex(16)}.tmp"
        )
        with self._lock:
            if self._windows_backend is not None:
                return self._windows_backend.acquire(
                    task=task,
                    project=project,
                    base=base,
                    run=run,
                )
            try:
                root_fd = self._open_root(create=False)
                receipt, catalog_fd = self._load_catalog(
                    root_fd,
                    name=name,
                    task_id=task,
                    project_id=project,
                    base_revision=base,
                )
                snapshot = ReviewedArtifactCatalogSnapshot(
                    task_id=task,
                    project_id=project,
                    base_revision=base,
                    run_id=run,
                    records=receipt.records,
                )
                os.mkdir(run_name, _DIRECTORY_MODE, dir_fd=root_fd)
                run_fd = os.open(run_name, _directory_flags(), dir_fd=root_fd)
                for record in snapshot.records:
                    payload = _read_regular(
                        catalog_fd,
                        record.artifact_id,
                        device=os.fstat(catalog_fd).st_dev,
                        maximum=record.maximum_bytes,
                    )
                    _write_file(run_fd, record.artifact_id, payload)
                manifest = _canonical_json(snapshot.to_mapping())
                _write_file(run_fd, REVIEWED_ARTIFACT_MANIFEST_NAME, manifest)
                os.fsync(run_fd)
                lease = TaskInputSnapshotLease(
                    snapshot=snapshot,
                    directory_fd=run_fd,
                    cleanup_parent_fd=root_fd,
                    cleanup_name=run_name,
                )
                lease_owns_snapshot = True
                return lease
            except ReviewedInputIngressError:
                raise
            except BaseException:
                _fail(ReviewedInputIngressErrorCode.INTEGRITY_FAILURE)
            finally:
                if (
                    not lease_owns_snapshot
                    and root_fd >= 0
                    and _RUN_NAME.fullmatch(run_name) is not None
                ):
                    # TaskInputSnapshotLease owns successful cleanup.  Before
                    # ownership transfer, remove every incomplete run snapshot.
                    with contextlib.suppress(ReviewedInputIngressError):
                        _remove_flat_directory(root_fd, run_name)
                if run_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(run_fd)
                if catalog_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(catalog_fd)
                if root_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(root_fd)

    def discard(
        self,
        *,
        task_id: str,
        project_id: str,
        base_revision: str,
    ) -> None:
        """Remove one exact durable catalog after cancellation or terminal use."""

        task, project, base = tuple(
            _identifier(item) for item in (task_id, project_id, base_revision)
        )
        name = _catalog_name(task, base)
        root_fd = -1
        catalog_fd = -1
        with self._lock:
            if self._windows_backend is not None:
                self._windows_backend.discard(task=task, project=project, base=base)
                return
            try:
                try:
                    root_fd = self._open_root(create=False)
                    _, catalog_fd = self._load_catalog(
                        root_fd,
                        name=name,
                        task_id=task,
                        project_id=project,
                        base_revision=base,
                    )
                except ReviewedInputIngressError as error:
                    if error.code is ReviewedInputIngressErrorCode.NOT_FOUND:
                        return
                    raise
                identity = _directory_identity(os.fstat(catalog_fd))
                os.close(catalog_fd)
                catalog_fd = -1
                _remove_flat_directory(root_fd, name, expected_identity=identity)
            finally:
                if catalog_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(catalog_fd)
                if root_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(root_fd)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._windows_backend is not None:
                self._windows_backend.close()
                return
            descriptor = self._parent_fd
            self._parent_fd = -1
            try:
                os.close(descriptor)
            except OSError:
                _fail(ReviewedInputIngressErrorCode.CLEANUP_FAILED)


__all__ = (
    "REVIEWED_INPUT_CATALOG_DIRECTORY",
    "REVIEWED_INPUT_CATALOG_MANIFEST",
    "REVIEWED_INPUT_CATALOG_SCHEMA_VERSION",
    "ReviewedInputCatalogStore",
    "ReviewedInputIngressError",
    "ReviewedInputIngressErrorCode",
    "ReviewedInputIngressPort",
    "ReviewedInputKind",
    "SealedReviewedInputCatalog",
    "TrustedReviewedInputBytes",
    "TrustedReviewedInputDescriptor",
    "TrustedReviewedInputFileDescriptor",
)
