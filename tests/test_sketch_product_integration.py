"""Focused product integration for Sketch CREATE -> UPDATE -> PartDesign."""

from __future__ import annotations

import dataclasses
import hashlib
import os

import pytest

import tests.test_execution_freecad_partdesign_groove_reviewed_execution as groove_cases
import tests.test_execution_freecad_partdesign_promotion_reviewed_execution as promotion_cases
from tests.test_intent_bridge_freecad_sketch_intent_adapter import _constraint_graph
from vibecad.engine.session import Session
from vibecad.execution import executor as executor_module
from vibecad.execution import freecad_reviewed_intent_execution as reviewed_execution
from vibecad.execution.freecad_partdesign_promotion_reviewed_execution import (
    PartDesignPromotionOperation,
)
from vibecad.execution.freecad_sketch_bootstrap_reviewed_execution import (
    sketch_bootstrap_profile_geometry_id,
)
from vibecad.execution.freecad_sketch_reviewed_execution import (
    REVIEWED_SKETCH_REGISTRATION_MANIFEST,
)
from vibecad.freecad_env import prepare_freecad_import
from vibecad.intent_bridge.freecad_sketch_bootstrap_adapter import (
    build_sketch_bootstrap_intent_graph,
)
from vibecad.parametric.freecad_sketch_intent_rules import ReviewedSketchOperation
from vibecad.sketch.contracts import (
    SketchAnchor,
    SketchConstraintNode,
    SketchIntentGraph,
    SketchResultPort,
    SketchTypedValue,
    encode_sketch_intent_graph,
)
from vibecad.sketch.ontology import SketchAnchorTargetKind
from vibecad.workflow.contracts import ValueSource
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1


def _program(graph: object, route: object, payload: bytes) -> ReviewedIntentProgramV1:
    return ReviewedIntentProgramV1(
        operation_id=route.operation_id,
        semantic_operation=route.semantic_operation,
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(payload).hexdigest(),
        intent_graph=graph,
    )


def _bootstrap_program(*, sketch_id: str, result_id: str) -> ReviewedIntentProgramV1:
    graph = build_sketch_bootstrap_intent_graph(
        graph_id="graph_sketch_product_bootstrap",
        body_id="body_sketch_product",
        node_id=sketch_id,
        result_id=result_id,
    )
    return _program(
        graph,
        reviewed_execution.REVIEWED_SKETCH_BOOTSTRAP_ROUTES[0],
        graph.canonical_bytes,
    )


def _bootstrap_circle_node(graph: SketchIntentGraph, *, sketch_id: str):
    source = graph.geometries[0]
    geometry_id = sketch_bootstrap_profile_geometry_id(sketch_id)
    properties = tuple(
        dataclasses.replace(
            item,
            typed_value=SketchTypedValue(
                value_type_term_ref_id=item.typed_value.value_type_term_ref_id,
                value_kind=item.typed_value.value_kind,
                value=10.0,
            ),
        )
        if item.property_term_ref_id == "property_radius"
        else item
        for item in source.properties
    )
    node = dataclasses.replace(
        source,
        geometry_id=geometry_id,
        properties=properties,
    )
    source_result = next(item for item in graph.results if item.producer_id == source.geometry_id)
    result = dataclasses.replace(source_result, producer_id=geometry_id)
    return node, result


def _radius_program(*, sketch_id: str) -> tuple[ReviewedIntentProgramV1, str]:
    source = _constraint_graph(ReviewedSketchOperation.RADIUS)
    circle, circle_result = _bootstrap_circle_node(source, sketch_id=sketch_id)
    constraint = source.constraints[0]
    properties = tuple(
        dataclasses.replace(
            item,
            typed_value=SketchTypedValue(
                value_type_term_ref_id=item.typed_value.value_type_term_ref_id,
                value_kind=item.typed_value.value_kind,
                value=1.0,
            ),
        )
        for item in constraint.properties
    )
    constraint = dataclasses.replace(constraint, properties=properties)
    constraint_result = next(
        item for item in source.results if item.producer_id == constraint.constraint_id
    )
    graph = dataclasses.replace(
        source,
        graph_id="graph_sketch_product_radius",
        sketch_id=sketch_id,
        geometries=(circle,),
        constraints=(constraint,),
        results=(circle_result, constraint_result),
    )
    route = next(
        item
        for item in reviewed_execution.REVIEWED_SKETCH_ROUTES
        if item.operation.operation_id == ReviewedSketchOperation.RADIUS.value
    )
    return _program(graph, route, encode_sketch_intent_graph(graph)), circle_result.result_id


