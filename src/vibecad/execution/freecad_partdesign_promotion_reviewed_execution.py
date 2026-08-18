"""Private product callbacks for reviewed PartDesign promotion operations.

The legacy promotion adapter already owns the six exact PFG identities and the
native rule owns every FreeCAD property name.  This module only supplies the
reviewed-family compatibility manifest and the ordered, engine-owned source
boundary needed by the product dispatcher.  Source order is always
``base? -> profiles (PFG ordinal order) -> spine?``; the Helix axis remains the
code-owned ``V_Axis`` semantic locator inside its profile and is never a public
object or sub-element selector.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge import freecad_partdesign_promotion_adapter as promotion_adapter
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef, IntentBridgeErrorCode
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_partdesign_promotion_adapter import (
    FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
    PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM,
    PROMOTION_CAPABILITY_SCHEMA_TERM,
    PROMOTION_INTENT_DOCUMENT_ROLE_TERM,
    PROMOTION_OPERATION_TERMS,
    PROMOTION_PLAN_DOCUMENT_ROLE_TERM,
    PROMOTION_PLAN_SCHEMA_TERM,
    PROMOTION_REQUEST_TERMS,
    PROMOTION_STRUCTURE_TERM,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanDraft,
    ReviewedPlanReceipt,
)
from vibecad.parametric import freecad_partdesign_promotion_rules as promotion_rules
from vibecad.parametric.feature_graph_v2 import (
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_promotion_rules import (
    MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
    PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE,
    PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
    PARTDESIGN_PROMOTION_RULE_ID,
    AuthenticatedPromotionObject,
    PartDesignPromotionBackendPlan,
    PartDesignPromotionConformanceReceipt,
    PartDesignPromotionExecutionBindings,
    PartDesignPromotionOperation,
    SemanticObjectSelection,
    apply_partdesign_promotion_plan,
    decode_partdesign_promotion_backend_plan,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.partdesign-promotion-ownership.v1\0"
_FREECAD_BUILD_DESCRIPTOR_SHA256 = hashlib.sha256(
    b"FreeCAD\0" + b"1.1.0\0" + PARTDESIGN_PROMOTION_FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _integrity_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _bridge_failure(path: str) -> None:
    from vibecad.intent_bridge.contracts import IntentBridgeError  # noqa: PLC0415

    raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)


PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_OPERATIONS: Final = tuple(PartDesignPromotionOperation)

# These property inventories describe, but do not select, the native rule.  The
# rule's private table remains the sole operation-to-TypeId execution authority.
_COMMON_PROPERTIES: Final = (
    "AllowMultiFace",
    "BaseFeature",
    "Midplane",
    "Profile",
    "Refine",
    "Reversed",
)
_FAMILY_PROPERTIES: Final = MappingProxyType(
    {
        "loft": ("Closed", "Ruled", "Sections"),
        "pipe": (
            "AuxiliaryCurvilinear",
            "AuxiliarySpine",
            "AuxiliarySpineTangent",
            "Mode",
            "Sections",
            "Spine",
            "SpineTangent",
            "Transformation",
            "Transition",
        ),
        "helix": (
            "Angle",
            "Growth",
            "Height",
            "LeftHanded",
            "Mode",
            "Outside",
            "Pitch",
            "ReferenceAxis",
            "Turns",
        ),
    }
)


def _native_spec(operation: PartDesignPromotionOperation) -> object:
    try:
        return promotion_rules._NATIVE_OPERATION_SPECS[operation]  # noqa: SLF001
    except (AttributeError, KeyError):
        _integrity_failure()


PARTDESIGN_PROMOTION_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=item.operation.value,
        semantic_term=_bridge_term(item.operation_term),
        native_type_id=_native_spec(item.operation).type_id,
        native_operation=item.operation.value,
        native_property_names=tuple(
            sorted((*_COMMON_PROPERTIES, *_FAMILY_PROPERTIES[item.family]))
        ),
    )
    for item in PROMOTION_OPERATION_TERMS
)

PARTDESIGN_PROMOTION_MANIFEST: Final = FamilyBatchManifest(
    # ``partdesign`` keeps the established formal ids
    # (for example ``partdesign.additive_loft``) without inventing a new public id.
    family_id="partdesign",
    family_version="1.0.0",
    adapter=FREECAD_PARTDESIGN_PROMOTION_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_FREECAD_BUILD_DESCRIPTOR_SHA256,
    rule_id=PARTDESIGN_PROMOTION_RULE_ID,
    rule_contract_sha256=PARTDESIGN_PROMOTION_RULE_CONTRACT_SHA256,
    intent_role_term=PROMOTION_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PROMOTION_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=PROMOTION_CAPABILITY_SCHEMA_TERM,
    capability_media_type=("application/vnd.vibecad.freecad-partdesign-promotion-capability+json"),
    plan_role_term=PROMOTION_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=PROMOTION_PLAN_SCHEMA_TERM,
    plan_media_type=PARTDESIGN_PROMOTION_PLAN_MEDIA_TYPE,
    request_terms=PROMOTION_REQUEST_TERMS,
    operations=PARTDESIGN_PROMOTION_OPERATION_SPECS,
    max_plan_bytes=MAX_PARTDESIGN_PROMOTION_PLAN_BYTES,
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PARTDESIGN_PROMOTION_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PARTDESIGN_PROMOTION_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_OPERATIONS
    }
)
PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)


@dataclass(frozen=True, slots=True)
class PartDesignPromotionSourceContract:
    """Bounded flattened-source contract for one operation."""

    operation: PartDesignPromotionOperation
    minimum: int
    maximum: int
    base_required: bool
    profile_minimum: int
    profile_maximum: int
    spine_required: bool

    def __post_init__(self) -> None:
        native = _native_spec(self.operation)
        family = native.family
        if (
            not 1 <= self.minimum <= self.maximum <= 8
            or self.base_required is not (not native.additive)
            or self.profile_minimum != (2 if family == "loft" else 1)
            or self.profile_maximum != (8 if family == "loft" else 1)
            or self.spine_required is not (family == "pipe")
        ):
            _integrity_failure()

    def selections(
        self, plan: PartDesignPromotionBackendPlan
    ) -> tuple[tuple[str, SemanticObjectSelection], ...]:
        if type(plan) is not PartDesignPromotionBackendPlan or plan.operation is not self.operation:
            _integrity_failure()
        items = (
            *((("base", plan.base),) if plan.base is not None else ()),
            *(("profile", item) for item in plan.profiles),
            *((("spine", plan.spine),) if plan.spine is not None else ()),
        )
        if (
            not self.minimum <= len(items) <= self.maximum
            or (plan.base is not None) is False
            and self.base_required
            or not self.profile_minimum <= len(plan.profiles) <= self.profile_maximum
            or (plan.spine is not None) is not self.spine_required
            or len({item.node_id for _, item in items}) != len(items)
            or len({item.result_id for _, item in items}) != len(items)
        ):
            _integrity_failure()
        return items


def _source_contract(operation: PartDesignPromotionOperation) -> PartDesignPromotionSourceContract:
    native = _native_spec(operation)
    profile_minimum = 2 if native.family == "loft" else 1
    profile_maximum = 8 if native.family == "loft" else 1
    base_minimum = 0 if native.additive else 1
    spine_count = 1 if native.family == "pipe" else 0
    return PartDesignPromotionSourceContract(
        operation=operation,
        minimum=base_minimum + profile_minimum + spine_count,
        # The executor collection is globally bounded at eight.  Therefore an
        # otherwise-valid 8-profile Loft with a base is deliberately inert.
        maximum=min(8, 1 + profile_maximum + spine_count),
        base_required=not native.additive,
        profile_minimum=profile_minimum,
        profile_maximum=profile_maximum,
        spine_required=native.family == "pipe",
    )


PARTDESIGN_PROMOTION_SOURCE_CONTRACTS: Final = MappingProxyType(
    {operation: _source_contract(operation) for operation in PartDesignPromotionOperation}
)


def _reviewed_plan_draft(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    if manifest is not PARTDESIGN_PROMOTION_MANIFEST:
        _bridge_failure("/manifest")
    try:
        graph = decode_parametric_feature_graph_v2(
            payload,
            expected_sha256=document.document_digest,
        )
        plan, subject = promotion_adapter._build_plan(  # noqa: SLF001
            document,
            payload,
            graph,
            request_digest,
        )
        operation = _OPERATIONS_BY_ID[plan.operation.value]
        PARTDESIGN_PROMOTION_SOURCE_CONTRACTS[plan.operation].selections(plan)
    except Exception as error:
        if getattr(error, "code", None) is not None:
            raise
        _bridge_failure("/intent_document")
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=operation.semantic_term,
        subjects=(subject,),
    )


def _validate_reviewed_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(decoded) is not PartDesignPromotionBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != PARTDESIGN_PROMOTION_MANIFEST.manifest_sha256
        or receipt.adapter != PARTDESIGN_PROMOTION_MANIFEST.adapter
        or _OPERATIONS_BY_ID.get(decoded.operation.value) != operation
        or decoded.lowering_request_sha256 != receipt.request_digest
        or decoded.adapter_contract_sha256 != receipt.adapter.adapter_contract_sha256
        or decoded.source_artifact_id != receipt.source_document.artifact_id
        or decoded.source_graph_id != receipt.source_document.document_id
        or decoded.source_graph_sha256 != receipt.source_document.document_digest
        or decoded.source_content_sha256 != receipt.source_document.content_sha256
        or decoded.plan_sha256 != receipt.plan_document.document_digest
    ):
        _integrity_failure()
    PARTDESIGN_PROMOTION_SOURCE_CONTRACTS[decoded.operation].selections(decoded)


def partdesign_promotion_reviewed_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        PARTDESIGN_PROMOTION_MANIFEST,
        sink,
        build_plan=_reviewed_plan_draft,
        decode_plan=decode_partdesign_promotion_backend_plan,
        validate_binding=_validate_reviewed_binding,
    )


def resolve_partdesign_promotion_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartDesignPromotionBackendPlan:
    if (
        type(plan) is not PartDesignPromotionBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PARTDESIGN_PROMOTION_MANIFEST.operations
        or _OPERATIONS_BY_ID.get(plan.operation.value) != operation
        or plan.adapter_contract_sha256
        != PARTDESIGN_PROMOTION_MANIFEST.adapter.adapter_contract_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    PARTDESIGN_PROMOTION_SOURCE_CONTRACTS[plan.operation].selections(plan)
    try:
        decoded = decode_partdesign_promotion_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except Exception:
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_partdesign_promotion_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    _validate_reviewed_binding(plan, receipt, operation)
    _validate_plan_contract(plan, receipt.plan_document, operation)


def _shape_sha256(item: object) -> str:
    try:
        payload = item.Shape.exportBrepToString().encode("utf-8")
    except Exception:
        _integrity_failure()
    if not payload:
        _integrity_failure()
    return hashlib.sha256(payload).hexdigest()


def _source_receipt_fresh(item: object, receipt: object) -> bool:
    """Require a content-bound live-shape receipt; identity alone is insufficient."""

    try:
        expected = receipt.result_shape_sha256
        return (
            type(expected) is str
            and len(expected) == 64
            and hmac.compare_digest(_shape_sha256(item), expected)
        )
    except Exception:
        return False


def _profile_shape(item: object, *, spine: bool) -> bool:
    try:
        shape = item.Shape
        wires = tuple(shape.Wires)
        open_vertices = len(item.OpenVertices)
        return (
            item.TypeId == "Sketcher::SketchObject"
            and item.isValid()
            and tuple(item.State) == ("Up-to-date",)
            and not shape.isNull()
            and shape.isValid()
            and len(wires) == 1
            and len(shape.Edges) >= 1
            and (open_vertices in {0, 2} if spine else wires[0].isClosed() and open_vertices == 0)
        )
    except Exception:
        return False


def _solid_shape(item: object) -> bool:
    try:
        shape = item.Shape
        return (
            item.isValid()
            and tuple(item.State) == ("Up-to-date",)
            and not shape.isNull()
            and shape.isValid()
            and str(shape.ShapeType) == "Solid"
            and len(shape.Solids) == 1
            and math.isfinite(float(shape.Volume))
            and float(shape.Volume) > 1e-9
        )
    except Exception:
        return False


def _parent_body(item: object) -> object:
    try:
        resolver = item.getParentGeoFeatureGroup
        body = resolver()
        if not callable(resolver) or body is None:
            raise ValueError
    except Exception:
        _integrity_failure()
    return body


def _authenticated_source_bindings(
    document: object,
    plan: PartDesignPromotionBackendPlan,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> tuple[PartDesignPromotionExecutionBindings, tuple[str, ...]]:
    """Convert exact same-run results into the existing native binding contract."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentRoute,
        ReviewedNativeExecutionResult,
        _ReviewedProductResultKind,
    )

    contract = PARTDESIGN_PROMOTION_SOURCE_CONTRACTS[plan.operation]
    selections = contract.selections(plan)
    if (
        session is None
        or type(source_results) is not tuple
        or len(source_results) != len(selections)
        or any(type(item) is not ReviewedNativeExecutionResult for item in source_results)
    ):
        _integrity_failure()
    try:
        read_identity = session.read_object_identity
        document_objects = tuple(document.Objects)
        if session.doc is not document or not callable(read_identity):
            raise ValueError
    except Exception:
        _integrity_failure()
    objects = tuple(item.object for item in source_results)
    if len({id(item) for item in objects}) != len(objects):
        _integrity_failure()
    bodies = tuple(_parent_body(item) for item in objects)
    body = bodies[0]
    if any(item is not body for item in bodies):
        _integrity_failure()

    authenticated: list[tuple[str, AuthenticatedPromotionObject]] = []
    digests = []
    for (role, selection), source in zip(selections, source_results, strict=True):
        item = source.object
        route = source.route
        receipt = source.native_receipt
        try:
            identity = read_identity(item)
        except Exception:
            _integrity_failure()
        expected_kind = (
            _ReviewedProductResultKind.SOLID
            if role == "base"
            else _ReviewedProductResultKind.VALID_SHAPE
        )
        expected_roles = (
            {SemanticRole.PRIMITIVE, SemanticRole.FEATURE}
            if role == "base"
            else {SemanticRole.FEATURE}
        )
        expected_type = None if role == "base" else "Sketcher::SketchObject"
        if (
            type(route) is not ReviewedIntentRoute
            or route.operation not in route.manifest.operations
            or route.operation.native_type_id != getattr(item, "TypeId", None)
            or type(identity) is not EntityIdentity
            or identity.object_type != route.operation.native_type_id
            or identity.feature_id is None
            or identity.semantic_role not in expected_roles
            or identity.provenance.source is not ProvenanceSource.MODEL
            or identity.provenance.operation_id != "apply_reviewed_intent"
            or getattr(item, "Document", None) is not document
            or not any(item is existing for existing in document_objects)
            or source.result_kind is not expected_kind
            or tuple(source.semantic_roles) != (identity.semantic_role,)
            or source.plan_sha256 != getattr(receipt, "plan_sha256", None)
            or getattr(receipt, "object_name", getattr(receipt, "sketch_object_name", None))
            != getattr(item, "Name", None)
            or (expected_type is not None and item.TypeId != expected_type)
            or not _source_receipt_fresh(item, receipt)
            or (role == "base" and not _solid_shape(item))
            or (role == "profile" and not _profile_shape(item, spine=False))
            or (role == "spine" and not _profile_shape(item, spine=True))
        ):
            _integrity_failure()
        digests.append(_shape_sha256(item))
        authenticated.append(
            (
                role,
                AuthenticatedPromotionObject(
                    object=item,
                    node_id=selection.node_id,
                    result_id=selection.result_id,
                ),
            )
        )

    base = next((item for role, item in authenticated if role == "base"), None)
    profiles = tuple(item for role, item in authenticated if role == "profile")
    spine = next((item for role, item in authenticated if role == "spine"), None)
    expected_group = tuple(item.object for _, item in authenticated)
    try:
        if (
            body.Document is not document
            or body.TypeId != "PartDesign::Body"
            or tuple(body.Group) != expected_group
            or body.Tip is not (None if base is None else base.object)
        ):
            raise ValueError
    except Exception:
        _integrity_failure()
    return (
        PartDesignPromotionExecutionBindings(
            document=document,
            body=body,
            body_id=plan.body_id,
            base=base,
            profiles=profiles,
            spine=spine,
        ),
        tuple(digests),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPromotionResultInvariant:
    operation: PartDesignPromotionOperation
    native_type_id: str
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        if (
            type(self.operation) is not PartDesignPromotionOperation
            or self.native_type_id != _native_spec(self.operation).type_id
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        body: object,
        result: object,
        receipt: PartDesignPromotionConformanceReceipt,
        result_shape_sha256: str,
    ) -> None:
        try:
            shape = result.Shape
            before = float(receipt.before_volume_mm3)
            after = float(receipt.after_volume_mm3)
            native = _native_spec(self.operation)
            valid = (
                type(receipt) is PartDesignPromotionConformanceReceipt
                and receipt.operation is self.operation
                and result.Document is document
                and document.getObject(receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.native_type_id
                and _parent_body(result) is body
                and body.Tip is result
                and result.isValid()
                and tuple(result.State) == ("Up-to-date",)
                and not shape.isNull()
                and shape.isValid()
                and str(shape.ShapeType) == "Solid"
                and len(shape.Solids) == 1
                and math.isfinite(float(shape.Volume))
                and float(shape.Volume) > 1e-9
                and math.isclose(float(shape.Volume), after, rel_tol=0.0, abs_tol=1e-9)
                and (
                    native.additive
                    and (before == 0.0 or after > before)
                    or not native.additive
                    and 0.0 < after < before
                )
            )
            # Keep the digest mandatory for both additive and subtractive paths;
            # it is separated from the expression above to avoid precedence drift.
            valid = valid and hmac.compare_digest(_shape_sha256(result), result_shape_sha256)
        except Exception:
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adopted_observation(self, observation: object) -> None:
        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != self.native_type_id
            or observation.semantic_role != self.semantic_role.value
            or observation.valid_shape is not True
            or observation.solid_count != 1
            or observation.volume_mm3 is None
            or observation.volume_mm3 <= 1e-9
        ):
            _integrity_failure()


PARTDESIGN_PROMOTION_RESULT_INVARIANTS: Final = MappingProxyType(
    {
        operation: PartDesignPromotionResultInvariant(
            operation=operation,
            native_type_id=_native_spec(operation).type_id,
        )
        for operation in PartDesignPromotionOperation
    }
)


def _result_properties_match(
    plan: PartDesignPromotionBackendPlan,
    bindings: PartDesignPromotionExecutionBindings,
    result: object,
) -> bool:
    """Recheck the static native property subset at the product boundary."""

    try:
        if (
            result.BaseFeature is not (None if bindings.base is None else bindings.base.object)
            or result.Profile[0] is not bindings.profiles[0].object
            or tuple(result.Profile[1]) != ()
            or bool(result.Midplane)
            or bool(result.Reversed)
            or not bool(result.Refine)
            or bool(result.AllowMultiFace)
        ):
            return False
        family = _native_spec(plan.operation).family
        if family == "loft":
            return (
                tuple(item[0] for item in result.Sections)
                == tuple(item.object for item in bindings.profiles[1:])
                and tuple(tuple(item[1]) for item in result.Sections)
                == (("",),) * (len(bindings.profiles) - 1)
                and not bool(result.Closed)
                and not bool(result.Ruled)
            )
        if family == "pipe":
            if bindings.spine is None:
                return False
            expected_edges = tuple(
                f"Edge{index}" for index in range(1, len(bindings.spine.object.Shape.Edges) + 1)
            )
            return (
                result.Spine[0] is bindings.spine.object
                and tuple(result.Spine[1]) == expected_edges
                and str(result.Mode) == "Standard"
                and str(result.Transformation) == "Constant"
                and str(result.Transition) == "Transformed"
                and not tuple(result.Sections)
                and result.AuxiliarySpine is None
                and not bool(result.AuxiliaryCurvilinear)
                and not bool(result.SpineTangent)
                and not bool(result.AuxiliarySpineTangent)
            )
        return (
            result.ReferenceAxis[0] is bindings.profiles[0].object
            and tuple(result.ReferenceAxis[1]) == ("V_Axis",)
            and str(result.Mode) == "pitch-height-angle"
            and math.isclose(float(result.Pitch), plan.pitch_mm, rel_tol=0.0, abs_tol=1e-9)
            and math.isclose(float(result.Height), plan.height_mm, rel_tol=0.0, abs_tol=1e-9)
            and math.isclose(float(result.Angle), 0.0, rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(float(result.Growth), 0.0, rel_tol=0.0, abs_tol=1e-12)
            and math.isclose(float(result.Turns), plan.turns, rel_tol=0.0, abs_tol=1e-9)
            and not bool(result.LeftHanded)
            and not bool(result.Outside)
        )
    except Exception:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPromotionOwnershipClosure:
    invariant: PartDesignPromotionResultInvariant
    native_receipt: PartDesignPromotionConformanceReceipt
    plan: PartDesignPromotionBackendPlan = field(repr=False)
    bindings: PartDesignPromotionExecutionBindings = field(repr=False, compare=False)
    source_shape_sha256s: tuple[str, ...]
    result_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.invariant) is not PartDesignPromotionResultInvariant
            or type(self.native_receipt) is not PartDesignPromotionConformanceReceipt
            or self.native_receipt.operation is not self.invariant.operation
            or type(self.plan) is not PartDesignPromotionBackendPlan
            or self.plan.operation is not self.invariant.operation
            or self.plan.plan_sha256 != self.native_receipt.plan_sha256
            or type(self.bindings) is not PartDesignPromotionExecutionBindings
            or type(self.source_shape_sha256s) is not tuple
            or not self.source_shape_sha256s
            or any(type(item) is not str or len(item) != 64 for item in self.source_shape_sha256s)
            or type(self.result_shape_sha256) is not str
            or len(self.result_shape_sha256) != 64
        ):
            _integrity_failure()
        body = "\0".join(
            (
                self.native_receipt.receipt_sha256,
                self.invariant.operation.value,
                self.result_shape_sha256,
                *self.source_shape_sha256s,
            )
        ).encode("ascii")
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_OWNERSHIP_DIGEST_DOMAIN + body).hexdigest(),
        )

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def operation(self) -> PartDesignPromotionOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    def validate_native_result(self, document: object, result: object) -> None:
        self.invariant.validate_native_result(
            document,
            self.bindings.body,
            result,
            self.native_receipt,
            self.result_shape_sha256,
        )
        source_objects = tuple(
            item
            for item in (
                None if self.bindings.base is None else self.bindings.base.object,
                *(profile.object for profile in self.bindings.profiles),
                None if self.bindings.spine is None else self.bindings.spine.object,
            )
            if item is not None
        )
        try:
            source_digests = tuple(_shape_sha256(item) for item in source_objects)
            ownership_valid = (
                tuple(self.bindings.body.Group) == (*source_objects, result)
                and all(_parent_body(item) is self.bindings.body for item in source_objects)
                and source_digests == self.source_shape_sha256s
            )
        except Exception:
            ownership_valid = False
        if not ownership_valid or not _result_properties_match(self.plan, self.bindings, result):
            _integrity_failure()

    def validate_adoption(self, document: object, result: object, observation: object) -> None:
        self.validate_native_result(document, result)
        self.invariant.validate_adopted_observation(observation)


