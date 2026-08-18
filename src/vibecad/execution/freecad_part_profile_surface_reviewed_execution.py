"""Private product callbacks for reviewed Part profile/surface operations.

The six reviewed operations consume ordered, engine-owned upstream results.
This module authenticates their managed identity, provenance, current shape,
and PFG source selection before entering the existing native rule. It also
owns the exact solid-versus-surface result contract used during adoption.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import (
    EntityIdentity,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_profile_surface_adapter import (
    PART_PROFILE_SURFACE_MANIFEST,
    PART_PROFILE_SURFACE_STRUCTURE_TERM,
    FreeCADPartProfileSurfaceAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_profile_surface_rules import (
    PART_PROFILE_SURFACE_NATIVE_SPECS,
    AuthenticatedPartProfileSurfaceObject,
    PartProfileSurfaceBackendPlan,
    PartProfileSurfaceConformanceReceipt,
    PartProfileSurfaceExecutionBindings,
    PartProfileSurfaceOperation,
    PartProfileSurfaceResultKind,
    PartProfileSurfaceSourceRole,
    ProfileSurfaceSourceRequirement,
    apply_part_profile_surface_plan,
    decode_part_profile_surface_backend_plan,
)
from vibecad.validation import EntityObservation


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _integrity_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS: Final = (
    PartProfileSurfaceOperation.EXTRUSION,
    PartProfileSurfaceOperation.REVOLUTION,
    PartProfileSurfaceOperation.LOFT,
    PartProfileSurfaceOperation.SWEEP,
    PartProfileSurfaceOperation.RULED_SURFACE,
    PartProfileSurfaceOperation.FACE,
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PART_PROFILE_SURFACE_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PART_PROFILE_SURFACE_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS
    }
)
PART_PROFILE_SURFACE_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)

# These are the exact engine-owned inputs required at the native authority
# seam. Every source is resolved by both PFG node_id and result_id to an
# already-authenticated object in the same document.
PART_PROFILE_SURFACE_REQUIRED_SOURCE_BINDINGS: Final = MappingProxyType(
    {
        operation.value: PART_PROFILE_SURFACE_NATIVE_SPECS[operation].source_requirements
        for operation in PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartProfileSurfaceResultInvariant:
    """Exact family-owned native and adopted-result contract."""

    operation: PartProfileSurfaceOperation
    native_type_id: str
    shape_types: tuple[str, ...]
    solid_count: int
    minimum_edge_count: int
    minimum_face_count: int
    exact_face_count: int | None
    require_positive_area: bool
    require_positive_volume: bool
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        expected = PART_PROFILE_SURFACE_NATIVE_SPECS.get(self.operation)
        if (
            expected is None
            or self.native_type_id != expected.type_id
            or self.semantic_role is not SemanticRole.FEATURE
            or self.shape_types not in {("Solid",), ("Face",)}
            or self.solid_count not in {0, 1}
            or self.minimum_edge_count != 1
            or self.minimum_face_count != 1
            or self.exact_face_count not in {None, 1}
            or type(self.require_positive_area) is not bool
            or type(self.require_positive_volume) is not bool
            or (
                expected.result_kind is PartProfileSurfaceResultKind.SOLID
                and (
                    self.shape_types != ("Solid",)
                    or self.solid_count != 1
                    or self.exact_face_count is not None
                    or self.require_positive_area
                    or not self.require_positive_volume
                )
            )
            or (
                expected.result_kind is PartProfileSurfaceResultKind.SURFACE
                and (
                    self.shape_types != ("Face",)
                    or self.solid_count != 0
                    or self.exact_face_count != 1
                    or not self.require_positive_area
                    or self.require_positive_volume
                )
            )
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        result: object,
        receipt: PartProfileSurfaceConformanceReceipt,
    ) -> None:
        """Recheck ownership, exact shape class, topology, and rule effect."""

        try:
            shape = result.Shape
            shape_type = str(shape.ShapeType)
            edge_count = len(shape.Edges)
            face_count = len(shape.Faces)
            solid_count = len(shape.Solids)
            length = float(shape.Length)
            area = float(shape.Area)
            volume = float(shape.Volume)
            shape_sha256 = hashlib.sha256(shape.exportBrepToString().encode("utf-8")).hexdigest()
            valid = (
                result.Document is document
                and document.getObject(receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.native_type_id
                and result.isValid()
                and tuple(result.State) == ("Up-to-date",)
                and not shape.isNull()
                and shape.isValid()
                and shape_type in self.shape_types
                and solid_count == self.solid_count
                and edge_count >= self.minimum_edge_count
                and face_count >= self.minimum_face_count
                and (self.exact_face_count is None or face_count == self.exact_face_count)
                and length > 1e-9
                and (not self.require_positive_area or area > 1e-9)
                and (not self.require_positive_volume or volume > 1e-9)
                and type(receipt) is PartProfileSurfaceConformanceReceipt
                and receipt.operation is self.operation
                and hmac.compare_digest(receipt.result_shape_sha256, shape_sha256)
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adopted_observation(self, observation: object) -> None:
        """Validate the generic observation without weakening it for surfaces."""

        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != self.native_type_id
            or observation.semantic_role != self.semantic_role.value
            or observation.valid_shape is not True
            or observation.solid_count != self.solid_count
            or (
                self.require_positive_volume
                and (observation.volume_mm3 is None or observation.volume_mm3 <= 1e-9)
            )
            or (
                self.require_positive_area
                and (observation.area_mm2 is None or observation.area_mm2 <= 1e-9)
            )
        ):
            _integrity_failure()


def _result_invariant(
    operation: PartProfileSurfaceOperation,
    *,
    shape_type: str,
    solid_count: int,
    exact_face_count: int | None,
    positive_area: bool,
    positive_volume: bool,
) -> PartProfileSurfaceResultInvariant:
    return PartProfileSurfaceResultInvariant(
        operation=operation,
        native_type_id=PART_PROFILE_SURFACE_NATIVE_SPECS[operation].type_id,
        shape_types=(shape_type,),
        solid_count=solid_count,
        minimum_edge_count=1,
        minimum_face_count=1,
        exact_face_count=exact_face_count,
        require_positive_area=positive_area,
        require_positive_volume=positive_volume,
    )


PART_PROFILE_SURFACE_RESULT_INVARIANTS: Final = MappingProxyType(
    {
        operation: _result_invariant(
            operation,
            shape_type="Solid",
            solid_count=1,
            exact_face_count=None,
            positive_area=False,
            positive_volume=True,
        )
        for operation in (
            PartProfileSurfaceOperation.EXTRUSION,
            PartProfileSurfaceOperation.REVOLUTION,
            PartProfileSurfaceOperation.LOFT,
            PartProfileSurfaceOperation.SWEEP,
        )
    }
    | {
        operation: _result_invariant(
            operation,
            shape_type="Face",
            solid_count=0,
            exact_face_count=1,
            positive_area=True,
            positive_volume=False,
        )
        for operation in (
            PartProfileSurfaceOperation.RULED_SURFACE,
            PartProfileSurfaceOperation.FACE,
        )
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartProfileSurfaceOwnershipClosure:
    """Content-bound result contract consumed during identity adoption."""

    invariant: PartProfileSurfaceResultInvariant
    native_receipt: PartProfileSurfaceConformanceReceipt

    def __post_init__(self) -> None:
        if (
            type(self.invariant) is not PartProfileSurfaceResultInvariant
            or type(self.native_receipt) is not PartProfileSurfaceConformanceReceipt
            or self.native_receipt.operation is not self.invariant.operation
        ):
            _integrity_failure()

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def native_type_id(self) -> str:
        return self.invariant.native_type_id

    @property
    def semantic_role(self) -> SemanticRole:
        return self.invariant.semantic_role

    @property
    def operation(self) -> PartProfileSurfaceOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def receipt_sha256(self) -> str:
        return self.native_receipt.receipt_sha256

    @property
    def source_shape_sha256s(self) -> tuple[str, ...]:
        return self.native_receipt.source_shape_sha256s

    @property
    def result_shape_sha256(self) -> str:
        return self.native_receipt.result_shape_sha256

    def validate_native_result(self, document: object, result: object) -> None:
        self.invariant.validate_native_result(document, result, self.native_receipt)

    def validate_adopted_observation(self, observation: object) -> None:
        self.invariant.validate_adopted_observation(observation)

    def validate_adoption(
        self,
        document: object,
        result: object,
        observation: object,
    ) -> None:
        """Revalidate the native object and its adopted generic observation."""

        self.validate_native_result(document, result)
        self.validate_adopted_observation(observation)


def resolve_part_profile_surface_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Return one exact reviewed operation, or remain inert on a mismatch."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def part_profile_surface_reviewed_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    return FreeCADPartProfileSurfaceAdapter(sink)


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartProfileSurfaceBackendPlan:
    if (
        type(plan) is not PartProfileSurfaceBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_PROFILE_SURFACE_MANIFEST.operations
        or plan.operation not in PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
        or plan.adapter_contract_sha256
        != PART_PROFILE_SURFACE_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_PROFILE_SURFACE_MANIFEST.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    expected_requirements = PART_PROFILE_SURFACE_REQUIRED_SOURCE_BINDINGS[plan.operation.value]
    if not expected_requirements or any(
        type(item) is not ProfileSurfaceSourceRequirement or item.minimum < 1
        for item in expected_requirements
    ):
        _integrity_failure()
    try:
        decoded = decode_part_profile_surface_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_part_profile_surface_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind one canonical dependent plan to its exact reviewed route."""

    if (
        type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_PROFILE_SURFACE_MANIFEST.manifest_sha256
        or receipt.adapter != PART_PROFILE_SURFACE_MANIFEST.adapter
    ):
        _integrity_failure()
    checked = _validate_plan_contract(plan, receipt.plan_document, operation)
    if (
        checked.lowering_request_sha256 != receipt.request_digest
        or checked.source_artifact_id != receipt.source_document.artifact_id
        or checked.source_graph_id != receipt.source_document.document_id
        or checked.source_graph_sha256 != receipt.source_document.document_digest
        or checked.source_content_sha256 != receipt.source_document.content_sha256
    ):
        _integrity_failure()


