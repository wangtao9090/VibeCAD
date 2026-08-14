"""Focused and managed-runtime gates for the reviewed Sketcher family."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import vibecad.intent_bridge.freecad_sketch_intent_adapter as adapter_module
from vibecad.intent_bridge.contracts import DocumentRef, IntentBridgeError
from vibecad.intent_bridge.freecad_sketch_intent_adapter import (
    REVIEWED_SKETCH_FAMILY_MANIFEST,
    REVIEWED_SKETCH_ONTOLOGY,
    REVIEWED_SKETCH_OPERATION_SPECS,
    FreeCADReviewedSketchAdapter,
)
from vibecad.parametric.freecad_sketch_intent_rules import (
    REVIEWED_SKETCH_NATIVE_TYPE_ID,
    ReviewedSketchBackendPlan,
    ReviewedSketchOperation,
    ReviewedSketchRuleError,
    decode_reviewed_sketch_backend_plan,
)
from vibecad.runtime import paths as runtime_paths
from vibecad.runtime import status as runtime_status
from vibecad.sketch.contracts import (
    SketchAnchor,
    SketchConstraintMode,
    SketchConstraintNode,
    SketchGeometryNode,
    SketchIntentError,
    SketchIntentGraph,
    SketchProperty,
    SketchResultPort,
    SketchTypedValue,
    encode_sketch_intent_graph,
)
from vibecad.sketch.ontology import (
    SketchAnchorTargetKind,
    SketchOntologyTermRef,
    SketchValueKind,
)


def _sha(value: bytes | str) -> str:
    payload = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_TERMS = {item.reference.term_ref_id: item.reference for item in REVIEWED_SKETCH_ONTOLOGY.terms}


class _MemorySink:
    def __init__(self) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        self.items[document.artifact_id] = (document, payload)
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        stored_document, payload = self.items[document.artifact_id]
        assert stored_document == document and len(payload) <= maximum_bytes
        return payload


def _property(name: str, value: object, *, angle: bool = False) -> SketchProperty:
    vector = type(value) is list
    value_type = "point2" if vector else ("angle" if angle else "length")
    unit = "degree" if angle else "mm"
    return SketchProperty(
        property_term_ref_id=f"property_{name}",
        typed_value=SketchTypedValue(
            value_type_term_ref_id=f"type_{value_type}",
            value_kind=SketchValueKind.VECTOR if vector else SketchValueKind.NUMBER,
            value=value,
        ),
        unit_term_ref_id=f"unit_{unit}",
    )


_GEOMETRY_VALUES = {
    ReviewedSketchOperation.POINT: (("position", [2.0, 3.0], False),),
    ReviewedSketchOperation.LINE: (
        ("start", [0.0, 0.0], False),
        ("end", [10.0, 2.0], False),
    ),
    ReviewedSketchOperation.CIRCLE: (
        ("center", [0.0, 0.0], False),
        ("radius", 5.0, False),
    ),
    ReviewedSketchOperation.ARC: (
        ("center", [0.0, 0.0], False),
        ("radius", 5.0, False),
        ("start_angle", 0.0, True),
        ("sweep_angle", 90.0, True),
    ),
    ReviewedSketchOperation.SLOT: (
        ("start", [-10.0, 0.0], False),
        ("end", [10.0, 3.0], False),
        ("width", 4.0, False),
    ),
}
_GEOMETRY_PORTS = {
    ReviewedSketchOperation.POINT: (("point", "point"),),
    ReviewedSketchOperation.LINE: (("curve", "line"),),
    ReviewedSketchOperation.CIRCLE: (("curve", "circle"),),
    ReviewedSketchOperation.ARC: (("curve", "arc"),),
    ReviewedSketchOperation.SLOT: (
        ("side_a", "line"),
        ("cap_end", "arc"),
        ("side_b", "line"),
        ("cap_start", "arc"),
    ),
}


def _geometry(
    operation: ReviewedSketchOperation,
    suffix: str,
    *,
    values: tuple[tuple[str, object, bool], ...] | None = None,
) -> tuple[SketchGeometryNode, tuple[SketchResultPort, ...]]:
    geometry_id = f"geometry_{suffix}"
    ports = _GEOMETRY_PORTS[operation]
    results = tuple(
        SketchResultPort(
            result_id=f"result_{suffix}_{port}",
            producer_id=geometry_id,
            port_id=port,
            value_type_term_ref_id=f"type_{value_type}",
        )
        for port, value_type in ports
    )
    node = SketchGeometryNode(
        geometry_id=geometry_id,
        geometry_term_ref_id=f"operation_{operation.value}",
        properties=tuple(
            _property(name, value, angle=angle)
            for name, value, angle in (_GEOMETRY_VALUES[operation] if values is None else values)
        ),
        result_ids=tuple(item.result_id for item in results),
    )
    return node, results


def _graph_document(graph: SketchIntentGraph) -> tuple[DocumentRef, bytes]:
    payload = encode_sketch_intent_graph(graph)
    return (
        DocumentRef(
            artifact_id=f"artifact_{graph.graph_id}",
            role_term_ref_id=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_role_term.term_ref_id,
            schema_term_ref_id=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_schema_term.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=_sha(payload),
            size_bytes=len(payload),
            media_type=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_media_type,
        ),
        payload,
    )


def _geometry_graph(operation: ReviewedSketchOperation) -> SketchIntentGraph:
    geometry, results = _geometry(operation, operation.value)
    return SketchIntentGraph(
        schema_version=1,
        graph_id=f"graph_{operation.value}",
        sketch_id="sketch_main",
        terms=tuple(_TERMS.values()),
        geometries=(geometry,),
        anchors=(),
        constraints=(),
        results=results,
    )


def _constraint_sources(
    operation: ReviewedSketchOperation,
) -> tuple[
    tuple[SketchGeometryNode, ...], tuple[SketchResultPort, ...], tuple[tuple[str, str], ...]
]:
    point_a, point_a_results = _geometry(ReviewedSketchOperation.POINT, "point_a")
    point_b, point_b_results = _geometry(
        ReviewedSketchOperation.POINT,
        "point_b",
        values=(("position", [12.0, 8.0], False),),
    )
    line_a, line_a_results = _geometry(ReviewedSketchOperation.LINE, "line_a")
    line_b, line_b_results = _geometry(
        ReviewedSketchOperation.LINE,
        "line_b",
        values=(("start", [0.0, 5.0], False), ("end", [3.0, 12.0], False)),
    )
    circle, circle_results = _geometry(ReviewedSketchOperation.CIRCLE, "circle")
    if operation is ReviewedSketchOperation.COINCIDENT:
        return (
            (line_a,),
            line_a_results,
            ((line_a_results[0].result_id, "start"), ("sketch_main", "origin")),
        )
    if operation in {
        ReviewedSketchOperation.HORIZONTAL,
        ReviewedSketchOperation.VERTICAL,
        ReviewedSketchOperation.LENGTH,
    }:
        return (line_a,), line_a_results, ((line_a_results[0].result_id, "whole"),)
    if operation in {
        ReviewedSketchOperation.PARALLEL,
        ReviewedSketchOperation.PERPENDICULAR,
        ReviewedSketchOperation.EQUAL,
        ReviewedSketchOperation.ANGLE,
    }:
        return (
            (line_a, line_b),
            (*line_a_results, *line_b_results),
            ((line_a_results[0].result_id, "whole"), (line_b_results[0].result_id, "whole")),
        )
    if operation is ReviewedSketchOperation.TANGENT:
        return (
            (line_a, circle),
            (*line_a_results, *circle_results),
            ((line_a_results[0].result_id, "whole"), (circle_results[0].result_id, "whole")),
        )
    if operation is ReviewedSketchOperation.SYMMETRIC:
        return (
            (point_a, point_b, line_a),
            (*point_a_results, *point_b_results, *line_a_results),
            (
                (point_a_results[0].result_id, "point"),
                (point_b_results[0].result_id, "point"),
                (line_a_results[0].result_id, "whole"),
            ),
        )
    if operation in {
        ReviewedSketchOperation.DISTANCE,
        ReviewedSketchOperation.DISTANCE_X,
        ReviewedSketchOperation.DISTANCE_Y,
    }:
        return (
            (point_a, point_b),
            (*point_a_results, *point_b_results),
            ((point_a_results[0].result_id, "point"), (point_b_results[0].result_id, "point")),
        )
    return (circle,), circle_results, ((circle_results[0].result_id, "whole"),)


def _constraint_graph(
    operation: ReviewedSketchOperation,
    *,
    mode: SketchConstraintMode = SketchConstraintMode.DRIVING,
) -> SketchIntentGraph:
    geometries, geometry_results, references = _constraint_sources(operation)
    anchors = tuple(
        SketchAnchor(
            anchor_id=f"anchor_{index}",
            target_kind=(
                SketchAnchorTargetKind.SKETCH
                if target == "sketch_main"
                else SketchAnchorTargetKind.RESULT
            ),
            target_id=target,
            role_term_ref_id=f"role_{role}",
        )
        for index, (target, role) in enumerate(references)
    )
    properties = ()
    if operation in {
        ReviewedSketchOperation.DISTANCE,
        ReviewedSketchOperation.DISTANCE_X,
        ReviewedSketchOperation.DISTANCE_Y,
        ReviewedSketchOperation.LENGTH,
        ReviewedSketchOperation.RADIUS,
        ReviewedSketchOperation.DIAMETER,
    }:
        properties = (_property("value", 7.0),)
    elif operation is ReviewedSketchOperation.ANGLE:
        properties = (_property("value", 45.0, angle=True),)
    result = SketchResultPort(
        result_id=f"result_{operation.value}",
        producer_id=f"constraint_{operation.value}",
        port_id="constraint",
        value_type_term_ref_id="type_constraint",
    )
    constraint = SketchConstraintNode(
        constraint_id=f"constraint_{operation.value}",
        constraint_term_ref_id=f"operation_{operation.value}",
        anchor_ids=tuple(item.anchor_id for item in anchors),
        properties=properties,
        result_ids=(result.result_id,),
        mode=mode,
    )
    return SketchIntentGraph(
        schema_version=1,
        graph_id=f"graph_{operation.value}",
        sketch_id="sketch_main",
        terms=tuple(_TERMS.values()),
        geometries=geometries,
        anchors=anchors,
        constraints=(constraint,),
        results=(*geometry_results, result),
    )


def _build(graph: SketchIntentGraph) -> ReviewedSketchBackendPlan:
    document, payload = _graph_document(graph)
    draft = adapter_module._build_plan(
        document,
        payload,
        "a" * 64,
        REVIEWED_SKETCH_FAMILY_MANIFEST,
    )
    return decode_reviewed_sketch_backend_plan(
        draft.payload,
        expected_content_sha256=_sha(draft.payload),
        expected_plan_sha256=draft.semantic_plan_sha256,
    )


def test_manifest_freezes_twenty_specs_one_typeid_and_open_ontology() -> None:
    assert len(REVIEWED_SKETCH_OPERATION_SPECS) == 20
    assert {item.operation_id for item in REVIEWED_SKETCH_OPERATION_SPECS} == {
        item.value for item in ReviewedSketchOperation
    }
    assert {item.native_type_id for item in REVIEWED_SKETCH_OPERATION_SPECS} == {
        REVIEWED_SKETCH_NATIVE_TYPE_ID
    }
    assert len({item.specification_sha256 for item in REVIEWED_SKETCH_OPERATION_SPECS}) == 20
    assert REVIEWED_SKETCH_FAMILY_MANIFEST.executable is False
    assert REVIEWED_SKETCH_FAMILY_MANIFEST.grants_execution_authority is False
    assert len(REVIEWED_SKETCH_ONTOLOGY.terms) == 48
    for definition in adapter_module._OPERATION_DEFINITIONS.values():
        assert definition.reference in _TERMS.values()
    adapter = FreeCADReviewedSketchAdapter(_MemorySink())
    assert adapter.manifest == REVIEWED_SKETCH_FAMILY_MANIFEST
    assert adapter.descriptor == REVIEWED_SKETCH_FAMILY_MANIFEST.adapter
    assert adapter.executable is False and adapter.grants_execution_authority is False


@pytest.mark.parametrize("operation", tuple(adapter_module._GEOMETRY_CONTRACTS))
def test_all_geometry_semantics_lower_to_canonical_authority_free_plans(
    operation: ReviewedSketchOperation,
) -> None:
    plan = _build(_geometry_graph(operation))
    assert plan.operation is operation
    assert plan.references == ()
    assert plan.construction is False
    assert plan.mode is None and plan.enabled is None
    assert decode_reviewed_sketch_backend_plan(plan.canonical_bytes) == plan
    assert b'"authority":"none"' in plan.canonical_bytes
    if operation is ReviewedSketchOperation.ARC:
        parameters = {item.key: item.value for item in plan.parameters}
        assert parameters["sweep_angle_rad"] == pytest.approx(0.5 * 3.141592653589793)


@pytest.mark.parametrize("operation", tuple(adapter_module._CONSTRAINT_CONTRACTS))
def test_all_constraint_semantics_lower_with_typed_result_anchors(
    operation: ReviewedSketchOperation,
) -> None:
    plan = _build(_constraint_graph(operation))
    assert plan.operation is operation
    assert plan.mode == "driving" and plan.enabled is True
    assert len(plan.references) == adapter_module._CONSTRAINT_CONTRACTS[operation][0].__len__()
    assert all(item.source_kind in {"result", "sketch"} for item in plan.references)
    assert all(
        item.producer_node_sha256 is not None
        for item in plan.references
        if item.source_kind == "result"
    )
    if operation is ReviewedSketchOperation.ANGLE:
        assert plan.parameters[0].value == pytest.approx(0.25 * 3.141592653589793)


def test_unknown_reference_mode_disconnected_scope_and_plan_tamper_fail_closed() -> None:
    graph = _geometry_graph(ReviewedSketchOperation.LINE)
    unknown = SketchOntologyTermRef(
        term_ref_id="operation_unknown",
        namespace="example.unknown",
        vocabulary_version="1.0.0",
        term_id="geometry.unknown",
        term_definition_sha256="f" * 64,
    )
    unknown_geometry = dataclasses.replace(
        graph.geometries[0],
        geometry_term_ref_id=unknown.term_ref_id,
    )
    unknown_graph = dataclasses.replace(
        graph,
        terms=(*graph.terms, unknown),
        geometries=(unknown_geometry,),
    )
    with pytest.raises(IntentBridgeError):
        _build(unknown_graph)

    reference_graph = _constraint_graph(
        ReviewedSketchOperation.LENGTH,
        mode=SketchConstraintMode.REFERENCE,
    )
    with pytest.raises(IntentBridgeError):
        _build(reference_graph)

    dangling_anchor = dataclasses.replace(
        reference_graph.anchors[0],
        target_id="result_missing",
    )
    with pytest.raises(SketchIntentError):
        dataclasses.replace(reference_graph, anchors=(dangling_anchor,))

    wrong_result = dataclasses.replace(
        reference_graph.results[0],
        value_type_term_ref_id="type_circle",
    )
    wrong_kind = dataclasses.replace(
        reference_graph,
        results=(wrong_result, *reference_graph.results[1:]),
    )
    with pytest.raises(IntentBridgeError):
        _build(wrong_kind)

    extra, extra_results = _geometry(ReviewedSketchOperation.POINT, "disconnected")
    disconnected = dataclasses.replace(
        graph,
        geometries=(*graph.geometries, extra),
        results=(*graph.results, *extra_results),
    )
    with pytest.raises(IntentBridgeError):
        _build(disconnected)

    plan = _build(graph)
    with pytest.raises(ReviewedSketchRuleError):
        decode_reviewed_sketch_backend_plan(plan.canonical_bytes + b" ")
    tampered = plan.canonical_bytes.replace(
        plan.source_graph_sha256.encode("ascii"),
        b"0" * 64,
    )
    with pytest.raises(ReviewedSketchRuleError):
        decode_reviewed_sketch_backend_plan(
            tampered,
            expected_plan_sha256=plan.plan_sha256,
        )


def _managed_runtime_python() -> Path:
    runtime = runtime_paths.active_runtime_python()
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 for managed FreeCAD gates")
    if (
        not runtime.is_file()
        or not runtime_paths.ready_sentinel().is_file()
        or not runtime_status.engine_compatible(runtime)
    ):
        pytest.fail("an existing compatible managed FreeCAD runtime is required")
    return runtime


def _run_freecad(code: str) -> None:
    completed = subprocess.run(
        [str(_managed_runtime_python()), "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.slow
def test_real_freecad_geometry_batch_create_save_reopen_and_metadata_gate(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).parents[1] / "src"
    target = tmp_path / "reviewed-sketch-geometries.FCStd"
    code = f"""
