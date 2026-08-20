from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import inspect
import os
import queue
import runpy
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

from tests.fixtures.freecad_workbench.fake_host import (
    FakeFreeCAD,
    FakeFreeCADGui,
    FakeLocalAgentClient,
    FakeWorkbench,
    _fake_release_envelope,
    force_cleanup_workbench,
    install_fake_pyside,
    make_fake_freecad,
    make_fake_freecad_gui,
    pump_main_events,
    settle_workbench_events,
)
from vibecad._file_compat import capture_windows_path

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
    "ReleaseFileSummary",
    "ReleaseSummary",
    "project_page_from_mapping",
    "project_summary_from_detail_mapping",
    "task_summary_from_detail_mapping",
    "task_page_from_mapping",
    "release_summary_from_mapping",
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
_RELEASE_FILE_FIELDS = ("name", "media_type", "sha256", "size_bytes", "resource_uri")
_RELEASE_FIELDS = (
    "release_id",
    "status",
    "generation",
    "task_id",
    "task_generation",
    "project_id",
    "revision_id",
    "revision_manifest_sha256",
    "verification_id",
    "verification_digest",
    "observation_digest",
    "manifest",
    "drawing",
    "bom_json",
    "bom_csv",
    "validation_report_uri",
    "package",
    "approved_at_ms",
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
        ("ReleaseFileSummary", _RELEASE_FILE_FIELDS),
        ("ReleaseSummary", _RELEASE_FIELDS),
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


def test_release_projection_requires_exact_draft_and_approved_resource_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _load_state_module(monkeypatch)
    draft_mapping = _fake_release_envelope(
        status="draft",
        generation=0,
        approved_at_ms=None,
    )

    draft = state.release_summary_from_mapping(draft_mapping)

    assert draft.status == "draft"
    assert draft.generation == 0
    assert draft.drawing.resource_uri.endswith("/assembly-drawing.pdf")
    assert draft.package.resource_uri is None
    approved_mapping = _fake_release_envelope(
        status="approved",
        generation=1,
        approved_at_ms=1_000_000_000,
    )
    approved = state.release_summary_from_mapping(approved_mapping)
    assert approved.status == "approved"
    assert approved.package.resource_uri.endswith("/vibecad-release.zip")

    approved_mapping["result"]["package"]["resource_uri"] = None
    with pytest.raises(state.ProjectionError, match=r"^invalid public mapping$"):
        state.release_summary_from_mapping(approved_mapping)


def test_p2_release_dock_builds_exact_revision_then_approves_exact_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    emitted: list[dict[str, object]] = []
    dock.request.connect(emitted.append)
    project_id = "project_" + "1" * 32
    task = _task_record(
        "1",
        generation=3,
        candidate_revision="revision_" + "4" * 32,
        committed_revision="revision_" + "4" * 32,
        draft_id="draft_" + "4" * 32,
    )
    task["project_id"] = project_id
    task["status"] = "succeeded"
    task["next_action"] = "none"
    dock._project_ids = [project_id]
    dock.project_selector.addItem(project_id)
    dock._task_ids = [task["task_id"]]
    dock._tasks_by_id = {task["task_id"]: task}
    dock.task_selector.addItem(task["task_id"])
    dock._update_preview_actions()

    assert dock.build_release_button.enabled
    dock.build_release_button.click()
    create = emitted[-1]
    assert create["kind"] == "release_create"
    assert create["task_id"] == task["task_id"]
    assert create["expected_generation"] == 3
    assert create["revision_id"] == "revision_" + "4" * 32
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": create["request_id"],
            "kind": "release_created",
            "response": _fake_release_envelope(status="draft", generation=0, approved_at_ms=None),
        }
    )
    assert dock.release_status_label.text.startswith("Draft package ")
    assert dock.approve_release_button.enabled

    dock.approve_release_button.click()
    approve = emitted[-1]
    assert approve["kind"] == "release_approve"
    assert approve["expected_generation"] == 0
    assert approve["expected_package_sha256"] == "9" * 64
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": approve["request_id"],
            "kind": "release_approved",
            "response": _fake_release_envelope(
                status="approved", generation=1, approved_at_ms=1_000_000_000
            ),
        }
    )
    assert dock.release_status_label.text.startswith("Approved package ")
    assert dock.save_release_button.enabled


def test_p2_gateway_release_save_requires_private_host_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    capability = object()
    results: queue.Queue[tuple[object, object, FakeLocalAgentClient]] = queue.Queue()
    destination = str((tmp_path / "assembly-drawing.pdf").resolve())
    uri = "vibecad://release/release_" + "8" * 32 + "/assembly-drawing.pdf"

    def worker() -> None:
        client = FakeLocalAgentClient()
        gateway = gateway_module.KernelGateway(
            lambda: client,
            wire_capability=capability,
        )
        gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
        public = gateway.handle(
            {
                "schema_version": 1,
                "request_id": 1,
                "kind": "release_save",
                "uri": uri,
                "destination": destination,
            }
        )
        private = gateway.handle(
            gateway_module._PrivateWireCommand(
                {
                    "schema_version": 1,
                    "request_id": 2,
                    "kind": "release_save",
                    "uri": uri,
                    "destination": destination,
                },
                capability,
            )
        )
        results.put((public, private, client))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    public, private, client = results.get_nowait()
    assert public["kind"] == "error"
    assert public["code"] == "invalid_input"
    assert private["kind"] == "release_saved"
    assert private["response"]["destination"] == destination
    assert Path(destination).read_bytes() == client.release_bytes
    assert [call[0] for call in client.calls].count("save_release_resource") == 1


def test_c03_detail_parsers_accept_real_fake_authority_and_reject_project_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _load_state_module(monkeypatch)
    client = FakeLocalAgentClient()
    task = state.task_summary_from_detail_mapping(
        client.get_task_request({"schema_version": 1, "task_id": "task_" + "1" * 32})
    )
    project_response = client.get_project_request(
        {"schema_version": 1, "project_id": "project_" + "1" * 32}
    )
    project = state.project_summary_from_detail_mapping(project_response)

    assert (task.generation, task.base_revision, task.draft_id) == (
        3,
        "revision_" + "1" * 32,
        "draft_" + "4" * 32,
    )
    assert (project.generation, project.revision_id) == (
        2,
        "revision_" + "1" * 32,
    )
    project_response["result"]["current"]["revision"]["manifest_sha256"] = "f" * 64
    with pytest.raises(state.ProjectionError):
        state.project_summary_from_detail_mapping(project_response)


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


def test_fix04_legacy_gateway_private_close_stays_on_one_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []
    responses: queue.Queue[dict[str, object]] = queue.Queue()

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    capability = object()
    commands = (
        {"schema_version": 1, "kind": "connect", "request_id": 0},
        {
            "schema_version": 1,
            "kind": "list_projects",
            "request_id": 1,
            "cursor": None,
        },
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "kind": "close", "request_id": 2},
            capability,
        ),
    )

    def worker() -> None:
        gateway = _fix04_gateway(gateway_module, make_client, capability)
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


def test_c02_gateway_opens_then_claims_on_same_worker_client_and_emits_plain_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []
    events: queue.Queue[dict[str, object]] = queue.Queue()
    source = {"kind": "head", "project_id": "project_" + "1" * 32}
    open_key = "checkout_open_" + "8" * 32

    def worker() -> None:
        client = FakeLocalAgentClient()
        clients.append(client)
        gateway = gateway_module.KernelGateway(lambda: client)
        gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
        events.put(
            gateway.handle(
                {
                    "schema_version": 1,
                    "request_id": 1,
                    "kind": "preview_open",
                    "source": source,
                    "open_key": open_key,
                }
            )
        )

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    event = events.get_nowait()
    assert event["kind"] == "preview_opened"
    assert set(event) == {"schema_version", "request_id", "kind", "response"}
    assert type(event["response"]) is dict
    client = clients[0]
    assert [call[0] for call in client.calls] == [
        "ping",
        "open_checkout",
        "claim_file_grant",
    ]
    assert len({call[2] for call in client.calls}) == 1
    assert event["response"]["source"] == source
    assert event["response"]["open_key"] == open_key
    assert "local_path" not in event["response"]["descriptor"]
    assert Path(event["response"]["claim"]["local_path"]).name == "model.FCStd"


def test_p1_gateway_keepalive_uses_owner_thread_without_consuming_request_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    results: queue.Queue[tuple[object, ...]] = queue.Queue()

    def worker() -> None:
        client = FakeLocalAgentClient()
        gateway = gateway_module.KernelGateway(lambda: client)
        connected = gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
        first = gateway.keepalive()
        client.ping_failures = 1
        second = gateway.keepalive()
        projects = gateway.handle(
            {
                "schema_version": 1,
                "request_id": 1,
                "kind": "list_projects",
                "cursor": None,
            }
        )
        results.put((connected, first, second, projects, client))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()

    connected, first, second, projects, client = results.get_nowait()
    assert connected["kind"] == "connected"
    assert first is True
    assert second is False
    assert projects["kind"] == "projects"
    assert [call[0] for call in client.calls] == [
        "ping",
        "ping",
        "ping",
        "list_projects",
    ]
    assert len({call[2] for call in client.calls}) == 1


def test_p1_gateway_requires_private_authority_for_edit_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    capability = object()
    results: queue.Queue[tuple[object, object, FakeLocalAgentClient]] = queue.Queue()

    def worker() -> None:
        client = FakeLocalAgentClient()
        gateway = gateway_module.KernelGateway(
            lambda: client,
            wire_capability=capability,
        )
        gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
        gateway.handle(
            {
                "schema_version": 1,
                "request_id": 1,
                "kind": "preview_open",
                "source": {"kind": "head", "project_id": "project_" + "1" * 32},
                "open_key": "checkout_open_" + "2" * 32,
            }
        )
        editable_path = client.checkout_paths["checkout_" + "6" * 32]
        editable_path.write_bytes(editable_path.read_bytes() + b"saved user edit\n")
        public = gateway.handle(
            {
                "schema_version": 1,
                "request_id": 2,
                "kind": "edit_checkpoint",
                "checkpoint_key": "checkpoint_create_" + "3" * 32,
                "checkout_id": "checkout_" + "6" * 32,
            }
        )
        command = gateway_module._PrivateWireCommand(  # noqa: SLF001
            {
                "schema_version": 1,
                "request_id": 3,
                "kind": "edit_checkpoint",
                "checkpoint_key": "checkpoint_create_" + "4" * 32,
                "checkout_id": "checkout_" + "6" * 32,
            },
            capability,
        )
        private = gateway.handle(command)
        results.put((public, private, client))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    public, private, client = results.get_nowait()

    assert public["kind"] == "error"
    assert public["code"] == "invalid_input"
    assert private["kind"] == "edit_checkpointed"
    assert private["response"]["outcome"] == "task"
    assert private["response"]["task"]["task_run"]["status"] == "succeeded"
    assert [call[0] for call in client.calls].count("get_checkout") == 1
    assert [call[0] for call in client.calls].count("checkpoint_checkout") == 1


def test_c02_dock_selected_head_and_draft_buttons_emit_preview_open_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    emitted: list[dict[str, object]] = []
    dock.request.connect(emitted.append)
    dock._project_ids = ["project_" + "1" * 32]
    dock.project_selector.addItem(dock._project_ids[0])
    task = _task_record(
        "1",
        generation=3,
        candidate_revision="revision_" + "4" * 32,
        draft_id="draft_" + "4" * 32,
    )
    task["status"] = "awaiting_user_review"
    dock._task_ids = [task["task_id"]]
    dock._tasks_by_id = {task["task_id"]: task}
    dock.task_selector.addItem(task["task_id"])

    dock.open_head_button.click()
    dock.open_draft_button.click()

    preview_commands = [command for command in emitted if command["kind"] == "preview_open"]
    assert len(preview_commands) == 2
    assert preview_commands[0]["source"] == {
        "kind": "head",
        "project_id": "project_" + "1" * 32,
    }
    assert preview_commands[1]["source"] == {
        "kind": "draft",
        "task_id": "task_" + "1" * 32,
        "draft_id": "draft_" + "4" * 32,
        "expected_generation": 3,
    }
    assert preview_commands[0]["open_key"].startswith("checkout_open_")
    assert preview_commands[1]["open_key"].startswith("checkout_open_")
    assert preview_commands[0]["open_key"] != preview_commands[1]["open_key"]


def test_p1_dock_marks_managed_previews_as_agent_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()

    assert dock.ownership_status_label.objectName() == "VibeCADEditingOwnership"
    assert dock.ownership_status_label.text == "No managed preview"

    dock._preview_pending_sources.add("head")
    dock._update_preview_actions()

    assert dock.ownership_status_label.text == (
        "Agent preview — do not edit; local edits disable review"
    )
    dock.set_preview_eligibility(False, recovery_required=True)
    assert dock.ownership_status_label.text == (
        "Preview ownership recovery required — reload managed previews"
    )


def test_c02_host_opens_on_main_and_deactivates_document_checkout_client_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    events: list[str] = []
    freecad = make_fake_freecad(events=events)
    assert isinstance(freecad, FakeFreeCAD)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient(events=events)
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(gateway_module, "KernelGateway", lambda: original_gateway(make_client))
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")
    host.activate_workbench()
    try:
        pump_main_events(
            lambda: (
                bool(freecad_gui.main_window.docks)
                and freecad_gui.main_window.docks[0].task_selector.items
            )
        )
        dock = freecad_gui.main_window.docks[0]
        dock.open_head_button.click()
        dock.open_draft_button.click()
        pump_main_events(lambda: len(freecad.documents) == 2)

        assert len(clients) == 1
        assert freecad.opened_paths == [
            str(clients[0].checkout_paths["checkout_" + "6" * 32]),
            str(clients[0].checkout_paths["checkout_" + "7" * 32]),
        ]
        assert all(document.Modified is False for document in freecad.documents.values())
        host.deactivate_workbench()
        assert freecad.documents == {}
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

        close_events = [event for event in events if event != "document.open"]
        assert close_events == [
            "document.close",
            "document.close",
            "checkout.close:" + "checkout_" + "6" * 32,
            "checkout.close:" + "checkout_" + "7" * 32,
            "client.close",
        ]
    finally:
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")


def test_p1_host_timer_keeps_gateway_connection_live_on_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad = make_fake_freecad()
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(gateway_module, "KernelGateway", lambda: original_gateway(make_client))
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")
    host.activate_workbench()
    session = host._session
    assert session is not None
    timer = session.keepalive_timer
    assert timer is not None
    try:
        pump_main_events(lambda: session.lifecycle == "active" and not session._pending)
        assert timer.isActive()
        assert timer.interval == 10_000
        assert len(clients) == 1
        timer.timeout.emit()
        pump_main_events(lambda: session._keepalive_count == 1)

        ping_calls = [call for call in clients[0].calls if call[0] == "ping"]
        assert len(ping_calls) == 2
        assert ping_calls[0][2] == ping_calls[1][2] == session.worker_thread_id
        assert session.lifecycle == "active"
        assert session.client_construction_count == 1
    finally:
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
    assert not timer.isActive()


@pytest.mark.parametrize(
    ("save_before_checkpoint", "recomputed_before_checkpoint"),
    [(False, False), (True, False), (False, True)],
)
def test_p1_editable_head_checkpoints_then_closes_and_refreshes(
    monkeypatch: pytest.MonkeyPatch,
    save_before_checkpoint: bool,
    recomputed_before_checkpoint: bool,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    events: list[str] = []
    freecad = make_fake_freecad(events=events)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient(events=events)
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(gateway_module, "KernelGateway", lambda: original_gateway(make_client))
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")
    host.activate_workbench()
    try:
        pump_main_events(
            lambda: (
                bool(freecad_gui.main_window.docks)
                and freecad_gui.main_window.docks[0].task_selector.items
            )
        )
        dock = freecad_gui.main_window.docks[0]
        assert dock.open_edit_button.enabled is True

        dock.open_edit_button.click()
        pump_main_events(lambda: len(freecad.documents) == 1)

        checkout_id = dock._preview_checkouts["edit"]
        document = next(iter(freecad.documents.values()))
        assert dock.ownership_status_label.text == (
            "User editable HEAD — Save stays local; Checkpoint publishes a revision"
        )
        assert dock.open_head_button.enabled is False
        assert dock.open_draft_button.enabled is False
        document.Modified = True
        if save_before_checkpoint:
            document.save()
            if sys.platform == "win32":
                assert Path(document.FileName).is_file()
            else:
                assert stat.S_IMODE(Path(document.FileName).stat().st_mode) == 0o644
        elif recomputed_before_checkpoint:
            document.simulate_recomputed_edit()
            assert document.Modified is False

        edited_path = Path(document.FileName)

        dock.checkpoint_edit_button.click()
        pump_main_events(
            lambda: (
                not freecad.documents
                and "edit" not in dock._preview_checkouts
                and any(call[0] == "checkpoint_checkout" for call in clients[0].calls)
            )
        )

        operation_names = [call[0] for call in clients[0].calls]
        assert operation_names.index("checkpoint_checkout") < operation_names.index(
            "close_checkout"
        )
        checkpoint_call = next(
            call for call in clients[0].calls if call[0] == "checkpoint_checkout"
        )
        assert checkpoint_call[1]["checkout_id"] == checkout_id
        assert checkpoint_call[1]["checkpoint_key"].startswith("checkpoint_create_")
        if save_before_checkpoint:
            assert "document.recompute" not in events
        else:
            assert events.index("document.recompute") < events.index("document.save")
            assert events.count("document.recompute") == 1
        assert events.count("document.save") == 1
        assert freecad.document_observers == []
        assert events.index("document.save") < events.index("document.close")
        if sys.platform == "win32":
            assert capture_windows_path(edited_path, directory=False).path == str(edited_path)
        else:
            assert stat.S_IMODE(edited_path.stat().st_mode) == 0o600
        assert dock.edit_status_label.text == "Editable HEAD closed"
        assert dock.open_edit_button.enabled is True
    finally:
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")


def test_p1_private_mode_restore_is_exact_and_rejects_file_aliases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    host = _load_workbench_module(monkeypatch, "host")

    managed = tmp_path / "model.FCStd"
    managed.write_bytes(b"managed FreeCAD edit")
    managed.chmod(0o644)
    host._restore_private_document_mode(str(managed))
    if sys.platform == "win32":
        assert capture_windows_path(managed, directory=False).path == str(managed)
    else:
        assert stat.S_IMODE(managed.stat().st_mode) == 0o600

    target = tmp_path / "aliased.FCStd"
    target.write_bytes(b"must remain untouched")
    target.chmod(0o644)
    symbolic = tmp_path / "symbolic.FCStd"
    symbolic.symlink_to(target)
    with pytest.raises(host.PreviewError):
        host._restore_private_document_mode(str(symbolic))

    hardlink = tmp_path / "hardlink.FCStd"
    os.link(target, hardlink)
    with pytest.raises(host.PreviewError):
        host._restore_private_document_mode(str(hardlink))
    if sys.platform == "win32":
        assert target.read_bytes() == b"must remain untouched"
    else:
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_p1_editable_head_clean_checkpoint_is_noop_and_discard_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad = make_fake_freecad()
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    original_gateway = gateway_module.KernelGateway
    monkeypatch.setattr(gateway_module, "KernelGateway", lambda: original_gateway(make_client))
    monkeypatch.delitem(sys.modules, "vibecad_workbench.dock", raising=False)
    host = _load_workbench_module(monkeypatch, "host")
    host.activate_workbench()
    try:
        pump_main_events(
            lambda: (
                bool(freecad_gui.main_window.docks)
                and freecad_gui.main_window.docks[0].task_selector.items
            )
        )
        dock = freecad_gui.main_window.docks[0]
        dock.open_edit_button.click()
        pump_main_events(lambda: len(freecad.documents) == 1)

        dock.checkpoint_edit_button.click()
        pump_main_events(lambda: dock.edit_status_label.text == "No saved changes to checkpoint")
        assert dock.edit_status_label.text == "No saved changes to checkpoint"
        assert "edit" in dock._preview_checkouts
        assert not any(call[0] == "checkpoint_checkout" for call in clients[0].calls)

        next(iter(freecad.documents.values())).Modified = True
        dock.discard_edit_button.click()
        pump_main_events(lambda: not freecad.documents and "edit" not in dock._preview_checkouts)

        assert not any(call[0] == "checkpoint_checkout" for call in clients[0].calls)
        assert any(call[0] == "close_checkout" for call in clients[0].calls)
        assert dock.edit_status_label.text == "Editable HEAD closed"
    finally:
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")


def test_fail_preview_open_error_clears_pending_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    emitted: list[dict[str, object]] = []
    dock.request.connect(emitted.append)
    dock._project_ids = ["project_" + "1" * 32]
    dock.project_selector.addItem(dock._project_ids[0])

    dock.open_head_button.click()
    first = [command for command in emitted if command["kind"] == "preview_open"][0]
    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": first["request_id"],
            "kind": "error",
            "operation": "preview_open",
            "code": "invalid_input",
            "outcome": "known_failure",
        }
    )
    dock.open_head_button.click()

    preview_commands = [command for command in emitted if command["kind"] == "preview_open"]
    assert len(preview_commands) == 2
    assert preview_commands[0]["open_key"] != preview_commands[1]["open_key"]


def _start_fail_cleanup_host(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, FakeFreeCAD, list[FakeLocalAgentClient], list[str]]:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    events: list[str] = []
    freecad = make_fake_freecad(events=events)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient(events=events)
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
    pump_main_events(
        lambda: (
            bool(freecad_gui.main_window.docks)
            and freecad_gui.main_window.docks[0].task_selector.items
        )
    )
    dock = freecad_gui.main_window.docks[0]
    dock.open_head_button.click()
    dock.open_draft_button.click()
    pump_main_events(lambda: len(freecad.documents) == 2)
    return host, freecad, clients, events


