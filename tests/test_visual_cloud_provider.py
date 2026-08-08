"""Focused one-effect tests for the provider-neutral cloud adapter."""

from __future__ import annotations

import dataclasses
import hashlib
import io

import pytest
from PIL import Image

from vibecad.runtime.contracts import (
    RuntimeBudget,
    RuntimeDiagnostic,
    RuntimeIdentity,
    RuntimeLifecycleState,
)
from vibecad.visual.cloud_provider import (
    CloudVisualOutcomeKind,
    CloudVisualProvider,
    CloudVisualRequest,
    CloudVisualTransportOutcome,
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
from vibecad.visual.evidence import NormalizedEvidencePoint, ProviderFeatureEvidence
from vibecad.visual.evidence_provider import (
    VisualEvidenceProviderBinding,
    VisualEvidenceProviderError,
    VisualEvidenceProviderErrorCode,
)
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.provider import (
    VisualProviderBinding,
    VisualProviderExecutionReceipt,
    VisualProviderOutput,
    VisualProviderRuntimeProfile,
    build_visual_provider_invocation,
)
from vibecad.visual.provider_images import (
    ProviderImageDetail,
    VisualProviderCapabilityProfile,
)
from vibecad.visual.reconstruction import (
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    reconstruction_identity,
)

_IMAGE_CREATE_KEY = "image_set_create_33333333333333333333333333333333"
_RECONSTRUCTION_CREATE_KEY = "reconstruction_create_44444444444444444444444444444444"
_RECONSTRUCTION_ID, _ = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (64, 48), (20, 80, 140)).save(stream, format="PNG")
    return stream.getvalue()


