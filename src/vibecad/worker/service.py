"""Child-side FreeCAD owner for the private Worker protocol."""

from __future__ import annotations

import array
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vibecad._file_compat import WindowsPathCapability
from vibecad.execution.adapter import AdapterError, _ValidatedProgramExecution
from vibecad.execution.candidate import ActiveCandidate, SessionBinding
from vibecad.execution.executor import (
    ExecutorError,
    ExecutorErrorCode,
    InProcessCadExecutor,
    _bom_observation,
    _component_observations,
    _entity_observations,
    _export_session_step,
    _interference_observations,
    _shape_observation,
)
from vibecad.execution.freecad_reviewed_artifact_inputs import (
    MAX_REVIEWED_ARTIFACT_BYTES,
    MAX_REVIEWED_ARTIFACTS,
    ReviewedArtifactCatalogRecord,
    ReviewedArtifactCatalogSnapshot,
    ReviewedArtifactInputError,
    ReviewedArtifactInputErrorCode,
    _ReviewedArtifactRunResolver,
)
from vibecad.execution.revisions import ProjectHead
from vibecad.freecad_env import prepare_freecad_import
from vibecad.freeform.compiler import FreeformCompileError, compile_freeform
from vibecad.freeform.contracts import FreeformContractError, FreeformDesign
from vibecad.interaction.cad import (
    ValidatedImportEvidence,
    ValidatedMaterializationEvidence,
)
from vibecad.parametric.freecad_imageplane_rules import HostOwnedImageStager
from vibecad.parametric.freecad_part_file_import_rules import HostOwnedImportStager
from vibecad.worker import windows_files as _windows_files
from vibecad.worker.codec import (
    MAX_WORKER_REQUEST_BYTES,
    WorkerCodecError,
    WorkerWireErrorCode,
    decode_worker_request,
    encode_worker_response,
    error_response,
    success_response,
)
from vibecad.workflow.contracts import ModelProgram

_CANDIDATE = re.compile(r"worker_candidate_[0-9a-f]{32}\Z")
_WORKER_REVISION = re.compile(r"worker_revision_[0-9a-f]{32}\Z")
_SESSION = re.compile(r"worker_session_[0-9a-f]{32}\Z")
_PROGRAM = re.compile(r"worker_program_[0-9a-f]{32}\Z")
_PROJECT = re.compile(r"project_[0-9a-f]{32}\Z")
_REVISION = re.compile(r"revision_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ARTIFACT_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_REVISION_FILE = re.compile(r"(?:manifest\.json|model\.FCStd|model\.step)\Z")
_STAGE_NAME = re.compile(r"\.(?:import|normalized|stage|work)\.[0-9a-f]{32}\.FCStd\Z")
_MAX_SESSIONS = 6
_MAX_CANDIDATES = 8
_MAX_REVISIONS = 8
_MAX_DIRECTORY_ENTRIES = 64
_MAX_FILE_BYTES = 536_870_912
_READ_CHUNK_BYTES = 1_048_576
_MAX_ARTIFACT_MANIFEST_BYTES = 1_048_576
_MAX_ARTIFACT_DIRECTORY_ENTRIES = MAX_REVIEWED_ARTIFACTS + 1
_ARTIFACT_SNAPSHOT_KIND = "reviewed_artifact_snapshot_v1"


class _WorkerExecutor(InProcessCadExecutor):
    """Store-free subset of the existing trusted CAD implementation."""

    __slots__ = ()

    def __init__(self) -> None:
        pass


class _ServiceError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: WorkerWireErrorCode) -> None:
        self.code = code
        self.args = (code.value,)


@dataclass(frozen=True, slots=True)
class _Identity:
    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    dev: int
    ino: int
    mode: int
    uid: int
    gid: int


@dataclass(slots=True)
class _Candidate:
    candidate_id: str
    project_id: str
    revision_id: str
    base_revision_id: str
    directory_fd: int
    directory_identity: _DirectoryIdentity | None
    model_identity: _Identity | _windows_files.WindowsEntryIdentity
    step_identity: _Identity | _windows_files.WindowsEntryIdentity
    directory_capability: WindowsPathCapability | None = None


@dataclass(frozen=True, slots=True)
class _ExpectedFile:
    name: str
    sha256: str
    size_bytes: int


@dataclass(slots=True)
class _Revision:
    revision_id: str
    project_id: str
    store_revision_id: str
    model_name: str | None
    directory_fd: int
    directory_identity: _DirectoryIdentity | None
    entries: tuple[tuple[str, _Identity | _windows_files.WindowsEntryIdentity], ...]
    files: tuple[_ExpectedFile, ...]
    directory_capability: WindowsPathCapability | None = None


@dataclass(slots=True)
class _Session:
    session_id: str
    capability_kind: str
    capability_id: str
    value: object
    freeform_digest: str | None = None
    freeform_design_id: str | None = None
    freeform_design_json: str | None = None
    freeform_schema_version: int | None = None
    cad_bridge: contextlib.AbstractContextManager[WindowsPathCapability] | None = None


@dataclass(slots=True)
class _Program:
    program_id: str
    session_id: str
    candidate_id: str
    command_ids: tuple[str, ...]
    deadlines_ms: tuple[int, ...]
    execution: _ValidatedProgramExecution
    artifact_resolver: _ReviewedArtifactRunResolver | None = None
    artifact_run_token: object | None = None
    next_index: int = 0

    def close(self) -> None:
        self.execution.close()
        self.artifact_resolver = None
        self.artifact_run_token = None


@dataclass(frozen=True, slots=True)
class _ValidationDirectory:
    descriptor: int
    identity: _DirectoryIdentity | None
    capability: WindowsPathCapability | None
    entries: tuple[
        tuple[str, _Identity | _windows_files.WindowsEntryIdentity],
        ...,
    ]


