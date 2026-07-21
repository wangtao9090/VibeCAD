"""Pre-execution validation tests for declarative model programs."""

from __future__ import annotations

import builtins
import copy
import dataclasses
import pickle
from collections.abc import Iterator, Mapping
from types import MappingProxyType

import pytest

from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER, SCHEMA_VERSION
from vibecad.workflow.program import (
    DEFAULT_MAX_COMMANDS,
    BoundCommand,
    ProgramErrorCode,
    ProgramValidationError,
    ValidatedProgram,
    validate_model_program,
)


class _HostileMapping(Mapping):
    def __init__(self, *, fail_iteration: bool) -> None:
        self.fail_iteration = fail_iteration

    def __getitem__(self, key):
        raise RuntimeError("hostile value access")

    def __iter__(self) -> Iterator[str]:
        if self.fail_iteration:
            raise RuntimeError("hostile key iteration")
        return iter(("schema_version", "code", "path", "message"))

    def __len__(self) -> int:
        return 4


def _command(
    command_id: str,
    op: str = "inspect_model",
    *,
    target: Mapping[str, object] | None = None,
    args: Mapping[str, object] | None = None,
    depends_on: tuple[str, ...] = (),
    preserve: tuple[str, ...] = (),
) -> ModelCommand:
    return ModelCommand(
        id=command_id,
        op=op,
        target={} if target is None else target,
        args={} if args is None else args,
        depends_on=depends_on,
        preserve=preserve,
        source=ValueSource.MODEL,
    )


def _program(*commands: ModelCommand) -> ModelProgram:
    return ModelProgram(
        task_id="task-1",
        base_revision="revision-1",
        operations=commands,
        acceptance=AcceptanceSpec(id="acceptance-1", criteria=()),
    )


def _error(program: object, code: ProgramErrorCode, path: str, **kwargs):
    with pytest.raises(ProgramValidationError) as caught:
        validate_model_program(program, **kwargs)
    assert caught.value.code is code
    assert caught.value.path == path
    return caught.value


def _custom_registry(
    *,
    operation: str = "toggle_visibility",
    handler_name: str = "set_visibility",
    shape: ValueShape = ValueShape.BOOLEAN,
) -> OperationRegistry:
    return OperationRegistry(
        (
            OperationMetadata(
                operation=operation,
                handler_name=handler_name,
                risk_class=RiskClass.MUTATING,
                evidence_required=True,
                argument_fields=(FieldMetadata("value", "value", shape),),
            ),
        )
    )


def _label_registry() -> OperationRegistry:
    return OperationRegistry(
        (
            OperationMetadata(
                operation="set_label",
                handler_name="set_label",
                risk_class=RiskClass.MUTATING,
                evidence_required=True,
                argument_fields=(
                    FieldMetadata("name", "name", ValueShape.NONBLANK_STRING),
                ),
            ),
        )
    )


def _dimension_registry() -> OperationRegistry:
    return OperationRegistry(
        (
            OperationMetadata(
                operation="set_dimension",
                handler_name="set_dimension",
                risk_class=RiskClass.MUTATING,
                evidence_required=True,
                argument_fields=(
                    FieldMetadata("parameter", "parameter", ValueShape.NONBLANK_STRING),
                    FieldMetadata("value", "value", ValueShape.POSITIVE_NUMBER),
                ),
            ),
        )
    )


