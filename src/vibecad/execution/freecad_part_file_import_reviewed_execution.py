"""Private product contracts for reviewed Part file imports.

The shared dispatcher registers this family only through the run-scoped
artifact resolver.  The native rule needs three engine-owned capabilities:
an exact artifact ``DocumentRef``, its authenticated ``ArtifactReader``, and
a private host stager.  The shared callback therefore fails closed before
mutation unless that complete authority bundle is present.  No path, label,
store key, or document-object fallback is accepted here.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.freecad_reviewed_artifact_inputs import ReviewedArtifactContext
from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef, IntentBridgeError
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_file_import_adapter import (
    PART_FILE_IMPORT_MANIFEST,
    PART_FILE_IMPORT_STRUCTURE_TERM,
    FreeCADPartFileImportAdapter,
)
from vibecad.intent_bridge.ports import ArtifactReader, read_verified_document
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_file_import_rules import (
    MAX_PART_FILE_IMPORT_ARTIFACT_BYTES,
    PART_FILE_IMPORT_NATIVE_SPECS,
    HostOwnedImportStager,
    PartFileImportBackendPlan,
    PartFileImportConformanceReceipt,
    PartFileImportExecutionBindings,
    PartFileImportOperation,
    apply_part_file_import_plan,
    decode_part_file_import_backend_plan,
)
from vibecad.validation.contracts import EntityObservation


def _integrity_failure() -> None:
    # Lazy imports preserve shared dispatcher -> family module initialization.
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


PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS: Final = tuple(PartFileImportOperation)
_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PART_FILE_IMPORT_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PART_FILE_IMPORT_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS
    }
)
PART_FILE_IMPORT_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)

# The shared executor now owns and supplies exact run-scoped artifact
# authority.  These constants freeze that registration decision for tests and
# private integration consumers; they are not runtime feature detection.
PART_FILE_IMPORT_SHARED_REGISTRATION_READY: Final = True
PART_FILE_IMPORT_SHARED_BLOCKERS: Final = ()

_ARTIFACT_REQUIREMENT_CONTRACT_SHA256: Final = hashlib.sha256(
    b"vibecad-reviewed-part-file-import-artifact-requirement-v1\0"
    + PART_FILE_IMPORT_MANIFEST.manifest_sha256.encode("ascii")
).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartFileImportArtifactAuthority:
    """Process-local, engine-owned artifact capabilities for one import."""

    artifact_document: DocumentRef
    artifacts: ArtifactReader = field(repr=False, compare=False)
    stager: HostOwnedImportStager = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.artifact_document) is not DocumentRef
            or not isinstance(self.artifacts, ArtifactReader)
            or type(self.stager) is not HostOwnedImportStager
        ):
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartFileImportResultInvariant:
    """Exact detached valid-shape contract for one native importer."""

    operation: PartFileImportOperation
    native_type_id: str
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        expected = PART_FILE_IMPORT_NATIVE_SPECS.get(self.operation)
        if (
            expected is None
            or self.native_type_id != expected.type_id
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        result: object,
        receipt: PartFileImportConformanceReceipt,
    ) -> None:
        try:
            shape = result.Shape
            raw = shape.exportBrepToString().encode("utf-8")
            valid = (
                type(receipt) is PartFileImportConformanceReceipt
                and receipt.operation is self.operation
                and result.Document is document
                and document.getObject(receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.native_type_id
                and result.FileName == ""
                and result.isValid() is True
                and tuple(result.State) == ("Up-to-date",)
                and shape.isNull() is False
                and shape.isValid() is True
                and str(shape.ShapeType) == receipt.result_shape_type
                and len(shape.Edges) == receipt.edge_count
                and len(shape.Faces) == receipt.face_count
                and len(shape.Solids) == receipt.solid_count
                and 1
                <= len(shape.Vertexes) + len(shape.Edges) + len(shape.Faces) + len(shape.Solids)
                and math.isfinite(float(shape.Length))
                and math.isfinite(float(shape.Area))
                and math.isfinite(float(shape.Volume))
                and hmac.compare_digest(
                    hashlib.sha256(raw).hexdigest(),
                    receipt.result_shape_sha256,
                )
            )
        except (Exception, SystemExit, UnicodeError, OverflowError):
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
        ):
            _integrity_failure()


PART_FILE_IMPORT_RESULT_INVARIANTS: Final = MappingProxyType(
    {
        operation: PartFileImportResultInvariant(
            operation=operation,
            native_type_id=PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id,
        )
        for operation in PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartFileImportOwnershipClosure:
    """Content-bound native receipt retained through identity adoption."""

    invariant: PartFileImportResultInvariant
    native_receipt: PartFileImportConformanceReceipt

    def __post_init__(self) -> None:
        if (
            type(self.invariant) is not PartFileImportResultInvariant
            or type(self.native_receipt) is not PartFileImportConformanceReceipt
            or self.native_receipt.operation is not self.invariant.operation
        ):
            _integrity_failure()

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def operation(self) -> PartFileImportOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def receipt_sha256(self) -> str:
        return self.native_receipt.receipt_sha256

    @property
    def artifact_id(self) -> str:
        return self.native_receipt.artifact_id

    @property
    def artifact_content_sha256(self) -> str:
        return self.native_receipt.artifact_content_sha256

    @property
    def result_shape_sha256(self) -> str:
        return self.native_receipt.result_shape_sha256

    def validate_native_result(self, document: object, result: object) -> None:
        self.invariant.validate_native_result(document, result, self.native_receipt)

    def validate_adopted_observation(self, observation: object) -> None:
        self.invariant.validate_adopted_observation(observation)
        if observation.solid_count != self.native_receipt.solid_count:
            _integrity_failure()

    def validate_adoption(
        self,
        document: object,
        result: object,
        observation: object,
    ) -> None:
        self.validate_native_result(document, result)
        self.validate_adopted_observation(observation)


def resolve_part_file_import_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve only the complete static family plus semantic identity."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def part_file_import_reviewed_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    return FreeCADPartFileImportAdapter(sink)


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartFileImportBackendPlan:
    if (
        type(plan) is not PartFileImportBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PART_FILE_IMPORT_MANIFEST.operations
        or plan.operation not in PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
        or plan.adapter_contract_sha256 != PART_FILE_IMPORT_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PART_FILE_IMPORT_MANIFEST.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
        or plan_document.role_term_ref_id != PART_FILE_IMPORT_MANIFEST.plan_role_term.term_ref_id
        or plan_document.schema_term_ref_id
        != PART_FILE_IMPORT_MANIFEST.plan_schema_term.term_ref_id
        or plan_document.media_type != PART_FILE_IMPORT_MANIFEST.plan_media_type
    ):
        _integrity_failure()
    try:
        decoded = decode_part_file_import_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_part_file_import_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind a canonical import plan to its exact Reviewed route receipt."""

    if (
        type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != PART_FILE_IMPORT_MANIFEST.manifest_sha256
        or receipt.adapter != PART_FILE_IMPORT_MANIFEST.adapter
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


def _verified_artifact_payload(
    plan: PartFileImportBackendPlan,
    authority: PartFileImportArtifactAuthority,
) -> bytes:
    artifact = authority.artifact_document
    spec = PART_FILE_IMPORT_NATIVE_SPECS[plan.operation]
    if (
        artifact.artifact_id != plan.artifact_id
        or not hmac.compare_digest(artifact.content_sha256, plan.artifact_content_sha256)
        or not hmac.compare_digest(artifact.document_digest, plan.artifact_content_sha256)
        or artifact.document_id != f"part_file_import_{plan.artifact_content_sha256[:32]}"
        or artifact.role_term_ref_id != spec.artifact_role_term_ref_id
        or artifact.schema_term_ref_id != spec.artifact_schema_term_ref_id
        or artifact.media_type != spec.artifact_media_type
        or artifact.size_bytes > MAX_PART_FILE_IMPORT_ARTIFACT_BYTES
    ):
        _integrity_failure()
    try:
        return read_verified_document(
            authority.artifacts,
            artifact,
            maximum_bytes=MAX_PART_FILE_IMPORT_ARTIFACT_BYTES,
        )
    except (IntentBridgeError, SystemExit):
        _integrity_failure()


def extract_part_file_import_artifact_requirement(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> object:
    """Project one exact generic artifact requirement from the sealed plan."""

    checked = _validate_plan_contract(plan, plan_document, operation)
    spec = PART_FILE_IMPORT_NATIVE_SPECS[checked.operation]
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedArtifactRequirement,
    )

    return _ReviewedArtifactRequirement(
        artifact_id=checked.artifact_id,
        content_sha256=checked.artifact_content_sha256,
        role_term_ref_id=spec.artifact_role_term_ref_id,
        schema_term_ref_id=spec.artifact_schema_term_ref_id,
        media_type=spec.artifact_media_type,
        maximum_bytes=MAX_PART_FILE_IMPORT_ARTIFACT_BYTES,
    )


def part_file_import_artifact_requirement_descriptor() -> object:
    """Build the shared descriptor without making this family a current route."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedArtifactRequirementDescriptor,
    )

    return _ReviewedArtifactRequirementDescriptor(
        descriptor_id="reviewed_part_file_import_artifact",
        descriptor_version="1.0.0",
        descriptor_contract_sha256=_ARTIFACT_REQUIREMENT_CONTRACT_SHA256,
        operation_ids=tuple(item.value for item in PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS),
        extract=extract_part_file_import_artifact_requirement,
    )


def execute_part_file_import_reviewed_plan_with_authority(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    authority: PartFileImportArtifactAuthority,
) -> object:
    """Internal hook for an exact, executor-owned artifact authority bundle."""

    if (
        document is None
        or type(payload) is not bytes
        or type(authority) is not PartFileImportArtifactAuthority
    ):
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_part_file_import_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()

    # Read and authenticate before observing a mutation baseline or invoking
    # FreeCAD.  The native rule repeats this check at the actual authority seam.
    artifact_payload = _verified_artifact_payload(checked, authority)
    if len(artifact_payload) != authority.artifact_document.size_bytes:
        _integrity_failure()
    try:
        before = tuple(document.Objects)
    except (Exception, SystemExit):
        _integrity_failure()
    receipt = apply_part_file_import_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=PartFileImportExecutionBindings(
            document=document,
            artifact_document=authority.artifact_document,
            artifacts=authority.artifacts,
            stager=authority.stager,
            body_id=checked.body_id,
            expected_adapter_contract_sha256=(
                PART_FILE_IMPORT_MANIFEST.adapter.adapter_contract_sha256
            ),
            expected_manifest_sha256=PART_FILE_IMPORT_MANIFEST.manifest_sha256,
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
        type(receipt) is not PartFileImportConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.plan_sha256 != checked.plan_sha256
        or receipt.artifact_id != checked.artifact_id
        or not hmac.compare_digest(
            receipt.artifact_content_sha256,
            checked.artifact_content_sha256,
        )
        or receipt.artifact_size_bytes != authority.artifact_document.size_bytes
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
    ):
        _integrity_failure()
    ownership = PartFileImportOwnershipClosure(
        invariant=PART_FILE_IMPORT_RESULT_INVARIANTS[checked.operation],
        native_receipt=receipt,
    )
    ownership.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


def execute_part_file_import_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Shared callback; fail closed until the shared context owns artifacts."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.source_results
    ):
        _integrity_failure()
    artifact_context = context.artifact_context
    if type(artifact_context) is not ReviewedArtifactContext:
        _integrity_failure()
    try:
        stager = artifact_context.stager_factory.create()
    except (Exception, SystemExit):
        _integrity_failure()
    if type(stager) is not HostOwnedImportStager:
        _integrity_failure()
    authority = PartFileImportArtifactAuthority(
        artifact_document=artifact_context.artifact_document,
        artifacts=artifact_context.artifacts,
        stager=stager,
    )
    return execute_part_file_import_reviewed_plan_with_authority(
        document,
        plan,
        payload,
        plan_document,
        operation,
        authority,
    )


@dataclass(frozen=True, slots=True)
class PartFileImportReviewedFamilySpec:
    """Family descriptor payload registered by the shared dispatcher."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]
    result_invariants: MappingProxyType
    shared_registration_ready: bool
    shared_blockers: tuple[str, ...]


PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC: Final = PartFileImportReviewedFamilySpec(
    manifest=PART_FILE_IMPORT_MANIFEST,
    subject_type_term=_bridge_term(PART_FILE_IMPORT_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=part_file_import_reviewed_adapter_factory,
    validate_plan=validate_part_file_import_reviewed_plan,
    execute_plan=execute_part_file_import_reviewed_plan,
    result_invariants=PART_FILE_IMPORT_RESULT_INVARIANTS,
    shared_registration_ready=PART_FILE_IMPORT_SHARED_REGISTRATION_READY,
    shared_blockers=PART_FILE_IMPORT_SHARED_BLOCKERS,
)


def build_part_file_import_reviewed_family_descriptor() -> object:
    """Return the complete private descriptor used by shared registration."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedIntentFamilyDescriptor,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    return _ReviewedIntentFamilyDescriptor(
        manifest=PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.manifest,
        subject_type_term=PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.subject_type_term,
        adapter_factory=PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.adapter_factory,
        validate_plan=PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.validate_plan,
        execute_plan=PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.execute_plan,
        product_results=tuple(
            _ReviewedProductResultContract(
                operation_id=operation.value,
                result_kind=_ReviewedProductResultKind.VALID_SHAPE,
                owned_type_ids=(PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id,),
                semantic_roles=(SemanticRole.FEATURE,),
            )
            for operation in PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS
        ),
        artifact_requirement=part_file_import_artifact_requirement_descriptor(),
    )


__all__ = [
    "PART_FILE_IMPORT_RESULT_INVARIANTS",
    "PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC",
    "PART_FILE_IMPORT_REVIEWED_PRODUCT_IDENTITIES",
    "PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS",
    "PART_FILE_IMPORT_SHARED_BLOCKERS",
    "PART_FILE_IMPORT_SHARED_REGISTRATION_READY",
    "PartFileImportArtifactAuthority",
    "PartFileImportOwnershipClosure",
    "PartFileImportResultInvariant",
    "PartFileImportReviewedFamilySpec",
    "build_part_file_import_reviewed_family_descriptor",
    "execute_part_file_import_reviewed_plan",
    "execute_part_file_import_reviewed_plan_with_authority",
    "extract_part_file_import_artifact_requirement",
    "part_file_import_artifact_requirement_descriptor",
    "part_file_import_reviewed_adapter_factory",
    "resolve_part_file_import_reviewed_operation",
    "validate_part_file_import_reviewed_plan",
]