def _image_set() -> tuple[ImageSet, tuple[bytes, ...]]:
    raw = _png()
    image_set_id, create_digest = image_set_identity(_IMAGE_CREATE_KEY)
    record = ImageSet(
        id=image_set_id,
        create_key_digest=create_digest,
        inputs=(
            VisualInput(
                original=ImageRef(
                    id=visual_input_identity(_IMAGE_CREATE_KEY, 0, "original"),
                    sha256=hashlib.sha256(b"original").hexdigest(),
                    size_bytes=128,
                    mime=ImageMime.PNG,
                    width=64,
                    height=48,
                    profile=SOURCE_PNG_PROFILE,
                ),
                normalized=ImageRef(
                    id=visual_input_identity(_IMAGE_CREATE_KEY, 0, "normalized"),
                    sha256=hashlib.sha256(raw).hexdigest(),
                    size_bytes=len(raw),
                    mime=ImageMime.PNG,
                    width=64,
                    height=48,
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
    return record, (raw,)


def _profiles():
    runtime = VisualProviderRuntimeProfile(
        identity=RuntimeIdentity(family="visual", provider="candidate_cloud", version="1.0"),
        model="vision-model",
        model_version="2026-08-04",
        execution_profile="cloud_provider",
        network=True,
    )
    images = VisualProviderCapabilityProfile(
        provider="candidate_cloud",
        model="vision-model",
        model_version="2026-08-04",
        data_policy_profile="personal-default",
        max_source_images=16,
        max_image_parts=20,
        max_image_bytes=2 * 1024 * 1024,
        max_batch_image_bytes=20 * 1024 * 1024,
        preferred_long_edge=1568,
        max_long_edge=2000,
        detail=ProviderImageDetail.HIGH,
        supports_detail_crops=True,
        transport_timeout_ms=120_000,
    )
    return runtime, images


def _invocation(runtime: VisualProviderRuntimeProfile, image_set: ImageSet):
    return build_visual_provider_invocation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=1,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        budget=RuntimeBudget(
            max_elapsed_ms=180_000,
            max_memory_bytes=512 * 1024 * 1024,
            max_output_bytes=1024 * 1024,
        ),
        deadline_ms=200_000,
        runtime_profile=runtime,
    )


def _observation(invocation) -> VisualObservation:
    return VisualObservation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=1,
        image_set_id=invocation.payload["image_set_id"],
        image_set_manifest_sha256=invocation.payload["image_set_manifest_sha256"],
        invocation_id=invocation.invocation_id,
        claims=(
            VisualClaim(
                name="overall.width",
                status=VisualClaimStatus.CONFIRMED,
                source_indices=(0,),
                value=40,
                unit=VisualClaimUnit.MM,
                description="Candidate visual estimate",
            ),
        ),
    )


def _execution_receipt(
    *,
    request_sha256: str = "1" * 64,
    image_batch_sha256: str = "2" * 64,
) -> VisualProviderExecutionReceipt:
    return VisualProviderExecutionReceipt(
        request_sha256=request_sha256,
        image_batch_sha256=image_batch_sha256,
        response_id_sha256="3" * 64,
        response_output_sha256="4" * 64,
        response_model="vision-model",
        data_policy_profile="personal-default",
        input_tokens=120,
        output_tokens=30,
        total_tokens=150,
        transport_timeout_ms=120_000,
    )


class _Transport:
    __slots__ = ("_outcome", "bind_receipt", "calls", "last_request", "raise_error")

    def __init__(
        self,
        outcome=None,
        *,
        raise_error: bool = False,
        bind_receipt: bool = True,
    ) -> None:
        self._outcome = outcome
        self.raise_error = raise_error
        self.bind_receipt = bind_receipt
        self.calls = 0
        self.last_request = None

    def invoke(self, request, *, timeout_ms):
        self.calls += 1
        assert timeout_ms == 120_000
        assert type(request) is CloudVisualRequest
        self.last_request = request
        if self.raise_error:
            raise TimeoutError
        if (
            self.bind_receipt
            and type(self._outcome) is CloudVisualTransportOutcome
            and self._outcome.kind is CloudVisualOutcomeKind.SUCCEEDED
        ):
            assert self._outcome.execution_receipt is not None
            return dataclasses.replace(
                self._outcome,
                execution_receipt=dataclasses.replace(
                    self._outcome.execution_receipt,
                    request_sha256=request.request_sha256,
                    image_batch_sha256=request.image_batch.manifest_sha256,
                ),
            )
        return self._outcome


class _FeatureTransport:
    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request, *, timeout_ms):
        self.calls += 1
        assert timeout_ms == 120_000
        observation = _observation(request.invocation)
        return CloudVisualTransportOutcome(
            kind=CloudVisualOutcomeKind.SUCCEEDED,
            value=observation,
            execution_receipt=_execution_receipt(
                request_sha256=request.request_sha256,
                image_batch_sha256=request.image_batch.manifest_sha256,
            ),
            feature_evidence=(
                ProviderFeatureEvidence(
                    local_feature_id="outer_width_edge",
                    source_index=0,
                    provider_image_id=request.image_batch.parts[0].id,
                    family=PrimitiveFamily.LINE,
                    points=(
                        NormalizedEvidencePoint(x=0.2, y=0.4),
                        NormalizedEvidencePoint(x=0.8, y=0.4),
                    ),
                    localization_uncertainty_norm=0.01,
                    claim_ids=(observation.claims[0].id,),
                ),
            ),
        )


def _provider(transport: _Transport):
    image_set, images = _image_set()
    runtime, image_profile = _profiles()

    def reader(image_set_id: str, manifest_sha256: str):
        assert image_set_id == image_set.id
        assert manifest_sha256 == image_set.manifest_sha256
        return image_set, images

    provider = CloudVisualProvider(
        runtime_profile=runtime,
        image_profile=image_profile,
        image_reader=reader,
        transport=transport,
    )
    return image_set, runtime, provider


def test_success_is_one_transport_effect_and_returns_strict_visual_result() -> None:
    transport = _Transport()
    image_set, runtime, provider = _provider(transport)
    invocation = _invocation(runtime, image_set)
    transport._outcome = CloudVisualTransportOutcome(
        kind=CloudVisualOutcomeKind.SUCCEEDED,
        value=_observation(invocation),
        execution_receipt=_execution_receipt(),
    )
    binding = VisualProviderBinding(provider=provider)

    first = binding.control.start(invocation)
    replay = binding.control.start(invocation)
    result = binding.retrieve_result(invocation)

    assert first == replay
    assert first.state is RuntimeLifecycleState.SUCCEEDED
    assert transport.calls == provider.transport_count == 1
    assert result is not None
    assert result.provenance is not None
    assert result.provenance.details["execution"]["total_tokens"] == 150
    assert VisualProviderOutput.from_mapping(result.output).value == _observation(invocation)
    assert transport.last_request is not None
    assert len(transport.last_request.image_parts) == 1
    assert "data" not in transport.last_request.to_manifest_mapping()
    assert binding.runtime_profile == runtime


def test_success_binds_feature_evidence_and_exposes_it_through_narrow_reader() -> None:
    transport = _FeatureTransport()
    image_set, runtime, provider = _provider(transport)
    invocation = _invocation(runtime, image_set)
    provider_binding = VisualProviderBinding(provider=provider)
    evidence_binding = VisualEvidenceProviderBinding(provider_binding=provider_binding)

    status = provider_binding.control.start(invocation)
    result = provider_binding.retrieve_result(invocation)
    assert result is not None
    output = VisualProviderOutput.from_mapping(result.output)
    observation = output.value
    assert type(observation) is VisualObservation
    evidence = evidence_binding.retrieve(invocation, result)

    assert status.state is RuntimeLifecycleState.SUCCEEDED
    assert evidence is not None
    assert evidence.image_set_id == image_set.id
    assert evidence.observation_id == observation.id
    assert evidence.features[0].local_feature_id == "outer_width_edge"
    assert evidence.features[0].pixel_points[0].x_px == pytest.approx(12.6)
    assert evidence.features[0].pixel_points[1].x_px == pytest.approx(50.4)
    assert transport.calls == 1

    provider._bound_evidence[invocation.invocation_id] = dataclasses.replace(
        evidence,
        image_batch_manifest_sha256="f" * 64,
    )
    with pytest.raises(VisualEvidenceProviderError) as raised:
        evidence_binding.retrieve(invocation, result)
    assert raised.value.code is VisualEvidenceProviderErrorCode.RESULT_MISMATCH


def test_process_restart_makes_optional_evidence_unknown_without_provider_replay() -> None:
    transport = _FeatureTransport()
    image_set, runtime, provider = _provider(transport)
    invocation = _invocation(runtime, image_set)
    binding = VisualProviderBinding(provider=provider)
    binding.control.start(invocation)
    result = binding.retrieve_result(invocation)
    assert result is not None
    observation = VisualProviderOutput.from_mapping(result.output).value
    assert type(observation) is VisualObservation

    fresh_transport = _FeatureTransport()
    _, _, fresh_provider = _provider(fresh_transport)
    fresh_binding = VisualEvidenceProviderBinding(
        provider_binding=VisualProviderBinding(provider=fresh_provider)
    )

    assert fresh_binding.retrieve(invocation, result) is None
    assert fresh_transport.calls == 0


def test_mismatched_request_or_image_receipt_fails_closed_without_replay() -> None:
    transport = _Transport(bind_receipt=False)
    image_set, runtime, provider = _provider(transport)
    invocation = _invocation(runtime, image_set)
    transport._outcome = CloudVisualTransportOutcome(
        kind=CloudVisualOutcomeKind.SUCCEEDED,
        value=_observation(invocation),
        execution_receipt=_execution_receipt(),
    )

    first = provider.start(invocation)
    replay = provider.start(invocation)

    assert first == replay
    assert first.state is RuntimeLifecycleState.UNKNOWN
    assert provider.get_result(invocation.invocation_id) is None
    assert provider.get_bound_evidence(invocation.invocation_id) is None
    assert transport.calls == 1


def test_transport_exception_is_unknown_and_never_replayed_by_reconcile_or_start() -> None:
    transport = _Transport(raise_error=True)
    image_set, runtime, provider = _provider(transport)
    invocation = _invocation(runtime, image_set)

    started = provider.start(invocation)
    reconciled = provider.reconcile(invocation.invocation_id)
    replay = provider.start(invocation)

    assert started.state is RuntimeLifecycleState.UNKNOWN
    assert reconciled == replay == started
    assert provider.get_result(invocation.invocation_id) is None
    assert transport.calls == provider.transport_count == 1


def test_definitive_provider_rejection_is_failed_without_automatic_retry() -> None:
    transport = _Transport(
        CloudVisualTransportOutcome(
            kind=CloudVisualOutcomeKind.DEFINITIVE_FAILURE,
            diagnostic=RuntimeDiagnostic(
                code="provider.request_rejected",
                message="Provider rejected the request before execution.",
                retryable=True,
            ),
        )
    )
    image_set, runtime, provider = _provider(transport)
    invocation = _invocation(runtime, image_set)

    status = provider.start(invocation)
    result = provider.get_result(invocation.invocation_id)

    assert status.state is RuntimeLifecycleState.FAILED
    assert result is not None and result.state is RuntimeLifecycleState.FAILED
    assert provider.reconcile(invocation.invocation_id) == status
    assert transport.calls == 1


def test_non_success_transport_outcomes_cannot_smuggle_feature_evidence() -> None:
    image_set, runtime, _provider_value = _provider(_Transport())
    invocation = _invocation(runtime, image_set)
    observation = _observation(invocation)
    feature = ProviderFeatureEvidence(
        local_feature_id="edge",
        source_index=0,
        provider_image_id="provider_image_" + "1" * 32,
        family=PrimitiveFamily.LINE,
        points=(NormalizedEvidencePoint(x=0.1, y=0.2),),
        localization_uncertainty_norm=0.01,
        claim_ids=(observation.claims[0].id,),
    )

    with pytest.raises(ValueError):
        CloudVisualTransportOutcome(
            kind=CloudVisualOutcomeKind.UNKNOWN,
            feature_evidence=(feature,),
        )
    with pytest.raises(ValueError):
        CloudVisualTransportOutcome(
            kind=CloudVisualOutcomeKind.DEFINITIVE_FAILURE,
            diagnostic=RuntimeDiagnostic(code="provider.failed", message="Failed."),
            feature_evidence=(feature,),
        )
