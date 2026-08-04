"""Application-owned handoff from a visual proposal to the ordinary Task kernel.

This module defines a narrow, idempotency-bound port.  The visual provider never
receives this port: only the application composition root may implement it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from vibecad.visual.drafts import (
    BaseHeadBinding,
    derive_adoption_identity,
    derive_adoption_task_identity,
)
from vibecad.visual.reconstruction import ReconstructionProposal
from vibecad.workflow.contracts import ModelCommand, ModelProgram, ValueSource

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_PROGRAM_DIGEST_DOMAIN = b"vibecad-visual-adoption-program-v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad-visual-adoption-receipt-v1\0"
_ABSENCE_RECEIPT_DIGEST_DOMAIN = b"vibecad-visual-adoption-absence-v1\0"
_MAX_ADOPTION_BYTES = 512 * 1024


class VisualAdoptionContractError(ValueError):
    """Bounded rejection for a mismatched adoption request or receipt."""

    def __init__(self) -> None:
        super().__init__("invalid_visual_adoption_contract")


def _fail() -> None:
    raise VisualAdoptionContractError


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail()
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
        _fail()
    if not raw or len(raw) > _MAX_ADOPTION_BYTES:
        _fail()
    return raw


def visual_adoption_program_digest(program: object) -> str:
    if type(program) is not ModelProgram:
        _fail()
    return hashlib.sha256(_PROGRAM_DIGEST_DOMAIN + _canonical(program.to_mapping())).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualAdoptionRequest:
    """One immutable request for an ordinary REQUIRE_REVIEW Task."""

    reconstruction_id: str
    adoption_key_sha256: str
    adoption_intent_sha256: str
    task_create_key: str
    task_id: str
    base_head: BaseHeadBinding
    proposal: ReconstructionProposal
    program: ModelProgram
    program_sha256: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.base_head) is not BaseHeadBinding
            or type(self.proposal) is not ReconstructionProposal
        ):
            _fail()
        expected_key, expected_intent = derive_adoption_identity(
            self.reconstruction_id,
            self.proposal.digest,
            self.base_head.sha256,
        )
        expected_create_key, expected_task_id = derive_adoption_task_identity(expected_key)
        if (
            not hmac.compare_digest(_digest(self.adoption_key_sha256), expected_key)
            or not hmac.compare_digest(_digest(self.adoption_intent_sha256), expected_intent)
            or self.task_create_key != expected_create_key
            or self.task_id != expected_task_id
            or self.proposal.observation.reconstruction_id != self.reconstruction_id
            or type(self.program) is not ModelProgram
            or self.program.task_id != expected_task_id
            or self.program.base_revision != self.base_head.revision_id
            or self.program.acceptance != self.proposal.acceptance
            or len(self.program.operations) != 1
        ):
            _fail()
        operation = self.program.operations[0]
        if (
            operation.op != "create_parametric_design"
            or operation.source is not ValueSource.MODEL
            or operation.target
            or operation.preserve
            or operation.depends_on
            or operation.to_mapping()["args"] != {"design": self.proposal.design.to_mapping()}
        ):
            _fail()
        expected_program_sha256 = visual_adoption_program_digest(self.program)
        if self.program_sha256 and not hmac.compare_digest(
            _digest(self.program_sha256), expected_program_sha256
        ):
            _fail()
        object.__setattr__(self, "program_sha256", expected_program_sha256)


def build_visual_adoption_request(
    *,
    reconstruction_id: str,
    adoption_key_sha256: str,
    adoption_intent_sha256: str,
    base_head: BaseHeadBinding,
    proposal: ReconstructionProposal,
) -> VisualAdoptionRequest:
    """Build the only admitted hidden parametric program for visual adoption."""

    task_create_key, task_id = derive_adoption_task_identity(adoption_key_sha256)
    program = ModelProgram(
        task_id=task_id,
        base_revision=base_head.revision_id,
        operations=(
            ModelCommand(
                id="visual-adoption-create-design",
                op="create_parametric_design",
                args={"design": proposal.design.to_mapping()},
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=proposal.acceptance,
    )
    return VisualAdoptionRequest(
        reconstruction_id=reconstruction_id,
        adoption_key_sha256=adoption_key_sha256,
        adoption_intent_sha256=adoption_intent_sha256,
        task_create_key=task_create_key,
        task_id=task_id,
        base_head=base_head,
        proposal=proposal,
        program=program,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualAdoptionReceipt:
    """Proof that the exact ordinary Task/program is durably observable."""

    task_id: str
    adoption_intent_sha256: str
    base_head_sha256: str
    program_sha256: str
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or _TASK_ID.fullmatch(self.task_id) is None:
            _fail()
        for name in (
            "adoption_intent_sha256",
            "base_head_sha256",
            "program_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name)))
        body = {
            "task_id": self.task_id,
            "adoption_intent_sha256": self.adoption_intent_sha256,
            "base_head_sha256": self.base_head_sha256,
            "program_sha256": self.program_sha256,
        }
        expected = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical(body)).hexdigest()
        if self.receipt_sha256 and not hmac.compare_digest(_digest(self.receipt_sha256), expected):
            _fail()
        object.__setattr__(self, "receipt_sha256", expected)


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualAdoptionAbsenceReceipt:
    """Proof that the prior ensure attempt is settled and can no longer publish a Task."""

    task_id: str
    adoption_intent_sha256: str
    base_head_sha256: str
    program_sha256: str
    receipt_sha256: str = ""

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or _TASK_ID.fullmatch(self.task_id) is None:
            _fail()
        for name in (
            "adoption_intent_sha256",
            "base_head_sha256",
            "program_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name)))
        body = {
            "task_id": self.task_id,
            "adoption_intent_sha256": self.adoption_intent_sha256,
            "base_head_sha256": self.base_head_sha256,
            "program_sha256": self.program_sha256,
        }
        expected = hashlib.sha256(_ABSENCE_RECEIPT_DIGEST_DOMAIN + _canonical(body)).hexdigest()
        if self.receipt_sha256 and not hmac.compare_digest(_digest(self.receipt_sha256), expected):
            _fail()
        object.__setattr__(self, "receipt_sha256", expected)


def validate_visual_adoption_receipt(
    request: VisualAdoptionRequest,
    receipt: object,
) -> VisualAdoptionReceipt:
    if type(request) is not VisualAdoptionRequest or type(receipt) is not VisualAdoptionReceipt:
        _fail()
    if (
        receipt.task_id != request.task_id
        or receipt.adoption_intent_sha256 != request.adoption_intent_sha256
        or receipt.base_head_sha256 != request.base_head.sha256
        or receipt.program_sha256 != request.program_sha256
    ):
        _fail()
    return receipt


def validate_visual_adoption_absence_receipt(
    request: VisualAdoptionRequest,
    receipt: object,
) -> VisualAdoptionAbsenceReceipt:
    """Validate a trusted, settled-absence proof for the exact adoption intent."""

    if (
        type(request) is not VisualAdoptionRequest
        or type(receipt) is not VisualAdoptionAbsenceReceipt
    ):
        _fail()
    if (
        receipt.task_id != request.task_id
        or receipt.adoption_intent_sha256 != request.adoption_intent_sha256
        or receipt.base_head_sha256 != request.base_head.sha256
        or receipt.program_sha256 != request.program_sha256
    ):
        _fail()
    return receipt


@runtime_checkable
class VisualAdoptionPort(Protocol):
    """Application-owned authority; intentionally separate from the provider."""

    def inspect_head(self, project_id: str) -> BaseHeadBinding: ...

    def ensure_review_task(
        self,
        request: VisualAdoptionRequest,
    ) -> VisualAdoptionReceipt | None: ...

    def reconcile_review_task(
        self,
        request: VisualAdoptionRequest,
    ) -> VisualAdoptionReceipt | VisualAdoptionAbsenceReceipt | None: ...


__all__ = [
    "VisualAdoptionContractError",
    "VisualAdoptionAbsenceReceipt",
    "VisualAdoptionPort",
    "VisualAdoptionReceipt",
    "VisualAdoptionRequest",
    "build_visual_adoption_request",
    "validate_visual_adoption_receipt",
    "validate_visual_adoption_absence_receipt",
    "visual_adoption_program_digest",
]
