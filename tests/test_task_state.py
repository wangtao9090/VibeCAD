"""Pure schema-v1 TaskRun state-contract tests."""

from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

import vibecad.workflow.state as state_module
from vibecad.workflow.contracts import (
    AcceptanceSpec,
    ErrorCategory,
    ModelProgram,
    StepError,
    StepResult,
)
from vibecad.workflow.state import (
    MAX_ARTIFACT_REFS,
    MAX_CRITERION_VERDICTS,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    MAX_STEP_RECORDS,
    MAX_TRANSITION_RECORDS,
    MAX_VERDICT_EVIDENCE,
    MAX_VERIFICATION_REPORTS,
    CriterionVerdict,
    NextAction,
    ReasoningOwner,
    ReviewPolicy,
    TaskArtifactRef,
    TaskEvent,
    TaskRun,
    TaskStateError,
    TaskStateErrorCode,
    TaskStatus,
    TaskStepRecord,
    TaskTransitionRecord,
    VerificationReport,
    append_step_result,
    append_verification,
    new_task_run,
    next_action_for,
    task_creation_identity,
    transition_task,
)

TASK_ID = "task_0123456789abcdef0123456789abcdef"
KEYED_TASK_ID = "task_e9f9dc52c8f75cd72feddee2648564b8"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
CANDIDATE_REVISION = "revision_11111111111111111111111111111111"
COMMITTED_REVISION = CANDIDATE_REVISION
OTHER_REVISION = "revision_22222222222222222222222222222222"
CREATION_DIGEST = "e9f9dc52c8f75cd72feddee2648564b8b4bf0b07836368165d3a0c1fedeee1ef"


def _error(*, needs_input: bool = False) -> StepError:
    return StepError(
        category=ErrorCategory.RUNTIME,
        code="injected_failure",
        message="The injected operation failed",
        retryable=False,
        needs_input=needs_input,
        related_objects=(),
        diagnostic_artifacts=(),
    )


def _program() -> ModelProgram:
    return ModelProgram(
        task_id=TASK_ID,
        base_revision=BASE_REVISION,
        operations=(),
        acceptance=AcceptanceSpec(id="acceptance-1", criteria=()),
    )


def _report(*, passed: bool) -> VerificationReport:
    outcome = state_module.CriterionOutcome.PASS if passed else state_module.CriterionOutcome.FAIL
    return VerificationReport(
        id="verification_0123456789abcdef0123456789abcdef",
        acceptance_id="acceptance-1",
        candidate_revision=CANDIDATE_REVISION,
        manifest_sha256="a" * 64,
        observation_digest="b" * 64,
        passed=passed,
        verdicts=(
            CriterionVerdict(
                criterion_id="volume",
                required=True,
                outcome=outcome,
                message="Volume matched" if passed else "Volume did not match",
            ),
        ),
    )


def _task() -> TaskRun:
    return new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )


def test_task_creation_digest_is_durable_while_legacy_records_remain_readable():
    keyed = new_task_run(
        task_id=KEYED_TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
        creation_digest=CREATION_DIGEST,
    )

    assert keyed.creation_digest == CREATION_DIGEST
    assert keyed.to_mapping()["creation_digest"] == CREATION_DIGEST
    assert TaskRun.from_mapping(keyed.to_mapping()) == keyed

    legacy = _task().to_mapping()
    legacy.pop("creation_digest", None)
    restored = TaskRun.from_mapping(legacy)
    assert restored.creation_digest is None
    assert restored.to_mapping()["creation_digest"] is None
    assert TaskRun.from_mapping(_task().to_mapping()) == _task()


def test_task_creation_identity_is_frozen_and_digest_must_bind_the_task_id():
    assert task_creation_identity("task_create_0123456789abcdef0123456789abcdef") == (
        KEYED_TASK_ID,
        CREATION_DIGEST,
    )

    with pytest.raises(TaskStateError) as caught:
        new_task_run(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
            creation_digest=CREATION_DIGEST,
        )

    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/creation_digest",
    )


def _to_executing() -> TaskRun:
    task = transition_task(_task(), TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=_program())
    task = transition_task(task, TaskEvent.START_VALIDATION)
    return transition_task(
        task,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=CANDIDATE_REVISION,
    )


def _to_verifying() -> TaskRun:
    return transition_task(_to_executing(), TaskEvent.COMPLETE_EXECUTION)


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ((TaskEvent.REQUEST_PLAN,), TaskStatus.NEEDS_PLAN),
        (
            (TaskEvent.REQUEST_PLAN, TaskEvent.SUBMIT_PROGRAM),
            TaskStatus.PROGRAM_READY,
        ),
        (
            (TaskEvent.REQUEST_PLAN, TaskEvent.SUBMIT_PROGRAM, TaskEvent.START_VALIDATION),
            TaskStatus.VALIDATING_PROGRAM,
        ),
        (
            (
                TaskEvent.REQUEST_PLAN,
                TaskEvent.SUBMIT_PROGRAM,
                TaskEvent.START_VALIDATION,
                TaskEvent.VALIDATE_PROGRAM,
            ),
            TaskStatus.EXECUTING,
        ),
    ],
)
def test_pre_candidate_transition_paths(events, expected):
    task = _task()
    for event in events:
        kwargs = {"program": _program()} if event is TaskEvent.SUBMIT_PROGRAM else {}
        if event is TaskEvent.VALIDATE_PROGRAM:
            kwargs["candidate_revision"] = CANDIDATE_REVISION
        task = transition_task(task, event, **kwargs)

    assert task.status is expected
    assert [record.sequence for record in task.transitions] == list(range(1, len(events) + 1))


@pytest.mark.parametrize(
    ("events", "expected", "needs_input"),
    [
        (
            (TaskEvent.COMPLETE_EXECUTION, TaskEvent.PASS_VERIFICATION, TaskEvent.COMMIT),
            TaskStatus.SUCCEEDED,
            False,
        ),
        ((TaskEvent.FAIL_EXECUTION, TaskEvent.COMPLETE_ROLLBACK), TaskStatus.FAILED, False),
        ((TaskEvent.FAIL_VERIFICATION, TaskEvent.COMPLETE_ROLLBACK), TaskStatus.FAILED, False),
        ((TaskEvent.REQUIRE_RECOVERY,), TaskStatus.RECOVERY_REQUIRED, False),
        ((TaskEvent.REQUIRE_CLEANUP,), TaskStatus.CLEANUP_REQUIRED, False),
    ],
)
def test_candidate_transition_paths(events, expected, needs_input):
    task = _to_executing()
    for event in events:
        if event is TaskEvent.FAIL_VERIFICATION and task.status is TaskStatus.EXECUTING:
            task = transition_task(task, TaskEvent.COMPLETE_EXECUTION)
        kwargs = {}
        if event is TaskEvent.PASS_VERIFICATION:
            kwargs["verification"] = _report(passed=True)
        if event in {
            TaskEvent.FAIL_EXECUTION,
            TaskEvent.FAIL_VERIFICATION,
            TaskEvent.REQUIRE_RECOVERY,
            TaskEvent.REQUIRE_CLEANUP,
        }:
            kwargs["error"] = _error(needs_input=needs_input)
        if event is TaskEvent.COMMIT:
            kwargs["committed_revision"] = COMMITTED_REVISION
        task = transition_task(task, event, **kwargs)

    assert task.status is expected
    if expected is TaskStatus.SUCCEEDED:
        assert task.committed_revision == COMMITTED_REVISION
    if expected is TaskStatus.FAILED:
        assert task.last_error == _error(needs_input=needs_input)


def test_validation_rejection_can_needs_input_without_a_candidate():
    task = transition_task(_task(), TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=_program())
    task = transition_task(task, TaskEvent.START_VALIDATION)

    result = transition_task(task, TaskEvent.REJECT_PROGRAM, error=_error(needs_input=True))

    assert result.status is TaskStatus.NEEDS_INPUT
    assert result.candidate_revision is None


def test_terminal_and_wrong_source_transitions_are_rejected_stably():
    with pytest.raises(TaskStateError) as caught:
        transition_task(_task(), TaskEvent.COMMIT, committed_revision=COMMITTED_REVISION)
    assert caught.value.code is TaskStateErrorCode.INVALID_TRANSITION
    assert caught.value.path == "/event"

    task = _to_verifying()
    task = transition_task(task, TaskEvent.PASS_VERIFICATION, verification=_report(passed=True))
    task = transition_task(task, TaskEvent.COMMIT, committed_revision=COMMITTED_REVISION)
    with pytest.raises(TaskStateError) as caught:
        transition_task(task, TaskEvent.REQUEST_PLAN)
    assert caught.value.code is TaskStateErrorCode.TERMINAL_STATE


def test_candidate_states_and_terminal_invariants_require_consistent_revisions_and_evidence():
    validating = transition_task(
        transition_task(
            transition_task(_task(), TaskEvent.REQUEST_PLAN),
            TaskEvent.SUBMIT_PROGRAM,
            program=_program(),
        ),
        TaskEvent.START_VALIDATION,
    )
    with pytest.raises(TaskStateError) as caught:
        transition_task(validating, TaskEvent.VALIDATE_PROGRAM)
    assert caught.value.code is TaskStateErrorCode.INVALID_IDENTIFIER

    verifying = _to_verifying()
    with pytest.raises(TaskStateError) as caught:
        transition_task(verifying, TaskEvent.PASS_VERIFICATION, verification=_report(passed=False))
    assert caught.value.code is TaskStateErrorCode.INVARIANT_VIOLATION

    with pytest.raises(TaskStateError) as caught:
        transition_task(verifying, TaskEvent.FAIL_VERIFICATION)
    assert caught.value.code is TaskStateErrorCode.MISSING_ERROR


def test_program_must_bind_the_task_and_base_revision():
    task = transition_task(_task(), TaskEvent.REQUEST_PLAN)
    wrong_task = ModelProgram(
        task_id="task_ffffffffffffffffffffffffffffffff",
        base_revision=BASE_REVISION,
        operations=(),
        acceptance=AcceptanceSpec(id="acceptance-1", criteria=()),
    )

    with pytest.raises(TaskStateError) as caught:
        transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=wrong_task)
    assert caught.value.code is TaskStateErrorCode.PROGRAM_MISMATCH
    assert caught.value.path == "/program/task_id"


def test_task_run_round_trip_is_strict_and_defensively_copied():
    task = _to_executing()
    task = append_step_result(
        task,
        StepResult(
            ok=True,
            value={"shape": [1, 2]},
            elapsed_ms=1,
            revision=CANDIDATE_REVISION,
        ),
    )
    task = transition_task(task, TaskEvent.COMPLETE_EXECUTION)
    task = append_verification(task, _report(passed=True))
    encoded = task.to_mapping()
    restored = TaskRun.from_mapping(encoded)

    assert restored == task
    encoded["steps"][0]["result"]["value"]["shape"].append(3)
    assert task.steps[0].result.to_mapping()["value"] == {"shape": [1, 2]}
    with pytest.raises(FrozenInstanceError):
        task.status = TaskStatus.FAILED

    malformed = deepcopy(task.to_mapping())
    malformed["unknown"] = True
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(malformed)
    assert caught.value.code is TaskStateErrorCode.UNKNOWN_FIELD


def test_canonical_identifiers_and_sequence_integrity_are_enforced():
    with pytest.raises(TaskStateError) as caught:
        new_task_run(
            task_id="../task_0123456789abcdef0123456789abcdef",
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
    assert caught.value.code is TaskStateErrorCode.INVALID_IDENTIFIER

    malformed = _to_executing().to_mapping()
    malformed["transitions"][0]["sequence"] = 2
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(malformed)
    assert caught.value.code is TaskStateErrorCode.INVARIANT_VIOLATION


def test_bounded_histories_and_duplicate_ids_are_rejected():
    task = _to_executing()
    step = StepResult(ok=True, value=None, elapsed_ms=0, revision=CANDIDATE_REVISION)
    for _ in range(MAX_STEP_RECORDS):
        task = append_step_result(task, step)
    with pytest.raises(TaskStateError) as caught:
        append_step_result(task, step)
    assert caught.value.code is TaskStateErrorCode.BUDGET_EXCEEDED

    assert MAX_TRANSITION_RECORDS >= 8
    assert MAX_VERIFICATION_REPORTS >= 1
    assert MAX_ARTIFACT_REFS >= 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.CREATED, NextAction.REQUEST_PLAN),
        (TaskStatus.NEEDS_PLAN, NextAction.SUBMIT_PROGRAM),
        (TaskStatus.PROGRAM_READY, NextAction.VALIDATE_PROGRAM),
        (TaskStatus.VALIDATING_PROGRAM, NextAction.WAIT),
        (TaskStatus.EXECUTING, NextAction.WAIT),
        (TaskStatus.VERIFYING, NextAction.WAIT),
        (TaskStatus.COMMITTING, NextAction.WAIT),
        (TaskStatus.ROLLING_BACK, NextAction.WAIT),
        (TaskStatus.NEEDS_INPUT, NextAction.PROVIDE_INPUT),
        (TaskStatus.RECOVERY_REQUIRED, NextAction.RECONCILE),
        (TaskStatus.CLEANUP_REQUIRED, NextAction.CLEANUP),
        (TaskStatus.SUCCEEDED, NextAction.NONE),
        (TaskStatus.FAILED, NextAction.NONE),
    ],
)
def test_next_actions_are_deterministic(status, expected):
    assert next_action_for(status) is expected


def test_required_candidate_and_committed_identifiers_take_identifier_precedence():
    validating = transition_task(
        transition_task(
            transition_task(_task(), TaskEvent.REQUEST_PLAN),
            TaskEvent.SUBMIT_PROGRAM,
            program=_program(),
        ),
        TaskEvent.START_VALIDATION,
    )
    with pytest.raises(TaskStateError) as caught:
        transition_task(validating, TaskEvent.VALIDATE_PROGRAM)
    assert caught.value.code is TaskStateErrorCode.INVALID_IDENTIFIER
    assert caught.value.path == "/candidate_revision"

    committing = transition_task(
        _to_verifying(),
        TaskEvent.PASS_VERIFICATION,
        verification=_report(passed=True),
    )
    with pytest.raises(TaskStateError) as caught:
        transition_task(committing, TaskEvent.COMMIT)
    assert caught.value.code is TaskStateErrorCode.INVALID_IDENTIFIER
    assert caught.value.path == "/committed_revision"


def test_task_run_rejects_forged_candidate_data_before_candidate_creation():
    forged = transition_task(_task(), TaskEvent.REQUEST_PLAN).to_mapping()
    forged["candidate_revision"] = CANDIDATE_REVISION

    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged)
    assert caught.value.code is TaskStateErrorCode.INVARIANT_VIOLATION
    assert caught.value.path == "/candidate_revision"


def test_append_artifact_is_candidate_bound_and_pathless():
    append_artifact = state_module.append_artifact
    artifact = TaskArtifactRef(
        id="artifact_0123456789abcdef0123456789abcdef",
        name="candidate.step",
        format="step",
        sha256="c" * 64,
        size_bytes=12,
        candidate_revision=CANDIDATE_REVISION,
    )
    task = append_artifact(_to_executing(), artifact)

    assert task.artifacts == (artifact,)
    assert "path" not in artifact.to_mapping()
    with pytest.raises(TaskStateError) as caught:
        TaskArtifactRef(
            id="artifact_0123456789abcdef0123456789abcdef",
            name="../candidate.step",
            format="step",
            sha256="c" * 64,
            size_bytes=12,
            candidate_revision=CANDIDATE_REVISION,
        )
    assert caught.value.code is TaskStateErrorCode.INVALID_VALUE


def test_rich_verdicts_are_bounded_and_bind_acceptance_identity():
    outcome = state_module.CriterionOutcome
    verdict = CriterionVerdict(
        criterion_id="volume",
        required=True,
        outcome=outcome.PASS,
        expected=7200,
        observed=7200,
        delta=0,
        tolerance=0,
        evidence=("/shapes/box/volume",),
        message="Volume matched",
    )
    report = VerificationReport(
        id="verification_0123456789abcdef0123456789abcdef",
        acceptance_id="acceptance-1",
        candidate_revision=CANDIDATE_REVISION,
        manifest_sha256="a" * 64,
        observation_digest="b" * 64,
        passed=True,
        verdicts=(verdict,),
    )
    assert report.to_mapping()["acceptance_id"] == "acceptance-1"
    assert report.verdicts[0].outcome is outcome.PASS


