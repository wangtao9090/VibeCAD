"""Private reviewed product callbacks for the eight residual Part operations.

The exact Part-core adapter and native rule already own lowering and mutation.
This module supplies only the missing product boundary: ordered whole-object
sources must be engine-owned ``ReviewedNativeExecutionResult`` instances from
the same program run, and their managed identity, provenance, shape, and
object state are rechecked immediately before the native rule may mutate the
document.  The family descriptor is an explicit handoff and is deliberately
not added to ``CURRENT_REVIEWED_INTENT_ROUTES`` here.

The Part rule supports as many as sixteen aggregate sources.  The current
reviewed program wire is closed at eight, so the product contracts below are
truthful for two through eight sources only; plans with nine through sixteen
remain valid Part-core plans but are not product-executable through this seam.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
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

_MAX_PRODUCT_SOURCES: Final = 8
_SOURCE_STATE_DOMAIN = b"vibecad.reviewed-part-residual.source-state.v1\0"
_CLOSURE_DOMAIN = b"vibecad.reviewed-part-residual.ownership-closure.v1\0"


def _integrity_failure() -> None:
    # Lazy imports avoid a cycle while the shared dispatcher defines the
    # private descriptor and result types consumed by the handoff below.
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _integrity_failure()
    if not raw or len(raw) > 32 * 1024:
        _integrity_failure()
    return raw


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


class PartResidualProductResultKind(StrEnum):
    """Closed product observation kinds used by the shared handoff."""

    SOLID = "solid"
    VALID_SHAPE = "valid_shape"


PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS: Final = (
    PartCoreOperation.SECTION,
    PartCoreOperation.MULTI_FUSE,
    PartCoreOperation.MULTI_COMMON,
    PartCoreOperation.COMPOUND,
    PartCoreOperation.MIRROR,
    PartCoreOperation.SCALE,
    PartCoreOperation.REVERSE,
    PartCoreOperation.REFINE,
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PART_CORE_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PART_CORE_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS
    }
)
PART_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartResidualProductContract:
    """Exact current-wire source cardinality and singleton result closure."""

    operation: PartCoreOperation
    minimum_sources: int
    maximum_sources: int
    result_kind: PartResidualProductResultKind
    native_type_id: str
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        reviewed = _OPERATIONS_BY_ID.get(getattr(self.operation, "value", None))
        if (
            self.operation not in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS
            or reviewed is None
            or self.native_type_id != reviewed.native_type_id
            or not 1 <= self.minimum_sources <= self.maximum_sources <= _MAX_PRODUCT_SOURCES
            or type(self.result_kind) is not PartResidualProductResultKind
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def accepts(self, count: int) -> bool:
        return type(count) is int and self.minimum_sources <= count <= self.maximum_sources


PART_RESIDUAL_PRODUCT_CONTRACTS: Final = MappingProxyType(
    {
        operation: PartResidualProductContract(
            operation=operation,
            minimum_sources=(
                2
                if operation
                in {
                    PartCoreOperation.SECTION,
                    PartCoreOperation.MULTI_FUSE,
                    PartCoreOperation.MULTI_COMMON,
                    PartCoreOperation.COMPOUND,
                }
                else 1
            ),
            maximum_sources=(
                _MAX_PRODUCT_SOURCES
                if operation
                in {
                    PartCoreOperation.MULTI_FUSE,
                    PartCoreOperation.MULTI_COMMON,
                    PartCoreOperation.COMPOUND,
                }
                else (2 if operation is PartCoreOperation.SECTION else 1)
            ),
            result_kind=(
                PartResidualProductResultKind.VALID_SHAPE
                if operation
                in {
                    PartCoreOperation.SECTION,
                    PartCoreOperation.COMPOUND,
                    # The reviewed native rule proves reverse by a negative
                    # signed volume.  It is a valid single-solid shape, but it
                    # cannot satisfy the shared positive-volume SOLID kind.
                    PartCoreOperation.REVERSE,
                }
                else PartResidualProductResultKind.SOLID
            ),
            native_type_id=_OPERATIONS_BY_ID[operation.value].native_type_id,
        )
        for operation in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS
    }
)


def resolve_part_residual_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve one exact static identity without registering a public route."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def part_residual_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    """Reuse the reviewed Part-core lowerer; no lowering logic is copied here."""

    return build_part_core_adapter(sink)


def validate_part_residual_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind an exact Part-core plan to the truthful current-wire product subset."""

    contract = PART_RESIDUAL_PRODUCT_CONTRACTS.get(getattr(plan, "operation", None))
    if (
        type(plan) is not PartCoreBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_CORE_MANIFEST.operations
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_CORE_MANIFEST.manifest_sha256
        or receipt.adapter != PART_CORE_MANIFEST.adapter
        or type(contract) is not PartResidualProductContract
        or plan.operation.value != operation.operation_id
        or not contract.accepts(len(plan.sources))
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


def _shape_sha256(item: object) -> str:
    try:
        shape = item.Shape
        raw = shape.exportBrepToString().encode("utf-8")
        valid = shape.isNull() is False and shape.isValid() is True
    except (Exception, SystemExit, AttributeError, UnicodeError, TypeError, ValueError):
        _integrity_failure()
    if not raw or not valid:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class _AuthenticatedSourceSnapshot:
    object: object = field(repr=False, compare=False)
    identity_sha256: str
    shape_sha256: str
    state_sha256: str
    source_plan_sha256: str

    def __post_init__(self) -> None:
        if self.object is None or any(
            not _is_sha256(item)
            for item in (
                self.identity_sha256,
                self.shape_sha256,
                self.state_sha256,
                self.source_plan_sha256,
            )
        ):
            _integrity_failure()


def _source_snapshot(
    document: object,
    source: object,
    *,
    session: object,
    run_token: object,
) -> _AuthenticatedSourceSnapshot:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
    )

    if (
        type(source) is not ReviewedNativeExecutionResult
        or run_token is None
        or not source._is_retained_for_run(run_token)
        or len(source.owned_objects) != 1
        or source.owned_objects[0] is not source.object
        or len(source.semantic_roles) != 1
        or source.result_kind.value not in {"solid", "valid_shape"}
    ):
        _integrity_failure()
    item = source.object
    receipt = source.native_receipt
    try:
        if session.doc is not document:
            raise ValueError
        identity = session.read_object_identity(item)
        document_objects = tuple(document.Objects)
        object_name = item.Name
        object_type = item.TypeId
        object_state = tuple(item.State)
        object_valid = item.isValid()
    except (Exception, SystemExit, AttributeError, KeyError, TypeError, ValueError):
        _integrity_failure()
    if (
        type(identity) is not EntityIdentity
        or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
        or source.route.operation not in source.route.manifest.operations
        or getattr(receipt, "plan_sha256", None) != source.plan_sha256
        or (
            getattr(receipt, "plan_content_sha256", source.plan_content_sha256)
            != source.plan_content_sha256
        )
        or getattr(receipt, "object_name", None) != object_name
        or getattr(receipt, "operation", None) is None
        or getattr(receipt.operation, "value", None) != source.route.operation.operation_id
        or getattr(item, "Document", None) is not document
        or not any(item is existing for existing in document_objects)
        or object_type != source.route.operation.native_type_id
        or identity.object_type != object_type
        or identity.feature_id is None
        or identity.semantic_role is not source.semantic_roles[0]
        or identity.provenance.source is not ProvenanceSource.MODEL
        or identity.provenance.operation_id != "apply_reviewed_intent"
        or object_valid is not True
        or object_state != ("Up-to-date",)
    ):
        _integrity_failure()
    shape_sha256 = _shape_sha256(item)
    expected_shape_sha256 = getattr(receipt, "result_shape_sha256", None)
    if not _is_sha256(expected_shape_sha256) or not hmac.compare_digest(
        shape_sha256, expected_shape_sha256
    ):
        _integrity_failure()
    identity_mapping = identity.to_mapping()
    identity_sha256 = hashlib.sha256(_canonical(identity_mapping)).hexdigest()
    state_body = {
        "identity_sha256": identity_sha256,
        "name": object_name,
        "native_type_id": object_type,
        "object_state": list(object_state),
        "shape_sha256": shape_sha256,
        "source_plan_sha256": source.plan_sha256,
        "source_plan_content_sha256": source.plan_content_sha256,
        "source_route_contract_sha256": source.route.route_contract_sha256,
        "semantic_role": source.semantic_roles[0].value,
    }
    state_sha256 = hashlib.sha256(_SOURCE_STATE_DOMAIN + _canonical(state_body)).hexdigest()
    return _AuthenticatedSourceSnapshot(
        object=item,
        identity_sha256=identity_sha256,
        shape_sha256=shape_sha256,
        state_sha256=state_sha256,
        source_plan_sha256=source.plan_sha256,
    )


