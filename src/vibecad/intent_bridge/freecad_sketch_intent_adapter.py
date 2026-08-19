"""Private exact lowering for the first reviewed FreeCAD Sketcher family.

The open ontology is backend-neutral.  Only the static reviewed operation
specifications in this module associate its twenty semantic operations with
``Sketcher::SketchObject``.  Lowering emits an authority-free plan; mutation is
still an explicit trusted-host action.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Final

from vibecad.intent_bridge.contracts import (
    AdapterDescriptor,
    BackendLoweringRequest,
    BackendLoweringResult,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.ports import ArtifactReader, TrustedCodecRegistry, TrustedProofPolicy
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanDraft,
    ReviewedPlanReceipt,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID,
    SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID,
    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    SKETCH_SELECTOR_KIND_TERMS,
)
from vibecad.parametric.freecad_sketch_intent_rules import (
    MAX_REVIEWED_SKETCH_PLAN_BYTES,
    REVIEWED_SKETCH_FREECAD_ENGINE_BUILD_ID,
    REVIEWED_SKETCH_NATIVE_TYPE_ID,
    REVIEWED_SKETCH_PLAN_MEDIA_TYPE,
    REVIEWED_SKETCH_RULE_CONTRACT_SHA256,
    REVIEWED_SKETCH_RULE_ID,
    ReviewedSketchBackendPlan,
    ReviewedSketchOperation,
    ReviewedSketchParameter,
    ReviewedSketchReference,
    ReviewedSketchResult,
    decode_reviewed_sketch_backend_plan,
    reviewed_sketch_native_operation,
    reviewed_sketch_node_sha256,
)
from vibecad.sketch.contracts import (
    SketchConstraintMode,
    SketchConstraintNode,
    SketchGeometryNode,
    SketchIntentError,
    SketchIntentGraph,
    SketchProperty,
    decode_sketch_intent_graph,
    resolve_sketch_intent,
)
from vibecad.sketch.ontology import (
    SketchAnchorSlotSignature,
    SketchAnchorTargetKind,
    SketchOntologyCatalog,
    SketchOntologyTermDefinition,
    SketchOntologyTermRef,
    SketchPropertySignature,
    SketchResultPortSignature,
    SketchTermKind,
    SketchValueKind,
    define_sketch_term,
)

_NAMESPACE = "vibecad.sketch.reviewed"
_VERSION = "1.0.0"
_DOCUMENT_TERM_DOMAIN = b"vibecad.freecad-reviewed-sketch-document-term.v1\0"
_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-reviewed-sketch-adapter.v1\0"


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _bridge(reference: SketchOntologyTermRef) -> BridgeTermRef:
    return BridgeTermRef(**reference.to_mapping())


def _document_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    digest = hashlib.sha256(
        b"\0".join(
            (
                _DOCUMENT_TERM_DOMAIN,
                _NAMESPACE.encode("ascii"),
                _VERSION.encode("ascii"),
                term_id.encode("ascii"),
            )
        )
    ).hexdigest()
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=_NAMESPACE,
        vocabulary_version=_VERSION,
        term_id=term_id,
        term_definition_sha256=digest,
    )


def _leaf(term_ref_id: str, term_id: str, kind: SketchTermKind) -> SketchOntologyTermDefinition:
    return define_sketch_term(
        term_ref_id=term_ref_id,
        namespace=_NAMESPACE,
        vocabulary_version=_VERSION,
        term_id=term_id,
        kind=kind,
    )


_ROLES: Final = {
    name: _leaf(
        f"role_{name}",
        f"anchor-role.{name.replace('_', '-')}",
        SketchTermKind.ANCHOR_ROLE,
    )
    for name in ("point", "start", "end", "center", "whole", "origin", "x_axis", "y_axis")
}
_PROPERTIES: Final = {
    name: _leaf(
        f"property_{name}",
        f"property.{name.replace('_', '-')}",
        SketchTermKind.PROPERTY,
    )
    for name in (
        "position",
        "start",
        "end",
        "center",
        "radius",
        "start_angle",
        "sweep_angle",
        "width",
        "value",
    )
}
_UNITS: Final = {
    "mm": _leaf("unit_mm", "unit.millimetre", SketchTermKind.UNIT),
    "rad": _leaf("unit_rad", "unit.radian", SketchTermKind.UNIT),
    "degree": _leaf("unit_degree", "unit.degree", SketchTermKind.UNIT),
}
_VALUE_TYPES: Final = {
    name: _leaf(
        f"type_{name}",
        f"value-type.{name.replace('_', '-')}",
        SketchTermKind.VALUE_TYPE,
    )
    for name in ("point2", "length", "angle", "point", "line", "circle", "arc", "constraint")
}


def _property_signature(
    name: str,
    value_type: str,
    value_kind: SketchValueKind,
    units: tuple[str, ...],
) -> SketchPropertySignature:
    return SketchPropertySignature(
        property_term_ref_id=_PROPERTIES[name].reference.term_ref_id,
        value_kinds=(value_kind,),
        value_type_term_ref_ids=(_VALUE_TYPES[value_type].reference.term_ref_id,),
        unit_term_ref_ids=tuple(_UNITS[item].reference.term_ref_id for item in units),
    )


def _result_signature(port_id: str, value_type: str) -> SketchResultPortSignature:
    return SketchResultPortSignature(
        port_id=port_id,
        value_type_term_ref_ids=(_VALUE_TYPES[value_type].reference.term_ref_id,),
    )


def _anchor_signature(slot_id: str, shape: str) -> SketchAnchorSlotSignature:
    roles, value_types, sketch = {
        "point": (
            ("point", "start", "end", "center", "origin"),
            ("point", "line", "circle", "arc"),
            True,
        ),
        "line": (("whole",), ("line",), False),
        "circular": (("whole",), ("circle", "arc"), False),
        "curve": (("whole",), ("line", "circle", "arc"), False),
        "axis": (("whole", "x_axis", "y_axis"), ("line",), True),
    }[shape]
    targets = (SketchAnchorTargetKind.RESULT,)
    if sketch:
        targets += (SketchAnchorTargetKind.SKETCH,)
    return SketchAnchorSlotSignature(
        slot_id=slot_id,
        target_kinds=targets,
        role_term_ref_ids=tuple(_ROLES[item].reference.term_ref_id for item in roles),
        result_type_term_ref_ids=tuple(
            _VALUE_TYPES[item].reference.term_ref_id for item in value_types
        ),
    )


_VECTOR = SketchValueKind.VECTOR
_NUMBER = SketchValueKind.NUMBER
_GEOMETRY_CONTRACTS: Final = {
    ReviewedSketchOperation.POINT: (
        (("position", "point2", _VECTOR, ("mm",)),),
        (("point", "point"),),
    ),
    ReviewedSketchOperation.LINE: (
        (("start", "point2", _VECTOR, ("mm",)), ("end", "point2", _VECTOR, ("mm",))),
        (("curve", "line"),),
    ),
    ReviewedSketchOperation.CIRCLE: (
        (("center", "point2", _VECTOR, ("mm",)), ("radius", "length", _NUMBER, ("mm",))),
        (("curve", "circle"),),
    ),
    ReviewedSketchOperation.ARC: (
        (
            ("center", "point2", _VECTOR, ("mm",)),
            ("radius", "length", _NUMBER, ("mm",)),
            ("start_angle", "angle", _NUMBER, ("rad", "degree")),
            ("sweep_angle", "angle", _NUMBER, ("rad", "degree")),
        ),
        (("curve", "arc"),),
    ),
    ReviewedSketchOperation.SLOT: (
        (
            ("start", "point2", _VECTOR, ("mm",)),
            ("end", "point2", _VECTOR, ("mm",)),
            ("width", "length", _NUMBER, ("mm",)),
        ),
        (("side_a", "line"), ("cap_end", "arc"), ("side_b", "line"), ("cap_start", "arc")),
    ),
}
_CONSTRAINT_CONTRACTS: Final = {
    ReviewedSketchOperation.COINCIDENT: (("point", "point"), None),
    ReviewedSketchOperation.HORIZONTAL: (("line",), None),
    ReviewedSketchOperation.VERTICAL: (("line",), None),
    ReviewedSketchOperation.PARALLEL: (("line", "line"), None),
    ReviewedSketchOperation.PERPENDICULAR: (("line", "line"), None),
    ReviewedSketchOperation.TANGENT: (("curve", "curve"), None),
    ReviewedSketchOperation.EQUAL: (("curve", "curve"), None),
    ReviewedSketchOperation.SYMMETRIC: (("point", "point", "axis"), None),
    ReviewedSketchOperation.DISTANCE: (("point", "point"), "length"),
    ReviewedSketchOperation.DISTANCE_X: (("point", "point"), "length"),
    ReviewedSketchOperation.DISTANCE_Y: (("point", "point"), "length"),
    ReviewedSketchOperation.LENGTH: (("line",), "length"),
    ReviewedSketchOperation.RADIUS: (("circular",), "length"),
    ReviewedSketchOperation.DIAMETER: (("circular",), "length"),
    ReviewedSketchOperation.ANGLE: (("line", "line"), "angle"),
}


def _operation_definition(operation: ReviewedSketchOperation) -> SketchOntologyTermDefinition:
    if operation in _GEOMETRY_CONTRACTS:
        property_contracts, result_contracts = _GEOMETRY_CONTRACTS[operation]
        return define_sketch_term(
            term_ref_id=f"operation_{operation.value}",
            namespace=_NAMESPACE,
            vocabulary_version=_VERSION,
            term_id=f"geometry.{operation.value.replace('_', '-')}",
            kind=SketchTermKind.GEOMETRY,
            properties=tuple(_property_signature(*item) for item in property_contracts),
            result_ports=tuple(_result_signature(*item) for item in result_contracts),
        )
    anchor_shapes, property_type = _CONSTRAINT_CONTRACTS[operation]
    properties = ()
    if property_type is not None:
        units = ("mm",) if property_type == "length" else ("rad", "degree")
        properties = (_property_signature("value", property_type, _NUMBER, units),)
    return define_sketch_term(
        term_ref_id=f"operation_{operation.value}",
        namespace=_NAMESPACE,
        vocabulary_version=_VERSION,
        term_id=f"constraint.{operation.value.replace('_', '-')}",
        kind=SketchTermKind.CONSTRAINT,
        anchor_slots=tuple(
            _anchor_signature(f"anchor_{index}", shape) for index, shape in enumerate(anchor_shapes)
        ),
        properties=properties,
        result_ports=(_result_signature("constraint", "constraint"),),
    )


_OPERATION_DEFINITIONS: Final = {
    operation: _operation_definition(operation) for operation in ReviewedSketchOperation
}
REVIEWED_SKETCH_ONTOLOGY: Final = SketchOntologyCatalog(
    schema_version=1,
    ontology_id="freecad_reviewed_sketch_v1",
    terms=(
        *_ROLES.values(),
        *_PROPERTIES.values(),
        *_UNITS.values(),
        *_VALUE_TYPES.values(),
        *_OPERATION_DEFINITIONS.values(),
    ),
)

REVIEWED_SKETCH_INTENT_ROLE_TERM: Final = _document_term(
    "role_reviewed_sketch_intent", "document-role.reviewed-sketch-intent"
)
REVIEWED_SKETCH_CAPABILITY_ROLE_TERM: Final = _document_term(
    "role_reviewed_sketch_capability", "document-role.reviewed-sketch-capability"
)
REVIEWED_SKETCH_CAPABILITY_SCHEMA_TERM: Final = _document_term(
    "schema_reviewed_sketch_capability_v1", "document-schema.reviewed-sketch-capability-v1"
)
REVIEWED_SKETCH_PLAN_ROLE_TERM: Final = _document_term(
    "role_reviewed_sketch_plan", "document-role.reviewed-sketch-plan"
)
REVIEWED_SKETCH_PLAN_SCHEMA_TERM: Final = _document_term(
    "schema_reviewed_sketch_plan_v1", "document-schema.reviewed-sketch-plan-v1"
)

_ADAPTER_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _ADAPTER_CONTRACT_DOMAIN,
            REVIEWED_SKETCH_RULE_ID.encode("ascii"),
            REVIEWED_SKETCH_RULE_CONTRACT_SHA256.encode("ascii"),
            REVIEWED_SKETCH_ONTOLOGY.catalog_sha256.encode("ascii"),
            b"exact-graph;typed-result-anchors;driving-only;atomic-plan;no-authority",
        )
    )
).hexdigest()
FREECAD_REVIEWED_SKETCH_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_reviewed_sketch_adapter",
    adapter_version="1.0.0",
    adapter_contract_sha256=_ADAPTER_CONTRACT_SHA256,
)
REVIEWED_SKETCH_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=operation.value,
        semantic_term=_bridge(_OPERATION_DEFINITIONS[operation].reference),
        native_type_id=REVIEWED_SKETCH_NATIVE_TYPE_ID,
        native_operation=reviewed_sketch_native_operation(operation),
    )
    for operation in ReviewedSketchOperation
)
_REQUEST_TERMS: Final = (
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    REVIEWED_SKETCH_INTENT_ROLE_TERM,
    REVIEWED_SKETCH_CAPABILITY_ROLE_TERM,
    REVIEWED_SKETCH_CAPABILITY_SCHEMA_TERM,
    REVIEWED_SKETCH_PLAN_ROLE_TERM,
    REVIEWED_SKETCH_PLAN_SCHEMA_TERM,
    *SKETCH_SELECTOR_KIND_TERMS,
    *(_bridge(item.reference) for item in REVIEWED_SKETCH_ONTOLOGY.terms),
)
REVIEWED_SKETCH_FAMILY_MANIFEST: Final = FamilyBatchManifest(
    family_id="freecad_reviewed_sketch",
    family_version="1.0.0",
    adapter=FREECAD_REVIEWED_SKETCH_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=hashlib.sha256(
        REVIEWED_SKETCH_FREECAD_ENGINE_BUILD_ID.encode("ascii")
    ).hexdigest(),
    rule_id=REVIEWED_SKETCH_RULE_ID,
    rule_contract_sha256=REVIEWED_SKETCH_RULE_CONTRACT_SHA256,
    intent_role_term=REVIEWED_SKETCH_INTENT_ROLE_TERM,
    intent_schema_term=SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    intent_media_type=SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    capability_role_term=REVIEWED_SKETCH_CAPABILITY_ROLE_TERM,
    capability_schema_term=REVIEWED_SKETCH_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.reviewed-sketch-capability+json",
    plan_role_term=REVIEWED_SKETCH_PLAN_ROLE_TERM,
    plan_schema_term=REVIEWED_SKETCH_PLAN_SCHEMA_TERM,
    plan_media_type=REVIEWED_SKETCH_PLAN_MEDIA_TYPE,
    request_terms=_REQUEST_TERMS,
    operations=REVIEWED_SKETCH_OPERATION_SPECS,
    max_plan_bytes=MAX_REVIEWED_SKETCH_PLAN_BYTES,
)


def _term_is(graph: SketchIntentGraph, ref_id: str, expected: SketchOntologyTermRef) -> bool:
    return next((item for item in graph.terms if item.term_ref_id == ref_id), None) == expected


def _property_by_name(
    graph: SketchIntentGraph,
    properties: tuple[SketchProperty, ...],
) -> dict[str, SketchProperty]:
    names = {item.reference.term_ref_id: name for name, item in _PROPERTIES.items()}
    result: dict[str, SketchProperty] = {}
    for item in properties:
        name = names.get(item.property_term_ref_id)
        if name is None or not _term_is(
            graph, item.property_term_ref_id, _PROPERTIES[name].reference
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/properties")
        result[name] = item
    return result


def _number(value: object, path: str) -> float:
    if type(value) not in {int, float}:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    result = float(value)
    if not math.isfinite(result):
        _fail(IntentBridgeErrorCode.INVALID_INPUT, path)
    return 0.0 if result == 0.0 else result


def _vector2(item: SketchProperty, graph: SketchIntentGraph) -> tuple[float, float]:
    if (
        item.typed_value.value_kind is not SketchValueKind.VECTOR
        or not _term_is(
            graph,
            item.typed_value.value_type_term_ref_id,
            _VALUE_TYPES["point2"].reference,
        )
        or not _term_is(graph, item.unit_term_ref_id or "", _UNITS["mm"].reference)
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/vector")
    value = item.typed_value.decoded_value
    if type(value) is not list or len(value) != 2:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/graph/vector")
    return (_number(value[0], "/graph/vector"), _number(value[1], "/graph/vector"))


def _scalar(
    item: SketchProperty,
    graph: SketchIntentGraph,
    *,
    value_type: str,
    units: tuple[str, ...],
) -> float:
    if item.typed_value.value_kind is not SketchValueKind.NUMBER or not _term_is(
        graph,
        item.typed_value.value_type_term_ref_id,
        _VALUE_TYPES[value_type].reference,
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/value")
    unit = next(
        (
            name
            for name in units
            if _term_is(graph, item.unit_term_ref_id or "", _UNITS[name].reference)
        ),
        None,
    )
    if unit is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/unit")
    value = _number(item.typed_value.decoded_value, "/graph/value")
    return math.radians(value) if unit == "degree" else value


def _parameters(
    graph: SketchIntentGraph,
    node: SketchGeometryNode | SketchConstraintNode,
    operation: ReviewedSketchOperation,
) -> tuple[ReviewedSketchParameter, ...]:
    values = _property_by_name(graph, node.properties)
    normalized: dict[str, float] = {}
    if operation is ReviewedSketchOperation.POINT:
        normalized["x_mm"], normalized["y_mm"] = _vector2(values["position"], graph)
    elif operation in {ReviewedSketchOperation.LINE, ReviewedSketchOperation.SLOT}:
        normalized["x1_mm"], normalized["y1_mm"] = _vector2(values["start"], graph)
        normalized["x2_mm"], normalized["y2_mm"] = _vector2(values["end"], graph)
        if operation is ReviewedSketchOperation.SLOT:
            normalized["width_mm"] = _scalar(
                values["width"], graph, value_type="length", units=("mm",)
            )
    elif operation in {ReviewedSketchOperation.CIRCLE, ReviewedSketchOperation.ARC}:
        normalized["cx_mm"], normalized["cy_mm"] = _vector2(values["center"], graph)
        normalized["radius_mm"] = _scalar(
            values["radius"], graph, value_type="length", units=("mm",)
        )
        if operation is ReviewedSketchOperation.ARC:
            normalized["start_angle_rad"] = _scalar(
                values["start_angle"],
                graph,
                value_type="angle",
                units=("rad", "degree"),
            ) % (2.0 * math.pi)
            normalized["sweep_angle_rad"] = _scalar(
                values["sweep_angle"],
                graph,
                value_type="angle",
                units=("rad", "degree"),
            )
    elif operation in {
        ReviewedSketchOperation.DISTANCE,
        ReviewedSketchOperation.DISTANCE_X,
        ReviewedSketchOperation.DISTANCE_Y,
        ReviewedSketchOperation.LENGTH,
        ReviewedSketchOperation.RADIUS,
        ReviewedSketchOperation.DIAMETER,
    }:
        normalized["value_mm"] = _scalar(values["value"], graph, value_type="length", units=("mm",))
    elif operation is ReviewedSketchOperation.ANGLE:
        normalized["value_rad"] = _scalar(
            values["value"], graph, value_type="angle", units=("rad", "degree")
        )
    elif values:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/properties")
    return tuple(ReviewedSketchParameter(key=key, value=value) for key, value in normalized.items())


def _normalized_node_sha256(
    graph: SketchIntentGraph,
    node: SketchGeometryNode | SketchConstraintNode,
) -> str:
    anchors = {item.anchor_id: item for item in graph.anchors}
    results = {item.result_id: item for item in graph.results}
    return reviewed_sketch_node_sha256(
        {
            "node": node.to_mapping(),
            "anchors": [anchors[item].to_mapping() for item in node.anchor_ids],
            "results": [results[item].to_mapping() for item in node.result_ids],
        }
    )


def _references(
    graph: SketchIntentGraph,
    node: SketchConstraintNode,
) -> tuple[ReviewedSketchReference, ...]:
    anchors = {item.anchor_id: item for item in graph.anchors}
    results = {item.result_id: item for item in graph.results}
    geometries = {item.geometry_id: item for item in graph.geometries}
    role_names = {item.reference.term_ref_id: name for name, item in _ROLES.items()}
    value_names = {item.reference.term_ref_id: name for name, item in _VALUE_TYPES.items()}
    converted: list[ReviewedSketchReference] = []
    for anchor_id in node.anchor_ids:
        anchor = anchors[anchor_id]
        role = role_names.get(anchor.role_term_ref_id)
        if role is None or not _term_is(graph, anchor.role_term_ref_id, _ROLES[role].reference):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/anchors/role")
        if anchor.target_kind is SketchAnchorTargetKind.SKETCH:
            converted.append(
                ReviewedSketchReference(
                    source_kind="sketch",
                    target_id=anchor.target_id,
                    role=role,
                )
            )
            continue
        if anchor.target_kind is not SketchAnchorTargetKind.RESULT:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/anchors/target")
        result = results[anchor.target_id]
        producer = geometries.get(result.producer_id)
        value_type = value_names.get(result.value_type_term_ref_id)
        if (
            producer is None
            or value_type not in {"point", "line", "circle", "arc"}
            or not _term_is(
                graph,
                result.value_type_term_ref_id,
                _VALUE_TYPES[value_type].reference,
            )
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/anchors/result")
        converted.append(
            ReviewedSketchReference(
                source_kind="result",
                target_id=result.result_id,
                role=role,
                producer_geometry_id=producer.geometry_id,
                producer_node_sha256=_normalized_node_sha256(graph, producer),
                port_id=result.port_id,
                value_type=value_type,
            )
        )
    return tuple(converted)


def _unique_sink(graph: SketchIntentGraph) -> SketchGeometryNode | SketchConstraintNode:
    results = {item.result_id: item for item in graph.results}
    nodes: dict[str, SketchGeometryNode | SketchConstraintNode] = {
        **{item.geometry_id: item for item in graph.geometries},
        **{item.constraint_id: item for item in graph.constraints},
    }
    dependencies: set[str] = set()
    anchors = {item.anchor_id: item for item in graph.anchors}
    for constraint in graph.constraints:
        for anchor_id in constraint.anchor_ids:
            anchor = anchors[anchor_id]
            if anchor.target_kind is SketchAnchorTargetKind.RESULT:
                dependencies.add(results[anchor.target_id].producer_id)
    sinks = tuple(nodes[item] for item in sorted(set(nodes) - dependencies))
    if len(sinks) != 1:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/sink")
    sink = sinks[0]
    if type(sink) is SketchGeometryNode:
        if len(graph.geometries) != 1 or graph.constraints:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    elif len(graph.constraints) != 1 or dependencies != {
        item.geometry_id for item in graph.geometries
    }:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/scope")
    return sink


def _build_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    try:
        graph = decode_sketch_intent_graph(payload)
        resolution = resolve_sketch_intent(graph, REVIEWED_SKETCH_ONTOLOGY)
    except SketchIntentError:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/graph")
    if (
        graph.graph_id != document.document_id
        or graph.graph_sha256 != document.document_digest
        or resolution.unresolved_term_ref_ids
        or resolution.inert_geometry_ids
        or resolution.inert_constraint_ids
        or set(resolution.structurally_resolved_geometry_ids)
        != {item.geometry_id for item in graph.geometries}
        or set(resolution.structurally_resolved_constraint_ids)
        != {item.constraint_id for item in graph.constraints}
    ):
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/resolution")
    node = _unique_sink(graph)
    term_ref_id = (
        node.geometry_term_ref_id
        if type(node) is SketchGeometryNode
        else node.constraint_term_ref_id
    )
    operation = next(
        (
            item
            for item, definition in _OPERATION_DEFINITIONS.items()
            if definition.reference.term_ref_id == term_ref_id
            and _term_is(graph, term_ref_id, definition.reference)
        ),
        None,
    )
    if operation is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/operation")
    operation_spec = manifest.operation_for_term(
        _bridge(_OPERATION_DEFINITIONS[operation].reference)
    )
    if operation_spec is None:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/manifest/operation")
    if type(node) is SketchConstraintNode and node.mode is not SketchConstraintMode.DRIVING:
        _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/graph/mode")
    graph_results = {item.result_id: item for item in graph.results}
    results = tuple(
        ReviewedSketchResult(
            result_id=graph_results[result_id].result_id,
            port_id=graph_results[result_id].port_id,
        )
        for result_id in node.result_ids
    )
    node_id = node.geometry_id if type(node) is SketchGeometryNode else node.constraint_id
    plan = ReviewedSketchBackendPlan(
        source_artifact_id=document.artifact_id,
        source_graph_id=graph.graph_id,
        source_graph_sha256=graph.graph_sha256,
        source_content_sha256=document.content_sha256,
        request_digest=request_digest,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        manifest_sha256=manifest.manifest_sha256,
        operation_specification_sha256=operation_spec.specification_sha256,
        sketch_id=graph.sketch_id,
        node_id=node_id,
        node_sha256=_normalized_node_sha256(graph, node),
        operation=operation,
        parameters=_parameters(graph, node, operation),
        references=() if type(node) is SketchGeometryNode else _references(graph, node),
        results=results,
        construction=node.construction if type(node) is SketchGeometryNode else None,
        mode=None if type(node) is SketchGeometryNode else node.mode.value,
        enabled=None if type(node) is SketchGeometryNode else node.enabled,
    )
    selector_kind = (
        SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID
        if type(node) is SketchGeometryNode
        else SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID
    )
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=operation_spec.semantic_term,
        subjects=(
            SubjectRef(
                artifact_id=document.artifact_id,
                selector_kind_term_ref_id=selector_kind,
                selector_id=node_id,
            ),
        ),
    )


def _validate_binding(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if type(plan) is not ReviewedSketchBackendPlan:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan")
    if (
        plan.request_digest != receipt.request_digest
        or plan.adapter_contract_sha256 != receipt.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != receipt.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.source_artifact_id != receipt.source_document.artifact_id
        or plan.source_graph_id != receipt.source_document.document_id
        or plan.source_graph_sha256 != receipt.source_document.document_digest
        or plan.source_content_sha256 != receipt.source_document.content_sha256
        or plan.plan_sha256 != receipt.plan_document.document_digest
        or operation.operation_id != plan.operation.value
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/plan/binding")


@dataclass(slots=True)
class FreeCADReviewedSketchAdapter:
    """Thin family facade over the shared exact reviewed lowering engine."""

    sink: PlanSink
    _inner: ExactReviewedFamilyAdapter = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._inner = ExactReviewedFamilyAdapter(
            REVIEWED_SKETCH_FAMILY_MANIFEST,
            self.sink,
            build_plan=_build_plan,
            decode_plan=decode_reviewed_sketch_backend_plan,
            validate_binding=_validate_binding,
        )

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._inner.descriptor

    @property
    def manifest(self) -> FamilyBatchManifest:
        return self._inner.manifest

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def lower(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> BackendLoweringResult:
        return self._inner.lower(
            request,
            artifacts=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
        )

    def lower_with_receipt(
        self,
        request: BackendLoweringRequest,
        *,
        artifacts: ArtifactReader,
        codecs: TrustedCodecRegistry,
        proof_policy: TrustedProofPolicy,
    ) -> tuple[BackendLoweringResult, ReviewedPlanReceipt]:
        return self._inner.lower_with_receipt(
            request,
            artifacts=artifacts,
            codecs=codecs,
            proof_policy=proof_policy,
        )

    def read_plan(
        self,
        receipt: ReviewedPlanReceipt,
    ) -> tuple[ReviewedSketchBackendPlan, bytes]:
        payload = self.sink.read_exact(
            receipt.plan_document,
            MAX_REVIEWED_SKETCH_PLAN_BYTES,
        )
        return (
            decode_reviewed_sketch_backend_plan(
                payload,
                expected_content_sha256=receipt.plan_document.content_sha256,
                expected_plan_sha256=receipt.plan_document.document_digest,
            ),
            payload,
        )


__all__ = [
    "FREECAD_REVIEWED_SKETCH_ADAPTER_DESCRIPTOR",
    "FreeCADReviewedSketchAdapter",
    "REVIEWED_SKETCH_FAMILY_MANIFEST",
    "REVIEWED_SKETCH_ONTOLOGY",
    "REVIEWED_SKETCH_OPERATION_SPECS",
]
