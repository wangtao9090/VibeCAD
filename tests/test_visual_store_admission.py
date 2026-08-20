from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.test_visual_service import _proposal
from vibecad._file_compat import set_private_dacl
from vibecad.application.data import ApplicationDataLayout
from vibecad.runtime.contracts import RuntimeBudget, RuntimeLifecycleState
from vibecad.visual.admission_inputs import (
    AdmissionExpectedDigests,
    AdmissionImageSetRef,
    VisualAdmissionInputBundle,
)
from vibecad.visual.calibration_authority import (
    ConfirmedPlanarLandmark,
    ConfirmedPlanarMetricBasis,
)
from vibecad.visual.drafts import (
    BaseHeadBinding,
    DeleteCleanup,
    ProviderInvocationRecord,
    ReconstructionDraft,
    ReconstructionLastError,
    derive_adoption_identity,
    encode_reconstruction_draft,
    reconstruction_payload,
)
from vibecad.visual.evidence import NormalizedEvidencePoint, ProviderFeatureEvidence
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.provider import (
    VISUAL_PROVIDER_IDENTITY,
    VISUAL_PROVIDER_MODEL,
    VISUAL_PROVIDER_MODEL_VERSION,
    build_visual_provider_invocation,
    visual_provider_input_digest,
)
from vibecad.visual.provider_images import (
    ProviderImageDetail,
    VisualProviderCapabilityProfile,
)
from vibecad.visual.reconstruction import (
    ReconstructionProposal,
    ReconstructionStatus,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    reconstruction_identity,
)
from vibecad.visual.store import (
    ReconstructionDraftStore,
    ReconstructionDraftStoreError,
    ReconstructionDraftStoreErrorCode,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _layout(tmp_path: Path) -> ApplicationDataLayout:
    return ApplicationDataLayout.open(tmp_path.resolve() / "data")


def _store(layout: ApplicationDataLayout) -> ReconstructionDraftStore:
    return ReconstructionDraftStore(
        root=layout.reconstruction_drafts,
        expected_root_identity=layout.identity_for(layout.reconstruction_drafts),
        lease_manager=ResourceLeaseManager(
            layout.locks,
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        ),
    )


def _ready() -> ReconstructionDraft:
    reconstruction_id, create_digest = reconstruction_identity("reconstruction_create_" + "1" * 32)
    return ReconstructionDraft(
        reconstruction_id=reconstruction_id,
        create_key_sha256=create_digest,
        generation=0,
        status=ReconstructionStatus.READY,
        base_head=BaseHeadBinding(
            project_id="project_" + "2" * 32,
            generation=3,
            revision_id="revision_" + "4" * 32,
            manifest_sha256="5" * 64,
        ),
        image_set_id="image_set_" + "6" * 32,
        image_set_manifest_sha256="7" * 64,
    )


def _intent(ready: ReconstructionDraft) -> ProviderInvocationRecord:
    budget = RuntimeBudget(
        max_elapsed_ms=1_000,
        max_memory_bytes=64 * 1024 * 1024,
        max_output_bytes=1024 * 1024,
    )
    invocation = build_visual_provider_invocation(
        reconstruction_id=ready.reconstruction_id,
        generation=1,
        image_set_id=ready.image_set_id,
        image_set_manifest_sha256=ready.image_set_manifest_sha256,
        budget=budget,
        deadline_ms=2_000_000_000_000,
    )
    return ProviderInvocationRecord(
        invocation_id=invocation.invocation_id,
        attempt_generation=1,
        runtime=VISUAL_PROVIDER_IDENTITY,
        model=VISUAL_PROVIDER_MODEL,
        model_version=VISUAL_PROVIDER_MODEL_VERSION,
        budget=budget,
        deadline_ms=invocation.deadline_ms,
        input_sha256=visual_provider_input_digest(invocation),
    )


def _observation(ready: ReconstructionDraft, invocation_id: str) -> VisualObservation:
    return VisualObservation(
        reconstruction_id=ready.reconstruction_id,
        generation=1,
        image_set_id=ready.image_set_id,
        image_set_manifest_sha256=ready.image_set_manifest_sha256,
        invocation_id=invocation_id,
        claims=(
            VisualClaim(
                name="overall.depth",
                status=VisualClaimStatus.CONFIRMED,
                source_indices=(0,),
                value=8,
                unit=VisualClaimUnit.MM,
                description="Fixture depth",
            ),
        ),
    )


def _proposed(
    store: ReconstructionDraftStore,
) -> tuple[ReconstructionDraft, ReconstructionProposal]:
    ready = _ready()
    store.create(ready)
    intent = _intent(ready)
    observing = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(intent,),
    )
    store.compare_and_set(ready.reconstruction_id, 0, observing)
    observation = _observation(ready, intent.invocation_id)
    proposal = _proposal(observation)
    observation_payload = reconstruction_payload(observation)
    proposal_payload = reconstruction_payload(proposal)
    receipt = dataclasses.replace(
        intent,
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        start_receipt_sha256="8" * 64,
        result_sha256="9" * 64,
        output_sha256="a" * 64,
    )
    proposed = dataclasses.replace(
        observing,
        generation=2,
        status=ReconstructionStatus.PROPOSED,
        observation_ref=observation_payload.ref,
        proposal_ref=proposal_payload.ref,
        provider_invocations=(receipt,),
    )
    store.compare_and_set(
        ready.reconstruction_id,
        1,
        proposed,
        (observation_payload, proposal_payload),
    )
    return proposed, proposal


