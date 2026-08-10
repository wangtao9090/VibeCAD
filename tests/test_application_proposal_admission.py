"""Focused tests for the private A11 proposal-admission coordinator."""

from __future__ import annotations

import dataclasses
import inspect
import json
from pathlib import Path

import pytest

from tests.test_application_proposal_evidence_evaluator import _case
from vibecad.application import proposal_admission as admission_module
from vibecad.application.data import ApplicationDataLayout
from vibecad.application.proposal_admission import (
    ProposalAdmissionError,
    ProposalAdmissionErrorCode,
    admit_proposal_evidence,
    revalidate_proposal_admission,
)
from vibecad.application.proposal_evidence_evaluator import evaluate_proposal_evidence
from vibecad.runtime.contracts import RuntimeBudget, RuntimeLifecycleState
from vibecad.visual.calibration_authority import build_in_memory_planar_calibration_receipt
from vibecad.visual.drafts import (
    BaseHeadBinding,
    ProviderInvocationRecord,
    ReconstructionDraft,
    reconstruction_payload,
)
from vibecad.visual.evidence import NormalizedEvidencePoint
from vibecad.visual.proposal_coverage import derive_proposal_coverage_plan
from vibecad.visual.provider import (
    VISUAL_PROVIDER_IDENTITY,
    VISUAL_PROVIDER_MODEL,
    VISUAL_PROVIDER_MODEL_VERSION,
    build_visual_provider_invocation,
    visual_provider_input_digest,
)
from vibecad.visual.provider_images import ProviderDetailCrop, prepare_provider_image_batch
from vibecad.visual.reconstruction import (
    ReconstructionProposal,
    ReconstructionStatus,
    VisualObservation,
    reconstruction_identity,
    visual_invocation_identity,
)
from vibecad.visual.store import ReconstructionDraftStore, ReconstructionDraftStoreError
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

_CREATE_KEY = "reconstruction_create_" + "a" * 32


def _draft_store(layout: ApplicationDataLayout) -> ReconstructionDraftStore:
    return ReconstructionDraftStore(
        root=layout.reconstruction_drafts,
        expected_root_identity=layout.identity_for(layout.reconstruction_drafts),
        lease_manager=ResourceLeaseManager(
            layout.locks,
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        ),
    )


def _invocation_record(
    *,
    reconstruction_id: str,
    generation: int,
    image_set_id: str,
    manifest: str,
    answer_digests: tuple[str, ...],
) -> ProviderInvocationRecord:
    budget = RuntimeBudget(
        max_elapsed_ms=60_000,
        max_memory_bytes=64 * 1024 * 1024,
        max_output_bytes=1024 * 1024,
    )
    invocation = build_visual_provider_invocation(
        reconstruction_id=reconstruction_id,
        generation=generation,
        image_set_id=image_set_id,
        image_set_manifest_sha256=manifest,
        clarification_answer_digests=answer_digests,
        budget=budget,
        deadline_ms=2_000_000_000_000,
    )
    return ProviderInvocationRecord(
        invocation_id=invocation.invocation_id,
        attempt_generation=generation,
        runtime=VISUAL_PROVIDER_IDENTITY,
        model=VISUAL_PROVIDER_MODEL,
        model_version=VISUAL_PROVIDER_MODEL_VERSION,
        budget=budget,
        deadline_ms=invocation.deadline_ms,
        input_sha256=visual_provider_input_digest(invocation),
    )


def _succeeded(record: ProviderInvocationRecord, suffix: str) -> ProviderInvocationRecord:
    return dataclasses.replace(
        record,
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        start_receipt_sha256=suffix * 64,
        result_sha256=chr(ord(suffix) + 1) * 64,
        output_sha256=chr(ord(suffix) + 2) * 64,
    )


def _observation_for(
    template: VisualObservation,
    *,
    reconstruction_id: str,
    generation: int,
) -> VisualObservation:
    return dataclasses.replace(
        template,
        reconstruction_id=reconstruction_id,
        generation=generation,
        invocation_id=visual_invocation_identity(
            reconstruction_id,
            generation,
            template.image_set_id,
            template.image_set_manifest_sha256,
        ),
        id="",
        digest="",
    )


