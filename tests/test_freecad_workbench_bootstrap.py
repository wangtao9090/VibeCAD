from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibecad.daemon.state import (
    DAEMON_ENDPOINT_NAME,
    DAEMON_RECEIPT_NAME,
    daemon_run_root,
)
from vibecad.runtime import paths, spec, status

_PROBE_PREFIX = "VIBECAD_BOOTSTRAP_PROBE="
_PARENT_PREFIX = "VIBECAD_BOOTSTRAP_PARENT="
_PROBE_TIMEOUT_SECONDS = 35
_MAX_UNIX_ENDPOINT_BYTES = 103
_EVIDENCE_TAIL_CHARACTERS = 2_000
_DARWIN_TEMP_PARENT = Path("/private/tmp")
_DARWIN_TEMP_PREFIX = "vc-c00b-"


class _ProcBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True, slots=True)
class _DarwinProcessToken:
    pid: int
    birth_sec: int
    birth_usec: int
    euid: int
    pgid: int
    sid: int


@dataclass(frozen=True, slots=True)
class _CleanupOutcome:
    clean: bool
    retire_attempted: bool
    term_sent: bool
    kill_sent: bool
    detail: str


class _AuthenticatedInspectionError(RuntimeError):
    pass


class _AuthenticatedInspectionTimeout(subprocess.TimeoutExpired):
    pass


@dataclass(slots=True)
class _ProbeAttempt:
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    parse_error: str | None = None
    result: dict[str, object] = field(default_factory=dict)
    action_error: BaseException | None = None
    action_traceback: str | None = None
    cleanup_error: BaseException | None = None
    cleanup_traceback: str | None = None
    evidence_error: BaseException | None = None
    evidence_traceback: str | None = None


@dataclass(slots=True)
class _CleanupAssertionOwnership:
    evidence_emitted: bool = False
    body_owns_assertion: bool = False
    launch_attempted: bool = False
    cleanup_started: bool = False
    cleanup_outcome: _CleanupOutcome | None = None
    cleanup_error: BaseException | None = None
    cleanup_traceback: str | None = None

    def mark_launch_attempted(self) -> None:
        if self.cleanup_started:
            raise RuntimeError("cleanup started before launch attempt")
        self.launch_attempted = True

    def cleanup_once(
        self,
        cleanup: Callable[[], _CleanupOutcome],
    ) -> _CleanupOutcome:
        if self.cleanup_started:
            if self.cleanup_outcome is None:
                raise RuntimeError("cleanup cache is incomplete")
            return self.cleanup_outcome
        self.cleanup_started = True
        self.cleanup_outcome = _CleanupOutcome(
            False,
            False,
            False,
            False,
            "cleanup_in_progress",
        )
        try:
            outcome = cleanup()
            if type(outcome) is not _CleanupOutcome:
                raise TypeError("cleanup did not return _CleanupOutcome")
        except BaseException as error:
            self.cleanup_error = error
            self.cleanup_traceback = traceback.format_exc()
            outcome = _CleanupOutcome(
                False,
                False,
                False,
                False,
                "cleanup_error",
            )
        self.cleanup_outcome = outcome
        return outcome

    def transfer_to_body(self, *, evidence_emitted: bool) -> None:
        if self.evidence_emitted or self.body_owns_assertion:
            raise RuntimeError("parent evidence ownership was already transferred")
        self.evidence_emitted = evidence_emitted
        self.body_owns_assertion = True


def _read_proc_bsd_info(pid: int) -> tuple[int, int, int, int, int]:
    if sys.platform != "darwin":
        raise RuntimeError("Darwin process identity is unavailable")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    proc_pidinfo = library.proc_pidinfo
    proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    proc_pidinfo.restype = ctypes.c_int
    value = _ProcBSDInfo()
    ctypes.set_errno(0)
    observed = proc_pidinfo(
        pid,
        3,
        0,
        ctypes.byref(value),
        ctypes.sizeof(value),
    )
    if observed == 0 and ctypes.get_errno() == errno.ESRCH:
        raise ProcessLookupError(pid)
    if observed != ctypes.sizeof(value):
        raise RuntimeError("Darwin process identity read was incomplete")
    return (
        int(value.pbi_pid),
        int(value.pbi_start_tvsec),
        int(value.pbi_start_tvusec),
        int(value.pbi_uid),
        int(value.pbi_pgid),
    )


def _darwin_process_token(
    pid: int,
    *,
    _read_info: Callable[[int], tuple[int, int, int, int, int]] = _read_proc_bsd_info,
    _getsid: Callable[[int], int] = os.getsid,
    _geteuid: Callable[[], int] = os.geteuid,
) -> _DarwinProcessToken:
    if type(pid) is not int or pid <= 1:
        raise ValueError("daemon PID is invalid")
    first = _read_info(pid)
    sid = _getsid(pid)
    second = _read_info(pid)
    if first != second:
        raise RuntimeError("Darwin process identity changed during capture")
    reported_pid, birth_sec, birth_usec, euid, pgid = first
    if (
        reported_pid != pid
        or birth_sec <= 0
        or not 0 <= birth_usec < 1_000_000
        or euid != _geteuid()
        or pgid != pid
        or sid != pid
    ):
        raise ValueError("daemon process ownership or session identity is invalid")
    return _DarwinProcessToken(pid, birth_sec, birth_usec, euid, pgid, sid)


def _safe_absent_or_empty_run_root(run_root: Path) -> bool:
    if os.path.lexists(run_root / DAEMON_RECEIPT_NAME) or os.path.lexists(
        run_root / DAEMON_ENDPOINT_NAME
    ):
        return False
    try:
        before = os.lstat(run_root)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    getuid = getattr(os, "geteuid", None)
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_IMODE(before.st_mode) & 0o077
        or (getuid is not None and before.st_uid != getuid())
    ):
        return False
    try:
        with os.scandir(run_root) as entries:
            if next(entries, None) is not None:
                return False
        after = os.lstat(run_root)
    except OSError:
        return False
    return (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
    )


