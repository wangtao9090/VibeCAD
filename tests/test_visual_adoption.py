from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.test_visual_service import (
    _budget,
    _create,
    _head,
    _invocation,
    _observation,
    _proposal,
    _question_observation,
    _sealed_image_set,
    _stores,
)
from vibecad.runtime.contracts import RuntimeDiagnostic
from vibecad.visual.adoption import (
    VisualAdoptionAbsenceReceipt,
    VisualAdoptionReceipt,
    VisualAdoptionRequest,
    VisualAdoptionWithdrawalReceipt,
    visual_adoption_program_digest,
)
from vibecad.visual.drafts import (
    BaseHeadBinding,
    derive_adoption_identity,
    derive_adoption_task_identity,
)
from vibecad.visual.fake_provider import (
    DeterministicFakeVisualProvider,
    FakeVisualFixture,
    FakeVisualOutcomeKind,
)
from vibecad.visual.provider import (
    VisualProviderBinding,
    VisualProviderError,
    build_visual_provider_invocation,
    visual_provider_input_digest,
)
from vibecad.visual.reconstruction import ReconstructionStatus
from vibecad.visual.service import (
    VisualReconstructionService,
    VisualServiceError,
    VisualServiceErrorCode,
)


def _receipt(request: VisualAdoptionRequest) -> VisualAdoptionReceipt:
    return VisualAdoptionReceipt(
        task_id=request.task_id,
        adoption_intent_sha256=request.adoption_intent_sha256,
        base_head_sha256=request.base_head.sha256,
        program_sha256=request.program_sha256,
    )


def _absence_receipt(request: VisualAdoptionRequest) -> VisualAdoptionAbsenceReceipt:
    return VisualAdoptionAbsenceReceipt(
        task_id=request.task_id,
        adoption_intent_sha256=request.adoption_intent_sha256,
        base_head_sha256=request.base_head.sha256,
        program_sha256=request.program_sha256,
    )


def _withdrawal_receipt(request: VisualAdoptionRequest) -> VisualAdoptionWithdrawalReceipt:
    return VisualAdoptionWithdrawalReceipt(
        task_id=request.task_id,
        adoption_intent_sha256=request.adoption_intent_sha256,
        base_head_sha256=request.base_head.sha256,
        program_sha256=request.program_sha256,
        cancelled_generation=1,
    )


class _AdoptionProbe:
    def __init__(
        self,
        *,
        drafts,
        current_head: BaseHeadBinding | None = None,
        ensure_result: str = "receipt",
        reconcile_result: str = "receipt",
    ) -> None:
        self._drafts = drafts
        self._head = current_head or _head()
        self._ensure_result = ensure_result
        self._reconcile_result = reconcile_result
        self.ensure_calls: list[VisualAdoptionRequest] = []
        self.reconcile_calls: list[VisualAdoptionRequest] = []

    def inspect_head(self, project_id: str) -> BaseHeadBinding:
        assert project_id == self._head.project_id
        return self._head

    def ensure_review_task(
        self,
        request: VisualAdoptionRequest,
    ) -> VisualAdoptionReceipt | None:
        durable = self._drafts.load(request.reconstruction_id)
        assert durable.status is ReconstructionStatus.ADOPTING
        assert durable.adoption_key_sha256 == request.adoption_key_sha256
        assert durable.adoption_intent_sha256 == request.adoption_intent_sha256
        self.ensure_calls.append(request)
        if self._ensure_result == "none":
            return None
        if self._ensure_result == "wrong":
            return dataclasses.replace(
                _receipt(request),
                adoption_intent_sha256="0" * 64,
                receipt_sha256="",
            )
        return _receipt(request)

    def reconcile_review_task(
        self,
        request: VisualAdoptionRequest,
    ) -> (
        VisualAdoptionReceipt
        | VisualAdoptionAbsenceReceipt
        | VisualAdoptionWithdrawalReceipt
        | None
    ):
        durable = self._drafts.load(request.reconstruction_id)
        assert durable.status is ReconstructionStatus.RECOVERY_REQUIRED
        self.reconcile_calls.append(request)
        if self._reconcile_result == "none":
            return None
        if self._reconcile_result == "absent":
            return _absence_receipt(request)
        if self._reconcile_result == "wrong_absent":
            return dataclasses.replace(
                _absence_receipt(request),
                program_sha256="0" * 64,
                receipt_sha256="",
            )
        if self._reconcile_result == "withdrawn":
            return _withdrawal_receipt(request)
        if self._reconcile_result == "wrong_withdrawn":
            return dataclasses.replace(
                _withdrawal_receipt(request),
                adoption_intent_sha256="0" * 64,
                receipt_sha256="",
            )
        return _receipt(request)