def _profile() -> VisualProviderCapabilityProfile:
    return VisualProviderCapabilityProfile(
        provider="candidate",
        model="vision-model",
        model_version="2026-08-08",
        data_policy_profile="personal-default",
        max_source_images=1,
        max_image_parts=1,
        max_image_bytes=2 * 1024 * 1024,
        max_batch_image_bytes=2 * 1024 * 1024,
        preferred_long_edge=512,
        max_long_edge=512,
        detail=ProviderImageDetail.HIGH,
        supports_detail_crops=False,
        transport_timeout_ms=120_000,
    )


def _landmarks() -> tuple[ConfirmedPlanarLandmark, ...]:
    values = (
        ("origin", 0.0, 0.0, 0.0, 0.0),
        ("positive-x", 1.0, 0.0, 100.0, 0.0),
        ("positive-y", 0.0, 1.0, 0.0, 100.0),
        ("opposite", 1.0, 1.0, 100.0, 100.0),
    )
    return tuple(
        ConfirmedPlanarLandmark(
            landmark_id=identifier,
            confirmation_id=f"confirm-{identifier}",
            normalized_x=x,
            normalized_y=y,
            localization_uncertainty_norm=0.001,
            x_mm=x_mm,
            y_mm=y_mm,
            plane_uncertainty_mm=0.01,
        )
        for identifier, x, y, x_mm, y_mm in values
    )


def _bundle(draft: ReconstructionDraft, **changes: object) -> VisualAdmissionInputBundle:
    claim_id = _observation(_ready(), _intent(_ready()).invocation_id).claims[0].id
    values: dict[str, object] = {
        "reconstruction_id": draft.reconstruction_id,
        "base_head_sha256": draft.base_head.sha256,
        "observation_ref": draft.observation_ref,
        "proposal_ref": draft.proposal_ref,
        "image_set_ref": AdmissionImageSetRef(
            image_set_id=draft.image_set_id,
            manifest_sha256=draft.image_set_manifest_sha256,
        ),
        "source_index": 0,
        "provider_profile": _profile(),
        "provider_features": (
            ProviderFeatureEvidence(
                local_feature_id="rectangle",
                source_index=0,
                provider_image_id="provider_image_" + "b" * 32,
                family=PrimitiveFamily.ROTATED_RECTANGLE,
                points=tuple(
                    NormalizedEvidencePoint(x=x, y=y)
                    for x, y in ((0.2, 0.2), (0.8, 0.2), (0.8, 0.7), (0.2, 0.7))
                ),
                localization_uncertainty_norm=0.001,
                claim_ids=(claim_id,),
            ),
        ),
        "calibration_landmarks": _landmarks(),
        "metric_basis": ConfirmedPlanarMetricBasis(
            frame_id="front-plane",
            confirmation_id="confirm-basis",
            origin_landmark_id="origin",
            positive_x_landmark_id="positive-x",
            positive_y_landmark_id="positive-y",
        ),
        "expected": AdmissionExpectedDigests(
            **{name: _sha(name) for name in AdmissionExpectedDigests.__dataclass_fields__}
        ),
    }
    values.update(changes)
    return VisualAdmissionInputBundle(**values)


