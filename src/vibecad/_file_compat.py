"""Fail-closed cross-platform file descriptor primitives."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

LOCK_SH = 1
LOCK_EX = 2
LOCK_NB = 4
LOCK_UN = 8


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33
    _OBJECT_BASIC_INFORMATION_CLASS = 0
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _TOKEN_OWNER = 4
    _ADMINISTRATORS_SID = "S-1-5-32-544"
    _WIN_ACCOUNT_ADMINISTRATOR_SID = 38
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SDDL_REVISION_1 = 1
    _FILE_ATTRIBUTE_READONLY = 0x00000001
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_READ_ATTRIBUTES = 0x00000080
    _FILE_WRITE_ATTRIBUTES = 0x00000100
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _READ_CONTROL = 0x00020000
    _DELETE = 0x00010000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _CREATE_NEW = 1
    _OPEN_ALWAYS = 4
    _ERROR_ALREADY_EXISTS = 183
    _ERROR_INVALID_PARAMETER = 87
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _HANDLE_FLAG_INHERIT = 0x00000001
    _FILE_BASIC_INFO_CLASS = 0
    _FILE_ID_INFO_CLASS = 18
    _FILE_BEGIN = 0
    _FILE_CURRENT = 1
    _MOVEFILE_REPLACE_EXISTING = 0x00000001
    _MOVEFILE_WRITE_THROUGH = 0x00000008
    _FILE_DISPOSITION_INFO_CLASS = 4
    _FILE_DISPOSITION_INFO_EX_CLASS = 21
    _FILE_DISPOSITION_FLAG_DELETE = 0x00000001
    _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _WRITE_ACCESS = (
        0x0002  # FILE_WRITE_DATA
        | 0x0004  # FILE_APPEND_DATA
        | 0x0010  # FILE_WRITE_EA
        | 0x0100  # FILE_WRITE_ATTRIBUTES
        | 0x00010000  # DELETE
        | 0x00040000  # WRITE_DAC
        | 0x00080000  # WRITE_OWNER
        | 0x40000000  # GENERIC_WRITE
        | 0x10000000  # GENERIC_ALL
    )

    class _Overlapped(ctypes.Structure):
        _fields_ = (
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        )

    class _ObjectBasicInformation(ctypes.Structure):
        _fields_ = (
            ("Attributes", wintypes.ULONG),
            ("GrantedAccess", wintypes.ULONG),
            ("HandleCount", wintypes.ULONG),
            ("PointerCount", wintypes.ULONG),
            ("Reserved", wintypes.ULONG * 10),
        )

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    class _FileId128(ctypes.Structure):
        _fields_ = (("Identifier", ctypes.c_ubyte * 16),)

    class _FileIdInfo(ctypes.Structure):
        _fields_ = (
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        )

    class _FileBasicInfo(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        )

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = (("DeleteFile", wintypes.BOOL),)

    class _FileDispositionInfoEx(ctypes.Structure):
        _fields_ = (("Flags", wintypes.DWORD),)

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _lock_file_ex.restype = wintypes.BOOL
    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )
    _unlock_file_ex.restype = wintypes.BOOL
    _nt_query_object = _ntdll.NtQueryObject
    _nt_query_object.argtypes = (
        wintypes.HANDLE,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG),
    )
    _nt_query_object.restype = ctypes.c_long
    _get_current_process = _kernel32.GetCurrentProcess
    _get_current_process.argtypes = ()
    _get_current_process.restype = wintypes.HANDLE
    _open_process_token = _advapi32.OpenProcessToken
    _open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    _open_process_token.restype = wintypes.BOOL
    _get_token_information = _advapi32.GetTokenInformation
    _get_token_information.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _get_token_information.restype = wintypes.BOOL
    _convert_sid = _advapi32.ConvertSidToStringSidW
    _convert_sid.argtypes = (wintypes.LPVOID, ctypes.POINTER(ctypes.c_wchar_p))
    _convert_sid.restype = wintypes.BOOL
    _convert_string_sid = _advapi32.ConvertStringSidToSidW
    _convert_string_sid.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.LPVOID))
    _convert_string_sid.restype = wintypes.BOOL
    _is_well_known_sid = _advapi32.IsWellKnownSid
    _is_well_known_sid.argtypes = (wintypes.LPVOID, wintypes.DWORD)
    _is_well_known_sid.restype = wintypes.BOOL
    _get_named_security = _advapi32.GetNamedSecurityInfoW
    _get_named_security.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    _get_named_security.restype = wintypes.DWORD
    _convert_descriptor = _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    _convert_descriptor.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    _convert_descriptor.restype = wintypes.BOOL
    _convert_sddl = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    _convert_sddl.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    _convert_sddl.restype = wintypes.BOOL
    _set_file_security = _advapi32.SetFileSecurityW
    _set_file_security.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID)
    _set_file_security.restype = wintypes.BOOL
    _local_free = _kernel32.LocalFree
    _local_free.argtypes = (wintypes.LPVOID,)
    _local_free.restype = wintypes.LPVOID
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL
    _get_final_path = _kernel32.GetFinalPathNameByHandleW
    _get_final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    _get_final_path.restype = wintypes.DWORD
    _get_long_path_name = _kernel32.GetLongPathNameW
    _get_long_path_name.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    _get_long_path_name.restype = wintypes.DWORD
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE
    _create_directory = _kernel32.CreateDirectoryW
    _create_directory.argtypes = (
        wintypes.LPCWSTR,
        ctypes.POINTER(_SecurityAttributes),
    )
    _create_directory.restype = wintypes.BOOL
    _set_handle_information = _kernel32.SetHandleInformation
    _set_handle_information.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    _set_handle_information.restype = wintypes.BOOL
    _get_file_information = _kernel32.GetFileInformationByHandle
    _get_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    _get_file_information.restype = wintypes.BOOL
    _get_file_information_ex = _kernel32.GetFileInformationByHandleEx
    _get_file_information_ex.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _get_file_information_ex.restype = wintypes.BOOL
    _get_security = _advapi32.GetSecurityInfo
    _get_security.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    _get_security.restype = wintypes.DWORD
    _set_file_pointer = _kernel32.SetFilePointerEx
    _set_file_pointer.argtypes = (
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    )
    _set_file_pointer.restype = wintypes.BOOL
    _read_file = _kernel32.ReadFile
    _read_file.argtypes = (
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    _read_file.restype = wintypes.BOOL
    _move_file_ex = _kernel32.MoveFileExW
    _move_file_ex.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    )
    _move_file_ex.restype = wintypes.BOOL
    _set_file_information = _kernel32.SetFileInformationByHandle
    _set_file_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _set_file_information.restype = wintypes.BOOL


_CAPABILITY_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITY_VOLUME = re.compile(r"[0-9a-f]{16}\Z")
_CAPABILITY_FILE_ID = re.compile(r"[0-9a-f]{32}\Z")
_PREAD_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class WindowsPathCapability:
    """Serializable identity proof for one protected Windows filesystem object."""

    path: str
    volume: int
    file_id: int
    owner_sid: str
    security_sha256: str
    generation_token: str

    def to_mapping(self) -> dict[str, object]:
        return {
            # Win32 volume and FILE_ID_128 values exceed the canonical JSON
            # safe-integer range.  Fixed-width lowercase hex is lossless and
            # has exactly one accepted wire representation.
            "file_id": f"{self.file_id:032x}",
            "generation_token": self.generation_token,
            "owner_sid": self.owner_sid,
            "path": self.path,
            "schema_version": 1,
            "security_sha256": self.security_sha256,
            "volume": f"{self.volume:016x}",
        }

    @classmethod
    def from_mapping(cls, value: object) -> WindowsPathCapability:
        fields = {
            "file_id",
            "generation_token",
            "owner_sid",
            "path",
            "schema_version",
            "security_sha256",
            "volume",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or value.get("schema_version") != 1
            or type(value.get("path")) is not str
            or type(value.get("owner_sid")) is not str
            or type(value.get("volume")) is not str
            or type(value.get("file_id")) is not str
            or type(value.get("security_sha256")) is not str
            or type(value.get("generation_token")) is not str
            or _CAPABILITY_TOKEN.fullmatch(value["security_sha256"]) is None
            or _CAPABILITY_TOKEN.fullmatch(value["generation_token"]) is None
            or _CAPABILITY_VOLUME.fullmatch(value["volume"]) is None
            or _CAPABILITY_FILE_ID.fullmatch(value["file_id"]) is None
        ):
            raise ValueError("invalid Windows path capability")
        return cls(
            path=value["path"],
            volume=int(value["volume"], 16),
            file_id=int(value["file_id"], 16),
            owner_sid=value["owner_sid"],
            security_sha256=value["security_sha256"],
            generation_token=value["generation_token"],
        )


def _windows_handle(fd: int) -> int:
    try:
        handle = msvcrt.get_osfhandle(fd)  # type: ignore[name-defined]
    except (OSError, ValueError):
        raise OSError(errno.EBADF, "invalid file descriptor") from None
    if handle == -1:
        raise OSError(errno.EBADF, "invalid file descriptor")
    return handle


def flock(fd: int, operation: int) -> None:
    """Apply BSD-style shared/exclusive whole-file locking."""

    if sys.platform != "win32":
        import fcntl

        fcntl.flock(fd, operation)
        return
    handle = _windows_handle(fd)
    overlapped = _Overlapped()  # type: ignore[name-defined]
    if operation == LOCK_UN:
        if not _unlock_file_ex(  # type: ignore[name-defined]
            handle,
            0,
            0xFFFFFFFF,
            0xFFFFFFFF,
            ctypes.byref(overlapped),  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        return
    if operation not in {LOCK_SH, LOCK_SH | LOCK_NB, LOCK_EX, LOCK_EX | LOCK_NB}:
        raise OSError(errno.ENOTSUP, "unsupported lock operation")
    flags = 0
    if operation & LOCK_EX:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK  # type: ignore[name-defined]
    if operation & LOCK_NB:
        flags |= _LOCKFILE_FAIL_IMMEDIATELY  # type: ignore[name-defined]
    if _lock_file_ex(  # type: ignore[name-defined]
        handle,
        flags,
        0,
        0xFFFFFFFF,
        0xFFFFFFFF,
        ctypes.byref(overlapped),  # type: ignore[name-defined]
    ):
        return
    error = ctypes.get_last_error()  # type: ignore[name-defined]
    if error == _ERROR_LOCK_VIOLATION:  # type: ignore[name-defined]
        raise BlockingIOError(errno.EWOULDBLOCK, "file is already locked")
    raise ctypes.WinError(error)  # type: ignore[name-defined]


def require_read_only(fd: int) -> None:
    """Reject a descriptor carrying any mutation authority."""

    if sys.platform != "win32":
        import fcntl

        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        except OSError:
            raise OSError(errno.EBADF, "cannot inspect file descriptor") from None
        if flags & os.O_ACCMODE != os.O_RDONLY:
            raise OSError(errno.EACCES, "file descriptor is not read-only")
        return
    handle = _windows_handle(fd)
    information = _ObjectBasicInformation()  # type: ignore[name-defined]
    returned = wintypes.ULONG()  # type: ignore[name-defined]
    status = _nt_query_object(  # type: ignore[name-defined]
        handle,
        _OBJECT_BASIC_INFORMATION_CLASS,  # type: ignore[name-defined]
        ctypes.byref(information),  # type: ignore[name-defined]
        ctypes.sizeof(information),  # type: ignore[name-defined]
        ctypes.byref(returned),  # type: ignore[name-defined]
    )
    if status < 0:
        raise OSError(errno.EACCES, "cannot inspect file descriptor access")
    if information.GrantedAccess & _WRITE_ACCESS:  # type: ignore[name-defined]
        raise OSError(errno.EACCES, "file descriptor is not read-only")


def pread(fd: int, length: int, offset: int) -> bytes:
    """Read at an offset while restoring the CRT file pointer on Windows."""

    if sys.platform != "win32":
        return os.pread(fd, length, offset)
    if type(length) is not int or type(offset) is not int or length < 0 or offset < 0:
        raise OSError(errno.EINVAL, "invalid positional read")
    # A duplicated Windows disk handle shares its file pointer.  Use ReadFile
    # directly (rather than CRT os.read, whose text mode treats 0x1a as EOF),
    # serialize our own positional reads, and restore the pointer.  A foreign
    # concurrent pointer change can only make the caller's later digest check
    # fail closed.
    handle = _windows_handle(fd)
    with _PREAD_LOCK:
        original = ctypes.c_longlong()  # type: ignore[name-defined]
        if not _set_file_pointer(  # type: ignore[name-defined]
            handle,
            0,
            ctypes.byref(original),  # type: ignore[name-defined]
            _FILE_CURRENT,  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        try:
            if not _set_file_pointer(  # type: ignore[name-defined]
                handle,
                offset,
                None,
                _FILE_BEGIN,  # type: ignore[name-defined]
            ):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
            if length == 0:
                return b""
            buffer = ctypes.create_string_buffer(length)  # type: ignore[name-defined]
            read = wintypes.DWORD()  # type: ignore[name-defined]
            if not _read_file(  # type: ignore[name-defined]
                handle,
                buffer,
                length,
                ctypes.byref(read),  # type: ignore[name-defined]
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
            return buffer.raw[: read.value]
        finally:
            if not _set_file_pointer(  # type: ignore[name-defined]
                handle,
                original.value,
                None,
                _FILE_BEGIN,  # type: ignore[name-defined]
            ):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]


def _current_process_token_sid(information_class: int) -> str:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows SID is unavailable")
    token = wintypes.HANDLE()  # type: ignore[name-defined]
    if not _open_process_token(  # type: ignore[name-defined]
        _get_current_process(),
        _TOKEN_QUERY,
        ctypes.byref(token),  # type: ignore[name-defined]
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    try:
        needed = wintypes.DWORD()  # type: ignore[name-defined]
        ctypes.set_last_error(0)  # type: ignore[name-defined]
        _get_token_information(  # type: ignore[name-defined]
            token,
            information_class,
            None,
            0,
            ctypes.byref(needed),  # type: ignore[name-defined]
        )
        if not needed.value:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        buffer = ctypes.create_string_buffer(needed.value)  # type: ignore[name-defined]
        if not _get_token_information(  # type: ignore[name-defined]
            token,
            information_class,
            buffer,
            needed,
            ctypes.byref(needed),  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]  # type: ignore[name-defined]
        rendered = ctypes.c_wchar_p()  # type: ignore[name-defined]
        if not _convert_sid(sid_pointer, ctypes.byref(rendered)):  # type: ignore[name-defined]
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        try:
            if not rendered.value:
                raise OSError(errno.EACCES, "current Windows SID is unavailable")
            return rendered.value
        finally:
            _local_free(ctypes.cast(rendered, wintypes.LPVOID))  # type: ignore[name-defined]
    finally:
        _close_handle(token)  # type: ignore[name-defined]


def current_user_sid() -> str:
    """Return the effective Windows token SID, never a fabricated POSIX uid."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows SID is unavailable")
    return _current_process_token_sid(_TOKEN_USER)  # type: ignore[name-defined]


