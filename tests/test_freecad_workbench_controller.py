from __future__ import annotations

import ast
import importlib
import importlib.util
import queue
import runpy
import sys
import threading
from collections.abc import Callable
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

from tests.fixtures.freecad_workbench.fake_host import (
    FakeFreeCADGui,
    FakeLocalAgentClient,
    FakeWorkbench,
    install_fake_pyside,
    make_fake_freecad_gui,
    pump_main_events,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_ROOT = _REPO_ROOT / "freecad" / "VibeCAD"
_INIT_GUI = _ADDON_ROOT / "InitGui.py"
_STATE = _ADDON_ROOT / "vibecad_workbench" / "state.py"
_GATEWAY = _ADDON_ROOT / "vibecad_workbench" / "gateway.py"
_HOST = _ADDON_ROOT / "vibecad_workbench" / "host.py"
_STATE_PUBLIC_NAMES = (
    "ProjectionError",
    "ProjectSummary",
    "ProjectPage",
    "TaskSummary",
    "TaskPage",
    "project_page_from_mapping",
    "task_page_from_mapping",
)
_PROJECT_FIELDS = (
    "project_id",
    "generation",
    "revision_id",
    "manifest_sha256",
)
_TASK_FIELDS = (
    "task_id",
    "project_id",
    "generation",
    "base_revision",
    "reasoning_owner",
    "review_policy",
    "status",
    "next_action",
    "candidate_revision",
    "committed_revision",
    "draft_id",
)


class _DictSubclass(dict[object, object]):
    pass


class _ListSubclass(list[object]):
    pass


class _StrSubclass(str):
    pass


def _execute_init_gui(
    monkeypatch: pytest.MonkeyPatch,
    host: FakeFreeCADGui,
) -> dict[str, object]:
    monkeypatch.setitem(sys.modules, "FreeCADGui", host)
    return runpy.run_path(
        str(_INIT_GUI),
        init_globals={"Workbench": FakeWorkbench},
        run_name="InitGui",
    )


def _load_state_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    module_name = "_vibecad_workbench_state_test"
    spec = importlib.util.spec_from_file_location(module_name, _STATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _imports(path: Path) -> tuple[str, ...]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            imported.append(node.module)
    return tuple(imported)


def _project_record(
    digit: str,
    *,
    generation: object = 0,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": f"project_{digit * 32}",
        "generation": generation,
        "revision_id": f"revision_{digit * 32}",
        "manifest_sha256": digit * 64,
    }


def _project_envelope(
    projects: object | None = None,
    *,
    next_cursor: object = "opaque:project:cursor",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": True,
        "result": {
            "schema_version": 1,
            "projects": (
                [_project_record("0"), _project_record("1", generation=9_007_199_254_740_991)]
                if projects is None
                else projects
            ),
            "next_cursor": next_cursor,
        },
        "error": None,
    }


def _task_record(
    digit: str,
    *,
    generation: object = 0,
    candidate_revision: object = None,
    committed_revision: object = None,
    draft_id: object = None,
) -> dict[str, object]:
    return {
        "task_id": f"task_{digit * 32}",
        "project_id": f"project_{digit * 32}",
        "generation": generation,
        "base_revision": f"revision_{digit * 32}",
        "reasoning_owner": "server",
        "review_policy": "required",
        "status": "active",
        "next_action": "review",
        "candidate_revision": candidate_revision,
        "committed_revision": committed_revision,
        "draft_id": draft_id,
    }


def _task_envelope(
    tasks: object | None = None,
    *,
    next_cursor: object = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": True,
        "result": {
            "tasks": (
                [
                    _task_record("0"),
                    _task_record(
                        "1",
                        generation=9_007_199_254_740_991,
                        candidate_revision="revision_22222222222222222222222222222222",
                        committed_revision="revision_33333333333333333333333333333333",
                        draft_id="draft_44444444444444444444444444444444",
                    ),
                ]
                if tasks is None
                else tasks
            ),
            "next_cursor": next_cursor,
        },
        "error": None,
    }


def _replace(mapping: dict[str, object], **values: object) -> dict[str, object]:
    replaced = mapping.copy()
    replaced.update(values)
    return replaced


def _with_subclass_key(
    mapping: dict[str, object],
    key: str,
) -> dict[str, object]:
    replaced = mapping.copy()
    value = replaced.pop(key)
    replaced[_StrSubclass(key)] = value
    return replaced


def _invalid_public_mappings(case_id: str) -> list[tuple[str, object]]:
    if case_id == "outer-not-plain-dict":
        project = _project_envelope()
        task = _task_envelope()
        return [
            ("project", _DictSubclass(project)),
            ("project", MappingProxyType(project)),
            ("task", _DictSubclass(task)),
            ("task", MappingProxyType(task)),
        ]
    if case_id == "outer-key-set":
        project_extra = _project_envelope()
        project_extra["unknown"] = None
        project_missing = _project_envelope()
        del project_missing["error"]
        task_extra = _task_envelope()
        task_extra["unknown"] = None
        return [
            ("project", project_extra),
            ("project", project_missing),
            ("task", task_extra),
        ]
    if case_id == "outer-envelope-state":
        return [
            ("project", _replace(_project_envelope(), schema_version=True)),
            ("project", _replace(_project_envelope(), schema_version=2)),
            ("project", _replace(_project_envelope(), ok=1)),
            ("task", _replace(_task_envelope(), ok=False)),
            ("task", _replace(_task_envelope(), error={})),
        ]
    if case_id == "result-container-types":
        project_result_subclass = _project_envelope()
        project_result_subclass["result"] = _DictSubclass(project_result_subclass["result"])
        project_list_subclass = _project_envelope()
        project_list_subclass["result"]["projects"] = _ListSubclass(
            project_list_subclass["result"]["projects"]
        )
        project_record_subclass = _project_envelope([_DictSubclass(_project_record("0"))])
        task_tuple = _task_envelope(tuple([_task_record("0")]))
        return [
            ("project", project_result_subclass),
            ("project", project_list_subclass),
            ("project", project_record_subclass),
            ("task", task_tuple),
        ]
    if case_id == "project-record-shape":
        extra = _project_record("0")
        extra["unknown"] = None
        missing = _project_record("0")
        del missing["revision_id"]
        return [
            ("project", _project_envelope([extra])),
            ("project", _project_envelope([missing])),
            ("project", _project_envelope([_project_record("0", generation=True)])),
            ("project", _project_envelope([_project_record("0", generation=-1)])),
            (
                "project",
                _project_envelope([_project_record("0", generation=9_007_199_254_740_992)]),
            ),
            (
                "project",
                _project_envelope([_replace(_project_record("0"), project_id="project_NOT_HEX")]),
            ),
            (
                "project",
                _project_envelope([_replace(_project_record("0"), revision_id="revision_NOT_HEX")]),
            ),
            (
                "project",
                _project_envelope([_replace(_project_record("0"), manifest_sha256="A" * 64)]),
            ),
            (
                "project",
                _project_envelope(
                    [_replace(_project_record("0"), project_id=_StrSubclass("project_" + "0" * 32))]
                ),
            ),
        ]
    if case_id == "project-order-uniqueness":
        first = _project_record("0")
        second = _project_record("1")
        return [
            ("project", _project_envelope([second, first])),
            ("project", _project_envelope([first, first.copy()])),
        ]
    if case_id == "task-record-shape":
        extra = _task_record("0")
        extra["unknown"] = None
        missing = _task_record("0")
        del missing["status"]
        required_empty = [
            _replace(_task_record("0"), **{name: ""})
            for name in ("reasoning_owner", "review_policy", "status", "next_action")
        ]
        return [
            ("task", _task_envelope([extra])),
            ("task", _task_envelope([missing])),
            ("task", _task_envelope([_task_record("0", generation=True)])),
            (
                "task",
                _task_envelope([_replace(_task_record("0"), task_id="task_NOT_HEX")]),
            ),
            (
                "task",
                _task_envelope([_replace(_task_record("0"), project_id="project_NOT_HEX")]),
            ),
            (
                "task",
                _task_envelope([_replace(_task_record("0"), base_revision="revision_NOT_HEX")]),
            ),
            (
                "task",
                _task_envelope([_task_record("0", candidate_revision="revision_NOT_HEX")]),
            ),
            (
                "task",
                _task_envelope([_task_record("0", committed_revision="revision_NOT_HEX")]),
            ),
            ("task", _task_envelope([_task_record("0", draft_id="draft_NOT_HEX")])),
            *(("task", _task_envelope([record])) for record in required_empty),
        ]
    if case_id == "task-order-cursor":
        first = _task_record("0")
        second = _task_record("1")
        return [
            ("task", _task_envelope([second, first])),
            ("task", _task_envelope([first, first.copy()])),
            ("task", _task_envelope(next_cursor="")),
            ("task", _task_envelope(next_cursor=17)),
            ("project", _project_envelope(next_cursor=_StrSubclass("opaque"))),
        ]
    raise AssertionError(f"unknown malformed case {case_id}")


def test_init_gui_registers_exactly_one_workbench_across_reexecution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = make_fake_freecad_gui()
    host._vibecad_workbench_instance = None

    first = _execute_init_gui(monkeypatch, host)
    second = _execute_init_gui(monkeypatch, host)

    assert len(host.added_workbenches) == 1
    instance = host.added_workbenches[0]
    assert host._vibecad_workbench_instance is instance
    assert type(instance).__name__ == "VibeCADWorkbench"
    assert isinstance(instance, FakeWorkbench)
    assert instance.MenuText == "VibeCAD"
    assert instance.ToolTip == "VibeCAD thin client"
    assert instance.GetClassName() == "Gui::PythonWorkbench"
    assert instance.Initialize() is None
    assert first["FreeCADGui"] is host
    assert second["FreeCADGui"] is host


def test_init_gui_retries_after_add_workbench_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = make_fake_freecad_gui(fail_first_add=True)

    with pytest.raises(RuntimeError, match="synthetic addWorkbench failure"):
        _execute_init_gui(monkeypatch, host)

    assert getattr(host, "_vibecad_workbench_instance", None) is None
    assert host.added_workbenches == []
    assert host.add_attempts == 1

    _execute_init_gui(monkeypatch, host)
    _execute_init_gui(monkeypatch, host)

    assert host.add_attempts == 2
    assert len(host.added_workbenches) == 1
    assert host._vibecad_workbench_instance is host.added_workbenches[0]


def test_init_gui_import_boundary_excludes_daemon_qt_and_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _imports(_INIT_GUI) == ("FreeCADGui",)
    module = ast.parse(_INIT_GUI.read_text(encoding="utf-8"), filename=str(_INIT_GUI))
    workbench = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "VibeCADWorkbench"
    )
    assert [ast.unparse(base) for base in workbench.bases] == ["Workbench"]
    assignments = {
        target.id: node.value.value
        for node in workbench.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert assignments["ToolTip"] == "VibeCAD thin client"

    host = make_fake_freecad_gui()
    before = set(sys.modules)
    _execute_init_gui(monkeypatch, host)
    added = set(sys.modules) - before
    forbidden = ("vibecad.daemon", "vibecad.store", "PySide", "PySide2", "PySide6")
    assert not any(name.startswith(forbidden) for name in added)


def test_state_module_import_boundary_excludes_freecad_qt_daemon_and_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(_imports(_STATE)) == {"dataclasses", "re"}

    before = set(sys.modules)
    state = _load_state_module(monkeypatch)
    added = set(sys.modules) - before
    forbidden = (
        "FreeCAD",
        "FreeCADGui",
        "PySide",
        "PySide2",
        "PySide6",
        "vibecad.daemon",
        "vibecad.store",
    )
    assert not any(name.startswith(forbidden) for name in added)
    assert state.__all__ == _STATE_PUBLIC_NAMES
    for name, expected_fields in (
        ("ProjectSummary", _PROJECT_FIELDS),
        ("ProjectPage", ("projects", "next_cursor")),
        ("TaskSummary", _TASK_FIELDS),
        ("TaskPage", ("tasks", "next_cursor")),
    ):
        data_type = getattr(state, name)
        assert is_dataclass(data_type)
        assert data_type.__dataclass_params__.frozen
        assert tuple(field.name for field in fields(data_type)) == expected_fields
        assert all(field.default is MISSING for field in fields(data_type))
        assert "__slots__" in vars(data_type)


def test_project_page_from_mapping_projects_and_detaches_public_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _load_state_module(monkeypatch)
    response = _project_envelope()

    page = state.project_page_from_mapping(response)

    assert page == state.ProjectPage(
        projects=(
            state.ProjectSummary(
                project_id="project_" + "0" * 32,
                generation=0,
                revision_id="revision_" + "0" * 32,
                manifest_sha256="0" * 64,
            ),
            state.ProjectSummary(
                project_id="project_" + "1" * 32,
                generation=9_007_199_254_740_991,
                revision_id="revision_" + "1" * 32,
                manifest_sha256="1" * 64,
            ),
        ),
        next_cursor="opaque:project:cursor",
    )
    response["result"]["projects"][0]["project_id"] = "project_" + "f" * 32
    response["result"]["projects"].clear()
    response["result"]["next_cursor"] = "changed"
    assert page.projects[0].project_id == "project_" + "0" * 32
    assert len(page.projects) == 2
    assert page.next_cursor == "opaque:project:cursor"
    assert not hasattr(page, "__dict__")


def test_task_page_from_mapping_projects_and_detaches_public_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _load_state_module(monkeypatch)
    response = _task_envelope(next_cursor="opaque:task:cursor")

    page = state.task_page_from_mapping(response)

    assert page == state.TaskPage(
        tasks=(
            state.TaskSummary(
                task_id="task_" + "0" * 32,
                project_id="project_" + "0" * 32,
                generation=0,
                base_revision="revision_" + "0" * 32,
                reasoning_owner="server",
                review_policy="required",
                status="active",
                next_action="review",
                candidate_revision=None,
                committed_revision=None,
                draft_id=None,
            ),
            state.TaskSummary(
                task_id="task_" + "1" * 32,
                project_id="project_" + "1" * 32,
                generation=9_007_199_254_740_991,
                base_revision="revision_" + "1" * 32,
                reasoning_owner="server",
                review_policy="required",
                status="active",
                next_action="review",
                candidate_revision="revision_" + "2" * 32,
                committed_revision="revision_" + "3" * 32,
                draft_id="draft_" + "4" * 32,
            ),
        ),
        next_cursor="opaque:task:cursor",
    )
    response["result"]["tasks"][1]["draft_id"] = None
    response["result"]["tasks"].clear()
    response["result"]["next_cursor"] = None
    assert page.tasks[1].draft_id == "draft_" + "4" * 32
    assert len(page.tasks) == 2
    assert page.next_cursor == "opaque:task:cursor"
    assert not hasattr(page, "__dict__")


@pytest.mark.parametrize(
    "case_id",
    (
        "outer-not-plain-dict",
        "outer-key-set",
        "outer-envelope-state",
        "result-container-types",
        "project-record-shape",
        "project-order-uniqueness",
        "task-record-shape",
        "task-order-cursor",
    ),
)
def test_projection_rejects_malformed_public_mappings(
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
) -> None:
    state = _load_state_module(monkeypatch)
    project: Callable[[object], object] = state.project_page_from_mapping
    task: Callable[[object], object] = state.task_page_from_mapping

    for projection, mapping in _invalid_public_mappings(case_id):
        function = project if projection == "project" else task
        with pytest.raises(state.ProjectionError, match=r"^invalid public mapping$"):
            function(mapping)


def test_projection_rejects_str_subclass_keys_at_each_dict_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _load_state_module(monkeypatch)
    project_record = _with_subclass_key(_project_record("0"), "project_id")
    project_result = _project_envelope()
    project_result["result"] = _with_subclass_key(
        project_result["result"],
        "projects",
    )
    task_record = _with_subclass_key(_task_record("0"), "task_id")
    cases = (
        (
            state.project_page_from_mapping,
            _with_subclass_key(_project_envelope(), "schema_version"),
        ),
        (state.project_page_from_mapping, project_result),
        (state.project_page_from_mapping, _project_envelope([project_record])),
        (state.task_page_from_mapping, _task_envelope([task_record])),
    )

    for projection, mapping in cases:
        with pytest.raises(state.ProjectionError, match=r"^invalid public mapping$"):
            projection(mapping)


def test_projection_error_is_stable_and_does_not_echo_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _load_state_module(monkeypatch)
    secret = "private-marker-that-must-not-be-echoed"
    mapping = _project_envelope([_replace(_project_record("0"), project_id=secret)])

    with pytest.raises(state.ProjectionError) as raised:
        state.project_page_from_mapping(mapping)

    assert str(raised.value) == "invalid public mapping"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def _load_workbench_module(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> ModuleType:
    monkeypatch.syspath_prepend(str(_ADDON_ROOT))
    qualified = f"vibecad_workbench.{name}"
    monkeypatch.delitem(sys.modules, qualified, raising=False)
    return importlib.import_module(qualified)


def test_gateway_client_lifecycle_is_owned_by_one_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []
    responses: queue.Queue[dict[str, object]] = queue.Queue()

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    commands = (
        {"schema_version": 1, "kind": "connect", "request_id": 0},
        {
            "schema_version": 1,
            "kind": "list_projects",
            "request_id": 1,
            "cursor": None,
        },
        {"schema_version": 1, "kind": "close", "request_id": 2},
    )

    def worker() -> None:
        gateway = gateway_module.KernelGateway(make_client)
        for command in commands:
            responses.put(gateway.handle(command))

    main_thread_id = threading.get_ident()
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()

    events = [responses.get_nowait() for _ in commands]
    assert [event["kind"] for event in events] == [
        "connected",
        "projects",
        "closed",
    ]
    assert len(clients) == 1
    client = clients[0]
    operation_threads = [thread_id for _, _, thread_id in client.calls]
    assert operation_threads
    assert len(set(operation_threads)) == 1
    assert client.created_thread_id == operation_threads[0] == client.closed_thread_id
    assert client.created_thread_id != main_thread_id
    assert client.calls[1][:2] == (
        "list_projects",
        {"schema_version": 1, "limit": 50, "cursor": None},
    )


def test_workbench_activation_creates_one_dock_without_blocking_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(
        gateway_module,
        "KernelGateway",
        lambda: original_gateway(make_client),
    )
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")
    main_thread_id = threading.get_ident()
    assert host.workbench_snapshot() == {
        "schema_version": 1,
        "lifecycle": "inactive",
        "dock_count": 0,
        "main_thread_id": main_thread_id,
        "worker_thread_id": None,
        "daemon_id": None,
        "heartbeat_count": 0,
        "client_construction_count": 0,
    }

    host.activate_workbench()

    assert len(freecad_gui.main_window.docks) == 1
    assert host.workbench_snapshot()["lifecycle"] in {"starting", "active"}
    dock = freecad_gui.main_window.docks[0]
    assert dock.object_name == "VibeCADReviewDock"
    assert not hasattr(dock, "accept_button")
    assert not hasattr(dock, "reject_button")
    host.activate_workbench()
    assert len(freecad_gui.main_window.docks) == 1
    pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] == 1)
    assert len(clients) == 1
    client = clients[0]
    active = host.workbench_snapshot()
    assert active == {
        "schema_version": 1,
        "lifecycle": "active",
        "dock_count": 1,
        "main_thread_id": main_thread_id,
        "worker_thread_id": client.created_thread_id,
        "daemon_id": client.daemon_id,
        "heartbeat_count": 1,
        "client_construction_count": 1,
    }
    assert freecad_gui.main_window.children() == [dock]
    assert freecad_gui.main_window.findChildren(
        fake_pyside.QtWidgets.QDockWidget,
        "VibeCADReviewDock",
    ) == [dock]
    assert dock.parent() is freecad_gui.main_window
    assert client.created_thread_id != main_thread_id

    session = host._session
    assert session is not None
    delete_later_calls = 0
    original_delete_later = dock.deleteLater

    def delete_later() -> None:
        nonlocal delete_later_calls
        delete_later_calls += 1
        original_delete_later()

    monkeypatch.setattr(dock, "deleteLater", delete_later)
    host.deactivate_workbench()
    host.deactivate_workbench()
    assert host.workbench_snapshot()["lifecycle"] == "stopping"
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
    assert freecad_gui.main_window.docks == []
    assert freecad_gui.main_window.children() == []
    assert (
        freecad_gui.main_window.findChildren(
            fake_pyside.QtWidgets.QDockWidget,
            "VibeCADReviewDock",
        )
        == []
    )
    assert dock.parent() is None
    assert dock.hidden is True
    assert dock.delete_scheduled is True
    assert dock.deleted is False
    assert delete_later_calls == 1
    session._finished()
    session._finished()
    assert delete_later_calls == 1
    assert client.close_call_count == 1
    assert client.closed_thread_id == client.created_thread_id
    final = host.workbench_snapshot()
    assert final["worker_thread_id"] is None
    assert final["daemon_id"] is None
    assert final["client_construction_count"] == 1


def test_c01_host_activates_with_nested_only_pyside6_enums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside(nested_only=True)
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(
        gateway_module,
        "KernelGateway",
        lambda: original_gateway(make_client),
    )
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")

    host.activate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "active")

    assert len(freecad_gui.main_window.docks) == 1
    assert len(clients) == 1
    assert host.workbench_snapshot()["daemon_id"] == clients[0].daemon_id
    host.deactivate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
    assert freecad_gui.main_window.docks == []
    assert clients[0].close_call_count == 1


