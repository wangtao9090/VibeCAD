"""Contracts for a user-origin, exact-file manual checkpoint task."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vibecad.execution.revisions import ProjectHead
from vibecad.workflow.contracts import ModelProgram, ValueSource
from vibecad.workflow.manual_checkpoint import (
    ManualCheckpointError,
    ManualCheckpointErrorCode,
    build_manual_checkpoint_binding,
    manual_checkpoint_task_identity,
    parse_bound_manual_checkpoint_task,
    require_matching_manual_checkpoint_task,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    new_task_run,
    transition_task,
)

CHECKPOINT_KEY = "checkpoint_create_" + "a" * 32
CHECKOUT_ID = "checkout_" + "b" * 32
PROJECT_ID = "project_" + "c" * 32
MODEL_DIGEST = "d" * 64


def _head() -> ProjectHead:
    return ProjectHead(
        project_id=PROJECT_ID,
        generation=7,
        revision_id="revision_" + "e" * 32,
        manifest_sha256="f" * 64,
    )


def _bound_task():
    binding = build_manual_checkpoint_binding(
        checkpoint_key=CHECKPOINT_KEY,
        checkout_id=CHECKOUT_ID,
        project_id=PROJECT_ID,
        expected_head=_head(),
        model_sha256=MODEL_DIGEST,
        model_size_bytes=4096,
    )
    task = new_task_run(
        task_id=binding.task_id,
        project_id=PROJECT_ID,
        base_revision=binding.expected_head.revision_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
        creation_digest=binding.creation_digest,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=binding.program)
    return binding, task


def test_manual_checkpoint_binds_user_provenance_and_exact_file_intent() -> None:
    binding, task = _bound_task()

    assert manual_checkpoint_task_identity(CHECKPOINT_KEY) == (
        binding.task_id,
        binding.creation_digest,
    )
    assert binding.program.operations[0].source is ValueSource.USER
    assert binding.program.operations[0].op == "system.checkpoint_checkout"
    assert binding.program.acceptance.criteria[1].check == "solid_count"
    assert binding.program.acceptance.criteria[1].expected == 1
    assert parse_bound_manual_checkpoint_task(task) == binding
    assert (
        require_matching_manual_checkpoint_task(
            task,
            checkpoint_key=CHECKPOINT_KEY,
            checkout_id=CHECKOUT_ID,
            project_id=PROJECT_ID,
            expected_head=_head().revision_id,
            model_sha256=MODEL_DIGEST,
            model_size_bytes=4096,
        )
        == binding
    )


@pytest.mark.parametrize(
    "change",
    (
        {"checkout_id": "checkout_" + "1" * 32},
        {"project_id": "project_" + "2" * 32},
        {"expected_head": "revision_" + "3" * 32},
        {"model_sha256": "4" * 64},
        {"model_size_bytes": 4097},
    ),
)
def test_manual_checkpoint_replay_rejects_any_changed_immutable_intent(change) -> None:
    _binding, task = _bound_task()
    request = {
        "checkpoint_key": CHECKPOINT_KEY,
        "checkout_id": CHECKOUT_ID,
        "project_id": PROJECT_ID,
        "expected_head": _head().revision_id,
        "model_sha256": MODEL_DIGEST,
        "model_size_bytes": 4096,
        **change,
    }
    with pytest.raises(ManualCheckpointError) as caught:
        require_matching_manual_checkpoint_task(task, **request)
    assert caught.value.code is ManualCheckpointErrorCode.CONFLICT


def test_reserved_manual_program_cannot_authorize_forged_task_or_policy() -> None:
    binding, task = _bound_task()
    forged = new_task_run(
        task_id="task_" + "5" * 32,
        project_id=PROJECT_ID,
        base_revision=_head().revision_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
        creation_digest="5" * 32 + "6" * 32,
    )
    forged = transition_task(forged, TaskEvent.REQUEST_PLAN)
    forged = transition_task(
        forged,
        TaskEvent.SUBMIT_PROGRAM,
        program=ModelProgram(
            task_id=forged.id,
            base_revision=binding.program.base_revision,
            operations=binding.program.operations,
            acceptance=binding.program.acceptance,
        ),
    )

    assert parse_bound_manual_checkpoint_task(forged) is None
    assert (
        parse_bound_manual_checkpoint_task(replace(task, review_policy=ReviewPolicy.REQUIRE_REVIEW))
        is None
    )