def _current_default_owner_sid() -> str:
    """Return the owner SID Windows assigns to objects created by this token."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows SID is unavailable")
    return _current_process_token_sid(_TOKEN_OWNER)  # type: ignore[name-defined]


def _trusted_windows_owner_sids() -> frozenset[str]:
    """Return owners accepted for current-token private objects.

    UAC-disabled Administrator tokens, including GitHub's hosted Windows
    runner, use the built-in Administrators group as their default object
    owner.  That group already has an explicit full-control ACE in every
    VibeCAD private DACL.  Accept it only when it is this token's actual
    default owner; standard-user tokens continue to require their user SID.
    """

    user = current_user_sid()
    default_owner = _current_default_owner_sid()
    if default_owner == _ADMINISTRATORS_SID:  # type: ignore[name-defined]
        return frozenset((user, default_owner))
    return frozenset((user,))


def _sid_is_well_known(sid: str, sid_type: int) -> bool:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows SID is unavailable")
    pointer = wintypes.LPVOID()  # type: ignore[name-defined]
    if not _convert_string_sid(sid, ctypes.byref(pointer)):  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    try:
        return bool(_is_well_known_sid(pointer, sid_type))  # type: ignore[name-defined]
    finally:
        _local_free(pointer)  # type: ignore[name-defined]


def _windows_security(path: Path) -> tuple[str, str]:
    owner = wintypes.LPVOID()  # type: ignore[name-defined]
    descriptor = wintypes.LPVOID()  # type: ignore[name-defined]
    result = _get_named_security(  # type: ignore[name-defined]
        windows_extended_path(path),
        _SE_FILE_OBJECT,  # type: ignore[name-defined]
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,  # type: ignore[name-defined]
        ctypes.byref(owner),  # type: ignore[name-defined]
        None,
        None,
        None,
        ctypes.byref(descriptor),  # type: ignore[name-defined]
    )
    if result:
        raise OSError(result, "GetNamedSecurityInfoW failed", os.fspath(path))
    owner_text = ctypes.c_wchar_p()  # type: ignore[name-defined]
    rendered = ctypes.c_wchar_p()  # type: ignore[name-defined]
    length = wintypes.DWORD()  # type: ignore[name-defined]
    try:
        if not _convert_sid(owner, ctypes.byref(owner_text)):  # type: ignore[name-defined]
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        if not _convert_descriptor(  # type: ignore[name-defined]
            descriptor,
            _SDDL_REVISION_1,  # type: ignore[name-defined]
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,  # type: ignore[name-defined]
            ctypes.byref(rendered),  # type: ignore[name-defined]
            ctypes.byref(length),  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        if not owner_text.value or not rendered.value:
            raise OSError(errno.EACCES, "Windows security descriptor is unavailable")
        return owner_text.value, rendered.value
    finally:
        if owner_text:
            _local_free(ctypes.cast(owner_text, wintypes.LPVOID))  # type: ignore[name-defined]
        if rendered:
            _local_free(ctypes.cast(rendered, wintypes.LPVOID))  # type: ignore[name-defined]
        if descriptor:
            _local_free(descriptor)  # type: ignore[name-defined]


def _without_windows_namespace_prefix(raw: str) -> str:
    if raw.startswith("\\\\?\\UNC\\"):
        return "\\\\" + raw[8:]
    if raw.startswith("\\\\?\\"):
        return raw[4:]
    return raw


def _windows_long_path(path: Path) -> Path:
    """Expand DOS 8.3 components without following a reparse-point target."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows long paths are unavailable")
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows path must be normalized and absolute")
    source = windows_extended_path(absolute)
    size = 512
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)  # type: ignore[name-defined]
        length = int(_get_long_path_name(source, buffer, size))  # type: ignore[name-defined]
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        if length < size:
            return Path(os.path.abspath(_without_windows_namespace_prefix(buffer.value)))
        size = length + 1
    raise OSError(errno.ENAMETOOLONG, "Windows long path is too long")