def test_fix04_force_cleanup_retires_thread_and_discards_owned_main_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, _clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    thread = session.thread
    assert thread is not None
    fixture = importlib.import_module("tests.fixtures.freecad_workbench.fake_host")

    def fail_event_pump(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("synthetic main-event pump failure")

    monkeypatch.setattr(fixture, "pump_main_events", fail_event_pump)
    force_cleanup_workbench(host, freecad)

    assert not thread.isRunning()
    with fixture._MAIN_EVENTS.mutex:
        retained = list(fixture._MAIN_EVENTS.queue)
    assert all(getattr(slot, "__self__", None) is not session for slot, _args in retained)


def test_c02_force_cleanup_never_executes_or_discards_foreign_main_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, _clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    thread = session.thread
    assert thread is not None
    fixture = importlib.import_module("tests.fixtures.freecad_workbench.fake_host")

    class _ForeignOwner:
        def __init__(self) -> None:
            self.calls = 0

        def callback(self) -> None:
            self.calls += 1
            raise AssertionError("foreign callback must remain untouched")

    foreign = _ForeignOwner()
    foreign_event = (foreign.callback, ())
    fixture._MAIN_EVENTS.put(foreign_event)
    try:
        force_cleanup_workbench(host, freecad)
        with fixture._MAIN_EVENTS.mutex:
            retained = any(item is foreign_event for item in fixture._MAIN_EVENTS.queue)

        assert (
            foreign.calls,
            retained,
            host._session is None,
            thread.isRunning(),
        ) == (0, True, True, False)
    finally:
        with fixture._MAIN_EVENTS.mutex:
            for index, item in enumerate(fixture._MAIN_EVENTS.queue):
                if item is foreign_event:
                    del fixture._MAIN_EVENTS.queue[index]
                    break


def test_fail_document_close_retains_authority_until_retry_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = _start_fail_cleanup_host(monkeypatch)
    owned_session = host._session
    assert owned_session is not None
    owned_thread = owned_session.thread
    owned_dock = owned_session.dock
    assert owned_thread is not None
    assert owned_dock is not None
    freecad.close_failures = 1
    try:
        host.deactivate_workbench()

        assert host.workbench_snapshot()["lifecycle"] == "stopping"
        assert clients[0].close_call_count == 0
        assert len(freecad.documents) == 2

        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

        assert freecad.documents == {}
        assert events[-3:] == [
            "checkout.close:" + "checkout_" + "6" * 32,
            "checkout.close:" + "checkout_" + "7" * 32,
            "client.close",
        ]
    finally:
        freecad.close_failures = 0
        force_cleanup_workbench(host, freecad)
        assert host._session is None
        assert not owned_thread.isRunning()
        assert owned_session._dock_count == 0
        assert owned_dock.parent() is None
        assert freecad.documents == {}


def test_fail_checkout_close_retains_client_until_retry_converges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = _start_fail_cleanup_host(monkeypatch)
    owned_session = host._session
    assert owned_session is not None
    owned_thread = owned_session.thread
    owned_dock = owned_session.dock
    assert owned_thread is not None
    assert owned_dock is not None
    client = clients[0]
    client.checkout_close_failures = 1
    before = host.workbench_snapshot()["heartbeat_count"]
    try:
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] > before)

        assert host.workbench_snapshot()["lifecycle"] == "stopping"
        assert client.close_call_count == 0
        assert freecad.documents == {}

        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

        assert events[-3:] == [
            "checkout.close:" + "checkout_" + "6" * 32,
            "checkout.close:" + "checkout_" + "7" * 32,
            "client.close",
        ]
        close_calls = [call for call in client.calls if call[0] == "close_checkout"]
        assert [call[1]["checkout_id"] for call in close_calls] == [
            "checkout_" + "6" * 32,
            "checkout_" + "6" * 32,
            "checkout_" + "7" * 32,
        ]
    finally:
        client.checkout_close_failures = 0
        force_cleanup_workbench(host, freecad)
        assert host._session is None
        assert not owned_thread.isRunning()
        assert owned_session._dock_count == 0
        assert owned_dock.parent() is None
        assert freecad.documents == {}


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
    assert dock.accept_button.object_name == "VibeCADAcceptDraft"
    assert dock.reject_button.object_name == "VibeCADRejectDraft"
    assert dock.accept_button.enabled is False
    assert dock.reject_button.enabled is False
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


def test_fix04_legacy_gateway_private_review_detaches_and_stays_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    project_id = "project_" + "1" * 32
    task_id = "task_" + "1" * 32
    draft_id = "draft_" + "4" * 32

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
        _fix04_private_gateway_command(
            gateway_module,
            {
                "schema_version": 1,
                "kind": "review",
                "request_id": 4,
                "decision": "reject",
                "task_id": task_id,
                "draft_id": draft_id,
                "expected_generation": 3,
            },
            capability,
        )
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
    event = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            review,
            capability,
        )
    )
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


@pytest.mark.parametrize(
    "malformation",
    (
        "head-missing",
        "head-extra",
        "wrong-kind",
        "project-id",
        "draft-missing",
        "draft-extra",
        "task-id",
        "draft-id",
        "bool-generation",
        "negative-generation",
        "source-subclass",
        "command-subclass",
        "open-key",
    ),
)
def test_c02_gateway_rejects_non_exact_preview_source_and_open_key_without_effect(
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    source: dict[str, object] = {
        "kind": "draft",
        "task_id": "task_" + "1" * 32,
        "draft_id": "draft_" + "4" * 32,
        "expected_generation": 3,
    }
    if malformation.startswith("head-") or malformation in {"wrong-kind", "project-id"}:
        source = {
            "kind": "head",
            "project_id": "project_" + "1" * 32,
        }
    if malformation == "head-missing":
        del source["project_id"]
    elif malformation == "head-extra":
        source["unexpected"] = None
    elif malformation == "wrong-kind":
        source["kind"] = "branch"
    elif malformation == "project-id":
        source["project_id"] = "project_" + "g" * 32
    elif malformation == "draft-missing":
        del source["draft_id"]
    elif malformation == "draft-extra":
        source["unexpected"] = None
    elif malformation == "task-id":
        source["task_id"] = "task_short"
    elif malformation == "draft-id":
        source["draft_id"] = "draft_" + "G" * 32
    elif malformation == "bool-generation":
        source["expected_generation"] = True
    elif malformation == "negative-generation":
        source["expected_generation"] = -1
    elif malformation == "source-subclass":
        source = _DictSubclass(source)

    command: dict[str, object] = {
        "schema_version": 1,
        "request_id": 11,
        "kind": "preview_open",
        "source": source,
        "open_key": "checkout_open_" + "f" * 32,
    }
    if malformation == "command-subclass":
        command = _DictSubclass(command)
    elif malformation == "open-key":
        command["open_key"] = "checkout_open_" + "f" * 31

    event = gateway.handle(command)

    expected_request_id = -1 if malformation == "command-subclass" else 11
    expected_operation = "invalid" if malformation == "command-subclass" else "preview_open"
    assert event == {
        "schema_version": 1,
        "request_id": expected_request_id,
        "kind": "error",
        "operation": expected_operation,
        "code": "invalid_input",
        "outcome": "known_failure",
    }
    assert [
        call[0]
        for call in client.calls
        if call[0] in {"open_checkout", "claim_file_grant", "close_checkout"}
    ] == []


def test_fix04_legacy_gateway_authenticated_review_exception_is_one_call_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    client.review_failure = True
    task_id = "task_" + "1" * 32
    draft_id = "draft_" + "4" * 32

    event = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {
                "schema_version": 1,
                "request_id": 7,
                "kind": "review",
                "decision": "accept",
                "task_id": task_id,
                "draft_id": draft_id,
                "expected_generation": 3,
            },
            capability,
        )
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


def test_c01_gateway_refresh_task_accepts_event_depth_eleven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    nested: object = None
    for _ in range(9):
        nested = {"level": nested}
    response = {
        "schema_version": 1,
        "ok": True,
        "result": nested,
        "error": None,
    }

    def get_task(request: dict[str, object]) -> dict[str, object]:
        client._record("get_task", request)
        return response

    client.get_task_request = get_task

    command = {
        "schema_version": 1,
        "request_id": 1,
        "kind": "refresh_task",
        "task_id": "task_" + "1" * 32,
    }
    event = gateway.handle(command)

    assert event == {
        "schema_version": 1,
        "request_id": 1,
        "kind": "task",
        "response": response,
    }
    assert gateway.handle(command) == event
    assert [name for name, _, _ in client.calls].count("get_task") == 1


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


def test_fix04_fake_freecad_monotonic_names_preserve_existing_document_identity() -> None:
    freecad = make_fake_freecad()
    document1 = freecad.openDocument("/managed/preview/one.FCStd")
    document2 = freecad.openDocument("/managed/preview/two.FCStd")

    assert (document1.Name, document2.Name) == (
        "VibeCADPreview1",
        "VibeCADPreview2",
    )

    freecad.closeDocument(document1.Name)

    assert freecad.listDocuments() == {document2.Name: document2}
    assert freecad.getDocument(document2.Name) is document2

    document3 = freecad.openDocument("/managed/preview/three.FCStd")

    assert (document1.Name, document2.Name, document3.Name) == (
        "VibeCADPreview1",
        "VibeCADPreview2",
        "VibeCADPreview3",
    )
    assert len({document1.Name, document2.Name, document3.Name}) == 3
    assert freecad.getDocument(document2.Name) is document2
    assert freecad.getDocument(document3.Name) is document3
    assert document3 is not document2
    assert freecad.listDocuments() == {
        document2.Name: document2,
        document3.Name: document3,
    }


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
    host_source = ast.parse(
        _HOST.read_text(encoding="utf-8"),
        filename=str(_HOST),
    )
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "wait"
        for node in ast.walk(host_source)
    )


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


def _start_fix02_host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    configure_client: Callable[[FakeLocalAgentClient], None] | None = None,
    client_factory: Callable[[], FakeLocalAgentClient] | None = None,
) -> tuple[
    object,
    FakeFreeCAD,
    FakeFreeCADGui,
    list[FakeLocalAgentClient],
    list[str],
]:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    events: list[str] = []
    freecad = make_fake_freecad(events=events)
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    clients: list[FakeLocalAgentClient] = []

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient(events=events) if client_factory is None else client_factory()
        if client_factory is not None:
            client.events = events
        if configure_client is not None:
            configure_client(client)
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
    pump_main_events(
        lambda: (
            bool(freecad_gui.main_window.docks)
            and freecad_gui.main_window.docks[0].task_selector.items
        )
    )
    return host, freecad, freecad_gui, clients, events


def _fix02_acquired_response(
    source: dict[str, object],
    open_key: str,
) -> dict[str, object]:
    client = FakeLocalAgentClient()
    opened = client.open_checkout(open_key=open_key, source=source)
    grant = opened.pop("file_grant")
    assert type(grant) is dict
    claim = client.claim_file_grant(grant_id=grant["grant_id"])
    return {
        "source": dict(source),
        "open_key": open_key,
        "descriptor": opened,
        "claim": claim,
    }


def _fix02_close_response(
    *,
    checkout_id: str = "checkout_" + "6" * 32,
    state: str = "closed",
    extra: bool = False,
) -> dict[str, object]:
    acquired = _fix02_acquired_response(
        {"kind": "head", "project_id": "project_" + "1" * 32},
        "checkout_open_" + "8" * 32,
    )
    descriptor = acquired["descriptor"]
    assert type(descriptor) is dict
    descriptor["checkout_id"] = checkout_id
    descriptor["state"] = state
    if extra:
        descriptor["unexpected"] = None
    return descriptor


@pytest.mark.parametrize("failure_stage", ("nested-validation", "claim"))
def test_fix04_legacy_acquisition_lost_close_ack_reconciles_before_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    close_attempts = 0

    def malformed(response: dict[str, object]) -> dict[str, object]:
        response["file_grant"]["unexpected"] = None
        return response

    def close_then_lose_ack(*, checkout_id: str) -> dict[str, object]:
        nonlocal close_attempts
        close_attempts += 1
        client._record("close_checkout", {"checkout_id": checkout_id})
        assert close_attempts == 1
        client.checkout_descriptors[checkout_id]["state"] = "closed"
        client.events.append(f"checkout.close:{checkout_id}")
        raise RuntimeError("synthetic acquisition cleanup acknowledgement lost")

    if failure_stage == "nested-validation":
        client.open_checkout_transform = malformed
    else:
        client.claim_file_grant_failures = 1
    monkeypatch.setattr(client, "close_checkout", close_then_lose_ack)
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    source = {"kind": "head", "project_id": "project_" + "1" * 32}
    checkout_id = "checkout_" + "6" * 32
    gateway.handle({"schema_version": 1, "kind": "connect", "request_id": 0})

    failed_open = gateway.handle(
        {
            "schema_version": 1,
            "kind": "preview_open",
            "request_id": 1,
            "source": source,
            "open_key": "checkout_open_" + "8" * 32,
        }
    )
    closed = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "kind": "close", "request_id": 2},
            capability,
        )
    )

    assert failed_open["kind"] == "error"
    assert closed["kind"] == "closed"
    close_calls = [call for call in client.calls if call[0] == "close_checkout"]
    assert [call[1]["checkout_id"] for call in close_calls] == [checkout_id]
    names = [call[0] for call in client.calls]
    assert names.index("close_checkout") < names.index("get_checkout")
    assert client.close_call_count == 1
    assert client.events[-2:] == [
        f"checkout.close:{checkout_id}",
        "client.close",
    ]


@pytest.mark.parametrize("identity_failure", ("missing", "request-mismatch"))
def test_fix04_legacy_untrustworthy_checkout_identity_blocks_authenticated_close(
    monkeypatch: pytest.MonkeyPatch,
    identity_failure: str,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()

    def untrustworthy(response: dict[str, object]) -> dict[str, object]:
        if identity_failure == "missing":
            del response["checkout_id"]
        else:
            response["open_key"] = "checkout_open_" + "9" * 32
        return response

    client.open_checkout_transform = untrustworthy
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "kind": "connect", "request_id": 0})
    failed_open = gateway.handle(
        {
            "schema_version": 1,
            "kind": "preview_open",
            "request_id": 1,
            "source": {
                "kind": "head",
                "project_id": "project_" + "1" * 32,
            },
            "open_key": "checkout_open_" + "8" * 32,
        }
    )
    attempted_close = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "kind": "close", "request_id": 2},
            capability,
        )
    )

    assert failed_open["kind"] == "error"
    assert attempted_close["kind"] == "error"
    assert attempted_close["operation"] == "close"
    assert not any(call[0] == "close_checkout" for call in client.calls)
    assert client.close_call_count == 0


@pytest.mark.parametrize(
    "response",
    (
        _fix02_close_response(state="open"),
        _fix02_close_response(extra=True),
        _fix02_close_response(checkout_id="checkout_" + "9" * 32),
    ),
)
def test_fix02_checkout_cleanup_advances_only_on_exact_closed_ack(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, object],
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    client = clients[0]
    head_checkout = "checkout_" + "6" * 32
    client.checkout_close_responses = [response]
    before = host.workbench_snapshot()["heartbeat_count"]
    try:
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] > before)

        assert host.workbench_snapshot()["lifecycle"] == "stopping"
        assert client.close_call_count == 0
        assert session.preview is not None
        assert head_checkout in session.preview.ready_checkout_ids()
    finally:
        client.checkout_close_responses.clear()
        force_cleanup_workbench(host, freecad)


def test_fix04_legacy_authenticated_client_close_failure_is_sticky(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    client.client_close_failures = 1
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "kind": "connect", "request_id": 0})

    first = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "kind": "close", "request_id": 1},
            capability,
        )
    )
    second = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "kind": "close", "request_id": 2},
            capability,
        )
    )

    assert first["kind"] == "error"
    assert first["operation"] == "close"
    assert second["kind"] == "error"
    assert second["operation"] == "close"
    assert client.close_call_count == 1
    assert client.closed_thread_id is None


