"""Fresh-process lifecycle and watchdog for one FreeCAD Worker generation."""

from __future__ import annotations

import array
import contextlib
import ctypes
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Final

from vibecad.worker.codec import (
    MAX_WORKER_RESPONSE_BYTES,
    WorkerCodecError,
    WorkerWireErrorCode,
    decode_worker_response,
    encode_worker_request,
)

_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")
_TERMINATION_LIMIT_SECONDS = 5.0
_TERM_GRACE_SECONDS = 0.25
_PATH_TYPE = type(Path())
_PROTOCOL_FD: Final = 3
_POSIX_SPAWN_SETSIGDEF: Final = 0x0004
_POSIX_SPAWN_SETSIGMASK: Final = 0x0008
_POSIX_SPAWN_SETSID: Final = 0x0400
_POSIX_SPAWN_CLOEXEC_DEFAULT: Final = 0x4000
_STARTUP_CLEANUP_POLL_SECONDS = 0.05
_STARTUP_CLEANUP_LOCK = threading.Lock()
_STARTUP_CLEANUP_CONDITION = threading.Condition(_STARTUP_CLEANUP_LOCK)
_STARTUP_CLEANUP: dict[str, _WorkerProcess] = {}
_STARTUP_CLEANUP_SWEEPER: threading.Thread | None = None
_STARTUP_CLEANUP_SWEEPER_READY: threading.Event | None = None


class WorkerGenerationState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    TERMINATING = "terminating"
    DEAD = "dead"
    CLEANUP_REQUIRED = "cleanup_required"


class WorkerErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_HANDLE = "invalid_handle"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    INVALID_CANDIDATE = "invalid_candidate"
    CAD_FAILURE = "cad_failure"
    ARTIFACT_FAILURE = "artifact_failure"
    INTEGRITY_FAILURE = "integrity_failure"
    START_FAILED = "start_failed"
    GENERATION_LOST = "generation_lost"
    CLOSED = "closed"


_MESSAGES = {
    WorkerErrorCode.INVALID_INPUT: "The Worker input is invalid.",
    WorkerErrorCode.INVALID_HANDLE: "The Worker capability is invalid.",
    WorkerErrorCode.RESOURCE_EXHAUSTED: "Worker capacity is exhausted.",
    WorkerErrorCode.INVALID_CANDIDATE: "The Worker candidate is invalid.",
    WorkerErrorCode.CAD_FAILURE: "The Worker CAD operation failed.",
    WorkerErrorCode.ARTIFACT_FAILURE: "The Worker artifact operation failed.",
    WorkerErrorCode.INTEGRITY_FAILURE: "The Worker integrity check failed.",
    WorkerErrorCode.START_FAILED: "The Worker generation could not start.",
    WorkerErrorCode.GENERATION_LOST: "The Worker generation was lost.",
    WorkerErrorCode.CLOSED: "The Worker generation is closed.",
}


class WorkerError(RuntimeError):
    __slots__ = ("code", "message", "schema_version", "uncertain")

    def __init__(self, code: WorkerErrorCode) -> None:
        if type(code) is not WorkerErrorCode:
            raise TypeError("code must be a WorkerErrorCode")
        self.schema_version = 1
        self.code = code
        self.message = _MESSAGES[code]
        self.uncertain = code is WorkerErrorCode.GENERATION_LOST
        self.args = (self.message,)


class _ChildIdentityReleased(OSError):
    """The owned child PID can no longer anchor its process-group identity."""


_WIRE_ERRORS = {
    WorkerWireErrorCode.INVALID_REQUEST: WorkerErrorCode.INVALID_INPUT,
    WorkerWireErrorCode.INVALID_HANDLE: WorkerErrorCode.INVALID_HANDLE,
    WorkerWireErrorCode.RESOURCE_EXHAUSTED: WorkerErrorCode.RESOURCE_EXHAUSTED,
    WorkerWireErrorCode.INVALID_INPUT: WorkerErrorCode.INVALID_INPUT,
    WorkerWireErrorCode.INVALID_CANDIDATE: WorkerErrorCode.INVALID_CANDIDATE,
    WorkerWireErrorCode.CAD_FAILURE: WorkerErrorCode.CAD_FAILURE,
    WorkerWireErrorCode.ARTIFACT_FAILURE: WorkerErrorCode.ARTIFACT_FAILURE,
    WorkerWireErrorCode.INTEGRITY_FAILURE: WorkerErrorCode.INTEGRITY_FAILURE,
}


