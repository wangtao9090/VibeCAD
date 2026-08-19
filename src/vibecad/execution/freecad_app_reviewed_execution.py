"""Private product callbacks for reviewed FreeCAD App document objects.

The existing App family rule owns all native creation and rollback.  This
module adds only the product-side closure and dependency contracts needed by
the shared Reviewed dispatcher.  Related objects come exclusively from the
executor's opaque same-run result table; graph input never supplies a native
name, TypeId, property, expression, path, or callable.
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
from vibecad.intent_bridge.freecad_app_family_adapter import (
    APP_FAMILY_MANIFEST,
    APP_FAMILY_STRUCTURE_TERM,
    FreeCADAppFamilyAdapter,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_app_family_rules import (
    APP_FAMILY_NATIVE_TYPE_IDS,
    APP_FAMILY_RELATION_KINDS,
    AppFamilyBackendPlan,
    AppFamilyConformanceReceipt,
    AppFamilyExecutionBindings,
    AppFamilyOperation,
    AppFamilyRelationKind,
    apply_app_family_plan,
    decode_app_family_backend_plan,
)
from vibecad.validation.contracts import EntityObservation

_STATE_DOMAIN = b"vibecad.reviewed-app-product-state.v1\0"
_RECEIPT_DOMAIN = b"vibecad.reviewed-app-product-receipt.v1\0"
_PART_OWNED_TYPE_IDS: Final = (
    "App::Part",
    "App::Origin",
    "App::Line",
    "App::Line",
    "App::Line",
    "App::Plane",
    "App::Plane",
    "App::Plane",
    "App::Point",
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
    # Lazy import prevents a cycle while the shared dispatcher constructs the
    # descriptor backed by this module.
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


class AppReviewedResultKind(StrEnum):
    """Closed App-family result kind; no shape or solid claim is implied."""

    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class AppReviewedProductContract:
    """Static source, ownership, and adoption contract for one App semantic."""

    operation: AppFamilyOperation
    minimum_sources: int
    maximum_sources: int
    result_kind: AppReviewedResultKind
    owned_type_ids: tuple[str, ...]
    semantic_roles: tuple[SemanticRole, ...]
    ownership: str

    def __post_init__(self) -> None:
        expected_sources = (
            0 if APP_FAMILY_RELATION_KINDS[self.operation] is AppFamilyRelationKind.NONE else 1
        )
        if (
            type(self.operation) is not AppFamilyOperation
            or type(self.minimum_sources) is not int
            or type(self.maximum_sources) is not int
            or self.minimum_sources != expected_sources
            or self.maximum_sources != expected_sources
            or type(self.result_kind) is not AppReviewedResultKind
            or self.result_kind is not AppReviewedResultKind.REFERENCE
            or type(self.owned_type_ids) is not tuple
            or not self.owned_type_ids
            or self.owned_type_ids[0] != APP_FAMILY_NATIVE_TYPE_IDS[self.operation]
            or type(self.semantic_roles) is not tuple
            or len(self.semantic_roles) != len(self.owned_type_ids)
            or any(type(item) is not SemanticRole for item in self.semantic_roles)
            or self.ownership != "document-root"
        ):
            _integrity_failure()

    @property
    def operation_id(self) -> str:
        return self.operation.value


def _contract(operation: AppFamilyOperation) -> AppReviewedProductContract:
    owned_type_ids = (
        _PART_OWNED_TYPE_IDS
        if operation is AppFamilyOperation.POSITIONED_PART
        else (APP_FAMILY_NATIVE_TYPE_IDS[operation],)
    )
    roles = (
        (SemanticRole.PART, *(SemanticRole.SUPPORT for _ in range(8)))
        if operation is AppFamilyOperation.POSITIONED_PART
        else (SemanticRole.SUPPORT,)
    )
    source_count = 0 if APP_FAMILY_RELATION_KINDS[operation] is AppFamilyRelationKind.NONE else 1
    return AppReviewedProductContract(
        operation=operation,
        minimum_sources=source_count,
        maximum_sources=source_count,
        result_kind=AppReviewedResultKind.REFERENCE,
        owned_type_ids=owned_type_ids,
        semantic_roles=roles,
        ownership="document-root",
    )


APP_REVIEWED_PRODUCT_CONTRACTS: Final = MappingProxyType(
    {operation: _contract(operation) for operation in AppFamilyOperation}
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in APP_FAMILY_MANIFEST.operations}
)
APP_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(
    (
        f"{APP_FAMILY_MANIFEST.family_id}.{operation.value}",
        _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
    )
    for operation in AppFamilyOperation
)


def resolve_app_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve one complete static identity without accepting aliases."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    try:
        index = APP_REVIEWED_PRODUCT_IDENTITIES.index((operation_id, semantic_operation))
    except ValueError:
        return None
    return _OPERATIONS_BY_ID[tuple(AppFamilyOperation)[index].value]


def app_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return FreeCADAppFamilyAdapter(sink)


def validate_app_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind one canonical App plan to its exact static product contract."""

    if (
        type(plan) is not AppFamilyBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or operation not in APP_FAMILY_MANIFEST.operations
        or receipt.operation != operation
        or receipt.manifest_sha256 != APP_FAMILY_MANIFEST.manifest_sha256
        or receipt.adapter != APP_FAMILY_MANIFEST.adapter
        or plan.operation.value != operation.operation_id
        or plan.operation not in APP_REVIEWED_PRODUCT_CONTRACTS
        or plan.adapter_contract_sha256 != APP_FAMILY_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != APP_FAMILY_MANIFEST.manifest_sha256
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
    contract = APP_REVIEWED_PRODUCT_CONTRACTS[plan.operation]
    has_relation = plan.related_node_id is not None and plan.related_result_id is not None
    if has_relation != (contract.minimum_sources == 1):
        _integrity_failure()
    try:
        decoded = decode_app_family_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=receipt.plan_document.content_sha256,
            expected_plan_sha256=receipt.plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()


def _canonical_json(value: object) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (Exception, SystemExit):
        _integrity_failure()
    if len(payload) > 64 * 1024:
        _integrity_failure()
    return payload


def _finite(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        _integrity_failure()
    if not math.isfinite(result):
        _integrity_failure()
    return result


def _vector(value: object) -> list[float]:
    try:
        result = [_finite(item) for item in tuple(value)]
    except (Exception, SystemExit):
        _integrity_failure()
    if len(result) != 3:
        _integrity_failure()
    return result


def _placement(value: object) -> list[float]:
    try:
        result = [
            _finite(value.Base.x),
            _finite(value.Base.y),
            _finite(value.Base.z),
            *(_finite(item) for item in tuple(value.Rotation.Q)),
        ]
    except (Exception, SystemExit):
        _integrity_failure()
    if len(result) != 7:
        _integrity_failure()
    return result


def _object_ref(value: object) -> dict[str, str]:
    try:
        name = value.Name
        type_id = value.TypeId
    except (Exception, SystemExit):
        _integrity_failure()
    if type(name) is not str or not name or type(type_id) is not str or not type_id:
        _integrity_failure()
    return {"name": name, "type_id": type_id}


def _state_mapping(
    document: object,
    primary: object,
    owned: tuple[object, ...],
    operation: AppFamilyOperation,
) -> dict[str, object]:
    contract = APP_REVIEWED_PRODUCT_CONTRACTS[operation]
    try:
        document_objects = tuple(document.Objects)
        state = tuple(primary.State)
        expression_engine = tuple(primary.ExpressionEngine)
        parent = primary.getParentGroup()
        valid = primary.isValid()
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(owned) is not tuple
        or len(owned) != len(contract.owned_type_ids)
        or owned[0] is not primary
        or len({id(item) for item in owned}) != len(owned)
        or any(item.Document is not document for item in owned)
        or any(not any(item is actual for actual in document_objects) for item in owned)
        or any(item.isValid() is not True for item in owned)
        or any(tuple(item.State) != ("Up-to-date",) for item in owned)
        or any(
            getattr(item, "TypeId", None) != type_id
            for item, type_id in zip(owned, contract.owned_type_ids, strict=True)
        )
        or parent is not None
        or valid is not True
        or state != ("Up-to-date",)
        or expression_engine
    ):
        _integrity_failure()

    facts: dict[str, object]
    if operation is AppFamilyOperation.TEXT_ANNOTATION:
        facts = {"lines": list(primary.LabelText), "position": _vector(primary.Position)}
    elif operation is AppFamilyOperation.LEADER_ANNOTATION:
        facts = {
            "base_position": _vector(primary.BasePosition),
            "lines": list(primary.LabelText),
            "text_position": _vector(primary.TextPosition),
        }
    elif operation is AppFamilyOperation.DOCUMENT_GROUP:
        facts = {"members": [_object_ref(item) for item in tuple(primary.Group)]}
    elif operation is AppFamilyOperation.OBJECT_LINK:
        facts = {
            "link_placement": _placement(primary.LinkPlacement),
            "link_transform": bool(primary.LinkTransform),
            "linked_object": _object_ref(primary.LinkedObject),
            "placement": _placement(primary.Placement),
        }
    elif operation is AppFamilyOperation.LINK_GROUP:
        facts = {
            "elements": [_object_ref(item) for item in tuple(primary.ElementList)],
            "link_mode": str(primary.LinkMode),
            "placement": _placement(primary.Placement),
        }
    elif operation is AppFamilyOperation.MATERIAL_DEFINITION:
        facts = {"material": dict(primary.Material)}
    elif operation is AppFamilyOperation.POSITIONED_PART:
        try:
            origin = primary.Origin
            helpers = tuple(origin.OriginFeatures)
        except (Exception, SystemExit):
            _integrity_failure()
        if (
            origin is not owned[1]
            or any(
                actual is not expected for actual, expected in zip(helpers, owned[2:], strict=True)
            )
            or origin.getParentGroup() is not primary
            or any(helper.getParentGroup() is not origin for helper in helpers)
        ):
            _integrity_failure()
        facts = {
            "members": [_object_ref(item) for item in tuple(primary.Group)],
            "origin": _object_ref(origin),
            "origin_features": [{**_object_ref(item), "role": str(item.Role)} for item in helpers],
            "placement": _placement(primary.Placement),
        }
    elif operation is AppFamilyOperation.PLACEMENT_REFERENCE:
        facts = {"placement": _placement(primary.Placement)}
    elif operation is AppFamilyOperation.TEXT_DOCUMENT:
        facts = {"text": str(primary.Text)}
    elif operation is AppFamilyOperation.SCALAR_VARIABLE_SET:
        facts = {
            "property_group": str(primary.getGroupOfProperty("Value")),
            "property_type": str(primary.getTypeIdOfProperty("Value")),
            "value": _finite(primary.Value),
        }
    else:  # pragma: no cover - closed enum defensive boundary
        _integrity_failure()
    return {
        "operation": operation.value,
        "owned": [_object_ref(item) for item in owned],
        "primary": _object_ref(primary),
        "state": list(state),
        "facts": facts,
    }


def _state_sha256(
    document: object,
    primary: object,
    owned: tuple[object, ...],
    operation: AppFamilyOperation,
) -> str:
    return hashlib.sha256(
        _STATE_DOMAIN + _canonical_json(_state_mapping(document, primary, owned, operation))
    ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class AppReviewedProductReceipt:
    """Native receipt plus a live, family-owned non-shape state commitment."""

    native_receipt: AppFamilyConformanceReceipt
    state_sha256: str
    owned_type_ids: tuple[str, ...]
    semantic_roles: tuple[SemanticRole, ...]
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.native_receipt) is not AppFamilyConformanceReceipt:
            _integrity_failure()
        contract = APP_REVIEWED_PRODUCT_CONTRACTS[self.native_receipt.operation]
        if (
            type(self.state_sha256) is not str
            or len(self.state_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.state_sha256)
            or self.owned_type_ids != contract.owned_type_ids
            or self.semantic_roles != contract.semantic_roles
        ):
            _integrity_failure()
        body = {
            "native_receipt_sha256": self.native_receipt.receipt_sha256,
            "state_sha256": self.state_sha256,
            "owned_type_ids": list(self.owned_type_ids),
            "semantic_roles": [item.value for item in self.semantic_roles],
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_RECEIPT_DOMAIN + _canonical_json(body)).hexdigest(),
        )

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def operation(self) -> AppFamilyOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def native_type_id(self) -> str:
        return self.native_receipt.native_type_id

    @property
    def owned_object_names(self) -> tuple[str, ...]:
        return self.native_receipt.owned_object_names

    def _owned(self, document: object) -> tuple[object, ...]:
        try:
            owned = tuple(document.getObject(name) for name in self.owned_object_names)
        except (Exception, SystemExit):
            _integrity_failure()
        if any(item is None for item in owned):
            _integrity_failure()
        return owned

    def validate_current(
        self,
        document: object,
        primary: object,
        owned: tuple[object, ...] | None = None,
    ) -> None:
        """Reject a stale, moved, replaced, or closure-substituted App product."""

        actual_owned = self._owned(document) if owned is None else owned
        if (
            type(actual_owned) is not tuple
            or not actual_owned
            or actual_owned[0] is not primary
            or tuple(item.Name for item in actual_owned) != self.owned_object_names
            or not hmac.compare_digest(
                _state_sha256(document, primary, actual_owned, self.operation),
                self.state_sha256,
            )
        ):
            _integrity_failure()

    def validate_adoption(
        self,
        document: object,
        primary: object,
        observation: object,
    ) -> None:
        """Close generic REFERENCE adoption with App-family live facts."""

        contract = APP_REVIEWED_PRODUCT_CONTRACTS[self.operation]
        self.validate_current(document, primary)
        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != contract.owned_type_ids[0]
            or observation.semantic_role != contract.semantic_roles[0].value
            or observation.provenance.get("source") != ProvenanceSource.MODEL.value
            or not observation.provenance.get("operation_id")
        ):
            _integrity_failure()