def test_c01_host_activates_with_flat_only_qt5_enums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside(flat_only=True)
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(
        gateway_module,
        "KernelGateway",
        lambda: original_gateway(make_client),
    )
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")

    host.activate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "active")

    assert len(freecad_gui.main_window.docks) == 1
    assert len(clients) == 1
    assert host.workbench_snapshot()["daemon_id"] == clients[0].daemon_id
    host.deactivate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
    assert freecad_gui.main_window.docks == []
    assert clients[0].close_call_count == 1


def test_gateway_exact_commands_detach_filter_and_review_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    project_id = "project_" + "1" * 32
    task_id = "task_" + "1" * 32
    draft_id = "draft_" + "5" * 32

    assert (
        gateway.handle({"schema_version": 1, "kind": "connect", "request_id": 0})["kind"]
        == "connected"
    )
    tasks = gateway.handle(
        {
            "schema_version": 1,
            "kind": "list_tasks",
            "request_id": 1,
            "cursor": "opaque",
        }
    )
    assert tasks["kind"] == "tasks"
    assert len(tasks["response"]["result"]["tasks"]) == 2
    assert client.calls[-1][:2] == (
        "list_tasks",
        {"schema_version": 1, "limit": 50, "cursor": "opaque"},
    )
    assert client.last_tasks_response is not None
    client.last_tasks_response["result"]["tasks"].clear()
    assert len(tasks["response"]["result"]["tasks"]) == 2

    assert (
        gateway.handle(
            {
                "schema_version": 1,
                "kind": "refresh_project",
                "request_id": 2,
                "project_id": project_id,
            }
        )["kind"]
        == "project"
    )
    assert client.calls[-1][:2] == (
        "get_project",
        {"schema_version": 1, "project_id": project_id},
    )
    assert (
        gateway.handle(
            {
                "schema_version": 1,
                "kind": "refresh_task",
                "request_id": 3,
                "task_id": task_id,
            }
        )["kind"]
        == "task"
    )
    assert client.calls[-1][:2] == (
        "get_task",
        {"schema_version": 1, "task_id": task_id},
    )
    reject = gateway.handle(
        {
            "schema_version": 1,
            "kind": "review",
            "request_id": 4,
            "decision": "reject",
            "task_id": task_id,
            "draft_id": draft_id,
            "expected_generation": 3,
        }
    )
    assert reject["kind"] == "review"
    assert [name for name, _, _ in client.calls].count("reject_draft") == 1

    client.review_failure = True
    review = {
        "schema_version": 1,
        "kind": "review",
        "request_id": 5,
        "decision": "accept",
        "task_id": task_id,
        "draft_id": draft_id,
        "expected_generation": 3,
    }
    event = gateway.handle(review)
    assert event == {
        "schema_version": 1,
        "request_id": 5,
        "kind": "error",
        "operation": "review",
        "code": "closed",
        "outcome": "unknown_outcome",
    }
    assert [name for name, _, _ in client.calls].count("accept_draft") == 1
    assert client.calls[-1][1] == {
        "schema_version": 1,
        "task_id": task_id,
        "draft_id": draft_id,
        "expected_generation": 3,
    }
    assert client.closed_thread_id == threading.get_ident()


