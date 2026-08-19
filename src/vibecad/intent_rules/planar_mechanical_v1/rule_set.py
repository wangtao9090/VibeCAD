"""Reviewed planar-mechanical v1 evidence admission and intent emission.

The rule pack is intentionally concrete while its outputs remain backend
neutral.  New visual/CAD semantics belong in additional reviewed rule packs;
the generic compiler and bridge wire do not need to change.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProofAssertion,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_GRAPH_RESULT,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    SKETCH_ROOT_SELECTOR_TERM,
)
from vibecad.intent_bridge.visual_feature_graph_codec import (
    VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
    VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
)
from vibecad.intent_compiler.contracts import (
    CompiledIntentDocument,
    DocumentSignature,
    IntentRuleDescriptor,
    IntentRuleSetDescriptor,
    RuleSetCompileContext,
    RuleSetEmission,
    canonical_bytes,
)
from vibecad.parametric.feature_graph_v2 import (
    DesignParameterV2,
    FeatureBodyV2,
    FeatureDependencyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    TermTypedValueV2,
    encode_parametric_feature_graph_v2,
)
from vibecad.sketch.contracts import (
    SKETCH_INTENT_SCHEMA_VERSION,
    SketchGeometryNode,
    SketchIntentGraph,
    SketchProperty,
    SketchTypedValue,
    SketchValueKind,
    encode_sketch_intent_graph,
)
from vibecad.visual.feature_graph import (
    AssertionState,
    ClosureState,
    EntityLayer,
    FeatureRelation,
    GeometryRecord,
    GraphElementKind,
    MeasurementEstimateKind,
    MetricPlaneFrameBinding,
    MetricUncertaintyKind,
    ProvenanceKind,
    VisualFeatureGraph,
    decode_visual_feature_graph,
)

from .terms import (
    MAX_COORDINATE_UNCERTAINTY_MM,
    MAX_DEPTH_MM,
    MAX_INNER_CIRCLES,
    MAX_RECTANGLE_RESIDUAL_MM,
    MIN_ADVISORY_SUPPORT,
    MIN_FEATURE_SIZE_MM,
    PFG_ENCODING_CANONICAL_JSON,
    PFG_FAMILY_EXTRUSION,
    PFG_FAMILY_REFERENCE,
    PFG_LOCATOR_SKETCH_DOCUMENT,
    PFG_OPERATION_ADD,
    PFG_OPERATION_REFERENCE_PROFILES,
    PFG_OPERATION_REMOVE,
    PFG_OUTPUT_TERMS,
    PFG_PARAMETER_DEPTH,
    PFG_PARAMETER_EXTENT,
    PFG_PORT_BASE,
    PFG_PORT_DEPTH,
    PFG_PORT_EXTENT,
    PFG_PORT_EXTERNAL,
    PFG_PORT_PROFILE,
    PFG_RESULT_INNER_PROFILE,
    PFG_RESULT_OUTER_PROFILE,
    PFG_RESULT_SOLID,
    PFG_STRUCTURE_FEATURE,
    PFG_STRUCTURE_REFERENCE,
    PFG_TYPE_EXTENT_THROUGH,
    PFG_TYPE_LENGTH_MM,
    PFG_TYPE_SKETCH_DOCUMENT,
    PFG_TYPE_SOLID,
    PFG_TYPE_WIRE,
    PLANAR_MECHANICAL_V1_CUSTOM_BRIDGE_TERMS,
    PREDICATE_PARAMETRIC_COMPILED,
    PREDICATE_SKETCH_COMPILED,
    ROLE_COMPONENT,
    ROLE_DECISION,
    ROLE_DEPTH,
    ROLE_OUTER_PROFILE,
    ROLE_PARAMETRIC_INTENT,
    ROLE_SKETCH_INTENT,
    ROLE_VISUAL_EVIDENCE,
    RULE_COMPILE_PARAMETRIC,
    RULE_COMPILE_SKETCH,
    RULE_SET_PLANAR_MECHANICAL_V1,
    SKETCH_GEOMETRY_CIRCLE,
    SKETCH_GEOMETRY_RECTANGLE,
    SKETCH_OUTPUT_TERMS,
    SKETCH_PROPERTY_CENTER,
    SKETCH_PROPERTY_HALF_EXTENTS,
    SKETCH_PROPERTY_RADIUS,
    SKETCH_PROPERTY_ROTATION,
    SKETCH_UNIT_MM,
    SKETCH_UNIT_RAD,
    SKETCH_VALUE_SCALAR,
    SKETCH_VALUE_VECTOR2,
    VFG_CIRCLE,
    VFG_COMPONENT,
    VFG_DECISION,
    VFG_INNER_PROFILE,
    VFG_MODALITY_IMAGE,
    VFG_OUTER_PROFILE,
    VFG_QUANTITY_DEPTH,
    VFG_RELATION_DECISION_SUBJECT,
    VFG_RELATION_THROUGH_EXTENT,
    VFG_REQUIRED_TERMS,
    VFG_ROLE_COMPONENT,
    VFG_ROLE_DECISION,
    VFG_ROLE_PROFILE,
    VFG_ROTATED_RECTANGLE,
    VFG_SAMPLE_BOUNDARY,
    VFG_SAMPLE_CENTER,
    VFG_SAMPLE_CORNER,
    VFG_UNIT_MM,
    as_parametric_term,
    as_sketch_term,
)

_RULE_SET_CONTRACT_DOMAIN = b"vibecad.intent-rules.planar-mechanical-v1.rule-set\0"
_EMITTER_CONTRACT_DOMAIN = b"vibecad.intent-rules.planar-mechanical-v1.emitter\0"


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _contract_payload() -> dict[str, object]:
    all_terms = {
        item.term_ref_id: item
        for item in (
            *VFG_REQUIRED_TERMS,
            *SKETCH_OUTPUT_TERMS,
            *PFG_OUTPUT_TERMS,
            *PLANAR_MECHANICAL_V1_CUSTOM_BRIDGE_TERMS,
        )
    }
    return {
        "admission": {
            "decision": "unique-human-confirmed",
            "frame": "single-image-bound-metric-plane-mm-with-derived-or-confirmed-receipt",
            "outer": "one-rotated-rectangle",
            "inner": "zero-to-sixteen-non-overlapping-contained-circles",
            "depth": "one-positive-exact-mm-measurement",
            "extent": "one-explicit-through-relation-per-inner-circle",
        },
        "limits": {
            "max_coordinate_uncertainty_mm": MAX_COORDINATE_UNCERTAINTY_MM,
            "max_depth_mm": MAX_DEPTH_MM,
            "max_inner_circles": MAX_INNER_CIRCLES,
            "max_rectangle_residual_mm": MAX_RECTANGLE_RESIDUAL_MM,
            "min_advisory_support": MIN_ADVISORY_SUPPORT,
            "min_feature_size_mm": MIN_FEATURE_SIZE_MM,
        },
        "outputs": {
            "parametric": "reference-profiles;extrusion-add;sequential-extrusion-remove",
            "sketch": "rotated-rectangle-and-circles",
        },
        "terms": [
            item.to_mapping()
            for item in sorted(all_terms.values(), key=lambda term: term.term_ref_id)
        ],
        "version": 1,
    }


PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256 = hashlib.sha256(
    _RULE_SET_CONTRACT_DOMAIN + canonical_bytes(_contract_payload())
).hexdigest()
PLANAR_MECHANICAL_V1_EMITTER_CONTRACT_SHA256 = hashlib.sha256(
    _EMITTER_CONTRACT_DOMAIN + canonical_bytes(_contract_payload())
).hexdigest()


@dataclass(frozen=True, slots=True)
class RotatedRectangle:
    center: tuple[float, float]
    half_extents: tuple[float, float]
    rotation_radians: float


@dataclass(frozen=True, slots=True)
class CircleProfile:
    geometry_id: str
    center: tuple[float, float]
    radius: float


@dataclass(frozen=True, slots=True)
class PlanarMechanicalEvidence:
    graph: VisualFeatureGraph
    decision_node_id: str
    component_node_id: str
    outer_geometry_id: str
    depth_measurement_id: str
    frame_id: str
    rectangle: RotatedRectangle
    circles: tuple[CircleProfile, ...]
    depth_mm: float


def _clean(value: float) -> float:
    result = round(value, 9)
    return 0.0 if result == 0 else result


def _supported(item: object) -> bool:
    support = getattr(item, "advisory_support", None)
    return type(support) in {int, float} and support >= MIN_ADVISORY_SUPPORT


def _terms_are_bound(graph: VisualFeatureGraph) -> bool:
    actual = {item.term_ref_id: item for item in graph.ontology_terms}
    return all(
        (bound := actual.get(expected.term_ref_id)) is not None
        and (
            bound.namespace,
            bound.vocabulary_version,
            bound.term_id,
            bound.term_definition_sha256,
        )
        == expected.semantic_identity
        for expected in VFG_REQUIRED_TERMS
    )


def _uncertainty_is_bounded(geometry: GeometryRecord) -> bool:
    for sample in geometry.samples:
        uncertainty = sample.uncertainty
        if uncertainty.kind is MetricUncertaintyKind.ABSOLUTE_BOUND:
            bounds = uncertainty.bounds
        elif uncertainty.kind is MetricUncertaintyKind.AXIS_BOUNDS:
            bounds = uncertainty.bounds
        else:
            return False
        if not bounds or max(bounds) > MAX_COORDINATE_UNCERTAINTY_MM:
            return False
    return True


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _angle_mod_pi(value: float) -> float:
    while value < -math.pi / 2:
        value += math.pi
    while value >= math.pi / 2:
        value -= math.pi
    return value


def _rectangle(geometry: GeometryRecord) -> RotatedRectangle | None:
    if (
        geometry.representation_term_ref_id != VFG_ROTATED_RECTANGLE.term_ref_id
        or VFG_OUTER_PROFILE.term_ref_id not in geometry.term_ref_ids
        or geometry.intrinsic_dimension != 1
        or geometry.closure is not ClosureState.CLOSED
        or geometry.state is not AssertionState.OBSERVED
        or not _supported(geometry)
        or len(geometry.samples) != 4
        or not _uncertainty_is_bounded(geometry)
        or any(VFG_SAMPLE_CORNER.term_ref_id not in item.term_ref_ids for item in geometry.samples)
    ):
        return None
    points = tuple((item.coordinates[0], item.coordinates[1]) for item in geometry.samples)
    center = (
        sum(item[0] for item in points) / 4,
        sum(item[1] for item in points) / 4,
    )
    ordered = tuple(
        sorted(
            points,
            key=lambda item: math.atan2(item[1] - center[1], item[0] - center[0]),
        )
    )
    edges = tuple(
        (
            ordered[(index + 1) % 4][0] - ordered[index][0],
            ordered[(index + 1) % 4][1] - ordered[index][1],
        )
        for index in range(4)
    )
    lengths = tuple(math.hypot(*edge) for edge in edges)
    if min(lengths) < MIN_FEATURE_SIZE_MM:
        return None
    if (
        math.hypot(edges[0][0] + edges[2][0], edges[0][1] + edges[2][1])
        > MAX_RECTANGLE_RESIDUAL_MM
        or math.hypot(edges[1][0] + edges[3][0], edges[1][1] + edges[3][1])
        > MAX_RECTANGLE_RESIDUAL_MM
        or abs(lengths[0] - lengths[2]) > MAX_RECTANGLE_RESIDUAL_MM
        or abs(lengths[1] - lengths[3]) > MAX_RECTANGLE_RESIDUAL_MM
        or abs(edges[0][0] * edges[1][0] + edges[0][1] * edges[1][1])
        / max(lengths[0], lengths[1])
        > MAX_RECTANGLE_RESIDUAL_MM
    ):
        return None
    candidates = (
        (lengths[0], lengths[1], _angle_mod_pi(math.atan2(edges[0][1], edges[0][0]))),
        (lengths[1], lengths[0], _angle_mod_pi(math.atan2(edges[1][1], edges[1][0]))),
    )
    width, height, angle = max(
        candidates,
        key=lambda item: (round(item[0] - item[1], 9), -abs(item[2]), -item[2]),
    )
    if width + MAX_RECTANGLE_RESIDUAL_MM < height:
        width, height, angle = height, width, _angle_mod_pi(angle + math.pi / 2)
    return RotatedRectangle(
        center=(_clean(center[0]), _clean(center[1])),
        half_extents=(_clean(width / 2), _clean(height / 2)),
        rotation_radians=_clean(angle),
    )


def _circle(geometry: GeometryRecord) -> CircleProfile | None:
    if (
        geometry.representation_term_ref_id != VFG_CIRCLE.term_ref_id
        or VFG_INNER_PROFILE.term_ref_id not in geometry.term_ref_ids
        or geometry.intrinsic_dimension != 1
        or geometry.closure is not ClosureState.CLOSED
        or geometry.state is not AssertionState.OBSERVED
        or not _supported(geometry)
        or len(geometry.samples) != 2
        or not _uncertainty_is_bounded(geometry)
    ):
        return None
    centers = [
        item for item in geometry.samples if VFG_SAMPLE_CENTER.term_ref_id in item.term_ref_ids
    ]
    boundaries = [
        item for item in geometry.samples if VFG_SAMPLE_BOUNDARY.term_ref_id in item.term_ref_ids
    ]
    if len(centers) != 1 or len(boundaries) != 1:
        return None
    center = (centers[0].coordinates[0], centers[0].coordinates[1])
    boundary = (boundaries[0].coordinates[0], boundaries[0].coordinates[1])
    radius = _distance(center, boundary)
    if radius < MIN_FEATURE_SIZE_MM:
        return None
    return CircleProfile(
        geometry_id=geometry.geometry_id,
        center=(_clean(center[0]), _clean(center[1])),
        radius=_clean(radius),
    )


def _relation_matches(
    relation: FeatureRelation,
    *,
    relation_term_ref_id: str,
    endpoints: tuple[tuple[str, GraphElementKind, str], ...],
) -> bool:
    actual = tuple(
        (item.role_term_ref_id, item.element.kind, item.element.element_id)
        for item in relation.endpoints
    )
    return (
        relation.relation_term_ref_id == relation_term_ref_id
        and relation.state is AssertionState.OBSERVED
        and _supported(relation)
        and actual == endpoints
    )


def _inside(rectangle: RotatedRectangle, circle: CircleProfile) -> bool:
    dx = circle.center[0] - rectangle.center[0]
    dy = circle.center[1] - rectangle.center[1]
    cosine = math.cos(rectangle.rotation_radians)
    sine = math.sin(rectangle.rotation_radians)
    local_x = cosine * dx + sine * dy
    local_y = -sine * dx + cosine * dy
    return (
        abs(local_x) + circle.radius
        <= rectangle.half_extents[0] + MAX_RECTANGLE_RESIDUAL_MM
        and abs(local_y) + circle.radius
        <= rectangle.half_extents[1] + MAX_RECTANGLE_RESIDUAL_MM
    )


def analyze_visual_feature_graph(graph: VisualFeatureGraph) -> PlanarMechanicalEvidence | None:
    """Return exact admitted evidence, or ``None`` for any untrusted/unknown case."""

    if type(graph) is not VisualFeatureGraph or not _terms_are_bound(graph):
        return None
    provenance_by_id = {item.provenance_id: item for item in graph.provenance}
    decision_nodes = [
        item
        for item in graph.nodes
        if VFG_DECISION.term_ref_id in item.term_ref_ids
    ]
    decisions = [
        item
        for item in decision_nodes
        if item.state is AssertionState.OBSERVED
        and any(
            provenance_by_id.get(identifier) is not None
            and provenance_by_id[identifier].kind is ProvenanceKind.HUMAN_CONFIRMATION
            for identifier in item.provenance_ids
        )
    ]
    component_nodes = [
        item
        for item in graph.nodes
        if VFG_COMPONENT.term_ref_id in item.term_ref_ids
    ]
    components = [
        item
        for item in component_nodes
        if item.layer is EntityLayer.COMPONENT
        and item.state is AssertionState.OBSERVED
        and _supported(item)
    ]
    if (
        len(decision_nodes) != 1
        or len(decisions) != 1
        or len(component_nodes) != 1
        or len(components) != 1
    ):
        return None
    decision = decisions[0]
    component = components[0]

    decision_relations = [
        item
        for item in graph.relations
        if item.relation_term_ref_id == VFG_RELATION_DECISION_SUBJECT.term_ref_id
    ]
    if len(decision_relations) != 1 or not _relation_matches(
        decision_relations[0],
        relation_term_ref_id=VFG_RELATION_DECISION_SUBJECT.term_ref_id,
        endpoints=(
            (VFG_ROLE_DECISION.term_ref_id, GraphElementKind.NODE, decision.node_id),
            (VFG_ROLE_COMPONENT.term_ref_id, GraphElementKind.NODE, component.node_id),
        ),
    ):
        return None

    geometry_by_id = {item.geometry_id: item for item in graph.geometries}
    selected = [geometry_by_id.get(identifier) for identifier in component.geometry_ids]
    if any(item is None for item in selected):
        return None
    selected_geometries = tuple(item for item in selected if item is not None)
    outers = [
        item
        for item in selected_geometries
        if VFG_OUTER_PROFILE.term_ref_id in item.term_ref_ids
    ]
    inner_geometries = [
        item
        for item in selected_geometries
        if VFG_INNER_PROFILE.term_ref_id in item.term_ref_ids
    ]
    if (
        len(outers) != 1
        or len(inner_geometries) > MAX_INNER_CIRCLES
        or len(selected_geometries) != 1 + len(inner_geometries)
    ):
        return None
    outer = outers[0]
    frame_by_id = {item.frame_id: item for item in graph.frames}
    frame = frame_by_id.get(outer.frame_id)
    source_by_id = {item.source_id: item for item in graph.sources}
    source = source_by_id.get(frame.source_id) if frame is not None else None
    if (
        frame is None
        or type(frame.binding) is not MetricPlaneFrameBinding
        or frame.binding.unit != "mm"
        or len(component.source_ids) != 1
        or frame.source_id != component.source_ids[0]
        or source is None
        or VFG_MODALITY_IMAGE.term_ref_id not in source.modality_term_ref_ids
        or not any(
            provenance_by_id.get(identifier) is not None
            and provenance_by_id[identifier].kind
            in {ProvenanceKind.DETERMINISTIC_DERIVATION, ProvenanceKind.HUMAN_CONFIRMATION}
            for identifier in frame.provenance_ids
        )
        or any(item.frame_id != frame.frame_id for item in inner_geometries)
    ):
        return None
    rectangle = _rectangle(outer)
    circles = tuple(
        sorted(
            (candidate for item in inner_geometries if (candidate := _circle(item)) is not None),
            key=lambda item: item.geometry_id,
        )
    )
    if rectangle is None or len(circles) != len(inner_geometries):
        return None
    if any(not _inside(rectangle, item) for item in circles):
        return None
    for index, first in enumerate(circles):
        for second in circles[index + 1 :]:
            if _distance(first.center, second.center) < first.radius + second.radius:
                return None

    depth_measurements = [
        item
        for item in graph.measurements
        if item.quantity_term_ref_id == VFG_QUANTITY_DEPTH.term_ref_id
        and any(
            target.kind is GraphElementKind.NODE and target.element_id == component.node_id
            for target in item.targets
        )
    ]
    if len(depth_measurements) != 1:
        return None
    depth = depth_measurements[0]
    if (
        len(depth.targets) != 1
        or depth.unit_term_ref_id != VFG_UNIT_MM.term_ref_id
        or depth.targets[0].kind is not GraphElementKind.NODE
        or depth.targets[0].element_id != component.node_id
        or depth.state is not AssertionState.OBSERVED
        or not _supported(depth)
        or depth.estimate.kind is not MeasurementEstimateKind.EXACT
        or len(depth.estimate.central) != 1
        or not MIN_FEATURE_SIZE_MM <= depth.estimate.central[0] <= MAX_DEPTH_MM
        or depth.frame_ids != (frame.frame_id,)
        or depth.transform_ids
    ):
        return None

    through_relations = [
        item
        for item in graph.relations
        if item.relation_term_ref_id == VFG_RELATION_THROUGH_EXTENT.term_ref_id
    ]
    if len(through_relations) != len(circles):
        return None
    expected_through = {
        (
            (VFG_ROLE_COMPONENT.term_ref_id, GraphElementKind.NODE, component.node_id),
            (VFG_ROLE_PROFILE.term_ref_id, GraphElementKind.GEOMETRY, item.geometry_id),
        )
        for item in circles
    }
    actual_through = {
        tuple(
            (endpoint.role_term_ref_id, endpoint.element.kind, endpoint.element.element_id)
            for endpoint in relation.endpoints
        )
        for relation in through_relations
        if relation.state is AssertionState.OBSERVED and _supported(relation)
    }
    if actual_through != expected_through:
        return None
    selected_subjects = {
        (GraphElementKind.NODE, decision.node_id),
        (GraphElementKind.NODE, component.node_id),
        (GraphElementKind.GEOMETRY, outer.geometry_id),
        *((GraphElementKind.GEOMETRY, item.geometry_id) for item in circles),
    }
    if any(
        any(
            (subject.kind, subject.element_id) in selected_subjects
            for subject in hypothesis.subject_refs
        )
        for hypothesis in graph.hypothesis_sets
    ):
        return None

    return PlanarMechanicalEvidence(
        graph=graph,
        decision_node_id=decision.node_id,
        component_node_id=component.node_id,
        outer_geometry_id=outer.geometry_id,
        depth_measurement_id=depth.measurement_id,
        frame_id=frame.frame_id,
        rectangle=rectangle,
        circles=circles,
        depth_mm=_clean(depth.estimate.central[0]),
    )


def _sketch_property(
    property_term: BridgeTermRef,
    value_type: BridgeTermRef,
    value_kind: SketchValueKind,
    value: object,
    unit_term: BridgeTermRef,
) -> SketchProperty:
    return SketchProperty(
        property_term_ref_id=property_term.term_ref_id,
        typed_value=SketchTypedValue(
            value_type_term_ref_id=value_type.term_ref_id,
            value_kind=value_kind,
            value=value,
        ),
        unit_term_ref_id=unit_term.term_ref_id,
    )


def build_sketch_intent_graph(evidence: PlanarMechanicalEvidence) -> SketchIntentGraph:
    digest = evidence.graph.graph_digest
    rectangle = evidence.rectangle
    geometries = [
        SketchGeometryNode(
            geometry_id="geometry.outer",
            geometry_term_ref_id=SKETCH_GEOMETRY_RECTANGLE.term_ref_id,
            properties=(
                _sketch_property(
                    SKETCH_PROPERTY_CENTER,
                    SKETCH_VALUE_VECTOR2,
                    SketchValueKind.VECTOR,
                    rectangle.center,
                    SKETCH_UNIT_MM,
                ),
                _sketch_property(
                    SKETCH_PROPERTY_HALF_EXTENTS,
                    SKETCH_VALUE_VECTOR2,
                    SketchValueKind.VECTOR,
                    rectangle.half_extents,
                    SKETCH_UNIT_MM,
                ),
                _sketch_property(
                    SKETCH_PROPERTY_ROTATION,
                    SKETCH_VALUE_SCALAR,
                    SketchValueKind.NUMBER,
                    rectangle.rotation_radians,
                    SKETCH_UNIT_RAD,
                ),
            ),
        )
    ]
    for index, circle in enumerate(evidence.circles):
        geometries.append(
            SketchGeometryNode(
                geometry_id=f"geometry.inner.{index:03d}",
                geometry_term_ref_id=SKETCH_GEOMETRY_CIRCLE.term_ref_id,
                properties=(
                    _sketch_property(
                        SKETCH_PROPERTY_CENTER,
                        SKETCH_VALUE_VECTOR2,
                        SketchValueKind.VECTOR,
                        circle.center,
                        SKETCH_UNIT_MM,
                    ),
                    _sketch_property(
                        SKETCH_PROPERTY_RADIUS,
                        SKETCH_VALUE_SCALAR,
                        SketchValueKind.NUMBER,
                        circle.radius,
                        SKETCH_UNIT_MM,
                    ),
                ),
            )
        )
    return SketchIntentGraph(
        schema_version=SKETCH_INTENT_SCHEMA_VERSION,
        graph_id=f"graph.sketch.pm1.{digest}",
        sketch_id=f"sketch.pm1.{digest}",
        terms=tuple(as_sketch_term(item) for item in SKETCH_OUTPUT_TERMS),
        geometries=tuple(geometries),
        anchors=(),
        constraints=(),
    )


def _pfg_port(
    node: str,
    name: str,
    role: BridgeTermRef,
    value_type: BridgeTermRef,
) -> FeatureInputPortV2:
    return FeatureInputPortV2(
        port_id=f"port.{node}.{name}",
        semantic_role_term_ref_id=role.term_ref_id,
        value_type_term_ref_id=value_type.term_ref_id,
        minimum_cardinality=1,
        maximum_cardinality=1,
        ordered=False,
    )


def _pfg_result(identifier: str, role: BridgeTermRef, value_type: BridgeTermRef) -> FeatureResultV2:
    return FeatureResultV2(
        result_id=identifier,
        semantic_role_term_ref_id=role.term_ref_id,
        value_type_term_ref_id=value_type.term_ref_id,
    )


def build_parametric_feature_graph(
    evidence: PlanarMechanicalEvidence,
    sketch: SketchIntentGraph,
) -> ParametricFeatureGraphV2:
    sketch_payload = encode_sketch_intent_graph(sketch)
    sketch_sha256 = hashlib.sha256(sketch_payload).hexdigest()
    depth_parameter = DesignParameterV2(
        parameter_id="parameter.depth",
        name="Depth",
        semantic_role_term_ref_id=PFG_PARAMETER_DEPTH.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value.depth",
            value_type_term_ref_id=PFG_TYPE_LENGTH_MM.term_ref_id,
            encoding_term_ref_id=PFG_ENCODING_CANONICAL_JSON.term_ref_id,
            value=evidence.depth_mm,
        ),
    )
    parameters = [depth_parameter]
    if evidence.circles:
        parameters.append(
            DesignParameterV2(
                parameter_id="parameter.extent-through",
                name="Through all",
                semantic_role_term_ref_id=PFG_PARAMETER_EXTENT.term_ref_id,
                value=TermTypedValueV2.from_value(
                    value_id="value.extent-through",
                    value_type_term_ref_id=PFG_TYPE_EXTENT_THROUGH.term_ref_id,
                    encoding_term_ref_id=PFG_ENCODING_CANONICAL_JSON.term_ref_id,
                    value="through_all",
                ),
            )
        )
    profile_results = [
        _pfg_result(
            "result.profiles.outer",
            PFG_RESULT_OUTER_PROFILE,
            PFG_TYPE_WIRE,
        )
    ]
    profile_results.extend(
        _pfg_result(
            f"result.profiles.inner.{index:03d}",
            PFG_RESULT_INNER_PROFILE,
            PFG_TYPE_WIRE,
        )
        for index in range(len(evidence.circles))
    )
    nodes = [
        FeatureNodeV2(
            node_id="node.profiles",
            body_id="body.main",
            name="Sketch profiles",
            intent=FeatureIntentV2(
                structural_kind_term_ref_id=PFG_STRUCTURE_REFERENCE.term_ref_id,
                family_term_ref_id=PFG_FAMILY_REFERENCE.term_ref_id,
                operation_term_ref_id=PFG_OPERATION_REFERENCE_PROFILES.term_ref_id,
                input_ports=(
                    _pfg_port(
                        "profiles",
                        "external",
                        PFG_PORT_EXTERNAL,
                        PFG_TYPE_SKETCH_DOCUMENT,
                    ),
                ),
                references=(
                    FeatureReferenceBindingV2(
                        binding_id="binding.profiles.external",
                        port_id="port.profiles.external",
                        reference_id="reference.sketch",
                    ),
                ),
            ),
            results=tuple(profile_results),
        ),
        FeatureNodeV2(
            node_id="node.add",
            body_id="body.main",
            name="Add extrusion",
            intent=FeatureIntentV2(
                structural_kind_term_ref_id=PFG_STRUCTURE_FEATURE.term_ref_id,
                family_term_ref_id=PFG_FAMILY_EXTRUSION.term_ref_id,
                operation_term_ref_id=PFG_OPERATION_ADD.term_ref_id,
                input_ports=(
                    _pfg_port("add", "profile", PFG_PORT_PROFILE, PFG_TYPE_WIRE),
                    _pfg_port("add", "depth", PFG_PORT_DEPTH, PFG_TYPE_LENGTH_MM),
                ),
                dependencies=(
                    FeatureDependencyV2(
                        dependency_id="dependency.add.profile",
                        port_id="port.add.profile",
                        upstream_node_id="node.profiles",
                        upstream_result_id="result.profiles.outer",
                    ),
                ),
                parameter_bindings=(
                    FeatureParameterBindingV2(
                        binding_id="binding.add.depth",
                        port_id="port.add.depth",
                        parameter_id="parameter.depth",
                    ),
                ),
            ),
            results=(
                _pfg_result("result.add.solid", PFG_RESULT_SOLID, PFG_TYPE_SOLID),
            ),
        ),
    ]
    upstream_node = "node.add"
    upstream_result = "result.add.solid"
    for index in range(len(evidence.circles)):
        suffix = f"{index:03d}"
        node_id = f"node.remove.{suffix}"
        result_id = f"result.remove.{suffix}.solid"
        nodes.append(
            FeatureNodeV2(
                node_id=node_id,
                body_id="body.main",
                name=f"Remove circle {index + 1}",
                intent=FeatureIntentV2(
                    structural_kind_term_ref_id=PFG_STRUCTURE_FEATURE.term_ref_id,
                    family_term_ref_id=PFG_FAMILY_EXTRUSION.term_ref_id,
                    operation_term_ref_id=PFG_OPERATION_REMOVE.term_ref_id,
                    input_ports=(
                        _pfg_port(f"remove.{suffix}", "base", PFG_PORT_BASE, PFG_TYPE_SOLID),
                        _pfg_port(f"remove.{suffix}", "profile", PFG_PORT_PROFILE, PFG_TYPE_WIRE),
                        _pfg_port(
                            f"remove.{suffix}",
                            "extent",
                            PFG_PORT_EXTENT,
                            PFG_TYPE_EXTENT_THROUGH,
                        ),
                    ),
                    dependencies=(
                        FeatureDependencyV2(
                            dependency_id=f"dependency.remove.{suffix}.base",
                            port_id=f"port.remove.{suffix}.base",
                            upstream_node_id=upstream_node,
                            upstream_result_id=upstream_result,
                        ),
                        FeatureDependencyV2(
                            dependency_id=f"dependency.remove.{suffix}.profile",
                            port_id=f"port.remove.{suffix}.profile",
                            upstream_node_id="node.profiles",
                            upstream_result_id=f"result.profiles.inner.{suffix}",
                        ),
                    ),
                    parameter_bindings=(
                        FeatureParameterBindingV2(
                            binding_id=f"binding.remove.{suffix}.extent",
                            port_id=f"port.remove.{suffix}.extent",
                            parameter_id="parameter.extent-through",
                        ),
                    ),
                ),
                results=(_pfg_result(result_id, PFG_RESULT_SOLID, PFG_TYPE_SOLID),),
            )
        )
        upstream_node = node_id
        upstream_result = result_id
    return ParametricFeatureGraphV2(
        graph_id=f"graph.parametric.pm1.{evidence.graph.graph_digest}",
        name="Planar mechanical component",
        terms=tuple(as_parametric_term(item) for item in PFG_OUTPUT_TERMS),
        bodies=(FeatureBodyV2(body_id="body.main", name="Main body"),),
        parameters=tuple(parameters),
        references=(
            SemanticReferenceV2(
                reference_id="reference.sketch",
                scope=SemanticReferenceScope.EXTERNAL,
                semantic_role_term_ref_id=PFG_PORT_EXTERNAL.term_ref_id,
                value_type_term_ref_id=PFG_TYPE_SKETCH_DOCUMENT.term_ref_id,
                locator_term_ref_id=PFG_LOCATOR_SKETCH_DOCUMENT.term_ref_id,
                source_content_sha256=sketch_sha256,
            ),
        ),
        nodes=tuple(nodes),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection.primary",
                node_id=upstream_node,
                result_id=upstream_result,
            ),
        ),
    )


def build_intent_graphs(
    evidence: PlanarMechanicalEvidence,
) -> tuple[SketchIntentGraph, ParametricFeatureGraphV2]:
    sketch = build_sketch_intent_graph(evidence)
    return sketch, build_parametric_feature_graph(evidence, sketch)


def _proof_terms() -> tuple[BridgeTermRef, ...]:
    return tuple(
        sorted(
            {
                item.term_ref_id: item
                for item in (
                    *PLANAR_MECHANICAL_V1_CUSTOM_BRIDGE_TERMS,
                    VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM,
                    VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM,
                    VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM,
                    SKETCH_ROOT_SELECTOR_TERM,
                    PFG_SELECTOR_GRAPH_RESULT,
                )
            }.values(),
            key=lambda item: item.term_ref_id,
        )
    )


class PlanarMechanicalV1RuleSet:
    """One deterministic, non-iterative reviewed rule set."""

    __slots__ = ("_descriptor",)

    def __init__(self) -> None:
        self._descriptor = IntentRuleSetDescriptor(
            rule_set_id="planar_mechanical_v1",
            rule_set_version="1.0.0",
            rule_set_contract_sha256=PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256,
            rule_set_term=RULE_SET_PLANAR_MECHANICAL_V1,
            input_signatures=(
                DocumentSignature(
                    role_term=ROLE_VISUAL_EVIDENCE,
                    schema_term=VISUAL_FEATURE_GRAPH_SCHEMA_TERM,
                ),
            ),
            output_signatures=(
                DocumentSignature(
                    role_term=ROLE_SKETCH_INTENT,
                    schema_term=SKETCH_INTENT_GRAPH_SCHEMA_TERM,
                ),
                DocumentSignature(
                    role_term=ROLE_PARAMETRIC_INTENT,
                    schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
                ),
            ),
            rules=(
                IntentRuleDescriptor(
                    rule_term=RULE_COMPILE_SKETCH,
                    predicate_term=PREDICATE_SKETCH_COMPILED,
                    emitter_contract_sha256=PLANAR_MECHANICAL_V1_EMITTER_CONTRACT_SHA256,
                    maximum_applications=1,
                ),
                IntentRuleDescriptor(
                    rule_term=RULE_COMPILE_PARAMETRIC,
                    predicate_term=PREDICATE_PARAMETRIC_COMPILED,
                    emitter_contract_sha256=PLANAR_MECHANICAL_V1_EMITTER_CONTRACT_SHA256,
                    maximum_applications=1,
                ),
            ),
        )

    @property
    def descriptor(self) -> IntentRuleSetDescriptor:
        return self._descriptor

    def emit(self, context: RuleSetCompileContext) -> RuleSetEmission:
        if len(context.input_documents) != 1 or len(context.requested_outputs) != 2:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/context")
        input_document, input_payload = context.input_documents[0]
        graph = decode_visual_feature_graph(input_payload)
        evidence = analyze_visual_feature_graph(graph)
        if evidence is None:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/evidence")
        expected_decision = SubjectRef(
            artifact_id=input_document.artifact_id,
            selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM.term_ref_id,
            selector_id=evidence.decision_node_id,
        )
        if (
            context.selection.rule_set_term.semantic_identity
            != RULE_SET_PLANAR_MECHANICAL_V1.semantic_identity
            or context.selection.decision_subjects != (expected_decision,)
        ):
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/selection")
        if context.max_rule_applications < 2 or context.max_subject_lookups < 6:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/pm1/budget")

        sketch, parametric = build_intent_graphs(evidence)
        sketch_payload = encode_sketch_intent_graph(sketch)
        parametric_payload = encode_parametric_feature_graph_v2(parametric)
        if len(sketch_payload) + len(parametric_payload) > context.max_output_bytes:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/pm1/output")
        output_by_schema = {
            signature.schema_term.semantic_identity: (output_id, signature)
            for output_id, signature in context.requested_outputs
        }
        try:
            sketch_output, sketch_signature = output_by_schema[
                SKETCH_INTENT_GRAPH_SCHEMA_TERM.semantic_identity
            ]
            parametric_output, parametric_signature = output_by_schema[
                PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.semantic_identity
            ]
        except KeyError:
            _fail(IntentBridgeErrorCode.AUTHORITY_VIOLATION, "/pm1/outputs")

        suffix = graph.graph_digest
        sketch_artifact = f"intent.sketch.pm1.{suffix}"
        parametric_artifact = f"intent.parametric.pm1.{suffix}"
        documents = (
            CompiledIntentDocument.create(
                output_id=sketch_output,
                artifact_id=sketch_artifact,
                role_term_ref_id=sketch_signature.role_term.term_ref_id,
                schema_term_ref_id=sketch_signature.schema_term.term_ref_id,
                document_id=sketch.graph_id,
                document_digest=sketch.graph_sha256,
                media_type=SKETCH_INTENT_GRAPH_MEDIA_TYPE,
                payload=sketch_payload,
            ),
            CompiledIntentDocument.create(
                output_id=parametric_output,
                artifact_id=parametric_artifact,
                role_term_ref_id=parametric_signature.role_term.term_ref_id,
                schema_term_ref_id=parametric_signature.schema_term.term_ref_id,
                document_id=parametric.graph_id,
                document_digest=parametric.graph_sha256,
                media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
                payload=parametric_payload,
            ),
        )
        premises = (
            ProofEndpoint(
                ordinal=0,
                role_term_ref_id=ROLE_DECISION.term_ref_id,
                subject=expected_decision,
            ),
            ProofEndpoint(
                ordinal=1,
                role_term_ref_id=ROLE_COMPONENT.term_ref_id,
                subject=SubjectRef(
                    artifact_id=input_document.artifact_id,
                    selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_NODE_SELECTOR_TERM.term_ref_id,
                    selector_id=evidence.component_node_id,
                ),
            ),
            ProofEndpoint(
                ordinal=2,
                role_term_ref_id=ROLE_OUTER_PROFILE.term_ref_id,
                subject=SubjectRef(
                    artifact_id=input_document.artifact_id,
                    selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_GEOMETRY_SELECTOR_TERM.term_ref_id,
                    selector_id=evidence.outer_geometry_id,
                ),
            ),
            ProofEndpoint(
                ordinal=3,
                role_term_ref_id=ROLE_DEPTH.term_ref_id,
                subject=SubjectRef(
                    artifact_id=input_document.artifact_id,
                    selector_kind_term_ref_id=VISUAL_FEATURE_GRAPH_MEASUREMENT_SELECTOR_TERM.term_ref_id,
                    selector_id=evidence.depth_measurement_id,
                ),
            ),
        )
        assertions = (
            ProofAssertion(
                assertion_id="assertion.pm1.sketch",
                predicate_term_ref_id=PREDICATE_SKETCH_COMPILED.term_ref_id,
                rule_term_ref_id=RULE_COMPILE_SKETCH.term_ref_id,
                premises=premises,
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_SKETCH_INTENT.term_ref_id,
                        subject=SubjectRef(
                            artifact_id=sketch_artifact,
                            selector_kind_term_ref_id=SKETCH_ROOT_SELECTOR_TERM.term_ref_id,
                            selector_id=sketch.sketch_id,
                        ),
                    ),
                ),
            ),
            ProofAssertion(
                assertion_id="assertion.pm1.parametric",
                predicate_term_ref_id=PREDICATE_PARAMETRIC_COMPILED.term_ref_id,
                rule_term_ref_id=RULE_COMPILE_PARAMETRIC.term_ref_id,
                premises=premises,
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=ROLE_PARAMETRIC_INTENT.term_ref_id,
                        subject=SubjectRef(
                            artifact_id=parametric_artifact,
                            selector_kind_term_ref_id=PFG_SELECTOR_GRAPH_RESULT.term_ref_id,
                            selector_id="selection.primary",
                        ),
                    ),
                ),
            ),
        )
        return RuleSetEmission(
            documents=documents,
            terms=_proof_terms(),
            assertions=assertions,
        )


__all__ = [
    "CircleProfile",
    "PLANAR_MECHANICAL_V1_EMITTER_CONTRACT_SHA256",
    "PLANAR_MECHANICAL_V1_RULE_SET_CONTRACT_SHA256",
    "PlanarMechanicalEvidence",
    "PlanarMechanicalV1RuleSet",
    "RotatedRectangle",
    "analyze_visual_feature_graph",
    "build_intent_graphs",
    "build_parametric_feature_graph",
    "build_sketch_intent_graph",
]