import hashlib, os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
import FreeCAD
from vibecad.intent_bridge.freecad_sketch_intent_adapter import REVIEWED_SKETCH_FAMILY_MANIFEST
from vibecad.parametric.freecad_sketch_intent_rules import (
    ReviewedSketchBackendPlan, ReviewedSketchExecutionBindings, ReviewedSketchOperation,
    ReviewedSketchParameter, ReviewedSketchResult, apply_reviewed_sketch_plan,
    reviewed_sketch_node_sha256,
)

def make_plan(operation, node_id, parameters, results):
    spec = next(item for item in REVIEWED_SKETCH_FAMILY_MANIFEST.operations
                if item.operation_id == operation.value)
    return ReviewedSketchBackendPlan(
        source_artifact_id='artifact_geometry_batch', source_graph_id='graph_geometry_batch',
        source_graph_sha256='1' * 64, source_content_sha256='2' * 64,
        request_digest='3' * 64,
        adapter_contract_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.manifest_sha256,
        operation_specification_sha256=spec.specification_sha256,
        sketch_id='sketch_geometry_batch', node_id=node_id,
        node_sha256=reviewed_sketch_node_sha256({{'operation': operation.value, 'node': node_id}}),
        operation=operation,
        parameters=tuple(ReviewedSketchParameter(key=key, value=value)
                         for key, value in parameters.items()),
        references=(),
        results=tuple(ReviewedSketchResult(result_id=result_id, port_id=port_id)
                      for port_id, result_id in results),
        construction=False, mode=None, enabled=None,
    )