def test_epoch_one_attach_load_restart_and_public_draft_wire_is_unchanged(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    proposed, _proposal_value = _proposed(store)
    bundle = _bundle(proposed)
    record_path = layout.reconstruction_drafts / proposed.reconstruction_id / "record.json"

    assert record_path.read_bytes() == encode_reconstruction_draft(proposed)
    assert (
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=proposed.generation,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=bundle,
        )
        == bundle
    )

    assert store.load(proposed.reconstruction_id) == proposed
    assert (
        _store(layout)._load_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=proposed.generation,
            expected_proposal_ref=proposed.proposal_ref,
        )
        == bundle
    )
    stored_mapping = json.loads(record_path.read_bytes())
    assert stored_mapping["storage_epoch"] == 2
    assert "admission_ref" not in json.loads(encode_reconstruction_draft(proposed))


def test_attach_and_load_are_generation_proposal_and_bundle_exact(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    proposed, _proposal_value = _proposed(store)
    bundle = _bundle(proposed)

    with pytest.raises(ReconstructionDraftStoreError) as stale:
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=proposed.generation - 1,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=bundle,
        )
    assert stale.value.code is ReconstructionDraftStoreErrorCode.CONFLICT

    store._attach_admission_exact(  # noqa: SLF001
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
        expected_proposal_ref=proposed.proposal_ref,
        bundle=bundle,
    )
    assert (
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=proposed.generation,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=bundle,
        )
        == bundle
    )

    changed_expected = dataclasses.replace(
        bundle.expected,
        fit_report_sha256=_sha("changed-fit"),
    )
    with pytest.raises(ReconstructionDraftStoreError) as changed:
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=proposed.generation,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=dataclasses.replace(bundle, expected=changed_expected),
        )
    assert changed.value.code is ReconstructionDraftStoreErrorCode.CONFLICT

    with pytest.raises(ReconstructionDraftStoreError) as stale_load:
        store._load_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=proposed.generation + 1,
            expected_proposal_ref=proposed.proposal_ref,
        )
    assert stale_load.value.code is ReconstructionDraftStoreErrorCode.CONFLICT


