"""Bounded connection and startup for the one local Task Kernel daemon."""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from vibecad._file_compat import (
    close_windows_handle,
    open_windows_directory_handle,
    validate_windows_handle_path,
)
from vibecad.daemon.client import LocalKernelClient
from vibecad.daemon.state import (
    DAEMON_AUTHORITY,
    DaemonError,
    DaemonErrorCode,
    daemon_run_root,
    read_boot_state,
)
from vibecad.daemon.windows_ipc import process_is_same_or_direct_child, process_start_ns
from vibecad.interaction.protocol_v2 import V2_HANDSHAKE_TIMEOUT_SECONDS
from vibecad.runtime import paths, status
from vibecad.runtime import platform as runtime_platform

DAEMON_BOOTSTRAP_TIMEOUT_SECONDS = 15.0
DAEMON_BOOTSTRAP_POLL_SECONDS = 0.02
DAEMON_RETIRE_TIMEOUT_SECONDS = 8.0
_DAEMON_ID_RE = re.compile(r"daemon_[0-9a-f]{32}\Z")
_WINDOWS_STARTUP_LOCK_HANDLE_ENV = "VIBECAD_STARTUP_LOCK_HANDLE"
_WINDOWS_SPAWN_LOCK = threading.Lock()

_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "APPDATA",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "QT_QPA_PLATFORM",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "VIBECAD_FREECAD_ENV",
        "VIBECAD_HOME",
        "WINDIR",
    }
)


def _daemon_environment() -> dict[str, str]:
    environment = {
        name: value
        for name in _SAFE_ENVIRONMENT_NAMES
        if (value := os.environ.get(name)) is not None
    }
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    return environment


def _python_program_path() -> str:
    getter = ctypes.pythonapi.Py_GetProgramFullPath
    getter.argtypes = ()
    getter.restype = ctypes.c_wchar_p
    value = getter()
    if type(value) is not str or not value:
        raise ValueError("Python startup program path is unavailable")
    return value


_FileIdentity = tuple[int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ExecutableEvidence:
    entry: Path
    entry_identity: _FileIdentity
    entry_mode: int
    target: Path
    target_identity: _FileIdentity
    target_mode: int


def _exact_absolute_path(value: object, *, label: str) -> Path:
    try:
        spelling = os.fspath(value)
    except TypeError as error:
        raise ValueError(f"{label} is unavailable") from error
    if type(spelling) is not str or not spelling or "\0" in spelling:
        raise ValueError(f"{label} is unavailable")
    candidate = Path(spelling)
    if not candidate.is_absolute() or os.path.normpath(spelling) != spelling:
        raise ValueError(f"{label} is not an exact absolute spelling")
    return candidate


def _file_identity(value: os.stat_result) -> _FileIdentity:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
    )


def _capture_executable(
    entry: Path,
    *,
    allow_missing: bool,
) -> _ExecutableEvidence | None:
    try:
        entry_before = entry.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ValueError(f"executable entry is unavailable: {entry}") from None
    except OSError as error:
        raise ValueError(f"executable entry is unavailable: {entry}") from error
    if not (stat.S_ISREG(entry_before.st_mode) or stat.S_ISLNK(entry_before.st_mode)):
        raise ValueError(f"executable entry is not a regular file or symlink: {entry}")
    try:
        target = entry.resolve(strict=True)
        target_before = target.lstat()
        executable_before = os.access(target, os.X_OK)
        entry_after = entry.lstat()
        resolved_after = entry.resolve(strict=True)
        target_after = target.lstat()
        executable_after = os.access(target, os.X_OK)
    except OSError as error:
        raise ValueError(f"executable identity is unavailable: {entry}") from error
    if (
        not stat.S_ISREG(target_before.st_mode)
        or not stat.S_ISREG(target_after.st_mode)
        or not executable_before
        or not executable_after
        or resolved_after != target
        or _file_identity(entry_before) != _file_identity(entry_after)
        or stat.S_IMODE(entry_before.st_mode) != stat.S_IMODE(entry_after.st_mode)
        or _file_identity(target_before) != _file_identity(target_after)
        or stat.S_IMODE(target_before.st_mode) != stat.S_IMODE(target_after.st_mode)
    ):
        raise ValueError(f"executable identity is invalid or unstable: {entry}")
    return _ExecutableEvidence(
        entry=entry,
        entry_identity=_file_identity(entry_before),
        entry_mode=stat.S_IMODE(entry_before.st_mode),
        target=target,
        target_identity=_file_identity(target_before),
        target_mode=stat.S_IMODE(target_before.st_mode),
    )


