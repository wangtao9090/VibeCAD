"""Private Reviewed product callbacks for three PartDesign Boolean semantics.

The family consumes exactly two ordered same-run products: the current Tip of
the target Body and the current Tip of one distinct external tool Body.  The
existing adapter binds both semantic Body/node/result identities into the
canonical plan; the existing native rule alone owns the TypeId, property, and
Fuse/Cut/Common enumeration mapping.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef, SubjectRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_partdesign_boolean_adapter import (
    BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM,
    BOOLEAN_CAPABILITY_SCHEMA_TERM,
    BOOLEAN_INTENT_DOCUMENT_ROLE_TERM,
    BOOLEAN_OPERATION_TERMS,
    BOOLEAN_PLAN_DOCUMENT_ROLE_TERM,
    BOOLEAN_PLAN_SCHEMA_TERM,
    BOOLEAN_REQUEST_TERMS,
    BOOLEAN_STRUCTURE_TERM,
    FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR,
    _build_plan,
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
from vibecad.parametric.feature_graph_v2 import (
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_boolean_rules import (
    MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
    PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID,
    PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE,
    PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
    PARTDESIGN_BOOLEAN_RULE_ID,
    AuthenticatedBooleanOperand,
    BooleanOperandSelection,
    PartDesignBooleanBackendPlan,
    PartDesignBooleanConformanceReceipt,
    PartDesignBooleanExecutionBindings,
    PartDesignBooleanOperation,
    apply_partdesign_boolean_plan,
    decode_partdesign_boolean_backend_plan,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.partdesign-boolean-ownership.v1\0"
_MANIFEST_BUILD_ID: Final = hashlib.sha256(
    b"FreeCAD-build\0" + PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()


def _integrity_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS: Final = tuple(PartDesignBooleanOperation)
PARTDESIGN_BOOLEAN_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=terms.operation.value,
        semantic_term=_bridge_term(terms.operation_term),
        native_type_id=PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID,
        native_operation="apply_partdesign_boolean_plan",
        native_property_names=("BaseFeature", "Group", "Refine", "Type"),
    )
    for terms in BOOLEAN_OPERATION_TERMS
)

PARTDESIGN_BOOLEAN_MANIFEST: Final = FamilyBatchManifest(
    family_id="partdesign.boolean",
    family_version="1.0.0",
    adapter=FREECAD_PARTDESIGN_BOOLEAN_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_MANIFEST_BUILD_ID,
    rule_id=PARTDESIGN_BOOLEAN_RULE_ID,
    rule_contract_sha256=PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
    intent_role_term=BOOLEAN_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=BOOLEAN_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=BOOLEAN_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-partdesign-boolean-capability+json",
    plan_role_term=BOOLEAN_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=BOOLEAN_PLAN_SCHEMA_TERM,
    plan_media_type=PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE,
    request_terms=BOOLEAN_REQUEST_TERMS,
    operations=PARTDESIGN_BOOLEAN_OPERATION_SPECS,
    max_plan_bytes=MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES,
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PARTDESIGN_BOOLEAN_MANIFEST.operations}
)
PARTDESIGN_BOOLEAN_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(
    (
        f"{PARTDESIGN_BOOLEAN_MANIFEST.family_id}.{operation.value}",
        _OPERATIONS_BY_ID[operation.value].semantic_term.term_id,
    )
    for operation in PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS
)


class PartDesignBooleanSourceRole(StrEnum):
    TARGET_BASE = "target_base"
    EXTERNAL_TOOL = "external_tool"


PARTDESIGN_BOOLEAN_REQUIRED_SOURCE_ROLES: Final = MappingProxyType(
    {
        operation.value: (
            PartDesignBooleanSourceRole.TARGET_BASE,
            PartDesignBooleanSourceRole.EXTERNAL_TOOL,
        )
        for operation in PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS
    }
)


def resolve_partdesign_boolean_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    try:
        index = PARTDESIGN_BOOLEAN_REVIEWED_PRODUCT_IDENTITIES.index(
            (operation_id, semantic_operation)
        )
    except ValueError:
        return None
    operation = PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS[index]
    return _OPERATIONS_BY_ID[operation.value]


def _build_reviewed_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    if manifest is not PARTDESIGN_BOOLEAN_MANIFEST:
        _integrity_failure()
    try:
        graph = decode_parametric_feature_graph_v2(
            payload,
            expected_sha256=document.document_digest,
        )
        plan, subject = _build_plan(document, payload, graph, request_digest)
    except (Exception, SystemExit):
        _integrity_failure()
    operation = _OPERATIONS_BY_ID.get(plan.operation.value)
    if operation is None or type(subject) is not SubjectRef:
        _integrity_failure()
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=operation.semantic_term,
        subjects=(subject,),
    )


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartDesignBooleanBackendPlan:
    if (
        type(plan) is not PartDesignBooleanBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PARTDESIGN_BOOLEAN_MANIFEST.operations
        or plan.operation.value != operation.operation_id
        or plan.adapter_contract_sha256
        != PARTDESIGN_BOOLEAN_MANIFEST.adapter.adapter_contract_sha256
        or len(plan.tools) != 1
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    try:
        decoded = decode_partdesign_boolean_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_partdesign_boolean_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(receipt) is not ReviewedPlanReceipt
        or receipt.operation != operation
        or receipt.manifest_sha256 != PARTDESIGN_BOOLEAN_MANIFEST.manifest_sha256
        or receipt.adapter != PARTDESIGN_BOOLEAN_MANIFEST.adapter
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


def partdesign_boolean_reviewed_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        PARTDESIGN_BOOLEAN_MANIFEST,
        sink,
        build_plan=_build_reviewed_plan,
        decode_plan=decode_partdesign_boolean_backend_plan,
        validate_binding=validate_partdesign_boolean_reviewed_plan,
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
    except (Exception, SystemExit):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def _valid_solid(item: object) -> bool:
    try:
        shape = item.Shape
        return (
            item.isValid() is True
            and tuple(item.State) == ("Up-to-date",)
            and shape.isNull() is False
            and shape.isValid() is True
            and len(shape.Solids) == 1
            and math.isfinite(float(shape.Volume))
            and float(shape.Volume) > 0.0
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _body_for_object(document: object, item: object) -> object:
    try:
        bodies = tuple(
            body
            for body in tuple(document.Objects)
            if getattr(body, "TypeId", None) == "PartDesign::Body"
            and any(item is child for child in tuple(body.Group))
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if len(bodies) != 1:
        _integrity_failure()
    return bodies[0]


def _selection_receipt(
    source: object,
    selection: BooleanOperandSelection,
) -> None:
    receipt = source.native_receipt
    if (
        getattr(receipt, "body_id", None) != selection.body_id
        or getattr(receipt, "node_id", None) != selection.node_id
        or getattr(receipt, "result_id", None) != selection.result_id
    ):
        _integrity_failure()


def _authenticate_source(
    document: object,
    session: object,
    source: object,
    selection: BooleanOperandSelection,
    current_routes: tuple[object, ...],
    document_objects: tuple[object, ...],
) -> tuple[object, object]:
    try:
        identity = session.read_object_identity(source.object)
    except (AttributeError, KeyError, TypeError, ValueError):
        _integrity_failure()
    item = source.object
    receipt = source.native_receipt
    receipt_operation = getattr(receipt, "operation", None)
    expected_shape_sha256 = getattr(receipt, "result_shape_sha256", None)
    if (
        type(identity) is not EntityIdentity
        or not any(source.route is route for route in current_routes)
        or source.route.operation not in source.route.manifest.operations
        or getattr(receipt_operation, "value", None) != source.route.operation.operation_id
        or getattr(receipt, "plan_sha256", None) != source.plan_sha256
        or getattr(receipt, "plan_content_sha256", None) != source.plan_content_sha256
        or getattr(receipt, "object_name", None) != getattr(item, "Name", None)
        or not _is_sha256(expected_shape_sha256)
        or not hmac.compare_digest(_shape_sha256(item), expected_shape_sha256)
        or getattr(item, "Document", None) is not document
        or not any(item is existing for existing in document_objects)
        or getattr(item, "TypeId", None) != source.route.operation.native_type_id
        or identity.object_type != source.route.operation.native_type_id
        or identity.feature_id is None
        or identity.semantic_role is not SemanticRole.FEATURE
        or identity.provenance.source is not ProvenanceSource.MODEL
        or identity.provenance.operation_id != "apply_reviewed_intent"
        or source.semantic_roles != (SemanticRole.FEATURE,)
        or source.result_kind.value != "solid"
        or not _valid_solid(item)
    ):
        _integrity_failure()
    _selection_receipt(source, selection)
    body = _body_for_object(document, item)
    try:
        if body.Tip is not item:
            raise ValueError
    except (AttributeError, ValueError):
        _integrity_failure()
    return item, body


def _authenticated_bindings(
    document: object,
    plan: PartDesignBooleanBackendPlan,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> PartDesignBooleanExecutionBindings:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
    )

    if (
        session is None
        or type(source_results) is not tuple
        or len(source_results) != 2
        or any(type(item) is not ReviewedNativeExecutionResult for item in source_results)
    ):
        _integrity_failure()
    try:
        if session.doc is not document or not callable(session.read_object_identity):
            raise ValueError
        document_objects = tuple(document.Objects)
    except (AttributeError, TypeError, ValueError):
        _integrity_failure()
    selections = (plan.base, plan.tools[0])
    authenticated = tuple(
        _authenticate_source(
            document,
            session,
            source,
            selection,
            CURRENT_REVIEWED_INTENT_ROUTES,
            document_objects,
        )
        for source, selection in zip(source_results, selections, strict=True)
    )
    (base, target_body), (tool, tool_body) = authenticated
    if base is tool or target_body is tool_body:
        _integrity_failure()
    return PartDesignBooleanExecutionBindings(
        document=document,
        target_body=target_body,
        target_body_id=plan.body_id,
        base=AuthenticatedBooleanOperand(
            object=base,
            body=target_body,
            body_id=plan.base.body_id,
            node_id=plan.base.node_id,
            result_id=plan.base.result_id,
        ),
        tools=(
            AuthenticatedBooleanOperand(
                object=tool,
                body=tool_body,
                body_id=plan.tools[0].body_id,
                node_id=plan.tools[0].node_id,
                result_id=plan.tools[0].result_id,
            ),
        ),
    )


def _volume_effect(receipt: PartDesignBooleanConformanceReceipt) -> bool:
    base = receipt.base_volume_mm3
    tool = receipt.tool_volumes_mm3[0]
    result = receipt.result_volume_mm3
    epsilon = max(1e-8, max(base, tool) * 1e-10)
    if receipt.operation is PartDesignBooleanOperation.FUSE:
        return result > base + epsilon and result <= base + tool + epsilon
    if receipt.operation is PartDesignBooleanOperation.CUT:
        return epsilon < result < base - epsilon
    return epsilon < result < base - epsilon and result <= tool + epsilon


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignBooleanOwnershipClosure:
    native_receipt: PartDesignBooleanConformanceReceipt
    body_id: str
    node_id: str
    result_id: str
    plan_content_sha256: str
    result_shape_sha256: str
    native_type_id: str = PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID
    semantic_role: SemanticRole = SemanticRole.FEATURE
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.native_receipt) is not PartDesignBooleanConformanceReceipt:
            _integrity_failure()
        if (
            self.native_receipt.operation not in PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS
            or self.native_type_id != PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID
            or self.semantic_role is not SemanticRole.FEATURE
            or any(
                type(item) is not str or not item
                for item in (self.body_id, self.node_id, self.result_id)
            )
            or not _is_sha256(self.plan_content_sha256)
            or not _is_sha256(self.result_shape_sha256)
            or not _volume_effect(self.native_receipt)
        ):
            _integrity_failure()
        digest = hashlib.sha256(
            b"\0".join(
                (
                    _OWNERSHIP_DIGEST_DOMAIN,
                    self.native_receipt.receipt_sha256.encode("ascii"),
                    self.body_id.encode("ascii"),
                    self.node_id.encode("ascii"),
                    self.result_id.encode("ascii"),
                    self.plan_content_sha256.encode("ascii"),
                    self.result_shape_sha256.encode("ascii"),
                    self.native_type_id.encode("ascii"),
                    self.semantic_role.value.encode("ascii"),
                )
            )
        ).hexdigest()
        object.__setattr__(self, "receipt_sha256", digest)

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def operation(self) -> PartDesignBooleanOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    def validate_native_result(self, document: object, result: object) -> None:
        try:
            valid = (
                getattr(result, "Document", None) is document
                and document.getObject(self.object_name) is result
                and getattr(result, "Name", None) == self.object_name
                and getattr(result, "TypeId", None) == self.native_type_id
                and _valid_solid(result)
                and hmac.compare_digest(_shape_sha256(result), self.result_shape_sha256)
                and math.isclose(
                    float(result.Shape.Volume),
                    self.native_receipt.result_volume_mm3,
                    rel_tol=0.0,
                    abs_tol=max(1e-9, self.native_receipt.result_volume_mm3 * 1e-12),
                )
                and _volume_effect(self.native_receipt)
            )
        except (Exception, SystemExit, TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adoption(
        self,
        document: object,
        result: object,
        observation: object,
    ) -> None:
        self.validate_native_result(document, result)
        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != self.native_type_id
            or observation.semantic_role != self.semantic_role.value
            or observation.valid_shape is not True
            or observation.solid_count != 1
            or observation.volume_mm3 is None
            or observation.volume_mm3 <= 0.0
        ):
            _integrity_failure()


def execute_partdesign_boolean_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> object:
    if document is None or type(payload) is not bytes:
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_partdesign_boolean_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    bindings = _authenticated_bindings(
        document,
        checked,
        source_results,
        session=session,
    )
    before = tuple(document.Objects)
    target_group = tuple(bindings.target_body.Group)
    target_tip = bindings.target_body.Tip
    tool_body = bindings.tools[0].body
    tool_group = tuple(tool_body.Group)
    tool_tip = tool_body.Tip
    tool = bindings.tools[0].object
    tool_shape_sha256 = _shape_sha256(tool)
    tool_volume = float(tool.Shape.Volume)
    tool_name = getattr(tool, "Name", None)
    tool_type_id = getattr(tool, "TypeId", None)
    receipt = apply_partdesign_boolean_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=bindings,
    )
    try:
        after = tuple(document.Objects)
        result = document.getObject(receipt.object_name)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(receipt) is not PartDesignBooleanConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.plan_sha256 != checked.plan_sha256
        or not _volume_effect(receipt)
        or len(after) != len(before) + 1
        or any(
            actual is not expected
            for actual, expected in zip(after[: len(before)], before, strict=True)
        )
        or result is not after[-1]
        or tuple(bindings.target_body.Group) != (*target_group, result)
        or target_tip is not bindings.base.object
        or bindings.target_body.Tip is not result
        or result.BaseFeature is not bindings.base.object
        or tuple(result.Group) != (tool_body,)
        or tuple(tool_body.Group) != tool_group
        or tool_body.Tip is not tool_tip
        or tool_tip is not tool
        or getattr(tool, "Document", None) is not document
        or getattr(tool, "Name", None) != tool_name
        or getattr(tool, "TypeId", None) != tool_type_id
        or not _valid_solid(tool)
        or not hmac.compare_digest(_shape_sha256(tool), tool_shape_sha256)
        or not math.isclose(
            float(tool.Shape.Volume),
            tool_volume,
            rel_tol=0.0,
            abs_tol=max(1e-9, tool_volume * 1e-12),
        )
        or getattr(result, "TypeId", None) != PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID
        or not _valid_solid(result)
    ):
        _integrity_failure()
    owned_receipt = PartDesignBooleanOwnershipClosure(
        native_receipt=receipt,
        body_id=checked.body_id,
        node_id=checked.node_id,
        result_id=checked.result_id,
        plan_content_sha256=plan_document.content_sha256,
        result_shape_sha256=_shape_sha256(result),
    )
    owned_receipt.validate_native_result(document, result)
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=owned_receipt)


def execute_partdesign_boolean_reviewed_plan(
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

    if type(context) is not _ReviewedFamilyExecutionContext or context.document is not document:
        _integrity_failure()
    return execute_partdesign_boolean_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


@dataclass(frozen=True, slots=True)
class PartDesignBooleanReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    adapter_factory: object = field(repr=False, compare=False)
    validate_plan: object = field(repr=False, compare=False)
    execute_plan: object = field(repr=False, compare=False)
    operation_ids: tuple[str, ...]
    minimum_sources: int
    maximum_sources: int

    def __post_init__(self) -> None:
        if (
            self.manifest is not PARTDESIGN_BOOLEAN_MANIFEST
            or self.subject_type_term != _bridge_term(BOOLEAN_STRUCTURE_TERM)
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
            or self.operation_ids
            != tuple(item.value for item in PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS)
            or self.minimum_sources != 2
            or self.maximum_sources != 2
        ):
            _integrity_failure()


PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC: Final = PartDesignBooleanReviewedFamilySpec(
    manifest=PARTDESIGN_BOOLEAN_MANIFEST,
    subject_type_term=_bridge_term(BOOLEAN_STRUCTURE_TERM),
    adapter_factory=partdesign_boolean_reviewed_adapter_factory,
    validate_plan=validate_partdesign_boolean_reviewed_plan,
    execute_plan=execute_partdesign_boolean_reviewed_plan,
    operation_ids=tuple(item.value for item in PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS),
    minimum_sources=2,
    maximum_sources=2,
)


__all__ = [
    "PARTDESIGN_BOOLEAN_MANIFEST",
    "PARTDESIGN_BOOLEAN_OPERATION_SPECS",
    "PARTDESIGN_BOOLEAN_REQUIRED_SOURCE_ROLES",
    "PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC",
    "PARTDESIGN_BOOLEAN_REVIEWED_OPERATIONS",
    "PARTDESIGN_BOOLEAN_REVIEWED_PRODUCT_IDENTITIES",
    "PartDesignBooleanOwnershipClosure",
    "PartDesignBooleanReviewedFamilySpec",
    "PartDesignBooleanSourceRole",
    "execute_partdesign_boolean_reviewed_plan",
    "execute_partdesign_boolean_reviewed_plan_with_sources",
    "partdesign_boolean_reviewed_adapter_factory",
    "resolve_partdesign_boolean_reviewed_operation",
    "validate_partdesign_boolean_reviewed_plan",
]
