"""Atomic, fail-closed local persistence for immutable TaskRun values."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.lease import LeaseError, ResourceLease, ResourceLeaseManager
from vibecad.workflow.state import TaskRun

__all__ = (
    "StoredTaskRun",
    "TaskRunStore",
    "TaskStoreError",
    "TaskStoreErrorCode",
    "TaskStoreRootTrust",
)

_KEY_DOMAIN = b"vibecad-task-store-key-v1\0"
_CHECKSUM_DOMAIN = b"vibecad-stored-task-run-v1\0"
_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 8192
_MAX_JSON_STRING_BYTES = 65536


class TaskStoreRootTrust(StrEnum):
    TRUSTED_LOCAL = "trusted_local"


class TaskStoreErrorCode(StrEnum):
    INVALID_ID = "invalid_id"
    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    CONFLICT = "conflict"
    CORRUPT_RECORD = "corrupt_record"
    RECORD_TOO_LARGE = "record_too_large"
    UNSAFE_STORE = "unsafe_store"
    LOCK_UNAVAILABLE = "lock_unavailable"
    IO_ERROR = "io_error"
    DURABILITY_UNCERTAIN = "durability_uncertain"


def _error_message(code: TaskStoreErrorCode) -> str:
    match code:
        case TaskStoreErrorCode.INVALID_ID:
            return "The task identifier is invalid."
        case TaskStoreErrorCode.NOT_FOUND:
            return "The task record was not found."
        case TaskStoreErrorCode.ALREADY_EXISTS:
            return "The task record already exists."
        case TaskStoreErrorCode.CONFLICT:
            return "The task record generation conflicts."
        case TaskStoreErrorCode.CORRUPT_RECORD:
            return "The task record is corrupt."
        case TaskStoreErrorCode.RECORD_TOO_LARGE:
            return "The task record exceeds the size limit."
        case TaskStoreErrorCode.UNSAFE_STORE:
            return "The task store is unsafe."
        case TaskStoreErrorCode.LOCK_UNAVAILABLE:
            return "The task record lock is unavailable."
        case TaskStoreErrorCode.IO_ERROR:
            return "The task store operation failed."
        case TaskStoreErrorCode.DURABILITY_UNCERTAIN:
            return "The task record committed but durability is uncertain."


class TaskStoreError(ValueError):
    def __init__(
        self,
        code: TaskStoreErrorCode,
        *,
        committed_generation: int | None = None,
    ) -> None:
        if type(code) is not TaskStoreErrorCode:
            raise TypeError("code must be a TaskStoreErrorCode")
        if code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
            if (
                type(committed_generation) is not int
                or committed_generation < 0
                or committed_generation > MAX_SAFE_JSON_INTEGER
            ):
                raise ValueError("committed_generation is required for uncertain durability")
        elif committed_generation is not None:
            raise ValueError("committed_generation is only valid for uncertain durability")
        self.code = code
        self.message = _error_message(code)
        if committed_generation is not None:
            self.committed_generation = committed_generation
        super().__init__(self.message)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredTaskRun:
    generation: int
    task_run: TaskRun

    def __post_init__(self) -> None:
        if (
            type(self.generation) is not int
            or self.generation < 0
            or self.generation > MAX_SAFE_JSON_INTEGER
        ):
            raise ValueError("generation must be a safe nonnegative integer")
        if type(self.task_run) is not TaskRun:
            raise TypeError("task_run must be an exact TaskRun")


class _RecordDecodeError(ValueError):
    pass


def _duplicate_checked_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _RecordDecodeError("duplicate key")
        result[key] = value
    return result


def _parse_float(value: str) -> float:
    if len(value) > 64:
        raise _RecordDecodeError("floating point token is too large")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _RecordDecodeError("non-finite values are forbidden")
    return parsed


def _reject_constant(_value: str):
    raise _RecordDecodeError("non-finite values are forbidden")


def _parse_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > 16:
        raise _RecordDecodeError("integer token is too large")
    return int(value)


def _canonical_json(value) -> bytes:
    failed = False
    result = b""
    try:
        result = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        failed = True
    if failed:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    return result


def _validate_json_resources(value) -> None:
    count = 0
    stack = [value]
    while stack:
        current = stack.pop()
        count += 1
        if count > _MAX_JSON_NODES:
            raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
        if type(current) is str:
            failed = False
            size = 0
            try:
                size = len(current.encode("utf-8"))
            except UnicodeError:
                failed = True
            if failed or size > _MAX_JSON_STRING_BYTES:
                raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
        elif type(current) is list:
            stack.extend(current)
        elif type(current) is dict:
            stack.extend(current.values())
            stack.extend(current.keys())
        elif current is None or type(current) in (bool, int):
            continue
        elif type(current) is float:
            if not math.isfinite(current):
                raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
        else:
            raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)


def _json_depth_is_safe(raw: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
            continue
        if byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return False
        elif byte in (93, 125):
            depth -= 1
            if depth < 0:
                return False
    return True


def _task_id(value) -> str:
    if type(value) is not str or _TASK_ID_RE.fullmatch(value) is None:
        raise TaskStoreError(TaskStoreErrorCode.INVALID_ID)
    return value


def _generation(value) -> int:
    if type(value) is not int or value < 0 or value > MAX_SAFE_JSON_INTEGER:
        raise TaskStoreError(TaskStoreErrorCode.CONFLICT)
    return value


def _record_name(task_id: str) -> str:
    digest = hashlib.sha256(_KEY_DOMAIN + task_id.encode("utf-8")).hexdigest()
    return f"{digest}.json"


def _record_body(task_run: TaskRun, generation: int):
    return {
        "generation": generation,
        "schema_version": 1,
        "task_run": task_run.to_mapping(),
    }


def _encode_record(task_run: TaskRun, generation: int) -> bytes:
    if type(task_run) is not TaskRun:
        raise TypeError("task_run must be an exact TaskRun")
    body = _record_body(task_run, generation)
    body_bytes = _canonical_json(body)
    checksum = hashlib.sha256(_CHECKSUM_DOMAIN + body_bytes).hexdigest()
    raw = _canonical_json({**body, "checksum": checksum})
    if len(raw) > _MAX_RECORD_BYTES:
        raise TaskStoreError(TaskStoreErrorCode.RECORD_TOO_LARGE)
    if not _json_depth_is_safe(raw):
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    _validate_json_resources({**body, "checksum": checksum})
    decoded = _decode_record(raw, task_run.id)
    if decoded.generation != generation or decoded.task_run != task_run:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    return raw


def _decode_record(raw: bytes, selected_task_id: str) -> StoredTaskRun:
    if type(raw) is not bytes:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    if len(raw) > _MAX_RECORD_BYTES:
        raise TaskStoreError(TaskStoreErrorCode.RECORD_TOO_LARGE)
    if not _json_depth_is_safe(raw):
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    failed = False
    decoded = None
    try:
        text = raw.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_duplicate_checked_object,
            parse_float=_parse_float,
            parse_int=_parse_integer,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        failed = True
    if failed:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    _validate_json_resources(decoded)
    if type(decoded) is not dict or set(decoded) != {
        "checksum",
        "generation",
        "schema_version",
        "task_run",
    }:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    schema_version = decoded["schema_version"]
    generation = decoded["generation"]
    task_mapping = decoded["task_run"]
    checksum = decoded["checksum"]
    if type(schema_version) is not int or schema_version != 1:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    if type(generation) is not int or generation < 0 or generation > MAX_SAFE_JSON_INTEGER:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    if type(task_mapping) is not dict:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    if type(checksum) is not str or _CHECKSUM_RE.fullmatch(checksum) is None:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    canonical = _canonical_json(decoded)
    if canonical != raw:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    body = {
        "generation": generation,
        "schema_version": schema_version,
        "task_run": task_mapping,
    }
    expected = hashlib.sha256(_CHECKSUM_DOMAIN + _canonical_json(body)).hexdigest()
    if not secrets.compare_digest(checksum, expected):
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    task_failed = False
    task_run = None
    try:
        task_run = TaskRun.from_mapping(task_mapping)
    except (KeyError, TypeError, ValueError, RecursionError):
        task_failed = True
    if task_failed or type(task_run) is not TaskRun:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    if task_run.to_mapping() != task_mapping or task_run.id != selected_task_id:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    return StoredTaskRun(generation=generation, task_run=task_run)


def _require_storage_capabilities() -> None:
    missing = False
    try:
        if sys.platform not in ("darwin", "linux"):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if (
            type(os.O_RDONLY) is not int
            or type(os.O_WRONLY) is not int
            or type(os.O_CREAT) is not int
            or type(os.O_EXCL) is not int
            or type(os.O_NOFOLLOW) is not int
            or type(os.O_CLOEXEC) is not int
            or type(os.O_DIRECTORY) is not int
            or type(os.O_TRUNC) is not int
            or type(os.O_APPEND) is not int
        ):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if type(os.supports_dir_fd) is not type({None}):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if type(os.supports_follow_symlinks) is not type({None}):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if (
            not callable(os.open)
            or not callable(os.stat)
            or not callable(os.fstat)
            or not callable(os.read)
            or not callable(os.write)
            or not callable(os.fsync)
            or not callable(os.replace)
            or not callable(os.unlink)
            or not callable(os.close)
            or not callable(os.geteuid)
            or not callable(os.get_inheritable)
        ):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if os.open not in os.supports_dir_fd:
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if os.stat not in os.supports_dir_fd:
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if os.stat not in os.supports_follow_symlinks:
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if os.unlink not in os.supports_dir_fd:
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    except AttributeError:
        missing = True
    if missing:
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)


def _coerce_root(root) -> tuple[str, ...]:
    if type(root) is not str and type(root) is not type(Path("/")):
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    failed = False
    path = None
    try:
        path = Path(root).absolute()
    except (OSError, TypeError, ValueError):
        failed = True
    if failed or path is None or not path.is_absolute():
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    parts = path.parts
    if not parts or parts[0] != "/" or any(part in ("", ".", "..") for part in parts[1:]):
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    return parts


def _close(fd: int) -> bool:
    failed = False
    try:
        os.close(fd)
    except OSError:
        failed = True
    return not failed


def _checked_fstat(fd: int, code: TaskStoreErrorCode):
    failed = False
    result = None
    try:
        result = os.fstat(fd)
    except OSError:
        failed = True
    if failed or result is None:
        raise TaskStoreError(code)
    return result


def _checked_inheritable(fd: int, code: TaskStoreErrorCode) -> bool:
    failed = False
    result = False
    try:
        result = os.get_inheritable(fd)
    except OSError:
        failed = True
    if failed or type(result) is not bool:
        raise TaskStoreError(code)
    return result


def _effective_uid() -> int:
    failed = False
    result = -1
    try:
        result = os.geteuid()
    except OSError:
        failed = True
    if failed or type(result) is not int or result < 0:
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    return result


def _directory_safe(result, *, final: bool) -> bool:
    if not stat.S_ISDIR(result.st_mode):
        return False
    if final:
        return result.st_uid == _effective_uid() and stat.S_IMODE(result.st_mode) == 0o700
    return True


def _open_root(parts: tuple[str, ...], expected_identity) -> tuple[int, object]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    failure = None
    current_fd = -1
    try:
        open_failed = False
        try:
            current_fd = os.open("/", flags)
        except (OSError, TypeError, ValueError):
            open_failed = True
        if open_failed or current_fd < 0:
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        current_stat = _checked_fstat(current_fd, TaskStoreErrorCode.UNSAFE_STORE)
        if _checked_inheritable(current_fd, TaskStoreErrorCode.UNSAFE_STORE):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if not _directory_safe(current_stat, final=len(parts) == 1):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        for index, part in enumerate(parts[1:], start=1):
            next_fd = -1
            next_open_failed = False
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except (OSError, TypeError, ValueError):
                next_open_failed = True
            if next_open_failed or next_fd < 0:
                raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
            if not _close(current_fd):
                _close(next_fd)
                current_fd = -1
                raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
            current_fd = next_fd
            current_stat = _checked_fstat(current_fd, TaskStoreErrorCode.UNSAFE_STORE)
            if _checked_inheritable(current_fd, TaskStoreErrorCode.UNSAFE_STORE):
                raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
            if not _directory_safe(current_stat, final=index == len(parts) - 1):
                raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        identity = (current_stat.st_dev, current_stat.st_ino)
        if expected_identity is not None and identity != expected_identity:
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    except TaskStoreError as exc:
        failure = exc.code
    if failure is not None:
        if current_fd >= 0:
            _close(current_fd)
        raise TaskStoreError(failure)
    return current_fd, current_stat


def _regular_safe(result, root_stat) -> bool:
    return (
        stat.S_ISREG(result.st_mode)
        and result.st_uid == _effective_uid()
        and stat.S_IMODE(result.st_mode) == 0o600
        and result.st_nlink == 1
        and result.st_dev == root_stat.st_dev
    )


def _path_stat(root_fd: int, filename: str):
    missing = False
    failed = False
    result = None
    try:
        result = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        missing = True
    except OSError:
        failed = True
    if failed:
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    if missing:
        return None
    return result


def _read_record(root_fd: int, root_stat, filename: str, task_id: str):
    before = _path_stat(root_fd, filename)
    if before is None:
        return None
    if not _regular_safe(before, root_stat):
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    fd = -1
    open_failed = False
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(filename, flags, dir_fd=root_fd)
    except OSError:
        open_failed = True
    if open_failed or fd < 0:
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    failure = None
    identity = None
    after_fd = None
    chunks = []
    total = 0
    try:
        opened = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
        identity = (opened.st_dev, opened.st_ino)
        expected = (before.st_dev, before.st_ino)
        if (
            not _regular_safe(opened, root_stat)
            or identity != expected
            or _checked_inheritable(fd, TaskStoreErrorCode.UNSAFE_STORE)
        ):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if opened.st_size > _MAX_RECORD_BYTES:
            raise TaskStoreError(TaskStoreErrorCode.RECORD_TOO_LARGE)
        read_failed = False
        while total <= _MAX_RECORD_BYTES:
            chunk = b""
            try:
                chunk = os.read(fd, min(65536, _MAX_RECORD_BYTES + 1 - total))
            except OSError:
                read_failed = True
            if read_failed or not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if read_failed:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        if total > _MAX_RECORD_BYTES:
            raise TaskStoreError(TaskStoreErrorCode.RECORD_TOO_LARGE)
        after_fd = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
    except TaskStoreError as exc:
        failure = exc.code
    close_ok = _close(fd)
    if failure is None and not close_ok:
        failure = TaskStoreErrorCode.IO_ERROR
    if failure is None:
        after_path = None
        try:
            after_path = _path_stat(root_fd, filename)
            if (
                after_path is None
                or after_fd is None
                or identity is None
                or not _regular_safe(after_fd, root_stat)
                or not _regular_safe(after_path, root_stat)
                or (after_fd.st_dev, after_fd.st_ino) != identity
                or (after_path.st_dev, after_path.st_ino) != identity
                or after_fd.st_size != total
            ):
                raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        except TaskStoreError as exc:
            failure = exc.code
    if failure is not None:
        raise TaskStoreError(failure)
    return _decode_record(b"".join(chunks), task_id)


def _temp_identity_matches(root_fd: int, name: str, identity) -> bool:
    result = _path_stat(root_fd, name)
    return result is not None and (result.st_dev, result.st_ino) == identity


def _cleanup_temp(root_fd: int, name: str, identity) -> None:
    if identity is None:
        return
    matches = False
    try:
        matches = _temp_identity_matches(root_fd, name, identity)
    except TaskStoreError:
        return
    if not matches:
        return
    try:
        os.unlink(name, dir_fd=root_fd)
    except OSError:
        return


def _write_all(fd: int, raw: bytes) -> bool:
    offset = 0
    failed = False
    while offset < len(raw):
        written = 0
        try:
            written = os.write(fd, raw[offset:])
        except OSError:
            failed = True
        if failed or written <= 0 or written > len(raw) - offset:
            return False
        offset += written
    return True


def _release(lease: ResourceLease) -> bool:
    failed = False
    try:
        lease.release(owner_token=lease.owner_token)
    except (LeaseError, OSError):
        failed = True
    return not failed


class TaskRunStore:
    __slots__ = ("_lease_manager", "_root_identity", "_root_parts")

    def __init__(
        self,
        root,
        lease_manager,
        *,
        trust,
    ) -> None:
        if type(trust) is not TaskStoreRootTrust or trust is not TaskStoreRootTrust.TRUSTED_LOCAL:
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if type(lease_manager) is not ResourceLeaseManager:
            raise TypeError("lease_manager must be an exact ResourceLeaseManager")
        _require_storage_capabilities()
        parts = _coerce_root(root)
        root_fd, root_stat = _open_root(parts, None)
        if not _close(root_fd):
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        self._root_parts = parts
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self._lease_manager = lease_manager

    def _acquire(self, task_id: str) -> ResourceLease:
        failed = False
        lease = None
        try:
            lease = self._lease_manager.acquire(f"task-store:{task_id}")
        except LeaseError:
            failed = True
        if failed or type(lease) is not ResourceLease:
            raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE)
        return lease

    def _load_locked(self, task_id: str):
        root_fd = -1
        failure = None
        stored = None
        try:
            root_fd, root_stat = _open_root(self._root_parts, self._root_identity)
            filename = _record_name(task_id)
            stored = _read_record(root_fd, root_stat, filename, task_id)
        except TaskStoreError as exc:
            failure = exc.code
        except OSError:
            failure = TaskStoreErrorCode.IO_ERROR
        close_ok = root_fd < 0 or _close(root_fd)
        if failure is not None:
            raise TaskStoreError(failure)
        if not close_ok:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        return stored

    def load(self, task_id):
        task_id = _task_id(task_id)
        _require_storage_capabilities()
        lease = self._acquire(task_id)
        failure = None
        stored = None
        try:
            try:
                stored = self._load_locked(task_id)
            except TaskStoreError as exc:
                failure = exc.code
            except OSError:
                failure = TaskStoreErrorCode.IO_ERROR
        finally:
            release_ok = _release(lease)
        if failure is not None:
            raise TaskStoreError(failure)
        if not release_ok:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        if stored is None:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
        return stored

    def create(self, task_run):
        if type(task_run) is not TaskRun:
            raise TypeError("task_run must be an exact TaskRun")
        raw = _encode_record(task_run, 0)
        return self._mutate(task_run.id, None, 0, task_run, raw)

    def validate_record(self, task_run, generation):
        generation = _generation(generation)
        _encode_record(task_run, generation)

    def compare_and_set(
        self,
        task_id,
        expected_generation,
        task_run,
    ):
        task_id = _task_id(task_id)
        expected_generation = _generation(expected_generation)
        if type(task_run) is not TaskRun:
            raise TypeError("task_run must be an exact TaskRun")
        if task_run.id != task_id:
            raise TaskStoreError(TaskStoreErrorCode.INVALID_ID)
        if expected_generation == MAX_SAFE_JSON_INTEGER:
            raise TaskStoreError(TaskStoreErrorCode.CONFLICT)
        next_generation = expected_generation + 1
        raw = _encode_record(task_run, next_generation)
        return self._mutate(
            task_id,
            expected_generation,
            next_generation,
            task_run,
            raw,
        )

    def _mutate(
        self,
        task_id: str,
        expected_generation: int | None,
        next_generation: int,
        task_run: TaskRun,
        raw: bytes,
    ) -> StoredTaskRun:
        _task_id(task_id)
        if task_run.id != task_id:
            raise TaskStoreError(TaskStoreErrorCode.INVALID_ID)
        _require_storage_capabilities()
        lease = self._acquire(task_id)
        failure = None
        committed_generation = None
        result = None
        try:
            try:
                result = self._mutate_locked(
                    task_id,
                    expected_generation,
                    next_generation,
                    task_run,
                    raw,
                )
            except TaskStoreError as exc:
                failure = exc.code
                if hasattr(exc, "committed_generation"):
                    committed_generation = exc.committed_generation
            except OSError:
                failure = TaskStoreErrorCode.IO_ERROR
        finally:
            release_ok = _release(lease)
        if failure is not None:
            if failure is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
                raise TaskStoreError(
                    failure,
                    committed_generation=committed_generation,
                )
            raise TaskStoreError(failure)
        if not release_ok:
            raise TaskStoreError(
                TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                committed_generation=next_generation,
            )
        assert result is not None
        return result

    def _mutate_locked(
        self,
        task_id: str,
        expected_generation: int | None,
        next_generation: int,
        task_run: TaskRun,
        raw: bytes,
    ) -> StoredTaskRun:
        root_fd = -1
        temp_fd = -1
        temp_name = ""
        temp_identity = None
        temp_created = False
        replaced = False
        failure = None
        try:
            root_fd, root_stat = _open_root(self._root_parts, self._root_identity)
            filename = _record_name(task_id)
            current = _read_record(root_fd, root_stat, filename, task_id)
            if expected_generation is None:
                if current is not None:
                    raise TaskStoreError(TaskStoreErrorCode.ALREADY_EXISTS)
            elif current is None:
                raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
            elif current.generation != expected_generation:
                raise TaskStoreError(TaskStoreErrorCode.CONFLICT)

            token = secrets.token_hex(16)
            temp_name = f".{filename}.{token}.tmp"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            temp_fd = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
            if temp_fd < 0:
                raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
            temp_created = True
            temp_stat = _checked_fstat(temp_fd, TaskStoreErrorCode.UNSAFE_STORE)
            temp_identity = (temp_stat.st_dev, temp_stat.st_ino)
            if not _regular_safe(temp_stat, root_stat) or _checked_inheritable(
                temp_fd,
                TaskStoreErrorCode.UNSAFE_STORE,
            ):
                raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
            if not _write_all(temp_fd, raw):
                raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
            os.fsync(temp_fd)
            close_ok = _close(temp_fd)
            temp_fd = -1
            if not close_ok:
                raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)

            verification_fd = -1
            verification_failed = False
            try:
                verification_fd, _verification_stat = _open_root(
                    self._root_parts,
                    self._root_identity,
                )
            except TaskStoreError:
                verification_failed = True
            if verification_failed:
                raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
            verification_close_ok = _close(verification_fd)
            verification_fd = -1
            if not verification_close_ok:
                raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)

            latest = _read_record(root_fd, root_stat, filename, task_id)
            if expected_generation is None and latest is not None:
                raise TaskStoreError(TaskStoreErrorCode.ALREADY_EXISTS)
            if expected_generation is not None:
                if latest is None:
                    raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
                if latest.generation != expected_generation:
                    raise TaskStoreError(TaskStoreErrorCode.CONFLICT)

            os.replace(
                temp_name,
                filename,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
            replaced = True
            os.fsync(root_fd)
        except TaskStoreError as exc:
            failure = exc.code
        except OSError:
            failure = (
                TaskStoreErrorCode.DURABILITY_UNCERTAIN if replaced else TaskStoreErrorCode.IO_ERROR
            )

        if temp_fd >= 0:
            temp_close_ok = _close(temp_fd)
            temp_fd = -1
            if failure is None and not temp_close_ok:
                failure = TaskStoreErrorCode.IO_ERROR
        if temp_created and not replaced:
            _cleanup_temp(root_fd, temp_name, temp_identity)
        root_close_ok = root_fd < 0 or _close(root_fd)
        if failure is None and not root_close_ok:
            failure = (
                TaskStoreErrorCode.DURABILITY_UNCERTAIN if replaced else TaskStoreErrorCode.IO_ERROR
            )

        if failure is not None:
            if failure is TaskStoreErrorCode.DURABILITY_UNCERTAIN or replaced:
                raise TaskStoreError(
                    TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                    committed_generation=next_generation,
                )
            raise TaskStoreError(failure)
        return StoredTaskRun(generation=next_generation, task_run=task_run)
