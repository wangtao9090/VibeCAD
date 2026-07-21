"""Safety and metadata tests for the semantic execution registry."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Iterator, Mapping

import pytest

import vibecad.execution.registry as registry_module
from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    ExecutionProfile,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    RegistryError,
    RegistryErrorCode,
    ResourceBudget,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
)


class _HostileIterable:
    def __init__(self, exception: BaseException) -> None:
        self.exception = exception

    def __iter__(self):
        raise self.exception


class _HostileMapping(Mapping):
    def __init__(self, exception: BaseException, *, fail_during_iteration: bool) -> None:
        self.exception = exception
        self.fail_during_iteration = fail_during_iteration

    def __getitem__(self, key):
        raise self.exception

    def __iter__(self) -> Iterator[str]:
        if self.fail_during_iteration:
            raise self.exception
        return iter(("schema_version", "code", "operation", "field", "message"))

    def __len__(self) -> int:
        return 5


def _fields(fields: tuple[FieldMetadata, ...]):
    return tuple(
        (field.name, field.handler_parameter, field.value_shape, field.required) for field in fields
    )


def _operation(
    operation: str = "create_sphere",
    *,
    handler_name: str = "add_sphere",
    target_fields: tuple[FieldMetadata, ...] = (),
    argument_fields: tuple[FieldMetadata, ...] = (),
    result_slots: tuple[ResultSlotMetadata, ...] = (),
) -> OperationMetadata:
    return OperationMetadata(
        operation=operation,
        handler_name=handler_name,
        risk_class=RiskClass.MUTATING,
        evidence_required=True,
        target_fields=target_fields,
        argument_fields=argument_fields,
        result_slots=result_slots,
    )


def test_default_registry_exposes_only_the_stage3_s3_1_operations():
    assert tuple(DEFAULT_OPERATION_REGISTRY) == (
        "create_box",
        "modify_parameter",
        "inspect_model",
    )
    assert len(DEFAULT_OPERATION_REGISTRY) == 3


def test_stage3_registry_removes_document_lifecycle_and_declares_execution_contracts():
    assert tuple(DEFAULT_OPERATION_REGISTRY) == (
        "create_box",
        "modify_parameter",
        "inspect_model",
    )

    create_box = DEFAULT_OPERATION_REGISTRY.lookup("create_box")
    assert create_box.execution_profiles == (registry_module.ExecutionProfile.HEADLESS,)
    assert create_box.direct_exposed is True
    assert tuple(slot.name for slot in create_box.result_slots) == ("object",)
    assert create_box.result_slots[0].result_field == "name"

    for operation in DEFAULT_OPERATION_REGISTRY.operations.values():
        assert operation.execution_profiles == (ExecutionProfile.HEADLESS,)
        assert operation.minimum_freecad_version == (1, 0)
        assert operation.maximum_freecad_version_exclusive == (2, 0)
        assert operation.requires_gui_main_thread is False
        assert type(operation.resource_budget) is ResourceBudget
        assert operation.direct_exposed is True

    modify = DEFAULT_OPERATION_REGISTRY.lookup("modify_parameter")
    assert modify.target_fields[0].value_shape is ValueShape.RESULT_REF
    assert modify.target_fields[0].referenced_value_shape is ValueShape.NONBLANK_STRING
    assert DEFAULT_OPERATION_REGISTRY.lookup("inspect_model").result_slots == ()

    with pytest.raises(RegistryError) as caught:
        DEFAULT_OPERATION_REGISTRY.lookup("create_document")
    assert caught.value.code is RegistryErrorCode.UNKNOWN_OPERATION


def test_stage3_value_shapes_and_execution_profiles_are_closed():
    assert {item.value for item in ExecutionProfile} == {
        "headless",
        "offscreen_gui",
        "interactive_gui",
    }
    assert {item.value for item in ValueShape} == {
        "nonblank_string",
        "boolean",
        "integer",
        "finite_number",
        "positive_number",
        "enum",
        "vector2",
        "vector3",
        "quantity",
        "result_ref",
        "object_selector",
    }


@pytest.mark.parametrize(
    "profiles",
    [(), (ExecutionProfile.HEADLESS, ExecutionProfile.HEADLESS), ("headless",)],
)
def test_operation_profiles_are_nonempty_unique_and_typed(profiles):
    with pytest.raises(RegistryError) as caught:
        OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            execution_profiles=profiles,
        )

    assert caught.value.code is RegistryErrorCode.INVALID_METADATA


def test_headless_profile_cannot_claim_a_gui_main_thread_requirement():
    with pytest.raises(RegistryError) as caught:
        OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            execution_profiles=(
                ExecutionProfile.HEADLESS,
                ExecutionProfile.INTERACTIVE_GUI,
            ),
            requires_gui_main_thread=True,
        )

    assert caught.value.code is RegistryErrorCode.INVALID_METADATA


@pytest.mark.parametrize(
    "budget",
    [
        ResourceBudget(max_runtime_ms=1, max_created_objects=0, max_result_bytes=1),
        object(),
    ],
)
def test_operation_resource_budget_is_typed(budget):
    if type(budget) is ResourceBudget:
        operation = OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            resource_budget=budget,
        )
        assert operation.resource_budget is budget
        return
    with pytest.raises(RegistryError) as caught:
        OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            resource_budget=budget,
        )
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_runtime_ms": 0},
        {"max_created_objects": -1},
        {"max_result_bytes": True},
    ],
)
def test_resource_budget_rejects_unbounded_or_untyped_values(kwargs):
    with pytest.raises(RegistryError) as caught:
        ResourceBudget(**kwargs)
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum_freecad_version": (2, 0), "maximum_freecad_version_exclusive": (2, 0)},
        {"minimum_freecad_version": (1,)},
        {"maximum_freecad_version_exclusive": (2, True)},
    ],
)
def test_freecad_version_range_is_bounded_and_nonempty(kwargs):
    with pytest.raises(RegistryError) as caught:
        OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            **kwargs,
        )
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA


def test_result_slot_and_result_ref_metadata_fail_closed():
    slot = ResultSlotMetadata("object", "name", ValueShape.NONBLANK_STRING)
    operation = _operation(result_slots=(slot,))
    assert operation.result_slots == (slot,)

    with pytest.raises(RegistryError) as caught:
        FieldMetadata("object", "name", ValueShape.RESULT_REF)
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    with pytest.raises(RegistryError) as caught:
        ResultSlotMetadata("object", "name", ValueShape.RESULT_REF)
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    with pytest.raises(RegistryError) as caught:
        _operation(result_slots=(slot, slot))
    assert caught.value.code is RegistryErrorCode.DUPLICATE_FIELD


def test_default_registry_has_exact_handler_risk_and_evidence_metadata():
    expected = {
        "create_box": ("add_box", RiskClass.MUTATING, True),
        "modify_parameter": ("modify_part", RiskClass.MUTATING, True),
        "inspect_model": ("describe_part", RiskClass.READ_ONLY, False),
    }

    actual = {
        name: (
            DEFAULT_OPERATION_REGISTRY.lookup(name).handler_name,
            DEFAULT_OPERATION_REGISTRY.lookup(name).risk_class,
            DEFAULT_OPERATION_REGISTRY.lookup(name).evidence_required,
        )
        for name in DEFAULT_OPERATION_REGISTRY
    }

    assert actual == expected


def test_default_registry_has_exact_field_shapes_and_bindings():
    create_box = DEFAULT_OPERATION_REGISTRY.lookup("create_box")
    assert create_box.target_fields == ()
    assert _fields(create_box.argument_fields) == (
        ("length", "length", ValueShape.POSITIVE_NUMBER, True),
        ("width", "width", ValueShape.POSITIVE_NUMBER, True),
        ("height", "height", ValueShape.POSITIVE_NUMBER, True),
        ("position", "position", ValueShape.VECTOR3, False),
    )

    modify_parameter = DEFAULT_OPERATION_REGISTRY.lookup("modify_parameter")
    assert _fields(modify_parameter.target_fields) == (
        ("object", "name", ValueShape.RESULT_REF, True),
    )
    assert (
        modify_parameter.target_fields[0].referenced_value_shape
        is ValueShape.NONBLANK_STRING
    )
    assert _fields(modify_parameter.argument_fields) == (
        ("parameter", "parameter", ValueShape.NONBLANK_STRING, True),
        ("value", "value", ValueShape.POSITIVE_NUMBER, True),
    )

    inspect_model = DEFAULT_OPERATION_REGISTRY.lookup("inspect_model")
    assert inspect_model.target_fields == ()
    assert inspect_model.argument_fields == ()


def test_registry_and_nested_metadata_are_immutable():
    operation = DEFAULT_OPERATION_REGISTRY.lookup("create_box")

    with pytest.raises(dataclasses.FrozenInstanceError):
        operation.handler_name = "other_handler"
    with pytest.raises(dataclasses.FrozenInstanceError):
        operation.argument_fields[0].required = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_OPERATION_REGISTRY._operations = {}
    with pytest.raises(TypeError):
        DEFAULT_OPERATION_REGISTRY.operations["other"] = operation


def test_operation_metadata_freezes_caller_owned_field_collections():
    fields = [FieldMetadata("radius", "radius", ValueShape.POSITIVE_NUMBER)]
    operation = OperationMetadata(
        operation="create_sphere",
        handler_name="add_sphere",
        risk_class=RiskClass.MUTATING,
        evidence_required=True,
        argument_fields=fields,  # type: ignore[arg-type]
    )
    fields.clear()

    assert tuple(field.name for field in operation.argument_fields) == ("radius",)


@pytest.mark.parametrize(
    "name",
    ["", "   ", "CreateBox", "create-box", "create__box", "_create_box", "create_box_"],
)
def test_names_must_be_nonblank_snake_case(name):
    with pytest.raises(RegistryError) as caught:
        _operation(operation=name)

    assert caught.value.code is RegistryErrorCode.INVALID_NAME


def test_handler_and_field_names_are_validated_too():
    with pytest.raises(RegistryError) as caught:
        _operation(handler_name="AddSphere")
    assert caught.value.code is RegistryErrorCode.INVALID_NAME

    with pytest.raises(RegistryError) as caught:
        FieldMetadata("radius-mm", "radius", ValueShape.POSITIVE_NUMBER)
    assert caught.value.code is RegistryErrorCode.INVALID_NAME

    with pytest.raises(RegistryError) as caught:
        FieldMetadata("radius", "radius-mm", ValueShape.POSITIVE_NUMBER)
    assert caught.value.code is RegistryErrorCode.INVALID_NAME


@pytest.mark.parametrize(
    "operation",
    [
        "run_python",
        "execute_code",
        "generate_script",
        "shell_command",
        "import_source",
    ],
)
def test_arbitrary_code_operation_tokens_are_rejected(operation):
    with pytest.raises(RegistryError) as caught:
        _operation(operation=operation)

    assert caught.value.code is RegistryErrorCode.UNSAFE_NAME
    assert caught.value.operation == operation


@pytest.mark.parametrize(
    "operation",
    [
        "run_bash",
        "run_freecad_macro",
        "spawn_process",
        "invoke_powershell",
        "open_pwsh",
        "run_zsh",
        "fork_worker",
        "run_osascript",
        "run_wscript",
        "run_ruby",
        "run_perl",
        "run_lua",
    ],
)
def test_shell_interpreter_process_and_macro_aliases_are_rejected(operation):
    with pytest.raises(RegistryError) as caught:
        _operation(operation=operation)

    assert caught.value.code is RegistryErrorCode.UNSAFE_NAME
    assert caught.value.operation == operation


def test_unsafe_tokens_are_matched_as_tokens_not_substrings():
    allowed = (
        _operation(operation="create_keyway", handler_name="add_keyway"),
        _operation(operation="create_shelling", handler_name="add_shelling"),
        _operation(operation="inspect_process", handler_name="describe_process"),
        _operation(operation="create_forklift", handler_name="add_forklift"),
        _operation(operation="respawn_feature", handler_name="add_respawn_feature"),
    )

    assert tuple(operation.operation for operation in allowed) == (
        "create_keyway",
        "create_shelling",
        "inspect_process",
        "create_forklift",
        "respawn_feature",
    )


def test_legitimate_semantic_file_and_source_names_are_not_overblocked():
    operation = _operation(
        operation="inspect_source_file",
        handler_name="describe_source_path",
        argument_fields=(FieldMetadata("source_path", "source_path", ValueShape.NONBLANK_STRING),),
    )

    assert operation.operation == "inspect_source_file"


def test_unsafe_handler_and_field_metadata_are_rejected():
    with pytest.raises(RegistryError) as caught:
        _operation(handler_name="run_shell")
    assert caught.value.code is RegistryErrorCode.UNSAFE_NAME

    with pytest.raises(RegistryError) as caught:
        FieldMetadata("source_code", "payload", ValueShape.NONBLANK_STRING)
    assert caught.value.code is RegistryErrorCode.UNSAFE_NAME


def test_duplicate_operations_are_rejected_with_a_stable_code():
    operation = _operation()

    with pytest.raises(RegistryError) as caught:
        OperationRegistry((operation, operation))

    assert caught.value.code is RegistryErrorCode.DUPLICATE_OPERATION
    assert caught.value.operation == "create_sphere"
    assert caught.value.to_mapping()["code"] == "duplicate_operation"


def test_registry_errors_round_trip_through_a_strict_versioned_record():
    error = RegistryError(
        RegistryErrorCode.DUPLICATE_FIELD,
        "program field is bound more than once",
        operation="modify_parameter",
        field="value",
    )

    encoded = error.to_mapping()

    assert encoded["schema_version"] == 1
    restored = RegistryError.from_mapping(encoded)
    assert restored.to_mapping() == encoded


@pytest.mark.parametrize(
    ("hostile_character", "split_line_count"),
    [
        ("\u007f", 1),
        ("\u0085", 2),
        ("\u2028", 2),
        ("\u2029", 2),
    ],
    ids=("del", "next-line", "line-separator", "paragraph-separator"),
)
def test_registry_error_message_boundary_rejects_direct_nonprintable_characters(
    hostile_character,
    split_line_count,
):
    hostile_message = f"safe-prefix{hostile_character}forged-line"
    assert not hostile_message.isprintable()
    assert len(hostile_message.splitlines()) == split_line_count

    with pytest.raises(ValueError) as caught:
        RegistryError(RegistryErrorCode.INVALID_METADATA, hostile_message)

    assert str(caught.value) == "message must be bounded printable single-line text"
    assert hostile_character not in str(caught.value)


@pytest.mark.parametrize(
    "hostile_character",
    ["\u007f", "\u0085", "\u2028", "\u2029"],
    ids=("del", "next-line", "line-separator", "paragraph-separator"),
)
def test_registry_error_message_boundary_rejects_nonprintable_parser_records(
    hostile_character,
):
    encoded = RegistryError(
        RegistryErrorCode.UNKNOWN_OPERATION,
        "operation is not registered",
        operation="create_sphere",
    ).to_mapping()
    encoded["message"] = f"safe-prefix{hostile_character}forged-line"

    with pytest.raises(RegistryError) as caught:
        RegistryError.from_mapping(encoded)

    assert caught.value.code is RegistryErrorCode.INVALID_ERROR_RECORD
    assert hostile_character not in str(caught.value)
    assert hostile_character not in str(caught.value.to_mapping())


def test_registry_error_message_boundary_preserves_printable_unicode_round_trip():
    message = "尺寸验证通过 — café ✅"
    assert message.isprintable()

    error = RegistryError(RegistryErrorCode.INVALID_METADATA, message)

    assert str(error).splitlines() == [f"execution registry error (invalid_metadata): {message}"]
    assert RegistryError.from_mapping(error.to_mapping()).message == message


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (lambda value: value.update(schema_version=2), RegistryErrorCode.UNSUPPORTED_VERSION),
        (lambda value: value.update(schema_version=True), RegistryErrorCode.INVALID_ERROR_RECORD),
        (lambda value: value.update(code="future_code"), RegistryErrorCode.INVALID_ERROR_RECORD),
        (lambda value: value.update(extra=True), RegistryErrorCode.INVALID_ERROR_RECORD),
        (lambda value: value.pop("message"), RegistryErrorCode.INVALID_ERROR_RECORD),
        (
            lambda value: value.update(operation="bad\ncontext"),
            RegistryErrorCode.INVALID_ERROR_RECORD,
        ),
        (
            lambda value: value.update(message="bad\nmessage"),
            RegistryErrorCode.INVALID_ERROR_RECORD,
        ),
    ],
)
def test_registry_error_record_parser_fails_closed(mutate, expected_code):
    encoded = RegistryError(
        RegistryErrorCode.UNKNOWN_OPERATION,
        "operation is not registered",
        operation="create_sphere",
    ).to_mapping()
    mutate(encoded)

    with pytest.raises(RegistryError) as caught:
        RegistryError.from_mapping(encoded)

    assert caught.value.code is expected_code
    assert caught.value.to_mapping()["schema_version"] == 1


@pytest.mark.parametrize("fail_during_iteration", [True, False])
def test_registry_error_parser_normalizes_hostile_mapping_exceptions(fail_during_iteration):
    mapping = _HostileMapping(
        RuntimeError("private hostile mapping detail"),
        fail_during_iteration=fail_during_iteration,
    )

    with pytest.raises(RegistryError) as caught:
        RegistryError.from_mapping(mapping)

    assert caught.value.code is RegistryErrorCode.INVALID_ERROR_RECORD
    assert "private hostile mapping detail" not in str(caught.value)
    assert len(str(caught.value.to_mapping())) < 512


def test_registry_error_parser_preserves_a_structured_mapping_failure():
    original = RegistryError(
        RegistryErrorCode.INVALID_ERROR_RECORD,
        "prestructured mapping failure",
    )
    mapping = _HostileMapping(original, fail_during_iteration=True)

    with pytest.raises(RegistryError) as caught:
        RegistryError.from_mapping(mapping)

    assert caught.value is original


def test_duplicate_program_fields_are_rejected_across_target_and_arguments():
    target = FieldMetadata("object", "name", ValueShape.NONBLANK_STRING)
    argument = FieldMetadata("object", "object_name", ValueShape.NONBLANK_STRING)

    with pytest.raises(RegistryError) as caught:
        _operation(target_fields=(target,), argument_fields=(argument,))

    assert caught.value.code is RegistryErrorCode.DUPLICATE_FIELD
    assert caught.value.field == "object"


def test_duplicate_handler_parameter_bindings_are_rejected():
    target = FieldMetadata("object", "name", ValueShape.NONBLANK_STRING)
    argument = FieldMetadata("label", "name", ValueShape.NONBLANK_STRING)

    with pytest.raises(RegistryError) as caught:
        _operation(target_fields=(target,), argument_fields=(argument,))

    assert caught.value.code is RegistryErrorCode.DUPLICATE_BINDING
    assert caught.value.field == "name"


def test_invalid_field_and_operation_metadata_fail_closed():
    with pytest.raises(RegistryError) as caught:
        FieldMetadata("radius", "radius", "number")  # type: ignore[arg-type]
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    with pytest.raises(RegistryError) as caught:
        FieldMetadata("radius", "radius", ValueShape.POSITIVE_NUMBER, required=1)  # type: ignore[arg-type]
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    with pytest.raises(RegistryError) as caught:
        _operation(argument_fields=(object(),))  # type: ignore[arg-type]
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    with pytest.raises(RegistryError) as caught:
        OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class="mutating",  # type: ignore[arg-type]
            evidence_required=True,
        )
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    with pytest.raises(RegistryError) as caught:
        OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class=RiskClass.MUTATING,
            evidence_required=1,  # type: ignore[arg-type]
        )
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA


def test_hostile_field_and_registry_iterators_are_normalized():
    with pytest.raises(RegistryError) as field_error:
        _operation(argument_fields=_HostileIterable(RuntimeError("private field detail")))  # type: ignore[arg-type]
    assert field_error.value.code is RegistryErrorCode.INVALID_METADATA
    assert "private field detail" not in str(field_error.value)

    with pytest.raises(RegistryError) as registry_error:
        OperationRegistry(_HostileIterable(RuntimeError("private registry detail")))  # type: ignore[arg-type]
    assert registry_error.value.code is RegistryErrorCode.INVALID_METADATA
    assert "private registry detail" not in str(registry_error.value)


def test_hostile_iterators_preserve_registry_errors_and_do_not_catch_base_exceptions():
    original = RegistryError(
        RegistryErrorCode.INVALID_METADATA,
        "prestructured iterable failure",
    )
    with pytest.raises(RegistryError) as caught:
        OperationRegistry(_HostileIterable(original))  # type: ignore[arg-type]
    assert caught.value is original

    with pytest.raises(KeyboardInterrupt):
        OperationRegistry(_HostileIterable(KeyboardInterrupt()))  # type: ignore[arg-type]


def test_unknown_lookup_fails_with_a_stable_machine_readable_error():
    with pytest.raises(RegistryError) as caught:
        DEFAULT_OPERATION_REGISTRY.lookup("create_sphere")

    assert caught.value.code is RegistryErrorCode.UNKNOWN_OPERATION
    assert caught.value.operation == "create_sphere"
    assert caught.value.to_mapping() == {
        "schema_version": 1,
        "code": "unknown_operation",
        "operation": "create_sphere",
        "field": None,
        "message": "operation is not registered",
    }


@pytest.mark.parametrize("operation", [None, True, 7, [], {}, "bad\nname", "x" * 1000])
def test_adversarial_lookup_names_return_bounded_structured_errors(operation):
    with pytest.raises(RegistryError) as caught:
        DEFAULT_OPERATION_REGISTRY.lookup(operation)  # type: ignore[arg-type]

    assert caught.value.code is RegistryErrorCode.INVALID_NAME
    assert caught.value.operation is None
    assert "bad\nname" not in str(caught.value)
    encoded = caught.value.to_mapping()
    assert encoded["schema_version"] == 1
    assert len(str(encoded)) < 512


def test_unsafe_lookup_returns_a_structured_error_without_execution():
    with pytest.raises(RegistryError) as caught:
        DEFAULT_OPERATION_REGISTRY.lookup("run_python")

    assert caught.value.code is RegistryErrorCode.UNSAFE_NAME
    assert caught.value.operation == "run_python"
    assert (
        RegistryError.from_mapping(caught.value.to_mapping()).code is RegistryErrorCode.UNSAFE_NAME
    )


def test_registry_contains_metadata_only_not_execution_hooks():
    assert {field.name for field in dataclasses.fields(OperationMetadata)} == {
        "operation",
        "handler_name",
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
    }
    assert all(
        not callable(value)
        for metadata in DEFAULT_OPERATION_REGISTRY.operations.values()
        for value in dataclasses.astuple(metadata)
    )


def test_execution_registry_imports_without_cad_mcp_or_model_sdks():
    code = """
import sys
import vibecad.execution
banned = {'FreeCAD', 'Part', 'mcp', 'anthropic', 'openai'}
loaded = sorted(name for name in banned if name in sys.modules)
assert not loaded, loaded
print('execution registry import boundary OK')
"""

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "execution registry import boundary OK" in result.stdout