class _DaemonCleanupGuard:
    def __init__(
        self,
        run_root: Path,
        *,
        _read_state: Callable[[Path], object] | None = None,
        _retire: Callable[..., bool] | None = None,
        _capture_token: Callable[[int], _DarwinProcessToken] = _darwin_process_token,
        _killpg: Callable[[int, int], None] = os.killpg,
        _clock: Callable[[], float] = time.monotonic,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        from vibecad.daemon.bootstrap import retire_local_kernel
        from vibecad.daemon.state import read_boot_state

        self.run_root = run_root
        self._read_state = read_boot_state if _read_state is None else _read_state
        self._retire = retire_local_kernel if _retire is None else _retire
        self._capture_token = _capture_token
        self._killpg = _killpg
        self._clock = _clock
        self._sleep = _sleep
        self._cold = False
        self._receipt = None
        self._token: _DarwinProcessToken | None = None
        self._token_error = False
        self._ambiguity_latched = False
        self._outcome: _CleanupOutcome | None = None

    @property
    def daemon_id(self) -> str | None:
        value = getattr(self._receipt, "daemon_id", None)
        return value if type(value) is str else None

    @property
    def daemon_pid(self) -> int | None:
        value = getattr(self._receipt, "pid", None)
        return value if type(value) is int else None

    @property
    def process_token(self) -> _DarwinProcessToken | None:
        return self._token

    @property
    def original_token_absent(self) -> bool:
        if self._receipt is None or self._token is None:
            return False
        return self._generation_status() in {"absent", "replaced"}

    def require_cold(self) -> None:
        if not _safe_absent_or_empty_run_root(self.run_root):
            raise RuntimeError("isolated daemon root was not cold")
        self._cold = True

    def _latch_post_auth_ambiguity(self) -> None:
        if self._receipt is not None and self._token is not None:
            self._ambiguity_latched = True

    def _fresh_state(self) -> object | None:
        from vibecad.daemon.state import DaemonError

        try:
            state = self._read_state(self.run_root)
        except DaemonError:
            if os.path.lexists(self.run_root / DAEMON_RECEIPT_NAME):
                self._latch_post_auth_ambiguity()
                raise RuntimeError("daemon publication is ambiguous") from None
            state = None
        if state is None:
            self._latch_post_auth_ambiguity()
        return state

    def observe_publication(self) -> object | None:
        if not self._cold:
            raise RuntimeError("cleanup guard did not prove cold state")
        state = self._fresh_state()
        if state is None:
            return None
        receipt = getattr(state, "receipt", None)
        daemon_id = getattr(receipt, "daemon_id", None)
        pid = getattr(receipt, "pid", None)
        if type(daemon_id) is not str or type(pid) is not int or pid <= 1:
            self._latch_post_auth_ambiguity()
            raise RuntimeError("authenticated daemon receipt is invalid")
        if self._receipt is not None and receipt != self._receipt:
            self._latch_post_auth_ambiguity()
            raise RuntimeError("daemon publication changed after observation")
        self._receipt = receipt
        if self._token is None and not self._token_error:
            try:
                self._token = self._capture_token(pid)
            except (OSError, RuntimeError, ValueError):
                self._token_error = True
        return state

    def _generation_status(self) -> str:
        pid = self.daemon_pid
        if pid is None:
            return "absent"
        try:
            current = self._capture_token(pid)
        except ProcessLookupError:
            return "absent"
        except (OSError, RuntimeError, ValueError):
            self._latch_post_auth_ambiguity()
            return "ambiguous"
        if self._token is None:
            return "ambiguous"
        if current == self._token:
            return "same"
        self._latch_post_auth_ambiguity()
        return "replaced"

    def _proof(self) -> bool:
        if self._receipt is None or self._token is None:
            return False
        try:
            state = self._fresh_state()
        except RuntimeError:
            self._latch_post_auth_ambiguity()
            publication_absent = False
        else:
            publication_absent = state is None
            if state is not None and getattr(state, "receipt", None) != self._receipt:
                self._latch_post_auth_ambiguity()
        return (
            self._generation_status() in {"absent", "replaced"}
            and publication_absent
            and _safe_absent_or_empty_run_root(self.run_root)
        )

    def _eligible_for_signal(self) -> bool:
        if self._receipt is None or self._token is None or self._ambiguity_latched:
            return False
        try:
            state = self._fresh_state()
            if state is None:
                return False
            if getattr(state, "receipt", None) != self._receipt:
                self._latch_post_auth_ambiguity()
                return False
            current = self._capture_token(self._token.pid)
        except (OSError, RuntimeError, ValueError):
            self._latch_post_auth_ambiguity()
            return False
        if current != self._token:
            self._latch_post_auth_ambiguity()
            return False
        return not self._ambiguity_latched

    def _wait_for_proof(self, timeout: float) -> bool:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if self._proof():
                return True
            self._sleep(min(0.02, max(0.0, deadline - self._clock())))
        return self._proof()

    def cleanup(self) -> _CleanupOutcome:
        if self._outcome is not None:
            return self._outcome
        if not self._cold:
            self._outcome = _CleanupOutcome(False, False, False, False, "not_cold")
            return self._outcome
        try:
            self.observe_publication()
        except RuntimeError:
            self._latch_post_auth_ambiguity()
        if self._receipt is None:
            self._outcome = _CleanupOutcome(
                False,
                False,
                False,
                False,
                "publication_unproven",
            )
            return self._outcome

        retired = False
        try:
            retired = self._retire(
                reason="runtime_upgrade",
                expected_daemon_id=self.daemon_id,
                run_root=self.run_root,
                timeout_seconds=8,
            )
        except Exception:
            retired = False
        if retired and self._proof():
            self._outcome = _CleanupOutcome(True, True, False, False, "retired")
            return self._outcome
        if self._proof():
            self._outcome = _CleanupOutcome(True, True, False, False, "retire_removed")
            return self._outcome
        if not self._eligible_for_signal():
            self._outcome = _CleanupOutcome(False, True, False, False, "signal_forbidden")
            return self._outcome

        term_sent = False
        kill_sent = False
        try:
            self._killpg(self._token.pgid, signal.SIGTERM)
            term_sent = True
        except OSError:
            self._outcome = _CleanupOutcome(False, True, False, False, "term_failed")
            return self._outcome
        if self._wait_for_proof(1.0):
            self._outcome = _CleanupOutcome(True, True, True, False, "term")
            return self._outcome
        if not self._eligible_for_signal():
            self._outcome = _CleanupOutcome(False, True, True, False, "kill_forbidden")
            return self._outcome
        try:
            self._killpg(self._token.pgid, signal.SIGKILL)
            kill_sent = True
        except OSError:
            self._outcome = _CleanupOutcome(False, True, True, False, "kill_failed")
            return self._outcome
        clean = self._wait_for_proof(1.0)
        self._outcome = _CleanupOutcome(
            clean,
            True,
            term_sent,
            kill_sent,
            "kill" if clean else "cleanup_unresolved",
        )
        return self._outcome


def _run_with_unconditional_cleanup(
    action: Callable[[], object],
    cleanup: Callable[[], object],
) -> object:
    try:
        return action()
    finally:
        cleanup()


def _exact_managed_runtime(
    prefix_value: str,
) -> tuple[Path, Path, status.RuntimeGenerationEvidence]:
    prefix = Path(prefix_value)
    if not prefix.is_absolute():
        pytest.fail("VIBECAD_FREECAD_ENV must be an absolute managed prefix")
    try:
        evidence = status.capture_runtime_generation_evidence(prefix)
        receipt = json.loads((prefix / ".vibecad_ready").read_text(encoding="utf-8"))
        freecadcmd = prefix / "bin" / "freecadcmd"
        freecadcmd_info = freecadcmd.stat()
        resolved_freecadcmd = freecadcmd.resolve(strict=True)
        resolved_freecadcmd.relative_to(prefix.resolve(strict=True))
    except (OSError, TypeError, ValueError) as error:
        pytest.fail(f"managed FreeCAD identity verification failed: {error}")
    if receipt != spec.expected_receipt():
        pytest.fail("VIBECAD_FREECAD_ENV does not have the exact managed receipt")
    if (
        not stat.S_ISREG(freecadcmd_info.st_mode)
        or not os.access(freecadcmd, os.X_OK)
        or evidence.prefix != prefix.resolve(strict=True)
    ):
        pytest.fail("managed FreeCAD binary identity is not executable and regular")
    return evidence.prefix, resolved_freecadcmd, evidence


def _probe_result(stdout: str) -> tuple[dict[str, object] | None, str | None]:
    payloads = [
        line.removeprefix(_PROBE_PREFIX)
        for line in stdout.splitlines()
        if line.startswith(_PROBE_PREFIX)
    ]
    if len(payloads) != 1:
        return None, f"expected one embedded probe result, observed {len(payloads)}"
    try:
        value = json.loads(payloads[0])
    except (TypeError, ValueError) as error:
        return None, f"embedded probe result is invalid JSON: {error}"
    if type(value) is not dict:
        return None, "embedded probe result is not an object"
    return value, None


def _bounded_tail(value: str | None) -> str | None:
    if value is None:
        return None
    return value[-_EVIDENCE_TAIL_CHARACTERS:]


def _exception_summary(error: BaseException | None) -> str | None:
    if error is None:
        return None
    return _bounded_tail(f"{type(error).__name__}: {error}")


def _timeout_stream(value: str | bytes | None) -> str:
    if type(value) is bytes:
        return value.decode(errors="replace")
    return value or ""


def _build_parent_evidence(
    attempt: _ProbeAttempt,
    cleanup: _CleanupOutcome,
    identity: dict[str, object],
) -> dict[str, object]:
    probe_status = attempt.result.get("status")
    probe_error = attempt.result.get("error")
    inspection_error = (
        attempt.action_error
        if isinstance(
            attempt.action_error,
            (_AuthenticatedInspectionError, _AuthenticatedInspectionTimeout),
        )
        else None
    )
    payload = dict(identity)
    payload.update(
        {
            "action_error": _exception_summary(attempt.action_error),
            "action_traceback_tail": _bounded_tail(attempt.action_traceback),
            "child_returncode": attempt.returncode,
            "child_stderr_tail": _bounded_tail(attempt.stderr),
            "child_stdout_tail": _bounded_tail(attempt.stdout),
            "cleanup": {
                "clean": cleanup.clean,
                "detail": cleanup.detail,
                "kill_sent": cleanup.kill_sent,
                "retire_attempted": cleanup.retire_attempted,
                "term_sent": cleanup.term_sent,
            },
            "cleanup_error": _exception_summary(attempt.cleanup_error),
            "cleanup_traceback_tail": _bounded_tail(attempt.cleanup_traceback),
            "inspection_error": _exception_summary(inspection_error),
            "probe_error_tail": (_bounded_tail(probe_error) if type(probe_error) is str else None),
            "probe_parse_error": _bounded_tail(attempt.parse_error),
            "probe_status": (_bounded_tail(probe_status) if type(probe_status) is str else None),
            "timed_out": attempt.timed_out,
        }
    )
    return payload


def _emit_parent_evidence(
    payload: dict[str, object],
    *,
    _print: Callable[..., object] = print,
) -> None:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    _print(_PARENT_PREFIX + encoded, flush=True)


def _raise_probe_failure(
    primary: BaseException,
    evidence_error: BaseException | None,
) -> None:
    if evidence_error is not None:
        raise primary from evidence_error
    raise primary


def _run_probe_lifecycle(
    *,
    action: Callable[[_ProbeAttempt], None],
    cleanup: Callable[[], _CleanupOutcome],
    evidence: Callable[[_ProbeAttempt, _CleanupOutcome], dict[str, object]],
    semantic: Callable[[_ProbeAttempt], None],
    ownership: _CleanupAssertionOwnership,
    _emit: Callable[[dict[str, object]], object] = _emit_parent_evidence,
) -> _ProbeAttempt:
    if ownership.evidence_emitted or ownership.body_owns_assertion:
        raise RuntimeError("parent evidence was already emitted")
    attempt = _ProbeAttempt()
    try:
        action(attempt)
    except _AuthenticatedInspectionTimeout as error:
        attempt.action_error = error
        attempt.action_traceback = traceback.format_exc()
    except subprocess.TimeoutExpired as error:
        attempt.timed_out = True
        attempt.stdout = _timeout_stream(error.stdout)
        attempt.stderr = _timeout_stream(error.stderr)
    except BaseException as error:
        attempt.action_error = error
        attempt.action_traceback = traceback.format_exc()

    try:
        parsed_result, attempt.parse_error = _probe_result(attempt.stdout)
    except BaseException as error:
        parsed_result = None
        attempt.parse_error = f"embedded probe result parsing raised {type(error).__name__}"
        if attempt.action_error is None and not attempt.timed_out:
            attempt.action_error = error
            attempt.action_traceback = traceback.format_exc()
    attempt.result = {} if parsed_result is None else parsed_result

    cleanup_outcome = ownership.cleanup_once(cleanup)
    attempt.cleanup_error = ownership.cleanup_error
    attempt.cleanup_traceback = ownership.cleanup_traceback

    try:
        parent_evidence = _build_parent_evidence(
            attempt,
            cleanup_outcome,
            evidence(attempt, cleanup_outcome),
        )
        _emit(parent_evidence)
    except BaseException as error:
        attempt.evidence_error = error
        attempt.evidence_traceback = traceback.format_exc()
        ownership.transfer_to_body(evidence_emitted=False)
    else:
        ownership.transfer_to_body(evidence_emitted=True)

    if attempt.action_error is not None:
        _raise_probe_failure(attempt.action_error, attempt.evidence_error)
    if attempt.timed_out:
        _raise_probe_failure(
            AssertionError("FreeCAD embedded probe timed out"),
            attempt.evidence_error,
        )
    if attempt.parse_error is not None:
        _raise_probe_failure(
            AssertionError(f"embedded probe output failure: {attempt.parse_error}"),
            attempt.evidence_error,
        )
    if attempt.returncode != 0:
        _raise_probe_failure(
            AssertionError(f"FreeCAD embedded probe returned {attempt.returncode}, expected 0"),
            attempt.evidence_error,
        )
    if attempt.cleanup_error is not None:
        _raise_probe_failure(attempt.cleanup_error, attempt.evidence_error)
    if not cleanup_outcome.clean:
        _raise_probe_failure(
            AssertionError(f"cleanup proof failed: {cleanup_outcome.detail}"),
            attempt.evidence_error,
        )
    if attempt.evidence_error is not None:
        raise attempt.evidence_error
    semantic(attempt)
    return attempt


def _finalize_probe_cleanup(
    cleanup: Callable[[], _CleanupOutcome],
    ownership: _CleanupAssertionOwnership,
) -> _CleanupOutcome:
    outcome = ownership.cleanup_once(cleanup)
    if ownership.body_owns_assertion or not ownership.launch_attempted:
        return outcome
    if ownership.cleanup_error is not None:
        raise ownership.cleanup_error
    if ownership.launch_attempted:
        assert outcome.clean, outcome
    return outcome


def _preflight_daemon_endpoint(endpoint: object) -> int:
    try:
        encoded = os.fsencode(endpoint)
    except (TypeError, UnicodeError) as error:
        raise ValueError("daemon endpoint path is invalid") from error
    if not encoded:
        raise ValueError("daemon endpoint path is empty")
    length = len(encoded)
    if length > _MAX_UNIX_ENDPOINT_BYTES:
        raise ValueError(f"daemon endpoint path length {length} exceeds {_MAX_UNIX_ENDPOINT_BYTES}")
    return length


def _runtime_daemon_endpoint() -> Path:
    return daemon_run_root(paths.data_root()) / DAEMON_ENDPOINT_NAME


def _invoke_after_endpoint_preflight(
    endpoint: object,
    invoke: Callable[[], object],
    *,
    ownership: _CleanupAssertionOwnership | None = None,
) -> object:
    _preflight_daemon_endpoint(endpoint)
    try:
        requested = os.fsencode(endpoint)
        expected = os.fsencode(_runtime_daemon_endpoint())
    except (TypeError, UnicodeError) as error:
        raise ValueError("daemon endpoint path is invalid") from error
    if requested != expected:
        raise ValueError("daemon endpoint does not match runtime endpoint")
    if ownership is not None:
        ownership.mark_launch_attempted()
    return invoke()


def _admit_canonical_private_root(spelling: Path) -> Path:
    try:
        resolved = spelling.resolve(strict=True)
        info = spelling.lstat()
    except OSError as error:
        raise ValueError("isolated root is unavailable") from error
    getuid = getattr(os, "geteuid", None)
    if spelling != resolved:
        raise ValueError("isolated root spelling is not canonical")
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (getuid is not None and info.st_uid != getuid())
    ):
        raise ValueError("isolated root is not owner-private")
    return resolved