def test_gateway_rejects_nonplain_or_open_commands_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    opened = 0

    def make_client() -> FakeLocalAgentClient:
        nonlocal opened
        opened += 1
        return FakeLocalAgentClient()

    gateway = gateway_module.KernelGateway(make_client)
    invalid = (
        {"schema_version": 1, "kind": "unknown", "request_id": 0},
        {"schema_version": 1, "kind": "connect", "request_id": 0, "extra": None},
        {"schema_version": True, "kind": "connect", "request_id": 0},
        _DictSubclass({"schema_version": 1, "kind": "connect", "request_id": 0}),
    )
    for command in invalid:
        assert gateway.handle(command) == {
            "schema_version": 1,
            "request_id": (
                0
                if type(command) is dict
                and command.get("kind") == "connect"
                and type(command.get("request_id")) is int
                else -1
            ),
            "kind": "error",
            "operation": (
                "connect"
                if type(command) is dict
                and command.get("kind") == "connect"
                and type(command.get("request_id")) is int
                else "invalid"
            ),
            "code": "invalid_input",
            "outcome": "known_failure",
        }
    assert opened == 0


def test_gateway_rejects_cross_thread_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "kind": "connect", "request_id": 0})
    failures: queue.Queue[BaseException] = queue.Queue()

    def cross_thread_call() -> None:
        try:
            gateway.handle(
                {
                    "schema_version": 1,
                    "kind": "list_projects",
                    "request_id": 1,
                    "cursor": None,
                }
            )
        except BaseException as error:
            failures.put(error)

    thread = threading.Thread(target=cross_thread_call)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert str(failures.get_nowait()) == "gateway thread authority violation"
    assert [name for name, _, _ in client.calls] == ["ping"]