cases = (
    (ReviewedSketchOperation.POINT, 'geometry_point', {{'x_mm': 1.0, 'y_mm': 2.0}},
     (('point', 'result_point'),)),
    (ReviewedSketchOperation.LINE, 'geometry_line',
     {{'x1_mm': 0.0, 'x2_mm': 10.0, 'y1_mm': 0.0, 'y2_mm': 3.0}},
     (('curve', 'result_line'),)),
    (ReviewedSketchOperation.CIRCLE, 'geometry_circle',
     {{'cx_mm': 20.0, 'cy_mm': 0.0, 'radius_mm': 4.0}},
     (('curve', 'result_circle'),)),
    (ReviewedSketchOperation.ARC, 'geometry_arc',
     {{'cx_mm': -20.0, 'cy_mm': 0.0, 'radius_mm': 5.0,
       'start_angle_rad': 0.0, 'sweep_angle_rad': 1.5707963267948966}},
     (('curve', 'result_arc'),)),
    (ReviewedSketchOperation.SLOT, 'geometry_slot',
     {{'width_mm': 4.0, 'x1_mm': -10.0, 'x2_mm': 10.0, 'y1_mm': -10.0, 'y2_mm': -7.0}},
     (('cap_end', 'result_slot_cap_end'), ('cap_start', 'result_slot_cap_start'),
      ('side_a', 'result_slot_side_a'), ('side_b', 'result_slot_side_b'))),
)
document = FreeCAD.newDocument('ReviewedSketchGeometryBatch')
document.UndoMode = 1
sketch = document.addObject('Sketcher::SketchObject', 'Sketch')
receipts = []
for operation, node_id, parameters, results in cases:
    plan = make_plan(operation, node_id, parameters, results)
    payload = plan.canonical_bytes
    receipts.append(apply_reviewed_sketch_plan(
        payload, expected_content_sha256=hashlib.sha256(payload).hexdigest(),
        expected_plan_sha256=plan.plan_sha256,
        bindings=ReviewedSketchExecutionBindings(
            document=document, sketch=sketch, sketch_id='sketch_geometry_batch'),
    ))