def _windows_handle_path(handle: int) -> Path:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows handle paths are unavailable")
    if type(handle) is not int or handle in {0, -1, _INVALID_HANDLE_VALUE}:
        raise OSError(errno.EBADF, "invalid Windows handle")
    size = 512
    while size <= 32768:
        buffer = ctypes.create_unicode_buffer(size)  # type: ignore[name-defined]
        length = int(_get_final_path(handle, buffer, size, 0))  # type: ignore[name-defined]
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        if length < size:
            return Path(os.path.abspath(_without_windows_namespace_prefix(buffer.value)))
        size = length + 1
    raise OSError(errno.ENAMETOOLONG, "Windows handle path is too long")


def _windows_handle_security(handle: int) -> tuple[str, str]:
    owner = wintypes.LPVOID()  # type: ignore[name-defined]
    descriptor = wintypes.LPVOID()  # type: ignore[name-defined]
    result = _get_security(  # type: ignore[name-defined]
        handle,
        _SE_FILE_OBJECT,  # type: ignore[name-defined]
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,  # type: ignore[name-defined]
        ctypes.byref(owner),  # type: ignore[name-defined]
        None,
        None,
        None,
        ctypes.byref(descriptor),  # type: ignore[name-defined]
    )
    if result:
        raise OSError(result, "GetSecurityInfo failed")
    owner_text = ctypes.c_wchar_p()  # type: ignore[name-defined]
    rendered = ctypes.c_wchar_p()  # type: ignore[name-defined]
    length = wintypes.DWORD()  # type: ignore[name-defined]
    try:
        if not _convert_sid(owner, ctypes.byref(owner_text)):  # type: ignore[name-defined]
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        if not _convert_descriptor(  # type: ignore[name-defined]
            descriptor,
            _SDDL_REVISION_1,  # type: ignore[name-defined]
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,  # type: ignore[name-defined]
            ctypes.byref(rendered),  # type: ignore[name-defined]
            ctypes.byref(length),  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        if not owner_text.value or not rendered.value:
            raise OSError(errno.EACCES, "Windows security descriptor is unavailable")
        return owner_text.value, rendered.value
    finally:
        if owner_text:
            _local_free(ctypes.cast(owner_text, wintypes.LPVOID))  # type: ignore[name-defined]
        if rendered:
            _local_free(ctypes.cast(rendered, wintypes.LPVOID))  # type: ignore[name-defined]
        if descriptor:
            _local_free(descriptor)  # type: ignore[name-defined]


def _validate_windows_security(owner: str, sddl: str) -> None:
    sid = current_user_sid()
    if owner not in _trusted_windows_owner_sids() or "D:P" not in sddl:
        raise OSError(errno.EACCES, "Windows capability DACL is not protected")
    # An allow ACE for any principal other than the current user, LocalSystem,
    # or Administrators would make replacement possible across trust domains.
    allowed_trustees = {sid, "OW", "SY", "BA"}
    if _sid_is_well_known(
        sid,
        _WIN_ACCOUNT_ADMINISTRATOR_SID,  # type: ignore[name-defined]
    ):
        # ConvertSecurityDescriptorToStringSecurityDescriptorW renders the
        # built-in local Administrator account as the canonical ``LA`` alias.
        allowed_trustees.add("LA")
    for ace in re.findall(r"\(([^()]*)\)", sddl):
        fields = ace.split(";")
        if len(fields) == 6 and fields[0] in {"A", "OA"} and fields[5] not in allowed_trustees:
            raise OSError(errno.EACCES, "Windows capability DACL grants foreign access")


def _windows_handle_information(handle: int, *, directory: bool) -> tuple[int, int]:
    information = _ByHandleFileInformation()  # type: ignore[name-defined]
    if not _get_file_information(handle, ctypes.byref(information)):  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    attributes = int(information.dwFileAttributes)
    is_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)  # type: ignore[name-defined]
    if (
        bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)  # type: ignore[name-defined]
        or is_directory != directory
        or (not directory and int(information.nNumberOfLinks) != 1)
    ):
        raise OSError(errno.EACCES, "Windows capability object is unsafe")
    identity = _FileIdInfo()  # type: ignore[name-defined]
    if not _get_file_information_ex(  # type: ignore[name-defined]
        handle,
        _FILE_ID_INFO_CLASS,  # type: ignore[name-defined]
        ctypes.byref(identity),  # type: ignore[name-defined]
        ctypes.sizeof(identity),  # type: ignore[name-defined]
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    file_id = int.from_bytes(bytes(identity.FileId.Identifier), "little")
    return int(identity.VolumeSerialNumber), file_id


def _capture_windows_handle(
    handle: int,
    path: Path,
    *,
    directory: bool,
    generation_token: str | None,
) -> WindowsPathCapability:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows handle validation is unavailable")
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows capability path must be normalized and absolute")
    canonical = _windows_long_path(absolute)
    opened_path = _windows_handle_path(handle)
    if os.path.normcase(os.fspath(opened_path)) != os.path.normcase(os.fspath(canonical)):
        raise OSError(errno.EACCES, "Windows handle resolves to another path")
    volume, file_id = _windows_handle_information(handle, directory=directory)
    owner, sddl = _windows_handle_security(handle)
    _validate_windows_security(owner, sddl)
    token = secrets.token_hex(32) if generation_token is None else generation_token
    if type(token) is not str or _CAPABILITY_TOKEN.fullmatch(token) is None:
        raise ValueError("invalid Windows capability generation token")
    return WindowsPathCapability(
        path=os.fspath(canonical),
        volume=volume,
        file_id=file_id,
        owner_sid=owner,
        security_sha256=hashlib.sha256(sddl.encode("utf-8")).hexdigest(),
        generation_token=token,
    )


def _open_windows_path_handle(
    path: Path,
    *,
    inheritable: bool,
    deny_delete: bool,
    write_attributes: bool = False,
) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows handle path must be normalized and absolute")
    attributes = _SecurityAttributes(  # type: ignore[name-defined]
        ctypes.sizeof(_SecurityAttributes),  # type: ignore[name-defined]
        None,
        bool(inheritable),
    )
    share = _FILE_SHARE_READ | _FILE_SHARE_WRITE  # type: ignore[name-defined]
    if not deny_delete:
        share |= _FILE_SHARE_DELETE  # type: ignore[name-defined]
    access = _FILE_READ_ATTRIBUTES | _READ_CONTROL  # type: ignore[name-defined]
    if write_attributes:
        access |= _FILE_WRITE_ATTRIBUTES  # type: ignore[name-defined]
    handle = _create_file(  # type: ignore[name-defined]
        windows_extended_path(absolute),
        access,
        share,
        ctypes.byref(attributes),  # type: ignore[name-defined]
        _OPEN_EXISTING,  # type: ignore[name-defined]
        _FILE_FLAG_BACKUP_SEMANTICS  # type: ignore[name-defined]
        | _FILE_FLAG_OPEN_REPARSE_POINT,  # type: ignore[name-defined]
        None,
    )
    if handle in {None, _INVALID_HANDLE_VALUE}:  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    return int(handle)


def _open_windows_file_mutation_handle(
    path: Path,
    *,
    delete_access: bool,
    directory: bool = False,
) -> int:
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows handle path must be normalized and absolute")
    attributes = _SecurityAttributes(  # type: ignore[name-defined]
        ctypes.sizeof(_SecurityAttributes),  # type: ignore[name-defined]
        None,
        False,
    )
    access = _FILE_READ_ATTRIBUTES | _READ_CONTROL  # type: ignore[name-defined]
    if delete_access:
        access |= _DELETE  # type: ignore[name-defined]
    handle = _create_file(  # type: ignore[name-defined]
        windows_extended_path(absolute),
        access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,  # type: ignore[name-defined]
        ctypes.byref(attributes),  # type: ignore[name-defined]
        _OPEN_EXISTING,  # type: ignore[name-defined]
        _FILE_FLAG_OPEN_REPARSE_POINT  # type: ignore[name-defined]
        | (_FILE_FLAG_BACKUP_SEMANTICS if directory else 0),  # type: ignore[name-defined]
        None,
    )
    if handle in {None, _INVALID_HANDLE_VALUE}:  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    return int(handle)


def set_private_dacl(path: Path) -> None:
    """Protect an existing Windows object for this user, SYSTEM and Administrators."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows DACL is unavailable")
    resolved = Path(os.path.abspath(path))
    sid = current_user_sid()
    sddl = f"D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid})"
    descriptor = wintypes.LPVOID()  # type: ignore[name-defined]
    size = wintypes.DWORD()  # type: ignore[name-defined]
    if not _convert_sddl(  # type: ignore[name-defined]
        sddl,
        _SDDL_REVISION_1,  # type: ignore[name-defined]
        ctypes.byref(descriptor),  # type: ignore[name-defined]
        ctypes.byref(size),  # type: ignore[name-defined]
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    try:
        if not _set_file_security(  # type: ignore[name-defined]
            windows_extended_path(resolved),
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,  # type: ignore[name-defined]
            descriptor,
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    finally:
        _local_free(descriptor)  # type: ignore[name-defined]


def protect_windows_path(path: Path, *, directory: bool) -> WindowsPathCapability:
    """Adopt one current-user Windows object into the protected-DACL boundary.

    The existing generation is held through a delete-denying native HANDLE while
    its owner, type, path and File ID are captured.  The DACL rewrite therefore
    cannot be redirected to a replacement name.  This is the migration path for
    VibeCAD directories created by releases that predate native Windows ACLs.
    """

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows DACL protection is unavailable")
    if type(directory) is not bool:
        raise TypeError("directory must be bool")
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows protection path must be normalized and absolute")
    handle = _open_windows_path_handle(
        absolute,
        inheritable=False,
        deny_delete=True,
    )
    try:
        opened_path = _windows_handle_path(handle)
        if os.path.normcase(os.fspath(opened_path)) != os.path.normcase(os.fspath(absolute)):
            raise OSError(errno.EACCES, "Windows protection handle resolves elsewhere")
        volume, file_id = _windows_handle_information(handle, directory=directory)
        owner, _sddl = _windows_handle_security(handle)
        if owner not in _trusted_windows_owner_sids():
            raise OSError(errno.EACCES, "Windows protection target has another owner")
        set_private_dacl(absolute)
        protected = _capture_windows_handle(
            handle,
            absolute,
            directory=directory,
            generation_token=None,
        )
        if (protected.volume, protected.file_id) != (volume, file_id):
            raise OSError(errno.EACCES, "Windows protection target identity changed")
        current = capture_windows_path(
            absolute,
            directory=directory,
            generation_token=protected.generation_token,
        )
        if current != protected:
            raise OSError(errno.EACCES, "Windows protected generation changed")
        return protected
    finally:
        close_windows_handle(handle)


def clear_windows_readonly(
    path: Path,
    *,
    expected: WindowsPathCapability,
) -> WindowsPathCapability:
    """Clear READONLY on one exact protected file through a pinned HANDLE."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows file attributes are unavailable")
    if type(expected) is not WindowsPathCapability:
        raise TypeError("expected must be a WindowsPathCapability")
    absolute = Path(os.path.abspath(path))
    if absolute != path or os.path.normcase(os.fspath(absolute)) != os.path.normcase(expected.path):
        raise OSError(errno.EINVAL, "Windows attribute path must match its capability")
    handle = _open_windows_path_handle(
        absolute,
        inheritable=False,
        deny_delete=True,
        write_attributes=True,
    )
    try:
        opened = _capture_windows_handle(
            handle,
            absolute,
            directory=False,
            generation_token=expected.generation_token,
        )
        if opened != expected:
            raise OSError(errno.EACCES, "Windows attribute target identity changed")
        basic = _FileBasicInfo()  # type: ignore[name-defined]
        if not _get_file_information_ex(  # type: ignore[name-defined]
            handle,
            _FILE_BASIC_INFO_CLASS,  # type: ignore[name-defined]
            ctypes.byref(basic),  # type: ignore[name-defined]
            ctypes.sizeof(basic),  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        attributes = int(basic.FileAttributes)
        if attributes & (  # type: ignore[name-defined]
            _FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError(errno.EACCES, "Windows attribute target is not a regular file")
        if attributes & _FILE_ATTRIBUTE_READONLY:  # type: ignore[name-defined]
            writable_attributes = attributes & ~_FILE_ATTRIBUTE_READONLY  # type: ignore[name-defined]
            if writable_attributes == 0:
                writable_attributes = _FILE_ATTRIBUTE_NORMAL  # type: ignore[name-defined]
            basic.FileAttributes = writable_attributes
            if not _set_file_information(  # type: ignore[name-defined]
                handle,
                _FILE_BASIC_INFO_CLASS,  # type: ignore[name-defined]
                ctypes.byref(basic),  # type: ignore[name-defined]
                ctypes.sizeof(basic),  # type: ignore[name-defined]
            ):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        current = _capture_windows_handle(
            handle,
            absolute,
            directory=False,
            generation_token=expected.generation_token,
        )
        if current != expected:
            raise OSError(errno.EACCES, "Windows attribute target identity changed")
        verified = _FileBasicInfo()  # type: ignore[name-defined]
        if not _get_file_information_ex(  # type: ignore[name-defined]
            handle,
            _FILE_BASIC_INFO_CLASS,  # type: ignore[name-defined]
            ctypes.byref(verified),  # type: ignore[name-defined]
            ctypes.sizeof(verified),  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        if int(verified.FileAttributes) & _FILE_ATTRIBUTE_READONLY:  # type: ignore[name-defined]
            raise OSError(errno.EACCES, "Windows file remains read-only")
        return current
    finally:
        close_windows_handle(handle)


def _private_windows_security_descriptor() -> wintypes.LPVOID:
    sid = current_user_sid()
    default_owner = _current_default_owner_sid()
    owner = default_owner if default_owner == _ADMINISTRATORS_SID else sid
    sddl = f"O:{owner}D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;{sid})"
    descriptor = wintypes.LPVOID()  # type: ignore[name-defined]
    size = wintypes.DWORD()  # type: ignore[name-defined]
    if not _convert_sddl(  # type: ignore[name-defined]
        sddl,
        _SDDL_REVISION_1,  # type: ignore[name-defined]
        ctypes.byref(descriptor),  # type: ignore[name-defined]
        ctypes.byref(size),  # type: ignore[name-defined]
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    return descriptor


def _validate_private_windows_path(
    path: Path, *, directory: bool
) -> tuple[os.stat_result, str, str]:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows path validation is unavailable")
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows capability path must be normalized and absolute")
    value = os.lstat(windows_extended_path(absolute))
    attributes = int(getattr(value, "st_file_attributes", 0))
    if (
        bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)  # type: ignore[name-defined]
        or stat.S_ISLNK(value.st_mode)
        or (directory and not stat.S_ISDIR(value.st_mode))
        or (not directory and (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1))
    ):
        raise OSError(errno.EACCES, "Windows capability object is unsafe")
    owner, sddl = _windows_security(absolute)
    _validate_windows_security(owner, sddl)
    return value, owner, sddl


def windows_extended_path(path: Path) -> str:
    """Return an absolute Win32 verbatim path without changing capability identity."""

    raw = os.path.abspath(path)
    if sys.platform != "win32" or raw.startswith("\\\\?\\"):
        return raw
    if raw.startswith("\\\\"):
        return "\\\\?\\UNC\\" + raw[2:]
    return "\\\\?\\" + raw


def capture_windows_path(
    path: Path,
    *,
    directory: bool,
    generation_token: str | None = None,
) -> WindowsPathCapability:
    """Capture a reparse-free, private Windows object as a wire-safe capability."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows path validation is unavailable")
    handle = _open_windows_path_handle(path, inheritable=False, deny_delete=True)
    try:
        return _capture_windows_handle(
            handle,
            path,
            directory=directory,
            generation_token=generation_token,
        )
    finally:
        close_windows_handle(handle)


def windows_fd_path(fd: int) -> Path:
    """Resolve a CRT descriptor through its kernel handle without using a supplied path."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows handle paths are unavailable")
    return _windows_handle_path(_windows_handle(fd))


def capture_windows_fd(
    fd: int,
    *,
    directory: bool,
    generation_token: str | None = None,
) -> WindowsPathCapability:
    """Capture identity and DACL through an already-open Windows descriptor."""

    handle = _windows_handle(fd)
    path = _windows_handle_path(handle)
    return validate_windows_handle_path(
        handle,
        path,
        directory=directory,
        generation_token=generation_token,
    )


def validate_windows_path(
    capability: WindowsPathCapability,
    *,
    directory: bool,
) -> Path:
    """Reopen and compare every security/identity field of a capability."""

    if type(capability) is not WindowsPathCapability:
        raise TypeError("capability must be a WindowsPathCapability")
    path = Path(capability.path)
    current = capture_windows_path(
        path,
        directory=directory,
        generation_token=capability.generation_token,
    )
    if current != capability:
        raise OSError(errno.EACCES, "Windows capability identity changed")
    return path


def open_windows_directory_handle(
    path: Path,
    *,
    inheritable: bool = False,
    deny_delete: bool = True,
) -> int:
    """Open and validate a private, non-reparse directory as a raw HANDLE."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows directory handles are unavailable")
    handle = _open_windows_path_handle(
        path,
        inheritable=inheritable,
        deny_delete=deny_delete,
    )
    try:
        _capture_windows_handle(
            handle,
            path,
            directory=True,
            generation_token=None,
        )
    except BaseException:
        close_windows_handle(handle)
        raise
    return handle


def open_windows_directory_fd(
    path: Path,
    *,
    inheritable: bool = False,
    deny_delete: bool = True,
) -> int:
    """Open a validated directory HANDLE and transfer it to a CRT descriptor."""

    handle = open_windows_directory_handle(
        path,
        inheritable=inheritable,
        deny_delete=deny_delete,
    )
    try:
        descriptor = msvcrt.open_osfhandle(  # type: ignore[name-defined]
            handle,
            os.O_RDONLY | os.O_BINARY,
        )
    except BaseException:
        close_windows_handle(handle)
        raise
    return descriptor


def set_windows_handle_inheritable(handle: int, inheritable: bool) -> None:
    """Set only the HANDLE inheritance bit used by STARTUPINFO.handle_list."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows HANDLE inheritance is unavailable")
    if type(handle) is not int or handle in {0, -1, _INVALID_HANDLE_VALUE}:
        raise OSError(errno.EBADF, "invalid Windows handle")
    if type(inheritable) is not bool:
        raise TypeError("inheritable must be bool")
    if not _set_handle_information(  # type: ignore[name-defined]
        handle,
        _HANDLE_FLAG_INHERIT,  # type: ignore[name-defined]
        _HANDLE_FLAG_INHERIT if inheritable else 0,  # type: ignore[name-defined]
    ):
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]


