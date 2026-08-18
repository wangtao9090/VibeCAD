"""Private product compatibility callbacks for reviewed ``Image::ImagePlane``.

The existing adapter binds the model-authored graph to an immutable raster
artifact id and digest.  This module deliberately does not resolve that id
from a pathname, label, object name, or store convention.  Native execution
requires one engine-owned :class:`ImagePlaneReviewedArtifactContext` carrying
the exact ``DocumentRef``, ``ArtifactReader``, document workspace, and private
stager.

Core68 does not yet place such a bundle on its shared family execution
context.  The dispatcher-compatible callback therefore fails before native
mutation today; ``execute_imageplane_reviewed_plan_with_artifacts`` is the
minimal integration hook.  The product contract is the safe CREATE subset of
the native place-or-edit rule.  An already-bound plane is rejected before the
rule is entered rather than silently performing an UPDATE under a CREATE
contract.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from vibecad.engine.document_assets import DocumentAssetWorkspace
from vibecad.execution.freecad_reviewed_artifact_inputs import ReviewedArtifactContext
from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_imageplane_adapter import (
    IMAGEPLANE_MANIFEST,
    IMAGEPLANE_OPERATION_SPEC,
    IMAGEPLANE_STRUCTURE_TERM,
    FreeCADImagePlaneAdapter,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.ports import ArtifactReader
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric import freecad_imageplane_rules as imageplane_rules
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_imageplane_rules import (
    IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID,
    IMAGEPLANE_ARTIFACT_SPECS,
    IMAGEPLANE_RULE_CONTRACT_SHA256,
    MAX_IMAGEPLANE_ARTIFACT_BYTES,
    HostOwnedImageStager,
    ImagePlaneBackendPlan,
    ImagePlaneConformanceReceipt,
    ImagePlaneExecutionBindings,
    apply_imageplane_plan,
    decode_imageplane_backend_plan,
)
from vibecad.validation.contracts import EntityObservation

_OWNERSHIP_RECEIPT_DOMAIN = b"vibecad.reviewed-imageplane-product-ownership.v1\0"
_ARTIFACT_REQUIREMENT_CONTRACT_SHA256: Final = hashlib.sha256(
    b"vibecad-reviewed-imageplane-artifact-requirement-v1\0"
    + IMAGEPLANE_MANIFEST.manifest_sha256.encode("ascii")
).hexdigest()
_CREATE_RECOVERY_CONTRACT_SHA256: Final = hashlib.sha256(
    b"vibecad-reviewed-imageplane-workspace-recovery-v1\0"
    + IMAGEPLANE_RULE_CONTRACT_SHA256.encode("ascii")
).hexdigest()


def _integrity_failure() -> None:
    # Lazy import keeps shared dispatcher -> family initialization acyclic.
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


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


IMAGEPLANE_REVIEWED_PRODUCT_IDENTITIES: Final = (
    (
        f"{IMAGEPLANE_MANIFEST.family_id}.{IMAGEPLANE_OPERATION_SPEC.operation_id}",
        _semantic_operation(IMAGEPLANE_OPERATION_SPEC),
    ),
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {IMAGEPLANE_REVIEWED_PRODUCT_IDENTITIES[0]: IMAGEPLANE_OPERATION_SPEC}
)


@dataclass(frozen=True, slots=True)
class ImagePlaneReviewedProductResultContract:
    """Family-owned projection of the shared static product result contract."""

    operation_id: str = "place_or_edit_image_plane"
    result_kind: str = "reference"
    owned_type_ids: tuple[str, ...] = ("Image::ImagePlane",)
    semantic_roles: tuple[SemanticRole, ...] = (SemanticRole.SUPPORT,)
    execution_mode: str = "create"
    minimum_sources: int = 0
    maximum_sources: int = 0

    def __post_init__(self) -> None:
        if (
            self.operation_id != IMAGEPLANE_OPERATION_SPEC.operation_id
            or self.result_kind != "reference"
            or self.owned_type_ids != (IMAGEPLANE_OPERATION_SPEC.native_type_id,)
            or self.semantic_roles != (SemanticRole.SUPPORT,)
            or self.execution_mode != "create"
            or self.minimum_sources != 0
            or self.maximum_sources != 0
        ):
            _integrity_failure()


IMAGEPLANE_REVIEWED_PRODUCT_RESULT_CONTRACT: Final = ImagePlaneReviewedProductResultContract()


@dataclass(frozen=True, slots=True, kw_only=True)
class ImagePlaneReviewedArtifactContext:
    """Exact engine-owned resources needed to resolve one raster reference."""

    document_assets: DocumentAssetWorkspace = field(repr=False, compare=False)
    artifact_document: DocumentRef
    artifacts: ArtifactReader = field(repr=False, compare=False)
    stager: HostOwnedImageStager = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.document_assets) is not DocumentAssetWorkspace
            or type(self.artifact_document) is not DocumentRef
            or not isinstance(self.artifacts, ArtifactReader)
            or type(self.stager) is not HostOwnedImageStager
        ):
            _integrity_failure()


def resolve_imageplane_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve only the complete formal/manifest semantic identity."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def imageplane_reviewed_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    return FreeCADImagePlaneAdapter(sink)


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> ImagePlaneBackendPlan:
    if (
        type(plan) is not ImagePlaneBackendPlan
        or type(plan_document) is not DocumentRef
        or operation is not IMAGEPLANE_OPERATION_SPEC
        or plan.adapter_contract_sha256 != IMAGEPLANE_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != IMAGEPLANE_MANIFEST.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or len(plan.canonical_bytes) != plan_document.size_bytes
        or not hmac.compare_digest(
            hashlib.sha256(plan.canonical_bytes).hexdigest(),
            plan_document.content_sha256,
        )
    ):
        _integrity_failure()
    try:
        decoded = decode_imageplane_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_imageplane_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind one canonical plan to the exact existing ImagePlane manifest."""

    if (
        type(receipt) is not ReviewedPlanReceipt
        or operation is not IMAGEPLANE_OPERATION_SPEC
        or receipt.operation != operation
        or receipt.manifest_sha256 != IMAGEPLANE_MANIFEST.manifest_sha256
        or receipt.adapter != IMAGEPLANE_MANIFEST.adapter
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


def extract_imageplane_artifact_requirement(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> object:
    """Project one exact generic artifact requirement from the sealed plan."""

    checked = _validate_plan_contract(plan, plan_document, operation)
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedArtifactRequirement,
    )

    return _ReviewedArtifactRequirement(
        artifact_id=checked.artifact_id,
        content_sha256=checked.artifact_content_sha256,
        role_term_ref_id=IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID,
        schema_term_ref_id=checked.artifact_schema_term_ref_id,
        media_type=checked.artifact_media_type,
        maximum_bytes=MAX_IMAGEPLANE_ARTIFACT_BYTES,
    )


def imageplane_artifact_requirement_descriptor() -> object:
    """Build the shared descriptor without making ImagePlane a current route."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedArtifactRequirementDescriptor,
    )

    return _ReviewedArtifactRequirementDescriptor(
        descriptor_id="reviewed_imageplane_artifact",
        descriptor_version="1.0.0",
        descriptor_contract_sha256=_ARTIFACT_REQUIREMENT_CONTRACT_SHA256,
        operation_ids=(IMAGEPLANE_OPERATION_SPEC.operation_id,),
        extract=extract_imageplane_artifact_requirement,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ImagePlaneResultInvariant:
    """Document-root, non-shape result and retained-asset invariant."""

    native_type_id: str = "Image::ImagePlane"
    semantic_role: SemanticRole = SemanticRole.SUPPORT

    def __post_init__(self) -> None:
        if (
            self.native_type_id != IMAGEPLANE_OPERATION_SPEC.native_type_id
            or self.semantic_role is not SemanticRole.SUPPORT
        ):
            _integrity_failure()

    def validate_adopted_observation(self, observation: object) -> None:
        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != self.native_type_id
            or observation.semantic_role != self.semantic_role.value
            or any(
                value is not None
                for value in (
                    observation.volume_mm3,
                    observation.area_mm2,
                    observation.bbox_mm,
                    observation.center_of_mass_mm,
                    observation.valid_shape,
                    observation.solid_count,
                )
            )
        ):
            _integrity_failure()


IMAGEPLANE_RESULT_INVARIANT: Final = ImagePlaneResultInvariant()


@dataclass(frozen=True, slots=True, kw_only=True)
class ImagePlaneOwnershipClosure:
    """Authenticated live-object plus included-file ownership receipt."""

    invariant: ImagePlaneResultInvariant
    native_receipt: ImagePlaneConformanceReceipt
    plan: ImagePlaneBackendPlan = field(repr=False)
    document_assets: DocumentAssetWorkspace = field(repr=False, compare=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.invariant) is not ImagePlaneResultInvariant
            or type(self.native_receipt) is not ImagePlaneConformanceReceipt
            or type(self.plan) is not ImagePlaneBackendPlan
            or type(self.document_assets) is not DocumentAssetWorkspace
            or self.native_receipt.disposition != "created"
            or self.native_receipt.plan_sha256 != self.plan.plan_sha256
            or self.native_receipt.artifact_id != self.plan.artifact_id
            or not hmac.compare_digest(
                self.native_receipt.artifact_content_sha256,
                self.plan.artifact_content_sha256,
            )
            or self.native_receipt.artifact_media_type != self.plan.artifact_media_type
        ):
            _integrity_failure()
        body = "\0".join(
            (
                self.native_receipt.receipt_sha256,
                self.plan.plan_sha256,
                self.plan.artifact_content_sha256,
                self.native_receipt.retained_alias,
                "document-workspace-owned;fcstd-included;reference-result",
            )
        ).encode("ascii")
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_OWNERSHIP_RECEIPT_DOMAIN + body).hexdigest(),
        )

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    def validate_native_result(self, document: object, result: object) -> None:
        try:
            workspace = self.document_assets.require_attached(document)
            valid = (
                result.Document is document
                and document.getObject(self.native_receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and result.Name == self.native_receipt.object_name
                and result.TypeId == self.invariant.native_type_id
                and result.getParentGroup() is None
                and math.isclose(
                    float(result.XSize),
                    self.native_receipt.x_size_mm,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and math.isclose(
                    float(result.YSize),
                    self.native_receipt.y_size_mm,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()
        try:
            # These are the exact native rule's content-bound conformance readers;
            # they never derive authority from Name, Label, or a caller path.
            imageplane_rules._validate_bound_feature(result, self.plan)  # noqa: SLF001
            imageplane_rules._validate_configuration(  # noqa: SLF001
                result,
                self.plan.configuration,
            )
            imageplane_rules._retained_signature(  # noqa: SLF001
                result,
                workspace,
                alias=self.native_receipt.retained_alias,
                content_sha256=self.native_receipt.artifact_content_sha256,
            )
        except (Exception, SystemExit):
            _integrity_failure()

    def validate_adoption(self, document: object, result: object, observation: object) -> None:
        self.validate_native_result(document, result)
        self.invariant.validate_adopted_observation(observation)


def _body_tips(document: object) -> tuple[tuple[object, object], ...]:
    try:
        return tuple(
            (item, item.Tip)
            for item in tuple(document.Objects)
            if getattr(item, "TypeId", None) == "PartDesign::Body"
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
            left,
            right,
            strict=True,
        )
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _ImagePlaneWorkspaceRecovery:
    document_assets: DocumentAssetWorkspace = field(repr=False, compare=False)
    document: object = field(repr=False, compare=False)
    workspace: Path
    before_manifest: tuple[tuple[str, str, int, str], ...]
    expected_entry: tuple[str, str, int, str]


def _workspace_state_sha256(manifest: tuple[tuple[str, str, int, str], ...]) -> str:
    return hashlib.sha256(
        b"vibecad-reviewed-imageplane-workspace-state-v1\0" + repr(manifest).encode("ascii")
    ).hexdigest()


def _require_workspace_recovery(
    document: object,
    operation: ReviewedOperationSpec,
    context: object,
    opaque: object,
) -> _ImagePlaneWorkspaceRecovery:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or operation is not IMAGEPLANE_OPERATION_SPEC
        or type(opaque) is not _ImagePlaneWorkspaceRecovery
        or opaque.document is not document
        or opaque.document_assets is not getattr(context.session, "_document_assets", None)
        or opaque.document_assets.require_attached(document) != opaque.workspace
    ):
        _integrity_failure()
    return opaque


def _prepare_imageplane_create_recovery(
    document: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> tuple[str, object]:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or operation is not IMAGEPLANE_OPERATION_SPEC
        or type(context.artifact_context) is not ReviewedArtifactContext
    ):
        _integrity_failure()
    generic = context.artifact_context
    assets = getattr(context.session, "_document_assets", None)
    document_ref = generic.artifact_document
    try:
        spec = IMAGEPLANE_ARTIFACT_SPECS[document_ref.media_type]
        workspace = assets.require_attached(document)
        before = imageplane_rules._workspace_manifest(workspace)  # noqa: SLF001
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(assets) is not DocumentAssetWorkspace
        or document_ref.role_term_ref_id != IMAGEPLANE_ARTIFACT_ROLE_TERM_REF_ID
        or document_ref.schema_term_ref_id != spec.schema_term_ref_id
        or not hmac.compare_digest(document_ref.document_digest, document_ref.content_sha256)
    ):
        _integrity_failure()
    opaque = _ImagePlaneWorkspaceRecovery(
        document_assets=assets,
        document=document,
        workspace=workspace,
        before_manifest=before,
        expected_entry=(
            document_ref.content_sha256 + spec.suffix,
            "file",
            document_ref.size_bytes,
            document_ref.content_sha256,
        ),
    )
    return _workspace_state_sha256(before), opaque


def _imageplane_workspace_is_committed(
    before: tuple[tuple[str, str, int, str], ...],
    current: tuple[tuple[str, str, int, str], ...],
    expected: tuple[str, str, int, str],
) -> bool:
    if expected in before:
        return current == before
    return (
        len(current) == len(before) + 1
        and expected in current
        and all(item in current for item in before)
    )


def _recover_imageplane_create(
    document: object,
    opaque: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> None:
    checked = _require_workspace_recovery(document, operation, context, opaque)
    try:
        current = imageplane_rules._workspace_manifest(checked.workspace)  # noqa: SLF001
        if current != checked.before_manifest:
            if (
                not _imageplane_workspace_is_committed(
                    checked.before_manifest,
                    current,
                    checked.expected_entry,
                )
                or checked.expected_entry in checked.before_manifest
            ):
                _integrity_failure()
            retained = checked.workspace / checked.expected_entry[0]
            if retained.parent != checked.workspace:
                _integrity_failure()
            retained.unlink()
        if (
            imageplane_rules._workspace_manifest(checked.workspace)  # noqa: SLF001
            != checked.before_manifest
        ):
            _integrity_failure()
    except (Exception, SystemExit):
        _integrity_failure()


def _verify_imageplane_create_state(
    document: object,
    opaque: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> str:
    checked = _require_workspace_recovery(document, operation, context, opaque)
    try:
        manifest = imageplane_rules._workspace_manifest(checked.workspace)  # noqa: SLF001
    except (Exception, SystemExit):
        _integrity_failure()
    return _workspace_state_sha256(manifest)


def _commit_imageplane_create(
    document: object,
    opaque: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> None:
    checked = _require_workspace_recovery(document, operation, context, opaque)
    try:
        current = imageplane_rules._workspace_manifest(checked.workspace)  # noqa: SLF001
    except (Exception, SystemExit):
        _integrity_failure()
    if not _imageplane_workspace_is_committed(
        checked.before_manifest,
        current,
        checked.expected_entry,
    ):
        _integrity_failure()


def imageplane_create_recovery_descriptor() -> object:
    """Build the shared late-adoption workspace recovery descriptor."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedCreateRecoveryDescriptor,
    )

    return _ReviewedCreateRecoveryDescriptor(
        descriptor_id="reviewed_imageplane_workspace",
        descriptor_version="1.0.0",
        descriptor_contract_sha256=_CREATE_RECOVERY_CONTRACT_SHA256,
        operation_ids=(IMAGEPLANE_OPERATION_SPEC.operation_id,),
        prepare=_prepare_imageplane_create_recovery,
        recover=_recover_imageplane_create,
        verify=_verify_imageplane_create_state,
        commit=_commit_imageplane_create,
    )