def _capture_directory_identity(directory: Path) -> _FileIdentity:
    try:
        before = directory.lstat()
        resolved = directory.resolve(strict=True)
        after = directory.lstat()
    except OSError as error:
        raise ValueError(f"runtime prefix is unavailable: {directory}") from error
    getuid = getattr(os, "getuid", None)
    if (
        not stat.S_ISDIR(before.st_mode)
        or directory != resolved
        or _file_identity(before) != _file_identity(after)
        or (hasattr(before, "st_uid") and getuid is not None and before.st_uid != getuid())
    ):
        raise ValueError(f"runtime prefix identity is invalid or unstable: {directory}")
    return _file_identity(before)


def _host_spellings(prefix: Path) -> tuple[Path, Path]:
    if runtime_platform.is_windows():
        return (
            prefix / "Library" / "bin" / "FreeCADCmd.exe",
            prefix / "Library" / "bin" / "FreeCAD.exe",
        )
    return prefix / "bin" / "freecadcmd", prefix / "bin" / "FreeCAD"


def _development_python_spellings(prefix: Path) -> tuple[Path, ...]:
    if runtime_platform.is_windows():
        return prefix / "python.exe", prefix / "Scripts" / "python.exe"
    return (prefix / "bin" / "python",)


def _capture_derived_hosts(
    host_paths: tuple[Path, ...],
) -> dict[Path, _ExecutableEvidence | None]:
    return {
        host: _capture_executable(host, allow_missing=True) for host in dict.fromkeys(host_paths)
    }


def _inode_keys(evidence: _ExecutableEvidence) -> frozenset[tuple[int, int]]:
    return frozenset(
        {
            evidence.entry_identity[:2],
            evidence.target_identity[:2],
        }
    )


def _executables_are_distinct(
    evidence_values: Iterable[_ExecutableEvidence | None],
) -> bool:
    seen: set[tuple[int, int]] = set()
    for value in evidence_values:
        if value is None:
            continue
        current = _inode_keys(value)
        if not current.isdisjoint(seen):
            return False
        seen.update(current)
    return True


