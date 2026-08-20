"""Run one Windows command in a parent-owned kill-on-close Job Object.

The installer cannot safely launch a package manager and assign it to a Job
Object afterwards: the child could create descendants in that gap.  Instead,
the parent launches this module as a gate that only reads one JSON request from
stdin.  The parent assigns the waiting gate to the Job Object first and sends
the request only after assignment succeeds.  Descendants then inherit the job.

Only the parent owns the job handle.  A hard parent exit therefore closes the
last handle and Windows terminates the gate and its complete process tree.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import queue
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

_CREATE_NO_WINDOW = 0x08000000
_HANDLE_FLAG_INHERIT = 0x00000001
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_GATE_FAILURE_EXIT_CODE = 125
_PERSISTENT_GATE_ARGUMENT = "--persistent-gate"


class WindowsJobError(RuntimeError):
    """The guarded Windows command could not be started safely."""


if sys.platform == "win32":
    from ctypes import wintypes

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.SetHandleInformation.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    _KERNEL32.SetHandleInformation.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover - declarations are intentionally Windows-only
    _KERNEL32 = None


def _win32_error(operation: str) -> WindowsJobError:
    code = ctypes.get_last_error()
    return WindowsJobError(f"{operation} failed with Win32 error {code}")


def _create_kill_on_close_job() -> int:
    if _KERNEL32 is None:
        raise WindowsJobError("Windows Job Objects are unavailable on this platform")
    handle = _KERNEL32.CreateJobObjectW(None, None)
    if not handle:
        raise _win32_error("CreateJobObjectW")
    value = int(handle)
    try:
        # CreateJobObjectW already returns a non-inheritable handle when given
        # no SECURITY_ATTRIBUTES.  Make that invariant explicit and fail closed.
        if not _KERNEL32.SetHandleInformation(handle, _HANDLE_FLAG_INHERIT, 0):
            raise _win32_error("SetHandleInformation")
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        information.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _KERNEL32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise _win32_error("SetInformationJobObject")
        return value
    except BaseException:
        _KERNEL32.CloseHandle(handle)
        raise


def _close_handle(handle: int) -> None:
    if _KERNEL32 is not None and handle:
        _KERNEL32.CloseHandle(handle)


def _assign_process_to_job(job_handle: int, process_handle: int) -> None:
    if _KERNEL32 is None:
        raise WindowsJobError("Windows Job Objects are unavailable on this platform")
    if not _KERNEL32.AssignProcessToJobObject(job_handle, process_handle):
        raise _win32_error("AssignProcessToJobObject")


def _normalize_command(command: Sequence[str | os.PathLike[str]]) -> list[str]:
    normalized: list[str] = []
    for value in command:
        try:
            item = os.fspath(value)
        except TypeError as exc:
            raise WindowsJobError("guarded command arguments must be path-like strings") from exc
        if not isinstance(item, str) or "\0" in item:
            raise WindowsJobError("guarded command contains an invalid argument")
        normalized.append(item)
    if not normalized or not normalized[0]:
        raise WindowsJobError("guarded command is empty")
    return normalized


def _normalize_environment(environment: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in environment.items():
        name = str(raw_name)
        value = str(raw_value)
        if not name or "=" in name or "\0" in name or "\0" in value:
            raise WindowsJobError("guarded command environment is invalid")
        normalized[name] = value
    return normalized


def _request_bytes(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None,
    environment: Mapping[str, str],
) -> tuple[list[str], bytes]:
    normalized_command = _normalize_command(command)
    normalized_cwd = None if cwd is None else os.fspath(cwd)
    if normalized_cwd is not None and (
        not isinstance(normalized_cwd, str) or "\0" in normalized_cwd
    ):
        raise WindowsJobError("guarded command working directory is invalid")
    payload = {
        "schema": 1,
        "command": normalized_command,
        "cwd": normalized_cwd,
        "environment": _normalize_environment(environment),
    }
    # ASCII JSON also round-trips any unpaired UTF-16 surrogate that Windows
    # may expose through a path or environment value.
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    encoded += b"\n"
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise WindowsJobError("guarded command request is too large")
    return normalized_command, encoded


def _gate_environment() -> dict[str, str]:
    environment = {
        str(name): str(value)
        for name, value in os.environ.items()
        if str(name).upper() not in {"PYTHONHOME", "PYTHONPATH"}
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _base_python_launcher() -> str:
    raw_launcher = getattr(sys, "_base_executable", None)
    if raw_launcher is None:
        raise WindowsJobError("base Python launcher is unavailable for the Windows job gate")
    try:
        launcher = os.fspath(raw_launcher)
    except TypeError as exc:
        raise WindowsJobError("base Python launcher is invalid") from exc
    if not isinstance(launcher, str) or not launcher or not os.path.isabs(launcher):
        raise WindowsJobError("base Python launcher is not an absolute path")
    # A venv python.exe on Windows can be a redirector which starts the real
    # interpreter before AssignProcessToJobObject can run.  Never fall back to
    # that path when CPython did not publish a distinct base executable.
    if sys.prefix != sys.base_prefix and os.path.normcase(launcher) == os.path.normcase(
        os.fspath(sys.executable)
    ):
        raise WindowsJobError("base Python launcher resolves to a virtualenv redirector")
    path = Path(launcher)
    try:
        info = path.lstat()
        is_junction = getattr(path, "is_junction", None)
        unsafe_alias = path.is_symlink() or (
            is_junction is not None and bool(is_junction())
        )
    except OSError as exc:
        raise WindowsJobError("base Python launcher is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or unsafe_alias or not os.access(path, os.X_OK):
        raise WindowsJobError("base Python launcher is not a real executable file")
    return launcher


def _gate_command(mode: str = "--gate") -> list[str]:
    if mode not in {"--gate", _PERSISTENT_GATE_ARGUMENT}:
        raise WindowsJobError("Windows job gate mode is invalid")
    launcher = _base_python_launcher()
    module_file = os.fspath(Path(__file__).resolve())
    if not os.path.isabs(module_file):
        raise WindowsJobError("isolated Windows job gate launcher is unavailable")
    # The command and environment for the real process are never placed on the
    # gate command line.  Isolated mode plus an absolute script path avoids
    # PYTHONPATH/current-directory module substitution.
    return [launcher, "-I", "-B", module_file, mode]


def _startup_info_for_handles(handles: Sequence[int]):
    if sys.platform != "win32":
        raise WindowsJobError("Windows handle inheritance is unavailable")
    normalized = []
    for handle in handles:
        if type(handle) is not int or handle <= 0:
            raise WindowsJobError("an inherited Windows handle is invalid")
        normalized.append(handle)
    startup = subprocess.STARTUPINFO()
    startup.lpAttributeList = {"handle_list": normalized}
    return startup


def _set_handles_inheritable(handles: Sequence[int], inheritable: bool) -> None:
    if _KERNEL32 is None:
        raise WindowsJobError("Windows handle inheritance is unavailable")
    flags = _HANDLE_FLAG_INHERIT if inheritable else 0
    for handle in handles:
        if not _KERNEL32.SetHandleInformation(
            ctypes.c_void_p(handle),
            _HANDLE_FLAG_INHERIT,
            flags,
        ):
            raise _win32_error("SetHandleInformation")


def _stop_gate(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None:
        with contextlib.suppress(OSError, ValueError):
            process.stdin.close()
        process.stdin = None
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=10)
    if process.stdout is not None:
        with contextlib.suppress(OSError):
            process.stdout.close()


def run_in_job(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str],
    before_dispatch: Callable[[], None] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` in a Windows kill-on-close process tree.

    ``environment`` is mandatory and is the exact environment supplied to the
    real command.  ``before_dispatch`` runs after successful job assignment and
    immediately before the JSON request is written, allowing callers to renew
    identity guards without exposing callbacks to a child process.
    """

    if sys.platform != "win32":
        raise WindowsJobError("Windows Job Objects are unavailable on this platform")
    normalized_command, request = _request_bytes(
        command,
        cwd=cwd,
        environment=environment,
    )
    job_handle = _create_kill_on_close_job()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            _gate_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_gate_environment(),
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        if process.stdin is None or process.stdout is None:
            raise WindowsJobError("Windows job gate pipes are unavailable")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            raise WindowsJobError("Windows job gate process handle is unavailable")
        # This must precede both before_dispatch and the first request byte.
        _assign_process_to_job(job_handle, int(process_handle))
        if before_dispatch is not None:
            before_dispatch()
        try:
            output, _ = process.communicate(request, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Closing the sole job handle terminates the gate and every child.
            _close_handle(job_handle)
            job_handle = 0
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                trailing, _ = process.communicate(timeout=10)
                if trailing:
                    output = trailing
            captured = locals().get("output", exc.output or b"")
            if isinstance(captured, bytes):
                captured = captured.decode("utf-8", "replace")
            raise subprocess.TimeoutExpired(
                normalized_command,
                timeout,
                output=captured,
            ) from exc
        return subprocess.CompletedProcess(
            normalized_command,
            process.returncode,
            stdout=output.decode("utf-8", "replace"),
            stderr=None,
        )
    except OSError as exc:
        _close_handle(job_handle)
        job_handle = 0
        if process is not None:
            _stop_gate(process)
        raise WindowsJobError(f"Windows job gate I/O failed: {exc}") from exc
    except BaseException:
        # If assignment succeeded, handle closure is sufficient and atomic for
        # the whole tree.  If it failed, the gate has received no request and is
        # explicitly stopped below.
        _close_handle(job_handle)
        job_handle = 0
        if process is not None:
            _stop_gate(process)
        raise
    finally:
        _close_handle(job_handle)


def _persistent_request_bytes(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None,
    environment: Mapping[str, str],
    socket_handles: Sequence[int],
) -> tuple[list[str], bytes]:
    normalized_command = _normalize_command(command)
    normalized_cwd = None if cwd is None else os.fspath(cwd)
    if normalized_cwd is not None and (
        not isinstance(normalized_cwd, str) or "\0" in normalized_cwd
    ):
        raise WindowsJobError("guarded command working directory is invalid")
    handles = []
    for value in socket_handles:
        if type(value) is not int or value <= 0:
            raise WindowsJobError("guarded command inherited socket is invalid")
        handles.append(value)
    if not handles or len(set(handles)) != len(handles) or len(handles) > 8:
        raise WindowsJobError("guarded command inherited socket set is invalid")
    payload = {
        "schema": 2,
        "command": normalized_command,
        "cwd": normalized_cwd,
        "environment": _normalize_environment(environment),
        "socket_handles": handles,
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    encoded += b"\n"
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise WindowsJobError("guarded command request is too large")
    return normalized_command, encoded


class WindowsJobProcess:
    """One persistent process tree whose lifetime is anchored by a Job Object."""

    __slots__ = ("_command", "_gate", "_job_handle", "_lock", "_pid")

    def __init__(
        self,
        *,
        command: Sequence[str],
        gate: subprocess.Popen[bytes],
        job_handle: int,
        pid: int,
    ) -> None:
        if not command or type(pid) is not int or pid <= 0 or job_handle <= 0:
            raise WindowsJobError("persistent Windows job process is invalid")
        self._command = tuple(command)
        self._gate = gate
        self._job_handle = job_handle
        self._pid = pid
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self._pid

    def bind_runtime_pid(self, pid: int) -> None:
        """Publish the authenticated interpreter PID behind a venv launcher."""

        if type(pid) is not int or pid <= 0:
            raise WindowsJobError("persistent Windows job runtime PID is invalid")
        with self._lock:
            if self._gate.poll() is not None or self._job_handle <= 0:
                raise WindowsJobError("persistent Windows job exited before PID binding")
            self._pid = pid

    @property
    def started(self) -> bool:
        return True

    @property
    def launch_primitive(self) -> str:
        return "windows_job"

    @property
    def identity_released(self) -> bool:
        return self.poll() is not None

    def _release_job(self) -> None:
        handle = self._job_handle
        self._job_handle = 0
        _close_handle(handle)

    def poll(self) -> int | None:
        with self._lock:
            result = self._gate.poll()
            if result is not None:
                # A failed gate must never strand a descendant in the still-open
                # job.  Closing the sole parent handle converges the whole tree.
                self._release_job()
            return result

    def exited_without_reaping(self) -> bool:
        return self.poll() is not None

    def tree_exists(self) -> bool:
        return self.poll() is None

    def wait(self, timeout: float) -> int:
        if type(timeout) not in {int, float} or timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            result = self._gate.wait(timeout=float(timeout))
        finally:
            with self._lock:
                if self._gate.poll() is not None:
                    self._release_job()
        return result

    def terminate_tree(self, *, timeout: float = 10.0) -> None:
        with self._lock:
            self._release_job()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self._gate.wait(timeout=timeout)

    def close(self) -> None:
        self.terminate_tree()


def _read_gate_handshake(
    pipe,
    *,
    timeout: float,
) -> bytes:
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            result.put((True, pipe.readline(_MAX_REQUEST_BYTES + 1)))
        except BaseException as exc:  # noqa: BLE001 - delivered to the owner
            result.put((False, exc))

    reader = threading.Thread(target=read, name="vibecad-windows-job-handshake", daemon=True)
    reader.start()
    try:
        ok, value = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise WindowsJobError("persistent Windows job gate handshake timed out") from exc
    if not ok:
        raise WindowsJobError("persistent Windows job gate handshake failed") from value
    if not isinstance(value, bytes):
        raise WindowsJobError("persistent Windows job gate handshake is invalid")
    return value


def spawn_persistent_in_job(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str],
    socket_handles: Sequence[int],
    startup_timeout: float = 15.0,
) -> WindowsJobProcess:
    """Spawn a persistent Windows tree after assignment to a private Job Object.

    ``socket_handles`` are inherited by the waiting gate and then by exactly the
    real child.  The gate closes its copies immediately after creation.  The
    caller must close its local child-side socket after this function returns.
    """

    if sys.platform != "win32":
        raise WindowsJobError("Windows Job Objects are unavailable on this platform")
    if type(startup_timeout) not in {int, float} or startup_timeout <= 0:
        raise WindowsJobError("persistent Windows job startup timeout is invalid")
    normalized_command, request = _persistent_request_bytes(
        command,
        cwd=cwd,
        environment=environment,
        socket_handles=socket_handles,
    )
    handles = tuple(int(value) for value in socket_handles)
    job_handle = _create_kill_on_close_job()
    gate: subprocess.Popen[bytes] | None = None
    inherited = False
    try:
        _set_handles_inheritable(handles, True)
        inherited = True
        gate = subprocess.Popen(
            _gate_command(_PERSISTENT_GATE_ARGUMENT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=_gate_environment(),
            close_fds=True,
            startupinfo=_startup_info_for_handles(handles),
            creationflags=_CREATE_NO_WINDOW,
        )
        _set_handles_inheritable(handles, False)
        inherited = False
        if gate.stdin is None or gate.stdout is None:
            raise WindowsJobError("persistent Windows job gate pipes are unavailable")
        process_handle = getattr(gate, "_handle", None)
        if process_handle is None:
            raise WindowsJobError("persistent Windows job gate process handle is unavailable")
        _assign_process_to_job(job_handle, int(process_handle))
        gate.stdin.write(request)
        gate.stdin.flush()
        gate.stdin.close()
        gate.stdin = None
        raw = _read_gate_handshake(gate.stdout, timeout=float(startup_timeout))
        try:
            payload = json.loads(raw.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WindowsJobError("persistent Windows job gate returned invalid JSON") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != 1
            or type(payload.get("pid")) is not int
            or int(payload["pid"]) <= 0
        ):
            raise WindowsJobError("persistent Windows job gate returned invalid identity")
        result = WindowsJobProcess(
            command=normalized_command,
            gate=gate,
            job_handle=job_handle,
            pid=int(payload["pid"]),
        )
        gate = None
        job_handle = 0
        return result
    except OSError as exc:
        raise WindowsJobError(f"persistent Windows job gate I/O failed: {exc}") from exc
    finally:
        if inherited:
            with contextlib.suppress(BaseException):
                _set_handles_inheritable(handles, False)
        _close_handle(job_handle)
        if gate is not None:
            _stop_gate(gate)


def _validated_gate_request(raw: bytes) -> tuple[list[str], str | None, dict[str, str]]:
    if not raw or len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise WindowsJobError("Windows job gate request is incomplete")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WindowsJobError("Windows job gate request is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        raise WindowsJobError("Windows job gate request schema is invalid")
    command = payload.get("command")
    environment = payload.get("environment")
    cwd = payload.get("cwd")
    if not isinstance(command, list) or not isinstance(environment, dict):
        raise WindowsJobError("Windows job gate request fields are invalid")
    if cwd is not None and not isinstance(cwd, str):
        raise WindowsJobError("Windows job gate working directory is invalid")
    return (
        _normalize_command(command),
        cwd,
        _normalize_environment(environment),
    )


def _validated_persistent_gate_request(
    raw: bytes,
) -> tuple[list[str], str | None, dict[str, str], tuple[int, ...]]:
    if not raw or len(raw) > _MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
        raise WindowsJobError("persistent Windows job gate request is incomplete")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WindowsJobError("persistent Windows job gate request is invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "command",
        "cwd",
        "environment",
        "socket_handles",
    }:
        raise WindowsJobError("persistent Windows job gate request shape is invalid")
    if payload.get("schema") != 2:
        raise WindowsJobError("persistent Windows job gate request schema is invalid")
    command = payload.get("command")
    environment = payload.get("environment")
    cwd = payload.get("cwd")
    handles = payload.get("socket_handles")
    if (
        not isinstance(command, list)
        or not isinstance(environment, dict)
        or not isinstance(handles, list)
        or not handles
        or len(handles) > 8
        or any(type(value) is not int or value <= 0 for value in handles)
        or len(set(handles)) != len(handles)
    ):
        raise WindowsJobError("persistent Windows job gate request fields are invalid")
    if cwd is not None and not isinstance(cwd, str):
        raise WindowsJobError("persistent Windows job gate working directory is invalid")
    return (
        _normalize_command(command),
        cwd,
        _normalize_environment(environment),
        tuple(handles),
    )


def _gate_main() -> int:
    try:
        raw = sys.stdin.buffer.readline(_MAX_REQUEST_BYTES + 1)
        command, cwd, environment = _validated_gate_request(raw)
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
        )
        return child.wait()
    except BaseException as exc:  # noqa: BLE001 - gate must fail closed with a diagnostic
        message = f"VibeCAD Windows job gate failed: {type(exc).__name__}: {exc}\n"
        with contextlib.suppress(OSError):
            sys.stderr.buffer.write(message.encode("utf-8", "replace")[-2000:])
            sys.stderr.buffer.flush()
        return _GATE_FAILURE_EXIT_CODE


def _close_inherited_socket(handle: int) -> None:
    connection = socket.socket(fileno=handle)
    connection.close()


def _persistent_gate_main() -> int:
    handles: tuple[int, ...] = ()
    child: subprocess.Popen[bytes] | None = None
    try:
        raw = sys.stdin.buffer.readline(_MAX_REQUEST_BYTES + 1)
        command, cwd, environment, handles = _validated_persistent_gate_request(raw)
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            startupinfo=_startup_info_for_handles(handles),
            creationflags=_CREATE_NO_WINDOW,
        )
        for handle in handles:
            _close_inherited_socket(handle)
        handles = ()
        sys.stdout.buffer.write(
            json.dumps(
                {"schema": 1, "pid": child.pid},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        sys.stdout.buffer.flush()
        return child.wait()
    except BaseException as exc:  # noqa: BLE001 - gate must fail closed
        message = f"VibeCAD persistent Windows job gate failed: {type(exc).__name__}: {exc}\n"
        with contextlib.suppress(OSError):
            sys.stdout.buffer.write(message.encode("utf-8", "replace")[-2000:])
            sys.stdout.buffer.flush()
        if child is not None and child.poll() is None:
            with contextlib.suppress(OSError):
                child.kill()
        return _GATE_FAILURE_EXIT_CODE
    finally:
        for handle in handles:
            with contextlib.suppress(OSError):
                _close_inherited_socket(handle)


if __name__ == "__main__":
    if sys.platform == "win32" and sys.argv[1:] == ["--gate"]:
        raise SystemExit(_gate_main())
    if sys.platform == "win32" and sys.argv[1:] == [_PERSISTENT_GATE_ARGUMENT]:
        raise SystemExit(_persistent_gate_main())
    raise SystemExit(_GATE_FAILURE_EXIT_CODE)
