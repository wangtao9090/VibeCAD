"""Bounded, snapshot-bound task discovery without CAD dependencies."""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
from pathlib import Path

import pytest

from vibecad.execution.revisions import LocalRevisionStore, RevisionStoreRootTrust
from vibecad.workflow.catalog import TaskCatalogService
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskStatus,
    transition_task,
)
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust

PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
STORE_KEY_DOMAIN = b"vibecad-task-store-key-v1\0"


def _stores(tmp_path: Path):
    roots = {name: tmp_path / name for name in ("locks", "tasks", "projects")}
    for root in roots.values():
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
    leases = ResourceLeaseManager(roots["locks"], trust=LeaseRootTrust.TRUSTED_LOCAL)
    tasks = TaskRunStore(
        roots["tasks"],
        leases,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    revisions = LocalRevisionStore(
        roots["projects"],
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    with leases.acquire_project_write(PROJECT_ID) as lease:
        revisions.initialize_empty_project(PROJECT_ID, lease)
    return tasks, revisions


def _catalog(tmp_path: Path) -> TaskCatalogService:
    tasks, revisions = _stores(tmp_path)
    return TaskCatalogService(task_store=tasks, revision_store=revisions)


def _create(catalog: TaskCatalogService, index: int):
    return catalog.create_task(
        task_id=f"task_{index:032x}",
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )


def _service(catalog: TaskCatalogService):
    module = importlib.import_module("vibecad.application.discovery")
    return module.TaskDiscoveryService(catalog=catalog)


def test_list_tasks_pages_in_canonical_order_and_limit_does_not_bind_cursor(
    tmp_path: Path,
):
    catalog = _catalog(tmp_path)
    for index in (3, 1, 2):
        _create(catalog, index)
    discovery = _service(catalog)

    first = discovery.list_tasks(limit=1, cursor=None)
    second = discovery.list_tasks(limit=2, cursor=first["next_cursor"])

    assert [item["task_id"] for item in first["tasks"]] == [f"task_{1:032x}"]
    assert [item["task_id"] for item in second["tasks"]] == [
        f"task_{2:032x}",
        f"task_{3:032x}",
    ]
    assert second["next_cursor"] is None
    assert set(first["tasks"][0]) == {
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
    }


def test_list_cursor_conflicts_after_any_catalog_snapshot_change(tmp_path: Path):
    catalog = _catalog(tmp_path)
    _create(catalog, 1)
    _create(catalog, 2)
    discovery = _service(catalog)
    first = discovery.list_tasks(limit=1, cursor=None)
    _create(catalog, 3)

    with pytest.raises(Exception) as caught:
        discovery.list_tasks(limit=1, cursor=first["next_cursor"])

    assert getattr(caught.value, "code", None).value == "conflict"


def test_task_events_page_uses_transition_sequence_and_stales_on_generation_change(
    tmp_path: Path,
):
    catalog = _catalog(tmp_path)
    created = _create(catalog, 1)
    program = ModelProgram(
        task_id=created.task_run.id,
        base_revision=created.task_run.base_revision,
        operations=(),
        acceptance=AcceptanceSpec(id="events", criteria=()),
    )
    current = catalog.compare_and_set(
        created,
        transition_task(created.task_run, TaskEvent.SUBMIT_PROGRAM, program=program),
    )
    discovery = _service(catalog)

    first = discovery.get_task_events(
        task_id=current.task_run.id,
        limit=1,
        cursor=None,
    )
    assert first["task_id"] == current.task_run.id
    assert first["generation"] == current.generation
    assert [item["sequence"] for item in first["transitions"]] == [1]

    advanced = catalog.compare_and_set(
        current,
        transition_task(current.task_run, TaskEvent.START_VALIDATION),
    )
    assert advanced.task_run.status is TaskStatus.VALIDATING_PROGRAM
    with pytest.raises(Exception) as caught:
        discovery.get_task_events(
            task_id=current.task_run.id,
            limit=1,
            cursor=first["next_cursor"],
        )
    assert getattr(caught.value, "code", None).value == "conflict"


@pytest.mark.parametrize("method", ["list_tasks", "get_task_events"])
def test_discovery_module_has_no_cad_or_runtime_imports(method: str):
    module = importlib.import_module("vibecad.application.discovery")
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert hasattr(module.TaskDiscoveryService, method)
    assert "FreeCAD" not in source
    assert "vibecad.interaction.cad" not in source
    assert "vibecad.application.project" not in source


def test_list_cursor_survives_reopen_but_conflicts_in_an_identical_other_store(
    tmp_path: Path,
):
    catalog = _catalog(tmp_path)
    _create(catalog, 1)
    _create(catalog, 2)
    first = _service(catalog).list_tasks(limit=1, cursor=None)
    cursor = first["next_cursor"]

    reopened_leases = ResourceLeaseManager(
        tmp_path / "locks",
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    reopened_tasks = TaskRunStore(
        tmp_path / "tasks",
        reopened_leases,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    reopened_revisions = LocalRevisionStore(
        tmp_path / "projects",
        reopened_leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    reopened = TaskCatalogService(
        task_store=reopened_tasks,
        revision_store=reopened_revisions,
    )
    assert [
        item["task_id"] for item in _service(reopened).list_tasks(limit=10, cursor=cursor)["tasks"]
    ] == [f"task_{2:032x}"]

    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    other_tasks, other_revisions = _stores(other)
    for source in (tmp_path / "tasks").iterdir():
        target = other / "tasks" / source.name
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
    foreign = _service(
        TaskCatalogService(
            task_store=other_tasks,
            revision_store=other_revisions,
        )
    )
    with pytest.raises(Exception) as caught:
        foreign.list_tasks(limit=10, cursor=cursor)
    assert getattr(caught.value, "code", None).value == "conflict"


def test_event_cursor_is_bound_to_task_and_store_namespace(tmp_path: Path):
    catalog = _catalog(tmp_path)
    first_task = _create(catalog, 1)
    second_task = _create(catalog, 2)
    discovery = _service(catalog)
    first = discovery.get_task_events(
        task_id=first_task.task_run.id,
        limit=1,
        cursor=None,
    )
    cursor = first["next_cursor"]
    assert cursor is None

    # Add a second persisted transition so the first page has a continuation.
    program = ModelProgram(
        task_id=first_task.task_run.id,
        base_revision=first_task.task_run.base_revision,
        operations=(),
        acceptance=AcceptanceSpec(id="foreign-events", criteria=()),
    )
    catalog.compare_and_set(
        first_task,
        transition_task(first_task.task_run, TaskEvent.SUBMIT_PROGRAM, program=program),
    )
    cursor = discovery.get_task_events(
        task_id=first_task.task_run.id,
        limit=1,
        cursor=None,
    )["next_cursor"]
    assert isinstance(cursor, str)

    with pytest.raises(Exception) as caught:
        discovery.get_task_events(
            task_id=second_task.task_run.id,
            limit=1,
            cursor=cursor,
        )
    assert getattr(caught.value, "code", None).value == "conflict"

    other = tmp_path / "event-other"
    other.mkdir(mode=0o700)
    other_tasks, other_revisions = _stores(other)
    for source in (tmp_path / "tasks").iterdir():
        target = other / "tasks" / source.name
        shutil.copyfile(source, target)
        os.chmod(target, 0o600)
    foreign = _service(
        TaskCatalogService(
            task_store=other_tasks,
            revision_store=other_revisions,
        )
    )
    with pytest.raises(Exception) as caught:
        foreign.get_task_events(
            task_id=first_task.task_run.id,
            limit=1,
            cursor=cursor,
        )
    assert getattr(caught.value, "code", None).value == "conflict"


def test_list_first_page_fails_when_a_later_task_record_is_corrupt(tmp_path: Path):
    catalog = _catalog(tmp_path)
    _create(catalog, 1)
    _create(catalog, 2)
    later_id = f"task_{2:032x}"
    filename = hashlib.sha256(STORE_KEY_DOMAIN + later_id.encode("ascii")).hexdigest() + ".json"
    path = tmp_path / "tasks" / filename
    raw = bytearray(path.read_bytes())
    raw[-2] = ord("0") if raw[-2] != ord("0") else ord("1")
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    before = path.read_bytes()

    with pytest.raises(Exception) as caught:
        _service(catalog).list_tasks(limit=1, cursor=None)

    assert getattr(caught.value, "code", None).value == "store_failure"
    assert path.read_bytes() == before