def _daemon_python() -> str:
    """Select only an exact stable development or managed-runtime Python."""

    active_prefix = _exact_absolute_path(
        paths.active_runtime_prefix(),
        label="active runtime prefix",
    )
    program_spelling = _python_program_path()
    executable_spelling = sys.executable
    prefix_spelling = sys.prefix
    if type(executable_spelling) is not str or type(prefix_spelling) is not str:
        raise ValueError("Python startup metadata is unavailable")
    program = _exact_absolute_path(
        program_spelling,
        label="Python startup program path",
    )
    executable = _exact_absolute_path(executable_spelling, label="sys.executable")
    captured_prefix = _exact_absolute_path(prefix_spelling, label="sys.prefix")

    active_hosts = _host_spellings(active_prefix)
    captured_hosts = _host_spellings(captured_prefix)
    all_host_paths = (*active_hosts, *captured_hosts)
    first_host_evidence = _capture_derived_hosts(all_host_paths)

    if program != executable:
        raise ValueError("Python startup path and sys.executable disagree")

    if program not in active_hosts:
        if program in captured_hosts:
            raise ValueError("inactive FreeCAD host cannot select a daemon Python")
        development_pythons = _development_python_spellings(captured_prefix)
        if program not in development_pythons:
            raise ValueError("daemon caller is not an exact admitted interpreter")
        first_prefix_identity = _capture_directory_identity(captured_prefix)
        first_python = _capture_executable(program, allow_missing=False)
        if first_python is None:  # pragma: no cover - allow_missing=False
            raise ValueError("development Python is unavailable")
        final_active_prefix = _exact_absolute_path(
            paths.active_runtime_prefix(),
            label="active runtime prefix",
        )
        final_program_spelling = _python_program_path()
        final_executable_spelling = sys.executable
        final_prefix_spelling = sys.prefix

        second_host_evidence = _capture_derived_hosts(all_host_paths)
        second_python = _capture_executable(program, allow_missing=False)
        second_prefix_identity = _capture_directory_identity(captured_prefix)
        if second_python is None:  # pragma: no cover - allow_missing=False
            raise ValueError("development Python is unavailable")
        host_inode_keys = {
            key
            for evidence in first_host_evidence.values()
            if evidence is not None
            for key in _inode_keys(evidence)
        }
        if (
            first_python != second_python
            or first_host_evidence != second_host_evidence
            or first_prefix_identity != second_prefix_identity
            or not _inode_keys(first_python).isdisjoint(host_inode_keys)
            or final_active_prefix != active_prefix
            or final_program_spelling != program_spelling
            or type(final_executable_spelling) is not str
            or final_executable_spelling != executable_spelling
            or type(final_prefix_spelling) is not str
            or final_prefix_spelling != prefix_spelling
        ):
            raise ValueError("development Python identity changed during selection")
        return executable_spelling

    selected_host = first_host_evidence[program]
    active_host_evidence = tuple(first_host_evidence[host] for host in active_hosts)
    if selected_host is None or any(evidence is None for evidence in active_host_evidence):
        raise ValueError("active FreeCAD host identity is unavailable")
    admitted_active_hosts = tuple(
        evidence for evidence in active_host_evidence if evidence is not None
    )
    selected_inode_keys = _inode_keys(selected_host)
    if sum(
        not selected_inode_keys.isdisjoint(_inode_keys(evidence))
        for evidence in admitted_active_hosts
    ) != 1 or not _executables_are_distinct(first_host_evidence.values()):
        raise ValueError("active FreeCAD host identity is not unique")

    active_checkpoints = [active_prefix]
    active_checkpoints.append(
        _exact_absolute_path(
            paths.active_runtime_prefix(),
            label="active runtime prefix",
        )
    )
    initially_ready = status.runtime_ready()
    active_checkpoints.append(
        _exact_absolute_path(
            paths.active_runtime_prefix(),
            label="active runtime prefix",
        )
    )
    if not initially_ready:
        raise ValueError("managed runtime is not ready for embedded daemon startup")

    first_prefix_identity = _capture_directory_identity(active_prefix)
    first = status.capture_runtime_generation_evidence(active_prefix)
    active_checkpoints.append(
        _exact_absolute_path(
            paths.active_runtime_prefix(),
            label="active runtime prefix",
        )
    )
    second = status.capture_runtime_generation_evidence(active_prefix)
    if (
        type(first) is not status.RuntimeGenerationEvidence
        or type(second) is not status.RuntimeGenerationEvidence
    ):
        raise ValueError("managed runtime evidence has an invalid type")
    daemon_python_spelling = os.fspath(first.python)

    expected_python = paths.env_python_for(active_prefix)
    first_python = _capture_executable(expected_python, allow_missing=False)
    if first_python is None:  # pragma: no cover - allow_missing=False
        raise ValueError("managed runtime Python is unavailable")
    active_checkpoints.append(
        _exact_absolute_path(
            paths.active_runtime_prefix(),
            label="active runtime prefix",
        )
    )
    finally_ready = status.runtime_ready()
    final_program_spelling = _python_program_path()
    final_executable_spelling = sys.executable
    final_prefix_spelling = sys.prefix

    second_host_evidence = _capture_derived_hosts(all_host_paths)
    second_python = _capture_executable(expected_python, allow_missing=False)
    second_prefix_identity = _capture_directory_identity(active_prefix)
    if second_python is None:  # pragma: no cover - allow_missing=False
        raise ValueError("managed runtime Python is unavailable")
    host_inode_keys = {
        key
        for evidence in first_host_evidence.values()
        if evidence is not None
        for key in _inode_keys(evidence)
    }
    if (
        first != second
        or first_host_evidence != second_host_evidence
        or any(checkpoint != active_prefix for checkpoint in active_checkpoints)
        or not finally_ready
        or first.prefix != active_prefix
        or first.prefix_identity != first_prefix_identity[:2]
        or first_prefix_identity != second_prefix_identity
        or first.python != expected_python
        or first.python_target != first_python.target
        or first.python_entry_identity != first_python.entry_identity
        or first.python_target_identity != first_python.target_identity
        or first_python != second_python
        or not _inode_keys(first_python).isdisjoint(host_inode_keys)
        or final_program_spelling != program_spelling
        or type(final_executable_spelling) is not str
        or final_executable_spelling != executable_spelling
        or type(final_prefix_spelling) is not str
        or final_prefix_spelling != prefix_spelling
    ):
        raise ValueError("managed runtime evidence changed during daemon selection")
    return daemon_python_spelling


