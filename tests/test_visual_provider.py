"""Focused tests for visual-only generic runtime composition."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from vibecad.runtime.contracts import (
    RuntimeBudget,
    RuntimeDescriptor,
    RuntimeDiagnostic,
    RuntimeHealth,
    RuntimeHealthState,
    RuntimeIdentity,
    RuntimeInvocation,
    RuntimeLifecycleState,
    RuntimeResult,
    RuntimeStatus,
)
from vibecad.visual.provider import (
    VISUAL_INTERNAL_CORRELATION_SEMANTICS,
    VISUAL_OBSERVE_V1,
    VISUAL_PROVIDER_DESCRIPTOR,
    VISUAL_PROVIDER_EXECUTION_PROFILE,
    VISUAL_PROVIDER_IDENTITY,
    VISUAL_PROVIDER_MODEL,
    VISUAL_PROVIDER_MODEL_VERSION,
    VISUAL_PROVIDER_RUNTIME_PROFILE,
    VisualProviderBinding,
    VisualProviderError,
    VisualProviderErrorCode,
    VisualProviderExecutionReceipt,
    VisualProviderOutput,
    VisualProviderRuntimeProfile,
    build_visual_provider_failure_result,
    build_visual_provider_invocation,
    build_visual_provider_success_result,
    validate_visual_provider_result,
    visual_provider_input_digest,
    visual_provider_output_digest,
    visual_runtime_correlation_id,
)
from vibecad.visual.reconstruction import (
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    reconstruction_identity,
)

_CREATE_KEY = "reconstruction_create_" + "1" * 32
_RECONSTRUCTION_ID, _ = reconstruction_identity(_CREATE_KEY)
_IMAGE_SET_ID = "image_set_" + "2" * 32
_MANIFEST_DIGEST = "a" * 64


def _budget() -> RuntimeBudget:
    return RuntimeBudget(
        max_elapsed_ms=1_000,
        max_memory_bytes=32 * 1024 * 1024,
        max_output_bytes=1024 * 1024,
    )


def _invocation(*, clarification_answer_digests: tuple[str, ...] = ()) -> RuntimeInvocation:
    return build_visual_provider_invocation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=1,
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
        clarification_answer_digests=clarification_answer_digests,
        budget=_budget(),
        deadline_ms=2_000,
    )


def _observation(invocation: RuntimeInvocation) -> VisualObservation:
    return VisualObservation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=1,
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
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


def _execution_receipt() -> VisualProviderExecutionReceipt:
    return VisualProviderExecutionReceipt(
        request_sha256="1" * 64,
        image_batch_sha256="2" * 64,
        response_id_sha256="3" * 64,
        response_output_sha256="4" * 64,
        response_model="vision-model",
        data_policy_profile="personal-default",
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        transport_timeout_ms=120_000,
    )


class _Provider:
    __slots__ = ("_control_calls", "_descriptor", "_result", "_result_calls")

    def __init__(
        self,
        result: RuntimeResult | None,
        descriptor: RuntimeDescriptor = VISUAL_PROVIDER_DESCRIPTOR,
    ) -> None:
        self._descriptor = descriptor
        self._result = result
        self._result_calls = 0
        self._control_calls = 0

    @property
    def runtime_descriptor(self):
        return self._descriptor

    @property
    def result_calls(self) -> int:
        return self._result_calls

    @property
    def control_calls(self) -> int:
        return self._control_calls

    def start(self, invocation):
        self._control_calls += 1
        return RuntimeStatus(
            invocation_id=invocation.invocation_id,
            runtime=invocation.runtime,
            state=RuntimeLifecycleState.PENDING,
        )

    def get_status(self, invocation_id):
        self._control_calls += 1
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.UNKNOWN,
        )

    def cancel(self, invocation_id, *, reason):
        self._control_calls += 1
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.CANCELLED,
        )

    def reconcile(self, invocation_id):
        self._control_calls += 1
        return RuntimeStatus(
            invocation_id=invocation_id,
            runtime=VISUAL_PROVIDER_IDENTITY,
            state=RuntimeLifecycleState.UNKNOWN,
        )

    def health(self, identity):
        self._control_calls += 1
        return RuntimeHealth(runtime=identity, state=RuntimeHealthState.HEALTHY)

    def get_result(self, invocation_id):
        self._result_calls += 1
        return self._result


def test_exact_descriptor_is_non_cad_local_model_and_generic_only() -> None:
    descriptor = VISUAL_PROVIDER_DESCRIPTOR

    assert descriptor.identity == VISUAL_PROVIDER_IDENTITY
    assert descriptor.identity.family == "visual"
    assert descriptor.identity.family != "cad"
    assert descriptor.capabilities == (VISUAL_OBSERVE_V1,)
    assert descriptor.execution_profiles == (VISUAL_PROVIDER_EXECUTION_PROFILE,)
    assert dict(descriptor.metadata) == {
        "correlation_semantics": VISUAL_INTERNAL_CORRELATION_SEMANTICS,
        "model": VISUAL_PROVIDER_MODEL,
        "model_version": VISUAL_PROVIDER_MODEL_VERSION,
        "network": False,
    }

    binding = VisualProviderBinding(provider=_Provider(None))
    assert binding.descriptor is descriptor
    assert binding.runtime_profile == VISUAL_PROVIDER_RUNTIME_PROFILE
    assert binding.registry.identities == (VISUAL_PROVIDER_IDENTITY,)
    assert binding.control is binding.results is binding.provider


def test_invocation_records_internal_visual_correlation_and_stable_input_digest() -> None:
    first = _invocation(clarification_answer_digests=("c" * 64, "b" * 64))
    reordered = _invocation(clarification_answer_digests=("b" * 64, "c" * 64))

    assert first == reordered
    assert first.owner_id == _RECONSTRUCTION_ID
    assert first.task_id == visual_runtime_correlation_id(_RECONSTRUCTION_ID, 1)
    assert not first.task_id.startswith("task_")
    assert first.payload["correlation_semantics"] == VISUAL_INTERNAL_CORRELATION_SEMANTICS
    assert first.payload["correlation_id"] == first.task_id
    assert first.payload["network"] is False
    assert first.payload["model"] == VISUAL_PROVIDER_MODEL
    assert first.payload["model_version"] == VISUAL_PROVIDER_MODEL_VERSION
    assert visual_provider_input_digest(first) == first.payload["input_digest"]
    assert first.input_artifacts[0].digest == _MANIFEST_DIGEST


def test_input_digest_binds_budget_deadline_answers_and_internal_correlation() -> None:
    invocation = _invocation()
    changed_answers = _invocation(clarification_answer_digests=("b" * 64,))

    assert invocation.invocation_id == changed_answers.invocation_id
    assert visual_provider_input_digest(invocation) != visual_provider_input_digest(changed_answers)

    for changed in (
        dataclasses.replace(invocation, deadline_ms=2_001),
        dataclasses.replace(invocation, task_id="task_not_allowed"),
        dataclasses.replace(invocation, execution_profile="interactive"),
    ):
        with pytest.raises(VisualProviderError) as caught:
            visual_provider_input_digest(changed)
        assert caught.value.code is VisualProviderErrorCode.INVALID_INVOCATION


def test_success_output_has_a_domain_separated_digest_and_exact_runtime_binding() -> None:
    invocation = _invocation()
    observation = _observation(invocation)
    result = build_visual_provider_success_result(invocation, observation)
    output = VisualProviderOutput.from_mapping(result.output)

    assert output.value == observation
    assert output.input_digest == visual_provider_input_digest(invocation)
    assert visual_provider_output_digest(output) == output.output_digest
    assert output.output_digest != observation.digest
    assert result.artifacts[0].artifact_id == observation.id
    assert result.artifacts[0].digest == output.output_digest
    assert result.provenance is not None
    assert result.provenance.details["input_digest"] == output.input_digest
    assert result.provenance.details["output_digest"] == output.output_digest
    assert result.provenance.details["network"] is False
    assert validate_visual_provider_result(invocation, result) is result

    tampered = output.to_mapping()
    tampered["output_digest"] = "b" * 64
    with pytest.raises(VisualProviderError) as caught:
        VisualProviderOutput.from_mapping(tampered)
    assert caught.value.subject == "output_digest"

    other_invocation = build_visual_provider_invocation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=2,
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
        budget=_budget(),
        deadline_ms=2_000,
    )
    with pytest.raises(VisualProviderError) as caught:
        build_visual_provider_success_result(other_invocation, observation)
    assert caught.value.subject == "visual_correlation"


def test_result_retrieval_is_one_read_non_waiting_idempotent_and_control_free() -> None:
    invocation = _invocation()
    result = build_visual_provider_success_result(invocation, _observation(invocation))
    provider = _Provider(result)
    binding = VisualProviderBinding(provider=provider)

    assert binding.retrieve_result(invocation) is result
    assert binding.retrieve_result(invocation) is result
    assert provider.result_calls == 2
    assert provider.control_calls == 0

    pending = _Provider(None)
    pending_binding = VisualProviderBinding(provider=pending)
    assert pending_binding.retrieve_result(invocation) is None
    assert pending_binding.retrieve_result(invocation) is None
    assert pending.result_calls == 2
    assert pending.control_calls == 0


def test_retrieval_rejects_mismatched_result_and_accepts_definitive_failure() -> None:
    invocation = _invocation()
    result = build_visual_provider_success_result(invocation, _observation(invocation))
    forged_artifact = dataclasses.replace(result.artifacts[0], digest="b" * 64)
    forged = dataclasses.replace(result, artifacts=(forged_artifact,))

    with pytest.raises(VisualProviderError) as caught:
        VisualProviderBinding(provider=_Provider(forged)).retrieve_result(invocation)
    assert caught.value.code is VisualProviderErrorCode.RESULT_MISMATCH

    failure = build_visual_provider_failure_result(
        invocation,
        RuntimeDiagnostic(
            code="provider.fixture_failure",
            message="Deterministic fixture failure.",
            retryable=True,
        ),
    )
    assert validate_visual_provider_result(invocation, failure) is failure
    assert VisualProviderBinding(provider=_Provider(failure)).retrieve_result(invocation) is failure


@pytest.mark.parametrize(
    "public_name",
    (
        "store",
        "_store",
        "resultStore",
        "lease_manager",
        "_lease_manager",
        "taskState",
        "revision_writer",
        "acceptDraft",
        "commit_result",
        "advanceHead",
        "rejectProposal",
        "reviewTask",
        "publicTool",
    ),
)
def test_authority_is_rejected_before_provider_metadata_is_read(public_name: str) -> None:
    descriptor_reads = 0

    def read_descriptor(self):
        nonlocal descriptor_reads
        descriptor_reads += 1
        return VISUAL_PROVIDER_DESCRIPTOR

    authority_type = type(
        "AuthorityProvider",
        (_Provider,),
        {
            "runtime_descriptor": property(read_descriptor),
            public_name: lambda self: None,
        },
    )

    with pytest.raises(VisualProviderError) as caught:
        VisualProviderBinding(provider=authority_type(None))

    assert caught.value.code is VisualProviderErrorCode.FORBIDDEN_AUTHORITY
    assert descriptor_reads == 0


def test_descriptor_and_provider_shape_are_exact_and_fail_closed() -> None:
    networked = RuntimeDescriptor(
        identity=VISUAL_PROVIDER_IDENTITY,
        capabilities=(VISUAL_OBSERVE_V1,),
        execution_profiles=(VISUAL_PROVIDER_EXECUTION_PROFILE,),
        metadata={
            "model": VISUAL_PROVIDER_MODEL,
            "model_version": VISUAL_PROVIDER_MODEL_VERSION,
            "network": True,
            "correlation_semantics": VISUAL_INTERNAL_CORRELATION_SEMANTICS,
        },
    )
    with pytest.raises(VisualProviderError) as caught:
        VisualProviderBinding(provider=_Provider(None, networked))
    assert caught.value.code is VisualProviderErrorCode.DESCRIPTOR_MISMATCH

    class WrongResultSignature(_Provider):
        def get_result(self, invocation_id, *, wait=False):
            raise AssertionError("must not be called")

    with pytest.raises(VisualProviderError) as caught:
        VisualProviderBinding(provider=WrongResultSignature(None))
    assert caught.value.code is VisualProviderErrorCode.INVALID_PROVIDER


def test_strict_cloud_runtime_profile_is_admitted_without_cad_authority() -> None:
    profile = VisualProviderRuntimeProfile(
        identity=RuntimeIdentity(family="visual", provider="candidate_cloud", version="1.0"),
        model="vision-model",
        model_version="2026-08-04",
        execution_profile="cloud_provider",
        network=True,
    )

    binding = VisualProviderBinding(provider=_Provider(None, profile.descriptor))

    assert binding.runtime_profile == profile
    assert binding.descriptor.metadata["network"] is True
    assert binding.registry.identities == (profile.identity,)


def test_cloud_success_requires_exact_execution_evidence_and_local_forbids_it() -> None:
    profile = VisualProviderRuntimeProfile(
        identity=RuntimeIdentity(family="visual", provider="candidate_cloud", version="1.0"),
        model="vision-model",
        model_version="2026-08-04",
        execution_profile="cloud_provider",
        network=True,
    )
    cloud_invocation = build_visual_provider_invocation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=1,
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
        budget=_budget(),
        deadline_ms=2_000,
        runtime_profile=profile,
    )
    observation = _observation(cloud_invocation)

    with pytest.raises(VisualProviderError):
        build_visual_provider_success_result(
            cloud_invocation,
            observation,
            runtime_profile=profile,
        )
    result = build_visual_provider_success_result(
        cloud_invocation,
        observation,
        runtime_profile=profile,
        execution_receipt=_execution_receipt(),
    )
    assert (
        validate_visual_provider_result(
            cloud_invocation,
            result,
            runtime_profile=profile,
        )
        is result
    )

    with pytest.raises(VisualProviderError):
        build_visual_provider_success_result(
            _invocation(),
            _observation(_invocation()),
            execution_receipt=_execution_receipt(),
        )


def test_provider_module_has_no_cad_adapter_storage_or_network_imports() -> None:
    import vibecad.visual.provider as provider_module

    path = Path(provider_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)

    assert "vibecad.interaction.cad_runtime" not in imports
    assert not imports.intersection(
        {"http.client", "httpx", "requests", "socket", "urllib", "urllib.request"}
    )
    assert set(inspect.signature(build_visual_provider_invocation).parameters) == {
        "reconstruction_id",
        "generation",
        "image_set_id",
        "image_set_manifest_sha256",
        "clarification_answer_digests",
        "budget",
        "deadline_ms",
        "runtime_profile",
    }