def test_fix02_main_validation_failure_adopts_checkout_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, freecad_gui, clients, _events = _start_fix02_host(monkeypatch)
    preview_module = importlib.import_module("vibecad_workbench.preview")
    dock = freecad_gui.main_window.docks[0]
    commands: list[dict[str, object]] = []
    dock.request.connect(commands.append)

    def malformed_acquire(
        _client: object,
        *,
        source: object,
        open_key: object,
    ) -> dict[str, object]:
        assert type(source) is dict
        assert type(open_key) is str
        acquired = _fix02_acquired_response(source, open_key)
        acquired["descriptor"]["unexpected"] = None
        return acquired

    monkeypatch.setattr(
        preview_module.PreviewCoordinator,
        "acquire",
        staticmethod(malformed_acquire),
    )
    before = host.workbench_snapshot()["heartbeat_count"]
    try:
        dock.open_head_button.click()

        checkout_id = "checkout_" + "6" * 32
        pump_main_events(
            lambda: any(
                call[0] == "close_checkout" and call[1]["checkout_id"] == checkout_id
                for call in clients[0].calls
            )
        )

        close_calls = [
            call
            for call in clients[0].calls
            if call[0] == "close_checkout" and call[1]["checkout_id"] == checkout_id
        ]
        assert host.workbench_snapshot()["heartbeat_count"] > before
        assert len(close_calls) == 1
        name, payload, worker_thread_id = close_calls[0]
        assert name == "close_checkout"
        assert payload == {"checkout_id": checkout_id}
        assert worker_thread_id != threading.get_ident()
        assert worker_thread_id == clients[0].created_thread_id
        assert freecad.documents == {}
        assert commands == []
    finally:
        host.deactivate_workbench()
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "selection_drift",
    ("project", "task", "draft", "generation"),
)
def test_fix02_stale_success_is_cleanup_only_after_current_selection_drift(
    monkeypatch: pytest.MonkeyPatch,
    selection_drift: str,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def configure(client: FakeLocalAgentClient) -> None:
        client.open_checkout_entered = entered
        client.open_checkout_release = release

    host, freecad, freecad_gui, clients, _events = _start_fix02_host(
        monkeypatch,
        configure_client=configure,
    )
    dock = freecad_gui.main_window.docks[0]
    commands: list[dict[str, object]] = []
    dock.request.connect(commands.append)
    before = host.workbench_snapshot()["heartbeat_count"]
    expected_checkout = (
        "checkout_" + "6" * 32 if selection_drift == "project" else "checkout_" + "7" * 32
    )
    try:
        if selection_drift == "project":
            dock.open_head_button.click()
        else:
            dock.open_draft_button.click()
        assert entered.wait(0.5)

        if selection_drift == "project":
            other_project = "project_" + "2" * 32
            dock._project_ids.append(other_project)
            dock.project_selector.addItem(other_project)
            dock.project_selector.setCurrentIndex(1)
        elif selection_drift == "task":
            task = _task_record(
                "9",
                generation=3,
                candidate_revision="revision_" + "9" * 32,
                draft_id="draft_" + "9" * 32,
            )
            task["project_id"] = "project_" + "1" * 32
            task["status"] = "awaiting_user_review"
            dock._task_ids.append(task["task_id"])
            dock._tasks_by_id[task["task_id"]] = task
            dock.task_selector.addItem(task["task_id"])
            dock.task_selector.setCurrentIndex(1)
        else:
            task_id = dock.current_task_id()
            assert task_id is not None
            task = _task_record(
                "1",
                generation=4 if selection_drift == "generation" else 3,
                candidate_revision="revision_" + "4" * 32,
                draft_id=(
                    "draft_" + "9" * 32 if selection_drift == "draft" else "draft_" + "4" * 32
                ),
            )
            task["project_id"] = "project_" + "1" * 32
            task["status"] = "awaiting_user_review"
            dock._tasks_by_id[task_id] = task
            dock._update_preview_actions()

        release.set()
        pump_main_events(
            lambda: any(
                call[0] == "close_checkout" and call[1]["checkout_id"] == expected_checkout
                for call in clients[0].calls
            )
        )

        close_calls = [
            call
            for call in clients[0].calls
            if call[0] == "close_checkout" and call[1]["checkout_id"] == expected_checkout
        ]
        assert host.workbench_snapshot()["heartbeat_count"] > before
        assert len(close_calls) == 1
        name, payload, worker_thread_id = close_calls[0]
        assert name == "close_checkout"
        assert payload == {"checkout_id": expected_checkout}
        assert worker_thread_id != threading.get_ident()
        assert worker_thread_id == clients[0].created_thread_id
        assert freecad.documents == {}
        assert commands == []
    finally:
        release.set()
        host.deactivate_workbench()
        force_cleanup_workbench(host, freecad)


def test_fix02_mismatched_error_operation_does_not_consume_preview_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    emitted: list[dict[str, object]] = []
    dock.request.connect(emitted.append)
    project_id = "project_" + "1" * 32
    dock._project_ids = [project_id]
    dock.project_selector.addItem(project_id)

    dock.open_head_button.click()
    preview_commands = [command for command in emitted if command["kind"] == "preview_open"]
    assert len(preview_commands) == 1
    request_id = preview_commands[0]["request_id"]
    pending = dock.expected_preview_open(request_id)
    assert pending is not None

    dock.handle_event(
        {
            "schema_version": 1,
            "request_id": request_id,
            "kind": "error",
            "operation": "list_tasks",
            "code": "invalid_input",
            "outcome": "known_failure",
        }
    )
    dock.open_head_button.click()

    assert dock.expected_preview_open(request_id) == pending
    assert len([command for command in emitted if command["kind"] == "preview_open"]) == 1


@pytest.mark.parametrize("open_count", (1, 2))
def test_fix02_inflight_preview_during_stopping_never_opens_document(
    monkeypatch: pytest.MonkeyPatch,
    open_count: int,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def configure(client: FakeLocalAgentClient) -> None:
        client.open_checkout_entered = entered
        client.open_checkout_release = release

    host, freecad, freecad_gui, clients, events = _start_fix02_host(
        monkeypatch,
        configure_client=configure,
    )
    dock = freecad_gui.main_window.docks[0]
    client = clients[0]
    checkout_id = "checkout_" + "6" * 32
    try:
        dock.open_head_button.click()
        if open_count == 2:
            dock.open_draft_button.click()
        assert entered.wait(0.5)
        host.deactivate_workbench()

        assert host.workbench_snapshot()["lifecycle"] == "stopping"
        assert client.close_call_count == 0
        release.set()
        pump_main_events(
            lambda: host.workbench_snapshot()["lifecycle"] == "inactive",
            timeout=2.0,
        )

        assert freecad.documents == {}
        assert freecad.opened_paths == []
        expected_cleanup = [f"checkout.close:{checkout_id}"]
        if open_count == 2:
            expected_cleanup.append("checkout.close:" + "checkout_" + "7" * 32)
        expected_cleanup.append("client.close")
        assert events[-len(expected_cleanup) :] == expected_cleanup
    finally:
        release.set()
        force_cleanup_workbench(host, freecad)


def test_fix04_legacy_clean_pair_requires_one_shared_refresh_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    try:
        assert session.dock.preview_projection().review_eligible is False
        _fix04_refresh_cycle(host, session.dock, clients[0])
        assert session.dock.preview_projection().review_eligible is True
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize("role", ("command", "event"))
def test_fix04_c1_private_wire_wrapper_deep_snapshot_is_immutable(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    wrapper_type = _fix04_wrapper_type(role, gateway_module)
    capability = object()
    original = {
        "schema_version": 1,
        "request_id": 17,
        "kind": "connect",
        "metadata": {
            "labels": ["alpha", "beta"],
            "state": {"ready": True},
        },
    }
    expected = _fix04_plain_wire_value(original)
    assert type(expected) is dict
    wrapper = _fix04_wrap(wrapper_type, original, capability)

    original_metadata = original["metadata"]
    assert type(original_metadata) is dict
    original_metadata["labels"] = ["mutated-source"]
    original_metadata["state"] = {"ready": False}
    assert _fix04_wrapper_payload(wrapper) == expected

    returned_metadata = wrapper["metadata"]
    assert isinstance(returned_metadata, Mapping)
    assert _fix04_plain_wire_value(returned_metadata["labels"]) == [
        "alpha",
        "beta",
    ]
    try:
        returned_metadata["state"] = {"ready": False}
    except (AttributeError, TypeError):
        pass

    assert _fix04_wrapper_payload(wrapper) == expected
    assert _fix04_wrapper_capability(wrapper) is capability


def test_fix04_c1_hosted_reserve_requires_exact_projected_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dock_module, dock = _fix04_c1_standalone_dock(monkeypatch)
    raw_commands: list[object] = []
    messages: list[dict[str, object]] = []
    dock.request.connect(raw_commands.append)
    projected_cursor = dock._sequence

    def transport(message: object) -> object:
        snapshot = _fix04_plain_wire_value(message)
        assert type(snapshot) is dict
        messages.append(snapshot)
        if snapshot.get("phase") == "reserve":
            return projected_cursor + 1
        return None

    bind = getattr(dock, "_bind_host_transport", None)
    assert callable(bind)
    bind(transport)
    projection_error = getattr(dock_module, "ProjectionError", None)
    assert isinstance(projection_error, type)

    with pytest.raises(projection_error):
        dock.start()

    assert [message["phase"] for message in messages] == ["reserve", "cancel"]
    assert messages[0]["projected_cursor"] == projected_cursor
    assert messages[1]["request_id"] == projected_cursor + 1
    assert dock._sequence == projected_cursor
    assert dock._pending == {}
    assert raw_commands == []


def test_fix04_c1_refresh_begin_precedes_every_refresh_reserve_and_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dock_module, dock = _fix04_c1_standalone_dock(monkeypatch)
    project_id = "project_" + "1" * 32
    task_id = "task_" + "2" * 32
    checkout_ids = (
        "checkout_" + "6" * 32,
        "checkout_" + "7" * 32,
    )
    dock._project_ids = [project_id]
    dock.project_selector.blockSignals(True)
    dock.project_selector.addItem(project_id)
    dock.project_selector.blockSignals(False)
    dock._task_ids = [task_id]
    dock.task_selector.blockSignals(True)
    dock.task_selector.addItem(task_id)
    dock.task_selector.blockSignals(False)
    dock._preview_checkouts = {
        "head": checkout_ids[0],
        "draft": checkout_ids[1],
    }
    messages: list[dict[str, object]] = []

    def transport(message: object) -> object:
        snapshot = _fix04_plain_wire_value(message)
        assert type(snapshot) is dict
        messages.append(snapshot)
        if snapshot.get("phase") == "reserve":
            return snapshot["projected_cursor"]
        return None

    bind = getattr(dock, "_bind_host_transport", None)
    assert callable(bind)
    bind(transport)

    dock.refresh()

    begin_indices = [
        index for index, message in enumerate(messages) if message.get("phase") == "refresh_begin"
    ]
    assert len(begin_indices) == 1
    begin_index = begin_indices[0]
    assert messages[begin_index]["checkout_ids"] == list(checkout_ids)
    refresh_operations = {
        "refresh_project",
        "refresh_task",
        "preview_refresh",
    }
    reserved_operations = {
        message["projected_cursor"]: message["kind"]
        for message in messages
        if (message.get("phase") == "reserve" and message.get("kind") in refresh_operations)
    }
    refresh_transport_indices = [
        index
        for index, message in enumerate(messages)
        if (
            message.get("phase") == "reserve"
            and message.get("kind") in refresh_operations
            or message.get("phase") == "enqueue"
            and message.get("request_id") in reserved_operations
        )
    ]
    assert refresh_transport_indices
    assert all(begin_index < index for index in refresh_transport_indices)


def test_fix04_c1_hosted_public_discard_preview_open_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dock_module, dock = _fix04_c1_standalone_dock(monkeypatch)
    _fix03_deterministic_open_keys(monkeypatch)
    project_id = "project_" + "1" * 32
    dock._project_ids = [project_id]
    dock.project_selector.blockSignals(True)
    dock.project_selector.addItem(project_id)
    dock.project_selector.blockSignals(False)
    messages: list[dict[str, object]] = []

    def transport(message: object) -> object:
        snapshot = _fix04_plain_wire_value(message)
        assert type(snapshot) is dict
        messages.append(snapshot)
        if snapshot.get("phase") == "reserve":
            return snapshot["projected_cursor"]
        return None

    bind = getattr(dock, "_bind_host_transport", None)
    assert callable(bind)
    bind(transport)
    dock.open_head_preview()
    request_ids = _fix04_pending_ids(dock, dock, "preview_opened")
    assert len(request_ids) == 1
    request_id = request_ids[0]
    context = dock.expected_preview_open(request_id)
    assert context is not None
    pending_before = dict(dock._pending)
    messages_before = list(messages)

    dock.discard_preview_open(request_id)

    assert dock._pending == pending_before
    assert dock.expected_preview_open(request_id) == context
    assert dock.pending_preview_open_count() == 1
    assert messages == messages_before

    private_discard = _fix04_c1_private_hosted_discard(dock)
    assert (
        private_discard(
            request_id,
            expected_kind="preview_opened",
            context=context,
        )
        is True
    )
    assert dock.expected_preview_open(request_id) is None
    assert dock.pending_preview_open_count() == 0


def test_fix04_c1_hosted_enqueue_exception_cancels_both_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _dock_module, dock = _fix04_c1_standalone_dock(monkeypatch)
    messages: list[dict[str, object]] = []
    reservations: dict[int, dict[str, object]] = {}
    cancelled: list[int] = []
    raw_commands: list[object] = []
    dock.request.connect(raw_commands.append)

    def transport(message: object) -> object:
        snapshot = _fix04_plain_wire_value(message)
        assert type(snapshot) is dict
        messages.append(snapshot)
        phase = snapshot.get("phase")
        request_id = snapshot.get("request_id")
        if phase == "reserve":
            request_id = snapshot["projected_cursor"]
            assert type(request_id) is int
            reservations[request_id] = snapshot
            return request_id
        if phase == "enqueue":
            raise RuntimeError("synthetic hosted enqueue failure")
        if phase == "cancel":
            assert type(request_id) is int
            cancelled.append(request_id)
            reservations.pop(request_id, None)
        return None

    bind = getattr(dock, "_bind_host_transport", None)
    assert callable(bind)
    bind(transport)

    with pytest.raises(RuntimeError, match="synthetic hosted enqueue failure"):
        dock.start()

    request_id = messages[0]["projected_cursor"]
    assert type(request_id) is int
    assert [message["phase"] for message in messages] == [
        "reserve",
        "enqueue",
        "cancel",
    ]
    assert cancelled == [request_id]
    assert reservations == {}
    assert dock._pending == {}
    assert dock._hosted_projection is None
    assert raw_commands == []

    transport({"phase": "cancel", "request_id": request_id})
    assert cancelled == [request_id, request_id]
    assert reservations == {}


def test_fix04_c1_hosted_enqueue_has_no_raw_dock_signal_but_standalone_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dock_module, standalone = _fix04_c1_standalone_dock(monkeypatch)
    standalone_raw: list[object] = []
    standalone.request.connect(standalone_raw.append)
    standalone.start()
    assert standalone_raw == [
        {
            "schema_version": 1,
            "request_id": 0,
            "kind": "connect",
        }
    ]

    hosted = dock_module.ReviewDock()
    hosted_raw: list[object] = []
    messages: list[dict[str, object]] = []
    hosted.request.connect(hosted_raw.append)

    def transport(message: object) -> object:
        snapshot = _fix04_plain_wire_value(message)
        assert type(snapshot) is dict
        messages.append(snapshot)
        if snapshot.get("phase") == "reserve":
            return snapshot["projected_cursor"]
        return None

    bind = getattr(hosted, "_bind_host_transport", None)
    assert callable(bind)
    bind(transport)
    hosted.start()

    assert [message["phase"] for message in messages] == [
        "reserve",
        "enqueue",
    ]
    assert hosted_raw == []


def test_fix04_c1_gateway_checkout_authority_cap_counts_recovery_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    connected = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 0,
            "kind": "connect",
        }
    )
    assert connected["kind"] == "connected"
    capacities = _fix04_c1_checkout_capacities(gateway, gateway_module)
    capacity = capacities[0] if len(capacities) == 1 else 2
    assert 1 <= capacity <= 1024
    retain_failure = _fix04_c1_exact_method(
        gateway,
        "retain",
        "acquisition",
        "failure",
    )
    retain_acquired = _fix04_c1_exact_method(gateway, "retain", "acquired")
    source = {
        "kind": "head",
        "project_id": "project_" + "1" * 32,
    }
    recovery = RuntimeError("synthetic retained recovery authority")
    recovery.recovery_required = True
    recovery.checkout_id = "checkout_" + "0" * 32
    recovery.source = dict(source)
    recovery.open_key = "checkout_open_" + "0" * 32
    recovery.descriptor = None
    recovery.cleanup_error = None
    retain_failure(recovery)
    for index in range(1, capacity):
        checkout_id = f"checkout_{index:032x}"
        retain_acquired(
            {
                "source": dict(source),
                "open_key": f"checkout_open_{index:032x}",
                "descriptor": {"checkout_id": checkout_id},
            }
        )
    checkout_maps = [
        value
        for name, value in vars(gateway).items()
        if "checkout" in name.lower() and type(value) is dict
    ]
    assert len(checkout_maps) == 1
    checkouts = checkout_maps[0]
    assert len(checkouts) == capacity
    assert any(
        getattr(record, "phase", None) in {"recovery", "close_uncertain"}
        for record in checkouts.values()
    )
    open_calls_before = sum(call[0] == "open_checkout" for call in client.calls)

    attempted = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 1,
            "kind": "preview_open",
            "source": dict(source),
            "open_key": "checkout_open_" + "f" * 32,
        }
    )

    assert sum(call[0] == "open_checkout" for call in client.calls) == open_calls_before
    assert len(capacities) == 1, "missing one exact checkout authority cap"
    assert attempted["kind"] == "error"
    assert attempted["operation"] == "preview_open"
    assert len(checkouts) == capacity
    assert any(
        getattr(record, "phase", None) in {"recovery", "close_uncertain"}
        for record in checkouts.values()
    )


@pytest.mark.parametrize("poison", ("modified", "descriptor-drift"))
def test_fix02_one_bad_binding_keeps_aggregate_eligibility_sticky_false(
    monkeypatch: pytest.MonkeyPatch,
    poison: str,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.preview is not None
    assert session.dock is not None
    coordinator = session.preview
    dock = session.dock
    client = clients[0]
    head_checkout = "checkout_" + "6" * 32
    head_binding = coordinator.binding_for_checkout(head_checkout)
    coordinator._disabled.clear()
    dock.set_preview_eligibility(True)
    if poison == "modified":
        head_binding.document.Modified = True
    else:
        client.checkout_descriptors[head_checkout]["source_head"]["generation"] = 3
    before = host.workbench_snapshot()["heartbeat_count"]
    try:
        dock.refresh()
        pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] >= before + 5)
        assert dock.preview_projection().review_eligible is False

        if poison == "modified":
            head_binding.document.Modified = False
        else:
            client.checkout_descriptors[head_checkout]["source_head"]["generation"] = 2
        before = host.workbench_snapshot()["heartbeat_count"]
        dock.refresh()
        pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] >= before + 5)
        assert dock.preview_projection().review_eligible is False
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix02_private_host_guard_and_single_discard_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.preview is not None
    assert session.dock is not None
    head_checkout = "checkout_" + "6" * 32
    guard = getattr(session, "_guard_preview_binding", None)
    discard = getattr(session, "_discard_preview_binding", None)
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    try:
        assert callable(guard)
        assert callable(discard)
        assert not hasattr(session.dock, "_guard_preview_binding")
        assert not hasattr(session.dock, "_discard_preview_binding")
        assert session.dock.accept_button.object_name == "VibeCADAcceptDraft"
        assert session.dock.reject_button.object_name == "VibeCADRejectDraft"
        assert session.dock.accept_button.enabled is False
        assert session.dock.reject_button.enabled is False

        binding = session.preview.binding_for_checkout(head_checkout)
        failures: queue.Queue[BaseException] = queue.Queue()

        def wrong_thread_guard() -> None:
            try:
                guard(head_checkout)
            except BaseException as error:
                failures.put(error)

        thread = threading.Thread(target=wrong_thread_guard)
        thread.start()
        thread.join(0.5)
        assert not thread.is_alive()
        assert isinstance(failures.get_nowait(), RuntimeError)

        before = len(events)
        discard(head_checkout)
        assert binding.document_name not in freecad.documents
        assert len(freecad.documents) == 1
        client = clients[0]
        pump_main_events(
            lambda: any(
                call[0] == "close_checkout" and call[1]["checkout_id"] == head_checkout
                for call in client.calls
            )
        )
        assert events[before : before + 2] == [
            "document.close",
            f"checkout.close:{head_checkout}",
        ]
        close_calls = [call for call in client.calls if call[0] == "close_checkout"]
        assert [call[1]["checkout_id"] for call in close_calls].count(head_checkout) == 1
        with pytest.raises(preview_error):
            guard(head_checkout)
    finally:
        force_cleanup_workbench(host, freecad)


def _fix04_refresh_cycle(
    host: object,
    dock: object,
    client: FakeLocalAgentClient,
) -> None:
    before_heartbeat = host.workbench_snapshot()["heartbeat_count"]
    before_refreshes = sum(call[0] == "get_checkout" for call in client.calls)
    dock.refresh()
    pump_main_events(
        lambda: (
            sum(call[0] == "get_checkout" for call in client.calls) >= before_refreshes + 2
            and host.workbench_snapshot()["heartbeat_count"] >= before_heartbeat + 5
        )
    )


def _fix04_wrapper_type(role: str, *modules: ModuleType) -> type[object]:
    candidates: list[type[object]] = []
    for module in modules:
        for name, value in vars(module).items():
            lowered = name.lower()
            if (
                isinstance(value, type)
                and name.startswith("_")
                and role in lowered
                and any(marker in lowered for marker in ("private", "authenticated", "wire"))
                and all(value is not candidate for candidate in candidates)
            ):
                candidates.append(value)
    assert len(candidates) == 1, f"missing exact private {role} wrapper"
    return candidates[0]


def _fix04_wrap(
    wrapper_type: type[object],
    payload: dict[str, object],
    capability: object,
) -> object:
    parameters = inspect.signature(wrapper_type).parameters
    payload_names = [
        name
        for name in parameters
        if any(marker in name.lower() for marker in ("payload", "message", "command", "event"))
    ]
    capability_names = [
        name
        for name in parameters
        if any(marker in name.lower() for marker in ("capability", "authority", "seal"))
    ]
    assert len(parameters) == 2
    assert len(payload_names) == 1
    assert len(capability_names) == 1
    return wrapper_type(
        **{
            payload_names[0]: payload,
            capability_names[0]: capability,
        }
    )


def _fix04_plain_wire_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _fix04_plain_wire_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_fix04_plain_wire_value(item) for item in value]
    return value


def _fix04_wrapper_payload(wrapper: object) -> dict[str, object]:
    values = (
        [getattr(wrapper, field.name) for field in fields(wrapper)]
        if is_dataclass(wrapper)
        else list(vars(wrapper).values())
    )
    payloads = [value for value in values if isinstance(value, Mapping)]
    assert len(payloads) == 1
    payload = _fix04_plain_wire_value(payloads[0])
    assert type(payload) is dict
    return payload


def _fix04_wrapper_capability(wrapper: object) -> object:
    values = (
        [getattr(wrapper, field.name) for field in fields(wrapper)]
        if is_dataclass(wrapper)
        else list(vars(wrapper).values())
    )
    capabilities = [value for value in values if not isinstance(value, Mapping)]
    assert len(capabilities) == 1
    return capabilities[0]


def _fix04_session_capability(session: object) -> object:
    owners = (
        session,
        getattr(session, "worker", None),
        getattr(getattr(session, "worker", None), "gateway", None),
    )
    capabilities: list[object] = []
    for owner in owners:
        if owner is None:
            continue
        for name, value in vars(owner).items():
            if (
                any(marker in name.lower() for marker in ("capability", "authority", "seal"))
                and not callable(value)
                and all(value is not candidate for candidate in capabilities)
            ):
                capabilities.append(value)
    assert len(capabilities) == 1, "missing shared per-session wire capability"
    return capabilities[0]


def _fix04_gateway(
    gateway_module: ModuleType,
    client_factory: Callable[[], object],
    capability: object,
) -> object:
    gateway_type = gateway_module.KernelGateway
    parameters = inspect.signature(gateway_type).parameters
    capability_names = [
        name
        for name in parameters
        if any(marker in name.lower() for marker in ("capability", "authority", "seal"))
    ]
    factory_names = [
        name for name in parameters if "factory" in name.lower() or "client" in name.lower()
    ]
    assert len(capability_names) == 1, "gateway must bind one exact capability"
    assert len(factory_names) == 1
    return gateway_type(
        **{
            capability_names[0]: capability,
            factory_names[0]: client_factory,
        }
    )


def _fix04_private_gateway_command(
    gateway_module: ModuleType,
    payload: dict[str, object],
    capability: object,
) -> object:
    return _fix04_wrap(
        _fix04_wrapper_type("command", gateway_module),
        payload,
        capability,
    )


def _fix04_pending_ids(
    session: object,
    dock: object,
    expected_kind: str,
) -> tuple[int, ...]:
    found: set[int] = set()
    for owner in (session, dock):
        for name, value in vars(owner).items():
            if "pending" not in name.lower() or type(value) is not dict:
                continue
            for request_id, pending in value.items():
                if (
                    type(request_id) is int
                    and type(pending) is tuple
                    and pending
                    and pending[0] == expected_kind
                ):
                    found.add(request_id)
    return tuple(sorted(found))


def _fix04_authenticated_review_commands(session: object) -> tuple[dict[str, object], ...]:
    worker = getattr(session, "worker", None)
    gateway = getattr(worker, "gateway", None)
    replays = getattr(gateway, "_replays", None)
    assert type(replays) is dict
    commands: list[dict[str, object]] = []
    for replay in replays.values():
        command = getattr(replay, "command", None)
        if (
            getattr(replay, "authenticated", None) is True
            and type(command) is dict
            and command.get("kind") == "review"
        ):
            commands.append(dict(command))
    return tuple(commands)


def _fix04_worker_round_trip(session: object, command: object) -> object:
    worker = getattr(session, "worker", None)
    thread = getattr(session, "thread", None)
    assert worker is not None
    assert thread is not None
    post = getattr(thread, "post", None)
    assert callable(post), "this deterministic transport test requires fake Qt"
    received: list[object] = []
    completed = threading.Event()

    def collect(event: object) -> None:
        received.append(event)
        completed.set()

    worker.event_ready.connect(collect)
    post(worker.dispatch, (command,))
    assert completed.wait(1.0)
    assert len(received) == 1
    return received[0]


def _fix04_request_review(session: object, *, decision: str) -> object:
    names = ("_request_review", "_submit_review", "_dispatch_review")
    methods = [getattr(session, name, None) for name in names]
    available = [method for method in methods if callable(method)]
    assert len(available) == 1, "missing Host-only review dispatch seam"
    return available[0](
        decision=decision,
        task_id="task_" + "1" * 32,
        draft_id="draft_" + "4" * 32,
        expected_generation=3,
    )


def test_c02_host_seam_binds_once_without_review_ui_or_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    review_calls_before = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
    try:
        bind = getattr(dock, "_bind_review_host", None)
        submit = getattr(dock, "_submit_host_review", None)
        discard = getattr(dock, "_discard_host_preview", None)

        assert callable(bind)
        assert callable(submit)
        assert callable(discard)
        assert dock.accept_button.objectName() == "VibeCADAcceptDraft"
        assert dock.reject_button.objectName() == "VibeCADRejectDraft"
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        assert (
            dock.request_review(
                decision="accept",
                task_id="task_" + "1" * 32,
                draft_id="draft_" + "4" * 32,
                expected_generation=3,
            )
            is None
        )
        with pytest.raises(preview_error, match="fresh shared review authority required"):
            submit(
                decision="accept",
                task_id="task_" + "1" * 32,
                draft_id="draft_" + "4" * 32,
                expected_generation=3,
            )
        with pytest.raises(RuntimeError, match="review host is already bound"):
            bind(
                submit_review=lambda **_payload: None,
                discard_preview=lambda _checkout_id: None,
            )

        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == review_calls_before
        )
    finally:
        force_cleanup_workbench(host, freecad)


def test_c02_authenticated_correlated_review_completion_and_error_deliver_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    gateway_module = importlib.import_module("vibecad_workbench.gateway")
    capability = _fix04_session_capability(session)
    event_type = _fix04_wrapper_type("event", gateway_module)
    entered = threading.Event()
    release = threading.Event()
    client.review_entered = entered
    client.review_release = release
    deliveries: list[tuple[dict[str, object], object, int]] = []

    def receive_completion(event: object, context: object) -> None:
        assert type(event) is dict
        assert _fix04_pending_ids(session, dock, "review") == ()
        deliveries.append((dict(event), context, threading.get_ident()))

    try:
        monkeypatch.setattr(dock, "_receive_host_review_completion", receive_completion)
        _fix04_refresh_cycle(host, dock, client)
        first_context = session._selection_stamp()
        first_request = dock._submit_host_review(
            decision="accept",
            task_id="task_" + "1" * 32,
            draft_id="draft_" + "4" * 32,
            expected_generation=3,
        )
        assert entered.wait(1.0)
        assert _fix04_pending_ids(session, dock, "review") == (first_request,)
        success = {
            "schema_version": 1,
            "request_id": first_request,
            "kind": "review",
            "response": {
                "schema_version": 1,
                "ok": True,
                "result": {},
                "error": None,
            },
        }

        session._receive(dict(success))
        session._receive(
            _fix04_wrap(
                event_type,
                dict(success) | {"request_id": first_request + 1},
                capability,
            )
        )
        assert deliveries == []
        assert _fix04_pending_ids(session, dock, "review") == (first_request,)

        release.set()
        pump_main_events(lambda: len(deliveries) == 1)
        delivered_success, delivered_context, delivered_thread = deliveries[0]
        assert delivered_success["request_id"] == first_request
        assert delivered_success["kind"] == "review"
        assert dock._authenticated_ok(delivered_success["response"])
        assert delivered_context == first_context
        assert delivered_thread == session.main_thread_id

        session._receive(_fix04_wrap(event_type, success, capability))
        session._receive(dict(success))
        assert len(deliveries) == 1

        client.review_entered = None
        client.review_release = None
        client.review_task_generation = 3
        client.review_task_status = "awaiting_user_review"
        client.review_committed_revision = None
        client.review_project_generation = 2
        client.review_project_revision = "revision_" + "1" * 32
        _fix04_refresh_cycle(host, dock, client)
        second_context = session._selection_stamp()
        client.review_failure = True
        second_request = dock._submit_host_review(
            decision="accept",
            task_id="task_" + "1" * 32,
            draft_id="draft_" + "4" * 32,
            expected_generation=3,
        )
        pump_main_events(lambda: len(deliveries) == 2)
        unknown = {
            "schema_version": 1,
            "request_id": second_request,
            "kind": "error",
            "operation": "review",
            "code": "internal_error",
            "outcome": "unknown_outcome",
        }

        assert deliveries[1] == (unknown, second_context, session.main_thread_id)
        assert _fix04_pending_ids(session, dock, "review") == ()
        assert session.lifecycle == "stopping"
        assert dock.preview_projection().recovery_required is True
        session._receive(_fix04_wrap(event_type, unknown, capability))
        session._receive(dict(unknown))
        assert len(deliveries) == 2
        assert sum(call[0] == "accept_draft" for call in client.calls) == 2
    finally:
        release.set()
        force_cleanup_workbench(host, freecad)