def _identity(value: os.stat_result) -> _Identity:
    return _Identity(
        dev=value.st_dev,
        ino=value.st_ino,
        mode=value.st_mode,
        uid=value.st_uid,
        nlink=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(
        dev=value.st_dev,
        ino=value.st_ino,
        mode=value.st_mode,
        uid=value.st_uid,
        gid=value.st_gid,
    )


def _private_directory(value: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.geteuid()
        and stat.S_IMODE(value.st_mode) == 0o700
    )


def _private_file(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == 0o600
        and 0 <= value.st_size <= _MAX_FILE_BYTES
    )


def _capture_candidate_entries(directory_fd: int) -> tuple[_Identity, _Identity]:
    try:
        directory = os.fstat(directory_fd)
        model = os.stat("model.FCStd", dir_fd=directory_fd, follow_symlinks=False)
        step = os.stat("model.step", dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
    if (
        not _private_file(model)
        or not _private_file(step)
        or model.st_dev != directory.st_dev
        or step.st_dev != directory.st_dev
    ):
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    return _identity(model), _identity(step)


def _stable_file_identity(
    value: _Identity | _windows_files.WindowsEntryIdentity,
) -> tuple[object, ...]:
    if type(value) is _windows_files.WindowsEntryIdentity:
        return _windows_files.stable_identity(value)  # type: ignore[return-value]
    return (value.dev, value.ino, value.mode, value.uid, value.nlink)


def _candidate_entries(
    candidate: _Candidate,
) -> tuple[
    _Identity | _windows_files.WindowsEntryIdentity,
    _Identity | _windows_files.WindowsEntryIdentity,
]:
    capability = candidate.directory_capability
    if capability is None:
        return _capture_candidate_entries(candidate.directory_fd)
    try:
        entries = dict(
            _windows_files.capture_entries(
                capability,
                maximum_entries=_MAX_DIRECTORY_ENTRIES,
            )
        )
        if not {"model.FCStd", "model.step"}.issubset(entries) or not set(entries).issubset(
            {
                "model.FCStd",
                "model.step",
                "seed-intent.json",
                "seed-binding.json",
            }
        ):
            raise OSError
        return entries["model.FCStd"], entries["model.step"]
    except (OSError, TypeError, ValueError):
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


@contextlib.contextmanager
def _directory_cwd(
    directory_fd: int,
    expected_identity: _DirectoryIdentity,
):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    previous = -1
    try:
        current = os.fstat(directory_fd)
        if _directory_identity(current) != expected_identity or not _private_directory(current):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        previous = os.open(".", flags)
        os.fchdir(directory_fd)
        if _directory_identity(os.stat(".", follow_symlinks=False)) != expected_identity:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        yield
    finally:
        if previous >= 0:
            try:
                os.fchdir(previous)
            finally:
                os.close(previous)


def _candidate_cwd(candidate: _Candidate):
    if candidate.directory_capability is not None:
        return _windows_files.directory_cwd(candidate.directory_capability)
    return _directory_cwd(candidate.directory_fd, candidate.directory_identity)


def _revision_cwd(revision: _Revision):
    if revision.directory_capability is not None:
        return _windows_files.directory_cwd(revision.directory_capability)
    return _directory_cwd(revision.directory_fd, revision.directory_identity)


def _engine_entry_path(
    capability: WindowsPathCapability | None,
    name: str,
) -> Path:
    if capability is None:
        return Path(name)
    return _windows_files.entry_path(capability, name)


def _load_windows_fcstd(
    engine: InProcessCadExecutor,
    *,
    source_directory: WindowsPathCapability,
    source: _windows_files.WindowsEntryIdentity,
    name: str,
) -> tuple[
    object,
    contextlib.AbstractContextManager[WindowsPathCapability],
]:
    """Load an exact long-path FCStd through a private short-lived CAD bridge."""

    bridge = _windows_files.cad_staging_directory()
    entered = False
    try:
        staging = bridge.__enter__()
        entered = True
        staged = _windows_files.stage_cad_input(
            source_directory,
            source,
            staging,
            name,
        )
        value = engine.load_fcstd(_windows_files.validate_entry(staged))
    except BaseException:
        if entered:
            try:
                bridge.__exit__(None, None, None)
            except BaseException:
                raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        raise
    return value, bridge


def _close_cad_bridge(
    bridge: contextlib.AbstractContextManager[WindowsPathCapability] | None,
) -> None:
    if bridge is not None:
        bridge.__exit__(None, None, None)


def _discard_loaded_session(
    engine: InProcessCadExecutor,
    value: object,
    bridge: contextlib.AbstractContextManager[WindowsPathCapability] | None,
) -> None:
    failed = False
    try:
        engine.close(value)
    except BaseException:
        failed = True
    try:
        _close_cad_bridge(bridge)
    except BaseException:
        failed = True
    if failed:
        raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)


def _capture_private_entries(directory_fd: int) -> tuple[tuple[str, _Identity], ...]:
    try:
        directory = os.fstat(directory_fd)
        if not _private_directory(directory):
            raise OSError
        names = tuple(sorted(os.listdir(directory_fd)))
        if (
            len(names) > _MAX_DIRECTORY_ENTRIES
            or len(names) != len(set(names))
            or any(type(name) is not str or name in {".", ".."} for name in names)
        ):
            raise OSError
        values: list[tuple[str, _Identity]] = []
        for name in names:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _private_file(current) or current.st_dev != directory.st_dev:
                raise OSError
            values.append((name, _identity(current)))
        return tuple(values)
    except OSError:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


def _capture_revision_entries(
    revision: _Revision,
) -> tuple[tuple[str, _Identity | _windows_files.WindowsEntryIdentity], ...]:
    if revision.directory_capability is None:
        entries = _capture_private_entries(revision.directory_fd)
    else:
        try:
            entries = _windows_files.capture_entries(
                revision.directory_capability,
                maximum_entries=_MAX_DIRECTORY_ENTRIES,
            )
        except (OSError, TypeError, ValueError):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
    if tuple(name for name, _entry in entries) != tuple(item.name for item in revision.files):
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    with _revision_cwd(revision):
        for expected, (name, identity) in zip(revision.files, entries, strict=True):
            digest, size, hashed = _hash_capability_entry(
                revision.directory_capability,
                name,
                expected=identity,
            )
            if hashed != identity or digest != expected.sha256 or size != expected.size_bytes:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    return entries


def _hash_capability_entry(
    capability: WindowsPathCapability | None,
    name: str,
    *,
    expected: _Identity | _windows_files.WindowsEntryIdentity | None = None,
) -> tuple[
    str,
    int,
    _Identity | _windows_files.WindowsEntryIdentity,
]:
    if capability is None:
        return _hash_relative(name)
    if expected is not None and type(expected) is not _windows_files.WindowsEntryIdentity:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    try:
        return _windows_files.hash_entry(
            capability,
            name,
            maximum_bytes=_MAX_FILE_BYTES,
            expected=expected,
        )
    except (OSError, TypeError, ValueError):
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


def _hash_relative(name: str) -> tuple[str, int, _Identity]:
    digest = hashlib.sha256()
    size = 0
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = -1
    result = None
    error = None
    try:
        fd = os.open(name, flags)
        before = os.fstat(fd)
        if not _private_file(before) or before.st_size <= 0:
            raise OSError
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_FILE_BYTES:
                raise OSError
            digest.update(chunk)
        after = os.fstat(fd)
        live = os.stat(name, follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(live)
            or size != before.st_size
        ):
            raise OSError
        result = (digest.hexdigest(), size, _identity(after))
    except BaseException as caught:
        error = caught
    close_failed = False
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            close_failed = True
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        raise _ServiceError(WorkerWireErrorCode.ARTIFACT_FAILURE) from None
    if close_failed or result is None:
        raise _ServiceError(WorkerWireErrorCode.ARTIFACT_FAILURE)
    return result


def _artifact_failure(code: ReviewedArtifactInputErrorCode) -> ReviewedArtifactInputError:
    return ReviewedArtifactInputError(code)


def _read_artifact_entry(
    directory_fd: int,
    name: str,
    *,
    maximum_bytes: int,
    expected: _Identity,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if (
            _identity(before) != expected
            or not _private_file(before)
            or not 1 <= before.st_size <= maximum_bytes
        ):
            raise OSError
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, maximum_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum_bytes:
                raise OSError
        after = os.fstat(descriptor)
        live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if size != before.st_size or _identity(after) != expected or _identity(live) != expected:
            raise OSError
        return b"".join(chunks)
    except OSError:
        raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise _artifact_failure(ReviewedArtifactInputErrorCode.CLEANUP_FAILED) from None


def _artifact_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_artifact_json_constant(_value: str) -> None:
    raise ValueError


def _artifact_record(value: object) -> ReviewedArtifactCatalogRecord:
    fields = _exact_mapping(
        value,
        {
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
        },
    )
    operations = fields["operation_ids"]
    if type(operations) is not list:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    try:
        return ReviewedArtifactCatalogRecord(
            artifact_id=fields["artifact_id"],  # type: ignore[arg-type]
            content_sha256=fields["content_sha256"],  # type: ignore[arg-type]
            document_id=fields["document_id"],  # type: ignore[arg-type]
            family_id=fields["family_id"],  # type: ignore[arg-type]
            maximum_bytes=fields["maximum_bytes"],  # type: ignore[arg-type]
            media_type=fields["media_type"],  # type: ignore[arg-type]
            operation_ids=tuple(operations),
            role_term_ref_id=fields["role_term_ref_id"],  # type: ignore[arg-type]
            schema_term_ref_id=fields["schema_term_ref_id"],  # type: ignore[arg-type]
            size_bytes=fields["size_bytes"],  # type: ignore[arg-type]
        )
    except ReviewedArtifactInputError:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


def _decode_artifact_manifest(raw: bytes) -> ReviewedArtifactCatalogSnapshot:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_ARTIFACT_MANIFEST_BYTES:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_artifact_json_pairs,
            parse_constant=_reject_artifact_json_constant,
        )
        fields = _exact_mapping(
            value,
            {
                "base_revision",
                "catalog_sha256",
                "project_id",
                "records",
                "run_id",
                "schema_version",
                "task_id",
            },
        )
        records = fields["records"]
        if (
            type(fields["schema_version"]) is not int
            or fields["schema_version"] != 1
            or type(records) is not list
            or not records
        ):
            raise ValueError
        snapshot = ReviewedArtifactCatalogSnapshot(
            task_id=fields["task_id"],  # type: ignore[arg-type]
            project_id=fields["project_id"],  # type: ignore[arg-type]
            base_revision=fields["base_revision"],  # type: ignore[arg-type]
            run_id=fields["run_id"],  # type: ignore[arg-type]
            records=tuple(_artifact_record(item) for item in records),
        )
        canonical = json.dumps(
            snapshot.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if fields["catalog_sha256"] != snapshot.catalog_sha256 or canonical != raw:
            raise ValueError
        return snapshot
    except (
        ReviewedArtifactInputError,
        _ServiceError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


def _artifact_snapshot_envelope(value: object) -> dict[str, object]:
    fields = _exact_mapping(
        value,
        {
            "base_revision",
            "catalog_sha256",
            "kind",
            "project_id",
            "run_id",
            "schema_version",
            "task_id",
        },
    )
    if (
        fields["kind"] != _ARTIFACT_SNAPSHOT_KIND
        or type(fields["schema_version"]) is not int
        or fields["schema_version"] != 1
        or type(fields["catalog_sha256"]) is not str
        or _DIGEST.fullmatch(fields["catalog_sha256"]) is None
        or any(
            type(fields[name]) is not str
            or len(fields[name]) > 128
            or _ARTIFACT_IDENTIFIER.fullmatch(fields[name]) is None
            for name in ("task_id", "project_id", "base_revision", "run_id")
        )
    ):
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    return fields


class _WorkerArtifactPayloadSource:
    __slots__ = (
        "_closed",
        "_directory_capability",
        "_directory_fd",
        "_directory_identity",
        "_entries",
        "_records",
    )

    def __init__(
        self,
        *,
        directory_fd: int,
        directory_identity: _DirectoryIdentity | None,
        entries: tuple[
            tuple[str, _Identity | _windows_files.WindowsEntryIdentity],
            ...,
        ],
        snapshot: ReviewedArtifactCatalogSnapshot,
        directory_capability: WindowsPathCapability | None = None,
    ) -> None:
        self._directory_fd = directory_fd
        self._directory_identity = directory_identity
        self._directory_capability = directory_capability
        self._entries = dict(entries)
        self._records = {item.artifact_id: item for item in snapshot.records}
        self._closed = False

    def _require_live(self) -> None:
        if self._closed:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.CLOSED)
        if self._directory_capability is not None:
            try:
                entries = _windows_files.capture_entries(
                    self._directory_capability,
                    maximum_entries=_MAX_ARTIFACT_DIRECTORY_ENTRIES,
                )
            except (OSError, TypeError, ValueError):
                raise _artifact_failure(
                    ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE
                ) from None
            if dict(entries) != self._entries:
                raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
            return
        try:
            current = os.fstat(self._directory_fd)
        except OSError:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.CLOSED) from None
        if _directory_identity(current) != self._directory_identity or not _private_directory(
            current
        ):
            raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        try:
            names = tuple(sorted(os.listdir(self._directory_fd)))
            if names != tuple(sorted(self._entries)):
                raise OSError
            for name, expected in self._entries.items():
                live = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
                if _identity(live) != expected:
                    raise OSError
        except OSError:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE) from None

    def read(self, record: ReviewedArtifactCatalogRecord, maximum_bytes: int) -> bytes:
        self._require_live()
        if (
            type(record) is not ReviewedArtifactCatalogRecord
            or self._records.get(record.artifact_id) != record
            or maximum_bytes != record.maximum_bytes
        ):
            raise _artifact_failure(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        expected = self._entries.get(record.artifact_id)
        if expected is None:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        if self._directory_capability is None:
            if type(expected) is not _Identity:
                raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
            payload = _read_artifact_entry(
                self._directory_fd,
                record.artifact_id,
                maximum_bytes=maximum_bytes,
                expected=expected,
            )
        else:
            if type(expected) is not _windows_files.WindowsEntryIdentity:
                raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
            try:
                payload = _windows_files.read_entry(
                    self._directory_capability,
                    record.artifact_id,
                    maximum_bytes=maximum_bytes,
                    expected=expected,
                )
            except (OSError, TypeError, ValueError):
                raise _artifact_failure(
                    ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE
                ) from None
        if (
            len(payload) != record.size_bytes
            or hashlib.sha256(payload).hexdigest() != record.content_sha256
        ):
            raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)
        return payload

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._directory_capability is not None:
            self._directory_capability = None
            return
        try:
            os.close(self._directory_fd)
        except OSError:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.CLEANUP_FAILED) from None


