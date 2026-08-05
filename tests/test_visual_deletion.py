from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.test_visual_adoption import _AdoptionProbe, _proposed, _service
from tests.test_visual_service import (
    _budget,
    _create,
    _head,
    _invocation,
    _PendingProbeProvider,
    _sealed_image_set,
    _stores,
)
from vibecad.visual.drafts import derive_adoption_identity
from vibecad.visual.fake_provider import (
    DeterministicFakeVisualProvider,
    FakeVisualFixture,
    FakeVisualOutcomeKind,
)
from vibecad.visual.inputs import (
    VisualInputStore,
    VisualInputStoreError,
    VisualInputStoreErrorCode,
)
from vibecad.visual.provider import visual_provider_input_digest
from vibecad.visual.reconstruction import ReconstructionStatus, reconstruction_identity
from vibecad.visual.service import VisualServiceError, VisualServiceErrorCode


def _empty_provider() -> DeterministicFakeVisualProvider:
    return DeterministicFakeVisualProvider({})


def test_delete_publishes_tombstone_before_source_cleanup_then_is_finally_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    service = _service(inputs, drafts, _empty_provider())
    ready = _create(service, image_set)
    original_delete = VisualInputStore.delete_exact
    cleanup_calls = 0

    def observe_tombstone(store, image_set_id, manifest_sha256):
        nonlocal cleanup_calls
        cleanup_calls += 1
        durable = drafts.load(ready.reconstruction_id)
        assert durable.status is ReconstructionStatus.DELETED
        assert durable.delete_cleanup is not None
        assert durable.delete_cleanup.image_set_id == image_set_id
        assert durable.delete_cleanup.image_set_manifest_sha256 == manifest_sha256
        return original_delete(store, image_set_id, manifest_sha256)

    monkeypatch.setattr(VisualInputStore, "delete_exact", observe_tombstone)

    deleted = service.delete(
        ready.reconstruction_id,
        expected_generation=ready.generation,
    )
    replay = service.delete(
        deleted.reconstruction_id,
        expected_generation=deleted.generation,
    )

    assert deleted.status is ReconstructionStatus.DELETED
    assert deleted.delete_cleanup is None
    assert deleted.generation == ready.generation + 3
    assert replay == deleted
    assert cleanup_calls == 1
    with pytest.raises(VisualInputStoreError) as missing:
        inputs.get(image_set.id)
    assert missing.value.code is VisualInputStoreErrorCode.NOT_FOUND


def test_delete_cleanup_failure_is_durable_and_new_service_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    provider = _empty_provider()
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)
    original_delete = VisualInputStore.delete_exact
    cleanup_calls = 0

    def fail_delete(store, image_set_id, manifest_sha256):
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise OSError("injected image cleanup failure")

    monkeypatch.setattr(VisualInputStore, "delete_exact", fail_delete)

    with pytest.raises(OSError, match="injected image cleanup failure"):
        service.delete(
            ready.reconstruction_id,
            expected_generation=ready.generation,
        )

    tombstone = drafts.load(ready.reconstruction_id)
    assert tombstone.status is ReconstructionStatus.DELETED
    assert tombstone.delete_cleanup is not None
    assert tombstone.delete_cleanup.image_set_id == image_set.id
    assert cleanup_calls == 1

    monkeypatch.setattr(VisualInputStore, "delete_exact", original_delete)
    recovered = _service(inputs, drafts, provider).delete(
        tombstone.reconstruction_id,
        expected_generation=tombstone.generation,
    )

    assert recovered.status is ReconstructionStatus.DELETED
    assert recovered.delete_cleanup is None
    assert recovered.generation == tombstone.generation + 2
    with pytest.raises(VisualInputStoreError) as missing:
        inputs.get(image_set.id)
    assert missing.value.code is VisualInputStoreErrorCode.NOT_FOUND


def test_delete_marker_finalize_failure_resumes_without_redeleting_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    provider = _empty_provider()
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)
    original_finalize = VisualInputStore.finalize_delete_exact
    delete_calls = 0
    original_delete = VisualInputStore.delete_exact

    def count_delete(store, image_set_id, manifest_sha256):
        nonlocal delete_calls
        delete_calls += 1
        return original_delete(store, image_set_id, manifest_sha256)

    def fail_finalize(store, image_set_id, manifest_sha256):
        raise OSError("injected marker finalize failure")

    monkeypatch.setattr(VisualInputStore, "delete_exact", count_delete)
    monkeypatch.setattr(VisualInputStore, "finalize_delete_exact", fail_finalize)

    with pytest.raises(OSError, match="injected marker finalize failure"):
        service.delete(
            ready.reconstruction_id,
            expected_generation=ready.generation,
        )

    source_deleted = drafts.load(ready.reconstruction_id)
    assert source_deleted.status is ReconstructionStatus.DELETED
    assert source_deleted.delete_cleanup is not None
    assert source_deleted.delete_cleanup.source_deleted is True
    assert delete_calls == 1

    monkeypatch.setattr(VisualInputStore, "finalize_delete_exact", original_finalize)
    recovered = _service(inputs, drafts, provider).delete(
        source_deleted.reconstruction_id,
        expected_generation=source_deleted.generation,
    )

    assert recovered.delete_cleanup is None
    assert recovered.generation == source_deleted.generation + 1
    assert delete_calls == 1