def test_c02_host_discard_port_keeps_document_before_checkout_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    client = clients[0]
    checkout_id = dock._preview_checkouts["head"]
    binding = session.preview.binding_for_checkout(checkout_id)
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    before = len(events)
    try:
        dock._discard_host_preview(checkout_id)
        pump_main_events(
            lambda: any(
                call[0] == "close_checkout" and call[1]["checkout_id"] == checkout_id
                for call in client.calls
            )
        )

        assert events[before : before + 2] == [
            "document.close",
            f"checkout.close:{checkout_id}",
        ]
        assert binding.document_name not in freecad.documents
        assert [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ].count(checkout_id) == 1
        assert client.close_call_count == 0
        assert all(getattr(value, "__self__", None) is not client for value in vars(dock).values())
        with pytest.raises(preview_error):
            dock._discard_host_preview(checkout_id)
        assert [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ].count(checkout_id) == 1
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "delivery_order",
    (
        pytest.param("old-close-first", id="pending-successor-after-old-close"),
        pytest.param("successor-open-first", id="pending-successor-before-old-close"),
    ),
)
def test_c02_state_model_pending_successor_waits_for_host_cycle_finalization(
    monkeypatch: pytest.MonkeyPatch,
    delivery_order: str,
) -> None:
    host, freecad, _freecad_gui, clients, _events = _start_fix02_host(
        monkeypatch,
        client_factory=lambda: FakeLocalAgentClient(
            monotonic_preview_authorities=True,
        ),
    )
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    dock.open_head_button.click()
    pump_main_events(lambda: len(freecad.documents) == 1)
    assert session.preview is not None
    coordinator = session.preview
    head_id = dock._preview_checkouts["head"]
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    coordinator.poison_binding(head_id)
    assert cycle.poisoned is True

    original_close_checkout = client.close_checkout
    first_close_entered = threading.Event()
    first_close_release = threading.Event()
    successor_close_entered = threading.Event()
    successor_close_release = threading.Event()
    close_index = 0

    def block_two_closes(*, checkout_id: str) -> dict[str, object]:
        nonlocal close_index
        response = original_close_checkout(checkout_id=checkout_id)
        close_index += 1
        if close_index == 1:
            first_close_entered.set()
            if not first_close_release.wait(2.0):
                raise RuntimeError("synthetic old close release deadline exceeded")
        elif close_index == 2:
            successor_close_entered.set()
            if not successor_close_release.wait(2.0):
                raise RuntimeError("synthetic successor close release deadline exceeded")
        return response

    original_event_emit = session.worker.event_ready.emit
    held_old_close: list[object] = []
    old_close_held = threading.Event()

    def order_preview_events(event: object) -> None:
        detached = (
            _fix04_plain_wire_value(event)
            if isinstance(event, Mapping)
            else _fix04_wrapper_payload(event)
        )
        assert type(detached) is dict
        payload = detached
        response = payload.get("response")
        if (
            delivery_order == "successor-open-first"
            and payload.get("kind") == "preview_closed"
            and type(response) is dict
            and response.get("checkout_id") == head_id
            and not held_old_close
        ):
            held_old_close.append(event)
            old_close_held.set()
            return
        original_event_emit(event)

    finalized: list[int] = []
    original_finalize = coordinator._finalize_retired_cycle

    def count_finalize(retired_cycle_id: object) -> None:
        assert type(retired_cycle_id) is int
        finalized.append(retired_cycle_id)
        original_finalize(retired_cycle_id)

    try:
        monkeypatch.setattr(client, "close_checkout", block_two_closes)
        monkeypatch.setattr(session.worker.event_ready, "emit", order_preview_events)
        monkeypatch.setattr(coordinator, "_finalize_retired_cycle", count_finalize)

        dock._discard_host_preview(head_id)
        assert first_close_entered.wait(1.0)
        assert session._cleanup_cycle_id == cycle_id
        assert session._cleanup_checkout_id == head_id
        assert coordinator._cycle is cycle
        assert cycle.poisoned is True

        dock.open_draft_button.click()
        assert dock.pending_preview_open_count() == 1
        assert session._review_tokens == {}
        assert session._fresh_preview_descriptors == {}
        assert not any(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)

        first_close_release.set()
        if delivery_order == "successor-open-first":
            assert old_close_held.wait(1.0)
            _fix04_settle_worker(session)
            pump_main_events(lambda: dock.pending_preview_open_count() == 0)
            draft_open_calls = [
                call
                for call in client.calls
                if call[0] == "open_checkout" and call[1]["source"]["kind"] == "draft"
            ]
            assert len(draft_open_calls) == 1
            successor_id = next(
                checkout_id for checkout_id in coordinator._owned if checkout_id != head_id
            )
            successor = coordinator._owned[successor_id]
            assert dock._preview_checkouts == {"head": head_id}
            assert successor.binding is None
            assert successor.document is None
            assert successor.document_closed is True
            assert coordinator._cycle is cycle
            assert cycle.poisoned is True
            assert finalized == []
            original_event_emit(held_old_close.pop())

        pump_main_events(lambda: successor_close_entered.is_set())
        successor_id = next(
            checkout_id for checkout_id in coordinator._owned if checkout_id != head_id
        )
        successor = coordinator._owned[successor_id]

        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert cycle.poisoned is True
        assert session._cleanup_cycle_id == cycle_id
        assert session._cleanup_checkout_id == successor_id
        assert tuple(coordinator._owned) == (head_id, successor_id)
        assert coordinator._owned[head_id].checkout_closed is True
        assert successor.binding is None
        assert successor.document is None
        assert successor.document_name is None
        assert successor.document_closed is True
        assert successor.checkout_closed is False
        assert freecad.documents == {}
        assert dock._preview_checkouts == {}
        assert dock.pending_preview_open_count() == 0
        assert session._review_tokens == {}
        assert session._fresh_preview_descriptors == {}
        assert session._refresh_candidates == {}
        assert finalized == []
        assert [call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"] == [
            head_id,
            successor_id,
        ]
        assert not any(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)

        successor_close_release.set()
        pump_main_events(
            lambda: (
                coordinator._active_cycle_id() is None
                and session._cleanup_cycle_id is None
                and not session._pending
            )
        )

        assert finalized == [cycle_id]
        assert dock._preview_recovery_required is False
        assert dock._preview_pending_sources == set()
        assert dock._preview_checkouts == {}

        dock.open_draft_button.click()
        pump_main_events(
            lambda: (
                dock._preview_checkouts.get("draft") not in {None, successor_id}
                and len(freecad.documents) == 1
            )
        )
        deliberate_successor_id = dock._preview_checkouts["draft"]
        assert deliberate_successor_id != successor_id
        assert coordinator._active_cycle_id() != cycle_id
        assert coordinator._cycle is not cycle
        assert coordinator._cycle is not None
        assert coordinator._cycle.poisoned is False
        assert coordinator.binding_for_checkout(deliberate_successor_id).document_name in (
            freecad.documents
        )
        assert finalized == [cycle_id]
    finally:
        first_close_release.set()
        successor_close_release.set()
        for event in held_old_close:
            original_event_emit(event)
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "refresh_outcome",
    (
        pytest.param("success", id="refresh-success"),
        pytest.param("error", id="refresh-error"),
    ),
)
def test_c02_active_last_close_refresh_plan_wakes_cycle_finalization(
    monkeypatch: pytest.MonkeyPatch,
    refresh_outcome: str,
) -> None:
    host, freecad, _freecad_gui, clients, _events = _start_fix02_host(
        monkeypatch,
        client_factory=lambda: FakeLocalAgentClient(
            monotonic_preview_authorities=True,
        ),
    )
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.worker is not None
    dock = session.dock
    client = clients[0]
    gateway = session.worker.gateway
    dock.open_head_button.click()
    pump_main_events(lambda: len(freecad.documents) == 1)
    coordinator = session.preview
    assert coordinator is not None
    head_id = dock._preview_checkouts["head"]
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int

    original_close_checkout = client.close_checkout
    close_entered = threading.Event()
    close_release = threading.Event()

    def block_last_close(*, checkout_id: str) -> dict[str, object]:
        response = original_close_checkout(checkout_id=checkout_id)
        close_entered.set()
        if not close_release.wait(2.0):
            raise RuntimeError("synthetic last close release deadline exceeded")
        return response

    original_get_checkout = client.get_checkout

    def refresh_checkout(*, checkout_id: str) -> dict[str, object]:
        if refresh_outcome == "error":
            client._record("get_checkout", {"checkout_id": checkout_id})
            raise RuntimeError("synthetic overlapping refresh failure")
        return original_get_checkout(checkout_id=checkout_id)

    original_handle = gateway.handle

    def preserve_inflight_refresh_success(command: object) -> object:
        payload = dict(command) if type(command) is dict else _fix04_wrapper_payload(command)
        if refresh_outcome == "success" and payload.get("kind") == "preview_refresh":
            response = _fix04_plain_wire_value(client.checkout_descriptors[head_id])
            assert type(response) is dict
            return {
                "schema_version": 1,
                "request_id": payload["request_id"],
                "kind": "preview_refreshed",
                "response": response,
            }
        return original_handle(command)

    original_event_emit = session.worker.event_ready.emit
    observed: list[tuple[str, object]] = []

    def observe_event(event: object) -> None:
        detached = (
            _fix04_plain_wire_value(event)
            if isinstance(event, Mapping)
            else _fix04_wrapper_payload(event)
        )
        assert type(detached) is dict
        kind = detached.get("kind")
        assert type(kind) is str
        observed.append((kind, detached.get("operation")))
        original_event_emit(event)

    finalized: list[int] = []
    original_finalize = coordinator._finalize_retired_cycle

    def count_finalize(retired_cycle_id: object) -> None:
        assert type(retired_cycle_id) is int
        finalized.append(retired_cycle_id)
        original_finalize(retired_cycle_id)

    try:
        monkeypatch.setattr(client, "close_checkout", block_last_close)
        monkeypatch.setattr(client, "get_checkout", refresh_checkout)
        monkeypatch.setattr(gateway, "handle", preserve_inflight_refresh_success)
        monkeypatch.setattr(session.worker.event_ready, "emit", observe_event)
        monkeypatch.setattr(coordinator, "_finalize_retired_cycle", count_finalize)

        dock._discard_host_preview(head_id)
        assert close_entered.wait(1.0)
        assert freecad.documents == {}
        assert session.lifecycle == "active"
        assert session._cleanup_cycle_id == cycle_id
        assert session._cleanup_checkout_id == head_id

        dock.refresh()
        barrier = session._refresh_barrier
        assert barrier is not None
        assert barrier.cycle_id == cycle_id
        assert barrier.bindings == (("head", head_id, id(coordinator._owned[head_id].binding)),)
        assert [pending[2] for pending in session._pending.values() if pending[3] == "normal"] == [
            "refresh_project",
            "refresh_task",
            "preview_refresh",
            "list_tasks",
        ]

        close_release.set()
        _fix04_settle_worker(session)
        pump_main_events(lambda: not session._pending and not dock._pending)

        expected_preview = (
            ("preview_refreshed", None)
            if refresh_outcome == "success"
            else ("error", "preview_refresh")
        )
        assert observed == [
            ("preview_closed", None),
            ("project", None),
            ("task", None),
            expected_preview,
            ("tasks", None),
        ]
        assert session.lifecycle == "active"
        assert host._session is session
        assert finalized == [cycle_id]
        assert coordinator._active_cycle_id() is None
        assert session._cleanup_cycle_id is None
        assert session._refresh_barrier is None
        assert session._refresh_candidates == {}
        assert session._review_tokens == {}
        assert session._fresh_preview_descriptors == {}
        assert dock._preview_checkouts == {}
        assert client.close_call_count == 0

        opens_before = sum(call[0] == "open_checkout" for call in client.calls)
        dock.open_head_button.click()
        pump_main_events(
            lambda: (
                len(freecad.documents) == 1
                and sum(call[0] == "open_checkout" for call in client.calls) == opens_before + 1
            )
        )
        successor_id = dock._preview_checkouts["head"]
        assert successor_id != head_id
        assert coordinator._active_cycle_id() != cycle_id
        assert coordinator._owned[successor_id].cleanup_only is False
        assert finalized == [cycle_id]
    finally:
        close_release.set()
        force_cleanup_workbench(host, freecad)


def test_c02_host_projects_exact_discard_and_resets_only_after_full_cycle_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, _freecad_gui, clients, events = _start_fix02_host(
        monkeypatch,
        client_factory=lambda: FakeLocalAgentClient(
            monotonic_preview_authorities=True,
        ),
    )
    dock_for_open = host._session.dock
    dock_for_open.open_head_button.click()
    dock_for_open.open_draft_button.click()
    pump_main_events(lambda: len(freecad.documents) == 2)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    initial = dict(dock._preview_checkouts)
    head_id = initial["head"]
    draft_id = initial["draft"]
    head_binding = coordinator.binding_for_checkout(head_id)
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    before = len(events)
    try:
        coordinator.poison_binding(head_id)
        assert cycle.poisoned is True
        dock.set_preview_eligibility(False, recovery_required=True)
        dock._discard_host_preview(head_id)
        pump_main_events(
            lambda: (
                [
                    call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
                ].count(head_id)
                == 1
                and dock._preview_checkouts == {"draft": draft_id}
            )
        )

        assert events[before : before + 2] == [
            "document.close",
            f"checkout.close:{head_id}",
        ]
        assert head_binding.document_name not in freecad.documents
        assert dock._preview_checkouts == {"draft": draft_id}
        assert dock._preview_recovery_required is True
        assert dock.open_head_button.enabled is True
        assert client.close_call_count == 0
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert cycle.poisoned is True

        dock.open_head_button.click()
        pump_main_events(
            lambda: (
                set(dock._preview_checkouts) == {"head", "draft"}
                and dock._preview_checkouts["head"] != head_id
                and len(freecad.documents) == 2
            )
        )
        replacement_id = dock._preview_checkouts["head"]
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert cycle.poisoned is True
        assert coordinator.aggregate_review_eligible() is False
        assert dock.preview_projection().review_eligible is False

        dock._discard_host_preview(draft_id)
        pump_main_events(
            lambda: (
                [
                    call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
                ].count(draft_id)
                == 1
                and dock._preview_checkouts == {"head": replacement_id}
            )
        )
        assert dock._preview_checkouts == {"head": replacement_id}
        assert coordinator._active_cycle_id() is not None
        assert dock._preview_recovery_required is True

        dock._discard_host_preview(replacement_id)
        pump_main_events(
            lambda: (
                coordinator._active_cycle_id() is None
                and dock._preview_checkouts == {}
                and not session._pending
            )
        )
        closed_ids = [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ]
        assert closed_ids.count(head_id) == 1
        assert closed_ids.count(draft_id) == 1
        assert closed_ids.count(replacement_id) == 1
        assert session._review_tokens == {}
        assert session._fresh_preview_descriptors == {}
        assert session._refresh_candidates == {}
        assert dock._preview_pending_sources == set()
        assert dock._preview_recovery_required is False
        assert client.close_call_count == 0

        dock.open_head_button.click()
        dock.open_draft_button.click()
        pump_main_events(
            lambda: (
                set(dock._preview_checkouts) == {"head", "draft"} and len(freecad.documents) == 2
            )
        )
        _fix04_refresh_cycle(host, dock, client)
        assert coordinator.aggregate_review_eligible() is True
        assert dock.preview_projection().review_eligible is True
    finally:
        force_cleanup_workbench(host, freecad)


def test_c02_active_discard_resumes_after_inflight_refresh_retires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, _freecad_gui, clients, events = _start_fix02_host(
        monkeypatch,
        client_factory=lambda: FakeLocalAgentClient(
            monotonic_preview_authorities=True,
        ),
    )
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    dock.open_head_button.click()
    dock.open_draft_button.click()
    pump_main_events(lambda: len(freecad.documents) == 2)
    coordinator = session.preview
    assert coordinator is not None
    client = clients[0]
    head_id = dock._preview_checkouts["head"]
    draft_id = dock._preview_checkouts["draft"]
    head_binding = coordinator.binding_for_checkout(head_id)
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    coordinator.poison_binding(head_id)
    assert cycle.poisoned is True
    original_get_checkout = client.get_checkout
    refresh_entered = threading.Event()
    refresh_release = threading.Event()
    refresh_calls = 0

    def block_first_refresh(*, checkout_id: str) -> dict[str, object]:
        nonlocal refresh_calls
        refresh_calls += 1
        response = original_get_checkout(checkout_id=checkout_id)
        if refresh_calls == 1:
            refresh_entered.set()
            if not refresh_release.wait(1.0):
                raise RuntimeError("synthetic refresh release deadline exceeded")
        return response

    before_events = len(events)
    try:
        monkeypatch.setattr(client, "get_checkout", block_first_refresh)
        dock.refresh()
        assert refresh_entered.wait(1.0)
        assert session._refresh_barrier is not None
        assert session._refresh_barrier.cycle_id == cycle_id
        assert len(_fix04_pending_ids(session, dock, "preview_refreshed")) == 2

        dock._discard_host_preview(head_id)
        assert head_binding.document_name not in freecad.documents
        assert session._cleanup_cycle_id == cycle_id
        assert session._refresh_barrier is None
        assert coordinator._cycle is cycle
        assert cycle.poisoned is True
        assert not any(
            call[0] == "close_checkout" and call[1]["checkout_id"] == head_id
            for call in client.calls
        )

        refresh_release.set()
        _fix04_settle_worker(session)
        pump_main_events(
            lambda: (
                [
                    call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
                ].count(head_id)
                == 1
                and dock._preview_checkouts == {"draft": draft_id}
            )
        )

        assert events[before_events : before_events + 2] == [
            "document.close",
            f"checkout.close:{head_id}",
        ]
        assert _fix04_pending_ids(session, dock, "preview_refreshed") == ()
        assert dock._preview_checkouts == {"draft": draft_id}
        assert dock.open_head_button.enabled is True
        assert client.close_call_count == 0
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert cycle.poisoned is True
        unchanged_projection = dict(dock._preview_checkouts)
        dock._project_host_preview_closed("checkout_" + "f" * 32)
        assert dock._preview_checkouts == unchanged_projection

        dock.open_head_button.click()
        pump_main_events(
            lambda: (
                set(dock._preview_checkouts) == {"head", "draft"}
                and dock._preview_checkouts["head"] != head_id
                and len(freecad.documents) == 2
            )
        )
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert cycle.poisoned is True
        assert coordinator.aggregate_review_eligible() is False
        assert dock.preview_projection().review_eligible is False
    finally:
        refresh_release.set()
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "first_close_outcome",
    ("success", "effect-then-raise-reconciled"),
)
def test_c02_deactivate_overlapping_active_discard_closes_remaining_cycle(
    monkeypatch: pytest.MonkeyPatch,
    first_close_outcome: str,
) -> None:
    host, freecad, _freecad_gui, clients, events = _start_fix02_host(
        monkeypatch,
        client_factory=lambda: FakeLocalAgentClient(
            monotonic_preview_authorities=True,
        ),
    )
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    dock.open_head_button.click()
    dock.open_draft_button.click()
    pump_main_events(lambda: len(freecad.documents) == 2)
    coordinator = session.preview
    assert coordinator is not None
    client = clients[0]
    head_id = dock._preview_checkouts["head"]
    draft_id = dock._preview_checkouts["draft"]
    head_binding = coordinator.binding_for_checkout(head_id)
    draft_binding = coordinator.binding_for_checkout(draft_id)
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    original_close_checkout = client.close_checkout
    close_entered = threading.Event()
    close_release = threading.Event()
    first_close = True
    before_events = len(events)

    def block_first_close(*, checkout_id: str) -> dict[str, object]:
        nonlocal first_close
        response = original_close_checkout(checkout_id=checkout_id)
        if first_close:
            first_close = False
            close_entered.set()
            if not close_release.wait(1.0):
                raise RuntimeError("synthetic overlapping close release deadline exceeded")
            if first_close_outcome == "effect-then-raise-reconciled":
                client.checkout_descriptors[checkout_id] = dict(response)
                raise RuntimeError("synthetic checkout close effect then raise")
        return response

    try:
        monkeypatch.setattr(client, "close_checkout", block_first_close)
        dock._discard_host_preview(head_id)
        assert close_entered.wait(1.0)
        assert head_binding.document_name not in freecad.documents
        assert draft_binding.document_name in freecad.documents
        assert session._cleanup_request_id is not None
        assert session._cleanup_checkout_id == head_id

        host.deactivate_workbench()

        assert session.lifecycle == "stopping"
        assert session._cleanup_retry_requested is True
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert draft_binding.document_name not in freecad.documents
        assert freecad.documents == {}
        assert client.close_call_count == 0

        close_release.set()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

        close_checkout_ids = [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ]
        assert close_checkout_ids.count(head_id) == 1
        assert close_checkout_ids.count(draft_id) == 1
        assert events[before_events:] == [
            "document.close",
            f"checkout.close:{head_id}",
            "document.close",
            f"checkout.close:{draft_id}",
            "client.close",
        ]
        assert freecad.documents == {}
        assert dock._preview_checkouts == {}
        assert coordinator._active_cycle_id() is None
        assert session._cleanup_request_id is None
        assert session._cleanup_checkout_id is None
        assert session._cleanup_retry_requested is False
        assert client.close_call_count == 1
        assert session._thread_retired is True
        assert host._session is None
    finally:
        close_release.set()
        force_cleanup_workbench(host, freecad)


def _fix04_replay_capacity(gateway: object, gateway_module: ModuleType) -> int:
    capacities: set[int] = set()
    for owner in (gateway, gateway_module):
        for name, value in vars(owner).items():
            lowered = name.lower()
            if (
                "replay" in lowered
                and any(
                    marker in lowered
                    for marker in ("capacity", "limit", "maximum", "max", "window")
                )
                and type(value) is int
            ):
                capacities.add(value)
    assert len(capacities) == 1, "missing bounded replay capacity"
    capacity = capacities.pop()
    assert 1 <= capacity <= 1024
    return capacity


def _fix04_private_signal(session: object) -> object:
    signals = [
        value
        for value in vars(session).values()
        if callable(getattr(value, "connect", None)) and callable(getattr(value, "emit", None))
    ]
    assert len(signals) == 1, "Host must own exactly one queued Signal(object)"
    return signals[0]