assert [item.operation for item in receipts] == [item[0] for item in cases]
assert sketch.GeometryCount == 8 and sketch.ConstraintCount == 9 and sketch.solve() == 0
document.recompute()
document.saveAs({str(target)!r})
FreeCAD.closeDocument(document.Name)

reopened = FreeCAD.openDocument({str(target)!r})
reopened.UndoMode = 1
sketch = reopened.getObject('Sketch')
assert sketch.GeometryCount == 8 and sketch.ConstraintCount == 9 and sketch.solve() == 0
sketch.moveGeometry(0, 1, FreeCAD.Vector(2.0, 4.0, 0.0))
assert sketch.solve() == 0
extra = make_plan(
    ReviewedSketchOperation.POINT, 'geometry_point_after_reopen',
    {{'x_mm': 30.0, 'y_mm': 30.0}}, (('point', 'result_point_after_reopen'),))
payload = extra.canonical_bytes
apply_reviewed_sketch_plan(
    payload, expected_content_sha256=hashlib.sha256(payload).hexdigest(),
    expected_plan_sha256=extra.plan_sha256,
    bindings=ReviewedSketchExecutionBindings(
        document=reopened, sketch=sketch, sketch_id='sketch_geometry_batch'),
)
assert sketch.GeometryCount == 9 and sketch.ConstraintCount == 9 and sketch.solve() == 0
FreeCAD.closeDocument(reopened.Name)
"""
    _run_freecad(code)


@pytest.mark.slow
def test_real_freecad_constraint_batch_solver_roundtrip_and_late_rollback(
    tmp_path: Path,
) -> None:
    source_root = Path(__file__).parents[1] / "src"
    target = tmp_path / "reviewed-sketch-constraints.FCStd"
    code = f"""