def _private_child_directory(parent: Path, name: str) -> Path:
    child = parent / name
    child.mkdir(mode=0o700)
    child.chmod(0o700)
    return child


def _remove_private_home(home: Path) -> bool:
    shutil.rmtree(home, ignore_errors=True)
    try:
        home.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _minimal_environment(*, source_root: Path, home: Path, python: Path) -> dict[str, str]:
    prefix_bin = python.parent
    freecad_root = _private_child_directory(home, "freecad-user")
    freecad_home = _private_child_directory(freecad_root, "home")
    freecad_data = _private_child_directory(freecad_root, "data")
    freecad_temp = _private_child_directory(freecad_root, "temp")
    return {
        "FREECAD_USER_DATA": str(freecad_data),
        "FREECAD_USER_HOME": str(freecad_home),
        "FREECAD_USER_TEMP": str(freecad_temp),
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.pathsep.join((str(prefix_bin), "/usr/bin", "/bin")),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(source_root),
        "PYTHONUNBUFFERED": "1",
        "TMPDIR": str(home),
    }


class _SpawnedProcess:
    """Minimal waitpid wrapper for a process created directly by posix_spawn."""

    __slots__ = ("_argv", "_externally_reaped", "_lock", "_pid", "_returncode")

    def __init__(self, *, argv: tuple[str, ...]) -> None:
        self._argv = argv
        self._lock = threading.Lock()
        self._pid = ctypes.c_int()
        self._returncode: int | None = None
        self._externally_reaped = False

    @property
    def started(self) -> bool:
        return self._pid.value > 0

    @property
    def pid(self) -> int:
        value = self._pid.value
        if value <= 0:
            raise OSError("Worker child has not started")
        return value

    @property
    def identity_released(self) -> bool:
        with self._lock:
            return self._returncode is not None or self._externally_reaped

    @property
    def launch_primitive(self) -> str:
        return "posix_spawn"

    def exited_without_reaping(self) -> bool:
        """Observe child exit while retaining its PID against reuse."""

        with self._lock:
            if self._returncode is not None or self._externally_reaped:
                raise _ChildIdentityReleased("Worker child identity was already released")
            try:
                result = os.waitid(
                    os.P_PID,
                    self.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except ChildProcessError:
                self._externally_reaped = True
                raise _ChildIdentityReleased("Worker child was reaped outside its owner") from None
            return result is not None

    def poll(self) -> int | None:
        with self._lock:
            if self._returncode is not None:
                return self._returncode
            try:
                waited, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self._externally_reaped = True
                raise OSError("Worker child was reaped outside its owner") from None
            if waited == 0:
                return None
            self._returncode = os.waitstatus_to_exitcode(status)
            return self._returncode

    def wait(self, timeout: float) -> int:
        if type(timeout) not in {int, float} or timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + float(timeout)
        while True:
            result = self.poll()
            if result is not None:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self._argv, timeout)
            time.sleep(min(0.005, remaining))


def _spawn_call(code: int) -> None:
    if code != 0:
        raise OSError(code, os.strerror(code))


