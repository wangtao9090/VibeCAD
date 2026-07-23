"""Execution-boundary tests for authentic validated model programs."""

from __future__ import annotations

import ast
import json
import os
import pickle
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import MappingProxyType

import pytest

import vibecad.execution.adapter as adapter_module
from vibecad.execution.adapter import (
    AdapterError,
    AdapterErrorCode,
)
from vibecad.execution.adapter import (
    execute_validated_program as _execute_validated_program,
)
from vibecad.execution.registry import (
    ExecutionProfile,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    ResourceBudget,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
)
from vibecad.execution.results import ToolDiagnosticClass, ToolResultCode
from vibecad.execution.selectors import SelectorV1
from vibecad.workflow.contracts import (
    AcceptanceSpec,
    EvidenceKind,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.program import (
    BoundResultRef,
    ValidatedProgram,
    validate_model_program,
)


class _Clock:
    def __init__(self, *values: int | BaseException) -> None:
        self.values = values
        self.calls = 0

    def __call__(self) -> int:
        if self.calls >= len(self.values):
            raise AssertionError("unexpected monotonic-clock read")
        value = self.values[self.calls]
        self.calls += 1
        if isinstance(value, BaseException):
            raise value
        return value


class _HostileHandlers(Mapping[str, object]):
    def __init__(self) -> None:
        self.accesses = 0

    def __getitem__(self, key: str) -> object:
        del key
        self.accesses += 1
        raise RuntimeError("private handler-mapping detail")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("handler mapping must not be iterated")

    def __len__(self) -> int:
        return 0


class _TrackingHandlers(dict[str, object]):
    def __init__(self, values: Mapping[str, object]) -> None:
        super().__init__(values)
        self.accesses: list[str] = []

    def __getitem__(self, key: str) -> object:
        self.accesses.append(key)
        return super().__getitem__(key)


class _McpLikeAttachment:
    def __repr__(self) -> str:
        return "private-mcp-attachment"


def _command(
    command_id: str,
    op: str = "inspect_model",
    *,
    args: Mapping[str, object] | None = None,
    target: Mapping[str, object] | None = None,
    depends_on: tuple[str, ...] = (),
) -> ModelCommand:
    return ModelCommand(
        id=command_id,
        op=op,
        target={} if target is None else target,
        args={} if args is None else args,
        depends_on=depends_on,
        preserve=(),
        source=ValueSource.MODEL,
    )


def _model_program(*commands: ModelCommand) -> ModelProgram:
    return ModelProgram(
        task_id="task-adapter",
        base_revision="untrusted-program-base",
        operations=commands,
        acceptance=AcceptanceSpec(id="acceptance-adapter", criteria=()),
    )


def _validated(*commands: ModelCommand) -> ValidatedProgram:
    return validate_model_program(_model_program(*commands))


def execute_validated_program(
    program: object,
    handlers: object,
    **kwargs: object,
):
    """Run legacy adapter-focused assertions through the explicit headless profile."""

    kwargs.setdefault("execution_profile", ExecutionProfile.HEADLESS)
    return _execute_validated_program(program, handlers, **kwargs)  # type: ignore[arg-type]


def _inspect_program(count: int = 1) -> ValidatedProgram:
    return _validated(*(_command(f"inspect-{index}") for index in range(count)))


def _three_step_program() -> ValidatedProgram:
    return _validated(
        _command(
            "box",
            "create_box",
            args={
                "length_mm": 10,
                "width_mm": 20,
                "height_mm": 30,
                "position_mm": [1, 2, 3],
            },
        ),
        _command(
            "modify",
            "modify_parameter",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"parameter": "length", "value_mm": 12},
            depends_on=("box",),
        ),
        _command("inspect", depends_on=("modify",)),
    )


def _selector_mapping(revision_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "project_11111111111111111111111111111111",
        "revision_id": revision_id,
        "entity_kind": "feature",
        "object_id": "object_22222222222222222222222222222222",
        "feature_id": "feature_33333333333333333333333333333333",
        "object_type": "Part::Box",
        "semantic_role": "primitive",
        "provenance": {"source": "model", "operation_id": "box"},
        "expected_cardinality": 1,
    }


def _selector_program() -> ValidatedProgram:
    revision = "revision_44444444444444444444444444444444"
    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="select_object",
                handler_name="select_object",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=False,
                target_fields=(FieldMetadata("object", "selector", ValueShape.OBJECT_SELECTOR),),
            ),
        )
    )
    return validate_model_program(
        ModelProgram(
            task_id="task-selector-adapter",
            base_revision=revision,
            operations=(
                _command(
                    "select",
                    "select_object",
                    target={"object": _selector_mapping(revision)},
                ),
            ),
            acceptance=AcceptanceSpec(id="acceptance-selector-adapter", criteria=()),
        ),
        registry=registry,
    )


def _install_clock(
    monkeypatch: pytest.MonkeyPatch,
    *values: int | BaseException,
) -> _Clock:
    clock = _Clock(*values)
    monkeypatch.setattr(adapter_module, "_monotonic_ns", clock)
    return clock


@pytest.fixture(scope="session")
def existing_freecad_python() -> str:
    """Return an explicitly selected ready runtime without installing anything."""

    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    prefix_value = os.environ.get("VIBECAD_FREECAD_ENV")
    if not prefix_value:
        pytest.fail("set VIBECAD_FREECAD_ENV to an existing ready FreeCAD environment")
    prefix = Path(prefix_value).expanduser()
    python = prefix / "bin" / "python"
    sentinel = prefix / ".vibecad_ready"
    if not python.is_file():
        pytest.fail("VIBECAD_FREECAD_ENV does not contain bin/python")
    if not sentinel.is_file():
        pytest.fail("VIBECAD_FREECAD_ENV does not contain the ready sentinel")
    return str(python)