def test_review_dock_discards_stale_project_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    commands: list[dict[str, object]] = []
    dock.request.connect(commands.append)

    dock.start()
    dock.handle_event(
        {
            "schema_version": 1,
            "kind": "connected",
            "request_id": commands[-1]["request_id"],
            "daemon_id": "daemon_" + "a" * 32,
            "worker_thread_id": 42,
        }
    )
    stale_request_id = commands[-1]["request_id"]
    dock.refresh()
    current_request_id = commands[-1]["request_id"]
    assert stale_request_id != current_request_id
    response = FakeLocalAgentClient().list_projects_request(
        {"schema_version": 1, "limit": 50, "cursor": None}
    )

    dock.handle_event(
        {
            "schema_version": 1,
            "kind": "projects",
            "request_id": stale_request_id,
            "response": response,
        }
    )
    assert dock.project_selector.items == []
    dock.handle_event(
        {
            "schema_version": 1,
            "kind": "projects",
            "request_id": current_request_id,
            "response": response,
        }
    )
    assert dock.project_selector.items == ["project_" + "1" * 32]


def test_c01_gateway_uses_exact_kind_integer_protocol_and_error_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)

    connected = gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    assert connected == {
        "schema_version": 1,
        "request_id": 0,
        "kind": "connected",
        "daemon_id": client.daemon_id,
        "worker_thread_id": threading.get_ident(),
    }
    tasks = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 1,
            "kind": "list_tasks",
            "cursor": None,
        }
    )
    assert set(tasks) == {"schema_version", "request_id", "kind", "response"}
    assert tasks["kind"] == "tasks"
    assert len(tasks["response"]["result"]["tasks"]) == 2
    assert client.calls[-1][:2] == (
        "list_tasks",
        {"schema_version": 1, "limit": 50, "cursor": None},
    )

    malformed = gateway.handle({"schema_version": 1, "request_id": 2, "kind": "list_tasks"})
    assert malformed == {
        "schema_version": 1,
        "request_id": 2,
        "kind": "error",
        "operation": "list_tasks",
        "code": "invalid_input",
        "outcome": "known_failure",
    }
    invalid = gateway.handle({"schema_version": True, "request_id": True, "kind": "connect"})
    assert invalid == {
        "schema_version": 1,
        "request_id": -1,
        "kind": "error",
        "operation": "invalid",
        "code": "invalid_input",
        "outcome": "known_failure",
    }


