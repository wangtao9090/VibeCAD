"""Content-bound ontology for the first planar-mechanical intent rule pack.

The terms in this module describe evidence, proof roles, editable sketch
geometry, and generic parametric intent.  They deliberately do not name a CAD
backend or a backend-native feature type.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from vibecad.intent_bridge.contracts import BridgeTermRef
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.sketch.ontology import SketchOntologyTermRef
from vibecad.visual.feature_graph import OntologyTermRef

ONTOLOGY_NAMESPACE = "org.vibecad.planar-mechanical"
ONTOLOGY_VERSION = "1.0.0"

MAX_INNER_CIRCLES = 16
MAX_COORDINATE_UNCERTAINTY_MM = 0.25
MAX_RECTANGLE_RESIDUAL_MM = 0.25
MIN_ADVISORY_SUPPORT = 0.90
MIN_FEATURE_SIZE_MM = 0.50
MAX_DEPTH_MM = 100_000.0

_DEFINITION_DOMAIN = b"vibecad.intent-rules.planar-mechanical-v1.term\0"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _term(term_ref_id: str, term_id: str, definition: str) -> BridgeTermRef:
    body = {
        "definition": definition,
        "namespace": ONTOLOGY_NAMESPACE,
        "term_id": term_id,
        "vocabulary_version": ONTOLOGY_VERSION,
    }
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=ONTOLOGY_NAMESPACE,
        vocabulary_version=ONTOLOGY_VERSION,
        term_id=term_id,
        term_definition_sha256=hashlib.sha256(_DEFINITION_DOMAIN + _canonical(body)).hexdigest(),
    )


def as_visual_term(term: BridgeTermRef) -> OntologyTermRef:
    return OntologyTermRef(**term.to_mapping())


def as_sketch_term(term: BridgeTermRef) -> SketchOntologyTermRef:
    return SketchOntologyTermRef(**term.to_mapping())


def as_parametric_term(term: BridgeTermRef) -> SemanticTermRefV2:
    return SemanticTermRefV2(**term.to_mapping())


def _ordered(terms: Iterable[BridgeTermRef]) -> tuple[BridgeTermRef, ...]:
    return tuple(sorted(terms, key=lambda item: item.term_ref_id))


# Compiler/proof vocabulary.
ROLE_VISUAL_EVIDENCE = _term(
    "pm1.role.visual-evidence",
    "role.visual-evidence",
    "A canonical VisualFeatureGraph used as evidence for this rule pack.",
)
ROLE_DECISION = _term(
    "pm1.role.decision",
    "role.evidence.decision",
    "The unique human-confirmed decision adopting the planar mechanical interpretation.",
)
ROLE_COMPONENT = _term(
    "pm1.role.component",
    "role.evidence.component",
    "The selected planar mechanical component evidence node.",
)
ROLE_OUTER_PROFILE = _term(
    "pm1.role.outer-profile",
    "role.evidence.outer-profile",
    "The selected closed outer profile geometry evidence.",
)
ROLE_DEPTH = _term(
    "pm1.role.depth",
    "role.evidence.depth",
    "The selected explicit component depth measurement evidence.",
)
ROLE_SKETCH_INTENT = _term(
    "pm1.role.sketch-intent",
    "role.intent.sketch",
    "The canonical editable planar sketch intent produced by this rule pack.",
)
ROLE_PARAMETRIC_INTENT = _term(
    "pm1.role.parametric-intent",
    "role.intent.parametric",
    "The canonical generic parametric feature intent produced by this rule pack.",
)
RULE_SET_PLANAR_MECHANICAL_V1 = _term(
    "pm1.rule-set",
    "rule-set.planar-mechanical-v1",
    "The reviewed planar mechanical v1 rule set.",
)
RULE_COMPILE_SKETCH = _term(
    "pm1.rule.compile-sketch",
    "rule.compile-planar-sketch-v1",
    "Compile admitted planar evidence into one canonical editable sketch graph.",
)
PREDICATE_SKETCH_COMPILED = _term(
    "pm1.predicate.sketch-compiled",
    "predicate.planar-sketch-compiled-v1",
    "The concluded sketch bytes are the exact deterministic compilation of the premises.",
)
RULE_COMPILE_PARAMETRIC = _term(
    "pm1.rule.compile-parametric",
    "rule.compile-planar-parametric-v1",
    "Compile admitted planar evidence and its sketch into a generic parametric feature graph.",
)
PREDICATE_PARAMETRIC_COMPILED = _term(
    "pm1.predicate.parametric-compiled",
    "predicate.planar-parametric-compiled-v1",
    "The concluded parametric bytes are the exact deterministic compilation of the premises.",
)


# VisualFeatureGraph evidence vocabulary.
VFG_MODALITY_IMAGE = _term(
    "pm1.vfg.modality.image",
    "visual.modality.image",
    "A still-image evidence source.",
)
VFG_DECISION = _term(
    "pm1.vfg.decision",
    "visual.decision.planar-mechanical-v1",
    "A human-confirmable decision to adopt the planar mechanical v1 interpretation.",
)
VFG_COMPONENT = _term(
    "pm1.vfg.component",
    "visual.entity.planar-mechanical-component",
    "One planar mechanical component represented in one metric plane.",
)
VFG_OUTER_PROFILE = _term(
    "pm1.vfg.outer-profile",
    "visual.profile.outer",
    "The additive outer boundary of the component.",
)
VFG_INNER_PROFILE = _term(
    "pm1.vfg.inner-profile",
    "visual.profile.inner",
    "A subtractive inner circular boundary of the component.",
)
VFG_ROTATED_RECTANGLE = _term(
    "pm1.vfg.rotated-rectangle",
    "visual.geometry.rotated-rectangle",
    "A closed rectangle represented by four metric corner samples.",
)
VFG_CIRCLE = _term(
    "pm1.vfg.circle",
    "visual.geometry.circle",
    "A closed circle represented by metric center and boundary samples.",
)
VFG_SAMPLE_CORNER = _term(
    "pm1.vfg.sample.corner",
    "visual.sample-role.corner",
    "A rectangle corner coordinate sample.",
)
VFG_SAMPLE_CENTER = _term(
    "pm1.vfg.sample.center",
    "visual.sample-role.center",
    "A circle center coordinate sample.",
)
VFG_SAMPLE_BOUNDARY = _term(
    "pm1.vfg.sample.boundary",
    "visual.sample-role.boundary",
    "A coordinate sample on a circle boundary.",
)
VFG_RELATION_DECISION_SUBJECT = _term(
    "pm1.vfg.relation.decision-subject",
    "visual.relation.decision-subject",
    "Connects the confirmed decision to exactly one selected component.",
)
VFG_RELATION_THROUGH_EXTENT = _term(
    "pm1.vfg.relation.through",
    "visual.relation.profile-through-component",
    "Declares that one inner profile removes material through the full component depth.",
)
VFG_ROLE_DECISION = _term(
    "pm1.vfg.role.decision",
    "visual.role.decision",
    "Decision endpoint role.",
)
VFG_ROLE_COMPONENT = _term(
    "pm1.vfg.role.component",
    "visual.role.component",
    "Component endpoint role.",
)
VFG_ROLE_PROFILE = _term(
    "pm1.vfg.role.profile",
    "visual.role.profile",
    "Profile endpoint role.",
)
VFG_QUANTITY_DEPTH = _term(
    "pm1.vfg.quantity.depth",
    "visual.quantity.component-depth",
    "Positive component extrusion depth.",
)
VFG_UNIT_MM = _term(
    "pm1.vfg.unit.mm",
    "unit.millimetre",
    "Millimetre length unit.",
)

VFG_REQUIRED_TERMS = _ordered(
    (
        VFG_MODALITY_IMAGE,
        VFG_DECISION,
        VFG_COMPONENT,
        VFG_OUTER_PROFILE,
        VFG_INNER_PROFILE,
        VFG_ROTATED_RECTANGLE,
        VFG_CIRCLE,
        VFG_SAMPLE_CORNER,
        VFG_SAMPLE_CENTER,
        VFG_SAMPLE_BOUNDARY,
        VFG_RELATION_DECISION_SUBJECT,
        VFG_RELATION_THROUGH_EXTENT,
        VFG_ROLE_DECISION,
        VFG_ROLE_COMPONENT,
        VFG_ROLE_PROFILE,
        VFG_QUANTITY_DEPTH,
        VFG_UNIT_MM,
    )
)


# SketchIntentGraph output vocabulary.
SKETCH_GEOMETRY_RECTANGLE = _term(
    "pm1.sketch.geometry.rectangle",
    "sketch.geometry.rotated-rectangle",
    "Editable rotated rectangle defined by center, half extents, and rotation.",
)
SKETCH_GEOMETRY_CIRCLE = _term(
    "pm1.sketch.geometry.circle",
    "sketch.geometry.circle",
    "Editable circle defined by center and radius.",
)
SKETCH_PROPERTY_CENTER = _term(
    "pm1.sketch.property.center",
    "sketch.property.center",
    "Two-dimensional center coordinate.",
)
SKETCH_PROPERTY_HALF_EXTENTS = _term(
    "pm1.sketch.property.half-extents",
    "sketch.property.half-extents",
    "Positive rectangle half width and half height.",
)
SKETCH_PROPERTY_ROTATION = _term(
    "pm1.sketch.property.rotation",
    "sketch.property.rotation",
    "Counter-clockwise planar rotation angle.",
)
SKETCH_PROPERTY_RADIUS = _term(
    "pm1.sketch.property.radius",
    "sketch.property.radius",
    "Positive circle radius.",
)
SKETCH_VALUE_VECTOR2 = _term(
    "pm1.sketch.value.vector2",
    "sketch.value.vector2",
    "A two-component numeric vector.",
)
SKETCH_VALUE_SCALAR = _term(
    "pm1.sketch.value.scalar",
    "sketch.value.scalar",
    "A finite scalar number.",
)
SKETCH_UNIT_MM = _term(
    "pm1.sketch.unit.mm",
    "unit.millimetre",
    "Millimetre length unit.",
)
SKETCH_UNIT_RAD = _term(
    "pm1.sketch.unit.rad",
    "unit.radian",
    "Radian angular unit for sketch values.",
)

SKETCH_OUTPUT_TERMS = _ordered(
    (
        SKETCH_GEOMETRY_RECTANGLE,
        SKETCH_GEOMETRY_CIRCLE,
        SKETCH_PROPERTY_CENTER,
        SKETCH_PROPERTY_HALF_EXTENTS,
        SKETCH_PROPERTY_ROTATION,
        SKETCH_PROPERTY_RADIUS,
        SKETCH_VALUE_VECTOR2,
        SKETCH_VALUE_SCALAR,
        SKETCH_UNIT_MM,
        SKETCH_UNIT_RAD,
    )
)


# ParametricFeatureGraphV2 output vocabulary.
PFG_STRUCTURE_REFERENCE = _term(
    "pm1.pfg.structure.reference",
    "parametric.structure.reference",
    "A non-modelling reference node.",
)
PFG_STRUCTURE_FEATURE = _term(
    "pm1.pfg.structure.feature",
    "parametric.structure.feature",
    "A modelling feature node.",
)
PFG_FAMILY_REFERENCE = _term(
    "pm1.pfg.family.reference",
    "parametric.family.reference",
    "Generic external reference family.",
)
PFG_FAMILY_EXTRUSION = _term(
    "pm1.pfg.family.extrusion",
    "parametric.family.extrusion",
    "Generic linear extrusion family.",
)
PFG_OPERATION_REFERENCE_PROFILES = _term(
    "pm1.pfg.operation.reference-profiles",
    "parametric.operation.reference-sketch-profiles",
    "Resolve declared profile results from one content-addressed sketch document.",
)
PFG_OPERATION_ADD = _term(
    "pm1.pfg.operation.add",
    "parametric.operation.add",
    "Add material using the feature family and inputs.",
)
PFG_OPERATION_REMOVE = _term(
    "pm1.pfg.operation.remove",
    "parametric.operation.remove",
    "Remove material using the feature family and inputs.",
)
PFG_PORT_EXTERNAL = _term(
    "pm1.pfg.port.external",
    "parametric.port-role.external-document",
    "External content-addressed intent document input.",
)
PFG_PORT_PROFILE = _term(
    "pm1.pfg.port.profile",
    "parametric.port-role.profile",
    "Closed planar profile input.",
)
PFG_PORT_BASE = _term(
    "pm1.pfg.port.base",
    "parametric.port-role.base-solid",
    "Upstream solid input.",
)
PFG_PORT_DEPTH = _term(
    "pm1.pfg.port.depth",
    "parametric.port-role.depth",
    "Positive linear depth parameter input.",
)
PFG_PORT_EXTENT = _term(
    "pm1.pfg.port.extent",
    "parametric.port-role.extent",
    "Feature extent policy input.",
)
PFG_RESULT_OUTER_PROFILE = _term(
    "pm1.pfg.result.outer-profile",
    "parametric.result-role.outer-profile",
    "Declared outer profile result.",
)
PFG_RESULT_INNER_PROFILE = _term(
    "pm1.pfg.result.inner-profile",
    "parametric.result-role.inner-profile",
    "Declared inner profile result.",
)
PFG_RESULT_SOLID = _term(
    "pm1.pfg.result.solid",
    "parametric.result-role.solid",
    "Resulting solid body.",
)
PFG_TYPE_SKETCH_DOCUMENT = _term(
    "pm1.pfg.type.sketch-document",
    "parametric.value-type.sketch-document",
    "Canonical SketchIntentGraph document content.",
)
PFG_TYPE_WIRE = _term(
    "pm1.pfg.type.wire",
    "parametric.value-type.closed-wire",
    "Closed planar wire suitable as a profile.",
)
PFG_TYPE_SOLID = _term(
    "pm1.pfg.type.solid",
    "parametric.value-type.solid",
    "Solid body value.",
)
PFG_TYPE_LENGTH_MM = _term(
    "pm1.pfg.type.length-mm",
    "parametric.value-type.length-mm",
    "Finite scalar length measured in millimetres.",
)
PFG_TYPE_EXTENT_THROUGH = _term(
    "pm1.pfg.type.extent-through",
    "parametric.value-type.extent-through-all",
    "An extent policy that removes through the complete base solid.",
)
PFG_LOCATOR_SKETCH_DOCUMENT = _term(
    "pm1.pfg.locator.sketch-document",
    "parametric.locator.content-addressed-sketch-document",
    "Locate an exact sketch document by its content SHA-256.",
)
PFG_PARAMETER_DEPTH = _term(
    "pm1.pfg.parameter.depth",
    "parametric.parameter-role.depth",
    "Component extrusion depth parameter.",
)
PFG_PARAMETER_EXTENT = _term(
    "pm1.pfg.parameter.extent",
    "parametric.parameter-role.extent",
    "Subtractive feature extent policy parameter.",
)
PFG_ENCODING_CANONICAL_JSON = _term(
    "pm1.pfg.encoding.canonical-json",
    "parametric.encoding.canonical-json",
    "Canonical JSON typed-value encoding.",
)

PFG_OUTPUT_TERMS = _ordered(
    (
        PFG_STRUCTURE_REFERENCE,
        PFG_STRUCTURE_FEATURE,
        PFG_FAMILY_REFERENCE,
        PFG_FAMILY_EXTRUSION,
        PFG_OPERATION_REFERENCE_PROFILES,
        PFG_OPERATION_ADD,
        PFG_OPERATION_REMOVE,
        PFG_PORT_EXTERNAL,
        PFG_PORT_PROFILE,
        PFG_PORT_BASE,
        PFG_PORT_DEPTH,
        PFG_PORT_EXTENT,
        PFG_RESULT_OUTER_PROFILE,
        PFG_RESULT_INNER_PROFILE,
        PFG_RESULT_SOLID,
        PFG_TYPE_SKETCH_DOCUMENT,
        PFG_TYPE_WIRE,
        PFG_TYPE_SOLID,
        PFG_TYPE_LENGTH_MM,
        PFG_TYPE_EXTENT_THROUGH,
        PFG_LOCATOR_SKETCH_DOCUMENT,
        PFG_PARAMETER_DEPTH,
        PFG_PARAMETER_EXTENT,
        PFG_ENCODING_CANONICAL_JSON,
    )
)

PLANAR_MECHANICAL_V1_CUSTOM_BRIDGE_TERMS = _ordered(
    (
        ROLE_VISUAL_EVIDENCE,
        ROLE_DECISION,
        ROLE_COMPONENT,
        ROLE_OUTER_PROFILE,
        ROLE_DEPTH,
        ROLE_SKETCH_INTENT,
        ROLE_PARAMETRIC_INTENT,
        RULE_SET_PLANAR_MECHANICAL_V1,
        RULE_COMPILE_SKETCH,
        PREDICATE_SKETCH_COMPILED,
        RULE_COMPILE_PARAMETRIC,
        PREDICATE_PARAMETRIC_COMPILED,
        PFG_TYPE_SOLID,
    )
)


__all__ = [
    name
    for name in globals()
    if name.startswith(
        ("MAX_", "MIN_", "PFG_", "ROLE_", "RULE_", "PREDICATE_", "SKETCH_", "VFG_")
    )
] + [
    "ONTOLOGY_NAMESPACE",
    "ONTOLOGY_VERSION",
    "PLANAR_MECHANICAL_V1_CUSTOM_BRIDGE_TERMS",
    "as_parametric_term",
    "as_sketch_term",
    "as_visual_term",
]
