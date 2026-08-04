from __future__ import annotations

import json

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
from vibecad.visual.contracts import ViewRole
from vibecad.visual.reconstruction import (
    MAX_RECONSTRUCTION_PROPOSAL_BYTES,
    MAX_VISUAL_OBSERVATION_BYTES,
    ClarificationAnswer,
    ClarificationKind,
    ClarificationQuestion,
    EvidenceBinding,
    ReconstructionContractError,
    ReconstructionContractErrorCode,
    ReconstructionNextAction,
    ReconstructionProposal,
    ReconstructionStatus,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    clarification_answer_for_question,
    clarification_question_for_claim,
    decode_reconstruction_proposal,
    decode_visual_observation,
    encode_reconstruction_proposal,
    encode_visual_observation,
    next_action_for_status,
    reconstruction_identity,
    visual_invocation_identity,
)
from vibecad.workflow.contracts import AcceptanceCriterion, AcceptanceKind, AcceptanceSpec

_MANIFEST_DIGEST = "a" * 64
_CREATE_KEY = "reconstruction_create_" + "1" * 32


def _ir_id(kind: str, index: int) -> str:
    return f"ir_{kind}_{index:032x}"


def _claim(
    *,
    status: VisualClaimStatus = VisualClaimStatus.CONFIRMED,
    name: str = "overall.depth",
    value: object = 8,
    blocking: bool = False,
    source_indices: tuple[int, ...] | None = None,
) -> VisualClaim:
    return VisualClaim(
        name=name,
        status=status,
        source_indices=((0, 1) if status is VisualClaimStatus.CROSS_VIEW_DERIVED else (0,))
        if source_indices is None
        else source_indices,
        value=value,
        unit=None if value is None else VisualClaimUnit.MM,
        blocking=blocking,
        description="Observed part depth",
    )


def _observation(
    claims: tuple[VisualClaim, ...],
    questions: tuple[ClarificationQuestion, ...] = (),
) -> VisualObservation:
    reconstruction_id, _ = reconstruction_identity(_CREATE_KEY)
    image_set_id = "image_set_" + "2" * 32
    generation = 1
    return VisualObservation(
        reconstruction_id=reconstruction_id,
        generation=generation,
        image_set_id=image_set_id,
        image_set_manifest_sha256=_MANIFEST_DIGEST,
        invocation_id=visual_invocation_identity(
            reconstruction_id,
            generation,
            image_set_id,
            _MANIFEST_DIGEST,
        ),
        claims=claims,
        questions=questions,
    )


