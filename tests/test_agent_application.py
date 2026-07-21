"""Lazy, store-backed AgentApplication composition tests."""

from __future__ import annotations

import hashlib
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
from vibecad.application.agent import AgentApplication
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
from vibecad.execution.revisions import LocalRevisionStore, ProjectHead
from vibecad.interaction.cad import CadExecutionPort
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.lease import LeaseError, LeaseErrorCode, ResourceLeaseManager
from vibecad.workflow.program import validate_model_program
from vibecad.workflow.service import (
    TaskService,
    TaskServiceError,
    TaskServiceErrorCode,
)
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy


def _task_id(index: int) -> str:
    return f"task_{index:032x}"


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
        )
    ) == ("locks", "tasks", "projects", "bootstrap", "checkouts")
    for path in (
        layout.root,
        layout.locks,
        layout.tasks,
        layout.projects,
        layout.bootstrap,
        layout.checkouts,
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


def test_empty_bootstrap_and_task_control_never_create_a_cad_runtime(tmp_path: Path):
    calls: list[str] = []

    def forbidden_runtime(*_args, **_kwargs):
        calls.append("runtime")
        raise AssertionError("CAD runtime must stay lazy")

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=forbidden_runtime,
    )
    project = app.bootstrap_empty()
    assert project.head.project_id.startswith("project_")
    assert project.head.generation == 0
    assert project.cleanup_required is False

    api = TaskApi(port=app)
    created = api.create_task(
        {
            "schema_version": 1,
            "project_id": project.head.project_id,
            "review_policy": "auto_commit",
        }
    )
    assert created["ok"] is True
    task_id = created["result"]["task_run"]["id"]
    loaded = api.get_task({"schema_version": 1, "task_id": task_id})
    assert loaded == created
    assert calls == []
    app.close()


def test_fresh_empty_application_path_does_not_import_cad_modules(tmp_path: Path):
    data_root = _data_root(tmp_path)
    script = f"""
import json
import sys
from pathlib import Path
from vibecad.application.agent import AgentApplication
from vibecad.application.task_api import TaskApi
app = AgentApplication.open(data_root=Path({str(data_root)!r}))
project = app.bootstrap_empty()
api = TaskApi(port=app)
response = api.create_task({{
    'schema_version': 1,
    'project_id': project.head.project_id,
    'review_policy': 'auto_commit'
}})
assert response['ok'] is True
task_id = response['result']['task_run']['id']
assert api.get_task({{'schema_version': 1, 'task_id': task_id}}) == response
rejected = api.reject_draft({{
    'schema_version': 1,
    'task_id': task_id,
    'draft_id': 'draft_' + '0' * 32,
    'expected_generation': 0,
}})
assert rejected['ok'] is False
assert rejected['error']['code'] == 'invalid_state'
assert AgentApplication.execution_capabilities()['headless'] == 'verified'
app.close()
forbidden = ('FreeCAD', 'Part', 'vibecad.engine', 'vibecad.tools',
             'vibecad.workflow.service', 'vibecad.execution.executor',
             'vibecad.execution.candidate')
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
            self.both_entered = threading.Event()

        def __enter__(self):
            with self.counter_lock:
                self.entries += 1
                if self.entries == 2:
                    self.both_entered.set()
            self.lock.acquire()
            return self

        def __exit__(self, *_args):
            self.lock.release()

    gate = ObservableGate()
    app._cad_gate = gate  # noqa: SLF001

    closers = [threading.Thread(target=app.close) for _ in range(2)]
    closers[0].start()
    assert entered.wait(timeout=2)
    closers[1].start()
    assert gate.both_entered.wait(timeout=2)
    release.set()
    for closer in closers:
        closer.join(timeout=3)

    assert all(not closer.is_alive() for closer in closers)
    assert gate.entries == 2
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
        def get_task(self, *, task_id: str):
            entered.set()
            assert release.wait(timeout=3)
            return authentic_catalog.get_task(task_id=task_id)

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