def execute_part_profile_surface_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Execute through the shared family context and its opaque source results."""

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
    return execute_part_profile_surface_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


def _authenticated_source_bindings(
    document: object,
    plan: PartProfileSurfaceBackendPlan,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> tuple[AuthenticatedPartProfileSurfaceObject, ...]:
    """Adapt exact engine-owned resolver results to the existing native rule."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        REVIEWED_PART_PROFILE_SURFACE_ROUTES,
        ReviewedNativeExecutionResult,
    )

    if (
        session is None
        or type(source_results) is not tuple
        or len(source_results) != len(plan.sources)
        or not source_results
        or any(type(item) is not ReviewedNativeExecutionResult for item in source_results)
    ):
        _integrity_failure()
    try:
        read_identity = session.read_object_identity
        if session.doc is not document or not callable(read_identity):
            raise ValueError
        document_objects = tuple(document.Objects)
    except (AttributeError, TypeError, ValueError):
        _integrity_failure()
    objects = tuple(item.object for item in source_results)
    if len({id(item) for item in objects}) != len(objects):
        _integrity_failure()
    authenticated = []
    for selection, source in zip(plan.sources, source_results, strict=True):
        item = source.object
        try:
            identity = read_identity(item)
        except (AttributeError, KeyError, TypeError, ValueError):
            _integrity_failure()
        profile_source_route = any(
            source.route is route for route in REVIEWED_PART_PROFILE_SURFACE_ROUTES
        )
        expected_role = (
            SemanticRole.FEATURE
            if profile_source_route
            or source.route.operation.native_type_id in {"Part::Cut", "Part::Fuse", "Part::Common"}
            else SemanticRole.PRIMITIVE
        )
        if (
            type(identity) is not EntityIdentity
            or getattr(item, "Document", None) is not document
            or not any(item is existing for existing in document_objects)
            or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
            or getattr(item, "TypeId", None) != source.route.operation.native_type_id
            or identity.object_type != source.route.operation.native_type_id
            or identity.feature_id is None
            or identity.semantic_role is not expected_role
            or identity.provenance.source is not ProvenanceSource.MODEL
            or identity.provenance.operation_id is None
            or getattr(source.native_receipt, "plan_sha256", None) != source.plan_sha256
            or not _source_shape_satisfies_role(item, selection.role)
            or not _source_shape_matches_receipt(item, source.native_receipt)
        ):
            _integrity_failure()
        authenticated.append(
            AuthenticatedPartProfileSurfaceObject(
                object=item,
                node_id=selection.node_id,
                result_id=selection.result_id,
            )
        )
    return tuple(authenticated)


