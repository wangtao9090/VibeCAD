from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from PIL import Image

from vibecad.application.data import ApplicationDataLayout
from vibecad.parametric.contracts import (
    BodyDefinition,
    DesignEvidence,
    DesignEvidenceOrigin,
    DesignEvidenceStatus,
    DesignParameter,
    DesignUnit,
    FeatureExtent,
    FeatureKind,
    GeometryKind,
    OriginPlane,
    ParameterKind,
    ParametricDesignIR,
    ParametricSketch,
    PartDesignFeature,
    PlaneKind,
    SketchGeometry,
    SketchPlane,
    SketchRole,
    UnitSystem,
)
from vibecad.runtime.contracts import (
    RuntimeBudget,
    RuntimeDiagnostic,
    RuntimeHealth,
    RuntimeHealthState,
    RuntimeIdentity,
    RuntimeLifecycleState,
    RuntimeStatus,
)
from vibecad.visual.cloud_provider import (
    CloudVisualOutcomeKind,
    CloudVisualProvider,
    CloudVisualTransportOutcome,
)
from vibecad.visual.contracts import (
    CalibrationStatus,
    ImageMime,
    ProcessingAuthorization,
    ViewRole,
)
from vibecad.visual.drafts import BaseHeadBinding
from vibecad.visual.evidence import NormalizedEvidencePoint, ProviderFeatureEvidence
from vibecad.visual.fake_provider import (
    DeterministicFakeVisualProvider,
    FakeVisualFixture,
    FakeVisualOutcomeKind,
)
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.inputs import (
    DescriptorSource,
    ImageIngress,
    SealImageSetRequest,
    VisualInputStore,
    bind_visual_input_locator,
)
from vibecad.visual.provider import (
    VISUAL_PROVIDER_DESCRIPTOR,
    VISUAL_PROVIDER_IDENTITY,
    VisualProviderBinding,
    VisualProviderExecutionReceipt,
    VisualProviderRuntimeProfile,
    build_visual_provider_failure_result,
    build_visual_provider_invocation,
    build_visual_provider_success_result,
    visual_provider_input_digest,
)
from vibecad.visual.provider_images import (
    ProviderImageDetail,
    VisualProviderCapabilityProfile,
)
from vibecad.visual.reconstruction import (
    ClarificationKind,
    EvidenceBinding,
    ReconstructionProposal,
    ReconstructionStatus,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    clarification_question_for_claim,
    reconstruction_identity,
)
from vibecad.visual.service import (
    VisualReconstructionService,
    VisualServiceError,
    VisualServiceErrorCode,
)
from vibecad.visual.store import ReconstructionDraftStore
from vibecad.workflow.contracts import AcceptanceCriterion, AcceptanceKind, AcceptanceSpec
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

_RECONSTRUCTION_CREATE_KEY = "reconstruction_create_" + "1" * 32
_IMAGE_CREATE_KEY = "image_set_create_" + "2" * 32


def _budget() -> RuntimeBudget:
    return RuntimeBudget(
        max_elapsed_ms=1_000,
        max_memory_bytes=32 * 1024 * 1024,
        max_output_bytes=1024 * 1024,
    )


def _head() -> BaseHeadBinding:
    return BaseHeadBinding(
        project_id="project_" + "3" * 32,
        generation=4,
        revision_id="revision_" + "5" * 32,
        manifest_sha256="6" * 64,
    )