def _spawn_daemon(
    *,
    startup_lock_fd: int | None = None,
    startup_lock_handle: int | None = None,
) -> subprocess.Popen[bytes]:
    package_root = Path(__file__).resolve().parents[2]
    environment = _daemon_environment()
    command = [_daemon_python(), "-B", "-m", "vibecad.daemon"]
    common = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        # Resolve from the already imported package location instead of a
        # caller-controlled CWD or PYTHONPATH. This also keeps checkout tests
        # honest before C14 installs the wheel into a fresh environment.
        "cwd": str(package_root),
        "env": environment,
    }
    if startup_lock_handle is not None:
        if (
            not runtime_platform.is_windows()
            or type(startup_lock_handle) is not int
            or startup_lock_handle <= 0
            or startup_lock_fd is not None
        ):
            raise ValueError("Windows daemon startup requires one exact lock handle")
        environment[_WINDOWS_STARTUP_LOCK_HANDLE_ENV] = str(startup_lock_handle)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [startup_lock_handle]}
        with _WINDOWS_SPAWN_LOCK:
            os.set_handle_inheritable(startup_lock_handle, True)
            try:
                return subprocess.Popen(
                    command,
                    **common,
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW,
                )
            finally:
                os.set_handle_inheritable(startup_lock_handle, False)
    if type(startup_lock_fd) is not int or startup_lock_fd < 0:
        raise ValueError("POSIX daemon startup requires one exact lock descriptor")
    environment[status.RUNTIME_MAINTENANCE_CLAIM_FD_ENV] = str(startup_lock_fd)
    return subprocess.Popen(
        command,
        **common,
        start_new_session=True,
        pass_fds=(startup_lock_fd,),
    )


def _stop_losing_process(process: object) -> bool:
    poll = getattr(process, "poll", None)
    if not callable(poll):
        return False
    try:
        if poll() is not None:
            return True
        pid = getattr(process, "pid", None)
        if type(pid) is int and pid > 1 and not runtime_platform.is_windows():
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGTERM)
        else:
            terminate = getattr(process, "terminate", None)
            if callable(terminate):
                terminate()
        wait = getattr(process, "wait", None)
        if callable(wait):
            try:
                wait(timeout=1.0)
                return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        if type(pid) is int and pid > 1 and not runtime_platform.is_windows():
            with contextlib.suppress(OSError):
                os.killpg(pid, signal.SIGKILL)
        else:
            kill = getattr(process, "kill", None)
            if callable(kill):
                kill()
        if callable(wait):
            try:
                wait(timeout=1.0)
                return True
            except (OSError, subprocess.TimeoutExpired):
                pass
        return poll() is not None
    except BaseException:
        return False