def _canonical_darwin_temp_parent() -> Path:
    try:
        resolved = _DARWIN_TEMP_PARENT.resolve(strict=True)
    except OSError as error:
        raise ValueError("canonical Darwin temp parent is unavailable") from error
    if resolved != _DARWIN_TEMP_PARENT or not resolved.is_dir():
        raise ValueError("Darwin temp parent spelling is not canonical")
    return resolved


def _admit_c00b_isolated_root(spelling: Path) -> Path:
    root = _admit_canonical_private_root(spelling)
    suffix = root.name.removeprefix(_DARWIN_TEMP_PREFIX)
    if (
        root.parent != _DARWIN_TEMP_PARENT
        or not root.name.startswith(_DARWIN_TEMP_PREFIX)
        or len(suffix) != 8
    ):
        raise ValueError("isolated root does not match /private/tmp/vc-c00b-<8>")
    return root


def _freecad_probe_command(freecadcmd: Path, repo: Path) -> list[str]:
    source_root = (repo / "src").resolve()
    fixture_root = (repo / "tests" / "fixtures" / "freecad_workbench").resolve()
    return [
        str(freecadcmd),
        "-P",
        str(source_root),
        "-P",
        str(fixture_root),
        str(fixture_root / "bootstrap_probe.py"),
    ]


def test_canonical_private_temp_root_admission_rejects_alias(
    tmp_path: Path,
) -> None:
    canonical_temp_parent = Path(tempfile.gettempdir()).resolve(strict=True)
    with tempfile.TemporaryDirectory(
        prefix="vibecad-c00b-admission-",
        dir=str(canonical_temp_parent),
    ) as spelling:
        root = Path(spelling)
        assert _admit_canonical_private_root(root) == root

    real_root = tmp_path.resolve() / "real-root"
    real_root.mkdir(mode=0o700)
    real_root.chmod(0o700)
    alias = tmp_path.resolve() / "root-alias"
    alias.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="spelling is not canonical"):
        _admit_canonical_private_root(alias)


def test_darwin_process_token_layout_and_stable_capture() -> None:
    assert ctypes.sizeof(_ProcBSDInfo) == 136
    assert _ProcBSDInfo.pbi_pgid.offset == 100
    assert _ProcBSDInfo.pbi_start_tvsec.offset == 120
    assert _ProcBSDInfo.pbi_start_tvusec.offset == 128
    sample = (9_001, 1_700_000_000, 123_456, 501, 9_001)

    assert _darwin_process_token(
        9_001,
        _read_info=lambda _pid: sample,
        _getsid=lambda _pid: 9_001,
        _geteuid=lambda: 501,
    ) == _DarwinProcessToken(
        pid=9_001,
        birth_sec=1_700_000_000,
        birth_usec=123_456,
        euid=501,
        pgid=9_001,
        sid=9_001,
    )

    def absent(_pid: int) -> tuple[int, int, int, int, int]:
        raise ProcessLookupError

    with pytest.raises(ProcessLookupError):
        _darwin_process_token(9_001, _read_info=absent)

    def incomplete(_pid: int) -> tuple[int, int, int, int, int]:
        raise RuntimeError("short proc_pidinfo read")

    with pytest.raises(RuntimeError, match="short proc_pidinfo"):
        _darwin_process_token(9_001, _read_info=incomplete)

    samples = iter((sample, (*sample[:-1], 9_002)))
    with pytest.raises(RuntimeError, match="changed during capture"):
        _darwin_process_token(
            9_001,
            _read_info=lambda _pid: next(samples),
            _getsid=lambda _pid: 9_001,
            _geteuid=lambda: 501,
        )


@pytest.mark.parametrize(
    ("sample", "sid", "euid"),
    (
        ((9_001, 1_700_000_000, 1, 502, 9_001), 9_001, 501),
        ((9_001, 1_700_000_000, 1, 501, 9_002), 9_001, 501),
        ((9_001, 1_700_000_000, 1, 501, 9_001), 9_002, 501),
    ),
)
def test_darwin_process_token_rejects_wrong_owner_group_or_session(
    sample: tuple[int, int, int, int, int],
    sid: int,
    euid: int,
) -> None:
    with pytest.raises(ValueError, match="ownership or session"):
        _darwin_process_token(
            9_001,
            _read_info=lambda _pid: sample,
            _getsid=lambda _pid: sid,
            _geteuid=lambda: euid,
        )


