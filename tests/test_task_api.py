"""Public Task API contract, ingress, routing, and capability tests."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from collections.abc import Mapping
from types import MappingProxyType

import pytest

import vibecad.application.task_api as task_api_module
from vibecad.application.task_api import (
    TaskApi,
    TaskApiErrorCode,
    TaskServicePort,
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.execution.registry import DEFAULT_OPERATION_REGISTRY, OperationRegistry
from vibecad.execution.revisions import (
    ProjectHead,
    RevisionArtifactRef,
    RevisionRef,
)
from vibecad.workflow.contracts import AcceptanceSpec, ErrorCategory, ModelProgram, StepError
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.revert import build_revert_binding
from vibecad.workflow.state import (
    CriterionOutcome,
    CriterionVerdict,
    ReasoningOwner,
    ReviewDraft,
    ReviewPolicy,
    TaskEvent,
    TaskRun,
    TaskStatus,
    VerificationReport,
    new_task_run,
    transition_task,
)
from vibecad.workflow.store import StoredTaskRun

TASK_ID = "task_0123456789abcdef0123456789abcdef"
OTHER_TASK_ID = "task_11111111111111111111111111111111"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
CANDIDATE_REVISION = "revision_11111111111111111111111111111111"
CREATE_KEY = "task_create_0123456789abcdef0123456789abcdef"
REVERT_KEY = "revert_create_0123456789abcdef0123456789abcdef"
KEYED_TASK_ID = "task_e9f9dc52c8f75cd72feddee2648564b8"
CREATION_DIGEST = "e9f9dc52c8f75cd72feddee2648564b8b4bf0b07836368165d3a0c1fedeee1ef"
COLLISION_DIGEST = CREATION_DIGEST[:32] + "f" * 32
DRAFT_ID = "draft_11111111111111111111111111111111"
OTHER_CANDIDATE_REVISION = "revision_22222222222222222222222222222222"
OTHER_DRAFT_ID = "draft_22222222222222222222222222222222"
ERROR_MESSAGES = {
    "missing_field": "A required request field is missing.",
    "unknown_field": "The request contains an unknown field.",
    "unsupported_version": "The request schema version is not supported.",
    "invalid_type": "A request value has an invalid type.",
    "invalid_value": "A request value is invalid.",
    "budget_exceeded": "The request exceeds a resource budget.",
    "invalid_input": "The request is invalid.",
    "unsupported_reasoning_owner": "The requested reasoning owner is not supported.",
    "invalid_state": "The task is not ready for this operation.",
    "not_found": "The task record was not found.",
    "conflict": "The task record changed concurrently.",
    "store_failure": "The task record operation failed.",
    "lease_unavailable": "The project write lease is unavailable.",
    "resource_exhausted": "The application resource capacity is exhausted.",
    "recovery_required": "The task requires explicit reconciliation.",
    "internal_error": "The request could not be completed.",
}


def _program(*, task_id: str = TASK_ID) -> ModelProgram:
    return ModelProgram(
        task_id=task_id,
        base_revision=BASE_REVISION,
        operations=(),
        acceptance=AcceptanceSpec(id="acceptance-api", criteria=()),
    )


def _program_json(*, task_id: str = TASK_ID) -> str:
    return json.dumps(
        _program(task_id=task_id).to_mapping(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _step_error(*, needs_input: bool = False) -> StepError:
    return StepError(
        category=ErrorCategory.RUNTIME,
        code="injected_failure",
        message="The injected operation failed.",
        retryable=False,
        needs_input=needs_input,
        related_objects=(),
        diagnostic_artifacts=(),
    )


def _report(*, candidate_revision: str = CANDIDATE_REVISION) -> VerificationReport:
    return VerificationReport(
        id="verification_0123456789abcdef0123456789abcdef",
        acceptance_id="acceptance-api",
        candidate_revision=candidate_revision,
        manifest_sha256="a" * 64,
        observation_digest="b" * 64,
        passed=True,
        verdicts=(
            CriterionVerdict(
                criterion_id="geometry",
                required=True,
                outcome=CriterionOutcome.PASS,
                message="Geometry matched.",
            ),
        ),
    )


def _created(
    *,
    task_id: str = TASK_ID,
    project_id: str = PROJECT_ID,
    review_policy: ReviewPolicy = ReviewPolicy.AUTO_COMMIT,
) -> TaskRun:
    return new_task_run(
        task_id=task_id,
        project_id=project_id,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=review_policy,
    )


def _keyed_created(
    *,
    project_id: str = PROJECT_ID,
    review_policy: ReviewPolicy = ReviewPolicy.AUTO_COMMIT,
) -> TaskRun:
    return new_task_run(
        task_id=KEYED_TASK_ID,
        project_id=project_id,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=review_policy,
        creation_digest=CREATION_DIGEST,
    )


def _keyed_needs_plan(
    *,
    project_id: str = PROJECT_ID,
    review_policy: ReviewPolicy = ReviewPolicy.AUTO_COMMIT,
) -> TaskRun:
    return transition_task(
        _keyed_created(project_id=project_id, review_policy=review_policy),
        TaskEvent.REQUEST_PLAN,
    )


def _create_request(
    *,
    create_key: object = CREATE_KEY,
    project_id: object = PROJECT_ID,
    review_policy: object = "auto_commit",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "create_key": create_key,
        "project_id": project_id,
        "review_policy": review_policy,
    }


def _revert_request(
    *,
    revert_key: object = REVERT_KEY,
    project_id: object = PROJECT_ID,
    source_revision: object = CANDIDATE_REVISION,
    expected_head: object = BASE_REVISION,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "revert_key": revert_key,
        "project_id": project_id,
        "source_revision": source_revision,
        "expected_head": expected_head,
    }


def _bound_revert_task() -> TaskRun:
    source = RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=OTHER_CANDIDATE_REVISION,
        manifest_sha256="a" * 64,
        model=RevisionArtifactRef(
            id="artifact_33333333333333333333333333333333",
            name="model.FCStd",
            format="fcstd",
            sha256="b" * 64,
            size_bytes=101,
        ),
        artifacts=(
            RevisionArtifactRef(
                id="artifact_44444444444444444444444444444444",
                name="model.step",
                format="step",
                sha256="c" * 64,
                size_bytes=202,
            ),
        ),
    )
    head = ProjectHead(
        project_id=PROJECT_ID,
        generation=2,
        revision_id=BASE_REVISION,
        manifest_sha256="d" * 64,
    )
    binding = build_revert_binding(
        revert_key=REVERT_KEY,
        project_id=PROJECT_ID,
        source_revision=source,
        expected_head=head,
    )
    task = new_task_run(
        task_id=binding.task_id,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
        creation_digest=binding.creation_digest,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    return transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=binding.program)


def _forged_revert_needs_plan() -> TaskRun:
    bound = _bound_revert_task()
    return transition_task(
        new_task_run(
            task_id=bound.id,
            project_id=bound.project_id,
            base_revision=bound.base_revision,
            reasoning_owner=bound.reasoning_owner,
            review_policy=bound.review_policy,
            creation_digest=bound.creation_digest,
        ),
        TaskEvent.REQUEST_PLAN,
    )


def test_create_key_derives_identity_and_accepts_a_progressed_replay_snapshot():
    replay = transition_task(
        new_task_run(
            task_id=KEYED_TASK_ID,
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
            creation_digest=CREATION_DIGEST,
        ),
        TaskEvent.REQUEST_PLAN,
    )
    replay = transition_task(
        replay,
        TaskEvent.SUBMIT_PROGRAM,
        program=_program(task_id=KEYED_TASK_ID),
    )
    port = _FakePort(StoredTaskRun(generation=3, task_run=replay))

    response = TaskApi(port=port).create_task(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "project_id": PROJECT_ID,
            "review_policy": "auto_commit",
        }
    )

    result = _assert_success(response)
    assert result["generation"] == 3
    assert result["task_run"]["id"] == KEYED_TASK_ID
    assert result["task_run"]["creation_digest"] == CREATION_DIGEST
    assert port.calls == [
        (
            "create_task",
            {
                "create_key": CREATE_KEY,
                "project_id": PROJECT_ID,
                "reasoning_owner": ReasoningOwner.EXTERNAL_PLAN,
                "review_policy": ReviewPolicy.AUTO_COMMIT,
            },
        )
    ]


def _review_draft(*, candidate_revision: str = CANDIDATE_REVISION) -> ReviewDraft:
    return ReviewDraft(
        id=f"draft_{candidate_revision.removeprefix('revision_')}",
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        base_generation=0,
        base_manifest_sha256="c" * 64,
        revision_id=candidate_revision,
        manifest_sha256="a" * 64,
        verification_id="verification_0123456789abcdef0123456789abcdef",
        acceptance_id="acceptance-api",
        observation_digest="b" * 64,
    )


def _review_task_at(
    status: TaskStatus,
    *,
    candidate_revision: str = CANDIDATE_REVISION,
) -> TaskRun:
    created = _created(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    needs_plan = transition_task(created, TaskEvent.REQUEST_PLAN)
    program_ready = transition_task(
        needs_plan,
        TaskEvent.SUBMIT_PROGRAM,
        program=_program(),
    )
    validating = transition_task(program_ready, TaskEvent.START_VALIDATION)
    executing = transition_task(
        validating,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=candidate_revision,
    )
    verifying = transition_task(executing, TaskEvent.COMPLETE_EXECUTION)
    preparing = transition_task(
        verifying,
        TaskEvent.PREPARE_REVIEW,
        verification=_report(candidate_revision=candidate_revision),
        draft=_review_draft(candidate_revision=candidate_revision),
    )
    awaiting = transition_task(preparing, TaskEvent.PUBLISH_DRAFT)
    accepting = transition_task(awaiting, TaskEvent.ACCEPT_DRAFT)
    rejected = transition_task(awaiting, TaskEvent.REJECT_DRAFT)
    succeeded = transition_task(
        accepting,
        TaskEvent.COMMIT,
        committed_revision=candidate_revision,
    )
    return {
        TaskStatus.PREPARING_REVIEW: preparing,
        TaskStatus.AWAITING_USER_REVIEW: awaiting,
        TaskStatus.ACCEPTING_DRAFT: accepting,
        TaskStatus.REJECTED: rejected,
        TaskStatus.SUCCEEDED: succeeded,
    }[status]


def _task_at(status: TaskStatus) -> TaskRun:
    review_status_values = {
        "preparing_review",
        "awaiting_user_review",
        "accepting_draft",
        "rejected",
    }
    if status.value in review_status_values:
        return _review_task_at(status)
    created = _created()
    needs_plan = transition_task(created, TaskEvent.REQUEST_PLAN)
    program_ready = transition_task(
        needs_plan,
        TaskEvent.SUBMIT_PROGRAM,
        program=_program(),
    )
    validating = transition_task(program_ready, TaskEvent.START_VALIDATION)
    executing = transition_task(
        validating,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=CANDIDATE_REVISION,
    )
    verifying = transition_task(executing, TaskEvent.COMPLETE_EXECUTION)
    committing = transition_task(
        verifying,
        TaskEvent.PASS_VERIFICATION,
        verification=_report(),
    )
    rolling_back = transition_task(
        executing,
        TaskEvent.FAIL_EXECUTION,
        error=_step_error(),
    )
    needs_input = transition_task(
        validating,
        TaskEvent.REJECT_PROGRAM,
        error=_step_error(needs_input=True),
    )
    cancel_requested = transition_task(executing, TaskEvent.REQUEST_CANCEL)
    cancelling = transition_task(cancel_requested, TaskEvent.START_CANCELLATION)
    cancelled = transition_task(cancelling, TaskEvent.CONFIRM_CANCELLED)
    cases = {
        TaskStatus.CREATED: created,
        TaskStatus.NEEDS_PLAN: needs_plan,
        TaskStatus.PROGRAM_READY: program_ready,
        TaskStatus.VALIDATING_PROGRAM: validating,
        TaskStatus.EXECUTING: executing,
        TaskStatus.VERIFYING: verifying,
        TaskStatus.COMMITTING: committing,
        TaskStatus.ROLLING_BACK: rolling_back,
        TaskStatus.NEEDS_INPUT: needs_input,
        TaskStatus.RECOVERY_REQUIRED: transition_task(
            validating,
            TaskEvent.REQUIRE_RECOVERY,
            error=_step_error(),
        ),
        TaskStatus.CLEANUP_REQUIRED: transition_task(
            validating,
            TaskEvent.REQUIRE_CLEANUP,
            error=_step_error(),
        ),
        TaskStatus.SUCCEEDED: transition_task(
            committing,
            TaskEvent.COMMIT,
            committed_revision=CANDIDATE_REVISION,
        ),
        TaskStatus.FAILED: transition_task(rolling_back, TaskEvent.COMPLETE_ROLLBACK),
        TaskStatus.CANCEL_REQUESTED: cancel_requested,
        TaskStatus.CANCELLING: cancelling,
        TaskStatus.CANCELLED: cancelled,
    }
    return cases[status]


def _stored(status: TaskStatus = TaskStatus.NEEDS_PLAN, *, generation: int = 0) -> StoredTaskRun:
    return StoredTaskRun(generation=generation, task_run=_task_at(status))


class _FakePort:
    def __init__(self, default: object) -> None:
        self.default = default
        self.responses: dict[str, object | list[object]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _reply(self, method: str, values: dict[str, object]) -> object:
        self.calls.append((method, values))
        response = self.responses.get(method, self.default)
        if type(response) is list:
            response = response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def create_task(self, **kwargs):
        return self._reply("create_task", kwargs)

    def revert_project(self, **kwargs):
        return self._reply("revert_project", kwargs)

    def get_task(self, **kwargs):
        return self._reply("get_task", kwargs)

    def list_tasks(self, **kwargs):
        return self._reply("list_tasks", kwargs)

    def get_task_events(self, **kwargs):
        return self._reply("get_task_events", kwargs)

    def submit_model_program(self, **kwargs):
        return self._reply("submit_model_program", kwargs)

    def continue_task(self, **kwargs):
        return self._reply("continue_task", kwargs)

    def reconcile_task(self, **kwargs):
        return self._reply("reconcile_task", kwargs)

    def cancel_task(self, **kwargs):
        return self._reply("cancel_task", kwargs)

    def accept_draft(self, **kwargs):
        return self._reply("accept_draft", kwargs)

    def reject_draft(self, **kwargs):
        return self._reply("reject_draft", kwargs)


class _HostileMapping(Mapping):
    def __getitem__(self, key):
        raise AssertionError("must not read hostile mapping")

    def __iter__(self):
        raise AssertionError("must not iterate hostile mapping")

    def __len__(self):
        raise AssertionError("must not size hostile mapping")


def _assert_success(response: dict[str, object]) -> dict[str, object]:
    assert set(response) == {"schema_version", "ok", "result", "error"}
    assert response["schema_version"] == 1
    assert response["ok"] is True
    assert type(response["result"]) is dict
    assert response["error"] is None
    assert json.loads(json.dumps(response, allow_nan=False)) == response
    return response["result"]


def _assert_error(response: dict[str, object], code: str, path: str) -> None:
    assert response == {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": code,
            "path": path,
            "message": ERROR_MESSAGES[code],
        },
    }
    assert json.loads(json.dumps(response, allow_nan=False)) == response


def _canonical_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _pad_field(request: dict[str, object], field: str, size: int) -> dict[str, object]:
    result = dict(request)
    result[field] = ""
    padding = size - _canonical_size(result)
    assert padding >= 0
    result[field] = "x" * padding
    assert _canonical_size(result) == size
    return result


def test_public_surface_error_taxonomies_and_method_signatures_are_closed():
    assert {item.value for item in TaskApiErrorCode} == set(ERROR_MESSAGES)
    assert {item.value for item in TaskServicePortErrorCode} == {
        "invalid_input",
        "unsupported_reasoning_owner",
        "invalid_state",
        "not_found",
        "conflict",
        "store_failure",
        "lease_unavailable",
        "resource_exhausted",
        "recovery_required",
    }
    api_methods = {
        name
        for name, value in vars(TaskApi).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert api_methods == {
        "create_task",
        "revert_project",
        "list_tasks",
        "get_task",
        "get_task_events",
        "submit_model_program",
        "resume_task",
        "cancel_task",
        "get_capabilities",
        "accept_draft",
        "reject_draft",
    }
    for name in api_methods:
        assert tuple(inspect.signature(getattr(TaskApi, name)).parameters) == ("self", "request")

    port_methods = {
        name
        for name, value in vars(TaskServicePort).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    assert port_methods == {
        "create_task",
        "revert_project",
        "list_tasks",
        "get_task",
        "get_task_events",
        "submit_model_program",
        "continue_task",
        "reconcile_task",
        "cancel_task",
        "accept_draft",
        "reject_draft",
    }
    assert tuple(inspect.signature(TaskServicePort.create_task).parameters) == (
        "self",
        "create_key",
        "project_id",
        "reasoning_owner",
        "review_policy",
    )
    assert tuple(inspect.signature(TaskServicePort.revert_project).parameters) == (
        "self",
        "revert_key",
        "project_id",
        "source_revision",
        "expected_head",
    )
    for name in ("accept_draft", "reject_draft"):
        assert tuple(inspect.signature(getattr(TaskServicePort, name)).parameters) == (
            "self",
            "task_id",
            "draft_id",
            "expected_generation",
        )
    assert tuple(inspect.signature(TaskServicePort.cancel_task).parameters) == (
        "self",
        "task_id",
        "expected_generation",
    )


def test_port_failure_requires_an_exact_closed_code():
    with pytest.raises(TypeError):
        TaskServicePortFailure(code="invalid_input")  # type: ignore[arg-type]


def _task_summary() -> dict[str, object]:
    task = _keyed_needs_plan()
    return {
        "task_id": task.id,
        "project_id": task.project_id,
        "generation": 0,
        "base_revision": task.base_revision,
        "reasoning_owner": task.reasoning_owner.value,
        "review_policy": task.review_policy.value,
        "status": task.status.value,
        "next_action": task.next_action.value,
        "candidate_revision": None,
        "committed_revision": None,
        "draft_id": None,
    }


def test_list_tasks_defaults_limit_accepts_explicit_null_cursor_and_copies_page():
    page = {"tasks": [_task_summary()], "next_cursor": None}
    port = _FakePort(page)

    response = TaskApi(port=port).list_tasks({"schema_version": 1, "cursor": None})

    result = _assert_success(response)
    assert result == page
    assert port.calls == [("list_tasks", {"limit": 50, "cursor": None})]
    assert result is not page
    assert result["tasks"][0] is not page["tasks"][0]


@pytest.mark.parametrize("limit", [1, 100])
def test_task_pages_accept_exact_limit_boundaries(limit: int):
    port = _FakePort({"tasks": [], "next_cursor": None})

    response = TaskApi(port=port).list_tasks({"schema_version": 1, "limit": limit})

    assert _assert_success(response)["tasks"] == []
    assert port.calls == [("list_tasks", {"limit": limit, "cursor": None})]


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (True, "invalid_type"),
        (1.0, "invalid_type"),
        (None, "invalid_type"),
        (0, "invalid_value"),
        (101, "invalid_value"),
    ],
)
def test_task_page_limit_is_exact_and_bounded(value: object, code: str):
    port = _FakePort({"tasks": [], "next_cursor": None})

    response = TaskApi(port=port).list_tasks({"schema_version": 1, "limit": value})

    assert response["ok"] is False
    assert response["error"]["code"] == code
    assert response["error"]["path"] == "/limit"
    assert port.calls == []


def test_get_task_events_accepts_endpoint_cursor_and_exact_persisted_mapping():
    task = _keyed_needs_plan()
    transition = task.transitions[0].to_mapping()
    cursor = "task_event_cursor_" + "1" * 64
    port = _FakePort(
        {
            "task_id": task.id,
            "generation": 0,
            "transitions": [transition],
            "next_cursor": cursor,
        }
    )

    response = TaskApi(port=port).get_task_events(
        {"schema_version": 1, "task_id": task.id, "limit": 1}
    )

    result = _assert_success(response)
    assert result["transitions"] == [transition]
    assert result["transitions"][0] is not transition
    assert result["next_cursor"] == cursor


@pytest.mark.parametrize(
    "cursor",
    [
        "task_list_cursor_" + "1" * 64,
        "task_event_cursor_" + "A" * 64,
        "task_event_cursor_" + "1" * 63,
        1,
    ],
)
def test_get_task_events_rejects_malformed_endpoint_cursor(cursor: object):
    port = _FakePort({})

    response = TaskApi(port=port).get_task_events(
        {"schema_version": 1, "task_id": KEYED_TASK_ID, "cursor": cursor}
    )

    assert response["ok"] is False
    assert response["error"]["code"] in {"invalid_type", "invalid_value"}
    assert response["error"]["path"] == "/cursor"
    assert port.calls == []


def test_task_page_rejects_short_untrusted_page_with_continuation():
    port = _FakePort(
        {
            "tasks": [_task_summary()],
            "next_cursor": "task_list_cursor_" + "1" * 64,
        }
    )

    response = TaskApi(port=port).list_tasks({"schema_version": 1, "limit": 2})

    assert response["ok"] is False
    assert response["error"]["code"] == "internal_error"


@pytest.mark.parametrize(
    "cursor",
    [
        "task_event_cursor_" + "1" * 64,
        "task_list_cursor_" + "A" * 64,
        "task_list_cursor_" + "1" * 63,
        1,
    ],
)
def test_list_tasks_rejects_malformed_endpoint_cursor(cursor: object):
    port = _FakePort({})

    response = TaskApi(port=port).list_tasks({"schema_version": 1, "cursor": cursor})

    assert response["ok"] is False
    assert response["error"]["code"] in {"invalid_type", "invalid_value"}
    assert response["error"]["path"] == "/cursor"
    assert port.calls == []


def test_events_explicit_null_cursor_uses_default_limit():
    task = _keyed_needs_plan()
    port = _FakePort(
        {
            "task_id": task.id,
            "generation": 0,
            "transitions": [task.transitions[0].to_mapping()],
            "next_cursor": None,
        }
    )

    response = TaskApi(port=port).get_task_events(
        {"schema_version": 1, "task_id": task.id, "cursor": None}
    )

    assert _assert_success(response)["generation"] == 0
    assert port.calls == [
        (
            "get_task_events",
            {"task_id": task.id, "limit": 50, "cursor": None},
        )
    ]


@pytest.mark.parametrize("sequences", [(2,), (1, 3)])
def test_events_reject_untrusted_non_contiguous_sequences(
    sequences: tuple[int, ...],
):
    task = _keyed_needs_plan()
    transition = task.transitions[0].to_mapping()
    transitions = [{**transition, "sequence": sequence} for sequence in sequences]
    port = _FakePort(
        {
            "task_id": task.id,
            "generation": 0,
            "transitions": transitions,
            "next_cursor": None,
        }
    )

    response = TaskApi(port=port).get_task_events({"schema_version": 1, "task_id": task.id})

    assert response["ok"] is False
    assert response["error"]["code"] == "internal_error"


def test_create_derives_the_task_id_external_plan_and_exact_result_envelope():
    port = _FakePort(StoredTaskRun(generation=0, task_run=_keyed_needs_plan()))

    response = TaskApi(port=port).create_task(_create_request())

    result = _assert_success(response)
    auto_commit = ReviewPolicy.AUTO_COMMIT
    assert port.calls == [
        (
            "create_task",
            {
                "create_key": CREATE_KEY,
                "project_id": PROJECT_ID,
                "reasoning_owner": ReasoningOwner.EXTERNAL_PLAN,
                "review_policy": auto_commit,
            },
        )
    ]
    assert set(result) == {"generation", "next_action", "task_run"}
    assert result == {
        "generation": 0,
        "next_action": "submit_program",
        "task_run": _keyed_needs_plan().to_mapping(),
    }


def test_create_requires_an_explicit_review_policy_before_calling_the_port():
    port = _FakePort(StoredTaskRun(generation=0, task_run=_keyed_needs_plan()))

    response = TaskApi(port=port).create_task(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "project_id": PROJECT_ID,
        }
    )

    _assert_error(response, "missing_field", "/review_policy")
    assert port.calls == []


@pytest.mark.parametrize(
    ("value", "code"),
    [
        (True, "invalid_type"),
        (1, "invalid_type"),
        (None, "invalid_type"),
        ("AUTO_COMMIT", "invalid_value"),
        ("manual", "invalid_value"),
    ],
)
def test_create_review_policy_ingress_is_exact(value, code):
    port = _FakePort(StoredTaskRun(generation=0, task_run=_keyed_needs_plan()))

    response = TaskApi(port=port).create_task(_create_request(review_policy=value))

    _assert_error(response, code, "/review_policy")
    assert port.calls == []


def test_create_require_review_passes_the_exact_enum_and_requires_matching_result():
    require_review = ReviewPolicy.REQUIRE_REVIEW
    needs_plan = _keyed_needs_plan(review_policy=require_review)
    port = _FakePort(StoredTaskRun(generation=0, task_run=needs_plan))

    response = TaskApi(port=port).create_task(_create_request(review_policy="require_review"))

    result = _assert_success(response)
    assert result["task_run"]["review_policy"] == "require_review"
    assert port.calls == [
        (
            "create_task",
            {
                "create_key": CREATE_KEY,
                "project_id": PROJECT_ID,
                "reasoning_owner": ReasoningOwner.EXTERNAL_PLAN,
                "review_policy": require_review,
            },
        )
    ]


def test_create_mismatched_review_policy_port_result_is_internal():
    auto_commit = ReviewPolicy.AUTO_COMMIT
    needs_plan = _keyed_needs_plan(review_policy=auto_commit)
    port = _FakePort(StoredTaskRun(generation=0, task_run=needs_plan))

    response = TaskApi(port=port).create_task(_create_request(review_policy="require_review"))

    _assert_error(response, "internal_error", "")
    assert [name for name, _ in port.calls] == ["create_task"]


def test_create_collision_calls_the_port_exactly_once():
    failure = TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
    port = _FakePort(failure)

    response = TaskApi(port=port).create_task(_create_request())

    _assert_error(response, "conflict", "")
    assert [name for name, _ in port.calls] == ["create_task"]


@pytest.mark.parametrize(
    ("create_key", "code"),
    [
        ("TASK_CREATE_" + "A" * 32, "invalid_value"),
        ("task_create_short", "invalid_value"),
        (7, "invalid_type"),
        (None, "invalid_type"),
    ],
)
def test_invalid_create_key_never_reaches_the_port(create_key, code):
    port = _FakePort(StoredTaskRun(generation=0, task_run=_keyed_needs_plan()))

    response = TaskApi(port=port).create_task(_create_request(create_key=create_key))

    _assert_error(response, code, "/create_key")
    assert port.calls == []


def test_invalid_create_key_precedes_other_invalid_create_intent_fields():
    response = TaskApi(port=object()).create_task(
        _create_request(
            create_key="bad",
            project_id="bad",
            review_policy="bad",
        )
    )

    _assert_error(response, "invalid_value", "/create_key")


def test_create_port_raise_is_redacted_and_not_retried():
    port = _FakePort(RuntimeError("/private/path secret"))

    response = TaskApi(port=port).create_task(_create_request())

    _assert_error(response, "internal_error", "")
    assert "private" not in json.dumps(response)
    assert [name for name, _ in port.calls] == ["create_task"]


def test_revert_project_forwards_exact_immutable_intent_and_returns_ordinary_task_result():
    task = _bound_revert_task()
    port = _FakePort(StoredTaskRun(generation=7, task_run=task))

    response = TaskApi(port=port).revert_project(_revert_request())

    assert port.calls == [
        (
            "revert_project",
            {
                "revert_key": REVERT_KEY,
                "project_id": PROJECT_ID,
                "source_revision": CANDIDATE_REVISION,
                "expected_head": BASE_REVISION,
            },
        )
    ]
    assert _assert_success(response) == {
        "generation": 7,
        "next_action": task.next_action.value,
        "task_run": task.to_mapping(),
    }


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("revert_key", "revert_create_short", "invalid_value"),
        ("revert_key", 7, "invalid_type"),
        ("project_id", "project_bad", "invalid_value"),
        ("source_revision", "revision_bad", "invalid_value"),
        ("expected_head", {"generation": 0, "revision_id": BASE_REVISION}, "invalid_type"),
    ],
)
def test_revert_project_ingress_is_exact_and_never_calls_the_port(field, value, code):
    port = _FakePort(
        StoredTaskRun(
            generation=0,
            task_run=_keyed_needs_plan(review_policy=ReviewPolicy.REQUIRE_REVIEW),
        )
    )
    request = _revert_request()
    request[field] = value

    response = TaskApi(port=port).revert_project(request)

    _assert_error(response, code, f"/{field}")
    assert port.calls == []


def test_revert_project_rejects_missing_or_unknown_fields_before_the_port():
    port = _FakePort(
        StoredTaskRun(
            generation=0,
            task_run=_keyed_needs_plan(review_policy=ReviewPolicy.REQUIRE_REVIEW),
        )
    )
    missing = _revert_request()
    del missing["expected_head"]
    unknown = {**_revert_request(), "expected_generation": 0}

    _assert_error(
        TaskApi(port=port).revert_project(missing),
        "missing_field",
        "/expected_head",
    )
    _assert_error(
        TaskApi(port=port).revert_project(unknown),
        "unknown_field",
        "/expected_generation",
    )
    assert port.calls == []


@pytest.mark.parametrize(
    "task",
    [
        _keyed_needs_plan(review_policy=ReviewPolicy.REQUIRE_REVIEW),
        _forged_revert_needs_plan(),
        _keyed_needs_plan(
            project_id="project_11111111111111111111111111111111",
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
        ),
        _keyed_needs_plan(review_policy=ReviewPolicy.AUTO_COMMIT),
    ],
)
def test_revert_project_rejects_unbound_port_results(task: TaskRun):
    port = _FakePort(StoredTaskRun(generation=0, task_run=task))

    response = TaskApi(port=port).revert_project(_revert_request())

    _assert_error(response, "internal_error", "")
    assert [name for name, _ in port.calls] == ["revert_project"]


def test_revert_project_maps_port_failure_and_redacts_untrusted_exception():
    conflict = _FakePort(TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT))
    raised = _FakePort(RuntimeError("/private/revert secret"))

    _assert_error(
        TaskApi(port=conflict).revert_project(_revert_request()),
        "conflict",
        "",
    )
    response = TaskApi(port=raised).revert_project(_revert_request())
    _assert_error(response, "internal_error", "")
    assert "private" not in json.dumps(response)
    assert [name for name, _ in conflict.calls] == ["revert_project"]
    assert [name for name, _ in raised.calls] == ["revert_project"]


def test_internal_failure_type_raised_by_a_port_is_never_trusted():
    injected = task_api_module._ApiFailure(  # type: ignore[attr-defined]
        TaskApiErrorCode.INVALID_INPUT,
        "/secret/private/path",
    )
    port = _FakePort(injected)

    port_response = TaskApi(port=port).get_task({"schema_version": 1, "task_id": TASK_ID})
    create_response = TaskApi(port=_FakePort(injected)).create_task(_create_request())

    _assert_error(port_response, "internal_error", "")
    _assert_error(create_response, "internal_error", "")
    assert "/secret" not in json.dumps(port_response)
    assert "/secret" not in json.dumps(create_response)


def test_review_port_attribute_lookup_cannot_inject_an_api_failure():
    injected = task_api_module._ApiFailure(  # type: ignore[attr-defined]
        TaskApiErrorCode.INVALID_INPUT,
        "/secret/private/path",
    )

    class HostileReviewPort:
        def __getattribute__(self, name):
            if name in {"accept_draft", "reject_draft"}:
                raise injected
            return super().__getattribute__(name)

    api = TaskApi(port=HostileReviewPort())
    for method in ("accept_draft", "reject_draft"):
        response = getattr(api, method)(_decision_request())
        _assert_error(response, "internal_error", "")
        assert "/secret" not in json.dumps(response)


@pytest.mark.parametrize("status", list(TaskStatus))
def test_get_task_projects_every_durable_next_action(status: TaskStatus):
    stored = _stored(status, generation=7)
    port = _FakePort(stored)

    response = TaskApi(port=port).get_task({"schema_version": 1, "task_id": TASK_ID})

    result = _assert_success(response)
    assert result == {
        "generation": 7,
        "next_action": stored.task_run.next_action.value,
        "task_run": stored.task_run.to_mapping(),
    }
    assert port.calls == [("get_task", {"task_id": TASK_ID})]


def test_task_projection_is_defensive_across_calls():
    stored = _stored(TaskStatus.NEEDS_PLAN, generation=4)
    port = _FakePort(stored)
    api = TaskApi(port=port)
    request = {"schema_version": 1, "task_id": TASK_ID}

    first = _assert_success(api.get_task(request))
    first["task_run"]["project_id"] = "corrupted"
    second = _assert_success(api.get_task(request))

    assert second["task_run"] == stored.task_run.to_mapping()
    assert stored.task_run.project_id == PROJECT_ID


def test_valid_program_json_is_decoded_once_and_submitted_as_a_model_program():
    port = _FakePort(_stored(TaskStatus.NEEDS_INPUT, generation=8))
    request = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "expected_generation": 7,
        "program_json": _program_json(),
    }

    response = TaskApi(port=port).submit_model_program(request)

    _assert_success(response)
    assert len(port.calls) == 1
    name, kwargs = port.calls[0]
    assert name == "submit_model_program"
    assert kwargs == {
        "task_id": TASK_ID,
        "expected_generation": 7,
        "program": _program(),
    }
    assert type(kwargs["program"]) is ModelProgram


def test_program_task_id_mismatch_is_rejected_before_the_port():
    port = _FakePort(_stored())
    request = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "expected_generation": 0,
        "program_json": _program_json(task_id=OTHER_TASK_ID),
    }

    response = TaskApi(port=port).submit_model_program(request)

    _assert_error(response, "invalid_input", "/program_json/task_id")
    assert port.calls == []


def _decision_request(
    *,
    draft_id: str = DRAFT_ID,
    expected_generation: int = 7,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "draft_id": draft_id,
        "expected_generation": expected_generation,
    }


def _cancel_request(*, expected_generation: object = 7) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "expected_generation": expected_generation,
    }


def test_cancel_task_accepts_exact_request_and_returns_persisted_terminal_state():
    cancelled = transition_task(
        _task_at(TaskStatus.NEEDS_PLAN),
        TaskEvent.REQUEST_CANCEL,
    )
    port = _FakePort(StoredTaskRun(generation=8, task_run=cancelled))

    response = TaskApi(port=port).cancel_task(_cancel_request())

    result = _assert_success(response)
    assert result == {
        "generation": 8,
        "next_action": "none",
        "task_run": cancelled.to_mapping(),
    }
    assert port.calls == [
        (
            "cancel_task",
            {
                "task_id": TASK_ID,
                "expected_generation": 7,
            },
        )
    ]


@pytest.mark.parametrize(
    "status",
    [
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLING,
        TaskStatus.CANCELLED,
    ],
)
def test_cancel_task_accepts_every_durable_cancellation_state(status: TaskStatus):
    stored = _stored(status, generation=11)
    port = _FakePort(stored)

    result = _assert_success(TaskApi(port=port).cancel_task(_cancel_request(expected_generation=7)))

    assert result["generation"] == 11
    assert result["task_run"]["status"] == status.value
    assert port.calls == [
        (
            "cancel_task",
            {"task_id": TASK_ID, "expected_generation": 7},
        )
    ]


@pytest.mark.parametrize(
    ("event", "status"),
    [
        (TaskEvent.REQUIRE_RECOVERY, TaskStatus.RECOVERY_REQUIRED),
        (TaskEvent.REQUIRE_CLEANUP, TaskStatus.CLEANUP_REQUIRED),
        (TaskEvent.CONFIRM_COMMITTED, TaskStatus.SUCCEEDED),
    ],
)
def test_cancel_task_accepts_only_proven_cancellation_descendants(
    event: TaskEvent,
    status: TaskStatus,
) -> None:
    origin = (
        _task_at(TaskStatus.COMMITTING)
        if event is TaskEvent.CONFIRM_COMMITTED
        else _task_at(TaskStatus.EXECUTING)
    )
    requested = transition_task(origin, TaskEvent.REQUEST_CANCEL)
    cancelling = transition_task(requested, TaskEvent.START_CANCELLATION)
    if event is TaskEvent.CONFIRM_COMMITTED:
        task = transition_task(
            cancelling,
            event,
            committed_revision=CANDIDATE_REVISION,
        )
    else:
        task = transition_task(cancelling, event, error=_step_error())
    stored = StoredTaskRun(generation=11, task_run=task)
    port = _FakePort(stored)

    result = _assert_success(
        TaskApi(port=port).cancel_task(
            _cancel_request(expected_generation=7),
        )
    )

    assert result["generation"] == 11
    assert result["task_run"]["status"] == status.value

    ordinary = _FakePort(_stored(status, generation=11))
    response = TaskApi(port=ordinary).cancel_task(
        _cancel_request(expected_generation=7),
    )
    _assert_error(response, "internal_error", "")


def test_cancel_task_rejects_future_generation_or_non_cancellation_port_results():
    malformed = (
        StoredTaskRun(generation=6, task_run=_task_at(TaskStatus.CANCELLED)),
        StoredTaskRun(generation=8, task_run=_task_at(TaskStatus.NEEDS_PLAN)),
    )
    for stored in malformed:
        port = _FakePort(stored)

        response = TaskApi(port=port).cancel_task(_cancel_request(expected_generation=7))

        _assert_error(response, "internal_error", "")
        assert [name for name, _ in port.calls] == ["cancel_task"]


@pytest.mark.parametrize(
    ("payload", "code", "path"),
    [
        ({"schema_version": 1}, "missing_field", "/expected_generation"),
        ({**_cancel_request(), "unknown": 1}, "unknown_field", "/unknown"),
        ({**_cancel_request(), "task_id": "task_bad"}, "invalid_value", "/task_id"),
        (
            {**_cancel_request(), "expected_generation": True},
            "invalid_type",
            "/expected_generation",
        ),
        (
            {**_cancel_request(), "expected_generation": -1},
            "invalid_value",
            "/expected_generation",
        ),
    ],
)
def test_cancel_task_ingress_is_exact_and_never_calls_the_port(payload, code, path):
    port = _FakePort(_stored(TaskStatus.CANCELLED, generation=8))

    response = TaskApi(port=port).cancel_task(payload)

    _assert_error(response, code, path)
    assert port.calls == []


def test_cancel_task_maps_port_failure_and_redacts_untrusted_exception():
    conflict = _FakePort(TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT))
    _assert_error(
        TaskApi(port=conflict).cancel_task(_cancel_request()),
        "conflict",
        "",
    )
    assert [name for name, _ in conflict.calls] == ["cancel_task"]

    raised = _FakePort(RuntimeError("/private/cancel-secret"))
    response = TaskApi(port=raised).cancel_task(_cancel_request())
    _assert_error(response, "internal_error", "")
    assert "private" not in json.dumps(response)
    assert [name for name, _ in raised.calls] == ["cancel_task"]


@pytest.mark.parametrize(
    ("api_method", "port_method", "terminal_status"),
    [
        ("accept_draft", "accept_draft", TaskStatus.SUCCEEDED),
        ("reject_draft", "reject_draft", "rejected"),
    ],
)
def test_review_decision_requests_are_exact_and_return_only_terminal_results(
    api_method,
    port_method,
    terminal_status,
):
    status = TaskStatus.REJECTED if terminal_status == "rejected" else terminal_status
    stored = StoredTaskRun(generation=8, task_run=_review_task_at(status))
    port = _FakePort(stored)

    response = getattr(TaskApi(port=port), api_method)(_decision_request())

    result = _assert_success(response)
    assert result["generation"] == 8
    assert result["task_run"]["draft"]["id"] == DRAFT_ID
    assert port.calls == [
        (
            port_method,
            {
                "task_id": TASK_ID,
                "draft_id": DRAFT_ID,
                "expected_generation": 7,
            },
        )
    ]


def test_review_decision_ingress_requires_exact_fields_and_draft_identity():
    api = TaskApi(port=object())
    cases = [
        ("accept_draft", {"schema_version": 1}, "missing_field", "/draft_id"),
        (
            "reject_draft",
            {**_decision_request(), "unknown": 1},
            "unknown_field",
            "/unknown",
        ),
        (
            "accept_draft",
            _decision_request(draft_id="revision_" + "1" * 32),
            "invalid_value",
            "/draft_id",
        ),
        (
            "reject_draft",
            {**_decision_request(), "expected_generation": True},
            "invalid_type",
            "/expected_generation",
        ),
    ]
    for method, request, code, path in cases:
        response = getattr(api, method)(request)
        _assert_error(response, code, path)


def test_review_decision_semantic_replays_and_conflicts_are_port_owned_and_single_call():
    succeeded = StoredTaskRun(
        generation=19,
        task_run=_review_task_at(TaskStatus.SUCCEEDED),
    )
    rejected = StoredTaskRun(
        generation=13,
        task_run=_review_task_at(TaskStatus.REJECTED),
    )
    for method, terminal in (("accept_draft", succeeded), ("reject_draft", rejected)):
        port = _FakePort(terminal)
        response = getattr(TaskApi(port=port), method)(_decision_request(expected_generation=0))
        _assert_success(response)
        assert port.calls == [
            (
                method,
                {
                    "task_id": TASK_ID,
                    "draft_id": DRAFT_ID,
                    "expected_generation": 0,
                },
            )
        ]

    conflict = TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
    conflict_requests = [
        ("accept_draft", _decision_request(draft_id=OTHER_DRAFT_ID)),
        ("reject_draft", _decision_request(expected_generation=999)),
        ("reject_draft", _decision_request()),
    ]
    for method, request in conflict_requests:
        port = _FakePort(conflict)
        response = getattr(TaskApi(port=port), method)(request)
        _assert_error(response, "conflict", "")
        assert port.calls == [
            (
                method,
                {
                    "task_id": TASK_ID,
                    "draft_id": request["draft_id"],
                    "expected_generation": request["expected_generation"],
                },
            )
        ]


def test_review_decision_malformed_semantic_port_results_are_internal():
    malformed = [
        (
            "accept_draft",
            StoredTaskRun(generation=8, task_run=_task_at(TaskStatus.SUCCEEDED)),
        ),
        (
            "accept_draft",
            StoredTaskRun(
                generation=8,
                task_run=_review_task_at(TaskStatus.AWAITING_USER_REVIEW),
            ),
        ),
        (
            "reject_draft",
            StoredTaskRun(
                generation=8,
                task_run=_review_task_at(TaskStatus.SUCCEEDED),
            ),
        ),
        (
            "reject_draft",
            StoredTaskRun(
                generation=8,
                task_run=_review_task_at(TaskStatus.AWAITING_USER_REVIEW),
            ),
        ),
        (
            "accept_draft",
            StoredTaskRun(
                generation=8,
                task_run=_review_task_at(
                    TaskStatus.SUCCEEDED,
                    candidate_revision=OTHER_CANDIDATE_REVISION,
                ),
            ),
        ),
    ]
    for method, stored in malformed:
        response = getattr(TaskApi(port=_FakePort(stored)), method)(_decision_request())
        _assert_error(response, "internal_error", "")


@pytest.mark.parametrize(
    ("status", "expected_calls", "success"),
    [
        (TaskStatus.CREATED, ["get_task"], False),
        (TaskStatus.NEEDS_PLAN, ["get_task"], False),
        (TaskStatus.PROGRAM_READY, ["get_task", "continue_task"], True),
        (TaskStatus.VALIDATING_PROGRAM, ["get_task", "reconcile_task"], True),
        (TaskStatus.EXECUTING, ["get_task", "reconcile_task"], True),
        (TaskStatus.VERIFYING, ["get_task", "reconcile_task"], True),
        (TaskStatus.COMMITTING, ["get_task", "reconcile_task"], True),
        (TaskStatus.ROLLING_BACK, ["get_task", "reconcile_task"], True),
        (TaskStatus.NEEDS_INPUT, ["get_task"], False),
        (TaskStatus.RECOVERY_REQUIRED, ["get_task", "reconcile_task"], True),
        (TaskStatus.CLEANUP_REQUIRED, ["get_task", "reconcile_task"], True),
        (TaskStatus.SUCCEEDED, ["get_task"], True),
        (TaskStatus.FAILED, ["get_task"], True),
        (TaskStatus.CANCEL_REQUESTED, ["get_task", "reconcile_task"], True),
        (TaskStatus.CANCELLING, ["get_task", "reconcile_task"], True),
        (TaskStatus.CANCELLED, ["get_task"], True),
    ],
)
def test_resume_dispatches_every_status_once(status, expected_calls, success):
    stored = _stored(status, generation=7)
    port = _FakePort(stored)
    request = {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 7}

    response = TaskApi(port=port).resume_task(request)

    if success:
        _assert_success(response)
    else:
        _assert_error(response, "invalid_state", "")
    assert [name for name, _ in port.calls] == expected_calls
    assert all(kwargs["task_id"] == TASK_ID for _, kwargs in port.calls)
    for name, kwargs in port.calls:
        if name != "get_task":
            assert kwargs["expected_generation"] == 7


def test_resume_routes_all_four_review_statuses_without_choosing_for_the_user():
    cases = [
        ("preparing_review", ["get_task", "reconcile_task"], True),
        ("accepting_draft", ["get_task", "reconcile_task"], True),
        ("awaiting_user_review", ["get_task"], False),
        ("rejected", ["get_task"], True),
    ]
    for status_value, expected_calls, success in cases:
        status = TaskStatus(status_value)
        port = _FakePort(StoredTaskRun(generation=7, task_run=_review_task_at(status)))

        response = TaskApi(port=port).resume_task(
            {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 7}
        )

        if success:
            _assert_success(response)
        else:
            _assert_error(response, "invalid_state", "")
        assert [name for name, _ in port.calls] == expected_calls


@pytest.mark.parametrize("status", list(TaskStatus))
def test_resume_generation_conflict_precedes_status_dispatch(status: TaskStatus):
    port = _FakePort(_stored(status, generation=8))

    response = TaskApi(port=port).resume_task(
        {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 7}
    )

    _assert_error(response, "conflict", "")
    assert [name for name, _ in port.calls] == ["get_task"]


@pytest.mark.parametrize("code", list(TaskServicePortErrorCode))
def test_all_neutral_port_failures_map_to_fixed_path_free_public_errors(code):
    port = _FakePort(TaskServicePortFailure(code=code))

    response = TaskApi(port=port).get_task({"schema_version": 1, "task_id": TASK_ID})

    _assert_error(response, code.value, "")


def test_resource_exhausted_is_a_closed_path_free_port_and_api_failure():
    code = TaskServicePortErrorCode("resource_exhausted")
    assert TaskApiErrorCode("resource_exhausted").value == code.value
    response = TaskApi(port=_FakePort(TaskServicePortFailure(code=code))).get_task(
        {"schema_version": 1, "task_id": TASK_ID}
    )
    _assert_error(response, "resource_exhausted", "")


@pytest.mark.parametrize("raised", [RuntimeError("secret /tmp/model"), KeyboardInterrupt()])
def test_port_raises_are_redacted_as_internal_error_without_retry(raised):
    port = _FakePort(raised)

    response = TaskApi(port=port).get_task({"schema_version": 1, "task_id": TASK_ID})

    _assert_error(response, "internal_error", "")
    assert len(port.calls) == 1
    assert "secret" not in json.dumps(response)


def test_resume_redacts_continue_and_reconcile_raises_without_retry():
    for status, method in (
        (TaskStatus.PROGRAM_READY, "continue_task"),
        (TaskStatus.EXECUTING, "reconcile_task"),
    ):
        port = _FakePort(_stored(status, generation=3))
        port.responses[method] = RuntimeError("secret")

        response = TaskApi(port=port).resume_task(
            {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 3}
        )

        _assert_error(response, "internal_error", "")
        assert [name for name, _ in port.calls] == ["get_task", method]


def test_non_exact_or_semantically_mismatched_port_results_are_internal():
    class StoredSubclass(StoredTaskRun):
        pass

    class FailureSubclass(TaskServicePortFailure):
        pass

    wrong_task = StoredTaskRun(generation=0, task_run=_created(task_id=OTHER_TASK_ID))
    values = [
        object(),
        StoredSubclass(generation=0, task_run=_task_at(TaskStatus.NEEDS_PLAN)),
        FailureSubclass(code=TaskServicePortErrorCode.CONFLICT),
        wrong_task,
    ]
    for value in values:
        response = TaskApi(port=_FakePort(value)).get_task(
            {"schema_version": 1, "task_id": TASK_ID}
        )
        _assert_error(response, "internal_error", "")


def test_forged_exact_port_failure_with_a_public_code_is_internal():
    forged = object.__new__(TaskServicePortFailure)
    object.__setattr__(forged, "code", TaskApiErrorCode.MISSING_FIELD)

    response = TaskApi(port=_FakePort(forged)).get_task({"schema_version": 1, "task_id": TASK_ID})

    _assert_error(response, "internal_error", "")


def test_create_rejects_a_created_wrong_project_or_wrong_digest_port_result_as_internal():
    collision = transition_task(
        new_task_run(
            task_id=KEYED_TASK_ID,
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.AUTO_COMMIT,
            creation_digest=COLLISION_DIGEST,
        ),
        TaskEvent.REQUEST_PLAN,
    )
    values = [
        StoredTaskRun(generation=0, task_run=_keyed_created()),
        StoredTaskRun(
            generation=0,
            task_run=_keyed_needs_plan(project_id="project_11111111111111111111111111111111"),
        ),
        StoredTaskRun(generation=1, task_run=collision),
    ]
    for value in values:
        response = TaskApi(port=_FakePort(value)).create_task(_create_request())
        _assert_error(response, "internal_error", "")


@pytest.mark.parametrize("value", [[], MappingProxyType({"schema_version": 1}), _HostileMapping()])
def test_non_exact_request_mappings_are_rejected_without_iteration(value):
    response = TaskApi(port=object()).get_capabilities(value)
    _assert_error(response, "invalid_type", "")


def test_dict_subclass_is_rejected_before_overridden_iteration():
    class HostileDict(dict):
        def __iter__(self):
            raise AssertionError("must not iterate")

    response = TaskApi(port=object()).get_capabilities(HostileDict(schema_version=1))
    _assert_error(response, "invalid_type", "")


@pytest.mark.parametrize(
    ("method", "payload", "path"),
    [
        ("create_task", {"schema_version": 1}, "/create_key"),
        ("get_task", {"schema_version": 1}, "/task_id"),
        (
            "resume_task",
            {"schema_version": 1, "task_id": TASK_ID},
            "/expected_generation",
        ),
        (
            "submit_model_program",
            {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 0},
            "/program_json",
        ),
        ("get_capabilities", {}, "/schema_version"),
    ],
)
def test_missing_fields_have_deterministic_paths(method, payload, path):
    response = getattr(TaskApi(port=object()), method)(payload)
    _assert_error(response, "missing_field", path)


def test_unknown_fields_are_sorted_and_json_pointer_escaped():
    response = TaskApi(port=object()).get_capabilities({"schema_version": 1, "z": 0, "a/b~c": 0})
    _assert_error(response, "unknown_field", "/a~1b~0c")


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_schema_version_requires_an_exact_integer(version):
    response = TaskApi(port=object()).get_capabilities({"schema_version": version})
    _assert_error(response, "invalid_type", "/schema_version")


@pytest.mark.parametrize("version", [0, 2, -1])
def test_unsupported_schema_versions_are_stable(version):
    response = TaskApi(port=object()).get_capabilities({"schema_version": version})
    _assert_error(response, "unsupported_version", "/schema_version")


def test_unsafe_schema_version_is_invalid_value_before_version_dispatch():
    response = TaskApi(port=object()).get_capabilities(
        {"schema_version": MAX_SAFE_JSON_INTEGER + 1}
    )
    _assert_error(response, "invalid_value", "/schema_version")


@pytest.mark.parametrize(
    ("method", "payload", "code", "path"),
    [
        (
            "create_task",
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "project_id": 1,
                "review_policy": "auto_commit",
            },
            "invalid_type",
            "/project_id",
        ),
        (
            "create_task",
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "project_id": "PROJECT_" + "A" * 32,
                "review_policy": "auto_commit",
            },
            "invalid_value",
            "/project_id",
        ),
        ("get_task", {"schema_version": 1, "task_id": "bad"}, "invalid_value", "/task_id"),
        (
            "resume_task",
            {"schema_version": 1, "task_id": TASK_ID, "expected_generation": True},
            "invalid_type",
            "/expected_generation",
        ),
        (
            "resume_task",
            {"schema_version": 1, "task_id": TASK_ID, "expected_generation": -1},
            "invalid_value",
            "/expected_generation",
        ),
        (
            "resume_task",
            {
                "schema_version": 1,
                "task_id": TASK_ID,
                "expected_generation": MAX_SAFE_JSON_INTEGER + 1,
            },
            "invalid_value",
            "/expected_generation",
        ),
    ],
)
def test_identifier_and_generation_fields_are_exact(method, payload, code, path):
    response = getattr(TaskApi(port=object()), method)(payload)
    _assert_error(response, code, path)


@pytest.mark.parametrize("value", [(1,), b"bytes", {1}, object(), float("nan"), float("inf")])
def test_hostile_outer_values_fail_closed_without_calling_the_port(value):
    port = _FakePort(_stored())
    response = TaskApi(port=port).create_task(_create_request(project_id=value))
    assert response["ok"] is False
    assert response["error"]["code"] in {"invalid_type", "invalid_value"}
    assert port.calls == []


def test_outer_cycle_alias_and_invalid_unicode_fail_closed():
    cycle: list[object] = []
    cycle.append(cycle)
    shared: list[object] = []
    requests = [
        {"schema_version": 1, "cycle": cycle},
        {"schema_version": 1, "left": shared, "right": shared},
        {"schema_version": 1, "bad": "\ud800"},
    ]
    for request in requests:
        response = TaskApi(port=object()).get_capabilities(request)
        assert response["ok"] is False
        assert response["error"]["code"] in set(ERROR_MESSAGES)


@pytest.mark.parametrize(
    ("method", "base", "padding_field"),
    [
        (
            "create_task",
            {
                "schema_version": 1,
                "create_key": CREATE_KEY,
                "project_id": "",
                "review_policy": "auto_commit",
            },
            "project_id",
        ),
        ("get_task", {"schema_version": 1, "task_id": ""}, "task_id"),
        (
            "resume_task",
            {"schema_version": 1, "task_id": "", "expected_generation": 0},
            "task_id",
        ),
        (
            "accept_draft",
            {
                "schema_version": 1,
                "task_id": TASK_ID,
                "draft_id": "",
                "expected_generation": 0,
            },
            "draft_id",
        ),
        (
            "reject_draft",
            {
                "schema_version": 1,
                "task_id": TASK_ID,
                "draft_id": "",
                "expected_generation": 0,
            },
            "draft_id",
        ),
        ("get_capabilities", {"schema_version": ""}, "schema_version"),
    ],
)
def test_small_request_canonical_budget_is_exact(method, base, padding_field):
    at_limit = _pad_field(base, padding_field, 4_096)
    over_limit = _pad_field(base, padding_field, 4_097)
    api = TaskApi(port=object())

    at_response = getattr(api, method)(at_limit)
    over_response = getattr(api, method)(over_limit)

    assert at_response["error"]["code"] != "budget_exceeded"
    _assert_error(over_response, "budget_exceeded", "")


def test_submit_metadata_budget_excludes_program_json_and_is_exact():
    base = {
        "schema_version": 1,
        "task_id": "",
        "expected_generation": 0,
        "program_json": "{}",
    }
    metadata_base = {key: value for key, value in base.items() if key != "program_json"}
    metadata_at = _pad_field(metadata_base, "task_id", 4_096)
    metadata_over = _pad_field(metadata_base, "task_id", 4_097)
    at_limit = {**metadata_at, "program_json": "{}"}
    over_limit = {**metadata_over, "program_json": "{}"}
    assert _canonical_size({k: v for k, v in at_limit.items() if k != "program_json"}) == 4_096

    at_response = TaskApi(port=object()).submit_model_program(at_limit)
    over_response = TaskApi(port=object()).submit_model_program(over_limit)

    assert at_response["error"]["code"] != "budget_exceeded"
    _assert_error(over_response, "budget_exceeded", "")


def test_program_raw_utf8_and_logical_budgets_are_exact():
    compact = _program_json()
    raw_at = compact + " " * (524_288 - len(compact.encode("utf-8")))
    raw_over = raw_at + " "
    assert len(raw_at.encode("utf-8")) == 524_288
    port = _FakePort(_stored(TaskStatus.NEEDS_INPUT, generation=1))
    base = {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 0}

    at_response = TaskApi(port=port).submit_model_program({**base, "program_json": raw_at})
    over_response = TaskApi(port=port).submit_model_program({**base, "program_json": raw_over})

    _assert_success(at_response)
    _assert_error(over_response, "budget_exceeded", "/program_json")
    assert [name for name, _ in port.calls] == ["submit_model_program"]
    assert 528_384 == 4_096 + 524_288


def test_submit_logical_sum_boundary_uses_maximum_metadata_and_raw_bytes():
    mapping = _program().to_mapping()
    mapping["acceptance"]["id"] = "acceptance-é"
    compact = json.dumps(
        mapping,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    raw_at = compact + " " * (524_288 - len(compact.encode("utf-8")))
    raw_over = raw_at + " "
    assert len(raw_at) < 524_288
    assert len(raw_over) == 524_288
    assert len(raw_over.encode("utf-8")) == 524_289

    decoded = task_api_module._decode_model_program(  # type: ignore[attr-defined]
        raw_at,
        metadata_bytes=4_096,
        task_id=TASK_ID,
    )
    with pytest.raises(task_api_module._ApiFailure) as caught:  # type: ignore[attr-defined]
        task_api_module._decode_model_program(  # type: ignore[attr-defined]
            raw_over,
            metadata_bytes=4_096,
            task_id=TASK_ID,
        )

    assert decoded.task_id == TASK_ID
    assert decoded.acceptance.id == "acceptance-é"
    assert caught.value.code is TaskApiErrorCode.BUDGET_EXCEEDED
    assert caught.value.path == "/program_json"


def test_huge_outer_and_program_strings_fail_before_utf8_encoding(monkeypatch):
    encoded_lengths: list[int] = []
    original = task_api_module._utf8_length  # type: ignore[attr-defined]

    def bounded_encode(value, path):
        encoded_lengths.append(len(value))
        assert len(value) <= (524_288 if path == "/program_json" else 4_096)
        return original(value, path)

    monkeypatch.setattr(task_api_module, "_utf8_length", bounded_encode)

    outer = TaskApi(port=object()).create_task(
        {
            "schema_version": 1,
            "create_key": CREATE_KEY,
            "project_id": "x" * 4_097,
            "review_policy": "auto_commit",
        }
    )
    with pytest.raises(task_api_module._ApiFailure) as caught:  # type: ignore[attr-defined]
        task_api_module._decode_model_program(  # type: ignore[attr-defined]
            "x" * 524_289,
            metadata_bytes=0,
            task_id=TASK_ID,
        )

    _assert_error(outer, "budget_exceeded", "/project_id")
    assert caught.value.code is TaskApiErrorCode.BUDGET_EXCEEDED
    assert 4_097 not in encoded_lengths
    assert 524_289 not in encoded_lengths


def test_deep_or_long_unknown_outer_input_never_recurses_or_expands_error_path():
    nested: object = float("nan")
    for _ in range(1_200):
        nested = [nested]
    long_name = "x" * 1_000

    deep_response = TaskApi(port=object()).get_capabilities(
        {"schema_version": 1, "unknown": nested}
    )
    long_response = TaskApi(port=object()).get_capabilities({"schema_version": 1, long_name: 0})

    _assert_error(deep_response, "unknown_field", "/unknown")
    _assert_error(long_response, "unknown_field", "/_truncated")
    assert len(long_response["error"]["path"].encode("utf-8")) <= 256


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("[" * 64 + "0" + "]" * 64, "invalid_type"),
        ("[" * 65 + "0" + "]" * 65, "budget_exceeded"),
        (json.dumps("[{braces}]"), "invalid_type"),
    ],
)
def test_program_depth_budget_is_64_and_scanner_ignores_string_braces(raw, code):
    request = {
        "schema_version": 1,
        "task_id": TASK_ID,
        "expected_generation": 0,
        "program_json": raw,
    }
    response = TaskApi(port=object()).submit_model_program(request)
    _assert_error(response, code, "/program_json")


def test_program_node_budget_is_exactly_8192_including_the_root():
    raw_at = json.dumps([0] * 8_191, separators=(",", ":"))
    raw_over = json.dumps([0] * 8_192, separators=(",", ":"))
    base = {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 0}

    at_response = TaskApi(port=object()).submit_model_program({**base, "program_json": raw_at})
    over_response = TaskApi(port=object()).submit_model_program({**base, "program_json": raw_over})

    _assert_error(at_response, "invalid_type", "/program_json")
    _assert_error(over_response, "budget_exceeded", "/program_json")


def test_program_value_string_and_key_utf8_budgets_are_exact():
    base = {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 0}
    string_at = json.dumps("x" * 65_536)
    string_over = json.dumps("x" * 65_537)
    key_at = json.dumps({"x" * 256: 0})
    key_over = json.dumps({"x" * 257: 0})

    string_at_response = TaskApi(port=object()).submit_model_program(
        {**base, "program_json": string_at}
    )
    string_over_response = TaskApi(port=object()).submit_model_program(
        {**base, "program_json": string_over}
    )
    key_at_response = TaskApi(port=object()).submit_model_program({**base, "program_json": key_at})
    key_over_response = TaskApi(port=object()).submit_model_program(
        {**base, "program_json": key_over}
    )

    _assert_error(string_at_response, "invalid_type", "/program_json")
    _assert_error(string_over_response, "budget_exceeded", "/program_json")
    assert key_at_response["error"]["code"] == "unknown_field"
    assert len(key_at_response["error"]["path"].encode("utf-8")) <= 256
    _assert_error(key_over_response, "budget_exceeded", "/program_json")


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"outer":{"value":1,"value":2}}',
    ],
)
def test_duplicate_program_keys_are_rejected_before_the_port(raw):
    port = _FakePort(_stored())
    response = TaskApi(port=port).submit_model_program(
        {
            "schema_version": 1,
            "task_id": TASK_ID,
            "expected_generation": 0,
            "program_json": raw,
        }
    )
    _assert_error(response, "invalid_input", "/program_json")
    assert port.calls == []


@pytest.mark.parametrize("raw", ["{", "{} {}", "\ufeff{}", '"\\x"', "null", "[]"])
def test_malformed_and_non_mapping_program_roots_are_normalized(raw):
    response = TaskApi(port=object()).submit_model_program(
        {
            "schema_version": 1,
            "task_id": TASK_ID,
            "expected_generation": 0,
            "program_json": raw,
        }
    )
    expected = "invalid_input" if raw in {"{", "{} {}", "\ufeff{}", '"\\x"'} else "invalid_type"
    _assert_error(response, expected, "/program_json")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("NaN", "invalid_value"),
        ("Infinity", "invalid_value"),
        ("-Infinity", "invalid_value"),
        ("1e309", "invalid_value"),
        (str(MAX_SAFE_JSON_INTEGER), "invalid_type"),
        (str(MAX_SAFE_JSON_INTEGER + 1), "invalid_value"),
        ("1" * 17, "invalid_value"),
    ],
)
def test_program_numeric_hooks_are_finite_and_safe(raw, code):
    response = TaskApi(port=object()).submit_model_program(
        {
            "schema_version": 1,
            "task_id": TASK_ID,
            "expected_generation": 0,
            "program_json": raw,
        }
    )
    _assert_error(response, code, "/program_json")


def test_nested_model_program_errors_are_relocated_and_messages_are_fixed():
    missing = _program().to_mapping()
    missing.pop("acceptance")
    unknown = _program().to_mapping()
    unknown["rogue"] = "secret value"
    base = {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 0}

    missing_response = TaskApi(port=object()).submit_model_program(
        {**base, "program_json": json.dumps(missing)}
    )
    unknown_response = TaskApi(port=object()).submit_model_program(
        {**base, "program_json": json.dumps(unknown)}
    )

    _assert_error(missing_response, "missing_field", "/program_json/acceptance")
    _assert_error(unknown_response, "unknown_field", "/program_json/rogue")
    assert "secret value" not in json.dumps(unknown_response)


def test_capabilities_match_the_exact_sorted_public_projection():
    response = TaskApi(port=object()).get_capabilities({"schema_version": 1})
    result = _assert_success(response)
    assert set(result) == {"registry_schema_version", "operations"}
    assert result["registry_schema_version"] == 1
    operations = result["operations"]
    assert [item["operation"] for item in operations] == [
        "create_box",
        "create_cylinder",
        "inspect_model",
        "modify_parameter",
        "move_part",
        "rotate_part",
    ]
    operation_keys = {
        "operation",
        "risk_class",
        "evidence_required",
        "target_fields",
        "argument_fields",
        "execution_profiles",
        "minimum_freecad_version",
        "maximum_freecad_version_exclusive",
        "requires_gui_main_thread",
        "resource_budget",
        "direct_exposed",
        "result_slots",
        "preservation_fields",
    }
    field_keys = {
        "name",
        "value_shape",
        "required",
        "enum_values",
        "allowed_units",
        "referenced_value_shape",
    }
    slot_keys = {"name", "value_shape", "enum_values", "allowed_units"}
    for operation in operations:
        assert set(operation) == operation_keys
        assert operation["execution_profiles"] == sorted(operation["execution_profiles"])
        assert set(operation["resource_budget"]) == {
            "max_runtime_ms",
            "max_created_objects",
            "max_result_bytes",
        }
        for group in ("target_fields", "argument_fields"):
            assert [item["name"] for item in operation[group]] == sorted(
                item["name"] for item in operation[group]
            )
            assert all(set(item) == field_keys for item in operation[group])
        assert all(set(item) == slot_keys for item in operation["result_slots"])
        assert operation["preservation_fields"] == sorted(operation["preservation_fields"])
    create_box = operations[0]
    assert [item["name"] for item in create_box["argument_fields"]] == [
        "height_mm",
        "length_mm",
        "position_mm",
        "width_mm",
    ]


def test_capability_projection_is_registry_order_independent_and_defensive():
    reversed_registry = OperationRegistry(
        reversed(tuple(DEFAULT_OPERATION_REGISTRY.operations.values()))
    )
    default_api = TaskApi(port=object())
    reversed_api = TaskApi(port=object(), registry=reversed_registry)

    first = _assert_success(default_api.get_capabilities({"schema_version": 1}))
    other = _assert_success(reversed_api.get_capabilities({"schema_version": 1}))
    assert first == other
    first["operations"][0]["operation"] = "corrupted"
    second = _assert_success(default_api.get_capabilities({"schema_version": 1}))
    assert second == other


def test_capabilities_contain_no_implementation_or_runtime_availability_keys():
    result = _assert_success(TaskApi(port=object()).get_capabilities({"schema_version": 1}))
    forbidden = {
        "handler_name",
        "handler_parameter",
        "result_field",
        "callable",
        "source",
        "import_path",
        "module",
        "installed",
        "available",
        "runtime_available",
    }
    stack = [result]
    while stack:
        current = stack.pop()
        if type(current) is dict:
            assert set(current).isdisjoint(forbidden)
            stack.extend(current.values())
        elif type(current) is list:
            stack.extend(current)


def test_fresh_import_and_capability_call_do_not_load_forbidden_modules():
    script = """
import json
import sys
from vibecad.application.task_api import TaskApi

api = TaskApi(port=object())
response = api.get_capabilities({"schema_version": 1})
assert response["ok"] is True
for method in ("accept_draft", "reject_draft"):
    response = getattr(api, method)({"schema_version": 1})
    assert response["ok"] is False
    assert response["error"]["code"] == "missing_field"
forbidden = (
    "FreeCAD", "Part", "mcp", "fastmcp", "vibecad.server", "vibecad.runtime",
    "vibecad.engine", "vibecad.tools", "vibecad.workflow.service",
    "vibecad.execution.executor", "vibecad.execution.candidate",
    "vibecad.execution.revisions",
)
loaded = sorted(name for name in sys.modules if any(
    name == prefix or name.startswith(prefix + ".") for prefix in forbidden
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