def _proposal_for(
    template: ReconstructionProposal,
    observation: VisualObservation,
) -> ReconstructionProposal:
    return dataclasses.replace(
        template,
        observation=observation,
        id="",
        digest="",
    )


def _durable_case(
    tmp_path: Path,
    *,
    omit_last_clarification: bool = False,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    visual_root = tmp_path / "visual"
    visual_root.mkdir()
    template, visual_store, batch, features, landmarks, basis = _case(visual_root)
    reconstruction_id, create_digest = reconstruction_identity(_CREATE_KEY)
    layout = ApplicationDataLayout.open((tmp_path / "drafts").resolve())
    store = _draft_store(layout)
    base_head = BaseHeadBinding(
        project_id="project_" + "b" * 32,
        generation=3,
        revision_id="revision_" + "c" * 32,
        manifest_sha256="d" * 64,
    )
    ready = ReconstructionDraft(
        reconstruction_id=reconstruction_id,
        create_key_sha256=create_digest,
        generation=0,
        status=ReconstructionStatus.READY,
        base_head=base_head,
        image_set_id=template.observation.image_set_id,
        image_set_manifest_sha256=template.observation.image_set_manifest_sha256,
    )
    store.create(ready)

    first_intent = _invocation_record(
        reconstruction_id=reconstruction_id,
        generation=1,
        image_set_id=ready.image_set_id,
        manifest=ready.image_set_manifest_sha256,
        answer_digests=(),
    )
    observing = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(first_intent,),
    )
    store.compare_and_set(reconstruction_id, 0, observing)
    first_observation = _observation_for(
        template.observation,
        reconstruction_id=reconstruction_id,
        generation=1,
    )
    first_observation_payload = reconstruction_payload(first_observation)
    first_receipt = _succeeded(first_intent, "1")
    needs_input = dataclasses.replace(
        observing,
        generation=2,
        status=ReconstructionStatus.NEEDS_INPUT,
        observation_ref=first_observation_payload.ref,
        provider_invocations=(first_receipt,),
    )
    store.compare_and_set(
        reconstruction_id,
        1,
        needs_input,
        (first_observation_payload,),
    )

    answer_payloads = tuple(
        reconstruction_payload(item) for item in template.clarification_answers
    )
    durable_answers = (
        answer_payloads[:-1] if omit_last_clarification else answer_payloads
    )
    ready_again = dataclasses.replace(
        needs_input,
        generation=3,
        status=ReconstructionStatus.READY,
        clarification_refs=tuple(item.ref for item in durable_answers),
    )
    store.compare_and_set(
        reconstruction_id,
        2,
        ready_again,
        durable_answers,
    )

    answer_digests = tuple(item.ref.contract_digest for item in durable_answers)
    second_intent = _invocation_record(
        reconstruction_id=reconstruction_id,
        generation=4,
        image_set_id=ready.image_set_id,
        manifest=ready.image_set_manifest_sha256,
        answer_digests=answer_digests,
    )
    observing_again = dataclasses.replace(
        ready_again,
        generation=4,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(first_receipt, second_intent),
    )
    store.compare_and_set(reconstruction_id, 3, observing_again)

    final_observation = _observation_for(
        template.observation,
        reconstruction_id=reconstruction_id,
        generation=4,
    )
    proposal = _proposal_for(template, final_observation)
    observation_payload = reconstruction_payload(final_observation)
    proposal_payload = reconstruction_payload(proposal)
    proposed = dataclasses.replace(
        observing_again,
        generation=5,
        status=ReconstructionStatus.PROPOSED,
        observation_ref=observation_payload.ref,
        proposal_ref=proposal_payload.ref,
        provider_invocations=(first_receipt, _succeeded(second_intent, "4")),
    )
    store.compare_and_set(
        reconstruction_id,
        4,
        proposed,
        (observation_payload, proposal_payload),
    )
    return (
        layout,
        store,
        proposed,
        proposal,
        visual_store,
        batch,
        features,
        landmarks,
        basis,
    )


def _admit(case):
    (
        _layout,
        store,
        draft,
        proposal,
        visual_store,
        batch,
        features,
        landmarks,
        basis,
    ) = case
    return admit_proposal_evidence(
        reconstruction_store=store,
        visual_input_store=visual_store,
        proposal=proposal,
        expected_generation=draft.generation,
        image_batch=batch,
        provider_features=features,
        calibration_landmarks=landmarks,
        metric_basis=basis,
    )