import hashlib, math, os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
import FreeCAD
from vibecad.intent_bridge.freecad_sketch_intent_adapter import REVIEWED_SKETCH_FAMILY_MANIFEST
from vibecad.parametric.freecad_sketch_intent_rules import (
    ReviewedSketchBackendPlan, ReviewedSketchExecutionBindings, ReviewedSketchOperation,
    ReviewedSketchParameter, ReviewedSketchReference, ReviewedSketchResult,
    ReviewedSketchRuleError, _validated_metadata, apply_reviewed_sketch_plan,
    reviewed_sketch_node_sha256,
)

def make_plan(
        operation, sketch_id, node_id, parameters, references, results, *, geometry, enabled=True):
    spec = next(item for item in REVIEWED_SKETCH_FAMILY_MANIFEST.operations
                if item.operation_id == operation.value)
    return ReviewedSketchBackendPlan(
        source_artifact_id='artifact_constraint_batch', source_graph_id='graph_constraint_batch',
        source_graph_sha256='1' * 64, source_content_sha256='2' * 64,
        request_digest='3' * 64,
        adapter_contract_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.adapter.adapter_contract_sha256,
        manifest_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.manifest_sha256,
        operation_specification_sha256=spec.specification_sha256,
        sketch_id=sketch_id, node_id=node_id,
        node_sha256=reviewed_sketch_node_sha256({{'operation': operation.value, 'node': node_id}}),
        operation=operation,
        parameters=tuple(ReviewedSketchParameter(key=key, value=value)
                         for key, value in parameters.items()),
        references=tuple(references),
        results=tuple(ReviewedSketchResult(result_id=result_id, port_id=port_id)
                      for port_id, result_id in results),
        construction=False if geometry else None,
        mode=None if geometry else 'driving', enabled=None if geometry else enabled,
    )

