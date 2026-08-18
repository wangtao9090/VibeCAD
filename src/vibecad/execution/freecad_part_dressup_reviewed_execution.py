"""Private product closure for the three legacy root-level Part dress-ups.

The formal family, exact adapter, and FreeCAD rule already exist.  This module
adds only the product-side compatibility payload needed by a future dispatcher
registration.  Sources are accepted solely as authenticated results retained
by the current reviewed run; semantic edge and face roles remain inside the
trusted native rule and never become public ``EdgeN``/``FaceN`` selectors.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass, field, is_dataclass, replace
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_dressup_adapter import (
    PART_DRESSUP_MANIFEST,
    PART_DRESSUP_TARGET_STRUCTURE_TERM,
    FreeCADPartDressupAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric import freecad_part_dressup_rules as dressup_rules
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_dressup_rules import (
    PART_DRESSUP_NATIVE_TYPE_IDS,
    PartDressupBackendPlan,
    PartDressupConformanceReceipt,
    PartDressupExecutionBindings,
    PartDressupOperation,
    PartDressupSelectionRole,
    apply_part_dressup_plan,
    decode_part_dressup_backend_plan,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.part-dressup-product-ownership.v1\0"


def _integrity_failure() -> None:
    # Lazy imports keep shared dispatcher -> family initialization acyclic.
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


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


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _shape_sha256(item: object) -> str:
    try:
        raw = item.Shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit, UnicodeError):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def _valid_solid(item: object) -> bool:
    try:
        shape = item.Shape
        volume = float(shape.Volume)
        return (
            item.isValid() is True
            and tuple(item.State) == ("Up-to-date",)
            and shape.isNull() is False
            and shape.isValid() is True
            and shape.ShapeType == "Solid"
            and len(tuple(shape.Solids)) == 1
            and math.isfinite(volume)
            and volume > 0.0
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _receipt_sha256_is_current(receipt: object, expected: object) -> bool:
    """Re-run a frozen receipt's constructor to detect process-local tampering."""

    if not _is_sha256(expected) or not is_dataclass(receipt):
        return False
    try:
        rebuilt = replace(receipt)
        actual = getattr(rebuilt, "receipt_sha256", None)
        return _is_sha256(actual) and hmac.compare_digest(expected, actual)
    except (Exception, SystemExit, TypeError, ValueError):
        return False


PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS: Final = tuple(PartDressupOperation)
_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PART_DRESSUP_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PART_DRESSUP_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS
    }
)
PART_DRESSUP_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)
PART_DRESSUP_REQUIRED_SOURCE_ROLES: Final = MappingProxyType(
    {operation.value: ("source_solid",) for operation in PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS}
)