def test_reconciliation_requires_durable_candidate_evidence_and_preserves_error():
    confirm_committed = TaskEvent.CONFIRM_COMMITTED
    confirm_uncommitted = TaskEvent.CONFIRM_UNCOMMITTED
    task = _to_verifying()
    task = transition_task(task, TaskEvent.PASS_VERIFICATION, verification=_report(passed=True))
    task = transition_task(task, TaskEvent.REQUIRE_RECOVERY, error=_error())

    task = transition_task(task, confirm_committed, committed_revision=COMMITTED_REVISION)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.last_error == _error()

    cleanup = transition_task(_to_executing(), TaskEvent.REQUIRE_CLEANUP, error=_error())
    rollback = transition_task(cleanup, confirm_uncommitted)
    assert rollback.status is TaskStatus.ROLLING_BACK
    assert rollback.last_error == _error()


def test_committed_revision_must_equal_the_verified_candidate_at_transition_and_parse():
    committing = _to_committing()
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            committing,
            TaskEvent.COMMIT,
            committed_revision=OTHER_REVISION,
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/committed_revision",
    )

    succeeded = transition_task(
        committing,
        TaskEvent.COMMIT,
        committed_revision=CANDIDATE_REVISION,
    ).to_mapping()
    succeeded["committed_revision"] = OTHER_REVISION
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(succeeded)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/committed_revision",
    )


@pytest.mark.parametrize(
    "attention_event",
    [TaskEvent.REQUIRE_CLEANUP, TaskEvent.REQUIRE_RECOVERY],
)
def test_pre_candidate_attention_round_trips_and_confirms_back_to_program_ready(
    attention_event,
):
    validating = transition_task(
        transition_task(
            transition_task(_task(), TaskEvent.REQUEST_PLAN),
            TaskEvent.SUBMIT_PROGRAM,
            program=_program(),
        ),
        TaskEvent.START_VALIDATION,
    )
    attention = transition_task(validating, attention_event, error=_error())
    assert attention.candidate_revision is None
    assert TaskRun.from_mapping(attention.to_mapping()) == attention

    resumed = transition_task(attention, TaskEvent.CONFIRM_PRE_CANDIDATE)
    assert resumed.status is TaskStatus.PROGRAM_READY
    assert resumed.program == validating.program
    assert resumed.candidate_revision is None
    assert resumed.last_error is None


def test_pre_candidate_cleanup_can_escalate_but_candidate_confirmations_cannot_cross_origins():
    validating = transition_task(
        transition_task(
            transition_task(_task(), TaskEvent.REQUEST_PLAN),
            TaskEvent.SUBMIT_PROGRAM,
            program=_program(),
        ),
        TaskEvent.START_VALIDATION,
    )
    cleanup = transition_task(validating, TaskEvent.REQUIRE_CLEANUP, error=_error())
    recovery = transition_task(cleanup, TaskEvent.REQUIRE_RECOVERY, error=_error())
    assert recovery.status is TaskStatus.RECOVERY_REQUIRED
    assert recovery.candidate_revision is None

    with pytest.raises(TaskStateError) as caught:
        transition_task(recovery, TaskEvent.CONFIRM_UNCOMMITTED)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            recovery,
            TaskEvent.CONFIRM_COMMITTED,
            committed_revision=COMMITTED_REVISION,
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )

    candidate_attention = transition_task(
        _to_executing(),
        TaskEvent.REQUIRE_RECOVERY,
        error=_error(),
    )
    with pytest.raises(TaskStateError) as caught:
        transition_task(candidate_attention, TaskEvent.CONFIRM_PRE_CANDIDATE)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )

    forged_pre = transition_task(recovery, TaskEvent.CONFIRM_PRE_CANDIDATE).to_mapping()
    forged_pre["status"] = TaskStatus.SUCCEEDED.value
    forged_pre["transitions"][-1]["event"] = TaskEvent.CONFIRM_COMMITTED.value
    forged_pre["transitions"][-1]["to_status"] = TaskStatus.SUCCEEDED.value
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged_pre)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )

    forged_candidate = candidate_attention.to_mapping()
    forged_candidate["status"] = TaskStatus.PROGRAM_READY.value
    forged_candidate["transitions"].append(
        {
            "schema_version": 1,
            "sequence": len(forged_candidate["transitions"]) + 1,
            "event": TaskEvent.CONFIRM_PRE_CANDIDATE.value,
            "from_status": TaskStatus.RECOVERY_REQUIRED.value,
            "to_status": TaskStatus.PROGRAM_READY.value,
        }
    )
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged_candidate)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )


def test_nested_contract_errors_are_translated_with_full_escaped_parent_pointer():
    task = _to_executing().to_mapping()
    task["steps"] = [
        {
            "schema_version": 1,
            "sequence": 1,
            "result": {"schema_version": 1, "ok": True, "value": None, "elapsed_ms": -1},
        }
    ]

    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(task)
    assert caught.value.path == "/steps/0/result/elapsed_ms"


def test_created_requires_a_request_plan_next_action():
    request_plan = NextAction.REQUEST_PLAN
    assert next_action_for(TaskStatus.CREATED) is request_plan


def test_verdict_rejects_legacy_passed_input_and_freezes_json_values():
    with pytest.raises(TypeError):
        CriterionVerdict(
            criterion_id="legacy",
            required=True,
            passed=True,
            message="Legacy input must fail",
        )

    expected = {"dimensions": [12, 20, 30]}
    verdict = CriterionVerdict(
        criterion_id="bbox",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        expected=expected,
        observed=[12, 20, 30],
        delta=[0, 0, 0],
        tolerance=[0, 0, 0],
        evidence=("/shapes/box/bbox",),
        message="Bounding box matched",
    )
    expected["dimensions"].append(99)
    assert verdict.to_mapping()["expected"] == {"dimensions": [12, 20, 30]}
    with pytest.raises(TypeError):
        verdict.expected["dimensions"] = ()


def test_verification_acceptance_id_is_required_and_verdict_paths_are_indexed():
    raw = _report(passed=True).to_mapping()
    del raw["acceptance_id"]
    with pytest.raises(TaskStateError) as caught:
        VerificationReport.from_mapping(raw)
    assert caught.value.path == "/acceptance_id"

    raw = _report(passed=True).to_mapping()
    raw["verdicts"][0]["evidence"] = ["not-a-pointer"]
    with pytest.raises(TaskStateError) as caught:
        VerificationReport.from_mapping(raw)
    assert caught.value.path == "/verdicts/0/evidence"


def test_steps_must_bind_candidate_and_committing_requires_passing_report():
    task = _to_executing()
    with pytest.raises(TaskStateError) as caught:
        append_step_result(task, StepResult(ok=True, value=None, elapsed_ms=0))
    assert caught.value.path == "/result/revision"

    forged = _to_executing().to_mapping()
    forged["status"] = TaskStatus.COMMITTING.value
    forged["transitions"][-1]["to_status"] = TaskStatus.COMMITTING.value
    forged["transitions"][-1]["event"] = TaskEvent.PASS_VERIFICATION.value
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged)
    assert caught.value.code is TaskStateErrorCode.INVARIANT_VIOLATION


def test_state_parser_rejects_container_subclasses_and_cycles_without_native_errors():
    class DictSubclass(dict):
        pass

    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(DictSubclass(_task().to_mapping()))
    assert caught.value.code is TaskStateErrorCode.INVALID_TYPE

    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="cycle",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected=cyclic,
            message="Cycle must fail",
        )
    assert caught.value.code is TaskStateErrorCode.INVALID_VALUE