def test_c01_gateway_review_exception_is_one_call_unknown_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    client.review_failure = True
    task_id = "task_" + "1" * 32
    draft_id = "draft_" + "5" * 32

    event = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 7,
            "kind": "review",
            "decision": "accept",
            "task_id": task_id,
            "draft_id": draft_id,
            "expected_generation": 3,
        }
    )

    assert event == {
        "schema_version": 1,
        "request_id": 7,
        "kind": "error",
        "operation": "review",
        "code": "closed",
        "outcome": "unknown_outcome",
    }
    assert [name for name, _, _ in client.calls].count("accept_draft") == 1
    assert client.closed_thread_id == threading.get_ident()


def test_c01_gateway_detach_rejects_oversized_values_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    client.projects_response = {
        "schema_version": 1,
        "ok": True,
        "result": {"schema_version": 1, "projects": [], "next_cursor": "x" * 20_000},
        "error": None,
    }

    event = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 9,
            "kind": "list_projects",
            "cursor": None,
        }
    )

    assert event == {
        "schema_version": 1,
        "request_id": 9,
        "kind": "error",
        "operation": "list_projects",
        "code": "internal_error",
        "outcome": "known_failure",
    }
    assert "x" * 20 not in repr(event)


def test_c01_gateway_bounds_every_detached_mapping_dimension_and_maps_closed_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})

    deep: object = None
    for _ in range(10):
        deep = {"level": deep}
    oversized = (
        {"wide": [None] * 1001},
        {"wide": {str(index): None for index in range(1001)}},
        {"k" * 129: None},
        {"integer": 9_007_199_254_740_992},
        {"deep": deep},
    )
    for request_id, result in enumerate(oversized, 1):
        client.projects_response = {
            "schema_version": 1,
            "ok": True,
            "result": result,
            "error": None,
        }
        event = gateway.handle(
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "list_projects",
                "cursor": None,
            }
        )
        assert event["code"] == "internal_error"
        assert event["outcome"] == "known_failure"

    class _AllowedCode(Enum):
        UNAVAILABLE = "unavailable"

    class _KnownFailure(RuntimeError):
        code = _AllowedCode.UNAVAILABLE

    def fail_known(_request: dict[str, object]) -> dict[str, object]:
        raise _KnownFailure

    client.list_projects_request = fail_known
    known = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 8,
            "kind": "list_projects",
            "cursor": None,
        }
    )
    assert known["code"] == "unavailable"
    assert known["outcome"] == "known_failure"


def test_c01_gateway_keeps_authenticated_failure_as_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    authenticated_failure = {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {"code": "conflict"},
    }
    client.projects_response = authenticated_failure

    event = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 1,
            "kind": "list_projects",
            "cursor": None,
        }
    )

    assert event == {
        "schema_version": 1,
        "request_id": 1,
        "kind": "projects",
        "response": authenticated_failure,
    }


def test_c01_dock_exact_signal_filter_context_and_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    commands: list[dict[str, object]] = []
    dock.request.connect(commands.append)

    dock.start()
    assert commands[-1] == {"schema_version": 1, "request_id": 0, "kind": "connect"}
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": 0,
            "kind": "connected",
            "daemon_id": "daemon_" + "a" * 32,
            "worker_thread_id": 42,
        }
    )
    assert commands[-1]["kind"] == "list_projects"
    projects_id = commands[-1]["request_id"]
    response = FakeLocalAgentClient().list_projects_request(
        {"schema_version": 1, "limit": 50, "cursor": None}
    )
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": projects_id,
            "kind": "projects",
            "response": response,
        }
    )
    assert dock.project_selector.items == ["project_" + "1" * 32]
    assert [command["kind"] for command in commands].count("list_tasks") == 1
    tasks_id = commands[-1]["request_id"]
    assert commands[-1] == {
        "schema_version": 1,
        "request_id": tasks_id,
        "kind": "list_tasks",
        "cursor": None,
    }
    tasks_response = FakeLocalAgentClient().list_tasks_request(
        {"schema_version": 1, "limit": 50, "cursor": None}
    )
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": tasks_id,
            "kind": "tasks",
            "response": tasks_response,
        }
    )
    assert dock.task_selector.items == ["task_" + "1" * 32]

    before = len(commands)
    dock.refresh()
    refreshed = commands[before:]
    assert [command["kind"] for command in refreshed] == [
        "refresh_project",
        "refresh_task",
        "list_tasks",
    ]
    assert all(type(command["request_id"]) is int for command in refreshed)

    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": refreshed[-1]["request_id"],
            "kind": "tasks",
            "response": {"schema_version": 1, "ok": False, "result": None, "error": {}},
        }
    )
    assert dock.status_label.text == "Unavailable"