def test_safe_absent_or_empty_run_root_rejects_unsafe_state(tmp_path: Path) -> None:
    run_root = tmp_path / "daemon"
    assert _safe_absent_or_empty_run_root(run_root)
    run_root.mkdir(mode=0o700)
    run_root.chmod(0o700)
    assert _safe_absent_or_empty_run_root(run_root)
    run_root.chmod(0o755)
    assert not _safe_absent_or_empty_run_root(run_root)
    run_root.chmod(0o700)
    (run_root / "unexpected").write_bytes(b"x")
    assert not _safe_absent_or_empty_run_root(run_root)
    (run_root / "unexpected").unlink()
    run_root.rmdir()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    run_root.symlink_to(target, target_is_directory=True)
    assert not _safe_absent_or_empty_run_root(run_root)


@pytest.mark.parametrize("semantic", ("timeout", "semantic_red"))
def test_probe_action_runs_cleanup_before_semantic_result(
    semantic: str,
) -> None:
    events: list[str] = []

    def action() -> int:
        events.append("launch")
        if semantic == "timeout":
            raise subprocess.TimeoutExpired(["freecadcmd"], 35)
        return 1

    try:
        result = _run_with_unconditional_cleanup(
            action,
            lambda: events.append("cleanup"),
        )
    except subprocess.TimeoutExpired:
        events.append("semantic")
    else:
        assert result == 1
        events.append("semantic")
    assert events == ["launch", "cleanup", "semantic"]


def test_probe_result_accepts_one_exact_payload_among_unrelated_output() -> None:
    expected = {"status": "ok", "daemon_pid": 9_100}
    stdout = "\n".join(
        (
            "FreeCAD startup banner",
            _PROBE_PREFIX + json.dumps(expected, sort_keys=True),
            "FreeCAD shutdown message",
        )
    )

    assert _probe_result(stdout) == (expected, None)


@pytest.mark.parametrize(
    "stdout",
    (
        "FreeCAD produced no embedded probe payload",
        _PROBE_PREFIX + "{malformed",
    ),
)
def test_probe_parse_failure_cleans_before_semantic_assertion(
    stdout: str,
    tmp_path: Path,
) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    events: list[str] = []
    retire_calls: list[dict[str, object]] = []
    signals: list[tuple[int, int]] = []

    def absent_state(_root: Path) -> object:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=absent_state,
        _retire=lambda **kwargs: retire_calls.append(kwargs) or False,
        _capture_token=lambda _pid: (_ for _ in ()).throw(
            AssertionError("no unobserved PID may be inspected")
        ),
        _killpg=lambda pid, sig: signals.append((pid, sig)),
    )
    guard.require_cold()

    def parse() -> tuple[dict[str, object] | None, str | None]:
        events.append("parse")
        return _probe_result(stdout)

    cleanup_outcomes: list[_CleanupOutcome] = []

    def cleanup() -> None:
        events.append("cleanup")
        cleanup_outcomes.append(guard.cleanup())

    result, parse_error = _run_with_unconditional_cleanup(parse, cleanup)
    events.append("semantic")

    assert events == ["parse", "cleanup", "semantic"]
    assert result is None
    assert parse_error is not None
    assert cleanup_outcomes == [_CleanupOutcome(False, False, False, False, "publication_unproven")]
    assert not guard.original_token_absent
    assert retire_calls == []
    assert signals == []


def test_cleanup_guard_retires_once_and_is_idempotent(tmp_path: Path) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    receipt = SimpleNamespace(daemon_id="daemon_" + "1" * 32, pid=9_101)
    state = SimpleNamespace(receipt=receipt)
    current_state: list[object | None] = [None]
    current_token: list[_DarwinProcessToken | None] = [
        _DarwinProcessToken(9_101, 100, 1, os.geteuid(), 9_101, 9_101)
    ]
    retire_calls: list[dict[str, object]] = []
    signals: list[tuple[int, int]] = []

    def read_state(_root: Path) -> object:
        if current_state[0] is None:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return current_state[0]

    def capture(_pid: int) -> _DarwinProcessToken:
        if current_token[0] is None:
            raise ProcessLookupError
        return current_token[0]

    def retire(**kwargs: object) -> bool:
        retire_calls.append(kwargs)
        current_state[0] = None
        current_token[0] = None
        return True

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=read_state,
        _retire=retire,
        _capture_token=capture,
        _killpg=lambda pid, sig: signals.append((pid, sig)),
    )
    guard.require_cold()
    current_state[0] = state
    assert guard.observe_publication() is state

    first = guard.cleanup()
    second = guard.cleanup()

    assert first == _CleanupOutcome(True, True, False, False, "retired")
    assert second is first
    assert len(retire_calls) == 1
    assert retire_calls[0]["expected_daemon_id"] == receipt.daemon_id
    assert retire_calls[0]["run_root"] == run_root
    assert signals == []


@pytest.mark.parametrize("escalation", ("term", "kill"))
def test_cleanup_guard_uses_one_bounded_exact_generation_signal(
    escalation: str,
    tmp_path: Path,
) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    receipt = SimpleNamespace(daemon_id="daemon_" + "2" * 32, pid=9_102)
    current_state: list[object | None] = [None]
    token = _DarwinProcessToken(9_102, 101, 2, os.geteuid(), 9_102, 9_102)
    current_token: list[_DarwinProcessToken | None] = [token]
    signals: list[tuple[int, int]] = []
    now = [0.0]

    def read_state(_root: Path) -> object:
        if current_state[0] is None:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return current_state[0]

    def capture(_pid: int) -> _DarwinProcessToken:
        if current_token[0] is None:
            raise ProcessLookupError
        return current_token[0]

    def killpg(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        if sig == signal.SIGTERM and escalation == "term":
            current_state[0] = None
            current_token[0] = None
        if sig == signal.SIGKILL:
            current_state[0] = None
            current_token[0] = None

    def sleep(duration: float) -> None:
        now[0] += duration

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=read_state,
        _retire=lambda **_kwargs: False,
        _capture_token=capture,
        _killpg=killpg,
        _clock=lambda: now[0],
        _sleep=sleep,
    )
    guard.require_cold()
    current_state[0] = SimpleNamespace(receipt=receipt)
    guard.observe_publication()

    outcome = guard.cleanup()

    expected_signals = [(9_102, signal.SIGTERM)]
    if escalation == "kill":
        expected_signals.append((9_102, signal.SIGKILL))
    assert signals == expected_signals
    assert outcome.clean
    assert outcome.term_sent
    assert outcome.kill_sent is (escalation == "kill")


@pytest.mark.parametrize(
    ("transition", "expected_clean"),
    (("replacement", False), ("removal", True), ("pid_reuse", False)),
)
def test_cleanup_guard_never_signals_replacement_removal_or_pid_reuse(
    transition: str,
    expected_clean: bool,
    tmp_path: Path,
) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    original_receipt = SimpleNamespace(daemon_id="daemon_" + "3" * 32, pid=9_103)
    replacement_receipt = SimpleNamespace(daemon_id="daemon_" + "4" * 32, pid=9_103)
    original_token = _DarwinProcessToken(
        9_103,
        102,
        3,
        os.geteuid(),
        9_103,
        9_103,
    )
    replacement_token = _DarwinProcessToken(
        9_103,
        103,
        4,
        os.geteuid(),
        9_103,
        9_103,
    )
    current_state: list[object | None] = [None]
    current_token: list[_DarwinProcessToken | None] = [original_token]
    signals: list[tuple[int, int]] = []

    def read_state(_root: Path) -> object:
        if current_state[0] is None:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return current_state[0]

    def capture(_pid: int) -> _DarwinProcessToken:
        if current_token[0] is None:
            raise ProcessLookupError
        return current_token[0]

    def retire(**_kwargs: object) -> bool:
        if transition == "replacement":
            current_state[0] = SimpleNamespace(receipt=replacement_receipt)
            current_token[0] = replacement_token
        elif transition == "removal":
            current_state[0] = None
            current_token[0] = None
        else:
            current_token[0] = replacement_token
        return False

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=read_state,
        _retire=retire,
        _capture_token=capture,
        _killpg=lambda pid, sig: signals.append((pid, sig)),
    )
    guard.require_cold()
    current_state[0] = SimpleNamespace(receipt=original_receipt)
    guard.observe_publication()

    outcome = guard.cleanup()

    assert outcome.clean is expected_clean
    assert signals == []


@pytest.mark.parametrize("token_error", (ValueError, RuntimeError))
def test_cleanup_guard_forbids_signal_when_process_identity_is_ambiguous(
    token_error: type[Exception],
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "daemon"
    receipt = SimpleNamespace(daemon_id="daemon_" + "5" * 32, pid=9_104)
    signals: list[tuple[int, int]] = []

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=lambda _root: SimpleNamespace(receipt=receipt),
        _retire=lambda **_kwargs: False,
        _capture_token=lambda _pid: (_ for _ in ()).throw(token_error("ambiguous")),
        _killpg=lambda pid, sig: signals.append((pid, sig)),
    )
    guard.require_cold()
    guard.observe_publication()

    outcome = guard.cleanup()

    assert not outcome.clean
    assert outcome.detail == "signal_forbidden"
    assert signals == []


