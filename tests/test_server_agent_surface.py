"""Registry-derived direct Agent surface contract tests."""

from __future__ import annotations

import importlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType

import anyio
import pytest

from vibecad.application.task_api import TaskServicePortErrorCode, TaskServicePortFailure
from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
)
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskStatus,
    new_task_run,
    transition_task,
)
from vibecad.workflow.store import StoredTaskRun

TASK_ID = "task_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
OTHER_PROJECT_ID = "project_11111111111111111111111111111111"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
OTHER_REVISION = "revision_11111111111111111111111111111111"

STABLE_TOOL_NAMES = (
    "ping",
    "get_runtime_status",
    "ensure_runtime",
    "uninstall_runtime",
    "get_capabilities",
    "create_project",
    "get_project",
    "list_projects",
    "list_revisions",
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_events",
    "submit_model_program",
    "resume_task",
    "accept_draft",
    "reject_draft",
    "export_task_artifacts",
)


def _surface_module():
    return importlib.import_module("vibecad.application.public_surface")


def _acceptance() -> AcceptanceSpec:
    return AcceptanceSpec(
        id="acceptance-direct",
        criteria=(
            AcceptanceCriterion(
                id="valid-shape",
                kind=AcceptanceKind.TOPOLOGY,
                check="valid_shape",
                target="model",
                expected=True,
            ),
        ),
    )


def _acceptance_json(spec: AcceptanceSpec | None = None) -> str:
    return json.dumps(
        (spec or _acceptance()).to_mapping(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _needs_plan() -> StoredTaskRun:
    created = new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    return StoredTaskRun(
        generation=0,
        task_run=transition_task(created, TaskEvent.REQUEST_PLAN),
    )


def _selector(
    *,
    project_id: str = PROJECT_ID,
    revision_id: str = BASE_REVISION,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "revision_id": revision_id,
        "entity_kind": "object",
        "object_id": "object_0123456789abcdef0123456789abcdef",
        "feature_id": None,
        "object_type": "Part::Feature",
        "semantic_role": "primitive",
        "provenance": {"source": "model", "operation_id": "create"},
        "expected_cardinality": 1,
    }


def _request(
    *,
    target: dict[str, object] | None = None,
    arguments: dict[str, object] | None = None,
    preserve: list[str] | None = None,
    acceptance_json: str | None = None,
    generation: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "expected_generation": generation,
        "target": {} if target is None else target,
        "arguments": (
            {"length_mm": 10, "width_mm": 20, "height_mm": 30} if arguments is None else arguments
        ),
        "preserve": [] if preserve is None else preserve,
        "acceptance_json": acceptance_json or _acceptance_json(),
    }


def _failure(code: str, path: str = "") -> dict[str, object]:
    messages = {
        "missing_field": "A required request field is missing.",
        "unknown_field": "The request contains an unknown field.",
        "invalid_type": "A request value has an invalid type.",
        "invalid_value": "A request value is invalid.",
        "budget_exceeded": "The request exceeds a resource budget.",
        "invalid_input": "The request is invalid.",
        "invalid_state": "The task is not ready for this operation.",
        "not_found": "The task record was not found.",
        "conflict": "The task record changed concurrently.",
        "internal_error": "The request could not be completed.",
    }
    return {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": code,
            "path": path,
            "message": messages[code],
        },
    }


class _Port:
    def __init__(self, stored: StoredTaskRun | TaskServicePortFailure | None = None) -> None:
        self.stored = _needs_plan() if stored is None else stored
        self.get_calls: list[str] = []
        self.submit_calls: list[tuple[str, int, ModelProgram]] = []
        self.submit_failure: TaskServicePortFailure | None = None
        self.raise_after_effect = False

    def get_task(self, *, task_id: str):
        self.get_calls.append(task_id)
        return self.stored

    def submit_model_program(
        self,
        *,
        task_id: str,
        expected_generation: int,
        program: ModelProgram,
    ):
        self.submit_calls.append((task_id, expected_generation, program))
        if self.submit_failure is not None:
            return self.submit_failure
        assert type(self.stored) is StoredTaskRun
        self.stored = StoredTaskRun(
            generation=expected_generation + 1,
            task_run=transition_task(
                self.stored.task_run,
                TaskEvent.SUBMIT_PROGRAM,
                program=program,
            ),
        )
        if self.raise_after_effect:
            raise RuntimeError("secret lost response")
        return self.stored


def test_direct_success_compiles_exact_one_command_and_returns_task_api_envelope():
    module = _surface_module()
    port = _Port()
    api = module.DirectOperationApi(port=port)

    result = api.invoke("create_box", _request())

    assert port.get_calls == [TASK_ID]
    assert len(port.submit_calls) == 1
    submitted_task, submitted_generation, program = port.submit_calls[0]
    assert (submitted_task, submitted_generation) == (TASK_ID, 0)
    assert program.to_mapping() == {
        "schema_version": 1,
        "task_id": TASK_ID,
        "base_revision": BASE_REVISION,
        "operations": [
            {
                "schema_version": 1,
                "id": "direct_operation",
                "op": "create_box",
                "target": {},
                "args": {"height_mm": 30, "length_mm": 10, "width_mm": 20},
                "preserve": [],
                "source": ValueSource.MODEL.value,
                "depends_on": [],
            }
        ],
        "acceptance": _acceptance().to_mapping(),
    }
    assert type(port.stored) is StoredTaskRun
    assert result == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "generation": 1,
            "next_action": "validate_program",
            "task_run": port.stored.task_run.to_mapping(),
        },
        "error": None,
    }


def test_direct_operation_name_is_exact_closed_and_never_reflected_or_compared():
    module = _surface_module()
    port = _Port()
    api = module.DirectOperationApi(port=port)

    class EvilName:
        equality_calls = 0

        def __eq__(self, _other):
            self.equality_calls += 1
            raise AssertionError("must not compare attacker objects")

        def __hash__(self):
            raise AssertionError("must not hash attacker objects")

        def __str__(self):
            raise AssertionError("must not reflect attacker objects")

    evil = EvilName()
    assert api.invoke(evil, _request()) == _failure("invalid_input")
    assert evil.equality_calls == 0
    assert api.invoke("secret_unknown_operation", _request()) == _failure("invalid_input")
    assert port.get_calls == []
    assert port.submit_calls == []


def test_direct_outer_ingress_is_exact_and_precedes_every_port_call():
    module = _surface_module()
    cases = []
    missing = _request()
    del missing["target"]
    cases.append((missing, _failure("missing_field", "/target")))
    unknown = {**_request(), "secret_output_path": "/secret"}
    cases.append((unknown, _failure("unknown_field", "/_unknown")))
    cases.append(
        (
            {**_request(), "expected_generation": True},
            _failure("invalid_type", "/expected_generation"),
        )
    )
    cases.append(({**_request(), "target": []}, _failure("invalid_type", "/target")))
    cases.append(({**_request(), "preserve": ()}, _failure("invalid_type", "/preserve")))

    for request, expected in cases:
        port = _Port()
        assert module.DirectOperationApi(port=port).invoke("create_box", request) == expected
        assert port.get_calls == []
        assert port.submit_calls == []


def test_registry_shapes_empty_create_and_inspect_contracts_fail_before_task_read():
    module = _surface_module()
    cases = (
        ("create_box", _request(target={"object": _selector()})),
        ("create_box", _request(arguments={"length_mm": 1, "width_mm": 2})),
        ("inspect_model", _request(arguments={"unexpected": 1})),
        ("inspect_model", _request(target={"unexpected": 1}, arguments={})),
        (
            "modify_parameter",
            _request(
                target={"object": {"command_id": "earlier", "slot": "object"}},
                arguments={"parameter": "height", "value_mm": 10},
            ),
        ),
        (
            "move_part",
            _request(
                target={"object": _selector()},
                arguments={"position_mm": [1, 2]},
            ),
        ),
        (
            "rotate_part",
            _request(
                target={"object": _selector()},
                arguments={"axis": "z", "angle_deg": 0},
            ),
        ),
        ("create_box", _request(preserve=["geometry"])),
    )
    for operation, request in cases:
        port = _Port()
        result = module.DirectOperationApi(port=port).invoke(operation, request)
        assert result["ok"] is False
        assert result["error"]["code"] in {
            "missing_field",
            "unknown_field",
            "invalid_type",
            "invalid_value",
            "invalid_input",
        }
        assert port.get_calls == []
        assert port.submit_calls == []


def test_duplicate_checked_acceptance_and_acceptance_budgets_precede_task_read():
    module = _surface_module()
    duplicate = '{"schema_version":1,"schema_version":1,"id":"a","criteria":[]}'
    over = "x" * (262_144 + 1)
    for raw, code in ((duplicate, "invalid_input"), (over, "budget_exceeded")):
        port = _Port()
        result = module.DirectOperationApi(port=port).invoke(
            "create_box",
            _request(acceptance_json=raw),
        )
        assert result == _failure(code, "/acceptance_json")
        assert port.get_calls == []
        assert port.submit_calls == []


