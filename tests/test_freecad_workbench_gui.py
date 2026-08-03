from __future__ import annotations

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Protocol

import pytest

from tests.test_freecad_workbench_bootstrap import (
    _CleanupAssertionOwnership,
    _CleanupOutcome,
    _DaemonCleanupGuard,
    _darwin_process_token,
    _DarwinProcessToken,
    _finalize_probe_cleanup,
    _safe_absent_or_empty_run_root,
)
from vibecad.daemon.state import (
    DAEMON_ENDPOINT_NAME,
    DAEMON_RECEIPT_NAME,
    daemon_run_root,
)
from vibecad.runtime import paths, spec, status

_RESULT_PREFIX = "VIBECAD_GUI_HARNESS="
_PARENT_EVIDENCE_PREFIX = "VIBECAD_GUI_PARENT="
_EVIDENCE_TAIL_CHARACTERS = 2_000
_CAMPAIGN_TIMEOUT_SECONDS = 60.0
_CLEANUP_RESERVE_SECONDS = 12.0
_DARWIN_TEMP_PARENT = Path("/private/tmp")
_DARWIN_TEMP_PREFIX = "vc-g1m00-"


def _load_gui_harness_module() -> ModuleType:
    source = Path(__file__).resolve().parent / "fixtures" / "freecad_workbench" / "gui_harness.py"
    module_spec = importlib.util.spec_from_file_location("_vibecad_gui_harness_test", source)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    path: Path
    target: Path
    entry: tuple[int, int, int, int, int, int, int, int]
    resolved: tuple[int, int, int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _PrivateRoots:
    root: Path
    vibecad: Path
    freecad_home: Path
    freecad_data: Path
    freecad_temp: Path
    tmp: Path


@dataclass(frozen=True, slots=True)
class _ParentAttempt:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    action_error: BaseException | None


class _Process(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class _GuiReclaimError(RuntimeError):
    def __init__(self, message: str, *, term_sent: bool, kill_sent: bool) -> None:
        super().__init__(message)
        self.term_sent = term_sent
        self.kill_sent = kill_sent


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _capture_gui_identity(binary: Path, prefix: Path) -> _FileIdentity:
    if binary != paths.freecad_path() or not binary.is_absolute():
        raise ValueError("GUI binary is not the selected runtime path")
    prefix = prefix.resolve(strict=True)
    try:
        entry_info = binary.lstat()
        target = binary.resolve(strict=True)
        target.relative_to(prefix)
        target_info = target.stat()
    except OSError as exc:
        raise ValueError("GUI binary identity is unavailable") from exc
    getuid = getattr(os, "geteuid", None)
    if (
        not stat.S_ISREG(entry_info.st_mode)
        or (getuid is not None and entry_info.st_uid != getuid())
        or entry_info.st_size <= 0
        or stat.S_IMODE(entry_info.st_mode) & 0o022
        or not stat.S_ISREG(target_info.st_mode)
        or target_info.st_size <= 0
        or stat.S_IMODE(target_info.st_mode) & 0o022
        or (getuid is not None and target_info.st_uid != getuid())
        or not os.access(target, os.X_OK)
    ):
        raise ValueError("GUI target is not one owner-controlled executable")
    return _FileIdentity(
        path=binary,
        target=target,
        entry=_stat_identity(entry_info),
        resolved=_stat_identity(target_info),
    )


def _revalidate_gui_identity(identity: _FileIdentity, prefix: Path) -> None:
    current = _capture_gui_identity(identity.path, prefix)
    if current != identity:
        raise RuntimeError("GUI binary generation changed")


def _freecad_module_root(repo: Path) -> Path:
    getuid = getattr(os, "geteuid", None)

    def admit_directory(path: Path) -> None:
        try:
            canonical = path.resolve(strict=True)
            info = path.lstat()
        except OSError as exc:
            raise ValueError("FreeCAD module root execution chain is unavailable") from exc
        if (
            path != canonical
            or not stat.S_ISDIR(info.st_mode)
            or not info.st_mode & stat.S_IRUSR
            or not info.st_mode & stat.S_IXUSR
            or stat.S_IMODE(info.st_mode) & 0o022
            or (getuid is not None and info.st_uid != getuid())
        ):
            raise ValueError(
                "FreeCAD module root execution chain is not canonical and owner-controlled"
            )

    try:
        canonical_repo = repo.resolve(strict=True)
    except OSError as exc:
        raise ValueError("FreeCAD module root repository is unavailable") from exc
    if repo != canonical_repo:
        raise ValueError("FreeCAD module root repository spelling is not canonical")
    freecad_root = repo / "freecad"
    module_root = repo / "freecad" / "VibeCAD"
    for directory in (repo, freecad_root, module_root):
        admit_directory(directory)
    for name in ("Init.py", "InitGui.py", "package.xml"):
        source = module_root / name
        try:
            source_info = source.lstat()
            canonical_source = source.resolve(strict=True)
        except OSError as exc:
            raise ValueError("FreeCAD module root is missing a required source") from exc
        if (
            source != canonical_source
            or not stat.S_ISREG(source_info.st_mode)
            or not source_info.st_mode & stat.S_IRUSR
            or stat.S_IMODE(source_info.st_mode) & 0o022
            or (getuid is not None and source_info.st_uid != getuid())
        ):
            raise ValueError("FreeCAD module root has an invalid required source")
    return module_root


def _gui_command(binary: Path, repo: Path) -> list[str]:
    return [
        str(binary),
        "-M",
        str(_freecad_module_root(repo)),
        "-P",
        str((repo / "src").resolve(strict=True)),
        "-P",
        str((repo / "tests" / "fixtures" / "freecad_workbench").resolve(strict=True)),
        "--run-test",
        "gui_harness",
    ]


def _private_roots(root: Path) -> _PrivateRoots:
    if root != root.resolve(strict=True):
        raise ValueError("isolated root spelling is not canonical")
    info = root.lstat()
    getuid = getattr(os, "geteuid", None)
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o700
        or (getuid is not None and info.st_uid != getuid())
    ):
        raise ValueError("isolated root is not owner-private")
    roots = _PrivateRoots(
        root=root,
        vibecad=root / "vibecad",
        freecad_home=root / "freecad-home",
        freecad_data=root / "freecad-data",
        freecad_temp=root / "freecad-temp",
        tmp=root / "tmp",
    )
    for child in (
        roots.vibecad,
        roots.freecad_home,
        roots.freecad_data,
        roots.freecad_temp,
        roots.tmp,
    ):
        child.mkdir(mode=0o700)
        child.chmod(0o700)
        child_info = child.lstat()
        if (
            child != child.resolve(strict=True)
            or not stat.S_ISDIR(child_info.st_mode)
            or stat.S_IMODE(child_info.st_mode) != 0o700
            or (getuid is not None and child_info.st_uid != getuid())
        ):
            raise ValueError("isolated child is not owner-private")
    return roots


def _gui_environment(roots: _PrivateRoots, prefix: Path) -> dict[str, str]:
    return {
        "FREECAD_USER_DATA": str(roots.freecad_data),
        "FREECAD_USER_HOME": str(roots.freecad_home),
        "FREECAD_USER_TEMP": str(roots.freecad_temp),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(roots.tmp),
        "VIBECAD_FREECAD_ENV": str(prefix),
        "VIBECAD_HOME": str(roots.vibecad),
    }


def _authenticate_runtime_generation(
    prefix: Path,
    expected: status.RuntimeGenerationEvidence,
    *,
    read_receipt: Callable[[Path], dict | None] | None = None,
    capture: Callable[[Path], status.RuntimeGenerationEvidence] | None = None,
    verify: Callable[[status.RuntimeGenerationEvidence], bool] | None = None,
) -> status.RuntimeGenerationEvidence:
    receipt_reader = status.read_prefix_receipt if read_receipt is None else read_receipt
    capture_generation = status.capture_runtime_generation_evidence if capture is None else capture
    verify_generation = status.verify_runtime_generation if verify is None else verify
    if type(expected) is not status.RuntimeGenerationEvidence or expected.prefix != prefix:
        raise RuntimeError("expected evidence does not bind the managed prefix")
    if receipt_reader(prefix) != spec.expected_receipt():
        raise RuntimeError("managed prefix receipt does not match the exact runtime spec")
    observed = capture_generation(prefix)
    if (
        type(observed) is not status.RuntimeGenerationEvidence
        or observed != expected
        or observed.prefix != prefix
    ):
        raise RuntimeError("managed runtime generation changed")
    if verify_generation(observed) is not True:
        raise RuntimeError("managed runtime verification failed")
    return observed


def _capture_authenticated_managed_generation(
    prefix: Path,
    *,
    environment: MutableMapping[str, str] | None = None,
    managed_prefix: Callable[[], Path] | None = None,
    capture: Callable[[Path], status.RuntimeGenerationEvidence] | None = None,
    authenticate: (
        Callable[
            [Path, status.RuntimeGenerationEvidence],
            status.RuntimeGenerationEvidence,
        ]
        | None
    ) = None,
) -> status.RuntimeGenerationEvidence:
    resolved = prefix.resolve(strict=True)
    selected_managed_prefix = (
        paths.env_prefix() if managed_prefix is None else managed_prefix()
    ).resolve(strict=True)
    if prefix != resolved or resolved != selected_managed_prefix:
        raise ValueError("runtime prefix is not the canonical managed prefix")

    target_environment = os.environ if environment is None else environment
    selection_key = "VIBECAD_FREECAD_ENV"
    if selection_key not in target_environment:
        raise ValueError("runtime selection is not the exact canonical managed prefix selection")
    original_selection = target_environment[selection_key]
    if type(original_selection) is not str or original_selection != str(resolved):
        raise ValueError("runtime selection is not the exact canonical managed prefix selection")

    capture_generation = status.capture_runtime_generation_evidence if capture is None else capture
    authenticate_generation = (
        _authenticate_runtime_generation if authenticate is None else authenticate
    )
    del target_environment[selection_key]
    try:
        expected = capture_generation(prefix)
        if type(expected) is not status.RuntimeGenerationEvidence:
            raise RuntimeError("capture did not return exact RuntimeGenerationEvidence")
        if expected.prefix != prefix:
            raise RuntimeError("captured evidence does not bind the managed prefix")
        authenticated = authenticate_generation(prefix, expected)
        if type(authenticated) is not status.RuntimeGenerationEvidence:
            raise RuntimeError("authentication did not return exact RuntimeGenerationEvidence")
        if authenticated.prefix != prefix:
            raise RuntimeError("authenticated evidence does not bind the managed prefix")
        if authenticated != expected:
            raise RuntimeError("authenticated managed runtime generation changed")
        return authenticated
    finally:
        target_environment[selection_key] = original_selection


def _parse_gui_result(stdout: str) -> dict[str, object]:
    payloads = [
        line.removeprefix(_RESULT_PREFIX)
        for line in stdout.splitlines()
        if line.startswith(_RESULT_PREFIX)
    ]
    if len(payloads) != 1:
        raise ValueError(f"expected one GUI harness result, observed {len(payloads)}")
    try:
        value = json.loads(payloads[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("GUI harness result is invalid JSON") from exc
    if type(value) is not dict:
        raise ValueError("GUI harness result is not an object")
    canonical = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if payloads[0] != canonical:
        raise ValueError("GUI harness result is not canonical JSON")
    return value


def _remaining(deadline: float, *, _clock: Callable[[], float] = time.monotonic) -> float:
    remaining = deadline - _clock()
    if remaining <= 0:
        raise TimeoutError("campaign-wide GUI deadline expired")
    return remaining


def _remaining_before_cleanup(
    deadline: float,
    *,
    _clock: Callable[[], float] = time.monotonic,
) -> float:
    remaining = _remaining(deadline, _clock=_clock) - _CLEANUP_RESERVE_SECONDS
    if remaining <= 0:
        raise TimeoutError("GUI action exhausted the cleanup reserve")
    return remaining


def _same_process_generation(
    expected: _DarwinProcessToken,
    *,
    capture: Callable[[int], _DarwinProcessToken] = _darwin_process_token,
) -> bool:
    try:
        return capture(expected.pid) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _reclaim_gui_process(
    process: _Process,
    token: _DarwinProcessToken,
    deadline: float,
    *,
    capture: Callable[[int], _DarwinProcessToken] = _darwin_process_token,
    killpg: Callable[[int, int], None] = os.killpg,
) -> tuple[bool, bool]:
    if process.pid != token.pid:
        raise _GuiReclaimError(
            "GUI process token does not match child",
            term_sent=False,
            kill_sent=False,
        )
    if process.poll() is not None:
        return False, False
    if not _same_process_generation(token, capture=capture):
        raise _GuiReclaimError(
            "GUI process identity is ambiguous; signaling forbidden",
            term_sent=False,
            kill_sent=False,
        )
    try:
        killpg(token.pgid, signal.SIGTERM)
    except OSError as exc:
        raise _GuiReclaimError(
            "GUI SIGTERM failed",
            term_sent=False,
            kill_sent=False,
        ) from exc
    try:
        process.wait(timeout=min(2.0, _remaining(deadline)))
        return True, False
    except subprocess.TimeoutExpired:
        pass
    except TimeoutError as exc:
        raise _GuiReclaimError(
            "GUI deadline expired after SIGTERM",
            term_sent=True,
            kill_sent=False,
        ) from exc
    if not _same_process_generation(token, capture=capture):
        raise _GuiReclaimError(
            "GUI process generation changed; SIGKILL forbidden",
            term_sent=True,
            kill_sent=False,
        )
    try:
        killpg(token.pgid, signal.SIGKILL)
    except OSError as exc:
        raise _GuiReclaimError(
            "GUI SIGKILL failed",
            term_sent=True,
            kill_sent=False,
        ) from exc
    try:
        process.wait(timeout=_remaining(deadline))
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        raise _GuiReclaimError(
            "GUI remained after SIGKILL",
            term_sent=True,
            kill_sent=True,
        ) from exc
    return True, True


def _recover_gui_child(
    process: _Process | None,
    token: _DarwinProcessToken | None,
    *,
    token_capture_failed: bool,
    deadline: float,
    capture: Callable[[int], _DarwinProcessToken] = _darwin_process_token,
    killpg: Callable[[int, int], None] = os.killpg,
) -> _CleanupOutcome:
    if process is None:
        return _CleanupOutcome(True, False, False, False, "not_launched")
    if process.poll() is not None:
        detail = "token_unavailable_natural_exit" if token_capture_failed else "exited"
        return _CleanupOutcome(True, False, False, False, detail)
    if token is None:
        try:
            process.wait(timeout=_remaining_before_cleanup(deadline))
        except (subprocess.TimeoutExpired, TimeoutError):
            return _CleanupOutcome(
                False,
                False,
                False,
                False,
                "token_unavailable_lingering",
            )
        return _CleanupOutcome(
            True,
            False,
            False,
            False,
            "token_unavailable_natural_exit",
        )
    try:
        term_sent, kill_sent = _reclaim_gui_process(
            process,
            token,
            deadline,
            capture=capture,
            killpg=killpg,
        )
    except _GuiReclaimError as exc:
        return _CleanupOutcome(
            False,
            False,
            exc.term_sent,
            exc.kill_sent,
            "authenticated_reclaim_failed",
        )
    if process.poll() is None:
        return _CleanupOutcome(
            False,
            False,
            term_sent,
            kill_sent,
            "authenticated_child_lingering",
        )
    return _CleanupOutcome(
        True,
        False,
        term_sent,
        kill_sent,
        "authenticated_reclaimed",
    )


def _cleanup_before_semantics(
    action: Callable[[], _ParentAttempt],
    cleanup: Callable[[], _CleanupOutcome],
    semantics: Callable[[_ParentAttempt, _CleanupOutcome], object],
    *,
    ownership: _CleanupAssertionOwnership | None = None,
    _emit: Callable[[dict[str, object]], object] | None = None,
) -> object:
    ownership = _CleanupAssertionOwnership() if ownership is None else ownership
    emit = _emit_parent_evidence if _emit is None else _emit
    attempt: _ParentAttempt
    try:
        attempt = action()
    except BaseException as exc:
        attempt = _ParentAttempt(None, "", "", False, exc)
    cleanup_outcome = ownership.cleanup_once(cleanup)
    parse_control_error: BaseException | None = None
    try:
        result = _parse_gui_result(attempt.stdout)
    except BaseException as exc:
        result = {}
        parse_error = _bounded_tail(f"{type(exc).__name__}: {exc}")
        if not isinstance(exc, Exception):
            parse_control_error = exc
    else:
        parse_error = None
    evidence_error: BaseException | None = None
    try:
        emit(_build_gui_parent_evidence(attempt, cleanup_outcome, result, parse_error))
    except BaseException as exc:
        evidence_error = exc
        ownership.transfer_to_body(evidence_emitted=False)
    else:
        ownership.transfer_to_body(evidence_emitted=True)

    failures: list[BaseException] = []
    if ownership.cleanup_error is not None:
        failures.append(ownership.cleanup_error)
    elif not cleanup_outcome.clean:
        failures.append(AssertionError(f"GUI cleanup failed: {cleanup_outcome.detail}"))
    if attempt.action_error is not None:
        failures.append(attempt.action_error)
    if parse_control_error is not None:
        failures.append(parse_control_error)
    if evidence_error is not None:
        failures.append(evidence_error)
    if len(failures) > 1:
        raise BaseExceptionGroup(
            "GUI action, cleanup, parse, or evidence failed",
            failures,
        )
    if failures:
        raise failures[0]
    return semantics(attempt, cleanup_outcome)


def _finalize_captured_attempt(
    attempt: _ParentAttempt,
    *,
    observe_publication: Callable[[], object],
    recheck: Callable[[], object],
) -> _ParentAttempt:
    try:
        observe_publication()
        recheck()
    except BaseException as exc:
        return replace(attempt, action_error=exc)
    return attempt


def _bounded_tail(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    if type(value) is bytes:
        value = value.decode(errors="replace")
    return value[-_EVIDENCE_TAIL_CHARACTERS:]


def _exception_summary(error: BaseException | None) -> str | None:
    if error is None:
        return None
    return _bounded_tail(f"{type(error).__name__}: {error}")


def _build_gui_parent_evidence(
    attempt: _ParentAttempt,
    cleanup: _CleanupOutcome,
    result: dict[str, object],
    parse_error: str | None,
) -> dict[str, object]:
    gui_status = result.get("status")
    gui_error = result.get("error")
    return {
        "action_error": _exception_summary(attempt.action_error),
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
        "gui_error_tail": _bounded_tail(gui_error) if type(gui_error) is str else None,
        "gui_parse_error": parse_error,
        "gui_status": _bounded_tail(gui_status) if type(gui_status) is str else None,
        "timed_out": attempt.timed_out,
    }


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
    _print(_PARENT_EVIDENCE_PREFIX + encoded, flush=True)


_SNAPSHOT_KEYS = frozenset(
    (
        "schema_version",
        "lifecycle",
        "dock_count",
        "main_thread_id",
        "worker_thread_id",
        "daemon_id",
        "heartbeat_count",
        "client_construction_count",
    )
)


def _exact_snapshot(
    result: dict[str, object],
    key: str,
    lifecycle: str,
    *,
    dock_count: int,
    client_construction_count: int,
) -> dict[str, object]:
    snapshot = result.get(key)
    if type(snapshot) is not dict or set(snapshot) != _SNAPSHOT_KEYS:
        raise AssertionError(f"{key} is not the exact technical snapshot")
    assert snapshot["schema_version"] == 1
    assert snapshot["lifecycle"] == lifecycle
    assert snapshot["dock_count"] == dock_count
    assert snapshot["client_construction_count"] == client_construction_count
    assert type(snapshot["heartbeat_count"]) is int
    return snapshot


def _validate_gui_semantics(
    attempt: _ParentAttempt,
    cleanup: _CleanupOutcome,
    *,
    expected_daemon_id: str,
    expected_gui_target: Path | None = None,
    expected_home: Path | None = None,
    expected_prefix: Path | None = None,
    expected_repo: Path | None = None,
) -> dict[str, object]:
    if attempt.timed_out:
        raise AssertionError("real GUI harness timed out")
    if attempt.returncode != 0:
        raise AssertionError(f"real GUI harness returned {attempt.returncode}")
    result = _parse_gui_result(attempt.stdout)
    initial = _exact_snapshot(
        result,
        "initial_snapshot",
        "inactive",
        dock_count=0,
        client_construction_count=0,
    )
    starting = _exact_snapshot(
        result,
        "starting_snapshot",
        "starting",
        dock_count=1,
        client_construction_count=0,
    )
    active = _exact_snapshot(
        result,
        "active_snapshot",
        "active",
        dock_count=1,
        client_construction_count=1,
    )
    refresh = _exact_snapshot(
        result,
        "refresh_snapshot",
        "active",
        dock_count=1,
        client_construction_count=1,
    )
    stopping = _exact_snapshot(
        result,
        "stopping_snapshot",
        "stopping",
        dock_count=1,
        client_construction_count=1,
    )
    final = _exact_snapshot(
        result,
        "final_snapshot",
        "inactive",
        dock_count=0,
        client_construction_count=1,
    )
    assert result.get("status") == "ok"
    assert result.get("error") is None
    assert result.get("modal_detected") is False
    assert result.get("addon_registered") is True
    assert result.get("workbench_activated") is True
    assert result.get("deactivation_via_workbench") is True
    assert result.get("workbench_count") == 1
    assert result.get("workbench_ids") in (["VibeCAD"], ["VibeCADWorkbench"])
    assert result.get("client_connected") is True
    assert result.get("active_dock_count") == 1
    assert result.get("dock_count_after_shutdown") == 0
    assert result.get("refresh_triggered") is True
    refresh_command_kinds = result.get("refresh_command_kinds")
    assert type(refresh_command_kinds) is list and refresh_command_kinds
    assert all(type(kind) is str for kind in refresh_command_kinds)
    assert set(refresh_command_kinds) <= {
        "list_projects",
        "refresh_project",
        "list_tasks",
        "refresh_task",
    }
    assert type(result.get("refresh_event_delta")) is int
    assert result["refresh_event_delta"] > 0
    assert type(result.get("refresh_heartbeat_delta")) is int
    assert result["refresh_heartbeat_delta"] > 0
    assert initial["worker_thread_id"] is None
    assert initial["daemon_id"] is None
    assert starting["worker_thread_id"] is None
    assert starting["daemon_id"] is None
    assert active.get("main_thread_id") == result.get("main_thread_id")
    assert type(active.get("worker_thread_id")) is int
    assert active["worker_thread_id"] != active["main_thread_id"]
    assert active.get("daemon_id") == expected_daemon_id
    assert type(active.get("heartbeat_count")) is int and active["heartbeat_count"] > 0
    assert refresh["main_thread_id"] == active["main_thread_id"]
    assert refresh["worker_thread_id"] == active["worker_thread_id"]
    assert refresh["daemon_id"] == expected_daemon_id
    assert refresh["heartbeat_count"] > active["heartbeat_count"]
    assert stopping["main_thread_id"] == active["main_thread_id"]
    assert stopping["worker_thread_id"] == active["worker_thread_id"]
    assert stopping["daemon_id"] == expected_daemon_id
    assert final.get("worker_thread_id") is None
    assert final.get("daemon_id") is None
    assert type(final.get("heartbeat_count")) is int
    assert final["heartbeat_count"] >= refresh["heartbeat_count"]
    assert type(result.get("harness_heartbeat_count")) is int
    assert result["harness_heartbeat_count"] > 0
    assert result.get("qt_binding") == "PySide"
    assert type(result.get("qt_binding_version")) is str
    assert result["qt_binding_version"]
    assert type(result.get("qt_version")) is str and result["qt_version"]
    if expected_gui_target is not None:
        assert Path(os.fspath(result.get("sys_executable"))).resolve(strict=True) == (
            expected_gui_target
        )
    if expected_home is not None:
        assert Path(os.fspath(result.get("vibecad_home"))).resolve(strict=True) == expected_home
    if expected_prefix is not None:
        assert Path(os.fspath(result.get("sys_prefix"))).resolve(strict=True) == expected_prefix
    if expected_repo is not None:
        expected_source = expected_repo / "src"
        assert result.get("bootstrap_source") == str(
            (expected_source / "vibecad" / "daemon" / "bootstrap.py").resolve(strict=True)
        )
        assert result.get("vibecad_source") == str(
            (expected_source / "vibecad" / "__init__.py").resolve(strict=True)
        )
        assert result.get("host_source") == str(
            (expected_repo / "freecad" / "VibeCAD" / "vibecad_workbench" / "host.py").resolve(
                strict=True
            )
        )
        assert result.get("init_gui_source") == str(
            (expected_repo / "freecad" / "VibeCAD" / "InitGui.py").resolve(strict=True)
        )
        assert result.get("gateway_source") == str(
            (expected_repo / "freecad" / "VibeCAD" / "vibecad_workbench" / "gateway.py").resolve(
                strict=True
            )
        )
        assert result.get("dock_source") == str(
            (expected_repo / "freecad" / "VibeCAD" / "vibecad_workbench" / "dock.py").resolve(
                strict=True
            )
        )
        assert result.get("harness_source") == str(
            (expected_repo / "tests" / "fixtures" / "freecad_workbench" / "gui_harness.py").resolve(
                strict=True
            )
        )
    assert cleanup.clean and cleanup.retire_attempted
    return result


def test_gui_command_and_environment_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = Path(__file__).resolve().parent.parent
    prefix = tmp_path / "prefix"
    binary = prefix / "bin" / "FreeCAD"
    monkeypatch.setattr(paths, "freecad_path", lambda: binary)
    root = tmp_path / "isolated"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    roots = _private_roots(root.resolve(strict=True))

    assert _gui_command(binary, repo) == [
        str(binary),
        "-M",
        str((repo / "freecad" / "VibeCAD").resolve()),
        "-P",
        str((repo / "src").resolve()),
        "-P",
        str((repo / "tests" / "fixtures" / "freecad_workbench").resolve()),
        "--run-test",
        "gui_harness",
    ]
    assert _gui_environment(roots, prefix) == {
        "FREECAD_USER_DATA": str(root / "freecad-data"),
        "FREECAD_USER_HOME": str(root / "freecad-home"),
        "FREECAD_USER_TEMP": str(root / "freecad-temp"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(root / "tmp"),
        "VIBECAD_FREECAD_ENV": str(prefix),
        "VIBECAD_HOME": str(root / "vibecad"),
    }


def test_freecad_module_root_is_the_canonical_direct_addon_directory(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    addon = repo / "freecad" / "VibeCAD"
    addon.mkdir(parents=True)
    for name in ("Init.py", "InitGui.py", "package.xml"):
        (addon / name).write_text(name, encoding="utf-8")

    observed = _freecad_module_root(repo)

    assert observed == addon.resolve(strict=True)
    assert observed != (repo / "freecad").resolve(strict=True)


@pytest.mark.parametrize("missing", ["Init.py", "InitGui.py", "package.xml"])
def test_freecad_module_root_rejects_missing_manifest_or_init(
    tmp_path: Path,
    missing: str,
) -> None:
    repo = (tmp_path / "repo").resolve()
    addon = repo / "freecad" / "VibeCAD"
    addon.mkdir(parents=True)
    for name in ("Init.py", "InitGui.py", "package.xml"):
        if name != missing:
            (addon / name).write_text(name, encoding="utf-8")

    with pytest.raises(ValueError, match="module root"):
        _freecad_module_root(repo)


def test_freecad_module_root_rejects_alias_and_wrong_parent_level(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    addon = repo / "freecad" / "VibeCAD"
    addon.mkdir(parents=True)
    for name in ("Init.py", "InitGui.py", "package.xml"):
        (addon / name).write_text(name, encoding="utf-8")
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)

    with pytest.raises(ValueError, match="canonical"):
        _freecad_module_root(alias)
    with pytest.raises(ValueError, match="module root"):
        _freecad_module_root(repo / "freecad")


@pytest.mark.parametrize(
    "target",
    [
        "repo",
        "freecad",
        "addon",
        "Init.py",
        "InitGui.py",
        "package.xml",
    ],
)
def test_freecad_module_root_rejects_group_or_world_writable_execution_chain(
    tmp_path: Path,
    target: str,
) -> None:
    repo = (tmp_path / "repo").resolve()
    freecad_root = repo / "freecad"
    addon = freecad_root / "VibeCAD"
    addon.mkdir(parents=True)
    sources = {name: addon / name for name in ("Init.py", "InitGui.py", "package.xml")}
    for name, source in sources.items():
        source.write_text(name, encoding="utf-8")
    selected = {
        "repo": repo,
        "freecad": freecad_root,
        "addon": addon,
        **sources,
    }[target]
    selected.chmod(0o777 if selected.is_dir() else 0o666)

    with pytest.raises(ValueError, match="module root"):
        _freecad_module_root(repo)


def test_freecad_module_root_directories_require_owner_read_and_search(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    addon = repo / "freecad" / "VibeCAD"
    addon.mkdir(parents=True)
    for name in ("Init.py", "InitGui.py", "package.xml"):
        (addon / name).write_text(name, encoding="utf-8")
    addon.chmod(0o400)

    try:
        with pytest.raises(ValueError, match="module root"):
            _freecad_module_root(repo)
    finally:
        addon.chmod(0o700)


def test_registration_failure_diagnostic_is_canonical_and_bounded(tmp_path: Path) -> None:
    module = _load_gui_harness_module()
    addon_root = tmp_path / "freecad" / "VibeCAD"
    addon_root.mkdir(parents=True)
    workbenches = {f"Workbench-{index:02d}-" + "x" * 100: object() for index in range(40)}

    diagnostic = module._registration_failure_diagnostic(
        workbenches,
        addon_root,
        config_get=lambda key: (
            "/one/module/path:/two/module/path" if key == "AdditionalModulePaths" else ""
        ),
    )

    prefix = "expected one registered VibeCAD Workbench: "
    assert diagnostic.startswith(prefix)
    encoded = diagnostic.removeprefix(prefix)
    payload = json.loads(encoded)
    assert encoded == json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert len(diagnostic) <= 2_000
    assert payload["registered_vibecad_count"] == 0
    assert payload["total_workbench_count"] == 40
    assert payload["workbench_names_truncated"] is True
    assert len(payload["workbench_names"]) <= 8
    assert payload["expected_addon_root_exists"] is True
    assert payload["additional_module_paths"] == "/one/module/path:/two/module/path"
    assert payload["diagnostic_read_errors"] == []


def test_registration_failure_diagnostic_reads_only_a_fixed_iterable_prefix(
    tmp_path: Path,
) -> None:
    module = _load_gui_harness_module()

    class CountingWorkbenches:
        def __init__(self) -> None:
            self.count = 0

        def __iter__(self) -> object:
            while self.count < 10_000:
                self.count += 1
                yield f"Workbench-{self.count}"

    workbenches = CountingWorkbenches()
    diagnostic = module._registration_failure_diagnostic(
        workbenches,
        tmp_path / "missing-addon",
        config_get=lambda _key: "",
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert workbenches.count <= 65
    assert payload["total_workbench_count"] is None
    assert payload["workbench_names_truncated"] is True
    assert payload["workbench_observed_count"] <= 65
    assert len(payload["workbench_names"]) <= 8


def test_registration_failure_diagnostic_bounds_hostile_error_and_unicode_fields(
    tmp_path: Path,
) -> None:
    module = _load_gui_harness_module()
    hostile_error = type("X" * 5_000, (RuntimeError,), {})

    class HostileWorkbenches:
        def __iter__(self) -> object:
            raise hostile_error("y" * 5_000)

    diagnostic = module._registration_failure_diagnostic(
        HostileWorkbenches(),
        tmp_path / "missing-addon",
        config_get=lambda _key: "路径" * 5_000,
    )
    encoded = diagnostic.removeprefix("expected one registered VibeCAD Workbench: ")
    payload = json.loads(encoded)

    assert len(diagnostic) <= 2_000
    assert encoded == json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert payload["diagnostic_read_errors"][0]["phase"] == "workbenches"
    assert len(payload["diagnostic_read_errors"][0]["type"]) <= 64
    assert all(ord(character) < 128 for character in payload["diagnostic_read_errors"][0]["type"])
    assert all(len(name) <= 64 for name in payload["workbench_names"])
    assert len(payload["additional_module_paths"]) <= 512
    assert all(ord(character) < 128 for character in payload["additional_module_paths"])


def test_registration_failure_diagnostic_read_error_does_not_mask_primary(
    tmp_path: Path,
) -> None:
    module = _load_gui_harness_module()

    def fail_config(_key: str) -> str:
        raise RuntimeError("diagnostic lookup red")

    diagnostic = module._registration_failure_diagnostic(
        {"NoneWorkbench": object()},
        tmp_path / "missing-addon",
        config_get=fail_config,
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert payload["registered_vibecad_count"] == 0
    assert payload["expected_addon_root_exists"] is False
    assert payload["additional_module_paths"] is None
    assert payload["diagnostic_read_errors"] == [
        {"phase": "additional_module_paths", "type": "RuntimeError"}
    ]


def test_registration_failure_diagnostic_addon_stat_error_does_not_mask_primary() -> None:
    module = _load_gui_harness_module()

    class BrokenAddonRoot:
        def is_dir(self) -> bool:
            raise OSError("stat red")

    diagnostic = module._registration_failure_diagnostic(
        {"NoneWorkbench": object()},
        BrokenAddonRoot(),
        config_get=lambda _key: "",
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert payload["expected_addon_root_exists"] is None
    assert payload["diagnostic_read_errors"] == [
        {"phase": "expected_addon_root", "type": "OSError"}
    ]


@pytest.mark.parametrize("exists", [True, False])
def test_registration_failure_diagnostic_accepts_only_exact_bool_addon_result(
    exists: bool,
) -> None:
    module = _load_gui_harness_module()

    class AddonRoot:
        def is_dir(self) -> bool:
            return exists

    diagnostic = module._registration_failure_diagnostic(
        {},
        AddonRoot(),
        config_get=lambda _key: "",
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert payload["expected_addon_root_exists"] is exists
    assert payload["diagnostic_read_errors"] == []


@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(float("nan"), id="nan"),
        pytest.param(object(), id="object"),
        pytest.param("yes", id="str"),
        pytest.param(1, id="int"),
        pytest.param(None, id="none"),
    ],
)
def test_registration_failure_diagnostic_rejects_non_bool_addon_result(
    invalid: object,
) -> None:
    module = _load_gui_harness_module()

    class AddonRoot:
        def is_dir(self) -> object:
            return invalid

    diagnostic = module._registration_failure_diagnostic(
        {},
        AddonRoot(),
        config_get=lambda _key: "",
    )
    encoded = diagnostic.removeprefix("expected one registered VibeCAD Workbench: ")
    payload = json.loads(encoded)

    assert payload["expected_addon_root_exists"] is None
    assert payload["diagnostic_read_errors"] == [
        {"phase": "expected_addon_root", "type": "TypeError"}
    ]
    assert encoded == json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert len(diagnostic) <= 2_000


def test_registration_failure_diagnostic_records_all_three_read_errors_in_order() -> None:
    module = _load_gui_harness_module()

    class BrokenWorkbenches:
        def __iter__(self) -> object:
            raise OSError("workbench red")

    class BrokenAddonRoot:
        def is_dir(self) -> bool:
            raise ValueError("addon red")

    def fail_config(_key: str) -> str:
        raise RuntimeError("config red")

    diagnostic = module._registration_failure_diagnostic(
        BrokenWorkbenches(),
        BrokenAddonRoot(),
        config_get=fail_config,
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert payload["diagnostic_read_errors"] == [
        {"phase": "workbenches", "type": "OSError"},
        {"phase": "additional_module_paths", "type": "RuntimeError"},
        {"phase": "expected_addon_root", "type": "ValueError"},
    ]


def test_registration_failure_diagnostic_records_two_errors_without_messages() -> None:
    module = _load_gui_harness_module()

    class BrokenAddonRoot:
        def is_dir(self) -> bool:
            raise OSError("must not appear")

    def fail_config(_key: str) -> str:
        raise RuntimeError("must not appear")

    diagnostic = module._registration_failure_diagnostic(
        {},
        BrokenAddonRoot(),
        config_get=fail_config,
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert payload["diagnostic_read_errors"] == [
        {"phase": "additional_module_paths", "type": "RuntimeError"},
        {"phase": "expected_addon_root", "type": "OSError"},
    ]
    assert "must not appear" not in diagnostic


def test_registration_failure_diagnostic_bounds_repeated_hostile_error_types() -> None:
    module = _load_gui_harness_module()
    hostile_error = type("恶" * 5_000, (RuntimeError,), {})

    class BrokenWorkbenches:
        def __iter__(self) -> object:
            raise hostile_error

    class BrokenAddonRoot:
        def is_dir(self) -> bool:
            raise hostile_error

    def fail_config(_key: str) -> str:
        raise hostile_error

    diagnostic = module._registration_failure_diagnostic(
        BrokenWorkbenches(),
        BrokenAddonRoot(),
        config_get=fail_config,
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert len(payload["diagnostic_read_errors"]) == 3
    assert [error["phase"] for error in payload["diagnostic_read_errors"]] == [
        "workbenches",
        "additional_module_paths",
        "expected_addon_root",
    ]
    assert all(
        set(error) == {"phase", "type"}
        and len(error["type"]) <= 64
        and all(ord(character) < 128 for character in error["type"])
        for error in payload["diagnostic_read_errors"]
    )
    assert len(diagnostic) <= 2_000


@pytest.mark.parametrize(
    "sensitive",
    [
        pytest.param("\\", id="backslash"),
        pytest.param('"', id="double-quote"),
        pytest.param('\\"', id="alternating"),
    ],
)
def test_registration_failure_diagnostic_bounds_json_sensitive_worst_case(
    sensitive: str,
) -> None:
    module = _load_gui_harness_module()

    class InvalidAddonRoot:
        def is_dir(self) -> None:
            return None

    def fixed_sensitive(length: int) -> str:
        return (sensitive * length)[:length]

    workbenches = {fixed_sensitive(63) + str(index): object() for index in range(8)}
    diagnostic = module._registration_failure_diagnostic(
        workbenches,
        InvalidAddonRoot(),
        config_get=lambda _key: fixed_sensitive(512),
    )
    prefix = "expected one registered VibeCAD Workbench: "
    encoded = diagnostic.removeprefix(prefix)
    payload = json.loads(encoded)

    assert diagnostic.startswith(prefix)
    assert len(diagnostic) <= 2_000
    assert encoded == json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert payload["diagnostic_read_errors"] == [
        {"phase": "expected_addon_root", "type": "TypeError"}
    ]
    assert len(payload["workbench_names"]) == 8
    assert all(
        len(name) == 64 and '"' not in name and "\\" not in name
        for name in payload["workbench_names"]
    )
    assert len(payload["additional_module_paths"]) == 512
    assert '"' not in payload["additional_module_paths"]
    assert "\\" not in payload["additional_module_paths"]


@pytest.mark.parametrize(
    "sensitive",
    [
        pytest.param("\\", id="backslash"),
        pytest.param('"', id="double-quote"),
        pytest.param('\\"', id="alternating"),
    ],
)
def test_registration_failure_diagnostic_three_error_schema_fits_encoded_budget(
    sensitive: str,
) -> None:
    module = _load_gui_harness_module()

    def sanitized(length: int) -> str:
        value = (sensitive * length)[:length]
        return module._diagnostic_ascii(value, length)

    payload = {
        "additional_module_paths": sanitized(512),
        "diagnostic_read_errors": [
            {"phase": "workbenches", "type": sanitized(64)},
            {"phase": "additional_module_paths", "type": sanitized(64)},
            {"phase": "expected_addon_root", "type": sanitized(64)},
        ],
        "expected_addon_root_exists": None,
        "registered_vibecad_count": 0,
        "total_workbench_count": None,
        "workbench_names": [sanitized(64) for _index in range(8)],
        "workbench_names_truncated": True,
        "workbench_observed_count": 65,
    }
    encoded = module._canonical_json(payload)
    diagnostic = "expected one registered VibeCAD Workbench: " + encoded

    assert len(diagnostic) <= 2_000
    assert encoded == json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_diagnostic_ascii_preserves_meaningful_mac_path() -> None:
    module = _load_gui_harness_module()
    path = "/Users/wangtao/Library/Application Support/FreeCAD/Mod/VibeCAD"

    assert module._diagnostic_ascii(path, 512) == path


@pytest.mark.parametrize(
    "control_error",
    [
        pytest.param(KeyboardInterrupt("diagnostic interrupted"), id="keyboard-interrupt"),
        pytest.param(SystemExit(101), id="system-exit"),
        pytest.param(GeneratorExit("diagnostic generator closed"), id="generator-exit"),
    ],
)
@pytest.mark.parametrize("phase", ["workbenches", "config", "root"])
def test_registration_failure_diagnostic_preserves_control_flow_base_exceptions(
    tmp_path: Path,
    control_error: BaseException,
    phase: str,
) -> None:
    module = _load_gui_harness_module()

    class BrokenWorkbenches:
        def __iter__(self) -> object:
            raise control_error

    def config_get(_key: str) -> str:
        raise control_error

    class BrokenAddonRoot:
        def is_dir(self) -> bool:
            raise control_error

    with pytest.raises(type(control_error)) as captured:
        module._registration_failure_diagnostic(
            BrokenWorkbenches() if phase == "workbenches" else {},
            BrokenAddonRoot() if phase == "root" else tmp_path / "missing-addon",
            config_get=config_get if phase == "config" else lambda _key: "",
        )

    assert captured.value is control_error


def test_registration_failure_diagnostic_iterable_oserror_does_not_mask_primary(
    tmp_path: Path,
) -> None:
    module = _load_gui_harness_module()

    class BrokenWorkbenches:
        def __iter__(self) -> object:
            raise OSError("workbench read red")

    diagnostic = module._registration_failure_diagnostic(
        BrokenWorkbenches(),
        tmp_path / "missing-addon",
        config_get=lambda _key: "",
    )
    payload = json.loads(diagnostic.removeprefix("expected one registered VibeCAD Workbench: "))

    assert payload["total_workbench_count"] is None
    assert payload["diagnostic_read_errors"] == [{"phase": "workbenches", "type": "OSError"}]


def test_gui_harness_is_one_unittest_loader_compatible_test() -> None:
    source = Path(__file__).resolve().parent / "fixtures" / "freecad_workbench" / "gui_harness.py"
    assert "vibecad" in sys.modules
    module = _load_gui_harness_module()

    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    assert suite.countTestCases() == 1
    assert "test_gui_harness" in str(next(iter(suite)))
    source_text = source.read_text(encoding="utf-8")
    assert "FreeCADGui.activateWorkbench(workbench_id)" in source_text
    assert 'FreeCADGui.activateWorkbench("NoneWorkbench")' in source_text
    assert "host.activate_workbench()" not in source_text
    probe_source = source_text[source_text.index("def _run_nested_gui_probe") :]
    assert probe_source.splitlines()[1].strip() == (
        "vibecad_package, bootstrap = _bind_repository_vibecad()"
    )


def test_gui_harness_pins_repo_source_ahead_of_installed_and_equivalent_paths(
    tmp_path: Path,
) -> None:
    module = _load_gui_harness_module()
    repo = Path(__file__).resolve().parent.parent
    repo_source = (repo / "src").resolve(strict=True)
    expected_package = (repo_source / "vibecad" / "__init__.py").resolve(strict=True)
    expected_bootstrap = (repo_source / "vibecad" / "daemon" / "bootstrap.py").resolve(strict=True)
    installed_source = tmp_path / "installed"
    installed_package = installed_source / "vibecad"
    installed_daemon = installed_package / "daemon"
    installed_daemon.mkdir(parents=True)
    (installed_package / "__init__.py").write_text("", encoding="utf-8")
    (installed_daemon / "bootstrap.py").write_text("", encoding="utf-8")
    repo_alias = tmp_path / "repo-source-alias"
    repo_alias.symlink_to(repo_source, target_is_directory=True)
    search_path = [
        str(installed_source),
        str(repo_alias),
        str(repo_source),
        str(repo_source / ".." / "src"),
    ]
    modules: dict[str, object] = {}
    imported: list[str] = []
    invalidations: list[str] = []

    def fake_import(name: str) -> object:
        imported.append(name)
        assert search_path[0] == str(repo_source)
        assert sum(Path(entry).resolve() == repo_source for entry in search_path) == 1
        imported_module = ModuleType(name)
        if name == "vibecad":
            imported_module.__file__ = str(Path(search_path[0]) / "vibecad" / "__init__.py")
        elif name == "vibecad.daemon.bootstrap":
            imported_module.__file__ = str(
                Path(search_path[0]) / "vibecad" / "daemon" / "bootstrap.py"
            )
        else:
            raise AssertionError(f"unexpected import: {name}")
        modules[name] = imported_module
        return imported_module

    package, bootstrap = module._bind_repository_vibecad(
        _modules=modules,
        _search_path=search_path,
        _import_module=fake_import,
        _invalidate_caches=lambda: invalidations.append("invalidated"),
    )

    assert imported == ["vibecad", "vibecad.daemon.bootstrap"]
    assert invalidations == ["invalidated"]
    assert search_path == [str(repo_source), str(installed_source)]
    assert search_path[0] == str(repo_source)
    assert Path(package.__file__).resolve(strict=True) == expected_package
    assert Path(bootstrap.__file__).resolve(strict=True) == expected_bootstrap


@pytest.mark.parametrize("preloaded_name", ["vibecad", "vibecad.daemon.adapters"])
def test_gui_harness_rejects_any_preloaded_vibecad_namespace(preloaded_name: str) -> None:
    module = _load_gui_harness_module()
    search_path = ["installed-first"]
    imports: list[str] = []
    invalidations: list[str] = []

    with pytest.raises(RuntimeError, match="preloaded"):
        module._bind_repository_vibecad(
            _modules={preloaded_name: object()},
            _search_path=search_path,
            _import_module=lambda name: imports.append(name),
            _invalidate_caches=lambda: invalidations.append("invalidated"),
        )

    assert search_path == ["installed-first"]
    assert imports == []
    assert invalidations == []


@pytest.mark.parametrize(
    ("wrong_name", "message"),
    [
        ("vibecad", "vibecad source identity mismatch"),
        ("vibecad.daemon.bootstrap", "daemon bootstrap source identity mismatch"),
    ],
)
def test_gui_harness_rejects_wrong_import_source_identity(
    tmp_path: Path,
    wrong_name: str,
    message: str,
) -> None:
    module = _load_gui_harness_module()
    repo_source = (Path(__file__).resolve().parent.parent / "src").resolve(strict=True)
    wrong_source = tmp_path / "installed" / "vibecad"
    wrong_bootstrap = wrong_source / "daemon" / "bootstrap.py"
    wrong_bootstrap.parent.mkdir(parents=True)
    wrong_package = wrong_source / "__init__.py"
    wrong_package.write_text("", encoding="utf-8")
    wrong_bootstrap.write_text("", encoding="utf-8")

    def fake_import(name: str) -> object:
        imported_module = ModuleType(name)
        if name == "vibecad":
            source = (
                wrong_package if wrong_name == name else repo_source / "vibecad" / "__init__.py"
            )
        elif name == "vibecad.daemon.bootstrap":
            source = (
                wrong_bootstrap
                if wrong_name == name
                else repo_source / "vibecad" / "daemon" / "bootstrap.py"
            )
        else:
            raise AssertionError(f"unexpected import: {name}")
        imported_module.__file__ = str(source)
        return imported_module

    with pytest.raises(RuntimeError, match=message):
        module._bind_repository_vibecad(
            _modules={},
            _search_path=[str(repo_source)],
            _import_module=fake_import,
            _invalidate_caches=lambda: None,
        )


def test_gui_harness_records_real_refresh_and_complete_lifecycle() -> None:
    source = (
        Path(__file__).resolve().parent / "fixtures" / "freecad_workbench" / "gui_harness.py"
    ).read_text(encoding="utf-8")
    for required in (
        '"starting_snapshot"',
        '"refresh_snapshot"',
        '"stopping_snapshot"',
        '"active_dock_count"',
        '"activation_dock_status"',
        '"last_snapshot"',
        '"refresh_command_kinds"',
        '"refresh_heartbeat_delta"',
        'findChildren(QtWidgets.QDockWidget, "VibeCADReviewDock")',
        "QtWidgets.QPushButton,",
        '"VibeCADRefresh"',
        "refresh_buttons[0].click()",
        '"client_construction_count"',
        '"init_gui_source"',
        '"gateway_source"',
        '"dock_source"',
        '"vibecad_source"',
        "_activation_terminal_diagnostic(",
    ):
        assert required in source


@pytest.mark.parametrize(
    ("snapshot", "dock_status", "expected"),
    [
        ({"lifecycle": "inactive"}, "Connecting", "inactive"),
        ({"lifecycle": "stopping"}, "Closing", "stopping"),
        ({"lifecycle": "starting"}, "Unavailable", "Unavailable"),
    ],
)
def test_gui_harness_activation_terminal_states_fail_fast_with_bounded_diagnostic(
    snapshot: dict[str, object],
    dock_status: str,
    expected: str,
) -> None:
    module = _load_gui_harness_module()

    diagnostic = module._activation_terminal_diagnostic(snapshot, dock_status)

    assert type(diagnostic) is str
    assert expected in diagnostic
    assert len(diagnostic) <= 500
    assert (
        module._activation_terminal_diagnostic(
            {"lifecycle": "starting"},
            "Connecting",
        )
        is None
    )


def test_gui_binary_identity_binds_entry_and_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = (tmp_path / "prefix").resolve()
    binary = prefix / "bin" / "FreeCAD"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binary")
    binary.chmod(0o700)
    os.link(binary, prefix / "FreeCAD-hardlink")
    monkeypatch.setattr(paths, "freecad_path", lambda: binary)

    identity = _capture_gui_identity(binary, prefix)
    assert identity.target == binary
    assert len(identity.entry) == 8
    assert len(identity.resolved) == 8
    assert identity.entry[4] == 2
    _revalidate_gui_identity(identity, prefix)
    binary.write_bytes(b"replacement")
    with pytest.raises(RuntimeError, match="generation changed"):
        _revalidate_gui_identity(identity, prefix)
    binary.chmod(0o722)
    with pytest.raises(ValueError, match="owner-controlled"):
        _capture_gui_identity(binary, prefix)


def test_runtime_readiness_uses_authenticated_receipt_and_full_verify(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )
    calls: list[object] = []

    assert (
        _authenticate_runtime_generation(
            prefix,
            generation,
            read_receipt=lambda value: (
                calls.append(("receipt", value)),
                spec.expected_receipt(),
            )[1],
            capture=lambda value: (
                calls.append(("capture", value)),
                generation,
            )[1],
            verify=lambda value: (
                calls.append(("verify", value)),
                True,
            )[1],
        )
        is generation
    )
    assert calls == [
        ("receipt", prefix),
        ("capture", prefix),
        ("verify", generation),
    ]

    with pytest.raises(RuntimeError, match="receipt"):
        _authenticate_runtime_generation(
            prefix,
            generation,
            read_receipt=lambda _value: None,
        )
    with pytest.raises(RuntimeError, match="verification failed"):
        _authenticate_runtime_generation(
            prefix,
            generation,
            read_receipt=lambda _value: spec.expected_receipt(),
            capture=lambda _value: generation,
            verify=lambda _value: False,
        )


def test_runtime_authentication_defaults_resolve_status_dependencies_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "prefix"
    generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )
    calls: list[object] = []
    monkeypatch.setattr(
        status,
        "read_prefix_receipt",
        lambda value: (
            calls.append(("receipt", value)),
            spec.expected_receipt(),
        )[1],
    )
    monkeypatch.setattr(
        status,
        "capture_runtime_generation_evidence",
        lambda value: (
            calls.append(("capture", value)),
            generation,
        )[1],
    )
    monkeypatch.setattr(
        status,
        "verify_runtime_generation",
        lambda value: (
            calls.append(("verify", value)),
            True,
        )[1],
    )
    monkeypatch.setattr(
        status,
        "_spawn_probe_process",
        lambda *args, **kwargs: pytest.fail("the real verifier must not spawn"),
    )

    assert _authenticate_runtime_generation(prefix, generation) is generation
    assert calls == [
        ("receipt", prefix),
        ("capture", prefix),
        ("verify", generation),
    ]


@pytest.mark.parametrize(
    "selection_case",
    [
        "absent",
        "dotdot",
        "symlink",
        "duplicate-slash",
        "tilde",
        "string-subclass",
        "non-string",
    ],
)
def test_managed_authentication_rejects_nonexact_selection_before_capture(
    selection_case: str,
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    prefix.mkdir()
    environment: dict[str, object] = {}
    if selection_case == "dotdot":
        environment["VIBECAD_FREECAD_ENV"] = str(prefix.parent / "child" / ".." / prefix.name)
    elif selection_case == "symlink":
        alias = tmp_path / "managed-alias"
        alias.symlink_to(prefix, target_is_directory=True)
        environment["VIBECAD_FREECAD_ENV"] = str(alias)
    elif selection_case == "duplicate-slash":
        environment["VIBECAD_FREECAD_ENV"] = f"{prefix.parent}//{prefix.name}"
    elif selection_case == "tilde":
        environment["VIBECAD_FREECAD_ENV"] = f"~/{prefix.name}"
    elif selection_case == "string-subclass":

        class Selection(str):
            pass

        environment["VIBECAD_FREECAD_ENV"] = Selection(str(prefix))
    elif selection_case == "non-string":
        environment["VIBECAD_FREECAD_ENV"] = 7

    before = dict(environment)
    with pytest.raises(ValueError, match="exact canonical managed prefix selection"):
        _capture_authenticated_managed_generation(
            prefix,
            environment=environment,  # type: ignore[arg-type]
            managed_prefix=lambda: prefix,
            capture=lambda _value: pytest.fail("invalid selection must fail before capture"),
            authenticate=lambda _value, _expected: pytest.fail(
                "invalid selection must fail before authentication"
            ),
        )

    assert environment == before


def test_managed_authentication_hides_selection_and_restores_exact_value(
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    prefix.mkdir()
    generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )
    environment = {"VIBECAD_FREECAD_ENV": str(prefix), "KEEP": "exact"}
    calls: list[object] = []

    def capture(value: Path) -> status.RuntimeGenerationEvidence:
        calls.append(("capture", value, dict(environment)))
        assert "VIBECAD_FREECAD_ENV" not in environment
        return generation

    def authenticate(
        value: Path,
        expected: status.RuntimeGenerationEvidence,
    ) -> status.RuntimeGenerationEvidence:
        calls.append(("authenticate", value, expected, dict(environment)))
        assert "VIBECAD_FREECAD_ENV" not in environment
        return generation

    assert (
        _capture_authenticated_managed_generation(
            prefix,
            environment=environment,
            managed_prefix=lambda: prefix,
            capture=capture,
            authenticate=authenticate,
        )
        is generation
    )
    assert environment == {"VIBECAD_FREECAD_ENV": str(prefix), "KEEP": "exact"}
    assert calls == [
        ("capture", prefix, {"KEEP": "exact"}),
        ("authenticate", prefix, generation, {"KEEP": "exact"}),
    ]


def test_managed_authentication_restores_exact_value_and_propagates_failure(
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    prefix.mkdir()
    generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )
    environment = {"VIBECAD_FREECAD_ENV": str(prefix)}
    failure = RuntimeError("authentication sentinel")

    def authenticate(
        _value: Path,
        _expected: status.RuntimeGenerationEvidence,
    ) -> status.RuntimeGenerationEvidence:
        assert "VIBECAD_FREECAD_ENV" not in environment
        environment["VIBECAD_FREECAD_ENV"] = "authentication mutation"
        raise failure

    with pytest.raises(RuntimeError, match="authentication sentinel") as caught:
        _capture_authenticated_managed_generation(
            prefix,
            environment=environment,
            managed_prefix=lambda: prefix,
            capture=lambda _value: generation,
            authenticate=authenticate,
        )

    assert caught.value is failure
    assert environment == {"VIBECAD_FREECAD_ENV": str(prefix)}


def test_managed_authentication_requires_exact_captured_evidence_type(
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    prefix.mkdir()

    class EvidenceSubclass(status.RuntimeGenerationEvidence):
        pass

    generation = EvidenceSubclass(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )
    environment = {"VIBECAD_FREECAD_ENV": str(prefix)}

    with pytest.raises(RuntimeError, match="exact RuntimeGenerationEvidence"):
        _capture_authenticated_managed_generation(
            prefix,
            environment=environment,
            managed_prefix=lambda: prefix,
            capture=lambda _value: generation,
            authenticate=lambda _value, _expected: pytest.fail(
                "invalid captured evidence must fail before authentication"
            ),
        )

    assert environment == {"VIBECAD_FREECAD_ENV": str(prefix)}


def test_managed_authentication_requires_exact_authenticated_evidence_type(
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    prefix.mkdir()
    generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )

    class EvidenceSubclass(status.RuntimeGenerationEvidence):
        pass

    authenticated = EvidenceSubclass(
        prefix=generation.prefix,
        prefix_identity=generation.prefix_identity,
        python=generation.python,
        python_entry_identity=generation.python_entry_identity,
        python_target=generation.python_target,
        python_target_identity=generation.python_target_identity,
    )
    environment = {"VIBECAD_FREECAD_ENV": str(prefix)}

    with pytest.raises(RuntimeError, match="exact RuntimeGenerationEvidence"):
        _capture_authenticated_managed_generation(
            prefix,
            environment=environment,
            managed_prefix=lambda: prefix,
            capture=lambda _value: generation,
            authenticate=lambda _value, _expected: authenticated,
        )

    assert environment == {"VIBECAD_FREECAD_ENV": str(prefix)}


def test_managed_authentication_rejects_same_prefix_different_generation(
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    prefix.mkdir()
    generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )
    different_generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(11, 12),
        python=generation.python,
        python_entry_identity=generation.python_entry_identity,
        python_target=generation.python_target,
        python_target_identity=generation.python_target_identity,
    )
    environment = {"VIBECAD_FREECAD_ENV": str(prefix)}

    with pytest.raises(RuntimeError, match="generation changed"):
        _capture_authenticated_managed_generation(
            prefix,
            environment=environment,
            managed_prefix=lambda: prefix,
            capture=lambda _value: generation,
            authenticate=lambda _value, _expected: different_generation,
        )

    assert environment == {"VIBECAD_FREECAD_ENV": str(prefix)}


def test_managed_authentication_rejects_noncanonical_or_wrong_managed_prefix(
    tmp_path: Path,
) -> None:
    prefix = (tmp_path / "managed").resolve()
    wrong = (tmp_path / "wrong").resolve()
    prefix.mkdir()
    wrong.mkdir()
    environment = {"VIBECAD_FREECAD_ENV": str(wrong)}

    with pytest.raises(ValueError, match="canonical managed prefix"):
        _capture_authenticated_managed_generation(
            wrong,
            environment=environment,
            managed_prefix=lambda: prefix,
            capture=lambda _value: pytest.fail("wrong prefix must fail before capture"),
            authenticate=lambda _value, _expected: pytest.fail(
                "wrong prefix must fail before authentication"
            ),
        )

    assert environment == {"VIBECAD_FREECAD_ENV": str(wrong)}


def test_managed_authentication_binds_returned_evidence_prefix(tmp_path: Path) -> None:
    prefix = (tmp_path / "managed").resolve()
    wrong = (tmp_path / "wrong").resolve()
    prefix.mkdir()
    wrong.mkdir()
    generation = status.RuntimeGenerationEvidence(
        prefix=prefix,
        prefix_identity=(1, 2),
        python=prefix / "bin" / "python",
        python_entry_identity=(1, 3, 4, 5, 6),
        python_target=prefix / "bin" / "python3.12",
        python_target_identity=(1, 7, 8, 9, 10),
    )
    wrong_generation = status.RuntimeGenerationEvidence(
        prefix=wrong,
        prefix_identity=generation.prefix_identity,
        python=generation.python,
        python_entry_identity=generation.python_entry_identity,
        python_target=generation.python_target,
        python_target_identity=generation.python_target_identity,
    )
    environment = {"VIBECAD_FREECAD_ENV": str(prefix)}

    with pytest.raises(RuntimeError, match="bind the managed prefix"):
        _capture_authenticated_managed_generation(
            prefix,
            environment=environment,
            managed_prefix=lambda: prefix,
            capture=lambda _value: generation,
            authenticate=lambda _value, _expected: wrong_generation,
        )

    assert environment == {"VIBECAD_FREECAD_ENV": str(prefix)}


def test_genuine_external_overlap_defense_remains_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = (tmp_path / "VibeCAD").resolve()
    prefix = home / "runtime" / "mamba" / "envs" / "vibecad"
    prefix.mkdir(parents=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(prefix))

    with pytest.raises(ValueError, match="FreeCAD process directory is unavailable"):
        status.freecad_process_environment()

    assert not (home / "runtime" / "freecad-user").exists()


def test_m00_authenticates_before_resolving_the_gui_binary() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    m00 = source[source.rindex("def test_real_managed_freecad_gui_workbench_m00(") :]

    assert m00.index("prefix_value != str(canonical_managed_prefix)") < m00.index(
        "_capture_authenticated_managed_generation(prefix)"
    )
    assert m00.index("_capture_authenticated_managed_generation(prefix)") < m00.index(
        "paths.freecad_path()"
    )
    assert m00.index("managed runtime selection was not restored") < m00.index(
        "paths.freecad_path()"
    )


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        _RESULT_PREFIX + "{}\n" + _RESULT_PREFIX + "{}",
        _RESULT_PREFIX + '{"b":1,"a":2}',
        _RESULT_PREFIX + "[]",
    ],
)
def test_gui_result_requires_exactly_one_canonical_mapping(stdout: str) -> None:
    with pytest.raises(ValueError):
        _parse_gui_result(stdout)

    assert _parse_gui_result(_RESULT_PREFIX + '{"a":2,"b":1}') == {"a": 2, "b": 1}


def test_reclaim_signals_only_the_exact_original_session() -> None:
    token = _DarwinProcessToken(91, 2, 3, os.geteuid(), 91, 91)
    signals: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 91

        def __init__(self) -> None:
            self.waits = 0

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(["FreeCAD"], timeout)
            return -signal.SIGKILL

    assert _reclaim_gui_process(
        FakeProcess(),
        token,
        time.monotonic() + 5,
        capture=lambda _pid: token,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
    ) == (True, True)
    assert signals == [(91, signal.SIGTERM), (91, signal.SIGKILL)]

    with pytest.raises(RuntimeError, match="signaling forbidden"):
        _reclaim_gui_process(
            FakeProcess(),
            token,
            time.monotonic() + 5,
            capture=lambda _pid: _DarwinProcessToken(91, 9, 9, os.geteuid(), 91, 91),
            killpg=lambda pgid, sig: signals.append((pgid, sig)),
        )
    assert signals == [(91, signal.SIGTERM), (91, signal.SIGKILL)]

    capture_calls = 0

    def replaced_after_term(_pid: int) -> _DarwinProcessToken:
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 1:
            return token
        return _DarwinProcessToken(91, 9, 9, os.geteuid(), 91, 91)

    with pytest.raises(_GuiReclaimError, match="SIGKILL forbidden") as captured:
        _reclaim_gui_process(
            FakeProcess(),
            token,
            time.monotonic() + 5,
            capture=replaced_after_term,
            killpg=lambda pgid, sig: signals.append((pgid, sig)),
        )
    assert captured.value.term_sent
    assert not captured.value.kill_sent
    assert signals == [
        (91, signal.SIGTERM),
        (91, signal.SIGKILL),
        (91, signal.SIGTERM),
    ]


def test_token_capture_failure_waits_naturally_without_signaling() -> None:
    signals: list[tuple[int, int]] = []

    class NaturalExit:
        pid = 91

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None and timeout > 0
            return 0

    natural = _recover_gui_child(
        NaturalExit(),
        None,
        token_capture_failed=True,
        deadline=time.monotonic() + 20,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
    )
    assert natural.clean
    assert natural.detail == "token_unavailable_natural_exit"
    assert signals == []

    class Lingering(NaturalExit):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(["FreeCAD"], timeout)

    lingering = _recover_gui_child(
        Lingering(),
        None,
        token_capture_failed=True,
        deadline=time.monotonic() + 20,
        killpg=lambda pgid, sig: signals.append((pgid, sig)),
    )
    assert not lingering.clean
    assert lingering.detail == "token_unavailable_lingering"
    assert signals == []


def test_one_campaign_deadline_reserves_cleanup_time() -> None:
    assert _remaining_before_cleanup(60.0, _clock=lambda: 0.0) == 48.0
    with pytest.raises(TimeoutError, match="cleanup reserve"):
        _remaining_before_cleanup(12.0, _clock=lambda: 0.0)


def test_cleanup_precedes_result_and_semantic_failures() -> None:
    order: list[str] = []
    payload = _RESULT_PREFIX + json.dumps(
        {
            "active_snapshot": {},
            "error": None,
            "final_snapshot": {},
            "modal_detected": True,
            "status": "ok",
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    def action() -> _ParentAttempt:
        order.append("action")
        return _ParentAttempt(0, payload, "", False, None)

    def cleanup() -> _CleanupOutcome:
        order.append("cleanup")
        return _CleanupOutcome(True, True, False, False, "retired")

    def semantics(attempt: _ParentAttempt, outcome: _CleanupOutcome) -> None:
        order.append("semantics")
        _validate_gui_semantics(attempt, outcome, expected_daemon_id="daemon-1")

    with pytest.raises(AssertionError):
        _cleanup_before_semantics(action, cleanup, semantics)
    assert order == ["action", "cleanup", "semantics"]


def test_registered_finalizer_and_body_share_one_cleanup_outcome() -> None:
    ownership = _CleanupAssertionOwnership()
    ownership.mark_launch_attempted()
    cleanup_calls = 0

    def cleanup() -> _CleanupOutcome:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return _CleanupOutcome(True, True, False, False, "retired")

    def action() -> _ParentAttempt:
        return _ParentAttempt(0, "", "", False, None)

    def semantics(
        _attempt: _ParentAttempt,
        outcome: _CleanupOutcome,
    ) -> _CleanupOutcome:
        return outcome

    body_outcome = _cleanup_before_semantics(
        action,
        cleanup,
        semantics,
        ownership=ownership,
    )
    finalizer_outcome = _finalize_probe_cleanup(cleanup, ownership)
    assert body_outcome is finalizer_outcome
    assert cleanup_calls == 1


def test_cleanup_failure_is_combined_with_action_failure() -> None:
    action_error = RuntimeError("action red")

    def action() -> _ParentAttempt:
        raise action_error

    def cleanup() -> _CleanupOutcome:
        return _CleanupOutcome(False, True, False, False, "cleanup red")

    with pytest.raises(BaseExceptionGroup) as captured:
        _cleanup_before_semantics(action, cleanup, lambda _attempt, _outcome: None)
    assert [str(error) for error in captured.value.exceptions] == [
        "GUI cleanup failed: cleanup red",
        "action red",
    ]


def test_cleanup_emits_one_bounded_parent_evidence_before_cleanup_failure() -> None:
    order: list[str] = []
    emitted: list[dict[str, object]] = []
    child_result = _RESULT_PREFIX + '{"error":"connect failed","status":"error"}'
    malicious = "pid=1;daemon_id=forged;signal=KILL;" + "x" * 3_000

    def action() -> _ParentAttempt:
        order.append("action")
        return _ParentAttempt(17, child_result + "\n" + malicious, malicious, False, None)

    def cleanup() -> _CleanupOutcome:
        order.append("cleanup")
        return _CleanupOutcome(False, False, False, False, "publication_unproven")

    def emit(payload: dict[str, object]) -> None:
        order.append("evidence")
        emitted.append(payload)

    def semantics(_attempt: _ParentAttempt, _outcome: _CleanupOutcome) -> None:
        order.append("semantics")

    with pytest.raises(AssertionError, match="publication_unproven"):
        _cleanup_before_semantics(action, cleanup, semantics, _emit=emit)

    assert order == ["action", "cleanup", "evidence"]
    assert len(emitted) == 1
    evidence = emitted[0]
    assert evidence["child_returncode"] == 17
    assert evidence["gui_status"] == "error"
    assert evidence["gui_error_tail"] == "connect failed"
    assert evidence["cleanup"]["detail"] == "publication_unproven"
    assert evidence["cleanup"]["term_sent"] is False
    assert evidence["cleanup"]["kill_sent"] is False
    assert len(evidence["child_stdout_tail"]) == 2_000
    assert len(evidence["child_stderr_tail"]) == 2_000


def test_cleanup_precedes_child_output_parse_even_when_parse_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    emitted: list[dict[str, object]] = []

    def action() -> _ParentAttempt:
        order.append("action")
        return _ParentAttempt(9, "untrusted child output", "stderr", False, None)

    def cleanup() -> _CleanupOutcome:
        order.append("cleanup")
        return _CleanupOutcome(False, False, False, False, "publication_unproven")

    def parse(_stdout: str) -> dict[str, object]:
        order.append("parse")
        raise RuntimeError("parse red")

    def emit(payload: dict[str, object]) -> None:
        order.append("evidence")
        emitted.append(payload)

    monkeypatch.setattr(sys.modules[__name__], "_parse_gui_result", parse)
    with pytest.raises(AssertionError, match="publication_unproven"):
        _cleanup_before_semantics(
            action,
            cleanup,
            lambda _attempt, _outcome: pytest.fail("semantics must not execute"),
            _emit=emit,
        )

    assert order == ["action", "cleanup", "parse", "evidence"]
    assert emitted[0]["gui_parse_error"] == "RuntimeError: parse red"


@pytest.mark.parametrize(
    "control_error",
    [
        pytest.param(KeyboardInterrupt("parse interrupted"), id="keyboard-interrupt"),
        pytest.param(SystemExit(73), id="system-exit"),
        pytest.param(GeneratorExit("parse generator closed"), id="generator-exit"),
    ],
)
def test_parse_control_flow_base_exception_is_raised_after_cleanup_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    control_error: BaseException,
) -> None:
    order: list[str] = []
    cleanup_calls = 0
    evidence_calls = 0
    semantics_calls = 0
    ownership = _CleanupAssertionOwnership()
    ownership.mark_launch_attempted()

    def action() -> _ParentAttempt:
        order.append("action")
        return _ParentAttempt(0, "child output", "child stderr", False, None)

    def cleanup() -> _CleanupOutcome:
        nonlocal cleanup_calls
        cleanup_calls += 1
        order.append("cleanup")
        return _CleanupOutcome(True, False, False, False, "clean")

    def parse(_stdout: str) -> dict[str, object]:
        order.append("parse")
        raise control_error

    def emit(_payload: dict[str, object]) -> None:
        nonlocal evidence_calls
        evidence_calls += 1
        order.append("evidence")

    def semantics(_attempt: _ParentAttempt, _outcome: _CleanupOutcome) -> None:
        nonlocal semantics_calls
        semantics_calls += 1

    monkeypatch.setattr(sys.modules[__name__], "_parse_gui_result", parse)
    with pytest.raises(type(control_error)) as captured:
        _cleanup_before_semantics(
            action,
            cleanup,
            semantics,
            ownership=ownership,
            _emit=emit,
        )

    assert captured.value is control_error
    assert order == ["action", "cleanup", "parse", "evidence"]
    assert cleanup_calls == 1
    assert evidence_calls == 1
    assert semantics_calls == 0
    assert _finalize_probe_cleanup(cleanup, ownership).detail == "clean"
    assert cleanup_calls == 1


def test_parse_control_flow_combines_after_cleanup_and_action_before_evidence_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    cleanup_calls = 0
    evidence_calls = 0
    semantics_calls = 0
    ownership = _CleanupAssertionOwnership()
    ownership.mark_launch_attempted()
    action_error = RuntimeError("action red")
    control_error = SystemExit(91)
    evidence_error = RuntimeError("evidence red")

    def action() -> _ParentAttempt:
        order.append("action")
        return _ParentAttempt(8, "child output", "child stderr", False, action_error)

    def cleanup() -> _CleanupOutcome:
        nonlocal cleanup_calls
        cleanup_calls += 1
        order.append("cleanup")
        return _CleanupOutcome(False, False, False, False, "cleanup red")

    def parse(_stdout: str) -> dict[str, object]:
        order.append("parse")
        raise control_error

    def emit(_payload: dict[str, object]) -> None:
        nonlocal evidence_calls
        evidence_calls += 1
        order.append("evidence")
        raise evidence_error

    def semantics(_attempt: _ParentAttempt, _outcome: _CleanupOutcome) -> None:
        nonlocal semantics_calls
        semantics_calls += 1

    monkeypatch.setattr(sys.modules[__name__], "_parse_gui_result", parse)
    with pytest.raises(BaseExceptionGroup) as captured:
        _cleanup_before_semantics(
            action,
            cleanup,
            semantics,
            ownership=ownership,
            _emit=emit,
        )

    failures = captured.value.exceptions
    assert str(failures[0]) == "GUI cleanup failed: cleanup red"
    assert failures[1] is action_error
    assert failures[2] is control_error
    assert failures[3] is evidence_error
    assert order == ["action", "cleanup", "parse", "evidence"]
    assert cleanup_calls == 1
    assert evidence_calls == 1
    assert semantics_calls == 0
    assert _finalize_probe_cleanup(cleanup, ownership).detail == "cleanup red"
    assert cleanup_calls == 1


@pytest.mark.parametrize("message", ["binary recheck red", "generation recheck red"])
def test_post_capture_failure_preserves_the_complete_parent_attempt(message: str) -> None:
    captured = _ParentAttempt(
        23,
        _RESULT_PREFIX + '{"error":"child red","status":"error"}',
        "bounded stderr",
        True,
        None,
    )

    def fail_recheck() -> None:
        raise RuntimeError(message)

    finalized = _finalize_captured_attempt(
        captured,
        observe_publication=lambda: None,
        recheck=fail_recheck,
    )

    assert finalized is not captured
    assert replace(finalized, action_error=None) == captured
    assert type(finalized.action_error) is RuntimeError
    assert str(finalized.action_error) == message


def test_post_capture_no_publication_result_continues_recheck() -> None:
    captured = _ParentAttempt(0, "stdout", "stderr", False, None)
    checks: list[str] = []

    finalized = _finalize_captured_attempt(
        captured,
        observe_publication=lambda: None,
        recheck=lambda: checks.append("rechecked"),
    )

    assert finalized is captured
    assert finalized.action_error is None
    assert checks == ["rechecked"]


@pytest.mark.parametrize(
    "message",
    [
        "cleanup guard did not prove cold state",
        "daemon publication is ambiguous",
        "daemon receipt is invalid",
        "daemon publication changed after authentication",
    ],
)
def test_post_capture_publication_runtime_error_is_fail_closed(message: str) -> None:
    captured = _ParentAttempt(31, "captured stdout", "captured stderr", True, None)
    checks: list[str] = []

    def publication_failure() -> None:
        raise RuntimeError(message)

    finalized = _finalize_captured_attempt(
        captured,
        observe_publication=publication_failure,
        recheck=lambda: checks.append("must not run"),
    )

    assert replace(finalized, action_error=None) == captured
    assert type(finalized.action_error) is RuntimeError
    assert str(finalized.action_error) == message
    assert checks == []


def test_post_capture_publication_base_exception_is_preserved() -> None:
    captured = _ParentAttempt(32, "stdout", "stderr", False, None)
    failure = KeyboardInterrupt("publication interrupted")
    checks: list[str] = []

    def publication_failure() -> None:
        raise failure

    finalized = _finalize_captured_attempt(
        captured,
        observe_publication=publication_failure,
        recheck=lambda: checks.append("must not run"),
    )

    assert replace(finalized, action_error=None) == captured
    assert finalized.action_error is failure
    assert checks == []


def test_post_capture_publication_failure_still_cleans_once_and_emits_original_child_fields() -> (
    None
):
    captured = _ParentAttempt(
        33,
        _RESULT_PREFIX + '{"error":"child failed","status":"error"}',
        "captured stderr",
        False,
        None,
    )
    cleanup_calls = 0
    emitted: list[dict[str, object]] = []

    def action() -> _ParentAttempt:
        return _finalize_captured_attempt(
            captured,
            observe_publication=lambda: (_ for _ in ()).throw(
                RuntimeError("daemon publication is ambiguous")
            ),
            recheck=lambda: pytest.fail("recheck must not run"),
        )

    def cleanup() -> _CleanupOutcome:
        nonlocal cleanup_calls
        cleanup_calls += 1
        return _CleanupOutcome(True, False, False, False, "clean")

    with pytest.raises(RuntimeError, match="publication is ambiguous"):
        _cleanup_before_semantics(
            action,
            cleanup,
            lambda _attempt, _outcome: pytest.fail("semantics must not run"),
            _emit=emitted.append,
        )

    assert cleanup_calls == 1
    assert len(emitted) == 1
    evidence = emitted[0]
    assert evidence["action_error"] == "RuntimeError: daemon publication is ambiguous"
    assert evidence["child_returncode"] == 33
    assert evidence["child_stdout_tail"] == captured.stdout
    assert evidence["child_stderr_tail"] == captured.stderr
    assert evidence["timed_out"] is False


def test_action_cleanup_and_evidence_errors_are_combined_and_finalizer_does_not_reenter() -> None:
    ownership = _CleanupAssertionOwnership()
    ownership.mark_launch_attempted()
    cleanup_calls = 0

    def action() -> _ParentAttempt:
        raise RuntimeError("action red")

    def cleanup() -> _CleanupOutcome:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise RuntimeError("cleanup red")

    def emit(_payload: dict[str, object]) -> None:
        raise RuntimeError("evidence red")

    with pytest.raises(BaseExceptionGroup) as captured:
        _cleanup_before_semantics(
            action,
            cleanup,
            lambda _attempt, _outcome: pytest.fail("semantics must not execute"),
            ownership=ownership,
            _emit=emit,
        )
    assert [str(error) for error in captured.value.exceptions] == [
        "cleanup red",
        "action red",
        "evidence red",
    ]
    assert cleanup_calls == 1
    assert _finalize_probe_cleanup(cleanup, ownership).detail == "cleanup_error"
    assert cleanup_calls == 1


def test_parent_rejects_timeout_no_result_modal_and_wrong_daemon() -> None:
    cleanup = _CleanupOutcome(True, True, False, False, "retired")
    timeout = _ParentAttempt(None, "", "", True, None)
    with pytest.raises(AssertionError, match="timed out"):
        _validate_gui_semantics(timeout, cleanup, expected_daemon_id="daemon-1")
    with pytest.raises(ValueError, match="observed 0"):
        _validate_gui_semantics(
            _ParentAttempt(0, "unrelated", "", False, None),
            cleanup,
            expected_daemon_id="daemon-1",
        )

    def snapshot(
        lifecycle: str,
        *,
        construction_count: int,
        daemon_id: str | None,
        dock_count: int,
        heartbeat_count: int,
        worker_thread_id: int | None,
    ) -> dict[str, object]:
        return {
            "client_construction_count": construction_count,
            "daemon_id": daemon_id,
            "dock_count": dock_count,
            "heartbeat_count": heartbeat_count,
            "lifecycle": lifecycle,
            "main_thread_id": 1,
            "schema_version": 1,
            "worker_thread_id": worker_thread_id,
        }

    base: dict[str, object] = {
        "active_dock_count": 1,
        "active_snapshot": snapshot(
            "active",
            construction_count=1,
            daemon_id="daemon-2",
            dock_count=1,
            heartbeat_count=1,
            worker_thread_id=2,
        ),
        "addon_registered": True,
        "client_connected": True,
        "deactivation_via_workbench": True,
        "dock_count_after_shutdown": 0,
        "error": None,
        "final_snapshot": snapshot(
            "inactive",
            construction_count=1,
            daemon_id=None,
            dock_count=0,
            heartbeat_count=2,
            worker_thread_id=None,
        ),
        "main_thread_id": 1,
        "modal_detected": False,
        "harness_heartbeat_count": 1,
        "initial_snapshot": snapshot(
            "inactive",
            construction_count=0,
            daemon_id=None,
            dock_count=0,
            heartbeat_count=0,
            worker_thread_id=None,
        ),
        "qt_binding": "PySide",
        "qt_binding_version": "6.8.0",
        "qt_version": "6.8",
        "refresh_event_delta": 1,
        "refresh_command_kinds": ["list_projects"],
        "refresh_heartbeat_delta": 1,
        "refresh_snapshot": snapshot(
            "active",
            construction_count=1,
            daemon_id="daemon-2",
            dock_count=1,
            heartbeat_count=2,
            worker_thread_id=2,
        ),
        "refresh_triggered": True,
        "starting_snapshot": snapshot(
            "starting",
            construction_count=0,
            daemon_id=None,
            dock_count=1,
            heartbeat_count=0,
            worker_thread_id=None,
        ),
        "status": "ok",
        "stopping_snapshot": snapshot(
            "stopping",
            construction_count=1,
            daemon_id="daemon-2",
            dock_count=1,
            heartbeat_count=2,
            worker_thread_id=2,
        ),
        "workbench_count": 1,
        "workbench_activated": True,
        "workbench_ids": ["VibeCADWorkbench"],
    }
    encoded = _RESULT_PREFIX + json.dumps(base, separators=(",", ":"), sort_keys=True)
    with pytest.raises(AssertionError):
        _validate_gui_semantics(
            _ParentAttempt(0, encoded, "", False, None),
            cleanup,
            expected_daemon_id="daemon-1",
        )
    base["modal_detected"] = True
    encoded = _RESULT_PREFIX + json.dumps(base, separators=(",", ":"), sort_keys=True)
    with pytest.raises(AssertionError):
        _validate_gui_semantics(
            _ParentAttempt(0, encoded, "", False, None),
            cleanup,
            expected_daemon_id="daemon-2",
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="real M00 uses Darwin process identity")
@pytest.mark.slow
def test_real_managed_freecad_gui_workbench_m00(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run real G1-M00")
    prefix_value = os.environ.get("VIBECAD_FREECAD_ENV")
    if type(prefix_value) is not str:
        pytest.fail("set VIBECAD_FREECAD_ENV to the exact canonical managed prefix")
    try:
        prefix = Path(prefix_value).resolve(strict=True)
        canonical_managed_prefix = paths.env_prefix().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        pytest.fail("set VIBECAD_FREECAD_ENV to the exact canonical managed prefix")
    if prefix != canonical_managed_prefix or prefix_value != str(canonical_managed_prefix):
        pytest.fail("set VIBECAD_FREECAD_ENV to the exact canonical managed prefix")
    generation = _capture_authenticated_managed_generation(prefix)
    if os.environ.get("VIBECAD_FREECAD_ENV") != prefix_value:
        pytest.fail("managed runtime selection was not restored after authentication")
    binary = paths.freecad_path()
    gui_identity = _capture_gui_identity(binary, prefix)
    repo = Path(__file__).resolve().parent.parent

    canonical_parent = _DARWIN_TEMP_PARENT.resolve(strict=True)
    if canonical_parent != _DARWIN_TEMP_PARENT:
        pytest.fail("Darwin temporary parent is not canonical")
    isolated = tempfile.TemporaryDirectory(prefix=_DARWIN_TEMP_PREFIX, dir=canonical_parent)
    request.addfinalizer(isolated.cleanup)
    root = Path(isolated.name)
    suffix = root.name.removeprefix(_DARWIN_TEMP_PREFIX)
    if (
        root.parent != canonical_parent
        or not root.name.startswith(_DARWIN_TEMP_PREFIX)
        or len(suffix) != 8
    ):
        pytest.fail("isolated root name is outside the M00 namespace")
    roots = _private_roots(root)
    environment = _gui_environment(roots, prefix)
    monkeypatch.setenv("VIBECAD_HOME", str(roots.vibecad))
    with status.runtime_maintenance_lock(timeout=5):
        current_generation = status.capture_runtime_generation_evidence(prefix)
        if current_generation != generation:
            pytest.fail("managed runtime generation changed before binding")
        status.write_external_runtime_receipt(prefix, evidence=current_generation)

    run_root = daemon_run_root(paths.data_root())
    cleanup_guard = _DaemonCleanupGuard(run_root)
    cleanup_guard.require_cold()
    deadline = time.monotonic() + _CAMPAIGN_TIMEOUT_SECONDS
    process: subprocess.Popen[str] | None = None
    process_token: _DarwinProcessToken | None = None
    token_capture_failed = False
    launch_count = 0
    term_sent = False
    kill_sent = False

    def cleanup() -> _CleanupOutcome:
        nonlocal kill_sent, term_sent
        gui_outcome = _recover_gui_child(
            process,
            process_token,
            token_capture_failed=token_capture_failed,
            deadline=deadline,
        )
        term_sent = gui_outcome.term_sent
        kill_sent = gui_outcome.kill_sent
        identity_clean = True
        identity_detail = "identity_not_checked"
        if process is not None and gui_outcome.clean:
            try:
                _revalidate_gui_identity(gui_identity, prefix)
                if (
                    status.read_prefix_receipt(prefix) != spec.expected_receipt()
                    or status.capture_runtime_generation_evidence(prefix) != generation
                ):
                    raise RuntimeError("managed runtime identity changed after child exit")
            except (OSError, RuntimeError, ValueError) as exc:
                identity_clean = False
                identity_detail = f"identity_recheck_failed:{type(exc).__name__}"
            else:
                identity_detail = "identity_rechecked"
        try:
            cleanup_guard.observe_publication()
        except RuntimeError:
            pass
        outcome = cleanup_guard.cleanup()
        return _CleanupOutcome(
            gui_outcome.clean and identity_clean and outcome.clean,
            outcome.retire_attempted,
            outcome.term_sent or term_sent,
            outcome.kill_sent or kill_sent,
            f"gui={gui_outcome.detail};{identity_detail};daemon={outcome.detail}",
        )

    ownership = _CleanupAssertionOwnership()
    request.addfinalizer(lambda: _finalize_probe_cleanup(cleanup, ownership))

    def action() -> _ParentAttempt:
        nonlocal launch_count, process, process_token, token_capture_failed
        _revalidate_gui_identity(gui_identity, prefix)
        if (
            status.read_prefix_receipt(prefix) != spec.expected_receipt()
            or status.capture_runtime_generation_evidence(prefix) != generation
        ):
            raise RuntimeError("managed runtime identity changed before launch")
        launch_count += 1
        if launch_count != 1:
            raise RuntimeError("M00 attempted more than one GUI launch")
        ownership.mark_launch_attempted()
        process = subprocess.Popen(
            _gui_command(binary, repo),
            env=environment,
            start_new_session=True,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            process_token = _darwin_process_token(process.pid)
        except (OSError, RuntimeError, ValueError) as exc:
            token_capture_failed = True
            raise RuntimeError("GUI process token capture failed") from exc
        try:
            stdout, stderr = process.communicate(timeout=_remaining_before_cleanup(deadline))
            attempt = _ParentAttempt(process.returncode, stdout, stderr, False, None)
        except subprocess.TimeoutExpired as exc:
            attempt = _ParentAttempt(
                None,
                exc.stdout or "",
                exc.stderr or "",
                True,
                None,
            )

        def recheck_after_capture() -> None:
            _revalidate_gui_identity(gui_identity, prefix)
            if status.capture_runtime_generation_evidence(prefix) != generation:
                raise RuntimeError("managed runtime generation changed after exit")

        return _finalize_captured_attempt(
            attempt,
            observe_publication=cleanup_guard.observe_publication,
            recheck=recheck_after_capture,
        )

    def semantics(attempt: _ParentAttempt, outcome: _CleanupOutcome) -> dict[str, object]:
        daemon_id = cleanup_guard.daemon_id
        assert type(daemon_id) is str
        assert launch_count == 1
        assert process_token is not None
        assert not _same_process_generation(process_token)
        assert cleanup_guard.original_token_absent
        assert not os.path.lexists(run_root / DAEMON_ENDPOINT_NAME)
        assert not os.path.lexists(run_root / DAEMON_RECEIPT_NAME)
        assert _safe_absent_or_empty_run_root(run_root)
        assert not any(roots.vibecad.joinpath("data", "checkouts").glob("*"))
        return _validate_gui_semantics(
            attempt,
            outcome,
            expected_daemon_id=daemon_id,
            expected_gui_target=gui_identity.target,
            expected_home=roots.vibecad,
            expected_prefix=prefix,
            expected_repo=repo,
        )

    _cleanup_before_semantics(
        action,
        cleanup,
        semantics,
        ownership=ownership,
    )
