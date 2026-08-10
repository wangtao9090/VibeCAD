"""Focused tests for the application-owned service admission gate."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tests.test_application_proposal_admission import _admit, _durable_case
from tests.test_visual_adoption import _AdoptionProbe
from vibecad.application import visual_admission as gate_module
from vibecad.application.proposal_admission import (
    ProposalAdmissionError,
    ProposalAdmissionErrorCode,
)
from vibecad.application.visual_admission import ApplicationVisualAdmissionGate
from vibecad.visual.admission_gate import (
    VisualAdmissionGateError,
    VisualAdmissionGateErrorCode,
)
from vibecad.visual.drafts import ReconstructionLastError, derive_adoption_identity
from vibecad.visual.fake_provider import DeterministicFakeVisualProvider
from vibecad.visual.provider import VisualProviderBinding
from vibecad.visual.reconstruction import ReconstructionStatus
from vibecad.visual.service import (
    VisualReconstructionService,
    VisualServiceError,
    VisualServiceErrorCode,
)


def _gate(case) -> ApplicationVisualAdmissionGate:
    _layout, store, _draft, _proposal, visual_store, *_rest = case
    return ApplicationVisualAdmissionGate(
        reconstruction_store=store,
        visual_input_store=visual_store,
    )


def test_gate_binds_the_exact_application_stores_and_accepts_complete_sidecar(
    tmp_path: Path,
) -> None:
    case = _durable_case(tmp_path)
    _layout, store, draft, _proposal, visual_store, *_rest = case
    _admit(case)
    gate = _gate(case)

    assert gate.reconstruction_store is store
    assert gate.visual_input_store is visual_store
    assert (
        gate.require_exact(
            draft.reconstruction_id,
            expected_generation=draft.generation,
        )
        is None
    )


def test_legacy_proposed_draft_without_sidecar_is_not_ready(tmp_path: Path) -> None:
    case = _durable_case(tmp_path)
    _layout, _store, draft, _proposal, _visual_store, *_rest = case
    gate = _gate(case)

    with pytest.raises(VisualAdmissionGateError) as caught:
        gate.require_exact(
            draft.reconstruction_id,
            expected_generation=draft.generation,
        )

    assert caught.value.code is VisualAdmissionGateErrorCode.NOT_READY


def test_legacy_draft_cannot_reach_any_adoption_port_call(tmp_path: Path) -> None:
    case = _durable_case(tmp_path)
    _layout, store, draft, _proposal, visual_store, *_rest = case
    adoption = _AdoptionProbe(drafts=store)
    service = VisualReconstructionService(
        inputs=visual_store,
        drafts=store,
        provider=VisualProviderBinding(provider=DeterministicFakeVisualProvider({})),
        admission=_gate(case),
        adoption=adoption,
    )

    with pytest.raises(VisualServiceError) as caught:
        service.adopt(
            draft.reconstruction_id,
            expected_generation=draft.generation,
        )

    assert caught.value.code is VisualServiceErrorCode.INVALID_STATE
    assert adoption.inspect_calls == []
    assert adoption.ensure_calls == []
    assert adoption.reconcile_calls == []
    assert service.get(draft.reconstruction_id) == draft


def test_complete_sidecar_revalidates_before_the_adoption_port(tmp_path: Path) -> None:
    case = _durable_case(tmp_path)
    _layout, store, draft, _proposal, visual_store, *_rest = case
    _admit(case)
    adoption = _AdoptionProbe(drafts=store, current_head=draft.base_head)
    service = VisualReconstructionService(
        inputs=visual_store,
        drafts=store,
        provider=VisualProviderBinding(provider=DeterministicFakeVisualProvider({})),
        admission=_gate(case),
        adoption=adoption,
    )

    adopted = service.adopt(
        draft.reconstruction_id,
        expected_generation=draft.generation,
    )

    assert adopted.status is ReconstructionStatus.ADOPTED
    assert adoption.inspect_calls == [draft.base_head.project_id]
    assert len(adoption.ensure_calls) == 1
    assert adoption.reconcile_calls == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            ProposalAdmissionErrorCode.DRIFT_DETECTED,
            VisualAdmissionGateErrorCode.INTEGRITY_FAILURE,
        ),
        (
            ProposalAdmissionErrorCode.INTEGRITY_FAILURE,
            VisualAdmissionGateErrorCode.INTEGRITY_FAILURE,
        ),
        (
            ProposalAdmissionErrorCode.STORE_FAILURE,
            VisualAdmissionGateErrorCode.UNAVAILABLE,
        ),
    ],
)
def test_gate_maps_private_revalidation_failures_without_reflecting_paths(
    tmp_path: Path,
    monkeypatch,
    source: ProposalAdmissionErrorCode,
    expected: VisualAdmissionGateErrorCode,
) -> None:
    case = _durable_case(tmp_path)
    _layout, _store, draft, _proposal, _visual_store, *_rest = case
    gate = _gate(case)

    def fail(**_kwargs):
        raise ProposalAdmissionError(source, "/hostile/private/path")

    monkeypatch.setattr(gate_module, "revalidate_proposal_admission", fail)

    with pytest.raises(VisualAdmissionGateError) as caught:
        gate.require_exact(
            draft.reconstruction_id,
            expected_generation=draft.generation,
        )

    assert caught.value.code is expected
    assert "/hostile/private/path" not in str(caught.value)


@pytest.mark.parametrize(
    "status",
    [ReconstructionStatus.ADOPTING, ReconstructionStatus.RECOVERY_REQUIRED],
)
def test_exact_sidecar_revalidates_after_durable_adoption_restart(
    tmp_path: Path,
    status: ReconstructionStatus,
) -> None:
    case = _durable_case(tmp_path)
    _layout, store, draft, proposal, _visual_store, *_rest = case
    _admit(case)
    adoption_key, adoption_intent = derive_adoption_identity(
        draft.reconstruction_id,
        proposal.digest,
        draft.base_head.sha256,
    )
    successor = store.compare_and_set(
        draft.reconstruction_id,
        draft.generation,
        dataclasses.replace(
            draft,
            generation=draft.generation + 1,
            status=ReconstructionStatus.ADOPTING,
            adoption_key_sha256=adoption_key,
            adoption_intent_sha256=adoption_intent,
        ),
    )
    if status is ReconstructionStatus.RECOVERY_REQUIRED:
        successor = store.compare_and_set(
            successor.reconstruction_id,
            successor.generation,
            dataclasses.replace(
                successor,
                generation=successor.generation + 1,
                status=ReconstructionStatus.RECOVERY_REQUIRED,
                last_error=ReconstructionLastError(
                    code="adoption.unknown",
                    phase="restart",
                    retryable=False,
                    diagnostic_digest="f" * 64,
                ),
            ),
        )

    assert (
        _gate(case).require_exact(
            successor.reconstruction_id,
            expected_generation=successor.generation,
        )
        is None
    )


def test_gate_rejects_non_exact_store_composition() -> None:
    with pytest.raises(TypeError, match="invalid visual admission gate composition"):
        ApplicationVisualAdmissionGate(  # type: ignore[arg-type]
            reconstruction_store=object(),
            visual_input_store=object(),
        )