def _assert_adapter_error(
    code: AdapterErrorCode,
    program: object,
    handlers: object,
    *,
    revision: object = None,
) -> AdapterError:
    with pytest.raises(AdapterError) as caught:
        execute_validated_program(  # type: ignore[arg-type]
            program,
            handlers,
            revision=revision,  # type: ignore[arg-type]
        )
    assert caught.value.code is code
    return caught.value


def test_adapter_error_codes_and_records_are_fixed() -> None:
    assert {item.value for item in AdapterErrorCode} == {
        "invalid_program",
        "invalid_handlers",
        "invalid_revision",
        "missing_handler",
        "non_callable_handler",
        "invalid_execution_profile",
        "unsupported_execution_profile",
    }

    for code in AdapterErrorCode:
        error = AdapterError(code)
        mapping = error.to_mapping()
        assert mapping == {
            "schema_version": SCHEMA_VERSION,
            "code": code.value,
            "message": error.message,
        }
        json.dumps(mapping)
        assert "private" not in str(error)

    with pytest.raises(TypeError, match="code must be an AdapterErrorCode"):
        AdapterError("invalid_program")  # type: ignore[arg-type]


@pytest.mark.parametrize("program", [object(), _model_program(_command("inspect"))])
def test_unvalidated_program_is_rejected_before_mapping_or_clock(
    program: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    error = _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, program, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0
    assert "private" not in str(error)


def test_unsealed_exact_validated_program_is_rejected_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forged = object.__new__(ValidatedProgram)
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, forged, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


@pytest.mark.parametrize("case", ["commands", "program", "command"])
def test_privately_forged_validated_program_structure_is_rejected(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated = _inspect_program()
    if case == "commands":
        object.__setattr__(validated, "_commands", list(validated.commands))
    elif case == "program":
        object.__setattr__(validated, "_program", object())
    else:
        object.__setattr__(validated.commands[0], "handler_kwargs", {})
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, validated, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("id", "", id="blank-id"),
        pytest.param("operation", 7, id="non-string-operation"),
        pytest.param("handler_name", " ", id="blank-handler-name"),
        pytest.param("handler_kwargs", {}, id="mutable-handler-kwargs"),
        pytest.param(
            "handler_kwargs",
            MappingProxyType({"private": object()}),
            id="non-json-handler-value",
        ),
        pytest.param("depends_on", [], id="mutable-dependencies"),
        pytest.param("preserve", [], id="mutable-preservation"),
        pytest.param("source", "model", id="untyped-source"),
        pytest.param("risk_class", "read_only", id="untyped-risk"),
        pytest.param("evidence_required", 1, id="non-boolean-evidence-flag"),
        pytest.param("execution_profiles", [], id="mutable-execution-profiles"),
        pytest.param("resource_budget", object(), id="untyped-resource-budget"),
        pytest.param("minimum_freecad_version", [1, 0], id="mutable-minimum-version"),
        pytest.param(
            "maximum_freecad_version_exclusive",
            [2, 0],
            id="mutable-maximum-version",
        ),
        pytest.param("requires_gui_main_thread", 1, id="untyped-gui-thread-flag"),
        pytest.param("result_slots", [], id="mutable-result-slots"),
    ],
)
def test_forged_bound_command_critical_fields_are_rejected_before_preflight(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated = _inspect_program()
    object.__setattr__(validated.commands[0], field, value)
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, validated, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


def test_forged_nested_result_slot_is_rejected_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _validated(
        _command(
            "box",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        )
    )
    original = program.commands[0].result_slots[0]
    slot = ResultSlotMetadata(original.name, original.result_field, original.value_shape)
    object.__setattr__(slot, "result_field", ["name"])
    object.__setattr__(program.commands[0], "result_slots", (slot,))
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, program, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


def test_forged_result_ref_cannot_escape_dependency_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _three_step_program()
    reference = program.commands[1].handler_kwargs["target"]
    object.__setattr__(reference, "command_id", "inspect")
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, program, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


def test_well_typed_result_contract_forgery_is_rejected_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _three_step_program()
    forged_slot = ResultSlotMetadata(
        "object",
        "volume",
        ValueShape.FINITE_NUMBER,
    )
    forged_ref = BoundResultRef("box", "object", ValueShape.FINITE_NUMBER)
    object.__setattr__(program.commands[0], "result_slots", (forged_slot,))
    object.__setattr__(
        program.commands[1],
        "handler_kwargs",
        MappingProxyType(
            {
                "target": forged_ref,
                "parameter": "length",
                "value": 12,
            }
        ),
    )
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, program, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


def test_well_typed_execution_profile_forgery_is_rejected_before_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _inspect_program()
    object.__setattr__(
        program.commands[0],
        "execution_profiles",
        (ExecutionProfile.INTERACTIVE_GUI,),
    )
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    with pytest.raises(AdapterError) as caught:
        _execute_validated_program(
            program,
            handlers,
            execution_profile=ExecutionProfile.INTERACTIVE_GUI,
        )

    assert caught.value.code is AdapterErrorCode.INVALID_PROGRAM
    assert handlers.accesses == 0
    assert clock.calls == 0


def test_typed_selector_survives_canonical_rebind_and_reaches_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _selector_program()
    received: list[SelectorV1] = []
    _install_clock(monkeypatch, 0, 1)

    def select_object(*, selector: SelectorV1) -> dict[str, object]:
        received.append(selector)
        return {"ok": True}

    outcomes = execute_validated_program(program, {"select_object": select_object})

    assert outcomes[0].result.ok is True
    assert len(received) == 1
    assert type(received[0]) is SelectorV1
    assert received[0].object_id == "object_22222222222222222222222222222222"


def test_well_typed_selector_forgery_is_rejected_before_handler_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _selector_program()
    selector = program.commands[0].handler_kwargs["selector"]
    object.__setattr__(
        selector,
        "object_id",
        "object_55555555555555555555555555555555",
    )
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, program, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


def test_cross_type_scalar_equality_cannot_bypass_canonical_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _validated(
        _command(
            "box",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        )
    )
    forged_kwargs = dict(program.commands[0].handler_kwargs)
    forged_kwargs["length"] = True
    object.__setattr__(
        program.commands[0],
        "handler_kwargs",
        MappingProxyType(forged_kwargs),
    )
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    _assert_adapter_error(AdapterErrorCode.INVALID_PROGRAM, program, handlers)

    assert handlers.accesses == 0
    assert clock.calls == 0


def test_program_validated_by_custom_registry_keeps_its_execution_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="custom_inspect",
                handler_name="custom_handler",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=False,
            ),
        )
    )
    program = validate_model_program(
        _model_program(_command("custom", "custom_inspect")),
        registry=registry,
    )
    calls = 0
    _install_clock(monkeypatch, 0, 1)

    def custom_handler() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True, "source": "custom"}

    outcomes = _execute_validated_program(
        program,
        {"custom_handler": custom_handler},
        execution_profile=ExecutionProfile.HEADLESS,
    )

    assert calls == 1
    assert len(outcomes) == 1
    assert outcomes[0].result.ok is True