def test_level_a_selector_requires_complete_bound_identity() -> None:
    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="select_object",
                handler_name="select_object",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=False,
                target_fields=(
                    FieldMetadata(
                        "object",
                        "selector",
                        ValueShape.OBJECT_SELECTOR,
                    ),
                ),
            ),
        )
    )
    revision = "revision_22222222222222222222222222222222"
    program = ModelProgram(
        task_id="task-selector-red",
        base_revision=revision,
        operations=(
            _command(
                "select",
                "select_object",
                target={
                    "object": {
                        "schema_version": 1,
                        "project_id": "project_11111111111111111111111111111111",
                        "revision_id": revision,
                        "object_id": "object_33333333333333333333333333333333",
                        "expected_cardinality": 1,
                    }
                },
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-selector-red", criteria=()),
    )

    _error(
        program,
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/target/object",
        registry=registry,
    )


def _level_a_selector_registry() -> OperationRegistry:
    return OperationRegistry(
        (
            OperationMetadata(
                operation="select_object",
                handler_name="select_object",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=False,
                target_fields=(
                    FieldMetadata("object", "selector", ValueShape.OBJECT_SELECTOR),
                ),
            ),
        )
    )


def _level_a_selector(*, revision_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": "project_11111111111111111111111111111111",
        "revision_id": revision_id,
        "entity_kind": "feature",
        "object_id": "object_33333333333333333333333333333333",
        "feature_id": "feature_44444444444444444444444444444444",
        "object_type": "Part::Box",
        "semantic_role": "primitive",
        "provenance": {"source": "model", "operation_id": "box"},
        "expected_cardinality": 1,
    }


def test_level_a_selector_is_typed_and_bound_to_program_base_revision() -> None:
    revision = "revision_22222222222222222222222222222222"
    validated = validate_model_program(
        ModelProgram(
            task_id="task-selector",
            base_revision=revision,
            operations=(
                _command(
                    "select",
                    "select_object",
                    target={"object": _level_a_selector(revision_id=revision)},
                ),
            ),
            acceptance=AcceptanceSpec(id="acceptance-selector", criteria=()),
        ),
        registry=_level_a_selector_registry(),
    )

    bound = validated.commands[0].handler_kwargs["selector"]
    assert type(bound).__name__ == "SelectorV1"
    assert bound.revision_id == revision
    assert bound.object_id == "object_33333333333333333333333333333333"
    assert bound.provenance.operation_id == "box"


def test_level_a_selector_rejects_stale_revision_before_execution() -> None:
    base_revision = "revision_22222222222222222222222222222222"
    program = ModelProgram(
        task_id="task-selector-stale",
        base_revision=base_revision,
        operations=(
            _command(
                "select",
                "select_object",
                target={
                    "object": _level_a_selector(
                        revision_id="revision_55555555555555555555555555555555"
                    )
                },
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-selector-stale", criteria=()),
    )

    _error(
        program,
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/target/object",
        registry=_level_a_selector_registry(),
    )


def test_default_entity_target_accepts_base_selector_or_dependency_object_ref() -> None:
    revision = "revision_22222222222222222222222222222222"
    selector_program = ModelProgram(
        task_id="task-default-selector",
        base_revision=revision,
        operations=(
            _command(
                "modify",
                "modify_parameter",
                target={"object": _level_a_selector(revision_id=revision)},
                args={"parameter": "length", "value_mm": 12},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-default-selector", criteria=()),
    )
    selector_bound = validate_model_program(selector_program).commands[0].handler_kwargs[
        "target"
    ]
    assert type(selector_bound).__name__ == "SelectorV1"

    result_program = ModelProgram(
        task_id="task-default-result-ref",
        base_revision=revision,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length_mm": 10, "width_mm": 5, "height_mm": 2},
            ),
            _command(
                "modify",
                "modify_parameter",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"parameter": "length", "value_mm": 12},
                depends_on=("box",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-default-result-ref", criteria=()),
    )
    result_bound = validate_model_program(result_program).commands[1].handler_kwargs["target"]
    assert type(result_bound).__name__ == "BoundResultRef"
    assert result_bound.value_shape is ValueShape.OBJECT_ID


@pytest.mark.parametrize(
    "target",
    [
        pytest.param(
            {
                key: value
                for key, value in _level_a_selector(
                    revision_id="revision_22222222222222222222222222222222"
                ).items()
                if key != "feature_id"
            },
            id="partial-selector",
        ),
        pytest.param(
            _level_a_selector(
                revision_id="revision_55555555555555555555555555555555"
            ),
            id="stale-selector",
        ),
    ],
)
def test_default_entity_target_rejects_partial_and_stale_selectors(target) -> None:
    revision = "revision_22222222222222222222222222222222"
    program = ModelProgram(
        task_id="task-default-invalid-selector",
        base_revision=revision,
        operations=(
            _command(
                "modify",
                "modify_parameter",
                target={"object": target},
                args={"parameter": "length", "value_mm": 12},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-default-invalid-selector", criteria=()),
    )

    _error(
        program,
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/target/object",
    )


def test_every_default_operation_is_bound_for_execution_without_invocation():
    program = _program(
        _command(
            "box",
            "create_box",
            args={
                "length_mm": 10,
                "width_mm": 5.5,
                "height_mm": 2,
                "position_mm": [-1, 0, 3],
            },
        ),
        _command(
            "cylinder",
            "create_cylinder",
            args={
                "radius_mm": 2,
                "height_mm": 8,
                "position_mm": [4, 5, 6],
                "axis": "z",
            },
        ),
        _command(
            "modify",
            "modify_parameter",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"parameter": "length", "value_mm": 12},
            depends_on=("box",),
            preserve=("width", "height"),
        ),
        _command(
            "move",
            "move_part",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"position_mm": [10, 20, 30]},
            depends_on=("modify",),
            preserve=("parameters",),
        ),
        _command(
            "rotate",
            "rotate_part",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"axis": "y", "angle_deg": -90},
            depends_on=("move",),
            preserve=("volume_mm3",),
        ),
        _command("inspect", depends_on=("cylinder", "rotate")),
    )

    validated = validate_model_program(program)

    assert validated.program is program
    assert [command.id for command in validated.commands] == [
        "box",
        "cylinder",
        "modify",
        "move",
        "rotate",
        "inspect",
    ]
    assert [command.handler_name for command in validated] == [
        "create_box",
        "create_cylinder",
        "modify_parameter",
        "move_part",
        "rotate_part",
        "inspect_model",
    ]
    assert dict(validated.commands[0].handler_kwargs) == {
        "length": 10,
        "width": 5.5,
        "height": 2,
        "position": (-1, 0, 3),
    }
    assert dict(validated.commands[1].handler_kwargs) == {
        "radius": 2,
        "height": 8,
        "position": (4, 5, 6),
        "axis": "z",
    }
    assert validated.commands[2].handler_kwargs["target"].command_id == "box"
    assert validated.commands[2].handler_kwargs["target"].slot == "object"
    assert dict(validated.commands[2].handler_kwargs) == {
        "target": validated.commands[2].handler_kwargs["target"],
        "parameter": "length",
        "value": 12,
    }
    assert dict(validated.commands[3].handler_kwargs) == {
        "target": validated.commands[3].handler_kwargs["target"],
        "position": (10, 20, 30),
    }
    assert dict(validated.commands[4].handler_kwargs) == {
        "target": validated.commands[4].handler_kwargs["target"],
        "axis": "y",
        "angle": -90,
    }
    assert dict(validated.commands[5].handler_kwargs) == {}
    assert validated.commands[2].preserve == ("width", "height")
    assert validated.commands[3].preserve == ("parameters",)
    assert validated.commands[4].preserve == ("volume_mm3",)
    assert validated.commands[5].risk_class is RiskClass.READ_ONLY
    assert validated.commands[5].evidence_required is False


def test_result_ref_binds_a_declared_dependency_slot_without_resolving_it():
    program = _program(
        _command(
            "box",
            "create_box",
            args={"length_mm": 10, "width_mm": 5, "height_mm": 2},
        ),
        _command(
            "modify",
            "modify_parameter",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"parameter": "length", "value_mm": 12},
            depends_on=("box",),
        ),
    )

    validated = validate_model_program(program)
    bound_ref = validated.commands[1].handler_kwargs["target"]

    assert type(bound_ref).__name__ == "BoundResultRef"
    assert bound_ref.command_id == "box"
    assert bound_ref.slot == "object"


def test_create_document_is_no_longer_a_model_program_operation():
    _error(
        _program(_command("document", "create_document", args={"name": "Part"})),
        ProgramErrorCode.UNKNOWN_OPERATION,
        "/operations/0/op",
    )


@pytest.mark.parametrize(
    "target",
    [
        {"command_id": "box"},
        {"command_id": "box", "slot": "object", "extra": True},
        "Box",
    ],
)
def test_entity_target_rejects_malformed_refs_and_raw_names_as_invalid_shapes(target):
    program = _program(
        _command(
            "box",
            "create_box",
            args={"length_mm": 10, "width_mm": 5, "height_mm": 2},
        ),
        _command(
            "modify",
            "modify_parameter",
            target={"object": target},
            args={"parameter": "length", "value_mm": 12},
            depends_on=("box",),
        ),
    )

    _error(
        program,
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/1/target/object",
    )


@pytest.mark.parametrize(
    "target",
    [
        {"command_id": "missing", "slot": "object"},
        {"command_id": "box", "slot": "missing"},
    ],
)
def test_entity_target_rejects_well_shaped_but_unavailable_result_refs(target):
    program = _program(
        _command(
            "box",
            "create_box",
            args={"length_mm": 10, "width_mm": 5, "height_mm": 2},
        ),
        _command(
            "modify",
            "modify_parameter",
            target={"object": target},
            args={"parameter": "length", "value_mm": 12},
            depends_on=("box",),
        ),
    )

    _error(
        program,
        ProgramErrorCode.INVALID_RESULT_REFERENCE,
        "/operations/1/target/object",
    )


def test_result_ref_requires_a_prior_transitive_dependency():
    valid = _program(
        _command(
            "box",
            "create_box",
            args={"length_mm": 10, "width_mm": 5, "height_mm": 2},
        ),
        _command("inspect", depends_on=("box",)),
        _command(
            "modify",
            "modify_parameter",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"parameter": "length", "value_mm": 12},
            depends_on=("inspect",),
        ),
    )
    validated = validate_model_program(valid)
    assert validated.commands[2].handler_kwargs["target"].command_id == "box"

    non_dependency = _program(
        valid.operations[0],
        _command(
            "modify",
            "modify_parameter",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"parameter": "length", "value_mm": 12},
        ),
    )
    _error(
        non_dependency,
        ProgramErrorCode.INVALID_RESULT_REFERENCE,
        "/operations/1/target/object",
    )

    forward = _program(
        _command(
            "modify",
            "modify_parameter",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"parameter": "length", "value_mm": 12},
            depends_on=("box",),
        ),
        valid.operations[0],
    )
    _error(
        forward,
        ProgramErrorCode.INVALID_RESULT_REFERENCE,
        "/operations/0/target/object",
    )


def test_result_ref_rejects_declared_slot_shape_mismatch():
    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="measure_value",
                handler_name="measure_value",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=False,
                result_slots=(
                    ResultSlotMetadata("measurement", "value", ValueShape.FINITE_NUMBER),
                ),
            ),
            OperationMetadata(
                operation="set_label",
                handler_name="set_label",
                risk_class=RiskClass.MUTATING,
                evidence_required=True,
                argument_fields=(
                    FieldMetadata(
                        "name",
                        "name",
                        ValueShape.RESULT_REF,
                        referenced_value_shape=ValueShape.NONBLANK_STRING,
                    ),
                ),
            ),
        )
    )
    program = _program(
        _command("measure", "measure_value"),
        _command(
            "label",
            "set_label",
            args={"name": {"command_id": "measure", "slot": "measurement"}},
            depends_on=("measure",),
        ),
    )

    _error(
        program,
        ProgramErrorCode.INVALID_RESULT_REFERENCE,
        "/operations/1/args/name",
        registry=registry,
    )


