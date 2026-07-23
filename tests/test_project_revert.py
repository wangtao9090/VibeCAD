"""Cross-layer contracts for replay-safe verified forward revert."""

from __future__ import annotations

from pathlib import Path

import pytest

from vibecad.application.agent import AgentApplication
from vibecad.execution.revisions import (
    ProjectHead,
    RevisionArtifactRef,
    RevisionRef,
)
from vibecad.workflow.catalog import TaskCatalogError, TaskCatalogErrorCode
from vibecad.workflow.contracts import ModelProgram
from vibecad.workflow.revert import (
    RevertProgramError,
    RevertProgramErrorCode,
    build_revert_binding,
    parse_bound_revert_task,
    require_matching_revert_task,
    revert_task_identity,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    new_task_run,
    transition_task,
)

REVERT_KEY = "revert_create_" + "a" * 32
OTHER_REVERT_KEY = "revert_create_" + "b" * 32
SOURCE_REVISION = "revision_" + "1" * 32
OTHER_SOURCE_REVISION = "revision_" + "2" * 32
SOURCE_BASE = "revision_" + "3" * 32
MANIFEST = "4" * 64
MODEL_DIGEST = "5" * 64
STEP_DIGEST = "6" * 64


def _data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


def _source(project_id: str, revision_id: str = SOURCE_REVISION) -> RevisionRef:
    return RevisionRef(
        id=revision_id,
        project_id=project_id,
        base_revision=SOURCE_BASE,
        manifest_sha256=MANIFEST,
        model=RevisionArtifactRef(
            id="artifact_" + "7" * 32,
            name="model.FCStd",
            format="fcstd",
            sha256=MODEL_DIGEST,
            size_bytes=101,
        ),
        artifacts=(
            RevisionArtifactRef(
                id="artifact_" + "8" * 32,
                name="model.step",
                format="step",
                sha256=STEP_DIGEST,
                size_bytes=202,
            ),
        ),
    )


def _bound_task(project_id: str, head: ProjectHead):
    binding = build_revert_binding(
        revert_key=REVERT_KEY,
        project_id=project_id,
        source_revision=_source(project_id),
        expected_head=head,
    )
    task = new_task_run(
        task_id=binding.task_id,
        project_id=project_id,
        base_revision=head.revision_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
        creation_digest=binding.creation_digest,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=binding.program)
    return binding, task


def _request(project_id: str, expected_head: str, **changes) -> dict[str, object]:
    request: dict[str, object] = {
        "schema_version": 1,
        "revert_key": REVERT_KEY,
        "project_id": project_id,
        "source_revision": SOURCE_REVISION,
        "expected_head": expected_head,
    }
    request.update(changes)
    return request


def test_revert_task_identity_is_key_stable_while_exact_intent_is_conflict_bound() -> None:
    project_id = "project_" + "9" * 32
    head = ProjectHead(
        project_id=project_id,
        generation=7,
        revision_id="revision_" + "a" * 32,
        manifest_sha256="b" * 64,
    )
    binding, task = _bound_task(project_id, head)

    assert revert_task_identity(REVERT_KEY) == (
        binding.task_id,
        binding.creation_digest,
    )
    assert revert_task_identity(OTHER_REVERT_KEY)[0] != binding.task_id
    assert (
        require_matching_revert_task(
            task,
            revert_key=REVERT_KEY,
            project_id=project_id,
            source_revision=SOURCE_REVISION,
            expected_head=head.revision_id,
        )
        == binding
    )

    for changed in (
        {"project_id": "project_" + "c" * 32},
        {"source_revision": OTHER_SOURCE_REVISION},
        {"expected_head": "revision_" + "d" * 32},
    ):
        values = {
            "revert_key": REVERT_KEY,
            "project_id": project_id,
            "source_revision": SOURCE_REVISION,
            "expected_head": head.revision_id,
            **changed,
        }
        try:
            require_matching_revert_task(task, **values)
        except RevertProgramError as error:
            assert error.code is RevertProgramErrorCode.CONFLICT
        else:  # pragma: no cover - makes a security regression explicit
            raise AssertionError("changed immutable revert intent must conflict")