@pytest.mark.parametrize("revision", ["", "   ", 7, True, object()])
def test_invalid_revision_is_rejected_before_handler_mapping(
    revision: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    error = _assert_adapter_error(
        AdapterErrorCode.INVALID_REVISION,
        _inspect_program(),
        handlers,
        revision=revision,
    )

    assert handlers.accesses == 0
    assert clock.calls == 0
    assert repr(revision) not in str(error)


@pytest.mark.parametrize("handlers", [None, object(), [], (), "inspect_model"])
def test_non_mapping_handlers_are_fixed_errors_before_clock(
    handlers: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _install_clock(monkeypatch)

    error = _assert_adapter_error(
        AdapterErrorCode.INVALID_HANDLERS,
        _inspect_program(),
        handlers,
    )

    assert clock.calls == 0
    assert "inspect_model" not in str(error)


@pytest.mark.parametrize(
    ("handlers", "code"),
    [
        ({"inspect_model": lambda: {}}, AdapterErrorCode.MISSING_HANDLER),
        ({"create_box": lambda **kwargs: kwargs}, AdapterErrorCode.MISSING_HANDLER),
        (
            {"create_box": object(), "inspect_model": lambda: {}},
            AdapterErrorCode.NON_CALLABLE_HANDLER,
        ),
        (
            {"create_box": lambda **kwargs: kwargs, "inspect_model": object()},
            AdapterErrorCode.NON_CALLABLE_HANDLER,
        ),
    ],
)
def test_first_and_final_handler_configuration_is_preflighted_before_execution(
    handlers: Mapping[str, object],
    code: AdapterErrorCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    supplied = dict(handlers)
    if callable(supplied.get("create_box")):
        supplied["create_box"] = lambda **kwargs: calls.append("box") or kwargs
    if callable(supplied.get("inspect_model")):
        supplied["inspect_model"] = lambda: calls.append("inspect") or {}
    clock = _install_clock(monkeypatch)
    program = _validated(
        _command(
            "box",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        ),
        _command("inspect", depends_on=("box",)),
    )

    _assert_adapter_error(code, program, supplied)

    assert calls == []
    assert clock.calls == 0


def test_hostile_handler_mapping_yields_fixed_error_without_clock_or_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers = _HostileHandlers()
    clock = _install_clock(monkeypatch)

    error = _assert_adapter_error(
        AdapterErrorCode.INVALID_HANDLERS,
        _inspect_program(),
        handlers,
    )

    assert handlers.accesses == 1
    assert clock.calls == 0
    assert "private handler-mapping detail" not in str(error)


def test_handler_mapping_is_snapshotted_once_before_any_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    handlers: _TrackingHandlers

    def box(**kwargs: object) -> dict[str, object]:
        calls.append(f"box:{kwargs['length']}")
        handlers["inspect_model"] = lambda: calls.append("replacement") or {"valid": False}
        return {
            "ok": True,
            "object_id": "object_22222222222222222222222222222222",
        }

    def inspect() -> dict[str, object]:
        calls.append("captured-inspect")
        return {"valid": True}

    handlers = _TrackingHandlers({"create_box": box, "inspect_model": inspect})
    _install_clock(monkeypatch, 0, 1_000_000, 2_000_000, 3_000_000)
    program = _validated(
        _command(
            "box",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        ),
        _command("inspect", depends_on=("box",)),
    )

    outcomes = execute_validated_program(program, handlers)

    assert [item.result.ok for item in outcomes] == [True, True]
    assert calls == ["box:1", "captured-inspect"]
    assert handlers.accesses == ["create_box", "inspect_model"]


def test_repeated_handler_name_is_resolved_once_but_each_command_runs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def inspect() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"valid": True, "call": calls}

    handlers = _TrackingHandlers({"inspect_model": inspect})
    _install_clock(monkeypatch, 0, 1, 2, 3)

    outcomes = execute_validated_program(_inspect_program(2), handlers)

    assert len(outcomes) == 2
    assert calls == 2
    assert handlers.accesses == ["inspect_model"]


def test_bound_command_fields_are_snapshotted_before_handlers_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _validated(
        _command(
            "box",
            "create_box",
            args={"length_mm": 10, "width_mm": 20, "height_mm": 30},
        ),
        _command("inspect", depends_on=("box",)),
    )
    inspect_command = program.commands[1]
    seen: list[dict[str, object]] = []

    def box(**kwargs: object) -> dict[str, object]:
        replacements = {
            "id": "replacement-id",
            "operation": "create_box",
            "handler_name": "replacement",
            "risk_class": RiskClass.MUTATING,
            "evidence_required": True,
            "handler_kwargs": MappingProxyType({"forged": 99}),
        }
        for field, value in replacements.items():
            object.__setattr__(inspect_command, field, value)
        return {
            "ok": True,
            "object_id": "object_22222222222222222222222222222222",
        }

    def inspect(**kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return {"ok": True, "valid": True}

    _install_clock(monkeypatch, 0, 1, 2, 3)

    outcomes = execute_validated_program(
        program,
        {"create_box": box, "inspect_model": inspect},
    )

    assert len(outcomes) == 2
    assert seen == [{}]
    inspect_result = outcomes[1].result
    assert inspect_result.operation_id == "inspect"
    assert dict(inspect_result.facts) == {
        "operation": "inspect_model",
        "handler_name": "inspect_model",
        "risk_class": "read_only",
        "execution_profile": "headless",
    }
    assert inspect_result.evidence == ()


def test_order_kwargs_clock_trusted_facts_and_adapter_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def box(**kwargs: object) -> dict[str, object]:
        calls.append(("box", kwargs))
        return {
            "ok": True,
            "object_id": "object_22222222222222222222222222222222",
            "volume": 6000,
        }

    def modify(**kwargs: object) -> dict[str, object]:
        calls.append(("modify", kwargs))
        return {"ok": True, "object_id": kwargs["target"]}

    def inspect(**kwargs: object) -> dict[str, object]:
        calls.append(("inspect", kwargs))
        return {"valid": True, "solid_count": 1}

    clock = _install_clock(
        monkeypatch,
        0,
        1_000_000,
        2_000_000,
        4_000_000,
        5_000_000,
        8_000_000,
    )

    outcomes = execute_validated_program(
        _three_step_program(),
        {
            "create_box": box,
            "modify_parameter": modify,
            "inspect_model": inspect,
        },
        revision="candidate-r1",
    )

    assert isinstance(outcomes, tuple)
    assert calls == [
        (
            "box",
            {"length": 10, "width": 20, "height": 30, "position": (1, 2, 3)},
        ),
        (
            "modify",
            {
                "target": "object_22222222222222222222222222222222",
                "parameter": "length",
                "value": 12,
            },
        ),
        ("inspect", {}),
    ]
    assert clock.calls == 6
    assert [item.result.elapsed_ms for item in outcomes] == [1, 2, 3]
    assert [item.result.operation_id for item in outcomes] == ["box", "modify", "inspect"]
    assert all(item.result.revision == "candidate-r1" for item in outcomes)
    assert [dict(item.result.facts) for item in outcomes] == [
        {
            "operation": "create_box",
            "handler_name": "create_box",
            "risk_class": "mutating",
            "execution_profile": "headless",
        },
        {
            "operation": "modify_parameter",
            "handler_name": "modify_parameter",
            "risk_class": "mutating",
            "execution_profile": "headless",
        },
        {
            "operation": "inspect_model",
            "handler_name": "inspect_model",
            "risk_class": "read_only",
            "execution_profile": "headless",
        },
    ]
    assert [len(item.result.evidence) for item in outcomes] == [1, 1, 0]
    for outcome, command_id in zip(outcomes[:2], ("box", "modify"), strict=True):
        evidence = outcome.result.evidence[0]
        assert evidence.id == f"{command_id}:execution"
        assert evidence.kind is EvidenceKind.OBSERVATION
        assert evidence.name == "execution_acknowledged"
        assert evidence.operation_id == command_id
        assert evidence.value == {"result_ok": True}


@pytest.mark.parametrize(
    ("profile", "code"),
    [
        ("headless", AdapterErrorCode.INVALID_EXECUTION_PROFILE),
        (ExecutionProfile.INTERACTIVE_GUI, AdapterErrorCode.UNSUPPORTED_EXECUTION_PROFILE),
    ],
)
def test_execution_profile_is_explicit_and_preflighted_before_handlers_or_clock(
    profile: object,
    code: AdapterErrorCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers = _TrackingHandlers({"inspect_model": lambda: {"valid": True}})
    clock = _install_clock(monkeypatch)

    with pytest.raises(AdapterError) as caught:
        _execute_validated_program(
            _inspect_program(),
            handlers,
            execution_profile=profile,  # type: ignore[arg-type]
        )

    assert caught.value.code is code
    assert handlers.accesses == []
    assert clock.calls == 0


def test_typed_result_ref_resolves_normalized_slot_before_consumer_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[dict[str, object]] = []
    _install_clock(monkeypatch, 0, 1, 2, 3, 4, 5)

    def modify(**kwargs: object) -> dict[str, object]:
        received.append(kwargs)
        return {"ok": True}

    outcomes = _execute_validated_program(
        _three_step_program(),
        {
            "create_box": lambda **kwargs: {
                "ok": True,
                "object_id": "object_22222222222222222222222222222222",
            },
            "modify_parameter": modify,
            "inspect_model": lambda: {"valid": True},
        },
        execution_profile=ExecutionProfile.HEADLESS,
    )

    assert [item.result.ok for item in outcomes] == [True, True, True]
    assert received == [
        {
            "target": "object_22222222222222222222222222222222",
            "parameter": "length",
            "value": 12,
        }
    ]


def test_private_execution_cursor_preserves_result_refs_and_one_step_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[dict[str, object]] = []
    _install_clock(monkeypatch, 0, 1, 2, 3, 4, 5)

    execution = adapter_module._prepare_validated_program_execution(
        _three_step_program(),
        {
            "create_box": lambda **kwargs: {
                "ok": True,
                "object_id": "object_22222222222222222222222222222222",
            },
            "modify_parameter": lambda **kwargs: received.append(kwargs) or {"ok": True},
            "inspect_model": lambda: {"valid": True},
        },
        execution_profile=ExecutionProfile.HEADLESS,
    )

    assert execution.done is False
    assert execution.next_runtime_ms == 30_000
    assert execution.outcomes == ()
    assert execution.step().result.operation_id == "box"
    assert execution.done is False
    assert execution.next_runtime_ms == 30_000
    assert execution.step().result.operation_id == "modify"
    assert execution.done is False
    assert execution.next_runtime_ms == 10_000
    assert execution.step().result.operation_id == "inspect"
    assert execution.done is True
    assert execution.next_runtime_ms is None
    assert [item.result.ok for item in execution.outcomes] == [True, True, True]
    assert received == [
        {
            "target": "object_22222222222222222222222222222222",
            "parameter": "length",
            "value": 12,
        }
    ]
    with pytest.raises(TypeError):
        pickle.dumps(execution)
    with pytest.raises(AdapterError):
        execution.step()


@pytest.mark.parametrize(
    "raw",
    [
        {"ok": True},
        {"ok": True, "object_id": ""},
        {"ok": True, "object_id": 7},
        {"ok": True, "object_id": "x" * 257},
    ],
)
def test_invalid_declared_result_slot_fails_producer_and_stops(
    raw: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer_calls = 0
    _install_clock(monkeypatch, 0, 1)

    def forbidden(**kwargs: object) -> object:
        nonlocal consumer_calls
        del kwargs
        consumer_calls += 1
        raise AssertionError("consumer must not run")

    outcomes = _execute_validated_program(
        _three_step_program(),
        {
            "create_box": lambda **kwargs: raw,
            "modify_parameter": forbidden,
            "inspect_model": forbidden,
        },
        execution_profile=ExecutionProfile.HEADLESS,
    )

    assert len(outcomes) == 1
    assert consumer_calls == 0
    assert outcomes[0].result.ok is False
    assert outcomes[0].result.error is not None
    assert outcomes[0].result.error.code == ToolResultCode.INVALID_TOOL_RESULT.value
    assert outcomes[0].diagnostic is ToolDiagnosticClass.INVALID_RESULT
    assert outcomes[0].result.evidence == ()
    assert "x" * 257 not in json.dumps(outcomes[0].result.to_mapping())


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (
            {
                "ok": False,
                "message": "safe failure",
                "retryable": True,
                "needs_input": False,
            },
            ToolResultCode.TOOL_REPORTED_ERROR,
        ),
        (
            {"ok": True, "error": "private contradiction"},
            ToolResultCode.CONTRADICTORY_TOOL_RESULT,
        ),
        (object(), ToolResultCode.INVALID_TOOL_RESULT),
        ([{"ok": True}, _McpLikeAttachment()], ToolResultCode.INVALID_TOOL_RESULT),
    ],
)
def test_first_normalized_failure_stops_globally_without_retry(
    raw: object,
    code: ToolResultCode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def inspect() -> object:
        nonlocal calls
        calls += 1
        return raw

    clock = _install_clock(monkeypatch, 0, 1_000_000)

    outcomes = execute_validated_program(
        _inspect_program(2),
        {"inspect_model": inspect},
    )

    assert len(outcomes) == 1
    assert calls == 1
    assert clock.calls == 2
    assert outcomes[0].result.ok is False
    assert outcomes[0].result.error is not None
    assert outcomes[0].result.error.code == code.value
    assert outcomes[0].result.evidence == ()
    serialized = json.dumps(outcomes[0].result.to_mapping())
    assert "private contradiction" not in serialized
    assert "private-mcp-attachment" not in serialized


def test_handler_exception_is_redacted_classified_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def inspect() -> object:
        nonlocal calls
        calls += 1
        raise ValueError("private exception detail")

    _install_clock(monkeypatch, 0, 1_000_000)

    outcomes = execute_validated_program(
        _inspect_program(2),
        {"inspect_model": inspect},
    )

    assert len(outcomes) == 1
    assert calls == 1
    assert outcomes[0].diagnostic is ToolDiagnosticClass.VALUE_EXCEPTION
    assert outcomes[0].result.error is not None
    assert outcomes[0].result.error.code == ToolResultCode.UNEXPECTED_TOOL_EXCEPTION.value
    serialized = json.dumps(outcomes[0].result.to_mapping())
    assert "private exception detail" not in serialized
    assert "ValueError" not in serialized


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(3)])
def test_handler_base_exception_is_propagated_as_same_object(
    exception: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def inspect() -> object:
        nonlocal calls
        calls += 1
        raise exception

    clock = _install_clock(monkeypatch, 0, 1_000_000)

    with pytest.raises(type(exception)) as caught:
        execute_validated_program(
            _inspect_program(2),
            {"inspect_model": inspect},
        )

    assert caught.value is exception
    assert calls == 1
    assert clock.calls == 2


@pytest.mark.parametrize(
    ("exception", "clock_exception"),
    [
        (KeyboardInterrupt(), RuntimeError("private clock failure")),
        (SystemExit(4), KeyboardInterrupt("private clock interrupt")),
    ],
)
def test_handler_base_exception_wins_when_final_clock_raises(
    exception: BaseException,
    clock_exception: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inspect() -> object:
        raise exception

    clock = _install_clock(monkeypatch, 0, clock_exception)

    with pytest.raises(type(exception)) as caught:
        execute_validated_program(_inspect_program(), {"inspect_model": inspect})

    assert caught.value is exception
    assert clock.calls == 2


@pytest.mark.parametrize(
    ("clock_values", "expected_handler_calls", "expected_clock_calls"),
    [
        (
            (RuntimeError("private start-clock failure"), 10),
            0,
            1,
        ),
        ((0, RuntimeError("private finish-clock failure")), 1, 2),
        ((10, 5), 1, 2),
    ],
    ids=["start-exception", "finish-exception", "backwards"],
)
def test_clock_failures_fail_closed_without_retry(
    clock_values: tuple[int | BaseException, int | BaseException],
    expected_handler_calls: int,
    expected_clock_calls: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def box(**kwargs: object) -> dict[str, object]:
        nonlocal calls
        del kwargs
        calls += 1
        return {
            "ok": True,
            "object_id": "object_22222222222222222222222222222222",
        }

    clock = _install_clock(monkeypatch, *clock_values)
    program = _validated(
        _command(
            "box-1",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        ),
        _command(
            "box-2",
            "create_box",
            args={"length_mm": 4, "width_mm": 5, "height_mm": 6},
        ),
    )

    outcomes = execute_validated_program(
        program,
        {"create_box": box},
    )

    assert len(outcomes) == 1
    assert calls == expected_handler_calls
    assert clock.calls == expected_clock_calls
    outcome = outcomes[0]
    assert outcome.result.ok is False
    assert outcome.result.elapsed_ms == 0
    assert outcome.result.error is not None
    assert outcome.result.error.code == "resource_budget_exceeded"
    assert outcome.result.error.retryable is False
    assert outcome.result.evidence == ()


def test_equal_monotonic_readings_are_a_valid_zero_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def inspect() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    clock = _install_clock(monkeypatch, 10, 10)

    outcome = execute_validated_program(
        _inspect_program(),
        {"inspect_model": inspect},
    )[0]

    assert calls == 1
    assert clock.calls == 2
    assert outcome.result.ok is True
    assert outcome.result.elapsed_ms == 0


def _resource_program(
    budget: ResourceBudget,
    *,
    evidence_required: bool = False,
    profiles: tuple[ExecutionProfile, ...] = (ExecutionProfile.HEADLESS,),
    minimum_version: tuple[int, int] = (1, 0),
    maximum_version: tuple[int, int] = (2, 0),
    requires_gui_main_thread: bool = False,
    command_count: int = 1,
) -> ValidatedProgram:
    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="bounded_inspect",
                handler_name="bounded_inspect",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=evidence_required,
                execution_profiles=profiles,
                minimum_freecad_version=minimum_version,
                maximum_freecad_version_exclusive=maximum_version,
                requires_gui_main_thread=requires_gui_main_thread,
                resource_budget=budget,
            ),
        )
    )
    return validate_model_program(
        _model_program(
            *(_command(f"bounded-{index}", "bounded_inspect") for index in range(command_count))
        ),
        registry=registry,
    )


@pytest.mark.parametrize(
    ("field", "ceiling"),
    [
        ("max_runtime_ms", 30_000),
        ("max_created_objects", 1),
        ("max_result_bytes", 262_144),
    ],
)
def test_admission_ceilings_accept_n_and_reject_n_plus_one_before_handler(
    field: str,
    ceiling: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def budget(value: int) -> ResourceBudget:
        values = {
            "max_runtime_ms": 30_000,
            "max_created_objects": 1,
            "max_result_bytes": 262_144,
        }
        values[field] = value
        return ResourceBudget(**values)

    accepted_calls = 0

    def accepted_handler() -> dict[str, object]:
        nonlocal accepted_calls
        accepted_calls += 1
        return {"ok": True}

    _install_clock(monkeypatch, 0, 1)
    accepted = _execute_validated_program(
        _resource_program(budget(ceiling)),
        {"bounded_inspect": accepted_handler},
        execution_profile=ExecutionProfile.HEADLESS,
        freecad_version=(1, 1),
        gui_main_thread=False,
        object_count=lambda: 0,
    )
    assert accepted_calls == 1
    assert accepted[0].result.ok is True

    rejected_calls = 0

    def rejected_handler() -> dict[str, object]:
        nonlocal rejected_calls
        rejected_calls += 1
        return {"ok": True}

    with pytest.raises(AdapterError) as caught:
        _execute_validated_program(
            _resource_program(budget(ceiling + 1)),
            {"bounded_inspect": rejected_handler},
            execution_profile=ExecutionProfile.HEADLESS,
            freecad_version=(1, 1),
            gui_main_thread=False,
            object_count=lambda: 0,
        )
    assert caught.value.code is AdapterErrorCode.INVALID_PROGRAM
    assert rejected_calls == 0


def test_version_and_gui_thread_constraints_are_preflighted_before_handler() -> None:
    calls = 0

    def handler() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True}

    versioned = _resource_program(
        ResourceBudget(),
        minimum_version=(1, 1),
        maximum_version=(1, 2),
    )
    with pytest.raises(AdapterError) as version_error:
        _execute_validated_program(
            versioned,
            {"bounded_inspect": handler},
            execution_profile=ExecutionProfile.HEADLESS,
            freecad_version=(1, 0),
            gui_main_thread=False,
            object_count=lambda: 0,
        )
    assert version_error.value.code is AdapterErrorCode.UNSUPPORTED_EXECUTION_PROFILE

    gui_program = _resource_program(
        ResourceBudget(),
        profiles=(ExecutionProfile.OFFSCREEN_GUI,),
        requires_gui_main_thread=True,
    )
    with pytest.raises(AdapterError) as thread_error:
        _execute_validated_program(
            gui_program,
            {"bounded_inspect": handler},
            execution_profile=ExecutionProfile.OFFSCREEN_GUI,
            freecad_version=(1, 1),
            gui_main_thread=False,
            object_count=lambda: 0,
        )
    assert thread_error.value.code is AdapterErrorCode.UNSUPPORTED_EXECUTION_PROFILE
    assert calls == 0


@pytest.mark.parametrize("resource", ["runtime", "objects", "result"])
def test_authentic_lower_command_budget_fails_after_return_and_stops_program(
    resource: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    budgets = {
        "runtime": ResourceBudget(
            max_runtime_ms=1,
            max_created_objects=1,
            max_result_bytes=262_144,
        ),
        "objects": ResourceBudget(
            max_runtime_ms=30_000,
            max_created_objects=1,
            max_result_bytes=262_144,
        ),
        "result": ResourceBudget(
            max_runtime_ms=30_000,
            max_created_objects=1,
            max_result_bytes=1,
        ),
    }
    calls = 0

    def handler() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True, "payload": "bounded"}

    _install_clock(monkeypatch, 0, 2_000_000)
    counts = iter((0, 2) if resource == "objects" else (0, 0))
    outcomes = _execute_validated_program(
        _resource_program(budgets[resource], command_count=2),
        {"bounded_inspect": handler},
        execution_profile=ExecutionProfile.HEADLESS,
        freecad_version=(1, 1),
        gui_main_thread=False,
        object_count=lambda: next(counts),
    )

    assert calls == 1
    assert len(outcomes) == 1
    assert outcomes[0].result.ok is False


def test_result_budget_applies_after_trusted_execution_evidence_is_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generous = ResourceBudget(
        max_runtime_ms=30_000,
        max_created_objects=0,
        max_result_bytes=262_144,
    )
    _install_clock(monkeypatch, 0, 1, 2, 3)
    successful = _execute_validated_program(
        _resource_program(generous, evidence_required=True),
        {"bounded_inspect": lambda: {"ok": True}},
        execution_profile=ExecutionProfile.HEADLESS,
        object_count=lambda: 0,
    )[0]
    final_size = len(
        json.dumps(
            successful.result.to_mapping(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    assert successful.result.ok is True
    assert len(successful.result.evidence) == 1

    limited = ResourceBudget(
        max_runtime_ms=30_000,
        max_created_objects=0,
        max_result_bytes=final_size - 1,
    )
    outcome = _execute_validated_program(
        _resource_program(limited, evidence_required=True),
        {"bounded_inspect": lambda: {"ok": True}},
        execution_profile=ExecutionProfile.HEADLESS,
        object_count=lambda: 0,
    )[0]

    assert outcome.result.ok is False
    assert outcome.result.error is not None
    assert outcome.result.error.code == "resource_budget_exceeded"


def test_raw_context_like_fields_cannot_override_trusted_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "ok": True,
        "elapsed_ms": 999,
        "revision": "untrusted-revision",
        "facts": {"untrusted": True},
        "artifacts": ["untrusted.step"],
        "warnings": ["untrusted warning"],
        "evidence": ["untrusted evidence"],
        "object_id": "object_22222222222222222222222222222222",
    }
    _install_clock(monkeypatch, 0, 2_500_000)
    program = _validated(
        _command(
            "box",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        ),
    )

    outcome = execute_validated_program(
        program,
        {"create_box": lambda **kwargs: raw},
        revision="candidate-r2",
    )[0]

    assert outcome.result.elapsed_ms == 2.5
    assert outcome.result.revision == "candidate-r2"
    assert dict(outcome.result.facts) == {
        "operation": "create_box",
        "handler_name": "create_box",
        "risk_class": "mutating",
        "execution_profile": "headless",
    }
    assert outcome.result.artifacts == ()
    assert outcome.result.warnings == ()
    assert len(outcome.result.evidence) == 1
    value = outcome.result.to_mapping()["value"]
    assert value["elapsed_ms"] == 999  # type: ignore[index]
    assert value["revision"] == "untrusted-revision"  # type: ignore[index]
    assert value["facts"] == {"untrusted": True}  # type: ignore[index]


def test_absent_candidate_revision_does_not_use_program_base_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_clock(monkeypatch, 0, 1)

    outcome = execute_validated_program(
        _inspect_program(),
        {"inspect_model": lambda: {"valid": True}},
    )[0]

    assert outcome.result.revision is None


def test_return_value_is_deeply_frozen_against_handler_owned_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {"valid": True, "bbox": {"values": [10, 20, 30]}}
    _install_clock(monkeypatch, 0, 1)

    outcome = execute_validated_program(
        _inspect_program(),
        {"inspect_model": lambda: raw},
    )[0]
    before = outcome.result.to_mapping()

    raw["bbox"]["values"].append(40)  # type: ignore[index,union-attr]

    assert outcome.result.to_mapping() == before


@pytest.mark.parametrize(
    "failure",
    [
        {"ok": False, "message": "reported"},
        {"ok": True, "error": "contradictory"},
        object(),
        RuntimeError("private exception"),
    ],
    ids=["reported", "contradictory", "invalid", "exception"],
)
def test_failed_evidence_required_command_receives_no_success_observation(
    failure: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_clock(monkeypatch, 0, 1)
    program = _validated(
        _command(
            "box",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        ),
    )

    def box(**kwargs: object) -> object:
        del kwargs
        if isinstance(failure, Exception):
            raise failure
        return failure

    outcome = execute_validated_program(program, {"create_box": box})[0]

    assert outcome.result.ok is False
    assert outcome.result.evidence == ()


def test_adapter_import_is_clean_in_an_isolated_python() -> None:
    source = Path(__file__).resolve().parent.parent / "src"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(source)!r}); "
        "import vibecad.execution.adapter; "
        "prefixes=('FreeCAD','Part','mcp','vibecad.server','vibecad.engine',"
        "'vibecad.tools','anthropic','openai'); "
        "loaded=sorted(name for name in sys.modules "
        "if any(name == p or name.startswith(p + '.') for p in prefixes)); "
        "print(loaded); raise SystemExit(1 if loaded else 0)"
    )

    process = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "[]"


def test_isolated_execution_path_remains_free_of_cad_mcp_and_model_imports() -> None:
    source = Path(__file__).resolve().parent.parent / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source)!r})