def _fresh_posix_spawn(
    *,
    process: _SpawnedProcess,
    argv: tuple[str, ...],
    env: dict[str, str],
    cwd: Path,
    parent_fd: int,
    child_fd: int,
) -> _SpawnedProcess:
    """Spawn without running Python code between fork and exec.

    Darwin's CLOEXEC_DEFAULT extension makes every descriptor close-on-exec
    unless a file action explicitly opens or duplicates it.  The resulting
    child gets only stdio backed by ``/dev/null`` and protocol descriptor 3.
    """

    if (
        os.sys.platform != "darwin"
        or type(process) is not _SpawnedProcess
        or process.started
        or type(argv) is not tuple
        or not argv
        or not all(type(item) is str and item and "\0" not in item for item in argv)
        or type(env) is not dict
        or not all(
            type(key) is str
            and type(value) is str
            and key
            and "\0" not in key
            and "\0" not in value
            for key, value in env.items()
        )
        or type(cwd) is not _PATH_TYPE
        or not cwd.is_absolute()
        or type(parent_fd) is not int
        or type(child_fd) is not int
        or min(parent_fd, child_fd) < 0
    ):
        raise OSError("fresh posix_spawn is unavailable")

    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file():
        raise OSError("Worker executable is unavailable")

    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    attribute_type = ctypes.c_void_p
    actions_type = ctypes.c_void_p
    required = (
        "posix_spawn",
        "posix_spawnattr_init",
        "posix_spawnattr_setsigdefault",
        "posix_spawnattr_setsigmask",
        "posix_spawnattr_setflags",
        "posix_spawnattr_destroy",
        "posix_spawn_file_actions_init",
        "posix_spawn_file_actions_addclose",
        "posix_spawn_file_actions_adddup2",
        "posix_spawn_file_actions_addopen",
        "posix_spawn_file_actions_addchdir_np",
        "posix_spawn_file_actions_destroy",
        "sigemptyset",
        "sigfillset",
    )
    if not all(hasattr(library, name) for name in required):
        raise OSError("fresh posix_spawn is unavailable")

    library.posix_spawnattr_init.argtypes = [ctypes.POINTER(attribute_type)]
    library.posix_spawnattr_init.restype = ctypes.c_int
    library.posix_spawnattr_setflags.argtypes = [
        ctypes.POINTER(attribute_type),
        ctypes.c_short,
    ]
    library.posix_spawnattr_setflags.restype = ctypes.c_int
    signal_set_type = ctypes.c_uint32
    library.posix_spawnattr_setsigdefault.argtypes = [
        ctypes.POINTER(attribute_type),
        ctypes.POINTER(signal_set_type),
    ]
    library.posix_spawnattr_setsigdefault.restype = ctypes.c_int
    library.posix_spawnattr_setsigmask.argtypes = [
        ctypes.POINTER(attribute_type),
        ctypes.POINTER(signal_set_type),
    ]
    library.posix_spawnattr_setsigmask.restype = ctypes.c_int
    library.posix_spawnattr_destroy.argtypes = [ctypes.POINTER(attribute_type)]
    library.posix_spawnattr_destroy.restype = ctypes.c_int
    library.posix_spawn_file_actions_init.argtypes = [ctypes.POINTER(actions_type)]
    library.posix_spawn_file_actions_init.restype = ctypes.c_int
    library.posix_spawn_file_actions_addclose.argtypes = [
        ctypes.POINTER(actions_type),
        ctypes.c_int,
    ]
    library.posix_spawn_file_actions_addclose.restype = ctypes.c_int
    library.posix_spawn_file_actions_adddup2.argtypes = [
        ctypes.POINTER(actions_type),
        ctypes.c_int,
        ctypes.c_int,
    ]
    library.posix_spawn_file_actions_adddup2.restype = ctypes.c_int
    library.posix_spawn_file_actions_addopen.argtypes = [
        ctypes.POINTER(actions_type),
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint16,
    ]
    library.posix_spawn_file_actions_addopen.restype = ctypes.c_int
    library.posix_spawn_file_actions_addchdir_np.argtypes = [
        ctypes.POINTER(actions_type),
        ctypes.c_char_p,
    ]
    library.posix_spawn_file_actions_addchdir_np.restype = ctypes.c_int
    library.posix_spawn_file_actions_destroy.argtypes = [ctypes.POINTER(actions_type)]
    library.posix_spawn_file_actions_destroy.restype = ctypes.c_int
    library.posix_spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        ctypes.POINTER(actions_type),
        ctypes.POINTER(attribute_type),
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]
    library.posix_spawn.restype = ctypes.c_int
    library.sigemptyset.argtypes = [ctypes.POINTER(signal_set_type)]
    library.sigemptyset.restype = ctypes.c_int
    library.sigfillset.argtypes = [ctypes.POINTER(signal_set_type)]
    library.sigfillset.restype = ctypes.c_int

    attributes = attribute_type()
    actions = actions_type()
    attributes_ready = False
    actions_ready = False
    try:
        _spawn_call(library.posix_spawnattr_init(ctypes.byref(attributes)))
        attributes_ready = True
        _spawn_call(
            library.posix_spawnattr_setflags(
                ctypes.byref(attributes),
                _POSIX_SPAWN_SETSIGDEF
                | _POSIX_SPAWN_SETSIGMASK
                | _POSIX_SPAWN_SETSID
                | _POSIX_SPAWN_CLOEXEC_DEFAULT,
            )
        )
        default_signals = signal_set_type()
        empty_mask = signal_set_type()
        _spawn_call(library.sigfillset(ctypes.byref(default_signals)))
        _spawn_call(library.sigemptyset(ctypes.byref(empty_mask)))
        _spawn_call(
            library.posix_spawnattr_setsigdefault(
                ctypes.byref(attributes),
                ctypes.byref(default_signals),
            )
        )
        _spawn_call(
            library.posix_spawnattr_setsigmask(
                ctypes.byref(attributes),
                ctypes.byref(empty_mask),
            )
        )
        _spawn_call(library.posix_spawn_file_actions_init(ctypes.byref(actions)))
        actions_ready = True
        _spawn_call(
            library.posix_spawn_file_actions_addclose(
                ctypes.byref(actions),
                parent_fd,
            )
        )
        _spawn_call(
            library.posix_spawn_file_actions_adddup2(
                ctypes.byref(actions),
                child_fd,
                _PROTOCOL_FD,
            )
        )
        if child_fd != _PROTOCOL_FD:
            _spawn_call(
                library.posix_spawn_file_actions_addclose(
                    ctypes.byref(actions),
                    child_fd,
                )
            )
        _spawn_call(
            library.posix_spawn_file_actions_addchdir_np(
                ctypes.byref(actions),
                os.fsencode(cwd),
            )
        )
        for descriptor, flags in (
            (0, os.O_RDONLY),
            (1, os.O_WRONLY),
            (2, os.O_WRONLY),
        ):
            _spawn_call(
                library.posix_spawn_file_actions_addopen(
                    ctypes.byref(actions),
                    descriptor,
                    b"/dev/null",
                    flags,
                    0,
                )
            )

        encoded_argv = tuple(os.fsencode(item) for item in argv)
        encoded_env = tuple(os.fsencode(f"{key}={value}") for key, value in sorted(env.items()))
        argv_array = (ctypes.c_char_p * (len(encoded_argv) + 1))(
            *encoded_argv,
            None,
        )
        env_array = (ctypes.c_char_p * (len(encoded_env) + 1))(
            *encoded_env,
            None,
        )
        _spawn_call(
            library.posix_spawn(
                ctypes.byref(process._pid),
                encoded_argv[0],
                ctypes.byref(actions),
                ctypes.byref(attributes),
                argv_array,
                env_array,
            )
        )
        if not process.started:
            raise OSError("posix_spawn returned an invalid pid")
        return process
    finally:
        if actions_ready:
            with contextlib.suppress(Exception):
                library.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        if attributes_ready:
            with contextlib.suppress(Exception):
                library.posix_spawnattr_destroy(ctypes.byref(attributes))


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _recv_exact(connection: socket.socket, size: int, *, deadline: float) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        connection.settimeout(_remaining(deadline))
        fragment = connection.recv(remaining)
        if not fragment:
            raise EOFError
        chunks.append(fragment)
        remaining -= len(fragment)
    return b"".join(chunks)