def test_admission_is_preserved_for_adoption_and_removed_on_rejection(tmp_path: Path) -> None:
    adopting_layout = _layout(tmp_path / "adopting")
    adopting_store = _store(adopting_layout)
    proposed, _proposal_value = _proposed(adopting_store)
    bundle = _bundle(proposed)
    adopting_store._attach_admission_exact(  # noqa: SLF001
        proposed.reconstruction_id,
        expected_generation=2,
        expected_proposal_ref=proposed.proposal_ref,
        bundle=bundle,
    )
    adoption_key, adoption_intent = derive_adoption_identity(
        proposed.reconstruction_id,
        proposed.proposal_ref.contract_digest,
        proposed.base_head.sha256,
    )
    adopting = dataclasses.replace(
        proposed,
        generation=3,
        status=ReconstructionStatus.ADOPTING,
        adoption_key_sha256=adoption_key,
        adoption_intent_sha256=adoption_intent,
    )
    adopting_store.compare_and_set(proposed.reconstruction_id, 2, adopting)
    assert (
        adopting_store._load_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=3,
            expected_proposal_ref=proposed.proposal_ref,
        )
        == bundle
    )
    recovery = dataclasses.replace(
        adopting,
        generation=4,
        status=ReconstructionStatus.RECOVERY_REQUIRED,
        last_error=ReconstructionLastError(
            code="adoption.unknown",
            phase="reconcile",
            retryable=False,
            diagnostic_digest=_sha("adoption-recovery"),
        ),
    )
    adopting_store.compare_and_set(proposed.reconstruction_id, 3, recovery)
    reconciled = dataclasses.replace(
        recovery,
        generation=5,
        status=ReconstructionStatus.PROPOSED,
        adoption_key_sha256=None,
        adoption_intent_sha256=None,
        last_error=None,
    )
    adopting_store.compare_and_set(proposed.reconstruction_id, 4, reconciled)
    assert (
        adopting_store._load_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=5,
            expected_proposal_ref=proposed.proposal_ref,
        )
        == bundle
    )

    rejected_layout = _layout(tmp_path / "rejected")
    rejected_store = _store(rejected_layout)
    proposed, _proposal_value = _proposed(rejected_store)
    rejected_store._attach_admission_exact(  # noqa: SLF001
        proposed.reconstruction_id,
        expected_generation=2,
        expected_proposal_ref=proposed.proposal_ref,
        bundle=_bundle(proposed),
    )
    rejected = dataclasses.replace(
        proposed,
        generation=3,
        status=ReconstructionStatus.REJECTED,
    )
    rejected_store.compare_and_set(proposed.reconstruction_id, 2, rejected)
    assert rejected_store.load(proposed.reconstruction_id) == rejected
    with pytest.raises(ReconstructionDraftStoreError) as missing:
        rejected_store._load_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=3,
            expected_proposal_ref=proposed.proposal_ref,
        )
    assert missing.value.code is ReconstructionDraftStoreErrorCode.NOT_FOUND
    names = {
        item.name
        for item in (rejected_layout.reconstruction_drafts / proposed.reconstruction_id).iterdir()
    }
    assert not any(name.startswith("admission_inputs_") for name in names)

    deleted_layout = _layout(tmp_path / "deleted")
    deleted_store = _store(deleted_layout)
    proposed, _proposal_value = _proposed(deleted_store)
    deleted_store._attach_admission_exact(  # noqa: SLF001
        proposed.reconstruction_id,
        expected_generation=2,
        expected_proposal_ref=proposed.proposal_ref,
        bundle=_bundle(proposed),
    )
    deleted = ReconstructionDraft(
        reconstruction_id=proposed.reconstruction_id,
        create_key_sha256=proposed.create_key_sha256,
        generation=3,
        status=ReconstructionStatus.DELETED,
        base_head=None,
        image_set_id=None,
        image_set_manifest_sha256=None,
        delete_cleanup=DeleteCleanup(
            image_set_id=proposed.image_set_id,
            image_set_manifest_sha256=proposed.image_set_manifest_sha256,
            payload_refs=proposed.payload_refs,
        ),
    )
    deleted_store.compare_and_set(proposed.reconstruction_id, 2, deleted)
    assert deleted_store.load(proposed.reconstruction_id) == deleted
    assert not any(
        item.name.startswith("admission_inputs_")
        for item in (deleted_layout.reconstruction_drafts / proposed.reconstruction_id).iterdir()
    )


def test_pre_replace_failure_rolls_back_admission_and_epoch_two_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.store as store_module

    layout = _layout(tmp_path)
    store = _store(layout)
    proposed, _proposal_value = _proposed(store)
    epoch_one = encode_reconstruction_draft(proposed)

    def fail_append(*args, **kwargs):
        raise ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)

    monkeypatch.setattr(store_module, "_append_staged_journal", fail_append)
    with pytest.raises(ReconstructionDraftStoreError) as failed:
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=2,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=_bundle(proposed),
        )
    assert failed.value.code is ReconstructionDraftStoreErrorCode.IO_ERROR
    directory = layout.reconstruction_drafts / proposed.reconstruction_id
    assert (directory / "record.json").read_bytes() == epoch_one
    assert not any(item.name.startswith("admission_inputs_") for item in directory.iterdir())


def test_reserved_full_payload_and_record_temp_roll_forward_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.store as store_module

    layout = _layout(tmp_path)
    store = _store(layout)
    proposed, _proposal_value = _proposed(store)
    bundle = _bundle(proposed)
    original_append = store_module._append_staged_journal
    original_rollback = ReconstructionDraftStore._rollback_failed_cas_locked

    def fail_append(*args, **kwargs):
        raise ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)

    monkeypatch.setattr(store_module, "_append_staged_journal", fail_append)
    monkeypatch.setattr(
        ReconstructionDraftStore,
        "_rollback_failed_cas_locked",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(ReconstructionDraftStoreError) as uncertain:
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=2,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=bundle,
        )
    assert uncertain.value.code is ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN

    monkeypatch.setattr(store_module, "_append_staged_journal", original_append)
    monkeypatch.setattr(
        ReconstructionDraftStore,
        "_rollback_failed_cas_locked",
        original_rollback,
    )
    assert (
        _store(layout)._load_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=2,
            expected_proposal_ref=proposed.proposal_ref,
        )
        == bundle
    )