def test_topological_order_uses_declaration_index_as_the_ready_tie_break():
    program = _program(
        _command("c", depends_on=("a",)),
        _command("b"),
        _command("a"),
        _command("d", depends_on=("b",)),
    )

    validated = validate_model_program(program)

    assert tuple(command.id for command in validated) == ("b", "a", "c", "d")


@pytest.mark.parametrize(
    ("commands", "code", "path"),
    [
        (
            (_command("same"), _command("same")),
            ProgramErrorCode.DUPLICATE_COMMAND_ID,
            "/operations/1/id",
        ),
        (
            (_command("a"), _command("b", depends_on=("a", "a"))),
            ProgramErrorCode.DUPLICATE_DEPENDENCY,
            "/operations/1/depends_on/1",
        ),
        (
            (_command("self", depends_on=("self",)),),
            ProgramErrorCode.SELF_DEPENDENCY,
            "/operations/0/depends_on/0",
        ),
        (
            (_command("a", depends_on=("missing",)),),
            ProgramErrorCode.UNKNOWN_DEPENDENCY,
            "/operations/0/depends_on/0",
        ),
        (
            (_command("a", depends_on=("b",)), _command("b", depends_on=("a",))),
            ProgramErrorCode.DEPENDENCY_CYCLE,
            "/operations",
        ),
    ],
)
def test_dependency_graph_failures_have_stable_codes_and_paths(commands, code, path):
    _error(_program(*commands), code, path)