from vibecad.execution.adapter import execute_validated_program
from vibecad.execution.registry import ExecutionProfile
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.program import validate_model_program
command = ModelCommand(
    id="inspect",
    op="inspect_model",
    target={{}},
    args={{}},
    depends_on=(),
    preserve=(),
    source=ValueSource.MODEL,
)
program = ModelProgram(
    task_id="isolated",
    base_revision="base",
    operations=(command,),
    acceptance=AcceptanceSpec(id="accept", criteria=()),
)
outcomes = execute_validated_program(
    validate_model_program(program),
    {{"inspect_model": lambda: {{"valid": True}}}},
    execution_profile=ExecutionProfile.HEADLESS,
)
assert len(outcomes) == 1 and outcomes[0].result.ok
prefixes = (
    "FreeCAD", "Part", "mcp", "vibecad.server", "vibecad.engine",
    "vibecad.tools", "anthropic", "openai",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
)
print(loaded)
raise SystemExit(1 if loaded else 0)
"""

    process = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "[]"


def test_adapter_source_has_no_dynamic_or_reflective_handler_resolution() -> None:
    source = Path(adapter_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)

    assert "importlib" not in imported
    assert not any(
        name.startswith(("vibecad.server", "vibecad.engine", "vibecad.tools")) for name in imported
    )
    assert called_names.isdisjoint(
        {"__import__", "eval", "exec", "getattr", "globals", "hasattr", "locals"}
    )


@pytest.mark.slow
def test_real_freecad_legacy_custom_registry_flow_and_cleanup(
    existing_freecad_python: str,
) -> None:
    source = Path(__file__).resolve().parent.parent / "src"
    code = """