_EXPECTED_SELECTION_ROLES: Final = MappingProxyType(
    {
        PartDressupOperation.EDGE_FILLET: (PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z),
        PartDressupOperation.EDGE_CHAMFER: (PartDressupSelectionRole.OUTER_MAX_X_MAX_Y_PARALLEL_Z),
        PartDressupOperation.FACE_THICKNESS: PartDressupSelectionRole.OUTER_MAX_Z_PLANAR_FACE,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDressupResultInvariant:
    """One native ``Part::*`` result with a real single-solid effect."""

    operation: PartDressupOperation
    native_type_id: str
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        if (
            type(self.operation) is not PartDressupOperation
            or self.native_type_id != PART_DRESSUP_NATIVE_TYPE_IDS.get(self.operation)
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        result: object,
        receipt: PartDressupConformanceReceipt,
        *,
        source_shape_sha256: str,
        result_shape_sha256: str,
    ) -> None:
        """Recheck root ownership, current shapes, and the dress-up effect."""

        try:
            source = document.getObject(receipt.source_object_name)
            source_volume = float(source.Shape.Volume)
            result_volume = float(result.Shape.Volume)
            epsilon = max(1e-9, abs(source_volume) * 1e-12)
            valid = (
                type(receipt) is PartDressupConformanceReceipt
                and receipt.operation is self.operation
                and receipt.selection_role is _EXPECTED_SELECTION_ROLES[self.operation]
                and receipt.native_type_id == self.native_type_id
                and source is not None
                and source is not result
                and source.Document is document
                and result.Document is document
                and document.getObject(receipt.object_name) is result
                and any(source is item for item in tuple(document.Objects))
                and any(result is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.native_type_id
                and not tuple(result.getParentGroup() or ())
                and _valid_solid(source)
                and _valid_solid(result)
                and hmac.compare_digest(_shape_sha256(source), source_shape_sha256)
                and hmac.compare_digest(_shape_sha256(result), result_shape_sha256)
                and not hmac.compare_digest(source_shape_sha256, result_shape_sha256)
                and not math.isclose(
                    source_volume,
                    result_volume,
                    rel_tol=0.0,
                    abs_tol=epsilon,
                )
            )
        except (Exception, SystemExit, TypeError, ValueError, OverflowError):
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
            or not math.isfinite(observation.volume_mm3)
            or observation.volume_mm3 <= 0.0
        ):
            _integrity_failure()


PART_DRESSUP_RESULT_INVARIANTS: Final = MappingProxyType(
    {
        operation: PartDressupResultInvariant(
            operation=operation,
            native_type_id=PART_DRESSUP_NATIVE_TYPE_IDS[operation],
        )
        for operation in PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDressupOwnershipClosure:
    """Bind native receipt, source freshness, and result shape through adoption."""

    invariant: PartDressupResultInvariant
    native_receipt: PartDressupConformanceReceipt
    source_receipt_sha256: str
    source_shape_sha256: str
    result_shape_sha256: str
    plan_content_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.invariant) is not PartDressupResultInvariant
            or type(self.native_receipt) is not PartDressupConformanceReceipt
            or self.native_receipt.operation is not self.invariant.operation
            or any(
                not _is_sha256(item)
                for item in (
                    self.source_receipt_sha256,
                    self.source_shape_sha256,
                    self.result_shape_sha256,
                    self.plan_content_sha256,
                )
            )
            or hmac.compare_digest(self.source_shape_sha256, self.result_shape_sha256)
        ):
            _integrity_failure()
        body = b"\0".join(
            (
                _OWNERSHIP_DIGEST_DOMAIN,
                self.native_receipt.receipt_sha256.encode("ascii"),
                self.source_receipt_sha256.encode("ascii"),
                self.source_shape_sha256.encode("ascii"),
                self.result_shape_sha256.encode("ascii"),
                self.plan_content_sha256.encode("ascii"),
                self.invariant.native_type_id.encode("ascii"),
            )
        )
        object.__setattr__(self, "receipt_sha256", hashlib.sha256(body).hexdigest())

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def operation(self) -> PartDressupOperation:
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

    def validate_native_result(self, document: object, result: object) -> None:
        self.invariant.validate_native_result(
            document,
            result,
            self.native_receipt,
            source_shape_sha256=self.source_shape_sha256,
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


def resolve_part_dressup_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve only one complete formal identity; aliases remain inert."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def part_dressup_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return FreeCADPartDressupAdapter(sink)


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartDressupBackendPlan:
    if (
        type(plan) is not PartDressupBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_DRESSUP_MANIFEST.operations
        or plan.operation not in PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS
        or _OPERATIONS_BY_ID.get(plan.operation.value) != operation
        or plan.selection_role is not _EXPECTED_SELECTION_ROLES[plan.operation]
        or plan.adapter_contract_sha256 != PART_DRESSUP_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_DRESSUP_MANIFEST.manifest_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
        or plan_document.role_term_ref_id != PART_DRESSUP_MANIFEST.plan_role_term.term_ref_id
        or plan_document.schema_term_ref_id != PART_DRESSUP_MANIFEST.plan_schema_term.term_ref_id
        or plan_document.media_type != PART_DRESSUP_MANIFEST.plan_media_type
    ):
        _integrity_failure()
    try:
        decoded = decode_part_dressup_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_part_dressup_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind one canonical plan to the exact adapter and formal operation."""

    if (
        type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_DRESSUP_MANIFEST.manifest_sha256
        or receipt.adapter != PART_DRESSUP_MANIFEST.adapter
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
) -> PartDressupBackendPlan:
    if type(payload) is not bytes:
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_part_dressup_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    return checked


def _authenticated_source(
    document: object,
    source_results: tuple[object, ...],
    *,
    session: object,
    run_token: object,
) -> tuple[object, str, str]:
    """Return one current same-run solid plus its shape and receipt digests."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
    )

    if (
        session is None
        or run_token is None
        or type(source_results) is not tuple
        or len(source_results) != 1
        or type(source_results[0]) is not ReviewedNativeExecutionResult
    ):
        _integrity_failure()
    source = source_results[0]
    item = source.object
    receipt = source.native_receipt
    try:
        if session.doc is not document:
            raise ValueError
        identity = session.read_object_identity(item)
        document_objects = tuple(document.Objects)
    except (AttributeError, KeyError, TypeError, ValueError):
        _integrity_failure()
    receipt_operation = getattr(receipt, "operation", None)
    expected_shape_sha256 = getattr(receipt, "result_shape_sha256", None)
    receipt_sha256 = getattr(receipt, "receipt_sha256", None)
    try:
        retained = source._is_retained_for_run(run_token)
    except (AttributeError, TypeError, ValueError):
        retained = False
    if (
        type(identity) is not EntityIdentity
        or not retained
        or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
        or source.route.operation not in source.route.manifest.operations
        or getattr(receipt_operation, "value", None) != source.route.operation.operation_id
        or getattr(receipt, "plan_sha256", None) != source.plan_sha256
        or getattr(receipt, "object_name", None) != getattr(item, "Name", None)
        or not _is_sha256(source.plan_content_sha256)
        or not _receipt_sha256_is_current(receipt, receipt_sha256)
        or not _is_sha256(expected_shape_sha256)
        or not hmac.compare_digest(_shape_sha256(item), expected_shape_sha256)
        or getattr(item, "Document", None) is not document
        or not any(item is current for current in document_objects)
        or getattr(item, "TypeId", None) != source.route.operation.native_type_id
        or source.object is not source.owned_objects[0]
        or identity.object_type != source.route.operation.native_type_id
        or identity.feature_id is None
        or identity.semantic_role not in {SemanticRole.PRIMITIVE, SemanticRole.FEATURE}
        or not source.semantic_roles
        or source.semantic_roles[0] is not identity.semantic_role
        or identity.provenance.source is not ProvenanceSource.MODEL
        or identity.provenance.operation_id != "apply_reviewed_intent"
        or source.result_kind.value != "solid"
        or not _valid_solid(item)
    ):
        _integrity_failure()
    return item, expected_shape_sha256, receipt_sha256


def execute_part_dressup_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
    run_token: object,
) -> object:
    """Execute one legacy Part dress-up from its sole authenticated source."""

    if document is None:
        _integrity_failure()
    checked = _decode_execution_plan(plan, payload, plan_document, operation)
    source, source_shape_sha256, source_receipt_sha256 = _authenticated_source(
        document,
        source_results,
        session=session,
        run_token=run_token,
    )
    try:
        before = tuple(document.Objects)
    except (Exception, SystemExit):
        _integrity_failure()
    receipt = apply_part_dressup_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=PartDressupExecutionBindings(
            document=document,
            container_id=checked.container_id,
            source_node_id=checked.source_node_id,
            source_solid_result_id=checked.source_solid_result_id,
            source_object=source,
        ),
    )
    try:
        after = tuple(document.Objects)
        result = document.getObject(receipt.object_name)
        added = tuple(item for item in after if not any(item is old for old in before))
        native_index = dressup_rules._resolve_semantic_selection(  # noqa: SLF001
            source,
            checked.selection_role,
        )
        current_source_sha256 = _shape_sha256(source)
        result_shape_sha256 = _shape_sha256(result)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(receipt) is not PartDressupConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.selection_role is not checked.selection_role
        or receipt.plan_sha256 != checked.plan_sha256
        or receipt.native_type_id != operation.native_type_id
        or receipt.source_object_name != getattr(source, "Name", None)
        or len(after) != len(before) + 1
        or any(
            actual is not expected
            for actual, expected in zip(after[: len(before)], before, strict=True)
        )
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
        or not hmac.compare_digest(current_source_sha256, source_shape_sha256)
    ):
        _integrity_failure()
    try:
        dressup_rules._validate_root_ownership(result)  # noqa: SLF001
        dressup_rules._validate_native_binding(  # noqa: SLF001
            result,
            source,
            checked.operation,
            native_index,
            checked.magnitude_mm,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    ownership = PartDressupOwnershipClosure(
        invariant=PART_DRESSUP_RESULT_INVARIANTS[checked.operation],
        native_receipt=receipt,
        source_receipt_sha256=source_receipt_sha256,
        source_shape_sha256=source_shape_sha256,
        result_shape_sha256=result_shape_sha256,
        plan_content_sha256=plan_document.content_sha256,
    )
    ownership.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


def execute_part_dressup_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Shared callback shape; descriptor registration remains a separate change."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if type(context) is not _ReviewedFamilyExecutionContext or context.document is not document:
        _integrity_failure()
    return execute_part_dressup_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
        run_token=context.run_token,
    )


