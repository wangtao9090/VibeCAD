"""Private coordinator for attaching and revalidating A11 admission inputs.

This boundary admits only one exact, fully recomputed COMPLETE proposal.  It
never accepts caller-created receipts, fits, plans, decisions, policies, or
tolerances, and it grants no Task, adoption, service, or MCP authority.
"""

from __future__ import annotations

from enum import StrEnum

from vibecad.application.proposal_evidence_evaluator import (
    ProposalEvidenceDecision,
    ProposalEvidenceEvaluationError,
    ProposalEvidenceEvaluationErrorCode,
    evaluate_proposal_evidence,
)
from vibecad.visual.admission_inputs import (
    AdmissionExpectedDigests,
    AdmissionImageSetRef,
    VisualAdmissionInputBundle,
    VisualAdmissionInputError,
)
from vibecad.visual.calibration_authority import (
    CalibrationAuthorityError,
    ConfirmedPlanarLandmark,
    ConfirmedPlanarMetricBasis,
    build_in_memory_planar_calibration_receipt,
)
from vibecad.visual.drafts import ReconstructionDraft, reconstruction_payload
from vibecad.visual.evidence import ProviderFeatureEvidence
from vibecad.visual.inputs import VisualInputStore, VisualInputStoreError
from vibecad.visual.proposal_coverage import (
    ProposalCoverageError,
    derive_proposal_coverage_plan,
)
from vibecad.visual.provider_images import (
    ProviderImageBatch,
    ProviderImageError,
    prepare_provider_image_batch,
)
from vibecad.visual.reconstruction import (
    ReconstructionContractError,
    ReconstructionProposal,
    ReconstructionStatus,
    decode_reconstruction_proposal,
)
from vibecad.visual.store import (
    ReconstructionDraftStore,
    ReconstructionDraftStoreError,
    ReconstructionDraftStoreErrorCode,
)
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER

_MAX_ERROR_PATH_BYTES = 256


class ProposalAdmissionErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BINDING_MISMATCH = "binding_mismatch"
    NOT_COMPLETE = "not_complete"
    DRIFT_DETECTED = "drift_detected"
    INTEGRITY_FAILURE = "integrity_failure"
    STORE_FAILURE = "store_failure"


class ProposalAdmissionError(RuntimeError):
    """Bounded fail-closed application error with no persisted value reflection."""

    def __init__(self, code: ProposalAdmissionErrorCode, path: str = "") -> None:
        if type(code) is not ProposalAdmissionErrorCode:
            raise TypeError("code must be an exact ProposalAdmissionErrorCode")
        if type(path) is not str:
            raise TypeError("path must be a string")
        try:
            encoded = path.encode("utf-8")
        except UnicodeError:
            raise ValueError("path must be bounded") from None
        if len(encoded) > _MAX_ERROR_PATH_BYTES:
            raise ValueError("path must be bounded")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: ProposalAdmissionErrorCode, path: str = "") -> None:
    raise ProposalAdmissionError(code, path)


