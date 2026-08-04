from __future__ import annotations

import dataclasses
import json

import pytest

from vibecad.runtime.contracts import (
    RuntimeBudget,
    RuntimeIdentity,
    RuntimeLifecycleState,
)
from vibecad.visual.drafts import (
    MAX_RECONSTRUCTION_CLARIFICATIONS,
    MAX_RECONSTRUCTION_DRAFT_MUTATIONS,
    MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES,
    MAX_RECONSTRUCTION_DRAFT_STORE_BYTES,
    MAX_RECONSTRUCTION_DRAFTS,
    MAX_RECONSTRUCTION_PROVIDER_INVOCATIONS,
    AdoptedSourceProvenance,
    BaseHeadBinding,
    DeleteCleanup,
    ProviderInvocationRecord,
    ReconstructionDraft,
    ReconstructionDraftError,
    ReconstructionDraftErrorCode,
    ReconstructionLastError,
    ReconstructionPayloadKind,
    ReconstructionPayloadRef,
    decode_reconstruction_draft,
    derive_adoption_identity,
    derive_adoption_task_identity,
    encode_reconstruction_draft,
    reconstruction_payload,
    validate_reconstruction_creation,
    validate_reconstruction_successor,
)
from vibecad.visual.reconstruction import (
    ClarificationAnswer,
    ReconstructionStatus,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    reconstruction_identity,
    visual_invocation_identity,
)

_CREATE_KEY = "reconstruction_create_" + "1" * 32
_IMAGE_SET_ID = "image_set_" + "2" * 32
_MANIFEST_DIGEST = "3" * 64


def _head() -> BaseHeadBinding:
    return BaseHeadBinding(
        project_id="project_" + "4" * 32,
        generation=7,
        revision_id="revision_" + "5" * 32,
        manifest_sha256="6" * 64,
    )


def _draft(
    *,
    generation: int = 0,
    status: ReconstructionStatus = ReconstructionStatus.READY,
    **changes,
) -> ReconstructionDraft:
    reconstruction_id, create_digest = reconstruction_identity(_CREATE_KEY)
    values = {
        "reconstruction_id": reconstruction_id,
        "create_key_sha256": create_digest,
        "generation": generation,
        "status": status,
        "base_head": _head(),
        "image_set_id": _IMAGE_SET_ID,
        "image_set_manifest_sha256": _MANIFEST_DIGEST,
    }
    values.update(changes)
    return ReconstructionDraft(**values)


def _observation(generation: int = 1) -> VisualObservation:
    reconstruction_id, _ = reconstruction_identity(_CREATE_KEY)
    claim = VisualClaim(
        name="overall.depth",
        status=VisualClaimStatus.CONFIRMED,
        source_indices=(0,),
        value=8,
        unit=VisualClaimUnit.MM,
        description="Observed depth",
    )
    return VisualObservation(
        reconstruction_id=reconstruction_id,
        generation=generation,
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
        invocation_id=visual_invocation_identity(
            reconstruction_id,
            generation,
            _IMAGE_SET_ID,
            _MANIFEST_DIGEST,
        ),
        claims=(claim,),
    )


def _intent(*, generation: int = 1) -> ProviderInvocationRecord:
    observation = _observation(generation)
    return ProviderInvocationRecord(
        invocation_id=observation.invocation_id,
        attempt_generation=generation,
        runtime=RuntimeIdentity(
            family="visual",
            provider="deterministic_fake",
            version="1.0",
        ),
        model="deterministic_visual_fixture",
        model_version="1",
        budget=RuntimeBudget(
            max_elapsed_ms=1000,
            max_memory_bytes=64 * 1024 * 1024,
            max_output_bytes=1024 * 1024,
        ),
        deadline_ms=2_000_000_000_000,
        input_sha256="7" * 64,
    )


