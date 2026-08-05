"""Offline contract tests for the first real cloud-VLM transport."""

from __future__ import annotations

import hashlib
import io
import json

import pytest
from PIL import Image

from vibecad.runtime.contracts import RuntimeBudget
from vibecad.visual.cloud_provider import (
    CloudVisualOutcomeKind,
    CloudVisualRequest,
)
from vibecad.visual.contracts import (
    NORMALIZATION_PROFILE,
    SOURCE_PNG_PROFILE,
    CalibrationStatus,
    ImageMime,
    ImageRef,
    ImageSet,
    ProcessingAuthorization,
    ViewRole,
    VisualInput,
    image_set_identity,
    visual_input_identity,
)
from vibecad.visual.openai_provider import (
    OPENAI_VISUAL_MODEL,
    OPENAI_VISUAL_MODEL_VERSION,
    OpenAIHttpResponse,
    OpenAIResponsesVisualTransport,
    OpenAIVisualTransportError,
    create_openai_visual_provider,
    openai_visual_profiles,
)
from vibecad.visual.provider import (
    VisualProviderBinding,
    build_visual_provider_invocation,
    visual_provider_input_digest,
)
from vibecad.visual.provider_images import ProviderImageDetail, prepare_provider_image_batch
from vibecad.visual.reconstruction import VisualClaimStatus, reconstruction_identity

_IMAGE_CREATE_KEY = "image_set_create_55555555555555555555555555555555"
_RECONSTRUCTION_CREATE_KEY = "reconstruction_create_66666666666666666666666666666666"
_RECONSTRUCTION_ID, _ = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (96, 72), (220, 220, 220)).save(stream, format="PNG")
    return stream.getvalue()


def _request() -> CloudVisualRequest:
    normalized = _png()
    image_set_id, create_digest = image_set_identity(_IMAGE_CREATE_KEY)
    image_set = ImageSet(
        id=image_set_id,
        create_key_digest=create_digest,
        inputs=(
            VisualInput(
                original=ImageRef(
                    id=visual_input_identity(_IMAGE_CREATE_KEY, 0, "original"),
                    sha256=hashlib.sha256(b"original").hexdigest(),
                    size_bytes=128,
                    mime=ImageMime.PNG,
                    width=96,
                    height=72,
                    profile=SOURCE_PNG_PROFILE,
                ),
                normalized=ImageRef(
                    id=visual_input_identity(_IMAGE_CREATE_KEY, 0, "normalized"),
                    sha256=hashlib.sha256(normalized).hexdigest(),
                    size_bytes=len(normalized),
                    mime=ImageMime.PNG,
                    width=96,
                    height=72,
                    profile=NORMALIZATION_PROFILE,
                ),
                view_role=ViewRole.FRONT,
                calibration_status=CalibrationStatus.UNKNOWN,
            ),
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=True,
        same_state=True,
        same_scale=True,
        processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
    )
    runtime, images = openai_visual_profiles(data_policy_profile="personal-default")
    invocation = build_visual_provider_invocation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=1,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        budget=RuntimeBudget(
            max_elapsed_ms=300_000,
            max_memory_bytes=512 * 1024 * 1024,
            max_output_bytes=1024 * 1024,
        ),
        deadline_ms=400_000,
        runtime_profile=runtime,
    )
    batch = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=(normalized,),
        profile=images,
    )
    return CloudVisualRequest(
        invocation=invocation,
        input_sha256=visual_provider_input_digest(invocation, runtime_profile=runtime),
        image_batch=batch,
    )


def _response_body(*, claim: dict[str, object] | None = None) -> bytes:
    selected = claim or {
        "name": "overall.width",
        "status": "confirmed",
        "source_indices": [0],
        "value": 40,
        "unit": "mm",
        "blocking": False,
        "description": "Readable dimension annotation.",
        "question": None,
    }
    output_text = json.dumps({"claims": [selected]}, separators=(",", ":"))
    return json.dumps(
        {
            "id": "resp_fixture_123",
            "model": OPENAI_VISUAL_MODEL,
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": output_text}],
                }
            ],
            "usage": {"input_tokens": 500, "output_tokens": 80, "total_tokens": 580},
        },
        separators=(",", ":"),
    ).encode("utf-8")


class _Sender:
    __slots__ = ("body", "calls", "headers", "response", "timeout_ms")

    def __init__(self, response: OpenAIHttpResponse) -> None:
        self.response = response
        self.calls = 0
        self.body = None
        self.headers = None
        self.timeout_ms = None

    def post(self, body, headers, *, timeout_ms):
        self.calls += 1
        self.body = body
        self.headers = dict(headers)
        self.timeout_ms = timeout_ms
        return self.response