def _send_frame(
    connection: socket.socket,
    raw: bytes,
    *,
    deadline: float,
    capability_fd: int | None,
) -> None:
    frame = struct.pack(">I", len(raw)) + raw
    connection.settimeout(_remaining(deadline))
    if capability_fd is None:
        connection.sendall(frame)
        return
    if type(capability_fd) is not int or capability_fd < 0 or not hasattr(connection, "sendmsg"):
        raise OSError("invalid Worker capability descriptor")
    rights = array.array("i", (capability_fd,))
    sent = connection.sendmsg(
        (frame,),
        ((socket.SOL_SOCKET, socket.SCM_RIGHTS, rights),),
    )
    if sent <= 0:
        raise OSError("Worker capability send failed")
    if sent < len(frame):
        connection.settimeout(_remaining(deadline))
        connection.sendall(frame[sent:])


def _receive_frame(connection: socket.socket, *, deadline: float) -> bytes:
    header = _recv_exact(connection, 4, deadline=deadline)
    size = struct.unpack(">I", header)[0]
    if size <= 0 or size > MAX_WORKER_RESPONSE_BYTES:
        raise WorkerCodecError("invalid Worker response frame")
    return _recv_exact(connection, size, deadline=deadline)


class _WorkerProcess:
    """Process-owned private channel; every uncertain result kills its group."""

    __slots__ = (
        "_connection",
        "_generation_id",
        "_home",
        "_io_lock",
        "_lifecycle_lock",
        "_process",
        "_shutdown_timeout_ms",
        "_state",
        "_termination_lock",
        "_test_timeout_cap_ms",
    )

    def __init__(
        self,
        *,
        process: _SpawnedProcess,
        connection: socket.socket,
        generation_id: str,
        home: Path,
        shutdown_timeout_ms: int,
        test_timeout_cap_ms: int | None,
    ) -> None:
        self._process = process
        self._connection = connection
        self._generation_id = generation_id
        self._home = home
        self._shutdown_timeout_ms = shutdown_timeout_ms
        self._test_timeout_cap_ms = test_timeout_cap_ms
        self._io_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._termination_lock = threading.Lock()
        self._state = WorkerGenerationState.STARTING

    @classmethod
    def _spawn(
        cls,
        *,
        command: tuple[str, ...],
        source_root: Path,
        readiness_timeout_ms: int,
        shutdown_timeout_ms: int,
        test_timeout_cap_ms: int | None = None,
    ) -> _WorkerProcess:
        if (
            type(command) is not tuple
            or not command
            or not all(type(item) is str and item for item in command)
            or type(source_root) is not _PATH_TYPE
            or not source_root.is_absolute()
            or not source_root.is_dir()
            or type(readiness_timeout_ms) is not int
            or readiness_timeout_ms <= 0
            or readiness_timeout_ms > 15_000
            or type(shutdown_timeout_ms) is not int
            or shutdown_timeout_ms <= 0
            or shutdown_timeout_ms > 5_000
        ):
            raise WorkerError(WorkerErrorCode.INVALID_INPUT)
        _ensure_startup_cleanup_sweeper()
        generation_id = f"worker_generation_{secrets.token_hex(16)}"
        parent = child = None
        process = None
        instance = None
        home = None
        try:
            home = Path(tempfile.mkdtemp(prefix="vibecad-worker-"))
            home.chmod(0o700)
            parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            python = Path(command[0])
            env = _minimal_environment(
                source_root=source_root,
                home=home,
                python=python,
            )
            argv = (
                *command,
                "--protocol-fd",
                str(_PROTOCOL_FD),
                "--generation-id",
                generation_id,
            )
            process = _SpawnedProcess(argv=argv)
            instance = cls(
                process=process,
                connection=parent,
                generation_id=generation_id,
                home=home,
                shutdown_timeout_ms=shutdown_timeout_ms,
                test_timeout_cap_ms=test_timeout_cap_ms,
            )
            parent = None
            _fresh_posix_spawn(
                process=process,
                argv=argv,
                env=env,
                cwd=home,
                parent_fd=instance._connection.fileno(),
                child_fd=child.fileno(),
            )
            child.close()
            child = None
            try:
                if os.getpgid(process.pid) != process.pid or os.getsid(process.pid) != process.pid:
                    raise WorkerError(WorkerErrorCode.START_FAILED)
                ready = instance._rpc(
                    "worker.ready",
                    {},
                    timeout_ms=readiness_timeout_ms,
                    allow_starting=True,
                )
                if (
                    set(ready)
                    != {
                        "worker_pid",
                        "python_version",
                        "freecad_version",
                    }
                    or type(ready["worker_pid"]) is not int
                    or ready["worker_pid"] != process.pid
                    or type(ready["python_version"]) is not str
                    or _VERSION.fullmatch(ready["python_version"]) is None
                    or type(ready["freecad_version"]) is not str
                    or _VERSION.fullmatch(ready["freecad_version"]) is None
                ):
                    raise WorkerError(WorkerErrorCode.START_FAILED)
                with instance._lifecycle_lock:
                    if instance._state is not WorkerGenerationState.STARTING:
                        raise WorkerError(WorkerErrorCode.START_FAILED)
                    instance._state = WorkerGenerationState.READY
                return instance
            except BaseException as error:
                instance._terminate_group()
                if not isinstance(error, Exception):
                    raise
                raise WorkerError(WorkerErrorCode.START_FAILED) from None
        except WorkerError:
            raise
        except BaseException as error:
            if instance is not None:
                instance._terminate_group()
            if home is not None and (
                instance is None or instance.state is WorkerGenerationState.DEAD
            ):
                shutil.rmtree(home, ignore_errors=True)
            if not isinstance(error, Exception):
                raise
            raise WorkerError(WorkerErrorCode.START_FAILED) from None
        finally:
            if parent is not None:
                with contextlib.suppress(OSError):
                    parent.close()
            if child is not None:
                with contextlib.suppress(OSError):
                    child.close()

    @classmethod
    def spawn_for_test(
        cls,
        *,
        command: tuple[str, ...],
        source_root: Path,
        readiness_timeout_ms: int,
        shutdown_timeout_ms: int,
    ) -> _WorkerProcess:
        return cls._spawn(
            command=command,
            source_root=source_root,
            readiness_timeout_ms=readiness_timeout_ms,
            shutdown_timeout_ms=shutdown_timeout_ms,
            test_timeout_cap_ms=shutdown_timeout_ms,
        )

    @classmethod
    def spawn(
        cls,
        *,
        python: Path,
        source_root: Path,
    ) -> _WorkerProcess:
        if type(python) is not _PATH_TYPE or not python.is_absolute() or not python.is_file():
            raise WorkerError(WorkerErrorCode.INVALID_INPUT)
        return cls._spawn(
            command=(str(python), "-B", "-m", "vibecad.worker"),
            source_root=source_root,
            readiness_timeout_ms=15_000,
            shutdown_timeout_ms=5_000,
            test_timeout_cap_ms=None,
        )

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def launch_primitive(self) -> str:
        return self._process.launch_primitive

    @property
    def state(self) -> WorkerGenerationState:
        with self._lifecycle_lock:
            return self._state

    def _group_exists(self) -> bool:
        if not self._process.started:
            return False
        try:
            os.killpg(self._process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _terminate_group(self) -> None:
        with self._lifecycle_lock:
            if self._state is WorkerGenerationState.DEAD:
                return
        acquired = self._termination_lock.acquire(timeout=_TERMINATION_LIMIT_SECONDS)
        if not acquired:
            return
        try:
            with self._lifecycle_lock:
                if self._state is WorkerGenerationState.DEAD:
                    return
            _retain_startup_cleanup(self)
            with self._lifecycle_lock:
                self._state = WorkerGenerationState.TERMINATING
                connection = self._connection
            deferred_control_flow: BaseException | None = None

            def remember_control_flow(error: BaseException) -> None:
                nonlocal deferred_control_flow
                if not isinstance(error, Exception) and deferred_control_flow is None:
                    deferred_control_flow = error

            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except BaseException as error:
                    remember_control_flow(error)
                try:
                    connection.close()
                except BaseException as error:
                    remember_control_flow(error)
                else:
                    try:
                        with self._lifecycle_lock:
                            if self._connection is connection:
                                self._connection = None
                    except BaseException as error:
                        remember_control_flow(error)

            try:
                deadline = time.monotonic() + _TERMINATION_LIMIT_SECONDS
                identity_held = False
                if self._process.started:
                    try:
                        self._process.exited_without_reaping()
                        identity_held = True
                    except _ChildIdentityReleased:
                        identity_held = False
                    except Exception:
                        pass
                    if identity_held:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(self._process.pid, signal.SIGTERM)
                    term_deadline = min(
                        deadline,
                        time.monotonic() + _TERM_GRACE_SECONDS,
                    )
                    while identity_held and time.monotonic() < term_deadline:
                        try:
                            if self._process.exited_without_reaping():
                                break
                        except _ChildIdentityReleased:
                            identity_held = False
                            break
                        except Exception:
                            break
                        time.sleep(0.005)
                    if identity_held:
                        with contextlib.suppress(ProcessLookupError, PermissionError):
                            os.killpg(self._process.pid, signal.SIGKILL)
                        try:
                            self._process.wait(timeout=max(0.001, deadline - time.monotonic()))
                        except (subprocess.TimeoutExpired, OSError):
                            pass
                    if identity_held:
                        while time.monotonic() < deadline:
                            try:
                                if not self._group_exists():
                                    break
                            except Exception:
                                break
                            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
            except BaseException as error:
                remember_control_flow(error)

            group_gone = False
            identity_released = not self._process.started
            try:
                group_gone = not self._group_exists()
            except BaseException as error:
                remember_control_flow(error)
            if self._process.started:
                try:
                    identity_released = self._process.identity_released
                except BaseException as error:
                    remember_control_flow(error)
                    identity_released = False
            cleanup_complete = False
            if group_gone and identity_released:
                try:
                    cleanup_complete = _remove_private_home(self._home)
                except BaseException as error:
                    remember_control_flow(error)
            with self._lifecycle_lock:
                cleanup_complete = cleanup_complete and self._connection is None
                self._state = (
                    WorkerGenerationState.DEAD
                    if cleanup_complete
                    else WorkerGenerationState.CLEANUP_REQUIRED
                )
            if deferred_control_flow is not None:
                raise deferred_control_flow
        finally:
            self._termination_lock.release()

    def _lost(self) -> WorkerError:
        self._terminate_group()
        return WorkerError(WorkerErrorCode.GENERATION_LOST)

    def _rpc(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
        allow_starting: bool = False,
    ) -> dict[str, object]:
        if (
            type(timeout_ms) is not int
            or timeout_ms <= 0
            or timeout_ms > 30_000
            or type(method) is not str
            or type(params) is not dict
        ):
            raise WorkerError(WorkerErrorCode.INVALID_INPUT)
        with self._io_lock:
            with self._lifecycle_lock:
                allowed = self._state is WorkerGenerationState.READY or (
                    allow_starting
                    and self._state is WorkerGenerationState.STARTING
                    and method == "worker.ready"
                )
                connection = self._connection
            if not allowed or connection is None:
                code = (
                    WorkerErrorCode.GENERATION_LOST
                    if self.state
                    in {
                        WorkerGenerationState.TERMINATING,
                        WorkerGenerationState.DEAD,
                        WorkerGenerationState.CLEANUP_REQUIRED,
                    }
                    else WorkerErrorCode.CLOSED
                )
                raise WorkerError(code)
            request_id = f"worker_request_{secrets.token_hex(16)}"
            try:
                effective_timeout_ms = timeout_ms
                if self._test_timeout_cap_ms is not None and not allow_starting:
                    effective_timeout_ms = min(
                        effective_timeout_ms,
                        self._test_timeout_cap_ms,
                    )
                raw = encode_worker_request(
                    {
                        "schema_version": 1,
                        "generation_id": self._generation_id,
                        "request_id": request_id,
                        "method": method,
                        "params": params,
                    }
                )
                deadline = time.monotonic() + effective_timeout_ms / 1000
                _send_frame(
                    connection,
                    raw,
                    deadline=deadline,
                    capability_fd=capability_fd,
                )
                response = decode_worker_response(
                    _receive_frame(connection, deadline=deadline),
                    expected_generation_id=self._generation_id,
                    expected_request_id=request_id,
                )
            except BaseException as error:
                lost = self._lost()
                if not isinstance(error, Exception):
                    raise
                raise lost from None
            with self._lifecycle_lock:
                accepted = (
                    not (
                        self._state is not WorkerGenerationState.READY
                        and not (
                            allow_starting
                            and self._state is WorkerGenerationState.STARTING
                            and method == "worker.ready"
                        )
                    )
                    and self._connection is connection
                )
            if not accepted:
                raise self._lost() from None
            if response["ok"] is True:
                return response["result"]  # type: ignore[return-value]
            error = response["error"]
            try:
                wire = WorkerWireErrorCode(error["code"])  # type: ignore[index]
            except (KeyError, TypeError, ValueError):
                raise self._lost() from None
            if wire is WorkerWireErrorCode.INTERNAL_ERROR:
                raise self._lost() from None
            raise WorkerError(_WIRE_ERRORS[wire])

    def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        return self._rpc(
            method,
            params,
            timeout_ms=timeout_ms,
            capability_fd=capability_fd,
        )

    def request_for_test(self, *, timeout_ms: int) -> dict[str, object]:
        return self._rpc("worker.ready", {}, timeout_ms=timeout_ms)

    def close_gracefully(self) -> None:
        self._terminate_group()

    def terminate(self) -> None:
        self._terminate_group()

    def __reduce__(self):
        raise TypeError("Worker processes cannot be serialized")


def _sweep_startup_cleanup() -> None:
    with _STARTUP_CLEANUP_LOCK:
        pending = tuple(_STARTUP_CLEANUP.values())
    for process in pending:
        try:
            process.terminate()
        except BaseException:
            pass
        if process.state is WorkerGenerationState.DEAD:
            with _STARTUP_CLEANUP_CONDITION:
                if _STARTUP_CLEANUP.get(process.generation_id) is process:
                    _STARTUP_CLEANUP.pop(process.generation_id, None)


def _run_startup_cleanup_sweeper() -> None:
    ready = _STARTUP_CLEANUP_SWEEPER_READY
    if ready is None:
        return
    ready.set()
    while True:
        with _STARTUP_CLEANUP_CONDITION:
            while not _STARTUP_CLEANUP:
                _STARTUP_CLEANUP_CONDITION.wait(_STARTUP_CLEANUP_POLL_SECONDS)
        _sweep_startup_cleanup()
        with _STARTUP_CLEANUP_CONDITION:
            if _STARTUP_CLEANUP:
                _STARTUP_CLEANUP_CONDITION.wait(_STARTUP_CLEANUP_POLL_SECONDS)


def _ensure_startup_cleanup_sweeper() -> None:
    global _STARTUP_CLEANUP_SWEEPER
    global _STARTUP_CLEANUP_SWEEPER_READY

    with _STARTUP_CLEANUP_CONDITION:
        monitor = _STARTUP_CLEANUP_SWEEPER
        ready = _STARTUP_CLEANUP_SWEEPER_READY
        if monitor is None or not monitor.is_alive():
            ready = threading.Event()
            monitor = threading.Thread(
                target=_run_startup_cleanup_sweeper,
                name="vibecad-worker-startup-cleanup",
                daemon=True,
            )
            _STARTUP_CLEANUP_SWEEPER = monitor
            _STARTUP_CLEANUP_SWEEPER_READY = ready
            try:
                monitor.start()
            except BaseException as error:
                _STARTUP_CLEANUP_SWEEPER = None
                _STARTUP_CLEANUP_SWEEPER_READY = None
                if not isinstance(error, Exception):
                    raise
                raise WorkerError(WorkerErrorCode.START_FAILED) from None
    if ready is None or not ready.wait(1) or not monitor.is_alive():
        raise WorkerError(WorkerErrorCode.START_FAILED)


def _retain_startup_cleanup(process: _WorkerProcess) -> None:
    with _STARTUP_CLEANUP_CONDITION:
        _STARTUP_CLEANUP[process.generation_id] = process
        _STARTUP_CLEANUP_CONDITION.notify_all()


__all__ = (
    "WorkerError",
    "WorkerErrorCode",
    "WorkerGenerationState",
)
