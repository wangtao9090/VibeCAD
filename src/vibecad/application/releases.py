"""Revision-bound CAD release drafts, approval records, and package resources."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import shutil
import stat
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from vibecad import _file_compat
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.interaction.cad import CadExecutionPort, ReleaseCadEvidence
from vibecad.interaction.storage import os
from vibecad.validation import BomObservation
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER, SCHEMA_VERSION
from vibecad.workflow.state import TaskArtifactRef, TaskRun, TaskStatus, VerificationReport
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
)

MAX_RELEASES = 4096
MAX_RELEASE_RECORD_BYTES = 64 * 1024
MAX_RELEASE_PACKAGE_BYTES = 1024 * 1024 * 1024
MAX_RELEASE_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_RELEASE_RESOURCE_BYTES = 64 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024

_CREATE_KEY = re.compile(r"release_create_[0-9a-f]{32}\Z")
_APPROVE_KEY = re.compile(r"release_approve_[0-9a-f]{32}\Z")
_RELEASE_ID = re.compile(r"release_[0-9a-f]{32}\Z")
_TASK_ID = re.compile(r"task_[0-9a-f]{32}\Z")
_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}\Z")
_REVISION_ID = re.compile(r"revision_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RESOURCE_URI = re.compile(
    r"vibecad://release/(release_[0-9a-f]{32})/"
    r"(assembly-drawing\.pdf|bom\.json|bom\.csv|manifest\.json|"
    r"validation-report\.json|vibecad-release\.zip)\Z"
)
_RECORD_DOMAIN = b"vibecad-release-record-v1\0"
_RELEASE_ID_DOMAIN = b"vibecad-release-id-v1\0"

_DERIVED_NAMES = (
    "bom.json",
    "bom.csv",
    "assembly-drawing.pdf",
    "manifest.json",
    "validation-report.json",
)
_DIRECTORY_NAMES = ("record.json", *_DERIVED_NAMES, "vibecad-release.zip")
_PACKAGE_NAMES = (
    "model.FCStd",
    "model.step",
    "bom.json",
    "bom.csv",
    "assembly-drawing.pdf",
    "manifest.json",
    "validation-report.json",
)
_MEDIA_TYPES = {
    "model.FCStd": "application/vnd.freecad.fcstd",
    "model.step": "model/step",
    "bom.json": "application/json",
    "bom.csv": "text/csv",
    "assembly-drawing.pdf": "application/pdf",
    "manifest.json": "application/json",
    "validation-report.json": "application/json",
    "vibecad-release.zip": "application/zip",
}


class ReleaseStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class ReleaseErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INTEGRITY_FAILURE = "integrity_failure"
    CAD_FAILURE = "cad_failure"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"


class ReleaseError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: ReleaseErrorCode) -> None:
        self.code = code
        self.args = (code.value,)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseFileRef:
    name: str
    media_type: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if (
            self.name not in {*_PACKAGE_NAMES, "vibecad-release.zip"}
            or self.media_type != _MEDIA_TYPES[self.name]
            or type(self.sha256) is not str
            or _DIGEST.fullmatch(self.sha256) is None
            or type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= MAX_RELEASE_PACKAGE_BYTES
            or (self.name == "vibecad-release.zip" and self.size_bytes > MAX_RELEASE_RESOURCE_BYTES)
        ):
            raise ValueError("invalid release file reference")

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ReleaseFileRef:
        if type(value) is not dict or set(value) != {
            "name",
            "media_type",
            "sha256",
            "size_bytes",
        }:
            raise ValueError("invalid release file reference")
        return cls(
            name=value["name"],
            media_type=value["media_type"],
            sha256=value["sha256"],
            size_bytes=value["size_bytes"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseRecord:
    release_id: str
    create_key: str
    task_id: str
    task_generation: int
    project_id: str
    revision_id: str
    revision_manifest_sha256: str
    verification_id: str
    verification_digest: str
    observation_digest: str
    manifest: ReleaseFileRef
    package: ReleaseFileRef
    drawing: ReleaseFileRef
    bom_json: ReleaseFileRef
    bom_csv: ReleaseFileRef
    validation_report: ReleaseFileRef
    status: ReleaseStatus = ReleaseStatus.DRAFT
    generation: int = 0
    approved_at_ms: int | None = None
    approval_key_sha256: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.release_id) is not str
            or _RELEASE_ID.fullmatch(self.release_id) is None
            or type(self.create_key) is not str
            or _CREATE_KEY.fullmatch(self.create_key) is None
            or type(self.task_id) is not str
            or _TASK_ID.fullmatch(self.task_id) is None
            or type(self.project_id) is not str
            or _PROJECT_ID.fullmatch(self.project_id) is None
            or type(self.revision_id) is not str
            or _REVISION_ID.fullmatch(self.revision_id) is None
            or type(self.task_generation) is not int
            or not 0 <= self.task_generation <= MAX_SAFE_JSON_INTEGER
            or type(self.generation) is not int
            or self.generation not in {0, 1}
        ):
            raise ValueError("invalid release identity")
        for digest in (
            self.revision_manifest_sha256,
            self.verification_digest,
            self.observation_digest,
        ):
            if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
                raise ValueError("invalid release digest")
        if type(self.verification_id) is not str or not self.verification_id:
            raise ValueError("invalid verification identity")
        if (
            type(self.manifest) is not ReleaseFileRef
            or self.manifest.name != "manifest.json"
            or type(self.package) is not ReleaseFileRef
            or self.package.name != "vibecad-release.zip"
            or type(self.drawing) is not ReleaseFileRef
            or self.drawing.name != "assembly-drawing.pdf"
            or type(self.bom_json) is not ReleaseFileRef
            or self.bom_json.name != "bom.json"
            or type(self.bom_csv) is not ReleaseFileRef
            or self.bom_csv.name != "bom.csv"
            or type(self.validation_report) is not ReleaseFileRef
            or self.validation_report.name != "validation-report.json"
        ):
            raise ValueError("invalid release artifacts")
        if type(self.status) is not ReleaseStatus:
            raise ValueError("invalid release status")
        if self.status is ReleaseStatus.DRAFT:
            if (
                self.generation != 0
                or self.approved_at_ms is not None
                or self.approval_key_sha256 is not None
            ):
                raise ValueError("invalid draft release state")
        elif (
            self.generation != 1
            or type(self.approved_at_ms) is not int
            or not 0 < self.approved_at_ms <= MAX_SAFE_JSON_INTEGER
            or type(self.approval_key_sha256) is not str
            or _DIGEST.fullmatch(self.approval_key_sha256) is None
        ):
            raise ValueError("invalid approved release state")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "release_id": self.release_id,
            "create_key": self.create_key,
            "task_id": self.task_id,
            "task_generation": self.task_generation,
            "project_id": self.project_id,
            "revision_id": self.revision_id,
            "revision_manifest_sha256": self.revision_manifest_sha256,
            "verification_id": self.verification_id,
            "verification_digest": self.verification_digest,
            "observation_digest": self.observation_digest,
            "manifest": self.manifest.to_mapping(),
            "package": self.package.to_mapping(),
            "drawing": self.drawing.to_mapping(),
            "bom_json": self.bom_json.to_mapping(),
            "bom_csv": self.bom_csv.to_mapping(),
            "validation_report": self.validation_report.to_mapping(),
            "status": self.status.value,
            "generation": self.generation,
            "approved_at_ms": self.approved_at_ms,
            "approval_key_sha256": self.approval_key_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ReleaseRecord:
        fields = {
            "schema_version",
            "release_id",
            "create_key",
            "task_id",
            "task_generation",
            "project_id",
            "revision_id",
            "revision_manifest_sha256",
            "verification_id",
            "verification_digest",
            "observation_digest",
            "manifest",
            "package",
            "drawing",
            "bom_json",
            "bom_csv",
            "validation_report",
            "status",
            "generation",
            "approved_at_ms",
            "approval_key_sha256",
        }
        if type(value) is not dict or set(value) != fields or value["schema_version"] != 1:
            raise ValueError("invalid release record")
        return cls(
            release_id=value["release_id"],
            create_key=value["create_key"],
            task_id=value["task_id"],
            task_generation=value["task_generation"],
            project_id=value["project_id"],
            revision_id=value["revision_id"],
            revision_manifest_sha256=value["revision_manifest_sha256"],
            verification_id=value["verification_id"],
            verification_digest=value["verification_digest"],
            observation_digest=value["observation_digest"],
            manifest=ReleaseFileRef.from_mapping(value["manifest"]),
            package=ReleaseFileRef.from_mapping(value["package"]),
            drawing=ReleaseFileRef.from_mapping(value["drawing"]),
            bom_json=ReleaseFileRef.from_mapping(value["bom_json"]),
            bom_csv=ReleaseFileRef.from_mapping(value["bom_csv"]),
            validation_report=ReleaseFileRef.from_mapping(value["validation_report"]),
            status=ReleaseStatus(value["status"]),
            generation=value["generation"],
            approved_at_ms=value["approved_at_ms"],
            approval_key_sha256=value["approval_key_sha256"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseResource:
    uri: str
    name: str
    media_type: str
    data: bytes


def _raise(code: ReleaseErrorCode) -> None:
    raise ReleaseError(code)


def _raise_task_store_error(error: TaskStoreError) -> None:
    if error.code is TaskStoreErrorCode.NOT_FOUND:
        _raise(ReleaseErrorCode.NOT_FOUND)
    if error.code in {
        TaskStoreErrorCode.CORRUPT_RECORD,
        TaskStoreErrorCode.UNSAFE_STORE,
    }:
        _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
    if error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
        _raise(ReleaseErrorCode.RESOURCE_EXHAUSTED)
    if error.code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
        _raise(ReleaseErrorCode.RECOVERY_REQUIRED)
    _raise(ReleaseErrorCode.STORE_FAILURE)


def _raise_revision_store_error(error: RevisionStoreError) -> None:
    if error.code is RevisionStoreErrorCode.NOT_FOUND:
        _raise(ReleaseErrorCode.NOT_FOUND)
    if error.code in {
        RevisionStoreErrorCode.INVALID_IDENTIFIER,
        RevisionStoreErrorCode.INVALID_INPUT,
    }:
        _raise(ReleaseErrorCode.INVALID_INPUT)
    if error.code in {
        RevisionStoreErrorCode.CORRUPT_RECORD,
        RevisionStoreErrorCode.CORRUPT_CONTENT,
        RevisionStoreErrorCode.UNSAFE_STORE,
    }:
        _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
    if error.code in {
        RevisionStoreErrorCode.BUDGET_EXCEEDED,
        RevisionStoreErrorCode.RESOURCE_EXHAUSTED,
    }:
        _raise(ReleaseErrorCode.RESOURCE_EXHAUSTED)
    if error.code in {
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
        RevisionStoreErrorCode.RECOVERY_REQUIRED,
        RevisionStoreErrorCode.CLEANUP_REQUIRED,
    }:
        _raise(ReleaseErrorCode.RECOVERY_REQUIRED)
    _raise(ReleaseErrorCode.STORE_FAILURE)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except Exception:
        _raise(ReleaseErrorCode.INTEGRITY_FAILURE)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _release_id(create_key: str) -> str:
    return (
        "release_"
        + hashlib.sha256(_RELEASE_ID_DOMAIN + create_key.encode("ascii")).hexdigest()[:32]
    )


def _file_ref(name: str, raw: bytes) -> ReleaseFileRef:
    return ReleaseFileRef(
        name=name,
        media_type=_MEDIA_TYPES[name],
        sha256=_digest(raw),
        size_bytes=len(raw),
    )


def _verification_digest(report: VerificationReport) -> str:
    return _digest(_canonical(report.to_mapping()))


def _bom_payload(
    bom: BomObservation,
    *,
    project_id: str,
    revision_id: str,
) -> tuple[bytes, bytes]:
    items = []
    for item_number, row in enumerate(bom.rows, start=1):
        item = row.to_mapping()
        item["item_number"] = item_number
        items.append(item)
    json_raw = _canonical(
        {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "revision_id": revision_id,
            "complete": bom.complete,
            "component_count": bom.component_count,
            "row_count": len(bom.rows),
            "total_quantity": bom.total_quantity,
            "total_mass_kg": bom.total_mass_kg,
            "items": items,
        }
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        (
            "item_number",
            "part_number",
            "description",
            "material",
            "density_kg_m3",
            "quantity",
            "unit_mass_kg",
            "total_mass_kg",
            "component_ids",
            "geometry_digest",
            "revision_id",
        )
    )
    for item_number, row in enumerate(bom.rows, start=1):
        writer.writerow(
            (
                item_number,
                row.part_number,
                row.description,
                row.material,
                row.density_kg_m3,
                row.quantity,
                row.unit_mass_kg,
                row.total_mass_kg,
                ";".join(row.component_ids),
                row.geometry_digest,
                revision_id,
            )
        )
    return json_raw, output.getvalue().encode("utf-8")


def _write_bytes(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        remaining = memoryview(raw)
        while remaining:
            written = os.write(descriptor, remaining[:_COPY_CHUNK_BYTES])
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _is_windows() -> bool:
    return sys.platform == "win32"


def _private_directory(path: Path) -> bool:
    """Validate the platform's private-directory security boundary."""

    if _is_windows():
        try:
            _file_compat.capture_windows_path(path, directory=True)
        except OSError:
            return False
        return True
    try:
        value = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(value.st_mode)
        and stat.S_IMODE(value.st_mode) == 0o700
        and value.st_uid == os.geteuid()
    )