def test_complete_proposal_attaches_and_restart_revalidates_every_expected_digest(
    tmp_path: Path,
) -> None:
    case = _durable_case(tmp_path)
    (
        layout,
        _store,
        draft,
        proposal,
        visual_store,
        batch,
        features,
        landmarks,
        basis,
    ) = case

    bundle = _admit(case)
    replay = revalidate_proposal_admission(
        reconstruction_store=_draft_store(layout),
        visual_input_store=visual_store,
        reconstruction_id=draft.reconstruction_id,
        expected_generation=draft.generation,
    )

    assert replay == bundle
    report = evaluate_proposal_evidence(
        proposal=proposal,
        visual_input_store=visual_store,
        image_batch=batch,
        provider_features=features,
        calibration_landmarks=landmarks,
        metric_basis=basis,
    )
    image_set, _normalized = visual_store.read_provider_images_exact(
        proposal.observation.image_set_id,
        proposal.observation.image_set_manifest_sha256,
    )
    receipt = build_in_memory_planar_calibration_receipt(
        image_set=image_set,
        image_batch=batch,
        source_index=0,
        landmarks=landmarks,
        metric_basis=basis,
    )
    plan = derive_proposal_coverage_plan(proposal=proposal)
    assert bundle.expected.provider_batch_manifest_sha256 == batch.manifest_sha256
    assert bundle.expected.calibration_receipt_sha256 == receipt.receipt_sha256
    assert (
        bundle.expected.calibration_authority_binding_sha256
        == receipt.authority_binding_sha256
    )
    assert bundle.expected.calibration_sha256 == receipt.calibration_sha256
    assert bundle.expected.capture_quality_sha256 == report.capture_quality_sha256
    assert bundle.expected.evidence_sha256 == report.evidence_sha256
    assert bundle.expected.fit_report_sha256 == report.fit_report_sha256
    assert bundle.expected.evaluation_report_sha256 == report.digest
    assert bundle.expected.coverage_plan_sha256 == plan.digest
    assert (
        bundle.expected.expected_operation_payload_sha256
        == plan.expected_operation_payload_sha256
    )


def test_coordinator_signature_has_no_derived_or_policy_inputs() -> None:
    parameters = inspect.signature(admit_proposal_evidence).parameters

    assert tuple(parameters) == (
        "reconstruction_store",
        "visual_input_store",
        "proposal",
        "expected_generation",
        "image_batch",
        "provider_features",
        "calibration_landmarks",
        "metric_basis",
    )
    assert not {
        "receipt",
        "fit",
        "plan",
        "decision",
        "policy",
        "tolerance",
        "clarification",
    } & set(parameters)
    assert tuple(inspect.signature(revalidate_proposal_admission).parameters) == (
        "reconstruction_store",
        "visual_input_store",
        "reconstruction_id",
        "expected_generation",
    )


def test_non_complete_evidence_never_attaches_a_bundle(tmp_path: Path) -> None:
    case = _durable_case(tmp_path)
    layout, store, draft, proposal, visual_store, batch, features, landmarks, basis = case
    shifted = dataclasses.replace(
        features[0],
        points=tuple(
            NormalizedEvidencePoint(x=x, y=y)
            for x, y in ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9))
        ),
    )

    with pytest.raises(ProposalAdmissionError) as caught:
        admit_proposal_evidence(
            reconstruction_store=store,
            visual_input_store=visual_store,
            proposal=proposal,
            expected_generation=draft.generation,
            image_batch=batch,
            provider_features=(shifted,),
            calibration_landmarks=landmarks,
            metric_basis=basis,
        )
    assert caught.value.code is ProposalAdmissionErrorCode.NOT_COMPLETE
    with pytest.raises(ReconstructionDraftStoreError):
        _draft_store(layout)._load_admission_exact(
            draft.reconstruction_id,
            expected_generation=draft.generation,
            expected_proposal_ref=draft.proposal_ref,
        )