def test_every_status_event_pair_has_exact_transition_or_rejection():
    executing = _to_executing()
    verifying = transition_task(executing, TaskEvent.COMPLETE_EXECUTION)
    committing = transition_task(
        verifying,
        TaskEvent.PASS_VERIFICATION,
        verification=_report(passed=True),
    )
    rolling_back = transition_task(executing, TaskEvent.FAIL_EXECUTION, error=_error())
    validating = transition_task(
        transition_task(
            transition_task(_task(), TaskEvent.REQUEST_PLAN),
            TaskEvent.SUBMIT_PROGRAM,
            program=_program(),
        ),
        TaskEvent.START_VALIDATION,
    )
    needs_input = transition_task(
        validating,
        TaskEvent.REJECT_PROGRAM,
        error=_error(needs_input=True),
    )
    status_tasks = {
        TaskStatus.CREATED: _task(),
        TaskStatus.NEEDS_PLAN: transition_task(_task(), TaskEvent.REQUEST_PLAN),
        TaskStatus.PROGRAM_READY: transition_task(
            transition_task(_task(), TaskEvent.REQUEST_PLAN),
            TaskEvent.SUBMIT_PROGRAM,
            program=_program(),
        ),
        TaskStatus.VALIDATING_PROGRAM: transition_task(
            transition_task(
                transition_task(_task(), TaskEvent.REQUEST_PLAN),
                TaskEvent.SUBMIT_PROGRAM,
                program=_program(),
            ),
            TaskEvent.START_VALIDATION,
        ),
        TaskStatus.EXECUTING: executing,
        TaskStatus.VERIFYING: verifying,
        TaskStatus.COMMITTING: committing,
        TaskStatus.ROLLING_BACK: rolling_back,
        TaskStatus.NEEDS_INPUT: needs_input,
        TaskStatus.RECOVERY_REQUIRED: transition_task(
            committing,
            TaskEvent.REQUIRE_RECOVERY,
            error=_error(),
        ),
        TaskStatus.CLEANUP_REQUIRED: transition_task(
            committing,
            TaskEvent.REQUIRE_CLEANUP,
            error=_error(),
        ),
        TaskStatus.SUCCEEDED: transition_task(
            committing,
            TaskEvent.COMMIT,
            committed_revision=COMMITTED_REVISION,
        ),
        TaskStatus.FAILED: transition_task(rolling_back, TaskEvent.COMPLETE_ROLLBACK),
    }
    expected = {
        (TaskStatus.CREATED, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
        (TaskStatus.CREATED, TaskEvent.REQUEST_PLAN): TaskStatus.NEEDS_PLAN,
        (TaskStatus.NEEDS_PLAN, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
        (TaskStatus.NEEDS_PLAN, TaskEvent.SUBMIT_PROGRAM): TaskStatus.PROGRAM_READY,
        (TaskStatus.PROGRAM_READY, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
        (TaskStatus.NEEDS_INPUT, TaskEvent.SUBMIT_PROGRAM): TaskStatus.PROGRAM_READY,
        (TaskStatus.NEEDS_INPUT, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
        (TaskStatus.PROGRAM_READY, TaskEvent.START_VALIDATION): TaskStatus.VALIDATING_PROGRAM,
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.VALIDATE_PROGRAM): TaskStatus.EXECUTING,
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REJECT_PROGRAM): TaskStatus.NEEDS_INPUT,
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
        (TaskStatus.EXECUTING, TaskEvent.COMPLETE_EXECUTION): TaskStatus.VERIFYING,
        (TaskStatus.EXECUTING, TaskEvent.FAIL_EXECUTION): TaskStatus.ROLLING_BACK,
        (TaskStatus.EXECUTING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
        (TaskStatus.EXECUTING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
        (TaskStatus.EXECUTING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
        (TaskStatus.VERIFYING, TaskEvent.PASS_VERIFICATION): TaskStatus.COMMITTING,
        (TaskStatus.VERIFYING, TaskEvent.FAIL_VERIFICATION): TaskStatus.ROLLING_BACK,
        (TaskStatus.VERIFYING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
        (TaskStatus.VERIFYING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
        (TaskStatus.VERIFYING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
        (TaskStatus.COMMITTING, TaskEvent.COMMIT): TaskStatus.SUCCEEDED,
        (TaskStatus.COMMITTING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
        (TaskStatus.COMMITTING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
        (TaskStatus.COMMITTING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
        (TaskStatus.ROLLING_BACK, TaskEvent.COMPLETE_ROLLBACK): TaskStatus.FAILED,
        (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
        (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
        (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
        (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_UNCOMMITTED): TaskStatus.ROLLING_BACK,
        (TaskStatus.CLEANUP_REQUIRED, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
        (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
        (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_UNCOMMITTED): TaskStatus.ROLLING_BACK,
    }
    for (status, event), task in (
        ((status, event), task) for status, task in status_tasks.items() for event in TaskEvent
    ):
        kwargs = {}
        if event is TaskEvent.SUBMIT_PROGRAM:
            kwargs["program"] = _program()
        elif event is TaskEvent.VALIDATE_PROGRAM:
            kwargs["candidate_revision"] = CANDIDATE_REVISION
        elif event in {
            TaskEvent.REJECT_PROGRAM,
            TaskEvent.FAIL_EXECUTION,
            TaskEvent.FAIL_VERIFICATION,
            TaskEvent.REQUIRE_RECOVERY,
            TaskEvent.REQUIRE_CLEANUP,
        }:
            kwargs["error"] = _error(needs_input=event is TaskEvent.REJECT_PROGRAM)
        elif event is TaskEvent.PASS_VERIFICATION:
            kwargs["verification"] = _report(passed=True)
        elif event in {TaskEvent.COMMIT, TaskEvent.CONFIRM_COMMITTED}:
            kwargs["committed_revision"] = COMMITTED_REVISION
        target = expected.get((status, event))
        if target is None:
            with pytest.raises(TaskStateError):
                transition_task(task, event, **kwargs)
        else:
            assert transition_task(task, event, **kwargs).status is target


def _report_with(
    *,
    number: int = 1,
    passed: bool = True,
    candidate_revision: str = CANDIDATE_REVISION,
    acceptance_id: str = "acceptance-1",
    verdicts: tuple[CriterionVerdict, ...] | None = None,
) -> VerificationReport:
    if verdicts is None:
        verdicts = (
            CriterionVerdict(
                criterion_id=f"criterion-{number}",
                required=True,
                outcome=(
                    state_module.CriterionOutcome.PASS
                    if passed
                    else state_module.CriterionOutcome.FAIL
                ),
                message="criterion result",
            ),
        )
    return VerificationReport(
        id=f"verification_{number:032x}",
        acceptance_id=acceptance_id,
        candidate_revision=candidate_revision,
        manifest_sha256="a" * 64,
        observation_digest="b" * 64,
        passed=passed,
        verdicts=verdicts,
    )


def _artifact(number: int = 1, *, candidate_revision: str = CANDIDATE_REVISION) -> TaskArtifactRef:
    return TaskArtifactRef(
        id=f"artifact_{number:032x}",
        name=f"candidate-{number}.step",
        format="step",
        sha256="c" * 64,
        size_bytes=number,
        candidate_revision=candidate_revision,
    )


def _to_committing() -> TaskRun:
    verifying = _to_verifying()
    return transition_task(
        verifying,
        TaskEvent.PASS_VERIFICATION,
        verification=_report_with(),
    )


def _matrix_task(status: TaskStatus) -> TaskRun:
    executing = _to_executing()
    verifying = transition_task(executing, TaskEvent.COMPLETE_EXECUTION)
    committing = transition_task(
        verifying,
        TaskEvent.PASS_VERIFICATION,
        verification=_report_with(),
    )
    rolling_back = transition_task(
        executing,
        TaskEvent.FAIL_EXECUTION,
        error=_error(needs_input=True),
    )
    program_ready = transition_task(
        _task(),
        TaskEvent.REQUEST_PLAN,
    )
    program_ready = transition_task(
        program_ready,
        TaskEvent.SUBMIT_PROGRAM,
        program=_program(),
    )
    validating = transition_task(program_ready, TaskEvent.START_VALIDATION)
    needs_plan = transition_task(_task(), TaskEvent.REQUEST_PLAN)
    needs_input = transition_task(
        validating,
        TaskEvent.REJECT_PROGRAM,
        error=_error(needs_input=True),
    )
    preparing_review = _to_preparing_review()
    awaiting_review = transition_task(preparing_review, TaskEvent.PUBLISH_DRAFT)
    accepting_draft = transition_task(awaiting_review, TaskEvent.ACCEPT_DRAFT)
    rejected = transition_task(awaiting_review, TaskEvent.REJECT_DRAFT)
    cancel_requested = transition_task(committing, TaskEvent.REQUEST_CANCEL)
    cancelling = transition_task(cancel_requested, TaskEvent.START_CANCELLATION)
    cancelled = transition_task(cancelling, TaskEvent.CONFIRM_CANCELLED)
    cases = {
        TaskStatus.CREATED: _task(),
        TaskStatus.NEEDS_PLAN: needs_plan,
        TaskStatus.PROGRAM_READY: program_ready,
        TaskStatus.VALIDATING_PROGRAM: validating,
        TaskStatus.EXECUTING: executing,
        TaskStatus.VERIFYING: verifying,
        TaskStatus.COMMITTING: committing,
        TaskStatus.PREPARING_REVIEW: preparing_review,
        TaskStatus.AWAITING_USER_REVIEW: awaiting_review,
        TaskStatus.ACCEPTING_DRAFT: accepting_draft,
        TaskStatus.ROLLING_BACK: rolling_back,
        TaskStatus.NEEDS_INPUT: needs_input,
        TaskStatus.RECOVERY_REQUIRED: transition_task(
            committing,
            TaskEvent.REQUIRE_RECOVERY,
            error=_error(),
        ),
        TaskStatus.CLEANUP_REQUIRED: transition_task(
            committing,
            TaskEvent.REQUIRE_CLEANUP,
            error=_error(),
        ),
        TaskStatus.SUCCEEDED: transition_task(
            committing,
            TaskEvent.COMMIT,
            committed_revision=COMMITTED_REVISION,
        ),
        TaskStatus.FAILED: transition_task(rolling_back, TaskEvent.COMPLETE_ROLLBACK),
        TaskStatus.REJECTED: rejected,
        TaskStatus.CANCEL_REQUESTED: cancel_requested,
        TaskStatus.CANCELLING: cancelling,
        TaskStatus.CANCELLED: cancelled,
    }
    return cases[status]


LEGAL_EDGES = {
    (TaskStatus.CREATED, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.CREATED, TaskEvent.REQUEST_PLAN): TaskStatus.NEEDS_PLAN,
    (TaskStatus.NEEDS_PLAN, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.NEEDS_PLAN, TaskEvent.SUBMIT_PROGRAM): TaskStatus.PROGRAM_READY,
    (TaskStatus.PROGRAM_READY, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.NEEDS_INPUT, TaskEvent.SUBMIT_PROGRAM): TaskStatus.PROGRAM_READY,
    (TaskStatus.NEEDS_INPUT, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCELLED,
    (TaskStatus.PROGRAM_READY, TaskEvent.START_VALIDATION): TaskStatus.VALIDATING_PROGRAM,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.VALIDATE_PROGRAM): TaskStatus.EXECUTING,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REJECT_PROGRAM): TaskStatus.NEEDS_INPUT,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.EXECUTING, TaskEvent.COMPLETE_EXECUTION): TaskStatus.VERIFYING,
    (TaskStatus.EXECUTING, TaskEvent.FAIL_EXECUTION): TaskStatus.ROLLING_BACK,
    (TaskStatus.EXECUTING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.EXECUTING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.VERIFYING, TaskEvent.PASS_VERIFICATION): TaskStatus.COMMITTING,
    (TaskStatus.VERIFYING, TaskEvent.FAIL_VERIFICATION): TaskStatus.ROLLING_BACK,
    (TaskStatus.VERIFYING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.VERIFYING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.COMMITTING, TaskEvent.COMMIT): TaskStatus.SUCCEEDED,
    (TaskStatus.COMMITTING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.COMMITTING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.PUBLISH_DRAFT): TaskStatus.AWAITING_USER_REVIEW,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.AWAITING_USER_REVIEW, TaskEvent.ACCEPT_DRAFT): TaskStatus.ACCEPTING_DRAFT,
    (TaskStatus.AWAITING_USER_REVIEW, TaskEvent.REJECT_DRAFT): TaskStatus.REJECTED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.COMMIT): TaskStatus.SUCCEEDED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.ABORT_ACCEPT): TaskStatus.AWAITING_USER_REVIEW,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.ROLLING_BACK, TaskEvent.COMPLETE_ROLLBACK): TaskStatus.FAILED,
    (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
    (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_UNCOMMITTED): TaskStatus.ROLLING_BACK,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_UNCOMMITTED): TaskStatus.ROLLING_BACK,
    (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.EXECUTING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.VERIFYING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.COMMITTING, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.PREPARING_REVIEW, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.ACCEPTING_DRAFT, TaskEvent.REQUEST_CANCEL): TaskStatus.CANCEL_REQUESTED,
    (TaskStatus.CANCEL_REQUESTED, TaskEvent.START_CANCELLATION): TaskStatus.CANCELLING,
    (TaskStatus.CANCELLING, TaskEvent.CONFIRM_CANCELLED): TaskStatus.CANCELLED,
    (TaskStatus.CANCELLING, TaskEvent.CONFIRM_COMMITTED): TaskStatus.SUCCEEDED,
    (TaskStatus.CANCELLING, TaskEvent.REQUIRE_RECOVERY): TaskStatus.RECOVERY_REQUIRED,
    (TaskStatus.CANCELLING, TaskEvent.REQUIRE_CLEANUP): TaskStatus.CLEANUP_REQUIRED,
}


def _edge_payload(event: TaskEvent) -> dict[str, object]:
    if event is TaskEvent.SUBMIT_PROGRAM:
        return {"program": _program()}
    if event is TaskEvent.VALIDATE_PROGRAM:
        return {"candidate_revision": CANDIDATE_REVISION}
    if event in {
        TaskEvent.REJECT_PROGRAM,
        TaskEvent.FAIL_EXECUTION,
        TaskEvent.FAIL_VERIFICATION,
        TaskEvent.REQUIRE_RECOVERY,
        TaskEvent.REQUIRE_CLEANUP,
    }:
        return {"error": _error(needs_input=event is TaskEvent.REJECT_PROGRAM)}
    if event is TaskEvent.PASS_VERIFICATION:
        return {"verification": _report_with()}
    if event in {TaskEvent.COMMIT, TaskEvent.CONFIRM_COMMITTED}:
        return {"committed_revision": COMMITTED_REVISION}
    return {}


@pytest.mark.parametrize(
    ("status", "event"),
    [(status, event) for status in TaskStatus for event in TaskEvent],
    ids=lambda item: item.value,
)
def test_full_status_event_matrix_has_exact_target_and_rejection_precedence(status, event):
    task = _matrix_task(status)
    target = LEGAL_EDGES.get((status, event))
    if target is not None:
        assert transition_task(task, event, **_edge_payload(event)).status is target
        return

    with pytest.raises(TaskStateError) as caught:
        transition_task(task, event, **_edge_payload(event))
    if status in {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
        TaskStatus.CANCELLED,
    }:
        assert caught.value.code is TaskStateErrorCode.TERMINAL_STATE
        assert caught.value.path == "/status"
    else:
        assert caught.value.code is TaskStateErrorCode.INVALID_TRANSITION
        assert caught.value.path == "/event"


@pytest.mark.parametrize(
    ("status", "event", "kwargs", "code", "path"),
    [
        (
            TaskStatus.NEEDS_PLAN,
            TaskEvent.SUBMIT_PROGRAM,
            {},
            TaskStateErrorCode.INVALID_TYPE,
            "/program",
        ),
        (
            TaskStatus.NEEDS_INPUT,
            TaskEvent.SUBMIT_PROGRAM,
            {},
            TaskStateErrorCode.INVALID_TYPE,
            "/program",
        ),
        (
            TaskStatus.VALIDATING_PROGRAM,
            TaskEvent.VALIDATE_PROGRAM,
            {},
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/candidate_revision",
        ),
        (
            TaskStatus.EXECUTING,
            TaskEvent.FAIL_EXECUTION,
            {},
            TaskStateErrorCode.MISSING_ERROR,
            "/error",
        ),
        (
            TaskStatus.VERIFYING,
            TaskEvent.PASS_VERIFICATION,
            {},
            TaskStateErrorCode.INVALID_TYPE,
            "/verification",
        ),
        (
            TaskStatus.COMMITTING,
            TaskEvent.COMMIT,
            {},
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/committed_revision",
        ),
        (
            TaskStatus.RECOVERY_REQUIRED,
            TaskEvent.CONFIRM_COMMITTED,
            {},
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/committed_revision",
        ),
        (
            TaskStatus.NEEDS_PLAN,
            TaskEvent.SUBMIT_PROGRAM,
            {"program": object()},
            TaskStateErrorCode.INVALID_TYPE,
            "/program",
        ),
        (
            TaskStatus.NEEDS_INPUT,
            TaskEvent.SUBMIT_PROGRAM,
            {"program": object()},
            TaskStateErrorCode.INVALID_TYPE,
            "/program",
        ),
        (
            TaskStatus.VALIDATING_PROGRAM,
            TaskEvent.VALIDATE_PROGRAM,
            {"candidate_revision": 7},
            TaskStateErrorCode.INVALID_TYPE,
            "/candidate_revision",
        ),
        (
            TaskStatus.EXECUTING,
            TaskEvent.FAIL_EXECUTION,
            {"error": object()},
            TaskStateErrorCode.INVALID_TYPE,
            "/error",
        ),
        (
            TaskStatus.VERIFYING,
            TaskEvent.PASS_VERIFICATION,
            {"verification": object()},
            TaskStateErrorCode.INVALID_TYPE,
            "/verification",
        ),
        (
            TaskStatus.COMMITTING,
            TaskEvent.COMMIT,
            {"committed_revision": 7},
            TaskStateErrorCode.INVALID_TYPE,
            "/committed_revision",
        ),
        (
            TaskStatus.CREATED,
            TaskEvent.REQUEST_PLAN,
            {"program": _program()},
            TaskStateErrorCode.INVALID_VALUE,
            "/program",
        ),
        (
            TaskStatus.EXECUTING,
            TaskEvent.COMPLETE_EXECUTION,
            {"error": _error()},
            TaskStateErrorCode.INVALID_VALUE,
            "/error",
        ),
    ],
)
def test_legal_edge_payloads_reject_missing_wrong_and_extra_values(
    status, event, kwargs, code, path
):
    with pytest.raises(TaskStateError) as caught:
        transition_task(_matrix_task(status), event, **kwargs)
    assert caught.value.code is code
    assert caught.value.path == path


class _NestedSentinel:
    def __iter__(self):
        raise AssertionError("nested input was touched before its outer budget")


@pytest.mark.parametrize(
    ("name", "maximum"),
    [
        ("steps", MAX_STEP_RECORDS),
        ("transitions", MAX_TRANSITION_RECORDS),
        ("verification_reports", MAX_VERIFICATION_REPORTS),
        ("artifacts", MAX_ARTIFACT_REFS),
    ],
)
def test_task_mapping_max_plus_one_fails_before_nested_sentinel(name, maximum):
    raw = _task().to_mapping()
    raw[name] = [_NestedSentinel()] * (maximum + 1)
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert caught.value.code is TaskStateErrorCode.BUDGET_EXCEEDED
    assert caught.value.path == f"/{name}"


def test_verdict_and_evidence_max_plus_one_fail_before_nested_sentinel():
    report = _report_with().to_mapping()
    report["verdicts"] = [_NestedSentinel()] * (MAX_CRITERION_VERDICTS + 1)
    with pytest.raises(TaskStateError) as caught:
        VerificationReport.from_mapping(report)
    assert caught.value.code is TaskStateErrorCode.BUDGET_EXCEEDED
    assert caught.value.path == "/verdicts"

    verdict = _report_with().to_mapping()["verdicts"][0]
    verdict["evidence"] = [_NestedSentinel()] * (MAX_VERDICT_EVIDENCE + 1)
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict.from_mapping(verdict)
    assert caught.value.code is TaskStateErrorCode.BUDGET_EXCEEDED
    assert caught.value.path == "/evidence"


def test_all_six_sequence_budgets_accept_exact_max_and_reject_max_plus_one():
    executing = _to_executing()
    result = StepResult(ok=True, value=None, elapsed_ms=0, revision=CANDIDATE_REVISION)
    for _ in range(MAX_STEP_RECORDS):
        executing = append_step_result(executing, result)
    assert len(executing.steps) == MAX_STEP_RECORDS
    with pytest.raises(TaskStateError) as caught:
        append_step_result(executing, result)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.BUDGET_EXCEEDED, "/steps")

    verifying = _to_verifying()
    for number in range(MAX_VERIFICATION_REPORTS):
        verifying = append_verification(verifying, _report_with(number=number + 1))
    assert len(verifying.verification_reports) == MAX_VERIFICATION_REPORTS
    with pytest.raises(TaskStateError) as caught:
        append_verification(verifying, _report_with(number=MAX_VERIFICATION_REPORTS + 1))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/verification_reports",
    )

    artifacts = _to_executing()
    for number in range(MAX_ARTIFACT_REFS):
        artifacts = state_module.append_artifact(artifacts, _artifact(number + 1))
    assert len(artifacts.artifacts) == MAX_ARTIFACT_REFS
    with pytest.raises(TaskStateError) as caught:
        state_module.append_artifact(artifacts, _artifact(MAX_ARTIFACT_REFS + 1))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/artifacts",
    )

    verdicts = tuple(
        CriterionVerdict(
            criterion_id=f"criterion-{number}",
            required=False,
            outcome=state_module.CriterionOutcome.PASS,
            message="bounded verdict",
        )
        for number in range(MAX_CRITERION_VERDICTS)
    )
    report = _report_with(number=77, verdicts=verdicts)
    assert len(report.verdicts) == MAX_CRITERION_VERDICTS
    with pytest.raises(TaskStateError) as caught:
        _report_with(number=78, verdicts=verdicts + (verdicts[0],))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/verdicts",
    )

    evidence = tuple(f"/observation/{number}" for number in range(MAX_VERDICT_EVIDENCE))
    verdict = CriterionVerdict(
        criterion_id="evidence-limit",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        evidence=evidence,
        message="bounded evidence",
    )
    assert len(verdict.evidence) == MAX_VERDICT_EVIDENCE
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="too-much-evidence",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            evidence=evidence + ("/observation/extra",),
            message="over budget",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/evidence",
    )


def test_transition_budget_exact_max_is_not_rejected_as_a_budget_before_history_validation():
    raw = _task().to_mapping()
    raw["transitions"] = [
        TaskTransitionRecord(
            sequence=number + 1,
            event=TaskEvent.REQUEST_PLAN,
            from_status=TaskStatus.CREATED,
            to_status=TaskStatus.NEEDS_PLAN,
        ).to_mapping()
        for number in range(MAX_TRANSITION_RECORDS)
    ]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert caught.value.code is TaskStateErrorCode.INVARIANT_VIOLATION
    assert caught.value.path == "/transitions"


def test_real_duplicates_sequences_capacity_and_append_immutability_are_stable():
    raw = _to_verifying().to_mapping()
    report = _report_with().to_mapping()
    raw["verification_reports"] = [report, deepcopy(report)]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.DUPLICATE_IDENTIFIER,
        "/verification_reports",
    )

    raw = _to_executing().to_mapping()
    artifact = _artifact().to_mapping()
    raw["artifacts"] = [artifact, deepcopy(artifact)]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.DUPLICATE_IDENTIFIER,
        "/artifacts",
    )

    verdict = _report_with().to_mapping()["verdicts"][0]
    report = _report_with().to_mapping()
    report["verdicts"] = [verdict, deepcopy(verdict)]
    with pytest.raises(TaskStateError) as caught:
        VerificationReport.from_mapping(report)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.DUPLICATE_IDENTIFIER,
        "/verdicts",
    )

    raw = _to_executing().to_mapping()
    step = TaskStepRecord(
        sequence=1,
        result=StepResult(ok=True, value=None, elapsed_ms=0, revision=CANDIDATE_REVISION),
    ).to_mapping()
    raw["steps"] = [step, deepcopy(step)]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/steps",
    )

    raw = _to_executing().to_mapping()
    raw["transitions"][1]["sequence"] = 3
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )

    full = _to_executing()
    for number in range(MAX_ARTIFACT_REFS):
        full = state_module.append_artifact(full, _artifact(number + 1))
    with pytest.raises(TaskStateError) as caught:
        state_module.append_artifact(full, _artifact(1))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/artifacts",
    )

    original = _to_executing()
    appended = state_module.append_artifact(original, _artifact())
    assert original.artifacts == ()
    assert appended.artifacts == (_artifact(),)
    mapping = appended.to_mapping()
    mapping["artifacts"][0]["name"] = "changed.step"
    assert appended.artifacts[0].name == "candidate-1.step"


@pytest.mark.parametrize(
    ("required", "outcome", "passed"),
    [
        (True, state_module.CriterionOutcome.PASS, True),
        (True, state_module.CriterionOutcome.FAIL, False),
        (True, state_module.CriterionOutcome.UNSUPPORTED, False),
        (False, state_module.CriterionOutcome.PASS, True),
        (False, state_module.CriterionOutcome.FAIL, True),
        (False, state_module.CriterionOutcome.UNSUPPORTED, True),
    ],
)
def test_required_optional_outcome_truth_table(required, outcome, passed):
    verdict = CriterionVerdict(
        criterion_id="truth-table",
        required=required,
        outcome=outcome,
        message="truth-table outcome",
    )
    report = _report_with(number=40, passed=passed, verdicts=(verdict,))
    assert report.passed is passed