def execute_partdesign_promotion_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        document is None
        or type(payload) is not bytes
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
    ):
        _integrity_failure()
    return execute_partdesign_promotion_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


def execute_partdesign_promotion_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> object:
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_partdesign_promotion_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except Exception:
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    bindings, source_shape_sha256s = _authenticated_source_bindings(
        document,
        checked,
        source_results,
        session=session,
    )
    before = tuple(document.Objects)
    before_group = tuple(bindings.body.Group)
    receipt = apply_partdesign_promotion_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=bindings,
    )
    try:
        result = document.getObject(receipt.object_name)
        after = tuple(document.Objects)
        after_group = tuple(bindings.body.Group)
        current_source_digests = tuple(_shape_sha256(item.object) for item in source_results)
        result_digest = _shape_sha256(result)
    except Exception:
        _integrity_failure()
    added = tuple(item for item in after if not any(item is old for old in before))
    if (
        type(receipt) is not PartDesignPromotionConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.plan_sha256 != checked.plan_sha256
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or after_group != (*before_group, result)
        or current_source_digests != source_shape_sha256s
        or result.TypeId != operation.native_type_id
        or not _result_properties_match(checked, bindings, result)
    ):
        _integrity_failure()
    ownership = PartDesignPromotionOwnershipClosure(
        invariant=PARTDESIGN_PROMOTION_RESULT_INVARIANTS[checked.operation],
        native_receipt=receipt,
        plan=checked,
        bindings=bindings,
        source_shape_sha256s=source_shape_sha256s,
        result_shape_sha256=result_digest,
    )
    ownership.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