def test_draft_record_is_canonical_checksummed_and_round_trips() -> None:
    draft = _draft()
    raw = encode_reconstruction_draft(draft)

    assert decode_reconstruction_draft(raw) == draft
    assert (
        json.dumps(
            json.loads(raw),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        == raw
    )

    tampered = json.loads(raw)
    tampered["generation"] = 1
    tampered_raw = json.dumps(
        tampered,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    with pytest.raises(ReconstructionDraftError) as error:
        decode_reconstruction_draft(tampered_raw)
    assert error.value.code is ReconstructionDraftErrorCode.INTEGRITY_FAILURE


def test_base_head_and_reconstruction_identity_are_content_bound() -> None:
    head = _head()
    with pytest.raises(ReconstructionDraftError) as error:
        dataclasses.replace(head, generation=head.generation + 1)
    assert error.value.code is ReconstructionDraftErrorCode.INTEGRITY_FAILURE

    with pytest.raises(ReconstructionDraftError) as error:
        dataclasses.replace(_draft(), reconstruction_id="reconstruction_" + "f" * 32)
    assert error.value.code is ReconstructionDraftErrorCode.INTEGRITY_FAILURE


def test_observation_and_float_clarification_payloads_are_immutable_and_bound() -> None:
    observation_payload = reconstruction_payload(_observation())
    answer_payload = reconstruction_payload(
        ClarificationAnswer(
            question_id="clarification_question_" + "8" * 32,
            claim_id="visual_claim_" + "9" * 32,
            response=12.5,
        )
    )

    assert observation_payload.ref.id == _observation().id
    assert observation_payload.ref.size_bytes == len(observation_payload.raw)
    assert answer_payload.ref.id.startswith("clarification_answer_")

    with pytest.raises(ReconstructionDraftError) as error:
        dataclasses.replace(
            observation_payload,
            raw=observation_payload.raw[:-1] + b" ",
        )
    assert error.value.code is ReconstructionDraftErrorCode.INTEGRITY_FAILURE


def test_provider_intent_receipt_and_generation_successors_are_monotonic() -> None:
    ready = _draft()
    intent = _intent()
    observing = _draft(
        generation=1,
        status=ReconstructionStatus.OBSERVING,
        provider_invocations=(intent,),
    )
    validate_reconstruction_successor(ready, observing)

    pending_record = dataclasses.replace(
        intent,
        lifecycle=RuntimeLifecycleState.PENDING,
        start_receipt_sha256="a" * 64,
    )
    pending = dataclasses.replace(
        observing,
        generation=2,
        provider_invocations=(pending_record,),
    )
    validate_reconstruction_successor(observing, pending)

    observation_payload = reconstruction_payload(_observation())
    terminal_record = dataclasses.replace(
        pending_record,
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        result_sha256="b" * 64,
        output_sha256="c" * 64,
    )
    completed = _draft(
        generation=3,
        status=ReconstructionStatus.READY,
        observation_ref=observation_payload.ref,
        provider_invocations=(terminal_record,),
    )
    validate_reconstruction_successor(pending, completed)

    with pytest.raises(ReconstructionDraftError) as error:
        validate_reconstruction_successor(ready, completed)
    assert error.value.code is ReconstructionDraftErrorCode.INVALID_TRANSITION


def test_unknown_outcome_requires_recovery_state_and_failed_requires_receipt() -> None:
    unknown = dataclasses.replace(
        _intent(),
        lifecycle=RuntimeLifecycleState.UNKNOWN,
        start_receipt_sha256="a" * 64,
    )
    recovery = _draft(
        generation=1,
        status=ReconstructionStatus.RECOVERY_REQUIRED,
        provider_invocations=(unknown,),
        last_error=ReconstructionLastError(
            code="provider.unknown",
            phase="reconcile",
            retryable=False,
            diagnostic_digest="e" * 64,
        ),
    )
    assert recovery.next_action.value == "run"

    with pytest.raises(ReconstructionDraftError):
        _draft(
            generation=1,
            status=ReconstructionStatus.OBSERVING,
            provider_invocations=(unknown,),
        )

    with pytest.raises(ReconstructionDraftError):
        _draft(
            generation=1,
            status=ReconstructionStatus.FAILED,
            provider_invocations=(_intent(),),
            last_error=ReconstructionLastError(
                code="provider.failed",
                phase="observe",
                retryable=True,
                diagnostic_digest="d" * 64,
            ),
        )


def test_deleted_tombstone_drops_active_sources_and_retains_adopted_provenance() -> None:
    deleted = ReconstructionDraft(
        reconstruction_id=_draft().reconstruction_id,
        create_key_sha256=_draft().create_key_sha256,
        generation=1,
        status=ReconstructionStatus.DELETED,
        base_head=None,
        image_set_id=None,
        image_set_manifest_sha256=None,
        delete_cleanup=DeleteCleanup(
            image_set_id=_IMAGE_SET_ID,
            image_set_manifest_sha256=_MANIFEST_DIGEST,
            payload_refs=(),
        ),
    )
    validate_reconstruction_successor(_draft(), deleted)
    assert deleted.image_set_id is None

    source_deleted = dataclasses.replace(
        deleted,
        generation=2,
        delete_cleanup=dataclasses.replace(deleted.delete_cleanup, source_deleted=True),
    )
    validate_reconstruction_successor(deleted, source_deleted)
    finalized = dataclasses.replace(source_deleted, generation=3, delete_cleanup=None)
    validate_reconstruction_successor(source_deleted, finalized)
    with pytest.raises(ReconstructionDraftError):
        validate_reconstruction_successor(
            deleted,
            dataclasses.replace(deleted, generation=2, delete_cleanup=None),
        )

    adoption_key = "a" * 64
    _task_create_key, adopted_task_id = derive_adoption_task_identity(adoption_key)
    adopted = dataclasses.replace(
        deleted,
        delete_cleanup=None,
        adoption_key_sha256=adoption_key,
        adoption_intent_sha256="b" * 64,
        adopted_task_id=adopted_task_id,
        adopted_source_provenance=AdoptedSourceProvenance(
            source_sha256=("d" * 64,),
            proposal_digest="e" * 64,
        ),
    )
    assert decode_reconstruction_draft(encode_reconstruction_draft(adopted)) == adopted

    with pytest.raises(ReconstructionDraftError):
        dataclasses.replace(adopted, adopted_task_id=None)


def test_record_decoder_rejects_extra_and_duplicate_fields() -> None:
    raw = encode_reconstruction_draft(_draft())
    value = json.loads(raw)
    value["extra"] = True
    extra = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(ReconstructionDraftError):
        decode_reconstruction_draft(extra)

    duplicate = raw[:-1] + b',"generation":0}'
    with pytest.raises(ReconstructionDraftError):
        decode_reconstruction_draft(duplicate)


def test_creation_gate_and_frozen_capacity_constants_are_explicit() -> None:
    validate_reconstruction_creation(_draft())
    with pytest.raises(ReconstructionDraftError) as error:
        validate_reconstruction_creation(_draft(generation=1))
    assert error.value.code is ReconstructionDraftErrorCode.INVALID_TRANSITION

    assert MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES == 256 * 1024
    assert MAX_RECONSTRUCTION_DRAFTS == 1024
    assert MAX_RECONSTRUCTION_DRAFT_STORE_BYTES == 2 * 1024 * 1024 * 1024
    assert MAX_RECONSTRUCTION_PROVIDER_INVOCATIONS == 16
    assert MAX_RECONSTRUCTION_CLARIFICATIONS == 128
    assert MAX_RECONSTRUCTION_DRAFT_MUTATIONS == 1


def test_invocation_and_clarification_n_plus_one_are_rejected() -> None:
    invocations = tuple(
        dataclasses.replace(
            _intent(generation=index),
            lifecycle=RuntimeLifecycleState.SUCCEEDED,
            start_receipt_sha256=f"{index:064x}",
            result_sha256=f"{index + 32:064x}",
            output_sha256=f"{index + 64:064x}",
        )
        for index in range(1, MAX_RECONSTRUCTION_PROVIDER_INVOCATIONS + 2)
    )
    with pytest.raises(ReconstructionDraftError) as error:
        _draft(generation=len(invocations), provider_invocations=invocations)
    assert error.value.code is ReconstructionDraftErrorCode.INVALID_INPUT

    clarifications = tuple(
        ReconstructionPayloadRef(
            kind=ReconstructionPayloadKind.CLARIFICATION,
            id=f"clarification_answer_{index:032x}",
            contract_digest=f"{index + 1:064x}",
            sha256=f"{index + 2:064x}",
            size_bytes=1,
        )
        for index in range(MAX_RECONSTRUCTION_CLARIFICATIONS + 1)
    )
    with pytest.raises(ReconstructionDraftError) as error:
        _draft(clarification_refs=clarifications)
    assert error.value.code is ReconstructionDraftErrorCode.INVALID_INPUT


def test_oversize_and_surrogate_json_fail_with_bounded_contract_errors() -> None:
    with pytest.raises(ReconstructionDraftError) as error:
        decode_reconstruction_draft(b"x" * (MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES + 1))
    assert error.value.code is ReconstructionDraftErrorCode.BUDGET_EXCEEDED

    with pytest.raises(ReconstructionDraftError):
        decode_reconstruction_draft(b'{"value":"\\ud800"}')


def test_adoption_identity_is_frozen_through_recovery() -> None:
    observation = reconstruction_payload(_observation()).ref
    proposal = ReconstructionPayloadRef(
        kind=ReconstructionPayloadKind.PROPOSAL,
        id="reconstruction_proposal_" + "a" * 32,
        contract_digest="b" * 64,
        sha256="c" * 64,
        size_bytes=1,
    )
    terminal = dataclasses.replace(
        _intent(),
        lifecycle=RuntimeLifecycleState.SUCCEEDED,
        start_receipt_sha256="d" * 64,
        result_sha256="e" * 64,
        output_sha256="f" * 64,
    )
    proposed = _draft(
        generation=2,
        status=ReconstructionStatus.PROPOSED,
        observation_ref=observation,
        proposal_ref=proposal,
        provider_invocations=(terminal,),
    )
    adoption_key, adoption_intent = derive_adoption_identity(
        proposed.reconstruction_id,
        proposal.contract_digest,
        proposed.base_head.sha256,
    )
    adopting = dataclasses.replace(
        proposed,
        generation=3,
        status=ReconstructionStatus.ADOPTING,
        adoption_key_sha256=adoption_key,
        adoption_intent_sha256=adoption_intent,
    )
    validate_reconstruction_successor(proposed, adopting)

    recovery = dataclasses.replace(
        adopting,
        generation=4,
        status=ReconstructionStatus.RECOVERY_REQUIRED,
        last_error=ReconstructionLastError(
            code="adoption.unknown",
            phase="reconcile",
            retryable=False,
            diagnostic_digest="1" * 64,
        ),
    )
    validate_reconstruction_successor(adopting, recovery)

    adopted = dataclasses.replace(
        recovery,
        generation=5,
        status=ReconstructionStatus.ADOPTED,
        adopted_task_id=derive_adoption_task_identity(adoption_key)[1],
        adopted_source_provenance=AdoptedSourceProvenance(
            source_sha256=("2" * 64,),
            proposal_digest=proposal.contract_digest,
        ),
        last_error=None,
    )
    validate_reconstruction_successor(recovery, adopted)

    with pytest.raises(ReconstructionDraftError):
        validate_reconstruction_successor(
            recovery,
            dataclasses.replace(adopted, adoption_key_sha256="3" * 64),
        )