def _fix04_settle_worker(session: object) -> None:
    thread = getattr(session, "thread", None)
    assert thread is not None
    post = getattr(thread, "post", None)
    assert callable(post), "this deterministic transport test requires fake Qt"
    settled = threading.Event()
    post(settled.set, ())
    assert settled.wait(1.0)


def _fix04_c1_standalone_dock(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, object]:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    return dock_module, dock_module.ReviewDock()


def _fix04_c1_private_hosted_discard(dock: object) -> Callable[..., object]:
    candidates = [
        getattr(dock, name)
        for name, value in vars(type(dock)).items()
        if (
            name.startswith("_")
            and "discard" in name.lower()
            and "host" in name.lower()
            and "pending" in name.lower()
            and callable(value)
        )
    ]
    assert len(candidates) == 1, "missing exact private hosted discard"
    return candidates[0]


def _fix04_c1_checkout_capacities(
    gateway: object,
    gateway_module: ModuleType,
) -> tuple[int, ...]:
    capacities: set[int] = set()
    for owner in (gateway, gateway_module):
        for name, value in vars(owner).items():
            lowered = name.lower()
            if (
                "checkout" in lowered
                and any(
                    marker in lowered for marker in ("capacity", "cap", "limit", "maximum", "max")
                )
                and type(value) is int
            ):
                capacities.add(value)
    return tuple(sorted(capacities))


def _fix04_c1_exact_method(owner: object, *markers: str) -> Callable[..., object]:
    candidates = [
        getattr(owner, name)
        for name, value in vars(type(owner)).items()
        if (
            name.startswith("_")
            and all(marker in name.lower() for marker in markers)
            and callable(value)
        )
    ]
    assert len(candidates) == 1, f"missing exact private method: {markers!r}"
    return candidates[0]


def _fix03_deterministic_open_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    import secrets as secrets_module

    digits = iter("89abcdef01234567")
    original = secrets_module.token_hex

    def token_hex(byte_count: int) -> str:
        return next(digits) * 32 if byte_count == 16 else original(byte_count)

    monkeypatch.setattr(secrets_module, "token_hex", token_hex)


def _fix03_gateway_open(
    gateway: object,
    *,
    request_id: int = 1,
    open_key: str = "checkout_open_" + "8" * 32,
) -> dict[str, object]:
    return gateway.handle(
        {
            "schema_version": 1,
            "request_id": request_id,
            "kind": "preview_open",
            "source": {
                "kind": "head",
                "project_id": "project_" + "1" * 32,
            },
            "open_key": open_key,
        }
    )


@pytest.mark.parametrize("corruption", ("malformed", "wrong-checkout"))
def test_fix03_bad_refresh_is_synchronously_false_and_cycle_sticky(
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    _fix03_deterministic_open_keys(monkeypatch)
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    client = clients[0]
    head_checkout = "checkout_" + "6" * 32
    draft_checkout = "checkout_" + "7" * 32
    invalid: object
    if corruption == "malformed":
        invalid = "not-a-refresh-mapping"
    else:
        invalid = dict(client.checkout_descriptors[draft_checkout])
    original_get_checkout = client.get_checkout

    def corrupt_head_refresh(*, checkout_id: str) -> object:
        if checkout_id == head_checkout:
            client._record("get_checkout", {"checkout_id": checkout_id})
            return invalid
        return original_get_checkout(checkout_id=checkout_id)

    try:
        assert dock.preview_projection().review_eligible is False
        monkeypatch.setattr(client, "get_checkout", corrupt_head_refresh)
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is False

        monkeypatch.setattr(client, "get_checkout", original_get_checkout)
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is False
        assert session.preview.aggregate_review_eligible() is False
    finally:
        host.deactivate_workbench()
        force_cleanup_workbench(host, freecad)


def test_fix04_legacy_unexpected_finish_activate_retains_same_session_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, _freecad_gui, clients, _events = _start_fix02_host(monkeypatch)
    session = host._session
    assert session is not None
    thread = session.thread
    assert thread is not None
    assert session.lifecycle == "active"
    assert clients[0].close_call_count == 0
    try:
        thread.quit()
        thread.join()
        pump_main_events(lambda: session.lifecycle != "active")

        assert host._session is session
        assert session.lifecycle == "stopping"
        assert host.workbench_snapshot()["lifecycle"] == "stopping"
        assert clients[0].close_call_count == 0
        host.activate_workbench()
        host.activate_workbench()
        assert host._session is session
        assert len(clients) == 1
        assert clients[0].close_call_count == 0
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_legacy_public_gateway_preview_close_is_never_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    opened = _fix03_gateway_open(gateway)
    checkout_id = "checkout_" + "6" * 32

    attempted = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 2,
            "kind": "preview_close",
            "checkout_id": checkout_id,
            "document_absent": True,
        }
    )

    assert opened["kind"] == "preview_opened"
    assert attempted["kind"] == "error"
    assert attempted["operation"] == "preview_close"
    assert attempted["code"] == "invalid_input"
    assert not any(call[0] == "close_checkout" for call in client.calls)
    assert client.close_call_count == 0


def test_fix04_legacy_stale_dock_close_stays_on_rejected_public_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix03_deterministic_open_keys(monkeypatch)
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    client = clients[0]
    checkout_id = "checkout_" + "6" * 32
    binding = session.preview.binding_for_checkout(checkout_id)
    transport = dock._host_transport
    assert callable(transport)
    transported: list[object] = []

    def observe_transport(message: object) -> object:
        transported.append(message)
        return transport(message)

    dock._host_transport = observe_transport
    heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
    sequence_before = dock._sequence
    dock_pending_before = dict(dock._pending)
    host_pending_before = dict(session._pending)
    hosted_projection_before = dock._hosted_projection
    retired_before = set(dock._retired_request_ids)
    status_before = dock.status_label.text
    preview_status_before = dock.preview_status_label.text
    preview_projection_before = dock.preview_projection()
    documents_before = dict(freecad.documents)
    calls_before = list(client.calls)
    try:
        results = (
            dock.request_preview_close(
                checkout_id,
                document_absent=True,
            ),
            dock.request_review(
                decision="accept",
                task_id="task_" + "1" * 32,
                draft_id="draft_" + "4" * 32,
                expected_generation=3,
            ),
            dock.request_client_close(),
            dock.request_close((checkout_id,)),
        )

        assert results == (None, None, None, None)
        assert transported == []
        assert host.workbench_snapshot()["heartbeat_count"] == heartbeat_before
        assert dock._sequence == sequence_before
        assert dock._pending == dock_pending_before
        assert session._pending == host_pending_before
        assert dock._hosted_projection == hosted_projection_before
        assert dock._retired_request_ids == retired_before
        assert dock.status_label.text == status_before
        assert dock.preview_status_label.text == preview_status_before
        assert dock.preview_projection() == preview_projection_before
        assert freecad.documents == documents_before
        assert client.calls == calls_before
        assert binding.document_name in freecad.documents
        assert freecad.documents[binding.document_name] is binding.document
        assert not any(
            call[0] == "close_checkout" and call[1]["checkout_id"] == checkout_id
            for call in client.calls
        )
        assert session.preview.binding_for_checkout(checkout_id) is binding
    finally:
        host.deactivate_workbench()
        force_cleanup_workbench(host, freecad)


def test_fix04_legacy_authenticated_review_exception_retains_client_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    assert _fix03_gateway_open(gateway)["kind"] == "preview_opened"
    client.review_failure = True

    review = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {
                "schema_version": 1,
                "request_id": 2,
                "kind": "review",
                "decision": "accept",
                "task_id": "task_" + "1" * 32,
                "draft_id": "draft_" + "4" * 32,
                "expected_generation": 3,
            },
            capability,
        )
    )
    attempted_close = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {
                "schema_version": 1,
                "request_id": 3,
                "kind": "close",
            },
            capability,
        )
    )

    assert review["kind"] == "error"
    assert review["operation"] == "review"
    assert review["outcome"] == "unknown_outcome"
    assert gateway._closed is False
    assert gateway._client is client
    assert attempted_close["kind"] == "error"
    assert attempted_close["operation"] == "close"
    assert client.close_call_count == 0


def test_fix03_reentrant_deactivate_inside_open_document_converges_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix03_deterministic_open_keys(monkeypatch)
    host, freecad, freecad_gui, clients, events = _start_fix02_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = freecad_gui.main_window.docks[0]
    original_open_document = freecad.openDocument
    hook_calls = 0

    def reentrant_open_document(local_path: str) -> object:
        nonlocal hook_calls
        document = original_open_document(local_path)
        hook_calls += 1
        host.deactivate_workbench()
        return document

    monkeypatch.setattr(freecad, "openDocument", reentrant_open_document)
    before = host.workbench_snapshot()["heartbeat_count"]
    try:
        dock.open_head_button.click()
        pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] > before)

        assert hook_calls == 1
        assert freecad.documents == {}
        assert session.dock.pending_preview_open_count() == 0
        assert session.lifecycle == "stopping"
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
        assert clients[0].close_call_count == 1
        assert events[-3:] == [
            "document.close",
            "checkout.close:" + "checkout_" + "6" * 32,
            "client.close",
        ]
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix03_exact_gateway_replay_is_one_open_and_one_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})

    first = _fix03_gateway_open(gateway)
    replay = _fix03_gateway_open(gateway)

    assert first["kind"] == "preview_opened"
    assert replay == first
    assert [call[0] for call in client.calls].count("open_checkout") == 1
    assert [call[0] for call in client.calls].count("claim_file_grant") == 1


def test_fix03_conflicting_gateway_replay_refuses_second_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    gateway = gateway_module.KernelGateway(lambda: client)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})

    first = _fix03_gateway_open(gateway)
    conflict = _fix03_gateway_open(
        gateway,
        open_key="checkout_open_" + "9" * 32,
    )
    refreshed = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 2,
            "kind": "preview_refresh",
            "checkout_id": "checkout_" + "6" * 32,
        }
    )

    assert first["kind"] == "preview_opened"
    assert conflict["kind"] == "error"
    assert [call[0] for call in client.calls].count("open_checkout") == 1
    assert [call[0] for call in client.calls].count("claim_file_grant") == 1
    assert refreshed["kind"] == "preview_refreshed"


@pytest.mark.parametrize("authority_state", ("pending", "retired"))
def test_fix04_legacy_request_id_exhaustion_never_wraps_or_aliases_authority(
    monkeypatch: pytest.MonkeyPatch,
    authority_state: str,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    dock_module = _load_workbench_module(monkeypatch, "dock")
    dock = dock_module.ReviewDock()
    context = (
        "head",
        {
            "kind": "head",
            "project_id": "project_" + "1" * 32,
        },
        "checkout_open_" + "8" * 32,
    )
    first = dock._next_request("preview_opened", context)
    assert first == 0
    if authority_state == "retired":
        dock.discard_preview_open(first)
        assert dock.expected_preview_open(first) is None
    else:
        assert dock.expected_preview_open(first) == context

    pending_before = dict(dock._pending)
    dock._sequence = 9_007_199_254_740_992
    with pytest.raises(dock_module.ProjectionError):
        dock._next_request("connected", None)

    assert dock._pending == pending_before
    assert dock._sequence == 9_007_199_254_740_992
    if authority_state == "pending":
        assert dock.expected_preview_open(first) == context
    else:
        assert dock.expected_preview_open(first) is None


def test_fix04_legacy_authenticated_client_close_then_raise_stays_sticky_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CloseThenRaiseClient(FakeLocalAgentClient):
        def close(self) -> None:
            if threading.get_ident() != self.created_thread_id:
                raise RuntimeError("fake client thread authority violation")
            self.close_call_count += 1
            self.closed_thread_id = threading.get_ident()
            self.events.append("client.close")
            raise RuntimeError("synthetic close acknowledgement lost")

    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = _CloseThenRaiseClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})

    first = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "request_id": 1, "kind": "close"},
            capability,
        )
    )
    second = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "request_id": 2, "kind": "close"},
            capability,
        )
    )

    assert first == {
        "schema_version": 1,
        "request_id": 1,
        "kind": "error",
        "operation": "close",
        "code": "internal_error",
        "outcome": "unknown_outcome",
    }
    assert second == {
        "schema_version": 1,
        "request_id": 2,
        "kind": "error",
        "operation": "close",
        "code": "internal_error",
        "outcome": "unknown_outcome",
    }
    assert gateway._closed is False
    assert gateway._client is client
    assert client.close_call_count == 1


@pytest.mark.parametrize("ack_failure", ("malformed", "lost"))
def test_fix03_uncertain_checkout_close_reconciles_before_any_retry(
    monkeypatch: pytest.MonkeyPatch,
    ack_failure: str,
) -> None:
    _fix03_deterministic_open_keys(monkeypatch)
    host, freecad, freecad_gui, clients, _events = _start_fix02_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    client = clients[0]
    dock = freecad_gui.main_window.docks[0]
    checkout_id = "checkout_" + "6" * 32
    dock.open_head_button.click()
    pump_main_events(lambda: len(freecad.documents) == 1)

    def uncertain_close_checkout(*, checkout_id: str) -> dict[str, object]:
        client._record("close_checkout", {"checkout_id": checkout_id})
        client.checkout_descriptors[checkout_id]["state"] = "closed"
        if ack_failure == "lost":
            raise RuntimeError("synthetic checkout acknowledgement lost")
        return dict(client.checkout_descriptors[checkout_id]) | {
            "unexpected": None,
        }

    monkeypatch.setattr(client, "close_checkout", uncertain_close_checkout)
    before = host.workbench_snapshot()["heartbeat_count"]
    try:
        session._discard_preview_binding(checkout_id)
        pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] > before)

        names = [name for name, _request, _thread_id in client.calls]
        if "get_checkout" not in names:
            before = host.workbench_snapshot()["heartbeat_count"]
            session._advance_cleanup()
            pump_main_events(lambda: host.workbench_snapshot()["heartbeat_count"] > before)
            names = [name for name, _request, _thread_id in client.calls]

        assert names.count("close_checkout") == 1
        assert names.count("get_checkout") >= 1
        assert names.index("close_checkout") < names.index("get_checkout")
    finally:
        host.deactivate_workbench()
        force_cleanup_workbench(host, freecad)


def test_fix03_host_guard_requires_fresh_correlated_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fix03_deterministic_open_keys(monkeypatch)
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    client = clients[0]
    checkout_id = "checkout_" + "6" * 32
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    try:
        with pytest.raises(preview_error):
            session._guard_preview_binding(checkout_id)

        _fix04_refresh_cycle(host, dock, client)
        session._guard_preview_binding(checkout_id)
    finally:
        host.deactivate_workbench()
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "drift",
    (
        "document-modified",
        "registry-identity",
        "descriptor-dirty",
        "descriptor-state",
        "descriptor-liveness",
        "descriptor-current-digest",
        "descriptor-current-size",
        "descriptor-source-head",
        "descriptor-resolved-source",
    ),
)
def test_fix03_every_observed_drift_poisons_guard_cycle_sticky(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    _fix03_deterministic_open_keys(monkeypatch)
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    checkout_id = "checkout_" + "6" * 32
    binding = coordinator.binding_for_checkout(checkout_id)
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    original_document = freecad.documents[binding.document_name]
    try:
        _fix04_refresh_cycle(host, dock, client)
        session._guard_preview_binding(checkout_id)

        if drift == "document-modified":
            binding.document.Modified = True
            with pytest.raises(preview_error):
                session._guard_preview_binding(checkout_id)
            binding.document.Modified = False
        elif drift == "registry-identity":
            other = coordinator.binding_for_checkout("checkout_" + "7" * 32)
            freecad.documents[binding.document_name] = other.document
            with pytest.raises(preview_error):
                session._guard_preview_binding(checkout_id)
            freecad.documents[binding.document_name] = original_document
        else:
            drifted = dict(client.checkout_descriptors[checkout_id])
            drifted["source"] = dict(drifted["source"])
            drifted["source_head"] = dict(drifted["source_head"])
            if drift == "descriptor-dirty":
                drifted["dirty"] = True
            elif drift == "descriptor-state":
                drifted["state"] = "closed"
            elif drift == "descriptor-liveness":
                drifted["source_liveness"] = "stale"
            elif drift == "descriptor-current-digest":
                drifted["current_model_sha256"] = "a" * 64
            elif drift == "descriptor-current-size":
                drifted["current_size_bytes"] = 24
            elif drift == "descriptor-source-head":
                drifted["source_head"]["generation"] = 3
            else:
                drifted["source"]["revision_id"] = "revision_" + "b" * 32
            original_descriptor = client.checkout_descriptors[checkout_id]
            client.checkout_descriptors[checkout_id] = drifted
            _fix04_refresh_cycle(host, dock, client)
            client.checkout_descriptors[checkout_id] = original_descriptor

        _fix04_refresh_cycle(host, dock, client)
        with pytest.raises(preview_error):
            session._guard_preview_binding(checkout_id)
        assert coordinator.aggregate_review_eligible() is False
        assert dock.preview_projection().review_eligible is False
    finally:
        binding.document.Modified = False
        freecad.documents[binding.document_name] = original_document
        host.deactivate_workbench()
        force_cleanup_workbench(host, freecad)


def test_fix04_open_checkout_effect_then_raise_is_unknown_sticky(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    original_open_checkout = client.open_checkout
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})

    def open_then_lose_ack(
        *,
        open_key: str,
        source: dict[str, object],
    ) -> dict[str, object]:
        original_open_checkout(open_key=open_key, source=source)
        raise RuntimeError("synthetic open-checkout acknowledgement lost")

    monkeypatch.setattr(client, "open_checkout", open_then_lose_ack)
    first = _fix03_gateway_open(gateway, request_id=1)
    second = _fix03_gateway_open(gateway, request_id=2)
    close = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {"schema_version": 1, "request_id": 3, "kind": "close"},
            capability,
        )
    )

    assert first["kind"] == "error"
    assert first["operation"] == "preview_open"
    assert first["outcome"] == "unknown_outcome"
    assert second["kind"] == "error"
    assert second["operation"] == "preview_open"
    assert second["outcome"] == "unknown_outcome"
    assert [call[0] for call in client.calls].count("open_checkout") == 1
    assert close["kind"] == "error"
    assert close["operation"] == "close"
    assert client.close_call_count == 0


def test_fix04_invalid_first_recoverable_request_id_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    request_id = 17

    invalid = gateway.handle(
        {
            "schema_version": 1,
            "request_id": request_id,
            "kind": "preview_open",
            "source": {
                "kind": "head",
                "project_id": "project_" + "1" * 32,
            },
        }
    )
    valid_after_invalid = _fix03_gateway_open(
        gateway,
        request_id=request_id,
    )

    assert invalid["kind"] == "error"
    assert invalid["operation"] == "preview_open"
    assert invalid["code"] == "invalid_input"
    assert valid_after_invalid["kind"] == "error"
    assert valid_after_invalid["operation"] == "preview_open"
    assert valid_after_invalid["code"] == "invalid_input"
    assert not any(call[0] == "open_checkout" for call in client.calls)


def test_fix04_bounded_replay_eviction_never_resurrects_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    opened = _fix03_gateway_open(gateway, request_id=1)
    capacity = _fix04_replay_capacity(gateway, gateway_module)
    assert opened["kind"] == "preview_opened"

    for request_id in range(2, capacity + 3):
        response = gateway.handle(
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "list_projects",
                "cursor": None,
            }
        )
        assert response["kind"] == "projects"

    replay_maps = [
        value
        for name, value in vars(gateway).items()
        if "replay" in name.lower() and type(value) is dict
    ]
    assert len(replay_maps) == 1
    assert len(replay_maps[0]) <= capacity
    calls_before = len(client.calls)
    evicted = _fix03_gateway_open(gateway, request_id=1)

    assert evicted["kind"] == "error"
    assert evicted["operation"] == "preview_open"
    assert evicted["code"] == "invalid_input"
    assert len(client.calls) == calls_before
    assert [call[0] for call in client.calls].count("open_checkout") == 1
    assert len(replay_maps[0]) <= capacity


def test_fix04_normal_id_exhaustion_drains_through_cleanup_reserve_without_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, _freecad_gui, clients, _events = _start_fix02_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    sequence_candidates: list[tuple[object, str]] = []
    for owner in (session, dock):
        for name, value in vars(owner).items():
            lowered = name.lower()
            if (
                type(value) is int
                and (
                    "sequence" in lowered
                    or ("next" in lowered and "request" in lowered and "id" in lowered)
                )
                and not any(
                    marker in lowered
                    for marker in ("cleanup", "close", "epoch", "generation", "refresh")
                )
            ):
                sequence_candidates.append((owner, name))
    assert len(sequence_candidates) == 1, "missing sole normal request-id allocator"
    sequence_owner, sequence_name = sequence_candidates[0]
    setattr(sequence_owner, sequence_name, 9_007_199_254_740_992)
    calls_before = len(clients[0].calls)

    try:
        dock.refresh()
        pump_main_events(lambda: session.lifecycle == "stopping")

        assert getattr(sequence_owner, sequence_name) != 0
        assert len(clients[0].calls) == calls_before
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
        close_request_id = session._close_request_id
        assert type(close_request_id) is int
        assert 0 <= close_request_id <= 9_007_199_254_740_991
        assert clients[0].close_call_count == 1
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_wrong_kind_authenticated_success_preserves_pending_and_zero_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def configure(client: FakeLocalAgentClient) -> None:
        client.open_checkout_entered = entered
        client.open_checkout_release = release

    host, freecad, freecad_gui, _clients, _events = _start_fix02_host(
        monkeypatch,
        configure_client=configure,
    )
    try:
        session = host._session
        assert session is not None
        assert session.dock is not None
        dock = session.dock
        dock.open_head_button.click()
        assert entered.wait(1.0)
        pending_before = _fix04_pending_ids(session, dock, "preview_opened")
        assert len(pending_before) == 1
        request_id = pending_before[0]
        gateway_module = importlib.import_module("vibecad_workbench.gateway")
        capability = _fix04_session_capability(session)
        wrapper = _fix04_wrap(
            _fix04_wrapper_type("event", host, gateway_module),
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "project",
                "response": {
                    "schema_version": 1,
                    "ok": True,
                    "result": {},
                    "error": None,
                },
            },
            capability,
        )
        projection_before = dock.preview_projection()
        status_before = dock.status_label.text

        session._receive(wrapper)

        assert _fix04_pending_ids(session, dock, "preview_opened") == pending_before
        assert dock.pending_preview_open_count() == 1
        assert dock.preview_projection() == projection_before
        assert dock.status_label.text == status_before
        assert session.preview is None
        assert freecad.documents == {}
        assert freecad_gui.main_window.docks == [dock]
    finally:
        release.set()
        force_cleanup_workbench(host, freecad)