def _stores(tmp_path: Path):
    layout = ApplicationDataLayout.open(tmp_path.resolve() / "data")
    manager = ResourceLeaseManager(layout.locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    inputs = VisualInputStore(
        root=layout.visual_inputs,
        expected_root_identity=layout.identity_for(layout.visual_inputs),
        lease_manager=manager,
    )
    drafts = ReconstructionDraftStore(
        root=layout.reconstruction_drafts,
        expected_root_identity=layout.identity_for(layout.reconstruction_drafts),
        lease_manager=manager,
    )
    return inputs, drafts


def _sealed_image_set(
    tmp_path: Path,
    inputs: VisualInputStore,
    *,
    create_key: str = _IMAGE_CREATE_KEY,
    processing_authorization: ProcessingAuthorization = ProcessingAuthorization.LOCAL_ONLY,
):
    source = tmp_path / "source.png"
    Image.new("RGB", (16, 12), (20, 80, 140)).save(source, format="PNG")
    os.chmod(source, 0o600)
    request = SealImageSetRequest(
        create_key=create_key,
        inputs=(
            ImageIngress(
                view_role=ViewRole.FRONT,
                calibration_status=CalibrationStatus.UNKNOWN,
                declared_mime=ImageMime.PNG,
            ),
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=True,
        same_state=True,
        same_scale=True,
        processing_authorization=processing_authorization,
    )
    fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        descriptor = DescriptorSource(
            fd=fd,
            locator=bind_visual_input_locator(request, 0, os.fstat(fd)),
        )
        return inputs.seal(request, (descriptor,))
    finally:
        os.close(fd)


def _sealed_multi_image_set(
    tmp_path: Path,
    inputs: VisualInputStore,
    *,
    view_roles: tuple[ViewRole, ...],
    same_object: bool = True,
    same_state: bool = True,
    same_scale: bool = True,
):
    request = SealImageSetRequest(
        create_key="image_set_create_" + "9" * 32,
        inputs=tuple(
            ImageIngress(
                view_role=role,
                calibration_status=CalibrationStatus.UNKNOWN,
                declared_mime=ImageMime.PNG,
            )
            for role in view_roles
        ),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=(),
        same_object=same_object,
        same_state=same_state,
        same_scale=same_scale,
        processing_authorization=ProcessingAuthorization.LOCAL_ONLY,
    )
    descriptors: list[DescriptorSource] = []
    fds: list[int] = []
    try:
        for index, _role in enumerate(view_roles):
            source = tmp_path / f"source-{index}.png"
            Image.new("RGB", (16, 12), (20 + index, 80, 140)).save(source, format="PNG")
            os.chmod(source, 0o600)
            fd = os.open(source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
            fds.append(fd)
            descriptors.append(
                DescriptorSource(
                    fd=fd,
                    locator=bind_visual_input_locator(request, index, os.fstat(fd)),
                )
            )
        return inputs.seal(request, tuple(descriptors))
    finally:
        for fd in fds:
            os.close(fd)


def _invocation(image_set, *, generation: int = 1):
    reconstruction_id, _ = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)
    return build_visual_provider_invocation(
        reconstruction_id=reconstruction_id,
        generation=generation,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )


def _observation(invocation) -> VisualObservation:
    return VisualObservation(
        reconstruction_id=invocation.payload["reconstruction_id"],
        generation=invocation.payload["generation"],
        image_set_id=invocation.payload["image_set_id"],
        image_set_manifest_sha256=invocation.payload["image_set_manifest_sha256"],
        invocation_id=invocation.invocation_id,
        claims=(
            VisualClaim(
                name="overall.depth",
                status=VisualClaimStatus.CONFIRMED,
                source_indices=(0,),
                value=8,
                unit=VisualClaimUnit.MM,
                description="Fixture depth",
            ),
        ),
    )


def _question_observation(invocation) -> VisualObservation:
    claim = VisualClaim(
        name="overall.depth",
        status=VisualClaimStatus.ASSUMED,
        source_indices=(0,),
        value=8,
        unit=VisualClaimUnit.MM,
        description="Fixture depth",
    )
    question = clarification_question_for_claim(claim, "Confirm the assumed depth")
    assert question.kind is ClarificationKind.CONFIRM_ASSUMPTION
    return VisualObservation(
        reconstruction_id=invocation.payload["reconstruction_id"],
        generation=invocation.payload["generation"],
        image_set_id=invocation.payload["image_set_id"],
        image_set_manifest_sha256=invocation.payload["image_set_manifest_sha256"],
        invocation_id=invocation.invocation_id,
        claims=(claim,),
        questions=(question,),
    )


def _ir_id(kind: str, index: int) -> str:
    return f"ir_{kind}_{index:032x}"


def _proposal(observation: VisualObservation) -> ReconstructionProposal:
    claim = observation.claims[0]
    evidence_id = _ir_id("evidence", 1)
    parameter_id = _ir_id("parameter", 1)
    sketch_id = _ir_id("sketch", 1)
    design = ParametricDesignIR(
        id=_ir_id("design", 1),
        name="Visual plate",
        units=UnitSystem(),
        body=BodyDefinition(id=_ir_id("body", 1), name="Visual plate body"),
        evidence=(
            DesignEvidence(
                id=evidence_id,
                status=DesignEvidenceStatus.CONFIRMED,
                origin=DesignEvidenceOrigin.IMAGE,
                source_refs=(claim.id,),
                description="Evidence from the sealed image set",
            ),
        ),
        parameters=(
            DesignParameter(
                id=parameter_id,
                name="Depth",
                kind=ParameterKind.LENGTH,
                value=8,
                unit=DesignUnit.MM,
                evidence_ids=(evidence_id,),
                minimum=0.1,
                maximum=1000,
            ),
        ),
        datum_planes=(),
        sketches=(
            ParametricSketch(
                id=sketch_id,
                name="Circular profile",
                role=SketchRole.PROFILE,
                plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
                geometries=(
                    SketchGeometry(
                        id=_ir_id("geometry", 1),
                        kind=GeometryKind.CIRCLE,
                        dimensions={"cx_mm": 0, "cy_mm": 0, "radius_mm": 5},
                        evidence_ids=(evidence_id,),
                    ),
                ),
                constraints=(),
                evidence_ids=(evidence_id,),
            ),
        ),
        features=(
            PartDesignFeature(
                id=_ir_id("feature", 1),
                name="Pad",
                kind=FeatureKind.PAD,
                sketch_id=sketch_id,
                base_feature_id=None,
                parameters={"length": parameter_id},
                evidence_ids=(evidence_id,),
                extent=FeatureExtent.LENGTH,
            ),
        ),
    )
    return ReconstructionProposal(
        observation=observation,
        design=design,
        acceptance=AcceptanceSpec(
            id="visual-acceptance-v1",
            criteria=(
                AcceptanceCriterion(
                    id="depth-check",
                    kind=AcceptanceKind.GEOMETRY,
                    check="entity_parameter",
                    target="body",
                    expected=8,
                    tolerance=0.01,
                ),
            ),
        ),
        evidence_bindings=(EvidenceBinding(evidence_id=evidence_id, claim_ids=(claim.id,)),),
        clarification_answers=(),
        part_type="mounting_plate",
        summary="One editable circular plate reconstructed from visual evidence.",
    )


def _service(inputs, drafts, provider) -> VisualReconstructionService:
    return VisualReconstructionService(
        inputs=inputs,
        drafts=drafts,
        provider=VisualProviderBinding(provider=provider),
    )


def _create(service: VisualReconstructionService, image_set):
    return service.create(
        create_key=_RECONSTRUCTION_CREATE_KEY,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        base_head=_head(),
    )


class _CloudObservationTransport:
    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request, *, timeout_ms):
        self.calls += 1
        assert timeout_ms == 1_000
        return CloudVisualTransportOutcome(
            kind=CloudVisualOutcomeKind.SUCCEEDED,
            value=_observation(request.invocation),
            execution_receipt=VisualProviderExecutionReceipt(
                request_sha256=request.request_sha256,
                image_batch_sha256=request.image_batch.manifest_sha256,
                response_id_sha256="1" * 64,
                response_output_sha256="2" * 64,
                response_model=request.image_batch.profile.model,
                data_policy_profile=request.image_batch.profile.data_policy_profile,
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                transport_timeout_ms=timeout_ms,
            ),
        )


class _CloudFeatureTransport:
    __slots__ = ("calls",)

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, request, *, timeout_ms):
        self.calls += 1
        assert timeout_ms == 1_000
        observation = _observation(request.invocation)
        return CloudVisualTransportOutcome(
            kind=CloudVisualOutcomeKind.SUCCEEDED,
            value=observation,
            execution_receipt=VisualProviderExecutionReceipt(
                request_sha256=request.request_sha256,
                image_batch_sha256=request.image_batch.manifest_sha256,
                response_id_sha256="1" * 64,
                response_output_sha256="2" * 64,
                response_model=request.image_batch.profile.model,
                data_policy_profile=request.image_batch.profile.data_policy_profile,
                input_tokens=100,
                output_tokens=20,
                total_tokens=120,
                transport_timeout_ms=timeout_ms,
            ),
            feature_evidence=(
                ProviderFeatureEvidence(
                    local_feature_id="overall.depth.edge",
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


def _cloud_profiles():
    runtime_profile = VisualProviderRuntimeProfile(
        identity=RuntimeIdentity(family="visual", provider="candidate_cloud", version="1.0"),
        model="vision-model",
        model_version="2026-08-04",
        execution_profile="cloud_provider",
        network=True,
    )
    image_profile = VisualProviderCapabilityProfile(
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
        transport_timeout_ms=1_000,
    )
    return runtime_profile, image_profile


def test_process_local_review_input_uses_exact_sealed_bytes_without_provider_replay(
    tmp_path: Path,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(
        tmp_path,
        inputs,
        processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
    )
    runtime_profile, image_profile = _cloud_profiles()
    transport = _CloudFeatureTransport()
    provider = CloudVisualProvider(
        runtime_profile=runtime_profile,
        image_profile=image_profile,
        image_reader=inputs.read_provider_images_exact,
        transport=transport,
    )
    service = _service(inputs, drafts, provider)
    created = _create(service, image_set)
    completed = service.run(
        created.reconstruction_id,
        expected_generation=created.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    value = service.load_process_local_review_input(completed)

    assert value is not None
    assert value.image_set == image_set
    assert (
        value.normalized_images
        == inputs.read_provider_images_exact(
            image_set.id,
            image_set.manifest_sha256,
        )[1]
    )
    assert value.evidence.observation_id == completed.observation_ref.id
    assert value.evidence.features[0].local_feature_id == "overall.depth.edge"
    assert transport.calls == provider.transport_count == 1

    fresh_transport = _CloudFeatureTransport()
    fresh_service = _service(
        inputs,
        drafts,
        CloudVisualProvider(
            runtime_profile=runtime_profile,
            image_profile=image_profile,
            image_reader=inputs.read_provider_images_exact,
            transport=fresh_transport,
        ),
    )
    assert fresh_service.load_process_local_review_input(completed) is None
    assert fresh_transport.calls == 0


def test_cloud_authorization_selects_dynamic_provider_and_preserves_durable_identity(
    tmp_path: Path,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(
        tmp_path,
        inputs,
        processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
    )
    runtime_profile = VisualProviderRuntimeProfile(
        identity=RuntimeIdentity(family="visual", provider="candidate_cloud", version="1.0"),
        model="vision-model",
        model_version="2026-08-04",
        execution_profile="cloud_provider",
        network=True,
    )
    image_profile = VisualProviderCapabilityProfile(
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
        transport_timeout_ms=1_000,
    )
    transport = _CloudObservationTransport()
    provider = CloudVisualProvider(
        runtime_profile=runtime_profile,
        image_profile=image_profile,
        image_reader=inputs.read_provider_images_exact,
        transport=transport,
    )
    service = _service(inputs, drafts, provider)
    created = _create(service, image_set)

    completed = service.run(
        created.reconstruction_id,
        expected_generation=created.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert completed.status is ReconstructionStatus.READY
    assert transport.calls == provider.transport_count == 1
    assert len(completed.provider_invocations) == 1
    intent = completed.provider_invocations[0]
    assert intent.runtime == runtime_profile.identity
    assert intent.model == runtime_profile.model
    assert intent.model_version == runtime_profile.model_version
    assert intent.lifecycle is RuntimeLifecycleState.SUCCEEDED


class _PendingProbeProvider:
    __slots__ = ("_descriptor", "_probe", "_reconciles", "_starts")

    def __init__(self, probe) -> None:
        self._descriptor = VISUAL_PROVIDER_DESCRIPTOR
        self._probe = probe
        self._starts = 0
        self._reconciles = 0

    @property
    def runtime_descriptor(self):
        return self._descriptor

    @property
    def starts(self):
        return self._starts

    @property
    def reconciles(self):
        return self._reconciles

    def start(self, invocation):
        self._starts += 1
        self._probe(invocation)
        return RuntimeStatus(
            invocation_id=invocation.invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.PENDING,
        )

    def get_status(self, invocation_id):
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.PENDING,
        )

    def cancel(self, invocation_id, *, reason):
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.CANCELLED,
        )

    def reconcile(self, invocation_id):
        self._reconciles += 1
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.RUNNING,
        )

    def health(self, identity):
        return RuntimeHealth(runtime=identity, state=RuntimeHealthState.HEALTHY)

    def get_result(self, invocation_id):
        return None


class _MissingResultProvider(_PendingProbeProvider):
    __slots__ = ()

    def start(self, invocation):
        self._starts += 1
        self._probe(invocation)
        return RuntimeStatus(
            invocation_id=invocation.invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.SUCCEEDED,
        )

    def reconcile(self, invocation_id):
        self._reconciles += 1
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.SUCCEEDED,
        )


class _ExceptionProvider(_PendingProbeProvider):
    __slots__ = ()

    def start(self, invocation):
        self._starts += 1
        self._probe(invocation)
        raise RuntimeError("provider detail must not be persisted")

    def reconcile(self, invocation_id):
        self._reconciles += 1
        raise RuntimeError("provider detail must not be persisted")


class _CancelledProvider(_PendingProbeProvider):
    __slots__ = ("_outcome",)

    def __init__(self, probe) -> None:
        super().__init__(probe)
        self._outcome = None

    def start(self, invocation):
        self._starts += 1
        self._probe(invocation)
        failed = build_visual_provider_failure_result(
            invocation,
            RuntimeDiagnostic(
                code="fixture.cancelled",
                message="Fixture cancellation",
                retryable=False,
            ),
        )
        self._outcome = dataclasses.replace(
            failed,
            state=RuntimeLifecycleState.CANCELLED,
            diagnostics=(),
        )
        return RuntimeStatus(
            invocation_id=invocation.invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.CANCELLED,
        )

    def reconcile(self, invocation_id):
        self._reconciles += 1
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.CANCELLED,
        )

    def get_result(self, invocation_id):
        return self._outcome


class _RecoveringProvider(_PendingProbeProvider):
    __slots__ = ("_needs_input", "_outcome")

    def __init__(self, probe, *, needs_input: bool = False) -> None:
        super().__init__(probe)
        self._outcome = None
        self._needs_input = needs_input

    def start(self, invocation):
        self._starts += 1
        self._probe(invocation)
        self._outcome = build_visual_provider_success_result(
            invocation,
            (_question_observation(invocation) if self._needs_input else _observation(invocation)),
        )
        return RuntimeStatus(
            invocation_id=invocation.invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.UNKNOWN,
        )

    def reconcile(self, invocation_id):
        self._reconciles += 1
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=(
                RuntimeLifecycleState.RUNNING
                if self._reconciles == 1
                else RuntimeLifecycleState.SUCCEEDED
            ),
        )

    def get_result(self, invocation_id):
        return self._outcome


def test_create_binds_exact_sealed_local_manifest_and_success_is_durable(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    observation = _observation(invocation)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=observation,
            )
        }
    )
    service = _service(inputs, drafts, provider)

    ready = _create(service, image_set)
    completed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert completed.status is ReconstructionStatus.READY
    assert completed.generation == 2
    assert completed.observation_ref.contract_digest == observation.digest
    assert completed.provider_invocations[-1].lifecycle is RuntimeLifecycleState.SUCCEEDED
    assert completed.provider_invocations[-1].start_receipt_sha256 is not None
    assert completed.provider_invocations[-1].result_sha256 is not None
    assert completed.provider_invocations[-1].output_sha256 is not None
    assert service.get(ready.reconstruction_id) == completed

    with pytest.raises(VisualServiceError) as caught:
        service.run(
            ready.reconstruction_id,
            expected_generation=ready.generation,
            budget=_budget(),
            deadline_ms=2_000_000_000_000,
        )
    assert caught.value.code is VisualServiceErrorCode.CONFLICT
    assert provider.execution_count == 1

    with pytest.raises(VisualServiceError) as caught:
        service.create(
            create_key="reconstruction_create_" + "9" * 32,
            image_set_id=image_set.id,
            image_set_manifest_sha256="0" * 64,
            base_head=_head(),
        )
    assert caught.value.code is VisualServiceErrorCode.INVALID_INPUT


