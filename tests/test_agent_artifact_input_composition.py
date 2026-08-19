"""Application composition tests for reviewed CAD task-input authority."""

from __future__ import annotations

from pathlib import Path

import pytest

import vibecad.application.agent as agent_module
import vibecad.execution.worker_port as worker_port_module
from vibecad.application.agent import AgentApplication
from vibecad.execution.candidate import ActiveCandidate, SessionBinding
from vibecad.execution.errors import ExecutorError, ExecutorErrorCode
from vibecad.execution.revisions import ProjectHead
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.program import ValidatedProgram, validate_model_program

_PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
_BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
_CANDIDATE_REVISION = "revision_11111111111111111111111111111111"


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self, **kwargs):
        del kwargs
        self.calls += 1
        raise AssertionError("provider must not be called")


class _Preflight:
    def __init__(self, required: bool) -> None:
        self.required = required
        self.programs: list[ValidatedProgram] = []

    def requires_artifact_snapshot(self, program: ValidatedProgram) -> bool:
        self.programs.append(program)
        return self.required


class _Worker:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.close_calls = 0

    def execute_program(self, *, program, candidate, session):
        del program, candidate, session
        self.execute_calls += 1
        return ()

    def close(self) -> None:
        self.close_calls += 1


def _data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _program() -> ValidatedProgram:
    return validate_model_program(
        ModelProgram(
            task_id="task_artifact_composition",
            base_revision=_BASE_REVISION,
            operations=(
                ModelCommand(
                    id="inspect",
                    op="inspect_model",
                    target={},
                    args={},
                    source=ValueSource.MODEL,
                ),
            ),
            acceptance=AcceptanceSpec(id="acceptance_artifact_composition", criteria=()),
        )
    )


def _candidate(tmp_path: Path) -> ActiveCandidate:
    return ActiveCandidate(
        project_id=_PROJECT_ID,
        base_head=ProjectHead(
            project_id=_PROJECT_ID,
            generation=1,
            revision_id=_BASE_REVISION,
            manifest_sha256="a" * 64,
        ),
        binding=SessionBinding(
            project_id=_PROJECT_ID,
            revision_id=_CANDIDATE_REVISION,
            session=object(),
        ),
        model_path=tmp_path / "candidate.FCStd",
        step_path=tmp_path / "candidate.step",
    )


def _install_worker(port, worker: _Worker, candidate: ActiveCandidate) -> None:
    state = worker_port_module._Capability(  # noqa: SLF001
        kind="candidate",
        key=(candidate.project_id, candidate.binding.revision_id),
        value=object(),
        base_head=candidate.base_head,
    )
    state.sessions.add(candidate.binding.session)
    port._worker = worker  # noqa: SLF001
    port._sessions[candidate.binding.session] = state  # noqa: SLF001


def test_private_composition_passes_exact_provider_and_preflight_identities(
    tmp_path: Path,
) -> None:
    provider = _Provider()
    preflight = _Preflight(False)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=agent_module._task_input_cad_port_factory(  # noqa: SLF001
            task_input_snapshot_provider=provider,
            task_input_preflight=preflight,
        ),
    )
    with app._cad_gate:  # noqa: SLF001
        port = app._cad_execution_port_under_gate()  # noqa: SLF001

    assert port._task_input_snapshot_provider is provider  # noqa: SLF001
    assert port._task_input_preflight is preflight  # noqa: SLF001
    assert provider.calls == 0
    app.close()


def test_default_application_composition_uses_host_reviewed_input_authority(
    tmp_path: Path,
) -> None:
    assert (
        agent_module._task_input_cad_port_factory()  # noqa: SLF001
        is agent_module._default_cad_port_factory  # noqa: SLF001
    )
    app = AgentApplication.open(data_root=_data_root(tmp_path))
    with app._cad_gate:  # noqa: SLF001
        port = app._cad_execution_port_under_gate()  # noqa: SLF001

    assert port._task_input_snapshot_provider is app._reviewed_inputs  # noqa: SLF001
    assert port._task_input_preflight is app._reviewed_inputs  # noqa: SLF001
    app.close()


def test_composed_provider_is_not_called_for_nonartifact_program(tmp_path: Path) -> None:
    provider = _Provider()
    preflight = _Preflight(False)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=agent_module._task_input_cad_port_factory(  # noqa: SLF001
            task_input_snapshot_provider=provider,
            task_input_preflight=preflight,
        ),
    )
    with app._cad_gate:  # noqa: SLF001
        port = app._cad_execution_port_under_gate()  # noqa: SLF001
    candidate = _candidate(tmp_path)
    worker = _Worker()
    _install_worker(port, worker, candidate)
    program = _program()

    assert port.execute_program(program=program, candidate=candidate) == ()
    assert preflight.programs == [program]
    assert provider.calls == 0
    assert worker.execute_calls == 1
    app.close()
    assert worker.close_calls == 1


def test_composed_artifact_preflight_without_provider_fails_closed(tmp_path: Path) -> None:
    preflight = _Preflight(True)
    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        cad_port_factory=agent_module._task_input_cad_port_factory(  # noqa: SLF001
            task_input_preflight=preflight,
        ),
    )
    with app._cad_gate:  # noqa: SLF001
        port = app._cad_execution_port_under_gate()  # noqa: SLF001
    candidate = _candidate(tmp_path)
    worker = _Worker()
    _install_worker(port, worker, candidate)

    with pytest.raises(ExecutorError) as caught:
        port.execute_program(program=_program(), candidate=candidate)
    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert worker.execute_calls == 0
    app.close()
    assert worker.close_calls == 1
