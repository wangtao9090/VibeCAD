"""Private host authority for immutable reviewed CAD input snapshots.

The provider represented here is application-owned.  It acquires one exact
task/project/base/run snapshot and returns an opaque directory-FD lease.  The
lease validates the complete directory and duplicates its capability for a
Worker without ever accepting or exposing a filesystem path.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import stat
from enum import StrEnum
from typing import Protocol, runtime_checkable

from vibecad.execution.freecad_reviewed_artifact_inputs import (
    ReviewedArtifactCatalogSnapshot,
)
from vibecad.workflow.program import ValidatedProgram

REVIEWED_ARTIFACT_SNAPSHOT_KIND = "reviewed_artifact_snapshot_v1"
REVIEWED_ARTIFACT_SNAPSHOT_SCHEMA_VERSION = 1
REVIEWED_ARTIFACT_MANIFEST_NAME = "manifest.json"

_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_MANIFEST_MAX_BYTES = 512 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class TaskInputSnapshotErrorCode(StrEnum):
    """Stable private failures from host task-input authority."""

    INVALID_INPUT = "invalid_input"
    AUTHORITY_VIOLATION = "authority_violation"
    INTEGRITY_FAILURE = "integrity_failure"
    CLOSED = "closed"
    ACQUISITION_FAILED = "acquisition_failed"
    CLEANUP_FAILED = "cleanup_failed"


class TaskInputSnapshotError(ValueError):
    """Bounded path-free host task-input failure."""

    __slots__ = ("code",)

    def __init__(self, code: TaskInputSnapshotErrorCode) -> None:
        if type(code) is not TaskInputSnapshotErrorCode:
            raise TypeError("code must be a TaskInputSnapshotErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: TaskInputSnapshotErrorCode) -> None:
    raise TaskInputSnapshotError(code)


@runtime_checkable
class TaskInputSnapshotProvider(Protocol):
    """Acquire one authenticated immutable directory snapshot for a run."""

    def acquire(
        self,
        *,
        task_id: str,
        project_id: str,
        base_revision: str,
        run_id: str,
    ) -> TaskInputSnapshotLease: ...


@runtime_checkable
class TaskInputProgramPreflight(Protocol):
    """Trusted static route scan deciding whether a program needs artifacts."""

    def requires_artifact_snapshot(self, program: ValidatedProgram) -> bool: ...


class _OpaqueCapability:
    __slots__ = ()

    def __copy__(self):
        raise TypeError("task input snapshot capabilities cannot be copied")

    def __deepcopy__(self, memo: object):
        del memo
        raise TypeError("task input snapshot capabilities cannot be copied")

    def __reduce__(self):
        raise TypeError("task input snapshot capabilities cannot be serialized")

    def __reduce_ex__(self, protocol: object):
        del protocol
        raise TypeError("task input snapshot capabilities cannot be serialized")


def _copy_snapshot(snapshot: object) -> ReviewedArtifactCatalogSnapshot:
    if type(snapshot) is not ReviewedArtifactCatalogSnapshot:
        _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
    try:
        copied = ReviewedArtifactCatalogSnapshot(
            task_id=snapshot.task_id,
            project_id=snapshot.project_id,
            base_revision=snapshot.base_revision,
            run_id=snapshot.run_id,
            records=snapshot.records,
        )
        if copied.to_mapping() != snapshot.to_mapping():
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        return copied
    except TaskInputSnapshotError:
        raise
    except BaseException:
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)


def _canonical_manifest(snapshot: ReviewedArtifactCatalogSnapshot) -> bytes:
    try:
        return json.dumps(
            snapshot.to_mapping(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except BaseException:
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_uid, stat.S_IMODE(value.st_mode))


def _validate_directory_stat(value: os.stat_result) -> tuple[int, int, int, int]:
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != _DIRECTORY_MODE
    ):
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
    return _identity(value)


def _require_read_only(fd: int) -> None:
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except OSError:
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
    if flags & os.O_ACCMODE != os.O_RDONLY:
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)


def _read_regular_file(
    directory_fd: int,
    *,
    name: str,
    directory_device: int,
    exact_size: int,
    maximum_size: int,
) -> bytes:
    if (
        type(name) is not str
        or not name
        or "/" in name
        or name in {".", ".."}
        or type(exact_size) is not int
        or type(maximum_size) is not int
        or exact_size < 0
        or exact_size > maximum_size
    ):
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
    opened = -1
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        opened = os.open(name, _FILE_OPEN_FLAGS, dir_fd=directory_fd)
        current = os.fstat(opened)
        if (
            not stat.S_ISREG(before.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or before.st_dev != current.st_dev
            or before.st_ino != current.st_ino
            or current.st_dev != directory_device
            or current.st_uid != os.geteuid()
            or stat.S_IMODE(current.st_mode) != _FILE_MODE
            or current.st_nlink != 1
            or current.st_size != exact_size
            or current.st_size > maximum_size
        ):
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(opened, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(opened)
        entry_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(payload) != exact_size
            or _identity(after) != _identity(current)
            or after.st_size != current.st_size
            or after.st_nlink != current.st_nlink
            or entry_after.st_dev != current.st_dev
            or entry_after.st_ino != current.st_ino
            or entry_after.st_size != current.st_size
            or entry_after.st_nlink != current.st_nlink
        ):
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        return payload
    except TaskInputSnapshotError:
        raise
    except (OSError, UnicodeError):
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
    finally:
        if opened >= 0:
            try:
                os.close(opened)
            except OSError:
                _fail(TaskInputSnapshotErrorCode.CLEANUP_FAILED)


def _validate_snapshot_directory(
    directory_fd: int,
    snapshot: ReviewedArtifactCatalogSnapshot,
    expected_identity: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    try:
        _require_read_only(directory_fd)
        before = os.fstat(directory_fd)
        root_identity = _validate_directory_stat(before)
        if expected_identity is not None and root_identity != expected_identity:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        artifact_names = tuple(record.artifact_id for record in snapshot.records)
        if REVIEWED_ARTIFACT_MANIFEST_NAME in artifact_names:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        expected_entries = {REVIEWED_ARTIFACT_MANIFEST_NAME, *artifact_names}
        if set(os.listdir(directory_fd)) != expected_entries:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        expected_manifest = _canonical_manifest(snapshot)
        if len(expected_manifest) > _MANIFEST_MAX_BYTES:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        manifest = _read_regular_file(
            directory_fd,
            name=REVIEWED_ARTIFACT_MANIFEST_NAME,
            directory_device=before.st_dev,
            exact_size=len(expected_manifest),
            maximum_size=_MANIFEST_MAX_BYTES,
        )
        if not hmac.compare_digest(manifest, expected_manifest):
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        for record in snapshot.records:
            payload = _read_regular_file(
                directory_fd,
                name=record.artifact_id,
                directory_device=before.st_dev,
                exact_size=record.size_bytes,
                maximum_size=record.maximum_bytes,
            )
            if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), record.content_sha256):
                _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        after = os.fstat(directory_fd)
        if (
            _validate_directory_stat(after) != root_identity
            or set(os.listdir(directory_fd)) != expected_entries
        ):
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        return root_identity
    except TaskInputSnapshotError:
        raise
    except (OSError, UnicodeError):
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)


class TaskInputSnapshotLease(_OpaqueCapability):
    """Opaque owner of one validated, run-bound immutable directory FD."""

    __slots__ = ("_closed", "_directory_fd", "_identity", "_snapshot")

    def __init__(
        self,
        *,
        snapshot: ReviewedArtifactCatalogSnapshot,
        directory_fd: int,
    ) -> None:
        if type(directory_fd) is not int or directory_fd < 0:
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        self._closed = True
        self._directory_fd = -1
        self._snapshot = _copy_snapshot(snapshot)
        duplicate = -1
        try:
            duplicate = os.dup(directory_fd)
            os.set_inheritable(duplicate, False)
            identity = _validate_snapshot_directory(duplicate, self._snapshot)
        except TaskInputSnapshotError:
            if duplicate >= 0:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
            raise
        except OSError:
            if duplicate >= 0:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        self._directory_fd = duplicate
        self._identity = identity
        self._closed = False

    @property
    def snapshot(self) -> ReviewedArtifactCatalogSnapshot:
        self._require_live()
        return _copy_snapshot(self._snapshot)

    @property
    def catalog_sha256(self) -> str:
        return self.snapshot.catalog_sha256

    def descriptor_mapping(self) -> dict[str, object]:
        snapshot = self.snapshot
        return {
            "base_revision": snapshot.base_revision,
            "catalog_sha256": snapshot.catalog_sha256,
            "kind": REVIEWED_ARTIFACT_SNAPSHOT_KIND,
            "project_id": snapshot.project_id,
            "run_id": snapshot.run_id,
            "schema_version": REVIEWED_ARTIFACT_SNAPSHOT_SCHEMA_VERSION,
            "task_id": snapshot.task_id,
        }

    def duplicate_directory_fd(self) -> int:
        self._require_live()
        duplicate = -1
        try:
            duplicate = os.dup(self._directory_fd)
            os.set_inheritable(duplicate, False)
            if _validate_snapshot_directory(duplicate, self._snapshot) != self._identity:
                _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
            return duplicate
        except TaskInputSnapshotError:
            if duplicate >= 0:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
            raise
        except OSError:
            if duplicate >= 0:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)

    def _require_live(self) -> None:
        if self._closed:
            _fail(TaskInputSnapshotErrorCode.CLOSED)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor = self._directory_fd
        self._directory_fd = -1
        try:
            os.close(descriptor)
        except OSError:
            _fail(TaskInputSnapshotErrorCode.CLEANUP_FAILED)

    def __enter__(self) -> TaskInputSnapshotLease:
        self._require_live()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


__all__ = (
    "REVIEWED_ARTIFACT_MANIFEST_NAME",
    "REVIEWED_ARTIFACT_SNAPSHOT_KIND",
    "REVIEWED_ARTIFACT_SNAPSHOT_SCHEMA_VERSION",
    "TaskInputProgramPreflight",
    "TaskInputSnapshotError",
    "TaskInputSnapshotErrorCode",
    "TaskInputSnapshotLease",
    "TaskInputSnapshotProvider",
)
