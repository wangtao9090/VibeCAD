"""P0-B two-client and single-Kernel acceptance contracts."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibecad.application.agent import AgentApplication
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.interaction.cad import CadExecutionPort, CandidateEvidence
from vibecad.interaction.protocol_v2 import V2Response
from vibecad.validation import (
    ArtifactObservation,
    ObservationSnapshot,
    ShapeObservation,
)
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    StepResult,
    ValueSource,
)
from vibecad.workflow.program import validate_model_program
from vibecad.workflow.state import TaskArtifactRef

_DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the authenticated local daemon is currently a macOS capability",
)


@dataclass(slots=True)
class _DaemonAcceptanceRig:
    base: Path
    daemon: object
    application: AgentApplication
    cad: _ReviewCadPort
    first: object
    second: object


class _ReviewCadPort(CadExecutionPort):
    """Deterministic CAD boundary for transport/kernel acceptance tests."""

    def __init__(self) -> None:
        self.closed_sessions = 0
        self.generation_lost = False

    def create_empty(self, *, revision_id: str) -> object:
        del revision_id
        return object()

    def load_fcstd(self, path: Path) -> object:
        assert path.is_file()
        return object()

    def checkpoint_fcstd(self, session: object, path: Path) -> None:
        assert session is not None
        path.write_bytes(b"FCStd C13 MCP-created review draft")
        path.chmod(0o600)

    def validate_program(self, program: ModelProgram):
        return validate_model_program(program)

    def execute_program(self, *, program: object, candidate: object):
        return tuple(
            NormalizedToolOutcome(
                result=StepResult(
                    ok=True,
                    value={"inspected": True},
                    elapsed_ms=0.25,
                    operation_id=command.id,
                    revision=candidate.binding.revision_id,
                )
            )
            for command in program.commands
        )

    def export_step(self, *, candidate: object, lease: object) -> None:
        assert lease is not None
        candidate.step_path.write_bytes(b"ISO-10303-21;C13-MCP;ENDSEC;")
        candidate.step_path.chmod(0o600)

    def close(self, session: object) -> None:
        assert session is not None
        self.closed_sessions += 1

    def collect_evidence(self, *, candidate: object) -> CandidateEvidence:
        revision = candidate.revision
        assert revision.model is not None
        assert len(revision.artifacts) == 1
        artifacts = tuple(
            TaskArtifactRef(
                id=item.id,
                name=item.name,
                format=item.format,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                candidate_revision=revision.id,
            )
            for item in (revision.model,) + revision.artifacts
        )
        return CandidateEvidence(
            snapshot=ObservationSnapshot(
                candidate_revision=revision.id,
                shapes=(
                    ShapeObservation(
                        target="body",
                        volume_mm3=7200.0,
                        area_mm2=2400.0,
                        bbox_mm=(12.0, 20.0, 30.0),
                        center_of_mass_mm=(6.0, 10.0, 15.0),
                        valid_shape=True,
                        solid_count=1,
                    ),
                ),
                artifacts=(
                    ArtifactObservation(
                        target="export",
                        exists=True,
                        non_empty=True,
                        format="step",
                    ),
                    ArtifactObservation(
                        target="model",
                        exists=True,
                        non_empty=True,
                        format="fcstd",
                    ),
                ),
            ),
            artifacts=artifacts,
        )

    def close_generation(self) -> None:
        return None


def _short_private_data_root() -> tuple[Path, Path]:
    base = Path(tempfile.mkdtemp(prefix="vc-c13-", dir="/private/tmp"))
    base.chmod(0o700)
    return base, base / "data"


@pytest.fixture
def daemon_acceptance_rig():
    from vibecad.daemon import LocalAgentClient, LocalKernelDaemon, LocalKernelState

    base, data_root = _short_private_data_root()
    captured: list[AgentApplication] = []
    cad = _ReviewCadPort()
    daemon = None
    first = None
    second = None

    def application_factory(*, layout, lease_manager):
        application = AgentApplication.from_captured_layout(
            layout=layout,
            lease_manager=lease_manager,
            cad_port_factory=lambda **_kwargs: cad,
        )
        captured.append(application)
        return application

    try:
        daemon = LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=application_factory,
        )
        assert len(captured) == 1
        first = LocalAgentClient.connect(
            daemon.run_root,
            artifact_root=data_root / "artifacts",
        )
        second = LocalAgentClient.connect(
            daemon.run_root,
            artifact_root=data_root / "artifacts",
        )
        yield _DaemonAcceptanceRig(
            base=base,
            daemon=daemon,
            application=captured[0],
            cad=cad,
            first=first,
            second=second,
        )
    finally:
        for client in (first, second):
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()
        if daemon is not None and daemon.state is not LocalKernelState.CLOSED:
            with contextlib.suppress(Exception):
                daemon.close()
        shutil.rmtree(base, ignore_errors=True)


def _program(task_id: str, base_revision: str) -> ModelProgram:
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
            id="acceptance-c13-shared-kernel",
            criteria=(
                AcceptanceCriterion(
                    id="body-volume",
                    kind=AcceptanceKind.GEOMETRY,
                    check="volume",
                    target="body",
                    expected=7200.0,
                    tolerance=0.0,
                    parameters={"unit": "mm^3"},
                    required=True,
                ),
            ),
        ),
    )


def _submit_review_program(client: object, *, task_id: str) -> dict[str, object]:
    current = client.get_task_request(
        {
            "schema_version": 1,
            "task_id": task_id,
        }
    )
    assert current["ok"] is True
    task = current["result"]["task_run"]
    program = _program(task_id, task["base_revision"])
    submitted = client.submit_model_program_request(
        {
            "schema_version": 1,
            "task_id": task_id,
            "expected_generation": current["result"]["generation"],
            "program_json": json.dumps(
                program.to_mapping(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        }
    )
    assert submitted["ok"] is True
    assert submitted["result"]["task_run"]["status"] == "awaiting_user_review"
    return submitted


def _create_project_and_task(
    client: object,
    *,
    project_key_digit: str,
    task_key_digit: str,
) -> tuple[str, str]:
    project = client.create_project_request(
        {
            "schema_version": 1,
            "create_key": "project_create_" + project_key_digit * 32,
            "kind": "empty",
        }
    )
    assert project["ok"] is True
    project_id = project["result"]["project_id"]
    task = client.create_task_request(
        {
            "schema_version": 1,
            "create_key": "task_create_" + task_key_digit * 32,
            "project_id": project_id,
            "review_policy": "require_review",
        }
    )
    assert task["ok"] is True
    return project_id, task["result"]["task_run"]["id"]


class _McpRoute:
    """Exercise the real MCP handler against one daemon-client slot."""

    def __init__(self, application: object) -> None:
        self.application = application

    @staticmethod
    def _call(name: str, request: dict[str, object]) -> dict[str, object]:
        import anyio

        import vibecad.server as server

        result = anyio.run(server._handle_call_tool, name, request)
        assert type(result.structuredContent) is dict
        return result.structuredContent

    def create_project_request(self, request: dict[str, object]) -> dict[str, object]:
        return self._call("create_project", request)

    def create_task_request(self, request: dict[str, object]) -> dict[str, object]:
        return self._call("create_task", request)

    def get_task_request(self, request: dict[str, object]) -> dict[str, object]:
        return self._call("get_task", request)

    def get_project_request(self, request: dict[str, object]) -> dict[str, object]:
        return self._call("get_project", request)

    def submit_model_program_request(
        self,
        request: dict[str, object],
    ) -> dict[str, object]:
        return self._call("submit_model_program", request)

    def accept_draft_request(self, request: dict[str, object]) -> dict[str, object]:
        return self._call("accept_draft", request)

    def reject_draft_request(self, request: dict[str, object]) -> dict[str, object]:
        return self._call("reject_draft", request)


def _wait_for_connections(daemon: object, expected: int) -> None:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if daemon.active_connections == expected:
            return
        time.sleep(0.01)
    assert daemon.active_connections == expected


class _RecordingKernelClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def call(
        self,
        method: object,
        params: object,
        *,
        request_id: object | None = None,
    ) -> V2Response:
        del request_id
        assert type(method) is str
        assert type(params) is dict
        self.calls.append((method, params))
        if method == "kernel.ping":
            result = {
                "schema_version": 1,
                "daemon_id": "daemon_" + "1" * 32,
                "status": "ready",
                "protocol": {"major": 2, "minor": 0},
            }
        elif method == "application.call":
            result = {
                "schema_version": 1,
                "ok": True,
                "result": {
                    "operation": params["operation"],
                    "request": params["request"],
                },
                "error": None,
            }
        elif method == "checkout.open":
            result = {
                "schema_version": 1,
                "checkout_id": "checkout_" + "2" * 32,
                "file_grant": {
                    "schema_version": 1,
                    "grant_id": "file_grant_" + "3" * 32,
                    "purpose": "open_managed_checkout",
                    "expires_in_ms": 30_000,
                },
            }
        elif method == "file_grant.claim":
            result = {
                "schema_version": 1,
                "grant_id": params["grant_id"],
                "checkout_id": "checkout_" + "2" * 32,
                "purpose": "open_managed_checkout",
                "local_path": "/private/tmp/checkout_" + "2" * 32 + "/model.FCStd",
                "current_model_sha256": "4" * 64,
                "current_size_bytes": 12,
            }
        else:
            raise AssertionError(f"unexpected method: {method}")
        return V2Response(
            request_id="request_" + "5" * 32,
            sequence=len(self.calls),
            result=result,
            error=None,
        )

    def close(self) -> None:
        self.closed = True


class _BootstrapProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.poll_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> None:
        self.poll_calls += 1
        return None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, *, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        return 0


def test_public_local_agent_client_is_a_thin_exact_application_adapter() -> None:
    from vibecad.daemon import LocalAgentClient

    kernel = _RecordingKernelClient()
    client = LocalAgentClient(kernel)
    request = {
        "schema_version": 1,
        "project_id": "project_" + "6" * 32,
    }

    assert client.get_project_request(request) == {
        "schema_version": 1,
        "ok": True,
        "result": {"operation": "get_project", "request": request},
        "error": None,
    }
    assert kernel.calls == [
        (
            "application.call",
            {"operation": "get_project", "request": request},
        )
    ]
    client.close()
    assert kernel.closed is True


def test_public_local_agent_client_exposes_only_session_bound_workbench_file_claims() -> None:
    from vibecad.daemon import LocalAgentClient

    kernel = _RecordingKernelClient()
    client = LocalAgentClient(kernel)
    opened = client.open_checkout(
        open_key="checkout_open_" + "7" * 32,
        source={
            "kind": "head",
            "project_id": "project_" + "6" * 32,
        },
    )
    grant = opened["file_grant"]
    assert "local_path" not in opened
    claimed = client.claim_file_grant(grant_id=grant["grant_id"])
    assert claimed["local_path"].endswith("/checkout_" + "2" * 32 + "/model.FCStd")
    assert [method for method, _params in kernel.calls] == [
        "checkout.open",
        "file_grant.claim",
    ]


def test_public_client_rejects_source_path_before_any_protocol_v2_call() -> None:
    from vibecad.daemon import LocalAgentClient

    kernel = _RecordingKernelClient()
    client = LocalAgentClient(kernel)

    result = client.create_project_request(
        {
            "schema_version": 1,
            "create_key": "project_create_" + "1" * 32,
            "kind": "empty",
            "source_path": "/private/should-never-cross.FCStd",
        }
    )

    assert result["ok"] is False
    assert result["error"] == {
        "schema_version": 1,
        "code": "unknown_field",
        "path": "/source_path",
        "message": "The request contains an unknown field.",
    }
    assert kernel.calls == []


def test_known_import_response_is_not_rewritten_by_post_response_source_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.daemon.client as client_module
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    data_root = tmp_path / "data"
    run_root = data_root / "daemon"
    data_root.mkdir()
    source = tmp_path / "source.FCStd"
    source.write_bytes(b"fcstd")
    expected = V2Response(
        request_id="request_" + "7" * 32,
        sequence=1,
        result={"schema_version": 1, "ok": True, "result": {}, "error": None},
        error=None,
    )

    class Pinned:
        before = source.stat()
        fd = 101
        verify_calls = 0
        closed = False

        def verify(self):
            self.verify_calls += 1
            if self.verify_calls == 2:
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)

        def close(self):
            self.closed = True

    pinned = Pinned()
    monkeypatch.setattr(
        client_module,
        "_open_import_source",
        lambda _path, *, managed_identity: pinned,
    )
    monkeypatch.setattr(
        client_module.LocalKernelClient,
        "_call",
        lambda _self, *_args, **_kwargs: expected,
    )
    client = object.__new__(client_module.LocalKernelClient)
    client._creator_pid = os.getpid()  # noqa: SLF001
    client._closed = False  # noqa: SLF001
    client._boot_state = SimpleNamespace(  # noqa: SLF001
        root=SimpleNamespace(path=run_root),
    )

    actual = client.import_project(
        {
            "schema_version": 1,
            "create_key": "project_create_" + "8" * 32,
            "kind": "import_fcstd",
        },
        source_path=str(source),
    )

    assert actual is expected
    assert pinned.verify_calls == 2
    assert pinned.closed is True


def test_daemon_bootstrap_reuses_a_live_kernel_without_spawning(tmp_path: Path) -> None:
    from vibecad.daemon.bootstrap import connect_or_start_local_kernel

    expected = _RecordingKernelClient()
    spawns: list[str] = []

    def connect(run_root: object):
        assert run_root == tmp_path / "daemon"
        return expected

    def spawn():
        spawns.append("spawn")
        raise AssertionError("a live daemon must be reused")

    actual = connect_or_start_local_kernel(
        run_root=tmp_path / "daemon",
        _connect=connect,
        _spawn=spawn,
    )
    assert actual is expected
    assert spawns == []


def test_daemon_bootstrap_concurrent_starters_keep_one_published_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.daemon.bootstrap as daemon_bootstrap
    from vibecad.daemon.state import DaemonError, DaemonErrorCode
    from vibecad.runtime import paths

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    run_root = paths.data_root() / "daemon"
    process_lock = threading.Lock()
    published = threading.Event()
    processes: list[_BootstrapProcess] = []
    terminated_groups: list[tuple[int, signal.Signals]] = []
    reaped_winners: list[int] = []
    results: list[_RecordingKernelClient] = []
    errors: list[BaseException] = []

    def connect(actual_root: object) -> _RecordingKernelClient:
        assert actual_root == run_root
        if not published.is_set():
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        return _RecordingKernelClient()

    def spawn() -> _BootstrapProcess:
        with process_lock:
            process = _BootstrapProcess(7_001 + len(processes))
            processes.append(process)
            published.set()
        return process

    def invoke() -> None:
        try:
            results.append(
                daemon_bootstrap.connect_or_start_local_kernel(
                    run_root=run_root,
                    _connect=connect,
                    _spawn=spawn,
                )
            )
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(
        daemon_bootstrap,
        "read_boot_state",
        lambda _root: SimpleNamespace(receipt=SimpleNamespace(pid=7_001)),
    )
    monkeypatch.setattr(
        daemon_bootstrap.os,
        "killpg",
        lambda pid, signum: terminated_groups.append((pid, signum)),
    )
    monkeypatch.setattr(
        daemon_bootstrap,
        "_reap_winning_process",
        lambda process: reaped_winners.append(process.pid),
    )

    workers = [threading.Thread(target=invoke) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(results) == 2
    assert [process.pid for process in processes] == [7_001]
    assert reaped_winners == [7_001]
    assert processes[0].poll_calls == 0
    assert processes[0].wait_timeouts == []
    assert terminated_groups == []


def test_daemon_bootstrap_deadline_covers_polling_and_cleans_spawned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.daemon.bootstrap as daemon_bootstrap
    from vibecad.daemon.state import DaemonError, DaemonErrorCode
    from vibecad.runtime import paths

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    process = _BootstrapProcess(0)
    connect_calls = 0
    sleeps: list[float] = []
    now = 0.0

    def clock() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        sleeps.append(duration)
        now += duration

    def connect(_run_root: object) -> _RecordingKernelClient:
        nonlocal connect_calls
        connect_calls += 1
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    with pytest.raises(DaemonError) as raised:
        daemon_bootstrap.connect_or_start_local_kernel(
            run_root=paths.data_root() / "daemon",
            timeout_seconds=0.25,
            _connect=connect,
            _spawn=lambda: process,
            _clock=clock,
            _sleep=sleep,
        )

    assert raised.value.code is DaemonErrorCode.UNAVAILABLE
    assert connect_calls >= 2
    assert sum(sleeps) == pytest.approx(0.25)
    assert process.poll_calls == 1
    assert process.terminate_calls == 1
    assert process.wait_timeouts == [1.0]
    assert process.kill_calls == 0


@_DARWIN_ONLY
def test_inherited_startup_claim_blocks_complete_marker_aba_after_parent_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from vibecad.runtime import status, uninstall

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    published = tmp_path / "published"
    marker = uninstall.uninstall_marker()
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    done_read, done_write = os.pipe()
    owner_pid = os.fork()
    if owner_pid == 0:
        os.close(ready_read)
        os.close(release_write)
        os.close(done_read)
        try:
            with status.runtime_maintenance_lock(timeout=2) as claim:
                startup_pid = os.fork()
                if startup_pid == 0:
                    try:
                        with status.inherited_runtime_maintenance_claim(
                            claim.inheritable_claim_fd()
                        ):
                            os.write(ready_write, b"R")
                            if os.read(release_read, 1) != b"G":
                                os._exit(2)
                            published.write_bytes(b"published")
                        os.write(done_write, b"D")
                    finally:
                        os._exit(0)
                os._exit(0)
        finally:
            os._exit(1)

    os.close(ready_write)
    os.close(release_read)
    os.close(done_write)
    errors: list[BaseException] = []
    marker_cycle: list[str] = []
    try:
        waited_pid, waited_status = os.waitpid(owner_pid, 0)
        assert waited_pid == owner_pid and os.waitstatus_to_exitcode(waited_status) == 0
        assert os.read(ready_read, 1) == b"R"

        def uninstall_cycle() -> None:
            try:
                with status.runtime_maintenance_lock(
                    timeout=3,
                    poll_interval=0.01,
                ):
                    assert published.read_bytes() == b"published"
                    marker.write_bytes(b"marked")
                    marker_cycle.append("appeared")
                    marker.unlink()
                    marker_cycle.append("cleared")
            except BaseException as error:
                errors.append(error)

        contender = threading.Thread(target=uninstall_cycle)
        contender.start()
        time.sleep(0.1)
        assert contender.is_alive()
        assert not marker.exists()
        assert not published.exists()

        os.write(release_write, b"G")
        assert os.read(done_read, 1) == b"D"
        contender.join(timeout=4)
        assert not contender.is_alive()
        assert errors == []
        assert marker_cycle == ["appeared", "cleared"]
        assert not marker.exists()
    finally:
        for descriptor in (ready_read, release_write, done_read):
            with contextlib.suppress(OSError):
                os.close(descriptor)


@_DARWIN_ONLY
def test_unproved_spawn_stop_defers_lock_release_until_process_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.daemon.bootstrap as daemon_bootstrap
    from vibecad.runtime import status

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(daemon_bootstrap.os, "killpg", lambda _pid, _signal: None)
    child_exit = threading.Event()
    acquired = threading.Event()

    class Process:
        pid = 8_001

        @staticmethod
        def poll():
            return None

        @staticmethod
        def wait(timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("daemon", timeout)
            assert child_exit.wait(3)
            return 0

    with status.runtime_maintenance_lock(timeout=2) as claim:
        daemon_bootstrap._stop_spawned_process(  # noqa: SLF001
            Process(),
            maintenance_claim=claim,
            inherited_claim=True,
        )

    def acquire_next_generation() -> None:
        with status.runtime_maintenance_lock(timeout=3, poll_interval=0.01):
            acquired.set()

    contender = threading.Thread(target=acquire_next_generation)
    contender.start()
    time.sleep(0.1)
    assert not acquired.is_set()
    child_exit.set()
    contender.join(timeout=4)
    assert not contender.is_alive()
    assert acquired.is_set()


def test_retire_helper_passes_one_hard_request_response_deadline(
    tmp_path: Path,
) -> None:
    from vibecad.daemon.bootstrap import retire_local_kernel
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    run_root.mkdir(mode=0o700)
    (run_root / "published").write_bytes(b"state")
    observed: list[float] = []

    class Client:
        daemon_id = "daemon_" + "1" * 32
        daemon_pid = os.getpid()
        closed = False

        @staticmethod
        def retire(*, reason, timeout_seconds):
            assert reason == "runtime_upgrade"
            observed.append(timeout_seconds)
            time.sleep(timeout_seconds + 0.01)
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)

        def close(self):
            self.closed = True

    client = Client()
    started = time.monotonic()
    with pytest.raises(DaemonError) as raised:
        retire_local_kernel(
            reason="runtime_upgrade",
            run_root=run_root,
            timeout_seconds=0.05,
            _connect=lambda _root: client,
        )
    elapsed = time.monotonic() - started

    assert raised.value.code is DaemonErrorCode.UNAVAILABLE
    assert len(observed) == 1 and 0 < observed[0] <= 0.05
    assert elapsed < 0.2
    assert client.closed is True


def test_retire_helper_never_treats_empty_run_root_as_process_completion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.daemon.bootstrap as daemon_bootstrap
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    run_root = tmp_path / "daemon"
    run_root.mkdir(mode=0o700)
    published = run_root / "published"
    published.write_bytes(b"state")
    now = 0.0

    class Client:
        daemon_id = "daemon_" + "2" * 32
        daemon_pid = 4_242

        @staticmethod
        def retire(*, reason, timeout_seconds):
            assert reason == "runtime_upgrade"
            assert timeout_seconds > 0
            published.unlink()
            return V2Response(
                request_id="request_" + "3" * 32,
                sequence=1,
                result={
                    "schema_version": 1,
                    "daemon_id": Client.daemon_id,
                    "status": "retiring",
                },
                error=None,
            )

        @staticmethod
        def close():
            return None

    def clock() -> float:
        return now

    def sleep(duration: float) -> None:
        nonlocal now
        now += duration

    monkeypatch.setattr(daemon_bootstrap, "_process_alive", lambda pid: pid == 4_242)
    with pytest.raises(DaemonError) as raised:
        daemon_bootstrap.retire_local_kernel(
            reason="runtime_upgrade",
            run_root=run_root,
            timeout_seconds=0.05,
            _connect=lambda _root: Client(),
            _clock=clock,
            _sleep=sleep,
        )

    assert raised.value.code is DaemonErrorCode.RECOVERY_REQUIRED
    assert run_root.is_dir() and list(run_root.iterdir()) == []


@_DARWIN_ONLY
@pytest.mark.parametrize("empty_at_entry", [True, False])
def test_retire_helper_requires_unclaimed_authority_when_receipt_is_absent(
    tmp_path: Path,
    empty_at_entry: bool,
) -> None:
    from vibecad.application.data import ApplicationDataLayout
    from vibecad.daemon.bootstrap import retire_local_kernel
    from vibecad.daemon.state import DAEMON_AUTHORITY, DaemonError, DaemonErrorCode
    from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

    data_root = tmp_path / "data"
    layout = ApplicationDataLayout.open(data_root)
    manager = ResourceLeaseManager(
        layout.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    authority = manager.acquire(DAEMON_AUTHORITY)
    run_root = data_root / "daemon"
    connect_calls: list[str] = []
    if not empty_at_entry:
        run_root.mkdir(mode=0o700)
        (run_root / "published").write_bytes(b"state")

    def unavailable(_root):
        connect_calls.append("connect")
        if not empty_at_entry:
            (run_root / "published").unlink()
        raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED)

    try:
        with pytest.raises(DaemonError) as raised:
            retire_local_kernel(
                reason="runtime_upgrade",
                run_root=run_root,
                timeout_seconds=0.05,
                _connect=unavailable,
            )
        assert raised.value.code is DaemonErrorCode.RECOVERY_REQUIRED
        assert connect_calls == ([] if empty_at_entry else ["connect"])
    finally:
        authority.release(owner_token=authority.owner_token)


def test_daemon_bootstrap_custom_root_is_connect_only_for_production_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.daemon.bootstrap as daemon_bootstrap
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    custom_root = tmp_path / "embedded" / "daemon"
    default_data_root = tmp_path / "default-data"
    spawn_calls: list[str] = []

    monkeypatch.setattr(daemon_bootstrap.paths, "data_root", lambda: default_data_root)

    def unavailable(_run_root: object) -> _RecordingKernelClient:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    def forbidden_spawn() -> object:
        spawn_calls.append("spawn")
        raise AssertionError("a custom run root must never start the production daemon")

    monkeypatch.setattr(daemon_bootstrap, "_spawn_daemon", forbidden_spawn)

    with pytest.raises(DaemonError) as raised:
        daemon_bootstrap.connect_or_start_local_kernel(
            run_root=custom_root,
            _connect=unavailable,
        )

    assert raised.value.code is DaemonErrorCode.UNAVAILABLE
    assert spawn_calls == []


def test_uninstall_marker_blocks_daemon_connect_and_spawn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.daemon.bootstrap as daemon_bootstrap
    from vibecad.daemon.state import DaemonError, DaemonErrorCode

    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".uninstall_requested"
    marker.write_bytes(b"")
    marker.chmod(0o600)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    calls: list[str] = []

    def forbidden_connect(_run_root: object) -> _RecordingKernelClient:
        calls.append("connect")
        raise AssertionError("a pending uninstall must block application entry")

    def forbidden_spawn() -> object:
        calls.append("spawn")
        raise AssertionError("a pending uninstall must block daemon startup")

    with pytest.raises(DaemonError) as raised:
        daemon_bootstrap.connect_or_start_local_kernel(
            _connect=forbidden_connect,
            _spawn=forbidden_spawn,
        )

    assert raised.value.code is DaemonErrorCode.RECOVERY_REQUIRED
    assert calls == []


def test_uninstall_marker_blocks_public_workbench_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.daemon.adapters as adapters
    import vibecad.daemon.bootstrap as daemon_bootstrap
    from vibecad.daemon.state import (
        DaemonError,
        DaemonErrorCode,
        daemon_run_root,
    )
    from vibecad.runtime import paths

    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".uninstall_requested"
    marker.write_bytes(b"")
    marker.chmod(0o600)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    calls: list[Path] = []

    def forbidden_connect(_cls, run_root: object, **_kwargs) -> object:
        calls.append(Path(run_root))
        raise AssertionError("a pending uninstall must block Workbench admission")

    monkeypatch.setattr(
        daemon_bootstrap.LocalKernelClient,
        "connect",
        classmethod(forbidden_connect),
    )

    with pytest.raises(DaemonError) as raised:
        adapters.LocalAgentClient.connect(daemon_run_root(paths.data_root()))

    assert raised.value.code is DaemonErrorCode.RECOVERY_REQUIRED
    assert calls == []


def test_public_client_rejects_and_closes_an_incompatible_kernel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.daemon.adapters as adapters
    from vibecad.daemon import LocalAgentClientError, LocalAgentClientErrorCode

    class IncompatibleKernel(_RecordingKernelClient):
        @property
        def daemon_id(self) -> str:
            return "daemon_" + "1" * 32

    kernel = IncompatibleKernel()
    monkeypatch.setattr(
        adapters,
        "connect_existing_local_kernel",
        lambda _root: kernel,
    )

    with pytest.raises(LocalAgentClientError) as raised:
        adapters.LocalAgentClient.connect(tmp_path / "daemon")

    assert raised.value.code is LocalAgentClientErrorCode.INCOMPATIBLE_KERNEL
    assert kernel.closed is True


def test_public_open_retires_one_incompatible_kernel_then_starts_one_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import vibecad.daemon.adapters as adapters

    class Kernel(_RecordingKernelClient):
        def __init__(self, *, compatible: bool, digit: str) -> None:
            super().__init__()
            self._compatible = compatible
            self._daemon_id = "daemon_" + digit * 32

        @property
        def daemon_id(self) -> str:
            return self._daemon_id

        def call(
            self,
            method: object,
            params: object,
            *,
            request_id: object | None = None,
        ) -> V2Response:
            del request_id
            assert method == "kernel.ping"
            assert params == {}
            self.calls.append(("kernel.ping", {}))
            result = {
                "schema_version": 1,
                "daemon_id": self.daemon_id,
                "status": "ready",
                "protocol": {"major": 2, "minor": 0},
            }
            if self._compatible:
                result |= {
                    "api": {
                        "name": adapters.KERNEL_API_NAME,
                        "epoch": adapters.KERNEL_API_EPOCH,
                    },
                    "implementation": {
                        "package_version": adapters.__version__,
                        "build_id": adapters.KERNEL_BUILD_ID,
                    },
                }
            return V2Response(
                request_id="request_" + "a" * 32,
                sequence=1,
                result=result,
                error=None,
            )

    old = Kernel(compatible=False, digit="1")
    new = Kernel(compatible=True, digit="2")
    kernels = iter((old, new))
    retire_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        adapters,
        "connect_or_start_local_kernel",
        lambda: next(kernels),
    )
    monkeypatch.setattr(
        adapters,
        "retire_local_kernel",
        lambda *, reason, expected_daemon_id: (
            retire_calls.append((reason, expected_daemon_id)) or False
        ),
    )
    monkeypatch.setattr(adapters.paths, "data_root", lambda: tmp_path / "data")

    client = adapters.LocalAgentClient.open()

    assert client.daemon_id == new.daemon_id
    assert old.closed is True
    assert new.closed is False
    assert retire_calls == [("incompatible_build", old.daemon_id)]
    assert old.calls == [("kernel.ping", {})]
    assert new.calls == [("kernel.ping", {})]
    client.close()


@_DARWIN_ONLY
def test_retire_helper_stops_detached_daemon_and_next_boot_gets_new_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vibecad.daemon.bootstrap import (
        connect_or_start_local_kernel,
        retire_local_kernel,
    )
    from vibecad.daemon.state import read_boot_state
    from vibecad.runtime import paths

    base = Path(tempfile.mkdtemp(prefix="vc-c13-retire-", dir="/private/tmp"))
    base.chmod(0o700)
    monkeypatch.setenv("VIBECAD_HOME", str(base))
    first = None
    second = None
    run_root = paths.data_root() / "daemon"
    try:
        first = connect_or_start_local_kernel(timeout_seconds=10)
        first_id = first.daemon_id
        assert (
            retire_local_kernel(
                reason="runtime_upgrade",
                expected_daemon_id="daemon_" + "f" * 32,
                timeout_seconds=8,
            )
            is False
        )
        assert first.call("kernel.ping", {}).result["daemon_id"] == first_id
        first.close()
        first = None

        assert retire_local_kernel(
            reason="runtime_upgrade",
            timeout_seconds=8,
        )
        assert run_root.is_dir()
        assert list(run_root.iterdir()) == []

        second = connect_or_start_local_kernel(timeout_seconds=10)
        assert second.daemon_id != first_id
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        with contextlib.suppress(Exception):
            retire_local_kernel(
                reason="runtime_upgrade",
                timeout_seconds=8,
            )
        if run_root.exists() and any(run_root.iterdir()):
            with contextlib.suppress(Exception):
                state = read_boot_state(run_root)
                os.kill(state.receipt.pid, signal.SIGTERM)
        shutil.rmtree(base, ignore_errors=True)


def test_mcp_application_opener_uses_the_public_daemon_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.daemon.adapters as adapters
    import vibecad.server as server

    expected = object()
    monkeypatch.setattr(server, "_enter_application_effect", lambda: True)
    monkeypatch.setattr(
        adapters.LocalAgentClient,
        "open",
        classmethod(lambda _cls: expected),
    )

    assert server._open_agent_application() is expected


@_DARWIN_ONLY
def test_mcp_import_admission_failures_keep_public_envelope_and_never_create(
    daemon_acceptance_rig: _DaemonAcceptanceRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anyio
    from mcp.types import CallToolResult

    import vibecad.server as server

    rig = daemon_acceptance_rig
    source = rig.base / "source.FCStd"
    source.write_bytes(b"fcstd")
    symlink = rig.base / "symlink.FCStd"
    symlink.symlink_to(source)
    hardlink = rig.base / "hardlink.FCStd"
    os.link(source, hardlink)
    fifo = rig.base / "fifo.FCStd"
    os.mkfifo(fifo)
    directory = rig.base / "directory.FCStd"
    directory.mkdir()
    rejected = (
        rig.base / "missing.FCStd",
        symlink,
        hardlink,
        fifo,
        directory,
    )

    class Slot:
        @staticmethod
        def get():
            return rig.first

    monkeypatch.setattr(server, "_application_slot", Slot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    monkeypatch.setattr(server, "_enter_application_effect", lambda: True)
    before = rig.second.list_projects_request({"schema_version": 1})

    for index, path in enumerate(rejected, start=1):
        result = anyio.run(
            server._handle_call_tool,
            "create_project",
            {
                "schema_version": 1,
                "create_key": f"project_create_{index:032x}",
                "kind": "import_fcstd",
                "source_path": str(path),
            },
        )
        assert type(result) is CallToolResult
        assert result.isError is True
        assert result.structuredContent["error"] == {
            "schema_version": 1,
            "code": "invalid_input",
            "path": "",
            "message": "The project request is invalid.",
        }

    after = rig.second.list_projects_request({"schema_version": 1})
    assert after == before


@_DARWIN_ONLY
def test_two_clients_share_one_daemon_task_draft_verdict_and_accepted_head(
    daemon_acceptance_rig: _DaemonAcceptanceRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.server as server
    from vibecad.daemon import (
        LocalAgentClient,
        LocalAgentClientError,
        LocalAgentClientErrorCode,
        LocalKernelState,
    )

    rig = daemon_acceptance_rig
    mcp = _McpRoute(rig.first)
    second = rig.second

    class Slot:
        @staticmethod
        def get():
            return mcp.application

    monkeypatch.setattr(server, "_application_slot", Slot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    monkeypatch.setattr(server, "_enter_application_effect", lambda: True)
    assert rig.first.daemon_id == second.daemon_id == rig.daemon.daemon_id
    assert rig.daemon.state is LocalKernelState.RUNNING
    _wait_for_connections(rig.daemon, 2)

    project_id, task_id = _create_project_and_task(
        mcp,
        project_key_digit="8",
        task_key_digit="9",
    )
    project_request = {"schema_version": 1, "project_id": project_id}
    base_head = mcp.get_project_request(project_request)["result"]["current"]["head"]
    submitted = _submit_review_program(mcp, task_id=task_id)
    task_request = {"schema_version": 1, "task_id": task_id}
    first_view = mcp.get_task_request(task_request)
    second_view = second.get_task_request(task_request)

    assert submitted == first_view == second_view
    assert first_view["ok"] is True
    task = first_view["result"]["task_run"]
    draft = task["draft"]
    report = task["verification_reports"][0]
    assert task["status"] == "awaiting_user_review"
    assert draft["id"] == "draft_" + draft["revision_id"].removeprefix("revision_")
    assert draft["manifest_sha256"] == report["manifest_sha256"]
    assert draft["observation_digest"] == report["observation_digest"]
    assert report["passed"] is True
    assert report["verdicts"][0]["criterion_id"] == "body-volume"
    assert report["verdicts"][0]["outcome"] == "pass"
    assert [item["name"] for item in task["artifacts"]] == [
        "model.FCStd",
        "model.step",
    ]
    assert all(item["candidate_revision"] == draft["revision_id"] for item in task["artifacts"])

    first_head = mcp.get_project_request(project_request)
    second_head = second.get_project_request(project_request)
    assert first_head == second_head
    assert first_head["result"]["current"]["head"] == base_head

    opened = rig.first.open_checkout(
        open_key="checkout_open_" + "a" * 32,
        source={
            "kind": "draft",
            "task_id": task_id,
            "draft_id": draft["id"],
            "expected_generation": first_view["result"]["generation"],
        },
    )
    grant_id = opened["file_grant"]["grant_id"]
    with pytest.raises(LocalAgentClientError) as cross_session:
        second.claim_file_grant(grant_id=grant_id)
    assert cross_session.value.code is LocalAgentClientErrorCode.UNAVAILABLE
    claimed = rig.first.claim_file_grant(grant_id=grant_id)
    assert Path(claimed["local_path"]).is_file()
    assert claimed["current_model_sha256"] == task["artifacts"][0]["sha256"]
    assert claimed["current_size_bytes"] == task["artifacts"][0]["size_bytes"]
    workbench_opened = second.open_checkout(
        open_key="checkout_open_" + "b" * 32,
        source={
            "kind": "draft",
            "task_id": task_id,
            "draft_id": draft["id"],
            "expected_generation": first_view["result"]["generation"],
        },
    )
    workbench_claimed = second.claim_file_grant(grant_id=workbench_opened["file_grant"]["grant_id"])
    assert Path(workbench_claimed["local_path"]).is_file()
    assert workbench_claimed["current_model_sha256"] == task["artifacts"][0]["sha256"]
    assert workbench_claimed["current_size_bytes"] == task["artifacts"][0]["size_bytes"]

    rig.first.close()
    _wait_for_connections(rig.daemon, 1)
    assert rig.daemon.state is LocalKernelState.RUNNING
    reconnected = LocalAgentClient.connect(
        rig.daemon.run_root,
        artifact_root=rig.base / "data" / "artifacts",
    )
    try:
        mcp.application = reconnected
        assert reconnected.daemon_id == second.daemon_id
        assert mcp.get_task_request(task_request) == second_view

        accepted = mcp.accept_draft_request(
            {
                "schema_version": 1,
                "task_id": task_id,
                "draft_id": draft["id"],
                "expected_generation": first_view["result"]["generation"],
            }
        )
        observed = second.get_task_request(task_request)
        assert accepted == observed
        assert accepted["ok"] is True
        assert accepted["result"]["task_run"]["status"] == "succeeded"
        assert accepted["result"]["task_run"]["committed_revision"] == draft["revision_id"]

        reconnected_head = mcp.get_project_request(project_request)
        second_head = second.get_project_request(project_request)
        assert reconnected_head == second_head
        assert (
            reconnected_head["result"]["current"]["head"]["revision_id"] == (draft["revision_id"])
        )
        assert (
            reconnected_head["result"]["current"]["head"]["manifest_sha256"]
            == (draft["manifest_sha256"])
        )
        assert rig.daemon.state is LocalKernelState.RUNNING
    finally:
        reconnected.close()


@_DARWIN_ONLY
def test_two_clients_share_rejected_draft_while_head_remains_unchanged(
    daemon_acceptance_rig: _DaemonAcceptanceRig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.server as server
    from vibecad.daemon import LocalKernelState

    rig = daemon_acceptance_rig
    mcp = _McpRoute(rig.first)

    class Slot:
        @staticmethod
        def get():
            return mcp.application

    monkeypatch.setattr(server, "_application_slot", Slot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    monkeypatch.setattr(server, "_enter_application_effect", lambda: True)
    project_id, task_id = _create_project_and_task(
        mcp,
        project_key_digit="b",
        task_key_digit="c",
    )
    project_request = {"schema_version": 1, "project_id": project_id}
    base_head = mcp.get_project_request(project_request)["result"]["current"]["head"]
    submitted = _submit_review_program(mcp, task_id=task_id)
    task_request = {"schema_version": 1, "task_id": task_id}
    first_view = mcp.get_task_request(task_request)
    assert submitted == first_view == rig.second.get_task_request(task_request)
    draft = first_view["result"]["task_run"]["draft"]

    rejected = mcp.reject_draft_request(
        {
            "schema_version": 1,
            "task_id": task_id,
            "draft_id": draft["id"],
            "expected_generation": first_view["result"]["generation"],
        }
    )
    observed = rig.second.get_task_request(task_request)
    assert rejected == observed
    assert rejected["ok"] is True
    rejected_task = rejected["result"]["task_run"]
    assert rejected_task["status"] == "rejected"
    assert rejected_task["draft"] == draft
    assert rejected_task["committed_revision"] is None
    assert (
        rejected_task["verification_reports"]
        == (first_view["result"]["task_run"]["verification_reports"])
    )
    assert rejected_task["artifacts"] == first_view["result"]["task_run"]["artifacts"]

    first_head = mcp.get_project_request(project_request)
    second_head = rig.second.get_project_request(project_request)
    assert first_head == second_head
    assert first_head["result"]["current"]["head"] == base_head
    assert first_head["result"]["current"]["head"]["revision_id"] != draft["revision_id"]
    assert rig.daemon.state is LocalKernelState.RUNNING