def test_intent_is_durable_before_start_and_observing_only_reconciles(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    reconstruction_id, _ = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)

    def probe(invocation) -> None:
        durable = drafts.load(reconstruction_id)
        assert durable.status is ReconstructionStatus.OBSERVING
        assert durable.provider_invocations[-1].invocation_id == invocation.invocation_id
        assert durable.provider_invocations[-1].lifecycle is None

    provider = _PendingProbeProvider(probe)
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    observing = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    reconciled = service.run(
        ready.reconstruction_id,
        expected_generation=observing.generation,
    )

    assert observing.status is ReconstructionStatus.OBSERVING
    assert reconciled.status is ReconstructionStatus.OBSERVING
    assert provider.starts == 1
    assert provider.reconciles == 1
    assert reconciled.provider_invocations[-1].start_receipt_sha256 == (
        observing.provider_invocations[-1].start_receipt_sha256
    )


def test_unknown_enters_recovery_and_recovery_never_restarts(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.UNKNOWN
            )
        }
    )
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    recovery = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    again = service.run(
        ready.reconstruction_id,
        expected_generation=recovery.generation,
    )

    assert recovery.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert again.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert provider.execution_count == 1
    assert again.provider_invocations[-1].invocation_id == invocation.invocation_id
    assert again.last_error.code == "provider.unknown"