@pytest.mark.parametrize("field", ["project_id", "revision_id"])
def test_mutator_selector_must_bind_exact_durable_task(field: str):
    module = _surface_module()
    selector = _selector(
        project_id=OTHER_PROJECT_ID if field == "project_id" else PROJECT_ID,
        revision_id=OTHER_REVISION if field == "revision_id" else BASE_REVISION,
    )
    port = _Port()
    result = module.DirectOperationApi(port=port).invoke(
        "modify_parameter",
        _request(
            target={"object": selector},
            arguments={"parameter": "height", "value_mm": 10},
        ),
    )
    assert result == _failure("invalid_input", f"/target/object/{field}")
    assert port.get_calls == [TASK_ID]
    assert port.submit_calls == []


def test_task_snapshot_generation_and_status_are_checked_before_effect():
    module = _surface_module()
    wrong_generation = _Port(StoredTaskRun(generation=1, task_run=_needs_plan().task_run))
    assert module.DirectOperationApi(port=wrong_generation).invoke(
        "create_box", _request()
    ) == _failure("conflict")
    assert wrong_generation.submit_calls == []

    created = new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    wrong_status = _Port(StoredTaskRun(generation=0, task_run=created))
    assert module.DirectOperationApi(port=wrong_status).invoke(
        "create_box", _request()
    ) == _failure("invalid_state")
    assert wrong_status.submit_calls == []


@pytest.mark.parametrize("validator_name", ["compile_acceptance_spec", "validate_model_program"])
def test_pure_validators_run_before_the_single_effecting_call(monkeypatch, validator_name: str):
    module = _surface_module()
    port = _Port()

    def fail(_value, **_kwargs):
        raise ValueError("secret validation failure")

    monkeypatch.setattr(module, validator_name, fail)
    result = module.DirectOperationApi(port=port).invoke("create_box", _request())
    assert result == _failure("invalid_input")
    assert port.get_calls == [TASK_ID]
    assert port.submit_calls == []


def test_port_failures_keep_the_task_api_taxonomy_and_envelope():
    module = _surface_module()
    missing = _Port(TaskServicePortFailure(code=TaskServicePortErrorCode.NOT_FOUND))
    assert module.DirectOperationApi(port=missing).invoke("create_box", _request()) == _failure(
        "not_found"
    )
    assert missing.submit_calls == []

    conflict = _Port()
    conflict.submit_failure = TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
    assert module.DirectOperationApi(port=conflict).invoke("create_box", _request()) == _failure(
        "conflict"
    )
    assert len(conflict.submit_calls) == 1


def test_lost_response_replay_cannot_execute_a_second_effect():
    module = _surface_module()
    port = _Port()
    port.raise_after_effect = True
    api = module.DirectOperationApi(port=port)

    assert api.invoke("create_box", _request()) == _failure("internal_error")
    assert len(port.submit_calls) == 1
    assert type(port.stored) is StoredTaskRun
    assert port.stored.task_run.status is TaskStatus.PROGRAM_READY

    assert api.invoke("create_box", _request()) == _failure("conflict")
    assert len(port.submit_calls) == 1


def test_direct_operation_set_is_registry_derived_and_sorted_without_literal_count():
    module = _surface_module()
    expected = tuple(
        sorted(
            name
            for name, metadata in DEFAULT_OPERATION_REGISTRY.operations.items()
            if metadata.direct_exposed
        )
    )
    assert module.direct_operation_names() == expected
    assert all(
        DEFAULT_OPERATION_REGISTRY.operations[name].direct_exposed
        for name in module.direct_operation_names()
    )


def _direct_names(registry: OperationRegistry) -> tuple[str, ...]:
    return tuple(
        sorted(name for name, metadata in registry.operations.items() if metadata.direct_exposed)
    )


def _spec_by_name(specs):
    return {spec.name: spec for spec in specs}


def test_public_tool_specs_are_stable_controls_then_sorted_registry_projection():
    module = _surface_module()
    expected_direct = _direct_names(DEFAULT_OPERATION_REGISTRY)
    specs = module.public_tool_specs()
    assert tuple(spec.name for spec in specs) == STABLE_TOOL_NAMES + expected_direct

    chosen = tuple(
        DEFAULT_OPERATION_REGISTRY.operations[name] for name in reversed(expected_direct)
    )
    hidden = replace(chosen[0], direct_exposed=False)
    custom = OperationRegistry((hidden, *chosen[1:]))
    custom_specs = module.public_tool_specs(custom)
    assert tuple(spec.name for spec in custom_specs) == STABLE_TOOL_NAMES + _direct_names(custom)
    assert hidden.operation not in tuple(spec.name for spec in custom_specs)


def test_public_annotations_match_the_independent_product_contract():
    module = _surface_module()
    expected = {
        "ping": (True, False, True, False),
        "get_runtime_status": (False, False, True, False),
        "ensure_runtime": (False, True, True, True),
        "uninstall_runtime": (False, True, True, False),
        "get_capabilities": (True, False, True, False),
        "create_project": (False, False, True, True),
        "get_project": (False, False, True, False),
        "list_projects": (True, False, True, False),
        "list_revisions": (True, False, True, False),
        "create_task": (False, False, True, False),
        "list_tasks": (True, False, True, False),
        "get_task": (False, False, True, False),
        "get_task_events": (True, False, True, False),
        "submit_model_program": (False, True, True, False),
        "resume_task": (False, True, True, False),
        "accept_draft": (False, True, True, False),
        "reject_draft": (False, True, True, False),
        "export_task_artifacts": (False, False, True, False),
        "create_box": (False, False, True, False),
        "create_cylinder": (False, False, True, False),
        "inspect_model": (False, False, True, False),
        "modify_parameter": (False, True, True, False),
        "move_part": (False, True, True, False),
        "rotate_part": (False, True, True, False),
    }
    actual = {
        spec.name: (
            spec.annotations.read_only,
            spec.annotations.destructive,
            spec.annotations.idempotent,
            spec.annotations.open_world,
        )
        for spec in module.public_tool_specs()
    }
    assert actual == expected


def test_every_public_schema_is_closed_complete_and_specialized():
    module = _surface_module()
    specs = _spec_by_name(module.public_tool_specs())
    expected_required = {
        "ping": (),
        "get_runtime_status": (),
        "ensure_runtime": (),
        "uninstall_runtime": ("confirm",),
        "get_capabilities": ("schema_version",),
        "create_project": ("schema_version", "create_key", "kind"),
        "get_project": ("schema_version", "project_id"),
        "list_projects": ("schema_version",),
        "list_revisions": ("schema_version", "project_id"),
        "create_task": ("schema_version", "create_key", "project_id", "review_policy"),
        "list_tasks": ("schema_version",),
        "get_task": ("schema_version", "task_id"),
        "get_task_events": ("schema_version", "task_id"),
        "submit_model_program": (
            "schema_version",
            "task_id",
            "expected_generation",
            "program_json",
        ),
        "resume_task": ("schema_version", "task_id", "expected_generation"),
        "accept_draft": (
            "schema_version",
            "task_id",
            "draft_id",
            "expected_generation",
        ),
        "reject_draft": (
            "schema_version",
            "task_id",
            "draft_id",
            "expected_generation",
        ),
        "export_task_artifacts": (
            "schema_version",
            "export_key",
            "task_id",
            "expected_generation",
            "revision_id",
            "draft_id",
        ),
    }
    for name in STABLE_TOOL_NAMES:
        input_schema = specs[name].input_schema
        output_schema = specs[name].output_schema
        assert input_schema["type"] == "object"
        assert input_schema["additionalProperties"] is False
        assert input_schema["required"] == expected_required[name]
        assert set(input_schema["required"]) <= set(input_schema["properties"])
        assert output_schema["type"] == "object"
        assert output_schema["additionalProperties"] is False
        assert output_schema["required"] == (
            "schema_version",
            "ok",
            "result",
            "error",
        )
        assert set(output_schema["properties"]) == {
            "schema_version",
            "ok",
            "result",
            "error",
        }
    assert set(specs["ping"].output_schema["properties"]["result"]["anyOf"][0]["properties"]) == {
        "schema_version",
        "service",
        "version",
    }
    assert set(
        specs["export_task_artifacts"].output_schema["properties"]["result"]["anyOf"][0][
            "properties"
        ]
    ) == {
        "schema_version",
        "export_key",
        "materialization_id",
        "source_kind",
        "task_id",
        "task_generation",
        "project_id",
        "revision_id",
        "manifest_sha256",
        "authoritative",
        "artifacts",
    }


