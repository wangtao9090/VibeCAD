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
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    ResourceLease,
    ResourceLeaseManager,
)
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
_JOURNAL_CHECKSUM_DOMAIN = b"vibecad-task-store-mutation-v1\0"
_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")
_RECORD_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
_TEMP_NAME_RE = re.compile(r"^\.[0-9a-f]{64}\.json\.[0-9a-f]{32}\.tmp$")
_DECIMAL_ID_RE = re.compile(r"^(0|[1-9][0-9]{0,19})$")
_MAX_RECORD_BYTES = 2 * 1024 * 1024
_MAX_TASK_RECORDS = 1024
_MAX_TASK_STORE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_JOURNAL_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 8192
_MAX_JSON_STRING_BYTES = 65536
_MUTATION_JOURNAL_NAME = ".mutation.json"
_CATALOG_LEASE_RESOURCE = "task-store:catalog"


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
    RESOURCE_EXHAUSTED = "resource_exhausted"


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
        case TaskStoreErrorCode.RESOURCE_EXHAUSTED:
            return "The task store capacity is exhausted."


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
    normalized_task_mapping = task_mapping
    if "creation_digest" not in task_mapping:
        normalized_task_mapping = {**task_mapping, "creation_digest": None}
    task_failed = False
    task_run = None
    try:
        task_run = TaskRun.from_mapping(task_mapping)
    except (KeyError, TypeError, ValueError, RecursionError):
        task_failed = True
    if task_failed or type(task_run) is not TaskRun:
        raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)
    if task_run.to_mapping() != normalized_task_mapping or task_run.id != selected_task_id:
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
            or not callable(os.scandir)
            or not callable(os.ftruncate)
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


@dataclass(frozen=True, slots=True)
class _StoreSnapshot:
    total_bytes: int
    record_bytes: int
    record_count: int
    journal_present: bool
    temp_names: tuple[str, ...]


def _record_task_id_for_scan(raw: bytes, filename: str) -> str:
    failed = False
    decoded = None
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_checked_object,
            parse_float=_parse_float,
            parse_int=_parse_integer,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        failed = True
    if failed or type(decoded) is not dict:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    task_mapping = decoded.get("task_run")
    task_id = task_mapping.get("id") if type(task_mapping) is dict else None
    if (
        type(task_id) is not str
        or _TASK_ID_RE.fullmatch(task_id) is None
        or _record_name(task_id) != filename
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    try:
        _decode_record(raw, task_id)
    except TaskStoreError:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED) from None
    return task_id


def _scan_store(root_fd: int, root_stat) -> _StoreSnapshot:
    total_bytes = 0
    record_bytes = 0
    record_count = 0
    journal_present = False
    temp_names: list[str] = []
    iterator = None
    failure = None
    try:
        iterator = os.scandir(root_fd)
        with iterator:
            for entry in iterator:
                name = entry.name
                if type(name) is not str or name in ("", ".", ".."):
                    raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
                result = _path_stat(root_fd, name)
                if result is None or not _regular_safe(result, root_stat):
                    raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
                if type(result.st_size) is not int or result.st_size < 0:
                    raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
                total_bytes += result.st_size
                if total_bytes > _MAX_TASK_STORE_BYTES:
                    raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                if _RECORD_NAME_RE.fullmatch(name) is not None:
                    record_count += 1
                    if record_count > _MAX_TASK_RECORDS or result.st_size > _MAX_RECORD_BYTES:
                        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                    raw, _record_stat = _read_named_bytes(
                        root_fd,
                        root_stat,
                        name,
                        limit=_MAX_RECORD_BYTES,
                        too_large=TaskStoreErrorCode.RESOURCE_EXHAUSTED,
                    )
                    _record_task_id_for_scan(raw, name)
                    record_bytes += result.st_size
                elif name == _MUTATION_JOURNAL_NAME:
                    if journal_present:
                        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                    journal_present = True
                elif _TEMP_NAME_RE.fullmatch(name) is not None:
                    temp_names.append(name)
                    if len(temp_names) > 1:
                        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                else:
                    raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    except TaskStoreError as exc:
        failure = exc.code
    except OSError:
        failure = TaskStoreErrorCode.IO_ERROR
    if failure is not None:
        raise TaskStoreError(failure)
    if len(temp_names) > 1 or (temp_names and not journal_present):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    return _StoreSnapshot(
        total_bytes=total_bytes,
        record_bytes=record_bytes,
        record_count=record_count,
        journal_present=journal_present,
        temp_names=tuple(sorted(temp_names)),
    )