@pytest.mark.parametrize("needs_input", [False, True])
def test_recovery_stays_reconcile_only_while_running_then_publishes_result(
    tmp_path: Path,
    needs_input: bool,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    reconstruction_id, _ = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)
    provider = _RecoveringProvider(
        lambda _invocation: drafts.load(reconstruction_id),
        needs_input=needs_input,
    )
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    recovery = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    still_recovering = _service(inputs, drafts, provider).run(
        ready.reconstruction_id,
        expected_generation=recovery.generation,
    )
    completed = _service(inputs, drafts, provider).run(
        ready.reconstruction_id,
        expected_generation=still_recovering.generation,
    )

    assert recovery.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert still_recovering.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert still_recovering.provider_invocations[-1].lifecycle is (RuntimeLifecycleState.RUNNING)
    assert completed.status is (
        ReconstructionStatus.NEEDS_INPUT if needs_input else ReconstructionStatus.READY
    )
    assert provider.starts == 1
    assert provider.reconciles == 2


@pytest.mark.parametrize("missing_result", [False, True])
def test_exception_and_missing_terminal_result_are_recovery_only(
    tmp_path: Path,
    missing_result: bool,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    reconstruction_id, _ = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)
    provider_type = _MissingResultProvider if missing_result else _ExceptionProvider
    provider = provider_type(lambda _invocation: drafts.load(reconstruction_id))
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    recovery = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )
    again = service.run(
        ready.reconstruction_id,
        expected_generation=recovery.generation,
    )

    assert recovery.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert again.status is ReconstructionStatus.RECOVERY_REQUIRED
    assert provider.starts == 1
    assert provider.reconciles == 1
    assert "detail" not in str(again.last_error.to_mapping())