@pytest.mark.parametrize(
    ("name", "cursor_pattern", "items_field"),
    [
        ("list_projects", r"^project_list_cursor_[0-9a-f]{64}$", "projects"),
        ("list_revisions", r"^revision_list_cursor_[0-9a-f]{64}$", "revisions"),
        ("list_tasks", r"^task_list_cursor_[0-9a-f]{64}$", "tasks"),
        ("get_task_events", r"^task_event_cursor_[0-9a-f]{64}$", "transitions"),
    ],
)
def test_discovery_schemas_freeze_optional_bounds_and_cursor_domains(
    name: str,
    cursor_pattern: str,
    items_field: str,
):
    spec = _spec_by_name(_surface_module().public_tool_specs())[name]
    properties = spec.input_schema["properties"]
    assert properties["limit"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
    }
    assert properties["cursor"]["anyOf"] == (
        {"type": "string", "pattern": cursor_pattern},
        {"type": "null"},
    )
    result_schema = spec.output_schema["properties"]["result"]["anyOf"][0]
    assert result_schema["properties"][items_field]["maxItems"] == 100
    assert result_schema["properties"]["next_cursor"]["anyOf"] == (
        {"type": "string", "pattern": cursor_pattern},
        {"type": "null"},
    )


def test_project_discovery_output_schemas_are_bounded_and_exact() -> None:
    specs = _spec_by_name(_surface_module().public_tool_specs())
    project_result = specs["list_projects"].output_schema["properties"]["result"]["anyOf"][0]
    assert tuple(project_result["properties"]) == (
        "schema_version",
        "projects",
        "next_cursor",
    )
    project_item = project_result["properties"]["projects"]["items"]
    assert tuple(project_item["properties"]) == (
        "schema_version",
        "project_id",
        "generation",
        "revision_id",
        "manifest_sha256",
    )

    revision_result = specs["list_revisions"].output_schema["properties"]["result"]["anyOf"][0]
    assert tuple(revision_result["properties"]) == (
        "schema_version",
        "project_id",
        "head",
        "revisions",
        "next_cursor",
    )
    assert revision_result["properties"]["head"] == project_item
    revision_item = revision_result["properties"]["revisions"]["items"]
    assert tuple(revision_item["properties"]) == (
        "schema_version",
        "id",
        "project_id",
        "base_revision",
        "manifest_sha256",
    )


def test_direct_input_schema_is_registry_derived_and_closed():
    module = _surface_module()
    specs = _spec_by_name(module.public_tool_specs())
    outer_fields = (
        "schema_version",
        "task_id",
        "expected_generation",
        "target",
        "arguments",
        "preserve",
        "acceptance_json",
    )
    box = specs["create_box"].input_schema
    assert box["required"] == outer_fields
    assert tuple(box["properties"]) == outer_fields
    assert box["additionalProperties"] is False
    assert box["properties"]["target"]["properties"] == {}
    assert box["properties"]["target"]["additionalProperties"] is False
    arguments = box["properties"]["arguments"]
    assert arguments["required"] == ("length_mm", "width_mm", "height_mm")
    assert set(arguments["properties"]) == {
        "length_mm",
        "width_mm",
        "height_mm",
        "position_mm",
    }
    assert arguments["properties"]["length_mm"] == {
        "type": "number",
        "exclusiveMinimum": 0,
    }
    assert arguments["properties"]["position_mm"]["minItems"] == 3
    assert arguments["properties"]["position_mm"]["maxItems"] == 3
    assert box["properties"]["preserve"]["maxItems"] == 0
    assert box["properties"]["acceptance_json"]["maxLength"] == 262_144

    inspect = specs["inspect_model"].input_schema["properties"]
    assert inspect["target"]["properties"] == {}
    assert inspect["arguments"]["properties"] == {}


def test_mutator_schema_uses_full_selector_v1_and_never_result_ref():
    module = _surface_module()
    spec = _spec_by_name(module.public_tool_specs())["modify_parameter"]
    target = spec.input_schema["properties"]["target"]
    selector = target["properties"]["object"]
    selector_fields = (
        "schema_version",
        "project_id",
        "revision_id",
        "entity_kind",
        "object_id",
        "feature_id",
        "object_type",
        "semantic_role",
        "provenance",
        "expected_cardinality",
    )
    assert selector["required"] == selector_fields
    assert tuple(selector["properties"]) == selector_fields
    assert selector["additionalProperties"] is False
    assert "result_ref" not in repr(selector)
    assert "command_id" not in selector["properties"]
    assert selector["properties"]["expected_cardinality"] == {
        "type": "integer",
        "const": 1,
    }
    assert selector["properties"]["provenance"]["additionalProperties"] is False
    arguments = spec.input_schema["properties"]["arguments"]
    assert arguments["properties"]["parameter"]["enum"] == (
        "height",
        "length",
        "radius",
        "width",
    )
    expected_preserve = DEFAULT_OPERATION_REGISTRY.operations[
        "modify_parameter"
    ].preservation_fields
    assert spec.input_schema["properties"]["preserve"]["items"]["enum"] == (expected_preserve)


def _assert_deeply_frozen(value: object) -> None:
    if isinstance(value, MappingProxyType):
        for nested in value.values():
            _assert_deeply_frozen(nested)
        return
    assert not isinstance(value, (dict, list, set))
    if isinstance(value, tuple):
        for nested in value:
            _assert_deeply_frozen(nested)


def test_public_metadata_is_deeply_frozen_deterministic_and_fresh():
    module = _surface_module()
    first = module.public_tool_specs()
    second = module.public_tool_specs()
    assert first == second
    assert first is not second
    assert all(left is not right for left, right in zip(first, second, strict=True))

    for spec in first:
        _assert_deeply_frozen(spec.input_schema)
        _assert_deeply_frozen(spec.output_schema)
    with pytest.raises(TypeError):
        first[0].input_schema["extra"] = True
    with pytest.raises(TypeError):
        first[0].output_schema["properties"]["ok"]["type"] = "string"
    with pytest.raises(AttributeError):
        first[0].input_schema["required"].append("extra")
    with pytest.raises(FrozenInstanceError):
        first[0].name = "other"


class _HostileOperationKey(str):
    armed = False
    calls: list[str] = []

    @classmethod
    def _trip(cls, method: str) -> None:
        if cls.armed:
            cls.calls.append(method)
            raise RuntimeError("secret-operation-key")

    def __hash__(self):
        type(self)._trip("__hash__")
        return str.__hash__(self)

    def __eq__(self, other):
        type(self)._trip("__eq__")
        return str.__eq__(self, other)

    def __lt__(self, other):
        type(self)._trip("__lt__")
        return str.__lt__(self, other)

    def __str__(self):
        type(self)._trip("__str__")
        return str.__str__(self)


class _HostileOperationMetadata(OperationMetadata):
    __slots__ = ()
    armed = False
    calls: list[str] = []

    def __getattribute__(self, name: str):
        cls = type(self)
        if cls.armed:
            cls.calls.append(name)
            raise RuntimeError("secret-operation-metadata")
        return object.__getattribute__(self, name)


class _HostileFieldMetadata(FieldMetadata):
    __slots__ = ()
    armed = False
    calls: list[str] = []

    def __getattribute__(self, name: str):
        cls = type(self)
        if cls.armed:
            cls.calls.append(name)
            raise RuntimeError("secret-field-metadata")
        return object.__getattribute__(self, name)


def _operation_metadata_subclass() -> _HostileOperationMetadata:
    base = DEFAULT_OPERATION_REGISTRY.operations["create_box"]
    return _HostileOperationMetadata(
        operation=base.operation,
        handler_name=base.handler_name,
        risk_class=base.risk_class,
        evidence_required=base.evidence_required,
        target_fields=base.target_fields,
        argument_fields=base.argument_fields,
        execution_profiles=base.execution_profiles,
        minimum_freecad_version=base.minimum_freecad_version,
        maximum_freecad_version_exclusive=base.maximum_freecad_version_exclusive,
        requires_gui_main_thread=base.requires_gui_main_thread,
        resource_budget=base.resource_budget,
        direct_exposed=base.direct_exposed,
        result_slots=base.result_slots,
        preservation_fields=base.preservation_fields,
    )


def _field_metadata_subclass() -> _HostileFieldMetadata:
    base = DEFAULT_OPERATION_REGISTRY.operations["create_box"].argument_fields[0]
    return _HostileFieldMetadata(
        name=base.name,
        handler_parameter=base.handler_parameter,
        value_shape=base.value_shape,
        required=base.required,
        enum_values=base.enum_values,
        allowed_units=base.allowed_units,
        referenced_value_shape=base.referenced_value_shape,
    )


def _forged_registry(entries: dict[object, object]) -> OperationRegistry:
    registry = OperationRegistry(())
    object.__setattr__(registry, "_operations", MappingProxyType(entries))
    return registry