def test_alternate_crop_batch_is_rejected_before_attach(tmp_path: Path) -> None:
    case = _durable_case(tmp_path)
    layout, store, draft, proposal, visual_store, batch, features, landmarks, basis = case
    image_set, normalized = visual_store.read_provider_images_exact(
        proposal.observation.image_set_id,
        proposal.observation.image_set_manifest_sha256,
    )
    profile = dataclasses.replace(
        batch.profile,
        max_image_parts=2,
        supports_detail_crops=True,
    )
    alternate = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=normalized,
        profile=profile,
        detail_crops=(
            ProviderDetailCrop(
                source_index=0,
                left=0,
                top=0,
                right=0.5,
                bottom=0.5,
                label="alternate",
            ),
        ),
    )

    with pytest.raises(ProposalAdmissionError) as caught:
        admit_proposal_evidence(
            reconstruction_store=store,
            visual_input_store=visual_store,
            proposal=proposal,
            expected_generation=draft.generation,
            image_batch=alternate,
            provider_features=features,
            calibration_landmarks=landmarks,
            metric_basis=basis,
        )
    assert caught.value.code is ProposalAdmissionErrorCode.BINDING_MISMATCH
    with pytest.raises(ReconstructionDraftStoreError):
        _draft_store(layout)._load_admission_exact(
            draft.reconstruction_id,
            expected_generation=draft.generation,
            expected_proposal_ref=draft.proposal_ref,
        )


def test_stale_generation_and_non_durable_clarification_fail_before_attach(
    tmp_path: Path,
) -> None:
    current = _durable_case(tmp_path / "stale")
    _layout, store, draft, proposal, visual_store, batch, features, landmarks, basis = current
    with pytest.raises(ProposalAdmissionError) as stale:
        admit_proposal_evidence(
            reconstruction_store=store,
            visual_input_store=visual_store,
            proposal=proposal,
            expected_generation=draft.generation - 1,
            image_batch=batch,
            provider_features=features,
            calibration_landmarks=landmarks,
            metric_basis=basis,
        )
    assert stale.value.code is ProposalAdmissionErrorCode.BINDING_MISMATCH

    mismatch = _durable_case(
        tmp_path / "clarification",
        omit_last_clarification=True,
    )
    with pytest.raises(ProposalAdmissionError) as clarification:
        _admit(mismatch)
    assert clarification.value.code is ProposalAdmissionErrorCode.BINDING_MISMATCH
    assert clarification.value.path == "/clarification_answers"


def test_restart_expected_digest_drift_fails_closed(tmp_path: Path, monkeypatch) -> None:
    case = _durable_case(tmp_path)
    layout, _store, draft, _proposal, visual_store, *_rest = case
    bundle = _admit(case)
    changed = dataclasses.replace(
        bundle.expected,
        evidence_sha256="f" * 64,
    )
    monkeypatch.setattr(admission_module, "_evaluate_expected", lambda **_kwargs: changed)

    with pytest.raises(ProposalAdmissionError) as caught:
        revalidate_proposal_admission(
            reconstruction_store=_draft_store(layout),
            visual_input_store=visual_store,
            reconstruction_id=draft.reconstruction_id,
            expected_generation=draft.generation,
        )
    assert caught.value.code is ProposalAdmissionErrorCode.DRIFT_DETECTED
    assert caught.value.path == "/expected"


def test_restart_unknown_sidecar_version_fails_closed(tmp_path: Path) -> None:
    case = _durable_case(tmp_path)
    layout, _store, draft, _proposal, visual_store, *_rest = case
    _admit(case)
    directory = layout.reconstruction_drafts / draft.reconstruction_id
    sidecar = next(directory.glob("admission_inputs_*.json"))
    mapping = json.loads(sidecar.read_bytes())
    mapping["schema_version"] = 2
    sidecar.write_bytes(
        json.dumps(mapping, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "ascii"
        )
    )

    with pytest.raises(ProposalAdmissionError) as caught:
        revalidate_proposal_admission(
            reconstruction_store=_draft_store(layout),
            visual_input_store=visual_store,
            reconstruction_id=draft.reconstruction_id,
            expected_generation=draft.generation,
        )
    assert caught.value.code is ProposalAdmissionErrorCode.STORE_FAILURE


def test_no_public_service_task_or_mcp_wiring() -> None:
    source = Path(admission_module.__file__).read_text(encoding="utf-8")

    assert "vibecad.visual.service" not in source
    assert "vibecad.service" not in source
    assert "vibecad.mcp" not in source
    assert "TaskStore" not in source
    assert admission_module.__all__ == ()