def test_c01_dock_rejects_superseded_same_selection_task_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    commands: list[dict[str, object]] = []
    dock.request.connect(commands.append)

    dock.start()
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": commands[-1]["request_id"],
            "kind": "connected",
            "daemon_id": "daemon_" + "a" * 32,
            "worker_thread_id": 42,
        }
    )
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": commands[-1]["request_id"],
            "kind": "projects",
            "response": _project_envelope([_project_record("0")], next_cursor=None),
        }
    )
    first_page_id = commands[-1]["request_id"]
    first_task = _replace(
        _task_record("0"),
        status="awaiting_user_review",
    )
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": first_page_id,
            "kind": "tasks",
            "response": _task_envelope([first_task], next_cursor="old-page-2"),
        }
    )
    old_continuation_id = commands[-1]["request_id"]

    dock.refresh()
    current_page_id = commands[-1]["request_id"]
    assert current_page_id != old_continuation_id
    command_count = len(commands)
    stale_task = _replace(
        _task_record("1"),
        project_id="project_" + "0" * 32,
        status="awaiting_user_review",
    )
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": old_continuation_id,
            "kind": "tasks",
            "response": _task_envelope([stale_task], next_cursor="old-page-3"),
        }
    )
    assert len(commands) == command_count
    assert dock.task_selector.items == []

    current_task = _replace(
        _task_record("2"),
        project_id="project_" + "0" * 32,
        status="awaiting_user_review",
    )
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": current_page_id,
            "kind": "tasks",
            "response": _task_envelope([current_task]),
        }
    )
    assert dock.task_selector.items == ["task_" + "2" * 32]


def test_c01_dock_rejects_selection_race_and_filters_after_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    commands: list[dict[str, object]] = []
    dock.request.connect(commands.append)
    dock.start()
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": commands[-1]["request_id"],
            "kind": "connected",
            "daemon_id": "daemon_" + "a" * 32,
            "worker_thread_id": 42,
        }
    )
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": commands[-1]["request_id"],
            "kind": "projects",
            "response": _project_envelope(
                [_project_record("0"), _project_record("1")],
                next_cursor=None,
            ),
        }
    )
    stale_id = commands[-1]["request_id"]
    dock.project_selector.setCurrentIndex(1)
    current_id = commands[-1]["request_id"]
    assert stale_id != current_id
    task_zero = _replace(
        _task_record("0"),
        status="awaiting_user_review",
    )
    task_one = _replace(
        _task_record("1"),
        status="awaiting_user_review",
    )
    response = _task_envelope([task_zero, task_one])

    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": stale_id,
            "kind": "tasks",
            "response": response,
        }
    )
    assert dock.task_selector.items == []
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": current_id,
            "kind": "tasks",
            "response": response,
        }
    )
    assert dock.task_selector.items == ["task_" + "1" * 32]

    dock.handle_event(
        {
            "schema_version": True,
            "request_id": current_id,
            "kind": "tasks",
            "response": response,
        }
    )
    assert dock.status_label.text == "Unavailable"


def test_c01_fake_host_enforces_both_thread_authorities() -> None:
    fake_pyside = install_fake_pyside()
    label = fake_pyside.QtWidgets.QLabel("ready", None)
    client = FakeLocalAgentClient()
    failures: queue.Queue[str] = queue.Queue()

    def violate() -> None:
        for action in (lambda: label.setText("bad"), client.ping):
            try:
                action()
            except RuntimeError as error:
                failures.put(str(error))

    thread = threading.Thread(target=violate)
    thread.start()
    thread.join(1)

    assert not thread.is_alive()
    assert failures.get_nowait() == "fake widget thread authority violation"
    assert failures.get_nowait() == "fake client thread authority violation"


def test_c01_fake_host_separates_dock_layout_from_qobject_ownership() -> None:
    fake_pyside = install_fake_pyside()
    main_window = make_fake_freecad_gui().main_window
    dock = fake_pyside.QtWidgets.QDockWidget("Review", main_window)
    dock.setObjectName("VibeCADReviewDock")

    main_window.addDockWidget(object(), dock)
    main_window.removeDockWidget(dock)

    assert main_window.docks == []
    assert main_window.children() == [dock]
    assert main_window.findChildren(
        fake_pyside.QtWidgets.QDockWidget,
        "VibeCADReviewDock",
    ) == [dock]
    assert dock.parent() is main_window
    dock.deleteLater()
    assert dock.delete_scheduled is True
    assert dock.deleted is False

    dock.setParent(None)

    assert main_window.children() == []
    assert main_window.findChildren(fake_pyside.QtWidgets.QDockWidget) == []
    assert dock.parent() is None


def test_c01_host_partial_constructor_failure_unwinds_without_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    dock_module = _load_workbench_module(monkeypatch, "dock")

    def fail_dock(_parent: object) -> object:
        raise RuntimeError("synthetic dock construction failure")

    monkeypatch.setattr(dock_module, "ReviewDock", fail_dock)
    host = _load_workbench_module(monkeypatch, "host")

    host.activate_workbench()

    snapshot = host.workbench_snapshot()
    assert snapshot["lifecycle"] == "inactive"
    assert snapshot["dock_count"] == 0
    assert snapshot["worker_thread_id"] is None
    assert snapshot["daemon_id"] is None
    assert snapshot["client_construction_count"] == 0
    assert freecad_gui.main_window.docks == []
    assert "wait" not in _HOST.read_text(encoding="utf-8")


def test_c01_host_add_dock_failure_detaches_constructed_dock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui(fail_first_dock_add=True)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    constructed_docks: list[object] = []
    original_dock = dock_module.ReviewDock

    def capture_dock(parent: object) -> object:
        dock = original_dock(parent)
        constructed_docks.append(dock)
        return dock

    monkeypatch.setattr(dock_module, "ReviewDock", capture_dock)
    host = _load_workbench_module(monkeypatch, "host")

    host.activate_workbench()

    assert freecad_gui.main_window.add_attempts == 1
    assert len(constructed_docks) == 1
    dock = constructed_docks[0]
    assert host.workbench_snapshot()["lifecycle"] == "inactive"
    assert host.workbench_snapshot()["dock_count"] == 0
    assert freecad_gui.main_window.docks == []
    assert freecad_gui.main_window.children() == []
    assert (
        freecad_gui.main_window.findChildren(
            fake_pyside.QtWidgets.QDockWidget,
            "VibeCADReviewDock",
        )
        == []
    )
    assert dock.parent() is None
    assert dock.hidden is True
    assert dock.delete_scheduled is True
    assert dock.deleted is False
    assert host._session is None