def _source_shape_satisfies_role(
    item: object,
    role: PartProfileSurfaceSourceRole,
) -> bool:
    """Apply the authority-free structural subset of native source checks."""

    try:
        shape = item.Shape
        edges = len(shape.Edges)
        faces = len(shape.Faces)
        solids = len(shape.Solids)
        length = float(shape.Length)
        if role in {
            PartProfileSurfaceSourceRole.PROFILE,
            PartProfileSurfaceSourceRole.BOUNDARY,
        }:
            wires = tuple(shape.Wires)
            return (
                str(shape.ShapeType) == "Wire"
                and 1 <= edges <= 256
                and faces == 0
                and solids == 0
                and len(wires) == 1
                and wires[0].isClosed()
            )
        return (
            role
            in {
                PartProfileSurfaceSourceRole.SPINE,
                PartProfileSurfaceSourceRole.CURVE,
            }
            and edges == 1
            and faces == 0
            and solids == 0
            and length > 1e-9
        )
    except (AttributeError, TypeError, ValueError, OverflowError, SystemExit):
        return False


def _source_shape_matches_receipt(item: object, receipt: object) -> bool:
    """Reject a same-identity source whose reviewed shape receipt is stale."""

    try:
        shape = item.Shape
        expected_digest = getattr(receipt, "result_shape_sha256", None)
        if type(expected_digest) is str and len(expected_digest) == 64:
            actual_digest = hashlib.sha256(shape.exportBrepToString().encode("utf-8")).hexdigest()
            return hmac.compare_digest(actual_digest, expected_digest)
        expected = receipt.shape
        return (
            shape.ShapeType == expected.shape_type
            and len(shape.Vertexes) == expected.vertex_count
            and len(shape.Edges) == expected.edge_count
            and len(shape.Faces) == expected.face_count
            and math.isclose(
                float(shape.Length),
                expected.length_mm,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                float(shape.Area),
                expected.area_mm2,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        )
    except (AttributeError, TypeError, ValueError, UnicodeError, OverflowError):
        return False


def execute_part_profile_surface_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> object:
    """Internal integration hook for engine-owned, same-run source results.

    ``source_results`` must come from the executor's private run-state resolver;
    this callback does not accept public object identifiers or inspect the
    document to discover candidates.
    """

    if document is None or type(payload) is not bytes:
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_part_profile_surface_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    sources = _authenticated_source_bindings(
        document,
        checked,
        source_results,
        session=session,
    )
    before = tuple(document.Objects)
    receipt = apply_part_profile_surface_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=PartProfileSurfaceExecutionBindings(
            document=document,
            body_id=checked.body_id,
            sources=sources,
            expected_adapter_contract_sha256=(
                PART_PROFILE_SURFACE_MANIFEST.adapter.adapter_contract_sha256
            ),
            expected_manifest_sha256=PART_PROFILE_SURFACE_MANIFEST.manifest_sha256,
            expected_operation_specification_sha256=operation.specification_sha256,
        ),
    )
    try:
        result = document.getObject(receipt.object_name)
        after = tuple(document.Objects)
        current_source_shape_sha256s = tuple(
            hashlib.sha256(item.object.Shape.exportBrepToString().encode("utf-8")).hexdigest()
            for item in source_results
        )
    except (Exception, SystemExit):
        _integrity_failure()
    added = tuple(item for item in after if not any(item is existing for existing in before))
    if (
        type(receipt) is not PartProfileSurfaceConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.plan_sha256 != checked.plan_sha256
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
        or current_source_shape_sha256s != receipt.source_shape_sha256s
    ):
        _integrity_failure()
    ownership = PartProfileSurfaceOwnershipClosure(
        invariant=PART_PROFILE_SURFACE_RESULT_INVARIANTS[checked.operation],
        native_receipt=receipt,
    )
    ownership.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


