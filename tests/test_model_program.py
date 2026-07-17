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


def test_every_default_operation_is_bound_for_execution_without_invocation():
    program = _program(
        _command("document", "create_document", args={"name": "Bracket"}),
        _command(
            "box",
            "create_box",
            args={"length": 10, "width": 5.5, "height": 2, "position": [-1, 0, 3]},
            depends_on=("document",),
        ),
        _command(
            "modify",
            "modify_parameter",
            target={"object": "Box"},
            args={"parameter": "Length", "value": 12},
            depends_on=("box",),
            preserve=("width", "height"),
        ),
        _command("inspect", depends_on=("modify",)),
    )

    validated = validate_model_program(program)

    assert validated.program is program
    assert [command.id for command in validated.commands] == [
        "document",
        "box",
        "modify",
        "inspect",
    ]
    assert [command.handler_name for command in validated] == [
        "new_document",
        "add_box",
        "modify_part",
        "describe_part",
    ]
    assert dict(validated.commands[0].handler_kwargs) == {"name": "Bracket"}
    assert dict(validated.commands[1].handler_kwargs) == {
        "length": 10,
        "width": 5.5,
        "height": 2,
        "position": (-1, 0, 3),
    }
    assert dict(validated.commands[2].handler_kwargs) == {
        "name": "Box",
        "parameter": "Length",
        "value": 12,
    }
    assert dict(validated.commands[3].handler_kwargs) == {}
    assert validated.commands[2].preserve == ("width", "height")
    assert validated.commands[3].risk_class is RiskClass.READ_ONLY
    assert validated.commands[3].evidence_required is False


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
                args={"parameter": "Length", "value": 10},
            )
        ),
        ProgramErrorCode.MISSING_FIELD,
        "/operations/0/target/object",
    )
    _error(
        _program(_command("box", "create_box", args={"width": 2, "height": 3})),
        ProgramErrorCode.MISSING_FIELD,
        "/operations/0/args/length",
    )


def test_extra_fields_are_sorted_and_use_escaped_exact_paths():
    _error(
        _program(
            _command(
                "modify",
                "modify_parameter",
                target={"object": "Box"},
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
    )


def test_unit_is_rejected_as_an_extra_field_under_r_b10():
    _error(
        _program(
            _command(
                "modify",
                "modify_parameter",
                target={"object": "Box"},
                args={"parameter": "Length", "value": 10, "unit": "mm"},
            )
        ),
        ProgramErrorCode.EXTRA_FIELD,
        "/operations/0/args/unit",
    )


def test_hostile_extra_field_uses_safe_group_path_and_is_not_reflected():
    hostile = "unknown\nFORGED"
    error = _error(
        _program(_command("document", "create_document", args={"name": "Part", hostile: 1})),
        ProgramErrorCode.EXTRA_FIELD,
        "/operations/0/args",
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
        _program(_command("document", "create_document", args={"name": value})),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/name",
    )


@pytest.mark.parametrize("value", [0, -1, True, False, "10", None])
def test_positive_number_shape_excludes_non_positive_boolean_and_other_types(value):
    _error(
        _program(
            _command(
                "modify",
                "modify_parameter",
                target={"object": "Box"},
                args={"parameter": "Length", "value": value},
            )
        ),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/value",
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_positive_numbers_are_rejected_even_for_forged_contracts(value):
    command = _command(
        "modify",
        "modify_parameter",
        target={"object": "Box"},
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
                args={"length": 1, "width": 2, "height": 3, "position": position},
            )
        ),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/position",
    )


def test_vector3_accepts_zero_and_negative_finite_coordinates_and_is_optional():
    with_position = validate_model_program(
        _program(
            _command(
                "box",
                "create_box",
                args={"length": 1, "width": 2, "height": 3, "position": (-1, 0, 2.5)},
            )
        )
    )
    without_position = validate_model_program(
        _program(
            _command(
                "box",
                "create_box",
                args={"length": 1, "width": 2, "height": 3},
            )
        )
    )

    assert with_position.commands[0].handler_kwargs["position"] == (-1, 0, 2.5)
    assert "position" not in without_position.commands[0].handler_kwargs


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


def test_wrong_shape_errors_do_not_reflect_hostile_field_values():
    hostile = "secret\nFORGED LOG LINE"
    error = _error(
        _program(_command("document", "create_document", args={"name": {hostile: 1}})),
        ProgramErrorCode.INVALID_VALUE_SHAPE,
        "/operations/0/args/name",
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