def test_c01_host_unstarted_residual_blocks_reactivation_until_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui(fail_first_dock_add=True)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(
        gateway_module,
        "KernelGateway",
        lambda: original_gateway(make_client),
    )
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    constructed_docks: list[object] = []
    original_dock = dock_module.ReviewDock
    detach_fails = True
    residual_delete_later_calls = 0

    def capture_dock(parent: object) -> object:
        nonlocal residual_delete_later_calls
        dock = original_dock(parent)
        constructed_docks.append(dock)
        if len(constructed_docks) == 1:
            original_set_parent = dock.setParent
            original_delete_later = dock.deleteLater

            def set_parent(target: object | None) -> None:
                if target is None and detach_fails:
                    raise RuntimeError("synthetic persistent setParent failure")
                original_set_parent(target)

            def delete_later() -> None:
                nonlocal residual_delete_later_calls
                residual_delete_later_calls += 1
                original_delete_later()

            monkeypatch.setattr(dock, "setParent", set_parent)
            monkeypatch.setattr(dock, "deleteLater", delete_later)
        return dock

    monkeypatch.setattr(dock_module, "ReviewDock", capture_dock)
    host = _load_workbench_module(monkeypatch, "host")

    host.activate_workbench()

    residual_session = host._session
    assert residual_session is not None
    residual_dock = constructed_docks[0]
    residual_thread = residual_session.thread
    assert residual_thread is not None
    assert not residual_thread.isRunning()
    assert host.workbench_snapshot()["lifecycle"] == "stopping"
    assert host.workbench_snapshot()["dock_count"] == 1
    assert freecad_gui.main_window.add_attempts == 1
    assert freecad_gui.main_window.docks == []
    assert freecad_gui.main_window.children() == [residual_dock]
    assert freecad_gui.main_window.findChildren(
        fake_pyside.QtWidgets.QDockWidget,
        "VibeCADReviewDock",
    ) == [residual_dock]
    assert clients == []
    assert residual_delete_later_calls == 0
    assert residual_dock.delete_scheduled is False

    host.activate_workbench()

    assert host._session is residual_session
    assert constructed_docks == [residual_dock]
    assert freecad_gui.main_window.add_attempts == 1
    assert host.workbench_snapshot()["lifecycle"] == "stopping"
    assert host.workbench_snapshot()["dock_count"] == 1
    assert clients == []
    assert residual_delete_later_calls == 0
    assert residual_dock.delete_scheduled is False

    host.activate_workbench()

    assert host._session is residual_session
    assert constructed_docks == [residual_dock]
    assert freecad_gui.main_window.add_attempts == 1
    assert host.workbench_snapshot()["lifecycle"] == "stopping"
    assert host.workbench_snapshot()["dock_count"] == 1
    assert clients == []
    assert residual_delete_later_calls == 0
    assert residual_dock.delete_scheduled is False

    detach_fails = False
    host.activate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "active")

    assert host._session is not residual_session
    assert len(constructed_docks) == 2
    replacement_dock = constructed_docks[1]
    assert freecad_gui.main_window.add_attempts == 2
    assert freecad_gui.main_window.docks == [replacement_dock]
    assert freecad_gui.main_window.children() == [replacement_dock]
    assert freecad_gui.main_window.findChildren(
        fake_pyside.QtWidgets.QDockWidget,
        "VibeCADReviewDock",
    ) == [replacement_dock]
    assert residual_dock.parent() is None
    assert residual_delete_later_calls == 1
    assert residual_dock.delete_scheduled is True
    assert len(clients) == 1
    assert clients[0].created_thread_id != threading.get_ident()

    host.deactivate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
    assert clients[0].close_call_count == 1
    assert freecad_gui.main_window.docks == []
    assert freecad_gui.main_window.children() == []


def test_c01_host_failure_after_connect_enqueue_closes_on_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(
        gateway_module,
        "KernelGateway",
        lambda: original_gateway(make_client),
    )
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    original_start = dock_module.ReviewDock.start
    started_docks: list[object] = []

    def fail_after_connect(self: object) -> None:
        started_docks.append(self)
        original_start(self)
        raise RuntimeError("synthetic post-connect Dock.start failure")

    monkeypatch.setattr(dock_module.ReviewDock, "start", fail_after_connect)
    host = _load_workbench_module(monkeypatch, "host")

    host.activate_workbench()

    session = host._session
    assert session is not None
    assert len(started_docks) == 1
    dock = started_docks[0]
    thread = session.thread
    assert host.workbench_snapshot()["lifecycle"] == "stopping"
    assert freecad_gui.main_window.docks == []
    assert freecad_gui.main_window.children() == []
    assert dock.parent() is None
    assert dock.hidden is True
    assert dock.delete_scheduled is False
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

    assert len(clients) == 1
    client = clients[0]
    assert client.close_call_count == 1
    assert client.closed_thread_id == client.created_thread_id
    assert client.created_thread_id != threading.get_ident()
    assert thread is not None
    assert not thread.isRunning()
    assert dock.delete_scheduled is True
    assert dock.deleted is False
    assert host._session is None
    assert host.workbench_snapshot() == {
        "schema_version": 1,
        "lifecycle": "inactive",
        "dock_count": 0,
        "main_thread_id": threading.get_ident(),
        "worker_thread_id": None,
        "daemon_id": None,
        "heartbeat_count": 2,
        "client_construction_count": 1,
    }