def _read_named_bytes(
    root_fd: int,
    root_stat,
    name: str,
    *,
    limit: int,
    too_large: TaskStoreErrorCode,
) -> tuple[bytes, object]:
    before = _path_stat(root_fd, name)
    if before is None or not _regular_safe(before, root_stat):
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    if before.st_size > limit:
        raise TaskStoreError(too_large)
    fd = -1
    open_failed = False
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
    except OSError:
        open_failed = True
    if open_failed or fd < 0:
        raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
    failure = None
    chunks: list[bytes] = []
    total = 0
    opened = None
    after_fd = None
    try:
        opened = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
        if (
            not _regular_safe(opened, root_stat)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or _checked_inheritable(fd, TaskStoreErrorCode.UNSAFE_STORE)
        ):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        while total <= limit:
            read_failed = False
            chunk = b""
            try:
                chunk = os.read(fd, min(65536, limit + 1 - total))
            except OSError:
                read_failed = True
            if read_failed:
                raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            raise TaskStoreError(too_large)
        after_fd = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
    except TaskStoreError as exc:
        failure = exc.code
    close_ok = _close(fd)
    if failure is None and not close_ok:
        failure = TaskStoreErrorCode.IO_ERROR
    if failure is None:
        after_path = _path_stat(root_fd, name)
        if (
            opened is None
            or after_fd is None
            or after_path is None
            or not _regular_safe(after_fd, root_stat)
            or not _regular_safe(after_path, root_stat)
            or (after_fd.st_dev, after_fd.st_ino) != (opened.st_dev, opened.st_ino)
            or (after_path.st_dev, after_path.st_ino) != (opened.st_dev, opened.st_ino)
            or after_fd.st_size != total
        ):
            failure = TaskStoreErrorCode.UNSAFE_STORE
    if failure is not None:
        raise TaskStoreError(failure)
    return b"".join(chunks), after_fd


def _journal_line(body: dict[str, object]) -> bytes:
    body_bytes = _canonical_json(body)
    checksum = hashlib.sha256(_JOURNAL_CHECKSUM_DOMAIN + body_bytes).hexdigest()
    raw = (
        _canonical_json(
            {
                "body": body,
                "body_sha256": checksum,
                "schema_version": 1,
            }
        )
        + b"\n"
    )
    if len(raw) > _MAX_JOURNAL_BYTES:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    return raw


