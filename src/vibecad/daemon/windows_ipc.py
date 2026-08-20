"""Fail-closed Win32 local transport and process identity primitives.

The Darwin daemon keeps using AF_UNIX/getpeereid/SCM_RIGHTS.  This module is
the separate Windows backend: a local-only named pipe protected by an
explicit current-user DACL, peer process-token validation, and server-side
DuplicateHandle for capability transfer.
"""

from __future__ import annotations

import ctypes
import functools
import hashlib
import os
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

__all__ = (
    "HANDLE_ENVELOPE_BYTES",
    "HANDLE_ENVELOPE_MAGIC",
    "WindowsNamedPipeConnection",
    "WindowsNamedPipeListener",
    "WindowsPeerIdentity",
    "connect_named_pipe",
    "current_process_start_ns",
    "current_user_sid",
    "duplicate_handle_from_peer",
    "encode_handle_envelope",
    "named_pipe_name",
    "process_is_same_or_direct_child",
    "process_start_ns",
    "require_expected_peer",
    "require_same_user_peer",
    "split_handle_envelope",
)


HANDLE_ENVELOPE_MAGIC = b"\x00VCH1"
HANDLE_ENVELOPE_BYTES = len(HANDLE_ENVELOPE_MAGIC) + 8

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_ACCESS_DENIED = 5
_ERROR_BROKEN_PIPE = 109
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_IO_PENDING = 997
_ERROR_MORE_DATA = 234
_ERROR_NO_DATA = 232
_ERROR_PIPE_BUSY = 231
_ERROR_PIPE_CONNECTED = 535
_ERROR_PIPE_NOT_CONNECTED = 233
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_INFINITE = 0xFFFFFFFF
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000

_PIPE_ACCESS_DUPLEX = 0x00000003
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
_PIPE_TYPE_BYTE = 0
_PIPE_READMODE_BYTE = 0
_PIPE_WAIT = 0
_PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
_PIPE_UNLIMITED_INSTANCES = 255

_PROCESS_DUP_HANDLE = 0x0040
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_DUPLICATE_SAME_ACCESS = 0x00000002
_HANDLE_FLAG_INHERIT = 0x00000001
_SDDL_REVISION_1 = 1
_WINDOWS_EPOCH_100NS = 116_444_736_000_000_000


class _SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    )


class _Overlapped(ctypes.Structure):
    _fields_ = (
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    )


class _FileTime(ctypes.Structure):
    _fields_ = (("low", wintypes.DWORD), ("high", wintypes.DWORD))


