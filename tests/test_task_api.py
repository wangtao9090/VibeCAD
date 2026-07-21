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
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.execution.registry import DEFAULT_OPERATION_REGISTRY, OperationRegistry
from vibecad.workflow.contracts import AcceptanceSpec, ErrorCategory, ModelProgram, StepError
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.state import (
    CriterionOutcome,
    CriterionVerdict,
    ReasoningOwner,
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


def _report() -> VerificationReport:
    return VerificationReport(
        id="verification_0123456789abcdef0123456789abcdef",
        acceptance_id="acceptance-api",
        candidate_revision=CANDIDATE_REVISION,
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


def _created(*, task_id: str = TASK_ID, project_id: str = PROJECT_ID) -> TaskRun:
    return new_task_run(
        task_id=task_id,
        project_id=project_id,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
    )


def _task_at(status: TaskStatus) -> TaskRun:
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

    def get_task(self, **kwargs):
        return self._reply("get_task", kwargs)

    def submit_model_program(self, **kwargs):
        return self._reply("submit_model_program", kwargs)

    def continue_task(self, **kwargs):
        return self._reply("continue_task", kwargs)

    def reconcile_task(self, **kwargs):
        return self._reply("reconcile_task", kwargs)


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
        "recovery_required",
    }
    for name in (
        "create_task",
        "get_task",
        "submit_model_program",
        "resume_task",
        "get_capabilities",
    ):
        assert tuple(inspect.signature(getattr(TaskApi, name)).parameters) == ("self", "request")


def test_port_failure_requires_an_exact_closed_code():
    with pytest.raises(TypeError):
        TaskServicePortFailure(code="invalid_input")  # type: ignore[arg-type]


def test_create_owns_the_task_id_external_plan_and_exact_result_envelope():
    port = _FakePort(_stored())
    calls = 0

    def id_factory() -> str:
        nonlocal calls
        calls += 1
        return TASK_ID

    response = TaskApi(port=port, task_id_factory=id_factory).create_task(
        {"schema_version": 1, "project_id": PROJECT_ID}
    )

    result = _assert_success(response)
    assert calls == 1
    assert port.calls == [
        (
            "create_task",
            {
                "task_id": TASK_ID,
                "project_id": PROJECT_ID,
                "reasoning_owner": ReasoningOwner.EXTERNAL_PLAN,
            },
        )
    ]
    assert set(result) == {"generation", "next_action", "task_run"}
    assert result == {
        "generation": 0,
        "next_action": "submit_program",
        "task_run": _task_at(TaskStatus.NEEDS_PLAN).to_mapping(),
    }


def test_create_collision_calls_the_id_source_and_port_exactly_once():
    failure = TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
    port = _FakePort(failure)
    generated: list[str] = []

    response = TaskApi(
        port=port,
        task_id_factory=lambda: generated.append(TASK_ID) or TASK_ID,
    ).create_task({"schema_version": 1, "project_id": PROJECT_ID})

    _assert_error(response, "conflict", "")
    assert generated == [TASK_ID]
    assert [name for name, _ in port.calls] == ["create_task"]


@pytest.mark.parametrize("generated", ["TASK_" + "A" * 32, "task_short", 7, None])
def test_invalid_generated_id_is_internal_and_never_reaches_the_port(generated):
    port = _FakePort(_stored())
    api = TaskApi(port=port, task_id_factory=lambda: generated)

    response = api.create_task({"schema_version": 1, "project_id": PROJECT_ID})

    _assert_error(response, "internal_error", "")
    assert port.calls == []


def test_id_factory_raise_is_redacted_and_not_retried():
    port = _FakePort(_stored())
    calls = 0

    def fail():
        nonlocal calls
        calls += 1
        raise RuntimeError("/private/path secret")

    response = TaskApi(port=port, task_id_factory=fail).create_task(
        {"schema_version": 1, "project_id": PROJECT_ID}
    )

    _assert_error(response, "internal_error", "")
    assert calls == 1
    assert "private" not in json.dumps(response)
    assert port.calls == []


def test_internal_failure_type_raised_by_factory_or_port_is_never_trusted():
    injected = task_api_module._ApiFailure(  # type: ignore[attr-defined]
        TaskApiErrorCode.INVALID_INPUT,
        "/secret/private/path",
    )
    port = _FakePort(injected)

    port_response = TaskApi(port=port).get_task({"schema_version": 1, "task_id": TASK_ID})
    factory_response = TaskApi(
        port=_FakePort(_stored()),
        task_id_factory=lambda: (_ for _ in ()).throw(injected),
    ).create_task({"schema_version": 1, "project_id": PROJECT_ID})

    _assert_error(port_response, "internal_error", "")
    _assert_error(factory_response, "internal_error", "")
    assert "/secret" not in json.dumps(port_response)
    assert "/secret" not in json.dumps(factory_response)


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

    response = TaskApi(port=_FakePort(forged)).get_task(
        {"schema_version": 1, "task_id": TASK_ID}
    )

    _assert_error(response, "internal_error", "")


def test_create_rejects_a_created_or_wrong_project_port_result_as_internal():
    values = [
        StoredTaskRun(generation=0, task_run=_task_at(TaskStatus.CREATED)),
        StoredTaskRun(
            generation=0,
            task_run=transition_task(
                _created(project_id="project_11111111111111111111111111111111"),
                TaskEvent.REQUEST_PLAN,
            ),
        ),
        StoredTaskRun(generation=1, task_run=_task_at(TaskStatus.NEEDS_PLAN)),
    ]
    for value in values:
        response = TaskApi(port=_FakePort(value), task_id_factory=lambda: TASK_ID).create_task(
            {"schema_version": 1, "project_id": PROJECT_ID}
        )
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
        ("create_task", {"schema_version": 1}, "/project_id"),
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
    response = TaskApi(port=object()).get_capabilities(
        {"schema_version": 1, "z": 0, "a/b~c": 0}
    )
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
        ("create_task", {"schema_version": 1, "project_id": 1}, "invalid_type", "/project_id"),
        (
            "create_task",
            {"schema_version": 1, "project_id": "PROJECT_" + "A" * 32},
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
    response = TaskApi(port=port).create_task({"schema_version": 1, "project_id": value})
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
        ("create_task", {"schema_version": 1, "project_id": ""}, "project_id"),
        ("get_task", {"schema_version": 1, "task_id": ""}, "task_id"),
        (
            "resume_task",
            {"schema_version": 1, "task_id": "", "expected_generation": 0},
            "task_id",
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
        {"schema_version": 1, "project_id": "x" * 4_097}
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
    long_response = TaskApi(port=object()).get_capabilities(
        {"schema_version": 1, long_name: 0}
    )

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
    key_at_response = TaskApi(port=object()).submit_model_program(
        {**base, "program_json": key_at}
    )
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

response = TaskApi(port=object()).get_capabilities({"schema_version": 1})
assert response["ok"] is True
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