@dataclass(frozen=True, slots=True)
class PartDesignPromotionReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]


PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC: Final = PartDesignPromotionReviewedFamilySpec(
    manifest=PARTDESIGN_PROMOTION_MANIFEST,
    subject_type_term=_bridge_term(PROMOTION_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PartDesignPromotionOperation),
    adapter_factory=partdesign_promotion_reviewed_adapter_factory,
    validate_plan=validate_partdesign_promotion_reviewed_plan,
    execute_plan=execute_partdesign_promotion_reviewed_plan,
)


__all__ = [
    "PARTDESIGN_PROMOTION_MANIFEST",
    "PARTDESIGN_PROMOTION_OPERATION_SPECS",
    "PARTDESIGN_PROMOTION_RESULT_INVARIANTS",
    "PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC",
    "PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_IDENTITIES",
    "PARTDESIGN_PROMOTION_REVIEWED_PRODUCT_OPERATIONS",
    "PARTDESIGN_PROMOTION_SOURCE_CONTRACTS",
    "PartDesignPromotionOwnershipClosure",
    "PartDesignPromotionResultInvariant",
    "PartDesignPromotionReviewedFamilySpec",
    "PartDesignPromotionSourceContract",
    "execute_partdesign_promotion_reviewed_plan",
    "execute_partdesign_promotion_reviewed_plan_with_sources",
    "partdesign_promotion_reviewed_adapter_factory",
    "resolve_partdesign_promotion_reviewed_operation",
    "validate_partdesign_promotion_reviewed_plan",
]