def test_graph_errors_do_not_reflect_hostile_command_ids():
    hostile = "unknown\nFORGED LOG LINE"
    error = _error(
        _program(_command("command", depends_on=(hostile,))),
        ProgramErrorCode.UNKNOWN_DEPENDENCY,
        "/operations/0/depends_on/0",
    )

    assert hostile not in str(error)
    assert "FORGED" not in error.message


def test_program_must_be_non_empty():
    _error(_program(), ProgramErrorCode.EMPTY_PROGRAM, "/operations")


def test_default_budget_accepts_64_and_rejects_65_commands():
    validate_model_program(_program(*(_command(f"op-{index}") for index in range(64))))

    _error(
        _program(*(_command(f"op-{index}") for index in range(65))),
        ProgramErrorCode.BUDGET_EXCEEDED,
        "/operations",
    )
    assert DEFAULT_MAX_COMMANDS == 64


def test_budget_can_be_injected_downward_for_testing():
    _error(
        _program(_command("a"), _command("b")),
        ProgramErrorCode.BUDGET_EXCEEDED,
        "/operations",
        max_commands=1,
    )


@pytest.mark.parametrize("budget", [True, False, 0, -1, 65, MAX_SAFE_JSON_INTEGER + 1])
def test_budget_configuration_must_stay_within_the_hard_phase_one_limit(budget):
    _error(
        _program(_command("a")),
        ProgramErrorCode.INVALID_CONFIGURATION,
        "/max_commands",
        max_commands=budget,
    )