def test_cleanup_guard_latches_one_post_auth_token_ambiguity(
    tmp_path: Path,
) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    receipt = SimpleNamespace(daemon_id="daemon_" + "6" * 32, pid=9_106)
    token = _DarwinProcessToken(
        9_106,
        106,
        7,
        os.geteuid(),
        9_106,
        9_106,
    )
    current_state: list[object | None] = [None]
    current_token: list[_DarwinProcessToken | None] = [token]
    capture_calls = 0
    signals: list[tuple[int, int]] = []

    def read_state(_root: Path) -> object:
        if current_state[0] is None:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return current_state[0]

    def capture(_pid: int) -> _DarwinProcessToken:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            raise RuntimeError("short post-auth process identity read")
        if current_token[0] is None:
            raise ProcessLookupError
        return current_token[0]

    def killpg(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        current_state[0] = None
        current_token[0] = None

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=read_state,
        _retire=lambda **_kwargs: False,
        _capture_token=capture,
        _killpg=killpg,
    )
    guard.require_cold()
    current_state[0] = SimpleNamespace(receipt=receipt)
    guard.observe_publication()

    outcome = guard.cleanup()

    assert outcome == _CleanupOutcome(
        False,
        True,
        False,
        False,
        "signal_forbidden",
    )
    assert capture_calls == 2
    assert signals == []


@pytest.mark.parametrize("retirement_succeeds", (False, True))
def test_cleanup_guard_latches_one_post_auth_publication_ambiguity(
    retirement_succeeds: bool,
    tmp_path: Path,
) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    receipt_path = run_root / DAEMON_RECEIPT_NAME
    receipt = SimpleNamespace(daemon_id="daemon_" + "7" * 32, pid=9_107)
    token = _DarwinProcessToken(
        9_107,
        107,
        8,
        os.geteuid(),
        9_107,
        9_107,
    )
    current_state: list[object | None] = [None]
    current_token: list[_DarwinProcessToken | None] = [token]
    read_calls = 0
    signals: list[tuple[int, int]] = []

    def read_state(_root: Path) -> object:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 2:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        if current_state[0] is None:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return current_state[0]

    def capture(_pid: int) -> _DarwinProcessToken:
        if current_token[0] is None:
            raise ProcessLookupError
        return current_token[0]

    def killpg(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        current_state[0] = None
        current_token[0] = None
        receipt_path.unlink()

    def retire(**_kwargs: object) -> bool:
        if retirement_succeeds:
            current_state[0] = None
            current_token[0] = None
            receipt_path.unlink()
        return retirement_succeeds

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=read_state,
        _retire=retire,
        _capture_token=capture,
        _killpg=killpg,
    )
    guard.require_cold()
    run_root.mkdir(mode=0o700)
    run_root.chmod(0o700)
    receipt_path.write_bytes(b"transient ambiguous publication")
    current_state[0] = SimpleNamespace(receipt=receipt)
    guard.observe_publication()

    outcome = guard.cleanup()

    assert outcome == (
        _CleanupOutcome(True, True, False, False, "retired")
        if retirement_succeeds
        else _CleanupOutcome(False, True, False, False, "signal_forbidden")
    )
    assert read_calls >= 3
    assert signals == []


@pytest.mark.parametrize("transition", ("publication", "token"))
def test_cleanup_guard_revalidates_publication_and_token_before_kill(
    transition: str,
    tmp_path: Path,
) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    original_receipt = SimpleNamespace(daemon_id="daemon_" + "6" * 32, pid=9_105)
    replacement_receipt = SimpleNamespace(
        daemon_id="daemon_" + "7" * 32,
        pid=9_105,
    )
    original_token = _DarwinProcessToken(
        9_105,
        104,
        5,
        os.geteuid(),
        9_105,
        9_105,
    )
    replacement_token = _DarwinProcessToken(
        9_105,
        105,
        6,
        os.geteuid(),
        9_105,
        9_105,
    )
    current_state: list[object | None] = [None]
    current_token = [original_token]
    signals: list[tuple[int, int]] = []
    now = [0.0]

    def read_state(_root: Path) -> object:
        if current_state[0] is None:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return current_state[0]

    def killpg(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        if sig != signal.SIGTERM:
            return
        if transition == "publication":
            current_state[0] = SimpleNamespace(receipt=replacement_receipt)
        else:
            current_token[0] = replacement_token

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=read_state,
        _retire=lambda **_kwargs: False,
        _capture_token=lambda _pid: current_token[0],
        _killpg=killpg,
        _clock=lambda: now[0],
        _sleep=lambda duration: now.__setitem__(0, now[0] + duration),
    )
    guard.require_cold()
    current_state[0] = SimpleNamespace(receipt=original_receipt)
    guard.observe_publication()

    outcome = guard.cleanup()

    assert outcome == _CleanupOutcome(
        False,
        True,
        True,
        False,
        "kill_forbidden",
    )
    assert signals == [(9_105, signal.SIGTERM)]


def test_embedded_probe_command_pins_both_repository_import_roots() -> None:
    repo = Path(__file__).resolve().parent.parent
    freecadcmd = Path("/verified-prefix/bin/freecadcmd")

    assert _freecad_probe_command(freecadcmd, repo) == [
        str(freecadcmd),
        "-P",
        str((repo / "src").resolve()),
        "-P",
        str((repo / "tests" / "fixtures" / "freecad_workbench").resolve()),
        str((repo / "tests" / "fixtures" / "freecad_workbench" / "bootstrap_probe.py").resolve()),
    ]


def _fake_probe_action(
    *,
    stdout: str,
    stderr: str = "",
    returncode: int = 0,
    events: list[str] | None = None,
) -> Callable[[_ProbeAttempt], None]:
    def action(attempt: _ProbeAttempt) -> None:
        if events is not None:
            events.append("action")
        attempt.returncode = returncode
        attempt.stdout = stdout
        attempt.stderr = stderr

    return action


def _valid_probe_stdout(
    *,
    status_value: str = "ok",
    error_value: str | None = None,
) -> str:
    payload: dict[str, object] = {"status": status_value}
    if error_value is not None:
        payload["error"] = error_value
    return _PROBE_PREFIX + json.dumps(payload, sort_keys=True)


class _SyntheticActionError(BaseException):
    pass


class _SyntheticCleanupError(BaseException):
    pass


class _SyntheticEvidenceError(RuntimeError):
    pass


def test_c00b_darwin_socket_layout_and_preflight_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_home = Path(
        "/private/var/folders/qk/0_b6krc135j3lrz44krcddr40000gn/T/"
        "vibecad-c00b-12345678/vibecad-home"
    )
    monkeypatch.setenv("VIBECAD_HOME", str(old_home))
    old_endpoint = daemon_run_root(paths.data_root()) / DAEMON_ENDPOINT_NAME
    assert paths.data_root() == old_home / "data"

    assert len(os.fsencode(old_endpoint)) == 115
    assert len(os.fsencode(old_endpoint)) > 103

    short_root = Path("/private/tmp/vc-c00b-12345678")
    short_home = short_root / "vibecad-home"
    monkeypatch.setenv("VIBECAD_HOME", str(short_home))
    short_endpoint = daemon_run_root(paths.data_root()) / DAEMON_ENDPOINT_NAME
    assert short_root.parent == Path("/private/tmp")
    assert short_root.name == "vc-c00b-12345678"
    assert paths.data_root() == short_home / "data"
    assert len(os.fsencode(short_endpoint)) == 66
    assert _preflight_daemon_endpoint(short_endpoint) == 66
    with pytest.raises(ValueError, match="115 exceeds 103"):
        _preflight_daemon_endpoint(old_endpoint)
    with pytest.raises(ValueError, match="empty"):
        _preflight_daemon_endpoint("")


def test_c00b_real_harness_preflight_fails_closed_before_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[bool] = []
    long_home = Path("/") / ("x" * 79)
    monkeypatch.setenv("VIBECAD_HOME", str(long_home))
    long_endpoint = daemon_run_root(paths.data_root()) / DAEMON_ENDPOINT_NAME
    assert len(os.fsencode(long_endpoint)) == 104

    with pytest.raises(ValueError, match="104 exceeds 103"):
        _invoke_after_endpoint_preflight(
            long_endpoint,
            lambda: launched.append(True),
        )

    assert launched == []


def test_c00b_endpoint_preflight_rejects_non_runtime_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    short_home = Path("/private/tmp/vc-c00b-12345678/vibecad-home")
    monkeypatch.setenv("VIBECAD_HOME", str(short_home))
    expected_endpoint = daemon_run_root(paths.data_root()) / DAEMON_ENDPOINT_NAME
    unrelated_endpoint = expected_endpoint.with_name("untrusted.sock")
    launched: list[bool] = []

    with pytest.raises(ValueError, match="does not match runtime endpoint"):
        _invoke_after_endpoint_preflight(
            unrelated_endpoint,
            lambda: launched.append(True),
        )

    assert launched == []