import json
import sys
sys.path.insert(0, __SOURCE__)

from vibecad.engine.session import Session
from vibecad.execution.adapter import execute_validated_program
from vibecad.execution.registry import (
    ExecutionProfile,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
)
from vibecad.feedback.text import describe_assembly
from vibecad.tools import modeling, modify
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.program import validate_model_program

def command(command_id, op, args=None, target=None, depends_on=()):
    return ModelCommand(
        id=command_id,
        op=op,
        target={} if target is None else target,
        args={} if args is None else args,
        depends_on=depends_on,
        preserve=(),
        source=ValueSource.MODEL,
    )

legacy_registry = OperationRegistry(
    (
        OperationMetadata(
            operation="create_box",
            handler_name="add_box",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            argument_fields=(
                FieldMetadata("length", "length", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("width", "width", ValueShape.POSITIVE_NUMBER),
                FieldMetadata("height", "height", ValueShape.POSITIVE_NUMBER),
                FieldMetadata(
                    "position",
                    "position",
                    ValueShape.VECTOR3,
                    required=False,
                ),
            ),
            result_slots=(
                ResultSlotMetadata("object", "name", ValueShape.NONBLANK_STRING),
            ),
        ),
        OperationMetadata(
            operation="modify_parameter",
            handler_name="modify_part",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            target_fields=(
                FieldMetadata(
                    "object",
                    "name",
                    ValueShape.RESULT_REF,
                    referenced_value_shape=ValueShape.NONBLANK_STRING,
                ),
            ),
            argument_fields=(
                FieldMetadata("parameter", "parameter", ValueShape.NONBLANK_STRING),
                FieldMetadata("value", "value", ValueShape.POSITIVE_NUMBER),
            ),
        ),
        OperationMetadata(
            operation="inspect_model",
            handler_name="describe_part",
            risk_class=RiskClass.READ_ONLY,
            evidence_required=False,
        ),
    )
)

program = ModelProgram(
    task_id="real-adapter",
    base_revision="untrusted-base",
    operations=(
        command(
            "box",
            "create_box",
            {"length": 10, "width": 20, "height": 30},
        ),
        command(
            "modify",
            "modify_parameter",
            {"parameter": "Length", "value": 12},
            {"object": {"command_id": "box", "slot": "object"}},
            ("box",),
        ),
        command("inspect", "inspect_model", depends_on=("modify",)),
    ),
    acceptance=AcceptanceSpec(id="real-acceptance", criteria=()),
)
validated = validate_model_program(program, registry=legacy_registry)
session = Session()
payload = {}
try:
    session.open_document("AdapterReal")
    outcomes = execute_validated_program(
        validated,
        {
            "add_box": lambda **kwargs: modeling.add_box(session, **kwargs),
            "modify_part": lambda **kwargs: modify.modify_part(session, **kwargs),
            "describe_part": lambda: describe_assembly(session),
        },
        execution_profile=ExecutionProfile.HEADLESS,
        revision="candidate-real-r1",
    )
    payload["outcomes"] = [item.result.to_mapping() for item in outcomes]
finally:
    session.close_document()
    payload["closed"] = session.doc is None
print(json.dumps(payload, ensure_ascii=False))
""".replace("__SOURCE__", repr(str(source)))

    process = subprocess.run(
        [existing_freecad_python, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert process.returncode == 0, process.stderr
    payload = json.loads(process.stdout.strip().splitlines()[-1])
    outcomes = payload["outcomes"]
    assert payload["closed"] is True
    assert len(outcomes) == 3
    assert all(item["ok"] is True for item in outcomes)
    assert outcomes[0]["value"]["volume"] == pytest.approx(6000)
    assert outcomes[1]["value"]["volume"] == pytest.approx(7200)
    inspection = outcomes[2]["value"]
    assert inspection["valid"] is True
    assert inspection["volume"] == pytest.approx(7200)
    assert inspection["bbox"] == pytest.approx({"x": 12, "y": 20, "z": 30})
    assert inspection["solid_count"] == 1
    assert [len(item["evidence"]) for item in outcomes] == [1, 1, 0]
    assert all(item["revision"] == "candidate-real-r1" for item in outcomes)
    assert all(item["elapsed_ms"] >= 0 for item in outcomes)
