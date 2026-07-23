"""Lazy, store-backed AgentApplication composition tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import vibecad.application.agent as agent_module
import vibecad.application.data as data_module
import vibecad.execution.revisions as revisions_module
from vibecad.application.agent import AgentApplication
from vibecad.application.artifacts import (
    ArtifactDependencyError,
    ArtifactDependencyErrorCode,
    ArtifactStoreError,
    ArtifactStoreErrorCode,
    LocalArtifactAuthority,
)
from vibecad.application.data import (
    ApplicationDataError,
    ApplicationDataErrorCode,
    ApplicationDataLayout,
)
from vibecad.application.project import ProjectRuntime
from vibecad.application.task_api import (
    TaskApi,
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.execution.candidate import (
    CandidateCoordinator,
    SessionBinding,
    SessionSlot,
)
from vibecad.execution.errors import ExecutorError, ExecutorErrorCode
from vibecad.execution.revisions import LocalRevisionStore, ProjectHead
from vibecad.interaction.cad import CadExecutionPort
from vibecad.interaction.checkouts import CheckoutFileSnapshot, HeadCheckoutSource
from vibecad.worker import WorkerGenerationState
from vibecad.workflow.catalog import (
    TaskCatalogError,
    TaskCatalogErrorCode,
    TaskCatalogService,
)
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ResourceLeaseManager,
)
from vibecad.workflow.program import validate_model_program
from vibecad.workflow.service import (
    TaskService,
    TaskServiceError,
    TaskServiceErrorCode,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskStatus,
    transition_task,
)
from vibecad.workflow.store import StoredTaskRun


def _task_id(index: int) -> str:
    return f"task_{index:032x}"


def _task_create_key(index: int) -> str:
    return f"task_create_{index:032x}"


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix().encode()
        value = path.lstat()
        digest.update(relative)
        digest.update(str(value.st_mode).encode())
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class _RuntimeService:
    def __init__(self, task_store, *, activity=None):
        self._task_store = task_store
        self._activity = activity

    def continue_task(self, *, task_id: str, expected_generation: int):
        if self._activity is not None:
            with self._activity["lock"]:
                self._activity["active"] += 1
                self._activity["maximum"] = max(self._activity["maximum"], self._activity["active"])
            time.sleep(0.03)
            with self._activity["lock"]:
                self._activity["active"] -= 1
        stored = self._task_store.load(task_id)
        assert stored.generation == expected_generation
        return stored


class _Runtime:
    def __init__(
        self,
        *,
        head,
        task_store,
        closeable=True,
        activity=None,
        **_ignored,
    ):
        self.head = head
        self.service = _RuntimeService(task_store, activity=activity)
        self.closeable = closeable
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        return self.closeable


class _FailingRuntimeService:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def continue_task(self, **_kwargs):
        raise self.error


class _GapCadPort(CadExecutionPort):
    def __init__(self) -> None:
        self.execute_calls = 0
        self.close_calls = 0

    def validate_program(self, program: ModelProgram):
        return validate_model_program(program)

    def execute_program(self, **_kwargs):
        self.execute_calls += 1
        raise AssertionError("a stale runtime must not execute a CAD handler")

    def close(self, _session: object) -> None:
        self.close_calls += 1


class _GapService:
    def __init__(self, service: TaskService, advance) -> None:
        self._service = service
        self._advance = advance

    @property
    def runtime_head(self):
        return self._service.runtime_head

    @property
    def runtime_stale(self):
        return self._service.runtime_stale

    def submit_model_program(self, **kwargs):
        self._advance()
        return self._service.submit_model_program(**kwargs)


def _model_program(task_id: str, base_revision: str) -> ModelProgram:
    return ModelProgram(
        task_id=task_id,
        base_revision=base_revision,
        operations=(
            ModelCommand(
                id="inspect",
                op="inspect_model",
                target={},
                args={},
                depends_on=(),
                preserve=(),
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=AcceptanceSpec(
            id="acceptance-gap",
            criteria=(
                AcceptanceCriterion(
                    id="volume",
                    kind=AcceptanceKind.GEOMETRY,
                    check="volume",
                    target="body",
                    expected=1.0,
                    tolerance=0.0,
                    parameters={"unit": "mm^3"},
                    required=True,
                ),
            ),
        ),
    )


def _seed_projects_and_tasks(app: AgentApplication, count: int):
    result = []
    for index in range(1, count + 1):
        task_id = _task_id(index)
        project_id = app.bootstrap_empty().head.project_id
        created = app.create_task(
            task_id=task_id,
            project_id=project_id,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
        result.append((project_id, task_id, created))
    return tuple(result)


def _data_root(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, parents=True)
    return home / "data"


def _direct_request(*, task_id: str, base_revision: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "expected_generation": 0,
        "target": {},
        "arguments": {"length_mm": 10, "width_mm": 20, "height_mm": 30},
        "preserve": [],
        "acceptance_json": json.dumps(
            _model_program(task_id, base_revision).acceptance.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }


def test_data_layout_creates_only_fixed_private_store_roots(tmp_path: Path):
    layout = ApplicationDataLayout.open(_data_root(tmp_path))
    assert tuple(
        path.name
        for path in (
            layout.locks,
            layout.tasks,
            layout.projects,
            layout.bootstrap,
            layout.checkouts,
            layout.artifacts,
        )
    ) == ("locks", "tasks", "projects", "bootstrap", "checkouts", "artifacts")
    for path in (
        layout.root,
        layout.locks,
        layout.tasks,
        layout.projects,
        layout.bootstrap,
        layout.checkouts,
        layout.artifacts,
    ):
        value = path.lstat()
        assert stat.S_ISDIR(value.st_mode)
        assert stat.S_IMODE(value.st_mode) == 0o700
        assert value.st_uid == os.geteuid()


def test_data_layout_concurrent_first_open_validates_the_created_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data = _data_root(tmp_path)
    original_mkdir = data_module.os.mkdir
    race = threading.Barrier(2)

    def synchronized_mkdir(path, mode=0o777, *, dir_fd=None):
        if path == data.name:
            race.wait(timeout=3)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(data_module.os, "mkdir", synchronized_mkdir)
    layouts: list[ApplicationDataLayout] = []
    errors: list[BaseException] = []

    def open_layout() -> None:
        try:
            layouts.append(ApplicationDataLayout.open(data))
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=open_layout) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=4)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(layouts) == 2
    assert all(layout.root == data for layout in layouts)


def test_data_layout_rejects_existing_unsafe_or_symlink_roots(tmp_path: Path):
    data = _data_root(tmp_path)
    data.mkdir(mode=0o755)
    with pytest.raises(ApplicationDataError) as unsafe:
        ApplicationDataLayout.open(data)
    assert unsafe.value.code is ApplicationDataErrorCode.UNSAFE_ROOT

    data.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    data.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ApplicationDataError) as linked:
        ApplicationDataLayout.open(data)
    assert linked.value.code is ApplicationDataErrorCode.UNSAFE_ROOT


def test_data_layout_rejects_an_artifact_child_symlink(tmp_path: Path):
    data = _data_root(tmp_path)
    data.mkdir(mode=0o700)
    outside = tmp_path / "outside-artifacts"
    outside.mkdir(mode=0o700)
    (data / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ApplicationDataError) as linked:
        ApplicationDataLayout.open(data)

    assert linked.value.code is ApplicationDataErrorCode.UNSAFE_ROOT
    assert tuple(outside.iterdir()) == ()


def test_data_layout_fails_closed_if_root_is_swapped_before_child_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data = _data_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    original = data_module._create_private
    swapped = False

    def swap_after_root(path: Path) -> None:
        nonlocal swapped
        original(path)
        if path == data and not swapped:
            swapped = True
            data.rename(data.with_name("detached-data"))
            data.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(data_module, "_create_private", swap_after_root)
    with pytest.raises(ApplicationDataError) as caught:
        ApplicationDataLayout.open(data)
    assert caught.value.code is ApplicationDataErrorCode.UNSAFE_ROOT
    assert tuple(outside.iterdir()) == ()


def test_application_open_rejects_a_lock_root_replaced_after_layout_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = _data_root(tmp_path)
    original_open = ApplicationDataLayout.open.__func__
    detached = data_root / "detached-locks"

    def swap_after_layout(cls, root):
        layout = original_open(cls, root)
        layout.locks.rename(detached)
        layout.locks.mkdir(mode=0o700)
        return layout

    monkeypatch.setattr(ApplicationDataLayout, "open", classmethod(swap_after_layout))

    with pytest.raises(TypeError, match="invalid AgentApplication composition"):
        AgentApplication.open(data_root=data_root)

    assert tuple((data_root / "locks").iterdir()) == ()


def test_application_composes_from_one_captured_layout_and_lease_manager(
    tmp_path: Path,
) -> None:
    layout = ApplicationDataLayout.open(_data_root(tmp_path))
    leases = ResourceLeaseManager(
        layout.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )

    def runtime_factory(**_kwargs):
        raise AssertionError("runtime construction must stay lazy")

    def cad_port_factory(**_kwargs):
        raise AssertionError("CAD construction must stay lazy")

    app = AgentApplication.from_captured_layout(
        layout=layout,
        lease_manager=leases,
        runtime_factory=runtime_factory,
        cad_port_factory=cad_port_factory,
    )

    assert app._layout is layout  # noqa: SLF001
    assert app._lease_manager is leases  # noqa: SLF001
    assert app._task_store._lease_manager is leases  # noqa: SLF001
    assert app._revision_store._lease_manager is leases  # noqa: SLF001
    assert app._runtime_factory is runtime_factory  # noqa: SLF001
    assert app._cad_port_factory is cad_port_factory  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()


def test_application_open_delegates_to_the_captured_layout_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = object()
    captured: list[dict[str, object]] = []

    def compose(cls, **kwargs):
        assert cls is AgentApplication
        captured.append(kwargs)
        return marker

    monkeypatch.setattr(
        AgentApplication,
        "from_captured_layout",
        classmethod(compose),
        raising=False,
    )

    result = AgentApplication.open(data_root=_data_root(tmp_path))

    assert result is marker
    assert len(captured) == 1
    layout = captured[0]["layout"]
    leases = captured[0]["lease_manager"]
    assert type(layout) is ApplicationDataLayout
    assert type(leases) is ResourceLeaseManager
    assert leases._root_parts == layout.locks.parts  # noqa: SLF001
    assert leases._root_identity == layout.identity_for(layout.locks)  # noqa: SLF001


def test_captured_layout_composition_runs_recovery_between_full_identity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = ApplicationDataLayout.open(_data_root(tmp_path))
    leases = ResourceLeaseManager(
        layout.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    expected = (
        layout.root,
        layout.locks,
        layout.tasks,
        layout.projects,
        layout.bootstrap,
        layout.checkouts,
        layout.artifacts,
    )
    events: list[object] = []
    original_require_current = ApplicationDataLayout.require_current

    def observed_require_current(self, path):
        events.append(path)
        return original_require_current(self, path)

    def observed_recovery(root):
        events.append("recovery")
        assert root == layout.bootstrap

    monkeypatch.setattr(
        ApplicationDataLayout,
        "require_current",
        observed_require_current,
    )
    monkeypatch.setattr(agent_module, "recover_bootstrap_cleanup", observed_recovery)

    app = AgentApplication.from_captured_layout(
        layout=layout,
        lease_manager=leases,
    )

    recovery_index = events.index("recovery")
    assert tuple(events[:7]) == expected
    assert tuple(events[-7:]) == expected
    assert recovery_index >= 7
    assert recovery_index < len(events) - 7
    app.close()


def test_captured_layout_composition_rejects_a_different_exact_lease_manager(
    tmp_path: Path,
) -> None:
    layout = ApplicationDataLayout.open(_data_root(tmp_path / "first"))
    other = ApplicationDataLayout.open(_data_root(tmp_path / "second"))
    leases = ResourceLeaseManager(
        other.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )

    with pytest.raises(TypeError, match="invalid AgentApplication composition"):
        AgentApplication.from_captured_layout(
            layout=layout,
            lease_manager=leases,
        )

    assert tuple(layout.tasks.iterdir()) == ()
    assert tuple(layout.projects.iterdir()) == ()
    assert tuple(layout.checkouts.iterdir()) == ()


@pytest.mark.parametrize("factory_name", ["runtime_factory", "cad_port_factory"])
def test_captured_layout_composition_rejects_an_invalid_factory_before_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    factory_name: str,
) -> None:
    layout = ApplicationDataLayout.open(_data_root(tmp_path))
    leases = ResourceLeaseManager(
        layout.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    recovery_calls: list[Path] = []
    monkeypatch.setattr(
        agent_module,
        "recover_bootstrap_cleanup",
        lambda root: recovery_calls.append(root),
    )

    with pytest.raises(TypeError, match="invalid AgentApplication composition"):
        AgentApplication.from_captured_layout(
            layout=layout,
            lease_manager=leases,
            **{factory_name: None},
        )

    assert recovery_calls == []
    assert tuple(layout.tasks.iterdir()) == ()
    assert tuple(layout.projects.iterdir()) == ()


@pytest.mark.parametrize(
    "path_name",
    ["root", "locks", "tasks", "projects", "bootstrap", "checkouts", "artifacts"],
)
def test_captured_layout_composition_closes_a_candidate_after_postcheck_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_name: str,
) -> None:
    layout = ApplicationDataLayout.open(_data_root(tmp_path))
    leases = ResourceLeaseManager(
        layout.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    original_init = AgentApplication.__init__
    original_close = AgentApplication.close
    candidates: list[AgentApplication] = []
    close_calls: list[AgentApplication] = []

    def construct_then_swap(self, **kwargs):
        original_init(self, **kwargs)
        candidates.append(self)
        target = getattr(layout, path_name)
        detached = target.with_name(f"detached-{path_name}")
        target.rename(detached)
        target.mkdir(mode=0o700)

    def observed_close(self):
        close_calls.append(self)
        return original_close(self)

    monkeypatch.setattr(AgentApplication, "__init__", construct_then_swap)
    monkeypatch.setattr(AgentApplication, "close", observed_close)

    with pytest.raises(ApplicationDataError) as caught:
        AgentApplication.from_captured_layout(
            layout=layout,
            lease_manager=leases,
        )

    assert caught.value.code is ApplicationDataErrorCode.UNSAFE_ROOT
    assert len(candidates) == 1
    assert close_calls == candidates
    assert candidates[0]._closed is True  # noqa: SLF001


def test_captured_layout_composition_closes_a_candidate_on_factory_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = ApplicationDataLayout.open(_data_root(tmp_path))
    leases = ResourceLeaseManager(
        layout.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )

    def requested_runtime_factory(**_kwargs):
        return None

    original_init = AgentApplication.__init__
    original_close = AgentApplication.close
    candidates: list[AgentApplication] = []
    close_calls: list[AgentApplication] = []

    def construct_then_replace_factory(self, **kwargs):
        original_init(self, **kwargs)
        self._runtime_factory = lambda **_kwargs: None
        candidates.append(self)

    def observed_close(self):
        close_calls.append(self)
        return original_close(self)

    monkeypatch.setattr(AgentApplication, "__init__", construct_then_replace_factory)
    monkeypatch.setattr(AgentApplication, "close", observed_close)

    with pytest.raises(TypeError, match="invalid AgentApplication composition"):
        AgentApplication.from_captured_layout(
            layout=layout,
            lease_manager=leases,
            runtime_factory=requested_runtime_factory,
        )

    assert len(candidates) == 1
    assert close_calls == candidates
    assert candidates[0]._closed is True  # noqa: SLF001


def test_first_project_request_rejects_bootstrap_replacement_without_mutating_it(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    bootstrap = app._layout.bootstrap  # noqa: SLF001
    detached = bootstrap.with_name("detached-bootstrap")
    bootstrap.rename(detached)
    bootstrap.mkdir(mode=0o700)

    with pytest.raises(TypeError, match="invalid durable project service composition"):
        app.create_project_request(
            {
                "schema_version": 1,
                "create_key": "project_create_" + "d" * 32,
                "kind": "empty",
            }
        )

    assert tuple(bootstrap.iterdir()) == ()
    app.close()


def test_first_artifact_request_rejects_artifact_replacement_without_mutating_it(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    artifacts = app._layout.artifacts  # noqa: SLF001
    detached = artifacts.with_name("detached-artifacts")
    artifacts.rename(detached)
    artifacts.mkdir(mode=0o700)

    with pytest.raises(ArtifactStoreError) as caught:
        app.export_task_artifacts_request({"schema_version": 1})

    assert caught.value.code is ArtifactStoreErrorCode.INTEGRITY_FAILURE
    assert tuple(artifacts.iterdir()) == ()
    app.close()


@pytest.mark.parametrize("entrypoint", ["project", "artifact"])
def test_first_stateful_request_rejects_a_replaced_data_root_without_mutating_it(
    tmp_path: Path,
    entrypoint: str,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    detached = data_root.with_name("detached-data")
    data_root.rename(detached)
    data_root.mkdir(mode=0o700)
    (data_root / "bootstrap").mkdir(mode=0o700)
    (data_root / "artifacts").mkdir(mode=0o700)
    before = tuple(sorted(path.name for path in data_root.iterdir()))

    if entrypoint == "project":
        with pytest.raises(TypeError, match="invalid durable project service composition"):
            app.create_project_request(
                {
                    "schema_version": 1,
                    "create_key": "project_create_" + "e" * 32,
                    "kind": "empty",
                }
            )
    else:
        with pytest.raises(ArtifactStoreError) as caught:
            app.export_task_artifacts_request({"schema_version": 1})
        assert caught.value.code is ArtifactStoreErrorCode.INTEGRITY_FAILURE

    assert tuple(sorted(path.name for path in data_root.iterdir())) == before
    app.close()


def test_empty_bootstrap_and_task_control_never_create_a_cad_runtime(tmp_path: Path):
    calls: list[str] = []

    def forbidden_runtime(*_args, **_kwargs):
        calls.append("runtime")
        raise AssertionError("CAD runtime must stay lazy")

    def forbidden_cad(*_args, **_kwargs):
        calls.append("cad")
        raise AssertionError("CAD port must stay lazy")

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=forbidden_runtime,
        cad_port_factory=forbidden_cad,
    )
    project = app.bootstrap_empty()
    assert project.head.project_id.startswith("project_")
    assert project.head.generation == 0
    assert project.cleanup_required is False

    api = TaskApi(port=app)
    created = api.create_task(
        {
            "schema_version": 1,
            "create_key": _task_create_key(1),
            "project_id": project.head.project_id,
            "review_policy": "auto_commit",
        }
    )
    assert created["ok"] is True
    task_id = created["result"]["task_run"]["id"]
    loaded = api.get_task({"schema_version": 1, "task_id": task_id})
    assert loaded == created
    listed = app.list_tasks_request({"schema_version": 1})
    assert listed["ok"] is True
    assert [item["task_id"] for item in listed["result"]["tasks"]] == [task_id]
    events = app.get_task_events_request({"schema_version": 1, "task_id": task_id})
    assert events["ok"] is True
    assert events["result"]["generation"] == 0
    assert [item["sequence"] for item in events["result"]["transitions"]] == [1]
    assert app._project_api is None  # noqa: SLF001
    assert app._project_service is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert calls == []
    app.close()


def test_application_exposes_identity_bound_checkout_file_snapshot(tmp_path: Path) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    try:
        project_id = "project_" + "b" * 32
        source = tmp_path / "source.FCStd"
        source.write_bytes(b"identity-bound checkout")
        source.chmod(0o600)
        with app._lease_manager.acquire_project_write(project_id) as lease:  # noqa: SLF001
            app._revision_store.import_trusted_fcstd(  # noqa: SLF001
                project_id,
                source,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                source.stat().st_size,
                lease,
            )
        opened = app.open_checkout(
            open_key="checkout_open_" + "a" * 32,
            source=HeadCheckoutSource(project_id=project_id),
        )

        snapshot = app.capture_checkout_file(checkout_id=opened.checkout_id)
        current = app.require_same_checkout_file(snapshot)

        assert type(snapshot) is CheckoutFileSnapshot
        assert current == snapshot
        assert current.descriptor == opened
        assert current.path == opened.local_path
    finally:
        app.close()


def test_task_create_request_replays_the_same_task_after_application_restart(tmp_path: Path):
    data_root = _data_root(tmp_path)
    first_app = AgentApplication.open(data_root=data_root)
    project = first_app.bootstrap_empty()
    request = {
        "schema_version": 1,
        "create_key": _task_create_key(5),
        "project_id": project.head.project_id,
        "review_policy": "require_review",
    }
    first = first_app.create_task_request(request)
    first_app.close()

    restarted = AgentApplication.open(data_root=data_root)
    replayed = restarted.create_task_request(request)

    assert first["ok"] is True
    assert replayed == first
    assert replayed["result"]["task_run"]["creation_digest"] is not None
    assert len(tuple(restarted._layout.tasks.glob("*.json"))) == 1  # noqa: SLF001
    restarted.close()


def test_application_preserves_catalog_capacity_as_public_port_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))

    def exhausted(*_args, **_kwargs):
        raise TaskCatalogError(TaskCatalogErrorCode.RESOURCE_EXHAUSTED)

    monkeypatch.setattr(TaskCatalogService, "create_task", exhausted)
    result = app.create_task(
        task_id=_task_id(1),
        project_id="project_0123456789abcdef0123456789abcdef",
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )

    assert result == TaskServicePortFailure(code=TaskServicePortErrorCode.RESOURCE_EXHAUSTED)
    app.close()


def test_fresh_empty_application_path_does_not_import_cad_modules(tmp_path: Path):
    data_root = _data_root(tmp_path)
    script = f"""