def apply(document, sketch, sketch_id, plan):
    payload = plan.canonical_bytes
    return apply_reviewed_sketch_plan(
        payload, expected_content_sha256=hashlib.sha256(payload).hexdigest(),
        expected_plan_sha256=plan.plan_sha256,
        bindings=ReviewedSketchExecutionBindings(
            document=document, sketch=sketch, sketch_id=sketch_id),
    )

def add_point(document, sketch, sketch_id, suffix, x, y):
    plan = make_plan(
        ReviewedSketchOperation.POINT, sketch_id, 'geometry_' + suffix,
        {{'x_mm': x, 'y_mm': y}}, (), (('point', 'result_' + suffix),), geometry=True)
    apply(document, sketch, sketch_id, plan)
    return plan, plan.results[0]

def add_line(document, sketch, sketch_id, suffix, x1, y1, x2, y2):
    plan = make_plan(
        ReviewedSketchOperation.LINE, sketch_id, 'geometry_' + suffix,
        {{'x1_mm': x1, 'x2_mm': x2, 'y1_mm': y1, 'y2_mm': y2}}, (),
        (('curve', 'result_' + suffix),), geometry=True)
    apply(document, sketch, sketch_id, plan)
    return plan, plan.results[0]

def add_circle(document, sketch, sketch_id, suffix, cx, cy, radius):
    plan = make_plan(
        ReviewedSketchOperation.CIRCLE, sketch_id, 'geometry_' + suffix,
        {{'cx_mm': cx, 'cy_mm': cy, 'radius_mm': radius}}, (),
        (('curve', 'result_' + suffix),), geometry=True)
    apply(document, sketch, sketch_id, plan)
    return plan, plan.results[0]