def test_rich_json_counts_scalars_containers_depth_aliases_and_thaws_exact_types():
    at_limit = {f"value-{number}": number for number in range(MAX_JSON_NODES - 1)}
    verdict = CriterionVerdict(
        criterion_id="nodes-at-limit",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        expected=at_limit,
        message="exact node budget",
    )
    assert verdict.to_mapping()["expected"] == at_limit
    over_limit = {f"value-{number}": number for number in range(MAX_JSON_NODES)}
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="nodes-over-limit",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected=over_limit,
            message="over node budget",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/expected",
    )

    nested: object = 0
    for _ in range(MAX_JSON_DEPTH):
        nested = [nested]
    CriterionVerdict(
        criterion_id="depth-at-limit",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        expected=nested,
        message="exact depth budget",
    )
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="depth-over-limit",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected=[nested],
            message="over depth budget",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/expected",
    )

    repeated = ["same"]
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="repeated-alias",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected={"left": repeated, "right": repeated},
            message="aliases are not durable JSON trees",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/expected/right",
    )

    round_trip = CriterionVerdict(
        criterion_id="thawed-types",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        expected={"array": (1, {"leaf": True})},
        message="plain JSON output",
    ).to_mapping()
    assert type(round_trip["expected"]) is dict
    assert type(round_trip["expected"]["array"]) is list
    assert type(round_trip["expected"]["array"][1]) is dict


@pytest.mark.parametrize("value", [{"bad": {1}}, b"bytes", object(), float("nan"), float("inf")])
def test_rich_json_rejects_non_json_and_non_finite_values(value):
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="invalid-json",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected=value,
            message="invalid JSON input",
        )
    assert caught.value.path == "/expected"
    assert caught.value.code in {TaskStateErrorCode.INVALID_TYPE, TaskStateErrorCode.INVALID_VALUE}


def test_json_and_measurements_reject_unsafe_integers_before_numeric_operations():
    unsafe = 10**10000
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="unsafe-json",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected=unsafe,
            message="unsafe integer",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/expected",
    )

    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="unsafe-measurement",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            delta=unsafe,
            message="unsafe measurement",
        )
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_VALUE, "/delta")

    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="unsafe-vector",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            delta=[unsafe],
            message="unsafe vector",
        )
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_VALUE, "/delta/0")


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("delta", True, "/delta"),
        ("delta", [1, "bad"], "/delta/1"),
        ("delta", [float("inf")], "/delta/0"),
        ("tolerance", -1, "/tolerance"),
        ("tolerance", [0, -1], "/tolerance/1"),
        ("tolerance", [], "/tolerance"),
    ],
)
def test_measurement_vectors_and_tolerance_boundaries_have_exact_paths(field, value, path):
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="measurement-boundary",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            message="measurement boundary",
            **{field: value},
        )
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_VALUE, path)


def test_exact_container_types_are_required_at_state_and_nested_contract_boundaries():
    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    class TupleSubclass(tuple):
        pass

    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(DictSubclass(_task().to_mapping()))
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_TYPE, "")

    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="subclass-list",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected=ListSubclass([1]),
            message="exact list only",
        )
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_TYPE, "/expected")

    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="subclass-tuple",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            expected=TupleSubclass((1,)),
            message="exact tuple only",
        )
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_TYPE, "/expected")

    raw = _to_executing().to_mapping()
    raw["program"] = DictSubclass(raw["program"])
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_TYPE, "/program")

    raw = _to_executing().to_mapping()
    raw["steps"] = ListSubclass([])
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_TYPE, "/steps")


def test_candidate_failure_with_input_hint_is_terminal_and_provenance_is_fail_closed():
    rolling = transition_task(
        _to_executing(),
        TaskEvent.FAIL_EXECUTION,
        error=_error(needs_input=True),
    )
    failed = transition_task(rolling, TaskEvent.COMPLETE_ROLLBACK)
    assert failed.status is TaskStatus.FAILED
    assert failed.last_error == _error(needs_input=True)

    with pytest.raises(TaskStateError) as caught:
        transition_task(
            rolling,
            TaskEvent.REQUEST_INPUT,
            error=_error(needs_input=True),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )

    with pytest.raises(TaskStateError) as caught:
        append_verification(_to_verifying(), _report_with(acceptance_id="wrong-acceptance"))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/report/acceptance_id",
    )

    forged = _to_executing().to_mapping()
    forged["verification_reports"] = [_report_with().to_mapping()]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/verification_reports",
    )

    rolling = transition_task(_to_executing(), TaskEvent.FAIL_EXECUTION, error=_error())
    raw = rolling.to_mapping()
    raw["last_error"] = None
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.MISSING_ERROR,
        "/last_error",
    )

    raw = _to_executing().to_mapping()
    raw["last_error"] = _error().to_mapping()
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/last_error",
    )


@pytest.mark.parametrize(
    "name",
    [".", "..", "../candidate.step", "dir/candidate.step", r"dir\\candidate.step"],
)
def test_artifact_names_are_pathless_and_artifact_reconciliation_guards_are_exact(name):
    with pytest.raises(TaskStateError) as caught:
        TaskArtifactRef(
            id="artifact_0123456789abcdef0123456789abcdef",
            name=name,
            format="step",
            sha256="c" * 64,
            size_bytes=1,
            candidate_revision=CANDIDATE_REVISION,
        )
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_VALUE, "/name")

    with pytest.raises(TaskStateError) as caught:
        state_module.append_artifact(
            _to_executing(),
            _artifact(candidate_revision=OTHER_REVISION),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/artifact/candidate_revision",
    )

    recovery = transition_task(_to_executing(), TaskEvent.REQUIRE_RECOVERY, error=_error())
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            recovery,
            TaskEvent.CONFIRM_COMMITTED,
            committed_revision=COMMITTED_REVISION,
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/verification_reports",
    )

    cleanup = transition_task(_to_executing(), TaskEvent.REQUIRE_CLEANUP, error=_error())
    rollback = transition_task(cleanup, TaskEvent.CONFIRM_UNCOMMITTED)
    assert rollback.status is TaskStatus.ROLLING_BACK
    assert rollback.last_error == _error()


def test_hostile_keys_nested_prefix_error_rendering_and_public_exports_are_stable():
    raw = _task().to_mapping()
    raw["a/b~c"] = True
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.UNKNOWN_FIELD, "/a~1b~0c")

    raw = _task().to_mapping()
    raw["x" * 10000] = True
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert caught.value.code is TaskStateErrorCode.UNKNOWN_FIELD
    assert len(caught.value.path) <= 256
    rendered = str(caught.value)
    assert "\n" not in rendered
    assert len(rendered) <= 512
    assert '"/' in rendered

    raw = _to_executing().to_mapping()
    raw = _to_executing().to_mapping()
    raw["steps"] = [
        {
            "schema_version": 1,
            "sequence": 1,
            "result": {"schema_version": 1, "ok": True, "elapsed_ms": -1, "value": None},
        }
    ]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/steps/0/result/elapsed_ms",
    )

    assert {
        "CriterionOutcome",
        "MAX_CRITERION_VERDICTS",
        "MAX_VERDICT_EVIDENCE",
        "TaskArtifactRef",
        "append_artifact",
    } <= set(state_module.__all__)


def _pre_candidate_needs_input() -> TaskRun:
    validating = transition_task(
        transition_task(
            transition_task(_task(), TaskEvent.REQUEST_PLAN),
            TaskEvent.SUBMIT_PROGRAM,
            program=_program(),
        ),
        TaskEvent.START_VALIDATION,
    )
    return transition_task(
        validating,
        TaskEvent.REJECT_PROGRAM,
        error=_error(needs_input=True),
    )


@pytest.mark.parametrize(
    ("status", "event", "keyword", "value", "code", "path"),
    [
        (
            TaskStatus.VALIDATING_PROGRAM,
            TaskEvent.VALIDATE_PROGRAM,
            "candidate_revision",
            None,
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/candidate_revision",
        ),
        (
            TaskStatus.VALIDATING_PROGRAM,
            TaskEvent.VALIDATE_PROGRAM,
            "candidate_revision",
            1,
            TaskStateErrorCode.INVALID_TYPE,
            "/candidate_revision",
        ),
        (
            TaskStatus.VALIDATING_PROGRAM,
            TaskEvent.VALIDATE_PROGRAM,
            "candidate_revision",
            "revision_bad",
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/candidate_revision",
        ),
        (
            TaskStatus.COMMITTING,
            TaskEvent.COMMIT,
            "committed_revision",
            None,
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/committed_revision",
        ),
        (
            TaskStatus.COMMITTING,
            TaskEvent.COMMIT,
            "committed_revision",
            1,
            TaskStateErrorCode.INVALID_TYPE,
            "/committed_revision",
        ),
        (
            TaskStatus.COMMITTING,
            TaskEvent.COMMIT,
            "committed_revision",
            "revision_bad",
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/committed_revision",
        ),
        (
            TaskStatus.RECOVERY_REQUIRED,
            TaskEvent.CONFIRM_COMMITTED,
            "committed_revision",
            None,
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/committed_revision",
        ),
        (
            TaskStatus.CLEANUP_REQUIRED,
            TaskEvent.CONFIRM_COMMITTED,
            "committed_revision",
            1,
            TaskStateErrorCode.INVALID_TYPE,
            "/committed_revision",
        ),
    ],
)
def test_required_identifiers_have_omitted_null_type_and_malformed_precedence(
    status, event, keyword, value, code, path
):
    kwargs = _edge_payload(event)
    if value is None:
        kwargs.pop(keyword, None)
    else:
        kwargs[keyword] = value
    with pytest.raises(TaskStateError) as caught:
        transition_task(_matrix_task(status), event, **kwargs)
    assert (caught.value.code, caught.value.path) == (code, path)


@pytest.mark.parametrize(
    ("status", "event", "kwargs", "code", "path"),
    [
        (
            TaskStatus.CREATED,
            TaskEvent.REQUEST_PLAN,
            {"candidate_revision": CANDIDATE_REVISION},
            TaskStateErrorCode.INVALID_VALUE,
            "/candidate_revision",
        ),
        (
            TaskStatus.NEEDS_PLAN,
            TaskEvent.SUBMIT_PROGRAM,
            {},
            TaskStateErrorCode.INVALID_TYPE,
            "/program",
        ),
        (
            TaskStatus.PROGRAM_READY,
            TaskEvent.START_VALIDATION,
            {"verification": _report_with()},
            TaskStateErrorCode.INVALID_VALUE,
            "/verification",
        ),
        (
            TaskStatus.VALIDATING_PROGRAM,
            TaskEvent.REJECT_PROGRAM,
            {},
            TaskStateErrorCode.MISSING_ERROR,
            "/error",
        ),
        (
            TaskStatus.EXECUTING,
            TaskEvent.COMPLETE_EXECUTION,
            {"error": _error()},
            TaskStateErrorCode.INVALID_VALUE,
            "/error",
        ),
        (
            TaskStatus.EXECUTING,
            TaskEvent.FAIL_EXECUTION,
            {"error": object()},
            TaskStateErrorCode.INVALID_TYPE,
            "/error",
        ),
        (
            TaskStatus.VERIFYING,
            TaskEvent.PASS_VERIFICATION,
            {"verification": object()},
            TaskStateErrorCode.INVALID_TYPE,
            "/verification",
        ),
        (
            TaskStatus.VERIFYING,
            TaskEvent.FAIL_VERIFICATION,
            {},
            TaskStateErrorCode.MISSING_ERROR,
            "/error",
        ),
        (
            TaskStatus.ROLLING_BACK,
            TaskEvent.COMPLETE_ROLLBACK,
            {"error": _error()},
            TaskStateErrorCode.INVALID_VALUE,
            "/error",
        ),
        (
            TaskStatus.RECOVERY_REQUIRED,
            TaskEvent.CONFIRM_UNCOMMITTED,
            {"committed_revision": COMMITTED_REVISION},
            TaskStateErrorCode.INVALID_VALUE,
            "/committed_revision",
        ),
        (
            TaskStatus.CLEANUP_REQUIRED,
            TaskEvent.CONFIRM_UNCOMMITTED,
            {"error": _error()},
            TaskStateErrorCode.INVALID_VALUE,
            "/error",
        ),
    ],
)
def test_event_families_have_missing_wrong_and_extra_payload_guards(
    status, event, kwargs, code, path
):
    with pytest.raises(TaskStateError) as caught:
        transition_task(_matrix_task(status), event, **kwargs)
    assert (caught.value.code, caught.value.path) == (code, path)


@pytest.mark.parametrize(
    ("status", "event", "keyword"),
    [
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.VALIDATE_PROGRAM, "candidate_revision"),
        (TaskStatus.COMMITTING, TaskEvent.COMMIT, "committed_revision"),
        (TaskStatus.RECOVERY_REQUIRED, TaskEvent.CONFIRM_COMMITTED, "committed_revision"),
        (TaskStatus.CLEANUP_REQUIRED, TaskEvent.CONFIRM_COMMITTED, "committed_revision"),
    ],
)
@pytest.mark.parametrize(
    ("value", "code"),
    [
        (None, TaskStateErrorCode.INVALID_IDENTIFIER),
        (1, TaskStateErrorCode.INVALID_TYPE),
        ("revision_bad", TaskStateErrorCode.INVALID_IDENTIFIER),
    ],
)
def test_required_identifier_null_wrong_type_and_malformed_values_are_distinct(
    status, event, keyword, value, code
):
    kwargs = _edge_payload(event)
    kwargs[keyword] = value
    with pytest.raises(TaskStateError) as caught:
        transition_task(_matrix_task(status), event, **kwargs)
    assert caught.value.code is code
    assert caught.value.path == f"/{keyword}"


@pytest.mark.parametrize(
    ("status", "event"),
    [
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REJECT_PROGRAM),
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_RECOVERY),
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.REQUIRE_CLEANUP),
        (TaskStatus.EXECUTING, TaskEvent.REQUIRE_RECOVERY),
        (TaskStatus.EXECUTING, TaskEvent.REQUIRE_CLEANUP),
        (TaskStatus.VERIFYING, TaskEvent.REQUIRE_RECOVERY),
        (TaskStatus.VERIFYING, TaskEvent.REQUIRE_CLEANUP),
        (TaskStatus.COMMITTING, TaskEvent.REQUIRE_RECOVERY),
        (TaskStatus.COMMITTING, TaskEvent.REQUIRE_CLEANUP),
        (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_RECOVERY),
        (TaskStatus.ROLLING_BACK, TaskEvent.REQUIRE_CLEANUP),
        (TaskStatus.CLEANUP_REQUIRED, TaskEvent.REQUIRE_RECOVERY),
    ],
)
def test_every_failure_event_requires_exact_structured_error_payload(status, event):
    with pytest.raises(TaskStateError) as caught:
        transition_task(_matrix_task(status), event)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.MISSING_ERROR, "/error")


@pytest.mark.parametrize("status", [TaskStatus.RECOVERY_REQUIRED, TaskStatus.CLEANUP_REQUIRED])
def test_reconciliation_payload_matrix_rejects_missing_wrong_and_extra_values(status):
    task = _matrix_task(status)
    for value, code in (
        (None, TaskStateErrorCode.INVALID_IDENTIFIER),
        (1, TaskStateErrorCode.INVALID_TYPE),
        ("revision_bad", TaskStateErrorCode.INVALID_IDENTIFIER),
    ):
        with pytest.raises(TaskStateError) as caught:
            transition_task(task, TaskEvent.CONFIRM_COMMITTED, committed_revision=value)
        assert (caught.value.code, caught.value.path) == (code, "/committed_revision")
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            task,
            TaskEvent.CONFIRM_UNCOMMITTED,
            committed_revision=COMMITTED_REVISION,
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/committed_revision",
    )


def test_authoritative_budget_constants_and_direct_exact_collections_are_accepted():
    assert MAX_STEP_RECORDS == 64
    assert MAX_TRANSITION_RECORDS == 136
    assert MAX_VERIFICATION_REPORTS == 16
    assert MAX_ARTIFACT_REFS == 128
    assert MAX_CRITERION_VERDICTS == 128
    assert MAX_VERDICT_EVIDENCE == 32

    executing = _to_executing()
    result = StepResult(ok=True, value=None, elapsed_ms=0, revision=CANDIDATE_REVISION)
    steps = tuple(TaskStepRecord(sequence=index + 1, result=result) for index in range(64))
    direct_steps = TaskRun(
        id=executing.id,
        project_id=executing.project_id,
        base_revision=executing.base_revision,
        reasoning_owner=executing.reasoning_owner,
        review_policy=executing.review_policy,
        status=executing.status,
        program=executing.program,
        candidate_revision=executing.candidate_revision,
        steps=steps,
        transitions=executing.transitions,
    )
    assert len(direct_steps.steps) == 64

    verifying = _to_verifying()
    reports = tuple(_report_with(number=index + 1) for index in range(16))
    direct_reports = TaskRun(
        id=verifying.id,
        project_id=verifying.project_id,
        base_revision=verifying.base_revision,
        reasoning_owner=verifying.reasoning_owner,
        review_policy=verifying.review_policy,
        status=verifying.status,
        program=verifying.program,
        candidate_revision=verifying.candidate_revision,
        verification_reports=reports,
        transitions=verifying.transitions,
    )
    assert len(direct_reports.verification_reports) == 16

    artifacts = tuple(_artifact(index + 1) for index in range(128))
    direct_artifacts = TaskRun(
        id=executing.id,
        project_id=executing.project_id,
        base_revision=executing.base_revision,
        reasoning_owner=executing.reasoning_owner,
        review_policy=executing.review_policy,
        status=executing.status,
        program=executing.program,
        candidate_revision=executing.candidate_revision,
        artifacts=artifacts,
        transitions=executing.transitions,
    )
    assert len(direct_artifacts.artifacts) == 128


