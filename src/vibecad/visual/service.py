"""Bounded visual reconstruction lifecycle orchestration.

The service composes sealed local visual inputs, durable reconstruction drafts,
and one admitted visual provider.  It owns no CAD, Task, review, or network
authority.  In particular, durable invocation intent is published before a
provider is started and an uncertain invocation is only reconciled, never
retried.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum

from vibecad.runtime.contracts import (
    RuntimeBudget,
    RuntimeDiagnostic,
    RuntimeEvidence,
    RuntimeInvocation,
    RuntimeLifecycleState,
    RuntimeProvenance,
    RuntimeResult,
    RuntimeStatus,
)
from vibecad.visual.adoption import (
    VisualAdoptionAbsenceReceipt,
    VisualAdoptionPort,
    VisualAdoptionReceipt,
    VisualAdoptionRequest,
    build_visual_adoption_request,
    validate_visual_adoption_absence_receipt,
    validate_visual_adoption_receipt,
)
from vibecad.visual.contracts import ProcessingAuthorization
from vibecad.visual.drafts import (
    AdoptedSourceProvenance,
    BaseHeadBinding,
    DeleteCleanup,
    ProviderInvocationRecord,
    ReconstructionDraft,
    ReconstructionLastError,
    decode_clarification_answer,
    derive_adoption_identity,
    reconstruction_payload,
)
from vibecad.visual.inputs import (
    VisualInputStore,
    VisualInputStoreError,
    VisualInputStoreErrorCode,
)
from vibecad.visual.provider import (
    VISUAL_PROVIDER_IDENTITY,
    VISUAL_PROVIDER_MODEL,
    VISUAL_PROVIDER_MODEL_VERSION,
    VisualProviderBinding,
    VisualProviderOutput,
    build_visual_provider_invocation,
    visual_provider_input_digest,
)
from vibecad.visual.reconstruction import (
    ReconstructionProposal,
    ReconstructionStatus,
    VisualObservation,
    clarification_answer_for_question,
    decode_reconstruction_proposal,
    decode_visual_observation,
    reconstruction_identity,
)
from vibecad.visual.store import ReconstructionDraftStore
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 1024 * 1024
_STATUS_DIGEST_DOMAIN = b"vibecad-visual-status-receipt-v1\0"
_RESULT_DIGEST_DOMAIN = b"vibecad-visual-result-receipt-v1\0"
_ERROR_DIGEST_DOMAIN = b"vibecad-visual-service-error-v1\0"


class VisualServiceErrorCode(StrEnum):
    """Stable service failures without provider-controlled text."""

    INVALID_INPUT = "invalid_input"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"
    ADOPTION_UNAVAILABLE = "adoption_unavailable"
    PROVIDER_RECEIPT_MISMATCH = "provider_receipt_mismatch"


class VisualServiceError(RuntimeError):
    def __init__(self, code: VisualServiceErrorCode) -> None:
        if type(code) is not VisualServiceErrorCode:
            raise TypeError("code must be an exact VisualServiceErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: VisualServiceErrorCode) -> None:
    raise VisualServiceError(code)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


def _canonical(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(VisualServiceErrorCode.PROVIDER_RECEIPT_MISMATCH)
    if len(raw) > _MAX_RECEIPT_BYTES:
        _fail(VisualServiceErrorCode.PROVIDER_RECEIPT_MISMATCH)
    return raw


def _diagnostic_mapping(value: RuntimeDiagnostic) -> dict[str, object]:
    return {
        "code": value.code,
        "message": value.message,
        "retryable": value.retryable,
        "details": _thaw(value.details),
    }


def _status_digest(status: RuntimeStatus) -> str:
    body = {
        "invocation_id": status.invocation_id,
        "runtime": {
            "family": status.runtime.family,
            "provider": status.runtime.provider,
            "version": status.runtime.version,
        },
        "state": status.state.value,
        "diagnostics": [_diagnostic_mapping(item) for item in status.diagnostics],
    }
    return hashlib.sha256(_STATUS_DIGEST_DOMAIN + _canonical(body)).hexdigest()


def _artifact_mapping(value) -> dict[str, object]:
    return {
        "artifact_id": value.artifact_id,
        "kind": value.kind,
        "media_type": value.media_type,
        "digest": value.digest,
        "runtime": {
            "family": value.runtime.family,
            "provider": value.runtime.provider,
            "version": value.runtime.version,
        },
        "metadata": _thaw(value.metadata),
    }


def _provenance_mapping(value: RuntimeProvenance | None) -> object:
    if value is None:
        return None
    return {
        "runtime": {
            "family": value.runtime.family,
            "provider": value.runtime.provider,
            "version": value.runtime.version,
        },
        "invocation_id": value.invocation_id,
        "input_artifact_ids": list(value.input_artifact_ids),
        "details": _thaw(value.details),
    }


def _evidence_mapping(value: RuntimeEvidence) -> dict[str, object]:
    return {"kind": value.kind, "name": value.name, "value": _thaw(value.value)}


def _result_digest(result: RuntimeResult) -> str:
    body = {
        "invocation_id": result.invocation_id,
        "runtime": {
            "family": result.runtime.family,
            "provider": result.runtime.provider,
            "version": result.runtime.version,
        },
        "state": result.state.value,
        "artifacts": [_artifact_mapping(item) for item in result.artifacts],
        "provenance": _provenance_mapping(result.provenance),
        "diagnostics": [_diagnostic_mapping(item) for item in result.diagnostics],
        "evidence": [_evidence_mapping(item) for item in result.evidence],
        "output": _thaw(result.output),
    }
    return hashlib.sha256(_RESULT_DIGEST_DOMAIN + _canonical(body)).hexdigest()


def _error_digest(*, code: str, phase: str) -> str:
    return hashlib.sha256(
        _ERROR_DIGEST_DOMAIN + _canonical({"code": code, "phase": phase})
    ).hexdigest()


def _last_error(*, code: str, phase: str, digest: str | None = None) -> ReconstructionLastError:
    return ReconstructionLastError(
        code=code,
        phase=phase,
        retryable=False,
        diagnostic_digest=digest or _error_digest(code=code, phase=phase),
    )


class VisualReconstructionService:
    """Coordinate the recoverable, provider-only portion of reconstruction."""

    __slots__ = ("_adoption", "_drafts", "_inputs", "_provider")

    def __init__(
        self,
        *,
        inputs: VisualInputStore,
        drafts: ReconstructionDraftStore,
        provider: VisualProviderBinding,
        adoption: VisualAdoptionPort | None = None,
    ) -> None:
        if (
            type(inputs) is not VisualInputStore
            or type(drafts) is not ReconstructionDraftStore
            or type(provider) is not VisualProviderBinding
            or (adoption is not None and not isinstance(adoption, VisualAdoptionPort))
        ):
            raise TypeError("invalid visual reconstruction service composition")
        self._inputs = inputs
        self._drafts = drafts
        self._provider = provider
        self._adoption = adoption

    def _load_expected(
        self,
        reconstruction_id: str,
        expected_generation: int,
    ) -> ReconstructionDraft:
        if (
            type(expected_generation) is not int
            or expected_generation < 0
            or expected_generation > MAX_SAFE_JSON_INTEGER
        ):
            _fail(VisualServiceErrorCode.INVALID_INPUT)
        draft = self._drafts.load(reconstruction_id)
        if draft.generation != expected_generation:
            _fail(VisualServiceErrorCode.CONFLICT)
        return draft

    def create(
        self,
        *,
        create_key: str,
        image_set_id: str,
        image_set_manifest_sha256: str,
        base_head: BaseHeadBinding,
    ) -> ReconstructionDraft:
        """Create a READY draft bound to one sealed local ImageSet manifest."""

        if type(base_head) is not BaseHeadBinding:
            raise TypeError("base_head must be an exact BaseHeadBinding")
        try:
            reconstruction_id, create_digest = reconstruction_identity(create_key)
        except (TypeError, ValueError):
            _fail(VisualServiceErrorCode.INVALID_INPUT)
        image_set = self._inputs.get(image_set_id)
        if (
            image_set.processing_authorization is not ProcessingAuthorization.LOCAL_ONLY
            or type(image_set_manifest_sha256) is not str
            or _DIGEST.fullmatch(image_set_manifest_sha256) is None
            or image_set.manifest_sha256 != image_set_manifest_sha256
        ):
            _fail(VisualServiceErrorCode.INVALID_INPUT)
        draft = ReconstructionDraft(
            reconstruction_id=reconstruction_id,
            create_key_sha256=create_digest,
            generation=0,
            status=ReconstructionStatus.READY,
            base_head=base_head,
            image_set_id=image_set.id,
            image_set_manifest_sha256=image_set.manifest_sha256,
        )
        created = self._drafts.create(draft)
        try:
            current_image_set = self._inputs.get(image_set.id)
        except VisualInputStoreError as error:
            if error.code is not VisualInputStoreErrorCode.NOT_FOUND:
                raise
            self.delete(
                created.reconstruction_id,
                expected_generation=created.generation,
            )
            _fail(VisualServiceErrorCode.CONFLICT)
        if current_image_set.manifest_sha256 != image_set.manifest_sha256:
            self.delete(
                created.reconstruction_id,
                expected_generation=created.generation,
            )
            _fail(VisualServiceErrorCode.CONFLICT)
        return created

    def get(self, reconstruction_id: str) -> ReconstructionDraft:
        return self._drafts.load(reconstruction_id)

    def run(
        self,
        reconstruction_id: str,
        *,
        expected_generation: int,
        budget: RuntimeBudget | None = None,
        deadline_ms: int | None = None,
    ) -> ReconstructionDraft:
        """Start a READY attempt or reconcile an already durable attempt."""

        draft = self._load_expected(reconstruction_id, expected_generation)
        if draft.status is ReconstructionStatus.FAILED:
            if type(budget) is not RuntimeBudget or type(deadline_ms) is not int:
                _fail(VisualServiceErrorCode.INVALID_INPUT)
            build_visual_provider_invocation(
                reconstruction_id=draft.reconstruction_id,
                generation=draft.generation + 2,
                image_set_id=draft.image_set_id,
                image_set_manifest_sha256=draft.image_set_manifest_sha256,
                clarification_answer_digests=tuple(
                    item.contract_digest for item in draft.clarification_refs
                ),
                budget=budget,
                deadline_ms=deadline_ms,
            )
            ready = dataclasses.replace(
                draft,
                generation=draft.generation + 1,
                status=ReconstructionStatus.READY,
                last_error=None,
            )
            draft = self._drafts.compare_and_set(
                draft.reconstruction_id,
                draft.generation,
                ready,
            )
        if draft.status is ReconstructionStatus.READY:
            if type(budget) is not RuntimeBudget or type(deadline_ms) is not int:
                _fail(VisualServiceErrorCode.INVALID_INPUT)
            invocation = build_visual_provider_invocation(
                reconstruction_id=draft.reconstruction_id,
                generation=draft.generation + 1,
                image_set_id=draft.image_set_id,
                image_set_manifest_sha256=draft.image_set_manifest_sha256,
                clarification_answer_digests=tuple(
                    item.contract_digest for item in draft.clarification_refs
                ),
                budget=budget,
                deadline_ms=deadline_ms,
            )
            intent = ProviderInvocationRecord(
                invocation_id=invocation.invocation_id,
                attempt_generation=draft.generation + 1,
                runtime=VISUAL_PROVIDER_IDENTITY,
                model=VISUAL_PROVIDER_MODEL,
                model_version=VISUAL_PROVIDER_MODEL_VERSION,
                budget=budget,
                deadline_ms=deadline_ms,
                input_sha256=visual_provider_input_digest(invocation),
            )
            observing = dataclasses.replace(
                draft,
                generation=draft.generation + 1,
                status=ReconstructionStatus.OBSERVING,
                provider_invocations=draft.provider_invocations + (intent,),
                last_error=None,
            )
            durable = self._drafts.compare_and_set(
                draft.reconstruction_id,
                draft.generation,
                observing,
            )
            try:
                status = self._provider.control.start(invocation)
            except Exception:
                return self._recover_after_exception(durable, phase="start")
            return self._consume_status(durable, invocation, status, phase="start")
        if draft.status is ReconstructionStatus.ADOPTING:
            if budget is not None or deadline_ms is not None:
                _fail(VisualServiceErrorCode.INVALID_INPUT)
            recovery = self._publish_adoption_recovery(draft, phase="restart")
            return self._reconcile_adoption(recovery)
        if draft.status in {
            ReconstructionStatus.OBSERVING,
            ReconstructionStatus.RECOVERY_REQUIRED,
        }:
            if budget is not None or deadline_ms is not None:
                _fail(VisualServiceErrorCode.INVALID_INPUT)
            if (
                draft.status is ReconstructionStatus.RECOVERY_REQUIRED
                and draft.adoption_key_sha256 is not None
            ):
                return self._reconcile_adoption(draft)
            invocation = self._reconstruct_invocation(draft)
            try:
                status = self._provider.control.reconcile(invocation.invocation_id)
            except Exception:
                return self._recover_after_exception(draft, phase="reconcile")
            return self._consume_status(draft, invocation, status, phase="reconcile")
        _fail(VisualServiceErrorCode.INVALID_STATE)

    def answer(
        self,
        reconstruction_id: str,
        *,
        expected_generation: int,
        question_id: str,
        response: bool | int | float | str,
    ) -> ReconstructionDraft:
        """Append one immutable answer bound to the current observation."""

        draft = self._load_expected(reconstruction_id, expected_generation)
        if draft.status is not ReconstructionStatus.NEEDS_INPUT or draft.observation_ref is None:
            _fail(VisualServiceErrorCode.INVALID_STATE)
        observation_payload = self._drafts.load_payload(
            draft.reconstruction_id,
            draft.observation_ref,
        )
        try:
            observation = decode_visual_observation(observation_payload.raw)
        except (TypeError, ValueError):
            _fail(VisualServiceErrorCode.INVALID_STATE)
        question = next((item for item in observation.questions if item.id == question_id), None)
        if question is None:
            _fail(VisualServiceErrorCode.INVALID_INPUT)
        for reference in draft.clarification_refs:
            payload = self._drafts.load_payload(draft.reconstruction_id, reference)
            try:
                existing = decode_clarification_answer(payload.raw)
            except (TypeError, ValueError):
                _fail(VisualServiceErrorCode.INVALID_STATE)
            if existing.question_id == question.id:
                _fail(VisualServiceErrorCode.CONFLICT)
        try:
            answer = clarification_answer_for_question(question, response)
            payload = reconstruction_payload(answer)
        except (TypeError, ValueError):
            _fail(VisualServiceErrorCode.INVALID_INPUT)
        successor = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.READY,
            clarification_refs=draft.clarification_refs + (payload.ref,),
        )
        return self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            successor,
            (payload,),
        )

    def reject(
        self,
        reconstruction_id: str,
        *,
        expected_generation: int,
    ) -> ReconstructionDraft:
        """Reject a stable reconstruction without touching CAD or HEAD state."""

        draft = self._load_expected(reconstruction_id, expected_generation)
        if draft.status not in {
            ReconstructionStatus.READY,
            ReconstructionStatus.NEEDS_INPUT,
            ReconstructionStatus.PROPOSED,
            ReconstructionStatus.FAILED,
        }:
            _fail(VisualServiceErrorCode.INVALID_STATE)
        successor = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.REJECTED,
            last_error=None,
        )
        return self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            successor,
        )

    def delete(
        self,
        reconstruction_id: str,
        *,
        expected_generation: int,
    ) -> ReconstructionDraft:
        """Delete visual sources through a resumable two-phase tombstone."""

        draft = self._load_expected(reconstruction_id, expected_generation)
        if draft.status is ReconstructionStatus.DELETED:
            if draft.delete_cleanup is None:
                return draft
            tombstone = draft
        else:
            if draft.status not in {
                ReconstructionStatus.READY,
                ReconstructionStatus.NEEDS_INPUT,
                ReconstructionStatus.PROPOSED,
                ReconstructionStatus.FAILED,
                ReconstructionStatus.REJECTED,
                ReconstructionStatus.ADOPTED,
            }:
                _fail(VisualServiceErrorCode.INVALID_STATE)
            cleanup = DeleteCleanup(
                image_set_id=draft.image_set_id,
                image_set_manifest_sha256=draft.image_set_manifest_sha256,
                payload_refs=draft.payload_refs,
            )
            adopted = draft.status is ReconstructionStatus.ADOPTED
            tombstone = ReconstructionDraft(
                reconstruction_id=draft.reconstruction_id,
                create_key_sha256=draft.create_key_sha256,
                generation=draft.generation + 1,
                status=ReconstructionStatus.DELETED,
                base_head=None,
                image_set_id=None,
                image_set_manifest_sha256=None,
                adoption_key_sha256=draft.adoption_key_sha256 if adopted else None,
                adoption_intent_sha256=(draft.adoption_intent_sha256 if adopted else None),
                adopted_task_id=draft.adopted_task_id if adopted else None,
                delete_cleanup=cleanup,
                adopted_source_provenance=(draft.adopted_source_provenance if adopted else None),
            )
            tombstone = self._drafts.compare_and_set(
                draft.reconstruction_id,
                draft.generation,
                tombstone,
            )
        cleanup = tombstone.delete_cleanup
        if cleanup is None:
            _fail(VisualServiceErrorCode.INVALID_STATE)
        if not cleanup.source_deleted:
            self._inputs.delete_exact(
                cleanup.image_set_id,
                cleanup.image_set_manifest_sha256,
            )
            source_deleted = dataclasses.replace(
                tombstone,
                generation=tombstone.generation + 1,
                delete_cleanup=dataclasses.replace(cleanup, source_deleted=True),
            )
            tombstone = self._drafts.compare_and_set(
                tombstone.reconstruction_id,
                tombstone.generation,
                source_deleted,
            )
            cleanup = tombstone.delete_cleanup
            if cleanup is None or not cleanup.source_deleted:
                _fail(VisualServiceErrorCode.INVALID_STATE)
        self._inputs.finalize_delete_exact(
            cleanup.image_set_id,
            cleanup.image_set_manifest_sha256,
        )
        finalized = dataclasses.replace(
            tombstone,
            generation=tombstone.generation + 1,
            delete_cleanup=None,
        )
        return self._drafts.compare_and_set(
            tombstone.reconstruction_id,
            tombstone.generation,
            finalized,
        )

    def adopt(
        self,
        reconstruction_id: str,
        *,
        expected_generation: int,
    ) -> ReconstructionDraft:
        """Persist adoption intent, then hand the proposal to the ordinary Task kernel."""

        draft = self._load_expected(reconstruction_id, expected_generation)
        if (
            draft.status is not ReconstructionStatus.PROPOSED
            or draft.proposal_ref is None
            or draft.base_head is None
            or self._adoption is None
        ):
            _fail(VisualServiceErrorCode.INVALID_STATE)
        proposal = self._load_proposal(draft)
        try:
            current_head = self._adoption.inspect_head(draft.base_head.project_id)
        except Exception:
            _fail(VisualServiceErrorCode.ADOPTION_UNAVAILABLE)
        if type(current_head) is not BaseHeadBinding or current_head != draft.base_head:
            _fail(VisualServiceErrorCode.CONFLICT)
        adoption_key, adoption_intent = derive_adoption_identity(
            draft.reconstruction_id,
            proposal.digest,
            draft.base_head.sha256,
        )
        request = build_visual_adoption_request(
            reconstruction_id=draft.reconstruction_id,
            adoption_key_sha256=adoption_key,
            adoption_intent_sha256=adoption_intent,
            base_head=draft.base_head,
            proposal=proposal,
        )
        adopting = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.ADOPTING,
            adoption_key_sha256=adoption_key,
            adoption_intent_sha256=adoption_intent,
        )
        durable = self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            adopting,
        )
        try:
            receipt = self._adoption.ensure_review_task(request)
        except Exception:
            return self._publish_adoption_recovery(durable, phase="ensure_task")
        return self._complete_adoption(durable, request, receipt)

    def _load_proposal(self, draft: ReconstructionDraft) -> ReconstructionProposal:
        if draft.proposal_ref is None:
            _fail(VisualServiceErrorCode.INVALID_STATE)
        payload = self._drafts.load_payload(draft.reconstruction_id, draft.proposal_ref)
        try:
            proposal = decode_reconstruction_proposal(payload.raw)
        except (TypeError, ValueError):
            _fail(VisualServiceErrorCode.INVALID_STATE)
        durable_answer_digests = {
            reference.contract_digest for reference in draft.clarification_refs
        }
        if proposal.digest != draft.proposal_ref.contract_digest or any(
            answer.digest not in durable_answer_digests for answer in proposal.clarification_answers
        ):
            _fail(VisualServiceErrorCode.INVALID_STATE)
        return proposal

    def _adoption_request_for(self, draft: ReconstructionDraft) -> VisualAdoptionRequest:
        if (
            draft.base_head is None
            or draft.adoption_key_sha256 is None
            or draft.adoption_intent_sha256 is None
        ):
            _fail(VisualServiceErrorCode.INVALID_STATE)
        return build_visual_adoption_request(
            reconstruction_id=draft.reconstruction_id,
            adoption_key_sha256=draft.adoption_key_sha256,
            adoption_intent_sha256=draft.adoption_intent_sha256,
            base_head=draft.base_head,
            proposal=self._load_proposal(draft),
        )

    def _reconcile_adoption(self, draft: ReconstructionDraft) -> ReconstructionDraft:
        if self._adoption is None:
            _fail(VisualServiceErrorCode.INVALID_STATE)
        request = self._adoption_request_for(draft)
        try:
            receipt = self._adoption.reconcile_review_task(request)
        except Exception:
            return self._publish_adoption_recovery(draft, phase="reconcile_task")
        if type(receipt) is VisualAdoptionAbsenceReceipt:
            try:
                validate_visual_adoption_absence_receipt(request, receipt)
            except Exception:
                return self._publish_adoption_recovery(draft, phase="reconcile_task")
            proposed = dataclasses.replace(
                draft,
                generation=draft.generation + 1,
                status=ReconstructionStatus.PROPOSED,
                adoption_key_sha256=None,
                adoption_intent_sha256=None,
                last_error=None,
            )
            return self._drafts.compare_and_set(
                draft.reconstruction_id,
                draft.generation,
                proposed,
            )
        return self._complete_adoption(draft, request, receipt)

    def _publish_adoption_recovery(
        self,
        draft: ReconstructionDraft,
        *,
        phase: str,
    ) -> ReconstructionDraft:
        successor = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.RECOVERY_REQUIRED,
            last_error=_last_error(code="adoption.unknown", phase=phase),
        )
        return self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            successor,
        )

    def _complete_adoption(
        self,
        draft: ReconstructionDraft,
        request: VisualAdoptionRequest,
        receipt: VisualAdoptionReceipt | None,
    ) -> ReconstructionDraft:
        if receipt is None:
            return self._publish_adoption_recovery(draft, phase="task_receipt")
        try:
            validated = validate_visual_adoption_receipt(request, receipt)
            image_set = self._inputs.get(draft.image_set_id)
        except Exception:
            return self._publish_adoption_recovery(draft, phase="task_receipt")
        if image_set.manifest_sha256 != draft.image_set_manifest_sha256:
            return self._publish_adoption_recovery(draft, phase="source_provenance")
        provenance = AdoptedSourceProvenance(
            source_sha256=tuple(item.original.sha256 for item in image_set.inputs),
            proposal_digest=request.proposal.digest,
        )
        successor = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.ADOPTED,
            adopted_task_id=validated.task_id,
            adopted_source_provenance=provenance,
            last_error=None,
        )
        return self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            successor,
        )

    def _reconstruct_invocation(self, draft: ReconstructionDraft) -> RuntimeInvocation:
        if not draft.provider_invocations:
            _fail(VisualServiceErrorCode.INVALID_STATE)
        record = draft.provider_invocations[-1]
        if (
            record.is_terminal
            or record.runtime != VISUAL_PROVIDER_IDENTITY
            or record.model != VISUAL_PROVIDER_MODEL
            or record.model_version != VISUAL_PROVIDER_MODEL_VERSION
        ):
            _fail(VisualServiceErrorCode.INVALID_STATE)
        invocation = build_visual_provider_invocation(
            reconstruction_id=draft.reconstruction_id,
            generation=record.attempt_generation,
            image_set_id=draft.image_set_id,
            image_set_manifest_sha256=draft.image_set_manifest_sha256,
            clarification_answer_digests=tuple(
                item.contract_digest for item in draft.clarification_refs
            ),
            budget=record.budget,
            deadline_ms=record.deadline_ms,
        )
        if (
            invocation.invocation_id != record.invocation_id
            or visual_provider_input_digest(invocation) != record.input_sha256
        ):
            _fail(VisualServiceErrorCode.INVALID_STATE)
        return invocation

    def _recover_after_exception(
        self,
        draft: ReconstructionDraft,
        *,
        phase: str,
    ) -> ReconstructionDraft:
        successor = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.RECOVERY_REQUIRED,
            last_error=_last_error(code="provider.exception", phase=phase),
        )
        return self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            successor,
        )

    @staticmethod
    def _checked_status(invocation: RuntimeInvocation, status: object) -> RuntimeStatus:
        if (
            type(status) is not RuntimeStatus
            or status.invocation_id != invocation.invocation_id
            or status.runtime != invocation.runtime
        ):
            _fail(VisualServiceErrorCode.PROVIDER_RECEIPT_MISMATCH)
        return status

    def _consume_status(
        self,
        draft: ReconstructionDraft,
        invocation: RuntimeInvocation,
        status: object,
        *,
        phase: str,
    ) -> ReconstructionDraft:
        try:
            checked = self._checked_status(invocation, status)
        except VisualServiceError:
            return self._recover_after_exception(draft, phase=phase)
        receipt_digest = _status_digest(checked)
        record = draft.provider_invocations[-1]
        start_digest = record.start_receipt_sha256 or receipt_digest

        if checked.state is RuntimeLifecycleState.UNKNOWN:
            updated = dataclasses.replace(
                record,
                lifecycle=RuntimeLifecycleState.UNKNOWN,
                start_receipt_sha256=start_digest,
                diagnostic_digest=receipt_digest,
            )
            return self._publish_recovery(
                draft,
                updated,
                code="provider.unknown",
                phase=phase,
                digest=receipt_digest,
            )
        if not checked.state.is_terminal:
            updated = dataclasses.replace(
                record,
                lifecycle=checked.state,
                start_receipt_sha256=start_digest,
                diagnostic_digest=receipt_digest,
            )
            recovering = draft.status is ReconstructionStatus.RECOVERY_REQUIRED
            successor = dataclasses.replace(
                draft,
                generation=draft.generation + 1,
                status=(
                    ReconstructionStatus.RECOVERY_REQUIRED
                    if recovering
                    else ReconstructionStatus.OBSERVING
                ),
                provider_invocations=draft.provider_invocations[:-1] + (updated,),
                last_error=(
                    _last_error(
                        code="provider.reconcile_pending",
                        phase="reconcile",
                        digest=receipt_digest,
                    )
                    if recovering
                    else None
                ),
            )
            return self._drafts.compare_and_set(
                draft.reconstruction_id,
                draft.generation,
                successor,
            )
        try:
            result = self._provider.retrieve_result(invocation)
        except Exception:
            updated = dataclasses.replace(
                record,
                lifecycle=RuntimeLifecycleState.UNKNOWN,
                start_receipt_sha256=start_digest,
                diagnostic_digest=receipt_digest,
            )
            return self._publish_recovery(
                draft,
                updated,
                code="provider.result_unknown",
                phase="result",
                digest=receipt_digest,
            )
        if result is None or result.state is not checked.state:
            updated = dataclasses.replace(
                record,
                lifecycle=RuntimeLifecycleState.UNKNOWN,
                start_receipt_sha256=start_digest,
                diagnostic_digest=receipt_digest,
            )
            return self._publish_recovery(
                draft,
                updated,
                code="provider.result_unknown",
                phase="result",
                digest=receipt_digest,
            )
        return self._publish_result(
            draft,
            record,
            result,
            start_digest=start_digest,
            status_digest=receipt_digest,
        )

    def _publish_recovery(
        self,
        draft: ReconstructionDraft,
        record: ProviderInvocationRecord,
        *,
        code: str,
        phase: str,
        digest: str,
    ) -> ReconstructionDraft:
        successor = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.RECOVERY_REQUIRED,
            provider_invocations=draft.provider_invocations[:-1] + (record,),
            last_error=_last_error(code=code, phase=phase, digest=digest),
        )
        return self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            successor,
        )

    def _publish_result(
        self,
        draft: ReconstructionDraft,
        record: ProviderInvocationRecord,
        result: RuntimeResult,
        *,
        start_digest: str,
        status_digest: str,
    ) -> ReconstructionDraft:
        receipt = _result_digest(result)
        if result.state is RuntimeLifecycleState.SUCCEEDED:
            output = VisualProviderOutput.from_mapping(result.output)
            payloads = ()
            if type(output.value) is VisualObservation:
                observation = reconstruction_payload(output.value)
                payloads = (observation,)
                status = (
                    ReconstructionStatus.NEEDS_INPUT
                    if output.value.questions
                    else ReconstructionStatus.READY
                )
                observation_ref = observation.ref
                proposal_ref = draft.proposal_ref
            elif type(output.value) is ReconstructionProposal:
                observation = reconstruction_payload(output.value.observation)
                proposal = reconstruction_payload(output.value)
                payloads = (observation, proposal)
                status = ReconstructionStatus.PROPOSED
                observation_ref = observation.ref
                proposal_ref = proposal.ref
            else:  # pragma: no cover - VisualProviderOutput is already strict.
                _fail(VisualServiceErrorCode.PROVIDER_RECEIPT_MISMATCH)
            updated = dataclasses.replace(
                record,
                lifecycle=result.state,
                start_receipt_sha256=start_digest,
                result_sha256=receipt,
                output_sha256=output.output_digest,
                diagnostic_digest=status_digest,
            )
            successor = dataclasses.replace(
                draft,
                generation=draft.generation + 1,
                status=status,
                observation_ref=observation_ref,
                proposal_ref=proposal_ref,
                provider_invocations=draft.provider_invocations[:-1] + (updated,),
                last_error=None,
            )
            return self._drafts.compare_and_set(
                draft.reconstruction_id,
                draft.generation,
                successor,
                payloads,
            )

        updated = dataclasses.replace(
            record,
            lifecycle=result.state,
            start_receipt_sha256=start_digest,
            result_sha256=receipt,
            diagnostic_digest=status_digest,
        )
        code = (
            "provider.failed"
            if result.state is RuntimeLifecycleState.FAILED
            else "provider.cancelled"
        )
        successor = dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.FAILED,
            provider_invocations=draft.provider_invocations[:-1] + (updated,),
            last_error=_last_error(code=code, phase="result", digest=receipt),
        )
        return self._drafts.compare_and_set(
            draft.reconstruction_id,
            draft.generation,
            successor,
        )
