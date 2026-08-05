"""Strict durable values for the visual reconstruction lifecycle.

The values in this module contain no filesystem, runtime, Task, review, or
HEAD mutation authority.  They are the checksummed record and immutable
payload contracts consumed by the reconstruction draft store.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from vibecad.runtime.contracts import RuntimeBudget, RuntimeIdentity, RuntimeLifecycleState
from vibecad.visual.contracts import MAX_IMAGE_SET_ITEMS
from vibecad.visual.reconstruction import (
    MAX_CLARIFICATION_RECORD_BYTES,
    MAX_RECONSTRUCTION_PROPOSAL_BYTES,
    MAX_VISUAL_OBSERVATION_BYTES,
    RECONSTRUCTION_SCHEMA_VERSION,
    ClarificationAnswer,
    ReconstructionProposal,
    ReconstructionStatus,
    VisualObservation,
    decode_reconstruction_proposal,
    decode_visual_observation,
    encode_reconstruction_proposal,
    encode_visual_observation,
    visual_invocation_identity,
)
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.state import task_creation_identity

MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES = 256 * 1024
MAX_RECONSTRUCTION_DRAFTS = 1024
MAX_RECONSTRUCTION_DRAFT_STORE_BYTES = 2 * 1024 * 1024 * 1024
MAX_RECONSTRUCTION_PROVIDER_INVOCATIONS = 16
MAX_RECONSTRUCTION_DRAFT_MUTATIONS = 1
MAX_RECONSTRUCTION_CLARIFICATIONS = 128
MAX_RECONSTRUCTION_SOURCE_DIGESTS = MAX_IMAGE_SET_ITEMS

_MAX_TEXT_BYTES = 256
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 8192
_MAX_JSON_STRING_BYTES = 64 * 1024

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_REVISION_ID = re.compile(r"^revision_[0-9a-f]{32}$")
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$")
_RECONSTRUCTION_ID = re.compile(r"^reconstruction_[0-9a-f]{32}$")
_IMAGE_SET_ID = re.compile(r"^image_set_[0-9a-f]{32}$")
_OBSERVATION_ID = re.compile(r"^visual_observation_[0-9a-f]{32}$")
_PROPOSAL_ID = re.compile(r"^reconstruction_proposal_[0-9a-f]{32}$")
_ANSWER_ID = re.compile(r"^clarification_answer_[0-9a-f]{32}$")
_INVOCATION_ID = re.compile(r"^visual_invocation_[0-9a-f]{32}$")
_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")

_RECONSTRUCTION_ID_DOMAIN = b"vibecad-reconstruction-id-v1\0"
_BASE_HEAD_DOMAIN = b"vibecad-reconstruction-base-head-v1\0"
_PROVIDER_INTENT_DOMAIN = b"vibecad-reconstruction-provider-intent-v1\0"
_DRAFT_CHECKSUM_DOMAIN = b"vibecad-reconstruction-draft-record-v1\0"
_ADOPTION_KEY_DOMAIN = b"vibecad-reconstruction-adoption-key-v1\0"
_ADOPTION_INTENT_DOMAIN = b"vibecad-reconstruction-adoption-intent-v1\0"


class ReconstructionDraftErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_VERSION = "unsupported_version"
    INTEGRITY_FAILURE = "integrity_failure"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_TRANSITION = "invalid_transition"


class ReconstructionDraftError(ValueError):
    """Bounded record error that never reflects rejected persisted text."""

    def __init__(self, code: ReconstructionDraftErrorCode) -> None:
        if type(code) is not ReconstructionDraftErrorCode:
            raise TypeError("code must be an exact ReconstructionDraftErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: ReconstructionDraftErrorCode) -> None:
    raise ReconstructionDraftError(code)


def _schema(value: object) -> int:
    if type(value) is not int or value != RECONSTRUCTION_SCHEMA_VERSION:
        _fail(ReconstructionDraftErrorCode.UNSUPPORTED_VERSION)
    return value


def _safe_integer(value: object, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= MAX_SAFE_JSON_INTEGER:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    return value


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    return value


def _digest(value: object) -> str:
    return _identifier(value, _DIGEST)


def _optional_digest(value: object) -> str | None:
    return None if value is None else _digest(value)


def _text(value: object, *, pattern: re.Pattern[str] | None = None) -> str:
    if type(value) is not str or not value or value.strip() != value or not value.isprintable():
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    if size > _MAX_TEXT_BYTES or (pattern is not None and pattern.fullmatch(value) is None):
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    return value


def _exact(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    return value


def _enum[EnumT: StrEnum](value: object, enum_type: type[EnumT]) -> EnumT:
    if type(value) is enum_type:
        return value
    if type(value) is not str:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    try:
        return enum_type(value)
    except ValueError:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)


def _canonical_json(value: object, *, maximum: int) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    if len(raw) > maximum:
        _fail(ReconstructionDraftErrorCode.BUDGET_EXCEEDED)
    return raw


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _parse_integer(raw: str) -> int:
    value = int(raw)
    if abs(value) > MAX_SAFE_JSON_INTEGER:
        raise ValueError
    return value


def _parse_float(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or abs(value) > MAX_SAFE_JSON_INTEGER:
        raise ValueError
    return value


def _json_depth_is_safe(raw: bytes) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                in_string = False
            continue
        if byte == 34:
            in_string = True
        elif byte in (91, 123):
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                return False
        elif byte in (93, 125):
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string and not escaped


def _validate_json_resources(value: object) -> None:
    pending = [value]
    nodes = 0
    while pending:
        selected = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            _fail(ReconstructionDraftErrorCode.BUDGET_EXCEEDED)
        if type(selected) is str:
            try:
                encoded = selected.encode("utf-8")
            except UnicodeError:
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
            if len(encoded) > _MAX_JSON_STRING_BYTES:
                _fail(ReconstructionDraftErrorCode.BUDGET_EXCEEDED)
        elif type(selected) is list:
            pending.extend(selected)
        elif type(selected) is dict:
            pending.extend(selected.keys())
            pending.extend(selected.values())
        elif selected is None or type(selected) in {bool, int, float}:
            continue
        else:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)


def _decode_json(raw: object, *, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or not raw:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    if len(raw) > maximum:
        _fail(ReconstructionDraftErrorCode.BUDGET_EXCEEDED)
    if not _json_depth_is_safe(raw):
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_duplicate_checked_object,
            parse_float=_parse_float,
            parse_int=_parse_integer,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    _validate_json_resources(value)
    if type(value) is not dict or _canonical_json(value, maximum=maximum) != raw:
        _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class BaseHeadBinding:
    project_id: str
    generation: int
    revision_id: str
    manifest_sha256: str
    sha256: str = ""
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "project_id", _identifier(self.project_id, _PROJECT_ID))
        object.__setattr__(self, "generation", _safe_integer(self.generation))
        object.__setattr__(self, "revision_id", _identifier(self.revision_id, _REVISION_ID))
        object.__setattr__(self, "manifest_sha256", _digest(self.manifest_sha256))
        expected = hashlib.sha256(
            _BASE_HEAD_DOMAIN + _canonical_json(self._body(), maximum=4096)
        ).hexdigest()
        if self.sha256 and not hmac.compare_digest(_digest(self.sha256), expected):
            _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
        object.__setattr__(self, "sha256", expected)

    def _body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "generation": self.generation,
            "revision_id": self.revision_id,
            "manifest_sha256": self.manifest_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body() | {"sha256": self.sha256}

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact(
            value,
            {
                "schema_version",
                "project_id",
                "generation",
                "revision_id",
                "manifest_sha256",
                "sha256",
            },
        )
        return cls(**data)


def derive_adoption_identity(
    reconstruction_id: object,
    proposal_digest: object,
    base_head_sha256: object,
) -> tuple[str, str]:
    """Derive the immutable adoption key and effect-intent digest."""

    body = {
        "schema_version": RECONSTRUCTION_SCHEMA_VERSION,
        "reconstruction_id": _identifier(reconstruction_id, _RECONSTRUCTION_ID),
        "proposal_digest": _digest(proposal_digest),
        "base_head_sha256": _digest(base_head_sha256),
    }
    adoption_key = hashlib.sha256(
        _ADOPTION_KEY_DOMAIN + _canonical_json(body, maximum=4096)
    ).hexdigest()
    adoption_intent = hashlib.sha256(
        _ADOPTION_INTENT_DOMAIN
        + _canonical_json(
            body | {"adoption_key_sha256": adoption_key},
            maximum=4096,
        )
    ).hexdigest()
    return adoption_key, adoption_intent


def derive_adoption_task_identity(adoption_key_sha256: object) -> tuple[str, str]:
    """Derive the one ordinary Task create key and ID for an adoption intent."""

    adoption_key = _digest(adoption_key_sha256)
    create_key = f"task_create_{adoption_key[:32]}"
    try:
        task_id, _creation_digest = task_creation_identity(create_key)
    except (TypeError, ValueError):  # pragma: no cover - derived key is canonical.
        _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
    return create_key, task_id


class ReconstructionPayloadKind(StrEnum):
    OBSERVATION = "observation"
    PROPOSAL = "proposal"
    CLARIFICATION = "clarification"


_PAYLOAD_PATTERNS = {
    ReconstructionPayloadKind.OBSERVATION: _OBSERVATION_ID,
    ReconstructionPayloadKind.PROPOSAL: _PROPOSAL_ID,
    ReconstructionPayloadKind.CLARIFICATION: _ANSWER_ID,
}
_PAYLOAD_MAXIMUMS = {
    ReconstructionPayloadKind.OBSERVATION: MAX_VISUAL_OBSERVATION_BYTES,
    ReconstructionPayloadKind.PROPOSAL: MAX_RECONSTRUCTION_PROPOSAL_BYTES,
    ReconstructionPayloadKind.CLARIFICATION: MAX_CLARIFICATION_RECORD_BYTES,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconstructionPayloadRef:
    kind: ReconstructionPayloadKind
    id: str
    contract_digest: str
    sha256: str
    size_bytes: int
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        kind = _enum(self.kind, ReconstructionPayloadKind)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "id", _identifier(self.id, _PAYLOAD_PATTERNS[kind]))
        object.__setattr__(self, "contract_digest", _digest(self.contract_digest))
        object.__setattr__(self, "sha256", _digest(self.sha256))
        size = _safe_integer(self.size_bytes, positive=True)
        if size > _PAYLOAD_MAXIMUMS[kind]:
            _fail(ReconstructionDraftErrorCode.BUDGET_EXCEEDED)
        object.__setattr__(self, "size_bytes", size)

    @property
    def filename(self) -> str:
        return self.id + ".json"

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "id": self.id,
            "contract_digest": self.contract_digest,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact(
            value,
            {"schema_version", "kind", "id", "contract_digest", "sha256", "size_bytes"},
        )
        return cls(**data)


def encode_clarification_answer(value: ClarificationAnswer) -> bytes:
    if type(value) is not ClarificationAnswer:
        raise TypeError("value must be an exact ClarificationAnswer")
    return _canonical_json(value.to_mapping(), maximum=MAX_CLARIFICATION_RECORD_BYTES)


def decode_clarification_answer(raw: object) -> ClarificationAnswer:
    mapping = _decode_json(raw, maximum=MAX_CLARIFICATION_RECORD_BYTES)
    try:
        result = ClarificationAnswer.from_mapping(mapping)
    except (TypeError, ValueError):
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    if encode_clarification_answer(result) != raw:
        _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconstructionPayload:
    ref: ReconstructionPayloadRef
    raw: bytes

    def __post_init__(self) -> None:
        if type(self.ref) is not ReconstructionPayloadRef or type(self.raw) is not bytes:
            raise TypeError("payload fields must be exact durable values")
        if len(self.raw) != self.ref.size_bytes:
            _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
        actual_sha256 = hashlib.sha256(self.raw).hexdigest()
        if not hmac.compare_digest(actual_sha256, self.ref.sha256):
            _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
        if self.ref.kind is ReconstructionPayloadKind.OBSERVATION:
            try:
                value: VisualObservation | ReconstructionProposal | ClarificationAnswer = (
                    decode_visual_observation(self.raw)
                )
            except (TypeError, ValueError):
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        elif self.ref.kind is ReconstructionPayloadKind.PROPOSAL:
            try:
                value = decode_reconstruction_proposal(self.raw)
            except (TypeError, ValueError):
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        else:
            value = decode_clarification_answer(self.raw)
        if value.id != self.ref.id or value.digest != self.ref.contract_digest:
            _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)


def reconstruction_payload(
    value: VisualObservation | ReconstructionProposal | ClarificationAnswer,
) -> ReconstructionPayload:
    if type(value) is VisualObservation:
        kind = ReconstructionPayloadKind.OBSERVATION
        raw = encode_visual_observation(value)
    elif type(value) is ReconstructionProposal:
        kind = ReconstructionPayloadKind.PROPOSAL
        raw = encode_reconstruction_proposal(value)
    elif type(value) is ClarificationAnswer:
        kind = ReconstructionPayloadKind.CLARIFICATION
        raw = encode_clarification_answer(value)
    else:
        raise TypeError("value must be an exact reconstruction payload value")
    return ReconstructionPayload(
        ref=ReconstructionPayloadRef(
            kind=kind,
            id=value.id,
            contract_digest=value.digest,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
        ),
        raw=raw,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderInvocationRecord:
    invocation_id: str
    attempt_generation: int
    runtime: RuntimeIdentity
    model: str
    model_version: str
    budget: RuntimeBudget
    deadline_ms: int
    input_sha256: str
    lifecycle: RuntimeLifecycleState | None = None
    start_receipt_sha256: str | None = None
    result_sha256: str | None = None
    output_sha256: str | None = None
    diagnostic_digest: str | None = None
    intent_sha256: str = ""
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "invocation_id", _identifier(self.invocation_id, _INVOCATION_ID))
        object.__setattr__(
            self, "attempt_generation", _safe_integer(self.attempt_generation, positive=True)
        )
        if type(self.runtime) is not RuntimeIdentity or type(self.budget) is not RuntimeBudget:
            raise TypeError("runtime and budget must be exact generic runtime values")
        object.__setattr__(self, "model", _text(self.model, pattern=_NAME))
        object.__setattr__(self, "model_version", _text(self.model_version, pattern=_VERSION))
        object.__setattr__(self, "deadline_ms", _safe_integer(self.deadline_ms, positive=True))
        object.__setattr__(self, "input_sha256", _digest(self.input_sha256))
        expected_intent = hashlib.sha256(
            _PROVIDER_INTENT_DOMAIN + _canonical_json(self._intent_body(), maximum=16 * 1024)
        ).hexdigest()
        if self.intent_sha256 and not hmac.compare_digest(
            _digest(self.intent_sha256), expected_intent
        ):
            _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
        object.__setattr__(self, "intent_sha256", expected_intent)
        lifecycle = None if self.lifecycle is None else _enum(self.lifecycle, RuntimeLifecycleState)
        object.__setattr__(self, "lifecycle", lifecycle)
        for name in (
            "start_receipt_sha256",
            "result_sha256",
            "output_sha256",
            "diagnostic_digest",
        ):
            object.__setattr__(self, name, _optional_digest(getattr(self, name)))
        if lifecycle is None:
            if any(
                getattr(self, name) is not None
                for name in (
                    "start_receipt_sha256",
                    "result_sha256",
                    "output_sha256",
                    "diagnostic_digest",
                )
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        else:
            if self.start_receipt_sha256 is None:
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
            if lifecycle.is_terminal:
                if self.result_sha256 is None:
                    _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
                if lifecycle is RuntimeLifecycleState.SUCCEEDED:
                    if self.output_sha256 is None:
                        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
                elif self.output_sha256 is not None:
                    _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
            elif self.result_sha256 is not None or self.output_sha256 is not None:
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)

    @property
    def is_terminal(self) -> bool:
        return self.lifecycle is not None and self.lifecycle.is_terminal

    def _intent_body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "invocation_id": self.invocation_id,
            "attempt_generation": self.attempt_generation,
            "runtime": {
                "family": self.runtime.family,
                "provider": self.runtime.provider,
                "version": self.runtime.version,
            },
            "model": self.model,
            "model_version": self.model_version,
            "budget": {
                "max_elapsed_ms": self.budget.max_elapsed_ms,
                "max_memory_bytes": self.budget.max_memory_bytes,
                "max_output_bytes": self.budget.max_output_bytes,
            },
            "deadline_ms": self.deadline_ms,
            "input_sha256": self.input_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return self._intent_body() | {
            "intent_sha256": self.intent_sha256,
            "lifecycle": None if self.lifecycle is None else self.lifecycle.value,
            "start_receipt_sha256": self.start_receipt_sha256,
            "result_sha256": self.result_sha256,
            "output_sha256": self.output_sha256,
            "diagnostic_digest": self.diagnostic_digest,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact(
            value,
            {
                "schema_version",
                "invocation_id",
                "attempt_generation",
                "runtime",
                "model",
                "model_version",
                "budget",
                "deadline_ms",
                "input_sha256",
                "intent_sha256",
                "lifecycle",
                "start_receipt_sha256",
                "result_sha256",
                "output_sha256",
                "diagnostic_digest",
            },
        )
        runtime = _exact(data["runtime"], {"family", "provider", "version"})
        budget = _exact(
            data["budget"],
            {"max_elapsed_ms", "max_memory_bytes", "max_output_bytes"},
        )
        return cls(
            schema_version=data["schema_version"],
            invocation_id=data["invocation_id"],
            attempt_generation=data["attempt_generation"],
            runtime=RuntimeIdentity(**runtime),
            model=data["model"],
            model_version=data["model_version"],
            budget=RuntimeBudget(**budget),
            deadline_ms=data["deadline_ms"],
            input_sha256=data["input_sha256"],
            intent_sha256=data["intent_sha256"],
            lifecycle=data["lifecycle"],
            start_receipt_sha256=data["start_receipt_sha256"],
            result_sha256=data["result_sha256"],
            output_sha256=data["output_sha256"],
            diagnostic_digest=data["diagnostic_digest"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconstructionLastError:
    code: str
    phase: str
    retryable: bool
    diagnostic_digest: str
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "code", _text(self.code, pattern=_NAME))
        object.__setattr__(self, "phase", _text(self.phase, pattern=_NAME))
        if type(self.retryable) is not bool:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        object.__setattr__(self, "diagnostic_digest", _digest(self.diagnostic_digest))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "code": self.code,
            "phase": self.phase,
            "retryable": self.retryable,
            "diagnostic_digest": self.diagnostic_digest,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        return cls(
            **_exact(value, {"schema_version", "code", "phase", "retryable", "diagnostic_digest"})
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeleteCleanup:
    image_set_id: str
    image_set_manifest_sha256: str
    payload_refs: tuple[ReconstructionPayloadRef, ...]
    source_deleted: bool = False
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "image_set_id", _identifier(self.image_set_id, _IMAGE_SET_ID))
        object.__setattr__(
            self, "image_set_manifest_sha256", _digest(self.image_set_manifest_sha256)
        )
        if type(self.source_deleted) is not bool:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if type(self.payload_refs) is not tuple or len(self.payload_refs) > 130:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if any(type(item) is not ReconstructionPayloadRef for item in self.payload_refs):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if len({item.id for item in self.payload_refs}) != len(self.payload_refs):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        object.__setattr__(
            self, "payload_refs", tuple(sorted(self.payload_refs, key=lambda item: item.id))
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "payload_refs": [item.to_mapping() for item in self.payload_refs],
            "source_deleted": self.source_deleted,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact(
            value,
            {
                "schema_version",
                "image_set_id",
                "image_set_manifest_sha256",
                "payload_refs",
                "source_deleted",
            },
        )
        refs = data["payload_refs"]
        if type(refs) is not list:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        return cls(
            schema_version=data["schema_version"],
            image_set_id=data["image_set_id"],
            image_set_manifest_sha256=data["image_set_manifest_sha256"],
            payload_refs=tuple(ReconstructionPayloadRef.from_mapping(item) for item in refs),
            source_deleted=data["source_deleted"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AdoptedSourceProvenance:
    source_sha256: tuple[str, ...]
    proposal_digest: str
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        if (
            type(self.source_sha256) is not tuple
            or not 1 <= len(self.source_sha256) <= MAX_RECONSTRUCTION_SOURCE_DIGESTS
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        digests = tuple(_digest(item) for item in self.source_sha256)
        if len(digests) != len(set(digests)):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        object.__setattr__(self, "source_sha256", tuple(sorted(digests)))
        object.__setattr__(self, "proposal_digest", _digest(self.proposal_digest))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_sha256": list(self.source_sha256),
            "proposal_digest": self.proposal_digest,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        data = _exact(value, {"schema_version", "source_sha256", "proposal_digest"})
        digests = data["source_sha256"]
        if type(digests) is not list:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        return cls(
            schema_version=data["schema_version"],
            source_sha256=tuple(digests),
            proposal_digest=data["proposal_digest"],
        )


def _optional_ref(value: object) -> ReconstructionPayloadRef | None:
    return None if value is None else ReconstructionPayloadRef.from_mapping(value)


def _optional_object[ItemT](value: object, item_type: type[ItemT]) -> ItemT | None:
    if value is None:
        return None
    return item_type.from_mapping(value)  # type: ignore[attr-defined,no-any-return]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconstructionDraft:
    reconstruction_id: str
    create_key_sha256: str
    generation: int
    status: ReconstructionStatus
    base_head: BaseHeadBinding | None
    image_set_id: str | None
    image_set_manifest_sha256: str | None
    observation_ref: ReconstructionPayloadRef | None = None
    proposal_ref: ReconstructionPayloadRef | None = None
    clarification_refs: tuple[ReconstructionPayloadRef, ...] = ()
    provider_invocations: tuple[ProviderInvocationRecord, ...] = ()
    adoption_key_sha256: str | None = None
    adoption_intent_sha256: str | None = None
    adopted_task_id: str | None = None
    last_error: ReconstructionLastError | None = None
    delete_cleanup: DeleteCleanup | None = None
    adopted_source_provenance: AdoptedSourceProvenance | None = None
    schema_version: int = RECONSTRUCTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        reconstruction_id = _identifier(self.reconstruction_id, _RECONSTRUCTION_ID)
        create_digest = _digest(self.create_key_sha256)
        expected_id = (
            "reconstruction_"
            + hashlib.sha256(_RECONSTRUCTION_ID_DOMAIN + bytes.fromhex(create_digest)).hexdigest()[
                :32
            ]
        )
        if not hmac.compare_digest(reconstruction_id, expected_id):
            _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
        object.__setattr__(self, "reconstruction_id", reconstruction_id)
        object.__setattr__(self, "create_key_sha256", create_digest)
        object.__setattr__(self, "generation", _safe_integer(self.generation))
        status = _enum(self.status, ReconstructionStatus)
        object.__setattr__(self, "status", status)
        if self.base_head is not None and type(self.base_head) is not BaseHeadBinding:
            raise TypeError("base_head must be an exact BaseHeadBinding or null")
        if self.image_set_id is not None:
            object.__setattr__(self, "image_set_id", _identifier(self.image_set_id, _IMAGE_SET_ID))
        object.__setattr__(
            self,
            "image_set_manifest_sha256",
            _optional_digest(self.image_set_manifest_sha256),
        )
        for name, kind in (
            ("observation_ref", ReconstructionPayloadKind.OBSERVATION),
            ("proposal_ref", ReconstructionPayloadKind.PROPOSAL),
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not ReconstructionPayloadRef or value.kind is not kind
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if (
            type(self.clarification_refs) is not tuple
            or len(self.clarification_refs) > MAX_RECONSTRUCTION_CLARIFICATIONS
            or any(
                type(item) is not ReconstructionPayloadRef
                or item.kind is not ReconstructionPayloadKind.CLARIFICATION
                for item in self.clarification_refs
            )
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if len({item.id for item in self.clarification_refs}) != len(self.clarification_refs):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        object.__setattr__(
            self,
            "clarification_refs",
            tuple(sorted(self.clarification_refs, key=lambda item: item.id)),
        )
        if (
            type(self.provider_invocations) is not tuple
            or len(self.provider_invocations) > MAX_RECONSTRUCTION_PROVIDER_INVOCATIONS
            or any(type(item) is not ProviderInvocationRecord for item in self.provider_invocations)
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if len({item.invocation_id for item in self.provider_invocations}) != len(
            self.provider_invocations
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        attempts = tuple(item.attempt_generation for item in self.provider_invocations)
        if attempts != tuple(sorted(attempts)) or len(attempts) != len(set(attempts)):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if any(item.attempt_generation > self.generation for item in self.provider_invocations):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if self.image_set_id is not None and self.image_set_manifest_sha256 is not None:
            for item in self.provider_invocations:
                expected_invocation = visual_invocation_identity(
                    self.reconstruction_id,
                    item.attempt_generation,
                    self.image_set_id,
                    self.image_set_manifest_sha256,
                )
                if item.invocation_id != expected_invocation:
                    _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
        outstanding = tuple(item for item in self.provider_invocations if not item.is_terminal)
        if len(outstanding) > 1 or (
            outstanding and outstanding[0] is not self.provider_invocations[-1]
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        for name in ("adoption_key_sha256", "adoption_intent_sha256"):
            object.__setattr__(self, name, _optional_digest(getattr(self, name)))
        if self.adopted_task_id is not None:
            object.__setattr__(self, "adopted_task_id", _identifier(self.adopted_task_id, _TASK_ID))
        for name, expected in (
            ("last_error", ReconstructionLastError),
            ("delete_cleanup", DeleteCleanup),
            ("adopted_source_provenance", AdoptedSourceProvenance),
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not expected:
                raise TypeError(f"{name} must be an exact {expected.__name__} or null")
        self._validate_state(outstanding)

    @property
    def project_id(self) -> str | None:
        return None if self.base_head is None else self.base_head.project_id

    @property
    def next_action(self):
        return self.status.next_action

    @property
    def payload_refs(self) -> tuple[ReconstructionPayloadRef, ...]:
        result = (
            tuple(item for item in (self.observation_ref, self.proposal_ref) if item is not None)
            + self.clarification_refs
        )
        return tuple(sorted(result, key=lambda item: item.id))

    def _validate_state(self, outstanding: tuple[ProviderInvocationRecord, ...]) -> None:
        if self.status is ReconstructionStatus.DELETED:
            if (
                any(
                    value is not None
                    for value in (
                        self.base_head,
                        self.image_set_id,
                        self.image_set_manifest_sha256,
                        self.observation_ref,
                        self.proposal_ref,
                        self.last_error,
                    )
                )
                or self.clarification_refs
                or self.provider_invocations
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
            if self.adopted_task_id is None:
                if self.adopted_source_provenance is not None:
                    _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
            elif (
                self.adoption_key_sha256 is None
                or self.adoption_intent_sha256 is None
                or self.adopted_source_provenance is None
                or self.adopted_task_id
                != derive_adoption_task_identity(self.adoption_key_sha256)[1]
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
            return
        if (
            self.base_head is None
            or self.image_set_id is None
            or self.image_set_manifest_sha256 is None
            or self.delete_cleanup is not None
            or (
                self.status is not ReconstructionStatus.ADOPTED
                and self.adopted_source_provenance is not None
            )
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if self.status is ReconstructionStatus.OBSERVING and len(outstanding) != 1:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if (
            self.status is ReconstructionStatus.OBSERVING
            and outstanding
            and outstanding[0].lifecycle is RuntimeLifecycleState.UNKNOWN
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if (
            self.status
            not in {ReconstructionStatus.OBSERVING, ReconstructionStatus.RECOVERY_REQUIRED}
            and outstanding
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if (
            self.status
            in {
                ReconstructionStatus.NEEDS_INPUT,
                ReconstructionStatus.PROPOSED,
                ReconstructionStatus.ADOPTING,
                ReconstructionStatus.ADOPTED,
            }
            and self.observation_ref is None
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if (
            self.status
            in {
                ReconstructionStatus.PROPOSED,
                ReconstructionStatus.ADOPTING,
                ReconstructionStatus.ADOPTED,
            }
            and self.proposal_ref is None
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if self.status in {
            ReconstructionStatus.NEEDS_INPUT,
            ReconstructionStatus.PROPOSED,
            ReconstructionStatus.ADOPTING,
            ReconstructionStatus.ADOPTED,
        } and (
            not self.provider_invocations
            or self.provider_invocations[-1].lifecycle is not RuntimeLifecycleState.SUCCEEDED
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if self.status is ReconstructionStatus.FAILED and self.last_error is None:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if self.status is ReconstructionStatus.FAILED and (
            not self.provider_invocations
            or self.provider_invocations[-1].lifecycle
            not in {RuntimeLifecycleState.FAILED, RuntimeLifecycleState.CANCELLED}
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if (
            self.status not in {ReconstructionStatus.FAILED, ReconstructionStatus.RECOVERY_REQUIRED}
            and self.last_error is not None
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if self.status is ReconstructionStatus.RECOVERY_REQUIRED:
            adoption_uncertain = (
                self.adoption_key_sha256 is not None and self.adoption_intent_sha256 is not None
            )
            if self.last_error is None or not (outstanding or adoption_uncertain):
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
            if (self.adoption_key_sha256 is None) != (
                self.adoption_intent_sha256 is None
            ) or self.adopted_task_id is not None:
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        elif self.status in {ReconstructionStatus.ADOPTING, ReconstructionStatus.ADOPTED}:
            if self.adoption_key_sha256 is None or self.adoption_intent_sha256 is None:
                _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        elif any(
            value is not None
            for value in (
                self.adoption_key_sha256,
                self.adoption_intent_sha256,
                self.adopted_task_id,
            )
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if self.status is ReconstructionStatus.ADOPTED and (
            self.adopted_task_id is None
            or self.adopted_source_provenance is None
            or self.adopted_source_provenance.proposal_digest != self.proposal_ref.contract_digest
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        if (
            self.status is ReconstructionStatus.ADOPTED
            and self.adopted_task_id != derive_adoption_task_identity(self.adoption_key_sha256)[1]
        ):
            _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
        if self.status is ReconstructionStatus.ADOPTING and self.adopted_task_id is not None:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reconstruction_id": self.reconstruction_id,
            "create_key_sha256": self.create_key_sha256,
            "generation": self.generation,
            "status": self.status.value,
            "base_head": None if self.base_head is None else self.base_head.to_mapping(),
            "image_set_id": self.image_set_id,
            "image_set_manifest_sha256": self.image_set_manifest_sha256,
            "observation_ref": None
            if self.observation_ref is None
            else self.observation_ref.to_mapping(),
            "proposal_ref": None if self.proposal_ref is None else self.proposal_ref.to_mapping(),
            "clarification_refs": [item.to_mapping() for item in self.clarification_refs],
            "provider_invocations": [item.to_mapping() for item in self.provider_invocations],
            "adoption_key_sha256": self.adoption_key_sha256,
            "adoption_intent_sha256": self.adoption_intent_sha256,
            "adopted_task_id": self.adopted_task_id,
            "last_error": None if self.last_error is None else self.last_error.to_mapping(),
            "delete_cleanup": None
            if self.delete_cleanup is None
            else self.delete_cleanup.to_mapping(),
            "adopted_source_provenance": (
                None
                if self.adopted_source_provenance is None
                else self.adopted_source_provenance.to_mapping()
            ),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        fields = {
            "schema_version",
            "reconstruction_id",
            "create_key_sha256",
            "generation",
            "status",
            "base_head",
            "image_set_id",
            "image_set_manifest_sha256",
            "observation_ref",
            "proposal_ref",
            "clarification_refs",
            "provider_invocations",
            "adoption_key_sha256",
            "adoption_intent_sha256",
            "adopted_task_id",
            "last_error",
            "delete_cleanup",
            "adopted_source_provenance",
        }
        data = _exact(value, fields)
        clarifications = data["clarification_refs"]
        invocations = data["provider_invocations"]
        if type(clarifications) is not list or type(invocations) is not list:
            _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
        return cls(
            schema_version=data["schema_version"],
            reconstruction_id=data["reconstruction_id"],
            create_key_sha256=data["create_key_sha256"],
            generation=data["generation"],
            status=data["status"],
            base_head=_optional_object(data["base_head"], BaseHeadBinding),
            image_set_id=data["image_set_id"],
            image_set_manifest_sha256=data["image_set_manifest_sha256"],
            observation_ref=_optional_ref(data["observation_ref"]),
            proposal_ref=_optional_ref(data["proposal_ref"]),
            clarification_refs=tuple(
                ReconstructionPayloadRef.from_mapping(item) for item in clarifications
            ),
            provider_invocations=tuple(
                ProviderInvocationRecord.from_mapping(item) for item in invocations
            ),
            adoption_key_sha256=data["adoption_key_sha256"],
            adoption_intent_sha256=data["adoption_intent_sha256"],
            adopted_task_id=data["adopted_task_id"],
            last_error=_optional_object(data["last_error"], ReconstructionLastError),
            delete_cleanup=_optional_object(data["delete_cleanup"], DeleteCleanup),
            adopted_source_provenance=_optional_object(
                data["adopted_source_provenance"], AdoptedSourceProvenance
            ),
        )


def validate_reconstruction_creation(value: ReconstructionDraft) -> None:
    """Require the one legal generation-zero durable creation state."""

    if type(value) is not ReconstructionDraft:
        raise TypeError("value must be an exact ReconstructionDraft")
    if (
        value.generation != 0
        or value.status is not ReconstructionStatus.READY
        or value.observation_ref is not None
        or value.proposal_ref is not None
        or value.clarification_refs
        or value.provider_invocations
        or value.adoption_key_sha256 is not None
        or value.adoption_intent_sha256 is not None
        or value.adopted_task_id is not None
        or value.last_error is not None
        or value.delete_cleanup is not None
        or value.adopted_source_provenance is not None
    ):
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)


_TRANSITIONS = {
    ReconstructionStatus.READY: {
        ReconstructionStatus.OBSERVING,
        ReconstructionStatus.REJECTED,
        ReconstructionStatus.DELETED,
    },
    ReconstructionStatus.OBSERVING: {
        ReconstructionStatus.OBSERVING,
        ReconstructionStatus.READY,
        ReconstructionStatus.NEEDS_INPUT,
        ReconstructionStatus.PROPOSED,
        ReconstructionStatus.FAILED,
        ReconstructionStatus.RECOVERY_REQUIRED,
    },
    ReconstructionStatus.NEEDS_INPUT: {
        ReconstructionStatus.READY,
        ReconstructionStatus.REJECTED,
        ReconstructionStatus.DELETED,
    },
    ReconstructionStatus.PROPOSED: {
        ReconstructionStatus.ADOPTING,
        ReconstructionStatus.REJECTED,
        ReconstructionStatus.DELETED,
    },
    ReconstructionStatus.ADOPTING: {
        ReconstructionStatus.ADOPTED,
        ReconstructionStatus.RECOVERY_REQUIRED,
    },
    ReconstructionStatus.ADOPTED: {ReconstructionStatus.DELETED},
    ReconstructionStatus.FAILED: {
        ReconstructionStatus.READY,
        ReconstructionStatus.REJECTED,
        ReconstructionStatus.DELETED,
    },
    ReconstructionStatus.RECOVERY_REQUIRED: {
        ReconstructionStatus.RECOVERY_REQUIRED,
        ReconstructionStatus.READY,
        ReconstructionStatus.NEEDS_INPUT,
        ReconstructionStatus.PROPOSED,
        ReconstructionStatus.ADOPTED,
        ReconstructionStatus.FAILED,
    },
    ReconstructionStatus.REJECTED: {ReconstructionStatus.DELETED},
    ReconstructionStatus.DELETED: {ReconstructionStatus.DELETED},
}


def _same_provider_intent(left: ProviderInvocationRecord, right: ProviderInvocationRecord) -> bool:
    return (
        left.invocation_id == right.invocation_id
        and left.attempt_generation == right.attempt_generation
        and left.runtime == right.runtime
        and left.model == right.model
        and left.model_version == right.model_version
        and left.budget == right.budget
        and left.deadline_ms == right.deadline_ms
        and left.input_sha256 == right.input_sha256
        and left.intent_sha256 == right.intent_sha256
    )


def validate_reconstruction_successor(
    previous: ReconstructionDraft,
    successor: ReconstructionDraft,
) -> None:
    """Validate one generation-CAS successor without performing persistence."""

    if type(previous) is not ReconstructionDraft or type(successor) is not ReconstructionDraft:
        raise TypeError("successor values must be exact ReconstructionDraft values")
    if (
        previous.reconstruction_id != successor.reconstruction_id
        or previous.create_key_sha256 != successor.create_key_sha256
        or successor.generation != previous.generation + 1
        or successor.status not in _TRANSITIONS[previous.status]
    ):
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    if successor.status is not ReconstructionStatus.DELETED and (
        previous.base_head != successor.base_head
        or previous.image_set_id != successor.image_set_id
        or previous.image_set_manifest_sha256 != successor.image_set_manifest_sha256
    ):
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    old = previous.provider_invocations
    new = successor.provider_invocations
    if successor.status is ReconstructionStatus.DELETED:
        if previous.status is ReconstructionStatus.DELETED:
            if previous.delete_cleanup is None or (
                previous.delete_cleanup.source_deleted and successor.delete_cleanup is not None
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
            if (
                not previous.delete_cleanup.source_deleted
                and successor.delete_cleanup
                != dataclasses.replace(previous.delete_cleanup, source_deleted=True)
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
            if (
                previous.adoption_key_sha256 != successor.adoption_key_sha256
                or previous.adoption_intent_sha256 != successor.adoption_intent_sha256
                or previous.adopted_task_id != successor.adopted_task_id
                or previous.adopted_source_provenance != successor.adopted_source_provenance
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
            return
        expected_cleanup = DeleteCleanup(
            image_set_id=previous.image_set_id,
            image_set_manifest_sha256=previous.image_set_manifest_sha256,
            payload_refs=previous.payload_refs,
        )
        if successor.delete_cleanup != expected_cleanup:
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
        if previous.status is ReconstructionStatus.ADOPTED:
            if (
                successor.adoption_key_sha256 != previous.adoption_key_sha256
                or successor.adoption_intent_sha256 != previous.adoption_intent_sha256
                or successor.adopted_task_id != previous.adopted_task_id
                or successor.adopted_source_provenance is None
                or successor.adopted_source_provenance.proposal_digest
                != previous.proposal_ref.contract_digest
            ):
                _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
        elif any(
            value is not None
            for value in (
                successor.adoption_key_sha256,
                successor.adoption_intent_sha256,
                successor.adopted_task_id,
                successor.adopted_source_provenance,
            )
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
        return
    if len(new) not in {len(old), len(old) + 1}:
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    provider_changed = new != old
    if len(new) == len(old) + 1:
        if (
            previous.status is not ReconstructionStatus.READY
            or successor.status is not ReconstructionStatus.OBSERVING
            or new[:-1] != old
            or new[-1].attempt_generation != successor.generation
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    elif new != old:
        if (
            previous.status
            not in {ReconstructionStatus.OBSERVING, ReconstructionStatus.RECOVERY_REQUIRED}
            or not old
            or new[:-1] != old[:-1]
            or not _same_provider_intent(old[-1], new[-1])
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
        if old[-1].is_terminal or new[-1].lifecycle is None:
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
        if (
            old[-1].lifecycle is not None
            and old[-1].start_receipt_sha256 != new[-1].start_receipt_sha256
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    clarification_changed = previous.clarification_refs != successor.clarification_refs
    if clarification_changed and not (
        previous.status is ReconstructionStatus.NEEDS_INPUT
        and successor.status is ReconstructionStatus.READY
        and set(previous.clarification_refs).issubset(successor.clarification_refs)
        and len(successor.clarification_refs) > len(previous.clarification_refs)
    ):
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    completed_success = (
        provider_changed and bool(new) and new[-1].lifecycle is RuntimeLifecycleState.SUCCEEDED
    )
    observation_changed = previous.observation_ref != successor.observation_ref
    proposal_changed = previous.proposal_ref != successor.proposal_ref
    if observation_changed and not (
        previous.status in {ReconstructionStatus.OBSERVING, ReconstructionStatus.RECOVERY_REQUIRED}
        and successor.status
        in {
            ReconstructionStatus.READY,
            ReconstructionStatus.NEEDS_INPUT,
            ReconstructionStatus.PROPOSED,
        }
        and completed_success
    ):
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    if proposal_changed and not (
        previous.status in {ReconstructionStatus.OBSERVING, ReconstructionStatus.RECOVERY_REQUIRED}
        and successor.status is ReconstructionStatus.PROPOSED
        and completed_success
    ):
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    if previous.status is ReconstructionStatus.PROPOSED:
        expected_key, expected_intent = derive_adoption_identity(
            previous.reconstruction_id,
            previous.proposal_ref.contract_digest,
            previous.base_head.sha256,
        )
        if successor.status is ReconstructionStatus.ADOPTING and (
            successor.adoption_key_sha256 != expected_key
            or successor.adoption_intent_sha256 != expected_intent
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    elif previous.status is ReconstructionStatus.ADOPTING:
        if (
            successor.adoption_key_sha256 != previous.adoption_key_sha256
            or successor.adoption_intent_sha256 != previous.adoption_intent_sha256
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    elif (
        previous.status is ReconstructionStatus.RECOVERY_REQUIRED
        and previous.adoption_key_sha256 is not None
    ):
        if successor.status in {
            ReconstructionStatus.RECOVERY_REQUIRED,
            ReconstructionStatus.ADOPTED,
        } and (
            successor.adoption_key_sha256 != previous.adoption_key_sha256
            or successor.adoption_intent_sha256 != previous.adoption_intent_sha256
        ):
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
        if successor.status not in {
            ReconstructionStatus.RECOVERY_REQUIRED,
            ReconstructionStatus.ADOPTED,
            ReconstructionStatus.PROPOSED,
        }:
            _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)
    elif successor.status is ReconstructionStatus.ADOPTED:
        _fail(ReconstructionDraftErrorCode.INVALID_TRANSITION)


def encode_reconstruction_draft(value: ReconstructionDraft) -> bytes:
    if type(value) is not ReconstructionDraft:
        raise TypeError("value must be an exact ReconstructionDraft")
    body = value.to_mapping()
    checksum = hashlib.sha256(
        _DRAFT_CHECKSUM_DOMAIN
        + _canonical_json(body, maximum=MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES)
    ).hexdigest()
    return _canonical_json(
        body | {"checksum": checksum},
        maximum=MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES,
    )


def decode_reconstruction_draft(raw: object) -> ReconstructionDraft:
    data = _decode_json(raw, maximum=MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES)
    expected_fields = set(ReconstructionDraft.__dataclass_fields__) | {"checksum"}
    if set(data) != expected_fields:
        _fail(ReconstructionDraftErrorCode.INVALID_INPUT)
    checksum = _digest(data.pop("checksum"))
    expected = hashlib.sha256(
        _DRAFT_CHECKSUM_DOMAIN
        + _canonical_json(data, maximum=MAX_RECONSTRUCTION_DRAFT_RECORD_BYTES)
    ).hexdigest()
    if not hmac.compare_digest(checksum, expected):
        _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
    result = ReconstructionDraft.from_mapping(data)
    if encode_reconstruction_draft(result) != raw:
        _fail(ReconstructionDraftErrorCode.INTEGRITY_FAILURE)
    return result


def reconstruction_draft_record_sha256(value: ReconstructionDraft) -> str:
    return hashlib.sha256(encode_reconstruction_draft(value)).hexdigest()