def test_program_and_registry_configuration_types_fail_closed():
    _error(object(), ProgramErrorCode.INVALID_INPUT, "")
    _error(
        _program(_command("a")),
        ProgramErrorCode.INVALID_CONFIGURATION,
        "/registry",
        registry=object(),
    )


@pytest.mark.parametrize(
    ("mutate", "path"),
    [
        (lambda command: object.__setattr__(command, "id", []), "/operations/0/id"),
        (
            lambda command: object.__setattr__(command, "depends_on", ([],)),
            "/operations/0/depends_on",
        ),
        (lambda command: object.__setattr__(command, "args", {}), "/operations/0/args"),
    ],
)
def test_forged_contract_types_return_structured_invalid_input(mutate, path):
    command = _command("a")
    mutate(command)

    _error(_program(command), ProgramErrorCode.INVALID_INPUT, path)


def test_unknown_operation_is_rejected_without_reflecting_its_value():
    hostile = "execute_python\nFORGED"

    error = _error(
        _program(_command("a", hostile)),
        ProgramErrorCode.UNKNOWN_OPERATION,
        "/operations/0/op",
    )

    assert hostile not in str(error)
    assert hostile not in error.message


def test_required_target_and_argument_fields_are_checked_separately():
    _error(
        _program(
            _command(
                "modify",
                "modify_parameter",
                args={"parameter": "length", "value_mm": 10},
            )
        ),
        ProgramErrorCode.MISSING_FIELD,
        "/operations/0/target/object",
    )
    _error(
        _program(
            _command(
                "box",
                "create_box",
                args={"width_mm": 2, "height_mm": 3},
            )
        ),
        ProgramErrorCode.MISSING_FIELD,
        "/operations/0/args/length_mm",
    )


def test_preserve_is_restricted_to_the_operation_vocabulary_with_exact_index_paths():
    _error(
        _program(
            _command(
                "box",
                "create_box",
                args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
                preserve=("length",),
            )
        ),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/preserve/0",
    )

    revision = "revision_22222222222222222222222222222222"
    _error(
        ModelProgram(
            task_id="task-invalid-preserve",
            base_revision=revision,
            operations=(
                _command(
                    "modify",
                    "modify_parameter",
                    target={"object": _level_a_selector(revision_id=revision)},
                    args={"parameter": "length", "value_mm": 4},
                    preserve=("width", "label"),
                ),
            ),
            acceptance=AcceptanceSpec(id="acceptance-invalid-preserve", criteria=()),
        ),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/preserve/1",
    )


def test_duplicate_preserve_field_is_rejected_before_execution():
    revision = "revision_22222222222222222222222222222222"
    _error(
        ModelProgram(
            task_id="task-duplicate-preserve",
            base_revision=revision,
            operations=(
                _command(
                    "modify",
                    "modify_parameter",
                    target={"object": _level_a_selector(revision_id=revision)},
                    args={"parameter": "length", "value_mm": 4},
                    preserve=("width", "width"),
                ),
            ),
            acceptance=AcceptanceSpec(id="acceptance-duplicate-preserve", criteria=()),
        ),
        ProgramErrorCode.INVALID_INPUT,
        "/operations/0/preserve/1",
    )


def test_extra_fields_are_sorted_and_use_escaped_exact_paths():
    _error(
        _program(
            _command(
                "modify",
                "set_dimension",
                args={
                    "parameter": "Length",
                    "value": 10,
                    "z_field": 1,
                    "unit/name~": "mm",
                },
            )
        ),
        ProgramErrorCode.EXTRA_FIELD,
        "/operations/0/args/unit~1name~0",
        registry=_dimension_registry(),
    )


def test_unit_is_rejected_as_an_extra_field_under_r_b10():
    _error(
        _program(
            _command(
                "modify",
                "set_dimension",
                args={"parameter": "Length", "value": 10, "unit": "mm"},
            )
        ),
        ProgramErrorCode.EXTRA_FIELD,
        "/operations/0/args/unit",
        registry=_dimension_registry(),
    )


def test_hostile_extra_field_uses_safe_group_path_and_is_not_reflected():
    hostile = "unknown\nFORGED"
    error = _error(
        _program(_command("label", "set_label", args={"name": "Part", hostile: 1})),
        ProgramErrorCode.EXTRA_FIELD,
        "/operations/0/args",
        registry=_label_registry(),
    )

    assert hostile not in str(error)
    assert hostile not in error.message


