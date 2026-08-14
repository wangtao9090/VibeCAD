"""Static formal capability specs for reviewed built-in FreeCAD intent packs.

The tables in this module are metadata projections of already-reviewed adapter
and native-rule contracts.  Importing this module is side-effect free and does
not import FreeCAD.  Runtime discovery later proves whether every declared
TypeId exists on the exact managed build.
"""

from __future__ import annotations

from typing import Final

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogSegment,
    CapabilityExecutionProfile,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
)
from vibecad.execution.freecad_intent_capabilities import (
    FreeCadIntentCapabilitySpec,
    build_freecad_intent_capability_catalog,
)
from vibecad.intent_bridge.freecad_parametric_adapter import (
    FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
    GROOVE_OPERATION_TERM,
)
from vibecad.intent_bridge.freecad_partdesign_primitive_adapter import (
    FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
    PRIMITIVE_OPERATION_TERMS,
)
from vibecad.intent_bridge.freecad_partdesign_promotion_adapter import (
    FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
    PROMOTION_OPERATION_TERMS,
)
from vibecad.intent_bridge.freecad_partdesign_reference_adapter import (
    FREECAD_REFERENCE_ADAPTER_DESCRIPTOR,
    REFERENCE_OPERATION_TERMS,
)
from vibecad.intent_bridge.freecad_planar_mechanical_adapter import (
    FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
)
from vibecad.intent_rules.planar_mechanical_v1.terms import (
    PFG_OPERATION_ADD,
    PFG_OPERATION_REFERENCE_PROFILES,
    PFG_OPERATION_REMOVE,
)
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
    PARTDESIGN_PRIMITIVE_RULE_ID,
    PartDesignPrimitiveOperation,
)
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
    PARTDESIGN_PROMOTION_RULE_ID,
    PartDesignPromotionOperation,
)
from vibecad.parametric.freecad_partdesign_reference_rules import (
    REFERENCE_RULE_CONTRACT_SHA256,
    REFERENCE_RULE_ID,
    PartDesignReferenceKind,
)
from vibecad.parametric.freecad_partdesign_sketch_rules import (
    GROOVE_RULE_CONTRACT_SHA256,
    GROOVE_RULE_ID,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    PLANAR_MECHANICAL_RULE_ID,
)

_LIFECYCLE: Final = (
    CapabilityLifecycleStage.EXECUTE,
    CapabilityLifecycleStage.CREATE,
    CapabilityLifecycleStage.EDIT,
    CapabilityLifecycleStage.RECOMPUTE,
    CapabilityLifecycleStage.SAVE,
    CapabilityLifecycleStage.REOPEN,
)

_PROMOTION_NATIVE_TYPES: Final = {
    PartDesignPromotionOperation.ADDITIVE_LOFT: "PartDesign::AdditiveLoft",
    PartDesignPromotionOperation.SUBTRACTIVE_LOFT: "PartDesign::SubtractiveLoft",
    PartDesignPromotionOperation.ADDITIVE_PIPE: "PartDesign::AdditivePipe",
    PartDesignPromotionOperation.SUBTRACTIVE_PIPE: "PartDesign::SubtractivePipe",
    PartDesignPromotionOperation.ADDITIVE_HELIX: "PartDesign::AdditiveHelix",
    PartDesignPromotionOperation.SUBTRACTIVE_HELIX: "PartDesign::SubtractiveHelix",
}

_REFERENCE_NATIVE_TYPES: Final = {
    PartDesignReferenceKind.DATUM_PLANE: "PartDesign::Plane",
    PartDesignReferenceKind.DATUM_LINE: "PartDesign::Line",
    PartDesignReferenceKind.DATUM_POINT: "PartDesign::Point",
    PartDesignReferenceKind.SHAPE_BINDER: "PartDesign::ShapeBinder",
    PartDesignReferenceKind.SUBSHAPE_BINDER: "PartDesign::SubShapeBinder",
}

_PRIMITIVE_NATIVE_TYPES: Final = {
    PartDesignPrimitiveOperation.ADDITIVE_BOX: "PartDesign::AdditiveBox",
    PartDesignPrimitiveOperation.SUBTRACTIVE_BOX: "PartDesign::SubtractiveBox",
    PartDesignPrimitiveOperation.ADDITIVE_CYLINDER: "PartDesign::AdditiveCylinder",
    PartDesignPrimitiveOperation.SUBTRACTIVE_CYLINDER: "PartDesign::SubtractiveCylinder",
    PartDesignPrimitiveOperation.ADDITIVE_SPHERE: "PartDesign::AdditiveSphere",
    PartDesignPrimitiveOperation.SUBTRACTIVE_SPHERE: "PartDesign::SubtractiveSphere",
    PartDesignPrimitiveOperation.ADDITIVE_CONE: "PartDesign::AdditiveCone",
    PartDesignPrimitiveOperation.SUBTRACTIVE_CONE: "PartDesign::SubtractiveCone",
    PartDesignPrimitiveOperation.ADDITIVE_ELLIPSOID: "PartDesign::AdditiveEllipsoid",
    PartDesignPrimitiveOperation.SUBTRACTIVE_ELLIPSOID: "PartDesign::SubtractiveEllipsoid",
    PartDesignPrimitiveOperation.ADDITIVE_PRISM: "PartDesign::AdditivePrism",
    PartDesignPrimitiveOperation.SUBTRACTIVE_PRISM: "PartDesign::SubtractivePrism",
    PartDesignPrimitiveOperation.ADDITIVE_WEDGE: "PartDesign::AdditiveWedge",
    PartDesignPrimitiveOperation.SUBTRACTIVE_WEDGE: "PartDesign::SubtractiveWedge",
    PartDesignPrimitiveOperation.ADDITIVE_TORUS: "PartDesign::AdditiveTorus",
    PartDesignPrimitiveOperation.SUBTRACTIVE_TORUS: "PartDesign::SubtractiveTorus",
}