def test_exact_128_transition_history_is_legal_and_directly_reconstructible():
    task = transition_task(_task(), TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=_program())
    for _ in range(42):
        task = transition_task(task, TaskEvent.START_VALIDATION)
        task = transition_task(task, TaskEvent.REQUIRE_RECOVERY, error=_error())
        task = transition_task(task, TaskEvent.CONFIRM_PRE_CANDIDATE)
    assert len(task.transitions) == 128
    direct = TaskRun(
        id=task.id,
        project_id=task.project_id,
        base_revision=task.base_revision,
        reasoning_owner=task.reasoning_owner,
        review_policy=task.review_policy,
        status=task.status,
        program=task.program,
        candidate_revision=None,
        verification_reports=(),
        last_error=None,
        transitions=task.transitions,
    )
    assert direct == task


def test_append_api_capacity_precedence_and_immutability_are_complete():
    result = StepResult(ok=True, value={"size": [1]}, elapsed_ms=0, revision=CANDIDATE_REVISION)
    steps = _to_executing()
    for _ in range(64):
        steps = append_step_result(steps, result)
    with pytest.raises(TaskStateError) as caught:
        append_step_result(steps, result)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.BUDGET_EXCEEDED, "/steps")
    before_steps = _to_executing()
    after_steps = append_step_result(before_steps, result)
    assert before_steps.steps == ()
    assert after_steps.steps[0].result.to_mapping()["value"] == {"size": [1]}

    reports = _to_verifying()
    for index in range(16):
        reports = append_verification(reports, _report_with(number=index + 1))
    with pytest.raises(TaskStateError) as caught:
        append_verification(reports, _report_with(number=1))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/verification_reports",
    )
    before_reports = _to_verifying()
    after_reports = append_verification(before_reports, _report_with())
    assert before_reports.verification_reports == ()
    assert after_reports.verification_reports == (_report_with(),)

    artifacts = _to_executing()
    for index in range(128):
        artifacts = state_module.append_artifact(artifacts, _artifact(index + 1))
    with pytest.raises(TaskStateError) as caught:
        state_module.append_artifact(artifacts, _artifact(1))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/artifacts",
    )
    before_artifacts = _to_executing()
    after_artifacts = state_module.append_artifact(before_artifacts, _artifact())
    assert before_artifacts.artifacts == ()
    assert after_artifacts.artifacts == (_artifact(),)


def test_step_and_transition_sequence_gaps_and_duplicates_are_rejected_independently():
    raw = _to_executing().to_mapping()
    step = TaskStepRecord(
        sequence=1,
        result=StepResult(ok=True, value=None, elapsed_ms=0, revision=CANDIDATE_REVISION),
    ).to_mapping()
    raw["steps"] = [step, {**step, "sequence": 3}]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/steps",
    )

    raw = _to_executing().to_mapping()
    raw["transitions"][1]["sequence"] = 1
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )


def test_hostile_subclasses_are_rejected_before_direct_or_nested_iteration():
    class DictSubclass(dict):
        pass

    class ListSubclass(list):
        pass

    class TupleSubclass(tuple):
        pass

    base = _to_executing()
    for field in ("steps", "verification_reports", "artifacts", "transitions"):
        fields = {
            "id": base.id,
            "project_id": base.project_id,
            "base_revision": base.base_revision,
            "reasoning_owner": base.reasoning_owner,
            "review_policy": base.review_policy,
            "status": base.status,
            "program": base.program,
            "candidate_revision": base.candidate_revision,
            "transitions": base.transitions,
        }
        fields[field] = TupleSubclass(())
        if field == "transitions":
            fields[field] = TupleSubclass(base.transitions)
        with pytest.raises(TaskStateError) as caught:
            TaskRun(**fields)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_TYPE,
            f"/{field}",
        )

    raw = _to_executing().to_mapping()
    raw["steps"] = ListSubclass(raw["steps"])
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_TYPE, "/steps")

    for field in ("verification_reports", "artifacts", "transitions"):
        raw = _to_executing().to_mapping()
        raw[field] = ListSubclass(raw[field])
        with pytest.raises(TaskStateError) as caught:
            TaskRun.from_mapping(raw)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_TYPE,
            f"/{field}",
        )

    raw = _to_executing().to_mapping()
    raw["steps"] = [
        DictSubclass(
            TaskStepRecord(
                sequence=1,
                result=StepResult(
                    ok=True,
                    value=None,
                    elapsed_ms=0,
                    revision=CANDIDATE_REVISION,
                ),
            ).to_mapping()
        )
    ]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.INVALID_TYPE, "/steps/0")

    raw = _to_executing().to_mapping()
    step = TaskStepRecord(
        sequence=1,
        result=StepResult(ok=True, value=None, elapsed_ms=0, revision=CANDIDATE_REVISION),
    ).to_mapping()
    step["result"] = DictSubclass(step["result"])
    raw["steps"] = [step]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/steps/0/result",
    )

    report = _report_with().to_mapping()
    report["verdicts"] = [DictSubclass(report["verdicts"][0])]
    with pytest.raises(TaskStateError) as caught:
        VerificationReport.from_mapping(report)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/verdicts/0",
    )

    raw = _to_executing().to_mapping()
    raw["transitions"] = [DictSubclass(raw["transitions"][0])]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/transitions/0",
    )

    raw = _to_verifying().to_mapping()
    raw["verification_reports"] = [DictSubclass(_report_with().to_mapping())]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/verification_reports/0",
    )

    raw = _to_executing().to_mapping()
    raw["artifacts"] = [DictSubclass(_artifact().to_mapping())]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/artifacts/0",
    )

    raw = _to_executing().to_mapping()
    raw["last_error"] = DictSubclass(_error().to_mapping())
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/last_error",
    )


def test_rich_verdict_contradictions_round_trip_and_measurement_boundaries_are_complete():
    required_pass = CriterionVerdict(
        criterion_id="contradiction",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        message="pass",
    )
    with pytest.raises(TaskStateError) as caught:
        _report_with(number=81, passed=False, verdicts=(required_pass,))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/passed",
    )

    required_fail = CriterionVerdict(
        criterion_id="contradiction-fail",
        required=True,
        outcome=state_module.CriterionOutcome.FAIL,
        message="fail",
    )
    with pytest.raises(TaskStateError) as caught:
        _report_with(number=82, passed=True, verdicts=(required_fail,))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/passed",
    )

    rich = CriterionVerdict(
        criterion_id="rich-round-trip",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        expected={"nested": [1, {"value": True}]},
        observed={"nested": [1.0, {"value": False}]},
        delta=[0, 0],
        tolerance=[0, 0],
        evidence=("/observations/0",),
        message="rich verdict",
    )
    report = _report_with(number=83, verdicts=(rich,))
    assert VerificationReport.from_mapping(report.to_mapping()) == report
    report_mapping = report.to_mapping()
    report_mapping["verdicts"][0]["expected"]["nested"].append("mutated")
    assert report.to_mapping()["verdicts"][0]["expected"] == {"nested": [1, {"value": True}]}

    task = append_step_result(
        _to_executing(),
        StepResult(
            ok=True,
            value={"rich": [1, {"text": "value"}]},
            elapsed_ms=0,
            revision=CANDIDATE_REVISION,
        ),
    )
    task = state_module.append_artifact(task, _artifact())
    task = transition_task(task, TaskEvent.COMPLETE_EXECUTION)
    task = append_verification(task, report)
    assert TaskRun.from_mapping(task.to_mapping()) == task

    vector = list(range(16))
    CriterionVerdict(
        criterion_id="vector-at-limit",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        delta=vector,
        tolerance=vector,
        message="vector at limit",
    )
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="vector-over-limit",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            delta=vector + [16],
            message="vector over limit",
        )
    assert (caught.value.code, caught.value.path) == (TaskStateErrorCode.BUDGET_EXCEEDED, "/delta")

    for field, value, path in (
        ("delta", float("nan"), "/delta"),
        ("delta", float("inf"), "/delta"),
        ("tolerance", float("nan"), "/tolerance"),
        ("tolerance", -0.1, "/tolerance"),
        ("delta", "not-a-number", "/delta"),
        ("tolerance", [0, float("inf")], "/tolerance/1"),
    ):
        with pytest.raises(TaskStateError) as caught:
            CriterionVerdict(
                criterion_id="measurement-boundary-complete",
                required=True,
                outcome=state_module.CriterionOutcome.PASS,
                message="measurement boundary",
                **{field: value},
            )
        assert caught.value.path == path
        assert caught.value.code in {
            TaskStateErrorCode.INVALID_TYPE,
            TaskStateErrorCode.INVALID_VALUE,
        }


def test_reachable_report_candidate_and_error_provenance_forgeries_are_rejected():
    committing = _to_committing()
    succeeded = transition_task(
        committing,
        TaskEvent.COMMIT,
        committed_revision=COMMITTED_REVISION,
    )
    for task in (committing, succeeded):
        raw = task.to_mapping()
        raw["verification_reports"] = []
        with pytest.raises(TaskStateError) as caught:
            TaskRun.from_mapping(raw)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/verification_reports",
        )

    needs_input = _pre_candidate_needs_input()
    resumed = transition_task(needs_input, TaskEvent.SUBMIT_PROGRAM, program=_program())
    assert resumed.last_error is None
    assert resumed.candidate_revision is None

    with pytest.raises(TaskStateError) as caught:
        append_step_result(
            _to_executing(),
            StepResult(ok=True, value=None, elapsed_ms=0, revision=OTHER_REVISION),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/result/revision",
    )

    with pytest.raises(TaskStateError) as caught:
        append_verification(_to_verifying(), _report_with(candidate_revision=OTHER_REVISION))
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/report/candidate_revision",
    )

    for status in (
        TaskStatus.CREATED,
        TaskStatus.NEEDS_PLAN,
        TaskStatus.PROGRAM_READY,
        TaskStatus.VALIDATING_PROGRAM,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
        TaskStatus.COMMITTING,
        TaskStatus.SUCCEEDED,
    ):
        raw = _matrix_task(status).to_mapping()
        raw["last_error"] = _error().to_mapping()
        with pytest.raises(TaskStateError) as caught:
            TaskRun.from_mapping(raw)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/last_error",
        )

    for status in (
        TaskStatus.ROLLING_BACK,
        TaskStatus.NEEDS_INPUT,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.CLEANUP_REQUIRED,
        TaskStatus.FAILED,
    ):
        raw = _matrix_task(status).to_mapping()
        raw["last_error"] = None
        with pytest.raises(TaskStateError) as caught:
            TaskRun.from_mapping(raw)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.MISSING_ERROR,
            "/last_error",
        )


def test_artifact_verifying_phase_and_reconciliation_evidence_payload_negatives_are_complete():
    verifying = _to_verifying()
    assert state_module.append_artifact(verifying, _artifact()).artifacts == (_artifact(),)
    for status in set(TaskStatus) - {TaskStatus.EXECUTING, TaskStatus.VERIFYING}:
        with pytest.raises(TaskStateError) as caught:
            state_module.append_artifact(_matrix_task(status), _artifact())
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_TRANSITION,
            "/status",
        )

    for status in (TaskStatus.RECOVERY_REQUIRED, TaskStatus.CLEANUP_REQUIRED):
        with_report = _matrix_task(status)
        committed = transition_task(
            with_report,
            TaskEvent.CONFIRM_COMMITTED,
            committed_revision=COMMITTED_REVISION,
        )
        assert committed.status is TaskStatus.SUCCEEDED
        uncommitted = transition_task(with_report, TaskEvent.CONFIRM_UNCOMMITTED)
        assert uncommitted.status is TaskStatus.ROLLING_BACK
        assert uncommitted.last_error == _error()

        no_evidence = _to_executing()
        no_evidence = transition_task(
            no_evidence,
            (
                TaskEvent.REQUIRE_RECOVERY
                if status is TaskStatus.RECOVERY_REQUIRED
                else TaskEvent.REQUIRE_CLEANUP
            ),
            error=_error(),
        )
        with pytest.raises(TaskStateError) as caught:
            transition_task(
                no_evidence,
                TaskEvent.CONFIRM_COMMITTED,
                committed_revision=COMMITTED_REVISION,
            )
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/verification_reports",
        )


def test_state_public_exports_are_exact_and_deferred_intent_is_absent():
    assert set(state_module.__all__) == {
        "MAX_ARTIFACT_REFS",
        "MAX_CRITERION_VERDICTS",
        "MAX_STEP_RECORDS",
        "MAX_TRANSITION_RECORDS",
        "MAX_VERIFICATION_REPORTS",
        "MAX_VERDICT_EVIDENCE",
        "CriterionOutcome",
        "CriterionVerdict",
        "NextAction",
        "ReasoningOwner",
        "ReviewDraft",
        "ReviewPolicy",
        "TaskArtifactRef",
        "TaskEvent",
        "TaskRun",
        "TaskStateError",
        "TaskStateErrorCode",
        "TaskStatus",
        "TaskStepRecord",
        "TaskTransitionRecord",
        "VerificationReport",
        "append_artifact",
        "append_step_result",
        "append_verification",
        "new_task_run",
        "next_action_for",
        "transition_task",
    }
    assert "Intent" not in state_module.__all__
    assert "IntentKind" not in state_module.__all__
    assert "intent" not in TaskRun.__dataclass_fields__
    assert "intent" not in _task().to_mapping()

    raw = _task().to_mapping()
    raw["intent"] = {"goal": "must remain deferred"}
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.UNKNOWN_FIELD,
        "/intent",
    )


def test_every_legal_event_rejects_each_unrelated_payload_family():
    payload_values = {
        "program": _program(),
        "candidate_revision": CANDIDATE_REVISION,
        "error": _error(needs_input=True),
        "verification": _report_with(),
        "committed_revision": COMMITTED_REVISION,
    }
    accepted_payloads = {
        TaskEvent.SUBMIT_PROGRAM: {"program"},
        TaskEvent.VALIDATE_PROGRAM: {"candidate_revision"},
        TaskEvent.REJECT_PROGRAM: {"error"},
        TaskEvent.FAIL_EXECUTION: {"error"},
        TaskEvent.FAIL_VERIFICATION: {"error"},
        TaskEvent.REQUIRE_RECOVERY: {"error"},
        TaskEvent.REQUIRE_CLEANUP: {"error"},
        TaskEvent.PASS_VERIFICATION: {"verification"},
        TaskEvent.COMMIT: {"committed_revision"},
        TaskEvent.CONFIRM_COMMITTED: {"committed_revision"},
    }

    for status, event in LEGAL_EDGES:
        for name, value in payload_values.items():
            if name in accepted_payloads.get(event, set()):
                continue
            kwargs = _edge_payload(event)
            kwargs[name] = value
            with pytest.raises(TaskStateError) as caught:
                transition_task(_matrix_task(status), event, **kwargs)
            assert (caught.value.code, caught.value.path) == (
                TaskStateErrorCode.INVALID_VALUE,
                f"/{name}",
            )


def test_every_failure_edge_distinguishes_missing_and_wrong_error_payloads():
    failure_edges = [
        (status, event)
        for status, event in LEGAL_EDGES
        if event
        in {
            TaskEvent.REJECT_PROGRAM,
            TaskEvent.FAIL_EXECUTION,
            TaskEvent.FAIL_VERIFICATION,
            TaskEvent.REQUIRE_RECOVERY,
            TaskEvent.REQUIRE_CLEANUP,
        }
    ]
    for status, event in failure_edges:
        with pytest.raises(TaskStateError) as caught:
            transition_task(_matrix_task(status), event)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.MISSING_ERROR,
            "/error",
        )

        with pytest.raises(TaskStateError) as caught:
            transition_task(_matrix_task(status), event, error=object())
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_TYPE,
            "/error",
        )