@dataclass(frozen=True, slots=True)
class PartProfileSurfaceReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]


PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC: Final = PartProfileSurfaceReviewedFamilySpec(
    manifest=PART_PROFILE_SURFACE_MANIFEST,
    subject_type_term=_bridge_term(PART_PROFILE_SURFACE_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_profile_surface_reviewed_adapter_factory,
    validate_plan=validate_part_profile_surface_reviewed_plan,
    execute_plan=execute_part_profile_surface_reviewed_plan,
)


__all__ = [
    "PART_PROFILE_SURFACE_REQUIRED_SOURCE_BINDINGS",
    "PART_PROFILE_SURFACE_RESULT_INVARIANTS",
    "PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC",
    "PART_PROFILE_SURFACE_REVIEWED_PRODUCT_IDENTITIES",
    "PART_PROFILE_SURFACE_REVIEWED_PRODUCT_OPERATIONS",
    "PartProfileSurfaceReviewedFamilySpec",
    "PartProfileSurfaceOwnershipClosure",
    "PartProfileSurfaceResultInvariant",
    "execute_part_profile_surface_reviewed_plan",
    "execute_part_profile_surface_reviewed_plan_with_sources",
    "part_profile_surface_reviewed_adapter_factory",
    "resolve_part_profile_surface_reviewed_operation",
    "validate_part_profile_surface_reviewed_plan",
]
