"""Private product-execution callbacks for reviewed Part curves and paths.

The shared reviewed-intent dispatcher owns proof construction, attestation,
lowering orchestration, and error normalization.  This module contributes only
the statically reviewed Part curve/path contracts needed by that dispatcher.
It is deliberately not a dynamic plugin registry or a public execution API.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_curve_adapter import (
    PART_CURVE_MANIFEST,
    PART_CURVE_STRUCTURE_TERM,
    FreeCADPartCurveAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_curve_rules import (
    PartCurveBackendPlan,
    PartCurveConformanceReceipt,
    PartCurveExecutionBindings,
    PartCurveOperation,
    apply_part_curve_plan,
    decode_part_curve_backend_plan,
)


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
    # Kept lazy so the shared dispatcher can import this family module without
    # creating an import cycle while defining its private descriptor types.
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


PART_CURVE_REVIEWED_PRODUCT_OPERATIONS: Final = (
    PartCurveOperation.CIRCLE,
    PartCurveOperation.ELLIPSE,
    PartCurveOperation.HELIX,
    PartCurveOperation.LINE,
    PartCurveOperation.PLANE,
    PartCurveOperation.POLYGON,
    PartCurveOperation.REGULAR_POLYGON,
    PartCurveOperation.SPIRAL,
    PartCurveOperation.VERTEX,
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PART_CURVE_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PART_CURVE_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PART_CURVE_REVIEWED_PRODUCT_OPERATIONS
    }
)
PART_CURVE_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)


def resolve_part_curve_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Return one exact static curve operation, or stay inert on any mismatch."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def part_curve_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    """Build the existing exact adapter through the shared family seam."""

    return FreeCADPartCurveAdapter(sink)


def validate_part_curve_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind a canonical curve plan back to its exact reviewed route."""

    if (
        type(plan) is not PartCurveBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_CURVE_MANIFEST.operations
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_CURVE_MANIFEST.manifest_sha256
        or receipt.adapter != PART_CURVE_MANIFEST.adapter
        or plan.operation.value != operation.operation_id
        or plan.operation not in PART_CURVE_REVIEWED_PRODUCT_OPERATIONS
        or plan.adapter_contract_sha256 != PART_CURVE_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_CURVE_MANIFEST.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.lowering_request_sha256 != receipt.request_digest
        or plan.source_artifact_id != receipt.source_document.artifact_id
        or plan.source_graph_id != receipt.source_document.document_id
        or plan.source_graph_sha256 != receipt.source_document.document_digest
        or plan.source_content_sha256 != receipt.source_document.content_sha256
        or receipt.plan_document.document_digest != plan.plan_sha256
        or receipt.plan_document.content_sha256 != hashlib.sha256(plan.canonical_bytes).hexdigest()
        or receipt.plan_document.size_bytes != len(plan.canonical_bytes)
    ):
        _integrity_failure()
    try:
        decoded = decode_part_curve_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=receipt.plan_document.content_sha256,
            expected_plan_sha256=receipt.plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()


def execute_part_curve_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Execute one exact curve plan and return the shared native result shape."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        document is None
        or type(plan) is not PartCurveBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_CURVE_MANIFEST.operations
        or plan.operation not in PART_CURVE_REVIEWED_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
        or plan.adapter_contract_sha256 != PART_CURVE_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_CURVE_MANIFEST.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.source_results
    ):
        _integrity_failure()
    try:
        decoded = decode_part_curve_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()

    before = tuple(document.Objects)
    receipt = apply_part_curve_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=PartCurveExecutionBindings(
            document=document,
            expected_adapter_contract_sha256=PART_CURVE_MANIFEST.adapter.adapter_contract_sha256,
            expected_manifest_sha256=PART_CURVE_MANIFEST.manifest_sha256,
            expected_operation_specification_sha256=operation.specification_sha256,
        ),
    )
    try:
        result = document.getObject(receipt.object_name)
        after = tuple(document.Objects)
    except (Exception, SystemExit):
        _integrity_failure()
    added = tuple(item for item in after if not any(item is existing for existing in before))
    if (
        type(receipt) is not PartCurveConformanceReceipt
        or receipt.operation is not plan.operation
        or receipt.plan_sha256 != plan.plan_sha256
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
    ):
        _integrity_failure()

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=receipt)


@dataclass(frozen=True, slots=True)
class PartCurveReviewedFamilySpec:
    """Arguments used to instantiate the shared private family descriptor."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]


PART_CURVE_REVIEWED_FAMILY_SPEC: Final = PartCurveReviewedFamilySpec(
    manifest=PART_CURVE_MANIFEST,
    subject_type_term=_bridge_term(PART_CURVE_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_CURVE_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_curve_reviewed_adapter_factory,
    validate_plan=validate_part_curve_reviewed_plan,
    execute_plan=execute_part_curve_reviewed_plan,
)


__all__ = [
    "PART_CURVE_REVIEWED_FAMILY_SPEC",
    "PART_CURVE_REVIEWED_PRODUCT_IDENTITIES",
    "PART_CURVE_REVIEWED_PRODUCT_OPERATIONS",
    "PartCurveReviewedFamilySpec",
    "execute_part_curve_reviewed_plan",
    "part_curve_reviewed_adapter_factory",
    "resolve_part_curve_reviewed_operation",
    "validate_part_curve_reviewed_plan",
]
