"""Private reviewed-family callbacks for dependency-bearing Part CSG products.

The model can name only earlier ModelProgram result slots. The executor owns
the corresponding opaque Reviewed results, and this module authenticates
those results against the current document, managed identity, provenance,
native receipt, and current BREP before binding them to the lowered PFG source
selections. No FreeCAD object name or TypeId comes from model input.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_core_adapter import (
    PART_CORE_MANIFEST,
    PART_CORE_STRUCTURE_TERM,
    build_part_core_adapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_core_rules import (
    AuthenticatedPartCoreObject,
    PartCoreBackendPlan,
    PartCoreConformanceReceipt,
    PartCoreExecutionBindings,
    PartCoreOperation,
    apply_part_core_plan,
    decode_part_core_backend_plan,
)


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _integrity_failure() -> None:
    # Lazy import prevents a cycle while the shared dispatcher constructs the
    # descriptor backed by this module.
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


PART_CSG_REVIEWED_PRODUCT_OPERATIONS: Final = (
    PartCoreOperation.CUT,
    PartCoreOperation.FUSE,
    PartCoreOperation.COMMON,
)


def part_csg_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    """Build the existing exact Part-core adapter through the shared seam."""

    return build_part_core_adapter(sink)


def validate_part_csg_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind a canonical two-source Part-core plan to one exact CSG route."""

    if (
        type(plan) is not PartCoreBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_CORE_MANIFEST.operations
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_CORE_MANIFEST.manifest_sha256
        or receipt.adapter != PART_CORE_MANIFEST.adapter
        or plan.operation not in PART_CSG_REVIEWED_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
        or len(plan.sources) != 2
        or plan.adapter_contract_sha256 != PART_CORE_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_CORE_MANIFEST.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.lowering_request_sha256 != receipt.request_digest
        or plan.source_artifact_id != receipt.source_document.artifact_id
        or plan.source_graph_id != receipt.source_document.document_id
        or plan.source_graph_sha256 != receipt.source_document.document_digest
        or plan.source_content_sha256 != receipt.source_document.content_sha256
        or plan.plan_sha256 != receipt.plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != receipt.plan_document.content_sha256
        or len(plan.canonical_bytes) != receipt.plan_document.size_bytes
    ):
        _integrity_failure()
    try:
        decoded = decode_part_core_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=receipt.plan_document.content_sha256,
            expected_plan_sha256=receipt.plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()


_BINDING_SEAL = object()


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedReviewedPartCsgBindings:
    """Factory-only binding of ordered PFG sources to Reviewed products."""

    plan_sha256: str
    execution: PartCoreExecutionBindings
    source_result_shape_sha256s: tuple[str, str]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._seal is not _BINDING_SEAL
            or type(self.plan_sha256) is not str
            or len(self.plan_sha256) != 64
            or type(self.execution) is not PartCoreExecutionBindings
            or type(self.source_result_shape_sha256s) is not tuple
            or len(self.source_result_shape_sha256s) != 2
            or any(
                type(item) is not str or len(item) != 64
                for item in self.source_result_shape_sha256s
            )
        ):
            _integrity_failure()


