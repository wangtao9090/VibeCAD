"""Private product callbacks for the reviewed PartDesign Groove operation.

The pre-registry Groove adapter owns the exact PFG identity and the native
Groove rule owns every FreeCAD property spelling.  This module supplies only
the reviewed-family compatibility manifest and the ordered, engine-owned
source boundary required by the product dispatcher.  Sources are exactly
``base solid -> closed Sketch profile``.  The revolution axis is the reviewed
``V_Axis`` locator embedded in the plan and is never accepted from model input
as a native string or topology selector.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge import freecad_parametric_adapter as groove_adapter
from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
)
from vibecad.intent_bridge.freecad_parametric_adapter import (
    FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
    GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    GROOVE_CAPABILITY_SCHEMA_TERM,
    GROOVE_INTENT_DOCUMENT_ROLE_TERM,
    GROOVE_OPERATION_TERM,
    GROOVE_PLAN_DOCUMENT_ROLE_TERM,
    GROOVE_PLAN_SCHEMA_TERM,
    GROOVE_REQUEST_TERMS,
    GROOVE_STRUCTURE_TERM,
    PlanSink,
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
from vibecad.parametric import freecad_sketch_intent_rules as sketch_rules
from vibecad.parametric.feature_graph_v2 import (
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_sketch_rules import (
    GROOVE_FREECAD_ENGINE_BUILD_ID,
    GROOVE_PLAN_MEDIA_TYPE,
    GROOVE_RULE_CONTRACT_SHA256,
    GROOVE_RULE_ID,
    MAX_GROOVE_PLAN_BYTES,
    GrooveBackendPlan,
    GrooveConformanceReceipt,
    GrooveExecutionBindings,
    apply_groove_plan,
    decode_groove_backend_plan,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.partdesign-groove-ownership.v1\0"
_PROFILE_EXECUTION_DIGEST_DOMAIN = b"vibecad.partdesign-groove-profile-execution.v1\0"
_FREECAD_BUILD_DESCRIPTOR_SHA256 = hashlib.sha256(
    GROOVE_FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()
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
    raise IntentBridgeError(IntentBridgeErrorCode.AUTHORITY_VIOLATION, path)


PARTDESIGN_GROOVE_OPERATION_SPEC: Final = ReviewedOperationSpec(
    # The shared LEGACY_TERM_ID seam later binds this local id to the current
    # formal id ``partdesign.groove.angle``.  This compatibility manifest does
    # not claim a new verification receipt.
    operation_id="angle",
    semantic_term=_bridge_term(GROOVE_OPERATION_TERM),
    native_type_id="PartDesign::Groove",
    native_operation="groove_angle",
    native_property_names=_GROOVE_PROPERTIES,
)

PARTDESIGN_GROOVE_MANIFEST: Final = FamilyBatchManifest(
    family_id="partdesign.groove",
    family_version="1.0.0",
    adapter=FREECAD_GROOVE_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_FREECAD_BUILD_DESCRIPTOR_SHA256,
    rule_id=GROOVE_RULE_ID,
    rule_contract_sha256=GROOVE_RULE_CONTRACT_SHA256,
    intent_role_term=GROOVE_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=GROOVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=GROOVE_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-groove-capability+json",
    plan_role_term=GROOVE_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=GROOVE_PLAN_SCHEMA_TERM,
    plan_media_type=GROOVE_PLAN_MEDIA_TYPE,
    request_terms=GROOVE_REQUEST_TERMS,
    operations=(PARTDESIGN_GROOVE_OPERATION_SPEC,),
    max_plan_bytes=MAX_GROOVE_PLAN_BYTES,
)

PARTDESIGN_GROOVE_REVIEWED_PRODUCT_IDENTITIES: Final = (
    (
        "partdesign.groove.angle",
        _semantic_operation(PARTDESIGN_GROOVE_OPERATION_SPEC),
    ),
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {PARTDESIGN_GROOVE_REVIEWED_PRODUCT_IDENTITIES[0]: (PARTDESIGN_GROOVE_OPERATION_SPEC)}
)


@dataclass(frozen=True, slots=True)
class PartDesignGrooveSourceContract:
    """Exact ordered source contract: base solid, then closed Sketch profile."""

    minimum: int = 2
    maximum: int = 2
    roles: tuple[str, str] = ("base", "profile")

    def __post_init__(self) -> None:
        if self.minimum != 2 or self.maximum != 2 or self.roles != ("base", "profile"):
            _integrity_failure()

    def selections(self, plan: GrooveBackendPlan) -> tuple[tuple[str, str, str], ...]:
        if type(plan) is not GrooveBackendPlan:
            _integrity_failure()
        items = (
            ("base", plan.base_node_id, plan.base_result_id),
            ("profile", plan.profile_node_id, plan.profile_result_id),
        )
        if (
            len({node_id for _, node_id, _ in items}) != 2
            or len({result_id for _, _, result_id in items}) != 2
        ):
            _integrity_failure()
        return items


PARTDESIGN_GROOVE_SOURCE_CONTRACT: Final = PartDesignGrooveSourceContract()


def _reviewed_plan_draft(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    if manifest is not PARTDESIGN_GROOVE_MANIFEST:
        _bridge_failure("/manifest")
    try:
        graph = decode_parametric_feature_graph_v2(
            payload,
            expected_sha256=document.document_digest,
        )
        plan, subject = groove_adapter._build_plan(  # noqa: SLF001
            document,
            payload,
            graph,
            request_digest,
        )
        PARTDESIGN_GROOVE_SOURCE_CONTRACT.selections(plan)
    except IntentBridgeError:
        raise
    except Exception as error:
        if getattr(error, "code", None) is not None:
            raise
        _bridge_failure("/intent_document")
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=PARTDESIGN_GROOVE_OPERATION_SPEC.semantic_term,
        subjects=(subject,),
    )


def _validate_reviewed_binding(
    decoded: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(decoded) is not GrooveBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or operation is not PARTDESIGN_GROOVE_OPERATION_SPEC
        or receipt.operation != operation
        or receipt.manifest_sha256 != PARTDESIGN_GROOVE_MANIFEST.manifest_sha256
        or receipt.adapter != PARTDESIGN_GROOVE_MANIFEST.adapter
        or decoded.lowering_request_sha256 != receipt.request_digest
        or decoded.adapter_contract_sha256 != receipt.adapter.adapter_contract_sha256
        or decoded.source_artifact_id != receipt.source_document.artifact_id
        or decoded.source_graph_id != receipt.source_document.document_id
        or decoded.source_graph_sha256 != receipt.source_document.document_digest
        or decoded.source_content_sha256 != receipt.source_document.content_sha256
        or decoded.plan_sha256 != receipt.plan_document.document_digest
    ):
        _integrity_failure()
    PARTDESIGN_GROOVE_SOURCE_CONTRACT.selections(decoded)


def partdesign_groove_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        PARTDESIGN_GROOVE_MANIFEST,
        sink,
        build_plan=_reviewed_plan_draft,
        decode_plan=decode_groove_backend_plan,
        validate_binding=_validate_reviewed_binding,
    )


def resolve_partdesign_groove_reviewed_operation(
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
) -> GrooveBackendPlan:
    if (
        type(plan) is not GrooveBackendPlan
        or type(plan_document) is not DocumentRef
        or operation is not PARTDESIGN_GROOVE_OPERATION_SPEC
        or plan.adapter_contract_sha256
        != PARTDESIGN_GROOVE_MANIFEST.adapter.adapter_contract_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
        or not 0.0 < plan.angle_degrees <= 360.0
        or type(plan.reversed) is not bool
    ):
        _integrity_failure()
    PARTDESIGN_GROOVE_SOURCE_CONTRACT.selections(plan)
    try:
        decoded = decode_groove_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except Exception:
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_partdesign_groove_reviewed_plan(
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
    try:
        expected = receipt.result_shape_sha256
        return (
            type(expected) is str
            and len(expected) == 64
            and hmac.compare_digest(_shape_sha256(item), expected)
        )
    except Exception:
        return False


def _profile_execution_sha256(item: object) -> str:
    """Bind native Sketch content while ignoring FreeCAD's reference-only BREP locations."""

    if type(getattr(item, "GeometryCount", None)) is not int:
        return _shape_sha256(item)
    try:
        native_geometry, native_constraints = sketch_rules._native_state_signature(  # noqa: SLF001
            item
        )
        matrix = item.Placement.toMatrix()
        placement = tuple(
            float(getattr(matrix, name))
            for name in (
                "A11",
                "A12",
                "A13",
                "A14",
                "A21",
                "A22",
                "A23",
                "A24",
                "A31",
                "A32",
                "A33",
                "A34",
                "A41",
                "A42",
                "A43",
                "A44",
            )
        )
        support = tuple((entry[0].Name, tuple(entry[1])) for entry in tuple(item.AttachmentSupport))
        shape = item.Shape
        box = shape.BoundBox
        payload = json.dumps(
            {
                "native_geometry_sha256s": native_geometry,
                "native_constraint_sha256s": native_constraints,
                "placement": placement,
                "map_mode": str(item.MapMode),
                "attachment_support": support,
                "solver": {
                    "dof": item.DoF,
                    "fully_constrained": item.FullyConstrained,
                    "diagnostics": tuple(
                        tuple(getattr(item, name))
                        for name in (
                            "ConflictingConstraints",
                            "RedundantConstraints",
                            "PartiallyRedundantConstraints",
                            "MalformedConstraints",
                        )
                    ),
                },
                "shape": {
                    "type": str(shape.ShapeType),
                    "vertices": len(shape.Vertexes),
                    "edges": len(shape.Edges),
                    "wires": len(shape.Wires),
                    "faces": len(shape.Faces),
                    "solids": len(shape.Solids),
                    "open_vertices": len(item.OpenVertices),
                    "bbox": (
                        float(box.XMin),
                        float(box.XMax),
                        float(box.YMin),
                        float(box.YMax),
                        float(box.ZMin),
                        float(box.ZMax),
                    ),
                },
                "intent_bindings": item.VibeCADReviewedSketchIntent,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except Exception:
        _integrity_failure()
    return hashlib.sha256(_PROFILE_EXECUTION_DIGEST_DOMAIN + payload).hexdigest()


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


def _closed_profile(item: object) -> bool:
    try:
        shape = item.Shape
        wires = tuple(shape.Wires)
        return (
            item.TypeId == "Sketcher::SketchObject"
            and item.isValid()
            and tuple(item.State) == ("Up-to-date",)
            and not shape.isNull()
            and shape.isValid()
            and len(wires) == 1
            and len(shape.Edges) >= 1
            and wires[0].isClosed()
            and len(item.OpenVertices) == 0
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
    plan: GrooveBackendPlan,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> tuple[GrooveExecutionBindings, tuple[str, str]]:
    """Convert exact same-run results into the native Groove binding contract."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedIntentRoute,
        ReviewedNativeExecutionResult,
        _ReviewedProductResultKind,
    )

    selections = PARTDESIGN_GROOVE_SOURCE_CONTRACT.selections(plan)
    if (
        session is None
        or type(source_results) is not tuple
        or len(source_results) != 2
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
    if len({id(item) for item in objects}) != 2:
        _integrity_failure()
    base, profile = objects
    body = _parent_body(base)
    if _parent_body(profile) is not body:
        _integrity_failure()

    digests: list[str] = []
    for (role, _node_id, _result_id), source in zip(selections, source_results, strict=True):
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
        if (
            type(route) is not ReviewedIntentRoute
            or route.operation not in route.manifest.operations
            or not any(route is current for current in CURRENT_REVIEWED_INTENT_ROUTES)
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
            or not source.semantic_roles
            or source.semantic_roles[0] is not identity.semantic_role
            or source.plan_sha256 != getattr(receipt, "plan_sha256", None)
            or getattr(receipt, "object_name", getattr(receipt, "sketch_object_name", None))
            != getattr(item, "Name", None)
            or not _source_receipt_fresh(item, receipt)
            or (role == "base" and not _solid_shape(item))
            or (role == "profile" and not _closed_profile(item))
        ):
            _integrity_failure()
        digests.append(
            _profile_execution_sha256(item) if role == "profile" else _shape_sha256(item)
        )

    try:
        group = tuple(body.Group)
        if (
            body.Document is not document
            or body.TypeId != "PartDesign::Body"
            or not any(body is item for item in document_objects)
            or body.Tip is not base
            or not any(base is item for item in group)
            or not any(profile is item for item in group)
        ):
            raise ValueError
    except Exception:
        _integrity_failure()
    return (
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
        (digests[0], digests[1]),
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignGrooveResultInvariant:
    native_type_id: str = "PartDesign::Groove"
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        if (
            self.native_type_id != "PartDesign::Groove"
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        body: object,
        result: object,
        receipt: GrooveConformanceReceipt,
        result_shape_sha256: str,
    ) -> None:
        try:
            shape = result.Shape
            valid = (
                type(receipt) is GrooveConformanceReceipt
                and result.Document is document
                and document.getObject(receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.native_type_id
                and _parent_body(result) is body
                and body.Tip is result
                and any(result is item for item in tuple(body.Group))
                and result.isValid()
                and tuple(result.State) == ("Up-to-date",)
                and not shape.isNull()
                and shape.isValid()
                and len(shape.Solids) == 1
                and math.isfinite(float(shape.Volume))
                and math.isclose(
                    float(shape.Volume),
                    receipt.after_volume_mm3,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and 0.0 < receipt.after_volume_mm3 < receipt.before_volume_mm3
                and hmac.compare_digest(_shape_sha256(result), result_shape_sha256)
            )
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


PARTDESIGN_GROOVE_RESULT_INVARIANT: Final = PartDesignGrooveResultInvariant()


def _result_properties_match(
    plan: GrooveBackendPlan,
    bindings: GrooveExecutionBindings,
    result: object,
) -> bool:
    try:
        return (
            result.BaseFeature is bindings.base_feature
            and result.Profile[0] is bindings.profile
            and tuple(result.Profile[1]) == ()
            and result.ReferenceAxis[0] is bindings.profile
            and tuple(result.ReferenceAxis[1]) == ("V_Axis",)
            and str(result.Type) == "Angle"
            and math.isclose(float(result.Angle), plan.angle_degrees, rel_tol=0.0, abs_tol=1e-9)
            and math.isclose(float(result.Angle2), 0.0, rel_tol=0.0, abs_tol=1e-12)
            and not bool(result.Midplane)
            and bool(result.Reversed) is plan.reversed
            and bool(result.Refine)
            and not bool(result.AllowMultiFace)
        )
    except Exception:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignGrooveOwnershipClosure:
    invariant: PartDesignGrooveResultInvariant
    native_receipt: GrooveConformanceReceipt
    plan: GrooveBackendPlan = field(repr=False)
    bindings: GrooveExecutionBindings = field(repr=False, compare=False)
    source_shape_sha256s: tuple[str, str]
    result_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.invariant) is not PartDesignGrooveResultInvariant
            or type(self.native_receipt) is not GrooveConformanceReceipt
            or type(self.plan) is not GrooveBackendPlan
            or self.plan.plan_sha256 != self.native_receipt.plan_sha256
            or self.plan.reversed is not self.native_receipt.reversed
            or type(self.bindings) is not GrooveExecutionBindings
            or type(self.source_shape_sha256s) is not tuple
            or len(self.source_shape_sha256s) != 2
            or any(type(item) is not str or len(item) != 64 for item in self.source_shape_sha256s)
            or type(self.result_shape_sha256) is not str
            or len(self.result_shape_sha256) != 64
        ):
            _integrity_failure()
        body = "\0".join(
            (
                self.native_receipt.receipt_sha256,
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
        try:
            source_digests = (
                _shape_sha256(self.bindings.base_feature),
                _profile_execution_sha256(self.bindings.profile),
            )
            ownership_valid = (
                source_digests == self.source_shape_sha256s
                and _parent_body(self.bindings.base_feature) is self.bindings.body
                and _parent_body(self.bindings.profile) is self.bindings.body
                and any(result is item for item in tuple(self.bindings.body.Group))
            )
        except Exception:
            ownership_valid = False
        if not ownership_valid or not _result_properties_match(self.plan, self.bindings, result):
            _integrity_failure()

    def validate_adoption(self, document: object, result: object, observation: object) -> None:
        self.validate_native_result(document, result)
        self.invariant.validate_adopted_observation(observation)


def execute_partdesign_groove_reviewed_plan(
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
    return execute_partdesign_groove_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


def execute_partdesign_groove_reviewed_plan_with_sources(
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
        decoded = decode_groove_backend_plan(
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
    receipt = apply_groove_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=bindings,
    )
    try:
        result = document.getObject(receipt.object_name)
        after = tuple(document.Objects)
        after_group = tuple(bindings.body.Group)
        current_source_digests = (
            _shape_sha256(bindings.base_feature),
            _profile_execution_sha256(bindings.profile),
        )
        result_digest = _shape_sha256(result)
    except Exception:
        _integrity_failure()
    added = tuple(item for item in after if not any(item is old for old in before))
    if (
        type(receipt) is not GrooveConformanceReceipt
        or receipt.plan_sha256 != checked.plan_sha256
        or receipt.reversed is not checked.reversed
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or len(after_group) != len(before_group) + 1
        or after_group[-1] is not result
        or any(
            current is not original
            for current, original in zip(after_group[:-1], before_group, strict=True)
        )
        or current_source_digests != source_shape_sha256s
        or result.TypeId != operation.native_type_id
        or not _result_properties_match(checked, bindings, result)
    ):
        _integrity_failure()
    ownership = PartDesignGrooveOwnershipClosure(
        invariant=PARTDESIGN_GROOVE_RESULT_INVARIANT,
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
class PartDesignGrooveReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]


PARTDESIGN_GROOVE_REVIEWED_FAMILY_SPEC: Final = PartDesignGrooveReviewedFamilySpec(
    manifest=PARTDESIGN_GROOVE_MANIFEST,
    subject_type_term=_bridge_term(GROOVE_STRUCTURE_TERM),
    operation_ids=("angle",),
    adapter_factory=partdesign_groove_reviewed_adapter_factory,
    validate_plan=validate_partdesign_groove_reviewed_plan,
    execute_plan=execute_partdesign_groove_reviewed_plan,
)


__all__ = [
    "PARTDESIGN_GROOVE_MANIFEST",
    "PARTDESIGN_GROOVE_OPERATION_SPEC",
    "PARTDESIGN_GROOVE_RESULT_INVARIANT",
    "PARTDESIGN_GROOVE_REVIEWED_FAMILY_SPEC",
    "PARTDESIGN_GROOVE_REVIEWED_PRODUCT_IDENTITIES",
    "PARTDESIGN_GROOVE_SOURCE_CONTRACT",
    "PartDesignGrooveOwnershipClosure",
    "PartDesignGrooveResultInvariant",
    "PartDesignGrooveReviewedFamilySpec",
    "PartDesignGrooveSourceContract",
    "execute_partdesign_groove_reviewed_plan",
    "execute_partdesign_groove_reviewed_plan_with_sources",
    "partdesign_groove_reviewed_adapter_factory",
    "resolve_partdesign_groove_reviewed_operation",
    "validate_partdesign_groove_reviewed_plan",
]
