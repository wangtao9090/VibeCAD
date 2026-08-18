"""Safety and metadata tests for the semantic execution registry."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Iterator, Mapping

import pytest

import vibecad.execution.registry as registry_module
from vibecad.application.public_surface import public_tool_specs
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

_STABLE_PUBLIC_TOOL_NAMES = (
    "ping",
    "get_runtime_status",
    "ensure_runtime",
    "uninstall_runtime",
    "get_capabilities",
    "query_freecad_runtime_capabilities",
    "create_project",
    "get_project",
    "list_projects",
    "list_revisions",
    "compare_revisions",
    "revert_project",
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_events",
    "submit_model_program",
    "resume_task",
    "cancel_task",
    "accept_draft",
    "reject_draft",
    "get_artifact_manifest",
    "export_task_artifacts",
    "create_release",
    "get_release",
    "approve_release",
    "create_reconstruction",
    "get_reconstruction",
    "run_reconstruction",
    "answer_reconstruction",
    "adopt_reconstruction",
    "reject_reconstruction",
    "delete_reconstruction",
)

_DEFAULT_DIRECT_DESCRIPTIONS = {
    "create_box": "向任务提交一个长方体直接操作",
    "create_cylinder": "向任务提交一个圆柱体直接操作",
    "inspect_model": "检查指定任务版本的模型事实",
    "modify_parameter": "按显式验收条件修改选定对象参数",
    "move_part": "按显式验收条件移动选定对象",
    "rotate_part": "按显式验收条件旋转选定对象",
}


class _DescriptionSubclass(str):
    pass


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


def test_default_registry_exposes_six_direct_and_twelve_private_operations():
    assert tuple(DEFAULT_OPERATION_REGISTRY) == (
        "create_box",
        "create_cylinder",
        "modify_parameter",
        "move_part",
        "rotate_part",
        "inspect_model",
        "create_cone",
        "create_sphere",
        "create_torus",
        "boolean_cut",
        "boolean_fuse",
        "boolean_common",
        "create_component",
        "set_component_bom",
        "place_component",
        "create_parametric_design",
        "modify_parametric_parameter",
        "apply_reviewed_intent",
    )
    assert len(DEFAULT_OPERATION_REGISTRY) == 18
    assert all(
        metadata.handler_name == operation
        for operation, metadata in DEFAULT_OPERATION_REGISTRY.operations.items()
    )


@pytest.mark.parametrize("stable_name", _STABLE_PUBLIC_TOOL_NAMES)
def test_direct_operation_cannot_collide_with_stable_public_namespace(stable_name):
    collision = dataclasses.replace(
        DEFAULT_OPERATION_REGISTRY.lookup("create_box"),
        operation=stable_name,
    )

    with pytest.raises(TypeError) as caught:
        public_tool_specs(OperationRegistry((collision,)))

    assert str(caught.value) == "registry public metadata is invalid"


def test_default_direct_descriptions_are_exact_and_projected_unchanged():
    metadata_descriptions = {
        name: DEFAULT_OPERATION_REGISTRY.lookup(name).description
        for name in DEFAULT_OPERATION_REGISTRY
        if DEFAULT_OPERATION_REGISTRY.lookup(name).direct_exposed
    }
    projected_descriptions = {
        spec.name: spec.description
        for spec in public_tool_specs()
        if spec.name in _DEFAULT_DIRECT_DESCRIPTIONS
    }

    assert metadata_descriptions == _DEFAULT_DIRECT_DESCRIPTIONS
    assert projected_descriptions == _DEFAULT_DIRECT_DESCRIPTIONS


def test_every_public_description_is_exact_bounded_printable_single_line_text():
    specs = public_tool_specs()

    assert all(type(spec.description) is str for spec in specs)
    assert all(spec.description.strip() for spec in specs)
    assert all(spec.description.isprintable() for spec in specs)
    assert all(len(spec.description.splitlines()) == 1 for spec in specs)
    assert all(len(spec.description.encode("utf-8")) <= 256 for spec in specs)


def test_custom_direct_fixture_gets_a_deterministic_safe_description_fallback():
    custom = dataclasses.replace(_operation(), direct_exposed=True)
    first = public_tool_specs(OperationRegistry((custom,)))[-1].description
    second = public_tool_specs(OperationRegistry((custom,)))[-1].description

    assert first == second
    assert type(first) is str
    assert first.strip() == first
    assert first.isprintable()
    assert len(first.splitlines()) == 1
    assert len(first.encode("utf-8")) <= 256
    assert custom.operation in first


@pytest.mark.parametrize(
    "description",
    (
        "",
        "   ",
        "forged\nsecond line",
        "forged\tdescription",
        "x" * 257,
        "界" * 86,
        _DescriptionSubclass("seemingly safe"),
    ),
)
def test_explicit_direct_description_rejects_invalid_or_subclass_text(description):
    with pytest.raises(RegistryError) as caught:
        OperationMetadata(
            operation="create_sphere",
            handler_name="add_sphere",
            risk_class=RiskClass.MUTATING,
            evidence_required=True,
            direct_exposed=True,
            description=description,
        )

    assert caught.value.code is RegistryErrorCode.INVALID_METADATA


def test_public_tool_names_are_unique_across_stable_and_direct_surfaces():
    names = tuple(spec.name for spec in public_tool_specs())

    assert names[: len(_STABLE_PUBLIC_TOOL_NAMES)] == _STABLE_PUBLIC_TOOL_NAMES
    assert len(names) == len(set(names)) == 39


def test_stage3_registry_removes_document_lifecycle_and_declares_execution_contracts():
    assert tuple(DEFAULT_OPERATION_REGISTRY) == (
        "create_box",
        "create_cylinder",
        "modify_parameter",
        "move_part",
        "rotate_part",
        "inspect_model",
        "create_cone",
        "create_sphere",
        "create_torus",
        "boolean_cut",
        "boolean_fuse",
        "boolean_common",
        "create_component",
        "set_component_bom",
        "place_component",
        "create_parametric_design",
        "modify_parametric_parameter",
        "apply_reviewed_intent",
    )

    create_box = DEFAULT_OPERATION_REGISTRY.lookup("create_box")
    assert create_box.execution_profiles == (registry_module.ExecutionProfile.HEADLESS,)
    assert create_box.direct_exposed is True
    assert tuple(slot.name for slot in create_box.result_slots) == ("object",)
    assert create_box.result_slots[0].result_field == "object_id"
    assert create_box.result_slots[0].value_shape is ValueShape.OBJECT_ID

    create_cylinder = DEFAULT_OPERATION_REGISTRY.lookup("create_cylinder")
    assert create_cylinder.result_slots == create_box.result_slots

    for operation in DEFAULT_OPERATION_REGISTRY.operations.values():
        assert operation.execution_profiles == (ExecutionProfile.HEADLESS,)
        assert operation.minimum_freecad_version == (1, 0)
        assert operation.maximum_freecad_version_exclusive == (2, 0)
        assert operation.requires_gui_main_thread is False
        assert type(operation.resource_budget) is ResourceBudget
    assert all(
        DEFAULT_OPERATION_REGISTRY.lookup(name).direct_exposed
        for name in tuple(DEFAULT_OPERATION_REGISTRY)[:6]
    )
    assert all(
        not DEFAULT_OPERATION_REGISTRY.lookup(name).direct_exposed
        for name in (
            "create_cone",
            "create_sphere",
            "create_torus",
            "boolean_cut",
            "boolean_fuse",
            "boolean_common",
            "create_component",
            "set_component_bom",
            "place_component",
            "create_parametric_design",
            "modify_parametric_parameter",
        )
    )

    modify = DEFAULT_OPERATION_REGISTRY.lookup("modify_parameter")
    assert modify.target_fields[0].value_shape is ValueShape.ENTITY_TARGET
    assert modify.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
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
        "result_ref_collection",
        "object_selector",
        "object_id",
        "entity_target",
        "angle_degrees",
        "parametric_design_ir",
        "reviewed_intent",
    }


def test_reviewed_intent_declares_one_bounded_ordered_source_collection() -> None:
    metadata = DEFAULT_OPERATION_REGISTRY.lookup("apply_reviewed_intent")
    fields = {item.name: item for item in metadata.argument_fields}

    assert set(fields) == {"intent", "source_a", "source_b", "sources"}
    assert fields["sources"] == FieldMetadata(
        "sources",
        "sources",
        ValueShape.RESULT_REF_COLLECTION,
        required=False,
        referenced_value_shape=ValueShape.OBJECT_ID,
    )
    assert registry_module._matches_value_shape((), ValueShape.RESULT_REF_COLLECTION)  # noqa: SLF001
    assert registry_module._matches_value_shape(  # noqa: SLF001
        tuple({"command_id": f"source_{index}", "slot": "object"} for index in range(8)),
        ValueShape.RESULT_REF_COLLECTION,
    )
    assert not registry_module._matches_value_shape(  # noqa: SLF001
        tuple({"command_id": f"source_{index}", "slot": "object"} for index in range(9)),
        ValueShape.RESULT_REF_COLLECTION,
    )


def test_component_selector_shape_accepts_only_explicit_app_part_objects():
    base = {
        "schema_version": 1,
        "project_id": "project_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "revision_id": "revision_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "entity_kind": "object",
        "object_id": "object_cccccccccccccccccccccccccccccccc",
        "feature_id": None,
        "object_type": "App::Part",
        "semantic_role": "part",
        "provenance": {"source": "model", "operation_id": "create-component"},
        "expected_cardinality": 1,
    }

    assert registry_module._matches_component_selector(base)
    assert not registry_module._matches_component_selector(
        {**base, "object_type": "Part::Box", "semantic_role": "primitive"}
    )
    assert not registry_module._matches_component_selector(
        {**base, "feature_id": "feature_dddddddddddddddddddddddddddddddd"}
    )


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
    slot = ResultSlotMetadata("object", "object_id", ValueShape.OBJECT_ID)
    operation = _operation(result_slots=(slot,))
    assert operation.result_slots == (slot,)

    with pytest.raises(RegistryError) as caught:
        FieldMetadata("object", "name", ValueShape.RESULT_REF)
    assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    for reference_shape in (
        ValueShape.RESULT_REF,
        ValueShape.RESULT_REF_COLLECTION,
    ):
        with pytest.raises(RegistryError) as caught:
            ResultSlotMetadata("object", "name", reference_shape)
        assert caught.value.code is RegistryErrorCode.INVALID_METADATA

    with pytest.raises(RegistryError) as caught:
        _operation(result_slots=(slot, slot))
    assert caught.value.code is RegistryErrorCode.DUPLICATE_FIELD


def test_default_registry_has_exact_handler_risk_and_evidence_metadata():
    expected = {
        "create_box": ("create_box", RiskClass.MUTATING, True),
        "create_cylinder": ("create_cylinder", RiskClass.MUTATING, True),
        "modify_parameter": ("modify_parameter", RiskClass.MUTATING, True),
        "move_part": ("move_part", RiskClass.MUTATING, True),
        "rotate_part": ("rotate_part", RiskClass.MUTATING, True),
        "inspect_model": ("inspect_model", RiskClass.READ_ONLY, False),
        "create_cone": ("create_cone", RiskClass.MUTATING, True),
        "create_sphere": ("create_sphere", RiskClass.MUTATING, True),
        "create_torus": ("create_torus", RiskClass.MUTATING, True),
        "boolean_cut": ("boolean_cut", RiskClass.MUTATING, True),
        "boolean_fuse": ("boolean_fuse", RiskClass.MUTATING, True),
        "boolean_common": ("boolean_common", RiskClass.MUTATING, True),
        "create_component": ("create_component", RiskClass.MUTATING, True),
        "set_component_bom": ("set_component_bom", RiskClass.MUTATING, True),
        "place_component": ("place_component", RiskClass.MUTATING, True),
        "create_parametric_design": (
            "create_parametric_design",
            RiskClass.MUTATING,
            True,
        ),
        "modify_parametric_parameter": (
            "modify_parametric_parameter",
            RiskClass.MUTATING,
            True,
        ),
        "apply_reviewed_intent": (
            "apply_reviewed_intent",
            RiskClass.MUTATING,
            True,
        ),
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
    assert _fields(create_box.target_fields) == (
        ("component", "component", ValueShape.ENTITY_TARGET, False),
    )
    assert create_box.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
    assert _fields(create_box.argument_fields) == (
        ("length_mm", "length", ValueShape.POSITIVE_NUMBER, True),
        ("width_mm", "width", ValueShape.POSITIVE_NUMBER, True),
        ("height_mm", "height", ValueShape.POSITIVE_NUMBER, True),
        ("position_mm", "position", ValueShape.VECTOR3, False),
    )

    create_cylinder = DEFAULT_OPERATION_REGISTRY.lookup("create_cylinder")
    assert _fields(create_cylinder.target_fields) == (
        ("component", "component", ValueShape.ENTITY_TARGET, False),
    )
    assert create_cylinder.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
    assert _fields(create_cylinder.argument_fields) == (
        ("radius_mm", "radius", ValueShape.POSITIVE_NUMBER, True),
        ("height_mm", "height", ValueShape.POSITIVE_NUMBER, True),
        ("position_mm", "position", ValueShape.VECTOR3, False),
        ("axis", "axis", ValueShape.ENUM, False),
    )
    assert create_cylinder.argument_fields[3].enum_values == ("x", "y", "z")

    create_cone = DEFAULT_OPERATION_REGISTRY.lookup("create_cone")
    assert _fields(create_cone.argument_fields) == (
        ("base_radius_mm", "radius1", ValueShape.POSITIVE_NUMBER, True),
        ("top_radius_mm", "radius2", ValueShape.POSITIVE_NUMBER, False),
        ("height_mm", "height", ValueShape.POSITIVE_NUMBER, True),
        ("position_mm", "position", ValueShape.VECTOR3, False),
        ("axis", "axis", ValueShape.ENUM, False),
    )
    assert create_cone.argument_fields[-1].enum_values == ("x", "y", "z")

    create_sphere = DEFAULT_OPERATION_REGISTRY.lookup("create_sphere")
    assert _fields(create_sphere.argument_fields) == (
        ("radius_mm", "radius", ValueShape.POSITIVE_NUMBER, True),
        ("position_mm", "position", ValueShape.VECTOR3, False),
    )

    create_torus = DEFAULT_OPERATION_REGISTRY.lookup("create_torus")
    assert _fields(create_torus.argument_fields) == (
        ("major_radius_mm", "radius1", ValueShape.POSITIVE_NUMBER, True),
        ("minor_radius_mm", "radius2", ValueShape.POSITIVE_NUMBER, True),
        ("position_mm", "position", ValueShape.VECTOR3, False),
        ("axis", "axis", ValueShape.ENUM, False),
    )
    assert create_torus.argument_fields[-1].enum_values == ("x", "y", "z")

    for operation in ("boolean_cut", "boolean_fuse", "boolean_common"):
        metadata = DEFAULT_OPERATION_REGISTRY.lookup(operation)
        assert _fields(metadata.target_fields) == (
            ("base", "base", ValueShape.ENTITY_TARGET, True),
            ("tool", "tool", ValueShape.ENTITY_TARGET, True),
        )
        assert all(
            field.referenced_value_shape is ValueShape.OBJECT_ID for field in metadata.target_fields
        )
        assert metadata.argument_fields == ()
        assert metadata.direct_exposed is False
        assert tuple(slot.name for slot in metadata.result_slots) == ("object",)

    modify_parameter = DEFAULT_OPERATION_REGISTRY.lookup("modify_parameter")
    assert _fields(modify_parameter.target_fields) == (
        ("object", "target", ValueShape.ENTITY_TARGET, True),
    )
    assert modify_parameter.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
    assert _fields(modify_parameter.argument_fields) == (
        ("parameter", "parameter", ValueShape.ENUM, True),
        ("value_mm", "value", ValueShape.POSITIVE_NUMBER, True),
    )
    assert modify_parameter.argument_fields[0].enum_values == (
        "base_radius",
        "height",
        "length",
        "major_radius",
        "minor_radius",
        "radius",
        "top_radius",
        "width",
    )

    move_part = DEFAULT_OPERATION_REGISTRY.lookup("move_part")
    assert _fields(move_part.target_fields) == (
        ("object", "target", ValueShape.ENTITY_TARGET, True),
    )
    assert move_part.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
    assert _fields(move_part.argument_fields) == (
        ("position_mm", "position", ValueShape.VECTOR3, True),
    )

    rotate_part = DEFAULT_OPERATION_REGISTRY.lookup("rotate_part")
    assert _fields(rotate_part.target_fields) == (
        ("object", "target", ValueShape.ENTITY_TARGET, True),
    )
    assert rotate_part.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
    assert _fields(rotate_part.argument_fields) == (
        ("axis", "axis", ValueShape.ENUM, True),
        ("angle_deg", "angle", ValueShape.ANGLE_DEGREES, True),
    )
    assert rotate_part.argument_fields[0].enum_values == ("x", "y", "z")

    inspect_model = DEFAULT_OPERATION_REGISTRY.lookup("inspect_model")
    assert inspect_model.target_fields == ()
    assert inspect_model.argument_fields == ()

    create_component = DEFAULT_OPERATION_REGISTRY.lookup("create_component")
    assert create_component.target_fields == ()
    assert _fields(create_component.argument_fields) == (
        ("name", "name", ValueShape.NONBLANK_STRING, True),
    )
    assert create_component.resource_budget.max_created_objects == 16
    assert create_component.result_slots[0].name == "component"

    set_component_bom = DEFAULT_OPERATION_REGISTRY.lookup("set_component_bom")
    assert _fields(set_component_bom.target_fields) == (
        ("component", "target", ValueShape.ENTITY_TARGET, True),
    )
    assert set_component_bom.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
    assert _fields(set_component_bom.argument_fields) == (
        ("part_number", "part_number", ValueShape.NONBLANK_STRING, True),
        ("description", "description", ValueShape.NONBLANK_STRING, True),
        ("material", "material", ValueShape.NONBLANK_STRING, True),
        ("density_kg_m3", "density", ValueShape.POSITIVE_NUMBER, True),
    )
    assert set_component_bom.resource_budget.max_created_objects == 0

    place_component = DEFAULT_OPERATION_REGISTRY.lookup("place_component")
    assert _fields(place_component.target_fields) == (
        ("component", "target", ValueShape.ENTITY_TARGET, True),
    )
    assert place_component.target_fields[0].referenced_value_shape is ValueShape.OBJECT_ID
    assert _fields(place_component.argument_fields) == (
        ("position_mm", "position", ValueShape.VECTOR3, True),
        ("rotation_axis", "rotation_axis", ValueShape.ENUM, True),
        ("angle_deg", "angle", ValueShape.FINITE_NUMBER, True),
    )

    create_parametric_design = DEFAULT_OPERATION_REGISTRY.lookup("create_parametric_design")
    assert create_parametric_design.target_fields == ()
    assert _fields(create_parametric_design.argument_fields) == (
        ("design", "design", ValueShape.PARAMETRIC_DESIGN_IR, True),
    )
    assert create_parametric_design.direct_exposed is False
    assert create_parametric_design.resource_budget.max_created_objects == 26
    assert tuple(slot.name for slot in create_parametric_design.result_slots) == ("body",)
    assert create_parametric_design.result_slots[0].result_field == "object_id"
    assert create_parametric_design.result_slots[0].value_shape is ValueShape.OBJECT_ID

    modify_parametric_parameter = DEFAULT_OPERATION_REGISTRY.lookup("modify_parametric_parameter")
    assert _fields(modify_parametric_parameter.target_fields) == (
        ("body", "target", ValueShape.OBJECT_SELECTOR, True),
    )
    assert modify_parametric_parameter.target_fields[0].referenced_value_shape is None
    assert _fields(modify_parametric_parameter.argument_fields) == (
        ("design", "design", ValueShape.PARAMETRIC_DESIGN_IR, True),
        ("parameter_id", "parameter_id", ValueShape.NONBLANK_STRING, True),
        ("value", "value", ValueShape.FINITE_NUMBER, True),
    )
    assert all(field.allowed_units == () for field in modify_parametric_parameter.argument_fields)
    assert modify_parametric_parameter.direct_exposed is False
    assert modify_parametric_parameter.preservation_fields == ()
    assert modify_parametric_parameter.resource_budget == ResourceBudget(
        max_runtime_ms=30_000,
        max_created_objects=0,
        max_result_bytes=65_536,
    )
    assert modify_parametric_parameter.result_slots == ()


def test_only_entity_mutators_declare_the_closed_preservation_vocabulary():
    expected = (
        "angle",
        "area_mm2",
        "base_radius",
        "bbox_mm",
        "center_of_mass_mm",
        "geometry",
        "height",
        "length",
        "major_radius",
        "minor_radius",
        "parameters",
        "placement",
        "radius",
        "solid_count",
        "top_radius",
        "valid_shape",
        "volume_mm3",
        "width",
    )

    for operation in ("modify_parameter", "move_part", "rotate_part"):
        assert DEFAULT_OPERATION_REGISTRY.lookup(operation).preservation_fields == expected
    for operation in ("create_box", "create_cylinder", "inspect_model"):
        assert DEFAULT_OPERATION_REGISTRY.lookup(operation).preservation_fields == ()


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


def test_operation_metadata_freezes_sorts_and_validates_preservation_fields():
    fields = ["width", "length"]
    operation = _operation()
    operation = OperationMetadata(
        operation=operation.operation,
        handler_name=operation.handler_name,
        risk_class=operation.risk_class,
        evidence_required=operation.evidence_required,
        preservation_fields=fields,  # type: ignore[arg-type]
    )
    fields.clear()

    assert operation.preservation_fields == ("length", "width")

    for invalid in (("length", "length"), ("bad-field",), "length"):
        with pytest.raises(RegistryError) as caught:
            OperationMetadata(
                operation="create_sphere",
                handler_name="add_sphere",
                risk_class=RiskClass.MUTATING,
                evidence_required=True,
                preservation_fields=invalid,  # type: ignore[arg-type]
            )
        assert caught.value.code is RegistryErrorCode.INVALID_METADATA


def test_entity_target_metadata_requires_object_id_result_authority():
    valid = FieldMetadata(
        "object",
        "target",
        ValueShape.ENTITY_TARGET,
        referenced_value_shape=ValueShape.OBJECT_ID,
    )
    assert valid.referenced_value_shape is ValueShape.OBJECT_ID

    for referenced_shape in (None, ValueShape.NONBLANK_STRING, ValueShape.RESULT_REF):
        with pytest.raises(RegistryError) as caught:
            FieldMetadata(
                "object",
                "target",
                ValueShape.ENTITY_TARGET,
                referenced_value_shape=referenced_shape,
            )
        assert caught.value.code is RegistryErrorCode.INVALID_METADATA


def test_result_ref_collection_metadata_requires_object_id_result_authority():
    for referenced_shape in (None, ValueShape.NONBLANK_STRING, ValueShape.RESULT_REF):
        with pytest.raises(RegistryError) as caught:
            FieldMetadata(
                "sources",
                "sources",
                ValueShape.RESULT_REF_COLLECTION,
                referenced_value_shape=referenced_shape,
            )
        assert caught.value.code is RegistryErrorCode.INVALID_METADATA


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
        DEFAULT_OPERATION_REGISTRY.lookup("create_prism")

    assert caught.value.code is RegistryErrorCode.UNKNOWN_OPERATION
    assert caught.value.operation == "create_prism"
    assert caught.value.to_mapping() == {
        "schema_version": 1,
        "code": "unknown_operation",
        "operation": "create_prism",
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
        "description",
        "result_slots",
        "preservation_fields",
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
