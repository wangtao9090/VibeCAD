"""Bind the private proposal-admission coordinator to exact application stores."""

from __future__ import annotations

from dataclasses import dataclass

from vibecad.application.proposal_admission import (
    ProposalAdmissionError,
    ProposalAdmissionErrorCode,
    revalidate_proposal_admission,
)
from vibecad.visual.admission_gate import (
    VisualAdmissionGateError,
    VisualAdmissionGateErrorCode,
)
from vibecad.visual.admission_inputs import VisualAdmissionInputBundle
from vibecad.visual.inputs import VisualInputStore
from vibecad.visual.store import ReconstructionDraftStore


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplicationVisualAdmissionGate:
    """Recompute one stored bundle without accepting caller-derived authority."""

    reconstruction_store: ReconstructionDraftStore
    visual_input_store: VisualInputStore

    def __post_init__(self) -> None:
        if (
            type(self.reconstruction_store) is not ReconstructionDraftStore
            or type(self.visual_input_store) is not VisualInputStore
        ):
            raise TypeError("invalid visual admission gate composition")

    def require_exact(
        self,
        reconstruction_id: str,
        *,
        expected_generation: int,
    ) -> None:
        try:
            result = revalidate_proposal_admission(
                reconstruction_store=self.reconstruction_store,
                visual_input_store=self.visual_input_store,
                reconstruction_id=reconstruction_id,
                expected_generation=expected_generation,
            )
        except ProposalAdmissionError as error:
            if error.code in {
                ProposalAdmissionErrorCode.INVALID_INPUT,
                ProposalAdmissionErrorCode.BINDING_MISMATCH,
                ProposalAdmissionErrorCode.NOT_COMPLETE,
            }:
                code = VisualAdmissionGateErrorCode.NOT_READY
            elif error.code in {
                ProposalAdmissionErrorCode.DRIFT_DETECTED,
                ProposalAdmissionErrorCode.INTEGRITY_FAILURE,
            }:
                code = VisualAdmissionGateErrorCode.INTEGRITY_FAILURE
            else:
                code = VisualAdmissionGateErrorCode.UNAVAILABLE
            raise VisualAdmissionGateError(code) from None
        if type(result) is not VisualAdmissionInputBundle:
            raise VisualAdmissionGateError(VisualAdmissionGateErrorCode.INTEGRITY_FAILURE)


__all__: tuple[str, ...] = ()
