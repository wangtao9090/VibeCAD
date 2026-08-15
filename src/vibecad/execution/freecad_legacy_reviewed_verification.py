"""Managed verification contracts for the pre-registry FreeCAD intent families.

The original built-in adapters predate :class:`FamilyBatchManifest`.  Their
formal capability specs are executable, but runtime conformance evidence must
still be bound to the exact adapter, rule, semantic operation and native
``TypeId`` before it can be considered for a VERIFIED promotion.  This module
provides that private bridge without changing the public capability catalog.

The manifests below are verification-only metadata.  They grant no authority,
are not persisted, and deliberately reuse the operation ids from
``current_freecad_intent_capability_specs`` verbatim.  Runtime receipt builders
are added below the frozen inventory so the mapping remains independently
testable and reviewable.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceFacet,
    ReviewedVerificationReceipt,
    _admit_reviewed_host_conformance_case_manifest,
    build_managed_freecad_conformance_host,
    build_promotion_verification_binding,
    build_reviewed_verification_receipt,
)
from vibecad.intent_bridge.contracts import AdapterDescriptor, BridgeTermRef
from vibecad.intent_bridge.freecad_parametric_adapter import (
    FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
    GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    GROOVE_CAPABILITY_SCHEMA_TERM,
    GROOVE_INTENT_DOCUMENT_ROLE_TERM,
    GROOVE_OPERATION_TERM,
    GROOVE_PLAN_DOCUMENT_ROLE_TERM,
    GROOVE_PLAN_SCHEMA_TERM,
    GROOVE_REQUEST_TERMS,
)
from vibecad.intent_bridge.freecad_partdesign_boolean_adapter import (
    BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM,
    BOOLEAN_CAPABILITY_SCHEMA_TERM,
    BOOLEAN_INTENT_DOCUMENT_ROLE_TERM,
    BOOLEAN_OPERATION_TERMS,
    BOOLEAN_PLAN_DOCUMENT_ROLE_TERM,
    BOOLEAN_PLAN_SCHEMA_TERM,
    BOOLEAN_REQUEST_TERMS,
    FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR,
)
from vibecad.intent_bridge.freecad_partdesign_dressup_transform_adapter import (
    DRESSUP_TRANSFORM_CAPABILITY_DOCUMENT_ROLE_TERM,
    DRESSUP_TRANSFORM_CAPABILITY_SCHEMA_TERM,
    DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM,
    DRESSUP_TRANSFORM_OPERATION_TERMS,
    DRESSUP_TRANSFORM_PLAN_DOCUMENT_ROLE_TERM,
    DRESSUP_TRANSFORM_PLAN_SCHEMA_TERM,
    DRESSUP_TRANSFORM_REQUEST_TERMS,
    FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR,
)
from vibecad.intent_bridge.freecad_partdesign_pattern_adapter import (
    FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR,
    PATTERN_CAPABILITY_DOCUMENT_ROLE_TERM,
    PATTERN_CAPABILITY_SCHEMA_TERM,
    PATTERN_INTENT_DOCUMENT_ROLE_TERM,
    PATTERN_OPERATION_TERMS,
    PATTERN_PLAN_DOCUMENT_ROLE_TERM,
    PATTERN_PLAN_SCHEMA_TERM,
    PATTERN_REQUEST_TERMS,
)
from vibecad.intent_bridge.freecad_partdesign_primitive_adapter import (
    FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
    PRIMITIVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    PRIMITIVE_CAPABILITY_SCHEMA_TERM,
    PRIMITIVE_INTENT_DOCUMENT_ROLE_TERM,
    PRIMITIVE_OPERATION_TERMS,
    PRIMITIVE_PLAN_DOCUMENT_ROLE_TERM,
    PRIMITIVE_PLAN_SCHEMA_TERM,
    PRIMITIVE_REQUEST_TERMS,
)
from vibecad.intent_bridge.freecad_partdesign_promotion_adapter import (
    FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
    PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM,
    PROMOTION_CAPABILITY_SCHEMA_TERM,
    PROMOTION_INTENT_DOCUMENT_ROLE_TERM,
    PROMOTION_OPERATION_TERMS,
    PROMOTION_PLAN_DOCUMENT_ROLE_TERM,
    PROMOTION_PLAN_SCHEMA_TERM,
    PROMOTION_REQUEST_TERMS,
)
from vibecad.intent_bridge.freecad_partdesign_reference_adapter import (
    FREECAD_REFERENCE_ADAPTER_DESCRIPTOR,
    REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM,
    REFERENCE_CAPABILITY_SCHEMA_TERM,
    REFERENCE_INTENT_DOCUMENT_ROLE_TERM,
    REFERENCE_OPERATION_TERMS,
    REFERENCE_PLAN_DOCUMENT_ROLE_TERM,
    REFERENCE_PLAN_SCHEMA_TERM,
    REFERENCE_REQUEST_TERMS,
)
from vibecad.intent_bridge.freecad_planar_mechanical_adapter import (
    FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
    PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM,
    PLANAR_CAPABILITY_SCHEMA_TERM,
    PLANAR_PLAN_DOCUMENT_ROLE_TERM,
    PLANAR_PLAN_SCHEMA_TERM,
    PLANAR_REQUEST_TERMS,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    FamilyBatchManifest,
    ReviewedOperationSpec,
)
from vibecad.intent_rules.planar_mechanical_v1.terms import (
    PFG_OPERATION_ADD,
    PFG_OPERATION_REFERENCE_PROFILES,
    PFG_OPERATION_REMOVE,
    ROLE_PARAMETRIC_INTENT,
)
from vibecad.parametric.freecad_partdesign_boolean_rules import (
    MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
    PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE,
    PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
    PARTDESIGN_BOOLEAN_RULE_ID,
    AuthenticatedBooleanOperand,
    BooleanOperandSelection,
    PartDesignBooleanBackendPlan,
    PartDesignBooleanExecutionBindings,
    PartDesignBooleanOperation,
    PartDesignBooleanRuleError,
    apply_partdesign_boolean_plan,
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES,
    PARTDESIGN_DRESSUP_TRANSFORM_PLAN_MEDIA_TYPE,
    PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
    PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID,
    AuthenticatedDressupTransformObject,
    PartDesignDressupTransformBackendPlan,
    PartDesignDressupTransformExecutionBindings,
    PartDesignDressupTransformOperation,
    PartDesignDressupTransformRuleError,
    apply_partdesign_dressup_transform_plan,
    operation_parameters_from_value,
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    SemanticObjectSelection as DressupTransformObjectSelection,
)
from vibecad.parametric.freecad_partdesign_pattern_rules import (
    MAX_PARTDESIGN_PATTERN_PLAN_BYTES,
    PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE,
    PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256,
    PARTDESIGN_PATTERN_RULE_ID,
    AuthenticatedPatternObject,
    PartDesignPatternBackendPlan,
    PartDesignPatternExecutionBindings,
    PartDesignPatternOperation,
    PartDesignPatternRuleError,
    PatternObjectSelection,
    PatternOriginAxis,
    PatternOriginPlane,
    apply_partdesign_pattern_plan,
)
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES,
    PARTDESIGN_PRIMITIVE_PLAN_MEDIA_TYPE,
    PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
    PARTDESIGN_PRIMITIVE_RULE_ID,
    AuthenticatedPrimitiveObject,
    PartDesignPrimitiveBackendPlan,
    PartDesignPrimitiveExecutionBindings,
    PartDesignPrimitiveOperation,
    PartDesignPrimitiveRuleError,
    PrimitiveParameterSet,
    apply_partdesign_primitive_plan,
)
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    SemanticObjectSelection as PrimitiveObjectSelection,
)
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
    PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE,
    PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
    PARTDESIGN_PROMOTION_RULE_ID,
    AuthenticatedPromotionObject,
    PartDesignPromotionBackendPlan,
    PartDesignPromotionExecutionBindings,
    PartDesignPromotionOperation,
    PartDesignPromotionRuleError,
    SemanticObjectSelection,
    apply_partdesign_promotion_plan,
)
from vibecad.parametric.freecad_partdesign_reference_rules import (
    MAX_REFERENCE_PLAN_BYTES,
    REFERENCE_PLAN_MEDIA_TYPE,
    REFERENCE_RULE_CONTRACT_SHA256,
    REFERENCE_RULE_ID,
    PartDesignReferenceKind,
    PartDesignReferencePlan,
    ReferenceExecutionBindings,
    ReferenceRuleError,
    apply_partdesign_reference_plan,
)
from vibecad.parametric.freecad_partdesign_sketch_rules import (
    GROOVE_PLAN_MEDIA_TYPE,
    GROOVE_RULE_CONTRACT_SHA256,
    GROOVE_RULE_ID,
    MAX_GROOVE_PLAN_BYTES,
    GrooveBackendPlan,
    GrooveExecutionBindings,
    GrooveRuleError,
    apply_groove_plan,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    MAX_PLANAR_MECHANICAL_PLAN_BYTES,
    PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    PLANAR_MECHANICAL_RULE_ID,
    PlanarCircleRemoval,
    PlanarDocumentBinding,
    PlanarMechanicalBackendPlan,
    PlanarMechanicalExecutionBindings,
    PlanarMechanicalRuleError,
    PlanarRectangleProfile,
    apply_planar_mechanical_plan,
)

LEGACY_REVIEWED_VERIFIER_ID: Final = "vcad.managed.freecad.legacy-reviewed"
LEGACY_REVIEWED_VERIFIER_VERSION: Final = "1.0.0"

_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
_FREECAD_BUILD_DESCRIPTOR_SHA256: Final = hashlib.sha256(
    _FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()
_CASE_CONTRACT_DOMAIN = b"vibecad-freecad-legacy-reviewed-case-v1\0"
_OBSERVATION_DOMAIN = b"vibecad-freecad-legacy-reviewed-observation-v1\0"
_VERIFICATION_LOCK = threading.Lock()


def _fail(path: str) -> None:
    raise CapabilityCatalogError(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _as_bridge(term: object) -> BridgeTermRef:
    if type(term) is BridgeTermRef:
        return term
    try:
        return BridgeTermRef(
            term_ref_id=term.term_ref_id,
            namespace=term.namespace,
            vocabulary_version=term.vocabulary_version,
            term_id=term.term_id,
            term_definition_sha256=term.term_definition_sha256,
        )
    except (AttributeError, TypeError, ValueError):
        _fail("legacy_reviewed/term")


def _merge_terms(*groups: tuple[object, ...]) -> tuple[BridgeTermRef, ...]:
    by_ref: dict[str, BridgeTermRef] = {}
    for term in (item for group in groups for item in group):
        bridged = _as_bridge(term)
        previous = by_ref.get(bridged.term_ref_id)
        if previous is not None and previous != bridged:
            _fail("legacy_reviewed/term_identity")
        by_ref[bridged.term_ref_id] = bridged
    return tuple(sorted(by_ref.values(), key=lambda item: item.term_ref_id))


@dataclass(frozen=True, slots=True, kw_only=True)
class _LegacyFamilyContract:
    family_id: str
    adapter: AdapterDescriptor
    rule_id: str
    rule_contract_sha256: str
    intent_role_term: BridgeTermRef
    capability_role_term: BridgeTermRef
    capability_schema_term: BridgeTermRef
    capability_media_type: str
    plan_role_term: BridgeTermRef
    plan_schema_term: BridgeTermRef
    plan_media_type: str
    request_terms: tuple[BridgeTermRef, ...]
    operation_terms: tuple[BridgeTermRef, ...]
    native_operation_by_id: tuple[tuple[str, str], ...]
    native_properties_by_id: tuple[tuple[str, tuple[str, ...]], ...]
    max_plan_bytes: int


_GROOVE_PROPERTIES: Final = (
    "AllowMultiFace",
    "Angle",
    "Angle2",
    "BaseFeature",
    "Midplane",
    "Profile",
    "ReferenceAxis",
    "Refine",
    "Reversed",
    "Type",
)

_PROMOTION_PROPERTIES: Final = {
    "loft": (
        "AllowMultiFace",
        "BaseFeature",
        "Closed",
        "Midplane",
        "Profile",
        "Refine",
        "Reversed",
        "Ruled",
        "Sections",
    ),
    "pipe": (
        "AuxiliaryCurvilinear",
        "AuxiliarySpine",
        "BaseFeature",
        "Mode",
        "Profile",
        "Refine",
        "Sections",
        "Spine",
        "Transformation",
        "Transition",
    ),
    "helix": (
        "Angle",
        "BaseFeature",
        "Growth",
        "Height",
        "LeftHanded",
        "Mode",
        "Outside",
        "Pitch",
        "Profile",
        "ReferenceAxis",
        "Refine",
    ),
}

_REFERENCE_PROPERTIES: Final = {
    "datum_plane": ("AttachmentOffset", "MapMode", "Support"),
    "datum_line": ("AttachmentOffset", "MapMode", "Support"),
    "datum_point": ("AttachmentOffset", "MapMode", "Support"),
    "shape_binder": ("Support", "TraceSupport"),
    "subshape_binder": ("BindMode", "Fuse", "MakeFace", "PartialLoad", "Relative", "Support"),
}

_PRIMITIVE_FAMILY_PROPERTIES: Final = {
    "box": ("Length", "Width", "Height"),
    "cylinder": ("Radius", "Height", "Angle", "FirstAngle", "SecondAngle"),
    "sphere": ("Radius", "Angle1", "Angle2", "Angle3"),
    "cone": ("Radius1", "Radius2", "Height", "Angle"),
    "ellipsoid": ("Radius1", "Radius2", "Radius3", "Angle1", "Angle2", "Angle3"),
    "prism": ("Polygon", "Circumradius", "Height", "FirstAngle", "SecondAngle"),
    "wedge": ("Xmin", "Ymin", "Zmin", "X2min", "Z2min", "Xmax", "Ymax", "Zmax", "X2max", "Z2max"),
    "torus": ("Radius1", "Radius2", "Angle1", "Angle2", "Angle3"),
}

_DRESSUP_PROPERTIES: Final = {
    "scaled": ("Factor", "Occurrences", "Originals", "Refine", "TransformMode"),
    "multi_transform": ("Originals", "Refine", "Shape", "Transformations", "TransformMode"),
    "fillet": ("Base", "Radius", "Refine", "SupportTransform", "UseAllEdges"),
    "chamfer": ("Base", "ChamferType", "Refine", "Size", "SupportTransform", "UseAllEdges"),
    "draft": (
        "Angle",
        "Base",
        "NeutralPlane",
        "PullDirection",
        "Refine",
        "Reversed",
        "SupportTransform",
    ),
    "thickness": (
        "Base",
        "Join",
        "Mode",
        "Reversed",
        "Value",
        "Intersection",
        "SupportTransform",
        "Refine",
    ),
}

_PATTERN_PROPERTIES: Final = {
    "linear_pattern": (
        "BaseFeature",
        "Direction",
        "Length",
        "Occurrences",
        "Originals",
        "Reversed",
    ),
    "polar_pattern": ("Angle", "Axis", "BaseFeature", "Occurrences", "Originals", "Reversed"),
    "mirrored": ("BaseFeature", "MirrorPlane", "Originals"),
}


def _properties_for(operation_id: str) -> tuple[str, ...]:
    local = operation_id.rsplit(".", 1)[-1]
    if operation_id == "partdesign.groove.angle":
        return _GROOVE_PROPERTIES
    if local in _REFERENCE_PROPERTIES:
        return _REFERENCE_PROPERTIES[local]
    if local in _DRESSUP_PROPERTIES:
        return _DRESSUP_PROPERTIES[local]
    if local in _PATTERN_PROPERTIES:
        return _PATTERN_PROPERTIES[local]
    if operation_id.startswith("partdesign.boolean."):
        return ("BaseFeature", "Group", "Refine", "Type")
    if operation_id.startswith("partdesign.planar-mechanical."):
        return {
            "reference-profiles": ("Geometry", "MapMode", "Support"),
            "add": ("Length", "Midplane", "Profile", "Reversed", "Type"),
            "remove": ("Length", "Midplane", "Profile", "Reversed", "Type"),
        }[local]
    primitive = local.removeprefix("additive_").removeprefix("subtractive_")
    if primitive in _PRIMITIVE_FAMILY_PROPERTIES:
        return (*_PRIMITIVE_FAMILY_PROPERTIES[primitive], "BaseFeature", "Placement", "Refine")
    promotion = local.removeprefix("additive_").removeprefix("subtractive_")
    if promotion in _PROMOTION_PROPERTIES:
        return _PROMOTION_PROPERTIES[promotion]
    _fail("legacy_reviewed/properties")


def _operation_pairs(items: object) -> tuple[tuple[str, str], ...]:
    try:
        return tuple((f"partdesign.{item.operation.value}", item.operation.value) for item in items)
    except (AttributeError, TypeError):
        _fail("legacy_reviewed/operation_pairs")


_FAMILY_CONTRACTS: Final = (
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.groove",
        adapter=FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
        rule_id=GROOVE_RULE_ID,
        rule_contract_sha256=GROOVE_RULE_CONTRACT_SHA256,
        intent_role_term=GROOVE_INTENT_DOCUMENT_ROLE_TERM,
        capability_role_term=GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=GROOVE_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-groove-capability+json",
        plan_role_term=GROOVE_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=GROOVE_PLAN_SCHEMA_TERM,
        plan_media_type=GROOVE_PLAN_MEDIA_TYPE,
        request_terms=GROOVE_REQUEST_TERMS,
        operation_terms=(_as_bridge(GROOVE_OPERATION_TERM),),
        native_operation_by_id=(("partdesign.groove.angle", "groove_angle"),),
        native_properties_by_id=(("partdesign.groove.angle", _GROOVE_PROPERTIES),),
        max_plan_bytes=MAX_GROOVE_PLAN_BYTES,
    ),
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.partdesign-promotion",
        adapter=FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
        rule_id=PARTDESIGN_PROMOTION_RULE_ID,
        rule_contract_sha256=PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
        intent_role_term=PROMOTION_INTENT_DOCUMENT_ROLE_TERM,
        capability_role_term=PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=PROMOTION_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-partdesign-promotion-capability+json",
        plan_role_term=PROMOTION_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=PROMOTION_PLAN_SCHEMA_TERM,
        plan_media_type=PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE,
        request_terms=PROMOTION_REQUEST_TERMS,
        operation_terms=tuple(
            _as_bridge(item.operation_term) for item in PROMOTION_OPERATION_TERMS
        ),
        native_operation_by_id=_operation_pairs(PROMOTION_OPERATION_TERMS),
        native_properties_by_id=tuple(
            (
                f"partdesign.{item.operation.value}",
                _PROMOTION_PROPERTIES[item.operation.value.rsplit("_", 1)[-1]],
            )
            for item in PROMOTION_OPERATION_TERMS
        ),
        max_plan_bytes=MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
    ),
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.partdesign-reference",
        adapter=FREECAD_REFERENCE_ADAPTER_DESCRIPTOR,
        rule_id=REFERENCE_RULE_ID,
        rule_contract_sha256=REFERENCE_RULE_CONTRACT_SHA256,
        intent_role_term=REFERENCE_INTENT_DOCUMENT_ROLE_TERM,
        capability_role_term=REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=REFERENCE_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-reference-capability+json",
        plan_role_term=REFERENCE_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=REFERENCE_PLAN_SCHEMA_TERM,
        plan_media_type=REFERENCE_PLAN_MEDIA_TYPE,
        request_terms=REFERENCE_REQUEST_TERMS,
        operation_terms=tuple(_as_bridge(item) for item in REFERENCE_OPERATION_TERMS.values()),
        native_operation_by_id=tuple(
            (f"partdesign.{kind.value}", kind.value) for kind in REFERENCE_OPERATION_TERMS
        ),
        native_properties_by_id=tuple(
            (f"partdesign.{kind.value}", _REFERENCE_PROPERTIES[kind.value])
            for kind in REFERENCE_OPERATION_TERMS
        ),
        max_plan_bytes=MAX_REFERENCE_PLAN_BYTES,
    ),
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.partdesign-primitive",
        adapter=FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
        rule_id=PARTDESIGN_PRIMITIVE_RULE_ID,
        rule_contract_sha256=PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
        intent_role_term=PRIMITIVE_INTENT_DOCUMENT_ROLE_TERM,
        capability_role_term=PRIMITIVE_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=PRIMITIVE_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-partdesign-primitive-capability+json",
        plan_role_term=PRIMITIVE_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=PRIMITIVE_PLAN_SCHEMA_TERM,
        plan_media_type=PARTDESIGN_PRIMITIVE_PLAN_MEDIA_TYPE,
        request_terms=PRIMITIVE_REQUEST_TERMS,
        operation_terms=tuple(
            _as_bridge(item.operation_term) for item in PRIMITIVE_OPERATION_TERMS
        ),
        native_operation_by_id=_operation_pairs(PRIMITIVE_OPERATION_TERMS),
        native_properties_by_id=tuple(
            (
                f"partdesign.{item.operation.value}",
                _properties_for(f"partdesign.{item.operation.value}"),
            )
            for item in PRIMITIVE_OPERATION_TERMS
        ),
        max_plan_bytes=MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES,
    ),
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.planar-mechanical",
        adapter=FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
        rule_id=PLANAR_MECHANICAL_RULE_ID,
        rule_contract_sha256=PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
        intent_role_term=ROLE_PARAMETRIC_INTENT,
        capability_role_term=PLANAR_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=PLANAR_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-planar-mechanical-capability+json",
        plan_role_term=PLANAR_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=PLANAR_PLAN_SCHEMA_TERM,
        plan_media_type=PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
        request_terms=_merge_terms(
            PLANAR_REQUEST_TERMS,
            (PFG_OPERATION_REFERENCE_PROFILES, PFG_OPERATION_ADD, PFG_OPERATION_REMOVE),
        ),
        operation_terms=(PFG_OPERATION_REFERENCE_PROFILES, PFG_OPERATION_ADD, PFG_OPERATION_REMOVE),
        native_operation_by_id=(
            ("partdesign.planar-mechanical.reference-profiles", "reference_profiles"),
            ("partdesign.planar-mechanical.add", "add"),
            ("partdesign.planar-mechanical.remove", "remove"),
        ),
        native_properties_by_id=tuple(
            (operation_id, _properties_for(operation_id))
            for operation_id in (
                "partdesign.planar-mechanical.reference-profiles",
                "partdesign.planar-mechanical.add",
                "partdesign.planar-mechanical.remove",
            )
        ),
        max_plan_bytes=MAX_PLANAR_MECHANICAL_PLAN_BYTES,
    ),
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.partdesign-dressup-transform",
        adapter=FREECAD_PARTDESIGN_DRESSUP_TRANSFORM_ADAPTER_DESCRIPTOR,
        rule_id=PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID,
        rule_contract_sha256=PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
        intent_role_term=DRESSUP_TRANSFORM_INTENT_DOCUMENT_ROLE_TERM,
        capability_role_term=DRESSUP_TRANSFORM_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=DRESSUP_TRANSFORM_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-partdesign-dressup-transform-capability+json",
        plan_role_term=DRESSUP_TRANSFORM_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=DRESSUP_TRANSFORM_PLAN_SCHEMA_TERM,
        plan_media_type=PARTDESIGN_DRESSUP_TRANSFORM_PLAN_MEDIA_TYPE,
        request_terms=DRESSUP_TRANSFORM_REQUEST_TERMS,
        operation_terms=tuple(
            _as_bridge(item.operation_term) for item in DRESSUP_TRANSFORM_OPERATION_TERMS
        ),
        native_operation_by_id=_operation_pairs(DRESSUP_TRANSFORM_OPERATION_TERMS),
        native_properties_by_id=tuple(
            (f"partdesign.{item.operation.value}", _DRESSUP_PROPERTIES[item.operation.value])
            for item in DRESSUP_TRANSFORM_OPERATION_TERMS
        ),
        max_plan_bytes=MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES,
    ),
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.partdesign-pattern",
        adapter=FREECAD_PARTDESIGN_PATTERN_ADAPTER_DESCRIPTOR,
        rule_id=PARTDESIGN_PATTERN_RULE_ID,
        rule_contract_sha256=PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256,
        intent_role_term=PATTERN_INTENT_DOCUMENT_ROLE_TERM,
        capability_role_term=PATTERN_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=PATTERN_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-partdesign-pattern-capability+json",
        plan_role_term=PATTERN_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=PATTERN_PLAN_SCHEMA_TERM,
        plan_media_type=PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE,
        request_terms=PATTERN_REQUEST_TERMS,
        operation_terms=tuple(_as_bridge(item.operation_term) for item in PATTERN_OPERATION_TERMS),
        native_operation_by_id=_operation_pairs(PATTERN_OPERATION_TERMS),
        native_properties_by_id=tuple(
            (f"partdesign.{item.operation.value}", _PATTERN_PROPERTIES[item.operation.value])
            for item in PATTERN_OPERATION_TERMS
        ),
        max_plan_bytes=MAX_PARTDESIGN_PATTERN_PLAN_BYTES,
    ),
    _LegacyFamilyContract(
        family_id="vcad.freecad.legacy.partdesign-boolean",
        adapter=FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR,
        rule_id=PARTDESIGN_BOOLEAN_RULE_ID,
        rule_contract_sha256=PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
        intent_role_term=BOOLEAN_INTENT_DOCUMENT_ROLE_TERM,
        capability_role_term=BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM,
        capability_schema_term=BOOLEAN_CAPABILITY_SCHEMA_TERM,
        capability_media_type="application/vnd.vibecad.freecad-partdesign-boolean-capability+json",
        plan_role_term=BOOLEAN_PLAN_DOCUMENT_ROLE_TERM,
        plan_schema_term=BOOLEAN_PLAN_SCHEMA_TERM,
        plan_media_type=PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE,
        request_terms=BOOLEAN_REQUEST_TERMS,
        operation_terms=tuple(_as_bridge(item.operation_term) for item in BOOLEAN_OPERATION_TERMS),
        native_operation_by_id=tuple(
            (f"partdesign.boolean.{item.operation.value}", item.operation.value)
            for item in BOOLEAN_OPERATION_TERMS
        ),
        native_properties_by_id=tuple(
            (
                f"partdesign.boolean.{item.operation.value}",
                ("BaseFeature", "Group", "Refine", "Type"),
            )
            for item in BOOLEAN_OPERATION_TERMS
        ),
        max_plan_bytes=MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
    ),
)


def _build_family_manifest(contract: _LegacyFamilyContract) -> FamilyBatchManifest:
    formal_specs = tuple(
        item
        for item in current_freecad_intent_capability_specs()
        if item.adapter_id == contract.adapter.adapter_id
    )
    term_by_id = {item.term_id: item for item in contract.operation_terms}
    native_operation_by_id = dict(contract.native_operation_by_id)
    native_properties_by_id = dict(contract.native_properties_by_id)
    if (
        not formal_specs
        or set(native_operation_by_id) != {item.operation_id for item in formal_specs}
        or set(native_properties_by_id) != {item.operation_id for item in formal_specs}
    ):
        _fail("legacy_reviewed/formal_inventory")
    operations = []
    for spec in formal_specs:
        semantic_term = term_by_id.get(spec.semantic_operation)
        if (
            semantic_term is None
            or spec.adapter_version != contract.adapter.adapter_version
            or spec.adapter_contract_sha256 != contract.adapter.adapter_contract_sha256
            or spec.rule_id != contract.rule_id
            or spec.rule_contract_sha256 != contract.rule_contract_sha256
        ):
            _fail("legacy_reviewed/formal_binding")
        operations.append(
            ReviewedOperationSpec(
                operation_id=spec.operation_id,
                semantic_term=semantic_term,
                native_type_id=spec.native_type_id,
                native_operation=native_operation_by_id[spec.operation_id],
                native_property_names=native_properties_by_id[spec.operation_id],
            )
        )
    return FamilyBatchManifest(
        family_id=contract.family_id,
        family_version="1.0.0",
        adapter=contract.adapter,
        backend_engine="FreeCAD",
        backend_version="1.1.0",
        backend_build_id=_FREECAD_BUILD_DESCRIPTOR_SHA256,
        rule_id=contract.rule_id,
        rule_contract_sha256=contract.rule_contract_sha256,
        intent_role_term=contract.intent_role_term,
        intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
        intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        capability_role_term=contract.capability_role_term,
        capability_schema_term=contract.capability_schema_term,
        capability_media_type=contract.capability_media_type,
        plan_role_term=contract.plan_role_term,
        plan_schema_term=contract.plan_schema_term,
        plan_media_type=contract.plan_media_type,
        request_terms=_merge_terms(contract.request_terms, contract.operation_terms),
        operations=tuple(operations),
        max_plan_bytes=contract.max_plan_bytes,
    )


LEGACY_REVIEWED_FAMILY_MANIFESTS: Final = tuple(
    _build_family_manifest(contract) for contract in _FAMILY_CONTRACTS
)

# Immutable, exact seam from the current formal capability operation id to the
# verification operation specification.  Native rule-local enum names never
# replace the public/formal operation identity in receipts.
LEGACY_REVIEWED_OPERATION_SPECS: Final = tuple(
    operation for manifest in LEGACY_REVIEWED_FAMILY_MANIFESTS for operation in manifest.operations
)
LEGACY_REVIEWED_OPERATION_SPEC_BY_ID: Final = {
    item.operation_id: item for item in LEGACY_REVIEWED_OPERATION_SPECS
}


def _case_contract(
    manifest: FamilyBatchManifest,
    operation: ReviewedOperationSpec,
    facet: ReviewedConformanceFacet,
) -> str:
    return hashlib.sha256(
        _CASE_CONTRACT_DOMAIN
        + manifest.manifest_sha256.encode("ascii")
        + operation.specification_sha256.encode("ascii")
        + facet.value.encode("ascii")
    ).hexdigest()


def _build_case_manifest(manifest: FamilyBatchManifest) -> ReviewedConformanceCaseManifest:
    cases = []
    for operation in manifest.operations:
        for facet in ReviewedConformanceFacet:
            contract_sha256 = _case_contract(manifest, operation, facet)
            cases.append(
                ReviewedConformanceCase(
                    case_id=f"legacy.{contract_sha256[:32]}",
                    operation_id=operation.operation_id,
                    operation_specification_sha256=operation.specification_sha256,
                    facet=facet,
                    case_contract_sha256=contract_sha256,
                )
            )
    return _admit_reviewed_host_conformance_case_manifest(
        manifest=manifest,
        cases=tuple(cases),
    )


LEGACY_REVIEWED_CASE_MANIFESTS: Final = tuple(
    _build_case_manifest(manifest) for manifest in LEGACY_REVIEWED_FAMILY_MANIFESTS
)


def _canonical(value: object, *, maximum: int = 64 * 1024) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail("legacy_reviewed/observation")
    if not raw or len(raw) > maximum:
        _fail("legacy_reviewed/observation")
    return raw


def _content_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _stable_fcstd_save_facts(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        _fail("legacy_reviewed/save")
    return {"format": "FCStd", "nonempty": True, "saved": True}


def _close_owned_documents(freecad: object, owned: dict[str, object]) -> None:
    try:
        current = freecad.listDocuments()
    except Exception:
        return
    for name, document in tuple(owned.items()):
        if current.get(name) is document:
            try:
                freecad.closeDocument(name)
            except Exception:
                pass


def _shape_facts(shape: object) -> dict[str, object]:
    return {
        "volume_mm3": round(float(shape.Volume), 9),
        "solids": len(shape.Solids),
        "faces": len(shape.Faces),
        "edges": len(shape.Edges),
    }


def _document_snapshot(document: object, body: object) -> tuple[object, ...]:
    return (
        tuple((item.Name, item.TypeId, bool(item.Visibility)) for item in document.Objects),
        tuple(body.Group),
        body.Tip,
        bool(document.HasPendingTransaction),
    )


class _FaultAfterCreateBody:
    """Trusted test double that faults only after the native object is added."""

    def __init__(self, body: object) -> None:
        self._body = body

    def __getattr__(self, name: str) -> object:
        return getattr(self._body, name)

    def newObject(self, type_id: str, name: str) -> object:  # noqa: N802
        self._body.newObject(type_id, name)
        raise RuntimeError("injected post-create failure")


class _FaultOnFirstRecomputeDocument:
    """Trusted test double that faults after a transaction added native objects."""

    def __init__(self, document: object) -> None:
        self._document = document
        self._fired = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._document, name)

    def recompute(self) -> object:
        if not self._fired:
            self._fired = True
            raise RuntimeError("injected recompute failure")
        return self._document.recompute()


def _primitive_parameter_value(
    family: str,
    *,
    translation: tuple[float, float, float],
) -> dict[str, object]:
    shape: dict[str, dict[str, int | float]] = {
        "box": {"size_x_mm": 6.0, "size_y_mm": 7.0, "size_z_mm": 8.0},
        "cylinder": {"radius_mm": 4.0, "height_mm": 8.0, "sweep_degrees": 360.0},
        "sphere": {
            "radius_mm": 5.0,
            "latitude_min_degrees": -90.0,
            "latitude_max_degrees": 90.0,
            "sweep_degrees": 360.0,
        },
        "cone": {
            "base_radius_mm": 4.0,
            "top_radius_mm": 2.0,
            "height_mm": 8.0,
            "sweep_degrees": 360.0,
        },
        "ellipsoid": {
            "radius_x_mm": 5.0,
            "radius_y_mm": 4.0,
            "radius_z_mm": 3.0,
            "latitude_min_degrees": -90.0,
            "latitude_max_degrees": 90.0,
            "sweep_degrees": 360.0,
        },
        "prism": {"side_count": 6, "circumradius_mm": 5.0, "height_mm": 8.0},
        "wedge": {
            "x_min_mm": 0.0,
            "y_min_mm": 0.0,
            "z_min_mm": 0.0,
            "x_inner_min_mm": 2.0,
            "z_inner_min_mm": 2.0,
            "x_max_mm": 10.0,
            "y_max_mm": 10.0,
            "z_max_mm": 10.0,
            "x_inner_max_mm": 8.0,
            "z_inner_max_mm": 8.0,
        },
        "torus": {
            "major_radius_mm": 7.0,
            "minor_radius_mm": 2.0,
            "latitude_min_degrees": -180.0,
            "latitude_max_degrees": 180.0,
            "sweep_degrees": 360.0,
        },
    }
    return {
        "shape": shape[family],
        "placement": {
            "translation_mm": list(translation),
            "rotation_axis": [0.0, 0.0, 1.0],
            "rotation_degrees": 0.0,
        },
    }


def _primitive_plan(
    operation: PartDesignPrimitiveOperation,
    *,
    adapter_contract_sha256: str,
    with_base: bool,
    translation: tuple[float, float, float],
    suffix: str,
) -> PartDesignPrimitiveBackendPlan:
    family = operation.value.split("_", 1)[1]
    base = (
        PrimitiveObjectSelection(
            node_id=f"base_{suffix}",
            result_id=f"base_result_{suffix}",
        )
        if with_base
        else None
    )
    return PartDesignPrimitiveBackendPlan(
        source_artifact_id=f"artifact_{suffix}",
        source_graph_id=f"graph_{suffix}",
        source_graph_sha256=hashlib.sha256(f"graph:{suffix}".encode()).hexdigest(),
        source_content_sha256=hashlib.sha256(f"content:{suffix}".encode()).hexdigest(),
        lowering_request_sha256=hashlib.sha256(f"request:{suffix}".encode()).hexdigest(),
        adapter_contract_sha256=adapter_contract_sha256,
        body_id=f"body_{suffix}",
        node_id=f"node_{suffix}",
        result_id=f"result_{suffix}",
        operation=operation,
        base=base,
        parameter_id=f"parameter_{suffix}",
        value_id=f"value_{suffix}",
        parameters=PrimitiveParameterSet.from_value(
            operation,
            _primitive_parameter_value(family, translation=translation),
        ),
    )


def _primitive_bindings(
    document: object,
    part: object,
    plan: PartDesignPrimitiveBackendPlan,
    *,
    suffix: str,
) -> tuple[object, object | None, PartDesignPrimitiveExecutionBindings]:
    body = document.addObject("PartDesign::Body", f"Body_{suffix}")
    base = None
    authenticated = None
    if plan.base is not None:
        base = body.newObject("PartDesign::Feature", f"Base_{suffix}")
        base.Shape = part.makeBox(
            40,
            40,
            40,
            __import__("FreeCAD").Vector(-20, -20, -20),
        )
        authenticated = AuthenticatedPrimitiveObject(
            object=base,
            node_id=plan.base.node_id,
            result_id=plan.base.result_id,
        )
    document.recompute()
    return (
        body,
        base,
        PartDesignPrimitiveExecutionBindings(
            document=document,
            body=body,
            body_id=plan.body_id,
            base=authenticated,
        ),
    )


_PRIMITIVE_EDIT: Final = {
    "box": ("Length", 7.0),
    "cylinder": ("Radius", 4.5),
    "sphere": ("Radius", 5.5),
    "cone": ("Height", 9.0),
    "ellipsoid": ("Radius1", 5.5),
    "prism": ("Circumradius", 5.5),
    "wedge": ("Xmax", 11.0),
    "torus": ("Radius1", 7.5),
}


def _execute_primitive_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    import Part  # type: ignore[import-not-found]  # noqa: PLC0415

    operation = PartDesignPrimitiveOperation(operation_id.removeprefix("partdesign."))
    additive = operation.value.startswith("additive_")
    family = operation.value.split("_", 1)[1]
    suffix = operation.value
    plan = _primitive_plan(
        operation,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        with_base=not additive,
        translation=(0.0, 0.0, 0.0),
        suffix=suffix,
    )
    document = freecad.newDocument(f"LegacyPrimitive_{suffix}")
    document.UndoMode = 1
    body, base, bindings = _primitive_bindings(document, Part, plan, suffix=suffix)
    raw = plan.canonical_bytes
    receipt = apply_partdesign_primitive_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    if (
        feature is None
        or feature.TypeId != LEGACY_REVIEWED_OPERATION_SPEC_BY_ID[operation_id].native_type_id
        or feature is not body.Tip
        or feature.BaseFeature is not base
        or not feature.isValid()
        or len(feature.Shape.Solids) != 1
    ):
        _fail("legacy_reviewed/primitive/create")
    create_facts = {
        "native_type_id": feature.TypeId,
        "object_name": feature.Name,
        "shape": _shape_facts(feature.Shape),
    }

    before_edit = _shape_facts(feature.Shape)
    edit_property, edit_value = _PRIMITIVE_EDIT[family]
    setattr(feature, edit_property, edit_value)
    recompute_result = document.recompute()
    after_edit = _shape_facts(feature.Shape)
    if (
        not feature.isValid()
        or len(feature.Shape.Solids) != 1
        or after_edit["volume_mm3"] == before_edit["volume_mm3"]
    ):
        _fail("legacy_reviewed/primitive/edit")
    edit_facts = {
        "property": edit_property,
        "value": edit_value,
        "before": before_edit,
        "after": after_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(feature.State),
        "valid": bool(feature.isValid()),
        "shape": after_edit,
    }

    model_path = temporary_root / f"{suffix}.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    object_name = feature.Name
    body_name = body.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted = reopened.getObject(object_name)
    persisted_body = reopened.getObject(body_name)
    if (
        persisted is None
        or persisted_body is None
        or persisted.TypeId != create_facts["native_type_id"]
        or persisted is not persisted_body.Tip
        or not persisted.isValid()
        or abs(float(getattr(persisted, edit_property)) - edit_value) > 1e-9
    ):
        _fail("legacy_reviewed/primitive/reopen")
    reopen_facts = {
        "native_type_id": persisted.TypeId,
        "property": edit_property,
        "value": float(getattr(persisted, edit_property)),
        "shape": _shape_facts(persisted.Shape),
    }
    freecad.closeDocument(reopened.Name)

    negative_document = freecad.newDocument(f"LegacyPrimitiveNegative_{suffix}")
    negative_document.UndoMode = 1
    negative_body, _negative_base, negative_bindings = _primitive_bindings(
        negative_document,
        Part,
        plan,
        suffix=f"negative_{suffix}",
    )
    negative_before = _document_snapshot(negative_document, negative_body)
    negative_rejected = False
    try:
        apply_partdesign_primitive_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=negative_bindings,
        )
    except PartDesignPrimitiveRuleError:
        negative_rejected = True
    if (
        not negative_rejected
        or _document_snapshot(negative_document, negative_body) != negative_before
    ):
        _fail("legacy_reviewed/primitive/negative")
    negative_facts = {"rejected": True, "mutation": False}
    freecad.closeDocument(negative_document.Name)

    rollback_plan = _primitive_plan(
        operation,
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        with_base=True,
        translation=(100.0, 100.0, 100.0),
        suffix=f"rollback_{suffix}",
    )
    rollback_document = freecad.newDocument(f"LegacyPrimitiveRollback_{suffix}")
    rollback_document.UndoMode = 1
    rollback_body, _rollback_base, rollback_bindings = _primitive_bindings(
        rollback_document,
        Part,
        rollback_plan,
        suffix=f"rollback_{suffix}",
    )
    rollback_before = _document_snapshot(rollback_document, rollback_body)
    rollback_raw = rollback_plan.canonical_bytes
    rollback_rejected = False
    try:
        apply_partdesign_primitive_plan(
            rollback_raw,
            expected_content_sha256=_content_sha256(rollback_raw),
            expected_plan_sha256=rollback_plan.plan_sha256,
            bindings=rollback_bindings,
        )
    except PartDesignPrimitiveRuleError:
        rollback_rejected = True
    if (
        not rollback_rejected
        or _document_snapshot(rollback_document, rollback_body) != rollback_before
        or rollback_document.HasPendingTransaction
    ):
        _fail("legacy_reviewed/primitive/rollback")
    rollback_facts = {"rejected": True, "state_restored": True}
    freecad.closeDocument(rollback_document.Name)

    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: negative_facts,
        ReviewedConformanceFacet.LATE_ROLLBACK: rollback_facts,
    }


def _boolean_plan(
    operation: PartDesignBooleanOperation,
    manifest: FamilyBatchManifest,
    *,
    suffix: str,
) -> PartDesignBooleanBackendPlan:
    return PartDesignBooleanBackendPlan(
        source_artifact_id=f"artifact_{suffix}",
        source_graph_id=f"graph_{suffix}",
        source_graph_sha256=hashlib.sha256(f"graph:{suffix}".encode()).hexdigest(),
        source_content_sha256=hashlib.sha256(f"content:{suffix}".encode()).hexdigest(),
        lowering_request_sha256=hashlib.sha256(f"request:{suffix}".encode()).hexdigest(),
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        body_id=f"target_body_{suffix}",
        node_id=f"boolean_{suffix}",
        result_id=f"boolean_result_{suffix}",
        operation=operation,
        base=BooleanOperandSelection(
            body_id=f"target_body_{suffix}",
            node_id=f"base_{suffix}",
            result_id=f"base_result_{suffix}",
        ),
        tools=(
            BooleanOperandSelection(
                body_id=f"tool_body_{suffix}",
                node_id=f"tool_{suffix}",
                result_id=f"tool_result_{suffix}",
            ),
        ),
    )


def _boolean_bindings(
    freecad: object,
    document: object,
    plan: PartDesignBooleanBackendPlan,
    *,
    suffix: str,
    tool_position: tuple[float, float, float],
) -> tuple[object, object, object, object, PartDesignBooleanExecutionBindings]:
    target = document.addObject("PartDesign::Body", f"TargetBody_{suffix}")
    base = target.newObject("PartDesign::AdditiveBox", f"Base_{suffix}")
    base.Length = 20.0
    base.Width = 20.0
    base.Height = 20.0
    tool_body = document.addObject("PartDesign::Body", f"ToolBody_{suffix}")
    tool = tool_body.newObject("PartDesign::AdditiveBox", f"Tool_{suffix}")
    tool.Length = 10.0
    tool.Width = 10.0
    tool.Height = 30.0
    tool.Placement.Base = freecad.Vector(*tool_position)
    document.recompute()
    tool_plan = plan.tools[0]
    bindings = PartDesignBooleanExecutionBindings(
        document=document,
        target_body=target,
        target_body_id=plan.body_id,
        base=AuthenticatedBooleanOperand(
            object=base,
            body=target,
            body_id=plan.base.body_id,
            node_id=plan.base.node_id,
            result_id=plan.base.result_id,
        ),
        tools=(
            AuthenticatedBooleanOperand(
                object=tool,
                body=tool_body,
                body_id=tool_plan.body_id,
                node_id=tool_plan.node_id,
                result_id=tool_plan.result_id,
            ),
        ),
    )
    return target, base, tool_body, tool, bindings


def _boolean_snapshot(document: object, bodies: tuple[object, ...]) -> tuple[object, ...]:
    return (
        tuple(document.Objects),
        tuple((body, tuple(body.Group), body.Tip) for body in bodies),
        tuple(
            (item, bool(item.Visibility))
            for item in document.Objects
            if hasattr(item, "Visibility")
        ),
        bool(document.HasPendingTransaction),
    )


def _execute_boolean_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    operation = PartDesignBooleanOperation(operation_id.rsplit(".", 1)[-1])
    suffix = f"boolean_{operation.value}"
    plan = _boolean_plan(operation, manifest, suffix=suffix)
    raw = plan.canonical_bytes
    document = freecad.newDocument(f"LegacyBoolean_{operation.value}")
    document.UndoMode = 1
    target, base, tool_body, tool, bindings = _boolean_bindings(
        freecad,
        document,
        plan,
        suffix=suffix,
        tool_position=(5.0, 5.0, -5.0),
    )
    receipt = apply_partdesign_boolean_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    native_enum = {
        PartDesignBooleanOperation.FUSE: "Fuse",
        PartDesignBooleanOperation.CUT: "Cut",
        PartDesignBooleanOperation.COMMON: "Common",
    }[operation]
    if (
        feature is None
        or feature.TypeId != "PartDesign::Boolean"
        or str(feature.Type) != native_enum
        or feature.BaseFeature is not base
        or tuple(feature.Group) != (tool_body,)
        or target.Tip is not feature
        or not feature.isValid()
        or len(feature.Shape.Solids) != 1
    ):
        _fail("legacy_reviewed/boolean/create")
    create_facts = {
        "native_type_id": feature.TypeId,
        "native_operation": str(feature.Type),
        "shape": _shape_facts(feature.Shape),
    }
    before_edit = _shape_facts(feature.Shape)
    tool.Length = 12.0
    recompute_result = document.recompute()
    after_edit = _shape_facts(feature.Shape)
    if (
        not feature.isValid()
        or tuple(feature.State) != ("Up-to-date",)
        or after_edit["volume_mm3"] == before_edit["volume_mm3"]
    ):
        _fail("legacy_reviewed/boolean/edit")
    edit_facts = {
        "dependency_property": "Length",
        "before": before_edit,
        "after": after_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(feature.State),
        "valid": bool(feature.isValid()),
    }
    model_path = temporary_root / f"{suffix}.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    feature_name = feature.Name
    target_name = target.Name
    tool_name = tool.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted = reopened.getObject(feature_name)
    persisted_target = reopened.getObject(target_name)
    persisted_tool = reopened.getObject(tool_name)
    if (
        persisted is None
        or persisted_target is None
        or persisted_tool is None
        or persisted.TypeId != "PartDesign::Boolean"
        or str(persisted.Type) != native_enum
        or persisted_target.Tip is not persisted
        or abs(float(persisted_tool.Length) - 12.0) > 1e-9
        or not persisted.isValid()
    ):
        _fail("legacy_reviewed/boolean/reopen")
    reopen_facts = {
        "native_operation": str(persisted.Type),
        "dependency_length": float(persisted_tool.Length),
        "shape": _shape_facts(persisted.Shape),
    }
    freecad.closeDocument(reopened.Name)

    negative_document = freecad.newDocument(f"LegacyBooleanNegative_{operation.value}")
    negative_document.UndoMode = 1
    negative_target, _base, negative_tool_body, _tool, negative_bindings = _boolean_bindings(
        freecad,
        negative_document,
        plan,
        suffix=f"negative_{suffix}",
        tool_position=(5.0, 5.0, -5.0),
    )
    negative_before = _boolean_snapshot(
        negative_document,
        (negative_target, negative_tool_body),
    )
    negative_rejected = False
    try:
        apply_partdesign_boolean_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=negative_bindings,
        )
    except PartDesignBooleanRuleError:
        negative_rejected = True
    if (
        not negative_rejected
        or _boolean_snapshot(negative_document, (negative_target, negative_tool_body))
        != negative_before
    ):
        _fail("legacy_reviewed/boolean/negative")
    freecad.closeDocument(negative_document.Name)

    rollback_document = freecad.newDocument(f"LegacyBooleanRollback_{operation.value}")
    rollback_document.UndoMode = 1
    rollback_target, _base, rollback_tool_body, _tool, rollback_bindings = _boolean_bindings(
        freecad,
        rollback_document,
        plan,
        suffix=f"rollback_{suffix}",
        tool_position=(100.0, 100.0, 100.0),
    )
    rollback_before = _boolean_snapshot(
        rollback_document,
        (rollback_target, rollback_tool_body),
    )
    rollback_rejected = False
    try:
        apply_partdesign_boolean_plan(
            raw,
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=rollback_bindings,
        )
    except PartDesignBooleanRuleError:
        rollback_rejected = True
    if (
        not rollback_rejected
        or _boolean_snapshot(rollback_document, (rollback_target, rollback_tool_body))
        != rollback_before
    ):
        _fail("legacy_reviewed/boolean/rollback")
    freecad.closeDocument(rollback_document.Name)

    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: {"rejected": True, "mutation": False},
        ReviewedConformanceFacet.LATE_ROLLBACK: {
            "rejected": True,
            "state_restored": True,
        },
    }


def _pattern_plan(
    operation: PartDesignPatternOperation,
    manifest: FamilyBatchManifest,
    *,
    suffix: str,
    span_mm: float = 30.0,
) -> PartDesignPatternBackendPlan:
    common: dict[str, object] = {
        "axis": None,
        "plane": None,
        "occurrences": None,
        "span_mm": None,
        "angle_degrees": None,
        "reversed": False,
    }
    if operation is PartDesignPatternOperation.LINEAR_PATTERN:
        common.update(axis=PatternOriginAxis.X, occurrences=3, span_mm=span_mm)
    elif operation is PartDesignPatternOperation.POLAR_PATTERN:
        common.update(axis=PatternOriginAxis.Z, occurrences=3, angle_degrees=180.0)
    else:
        common.update(plane=PatternOriginPlane.YZ)
    return PartDesignPatternBackendPlan(
        source_artifact_id=f"artifact_{suffix}",
        source_graph_id=f"graph_{suffix}",
        source_graph_sha256=hashlib.sha256(f"graph:{suffix}".encode()).hexdigest(),
        source_content_sha256=hashlib.sha256(f"content:{suffix}".encode()).hexdigest(),
        lowering_request_sha256=hashlib.sha256(f"request:{suffix}".encode()).hexdigest(),
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        body_id=f"body_{suffix}",
        node_id=f"pattern_{suffix}",
        result_id=f"pattern_result_{suffix}",
        operation=operation,
        base=PatternObjectSelection(
            node_id=f"source_{suffix}",
            result_id=f"source_result_{suffix}",
        ),
        source_feature=PatternObjectSelection(
            node_id=f"source_{suffix}",
            result_id=f"source_result_{suffix}",
        ),
        reference_id=f"reference_{suffix}",
        **common,
    )


def _add_rectangle_sketch(
    freecad: object,
    part: object,
    sketcher: object,
    sketch: object,
    *,
    width: float = 60.0,
    height: float = 40.0,
) -> None:
    points = (
        freecad.Vector(-width / 2.0, -height / 2.0, 0),
        freecad.Vector(width / 2.0, -height / 2.0, 0),
        freecad.Vector(width / 2.0, height / 2.0, 0),
        freecad.Vector(-width / 2.0, height / 2.0, 0),
    )
    for index, start in enumerate(points):
        sketch.addGeometry(part.LineSegment(start, points[(index + 1) % 4]), False)
    for index in range(4):
        sketch.addConstraint(sketcher.Constraint("Coincident", index, 2, (index + 1) % 4, 1))


def _pattern_bindings(
    freecad: object,
    part: object,
    sketcher: object,
    document: object,
    plan: PartDesignPatternBackendPlan,
    *,
    suffix: str,
    centered: bool,
) -> tuple[object, object, int, object, PartDesignPatternExecutionBindings]:
    document.addObject("PartDesign::Body", f"Decoy_{suffix}")
    body = document.addObject("PartDesign::Body", f"Body_{suffix}")
    outer = body.newObject("Sketcher::SketchObject", f"Outer_{suffix}")
    _add_rectangle_sketch(freecad, part, sketcher, outer)
    document.recompute()
    pad = body.newObject("PartDesign::Pad", f"Pad_{suffix}")
    pad.Profile = outer
    pad.Type = "Length"
    pad.Length = 8.0
    pad.Refine = True
    document.recompute()
    hole = body.newObject("Sketcher::SketchObject", f"Hole_{suffix}")
    if centered:
        x, y = 0.0, 0.0
    elif plan.operation is PartDesignPatternOperation.LINEAR_PATTERN:
        x, y = -15.0, 0.0
    elif plan.operation is PartDesignPatternOperation.POLAR_PATTERN:
        x, y = 15.0, 0.0
    else:
        x, y = 15.0, 6.0
    geometry = hole.addGeometry(
        part.Circle(freecad.Vector(x, y, 0), freecad.Vector(0, 0, 1), 3.0),
        False,
    )
    radius_constraint = hole.addConstraint(sketcher.Constraint("Radius", geometry, 3.0))
    document.recompute()
    pocket = body.newObject("PartDesign::Pocket", f"Pocket_{suffix}")
    pocket.Profile = hole
    pocket.Type = "ThroughAll"
    pocket.SideType = "One side"
    pocket.AlongSketchNormal = True
    pocket.UseCustomVector = False
    pocket.Reversed = True
    pocket.Refine = True
    document.recompute()
    authenticated = AuthenticatedPatternObject(
        object=pocket,
        node_id=plan.source_feature.node_id,
        result_id=plan.source_feature.result_id,
    )
    bindings = PartDesignPatternExecutionBindings(
        document=document,
        body=body,
        body_id=plan.body_id,
        base=AuthenticatedPatternObject(
            object=pocket,
            node_id=plan.base.node_id,
            result_id=plan.base.result_id,
        ),
        source_feature=authenticated,
    )
    return body, hole, radius_constraint, pocket, bindings


def _execute_pattern_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    import Part  # type: ignore[import-not-found]  # noqa: PLC0415
    import Sketcher  # type: ignore[import-not-found]  # noqa: PLC0415

    operation = PartDesignPatternOperation(operation_id.removeprefix("partdesign."))
    suffix = operation.value
    plan = _pattern_plan(operation, manifest, suffix=suffix)
    raw = plan.canonical_bytes
    document = freecad.newDocument(f"LegacyPattern_{suffix}")
    document.UndoMode = 1
    body, hole, radius_constraint, source, bindings = _pattern_bindings(
        freecad,
        Part,
        Sketcher,
        document,
        plan,
        suffix=suffix,
        centered=False,
    )
    receipt = apply_partdesign_pattern_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    expected_type = LEGACY_REVIEWED_OPERATION_SPEC_BY_ID[operation_id].native_type_id
    if (
        feature is None
        or feature.TypeId != expected_type
        or feature is not body.Tip
        or feature.BaseFeature is not source
        or tuple(feature.Originals) != (source,)
        or not feature.isValid()
        or len(feature.Shape.Solids) != 1
    ):
        _fail("legacy_reviewed/pattern/create")
    create_facts = {"native_type_id": feature.TypeId, "shape": _shape_facts(feature.Shape)}
    before_edit = _shape_facts(feature.Shape)
    if operation in {
        PartDesignPatternOperation.LINEAR_PATTERN,
        PartDesignPatternOperation.POLAR_PATTERN,
    }:
        feature.Occurrences = 4
        edit_property = "Occurrences"
        edit_value = 4.0
    else:
        hole.setDatum(radius_constraint, freecad.Units.Quantity("4 mm"))
        edit_property = "source_radius"
        edit_value = 4.0
    recompute_result = document.recompute()
    after_edit = _shape_facts(feature.Shape)
    if (
        not feature.isValid()
        or len(feature.Shape.Solids) != 1
        or after_edit["volume_mm3"] == before_edit["volume_mm3"]
    ):
        _fail("legacy_reviewed/pattern/edit")
    edit_facts = {
        "property": edit_property,
        "value": edit_value,
        "before": before_edit,
        "after": after_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(feature.State),
        "valid": bool(feature.isValid()),
    }
    model_path = temporary_root / f"{suffix}.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    feature_name = feature.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted = reopened.getObject(feature_name)
    if (
        persisted is None
        or persisted.TypeId != expected_type
        or not persisted.isValid()
        or len(persisted.Shape.Solids) != 1
    ):
        _fail("legacy_reviewed/pattern/reopen")
    reopen_facts = {"native_type_id": persisted.TypeId, "shape": _shape_facts(persisted.Shape)}
    freecad.closeDocument(reopened.Name)

    negative_document = freecad.newDocument(f"LegacyPatternNegative_{suffix}")
    negative_document.UndoMode = 1
    negative_body, _hole, _radius, _source, negative_bindings = _pattern_bindings(
        freecad,
        Part,
        Sketcher,
        negative_document,
        plan,
        suffix=f"negative_{suffix}",
        centered=False,
    )
    negative_before = _document_snapshot(negative_document, negative_body)
    rejected = False
    try:
        apply_partdesign_pattern_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=negative_bindings,
        )
    except PartDesignPatternRuleError:
        rejected = True
    if not rejected or _document_snapshot(negative_document, negative_body) != negative_before:
        _fail("legacy_reviewed/pattern/negative")
    freecad.closeDocument(negative_document.Name)

    rollback_span = 100.0 if operation is PartDesignPatternOperation.LINEAR_PATTERN else 30.0
    rollback_plan = _pattern_plan(
        operation,
        manifest,
        suffix=f"rollback_{suffix}",
        span_mm=rollback_span,
    )
    rollback_raw = rollback_plan.canonical_bytes
    rollback_document = freecad.newDocument(f"LegacyPatternRollback_{suffix}")
    rollback_document.UndoMode = 1
    rollback_body, _hole, _radius, _source, rollback_bindings = _pattern_bindings(
        freecad,
        Part,
        Sketcher,
        rollback_document,
        rollback_plan,
        suffix=f"rollback_{suffix}",
        centered=operation is not PartDesignPatternOperation.LINEAR_PATTERN,
    )
    rollback_before = _document_snapshot(rollback_document, rollback_body)
    rollback_rejected = False
    try:
        apply_partdesign_pattern_plan(
            rollback_raw,
            expected_content_sha256=_content_sha256(rollback_raw),
            expected_plan_sha256=rollback_plan.plan_sha256,
            bindings=rollback_bindings,
        )
    except PartDesignPatternRuleError:
        rollback_rejected = True
    if (
        not rollback_rejected
        or _document_snapshot(rollback_document, rollback_body) != rollback_before
    ):
        _fail("legacy_reviewed/pattern/rollback")
    freecad.closeDocument(rollback_document.Name)

    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: {"rejected": True, "mutation": False},
        ReviewedConformanceFacet.LATE_ROLLBACK: {
            "rejected": True,
            "state_restored": True,
        },
    }


def _groove_plan(manifest: FamilyBatchManifest, *, suffix: str) -> GrooveBackendPlan:
    return GrooveBackendPlan(
        source_artifact_id=f"artifact_{suffix}",
        source_graph_id=f"graph_{suffix}",
        source_graph_sha256=hashlib.sha256(f"graph:{suffix}".encode()).hexdigest(),
        source_content_sha256=hashlib.sha256(f"content:{suffix}".encode()).hexdigest(),
        lowering_request_sha256=hashlib.sha256(f"request:{suffix}".encode()).hexdigest(),
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        body_id=f"body_{suffix}",
        node_id=f"groove_{suffix}",
        result_id=f"groove_result_{suffix}",
        base_node_id=f"base_{suffix}",
        base_result_id=f"base_result_{suffix}",
        profile_node_id=f"profile_{suffix}",
        profile_result_id=f"profile_result_{suffix}",
        axis_reference_id=f"axis_{suffix}",
        axis_result_id=f"axis_result_{suffix}",
        angle_degrees=360.0,
        reversed=False,
    )


def _groove_bindings(
    freecad: object,
    part: object,
    document: object,
    plan: GrooveBackendPlan,
    *,
    suffix: str,
    far: bool,
) -> tuple[object, object, object, GrooveExecutionBindings]:
    body = document.addObject("PartDesign::Body", f"Body_{suffix}")
    base = body.newObject("PartDesign::Feature", f"Base_{suffix}")
    base.Shape = part.makeCylinder(10, 20)
    profile = body.newObject("Sketcher::SketchObject", f"Profile_{suffix}")
    profile.Placement = freecad.Placement(
        freecad.Vector(0, 0, 0),
        freecad.Rotation(freecad.Vector(1, 0, 0), 90),
    )
    start = 30.0 if far else 8.0
    end = 35.0 if far else 12.0
    points = ((start, 8.0), (end, 8.0), (end, 12.0), (start, 12.0), (start, 8.0))
    for index in range(len(points) - 1):
        profile.addGeometry(
            part.LineSegment(
                freecad.Vector(*points[index], 0),
                freecad.Vector(*points[index + 1], 0),
            ),
            False,
        )
    document.recompute()
    return (
        body,
        base,
        profile,
        GrooveExecutionBindings(
            document=document,
            body=body,
            base_feature=base,
            profile=profile,
            body_id=plan.body_id,
            base_node_id=plan.base_node_id,
            base_result_id=plan.base_result_id,
            profile_node_id=plan.profile_node_id,
            profile_result_id=plan.profile_result_id,
        ),
    )


def _execute_groove_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    import Part  # type: ignore[import-not-found]  # noqa: PLC0415
    import Sketcher  # type: ignore[import-not-found]  # noqa: F401, PLC0415

    if operation_id != "partdesign.groove.angle":
        _fail("legacy_reviewed/groove/operation")
    plan = _groove_plan(manifest, suffix="groove")
    raw = plan.canonical_bytes
    document = freecad.newDocument("LegacyGroove")
    document.UndoMode = 1
    body, base, profile, bindings = _groove_bindings(
        freecad,
        Part,
        document,
        plan,
        suffix="main",
        far=False,
    )
    receipt = apply_groove_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    if (
        feature is None
        or feature.TypeId != "PartDesign::Groove"
        or feature is not body.Tip
        or feature.BaseFeature is not base
        or feature.Profile[0] is not profile
        or tuple(feature.ReferenceAxis[1]) != ("V_Axis",)
        or not feature.isValid()
    ):
        _fail("legacy_reviewed/groove/create")
    create_facts = {"native_type_id": feature.TypeId, "shape": _shape_facts(feature.Shape)}
    before_edit = _shape_facts(feature.Shape)
    feature.Angle = 180.0
    recompute_result = document.recompute()
    after_edit = _shape_facts(feature.Shape)
    if (
        not feature.isValid()
        or after_edit["volume_mm3"] == before_edit["volume_mm3"]
        or abs(float(feature.Angle) - 180.0) > 1e-9
    ):
        _fail("legacy_reviewed/groove/edit")
    edit_facts = {
        "property": "Angle",
        "value": float(feature.Angle),
        "before": before_edit,
        "after": after_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(feature.State),
        "valid": bool(feature.isValid()),
    }
    model_path = temporary_root / "groove.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    feature_name = feature.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted = reopened.getObject(feature_name)
    if (
        persisted is None
        or persisted.TypeId != "PartDesign::Groove"
        or abs(float(persisted.Angle) - 180.0) > 1e-9
        or not persisted.isValid()
    ):
        _fail("legacy_reviewed/groove/reopen")
    reopen_facts = {
        "native_type_id": persisted.TypeId,
        "angle": float(persisted.Angle),
        "shape": _shape_facts(persisted.Shape),
    }
    freecad.closeDocument(reopened.Name)

    negative_document = freecad.newDocument("LegacyGrooveNegative")
    negative_document.UndoMode = 1
    negative_body, _base, _profile, negative_bindings = _groove_bindings(
        freecad,
        Part,
        negative_document,
        plan,
        suffix="negative",
        far=False,
    )
    negative_before = _document_snapshot(negative_document, negative_body)
    rejected = False
    try:
        apply_groove_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=negative_bindings,
        )
    except GrooveRuleError:
        rejected = True
    if not rejected or _document_snapshot(negative_document, negative_body) != negative_before:
        _fail("legacy_reviewed/groove/negative")
    freecad.closeDocument(negative_document.Name)

    rollback_document = freecad.newDocument("LegacyGrooveRollback")
    rollback_document.UndoMode = 1
    rollback_body, _base, _profile, rollback_bindings = _groove_bindings(
        freecad,
        Part,
        rollback_document,
        plan,
        suffix="rollback",
        far=True,
    )
    rollback_before = _document_snapshot(rollback_document, rollback_body)
    rollback_rejected = False
    try:
        apply_groove_plan(
            raw,
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=rollback_bindings,
        )
    except GrooveRuleError:
        rollback_rejected = True
    if (
        not rollback_rejected
        or _document_snapshot(rollback_document, rollback_body) != rollback_before
    ):
        _fail("legacy_reviewed/groove/rollback")
    freecad.closeDocument(rollback_document.Name)
    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: {"rejected": True, "mutation": False},
        ReviewedConformanceFacet.LATE_ROLLBACK: {
            "rejected": True,
            "state_restored": True,
        },
    }


def _reference_plan(
    kind: PartDesignReferenceKind,
    manifest: FamilyBatchManifest,
    *,
    suffix: str,
) -> PartDesignReferencePlan:
    return PartDesignReferencePlan(
        source_artifact_id=f"artifact_{suffix}",
        source_graph_id=f"graph_{suffix}",
        source_graph_sha256=hashlib.sha256(f"graph:{suffix}".encode()).hexdigest(),
        source_content_sha256=hashlib.sha256(f"content:{suffix}".encode()).hexdigest(),
        lowering_request_sha256=hashlib.sha256(f"request:{suffix}".encode()).hexdigest(),
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        body_id=f"body_{suffix}",
        node_id=f"reference_{suffix}",
        result_id=f"reference_result_{suffix}",
        support_reference_id=f"support_{suffix}",
        support_reference_sha256=hashlib.sha256(f"support:{suffix}".encode()).hexdigest(),
        kind=kind,
    )


_REFERENCE_SUBNAMES: Final = {
    PartDesignReferenceKind.DATUM_PLANE: "Face6",
    PartDesignReferenceKind.DATUM_LINE: "Edge10",
    PartDesignReferenceKind.DATUM_POINT: "Vertex7",
    PartDesignReferenceKind.SHAPE_BINDER: "",
    PartDesignReferenceKind.SUBSHAPE_BINDER: "Face1",
}


def _reference_fixture(
    part: object,
    document: object,
    plan: PartDesignReferencePlan,
    *,
    suffix: str,
    body_override: object | None = None,
) -> tuple[object, object, object, object, ReferenceExecutionBindings]:
    body = document.addObject("PartDesign::Body", f"Body_{suffix}")
    base = body.newObject("PartDesign::Feature", f"Base_{suffix}")
    base.Shape = part.makeBox(20, 20, 10)
    source_body = document.addObject("PartDesign::Body", f"SourceBody_{suffix}")
    source = source_body.newObject("PartDesign::Feature", f"Source_{suffix}")
    source.Shape = part.makeCylinder(3, 10)
    document.recompute()
    support = (
        base
        if plan.kind
        in {
            PartDesignReferenceKind.DATUM_PLANE,
            PartDesignReferenceKind.DATUM_LINE,
            PartDesignReferenceKind.DATUM_POINT,
        }
        else source
    )
    bindings = ReferenceExecutionBindings(
        document=document,
        body=body if body_override is None else body_override,
        support=support,
        body_id=plan.body_id,
        support_reference_id=plan.support_reference_id,
        support_reference_sha256=plan.support_reference_sha256,
        support_subname=_REFERENCE_SUBNAMES[plan.kind],
    )
    return body, base, source, support, bindings


def _reference_geometry_facts(value: object) -> dict[str, object]:
    shape = value.Shape
    placement = value.Placement
    result: dict[str, object] = {
        "placement": [
            round(float(item), 9)
            for item in (
                placement.Base.x,
                placement.Base.y,
                placement.Base.z,
                placement.Rotation.Q[0],
                placement.Rotation.Q[1],
                placement.Rotation.Q[2],
                placement.Rotation.Q[3],
            )
        ],
    }
    try:
        box = shape.BoundBox
        result["shape"] = {
            "faces": len(shape.Faces),
            "edges": len(shape.Edges),
            "vertices": len(shape.Vertexes),
            "area": round(float(shape.Area), 9),
            "length": round(float(shape.Length), 9),
            "bounds": [
                round(float(item), 9)
                for item in (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax)
            ],
        }
    except Exception:
        # Datum references can legitimately reopen with an abstract placement
        # and no directly measurable topological shape.  Placement remains the
        # stable user-visible datum contract; binders still take this branch's
        # full shape facts.
        result["shape"] = None
    return result


def _execute_reference_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    import Part  # type: ignore[import-not-found]  # noqa: PLC0415
    import PartDesign  # type: ignore[import-not-found]  # noqa: F401, PLC0415

    kind = PartDesignReferenceKind(operation_id.removeprefix("partdesign."))
    suffix = kind.value
    plan = _reference_plan(kind, manifest, suffix=suffix)
    raw = plan.canonical_bytes
    document = freecad.newDocument(f"LegacyReference_{suffix}")
    document.UndoMode = 1
    body, base, source, support, bindings = _reference_fixture(
        Part,
        document,
        plan,
        suffix=suffix,
    )
    initial_tip = body.Tip
    receipt = apply_partdesign_reference_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )
    result = document.getObject(receipt.object_name)
    expected_type = LEGACY_REVIEWED_OPERATION_SPEC_BY_ID[operation_id].native_type_id
    if (
        result is None
        or result.TypeId != expected_type
        or result not in body.Group
        or body.Tip is not initial_tip
        or not result.isValid()
    ):
        _fail("legacy_reviewed/reference/create")
    create_facts = {
        "native_type_id": result.TypeId,
        "support_subname": bindings.support_subname,
        "geometry": _reference_geometry_facts(result),
    }
    before_edit = _reference_geometry_facts(result)
    if support is base:
        base.Shape = Part.makeBox(20, 20, 15)
        edit_property = "support_box_height"
        edit_value = 15.0
    else:
        source.Shape = Part.makeCylinder(3, 12)
        edit_property = "support_cylinder_height"
        edit_value = 12.0
    recompute_result = document.recompute()
    after_edit = _reference_geometry_facts(result)
    if not result.isValid() or after_edit == before_edit:
        _fail("legacy_reviewed/reference/edit")
    edit_facts = {
        "property": edit_property,
        "value": edit_value,
        "before": before_edit,
        "after": after_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(result.State),
        "valid": bool(result.isValid()),
    }
    model_path = temporary_root / f"{suffix}.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    result_name = result.Name
    body_name = body.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted = reopened.getObject(result_name)
    persisted_body = reopened.getObject(body_name)
    if (
        persisted is None
        or persisted_body is None
        or persisted.TypeId != expected_type
        or persisted not in persisted_body.Group
        or not persisted.isValid()
    ):
        _fail("legacy_reviewed/reference/reopen")
    reopen_facts = {
        "native_type_id": persisted.TypeId,
        "geometry": _reference_geometry_facts(persisted),
    }
    freecad.closeDocument(reopened.Name)

    negative_document = freecad.newDocument(f"LegacyReferenceNegative_{suffix}")
    negative_document.UndoMode = 1
    negative_body, _base, _source, _support, negative_bindings = _reference_fixture(
        Part,
        negative_document,
        plan,
        suffix=f"negative_{suffix}",
    )
    negative_before = _document_snapshot(negative_document, negative_body)
    rejected = False
    try:
        apply_partdesign_reference_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=negative_bindings,
        )
    except ReferenceRuleError:
        rejected = True
    if not rejected or _document_snapshot(negative_document, negative_body) != negative_before:
        _fail("legacy_reviewed/reference/negative")
    freecad.closeDocument(negative_document.Name)

    rollback_document = freecad.newDocument(f"LegacyReferenceRollback_{suffix}")
    rollback_document.UndoMode = 1
    rollback_body, rollback_base, rollback_source, rollback_support, _ = _reference_fixture(
        Part,
        rollback_document,
        plan,
        suffix=f"rollback_{suffix}",
    )
    rollback_bindings = ReferenceExecutionBindings(
        document=rollback_document,
        body=_FaultAfterCreateBody(rollback_body),
        support=rollback_support,
        body_id=plan.body_id,
        support_reference_id=plan.support_reference_id,
        support_reference_sha256=plan.support_reference_sha256,
        support_subname=_REFERENCE_SUBNAMES[kind],
    )
    rollback_before = _document_snapshot(rollback_document, rollback_body)
    rollback_rejected = False
    try:
        apply_partdesign_reference_plan(
            raw,
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=rollback_bindings,
        )
    except ReferenceRuleError:
        rollback_rejected = True
    if (
        not rollback_rejected
        or _document_snapshot(rollback_document, rollback_body) != rollback_before
        or rollback_base not in rollback_body.Group
        or rollback_source.Document is not rollback_document
    ):
        _fail("legacy_reviewed/reference/rollback")
    freecad.closeDocument(rollback_document.Name)
    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: {"rejected": True, "mutation": False},
        ReviewedConformanceFacet.LATE_ROLLBACK: {
            "fault": "post-create",
            "rejected": True,
            "state_restored": True,
        },
    }


def _planar_mechanical_plan(
    manifest: FamilyBatchManifest,
    *,
    suffix: str,
) -> PlanarMechanicalBackendPlan:
    def digest(label: str) -> str:
        return hashlib.sha256(f"{label}:{suffix}".encode("ascii")).hexdigest()

    add_node_id = f"add_{suffix}"
    add_result_id = f"add_result_{suffix}"
    remove_node_id = f"remove_{suffix}"
    remove_result_id = f"remove_result_{suffix}"
    return PlanarMechanicalBackendPlan(
        sketch_document=PlanarDocumentBinding(
            artifact_id=f"sketch_artifact_{suffix}",
            document_id=f"sketch_document_{suffix}",
            document_digest=digest("sketch_document"),
            content_sha256=digest("sketch_content"),
        ),
        parametric_document=PlanarDocumentBinding(
            artifact_id=f"parametric_artifact_{suffix}",
            document_id=f"parametric_document_{suffix}",
            document_digest=digest("parametric_document"),
            content_sha256=digest("parametric_content"),
        ),
        lowering_request_sha256=digest("lowering_request"),
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        body_id=f"body_{suffix}",
        profiles_node_id=f"profiles_{suffix}",
        add_node_id=add_node_id,
        add_result_id=add_result_id,
        final_node_id=remove_node_id,
        final_result_id=remove_result_id,
        depth_parameter_id=f"depth_{suffix}",
        depth_mm=8.0,
        rectangle=PlanarRectangleProfile(
            geometry_id=f"rectangle_{suffix}",
            profile_result_id=f"rectangle_profile_{suffix}",
            center_x_mm=0.0,
            center_y_mm=0.0,
            half_width_mm=20.0,
            half_height_mm=15.0,
            rotation_radians=0.0,
        ),
        circles=(
            PlanarCircleRemoval(
                geometry_id=f"circle_{suffix}",
                profile_result_id=f"circle_profile_{suffix}",
                node_id=remove_node_id,
                result_id=remove_result_id,
                base_node_id=add_node_id,
                base_result_id=add_result_id,
                center_x_mm=0.0,
                center_y_mm=0.0,
                radius_mm=3.0,
            ),
        ),
    )


def _planar_mechanical_focus(
    document: object,
    receipt: object,
    operation_id: str,
) -> object:
    if operation_id == "partdesign.planar-mechanical.reference-profiles":
        return document.getObject(receipt.outer_sketch_name)
    if operation_id == "partdesign.planar-mechanical.add":
        return document.getObject(receipt.pad_name)
    if operation_id == "partdesign.planar-mechanical.remove":
        return document.getObject(receipt.pocket_names[-1])
    _fail("legacy_reviewed/planar_mechanical/operation")


def _planar_mechanical_facts(
    body: object,
    focused: object,
) -> dict[str, object]:
    placement = focused.Placement
    return {
        "native_type_id": focused.TypeId,
        "focused_name": focused.Name,
        "focused_placement": [
            round(float(item), 9)
            for item in (
                placement.Base.x,
                placement.Base.y,
                placement.Base.z,
                placement.Rotation.Q[0],
                placement.Rotation.Q[1],
                placement.Rotation.Q[2],
                placement.Rotation.Q[3],
            )
        ],
        "tip_name": body.Tip.Name,
        "tip_shape": _shape_facts(body.Tip.Shape),
    }


def _execute_planar_mechanical_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    suffix = operation_id.rsplit(".", 1)[-1].replace("-", "_")
    plan = _planar_mechanical_plan(manifest, suffix=suffix)
    raw = plan.canonical_bytes
    document = freecad.newDocument(f"LegacyPlanarMechanical_{suffix}")
    document.UndoMode = 1
    receipt = apply_planar_mechanical_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=PlanarMechanicalExecutionBindings(document=document),
    )
    body = document.getObject(receipt.body_name)
    focused = _planar_mechanical_focus(document, receipt, operation_id)
    expected_type = LEGACY_REVIEWED_OPERATION_SPEC_BY_ID[operation_id].native_type_id
    if (
        body is None
        or focused is None
        or focused.TypeId != expected_type
        or focused not in body.Group
        or not body.Tip.isValid()
    ):
        _fail("legacy_reviewed/planar_mechanical/create")
    create_facts = _planar_mechanical_facts(body, focused)
    before_edit = _planar_mechanical_facts(body, focused)
    if operation_id == "partdesign.planar-mechanical.reference-profiles":
        focused.Placement.Base.x = 1.0
        edit_property = "Placement.Base.x"
        edit_value = 1.0
    elif operation_id == "partdesign.planar-mechanical.add":
        focused.Length = 9.0
        edit_property = "Length"
        edit_value = 9.0
    else:
        circle_sketch = document.getObject(receipt.circle_sketch_names[-1])
        circle_sketch.setDatum(0, freecad.Units.Quantity("4 mm"))
        edit_property = "Profile.Radius"
        edit_value = 4.0
    recompute_result = document.recompute()
    after_edit = _planar_mechanical_facts(body, focused)
    if not body.Tip.isValid() or after_edit == before_edit:
        _fail("legacy_reviewed/planar_mechanical/edit")
    edit_facts = {
        "property": edit_property,
        "value": edit_value,
        "before": before_edit,
        "after": after_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(body.Tip.State),
        "valid": bool(body.Tip.isValid()),
    }
    model_path = temporary_root / f"{suffix}.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    body_name = body.Name
    focused_name = focused.Name
    tip_name = body.Tip.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted_body = reopened.getObject(body_name)
    persisted_focus = reopened.getObject(focused_name)
    persisted_tip = reopened.getObject(tip_name)
    if (
        persisted_body is None
        or persisted_focus is None
        or persisted_tip is None
        or persisted_focus.TypeId != expected_type
        or persisted_focus not in persisted_body.Group
        or persisted_body.Tip is not persisted_tip
        or not persisted_tip.isValid()
    ):
        _fail("legacy_reviewed/planar_mechanical/reopen")
    reopen_facts = _planar_mechanical_facts(persisted_body, persisted_focus)
    freecad.closeDocument(reopened.Name)

    negative = freecad.newDocument(f"LegacyPlanarMechanicalNegative_{suffix}")
    negative.UndoMode = 1
    negative_before = (tuple(negative.Objects), bool(negative.HasPendingTransaction))
    rejected = False
    try:
        apply_planar_mechanical_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=PlanarMechanicalExecutionBindings(document=negative),
        )
    except PlanarMechanicalRuleError:
        rejected = True
    negative_after = (tuple(negative.Objects), bool(negative.HasPendingTransaction))
    if not rejected or negative_after != negative_before:
        _fail("legacy_reviewed/planar_mechanical/negative")
    freecad.closeDocument(negative.Name)

    rollback = freecad.newDocument(f"LegacyPlanarMechanicalRollback_{suffix}")
    rollback.UndoMode = 1
    rollback_before = (tuple(rollback.Objects), bool(rollback.HasPendingTransaction))
    rollback_rejected = False
    try:
        apply_planar_mechanical_plan(
            raw,
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=PlanarMechanicalExecutionBindings(
                document=_FaultOnFirstRecomputeDocument(rollback)
            ),
        )
    except PlanarMechanicalRuleError:
        rollback_rejected = True
    rollback_after = (tuple(rollback.Objects), bool(rollback.HasPendingTransaction))
    if not rollback_rejected or rollback_after != rollback_before:
        _fail("legacy_reviewed/planar_mechanical/rollback")
    freecad.closeDocument(rollback.Name)
    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: {"rejected": True, "mutation": False},
        ReviewedConformanceFacet.LATE_ROLLBACK: {
            "fault": "post-create-recompute",
            "rejected": True,
            "state_restored": True,
        },
    }


def _promotion_plan(
    operation: PartDesignPromotionOperation,
    manifest: FamilyBatchManifest,
    *,
    suffix: str,
) -> PartDesignPromotionBackendPlan:
    def digest(label: str) -> str:
        return hashlib.sha256(f"{label}:{suffix}".encode("ascii")).hexdigest()

    family = operation.value.rsplit("_", 1)[-1]
    base = SemanticObjectSelection(
        node_id=f"base_{suffix}",
        result_id=f"base_result_{suffix}",
    )
    profile_count = 2 if family == "loft" else 1
    profiles = tuple(
        SemanticObjectSelection(
            node_id=f"profile_{suffix}_{index}",
            result_id=f"profile_result_{suffix}_{index}",
        )
        for index in range(profile_count)
    )
    spine = (
        SemanticObjectSelection(
            node_id=f"spine_{suffix}",
            result_id=f"spine_result_{suffix}",
        )
        if family == "pipe"
        else None
    )
    return PartDesignPromotionBackendPlan(
        source_artifact_id=f"artifact_{suffix}",
        source_graph_id=f"graph_{suffix}",
        source_graph_sha256=digest("graph"),
        source_content_sha256=digest("content"),
        lowering_request_sha256=digest("request"),
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        body_id=f"body_{suffix}",
        node_id=f"node_{suffix}",
        result_id=f"result_{suffix}",
        operation=operation,
        base=base,
        profiles=profiles,
        spine=spine,
        axis_reference_id=f"axis_{suffix}" if family == "helix" else None,
        axis_result_id=f"axis_result_{suffix}" if family == "helix" else None,
        pitch_mm=4.0 if family == "helix" else None,
        height_mm=12.0 if family == "helix" else None,
        angle_degrees=0.0 if family == "helix" else None,
    )


def _promotion_add_circle(
    freecad: object,
    part: object,
    sketcher: object,
    body: object,
    name: str,
    *,
    x: float,
    radius: float,
    z: float = 0.0,
) -> object:
    sketch = body.newObject("Sketcher::SketchObject", name)
    geometry = sketch.addGeometry(
        part.Circle(
            freecad.Vector(x, 0, 0),
            freecad.Vector(0, 0, 1),
            radius,
        ),
        False,
    )
    sketch.addConstraint(sketcher.Constraint("Radius", geometry, radius))
    sketch.Placement = freecad.Placement(
        freecad.Vector(0, 0, z),
        freecad.Rotation(),
    )
    return sketch


def _promotion_add_path(
    freecad: object,
    part: object,
    sketcher: object,
    body: object,
    name: str,
    *,
    x: float,
    length: float,
    z: float,
) -> object:
    sketch = body.newObject("Sketcher::SketchObject", name)
    geometry = sketch.addGeometry(
        part.LineSegment(
            freecad.Vector(x, 0, 0),
            freecad.Vector(x, length, 0),
        ),
        False,
    )
    sketch.addConstraint(sketcher.Constraint("Distance", geometry, length))
    sketch.Placement = freecad.Placement(
        freecad.Vector(0, 0, z),
        freecad.Rotation(freecad.Vector(1, 0, 0), 90),
    )
    return sketch


def _promotion_fixture(
    freecad: object,
    part: object,
    sketcher: object,
    document: object,
    plan: PartDesignPromotionBackendPlan,
    *,
    suffix: str,
    far: bool = False,
) -> tuple[object, object, tuple[object, ...], object | None, PartDesignPromotionExecutionBindings]:
    operation = plan.operation.value
    family = plan.family
    body = document.addObject("PartDesign::Body", f"Body_{suffix}")
    base = body.newObject("PartDesign::Feature", f"Base_{suffix}")
    if family == "helix":
        base.Shape = part.makeCylinder(
            6 if plan.additive else 10,
            12,
            freecad.Vector(0, 0, 0),
            freecad.Vector(0, 1, 0),
        )
    else:
        base.Shape = part.makeCylinder(10, 5 if plan.additive else 20)
    base_auth = AuthenticatedPromotionObject(
        object=base,
        node_id=plan.base.node_id,
        result_id=plan.base.result_id,
    )
    center = 30 if far else (9 if family == "helix" and not plan.additive else 5)
    if family == "loft":
        start_z = 5 if operation == "additive_loft" else 0
        profiles = tuple(
            _promotion_add_circle(
                freecad,
                part,
                sketcher,
                body,
                f"Profile_{suffix}_{index}",
                x=center if far else 0,
                radius=3 + 2 * (index / (len(plan.profiles) - 1)),
                z=start_z + 10 * (index / (len(plan.profiles) - 1)),
            )
            for index in range(len(plan.profiles))
        )
    else:
        start_z = 5 if operation == "additive_pipe" else 0
        radius = 2 if family == "pipe" or not plan.additive else 1
        profiles = (
            _promotion_add_circle(
                freecad,
                part,
                sketcher,
                body,
                f"Profile_{suffix}_0",
                x=center,
                radius=radius,
                z=start_z,
            ),
        )
    profile_auth = tuple(
        AuthenticatedPromotionObject(
            object=item,
            node_id=selection.node_id,
            result_id=selection.result_id,
        )
        for item, selection in zip(profiles, plan.profiles, strict=True)
    )
    spine = None
    spine_auth = None
    if family == "pipe":
        start_z = 5 if operation == "additive_pipe" else 0
        spine = _promotion_add_path(
            freecad,
            part,
            sketcher,
            body,
            f"Spine_{suffix}",
            x=center,
            length=15,
            z=start_z,
        )
        spine_auth = AuthenticatedPromotionObject(
            object=spine,
            node_id=plan.spine.node_id,
            result_id=plan.spine.result_id,
        )
    document.recompute()
    return (
        body,
        base,
        profiles,
        spine,
        PartDesignPromotionExecutionBindings(
            document=document,
            body=body,
            body_id=plan.body_id,
            base=base_auth,
            profiles=profile_auth,
            spine=spine_auth,
        ),
    )


def _promotion_facts(feature: object) -> dict[str, object]:
    return {
        "native_type_id": feature.TypeId,
        "name": feature.Name,
        "shape": _shape_facts(feature.Shape),
        "base_name": None if feature.BaseFeature is None else feature.BaseFeature.Name,
        "profile_name": feature.Profile[0].Name,
    }


def _execute_promotion_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    import Part  # type: ignore[import-not-found]  # noqa: PLC0415
    import Sketcher  # type: ignore[import-not-found]  # noqa: PLC0415

    operation = PartDesignPromotionOperation(operation_id.removeprefix("partdesign."))
    suffix = operation.value
    plan = _promotion_plan(operation, manifest, suffix=suffix)
    raw = plan.canonical_bytes
    document = freecad.newDocument(f"LegacyPromotion_{suffix}")
    document.UndoMode = 1
    body, _base, profiles, spine, bindings = _promotion_fixture(
        freecad,
        Part,
        Sketcher,
        document,
        plan,
        suffix=suffix,
    )
    receipt = apply_partdesign_promotion_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_name)
    expected_type = LEGACY_REVIEWED_OPERATION_SPEC_BY_ID[operation_id].native_type_id
    if (
        feature is None
        or feature.TypeId != expected_type
        or feature is not body.Tip
        or not feature.isValid()
    ):
        _fail("legacy_reviewed/promotion/create")
    create_facts = _promotion_facts(feature)
    before_edit = _promotion_facts(feature)
    if plan.family == "loft":
        profiles[-1].setDatum(0, freecad.Units.Quantity("4 mm"))
        edit_property = "Sections[-1].Radius"
        edit_value = 4.0
    elif plan.family == "pipe":
        spine.setDatum(0, freecad.Units.Quantity("12 mm"))
        edit_property = "Spine.Length"
        edit_value = 12.0
    else:
        feature.Height = 10.0
        edit_property = "Height"
        edit_value = 10.0
    recompute_result = document.recompute()
    after_edit = _promotion_facts(feature)
    if not feature.isValid() or after_edit == before_edit:
        _fail("legacy_reviewed/promotion/edit")
    edit_facts = {
        "property": edit_property,
        "value": edit_value,
        "before": before_edit,
        "after": after_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(feature.State),
        "valid": bool(feature.isValid()),
    }
    model_path = temporary_root / f"{suffix}.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    feature_name = feature.Name
    body_name = body.Name
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted = reopened.getObject(feature_name)
    persisted_body = reopened.getObject(body_name)
    if (
        persisted is None
        or persisted_body is None
        or persisted.TypeId != expected_type
        or persisted is not persisted_body.Tip
        or not persisted.isValid()
    ):
        _fail("legacy_reviewed/promotion/reopen")
    reopen_facts = _promotion_facts(persisted)
    freecad.closeDocument(reopened.Name)

    negative = freecad.newDocument(f"LegacyPromotionNegative_{suffix}")
    negative.UndoMode = 1
    negative_body, *_rest, negative_bindings = _promotion_fixture(
        freecad,
        Part,
        Sketcher,
        negative,
        plan,
        suffix=f"negative_{suffix}",
    )
    negative_before = _document_snapshot(negative, negative_body)
    rejected = False
    try:
        apply_partdesign_promotion_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=negative_bindings,
        )
    except PartDesignPromotionRuleError:
        rejected = True
    if not rejected or _document_snapshot(negative, negative_body) != negative_before:
        _fail("legacy_reviewed/promotion/negative")
    freecad.closeDocument(negative.Name)

    rollback = freecad.newDocument(f"LegacyPromotionRollback_{suffix}")
    rollback.UndoMode = 1
    rollback_body, *_rest, rollback_bindings = _promotion_fixture(
        freecad,
        Part,
        Sketcher,
        rollback,
        plan,
        suffix=f"rollback_{suffix}",
        far=True,
    )
    rollback_before = _document_snapshot(rollback, rollback_body)
    rollback_rejected = False
    try:
        apply_partdesign_promotion_plan(
            raw,
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=rollback_bindings,
        )
    except PartDesignPromotionRuleError:
        rollback_rejected = True
    if not rollback_rejected or _document_snapshot(rollback, rollback_body) != rollback_before:
        _fail("legacy_reviewed/promotion/rollback")
    freecad.closeDocument(rollback.Name)
    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: {"rejected": True, "mutation": False},
        ReviewedConformanceFacet.LATE_ROLLBACK: {
            "fault": "disconnected-native-result",
            "rejected": True,
            "state_restored": True,
        },
    }


def _dressup_parameters(
    operation: PartDesignDressupTransformOperation,
    *,
    failure: bool,
) -> dict[str, object]:
    edge_role = {"axis": "z", "first_side": "minimum", "second_side": "minimum"}
    face_role = {"axis": "z", "side": "maximum"}
    result: dict[str, object] = {
        PartDesignDressupTransformOperation.SCALED: {
            "factor": 1.5,
            "occurrences": 2,
        },
        PartDesignDressupTransformOperation.MULTI_TRANSFORM: {
            "steps": [
                {
                    "step_id": "scale_primary",
                    "kind": "scaled",
                    "parameters": {"factor": 1.25, "occurrences": 2},
                },
                {
                    "step_id": "mirror_yz",
                    "kind": "mirrored",
                    "parameters": {"mirror_plane": "yz"},
                },
            ]
        },
        PartDesignDressupTransformOperation.FILLET: {
            "edge_role": edge_role,
            "radius_mm": 1.0,
        },
        PartDesignDressupTransformOperation.CHAMFER: {
            "edge_role": edge_role,
            "size_mm": 1.0,
        },
        PartDesignDressupTransformOperation.DRAFT: {
            "face_role": face_role,
            "neutral_plane": "yz",
            "pull_direction": "x",
            "angle_degrees": 5.0,
            "reversed": False,
        },
        PartDesignDressupTransformOperation.THICKNESS: {
            "face_role": face_role,
            "value_mm": 1.0,
        },
    }[operation]
    if not failure:
        return result
    if operation is PartDesignDressupTransformOperation.SCALED:
        result["factor"] = 1.0
    elif operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM:
        result["steps"] = [
            {
                "step_id": "scale_noop_a",
                "kind": "scaled",
                "parameters": {"factor": 1.0, "occurrences": 2},
            },
            {
                "step_id": "scale_noop_b",
                "kind": "scaled",
                "parameters": {"factor": 1.0, "occurrences": 2},
            },
        ]
    elif operation is PartDesignDressupTransformOperation.FILLET:
        result["radius_mm"] = 1_000.0
    elif operation is PartDesignDressupTransformOperation.CHAMFER:
        result["size_mm"] = 1_000.0
    elif operation is PartDesignDressupTransformOperation.DRAFT:
        result["angle_degrees"] = 0.0
    else:
        result["value_mm"] = 1_000.0
    return result


def _dressup_plan(
    operation: PartDesignDressupTransformOperation,
    manifest: FamilyBatchManifest,
    *,
    suffix: str,
    failure: bool = False,
) -> PartDesignDressupTransformBackendPlan:
    def digest(label: str) -> str:
        return hashlib.sha256(f"{label}:{suffix}".encode("ascii")).hexdigest()

    return PartDesignDressupTransformBackendPlan(
        source_artifact_id=f"artifact_{suffix}",
        source_graph_id=f"graph_{suffix}",
        source_graph_sha256=digest("graph"),
        source_content_sha256=digest("content"),
        lowering_request_sha256=digest("request"),
        adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
        body_id=f"body_{suffix}",
        node_id=f"node_{suffix}",
        result_id=f"result_{suffix}",
        operation=operation,
        base=DressupTransformObjectSelection(
            node_id=f"base_{suffix}",
            result_id=f"base_result_{suffix}",
        ),
        parameter_id=f"parameter_{suffix}",
        value_id=f"value_{suffix}",
        parameters=operation_parameters_from_value(
            operation,
            _dressup_parameters(operation, failure=failure),
        ),
    )


def _dressup_fixture(
    part: object,
    document: object,
    plan: PartDesignDressupTransformBackendPlan,
    *,
    suffix: str,
) -> tuple[object, object, PartDesignDressupTransformExecutionBindings]:
    body = document.addObject("PartDesign::Body", f"Body_{suffix}")
    base = body.newObject("PartDesign::AdditiveBox", f"Base_{suffix}")
    base.Length = 10.0
    base.Width = 12.0
    base.Height = 14.0
    document.recompute()
    if base.Shape is None or base.Shape.isNull() or not base.Shape.isValid():
        # This import/use keeps the fixture's real native shape contract
        # explicit even on FreeCAD builds that lazily realize primitives.
        base.Shape = part.makeBox(10, 12, 14)
        document.recompute()
    authenticated = AuthenticatedDressupTransformObject(
        object=base,
        node_id=plan.base.node_id,
        result_id=plan.base.result_id,
    )
    return (
        body,
        base,
        PartDesignDressupTransformExecutionBindings(
            document=document,
            body=body,
            body_id=plan.body_id,
            base=authenticated,
        ),
    )


def _dressup_facts(feature: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "native_type_id": feature.TypeId,
        "name": feature.Name,
        "shape": _shape_facts(feature.Shape),
        "base_name": feature.BaseFeature.Name,
    }
    if feature.TypeId == "PartDesign::MultiTransform":
        facts["transformations"] = [
            {"name": item.Name, "native_type_id": item.TypeId} for item in feature.Transformations
        ]
    return facts


def _execute_dressup_operation(
    freecad: object,
    operation_id: str,
    manifest: FamilyBatchManifest,
    temporary_root: Path,
) -> dict[ReviewedConformanceFacet, dict[str, object]]:
    import Part  # type: ignore[import-not-found]  # noqa: PLC0415

    operation = PartDesignDressupTransformOperation(operation_id.removeprefix("partdesign."))
    suffix = operation.value
    plan = _dressup_plan(operation, manifest, suffix=suffix)
    raw = plan.canonical_bytes
    document = freecad.newDocument(f"LegacyDressup_{suffix}")
    document.UndoMode = 1
    body, base, bindings = _dressup_fixture(Part, document, plan, suffix=suffix)
    receipt = apply_partdesign_dressup_transform_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )
    feature = document.getObject(receipt.object_names[0])
    expected_type = LEGACY_REVIEWED_OPERATION_SPEC_BY_ID[operation_id].native_type_id
    if (
        feature is None
        or feature.TypeId != expected_type
        or feature is not body.Tip
        or not feature.isValid()
    ):
        _fail("legacy_reviewed/dressup/create")
    create_facts = _dressup_facts(feature)
    before_edit = _dressup_facts(feature)
    edit_target = (
        feature.Transformations[0]
        if operation is PartDesignDressupTransformOperation.MULTI_TRANSFORM
        else feature
    )
    edit_property, edit_value = {
        PartDesignDressupTransformOperation.SCALED: ("Factor", 1.6),
        PartDesignDressupTransformOperation.MULTI_TRANSFORM: ("Factor", 1.35),
        PartDesignDressupTransformOperation.FILLET: ("Radius", 1.5),
        PartDesignDressupTransformOperation.CHAMFER: ("Size", 1.5),
        PartDesignDressupTransformOperation.DRAFT: ("Angle", 8.0),
        PartDesignDressupTransformOperation.THICKNESS: ("Value", 1.5),
    }[operation]
    setattr(edit_target, edit_property, edit_value)
    document.recompute()
    after_parameter_edit = _dressup_facts(feature)
    base.Length = 11.0
    recompute_result = document.recompute()
    after_base_edit = _dressup_facts(feature)
    if (
        not feature.isValid()
        or after_parameter_edit == before_edit
        or after_base_edit == after_parameter_edit
    ):
        _fail("legacy_reviewed/dressup/edit")
    edit_facts = {
        "property": edit_property,
        "value": edit_value,
        "before": before_edit,
        "after_parameter_edit": after_parameter_edit,
        "after_base_edit": after_base_edit,
    }
    recompute_facts = {
        "return": None if recompute_result is None else bool(recompute_result),
        "state": tuple(feature.State),
        "valid": bool(feature.isValid()),
    }
    model_path = temporary_root / f"{suffix}.FCStd"
    document.saveAs(str(model_path))
    saved = model_path.read_bytes()
    save_facts = _stable_fcstd_save_facts(saved)
    feature_name = feature.Name
    body_name = body.Name
    child_names = tuple(receipt.object_names[1:])
    freecad.closeDocument(document.Name)
    reopened = freecad.openDocument(str(model_path))
    reopened.recompute()
    persisted = reopened.getObject(feature_name)
    persisted_body = reopened.getObject(body_name)
    if (
        persisted is None
        or persisted_body is None
        or persisted.TypeId != expected_type
        or persisted is not persisted_body.Tip
        or not persisted.isValid()
        or child_names
        and tuple(item.Name for item in persisted.Transformations) != child_names
    ):
        _fail("legacy_reviewed/dressup/reopen")
    reopen_facts = _dressup_facts(persisted)
    freecad.closeDocument(reopened.Name)

    negative = freecad.newDocument(f"LegacyDressupNegative_{suffix}")
    negative.UndoMode = 1
    negative_body, _negative_base, negative_bindings = _dressup_fixture(
        Part,
        negative,
        plan,
        suffix=f"negative_{suffix}",
    )
    negative_before = _document_snapshot(negative, negative_body)
    rejected = False
    try:
        apply_partdesign_dressup_transform_plan(
            raw + b" ",
            expected_content_sha256=_content_sha256(raw),
            expected_plan_sha256=plan.plan_sha256,
            bindings=negative_bindings,
        )
    except PartDesignDressupTransformRuleError:
        rejected = True
    if not rejected or _document_snapshot(negative, negative_body) != negative_before:
        _fail("legacy_reviewed/dressup/negative")
    freecad.closeDocument(negative.Name)

    rollback_plan = _dressup_plan(
        operation,
        manifest,
        suffix=f"rollback_{suffix}",
        failure=True,
    )
    rollback_raw = rollback_plan.canonical_bytes
    rollback = freecad.newDocument(f"LegacyDressupRollback_{suffix}")
    rollback.UndoMode = 1
    rollback_body, _rollback_base, rollback_bindings = _dressup_fixture(
        Part,
        rollback,
        rollback_plan,
        suffix=f"rollback_{suffix}",
    )
    rollback_before = _document_snapshot(rollback, rollback_body)
    rollback_rejected = False
    try:
        apply_partdesign_dressup_transform_plan(
            rollback_raw,
            expected_content_sha256=_content_sha256(rollback_raw),
            expected_plan_sha256=rollback_plan.plan_sha256,
            bindings=rollback_bindings,
        )
    except PartDesignDressupTransformRuleError:
        rollback_rejected = True
    if not rollback_rejected or _document_snapshot(rollback, rollback_body) != rollback_before:
        _fail("legacy_reviewed/dressup/rollback")
    freecad.closeDocument(rollback.Name)
    return {
        ReviewedConformanceFacet.CREATE: create_facts,
        ReviewedConformanceFacet.EDIT: edit_facts,
        ReviewedConformanceFacet.RECOMPUTE: recompute_facts,
        ReviewedConformanceFacet.SAVE: save_facts,
        ReviewedConformanceFacet.REOPEN: reopen_facts,
        ReviewedConformanceFacet.NEGATIVE: {"rejected": True, "mutation": False},
        ReviewedConformanceFacet.LATE_ROLLBACK: {
            "fault": "invalid-or-noop-native-result",
            "rejected": True,
            "state_restored": True,
        },
    }


class _LegacyFamilyExecutor:
    def __init__(self, freecad: object, manifest: FamilyBatchManifest) -> None:
        self._freecad = freecad
        self._manifest = manifest
        self._cache: dict[str, dict[ReviewedConformanceFacet, dict[str, object]]] = {}

    def __call__(self, case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        if case.operation_id not in {item.operation_id for item in self._manifest.operations}:
            _fail("legacy_reviewed/case_operation")
        facts = self._cache.get(case.operation_id)
        if facts is None:
            with tempfile.TemporaryDirectory(prefix="vibecad-legacy-reviewed-") as temporary:
                if self._manifest.adapter.adapter_id == "freecad_partdesign_primitive_adapter":
                    facts = _execute_primitive_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                elif self._manifest.adapter.adapter_id == "freecad_partdesign_boolean_adapter":
                    facts = _execute_boolean_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                elif self._manifest.adapter.adapter_id == "freecad_partdesign_pattern_adapter":
                    facts = _execute_pattern_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                elif self._manifest.adapter.adapter_id == "freecad_parametric_groove_adapter":
                    facts = _execute_groove_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                elif self._manifest.adapter.adapter_id == "freecad_partdesign_reference_adapter":
                    facts = _execute_reference_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                elif self._manifest.adapter.adapter_id == "freecad_planar_mechanical_v1_adapter":
                    facts = _execute_planar_mechanical_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                elif self._manifest.adapter.adapter_id == "freecad_partdesign_promotion_adapter":
                    facts = _execute_promotion_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                elif (
                    self._manifest.adapter.adapter_id
                    == "freecad_partdesign_dressup_transform_adapter"
                ):
                    facts = _execute_dressup_operation(
                        self._freecad,
                        case.operation_id,
                        self._manifest,
                        Path(temporary),
                    )
                else:
                    _fail("legacy_reviewed/unimplemented_family")
            self._cache[case.operation_id] = facts
        evidence = facts.get(case.facet)
        if evidence is None:
            _fail("legacy_reviewed/facet")
        body = {
            "authority": "none",
            "case_sha256": case.case_sha256,
            "challenge_sha256": challenge_sha256,
            "evidence": evidence,
            "facet": case.facet.value,
            "family_manifest_sha256": self._manifest.manifest_sha256,
            "operation_id": case.operation_id,
        }
        return _canonical(
            {
                **body,
                "observation_sha256": hashlib.sha256(
                    _OBSERVATION_DOMAIN + _canonical(body)
                ).hexdigest(),
            }
        )


def build_managed_freecad_legacy_reviewed_verification_receipts(
    *,
    freecad: object,
) -> tuple[tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding], ...]:
    """Run each exact legacy family and return ephemeral receipt/binding pairs."""

    if not _VERIFICATION_LOCK.acquire(blocking=False):
        _fail("legacy_reviewed/concurrent_verification")
    owned_before = {}
    try:
        owned_before = dict(freecad.listDocuments())
        results = []
        for manifest, case_manifest in zip(
            LEGACY_REVIEWED_FAMILY_MANIFESTS,
            LEGACY_REVIEWED_CASE_MANIFESTS,
            strict=True,
        ):
            host = build_managed_freecad_conformance_host(
                freecad=freecad,
                case_manifest=case_manifest,
                execute_case=_LegacyFamilyExecutor(freecad, manifest),
                verifier_id=LEGACY_REVIEWED_VERIFIER_ID,
                verifier_version=LEGACY_REVIEWED_VERIFIER_VERSION,
            )
            receipt = build_reviewed_verification_receipt(
                manifest=manifest,
                case_manifest=case_manifest,
                host=host,
            )
            results.append((receipt, build_promotion_verification_binding(receipt)))
        return tuple(results)
    finally:
        try:
            current = freecad.listDocuments()
        except Exception:
            current = {}
        created = {name: document for name, document in current.items() if name not in owned_before}
        _close_owned_documents(freecad, created)
        _VERIFICATION_LOCK.release()


__all__ = (
    "LEGACY_REVIEWED_CASE_MANIFESTS",
    "LEGACY_REVIEWED_FAMILY_MANIFESTS",
    "LEGACY_REVIEWED_OPERATION_SPEC_BY_ID",
    "LEGACY_REVIEWED_OPERATION_SPECS",
    "LEGACY_REVIEWED_VERIFIER_ID",
    "LEGACY_REVIEWED_VERIFIER_VERSION",
    "build_managed_freecad_legacy_reviewed_verification_receipts",
)