def test_candidate_bound_mapping_identifiers_have_required_precedence():
    factories = (
        (TaskArtifactRef.from_mapping, _artifact().to_mapping()),
        (VerificationReport.from_mapping, _report_with().to_mapping()),
    )
    cases = (
        ("missing", None, TaskStateErrorCode.INVALID_IDENTIFIER),
        ("present", None, TaskStateErrorCode.INVALID_IDENTIFIER),
        ("present", 7, TaskStateErrorCode.INVALID_TYPE),
        ("present", "revision_bad", TaskStateErrorCode.INVALID_IDENTIFIER),
    )
    for factory, original in factories:
        for mode, value, code in cases:
            raw = deepcopy(original)
            if mode == "missing":
                del raw["candidate_revision"]
            else:
                raw["candidate_revision"] = value
            with pytest.raises(TaskStateError) as caught:
                factory(raw)
            assert (caught.value.code, caught.value.path) == (
                code,
                "/candidate_revision",
            )

    for status in (TaskStatus.RECOVERY_REQUIRED, TaskStatus.CLEANUP_REQUIRED):
        with pytest.raises(TaskStateError) as caught:
            transition_task(_matrix_task(status), TaskEvent.CONFIRM_COMMITTED)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_IDENTIFIER,
            "/committed_revision",
        )


def test_step_and_transition_gap_and_duplicate_shapes_are_each_rejected():
    step = TaskStepRecord(
        sequence=1,
        result=StepResult(
            ok=True,
            value=None,
            elapsed_ms=0,
            revision=CANDIDATE_REVISION,
        ),
    ).to_mapping()
    for sequences in ((1, 3), (1, 1)):
        raw = _to_executing().to_mapping()
        raw["steps"] = [{**step, "sequence": value} for value in sequences]
        with pytest.raises(TaskStateError) as caught:
            TaskRun.from_mapping(raw)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/steps",
        )

    gap = _to_executing().to_mapping()
    for index, record in enumerate(gap["transitions"]):
        record["sequence"] = index + 1 if index == 0 else index + 2
    duplicate = _to_executing().to_mapping()
    duplicate["transitions"][1]["sequence"] = 1
    for raw in (gap, duplicate):
        with pytest.raises(TaskStateError) as caught:
            TaskRun.from_mapping(raw)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/transitions",
        )


def test_direct_builtin_lists_are_defensively_copied_before_caller_mutation():
    evidence = ["/measurement"]
    verdict = CriterionVerdict(
        criterion_id="copied-evidence",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        evidence=evidence,
        message="evidence is copied",
    )
    verdicts = [verdict]
    report = _report_with(number=91, verdicts=verdicts)
    evidence.clear()
    verdicts.clear()
    assert verdict.evidence == ("/measurement",)
    assert report.verdicts == (verdict,)

    verifying = _to_verifying()
    steps = [
        TaskStepRecord(
            sequence=1,
            result=StepResult(
                ok=True,
                value=None,
                elapsed_ms=0,
                revision=CANDIDATE_REVISION,
            ),
        )
    ]
    reports = [_report_with()]
    artifacts = [_artifact()]
    transitions = list(verifying.transitions)
    task = TaskRun(
        id=verifying.id,
        project_id=verifying.project_id,
        base_revision=verifying.base_revision,
        reasoning_owner=verifying.reasoning_owner,
        review_policy=verifying.review_policy,
        status=verifying.status,
        program=verifying.program,
        candidate_revision=verifying.candidate_revision,
        steps=steps,
        verification_reports=reports,
        artifacts=artifacts,
        transitions=transitions,
    )
    steps.clear()
    reports.clear()
    artifacts.clear()
    transitions.clear()
    assert len(task.steps) == 1
    assert len(task.verification_reports) == 1
    assert len(task.artifacts) == 1
    assert len(task.transitions) == len(verifying.transitions)


def test_hostile_direct_and_nested_containers_reject_before_iteration():
    class ExplodingList(list):
        def __iter__(self):
            raise AssertionError("hostile list was iterated")

    class ExplodingTuple(tuple):
        def __iter__(self):
            raise AssertionError("hostile tuple was iterated")

    class ExplodingDict(dict):
        def __iter__(self):
            raise AssertionError("hostile dict was iterated")

        def items(self):
            raise AssertionError("hostile dict items were read")

    executing = _to_executing()
    direct_fields = {
        "id": executing.id,
        "project_id": executing.project_id,
        "base_revision": executing.base_revision,
        "reasoning_owner": executing.reasoning_owner,
        "review_policy": executing.review_policy,
        "status": executing.status,
        "program": executing.program,
        "candidate_revision": executing.candidate_revision,
        "transitions": executing.transitions,
    }
    for field in ("steps", "verification_reports", "artifacts", "transitions"):
        fields = dict(direct_fields)
        value = executing.transitions if field == "transitions" else ()
        fields[field] = ExplodingTuple(value)
        with pytest.raises(TaskStateError) as caught:
            TaskRun(**fields)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_TYPE,
            f"/{field}",
        )

    with pytest.raises(TaskStateError) as caught:
        VerificationReport(
            id="verification_99999999999999999999999999999999",
            acceptance_id="acceptance-1",
            candidate_revision=CANDIDATE_REVISION,
            manifest_sha256="a" * 64,
            observation_digest="b" * 64,
            passed=True,
            verdicts=ExplodingTuple((_report_with().verdicts[0],)),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/verdicts",
    )

    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="hostile-evidence",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            evidence=ExplodingTuple(("/observations/0",)),
            message="hostile evidence",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/evidence",
    )

    raw = _to_executing().to_mapping()
    raw["program"]["operations"] = ExplodingList(raw["program"]["operations"])
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/program/operations",
    )

    raw = _to_executing().to_mapping()
    raw["program"]["acceptance"] = ExplodingDict(raw["program"]["acceptance"])
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/program/acceptance",
    )

    raw = _to_executing().to_mapping()
    raw["program"]["acceptance"]["criteria"] = ExplodingList([])
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/program/acceptance/criteria",
    )

    step = TaskStepRecord(
        sequence=1,
        result=StepResult(
            ok=True,
            value=None,
            elapsed_ms=0,
            revision=CANDIDATE_REVISION,
        ),
    ).to_mapping()
    step["result"]["artifacts"] = ExplodingList([])
    raw = _to_executing().to_mapping()
    raw["steps"] = [step]
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/steps/0/result/artifacts",
    )

    raw = _matrix_task(TaskStatus.ROLLING_BACK).to_mapping()
    raw["last_error"]["related_objects"] = ExplodingList([])
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/last_error/related_objects",
    )


def test_tolerance_has_independent_vector_and_unsafe_integer_boundaries():
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="tolerance-vector-over-limit",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            tolerance=list(range(17)),
            message="tolerance vector over limit",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/tolerance",
    )

    unsafe = 2**10000
    for value, path in (
        (unsafe, "/tolerance"),
        ([0, unsafe], "/tolerance/1"),
    ):
        with pytest.raises(TaskStateError) as caught:
            CriterionVerdict(
                criterion_id="unsafe-tolerance",
                required=True,
                outcome=state_module.CriterionOutcome.PASS,
                tolerance=value,
                message="unsafe tolerance",
            )
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_VALUE,
            path,
        )


def test_durable_candidate_and_acceptance_bindings_reject_raw_forgeries():
    result = StepResult(
        ok=True,
        value=None,
        elapsed_ms=0,
        revision=CANDIDATE_REVISION,
    )
    step_task = append_step_result(_to_executing(), result)
    step_raw = step_task.to_mapping()
    step_raw["steps"][0]["result"]["revision"] = OTHER_REVISION

    report_task = append_verification(_to_verifying(), _report_with())
    report_candidate_raw = report_task.to_mapping()
    report_candidate_raw["verification_reports"][0]["candidate_revision"] = OTHER_REVISION
    report_acceptance_raw = report_task.to_mapping()
    report_acceptance_raw["verification_reports"][0]["acceptance_id"] = "acceptance-other"

    artifact_task = state_module.append_artifact(_to_executing(), _artifact())
    artifact_raw = artifact_task.to_mapping()
    artifact_raw["artifacts"][0]["candidate_revision"] = OTHER_REVISION

    for raw, path in (
        (step_raw, "/steps"),
        (report_candidate_raw, "/verification_reports"),
        (report_acceptance_raw, "/verification_reports"),
        (artifact_raw, "/artifacts"),
    ):
        with pytest.raises(TaskStateError) as caught:
            TaskRun.from_mapping(raw)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVARIANT_VIOLATION,
            path,
        )


def test_reconciliation_rejects_nonpassing_evidence_and_preserves_both_errors():
    for require_event in (
        TaskEvent.REQUIRE_RECOVERY,
        TaskEvent.REQUIRE_CLEANUP,
    ):
        verifying = append_verification(_to_verifying(), _report_with(passed=False))
        task = transition_task(verifying, require_event, error=_error())
        with pytest.raises(TaskStateError) as caught:
            transition_task(
                task,
                TaskEvent.CONFIRM_COMMITTED,
                committed_revision=COMMITTED_REVISION,
            )
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVARIANT_VIOLATION,
            "/verification_reports",
        )

        with_evidence = _matrix_task(task.status)
        committed = transition_task(
            with_evidence,
            TaskEvent.CONFIRM_COMMITTED,
            committed_revision=COMMITTED_REVISION,
        )
        assert committed.last_error == with_evidence.last_error

        with pytest.raises(TaskStateError) as caught:
            transition_task(
                with_evidence,
                TaskEvent.CONFIRM_UNCOMMITTED,
                committed_revision=COMMITTED_REVISION,
            )
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.INVALID_VALUE,
            "/committed_revision",
        )


def test_nested_parser_preflight_preserves_exact_field_json_depth():
    accepted: object = 0
    for _ in range(MAX_JSON_DEPTH):
        accepted = [accepted]
    raw = _report_with(number=92).to_mapping()
    raw["verdicts"][0]["expected"] = accepted
    assert VerificationReport.from_mapping(raw).verdicts[0].expected is not None


def test_nested_parser_preflight_rejects_depth_before_late_hostile_value():
    class ExplodingList(list):
        def __iter__(self):
            raise AssertionError("late hostile list was iterated")

    nested: object = ExplodingList()
    for _ in range(73):
        nested = [nested]
    depth_raw = _report_with(number=93).to_mapping()
    depth_raw["verdicts"][0]["expected"] = nested
    with pytest.raises(TaskStateError) as caught:
        VerificationReport.from_mapping(depth_raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/verdicts/0",
    )


def test_nested_parser_preflight_contains_extreme_depth_without_recursion_error():
    deeply_nested: object = 0
    for _ in range(2048):
        deeply_nested = [deeply_nested]
    deep_raw = _report_with(number=94).to_mapping()
    deep_raw["verdicts"][0]["expected"] = deeply_nested
    try:
        VerificationReport.from_mapping(deep_raw)
    except RecursionError:
        pytest.fail("nested preflight leaked RecursionError")
    except TaskStateError as error:
        assert (error.code, error.path) == (
            TaskStateErrorCode.BUDGET_EXCEEDED,
            "/verdicts/0",
        )
    else:
        pytest.fail("deep nested parser input was accepted")


def test_nested_parser_preflight_rejects_nodes_before_late_hostile_value():
    class ExplodingList(list):
        def __iter__(self):
            raise AssertionError("late hostile list was iterated")

    oversized = {f"value-{number}": number for number in range(4096)}
    oversized["late"] = ExplodingList()
    nodes_raw = _report_with(number=95).to_mapping()
    nodes_raw["verdicts"][0]["expected"] = oversized
    with pytest.raises(TaskStateError) as caught:
        VerificationReport.from_mapping(nodes_raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/verdicts/0",
    )


@pytest.mark.parametrize("origin", ["pre_candidate", "candidate"], ids=str)
def test_needs_input_durable_states_require_the_submitted_program(origin):
    if origin == "pre_candidate":
        task = transition_task(_task(), TaskEvent.REQUEST_PLAN)
        task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=_program())
        task = transition_task(task, TaskEvent.START_VALIDATION)
        task = transition_task(
            task,
            TaskEvent.REJECT_PROGRAM,
            error=_error(needs_input=True),
        )
    else:
        task = _matrix_task(TaskStatus.NEEDS_INPUT)

    raw = task.to_mapping()
    raw["program"] = None
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/program",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected", "a" * 4096),
        ("observed", "é" * 2048),
        ("expected", {"a" * 256: 1}),
        ("observed", {"é" * 128: 1}),
    ],
    ids=("ascii-string", "multibyte-string", "ascii-key", "multibyte-key"),
)
def test_rich_json_strings_and_keys_accept_exact_utf8_byte_budgets(field, value):
    verdict = CriterionVerdict(
        criterion_id="exact-byte-budget",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        message="exact byte budget",
        **{field: value},
    )
    assert verdict.to_mapping()[field] == value


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected", "a" * 4097),
        ("observed", "é" * 2049),
        ("expected", {"a" * 257: 1}),
        ("observed", {"é" * 129: 1}),
    ],
    ids=("ascii-string", "multibyte-string", "ascii-key", "multibyte-key"),
)
def test_rich_json_strings_and_keys_reject_max_plus_one_utf8_byte(field, value):
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="over-byte-budget",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            message="over byte budget",
            **{field: value},
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        f"/{field}",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected", "\ud800"),
        ("observed", {"\ud800": 1}),
    ],
    ids=("string", "object-key"),
)
def test_rich_json_rejects_invalid_unicode_without_native_encoding_error(field, value):
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="invalid-unicode",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            message="invalid Unicode",
            **{field: value},
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        f"/{field}",
    )


@pytest.mark.parametrize(
    "pointer",
    ["/" + "a" * 255, "/" + "é" * 127 + "a"],
    ids=("ascii", "multibyte"),
)
def test_evidence_pointers_accept_exact_individual_utf8_byte_budget(pointer):
    verdict = CriterionVerdict(
        criterion_id="exact-evidence-pointer-budget",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        evidence=(pointer,),
        message="exact evidence pointer budget",
    )
    assert verdict.evidence == (pointer,)


@pytest.mark.parametrize(
    "pointer",
    ["/" + "a" * 256, "/" + "é" * 128],
    ids=("ascii", "multibyte"),
)
def test_evidence_pointers_reject_max_plus_one_individual_utf8_byte(pointer):
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="over-evidence-pointer-budget",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            evidence=(pointer,),
            message="over evidence pointer budget",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/evidence",
    )


def test_evidence_pointers_reject_invalid_unicode_without_native_encoding_error():
    CriterionVerdict(
        criterion_id="valid-before-invalid-evidence",
        required=True,
        outcome=state_module.CriterionOutcome.PASS,
        evidence=("/valid",),
        message="valid evidence baseline",
    )
    with pytest.raises(TaskStateError) as caught:
        CriterionVerdict(
            criterion_id="invalid-evidence-unicode",
            required=True,
            outcome=state_module.CriterionOutcome.PASS,
            evidence=("/\ud800",),
            message="invalid evidence Unicode",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/evidence",
    )


def test_nested_parser_propagates_trusted_parser_programming_errors():
    def fail_trusted_parser(_value):
        raise AssertionError("trusted parser bug")

    with pytest.raises(AssertionError, match="trusted parser bug"):
        state_module._parse_nested({}, fail_trusted_parser, "/trusted")


def test_nested_c1_alias_dag_is_rejected_before_parser_expansion(monkeypatch):
    shared: object = 0
    for _ in range(8):
        shared = [shared, shared]

    step = TaskStepRecord(
        sequence=1,
        result=StepResult(
            ok=True,
            value=None,
            elapsed_ms=0,
            revision=CANDIDATE_REVISION,
        ),
    ).to_mapping()
    step["result"]["value"] = shared
    raw = _to_executing().to_mapping()
    raw["steps"] = [step]

    def explode_parser(_value):
        raise AssertionError("nested parser ran before alias rejection")

    monkeypatch.setattr(TaskStepRecord, "from_mapping", explode_parser)
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/steps/0/result/value/1",
    )


@pytest.mark.parametrize("maximum", [256, 4096], ids=("short", "rich-json"))
def test_utf8_budget_rejects_by_codepoint_length_before_encoding(maximum):
    class ExplodingStr(str):
        def encode(self, *_args, **_kwargs):
            raise AssertionError("over-limit text was encoded")

    value = ExplodingStr("x" * (maximum + 1))
    with pytest.raises(TaskStateError) as caught:
        state_module._validate_utf8_budget(value, "/text", maximum)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/text",
    )