def _service(inputs, drafts, provider, adoption=None) -> VisualReconstructionService:
    return VisualReconstructionService(
        inputs=inputs,
        drafts=drafts,
        provider=VisualProviderBinding(provider=provider),
        adoption=adoption,
    )


def _proposed(tmp_path: Path, *, adoption=None):
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    proposal = _proposal(_observation(invocation))
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.PROPOSAL,
                value=proposal,
            )
        }
    )
    service = _service(inputs, drafts, provider, adoption)
    ready = _create(service, image_set)
    proposed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    assert proposed.status is ReconstructionStatus.PROPOSED
    return inputs, drafts, image_set, provider, service, proposed, proposal


def test_adoption_persists_exact_hidden_task_intent_before_effect_and_records_provenance(
    tmp_path: Path,
) -> None:
    inputs, drafts, image_set, provider, _, proposed, proposal = _proposed(tmp_path)
    adoption = _AdoptionProbe(drafts=drafts)
    service = _service(inputs, drafts, provider, adoption)

    adopted = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )

    assert len(adoption.ensure_calls) == 1
    request = adoption.ensure_calls[0]
    expected_create_key, expected_task_id = derive_adoption_task_identity(
        adopted.adoption_key_sha256
    )
    assert request.task_create_key == expected_create_key
    assert request.task_id == expected_task_id
    assert request.program.task_id == expected_task_id
    assert request.program.base_revision == proposed.base_head.revision_id
    assert request.program.acceptance == proposal.acceptance
    assert visual_adoption_program_digest(request.program) == request.program_sha256
    assert request.program.to_mapping()["operations"] == [
        {
            "schema_version": 1,
            "id": "visual-adoption-create-design",
            "op": "create_parametric_design",
            "args": {"design": proposal.design.to_mapping()},
            "source": "model",
            "target": {},
            "preserve": [],
            "depends_on": [],
        }
    ]
    assert adopted.status is ReconstructionStatus.ADOPTED
    assert adopted.adopted_task_id == expected_task_id
    assert adopted.adopted_source_provenance.proposal_digest == proposal.digest
    assert adopted.adopted_source_provenance.source_sha256 == tuple(
        item.original.sha256 for item in image_set.inputs
    )
    assert service.get(adopted.reconstruction_id) == adopted


def test_adoption_head_mismatch_has_no_task_effect(tmp_path: Path) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    changed_head = dataclasses.replace(_head(), generation=_head().generation + 1, sha256="")
    adoption = _AdoptionProbe(drafts=drafts, current_head=changed_head)
    service = _service(inputs, drafts, provider, adoption)

    with pytest.raises(VisualServiceError) as caught:
        service.adopt(
            proposed.reconstruction_id,
            expected_generation=proposed.generation,
        )

    assert caught.value.code is VisualServiceErrorCode.CONFLICT
    assert adoption.ensure_calls == []
    assert adoption.reconcile_calls == []
    assert service.get(proposed.reconstruction_id) == proposed


def test_lost_adoption_response_recovers_by_reconcile_without_duplicate_ensure(
    tmp_path: Path,
) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(drafts=drafts, ensure_result="none")
    service = _service(inputs, drafts, provider, adoption)

    recovery = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )
    adopted = _service(inputs, drafts, provider, adoption).run(
        recovery.reconstruction_id,
        expected_generation=recovery.generation,
    )

    assert recovery.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert adopted.status is ReconstructionStatus.ADOPTED
    assert len(adoption.ensure_calls) == 1
    assert len(adoption.reconcile_calls) == 1
    assert adoption.reconcile_calls[0] == adoption.ensure_calls[0]
    assert provider.execution_count == 1