def _assert_public_registry_rejected(entrypoint: str, registry: OperationRegistry) -> None:
    module = _surface_module()
    port = _Port()
    with pytest.raises(TypeError) as captured:
        if entrypoint == "names":
            module.direct_operation_names(registry)
        elif entrypoint == "api":
            module.DirectOperationApi(port=port, registry=registry)
        else:
            module.public_tool_specs(registry)
    assert str(captured.value) == "registry public metadata is invalid"
    assert port.get_calls == []
    assert port.submit_calls == []


@pytest.mark.parametrize("entrypoint", ["names", "api", "specs"])
def test_operation_key_subclass_is_rejected_before_magic_methods(entrypoint: str):
    first = _HostileOperationKey("create_box")
    second = _HostileOperationKey("create_cylinder")
    registry = _forged_registry(
        {
            first: DEFAULT_OPERATION_REGISTRY.operations["create_box"],
            second: DEFAULT_OPERATION_REGISTRY.operations["create_cylinder"],
        }
    )
    _HostileOperationKey.calls.clear()
    _HostileOperationKey.armed = True
    try:
        _assert_public_registry_rejected(entrypoint, registry)
    finally:
        _HostileOperationKey.armed = False
    assert _HostileOperationKey.calls == []


@pytest.mark.parametrize("entrypoint", ["names", "api", "specs"])
def test_operation_metadata_subclass_is_rejected_before_attribute_read(entrypoint: str):
    hostile = _operation_metadata_subclass()
    registry = _forged_registry({"create_box": hostile})
    _HostileOperationMetadata.calls.clear()
    _HostileOperationMetadata.armed = True
    try:
        _assert_public_registry_rejected(entrypoint, registry)
    finally:
        _HostileOperationMetadata.armed = False
    assert _HostileOperationMetadata.calls == []


@pytest.mark.parametrize("entrypoint", ["names", "api", "specs"])
def test_nested_field_subclass_is_rejected_before_attribute_read(entrypoint: str):
    hostile = _field_metadata_subclass()
    base = DEFAULT_OPERATION_REGISTRY.operations["create_box"]
    metadata = replace(base, argument_fields=(hostile, *base.argument_fields[1:]))
    registry = _forged_registry({"create_box": metadata})
    _HostileFieldMetadata.calls.clear()
    _HostileFieldMetadata.armed = True
    try:
        _assert_public_registry_rejected(entrypoint, registry)
    finally:
        _HostileFieldMetadata.armed = False
    assert _HostileFieldMetadata.calls == []


def test_registry_rejection_precedes_public_schema_generation(monkeypatch):
    hostile = _operation_metadata_subclass()
    registry = _forged_registry({"create_box": hostile})
    module = _surface_module()
    schema_calls: list[str] = []

    def forbidden_schema(name: str):
        schema_calls.append(name)
        raise AssertionError("schema generation must not start")

    monkeypatch.setattr(module, "_stable_input_schema", forbidden_schema)
    _HostileOperationMetadata.calls.clear()
    _HostileOperationMetadata.armed = True
    try:
        with pytest.raises(TypeError) as captured:
            module.public_tool_specs(registry)
    finally:
        _HostileOperationMetadata.armed = False
    assert str(captured.value) == "registry public metadata is invalid"
    assert _HostileOperationMetadata.calls == []
    assert schema_calls == []


def _server_module():
    return importlib.import_module("vibecad.server")


def _internal_envelope() -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": "internal_error",
            "path": "",
            "message": "The request could not be completed.",
        },
    }


