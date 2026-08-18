"""Private product callbacks for reviewed Part offset/projection operations.

Source objects enter only as ordered, engine-owned Reviewed results.  This
module does not resolve public object names or topology labels: the existing
trusted native rule owns source-shape validation and the sole ``Face1`` /
``Edge1`` projection mapping.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import (
    EntityIdentity,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_offset_projection_adapter import (
    PART_OFFSET_MANIFEST,
    PART_OFFSET_STRUCTURE_TERM,
    FreeCADPartOffsetProjectionAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric import freecad_part_offset_projection_rules as offset_rules
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_offset_projection_rules import (
    PART_OFFSET_NATIVE_TYPE_IDS,
    PART_OFFSET_SOURCE_ROLES,
    PartOffsetBackendPlan,
    PartOffsetConformanceReceipt,
    PartOffsetExecutionBindings,
    PartOffsetOperation,
    PartOffsetSourceBinding,
    apply_part_offset_plan,
    decode_part_offset_backend_plan,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.part-offset-projection-ownership.v1\0"


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


def _shape_sha256(item: object) -> str:
    try:
        raw = item.Shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS: Final = tuple(PartOffsetOperation)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PART_OFFSET_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PART_OFFSET_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS
    }
)
PART_OFFSET_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)
PART_OFFSET_REQUIRED_SOURCE_ROLES: Final = MappingProxyType(
    {
        operation.value: PART_OFFSET_SOURCE_ROLES[operation]
        for operation in PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS
    }
)

_EXPECTED_RESULT_CONTRACTS: Final = MappingProxyType(
    {
        PartOffsetOperation.SOLID_OFFSET: ("Solid", 1, False, True),
        PartOffsetOperation.PLANAR_WIRE_OFFSET: ("Wire", 0, True, False),
        PartOffsetOperation.EDGE_ON_FACE_PROJECTION: ("Compound", 0, True, False),
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartOffsetResultInvariant:
    """Exact result shape/effect contract inherited from the native rule."""

    operation: PartOffsetOperation
    native_type_id: str
    shape_type: str
    solid_count: int
    require_positive_length: bool
    require_positive_volume: bool
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        if type(self.operation) is not PartOffsetOperation:
            _integrity_failure()
        expected = _EXPECTED_RESULT_CONTRACTS.get(self.operation)
        if (
            expected is None
            or self.native_type_id != PART_OFFSET_NATIVE_TYPE_IDS.get(self.operation)
            or type(self.shape_type) is not str
            or type(self.solid_count) is not int
            or type(self.require_positive_length) is not bool
            or type(self.require_positive_volume) is not bool
            or (
                self.shape_type,
                self.solid_count,
                self.require_positive_length,
                self.require_positive_volume,
            )
            != expected
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        result: object,
        receipt: PartOffsetConformanceReceipt,
        *,
        result_shape_sha256: str,
    ) -> None:
        """Recheck ownership, native identity, shape kind, and geometry effect."""

        try:
            shape = result.Shape
            current_digest = _shape_sha256(result)
            length = float(shape.Length)
            volume = float(shape.Volume)
            valid = (
                type(receipt) is PartOffsetConformanceReceipt
                and receipt.operation is self.operation
                and receipt.native_type_id == self.native_type_id
                and result.Document is document
                and document.getObject(receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.native_type_id
                and result.isValid()
                and tuple(result.State) == ("Up-to-date",)
                and not shape.isNull()
                and shape.isValid()
                and shape.ShapeType == self.shape_type
                and len(shape.Solids) == self.solid_count
                and (not self.require_positive_length or length > 1e-9)
                and (not self.require_positive_volume or volume > 1e-9)
                and hmac.compare_digest(result_shape_sha256, current_digest)
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adopted_observation(self, observation: object) -> None:
        """Apply the family contract to the generic identity observation."""

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
        ):
            _integrity_failure()


PART_OFFSET_RESULT_INVARIANTS: Final = MappingProxyType(
    {
        operation: PartOffsetResultInvariant(
            operation=operation,
            native_type_id=PART_OFFSET_NATIVE_TYPE_IDS[operation],
            shape_type=contract[0],
            solid_count=contract[1],
            require_positive_length=contract[2],
            require_positive_volume=contract[3],
        )
        for operation, contract in _EXPECTED_RESULT_CONTRACTS.items()
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartOffsetOwnershipClosure:
    """Content-bound result and source closure consumed during adoption."""

    invariant: PartOffsetResultInvariant
    native_receipt: PartOffsetConformanceReceipt
    source_shape_sha256s: tuple[str, ...]
    result_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.invariant) is not PartOffsetResultInvariant
            or type(self.native_receipt) is not PartOffsetConformanceReceipt
        ):
            _integrity_failure()
        if (
            self.native_receipt.operation is not self.invariant.operation
            or type(self.source_shape_sha256s) is not tuple
            or any(not _is_sha256(item) for item in self.source_shape_sha256s)
            or not _is_sha256(self.result_shape_sha256)
        ):
            _integrity_failure()
        expected_sources = PART_OFFSET_SOURCE_ROLES[self.invariant.operation]
        if len(self.source_shape_sha256s) != len(expected_sources):
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
    def operation(self) -> PartOffsetOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def native_type_id(self) -> str:
        return self.invariant.native_type_id

    @property
    def semantic_role(self) -> SemanticRole:
        return self.invariant.semantic_role

    @property
    def source_object_names(self) -> tuple[str, ...]:
        return self.native_receipt.source_object_names

    def validate_native_result(self, document: object, result: object) -> None:
        self.invariant.validate_native_result(
            document,
            result,
            self.native_receipt,
            result_shape_sha256=self.result_shape_sha256,
        )

    def validate_adopted_observation(self, observation: object) -> None:
        self.invariant.validate_adopted_observation(observation)

    def validate_adoption(
        self,
        document: object,
        result: object,
        observation: object,
    ) -> None:
        self.validate_native_result(document, result)
        self.validate_adopted_observation(observation)


def resolve_part_offset_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Return one exact reviewed operation, or remain inert on a mismatch."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def part_offset_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return FreeCADPartOffsetProjectionAdapter(sink)


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartOffsetBackendPlan:
    if (
        type(plan) is not PartOffsetBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_OFFSET_MANIFEST.operations
        or plan.operation not in PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS
        or _OPERATIONS_BY_ID.get(plan.operation.value) != operation
        or plan.adapter_contract_sha256 != PART_OFFSET_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_OFFSET_MANIFEST.manifest_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
        or tuple(item.role for item in plan.sources)
        != PART_OFFSET_REQUIRED_SOURCE_ROLES[plan.operation.value]
    ):
        _integrity_failure()
    try:
        decoded = decode_part_offset_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_part_offset_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind one canonical dependent plan to its exact reviewed route."""

    if (
        type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_OFFSET_MANIFEST.manifest_sha256
        or receipt.adapter != PART_OFFSET_MANIFEST.adapter
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


def _decode_execution_plan(
    plan: object,
    payload: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartOffsetBackendPlan:
    if type(payload) is not bytes:
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_part_offset_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    return checked


def execute_part_offset_reviewed_plan(
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
    return execute_part_offset_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


def _source_shape_matches_receipt(item: object, receipt: object) -> bool:
    """Verify freshness across the native receipt formats used by Reviewed families."""

    try:
        shape = item.Shape
        expected_digest = getattr(receipt, "result_shape_sha256", None)
        if type(expected_digest) is str and len(expected_digest) == 64:
            return hmac.compare_digest(_shape_sha256(item), expected_digest)
        expected = receipt.shape
        return (
            shape.ShapeType == expected.shape_type
            and len(shape.Vertexes) == expected.vertex_count
            and len(shape.Edges) == expected.edge_count
            and len(shape.Faces) == expected.face_count
            and math.isclose(float(shape.Length), expected.length_mm, rel_tol=0.0, abs_tol=1e-9)
            and math.isclose(float(shape.Area), expected.area_mm2, rel_tol=0.0, abs_tol=1e-9)
        )
    except (Exception, SystemExit):
        return False


def _authenticated_source_bindings(
    document: object,
    plan: PartOffsetBackendPlan,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> tuple[tuple[PartOffsetSourceBinding, ...], tuple[str, ...]]:
    """Adapt ordered same-run resolver results to the existing trusted rule."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        REVIEWED_PART_CSG_ROUTES,
        REVIEWED_PART_OFFSET_ROUTES,
        REVIEWED_PART_PROFILE_SURFACE_ROUTES,
        ReviewedNativeExecutionResult,
    )

    if (
        session is None
        or type(source_results) is not tuple
        or not source_results
        or len(source_results) != len(plan.sources)
        or len(source_results) != len(PART_OFFSET_SOURCE_ROLES[plan.operation])
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
    bindings = []
    shape_sha256s = []
    for selection, source in zip(plan.sources, source_results, strict=True):
        item = source.object
        receipt = source.native_receipt
        try:
            identity = read_identity(item)
        except (AttributeError, KeyError, TypeError, ValueError):
            _integrity_failure()
        receipt_operation = getattr(receipt, "operation", None)
        feature_routes = (
            *REVIEWED_PART_CSG_ROUTES,
            *REVIEWED_PART_PROFILE_SURFACE_ROUTES,
            *REVIEWED_PART_OFFSET_ROUTES,
        )
        expected_role = (
            SemanticRole.FEATURE
            if any(source.route is route for route in feature_routes)
            else SemanticRole.PRIMITIVE
        )
        if (
            type(identity) is not EntityIdentity
            or source.route.operation not in source.route.manifest.operations
            or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
            or getattr(receipt_operation, "value", None) != source.route.operation.operation_id
            or getattr(receipt, "plan_sha256", None) != source.plan_sha256
            or getattr(receipt, "object_name", None) != getattr(item, "Name", None)
            or getattr(item, "Document", None) is not document
            or not any(item is existing for existing in document_objects)
            or getattr(item, "TypeId", None) != source.route.operation.native_type_id
            or identity.object_type != source.route.operation.native_type_id
            or identity.feature_id is None
            or identity.semantic_role is not expected_role
            or identity.provenance.source is not ProvenanceSource.MODEL
            or identity.provenance.operation_id != "apply_reviewed_intent"
            or not item.isValid()
            or tuple(item.State) != ("Up-to-date",)
            or not _source_shape_matches_receipt(item, receipt)
        ):
            _integrity_failure()
        try:
            offset_rules._validate_source_shape(selection.role, item)  # noqa: SLF001
        except (Exception, SystemExit):
            _integrity_failure()
        shape_sha256s.append(_shape_sha256(item))
        bindings.append(
            PartOffsetSourceBinding(
                role=selection.role,
                node_id=selection.node_id,
                result_id=selection.result_id,
                native_object=item,
            )
        )
    return tuple(bindings), tuple(shape_sha256s)


def execute_part_offset_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> object:
    """Execute from the resolver's exact ordered, same-run result tuple."""

    if document is None:
        _integrity_failure()
    checked = _decode_execution_plan(plan, payload, plan_document, operation)
    sources, source_shape_sha256s = _authenticated_source_bindings(
        document,
        checked,
        source_results,
        session=session,
    )
    source_objects = tuple(item.native_object for item in sources)
    before = tuple(document.Objects)
    receipt = apply_part_offset_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=PartOffsetExecutionBindings(
            document=document,
            container_id=checked.container_id,
            sources=sources,
        ),
    )
    try:
        result = document.getObject(receipt.object_name)
        after = tuple(document.Objects)
        current_source_shape_sha256s = tuple(_shape_sha256(item) for item in source_objects)
        current_result_shape_sha256 = _shape_sha256(result)
    except (Exception, SystemExit):
        _integrity_failure()
    added = tuple(item for item in after if not any(item is existing for existing in before))
    if (
        type(receipt) is not PartOffsetConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.plan_sha256 != checked.plan_sha256
        or receipt.native_type_id != operation.native_type_id
        or receipt.source_object_names != tuple(item.Name for item in source_objects)
        or current_source_shape_sha256s != source_shape_sha256s
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
    ):
        _integrity_failure()
    try:
        offset_rules._validate_root_ownership(result)  # noqa: SLF001
        offset_rules._validate_feature(  # noqa: SLF001
            result,
            checked.operation,
            checked.configuration,
            source_objects,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    ownership = PartOffsetOwnershipClosure(
        invariant=PART_OFFSET_RESULT_INVARIANTS[checked.operation],
        native_receipt=receipt,
        source_shape_sha256s=source_shape_sha256s,
        result_shape_sha256=current_result_shape_sha256,
    )
    ownership.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


@dataclass(frozen=True, slots=True)
class PartOffsetReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]


PART_OFFSET_REVIEWED_FAMILY_SPEC: Final = PartOffsetReviewedFamilySpec(
    manifest=PART_OFFSET_MANIFEST,
    subject_type_term=_bridge_term(PART_OFFSET_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_offset_reviewed_adapter_factory,
    validate_plan=validate_part_offset_reviewed_plan,
    execute_plan=execute_part_offset_reviewed_plan,
)


__all__ = [
    "PART_OFFSET_REQUIRED_SOURCE_ROLES",
    "PART_OFFSET_RESULT_INVARIANTS",
    "PART_OFFSET_REVIEWED_FAMILY_SPEC",
    "PART_OFFSET_REVIEWED_PRODUCT_IDENTITIES",
    "PART_OFFSET_REVIEWED_PRODUCT_OPERATIONS",
    "PartOffsetOwnershipClosure",
    "PartOffsetResultInvariant",
    "PartOffsetReviewedFamilySpec",
    "execute_part_offset_reviewed_plan",
    "execute_part_offset_reviewed_plan_with_sources",
    "part_offset_reviewed_adapter_factory",
    "resolve_part_offset_reviewed_operation",
    "validate_part_offset_reviewed_plan",
]