def test_delete_adopted_draft_retains_task_and_source_provenance(tmp_path: Path) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(drafts=drafts)
    service = _service(inputs, drafts, provider, adoption)
    adopted = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )

    deleted = service.delete(
        adopted.reconstruction_id,
        expected_generation=adopted.generation,
    )

    assert deleted.status is ReconstructionStatus.DELETED
    assert deleted.delete_cleanup is None
    assert deleted.adoption_key_sha256 == adopted.adoption_key_sha256
    assert deleted.adoption_intent_sha256 == adopted.adoption_intent_sha256
    assert deleted.adopted_task_id == adopted.adopted_task_id
    assert deleted.adopted_source_provenance == adopted.adopted_source_provenance


@pytest.mark.parametrize("blocked_state", ["observing", "recovery", "adopting"])
def test_delete_rejects_in_flight_states_without_cleanup_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_state: str,
) -> None:
    cleanup_calls = 0

    def unexpected_cleanup(store, image_set_id, manifest_sha256):
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise AssertionError("cleanup must not run for an in-flight draft")

    if blocked_state == "adopting":
        inputs, drafts, image_set, provider, _, proposed, proposal = _proposed(tmp_path)
        adoption_key, adoption_intent = derive_adoption_identity(
            proposed.reconstruction_id,
            proposal.digest,
            proposed.base_head.sha256,
        )
        blocked = drafts.compare_and_set(
            proposed.reconstruction_id,
            proposed.generation,
            dataclasses.replace(
                proposed,
                generation=proposed.generation + 1,
                status=ReconstructionStatus.ADOPTING,
                adoption_key_sha256=adoption_key,
                adoption_intent_sha256=adoption_intent,
            ),
        )
        service = _service(inputs, drafts, provider)
    else:
        inputs, drafts = _stores(tmp_path)
        image_set = _sealed_image_set(tmp_path, inputs)
        invocation = _invocation(image_set)
        if blocked_state == "observing":
            provider = _PendingProbeProvider(lambda _invocation: None)
        else:
            provider = DeterministicFakeVisualProvider(
                {
                    visual_provider_input_digest(invocation): FakeVisualFixture(
                        kind=FakeVisualOutcomeKind.UNKNOWN
                    )
                }
            )
        service = _service(inputs, drafts, provider)
        ready = _create(service, image_set)
        blocked = service.run(
            ready.reconstruction_id,
            expected_generation=ready.generation,
            budget=_budget(),
            deadline_ms=2_000_000_000_000,
        )

    expected_status = {
        "observing": ReconstructionStatus.OBSERVING,
        "recovery": ReconstructionStatus.RECOVERY_REQUIRED,
        "adopting": ReconstructionStatus.ADOPTING,
    }[blocked_state]
    assert blocked.status is expected_status
    monkeypatch.setattr(VisualInputStore, "delete_exact", unexpected_cleanup)

    with pytest.raises(VisualServiceError) as caught:
        service.delete(
            blocked.reconstruction_id,
            expected_generation=blocked.generation,
        )

    assert caught.value.code is VisualServiceErrorCode.INVALID_STATE
    assert cleanup_calls == 0
    assert drafts.load(blocked.reconstruction_id) == blocked
    assert inputs.get(image_set.id) == image_set


def test_delete_stale_generation_conflicts_before_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    service = _service(inputs, drafts, _empty_provider())
    ready = _create(service, image_set)
    cleanup_calls = 0

    def unexpected_cleanup(store, image_set_id, manifest_sha256):
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise AssertionError("cleanup must not run after a stale-generation conflict")

    monkeypatch.setattr(VisualInputStore, "delete_exact", unexpected_cleanup)

    with pytest.raises(VisualServiceError) as caught:
        service.delete(
            ready.reconstruction_id,
            expected_generation=ready.generation + 1,
        )

    assert caught.value.code is VisualServiceErrorCode.CONFLICT
    assert cleanup_calls == 0
    assert drafts.load(ready.reconstruction_id) == ready
    assert inputs.get(image_set.id) == image_set


def test_create_compensates_if_image_set_is_deleted_between_validation_and_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    service = _service(inputs, drafts, _empty_provider())
    original_get = VisualInputStore.get
    get_calls = 0

    def delete_after_first_read(store, image_set_id):
        nonlocal get_calls
        get_calls += 1
        value = original_get(store, image_set_id)
        if get_calls == 1:
            store.delete_exact(image_set.id, image_set.manifest_sha256)
            store.finalize_delete_exact(image_set.id, image_set.manifest_sha256)
        return value

    monkeypatch.setattr(VisualInputStore, "get", delete_after_first_read)

    with pytest.raises(VisualServiceError) as caught:
        _create(service, image_set)

    assert caught.value.code is VisualServiceErrorCode.CONFLICT
    reconstruction_id, _ = reconstruction_identity("reconstruction_create_" + "1" * 32)
    compensated = drafts.load(reconstruction_id)
    assert compensated.status is ReconstructionStatus.DELETED
    assert compensated.delete_cleanup is None

    recreated = _sealed_image_set(
        tmp_path,
        inputs,
        create_key="image_set_create_" + "8" * 32,
    )
    successor = service.create(
        create_key="reconstruction_create_" + "9" * 32,
        image_set_id=recreated.id,
        image_set_manifest_sha256=recreated.manifest_sha256,
        base_head=_head(),
    )
    assert successor.status is ReconstructionStatus.READY
    assert successor.base_head == _head()