def _shape_sha256(obj: object) -> str:
    try:
        payload = obj.Shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit):
        _integrity_failure()
    if not payload:
        _integrity_failure()
    return hashlib.sha256(payload).hexdigest()


def _validate_source_result(
    document: object,
    context: object,
    source: object,
) -> object:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
        _ReviewedFamilyExecutionContext,
    )

    if (
        type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or type(source) is not ReviewedNativeExecutionResult
        or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
        or not source.owned_objects
        or source.owned_objects[0] is not source.object
    ):
        _integrity_failure()
    try:
        session = context.session
        obj = source.object
        document_objects = tuple(document.Objects)
        receipt = source.native_receipt
        owned = source.owned_objects
        identities = tuple(session.read_object_identity(item) for item in owned)
        current = (
            session.doc is document
            and obj.Document is document
            and all(item.Document is document for item in owned)
            and all(any(item is actual for actual in document_objects) for item in owned)
            and obj.isValid() is True
            and tuple(obj.State) == ("Up-to-date",)
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        context.run_token is None
        or not source._is_retained_for_run(context.run_token)
        or not current
        or len(identities) != len(owned)
        or any(type(identity) is not EntityIdentity for identity in identities)
        or any(
            identity.object_type != item.TypeId
            or identity.feature_id is None
            or identity.semantic_role is not role
            or identity.provenance.source is not ProvenanceSource.MODEL
            or identity.provenance.operation_id is None
            for identity, item, role in zip(
                identities,
                owned,
                source.semantic_roles,
                strict=True,
            )
        )
        or source.route.operation.native_type_id != obj.TypeId
        or getattr(receipt, "plan_sha256", None) != source.plan_sha256
        or getattr(receipt, "object_name", None) != obj.Name
        or obj.TypeId
        in {"App::Origin", "App::Line", "App::Plane", "App::Point", "App::LinkElement"}
    ):
        _integrity_failure()

    if type(receipt) is AppReviewedProductReceipt:
        receipt.validate_current(document, obj, owned)
    else:
        expected_shape = getattr(receipt, "result_shape_sha256", None)
        if type(expected_shape) is not str or not hmac.compare_digest(
            _shape_sha256(obj), expected_shape
        ):
            _integrity_failure()
    return obj