@pytest.mark.parametrize("group", ["target", "args"])
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({object(): 1}, id="object-only"),
        pytest.param({"safe": 1, 2: 2}, id="mixed-string-integer"),
    ],
)
def test_forged_non_string_field_keys_fail_closed_at_the_group_path(group, fields):
    command = _command("inspect")
    object.__setattr__(command, group, MappingProxyType(fields))

    error = _error(
        _program(command),
        ProgramErrorCode.INVALID_INPUT,
        f"/operations/0/{group}",
    )

    assert "object" not in error.message
    assert "safe" not in error.message


@pytest.mark.parametrize("value", ["", "   ", 1, True, None])
def test_nonblank_string_shape_is_enforced(value):
    _error(
        _program(_command("label", "set_label", args={"name": value})),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/name",
        registry=_label_registry(),
    )


@pytest.mark.parametrize("value", [0, -1, True, False, "10", None])
def test_positive_number_shape_excludes_non_positive_boolean_and_other_types(value):
    _error(
        _program(
            _command(
                "modify",
                "set_dimension",
                args={"parameter": "Length", "value": value},
            )
        ),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/value",
        registry=_dimension_registry(),
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_positive_numbers_are_rejected_even_for_forged_contracts(value):
    command = _command(
        "modify",
        "set_dimension",
        args={"parameter": "Length", "value": 1},
    )
    object.__setattr__(
        command,
        "args",
        MappingProxyType({"parameter": "Length", "value": value}),
    )

    _error(
        _program(command),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/value",
        registry=_dimension_registry(),
    )


@pytest.mark.parametrize(
    "position",
    [
        (1, 2),
        (1, 2, 3, 4),
        (True, 2, 3),
        (1, "2", 3),
    ],
)
def test_vector3_shape_requires_exactly_three_non_boolean_numbers(position):
    _error(
        _program(
            _command(
                "box",
                "create_box",
                args={
                    "length_mm": 1,
                    "width_mm": 2,
                    "height_mm": 3,
                    "position_mm": position,
                },
            )
        ),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/position_mm",
    )


def test_vector3_accepts_zero_and_negative_finite_coordinates_and_is_optional():
    with_position = validate_model_program(
        _program(
            _command(
                "box",
                "create_box",
                args={
                    "length_mm": 1,
                    "width_mm": 2,
                    "height_mm": 3,
                    "position_mm": (-1, 0, 2.5),
                },
            )
        )
    )
    without_position = validate_model_program(
        _program(
            _command(
                "box",
                "create_box",
                args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
            )
        )
    )

    assert with_position.commands[0].handler_kwargs["position"] == (-1, 0, 2.5)
    assert "position" not in without_position.commands[0].handler_kwargs


def _rotation_program(angle: object) -> ModelProgram:
    return _program(
        _command(
            "box",
            "create_box",
            args={"length_mm": 1, "width_mm": 2, "height_mm": 3},
        ),
        _command(
            "rotate",
            "rotate_part",
            target={"object": {"command_id": "box", "slot": "object"}},
            args={"axis": "x", "angle_deg": angle},
            depends_on=("box",),
        ),
    )


@pytest.mark.parametrize("angle", [-359.999, -1, 1, 359.999])
def test_angle_degrees_accepts_finite_nonzero_open_interval(angle):
    validated = validate_model_program(_rotation_program(angle))

    assert validated.commands[1].handler_kwargs["angle"] == angle


@pytest.mark.parametrize("angle", [0, -360, 360, True, "90", None])
def test_angle_degrees_rejects_zero_boundaries_boolean_and_non_numbers(angle):
    _error(
        _rotation_program(angle),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/1/args/angle_deg",
    )


@pytest.mark.parametrize("angle", [float("nan"), float("inf"), -float("inf")])
def test_angle_degrees_rejects_forged_non_finite_numbers(angle):
    program = _rotation_program(90)
    object.__setattr__(
        program.operations[1],
        "args",
        MappingProxyType({"axis": "x", "angle_deg": angle}),
    )

    _error(
        program,
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/1/args/angle_deg",
    )


def test_object_id_shape_accepts_only_canonical_lowercase_ids():
    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="bind_object",
                handler_name="bind_object",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=False,
                argument_fields=(
                    FieldMetadata("object_id", "object_id", ValueShape.OBJECT_ID),
                ),
            ),
        )
    )
    canonical = "object_0123456789abcdef0123456789abcdef"
    validated = validate_model_program(
        _program(_command("bind", "bind_object", args={"object_id": canonical})),
        registry=registry,
    )
    assert validated.commands[0].handler_kwargs["object_id"] == canonical

    for invalid in (
        "0123456789abcdef0123456789abcdef",
        "object_0123456789abcdef0123456789abcde",
        "object_0123456789abcdef0123456789abcdef0",
        "object_0123456789ABCDEF0123456789ABCDEF",
        True,
    ):
        _error(
            _program(_command("bind", "bind_object", args={"object_id": invalid})),
            ProgramErrorCode.INVALID_VALUE_SHAPE,
            "/operations/0/args/object_id",
            registry=registry,
        )