def _shape_sha256(obj: object) -> str:
    try:
        raw = obj.Shape.exportBrepToString().encode("utf-8")  # type: ignore[attr-defined]
    except (AttributeError, UnicodeError, ValueError, TypeError, OverflowError):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def build_part_csg_reviewed_bindings(
    document: object,
    plan: object,
    operation: object,
    context: object,
) -> AuthenticatedReviewedPartCsgBindings:
    """Authenticate executor-owned source results and bind them in PFG order."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
        _ReviewedFamilyExecutionContext,
    )

    if (
        document is None
        or type(plan) is not PartCoreBackendPlan
        or type(operation) is not ReviewedOperationSpec
        or plan.operation not in PART_CSG_REVIEWED_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
        or len(plan.sources) != 2
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or type(context.source_results) is not tuple
        or len(context.source_results) != 2
        or any(type(item) is not ReviewedNativeExecutionResult for item in context.source_results)
    ):
        _integrity_failure()
    try:
        session = context.session
        read_identity = session.read_object_identity
        if session.doc is not document or not callable(read_identity):
            raise ValueError
        document_objects = tuple(document.Objects)
    except (AttributeError, TypeError, ValueError):
        _integrity_failure()

    authenticated: list[AuthenticatedPartCoreObject] = []
    shape_sha256s: list[str] = []
    seen_objects: list[object] = []
    for selection, source in zip(plan.sources, context.source_results, strict=True):
        try:
            obj = source.object
            identity = read_identity(obj)
            receipt = source.native_receipt
            owned = obj.Document is document and any(obj is item for item in document_objects)
            object_name = obj.Name
            object_type = obj.TypeId
        except (AttributeError, KeyError, TypeError, ValueError):
            _integrity_failure()
        expected_role = (
            SemanticRole.FEATURE
            if source.route.operation.native_type_id in {"Part::Cut", "Part::Fuse", "Part::Common"}
            else SemanticRole.PRIMITIVE
        )
        if (
            type(identity) is not EntityIdentity
            or type(receipt) is not PartCoreConformanceReceipt
            or not owned
            or any(obj is item for item in seen_objects)
            or not any(source.route is item for item in CURRENT_REVIEWED_INTENT_ROUTES)
            or source.route.manifest is not PART_CORE_MANIFEST
            or source.route.operation.operation_id != receipt.operation.value
            or source.plan_sha256 != receipt.plan_sha256
            or object_name != receipt.object_name
            or object_type != source.route.operation.native_type_id
            or identity.object_type != object_type
            or identity.feature_id is None
            or identity.semantic_role is not expected_role
            or identity.provenance.source is not ProvenanceSource.MODEL
            or identity.provenance.operation_id is None
        ):
            _integrity_failure()
        current_shape_sha256 = _shape_sha256(obj)
        if not hmac.compare_digest(current_shape_sha256, receipt.result_shape_sha256):
            _integrity_failure()
        seen_objects.append(obj)
        shape_sha256s.append(current_shape_sha256)
        authenticated.append(
            AuthenticatedPartCoreObject(
                object=obj,
                node_id=selection.node_id,
                result_id=selection.result_id,
            )
        )
    return AuthenticatedReviewedPartCsgBindings(
        plan_sha256=plan.plan_sha256,
        execution=PartCoreExecutionBindings(
            document=document,
            body_id=plan.body_id,
            sources=tuple(authenticated),
        ),
        source_result_shape_sha256s=(shape_sha256s[0], shape_sha256s[1]),
        _seal=_BINDING_SEAL,
    )


def execute_part_csg_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Execute one exact Cut/Fuse/Common plan from authenticated products."""

    if (
        document is None
        or type(plan) is not PartCoreBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or plan.operation not in PART_CSG_REVIEWED_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
    ):
        _integrity_failure()
    try:
        decoded = decode_part_core_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()

    bindings = build_part_csg_reviewed_bindings(document, plan, operation, context)
    before = tuple(document.Objects)
    receipt = apply_part_core_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=bindings.execution,
    )
    try:
        result = document.getObject(receipt.object_name)
        after = tuple(document.Objects)
        preserved_source_shapes = tuple(
            _shape_sha256(item.object) for item in bindings.execution.sources
        )
    except (Exception, SystemExit):
        _integrity_failure()
    added = tuple(item for item in after if not any(item is existing for existing in before))
    if (
        type(receipt) is not PartCoreConformanceReceipt
        or receipt.operation is not plan.operation
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.source_shape_sha256s != bindings.source_result_shape_sha256s
        or preserved_source_shapes != bindings.source_result_shape_sha256s
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
        or not hmac.compare_digest(_shape_sha256(result), receipt.result_shape_sha256)
    ):
        _integrity_failure()

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=receipt)


@dataclass(frozen=True, slots=True)
class PartCsgReviewedFamilySpec:
    """Arguments used to instantiate the shared private family descriptor."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]


PART_CSG_REVIEWED_FAMILY_SPEC: Final = PartCsgReviewedFamilySpec(
    manifest=PART_CORE_MANIFEST,
    subject_type_term=_bridge_term(PART_CORE_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_CSG_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_csg_reviewed_adapter_factory,
    validate_plan=validate_part_csg_reviewed_plan,
    execute_plan=execute_part_csg_reviewed_plan,
)


__all__ = [
    "PART_CSG_REVIEWED_FAMILY_SPEC",
    "PART_CSG_REVIEWED_PRODUCT_OPERATIONS",
    "AuthenticatedReviewedPartCsgBindings",
    "PartCsgReviewedFamilySpec",
    "build_part_csg_reviewed_bindings",
    "execute_part_csg_reviewed_plan",
    "part_csg_reviewed_adapter_factory",
    "validate_part_csg_reviewed_plan",
]