def _journal_body(
    *,
    state: str,
    task_id: str,
    target: str,
    old_sha256: str | None,
    new_sha256: str,
    new_size: int,
    temp_name: str,
    temp: dict[str, int | str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "new_sha256": new_sha256,
        "new_size": new_size,
        "old_sha256": old_sha256,
        "schema_version": 1,
        "state": state,
        "target": target,
        "task_id": task_id,
        "temp_name": temp_name,
    }
    if temp is not None:
        body["temp"] = temp
    return body


def _decode_journal_entry(raw: bytes) -> dict[str, object]:
    if not _json_depth_is_safe(raw):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    failed = False
    decoded = None
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_duplicate_checked_object,
            parse_float=_parse_float,
            parse_int=_parse_integer,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, RecursionError):
        failed = True
    if (
        failed
        or type(decoded) is not dict
        or set(decoded)
        != {
            "body",
            "body_sha256",
            "schema_version",
        }
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    try:
        _validate_json_resources(decoded)
    except TaskStoreError:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED) from None
    if decoded["schema_version"] != 1 or type(decoded["schema_version"]) is not int:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    body = decoded["body"]
    checksum = decoded["body_sha256"]
    if (
        type(body) is not dict
        or type(checksum) is not str
        or _CHECKSUM_RE.fullmatch(checksum) is None
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    try:
        canonical = _canonical_json(decoded)
    except TaskStoreError:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED) from None
    if canonical != raw:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    try:
        body_bytes = _canonical_json(body)
    except TaskStoreError:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED) from None
    expected_checksum = hashlib.sha256(_JOURNAL_CHECKSUM_DOMAIN + body_bytes).hexdigest()
    if not secrets.compare_digest(checksum, expected_checksum):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    common = {
        "new_sha256",
        "new_size",
        "old_sha256",
        "schema_version",
        "state",
        "target",
        "task_id",
        "temp_name",
    }
    state = body.get("state")
    expected_keys = common if state == "RESERVED" else common | {"temp"}
    if state not in ("RESERVED", "STAGED") or set(body) != expected_keys:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    task_id = body["task_id"]
    target = body["target"]
    temp_name = body["temp_name"]
    old_sha256 = body["old_sha256"]
    new_sha256 = body["new_sha256"]
    new_size = body["new_size"]
    old_sha256_invalid = old_sha256 is not None and (
        type(old_sha256) is not str or _CHECKSUM_RE.fullmatch(old_sha256) is None
    )
    if (
        type(body["schema_version"]) is not int
        or body["schema_version"] != 1
        or type(task_id) is not str
        or _TASK_ID_RE.fullmatch(task_id) is None
        or type(target) is not str
        or target != _record_name(task_id)
        or type(temp_name) is not str
        or _TEMP_NAME_RE.fullmatch(temp_name) is None
        or not temp_name.startswith(f".{target}.")
        or old_sha256_invalid
        or type(new_sha256) is not str
        or _CHECKSUM_RE.fullmatch(new_sha256) is None
        or type(new_size) is not int
        or new_size < 0
        or new_size > _MAX_RECORD_BYTES
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    if state == "STAGED":
        temp = body["temp"]
        if type(temp) is not dict or set(temp) != {
            "dev",
            "ino",
            "mode",
            "sha256",
            "size",
            "uid",
        }:
            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        if (
            any(
                type(temp[key]) is not str or _DECIMAL_ID_RE.fullmatch(temp[key]) is None
                for key in ("dev", "ino", "uid")
            )
            or type(temp["size"]) is not int
            or temp["size"] < 0
            or type(temp["mode"]) is not int
            or temp["mode"] != 0o600
            or temp["size"] != new_size
            or type(temp["sha256"]) is not str
            or temp["sha256"] != new_sha256
        ):
            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    return body


def _read_journal(
    root_fd: int,
    root_stat,
) -> tuple[dict[str, object], int, object, bytes]:
    raw, journal_stat = _read_named_bytes(
        root_fd,
        root_stat,
        _MUTATION_JOURNAL_NAME,
        limit=_MAX_JOURNAL_BYTES,
        too_large=TaskStoreErrorCode.RESOURCE_EXHAUSTED,
    )
    parts = raw.split(b"\n")
    complete = parts[:-1]
    partial = parts[-1]
    if not complete or len(complete) > 2 or any(not item for item in complete):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    entries = [_decode_journal_entry(item) for item in complete]
    first = entries[0]
    if first["state"] != "RESERVED":
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    if len(entries) == 2:
        second = entries[1]
        if partial or second["state"] != "STAGED":
            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
        for key in (
            "new_sha256",
            "new_size",
            "old_sha256",
            "schema_version",
            "target",
            "task_id",
            "temp_name",
        ):
            if first[key] != second[key]:
                raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    elif partial and len(raw) - len(partial) > _MAX_JOURNAL_BYTES:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    valid_length = sum(len(item) + 1 for item in complete)
    return entries[-1], valid_length, journal_stat, partial


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


def _create_reserved_journal(
    root_fd: int,
    root_stat,
    raw: bytes,
) -> tuple[int, int]:
    fd = -1
    created = False
    identity = None
    failure = None
    try:
        fd = os.open(
            _MUTATION_JOURNAL_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=root_fd,
        )
        created = True
        opened = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
        identity = (opened.st_dev, opened.st_ino)
        if not _regular_safe(opened, root_stat) or _checked_inheritable(
            fd, TaskStoreErrorCode.UNSAFE_STORE
        ):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if not _write_all(fd, raw):
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        os.fsync(fd)
    except TaskStoreError as exc:
        failure = exc.code
    except OSError:
        failure = TaskStoreErrorCode.IO_ERROR
    close_ok = fd < 0 or _close(fd)
    if failure is None and not close_ok:
        failure = TaskStoreErrorCode.IO_ERROR
    if failure is None:
        try:
            os.fsync(root_fd)
        except OSError:
            failure = TaskStoreErrorCode.IO_ERROR
    if failure is not None:
        if created and identity is not None:
            _cleanup_temp(root_fd, _MUTATION_JOURNAL_NAME, identity)
        raise TaskStoreError(failure)
    if identity is None:
        raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
    return identity


def _append_staged_journal(
    root_fd: int,
    root_stat,
    journal_identity: tuple[int, int],
    valid_length: int,
    expected_existing: bytes,
    raw: bytes,
) -> None:
    existing, existing_stat = _read_named_bytes(
        root_fd,
        root_stat,
        _MUTATION_JOURNAL_NAME,
        limit=_MAX_JOURNAL_BYTES,
        too_large=TaskStoreErrorCode.RESOURCE_EXHAUSTED,
    )
    before = _path_stat(root_fd, _MUTATION_JOURNAL_NAME)
    if (
        before is None
        or not _regular_safe(before, root_stat)
        or (before.st_dev, before.st_ino) != journal_identity
        or (existing_stat.st_dev, existing_stat.st_ino) != journal_identity
        or existing != expected_existing
        or before.st_size != len(expected_existing)
        or valid_length < 0
        or valid_length > before.st_size
        or valid_length + len(raw) > _MAX_JOURNAL_BYTES
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    fd = -1
    failure = None
    try:
        fd = os.open(
            _MUTATION_JOURNAL_NAME,
            os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        opened = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
        if (
            not _regular_safe(opened, root_stat)
            or (opened.st_dev, opened.st_ino) != journal_identity
            or _checked_inheritable(fd, TaskStoreErrorCode.UNSAFE_STORE)
        ):
            raise TaskStoreError(TaskStoreErrorCode.UNSAFE_STORE)
        if opened.st_size != valid_length:
            os.ftruncate(fd, valid_length)
            os.fsync(fd)
        if not _write_all(fd, raw):
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        os.fsync(fd)
    except TaskStoreError as exc:
        failure = exc.code
    except OSError:
        failure = TaskStoreErrorCode.IO_ERROR
    close_ok = fd < 0 or _close(fd)
    if failure is None and not close_ok:
        failure = TaskStoreErrorCode.IO_ERROR
    if failure is not None:
        raise TaskStoreError(failure)
    current, current_length, after, partial = _read_journal(root_fd, root_stat)
    if (
        current["state"] != "STAGED"
        or current_length != valid_length + len(raw)
        or (after.st_dev, after.st_ino) != journal_identity
        or partial
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)


def _unlink_exact(
    root_fd: int,
    root_stat,
    name: str,
    identity: tuple[int, int],
) -> None:
    before = _path_stat(root_fd, name)
    if (
        before is None
        or not _regular_safe(before, root_stat)
        or (before.st_dev, before.st_ino) != identity
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    try:
        os.unlink(name, dir_fd=root_fd)
        os.fsync(root_fd)
    except OSError:
        raise TaskStoreError(TaskStoreErrorCode.IO_ERROR) from None


def _temp_evidence(
    root_fd: int,
    root_stat,
    *,
    name: str,
    task_id: str,
    expected_sha256: str,
    expected_size: int,
) -> tuple[dict[str, int | str], tuple[int, int]]:
    raw, result = _read_named_bytes(
        root_fd,
        root_stat,
        name,
        limit=_MAX_RECORD_BYTES,
        too_large=TaskStoreErrorCode.RESOURCE_EXHAUSTED,
    )
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != expected_size or not secrets.compare_digest(digest, expected_sha256):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    _decode_record(raw, task_id)
    evidence: dict[str, int | str] = {
        "dev": str(result.st_dev),
        "ino": str(result.st_ino),
        "mode": stat.S_IMODE(result.st_mode),
        "sha256": digest,
        "size": len(raw),
        "uid": str(result.st_uid),
    }
    return evidence, (result.st_dev, result.st_ino)


def _open_verified_publication(
    root_fd: int,
    root_stat,
    *,
    name: str,
    task_id: str,
    expected: dict[str, int | str],
) -> tuple[int, tuple[int, int]]:
    evidence, identity = _temp_evidence(
        root_fd,
        root_stat,
        name=name,
        task_id=task_id,
        expected_sha256=expected["sha256"],
        expected_size=expected["size"],
    )
    if evidence != expected:
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    before = _path_stat(root_fd, name)
    if (
        before is None
        or not _regular_safe(before, root_stat)
        or (before.st_dev, before.st_ino) != identity
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    fd = -1
    failure = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd)
        opened = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
        after_path = _path_stat(root_fd, name)
        if (
            not _regular_safe(opened, root_stat)
            or after_path is None
            or not _regular_safe(after_path, root_stat)
            or (opened.st_dev, opened.st_ino) != identity
            or (after_path.st_dev, after_path.st_ino) != identity
            or str(opened.st_dev) != expected["dev"]
            or str(opened.st_ino) != expected["ino"]
            or str(opened.st_uid) != expected["uid"]
            or stat.S_IMODE(opened.st_mode) != expected["mode"]
            or opened.st_size != expected["size"]
            or _checked_inheritable(fd, TaskStoreErrorCode.UNSAFE_STORE)
        ):
            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
    except TaskStoreError as exc:
        failure = exc.code
    except OSError:
        failure = TaskStoreErrorCode.IO_ERROR
    if failure is not None:
        if fd >= 0:
            _close(fd)
        raise TaskStoreError(failure)
    return fd, identity


def _verify_published_identity(
    root_fd: int,
    root_stat,
    *,
    fd: int,
    name: str,
    identity: tuple[int, int],
    expected: dict[str, int | str],
) -> None:
    opened = _checked_fstat(fd, TaskStoreErrorCode.UNSAFE_STORE)
    published = _path_stat(root_fd, name)
    if (
        published is None
        or not _regular_safe(opened, root_stat)
        or not _regular_safe(published, root_stat)
        or (opened.st_dev, opened.st_ino) != identity
        or (published.st_dev, published.st_ino) != identity
        or str(opened.st_dev) != expected["dev"]
        or str(opened.st_ino) != expected["ino"]
        or str(opened.st_uid) != expected["uid"]
        or stat.S_IMODE(opened.st_mode) != expected["mode"]
        or opened.st_size != expected["size"]
    ):
        raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)


def _record_sha256(stored: StoredTaskRun | None) -> tuple[str | None, int]:
    if stored is None:
        return None, 0
    raw = _encode_record(stored.task_run, stored.generation)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _assert_capacity(
    snapshot: _StoreSnapshot,
    *,
    current: StoredTaskRun | None,
    raw: bytes,
    reserved_line: bytes,
    staged_bound_line: bytes,
) -> None:
    _old_sha256, old_size = _record_sha256(current)
    record_count = snapshot.record_count + (1 if current is None else 0)
    final_record_bytes = snapshot.record_bytes - old_size + len(raw)
    physical_peak = snapshot.total_bytes + len(reserved_line) + len(staged_bound_line) + len(raw)
    if (
        record_count > _MAX_TASK_RECORDS
        or final_record_bytes > _MAX_TASK_STORE_BYTES
        or physical_peak > _MAX_TASK_STORE_BYTES
    ):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)


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
        catalog_lease = None
        catalog_contended = False
        try:
            catalog_lease = lease_manager.acquire(_CATALOG_LEASE_RESOURCE)
        except LeaseError as exc:
            catalog_contended = exc.code is LeaseErrorCode.CONTENDED
            if not catalog_contended:
                raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE) from None
        if catalog_lease is not None and not _release(catalog_lease):
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)

    def _acquire(self, task_id: str) -> ResourceLease:
        return self._acquire_resource(f"task-store:{task_id}")

    def _acquire_catalog(self) -> ResourceLease:
        return self._acquire_resource(_CATALOG_LEASE_RESOURCE)

    def _acquire_resource(self, resource_id: str) -> ResourceLease:
        failed = False
        lease = None
        try:
            lease = self._lease_manager.acquire(resource_id)
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
        probed = self._load_locked(task_id)
        if probed is None:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
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
        catalog_lease = self._acquire_catalog()
        task_lease = None
        failure = None
        committed_generation = None
        result = None
        try:
            try:
                self._recover_pending_mutation()
                prepared = self._prepare_mutation(task_id, expected_generation, raw)
                task_lease = self._acquire(task_id)
                result = self._mutate_locked(
                    task_id,
                    expected_generation,
                    next_generation,
                    task_run,
                    raw,
                    prepared,
                )
            except TaskStoreError as exc:
                failure = exc.code
                if hasattr(exc, "committed_generation"):
                    committed_generation = exc.committed_generation
            except OSError:
                failure = TaskStoreErrorCode.IO_ERROR
        finally:
            task_release_ok = task_lease is None or _release(task_lease)
            catalog_release_ok = _release(catalog_lease)
        release_ok = task_release_ok and catalog_release_ok
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

    def _prepare_mutation(
        self,
        task_id: str,
        expected_generation: int | None,
        raw: bytes,
    ) -> tuple[str, bytes, bytes]:
        root_fd = -1
        failure = None
        prepared = None
        try:
            root_fd, root_stat = _open_root(self._root_parts, self._root_identity)
            snapshot = _scan_store(root_fd, root_stat)
            if snapshot.journal_present or snapshot.temp_names:
                raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
            target = _record_name(task_id)
            current = _read_record(root_fd, root_stat, target, task_id)
            self._require_expected(current, expected_generation)
            old_sha256, _old_size = _record_sha256(current)
            new_sha256 = hashlib.sha256(raw).hexdigest()
            temp_name = f".{target}.{secrets.token_hex(16)}.tmp"
            reserved = _journal_line(
                _journal_body(
                    state="RESERVED",
                    task_id=task_id,
                    target=target,
                    old_sha256=old_sha256,
                    new_sha256=new_sha256,
                    new_size=len(raw),
                    temp_name=temp_name,
                )
            )
            staged_bound = self._staged_bound_line(
                task_id=task_id,
                target=target,
                old_sha256=old_sha256,
                new_sha256=new_sha256,
                new_size=len(raw),
                temp_name=temp_name,
            )
            _assert_capacity(
                snapshot,
                current=current,
                raw=raw,
                reserved_line=reserved,
                staged_bound_line=staged_bound,
            )
            prepared = (temp_name, reserved, staged_bound)
        except TaskStoreError as exc:
            failure = exc.code
        except OSError:
            failure = TaskStoreErrorCode.IO_ERROR
        close_ok = root_fd < 0 or _close(root_fd)
        if failure is not None:
            raise TaskStoreError(failure)
        if not close_ok or prepared is None:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        return prepared

    @staticmethod
    def _staged_bound_line(
        *,
        task_id: str,
        target: str,
        old_sha256: str | None,
        new_sha256: str,
        new_size: int,
        temp_name: str,
    ) -> bytes:
        return _journal_line(
            _journal_body(
                state="STAGED",
                task_id=task_id,
                target=target,
                old_sha256=old_sha256,
                new_sha256=new_sha256,
                new_size=new_size,
                temp_name=temp_name,
                temp={
                    "dev": "9" * 20,
                    "ino": "9" * 20,
                    "mode": 0o600,
                    "sha256": new_sha256,
                    "size": new_size,
                    "uid": "9" * 20,
                },
            )
        )

    @staticmethod
    def _require_expected(
        current: StoredTaskRun | None,
        expected_generation: int | None,
    ) -> None:
        if expected_generation is None:
            if current is not None:
                raise TaskStoreError(TaskStoreErrorCode.ALREADY_EXISTS)
        elif current is None:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
        elif current.generation != expected_generation:
            raise TaskStoreError(TaskStoreErrorCode.CONFLICT)

    def _recover_pending_mutation(self) -> None:
        root_fd = -1
        journal = None
        clean = False
        failure = None
        try:
            root_fd, root_stat = _open_root(self._root_parts, self._root_identity)
            snapshot = _scan_store(root_fd, root_stat)
            if not snapshot.journal_present:
                if snapshot.temp_names:
                    raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                clean = True
            else:
                journal, _valid_length, _journal_stat, _partial = _read_journal(
                    root_fd,
                    root_stat,
                )
        except TaskStoreError as exc:
            failure = exc.code
        except OSError:
            failure = TaskStoreErrorCode.IO_ERROR
        close_ok = root_fd < 0 or _close(root_fd)
        if failure is not None:
            raise TaskStoreError(failure)
        if not close_ok:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        if clean:
            return
        if journal is None:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        lease = self._acquire(journal["task_id"])
        try:
            self._recover_pending_locked()
        finally:
            release_ok = _release(lease)
        if not release_ok:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)

    def _recover_pending_locked(self) -> None:
        root_fd = -1
        publication_fd = -1
        clean = False
        failure = None
        try:
            root_fd, root_stat = _open_root(self._root_parts, self._root_identity)
            snapshot = _scan_store(root_fd, root_stat)
            if not snapshot.journal_present:
                if snapshot.temp_names:
                    raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                clean = True
            else:
                journal, valid_length, journal_stat, partial = _read_journal(
                    root_fd,
                    root_stat,
                )
                journal_identity = (journal_stat.st_dev, journal_stat.st_ino)
                task_id = journal["task_id"]
                target = journal["target"]
                temp_name = journal["temp_name"]
                current = _read_record(root_fd, root_stat, target, task_id)
                current_sha256, _current_size = _record_sha256(current)
                temp_present = temp_name in snapshot.temp_names
                if snapshot.temp_names and not temp_present:
                    raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                if journal["state"] == "RESERVED":
                    if not temp_present:
                        if partial:
                            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                        if current_sha256 not in (
                            journal["old_sha256"],
                            journal["new_sha256"],
                        ):
                            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                        _unlink_exact(
                            root_fd,
                            root_stat,
                            _MUTATION_JOURNAL_NAME,
                            journal_identity,
                        )
                        clean = True
                    else:
                        evidence, _temp_identity = _temp_evidence(
                            root_fd,
                            root_stat,
                            name=temp_name,
                            task_id=task_id,
                            expected_sha256=journal["new_sha256"],
                            expected_size=journal["new_size"],
                        )
                        publication_fd, _publication_identity = _open_verified_publication(
                            root_fd,
                            root_stat,
                            name=temp_name,
                            task_id=task_id,
                            expected=evidence,
                        )
                        os.fsync(publication_fd)
                        synced_fd = publication_fd
                        publication_fd = -1
                        if not _close(synced_fd):
                            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
                        staged = _journal_body(
                            state="STAGED",
                            task_id=task_id,
                            target=target,
                            old_sha256=journal["old_sha256"],
                            new_sha256=journal["new_sha256"],
                            new_size=journal["new_size"],
                            temp_name=temp_name,
                            temp=evidence,
                        )
                        staged_line = _journal_line(staged)
                        if partial and (
                            len(partial) >= len(staged_line)
                            or staged_line[: len(partial)] != partial
                        ):
                            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                        _append_staged_journal(
                            root_fd,
                            root_stat,
                            journal_identity,
                            valid_length,
                            _journal_line(journal) + partial,
                            staged_line,
                        )
                        journal = staged
                if not clean:
                    if journal["state"] != "STAGED":
                        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                    current = _read_record(root_fd, root_stat, target, task_id)
                    current_sha256, _current_size = _record_sha256(current)
                    temp_stat = _path_stat(root_fd, temp_name)
                    if temp_stat is None:
                        if current_sha256 not in (
                            journal["old_sha256"],
                            journal["new_sha256"],
                        ):
                            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                    else:
                        evidence, _temp_identity = _temp_evidence(
                            root_fd,
                            root_stat,
                            name=temp_name,
                            task_id=task_id,
                            expected_sha256=journal["new_sha256"],
                            expected_size=journal["new_size"],
                        )
                        if evidence != journal["temp"] or current_sha256 != journal["old_sha256"]:
                            raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                        publication_fd, publication_identity = _open_verified_publication(
                            root_fd,
                            root_stat,
                            name=temp_name,
                            task_id=task_id,
                            expected=journal["temp"],
                        )
                        os.replace(
                            temp_name,
                            target,
                            src_dir_fd=root_fd,
                            dst_dir_fd=root_fd,
                        )
                        _verify_published_identity(
                            root_fd,
                            root_stat,
                            fd=publication_fd,
                            name=target,
                            identity=publication_identity,
                            expected=journal["temp"],
                        )
                        os.fsync(root_fd)
                        published = _read_record(root_fd, root_stat, target, task_id)
                        published_sha256, _published_size = _record_sha256(published)
                        if published_sha256 != journal["new_sha256"]:
                            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
                    _unlink_exact(
                        root_fd,
                        root_stat,
                        _MUTATION_JOURNAL_NAME,
                        journal_identity,
                    )
        except TaskStoreError as exc:
            failure = exc.code
        except OSError:
            failure = TaskStoreErrorCode.IO_ERROR
        publication_close_ok = publication_fd < 0 or _close(publication_fd)
        close_ok = root_fd < 0 or _close(root_fd)
        if failure is not None:
            raise TaskStoreError(failure)
        if not publication_close_ok:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        if not close_ok:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)

    def _mutate_locked(
        self,
        task_id: str,
        expected_generation: int | None,
        next_generation: int,
        task_run: TaskRun,
        raw: bytes,
        prepared: tuple[str, bytes, bytes],
    ) -> StoredTaskRun:
        root_fd = -1
        temp_fd = -1
        publication_fd = -1
        temp_name, reserved_line, _staged_bound = prepared
        temp_identity = None
        temp_created = False
        temp_rollforward_capable = False
        publication_identity_invalid = False
        journal_identity = None
        journal_created = False
        replaced = False
        failure = None
        result = None
        try:
            try:
                root_fd, root_stat = _open_root(self._root_parts, self._root_identity)
                snapshot = _scan_store(root_fd, root_stat)
                if snapshot.journal_present or snapshot.temp_names:
                    raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                filename = _record_name(task_id)
                current = _read_record(root_fd, root_stat, filename, task_id)
                self._require_expected(current, expected_generation)
                if (
                    current is not None
                    and current.task_run.creation_digest != task_run.creation_digest
                ):
                    raise TaskStoreError(TaskStoreErrorCode.CONFLICT)
                old_sha256, _old_size = _record_sha256(current)
                new_sha256 = hashlib.sha256(raw).hexdigest()
                staged_bound = self._staged_bound_line(
                    task_id=task_id,
                    target=filename,
                    old_sha256=old_sha256,
                    new_sha256=new_sha256,
                    new_size=len(raw),
                    temp_name=temp_name,
                )
                _assert_capacity(
                    snapshot,
                    current=current,
                    raw=raw,
                    reserved_line=reserved_line,
                    staged_bound_line=staged_bound,
                )
                journal_identity = _create_reserved_journal(root_fd, root_stat, reserved_line)
                journal_created = True

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
                temp_rollforward_capable = True
                os.fsync(temp_fd)
                close_ok = _close(temp_fd)
                temp_fd = -1
                if not close_ok:
                    raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)

                try:
                    evidence, evidence_identity = _temp_evidence(
                        root_fd,
                        root_stat,
                        name=temp_name,
                        task_id=task_id,
                        expected_sha256=new_sha256,
                        expected_size=len(raw),
                    )
                except TaskStoreError as exc:
                    publication_identity_invalid = exc.code in {
                        TaskStoreErrorCode.RESOURCE_EXHAUSTED,
                        TaskStoreErrorCode.UNSAFE_STORE,
                    }
                    raise
                if evidence_identity != temp_identity:
                    publication_identity_invalid = True
                    raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)
                staged_line = _journal_line(
                    _journal_body(
                        state="STAGED",
                        task_id=task_id,
                        target=filename,
                        old_sha256=old_sha256,
                        new_sha256=new_sha256,
                        new_size=len(raw),
                        temp_name=temp_name,
                        temp=evidence,
                    )
                )
                _append_staged_journal(
                    root_fd,
                    root_stat,
                    journal_identity,
                    len(reserved_line),
                    reserved_line,
                    staged_line,
                )

                verification_fd, _verification_stat = _open_root(
                    self._root_parts,
                    self._root_identity,
                )
                verification_close_ok = _close(verification_fd)
                if not verification_close_ok:
                    raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
                latest = _read_record(root_fd, root_stat, filename, task_id)
                self._require_expected(latest, expected_generation)

                try:
                    publication_fd, publication_identity = _open_verified_publication(
                        root_fd,
                        root_stat,
                        name=temp_name,
                        task_id=task_id,
                        expected=evidence,
                    )
                except TaskStoreError as exc:
                    publication_identity_invalid = exc.code in {
                        TaskStoreErrorCode.RESOURCE_EXHAUSTED,
                        TaskStoreErrorCode.UNSAFE_STORE,
                    }
                    raise

                os.replace(
                    temp_name,
                    filename,
                    src_dir_fd=root_fd,
                    dst_dir_fd=root_fd,
                )
                replaced = True
                _verify_published_identity(
                    root_fd,
                    root_stat,
                    fd=publication_fd,
                    name=filename,
                    identity=publication_identity,
                    expected=evidence,
                )
                os.fsync(root_fd)
                readback = _read_record(root_fd, root_stat, filename, task_id)
                if (
                    readback is None
                    or readback.generation != next_generation
                    or readback.task_run != task_run
                ):
                    raise TaskStoreError(
                        TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                        committed_generation=next_generation,
                    )
                _unlink_exact(
                    root_fd,
                    root_stat,
                    _MUTATION_JOURNAL_NAME,
                    journal_identity,
                )
                journal_created = False
                result = readback
            except TaskStoreError as exc:
                failure = exc.code
            except OSError:
                failure = (
                    TaskStoreErrorCode.DURABILITY_UNCERTAIN
                    if replaced
                    else TaskStoreErrorCode.IO_ERROR
                )

            if failure is not None and not replaced:
                if temp_created and temp_identity is not None:
                    _cleanup_temp(root_fd, temp_name, temp_identity)
                temp_remaining = False
                if root_fd >= 0:
                    try:
                        temp_remaining = _path_stat(root_fd, temp_name) is not None
                    except TaskStoreError:
                        temp_remaining = True
                if journal_created and journal_identity is not None and not temp_remaining:
                    try:
                        _unlink_exact(
                            root_fd,
                            root_stat,
                            _MUTATION_JOURNAL_NAME,
                            journal_identity,
                        )
                        journal_created = False
                    except TaskStoreError:
                        pass
                if (
                    journal_created
                    and temp_remaining
                    and temp_rollforward_capable
                    and not publication_identity_invalid
                ):
                    failure = TaskStoreErrorCode.DURABILITY_UNCERTAIN
        finally:
            publication_close_ok = publication_fd < 0 or _close(publication_fd)
            temp_close_ok = temp_fd < 0 or _close(temp_fd)
            root_close_ok = root_fd < 0 or _close(root_fd)
        if failure is None and not publication_close_ok:
            failure = (
                TaskStoreErrorCode.DURABILITY_UNCERTAIN if replaced else TaskStoreErrorCode.IO_ERROR
            )
        if failure is None and not temp_close_ok:
            failure = TaskStoreErrorCode.IO_ERROR
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
        if result is None:
            raise TaskStoreError(TaskStoreErrorCode.IO_ERROR)
        return result