@pytest.mark.parametrize("needs_input", [False, True])
def test_successful_observation_selects_ready_or_needs_input(
    tmp_path: Path,
    needs_input: bool,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    observation = _question_observation(invocation) if needs_input else _observation(invocation)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=observation,
            )
        }
    )
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    completed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    expected = ReconstructionStatus.NEEDS_INPUT if needs_input else ReconstructionStatus.READY
    assert completed.status is expected


def test_definitive_failure_is_durable_failed_state(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.FAILURE,
                diagnostic=RuntimeDiagnostic(
                    code="fixture.failure",
                    message="A bounded fixture failure",
                    retryable=False,
                ),
            )
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

    assert failed.status is ReconstructionStatus.FAILED
    assert failed.last_error.code == "provider.failed"
    assert failed.provider_invocations[-1].lifecycle is RuntimeLifecycleState.FAILED
    assert failed.provider_invocations[-1].result_sha256 is not None


def test_definitive_cancellation_is_durable_failed_state(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    reconstruction_id, _ = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)
    provider = _CancelledProvider(lambda _invocation: drafts.load(reconstruction_id))
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    cancelled = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert cancelled.status is ReconstructionStatus.FAILED
    assert cancelled.last_error.code == "provider.cancelled"
    assert cancelled.provider_invocations[-1].lifecycle is RuntimeLifecycleState.CANCELLED
    assert cancelled.provider_invocations[-1].result_sha256 is not None