@pytest.mark.parametrize(
    "key",
    ["x" * 257, "é" * 129],
    ids=("codepoint-over-limit", "multibyte-over-limit"),
)
def test_nested_key_budget_precedes_pointer_construction(monkeypatch, key):
    def explode_pointer(*_args, **_kwargs):
        raise AssertionError("over-limit key reached pointer construction")

    monkeypatch.setattr(state_module, "_error_pointer", explode_pointer)
    with pytest.raises(TaskStateError) as caught:
        state_module._guard_exact_nested_containers({key: None}, "/nested")
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/nested",
    )


def test_overlong_error_pointer_token_truncates_before_token_escaping():
    class ExplodingToken(str):
        def replace(self, *_args, **_kwargs):
            raise AssertionError("overlong token was escaped")

    pointer = state_module._error_pointer("/parent", ExplodingToken("x" * 257))
    assert pointer == "/parent/__truncated__"


def test_bounded_sequence_budget_precedes_tuple_copy(monkeypatch):
    def explode_tuple(*_args, **_kwargs):
        raise AssertionError("over-limit sequence was copied")

    monkeypatch.setattr(state_module, "tuple", explode_tuple, raising=False)
    with pytest.raises(TaskStateError) as caught:
        state_module._bounded_sequence([None] * 17, "/items", 16)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/items",
    )


def test_text_length_budget_precedes_strip(monkeypatch):
    expected = TaskStateError(
        TaskStateErrorCode.INVALID_VALUE,
        "/message",
        "expected bounded printable single-line text",
    )

    class ExplodingText(str):
        def strip(self, *_args, **_kwargs):
            raise AssertionError("over-limit text was stripped")

        def isprintable(self):
            raise AssertionError("over-limit text was scanned for printability")

        def splitlines(self, *_args, **_kwargs):
            raise AssertionError("over-limit text was split into lines")

    def return_expected_error(code, path, message):
        assert (code, path, message) == (
            TaskStateErrorCode.INVALID_VALUE,
            "/message",
            "expected bounded printable single-line text",
        )
        return expected

    monkeypatch.setattr(state_module, "str", ExplodingText, raising=False)
    monkeypatch.setattr(state_module, "_failure", return_expected_error)
    with pytest.raises(TaskStateError) as caught:
        state_module._text(ExplodingText("x" * 257), "/message")
    assert caught.value is expected


def test_public_error_path_budget_precedes_canonical_scan(monkeypatch):
    def explode_canonical_scan(_value):
        raise AssertionError("over-limit path was scanned")

    def explode_path_bounding(_value):
        raise AssertionError("over-limit path reached path bounding")

    monkeypatch.setattr(state_module, "is_canonical_json_pointer", explode_canonical_scan)
    monkeypatch.setattr(state_module, "_bounded_error_path", explode_path_bounding)
    with pytest.raises(ValueError, match="path must be a canonical RFC 6901 JSON Pointer"):
        TaskStateError(
            TaskStateErrorCode.INVALID_VALUE,
            "/" + "x" * 256,
            "bounded path",
        )


def test_public_error_message_budget_precedes_strip(monkeypatch):
    class ExplodingMessage(str):
        def strip(self, *_args, **_kwargs):
            raise AssertionError("over-limit error message was stripped")

        def isprintable(self):
            raise AssertionError("over-limit error message was scanned for printability")

        def splitlines(self, *_args, **_kwargs):
            raise AssertionError("over-limit error message was split into lines")

    monkeypatch.setattr(state_module, "str", ExplodingMessage, raising=False)
    monkeypatch.setattr(state_module, "is_canonical_json_pointer", lambda _value: True)
    with pytest.raises(ValueError, match="message must be bounded nonblank text"):
        TaskStateError(
            TaskStateErrorCode.INVALID_VALUE,
            ExplodingMessage(""),
            ExplodingMessage("x" * 257),
        )


@pytest.mark.parametrize(
    "source",
    ["x" * 4097, "é" * 2049],
    ids=("codepoint-over-limit", "multibyte-over-limit"),
)
def test_nested_string_budget_precedes_c1_parser(monkeypatch, source):
    raw = _to_executing().to_mapping()
    raw["program"]["operations"] = [
        {
            "schema_version": 1,
            "id": "operation-1",
            "op": "noop",
            "source": source,
        }
    ]

    def explode_parser(_value):
        raise AssertionError("C1 parser ran before nested string budget")

    monkeypatch.setattr(ModelProgram, "from_mapping", explode_parser)
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/program",
    )


def test_nested_unsafe_negative_integer_precedes_c1_parser(monkeypatch):
    raw = _to_executing().to_mapping()
    raw["program"]["schema_version"] = -(state_module.MAX_SAFE_JSON_INTEGER + 1)

    def explode_parser(_value):
        raise AssertionError("C1 parser ran before nested integer range check")

    def explode_abs(_value):
        raise AssertionError("nested unsafe integer reached abs")

    monkeypatch.setattr(ModelProgram, "from_mapping", explode_parser)
    monkeypatch.setattr(state_module, "abs", explode_abs, raising=False)
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/program/schema_version",
    )


@pytest.mark.parametrize("target", ["json", "measurement", "schema-version"])
def test_negative_integer_range_precedes_abs(monkeypatch, target):
    def explode_abs(_value):
        raise AssertionError("unsafe negative integer reached abs")

    monkeypatch.setattr(state_module, "abs", explode_abs, raising=False)
    value = -(state_module.MAX_SAFE_JSON_INTEGER + 1)
    path = {
        "json": "/expected",
        "measurement": "/delta",
        "schema-version": "/schema_version",
    }[target]
    with pytest.raises(TaskStateError) as caught:
        if target == "json":
            state_module._freeze_json(value, path)
        elif target == "measurement":
            state_module._measurement(value, path, nonnegative=False)
        else:
            state_module._schema_version(value)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        path,
    )


def test_root_mapping_population_budget_precedes_key_snapshot(monkeypatch):
    def explode_key_work(*_args, **_kwargs):
        raise AssertionError("over-limit mapping reached key processing")

    raw = {f"unknown-{index}": None for index in range(65)}
    allowed = set()
    required = set()
    monkeypatch.setattr(state_module, "tuple", explode_key_work, raising=False)
    monkeypatch.setattr(state_module, "set", explode_key_work, raising=False)
    monkeypatch.setattr(state_module, "sorted", explode_key_work, raising=False)
    monkeypatch.setattr(state_module, "_error_pointer", explode_key_work)
    with pytest.raises(TaskStateError) as caught:
        state_module._mapping(raw, "", allowed=allowed, required=required)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "",
    )


def test_root_mapping_population_accepts_exact_private_boundary():
    raw = {f"field-{index}": None for index in range(64)}
    parsed = state_module._mapping(raw, "", allowed=set(raw), required=set())
    assert parsed == raw
    assert parsed is not raw


def test_text_accepts_exact_character_boundary():
    value = "x" * 256
    assert state_module._text(value, "/text") == value


@pytest.mark.parametrize("target", ["json", "measurement", "schema-version"])
def test_negative_integer_exact_lower_bound_is_not_out_of_range(target):
    value = -state_module.MAX_SAFE_JSON_INTEGER
    if target == "json":
        assert state_module._freeze_json(value, "/expected") == value
    elif target == "measurement":
        assert state_module._measurement(value, "/delta", nonnegative=False) == value
    else:
        with pytest.raises(TaskStateError) as caught:
            state_module._schema_version(value)
        assert (caught.value.code, caught.value.path) == (
            TaskStateErrorCode.UNSUPPORTED_VERSION,
            "/schema_version",
        )


def test_public_error_and_nested_scalar_exact_budgets_are_accepted():
    message = "m" * 256
    error = TaskStateError(
        TaskStateErrorCode.INVALID_VALUE,
        "/" + "x" * 255,
        message,
    )
    assert len(error.path) <= 256
    assert error.message == message

    state_module._guard_exact_nested_containers(
        {
            "string": "x" * 4096,
            "multibyte": "é" * 2048,
            "integer": -state_module.MAX_SAFE_JSON_INTEGER,
        },
        "/nested",
    )


@pytest.mark.parametrize(
    "value",
    ["x" * 257, "é" * 257],
    ids=("ascii-over-limit", "multibyte-over-limit"),
)
def test_enum_length_precedes_enum_conversion(monkeypatch, value):
    def explode_missing(_enum_type, _value):
        raise AssertionError("over-limit enum reached conversion")

    monkeypatch.setattr(TaskStatus, "_missing_", classmethod(explode_missing))
    with pytest.raises(TaskStateError) as caught:
        next_action_for(value)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_VALUE,
        "/status",
    )


@pytest.mark.parametrize(
    "value",
    ["x" * 256, "é" * 256],
    ids=("ascii-exact", "multibyte-exact"),
)
def test_enum_exact_character_boundary_reaches_conversion(monkeypatch, value):
    def return_created(enum_type, actual):
        assert actual == value
        return enum_type.CREATED

    monkeypatch.setattr(TaskStatus, "_missing_", classmethod(return_created))
    assert next_action_for(value) is NextAction.REQUEST_PLAN


def test_root_mapping_overlong_unknown_keys_skip_sort(monkeypatch):
    raw = _task().to_mapping()
    prefix = "x" * 257
    for index in range(64 - len(raw)):
        raw[f"{prefix}{index:02d}"] = None
    assert len(raw) == 64

    def explode_sort(*_args, **_kwargs):
        raise AssertionError("overlong unknown fields reached sorting")

    monkeypatch.setattr(state_module, "sorted", explode_sort, raising=False)
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.UNKNOWN_FIELD,
        "/__truncated__",
    )


def test_root_mapping_short_unknown_fields_remain_lexicographic():
    raw = _task().to_mapping()
    raw["zeta"] = None
    raw["alpha"] = None
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.UNKNOWN_FIELD,
        "/alpha",
    )


@pytest.mark.parametrize(
    ("target", "enum_type", "expected_path"),
    [
        ("status", TaskStatus, "/status"),
        ("event", TaskEvent, "/event"),
        ("reasoning-owner", ReasoningOwner, "/reasoning_owner"),
        ("criterion-outcome", state_module.CriterionOutcome, "/outcome"),
    ],
)
def test_enum_class_proxy_is_rejected_before_native_operations(target, enum_type, expected_path):
    class EnumClassProxy:
        @property
        def __class__(self):
            return enum_type

        def __hash__(self):
            raise AssertionError("enum class proxy reached hashing")

    proxy = EnumClassProxy()
    with pytest.raises(TaskStateError) as caught:
        if target == "status":
            next_action_for(proxy)
        elif target == "event":
            transition_task(_task(), proxy)
        elif target == "reasoning-owner":
            new_task_run(
                task_id=TASK_ID,
                project_id=PROJECT_ID,
                base_revision=BASE_REVISION,
                reasoning_owner=proxy,
                review_policy=ReviewPolicy.AUTO_COMMIT,
            )
        else:
            CriterionVerdict(
                criterion_id="class-proxy",
                required=True,
                outcome=proxy,
                message="Proxy values are rejected",
            )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        expected_path,
    )


def test_public_error_rejects_code_class_proxy_before_value_access():
    class CodeClassProxy:
        @property
        def __class__(self):
            return TaskStateErrorCode

        @property
        def value(self):
            raise AssertionError("error code class proxy reached value access")

    with pytest.raises(TypeError, match="code must be a TaskStateErrorCode"):
        TaskStateError(CodeClassProxy(), "", "Proxy codes are rejected")


# S3-5 durable review contract REDs.  These intentionally exercise the public
# state surface through ``state_module`` so an absent symbol is an assertion
# failure in the focused test, rather than a collection-time import failure.
DRAFT_ID = "draft_11111111111111111111111111111111"
OTHER_DRAFT_ID = "draft_22222222222222222222222222222222"
OTHER_TASK_ID = "task_22222222222222222222222222222222"
OTHER_PROJECT_ID = "project_22222222222222222222222222222222"
BASE_GENERATION = 7
BASE_MANIFEST_SHA256 = "c" * 64


def _review_types():
    review_policy = getattr(state_module, "ReviewPolicy", None)
    review_draft = getattr(state_module, "ReviewDraft", None)
    assert review_policy is not None, "S3-5 ReviewPolicy is missing"
    assert review_draft is not None, "S3-5 ReviewDraft is missing"
    return review_policy, review_draft


def _review_event(name: str):
    event = getattr(TaskEvent, name, None)
    assert event is not None, f"S3-5 TaskEvent.{name} is missing"
    return event


def _review_draft(**changes):
    _, review_draft = _review_types()
    values = {
        "id": DRAFT_ID,
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "base_revision": BASE_REVISION,
        "base_generation": BASE_GENERATION,
        "base_manifest_sha256": BASE_MANIFEST_SHA256,
        "revision_id": CANDIDATE_REVISION,
        "manifest_sha256": "a" * 64,
        "verification_id": "verification_0123456789abcdef0123456789abcdef",
        "acceptance_id": "acceptance-1",
        "observation_digest": "b" * 64,
    }
    values.update(changes)
    return review_draft(**values)


def _policy_task(policy_name: str):
    review_policy, _ = _review_types()
    return new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=review_policy(policy_name),
    )


def _policy_to_verifying(policy_name: str):
    task = transition_task(_policy_task(policy_name), TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=_program())
    task = transition_task(task, TaskEvent.START_VALIDATION)
    task = transition_task(
        task,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=CANDIDATE_REVISION,
    )
    return transition_task(task, TaskEvent.COMPLETE_EXECUTION)


def _to_preparing_review():
    return transition_task(
        _policy_to_verifying("require_review"),
        _review_event("PREPARE_REVIEW"),
        verification=_report(passed=True),
        draft=_review_draft(),
    )


def _to_awaiting_review():
    return transition_task(_to_preparing_review(), _review_event("PUBLISH_DRAFT"))


def test_review_policy_is_closed_and_required_without_a_hidden_default():
    review_policy = getattr(state_module, "ReviewPolicy", None)
    assert review_policy is not None, "S3-5 ReviewPolicy is missing"
    assert {item.value for item in review_policy} == {"auto_commit", "require_review"}

    parameters = inspect.signature(new_task_run).parameters
    assert tuple(parameters) == (
        "task_id",
        "project_id",
        "base_revision",
        "reasoning_owner",
        "review_policy",
        "creation_digest",
    )
    assert parameters["review_policy"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        new_task_run(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        )
    with pytest.raises(TaskStateError) as caught:
        new_task_run(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy="auto_commit",
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TYPE,
        "/review_policy",
    )


def test_review_draft_has_exact_path_free_frozen_schema_and_round_trips():
    _, review_draft = _review_types()
    draft = _review_draft()
    expected = {
        "schema_version": 1,
        "id": DRAFT_ID,
        "task_id": TASK_ID,
        "project_id": PROJECT_ID,
        "base_revision": BASE_REVISION,
        "base_generation": BASE_GENERATION,
        "base_manifest_sha256": BASE_MANIFEST_SHA256,
        "revision_id": CANDIDATE_REVISION,
        "manifest_sha256": "a" * 64,
        "verification_id": "verification_0123456789abcdef0123456789abcdef",
        "acceptance_id": "acceptance-1",
        "observation_digest": "b" * 64,
    }

    assert draft.to_mapping() == expected
    assert review_draft.from_mapping(deepcopy(expected)) == draft
    assert not ({"path", "receipt", "lease", "session", "handle"} & set(expected))
    with pytest.raises(FrozenInstanceError):
        draft.id = OTHER_DRAFT_ID


def test_review_draft_id_is_derived_one_to_one_from_revision_suffix():
    _, review_draft = _review_types()
    malformed = _review_draft().to_mapping()
    malformed["id"] = OTHER_DRAFT_ID
    with pytest.raises(TaskStateError) as caught:
        review_draft.from_mapping(malformed)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/id",
    )


def test_review_status_event_and_next_action_enums_are_exact():
    assert {item.value for item in TaskStatus} == {
        "created",
        "needs_plan",
        "program_ready",
        "validating_program",
        "executing",
        "verifying",
        "committing",
        "preparing_review",
        "awaiting_user_review",
        "accepting_draft",
        "rolling_back",
        "needs_input",
        "recovery_required",
        "cleanup_required",
        "succeeded",
        "failed",
        "rejected",
        "cancel_requested",
        "cancelling",
        "cancelled",
    }
    assert {item.value for item in TaskEvent} == {
        "request_plan",
        "submit_program",
        "start_validation",
        "validate_program",
        "reject_program",
        "complete_execution",
        "fail_execution",
        "pass_verification",
        "prepare_review",
        "publish_draft",
        "accept_draft",
        "reject_draft",
        "abort_accept",
        "confirm_draft_uncommitted",
        "fail_verification",
        "commit",
        "complete_rollback",
        "request_input",
        "require_recovery",
        "require_cleanup",
        "confirm_committed",
        "confirm_uncommitted",
        "confirm_pre_candidate",
        "request_cancel",
        "start_cancellation",
        "confirm_cancelled",
    }
    assert {item.value for item in NextAction} == {
        "request_plan",
        "submit_program",
        "validate_program",
        "provide_input",
        "reconcile",
        "cleanup",
        "review_draft",
        "wait",
        "none",
    }
    expected = {
        "preparing_review": "reconcile",
        "awaiting_user_review": "review_draft",
        "accepting_draft": "reconcile",
        "rejected": "none",
        "cancel_requested": "reconcile",
        "cancelling": "reconcile",
        "cancelled": "none",
    }
    for status, action in expected.items():
        assert next_action_for(TaskStatus(status)) is NextAction(action)