def test_probe_lifecycle_success_cleans_emits_then_validates() -> None:
    events: list[str] = []
    emitted: list[dict[str, object]] = []
    ownership = _CleanupAssertionOwnership()

    def cleanup() -> _CleanupOutcome:
        events.append("cleanup")
        return _CleanupOutcome(True, True, False, False, "retired")

    def emit(evidence: dict[str, object]) -> None:
        events.append("evidence")
        emitted.append(evidence)

    def semantic(attempt: _ProbeAttempt) -> None:
        events.append("semantic")
        assert attempt.result == {"status": "ok"}

    attempt = _run_probe_lifecycle(
        action=_fake_probe_action(
            stdout=_valid_probe_stdout(),
            events=events,
        ),
        cleanup=cleanup,
        evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
        semantic=semantic,
        ownership=ownership,
        _emit=emit,
    )

    assert events == ["action", "cleanup", "evidence", "semantic"]
    assert len(emitted) == 1
    assert emitted[0]["cleanup"] == {
        "clean": True,
        "detail": "retired",
        "kill_sent": False,
        "retire_attempted": True,
        "term_sent": False,
    }
    assert attempt.result == {"status": "ok"}
    assert ownership.body_owns_assertion
    assert ownership.evidence_emitted


def test_probe_lifecycle_timeout_cleans_and_emits_before_failure() -> None:
    events: list[str] = []
    emitted: list[dict[str, object]] = []

    def action(_attempt: _ProbeAttempt) -> None:
        events.append("action")
        raise subprocess.TimeoutExpired(
            ["freecadcmd"],
            _PROBE_TIMEOUT_SECONDS,
            output="timeout stdout",
            stderr="timeout stderr",
        )

    with pytest.raises(AssertionError, match="timed out"):
        _run_probe_lifecycle(
            action=action,
            cleanup=lambda: (
                events.append("cleanup")
                or _CleanupOutcome(False, False, False, False, "publication_unproven")
            ),
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: events.append("semantic"),
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda value: (events.append("evidence"), emitted.append(value)),
        )

    assert events == ["action", "cleanup", "evidence"]
    assert emitted[0]["timed_out"] is True
    assert emitted[0]["child_stdout_tail"] == "timeout stdout"
    assert emitted[0]["child_stderr_tail"] == "timeout stderr"
    assert emitted[0]["cleanup"]["clean"] is False


@pytest.mark.parametrize("inspection_failure", ("timeout", "nonzero"))
def test_authenticated_inspection_failure_preserves_freecad_result(
    inspection_failure: str,
) -> None:
    events: list[str] = []
    emitted: list[dict[str, object]] = []

    def action(attempt: _ProbeAttempt) -> None:
        events.append("freecad")
        attempt.returncode = 0
        attempt.stdout = _valid_probe_stdout() + "\nfreecad stdout"
        attempt.stderr = "freecad stderr"
        events.append("inspection")
        if inspection_failure == "timeout":
            raise _AuthenticatedInspectionTimeout(
                ["ps", "-p", "9001"],
                5,
                output="inspection stdout",
                stderr="inspection stderr",
            )
        raise _AuthenticatedInspectionError("authenticated process inspection returned 7")

    expected_error = (
        _AuthenticatedInspectionTimeout
        if inspection_failure == "timeout"
        else _AuthenticatedInspectionError
    )
    with pytest.raises(expected_error, match="ps|inspection returned 7"):
        _run_probe_lifecycle(
            action=action,
            cleanup=lambda: (
                events.append("cleanup") or _CleanupOutcome(True, True, False, False, "retired")
            ),
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: events.append("semantic"),
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda value: (events.append("evidence"), emitted.append(value)),
        )

    assert events == ["freecad", "inspection", "cleanup", "evidence"]
    assert emitted[0]["child_returncode"] == 0
    assert emitted[0]["child_stdout_tail"].endswith("freecad stdout")
    assert emitted[0]["child_stderr_tail"] == "freecad stderr"
    assert emitted[0]["timed_out"] is False
    assert "Inspection" in emitted[0]["action_error"]
    assert emitted[0]["inspection_error"] == emitted[0]["action_error"]


@pytest.mark.parametrize(
    ("stdout", "message"),
    (
        ("no payload", "expected one embedded probe result"),
        (_PROBE_PREFIX + "{malformed", "invalid JSON"),
    ),
)
def test_probe_lifecycle_missing_or_malformed_output_fails_after_evidence(
    stdout: str,
    message: str,
) -> None:
    events: list[str] = []
    emitted: list[dict[str, object]] = []

    with pytest.raises(AssertionError, match=message):
        _run_probe_lifecycle(
            action=_fake_probe_action(
                stdout=stdout,
                returncode=17,
                events=events,
            ),
            cleanup=lambda: (
                events.append("cleanup")
                or _CleanupOutcome(False, True, True, False, "kill_forbidden")
            ),
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: events.append("semantic"),
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda value: (events.append("evidence"), emitted.append(value)),
        )

    assert events == ["action", "cleanup", "evidence"]
    assert emitted[0]["probe_parse_error"]


def test_probe_lifecycle_returncode_red_precedes_cleanup_red() -> None:
    events: list[str] = []
    emitted: list[dict[str, object]] = []

    with pytest.raises(AssertionError, match="returned 17"):
        _run_probe_lifecycle(
            action=_fake_probe_action(
                stdout=_valid_probe_stdout(),
                returncode=17,
                events=events,
            ),
            cleanup=lambda: (
                events.append("cleanup")
                or _CleanupOutcome(False, True, True, False, "kill_forbidden")
            ),
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: events.append("semantic"),
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda value: (events.append("evidence"), emitted.append(value)),
        )

    assert events == ["action", "cleanup", "evidence"]
    assert emitted[0]["child_returncode"] == 17
    assert emitted[0]["cleanup"]["clean"] is False


def test_probe_lifecycle_semantic_red_is_after_cleanup_and_evidence() -> None:
    events: list[str] = []

    def semantic(_attempt: _ProbeAttempt) -> None:
        events.append("semantic")
        raise AssertionError("semantic red")

    with pytest.raises(AssertionError, match="semantic red"):
        _run_probe_lifecycle(
            action=_fake_probe_action(
                stdout=_valid_probe_stdout(),
                events=events,
            ),
            cleanup=lambda: (
                events.append("cleanup") or _CleanupOutcome(True, True, False, False, "retired")
            ),
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=semantic,
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda _value: events.append("evidence"),
        )

    assert events == ["action", "cleanup", "evidence", "semantic"]


def test_probe_lifecycle_cleanup_only_red_precedes_semantics() -> None:
    events: list[str] = []
    emitted: list[dict[str, object]] = []

    with pytest.raises(AssertionError, match="cleanup proof failed"):
        _run_probe_lifecycle(
            action=_fake_probe_action(
                stdout=_valid_probe_stdout(),
                events=events,
            ),
            cleanup=lambda: (
                events.append("cleanup")
                or _CleanupOutcome(False, True, True, False, "kill_forbidden")
            ),
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: events.append("semantic"),
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda value: (events.append("evidence"), emitted.append(value)),
        )

    assert events == ["action", "cleanup", "evidence"]
    assert emitted[0]["cleanup"] == {
        "clean": False,
        "detail": "kill_forbidden",
        "kill_sent": False,
        "retire_attempted": True,
        "term_sent": True,
    }


def test_probe_lifecycle_action_and_cleanup_red_reports_action_first() -> None:
    events: list[str] = []
    emitted: list[dict[str, object]] = []

    class SyntheticActionError(BaseException):
        pass

    def action(_attempt: _ProbeAttempt) -> None:
        events.append("action")
        raise SyntheticActionError("action exploded")

    with pytest.raises(SyntheticActionError, match="action exploded"):
        _run_probe_lifecycle(
            action=action,
            cleanup=lambda: (
                events.append("cleanup")
                or _CleanupOutcome(False, False, False, False, "publication_unproven")
            ),
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: events.append("semantic"),
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda value: (events.append("evidence"), emitted.append(value)),
        )

    assert events == ["action", "cleanup", "evidence"]
    assert emitted[0]["action_error"] == "SyntheticActionError: action exploded"
    assert emitted[0]["action_traceback_tail"].endswith("SyntheticActionError: action exploded\n")
    assert emitted[0]["cleanup"]["clean"] is False