def test_fix04_malformed_authenticated_success_preserves_pending_and_zero_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def configure(client: FakeLocalAgentClient) -> None:
        client.open_checkout_entered = entered
        client.open_checkout_release = release

    host, freecad, freecad_gui, _clients, _events = _start_fix02_host(
        monkeypatch,
        configure_client=configure,
    )
    try:
        session = host._session
        assert session is not None
        assert session.dock is not None
        dock = session.dock
        dock.open_head_button.click()
        assert entered.wait(1.0)
        pending_before = _fix04_pending_ids(session, dock, "preview_opened")
        assert len(pending_before) == 1
        request_id = pending_before[0]
        gateway_module = importlib.import_module("vibecad_workbench.gateway")
        capability = _fix04_session_capability(session)
        wrapper = _fix04_wrap(
            _fix04_wrapper_type("event", host, gateway_module),
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": "preview_opened",
                "response": {},
                "unexpected": None,
            },
            capability,
        )
        projection_before = dock.preview_projection()
        status_before = dock.status_label.text

        session._receive(wrapper)

        assert _fix04_pending_ids(session, dock, "preview_opened") == pending_before
        assert dock.pending_preview_open_count() == 1
        assert dock.preview_projection() == projection_before
        assert dock.status_label.text == status_before
        assert session.preview is None
        assert freecad.documents == {}
        assert freecad_gui.main_window.docks == [dock]
    finally:
        release.set()
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    ("representation", "operation"),
    (
        ("raw", "preview_close"),
        ("raw", "review"),
        ("raw", "close"),
        ("plain", "preview_close"),
        ("plain", "review"),
        ("plain", "close"),
        ("forged-equal", "preview_close"),
        ("forged-equal", "review"),
        ("forged-equal", "close"),
    ),
)
def test_fix04_restricted_command_representation_matrix_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    representation: str,
    operation: str,
) -> None:
    class _PlainCommand:
        def __init__(self, payload: dict[str, object], capability: object) -> None:
            self.payload = payload
            self.capability = capability

    class _EqualCapability:
        def __init__(self, expected: object) -> None:
            self.expected = expected

        def __eq__(self, other: object) -> bool:
            return other is self.expected

    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    payloads: dict[str, dict[str, object]] = {
        "preview_close": {
            "schema_version": 1,
            "request_id": 11,
            "kind": "preview_close",
            "checkout_id": "checkout_" + "6" * 32,
            "document_absent": True,
        },
        "review": {
            "schema_version": 1,
            "request_id": 12,
            "kind": "review",
            "decision": "accept",
            "task_id": "task_" + "1" * 32,
            "draft_id": "draft_" + "4" * 32,
            "expected_generation": 3,
        },
        "close": {
            "schema_version": 1,
            "request_id": 13,
            "kind": "close",
        },
    }
    payload = payloads[operation]
    if representation == "raw":
        command: object = payload
    elif representation == "plain":
        command = _PlainCommand(payload, capability)
    else:
        forged_capability = _EqualCapability(capability)
        assert forged_capability == capability
        assert forged_capability is not capability
        command = _fix04_private_gateway_command(
            gateway_module,
            payload,
            forged_capability,
        )
    calls_before = len(client.calls)
    close_calls_before = client.close_call_count

    result = gateway.handle(command)
    event = result if type(result) is dict else _fix04_wrapper_payload(result)

    assert event["kind"] == "error"
    assert event["code"] == "invalid_input"
    assert len(client.calls) == calls_before
    assert client.close_call_count == close_calls_before
    assert not any(
        call[0] in {"close_checkout", "accept_draft", "reject_draft"}
        for call in client.calls[calls_before:]
    )


def test_fix04_fake_qt_single_signal_preserves_wrapper_identity_and_fifo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    wrapper_type = _fix04_wrapper_type("command", gateway_module)
    capability = object()
    first_payload = {
        "schema_version": 1,
        "request_id": 1,
        "kind": "connect",
        "metadata": {"labels": ["first"]},
    }
    first_expected = _fix04_plain_wire_value(first_payload)
    first = _fix04_wrap(
        wrapper_type,
        first_payload,
        capability,
    )
    first_payload["metadata"]["labels"].append("mutated")
    assert _fix04_wrapper_payload(first) == first_expected
    second = _fix04_wrap(
        wrapper_type,
        {"schema_version": 1, "request_id": 2, "kind": "connect"},
        capability,
    )
    qt_core = fake_pyside.QtCore

    class _Emitter(qt_core.QObject):
        wire = qt_core.Signal(object)

    class _Receiver(qt_core.QObject):
        def __init__(self) -> None:
            super().__init__()
            self.received: list[object] = []
            self.completed = threading.Event()

        def receive(self, value: object) -> None:
            self.received.append(value)
            if len(self.received) == 2:
                self.completed.set()

    emitter = _Emitter()
    receiver = _Receiver()
    thread = qt_core.QThread()
    receiver.moveToThread(thread)
    emitter.wire.connect(
        receiver.receive,
        qt_core.Qt.ConnectionType.QueuedConnection,
    )
    thread.start()
    try:
        emitter.wire.emit(first)
        emitter.wire.emit(second)
        assert receiver.completed.wait(1.0)
        assert len(receiver.received) == 2
        assert receiver.received[0] is first
        assert receiver.received[1] is second
        assert _fix04_wrapper_payload(receiver.received[0]) == first_expected
    finally:
        thread.quit()
        thread.join()
    assert not thread.isRunning()


def test_fix04_host_authenticated_document_absence_close_is_ordered_and_single_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = _start_fail_cleanup_host(monkeypatch)
    try:
        session = host._session
        assert session is not None
        assert session.worker is not None
        assert session.preview is not None
        gateway = session.worker.gateway
        gateway_module = importlib.import_module("vibecad_workbench.gateway")
        capability = _fix04_session_capability(session)
        original_handle = gateway.handle
        captured: list[tuple[object, int]] = []

        def observe_handle(command: object) -> object:
            captured.append((command, len(freecad.documents)))
            return original_handle(command)

        monkeypatch.setattr(gateway, "handle", observe_handle)
        before = len(events)
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

        restricted: list[tuple[object, dict[str, object], int]] = []
        for command, document_count in captured:
            payload = _fix04_wrapper_payload(command)
            if payload.get("kind") in {"preview_close", "close"}:
                restricted.append((command, payload, document_count))
        assert [payload["kind"] for _command, payload, _count in restricted] == [
            "preview_close",
            "preview_close",
            "close",
        ]
        assert all(
            type(command) is _fix04_wrapper_type("command", host, gateway_module)
            for command, _payload, _count in restricted
        )
        assert all(
            _fix04_wrapper_capability(command) is capability
            for command, _payload, _count in restricted
        )
        assert all(count == 0 for _command, _payload, count in restricted)
        assert all(
            payload.get("document_absent") is True
            for _command, payload, _count in restricted
            if payload["kind"] == "preview_close"
        )
        assert events[before:] == [
            "document.close",
            "document.close",
            "checkout.close:" + "checkout_" + "6" * 32,
            "checkout.close:" + "checkout_" + "7" * 32,
            "client.close",
        ]
        close_calls = [call for call in clients[0].calls if call[0] == "close_checkout"]
        assert [call[1]["checkout_id"] for call in close_calls] == [
            "checkout_" + "6" * 32,
            "checkout_" + "7" * 32,
        ]
        assert len(clients) == 1
        assert clients[0].close_call_count == 1
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_new_refresh_dispatch_invalidates_old_tokens_before_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    _fix04_refresh_cycle(host, dock, client)
    assert dock.preview_projection().review_eligible is True
    entered = threading.Event()
    release = threading.Event()
    original_get_checkout = client.get_checkout

    def block_refresh(*, checkout_id: str) -> dict[str, object]:
        entered.set()
        assert release.wait(1.0)
        return original_get_checkout(checkout_id=checkout_id)

    monkeypatch.setattr(client, "get_checkout", block_refresh)
    try:
        dock.refresh()
        assert entered.wait(1.0)

        assert dock.preview_projection().review_eligible is False
    finally:
        release.set()
        force_cleanup_workbench(host, freecad)


def test_fix04_correlated_refresh_error_is_nonsticky_and_exact_retry_mints_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    head_checkout = "checkout_" + "6" * 32
    original_get_checkout = client.get_checkout

    def fail_head(*, checkout_id: str) -> dict[str, object]:
        if checkout_id == head_checkout:
            client._record("get_checkout", {"checkout_id": checkout_id})
            raise RuntimeError("synthetic correlated refresh failure")
        return original_get_checkout(checkout_id=checkout_id)

    try:
        monkeypatch.setattr(client, "get_checkout", fail_head)
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is False

        monkeypatch.setattr(client, "get_checkout", original_get_checkout)
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is True
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_stale_older_refresh_success_after_new_dispatch_cannot_restore_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    older_entered = threading.Event()
    release_older = threading.Event()
    newer_entered = threading.Event()
    release_newer = threading.Event()
    original_get_checkout = client.get_checkout
    refresh_call = 0

    def ordered_refresh(*, checkout_id: str) -> dict[str, object]:
        nonlocal refresh_call
        refresh_call += 1
        if refresh_call == 1:
            older_entered.set()
            assert release_older.wait(1.0)
        elif refresh_call == 3:
            newer_entered.set()
            assert release_newer.wait(1.0)
        return original_get_checkout(checkout_id=checkout_id)

    monkeypatch.setattr(client, "get_checkout", ordered_refresh)
    before_heartbeat = host.workbench_snapshot()["heartbeat_count"]
    before_refreshes = sum(call[0] == "get_checkout" for call in client.calls)
    try:
        dock.refresh()
        assert older_entered.wait(1.0)
        dock.refresh()
        release_older.set()
        assert newer_entered.wait(1.0)
        pump_main_events(
            lambda: host.workbench_snapshot()["heartbeat_count"] >= before_heartbeat + 5
        )

        assert dock.preview_projection().review_eligible is False

        release_newer.set()
        pump_main_events(
            lambda: (
                sum(call[0] == "get_checkout" for call in client.calls) >= before_refreshes + 4
                and host.workbench_snapshot()["heartbeat_count"] >= before_heartbeat + 10
            )
        )
        assert dock.preview_projection().review_eligible is True
    finally:
        release_older.set()
        release_newer.set()
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "precondition",
    (
        "missing-head-token",
        "missing-draft-token",
        "mixed-refresh-cycle",
        "refresh-pending",
    ),
)
def test_fix04_host_review_precondition_matrix_has_zero_review_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    precondition: str,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    coordinator = session.preview
    assert coordinator is not None
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    head_checkout = "checkout_" + "6" * 32
    draft_checkout = "checkout_" + "7" * 32
    original_get_checkout = client.get_checkout
    pending_entered = threading.Event()
    release_pending = threading.Event()
    refresh_call = 0

    def selective_refresh(*, checkout_id: str) -> dict[str, object]:
        nonlocal refresh_call
        refresh_call += 1
        should_fail = (
            precondition == "missing-head-token"
            and checkout_id == head_checkout
            or precondition == "missing-draft-token"
            and checkout_id == draft_checkout
            or precondition == "mixed-refresh-cycle"
            and refresh_call in {2, 3}
        )
        if should_fail:
            client._record("get_checkout", {"checkout_id": checkout_id})
            raise RuntimeError("synthetic missing review token")
        return original_get_checkout(checkout_id=checkout_id)

    def pending_refresh(*, checkout_id: str) -> dict[str, object]:
        pending_entered.set()
        assert release_pending.wait(1.0)
        return original_get_checkout(checkout_id=checkout_id)

    review_calls_before = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
    try:
        if precondition == "refresh-pending":
            _fix04_refresh_cycle(host, dock, client)
            monkeypatch.setattr(client, "get_checkout", pending_refresh)
            before_heartbeat = host.workbench_snapshot()["heartbeat_count"]
            dock.refresh()
            assert pending_entered.wait(1.0)
            assert dock.preview_projection().review_eligible is False
            with pytest.raises(
                preview_error,
                match="fresh shared review authority required",
            ):
                _fix04_request_review(session, decision="accept")
            assert session._review_tokens == {}
            assert _fix04_pending_ids(session, dock, "review") == ()
            assert coordinator._cycle is cycle
            assert coordinator._active_cycle_id() == cycle_id
            release_pending.set()
            pump_main_events(
                lambda: host.workbench_snapshot()["heartbeat_count"] >= before_heartbeat + 5
            )
        else:
            monkeypatch.setattr(client, "get_checkout", selective_refresh)
            _fix04_refresh_cycle(host, dock, client)
            if precondition == "mixed-refresh-cycle":
                _fix04_refresh_cycle(host, dock, client)
            assert dock.preview_projection().review_eligible is False
            with pytest.raises(
                preview_error,
                match="fresh shared review authority required",
            ):
                _fix04_request_review(session, decision="accept")
            assert session._review_tokens == {}
            assert _fix04_pending_ids(session, dock, "review") == ()
            assert coordinator._cycle is cycle
            assert coordinator._active_cycle_id() == cycle_id
            _fix04_settle_worker(session)

        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == review_calls_before
        )
    finally:
        release_pending.set()
        force_cleanup_workbench(host, freecad)


