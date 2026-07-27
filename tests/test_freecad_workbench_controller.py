from __future__ import annotations

import ast
import importlib.util
import runpy
import sys
from collections.abc import Callable
from dataclasses import MISSING, fields, is_dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType

import pytest

from tests.fixtures.freecad_workbench.fake_host import (
    FakeFreeCADGui,
    FakeWorkbench,
    make_fake_freecad_gui,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_ROOT = _REPO_ROOT / "freecad" / "VibeCAD"
_INIT_GUI = _ADDON_ROOT / "InitGui.py"
_STATE = _ADDON_ROOT / "vibecad_workbench" / "state.py"
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