import json
import sys
from pathlib import Path
from vibecad.application.agent import AgentApplication
factory_calls = []
def forbidden_cad_factory(**_kwargs):
    factory_calls.append('cad')
    raise AssertionError('CAD factory must stay lazy')
app = AgentApplication.open(
    data_root=Path({str(data_root)!r}),
    cad_port_factory=forbidden_cad_factory,
)
project = app.bootstrap_empty()
projects = app.list_projects_request({{'schema_version': 1}})
assert projects['ok'] is True
assert [item['project_id'] for item in projects['result']['projects']] == [
    project.head.project_id
]
revisions = app.list_revisions_request({{
    'schema_version': 1,
    'project_id': project.head.project_id,
}})
assert revisions['ok'] is True
assert [item['id'] for item in revisions['result']['revisions']] == [
    project.head.revision_id
]
assert app._project_api is not None
assert app._project_service is None
assert app._cad_validation_port is None
assert app._runtimes == {{}}
response = app.create_task_request({{
    'schema_version': 1,
    'create_key': 'task_create_' + '2' * 32,
    'project_id': project.head.project_id,
    'review_policy': 'auto_commit'
}})
assert response['ok'] is True
task_id = response['result']['task_run']['id']
assert app.get_task_request({{'schema_version': 1, 'task_id': task_id}}) == response
rejected = app.reject_draft_request({{
    'schema_version': 1,
    'task_id': task_id,
    'draft_id': 'draft_' + '0' * 32,
    'expected_generation': 0,
}})
assert rejected['ok'] is False
assert rejected['error']['code'] == 'invalid_state'
assert app._artifact_authority is not None
assert app._artifact_store is None
assert app._artifact_service is None
assert app._artifact_api is None
assert app._cad_validation_port is None
assert app.get_capabilities_request({{'schema_version': 1}})['ok'] is True
assert AgentApplication.execution_capabilities()['headless'] == 'verified'
app.close()
assert factory_calls == []
forbidden = ('FreeCAD', 'Part', 'vibecad.engine', 'vibecad.tools',
             'vibecad.workflow.service', 'vibecad.execution.executor',
             'vibecad.application.project_create',
             'vibecad.application.public_surface')
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