_BINDING_SEAL = object()


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedReviewedPartResidualBindings:
    """Factory-only binding of ordered PFG sources to same-run products."""

    plan_sha256: str
    execution: PartCoreExecutionBindings
    source_result_plan_sha256s: tuple[str, ...]
    source_identity_sha256s: tuple[str, ...]
    source_shape_sha256s: tuple[str, ...]
    source_state_sha256s: tuple[str, ...]
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        count = len(getattr(self.execution, "sources", ()))
        sequences = (
            self.source_result_plan_sha256s,
            self.source_identity_sha256s,
            self.source_shape_sha256s,
            self.source_state_sha256s,
        )
        if (
            self._seal is not _BINDING_SEAL
            or not _is_sha256(self.plan_sha256)
            or type(self.execution) is not PartCoreExecutionBindings
            or not 1 <= count <= _MAX_PRODUCT_SOURCES
            or any(type(items) is not tuple or len(items) != count for items in sequences)
            or any(not _is_sha256(item) for items in sequences for item in items)
        ):
            _integrity_failure()


def build_part_residual_reviewed_bindings(
    document: object,
    plan: object,
    operation: object,
    context: object,
) -> AuthenticatedReviewedPartResidualBindings:
    """Authenticate exact ordered same-run sources without accepting names or IDs."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedNativeExecutionResult,
        _ReviewedFamilyExecutionContext,
    )

    contract = PART_RESIDUAL_PRODUCT_CONTRACTS.get(getattr(plan, "operation", None))
    if (
        document is None
        or type(plan) is not PartCoreBackendPlan
        or type(operation) is not ReviewedOperationSpec
        or operation != _OPERATIONS_BY_ID.get(plan.operation.value)
        or type(contract) is not PartResidualProductContract
        or plan.operation.value != operation.operation_id
        or not contract.accepts(len(plan.sources))
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.run_token is None
        or type(context.source_results) is not tuple
        or len(context.source_results) != len(plan.sources)
        or any(type(item) is not ReviewedNativeExecutionResult for item in context.source_results)
    ):
        _integrity_failure()
    snapshots = tuple(
        _source_snapshot(
            document,
            source,
            session=context.session,
            run_token=context.run_token,
        )
        for source in context.source_results
    )
    if len({id(item.object) for item in snapshots}) != len(snapshots):
        _integrity_failure()
    authenticated = tuple(
        AuthenticatedPartCoreObject(
            object=snapshot.object,
            node_id=selection.node_id,
            result_id=selection.result_id,
        )
        for selection, snapshot in zip(plan.sources, snapshots, strict=True)
    )
    return AuthenticatedReviewedPartResidualBindings(
        plan_sha256=plan.plan_sha256,
        execution=PartCoreExecutionBindings(
            document=document,
            body_id=plan.body_id,
            sources=authenticated,
        ),
        source_result_plan_sha256s=tuple(item.source_plan_sha256 for item in snapshots),
        source_identity_sha256s=tuple(item.identity_sha256 for item in snapshots),
        source_shape_sha256s=tuple(item.shape_sha256 for item in snapshots),
        source_state_sha256s=tuple(item.state_sha256 for item in snapshots),
        _seal=_BINDING_SEAL,
    )


def validate_part_residual_bindings_current(
    document: object,
    plan: object,
    operation: object,
    context: object,
    bindings: object,
) -> None:
    """Recheck every content binding at the final pre-mutation boundary."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        type(plan) is not PartCoreBackendPlan
        or type(operation) is not ReviewedOperationSpec
        or operation != _OPERATIONS_BY_ID.get(plan.operation.value)
        or type(context) is not _ReviewedFamilyExecutionContext
        or type(bindings) is not AuthenticatedReviewedPartResidualBindings
        or context.document is not document
        or bindings.plan_sha256 != plan.plan_sha256
        or bindings.execution.document is not document
        or bindings.execution.body_id != plan.body_id
        or len(context.source_results) != len(plan.sources)
        or len(bindings.execution.sources) != len(plan.sources)
    ):
        _integrity_failure()
    snapshots = tuple(
        _source_snapshot(
            document,
            source,
            session=context.session,
            run_token=context.run_token,
        )
        for source in context.source_results
    )
    expected_objects = tuple(item.object for item in snapshots)
    bound_objects = tuple(item.object for item in bindings.execution.sources)
    bound_selections = tuple((item.node_id, item.result_id) for item in bindings.execution.sources)
    plan_selections = tuple((item.node_id, item.result_id) for item in plan.sources)
    if (
        any(left is not right for left, right in zip(expected_objects, bound_objects, strict=True))
        or bound_selections != plan_selections
        or tuple(item.source_plan_sha256 for item in snapshots)
        != bindings.source_result_plan_sha256s
        or tuple(item.identity_sha256 for item in snapshots) != bindings.source_identity_sha256s
        or tuple(item.shape_sha256 for item in snapshots) != bindings.source_shape_sha256s
        or tuple(item.state_sha256 for item in snapshots) != bindings.source_state_sha256s
    ):
        _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartResidualResultInvariant:
    """Exact singleton native result contract for one residual operation."""

    contract: PartResidualProductContract

    def __post_init__(self) -> None:
        if (
            type(self.contract) is not PartResidualProductContract
            or PART_RESIDUAL_PRODUCT_CONTRACTS.get(self.contract.operation) != self.contract
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        result: object,
        receipt: PartCoreConformanceReceipt,
    ) -> None:
        try:
            shape = result.Shape
            volume = float(shape.Volume)
            solids = len(shape.Solids)
            edges = len(shape.Edges)
            valid = (
                type(receipt) is PartCoreConformanceReceipt
                and receipt.operation is self.contract.operation
                and result.Document is document
                and document.getObject(receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.contract.native_type_id
                and result.isValid() is True
                and tuple(result.State) == ("Up-to-date",)
                and shape.isNull() is False
                and shape.isValid() is True
                and math.isfinite(volume)
                and hmac.compare_digest(_shape_sha256(result), receipt.result_shape_sha256)
            )
            if self.contract.operation is PartCoreOperation.SECTION:
                valid = valid and solids == 0 and edges >= 1 and abs(volume) <= 1e-8
            elif self.contract.operation is PartCoreOperation.COMPOUND:
                valid = (
                    valid
                    and str(shape.ShapeType) == "Compound"
                    and len(shape.childShapes()) == len(receipt.source_shape_sha256s)
                )
            elif self.contract.operation is PartCoreOperation.REVERSE:
                valid = valid and solids == 1 and volume < 0.0
            else:
                valid = valid and solids == 1 and volume > 0.0
        except (Exception, SystemExit, AttributeError, TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            _integrity_failure()


PART_RESIDUAL_RESULT_INVARIANTS: Final = MappingProxyType(
    {
        operation: PartResidualResultInvariant(contract=contract)
        for operation, contract in PART_RESIDUAL_PRODUCT_CONTRACTS.items()
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartResidualOwnershipClosure:
    """Content-bound singleton result and ordered source closure."""

    invariant: PartResidualResultInvariant
    native_receipt: PartCoreConformanceReceipt
    plan_content_sha256: str
    source_identity_sha256s: tuple[str, ...]
    source_state_sha256s: tuple[str, ...]
    semantic_role: SemanticRole = SemanticRole.FEATURE
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        contract = getattr(self.invariant, "contract", None)
        count = len(getattr(self.native_receipt, "source_shape_sha256s", ()))
        if (
            type(self.invariant) is not PartResidualResultInvariant
            or type(self.native_receipt) is not PartCoreConformanceReceipt
            or type(contract) is not PartResidualProductContract
            or self.native_receipt.operation is not contract.operation
            or not contract.accepts(count)
            or not _is_sha256(self.plan_content_sha256)
            or type(self.source_identity_sha256s) is not tuple
            or type(self.source_state_sha256s) is not tuple
            or len(self.source_identity_sha256s) != count
            or len(self.source_state_sha256s) != count
            or any(
                not _is_sha256(item)
                for item in (*self.source_identity_sha256s, *self.source_state_sha256s)
            )
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()
        body = {
            "native_receipt_sha256": self.native_receipt.receipt_sha256,
            "plan_content_sha256": self.plan_content_sha256,
            "result_kind": contract.result_kind.value,
            "semantic_role": self.semantic_role.value,
            "source_identity_sha256s": list(self.source_identity_sha256s),
            "source_state_sha256s": list(self.source_state_sha256s),
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_CLOSURE_DOMAIN + _canonical(body)).hexdigest(),
        )

    @property
    def operation(self) -> PartCoreOperation:
        return self.native_receipt.operation

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def source_shape_sha256s(self) -> tuple[str, ...]:
        return self.native_receipt.source_shape_sha256s

    @property
    def result_shape_sha256(self) -> str:
        return self.native_receipt.result_shape_sha256

    def validate_native_result(self, document: object, result: object) -> None:
        self.invariant.validate_native_result(document, result, self.native_receipt)


def execute_part_residual_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Execute one residual operation after the final source revalidation."""

    if (
        document is None
        or type(plan) is not PartCoreBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or plan.operation not in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS
        or operation != _OPERATIONS_BY_ID.get(plan.operation.value)
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

    bindings = build_part_residual_reviewed_bindings(document, plan, operation, context)
    validate_part_residual_bindings_current(document, plan, operation, context, bindings)
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
    except (Exception, SystemExit, AttributeError):
        _integrity_failure()
    added = tuple(item for item in after if not any(item is existing for existing in before))
    if (
        type(receipt) is not PartCoreConformanceReceipt
        or receipt.operation is not plan.operation
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.source_shape_sha256s != bindings.source_shape_sha256s
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
    ):
        _integrity_failure()
    # Native execution must not mutate any authenticated source closure.
    validate_part_residual_bindings_current(document, plan, operation, context, bindings)
    closure = PartResidualOwnershipClosure(
        invariant=PART_RESIDUAL_RESULT_INVARIANTS[plan.operation],
        native_receipt=receipt,
        plan_content_sha256=plan_document.content_sha256,
        source_identity_sha256s=bindings.source_identity_sha256s,
        source_state_sha256s=bindings.source_state_sha256s,
    )
    closure.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=closure)


@dataclass(frozen=True, slots=True)
class PartResidualReviewedFamilySpec:
    """Arguments for constructing the unregistered shared family descriptor."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]
    minimum_sources: int
    maximum_sources: int


PART_RESIDUAL_REVIEWED_FAMILY_SPEC: Final = PartResidualReviewedFamilySpec(
    manifest=PART_CORE_MANIFEST,
    subject_type_term=_bridge_term(PART_CORE_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_residual_reviewed_adapter_factory,
    validate_plan=validate_part_residual_reviewed_plan,
    execute_plan=execute_part_residual_reviewed_plan,
    minimum_sources=1,
    maximum_sources=_MAX_PRODUCT_SOURCES,
)


def build_part_residual_reviewed_family_descriptor() -> object:
    """Return the private descriptor handoff without registering it as current."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedIntentFamilyDescriptor,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    results = tuple(
        _ReviewedProductResultContract(
            operation_id=contract.operation.value,
            result_kind=(
                _ReviewedProductResultKind.SOLID
                if contract.result_kind is PartResidualProductResultKind.SOLID
                else _ReviewedProductResultKind.VALID_SHAPE
            ),
            owned_type_ids=(contract.native_type_id,),
            semantic_roles=(contract.semantic_role,),
            source_count=count,
        )
        for contract in PART_RESIDUAL_PRODUCT_CONTRACTS.values()
        for count in range(contract.minimum_sources, contract.maximum_sources + 1)
    )
    return _ReviewedIntentFamilyDescriptor(
        manifest=PART_RESIDUAL_REVIEWED_FAMILY_SPEC.manifest,
        subject_type_term=PART_RESIDUAL_REVIEWED_FAMILY_SPEC.subject_type_term,
        adapter_factory=PART_RESIDUAL_REVIEWED_FAMILY_SPEC.adapter_factory,
        validate_plan=PART_RESIDUAL_REVIEWED_FAMILY_SPEC.validate_plan,
        execute_plan=PART_RESIDUAL_REVIEWED_FAMILY_SPEC.execute_plan,
        product_results=results,
        minimum_sources=PART_RESIDUAL_REVIEWED_FAMILY_SPEC.minimum_sources,
        maximum_sources=PART_RESIDUAL_REVIEWED_FAMILY_SPEC.maximum_sources,
        requires_same_run_sources=True,
    )


__all__ = [
    "PART_RESIDUAL_PRODUCT_CONTRACTS",
    "PART_RESIDUAL_RESULT_INVARIANTS",
    "PART_RESIDUAL_REVIEWED_FAMILY_SPEC",
    "PART_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES",
    "PART_RESIDUAL_REVIEWED_PRODUCT_OPERATIONS",
    "AuthenticatedReviewedPartResidualBindings",
    "PartResidualOwnershipClosure",
    "PartResidualProductContract",
    "PartResidualProductResultKind",
    "PartResidualResultInvariant",
    "PartResidualReviewedFamilySpec",
    "build_part_residual_reviewed_bindings",
    "build_part_residual_reviewed_family_descriptor",
    "execute_part_residual_reviewed_plan",
    "part_residual_reviewed_adapter_factory",
    "resolve_part_residual_reviewed_operation",
    "validate_part_residual_bindings_current",
    "validate_part_residual_reviewed_plan",
]