_PLANAR_MECHANICAL_NATIVE_TYPES: Final = (
    (
        "partdesign.planar-mechanical.reference-profiles",
        PFG_OPERATION_REFERENCE_PROFILES.term_id,
        "Sketcher::SketchObject",
    ),
    (
        "partdesign.planar-mechanical.add",
        PFG_OPERATION_ADD.term_id,
        "PartDesign::Pad",
    ),
    (
        "partdesign.planar-mechanical.remove",
        PFG_OPERATION_REMOVE.term_id,
        "PartDesign::Pocket",
    ),
)


def _spec(
    *,
    operation_id: str,
    semantic_operation: str,
    native_type_id: str,
    adapter,
    rule_id: str,
    rule_contract_sha256: str,
) -> FreeCadIntentCapabilitySpec:
    return FreeCadIntentCapabilitySpec(
        operation_id=operation_id,
        semantic_operation=semantic_operation,
        native_type_id=native_type_id,
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
        adapter_contract_sha256=adapter.adapter_contract_sha256,
        rule_id=rule_id,
        rule_contract_sha256=rule_contract_sha256,
        risk_class=CapabilityRiskClass.MUTATING,
        execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
        lifecycle_stages=_LIFECYCLE,
    )


def current_freecad_intent_capability_specs() -> tuple[FreeCadIntentCapabilitySpec, ...]:
    """Return the exact current built-in intent adapter declarations."""

    specs = [
        _spec(
            operation_id="partdesign.groove.angle",
            semantic_operation=GROOVE_OPERATION_TERM.term_id,
            native_type_id="PartDesign::Groove",
            adapter=FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
            rule_id=GROOVE_RULE_ID,
            rule_contract_sha256=GROOVE_RULE_CONTRACT_SHA256,
        )
    ]
    specs.extend(
        _spec(
            operation_id=f"partdesign.{item.operation.value}",
            semantic_operation=item.operation_term.term_id,
            native_type_id=_PROMOTION_NATIVE_TYPES[item.operation],
            adapter=FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
            rule_id=PARTDESIGN_PROMOTION_RULE_ID,
            rule_contract_sha256=PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
        )
        for item in PROMOTION_OPERATION_TERMS
    )
    specs.extend(
        _spec(
            operation_id=f"partdesign.{kind.value}",
            semantic_operation=REFERENCE_OPERATION_TERMS[kind].term_id,
            native_type_id=_REFERENCE_NATIVE_TYPES[kind],
            adapter=FREECAD_REFERENCE_ADAPTER_DESCRIPTOR,
            rule_id=REFERENCE_RULE_ID,
            rule_contract_sha256=REFERENCE_RULE_CONTRACT_SHA256,
        )
        for kind in PartDesignReferenceKind
    )
    specs.extend(
        _spec(
            operation_id=f"partdesign.{item.operation.value}",
            semantic_operation=item.operation_term.term_id,
            native_type_id=_PRIMITIVE_NATIVE_TYPES[item.operation],
            adapter=FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
            rule_id=PARTDESIGN_PRIMITIVE_RULE_ID,
            rule_contract_sha256=PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
        )
        for item in PRIMITIVE_OPERATION_TERMS
    )
    specs.extend(
        _spec(
            operation_id=operation_id,
            semantic_operation=semantic_operation,
            native_type_id=native_type_id,
            adapter=FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
            rule_id=PLANAR_MECHANICAL_RULE_ID,
            rule_contract_sha256=PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
        )
        for operation_id, semantic_operation, native_type_id in (_PLANAR_MECHANICAL_NATIVE_TYPES)
    )
    return tuple(sorted(specs, key=lambda item: item.operation_id))


def build_current_freecad_intent_capability_catalog(
    *, backend: CapabilityBackend
) -> CapabilityCatalogSegment:
    """Build the formal catalog consumed by the v2 capability projection."""

    return build_freecad_intent_capability_catalog(
        backend=backend,
        specs=current_freecad_intent_capability_specs(),
    )


__all__ = ()
