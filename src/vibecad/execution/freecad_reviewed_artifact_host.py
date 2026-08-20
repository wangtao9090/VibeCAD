"""Private host authority for immutable reviewed CAD input snapshots.

The provider represented here is application-owned.  It acquires one exact
task/project/base/run snapshot and returns an opaque directory-FD lease.  The
lease validates the complete directory and duplicates its capability for a
Worker without ever accepting or exposing a filesystem path.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from vibecad import _file_compat
from vibecad._file_compat import WindowsPathCapability
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
_CLEANUP_NAME = re.compile(r"\.[A-Za-z0-9][A-Za-z0-9_.:-]{0,191}\Z")


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
        _file_compat.require_read_only(fd)
    except OSError:
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


def _read_regular_file_windows(
    directory: Path,
    *,
    name: str,
    exact_size: int,
    maximum_size: int,
) -> bytes:
    if (
        type(name) is not str
        or not name
        or "/" in name
        or "\\" in name
        or name in {".", ".."}
        or type(exact_size) is not int
        or type(maximum_size) is not int
        or exact_size < 0
        or exact_size > maximum_size
    ):
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
    path = directory / name
    descriptor = -1
    try:
        before = _file_compat.capture_windows_path(path, directory=False)
        descriptor = os.open(
            _file_compat.windows_extended_path(path),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        os.set_inheritable(descriptor, False)
        _file_compat.require_read_only(descriptor)
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.volume, before.file_id)
            or opened.st_size != exact_size
            or opened.st_size > maximum_size
        ):
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        chunks: list[bytes] = []
        remaining = maximum_size + 1
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = _file_compat.capture_windows_path(
            path,
            directory=False,
            generation_token=before.generation_token,
        )
        final = os.fstat(descriptor)
        if (
            len(payload) != exact_size
            or after != before
            or (final.st_dev, final.st_ino, final.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
        ):
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        return payload
    except TaskInputSnapshotError:
        raise
    except (OSError, UnicodeError):
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                _fail(TaskInputSnapshotErrorCode.CLEANUP_FAILED)


def _validate_snapshot_path(
    capability: WindowsPathCapability,
    snapshot: ReviewedArtifactCatalogSnapshot,
) -> WindowsPathCapability:
    try:
        directory = _file_compat.validate_windows_path(capability, directory=True)
        artifact_names = tuple(record.artifact_id for record in snapshot.records)
        if REVIEWED_ARTIFACT_MANIFEST_NAME in artifact_names:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        expected_entries = {REVIEWED_ARTIFACT_MANIFEST_NAME, *artifact_names}
        if {entry.name for entry in directory.iterdir()} != expected_entries:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        expected_manifest = _canonical_manifest(snapshot)
        if len(expected_manifest) > _MANIFEST_MAX_BYTES:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        manifest = _read_regular_file_windows(
            directory,
            name=REVIEWED_ARTIFACT_MANIFEST_NAME,
            exact_size=len(expected_manifest),
            maximum_size=_MANIFEST_MAX_BYTES,
        )
        if not hmac.compare_digest(manifest, expected_manifest):
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        for record in snapshot.records:
            payload = _read_regular_file_windows(
                directory,
                name=record.artifact_id,
                exact_size=record.size_bytes,
                maximum_size=record.maximum_bytes,
            )
            if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), record.content_sha256):
                _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        _file_compat.validate_windows_path(capability, directory=True)
        if {entry.name for entry in directory.iterdir()} != expected_entries:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        return capability
    except TaskInputSnapshotError:
        raise
    except (OSError, UnicodeError):
        _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)


class _WindowsOwnedSnapshotCleanup(_OpaqueCapability):
    __slots__ = ("_capability", "_closed", "_expected_names", "_parent")

    def __init__(
        self,
        *,
        capability: WindowsPathCapability,
        parent: WindowsPathCapability,
        expected_names: tuple[str, ...],
    ) -> None:
        try:
            path = _file_compat.validate_windows_path(capability, directory=True)
            parent_path = _file_compat.validate_windows_path(parent, directory=True)
            if path.parent != parent_path or {item.name for item in path.iterdir()} != set(
                expected_names
            ):
                _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        except TaskInputSnapshotError:
            raise
        except OSError:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        self._capability = capability
        self._parent = parent
        self._expected_names = expected_names
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            path = _file_compat.validate_windows_path(self._capability, directory=True)
            parent = _file_compat.validate_windows_path(self._parent, directory=True)
            if path.parent != parent or {item.name for item in path.iterdir()} != set(
                self._expected_names
            ):
                raise OSError
            for name in self._expected_names:
                item = path / name
                _file_compat.capture_windows_path(item, directory=False)
                os.unlink(_file_compat.windows_extended_path(item))
            os.rmdir(_file_compat.windows_extended_path(path))
            _file_compat.validate_windows_path(self._parent, directory=True)
        except OSError:
            _fail(TaskInputSnapshotErrorCode.CLEANUP_FAILED)


class _OwnedSnapshotCleanup(_OpaqueCapability):
    """Descriptor-relative owner for one private run snapshot directory."""

    __slots__ = ("_closed", "_expected_names", "_identity", "_name", "_parent_fd")

    def __init__(self, *, parent_fd: int, name: str, expected_names: tuple[str, ...]) -> None:
        if (
            type(parent_fd) is not int
            or parent_fd < 0
            or type(name) is not str
            or _CLEANUP_NAME.fullmatch(name) is None
            or name in {".", ".."}
            or type(expected_names) is not tuple
            or not expected_names
            or any(
                type(item) is not str or not item or "/" in item or item in {".", ".."}
                for item in expected_names
            )
            or len(set(expected_names)) != len(expected_names)
        ):
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        duplicate = -1
        directory = -1
        try:
            duplicate = os.dup(parent_fd)
            os.set_inheritable(duplicate, False)
            _require_read_only(duplicate)
            parent = os.fstat(duplicate)
            _validate_directory_stat(parent)
            directory = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=duplicate,
            )
            current = os.fstat(directory)
            if current.st_dev != parent.st_dev:
                _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
            identity = _validate_directory_stat(current)
            if set(os.listdir(directory)) != set(expected_names):
                _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        except TaskInputSnapshotError:
            if duplicate >= 0:
                with contextlib.suppress(OSError):
                    os.close(duplicate)
            raise
        except OSError:
            if duplicate >= 0:
                with contextlib.suppress(OSError):
                    os.close(duplicate)
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        finally:
            if directory >= 0:
                with contextlib.suppress(OSError):
                    os.close(directory)
        self._parent_fd = duplicate
        self._name = name
        self._expected_names = expected_names
        self._identity = identity
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        directory = -1
        failed = False
        try:
            directory = os.open(
                self._name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._parent_fd,
            )
            current = os.fstat(directory)
            names = tuple(os.listdir(directory))
            if _validate_directory_stat(current) != self._identity or set(names) != set(
                self._expected_names
            ):
                _fail(TaskInputSnapshotErrorCode.CLEANUP_FAILED)
            for name in names:
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (
                    not stat.S_ISREG(entry.st_mode)
                    or entry.st_dev != current.st_dev
                    or entry.st_uid != os.geteuid()
                    or entry.st_nlink != 1
                    or stat.S_IMODE(entry.st_mode) != _FILE_MODE
                ):
                    _fail(TaskInputSnapshotErrorCode.CLEANUP_FAILED)
                os.unlink(name, dir_fd=directory)
            os.fsync(directory)
            os.close(directory)
            directory = -1
            os.rmdir(self._name, dir_fd=self._parent_fd)
            os.fsync(self._parent_fd)
        except (OSError, TaskInputSnapshotError):
            failed = True
        finally:
            if directory >= 0:
                with contextlib.suppress(OSError):
                    os.close(directory)
            try:
                os.close(self._parent_fd)
            except OSError:
                failed = True
            self._parent_fd = -1
        if failed:
            _fail(TaskInputSnapshotErrorCode.CLEANUP_FAILED)


class TaskInputSnapshotLease(_OpaqueCapability):
    """Opaque owner of one validated, run-bound immutable directory FD."""

    __slots__ = (
        "_cleanup",
        "_closed",
        "_directory_fd",
        "_identity",
        "_snapshot",
        "_windows_capability",
    )

    def __init__(
        self,
        *,
        snapshot: ReviewedArtifactCatalogSnapshot,
        directory_fd: int | None = None,
        cleanup_parent_fd: int | None = None,
        cleanup_name: str | None = None,
        directory_capability: WindowsPathCapability | None = None,
        cleanup_parent_capability: WindowsPathCapability | None = None,
    ) -> None:
        windows_mode = directory_capability is not None
        if windows_mode and sys.platform != "win32":
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        if windows_mode == (directory_fd is not None):
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        if not windows_mode and (type(directory_fd) is not int or directory_fd < 0):
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        if not windows_mode and (cleanup_parent_fd is None) != (cleanup_name is None):
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        if cleanup_parent_capability is not None and not windows_mode:
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        if windows_mode and (cleanup_parent_capability is None) != (cleanup_name is None):
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        if windows_mode and cleanup_parent_fd is not None:
            _fail(TaskInputSnapshotErrorCode.INVALID_INPUT)
        self._closed = True
        self._directory_fd = -1
        self._cleanup = None
        self._windows_capability = None
        duplicate = -1
        try:
            self._snapshot = _copy_snapshot(snapshot)
            if windows_mode:
                assert directory_capability is not None
                if cleanup_parent_capability is not None:
                    assert cleanup_name is not None
                    self._cleanup = _WindowsOwnedSnapshotCleanup(
                        capability=directory_capability,
                        parent=cleanup_parent_capability,
                        expected_names=(
                            REVIEWED_ARTIFACT_MANIFEST_NAME,
                            *(record.artifact_id for record in self._snapshot.records),
                        ),
                    )
                identity = _validate_snapshot_path(directory_capability, self._snapshot)
                self._windows_capability = directory_capability
            elif cleanup_parent_fd is not None:
                assert cleanup_name is not None
                self._cleanup = _OwnedSnapshotCleanup(
                    parent_fd=cleanup_parent_fd,
                    name=cleanup_name,
                    expected_names=(
                        REVIEWED_ARTIFACT_MANIFEST_NAME,
                        *(record.artifact_id for record in self._snapshot.records),
                    ),
                )
            if not windows_mode:
                assert directory_fd is not None
                duplicate = os.dup(directory_fd)
                os.set_inheritable(duplicate, False)
                identity = _validate_snapshot_directory(duplicate, self._snapshot)
        except TaskInputSnapshotError:
            if duplicate >= 0:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
            if self._cleanup is not None:
                try:
                    self._cleanup.close()
                except BaseException:
                    pass
            raise
        except OSError:
            if duplicate >= 0:
                try:
                    os.close(duplicate)
                except OSError:
                    pass
            if self._cleanup is not None:
                try:
                    self._cleanup.close()
                except BaseException:
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
        if self._windows_capability is not None:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
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

    def windows_capability_mapping(self) -> dict[str, object]:
        """Return a revalidated wire capability on Windows; unavailable on POSIX."""

        self._require_live()
        capability = self._windows_capability
        if capability is None:
            _fail(TaskInputSnapshotErrorCode.INTEGRITY_FAILURE)
        _validate_snapshot_path(capability, self._snapshot)
        return capability.to_mapping()

    def _require_live(self) -> None:
        if self._closed:
            _fail(TaskInputSnapshotErrorCode.CLOSED)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor = self._directory_fd
        self._directory_fd = -1
        cleanup = self._cleanup
        self._cleanup = None
        failed = False
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
        if cleanup is not None:
            try:
                cleanup.close()
            except BaseException:
                failed = True
        if failed:
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