def _defer_claim_release_until_process_exit(
    claim: object,
    process: object,
) -> None:
    """Keep the startup generation claimed until an unproved child really exits."""

    defer_release = getattr(claim, "defer_release", None)
    wait = getattr(process, "wait", None)
    if not callable(defer_release) or not callable(wait):
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    try:
        release = defer_release()
    except RuntimeError:
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None

    def reap_then_release() -> None:
        try:
            wait()
        except BaseException:
            # Preserve the live local fd and therefore fail closed if process
            # exit cannot be proven. Process teardown still closes the claim.
            return
        release()

    threading.Thread(
        target=reap_then_release,
        name="vibecad-daemon-stop-reaper",
        daemon=True,
    ).start()


def _stop_spawned_process(
    process: object,
    *,
    maintenance_claim: object,
    inherited_claim: bool,
) -> None:
    if _stop_losing_process(process) or not inherited_claim:
        return
    _defer_claim_release_until_process_exit(maintenance_claim, process)


def _reap_winning_process(process: object) -> None:
    """Retain and reap a spawned daemon without coupling it to the client."""

    wait = getattr(process, "wait", None)
    if not callable(wait):
        return

    def reap() -> None:
        with contextlib.suppress(BaseException):
            wait()

    threading.Thread(
        target=reap,
        name="vibecad-daemon-reaper",
        daemon=True,
    ).start()


def _canonical_run_root(value: object | None) -> Path:
    expected = daemon_run_root(paths.data_root())
    if value is None:
        return expected
    if type(value) is str:
        candidate = Path(value)
    elif type(value) is type(Path("/")):
        candidate = value
    else:
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    return candidate


def _clean_absent_run_root(root: Path) -> bool:
    try:
        value = os.lstat(root)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if runtime_platform.is_windows():
        handle = None
        try:
            handle = open_windows_directory_handle(
                root,
                inheritable=False,
                deny_delete=True,
            )
            capability = validate_windows_handle_path(
                handle,
                root,
                directory=True,
            )
            empty = not os.listdir(root)
            return empty and validate_windows_handle_path(
                handle,
                root,
                directory=True,
                expected=capability,
            ) == capability
        except (OSError, TypeError, ValueError):
            return False
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    close_windows_handle(handle)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) & 0o077
    ):
        return False
    try:
        with os.scandir(root) as entries:
            empty = next(entries, None) is None
        after = os.lstat(root)
        return empty and (after.st_dev, after.st_ino, after.st_mode, after.st_uid) == (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
        )
    except OSError:
        return False