@pytest.mark.parametrize("failure", ("remove", "set_parent"))
def test_c01_host_detach_is_ordered_best_effort_and_reports_residual_ownership(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(
        gateway_module,
        "KernelGateway",
        lambda: original_gateway(make_client),
    )
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")
    host.activate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "active")
    residual_session = host._session
    assert residual_session is not None
    residual_thread = residual_session.thread
    assert residual_thread is not None
    dock = freecad_gui.main_window.docks[0]
    calls: list[str] = []
    original_hide = dock.hide
    original_remove = freecad_gui.main_window.removeDockWidget
    original_set_parent = dock.setParent
    original_delete_later = dock.deleteLater
    detach_fails = True

    def hide() -> None:
        calls.append("hide")
        original_hide()

    def remove(target: object) -> None:
        calls.append("remove")
        if failure == "remove" and detach_fails:
            raise RuntimeError("synthetic removeDockWidget failure")
        original_remove(target)

    def set_parent(parent: object | None) -> None:
        calls.append("setParent")
        if failure == "set_parent" and detach_fails:
            raise RuntimeError("synthetic setParent failure")
        original_set_parent(parent)

    def delete_later() -> None:
        calls.append("deleteLater")
        original_delete_later()

    monkeypatch.setattr(dock, "hide", hide)
    monkeypatch.setattr(freecad_gui.main_window, "removeDockWidget", remove)
    monkeypatch.setattr(dock, "setParent", set_parent)
    monkeypatch.setattr(dock, "deleteLater", delete_later)

    host.deactivate_workbench()
    pump_main_events(lambda: residual_session._thread_retired)

    assert calls == ["hide", "remove", "setParent"]
    assert host._session is residual_session
    assert host.workbench_snapshot()["lifecycle"] == "stopping"
    assert host.workbench_snapshot()["dock_count"] == 1
    assert dock.delete_scheduled is False
    assert dock.deleted is False
    assert clients[0].close_call_count == 1
    assert clients[0].closed_thread_id == clients[0].created_thread_id
    assert not residual_thread.isRunning()
    if failure == "remove":
        assert dock.hidden is True
        assert freecad_gui.main_window.docks == [dock]
        assert freecad_gui.main_window.children() == []
        assert dock.parent() is None
    else:
        assert dock.hidden is True
        assert freecad_gui.main_window.docks == []
        assert freecad_gui.main_window.children() == [dock]
        assert dock.parent() is freecad_gui.main_window

    host.activate_workbench()

    assert host._session is residual_session
    assert freecad_gui.main_window.add_attempts == 1
    assert len(clients) == 1
    assert host.workbench_snapshot()["lifecycle"] == "stopping"
    assert host.workbench_snapshot()["dock_count"] == 1
    retry_calls = ["hide", "remove"] if failure == "remove" else ["hide", "setParent"]
    assert calls == ["hide", "remove", "setParent", *retry_calls]
    ownership_docks = set(freecad_gui.main_window.docks)
    ownership_docks.update(
        freecad_gui.main_window.findChildren(
            fake_pyside.QtWidgets.QDockWidget,
            "VibeCADReviewDock",
        )
    )
    assert ownership_docks == {dock}
    assert dock.delete_scheduled is False

    host.activate_workbench()

    assert host._session is residual_session
    assert freecad_gui.main_window.add_attempts == 1
    assert len(clients) == 1
    assert calls == [
        "hide",
        "remove",
        "setParent",
        *retry_calls,
        *retry_calls,
    ]
    assert dock.delete_scheduled is False

    detach_fails = False
    host.activate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "active")

    assert host._session is not residual_session
    assert freecad_gui.main_window.add_attempts == 2
    assert len(clients) == 2
    replacement_dock = freecad_gui.main_window.docks[0]
    assert replacement_dock is not dock
    assert freecad_gui.main_window.docks == [replacement_dock]
    assert freecad_gui.main_window.children() == [replacement_dock]
    assert freecad_gui.main_window.findChildren(
        fake_pyside.QtWidgets.QDockWidget,
        "VibeCADReviewDock",
    ) == [replacement_dock]
    assert dock.parent() is None
    recovery_calls = (
        ["hide", "remove", "deleteLater"]
        if failure == "remove"
        else ["hide", "setParent", "deleteLater"]
    )
    assert calls == [
        "hide",
        "remove",
        "setParent",
        *retry_calls,
        *retry_calls,
        *recovery_calls,
    ]
    assert calls.count("deleteLater") == 1
    assert dock.delete_scheduled is True
    assert clients[0].close_call_count == 1

    terminal_calls = list(calls)
    remembered_snapshot = dict(host._last_snapshot)
    residual_session._finished()
    residual_session._finished()
    assert calls == terminal_calls
    assert host._last_snapshot == remembered_snapshot

    host.deactivate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
    assert clients[1].close_call_count == 1
    assert freecad_gui.main_window.docks == []
    assert freecad_gui.main_window.children() == []


def test_c01_host_delete_later_failure_is_terminal_after_ownership_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(
        gateway_module,
        "KernelGateway",
        lambda: original_gateway(make_client),
    )
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")
    host.activate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "active")
    old_session = host._session
    assert old_session is not None
    old_thread = old_session.thread
    assert old_thread is not None
    old_dock = freecad_gui.main_window.docks[0]
    delete_later_attempts = 0
    calls: list[str] = []
    original_hide = old_dock.hide
    original_remove = freecad_gui.main_window.removeDockWidget
    original_set_parent = old_dock.setParent

    def hide() -> None:
        calls.append("hide")
        original_hide()

    def remove(target: object) -> None:
        calls.append("remove")
        original_remove(target)

    def set_parent(parent: object | None) -> None:
        calls.append("setParent")
        original_set_parent(parent)

    def fail_delete_later() -> None:
        nonlocal delete_later_attempts
        calls.append("deleteLater")
        delete_later_attempts += 1
        raise RuntimeError("synthetic deleteLater failure")

    monkeypatch.setattr(old_dock, "hide", hide)
    monkeypatch.setattr(freecad_gui.main_window, "removeDockWidget", remove)
    monkeypatch.setattr(old_dock, "setParent", set_parent)
    monkeypatch.setattr(old_dock, "deleteLater", fail_delete_later)
    host.deactivate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

    assert host._session is None
    assert not old_thread.isRunning()
    assert clients[0].close_call_count == 1
    assert host.workbench_snapshot()["dock_count"] == 0
    assert freecad_gui.main_window.docks == []
    assert freecad_gui.main_window.children() == []
    assert old_dock.parent() is None
    assert old_dock.delete_scheduled is False
    assert delete_later_attempts == 1
    assert calls == ["hide", "remove", "setParent", "deleteLater"]

    old_session._finished()
    old_session._finished()
    assert delete_later_attempts == 1
    assert calls == ["hide", "remove", "setParent", "deleteLater"]

    host.activate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "active")

    assert len(clients) == 2
    assert freecad_gui.main_window.add_attempts == 2
    assert len(freecad_gui.main_window.docks) == 1
    assert freecad_gui.main_window.docks[0] is not old_dock
    assert (
        len(
            freecad_gui.main_window.findChildren(
                fake_pyside.QtWidgets.QDockWidget,
                "VibeCADReviewDock",
            )
        )
        == 1
    )

    host.deactivate_workbench()
    pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
    assert clients[1].close_call_count == 1
