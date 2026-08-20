"""Private short physical package caches for managed Windows installs.

Micromamba canonicalizes DOS-device and junction aliases back to their physical
paths.  Windows therefore uses a real short directory, created with a protected
DACL.  A small helper owns the cache lifetime so a killed installer cannot
strand several gigabytes of extracted packages.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import queue
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vibecad.runtime import paths
from vibecad.runtime.windows_job_runner import WindowsJobError, _base_python_launcher

_REVIEWED_MAXIMUM_RELATIVE_PATH = 198
_DEFAULT_MAXIMUM_ROOT_LENGTH = 40
_LEGACY_MAXIMUM_VISIBLE_PATH = 259
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_FILE_PERSISTENT_ACLS = 0x00000008
_DRIVE_FIXED = 3
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_ERROR_PATH_NOT_FOUND = 3
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_SE_FILE_OBJECT = 1
_CREATE_NO_WINDOW = 0x08000000
_MOVEFILE_WRITE_THROUGH = 0x00000008
_RECEIPT_SCHEMA = 1
_RECEIPT_STATE = "active"
_RECEIPT_CLEANING_STATE = "cleaning"
_RECEIPT_STATES = {_RECEIPT_STATE, _RECEIPT_CLEANING_STATE}
_TOKEN_MARKER = ".vibecad-cache-token"
_HELPER_HANDSHAKE_TIMEOUT_SECONDS = 30.0
_HELPER_CLEANUP_TIMEOUT_SECONDS = 300.0
_RECEIPT_KEYS = {
    "schema",
    "state",
    "root",
    "device",
    "inode",
    "security_sha256",
    "token",
    "helper_pid",
    "helper_created",
}
_BLOCKED_ENVIRONMENT_KEYS = {
    "CONDARC",
    "MAMBARC",
    "PYTHONHOME",
    "PYTHONPATH",
    "XDG_CACHE_HOME",
    "_CE_CONDA",
    "_CE_M",
}


class PackageCacheError(RuntimeError):
    """A private short package cache could not be established safely."""


class _CleanupRetryable(PackageCacheError):
    """An exact owned cache is temporarily busy during recursive cleanup."""


class _CacheBackend(Protocol):
    def candidate_parents(self) -> tuple[Path, ...]: ...

    def validate_parent(self, parent: Path) -> None: ...

    def create_private_directory(self, path: Path) -> bool: ...


class _SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    )


class _FileTime(ctypes.Structure):
    _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))

    def integer(self) -> int:
        return (int(self.high) << 32) | int(self.low)


def _entry_is_alias(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction is not None and is_junction())
    except OSError:
        return True


def _validate_real_directory(path: Path) -> tuple[int, int]:
    path = Path(os.path.abspath(path.expanduser()))
    if not path.is_absolute() or path == Path(path.anchor):
        raise PackageCacheError(f"package cache path is unsafe: {path}")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current /= part
            if _entry_is_alias(current):
                raise PackageCacheError(f"package cache path contains an alias: {path}")
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or _entry_is_alias(path):
            raise PackageCacheError(f"package cache is not a real directory: {path}")
        return info.st_dev, info.st_ino
    except PackageCacheError:
        raise
    except OSError as exc:
        raise PackageCacheError(f"package cache directory is unavailable: {path}") from exc


def _win_error(function: str) -> OSError:
    error = ctypes.get_last_error() or 1
    value = ctypes.WinError(error)
    value.add_note(function)
    return value


class _WindowsCacheBackend:
    """Small ctypes boundary for discovery, ACLs, and private creation."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise PackageCacheError("the Windows package cache is unavailable")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._userenv = ctypes.WinDLL("userenv", use_last_error=True)

        self._get_current_process = self._kernel32.GetCurrentProcess
        self._get_current_process.argtypes = ()
        self._get_current_process.restype = ctypes.c_void_p
        self._close_handle = self._kernel32.CloseHandle
        self._close_handle.argtypes = (ctypes.c_void_p,)
        self._close_handle.restype = ctypes.c_int
        self._open_process_token = self._advapi32.OpenProcessToken
        self._open_process_token.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._open_process_token.restype = ctypes.c_int
        self._get_token_information = self._advapi32.GetTokenInformation
        self._get_token_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        )
        self._get_token_information.restype = ctypes.c_int
        self._convert_sid = self._advapi32.ConvertSidToStringSidW
        self._convert_sid.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        )
        self._convert_sid.restype = ctypes.c_int
        self._convert_sddl = (
            self._advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        )
        self._convert_sddl.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        )
        self._convert_sddl.restype = ctypes.c_int
        self._convert_descriptor = (
            self._advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        )
        self._convert_descriptor.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(ctypes.c_uint32),
        )
        self._convert_descriptor.restype = ctypes.c_int
        self._get_named_security = self._advapi32.GetNamedSecurityInfoW
        self._get_named_security.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        )
        self._get_named_security.restype = ctypes.c_uint32
        self._local_free = self._kernel32.LocalFree
        self._local_free.argtypes = (ctypes.c_void_p,)
        self._local_free.restype = ctypes.c_void_p
        self._create_directory = self._kernel32.CreateDirectoryW
        self._create_directory.argtypes = (
            ctypes.c_wchar_p,
            ctypes.POINTER(_SecurityAttributes),
        )
        self._create_directory.restype = ctypes.c_int
        self._get_profile_directory = self._userenv.GetUserProfileDirectoryW
        self._get_profile_directory.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        )
        self._get_profile_directory.restype = ctypes.c_int
        self._get_windows_directory = self._kernel32.GetWindowsDirectoryW
        self._get_windows_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
        self._get_windows_directory.restype = ctypes.c_uint32
        self._get_volume_path_name = self._kernel32.GetVolumePathNameW
        self._get_volume_path_name.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        )
        self._get_volume_path_name.restype = ctypes.c_int
        self._get_drive_type = self._kernel32.GetDriveTypeW
        self._get_drive_type.argtypes = (ctypes.c_wchar_p,)
        self._get_drive_type.restype = ctypes.c_uint32
        self._get_volume_information = self._kernel32.GetVolumeInformationW
        self._get_volume_information.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_wchar_p,
            ctypes.c_uint32,
        )
        self._get_volume_information.restype = ctypes.c_int

        token = self._current_token()
        try:
            self._sid = self._current_sid(token)
            self._profile = self._profile_directory(token)
        finally:
            self._close_handle(token)
        self._windows = self._windows_directory()

    def _current_token(self) -> ctypes.c_void_p:
        token = ctypes.c_void_p()
        if not self._open_process_token(
            self._get_current_process(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _win_error("OpenProcessToken")
        return token

    def _current_sid(self, token: ctypes.c_void_p) -> str:
        needed = ctypes.c_uint32()
        ctypes.set_last_error(0)
        self._get_token_information(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if needed.value == 0:
            raise _win_error("GetTokenInformation(size)")
        buffer = ctypes.create_string_buffer(needed.value)
        if not self._get_token_information(
            token, _TOKEN_USER, buffer, needed.value, ctypes.byref(needed)
        ):
            raise _win_error("GetTokenInformation")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        rendered = ctypes.c_wchar_p()
        if not self._convert_sid(sid_pointer, ctypes.byref(rendered)):
            raise _win_error("ConvertSidToStringSidW")
        try:
            if not rendered.value:
                raise PackageCacheError("the current Windows SID is unavailable")
            return rendered.value
        finally:
            self._local_free(ctypes.cast(rendered, ctypes.c_void_p))

    def _profile_directory(self, token: ctypes.c_void_p) -> Path:
        size = ctypes.c_uint32(0)
        ctypes.set_last_error(0)
        self._get_profile_directory(token, None, ctypes.byref(size))
        if size.value == 0:
            raise _win_error("GetUserProfileDirectoryW(size)")
        buffer = ctypes.create_unicode_buffer(size.value)
        if not self._get_profile_directory(token, buffer, ctypes.byref(size)):
            raise _win_error("GetUserProfileDirectoryW")
        return Path(os.path.abspath(buffer.value))

    def _windows_directory(self) -> Path:
        size = 260
        while size <= 32768:
            buffer = ctypes.create_unicode_buffer(size)
            length = int(self._get_windows_directory(buffer, size))
            if length == 0:
                raise _win_error("GetWindowsDirectoryW")
            if length < size:
                return Path(os.path.abspath(buffer.value))
            size = length + 1
        raise PackageCacheError("the Windows directory path is too large")

    def candidate_parents(self) -> tuple[Path, ...]:
        candidates = (self._profile, self._windows / "Temp")
        ordered = sorted(candidates, key=lambda value: (len(str(value)), str(value)))
        return tuple(dict.fromkeys(ordered))

    def validate_parent(self, parent: Path) -> None:
        _validate_real_directory(parent)
        raw = os.path.abspath(parent)
        if raw.startswith("\\\\"):
            raise PackageCacheError("package cache parent must be on a local volume")
        volume = ctypes.create_unicode_buffer(32768)
        if not self._get_volume_path_name(raw, volume, len(volume)):
            raise _win_error("GetVolumePathNameW")
        if self._get_drive_type(volume.value) != _DRIVE_FIXED:
            raise PackageCacheError("package cache parent must be on a fixed local volume")
        flags = ctypes.c_uint32()
        if not self._get_volume_information(
            volume.value, None, 0, None, None, ctypes.byref(flags), None, 0
        ):
            raise _win_error("GetVolumeInformationW")
        if not (flags.value & _FILE_PERSISTENT_ACLS):
            raise PackageCacheError("package cache volume does not preserve ACLs")

    def create_private_directory(self, path: Path) -> bool:
        descriptor = ctypes.c_void_p()
        descriptor_size = ctypes.c_uint32()
        sddl = (
            "D:P"
            "(A;OICI;FA;;;SY)"
            "(A;OICI;FA;;;BA)"
            f"(A;OICI;FA;;;{self._sid})"
        )
        if not self._convert_sddl(
            sddl,
            _SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            raise _win_error("ConvertStringSecurityDescriptorToSecurityDescriptorW")
        attributes = _SecurityAttributes(
            nLength=ctypes.sizeof(_SecurityAttributes),
            lpSecurityDescriptor=descriptor,
            bInheritHandle=False,
        )
        try:
            ctypes.set_last_error(0)
            if self._create_directory(os.fspath(path), ctypes.byref(attributes)):
                return True
            error = ctypes.get_last_error()
            if error in {
                _ERROR_ACCESS_DENIED,
                _ERROR_PATH_NOT_FOUND,
                _ERROR_FILE_EXISTS,
                _ERROR_ALREADY_EXISTS,
            }:
                return False
            raise ctypes.WinError(error)
        finally:
            self._local_free(descriptor)

    def security_digest(self, path: Path) -> str:
        owner = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self._get_named_security(
            os.fspath(path),
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if result != 0:
            raise OSError(result, "GetNamedSecurityInfoW failed", os.fspath(path))
        rendered = ctypes.c_wchar_p()
        length = ctypes.c_uint32()
        try:
            if not self._convert_descriptor(
                descriptor,
                _SDDL_REVISION_1,
                _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                ctypes.byref(rendered),
                ctypes.byref(length),
            ):
                raise _win_error("ConvertSecurityDescriptorToStringSecurityDescriptorW")
            value = rendered.value or ""
            if "D:P" not in value or self._sid not in value:
                raise PackageCacheError("package cache protected DACL is unavailable")
            return hashlib.sha256(value.encode("utf-8")).hexdigest()
        finally:
            if rendered:
                self._local_free(ctypes.cast(rendered, ctypes.c_void_p))
            if descriptor:
                self._local_free(descriptor)


def _environment_is_blocked(name: str) -> bool:
    upper = name.upper()
    return (
        upper in _BLOCKED_ENVIRONMENT_KEYS
        or upper.startswith("CONDA_")
        or upper.startswith("MAMBA_")
    )


def _extended_path(path: Path) -> str:
    raw = os.path.abspath(path)
    if raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def _remove_owned_root(root: Path, identity: tuple[int, int]) -> None:
    if _validate_real_directory(root) != identity:
        raise PackageCacheError("package cache generation identity changed before cleanup")
    try:
        shutil.rmtree(_extended_path(root) if sys.platform == "win32" else root)
    except OSError as exc:
        raise _CleanupRetryable(f"package cache cleanup is temporarily blocked: {root}") from exc
    if os.path.lexists(root):
        raise _CleanupRetryable(f"package cache survived one cleanup attempt: {root}")


def _remove_owned_root_with_retry(
    root: Path,
    identity: tuple[int, int],
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    delay = 0.05
    while True:
        try:
            _remove_owned_root(root, identity)
            return
        except _CleanupRetryable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def _candidate_names() -> tuple[str, ...]:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    random = secrets.SystemRandom()
    singles = random.sample(tuple(alphabet), len(alphabet))
    doubles = [random.choice(alphabet) + random.choice(alphabet) for _ in range(128)]
    return tuple(dict.fromkeys((*singles, *doubles)))


def _acquire_root(
    backend: _CacheBackend,
    maximum_root_length: int,
    names: Sequence[str],
) -> tuple[Path, tuple[int, int]]:
    if maximum_root_length < 8:
        raise PackageCacheError("package cache path budget is invalid")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for parent in backend.candidate_parents():
        parent = Path(os.path.abspath(parent.expanduser()))
        try:
            backend.validate_parent(parent)
        except (OSError, PackageCacheError):
            continue
        for name in names:
            if not name or len(name) > 2 or any(character not in alphabet for character in name):
                raise PackageCacheError("package cache candidate name is invalid")
            root = parent / name
            if len(os.fspath(root)) > maximum_root_length:
                continue
            if not backend.create_private_directory(root):
                continue
            try:
                identity = _validate_real_directory(root)
                for child_name in ("tmp", "pip", "xdg"):
                    (root / child_name).mkdir(mode=0o700)
                    _validate_real_directory(root / child_name)
                if _validate_real_directory(root) != identity:
                    raise PackageCacheError("package cache generation changed during creation")
                return root, identity
            except BaseException:
                with contextlib.suppress(BaseException):
                    identity = _validate_real_directory(root)
                    _remove_owned_root_with_retry(root, identity, timeout=30.0)
                raise
    raise PackageCacheError(
        "no safe short physical package-cache directory is available for this Windows user"
    )


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))


def _validate_receipt_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _RECEIPT_KEYS:
        raise PackageCacheError("package-cache recovery record has an invalid shape")
    if type(payload.get("schema")) is not int or payload["schema"] != _RECEIPT_SCHEMA:
        raise PackageCacheError("package-cache recovery record schema is invalid")
    if payload.get("state") not in _RECEIPT_STATES:
        raise PackageCacheError("package-cache recovery state is invalid")
    if not isinstance(payload.get("root"), str):
        raise PackageCacheError("package-cache recovery root is invalid")
    for name in ("device", "inode", "helper_pid", "helper_created"):
        if type(payload.get(name)) is not int or int(payload[name]) <= 0:
            raise PackageCacheError(f"package-cache recovery {name} is invalid")
    for name, length in (("security_sha256", 64), ("token", 64)):
        value = payload.get(name)
        if (
            not isinstance(value, str)
            or len(value) != length
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise PackageCacheError(f"package-cache recovery {name} is invalid")
    return dict(payload)


def _read_receipt(record: Path) -> dict[str, object] | None:
    try:
        before = record.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > 4096
    ):
        raise PackageCacheError("package-cache recovery record is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(record, flags)
    except OSError as exc:
        raise PackageCacheError("package-cache recovery record cannot be opened") from exc
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, 4097)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        live = record.lstat()
    except OSError as exc:
        raise PackageCacheError("package-cache recovery record changed while reading") from exc
    identities = {
        (value.st_dev, value.st_ino, value.st_size, stat.S_IFMT(value.st_mode))
        for value in (before, opened, after, live)
    }
    if len(identities) != 1 or len(raw) != before.st_size:
        raise PackageCacheError("package-cache recovery record changed while reading")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PackageCacheError("package-cache recovery record is corrupt") from exc
    validated = _validate_receipt_payload(payload)
    if raw.decode("utf-8") != _canonical_json(validated):
        raise PackageCacheError("package-cache recovery record is not canonical")
    return validated


def _write_receipt(record: Path, payload: Mapping[str, object]) -> None:
    validated = _validate_receipt_payload(dict(payload))
    if os.path.lexists(record):
        raise PackageCacheError("a package-cache recovery record already exists")
    record.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical_json(validated).encode("utf-8")
    temporary = record.with_name(f"{record.name}.{validated['token']}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("package-cache receipt short write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _publish_receipt_no_replace(temporary, record)
    except OSError as exc:
        raise PackageCacheError("package-cache recovery record could not be published") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
    if _read_receipt(record) != validated:
        raise PackageCacheError("package-cache recovery record publication was not durable")


def _publish_receipt_no_replace(temporary: Path, record: Path) -> None:
    """Publish one receipt atomically without overwriting an existing generation."""

    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move.restype = ctypes.c_int
        ctypes.set_last_error(0)
        if not move(os.fspath(temporary), os.fspath(record), _MOVEFILE_WRITE_THROUGH):
            error = ctypes.get_last_error() or 1
            raise OSError(error, "MoveFileExW receipt publication failed", os.fspath(record))
        return
    # Test/non-Windows fallback retains no-replace semantics.  The production
    # Windows path above is a single write-through rename, so it has no nlink=2
    # crash window.
    os.link(temporary, record, follow_symlinks=False)
    temporary.unlink()


def _replace_matching_receipt(
    record: Path,
    expected: Mapping[str, object],
    replacement: Mapping[str, object],
) -> dict[str, object]:
    """Atomically advance one exact receipt without changing its generation."""

    old = _validate_receipt_payload(dict(expected))
    new = _validate_receipt_payload(dict(replacement))
    if any(new[name] != old[name] for name in _RECEIPT_KEYS - {"state"}):
        raise PackageCacheError("package-cache receipt transition changed generation")
    if _read_receipt(record) != old:
        raise PackageCacheError("package-cache recovery record generation changed")
    raw = _canonical_json(new).encode("utf-8")
    temporary = record.with_name(f"{record.name}.{old['token']}.transition.tmp")
    if os.path.lexists(temporary):
        try:
            stale = temporary.lstat()
        except OSError as exc:
            raise PackageCacheError(
                "package-cache stale transition state cannot be inspected"
            ) from exc
        if (
            not stat.S_ISREG(stale.st_mode)
            or stat.S_ISLNK(stale.st_mode)
            or stale.st_nlink != 1
            or stale.st_size > 4096
        ):
            raise PackageCacheError("package-cache stale transition state is unsafe")
        try:
            temporary.unlink()
        except OSError as exc:
            raise PackageCacheError(
                "package-cache stale transition state cannot be cleared"
            ) from exc
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(temporary, flags, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise OSError("package-cache transition short write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if _read_receipt(record) != old:
            raise PackageCacheError("package-cache recovery record changed before transition")
        os.replace(temporary, record)
    except PackageCacheError:
        raise
    except OSError as exc:
        raise PackageCacheError("package-cache recovery state could not be advanced") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            temporary.unlink()
    if _read_receipt(record) != new:
        raise PackageCacheError("package-cache recovery state transition was not durable")
    return new


def _clear_matching_receipt(record: Path, expected: Mapping[str, object]) -> None:
    current = _read_receipt(record)
    if current is None:
        return
    if current != dict(expected):
        raise PackageCacheError("package-cache recovery record generation changed")
    try:
        record.unlink()
    except OSError as exc:
        raise PackageCacheError("package-cache recovery record could not be cleared") from exc


def _write_token_marker(root: Path, token: str) -> None:
    marker = root / _TOKEN_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(marker, flags, 0o600)
    try:
        raw = token.encode("ascii")
        if os.write(descriptor, raw) != len(raw):
            raise OSError("package-cache marker short write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_token_marker(root: Path, token: str) -> None:
    marker = root / _TOKEN_MARKER
    try:
        info = marker.lstat()
        raw = marker.read_bytes()
        live = marker.lstat()
    except OSError as exc:
        raise PackageCacheError("package-cache ownership marker is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or (info.st_dev, info.st_ino, info.st_size)
        != (live.st_dev, live.st_ino, live.st_size)
        or raw != token.encode("ascii")
    ):
        raise PackageCacheError("package-cache ownership marker is invalid")


def _process_creation_time(pid: int) -> int | None:
    if sys.platform != "win32" or pid <= 0:
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    open_process.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    get_times = kernel32.GetProcessTimes
    get_times.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
        ctypes.POINTER(_FileTime),
    )
    get_times.restype = ctypes.c_int
    ctypes.set_last_error(0)
    handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return None
        raise PackageCacheError(
            f"package-cache helper process identity query failed: {error or 'unknown'}"
        )
    try:
        creation = _FileTime()
        exit_time = _FileTime()
        kernel = _FileTime()
        user = _FileTime()
        ctypes.set_last_error(0)
        if not get_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            error = ctypes.get_last_error()
            raise PackageCacheError(
                f"package-cache helper process times are unavailable: {error or 'unknown'}"
            )
        if exit_time.integer() != 0:
            return None
        return creation.integer()
    finally:
        close_handle(handle)


def _process_matches(pid: int, creation: int) -> bool:
    observed = _process_creation_time(pid)
    return observed is not None and observed == creation


def _validate_recovery_root(
    backend: _WindowsCacheBackend,
    root: Path,
    maximum_root_length: int,
) -> None:
    raw = os.path.abspath(root)
    if root != Path(raw) or len(raw) > maximum_root_length:
        raise PackageCacheError("package-cache recovery root is outside its path budget")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if (
        not root.name
        or len(root.name) > 2
        or any(character not in alphabet for character in root.name)
    ):
        raise PackageCacheError("package-cache recovery root name is invalid")
    parents = {Path(os.path.abspath(value)) for value in backend.candidate_parents()}
    if root.parent not in parents:
        raise PackageCacheError("package-cache recovery root is outside reviewed parents")
    backend.validate_parent(root.parent)


def _cleanup_payload(
    payload: Mapping[str, object],
    *,
    record: Path,
    backend: _WindowsCacheBackend,
    maximum_root_length: int,
) -> None:
    expected = _validate_receipt_payload(dict(payload))
    current = _read_receipt(record)
    if current != expected:
        raise PackageCacheError("package-cache recovery record generation changed")
    root = Path(str(expected["root"]))
    if not os.path.lexists(root):
        _clear_matching_receipt(record, expected)
        return
    _validate_recovery_root(backend, root, maximum_root_length)
    identity = (int(expected["device"]), int(expected["inode"]))
    if _validate_real_directory(root) != identity:
        raise PackageCacheError("package-cache recovery identity changed")
    if backend.security_digest(root) != expected["security_sha256"]:
        raise PackageCacheError("package-cache recovery security descriptor changed")
    if expected["state"] == _RECEIPT_STATE:
        _validate_token_marker(root, str(expected["token"]))
        cleaning = dict(expected)
        cleaning["state"] = _RECEIPT_CLEANING_STATE
        expected = _replace_matching_receipt(record, expected, cleaning)
    _remove_owned_root(root, identity)
    _clear_matching_receipt(record, expected)


def _same_receipt_generation(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    return all(left[name] == right[name] for name in _RECEIPT_KEYS - {"state"})


def _cleanup_payload_with_retry(
    payload: Mapping[str, object],
    *,
    record: Path,
    backend: _WindowsCacheBackend,
    maximum_root_length: int,
    timeout: float,
) -> None:
    """Converge a partially deleted exact generation while retaining its receipt."""

    original = _validate_receipt_payload(dict(payload))
    deadline = time.monotonic() + timeout
    delay = 0.05
    while True:
        current = _read_receipt(record)
        if current is None:
            if os.path.lexists(Path(str(original["root"]))):
                raise PackageCacheError("package-cache receipt vanished before cleanup")
            return
        if not _same_receipt_generation(original, current):
            raise PackageCacheError("package-cache recovery record generation changed")
        try:
            _cleanup_payload(
                current,
                record=record,
                backend=backend,
                maximum_root_length=maximum_root_length,
            )
            return
        except _CleanupRetryable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def recover_stale_package_cache(
    *,
    record_path: Path | None = None,
    maximum_root_length: int = _DEFAULT_MAXIMUM_ROOT_LENGTH,
    _backend: _WindowsCacheBackend | None = None,
    _process_matches_fn=None,
) -> bool:
    """Remove one exact dead-helper cache; never guess from a short directory name."""

    if sys.platform != "win32" and _backend is None:
        return False
    record = paths.package_cache_record() if record_path is None else Path(record_path)
    payload = _read_receipt(record)
    if payload is None:
        return False
    alive = _process_matches if _process_matches_fn is None else _process_matches_fn
    helper_active = alive(int(payload["helper_pid"]), int(payload["helper_created"]))
    if type(helper_active) is not bool:
        raise PackageCacheError("package-cache helper liveness is indeterminate")
    if helper_active:
        raise PackageCacheError("the previous package-cache cleanup helper is still active")
    try:
        backend = _WindowsCacheBackend() if _backend is None else _backend
        _cleanup_payload_with_retry(
            payload,
            record=record,
            backend=backend,
            maximum_root_length=maximum_root_length,
            timeout=30.0,
        )
    except PackageCacheError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise PackageCacheError("package-cache recovery failed") from exc
    return True


@dataclass
class PackageCacheSession:
    """One identity-bound short physical package cache."""

    root: Path
    temporary: Path
    pip: Path
    xdg: Path
    _identity: tuple[int, int] = field(repr=False)
    _maximum_root_length: int = field(repr=False)
    _security_sha256: str | None = field(default=None, repr=False)
    _security_digest_fn: object | None = field(default=None, repr=False)
    _record_path: Path | None = field(default=None, repr=False)
    _receipt: dict[str, object] | None = field(default=None, repr=False)
    _control: object | None = field(default=None, repr=False)
    _helper_process: object | None = field(default=None, repr=False)

    @property
    def packages(self) -> Path:
        return self.root

    def child_environment(
        self,
        base: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        source = os.environ if base is None else base
        environment = {
            str(name): str(value)
            for name, value in source.items()
            if not _environment_is_blocked(str(name))
        }
        environment.update(
            {
                "CONDA_PKGS_DIRS": os.fspath(self.root),
                "PIP_CACHE_DIR": os.fspath(self.pip),
                "XDG_CACHE_HOME": os.fspath(self.xdg),
                "TEMP": os.fspath(self.temporary),
                "TMP": os.fspath(self.temporary),
                "TMPDIR": os.fspath(self.temporary),
                "PYTHONNOUSERSITE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            }
        )
        return environment

    def validate(self) -> None:
        if len(os.fspath(self.root)) > self._maximum_root_length:
            raise PackageCacheError("package cache root exceeded its path budget")
        if _validate_real_directory(self.root) != self._identity:
            raise PackageCacheError("package cache generation identity changed")
        _validate_real_directory(self.temporary)
        _validate_real_directory(self.pip)
        _validate_real_directory(self.xdg)
        if self._security_sha256 is not None and self._security_digest_fn is not None:
            observed = self._security_digest_fn(self.root)  # type: ignore[operator]
            if observed != self._security_sha256:
                raise PackageCacheError("package cache security descriptor changed")
        if self._receipt is not None and self._record_path is not None:
            if _read_receipt(self._record_path) != self._receipt:
                raise PackageCacheError("package cache recovery record changed")
            if not _process_matches(
                int(self._receipt["helper_pid"]),
                int(self._receipt["helper_created"]),
            ):
                raise PackageCacheError("package cache cleanup helper exited early")


def _native_helper_command(record: Path, maximum_root_length: int) -> list[str]:
    try:
        launcher = _base_python_launcher()
    except WindowsJobError as exc:
        raise PackageCacheError(
            "package-cache cleanup helper base interpreter is unavailable"
        ) from exc
    import_root = Path(__file__).resolve(strict=True).parents[2]
    bootstrap = (
        "import runpy,sys;"
        "sys.path.insert(0,sys.argv.pop(1));"
        "runpy.run_module('vibecad.runtime.windows_package_cache',run_name='__main__')"
    )
    return [
        launcher,
        "-I",
        "-B",
        "-c",
        bootstrap,
        os.fspath(import_root),
        "--cleanup-helper",
        os.fspath(record),
        str(maximum_root_length),
    ]


def _native_helper_environment() -> dict[str, str]:
    environment = {
        str(name): str(value)
        for name, value in os.environ.items()
        if not _environment_is_blocked(str(name))
    }
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _close_helper_streams(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()


def _abort_helper(process: subprocess.Popen[str], timeout: float = 10.0) -> None:
    if process.stdin is not None:
        with contextlib.suppress(Exception):
            process.stdin.close()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)
    finally:
        _close_helper_streams(process)


def _reap_helper(process: subprocess.Popen[str], timeout: float = 1.0) -> str:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)
    detail = ""
    if process.stderr is not None:
        with contextlib.suppress(Exception):
            detail = process.stderr.read()[-1000:]
    _close_helper_streams(process)
    return detail


def _read_helper_handshake(
    process: subprocess.Popen[str],
    timeout: float = _HELPER_HANDSHAKE_TIMEOUT_SECONDS,
) -> str:
    if process.stdout is None:
        raise PackageCacheError("package-cache cleanup helper stdout is unavailable")
    result: queue.Queue[object] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put(process.stdout.readline())
        except BaseException as exc:  # noqa: BLE001 - transfer reader failure
            result.put(exc)

    reader = threading.Thread(
        target=read,
        name="vibecad-cache-helper-handshake",
        daemon=True,
    )
    reader.start()
    try:
        value = result.get(timeout=timeout)
    except queue.Empty:
        raise PackageCacheError("package-cache cleanup helper handshake timed out") from None
    if isinstance(value, BaseException):
        raise PackageCacheError("package-cache cleanup helper handshake failed") from value
    return str(value)


def _start_native_session(
    record: Path,
    maximum_root_length: int,
) -> PackageCacheSession:
    recover_stale_package_cache(
        record_path=record,
        maximum_root_length=maximum_root_length,
    )
    options: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "env": _native_helper_environment(),
    }
    if sys.platform == "win32":
        options["creationflags"] = _CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            _native_helper_command(record, maximum_root_length),
            **options,
        )
    except OSError as exc:
        raise PackageCacheError("package-cache cleanup helper could not be started") from exc
    try:
        if process.stdout is None or process.stdin is None:
            raise PackageCacheError("package-cache cleanup helper pipes are unavailable")
        handshake = _read_helper_handshake(process)
        if not handshake:
            detail = ""
            if process.poll() is not None and process.stderr is not None:
                detail = process.stderr.read()[-1000:]
            raise PackageCacheError(
                f"package-cache cleanup helper failed to start: {detail}"
            )
        try:
            payload = _validate_receipt_payload(json.loads(handshake))
        except (ValueError, PackageCacheError) as exc:
            raise PackageCacheError(
                "package-cache cleanup helper handshake is invalid"
            ) from exc
        if payload["state"] != _RECEIPT_STATE:
            raise PackageCacheError("package-cache cleanup helper did not publish active state")
        if _canonical_json(payload) != handshake.rstrip("\r\n"):
            raise PackageCacheError("package-cache cleanup helper handshake is not canonical")
        if _read_receipt(record) != payload:
            raise PackageCacheError(
                "package-cache cleanup helper receipt does not match handshake"
            )
        if not _process_matches(int(payload["helper_pid"]), int(payload["helper_created"])):
            raise PackageCacheError("package-cache cleanup helper identity is unavailable")
        backend = _WindowsCacheBackend()
        root = Path(str(payload["root"]))
        session = PackageCacheSession(
            root=root,
            temporary=root / "tmp",
            pip=root / "pip",
            xdg=root / "xdg",
            _identity=(int(payload["device"]), int(payload["inode"])),
            _maximum_root_length=maximum_root_length,
            _security_sha256=str(payload["security_sha256"]),
            _security_digest_fn=backend.security_digest,
            _record_path=record,
            _receipt=payload,
            _control=process.stdin,
            _helper_process=process,
        )
        session.validate()
        process.stdout.close()
        return session
    except BaseException as error:
        _abort_helper(process)
        try:
            recover_stale_package_cache(
                record_path=record,
                maximum_root_length=maximum_root_length,
            )
        except BaseException as cleanup_error:
            error.add_note(f"package-cache failed-start cleanup failed: {cleanup_error}")
        raise


def _release_native_session(session: PackageCacheSession, timeout: float = 300.0) -> None:
    control = session._control
    if control is None or session._receipt is None or session._record_path is None:
        raise PackageCacheError("package-cache cleanup helper control is unavailable")
    try:
        control.close()  # type: ignore[union-attr]
        session._control = None
    except OSError as exc:
        raise PackageCacheError("package-cache cleanup helper could not be released") from exc
    deadline = time.monotonic() + timeout
    pid = int(session._receipt["helper_pid"])
    created = int(session._receipt["helper_created"])
    process = session._helper_process
    while time.monotonic() < deadline:
        helper_active = _process_matches(pid, created)
        root_exists = os.path.lexists(session.root)
        record_exists = os.path.lexists(session._record_path)
        if not helper_active and not root_exists and not record_exists:
            if process is not None:
                _reap_helper(process)  # type: ignore[arg-type]
            return
        if not helper_active:
            detail = ""
            if process is not None:
                detail = _reap_helper(process)  # type: ignore[arg-type]
            raise PackageCacheError(
                f"package-cache cleanup helper exited before convergence: {detail}"
            )
        time.sleep(0.05)
    if process is not None:
        _abort_helper(process)  # type: ignore[arg-type]
    raise PackageCacheError("package-cache cleanup helper did not converge")


def _helper_payload(
    root: Path,
    identity: tuple[int, int],
    security_sha256: str,
    token: str,
) -> dict[str, object]:
    pid = os.getpid()
    created = _process_creation_time(pid)
    if created is None:
        raise PackageCacheError("package-cache helper process identity is unavailable")
    return {
        "schema": _RECEIPT_SCHEMA,
        "state": _RECEIPT_STATE,
        "root": os.fspath(root),
        "device": identity[0],
        "inode": identity[1],
        "security_sha256": security_sha256,
        "token": token,
        "helper_pid": pid,
        "helper_created": created,
    }


def _cleanup_helper(record: Path, maximum_root_length: int) -> int:
    backend = _WindowsCacheBackend()
    root: Path | None = None
    identity: tuple[int, int] | None = None
    payload: dict[str, object] | None = None
    receipt_published = False
    try:
        root, identity = _acquire_root(
            backend,
            maximum_root_length,
            _candidate_names(),
        )
        token = secrets.token_hex(32)
        _write_token_marker(root, token)
        security_sha256 = backend.security_digest(root)
        payload = _helper_payload(root, identity, security_sha256, token)
        _write_receipt(record, payload)
        receipt_published = True
        sys.stdout.write(_canonical_json(payload) + "\n")
        sys.stdout.flush()
        # EOF arrives both on normal parent release and after a hard parent exit.
        sys.stdin.buffer.read()
        _cleanup_payload_with_retry(
            payload,
            record=record,
            backend=backend,
            maximum_root_length=maximum_root_length,
            timeout=_HELPER_CLEANUP_TIMEOUT_SECONDS,
        )
        return 0
    except BaseException as exc:  # noqa: BLE001 - helper must report and converge
        if payload is not None and receipt_published:
            with contextlib.suppress(BaseException):
                _cleanup_payload_with_retry(
                    payload,
                    record=record,
                    backend=backend,
                    maximum_root_length=maximum_root_length,
                    timeout=_HELPER_CLEANUP_TIMEOUT_SECONDS,
                )
        elif root is not None and identity is not None:
            with contextlib.suppress(BaseException):
                _remove_owned_root_with_retry(
                    root,
                    identity,
                    timeout=_HELPER_CLEANUP_TIMEOUT_SECONDS,
                )
        sys.stderr.write(f"package-cache helper failed: {exc}\n")
        sys.stderr.flush()
        return 1


@contextmanager
def package_cache_session(
    *,
    maximum_root_length: int = _DEFAULT_MAXIMUM_ROOT_LENGTH,
    _backend: _CacheBackend | None = None,
    _names: Sequence[str] | None = None,
    _record_path: Path | None = None,
) -> Iterator[PackageCacheSession]:
    """Yield a transient cache and remove the exact generation on every exit."""

    if _backend is None:
        if sys.platform != "win32":
            raise PackageCacheError("the Windows package cache is unavailable")
        record = paths.package_cache_record() if _record_path is None else Path(_record_path)
        session = _start_native_session(record, maximum_root_length)
        try:
            session.validate()
            yield session
            session.validate()
        except BaseException as error:
            try:
                _release_native_session(session)
            except BaseException as cleanup_error:
                error.add_note(f"package-cache cleanup failed: {cleanup_error}")
            raise
        else:
            _release_native_session(session)
        return

    root, identity = _acquire_root(
        _backend,
        maximum_root_length,
        _candidate_names() if _names is None else _names,
    )
    session = PackageCacheSession(
        root=root,
        temporary=root / "tmp",
        pip=root / "pip",
        xdg=root / "xdg",
        _identity=identity,
        _maximum_root_length=maximum_root_length,
    )
    try:
        session.validate()
        yield session
        session.validate()
    except BaseException as error:
        try:
            _remove_owned_root(root, identity)
        except BaseException as cleanup_error:
            error.add_note(f"package-cache cleanup failed: {cleanup_error}")
        raise
    else:
        _remove_owned_root(root, identity)


def _main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 3 or arguments[0] != "--cleanup-helper":
        return 2
    try:
        maximum = int(arguments[2])
    except ValueError:
        return 2
    return _cleanup_helper(Path(arguments[1]), maximum)


__all__ = [
    "PackageCacheError",
    "PackageCacheSession",
    "package_cache_session",
    "recover_stale_package_cache",
]


if __name__ == "__main__":
    raise SystemExit(_main())
