"""Private product callbacks for four reviewed document-root Part datums.

The shared dispatcher owns routing, proof, attestation, and error
normalization.  This module contributes one closed family descriptor payload:
exact identity resolution, canonical plan binding, and native execution for
unattached explicit-placement datums.  An LCS owns its seven generated origin
helpers in addition to the primary product object.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_datum_adapter import (
    PART_DATUM_MANIFEST,
    PART_DATUM_STRUCTURE_TERM,
    FreeCADPartDatumAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_datum_rules import (
    PART_DATUM_NATIVE_TYPE_IDS,
    PartDatumBackendPlan,
    PartDatumConformanceReceipt,
    PartDatumExecutionBindings,
    PartDatumOperation,
    apply_part_datum_plan,
    decode_part_datum_backend_plan,
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
    # Lazy imports keep shared dispatcher -> datum family initialization acyclic.
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


PART_DATUM_REVIEWED_PRODUCT_OPERATIONS: Final = tuple(PartDatumOperation)
_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PART_DATUM_MANIFEST.operations}
)
PART_DATUM_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(
    (
        f"{PART_DATUM_MANIFEST.family_id}.{operation.value}",
        _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
    )
    for operation in PART_DATUM_REVIEWED_PRODUCT_OPERATIONS
)


def resolve_part_datum_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve one complete static identity without accepting aliases."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    try:
        index = PART_DATUM_REVIEWED_PRODUCT_IDENTITIES.index((operation_id, semantic_operation))
    except ValueError:
        return None
    operation = PART_DATUM_REVIEWED_PRODUCT_OPERATIONS[index]
    return _OPERATIONS_BY_ID[operation.value]


def part_datum_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return FreeCADPartDatumAdapter(sink)


def validate_part_datum_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind a canonical datum plan to its exact static Reviewed route."""

    if (
        type(plan) is not PartDatumBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_DATUM_MANIFEST.operations
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_DATUM_MANIFEST.manifest_sha256
        or receipt.adapter != PART_DATUM_MANIFEST.adapter
        or plan.operation.value != operation.operation_id
        or plan.operation not in PART_DATUM_REVIEWED_PRODUCT_OPERATIONS
        or plan.adapter_contract_sha256 != PART_DATUM_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_DATUM_MANIFEST.manifest_sha256
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
        decoded = decode_part_datum_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=receipt.plan_document.content_sha256,
            expected_plan_sha256=receipt.plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()


def _body_tips(document: object) -> tuple[tuple[object, object], ...]:
    try:
        return tuple(
            (item, item.Tip) for item in document.Objects if item.TypeId == "PartDesign::Body"
        )
    except (Exception, SystemExit):
        _integrity_failure()


def _same_body_tips(
    left: tuple[tuple[object, object], ...],
    right: tuple[tuple[object, object], ...],
) -> bool:
    return len(left) == len(right) and all(
        actual_body is expected_body and actual_tip is expected_tip
        for (actual_body, actual_tip), (expected_body, expected_tip) in zip(
            left, right, strict=True
        )
    )


def execute_part_datum_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Execute one root datum and return its exact ordered ownership closure."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        document is None
        or type(plan) is not PartDatumBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_DATUM_MANIFEST.operations
        or plan.operation not in PART_DATUM_REVIEWED_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
        or plan.adapter_contract_sha256 != PART_DATUM_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_DATUM_MANIFEST.manifest_sha256
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.source_results
    ):
        _integrity_failure()
    try:
        decoded = decode_part_datum_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
        before = tuple(document.Objects)
        body_tips = _body_tips(document)
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()

    receipt = apply_part_datum_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=PartDatumExecutionBindings(
            document=document,
            container_id=plan.container_id,
        ),
    )
    try:
        after = tuple(document.Objects)
        owned = tuple(document.getObject(name) for name in receipt.owned_object_names)
        created = after[len(before) :]
        result = document.getObject(receipt.object_name)
    except (Exception, SystemExit):
        _integrity_failure()
    expected_count = 8 if plan.operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM else 1
    if (
        type(receipt) is not PartDatumConformanceReceipt
        or receipt.operation is not plan.operation
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.native_type_id != operation.native_type_id
        or len(after) != len(before) + expected_count
        or any(
            actual is not expected
            for actual, expected in zip(after[: len(before)], before, strict=True)
        )
        or len(created) != expected_count
        or len(owned) != expected_count
        or any(item is None for item in owned)
        or any(actual is not expected for actual, expected in zip(created, owned, strict=True))
        or result is not owned[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != PART_DATUM_NATIVE_TYPE_IDS[plan.operation]
        or not _same_body_tips(_body_tips(document), body_tips)
    ):
        _integrity_failure()
    if plan.operation is PartDatumOperation.LOCAL_COORDINATE_SYSTEM:
        try:
            if any(
                actual is not expected
                for actual, expected in zip(tuple(result.OriginFeatures), owned[1:], strict=True)
            ):
                _integrity_failure()
        except (Exception, SystemExit):
            _integrity_failure()

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(
        object=result,
        receipt=receipt,
        owned_objects=owned,
    )


@dataclass(frozen=True, slots=True)
class PartDatumReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]


PART_DATUM_REVIEWED_FAMILY_SPEC: Final = PartDatumReviewedFamilySpec(
    manifest=PART_DATUM_MANIFEST,
    subject_type_term=_bridge_term(PART_DATUM_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_DATUM_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_datum_reviewed_adapter_factory,
    validate_plan=validate_part_datum_reviewed_plan,
    execute_plan=execute_part_datum_reviewed_plan,
)


__all__ = [
    "PART_DATUM_REVIEWED_FAMILY_SPEC",
    "PART_DATUM_REVIEWED_PRODUCT_IDENTITIES",
    "PART_DATUM_REVIEWED_PRODUCT_OPERATIONS",
    "PartDatumReviewedFamilySpec",
    "execute_part_datum_reviewed_plan",
    "part_datum_reviewed_adapter_factory",
    "resolve_part_datum_reviewed_operation",
    "validate_part_datum_reviewed_plan",
]