def _process_alive(pid: int, *, started_ns: int | None = None) -> bool:
    if runtime_platform.is_windows():
        if type(started_ns) is not int or started_ns <= 0:
            return False
        try:
            return process_start_ns(pid) == started_ns
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _daemon_authority_unclaimed(run_root: Path) -> bool:
    """Prove no process owns the daemon lease when publication is unavailable."""

    lock_root = run_root.parent / "locks"
    try:
        info = os.lstat(lock_root)
    except FileNotFoundError:
        # Every normal daemon creates and pins both directories before taking
        # authority. An absent lock root plus an absent run root therefore
        # proves there is no published/closing normal generation.
        return not os.path.lexists(run_root)
    except OSError:
        return False
    if runtime_platform.is_windows():
        handle = None
        try:
            handle = open_windows_directory_handle(
                lock_root,
                inheritable=False,
                deny_delete=True,
            )
            validate_windows_handle_path(
                handle,
                lock_root,
                directory=True,
            )
        except (OSError, TypeError, ValueError):
            return False
        finally:
            if handle is not None:
                with contextlib.suppress(OSError):
                    close_windows_handle(handle)
    elif (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        return False
    try:
        from vibecad.workflow.lease import (
            LeaseError,
            LeaseRootTrust,
            ResourceLeaseManager,
        )

        manager = ResourceLeaseManager(
            lock_root,
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        lease = manager.acquire(DAEMON_AUTHORITY)
    except (LeaseError, OSError, RuntimeError, ValueError):
        return False
    try:
        lease.require_current()
        lease.release(owner_token=lease.owner_token)
    except LeaseError:
        if not lease.released:
            with contextlib.suppress(LeaseError):
                lease.release(owner_token=lease.owner_token)
        return False
    return True


def _runtime_uninstall_pending() -> bool:
    try:
        from vibecad.runtime.uninstall import uninstall_marker

        return os.path.lexists(uninstall_marker())
    except BaseException:
        return True


def connect_existing_local_kernel(run_root: object) -> LocalKernelClient:
    """Connect an application client without crossing runtime removal."""

    selected_root = _canonical_run_root(run_root)
    if selected_root != daemon_run_root(paths.data_root()):
        return LocalKernelClient.connect(selected_root)
    deadline = time.monotonic() + V2_HANDSHAKE_TIMEOUT_SECONDS
    try:
        with status.runtime_maintenance_lock(
            timeout=V2_HANDSHAKE_TIMEOUT_SECONDS,
            poll_interval=DAEMON_BOOTSTRAP_POLL_SECONDS,
        ):
            if _runtime_uninstall_pending():
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)
            return LocalKernelClient.connect(
                selected_root,
                timeout_seconds=remaining,
            )
    except DaemonError:
        raise
    except RuntimeError:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None


def retire_local_kernel(
    *,
    reason: object,
    expected_daemon_id: object | None = None,
    run_root: object | None = None,
    timeout_seconds: object = DAEMON_RETIRE_TIMEOUT_SECONDS,
    _connect: Callable[[object], LocalKernelClient] | None = None,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
    _maintenance_held: bool = False,
) -> bool:
    """Retire one authenticated daemon and wait for exact state cleanup."""

    if (
        reason not in {"incompatible_build", "runtime_uninstall", "runtime_upgrade"}
        or (
            expected_daemon_id is not None
            and (
                type(expected_daemon_id) is not str
                or _DAEMON_ID_RE.fullmatch(expected_daemon_id) is None
            )
        )
        or type(timeout_seconds) not in {int, float}
        or isinstance(timeout_seconds, bool)
        or not 0 < float(timeout_seconds) <= DAEMON_RETIRE_TIMEOUT_SECONDS
        or not callable(_clock)
        or not callable(_sleep)
        or type(_maintenance_held) is not bool
    ):
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    selected_root = _canonical_run_root(run_root)
    deadline = _clock() + float(timeout_seconds)
    if not _maintenance_held and selected_root == daemon_run_root(paths.data_root()):
        try:
            remaining = deadline - _clock()
            if remaining <= 0:
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            with status.runtime_maintenance_lock(
                timeout=remaining,
                poll_interval=min(DAEMON_BOOTSTRAP_POLL_SECONDS, remaining),
            ):
                remaining = deadline - _clock()
                if remaining <= 0:
                    raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
                return retire_local_kernel(
                    reason=reason,
                    expected_daemon_id=expected_daemon_id,
                    run_root=selected_root,
                    timeout_seconds=remaining,
                    _connect=_connect,
                    _clock=_clock,
                    _sleep=_sleep,
                    _maintenance_held=True,
                )
        except DaemonError:
            raise
        except RuntimeError:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
    if _clean_absent_run_root(selected_root):
        if _daemon_authority_unclaimed(selected_root):
            return True
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    connector = LocalKernelClient.connect if _connect is None else _connect
    if not callable(connector):
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    try:
        if _connect is None:
            remaining = deadline - _clock()
            if remaining <= 0:
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)
            client = LocalKernelClient.connect(
                selected_root,
                timeout_seconds=min(V2_HANDSHAKE_TIMEOUT_SECONDS, remaining),
            )
        else:
            client = connector(selected_root)
    except DaemonError:
        if _clean_absent_run_root(selected_root) and _daemon_authority_unclaimed(selected_root):
            return True
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
    try:
        if expected_daemon_id is not None and client.daemon_id != expected_daemon_id:
            return False
        retired_pid = getattr(client, "daemon_pid", None)
        retired_started_ns = getattr(client, "daemon_started_ns", None)
        if type(retired_pid) is not int or retired_pid <= 1:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        remaining = deadline - _clock()
        if remaining <= 0:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        response = client.retire(
            reason=reason,
            timeout_seconds=remaining,
        )
        if (
            response.error is not None
            or type(response.result) is not dict
            or response.result
            != {
                "schema_version": 1,
                "daemon_id": client.daemon_id,
                "status": "retiring",
            }
        ):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    finally:
        with contextlib.suppress(BaseException):
            client.close()
    while _clock() < deadline:
        if _clean_absent_run_root(selected_root) and not _process_alive(
            retired_pid,
            started_ns=retired_started_ns,
        ):
            return True
        _sleep(
            min(
                DAEMON_BOOTSTRAP_POLL_SECONDS,
                max(0.0, deadline - _clock()),
            )
        )
    if _clean_absent_run_root(selected_root) and not _process_alive(
        retired_pid,
        started_ns=retired_started_ns,
    ):
        return True
    raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)


