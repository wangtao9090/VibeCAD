from __future__ import annotations

import dataclasses
import hashlib

import pytest

from vibecad.execution.freecad_reviewed_intent_execution import (
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    lower_reviewed_intent,
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


def _box_graph() -> ParametricFeatureGraphV2:
    operation_terms = next(
        item for item in PART_CORE_OPERATION_TERMS if item.operation is PartCoreOperation.BOX
    )
    parameter = DesignParameterV2(
        parameter_id="parameter_box",
        name="Reviewed box dimensions",
        semantic_role_term_ref_id=PART_CORE_PARAMETERS_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_box",
            value_type_term_ref_id=PART_CORE_PARAMETERS_TYPE_TERM.term_ref_id,
            encoding_term_ref_id=PART_CORE_CANONICAL_JSON_TERM.term_ref_id,
            value={
                "shape": {"size_x_mm": 10.0, "size_y_mm": 8.0, "size_z_mm": 6.0},
                "placement": {
                    "translation_mm": [0.0, 0.0, 0.0],
                    "rotation_axis": [0.0, 0.0, 1.0],
                    "rotation_degrees": 0.0,
                },
            },
        ),
    )
    target = FeatureNodeV2(
        node_id="node_box",
        body_id="body_main",
        name="Reviewed box",
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
                    binding_id="binding_box",
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
        graph_id="graph_reviewed_box",
        name="Reviewed product box",
        terms=PART_CORE_PFG_TERMS,
        bodies=(FeatureBodyV2(body_id="body_main", name="Main body"),),
        parameters=(parameter,),
        references=(),
        nodes=(target,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_box",
                node_id=target.node_id,
                result_id=target.results[0].result_id,
            ),
        ),
    )


def _semantic_operation() -> str:
    operation = next(item for item in PART_CORE_OPERATION_SPECS if item.operation_id == "box")
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def reviewed_box_program() -> ReviewedIntentProgramV1:
    graph = _box_graph()
    return ReviewedIntentProgramV1(
        operation_id="freecad_part_core.box",
        semantic_operation=_semantic_operation(),
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


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


def test_reviewed_box_lowers_through_the_reviewed_adapter() -> None:
    program = reviewed_box_program()

    lowered = lower_reviewed_intent(program)

    assert lowered.route.operation_id == program.operation_id
    assert lowered.plan.operation is PartCoreOperation.BOX
    assert lowered.plan.sources == ()
    assert lowered.plan.parameters.value["shape"] == {
        "size_x_mm": 10.0,
        "size_y_mm": 8.0,
        "size_z_mm": 6.0,
    }
    assert lowered.result.plan_document.document_digest == lowered.plan.plan_sha256


__all__ = ["reviewed_box_program"]
