"""Strict provider-neutral contracts for visual CAD reconstruction.

This module contains values only.  It does not invoke a model, touch durable
storage, compile CAD, or grant candidate/review/HEAD authority.  A visual
provider may produce an observation and a proposal, but the proposal remains
an inert, evidence-bound value until the application adopts it through the
normal reviewed Task path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from vibecad.parametric.contracts import (
    DesignEvidenceStatus,
    ParametricContractError,
    ParametricDesignIR,
)
from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS, ViewRole
from vibecad.workflow.contracts import AcceptanceSpec
from vibecad.workflow.errors import ContractValidationError, is_canonical_json_pointer

RECONSTRUCTION_SCHEMA_VERSION = 1
MAX_VISUAL_CLAIMS = 128
MAX_CLARIFICATION_QUESTIONS = 128
MAX_CLARIFICATION_ANSWERS = 128
MAX_EVIDENCE_BINDINGS = 128
MAX_ALTERNATIVES = 16
MAX_UNSUPPORTED_ITEMS = 32
MAX_VISUAL_CLAIM_BYTES = 8 * 1024
MAX_CLARIFICATION_RECORD_BYTES = 8 * 1024
MAX_VISUAL_OBSERVATION_BYTES = 256 * 1024
MAX_RECONSTRUCTION_PROPOSAL_BYTES = 768 * 1024

_MAX_TEXT_BYTES = 512
_MAX_ERROR_PATH_BYTES = 512
_MAX_SAFE_INTEGER = 2**53 - 1

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_RECONSTRUCTION_CREATE_KEY = re.compile(r"^reconstruction_create_[0-9a-f]{32}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_CLAIM_ID = re.compile(r"^visual_claim_[0-9a-f]{32}$")
_QUESTION_ID = re.compile(r"^clarification_question_[0-9a-f]{32}$")
_ANSWER_ID = re.compile(r"^clarification_answer_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")
_PROPOSAL_ID = re.compile(r"^reconstruction_proposal_[0-9a-f]{32}$")
_INVOCATION_ID = re.compile(r"^visual_invocation_[0-9a-f]{32}$")
_CLAIM_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_EVIDENCE_ID = re.compile(r"^ir_evidence_[0-9a-f]{32}$")

_RECONSTRUCTION_ID_DOMAIN = b"vibecad-reconstruction-id-v1\0"
_INVOCATION_ID_DOMAIN = b"vibecad-visual-invocation-id-v1\0"
_CLAIM_DIGEST_DOMAIN = b"vibecad-visual-claim-v1\0"
_CLAIM_ID_DOMAIN = b"vibecad-visual-claim-id-v1\0"
_QUESTION_DIGEST_DOMAIN = b"vibecad-clarification-question-v1\0"
_QUESTION_ID_DOMAIN = b"vibecad-clarification-question-id-v1\0"
_ANSWER_DIGEST_DOMAIN = b"vibecad-clarification-answer-v1\0"
_ANSWER_ID_DOMAIN = b"vibecad-clarification-answer-id-v1\0"
_OBSERVATION_DIGEST_DOMAIN = b"vibecad-visual-observation-v1\0"
_OBSERVATION_ID_DOMAIN = b"vibecad-visual-observation-id-v1\0"
_ACCEPTANCE_DIGEST_DOMAIN = b"vibecad-reconstruction-acceptance-v1\0"
_PROPOSAL_DIGEST_DOMAIN = b"vibecad-reconstruction-proposal-v1\0"
_PROPOSAL_ID_DOMAIN = b"vibecad-reconstruction-proposal-id-v1\0"

type ClaimScalar = None | bool | int | float | str


class ReconstructionContractErrorCode(StrEnum):
    """Stable rejection categories for reconstruction value contracts."""

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_REFERENCE = "unknown_reference"
    PROPOSAL_BLOCKED = "proposal_blocked"


class ReconstructionContractError(ValueError):
    """Bounded error that never reflects rejected provider output."""

    def __init__(self, code: ReconstructionContractErrorCode, path: str = "") -> None:
        if type(code) is not ReconstructionContractErrorCode:
            raise TypeError("code must be an exact ReconstructionContractErrorCode")
        if (
            type(path) is not str
            or len(path.encode("utf-8")) > _MAX_ERROR_PATH_BYTES
            or not is_canonical_json_pointer(path)
        ):
            raise ValueError("path must be a bounded canonical JSON Pointer")
        self.code = code
        self.path = path
        super().__init__(code.value)


class VisualClaimStatus(StrEnum):
    CONFIRMED = "confirmed"
    CALIBRATED = "calibrated"
    CROSS_VIEW_DERIVED = "cross_view_derived"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class VisualClaimUnit(StrEnum):
    MM = "mm"
    DEG = "deg"
    COUNT = "count"
    RATIO = "ratio"


class ClarificationKind(StrEnum):
    CONFIRM_ASSUMPTION = "confirm_assumption"
    RESOLVE_UNKNOWN = "resolve_unknown"
    RESOLVE_CONFLICT = "resolve_conflict"


class ReconstructionNextAction(StrEnum):
    RUN = "run"
    WAIT = "wait"
    ANSWER = "answer"
    ADOPT_OR_REJECT = "adopt_or_reject"
    REVIEW_TASK = "review_task"
    NONE = "none"


class ReconstructionStatus(StrEnum):
    READY = "ready"
    OBSERVING = "observing"
    NEEDS_INPUT = "needs_input"
    PROPOSED = "proposed"
    ADOPTING = "adopting"
    ADOPTED = "adopted"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    REJECTED = "rejected"
    DELETED = "deleted"

    @property
    def next_action(self) -> ReconstructionNextAction:
        """Return the only status-derived primary action; it is never persisted."""

        return _NEXT_ACTION[self]


_NEXT_ACTION = {
    ReconstructionStatus.READY: ReconstructionNextAction.RUN,
    ReconstructionStatus.OBSERVING: ReconstructionNextAction.WAIT,
    ReconstructionStatus.NEEDS_INPUT: ReconstructionNextAction.ANSWER,
    ReconstructionStatus.PROPOSED: ReconstructionNextAction.ADOPT_OR_REJECT,
    ReconstructionStatus.ADOPTING: ReconstructionNextAction.WAIT,
    ReconstructionStatus.ADOPTED: ReconstructionNextAction.REVIEW_TASK,
    ReconstructionStatus.FAILED: ReconstructionNextAction.RUN,
    ReconstructionStatus.RECOVERY_REQUIRED: ReconstructionNextAction.RUN,
    ReconstructionStatus.REJECTED: ReconstructionNextAction.NONE,
    ReconstructionStatus.DELETED: ReconstructionNextAction.NONE,
}


def _fail(code: ReconstructionContractErrorCode, path: str = "") -> None:
    raise ReconstructionContractError(code, path)


def _schema(value: object, path: str = "/schema_version") -> int:
    if type(value) is not int or value != RECONSTRUCTION_SCHEMA_VERSION:
        _fail(ReconstructionContractErrorCode.UNSUPPORTED_VERSION, path)
    return value


def _fields(value: object, expected: set[str], path: str = "") -> dict[str, Any]:
    if type(value) is not dict:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    if set(value) != expected or any(type(key) is not str for key in value):
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    return value


def _identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    return _identifier(value, _DIGEST, path)


def _text(value: object, path: str, *, maximum: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    if not encoded or len(encoded) > maximum or value.strip() != value or not value.isprintable():
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    return value


def _enum[EnumT: StrEnum](value: object, enum_type: type[EnumT], path: str) -> EnumT:
    if type(value) is enum_type:
        return value
    if type(value) is not str:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    try:
        return enum_type(value)
    except ValueError:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)


def _tuple[ItemT](
    value: object,
    item_type: type[ItemT],
    path: str,
    *,
    maximum: int,
) -> tuple[ItemT, ...]:
    if type(value) is not tuple or len(value) > maximum:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    if any(type(item) is not item_type for item in value):
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    return value


def _string_tuple(value: object, path: str, *, maximum: int) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > maximum:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    return tuple(_text(item, f"{path}/{index}") for index, item in enumerate(value))


def _scalar(value: object, path: str, *, allow_none: bool) -> ClaimScalar:
    if value is None:
        if allow_none:
            return None
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    if type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
        return value
    if type(value) is str:
        return _text(value, path)
    _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)


def _canonical_json(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _fail(ReconstructionContractErrorCode.INVALID_INPUT)
    if len(raw) > maximum:
        _fail(ReconstructionContractErrorCode.BUDGET_EXCEEDED)
    return raw


def _body_digest(domain: bytes, body: object, *, maximum: int) -> str:
    return hashlib.sha256(domain + _canonical_json(body, maximum=maximum)).hexdigest()


def _derived_identifier(prefix: str, domain: bytes, digest: str) -> str:
    return prefix + hashlib.sha256(domain + bytes.fromhex(digest)).hexdigest()[:32]


def _verify_identity(
    supplied_id: object,
    supplied_digest: object,
    *,
    expected_digest: str,
    id_prefix: str,
    id_domain: bytes,
    id_pattern: re.Pattern[str],
    id_path: str = "/id",
    digest_path: str = "/digest",
) -> tuple[str, str]:
    expected_id = _derived_identifier(id_prefix, id_domain, expected_digest)
    if supplied_id != "":
        _identifier(supplied_id, id_pattern, id_path)
        if supplied_id != expected_id:
            _fail(ReconstructionContractErrorCode.INTEGRITY_FAILURE, id_path)
    if supplied_digest != "":
        _digest(supplied_digest, digest_path)
        if supplied_digest != expected_digest:
            _fail(ReconstructionContractErrorCode.INTEGRITY_FAILURE, digest_path)
    return expected_id, expected_digest


def _wire_list(value: object, path: str, *, maximum: int) -> list[object]:
    if type(value) is not list:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    if len(value) > maximum:
        _fail(ReconstructionContractErrorCode.BUDGET_EXCEEDED, path)
    return value


def _decode(raw: object, *, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or not raw:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT)
    if len(raw) > maximum:
        _fail(ReconstructionContractErrorCode.BUDGET_EXCEEDED)
    try:
        value = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        _fail(ReconstructionContractErrorCode.INVALID_INPUT)
    if type(value) is not dict:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT)
    return value


def reconstruction_identity(create_key: object) -> tuple[str, str]:
    """Derive one retry-stable reconstruction identity from a create key."""

    canonical = _identifier(create_key, _RECONSTRUCTION_CREATE_KEY, "/create_key")
    create_key_digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()
    identifier = _derived_identifier(
        "reconstruction_",
        _RECONSTRUCTION_ID_DOMAIN,
        create_key_digest,
    )
    return identifier, create_key_digest


def visual_invocation_identity(
    reconstruction_id: object,
    generation: object,
    image_set_id: object,
    image_set_manifest_sha256: object,
) -> str:
    """Derive the one provider invocation identity for an observation attempt."""

    canonical_reconstruction = _identifier(
        reconstruction_id,
        _RECONSTRUCTION_ID,
        "/reconstruction_id",
    )
    if type(generation) is not int or not 1 <= generation <= _MAX_SAFE_INTEGER:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/generation")
    canonical_image_set = _identifier(image_set_id, _IMAGE_SET_ID, "/image_set_id")
    canonical_manifest = _digest(image_set_manifest_sha256, "/image_set_manifest_sha256")
    seed = _canonical_json(
        {
            "generation": generation,
            "image_set_id": canonical_image_set,
            "image_set_manifest_sha256": canonical_manifest,
            "reconstruction_id": canonical_reconstruction,
        },
        maximum=4 * 1024,
    )
    return "visual_invocation_" + hashlib.sha256(_INVOCATION_ID_DOMAIN + seed).hexdigest()[:32]


def next_action_for_status(value: object) -> ReconstructionNextAction:
    """Return the deterministic primary action for one strict status value."""

    status = _enum(value, ReconstructionStatus, "/status")
    return status.next_action


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualClaim:
    """One image-derived fact with explicit provenance and uncertainty state."""

    name: str
    status: VisualClaimStatus
    source_indices: tuple[int, ...]
    value: ClaimScalar
    unit: VisualClaimUnit | None = None
    blocking: bool = False
    description: str | None = None
    id: str = ""
    digest: str = ""
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        name = _text(self.name, "/name", maximum=128)
        if _CLAIM_NAME.fullmatch(name) is None:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/name")
        object.__setattr__(self, "name", name)
        status = _enum(self.status, VisualClaimStatus, "/status")
        object.__setattr__(self, "status", status)
        if type(self.source_indices) is not tuple or not self.source_indices:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/source_indices")
        if (
            len(self.source_indices) > MAX_IMAGE_SET_ITEMS
            or any(
                type(index) is not int or not 0 <= index < MAX_IMAGE_SET_ITEMS
                for index in self.source_indices
            )
            or len(set(self.source_indices)) != len(self.source_indices)
        ):
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/source_indices")
        if status is VisualClaimStatus.CROSS_VIEW_DERIVED and len(self.source_indices) < 2:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/source_indices")
        object.__setattr__(self, "source_indices", tuple(sorted(self.source_indices)))
        allow_none = status in {VisualClaimStatus.UNKNOWN, VisualClaimStatus.CONFLICT}
        value = _scalar(self.value, "/value", allow_none=allow_none)
        if allow_none and value is not None:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/value")
        object.__setattr__(self, "value", value)
        if self.unit is not None:
            object.__setattr__(self, "unit", _enum(self.unit, VisualClaimUnit, "/unit"))
        if type(self.blocking) is not bool:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/blocking")
        if status not in {VisualClaimStatus.UNKNOWN, VisualClaimStatus.CONFLICT} and self.blocking:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/blocking")
        if self.description is not None:
            object.__setattr__(self, "description", _text(self.description, "/description"))
        expected_digest = _body_digest(
            _CLAIM_DIGEST_DOMAIN,
            self._body_mapping(),
            maximum=MAX_VISUAL_CLAIM_BYTES,
        )
        identifier, digest = _verify_identity(
            self.id,
            self.digest,
            expected_digest=expected_digest,
            id_prefix="visual_claim_",
            id_domain=_CLAIM_ID_DOMAIN,
            id_pattern=_CLAIM_ID,
        )
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "digest", digest)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "status": self.status.value,
            "source_indices": list(self.source_indices),
            "value": self.value,
            "unit": None if self.unit is None else self.unit.value,
            "blocking": self.blocking,
            "description": self.description,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"id": self.id, "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            {
                "schema_version",
                "id",
                "digest",
                "name",
                "status",
                "source_indices",
                "value",
                "unit",
                "blocking",
                "description",
            },
        )
        indices = _wire_list(data["source_indices"], "/source_indices", maximum=MAX_IMAGE_SET_ITEMS)
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            digest=data["digest"],
            name=data["name"],
            status=data["status"],
            source_indices=tuple(indices),
            value=data["value"],
            unit=data["unit"],
            blocking=data["blocking"],
            description=data["description"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClarificationQuestion:
    """One immutable question tied to the claim it can clarify."""

    claim_id: str
    kind: ClarificationKind
    prompt: str
    id: str = ""
    digest: str = ""
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "claim_id", _identifier(self.claim_id, _CLAIM_ID, "/claim_id"))
        object.__setattr__(self, "kind", _enum(self.kind, ClarificationKind, "/kind"))
        object.__setattr__(self, "prompt", _text(self.prompt, "/prompt"))
        expected_digest = _body_digest(
            _QUESTION_DIGEST_DOMAIN,
            self._body_mapping(),
            maximum=MAX_CLARIFICATION_RECORD_BYTES,
        )
        identifier, digest = _verify_identity(
            self.id,
            self.digest,
            expected_digest=expected_digest,
            id_prefix="clarification_question_",
            id_domain=_QUESTION_ID_DOMAIN,
            id_pattern=_QUESTION_ID,
        )
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "digest", digest)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "kind": self.kind.value,
            "prompt": self.prompt,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"id": self.id, "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            {"schema_version", "id", "digest", "claim_id", "kind", "prompt"},
        )
        return cls(**data)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClarificationAnswer:
    """One immutable user answer; ``True`` explicitly confirms an assumption."""

    question_id: str
    claim_id: str
    response: bool | int | float | str
    id: str = ""
    digest: str = ""
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self,
            "question_id",
            _identifier(self.question_id, _QUESTION_ID, "/question_id"),
        )
        object.__setattr__(self, "claim_id", _identifier(self.claim_id, _CLAIM_ID, "/claim_id"))
        response = _scalar(self.response, "/response", allow_none=False)
        assert response is not None
        object.__setattr__(self, "response", response)
        expected_digest = _body_digest(
            _ANSWER_DIGEST_DOMAIN,
            self._body_mapping(),
            maximum=MAX_CLARIFICATION_RECORD_BYTES,
        )
        identifier, digest = _verify_identity(
            self.id,
            self.digest,
            expected_digest=expected_digest,
            id_prefix="clarification_answer_",
            id_domain=_ANSWER_ID_DOMAIN,
            id_pattern=_ANSWER_ID,
        )
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "digest", digest)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "question_id": self.question_id,
            "claim_id": self.claim_id,
            "response": self.response,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"id": self.id, "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            {"schema_version", "id", "digest", "question_id", "claim_id", "response"},
        )
        return cls(**data)


def clarification_question_for_claim(claim: VisualClaim, prompt: str) -> ClarificationQuestion:
    """Build the status-appropriate clarification question for one exact claim."""

    if type(claim) is not VisualClaim:
        raise TypeError("claim must be an exact VisualClaim")
    kinds = {
        VisualClaimStatus.ASSUMED: ClarificationKind.CONFIRM_ASSUMPTION,
        VisualClaimStatus.UNKNOWN: ClarificationKind.RESOLVE_UNKNOWN,
        VisualClaimStatus.CONFLICT: ClarificationKind.RESOLVE_CONFLICT,
    }
    kind = kinds.get(claim.status)
    if kind is None:
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/claim/status")
    return ClarificationQuestion(claim_id=claim.id, kind=kind, prompt=prompt)


def clarification_answer_for_question(
    question: ClarificationQuestion,
    response: bool | int | float | str,
) -> ClarificationAnswer:
    """Build an immutable answer bound to one exact question and claim."""

    if type(question) is not ClarificationQuestion:
        raise TypeError("question must be an exact ClarificationQuestion")
    return ClarificationAnswer(
        question_id=question.id,
        claim_id=question.claim_id,
        response=response,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualObservation:
    """One invocation result bound to an exact sealed ImageSet manifest."""

    reconstruction_id: str
    generation: int
    image_set_id: str
    image_set_manifest_sha256: str
    invocation_id: str
    claims: tuple[VisualClaim, ...]
    questions: tuple[ClarificationQuestion, ...] = ()
    id: str = ""
    digest: str = ""
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self,
            "reconstruction_id",
            _identifier(self.reconstruction_id, _RECONSTRUCTION_ID, "/reconstruction_id"),
        )
        if type(self.generation) is not int or not 1 <= self.generation <= _MAX_SAFE_INTEGER:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/generation")
        object.__setattr__(
            self,
            "image_set_id",
            _identifier(self.image_set_id, _IMAGE_SET_ID, "/image_set_id"),
        )
        object.__setattr__(
            self,
            "image_set_manifest_sha256",
            _digest(self.image_set_manifest_sha256, "/image_set_manifest_sha256"),
        )
        expected_invocation = visual_invocation_identity(
            self.reconstruction_id,
            self.generation,
            self.image_set_id,
            self.image_set_manifest_sha256,
        )
        _identifier(self.invocation_id, _INVOCATION_ID, "/invocation_id")
        if self.invocation_id != expected_invocation:
            _fail(ReconstructionContractErrorCode.INTEGRITY_FAILURE, "/invocation_id")
        claims = _tuple(
            self.claims,
            VisualClaim,
            "/claims",
            maximum=MAX_VISUAL_CLAIMS,
        )
        if not claims:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/claims")
        if len({item.id for item in claims}) != len(claims):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/claims")
        if len({item.name for item in claims}) != len(claims):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/claims")
        claims = tuple(sorted(claims, key=lambda item: item.id))
        object.__setattr__(self, "claims", claims)
        questions = _tuple(
            self.questions,
            ClarificationQuestion,
            "/questions",
            maximum=MAX_CLARIFICATION_QUESTIONS,
        )
        if len({item.id for item in questions}) != len(questions):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/questions")
        if len({item.claim_id for item in questions}) != len(questions):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/questions")
        claim_by_id = {item.id: item for item in claims}
        expected_kind = {
            VisualClaimStatus.ASSUMED: ClarificationKind.CONFIRM_ASSUMPTION,
            VisualClaimStatus.UNKNOWN: ClarificationKind.RESOLVE_UNKNOWN,
            VisualClaimStatus.CONFLICT: ClarificationKind.RESOLVE_CONFLICT,
        }
        for index, question in enumerate(questions):
            claim = claim_by_id.get(question.claim_id)
            if claim is None:
                _fail(
                    ReconstructionContractErrorCode.UNKNOWN_REFERENCE,
                    f"/questions/{index}/claim_id",
                )
            if expected_kind.get(claim.status) is not question.kind:
                _fail(ReconstructionContractErrorCode.INVALID_INPUT, f"/questions/{index}/kind")
        question_claim_ids = {item.claim_id for item in questions}
        for index, claim in enumerate(claims):
            needs_question = claim.status is VisualClaimStatus.ASSUMED or (
                claim.blocking
                and claim.status in {VisualClaimStatus.UNKNOWN, VisualClaimStatus.CONFLICT}
            )
            if needs_question and claim.id not in question_claim_ids:
                _fail(ReconstructionContractErrorCode.INVALID_INPUT, f"/claims/{index}")
        questions = tuple(sorted(questions, key=lambda item: item.id))
        object.__setattr__(self, "questions", questions)
        expected_digest = _body_digest(
            _OBSERVATION_DIGEST_DOMAIN,
            self._body_mapping(),
            maximum=MAX_VISUAL_OBSERVATION_BYTES,
        )
        identifier, digest = _verify_identity(
            self.id,
            self.digest,
            expected_digest=expected_digest,
            id_prefix="visual_observation_",
            id_domain=_OBSERVATION_ID_DOMAIN,
            id_pattern=_OBSERVATION_ID,
        )
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "digest", digest)

    @property
    def proposal_blockers(self) -> tuple[str, ...]:
        """Return sorted blocking unknown/conflict claim IDs."""

        return tuple(
            sorted(
                claim.id
                for claim in self.claims
                if claim.blocking
                and claim.status in {VisualClaimStatus.UNKNOWN, VisualClaimStatus.CONFLICT}
            )
        )

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reconstruction_id": self.reconstruction_id,
            "generation": self.generation,
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "invocation_id": self.invocation_id,
            "claims": [item.to_mapping() for item in self.claims],
            "questions": [item.to_mapping() for item in self.questions],
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"id": self.id, "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            {
                "schema_version",
                "id",
                "digest",
                "reconstruction_id",
                "generation",
                "image_set_id",
                "image_set_manifest_sha256",
                "invocation_id",
                "claims",
                "questions",
            },
        )
        claims = _wire_list(data["claims"], "/claims", maximum=MAX_VISUAL_CLAIMS)
        questions = _wire_list(
            data["questions"],
            "/questions",
            maximum=MAX_CLARIFICATION_QUESTIONS,
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            digest=data["digest"],
            reconstruction_id=data["reconstruction_id"],
            generation=data["generation"],
            image_set_id=data["image_set_id"],
            image_set_manifest_sha256=data["image_set_manifest_sha256"],
            invocation_id=data["invocation_id"],
            claims=tuple(VisualClaim.from_mapping(item) for item in claims),
            questions=tuple(ClarificationQuestion.from_mapping(item) for item in questions),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class EvidenceBinding:
    """Complete binding from one IR evidence record to visual claims."""

    evidence_id: str
    claim_ids: tuple[str, ...]
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self,
            "evidence_id",
            _identifier(self.evidence_id, _EVIDENCE_ID, "/evidence_id"),
        )
        if type(self.claim_ids) is not tuple or not self.claim_ids or len(self.claim_ids) > 8:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/claim_ids")
        claim_ids = tuple(
            _identifier(item, _CLAIM_ID, f"/claim_ids/{index}")
            for index, item in enumerate(self.claim_ids)
        )
        if len(set(claim_ids)) != len(claim_ids):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/claim_ids")
        object.__setattr__(self, "claim_ids", tuple(sorted(claim_ids)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "claim_ids": list(self.claim_ids),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(value, {"schema_version", "evidence_id", "claim_ids"})
        claim_ids = _wire_list(data["claim_ids"], "/claim_ids", maximum=8)
        return cls(
            schema_version=data["schema_version"],
            evidence_id=data["evidence_id"],
            claim_ids=tuple(claim_ids),
        )


def _acceptance_digest(acceptance: AcceptanceSpec) -> str:
    return _body_digest(
        _ACCEPTANCE_DIGEST_DOMAIN,
        acceptance.to_mapping(),
        maximum=256 * 1024,
    )


def _nested_design(value: object) -> ParametricDesignIR:
    try:
        return ParametricDesignIR.from_mapping(value)
    except ParametricContractError as error:
        path = "/design" + error.path
        if len(path.encode("utf-8")) > _MAX_ERROR_PATH_BYTES:
            path = "/design"
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    except (TypeError, ValueError):
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/design")


def _nested_acceptance(value: object) -> AcceptanceSpec:
    try:
        return AcceptanceSpec.from_mapping(value)
    except ContractValidationError as error:
        path = "/acceptance" + error.path
        if len(path.encode("utf-8")) > _MAX_ERROR_PATH_BYTES:
            path = "/acceptance"
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, path)
    except (TypeError, ValueError):
        _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/acceptance")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconstructionProposal:
    """An inert, evidence-complete visual reconstruction proposal.

    It intentionally embeds no ``ModelProgram``.  Adoption may later translate
    the exact design and acceptance values into the ordinary reviewed Task path.
    """

    observation: VisualObservation
    design: ParametricDesignIR
    acceptance: AcceptanceSpec
    evidence_bindings: tuple[EvidenceBinding, ...]
    clarification_answers: tuple[ClarificationAnswer, ...]
    part_type: str
    summary: str
    alternatives: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    expected_views: tuple[ViewRole, ...] = ()
    design_digest: str = ""
    acceptance_digest: str = ""
    id: str = ""
    digest: str = ""
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        if type(self.observation) is not VisualObservation:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/observation")
        if self.observation.proposal_blockers:
            _fail(ReconstructionContractErrorCode.PROPOSAL_BLOCKED, "/observation/claims")
        if type(self.design) is not ParametricDesignIR:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/design")
        if type(self.acceptance) is not AcceptanceSpec:
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/acceptance")
        expected_design_digest = self.design.digest
        if self.design_digest != "":
            _digest(self.design_digest, "/design_digest")
            if self.design_digest != expected_design_digest:
                _fail(ReconstructionContractErrorCode.INTEGRITY_FAILURE, "/design_digest")
        object.__setattr__(self, "design_digest", expected_design_digest)
        expected_acceptance_digest = _acceptance_digest(self.acceptance)
        if self.acceptance_digest != "":
            _digest(self.acceptance_digest, "/acceptance_digest")
            if self.acceptance_digest != expected_acceptance_digest:
                _fail(ReconstructionContractErrorCode.INTEGRITY_FAILURE, "/acceptance_digest")
        object.__setattr__(self, "acceptance_digest", expected_acceptance_digest)

        answers = _tuple(
            self.clarification_answers,
            ClarificationAnswer,
            "/clarification_answers",
            maximum=MAX_CLARIFICATION_ANSWERS,
        )
        if len({item.id for item in answers}) != len(answers) or len(
            {item.question_id for item in answers}
        ) != len(answers):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/clarification_answers")
        question_by_id = {item.id: item for item in self.observation.questions}
        answer_by_question: dict[str, ClarificationAnswer] = {}
        for index, answer in enumerate(answers):
            question = question_by_id.get(answer.question_id)
            if question is None or question.claim_id != answer.claim_id:
                _fail(
                    ReconstructionContractErrorCode.UNKNOWN_REFERENCE,
                    f"/clarification_answers/{index}/question_id",
                )
            answer_by_question[answer.question_id] = answer
        question_by_claim = {item.claim_id: item for item in self.observation.questions}
        for index, claim in enumerate(self.observation.claims):
            if claim.status is not VisualClaimStatus.ASSUMED:
                continue
            question = question_by_claim.get(claim.id)
            answer = None if question is None else answer_by_question.get(question.id)
            if (
                question is None
                or question.kind is not ClarificationKind.CONFIRM_ASSUMPTION
                or answer is None
                or answer.response is not True
            ):
                _fail(
                    ReconstructionContractErrorCode.PROPOSAL_BLOCKED, f"/observation/claims/{index}"
                )
        answers = tuple(sorted(answers, key=lambda item: item.id))
        object.__setattr__(self, "clarification_answers", answers)

        bindings = _tuple(
            self.evidence_bindings,
            EvidenceBinding,
            "/evidence_bindings",
            maximum=MAX_EVIDENCE_BINDINGS,
        )
        if len({item.evidence_id for item in bindings}) != len(bindings):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/evidence_bindings")
        evidence_by_id = {item.id: item for item in self.design.evidence}
        binding_by_id = {item.evidence_id: item for item in bindings}
        if set(evidence_by_id) != set(binding_by_id):
            _fail(ReconstructionContractErrorCode.UNKNOWN_REFERENCE, "/evidence_bindings")
        claim_by_id = {item.id: item for item in self.observation.claims}
        projected_status = {
            VisualClaimStatus.CONFIRMED: DesignEvidenceStatus.CONFIRMED,
            VisualClaimStatus.CALIBRATED: DesignEvidenceStatus.CALIBRATED,
            VisualClaimStatus.CROSS_VIEW_DERIVED: DesignEvidenceStatus.CROSS_VIEW_DERIVED,
            VisualClaimStatus.ASSUMED: DesignEvidenceStatus.CONFIRMED,
        }
        for binding_index, binding in enumerate(bindings):
            evidence = evidence_by_id[binding.evidence_id]
            if tuple(evidence.source_refs) != binding.claim_ids:
                _fail(
                    ReconstructionContractErrorCode.INTEGRITY_FAILURE,
                    f"/evidence_bindings/{binding_index}/claim_ids",
                )
            for claim_index, claim_id in enumerate(binding.claim_ids):
                claim = claim_by_id.get(claim_id)
                if claim is None:
                    _fail(
                        ReconstructionContractErrorCode.UNKNOWN_REFERENCE,
                        f"/evidence_bindings/{binding_index}/claim_ids/{claim_index}",
                    )
                expected_status = projected_status.get(claim.status)
                if expected_status is None or evidence.status is not expected_status:
                    _fail(
                        ReconstructionContractErrorCode.INTEGRITY_FAILURE,
                        f"/evidence_bindings/{binding_index}/claim_ids/{claim_index}",
                    )
        bindings = tuple(sorted(bindings, key=lambda item: item.evidence_id))
        object.__setattr__(self, "evidence_bindings", bindings)

        object.__setattr__(self, "part_type", _text(self.part_type, "/part_type", maximum=128))
        object.__setattr__(self, "summary", _text(self.summary, "/summary", maximum=2 * 1024))
        alternatives = _string_tuple(
            self.alternatives,
            "/alternatives",
            maximum=MAX_ALTERNATIVES,
        )
        unsupported = _string_tuple(
            self.unsupported,
            "/unsupported",
            maximum=MAX_UNSUPPORTED_ITEMS,
        )
        if len(set(alternatives)) != len(alternatives):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/alternatives")
        if len(set(unsupported)) != len(unsupported):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/unsupported")
        object.__setattr__(self, "alternatives", tuple(sorted(alternatives)))
        object.__setattr__(self, "unsupported", tuple(sorted(unsupported)))
        if type(self.expected_views) is not tuple or len(self.expected_views) > len(ViewRole):
            _fail(ReconstructionContractErrorCode.INVALID_INPUT, "/expected_views")
        views = tuple(
            _enum(item, ViewRole, f"/expected_views/{index}")
            for index, item in enumerate(self.expected_views)
        )
        if len(set(views)) != len(views):
            _fail(ReconstructionContractErrorCode.DUPLICATE_ID, "/expected_views")
        object.__setattr__(
            self, "expected_views", tuple(sorted(views, key=lambda item: item.value))
        )

        expected_digest = _body_digest(
            _PROPOSAL_DIGEST_DOMAIN,
            self._body_mapping(),
            maximum=MAX_RECONSTRUCTION_PROPOSAL_BYTES,
        )
        identifier, digest = _verify_identity(
            self.id,
            self.digest,
            expected_digest=expected_digest,
            id_prefix="reconstruction_proposal_",
            id_domain=_PROPOSAL_ID_DOMAIN,
            id_pattern=_PROPOSAL_ID,
        )
        object.__setattr__(self, "id", identifier)
        object.__setattr__(self, "digest", digest)

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observation": self.observation.to_mapping(),
            "design": self.design.to_mapping(),
            "design_digest": self.design_digest,
            "acceptance": self.acceptance.to_mapping(),
            "acceptance_digest": self.acceptance_digest,
            "evidence_bindings": [item.to_mapping() for item in self.evidence_bindings],
            "clarification_answers": [item.to_mapping() for item in self.clarification_answers],
            "part_type": self.part_type,
            "summary": self.summary,
            "alternatives": list(self.alternatives),
            "unsupported": list(self.unsupported),
            "expected_views": [item.value for item in self.expected_views],
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"id": self.id, "digest": self.digest}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _fields(
            value,
            {
                "schema_version",
                "id",
                "digest",
                "observation",
                "design",
                "design_digest",
                "acceptance",
                "acceptance_digest",
                "evidence_bindings",
                "clarification_answers",
                "part_type",
                "summary",
                "alternatives",
                "unsupported",
                "expected_views",
            },
        )
        bindings = _wire_list(
            data["evidence_bindings"],
            "/evidence_bindings",
            maximum=MAX_EVIDENCE_BINDINGS,
        )
        answers = _wire_list(
            data["clarification_answers"],
            "/clarification_answers",
            maximum=MAX_CLARIFICATION_ANSWERS,
        )
        alternatives = _wire_list(data["alternatives"], "/alternatives", maximum=MAX_ALTERNATIVES)
        unsupported = _wire_list(
            data["unsupported"],
            "/unsupported",
            maximum=MAX_UNSUPPORTED_ITEMS,
        )
        expected_views = _wire_list(
            data["expected_views"],
            "/expected_views",
            maximum=len(ViewRole),
        )
        return cls(
            schema_version=data["schema_version"],
            id=data["id"],
            digest=data["digest"],
            observation=VisualObservation.from_mapping(data["observation"]),
            design=_nested_design(data["design"]),
            design_digest=data["design_digest"],
            acceptance=_nested_acceptance(data["acceptance"]),
            acceptance_digest=data["acceptance_digest"],
            evidence_bindings=tuple(EvidenceBinding.from_mapping(item) for item in bindings),
            clarification_answers=tuple(ClarificationAnswer.from_mapping(item) for item in answers),
            part_type=data["part_type"],
            summary=data["summary"],
            alternatives=tuple(alternatives),
            unsupported=tuple(unsupported),
            expected_views=tuple(expected_views),
        )


def encode_visual_observation(value: VisualObservation) -> bytes:
    if type(value) is not VisualObservation:
        raise TypeError("value must be an exact VisualObservation")
    return _canonical_json(value.to_mapping(), maximum=MAX_VISUAL_OBSERVATION_BYTES)


def decode_visual_observation(raw: object) -> VisualObservation:
    value = _decode(raw, maximum=MAX_VISUAL_OBSERVATION_BYTES)
    result = VisualObservation.from_mapping(value)
    if encode_visual_observation(result) != raw:
        _fail(ReconstructionContractErrorCode.INTEGRITY_FAILURE)
    return result


def encode_reconstruction_proposal(value: ReconstructionProposal) -> bytes:
    if type(value) is not ReconstructionProposal:
        raise TypeError("value must be an exact ReconstructionProposal")
    return _canonical_json(value.to_mapping(), maximum=MAX_RECONSTRUCTION_PROPOSAL_BYTES)


def decode_reconstruction_proposal(raw: object) -> ReconstructionProposal:
    value = _decode(raw, maximum=MAX_RECONSTRUCTION_PROPOSAL_BYTES)
    result = ReconstructionProposal.from_mapping(value)
    if encode_reconstruction_proposal(result) != raw:
        _fail(ReconstructionContractErrorCode.INTEGRITY_FAILURE)
    return result