def _private_regular_stat(value: os.stat_result) -> bool:
    """Check the POSIX portion of a regular-file boundary.

    On Windows, ``_StorageOS.fstat`` has already authenticated the opened
    descriptor against the file's protected DACL and File ID.  UID/mode bits
    are not an access-control authority there and must not be used as one.
    """

    return _is_windows() or (
        stat.S_IMODE(value.st_mode) == 0o600 and value.st_uid == os.geteuid()
    )


def _read_file_bytes(path: Path, *, maximum_bytes: int) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not _private_regular_stat(before)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum_bytes
        ):
            raise OSError
        output = bytearray()
        while len(output) < before.st_size:
            chunk = os.read(
                descriptor,
                min(_COPY_CHUNK_BYTES, before.st_size - len(output)),
            )
            if not chunk:
                raise OSError
            output.extend(chunk)
        if os.read(descriptor, 1):
            raise OSError
        after = os.fstat(descriptor)
        live = path.lstat()

        def identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                stat.S_IFMT(value.st_mode),
            )

        if identity(before) != identity(after) or identity(after) != identity(live):
            raise OSError
        return bytes(output)
    except OSError:
        _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                _raise(ReleaseErrorCode.RECOVERY_REQUIRED)


def _hash_file(path: Path, *, maximum_bytes: int) -> tuple[int, str]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or not _private_regular_stat(before)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum_bytes
        ):
            raise OSError
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        live = path.lstat()

        def identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
            )

        if identity(before) != identity(after) or identity(after) != identity(live):
            raise OSError
        return size, digest.hexdigest()
    except OSError:
        _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                _raise(ReleaseErrorCode.RECOVERY_REQUIRED)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    return info


