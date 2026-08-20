"""Crash-safe append-only storage for organic derived-artifact generations."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import json
import os as _native_os
import re
import secrets
import stat
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from vibecad import _file_compat
from vibecad.interaction.storage import SafeRoot, StorageFailure, os
from vibecad.organic.contracts import (
    MAX_OUTPUT_ITEM_BYTES,
    MAX_SOURCE_BYTES,
    DerivedArtifactKind,
    DerivedArtifactSet,
    MeshJobRequest,
    MeshMediaType,
)
from vibecad.organic.persistence import (
    MAX_ORGANIC_MANIFEST_BYTES,
    OrganicGenerationManifest,
    OrganicPersistenceError,
    build_organic_manifest,
    decode_organic_manifest,
    encode_organic_manifest,
)

MAX_ORGANIC_JOBS = 128
MAX_ORGANIC_GENERATIONS_PER_JOB = 16
MAX_ORGANIC_TEMPORARIES = 16
MAX_ORGANIC_STORE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_RECOVERY_OVERHEAD_BYTES = MAX_ORGANIC_TEMPORARIES * MAX_ORGANIC_MANIFEST_BYTES

_LOCK_NAME = "organic-store.lock"
_MANIFEST_NAME = "manifest.json"
_MANIFEST_TEMP_NAME = "manifest.tmp"
_TOMBSTONE_DOMAIN = b"vibecad-organic-generation-tombstone-v1\0"
_COPY_CHUNK_BYTES = 64 * 1024

_JOB_ID = re.compile(r"^mesh_job_([0-9a-f]{32})$")
_SOURCE_ID = re.compile(r"^mesh_input_[0-9a-f]{32}$")
_ARTIFACT_ID = re.compile(r"^derived_artifact_[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_FINAL_NAME = re.compile(r"^mesh_generation_([0-9a-f]{32})_([0-9a-f]{16})$")
_STAGE_NAME = re.compile(r"^\.stage_([0-9a-f]{32})_([0-9a-f]{16})_([0-9a-f]{32})$")
_DELETE_NAME = re.compile(r"^\.delete_([0-9a-f]{32})_([0-9a-f]{16})$")
_TOMBSTONE_NAME = re.compile(r"^\.deleted_([0-9a-f]{32})_([0-9a-f]{16})\.json$")
_TOMBSTONE_TEMP_NAME = re.compile(r"^\.deleted_([0-9a-f]{32})_([0-9a-f]{16})_([0-9a-f]{32})\.tmp$")

_ARTIFACT_FILENAMES = {
    DerivedArtifactKind.CONTROL_CAGE: "control_cage.ply",
    DerivedArtifactKind.EDITABLE_BLEND: "editable.blend",
    DerivedArtifactKind.EVALUATED_GLB: "evaluated.glb",
    DerivedArtifactKind.PREVIEW_PNG: "preview.png",
    DerivedArtifactKind.VALIDATION_REPORT: "validation.json",
}
_PAYLOAD_NAMES = frozenset(_ARTIFACT_FILENAMES.values()) | {"source.ply", "source.stl"}
_GENERATION_NAMES = _PAYLOAD_NAMES | {_MANIFEST_NAME}
_STAGE_NAMES = _GENERATION_NAMES | {_MANIFEST_TEMP_NAME}


@dataclass(slots=True)
class _OrganicProcessLock:
    mutex: threading.Lock
    owner_thread: int | None = None
    active_fd: int | None = None


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[tuple[int, int], _OrganicProcessLock] = {}
_PROCESS_LOCKS_PID = os.getpid()


def _reset_process_locks_after_fork() -> None:
    global _PROCESS_LOCKS
    global _PROCESS_LOCKS_GUARD
    global _PROCESS_LOCKS_PID

    for entry in _PROCESS_LOCKS.values():
        if entry.active_fd is not None:
            _close(entry.active_fd)
    _PROCESS_LOCKS_GUARD = threading.Lock()
    _PROCESS_LOCKS = {}
    _PROCESS_LOCKS_PID = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_locks_after_fork)


class OrganicArtifactStoreErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RECOVERY_REQUIRED = "recovery_required"
    DURABILITY_UNCERTAIN = "durability_uncertain"


class OrganicArtifactStoreError(RuntimeError):
    """Bounded storage failure that never reflects filesystem data."""

    def __init__(self, code: OrganicArtifactStoreErrorCode) -> None:
        if type(code) is not OrganicArtifactStoreErrorCode:
            raise TypeError("code must be an exact OrganicArtifactStoreErrorCode")
        self.code = code
        super().__init__(code.value)


def _raise(code: OrganicArtifactStoreErrorCode) -> None:
    raise OrganicArtifactStoreError(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class OrganicPayloadSource:
    payload_id: str
    fd: int

    def __post_init__(self) -> None:
        if (
            type(self.payload_id) is not str
            or (
                _SOURCE_ID.fullmatch(self.payload_id) is None
                and _ARTIFACT_ID.fullmatch(self.payload_id) is None
            )
            or type(self.fd) is not int
            or self.fd < 0
        ):
            _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True, kw_only=True)
class OrganicRecoverySummary:
    published_stages: int
    removed_partial_stages: int
    completed_deletions: int


def _close(fd: int) -> bool:
    try:
        os.close(fd)
    except OSError:
        return False
    return True


def _job_suffix(mesh_job_id: object) -> str:
    if type(mesh_job_id) is not str:
        _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
    matched = _JOB_ID.fullmatch(mesh_job_id)
    if matched is None:
        _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
    return matched.group(1)


def _generation(value: object) -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
    return value


def _final_name(mesh_job_id: str, generation: int) -> str:
    return f"mesh_generation_{_job_suffix(mesh_job_id)}_{_generation(generation):016x}"


def _delete_name(mesh_job_id: str, generation: int) -> str:
    return f".delete_{_job_suffix(mesh_job_id)}_{_generation(generation):016x}"


def _tombstone_name(mesh_job_id: str, generation: int) -> str:
    return f".deleted_{_job_suffix(mesh_job_id)}_{_generation(generation):016x}.json"


def _source_filename(request: MeshJobRequest) -> str:
    if request.source.media_type is MeshMediaType.PLY:
        return "source.ply"
    if request.source.media_type is MeshMediaType.STL:
        return "source.stl"
    _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)


def _safe_regular(info: os.stat_result, root: SafeRoot, *, maximum: int) -> bool:
    return root.regular_file(info, maximum=maximum)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _stat_binding(info: os.stat_result) -> tuple[int, ...]:
    identity_epoch = (
        int(info.st_birthtime_ns)
        if sys.platform == "win32" and hasattr(info, "st_birthtime_ns")
        else info.st_ctime_ns
    )
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        identity_epoch,
    )


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)


def _list_names(directory_fd: int, *, maximum: int) -> tuple[str, ...]:
    try:
        names = list(os.listdir(directory_fd))
        if len(names) > maximum:
            _raise(OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED)
    except OrganicArtifactStoreError:
        raise
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
    return tuple(names)


def _fsync(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.DURABILITY_UNCERTAIN)


def _rename_directory_noreplace(parent_fd: int, source: str, destination: str) -> None:
    if sys.platform == "win32":
        os.rename(
            source,
            destination,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        return
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
            raise OSError(errno.ENOTSUP, "no-replace rename unavailable")
        operation.restype = ctypes.c_int
        ctypes.set_errno(0)
        if operation(*arguments) != 0:
            code = ctypes.get_errno() or errno.EIO
            if code in {errno.EEXIST, errno.ENOTEMPTY}:
                raise FileExistsError(code, "destination exists")
            raise OSError(code, "no-replace rename failed")
    except (AttributeError, UnicodeError) as exc:
        raise OSError(errno.ENOTSUP, "no-replace rename unavailable") from exc
    except OSError:
        raise


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    try:
        while view:
            written = os.write(fd, view)
            if written <= 0:
                _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            view = view[written:]
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)


def _write_exclusive(
    root: SafeRoot, parent_fd: int, name: str, raw: bytes, *, maximum: int
) -> None:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _raise(OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED)
    fd = -1
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        opened = os.fstat(fd)
        if not _safe_regular(opened, root, maximum=maximum):
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        _write_all(fd, raw)
        _fsync(fd)
        after = os.fstat(fd)
        if after.st_size != len(raw) or _identity(after) != _identity(opened):
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    except FileExistsError:
        _raise(OrganicArtifactStoreErrorCode.CONFLICT)
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
    finally:
        if fd >= 0 and not _close(fd):
            _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)


def _remove_regular_at(
    root: SafeRoot,
    parent_fd: int,
    name: str,
    *,
    maximum: int,
) -> None:
    info = _stat_at(parent_fd, name)
    if info is None:
        return
    if not _safe_regular(info, root, maximum=maximum):
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
    _fsync(parent_fd)


def _write_atomic_marker(
    root: SafeRoot,
    parent_fd: int,
    final_name: str,
    temporary_name: str,
    raw: bytes,
    *,
    maximum: int,
) -> None:
    published = False
    try:
        _write_exclusive(root, parent_fd, temporary_name, raw, maximum=maximum)
        try:
            _rename_directory_noreplace(parent_fd, temporary_name, final_name)
        except FileExistsError:
            _raise(OrganicArtifactStoreErrorCode.CONFLICT)
        except OSError:
            _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
        published = True
        _fsync(parent_fd)
    finally:
        if not published:
            with contextlib.suppress(OrganicArtifactStoreError):
                _remove_regular_at(
                    root,
                    parent_fd,
                    temporary_name,
                    maximum=maximum,
                )


def _copy_fd(
    root: SafeRoot,
    source_fd: int,
    destination_fd: int,
    name: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    source_capability = None
    try:
        before = _native_os.fstat(source_fd)
        if sys.platform == "win32":
            source_capability = _file_compat.capture_windows_external_fd(source_fd)
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
    if (
        not stat.S_ISREG(before.st_mode)
        or (
            sys.platform != "win32"
            and (
                before.st_uid != root.uid
                or stat.S_IMODE(before.st_mode) != 0o600
            )
        )
        or before.st_nlink != 1
        or before.st_size != expected_size
        or expected_size <= 0
        or expected_size > MAX_OUTPUT_ITEM_BYTES
        or _DIGEST.fullmatch(expected_sha256) is None
    ):
        _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
    target_fd = -1
    digest = hashlib.sha256()
    offset = 0
    try:
        target_fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=destination_fd,
        )
        target_info = os.fstat(target_fd)
        if not _safe_regular(target_info, root, maximum=expected_size):
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        while offset < expected_size:
            chunk = os.pread(source_fd, min(_COPY_CHUNK_BYTES, expected_size - offset), offset)
            if not chunk:
                _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
            _write_all(target_fd, chunk)
            digest.update(chunk)
            offset += len(chunk)
        _fsync(target_fd)
        after = _native_os.fstat(source_fd)
        if sys.platform == "win32":
            assert source_capability is not None
            current_source = _file_compat.capture_windows_external_fd(
                source_fd,
                generation_token=source_capability.generation_token,
            )
            if current_source != source_capability:
                _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
        if _stat_binding(before) != _stat_binding(after) or digest.hexdigest() != expected_sha256:
            _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
    finally:
        if target_fd >= 0 and not _close(target_fd):
            _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)


def _read_file(root: SafeRoot, parent_fd: int, name: str, *, maximum: int) -> bytes:
    try:
        raw, _ = root.read_file_at(parent_fd, name, maximum=maximum)
        return raw
    except StorageFailure:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)


def _payload_expectations(manifest: OrganicGenerationManifest) -> dict[str, tuple[str, int, str]]:
    result: dict[str, tuple[str, int, str]] = {
        _source_filename(manifest.request): (
            manifest.request.source.source_id,
            manifest.request.source.byte_count,
            manifest.request.source.sha256,
        )
    }
    for artifact in manifest.result.artifacts:
        result[_ARTIFACT_FILENAMES[artifact.kind]] = (
            artifact.artifact_id,
            artifact.byte_count,
            artifact.sha256,
        )
    return result


def _verify_generation(
    root: SafeRoot,
    parent_fd: int,
) -> tuple[OrganicGenerationManifest, bytes]:
    names = frozenset(_list_names(parent_fd, maximum=len(_GENERATION_NAMES)))
    if _MANIFEST_NAME not in names or not names <= _GENERATION_NAMES:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    raw = _read_file(root, parent_fd, _MANIFEST_NAME, maximum=MAX_ORGANIC_MANIFEST_BYTES)
    try:
        manifest = decode_organic_manifest(raw)
    except OrganicPersistenceError:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    expectations = _payload_expectations(manifest)
    if names != frozenset(expectations) | {_MANIFEST_NAME}:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    for name, (_, size, digest) in expectations.items():
        try:
            observed_digest, observed_size, _ = root.hash_open_file(
                parent_fd,
                name,
                maximum=max(MAX_SOURCE_BYTES, MAX_OUTPUT_ITEM_BYTES),
            )
        except StorageFailure:
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        if observed_size != size or not secrets.compare_digest(observed_digest, digest):
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    return manifest, raw


def _verify_deleting_generation(
    root: SafeRoot,
    parent_fd: int,
    *,
    mesh_job_id: str,
    generation: int,
    manifest_sha256: str,
) -> None:
    names = frozenset(_list_names(parent_fd, maximum=len(_GENERATION_NAMES)))
    if not names:
        return
    if _MANIFEST_NAME not in names or not names <= _GENERATION_NAMES:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    raw = _read_file(root, parent_fd, _MANIFEST_NAME, maximum=MAX_ORGANIC_MANIFEST_BYTES)
    try:
        manifest = decode_organic_manifest(raw)
    except OrganicPersistenceError:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    if (
        manifest.request.mesh_job_id != mesh_job_id
        or manifest.request.generation != generation
        or manifest.manifest_sha256 != manifest_sha256
    ):
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    expectations = _payload_expectations(manifest)
    for name in names - {_MANIFEST_NAME}:
        expected = expectations.get(name)
        if expected is None:
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        _, size, digest = expected
        try:
            observed_digest, observed_size, _ = root.hash_open_file(
                parent_fd,
                name,
                maximum=max(MAX_SOURCE_BYTES, MAX_OUTPUT_ITEM_BYTES),
            )
        except StorageFailure:
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        if observed_size != size or not secrets.compare_digest(observed_digest, digest):
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)


def _open_directory(
    root: SafeRoot,
    root_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, os.stat_result]:
    expected = _stat_at(root_fd, name)
    if expected is None:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    required_identity = _identity(expected) if expected_identity is None else expected_identity
    if _identity(expected) != required_identity:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
    try:
        return root.open_directory_at(root_fd, name, expected_identity=required_identity)
    except StorageFailure:
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)


def _verify_directory_binding(
    root: SafeRoot,
    parent_fd: int,
    name: str,
    directory_fd: int,
) -> None:
    try:
        expected = os.fstat(directory_fd)
        root.verify_directory_entry(parent_fd, name, expected=expected)
    except (OSError, StorageFailure):
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)


def _remove_directory(
    root: SafeRoot,
    root_fd: int,
    name: str,
    *,
    allowed: frozenset[str],
    expected_identity: tuple[int, int] | None = None,
) -> None:
    directory_fd, _ = _open_directory(
        root,
        root_fd,
        name,
        expected_identity=expected_identity,
    )
    try:
        names = _list_names(directory_fd, maximum=len(allowed))
        if not set(names) <= allowed:
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        ordered = sorted(names, key=lambda entry: entry == _MANIFEST_NAME)
        for entry in ordered:
            info = _stat_at(directory_fd, entry)
            maximum = (
                MAX_ORGANIC_MANIFEST_BYTES
                if entry in {_MANIFEST_NAME, _MANIFEST_TEMP_NAME}
                else MAX_OUTPUT_ITEM_BYTES
            )
            if info is None or not _safe_regular(info, root, maximum=maximum):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            try:
                os.unlink(entry, dir_fd=directory_fd)
            except OSError:
                _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            _fsync(directory_fd)
        _verify_directory_binding(root, root_fd, name, directory_fd)
    finally:
        if not _close(directory_fd):
            _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
    try:
        os.rmdir(name, dir_fd=root_fd)
        _fsync(root_fd)
    except OSError:
        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)


def _tombstone_bytes(manifest: OrganicGenerationManifest) -> bytes:
    body = {
        "generation": manifest.request.generation,
        "manifest_sha256": manifest.manifest_sha256,
        "mesh_job_id": manifest.request.mesh_job_id,
        "schema_version": 1,
    }
    body_raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    envelope = {
        "body": body,
        "body_sha256": hashlib.sha256(_TOMBSTONE_DOMAIN + body_raw).hexdigest(),
        "schema_version": 1,
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _decode_tombstone(raw: bytes) -> tuple[str, int, str]:
    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if type(key) is not str or key in result:
                raise ValueError
            result[key] = item
        return result

    try:
        if type(raw) is not bytes or not raw or len(raw) > MAX_ORGANIC_MANIFEST_BYTES:
            raise ValueError
        value = json.loads(
            raw,
            object_pairs_hook=strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if type(value) is not dict or set(value) != {"body", "body_sha256", "schema_version"}:
            raise ValueError
        body = value["body"]
        if (
            value["schema_version"] != 1
            or type(body) is not dict
            or set(body) != {"generation", "manifest_sha256", "mesh_job_id", "schema_version"}
            or body["schema_version"] != 1
            or type(body["mesh_job_id"]) is not str
            or _JOB_ID.fullmatch(body["mesh_job_id"]) is None
            or type(body["manifest_sha256"]) is not str
            or _DIGEST.fullmatch(body["manifest_sha256"]) is None
            or type(body["generation"]) is not int
            or not 0 < body["generation"] <= 2**53 - 1
        ):
            raise ValueError
        body_raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        expected = hashlib.sha256(_TOMBSTONE_DOMAIN + body_raw).hexdigest()
        if value["body_sha256"] != expected:
            raise ValueError
        canonical = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if canonical != raw:
            raise ValueError
        return body["mesh_job_id"], body["generation"], body["manifest_sha256"]
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        RecursionError,
        UnicodeError,
    ):
        _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)


def _directory_usage(
    root: SafeRoot,
    root_fd: int,
    name: str,
    *,
    allowed: frozenset[str],
) -> int:
    directory_fd, _ = _open_directory(root, root_fd, name)
    total = 0
    try:
        entries = _list_names(directory_fd, maximum=len(allowed))
        if not set(entries) <= allowed:
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        for entry in entries:
            maximum = (
                MAX_ORGANIC_MANIFEST_BYTES
                if entry in {_MANIFEST_NAME, _MANIFEST_TEMP_NAME}
                else MAX_OUTPUT_ITEM_BYTES
            )
            info = _stat_at(directory_fd, entry)
            if info is None or not _safe_regular(info, root, maximum=maximum):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            total += info.st_size
        _verify_directory_binding(root, root_fd, name, directory_fd)
    finally:
        if not _close(directory_fd):
            _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
    return total


def _store_snapshot(
    root: SafeRoot,
    root_fd: int,
    *,
    recovery_overhead: bool = False,
) -> tuple[tuple[str, ...], int]:
    maximum_entries = (
        MAX_ORGANIC_JOBS * MAX_ORGANIC_GENERATIONS_PER_JOB * 2 + MAX_ORGANIC_TEMPORARIES * 2 + 1
    )
    names = _list_names(root_fd, maximum=maximum_entries)
    total = 0
    for name in names:
        if name == _LOCK_NAME:
            info = _stat_at(root_fd, name)
            if info is None or not _safe_regular(info, root, maximum=1):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            total += info.st_size
        elif _TOMBSTONE_NAME.fullmatch(name) or _TOMBSTONE_TEMP_NAME.fullmatch(name):
            info = _stat_at(root_fd, name)
            if info is None or not _safe_regular(info, root, maximum=MAX_ORGANIC_MANIFEST_BYTES):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            total += info.st_size
        elif _FINAL_NAME.fullmatch(name) or _DELETE_NAME.fullmatch(name):
            total += _directory_usage(root, root_fd, name, allowed=_GENERATION_NAMES)
        elif _STAGE_NAME.fullmatch(name):
            total += _directory_usage(root, root_fd, name, allowed=_STAGE_NAMES)
        else:
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        maximum_bytes = MAX_ORGANIC_STORE_BYTES + (
            _MAX_RECOVERY_OVERHEAD_BYTES if recovery_overhead else 0
        )
        if total > maximum_bytes:
            _raise(OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED)
    return names, total


def _validate_store_counts(names: tuple[str, ...]) -> None:
    temporary_count = sum(
        1 for name in names if _STAGE_NAME.fullmatch(name) or _TOMBSTONE_TEMP_NAME.fullmatch(name)
    )
    delete_count = sum(1 for name in names if _DELETE_NAME.fullmatch(name))
    identities: set[tuple[str, str]] = set()
    jobs: set[str] = set()
    for name in names:
        matched = (
            _FINAL_NAME.fullmatch(name)
            or _TOMBSTONE_NAME.fullmatch(name)
            or _STAGE_NAME.fullmatch(name)
            or _DELETE_NAME.fullmatch(name)
        )
        if matched is None:
            continue
        identity = matched.group(1), matched.group(2)
        identities.add(identity)
        jobs.add(identity[0])
    generations_by_job: dict[str, int] = {}
    for job, _generation_hex in identities:
        generations_by_job[job] = generations_by_job.get(job, 0) + 1
    if (
        temporary_count > MAX_ORGANIC_TEMPORARIES
        or delete_count > MAX_ORGANIC_TEMPORARIES
        or len(jobs) > MAX_ORGANIC_JOBS
        or any(count > MAX_ORGANIC_GENERATIONS_PER_JOB for count in generations_by_job.values())
    ):
        _raise(OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED)


class OrganicArtifactStore:
    """Immutable local generation store with explicit recovery and exact deletion."""

    __slots__ = ("_entry", "_pid", "_root")

    def __init__(self, root: SafeRoot) -> None:
        if type(root) is not SafeRoot:
            raise TypeError("root must be an exact SafeRoot")
        if os.getpid() != _PROCESS_LOCKS_PID:
            _reset_process_locks_after_fork()
        self._root = root
        with _PROCESS_LOCKS_GUARD:
            self._entry = _PROCESS_LOCKS.setdefault(
                root.identity,
                _OrganicProcessLock(threading.Lock()),
            )
        self._pid = os.getpid()

    @contextmanager
    def _hold(self) -> Iterator[int]:
        if os.getpid() != self._pid:
            _raise(OrganicArtifactStoreErrorCode.LEASE_UNAVAILABLE)
        thread_id = threading.get_ident()
        with _PROCESS_LOCKS_GUARD:
            if self._entry.owner_thread == thread_id:
                _raise(OrganicArtifactStoreErrorCode.LEASE_UNAVAILABLE)
        self._entry.mutex.acquire()
        root_fd = -1
        lock_fd = -1
        try:
            with _PROCESS_LOCKS_GUARD:
                self._entry.owner_thread = thread_id
            root_fd = self._root.open()
            lock_fd = os.open(
                _LOCK_NAME,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=root_fd,
            )
            info = os.fstat(lock_fd)
            if not _safe_regular(info, self._root, maximum=1):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            with _PROCESS_LOCKS_GUARD:
                self._entry.active_fd = lock_fd
            _file_compat.flock(lock_fd, _file_compat.LOCK_EX)
            current = _stat_at(root_fd, _LOCK_NAME)
            if current is None or _stat_binding(current) != _stat_binding(info):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            yield root_fd
        except OrganicArtifactStoreError:
            raise
        except (OSError, StorageFailure):
            _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
        finally:
            if lock_fd >= 0:
                with contextlib.suppress(OSError):
                    _file_compat.flock(lock_fd, _file_compat.LOCK_UN)
                _close(lock_fd)
            if root_fd >= 0:
                _close(root_fd)
            with _PROCESS_LOCKS_GUARD:
                self._entry.active_fd = None
                self._entry.owner_thread = None
            self._entry.mutex.release()

    def _check_budget(self, root_fd: int, mesh_job_id: str, *, additional_bytes: int) -> None:
        names, used_bytes = _store_snapshot(self._root, root_fd)
        finals = tuple(
            matched for name in names if (matched := _FINAL_NAME.fullmatch(name)) is not None
        )
        stages = tuple(
            matched for name in names if (matched := _STAGE_NAME.fullmatch(name)) is not None
        )
        tombstones = tuple(
            matched for name in names if (matched := _TOMBSTONE_NAME.fullmatch(name)) is not None
        )
        tombstone_temporaries = tuple(
            name for name in names if _TOMBSTONE_TEMP_NAME.fullmatch(name)
        )
        jobs = {matched.group(1) for matched in finals}
        jobs.update(matched.group(1) for matched in tombstones)
        suffix = _job_suffix(mesh_job_id)
        generations = sum(1 for matched in finals + tombstones if matched.group(1) == suffix)
        if (
            (suffix not in jobs and len(jobs) >= MAX_ORGANIC_JOBS)
            or generations >= MAX_ORGANIC_GENERATIONS_PER_JOB
            or len(stages) + len(tombstone_temporaries) >= MAX_ORGANIC_TEMPORARIES
            or type(additional_bytes) is not int
            or additional_bytes <= 0
            or additional_bytes > MAX_ORGANIC_STORE_BYTES
            or used_bytes > MAX_ORGANIC_STORE_BYTES - additional_bytes
        ):
            _raise(OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED)

    def _load_from_root(
        self,
        root_fd: int,
        mesh_job_id: str,
        generation: int,
    ) -> tuple[OrganicGenerationManifest, bytes, tuple[int, int]]:
        name = _final_name(mesh_job_id, generation)
        if _stat_at(root_fd, name) is None:
            _raise(OrganicArtifactStoreErrorCode.NOT_FOUND)
        directory_fd, opened = _open_directory(self._root, root_fd, name)
        try:
            manifest, raw = _verify_generation(self._root, directory_fd)
            _verify_directory_binding(self._root, root_fd, name, directory_fd)
        finally:
            if not _close(directory_fd):
                _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
        if manifest.request.mesh_job_id != mesh_job_id or manifest.request.generation != generation:
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        return manifest, raw, _identity(opened)

    def _recover_locked(self, root_fd: int) -> OrganicRecoverySummary:
        published = 0
        removed = 0
        deleted = 0
        names, _ = _store_snapshot(self._root, root_fd, recovery_overhead=True)
        _validate_store_counts(names)

        for temporary_name in sorted(
            name for name in names if _TOMBSTONE_TEMP_NAME.fullmatch(name)
        ):
            _remove_regular_at(
                self._root,
                root_fd,
                temporary_name,
                maximum=MAX_ORGANIC_MANIFEST_BYTES,
            )
        names, _ = _store_snapshot(self._root, root_fd, recovery_overhead=True)

        for tombstone_name in sorted(name for name in names if _TOMBSTONE_NAME.fullmatch(name)):
            tombstone_match = _TOMBSTONE_NAME.fullmatch(tombstone_name)
            assert tombstone_match is not None
            raw = _read_file(
                self._root, root_fd, tombstone_name, maximum=MAX_ORGANIC_MANIFEST_BYTES
            )
            mesh_job_id, generation, manifest_sha256 = _decode_tombstone(raw)
            if _job_suffix(mesh_job_id) != tombstone_match.group(1) or generation != int(
                tombstone_match.group(2), 16
            ):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            final_name = _final_name(mesh_job_id, generation)
            delete_name = _delete_name(mesh_job_id, generation)
            final_exists = _stat_at(root_fd, final_name) is not None
            delete_exists = _stat_at(root_fd, delete_name) is not None
            if final_exists and delete_exists:
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            if final_exists:
                final_manifest, _, final_identity = self._load_from_root(
                    root_fd, mesh_job_id, generation
                )
                if final_manifest.manifest_sha256 != manifest_sha256:
                    _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
                try:
                    _rename_directory_noreplace(root_fd, final_name, delete_name)
                except OSError:
                    _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
                _fsync(root_fd)
                delete_exists = True
            else:
                final_identity = None
            if delete_exists:
                delete_fd, delete_info = _open_directory(
                    self._root,
                    root_fd,
                    delete_name,
                    expected_identity=final_identity,
                )
                try:
                    _verify_deleting_generation(
                        self._root,
                        delete_fd,
                        mesh_job_id=mesh_job_id,
                        generation=generation,
                        manifest_sha256=manifest_sha256,
                    )
                    _verify_directory_binding(
                        self._root,
                        root_fd,
                        delete_name,
                        delete_fd,
                    )
                finally:
                    if not _close(delete_fd):
                        _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
                _remove_directory(
                    self._root,
                    root_fd,
                    delete_name,
                    allowed=_GENERATION_NAMES,
                    expected_identity=_identity(delete_info),
                )
                deleted += 1

        names, _ = _store_snapshot(self._root, root_fd)
        if any(_DELETE_NAME.fullmatch(name) for name in names):
            _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
        for stage_name in sorted(name for name in names if _STAGE_NAME.fullmatch(name)):
            matched = _STAGE_NAME.fullmatch(stage_name)
            assert matched is not None
            directory_fd, stage_info = _open_directory(self._root, root_fd, stage_name)
            stage_identity = _identity(stage_info)
            try:
                entries = frozenset(_list_names(directory_fd, maximum=len(_STAGE_NAMES)))
                if not entries <= _STAGE_NAMES:
                    _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
                if _MANIFEST_TEMP_NAME in entries and _MANIFEST_NAME in entries:
                    _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
                if _MANIFEST_NAME not in entries:
                    pass
                else:
                    manifest, raw = _verify_generation(self._root, directory_fd)
                _verify_directory_binding(
                    self._root,
                    root_fd,
                    stage_name,
                    directory_fd,
                )
            finally:
                if not _close(directory_fd):
                    _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            if _MANIFEST_NAME not in entries:
                _remove_directory(
                    self._root,
                    root_fd,
                    stage_name,
                    allowed=_STAGE_NAMES,
                    expected_identity=stage_identity,
                )
                removed += 1
                continue
            if _job_suffix(manifest.request.mesh_job_id) != matched.group(
                1
            ) or manifest.request.generation != int(matched.group(2), 16):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            final_name = _final_name(manifest.request.mesh_job_id, manifest.request.generation)
            tombstone_name = _tombstone_name(
                manifest.request.mesh_job_id, manifest.request.generation
            )
            if _stat_at(root_fd, tombstone_name) is not None:
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            if _stat_at(root_fd, final_name) is not None:
                _, final_raw, _ = self._load_from_root(
                    root_fd,
                    manifest.request.mesh_job_id,
                    manifest.request.generation,
                )
                if final_raw != raw:
                    _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
                _remove_directory(
                    self._root,
                    root_fd,
                    stage_name,
                    allowed=_GENERATION_NAMES,
                    expected_identity=stage_identity,
                )
                removed += 1
                continue
            try:
                _rename_directory_noreplace(root_fd, stage_name, final_name)
            except OSError:
                _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            _fsync(root_fd)
            final_fd, _ = _open_directory(
                self._root,
                root_fd,
                final_name,
                expected_identity=stage_identity,
            )
            try:
                recovered_manifest, recovered_raw = _verify_generation(
                    self._root,
                    final_fd,
                )
                _verify_directory_binding(
                    self._root,
                    root_fd,
                    final_name,
                    final_fd,
                )
            finally:
                if not _close(final_fd):
                    _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            if recovered_manifest != manifest or recovered_raw != raw:
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            published += 1
        final_names, _ = _store_snapshot(self._root, root_fd)
        _validate_store_counts(final_names)
        if any(
            _STAGE_NAME.fullmatch(name)
            or _DELETE_NAME.fullmatch(name)
            or _TOMBSTONE_TEMP_NAME.fullmatch(name)
            for name in final_names
        ):
            _raise(OrganicArtifactStoreErrorCode.RECOVERY_REQUIRED)
        return OrganicRecoverySummary(
            published_stages=published,
            removed_partial_stages=removed,
            completed_deletions=deleted,
        )

    def recover_pending(self) -> OrganicRecoverySummary:
        with self._hold() as root_fd:
            return self._recover_locked(root_fd)

    def publish(
        self,
        request: MeshJobRequest,
        source: OrganicPayloadSource,
        result: DerivedArtifactSet,
        artifacts: tuple[OrganicPayloadSource, ...],
    ) -> OrganicGenerationManifest:
        if (
            type(request) is not MeshJobRequest
            or type(source) is not OrganicPayloadSource
            or type(result) is not DerivedArtifactSet
            or type(artifacts) is not tuple
            or any(type(item) is not OrganicPayloadSource for item in artifacts)
            or source.payload_id != request.source.source_id
        ):
            _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
        expected_artifacts = {item.artifact_id: item for item in result.artifacts}
        supplied_artifacts = {item.payload_id: item for item in artifacts}
        if len(supplied_artifacts) != len(artifacts) or set(supplied_artifacts) != set(
            expected_artifacts
        ):
            _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
        try:
            raw = encode_organic_manifest(request, result)
            manifest = build_organic_manifest(request, result)
        except OrganicPersistenceError:
            _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
        with self._hold() as root_fd:
            self._recover_locked(root_fd)
            final_name = _final_name(request.mesh_job_id, request.generation)
            tombstone_name = _tombstone_name(request.mesh_job_id, request.generation)
            if _stat_at(root_fd, tombstone_name) is not None:
                _raise(OrganicArtifactStoreErrorCode.CONFLICT)
            if _stat_at(root_fd, final_name) is not None:
                existing, existing_raw, _ = self._load_from_root(
                    root_fd, request.mesh_job_id, request.generation
                )
                if existing_raw == raw:
                    return existing
                _raise(OrganicArtifactStoreErrorCode.CONFLICT)
            self._check_budget(
                root_fd,
                request.mesh_job_id,
                additional_bytes=(
                    request.source.byte_count
                    + sum(item.byte_count for item in result.artifacts)
                    + len(raw)
                ),
            )
            stage_name = (
                f".stage_{_job_suffix(request.mesh_job_id)}_{request.generation:016x}_"
                f"{secrets.token_hex(16)}"
            )
            try:
                os.mkdir(stage_name, 0o700, dir_fd=root_fd)
                _fsync(root_fd)
            except OSError:
                _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            stage_fd, _ = _open_directory(self._root, root_fd, stage_name)
            published = False
            try:
                _copy_fd(
                    self._root,
                    source.fd,
                    stage_fd,
                    _source_filename(request),
                    expected_size=request.source.byte_count,
                    expected_sha256=request.source.sha256,
                )
                for artifact in result.artifacts:
                    payload = supplied_artifacts[artifact.artifact_id]
                    _copy_fd(
                        self._root,
                        payload.fd,
                        stage_fd,
                        _ARTIFACT_FILENAMES[artifact.kind],
                        expected_size=artifact.byte_count,
                        expected_sha256=artifact.sha256,
                    )
                _write_atomic_marker(
                    self._root,
                    stage_fd,
                    _MANIFEST_NAME,
                    _MANIFEST_TEMP_NAME,
                    raw,
                    maximum=MAX_ORGANIC_MANIFEST_BYTES,
                )
                _fsync(stage_fd)
                verified, verified_raw = _verify_generation(self._root, stage_fd)
                if verified != manifest or verified_raw != raw:
                    _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
                _verify_directory_binding(
                    self._root,
                    root_fd,
                    stage_name,
                    stage_fd,
                )
                if not _close(stage_fd):
                    stage_fd = -1
                    _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
                stage_fd = -1
                try:
                    _rename_directory_noreplace(root_fd, stage_name, final_name)
                except FileExistsError:
                    _raise(OrganicArtifactStoreErrorCode.CONFLICT)
                except OSError:
                    _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
                published = True
                _fsync(root_fd)
                loaded, loaded_raw, _ = self._load_from_root(
                    root_fd,
                    request.mesh_job_id,
                    request.generation,
                )
                if loaded != manifest or loaded_raw != raw:
                    _raise(OrganicArtifactStoreErrorCode.DURABILITY_UNCERTAIN)
                return loaded
            finally:
                if stage_fd >= 0:
                    _close(stage_fd)
                if not published and _stat_at(root_fd, stage_name) is not None:
                    with contextlib.suppress(OrganicArtifactStoreError):
                        _remove_directory(
                            self._root,
                            root_fd,
                            stage_name,
                            allowed=_STAGE_NAMES,
                        )

    def load_exact(self, mesh_job_id: str, generation: int) -> OrganicGenerationManifest:
        with self._hold() as root_fd:
            self._recover_locked(root_fd)
            manifest, _, _ = self._load_from_root(root_fd, mesh_job_id, generation)
            return manifest

    def read_payload_exact(
        self,
        mesh_job_id: str,
        generation: int,
        payload_id: str,
        sha256: str,
    ) -> bytes:
        if (
            type(payload_id) is not str
            or type(sha256) is not str
            or _DIGEST.fullmatch(sha256) is None
        ):
            _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
        with self._hold() as root_fd:
            self._recover_locked(root_fd)
            manifest, _, _ = self._load_from_root(root_fd, mesh_job_id, generation)
            expectations = _payload_expectations(manifest)
            matched = [
                (name, size)
                for name, (expected_id, size, expected_digest) in expectations.items()
                if expected_id == payload_id and expected_digest == sha256
            ]
            if len(matched) != 1:
                _raise(OrganicArtifactStoreErrorCode.NOT_FOUND)
            directory_fd, _ = _open_directory(
                self._root, root_fd, _final_name(mesh_job_id, generation)
            )
            try:
                raw = _read_file(self._root, directory_fd, matched[0][0], maximum=matched[0][1])
                _verify_directory_binding(
                    self._root,
                    root_fd,
                    _final_name(mesh_job_id, generation),
                    directory_fd,
                )
            finally:
                if not _close(directory_fd):
                    _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            if len(raw) != matched[0][1] or hashlib.sha256(raw).hexdigest() != sha256:
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)
            return raw

    def delete_exact(
        self,
        mesh_job_id: str,
        generation: int,
        manifest_sha256: str,
    ) -> None:
        if type(manifest_sha256) is not str or _DIGEST.fullmatch(manifest_sha256) is None:
            _raise(OrganicArtifactStoreErrorCode.INVALID_INPUT)
        with self._hold() as root_fd:
            self._recover_locked(root_fd)
            tombstone_name = _tombstone_name(mesh_job_id, generation)
            existing_tombstone = _stat_at(root_fd, tombstone_name)
            if existing_tombstone is not None:
                raw = _read_file(
                    self._root,
                    root_fd,
                    tombstone_name,
                    maximum=MAX_ORGANIC_MANIFEST_BYTES,
                )
                tombstone_job, tombstone_generation, tombstone_digest = _decode_tombstone(raw)
                if (
                    tombstone_job == mesh_job_id
                    and tombstone_generation == generation
                    and tombstone_digest == manifest_sha256
                ):
                    return
                _raise(OrganicArtifactStoreErrorCode.CONFLICT)
            manifest, _, final_identity = self._load_from_root(root_fd, mesh_job_id, generation)
            if manifest.manifest_sha256 != manifest_sha256:
                _raise(OrganicArtifactStoreErrorCode.CONFLICT)
            tombstone_temporary = (
                f".deleted_{_job_suffix(mesh_job_id)}_{generation:016x}_{secrets.token_hex(16)}.tmp"
            )
            _write_atomic_marker(
                self._root,
                root_fd,
                tombstone_name,
                tombstone_temporary,
                _tombstone_bytes(manifest),
                maximum=MAX_ORGANIC_MANIFEST_BYTES,
            )
            final_name = _final_name(mesh_job_id, generation)
            delete_name = _delete_name(mesh_job_id, generation)
            try:
                _rename_directory_noreplace(root_fd, final_name, delete_name)
            except OSError:
                _raise(OrganicArtifactStoreErrorCode.DURABILITY_UNCERTAIN)
            _fsync(root_fd)
            delete_fd, delete_info = _open_directory(
                self._root,
                root_fd,
                delete_name,
                expected_identity=final_identity,
            )
            try:
                _verify_deleting_generation(
                    self._root,
                    delete_fd,
                    mesh_job_id=mesh_job_id,
                    generation=generation,
                    manifest_sha256=manifest_sha256,
                )
                _verify_directory_binding(
                    self._root,
                    root_fd,
                    delete_name,
                    delete_fd,
                )
            finally:
                if not _close(delete_fd):
                    _raise(OrganicArtifactStoreErrorCode.STORE_FAILURE)
            _remove_directory(
                self._root,
                root_fd,
                delete_name,
                allowed=_GENERATION_NAMES,
                expected_identity=_identity(delete_info),
            )
            names, _ = _store_snapshot(self._root, root_fd)
            _validate_store_counts(names)
            if (
                _stat_at(root_fd, final_name) is not None
                or _stat_at(root_fd, delete_name) is not None
            ):
                _raise(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE)


__all__ = (
    "MAX_ORGANIC_GENERATIONS_PER_JOB",
    "MAX_ORGANIC_JOBS",
    "MAX_ORGANIC_STORE_BYTES",
    "MAX_ORGANIC_TEMPORARIES",
    "OrganicArtifactStore",
    "OrganicArtifactStoreError",
    "OrganicArtifactStoreErrorCode",
    "OrganicPayloadSource",
    "OrganicRecoverySummary",
)