def test_policy_gates_auto_commit_and_review_transition_paths():
    auto_verifying = _policy_to_verifying("auto_commit")
    committing = transition_task(
        auto_verifying,
        TaskEvent.PASS_VERIFICATION,
        verification=_report(passed=True),
    )
    assert committing.status is TaskStatus.COMMITTING
    assert committing.draft is None

    with pytest.raises(TaskStateError) as caught:
        transition_task(
            auto_verifying,
            _review_event("PREPARE_REVIEW"),
            verification=_report(passed=True),
            draft=_review_draft(),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )

    review_verifying = _policy_to_verifying("require_review")
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            review_verifying,
            TaskEvent.PASS_VERIFICATION,
            verification=_report(passed=True),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )

    preparing = transition_task(
        review_verifying,
        _review_event("PREPARE_REVIEW"),
        verification=_report(passed=True),
        draft=_review_draft(),
    )
    assert preparing.status.value == "preparing_review"
    assert preparing.draft == _review_draft()
    awaiting = transition_task(preparing, _review_event("PUBLISH_DRAFT"))
    assert awaiting.status.value == "awaiting_user_review"
    assert awaiting.next_action.value == "review_draft"


@pytest.mark.parametrize(
    ("field", "value", "path"),
    [
        ("task_id", OTHER_TASK_ID, "/draft/task_id"),
        ("project_id", OTHER_PROJECT_ID, "/draft/project_id"),
        ("base_revision", OTHER_REVISION, "/draft/base_revision"),
        ("manifest_sha256", "d" * 64, "/draft/manifest_sha256"),
        (
            "verification_id",
            "verification_22222222222222222222222222222222",
            "/draft/verification_id",
        ),
        ("acceptance_id", "other-acceptance", "/draft/acceptance_id"),
        ("observation_digest", "d" * 64, "/draft/observation_digest"),
    ],
)
def test_prepare_review_binds_draft_to_task_program_and_passing_report(field, value, path):
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            _policy_to_verifying("require_review"),
            _review_event("PREPARE_REVIEW"),
            verification=_report(passed=True),
            draft=_review_draft(**{field: value}),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        path,
    )


def test_review_history_and_policy_cannot_be_forged_during_round_trip():
    awaiting = _to_awaiting_review()
    raw = awaiting.to_mapping()
    raw["review_policy"] = "auto_commit"
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/review_policy",
    )

    raw = awaiting.to_mapping()
    raw["draft"] = None
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/draft",
    )

    raw = awaiting.to_mapping()
    raw["draft"]["verification_id"] = "verification_22222222222222222222222222222222"
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(raw)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/draft/verification_id",
    )


def test_reject_is_a_normal_terminal_decision_that_preserves_review_evidence():
    awaiting = _to_awaiting_review()
    rejected = transition_task(awaiting, _review_event("REJECT_DRAFT"))

    assert rejected.status.value == "rejected"
    assert rejected.next_action is NextAction.NONE
    assert rejected.draft == awaiting.draft
    assert rejected.verification_reports == awaiting.verification_reports
    assert rejected.committed_revision is None
    assert rejected.last_error is None
    assert TaskRun.from_mapping(rejected.to_mapping()) == rejected
    with pytest.raises(TaskStateError) as caught:
        transition_task(rejected, TaskEvent.REQUEST_PLAN)
    assert caught.value.code is TaskStateErrorCode.TERMINAL_STATE


def test_accept_abort_and_reviewed_success_preserve_the_immutable_draft():
    awaiting = _to_awaiting_review()
    accepting = transition_task(awaiting, _review_event("ACCEPT_DRAFT"))
    assert accepting.status.value == "accepting_draft"
    assert accepting.next_action is NextAction.RECONCILE

    aborted = transition_task(accepting, _review_event("ABORT_ACCEPT"))
    assert aborted.status.value == "awaiting_user_review"
    assert aborted.draft == awaiting.draft
    assert aborted.last_error is None

    accepting = transition_task(aborted, _review_event("ACCEPT_DRAFT"))
    succeeded = transition_task(
        accepting,
        TaskEvent.COMMIT,
        committed_revision=CANDIDATE_REVISION,
    )
    assert succeeded.status is TaskStatus.SUCCEEDED
    assert succeeded.committed_revision == succeeded.draft.revision_id
    assert succeeded.draft == awaiting.draft
    assert TaskRun.from_mapping(succeeded.to_mapping()) == succeeded


def test_reviewed_success_cannot_bypass_explicit_acceptance_provenance():
    recovery = transition_task(
        _to_preparing_review(),
        TaskEvent.REQUIRE_RECOVERY,
        error=_error(),
    )
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            recovery,
            TaskEvent.CONFIRM_COMMITTED,
            committed_revision=CANDIDATE_REVISION,
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )

    forged = recovery.to_mapping()
    forged["status"] = TaskStatus.SUCCEEDED.value
    forged["committed_revision"] = CANDIDATE_REVISION
    forged["last_error"] = None
    forged["transitions"].append(
        {
            "schema_version": 1,
            "sequence": len(forged["transitions"]) + 1,
            "event": TaskEvent.CONFIRM_COMMITTED.value,
            "from_status": TaskStatus.RECOVERY_REQUIRED.value,
            "to_status": TaskStatus.SUCCEEDED.value,
        }
    )
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )


@pytest.mark.parametrize("attention_event", [TaskEvent.REQUIRE_RECOVERY, TaskEvent.REQUIRE_CLEANUP])
@pytest.mark.parametrize("origin", ["preparing", "accepting"])
def test_review_attention_can_only_confirm_exact_draft_uncommitted_back_to_awaiting(
    origin, attention_event
):
    task = _to_preparing_review()
    if origin == "accepting":
        task = transition_task(task, _review_event("PUBLISH_DRAFT"))
        task = transition_task(task, _review_event("ACCEPT_DRAFT"))
    attention = transition_task(task, attention_event, error=_error())
    assert attention.last_error == _error()

    restored = transition_task(
        attention,
        _review_event("CONFIRM_DRAFT_UNCOMMITTED"),
    )
    assert restored.status.value == "awaiting_user_review"
    assert restored.draft == _review_draft()
    assert restored.last_error is None
    assert restored.next_action.value == "review_draft"
    assert TaskRun.from_mapping(restored.to_mapping()) == restored

    ordinary_attention = transition_task(
        transition_task(
            _policy_to_verifying("auto_commit"),
            TaskEvent.PASS_VERIFICATION,
            verification=_report(passed=True),
        ),
        attention_event,
        error=_error(),
    )
    with pytest.raises(TaskStateError) as caught:
        transition_task(
            ordinary_attention,
            _review_event("CONFIRM_DRAFT_UNCOMMITTED"),
        )
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )


def _cancel_status(value: str) -> TaskStatus:
    return TaskStatus(value)


def _cancel_event(value: str) -> TaskEvent:
    return TaskEvent(value)


@pytest.mark.parametrize(
    "origin",
    [
        TaskStatus.CREATED,
        TaskStatus.NEEDS_PLAN,
        TaskStatus.PROGRAM_READY,
        TaskStatus.NEEDS_INPUT,
    ],
)
def test_durable_cancellation_contract_cancels_idle_states_in_one_transition(origin):
    task = _matrix_task(origin)

    cancelled = transition_task(task, _cancel_event("request_cancel"))

    assert cancelled.status is _cancel_status("cancelled")
    assert cancelled.next_action is NextAction.NONE
    assert cancelled.program == task.program
    assert cancelled.candidate_revision == task.candidate_revision
    assert cancelled.draft == task.draft
    assert cancelled.verification_reports == task.verification_reports
    assert cancelled.artifacts == task.artifacts
    assert cancelled.committed_revision is None
    assert cancelled.last_error is None
    assert cancelled.transitions[:-1] == task.transitions
    assert cancelled.transitions[-1].event is _cancel_event("request_cancel")
    assert TaskRun.from_mapping(cancelled.to_mapping()) == cancelled


@pytest.mark.parametrize(
    "origin",
    [
        TaskStatus.VALIDATING_PROGRAM,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
        TaskStatus.COMMITTING,
        TaskStatus.PREPARING_REVIEW,
        TaskStatus.ACCEPTING_DRAFT,
    ],
)
def test_durable_cancellation_contract_persists_active_intent_before_cancelling(origin):
    task = _matrix_task(origin)

    requested = transition_task(task, _cancel_event("request_cancel"))
    cancelling = transition_task(requested, _cancel_event("start_cancellation"))
    cancelled = transition_task(cancelling, _cancel_event("confirm_cancelled"))

    assert requested.status is _cancel_status("cancel_requested")
    assert requested.next_action is NextAction.RECONCILE
    assert cancelling.status is _cancel_status("cancelling")
    assert cancelling.next_action is NextAction.RECONCILE
    assert cancelled.status is _cancel_status("cancelled")
    assert cancelled.next_action is NextAction.NONE
    for value in (requested, cancelling, cancelled):
        assert value.program == task.program
        assert value.candidate_revision == task.candidate_revision
        assert value.draft == task.draft
        assert value.steps == task.steps
        assert value.verification_reports == task.verification_reports
        assert value.artifacts == task.artifacts
        assert value.committed_revision is None
        assert value.last_error is None
        assert TaskRun.from_mapping(value.to_mapping()) == value


@pytest.mark.parametrize(
    ("origin", "forbidden_confirmation"),
    [
        (TaskStatus.VALIDATING_PROGRAM, TaskEvent.CONFIRM_PRE_CANDIDATE),
        (TaskStatus.EXECUTING, TaskEvent.CONFIRM_UNCOMMITTED),
        (TaskStatus.PREPARING_REVIEW, TaskEvent.CONFIRM_DRAFT_UNCOMMITTED),
    ],
)
@pytest.mark.parametrize(
    "attention_event",
    [TaskEvent.REQUIRE_RECOVERY, TaskEvent.REQUIRE_CLEANUP],
)
def test_durable_cancellation_contract_attention_can_only_finish_or_confirm_commit(
    origin, forbidden_confirmation, attention_event
):
    requested = transition_task(_matrix_task(origin), _cancel_event("request_cancel"))
    cancelling = transition_task(requested, _cancel_event("start_cancellation"))
    attention = transition_task(cancelling, attention_event, error=_error())

    with pytest.raises(TaskStateError) as caught:
        transition_task(attention, forbidden_confirmation)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVALID_TRANSITION,
        "/event",
    )

    cancelled = transition_task(attention, _cancel_event("confirm_cancelled"))
    assert cancelled.status is _cancel_status("cancelled")
    assert cancelled.last_error is None
    assert cancelled.candidate_revision == cancelling.candidate_revision
    assert cancelled.draft == cancelling.draft
    assert TaskRun.from_mapping(cancelled.to_mapping()) == cancelled


@pytest.mark.parametrize("origin", [TaskStatus.COMMITTING, TaskStatus.ACCEPTING_DRAFT])
def test_durable_cancellation_contract_commit_wins_only_with_existing_verified_evidence(origin):
    requested = transition_task(_matrix_task(origin), _cancel_event("request_cancel"))
    cancelling = transition_task(requested, _cancel_event("start_cancellation"))

    succeeded = transition_task(
        cancelling,
        TaskEvent.CONFIRM_COMMITTED,
        committed_revision=CANDIDATE_REVISION,
    )

    assert succeeded.status is TaskStatus.SUCCEEDED
    assert succeeded.committed_revision == CANDIDATE_REVISION
    assert succeeded.verification_reports == cancelling.verification_reports
    assert TaskRun.from_mapping(succeeded.to_mapping()) == succeeded


@pytest.mark.parametrize(
    "origin",
    [
        TaskStatus.AWAITING_USER_REVIEW,
        TaskStatus.ROLLING_BACK,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.CLEANUP_REQUIRED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.REJECTED,
    ],
)
def test_durable_cancellation_contract_rejects_review_rollback_attention_and_terminals(origin):
    task = _matrix_task(origin)

    with pytest.raises(TaskStateError) as caught:
        transition_task(task, _cancel_event("request_cancel"))

    expected = (
        TaskStateErrorCode.TERMINAL_STATE
        if origin in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.REJECTED}
        else TaskStateErrorCode.INVALID_TRANSITION
    )
    assert caught.value.code is expected


def test_durable_cancellation_contract_rejects_forged_confirmation_without_provenance():
    attention = _matrix_task(TaskStatus.RECOVERY_REQUIRED)
    forged = attention.to_mapping()
    forged["status"] = "cancelled"
    forged["last_error"] = None
    forged["transitions"].append(
        {
            "schema_version": 1,
            "sequence": len(forged["transitions"]) + 1,
            "event": "confirm_cancelled",
            "from_status": "recovery_required",
            "to_status": "cancelled",
        }
    )

    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged)

    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )


def test_durable_cancellation_contract_program_presence_matches_submission_provenance():
    cancelled = transition_task(
        _matrix_task(TaskStatus.NEEDS_PLAN),
        _cancel_event("request_cancel"),
    )
    forged = cancelled.to_mapping()
    forged["program"] = _program().to_mapping()

    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged)

    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/program",
    )


def _task_with_full_ordinary_transition_history(*, review: bool) -> TaskRun:
    if review:
        task = _to_awaiting_review()
        while len(task.transitions) < 128:
            event = (
                TaskEvent.ACCEPT_DRAFT
                if task.status is TaskStatus.AWAITING_USER_REVIEW
                else TaskEvent.ABORT_ACCEPT
            )
            task = transition_task(task, event)
        assert task.status is TaskStatus.ACCEPTING_DRAFT
        return task

    task = _matrix_task(TaskStatus.NEEDS_INPUT)
    while len(task.transitions) < 128:
        if task.status is TaskStatus.NEEDS_INPUT:
            task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=_program())
        elif task.status is TaskStatus.PROGRAM_READY:
            task = transition_task(task, TaskEvent.START_VALIDATION)
        else:
            assert task.status is TaskStatus.VALIDATING_PROGRAM
            task = transition_task(
                task,
                TaskEvent.REJECT_PROGRAM,
                error=_error(needs_input=True),
            )
    assert task.status is TaskStatus.PROGRAM_READY
    return task


def test_durable_cancellation_contract_reserves_a_tail_after_128_ordinary_transitions():
    idle = _task_with_full_ordinary_transition_history(review=False)
    with pytest.raises(TaskStateError) as caught:
        transition_task(idle, TaskEvent.START_VALIDATION)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.BUDGET_EXCEEDED,
        "/transitions",
    )

    forged = idle.to_mapping()
    forged["status"] = TaskStatus.VALIDATING_PROGRAM.value
    forged["transitions"].append(
        TaskTransitionRecord(
            sequence=129,
            event=TaskEvent.START_VALIDATION,
            from_status=TaskStatus.PROGRAM_READY,
            to_status=TaskStatus.VALIDATING_PROGRAM,
        ).to_mapping()
    )
    with pytest.raises(TaskStateError) as caught:
        TaskRun.from_mapping(forged)
    assert (caught.value.code, caught.value.path) == (
        TaskStateErrorCode.INVARIANT_VIOLATION,
        "/transitions",
    )

    idle_cancelled = transition_task(idle, _cancel_event("request_cancel"))
    assert len(idle_cancelled.transitions) == 129
    assert idle_cancelled.status is _cancel_status("cancelled")

    active = _task_with_full_ordinary_transition_history(review=True)
    requested = transition_task(active, _cancel_event("request_cancel"))
    cancelling = transition_task(requested, _cancel_event("start_cancellation"))
    cleanup = transition_task(cancelling, TaskEvent.REQUIRE_CLEANUP, error=_error())
    recovery = transition_task(cleanup, TaskEvent.REQUIRE_RECOVERY, error=_error())
    cancelled = transition_task(recovery, _cancel_event("confirm_cancelled"))

    assert len(cancelled.transitions) == 133
    assert len(cancelled.transitions) <= MAX_TRANSITION_RECORDS
    assert cancelled.status is _cancel_status("cancelled")
    assert TaskRun.from_mapping(cancelled.to_mapping()) == cancelled