def result_ref(plan, result, role, value_type):
    return ReviewedSketchReference(
        source_kind='result', target_id=result.result_id, role=role,
        producer_geometry_id=plan.node_id, producer_node_sha256=plan.node_sha256,
        port_id=result.port_id, value_type=value_type)

def sketch_ref(sketch_id, role):
    return ReviewedSketchReference(source_kind='sketch', target_id=sketch_id, role=role)

def add_constraint(
        document, sketch, sketch_id, operation, references, value=None, suffix=None, enabled=True):
    parameters = {{}}
    if value is not None:
        key = 'value_rad' if operation is ReviewedSketchOperation.ANGLE else 'value_mm'
        parameters[key] = value
    suffix = operation.value if suffix is None else suffix
    plan = make_plan(
        operation, sketch_id, 'constraint_' + suffix, parameters, references,
        (('constraint', 'result_constraint_' + suffix),), geometry=False, enabled=enabled)
    return apply(document, sketch, sketch_id, plan), plan

document = FreeCAD.newDocument('ReviewedSketchConstraintBatch')
document.UndoMode = 1
operations = tuple(item for item in ReviewedSketchOperation if item.value not in
                   {{'point', 'line', 'circle', 'arc', 'slot'}})
for operation in operations:
    sketch_id = 'sketch_' + operation.value
    sketch = document.addObject('Sketcher::SketchObject', 'Sketch_' + operation.value)
    if operation is ReviewedSketchOperation.COINCIDENT:
        first, first_result = add_line(document, sketch, sketch_id, 'line', 0, 0, 10, 2)
        refs = (result_ref(first, first_result, 'start', 'line'), sketch_ref(sketch_id, 'origin'))
    elif operation in {{ReviewedSketchOperation.HORIZONTAL, ReviewedSketchOperation.LENGTH}}:
        first, first_result = add_line(document, sketch, sketch_id, 'line', 0, 0, 10, 0)
        refs = (result_ref(first, first_result, 'whole', 'line'),)
    elif operation is ReviewedSketchOperation.VERTICAL:
        first, first_result = add_line(document, sketch, sketch_id, 'line', 0, 0, 0, 10)
        refs = (result_ref(first, first_result, 'whole', 'line'),)
    elif operation in {{ReviewedSketchOperation.PARALLEL, ReviewedSketchOperation.EQUAL}}:
        first, first_result = add_line(document, sketch, sketch_id, 'line_a', 0, 0, 10, 0)
        second, second_result = add_line(document, sketch, sketch_id, 'line_b', 0, 5, 10, 5)
        refs = (result_ref(first, first_result, 'whole', 'line'),
                result_ref(second, second_result, 'whole', 'line'))
    elif operation is ReviewedSketchOperation.PERPENDICULAR:
        first, first_result = add_line(document, sketch, sketch_id, 'line_a', 0, 0, 10, 0)
        second, second_result = add_line(document, sketch, sketch_id, 'line_b', 0, 0, 0, 10)
        refs = (result_ref(first, first_result, 'whole', 'line'),
                result_ref(second, second_result, 'whole', 'line'))
    elif operation is ReviewedSketchOperation.TANGENT:
        first, first_result = add_line(document, sketch, sketch_id, 'line', -10, 5, 10, 5)
        second, second_result = add_circle(document, sketch, sketch_id, 'circle', 0, 0, 5)
        refs = (result_ref(first, first_result, 'whole', 'line'),
                result_ref(second, second_result, 'whole', 'circle'))
    elif operation is ReviewedSketchOperation.SYMMETRIC:
        first, first_result = add_point(document, sketch, sketch_id, 'point_a', -3, 2)
        second, second_result = add_point(document, sketch, sketch_id, 'point_b', 3, 2)
        axis, axis_result = add_line(document, sketch, sketch_id, 'axis', 0, -10, 0, 10)
        refs = (result_ref(first, first_result, 'point', 'point'),
                result_ref(second, second_result, 'point', 'point'),
                result_ref(axis, axis_result, 'whole', 'line'))
    elif operation in {{ReviewedSketchOperation.DISTANCE, ReviewedSketchOperation.DISTANCE_X,
                         ReviewedSketchOperation.DISTANCE_Y}}:
        first, first_result = add_point(document, sketch, sketch_id, 'point_a', 0, 0)
        second, second_result = add_point(document, sketch, sketch_id, 'point_b', 6, 8)
        refs = (result_ref(first, first_result, 'point', 'point'),
                result_ref(second, second_result, 'point', 'point'))
    elif operation in {{ReviewedSketchOperation.RADIUS, ReviewedSketchOperation.DIAMETER}}:
        first, first_result = add_circle(document, sketch, sketch_id, 'circle', 0, 0, 5)
        refs = (result_ref(first, first_result, 'whole', 'circle'),)
    else:
        first, first_result = add_line(document, sketch, sketch_id, 'line_a', 0, 0, 10, 0)
        second, second_result = add_line(document, sketch, sketch_id, 'line_b', 0, 0, 10, 10)
        refs = (result_ref(first, first_result, 'whole', 'line'),
                result_ref(second, second_result, 'whole', 'line'))
    value = {{
        ReviewedSketchOperation.DISTANCE: 10.0,
        ReviewedSketchOperation.DISTANCE_X: 6.0,
        ReviewedSketchOperation.DISTANCE_Y: 8.0,
        ReviewedSketchOperation.LENGTH: 10.0,
        ReviewedSketchOperation.RADIUS: 5.0,
        ReviewedSketchOperation.DIAMETER: 10.0,
        ReviewedSketchOperation.ANGLE: math.pi / 4.0,
    }}.get(operation)
    receipt, _ = add_constraint(document, sketch, sketch_id, operation, refs, value)
    assert receipt.operation is operation and sketch.solve() == 0

