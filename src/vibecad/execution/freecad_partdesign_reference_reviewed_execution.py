"""Private Reviewed product boundary for five PartDesign reference operations.

The PFG and canonical backend plan never carry a FreeCAD ``FaceN``, ``EdgeN``,
``VertexN``, object name, or label.  This module consumes already-retained
same-run products, authenticates their managed identities, and asks the native
reference rule to resolve one unique live semantic role.  Registration in the
shared CURRENT route table is deliberately out of scope.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vibecad.execution.freecad_partdesign_primitive_reviewed_execution import (
    PartDesignReviewedBaseBinding,
)
from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge.contracts import (
    AdapterDescriptor,
    BridgeTermRef,
    DocumentRef,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_partdesign_reference_adapter import (
    FREECAD_REFERENCE_ADAPTER_DESCRIPTOR,
    REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM,
    REFERENCE_CAPABILITY_SCHEMA_TERM,
    REFERENCE_INTENT_DOCUMENT_ROLE_TERM,
    REFERENCE_OPERATION_TERMS,
    REFERENCE_PLAN_DOCUMENT_ROLE_TERM,
    REFERENCE_PLAN_SCHEMA_TERM,
    REFERENCE_REQUEST_TERMS,
    REFERENCE_STRUCTURE_TERM,
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
from vibecad.parametric.freecad_partdesign_reference_rules import (
    MAX_REFERENCE_PLAN_BYTES,
    REFERENCE_FREECAD_ENGINE_BUILD_ID,
    REFERENCE_PLAN_MEDIA_TYPE,
    REFERENCE_REVIEWED_SELECTION_RULE_CONTRACT_SHA256,
    REFERENCE_REVIEWED_SELECTION_RULE_ID,
    PartDesignReferenceKind,
    PartDesignReferencePlan,
    ReferenceConformanceReceipt,
    ReferenceExecutionBindings,
    ReviewedSubelementSelectionReceipt,
    apply_partdesign_reference_plan,
    decode_partdesign_reference_plan,
    locate_reviewed_reference_subelement,
)

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.partdesign-reference-ownership.v1\0"
_ENTITY_IDENTITY_DIGEST_DOMAIN = b"vibecad.reviewed-entity-identity.v1\0"
_MANIFEST_BUILD_ID: Final = hashlib.sha256(
    b"FreeCAD-build\0" + REFERENCE_FREECAD_ENGINE_BUILD_ID.encode("ascii")
).hexdigest()
_REVIEWED_ADAPTER_CONTRACT_DOMAIN = b"vibecad.freecad-reference-reviewed-adapter.v2\0"
FREECAD_REFERENCE_REVIEWED_ADAPTER_DESCRIPTOR: Final = AdapterDescriptor(
    adapter_id="freecad_partdesign_reference_reviewed_adapter",
    adapter_version="2.0.0",
    adapter_contract_sha256=hashlib.sha256(
        b"\0".join(
            (
                _REVIEWED_ADAPTER_CONTRACT_DOMAIN,
                FREECAD_REFERENCE_ADAPTER_DESCRIPTOR.adapter_contract_sha256.encode("ascii"),
                REFERENCE_REVIEWED_SELECTION_RULE_CONTRACT_SHA256.encode("ascii"),
            )
        )
    ).hexdigest(),
)


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


_NATIVE_OPERATION_CONTRACTS: Final = MappingProxyType(
    {
        PartDesignReferenceKind.DATUM_PLANE: (
            "PartDesign::Plane",
            ("AttachmentSupport", "MapMode"),
        ),
        PartDesignReferenceKind.DATUM_LINE: (
            "PartDesign::Line",
            ("AttachmentSupport", "MapMode"),
        ),
        PartDesignReferenceKind.DATUM_POINT: (
            "PartDesign::Point",
            ("AttachmentSupport", "MapMode"),
        ),
        PartDesignReferenceKind.SHAPE_BINDER: (
            "PartDesign::ShapeBinder",
            ("Support", "TraceSupport"),
        ),
        PartDesignReferenceKind.SUBSHAPE_BINDER: (
            "PartDesign::SubShapeBinder",
            (
                "BindMode",
                "Fuse",
                "MakeFace",
                "PartialLoad",
                "Relative",
                "Support",
            ),
        ),
    }
)

PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS: Final = tuple(PartDesignReferenceKind)
PARTDESIGN_REFERENCE_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=kind.value,
        semantic_term=_bridge_term(REFERENCE_OPERATION_TERMS[kind]),
        native_type_id=_NATIVE_OPERATION_CONTRACTS[kind][0],
        native_operation="apply_partdesign_reference_plan",
        native_property_names=_NATIVE_OPERATION_CONTRACTS[kind][1],
    )
    for kind in PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS
)

PARTDESIGN_REFERENCE_COMPAT_MANIFEST: Final = FamilyBatchManifest(
    family_id="partdesign",
    family_version="1.0.0",
    adapter=FREECAD_REFERENCE_REVIEWED_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=_MANIFEST_BUILD_ID,
    rule_id=REFERENCE_REVIEWED_SELECTION_RULE_ID,
    rule_contract_sha256=REFERENCE_REVIEWED_SELECTION_RULE_CONTRACT_SHA256,
    intent_role_term=REFERENCE_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=REFERENCE_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=REFERENCE_CAPABILITY_SCHEMA_TERM,
    capability_media_type="application/vnd.vibecad.freecad-reference-capability+json",
    plan_role_term=REFERENCE_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=REFERENCE_PLAN_SCHEMA_TERM,
    plan_media_type=REFERENCE_PLAN_MEDIA_TYPE,
    request_terms=REFERENCE_REQUEST_TERMS,
    operations=PARTDESIGN_REFERENCE_OPERATION_SPECS,
    max_plan_bytes=MAX_REFERENCE_PLAN_BYTES,
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PARTDESIGN_REFERENCE_COMPAT_MANIFEST.operations}
)
PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(
    (
        f"{PARTDESIGN_REFERENCE_COMPAT_MANIFEST.family_id}.{kind.value}",
        _OPERATIONS_BY_ID[kind.value].semantic_term.term_id,
    )
    for kind in PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS
)


class PartDesignReferenceSourceRole(StrEnum):
    TARGET_BODY_BASE = "target_body_base"
    SUPPORT = "support"


PARTDESIGN_REFERENCE_REQUIRED_SOURCE_ROLES: Final = MappingProxyType(
    {
        kind.value: (
            (PartDesignReferenceSourceRole.TARGET_BODY_BASE,)
            if kind
            in {
                PartDesignReferenceKind.DATUM_PLANE,
                PartDesignReferenceKind.DATUM_LINE,
                PartDesignReferenceKind.DATUM_POINT,
            }
            else (
                PartDesignReferenceSourceRole.TARGET_BODY_BASE,
                PartDesignReferenceSourceRole.SUPPORT,
            )
        )
        for kind in PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS
    }
)


def resolve_partdesign_reference_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    try:
        index = PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES.index(
            (operation_id, semantic_operation)
        )
    except ValueError:
        return None
    kind = PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS[index]
    return _OPERATIONS_BY_ID[kind.value]


def _build_reviewed_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    if manifest is not PARTDESIGN_REFERENCE_COMPAT_MANIFEST:
        _integrity_failure()
    try:
        graph = decode_parametric_feature_graph_v2(
            payload,
            expected_sha256=document.document_digest,
        )
        legacy_plan, subject = _build_plan(document, graph, request_digest)
        plan = replace(
            legacy_plan,
            adapter_contract_sha256=FREECAD_REFERENCE_REVIEWED_ADAPTER_DESCRIPTOR.adapter_contract_sha256,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    operation = _OPERATIONS_BY_ID.get(plan.kind.value)
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
) -> PartDesignReferencePlan:
    if (
        type(plan) is not PartDesignReferencePlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PARTDESIGN_REFERENCE_COMPAT_MANIFEST.operations
        or plan.kind.value != operation.operation_id
        or plan.adapter_contract_sha256
        != PARTDESIGN_REFERENCE_COMPAT_MANIFEST.adapter.adapter_contract_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    try:
        decoded = decode_partdesign_reference_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_partdesign_reference_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(receipt) is not ReviewedPlanReceipt
        or receipt.operation != operation
        or receipt.manifest_sha256 != PARTDESIGN_REFERENCE_COMPAT_MANIFEST.manifest_sha256
        or receipt.adapter != PARTDESIGN_REFERENCE_COMPAT_MANIFEST.adapter
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


def partdesign_reference_reviewed_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        PARTDESIGN_REFERENCE_COMPAT_MANIFEST,
        sink,
        build_plan=_build_reviewed_plan,
        decode_plan=decode_partdesign_reference_plan,
        validate_binding=validate_partdesign_reference_reviewed_plan,
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


def _identity_sha256(identity: EntityIdentity) -> str:
    if type(identity) is not EntityIdentity:
        _integrity_failure()
    try:
        raw = json.dumps(
            identity.to_mapping(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (Exception, SystemExit):
        _integrity_failure()
    return hashlib.sha256(_ENTITY_IDENTITY_DIGEST_DOMAIN + raw).hexdigest()


def _authenticate_source(
    document: object,
    session: object,
    source: object,
    *,
    run_token: object,
) -> tuple[object, EntityIdentity, object]:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
    )

    if type(source) is not ReviewedNativeExecutionResult or run_token is None:
        _integrity_failure()
    item = source.object
    ownership = source.native_receipt
    try:
        identity = session.read_object_identity(item)
        receipt_operation = ownership.operation
        document_objects = tuple(document.Objects)
        retained = source._is_retained_for_run(run_token)  # noqa: SLF001
    except (Exception, SystemExit):
        _integrity_failure()
    expected_shape_sha256 = getattr(ownership, "result_shape_sha256", None)
    if (
        type(identity) is not EntityIdentity
        or session.doc is not document
        or not retained
        or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
        or source.route.operation not in source.route.manifest.operations
        or getattr(receipt_operation, "value", None) != source.route.operation.operation_id
        or getattr(ownership, "plan_sha256", None) != source.plan_sha256
        or not _is_sha256(getattr(ownership, "receipt_sha256", None))
        or not _is_sha256(expected_shape_sha256)
        or not hmac.compare_digest(_shape_sha256(item), expected_shape_sha256)
        or getattr(item, "Document", None) is not document
        or not any(item is current for current in document_objects)
        or getattr(item, "TypeId", None) != source.route.operation.native_type_id
        or identity.object_type != item.TypeId
        or identity.feature_id is None
        or identity.semantic_role is not SemanticRole.FEATURE
        or identity.provenance.source is not ProvenanceSource.MODEL
        or identity.provenance.operation_id != "apply_reviewed_intent"
        or source.semantic_roles != (SemanticRole.FEATURE,)
        or source.result_kind.value not in {"solid", "valid_shape"}
    ):
        _integrity_failure()
    return item, identity, ownership


def _authenticated_bindings(
    document: object,
    plan: PartDesignReferencePlan,
    plan_document: DocumentRef,
    source_results: tuple[object, ...],
    *,
    session: object,
    run_token: object,
) -> tuple[ReferenceExecutionBindings, object, ReviewedSubelementSelectionReceipt]:
    expected_count = len(PARTDESIGN_REFERENCE_REQUIRED_SOURCE_ROLES[plan.kind.value])
    if (
        session is None
        or getattr(session, "doc", None) is not document
        or type(source_results) is not tuple
        or len(source_results) != expected_count
    ):
        _integrity_failure()
    authenticated = tuple(
        _authenticate_source(document, session, source, run_token=run_token)
        for source in source_results
    )
    target_source = source_results[0]
    target, _target_identity, target_ownership = authenticated[0]
    support_source = source_results[-1]
    support, support_identity, support_ownership = authenticated[-1]
    base_binding = getattr(target_ownership, "partdesign_base_binding", None)
    if type(base_binding) is not PartDesignReviewedBaseBinding or base_binding.object is not target:
        _integrity_failure()
    try:
        base_binding.validate(document)
        body = base_binding.body
        body_identity = session.read_object_identity(body)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(body_identity) is not EntityIdentity
        or body_identity.object_type != "PartDesign::Body"
        or body_identity.feature_id is None
        or body_identity.semantic_role is not SemanticRole.PART
        or body_identity.provenance.source is not ProvenanceSource.MODEL
        or body_identity.provenance.operation_id != "apply_reviewed_intent"
        or (expected_count == 2 and (target is support or target_source is support_source))
        or not hmac.compare_digest(
            plan.support_reference_sha256,
            support_source.plan_content_sha256,
        )
    ):
        _integrity_failure()
    selection = locate_reviewed_reference_subelement(
        plan=plan,
        reference_plan_content_sha256=plan_document.content_sha256,
        source_shape=support.Shape,
        source_plan_sha256=support_source.plan_sha256,
        source_plan_content_sha256=support_source.plan_content_sha256,
        source_native_receipt_sha256=support_ownership.receipt_sha256,
        target_body_entity_identity_sha256=_identity_sha256(body_identity),
        support_entity_identity_sha256=_identity_sha256(support_identity),
    )
    return (
        ReferenceExecutionBindings(
            document=document,
            body=body,
            support=support,
            body_id=plan.body_id,
            support_reference_id=plan.support_reference_id,
            support_reference_sha256=plan.support_reference_sha256,
            target_body_entity_identity_sha256=selection.target_body_entity_identity_sha256,
            support_entity_identity_sha256=selection.support_entity_identity_sha256,
            selection_receipt=selection,
        ),
        support,
        selection,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignReferenceOwnershipClosure:
    native_receipt: ReferenceConformanceReceipt
    body_id: str
    node_id: str
    result_id: str
    plan_content_sha256: str
    selection_receipt_sha256: str
    native_type_id: str
    semantic_role: SemanticRole = SemanticRole.SUPPORT
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        expected_type = _NATIVE_OPERATION_CONTRACTS.get(
            getattr(self.native_receipt, "kind", None),
            (None, ()),
        )[0]
        if (
            type(self.native_receipt) is not ReferenceConformanceReceipt
            or self.native_type_id != expected_type
            or self.semantic_role is not SemanticRole.SUPPORT
            or any(
                type(item) is not str or not item
                for item in (self.body_id, self.node_id, self.result_id)
            )
            or not _is_sha256(self.plan_content_sha256)
            or not _is_sha256(self.selection_receipt_sha256)
            or not hmac.compare_digest(
                self.selection_receipt_sha256,
                self.native_receipt.selection_receipt_sha256,
            )
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
                    self.selection_receipt_sha256.encode("ascii"),
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
    def operation(self) -> PartDesignReferenceKind:
        return self.native_receipt.kind

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name


def execute_partdesign_reference_reviewed_plan_with_sources(
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
    if type(payload) is not bytes:
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_partdesign_reference_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    bindings, support, selection = _authenticated_bindings(
        document,
        checked,
        plan_document,
        source_results,
        session=session,
        run_token=run_token,
    )
    try:
        before = tuple(document.Objects)
        before_group = tuple(bindings.body.Group)
        before_tip = bindings.body.Tip
        support_shape_sha256 = _shape_sha256(support)
    except (Exception, SystemExit):
        _integrity_failure()
    receipt = apply_partdesign_reference_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=bindings,
    )
    try:
        after = tuple(document.Objects)
        result = document.getObject(receipt.object_name)
        after_group = tuple(bindings.body.Group)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(receipt) is not ReferenceConformanceReceipt
        or receipt.kind is not checked.kind
        or receipt.plan_sha256 != checked.plan_sha256
        or receipt.selection_receipt_sha256 != selection.receipt_sha256
        or len(after) != len(before) + 1
        or any(
            current is not original
            for current, original in zip(after[: len(before)], before, strict=True)
        )
        or after[-1] is not result
        or after_group != (*before_group, result)
        or bindings.body.Tip is not before_tip
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
        or not hmac.compare_digest(_shape_sha256(support), support_shape_sha256)
    ):
        _integrity_failure()
    ownership = PartDesignReferenceOwnershipClosure(
        native_receipt=receipt,
        body_id=checked.body_id,
        node_id=checked.node_id,
        result_id=checked.result_id,
        plan_content_sha256=plan_document.content_sha256,
        selection_receipt_sha256=selection.receipt_sha256,
        native_type_id=operation.native_type_id,
    )
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


def execute_partdesign_reference_reviewed_plan(
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
        type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.run_token is None
    ):
        _integrity_failure()
    return execute_partdesign_reference_reviewed_plan_with_sources(
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
class PartDesignReferenceReviewedFamilySpec:
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
            self.manifest is not PARTDESIGN_REFERENCE_COMPAT_MANIFEST
            or self.subject_type_term != _bridge_term(REFERENCE_STRUCTURE_TERM)
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
            or self.operation_ids
            != tuple(kind.value for kind in PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS)
            or self.minimum_sources != 1
            or self.maximum_sources != 2
        ):
            _integrity_failure()


PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC: Final = PartDesignReferenceReviewedFamilySpec(
    manifest=PARTDESIGN_REFERENCE_COMPAT_MANIFEST,
    subject_type_term=_bridge_term(REFERENCE_STRUCTURE_TERM),
    adapter_factory=partdesign_reference_reviewed_adapter_factory,
    validate_plan=validate_partdesign_reference_reviewed_plan,
    execute_plan=execute_partdesign_reference_reviewed_plan,
    operation_ids=tuple(kind.value for kind in PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS),
    minimum_sources=1,
    maximum_sources=2,
)


def build_partdesign_reference_reviewed_family_descriptor() -> object:
    """Build the complete private descriptor without registering CURRENT routes."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFormalSemanticBinding,
        _ReviewedIntentFamilyDescriptor,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    return _ReviewedIntentFamilyDescriptor(
        manifest=PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC.manifest,
        subject_type_term=PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC.subject_type_term,
        adapter_factory=PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC.adapter_factory,
        validate_plan=PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC.validate_plan,
        execute_plan=PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC.execute_plan,
        product_results=tuple(
            _ReviewedProductResultContract(
                operation_id=kind.value,
                result_kind=_ReviewedProductResultKind.REFERENCE,
                owned_type_ids=(_NATIVE_OPERATION_CONTRACTS[kind][0],),
                semantic_roles=(SemanticRole.SUPPORT,),
                source_count=len(PARTDESIGN_REFERENCE_REQUIRED_SOURCE_ROLES[kind.value]),
            )
            for kind in PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS
        ),
        minimum_sources=1,
        maximum_sources=2,
        formal_semantic_binding=_ReviewedFormalSemanticBinding.LEGACY_TERM_ID,
        requires_same_run_sources=True,
    )


__all__ = [
    "FREECAD_REFERENCE_REVIEWED_ADAPTER_DESCRIPTOR",
    "PARTDESIGN_REFERENCE_COMPAT_MANIFEST",
    "PARTDESIGN_REFERENCE_OPERATION_SPECS",
    "PARTDESIGN_REFERENCE_REQUIRED_SOURCE_ROLES",
    "PARTDESIGN_REFERENCE_REVIEWED_FAMILY_SPEC",
    "PARTDESIGN_REFERENCE_REVIEWED_OPERATIONS",
    "PARTDESIGN_REFERENCE_REVIEWED_PRODUCT_IDENTITIES",
    "PartDesignReferenceOwnershipClosure",
    "PartDesignReferenceReviewedFamilySpec",
    "PartDesignReferenceSourceRole",
    "build_partdesign_reference_reviewed_family_descriptor",
    "execute_partdesign_reference_reviewed_plan",
    "execute_partdesign_reference_reviewed_plan_with_sources",
    "partdesign_reference_reviewed_adapter_factory",
    "resolve_partdesign_reference_reviewed_operation",
    "validate_partdesign_reference_reviewed_plan",
]
