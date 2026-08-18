"""Private Reviewed product bridge for source-bound FlatFace Sketch CREATE."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_sketch_flatface_bootstrap_adapter import (
    FLATFACE_SKETCH_BODY_OWNERSHIP_TERM,
    FLATFACE_SKETCH_FAMILY_MANIFEST,
    FLATFACE_SKETCH_OPERATION_SPEC,
    FLATFACE_SKETCH_SELECTOR_TERM,
    FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR,
    flatface_sketch_reviewed_adapter_factory,
    validate_flatface_sketch_reviewed_plan,
)
from vibecad.intent_bridge.reviewed_family_engine import FamilyBatchManifest, ReviewedOperationSpec
from vibecad.parametric import freecad_sketch_flatface_bootstrap_rules as flatface_rules
from vibecad.parametric.freecad_sketch_flatface_bootstrap_rules import (
    FLATFACE_SKETCH_NATIVE_TYPE_ID,
    FlatFaceSelectionEvidence,
    FlatFaceSketchBackendPlan,
    FlatFaceSketchConformanceReceipt,
    FlatFaceSketchExecutionBindings,
    FlatFaceSketchRuleError,
    apply_flatface_sketch_plan,
    decode_flatface_sketch_backend_plan,
    select_unique_zmax_planar_face,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DOMAIN = b"vibecad.flatface-sketch-product-ownership.v1\0"


def _integrity_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _bridge(term: object) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


FLATFACE_SKETCH_REVIEWED_PRODUCT_IDENTITIES: Final = (
    (
        f"{FLATFACE_SKETCH_FAMILY_MANIFEST.family_id}."
        f"{FLATFACE_SKETCH_OPERATION_SPEC.operation_id}",
        _semantic_operation(FLATFACE_SKETCH_OPERATION_SPEC),
    ),
)


@dataclass(frozen=True, slots=True)
class FlatFaceSketchProductContract:
    minimum_sources: int = 1
    maximum_sources: int = 1
    requires_same_run_sources: bool = True
    primary_native_type_id: str = FLATFACE_SKETCH_NATIVE_TYPE_ID

    def __post_init__(self) -> None:
        if (
            self.minimum_sources != 1
            or self.maximum_sources != 1
            or self.requires_same_run_sources is not True
            or self.primary_native_type_id != FLATFACE_SKETCH_NATIVE_TYPE_ID
        ):
            _integrity_failure()


FLATFACE_SKETCH_PRODUCT_CONTRACT: Final = FlatFaceSketchProductContract()
FLATFACE_SKETCH_PRODUCT_CONTRACTS: Final = MappingProxyType(
    {FLATFACE_SKETCH_OPERATION_SPEC.operation_id: FLATFACE_SKETCH_PRODUCT_CONTRACT}
)


def resolve_flatface_sketch_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return (
        FLATFACE_SKETCH_OPERATION_SPEC
        if (operation_id, semantic_operation) == FLATFACE_SKETCH_REVIEWED_PRODUCT_IDENTITIES[0]
        else None
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
        return (
            item.isValid() is True
            and tuple(item.State) == ("Up-to-date",)
            and shape.isNull() is False
            and shape.isValid() is True
            and len(tuple(shape.Solids)) == 1
            and float(shape.Volume) > 1e-9
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _receipt_shape_sha256(source: object) -> str:
    receipt = source.native_receipt
    candidates = tuple(
        item
        for item in (
            getattr(receipt, "result_shape_sha256", None),
            getattr(receipt, "shape_sha256", None),
        )
        if _is_sha256(item)
    )
    if len(set(candidates)) != 1:
        _integrity_failure()
    return candidates[0]


@dataclass(frozen=True, slots=True, kw_only=True)
class _AuthenticatedFlatFaceSource:
    base: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    base_shape_sha256: str

    def __post_init__(self) -> None:
        if self.base is None or self.body is None or not _is_sha256(self.base_shape_sha256):
            _integrity_failure()


def _authenticate_source(
    document: object,
    source_results: tuple[object, ...],
    *,
    session: object,
    run_token: object,
) -> _AuthenticatedFlatFaceSource:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
    )

    if (
        session is None
        or getattr(session, "doc", None) is not document
        or run_token is None
        or type(source_results) is not tuple
        or len(source_results) != 1
        or type(source_results[0]) is not ReviewedNativeExecutionResult
    ):
        _integrity_failure()
    source = source_results[0]
    base = source.object
    receipt = source.native_receipt
    try:
        document_objects = tuple(document.Objects)
        base_identity = session.read_object_identity(base)
        resolver = base.getParentGeoFeatureGroup
        body = resolver() if callable(resolver) else None
        body_identity = session.read_object_identity(body)
        retained = source._is_retained_for_run(run_token)  # noqa: SLF001
        group = tuple(body.Group)
    except (Exception, SystemExit):
        _integrity_failure()
    base_shape_sha256 = _receipt_shape_sha256(source)
    receipt_name = getattr(receipt, "object_name", None)
    if (
        type(base_identity) is not EntityIdentity
        or type(body_identity) is not EntityIdentity
        or not retained
        or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
        or source.route.operation not in source.route.manifest.operations
        or source.object is not source.owned_objects[0]
        or source.result_kind.value != "solid"
        or source.plan_sha256 != getattr(receipt, "plan_sha256", None)
        or receipt_name != getattr(base, "Name", None)
        or not _is_sha256(getattr(receipt, "receipt_sha256", None))
        or not hmac.compare_digest(_shape_sha256(base), base_shape_sha256)
        or getattr(base, "Document", None) is not document
        or not any(base is item for item in document_objects)
        or getattr(base, "TypeId", None) != source.route.operation.native_type_id
        or not _valid_solid(base)
        or base_identity.object_type != base.TypeId
        or base_identity.feature_id is None
        or base_identity.semantic_role not in source.semantic_roles
        or base_identity.provenance.source is not ProvenanceSource.MODEL
        or base_identity.provenance.operation_id != "apply_reviewed_intent"
        or body is None
        or getattr(body, "Document", None) is not document
        or getattr(body, "TypeId", None) != "PartDesign::Body"
        or not any(body is item for item in document_objects)
        or body_identity.object_type != "PartDesign::Body"
        or body_identity.feature_id is None
        or body_identity.semantic_role is not SemanticRole.PART
        or body_identity.provenance.source is not ProvenanceSource.MODEL
        or body_identity.provenance.operation_id != "apply_reviewed_intent"
        or not any(base is item for item in group)
        or body.Tip is not base
    ):
        _integrity_failure()
    return _AuthenticatedFlatFaceSource(
        base=base,
        body=body,
        base_shape_sha256=base_shape_sha256,
    )


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> FlatFaceSketchBackendPlan:
    if (
        type(plan) is not FlatFaceSketchBackendPlan
        or type(plan_document) is not DocumentRef
        or operation != FLATFACE_SKETCH_OPERATION_SPEC
        or plan.manifest_sha256 != FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256
        or plan.adapter_contract_sha256
        != FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    try:
        decoded = decode_flatface_sketch_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except FlatFaceSketchRuleError:
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


@dataclass(frozen=True, slots=True, kw_only=True)
class FlatFaceSketchOwnershipReceipt:
    native_receipt: FlatFaceSketchConformanceReceipt
    plan: FlatFaceSketchBackendPlan = field(repr=False)
    object: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    base: object = field(repr=False, compare=False)
    plan_content_sha256: str
    source_shape_sha256: str
    selection_identity: tuple[str, str, str, str]
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.native_receipt) is not FlatFaceSketchConformanceReceipt
            or type(self.plan) is not FlatFaceSketchBackendPlan
            or self.native_receipt.plan_sha256 != self.plan.plan_sha256
            or self.object is None
            or self.body is None
            or self.base is None
            or not _is_sha256(self.plan_content_sha256)
            or not _is_sha256(self.source_shape_sha256)
            or type(self.selection_identity) is not tuple
            or len(self.selection_identity) != 4
        ):
            _integrity_failure()
        body = b"\0".join(
            (
                _OWNERSHIP_DOMAIN,
                self.native_receipt.receipt_sha256.encode("ascii"),
                self.plan_content_sha256.encode("ascii"),
                self.source_shape_sha256.encode("ascii"),
                self.plan.body_id.encode("utf-8"),
                self.plan.base_node_id.encode("utf-8"),
                self.plan.base_result_id.encode("utf-8"),
                self.plan.node_id.encode("utf-8"),
                self.plan.result_id.encode("utf-8"),
                *(item.encode("utf-8") for item in self.selection_identity),
            )
        )
        object.__setattr__(self, "receipt_sha256", hashlib.sha256(body).hexdigest())

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def body_name(self) -> str:
        return self.native_receipt.body_name

    @property
    def base_name(self) -> str:
        return self.native_receipt.base_name

    @property
    def state_sha256(self) -> str:
        return self.native_receipt.state_sha256

    @property
    def shape_sha256(self) -> str:
        return self.native_receipt.shape_sha256

    @property
    def face_selection(self) -> FlatFaceSelectionEvidence:
        return self.native_receipt.selection

    def owned_objects(self, result: object) -> tuple[object, ...]:
        if result is not self.object:
            _integrity_failure()
        return (self.object,)

    def validate_native_result(self, document: object, result: object) -> None:
        try:
            _face, _native_label, evidence = select_unique_zmax_planar_face(self.base)
            valid = (
                result is self.object
                and getattr(result, "Document", None) is document
                and document.getObject(self.object_name) is result
                and getattr(result, "TypeId", None) == FLATFACE_SKETCH_NATIVE_TYPE_ID
                and getattr(self.body, "Document", None) is document
                and self.body.Name == self.body_name
                and getattr(self.base, "Document", None) is document
                and self.base.Name == self.base_name
                and tuple(item.Name for item in tuple(self.body.Group))
                == self.native_receipt.group_after_names
                and self.body.Tip is result
                and result.MapMode == "FlatFace"
                and tuple(result.AttachmentSupport)[0][0] is self.base
                and evidence == self.face_selection
                and hmac.compare_digest(_shape_sha256(self.base), self.source_shape_sha256)
                and hmac.compare_digest(_shape_sha256(result), self.shape_sha256)
                and hmac.compare_digest(
                    flatface_rules._geometry_sha256(result),  # noqa: SLF001
                    self.native_receipt.geometry_sha256,
                )
                and hmac.compare_digest(
                    flatface_rules._state_sha256(  # noqa: SLF001
                        self.body,
                        self.base,
                        result,
                        evidence,
                    ),
                    self.state_sha256,
                )
            )
        except (Exception, SystemExit):
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
            or observation.object_type != FLATFACE_SKETCH_NATIVE_TYPE_ID
            or observation.semantic_role != SemanticRole.FEATURE.value
            or observation.valid_shape is not True
        ):
            _integrity_failure()


def execute_flatface_sketch_reviewed_plan_with_sources(
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
    checked = _validate_plan_contract(plan, plan_document, operation)
    if type(payload) is not bytes or payload != checked.canonical_bytes:
        _integrity_failure()
    authenticated = _authenticate_source(
        document,
        source_results,
        session=session,
        run_token=run_token,
    )
    try:
        before, snapshots = flatface_rules._snapshot_document(document)  # noqa: SLF001
    except FlatFaceSketchRuleError:
        _integrity_failure()
    try:
        native_receipt = apply_flatface_sketch_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
            bindings=FlatFaceSketchExecutionBindings(
                document=document,
                body=authenticated.body,
                base=authenticated.base,
                body_id=checked.body_id,
                base_node_id=checked.base_node_id,
                base_result_id=checked.base_result_id,
            ),
        )
        result = document.getObject(native_receipt.object_name)
        if result is None:
            _integrity_failure()
        ownership = FlatFaceSketchOwnershipReceipt(
            native_receipt=native_receipt,
            plan=checked,
            object=result,
            body=authenticated.body,
            base=authenticated.base,
            plan_content_sha256=plan_document.content_sha256,
            source_shape_sha256=authenticated.base_shape_sha256,
            selection_identity=FLATFACE_SKETCH_SELECTOR_TERM.semantic_identity,
        )
        ownership.validate_native_result(document, result)
    except KeyboardInterrupt:
        if not flatface_rules._restore_document(document, before, snapshots):  # noqa: SLF001
            _integrity_failure()
        raise
    except BaseException:
        if not flatface_rules._restore_document(document, before, snapshots):  # noqa: SLF001
            _integrity_failure()
        _integrity_failure()

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


def execute_flatface_sketch_reviewed_plan(
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
    return execute_flatface_sketch_reviewed_plan_with_sources(
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
class FlatFaceSketchReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    adapter_factory: object = field(repr=False, compare=False)
    validate_plan: object = field(repr=False, compare=False)
    execute_plan: object = field(repr=False, compare=False)
    operation_ids: tuple[str, ...]
    minimum_sources: int
    maximum_sources: int
    requires_same_run_sources: bool


FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC: Final = FlatFaceSketchReviewedFamilySpec(
    manifest=FLATFACE_SKETCH_FAMILY_MANIFEST,
    subject_type_term=_bridge(FLATFACE_SKETCH_BODY_OWNERSHIP_TERM),
    adapter_factory=flatface_sketch_reviewed_adapter_factory,
    validate_plan=validate_flatface_sketch_reviewed_plan,
    execute_plan=execute_flatface_sketch_reviewed_plan,
    operation_ids=(FLATFACE_SKETCH_OPERATION_SPEC.operation_id,),
    minimum_sources=1,
    maximum_sources=1,
    requires_same_run_sources=True,
)


def build_flatface_sketch_reviewed_family_descriptor() -> object:
    """Build the private descriptor without registering a CURRENT route."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedIntentFamilyDescriptor,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    return _ReviewedIntentFamilyDescriptor(
        manifest=FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC.manifest,
        subject_type_term=FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC.subject_type_term,
        adapter_factory=FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC.adapter_factory,
        validate_plan=FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC.validate_plan,
        execute_plan=FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC.execute_plan,
        product_results=(
            _ReviewedProductResultContract(
                operation_id=FLATFACE_SKETCH_OPERATION_SPEC.operation_id,
                result_kind=_ReviewedProductResultKind.VALID_SHAPE,
                owned_type_ids=(FLATFACE_SKETCH_NATIVE_TYPE_ID,),
                semantic_roles=(SemanticRole.FEATURE,),
                source_count=1,
                requires_state_sha256=True,
            ),
        ),
        minimum_sources=1,
        maximum_sources=1,
        requires_same_run_sources=True,
    )