def _require_unbound_create(document: object, plan: ImagePlaneBackendPlan) -> None:
    """Reject the native rule's edit branch before any artifact read or mutation."""

    try:
        object_name = imageplane_rules._object_name(plan)  # noqa: SLF001
        binding_sha256 = imageplane_rules._binding_sha256(plan)  # noqa: SLF001
        existing = document.getObject(object_name)
        matches = tuple(
            item
            for item in tuple(document.Objects)
            if "VibeCADImagePlaneKey" in tuple(item.PropertiesList)
            and str(item.VibeCADImagePlaneKey) == binding_sha256
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if existing is not None or matches:
        _integrity_failure()


def execute_imageplane_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Dispatcher-compatible callback; blocked until shared supplies artifacts."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        document is None
        or type(payload) is not bytes
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.source_results
    ):
        _integrity_failure()
    generic = context.artifact_context
    if type(generic) is not ReviewedArtifactContext:
        _integrity_failure()
    try:
        stager = generic.stager_factory.create()
    except (Exception, SystemExit):
        _integrity_failure()
    document_assets = getattr(context.session, "_document_assets", None)
    if (
        type(stager) is not HostOwnedImageStager
        or type(document_assets) is not DocumentAssetWorkspace
    ):
        _integrity_failure()
    artifact_context = ImagePlaneReviewedArtifactContext(
        document_assets=document_assets,
        artifact_document=generic.artifact_document,
        artifacts=generic.artifacts,
        stager=stager,
    )
    return execute_imageplane_reviewed_plan_with_artifacts(
        document,
        plan,
        payload,
        plan_document,
        operation,
        artifact_context,
        session=context.session,
    )


