"""Bounded connection and startup for the one local Task Kernel daemon."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vibecad.daemon.client import LocalKernelClient
from vibecad.daemon.state import (
    DAEMON_AUTHORITY,
    DaemonError,
    DaemonErrorCode,
    daemon_run_root,
    read_boot_state,
)
from vibecad.interaction.protocol_v2 import V2_HANDSHAKE_TIMEOUT_SECONDS
from vibecad.runtime import paths, status

DAEMON_BOOTSTRAP_TIMEOUT_SECONDS = 15.0
DAEMON_BOOTSTRAP_POLL_SECONDS = 0.02
DAEMON_RETIRE_TIMEOUT_SECONDS = 8.0
_DAEMON_ID_RE = re.compile(r"daemon_[0-9a-f]{32}\Z")

_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "QT_QPA_PLATFORM",
        "TMPDIR",
        "USER",
        "VIBECAD_FREECAD_ENV",
        "VIBECAD_HOME",
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


def _spawn_daemon(*, startup_lock_fd: int) -> subprocess.Popen[bytes]:
    package_root = Path(__file__).resolve().parents[2]
    environment = _daemon_environment()
    environment[status.RUNTIME_MAINTENANCE_CLAIM_FD_ENV] = str(startup_lock_fd)
    return subprocess.Popen(
        [sys.executable, "-B", "-m", "vibecad.daemon"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        # Resolve from the already imported package location instead of a
        # caller-controlled CWD or PYTHONPATH. This also keeps checkout tests
        # honest before C14 installs the wheel into a fresh environment.
        cwd=str(package_root),
        env=environment,
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
        if type(pid) is int and pid > 1:
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
        if type(pid) is int and pid > 1:
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


def _process_alive(pid: int) -> bool:
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
    if (
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
        if _clean_absent_run_root(selected_root) and not _process_alive(retired_pid):
            return True
        _sleep(
            min(
                DAEMON_BOOTSTRAP_POLL_SECONDS,
                max(0.0, deadline - _clock()),
            )
        )
    if _clean_absent_run_root(selected_root) and not _process_alive(retired_pid):
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
        published_pid = read_boot_state(selected_root).receipt.pid
    except DaemonError:
        connected.close()
        _stop_spawned_process(
            process,
            maintenance_claim=_maintenance_claim,
            inherited_claim=_spawn is None,
        )
        raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED) from None
    spawned_pid = getattr(process, "pid", None)
    if type(spawned_pid) is int and spawned_pid > 1 and spawned_pid != published_pid:
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