@pytest.mark.parametrize("value", [True, False])
def test_boolean_shape_accepts_exact_booleans(value):
    validated = validate_model_program(
        _program(_command("toggle", "toggle_visibility", args={"value": value})),
        registry=_custom_registry(),
    )

    assert validated.commands[0].handler_kwargs["value"] is value


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_boolean_shape_rejects_integer_string_and_null(value):
    _error(
        _program(_command("toggle", "toggle_visibility", args={"value": value})),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/value",
        registry=_custom_registry(),
    )


@pytest.mark.parametrize(
    ("field", "accepted", "rejected"),
    [
        (FieldMetadata("value", "value", ValueShape.INTEGER), -2, True),
        (FieldMetadata("value", "value", ValueShape.FINITE_NUMBER), -2.5, "inf"),
        (
            FieldMetadata(
                "value",
                "value",
                ValueShape.ENUM,
                enum_values=("x", "y", "z"),
            ),
            "z",
            "w",
        ),
        (FieldMetadata("value", "value", ValueShape.VECTOR2), (-1, 2.5), (1, 2, 3)),
        (
            FieldMetadata(
                "value",
                "value",
                ValueShape.QUANTITY,
                allowed_units=("mm", "deg"),
            ),
            {"value": 12.5, "unit": "mm"},
            {"value": 12.5, "unit": "cm"},
        ),
    ],
)
def test_extended_closed_value_shapes(field, accepted, rejected):
    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="set_value",
                handler_name="set_value",
                risk_class=RiskClass.MUTATING,
                evidence_required=True,
                argument_fields=(field,),
            ),
        )
    )

    validated = validate_model_program(
        _program(_command("set", "set_value", args={"value": accepted})),
        registry=registry,
    )
    assert validated.commands[0].handler_kwargs["value"] is not None

    _error(
        _program(_command("set", "set_value", args={"value": rejected})),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/value",
        registry=registry,
    )


def test_wrong_shape_errors_do_not_reflect_hostile_field_values():
    hostile = "secret\nFORGED LOG LINE"
    error = _error(
        _program(_command("label", "set_label", args={"name": {hostile: 1}})),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/name",
        registry=_label_registry(),
    )

    assert hostile not in str(error)
    assert hostile not in error.message


def test_validated_bound_data_is_deeply_immutable_and_detached_from_callers():
    caller_kwargs = {"position": [1, 2, {"axis": [3]}]}
    command = BoundCommand(
        id="a",
        operation="create_box",
        handler_name="add_box",
        handler_kwargs=caller_kwargs,
        depends_on=["before"],  # type: ignore[arg-type]
        preserve=["length"],  # type: ignore[arg-type]
        source=ValueSource.MODEL,
        risk_class=RiskClass.MUTATING,
        evidence_required=True,
    )
    caller_kwargs["position"][2]["axis"].append(4)
    caller_kwargs["other"] = 5

    assert command.handler_kwargs["position"] == (1, 2, {"axis": (3,)})
    assert "other" not in command.handler_kwargs
    assert command.depends_on == ("before",)
    assert command.preserve == ("length",)
    with pytest.raises(TypeError):
        command.handler_kwargs["other"] = 1
    with pytest.raises(TypeError):
        command.handler_kwargs["position"][2]["axis"] = ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        command.handler_name = "other"


def test_validated_program_is_sealed_immutable_and_not_serializable():
    with pytest.raises(TypeError):
        ValidatedProgram()

    forged = object.__new__(ValidatedProgram)
    with pytest.raises(TypeError, match="not authentic"):
        _ = forged.commands

    validated = validate_model_program(_program(_command("inspect")))
    validated.require_authentic()
    assert copy.copy(validated) is validated
    assert copy.deepcopy(validated) is validated
    with pytest.raises(AttributeError):
        validated._commands = ()
    with pytest.raises(TypeError, match="cannot be serialized"):
        pickle.dumps(validated)


@pytest.mark.parametrize("code", list(ProgramErrorCode))
def test_program_error_records_round_trip_for_every_stable_code(code):
    error = ProgramValidationError(code, "", "safe message")

    decoded = ProgramValidationError.from_mapping(error.to_mapping())

    assert decoded.schema_version == SCHEMA_VERSION
    assert decoded.code is code
    assert decoded.path == ""
    assert decoded.message == "safe message"


