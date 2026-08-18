from __future__ import annotations

import dataclasses
import hashlib
import sys
from types import MappingProxyType, ModuleType, SimpleNamespace

import pytest

import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_CSG_ROUTES,
    REVIEWED_PART_CURVE_ROUTES,
    REVIEWED_PART_DATUM_ROUTES,
    REVIEWED_PART_OFFSET_ROUTES,
    REVIEWED_PART_PRIMITIVE_ROUTES,
    REVIEWED_PART_PROFILE_SURFACE_ROUTES,
    REVIEWED_PARTDESIGN_BOOLEAN_ROUTES,
    REVIEWED_PARTDESIGN_PATTERN_ROUTES,
    REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES,
    REVIEWED_PARTDESIGN_PROMOTION_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    lower_reviewed_intent,
    route_reviewed_intent,
)
from vibecad.execution.registry import DEFAULT_OPERATION_REGISTRY, ValueShape
from vibecad.intent_bridge.freecad_part_core_adapter import (
    PART_CORE_CANONICAL_JSON_TERM,
    PART_CORE_OPERATION_SPECS,
    PART_CORE_OPERATION_TERMS,
    PART_CORE_PARAMETERS_ROLE_TERM,
    PART_CORE_PARAMETERS_TYPE_TERM,
    PART_CORE_PFG_TERMS,
    PART_CORE_RESULT_ROLE_TERM,
    PART_CORE_SHAPE_TYPE_TERM,
    PART_CORE_SOURCE_ROLE_TERM,
    PART_CORE_STRUCTURE_TERM,
)
from vibecad.parametric.feature_graph_v2 import (
    DesignParameterV2,
    FeatureBodyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_part_core_rules import PartCoreOperation
from vibecad.workflow.reviewed_intent import (
    ReviewedIntentProgramError,
    ReviewedIntentProgramErrorCode,
    ReviewedIntentProgramV1,
)

_PRIMITIVE_PARAMETERS: dict[PartCoreOperation, dict[str, object]] = {
    PartCoreOperation.BOX: {
        "size_x_mm": 10.0,
        "size_y_mm": 8.0,
        "size_z_mm": 6.0,
    },
    PartCoreOperation.CONE: {
        "base_radius_mm": 5.0,
        "top_radius_mm": 2.0,
        "height_mm": 8.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.CYLINDER: {
        "radius_mm": 5.0,
        "height_mm": 8.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.ELLIPSOID: {
        "radius_x_mm": 5.0,
        "radius_y_mm": 4.0,
        "radius_z_mm": 3.0,
        "latitude_min_degrees": -90.0,
        "latitude_max_degrees": 90.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.PRISM: {
        "side_count": 6,
        "circumradius_mm": 5.0,
        "height_mm": 8.0,
    },
    PartCoreOperation.SPHERE: {
        "radius_mm": 5.0,
        "latitude_min_degrees": -90.0,
        "latitude_max_degrees": 90.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.TORUS: {
        "major_radius_mm": 8.0,
        "minor_radius_mm": 2.0,
        "latitude_min_degrees": -180.0,
        "latitude_max_degrees": 180.0,
        "sweep_degrees": 360.0,
    },
    PartCoreOperation.WEDGE: {
        "x_min_mm": 0.0,
        "y_min_mm": 0.0,
        "z_min_mm": 0.0,
        "x_inner_min_mm": 2.0,
        "z_inner_min_mm": 1.0,
        "x_max_mm": 10.0,
        "y_max_mm": 8.0,
        "z_max_mm": 6.0,
        "x_inner_max_mm": 8.0,
        "z_inner_max_mm": 5.0,
    },
}


def _primitive_graph(operation: PartCoreOperation) -> ParametricFeatureGraphV2:
    operation_terms = next(
        item for item in PART_CORE_OPERATION_TERMS if item.operation is operation
    )
    operation_id = operation.value
    parameter = DesignParameterV2(
        parameter_id=f"parameter_{operation_id}",
        name=f"Reviewed {operation_id} parameters",
        semantic_role_term_ref_id=PART_CORE_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id=f"value_{operation_id}",
            value_type_term_ref_id=PART_CORE_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_CORE_CANONICAL_JSON_TERM.term_ref_id,
            value={
                "shape": _PRIMITIVE_PARAMETERS[operation],
                "placement": {
                    "translation_mm": [0.0, 0.0, 0.0],
                    "rotation_axis": [0.0, 0.0, 1.0],
                    "rotation_degrees": 0.0,
                },
            },
        ),
    )
    target = FeatureNodeV2(
        node_id=f"node_{operation_id}",
        body_id="body_main",
        name=f"Reviewed {operation_id}",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_CORE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=operation_terms.family_term.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_sources",
                    semantic_role_term_ref_id=PART_CORE_SOURCE_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
                    minimum_cardinality=0,
                    maximum_cardinality=1,
                    ordered=False,
                ),
                FeatureInputPortV2(
                    port_id="port_parameters",
                    semantic_role_term_ref_id=PART_CORE_PARAMETERS_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=PART_CORE_PARAMETERS_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id=f"binding_{operation_id}",
                    port_id="port_parameters",
                    parameter_id=parameter.parameter_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_box",
                semantic_role_term_ref_id=PART_CORE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_CORE_SHAPE_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=f"graph_reviewed_{operation_id}",
        name=f"Reviewed product {operation_id}",
        terms=PART_CORE_PFG_TERMS,
        bodies=(FeatureBodyV2(body_id="body_main", name="Main body"),),
        parameters=(parameter,),
        references=(),
        nodes=(target,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id=f"selection_{operation_id}",
                node_id=target.node_id,
                result_id=target.results[0].result_id,
            ),
        ),
    )


def _semantic_operation(operation: PartCoreOperation) -> str:
    spec = next(item for item in PART_CORE_OPERATION_SPECS if item.operation_id == operation.value)
    namespace, version, term_id, digest = spec.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def reviewed_primitive_program(operation: PartCoreOperation) -> ReviewedIntentProgramV1:
    graph = _primitive_graph(operation)
    return ReviewedIntentProgramV1(
        operation_id=f"freecad_part_core.{operation.value}",
        semantic_operation=_semantic_operation(operation),
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def reviewed_box_program() -> ReviewedIntentProgramV1:
    return reviewed_primitive_program(PartCoreOperation.BOX)


def test_reviewed_intent_program_is_canonical_content_bound_and_registry_closed() -> None:
    program = reviewed_box_program()
    decoded = ReviewedIntentProgramV1.from_mapping(program.to_mapping())

    assert decoded == program
    assert decoded.canonical_bytes == program.canonical_bytes
    assert len(program.program_sha256) == 64
    metadata = DEFAULT_OPERATION_REGISTRY.lookup("apply_reviewed_intent")
    assert metadata.handler_name == "apply_reviewed_intent"
    assert metadata.direct_exposed is False
    assert metadata.argument_fields[0].value_shape is ValueShape.REVIEWED_INTENT
    assert metadata.result_slots[0].value_shape is ValueShape.OBJECT_ID
    assert metadata.resource_budget.max_created_objects == 10


def test_reviewed_intent_program_rejects_digest_rebound_and_extra_authority() -> None:
    program = reviewed_box_program()
    rebound = {**program.to_mapping(), "intent_graph_sha256": "f" * 64}
    with pytest.raises(ReviewedIntentProgramError) as caught:
        ReviewedIntentProgramV1.from_mapping(rebound)
    assert caught.value.code is ReviewedIntentProgramErrorCode.INTEGRITY_FAILURE

    extra = {**program.to_mapping(), "native_type_id": "Part::Box"}
    with pytest.raises(ReviewedIntentProgramError) as caught:
        ReviewedIntentProgramV1.from_mapping(extra)
    assert caught.value.code is ReviewedIntentProgramErrorCode.INVALID_INPUT

    graph = program.intent_graph
    operation_terms = next(
        item for item in PART_CORE_OPERATION_TERMS if item.operation is PartCoreOperation.BOX
    )
    terms = tuple(
        dataclasses.replace(term, term_definition_sha256="e" * 64)
        if term == operation_terms.operation_term
        else term
        for term in graph.terms
    )
    rebound_graph = dataclasses.replace(graph, terms=terms)
    rebound_program = ReviewedIntentProgramV1(
        operation_id=program.operation_id,
        semantic_operation=program.semantic_operation,
        intent_graph_sha256=rebound_graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(rebound_graph.canonical_bytes).hexdigest(),
        intent_graph=rebound_graph,
    )
    assert rebound_program.semantic_operation == program.semantic_operation
    assert rebound_program.intent_graph_sha256 != program.intent_graph_sha256

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        lower_reviewed_intent(rebound_program)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.LOWERING_FAILED


@pytest.mark.parametrize("operation", tuple(_PRIMITIVE_PARAMETERS))
def test_reviewed_primitives_lower_through_the_reviewed_adapter(
    operation: PartCoreOperation,
) -> None:
    program = reviewed_primitive_program(operation)

    lowered = lower_reviewed_intent(program)

    assert lowered.route.operation_id == program.operation_id
    assert lowered.plan.operation is operation
    assert lowered.plan.sources == ()
    assert lowered.plan.parameters.value["shape"] == _PRIMITIVE_PARAMETERS[operation]
    assert lowered.result.plan_document.document_digest == lowered.plan.plan_sha256


def test_reviewed_primitive_route_table_is_exact_and_closed() -> None:
    programs = tuple(reviewed_primitive_program(operation) for operation in _PRIMITIVE_PARAMETERS)

    assert CURRENT_REVIEWED_INTENT_ROUTES == (
        *REVIEWED_PART_PRIMITIVE_ROUTES,
        *REVIEWED_PART_CURVE_ROUTES,
        *REVIEWED_PART_CSG_ROUTES,
        *REVIEWED_PART_DATUM_ROUTES,
        *REVIEWED_PART_PROFILE_SURFACE_ROUTES,
        *REVIEWED_PART_OFFSET_ROUTES,
        *REVIEWED_PARTDESIGN_PROMOTION_ROUTES,
        *REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES,
        *REVIEWED_PARTDESIGN_PATTERN_ROUTES,
        *REVIEWED_PARTDESIGN_BOOLEAN_ROUTES,
    )
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 61
    assert len(REVIEWED_PART_PRIMITIVE_ROUTES) == len(programs) == 8
    assert len(REVIEWED_PART_CURVE_ROUTES) == 9
    assert len(REVIEWED_PART_CSG_ROUTES) == 3
    assert len(REVIEWED_PART_DATUM_ROUTES) == 4
    assert len(REVIEWED_PART_PROFILE_SURFACE_ROUTES) == 6
    assert len(REVIEWED_PART_OFFSET_ROUTES) == 3
    assert len(REVIEWED_PARTDESIGN_PROMOTION_ROUTES) == 6
    assert len(REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES) == 16
    assert len(REVIEWED_PARTDESIGN_PATTERN_ROUTES) == 3
    assert len(REVIEWED_PARTDESIGN_BOOLEAN_ROUTES) == 3
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PART_PRIMITIVE_ROUTES
    } == {"solid"}
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PART_CURVE_ROUTES
    } == {"valid_shape"}
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PART_CSG_ROUTES
    } == {"solid"}
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PART_PROFILE_SURFACE_ROUTES
    } == {"solid", "valid_shape"}
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PART_OFFSET_ROUTES
    } == {"solid", "valid_shape"}
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PARTDESIGN_PROMOTION_ROUTES
    } == {"solid"}
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PARTDESIGN_PATTERN_ROUTES
    } == {"solid"}
    assert {
        route.family.product_result(route.operation).result_kind.value
        for route in REVIEWED_PARTDESIGN_BOOLEAN_ROUTES
    } == {"solid"}
    assert all(
        route.family.product_result(route.operation).semantic_roles[0].value == "feature"
        for route in REVIEWED_PART_CSG_ROUTES
    )
    assert all(
        route.family.product_result(route.operation).semantic_roles[0].value == "feature"
        for route in REVIEWED_PART_OFFSET_ROUTES
    )
    assert all(
        len(route.family.product_result(route.operation).owned_type_ids) == 1
        for route in (
            *REVIEWED_PART_PRIMITIVE_ROUTES,
            *REVIEWED_PART_CURVE_ROUTES,
            *REVIEWED_PART_CSG_ROUTES,
            *REVIEWED_PART_PROFILE_SURFACE_ROUTES,
            *REVIEWED_PART_OFFSET_ROUTES,
            *REVIEWED_PARTDESIGN_PROMOTION_ROUTES,
            *REVIEWED_PARTDESIGN_PATTERN_ROUTES,
            *REVIEWED_PARTDESIGN_BOOLEAN_ROUTES,
        )
    )
    assert tuple(route_reviewed_intent(program) for program in programs) == (
        REVIEWED_PART_PRIMITIVE_ROUTES
    )
    assert {route.operation.native_type_id for route in REVIEWED_PART_PRIMITIVE_ROUTES} == {
        "Part::Box",
        "Part::Cone",
        "Part::Cylinder",
        "Part::Ellipsoid",
        "Part::Prism",
        "Part::Sphere",
        "Part::Torus",
        "Part::Wedge",
    }
    assert len({id(route.family) for route in REVIEWED_PART_PRIMITIVE_ROUTES}) == 1
    assert all(route.manifest == route.family.manifest for route in REVIEWED_PART_PRIMITIVE_ROUTES)
    assert all(
        route.subject_type_term == route.family.subject_type_term
        for route in REVIEWED_PART_PRIMITIVE_ROUTES
    )