@dataclass(frozen=True, slots=True)
class PartDressupReviewedFamilySpec:
    """Complete family-only compatibility payload; intentionally unregistered."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter] = field(
        repr=False,
        compare=False,
    )
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None] = field(
        repr=False,
        compare=False,
    )
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ] = field(repr=False, compare=False)
    result_invariants: MappingProxyType = field(repr=False)
    minimum_sources: int = 1
    maximum_sources: int = 1
    requires_same_run_sources: bool = True

    def __post_init__(self) -> None:
        if (
            self.manifest is not PART_DRESSUP_MANIFEST
            or self.subject_type_term != _bridge_term(PART_DRESSUP_TARGET_STRUCTURE_TERM)
            or self.operation_ids
            != tuple(item.value for item in PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS)
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
            or self.result_invariants is not PART_DRESSUP_RESULT_INVARIANTS
            or self.minimum_sources != 1
            or self.maximum_sources != 1
            or self.requires_same_run_sources is not True
        ):
            _integrity_failure()


PART_DRESSUP_REVIEWED_FAMILY_SPEC: Final = PartDressupReviewedFamilySpec(
    manifest=PART_DRESSUP_MANIFEST,
    subject_type_term=_bridge_term(PART_DRESSUP_TARGET_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_dressup_reviewed_adapter_factory,
    validate_plan=validate_part_dressup_reviewed_plan,
    execute_plan=execute_part_dressup_reviewed_plan,
    result_invariants=PART_DRESSUP_RESULT_INVARIANTS,
)


def build_part_dressup_reviewed_family_descriptor() -> object:
    """Build the complete descriptor without adding it to ``CURRENT`` routes."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedIntentFamilyDescriptor,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    return _ReviewedIntentFamilyDescriptor(
        manifest=PART_DRESSUP_REVIEWED_FAMILY_SPEC.manifest,
        subject_type_term=PART_DRESSUP_REVIEWED_FAMILY_SPEC.subject_type_term,
        adapter_factory=PART_DRESSUP_REVIEWED_FAMILY_SPEC.adapter_factory,
        validate_plan=PART_DRESSUP_REVIEWED_FAMILY_SPEC.validate_plan,
        execute_plan=PART_DRESSUP_REVIEWED_FAMILY_SPEC.execute_plan,
        product_results=tuple(
            _ReviewedProductResultContract(
                operation_id=operation.value,
                result_kind=_ReviewedProductResultKind.SOLID,
                owned_type_ids=(PART_DRESSUP_NATIVE_TYPE_IDS[operation],),
                semantic_roles=(SemanticRole.FEATURE,),
            )
            for operation in PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS
        ),
        minimum_sources=PART_DRESSUP_REVIEWED_FAMILY_SPEC.minimum_sources,
        maximum_sources=PART_DRESSUP_REVIEWED_FAMILY_SPEC.maximum_sources,
        requires_same_run_sources=(PART_DRESSUP_REVIEWED_FAMILY_SPEC.requires_same_run_sources),
    )


__all__ = [
    "PART_DRESSUP_REQUIRED_SOURCE_ROLES",
    "PART_DRESSUP_RESULT_INVARIANTS",
    "PART_DRESSUP_REVIEWED_FAMILY_SPEC",
    "PART_DRESSUP_REVIEWED_PRODUCT_IDENTITIES",
    "PART_DRESSUP_REVIEWED_PRODUCT_OPERATIONS",
    "PartDressupOwnershipClosure",
    "PartDressupResultInvariant",
    "PartDressupReviewedFamilySpec",
    "build_part_dressup_reviewed_family_descriptor",
    "execute_part_dressup_reviewed_plan",
    "execute_part_dressup_reviewed_plan_with_sources",
    "part_dressup_reviewed_adapter_factory",
    "resolve_part_dressup_reviewed_operation",
    "validate_part_dressup_reviewed_plan",
]
