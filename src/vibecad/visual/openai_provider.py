"""Thin OpenAI Responses transport for strict visual observations.

The transport owns only an in-memory API key and one HTTPS sender.  It turns a
bounded provider-image batch into one Responses API call, accepts one strict
JSON observation payload, and returns provider-neutral domain values.  It has
no CAD, Task, revision, review, or durable-storage authority.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from vibecad.runtime.contracts import RuntimeDiagnostic, RuntimeIdentity
from vibecad.visual.cloud_provider import (
    CloudVisualOutcomeKind,
    CloudVisualProvider,
    CloudVisualRequest,
    CloudVisualTransportOutcome,
)
from vibecad.visual.provider import (
    VisualProviderExecutionReceipt,
    VisualProviderRuntimeProfile,
)
from vibecad.visual.provider_images import (
    ProviderImageDetail,
    ProviderImagePartKind,
    VisualProviderCapabilityProfile,
)
from vibecad.visual.reconstruction import (
    ClarificationQuestion,
    VisualClaim,
    VisualClaimStatus,
    VisualObservation,
    clarification_question_for_claim,
)

OPENAI_VISUAL_MODEL = "gpt-5.6-sol"
OPENAI_VISUAL_MODEL_VERSION = "alias-2026-08-04"
OPENAI_VISUAL_PROVIDER_VERSION = "responses-v1"

_OPENAI_HOST = "api.openai.com"
_OPENAI_PATH = "/v1/responses"
_MAX_HTTP_BODY_BYTES = 96 * 1024 * 1024
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_MAX_OUTPUT_TEXT_BYTES = 512 * 1024
_MAX_RESPONSE_ITEMS = 64
_MAX_CONTENT_ITEMS = 64
_MAX_API_KEY_BYTES = 4096
_MAX_SOURCE_INDEX = 15
_MAX_SAFE_INTEGER = 2**53 - 1
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class OpenAIVisualTransportError(ValueError):
    """Bounded local composition error that never reflects provider output."""


def _invalid() -> None:
    raise OpenAIVisualTransportError("invalid_openai_visual_transport")


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError
        result[key] = value
    return result


def _decode_json(raw: bytes, *, maximum: int) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        raise ValueError
    try:
        return json.loads(
            raw,
            object_pairs_hook=_duplicate_checked_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, RecursionError, json.JSONDecodeError):
        raise ValueError from None


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError
    return value


def _list(value: object, *, maximum: int) -> list[object]:
    if type(value) is not list or len(value) > maximum:
        raise ValueError
    return value


def _text(value: object, *, maximum: int = 4096) -> str:
    if type(value) is not str:
        raise ValueError
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        raise ValueError from None
    if not raw or len(raw) > maximum or value.strip() != value or not value.isprintable():
        raise ValueError
    return value


def _token_count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        raise ValueError
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenAIHttpResponse:
    status: int
    body: bytes
    complete: bool = True

    def __post_init__(self) -> None:
        if type(self.status) is not int or not 100 <= self.status <= 599:
            _invalid()
        if type(self.body) is not bytes or len(self.body) > _MAX_HTTP_RESPONSE_BYTES:
            _invalid()
        if type(self.complete) is not bool or (not self.complete and self.body):
            _invalid()


class OpenAIHttpSender(Protocol):
    def post(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout_ms: int,
    ) -> OpenAIHttpResponse: ...


class _OpenAIHttpsSender:
    __slots__ = ()

    def post(
        self,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout_ms: int,
    ) -> OpenAIHttpResponse:
        connection = http.client.HTTPSConnection(
            _OPENAI_HOST,
            443,
            timeout=timeout_ms / 1000,
        )
        try:
            connection.request("POST", _OPENAI_PATH, body=body, headers=dict(headers))
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None:
                try:
                    if int(length) > _MAX_HTTP_RESPONSE_BYTES:
                        return OpenAIHttpResponse(status=response.status, body=b"", complete=False)
                except ValueError:
                    return OpenAIHttpResponse(status=response.status, body=b"", complete=False)
            raw = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_HTTP_RESPONSE_BYTES:
                return OpenAIHttpResponse(status=response.status, body=b"", complete=False)
            return OpenAIHttpResponse(status=response.status, body=raw)
        finally:
            connection.close()


_OBSERVATION_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [item.value for item in VisualClaimStatus],
                    },
                    "source_indices": {"type": "array", "items": {"type": "integer"}},
                    "value": {
                        "anyOf": [
                            {"type": "number"},
                            {"type": "string"},
                            {"type": "boolean"},
                            {"type": "null"},
                        ]
                    },
                    "unit": {
                        "anyOf": [
                            {"type": "string", "enum": ["mm", "deg", "count", "ratio"]},
                            {"type": "null"},
                        ]
                    },
                    "blocking": {"type": "boolean"},
                    "description": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "question": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                },
                "required": [
                    "name",
                    "status",
                    "source_indices",
                    "value",
                    "unit",
                    "blocking",
                    "description",
                    "question",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}


def _prompt(request: CloudVisualRequest) -> str:
    source_count = len(
        {
            part.source_index
            for part in request.image_parts
            if part.kind is ProviderImagePartKind.OVERVIEW
        }
    )
    return (
        "Analyze these views of one mechanical object or engineering drawing and return only "
        "the requested visual-observation JSON. Source indices are zero-based and range from "
        f"0 to {source_count - 1}. Treat detail crops as evidence for their stated source, not "
        "as new sources. Extract only reconstruction-relevant geometry, dimensions, counts, "
        "angles, holes, symmetry, and feature relationships. Mark an absolute millimetre value "
        "confirmed only when a readable dimension or explicit scale supports it. Never infer "
        "absolute size from an unscaled photo. Treat a scale reference as metric evidence only "
        "for a coplanar measured boundary unless explicit camera calibration supports more. "
        "Use cross_view_derived only with at least two "
        "source indices. Assumptions need a concise confirmation question. Unknown or conflicting "
        "facts that block safe parametric reconstruction must have blocking=true and a question. "
        "When blur, glare, occlusion, perspective, or missing scale blocks a fact, ask for one "
        "concrete recapture or measurement. After a user resolves a hidden-geometry branch, replan "
        "from the complete evidence rather than silently patching an old candidate. "
        "Do not invent hidden geometry, do not propose CAD operations, and do not claim certainty "
        "from blur, occlusion, or duplicate views. Emit at least one claim."
    )


def _request_body(request: CloudVisualRequest) -> bytes:
    content: list[dict[str, object]] = [{"type": "input_text", "text": _prompt(request)}]
    for part in request.image_parts:
        label = (
            f"source_index={part.source_index}; view_role={part.view_role.value}; "
            f"part_kind={part.kind.value}; label={part.label or 'overview'}"
        )
        content.append({"type": "input_text", "text": label})
        encoded = base64.b64encode(part.data).decode("ascii")
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{encoded}",
                "detail": part.detail.value,
            }
        )
    value = {
        "model": request.image_batch.profile.model,
        "store": False,
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 8192,
        "input": [{"role": "user", "content": content}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "vibecad_visual_observation_v1",
                "strict": True,
                "schema": _OBSERVATION_SCHEMA,
            }
        },
    }
    raw = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(raw) > _MAX_HTTP_BODY_BYTES:
        _invalid()
    return raw


def _output_text(response: dict[str, object]) -> str:
    texts: list[str] = []
    for item in _list(response.get("output"), maximum=_MAX_RESPONSE_ITEMS):
        output = _object(item)
        if output.get("type") != "message":
            continue
        for content in _list(output.get("content"), maximum=_MAX_CONTENT_ITEMS):
            part = _object(content)
            if part.get("type") == "refusal":
                raise ValueError
            if part.get("type") == "output_text":
                texts.append(_text(part.get("text"), maximum=_MAX_OUTPUT_TEXT_BYTES))
    if len(texts) != 1:
        raise ValueError
    return texts[0]


def _observation(request: CloudVisualRequest, output_text: str) -> VisualObservation:
    decoded = _object(_decode_json(output_text.encode("utf-8"), maximum=_MAX_OUTPUT_TEXT_BYTES))
    if set(decoded) != {"claims"}:
        raise ValueError
    source_count = len(
        {
            part.source_index
            for part in request.image_parts
            if part.kind is ProviderImagePartKind.OVERVIEW
        }
    )
    claims: list[VisualClaim] = []
    questions: list[ClarificationQuestion] = []
    for item in _list(decoded["claims"], maximum=128):
        claim_value = _object(item)
        if set(claim_value) != {
            "name",
            "status",
            "source_indices",
            "value",
            "unit",
            "blocking",
            "description",
            "question",
        }:
            raise ValueError
        indices = _list(claim_value["source_indices"], maximum=16)
        if any(
            type(index) is not int or not 0 <= index < source_count or index > _MAX_SOURCE_INDEX
            for index in indices
        ):
            raise ValueError
        claim = VisualClaim(
            name=claim_value["name"],
            status=claim_value["status"],
            source_indices=tuple(indices),
            value=claim_value["value"],
            unit=claim_value["unit"],
            blocking=claim_value["blocking"],
            description=claim_value["description"],
        )
        question_text = claim_value["question"]
        needs_question = claim.status is VisualClaimStatus.ASSUMED or (
            claim.blocking
            and claim.status in {VisualClaimStatus.UNKNOWN, VisualClaimStatus.CONFLICT}
        )
        if needs_question:
            questions.append(clarification_question_for_claim(claim, question_text))
        elif question_text is not None:
            raise ValueError
        claims.append(claim)
    payload = request.invocation.payload
    return VisualObservation(
        reconstruction_id=payload["reconstruction_id"],
        generation=payload["generation"],
        image_set_id=payload["image_set_id"],
        image_set_manifest_sha256=payload["image_set_manifest_sha256"],
        invocation_id=request.invocation.invocation_id,
        claims=tuple(claims),
        questions=tuple(questions),
    )


def _diagnostic(code: str, message: str, *, retryable: bool = False) -> RuntimeDiagnostic:
    return RuntimeDiagnostic(code=code, message=message, retryable=retryable)


class OpenAIResponsesVisualTransport:
    """One exact, non-retrying OpenAI Responses API transport."""

    __slots__ = ("_api_key", "_sender")

    def __init__(self, api_key: str, *, sender: OpenAIHttpSender | None = None) -> None:
        if type(api_key) is not str:
            _invalid()
        try:
            raw_key = api_key.encode("ascii")
        except UnicodeError:
            _invalid()
        if (
            not raw_key
            or len(raw_key) > _MAX_API_KEY_BYTES
            or api_key.strip() != api_key
            or any(byte <= 32 or byte >= 127 for byte in raw_key)
        ):
            _invalid()
        selected = _OpenAIHttpsSender() if sender is None else sender
        if not callable(getattr(selected, "post", None)):
            _invalid()
        self._api_key = api_key
        self._sender = selected

    def invoke(
        self,
        request: CloudVisualRequest,
        *,
        timeout_ms: int,
    ) -> CloudVisualTransportOutcome:
        if (
            type(request) is not CloudVisualRequest
            or type(timeout_ms) is not int
            or timeout_ms != request.image_batch.profile.transport_timeout_ms
            or request.image_batch.profile.provider != "openai"
            or request.image_batch.profile.model != OPENAI_VISUAL_MODEL
            or request.image_batch.profile.model_version != OPENAI_VISUAL_MODEL_VERSION
            or request.invocation.runtime.provider != "openai"
            or request.invocation.payload.get("model") != OPENAI_VISUAL_MODEL
            or request.invocation.payload.get("model_version") != OPENAI_VISUAL_MODEL_VERSION
            or request.invocation.payload.get("network") is not True
            or request.invocation.execution_profile != "cloud_provider"
        ):
            _invalid()
        body = _request_body(request)
        response = self._sender.post(
            body,
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            timeout_ms=timeout_ms,
        )
        if type(response) is not OpenAIHttpResponse:
            raise TypeError
        if response.status != 200:
            return CloudVisualTransportOutcome(
                kind=CloudVisualOutcomeKind.DEFINITIVE_FAILURE,
                diagnostic=_diagnostic(
                    "provider.openai_request_rejected",
                    "OpenAI rejected or failed the Responses request.",
                    retryable=response.status in {408, 409, 429} or response.status >= 500,
                ),
            )
        if not response.complete:
            return CloudVisualTransportOutcome(
                kind=CloudVisualOutcomeKind.DEFINITIVE_FAILURE,
                diagnostic=_diagnostic(
                    "provider.openai_response_too_large",
                    "OpenAI returned a response larger than the accepted result envelope.",
                ),
            )
        try:
            decoded = _object(_decode_json(response.body, maximum=_MAX_HTTP_RESPONSE_BYTES))
            if decoded.get("status") != "completed":
                raise ValueError
            response_id = _text(decoded.get("id"), maximum=512)
            response_model = _text(decoded.get("model"), maximum=128)
            if _PROFILE_NAME.fullmatch(response_model) is None:
                raise ValueError
            usage = _object(decoded.get("usage"))
            input_tokens = _token_count(usage.get("input_tokens"))
            output_tokens = _token_count(usage.get("output_tokens"))
            total_tokens = _token_count(usage.get("total_tokens"))
            if total_tokens != input_tokens + output_tokens:
                raise ValueError
            text = _output_text(decoded)
            value = _observation(request, text)
            receipt = VisualProviderExecutionReceipt(
                request_sha256=request.request_sha256,
                image_batch_sha256=request.image_batch.manifest_sha256,
                response_id_sha256=hashlib.sha256(response_id.encode("utf-8")).hexdigest(),
                response_output_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                response_model=response_model,
                data_policy_profile=request.image_batch.profile.data_policy_profile,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                transport_timeout_ms=timeout_ms,
            )
        except (TypeError, ValueError, OverflowError):
            return CloudVisualTransportOutcome(
                kind=CloudVisualOutcomeKind.DEFINITIVE_FAILURE,
                diagnostic=_diagnostic(
                    "provider.openai_invalid_response",
                    "OpenAI returned an unusable visual observation response.",
                ),
            )
        return CloudVisualTransportOutcome(
            kind=CloudVisualOutcomeKind.SUCCEEDED,
            value=value,
            execution_receipt=receipt,
        )


def openai_visual_profiles(
    *,
    data_policy_profile: str,
) -> tuple[VisualProviderRuntimeProfile, VisualProviderCapabilityProfile]:
    """Return the exact quality-first OpenAI pilot profiles."""

    if type(data_policy_profile) is not str or _PROFILE_NAME.fullmatch(data_policy_profile) is None:
        _invalid()
    runtime = VisualProviderRuntimeProfile(
        identity=RuntimeIdentity(
            family="visual",
            provider="openai",
            version=OPENAI_VISUAL_PROVIDER_VERSION,
        ),
        model=OPENAI_VISUAL_MODEL,
        model_version=OPENAI_VISUAL_MODEL_VERSION,
        execution_profile="cloud_provider",
        network=True,
    )
    images = VisualProviderCapabilityProfile(
        provider="openai",
        model=OPENAI_VISUAL_MODEL,
        model_version=OPENAI_VISUAL_MODEL_VERSION,
        data_policy_profile=data_policy_profile,
        max_source_images=16,
        max_image_parts=32,
        max_image_bytes=8 * 1024 * 1024,
        max_batch_image_bytes=64 * 1024 * 1024,
        preferred_long_edge=2048,
        max_long_edge=4096,
        detail=ProviderImageDetail.ORIGINAL,
        supports_detail_crops=True,
        transport_timeout_ms=180_000,
    )
    return runtime, images


def create_openai_visual_provider(
    *,
    api_key: str,
    image_reader: object,
    data_policy_profile: str,
    sender: OpenAIHttpSender | None = None,
) -> CloudVisualProvider:
    """Compose the real OpenAI transport behind the provider-neutral adapter."""

    runtime, images = openai_visual_profiles(data_policy_profile=data_policy_profile)
    return CloudVisualProvider(
        runtime_profile=runtime,
        image_profile=images,
        image_reader=image_reader,
        transport=OpenAIResponsesVisualTransport(api_key, sender=sender),
    )


__all__ = [
    "OPENAI_VISUAL_MODEL",
    "OPENAI_VISUAL_MODEL_VERSION",
    "OPENAI_VISUAL_PROVIDER_VERSION",
    "OpenAIHttpResponse",
    "OpenAIHttpSender",
    "OpenAIResponsesVisualTransport",
    "OpenAIVisualTransportError",
    "create_openai_visual_provider",
    "openai_visual_profiles",
]