class _SidAndAttributes(ctypes.Structure):
    _fields_ = (("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD))


class _TokenUser(ctypes.Structure):
    _fields_ = (("user", _SidAndAttributes),)


class _ProcessEntry32W(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    )


@dataclass(frozen=True, slots=True)
class WindowsPeerIdentity:
    sid: str
    pid: int
    started_ns: int


def _windows_only() -> None:
    if sys.platform != "win32":
        raise OSError("Win32 local IPC is unavailable on this platform")


def _raise_last_error(message: str) -> None:
    error = ctypes.get_last_error() or 1
    raise OSError(error, message, None, error)


class _Win32:
    def __init__(self) -> None:
        _windows_only()
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        self.get_current_process = self.kernel32.GetCurrentProcess
        self.get_current_process.argtypes = ()
        self.get_current_process.restype = wintypes.HANDLE

        self.close_handle = self.kernel32.CloseHandle
        self.close_handle.argtypes = (wintypes.HANDLE,)
        self.close_handle.restype = wintypes.BOOL

        self.local_free = self.kernel32.LocalFree
        self.local_free.argtypes = (ctypes.c_void_p,)
        self.local_free.restype = ctypes.c_void_p

        self.create_event = self.kernel32.CreateEventW
        self.create_event.argtypes = (
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        self.create_event.restype = wintypes.HANDLE

        self.wait_for_single_object = self.kernel32.WaitForSingleObject
        self.wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.wait_for_single_object.restype = wintypes.DWORD

        self.cancel_io_ex = self.kernel32.CancelIoEx
        self.cancel_io_ex.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Overlapped))
        self.cancel_io_ex.restype = wintypes.BOOL

        self.get_overlapped_result = self.kernel32.GetOverlappedResult
        self.get_overlapped_result.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_Overlapped),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
        )
        self.get_overlapped_result.restype = wintypes.BOOL

        self.create_named_pipe = self.kernel32.CreateNamedPipeW
        self.create_named_pipe.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_SecurityAttributes),
        )
        self.create_named_pipe.restype = wintypes.HANDLE

        self.connect_named_pipe = self.kernel32.ConnectNamedPipe
        self.connect_named_pipe.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Overlapped))
        self.connect_named_pipe.restype = wintypes.BOOL

        self.disconnect_named_pipe = self.kernel32.DisconnectNamedPipe
        self.disconnect_named_pipe.argtypes = (wintypes.HANDLE,)
        self.disconnect_named_pipe.restype = wintypes.BOOL

        self.wait_named_pipe = self.kernel32.WaitNamedPipeW
        self.wait_named_pipe.argtypes = (wintypes.LPCWSTR, wintypes.DWORD)
        self.wait_named_pipe.restype = wintypes.BOOL

        self.create_file = self.kernel32.CreateFileW
        self.create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self.create_file.restype = wintypes.HANDLE

        self.set_named_pipe_handle_state = self.kernel32.SetNamedPipeHandleState
        self.set_named_pipe_handle_state.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self.set_named_pipe_handle_state.restype = wintypes.BOOL

        self.read_file = self.kernel32.ReadFile
        self.read_file.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_Overlapped),
        )
        self.read_file.restype = wintypes.BOOL

        self.write_file = self.kernel32.WriteFile
        self.write_file.argtypes = (
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(_Overlapped),
        )
        self.write_file.restype = wintypes.BOOL

        self.set_handle_information = self.kernel32.SetHandleInformation
        self.set_handle_information.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self.set_handle_information.restype = wintypes.BOOL

        self.get_handle_information = self.kernel32.GetHandleInformation
        self.get_handle_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self.get_handle_information.restype = wintypes.BOOL

        self.get_named_pipe_client_process_id = self.kernel32.GetNamedPipeClientProcessId
        self.get_named_pipe_client_process_id.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.ULONG),
        )
        self.get_named_pipe_client_process_id.restype = wintypes.BOOL

        self.get_named_pipe_server_process_id = self.kernel32.GetNamedPipeServerProcessId
        self.get_named_pipe_server_process_id.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.ULONG),
        )
        self.get_named_pipe_server_process_id.restype = wintypes.BOOL

        self.open_process = self.kernel32.OpenProcess
        self.open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.open_process.restype = wintypes.HANDLE

        self.get_process_times = self.kernel32.GetProcessTimes
        self.get_process_times.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        )
        self.get_process_times.restype = wintypes.BOOL

        self.create_toolhelp32_snapshot = self.kernel32.CreateToolhelp32Snapshot
        self.create_toolhelp32_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        self.create_toolhelp32_snapshot.restype = wintypes.HANDLE

        self.process32_first = self.kernel32.Process32FirstW
        self.process32_first.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        )
        self.process32_first.restype = wintypes.BOOL

        self.process32_next = self.kernel32.Process32NextW
        self.process32_next.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessEntry32W),
        )
        self.process32_next.restype = wintypes.BOOL

        self.duplicate_handle = self.kernel32.DuplicateHandle
        self.duplicate_handle.argtypes = (
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        self.duplicate_handle.restype = wintypes.BOOL

        self.open_process_token = self.advapi32.OpenProcessToken
        self.open_process_token.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        )
        self.open_process_token.restype = wintypes.BOOL

        self.get_token_information = self.advapi32.GetTokenInformation
        self.get_token_information.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        )
        self.get_token_information.restype = wintypes.BOOL

        self.convert_sid_to_string = self.advapi32.ConvertSidToStringSidW
        self.convert_sid_to_string.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
        self.convert_sid_to_string.restype = wintypes.BOOL

        self.convert_sddl = self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        self.convert_sddl.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.ULONG),
        )
        self.convert_sddl.restype = wintypes.BOOL