def test_post_replace_failure_recovers_admission_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    proposed, _proposal_value = _proposed(store)
    bundle = _bundle(proposed)
    original_cleanup = ReconstructionDraftStore._finish_committed_cleanup

    def fail_cleanup(self, draft_fd, journal, successor):
        raise ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)

    monkeypatch.setattr(ReconstructionDraftStore, "_finish_committed_cleanup", fail_cleanup)
    with pytest.raises(ReconstructionDraftStoreError) as uncertain:
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=2,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=bundle,
        )
    assert uncertain.value.code is ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN
    assert uncertain.value.committed_generation == 2

    monkeypatch.setattr(ReconstructionDraftStore, "_finish_committed_cleanup", original_cleanup)
    assert (
        _store(layout)._load_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=2,
            expected_proposal_ref=proposed.proposal_ref,
        )
        == bundle
    )


def test_admission_tamper_and_orphan_files_fail_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    proposed, _proposal_value = _proposed(store)
    bundle = _bundle(proposed)
    store._attach_admission_exact(  # noqa: SLF001
        proposed.reconstruction_id,
        expected_generation=2,
        expected_proposal_ref=proposed.proposal_ref,
        bundle=bundle,
    )
    directory = layout.reconstruction_drafts / proposed.reconstruction_id
    admission_path = next(directory.glob("admission_inputs_*.json"))
    tampered = bytearray(admission_path.read_bytes())
    tampered[-2] ^= 1
    admission_path.write_bytes(tampered)
    with pytest.raises(ReconstructionDraftStoreError) as corrupt:
        _store(layout).load(proposed.reconstruction_id)
    assert corrupt.value.code is ReconstructionDraftStoreErrorCode.CORRUPT_RECORD

    orphan_layout = _layout(tmp_path / "orphan")
    orphan_store = _store(orphan_layout)
    orphan_proposed, _proposal_value = _proposed(orphan_store)
    orphan = (
        orphan_layout.reconstruction_drafts
        / orphan_proposed.reconstruction_id
        / ("admission_inputs_" + "f" * 32 + ".json")
    )
    orphan.write_bytes(b"{}")
    orphan.chmod(0o600)
    if os.name == "nt":
        set_private_dacl(orphan)
    with pytest.raises(ReconstructionDraftStoreError) as unexpected:
        orphan_store.load(orphan_proposed.reconstruction_id)
    assert unexpected.value.code is ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED


def test_admission_peak_budget_has_exact_n_and_n_plus_one_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.store as store_module

    layout = _layout(tmp_path)
    store = _store(layout)
    proposed, _proposal_value = _proposed(store)
    bundle = _bundle(proposed)
    bundle_raw = store_module.encode_visual_admission_inputs(bundle)
    reference = store_module._admission_ref_for(  # noqa: SLF001
        bundle,
        admitted_generation=proposed.generation,
        raw=bundle_raw,
    )
    new_raw = store_module._encode_record(  # noqa: SLF001
        store_module._StoredDraftRecord(  # noqa: SLF001
            draft=proposed,
            admission_ref=reference,
        )
    )
    directory = layout.reconstruction_drafts / proposed.reconstruction_id
    current_bytes = sum(item.stat().st_size for item in directory.iterdir())
    exact_peak = current_bytes + len(new_raw) + len(bundle_raw) + store_module._MAX_JOURNAL_BYTES

    monkeypatch.setattr(
        store_module,
        "MAX_RECONSTRUCTION_DRAFT_STORE_BYTES",
        exact_peak - 1,
    )
    with pytest.raises(ReconstructionDraftStoreError) as overflow:
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=2,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=bundle,
        )
    assert overflow.value.code is ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED
    assert (directory / "record.json").read_bytes() == encode_reconstruction_draft(proposed)

    monkeypatch.setattr(
        store_module,
        "MAX_RECONSTRUCTION_DRAFT_STORE_BYTES",
        exact_peak,
    )
    assert (
        store._attach_admission_exact(  # noqa: SLF001
            proposed.reconstruction_id,
            expected_generation=2,
            expected_proposal_ref=proposed.proposal_ref,
            bundle=bundle,
        )
        == bundle
    )