def test_probe_child_text_never_authenticates_cleanup(
    tmp_path: Path,
) -> None:
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    retire_calls: list[dict[str, object]] = []
    signals: list[tuple[int, int]] = []
    payload = {
        "daemon_id": "daemon_" + "f" * 32,
        "daemon_pid": 9_999,
        "status": "ok",
    }

    def absent_state(_root: Path) -> object:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    guard = _DaemonCleanupGuard(
        run_root,
        _read_state=absent_state,
        _retire=lambda **kwargs: retire_calls.append(kwargs) or True,
        _capture_token=lambda _pid: (_ for _ in ()).throw(
            AssertionError("child text must not authorize PID inspection")
        ),
        _killpg=lambda pid, sig: signals.append((pid, sig)),
    )
    guard.require_cold()

    with pytest.raises(AssertionError, match="cleanup proof failed"):
        _run_probe_lifecycle(
            action=_fake_probe_action(
                stdout=_PROBE_PREFIX + json.dumps(payload, sort_keys=True),
            ),
            cleanup=guard.cleanup,
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: None,
            ownership=_CleanupAssertionOwnership(),
            _emit=lambda _value: None,
        )

    assert guard.daemon_id is None
    assert guard.daemon_pid is None
    assert retire_calls == []
    assert signals == []