@functools.lru_cache(maxsize=1)
def _api() -> _Win32:
    return _Win32()


def _valid_handle(handle: object) -> bool:
    return type(handle) is int and handle not in {0, _INVALID_HANDLE_VALUE}


def _close_handle(handle: int) -> None:
    if _valid_handle(handle):
        _api().close_handle(handle)


def _filetime_value(value: _FileTime) -> int:
    return (int(value.high) << 32) | int(value.low)


def _process_start_ns_from_handle(handle: int) -> int:
    api = _api()
    creation = _FileTime()
    exit_time = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    if not api.get_process_times(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        _raise_last_error("GetProcessTimes failed")
    raw = _filetime_value(creation)
    if raw <= _WINDOWS_EPOCH_100NS:
        raise OSError("invalid Win32 process creation time")
    return (raw - _WINDOWS_EPOCH_100NS) * 100


def _sid_from_process_handle(process: int) -> str:
    api = _api()
    token = wintypes.HANDLE()
    if not api.open_process_token(process, _TOKEN_QUERY, ctypes.byref(token)):
        _raise_last_error("OpenProcessToken failed")
    try:
        needed = wintypes.DWORD()
        ctypes.set_last_error(0)
        api.get_token_information(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or needed.value == 0:
            _raise_last_error("GetTokenInformation sizing failed")
        buffer = ctypes.create_string_buffer(needed.value)
        if not api.get_token_information(
            token,
            _TOKEN_USER,
            buffer,
            needed,
            ctypes.byref(needed),
        ):
            _raise_last_error("GetTokenInformation failed")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        if not token_user.user.sid:
            raise OSError("process token contains no user SID")
        text = wintypes.LPWSTR()
        if not api.convert_sid_to_string(token_user.user.sid, ctypes.byref(text)):
            _raise_last_error("ConvertSidToStringSid failed")
        try:
            value = text.value
            if type(value) is not str or not value.startswith("S-1-"):
                raise OSError("process token contains an invalid user SID")
            return value
        finally:
            api.local_free(text)
    finally:
        _close_handle(int(token.value or 0))


@functools.lru_cache(maxsize=1)
def current_user_sid() -> str:
    return _sid_from_process_handle(int(_api().get_current_process()))


def current_process_start_ns() -> int:
    return _process_start_ns_from_handle(int(_api().get_current_process()))


def process_start_ns(pid: int) -> int:
    """Return the exact Win32 creation time for one still-queryable process."""

    handle, identity = _open_process_identity(pid)
    try:
        return identity.started_ns
    finally:
        _close_handle(handle)


def _snapshot_parent_pid(pid: int) -> int:
    snapshot = int(_api().create_toolhelp32_snapshot(_TH32CS_SNAPPROCESS, 0) or 0)
    if not _valid_handle(snapshot):
        _raise_last_error("CreateToolhelp32Snapshot failed")
    try:
        entry = _ProcessEntry32W(dwSize=ctypes.sizeof(_ProcessEntry32W))
        if not _api().process32_first(snapshot, ctypes.byref(entry)):
            _raise_last_error("Process32FirstW failed")
        while True:
            if int(entry.th32ProcessID) == pid:
                parent = int(entry.th32ParentProcessID)
                if not 0 < parent <= 0xFFFFFFFF:
                    raise OSError("Win32 process has no queryable parent")
                return parent
            if not _api().process32_next(snapshot, ctypes.byref(entry)):
                raise OSError("Win32 process is absent from the native snapshot")
    finally:
        _close_handle(snapshot)


def process_is_same_or_direct_child(
    pid: int,
    *,
    started_ns: int,
    spawned_pid: int,
    spawned_started_ns: int,
) -> bool:
    """Bind a receipt generation to its Popen process or one venv launcher child.

    Windows virtual-environment launchers may remain as a tiny parent process
    while the real interpreter runs as their direct child. Both generations
    are pinned by live process handles, current-user SID and exact creation
    time before the native parent PID is trusted.
    """

    if any(
        type(value) is not int or value <= 0
        for value in (pid, started_ns, spawned_pid, spawned_started_ns)
    ):
        return False
    child_handle = 0
    spawned_handle = 0
    try:
        child_handle, child = _open_process_identity(pid)
        spawned_handle, spawned = _open_process_identity(spawned_pid)
        if (
            child.started_ns != started_ns
            or spawned.started_ns != spawned_started_ns
            or child.sid != current_user_sid()
            or spawned.sid != child.sid
        ):
            return False
        if child == spawned:
            return True
        if child.started_ns < spawned.started_ns:
            return False
        return _snapshot_parent_pid(child.pid) == spawned.pid
    except OSError:
        return False
    finally:
        _close_handle(spawned_handle)
        _close_handle(child_handle)


def _open_process_identity(pid: int, *, duplicate: bool = False) -> tuple[int, WindowsPeerIdentity]:
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise OSError("invalid Win32 process id")
    access = _PROCESS_QUERY_LIMITED_INFORMATION
    if duplicate:
        access |= _PROCESS_DUP_HANDLE
    handle = int(_api().open_process(access, False, pid) or 0)
    if not _valid_handle(handle):
        _raise_last_error("OpenProcess failed")
    try:
        identity = WindowsPeerIdentity(
            sid=_sid_from_process_handle(handle),
            pid=pid,
            started_ns=_process_start_ns_from_handle(handle),
        )
    except BaseException:
        _close_handle(handle)
        raise
    return handle, identity


def named_pipe_name(run_root: object) -> str:
    _windows_only()
    try:
        raw = os.fspath(run_root)
    except TypeError as error:
        raise OSError("invalid daemon run root") from error
    if type(raw) is not str or not raw or "\0" in raw or not Path(raw).is_absolute():
        raise OSError("invalid daemon run root")
    canonical = os.path.normcase(os.path.abspath(raw))
    digest = hashlib.sha256(
        (current_user_sid() + "\0" + canonical).encode("utf-16-le", "strict")
    ).hexdigest()
    return rf"\\.\pipe\vibecad-kernel-{digest}"


def _security_attributes() -> tuple[_SecurityAttributes, ctypes.c_void_p]:
    api = _api()
    descriptor = ctypes.c_void_p()
    # Protected DACL: only LocalSystem and the current logon user receive access.
    sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{current_user_sid()})"
    if not api.convert_sddl(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        _raise_last_error("named-pipe security descriptor creation failed")
    return (
        _SecurityAttributes(
            nLength=ctypes.sizeof(_SecurityAttributes),
            lpSecurityDescriptor=descriptor,
            bInheritHandle=False,
        ),
        descriptor,
    )


def _deadline_milliseconds(timeout: float | None) -> int:
    if timeout is None:
        return _INFINITE
    if type(timeout) not in {int, float} or isinstance(timeout, bool) or timeout < 0:
        raise ValueError("timeout must be a non-negative number or None")
    return max(1, min(_INFINITE - 1, int(float(timeout) * 1000 + 0.999)))


def _overlapped_result(
    handle: int,
    operation,
    *,
    timeout: float | None,
    allowed_errors: frozenset[int] = frozenset(),
) -> tuple[bool, int, int]:
    api = _api()
    event = int(api.create_event(None, True, False, None) or 0)
    if not _valid_handle(event):
        _raise_last_error("CreateEvent failed")
    overlapped = _Overlapped(hEvent=event)
    transferred = wintypes.DWORD()
    try:
        ctypes.set_last_error(0)
        succeeded = bool(operation(ctypes.byref(overlapped), ctypes.byref(transferred)))
        error = 0 if succeeded else ctypes.get_last_error()
        if not succeeded and error not in {_ERROR_IO_PENDING, *allowed_errors}:
            raise OSError(error, "overlapped named-pipe operation failed", None, error)
        if error in allowed_errors:
            return False, int(transferred.value), error
        if error == _ERROR_IO_PENDING:
            wait = api.wait_for_single_object(event, _deadline_milliseconds(timeout))
            if wait == _WAIT_TIMEOUT:
                api.cancel_io_ex(handle, ctypes.byref(overlapped))
                api.wait_for_single_object(event, _INFINITE)
                api.get_overlapped_result(
                    handle,
                    ctypes.byref(overlapped),
                    ctypes.byref(transferred),
                    False,
                )
                raise TimeoutError
            if wait != _WAIT_OBJECT_0:
                api.cancel_io_ex(handle, ctypes.byref(overlapped))
                _raise_last_error("WaitForSingleObject failed")
            ctypes.set_last_error(0)
            if not api.get_overlapped_result(
                handle,
                ctypes.byref(overlapped),
                ctypes.byref(transferred),
                False,
            ):
                error = ctypes.get_last_error()
                if error in allowed_errors:
                    return False, int(transferred.value), error
                raise OSError(error, "GetOverlappedResult failed", None, error)
        return True, int(transferred.value), 0
    finally:
        _close_handle(event)


class WindowsNamedPipeConnection:
    """Socket-shaped, non-inheritable byte-stream wrapper around one pipe handle."""

    __slots__ = (
        "_closed",
        "_handle",
        "_io_lock",
        "_peer_identity",
        "_server_side",
        "_timeout",
    )

    def __init__(self, handle: int, *, server_side: bool) -> None:
        if not _valid_handle(handle) or type(server_side) is not bool:
            raise OSError("invalid named-pipe handle")
        self._handle = handle
        self._server_side = server_side
        self._timeout: float | None = None
        self._closed = False
        self._io_lock = threading.Lock()
        self._peer_identity: WindowsPeerIdentity | None = None
        self.set_inheritable(False)

    @property
    def handle(self) -> int:
        if self._closed:
            raise OSError("named-pipe connection is closed")
        return self._handle

    def fileno(self) -> int:
        return self.handle

    def settimeout(self, value: float | None) -> None:
        if value is not None and (
            type(value) not in {int, float} or isinstance(value, bool) or value < 0
        ):
            raise ValueError("timeout must be a non-negative number or None")
        self._timeout = None if value is None else float(value)

    def set_inheritable(self, inheritable: bool) -> None:
        if type(inheritable) is not bool:
            raise TypeError("inheritable must be bool")
        api = _api()
        if not api.set_handle_information(
            self.handle,
            _HANDLE_FLAG_INHERIT,
            _HANDLE_FLAG_INHERIT if inheritable else 0,
        ):
            _raise_last_error("SetHandleInformation failed")

    def get_inheritable(self) -> bool:
        flags = wintypes.DWORD()
        if not _api().get_handle_information(self.handle, ctypes.byref(flags)):
            _raise_last_error("GetHandleInformation failed")
        return bool(flags.value & _HANDLE_FLAG_INHERIT)

    def _peer_pid(self) -> int:
        value = wintypes.ULONG()
        operation = (
            _api().get_named_pipe_client_process_id
            if self._server_side
            else _api().get_named_pipe_server_process_id
        )
        if not operation(self.handle, ctypes.byref(value)) or value.value == 0:
            _raise_last_error("named-pipe peer process id is unavailable")
        return int(value.value)

    def peer_identity(self, *, duplicate: bool = False) -> tuple[int, WindowsPeerIdentity]:
        first_pid = self._peer_pid()
        process, identity = _open_process_identity(first_pid, duplicate=duplicate)
        try:
            if self._peer_pid() != first_pid:
                raise OSError("named-pipe peer changed during identity validation")
            if self._peer_identity is not None and identity != self._peer_identity:
                raise OSError("named-pipe peer identity changed")
            return process, identity
        except BaseException:
            _close_handle(process)
            raise

    def pin_peer(self, identity: WindowsPeerIdentity) -> None:
        if type(identity) is not WindowsPeerIdentity:
            raise TypeError("identity must be WindowsPeerIdentity")
        if self._peer_identity is not None and self._peer_identity != identity:
            raise OSError("named-pipe peer identity is already pinned")
        self._peer_identity = identity

    def recv(self, size: int) -> bytes:
        if type(size) is not int or size <= 0:
            raise ValueError("size must be positive")
        buffer = ctypes.create_string_buffer(size)

        def operation(overlapped, transferred):
            return _api().read_file(
                self.handle,
                buffer,
                size,
                transferred,
                overlapped,
            )

        with self._io_lock:
            succeeded, count, error = _overlapped_result(
                self.handle,
                operation,
                timeout=self._timeout,
                allowed_errors=frozenset(
                    {
                        _ERROR_BROKEN_PIPE,
                        _ERROR_NO_DATA,
                        _ERROR_PIPE_NOT_CONNECTED,
                        _ERROR_MORE_DATA,
                    }
                ),
            )
        if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED}:
            return b""
        if not succeeded and error != _ERROR_MORE_DATA:
            raise OSError(error, "named-pipe read failed", None, error)
        return buffer.raw[:count]

    def sendall(self, data: object) -> None:
        try:
            view = memoryview(data).cast("B")
        except (TypeError, ValueError) as error:
            raise TypeError("a bytes-like object is required") from error
        with self._io_lock:
            while view:
                chunk = bytes(view[: 64 * 1024])
                buffer = ctypes.create_string_buffer(chunk)

                def operation(overlapped, transferred, buffer=buffer, chunk=chunk):
                    return _api().write_file(
                        self.handle,
                        buffer,
                        len(chunk),
                        transferred,
                        overlapped,
                    )

                succeeded, count, error = _overlapped_result(
                    self.handle,
                    operation,
                    timeout=self._timeout,
                    allowed_errors=frozenset(
                        {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED}
                    ),
                )
                if not succeeded or count <= 0:
                    raise BrokenPipeError(error, "named-pipe peer disconnected")
                view = view[count:]

    def shutdown(self, _how: object = None) -> None:
        if self._closed:
            return
        _api().cancel_io_ex(self._handle, None)
        if self._server_side:
            _api().disconnect_named_pipe(self._handle)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handle = self._handle
        self._handle = 0
        _api().cancel_io_ex(handle, None)
        if self._server_side:
            _api().disconnect_named_pipe(handle)
        _close_handle(handle)


class WindowsNamedPipeListener:
    """One local-only named-pipe listener with bounded overlapped accept."""

    __slots__ = ("_closed", "_ever_accepted", "_lock", "_name", "_pending", "_timeout")

    def __init__(self, name: str) -> None:
        if type(name) is not str or not name.startswith(r"\\.\pipe\vibecad-kernel-"):
            raise OSError("invalid VibeCAD named-pipe name")
        self._name = name
        self._timeout: float | None = None
        self._closed = False
        self._ever_accepted = False
        self._lock = threading.Lock()
        self._pending = self._create_instance(first=True)

    def _create_instance(self, *, first: bool) -> int:
        attributes, descriptor = _security_attributes()
        try:
            open_mode = _PIPE_ACCESS_DUPLEX | _FILE_FLAG_OVERLAPPED
            if first:
                open_mode |= _FILE_FLAG_FIRST_PIPE_INSTANCE
            handle = int(
                _api().create_named_pipe(
                    self._name,
                    open_mode,
                    _PIPE_TYPE_BYTE
                    | _PIPE_READMODE_BYTE
                    | _PIPE_WAIT
                    | _PIPE_REJECT_REMOTE_CLIENTS,
                    _PIPE_UNLIMITED_INSTANCES,
                    64 * 1024,
                    64 * 1024,
                    0,
                    ctypes.byref(attributes),
                )
                or 0
            )
            if not _valid_handle(handle):
                _raise_last_error("CreateNamedPipe failed")
            if not _api().set_handle_information(handle, _HANDLE_FLAG_INHERIT, 0):
                _close_handle(handle)
                _raise_last_error("SetHandleInformation failed")
            return handle
        finally:
            _api().local_free(descriptor)

    def settimeout(self, value: float | None) -> None:
        if value is not None and (
            type(value) not in {int, float} or isinstance(value, bool) or value < 0
        ):
            raise ValueError("timeout must be a non-negative number or None")
        self._timeout = None if value is None else float(value)

    def get_inheritable(self) -> bool:
        with self._lock:
            handle = self._pending
        flags = wintypes.DWORD()
        if not _api().get_handle_information(handle, ctypes.byref(flags)):
            _raise_last_error("GetHandleInformation failed")
        return bool(flags.value & _HANDLE_FLAG_INHERIT)

    def accept(self) -> tuple[WindowsNamedPipeConnection, None]:
        with self._lock:
            if self._closed:
                raise OSError("named-pipe listener is closed")
            if not _valid_handle(self._pending):
                self._pending = self._create_instance(first=not self._ever_accepted)
            handle = self._pending

        def operation(overlapped, _transferred):
            return _api().connect_named_pipe(handle, overlapped)

        try:
            succeeded, _count, error = _overlapped_result(
                handle,
                operation,
                timeout=self._timeout,
                allowed_errors=frozenset({_ERROR_PIPE_CONNECTED}),
            )
            if not succeeded and error != _ERROR_PIPE_CONNECTED:
                raise OSError(error, "ConnectNamedPipe failed", None, error)
        except TimeoutError:
            # A cancelled overlapped ConnectNamedPipe leaves this instance in
            # the listening state.  Retaining it avoids a namespace gap where
            # another same-user process could race to become first instance.
            raise
        with self._lock:
            if self._closed or self._pending != handle:
                _api().disconnect_named_pipe(handle)
                _close_handle(handle)
                raise OSError("named-pipe listener closed during accept")
            self._ever_accepted = True
            try:
                # Keep one unconnected instance alive before publishing this
                # accepted connection to the service.  Concurrent clients can
                # therefore wait without observing a pipe-namespace gap.
                self._pending = self._create_instance(first=False)
            except BaseException:
                self._pending = 0
                _api().disconnect_named_pipe(handle)
                _close_handle(handle)
                raise
        return WindowsNamedPipeConnection(handle, server_side=True), None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle = self._pending
            self._pending = 0
        if _valid_handle(handle):
            _api().cancel_io_ex(handle, None)
            _api().disconnect_named_pipe(handle)
            _close_handle(handle)


def connect_named_pipe(name: str, *, timeout: float) -> WindowsNamedPipeConnection:
    if type(timeout) not in {int, float} or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be positive")
    if type(name) is not str or not name.startswith(r"\\.\pipe\vibecad-kernel-"):
        raise OSError("invalid VibeCAD named-pipe name")
    milliseconds = _deadline_milliseconds(float(timeout))
    api = _api()
    ctypes.set_last_error(0)
    if not api.wait_named_pipe(name, milliseconds):
        error = ctypes.get_last_error()
        if error in {_ERROR_PIPE_BUSY, 2}:
            raise TimeoutError
        raise OSError(error, "WaitNamedPipe failed", None, error)
    handle = int(
        api.create_file(
            name,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_OVERLAPPED | _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION,
            None,
        )
        or 0
    )
    if not _valid_handle(handle):
        _raise_last_error("opening the daemon named pipe failed")
    try:
        mode = wintypes.DWORD(_PIPE_READMODE_BYTE)
        if not api.set_named_pipe_handle_state(handle, ctypes.byref(mode), None, None):
            _raise_last_error("SetNamedPipeHandleState failed")
        return WindowsNamedPipeConnection(handle, server_side=False)
    except BaseException:
        _close_handle(handle)
        raise


def require_same_user_peer(connection: object) -> WindowsPeerIdentity:
    if type(connection) is not WindowsNamedPipeConnection:
        raise OSError("peer identity requires a connected VibeCAD named pipe")
    process, identity = connection.peer_identity()
    try:
        if identity.sid != current_user_sid():
            raise PermissionError(_ERROR_ACCESS_DENIED, "named-pipe peer belongs to another user")
        connection.pin_peer(identity)
        return identity
    finally:
        _close_handle(process)


def require_expected_peer(
    connection: object,
    *,
    pid: int,
    started_ns: int,
) -> WindowsPeerIdentity:
    identity = require_same_user_peer(connection)
    if identity.pid != pid or identity.started_ns != started_ns:
        raise OSError("named-pipe peer is not the published daemon process generation")
    return identity


def encode_handle_envelope(payload: bytes, descriptor: int) -> bytes:
    if type(payload) is not bytes or type(descriptor) is not int or descriptor < 0:
        raise TypeError("invalid handle envelope")
    import msvcrt  # noqa: PLC0415

    handle = msvcrt.get_osfhandle(descriptor)
    if type(handle) is not int or handle <= 0 or handle > 0xFFFFFFFFFFFFFFFF:
        raise OSError("descriptor has no transferable Win32 handle")
    return HANDLE_ENVELOPE_MAGIC + handle.to_bytes(8, "little") + payload


def split_handle_envelope(payload: bytes) -> tuple[bytes, int | None]:
    if type(payload) is not bytes:
        raise TypeError("payload must be bytes")
    if not payload.startswith(HANDLE_ENVELOPE_MAGIC):
        return payload, None
    if len(payload) <= HANDLE_ENVELOPE_BYTES:
        raise OSError("malformed Win32 handle envelope")
    handle = int.from_bytes(payload[len(HANDLE_ENVELOPE_MAGIC) : HANDLE_ENVELOPE_BYTES], "little")
    if handle in {0, _INVALID_HANDLE_VALUE}:
        raise OSError("malformed Win32 source handle")
    return payload[HANDLE_ENVELOPE_BYTES:], handle


def duplicate_handle_from_peer(
    connection: object,
    source_handle: int,
) -> int:
    """Copy a handle out of the authenticated pipe peer and return a CRT fd."""

    if type(connection) is not WindowsNamedPipeConnection or type(source_handle) is not int:
        raise OSError("invalid Win32 handle transfer")
    process, identity = connection.peer_identity(duplicate=True)
    target = wintypes.HANDLE()
    try:
        if identity.sid != current_user_sid():
            raise PermissionError(_ERROR_ACCESS_DENIED, "handle source belongs to another user")
        if not _api().duplicate_handle(
            process,
            source_handle,
            _api().get_current_process(),
            ctypes.byref(target),
            0,
            False,
            _DUPLICATE_SAME_ACCESS,
        ):
            _raise_last_error("DuplicateHandle from pipe peer failed")
        # Re-read the kernel-bound pipe PID after duplication.  The retained
        # process handle and pinned creation time close the PID-reuse window.
        verify_process, after = connection.peer_identity()
        _close_handle(verify_process)
        if after != identity:
            raise OSError("named-pipe peer changed during handle duplication")
        import msvcrt  # noqa: PLC0415

        raw_target = int(target.value or 0)
        descriptor = msvcrt.open_osfhandle(raw_target, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        target.value = None
        os.set_inheritable(descriptor, False)
        return descriptor
    finally:
        if target.value:
            _close_handle(int(target.value))
        _close_handle(process)
