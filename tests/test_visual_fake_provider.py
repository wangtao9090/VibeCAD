"""Focused tests for the local deterministic visual provider."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

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
    RuntimeHealthState,
    RuntimeIdentity,
    RuntimeLifecycleState,
)
from vibecad.visual.fake_provider import (
    DeterministicFakeVisualProvider,
    FakeVisualFixture,
    FakeVisualOutcomeKind,
    FakeVisualProviderError,
    FakeVisualProviderErrorCode,
)
from vibecad.visual.provider import (
    VISUAL_PROVIDER_IDENTITY,
    VisualProviderBinding,
    VisualProviderError,
    VisualProviderErrorCode,
    VisualProviderOutput,
    build_visual_provider_invocation,
    validate_visual_provider_result,
    visual_provider_input_digest,
)
from vibecad.visual.reconstruction import (
    ClarificationAnswer,
    EvidenceBinding,
    ReconstructionProposal,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    clarification_answer_for_question,
    clarification_question_for_claim,
    reconstruction_identity,
)
from vibecad.workflow.contracts import AcceptanceCriterion, AcceptanceKind, AcceptanceSpec

_CREATE_KEY = "reconstruction_create_" + "1" * 32
_RECONSTRUCTION_ID, _ = reconstruction_identity(_CREATE_KEY)
_IMAGE_SET_ID = "image_set_" + "2" * 32
_MANIFEST_DIGEST = "a" * 64


def _ir_id(kind: str, index: int) -> str:
    return f"ir_{kind}_{index:032x}"


def _budget() -> RuntimeBudget:
    return RuntimeBudget(
        max_elapsed_ms=1_000,
        max_memory_bytes=32 * 1024 * 1024,
        max_output_bytes=1024 * 1024,
    )


def _answer(name: str, response: bool | int | float | str) -> ClarificationAnswer:
    claim = VisualClaim(
        name=name,
        status=VisualClaimStatus.UNKNOWN,
        source_indices=(0,),
        value=None,
    )
    question = clarification_question_for_claim(claim, f"Resolve {name}.")
    return clarification_answer_for_question(question, response)


def _invocation(*, generation: int, answer_digests: tuple[str, ...] = ()):
    return build_visual_provider_invocation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=generation,
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
        clarification_answer_digests=answer_digests,
        budget=_budget(),
        deadline_ms=2_000,
    )


def _observation(invocation) -> VisualObservation:
    return VisualObservation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=invocation.payload["generation"],
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


def _design(claim: VisualClaim) -> ParametricDesignIR:
    evidence_id = _ir_id("evidence", 1)
    parameter_id = _ir_id("parameter", 1)
    sketch_id = _ir_id("sketch", 1)
    return ParametricDesignIR(
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


def _proposal(
    observation: VisualObservation,
    *,
    answers: tuple[ClarificationAnswer, ...] = (),
) -> ReconstructionProposal:
    claim = observation.claims[0]
    design = _design(claim)
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
        evidence_bindings=(
            EvidenceBinding(evidence_id=design.evidence[0].id, claim_ids=(claim.id,)),
        ),
        clarification_answers=answers,
        part_type="mounting_plate",
        summary="One editable circular plate reconstructed from visual evidence.",
    )


def _fixture(
    kind: FakeVisualOutcomeKind,
    *,
    value: VisualObservation | ReconstructionProposal | None = None,
    diagnostic: RuntimeDiagnostic | None = None,
) -> FakeVisualFixture:
    return FakeVisualFixture(kind=kind, value=value, diagnostic=diagnostic)


def _assumed_proposal(
    invocation,
) -> tuple[ReconstructionProposal, ClarificationAnswer]:
    claim = VisualClaim(
        name="overall.depth",
        status=VisualClaimStatus.ASSUMED,
        source_indices=(0,),
        value=8,
        unit=VisualClaimUnit.MM,
        description="Fixture depth",
    )
    question = clarification_question_for_claim(claim, "Confirm the assumed depth.")
    answer = clarification_answer_for_question(question, True)
    observation = VisualObservation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=invocation.payload["generation"],
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
        invocation_id=invocation.invocation_id,
        claims=(claim,),
        questions=(question,),
    )
    return _proposal(observation, answers=(answer,)), answer


def test_observation_and_proposal_fixtures_return_exact_validated_results() -> None:
    observation_invocation = _invocation(generation=1)
    proposal_invocation = _invocation(generation=2)
    observation = _observation(observation_invocation)
    proposal = _proposal(_observation(proposal_invocation))
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(observation_invocation): _fixture(
                FakeVisualOutcomeKind.OBSERVATION,
                value=observation,
            ),
            visual_provider_input_digest(proposal_invocation): _fixture(
                FakeVisualOutcomeKind.PROPOSAL,
                value=proposal,
            ),
        }
    )
    binding = VisualProviderBinding(provider=provider)

    for invocation, expected in (
        (observation_invocation, observation),
        (proposal_invocation, proposal),
    ):
        status = provider.start(invocation)
        result = binding.retrieve_result(invocation)

        assert status.state is RuntimeLifecycleState.SUCCEEDED
        assert result is not None
        assert validate_visual_provider_result(invocation, result) is result
        assert VisualProviderOutput.from_mapping(result.output).value == expected

    assert provider.execution_count == 2
    assert provider.known_invocation_count == 2


def test_definitive_failure_and_unknown_are_distinct_nonwaiting_outcomes() -> None:
    failure_invocation = _invocation(generation=3)
    unknown_invocation = _invocation(generation=4)
    diagnostic = RuntimeDiagnostic(
        code="provider.fixture_failure",
        message="Deterministic fixture failure.",
        retryable=True,
    )
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(failure_invocation): _fixture(
                FakeVisualOutcomeKind.FAILURE,
                diagnostic=diagnostic,
            ),
            visual_provider_input_digest(unknown_invocation): _fixture(
                FakeVisualOutcomeKind.UNKNOWN
            ),
        }
    )
    binding = VisualProviderBinding(provider=provider)

    failed = provider.start(failure_invocation)
    failed_result = binding.retrieve_result(failure_invocation)
    assert failed.state is RuntimeLifecycleState.FAILED
    assert failed.diagnostics == (diagnostic,)
    assert failed_result is not None
    assert failed_result.state is RuntimeLifecycleState.FAILED
    assert failed_result.diagnostics == (diagnostic,)

    unknown = provider.start(unknown_invocation)
    assert unknown.state is RuntimeLifecycleState.UNKNOWN
    assert binding.retrieve_result(unknown_invocation) is None
    assert binding.retrieve_result(unknown_invocation) is None
    assert provider.get_status(unknown_invocation.invocation_id).state is (
        RuntimeLifecycleState.UNKNOWN
    )
    assert provider.reconcile(unknown_invocation.invocation_id).state is (
        RuntimeLifecycleState.UNKNOWN
    )
    assert provider.cancel(unknown_invocation.invocation_id, reason="stop").state is (
        RuntimeLifecycleState.UNKNOWN
    )


def test_duplicate_start_is_idempotent_and_changed_digest_conflicts() -> None:
    invocation = _invocation(generation=5)
    changed = _invocation(
        generation=5,
        answer_digests=(_answer("overall.width", 12).digest,),
    )
    assert invocation.invocation_id == changed.invocation_id
    assert visual_provider_input_digest(invocation) != visual_provider_input_digest(changed)

    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): _fixture(
                FakeVisualOutcomeKind.OBSERVATION,
                value=_observation(invocation),
            )
        }
    )
    first = provider.start(invocation)
    second = provider.start(invocation)

    assert second is first
    assert provider.execution_count == 1
    assert provider.known_invocation_count == 1
    with pytest.raises(FakeVisualProviderError) as caught:
        provider.start(changed)
    assert caught.value.code is FakeVisualProviderErrorCode.CONFLICT
    assert provider.execution_count == 1


def test_provider_cannot_invent_an_assumption_confirmation_for_a_proposal() -> None:
    invocation = _invocation(generation=8)
    proposal, _answer_value = _assumed_proposal(invocation)
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): _fixture(
                FakeVisualOutcomeKind.PROPOSAL,
                value=proposal,
            )
        }
    )

    with pytest.raises(VisualProviderError) as caught:
        provider.start(invocation)

    assert caught.value.code is VisualProviderErrorCode.RESULT_MISMATCH
    assert caught.value.subject == "clarification_answers"
    assert provider.execution_count == 0
    assert provider.known_invocation_count == 0


def test_proposal_accepts_only_an_answer_digest_bound_by_the_invocation() -> None:
    seed = _invocation(generation=9)
    proposal, answer = _assumed_proposal(seed)
    invocation = _invocation(generation=9, answer_digests=(answer.digest,))
    assert invocation.invocation_id == seed.invocation_id
    provider = DeterministicFakeVisualProvider(
        {
            visual_provider_input_digest(invocation): _fixture(
                FakeVisualOutcomeKind.PROPOSAL,
                value=proposal,
            )
        }
    )

    assert provider.start(invocation).state is RuntimeLifecycleState.SUCCEEDED
    result = provider.get_result(invocation.invocation_id)
    assert result is not None
    assert validate_visual_provider_result(invocation, result) is result


def test_missing_fixture_does_not_publish_partial_invocation_state() -> None:
    provider = DeterministicFakeVisualProvider({})

    with pytest.raises(FakeVisualProviderError) as caught:
        provider.start(_invocation(generation=6))

    assert caught.value.code is FakeVisualProviderErrorCode.MISSING_FIXTURE
    assert provider.execution_count == 0
    assert provider.known_invocation_count == 0


def test_restart_reconcile_is_unknown_and_never_replays_start() -> None:
    invocation = _invocation(generation=7)
    fixtures = {
        visual_provider_input_digest(invocation): _fixture(
            FakeVisualOutcomeKind.OBSERVATION,
            value=_observation(invocation),
        )
    }
    first = DeterministicFakeVisualProvider(fixtures)
    assert first.start(invocation).state is RuntimeLifecycleState.SUCCEEDED
    assert first.execution_count == 1

    restarted = DeterministicFakeVisualProvider(fixtures)
    assert restarted.reconcile(invocation.invocation_id).state is RuntimeLifecycleState.UNKNOWN
    assert restarted.get_status(invocation.invocation_id).state is RuntimeLifecycleState.UNKNOWN
    assert restarted.get_result(invocation.invocation_id) is None
    assert restarted.execution_count == 0
    assert restarted.known_invocation_count == 0


def test_fixture_shape_catalog_budget_health_and_inputs_fail_closed() -> None:
    diagnostic = RuntimeDiagnostic(code="fixture.failed", message="Failed.")
    invalid_fixtures = (
        {"not-a-digest": _fixture(FakeVisualOutcomeKind.UNKNOWN)},
        {"a" * 64: object()},
        {f"{index:064x}": _fixture(FakeVisualOutcomeKind.UNKNOWN) for index in range(129)},
    )
    for fixtures in invalid_fixtures:
        with pytest.raises(FakeVisualProviderError) as caught:
            DeterministicFakeVisualProvider(fixtures)  # type: ignore[arg-type]
        assert caught.value.code is FakeVisualProviderErrorCode.INVALID_FIXTURES

    for fixture in (
        lambda: _fixture(FakeVisualOutcomeKind.OBSERVATION),
        lambda: _fixture(FakeVisualOutcomeKind.PROPOSAL),
        lambda: _fixture(FakeVisualOutcomeKind.FAILURE),
        lambda: _fixture(FakeVisualOutcomeKind.UNKNOWN, diagnostic=diagnostic),
    ):
        with pytest.raises(FakeVisualProviderError) as caught:
            fixture()
        assert caught.value.code is FakeVisualProviderErrorCode.INVALID_FIXTURES

    provider = DeterministicFakeVisualProvider({})
    assert provider.health(VISUAL_PROVIDER_IDENTITY).state is RuntimeHealthState.HEALTHY
    with pytest.raises(FakeVisualProviderError) as caught:
        provider.health(RuntimeIdentity(family="visual", provider="other", version="1.0"))
    assert caught.value.code is FakeVisualProviderErrorCode.IDENTITY_MISMATCH
    with pytest.raises(FakeVisualProviderError) as caught:
        provider.reconcile("not-an-invocation")
    assert caught.value.code is FakeVisualProviderErrorCode.INVALID_INVOCATION_ID


def test_constructor_and_module_have_no_external_or_application_authority_seams() -> None:
    import vibecad.visual.fake_provider as fake_module

    assert set(inspect.signature(DeterministicFakeVisualProvider.__init__).parameters) == {
        "self",
        "fixtures",
    }
    assert {field.name for field in dataclasses.fields(FakeVisualFixture)} == {
        "kind",
        "value",
        "diagnostic",
    }
    provider = DeterministicFakeVisualProvider({})
    VisualProviderBinding(provider=provider)

    path = Path(fake_module.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)

    assert not imports.intersection(
        {
            "http.client",
            "httpx",
            "requests",
            "socket",
            "urllib",
            "urllib.request",
            "vibecad.application.agent",
            "vibecad.interaction.storage",
            "vibecad.tasks.store",
        }
    )