@dataclass(frozen=True, slots=True)
class FlatFaceSketchRegistrationHandoff:
    route_identity: tuple[str, str]
    manifest_sha256: str
    source_count: int
    result_kind: str
    owned_type_ids: tuple[str, ...]
    requires_same_run_sources: bool
    downstream_operation_id: str
    downstream_profile_source_index: int
    downstream_receipt_fields: tuple[str, ...]
    blockers: tuple[str, ...]

    @property
    def shared_registration_ready(self) -> bool:
        return False


FLATFACE_SKETCH_REGISTRATION_HANDOFF: Final = FlatFaceSketchRegistrationHandoff(
    route_identity=FLATFACE_SKETCH_REVIEWED_PRODUCT_IDENTITIES[0],
    manifest_sha256=FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256,
    source_count=1,
    result_kind="valid_shape",
    owned_type_ids=(FLATFACE_SKETCH_NATIVE_TYPE_ID,),
    requires_same_run_sources=True,
    downstream_operation_id="partdesign_residual.hole",
    downstream_profile_source_index=1,
    downstream_receipt_fields=(
        "object_name",
        "plan_sha256",
        "receipt_sha256",
        "shape_sha256",
    ),
    blockers=(
        "shared-dispatcher-registration-not-in-family-scope",
        "intel-and-arm-release-attestation-refresh-pending",
    ),
)


__all__ = [
    "FLATFACE_SKETCH_PRODUCT_CONTRACT",
    "FLATFACE_SKETCH_PRODUCT_CONTRACTS",
    "FLATFACE_SKETCH_REGISTRATION_HANDOFF",
    "FLATFACE_SKETCH_REVIEWED_FAMILY_SPEC",
    "FLATFACE_SKETCH_REVIEWED_PRODUCT_IDENTITIES",
    "FlatFaceSketchOwnershipReceipt",
    "FlatFaceSketchProductContract",
    "FlatFaceSketchRegistrationHandoff",
    "FlatFaceSketchReviewedFamilySpec",
    "build_flatface_sketch_reviewed_family_descriptor",
    "execute_flatface_sketch_reviewed_plan",
    "execute_flatface_sketch_reviewed_plan_with_sources",
    "resolve_flatface_sketch_reviewed_operation",
]