def _connect_or_start_local_kernel_locked(
    *,
    run_root: object | None = None,
    timeout_seconds: float = DAEMON_BOOTSTRAP_TIMEOUT_SECONDS,
    _connect: Callable[[object], LocalKernelClient] | None = None,
    _spawn: Callable[[], object] | None = None,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
    _maintenance_claim: object,
) -> LocalKernelClient:
    """Connect to the live daemon or start the fixed local entry once.

    Startup may race across clients. The daemon authority lease elects the
    winner; this function only waits for the published authenticated winner.
    It never retries an application request and never couples client close to
    daemon shutdown.
    """

    if (
        type(timeout_seconds) not in {int, float}
        or isinstance(timeout_seconds, bool)
        or not 0 < float(timeout_seconds) <= DAEMON_BOOTSTRAP_TIMEOUT_SECONDS
        or not callable(_clock)
        or not callable(_sleep)
    ):
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    deadline = _clock() + float(timeout_seconds)
    selected_root = _canonical_run_root(run_root)
    if _runtime_uninstall_pending():
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    connector = LocalKernelClient.connect if _connect is None else _connect
    spawner = _spawn_daemon if _spawn is None else _spawn
    if not callable(connector) or not callable(spawner):
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)

    def connect() -> LocalKernelClient:
        remaining = deadline - _clock()
        if remaining <= 0:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        if _connect is None:
            return LocalKernelClient.connect(
                selected_root,
                timeout_seconds=min(V2_HANDSHAKE_TIMEOUT_SECONDS, remaining),
            )
        candidate = connector(selected_root)
        if _clock() >= deadline:
            with contextlib.suppress(BaseException):
                candidate.close()
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return candidate

    try:
        return connect()
    except DaemonError:
        pass

    # The production entry has one fixed data root. A custom root is a
    # connect-only test/embedding seam and must not accidentally start a daemon
    # against a different durable store.
    if _spawn is None and selected_root != daemon_run_root(paths.data_root()):
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)
    if _clock() >= deadline:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    try:
        if _spawn is None:
            if runtime_platform.is_windows():
                claim_handle = getattr(
                    _maintenance_claim,
                    "inheritable_claim_handle",
                    None,
                )
                if not callable(claim_handle):
                    raise RuntimeError
                process = spawner(startup_lock_handle=claim_handle())
            else:
                claim_descriptor = getattr(
                    _maintenance_claim,
                    "inheritable_claim_fd",
                    None,
                )
                if not callable(claim_descriptor):
                    raise RuntimeError
                process = spawner(startup_lock_fd=claim_descriptor())
        else:
            process = spawner()
    except (OSError, RuntimeError, ValueError):
        raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
    if _clock() >= deadline:
        _stop_spawned_process(
            process,
            maintenance_claim=_maintenance_claim,
            inherited_claim=_spawn is None,
        )
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)
    connected = None
    while _clock() < deadline:
        try:
            connected = connect()
            break
        except DaemonError:
            _sleep(
                min(
                    DAEMON_BOOTSTRAP_POLL_SECONDS,
                    max(0.0, deadline - _clock()),
                )
            )
    if connected is None:
        _stop_spawned_process(
            process,
            maintenance_claim=_maintenance_claim,
            inherited_claim=_spawn is None,
        )
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    # If another starter won, do not leave our contended child behind. The
    # authenticated receipt is the authority for that distinction.
    try:
        published = read_boot_state(selected_root).receipt
    except DaemonError:
        connected.close()
        _stop_spawned_process(
            process,
            maintenance_claim=_maintenance_claim,
            inherited_claim=_spawn is None,
        )
        raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED) from None
    spawned_pid = getattr(process, "pid", None)
    spawned_generation_matches = spawned_pid == published.pid
    if (
        runtime_platform.is_windows()
        and type(spawned_pid) is int
        and spawned_pid > 1
    ):
        try:
            spawned_started_ns = process_start_ns(spawned_pid)
        except OSError:
            spawned_generation_matches = False
        else:
            spawned_generation_matches = process_is_same_or_direct_child(
                published.pid,
                started_ns=published.started_ns,
                spawned_pid=spawned_pid,
                spawned_started_ns=spawned_started_ns,
            )
    if type(spawned_pid) is int and spawned_pid > 1 and not spawned_generation_matches:
        _stop_spawned_process(
            process,
            maintenance_claim=_maintenance_claim,
            inherited_claim=_spawn is None,
        )
    else:
        _reap_winning_process(process)
    return connected