def test_server_import_and_discovery_are_lazy_and_have_no_legacy_session(tmp_path: Path) -> None:
    home = tmp_path / "never-created"
    script = f"""
import anyio
import json
import os
import sys
os.environ['VIBECAD_HOME'] = {str(home)!r}
import vibecad.server as server
anyio.run(server._handle_list_tools)
anyio.run(server._handle_list_resource_templates)
forbidden = ('FreeCAD', 'Part', 'vibecad.engine.session', 'vibecad.tools',
             'vibecad.application.agent', 'vibecad.application.project_create',
             'vibecad.application.artifacts', 'vibecad.execution.candidate',
             'vibecad.execution.executor')
loaded = sorted(name for name in sys.modules if any(
    name == prefix or name.startswith(prefix + '.') for prefix in forbidden
))
assert loaded == [], json.dumps(loaded)
assert not os.path.exists({str(home)!r})
source = open(server.__file__, encoding='utf-8').read()
assert '@mcp.tool' not in source
assert '_session' not in source
assert 'vibecad.tools' not in source
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_sdk_namespace_and_direct_root_path_logs_are_discarded() -> None:
    server = _server_module()
    sdk_logger = logging.getLogger("mcp")
    assert sdk_logger.propagate is False
    assert len(sdk_logger.handlers) == 1
    assert type(sdk_logger.handlers[0]) is server._DiscardOnlyHandler

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    root = logging.getLogger()
    handler = Capture()
    root.addHandler(handler)
    try:
        root.handle(
            logging.LogRecord(
                "root",
                logging.ERROR,
                "/private/site-packages/mcp/shared/secret_session.py",
                10,
                "secret request value",
                (),
                None,
            )
        )
    finally:
        root.removeHandler(handler)
    assert records == []


def test_low_level_tools_list_is_exact_sdk_projection_of_public_specs() -> None:
    from mcp import types

    server = _server_module()
    result = anyio.run(server._handle_list_tools)
    specs = _surface_module().public_tool_specs()

    assert type(result) is types.ListToolsResult
    assert [tool.name for tool in result.tools] == [spec.name for spec in specs]
    for tool, spec in zip(result.tools, specs, strict=True):
        assert type(tool) is types.Tool
        assert tool.description == spec.description
        assert tool.inputSchema == server._thaw_json(spec.input_schema)
        assert tool.outputSchema is None
        assert server._OUTPUT_SCHEMAS[tool.name] == server._validation_schema(spec.output_schema)
        assert type(tool.annotations) is types.ToolAnnotations
        assert (
            tool.annotations.readOnlyHint,
            tool.annotations.destructiveHint,
            tool.annotations.idempotentHint,
            tool.annotations.openWorldHint,
        ) == (
            spec.annotations.read_only,
            spec.annotations.destructive,
            spec.annotations.idempotent,
            spec.annotations.open_world,
        )


def test_every_discovered_tool_has_a_nonempty_single_line_description() -> None:
    result = anyio.run(_server_module()._handle_list_tools)

    assert len(result.tools) == 24
    for tool in result.tools:
        assert type(tool.description) is str, tool.name
        assert tool.description == tool.description.strip(), tool.name
        assert tool.description, tool.name
        assert "\n" not in tool.description and "\r" not in tool.description, tool.name


def test_owned_tools_list_fixed_frame_fits_the_discovery_budget() -> None:
    from vibecad import mcp_transport

    server = _server_module()
    descriptor = mcp_transport.prevalidate_client_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    response = server._owned_dispatch_descriptor(descriptor)
    assert response is not None
    frame = mcp_transport.OwnedStdioRunner._encoded_response(response)
    assert frame == (
        json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    assert response["id"] == 1
    assert len(frame) <= 32_768


def test_discovery_omits_optional_output_schema_from_every_tool() -> None:
    from vibecad import mcp_transport

    server = _server_module()
    descriptor = mcp_transport.prevalidate_client_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )

    response = server._owned_dispatch_descriptor(descriptor)
    assert response is not None
    tools = response["result"]["tools"]
    assert len(tools) == 24
    assert all("outputSchema" not in tool for tool in tools)


def test_owned_dispatch_manually_initializes_and_uses_typed_sdk_handlers() -> None:
    from mcp import types

    from vibecad import mcp_transport

    server = _server_module()
    initialize = mcp_transport.prevalidate_client_message(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    initialized = server._owned_dispatch_descriptor(initialize)

    assert initialized == {
        "jsonrpc": "2.0",
        "id": "init",
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {
                "experimental": {},
                "resources": {"subscribe": False, "listChanged": False},
                "tools": {"listChanged": False},
            },
            "serverInfo": {"name": "vibecad", "version": server.__version__},
        },
    }

    ping = mcp_transport.prevalidate_client_message(
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
    )
    assert server._owned_dispatch_descriptor(ping) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }
    typed = types.ClientRequest.model_validate({"method": ping.method, "params": dict(ping.params)})
    assert isinstance(typed.root, types.PingRequest)


def test_owned_dispatch_sanitizes_missing_handler_and_hostile_sdk_error(monkeypatch) -> None:
    from mcp import types
    from mcp.shared.exceptions import McpError

    from vibecad import mcp_transport

    server = _server_module()
    ping = mcp_transport.prevalidate_client_message(
        {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}
    )
    monkeypatch.delitem(server._sdk.request_handlers, types.PingRequest)
    assert server._owned_dispatch_descriptor(ping) == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32603, "message": "Internal error"},
    }

    async def hostile_handler(_request):
        raise McpError(
            types.ErrorData(
                code=-32_603,
                message="secret-sdk-message",
                data={"secret": "secret-sdk-data"},
            )
        )

    monkeypatch.setitem(server._sdk.request_handlers, types.PingRequest, hostile_handler)
    response = server._owned_dispatch_descriptor(ping)
    assert response == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32603, "message": "Internal error"},
    }
    assert "secret" not in json.dumps(response)


def test_owned_stdio_starts_workers_before_instant_auto_install_swap(monkeypatch) -> None:
    server = _server_module()
    exits: list[int] = []
    observations: list[tuple[bool, tuple[bool, ...]]] = []
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")
    monkeypatch.setattr(server, "runtime_swappable", lambda: True)
    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(server, "_read_stdio_chunk", lambda _maximum: b"")
    monkeypatch.setattr(server, "_write_stdio_frame", lambda _frame: None)
    monkeypatch.setattr(server.os, "_exit", exits.append)

    def instant_install() -> None:
        runner = server._active_owned_runner
        assert runner is not None
        observations.append(
            (
                runner.lifecycle.accepts_work,
                tuple(worker.is_alive() for worker in runner._workers),
            )
        )
        assert server._try_schedule_swap() is True

    monkeypatch.setattr(server, "_spawn_install", instant_install)
    server._run_owned_stdio(auto_install=True)

    assert observations == [(True, (True, True, True, True))]
    assert exits == [75]
    assert server._active_owned_runner is None


def test_owned_stdio_initializes_candidate_limit_before_first_worker_application(
    monkeypatch,
) -> None:
    from vibecad.execution import revisions

    server = _server_module()
    responses: list[dict[str, object]] = []
    read_index = 0
    main_thread = threading.current_thread()
    application_threads: list[threading.Thread] = []

    monkeypatch.setattr(revisions._CandidateFileLimitRuntime, "_initialized_pid", None)
    monkeypatch.setattr(revisions._CandidateFileLimitRuntime, "_poisoned_pid", None)
    monkeypatch.setattr(revisions._CandidateFileLimitRuntime, "_gate", None)
    monkeypatch.setattr(revisions, "_install_candidate_file_signal_policy", lambda: True)
    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)

    class FakeApplication:
        def close(self) -> None:
            return None

        def __getattr__(self, name: str):
            if name in server._APPLICATION_METHODS:
                return lambda *_args, **_kwargs: _internal_envelope()
            raise AttributeError(name)

    def open_application() -> object:
        application_threads.append(threading.current_thread())
        revisions._initialize_candidate_file_limit_runtime()
        return FakeApplication()

    slot = server._ApplicationSlot(open_application)
    monkeypatch.setattr(server, "_application_slot", slot)

    initialize = {
        "jsonrpc": "2.0",
        "id": "initialize",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    call = {
        "jsonrpc": "2.0",
        "id": "get-project",
        "method": "tools/call",
        "params": {
            "name": "get_project",
            "arguments": {"schema_version": 1, "project_id": PROJECT_ID},
        },
    }

    def wait_until(predicate) -> None:
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, responses
            time.sleep(0.01)

    def read_chunk(_maximum: int) -> bytes:
        nonlocal read_index
        if read_index == 0:
            message = initialize
        elif read_index == 1:
            wait_until(lambda: len(responses) == 1)
            message = initialized
        elif read_index == 2:
            wait_until(
                lambda: (
                    server._active_owned_runner is not None
                    and server._active_owned_runner.handshake_state == "READY"
                )
            )
            message = call
        else:
            wait_until(lambda: len(responses) == 2)
            return b""
        read_index += 1
        return json.dumps(message, separators=(",", ":")).encode() + b"\n"

    def write_frame(frame: bytes) -> None:
        responses.append(json.loads(frame))

    monkeypatch.setattr(server, "_read_stdio_chunk", read_chunk)
    monkeypatch.setattr(server, "_write_stdio_frame", write_frame)

    server._run_owned_stdio()

    assert application_threads and application_threads[0] is not main_thread
    assert revisions._CandidateFileLimitRuntime._initialized_pid == os.getpid()
    assert responses[1]["id"] == "get-project"
    assert "result" in responses[1]
    assert "error" not in responses[1]


def test_owned_stdio_initialization_failure_precedes_worker_and_input_start(monkeypatch) -> None:
    from vibecad.execution import revisions

    server = _server_module()
    reads: list[int] = []

    def fail_initialization() -> None:
        raise RuntimeError("candidate runtime initialization failed")

    def read_chunk(maximum: int) -> bytes:
        reads.append(maximum)
        return b""

    monkeypatch.setattr(
        revisions,
        "_initialize_candidate_file_limit_runtime",
        fail_initialization,
    )
    monkeypatch.setattr(server, "_read_stdio_chunk", read_chunk)

    with pytest.raises(RuntimeError, match="candidate runtime initialization failed"):
        server._run_owned_stdio()

    assert reads == []
    assert server._active_owned_runner is None


def test_real_owned_worker_can_lazy_open_application_after_process_initialization(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "owned-worker-data"
    script = f"""