def test_restart_from_durable_adopting_publishes_recovery_then_only_reconciles(
    tmp_path: Path,
) -> None:
    inputs, drafts, _, provider, _, proposed, proposal = _proposed(tmp_path)
    adoption_key, adoption_intent = derive_adoption_identity(
        proposed.reconstruction_id,
        proposal.digest,
        proposed.base_head.sha256,
    )
    adopting = drafts.compare_and_set(
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
    adoption = _AdoptionProbe(drafts=drafts)

    adopted = _service(inputs, drafts, provider, adoption).run(
        adopting.reconstruction_id,
        expected_generation=adopting.generation,
    )

    assert adopted.status is ReconstructionStatus.ADOPTED
    assert adoption.ensure_calls == []
    assert len(adoption.reconcile_calls) == 1


def test_wrong_adoption_receipt_enters_recovery(tmp_path: Path) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(drafts=drafts, ensure_result="wrong")
    service = _service(inputs, drafts, provider, adoption)

    recovery = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )

    assert recovery.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert recovery.last_error.code == "adoption.unknown"
    assert len(adoption.ensure_calls) == 1
    assert adoption.reconcile_calls == []


def test_settled_absence_returns_to_proposed_without_replaying_task_effect(
    tmp_path: Path,
) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(
        drafts=drafts,
        ensure_result="none",
        reconcile_result="absent",
    )
    service = _service(inputs, drafts, provider, adoption)

    recovery = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )
    settled = _service(inputs, drafts, provider, adoption).run(
        recovery.reconstruction_id,
        expected_generation=recovery.generation,
    )

    assert settled.status is ReconstructionStatus.PROPOSED
    assert settled.adoption_key_sha256 is None
    assert settled.adoption_intent_sha256 is None
    assert settled.last_error is None
    assert len(adoption.ensure_calls) == 1
    assert len(adoption.reconcile_calls) == 1


def test_mismatched_absence_receipt_remains_recovery_required(tmp_path: Path) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(
        drafts=drafts,
        ensure_result="none",
        reconcile_result="wrong_absent",
    )
    service = _service(inputs, drafts, provider, adoption)

    recovery = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )
    still_recovering = service.run(
        recovery.reconstruction_id,
        expected_generation=recovery.generation,
    )

    assert still_recovering.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert len(adoption.ensure_calls) == 1
    assert len(adoption.reconcile_calls) == 1


def test_exact_partial_withdrawal_returns_to_proposed_then_allows_reject_and_delete(
    tmp_path: Path,
) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(
        drafts=drafts,
        ensure_result="none",
        reconcile_result="withdrawn",
    )
    service = _service(inputs, drafts, provider, adoption)
    recovery = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )

    withdrawn = service.run(
        recovery.reconstruction_id,
        expected_generation=recovery.generation,
    )

    assert withdrawn.status is ReconstructionStatus.PROPOSED
    assert withdrawn.adoption_key_sha256 is None
    assert withdrawn.adoption_intent_sha256 is None
    assert withdrawn.adopted_task_id is None
    assert withdrawn.last_error is None
    rejected = service.reject(
        withdrawn.reconstruction_id,
        expected_generation=withdrawn.generation,
    )
    assert rejected.status is ReconstructionStatus.REJECTED
    deleted = service.delete(
        rejected.reconstruction_id,
        expected_generation=rejected.generation,
    )
    assert deleted.status is ReconstructionStatus.DELETED


def test_mismatched_withdrawal_receipt_remains_recovery_required(tmp_path: Path) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(
        drafts=drafts,
        ensure_result="none",
        reconcile_result="wrong_withdrawn",
    )
    service = _service(inputs, drafts, provider, adoption)
    recovery = service.adopt(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )

    still_recovering = service.run(
        recovery.reconstruction_id,
        expected_generation=recovery.generation,
    )

    assert still_recovering.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert len(adoption.ensure_calls) == 1
    assert len(adoption.reconcile_calls) == 1