def close_windows_handle(handle: int) -> None:
    """Close a raw HANDLE returned by this module."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows HANDLEs are unavailable")
    if type(handle) is not int or handle in {0, -1, _INVALID_HANDLE_VALUE}:
        raise OSError(errno.EBADF, "invalid Windows handle")
    if not _close_handle(handle):  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]


def validate_windows_handle_path(
    handle: int,
    path: Path,
    *,
    directory: bool = True,
    expected: WindowsPathCapability | None = None,
    generation_token: str | None = None,
) -> WindowsPathCapability:
    """Validate an inherited raw HANDLE against its current protected path.

    The supplied handle is never closed.  A second delete-denying handle pins
    the named object while File ID, volume, owner and protected DACL are
    compared, so a stale inherited handle cannot authorize a replacement.
    """

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows HANDLE validation is unavailable")
    if expected is not None and type(expected) is not WindowsPathCapability:
        raise TypeError("expected must be a WindowsPathCapability")
    if expected is not None and generation_token is not None:
        raise ValueError("generation_token and expected are mutually exclusive")
    token = expected.generation_token if expected is not None else generation_token
    opened = _capture_windows_handle(
        handle,
        path,
        directory=directory,
        generation_token=token,
    )
    current_handle = _open_windows_path_handle(
        path,
        inheritable=False,
        deny_delete=True,
    )
    try:
        current = _capture_windows_handle(
            current_handle,
            path,
            directory=directory,
            generation_token=opened.generation_token,
        )
    finally:
        close_windows_handle(current_handle)
    if current != opened or (expected is not None and opened != expected):
        raise OSError(errno.EACCES, "Windows handle capability identity changed")
    return opened


def _pin_windows_parent(
    path: Path,
    expected_parent: WindowsPathCapability | None,
) -> int | None:
    if expected_parent is None:
        return None
    if type(expected_parent) is not WindowsPathCapability:
        raise TypeError("expected_parent must be a WindowsPathCapability")
    parent = Path(expected_parent.path)
    if os.path.normcase(os.fspath(path.parent)) != os.path.normcase(os.fspath(parent)):
        raise OSError(errno.EACCES, "Windows child is outside its expected parent")
    handle = open_windows_directory_handle(parent, inheritable=False, deny_delete=True)
    try:
        validate_windows_handle_path(
            handle,
            parent,
            directory=True,
            expected=expected_parent,
        )
    except BaseException:
        close_windows_handle(handle)
        raise
    return handle


def ensure_private_directory(
    path: Path,
    *,
    expected_parent: WindowsPathCapability | None = None,
    exclusive: bool = False,
) -> WindowsPathCapability:
    """Atomically create or validate a private Windows directory.

    A newly-created directory receives its protected DACL in CreateDirectoryW,
    before it is observable.  An existing race winner is only validated; this
    function never rewrites an unknown object's security descriptor.
    """

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "private Windows directories are unavailable")
    if type(exclusive) is not bool:
        raise TypeError("exclusive must be bool")
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows directory path must be normalized and absolute")
    parent_handle = _pin_windows_parent(absolute, expected_parent)
    descriptor = _private_windows_security_descriptor()
    attributes = _SecurityAttributes(  # type: ignore[name-defined]
        ctypes.sizeof(_SecurityAttributes),  # type: ignore[name-defined]
        descriptor,
        False,
    )
    try:
        ctypes.set_last_error(0)  # type: ignore[name-defined]
        if not _create_directory(  # type: ignore[name-defined]
            windows_extended_path(absolute),
            ctypes.byref(attributes),  # type: ignore[name-defined]
        ):
            error = ctypes.get_last_error()  # type: ignore[name-defined]
            if error != _ERROR_ALREADY_EXISTS or exclusive:  # type: ignore[name-defined]
                raise ctypes.WinError(error)  # type: ignore[name-defined]
        capability = capture_windows_path(absolute, directory=True)
        if expected_parent is not None and capability.volume != expected_parent.volume:
            raise OSError(errno.EXDEV, "Windows child is on another volume")
        if parent_handle is not None:
            validate_windows_handle_path(
                parent_handle,
                Path(expected_parent.path),  # type: ignore[union-attr]
                directory=True,
                expected=expected_parent,
            )
        return capability
    finally:
        _local_free(descriptor)  # type: ignore[name-defined]
        if parent_handle is not None:
            close_windows_handle(parent_handle)


def open_private_file(
    path: Path,
    *,
    create: bool = True,
    read_write: bool = True,
    exclusive: bool = False,
    share_delete: bool = False,
    expected_parent: WindowsPathCapability | None = None,
) -> tuple[int, WindowsPathCapability]:
    """Atomically create/open a private regular file and return its CRT fd.

    The raw HANDLE remains pinned while its protected DACL and identity are
    checked, then ownership is transferred to the CRT descriptor.  Callers may
    opt into delete sharing for POSIX-style read handles that must stay bound
    across an atomic rename; mutation/lock handles remain delete-denying.
    """

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "private Windows files are unavailable")
    if (
        type(create) is not bool
        or type(read_write) is not bool
        or type(exclusive) is not bool
        or type(share_delete) is not bool
    ):
        raise TypeError("create, read_write, exclusive and share_delete must be bool")
    if exclusive and not create:
        raise ValueError("exclusive requires create")
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows file path must be normalized and absolute")
    parent_handle = _pin_windows_parent(absolute, expected_parent)
    descriptor = _private_windows_security_descriptor()
    attributes = _SecurityAttributes(  # type: ignore[name-defined]
        ctypes.sizeof(_SecurityAttributes),  # type: ignore[name-defined]
        descriptor,
        False,
    )
    raw_handle: int | None = None
    try:
        access = _GENERIC_READ  # type: ignore[name-defined]
        if read_write:
            access |= _GENERIC_WRITE  # type: ignore[name-defined]
        share_mode = _FILE_SHARE_READ | _FILE_SHARE_WRITE  # type: ignore[name-defined]
        if share_delete:
            share_mode |= _FILE_SHARE_DELETE  # type: ignore[name-defined]
        raw = _create_file(  # type: ignore[name-defined]
            windows_extended_path(absolute),
            access | _FILE_READ_ATTRIBUTES | _READ_CONTROL,  # type: ignore[name-defined]
            share_mode,
            ctypes.byref(attributes),  # type: ignore[name-defined]
            (
                _CREATE_NEW  # type: ignore[name-defined]
                if exclusive
                else _OPEN_ALWAYS  # type: ignore[name-defined]
                if create
                else _OPEN_EXISTING  # type: ignore[name-defined]
            ),
            _FILE_FLAG_OPEN_REPARSE_POINT,  # type: ignore[name-defined]
            None,
        )
        if raw in {None, _INVALID_HANDLE_VALUE}:  # type: ignore[name-defined]
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        raw_handle = int(raw)
        capability = _capture_windows_handle(
            raw_handle,
            absolute,
            directory=False,
            generation_token=None,
        )
        if expected_parent is not None and capability.volume != expected_parent.volume:
            raise OSError(errno.EXDEV, "Windows file is on another volume")
        if parent_handle is not None:
            validate_windows_handle_path(
                parent_handle,
                Path(expected_parent.path),  # type: ignore[union-attr]
                directory=True,
                expected=expected_parent,
            )
        flags = os.O_BINARY | (os.O_RDWR if read_write else os.O_RDONLY)
        descriptor_fd = msvcrt.open_osfhandle(raw_handle, flags)  # type: ignore[name-defined]
        raw_handle = None
        return descriptor_fd, capability
    finally:
        _local_free(descriptor)  # type: ignore[name-defined]
        if raw_handle is not None:
            close_windows_handle(raw_handle)
        if parent_handle is not None:
            close_windows_handle(parent_handle)


def replace_windows_file(
    source: Path,
    destination: Path,
    *,
    source_parent: WindowsPathCapability,
    destination_parent: WindowsPathCapability | None = None,
    expected_source: WindowsPathCapability | None = None,
    expected_destination: WindowsPathCapability | None = None,
) -> WindowsPathCapability:
    """Atomically replace one private file while preserving exact File ID.

    The source is pinned by a raw handle that shares DELETE (so MoveFileExW can
    move it) but carries no mutation authority itself.  Both parent directory
    capabilities remain delete-denying pins throughout the write-through move.
    """

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "atomic Windows replacement is unavailable")
    source_path = Path(os.path.abspath(source))
    destination_path = Path(os.path.abspath(destination))
    if source_path != source or destination_path != destination:
        raise OSError(errno.EINVAL, "Windows replacement paths must be normalized")
    if type(source_parent) is not WindowsPathCapability or (
        destination_parent is not None and type(destination_parent) is not WindowsPathCapability
    ):
        raise TypeError("replacement parents must be WindowsPathCapability values")
    if expected_source is not None and type(expected_source) is not WindowsPathCapability:
        raise TypeError("expected_source must be a WindowsPathCapability")
    if expected_destination is not None and type(expected_destination) is not WindowsPathCapability:
        raise TypeError("expected_destination must be a WindowsPathCapability")
    destination_parent = source_parent if destination_parent is None else destination_parent
    source_parent_handle = _pin_windows_parent(source_path, source_parent)
    destination_parent_handle: int | None = None
    source_handle: int | None = None
    try:
        if destination_parent == source_parent:
            destination_parent_handle = source_parent_handle
        else:
            destination_parent_handle = _pin_windows_parent(
                destination_path,
                destination_parent,
            )
        source_handle = _open_windows_file_mutation_handle(
            source_path,
            delete_access=False,
            directory=False,
        )
        source_capability = _capture_windows_handle(
            source_handle,
            source_path,
            directory=False,
            generation_token=(
                expected_source.generation_token if expected_source is not None else None
            ),
        )
        if expected_source is not None and source_capability != expected_source:
            raise OSError(errno.EACCES, "Windows replacement source identity changed")
        try:
            current_destination = capture_windows_path(
                destination_path,
                directory=False,
                generation_token=(
                    expected_destination.generation_token
                    if expected_destination is not None
                    else None
                ),
            )
        except FileNotFoundError:
            if expected_destination is not None:
                raise OSError(
                    errno.EACCES,
                    "Windows replacement destination disappeared",
                ) from None
        else:
            if expected_destination is not None and current_destination != expected_destination:
                raise OSError(
                    errno.EACCES,
                    "Windows replacement destination identity changed",
                )
        if source_capability.volume != destination_parent.volume:
            raise OSError(errno.EXDEV, "Windows replacement crosses volumes")
        if not _move_file_ex(  # type: ignore[name-defined]
            windows_extended_path(source_path),
            windows_extended_path(destination_path),
            _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        moved = validate_windows_handle_path(
            source_handle,
            destination_path,
            directory=False,
            generation_token=source_capability.generation_token,
        )
        if (
            moved.volume,
            moved.file_id,
            moved.owner_sid,
            moved.security_sha256,
        ) != (
            source_capability.volume,
            source_capability.file_id,
            source_capability.owner_sid,
            source_capability.security_sha256,
        ):
            raise OSError(errno.EACCES, "Windows replacement File ID changed")
        try:
            os.lstat(windows_extended_path(source_path))
        except FileNotFoundError:
            pass
        else:
            raise OSError(errno.EACCES, "Windows replacement source name reappeared")
        assert source_parent_handle is not None
        validate_windows_handle_path(
            source_parent_handle,
            Path(source_parent.path),
            directory=True,
            expected=source_parent,
        )
        assert destination_parent_handle is not None
        validate_windows_handle_path(
            destination_parent_handle,
            Path(destination_parent.path),
            directory=True,
            expected=destination_parent,
        )
        return moved
    finally:
        if source_handle is not None:
            close_windows_handle(source_handle)
        if (
            destination_parent_handle is not None
            and destination_parent_handle != source_parent_handle
        ):
            close_windows_handle(destination_parent_handle)
        if source_parent_handle is not None:
            close_windows_handle(source_parent_handle)


def _delete_windows_object(
    path: Path,
    *,
    parent: WindowsPathCapability,
    expected: WindowsPathCapability,
    directory: bool,
) -> None:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "exact Windows deletion is unavailable")
    absolute = Path(os.path.abspath(path))
    if absolute != path:
        raise OSError(errno.EINVAL, "Windows deletion path must be normalized")
    if type(parent) is not WindowsPathCapability or type(expected) is not WindowsPathCapability:
        raise TypeError("parent and expected must be WindowsPathCapability values")
    parent_handle = _pin_windows_parent(absolute, parent)
    handle: int | None = None
    deletion_requested = False
    try:
        handle = _open_windows_file_mutation_handle(
            absolute,
            delete_access=True,
            directory=directory,
        )
        opened = _capture_windows_handle(
            handle,
            absolute,
            directory=directory,
            generation_token=expected.generation_token,
        )
        if opened != expected:
            raise OSError(errno.EACCES, "Windows deletion target identity changed")
        extended = _FileDispositionInfoEx(  # type: ignore[name-defined]
            _FILE_DISPOSITION_FLAG_DELETE  # type: ignore[name-defined]
            | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS  # type: ignore[name-defined]
        )
        if not _set_file_information(  # type: ignore[name-defined]
            handle,
            _FILE_DISPOSITION_INFO_EX_CLASS,  # type: ignore[name-defined]
            ctypes.byref(extended),  # type: ignore[name-defined]
            ctypes.sizeof(extended),  # type: ignore[name-defined]
        ):
            error = ctypes.get_last_error()  # type: ignore[name-defined]
            if error != _ERROR_INVALID_PARAMETER:  # type: ignore[name-defined]
                raise ctypes.WinError(error)  # type: ignore[name-defined]
            legacy = _FileDispositionInfo(True)  # type: ignore[name-defined]
            if not _set_file_information(  # type: ignore[name-defined]
                handle,
                _FILE_DISPOSITION_INFO_CLASS,  # type: ignore[name-defined]
                ctypes.byref(legacy),  # type: ignore[name-defined]
                ctypes.sizeof(legacy),  # type: ignore[name-defined]
            ):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        deletion_requested = True
        assert parent_handle is not None
        validate_windows_handle_path(
            parent_handle,
            Path(parent.path),
            directory=True,
            expected=parent,
        )
    finally:
        if handle is not None:
            close_windows_handle(handle)
        if parent_handle is not None:
            close_windows_handle(parent_handle)
    if deletion_requested:
        try:
            os.lstat(windows_extended_path(absolute))
        except FileNotFoundError:
            return
        raise OSError(errno.EACCES, "Windows deletion target name still exists")


def delete_windows_file(
    path: Path,
    *,
    parent: WindowsPathCapability,
    expected: WindowsPathCapability,
) -> None:
    """Delete the exact private regular file named by a capability."""

    _delete_windows_object(
        path,
        parent=parent,
        expected=expected,
        directory=False,
    )


def delete_windows_directory(
    path: Path,
    *,
    parent: WindowsPathCapability,
    expected: WindowsPathCapability,
) -> None:
    """Delete one exact empty private directory through its native HANDLE."""

    _delete_windows_object(
        path,
        parent=parent,
        expected=expected,
        directory=True,
    )


def _delete_windows_capability(
    expected: WindowsPathCapability,
    *,
    directory: bool,
) -> None:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "exact Windows deletion is unavailable")
    if type(expected) is not WindowsPathCapability:
        raise TypeError("expected must be a WindowsPathCapability")
    absolute = Path(os.path.abspath(expected.path))
    if os.path.normcase(os.fspath(absolute)) != os.path.normcase(expected.path):
        raise OSError(errno.EINVAL, "Windows deletion path must be normalized")
    handle: int | None = None
    deletion_requested = False
    try:
        handle = _open_windows_file_mutation_handle(
            absolute,
            delete_access=True,
            directory=directory,
        )
        opened = _capture_windows_handle(
            handle,
            absolute,
            directory=directory,
            generation_token=expected.generation_token,
        )
        if opened != expected:
            raise OSError(errno.EACCES, "Windows deletion target identity changed")
        extended = _FileDispositionInfoEx(  # type: ignore[name-defined]
            _FILE_DISPOSITION_FLAG_DELETE  # type: ignore[name-defined]
            | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS  # type: ignore[name-defined]
        )
        if not _set_file_information(  # type: ignore[name-defined]
            handle,
            _FILE_DISPOSITION_INFO_EX_CLASS,  # type: ignore[name-defined]
            ctypes.byref(extended),  # type: ignore[name-defined]
            ctypes.sizeof(extended),  # type: ignore[name-defined]
        ):
            error = ctypes.get_last_error()  # type: ignore[name-defined]
            if error != _ERROR_INVALID_PARAMETER:  # type: ignore[name-defined]
                raise ctypes.WinError(error)  # type: ignore[name-defined]
            legacy = _FileDispositionInfo(True)  # type: ignore[name-defined]
            if not _set_file_information(  # type: ignore[name-defined]
                handle,
                _FILE_DISPOSITION_INFO_CLASS,  # type: ignore[name-defined]
                ctypes.byref(legacy),  # type: ignore[name-defined]
                ctypes.sizeof(legacy),  # type: ignore[name-defined]
            ):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        deletion_requested = True
    finally:
        if handle is not None:
            close_windows_handle(handle)
    if deletion_requested:
        try:
            os.lstat(windows_extended_path(absolute))
        except FileNotFoundError:
            return
        raise OSError(errno.EACCES, "Windows deletion target name still exists")


def delete_windows_file_capability(expected: WindowsPathCapability) -> None:
    """Delete one exact private file without trusting its ordinary parent."""

    _delete_windows_capability(expected, directory=False)


def delete_windows_directory_capability(expected: WindowsPathCapability) -> None:
    """Delete an exact private directory without trusting its ordinary parent.

    This variant is for an isolated random staging root whose parent is an
    ordinary system directory.  Deletion is requested on the identity-checked
    directory HANDLE itself, so a path replacement is never deleted.
    """

    _delete_windows_capability(expected, directory=True)


def _rename_windows_object(
    source: Path,
    destination: Path,
    *,
    source_parent: WindowsPathCapability,
    destination_parent: WindowsPathCapability | None = None,
    expected_source: WindowsPathCapability | None = None,
    directory: bool,
) -> WindowsPathCapability:
    """Atomically publish one private object without replacing a winner."""

    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "atomic Windows rename is unavailable")
    source_path = Path(os.path.abspath(source))
    destination_path = Path(os.path.abspath(destination))
    if source_path != source or destination_path != destination:
        raise OSError(errno.EINVAL, "Windows rename paths must be normalized")
    if type(source_parent) is not WindowsPathCapability or (
        destination_parent is not None and type(destination_parent) is not WindowsPathCapability
    ):
        raise TypeError("rename parents must be WindowsPathCapability values")
    if expected_source is not None and type(expected_source) is not WindowsPathCapability:
        raise TypeError("expected_source must be a WindowsPathCapability")
    destination_parent = source_parent if destination_parent is None else destination_parent
    source_parent_handle = _pin_windows_parent(source_path, source_parent)
    destination_parent_handle: int | None = None
    source_handle: int | None = None
    try:
        if destination_parent == source_parent:
            destination_parent_handle = source_parent_handle
        else:
            destination_parent_handle = _pin_windows_parent(
                destination_path,
                destination_parent,
            )
        source_handle = _open_windows_file_mutation_handle(
            source_path,
            delete_access=False,
            directory=directory,
        )
        source_capability = _capture_windows_handle(
            source_handle,
            source_path,
            directory=directory,
            generation_token=(
                expected_source.generation_token if expected_source is not None else None
            ),
        )
        if expected_source is not None and source_capability != expected_source:
            raise OSError(errno.EACCES, "Windows rename source identity changed")
        if source_capability.volume != destination_parent.volume:
            raise OSError(errno.EXDEV, "Windows rename crosses volumes")
        try:
            os.lstat(windows_extended_path(destination_path))
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "Windows rename destination exists")
        if not _move_file_ex(  # type: ignore[name-defined]
            windows_extended_path(source_path),
            windows_extended_path(destination_path),
            _MOVEFILE_WRITE_THROUGH,  # type: ignore[name-defined]
        ):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
        moved = validate_windows_handle_path(
            source_handle,
            destination_path,
            directory=directory,
            generation_token=source_capability.generation_token,
        )
        if (
            moved.volume,
            moved.file_id,
            moved.owner_sid,
            moved.security_sha256,
        ) != (
            source_capability.volume,
            source_capability.file_id,
            source_capability.owner_sid,
            source_capability.security_sha256,
        ):
            raise OSError(errno.EACCES, "Windows rename File ID changed")
        try:
            os.lstat(windows_extended_path(source_path))
        except FileNotFoundError:
            pass
        else:
            raise OSError(errno.EACCES, "Windows rename source name reappeared")
        assert source_parent_handle is not None
        validate_windows_handle_path(
            source_parent_handle,
            Path(source_parent.path),
            directory=True,
            expected=source_parent,
        )
        assert destination_parent_handle is not None
        validate_windows_handle_path(
            destination_parent_handle,
            Path(destination_parent.path),
            directory=True,
            expected=destination_parent,
        )
        return moved
    finally:
        if source_handle is not None:
            close_windows_handle(source_handle)
        if (
            destination_parent_handle is not None
            and destination_parent_handle != source_parent_handle
        ):
            close_windows_handle(destination_parent_handle)
        if source_parent_handle is not None:
            close_windows_handle(source_parent_handle)


def rename_windows_directory(
    source: Path,
    destination: Path,
    *,
    source_parent: WindowsPathCapability,
    destination_parent: WindowsPathCapability | None = None,
    expected_source: WindowsPathCapability | None = None,
) -> WindowsPathCapability:
    """Atomically publish one private directory without replacing a winner."""

    return _rename_windows_object(
        source,
        destination,
        source_parent=source_parent,
        destination_parent=destination_parent,
        expected_source=expected_source,
        directory=True,
    )


def rename_windows_file(
    source: Path,
    destination: Path,
    *,
    source_parent: WindowsPathCapability,
    destination_parent: WindowsPathCapability | None = None,
    expected_source: WindowsPathCapability | None = None,
) -> WindowsPathCapability:
    """Atomically publish one private regular file without replacing a winner."""

    return _rename_windows_object(
        source,
        destination,
        source_parent=source_parent,
        destination_parent=destination_parent,
        expected_source=expected_source,
        directory=False,
    )


@dataclass(frozen=True, slots=True)
class WindowsExternalFileCapability:
    """Identity-only capability for one untrusted, delete-pinned input file.

    External user-selected files are not required to carry VibeCAD's private
    DACL.  They are instead opened without delete sharing and pinned by their
    native volume and 128-bit File ID while bytes are copied into private state.
    """

    path: str
    volume: int
    file_id: int
    generation_token: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "file_id": f"{self.file_id:032x}",
            "generation_token": self.generation_token,
            "path": self.path,
            "schema_version": 1,
            "volume": f"{self.volume:016x}",
        }

    @classmethod
    def from_mapping(cls, value: object) -> WindowsExternalFileCapability:
        fields = {
            "file_id",
            "generation_token",
            "path",
            "schema_version",
            "volume",
        }
        if (
            type(value) is not dict
            or set(value) != fields
            or value.get("schema_version") != 1
            or type(value.get("path")) is not str
            or type(value.get("volume")) is not str
            or type(value.get("file_id")) is not str
            or type(value.get("generation_token")) is not str
            or _CAPABILITY_VOLUME.fullmatch(value["volume"]) is None
            or _CAPABILITY_FILE_ID.fullmatch(value["file_id"]) is None
            or _CAPABILITY_TOKEN.fullmatch(value["generation_token"]) is None
        ):
            raise ValueError("invalid Windows external file capability")
        return cls(
            path=value["path"],
            volume=int(value["volume"], 16),
            file_id=int(value["file_id"], 16),
            generation_token=value["generation_token"],
        )


def _open_windows_external_file_handle(path: Path) -> int:
    if sys.platform != "win32":
        raise OSError(errno.ENOTSUP, "Windows external files are unavailable")
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute() or absolute != path:
        raise OSError(errno.EINVAL, "Windows external file path must be normalized")
    attributes = _SecurityAttributes(  # type: ignore[name-defined]
        ctypes.sizeof(_SecurityAttributes),  # type: ignore[name-defined]
        None,
        False,
    )
    handle = _create_file(  # type: ignore[name-defined]
        windows_extended_path(absolute),
        _GENERIC_READ | _FILE_READ_ATTRIBUTES,  # type: ignore[name-defined]
        # Reads and writes may coexist so hostile mutation is observable, but
        # DELETE sharing is deliberately withheld to prevent name replacement.
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,  # type: ignore[name-defined]
        ctypes.byref(attributes),
        _OPEN_EXISTING,  # type: ignore[name-defined]
        _FILE_FLAG_OPEN_REPARSE_POINT,  # type: ignore[name-defined]
        None,
    )
    if handle in {None, _INVALID_HANDLE_VALUE}:  # type: ignore[name-defined]
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[name-defined]
    return int(handle)


def _capture_windows_external_handle(
    handle: int,
    *,
    generation_token: str | None,
) -> WindowsExternalFileCapability:
    path = _windows_handle_path(handle)
    volume, file_id = _windows_handle_information(handle, directory=False)
    token = secrets.token_hex(32) if generation_token is None else generation_token
    if type(token) is not str or _CAPABILITY_TOKEN.fullmatch(token) is None:
        raise ValueError("invalid Windows external capability generation token")
    return WindowsExternalFileCapability(
        path=os.fspath(path),
        volume=volume,
        file_id=file_id,
        generation_token=token,
    )


def open_windows_external_file(
    path: Path,
) -> tuple[int, WindowsExternalFileCapability]:
    """Open an ordinary user file read-only while denying name replacement."""

    handle = _open_windows_external_file_handle(path)
    try:
        capability = _capture_windows_external_handle(handle, generation_token=None)
        descriptor = msvcrt.open_osfhandle(  # type: ignore[name-defined]
            handle,
            os.O_RDONLY | os.O_BINARY,
        )
        handle = -1
        os.set_inheritable(descriptor, False)
        return descriptor, capability
    finally:
        if handle not in {-1, None}:
            close_windows_handle(handle)


def capture_windows_external_fd(
    fd: int,
    *,
    generation_token: str | None = None,
) -> WindowsExternalFileCapability:
    """Recapture an open ordinary input through its native kernel handle."""

    return _capture_windows_external_handle(
        _windows_handle(fd),
        generation_token=generation_token,
    )


def validate_windows_external_file(
    capability: WindowsExternalFileCapability,
) -> Path:
    """Reopen and compare one delete-pinned external file's exact identity."""

    if type(capability) is not WindowsExternalFileCapability:
        raise TypeError("capability must be a WindowsExternalFileCapability")
    path = Path(capability.path)
    handle = _open_windows_external_file_handle(path)
    try:
        current = _capture_windows_external_handle(
            handle,
            generation_token=capability.generation_token,
        )
    finally:
        close_windows_handle(handle)
    if current != capability:
        raise OSError(errno.EACCES, "Windows external file identity changed")
    return path