def build_app_reviewed_bindings(
    document: object,
    plan: object,
    operation: object,
    context: object,
) -> AppFamilyExecutionBindings:
    """Bind exact zero/one PFG relation to an authenticated same-run product."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        document is None
        or type(plan) is not AppFamilyBackendPlan
        or type(operation) is not ReviewedOperationSpec
        or operation not in APP_FAMILY_MANIFEST.operations
        or plan.operation.value != operation.operation_id
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
    ):
        _integrity_failure()
    contract = APP_REVIEWED_PRODUCT_CONTRACTS[plan.operation]
    if len(context.source_results) != contract.minimum_sources:
        _integrity_failure()
    if contract.minimum_sources == 0:
        if plan.related_node_id is not None or plan.related_result_id is not None:
            _integrity_failure()
        return AppFamilyExecutionBindings(document=document, container_id=plan.container_id)

    if (
        plan.related_node_id is None
        or plan.related_result_id is None
        or len(context.source_results) != 1
    ):
        _integrity_failure()
    related = _validate_source_result(document, context, context.source_results[0])
    return AppFamilyExecutionBindings(
        document=document,
        container_id=plan.container_id,
        related_node_id=plan.related_node_id,
        related_result_id=plan.related_result_id,
        related_object=related,
    )


def _body_tips(document: object) -> tuple[tuple[object, object], ...]:
    try:
        return tuple(
            (item, item.Tip) for item in document.Objects if item.TypeId == "PartDesign::Body"
        )
    except (Exception, SystemExit):
        _integrity_failure()


def execute_app_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Execute one CREATE-only App plan and close its exact ownership result."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
        _ReviewedFamilyNativeExecution,
    )

    if (
        document is None
        or type(plan) is not AppFamilyBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in APP_FAMILY_MANIFEST.operations
        or plan.operation.value != operation.operation_id
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
    ):
        _integrity_failure()
    try:
        decoded = decode_app_family_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
        before = tuple(document.Objects)
        body_tips_before = _body_tips(document)
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    bindings = build_app_reviewed_bindings(document, plan, operation, context)
    receipt = apply_app_family_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=bindings,
    )
    contract = APP_REVIEWED_PRODUCT_CONTRACTS[plan.operation]
    try:
        after = tuple(document.Objects)
        owned = tuple(document.getObject(name) for name in receipt.owned_object_names)
        added = after[len(before) :]
        primary = document.getObject(receipt.object_name)
        body_tips_after = _body_tips(document)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(receipt) is not AppFamilyConformanceReceipt
        or receipt.operation is not plan.operation
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.native_type_id != operation.native_type_id
        or len(after) != len(before) + len(contract.owned_type_ids)
        or any(
            actual is not expected
            for actual, expected in zip(after[: len(before)], before, strict=True)
        )
        or len(owned) != len(contract.owned_type_ids)
        or any(item is None for item in owned)
        or any(actual is not expected for actual, expected in zip(added, owned, strict=True))
        or primary is not owned[0]
        or tuple(item.TypeId for item in owned) != contract.owned_type_ids
        or len(body_tips_after) != len(body_tips_before)
        or any(
            actual_body is not expected_body or actual_tip is not expected_tip
            for (actual_body, actual_tip), (expected_body, expected_tip) in zip(
                body_tips_after, body_tips_before, strict=True
            )
        )
    ):
        _integrity_failure()
    product_receipt = AppReviewedProductReceipt(
        native_receipt=receipt,
        state_sha256=_state_sha256(document, primary, owned, plan.operation),
        owned_type_ids=contract.owned_type_ids,
        semantic_roles=contract.semantic_roles,
    )
    return _ReviewedFamilyNativeExecution(
        object=primary,
        receipt=product_receipt,
        owned_objects=owned,
        state_sha256=product_receipt.state_sha256,
    )


@dataclass(frozen=True, slots=True)
class AppReviewedFamilySpec:
    """One source-cardinality-homogeneous descriptor payload for integration."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    product_contracts: tuple[AppReviewedProductContract, ...]
    minimum_sources: int
    maximum_sources: int
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]

    def __post_init__(self) -> None:
        if (
            self.manifest is not APP_FAMILY_MANIFEST
            or self.subject_type_term != _bridge_term(APP_FAMILY_STRUCTURE_TERM)
            or type(self.operation_ids) is not tuple
            or not self.operation_ids
            or type(self.product_contracts) is not tuple
            or tuple(item.operation_id for item in self.product_contracts) != self.operation_ids
            or any(
                item.minimum_sources != self.minimum_sources
                or item.maximum_sources != self.maximum_sources
                for item in self.product_contracts
            )
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
        ):
            _integrity_failure()