def _distance_x_program(*, sketch_id: str, circle_result_id: str) -> ReviewedIntentProgramV1:
    source = _constraint_graph(ReviewedSketchOperation.RADIUS)
    circle, circle_result = _bootstrap_circle_node(source, sketch_id=sketch_id)
    assert circle_result.result_id == circle_result_id
    anchors = (
        SketchAnchor(
            anchor_id="anchor_bootstrap_circle_center",
            target_kind=SketchAnchorTargetKind.RESULT,
            target_id=circle_result_id,
            role_term_ref_id="role_center",
        ),
        SketchAnchor(
            anchor_id="anchor_sketch_origin",
            target_kind=SketchAnchorTargetKind.SKETCH,
            target_id=sketch_id,
            role_term_ref_id="role_origin",
        ),
    )
    distance_source = _constraint_graph(ReviewedSketchOperation.DISTANCE_X)
    distance_constraint = distance_source.constraints[0]
    properties = tuple(
        dataclasses.replace(
            item,
            typed_value=SketchTypedValue(
                value_type_term_ref_id=item.typed_value.value_type_term_ref_id,
                value_kind=item.typed_value.value_kind,
                value=5.0,
            ),
        )
        for item in distance_constraint.properties
    )
    constraint = SketchConstraintNode(
        constraint_id="constraint_bootstrap_center_x",
        constraint_term_ref_id="operation_distance_x",
        anchor_ids=tuple(item.anchor_id for item in anchors),
        properties=properties,
        result_ids=("result_bootstrap_center_x",),
    )
    constraint_result = SketchResultPort(
        result_id="result_bootstrap_center_x",
        producer_id=constraint.constraint_id,
        port_id="constraint",
        value_type_term_ref_id="type_constraint",
    )
    graph = SketchIntentGraph(
        schema_version=1,
        graph_id="graph_sketch_product_center_x",
        sketch_id=sketch_id,
        terms=source.terms,
        geometries=(circle,),
        anchors=anchors,
        constraints=(constraint,),
        results=(circle_result, constraint_result),
    )
    route = next(
        item
        for item in reviewed_execution.REVIEWED_SKETCH_ROUTES
        if item.operation.operation_id == ReviewedSketchOperation.DISTANCE_X.value
    )
    return _program(graph, route, encode_sketch_intent_graph(graph))


def _context(_operation_id: str) -> executor_module._InvocationContext:
    return executor_module._InvocationContext(
        operation_id="apply_reviewed_intent",
        operation="apply_reviewed_intent",
        preserve=(),
        source=ValueSource.MODEL,
    )


def _apply(
    session: Session,
    state: executor_module._ReviewedProductRunState,
    program: ReviewedIntentProgramV1,
    *,
    sources: tuple[str, ...] = (),
) -> dict[str, object]:
    return executor_module._managed_apply_reviewed_intent(
        session,
        _context(program.operation_id),
        execution_leaf=reviewed_execution.execute_reviewed_intent_native,
        reviewed_products=state,
        intent=program.to_mapping(),
        sources=sources,
    )


def test_current_sketch_product_routes_are_exact_at_positions_99_to_120() -> None:
    routes = reviewed_execution.CURRENT_REVIEWED_INTENT_ROUTES
    assert len(routes) == 126
    assert routes[99:100] == reviewed_execution.REVIEWED_SKETCH_BOOTSTRAP_ROUTES
    assert routes[100:120] == reviewed_execution.REVIEWED_SKETCH_ROUTES
    assert len(REVIEWED_SKETCH_REGISTRATION_MANIFEST.operations) == 20
    assert reviewed_execution.REVIEWED_SKETCH_BOOTSTRAP_ROUTES[0].family.minimum_sources == 0
    assert reviewed_execution.REVIEWED_SKETCH_BOOTSTRAP_ROUTES[0].family.maximum_sources == 0
    assert all(route.family.minimum_sources == 1 for route in routes[100:120])
    assert all(route.family.maximum_sources == 1 for route in routes[100:120])


@pytest.mark.slow
def test_real_managed_sketch_create_two_updates_promotion_and_groove(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 for managed FreeCAD")

    sketch_id = "sketch_product_profile"
    bootstrap = _bootstrap_program(
        sketch_id=sketch_id,
        result_id="result_circle_curve",
    )
    radius, circle_result_id = _radius_program(sketch_id=sketch_id)
    center = _distance_x_program(
        sketch_id=sketch_id,
        circle_result_id=circle_result_id,
    )
    promotion = promotion_cases._program(  # noqa: SLF001
        PartDesignPromotionOperation.ADDITIVE_HELIX
    )
    groove = groove_cases._program()  # noqa: SLF001

    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda route, *, freecad: None,
    )
    prepare_freecad_import()
    import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

    document = FreeCAD.newDocument("SketchProductIntegration")
    document.UndoMode = 1
    session = Session()
    session._doc = document  # noqa: SLF001 - isolated native product gate
    session._loaded = True  # noqa: SLF001 - import already prepared above
    state = executor_module._ReviewedProductRunState()
    try:
        created = _apply(session, state, bootstrap)
        sketch_object_id = created["object_id"]
        first_update = _apply(session, state, radius, sources=(sketch_object_id,))
        second_update = _apply(session, state, center, sources=(sketch_object_id,))
        promoted = _apply(session, state, promotion, sources=(sketch_object_id,))
        grooved = _apply(
            session,
            state,
            groove,
            sources=(promoted["object_id"], sketch_object_id),
        )

        assert first_update["object_id"] == sketch_object_id
        assert second_update["object_id"] == sketch_object_id
        assert promoted["after"]["object_type"] == "PartDesign::AdditiveHelix"
        assert grooved["after"]["object_type"] == "PartDesign::Groove"
        assert grooved["after"]["solid_count"] == 1
    finally:
        session._doc = None  # noqa: SLF001 - native close is explicit below
        FreeCAD.closeDocument(document.Name)