def test_parent_evidence_is_single_bounded_deterministic_json() -> None:
    attempt = _ProbeAttempt(
        returncode=17,
        stdout="A" * 2_100,
        stderr="B" * 2_100,
        parse_error="P" * 2_100,
        result={"status": "S" * 2_100, "error": "C" * 2_100},
        action_error=ValueError("D" * 2_100),
        action_traceback="T" * 2_100,
    )
    payload = _build_parent_evidence(
        attempt,
        _CleanupOutcome(False, True, True, True, "cleanup_unresolved"),
        {"identity": "synthétique"},
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    _emit_parent_evidence(
        payload,
        _print=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert kwargs == {"flush": True}
    assert len(args) == 1
    line = args[0]
    assert type(line) is str
    assert line.startswith(_PARENT_PREFIX)
    encoded = line.removeprefix(_PARENT_PREFIX)
    assert encoded == json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert "synthétique" not in encoded
    assert "\\u00e9" in encoded
    assert len(payload["child_stdout_tail"]) == 2_000
    assert len(payload["child_stderr_tail"]) == 2_000
    assert len(payload["probe_parse_error"]) == 2_000
    assert len(payload["probe_error_tail"]) == 2_000
    assert len(payload["probe_status"]) == 2_000
    assert len(payload["action_error"]) == 2_000
    assert len(payload["action_traceback_tail"]) == 2_000
    assert len(encoded) < 15_000


def test_cleanup_finalizer_preserves_prelaunch_primary_and_body_ownership() -> None:
    outcome = _CleanupOutcome(
        False,
        False,
        False,
        False,
        "publication_unproven",
    )
    early_ownership = _CleanupAssertionOwnership()
    early_events: list[str] = []

    def prelaunch_cleanup() -> _CleanupOutcome:
        early_events.append("cleanup")
        return outcome

    with pytest.raises(RuntimeError, match="prelaunch setup failed"):
        try:
            early_events.append("setup_error")
            raise RuntimeError("prelaunch setup failed")
        finally:
            _finalize_probe_cleanup(prelaunch_cleanup, early_ownership)

    assert early_events == ["setup_error", "cleanup"]

    launched_ownership = _CleanupAssertionOwnership()
    launched_ownership.mark_launch_attempted()
    with pytest.raises(AssertionError, match="publication_unproven"):
        _finalize_probe_cleanup(lambda: outcome, launched_ownership)

    cleanup_calls = 0
    body_ownership = _CleanupAssertionOwnership()

    def cleanup() -> _CleanupOutcome:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return outcome

    with pytest.raises(RuntimeError, match="action exploded"):
        _run_probe_lifecycle(
            action=lambda _attempt: (_ for _ in ()).throw(RuntimeError("action exploded")),
            cleanup=cleanup,
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: None,
            ownership=body_ownership,
            _emit=lambda _value: None,
        )
    assert body_ownership.evidence_emitted
    assert body_ownership.body_owns_assertion

    assert _finalize_probe_cleanup(cleanup, body_ownership) is outcome
    assert cleanup_calls == 1


def test_cleanup_baseexception_is_not_repeated_by_finalizer() -> None:
    events: list[str] = []
    ownership = _CleanupAssertionOwnership()

    class SyntheticCleanupError(BaseException):
        pass

    def cleanup() -> _CleanupOutcome:
        events.append("cleanup_term_side_effect")
        raise SyntheticCleanupError("cleanup failed after TERM-like side effect")

    with pytest.raises(SyntheticCleanupError, match="TERM-like side effect"):
        _run_probe_lifecycle(
            action=_fake_probe_action(
                stdout=_valid_probe_stdout(),
                events=events,
            ),
            cleanup=cleanup,
            evidence=lambda _attempt, _outcome: {"identity": "synthetic"},
            semantic=lambda _attempt: events.append("semantic"),
            ownership=ownership,
            _emit=lambda _value: events.append("evidence"),
        )
    events.append("body_error")

    assert _finalize_probe_cleanup(cleanup, ownership) == _CleanupOutcome(
        False,
        False,
        False,
        False,
        "cleanup_error",
    )
    assert events == [
        "action",
        "cleanup_term_side_effect",
        "evidence",
        "body_error",
    ]


@pytest.mark.parametrize("evidence_stage", ("build", "stringify", "emit"))
@pytest.mark.parametrize(
    ("primary_stage", "expected_type", "expected_message"),
    (
        ("action", _SyntheticActionError, "action exploded"),
        ("cleanup_proof", AssertionError, "signal_forbidden"),
        ("cleanup_baseexception", _SyntheticCleanupError, "TERM-like side effect"),
    ),
)
def test_existing_primary_precedes_compound_evidence_failure(
    evidence_stage: str,
    primary_stage: str,
    expected_type: type[BaseException],
    expected_message: str,
) -> None:
    events: list[str] = []
    attempts: list[_ProbeAttempt] = []
    ownership = _CleanupAssertionOwnership()

    def action(attempt: _ProbeAttempt) -> None:
        events.append("action")
        attempts.append(attempt)
        attempt.returncode = 0
        attempt.stdout = _valid_probe_stdout()
        if primary_stage == "action":
            raise _SyntheticActionError("action exploded")

    def cleanup() -> _CleanupOutcome:
        events.append("cleanup")
        if primary_stage == "cleanup_baseexception":
            raise _SyntheticCleanupError("cleanup failed after TERM-like side effect")
        if primary_stage == "cleanup_proof":
            return _CleanupOutcome(False, True, True, False, "signal_forbidden")
        return _CleanupOutcome(True, True, False, False, "retired")

    def evidence(
        _attempt: _ProbeAttempt,
        _outcome: _CleanupOutcome,
    ) -> dict[str, object]:
        events.append("evidence_build")
        if evidence_stage == "build":
            raise _SyntheticEvidenceError("evidence build failed")
        if evidence_stage == "stringify":
            return {"identity": object()}
        return {"identity": "synthetic"}

    def emit(value: dict[str, object]) -> None:
        events.append("evidence_emit")
        if evidence_stage == "stringify":
            _emit_parent_evidence(value, _print=lambda *_args, **_kwargs: None)
            raise AssertionError("unserializable evidence unexpectedly emitted")
        raise _SyntheticEvidenceError("evidence emit failed")

    with pytest.raises(expected_type, match=expected_message) as raised:
        _run_probe_lifecycle(
            action=action,
            cleanup=cleanup,
            evidence=evidence,
            semantic=lambda _attempt: events.append("semantic"),
            ownership=ownership,
            _emit=emit,
        )

    evidence_error = raised.value.__cause__
    expected_evidence_type = TypeError if evidence_stage == "stringify" else _SyntheticEvidenceError
    assert type(evidence_error) is expected_evidence_type
    if evidence_stage == "stringify":
        assert "not JSON serializable" in str(evidence_error)
    else:
        assert f"evidence {evidence_stage} failed" in str(evidence_error)
    assert attempts[0].evidence_error is evidence_error
    assert attempts[0].evidence_traceback

    outcome = _finalize_probe_cleanup(cleanup, ownership)
    assert (
        outcome.detail
        == {
            "action": "retired",
            "cleanup_proof": "signal_forbidden",
            "cleanup_baseexception": "cleanup_error",
        }[primary_stage]
    )
    assert events.count("cleanup") == 1
    assert "semantic" not in events
    assert ownership.body_owns_assertion
    assert not ownership.evidence_emitted


@pytest.mark.parametrize("failure_stage", ("build", "stringify", "emit"))
def test_evidence_failure_keeps_cleanup_at_most_once(
    failure_stage: str,
) -> None:
    events: list[str] = []
    attempts: list[_ProbeAttempt] = []
    ownership = _CleanupAssertionOwnership()
    clean = _CleanupOutcome(True, True, False, False, "retired")

    def cleanup() -> _CleanupOutcome:
        events.append("cleanup")
        return clean

    def action(attempt: _ProbeAttempt) -> None:
        events.append("action")
        attempts.append(attempt)
        attempt.returncode = 0
        attempt.stdout = _valid_probe_stdout()

    def evidence(
        _attempt: _ProbeAttempt,
        _outcome: _CleanupOutcome,
    ) -> dict[str, object]:
        events.append("evidence_build")
        if failure_stage == "build":
            raise _SyntheticEvidenceError("evidence build failed")
        if failure_stage == "stringify":
            return {"identity": object()}
        return {"identity": "synthetic"}

    def emit(value: dict[str, object]) -> None:
        events.append("evidence_emit")
        if failure_stage == "stringify":
            _emit_parent_evidence(value, _print=lambda *_args, **_kwargs: None)
            raise AssertionError("unserializable evidence unexpectedly emitted")
        raise _SyntheticEvidenceError("evidence emit failed")

    expected_error = TypeError if failure_stage == "stringify" else _SyntheticEvidenceError
    expected_message = "serializable" if failure_stage == "stringify" else failure_stage
    with pytest.raises(expected_error, match=expected_message) as raised:
        _run_probe_lifecycle(
            action=action,
            cleanup=cleanup,
            evidence=evidence,
            semantic=lambda _attempt: events.append("semantic"),
            ownership=ownership,
            _emit=emit,
        )
    events.append("body_error")

    assert _finalize_probe_cleanup(cleanup, ownership) is clean
    assert attempts[0].evidence_error is raised.value
    assert attempts[0].evidence_traceback
    assert raised.value.__cause__ is None
    assert ownership.body_owns_assertion
    assert not ownership.evidence_emitted
    assert events.count("cleanup") == 1
    assert events == (
        ["action", "cleanup", "evidence_build", "body_error"]
        if failure_stage == "build"
        else [
            "action",
            "cleanup",
            "evidence_build",
            "evidence_emit",
            "body_error",
        ]
    )


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the real cleanup proof uses Darwin PROC_PIDTBSDINFO",
)
@pytest.mark.slow
def test_real_freecad_embedded_interpreter_bootstraps_and_retires_one_daemon(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    prefix_value = os.environ.get("VIBECAD_FREECAD_ENV")
    if not prefix_value:
        pytest.fail("set VIBECAD_FREECAD_ENV to an identity-verified managed prefix")

    prefix, freecadcmd, runtime_evidence = _exact_managed_runtime(prefix_value)
    repo = Path(__file__).resolve().parent.parent
    source_root = (repo / "src").resolve()
    canonical_temp_parent = _canonical_darwin_temp_parent()
    isolated = tempfile.TemporaryDirectory(
        prefix=_DARWIN_TEMP_PREFIX,
        dir=str(canonical_temp_parent),
    )
    request.addfinalizer(isolated.cleanup)
    isolated_root = _admit_c00b_isolated_root(Path(isolated.name))
    isolated_home = isolated_root / "vibecad-home"
    freecad_user_home = isolated_root / "freecad-user"
    isolated_tmp = isolated_root / "tmp"
    for private_root in (isolated_home, freecad_user_home, isolated_tmp):
        private_root.mkdir(mode=0o700)
        private_root.chmod(0o700)
        _admit_canonical_private_root(private_root)

    monkeypatch.setenv("VIBECAD_HOME", str(isolated_home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(prefix))
    expected_run_root = daemon_run_root(paths.data_root())
    expected_socket = _runtime_daemon_endpoint()
    cleanup_guard = _DaemonCleanupGuard(expected_run_root)
    cleanup_guard.require_cold()
    ownership = _CleanupAssertionOwnership()

    def require_cleanup() -> _CleanupOutcome:
        return _finalize_probe_cleanup(cleanup_guard.cleanup, ownership)

    request.addfinalizer(require_cleanup)

    with status.runtime_maintenance_lock(timeout=5):
        launch_evidence = status.capture_runtime_generation_evidence(prefix)
        status.write_external_runtime_receipt(prefix, evidence=launch_evidence)
        runtime_checks = {
            "active_prefix": paths.active_runtime_prefix() == prefix,
            "bound_prefix": paths.bound_external_prefix() == prefix,
            "generation": launch_evidence == runtime_evidence,
            "ready": status.runtime_ready(),
            "receipt_parent": (
                paths.external_runtime_receipt().parent == isolated_home / "runtime"
            ),
        }
    environment = os.environ.copy()
    environment.update(
        {
            "FREECAD_USER_HOME": str(freecad_user_home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "QT_QPA_PLATFORM": "offscreen",
            "TMPDIR": str(isolated_tmp),
        }
    )
    command = _freecad_probe_command(freecadcmd, repo)

    process_command = ""

    def invoke_probe(attempt: _ProbeAttempt) -> None:
        nonlocal process_command

        if not all(runtime_checks.values()):
            raise RuntimeError("managed runtime identity changed before launch")
        completed = _invoke_after_endpoint_preflight(
            expected_socket,
            lambda: subprocess.run(
                command,
                capture_output=True,
                env=environment,
                text=True,
                timeout=_PROBE_TIMEOUT_SECONDS,
            ),
            ownership=ownership,
        )
        if type(completed) is not subprocess.CompletedProcess:
            raise TypeError("FreeCAD invocation did not return CompletedProcess")
        attempt.returncode = completed.returncode
        attempt.stdout = completed.stdout
        attempt.stderr = completed.stderr
        published = cleanup_guard.observe_publication()
        if published is not None:
            receipt = published.receipt
            try:
                process = subprocess.run(
                    ["ps", "-p", str(receipt.pid), "-o", "command="],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except subprocess.TimeoutExpired as error:
                raise _AuthenticatedInspectionTimeout(
                    error.cmd,
                    error.timeout,
                    output=error.stdout,
                    stderr=error.stderr,
                ) from error
            if process.returncode != 0:
                raise _AuthenticatedInspectionError(
                    f"authenticated process inspection returned {process.returncode}"
                )
            process_command = process.stdout.strip()

    def parent_identity(
        _attempt: _ProbeAttempt,
        _cleanup: _CleanupOutcome,
    ) -> dict[str, object]:
        process_token = cleanup_guard.process_token
        return {
            "daemon_id": cleanup_guard.daemon_id,
            "daemon_pid": cleanup_guard.daemon_pid,
            "daemon_process": _bounded_tail(process_command),
            "daemon_token": (
                None
                if process_token is None
                else {
                    "birth_sec": process_token.birth_sec,
                    "birth_usec": process_token.birth_usec,
                    "euid": process_token.euid,
                    "pgid": process_token.pgid,
                    "sid": process_token.sid,
                }
            ),
            "endpoint_bytes": len(os.fsencode(expected_socket)),
            "managed_python": str(launch_evidence.python),
            "prefix_identity": list(runtime_evidence.prefix_identity),
            "python_entry_identity": list(runtime_evidence.python_entry_identity),
            "runtime_checks": runtime_checks,
            "runtime_prefix": str(launch_evidence.prefix),
        }

    def validate_semantics(attempt: _ProbeAttempt) -> None:
        from vibecad.daemon.state import DaemonError, read_boot_state

        result = attempt.result
        cleanup_outcome = ownership.cleanup_once(cleanup_guard.cleanup)
        daemon_id = cleanup_guard.daemon_id
        daemon_pid = cleanup_guard.daemon_pid
        run_root_value = result.get("run_root")
        socket_value = result.get("socket")
        assert all(runtime_checks.values()), runtime_checks
        assert len(os.fsencode(expected_socket)) == 66
        assert cleanup_outcome.clean, cleanup_outcome
        assert cleanup_outcome.retire_attempted
        assert type(daemon_id) is str
        assert type(daemon_pid) is int
        assert cleanup_guard.original_token_absent
        assert not os.path.lexists(expected_socket)
        assert not os.path.lexists(expected_run_root / DAEMON_RECEIPT_NAME)
        assert _safe_absent_or_empty_run_root(expected_run_root)
        with pytest.raises(DaemonError):
            read_boot_state(expected_run_root)
        if run_root_value is not None:
            assert run_root_value == str(expected_run_root)
        if socket_value is not None:
            assert socket_value == str(expected_socket)
        if result.get("daemon_id") is not None:
            assert result["daemon_id"] == daemon_id
        if result.get("daemon_pid") is not None:
            assert result["daemon_pid"] == daemon_pid
        assert result["probe_source"] == str(
            (repo / "tests" / "fixtures" / "freecad_workbench" / "bootstrap_probe.py").resolve()
        )
        assert result["repo_source"] == str(source_root)
        assert result["vibecad_preloaded"] == []
        assert result["repo_source_occurrences_after"] == 1
        assert result["sys_path_zero"] == str(source_root)
        assert result["bootstrap_source"] == str(
            source_root / "vibecad" / "daemon" / "bootstrap.py"
        )
        assert result["vibecad_source"] == str(source_root / "vibecad" / "__init__.py")
        assert result["client_closed"] is True
        assert result["cold_run_root_absent"] is True
        assert result["status"] == "ok"
        assert result["sys_prefix"] == str(prefix)
        assert os.path.samefile(os.fspath(result["sys_executable"]), freecadcmd)
        assert process_command == f"{launch_evidence.python} -B -m vibecad.daemon"

    _run_probe_lifecycle(
        action=invoke_probe,
        cleanup=cleanup_guard.cleanup,
        evidence=parent_identity,
        semantic=validate_semantics,
        ownership=ownership,
    )