_NO_SOURCE_OPERATIONS: Final = tuple(
    operation
    for operation in AppFamilyOperation
    if APP_FAMILY_RELATION_KINDS[operation] is AppFamilyRelationKind.NONE
)
_ONE_SOURCE_OPERATIONS: Final = tuple(
    operation
    for operation in AppFamilyOperation
    if APP_FAMILY_RELATION_KINDS[operation] is not AppFamilyRelationKind.NONE
)


def _family_spec(
    operations: tuple[AppFamilyOperation, ...],
    *,
    source_count: int,
) -> AppReviewedFamilySpec:
    contracts = tuple(APP_REVIEWED_PRODUCT_CONTRACTS[item] for item in operations)
    return AppReviewedFamilySpec(
        manifest=APP_FAMILY_MANIFEST,
        subject_type_term=_bridge_term(APP_FAMILY_STRUCTURE_TERM),
        operation_ids=tuple(item.value for item in operations),
        product_contracts=contracts,
        minimum_sources=source_count,
        maximum_sources=source_count,
        adapter_factory=app_reviewed_adapter_factory,
        validate_plan=validate_app_reviewed_plan,
        execute_plan=execute_app_reviewed_plan,
    )


APP_NO_SOURCE_REVIEWED_FAMILY_SPEC: Final = _family_spec(
    _NO_SOURCE_OPERATIONS,
    source_count=0,
)
APP_ONE_SOURCE_REVIEWED_FAMILY_SPEC: Final = _family_spec(
    _ONE_SOURCE_OPERATIONS,
    source_count=1,
)
APP_REVIEWED_FAMILY_SPECS: Final = (
    APP_NO_SOURCE_REVIEWED_FAMILY_SPEC,
    APP_ONE_SOURCE_REVIEWED_FAMILY_SPEC,
)


__all__ = [
    "APP_NO_SOURCE_REVIEWED_FAMILY_SPEC",
    "APP_ONE_SOURCE_REVIEWED_FAMILY_SPEC",
    "APP_REVIEWED_FAMILY_SPECS",
    "APP_REVIEWED_PRODUCT_CONTRACTS",
    "APP_REVIEWED_PRODUCT_IDENTITIES",
    "AppReviewedFamilySpec",
    "AppReviewedProductContract",
    "AppReviewedProductReceipt",
    "AppReviewedResultKind",
    "app_reviewed_adapter_factory",
    "build_app_reviewed_bindings",
    "execute_app_reviewed_plan",
    "resolve_app_reviewed_operation",
    "validate_app_reviewed_plan",
]