sketch_id = 'sketch_inactive'
sketch = document.addObject('Sketcher::SketchObject', 'Sketch_inactive')
line, line_result = add_line(document, sketch, sketch_id, 'inactive_line', 0, 0, 10, 3)
before_dof = sketch.DoF
inactive_receipt, _ = add_constraint(
    document, sketch, sketch_id, ReviewedSketchOperation.HORIZONTAL,
    (result_ref(line, line_result, 'whole', 'line'),), suffix='inactive_horizontal', enabled=False)
assert inactive_receipt.dof == before_dof and sketch.DoF == before_dof
assert not sketch.getActive(inactive_receipt.constraint_indices[0])

document.recompute()
document.saveAs({str(target)!r})
FreeCAD.closeDocument(document.Name)
document = FreeCAD.openDocument({str(target)!r})
document.UndoMode = 1
for operation in operations:
    sketch_id = 'sketch_' + operation.value
    sketch = document.getObject('Sketch_' + operation.value)
    _validated_metadata(sketch, sketch_id)
    assert sketch.solve() == 0
_validated_metadata(document.getObject('Sketch_inactive'), 'sketch_inactive')

sketch_id = 'sketch_late_rollback'
sketch = document.addObject('Sketcher::SketchObject', 'Sketch_late_rollback')
line, line_result = add_line(document, sketch, sketch_id, 'full_line', 0, 0, 10, 0)
line_start = result_ref(line, line_result, 'start', 'line')
line_whole = result_ref(line, line_result, 'whole', 'line')
add_constraint(document, sketch, sketch_id, ReviewedSketchOperation.COINCIDENT,
               (line_start, sketch_ref(sketch_id, 'origin')), suffix='full_coincident')
add_constraint(document, sketch, sketch_id, ReviewedSketchOperation.HORIZONTAL,
               (line_whole,), suffix='full_horizontal')
add_constraint(document, sketch, sketch_id, ReviewedSketchOperation.LENGTH,
               (line_whole,), 10.0, suffix='full_length')
assert sketch.DoF == 0 and sketch.FullyConstrained
before = (sketch.GeometryCount, sketch.ConstraintCount,
          getattr(sketch, 'VibeCADReviewedSketchIntent'), tuple(document.Objects))
try:
    add_constraint(document, sketch, sketch_id, ReviewedSketchOperation.VERTICAL,
                   (line_whole,), suffix='invalid_vertical')
    raise AssertionError('overconstraint was not rejected')
except ReviewedSketchRuleError:
    pass
after = (sketch.GeometryCount, sketch.ConstraintCount,
         getattr(sketch, 'VibeCADReviewedSketchIntent'), tuple(document.Objects))
assert after == before and not document.HasPendingTransaction and sketch.solve() == 0
FreeCAD.closeDocument(document.Name)
"""
    _run_freecad(code)