def test_fix04_semantic_task_refresh_preserves_selection_epoch_and_exact_review_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    try:
        session = host._session
        assert session is not None
        assert session.dock is not None
        dock = session.dock
        client = clients[0]
        coordinator = session.preview
        assert coordinator is not None
        cycle = coordinator._cycle
        cycle_id = coordinator._active_cycle_id()
        assert cycle is not None
        assert type(cycle_id) is int
        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        task_before = dock._selected_task()
        semantic_before = (
            dock.current_project_id(),
            dock.current_task_id(),
            dock._task_value(task_before, "draft_id"),
            dock._task_value(task_before, "generation"),
        )
        epoch_before = dock._task_selection_epoch

        _fix04_refresh_cycle(host, dock, client)

        task_after = dock._selected_task()
        assert (
            dock.current_project_id(),
            dock.current_task_id(),
            dock._task_value(task_after, "draft_id"),
            dock._task_value(task_after, "generation"),
        ) == semantic_before
        assert dock._task_selection_epoch == epoch_before
        review_calls_before = sum(
            call[0] in {"accept_draft", "reject_draft"} for call in client.calls
        )
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        _fix04_request_review(session, decision="accept")
        pump_main_events(
            lambda: (
                sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
                == review_calls_before + 1
                and host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
            )
        )

        with pytest.raises(
            preview_error,
            match="fresh shared review authority required",
        ):
            _fix04_request_review(session, decision="accept")
        assert session._review_tokens == {}
        assert _fix04_pending_ids(session, dock, "review") == ()
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        _fix04_settle_worker(session)
        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == review_calls_before + 1
        )
        assert dock.preview_projection().review_eligible is False
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_real_task_selection_aba_rejects_old_review_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    try:
        session = host._session
        assert session is not None
        assert session.dock is not None
        dock = session.dock
        client = clients[0]
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is True
        original_task_id = dock.current_task_id()
        assert original_task_id is not None
        other_task = _task_record(
            "9",
            generation=3,
            candidate_revision="revision_" + "9" * 32,
            draft_id="draft_" + "9" * 32,
        )
        other_task["project_id"] = "project_" + "1" * 32
        other_task["status"] = "awaiting_user_review"
        other_task_id = other_task["task_id"]
        assert type(other_task_id) is str
        dock._task_ids.append(other_task_id)
        dock._tasks_by_id[other_task_id] = other_task
        dock.task_selector.addItem(other_task_id)
        epoch_before = dock._task_selection_epoch
        review_calls_before = sum(
            call[0] in {"accept_draft", "reject_draft"} for call in client.calls
        )

        dock.task_selector.setCurrentIndex(1)
        dock.task_selector.setCurrentIndex(0)

        assert dock.current_task_id() == original_task_id
        assert dock._task_selection_epoch == epoch_before + 2
        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")
        _fix04_settle_worker(session)

        assert dock.preview_projection().review_eligible is False
        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == review_calls_before
        )
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_exact_same_cycle_dual_token_review_is_authenticated_and_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    try:
        session = host._session
        assert session is not None
        assert session.dock is not None
        assert session.worker is not None
        dock = session.dock
        client = clients[0]
        coordinator = session.preview
        assert coordinator is not None
        cycle = coordinator._cycle
        cycle_id = coordinator._active_cycle_id()
        assert cycle is not None
        assert type(cycle_id) is int
        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is True
        gateway = session.worker.gateway
        capability = _fix04_session_capability(session)
        original_handle = gateway.handle
        captured: list[object] = []

        def observe_handle(command: object) -> object:
            captured.append(command)
            return original_handle(command)

        monkeypatch.setattr(gateway, "handle", observe_handle)
        before_heartbeat = host.workbench_snapshot()["heartbeat_count"]
        _fix04_request_review(session, decision="accept")
        pump_main_events(
            lambda: (
                sum(call[0] == "accept_draft" for call in client.calls) == 1
                and host.workbench_snapshot()["heartbeat_count"] > before_heartbeat
            )
        )

        review_commands = [
            command
            for command in captured
            if _fix04_wrapper_payload(command).get("kind") == "review"
        ]
        assert len(review_commands) == 1
        assert _fix04_wrapper_capability(review_commands[0]) is capability
        assert _fix04_wrapper_payload(review_commands[0])["decision"] == "accept"
        assert dock.preview_projection().review_eligible is False

        with pytest.raises(
            preview_error,
            match="fresh shared review authority required",
        ):
            _fix04_request_review(session, decision="accept")
        assert session._review_tokens == {}
        assert _fix04_pending_ids(session, dock, "review") == ()
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        _fix04_settle_worker(session)
        assert sum(call[0] == "accept_draft" for call in client.calls) == 1
    finally:
        try:
            host.deactivate_workbench()
        finally:
            force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    ("drift", "aggregate_eligible"),
    (
        ("source-head", False),
        ("candidate-revision", True),
    ),
)
def test_c02_draft_revision_authority_mismatch_is_poisoned_before_review(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
    aggregate_eligible: bool,
) -> None:
    def configure(client: FakeLocalAgentClient) -> None:
        def drift_draft(response: dict[str, object]) -> dict[str, object]:
            source = response["source"]
            assert type(source) is dict
            if source["kind"] != "draft":
                return response
            if drift == "source-head":
                source_head = response["source_head"]
                assert type(source_head) is dict
                source_head["generation"] = 99
                source_head["revision_id"] = "revision_" + "b" * 32
                source_head["manifest_sha256"] = "c" * 64
            else:
                source["revision_id"] = "revision_" + "e" * 32
                source["manifest_sha256"] = "f" * 64
            return response

        client.open_checkout_transform = drift_draft

    host, freecad, _freecad_gui, clients, _events = _start_fix02_host(
        monkeypatch,
        configure_client=configure,
    )
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    try:
        dock.open_head_button.click()
        dock.open_draft_button.click()
        pump_main_events(lambda: len(freecad.documents) == 2)
        coordinator = session.preview
        assert coordinator is not None

        aggregate_before_refresh = coordinator.aggregate_review_eligible()
        _fix04_refresh_cycle(host, dock, client)
        projected_before_review = dock.preview_projection().review_eligible
        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        with pytest.raises(
            preview_error,
            match="fresh shared review authority required",
        ):
            _fix04_request_review(session, decision="accept")
        assert session._review_tokens == {}
        assert _fix04_pending_ids(session, dock, "review") == ()
        _fix04_settle_worker(session)
        cycle = coordinator._cycle

        assert (
            aggregate_before_refresh,
            projected_before_review,
            sum(call[0] == "accept_draft" for call in client.calls),
            cycle is not None and cycle.poisoned,
        ) == (
            aggregate_eligible,
            False,
            0,
            True,
        )
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize("task_field", ("candidate_revision", "base_revision"))
def test_c02_final_review_rebinds_draft_candidate_and_base(
    monkeypatch: pytest.MonkeyPatch,
    task_field: str,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    try:
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is True
        selected_task = dock._selected_task()
        task_id = dock.current_task_id()
        assert is_dataclass(selected_task)
        assert type(task_id) is str
        dock._tasks_by_id[task_id] = replace(
            selected_task,
            **{task_field: "revision_" + "e" * 32},
        )

        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")
        _fix04_settle_worker(session)
        cycle = coordinator._cycle

        assert not any(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
        assert _fix04_authenticated_review_commands(session) == ()
        assert dock.preview_projection().review_eligible is False
        assert session._review_tokens == {}
        assert cycle is not None and cycle.poisoned is True
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_review_enqueue_failure_consumes_tokens_until_new_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    _fix04_refresh_cycle(host, dock, client)
    assert dock.preview_projection().review_eligible is True
    signal = _fix04_private_signal(session)
    original_emit = signal.emit
    failed = False

    def fail_review_enqueue(value: object) -> None:
        nonlocal failed
        payload = _fix04_wrapper_payload(value)
        if payload.get("kind") == "review" and not failed:
            failed = True
            raise RuntimeError("synthetic queued review enqueue failure")
        original_emit(value)

    try:
        with monkeypatch.context() as enqueue_patch:
            enqueue_patch.setattr(signal, "emit", fail_review_enqueue)
            with pytest.raises(RuntimeError):
                _fix04_request_review(session, decision="accept")

        pending_after_failure = _fix04_pending_ids(session, dock, "review")
        assert failed is True
        assert len(pending_after_failure) == 1
        request_id = pending_after_failure[0]
        assert session._pending[request_id][5] is True
        assert session._review_enqueue_ambiguous_id == request_id
        assert session.lifecycle == "stopping"
        assert session._open_recovery_required is False
        assert dock.preview_projection().review_eligible is False
        assert dock.preview_projection().recovery_required is True
        assert not any(call[0] == "accept_draft" for call in client.calls)

        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")
        refreshes_before = sum(call[0] == "get_checkout" for call in client.calls)
        dock.refresh()
        _fix04_settle_worker(session)
        assert not any(call[0] == "accept_draft" for call in client.calls)
        assert sum(call[0] == "get_checkout" for call in client.calls) == refreshes_before
        assert _fix04_pending_ids(session, dock, "review") == pending_after_failure
        assert session._review_enqueue_ambiguous_id == request_id
        assert host._session is session
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "cleanup_reason",
    ("stale-success", "validation-failure"),
)
def test_fix04_active_preview_cleanup_keeps_normal_ids_and_followup_open_refresh(
    monkeypatch: pytest.MonkeyPatch,
    cleanup_reason: str,
) -> None:
    host, freecad, freecad_gui, clients, _events = _start_fix02_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.worker is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    gateway = session.worker.gateway
    original_handle = gateway.handle
    captured: list[tuple[dict[str, object], bool]] = []

    def observe_handle(command: object) -> object:
        payload = dict(command) if type(command) is dict else _fix04_wrapper_payload(command)
        captured.append((payload, type(command) is not dict))
        return original_handle(command)

    monkeypatch.setattr(gateway, "handle", observe_handle)
    if cleanup_reason == "stale-success":
        original_current_preview_open = dock.current_preview_open
        current_checks = 0

        def stale_once(request_id: int) -> object:
            nonlocal current_checks
            current_checks += 1
            if current_checks == 1:
                return None
            return original_current_preview_open(request_id)

        monkeypatch.setattr(dock, "current_preview_open", stale_once)
    else:
        preview_module = importlib.import_module("vibecad_workbench.preview")
        original_acquire = preview_module.PreviewCoordinator.acquire
        acquire_calls = 0

        def invalid_claim_once(
            gateway_client: object,
            *,
            source: object,
            open_key: object,
        ) -> dict[str, object]:
            nonlocal acquire_calls
            acquired = original_acquire(
                gateway_client,
                source=source,
                open_key=open_key,
            )
            acquire_calls += 1
            if acquire_calls == 1:
                claim = dict(acquired["claim"])
                claim["unexpected"] = None
                acquired["claim"] = claim
            return acquired

        monkeypatch.setattr(
            preview_module.PreviewCoordinator,
            "acquire",
            staticmethod(invalid_claim_once),
        )

    try:
        dock.open_head_button.click()
        pump_main_events(
            lambda: (
                any(call[0] == "close_checkout" for call in client.calls)
                and session._cleanup_request_id is None
                and session.preview is not None
                and session.preview._active_cycle_id() is None
            )
        )

        initial_open = [
            payload for payload, _private in captured if payload.get("kind") == "preview_open"
        ]
        cleanup = [
            (payload, private)
            for payload, private in captured
            if payload.get("kind") == "preview_close"
        ]
        assert len(initial_open) == 1
        assert len(cleanup) == 1
        cleanup_payload, cleanup_private = cleanup[0]
        close_calls = [call for call in client.calls if call[0] == "close_checkout"]
        assert len(close_calls) == 1
        assert close_calls[0][1] == {
            "checkout_id": cleanup_payload["checkout_id"],
        }
        assert close_calls[0][2] == client.created_thread_id
        assert cleanup_private is True
        assert session.lifecycle == "active"
        assert host._session is session
        assert client.close_call_count == 0
        assert freecad.documents == {}

        opens_before = sum(call[0] == "open_checkout" for call in client.calls)
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        dock.open_head_button.click()
        pump_main_events(
            lambda: (
                host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
                and sum(call[0] == "open_checkout" for call in client.calls)
                == opens_before + 1
                and len(freecad.documents) == 1
            )
        )
        followup_open = [
            payload for payload, _private in captured if payload.get("kind") == "preview_open"
        ][-1]
        followup_opened = (
            sum(call[0] == "open_checkout" for call in client.calls) == opens_before + 1
            and len(freecad.documents) == 1
        )
        followup_refreshed = False
        if followup_opened:
            refreshes_before = sum(call[0] == "get_checkout" for call in client.calls)
            heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
            dock.refresh()
            pump_main_events(
                lambda: (
                    sum(call[0] == "get_checkout" for call in client.calls) == refreshes_before + 1
                    and host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
                )
            )
            followup_refreshed = True

        assert (
            initial_open[0]["request_id"] < cleanup_payload["request_id"],
            cleanup_payload["request_id"] < followup_open["request_id"],
            followup_opened,
            followup_refreshed,
        ) == (True, True, True, True)
        assert host._session is session
        assert freecad_gui.main_window.docks == [dock]
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_terminal_high_lane_never_reopens_normal_business(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_module = _load_workbench_module(monkeypatch, "gateway")
    client = FakeLocalAgentClient()
    capability = object()
    gateway = _fix04_gateway(gateway_module, lambda: client, capability)
    gateway.handle({"schema_version": 1, "request_id": 0, "kind": "connect"})
    opened = _fix03_gateway_open(gateway, request_id=1)
    checkout_id = opened["response"]["descriptor"]["checkout_id"]
    terminal_id = 9_007_199_254_740_991

    closed = gateway.handle(
        _fix04_private_gateway_command(
            gateway_module,
            {
                "schema_version": 1,
                "request_id": terminal_id,
                "kind": "preview_close",
                "checkout_id": checkout_id,
                "document_absent": True,
            },
            capability,
        )
    )
    calls_before = len(client.calls)
    resumed = gateway.handle(
        {
            "schema_version": 1,
            "request_id": 2,
            "kind": "list_projects",
            "cursor": None,
        }
    )

    assert _fix04_wrapper_payload(closed)["kind"] == "preview_closed"
    assert resumed == {
        "schema_version": 1,
        "request_id": 2,
        "kind": "error",
        "operation": "list_projects",
        "code": "invalid_input",
        "outcome": "known_failure",
    }
    assert len(client.calls) == calls_before


def test_fix04_unbound_owned_authority_blocks_review_until_exact_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, freecad_gui, clients, _events = _start_fix02_host(
        monkeypatch,
        client_factory=lambda: FakeLocalAgentClient(
            monotonic_preview_authorities=True,
        ),
    )
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    original_open_document = freecad.openDocument
    first_document = True

    def open_modified_once(local_path: str) -> object:
        nonlocal first_document
        document = original_open_document(local_path)
        if first_document:
            first_document = False
            document.Modified = True
        return document

    monkeypatch.setattr(freecad, "openDocument", open_modified_once)
    freecad.close_failures = 1
    try:
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        dock.open_head_button.click()
        pump_main_events(
            lambda: (
                host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
                and dock.pending_preview_open_count() == 0
            )
        )

        coordinator = session.preview
        assert coordinator is not None
        old_checkout = "checkout_" + f"{6:032x}"
        old_record = coordinator._owned[old_checkout]
        assert old_record.binding is None
        assert old_record.document is not None
        assert old_record.document_closed is False
        assert freecad.getDocument(old_record.document_name) is old_record.document
        assert coordinator.ready_checkout_ids() == ()
        assert not any(call[0] == "close_checkout" for call in client.calls)

        monkeypatch.setattr(freecad, "openDocument", original_open_document)
        dock.open_head_button.click()
        dock.open_draft_button.click()
        pump_main_events(lambda: len(freecad.documents) == 3)
        assert tuple(client.checkout_descriptors) == tuple(
            "checkout_" + f"{authority:032x}" for authority in (6, 7, 8)
        )
        assert [call[1]["grant_id"] for call in client.calls if call[0] == "claim_file_grant"] == [
            "file_grant_" + f"{authority:032x}" for authority in (6, 7, 8)
        ]

        _fix04_refresh_cycle(host, dock, client)
        aggregate_with_unbound = coordinator.aggregate_review_eligible()
        projected_with_unbound = dock.preview_projection().review_eligible
        minted_with_unbound = bool(session._review_tokens)
        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        review_rejected = False
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        try:
            _fix04_request_review(session, decision="accept")
        except preview_error:
            review_rejected = True
        else:
            pump_main_events(
                lambda: host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
            )
        old_review_calls = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)

        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
        old_close_calls = [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ]
        assert old_close_calls == ["checkout_" + f"{authority:032x}" for authority in (6, 7, 8)]
        assert client.close_call_count == 1
        assert freecad.documents == {}

        host.activate_workbench()
        pump_main_events(
            lambda: (
                len(clients) == 2
                and bool(freecad_gui.main_window.docks)
                and freecad_gui.main_window.docks[0].task_selector.items
            )
        )
        replacement_dock = freecad_gui.main_window.docks[0]
        replacement_client = clients[1]
        replacement_dock.open_head_button.click()
        replacement_dock.open_draft_button.click()
        pump_main_events(lambda: len(freecad.documents) == 2)
        _fix04_refresh_cycle(
            host,
            replacement_dock,
            replacement_client,
        )
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        replacement_session = host._session
        assert replacement_session is not None
        _fix04_request_review(replacement_session, decision="accept")
        pump_main_events(
            lambda: (
                sum(
                    call[0] in {"accept_draft", "reject_draft"} for call in replacement_client.calls
                )
                == 1
                and host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
            )
        )

        assert (
            aggregate_with_unbound,
            projected_with_unbound,
            minted_with_unbound,
            review_rejected,
            old_review_calls,
        ) == (False, False, False, True, 0)
        assert replacement_client.close_call_count == 0
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "close_outcome",
    ("no-effect", "effect-then-raise"),
)
def test_fix04_transport_exhaustion_close_retry_is_effect_sensitive(
    monkeypatch: pytest.MonkeyPatch,
    close_outcome: str,
) -> None:
    host, freecad, clients, events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    events_before = len(events)
    effect_close_calls = 0
    if close_outcome == "no-effect":
        freecad.close_failures = 1
    else:
        original_close_document = freecad.closeDocument

        def close_then_raise(name: str) -> None:
            nonlocal effect_close_calls
            effect_close_calls += 1
            original_close_document(name)
            raise RuntimeError("synthetic close effect then lost acknowledgement")

        monkeypatch.setattr(freecad, "closeDocument", close_then_raise)

    try:
        dock._sequence = 9_007_199_254_740_992
        dock.refresh()

        assert session.lifecycle == "stopping"
        assert host._session is session
        assert client.close_call_count == 0
        if close_outcome == "no-effect":
            assert len(freecad.documents) == 2
            host.deactivate_workbench()
            settle_workbench_events(
                session,
                lambda: (
                    sum(call[0] == "close_checkout" for call in client.calls) == 2
                    and session._cleanup_request_id is None
                ),
            )
            _fix04_settle_worker(session)
            settle_workbench_events(
                session,
                lambda: (
                    host.workbench_snapshot()["lifecycle"] == "inactive"
                    or (session._open_recovery_required and session._cleanup_request_id is None)
                ),
            )

            assert (
                session.lifecycle,
                session._open_recovery_required,
                client.close_call_count,
                freecad.documents,
                events[events_before:],
            ) == (
                "inactive",
                False,
                1,
                {},
                [
                    "document.close",
                    "document.close",
                    "document.close",
                    "checkout.close:" + "checkout_" + "6" * 32,
                    "checkout.close:" + "checkout_" + "7" * 32,
                    "client.close",
                ],
            )
            assert host._session is None
        else:
            assert effect_close_calls == 1
            assert len(freecad.documents) == 1
            retained_documents = dict(freecad.documents)
            host.deactivate_workbench()

            assert host._session is session
            assert session.lifecycle == "stopping"
            assert session._open_recovery_required is True
            assert effect_close_calls == 1
            assert freecad.documents == retained_documents
            assert not any(call[0] == "close_checkout" for call in client.calls)
            assert client.close_call_count == 0
    finally:
        freecad.close_failures = 0
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "exhaustion_phase",
    ("active-normal", "stopping-private"),
)
def test_fix04_cleanup_allocator_exhaustion_is_lifecycle_sensitive(
    monkeypatch: pytest.MonkeyPatch,
    exhaustion_phase: str,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.worker is not None
    dock = session.dock
    client = clients[0]
    gateway = session.worker.gateway
    original_handle = gateway.handle
    captured: list[tuple[dict[str, object], bool]] = []

    def observe_handle(command: object) -> object:
        payload = dict(command) if type(command) is dict else _fix04_wrapper_payload(command)
        captured.append((payload, type(command) is not dict))
        return original_handle(command)

    monkeypatch.setattr(gateway, "handle", observe_handle)
    try:
        if exhaustion_phase == "active-normal":
            first_private_id = host._NORMAL_ID_MAX + 1
            dock._sequence = first_private_id
            session._discard_preview_binding("checkout_" + "6" * 32)

            lifecycle_after_trigger = session.lifecycle
            recovery_after_trigger = session._open_recovery_required
            pending_after_trigger = tuple(
                request_id
                for request_id, pending in session._pending.items()
                if pending[2] == "preview_close"
            )
            emitted_after_trigger = tuple(
                payload["request_id"]
                for payload, _private in captured
                if payload.get("kind") == "preview_close"
            )

            host.deactivate_workbench()
            pump_main_events(
                lambda: (
                    host.workbench_snapshot()["lifecycle"] == "inactive"
                    or (session._open_recovery_required and session._cleanup_request_id is None)
                )
            )
            preview_close_commands = [
                (payload, private)
                for payload, private in captured
                if payload.get("kind") == "preview_close"
            ]
            client_close_commands = [
                (payload, private)
                for payload, private in captured
                if payload.get("kind") == "close"
            ]
            checkout_close_ids = [
                call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
            ]

            assert (
                lifecycle_after_trigger,
                recovery_after_trigger,
                pending_after_trigger,
                emitted_after_trigger,
                [(payload["request_id"], private) for payload, private in preview_close_commands],
                [(payload["request_id"], private) for payload, private in client_close_commands],
                checkout_close_ids,
                client.close_call_count,
                freecad.documents,
                session.lifecycle,
                host._session,
            ) == (
                "stopping",
                False,
                (),
                (),
                [
                    (first_private_id, True),
                    (first_private_id + 1, True),
                ],
                [(first_private_id + 2, True)],
                [
                    "checkout_" + "6" * 32,
                    "checkout_" + "7" * 32,
                ],
                1,
                {},
                "inactive",
                None,
            )
            return

        session._cleanup_cursor = host._MAX_SAFE_INTEGER + 1
        host.deactivate_workbench()
        _fix04_settle_worker(session)

        assert session.lifecycle == "stopping"
        assert session._open_recovery_required is True
        assert host._session is session
        assert not any(pending[2] == "preview_close" for pending in session._pending.values())
        assert not any(
            payload.get("kind") in {"preview_close", "close"} for payload, _private in captured
        )
        assert not any(call[0] == "close_checkout" for call in client.calls)
        assert client.close_call_count == 0
        assert freecad.documents == {}
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_project_switch_immediately_revokes_live_same_project_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    try:
        _fix04_refresh_cycle(host, dock, clients[0])
        assert dock.preview_projection().review_eligible is True

        project_b = "project_" + "2" * 32
        dock._project_ids.append(project_b)
        dock.project_selector.addItem(project_b)
        dock.project_selector.setCurrentIndex(1)

        assert dock.current_project_id() == project_b
        assert dock.preview_projection().review_eligible is False
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_selected_task_project_drift_revokes_review_authority_and_retires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    try:
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is True
        assert set(session._review_tokens) == {"head", "draft"}
        checkout_ids = tuple(dock._preview_checkouts.values())
        assert len(checkout_ids) == 2
        assert len(set(checkout_ids)) == 2

        selected_task = dock._selected_task()
        task_id = dock.current_task_id()
        assert is_dataclass(selected_task)
        assert type(task_id) is str
        drifted_task = replace(
            selected_task,
            project_id="project_" + "2" * 32,
        )
        dock._tasks_by_id[task_id] = drifted_task
        assert {
            field.name
            for field in fields(selected_task)
            if getattr(selected_task, field.name) != getattr(drifted_task, field.name)
        } == {"project_id"}
        assert dock._selected_task() is drifted_task

        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")
        _fix04_settle_worker(session)

        assert _fix04_authenticated_review_commands(session) == ()
        assert not any(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
        assert dock.preview_projection().review_eligible is False
        assert session._review_tokens == {}
        assert session._refresh_candidates == {}
        assert session._fresh_preview_descriptors == {}
        cycle = coordinator._cycle
        assert cycle is not None
        assert cycle.poisoned is True

        refreshes_before = sum(call[0] == "get_checkout" for call in client.calls)
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        dock.refresh()
        pump_main_events(
            lambda: (
                sum(call[0] == "get_checkout" for call in client.calls) == refreshes_before + 2
                and host.workbench_snapshot()["heartbeat_count"] >= heartbeat_before + 5
            )
        )
        assert sum(call[0] == "get_checkout" for call in client.calls) == refreshes_before + 2
        assert coordinator._cycle is cycle
        assert cycle.poisoned is True
        assert session._review_tokens == {}
        assert session._refresh_candidates == {}
        assert session._fresh_preview_descriptors == {}
        assert dock.preview_projection().review_eligible is False

        host.deactivate_workbench()
        _fix04_settle_worker(session)
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
        closed_checkout_ids = [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ]

        assert sorted(closed_checkout_ids) == sorted(checkout_ids)
        assert client.close_call_count == 1
        assert freecad.documents == {}
        assert coordinator._cycle is None
        assert session.lifecycle == "inactive"
        assert host._session is None
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_cross_project_preview_authorities_require_one_exact_project_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_a = "project_" + "1" * 32
    project_b = "project_" + "2" * 32
    task_b = _task_record(
        "2",
        generation=4,
        candidate_revision="revision_" + "8" * 32,
        draft_id="draft_" + "8" * 32,
    )
    task_b["status"] = "awaiting_user_review"
    task_b_id = task_b["task_id"]
    task_b_draft = task_b["draft_id"]
    assert type(task_b_id) is str
    assert type(task_b_draft) is str
    task_a = _task_record(
        "1",
        generation=3,
        candidate_revision="revision_" + "4" * 32,
        draft_id="draft_" + "4" * 32,
    )
    task_a["status"] = "awaiting_user_review"
    projects_response = _project_envelope(
        [
            _project_record("1", generation=2),
            _project_record("2", generation=3),
        ],
        next_cursor=None,
    )
    tasks_response = _task_envelope([task_a, task_b], next_cursor=None)

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient(
            draft_project_by_task_id={
                task_b_id: project_b,
            },
        )
        client.projects_response = projects_response
        client.tasks_response = tasks_response
        original_get_task = client.get_task_request

        def get_task(request: dict[str, object]) -> dict[str, object]:
            response = original_get_task(request)
            if request.get("task_id") != task_b_id:
                return response
            result = response["result"]
            assert type(result) is dict
            task_run = result["task_run"]
            assert type(task_run) is dict
            draft = task_run["draft"]
            assert type(draft) is dict
            result["generation"] = 4
            result["next_action"] = "review"
            task_run["project_id"] = project_b
            task_run["status"] = "awaiting_user_review"
            task_run["candidate_revision"] = "revision_" + "8" * 32
            task_run["committed_revision"] = None
            draft["id"] = task_b_draft
            draft["project_id"] = project_b
            draft["revision_id"] = "revision_" + "8" * 32
            return response

        client.get_task_request = get_task
        return client

    host, freecad, freecad_gui, clients, _events = _start_fix02_host(
        monkeypatch,
        client_factory=make_client,
    )
    first_session = host._session
    assert first_session is not None
    assert first_session.dock is not None
    first_dock = first_session.dock
    first_client = clients[0]
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    try:
        first_dock.open_head_button.click()
        pump_main_events(
            lambda: (
                first_dock._preview_checkouts.get("head") is not None
                and len(freecad.documents) == 1
            )
        )
        first_dock.project_selector.setCurrentIndex(1)
        eligibility_immediately_after_switch = first_dock.preview_projection().review_eligible
        pump_main_events(
            lambda: (
                first_dock.current_project_id() == project_b
                and first_dock.current_task_id() == task_b_id
            )
        )
        first_dock.open_draft_button.click()
        pump_main_events(
            lambda: (
                first_dock._preview_checkouts.get("draft") is not None
                and len(freecad.documents) == 2
            )
        )

        first_coordinator = first_session.preview
        assert first_coordinator is not None
        mixed_checkout_ids = dict(first_dock._preview_checkouts)
        mixed_projects = tuple(
            dict(first_coordinator.binding_for_checkout(mixed_checkout_ids[kind]).descriptor)[
                "source"
            ]["project_id"]
            for kind in ("head", "draft")
        )
        _fix04_refresh_cycle(host, first_dock, first_client)
        mixed_aggregate = first_coordinator.aggregate_review_eligible()
        mixed_tokens = bool(first_session._review_tokens)
        mixed_projection = first_dock.preview_projection().review_eligible
        mixed_rejected = False
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        try:
            first_session._request_review(
                decision="accept",
                task_id=task_b_id,
                draft_id=task_b_draft,
                expected_generation=4,
            )
        except preview_error:
            mixed_rejected = True
            _fix04_settle_worker(first_session)
        else:
            pump_main_events(
                lambda: (
                    sum(call[0] == "accept_draft" for call in first_client.calls) == 1
                    and host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
                )
            )
        mixed_review_commands = _fix04_authenticated_review_commands(first_session)
        mixed_daemon_reviews = sum(
            call[0] in {"accept_draft", "reject_draft"} for call in first_client.calls
        )

        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
        first_closed_ids = [
            call[1]["checkout_id"] for call in first_client.calls if call[0] == "close_checkout"
        ]
        first_cycle_fully_retired = (
            first_session.preview is not None
            and first_session.preview._active_cycle_id() is None
            and first_session.lifecycle == "inactive"
            and first_client.close_call_count == 1
            and len(first_closed_ids) == 2
            and len(set(first_closed_ids)) == 2
            and freecad.documents == {}
            and host._session is None
        )

        host.activate_workbench()
        pump_main_events(
            lambda: (
                len(clients) == 2
                and bool(freecad_gui.main_window.docks)
                and bool(freecad_gui.main_window.docks[0].task_selector.items)
            )
        )
        clean_session = host._session
        assert clean_session is not None
        assert clean_session.dock is not None
        clean_dock = clean_session.dock
        clean_client = clients[1]
        clean_dock.project_selector.setCurrentIndex(1)
        pump_main_events(
            lambda: (
                clean_dock.current_project_id() == project_b
                and clean_dock.current_task_id() == task_b_id
            )
        )
        clean_dock.open_head_button.click()
        clean_dock.open_draft_button.click()
        pump_main_events(
            lambda: (
                set(clean_dock._preview_checkouts) == {"head", "draft"}
                and len(freecad.documents) == 2
            )
        )
        clean_coordinator = clean_session.preview
        assert clean_coordinator is not None
        clean_checkout_ids = dict(clean_dock._preview_checkouts)
        clean_projects = tuple(
            dict(clean_coordinator.binding_for_checkout(clean_checkout_ids[kind]).descriptor)[
                "source"
            ]["project_id"]
            for kind in ("head", "draft")
        )
        _fix04_refresh_cycle(host, clean_dock, clean_client)
        clean_aggregate = clean_coordinator.aggregate_review_eligible()
        clean_tokens = bool(clean_session._review_tokens)
        clean_projection = clean_dock.preview_projection().review_eligible
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        clean_session._request_review(
            decision="accept",
            task_id=task_b_id,
            draft_id=task_b_draft,
            expected_generation=4,
        )
        pump_main_events(
            lambda: (
                sum(call[0] == "accept_draft" for call in clean_client.calls) == 1
                and host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
            )
        )
        with pytest.raises(preview_error):
            clean_session._request_review(
                decision="accept",
                task_id=task_b_id,
                draft_id=task_b_draft,
                expected_generation=4,
            )
        _fix04_settle_worker(clean_session)
        clean_review_commands = _fix04_authenticated_review_commands(clean_session)
        clean_daemon_reviews = sum(
            call[0] in {"accept_draft", "reject_draft"} for call in clean_client.calls
        )

        assert (
            eligibility_immediately_after_switch,
            mixed_projects,
            mixed_aggregate,
            mixed_tokens,
            mixed_projection,
            mixed_rejected,
            len(mixed_review_commands),
            mixed_daemon_reviews,
            first_cycle_fully_retired,
            clean_projects,
            clean_aggregate,
            clean_tokens,
            clean_projection,
            len(clean_review_commands),
            clean_daemon_reviews,
            (
                clean_review_commands[0]["task_id"],
                clean_review_commands[0]["draft_id"],
                clean_review_commands[0]["expected_generation"],
            ),
        ) == (
            False,
            (project_a, project_b),
            False,
            False,
            False,
            True,
            0,
            0,
            True,
            (project_b, project_b),
            True,
            True,
            True,
            1,
            1,
            (task_b_id, task_b_draft, 4),
        )
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "local_observation",
    (
        "unchanged",
        "same-path-save-reset",
        "save-as",
        "external-same-path-rewrite",
    ),
)
def test_fix04_review_requires_final_local_file_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    local_observation: str,
) -> None:
    baseline = b"VibeCAD clean managed model\n"
    changed = b"X" * len(baseline)
    assert changed != baseline
    assert len(changed) == len(baseline)
    managed_root = (tmp_path / "managed").resolve()
    host, freecad, freecad_gui, clients, _events = _start_fix02_host(
        monkeypatch,
        client_factory=lambda: FakeLocalAgentClient(
            materialized_checkout_root=managed_root,
            materialized_model_bytes=baseline,
        ),
    )
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
    try:
        dock.open_head_button.click()
        dock.open_draft_button.click()
        pump_main_events(
            lambda: (
                set(dock._preview_checkouts) == {"head", "draft"} and len(freecad.documents) == 2
            )
        )
        coordinator = session.preview
        assert coordinator is not None
        bindings = {
            kind: coordinator.binding_for_checkout(checkout_id)
            for kind, checkout_id in dock._preview_checkouts.items()
        }
        for kind, binding in bindings.items():
            checkout_id = dock._preview_checkouts[kind]
            local_path = client.checkout_paths[checkout_id]
            descriptor = dict(binding.descriptor)
            claim = dict(binding.claim)
            observed = local_path.read_bytes()
            observed_digest = hashlib.sha256(observed).hexdigest()
            assert binding.document.FileName == str(local_path)
            assert observed == baseline
            assert (
                descriptor["current_model_sha256"],
                descriptor["current_size_bytes"],
                claim["local_path"],
                claim["current_model_sha256"],
                claim["current_size_bytes"],
            ) == (
                observed_digest,
                len(observed),
                str(local_path),
                observed_digest,
                len(observed),
            )

        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is True
        head_binding = bindings["head"]
        head_path = Path(head_binding.document.FileName)
        if local_observation == "same-path-save-reset":
            head_binding.document.Modified = True
            head_path.write_bytes(changed)
            head_binding.document.Modified = False
        elif local_observation == "save-as":
            saved_as = (tmp_path / "user-save-as.FCStd").resolve()
            saved_as.write_bytes(baseline)
            head_binding.document.FileName = str(saved_as)
            head_binding.document.Modified = False
        elif local_observation == "external-same-path-rewrite":
            head_path.write_bytes(changed)
            assert head_binding.document.Modified is False

        rejected = False
        heartbeat_before = host.workbench_snapshot()["heartbeat_count"]
        try:
            _fix04_request_review(session, decision="accept")
        except preview_error:
            rejected = True
            _fix04_settle_worker(session)
        else:
            pump_main_events(
                lambda: (
                    sum(call[0] == "accept_draft" for call in client.calls) == 1
                    and host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
                )
            )
        review_commands = _fix04_authenticated_review_commands(session)
        daemon_reviews = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
        aggregate_after = coordinator.aggregate_review_eligible()
        cycle = coordinator._cycle
        cycle_poisoned = cycle is not None and cycle.poisoned
        expected_unchanged = local_observation == "unchanged"
        assert (
            rejected,
            len(review_commands),
            daemon_reviews,
            aggregate_after,
            cycle_poisoned,
            dock.preview_projection().review_eligible,
        ) == (
            not expected_unchanged,
            1 if expected_unchanged else 0,
            1 if expected_unchanged else 0,
            expected_unchanged,
            not expected_unchanged,
            False,
        )
        if review_commands:
            assert (
                review_commands[0]["decision"],
                review_commands[0]["task_id"],
                review_commands[0]["draft_id"],
                review_commands[0]["expected_generation"],
            ) == (
                "accept",
                "task_" + "1" * 32,
                "draft_" + "4" * 32,
                3,
            )
        assert freecad_gui.main_window.docks == [dock]
        if not expected_unchanged:
            host.deactivate_workbench()
            pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
            closed_checkouts = [
                call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
            ]
            assert len(closed_checkouts) == 2
            assert len(set(closed_checkouts)) == 2
            assert client.close_call_count == 1
            assert freecad.documents == {}
            assert coordinator._cycle is None
            assert host._session is None
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_review_pre_effect_enqueue_failure_retains_exact_ambiguous_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    _fix04_refresh_cycle(host, dock, client)
    assert dock.preview_projection().review_eligible is True
    signal = _fix04_private_signal(session)
    failed = False

    def fail_before_emit(value: object) -> None:
        nonlocal failed
        if _fix04_wrapper_payload(value).get("kind") == "review":
            failed = True
            raise RuntimeError("synthetic pre-effect review enqueue failure")
        raise AssertionError("unexpected non-review dispatch")

    try:
        with monkeypatch.context() as enqueue_patch:
            enqueue_patch.setattr(signal, "emit", fail_before_emit)
            with pytest.raises(RuntimeError):
                _fix04_request_review(session, decision="accept")

        pending_after_failure = _fix04_pending_ids(session, dock, "review")
        assert failed is True
        assert len(pending_after_failure) == 1
        request_id = pending_after_failure[0]
        assert session._pending[request_id][5] is True
        assert session._review_enqueue_ambiguous_id == request_id
        assert session.lifecycle == "stopping"
        assert session._open_recovery_required is False
        assert not any(call[0] == "accept_draft" for call in client.calls)
        assert dock.preview_projection().review_eligible is False
        assert dock.preview_projection().recovery_required is True
        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")
        refresh_calls_before = sum(call[0] == "get_checkout" for call in client.calls)
        dock.refresh()
        _fix04_settle_worker(session)

        assert not any(call[0] == "accept_draft" for call in client.calls)
        assert sum(call[0] == "get_checkout" for call in client.calls) == refresh_calls_before
        assert _fix04_pending_ids(session, dock, "review") == pending_after_failure
        assert session._review_enqueue_ambiguous_id == request_id
        assert host._session is session
    finally:
        force_cleanup_workbench(host, freecad)