def test_successful_proposal_publishes_both_immutable_payloads(tmp_path: Path) -> None:
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
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    completed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert completed.status is ReconstructionStatus.PROPOSED
    assert completed.observation_ref.contract_digest == proposal.observation.digest
    assert completed.proposal_ref.contract_digest == proposal.digest
    assert completed.provider_invocations[-1].output_sha256 is not None


def test_provider_source_index_must_exist_in_the_sealed_image_set(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_image_set(tmp_path, inputs)
    invocation = _invocation(image_set)
    observation = VisualObservation(
        reconstruction_id=invocation.payload["reconstruction_id"],
        generation=invocation.payload["generation"],
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        invocation_id=invocation.invocation_id,
        claims=(
            VisualClaim(
                name="overall.depth",
                status=VisualClaimStatus.CONFIRMED,
                source_indices=(1,),
                value=8,
                unit=VisualClaimUnit.MM,
            ),
        ),
    )
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=observation,
            )
        }
    )
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    completed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert completed.status is ReconstructionStatus.FAILED
    assert completed.last_error.code == "provider.source_binding_invalid"
    assert completed.observation_ref is None
    assert completed.provider_invocations[-1].lifecycle is RuntimeLifecycleState.FAILED


@pytest.mark.parametrize(
    ("view_roles", "same_object", "same_state", "same_scale"),
    [
        ((ViewRole.FRONT, ViewRole.FRONT), True, True, True),
        ((ViewRole.FRONT, ViewRole.RIGHT), False, True, True),
        ((ViewRole.FRONT, ViewRole.RIGHT), True, False, True),
        ((ViewRole.FRONT, ViewRole.RIGHT), True, True, False),
        ((ViewRole.FRONT, ViewRole.UNKNOWN), True, True, True),
    ],
)
def test_cross_view_claim_requires_distinct_known_roles_and_one_object_state_scale(
    tmp_path: Path,
    view_roles: tuple[ViewRole, ...],
    same_object: bool,
    same_state: bool,
    same_scale: bool,
) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_multi_image_set(
        tmp_path,
        inputs,
        view_roles=view_roles,
        same_object=same_object,
        same_state=same_state,
        same_scale=same_scale,
    )
    invocation = _invocation(image_set)
    observation = VisualObservation(
        reconstruction_id=invocation.payload["reconstruction_id"],
        generation=invocation.payload["generation"],
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        invocation_id=invocation.invocation_id,
        claims=(
            VisualClaim(
                name="overall.depth",
                status=VisualClaimStatus.CROSS_VIEW_DERIVED,
                source_indices=(0, 1),
                value=8,
                unit=VisualClaimUnit.MM,
            ),
        ),
    )
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=observation,
            )
        }
    )
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    completed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert completed.status is ReconstructionStatus.FAILED
    assert completed.last_error.code == "provider.source_binding_invalid"


