"""Store-only task catalog contracts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import vibecad.workflow.catalog as catalog_module
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreRootTrust,
)
from vibecad.workflow.catalog import (
    TaskCatalogError,
    TaskCatalogErrorCode,
    TaskCatalogService,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskStatus,
    new_task_run,
    transition_task,
)
from vibecad.workflow.store import (
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
    TaskStoreRootTrust,
)

TASK_ID = "task_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
CREATE_KEY = "task_create_0123456789abcdef0123456789abcdef"
KEYED_TASK_ID = "task_e9f9dc52c8f75cd72feddee2648564b8"
CREATION_DIGEST = "e9f9dc52c8f75cd72feddee2648564b8b4bf0b07836368165d3a0c1fedeee1ef"
COLLISION_DIGEST = CREATION_DIGEST[:32] + "f" * 32


def _stores(tmp_path: Path):
    locks = tmp_path / "locks"
    tasks = tmp_path / "tasks"
    projects = tmp_path / "projects"
    for root in (locks, tasks, projects):
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)
    leases = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    task_store = TaskRunStore(tasks, leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    revision_store = LocalRevisionStore(
        projects,
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    with leases.acquire_project_write(PROJECT_ID) as lease:
        head = revision_store.initialize_empty_project(PROJECT_ID, lease)
    return leases, task_store, revision_store, head


def _program_for(task) -> ModelProgram:
    return ModelProgram(
        task_id=task.id,
        base_revision=task.base_revision,
        operations=(),
        acceptance=AcceptanceSpec(id="catalog-replay", criteria=()),
    )


def test_catalog_creates_and_gets_a_task_without_any_cad_port(tmp_path: Path):
    _leases, tasks, revisions, head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)

    created = catalog.create_task(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )

    assert created.generation == 0
    assert created.task_run.status is TaskStatus.NEEDS_PLAN
    assert created.task_run.base_revision == head.revision_id
    assert catalog.get_task(task_id=TASK_ID) == created
    assert set(TaskCatalogService.__dict__) >= {
        "create_task",
        "get_task",
        "reject_draft",
    }


def test_catalog_replays_current_generation_after_response_loss_and_restart(tmp_path: Path):
    _leases, tasks, revisions, _head = _stores(tmp_path)
    first = TaskCatalogService(task_store=tasks, revision_store=revisions)
    created = first.create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    current = first.compare_and_set(
        created,
        transition_task(
            created.task_run,
            TaskEvent.SUBMIT_PROGRAM,
            program=_program_for(created.task_run),
        ),
    )

    restarted = TaskCatalogService(task_store=tasks, revision_store=revisions)
    replayed = restarted.create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )

    assert replayed == current
    assert replayed.task_run.status is TaskStatus.PROGRAM_READY
    assert replayed.task_run.id == KEYED_TASK_ID
    assert replayed.task_run.creation_digest == CREATION_DIGEST


def test_catalog_replay_does_not_read_head_and_conflicting_intent_fails_closed(
    tmp_path: Path,
    monkeypatch,
):
    _leases, tasks, revisions, _head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    created = catalog.create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )

    def forbidden_head(*_args, **_kwargs):
        raise AssertionError("replay must not read current HEAD")

    monkeypatch.setattr(LocalRevisionStore, "load_head", forbidden_head)
    assert (
        catalog.create_task(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
        == created
    )
    for project_id, policy in (
        ("project_11111111111111111111111111111111", ReviewPolicy.AUTO_COMMIT),
        (PROJECT_ID, ReviewPolicy.REQUIRE_REVIEW),
    ):
        with pytest.raises(TaskCatalogError) as caught:
            catalog.create_task(
                create_key=CREATE_KEY,
                project_id=project_id,
                reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                review_policy=policy,
            )
        assert caught.value.code is TaskCatalogErrorCode.CONFLICT


@pytest.mark.parametrize("occupant", ["legacy", "prefix_collision"])
def test_catalog_never_adopts_a_legacy_or_full_digest_collision_occupant(
    tmp_path: Path,
    occupant: str,
):
    _leases, tasks, revisions, head = _stores(tmp_path)
    digest = None if occupant == "legacy" else COLLISION_DIGEST
    task = transition_task(
        new_task_run(
            task_id=KEYED_TASK_ID,
            project_id=PROJECT_ID,
            base_revision=head.revision_id,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
            creation_digest=digest,
        ),
        TaskEvent.REQUEST_PLAN,
    )
    tasks.create(task)

    with pytest.raises(TaskCatalogError) as caught:
        TaskCatalogService(task_store=tasks, revision_store=revisions).create_task(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )

    assert caught.value.code is TaskCatalogErrorCode.CONFLICT


def test_durability_uncertain_create_returns_a_matching_progressed_readback(
    tmp_path: Path,
    monkeypatch,
):
    _leases, tasks, revisions, _head = _stores(tmp_path)
    real_create = TaskRunStore.create

    def create_then_advance_and_lose_reply(store, task):
        created = real_create(store, task)
        progressed = transition_task(
            task,
            TaskEvent.SUBMIT_PROGRAM,
            program=_program_for(task),
        )
        store.compare_and_set(task.id, created.generation, progressed)
        raise TaskStoreError(
            TaskStoreErrorCode.DURABILITY_UNCERTAIN,
            committed_generation=0,
        )

    monkeypatch.setattr(TaskRunStore, "create", create_then_advance_and_lose_reply)
    result = TaskCatalogService(task_store=tasks, revision_store=revisions).create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )

    assert result.generation == 1
    assert result.task_run.status is TaskStatus.PROGRAM_READY
    assert result.task_run.id == KEYED_TASK_ID
    assert result.task_run.creation_digest == CREATION_DIGEST


def test_concurrent_same_create_key_publishes_exactly_one_task_record(tmp_path: Path):
    _leases, tasks, revisions, _head = _stores(tmp_path)
    barrier = threading.Barrier(8)
    results = []
    failures = []

    def create() -> None:
        try:
            barrier.wait()
            results.append(
                TaskCatalogService(task_store=tasks, revision_store=revisions).create_task(
                    create_key=CREATE_KEY,
                    project_id=PROJECT_ID,
                    reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                    review_policy=ReviewPolicy.AUTO_COMMIT,
                )
            )
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == [], [
        (type(error).__name__, getattr(error, "code", None)) for error in failures
    ]
    assert len(results) == 8
    assert {item.task_run.id for item in results} == {KEYED_TASK_ID}
    assert {item.generation for item in results} == {0}
    assert len(tuple((tmp_path / "tasks").glob("*.json"))) == 1


def test_keyed_create_retry_progress_is_not_spent_while_the_caller_is_descheduled(
    tmp_path: Path,
    monkeypatch,
):
    _leases, tasks, revisions, _head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    winner = catalog.create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    real_load = TaskRunStore.load
    load_calls = 0
    create_calls = 0
    clock = iter((0.0, 2.0))

    def hide_winner_once(store, task_id):
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
        return real_load(store, task_id)

    def contended_create(_store, _task):
        nonlocal create_calls
        create_calls += 1
        raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE)

    monkeypatch.setattr(TaskRunStore, "load", hide_winner_once)
    monkeypatch.setattr(TaskRunStore, "create", contended_create)
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(catalog_module.time, "sleep", lambda _delay: None)

    assert (
        catalog.create_task(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
        == winner
    )
    assert load_calls == 2
    assert create_calls == 1


def test_keyed_create_lock_retry_attempts_remain_bounded(tmp_path: Path, monkeypatch):
    _leases, tasks, revisions, head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    load_calls = 0
    create_calls = 0

    def missing(_store, _task_id):
        nonlocal load_calls
        load_calls += 1
        raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)

    def contended(_store, _task):
        nonlocal create_calls
        create_calls += 1
        raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE)

    monkeypatch.setattr(TaskRunStore, "load", missing)
    monkeypatch.setattr(TaskRunStore, "create", contended)
    monkeypatch.setattr(LocalRevisionStore, "load_head", lambda _store, _project_id: head)
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(catalog_module.time, "sleep", lambda _delay: None)

    with pytest.raises(TaskCatalogError) as caught:
        catalog.create_task(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )

    assert caught.value.code is TaskCatalogErrorCode.STORE_FAILURE
    expected_attempts = catalog_module._REPLAY_RETRY_LIMIT + 1
    assert load_calls == expected_attempts
    assert create_calls == expected_attempts


def test_keyed_create_post_deadline_grace_is_bounded_and_backed_off(
    tmp_path: Path,
    monkeypatch,
):
    _leases, tasks, revisions, head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    load_calls = 0
    create_calls = 0
    clock_calls = 0
    sleep_delays = []

    def missing(_store, _task_id):
        nonlocal load_calls
        load_calls += 1
        raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)

    def contended(_store, _task):
        nonlocal create_calls
        create_calls += 1
        raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE)

    def jump_past_deadline():
        nonlocal clock_calls
        clock_calls += 1
        return 0.0 if clock_calls == 1 else 2.0

    monkeypatch.setattr(TaskRunStore, "load", missing)
    monkeypatch.setattr(TaskRunStore, "create", contended)
    monkeypatch.setattr(LocalRevisionStore, "load_head", lambda _store, _project_id: head)
    monkeypatch.setattr(catalog_module.time, "monotonic", jump_past_deadline)
    monkeypatch.setattr(catalog_module.time, "sleep", sleep_delays.append)

    with pytest.raises(TaskCatalogError) as caught:
        catalog.create_task(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )

    assert caught.value.code is TaskCatalogErrorCode.STORE_FAILURE
    expected_attempts = catalog_module._REPLAY_DEADLINE_GRACE_RETRY_LIMIT + 1
    assert load_calls == expected_attempts
    assert create_calls == expected_attempts
    assert len(sleep_delays) == catalog_module._REPLAY_DEADLINE_GRACE_RETRY_LIMIT
    assert max(sleep_delays) == catalog_module._REPLAY_DEADLINE_GRACE_DELAY_CAP_SECONDS


def test_durability_uncertain_readback_uses_the_same_retry_budget(
    tmp_path: Path,
    monkeypatch,
):
    _leases, tasks, revisions, head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    load_calls = 0
    create_calls = 0

    def missing_then_contended(_store, _task_id):
        nonlocal load_calls
        load_calls += 1
        if load_calls <= 2:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
        raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE)

    def contend_then_lose_reply(_store, _task):
        nonlocal create_calls
        create_calls += 1
        if create_calls == 1:
            raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE)
        raise TaskStoreError(
            TaskStoreErrorCode.DURABILITY_UNCERTAIN,
            committed_generation=0,
        )

    monkeypatch.setattr(TaskRunStore, "load", missing_then_contended)
    monkeypatch.setattr(TaskRunStore, "create", contend_then_lose_reply)
    monkeypatch.setattr(LocalRevisionStore, "load_head", lambda _store, _project_id: head)
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(catalog_module.time, "sleep", lambda _delay: None)

    with pytest.raises(TaskCatalogError) as caught:
        catalog.create_task(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )

    assert caught.value.code is TaskCatalogErrorCode.STORE_FAILURE
    assert create_calls == 2
    assert load_calls == catalog_module._REPLAY_RETRY_LIMIT + 2


def test_already_exists_gets_a_final_readback_after_retry_budget_exhaustion(
    tmp_path: Path,
    monkeypatch,
):
    _leases, tasks, revisions, head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    winner = catalog.create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    real_load = TaskRunStore.load
    load_calls = 0
    create_calls = 0

    def hide_winner_until_final_readback(store, task_id):
        nonlocal load_calls
        load_calls += 1
        if load_calls <= catalog_module._REPLAY_RETRY_LIMIT + 1:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
        return real_load(store, task_id)

    def contend_until_already_exists(_store, _task):
        nonlocal create_calls
        create_calls += 1
        if create_calls <= catalog_module._REPLAY_RETRY_LIMIT:
            raise TaskStoreError(TaskStoreErrorCode.LOCK_UNAVAILABLE)
        raise TaskStoreError(TaskStoreErrorCode.ALREADY_EXISTS)

    monkeypatch.setattr(TaskRunStore, "load", hide_winner_until_final_readback)
    monkeypatch.setattr(TaskRunStore, "create", contend_until_already_exists)
    monkeypatch.setattr(LocalRevisionStore, "load_head", lambda _store, _project_id: head)
    monkeypatch.setattr(catalog_module.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(catalog_module.time, "sleep", lambda _delay: None)

    assert (
        catalog.create_task(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
        == winner
    )
    assert create_calls == catalog_module._REPLAY_RETRY_LIMIT + 1
    assert load_calls == catalog_module._REPLAY_RETRY_LIMIT + 2


def test_unrelated_catalog_lock_release_allows_keyed_create_to_become_the_winner(
    tmp_path: Path,
):
    leases, tasks, revisions, _head = _stores(tmp_path)
    held = leases.acquire("task-store:catalog")
    results = []
    failures = []

    def create() -> None:
        try:
            results.append(
                TaskCatalogService(task_store=tasks, revision_store=revisions).create_task(
                    create_key=CREATE_KEY,
                    project_id=PROJECT_ID,
                    reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                    review_policy=ReviewPolicy.AUTO_COMMIT,
                )
            )
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=create)
    thread.start()
    time.sleep(0.02)
    assert thread.is_alive()
    held.release(owner_token=held.owner_token)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert len(results) == 1
    assert results[0].task_run.id == KEYED_TASK_ID
    assert results[0].task_run.creation_digest == CREATION_DIGEST


def test_exact_already_exists_race_reloads_and_replays_the_winner(
    tmp_path: Path,
    monkeypatch,
):
    _leases, tasks, revisions, _head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    winner = catalog.create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    real_load = TaskRunStore.load
    load_calls = 0

    def hide_winner_once(store, task_id):
        nonlocal load_calls
        load_calls += 1
        if load_calls == 1:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND)
        return real_load(store, task_id)

    def already_exists(_store, _task):
        raise TaskStoreError(TaskStoreErrorCode.ALREADY_EXISTS)

    monkeypatch.setattr(TaskRunStore, "load", hide_winner_once)
    monkeypatch.setattr(TaskRunStore, "create", already_exists)

    replayed = catalog.create_task(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )

    assert replayed == winner
    assert load_calls == 2


def test_two_processes_with_the_same_create_key_converge_on_one_record(tmp_path: Path):
    _leases, _tasks, _revisions, _head = _stores(tmp_path)
    ready = [tmp_path / f"ready-{index}" for index in range(2)]
    go = tmp_path / "go"
    script = """