def _generation(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_JSON_INTEGER:
        _fail(ProposalAdmissionErrorCode.INVALID_INPUT, "/expected_generation")
    return value


def _store_failure(error: ReconstructionDraftStoreError) -> None:
    code = (
        ProposalAdmissionErrorCode.BINDING_MISMATCH
        if error.code
        in {
            ReconstructionDraftStoreErrorCode.INVALID_ID,
            ReconstructionDraftStoreErrorCode.NOT_FOUND,
            ReconstructionDraftStoreErrorCode.CONFLICT,
        }
        else ProposalAdmissionErrorCode.STORE_FAILURE
    )
    _fail(code, "/reconstruction_store")


def _load_expected_draft(
    reconstruction_store: ReconstructionDraftStore,
    reconstruction_id: str,
    expected_generation: int,
) -> ReconstructionDraft:
    try:
        draft = reconstruction_store.load(reconstruction_id)
    except ReconstructionDraftStoreError as error:
        _store_failure(error)
    if (
        draft.generation != expected_generation
        or draft.status is not ReconstructionStatus.PROPOSED
        or draft.base_head is None
        or draft.observation_ref is None
        or draft.proposal_ref is None
        or draft.image_set_id is None
        or draft.image_set_manifest_sha256 is None
    ):
        _fail(ProposalAdmissionErrorCode.BINDING_MISMATCH, "/draft")
    return draft


def _validate_proposal_binding(
    draft: ReconstructionDraft,
    proposal: ReconstructionProposal,
) -> None:
    try:
        proposal_ref = reconstruction_payload(proposal).ref
        observation_ref = reconstruction_payload(proposal.observation).ref
    except (TypeError, ValueError):
        _fail(ProposalAdmissionErrorCode.INVALID_INPUT, "/proposal")
    if (
        proposal.observation.reconstruction_id != draft.reconstruction_id
        or proposal.observation.image_set_id != draft.image_set_id
        or proposal.observation.image_set_manifest_sha256
        != draft.image_set_manifest_sha256
        or proposal_ref != draft.proposal_ref
        or observation_ref != draft.observation_ref
    ):
        _fail(ProposalAdmissionErrorCode.BINDING_MISMATCH, "/proposal")
    durable_answer_digests = {
        reference.contract_digest for reference in draft.clarification_refs
    }
    proposal_answer_digests = {
        answer.digest for answer in proposal.clarification_answers
    }
    if proposal_answer_digests != durable_answer_digests:
        _fail(ProposalAdmissionErrorCode.BINDING_MISMATCH, "/clarification_answers")


def _evaluate_expected(
    *,
    proposal: ReconstructionProposal,
    visual_input_store: VisualInputStore,
    image_batch: ProviderImageBatch,
    provider_features: tuple[ProviderFeatureEvidence, ...],
    calibration_landmarks: tuple[ConfirmedPlanarLandmark, ...],
    metric_basis: ConfirmedPlanarMetricBasis,
) -> AdmissionExpectedDigests:
    try:
        report = evaluate_proposal_evidence(
            proposal=proposal,
            visual_input_store=visual_input_store,
            image_batch=image_batch,
            provider_features=provider_features,
            calibration_landmarks=calibration_landmarks,
            metric_basis=metric_basis,
        )
    except ProposalEvidenceEvaluationError as error:
        code = (
            ProposalAdmissionErrorCode.BINDING_MISMATCH
            if error.code is ProposalEvidenceEvaluationErrorCode.BINDING_MISMATCH
            else ProposalAdmissionErrorCode.INVALID_INPUT
        )
        _fail(code, error.path)
    except VisualInputStoreError:
        _fail(ProposalAdmissionErrorCode.STORE_FAILURE, "/visual_input_store")
    except (ProviderImageError, CalibrationAuthorityError, ProposalCoverageError, ValueError):
        _fail(ProposalAdmissionErrorCode.INVALID_INPUT, "/evaluator")
    if report.decision is not ProposalEvidenceDecision.COMPLETE:
        _fail(ProposalAdmissionErrorCode.NOT_COMPLETE, "/decision")
    try:
        image_set, normalized_bytes = visual_input_store.read_provider_images_exact(
            proposal.observation.image_set_id,
            proposal.observation.image_set_manifest_sha256,
        )
        expected_batch = prepare_provider_image_batch(
            image_set=image_set,
            normalized_images=normalized_bytes,
            profile=image_batch.profile,
            detail_crops=(),
        )
        source_indices = {item.source_index for item in provider_features}
        if len(source_indices) != 1:
            _fail(ProposalAdmissionErrorCode.INTEGRITY_FAILURE, "/provider_features")
        source_index = next(iter(source_indices))
        receipt = build_in_memory_planar_calibration_receipt(
            image_set=image_set,
            image_batch=expected_batch,
            source_index=source_index,
            landmarks=calibration_landmarks,
            metric_basis=metric_basis,
        )
        plan = derive_proposal_coverage_plan(proposal=proposal)
    except ProposalAdmissionError:
        raise
    except VisualInputStoreError:
        _fail(ProposalAdmissionErrorCode.STORE_FAILURE, "/visual_input_store")
    except (ProviderImageError, CalibrationAuthorityError, ProposalCoverageError):
        _fail(ProposalAdmissionErrorCode.INTEGRITY_FAILURE, "/recomputed")
    batch_bytes_exact = len(expected_batch.parts) == len(image_batch.parts) and all(
        left.data == right.data
        for left, right in zip(expected_batch.parts, image_batch.parts, strict=True)
    )
    if (
        expected_batch != image_batch
        or not batch_bytes_exact
        or report.calibration_receipt_sha256s != (receipt.receipt_sha256,)
        or report.coverage_plan_digest != plan.digest
    ):
        _fail(ProposalAdmissionErrorCode.INTEGRITY_FAILURE, "/recomputed")
    return AdmissionExpectedDigests(
        provider_batch_manifest_sha256=expected_batch.manifest_sha256,
        calibration_receipt_sha256=receipt.receipt_sha256,
        calibration_authority_binding_sha256=receipt.authority_binding_sha256,
        calibration_sha256=receipt.calibration_sha256,
        capture_quality_sha256=report.capture_quality_sha256,
        evidence_sha256=report.evidence_sha256,
        fit_report_sha256=report.fit_report_sha256,
        evaluation_report_sha256=report.digest,
        coverage_plan_sha256=plan.digest,
        expected_operation_payload_sha256=plan.expected_operation_payload_sha256,
    )


def admit_proposal_evidence(
    *,
    reconstruction_store: ReconstructionDraftStore,
    visual_input_store: VisualInputStore,
    proposal: ReconstructionProposal,
    expected_generation: int,
    image_batch: ProviderImageBatch,
    provider_features: tuple[ProviderFeatureEvidence, ...],
    calibration_landmarks: tuple[ConfirmedPlanarLandmark, ...],
    metric_basis: ConfirmedPlanarMetricBasis,
) -> VisualAdmissionInputBundle:
    """Recompute one COMPLETE proposal and atomically attach its raw input bundle."""

    if (
        type(reconstruction_store) is not ReconstructionDraftStore
        or type(visual_input_store) is not VisualInputStore
        or type(proposal) is not ReconstructionProposal
        or type(image_batch) is not ProviderImageBatch
        or type(provider_features) is not tuple
        or any(type(item) is not ProviderFeatureEvidence for item in provider_features)
        or type(calibration_landmarks) is not tuple
        or any(type(item) is not ConfirmedPlanarLandmark for item in calibration_landmarks)
        or type(metric_basis) is not ConfirmedPlanarMetricBasis
    ):
        _fail(ProposalAdmissionErrorCode.INVALID_INPUT)
    selected_generation = _generation(expected_generation)
    draft = _load_expected_draft(
        reconstruction_store,
        proposal.observation.reconstruction_id,
        selected_generation,
    )
    _validate_proposal_binding(draft, proposal)
    expected = _evaluate_expected(
        proposal=proposal,
        visual_input_store=visual_input_store,
        image_batch=image_batch,
        provider_features=provider_features,
        calibration_landmarks=calibration_landmarks,
        metric_basis=metric_basis,
    )
    source_indices = {item.source_index for item in provider_features}
    if len(source_indices) != 1:
        _fail(ProposalAdmissionErrorCode.INVALID_INPUT, "/provider_features")
    try:
        bundle = VisualAdmissionInputBundle(
            reconstruction_id=draft.reconstruction_id,
            base_head_sha256=draft.base_head.sha256,
            observation_ref=draft.observation_ref,
            proposal_ref=draft.proposal_ref,
            image_set_ref=AdmissionImageSetRef(
                image_set_id=draft.image_set_id,
                manifest_sha256=draft.image_set_manifest_sha256,
            ),
            source_index=next(iter(source_indices)),
            provider_profile=image_batch.profile,
            provider_features=provider_features,
            calibration_landmarks=calibration_landmarks,
            metric_basis=metric_basis,
            expected=expected,
        )
    except VisualAdmissionInputError:
        _fail(ProposalAdmissionErrorCode.INTEGRITY_FAILURE, "/bundle")
    try:
        return reconstruction_store._attach_admission_exact(
            draft.reconstruction_id,
            expected_generation=selected_generation,
            expected_proposal_ref=draft.proposal_ref,
            bundle=bundle,
        )
    except ReconstructionDraftStoreError as error:
        _store_failure(error)


def revalidate_proposal_admission(
    *,
    reconstruction_store: ReconstructionDraftStore,
    visual_input_store: VisualInputStore,
    reconstruction_id: str,
    expected_generation: int,
) -> VisualAdmissionInputBundle:
    """Reload exact raw inputs and fail closed unless every derived digest replays."""

    if (
        type(reconstruction_store) is not ReconstructionDraftStore
        or type(visual_input_store) is not VisualInputStore
        or type(reconstruction_id) is not str
    ):
        _fail(ProposalAdmissionErrorCode.INVALID_INPUT)
    selected_generation = _generation(expected_generation)
    draft = _load_expected_draft(
        reconstruction_store,
        reconstruction_id,
        selected_generation,
    )
    try:
        bundle = reconstruction_store._load_admission_exact(
            draft.reconstruction_id,
            expected_generation=selected_generation,
            expected_proposal_ref=draft.proposal_ref,
        )
        payload = reconstruction_store.load_payload(
            draft.reconstruction_id,
            draft.proposal_ref,
        )
    except ReconstructionDraftStoreError as error:
        _store_failure(error)
    try:
        proposal = decode_reconstruction_proposal(payload.raw)
    except (ReconstructionContractError, TypeError, ValueError):
        _fail(ProposalAdmissionErrorCode.INTEGRITY_FAILURE, "/proposal")
    _validate_proposal_binding(draft, proposal)
    try:
        image_set, normalized_bytes = visual_input_store.read_provider_images_exact(
            bundle.image_set_ref.image_set_id,
            bundle.image_set_ref.manifest_sha256,
        )
        image_batch = prepare_provider_image_batch(
            image_set=image_set,
            normalized_images=normalized_bytes,
            profile=bundle.provider_profile,
            detail_crops=(),
        )
    except VisualInputStoreError:
        _fail(ProposalAdmissionErrorCode.STORE_FAILURE, "/visual_input_store")
    except ProviderImageError:
        _fail(ProposalAdmissionErrorCode.DRIFT_DETECTED, "/image_batch")
    try:
        actual = _evaluate_expected(
            proposal=proposal,
            visual_input_store=visual_input_store,
            image_batch=image_batch,
            provider_features=bundle.provider_features,
            calibration_landmarks=bundle.calibration_landmarks,
            metric_basis=bundle.metric_basis,
        )
    except ProposalAdmissionError as error:
        if error.code in {
            ProposalAdmissionErrorCode.NOT_COMPLETE,
            ProposalAdmissionErrorCode.INVALID_INPUT,
            ProposalAdmissionErrorCode.BINDING_MISMATCH,
            ProposalAdmissionErrorCode.INTEGRITY_FAILURE,
        }:
            _fail(ProposalAdmissionErrorCode.DRIFT_DETECTED, error.path)
        raise
    if actual != bundle.expected:
        _fail(ProposalAdmissionErrorCode.DRIFT_DETECTED, "/expected")
    final_draft = _load_expected_draft(
        reconstruction_store,
        reconstruction_id,
        selected_generation,
    )
    if final_draft != draft:
        _fail(ProposalAdmissionErrorCode.BINDING_MISMATCH, "/draft")
    return bundle


__all__: tuple[str, ...] = ()