def test_cross_view_claim_accepts_distinct_bound_complementary_views(tmp_path: Path) -> None:
    inputs, drafts = _stores(tmp_path)
    image_set = _sealed_multi_image_set(
        tmp_path,
        inputs,
        view_roles=(ViewRole.FRONT, ViewRole.RIGHT),
    )
    invocation = _invocation(image_set)
    observation = VisualObservation(
        reconstruction_id=invocation.payload["reconstruction_id"],
        generation=invocation.payload["generation"],
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        invocation_id=invocation.invocation_id,
        claims=(
            VisualClaim(
                name="overall.depth",
                status=VisualClaimStatus.CROSS_VIEW_DERIVED,
                source_indices=(0, 1),
                value=8,
                unit=VisualClaimUnit.MM,
            ),
        ),
    )
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): FakeVisualFixture(
                kind=FakeVisualOutcomeKind.OBSERVATION,
                value=observation,
            )
        }
    )
    service = _service(inputs, drafts, provider)
    ready = _create(service, image_set)

    completed = service.run(
        ready.reconstruction_id,
        expected_generation=ready.generation,
        budget=_budget(),
        deadline_ms=2_000_000_000_000,
    )

    assert completed.status is ReconstructionStatus.READY
    assert completed.observation_ref.contract_digest == observation.digest