import json
import os
import sys
import time
from pathlib import Path
from vibecad.execution.revisions import LocalRevisionStore, RevisionStoreRootTrust
from vibecad.workflow.catalog import TaskCatalogService
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust

root = Path(sys.argv[1])
ready = Path(sys.argv[2])
go = Path(sys.argv[3])
leases = ResourceLeaseManager(root / "locks", trust=LeaseRootTrust.TRUSTED_LOCAL)
tasks = TaskRunStore(root / "tasks", leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
revisions = LocalRevisionStore(
    root / "projects", leases, trust=RevisionStoreRootTrust.TRUSTED_LOCAL
)
catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 5
while not go.exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("barrier timeout")
    time.sleep(0.002)
stored = catalog.create_task(
    create_key=sys.argv[4],
    project_id=sys.argv[5],
    reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
    review_policy=ReviewPolicy.AUTO_COMMIT,
)
print(json.dumps({"id": stored.task_run.id, "generation": stored.generation}))
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(tmp_path),
                str(ready[index]),
                str(go),
                CREATE_KEY,
                PROJECT_ID,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    deadline = time.monotonic() + 5
    while not all(path.exists() for path in ready):
        if time.monotonic() >= deadline:
            pytest.fail("subprocess create barrier timed out")
        time.sleep(0.002)
    go.write_text("go", encoding="utf-8")
    completed = [process.communicate(timeout=10) for process in processes]

    assert all(process.returncode == 0 for process in processes), completed
    payloads = [json.loads(stdout) for stdout, _stderr in completed]
    assert payloads == [
        {"id": KEYED_TASK_ID, "generation": 0},
        {"id": KEYED_TASK_ID, "generation": 0},
    ]
    assert len(tuple((tmp_path / "tasks").glob("*.json"))) == 1


def test_catalog_errors_are_closed_and_path_free():
    for code in TaskCatalogErrorCode:
        error = TaskCatalogError(code)
        assert error.code is code
        assert error.to_mapping()["code"] == code.value
        assert "path" not in json.dumps(error.to_mapping())


def test_catalog_preserves_task_store_capacity_for_create_and_cas(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _leases, tasks, revisions, _head = _stores(tmp_path)
    catalog = TaskCatalogService(task_store=tasks, revision_store=revisions)
    stored = catalog.create_task(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )

    def exhausted(*_args, **_kwargs):
        raise TaskStoreError(TaskStoreErrorCode.RESOURCE_EXHAUSTED)

    monkeypatch.setattr(TaskRunStore, "compare_and_set", exhausted)
    with pytest.raises(TaskCatalogError) as caught:
        catalog.compare_and_set(stored, stored.task_run)
    assert caught.value.code is TaskCatalogErrorCode.RESOURCE_EXHAUSTED

    other_root = tmp_path / "second"
    other_root.mkdir(mode=0o700)
    other_leases = ResourceLeaseManager(
        tmp_path / "locks",
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    other_tasks = TaskRunStore(
        other_root,
        other_leases,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    other_catalog = TaskCatalogService(task_store=other_tasks, revision_store=revisions)
    monkeypatch.setattr(TaskRunStore, "create", exhausted)
    with pytest.raises(TaskCatalogError) as caught:
        other_catalog.create_task(
            task_id="task_11111111111111111111111111111111",
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
    assert caught.value.code is TaskCatalogErrorCode.RESOURCE_EXHAUSTED


def test_fresh_catalog_import_and_create_do_not_load_cad_modules():
    script = f"""
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from vibecad.execution.revisions import LocalRevisionStore, RevisionStoreRootTrust
from vibecad.workflow.catalog import TaskCatalogService
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust

with TemporaryDirectory() as value:
    root = Path(value).resolve()
    roots = [root / name for name in ('locks', 'tasks', 'projects')]
    for item in roots:
        item.mkdir(mode=0o700)
        os.chmod(item, 0o700)
    leases = ResourceLeaseManager(roots[0], trust=LeaseRootTrust.TRUSTED_LOCAL)
    tasks = TaskRunStore(roots[1], leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    revisions = LocalRevisionStore(
        roots[2], leases, trust=RevisionStoreRootTrust.TRUSTED_LOCAL
    )
    with leases.acquire_project_write('{PROJECT_ID}') as lease:
        revisions.initialize_empty_project('{PROJECT_ID}', lease)
    TaskCatalogService(task_store=tasks, revision_store=revisions).create_task(
        task_id='{TASK_ID}', project_id='{PROJECT_ID}',
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
forbidden = ('FreeCAD', 'Part', 'vibecad.engine', 'vibecad.tools',
             'vibecad.execution.executor', 'vibecad.execution.candidate')
loaded = sorted(name for name in sys.modules if any(
    name == prefix or name.startswith(prefix + '.') for prefix in forbidden
))
assert loaded == [], json.dumps(loaded)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
