from __future__ import annotations

import dataclasses
import hashlib
import os
import stat
from pathlib import Path

import pytest

from vibecad._file_compat import capture_windows_path, set_private_dacl
from vibecad.application.data import ApplicationDataLayout
from vibecad.runtime.contracts import RuntimeBudget, RuntimeLifecycleState
from vibecad.visual.drafts import (
    BaseHeadBinding,
    DeleteCleanup,
    ProviderInvocationRecord,
    ReconstructionDraft,
    ReconstructionPayload,
    encode_reconstruction_draft,
    reconstruction_payload,
)
from vibecad.visual.provider import (
    VISUAL_PROVIDER_IDENTITY,
    VISUAL_PROVIDER_MODEL,
    VISUAL_PROVIDER_MODEL_VERSION,
    build_visual_provider_invocation,
    visual_provider_input_digest,
)
from vibecad.visual.reconstruction import (
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


def _layout(tmp_path: Path) -> ApplicationDataLayout:
    return ApplicationDataLayout.open(tmp_path.resolve() / "data")


def _store(layout: ApplicationDataLayout) -> ReconstructionDraftStore:
    manager = ResourceLeaseManager(
        layout.locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    return ReconstructionDraftStore(
        root=layout.reconstruction_drafts,
        expected_root_identity=layout.identity_for(layout.reconstruction_drafts),
        lease_manager=manager,
    )


def _draft(*, create_seed: int = 1, image_seed: int = 2) -> ReconstructionDraft:
    create_key = f"reconstruction_create_{create_seed:032x}"
    reconstruction_id, create_digest = reconstruction_identity(create_key)
    return ReconstructionDraft(
        reconstruction_id=reconstruction_id,
        create_key_sha256=create_digest,
        generation=0,
        status=ReconstructionStatus.READY,
        base_head=BaseHeadBinding(
            project_id="project_" + "3" * 32,
            generation=4,
            revision_id="revision_" + "5" * 32,
            manifest_sha256="6" * 64,
        ),
        image_set_id=f"image_set_{image_seed:032x}",
        image_set_manifest_sha256="7" * 64,
    )


def _invocation_record(draft: ReconstructionDraft) -> ProviderInvocationRecord:
    budget = RuntimeBudget(
        max_elapsed_ms=1000,
        max_memory_bytes=64 * 1024 * 1024,
        max_output_bytes=1024 * 1024,
    )
    invocation = build_visual_provider_invocation(
        reconstruction_id=draft.reconstruction_id,
        generation=1,
        image_set_id=draft.image_set_id,
        image_set_manifest_sha256=draft.image_set_manifest_sha256,
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


def _observation(draft: ReconstructionDraft, invocation_id: str) -> VisualObservation:
    return VisualObservation(
        reconstruction_id=draft.reconstruction_id,
        generation=1,
        image_set_id=draft.image_set_id,
        image_set_manifest_sha256=draft.image_set_manifest_sha256,
        invocation_id=invocation_id,
        claims=(
            VisualClaim(
                name="overall.depth",
                status=VisualClaimStatus.CONFIRMED,
                source_indices=(0,),
                value=8,
                unit=VisualClaimUnit.MM,
                description="Observed depth",
            ),
        ),
    )


def _persist_observation_payload(
    store: ReconstructionDraftStore,
    ready: ReconstructionDraft,
) -> ReconstructionPayload:
    store.create(ready)
    intent = _invocation_record(ready)
    observing = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(intent,),
    )
    store.compare_and_set(ready.reconstruction_id, 0, observing)
    payload = reconstruction_payload(_observation(ready, intent.invocation_id))
    receipt = dataclasses.replace(
        intent,
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        start_receipt_sha256="8" * 64,
        result_sha256="9" * 64,
        output_sha256="a" * 64,
    )
    completed = dataclasses.replace(
        observing,
        generation=2,
        status=ReconstructionStatus.READY,
        observation_ref=payload.ref,
        provider_invocations=(receipt,),
    )
    store.compare_and_set(ready.reconstruction_id, 1, completed, (payload,))
    return payload


def test_create_load_replay_restart_and_private_permissions(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    draft = _draft()
    store = _store(layout)

    assert store.create(draft) == draft
    assert store.create(draft) == draft
    assert store.load(draft.reconstruction_id) == draft
    assert _store(layout).load(draft.reconstruction_id) == draft

    directory = layout.reconstruction_drafts / draft.reconstruction_id
    if os.name == "nt":
        capture_windows_path(directory, directory=True)
        capture_windows_path(directory / "record.json", directory=False)
    else:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / "record.json").stat().st_mode) == 0o600


def test_load_payload_succeeds_after_restart_and_rejects_nonmembers(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    ready = _draft()
    payload = _persist_observation_payload(_store(layout), ready)

    restarted = _store(layout)
    assert restarted.load_payload(ready.reconstruction_id, payload.ref) == payload

    stale_ref = dataclasses.replace(payload.ref, sha256="b" * 64)
    with pytest.raises(ReconstructionDraftStoreError) as error:
        restarted.load_payload(ready.reconstruction_id, stale_ref)
    assert error.value.code is ReconstructionDraftStoreErrorCode.CONFLICT

    other_ready = _draft(create_seed=8, image_seed=9)
    other_intent = _invocation_record(other_ready)
    other_ref = reconstruction_payload(_observation(other_ready, other_intent.invocation_id)).ref
    with pytest.raises(ReconstructionDraftStoreError) as error:
        restarted.load_payload(ready.reconstruction_id, other_ref)
    assert error.value.code is ReconstructionDraftStoreErrorCode.NOT_FOUND


def test_load_payload_tamper_fails_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    ready = _draft()
    payload = _persist_observation_payload(_store(layout), ready)
    payload_path = layout.reconstruction_drafts / ready.reconstruction_id / payload.ref.filename
    tampered = bytearray(payload_path.read_bytes())
    tampered[-2] ^= 1
    payload_path.write_bytes(tampered)

    with pytest.raises(ReconstructionDraftStoreError) as error:
        _store(layout).load_payload(ready.reconstruction_id, payload.ref)
    assert error.value.code is ReconstructionDraftStoreErrorCode.CORRUPT_RECORD


def test_creation_gate_conflict_and_one_to_one_image_ownership(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    first = _draft()
    store.create(first)

    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.create(dataclasses.replace(first, generation=1))
    assert error.value.code is ReconstructionDraftStoreErrorCode.CONFLICT

    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.create(_draft(create_seed=8, image_seed=2))
    assert error.value.code is ReconstructionDraftStoreErrorCode.CONFLICT


def test_create_replay_detects_preexisting_duplicate_image_owners(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    first = _draft()
    second = _draft(create_seed=8, image_seed=9)
    store.create(first)
    store.create(second)

    duplicate = dataclasses.replace(
        second,
        image_set_id=first.image_set_id,
        image_set_manifest_sha256=first.image_set_manifest_sha256,
    )
    second_record = layout.reconstruction_drafts / second.reconstruction_id / "record.json"
    second_record.write_bytes(encode_reconstruction_draft(duplicate))
    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.create(first)
    assert error.value.code is ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED


def test_finalized_deleted_tombstones_do_not_claim_a_null_image_owner(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    for create_seed, image_seed in ((1, 2), (8, 9)):
        ready = _draft(create_seed=create_seed, image_seed=image_seed)
        store.create(ready)
        deleted = ReconstructionDraft(
            reconstruction_id=ready.reconstruction_id,
            create_key_sha256=ready.create_key_sha256,
            generation=1,
            status=ReconstructionStatus.DELETED,
            base_head=None,
            image_set_id=None,
            image_set_manifest_sha256=None,
            delete_cleanup=DeleteCleanup(
                image_set_id=ready.image_set_id,
                image_set_manifest_sha256=ready.image_set_manifest_sha256,
                payload_refs=(),
            ),
        )
        store.compare_and_set(ready.reconstruction_id, 0, deleted)
        source_deleted = dataclasses.replace(
            deleted,
            generation=2,
            delete_cleanup=dataclasses.replace(deleted.delete_cleanup, source_deleted=True),
        )
        store.compare_and_set(ready.reconstruction_id, 1, source_deleted)
        store.compare_and_set(
            ready.reconstruction_id,
            2,
            dataclasses.replace(source_deleted, generation=3, delete_cleanup=None),
        )

    third = _draft(create_seed=10, image_seed=10)
    assert store.create(third) == third


def test_generation_cas_publishes_and_cleans_immutable_payloads(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    ready = _draft()
    store.create(ready)

    intent = _invocation_record(ready)
    observing = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(intent,),
    )
    assert store.compare_and_set(ready.reconstruction_id, 0, observing) == observing

    payload = reconstruction_payload(_observation(ready, intent.invocation_id))
    receipt = dataclasses.replace(
        intent,
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        start_receipt_sha256="8" * 64,
        result_sha256="9" * 64,
        output_sha256="a" * 64,
    )
    completed = dataclasses.replace(
        observing,
        generation=2,
        status=ReconstructionStatus.READY,
        observation_ref=payload.ref,
        provider_invocations=(receipt,),
    )
    assert (
        store.compare_and_set(
            ready.reconstruction_id,
            1,
            completed,
            (payload,),
        )
        == completed
    )
    payload_path = layout.reconstruction_drafts / ready.reconstruction_id / payload.ref.filename
    assert payload_path.read_bytes() == payload.raw
    if os.name == "nt":
        capture_windows_path(payload_path, directory=False)
    else:
        assert stat.S_IMODE(payload_path.stat().st_mode) == 0o600

    rejected = dataclasses.replace(
        completed,
        generation=3,
        status=ReconstructionStatus.REJECTED,
    )
    store.compare_and_set(ready.reconstruction_id, 2, rejected)
    deleted = ReconstructionDraft(
        reconstruction_id=ready.reconstruction_id,
        create_key_sha256=ready.create_key_sha256,
        generation=4,
        status=ReconstructionStatus.DELETED,
        base_head=None,
        image_set_id=None,
        image_set_manifest_sha256=None,
        delete_cleanup=DeleteCleanup(
            image_set_id=ready.image_set_id,
            image_set_manifest_sha256=ready.image_set_manifest_sha256,
            payload_refs=(payload.ref,),
        ),
    )
    store.compare_and_set(ready.reconstruction_id, 3, deleted)
    assert not payload_path.exists()
    assert store.load(ready.reconstruction_id) == deleted

    source_deleted = dataclasses.replace(
        deleted,
        generation=5,
        delete_cleanup=dataclasses.replace(deleted.delete_cleanup, source_deleted=True),
    )
    store.compare_and_set(ready.reconstruction_id, 4, source_deleted)
    final_tombstone = dataclasses.replace(
        source_deleted,
        generation=6,
        delete_cleanup=None,
    )
    store.compare_and_set(ready.reconstruction_id, 5, final_tombstone)
    assert store.load(ready.reconstruction_id) == final_tombstone


def test_stale_generation_and_missing_payload_fail_without_mutation(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    ready = _draft()
    store.create(ready)
    intent = _invocation_record(ready)
    observing = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(intent,),
    )

    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.compare_and_set(ready.reconstruction_id, 1, observing)
    assert error.value.code is ReconstructionDraftStoreErrorCode.CONFLICT
    assert store.load(ready.reconstruction_id) == ready

    payload = reconstruction_payload(_observation(ready, intent.invocation_id))
    receipt = dataclasses.replace(
        intent,
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        start_receipt_sha256="8" * 64,
        result_sha256="9" * 64,
        output_sha256="a" * 64,
    )
    completed = dataclasses.replace(
        observing,
        generation=2,
        status=ReconstructionStatus.READY,
        observation_ref=payload.ref,
        provider_invocations=(receipt,),
    )
    store.compare_and_set(ready.reconstruction_id, 0, observing)
    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.compare_and_set(ready.reconstruction_id, 1, completed)
    assert error.value.code is ReconstructionDraftStoreErrorCode.CONFLICT
    assert store.load(ready.reconstruction_id) == observing


def test_post_replace_failure_is_recovered_without_second_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.store as store_module

    layout = _layout(tmp_path)
    store = _store(layout)
    ready = _draft()
    store.create(ready)
    rejected = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.REJECTED,
    )

    original_cleanup = ReconstructionDraftStore._finish_committed_cleanup

    def fail_cleanup(self, draft_fd, journal, successor):
        raise ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)

    monkeypatch.setattr(ReconstructionDraftStore, "_finish_committed_cleanup", fail_cleanup)
    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.compare_and_set(ready.reconstruction_id, 0, rejected)
    assert error.value.code is ReconstructionDraftStoreErrorCode.DURABILITY_UNCERTAIN
    assert error.value.committed_generation == 1

    monkeypatch.setattr(
        ReconstructionDraftStore,
        "_finish_committed_cleanup",
        original_cleanup,
    )
    assert store.load(ready.reconstruction_id) == rejected
    assert not any(
        path.name.startswith(".record") or path.name == store_module._JOURNAL_NAME
        for path in (layout.reconstruction_drafts / ready.reconstruction_id).iterdir()
    )


def test_pre_replace_failure_rolls_back_owned_remnants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.store as store_module

    layout = _layout(tmp_path)
    store = _store(layout)
    ready = _draft()
    store.create(ready)
    rejected = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.REJECTED,
    )

    def fail_append(*args, **kwargs):
        raise ReconstructionDraftStoreError(ReconstructionDraftStoreErrorCode.IO_ERROR)

    monkeypatch.setattr(store_module, "_append_staged_journal", fail_append)
    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.compare_and_set(ready.reconstruction_id, 0, rejected)
    assert error.value.code is ReconstructionDraftStoreErrorCode.IO_ERROR

    directory = layout.reconstruction_drafts / ready.reconstruction_id
    assert {path.name for path in directory.iterdir()} == {"record.json"}
    assert store.load(ready.reconstruction_id) == ready


def test_reserved_old_recovery_and_payload_tamper_fail_closed(tmp_path: Path) -> None:
    import vibecad.visual.store as store_module

    layout = _layout(tmp_path)
    store = _store(layout)
    ready = _draft()
    store.create(ready)
    directory = layout.reconstruction_drafts / ready.reconstruction_id
    old_raw = (directory / "record.json").read_bytes()
    journal = store_module._MutationJournal(
        state="RESERVED",
        reconstruction_id=ready.reconstruction_id,
        old_sha256=hashlib.sha256(old_raw).hexdigest(),
        new_sha256="f" * 64,
        new_size=1,
        record_temp_name=".record." + "e" * 32 + ".tmp",
        add_payloads=(),
        remove_payloads=(),
    )
    journal_path = directory / store_module._JOURNAL_NAME
    journal_path.write_bytes(journal.to_line())
    journal_path.chmod(0o600)
    if os.name == "nt":
        set_private_dacl(journal_path)
    assert store.load(ready.reconstruction_id) == ready
    assert not journal_path.exists()

    intent = _invocation_record(ready)
    observing = dataclasses.replace(
        ready,
        generation=1,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(intent,),
    )
    store.compare_and_set(ready.reconstruction_id, 0, observing)
    payload = reconstruction_payload(_observation(ready, intent.invocation_id))
    receipt = dataclasses.replace(
        intent,
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        start_receipt_sha256="8" * 64,
        result_sha256="9" * 64,
        output_sha256="a" * 64,
    )
    completed = dataclasses.replace(
        observing,
        generation=2,
        status=ReconstructionStatus.READY,
        observation_ref=payload.ref,
        provider_invocations=(receipt,),
    )
    store.compare_and_set(ready.reconstruction_id, 1, completed, (payload,))
    payload_path = directory / payload.ref.filename
    tampered = bytearray(payload_path.read_bytes())
    tampered[-2] ^= 1
    payload_path.write_bytes(tampered)
    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.load(ready.reconstruction_id)
    assert error.value.code is ReconstructionDraftStoreErrorCode.CORRUPT_RECORD


def test_store_byte_quota_accepts_exact_boundary_and_rejects_n_plus_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.store as store_module

    draft = _draft()
    record_size = len(encode_reconstruction_draft(draft))
    monkeypatch.setattr(store_module, "MAX_RECONSTRUCTION_DRAFT_STORE_BYTES", record_size)
    exact_layout = _layout(tmp_path / "exact")
    assert _store(exact_layout).create(draft) == draft

    monkeypatch.setattr(
        store_module,
        "MAX_RECONSTRUCTION_DRAFT_STORE_BYTES",
        record_size - 1,
    )
    overflow_layout = _layout(tmp_path / "overflow")
    with pytest.raises(ReconstructionDraftStoreError) as error:
        _store(overflow_layout).create(_draft(create_seed=9, image_seed=9))
    assert error.value.code is ReconstructionDraftStoreErrorCode.RESOURCE_EXHAUSTED


def test_symlink_record_and_captured_root_swap_fail_closed(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    store = _store(layout)
    draft = _draft()
    store.create(draft)
    directory = layout.reconstruction_drafts / draft.reconstruction_id
    record = directory / "record.json"
    moved = tmp_path / "moved.json"
    record.rename(moved)
    record.symlink_to(moved)
    with pytest.raises(ReconstructionDraftStoreError) as error:
        store.load(draft.reconstruction_id)
    assert error.value.code is ReconstructionDraftStoreErrorCode.UNSAFE_STORE

    root = layout.reconstruction_drafts
    swapped = root.with_name("reconstruction_drafts_old")
    root.rename(swapped)
    root.mkdir(mode=0o700)
    with pytest.raises(ReconstructionDraftStoreError):
        store.load(draft.reconstruction_id)