from pathlib import Path
from vibecad import server
server._application_runtime_guard = lambda: None
server.paths.data_root = lambda: Path({str(data_root)!r})
server.main()
"""
    environment = os.environ.copy()
    environment["VIBECAD_AUTO_INSTALL"] = "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None and process.stdout is not None

    def read_response(timeout: float = 5.0) -> dict[str, object]:
        result: dict[str, bytes] = {}
        reader = threading.Thread(
            target=lambda: result.setdefault("line", process.stdout.readline()),
            daemon=True,
        )
        reader.start()
        reader.join(timeout)
        if "line" not in result:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("owned stdio subprocess did not produce a bounded response")
        return json.loads(result["line"])

    initialize = {
        "jsonrpc": "2.0",
        "id": "initialize",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    process.stdin.write(json.dumps(initialize, separators=(",", ":")).encode() + b"\n")
    process.stdin.flush()
    initialized = read_response()
    assert initialized["result"]["protocolVersion"] == "2025-11-25"

    messages = (
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": "get-project",
            "method": "tools/call",
            "params": {
                "name": "get_project",
                "arguments": {"schema_version": 1, "project_id": PROJECT_ID},
            },
        },
    )
    process.stdin.write(
        b"".join(
            json.dumps(message, separators=(",", ":")).encode() + b"\n" for message in messages
        )
    )
    process.stdin.flush()
    response = read_response()
    process.stdin.close()
    returncode = process.wait(timeout=15)
    stderr = process.stderr.read() if process.stderr is not None else b""

    assert returncode == 0, stderr.decode(errors="replace")
    assert response["id"] == "get-project"
    assert "error" not in response
    assert response["result"]["structuredContent"]["error"]["code"] == "not_found"
    assert data_root.is_dir()


def test_runtime_guard_response_is_flushed_before_swap_and_never_opens_application(
    monkeypatch,
) -> None:
    from vibecad import mcp_transport

    server = _server_module()
    chunks: queue.Queue[bytes] = queue.Queue()
    condition = threading.Condition()
    responses: list[dict[str, object]] = []
    exits: list[tuple[int, int]] = []
    application_calls: list[str] = []

    class ClosedSlot:
        def get(self):
            application_calls.append("get")
            raise AssertionError("guarded resource must not open application")

        def close(self):
            application_calls.append("close")
            return True

    def write(frame: bytes) -> None:
        with condition:
            responses.append(json.loads(frame[:-1]))
            condition.notify_all()

    def send(message: dict[str, object]) -> None:
        chunks.put(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def wait_responses(count: int) -> None:
        deadline = time.monotonic() + 5
        with condition:
            while len(responses) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0, responses
                condition.wait(remaining)

    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(server, "_application_slot", ClosedSlot())
    monkeypatch.setattr(server._installer, "is_ready", lambda: True)
    monkeypatch.setattr(server, "_in_conda_runtime", lambda: False)
    monkeypatch.setattr(server, "_supervised", lambda: True)
    monkeypatch.setattr(server, "runtime_swappable", lambda: True)
    lifecycle = mcp_transport.ProcessLifecycle()
    runner = mcp_transport.OwnedStdioRunner(
        dispatch=server._owned_dispatch_descriptor,
        lifecycle=lifecycle,
        close_application=server._application_slot.close,
        uninstall_recovery_response=server._uninstall_recovery_response,
        exit_process=lambda code: exits.append((code, len(responses))),
        failure_response=server._owned_failure_response,
    )
    monkeypatch.setattr(server, "_active_owned_runner", runner)
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": lambda _maximum: chunks.get(timeout=5), "write_frame": write},
        daemon=True,
    )
    thread.start()
    send(
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    wait_responses(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    deadline = time.monotonic() + 5
    while runner.handshake_state != "READY":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    uri = "vibecad://artifact/materialization_" + "a" * 64 + "/artifact_" + "b" * 32
    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "resources/read",
            "params": {"uri": uri},
        }
    )
    wait_responses(2)
    deadline = time.monotonic() + 5
    while not exits:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    chunks.put(b"")
    thread.join(5)

    assert responses[1] == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32004, "message": "The managed CAD runtime is not active."},
    }
    assert exits == [(75, 2)]
    assert application_calls == []


def test_confirmed_uninstall_close_failure_keeps_marker_and_returns_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    from vibecad import mcp_transport

    server = _server_module()
    marker = tmp_path / ".uninstall_requested"
    chunks: queue.Queue[bytes] = queue.Queue()
    condition = threading.Condition()
    responses: list[dict[str, object]] = []
    exits: list[int] = []
    close_calls: list[str] = []

    class FailingSlot:
        def close(self):
            close_calls.append("close")
            return False

    def write(frame: bytes) -> None:
        with condition:
            responses.append(json.loads(frame[:-1]))
            condition.notify_all()

    def send(message: dict[str, object]) -> None:
        chunks.put(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def wait_responses(count: int) -> None:
        deadline = time.monotonic() + 5
        with condition:
            while len(responses) < count:
                remaining = deadline - time.monotonic()
                assert remaining > 0, responses
                condition.wait(remaining)

    monkeypatch.setattr(server, "_supervised", lambda: True)
    monkeypatch.setattr(
        server._uninstall,
        "preview_uninstall",
        lambda: {"ok": True, "size_mb": 1.0},
    )

    def mark_uninstall():
        marker.touch()
        return {"ok": True, "marked": True}

    monkeypatch.setattr(server._uninstall, "request_uninstall", mark_uninstall)
    lifecycle = mcp_transport.ProcessLifecycle()
    slot = FailingSlot()
    runner = mcp_transport.OwnedStdioRunner(
        dispatch=server._owned_dispatch_descriptor,
        lifecycle=lifecycle,
        close_application=slot.close,
        uninstall_recovery_response=server._uninstall_recovery_response,
        exit_process=exits.append,
        failure_response=server._owned_failure_response,
    )
    monkeypatch.setattr(server, "_active_owned_runner", runner)
    thread = threading.Thread(
        target=runner.run,
        kwargs={"read_chunk": lambda _maximum: chunks.get(timeout=5), "write_frame": write},
        daemon=True,
    )
    thread.start()
    send(
        {
            "jsonrpc": "2.0",
            "id": "initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
    )
    wait_responses(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    deadline = time.monotonic() + 5
    while runner.handshake_state != "READY":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "uninstall_runtime",
                "arguments": {"confirm": True},
            },
        }
    )
    wait_responses(2)
    chunks.put(b"")
    thread.join(5)

    envelope = responses[1]["result"]["structuredContent"]
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "recovery_required"
    assert responses[1]["result"]["isError"] is True
    assert marker.exists()
    assert close_calls == ["close"]
    assert exits == []


def test_real_owned_stdio_initializes_lists_tools_and_never_calls_sdk_stdio() -> None:
    server = _server_module()
    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "mcp.run()" not in source
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    requests = (
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}},
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "vibecad.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(initialize, separators=(",", ":")).encode() + b"\n")
    process.stdin.flush()
    responses = {1: json.loads(process.stdout.readline())}
    process.stdin.write(
        b"".join(
            json.dumps(item, separators=(",", ":")).encode("utf-8") + b"\n" for item in requests
        )
    )
    process.stdin.flush()
    for _index in range(2):
        response = json.loads(process.stdout.readline())
        responses[response["id"]] = response
    process.stdin.close()
    returncode = process.wait(timeout=15)
    stderr = process.stderr.read() if process.stderr is not None else b""

    assert returncode == 0, stderr.decode(errors="replace")
    assert set(responses) == {1, 2, 3}
    assert responses[1]["result"]["protocolVersion"] == "2025-06-18"
    assert [tool["name"] for tool in responses[2]["result"]["tools"]] == [
        item.name for item in _surface_module().public_tool_specs()
    ]
    assert responses[3] == {"jsonrpc": "2.0", "id": 3, "result": {}}


def test_call_result_has_exact_structured_and_canonical_text_envelope() -> None:
    from mcp import types

    server = _server_module()
    result = anyio.run(server._handle_call_tool, "ping", {})

    assert type(result) is types.CallToolResult
    assert result.isError is False
    assert result.structuredContent == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "schema_version": 1,
            "service": "vibecad",
            "version": importlib.import_module("vibecad").__version__,
        },
        "error": None,
    }
    assert result.content == [
        types.TextContent(
            type="text",
            text=json.dumps(
                result.structuredContent,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    ]


def test_internal_output_validation_remains_when_discovery_omits_output_schema(
    monkeypatch,
) -> None:
    from mcp.shared.exceptions import McpError

    server = _server_module()
    monkeypatch.setattr(
        server,
        "ping",
        lambda: {"schema_version": 1, "service": "vibecad"},
    )

    with pytest.raises(McpError) as caught:
        anyio.run(server._handle_call_tool, "ping", {})

    assert (caught.value.error.code, caught.value.error.message) == (
        -32603,
        "Tool request could not be completed.",
    )


def _successful_export_envelope() -> dict[str, object]:
    materialization_id = "materialization_" + "a" * 64
    model_id = "artifact_" + "b" * 32
    step_id = "artifact_" + "c" * 32
    artifacts = [
        {
            "schema_version": 1,
            "id": model_id,
            "name": "model.FCStd",
            "format": "fcstd",
            "sha256": "d" * 64,
            "size_bytes": 1_024,
            "resource_uri": f"vibecad://artifact/{materialization_id}/{model_id}",
        },
        {
            "schema_version": 1,
            "id": step_id,
            "name": "model.step",
            "format": "step",
            "sha256": "e" * 64,
            "size_bytes": 2_048,
            "resource_uri": f"vibecad://artifact/{materialization_id}/{step_id}",
        },
    ]
    return {
        "schema_version": 1,
        "ok": True,
        "result": {
            "schema_version": 1,
            "export_key": "export_0123456789abcdef0123456789abcdef",
            "materialization_id": materialization_id,
            "source_kind": "committed",
            "task_id": TASK_ID,
            "task_generation": 0,
            "project_id": PROJECT_ID,
            "revision_id": BASE_REVISION,
            "manifest_sha256": "f" * 64,
            "authoritative": False,
            "artifacts": artifacts,
        },
        "error": None,
    }


def _export_arguments() -> dict[str, object]:
    return {
        "schema_version": 1,
        "export_key": "export_0123456789abcdef0123456789abcdef",
        "task_id": TASK_ID,
        "expected_generation": 0,
        "revision_id": BASE_REVISION,
        "draft_id": None,
    }


def test_successful_export_returns_text_and_exact_typed_resource_links(monkeypatch) -> None:
    from mcp import types

    server = _server_module()
    envelope = _successful_export_envelope()
    calls: list[dict[str, object]] = []

    class ExportApplication:
        def export_task_artifacts_request(self, arguments):
            calls.append(arguments)
            return envelope

    class ReadySlot:
        def get(self):
            return ExportApplication()

    monkeypatch.setattr(server, "_application_slot", ReadySlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)

    result = anyio.run(server._handle_call_tool, "export_task_artifacts", _export_arguments())

    assert result.isError is False
    assert calls == [_export_arguments()]
    assert len(result.content) == 3
    assert type(result.content[0]) is types.TextContent
    assert result.content[0].text == json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    links = result.content[1:]
    assert all(type(item) is types.ResourceLink for item in links)
    artifacts = envelope["result"]["artifacts"]
    assert [item.model_dump(by_alias=True, exclude_none=True, mode="json") for item in links] == [
        {
            "name": artifacts[0]["name"],
            "uri": artifacts[0]["resource_uri"],
            "mimeType": "application/vnd.freecad.fcstd",
            "size": artifacts[0]["size_bytes"],
            "type": "resource_link",
        },
        {
            "name": artifacts[1]["name"],
            "uri": artifacts[1]["resource_uri"],
            "mimeType": "model/step",
            "size": artifacts[1]["size_bytes"],
            "type": "resource_link",
        },
    ]


def test_resource_links_never_appear_on_failed_export_or_other_tools(monkeypatch) -> None:
    from mcp import types

    server = _server_module()

    class FailingApplication:
        def export_task_artifacts_request(self, _arguments):
            return _internal_envelope()

    class ReadySlot:
        def get(self):
            return FailingApplication()

    monkeypatch.setattr(server, "_application_slot", ReadySlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)

    failed = anyio.run(server._handle_call_tool, "export_task_artifacts", _export_arguments())
    other = anyio.run(server._handle_call_tool, "ping", {})

    assert failed.isError is True
    assert [type(item) for item in failed.content] == [types.TextContent]
    assert other.isError is False
    assert [type(item) for item in other.content] == [types.TextContent]


def test_unknown_and_invalid_tools_are_fixed_and_never_open_application(monkeypatch) -> None:
    from mcp.shared.exceptions import McpError

    server = _server_module()
    application_calls: list[str] = []

    class ClosedSlot:
        def get(self):
            application_calls.append("get")
            raise AssertionError("application must remain unopened")

    monkeypatch.setattr(server, "_application_slot", ClosedSlot())
    with pytest.raises(McpError) as unknown:
        anyio.run(server._handle_call_tool, "secret_unknown_tool", {})
    assert (unknown.value.error.code, unknown.value.error.message) == (
        -32602,
        "Tool name is not available.",
    )

    invalid = anyio.run(
        server._handle_call_tool,
        "create_project",
        {"schema_version": 1, "secret_path": "/private/secret.FCStd"},
    )
    assert invalid.isError is True
    assert invalid.structuredContent["error"]["code"] in {
        "missing_field",
        "unknown_field",
    }
    assert "secret" not in invalid.content[0].text
    assert application_calls == []


def test_runtime_guard_precedes_application_open_for_domain_tools(monkeypatch) -> None:
    server = _server_module()
    application_calls: list[str] = []

    class ClosedSlot:
        def get(self):
            application_calls.append("get")
            raise AssertionError("application must remain unopened")

    monkeypatch.setattr(server, "_application_slot", ClosedSlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: server._runtime_unavailable())
    result = anyio.run(
        server._handle_call_tool,
        "get_project",
        {"schema_version": 1, "project_id": PROJECT_ID},
    )

    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "runtime_unavailable"
    assert application_calls == []


def _model_program_for_server_surface() -> dict[str, object]:
    return {
        "schema_version": 1,
        "task_id": TASK_ID,
        "base_revision": BASE_REVISION,
        "operations": [],
        "acceptance": _acceptance().to_mapping(),
    }


@pytest.mark.parametrize(
    ("tool_name", "facade", "arguments"),
    [
        (
            "create_project",
            "create_project_request",
            {
                "schema_version": 1,
                "create_key": "project_create_0123456789abcdef0123456789abcdef",
                "kind": "empty",
            },
        ),
        ("get_project", "get_project_request", {"schema_version": 1, "project_id": PROJECT_ID}),
        ("list_projects", "list_projects_request", {"schema_version": 1}),
        (
            "list_revisions",
            "list_revisions_request",
            {"schema_version": 1, "project_id": PROJECT_ID},
        ),
        (
            "create_task",
            "create_task_request",
            {
                "schema_version": 1,
                "create_key": "task_create_0123456789abcdef0123456789abcdef",
                "project_id": PROJECT_ID,
                "review_policy": "auto_commit",
            },
        ),
        ("list_tasks", "list_tasks_request", {"schema_version": 1}),
        ("get_task", "get_task_request", {"schema_version": 1, "task_id": TASK_ID}),
        (
            "get_task_events",
            "get_task_events_request",
            {"schema_version": 1, "task_id": TASK_ID},
        ),
        (
            "submit_model_program",
            "submit_model_program_request",
            {
                "schema_version": 1,
                "task_id": TASK_ID,
                "expected_generation": 0,
                "program_json": json.dumps(_model_program_for_server_surface()),
            },
        ),
        (
            "resume_task",
            "resume_task_request",
            {"schema_version": 1, "task_id": TASK_ID, "expected_generation": 0},
        ),
        (
            "accept_draft",
            "accept_draft_request",
            {
                "schema_version": 1,
                "task_id": TASK_ID,
                "draft_id": "draft_0123456789abcdef0123456789abcdef",
                "expected_generation": 0,
            },
        ),
        (
            "reject_draft",
            "reject_draft_request",
            {
                "schema_version": 1,
                "task_id": TASK_ID,
                "draft_id": "draft_0123456789abcdef0123456789abcdef",
                "expected_generation": 0,
            },
        ),
        (
            "export_task_artifacts",
            "export_task_artifacts_request",
            {
                "schema_version": 1,
                "export_key": "export_0123456789abcdef0123456789abcdef",
                "task_id": TASK_ID,
                "expected_generation": 0,
                "revision_id": BASE_REVISION,
                "draft_id": None,
            },
        ),
        ("create_box", "invoke_direct_operation_request", _request()),
        (
            "create_cylinder",
            "invoke_direct_operation_request",
            _request(arguments={"radius_mm": 4, "height_mm": 8}),
        ),
        ("inspect_model", "invoke_direct_operation_request", _request(arguments={})),
        (
            "modify_parameter",
            "invoke_direct_operation_request",
            _request(
                target={"object": _selector()},
                arguments={"parameter": "height", "value_mm": 8},
            ),
        ),
        (
            "move_part",
            "invoke_direct_operation_request",
            _request(
                target={"object": _selector()},
                arguments={"position_mm": [1, 2, 3]},
            ),
        ),
        (
            "rotate_part",
            "invoke_direct_operation_request",
            _request(
                target={"object": _selector()},
                arguments={"axis": "z", "angle_deg": 90},
            ),
        ),
    ],
)
def test_each_domain_tool_invokes_only_its_application_facade_once(
    monkeypatch,
    tool_name: str,
    facade: str,
    arguments: dict[str, object],
) -> None:
    server = _server_module()
    calls: list[tuple[str, tuple[object, ...]]] = []

    class FakeApplication:
        def __getattr__(self, name: str):
            def invoke(*values):
                calls.append((name, values))
                return _internal_envelope()

            return invoke

    class ReadySlot:
        def get(self):
            return FakeApplication()

    monkeypatch.setattr(server, "_application_slot", ReadySlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)

    result = anyio.run(server._handle_call_tool, tool_name, arguments)

    assert result.isError is True
    assert [name for name, _ in calls] == [facade]
    if tool_name in {
        "create_box",
        "create_cylinder",
        "inspect_model",
        "modify_parameter",
        "move_part",
        "rotate_part",
    }:
        assert calls[0][1] == (tool_name, arguments)
    else:
        assert calls[0][1] == (arguments,)


def test_application_slot_is_single_flight_and_closes_exact_instance_once() -> None:
    server = _server_module()
    entered = threading.Event()
    release = threading.Event()
    opens: list[object] = []

    class FakeApplication:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

        def __getattr__(self, name: str):
            if name in server._APPLICATION_METHODS:
                return lambda *_args, **_kwargs: _internal_envelope()
            raise AttributeError(name)

    application = FakeApplication()

    def open_application():
        opens.append(object())
        entered.set()
        assert release.wait(timeout=3)
        return application

    slot = server._ApplicationSlot(open_application)
    results: list[object] = []
    workers = [threading.Thread(target=lambda: results.append(slot.get())) for _ in range(4)]
    for worker in workers:
        worker.start()
    assert entered.wait(timeout=2)
    release.set()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert opens == [opens[0]]
    assert results == [application] * 4
    assert slot.state == "READY"
    slot.close()
    slot.close()
    assert application.close_calls == 1
    assert slot.state == "CLOSED"
    with pytest.raises(RuntimeError):
        slot.get()


def test_application_slot_open_failure_resets_and_invalid_candidate_is_closed() -> None:
    server = _server_module()
    attempts = 0

    class InvalidApplication:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    invalid = InvalidApplication()

    def open_application():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("secret open failure")
        return invalid

    slot = server._ApplicationSlot(open_application)
    with pytest.raises(RuntimeError, match="application open failed"):
        slot.get()
    assert slot.state == "UNOPENED"
    with pytest.raises(RuntimeError, match="application open failed"):
        slot.get()
    assert invalid.close_calls == 1
    assert slot.state == "UNOPENED"


def test_application_slot_invalid_candidate_close_failure_is_fail_closed() -> None:
    server = _server_module()
    opens = 0

    class UnclosableApplication:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secret close failure")

    invalid = UnclosableApplication()

    def open_application():
        nonlocal opens
        opens += 1
        return invalid

    slot = server._ApplicationSlot(open_application)
    with pytest.raises(RuntimeError, match="application open failed"):
        slot.get()

    assert invalid.close_calls == 1
    assert slot.state == "CLOSED"
    assert slot.close() is False
    with pytest.raises(RuntimeError, match="application slot is closed"):
        slot.get()
    assert opens == 1


def test_application_slot_pid_mismatch_fails_before_lock_or_open(monkeypatch) -> None:
    server = _server_module()
    opens: list[str] = []
    slot = server._ApplicationSlot(lambda: opens.append("open"))
    monkeypatch.setattr(slot, "_pid", os.getpid() + 1)

    with pytest.raises(RuntimeError, match="unavailable in this process"):
        slot.get()

    assert slot.state == "CLOSED"
    assert opens == []


def test_schema_pattern_and_utf8_budgets_fail_before_application_open(monkeypatch) -> None:
    server = _server_module()
    application_calls: list[str] = []

    class ClosedSlot:
        def get(self):
            application_calls.append("get")
            raise AssertionError("application must remain unopened")

    monkeypatch.setattr(server, "_application_slot", ClosedSlot())
    newline_id = anyio.run(
        server._handle_call_tool,
        "get_project",
        {"schema_version": 1, "project_id": PROJECT_ID + "\n"},
    )
    multibyte_path = anyio.run(
        server._handle_call_tool,
        "create_project",
        {
            "schema_version": 1,
            "create_key": "project_create_0123456789abcdef0123456789abcdef",
            "kind": "import_fcstd",
            "source_path": "/" + "界" * 2_000,
        },
    )

    assert newline_id.structuredContent["error"]["code"] == "invalid_value"
    assert multibyte_path.structuredContent["error"] == {
        "schema_version": 1,
        "code": "budget_exceeded",
        "path": "/source_path",
        "message": "The request exceeds a resource budget.",
    }
    assert application_calls == []


def test_get_capabilities_is_pure_and_does_not_open_application(tmp_path: Path) -> None:
    home = tmp_path / "capabilities-does-not-create"
    script = f"""