def test_default_worker_port_composition_keeps_cad_modules_out_of_parent(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    script = f"""
import json
import sys
from pathlib import Path
from vibecad.application.agent import AgentApplication
app = AgentApplication.open(data_root=Path({str(data_root)!r}))
with app._cad_gate:
    port = app._cad_execution_port_under_gate()
assert type(port).__name__ == 'WorkerCadExecutionPort'
forbidden = (
    'FreeCAD',
    'Part',
    'vibecad.engine',
    'vibecad.tools',
    'vibecad.execution.executor',
    'vibecad.worker.service',
)
loaded = sorted(name for name in sys.modules if any(
    name == prefix or name.startswith(prefix + '.') for prefix in forbidden
))
assert loaded == [], json.dumps(loaded)
app.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_review_gate_release_failure_is_recovery_after_no_cad_reject_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReleaseFailure:
        def __enter__(self) -> None:
            return None

        def __exit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback
            raise ArtifactDependencyError(ArtifactDependencyErrorCode.STORE_FAILURE)

    calls: list[str] = []
    original_reject = TaskCatalogService.reject_draft

    def observed_reject(self, **kwargs):
        calls.append("reject")
        return original_reject(self, **kwargs)

    monkeypatch.setattr(TaskCatalogService, "reject_draft", observed_reject)
    monkeypatch.setattr(
        LocalArtifactAuthority,
        "acquire_export_gate",
        lambda self, *, task_id: ReleaseFailure(),
    )
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]

    result = app.reject_draft(
        task_id=task_id,
        draft_id="draft_" + "0" * 32,
        expected_generation=stored.generation,
    )

    assert project_id == stored.task_run.project_id
    assert calls == ["reject"]
    assert result == TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
    assert app._task_store.load(task_id) == stored  # noqa: SLF001
    assert type(app._artifact_authority) is LocalArtifactAuthority  # noqa: SLF001
    assert app._artifact_store is None  # noqa: SLF001
    assert app._artifact_service is None  # noqa: SLF001
    assert app._artifact_api is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()


def test_light_review_authority_is_reused_by_later_artifact_bundle(tmp_path: Path) -> None:
    cad_factory_calls: list[str] = []
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: cad_factory_calls.append("cad"),
    )
    _project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]

    rejected = app.reject_draft(
        task_id=task_id,
        draft_id="draft_" + "0" * 32,
        expected_generation=stored.generation,
    )

    assert rejected == TaskServicePortFailure(code=TaskServicePortErrorCode.INVALID_STATE)
    authority = app._artifact_authority  # noqa: SLF001
    assert type(authority) is LocalArtifactAuthority
    assert app._artifact_store is None  # noqa: SLF001
    assert app._artifact_service is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert cad_factory_calls == []

    malformed = app.export_task_artifacts_request({"schema_version": 1})

    assert malformed["error"]["code"] == "missing_field"
    assert app._artifact_service._authority is authority  # noqa: SLF001
    assert app._artifact_store is not None  # noqa: SLF001
    assert app._cad_validation_port is not None  # noqa: SLF001
    assert cad_factory_calls == []
    app.close()


@pytest.mark.parametrize(
    ("dependency_code", "port_code"),
    (
        (ArtifactDependencyErrorCode.NOT_FOUND, TaskServicePortErrorCode.NOT_FOUND),
        (ArtifactDependencyErrorCode.INVALID_STATE, TaskServicePortErrorCode.INVALID_STATE),
        (ArtifactDependencyErrorCode.CONFLICT, TaskServicePortErrorCode.CONFLICT),
        (
            ArtifactDependencyErrorCode.LEASE_UNAVAILABLE,
            TaskServicePortErrorCode.LEASE_UNAVAILABLE,
        ),
        (
            ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED,
            TaskServicePortErrorCode.RESOURCE_EXHAUSTED,
        ),
        (
            ArtifactDependencyErrorCode.RECOVERY_REQUIRED,
            TaskServicePortErrorCode.RECOVERY_REQUIRED,
        ),
        (ArtifactDependencyErrorCode.INTEGRITY_FAILURE, TaskServicePortErrorCode.STORE_FAILURE),
        (ArtifactDependencyErrorCode.CAD_FAILURE, TaskServicePortErrorCode.STORE_FAILURE),
        (ArtifactDependencyErrorCode.STORE_FAILURE, TaskServicePortErrorCode.STORE_FAILURE),
        (ArtifactDependencyErrorCode.RUNTIME_UNAVAILABLE, TaskServicePortErrorCode.STORE_FAILURE),
        (ArtifactDependencyErrorCode.INTERNAL_ERROR, TaskServicePortErrorCode.STORE_FAILURE),
    ),
)
def test_review_gate_preentry_failures_use_closed_task_port_taxonomy(
    dependency_code: ArtifactDependencyErrorCode,
    port_code: TaskServicePortErrorCode,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EntryFailure:
        def __enter__(self) -> None:
            raise ArtifactDependencyError(dependency_code)

        def __exit__(self, exc_type, exc, traceback) -> None:
            raise AssertionError("an unentered gate must not be released")

    catalog_calls: list[str] = []
    monkeypatch.setattr(
        TaskCatalogService,
        "reject_draft",
        lambda self, **kwargs: catalog_calls.append("reject"),
    )
    monkeypatch.setattr(
        LocalArtifactAuthority,
        "acquire_export_gate",
        lambda self, *, task_id: EntryFailure(),
    )
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    _project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    before = _tree_digest(app._layout.tasks)  # noqa: SLF001

    result = app.reject_draft(
        task_id=task_id,
        draft_id="draft_" + "0" * 32,
        expected_generation=stored.generation,
    )

    assert result == TaskServicePortFailure(code=port_code)
    assert catalog_calls == []
    assert app._task_store.load(task_id) == stored  # noqa: SLF001
    assert _tree_digest(app._layout.tasks) == before  # noqa: SLF001
    assert app._artifact_store is None  # noqa: SLF001
    assert app._artifact_service is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    app.close()


def test_lazy_application_composes_exact_private_adapters_and_shared_authority(
    tmp_path: Path,
) -> None:
    from vibecad.application.artifacts import (
        ArtifactApi,
        ArtifactMaterializationService,
        ArtifactStore,
        LocalArtifactAuthority,
    )
    from vibecad.application.project_api import ProjectApi
    from vibecad.application.project_create import DurableProjectService
    from vibecad.application.public_surface import DirectOperationApi

    app = AgentApplication.open(data_root=_data_root(tmp_path))
    assert app._project_api is None  # noqa: SLF001
    assert app._artifact_store is None  # noqa: SLF001
    assert app._task_api is None  # noqa: SLF001
    assert app._direct_api is None  # noqa: SLF001

    project = app.create_project_request(
        {
            "schema_version": 1,
            "create_key": "project_create_" + "1" * 32,
            "kind": "empty",
        }
    )
    assert project["ok"] is True
    task = app.create_task_request(
        {
            "schema_version": 1,
            "create_key": _task_create_key(3),
            "project_id": project["result"]["project_id"],
            "review_policy": "auto_commit",
        }
    )
    assert task["ok"] is True
    assert app.invoke_direct_operation_request("unknown_operation", {})["error"]["code"] == (
        "invalid_input"
    )
    artifact = app.export_task_artifacts_request(
        {
            "schema_version": 1,
            "export_key": "export_" + "2" * 32,
            "task_id": _task_id(999),
            "expected_generation": 0,
            "revision_id": "revision_" + "3" * 32,
            "draft_id": None,
        }
    )
    assert artifact["error"]["code"] == "not_found"

    assert type(app._project_service) is DurableProjectService  # noqa: SLF001
    assert type(app._project_api) is ProjectApi  # noqa: SLF001
    assert app._project_api._port is app  # noqa: SLF001
    assert app._project_service._revision_store is app._revision_store  # noqa: SLF001
    assert app._project_service._lease_manager is app._lease_manager  # noqa: SLF001
    assert type(app._task_api) is TaskApi  # noqa: SLF001
    assert app._task_api._port is app  # noqa: SLF001
    assert type(app._direct_api) is DirectOperationApi  # noqa: SLF001
    assert app._direct_api._port is app  # noqa: SLF001
    assert type(app._artifact_store) is ArtifactStore  # noqa: SLF001
    assert app._artifact_store.root == app._layout.artifacts  # noqa: SLF001
    assert type(app._artifact_authority) is LocalArtifactAuthority  # noqa: SLF001
    assert app._artifact_authority._task_store is app._task_store  # noqa: SLF001
    assert app._artifact_authority._revision_store is app._revision_store  # noqa: SLF001
    assert app._artifact_authority._lease_manager is app._lease_manager  # noqa: SLF001
    assert type(app._artifact_service) is ArtifactMaterializationService  # noqa: SLF001
    assert app._artifact_service._store is app._artifact_store  # noqa: SLF001
    assert app._artifact_service._authority is app._artifact_authority  # noqa: SLF001
    assert app._artifact_service._cad is app._cad_validation_port  # noqa: SLF001
    assert type(app._artifact_api) is ArtifactApi  # noqa: SLF001
    assert app._artifact_api._port is app  # noqa: SLF001
    app.close()


def test_project_request_facade_replays_create_key_and_gets_current_snapshot(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    request = {
        "schema_version": 1,
        "create_key": "project_create_" + "4" * 32,
        "kind": "empty",
    }

    first = app.create_project_request(request)
    second = app.create_project_request(request)

    assert first["ok"] is True
    assert second == first
    loaded = app.get_project_request(
        {
            "schema_version": 1,
            "project_id": first["result"]["project_id"],
        }
    )
    assert loaded["ok"] is True
    assert loaded["result"]["current"] == first["result"]["generation_zero"]
    app.close()


def test_cancel_task_request_is_store_only_and_keeps_heavy_components_lazy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_calls: list[str] = []
    cad_calls: list[str] = []

    def forbidden_runtime(**_kwargs):
        runtime_calls.append("runtime")
        raise AssertionError("idle cancellation must not create a CAD runtime")

    def forbidden_cad(**_kwargs):
        cad_calls.append("cad")
        raise AssertionError("idle cancellation must not create a CAD port")

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=forbidden_runtime,
        cad_port_factory=forbidden_cad,
    )
    project = app.bootstrap_empty()
    created = app.create_task_request(
        {
            "schema_version": 1,
            "create_key": "task_create_" + "7" * 32,
            "project_id": project.head.project_id,
            "review_policy": "auto_commit",
        }
    )
    assert created["ok"] is True
    task_id = created["result"]["task_run"]["id"]
    before_projects = _tree_digest(app._layout.projects)  # noqa: SLF001

    def forbidden_project_write(*_args, **_kwargs):
        raise AssertionError("idle cancellation must not acquire a project write lease")

    monkeypatch.setattr(
        ResourceLeaseManager,
        "acquire_project_write",
        forbidden_project_write,
    )
    cancelled = app.cancel_task_request(
        {
            "schema_version": 1,
            "task_id": task_id,
            "expected_generation": created["result"]["generation"],
        }
    )

    assert cancelled["ok"] is True
    assert cancelled["result"]["generation"] == created["result"]["generation"] + 1
    assert cancelled["result"]["next_action"] == "none"
    assert cancelled["result"]["task_run"]["status"] == "cancelled"
    assert _tree_digest(app._layout.projects) == before_projects  # noqa: SLF001
    assert app._task_api is not None  # noqa: SLF001
    assert app._project_service is None  # noqa: SLF001
    assert app._direct_api is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert app._artifact_store is None  # noqa: SLF001
    assert app._artifact_authority is None  # noqa: SLF001
    assert app._artifact_service is None  # noqa: SLF001
    assert app._artifact_api is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    assert runtime_calls == []
    assert cad_calls == []
    app.close()


@pytest.mark.parametrize(
    ("generation_kind", "expected_code"),
    (
        ("stale", "conflict"),
        ("future", "conflict"),
        ("exact", "invalid_state"),
    ),
)
def test_cancelled_task_submit_preserves_expected_generation_contract(
    tmp_path: Path,
    generation_kind: str,
    expected_code: str,
) -> None:
    cad_calls: list[str] = []
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: cad_calls.append("cad"),
    )
    project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    cancelled = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )
    assert cancelled.task_run.status is TaskStatus.CANCELLED
    generation = {
        "stale": created.generation,
        "future": cancelled.generation + 100,
        "exact": cancelled.generation,
    }[generation_kind]
    request = {
        "schema_version": 1,
        "task_id": task_id,
        "expected_generation": generation,
        "program_json": json.dumps(
            _model_program(task_id, created.task_run.base_revision).to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    }

    result = app.submit_model_program_request(request)

    assert result["ok"] is False
    assert result["error"]["code"] == expected_code
    assert app._task_store.load(task_id) == cancelled  # noqa: SLF001
    assert app._cad_task_admissions == {}  # noqa: SLF001
    assert cad_calls == []
    assert project_id == cancelled.task_run.project_id
    app.close()


def test_idle_cancel_does_not_terminate_an_unrelated_cad_generation(
    tmp_path: Path,
) -> None:
    class Generation(CadExecutionPort):
        def __init__(self) -> None:
            self.terminate_calls = 0

        def terminate_generation(self) -> None:
            self.terminate_calls += 1

        def close_generation(self) -> None:
            return None

    port = Generation()
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: port,
    )
    _project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    app._cad_execution_port = port  # noqa: SLF001

    cancelled = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )

    assert cancelled.task_run.status is TaskStatus.CANCELLED
    assert port.terminate_calls == 0
    assert app._cad_execution_port is port  # noqa: SLF001
    app.close()


def test_cancel_task_request_replays_original_generation_after_response_loss(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    _project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    request = {
        "schema_version": 1,
        "task_id": task_id,
        "expected_generation": created.generation,
    }

    first = app.cancel_task_request(request)
    replayed = app.cancel_task_request(request)
    future = app.cancel_task_request(
        {
            **request,
            "expected_generation": first["result"]["generation"] + 100,
        }
    )

    assert first["ok"] is True
    assert replayed == first
    assert future["ok"] is False
    assert future["error"]["code"] == "conflict"
    assert app._cad_task_admissions == {}  # noqa: SLF001
    app.close()


def test_active_cancel_fences_generation_before_store_only_reconcile_without_cad_gate(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=lambda **kwargs: _Runtime(**kwargs),
        cad_port_factory=lambda **_kwargs: None,
    )
    project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    head = app._revision_store.load_head(project_id)  # noqa: SLF001
    lease = app._lease_manager.acquire_project_write(project_id)  # noqa: SLF001
    try:
        candidate_revision = app._revision_store.begin_revision(  # noqa: SLF001
            project_id,
            head,
            lease,
        )
    finally:
        lease.release(owner_token=lease.owner_token)
    submitted = transition_task(
        created.task_run,
        TaskEvent.SUBMIT_PROGRAM,
        program=_model_program(task_id, created.task_run.base_revision),
    )
    stored = app._task_store.compare_and_set(task_id, created.generation, submitted)  # noqa: SLF001
    validating = transition_task(stored.task_run, TaskEvent.START_VALIDATION)
    stored = app._task_store.compare_and_set(task_id, stored.generation, validating)  # noqa: SLF001
    executing = transition_task(
        stored.task_run,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=candidate_revision,
    )
    stored = app._task_store.compare_and_set(task_id, stored.generation, executing)  # noqa: SLF001
    assert stored.task_run.status is TaskStatus.EXECUTING

    events: list[str] = []
    terminated = threading.Event()

    class Generation:
        def terminate_generation(self) -> None:
            durable = app._task_store.load(task_id)  # noqa: SLF001
            assert durable.task_run.status is TaskStatus.CANCEL_REQUESTED
            events.append("cancel_requested_then_terminate")
            terminated.set()

    class ForbiddenGate:
        def __enter__(self):
            raise AssertionError("active cancellation must not wait for the CAD gate")

        def __exit__(self, *_args):
            return False

    app._cad_execution_port = Generation()  # noqa: SLF001
    app._cad_task_admissions[task_id] = 1  # noqa: SLF001
    app._runtimes[project_id] = object()  # noqa: SLF001
    app._runtimes["project_" + "8" * 32] = object()  # noqa: SLF001
    app._cad_gate = ForbiddenGate()  # noqa: SLF001

    def drain_admission() -> None:
        assert terminated.wait(timeout=3)
        with app._cad_admission_condition:  # noqa: SLF001
            app._cad_task_admissions.pop(task_id, None)  # noqa: SLF001
            app._cad_admission_condition.notify_all()  # noqa: SLF001

    drain = threading.Thread(target=drain_admission)
    drain.start()

    cancelled = app.cancel_task(
        task_id=task_id,
        expected_generation=stored.generation,
    )
    drain.join(timeout=3)

    assert cancelled.task_run.status is TaskStatus.CANCELLED
    assert not drain.is_alive()
    assert events == ["cancel_requested_then_terminate"]
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    cancellation_events = [
        record.event
        for record in cancelled.task_run.transitions
        if record.event
        in {
            TaskEvent.REQUEST_CANCEL,
            TaskEvent.START_CANCELLATION,
            TaskEvent.CONFIRM_CANCELLED,
        }
    ]
    assert cancellation_events == [
        TaskEvent.REQUEST_CANCEL,
        TaskEvent.START_CANCELLATION,
        TaskEvent.CONFIRM_CANCELLED,
    ]
    app._cad_task_admissions.clear()  # noqa: SLF001
    app._cad_gate = threading.Lock()  # noqa: SLF001
    app.close()


def test_cancel_keeps_the_owner_admitted_until_durable_request_is_fenced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service_entered = threading.Event()
    cancellation_persisted = threading.Event()
    owner_returning = threading.Event()
    results: list[object] = []
    events: list[str] = []

    class GenerationCad(CadExecutionPort):
        generation_lost = False

        def terminate_generation(self) -> None:
            durable = app._task_store.load(task_id)  # noqa: SLF001
            assert durable.task_run.status is TaskStatus.CANCEL_REQUESTED
            self.generation_lost = True
            events.append("terminate")

    port = GenerationCad()

    class Service:
        def __init__(self, store) -> None:
            self._store = store

        def continue_task(self, *, task_id: str, expected_generation: int):
            del expected_generation
            service_entered.set()
            assert cancellation_persisted.wait(timeout=3)
            durable = self._store.load(task_id)
            assert durable.task_run.status is TaskStatus.CANCEL_REQUESTED
            events.append("owner-return")
            owner_returning.set()
            return durable

    class Runtime(_Runtime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.service = Service(kwargs["task_store"])

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=lambda **kwargs: Runtime(**kwargs),
        cad_port_factory=lambda **_kwargs: port,
    )
    _project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    original_cancel = TaskCatalogService.cancel_task

    def observed_cancel(self, **kwargs):
        result = original_cancel(self, **kwargs)
        if kwargs["task_id"] == task_id:
            events.append("persist")
            cancellation_persisted.set()
            assert owner_returning.wait(timeout=3)
        return result

    monkeypatch.setattr(TaskCatalogService, "cancel_task", observed_cancel)
    caller = threading.Thread(
        target=lambda: results.append(
            app.continue_task(
                task_id=task_id,
                expected_generation=created.generation,
            )
        )
    )
    caller.start()
    assert service_entered.wait(timeout=2)

    cancelled = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )
    caller.join(timeout=3)

    assert cancelled.task_run.status is TaskStatus.CANCELLED
    assert not caller.is_alive()
    assert len(results) == 1
    assert type(results[0]) is StoredTaskRun
    assert results[0].task_run.status is TaskStatus.CANCEL_REQUESTED
    assert results[0].task_run.transitions[-1].event is TaskEvent.REQUEST_ACTIVE_CANCEL
    assert events == ["persist", "owner-return", "terminate"]
    assert port.generation_lost is True
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._cad_task_admissions == {}  # noqa: SLF001
    app.close()


def test_failed_generation_fence_stays_unstarted_across_store_only_reconcile_and_restart(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    app = AgentApplication.open(data_root=data_root)
    project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    head = app._revision_store.load_head(project_id)  # noqa: SLF001
    with app._lease_manager.acquire_project_write(project_id) as lease:  # noqa: SLF001
        candidate_revision = app._revision_store.begin_revision(  # noqa: SLF001
            project_id,
            head,
            lease,
        )
    submitted = transition_task(
        created.task_run,
        TaskEvent.SUBMIT_PROGRAM,
        program=_model_program(task_id, created.task_run.base_revision),
    )
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        task_id,
        created.generation,
        submitted,
    )
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        task_id,
        stored.generation,
        transition_task(stored.task_run, TaskEvent.START_VALIDATION),
    )
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        task_id,
        stored.generation,
        transition_task(
            stored.task_run,
            TaskEvent.VALIDATE_PROGRAM,
            candidate_revision=candidate_revision,
        ),
    )

    class UncertainGeneration(CadExecutionPort):
        generation_lost = True

        def __init__(self) -> None:
            self.terminate_calls = 0

        def terminate_generation(self) -> None:
            self.terminate_calls += 1
            raise RuntimeError("termination is uncertain")

    port = UncertainGeneration()
    app._cad_execution_port = port  # noqa: SLF001
    app._cad_task_admissions[task_id] = 1  # noqa: SLF001

    failed = app.cancel_task(
        task_id=task_id,
        expected_generation=stored.generation,
    )
    requested = app._task_store.load(task_id)  # noqa: SLF001
    reconciled = app.reconcile_task(
        task_id=task_id,
        expected_generation=requested.generation,
    )

    assert failed == TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
    assert requested.task_run.status is TaskStatus.CANCEL_REQUESTED
    assert requested.task_run.transitions[-1].event is TaskEvent.REQUEST_CANCEL
    assert all(
        record.event is not TaskEvent.START_CANCELLATION
        for record in requested.task_run.transitions
    )
    assert reconciled == requested
    assert port.terminate_calls == 1
    assert app._cad_execution_port is port  # noqa: SLF001
    assert app._cad_fence_required is True  # noqa: SLF001
    app._cad_task_admissions.clear()  # noqa: SLF001
    app.close()

    restarted = AgentApplication.open(data_root=data_root)
    replayed = restarted.reconcile_task(
        task_id=task_id,
        expected_generation=requested.generation,
    )
    assert replayed == requested
    assert restarted._cad_execution_port is None  # noqa: SLF001
    restarted.close()


def test_failed_generation_fence_retries_the_same_handle_before_starting_cancellation(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    _project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]

    class RetryableGeneration(CadExecutionPort):
        generation_lost = True

        def __init__(self) -> None:
            self.terminate_calls = 0

        def terminate_generation(self) -> None:
            self.terminate_calls += 1
            if self.terminate_calls == 1:
                raise RuntimeError("first termination is uncertain")

    port = RetryableGeneration()
    app._cad_execution_port = port  # noqa: SLF001
    app._cad_task_admissions[task_id] = 1  # noqa: SLF001

    first = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )
    requested = app._task_store.load(task_id)  # noqa: SLF001
    with app._cad_admission_condition:  # noqa: SLF001
        app._cad_task_admissions.clear()  # noqa: SLF001
        app._cad_admission_condition.notify_all()  # noqa: SLF001
    replayed = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )

    assert first == TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
    assert requested.task_run.status is TaskStatus.CANCEL_REQUESTED
    assert replayed.task_run.status is TaskStatus.CANCELLED
    assert port.terminate_calls == 2
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._cad_fence_required is False  # noqa: SLF001
    app.close()


def test_self_lost_generation_is_retained_until_termination_is_proven(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))

    class SelfLostGeneration(CadExecutionPort):
        generation_lost = True

        def __init__(self) -> None:
            self.terminate_calls = 0

        def terminate_generation(self) -> None:
            self.terminate_calls += 1
            if self.terminate_calls == 1:
                raise RuntimeError("cleanup is still required")

    port = SelfLostGeneration()
    app._cad_execution_port = port  # noqa: SLF001

    app._retire_lost_generation(port)  # noqa: SLF001

    assert port.terminate_calls == 1
    assert app._cad_execution_port is port  # noqa: SLF001
    assert app._cad_fence_required is True  # noqa: SLF001
    assert app._fence_cad_generation() is True  # noqa: SLF001
    assert port.terminate_calls == 2
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._cad_fence_required is False  # noqa: SLF001
    app.close()


def test_concurrent_active_cancel_callers_converge_on_one_terminal_result(
    tmp_path: Path,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    _project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    submitted = transition_task(
        created.task_run,
        TaskEvent.SUBMIT_PROGRAM,
        program=_model_program(task_id, created.task_run.base_revision),
    )
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        task_id,
        created.generation,
        submitted,
    )
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        task_id,
        stored.generation,
        transition_task(stored.task_run, TaskEvent.START_VALIDATION),
    )
    terminated = threading.Event()

    class Generation(CadExecutionPort):
        generation_lost = True

        def __init__(self) -> None:
            self.terminate_calls = 0

        def terminate_generation(self) -> None:
            self.terminate_calls += 1
            terminated.set()

    port = Generation()
    app._cad_execution_port = port  # noqa: SLF001
    app._cad_task_admissions[task_id] = 1  # noqa: SLF001

    def drain_admission() -> None:
        assert terminated.wait(timeout=3)
        with app._cad_admission_condition:  # noqa: SLF001
            app._cad_task_admissions.clear()  # noqa: SLF001
            app._cad_admission_condition.notify_all()  # noqa: SLF001

    barrier = threading.Barrier(17)
    results: list[object] = []

    def cancel() -> None:
        barrier.wait()
        results.append(
            app.cancel_task(
                task_id=task_id,
                expected_generation=stored.generation,
            )
        )

    drainer = threading.Thread(target=drain_admission)
    callers = [threading.Thread(target=cancel) for _index in range(16)]
    drainer.start()
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(timeout=5)
    drainer.join(timeout=5)

    assert all(not caller.is_alive() for caller in callers)
    assert not drainer.is_alive()
    assert len(results) == 16
    assert all(type(result) is StoredTaskRun for result in results), results
    durable = app._task_store.load(task_id)  # noqa: SLF001
    assert durable.task_run.status is TaskStatus.CANCELLED
    assert all(result == durable for result in results)
    assert port.terminate_calls == 1
    app.close()


def test_exact_orphan_cancel_replays_original_generation_after_lease_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    head = app._revision_store.load_head(project_id)  # noqa: SLF001
    revision_id = "revision_" + "e" * 32
    with app._lease_manager.acquire_project_write(project_id):  # noqa: SLF001
        reserved = revisions_module._reserve_quota(
            app._revision_store,  # noqa: SLF001
            "candidate",
            project_id,
            head,
            revision_id,
            task_id,
            None,
            8,
        )
    assert reserved[2] is None
    original_acquire = ResourceLeaseManager.acquire_project_write
    acquire_calls = 0

    def contend_once(self, requested_project_id):
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls == 1:
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        return original_acquire(self, requested_project_id)

    monkeypatch.setattr(
        ResourceLeaseManager,
        "acquire_project_write",
        contend_once,
    )

    first = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )
    replayed = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )

    assert first.task_run.status is TaskStatus.CANCEL_REQUESTED
    assert first.task_run.transitions[-1].event is TaskEvent.REQUEST_ACTIVE_CANCEL
    assert replayed.task_run.status is TaskStatus.CANCELLED
    assert [
        record.event
        for record in replayed.task_run.transitions
        if record.event
        in {
            TaskEvent.REQUEST_ACTIVE_CANCEL,
            TaskEvent.START_CANCELLATION,
            TaskEvent.CONFIRM_CANCELLED,
        }
    ] == [
        TaskEvent.REQUEST_ACTIVE_CANCEL,
        TaskEvent.START_CANCELLATION,
        TaskEvent.CONFIRM_CANCELLED,
    ]
    assert tuple(app._layout.projects.rglob("reservation.json")) == ()  # noqa: SLF001
    assert app._revision_store.load_head(project_id) == head  # noqa: SLF001
    app.close()


@pytest.mark.slow
def test_real_managed_generation_is_terminated_by_active_cancellation(
    tmp_path: Path,
) -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    python = Path(python_raw)
    if not python.is_file():
        pytest.skip("managed FreeCAD Python is unavailable")

    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime.status import capture_runtime_generation_evidence

    evidence = capture_runtime_generation_evidence(runtime_paths.active_runtime_prefix())
    assert python.resolve() == evidence.python.resolve()

    app = AgentApplication.open(data_root=_data_root(tmp_path))
    project = app.bootstrap_empty()
    with app._cad_gate:  # noqa: SLF001
        runtime = app._runtime_for(project.head.project_id)  # noqa: SLF001
    assert type(runtime) is not TaskServicePortFailure
    port = app._cad_execution_port  # noqa: SLF001
    worker = port._worker  # noqa: SLF001
    assert worker.state is WorkerGenerationState.READY

    created = app.create_task(
        task_id=_task_id(1),
        project_id=project.head.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    lease = app._lease_manager.acquire_project_write(project.head.project_id)  # noqa: SLF001
    try:
        candidate_revision = app._revision_store.begin_revision(  # noqa: SLF001
            project.head.project_id,
            project.head,
            lease,
        )
    finally:
        lease.release(owner_token=lease.owner_token)
    submitted = transition_task(
        created.task_run,
        TaskEvent.SUBMIT_PROGRAM,
        program=_model_program(created.task_run.id, created.task_run.base_revision),
    )
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        created.task_run.id,
        created.generation,
        submitted,
    )
    validating = transition_task(stored.task_run, TaskEvent.START_VALIDATION)
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        stored.task_run.id,
        stored.generation,
        validating,
    )
    executing = transition_task(
        stored.task_run,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=candidate_revision,
    )
    stored = app._task_store.compare_and_set(  # noqa: SLF001
        stored.task_run.id,
        stored.generation,
        executing,
    )
    admitted = threading.Event()
    release_owner = threading.Event()

    def hold_real_admission() -> None:
        with app._cad_task_admission(stored.task_run.id):  # noqa: SLF001
            admitted.set()
            assert release_owner.wait(timeout=5)

    owner = threading.Thread(target=hold_real_admission)
    owner.start()
    assert admitted.wait(timeout=2)

    def release_after_worker_death() -> None:
        deadline = time.monotonic() + 5
        while worker.state is not WorkerGenerationState.DEAD:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        release_owner.set()

    releaser = threading.Thread(target=release_after_worker_death)
    releaser.start()

    cancelled = app.cancel_task(
        task_id=stored.task_run.id,
        expected_generation=stored.generation,
    )
    owner.join(timeout=2)
    releaser.join(timeout=2)

    assert cancelled.task_run.status is TaskStatus.CANCELLED
    assert not owner.is_alive()
    assert not releaser.is_alive()
    assert worker.state is WorkerGenerationState.DEAD
    assert port.generation_lost is True
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    assert app._cad_task_admissions == {}  # noqa: SLF001
    app.close()


def test_restart_reconciles_started_cancellation_without_starting_cad(
    tmp_path: Path,
) -> None:
    data_root = _data_root(tmp_path)
    first = AgentApplication.open(data_root=data_root)
    project_id, task_id, created = _seed_projects_and_tasks(first, 1)[0]
    head = first._revision_store.load_head(project_id)  # noqa: SLF001
    lease = first._lease_manager.acquire_project_write(project_id)  # noqa: SLF001
    try:
        candidate_revision = first._revision_store.begin_revision(  # noqa: SLF001
            project_id,
            head,
            lease,
        )
    finally:
        lease.release(owner_token=lease.owner_token)
    submitted = transition_task(
        created.task_run,
        TaskEvent.SUBMIT_PROGRAM,
        program=_model_program(task_id, created.task_run.base_revision),
    )
    stored = first._task_store.compare_and_set(task_id, created.generation, submitted)  # noqa: SLF001
    validating = transition_task(stored.task_run, TaskEvent.START_VALIDATION)
    stored = first._task_store.compare_and_set(task_id, stored.generation, validating)  # noqa: SLF001
    executing = transition_task(
        stored.task_run,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=candidate_revision,
    )
    stored = first._task_store.compare_and_set(task_id, stored.generation, executing)  # noqa: SLF001
    requested = first._catalog.cancel_task(  # noqa: SLF001
        task_id=task_id,
        expected_generation=stored.generation,
    )
    started = first._catalog.start_cancellation(  # noqa: SLF001
        task_id=task_id,
        expected_generation=requested.generation,
    )
    first.close()

    calls: list[str] = []

    def forbidden_factory(**_kwargs):
        calls.append("cad")
        raise AssertionError("cancellation recovery must remain store-only")

    restarted = AgentApplication.open(
        data_root=data_root,
        runtime_factory=forbidden_factory,
        cad_port_factory=forbidden_factory,
    )
    cancelled = restarted.reconcile_task(
        task_id=task_id,
        expected_generation=started.generation,
    )

    assert cancelled.task_run.project_id == project_id
    assert cancelled.task_run.status is TaskStatus.CANCELLED
    assert calls == []
    assert restarted._cad_execution_port is None  # noqa: SLF001
    assert restarted._runtimes == {}  # noqa: SLF001
    restarted.close()


def test_project_discovery_is_lazy_and_reuses_the_api_for_later_mutation(
    tmp_path: Path,
) -> None:
    runtime_calls: list[str] = []
    cad_calls: list[str] = []

    def forbidden_runtime(**_kwargs):
        runtime_calls.append("runtime")
        raise AssertionError("project discovery must not create a CAD runtime")

    def forbidden_cad(**_kwargs):
        cad_calls.append("cad")
        raise AssertionError("project discovery must not create a CAD port")

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=forbidden_runtime,
        cad_port_factory=forbidden_cad,
    )
    seeded = app.bootstrap_empty()
    project_id = seeded.head.project_id
    assert app._project_api is None  # noqa: SLF001

    projects = app.list_projects_request({"schema_version": 1})

    assert projects["ok"] is True
    assert projects["result"] == {
        "schema_version": 1,
        "projects": [
            {
                "schema_version": 1,
                "project_id": project_id,
                "generation": 0,
                "revision_id": seeded.head.revision_id,
                "manifest_sha256": seeded.head.manifest_sha256,
            }
        ],
        "next_cursor": None,
    }
    api = app._project_api  # noqa: SLF001
    assert api is not None
    revisions = app.list_revisions_request(
        {
            "schema_version": 1,
            "project_id": project_id,
        }
    )
    assert revisions["ok"] is True
    assert revisions["result"]["project_id"] == project_id
    assert revisions["result"]["head"] == projects["result"]["projects"][0]
    assert revisions["result"]["revisions"] == [
        {
            "schema_version": 1,
            "id": seeded.head.revision_id,
            "project_id": project_id,
            "base_revision": None,
            "manifest_sha256": seeded.head.manifest_sha256,
        }
    ]
    assert app._project_api is api  # noqa: SLF001
    assert app._project_service is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    assert runtime_calls == []
    assert cad_calls == []

    created = app.create_project_request(
        {
            "schema_version": 1,
            "create_key": "project_create_" + "8" * 32,
            "kind": "empty",
        }
    )
    assert created["ok"] is True
    assert app._project_api is api  # noqa: SLF001
    assert (
        app.get_project_request(
            {
                "schema_version": 1,
                "project_id": created["result"]["project_id"],
            }
        )["ok"]
        is True
    )
    assert app._project_service is not None  # noqa: SLF001
    assert app._cad_validation_port is not None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    assert runtime_calls == []
    assert cad_calls == []
    app.close()


@pytest.mark.parametrize(
    "name",
    (
        "INVALID_INPUT",
        "NOT_FOUND",
        "CONFLICT",
        "RESOURCE_EXHAUSTED",
        "INTEGRITY_FAILURE",
        "STORE_FAILURE",
        "RECOVERY_REQUIRED",
    ),
)
def test_application_bridges_revision_discovery_failures(
    name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibecad.application.project_api import (
        ProjectServicePortErrorCode,
        ProjectServicePortFailure,
    )
    from vibecad.application.revision_discovery import (
        RevisionDiscoveryError,
        RevisionDiscoveryErrorCode,
        RevisionDiscoveryService,
    )

    code = RevisionDiscoveryErrorCode[name]

    def fail(*_args, **_kwargs):
        raise RevisionDiscoveryError(code)

    monkeypatch.setattr(RevisionDiscoveryService, "list_projects", fail)
    monkeypatch.setattr(RevisionDiscoveryService, "list_revisions", fail)
    app = AgentApplication.open(data_root=_data_root(tmp_path))

    assert app.list_projects(limit=50, cursor=None) == ProjectServicePortFailure(
        code=ProjectServicePortErrorCode(code.value)
    )
    assert app.list_revisions(
        project_id="project_" + "1" * 32,
        limit=50,
        cursor=None,
    ) == ProjectServicePortFailure(code=ProjectServicePortErrorCode(code.value))
    assert app._project_service is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()


def test_retained_private_adapters_cannot_bypass_application_close(tmp_path: Path) -> None:
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    project = app.create_project_request(
        {
            "schema_version": 1,
            "create_key": "project_create_" + "6" * 32,
            "kind": "empty",
        }
    )
    project_id = project["result"]["project_id"]
    revision_id = project["result"]["generation_zero"]["revision"]["id"]
    task = app.create_task_request(
        {
            "schema_version": 1,
            "create_key": _task_create_key(4),
            "project_id": project_id,
            "review_policy": "auto_commit",
        }
    )
    task_id = task["result"]["task_run"]["id"]
    app.invoke_direct_operation_request("unknown_operation", {})
    app.export_task_artifacts_request({"schema_version": 1})
    task_api = app._task_api  # noqa: SLF001
    project_api = app._project_api  # noqa: SLF001
    direct_api = app._direct_api  # noqa: SLF001
    artifact_api = app._artifact_api  # noqa: SLF001
    app.close()
    before = _tree_digest(app._layout.root)  # noqa: SLF001

    assert (
        task_api.get_task({"schema_version": 1, "task_id": task_id})["error"]["code"]
        == "internal_error"
    )
    assert (
        project_api.get_project({"schema_version": 1, "project_id": project_id})["error"]["code"]
        == "internal_error"
    )
    assert project_api.list_projects({"schema_version": 1})["error"]["code"] == "internal_error"
    assert (
        direct_api.invoke(
            "create_box",
            _direct_request(task_id=task_id, base_revision=revision_id),
        )["error"]["code"]
        == "internal_error"
    )
    assert (
        artifact_api.export_task_artifacts(
            {
                "schema_version": 1,
                "export_key": "export_" + "7" * 32,
                "task_id": task_id,
                "expected_generation": 0,
                "revision_id": revision_id,
                "draft_id": None,
            }
        )["error"]["code"]
        == "internal_error"
    )
    with pytest.raises(RuntimeError):
        app.get_task_request({"schema_version": 1, "task_id": task_id})
    with pytest.raises(RuntimeError):
        app.read_artifact_resource("vibecad://artifact/closed")
    assert _tree_digest(app._layout.root) == before  # noqa: SLF001


def test_artifact_bundle_partial_construction_closes_its_only_store_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.application.artifacts as artifacts_module

    closes: list[object] = []
    original_close = artifacts_module.ArtifactStore.close

    def observed_close(store) -> None:
        closes.append(store)
        original_close(store)

    class FailingService:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("post-store composition failure")

    monkeypatch.setattr(artifacts_module.ArtifactStore, "close", observed_close)
    monkeypatch.setattr(artifacts_module, "ArtifactMaterializationService", FailingService)
    app = AgentApplication.open(data_root=_data_root(tmp_path))

    with pytest.raises(RuntimeError, match="post-store composition failure"):
        app.export_task_artifacts_request({"schema_version": 1})

    assert len(closes) == 1
    store = closes[0]
    assert store._root_fd == -1  # noqa: SLF001
    assert store._requests_fd == -1  # noqa: SLF001
    assert store._materializations_fd == -1  # noqa: SLF001
    assert app._artifact_store is None  # noqa: SLF001
    app.close()
    assert len(closes) == 1


def test_application_capabilities_are_static_and_honest():
    capabilities = AgentApplication.execution_capabilities()
    assert capabilities == {
        "headless": "verified",
        "offscreen_gui": "planned_unavailable",
        "interactive_gui": "planned_unavailable",
        "daemon": False,
        "authenticated_transport": False,
        "ipc_server": False,
    }


def test_non_macos_application_is_unsupported_before_data_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "must-not-exist" / "data"
    monkeypatch.setattr(agent_module.sys, "platform", "linux")
    assert AgentApplication.execution_capabilities()["headless"] == ("unsupported_platform")
    with pytest.raises(ApplicationDataError) as caught:
        AgentApplication.open(data_root=root)
    assert caught.value.code is ApplicationDataErrorCode.UNSUPPORTED_PLATFORM
    assert not root.parent.exists()


def test_cad_calls_are_serialized_across_isolated_project_runtimes(tmp_path: Path):
    activity = {"lock": threading.Lock(), "active": 0, "maximum": 0}
    runtimes = {}

    def factory(**kwargs):
        runtime = _Runtime(activity=activity, **kwargs)
        runtimes[kwargs["project_id"]] = runtime
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    seeded = _seed_projects_and_tasks(app, 2)
    results = []

    def invoke(item):
        _, task_id, stored = item
        results.append(app.continue_task(task_id=task_id, expected_generation=stored.generation))

    threads = [threading.Thread(target=invoke, args=(item,)) for item in seeded]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert activity["maximum"] == 1
    assert len(runtimes) == 2
    assert runtimes[seeded[0][0]] is not runtimes[seeded[1][0]]
    app.close()


def test_lru_evicts_only_a_closeable_runtime(tmp_path: Path):
    created = []

    def factory(**kwargs):
        runtime = _Runtime(**kwargs)
        created.append(runtime)
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    seeded = _seed_projects_and_tasks(app, 5)
    for _, task_id, stored in seeded:
        assert app.continue_task(task_id=task_id, expected_generation=stored.generation) == stored

    assert len(created) == 5
    assert created[0].close_calls == 1
    assert all(item.close_calls == 0 for item in created[1:])
    app.close()


def test_fifth_project_returns_resource_exhausted_without_opening_runtime(tmp_path: Path):
    created = []

    def factory(**kwargs):
        runtime = _Runtime(closeable=False, **kwargs)
        created.append(runtime)
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    seeded = _seed_projects_and_tasks(app, 5)
    for _, task_id, stored in seeded[:4]:
        assert app.continue_task(task_id=task_id, expected_generation=stored.generation) == stored

    _, fifth_task, fifth_stored = seeded[4]
    result = app.continue_task(
        task_id=fifth_task,
        expected_generation=fifth_stored.generation,
    )
    assert result == TaskServicePortFailure(code=TaskServicePortErrorCode.RESOURCE_EXHAUSTED)
    assert len(created) == 4
    app.close()


def test_runtime_remains_tracked_when_short_lease_release_and_close_are_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    created: list[_Runtime] = []

    def factory(**kwargs):
        runtime = _Runtime(closeable=False, **kwargs)
        created.append(runtime)
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    manager = app._lease_manager  # noqa: SLF001
    original_release = ResourceLeaseManager.release

    def release_then_lose_response(self, lease, *, owner_token):
        original_release(self, lease, owner_token=owner_token)
        if self is manager and getattr(lease, "project_id", None) == project_id:
            raise LeaseError(LeaseErrorCode.IO_ERROR, resource_key=lease.resource_key)

    monkeypatch.setattr(ResourceLeaseManager, "release", release_then_lose_response)
    result = app.continue_task(task_id=task_id, expected_generation=stored.generation)

    assert result == TaskServicePortFailure(code=TaskServicePortErrorCode.LEASE_UNAVAILABLE)
    assert len(created) == 1
    assert created[0].close_calls == 1
    assert app._runtimes[project_id] is created[0]  # noqa: SLF001

    monkeypatch.setattr(ResourceLeaseManager, "release", original_release)
    created[0].closeable = True
    app.close()


def test_application_close_retains_uncertain_runtime_authority_without_retry(
    tmp_path: Path,
):
    created: list[_Runtime] = []

    def factory(**kwargs):
        runtime = _Runtime(closeable=False, **kwargs)
        created.append(runtime)
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    assert app.continue_task(task_id=task_id, expected_generation=stored.generation) == stored

    app.close()

    assert app._closed is True  # noqa: SLF001
    assert app._runtimes[project_id] is created[0]  # noqa: SLF001
    assert created[0].close_calls == 1
    app.close()
    assert created[0].close_calls == 1


def test_concurrent_application_close_attempts_uncertain_runtime_only_once(
    tmp_path: Path,
):
    entered = threading.Event()
    release = threading.Event()
    created: list[_Runtime] = []

    class BlockingRuntime(_Runtime):
        def close(self):
            self.close_calls += 1
            entered.set()
            assert release.wait(timeout=3)
            return False

    def factory(**kwargs):
        runtime = BlockingRuntime(**kwargs)
        created.append(runtime)
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    assert app.continue_task(task_id=task_id, expected_generation=stored.generation) == stored

    class ObservableGate:
        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.counter_lock = threading.Lock()
            self.entries = 0

        def __enter__(self):
            with self.counter_lock:
                self.entries += 1
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    gate = ObservableGate()
    app._cad_gate = gate  # noqa: SLF001

    second_returned = threading.Event()

    def second_close() -> None:
        app.close()
        second_returned.set()

    closers = [threading.Thread(target=app.close), threading.Thread(target=second_close)]
    closers[0].start()
    assert entered.wait(timeout=2)
    closers[1].start()
    assert not second_returned.wait(timeout=0.1)
    assert gate.entries == 1
    release.set()
    for closer in closers:
        closer.join(timeout=3)

    assert all(not closer.is_alive() for closer in closers)
    assert second_returned.is_set()
    assert gate.entries == 1
    assert created[0].close_calls == 1
    assert app._closed is True  # noqa: SLF001
    assert app._runtimes[project_id] is created[0]  # noqa: SLF001


def test_close_wins_over_a_cad_call_waiting_before_the_global_gate(tmp_path: Path):
    created_runtimes: list[object] = []

    def factory(**kwargs):
        runtime = _Runtime(**kwargs)
        created_runtimes.append(runtime)
        return runtime

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=factory,
    )
    _, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    authentic_catalog = app._catalog  # noqa: SLF001
    entered = threading.Event()
    release = threading.Event()

    class BlockingCatalog:
        def load_expected(self, task_id: str, generation: object):
            entered.set()
            assert release.wait(timeout=3)
            return authentic_catalog.load_expected(task_id, generation)

    app._catalog = BlockingCatalog()  # noqa: SLF001
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            app.continue_task(task_id=task_id, expected_generation=stored.generation)
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=invoke)
    caller.start()
    assert entered.wait(timeout=2)
    app.close()
    release.set()
    caller.join(timeout=3)

    assert not caller.is_alive()
    assert len(errors) == 1 and type(errors[0]) is RuntimeError
    assert created_runtimes == []
    assert not app._runtimes  # noqa: SLF001


def test_close_wins_over_import_before_it_enters_the_global_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    entered = threading.Event()
    release = threading.Event()
    port_calls: list[str] = []
    project_id = "project_" + "f" * 32

    def blocked_project_id() -> str:
        entered.set()
        assert release.wait(timeout=3)
        return project_id

    monkeypatch.setattr(agent_module, "_new_project_id", blocked_project_id)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: port_calls.append("port"),
    )
    source = tmp_path / "source.FCStd"
    source.write_bytes(b"private source")
    source.chmod(0o600)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            app.bootstrap_import(source=source)
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=invoke)
    caller.start()
    assert entered.wait(timeout=2)
    app.close()
    release.set()
    caller.join(timeout=3)

    assert not caller.is_alive()
    assert len(errors) == 1 and type(errors[0]) is RuntimeError
    assert port_calls == []
    assert tuple(app._layout.projects.iterdir()) == ()  # noqa: SLF001


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_application_rejects_a_fork_inherited_capability(tmp_path: Path):
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    app.bootstrap_empty()
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(read_fd)
        try:
            app.get_task(task_id=_task_id(1))
        except RuntimeError:
            os.write(write_fd, b"rejected")
            os._exit(0)
        os._exit(1)

    os.close(write_fd)
    message = os.read(read_fd, 32)
    _, status = os.waitpid(child, 0)
    os.close(read_fd)
    assert os.waitstatus_to_exitcode(status) == 0
    assert message == b"rejected"
    app.close()


def test_two_application_instances_share_one_process_cad_gate(tmp_path: Path):
    activity = {"lock": threading.Lock(), "active": 0, "maximum": 0}

    def factory(**kwargs):
        return _Runtime(activity=activity, **kwargs)

    first = AgentApplication.open(data_root=_data_root(tmp_path / "first"), runtime_factory=factory)
    second = AgentApplication.open(
        data_root=_data_root(tmp_path / "second"), runtime_factory=factory
    )
    first_item = _seed_projects_and_tasks(first, 1)[0]
    second_item = _seed_projects_and_tasks(second, 1)[0]

    threads = [
        threading.Thread(
            target=lambda app, item: app.continue_task(
                task_id=item[1], expected_generation=item[2].generation
            ),
            args=(app, item),
        )
        for app, item in ((first, first_item), (second, second_item))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert activity["maximum"] == 1
    first.close()
    second.close()


def test_task_project_and_artifact_cad_paths_share_the_process_gate(tmp_path: Path):
    activity = {"lock": threading.Lock(), "active": 0, "maximum": 0}
    calls: list[str] = []

    class ValidationCad(CadExecutionPort):
        def _run(self, name: str):
            calls.append(name)
            with activity["lock"]:
                activity["active"] += 1
                activity["maximum"] = max(activity["maximum"], activity["active"])
            time.sleep(0.03)
            with activity["lock"]:
                activity["active"] -= 1
            return object()

        def validate_import(self, _path: Path):
            return self._run("validate_import")

        def revalidate_normalized_import(self, _path: Path):
            return self._run("revalidate_normalized_import")

        def validate_materialization(self, *, fcstd: Path, step: Path):
            del fcstd, step
            return self._run("validate_materialization")

    def runtime_factory(**kwargs):
        return _Runtime(activity=activity, **kwargs)

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=runtime_factory,
        cad_port_factory=lambda **_kwargs: ValidationCad(),
    )
    _, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    app.create_project_request(
        {
            "schema_version": 1,
            "create_key": "project_create_" + "5" * 32,
            "kind": "empty",
        }
    )
    app.export_task_artifacts_request({"schema_version": 1})
    project_cad = app._project_service._cad_port_factory(  # noqa: SLF001
        revision_store=app._revision_store,  # noqa: SLF001
    )
    artifact_cad = app._artifact_service._cad  # noqa: SLF001
    assert project_cad is artifact_cad is app._cad_validation_port  # noqa: SLF001

    barrier = threading.Barrier(3)

    def task_call() -> None:
        barrier.wait()
        app.continue_task(task_id=task_id, expected_generation=stored.generation)

    def project_call() -> None:
        barrier.wait()
        project_cad.validate_import(tmp_path / "model.FCStd")

    def artifact_call() -> None:
        barrier.wait()
        artifact_cad.validate_materialization(
            fcstd=tmp_path / "materialized.FCStd",
            step=tmp_path / "materialized.step",
        )

    workers = [
        threading.Thread(target=task_call),
        threading.Thread(target=project_call),
        threading.Thread(target=artifact_call),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert activity["maximum"] == 1
    assert sorted(calls) == ["validate_import", "validate_materialization"]
    app.close()


def test_validation_and_project_runtimes_share_one_lazy_application_cad_port(
    tmp_path: Path,
) -> None:
    class SharedCad(CadExecutionPort):
        def validate_import(self, _path: Path):
            return object()

    port = SharedCad()
    factory_calls: list[object] = []
    runtime_ports: list[object] = []

    def cad_factory(**_kwargs):
        factory_calls.append(object())
        return port

    def runtime_factory(**kwargs):
        runtime_ports.append(kwargs["cad_port"])
        return _Runtime(**kwargs)

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=runtime_factory,
        cad_port_factory=cad_factory,
    )
    _, task_id, stored = _seed_projects_and_tasks(app, 1)[0]

    assert app._invoke_validation_cad("validate_import", tmp_path / "model.FCStd") is not None  # noqa: SLF001
    assert app.continue_task(task_id=task_id, expected_generation=stored.generation) == stored

    assert len(factory_calls) == 1
    assert runtime_ports == [port]
    app.close()


def test_generation_fence_rejects_runtime_created_by_an_older_epoch(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []
    created_runtimes: list[_Runtime] = []

    class KillableCad(CadExecutionPort):
        def __init__(self) -> None:
            self.generation_lost = False
            self.terminate_calls = 0

        def terminate_generation(self) -> None:
            self.terminate_calls += 1
            self.generation_lost = True

    port = KillableCad()

    def runtime_factory(**kwargs):
        entered.set()
        assert release.wait(timeout=3)
        runtime = _Runtime(**kwargs)
        created_runtimes.append(runtime)
        return runtime

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=runtime_factory,
        cad_port_factory=lambda **_kwargs: port,
    )
    _, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    caller = threading.Thread(
        target=lambda: results.append(
            app.continue_task(
                task_id=task_id,
                expected_generation=stored.generation,
            )
        )
    )
    caller.start()
    assert entered.wait(timeout=2)

    assert app._fence_cad_generation() is True  # noqa: SLF001
    release.set()
    caller.join(timeout=3)

    assert not caller.is_alive()
    assert results == [TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)]
    assert port.terminate_calls == 1
    assert len(created_runtimes) == 1
    assert created_runtimes[0].close_calls == 1
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()


def test_idle_task_cancel_terminates_a_blocked_runtime_load_and_allows_a_new_generation(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []

    class BlockingLoadCad(CadExecutionPort):
        def __init__(self) -> None:
            self.generation_lost = False
            self.terminate_calls = 0

        def open_revision(self, *, store, revision):
            del store, revision
            entered.set()
            assert release.wait(timeout=3)
            if self.generation_lost:
                raise ExecutorError(ExecutorErrorCode.CAD_FAILURE)
            return object()

        def terminate_generation(self) -> None:
            durable = app._task_store.load(task_id)  # noqa: SLF001
            assert durable.task_run.status is TaskStatus.CANCEL_REQUESTED
            self.terminate_calls += 1
            self.generation_lost = True
            release.set()

    class HealthyCad(CadExecutionPort):
        generation_lost = False

        def open_revision(self, *, store, revision):
            del store, revision
            return object()

        def close(self, _session: object) -> None:
            return None

        def close_generation(self) -> None:
            return None

    blocked = BlockingLoadCad()
    healthy = HealthyCad()
    ports = [blocked, healthy]
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: ports.pop(0),
    )
    project_id, task_id, created = _seed_projects_and_tasks(app, 1)[0]
    head_before = app._revision_store.load_head(project_id)  # noqa: SLF001
    caller = threading.Thread(
        target=lambda: results.append(
            app.submit_model_program(
                task_id=task_id,
                expected_generation=created.generation,
                program=_model_program(task_id, created.task_run.base_revision),
            )
        )
    )
    caller.start()
    assert entered.wait(timeout=2)
    assert app._task_store.load(task_id) == created  # noqa: SLF001

    cancelled = app.cancel_task(
        task_id=task_id,
        expected_generation=created.generation,
    )
    caller.join(timeout=3)

    assert cancelled.task_run.status is TaskStatus.CANCELLED
    assert not caller.is_alive()
    assert len(results) == 1
    assert type(results[0]) is StoredTaskRun
    assert any(
        record.event
        in {
            TaskEvent.REQUEST_CANCEL,
            TaskEvent.REQUEST_ACTIVE_CANCEL,
        }
        for record in results[0].task_run.transitions
    )
    assert blocked.terminate_calls == 1
    assert app._revision_store.load_head(project_id) == head_before  # noqa: SLF001
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    assert app._cad_task_admissions == {}  # noqa: SLF001

    with app._cad_gate:  # noqa: SLF001
        replacement = app._runtime_for(project_id)  # noqa: SLF001
    assert type(replacement) is ProjectRuntime
    assert app._cad_execution_port is healthy  # noqa: SLF001
    assert ports == []
    app.close()


def test_cancelling_a_queued_task_does_not_terminate_the_current_gate_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_entered = threading.Event()
    release_first = threading.Event()
    second_prechecked = threading.Event()
    results: dict[str, object] = {}

    class BlockingCad(CadExecutionPort):
        def __init__(self) -> None:
            self.open_calls = 0
            self.terminate_calls = 0

        def open_revision(self, *, store, revision):
            del store, revision
            self.open_calls += 1
            first_entered.set()
            assert release_first.wait(timeout=3)
            return object()

        def close(self, _session: object) -> None:
            return None

        def terminate_generation(self) -> None:
            self.terminate_calls += 1
            release_first.set()

        def close_generation(self) -> None:
            release_first.set()

    port = BlockingCad()
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: port,
    )
    first, second = _seed_projects_and_tasks(app, 2)
    original_load_expected = TaskCatalogService.load_expected
    second_reads = 0

    def observed_load_expected(self, task_id, generation):
        nonlocal second_reads
        result = original_load_expected(self, task_id, generation)
        if task_id == second[1]:
            second_reads += 1
            if second_reads == 1:
                second_prechecked.set()
        return result

    monkeypatch.setattr(
        TaskCatalogService,
        "load_expected",
        observed_load_expected,
    )
    owner = threading.Thread(
        target=lambda: results.setdefault(
            "first",
            app.continue_task(
                task_id=first[1],
                expected_generation=first[2].generation,
            ),
        )
    )
    queued = threading.Thread(
        target=lambda: results.setdefault(
            "second",
            app.continue_task(
                task_id=second[1],
                expected_generation=second[2].generation,
            ),
        )
    )
    owner.start()
    assert first_entered.wait(timeout=2)
    queued.start()
    assert second_prechecked.wait(timeout=2)

    cancelled = app.cancel_task(
        task_id=second[1],
        expected_generation=second[2].generation,
    )

    assert cancelled.task_run.status is TaskStatus.CANCELLED
    assert port.terminate_calls == 0
    assert owner.is_alive()
    release_first.set()
    owner.join(timeout=3)
    queued.join(timeout=3)

    assert not owner.is_alive()
    assert not queued.is_alive()
    assert port.open_calls == 1
    assert results["second"] == TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
    assert app._task_store.load(second[1]) == cancelled  # noqa: SLF001
    assert app._cad_task_admissions == {}  # noqa: SLF001
    app.close()


def test_old_epoch_cannot_reuse_a_cached_runtime_from_the_new_generation(
    tmp_path: Path,
) -> None:
    class GenerationCad(CadExecutionPort):
        def close_generation(self) -> None:
            return None

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=lambda **kwargs: _Runtime(**kwargs),
        cad_port_factory=lambda **_kwargs: GenerationCad(),
    )
    project_id, _task_id_value, _stored = _seed_projects_and_tasks(app, 1)[0]
    old_epoch = app._generation_epoch  # noqa: SLF001
    assert app._fence_cad_generation() is True  # noqa: SLF001
    current_epoch = app._generation_epoch  # noqa: SLF001
    assert current_epoch == old_epoch + 1

    fresh = app._runtime_for(  # noqa: SLF001
        project_id,
        expected_generation_epoch=current_epoch,
    )
    stale = app._runtime_for(  # noqa: SLF001
        project_id,
        expected_generation_epoch=old_epoch,
    )

    assert type(fresh) is _Runtime
    assert stale == TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
    assert app._runtimes[project_id] is fresh  # noqa: SLF001
    app.close()


def test_runtime_startup_worker_loss_is_recoverable_and_next_generation_starts(
    tmp_path: Path,
) -> None:
    class StartupCad(CadExecutionPort):
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail
            self.generation_lost = False
            self.open_calls = 0
            self.close_calls = 0

        def open_revision(self, *, store, revision):
            del store, revision
            self.open_calls += 1
            if self.fail:
                self.generation_lost = True
                raise ExecutorError(ExecutorErrorCode.CAD_FAILURE)
            return object()

        def close(self, _session: object) -> None:
            self.close_calls += 1

        def terminate_generation(self) -> None:
            self.generation_lost = True

        def close_generation(self) -> None:
            return None

    ports = [StartupCad(fail=True), StartupCad(fail=False)]

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: ports.pop(0),
    )
    project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]

    failed = app.continue_task(
        task_id=task_id,
        expected_generation=stored.generation,
    )

    assert failed == TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
    assert ports[0].open_calls == 0
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001

    retried = app.continue_task(
        task_id=task_id,
        expected_generation=stored.generation,
    )

    assert retried == TaskServicePortFailure(code=TaskServicePortErrorCode.INVALID_STATE)
    assert ports == []
    assert set(app._runtimes) == {project_id}  # noqa: SLF001
    app.close()


def test_generation_fence_rejects_an_already_admitted_validation_call(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    factory_calls: list[str] = []
    errors: list[BaseException] = []

    class BlockingGate:
        def __enter__(self):
            entered.set()
            assert release.wait(timeout=3)

        def __exit__(self, *_args):
            return False

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: factory_calls.append("cad"),
    )
    app._cad_gate = BlockingGate()  # noqa: SLF001

    def validate() -> None:
        try:
            app._invoke_validation_cad(  # noqa: SLF001
                "validate_import",
                tmp_path / "model.FCStd",
            )
        except BaseException as error:
            errors.append(error)

    caller = threading.Thread(target=validate)
    caller.start()
    assert entered.wait(timeout=2)
    assert app._fence_cad_generation() is True  # noqa: SLF001
    release.set()
    caller.join(timeout=3)

    assert not caller.is_alive()
    assert len(errors) == 1
    assert type(errors[0]) is ExecutorError
    assert errors[0].code is ExecutorErrorCode.CAD_FAILURE
    assert factory_calls == []
    assert app._cad_execution_port is None  # noqa: SLF001
    app._cad_gate = threading.Lock()  # noqa: SLF001
    app.close()


def test_observed_worker_loss_evicts_every_runtime_in_the_generation(
    tmp_path: Path,
) -> None:
    class LossAwareCad(CadExecutionPort):
        generation_lost = False

        def terminate_generation(self) -> None:
            self.generation_lost = True

        def close_generation(self) -> None:
            return None

    port = LossAwareCad()
    lose_generation = False

    class Service(_RuntimeService):
        def continue_task(self, *, task_id: str, expected_generation: int):
            result = super().continue_task(
                task_id=task_id,
                expected_generation=expected_generation,
            )
            if lose_generation:
                port.generation_lost = True
            return result

    class Runtime(_Runtime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.service = Service(kwargs["task_store"])

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=lambda **kwargs: Runtime(**kwargs),
        cad_port_factory=lambda **_kwargs: port,
    )
    first, second = _seed_projects_and_tasks(app, 2)
    for _project_id, task_id, stored in (first, second):
        assert (
            app.continue_task(
                task_id=task_id,
                expected_generation=stored.generation,
            )
            == stored
        )
    assert set(app._runtimes) == {first[0], second[0]}  # noqa: SLF001

    lose_generation = True
    assert (
        app.continue_task(
            task_id=first[1],
            expected_generation=first[2].generation,
        )
        == first[2]
    )

    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()


def test_close_closes_admission_before_runtime_teardown_and_store_outside_cad_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibecad.application.artifacts import ArtifactStore

    entered = threading.Event()
    release = threading.Event()
    store_closed = threading.Event()

    class BlockingRuntime(_Runtime):
        def close(self):
            self.close_calls += 1
            entered.set()
            assert release.wait(timeout=3)
            return True

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=lambda **kwargs: BlockingRuntime(**kwargs),
    )
    project_id, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    assert app.continue_task(task_id=task_id, expected_generation=stored.generation) == stored
    task_api = app._task_api_for_request()  # noqa: SLF001
    app.export_task_artifacts_request({"schema_version": 1})
    original_close = ArtifactStore.close

    def close_outside_gate(store) -> None:
        assert app._closed is True  # noqa: SLF001
        assert app._cad_gate.acquire(blocking=False) is True  # noqa: SLF001
        app._cad_gate.release()  # noqa: SLF001
        original_close(store)
        store_closed.set()

    monkeypatch.setattr(ArtifactStore, "close", close_outside_gate)
    closer = threading.Thread(target=app.close)
    closer.start()
    assert entered.wait(timeout=2)

    assert app._closed is True  # noqa: SLF001
    retained = task_api.get_task({"schema_version": 1, "task_id": task_id})
    assert retained["error"]["code"] == "internal_error"

    release.set()
    closer.join(timeout=3)
    assert not closer.is_alive()
    assert store_closed.is_set()
    assert project_id not in app._runtimes  # noqa: SLF001


def test_close_attempts_artifact_store_after_generation_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibecad.application.artifacts import ArtifactStore

    store_close_calls = 0

    class FailingCloseCad(CadExecutionPort):
        def validate_import(self, _path: Path):
            return object()

        def close_generation(self) -> None:
            raise RuntimeError("generation close failed")

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=lambda **_kwargs: FailingCloseCad(),
    )
    assert app._invoke_validation_cad("validate_import", tmp_path / "model.FCStd") is not None  # noqa: SLF001
    app.export_task_artifacts_request({"schema_version": 1})
    original_close = ArtifactStore.close

    def observed_close(store) -> None:
        nonlocal store_close_calls
        store_close_calls += 1
        original_close(store)

    monkeypatch.setattr(ArtifactStore, "close", observed_close)
    with pytest.raises(RuntimeError, match="generation close failed"):
        app.close()

    assert store_close_calls == 1
    assert app._closed is True  # noqa: SLF001
    assert app._cad_execution_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    app.close()
    assert store_close_calls == 1


@pytest.mark.parametrize("code", tuple(TaskServiceErrorCode))
def test_application_bridges_every_closed_task_service_error(tmp_path: Path, code):
    expected = TaskServicePortErrorCode(code.value)

    def factory(**kwargs):
        runtime = _Runtime(**kwargs)
        runtime.service = _FailingRuntimeService(TaskServiceError(code))
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    _, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    assert app.continue_task(
        task_id=task_id,
        expected_generation=stored.generation,
    ) == TaskServicePortFailure(code=expected)
    app.close()


def test_application_does_not_reclassify_an_unknown_service_exception(tmp_path: Path):
    def factory(**kwargs):
        runtime = _Runtime(**kwargs)
        runtime.service = _FailingRuntimeService(RuntimeError("private-detail"))
        return runtime

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    _, task_id, stored = _seed_projects_and_tasks(app, 1)[0]
    with pytest.raises(RuntimeError, match="private-detail"):
        app.continue_task(task_id=task_id, expected_generation=stored.generation)
    app.close()


def test_full_head_gap_evicts_stale_runtime_before_any_durable_or_cad_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    state = {"advanced": False}
    built_heads = []
    ports = []

    def factory(**kwargs):
        head = kwargs["head"]
        port = _GapCadPort()
        binding = SessionBinding(
            project_id=kwargs["project_id"],
            revision_id=head.revision_id,
            session=object(),
        )
        coordinator = CandidateCoordinator(
            store=kwargs["revision_store"],
            snapshot_port=port,
            session_slot=SessionSlot(binding),
        )
        service = TaskService(
            task_store=kwargs["task_store"],
            revision_store=kwargs["revision_store"],
            lease_manager=kwargs["lease_manager"],
            coordinator=coordinator,
            executor=port,
            runtime_head=head,
        )
        built_heads.append(head)
        ports.append(port)
        wrapped = _GapService(service, lambda: state.update(advanced=True))
        return ProjectRuntime(
            project_id=kwargs["project_id"],
            coordinator=coordinator,
            service=wrapped,
        )

    app = AgentApplication.open(data_root=_data_root(tmp_path), runtime_factory=factory)
    project = app.bootstrap_empty()
    head0 = project.head
    head1 = ProjectHead(
        project_id=head0.project_id,
        generation=head0.generation + 1,
        revision_id=head0.revision_id,
        manifest_sha256=head0.manifest_sha256,
    )
    task_id = _task_id(99)
    created = app.create_task(
        task_id=task_id,
        project_id=head0.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    original_load_head = LocalRevisionStore.load_head

    def advancing_head(store, project_id):
        if store is app._revision_store and state["advanced"]:  # noqa: SLF001
            return head1
        return original_load_head(store, project_id)

    monkeypatch.setattr(LocalRevisionStore, "load_head", advancing_head)
    before_projects = _tree_digest(app._layout.projects)  # noqa: SLF001
    result = app.submit_model_program(
        task_id=task_id,
        expected_generation=created.generation,
        program=_model_program(task_id, head0.revision_id),
    )
    assert result == TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
    assert app.get_task(task_id=task_id) == created
    assert _tree_digest(app._layout.projects) == before_projects  # noqa: SLF001
    assert ports[0].execute_calls == 0
    assert ports[0].close_calls == 1
    assert head0.project_id not in app._runtimes  # noqa: SLF001

    with app._cad_gate:  # noqa: SLF001
        rebuilt = app._runtime_for(head0.project_id)  # noqa: SLF001
    assert type(rebuilt) is ProjectRuntime
    assert built_heads == [head0, head1]
    app.close()