def test_reviewed_family_descriptor_owns_lower_read_and_execute_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    base_route = REVIEWED_PART_PRIMITIVE_ROUTES[0]

    def adapter_factory(sink: object):
        calls.append("adapter")
        return reviewed_execution.build_part_core_adapter(sink)

    def validate_plan(plan: object, receipt: object, operation: object) -> None:
        calls.append("validate")
        reviewed_execution._validate_part_core_plan(plan, receipt, operation)

    def execute_plan(
        document: object,
        plan: object,
        payload: bytes,
        plan_document: object,
        operation: object,
        context: object,
    ):
        del plan, payload
        assert context.document is document
        assert context.session.doc is document
        assert context.source_results == ()
        calls.append("execute")
        result = SimpleNamespace(
            TypeId=operation.native_type_id,
            Shape=SimpleNamespace(
                isValid=lambda: True,
                Solids=(object(),),
                Volume=1.0,
            ),
        )
        document.Objects = (*document.Objects, result)
        return reviewed_execution._ReviewedFamilyNativeExecution(
            object=result,
            receipt=SimpleNamespace(plan_sha256=plan_document.document_digest),
        )

    family = reviewed_execution._ReviewedIntentFamilyDescriptor(
        manifest=base_route.manifest,
        subject_type_term=base_route.subject_type_term,
        adapter_factory=adapter_factory,
        validate_plan=validate_plan,
        execute_plan=execute_plan,
        product_results=base_route.family.product_results,
    )
    route = dataclasses.replace(base_route, family=family)
    monkeypatch.setattr(
        reviewed_execution,
        "_ROUTES_BY_IDENTITY",
        MappingProxyType({(route.operation_id, route.semantic_operation): route}),
    )
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda selected, *, freecad: None,
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    document = SimpleNamespace(Objects=())

    result = reviewed_execution.execute_reviewed_intent_native(
        SimpleNamespace(doc=document),
        reviewed_box_program(),
    )

    assert calls == ["adapter", "validate", "execute"]
    assert result.route == route
    assert result.object is document.Objects[0]
    assert result.object.TypeId == "Part::Box"
    assert result.native_receipt.plan_sha256 == result.plan_sha256


__all__ = ["reviewed_box_program", "reviewed_primitive_program"]