import anyio
import os
import sys
os.environ['VIBECAD_HOME'] = {str(home)!r}
import vibecad.server as server
result = anyio.run(server._handle_call_tool, 'get_capabilities', {{'schema_version': 1}})
assert result.isError is False
assert len(result.structuredContent['result']['operations']) == 6
assert 'vibecad.application.agent' not in sys.modules
assert 'vibecad.interaction.cad' not in sys.modules
assert 'FreeCAD' not in sys.modules and 'Part' not in sys.modules
assert not os.path.exists({str(home)!r})
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_sdk_tool_projection_is_deeply_serializable() -> None:
    server = _server_module()

    for tool in anyio.run(server._handle_list_tools).tools:
        serialized = tool.model_dump_json()
        assert json.loads(serialized)["name"] == tool.name


def test_unexpected_handler_and_output_validator_failures_are_fixed(monkeypatch) -> None:
    from mcp.shared.exceptions import McpError

    server = _server_module()
    calls: list[str] = []

    class ExplodingApplication:
        def get_project_request(self, _request):
            calls.append("get_project_request")
            raise RuntimeError("secret handler path /private/model.FCStd")

    class ReadySlot:
        def get(self):
            return ExplodingApplication()

    monkeypatch.setattr(server, "_application_slot", ReadySlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    with pytest.raises(McpError) as handler_error:
        anyio.run(
            server._handle_call_tool,
            "get_project",
            {"schema_version": 1, "project_id": PROJECT_ID},
        )
    assert (handler_error.value.error.code, handler_error.value.error.message) == (
        -32603,
        "Tool request could not be completed.",
    )
    assert "secret" not in str(handler_error.value)
    assert calls == ["get_project_request"]

    class ExplodingValidator:
        def is_valid(self, _value):
            raise RuntimeError("secret validator value")

    monkeypatch.setitem(server._OUTPUT_VALIDATORS, "ping", ExplodingValidator())
    with pytest.raises(McpError) as validator_error:
        anyio.run(server._handle_call_tool, "ping", {})
    assert (validator_error.value.error.code, validator_error.value.error.message) == (
        -32603,
        "Tool request could not be completed.",
    )
    assert "secret" not in str(validator_error.value)


def test_low_level_resource_handlers_return_exact_sdk_blob_and_use_app_once(monkeypatch) -> None:
    from mcp import types

    server = _server_module()
    uri = "vibecad://artifact/materialization_" + "1" * 64 + "/artifact_" + "2" * 32
    calls: list[str] = []

    class Content:
        def __init__(self) -> None:
            self.uri = uri
            self.blob = "YWJj"
            self.mime_type = "model/step"

    class FakeApplication:
        def read_artifact_resource(self, value):
            calls.append(value)
            return Content()

    class ReadySlot:
        def get(self):
            return FakeApplication()

    monkeypatch.setattr(server, "_application_slot", ReadySlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    templates = anyio.run(server._handle_list_resource_templates)
    result = anyio.run(server._handle_read_resource, uri)

    assert type(templates) is types.ListResourceTemplatesResult
    assert [item.uriTemplate for item in templates.resourceTemplates] == [
        "vibecad://artifact/{materialization_id}/{artifact_id}"
    ]
    assert type(result) is types.ReadResourceResult
    assert result.contents == [
        types.BlobResourceContents(uri=uri, blob="YWJj", mimeType="model/step")
    ]
    assert calls == [uri]


def test_typed_non_artifact_resource_uri_gets_fixed_owned_grammar_error(monkeypatch) -> None:
    from mcp.shared.exceptions import McpError

    server = _server_module()
    application_calls: list[str] = []

    class ClosedSlot:
        def get(self):
            application_calls.append("get")
            raise AssertionError("application must remain unopened")

    monkeypatch.setattr(server, "_application_slot", ClosedSlot())
    with pytest.raises(McpError) as caught:
        anyio.run(server._handle_read_resource, "file:///secret/path")

    assert (caught.value.error.code, caught.value.error.message) == (
        -32602,
        "Artifact resource identifier is invalid.",
    )
    assert application_calls == []
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("code_name", "expected"),
    [
        ("INVALID_IDENTIFIER", (-32602, "Artifact resource identifier is invalid.")),
        ("UNAVAILABLE", (-32002, "Artifact resource is unavailable.")),
        ("READ_LIMIT", (-32001, "Artifact resource exceeds the read limit.")),
        ("RUNTIME_UNAVAILABLE", (-32004, "The managed CAD runtime is not active.")),
        ("INTERNAL_ERROR", (-32603, "Artifact resource could not be read.")),
    ],
)
def test_artifact_resource_errors_have_complete_fixed_mapping(
    monkeypatch,
    code_name: str,
    expected: tuple[int, str],
) -> None:
    from mcp.shared.exceptions import McpError

    from vibecad.application.artifacts import ArtifactResourceError, ArtifactResourceErrorCode

    server = _server_module()
    uri = "vibecad://artifact/materialization_" + "1" * 64 + "/artifact_" + "2" * 32
    calls: list[str] = []

    class FailingApplication:
        def read_artifact_resource(self, value):
            calls.append(value)
            raise ArtifactResourceError(getattr(ArtifactResourceErrorCode, code_name))

    class ReadySlot:
        def get(self):
            return FailingApplication()

    monkeypatch.setattr(server, "_application_slot", ReadySlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    with pytest.raises(McpError) as caught:
        anyio.run(server._handle_read_resource, uri)

    assert (caught.value.error.code, caught.value.error.message) == expected
    assert calls == [uri]


def test_artifact_error_type_import_failure_is_fixed_and_precedes_application(
    monkeypatch,
) -> None:
    from mcp.shared.exceptions import McpError

    server = _server_module()
    uri = "vibecad://artifact/materialization_" + "1" * 64 + "/artifact_" + "2" * 32
    application_calls: list[str] = []

    class ClosedSlot:
        def get(self):
            application_calls.append("get")
            raise AssertionError("application must remain unopened")

    def fail_import(_name: str):
        raise ImportError("secret import path /private/artifacts.py")

    monkeypatch.setattr(server, "_application_slot", ClosedSlot())
    monkeypatch.setattr(server, "_application_runtime_guard", lambda: None)
    monkeypatch.setattr(server.importlib, "import_module", fail_import)
    with pytest.raises(McpError) as caught:
        anyio.run(server._handle_read_resource, uri)

    assert (caught.value.error.code, caught.value.error.message) == (
        -32603,
        "Artifact resource could not be read.",
    )
    assert "secret" not in str(caught.value)
    assert application_calls == []


def test_real_sdk_low_level_handlers_are_registered_and_return_server_results() -> None:
    from mcp import types

    server = _server_module()
    sdk = server.mcp._mcp_server
    list_handler = sdk.request_handlers[types.ListToolsRequest]
    call_handler = sdk.request_handlers[types.CallToolRequest]

    listed = anyio.run(list_handler, types.ListToolsRequest())
    called = anyio.run(
        call_handler,
        types.CallToolRequest(params=types.CallToolRequestParams(name="ping", arguments={})),
    )

    assert type(listed) is types.ServerResult
    assert type(listed.root) is types.ListToolsResult
    assert type(called) is types.ServerResult
    assert type(called.root) is types.CallToolResult