def _copy_source_to_zip(
    archive: zipfile.ZipFile,
    *,
    path: Path,
    expected: RevisionArtifactRef,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (not _is_windows() and before.st_uid != os.geteuid())
            or before.st_nlink != 1
            or before.st_size != expected.size_bytes
        ):
            raise OSError
        digest = hashlib.sha256()
        size = 0
        with archive.open(_zip_info(expected.name), "w") as destination:
            while True:
                chunk = os.read(descriptor, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        after = os.fstat(descriptor)
        live = path.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (live.st_dev, live.st_ino, live.st_size, live.st_mtime_ns)
            or size != expected.size_bytes
            or digest.hexdigest() != expected.sha256
        ):
            raise OSError
    except OSError:
        _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                _raise(ReleaseErrorCode.RECOVERY_REQUIRED)


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def _root_lock(root: Path) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(str(root), threading.RLock())


class ReleaseStore:
    """Small durable store for immutable packages and one approval transition."""

    __slots__ = ("_identity", "_lock", "_root")

    def __init__(self, *, root: Path, expected_identity: tuple[int, int]) -> None:
        if type(root) is not type(Path("/")) or not root.is_absolute():
            _raise(ReleaseErrorCode.INVALID_INPUT)
        self._root = root
        self._identity = expected_identity
        self._lock = _root_lock(root)
        self._require_root()

    def _require_root(self) -> None:
        try:
            value = self._root.lstat()
        except OSError:
            _raise(ReleaseErrorCode.STORE_FAILURE)
        if not _private_directory(self._root) or (
            value.st_dev,
            value.st_ino,
        ) != self._identity:
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)

    @staticmethod
    def _record_raw(record: ReleaseRecord) -> bytes:
        body = record.to_mapping()
        return _canonical(
            {
                "body": body,
                "checksum": _digest(_RECORD_DOMAIN + _canonical(body)),
            }
        )

    def _directory(self, release_id: str) -> Path:
        return self._root / release_id

    def _load_unlocked(self, release_id: str) -> ReleaseRecord:
        if type(release_id) is not str or _RELEASE_ID.fullmatch(release_id) is None:
            _raise(ReleaseErrorCode.INVALID_INPUT)
        directory = self._directory(release_id)
        try:
            directory.lstat()
        except FileNotFoundError:
            _raise(ReleaseErrorCode.NOT_FOUND)
        except OSError:
            _raise(ReleaseErrorCode.STORE_FAILURE)
        if not _private_directory(directory):
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        try:
            names = tuple(sorted(path.name for path in directory.iterdir()))
        except OSError:
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        if names != tuple(sorted(_DIRECTORY_NAMES)):
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        raw = _read_file_bytes(
            directory / "record.json",
            maximum_bytes=MAX_RELEASE_RECORD_BYTES,
        )
        try:
            envelope = json.loads(raw)
            if type(envelope) is not dict or set(envelope) != {"body", "checksum"}:
                raise ValueError
            body = envelope["body"]
            if envelope["checksum"] != _digest(_RECORD_DOMAIN + _canonical(body)):
                raise ValueError
            record = ReleaseRecord.from_mapping(body)
        except ReleaseError:
            raise
        except Exception:
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        if record.release_id != release_id:
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        for reference in (
            record.manifest,
            record.package,
            record.drawing,
            record.bom_json,
            record.bom_csv,
            record.validation_report,
        ):
            path = directory / reference.name
            size, digest = _hash_file(path, maximum_bytes=MAX_RELEASE_PACKAGE_BYTES)
            if size != reference.size_bytes or digest != reference.sha256:
                _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        return record

    def load(self, release_id: str) -> ReleaseRecord:
        with self._lock:
            self._require_root()
            return self._load_unlocked(release_id)

    def create(
        self,
        *,
        record: ReleaseRecord,
        derived: dict[str, bytes],
        model_path: Path,
        model: RevisionArtifactRef,
        step_path: Path,
        step: RevisionArtifactRef,
    ) -> ReleaseRecord:
        with self._lock:
            self._require_root()
            target = self._directory(record.release_id)
            if target.exists():
                existing = self._load_unlocked(record.release_id)
                if (
                    existing.create_key == record.create_key
                    and existing.task_id == record.task_id
                    and existing.task_generation == record.task_generation
                    and existing.revision_id == record.revision_id
                ):
                    return existing
                _raise(ReleaseErrorCode.CONFLICT)
            if (
                sum(1 for path in self._root.iterdir() if _RELEASE_ID.fullmatch(path.name))
                >= MAX_RELEASES
            ):
                _raise(ReleaseErrorCode.RESOURCE_EXHAUSTED)
            temporary = self._root / f".{record.release_id}.{secrets.token_hex(16)}.tmp"
            try:
                temporary.mkdir(mode=0o700)
                for name in _DERIVED_NAMES:
                    _write_bytes(temporary / name, derived[name])
                package_path = temporary / "vibecad-release.zip"
                with zipfile.ZipFile(package_path, "w", allowZip64=True) as archive:
                    _copy_source_to_zip(archive, path=model_path, expected=model)
                    _copy_source_to_zip(archive, path=step_path, expected=step)
                    for name in _PACKAGE_NAMES[2:]:
                        archive.writestr(_zip_info(name), derived[name])
                os.chmod(package_path, 0o600)
                package_raw_digest = hashlib.sha256()
                package_size = 0
                with package_path.open("rb") as source:
                    while chunk := source.read(_COPY_CHUNK_BYTES):
                        package_raw_digest.update(chunk)
                        package_size += len(chunk)
                    # Windows rejects FlushFileBuffers for this read-only CRT
                    # descriptor.  The writer has already closed the ZIP handle;
                    # the following protected re-open and identity checks provide
                    # the corresponding fail-closed boundary.
                    if not _is_windows():
                        os.fsync(source.fileno())
                if not 1 <= package_size <= MAX_RELEASE_RESOURCE_BYTES:
                    _raise(ReleaseErrorCode.RESOURCE_EXHAUSTED)
                completed = replace(
                    record,
                    package=ReleaseFileRef(
                        name="vibecad-release.zip",
                        media_type=_MEDIA_TYPES["vibecad-release.zip"],
                        sha256=package_raw_digest.hexdigest(),
                        size_bytes=package_size,
                    ),
                )
                _write_bytes(temporary / "record.json", self._record_raw(completed))
                # A release directory is published only into an absent name.
                # The Windows adapter's directory rename validates both parent
                # capabilities; ``replace`` is deliberately file-only there.
                if _is_windows():
                    os.rename(temporary, target)
                else:
                    os.replace(temporary, target)
                root_fd = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
            except ReleaseError:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                _raise(ReleaseErrorCode.STORE_FAILURE)
            return self._load_unlocked(record.release_id)

    def approve(
        self,
        *,
        release_id: str,
        expected_generation: int,
        expected_package_sha256: str,
        approval_key: str,
    ) -> ReleaseRecord:
        with self._lock:
            self._require_root()
            current = self._load_unlocked(release_id)
            key_digest = _digest(approval_key.encode("ascii"))
            if current.status is ReleaseStatus.APPROVED:
                if (
                    current.approval_key_sha256 == key_digest
                    and current.package.sha256 == expected_package_sha256
                ):
                    return current
                _raise(ReleaseErrorCode.CONFLICT)
            if (
                current.generation != expected_generation
                or current.package.sha256 != expected_package_sha256
            ):
                _raise(ReleaseErrorCode.CONFLICT)
            approved = replace(
                current,
                status=ReleaseStatus.APPROVED,
                generation=1,
                approved_at_ms=time.time_ns() // 1_000_000,
                approval_key_sha256=key_digest,
            )
            path = self._directory(release_id) / "record.json"
            temporary = path.with_name(f".record.{secrets.token_hex(16)}.tmp")
            try:
                _write_bytes(temporary, self._record_raw(approved))
                os.replace(temporary, path)
                directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except Exception:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
                _raise(ReleaseErrorCode.RECOVERY_REQUIRED)
            return self._load_unlocked(release_id)

    def read_resource(self, uri: object) -> ReleaseResource:
        if type(uri) is not str or (match := _RESOURCE_URI.fullmatch(uri)) is None:
            _raise(ReleaseErrorCode.INVALID_INPUT)
        release_id, name = match.groups()
        with self._lock:
            record = self._load_unlocked(release_id)
            if name == "vibecad-release.zip" and record.status is not ReleaseStatus.APPROVED:
                _raise(ReleaseErrorCode.INVALID_STATE)
            reference = {
                "assembly-drawing.pdf": record.drawing,
                "bom.json": record.bom_json,
                "bom.csv": record.bom_csv,
                "manifest.json": record.manifest,
                "validation-report.json": record.validation_report,
                "vibecad-release.zip": record.package,
            }.get(name)
            path = self._directory(release_id) / name
            if reference is None:
                _raise(ReleaseErrorCode.NOT_FOUND)
            raw = _read_file_bytes(path, maximum_bytes=MAX_RELEASE_RESOURCE_BYTES)
            if len(raw) != reference.size_bytes or _digest(raw) != reference.sha256:
                _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
            return ReleaseResource(uri=uri, name=name, media_type=reference.media_type, data=raw)