def test_quality_first_profile_is_bounded_to_sixteen_adaptive_derivatives() -> None:
    runtime, images = openai_visual_profiles(data_policy_profile="enterprise-default")

    assert runtime.identity.provider == images.provider == "openai"
    assert runtime.model == images.model == OPENAI_VISUAL_MODEL
    assert runtime.model_version == images.model_version == OPENAI_VISUAL_MODEL_VERSION
    assert images.max_source_images == 16
    assert images.max_image_parts == 32
    assert images.preferred_long_edge == 2048
    assert images.max_long_edge == 4096
    assert images.detail is ProviderImageDetail.ORIGINAL
    assert images.transport_timeout_ms == 180_000

    provider = create_openai_visual_provider(
        api_key="sk-test",
        image_reader=lambda _image_set_id, _manifest_sha256: None,
        data_policy_profile="enterprise-default",
        sender=_Sender(OpenAIHttpResponse(status=500, body=b"{}")),
    )
    binding = VisualProviderBinding(provider=provider)
    assert binding.runtime_profile == runtime


def test_success_builds_one_strict_response_call_and_execution_receipt() -> None:
    request = _request()
    sender = _Sender(OpenAIHttpResponse(status=200, body=_response_body()))
    transport = OpenAIResponsesVisualTransport("sk-test-value", sender=sender)

    outcome = transport.invoke(request, timeout_ms=180_000)

    assert outcome.kind is CloudVisualOutcomeKind.SUCCEEDED
    assert outcome.value is not None
    assert outcome.value.claims[0].status is VisualClaimStatus.CONFIRMED
    assert outcome.value.claims[0].value == 40
    assert outcome.execution_receipt is not None
    assert outcome.execution_receipt.request_sha256 == request.request_sha256
    assert outcome.execution_receipt.image_batch_sha256 == request.image_batch.manifest_sha256
    assert outcome.execution_receipt.total_tokens == 580
    assert sender.calls == 1
    assert sender.timeout_ms == 180_000
    assert sender.headers == {
        "Authorization": "Bearer sk-test-value",
        "Content-Type": "application/json",
    }

    wire = json.loads(sender.body)
    assert wire["model"] == OPENAI_VISUAL_MODEL
    assert wire["store"] is False
    assert wire["reasoning"] == {"effort": "medium"}
    assert wire["text"]["format"]["type"] == "json_schema"
    assert wire["text"]["format"]["strict"] is True
    content = wire["input"][0]["content"]
    assert "Never infer absolute size from an unscaled photo" in content[0]["text"]
    assert content[2]["type"] == "input_image"
    assert content[2]["image_url"].startswith("data:image/png;base64,")
    assert "sk-test-value" not in sender.body.decode("utf-8")
    assert "sk-test-value" not in repr(transport)
    assert "sk-test-value" not in repr(outcome.execution_receipt)


def test_blocking_unknown_becomes_a_bound_clarification_question() -> None:
    claim = {
        "name": "overall.depth",
        "status": "unknown",
        "source_indices": [0],
        "value": None,
        "unit": "mm",
        "blocking": True,
        "description": "Depth is not visible in the front view.",
        "question": "What is the overall depth in millimetres?",
    }
    sender = _Sender(OpenAIHttpResponse(status=200, body=_response_body(claim=claim)))

    outcome = OpenAIResponsesVisualTransport("sk-test", sender=sender).invoke(
        _request(),
        timeout_ms=180_000,
    )

    assert outcome.kind is CloudVisualOutcomeKind.SUCCEEDED
    assert outcome.value is not None
    assert outcome.value.proposal_blockers == (outcome.value.claims[0].id,)
    assert outcome.value.questions[0].claim_id == outcome.value.claims[0].id


@pytest.mark.parametrize(
    "response",
    (
        OpenAIHttpResponse(status=429, body=b"{}"),
        OpenAIHttpResponse(status=200, body=b"", complete=False),
        OpenAIHttpResponse(status=200, body=b"{}"),
        OpenAIHttpResponse(
            status=200,
            body=_response_body(
                claim={
                    "name": "overall.depth",
                    "status": "unknown",
                    "source_indices": [0],
                    "value": None,
                    "unit": "mm",
                    "blocking": True,
                    "description": "Missing depth.",
                    "question": None,
                }
            ),
        ),
    ),
)
def test_rejections_and_unusable_responses_fail_definitively_without_retry(
    response: OpenAIHttpResponse,
) -> None:
    sender = _Sender(response)

    outcome = OpenAIResponsesVisualTransport("sk-test", sender=sender).invoke(
        _request(),
        timeout_ms=180_000,
    )

    assert outcome.kind is CloudVisualOutcomeKind.DEFINITIVE_FAILURE
    assert outcome.diagnostic is not None
    assert outcome.value is None
    assert outcome.execution_receipt is None
    assert sender.calls == 1


def test_credentials_and_profile_values_are_fail_closed() -> None:
    for key in ("", " leading", "trailing ", "bad\nkey"):
        with pytest.raises(OpenAIVisualTransportError):
            OpenAIResponsesVisualTransport(key)
    for profile in ("", "Enterprise Default", "bad/profile"):
        with pytest.raises(OpenAIVisualTransportError):
            openai_visual_profiles(data_policy_profile=profile)