def _design(
    claim: VisualClaim,
    *,
    evidence_status: DesignEvidenceStatus | None = None,
    source_refs: tuple[str, ...] | None = None,
) -> ParametricDesignIR:
    evidence_id = _ir_id("evidence", 1)
    parameter_id = _ir_id("parameter", 1)
    sketch_id = _ir_id("sketch", 1)
    geometry_id = _ir_id("geometry", 1)
    return ParametricDesignIR(
        id=_ir_id("design", 1),
        name="Visual plate",
        units=UnitSystem(),
        body=BodyDefinition(id=_ir_id("body", 1), name="Visual plate body"),
        evidence=(
            DesignEvidence(
                id=evidence_id,
                status=evidence_status
                or {
                    VisualClaimStatus.CONFIRMED: DesignEvidenceStatus.CONFIRMED,
                    VisualClaimStatus.CALIBRATED: DesignEvidenceStatus.CALIBRATED,
                    VisualClaimStatus.CROSS_VIEW_DERIVED: (DesignEvidenceStatus.CROSS_VIEW_DERIVED),
                    VisualClaimStatus.ASSUMED: DesignEvidenceStatus.CONFIRMED,
                }[claim.status],
                origin=DesignEvidenceOrigin.IMAGE,
                source_refs=source_refs or (claim.id,),
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
                        id=geometry_id,
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


def _acceptance() -> AcceptanceSpec:
    return AcceptanceSpec(
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
    )


def _proposal(
    claim: VisualClaim,
    *,
    observation: VisualObservation | None = None,
    answers: tuple[ClarificationAnswer, ...] = (),
    design: ParametricDesignIR | None = None,
    bindings: tuple[EvidenceBinding, ...] | None = None,
) -> ReconstructionProposal:
    checked_design = design or _design(claim)
    checked_bindings = (
        bindings
        if bindings is not None
        else (
            EvidenceBinding(
                evidence_id=checked_design.evidence[0].id,
                claim_ids=(claim.id,),
            ),
        )
    )
    return ReconstructionProposal(
        observation=observation or _observation((claim,)),
        design=checked_design,
        acceptance=_acceptance(),
        evidence_bindings=checked_bindings,
        clarification_answers=answers,
        part_type="mounting_plate",
        summary="One editable circular plate reconstructed from visual evidence.",
        alternatives=("Revolve the same circular profile",),
        unsupported=("Surface texture",),
        expected_views=(ViewRole.FRONT, ViewRole.ISOMETRIC),
    )


def test_claim_identity_digest_and_mapping_are_canonical() -> None:
    claim = _claim()

    assert claim.id.startswith("visual_claim_")
    assert len(claim.digest) == 64
    assert VisualClaim.from_mapping(claim.to_mapping()) == claim
    assert claim.source_indices == (0,)

    tampered = claim.to_mapping()
    tampered["value"] = 9
    with pytest.raises(ReconstructionContractError) as caught:
        VisualClaim.from_mapping(tampered)
    assert caught.value.code is ReconstructionContractErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    ("status", "value"),
    [
        (VisualClaimStatus.CONFIRMED, 1),
        (VisualClaimStatus.CALIBRATED, 1),
        (VisualClaimStatus.CROSS_VIEW_DERIVED, 1),
        (VisualClaimStatus.ASSUMED, 1),
        (VisualClaimStatus.UNKNOWN, None),
        (VisualClaimStatus.CONFLICT, None),
    ],
)
def test_all_claim_statuses_are_closed_and_round_trip(
    status: VisualClaimStatus,
    value: int | None,
) -> None:
    claim = _claim(status=status, value=value, blocking=status in {VisualClaimStatus.UNKNOWN})

    assert VisualClaim.from_mapping(claim.to_mapping()).status is status


def test_cross_view_claim_requires_at_least_two_sources() -> None:
    with pytest.raises(ReconstructionContractError) as caught:
        _claim(
            status=VisualClaimStatus.CROSS_VIEW_DERIVED,
            value=8,
            source_indices=(0,),
        )

    assert caught.value.code is ReconstructionContractErrorCode.INVALID_INPUT
    assert caught.value.path == "/source_indices"


def test_observation_binds_exact_image_set_manifest_and_deterministic_invocation() -> None:
    claim = _claim()
    observation = _observation((claim,))
    raw = encode_visual_observation(observation)

    assert decode_visual_observation(raw) == observation
    assert observation.id.startswith("visual_observation_")
    assert observation.proposal_blockers == ()

    changed = observation.to_mapping()
    changed["image_set_manifest_sha256"] = "b" * 64
    with pytest.raises(ReconstructionContractError) as caught:
        VisualObservation.from_mapping(changed)
    assert caught.value.code is ReconstructionContractErrorCode.INTEGRITY_FAILURE
    assert caught.value.path == "/invocation_id"


def test_observation_requires_status_matched_questions_for_assumptions_and_blockers() -> None:
    assumed = _claim(status=VisualClaimStatus.ASSUMED)
    with pytest.raises(ReconstructionContractError):
        _observation((assumed,))

    question = clarification_question_for_claim(assumed, "Is the hidden side symmetric?")
    assert _observation((assumed,), (question,)).questions == (question,)

    wrong = ClarificationQuestion(
        claim_id=assumed.id,
        kind=ClarificationKind.RESOLVE_UNKNOWN,
        prompt="Wrong question kind",
    )
    with pytest.raises(ReconstructionContractError):
        _observation((assumed,), (wrong,))


def test_proposal_round_trips_exact_ir_acceptance_and_evidence_binding() -> None:
    claim = _claim()
    proposal = _proposal(claim)
    raw = encode_reconstruction_proposal(proposal)
    restored = decode_reconstruction_proposal(raw)

    assert restored == proposal
    assert restored.design == proposal.design
    assert restored.design_digest == proposal.design.digest
    assert restored.acceptance == proposal.acceptance
    assert len(restored.acceptance_digest) == 64
    assert restored.evidence_bindings[0].claim_ids == (claim.id,)
    assert "operations" not in restored.to_mapping()
    assert "program" not in restored.to_mapping()


def test_assumed_claim_requires_an_explicit_true_clarification_answer() -> None:
    claim = _claim(status=VisualClaimStatus.ASSUMED)
    question = clarification_question_for_claim(claim, "Confirm the assumed depth?")
    observation = _observation((claim,), (question,))

    with pytest.raises(ReconstructionContractError) as caught:
        _proposal(claim, observation=observation)
    assert caught.value.code is ReconstructionContractErrorCode.PROPOSAL_BLOCKED

    denied = clarification_answer_for_question(question, False)
    with pytest.raises(ReconstructionContractError) as caught:
        _proposal(claim, observation=observation, answers=(denied,))
    assert caught.value.code is ReconstructionContractErrorCode.PROPOSAL_BLOCKED

    confirmed = clarification_answer_for_question(question, True)
    assert _proposal(claim, observation=observation, answers=(confirmed,)).id.startswith(
        "reconstruction_proposal_"
    )


@pytest.mark.parametrize("status", [VisualClaimStatus.UNKNOWN, VisualClaimStatus.CONFLICT])
def test_blocking_unknown_or_conflict_prevents_a_proposal(status: VisualClaimStatus) -> None:
    supported = _claim()
    blocker = _claim(
        status=status,
        name="overall.width",
        value=None,
        blocking=True,
    )
    question = clarification_question_for_claim(blocker, "Resolve the missing dimension?")
    observation = _observation((supported, blocker), (question,))

    with pytest.raises(ReconstructionContractError) as caught:
        _proposal(supported, observation=observation)
    assert caught.value.code is ReconstructionContractErrorCode.PROPOSAL_BLOCKED


def test_nonblocking_unknown_can_remain_as_unsupported_context() -> None:
    supported = _claim()
    unknown = _claim(
        status=VisualClaimStatus.UNKNOWN,
        name="surface.texture",
        value=None,
        blocking=False,
    )
    proposal = _proposal(supported, observation=_observation((supported, unknown)))

    assert proposal.observation.proposal_blockers == ()


def test_every_ir_evidence_record_must_bind_exactly_to_its_claim_sources() -> None:
    claim = _claim()
    design = _design(claim)

    with pytest.raises(ReconstructionContractError) as caught:
        _proposal(claim, design=design, bindings=())
    assert caught.value.code is ReconstructionContractErrorCode.UNKNOWN_REFERENCE

    other_claim = _claim(name="overall.width", value=20)
    wrong_binding = EvidenceBinding(
        evidence_id=design.evidence[0].id,
        claim_ids=(other_claim.id,),
    )
    with pytest.raises(ReconstructionContractError) as caught:
        _proposal(
            claim,
            observation=_observation((claim, other_claim)),
            design=design,
            bindings=(wrong_binding,),
        )
    assert caught.value.code is ReconstructionContractErrorCode.INTEGRITY_FAILURE


def test_ir_evidence_status_must_preserve_visual_claim_status() -> None:
    claim = _claim(status=VisualClaimStatus.CALIBRATED)
    design = _design(claim, evidence_status=DesignEvidenceStatus.CONFIRMED)

    with pytest.raises(ReconstructionContractError) as caught:
        _proposal(claim, design=design)
    assert caught.value.code is ReconstructionContractErrorCode.INTEGRITY_FAILURE


def test_unknown_fields_versions_noncanonical_json_and_budgets_fail_closed() -> None:
    observation = _observation((_claim(),))
    mapping = observation.to_mapping()
    mapping["extension"] = True
    with pytest.raises(ReconstructionContractError) as caught:
        VisualObservation.from_mapping(mapping)
    assert caught.value.code is ReconstructionContractErrorCode.INVALID_INPUT

    mapping = observation.to_mapping()
    mapping["schema_version"] = 2
    with pytest.raises(ReconstructionContractError) as caught:
        VisualObservation.from_mapping(mapping)
    assert caught.value.code is ReconstructionContractErrorCode.UNSUPPORTED_VERSION

    raw = encode_visual_observation(observation)
    with pytest.raises(ReconstructionContractError) as caught:
        decode_visual_observation(b" " + raw)
    assert caught.value.code is ReconstructionContractErrorCode.INTEGRITY_FAILURE

    with pytest.raises(ReconstructionContractError) as caught:
        decode_visual_observation(b"x" * (MAX_VISUAL_OBSERVATION_BYTES + 1))
    assert caught.value.code is ReconstructionContractErrorCode.BUDGET_EXCEEDED

    with pytest.raises(ReconstructionContractError) as caught:
        decode_reconstruction_proposal(b"x" * (MAX_RECONSTRUCTION_PROPOSAL_BYTES + 1))
    assert caught.value.code is ReconstructionContractErrorCode.BUDGET_EXCEEDED


def test_nested_contract_unknown_fields_are_translated_to_bounded_errors() -> None:
    proposal = _proposal(_claim())
    mapping = proposal.to_mapping()
    design = mapping["design"]
    assert isinstance(design, dict)
    design["extension"] = True

    with pytest.raises(ReconstructionContractError) as caught:
        ReconstructionProposal.from_mapping(mapping)
    assert caught.value.code is ReconstructionContractErrorCode.INVALID_INPUT
    assert caught.value.path == "/design/extension"


def test_wire_digest_rejects_semantic_tampering_even_when_json_is_canonical() -> None:
    proposal = _proposal(_claim())
    mapping = proposal.to_mapping()
    mapping["summary"] = "Tampered summary"
    raw = json.dumps(mapping, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "ascii"
    )

    with pytest.raises(ReconstructionContractError) as caught:
        decode_reconstruction_proposal(raw)
    assert caught.value.code is ReconstructionContractErrorCode.INTEGRITY_FAILURE


def test_every_reconstruction_status_has_one_deterministic_next_action() -> None:
    expected = {
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

    assert set(ReconstructionStatus) == set(expected)
    for status, action in expected.items():
        assert status.next_action is action
        assert next_action_for_status(status.value) is action