class ReleaseService:
    __slots__ = ("_cad", "_revision_store", "_store", "_task_store")

    def __init__(
        self,
        *,
        store: ReleaseStore,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
        cad: CadExecutionPort,
    ) -> None:
        self._store = store
        self._task_store = task_store
        self._revision_store = revision_store
        self._cad = cad

    def _authority(
        self,
        *,
        task_id: str,
        expected_generation: int,
        revision_id: str,
    ) -> tuple[StoredTaskRun, TaskRun, RevisionRef, VerificationReport]:
        try:
            stored = self._task_store.load(task_id)
        except TaskStoreError as error:
            _raise_task_store_error(error)
        except KeyError:
            _raise(ReleaseErrorCode.NOT_FOUND)
        except Exception:
            _raise(ReleaseErrorCode.STORE_FAILURE)
        try:
            revision = self._revision_store.load_revision(
                stored.task_run.project_id,
                revision_id,
            )
        except RevisionStoreError as error:
            _raise_revision_store_error(error)
        except KeyError:
            _raise(ReleaseErrorCode.NOT_FOUND)
        except Exception:
            _raise(ReleaseErrorCode.STORE_FAILURE)
        task = stored.task_run
        if stored.generation != expected_generation:
            _raise(ReleaseErrorCode.CONFLICT)
        if (
            task.status is not TaskStatus.SUCCEEDED
            or task.committed_revision != revision_id
            or revision.project_id != task.project_id
        ):
            _raise(ReleaseErrorCode.INVALID_STATE)
        reports = tuple(
            report
            for report in task.verification_reports
            if report.passed
            and report.candidate_revision == revision.id
            and report.manifest_sha256 == revision.manifest_sha256
        )
        if len(reports) != 1:
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        if (
            len(task.artifacts) != 2
            or tuple(item.name for item in task.artifacts) != ("model.FCStd", "model.step")
            or len(revision.artifacts) != 1
        ):
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        for task_ref, revision_ref in zip(
            task.artifacts,
            (revision.model, revision.artifacts[0]),
            strict=True,
        ):
            if (
                type(task_ref) is not TaskArtifactRef
                or task_ref.id != revision_ref.id
                or task_ref.sha256 != revision_ref.sha256
                or task_ref.size_bytes != revision_ref.size_bytes
                or task_ref.name != revision_ref.name
                or task_ref.format != revision_ref.format
                or task_ref.candidate_revision != revision.id
            ):
                _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        return stored, task, revision, reports[0]

    def create_release(
        self,
        *,
        create_key: str,
        task_id: str,
        expected_generation: int,
        revision_id: str,
    ) -> ReleaseRecord:
        if (
            type(create_key) is not str
            or _CREATE_KEY.fullmatch(create_key) is None
            or type(task_id) is not str
            or _TASK_ID.fullmatch(task_id) is None
            or type(expected_generation) is not int
            or not 0 <= expected_generation <= MAX_SAFE_JSON_INTEGER
            or type(revision_id) is not str
            or _REVISION_ID.fullmatch(revision_id) is None
        ):
            _raise(ReleaseErrorCode.INVALID_INPUT)
        existing_id = _release_id(create_key)
        try:
            existing = self._store.load(existing_id)
        except ReleaseError as error:
            if error.code is not ReleaseErrorCode.NOT_FOUND:
                raise
        else:
            if (
                existing.task_id == task_id
                and existing.task_generation == expected_generation
                and existing.revision_id == revision_id
            ):
                return existing
            _raise(ReleaseErrorCode.CONFLICT)
        stored_before, task, revision, report = self._authority(
            task_id=task_id,
            expected_generation=expected_generation,
            revision_id=revision_id,
        )
        if revision.model.size_bytes + revision.artifacts[0].size_bytes > (
            MAX_RELEASE_RESOURCE_BYTES
        ):
            _raise(ReleaseErrorCode.RESOURCE_EXHAUSTED)
        try:
            evidence = self._cad.render_release(revision=revision)
        except Exception:
            _raise(ReleaseErrorCode.CAD_FAILURE)
        if type(evidence) is not ReleaseCadEvidence or evidence.revision_id != revision.id:
            _raise(ReleaseErrorCode.INTEGRITY_FAILURE)
        bom_json_raw, bom_csv_raw = _bom_payload(
            evidence.bom,
            project_id=task.project_id,
            revision_id=revision.id,
        )
        validation_raw = _canonical(report.to_mapping())
        file_refs = (
            revision.model,
            revision.artifacts[0],
            _file_ref("bom.json", bom_json_raw),
            _file_ref("bom.csv", bom_csv_raw),
            _file_ref("assembly-drawing.pdf", evidence.drawing_pdf),
            _file_ref("validation-report.json", validation_raw),
        )
        manifest_raw = _canonical(
            {
                "schema_version": SCHEMA_VERSION,
                "release_id": existing_id,
                "task_id": task.id,
                "task_generation": stored_before.generation,
                "project_id": task.project_id,
                "revision_id": revision.id,
                "revision_manifest_sha256": revision.manifest_sha256,
                "verification_id": report.id,
                "verification_digest": _verification_digest(report),
                "observation_digest": report.observation_digest,
                "drawing": {
                    "views": list(evidence.view_names),
                    "balloon_items": [list(item) for item in evidence.balloon_items],
                },
                "files": [
                    {
                        "name": item.name,
                        "media_type": _MEDIA_TYPES[item.name],
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in file_refs
                ],
            }
        )
        manifest = _file_ref("manifest.json", manifest_raw)
        placeholder_package = ReleaseFileRef(
            name="vibecad-release.zip",
            media_type=_MEDIA_TYPES["vibecad-release.zip"],
            sha256="0" * 64,
            size_bytes=1,
        )
        record = ReleaseRecord(
            release_id=existing_id,
            create_key=create_key,
            task_id=task.id,
            task_generation=stored_before.generation,
            project_id=task.project_id,
            revision_id=revision.id,
            revision_manifest_sha256=revision.manifest_sha256,
            verification_id=report.id,
            verification_digest=_verification_digest(report),
            observation_digest=report.observation_digest,
            manifest=manifest,
            package=placeholder_package,
            drawing=_file_ref("assembly-drawing.pdf", evidence.drawing_pdf),
            bom_json=_file_ref("bom.json", bom_json_raw),
            bom_csv=_file_ref("bom.csv", bom_csv_raw),
            validation_report=_file_ref("validation-report.json", validation_raw),
        )
        derived = {
            "bom.json": bom_json_raw,
            "bom.csv": bom_csv_raw,
            "assembly-drawing.pdf": evidence.drawing_pdf,
            "manifest.json": manifest_raw,
            "validation-report.json": validation_raw,
        }
        try:
            model_path = self._revision_store.revision_model_path(task.project_id, revision.id)
            step_path = self._revision_store.revision_artifact_path(
                task.project_id,
                revision.id,
                revision.artifacts[0].id,
            )
            result = self._store.create(
                record=record,
                derived=derived,
                model_path=model_path,
                model=revision.model,
                step_path=step_path,
                step=revision.artifacts[0],
            )
            stored_after = self._task_store.load(task.id)
            revision_after = self._revision_store.load_revision(task.project_id, revision.id)
        except ReleaseError:
            raise
        except Exception:
            _raise(ReleaseErrorCode.STORE_FAILURE)
        if stored_after != stored_before or revision_after != revision:
            _raise(ReleaseErrorCode.CONFLICT)
        return result

    def get_release(self, *, release_id: str) -> ReleaseRecord:
        return self._store.load(release_id)

    def approve_release(
        self,
        *,
        release_id: str,
        expected_generation: int,
        expected_package_sha256: str,
        approval_key: str,
    ) -> ReleaseRecord:
        if (
            type(release_id) is not str
            or _RELEASE_ID.fullmatch(release_id) is None
            or type(expected_generation) is not int
            or expected_generation not in {0, 1}
            or type(expected_package_sha256) is not str
            or _DIGEST.fullmatch(expected_package_sha256) is None
            or type(approval_key) is not str
            or _APPROVE_KEY.fullmatch(approval_key) is None
        ):
            _raise(ReleaseErrorCode.INVALID_INPUT)
        current = self._store.load(release_id)
        self._authority(
            task_id=current.task_id,
            expected_generation=current.task_generation,
            revision_id=current.revision_id,
        )
        return self._store.approve(
            release_id=release_id,
            expected_generation=expected_generation,
            expected_package_sha256=expected_package_sha256,
            approval_key=approval_key,
        )


def release_projection(record: ReleaseRecord) -> dict[str, object]:
    base = f"vibecad://release/{record.release_id}"
    return {
        "release_id": record.release_id,
        "status": record.status.value,
        "generation": record.generation,
        "task_id": record.task_id,
        "task_generation": record.task_generation,
        "project_id": record.project_id,
        "revision_id": record.revision_id,
        "revision_manifest_sha256": record.revision_manifest_sha256,
        "verification_id": record.verification_id,
        "verification_digest": record.verification_digest,
        "observation_digest": record.observation_digest,
        "manifest": {**record.manifest.to_mapping(), "resource_uri": f"{base}/manifest.json"},
        "drawing": {
            **record.drawing.to_mapping(),
            "resource_uri": f"{base}/assembly-drawing.pdf",
        },
        "bom_json": {**record.bom_json.to_mapping(), "resource_uri": f"{base}/bom.json"},
        "bom_csv": {**record.bom_csv.to_mapping(), "resource_uri": f"{base}/bom.csv"},
        "validation_report_uri": f"{base}/validation-report.json",
        "package": {
            **record.package.to_mapping(),
            "resource_uri": (
                f"{base}/vibecad-release.zip" if record.status is ReleaseStatus.APPROVED else None
            ),
        },
        "approved_at_ms": record.approved_at_ms,
    }


_API_MESSAGES = {
    ReleaseErrorCode.INVALID_INPUT: "The release request is invalid.",
    ReleaseErrorCode.NOT_FOUND: "The release or source was not found.",
    ReleaseErrorCode.INVALID_STATE: "The source Revision is not eligible for release.",
    ReleaseErrorCode.CONFLICT: "The release request conflicts with durable state.",
    ReleaseErrorCode.RESOURCE_EXHAUSTED: "The release capacity is exhausted.",
    ReleaseErrorCode.INTEGRITY_FAILURE: "The release integrity check failed.",
    ReleaseErrorCode.CAD_FAILURE: "The release drawing could not be generated.",
    ReleaseErrorCode.STORE_FAILURE: "The release store operation failed.",
    ReleaseErrorCode.RECOVERY_REQUIRED: "The release operation requires recovery.",
}


class ReleaseApi:
    __slots__ = ("_service",)

    def __init__(self, *, service: ReleaseService) -> None:
        self._service = service

    @staticmethod
    def _envelope(value: ReleaseRecord) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "result": release_projection(value),
            "error": None,
        }

    @staticmethod
    def _failure(error: ReleaseError) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "result": None,
            "error": {
                "schema_version": SCHEMA_VERSION,
                "code": error.code.value,
                "path": "",
                "message": _API_MESSAGES[error.code],
            },
        }

    @staticmethod
    def _request(request: object, fields: set[str]) -> dict[str, object]:
        if (
            type(request) is not dict
            or set(request) != fields
            or request.get("schema_version") != 1
        ):
            _raise(ReleaseErrorCode.INVALID_INPUT)
        return request

    def create_release(self, request: object) -> dict[str, object]:
        try:
            value = self._request(
                request,
                {
                    "schema_version",
                    "create_key",
                    "task_id",
                    "expected_generation",
                    "revision_id",
                },
            )
            return self._envelope(
                self._service.create_release(
                    create_key=value["create_key"],
                    task_id=value["task_id"],
                    expected_generation=value["expected_generation"],
                    revision_id=value["revision_id"],
                )
            )
        except ReleaseError as error:
            return self._failure(error)
        except Exception:
            return self._failure(ReleaseError(ReleaseErrorCode.RECOVERY_REQUIRED))

    def get_release(self, request: object) -> dict[str, object]:
        try:
            value = self._request(request, {"schema_version", "release_id"})
            return self._envelope(self._service.get_release(release_id=value["release_id"]))
        except ReleaseError as error:
            return self._failure(error)
        except Exception:
            return self._failure(ReleaseError(ReleaseErrorCode.RECOVERY_REQUIRED))

    def approve_release(self, request: object) -> dict[str, object]:
        try:
            value = self._request(
                request,
                {
                    "schema_version",
                    "release_id",
                    "expected_generation",
                    "expected_package_sha256",
                    "approval_key",
                },
            )
            return self._envelope(
                self._service.approve_release(
                    release_id=value["release_id"],
                    expected_generation=value["expected_generation"],
                    expected_package_sha256=value["expected_package_sha256"],
                    approval_key=value["approval_key"],
                )
            )
        except ReleaseError as error:
            return self._failure(error)
        except Exception:
            return self._failure(ReleaseError(ReleaseErrorCode.RECOVERY_REQUIRED))


__all__ = (
    "ReleaseApi",
    "ReleaseError",
    "ReleaseErrorCode",
    "ReleaseFileRef",
    "ReleaseRecord",
    "ReleaseResource",
    "ReleaseService",
    "ReleaseStatus",
    "ReleaseStore",
    "release_projection",
)