def execute_imageplane_reviewed_plan_with_artifacts(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    artifact_context: ImagePlaneReviewedArtifactContext,
    *,
    session: object,
) -> object:
    """Execute the safe CREATE subset with an authenticated artifact bundle."""

    checked = _validate_plan_contract(plan, plan_document, operation)
    if (
        document is None
        or type(payload) is not bytes
        or type(artifact_context) is not ImagePlaneReviewedArtifactContext
        or session is None
        or getattr(session, "doc", None) is not document
        or getattr(session, "_document_assets", None) is not artifact_context.document_assets
    ):
        _integrity_failure()
    try:
        decoded = decode_imageplane_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
        before = tuple(document.Objects)
        body_tips = _body_tips(document)
        artifact_context.document_assets.require_attached(document)
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    _require_unbound_create(document, checked)

    receipt = apply_imageplane_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=ImagePlaneExecutionBindings(
            document=document,
            document_assets=artifact_context.document_assets,
            artifact_document=artifact_context.artifact_document,
            artifacts=artifact_context.artifacts,
            stager=artifact_context.stager,
            container_id=checked.container_id,
            expected_adapter_contract_sha256=checked.adapter_contract_sha256,
            expected_manifest_sha256=checked.manifest_sha256,
            expected_operation_specification_sha256=(checked.operation_specification_sha256),
        ),
    )
    try:
        after = tuple(document.Objects)
        result = document.getObject(receipt.object_name)
        added = tuple(item for item in after if not any(item is old for old in before))
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(receipt) is not ImagePlaneConformanceReceipt
        or receipt.disposition != "created"
        or receipt.plan_sha256 != checked.plan_sha256
        or len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
        or not _same_body_tips(_body_tips(document), body_tips)
    ):
        _integrity_failure()
    ownership = ImagePlaneOwnershipClosure(
        invariant=IMAGEPLANE_RESULT_INVARIANT,
        native_receipt=receipt,
        plan=checked,
        document_assets=artifact_context.document_assets,
    )
    ownership.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