class _WorkerArtifactStagerFactory:
    __slots__ = ("_capability", "_closed", "_device", "_inode", "_root")

    def __init__(self) -> None:
        self._capability = None
        if sys.platform == "win32":
            root = Path(tempfile.gettempdir()).resolve() / (
                f"vibecad-reviewed-artifact-{secrets.token_hex(16)}"
            )
            try:
                from vibecad import _file_compat  # noqa: PLC0415

                self._capability = _file_compat.ensure_private_directory(root)
                info = root.lstat()
            except (OSError, TypeError, ValueError):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        else:
            root = Path(tempfile.mkdtemp(prefix="vibecad-reviewed-artifact-"))
            root.chmod(0o700)
            info = root.lstat()
            if not _private_directory(info):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        self._root = root
        self._device = info.st_dev
        self._inode = info.st_ino
        self._closed = False

    def _require_live(self) -> None:
        if self._closed:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.CLOSED)
        if self._capability is not None:
            try:
                _windows_files.validate_directory(self._capability)
            except (OSError, TypeError, ValueError):
                raise _artifact_failure(
                    ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE
                ) from None
            return
        try:
            info = self._root.lstat()
        except OSError:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE) from None
        if (
            not _private_directory(info)
            or info.st_dev != self._device
            or info.st_ino != self._inode
        ):
            raise _artifact_failure(ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE)

    def create(
        self,
        *,
        record: ReviewedArtifactCatalogRecord,
        family_id: str,
        operation_id: str,
    ) -> object:
        self._require_live()
        if (
            type(record) is not ReviewedArtifactCatalogRecord
            or family_id != record.family_id
            or operation_id not in record.operation_ids
        ):
            raise _artifact_failure(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)
        if family_id == "freecad_part_file_import":
            return HostOwnedImportStager(self._root)
        if family_id == "freecad_imageplane":
            return HostOwnedImageStager(self._root)
        raise _artifact_failure(ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._capability is not None:
            try:
                root = _windows_files.validate_directory(self._capability)
                if tuple(root.iterdir()):
                    raise OSError
                os.rmdir(root)
                self._capability = None
                return
            except OSError:
                raise _artifact_failure(
                    ReviewedArtifactInputErrorCode.CLEANUP_FAILED
                ) from None
        try:
            info = self._root.lstat()
            if (
                not _private_directory(info)
                or info.st_dev != self._device
                or info.st_ino != self._inode
                or tuple(self._root.iterdir())
            ):
                raise OSError
            self._root.rmdir()
        except OSError:
            raise _artifact_failure(ReviewedArtifactInputErrorCode.CLEANUP_FAILED) from None


def _open_artifact_run_resolver_posix(
    descriptor: int,
    envelope: dict[str, object],
) -> tuple[_ReviewedArtifactRunResolver, object]:
    pinned = -1
    source: _WorkerArtifactPayloadSource | None = None
    stagers: _WorkerArtifactStagerFactory | None = None
    try:
        received = os.fstat(descriptor)
        if not _private_directory(received):
            raise OSError
        pinned = os.dup(descriptor)
        os.set_inheritable(pinned, False)
        captured = os.fstat(pinned)
        if _directory_identity(received) != _directory_identity(captured):
            raise OSError
        names = tuple(sorted(os.listdir(pinned)))
        if (
            not names
            or len(names) > _MAX_ARTIFACT_DIRECTORY_ENTRIES
            or len(names) != len(set(names))
        ):
            raise OSError
        entries: list[tuple[str, _Identity]] = []
        for name in names:
            current = os.stat(name, dir_fd=pinned, follow_symlinks=False)
            if not _private_file(current) or current.st_dev != captured.st_dev:
                raise OSError
            entries.append((name, _identity(current)))
        entry_map = dict(entries)
        manifest_identity = entry_map.get("manifest.json")
        if manifest_identity is None:
            raise OSError
        manifest = _read_artifact_entry(
            pinned,
            "manifest.json",
            maximum_bytes=_MAX_ARTIFACT_MANIFEST_BYTES,
            expected=manifest_identity,
        )
        snapshot = _decode_artifact_manifest(manifest)
        expected_names = tuple(
            sorted(("manifest.json", *(item.artifact_id for item in snapshot.records)))
        )
        if names != expected_names:
            raise OSError
        if (
            envelope["task_id"] != snapshot.task_id
            or envelope["project_id"] != snapshot.project_id
            or envelope["base_revision"] != snapshot.base_revision
            or envelope["run_id"] != snapshot.run_id
            or envelope["catalog_sha256"] != snapshot.catalog_sha256
        ):
            raise OSError
        for record in snapshot.records:
            expected = entry_map[record.artifact_id]
            payload = _read_artifact_entry(
                pinned,
                record.artifact_id,
                maximum_bytes=min(record.maximum_bytes, MAX_REVIEWED_ARTIFACT_BYTES),
                expected=expected,
            )
            if (
                len(payload) != record.size_bytes
                or hashlib.sha256(payload).hexdigest() != record.content_sha256
            ):
                raise OSError
        if tuple(sorted(os.listdir(pinned))) != names or tuple(
            (name, _identity(os.stat(name, dir_fd=pinned, follow_symlinks=False))) for name in names
        ) != tuple(entries):
            raise OSError
        source = _WorkerArtifactPayloadSource(
            directory_fd=pinned,
            directory_identity=_directory_identity(captured),
            entries=tuple(entries),
            snapshot=snapshot,
        )
        pinned = -1
        stagers = _WorkerArtifactStagerFactory()
        run_token = object()
        resolver = _ReviewedArtifactRunResolver(
            snapshot=snapshot,
            source=source,
            stager_factory=stagers,
            task_id=snapshot.task_id,
            project_id=snapshot.project_id,
            base_revision=snapshot.base_revision,
            run_id=snapshot.run_id,
            run_token=run_token,
        )
        return resolver, run_token
    except (OSError, ReviewedArtifactInputError, _ServiceError):
        if stagers is not None:
            with contextlib.suppress(BaseException):
                stagers.close()
        if source is not None:
            with contextlib.suppress(BaseException):
                source.close()
        if pinned >= 0:
            with contextlib.suppress(OSError):
                os.close(pinned)
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


def _open_artifact_run_resolver_windows(
    capability: WindowsPathCapability,
    envelope: dict[str, object],
) -> tuple[_ReviewedArtifactRunResolver, object]:
    source: _WorkerArtifactPayloadSource | None = None
    stagers: _WorkerArtifactStagerFactory | None = None
    try:
        entries = _windows_files.capture_entries(
            capability,
            maximum_entries=_MAX_ARTIFACT_DIRECTORY_ENTRIES,
        )
        names = tuple(name for name, _identity_value in entries)
        if not names:
            raise OSError
        entry_map = dict(entries)
        manifest_identity = entry_map.get("manifest.json")
        if type(manifest_identity) is not _windows_files.WindowsEntryIdentity:
            raise OSError
        manifest = _windows_files.read_entry(
            capability,
            "manifest.json",
            maximum_bytes=_MAX_ARTIFACT_MANIFEST_BYTES,
            expected=manifest_identity,
        )
        snapshot = _decode_artifact_manifest(manifest)
        expected_names = tuple(
            sorted(("manifest.json", *(item.artifact_id for item in snapshot.records)))
        )
        if names != expected_names:
            raise OSError
        if (
            envelope["task_id"] != snapshot.task_id
            or envelope["project_id"] != snapshot.project_id
            or envelope["base_revision"] != snapshot.base_revision
            or envelope["run_id"] != snapshot.run_id
            or envelope["catalog_sha256"] != snapshot.catalog_sha256
        ):
            raise OSError
        for record in snapshot.records:
            expected = entry_map[record.artifact_id]
            if type(expected) is not _windows_files.WindowsEntryIdentity:
                raise OSError
            payload = _windows_files.read_entry(
                capability,
                record.artifact_id,
                maximum_bytes=min(record.maximum_bytes, MAX_REVIEWED_ARTIFACT_BYTES),
                expected=expected,
            )
            if (
                len(payload) != record.size_bytes
                or hashlib.sha256(payload).hexdigest() != record.content_sha256
            ):
                raise OSError
        if _windows_files.capture_entries(
            capability,
            maximum_entries=_MAX_ARTIFACT_DIRECTORY_ENTRIES,
        ) != entries:
            raise OSError
        source = _WorkerArtifactPayloadSource(
            directory_fd=-1,
            directory_identity=None,
            entries=entries,
            snapshot=snapshot,
            directory_capability=capability,
        )
        stagers = _WorkerArtifactStagerFactory()
        run_token = object()
        resolver = _ReviewedArtifactRunResolver(
            snapshot=snapshot,
            source=source,
            stager_factory=stagers,
            task_id=snapshot.task_id,
            project_id=snapshot.project_id,
            base_revision=snapshot.base_revision,
            run_id=snapshot.run_id,
            run_token=run_token,
        )
        return resolver, run_token
    except (OSError, TypeError, ValueError, ReviewedArtifactInputError, _ServiceError):
        if stagers is not None:
            with contextlib.suppress(BaseException):
                stagers.close()
        if source is not None:
            with contextlib.suppress(BaseException):
                source.close()
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


def _open_artifact_run_resolver(
    descriptor: int,
    envelope: dict[str, object],
    *,
    directory_capability: WindowsPathCapability | None = None,
) -> tuple[_ReviewedArtifactRunResolver, object]:
    if directory_capability is not None:
        if descriptor >= 0 or sys.platform != "win32":
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        return _open_artifact_run_resolver_windows(directory_capability, envelope)
    return _open_artifact_run_resolver_posix(descriptor, envelope)


def _exact_mapping(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    return value


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    return value


def _expected_files(value: object) -> tuple[_ExpectedFile, ...]:
    if type(value) is not list or not 1 <= len(value) <= 3:
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    result: list[_ExpectedFile] = []
    for raw in value:
        fields = _exact_mapping(raw, {"name", "sha256", "size_bytes"})
        name = _identifier(fields["name"], _REVISION_FILE)
        digest = _identifier(fields["sha256"], _DIGEST)
        size = fields["size_bytes"]
        if type(size) is not int or not 1 <= size <= _MAX_FILE_BYTES:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        result.append(
            _ExpectedFile(
                name=name,
                sha256=digest,
                size_bytes=size,
            )
        )
    names = tuple(item.name for item in result)
    if (
        names != tuple(sorted(names))
        or len(names) != len(set(names))
        or "manifest.json" not in names
    ):
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    return tuple(result)


def _evidence_mapping(value: object) -> dict[str, object]:
    if type(value) is not ValidatedImportEvidence:
        raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
    return {
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _materialization_mapping(value: object) -> dict[str, object]:
    if type(value) is not ValidatedMaterializationEvidence:
        raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
    return {
        "fcstd_sha256": value.fcstd_sha256,
        "fcstd_size_bytes": value.fcstd_size_bytes,
        "step_sha256": value.step_sha256,
        "step_size_bytes": value.step_size_bytes,
    }


def _outcome_mapping(outcome: object) -> dict[str, object]:
    from vibecad.execution.results import NormalizedToolOutcome

    if type(outcome) is not NormalizedToolOutcome:
        raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
    diagnostic = outcome.diagnostic
    return {
        "result": outcome.result.to_mapping(),
        "diagnostic": None if diagnostic is None else diagnostic.value,
    }


def _executor_code(error: ExecutorError) -> WorkerWireErrorCode:
    return {
        ExecutorErrorCode.INVALID_INPUT: WorkerWireErrorCode.INVALID_INPUT,
        ExecutorErrorCode.INVALID_CANDIDATE: WorkerWireErrorCode.INVALID_CANDIDATE,
        ExecutorErrorCode.INVALID_LEASE: WorkerWireErrorCode.INVALID_INPUT,
        ExecutorErrorCode.CAD_FAILURE: WorkerWireErrorCode.CAD_FAILURE,
        ExecutorErrorCode.ARTIFACT_FAILURE: WorkerWireErrorCode.ARTIFACT_FAILURE,
        ExecutorErrorCode.INTEGRITY_FAILURE: WorkerWireErrorCode.INTEGRITY_FAILURE,
        ExecutorErrorCode.INTERNAL_FAILURE: WorkerWireErrorCode.INTERNAL_ERROR,
    }[error.code]


class WorkerService:
    __slots__ = (
        "_candidates",
        "_engine",
        "_generation_id",
        "_programs",
        "_revisions",
        "_sessions",
        "_shutdown",
    )

    def __init__(self, generation_id: str) -> None:
        if (
            type(generation_id) is not str
            or re.fullmatch(r"worker_generation_[0-9a-f]{32}", generation_id) is None
        ):
            raise ValueError("invalid generation")
        self._generation_id = generation_id
        self._engine = _WorkerExecutor()
        self._candidates: dict[str, _Candidate] = {}
        self._revisions: dict[str, _Revision] = {}
        self._sessions: dict[str, _Session] = {}
        self._programs: dict[str, _Program] = {}
        self._shutdown = False

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown

    def _candidate(self, candidate_id: object) -> _Candidate:
        identifier = _identifier(candidate_id, _CANDIDATE)
        candidate = self._candidates.get(identifier)
        if candidate is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return candidate

    def _session(self, session_id: object) -> _Session:
        identifier = _identifier(session_id, _SESSION)
        session = self._sessions.get(identifier)
        if session is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return session

    def _revision(self, revision_id: object) -> _Revision:
        identifier = _identifier(revision_id, _WORKER_REVISION)
        revision = self._revisions.get(identifier)
        if revision is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return revision

    def _program(self, program_id: object) -> _Program:
        identifier = _identifier(program_id, _PROGRAM)
        program = self._programs.get(identifier)
        if program is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return program

    def _close_program(self, program: _Program) -> None:
        current = self._programs.pop(program.program_id, None)
        if current is not program:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
        try:
            program.close()
        except BaseException:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None

    def _require_pair(
        self,
        *,
        session_id: object,
        candidate_id: object,
    ) -> tuple[_Session, _Candidate]:
        session = self._session(session_id)
        candidate = self._candidate(candidate_id)
        if (
            session.capability_kind != "candidate"
            or session.capability_id != candidate.candidate_id
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return session, candidate

    def _require_observation_pair(
        self,
        *,
        session_id: object,
        capability_kind: object,
        capability_id: object,
    ) -> tuple[_Session, _Candidate | _Revision]:
        session = self._session(session_id)
        if capability_kind == "candidate":
            capability = self._candidate(capability_id)
            identifier = capability.candidate_id
        elif capability_kind == "revision":
            capability = self._revision(capability_id)
            identifier = capability.revision_id
        else:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        if session.capability_kind != capability_kind or session.capability_id != identifier:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return session, capability

    def _bind(self, params: object, descriptors: tuple[int, ...]) -> dict[str, object]:
        field_names = {
            "candidate_id",
            "project_id",
            "revision_id",
            "base_revision_id",
        }
        if sys.platform == "win32":
            field_names.add("path_capability")
        fields = _exact_mapping(
            params,
            field_names,
        )
        expected_descriptors = 0 if sys.platform == "win32" else 1
        if len(descriptors) != expected_descriptors or len(self._candidates) >= _MAX_CANDIDATES:
            raise _ServiceError(
                WorkerWireErrorCode.INVALID_REQUEST
                if len(descriptors) != expected_descriptors
                else WorkerWireErrorCode.RESOURCE_EXHAUSTED
            )
        candidate_id = _identifier(fields["candidate_id"], _CANDIDATE)
        project_id = _identifier(fields["project_id"], _PROJECT)
        revision_id = _identifier(fields["revision_id"], _REVISION)
        base_revision_id = _identifier(fields["base_revision_id"], _REVISION)
        if candidate_id in self._candidates:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        if base_revision_id == revision_id:
            raise _ServiceError(WorkerWireErrorCode.INVALID_CANDIDATE)
        descriptor = -1
        directory_identity: _DirectoryIdentity | None = None
        directory_capability: WindowsPathCapability | None = None
        if sys.platform == "win32":
            try:
                directory_capability = _windows_files.capability_from_mapping(
                    fields["path_capability"]
                )
                candidate_probe = _Candidate(
                    candidate_id=candidate_id,
                    project_id=project_id,
                    revision_id=revision_id,
                    base_revision_id=base_revision_id,
                    directory_fd=-1,
                    directory_identity=None,
                    model_identity=None,  # type: ignore[arg-type]
                    step_identity=None,  # type: ignore[arg-type]
                    directory_capability=directory_capability,
                )
                model, step = _candidate_entries(candidate_probe)
            except (OSError, TypeError, ValueError, _ServiceError):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        else:
            descriptor = descriptors[0]
            try:
                os.set_inheritable(descriptor, False)
                directory = os.fstat(descriptor)
                if not _private_directory(directory):
                    raise OSError
                entries = set(os.listdir(descriptor))
                if not {"model.FCStd", "model.step"}.issubset(entries) or not entries.issubset(
                    {
                        "model.FCStd",
                        "model.step",
                        "seed-intent.json",
                        "seed-binding.json",
                    }
                ):
                    raise OSError
                model, step = _capture_candidate_entries(descriptor)
                directory_identity = _directory_identity(directory)
            except (OSError, _ServiceError):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        candidate = _Candidate(
            candidate_id=candidate_id,
            project_id=project_id,
            revision_id=revision_id,
            base_revision_id=base_revision_id,
            directory_fd=descriptor,
            directory_identity=directory_identity,
            model_identity=model,
            step_identity=step,
            directory_capability=directory_capability,
        )
        self._candidates[candidate_id] = candidate
        return {"candidate_id": candidate_id}

    def _release_candidate(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"candidate_id"})
        candidate = self._candidate(fields["candidate_id"])
        if any(
            session.capability_kind == "candidate"
            and session.capability_id == candidate.candidate_id
            for session in self._sessions.values()
        ) or any(
            program.candidate_id == candidate.candidate_id for program in self._programs.values()
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        if candidate.directory_fd >= 0:
            try:
                os.close(candidate.directory_fd)
            except OSError:
                raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        self._candidates.pop(candidate.candidate_id, None)
        return {"candidate_id": candidate.candidate_id}

    def _bind_revision(
        self,
        params: object,
        descriptors: tuple[int, ...],
    ) -> dict[str, object]:
        field_names = {
            "revision_id",
            "project_id",
            "store_revision_id",
            "model_name",
            "files",
        }
        if sys.platform == "win32":
            field_names.add("path_capability")
        fields = _exact_mapping(
            params,
            field_names,
        )
        expected_descriptors = 0 if sys.platform == "win32" else 1
        if len(descriptors) != expected_descriptors or len(self._revisions) >= _MAX_REVISIONS:
            raise _ServiceError(
                WorkerWireErrorCode.INVALID_REQUEST
                if len(descriptors) != expected_descriptors
                else WorkerWireErrorCode.RESOURCE_EXHAUSTED
            )
        revision_id = _identifier(fields["revision_id"], _WORKER_REVISION)
        project_id = _identifier(fields["project_id"], _PROJECT)
        store_revision_id = _identifier(fields["store_revision_id"], _REVISION)
        model_name = fields["model_name"]
        if model_name is not None and model_name != "model.FCStd":
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        files = _expected_files(fields["files"])
        names = tuple(item.name for item in files)
        if (
            revision_id in self._revisions
            or (model_name is None and "model.FCStd" in names)
            or (model_name is not None and "model.FCStd" not in names)
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        descriptor = -1
        directory_identity: _DirectoryIdentity | None = None
        directory_capability: WindowsPathCapability | None = None
        try:
            if sys.platform == "win32":
                directory_capability = _windows_files.capability_from_mapping(
                    fields["path_capability"]
                )
                _windows_files.validate_directory(directory_capability)
            else:
                descriptor = descriptors[0]
                os.set_inheritable(descriptor, False)
                directory = os.fstat(descriptor)
                if not _private_directory(directory):
                    raise OSError
                directory_identity = _directory_identity(directory)
            revision = _Revision(
                revision_id=revision_id,
                project_id=project_id,
                store_revision_id=store_revision_id,
                model_name=model_name,
                directory_fd=descriptor,
                directory_identity=directory_identity,
                entries=(),
                files=files,
                directory_capability=directory_capability,
            )
            entries = _capture_revision_entries(revision)
            revision.entries = entries
        except (OSError, _ServiceError):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        self._revisions[revision_id] = revision
        return {"revision_id": revision_id}

    def _release_revision(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"revision_id"})
        revision = self._revision(fields["revision_id"])
        if any(
            session.capability_kind == "revision" and session.capability_id == revision.revision_id
            for session in self._sessions.values()
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        if revision.directory_fd >= 0:
            try:
                os.close(revision.directory_fd)
            except OSError:
                raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        self._revisions.pop(revision.revision_id, None)
        return {"revision_id": revision.revision_id}

    def _load_revision(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"revision_id"})
        revision = self._revision(fields["revision_id"])
        if len(self._sessions) >= _MAX_SESSIONS:
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        bridge: contextlib.AbstractContextManager[WindowsPathCapability] | None = None
        with _revision_cwd(revision):
            current = _capture_revision_entries(revision)
            if current != revision.entries:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            if revision.model_name is None:
                value = self._engine.create_empty(
                    revision_id=revision.store_revision_id,
                )
            else:
                if revision.directory_capability is None:
                    value = self._engine.load_fcstd(Path(revision.model_name))
                else:
                    source = dict(current)[revision.model_name]
                    if type(source) is not _windows_files.WindowsEntryIdentity:
                        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                    value, bridge = _load_windows_fcstd(
                        self._engine,
                        source_directory=revision.directory_capability,
                        source=source,
                        name=revision.model_name,
                    )
            try:
                revalidated = _capture_revision_entries(revision)
            except BaseException as error:
                _discard_loaded_session(self._engine, value, bridge)
                if not isinstance(error, Exception):
                    raise
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
            if revalidated != current:
                _discard_loaded_session(self._engine, value, bridge)
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        session_id = f"worker_session_{secrets.token_hex(16)}"
        self._sessions[session_id] = _Session(
            session_id=session_id,
            capability_kind="revision",
            capability_id=revision.revision_id,
            value=value,
            cad_bridge=bridge,
        )
        return {"session_id": session_id}

    def _create_empty(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"candidate_id"})
        candidate = self._candidate(fields["candidate_id"])
        if len(self._sessions) >= _MAX_SESSIONS:
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        value = self._engine.create_empty(revision_id=candidate.revision_id)
        session_id = f"worker_session_{secrets.token_hex(16)}"
        self._sessions[session_id] = _Session(
            session_id=session_id,
            capability_kind="candidate",
            capability_id=candidate.candidate_id,
            value=value,
        )
        return {"session_id": session_id}

    def _load(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"candidate_id"})
        candidate = self._candidate(fields["candidate_id"])
        if len(self._sessions) >= _MAX_SESSIONS:
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        bridge: contextlib.AbstractContextManager[WindowsPathCapability] | None = None
        with _candidate_cwd(candidate):
            current_model, current_step = _candidate_entries(candidate)
            if (
                current_model != candidate.model_identity
                or current_step != candidate.step_identity
                or current_model.size <= 0
            ):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            if candidate.directory_capability is None:
                value = self._engine.load_fcstd(Path("model.FCStd"))
            else:
                if type(current_model) is not _windows_files.WindowsEntryIdentity:
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                value, bridge = _load_windows_fcstd(
                    self._engine,
                    source_directory=candidate.directory_capability,
                    source=current_model,
                    name="model.FCStd",
                )
                try:
                    revalidated = _candidate_entries(candidate)
                except BaseException as error:
                    _discard_loaded_session(self._engine, value, bridge)
                    if not isinstance(error, Exception):
                        raise
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
                if revalidated != (current_model, current_step):
                    _discard_loaded_session(self._engine, value, bridge)
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        session_id = f"worker_session_{secrets.token_hex(16)}"
        self._sessions[session_id] = _Session(
            session_id=session_id,
            capability_kind="candidate",
            capability_id=candidate.candidate_id,
            value=value,
            cad_bridge=bridge,
        )
        return {"session_id": session_id}

    def _checkpoint(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"session_id", "candidate_id"})
        session, candidate = self._require_pair(
            session_id=fields["session_id"],
            candidate_id=fields["candidate_id"],
        )
        with _candidate_cwd(candidate):
            current_model, current_step = _candidate_entries(candidate)
            if current_model != candidate.model_identity or current_step != candidate.step_identity:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            try:
                if candidate.directory_capability is not None:
                    with _windows_files.cad_staging_directory() as staging:
                        model_path = _windows_files.cad_output_path(
                            staging,
                            "model.FCStd",
                        )
                        self._engine.checkpoint_fcstd(session.value, model_path)
                        staged_model = _windows_files.capture_cad_output(
                            staging,
                            "model.FCStd",
                        )
                        if session.freeform_digest is not None:
                            self._verify_freeform_checkpoint(session, model_path)
                        model = _windows_files.publish_cad_output(
                            staging,
                            staged_model,
                            candidate.directory_capability,
                            "model.FCStd",
                            expected_destination=current_model,
                        )
                    captured_model, step = _candidate_entries(candidate)
                    if captured_model != model or step != current_step:
                        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                else:
                    model_path = Path("model.FCStd")
                    self._engine.checkpoint_fcstd(session.value, model_path)
                    model, step = _candidate_entries(candidate)
                    if step != current_step:
                        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                    if session.freeform_digest is not None:
                        self._verify_freeform_checkpoint(session, model_path)
                digest, size, hashed = _hash_capability_entry(
                    candidate.directory_capability,
                    "model.FCStd",
                    expected=model,
                )
                if hashed != model:
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            except BaseException:
                raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        candidate.model_identity = model
        candidate.step_identity = step
        return {"sha256": digest, "size_bytes": size}

    def _verify_freeform_checkpoint(self, session: _Session, model_path: Path) -> None:
        reloaded = self._engine.load_fcstd(model_path)
        try:
            matches = tuple(
                item
                for item in reloaded.doc.Objects
                if getattr(item, "VibeCADFreeformDesignDigest", None) == session.freeform_digest
            )
            if (
                len(matches) != 1
                or getattr(matches[0], "VibeCADFreeformSchemaVersion", None)
                != session.freeform_schema_version
                or getattr(matches[0], "VibeCADFreeformDesignId", None)
                != session.freeform_design_id
                or getattr(matches[0], "VibeCADFreeformDesignJson", None)
                != session.freeform_design_json
            ):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        finally:
            self._engine.close(reloaded)

    def _compile_freeform(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(
            params,
            {"session_id", "candidate_id", "design", "design_sha256"},
        )
        session, _candidate = self._require_pair(
            session_id=fields["session_id"],
            candidate_id=fields["candidate_id"],
        )
        if session.freeform_digest is not None or any(
            item.session_id == session.session_id for item in self._programs.values()
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        try:
            design = FreeformDesign.from_mapping(fields["design"])
            digest = fields["design_sha256"]
            if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
                raise ValueError
            if digest != design.digest:
                raise ValueError
        except (FreeformContractError, ValueError, TypeError):
            raise _ServiceError(WorkerWireErrorCode.INVALID_INPUT) from None
        try:
            compiled = compile_freeform(design, document=session.value.doc)
        except FreeformCompileError:
            raise _ServiceError(WorkerWireErrorCode.CAD_FAILURE) from None
        except BaseException:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        if not (
            compiled.document is session.value.doc
            and compiled.design_id == design.id
            and compiled.design_digest == digest
            and compiled.created_document is False
            and getattr(compiled.result_object, "VibeCADFreeformSchemaVersion", None)
            == design.schema_version
            and getattr(compiled.result_object, "VibeCADFreeformDesignId", None) == design.id
            and getattr(compiled.result_object, "VibeCADFreeformDesignDigest", None) == digest
            and getattr(compiled.result_object, "VibeCADFreeformDesignJson", None)
            == design.to_canonical_json()
        ):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        session.freeform_digest = digest
        session.freeform_design_id = design.id
        session.freeform_design_json = design.to_canonical_json()
        session.freeform_schema_version = design.schema_version
        return {
            "design_id": design.id,
            "design_sha256": digest,
            "result_name": compiled.result_object.Name,
        }

    def _close_session(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"session_id"})
        session = self._session(fields["session_id"])
        failed = False
        for program in tuple(self._programs.values()):
            if program.session_id == session.session_id:
                try:
                    self._close_program(program)
                except _ServiceError:
                    failed = True
        try:
            self._engine.close(session.value)
        except BaseException:
            failed = True
        try:
            _close_cad_bridge(session.cad_bridge)
        except BaseException:
            failed = True
        self._sessions.pop(session.session_id, None)
        if failed:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
        return {"session_id": session.session_id}

    def _observe(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(
            params,
            {"session_id", "capability_kind", "capability_id"},
        )
        session, capability = self._require_observation_pair(
            session_id=fields["session_id"],
            capability_kind=fields["capability_kind"],
            capability_id=fields["capability_id"],
        )
        if any(item.session_id == session.session_id for item in self._programs.values()):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        if type(capability) is _Candidate:
            context = _candidate_cwd(capability)
            before = _candidate_entries(capability)
            if before != (capability.model_identity, capability.step_identity):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        else:
            context = _revision_cwd(capability)
            before = _capture_revision_entries(capability)
            if before != capability.entries:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        try:
            with context:
                objects = tuple(session.value.doc.Objects)  # type: ignore[attr-defined]
                shape = None if not objects else _shape_observation(session.value)
                entities = _entity_observations(session.value)
                components = _component_observations(session.value)
                interferences = _interference_observations(session.value)
                bom = _bom_observation(session.value, components)
        except _ServiceError:
            raise
        except BaseException:
            raise _ServiceError(WorkerWireErrorCode.CAD_FAILURE) from None
        if type(capability) is _Candidate:
            after = _candidate_entries(capability)
        else:
            after = _capture_revision_entries(capability)
        if after != before:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        return {
            "shape": None if shape is None else shape.to_mapping(),
            "entities": [item.to_mapping() for item in entities],
            "components": [item.to_mapping() for item in components],
            "interferences": [item.to_mapping() for item in interferences],
            "bom": None if bom is None else bom.to_mapping(),
        }

    def _render_release(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"session_id", "revision_id"})
        session, capability = self._require_observation_pair(
            session_id=fields["session_id"],
            capability_kind="revision",
            capability_id=fields["revision_id"],
        )
        if type(capability) is not _Revision or any(
            item.session_id == session.session_id for item in self._programs.values()
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        before = _capture_revision_entries(capability)
        if before != capability.entries:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        try:
            with _revision_cwd(capability):
                components = _component_observations(session.value)
                bom = _bom_observation(session.value, components)
                if bom is None or not bom.complete:
                    raise ValueError
                from vibecad.feedback.release_drawing import (  # noqa: PLC0415
                    DRAWING_VIEWS,
                    render_assembly_drawing,
                )

                drawing, balloon_items = render_assembly_drawing(
                    session.value,
                    bom=bom,
                    project_id=capability.project_id,
                    revision_id=capability.store_revision_id,
                )
        except _ServiceError:
            raise
        except BaseException:
            raise _ServiceError(WorkerWireErrorCode.CAD_FAILURE) from None
        if _capture_revision_entries(capability) != before:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        return {
            "revision_id": capability.store_revision_id,
            "bom": bom.to_mapping(),
            "drawing_pdf_base64": base64.b64encode(drawing).decode("ascii"),
            "view_names": list(DRAWING_VIEWS),
            "balloon_items": [list(item) for item in balloon_items],
        }

    def _validation_directory(
        self,
        capability_mapping: object | None,
        descriptors: tuple[int, ...],
    ) -> _ValidationDirectory:
        if sys.platform == "win32":
            if descriptors or capability_mapping is None:
                raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
            try:
                capability = _windows_files.capability_from_mapping(capability_mapping)
                entries = _windows_files.capture_entries(
                    capability,
                    maximum_entries=_MAX_DIRECTORY_ENTRIES,
                )
            except (OSError, TypeError, ValueError):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
            return _ValidationDirectory(
                descriptor=-1,
                identity=None,
                capability=capability,
                entries=entries,
            )
        if len(descriptors) != 1 or capability_mapping is not None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        descriptor = descriptors[0]
        try:
            os.set_inheritable(descriptor, False)
            current = os.fstat(descriptor)
            if not _private_directory(current):
                raise OSError
            entries = _capture_private_entries(descriptor)
        except (OSError, _ServiceError):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        return _ValidationDirectory(
            descriptor=descriptor,
            identity=_directory_identity(current),
            capability=None,
            entries=entries,
        )

    def _validate_import(
        self,
        params: object,
        descriptors: tuple[int, ...],
        *,
        normalize: bool,
    ) -> dict[str, object]:
        field_names = {"name"}
        if sys.platform == "win32":
            field_names.add("path_capability")
        fields = _exact_mapping(params, field_names)
        name = _identifier(fields["name"], _STAGE_NAME)
        access = self._validation_directory(
            fields.get("path_capability"),
            descriptors,
        )
        before = access.entries
        before_mapping = dict(before)
        target_before = before_mapping.get(name)
        if target_before is None or target_before.size <= 0:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        if access.capability is None:
            with _directory_cwd(access.descriptor, access.identity):  # type: ignore[arg-type]
                if normalize:
                    evidence = self._engine.validate_import(Path(name))
                else:
                    evidence = self._engine.revalidate_normalized_import(Path(name))
                digest, size, target_after_hash = _hash_capability_entry(
                    None,
                    name,
                )
        else:
            if type(target_before) is not _windows_files.WindowsEntryIdentity:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            with _windows_files.directory_cwd(access.capability):
                with _windows_files.cad_staging_directory() as staging:
                    staged = _windows_files.stage_cad_input(
                        access.capability,
                        target_before,
                        staging,
                        name,
                    )
                    staged_path = _windows_files.validate_entry(staged)
                    if normalize:
                        evidence = self._engine.validate_import(staged_path)
                        normalized = _windows_files.capture_cad_output(
                            staging,
                            name,
                        )
                        published = _windows_files.publish_cad_output(
                            staging,
                            normalized,
                            access.capability,
                            name,
                            expected_destination=target_before,
                        )
                        expected_after = published
                    else:
                        with _windows_files.cad_directory_cwd(staging):
                            evidence = self._engine.revalidate_normalized_import(Path(name))
                        if _windows_files.validate_entry(staged) != staged_path:
                            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                        expected_after = target_before
                digest, size, target_after_hash = _hash_capability_entry(
                    access.capability,
                    name,
                    expected=expected_after,
                )
        if access.capability is None:
            after = _capture_private_entries(access.descriptor)
        else:
            try:
                after = _windows_files.capture_entries(
                    access.capability,
                    maximum_entries=_MAX_DIRECTORY_ENTRIES,
                )
            except (OSError, TypeError, ValueError):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        after_mapping = dict(after)
        target_after = after_mapping.get(name)
        if (
            target_after is None
            or target_after_hash != target_after
            or set(after_mapping) != set(before_mapping)
            or any(
                after_mapping[entry_name] != entry_identity
                for entry_name, entry_identity in before
                if entry_name != name
            )
            or (not normalize and target_after != target_before)
        ):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        result = _evidence_mapping(evidence)
        if result != {"sha256": digest, "size_bytes": size}:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        return result

    def _validate_materialization(
        self,
        params: object,
        descriptors: tuple[int, ...],
    ) -> dict[str, object]:
        field_names = {"path_capability"} if sys.platform == "win32" else set()
        fields = _exact_mapping(params, field_names)
        access = self._validation_directory(
            fields.get("path_capability"),
            descriptors,
        )
        before = access.entries
        before_mapping = dict(before)
        if (
            before_mapping.get("model.FCStd") is None
            or before_mapping.get("model.step") is None
            or before_mapping["model.FCStd"].size <= 0
            or before_mapping["model.step"].size <= 0
        ):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        if access.capability is None:
            with _directory_cwd(access.descriptor, access.identity):  # type: ignore[arg-type]
                evidence = self._engine.validate_materialization(
                    fcstd=Path("model.FCStd"),
                    step=Path("model.step"),
                )
                fcstd_sha256, fcstd_size, fcstd_identity = _hash_capability_entry(
                    None,
                    "model.FCStd",
                    expected=before_mapping["model.FCStd"],
                )
                step_sha256, step_size, step_identity = _hash_capability_entry(
                    None,
                    "model.step",
                    expected=before_mapping["model.step"],
                )
        else:
            source_fcstd = before_mapping["model.FCStd"]
            source_step = before_mapping["model.step"]
            if (
                type(source_fcstd) is not _windows_files.WindowsEntryIdentity
                or type(source_step) is not _windows_files.WindowsEntryIdentity
            ):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            with _windows_files.directory_cwd(access.capability):
                with _windows_files.cad_staging_directory() as staging:
                    staged_fcstd = _windows_files.stage_cad_input(
                        access.capability,
                        source_fcstd,
                        staging,
                        "model.FCStd",
                    )
                    staged_step = _windows_files.stage_cad_input(
                        access.capability,
                        source_step,
                        staging,
                        "model.step",
                    )
                    fcstd_path = _windows_files.validate_entry(staged_fcstd)
                    step_path = _windows_files.validate_entry(staged_step)
                    evidence = self._engine.validate_materialization(
                        fcstd=fcstd_path,
                        step=step_path,
                    )
                    if (
                        _windows_files.validate_entry(staged_fcstd) != fcstd_path
                        or _windows_files.validate_entry(staged_step) != step_path
                    ):
                        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                fcstd_sha256, fcstd_size, fcstd_identity = _hash_capability_entry(
                    access.capability,
                    "model.FCStd",
                    expected=source_fcstd,
                )
                step_sha256, step_size, step_identity = _hash_capability_entry(
                    access.capability,
                    "model.step",
                    expected=source_step,
                )
        if access.capability is None:
            after = _capture_private_entries(access.descriptor)
        else:
            try:
                after = _windows_files.capture_entries(
                    access.capability,
                    maximum_entries=_MAX_DIRECTORY_ENTRIES,
                )
            except (OSError, TypeError, ValueError):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        if (
            after != before
            or fcstd_identity != before_mapping["model.FCStd"]
            or step_identity != before_mapping["model.step"]
        ):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        result = _materialization_mapping(evidence)
        if result != {
            "fcstd_sha256": fcstd_sha256,
            "fcstd_size_bytes": fcstd_size,
            "step_sha256": step_sha256,
            "step_size_bytes": step_size,
        }:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        return result

    def _begin_program(
        self,
        params: object,
        descriptors: tuple[int, ...],
    ) -> dict[str, object]:
        if type(params) is not dict:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        if set(params) == {"session_id", "candidate_id", "program"}:
            if descriptors:
                raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
            fields = params
            artifact_envelope = None
            artifact_path_capability = None
        elif sys.platform == "win32" and set(params) == {
            "session_id",
            "candidate_id",
            "program",
            "artifact_snapshot",
            "artifact_path_capability",
        }:
            if descriptors:
                raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
            fields = params
            artifact_envelope = _artifact_snapshot_envelope(fields["artifact_snapshot"])
            try:
                artifact_path_capability = _windows_files.capability_from_mapping(
                    fields["artifact_path_capability"]
                )
                _windows_files.validate_directory(artifact_path_capability)
            except (OSError, TypeError, ValueError):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        elif sys.platform != "win32" and set(params) == {
            "session_id",
            "candidate_id",
            "program",
            "artifact_snapshot",
        }:
            if len(descriptors) != 1:
                raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
            fields = params
            artifact_envelope = _artifact_snapshot_envelope(fields["artifact_snapshot"])
            artifact_path_capability = None
        else:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        session, candidate = self._require_pair(
            session_id=fields["session_id"],
            candidate_id=fields["candidate_id"],
        )
        if any(item.session_id == session.session_id for item in self._programs.values()):
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        if session.freeform_digest is not None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        resolver: _ReviewedArtifactRunResolver | None = None
        run_token: object | None = None
        resolver_bound = False
        execution = None

        def close_pending() -> None:
            if execution is not None:
                with contextlib.suppress(BaseException):
                    execution.close()
            if resolver is not None and not resolver_bound:
                with contextlib.suppress(BaseException):
                    resolver.close()

        try:
            source = ModelProgram.from_mapping(fields["program"])
            if artifact_envelope is not None and (
                artifact_envelope["task_id"] != source.task_id
                or artifact_envelope["project_id"] != candidate.project_id
                or artifact_envelope["base_revision"] != source.base_revision
                or source.base_revision != candidate.base_revision_id
            ):
                raise _ServiceError(WorkerWireErrorCode.INVALID_INPUT)
            validated = self._engine.validate_program(source)
            binding = SessionBinding(
                project_id=candidate.project_id,
                revision_id=candidate.revision_id,
                session=session.value,
            )
            active = ActiveCandidate(
                project_id=candidate.project_id,
                base_head=ProjectHead(
                    project_id=candidate.project_id,
                    generation=0,
                    revision_id=candidate.base_revision_id,
                    manifest_sha256="0" * 64,
                ),
                binding=binding,
                model_path=_engine_entry_path(
                    candidate.directory_capability,
                    "model.FCStd",
                ),
                step_path=_engine_entry_path(
                    candidate.directory_capability,
                    "model.step",
                ),
            )
            if artifact_envelope is not None:
                resolver, run_token = _open_artifact_run_resolver(
                    -1 if artifact_path_capability is not None else descriptors[0],
                    artifact_envelope,
                    directory_capability=artifact_path_capability,
                )
            prepare_kwargs: dict[str, object] = {}
            if resolver is not None:
                prepare_kwargs = {
                    "artifact_resolver": resolver,
                    "artifact_run_token": run_token,
                }
            execution = self._engine._prepare_program_execution(
                program=validated,
                candidate=active,
                **prepare_kwargs,
            )
            if artifact_envelope is not None:
                if type(execution) is not _ValidatedProgramExecution:
                    raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
                execution._bind_run_resource(resolver)
                resolver_bound = True
            command_ids = tuple(item.id for item in validated.commands)
            deadlines = tuple(item.resource_budget.max_runtime_ms for item in validated.commands)
        except (ExecutorError, AdapterError):
            close_pending()
            raise
        except _ServiceError:
            close_pending()
            raise
        except Exception:
            close_pending()
            raise _ServiceError(WorkerWireErrorCode.INVALID_INPUT) from None
        program_id = f"worker_program_{secrets.token_hex(16)}"
        self._programs[program_id] = _Program(
            program_id=program_id,
            session_id=session.session_id,
            candidate_id=candidate.candidate_id,
            command_ids=command_ids,
            deadlines_ms=deadlines,
            execution=execution,
            artifact_resolver=resolver,
            artifact_run_token=run_token,
        )
        return {
            "program_id": program_id,
            "command_ids": list(command_ids),
            "command_deadlines_ms": list(deadlines),
        }

    def _execute_command(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"program_id", "index"})
        program = self._program(fields["program_id"])
        index = fields["index"]
        if (
            type(index) is not int
            or index != program.next_index
            or index < 0
            or index >= len(program.command_ids)
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        try:
            outcome = program.execution.step()
            done = program.execution.done
            result = {
                "index": index,
                "command_id": program.command_ids[index],
                "runtime_limit_ms": program.deadlines_ms[index],
                "done": done,
                "outcome": _outcome_mapping(outcome),
            }
        except BaseException:
            try:
                self._close_program(program)
            except _ServiceError:
                pass
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        program.next_index += 1
        if done:
            self._close_program(program)
        return result

    def _export_step(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"session_id", "candidate_id"})
        session, candidate = self._require_pair(
            session_id=fields["session_id"],
            candidate_id=fields["candidate_id"],
        )
        if any(item.session_id == session.session_id for item in self._programs.values()):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        with _candidate_cwd(candidate):
            current_model, current_step = _candidate_entries(candidate)
            if current_model != candidate.model_identity or current_step != candidate.step_identity:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            try:
                if candidate.directory_capability is not None:
                    with _windows_files.cad_staging_directory() as staging:
                        model_path = _windows_files.cad_output_path(
                            staging,
                            "model.FCStd",
                        )
                        reserved_step = _windows_files.reserve_cad_output(
                            staging,
                            "model.step",
                        )
                        step_path = _windows_files.validate_entry(reserved_step)
                        _export_session_step(
                            session=session.value,
                            model_path=model_path,
                            step_path=step_path,
                        )
                        staged_step = _windows_files.capture_cad_output(
                            staging,
                            "model.step",
                        )
                        step = _windows_files.publish_cad_output(
                            staging,
                            staged_step,
                            candidate.directory_capability,
                            "model.step",
                            expected_destination=current_step,
                        )
                    model, captured_step = _candidate_entries(candidate)
                    if model != current_model or captured_step != step:
                        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                else:
                    _export_session_step(
                        session=session.value,
                        model_path=Path("model.FCStd"),
                        step_path=Path("model.step"),
                    )
                    model, step = _candidate_entries(candidate)
                    if model != current_model or _stable_file_identity(
                        step
                    ) != _stable_file_identity(current_step):
                        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                digest, size, hashed = _hash_capability_entry(
                    candidate.directory_capability,
                    "model.step",
                    expected=step,
                )
                if hashed != step:
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            except BaseException:
                raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        candidate.model_identity = model
        candidate.step_identity = step
        return {"sha256": digest, "size_bytes": size}

    def _ready(self, params: object) -> dict[str, object]:
        _exact_mapping(params, set())
        prepare_freecad_import()
        import FreeCAD  # noqa: PLC0415
        import Part  # noqa: F401, PLC0415

        version = FreeCAD.Version()
        freecad_version = ".".join(version[:3])
        return {
            "worker_pid": os.getpid(),
            "python_version": ".".join(map(str, os.sys.version_info[:3])),
            "freecad_version": freecad_version,
        }

    def _shutdown_worker(self, params: object) -> dict[str, object]:
        _exact_mapping(params, set())
        failed = False
        for program in tuple(self._programs.values()):
            try:
                self._close_program(program)
            except _ServiceError:
                failed = True
        for session in tuple(self._sessions.values()):
            try:
                self._engine.close(session.value)
            except Exception:
                failed = True
            try:
                _close_cad_bridge(session.cad_bridge)
            except Exception:
                failed = True
        self._sessions.clear()
        for candidate in tuple(self._candidates.values()):
            if candidate.directory_fd >= 0:
                try:
                    os.close(candidate.directory_fd)
                except OSError:
                    failed = True
        self._candidates.clear()
        for revision in tuple(self._revisions.values()):
            if revision.directory_fd >= 0:
                try:
                    os.close(revision.directory_fd)
                except OSError:
                    failed = True
        self._revisions.clear()
        if failed:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
        self._shutdown = True
        return {"closed": True}

    def dispatch(
        self,
        method: str,
        params: object,
        descriptors: tuple[int, ...],
    ) -> dict[str, object]:
        descriptor_methods = {
            "candidate.bind",
            "revision.bind",
            "validation.validate_import",
            "validation.revalidate_import",
            "validation.validate_materialization",
        }
        if sys.platform == "win32":
            if descriptors:
                raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        elif (method in descriptor_methods and not descriptors) or (
            method not in {*descriptor_methods, "program.begin"} and descriptors
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        handlers = {
            "worker.ready": self._ready,
            "candidate.bind": lambda value: self._bind(value, descriptors),
            "candidate.release": self._release_candidate,
            "revision.bind": lambda value: self._bind_revision(value, descriptors),
            "revision.release": self._release_revision,
            "session.create_empty": self._create_empty,
            "session.load_fcstd": self._load,
            "session.load_revision": self._load_revision,
            "session.checkpoint_fcstd": self._checkpoint,
            "session.compile_freeform": self._compile_freeform,
            "session.observe": self._observe,
            "session.render_release": self._render_release,
            "session.close": self._close_session,
            "program.begin": lambda value: self._begin_program(value, descriptors),
            "program.execute_command": self._execute_command,
            "session.export_step": self._export_step,
            "validation.validate_import": lambda value: self._validate_import(
                value,
                descriptors,
                normalize=True,
            ),
            "validation.revalidate_import": lambda value: self._validate_import(
                value,
                descriptors,
                normalize=False,
            ),
            "validation.validate_materialization": lambda value: self._validate_materialization(
                value, descriptors
            ),
            "worker.shutdown": self._shutdown_worker,
        }
        handler = handlers.get(method)
        if handler is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        return handler(params)

    def close(self) -> None:
        for program in tuple(self._programs.values()):
            with contextlib.suppress(BaseException):
                self._close_program(program)
        for session in tuple(self._sessions.values()):
            with contextlib.suppress(Exception):
                self._engine.close(session.value)
            with contextlib.suppress(Exception):
                _close_cad_bridge(session.cad_bridge)
        self._sessions.clear()
        for candidate in tuple(self._candidates.values()):
            if candidate.directory_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(candidate.directory_fd)
        self._candidates.clear()
        for revision in tuple(self._revisions.values()):
            if revision.directory_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(revision.directory_fd)
        self._revisions.clear()


def _recv_header_with_descriptors(
    connection: socket.socket,
) -> tuple[bytes, tuple[int, ...]]:
    if sys.platform == "win32":
        # Windows Worker generations use an inherited private socket inside a
        # parent-owned Job Object.  Path capabilities are carried in the
        # canonical request body; Winsock has no SCM_RIGHTS ancillary channel.
        return _recv_exact(connection, 4), ()
    header = bytearray()
    descriptors: list[int] = []
    ancillary_size = socket.CMSG_SPACE(array.array("i", range(4)).itemsize * 4)
    try:
        while len(header) < 4:
            fragment, ancillary, flags, _address = connection.recvmsg(
                4 - len(header),
                ancillary_size,
            )
            if not fragment:
                raise EOFError
            header.extend(fragment)
            unexpected = False
            for level, kind, data in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    unexpected = True
                    continue
                received = array.array("i")
                if len(data) % received.itemsize:
                    unexpected = True
                    continue
                received.frombytes(data)
                descriptors.extend(received)
            if (
                unexpected
                or flags & (getattr(socket, "MSG_CTRUNC", 0) | getattr(socket, "MSG_TRUNC", 0))
                or len(descriptors) > 1
            ):
                raise WorkerCodecError("unexpected ancillary data")
    except BaseException:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    return bytes(header), tuple(descriptors)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        fragment = connection.recv(remaining)
        if not fragment:
            raise EOFError
        chunks.append(fragment)
        remaining -= len(fragment)
    return b"".join(chunks)


def _recv_request(connection: socket.socket) -> tuple[dict[str, object], tuple[int, ...]]:
    header, descriptors = _recv_header_with_descriptors(connection)
    size = struct.unpack(">I", header)[0]
    if size <= 0 or size > MAX_WORKER_REQUEST_BYTES:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise WorkerCodecError("invalid Worker request frame")
    try:
        request = decode_worker_request(_recv_exact(connection, size))
    except BaseException:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    return request, descriptors


def _send_response(connection: socket.socket, response: dict[str, object]) -> None:
    raw = encode_worker_response(response)
    connection.sendall(struct.pack(">I", len(raw)) + raw)


def serve_worker(connection: socket.socket, generation_id: str) -> int:
    service = WorkerService(generation_id)
    try:
        while True:
            try:
                request, descriptors = _recv_request(connection)
            except EOFError:
                return 0
            if request["generation_id"] != generation_id:
                return 2
            internal = False
            succeeded = False
            response: dict[str, object] | None = None
            try:
                result = service.dispatch(
                    request["method"],  # type: ignore[arg-type]
                    request["params"],
                    descriptors,
                )
                succeeded = True
            except _ServiceError as error:
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=error.code,
                )
                internal = error.code is WorkerWireErrorCode.INTERNAL_ERROR
            except ExecutorError as error:
                code = _executor_code(error)
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=code,
                )
                internal = code is WorkerWireErrorCode.INTERNAL_ERROR
            except AdapterError:
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=WorkerWireErrorCode.INVALID_INPUT,
                )
            except BaseException:
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=WorkerWireErrorCode.INTERNAL_ERROR,
                )
                internal = True
            finally:
                if request["method"] not in {"candidate.bind", "revision.bind"} or not succeeded:
                    for descriptor in descriptors:
                        with contextlib.suppress(OSError):
                            os.close(descriptor)
            if response is None:
                response = success_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    result=result,
                )
            _send_response(connection, response)
            if internal or service.shutdown_requested:
                return 0 if service.shutdown_requested and not internal else 3
    finally:
        service.close()


__all__ = ("WorkerService", "serve_worker")