def test_reserved_revert_program_cannot_authorize_an_ordinary_task_identity() -> None:
    project_id = "project_" + "9" * 32
    head = ProjectHead(
        project_id=project_id,
        generation=7,
        revision_id="revision_" + "a" * 32,
        manifest_sha256="b" * 64,
    )
    binding, _task = _bound_task(project_id, head)
    forged = new_task_run(
        task_id="task_" + "f" * 32,
        project_id=project_id,
        base_revision=head.revision_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
        creation_digest="f" * 64,
    )
    forged = transition_task(forged, TaskEvent.REQUEST_PLAN)
    forged_program = ModelProgram(
        task_id=forged.id,
        base_revision=binding.program.base_revision,
        operations=binding.program.operations,
        acceptance=binding.program.acceptance,
    )
    forged = transition_task(
        forged,
        TaskEvent.SUBMIT_PROGRAM,
        program=forged_program,
    )

    assert parse_bound_revert_task(forged) is None
    with pytest.raises(RevertProgramError) as caught:
        require_matching_revert_task(
            forged,
            revert_key=REVERT_KEY,
            project_id=project_id,
            source_revision=SOURCE_REVISION,
            expected_head=head.revision_id,
        )
    assert caught.value.code is RevertProgramErrorCode.CONFLICT


def test_application_replays_bound_revert_before_constructing_a_cad_runtime(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def forbidden_runtime(**_kwargs):
        calls.append("runtime")
        raise AssertionError("durable revert replay must not construct CAD")

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=forbidden_runtime,
    )
    project = app.bootstrap_empty()
    expected = ProjectHead(
        project_id=project.head.project_id,
        generation=1,
        revision_id="revision_" + "e" * 32,
        manifest_sha256="f" * 64,
    )
    binding, task = _bound_task(project.head.project_id, expected)
    stored = app._task_store.create(task)  # noqa: SLF001

    replayed = app.revert_project_request(_request(project.head.project_id, expected.revision_id))
    changed = app.revert_project_request(
        _request(
            project.head.project_id,
            expected.revision_id,
            source_revision=OTHER_SOURCE_REVISION,
        )
    )

    assert replayed["ok"] is True
    assert replayed["result"]["generation"] == stored.generation
    assert replayed["result"]["task_run"]["id"] == binding.task_id
    assert replayed["result"]["task_run"]["status"] == "program_ready"
    assert changed["ok"] is False
    assert changed["error"]["code"] == "conflict"
    assert calls == []
    assert app._runtimes == {}  # noqa: SLF001
    app.close()


def test_application_rechecks_revert_replay_after_waiting_for_the_cad_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_calls: list[str] = []

    def forbidden_runtime(**_kwargs):
        runtime_calls.append("runtime")
        raise AssertionError("a raced durable replay must not construct CAD")

    app = AgentApplication.open(
        data_root=_data_root(tmp_path),
        runtime_factory=forbidden_runtime,
    )
    project = app.bootstrap_empty()
    expected = ProjectHead(
        project_id=project.head.project_id,
        generation=1,
        revision_id="revision_" + "e" * 32,
        manifest_sha256="f" * 64,
    )
    binding, task = _bound_task(project.head.project_id, expected)
    stored = app._task_store.create(task)  # noqa: SLF001
    catalog_type = type(app._catalog)  # noqa: SLF001
    original_get = catalog_type.get_task
    reads = 0

    def raced_get(catalog, *, task_id: str):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise TaskCatalogError(TaskCatalogErrorCode.NOT_FOUND)
        return original_get(catalog, task_id=task_id)

    monkeypatch.setattr(catalog_type, "get_task", raced_get)

    replayed = app.revert_project_request(_request(project.head.project_id, expected.revision_id))

    assert replayed["ok"] is True
    assert replayed["result"]["generation"] == stored.generation
    assert replayed["result"]["task_run"]["id"] == binding.task_id
    assert reads == 2
    assert runtime_calls == []
    assert app._runtimes == {}  # noqa: SLF001
    app.close()