def connect_or_start_local_kernel(
    *,
    run_root: object | None = None,
    timeout_seconds: float = DAEMON_BOOTSTRAP_TIMEOUT_SECONDS,
    _connect: Callable[[object], LocalKernelClient] | None = None,
    _spawn: Callable[[], object] | None = None,
    _clock: Callable[[], float] = time.monotonic,
    _sleep: Callable[[float], None] = time.sleep,
) -> LocalKernelClient:
    """Serialize daemon reuse/start against runtime install and removal."""

    if (
        type(timeout_seconds) not in {int, float}
        or isinstance(timeout_seconds, bool)
        or not 0 < float(timeout_seconds) <= DAEMON_BOOTSTRAP_TIMEOUT_SECONDS
        or not callable(_clock)
        or not callable(_sleep)
    ):
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    deadline = _clock() + float(timeout_seconds)
    selected_root = _canonical_run_root(run_root)
    canonical_root = daemon_run_root(paths.data_root())
    if selected_root != canonical_root:
        return _connect_or_start_local_kernel_locked(
            run_root=selected_root,
            timeout_seconds=float(timeout_seconds),
            _connect=_connect,
            _spawn=_spawn,
            _clock=_clock,
            _sleep=_sleep,
            _maintenance_claim=None,
        )
    try:
        remaining = deadline - _clock()
        if remaining <= 0:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        with status.runtime_maintenance_lock(
            timeout=remaining,
            poll_interval=min(DAEMON_BOOTSTRAP_POLL_SECONDS, remaining),
        ) as claim:
            remaining = deadline - _clock()
            if remaining <= 0:
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)
            return _connect_or_start_local_kernel_locked(
                run_root=run_root,
                timeout_seconds=remaining,
                _connect=_connect,
                _spawn=_spawn,
                _clock=_clock,
                _sleep=_sleep,
                _maintenance_claim=claim,
            )
    except DaemonError:
        raise
    except RuntimeError:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None


__all__ = (
    "DAEMON_BOOTSTRAP_POLL_SECONDS",
    "DAEMON_BOOTSTRAP_TIMEOUT_SECONDS",
    "DAEMON_RETIRE_TIMEOUT_SECONDS",
    "connect_or_start_local_kernel",
    "retire_local_kernel",
)
