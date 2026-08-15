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
from dataclasses import dataclass
from typing import Final

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceFacet,
    _admit_reviewed_host_conformance_case_manifest,
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
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    MAX_PARTDESIGN_DRESSUP_TRANSFORM_PLAN_BYTES,
    PARTDESIGN_DRESSUP_TRANSFORM_PLAN_MEDIA_TYPE,
    PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
    PARTDESIGN_DRESSUP_TRANSFORM_RULE_ID,
)
from vibecad.parametric.freecad_partdesign_pattern_rules import (
    MAX_PARTDESIGN_PATTERN_PLAN_BYTES,
    PARTDESIGN_PATTERN_PLAN_MEDIA_TYPE,
    PARTDESIGN_PATTERN_RULE_CONTRACT_SHA256,
    PARTDESIGN_PATTERN_RULE_ID,
)
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES,
    PARTDESIGN_PRIMITIVE_PLAN_MEDIA_TYPE,
    PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
    PARTDESIGN_PRIMITIVE_RULE_ID,
)
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
    PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE,
    PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
    PARTDESIGN_PROMOTION_RULE_ID,
)
from vibecad.parametric.freecad_partdesign_reference_rules import (
    MAX_REFERENCE_PLAN_BYTES,
    REFERENCE_PLAN_MEDIA_TYPE,
    REFERENCE_RULE_CONTRACT_SHA256,
    REFERENCE_RULE_ID,
)
from vibecad.parametric.freecad_partdesign_sketch_rules import (
    GROOVE_PLAN_MEDIA_TYPE,
    GROOVE_RULE_CONTRACT_SHA256,
    GROOVE_RULE_ID,
    MAX_GROOVE_PLAN_BYTES,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    MAX_PLANAR_MECHANICAL_PLAN_BYTES,
    PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    PLANAR_MECHANICAL_RULE_ID,
)

LEGACY_REVIEWED_VERIFIER_ID: Final = "vcad.managed.freecad.legacy-reviewed"
LEGACY_REVIEWED_VERIFIER_VERSION: Final = "1.0.0"

_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
_FREECAD_BUILD_DESCRIPTOR_SHA256: Final = hashlib.sha256(
    _FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()
_CASE_CONTRACT_DOMAIN = b"vibecad-freecad-legacy-reviewed-case-v1\0"


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


__all__ = (
    "LEGACY_REVIEWED_CASE_MANIFESTS",
    "LEGACY_REVIEWED_FAMILY_MANIFESTS",
    "LEGACY_REVIEWED_OPERATION_SPEC_BY_ID",
    "LEGACY_REVIEWED_OPERATION_SPECS",
    "LEGACY_REVIEWED_VERIFIER_ID",
    "LEGACY_REVIEWED_VERIFIER_VERSION",
)