_REVIEW_ATTESTATION_FAILURES = (
    (
        "windows-file-id-mismatch",
        "windows-path-validation-failure",
        "second-fstat-drift",
        "windows-short-read",
    )
    if os.name == "nt"
    else (
        "wrong-euid",
        "second-fstat-drift",
        "lstat-device-mismatch",
        "lstat-inode-mismatch",
    )
)


@pytest.mark.parametrize("attestation_failure", _REVIEW_ATTESTATION_FAILURES)
def test_c02_review_attestor_identity_drift_is_sticky_before_review_effect(
    monkeypatch: pytest.MonkeyPatch,
    attestation_failure: str,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    preview_module = importlib.import_module("vibecad_workbench.preview")
    preview_error = preview_module.PreviewError
    original_fstat = preview_module.os.fstat
    original_lstat = getattr(preview_module.os, "lstat", None)
    original_geteuid = getattr(preview_module.os, "geteuid", None)
    stat_fields = (
        "st_mode",
        "st_dev",
        "st_ino",
        "st_nlink",
        "st_uid",
        "st_gid",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )

    def drifted_stat(observed: object, **changes: int) -> object:
        drifted = type("_DriftedStat", (), {})()
        for field_name in stat_fields:
            setattr(
                drifted,
                field_name,
                changes.get(field_name, getattr(observed, field_name)),
            )
        return drifted

    try:
        _fix04_refresh_cycle(host, dock, client)
        assert dock.preview_projection().review_eligible is True
        commands_before = len(_fix04_authenticated_review_commands(session))
        effects_before = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)

        if attestation_failure == "windows-file-id-mismatch":
            windows_files = preview_module._windows_files
            assert windows_files is not None
            original_capture = windows_files.capture_windows_fd

            def mismatched_capture(*args: object, **kwargs: object) -> object:
                observed = original_capture(*args, **kwargs)
                return replace(observed, file_id=observed.file_id ^ 1)

            monkeypatch.setattr(
                windows_files,
                "capture_windows_fd",
                mismatched_capture,
            )
        elif attestation_failure == "windows-path-validation-failure":
            windows_files = preview_module._windows_files
            assert windows_files is not None

            def reject_path_validation(*_args: object, **_kwargs: object) -> None:
                raise OSError("synthetic Windows path identity replacement")

            monkeypatch.setattr(
                windows_files,
                "validate_windows_path",
                reject_path_validation,
            )
        elif attestation_failure == "windows-short-read":
            windows_files = preview_module._windows_files
            assert windows_files is not None
            monkeypatch.setattr(
                windows_files,
                "pread",
                lambda *_args, **_kwargs: b"",
            )
        elif attestation_failure == "wrong-euid":
            assert callable(original_geteuid)
            monkeypatch.setattr(
                preview_module.os,
                "geteuid",
                lambda: original_geteuid() + 1,
            )
        elif attestation_failure == "second-fstat-drift":
            fstat_calls = 0

            def second_fstat_drift(descriptor: int) -> object:
                nonlocal fstat_calls
                fstat_calls += 1
                observed = original_fstat(descriptor)
                if fstat_calls == 2:
                    return drifted_stat(
                        observed,
                        st_mtime_ns=observed.st_mtime_ns + 1,
                    )
                return observed

            monkeypatch.setattr(preview_module.os, "fstat", second_fstat_drift)
        else:
            assert callable(original_lstat)
            mismatch_field = (
                "st_dev" if attestation_failure == "lstat-device-mismatch" else "st_ino"
            )

            def mismatched_lstat(local_path: object) -> object:
                observed = original_lstat(local_path)
                return drifted_stat(
                    observed,
                    **{mismatch_field: getattr(observed, mismatch_field) + 1},
                )

            monkeypatch.setattr(preview_module.os, "lstat", mismatched_lstat)

        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")

        assert coordinator._cycle is not None
        assert coordinator._cycle.poisoned is True
        assert coordinator.aggregate_review_eligible() is False
        assert dock.preview_projection().review_eligible is False
        assert len(_fix04_authenticated_review_commands(session)) == commands_before
        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == effects_before
        )
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO attestation contract")
def test_c02_review_attestor_opens_fifo_nonblocking_and_rejects_before_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    preview_module = importlib.import_module("vibecad_workbench.preview")
    preview_error = preview_module.PreviewError
    nonblocking = getattr(preview_module.os, "O_NONBLOCK", None)
    assert type(nonblocking) is int
    original_open = preview_module.os.open
    observed_flags: list[int] = []

    def guarded_fifo_open(local_path: object, flags: int) -> int:
        observed_flags.append(flags)
        if flags & nonblocking != nonblocking:
            raise BlockingIOError("synthetic guard prevented blocking FIFO open")
        return original_open(local_path, flags)

    try:
        _fix04_refresh_cycle(host, dock, client)
        head_id = dock._preview_checkouts["head"]
        head_binding = coordinator.binding_for_checkout(head_id)
        fifo_path = Path(head_binding.document.FileName)
        fifo_path.unlink()
        os.mkfifo(fifo_path)
        cycle = coordinator._cycle
        assert cycle is not None
        review_commands_before = len(_fix04_authenticated_review_commands(session))
        review_effects_before = sum(
            call[0] in {"accept_draft", "reject_draft"} for call in client.calls
        )
        monkeypatch.setattr(preview_module.os, "open", guarded_fifo_open)
        started = time.monotonic()

        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")

        assert time.monotonic() - started < 0.5
        assert len(observed_flags) == 2
        assert all(flags & nonblocking == nonblocking for flags in observed_flags)
        assert cycle.poisoned is True
        assert coordinator.aggregate_review_eligible() is False
        assert dock.preview_projection().review_eligible is False
        assert session._review_tokens == {}
        assert _fix04_pending_ids(session, dock, "review") == ()
        assert len(_fix04_authenticated_review_commands(session)) == review_commands_before
        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == review_effects_before
        )

        monkeypatch.setattr(preview_module.os, "open", original_open)
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
        assert client.close_call_count == 1
        assert session._thread_retired is True
        assert host._session is None
    finally:
        force_cleanup_workbench(host, freecad)


def test_c02_deactivate_retires_refresh_authority_before_cycle_and_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    close_entered = threading.Event()
    close_release = threading.Event()
    close_calls = 0
    original_close_checkout = client.close_checkout

    def block_first_close(*, checkout_id: str) -> dict[str, object]:
        nonlocal close_calls
        close_calls += 1
        response = original_close_checkout(checkout_id=checkout_id)
        if close_calls == 1:
            close_entered.set()
            if not close_release.wait(1.0):
                raise RuntimeError("synthetic checkout-close release deadline exceeded")
        return response

    try:
        _fix04_refresh_cycle(host, dock, client)
        assert session._refresh_barrier is not None
        assert session._review_tokens
        assert session._fresh_preview_descriptors
        monkeypatch.setattr(client, "close_checkout", block_first_close)

        host.deactivate_workbench()
        assert close_entered.wait(1.0)

        assert session.lifecycle == "stopping"
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert cycle.poisoned is True
        assert session._refresh_barrier is None
        assert session._review_tokens == {}
        assert session._refresh_candidates == {}
        assert session._fresh_preview_descriptors == {}
        assert set(dock._preview_checkouts) == {"head", "draft"}
        assert session._cleanup_request_id is not None
        assert client.close_call_count == 0
        assert session._thread_retired is False
        assert host._session is session

        close_release.set()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")
        closed_ids = [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ]
        assert len(closed_ids) == 2
        assert len(set(closed_ids)) == 2
        assert coordinator._active_cycle_id() is None
        assert dock._preview_checkouts == {}
        assert client.close_call_count == 1
        assert session._thread_retired is True
        assert host._session is None
    finally:
        close_release.set()
        force_cleanup_workbench(host, freecad)


def test_c02_ambiguous_review_blocks_cycle_finalize_until_exact_authenticated_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.worker is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    coordinator = session.preview
    client = clients[0]
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    signal = _fix04_private_signal(session)
    original_dispatch_emit = signal.emit
    original_event_emit = session.worker.event_ready.emit
    review_held = threading.Event()
    held_events: list[object] = []
    failed = False

    def hold_exact_review(event: object) -> None:
        if _fix04_wrapper_payload(event).get("kind") == "review":
            held_events.append(event)
            review_held.set()
            return
        original_event_emit(event)

    def emit_then_raise(value: object) -> None:
        nonlocal failed
        if _fix04_wrapper_payload(value).get("kind") == "review" and not failed:
            failed = True
            original_dispatch_emit(value)
            raise RuntimeError("synthetic post-effect review enqueue failure")
        original_dispatch_emit(value)

    try:
        _fix04_refresh_cycle(host, dock, client)
        monkeypatch.setattr(session.worker.event_ready, "emit", hold_exact_review)
        with monkeypatch.context() as enqueue_patch:
            enqueue_patch.setattr(signal, "emit", emit_then_raise)
            with pytest.raises(RuntimeError, match="post-effect"):
                _fix04_request_review(session, decision="accept")
        assert review_held.wait(1.0)
        pending = _fix04_pending_ids(session, dock, "review")
        assert len(pending) == 1
        assert session._review_enqueue_ambiguous_id == pending[0]
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1

        host.deactivate_workbench()
        pump_main_events(
            lambda: (
                [call[0] for call in client.calls].count("close_checkout") == 2
                and session._cleanup_request_id is None
                and dock._preview_checkouts == {}
            )
        )

        assert coordinator.cleanup_complete() is True
        assert coordinator._cycle is cycle
        assert coordinator._active_cycle_id() == cycle_id
        assert session._cleanup_cycle_id == cycle_id
        assert _fix04_pending_ids(session, dock, "review") == pending
        assert session._review_enqueue_ambiguous_id == pending[0]
        assert session._refresh_barrier is None
        assert client.close_call_count == 0
        assert session._thread_retired is False
        assert host._session is session
        assert len(held_events) == 1

        session._receive(held_events[0])
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

        assert session._review_enqueue_ambiguous_id is None
        assert _fix04_pending_ids(session, dock, "review") == ()
        assert coordinator._active_cycle_id() is None
        assert client.close_call_count == 1
        assert session._thread_retired is True
        assert host._session is None
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_review_effect_then_raise_stops_until_correlated_safe_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    entered = threading.Event()
    release = threading.Event()
    client.review_entered = entered
    client.review_release = release
    _fix04_refresh_cycle(host, dock, client)
    assert dock.preview_projection().review_eligible is True
    signal = _fix04_private_signal(session)
    original_emit = signal.emit
    failed = False
    refresh_calls_before = sum(call[0] == "get_checkout" for call in client.calls)
    heartbeat_before = host.workbench_snapshot()["heartbeat_count"]

    def emit_then_raise(value: object) -> None:
        nonlocal failed
        if _fix04_wrapper_payload(value).get("kind") == "review" and not failed:
            failed = True
            original_emit(value)
            raise RuntimeError("synthetic post-effect review enqueue failure")
        original_emit(value)

    try:
        with monkeypatch.context() as enqueue_patch:
            enqueue_patch.setattr(signal, "emit", emit_then_raise)
            with pytest.raises(RuntimeError):
                _fix04_request_review(session, decision="accept")

        assert entered.wait(1.0)
        host.deactivate_workbench()
        lifecycle_after_raise = session.lifecycle
        global_recovery_after_raise = session._open_recovery_required
        projected_recovery_after_raise = dock.preview_projection().recovery_required
        pending_after_raise = _fix04_pending_ids(session, dock, "review")
        assert len(pending_after_raise) == 1
        assert session._review_enqueue_ambiguous_id == pending_after_raise[0]
        assert host._session is session
        host.activate_workbench()
        assert host._session is session

        gateway_module = importlib.import_module("vibecad_workbench.gateway")
        event_type = _fix04_wrapper_type("event", gateway_module)
        capability = _fix04_session_capability(session)
        exact = {
            "schema_version": 1,
            "request_id": pending_after_raise[0],
            "kind": "review",
            "response": {
                "schema_version": 1,
                "ok": True,
                "result": {},
                "error": None,
            },
        }
        wrong_response = dict(exact)
        wrong_response["response"] = {}
        session._receive(dict(exact))
        session._receive(_fix04_wrap(event_type, exact, object()))
        session._receive(
            _fix04_wrap(
                event_type,
                dict(exact) | {"request_id": pending_after_raise[0] + 1},
                capability,
            )
        )
        session._receive(_fix04_wrap(event_type, wrong_response, capability))
        assert _fix04_pending_ids(session, dock, "review") == pending_after_raise
        assert session._review_enqueue_ambiguous_id == pending_after_raise[0]
        assert session._thread_retired is False
        assert client.close_call_count == 0
        assert host._session is session

        preview_error = importlib.import_module("vibecad_workbench.preview").PreviewError
        with pytest.raises(preview_error):
            _fix04_request_review(session, decision="accept")

        release.set()
        _fix04_settle_worker(session)
        pump_main_events(
            lambda: (
                host.workbench_snapshot()["heartbeat_count"] > heartbeat_before
                and not _fix04_pending_ids(session, dock, "review")
                and host.workbench_snapshot()["lifecycle"] == "inactive"
            )
        )
        review_calls = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
        refresh_calls = sum(call[0] == "get_checkout" for call in client.calls)

        assert (
            failed,
            lifecycle_after_raise,
            global_recovery_after_raise,
            projected_recovery_after_raise,
            review_calls,
            refresh_calls == refresh_calls_before,
        ) == (True, "stopping", False, True, 1, True)
        assert session._review_enqueue_ambiguous_id is None
        assert [call[0] for call in client.calls].count("close_checkout") == 2
        assert client.close_call_count == 1
        assert freecad.documents == {}
        assert host._session is None
    finally:
        release.set()
        force_cleanup_workbench(host, freecad)


def test_fix04_pre_effect_review_enqueue_failure_retains_ambiguous_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    signal = _fix04_private_signal(session)
    original_emit = signal.emit
    failed = False

    def raise_before_effect(value: object) -> None:
        nonlocal failed
        if _fix04_wrapper_payload(value).get("kind") == "review" and not failed:
            failed = True
            raise RuntimeError("synthetic pre-effect review enqueue failure")
        original_emit(value)

    try:
        _fix04_refresh_cycle(host, dock, client)
        with monkeypatch.context() as enqueue_patch:
            enqueue_patch.setattr(signal, "emit", raise_before_effect)
            with pytest.raises(RuntimeError, match="pre-effect"):
                _fix04_request_review(session, decision="accept")

        pending = _fix04_pending_ids(session, dock, "review")
        assert len(pending) == 1
        assert session._review_enqueue_ambiguous_id == pending[0]
        assert session.lifecycle == "stopping"
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 0

        host.deactivate_workbench()
        _fix04_settle_worker(session)
        pump_main_events(lambda: [call[0] for call in client.calls].count("close_checkout") == 2)
        host.deactivate_workbench()
        _fix04_settle_worker(session)

        assert _fix04_pending_ids(session, dock, "review") == pending
        assert session._review_enqueue_ambiguous_id == pending[0]
        assert session._client_close_requested is False
        assert session._thread_retired is False
        assert client.close_call_count == 0
        assert host._session is session
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 0
    finally:
        force_cleanup_workbench(host, freecad)


def test_fix04_exact_authenticated_closed_event_authorizes_normal_retirement_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = _start_fail_cleanup_host(monkeypatch)
    try:
        session = host._session
        assert session is not None
        assert session.worker is not None
        assert session.thread is not None
        assert session.dock is not None
        dock = session.dock
        thread = session.thread
        gateway = session.worker.gateway
        gateway_module = importlib.import_module("vibecad_workbench.gateway")
        capability = _fix04_session_capability(session)
        original_handle = gateway.handle
        returned: list[object] = []

        def observe_handle(command: object) -> object:
            event = original_handle(command)
            returned.append(event)
            return event

        monkeypatch.setattr(gateway, "handle", observe_handle)
        host.deactivate_workbench()
        pump_main_events(lambda: host.workbench_snapshot()["lifecycle"] == "inactive")

        closed_events = [
            event for event in returned if _fix04_wrapper_payload(event).get("kind") == "closed"
        ]
        assert len(closed_events) == 1
        assert type(closed_events[0]) is _fix04_wrapper_type("event", host, gateway_module)
        assert _fix04_wrapper_capability(closed_events[0]) is capability
        assert _fix04_wrapper_payload(closed_events[0]) == {
            "schema_version": 1,
            "request_id": session._close_request_id,
            "kind": "closed",
        }
        assert session._retirement_authorized is True
        assert session._thread_retired is True
        assert session.lifecycle == "inactive"
        assert session._dock_count == 0
        assert dock.parent() is None
        assert not thread.isRunning()
        assert host._session is None
        assert freecad.documents == {}
        assert session.preview is not None
        assert session.preview.cleanup_complete() is True
        assert clients[0].close_call_count == 1

        session._finished()
        session._finished()
        host.deactivate_workbench()
        host.deactivate_workbench()

        assert host._session is None
        assert session.lifecycle == "inactive"
        assert session._dock_count == 0
        assert clients[0].close_call_count == 1
    finally:
        force_cleanup_workbench(host, freecad)
