"""Bounded real-GUI harness loaded by FreeCAD's unittest test runner."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import stat
import sys
import threading
import time
import traceback
import unittest
from collections.abc import Callable
from pathlib import Path

_RESULT_PREFIX = "VIBECAD_GUI_HARNESS="
_HARNESS_TIMEOUT_SECONDS = 45.0
_WORKBENCH_READ_LIMIT = 64


def _canonical_json(value: dict[str, object]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _source(value: object) -> str:
    source = inspect.getsourcefile(value)
    if source is None:
        raise RuntimeError("reviewed Python source identity is unavailable")
    return str(Path(source).resolve(strict=True))


def _matches_path(value: object, expected: Path) -> bool:
    try:
        return Path(os.fspath(value)).resolve() == expected
    except (OSError, TypeError, ValueError):
        return False


def _bind_repository_vibecad(
    *,
    _modules: dict[str, object] | None = None,
    _search_path: list[str] | None = None,
    _import_module: Callable[[str], object] = importlib.import_module,
    _invalidate_caches: Callable[[], None] = importlib.invalidate_caches,
) -> tuple[object, object]:
    harness_source = Path(__file__).resolve(strict=True)
    expected_repo = harness_source.parents[3]
    expected_harness = (
        expected_repo / "tests" / "fixtures" / "freecad_workbench" / "gui_harness.py"
    ).resolve(strict=True)
    repo_source = (expected_repo / "src").resolve(strict=True)
    expected_vibecad = (repo_source / "vibecad" / "__init__.py").resolve(strict=True)
    expected_bootstrap = (repo_source / "vibecad" / "daemon" / "bootstrap.py").resolve(strict=True)
    if harness_source != expected_harness:
        raise RuntimeError("GUI harness source identity mismatch")
    for label, source in (
        ("GUI harness", expected_harness),
        ("vibecad", expected_vibecad),
        ("daemon bootstrap", expected_bootstrap),
    ):
        source_info = source.lstat()
        if not stat.S_ISREG(source_info.st_mode):
            raise RuntimeError(f"{label} repository source is not regular")

    modules = sys.modules if _modules is None else _modules
    preloaded = sorted(name for name in modules if name == "vibecad" or name.startswith("vibecad."))
    if preloaded:
        raise RuntimeError("vibecad was preloaded before GUI harness repository binding")

    search_path = sys.path if _search_path is None else _search_path
    search_path[:] = [entry for entry in search_path if not _matches_path(entry, repo_source)]
    search_path.insert(0, str(repo_source))
    if search_path[0] != str(repo_source):
        raise RuntimeError("repository source is not exact sys.path[0]")
    if sum(_matches_path(entry, repo_source) for entry in search_path) != 1:
        raise RuntimeError("repository source path deduplication failed")
    _invalidate_caches()

    vibecad_package = _import_module("vibecad")
    if _source(vibecad_package) != str(expected_vibecad):
        raise RuntimeError("vibecad source identity mismatch")
    bootstrap = _import_module("vibecad.daemon.bootstrap")
    if _source(bootstrap) != str(expected_bootstrap):
        raise RuntimeError("daemon bootstrap source identity mismatch")
    return vibecad_package, bootstrap


def _activation_terminal_diagnostic(
    snapshot: dict[str, object],
    dock_status: str | None,
) -> str | None:
    lifecycle = snapshot.get("lifecycle")
    if lifecycle not in {"inactive", "stopping"} and dock_status != "Unavailable":
        return None
    encoded = _canonical_json({"dock_status": dock_status, "snapshot": snapshot})
    return f"activation terminated before active state: {encoded}"[:500]


def _diagnostic_ascii(value: str, limit: int) -> str:
    return "".join(
        character if 32 <= ord(character) < 127 and character not in {'"', "\\"} else "?"
        for character in value[:limit]
    )


def _registration_failure_diagnostic(
    workbenches: object,
    expected_addon_root: Path,
    *,
    config_get: Callable[[str], object],
) -> str:
    diagnostic_read_errors: list[dict[str, str]] = []

    def record_error(phase: str, error_type: str) -> None:
        if phase not in {
            "workbenches",
            "additional_module_paths",
            "expected_addon_root",
        }:
            raise RuntimeError("invalid diagnostic read phase")
        if len(diagnostic_read_errors) >= 3:
            return
        diagnostic_read_errors.append(
            {
                "phase": phase,
                "type": _diagnostic_ascii(error_type, 64),
            }
        )

    observed_entries: list[object] = []
    observed_count = 0
    iteration_complete = False
    try:
        iterator = iter(workbenches)
        for _index in range(_WORKBENCH_READ_LIMIT + 1):
            try:
                entry = next(iterator)
            except StopIteration:
                iteration_complete = True
                break
            observed_count += 1
            if len(observed_entries) < _WORKBENCH_READ_LIMIT:
                observed_entries.append(entry)
    except Exception as exc:
        record_error("workbenches", type(exc).__name__)
    names = sorted(_diagnostic_ascii(name, 64) for name in observed_entries if type(name) is str)
    try:
        additional = config_get("AdditionalModulePaths")
    except Exception as exc:
        additional_module_paths = None
        record_error("additional_module_paths", type(exc).__name__)
    else:
        additional_module_paths = (
            _diagnostic_ascii(additional, 512) if type(additional) is str else None
        )
    try:
        observed_addon_root_exists = expected_addon_root.is_dir()
    except Exception as exc:
        expected_addon_root_exists = None
        record_error("expected_addon_root", type(exc).__name__)
    else:
        if type(observed_addon_root_exists) is bool:
            expected_addon_root_exists = observed_addon_root_exists
        else:
            expected_addon_root_exists = None
            record_error("expected_addon_root", "TypeError")
    total_workbench_count = len(workbenches) if type(workbenches) is dict else None
    payload: dict[str, object] = {
        "additional_module_paths": additional_module_paths,
        "diagnostic_read_errors": diagnostic_read_errors,
        "expected_addon_root_exists": expected_addon_root_exists,
        "registered_vibecad_count": sum(
            name in {"VibeCAD", "VibeCADWorkbench"}
            for name in observed_entries
            if type(name) is str
        ),
        "total_workbench_count": total_workbench_count,
        "workbench_observed_count": observed_count,
        "workbench_names": names[:8],
        "workbench_names_truncated": (
            not iteration_complete or observed_count > _WORKBENCH_READ_LIMIT or len(names) > 8
        ),
    }
    return "expected one registered VibeCAD Workbench: " + _canonical_json(payload)


def _run_nested_gui_probe() -> dict[str, object]:
    vibecad_package, bootstrap = _bind_repository_vibecad()

    import FreeCAD
    import FreeCADGui
    import PySide
    from PySide import QtCore, QtWidgets

    workbenches = FreeCADGui.listWorkbenches()
    registered_ids = sorted(name for name in ("VibeCAD", "VibeCADWorkbench") if name in workbenches)
    if len(registered_ids) != 1:
        expected_addon_root = Path(__file__).resolve(strict=True).parents[3] / "freecad" / "VibeCAD"
        raise RuntimeError(
            _registration_failure_diagnostic(
                workbenches,
                expected_addon_root,
                config_get=lambda key: FreeCAD.ConfigGet(key),
            )
        )
    if "NoneWorkbench" not in workbenches:
        raise RuntimeError("NoneWorkbench is unavailable for callback-driven deactivation")
    workbench_id = registered_ids[0]

    from vibecad_workbench import dock, gateway, host

    deadline = time.monotonic() + _HARNESS_TIMEOUT_SECONDS
    app = QtWidgets.QApplication.instance()
    if app is None:
        raise RuntimeError("FreeCAD QApplication is unavailable")
    main_window = FreeCADGui.getMainWindow()
    main_thread_id = threading.get_ident()
    heartbeat = 0
    modal_detected = False
    active_snapshot: dict[str, object] | None = None
    activation_dock_status: str | None = None
    final_snapshot: dict[str, object] | None = None
    initial_snapshot: dict[str, object] | None = None
    last_snapshot: dict[str, object] | None = None
    refresh_snapshot: dict[str, object] | None = None
    starting_snapshot: dict[str, object] | None = None
    stopping_snapshot: dict[str, object] | None = None
    active_dock_count = 0
    refresh_event_baseline = 0
    refresh_event_delta = 0
    refresh_heartbeat_baseline = 0
    refresh_heartbeat_delta = 0
    refresh_command_kinds: list[str] = []
    refresh_triggered = False
    error: str | None = None
    phase = "activating"

    loop = QtCore.QEventLoop()
    heartbeat_timer = QtCore.QTimer()
    heartbeat_timer.setInterval(25)

    def finish_with_error(message: str) -> None:
        nonlocal error
        if error is None:
            error = message
        loop.quit()

    def on_heartbeat() -> None:
        nonlocal active_dock_count
        nonlocal active_snapshot
        nonlocal activation_dock_status
        nonlocal final_snapshot
        nonlocal heartbeat
        nonlocal last_snapshot
        nonlocal modal_detected
        nonlocal phase
        nonlocal refresh_command_kinds
        nonlocal refresh_event_baseline
        nonlocal refresh_event_delta
        nonlocal refresh_heartbeat_baseline
        nonlocal refresh_heartbeat_delta
        nonlocal refresh_snapshot
        nonlocal refresh_triggered
        nonlocal stopping_snapshot
        heartbeat += 1
        if app.activeModalWidget() is not None:
            modal_detected = True
            finish_with_error("modal widget detected")
            return
        try:
            snapshot = host.workbench_snapshot()
            if type(snapshot) is not dict:
                raise RuntimeError("workbench snapshot is not a plain mapping")
            last_snapshot = dict(snapshot)
            if phase == "activating":
                observed_docks = main_window.findChildren(
                    QtWidgets.QDockWidget,
                    "VibeCADReviewDock",
                )
                activation_dock_status = None
                if len(observed_docks) == 1:
                    status_labels = observed_docks[0].findChildren(
                        QtWidgets.QLabel,
                        "VibeCADConnectionStatus",
                    )
                    if len(status_labels) == 1:
                        activation_dock_status = status_labels[0].text()
                terminal = _activation_terminal_diagnostic(
                    snapshot,
                    activation_dock_status,
                )
                if terminal is not None:
                    finish_with_error(terminal)
                    return
                if (
                    snapshot.get("lifecycle") == "active"
                    and type(snapshot.get("worker_thread_id")) is int
                    and type(snapshot.get("daemon_id")) is str
                    and type(snapshot.get("heartbeat_count")) is int
                    and snapshot["heartbeat_count"] > 0
                    and snapshot.get("client_construction_count") == 1
                ):
                    active_snapshot = dict(snapshot)
                    active_docks = main_window.findChildren(
                        QtWidgets.QDockWidget,
                        "VibeCADReviewDock",
                    )
                    if len(active_docks) != 1:
                        raise RuntimeError(
                            f"expected one active VibeCAD Dock, observed {len(active_docks)}"
                        )
                    active_dock_count = len(active_docks)
                    refresh_buttons = active_docks[0].findChildren(
                        QtWidgets.QPushButton,
                        "VibeCADRefresh",
                    )
                    if len(refresh_buttons) != 1:
                        raise RuntimeError(
                            f"expected one VibeCAD Refresh action, observed {len(refresh_buttons)}"
                        )
                    refresh_event_baseline = int(snapshot["heartbeat_count"])
                    refresh_heartbeat_baseline = heartbeat

                    def observe_refresh(command: object) -> None:
                        if type(command) is not dict or type(command.get("kind")) is not str:
                            refresh_command_kinds.append("invalid")
                        else:
                            refresh_command_kinds.append(command["kind"])

                    active_docks[0].request.connect(observe_refresh)
                    try:
                        refresh_buttons[0].click()
                    finally:
                        active_docks[0].request.disconnect(observe_refresh)
                    if not refresh_command_kinds or "invalid" in refresh_command_kinds:
                        raise RuntimeError("Refresh emitted no valid worker command")
                    refresh_triggered = True
                    phase = "refreshing"
            elif phase == "refreshing":
                event_count = snapshot.get("heartbeat_count")
                if (
                    snapshot.get("lifecycle") == "active"
                    and snapshot.get("client_construction_count") == 1
                    and type(event_count) is int
                    and event_count > refresh_event_baseline
                    and heartbeat > refresh_heartbeat_baseline
                ):
                    refresh_snapshot = dict(snapshot)
                    refresh_event_delta = event_count - refresh_event_baseline
                    refresh_heartbeat_delta = heartbeat - refresh_heartbeat_baseline
                    FreeCADGui.activateWorkbench("NoneWorkbench")
                    observed_stopping = host.workbench_snapshot()
                    if type(observed_stopping) is not dict:
                        raise RuntimeError("stopping workbench snapshot is not a plain mapping")
                    stopping_snapshot = dict(observed_stopping)
                    phase = "deactivating"
            elif phase == "deactivating":
                if (
                    snapshot.get("lifecycle") == "inactive"
                    and snapshot.get("dock_count") == 0
                    and snapshot.get("worker_thread_id") is None
                    and snapshot.get("daemon_id") is None
                ):
                    final_snapshot = dict(snapshot)
                    loop.quit()
        except BaseException as exc:
            finish_with_error(f"{type(exc).__name__}: {exc}")

    heartbeat_timer.timeout.connect(on_heartbeat)
    deadline_timer = QtCore.QTimer()
    deadline_timer.setSingleShot(True)
    deadline_timer.timeout.connect(lambda: finish_with_error("absolute GUI deadline expired"))

    try:
        observed_initial = host.workbench_snapshot()
        if type(observed_initial) is not dict:
            raise RuntimeError("initial workbench snapshot is not a plain mapping")
        initial_snapshot = dict(observed_initial)
        FreeCADGui.activateWorkbench(workbench_id)
        observed_starting = host.workbench_snapshot()
        if type(observed_starting) is not dict:
            raise RuntimeError("starting workbench snapshot is not a plain mapping")
        starting_snapshot = dict(observed_starting)
        heartbeat_timer.start()
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
        deadline_timer.start(remaining_ms)
        run_loop = getattr(loop, "exec", None)
        if run_loop is None:
            run_loop = loop.exec_
        run_loop()
    finally:
        heartbeat_timer.stop()
        deadline_timer.stop()
        try:
            host.deactivate_workbench()
        except BaseException as exc:
            if error is None:
                error = f"deactivate {type(exc).__name__}: {exc}"

    dock_count = len(main_window.findChildren(QtWidgets.QDockWidget, "VibeCADReviewDock"))
    addon_root = Path(host.__file__).resolve(strict=True).parent.parent
    init_gui_source = addon_root / "InitGui.py"
    if not init_gui_source.is_file():
        raise RuntimeError("reviewed InitGui.py source is unavailable")
    result: dict[str, object] = {
        "active_dock_count": active_dock_count,
        "active_snapshot": active_snapshot,
        "activation_dock_status": activation_dock_status,
        "addon_name": "VibeCAD",
        "addon_registered": len(registered_ids) == 1,
        "bootstrap_source": _source(bootstrap),
        "client_connected": (
            active_snapshot is not None
            and type(active_snapshot.get("daemon_id")) is str
            and bool(active_snapshot["daemon_id"])
        ),
        "deactivation_via_workbench": final_snapshot is not None,
        "dock_source": _source(dock),
        "dock_count_after_shutdown": dock_count,
        "error": error,
        "final_snapshot": final_snapshot,
        "harness_heartbeat_count": heartbeat,
        "harness_source": str(Path(__file__).resolve(strict=True)),
        "host_source": _source(host),
        "init_gui_source": str(init_gui_source),
        "initial_snapshot": initial_snapshot,
        "last_snapshot": last_snapshot,
        "gateway_source": _source(gateway),
        "main_thread_id": main_thread_id,
        "modal_detected": modal_detected,
        "qt_binding": PySide.__name__,
        "qt_binding_version": getattr(PySide, "__version__", None),
        "qt_version": QtCore.qVersion(),
        "refresh_command_kinds": refresh_command_kinds,
        "refresh_event_delta": refresh_event_delta,
        "refresh_heartbeat_delta": refresh_heartbeat_delta,
        "refresh_snapshot": refresh_snapshot,
        "refresh_triggered": refresh_triggered,
        "starting_snapshot": starting_snapshot,
        "status": "ok" if error is None and final_snapshot is not None else "error",
        "stopping_snapshot": stopping_snapshot,
        "sys_executable": str(Path(sys.executable).resolve(strict=True)),
        "sys_prefix": str(Path(sys.prefix).resolve(strict=True)),
        "vibecad_source": _source(vibecad_package),
        "vibecad_home": os.environ.get("VIBECAD_HOME"),
        "workbench_count": len(registered_ids),
        "workbench_ids": registered_ids,
        "workbench_activated": active_snapshot is not None,
    }
    return result


class GuiHarnessTest(unittest.TestCase):
    """One FreeCAD-discoverable test with one canonical result record."""

    def test_gui_harness(self) -> None:
        try:
            result = _run_nested_gui_probe()
        except BaseException as exc:
            result = {
                "error": f"{type(exc).__name__}: {exc}",
                "status": "error",
                "traceback_tail": traceback.format_exc()[-2_000:],
            }
        print(_RESULT_PREFIX + _canonical_json(result), flush=True)
        self.assertEqual(result.get("status"), "ok", result)