def test_answer_valid_wrong_duplicate_and_stale_are_distinct(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    first_invocation = _invocation(image_set)
    first_observation = _question_observation(first_invocation)
    first_provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(first_invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=first_observation,
            )
        }
    )
    service = _service(inputs, drafts, first_provider)
    ready = _create(service, image_set)
    needs_input = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    question = first_observation.questions[0]

    with pytest.raises(VisualServiceError) as wrong:
        service.answer(
            needs_input.reconstruction_id,
            expected_generation=needs_input.generation,
            question_id=question.id,
            response="not-a-boolean",
        )
    assert wrong.value.code is VisualServiceErrorCode.INVALID_INPUT

    with pytest.raises(VisualServiceError) as wrong_question:
        service.answer(
            needs_input.reconstruction_id,
            expected_generation=needs_input.generation,
            question_id="clarification_question_" + "0" * 32,
            response=True,
        )
    assert wrong_question.value.code is VisualServiceErrorCode.INVALID_INPUT

    answered = service.answer(
        needs_input.reconstruction_id,
        expected_generation=needs_input.generation,
        question_id=question.id,
        response=True,
    )
    assert answered.status is ReconstructionStatus.READY
    assert len(answered.clarification_refs) == 1

    with pytest.raises(VisualServiceError) as stale:
        service.answer(
            needs_input.reconstruction_id,
            expected_generation=needs_input.generation,
            question_id=question.id,
            response=True,
        )
    assert stale.value.code is VisualServiceErrorCode.CONFLICT

    second_invocation = build_visual_provider_invocation(
        reconstruction_id=answered.reconstruction_id,
        generation=answered.generation + 1,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        clarification_answer_digests=(answered.clarification_refs[0].contract_digest,),
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    second_observation = _question_observation(second_invocation)
    second_provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(second_invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=second_observation,
            )
        }
    )
    repeated_question = _service(inputs, drafts, second_provider).run(
        answered.reconstruction_id,
        expected_generation=answered.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    assert repeated_question.status is ReconstructionStatus.NEEDS_INPUT

    with pytest.raises(VisualServiceError) as duplicate:
        _service(inputs, drafts, second_provider).answer(
            repeated_question.reconstruction_id,
            expected_generation=repeated_question.generation,
            question_id=question.id,
            response=True,
        )
    assert duplicate.value.code is VisualServiceErrorCode.CONFLICT


def test_failed_retry_starts_exactly_one_new_invocation(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    first = _invocation(image_set)
    retry = _invocation(image_set, generation=4)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(first): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.FAILURE,
                diagnostic=RuntimeDiagnostic(
                    code="fixture.failure",
                    message="First attempt fails",
                    retryable=False,
                ),
            ),
            visual_provider_input_digest(retry): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=_observation(retry),
            ),
        }
    )
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)
    failed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    with pytest.raises(VisualProviderError):
        service.run(
            failed.reconstruction_id,
            expected_generation=failed.generation,
            budget=_budget(),
            deadline_ms=0,
        )
    assert service.get(failed.reconstruction_id) == failed

    completed = service.run(
        failed.reconstruction_id,
        expected_generation=failed.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert failed.status is ReconstructionStatus.FAILED
    assert completed.status is ReconstructionStatus.READY
    assert provider.execution_count == 2
    assert len(completed.provider_invocations) == 2
    assert completed.provider_invocations[0].invocation_id == first.invocation_id
    assert completed.provider_invocations[1].invocation_id == retry.invocation_id


def test_reject_has_no_new_provider_or_adoption_effect(tmp_path: Path) -> None:
    inputs, drafts, _, provider, _, proposed, _ = _proposed(tmp_path)
    adoption = _AdoptionProbe(drafts=drafts)
    service = _service(inputs, drafts, provider, adoption)
    executions_before = provider.execution_count

    rejected = service.reject(
        proposed.reconstruction_id,
        expected_generation=proposed.generation,
    )

    assert rejected.status is ReconstructionStatus.REJECTED
    assert provider.execution_count == executions_before
    assert adoption.ensure_calls == []
    assert adoption.reconcile_calls == []