@pytest.mark.parametrize(
    ("args", "exception"),
    [
        (("not-a-code", "", "message"), TypeError),
        ((ProgramErrorCode.INVALID_INPUT, "not/a/pointer", "message"), ValueError),
        ((ProgramErrorCode.INVALID_INPUT, "/path\nforged", "message"), ValueError),
        ((ProgramErrorCode.INVALID_INPUT, "", ""), ValueError),
        ((ProgramErrorCode.INVALID_INPUT, "", "message\u2028forged"), ValueError),
    ],
)
def test_program_error_constructor_rejects_malformed_records(args, exception):
    with pytest.raises(exception):
        ProgramValidationError(*args)


def test_program_error_constructor_rejects_wrong_schema_version():
    with pytest.raises(ValueError):
        ProgramValidationError(
            ProgramErrorCode.INVALID_INPUT,
            "",
            "message",
            schema_version=2,
        )


@pytest.mark.parametrize(
    "record",
    [
        None,
        {},
        {
            "schema_version": 1,
            "code": "invalid_input",
            "path": "",
            "message": "message",
            "extension": True,
        },
        {"schema_version": True, "code": "invalid_input", "path": "", "message": "message"},
        {"schema_version": 1, "code": 1, "path": "", "message": "message"},
        {"schema_version": 1, "code": "unknown", "path": "", "message": "message"},
        {
            "schema_version": 1,
            "code": "invalid_input",
            "path": "bad",
            "message": "message",
        },
        {
            "schema_version": 1,
            "code": "invalid_input",
            "path": "/ok\nforged",
            "message": "message",
        },
        {"schema_version": 1, "code": "invalid_input", "path": "", "message": ""},
        {
            "schema_version": 1,
            "code": "invalid_input",
            "path": "",
            "message": "message\u2028forged",
        },
        {
            "schema_version": 1,
            "code": "invalid_input",
            "path": "",
            "message": "x" * 257,
        },
    ],
)
def test_malformed_error_mappings_fail_with_structured_current_version_errors(record):
    with pytest.raises(ProgramValidationError) as caught:
        ProgramValidationError.from_mapping(record)

    assert caught.value.code is ProgramErrorCode.INVALID_ERROR_RECORD
    assert caught.value.schema_version == SCHEMA_VERSION
    assert "forged" not in str(caught.value).lower()


def test_unsupported_error_mapping_version_has_a_stable_code():
    with pytest.raises(ProgramValidationError) as caught:
        ProgramValidationError.from_mapping(
            {"schema_version": 2, "code": "invalid_input", "path": "", "message": "message"}
        )

    assert caught.value.code is ProgramErrorCode.UNSUPPORTED_VERSION
    assert caught.value.path == "/schema_version"


@pytest.mark.parametrize(
    "mapping", [_HostileMapping(fail_iteration=True), _HostileMapping(fail_iteration=False)]
)
def test_hostile_error_mappings_are_normalized_without_raw_exception_text(mapping):
    with pytest.raises(ProgramValidationError) as caught:
        ProgramValidationError.from_mapping(mapping)

    assert caught.value.code is ProgramErrorCode.INVALID_ERROR_RECORD
    assert "hostile" not in str(caught.value)


def test_validation_resolves_no_handler_and_performs_no_runtime_import(monkeypatch):
    handler_calls = 0

    def handler_spy(*args, **kwargs):
        nonlocal handler_calls
        handler_calls += 1

    assert callable(handler_spy)
    imported: list[str] = []
    original_import = builtins.__import__

    def import_spy(name, globals=None, locals=None, fromlist=(), level=0):
        imported.append(name)
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_spy)
    registry = _custom_registry(handler_name="handler_spy")
    validated = validate_model_program(
        _program(_command("toggle", "toggle_visibility", args={"value": True})),
        registry=registry,
    )

    assert handler_calls == 0
    assert validated.commands[0].handler_name == "handler_spy"
    forbidden = ("FreeCAD", "Part", "mcp", "anthropic", "openai", "vibecad.tools")
    assert not any(
        name == prefix or name.startswith(f"{prefix}.") for name in imported for prefix in forbidden
    )


def test_default_registry_is_not_mutated_by_validation():
    before = tuple(DEFAULT_OPERATION_REGISTRY.operations.items())

    validate_model_program(_program(_command("inspect")))

    assert tuple(DEFAULT_OPERATION_REGISTRY.operations.items()) == before