@dataclass(frozen=True, slots=True)
class ImagePlaneReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]
    result_contract: ImagePlaneReviewedProductResultContract


IMAGEPLANE_REVIEWED_FAMILY_SPEC: Final = ImagePlaneReviewedFamilySpec(
    manifest=IMAGEPLANE_MANIFEST,
    subject_type_term=_bridge_term(IMAGEPLANE_STRUCTURE_TERM),
    operation_ids=(IMAGEPLANE_OPERATION_SPEC.operation_id,),
    adapter_factory=imageplane_reviewed_adapter_factory,
    validate_plan=validate_imageplane_reviewed_plan,
    execute_plan=execute_imageplane_reviewed_plan,
    result_contract=IMAGEPLANE_REVIEWED_PRODUCT_RESULT_CONTRACT,
)


def build_imageplane_reviewed_family_descriptor() -> object:
    """Return the complete private descriptor; registration stays deliberately separate."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedIntentFamilyDescriptor,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    return _ReviewedIntentFamilyDescriptor(
        manifest=IMAGEPLANE_REVIEWED_FAMILY_SPEC.manifest,
        subject_type_term=IMAGEPLANE_REVIEWED_FAMILY_SPEC.subject_type_term,
        adapter_factory=IMAGEPLANE_REVIEWED_FAMILY_SPEC.adapter_factory,
        validate_plan=IMAGEPLANE_REVIEWED_FAMILY_SPEC.validate_plan,
        execute_plan=IMAGEPLANE_REVIEWED_FAMILY_SPEC.execute_plan,
        product_results=(
            _ReviewedProductResultContract(
                operation_id=IMAGEPLANE_OPERATION_SPEC.operation_id,
                result_kind=_ReviewedProductResultKind.REFERENCE,
                owned_type_ids=(IMAGEPLANE_OPERATION_SPEC.native_type_id,),
                semantic_roles=(SemanticRole.SUPPORT,),
            ),
        ),
        artifact_requirement=imageplane_artifact_requirement_descriptor(),
        create_recovery=imageplane_create_recovery_descriptor(),
    )


__all__ = [
    "IMAGEPLANE_RESULT_INVARIANT",
    "IMAGEPLANE_REVIEWED_FAMILY_SPEC",
    "IMAGEPLANE_REVIEWED_PRODUCT_IDENTITIES",
    "IMAGEPLANE_REVIEWED_PRODUCT_RESULT_CONTRACT",
    "ImagePlaneOwnershipClosure",
    "ImagePlaneResultInvariant",
    "ImagePlaneReviewedArtifactContext",
    "ImagePlaneReviewedFamilySpec",
    "ImagePlaneReviewedProductResultContract",
    "build_imageplane_reviewed_family_descriptor",
    "execute_imageplane_reviewed_plan",
    "execute_imageplane_reviewed_plan_with_artifacts",
    "extract_imageplane_artifact_requirement",
    "imageplane_artifact_requirement_descriptor",
    "imageplane_create_recovery_descriptor",
    "imageplane_reviewed_adapter_factory",
    "resolve_imageplane_reviewed_operation",
    "validate_imageplane_reviewed_plan",
]
