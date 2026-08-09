from __future__ import annotations

import dataclasses
import inspect

import pytest

from vibecad.parametric.contracts import (
    BodyDefinition,
    ConstraintKind,
    DerivedParameterExpression,
    DesignEvidence,
    DesignEvidenceOrigin,
    DesignEvidenceStatus,
    DesignParameter,
    DesignUnit,
    FeatureExtent,
    FeatureKind,
    GeometryKind,
    ParameterKind,
    ParametricDesignIR,
    ParametricSketch,
    PartDesignFeature,
    PlaneKind,
    ReferencePoint,
    SketchConstraint,
    SketchGeometry,
    SketchPlane,
    SketchReference,
    SketchRole,
    UnitSystem,
)
from vibecad.visual.proposal_coverage import (
    MAX_FIRST_SLICE_CONSUMERS,
    ConsumerRequirement,
    CoverageConsumerKind,
    CoverageMode,
    ProposalCoverageError,
    ProposalCoverageErrorCode,
    ProposalCoveragePlan,
    derive_proposal_coverage_plan,
)
from vibecad.visual.reconstruction import (
    EvidenceBinding,
    ReconstructionProposal,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    visual_invocation_identity,
)
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ValueSource,
)

_RECONSTRUCTION_ID = "reconstruction_00000000000000000000000000000001"
_IMAGE_SET_ID = "image_set_00000000000000000000000000000001"
_MANIFEST_SHA256 = "1" * 64


def _ir_id(kind: str, index: int) -> str:
    return f"ir_{kind}_{index:032x}"


def _claim(name: str, index: int) -> VisualClaim:
    return VisualClaim(
        name=f"coverage.{name}",
        status=VisualClaimStatus.CONFIRMED,
        source_indices=(0,),
        value=index + 1,
        unit=VisualClaimUnit.MM,
        description=f"Evidence for {name}",
    )


def _rectangle_geometries(
    evidence_ids: dict[str, str],
    *,
    missing_first: bool,
    shared_first: bool,
) -> tuple[SketchGeometry, ...]:
    points = (
        (0, 0, 40, 0),
        (40, 0, 40, 30),
        (40, 30, 0, 30),
        (0, 30, 0, 0),
    )
    result: list[SketchGeometry] = []
    for index, (x1, y1, x2, y2) in enumerate(points):
        key = "edge0" if shared_first and index == 1 else f"edge{index}"
        ids = () if missing_first and index == 0 else (evidence_ids[key],)
        result.append(
            SketchGeometry(
                id=_ir_id("geometry", index + 1),
                kind=GeometryKind.LINE,
                dimensions={"x1_mm": x1, "y1_mm": y1, "x2_mm": x2, "y2_mm": y2},
                evidence_ids=ids,
            )
        )
    return tuple(result)


def _proposal(
    *,
    holes: int = 0,
    length: int | float = 8,
    summary: str = "Rectangular plate reconstructed from photos.",
    reverse_declarations: bool = False,
    missing_first_geometry_evidence: bool = False,
    shared_geometry_evidence: bool = False,
    shared_claim: bool = False,
    orphan_evidence: bool = False,
    derived_constraint_count: int = 0,
) -> ReconstructionProposal:
    keys = ["length", "profile", "edge0", "edge1", "edge2", "edge3", "pad"]
    if missing_first_geometry_evidence:
        keys.remove("edge0")
    if shared_geometry_evidence:
        keys.remove("edge1")
    if holes:
        keys.extend(("diameter", "locations", "hole_feature"))
        keys.extend(f"hole{index}" for index in range(holes))
    if orphan_evidence:
        keys.append("orphan")
    claims = {key: _claim(key, index) for index, key in enumerate(keys)}
    evidence_ids = {key: _ir_id("evidence", index + 1) for index, key in enumerate(keys)}
    evidence = tuple(
        DesignEvidence(
            id=evidence_ids[key],
            status=DesignEvidenceStatus.CONFIRMED,
            origin=DesignEvidenceOrigin.IMAGE,
            source_refs=(
                claims["edge0"].id if shared_claim and key == "edge1" else claims[key].id,
            ),
            description=f"Coverage for {key}",
        )
        for key in keys
    )
    bindings = tuple(
        EvidenceBinding(
            evidence_id=evidence_ids[key],
            claim_ids=(claims["edge0"].id if shared_claim and key == "edge1" else claims[key].id,),
        )
        for key in keys
    )
    if reverse_declarations:
        evidence = tuple(reversed(evidence))
        bindings = tuple(reversed(bindings))

    profile_geometries = _rectangle_geometries(
        evidence_ids,
        missing_first=missing_first_geometry_evidence,
        shared_first=shared_geometry_evidence,
    )
    constraints = tuple(
        SketchConstraint(
            id=_ir_id("constraint", index + 1),
            kind=ConstraintKind.HORIZONTAL,
            references=(
                SketchReference(
                    target=profile_geometries[0].id,
                    point=ReferencePoint.WHOLE,
                ),
            ),
        )
        for index in range(derived_constraint_count)
    )
    profile = ParametricSketch(
        id=_ir_id("sketch", 1),
        name="Plate outline",
        role=SketchRole.PROFILE,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin="xy"),
        geometries=profile_geometries,
        constraints=constraints,
        evidence_ids=(evidence_ids["profile"],),
    )
    parameters = [
        DesignParameter(
            id=_ir_id("parameter", 1),
            name="Plate thickness",
            kind=ParameterKind.LENGTH,
            value=length,
            unit=DesignUnit.MM,
            evidence_ids=(evidence_ids["length"],),
            minimum=0.1,
            maximum=100,
        )
    ]
    pad = PartDesignFeature(
        id=_ir_id("feature", 1),
        name="Pad",
        kind=FeatureKind.PAD,
        sketch_id=profile.id,
        base_feature_id=None,
        parameters={"length": parameters[0].id},
        evidence_ids=(evidence_ids["pad"],),
        extent=FeatureExtent.LENGTH,
    )
    sketches = [profile]
    features = [pad]
    if holes:
        parameters.append(
            DesignParameter(
                id=_ir_id("parameter", 2),
                name="Hole diameter",
                kind=ParameterKind.LENGTH,
                value=4,
                unit=DesignUnit.MM,
                evidence_ids=(evidence_ids["diameter"],),
                minimum=0.1,
                maximum=20,
            )
        )
        circles = tuple(
            SketchGeometry(
                id=_ir_id("geometry", 5 + index),
                kind=GeometryKind.CIRCLE,
                dimensions={"cx_mm": 8 + index * 8, "cy_mm": 10, "radius_mm": 1},
                evidence_ids=(evidence_ids[f"hole{index}"],),
            )
            for index in range(holes)
        )
        locations = ParametricSketch(
            id=_ir_id("sketch", 2),
            name="Hole locations",
            role=SketchRole.HOLE_LOCATIONS,
            plane=SketchPlane(kind=PlaneKind.ORIGIN, origin="xy"),
            geometries=circles,
            constraints=(),
            evidence_ids=(evidence_ids["locations"],),
        )
        sketches.append(locations)
        features.append(
            PartDesignFeature(
                id=_ir_id("feature", 2),
                name="Through holes",
                kind=FeatureKind.HOLE,
                sketch_id=locations.id,
                base_feature_id=pad.id,
                parameters={"diameter": parameters[1].id},
                evidence_ids=(evidence_ids["hole_feature"],),
                extent=FeatureExtent.THROUGH_ALL,
                location_geometry_ids=tuple(item.id for item in circles),
            )
        )
    design = ParametricDesignIR(
        id=_ir_id("design", 1),
        name="Photo plate",
        units=UnitSystem(),
        body=BodyDefinition(id=_ir_id("body", 1), name="Photo plate body"),
        evidence=evidence,
        parameters=tuple(parameters),
        datum_planes=(),
        sketches=tuple(sketches),
        features=tuple(features),
    )
    observation = VisualObservation(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=1,
        image_set_id=_IMAGE_SET_ID,
        image_set_manifest_sha256=_MANIFEST_SHA256,
        invocation_id=visual_invocation_identity(
            _RECONSTRUCTION_ID,
            1,
            _IMAGE_SET_ID,
            _MANIFEST_SHA256,
        ),
        claims=tuple(claims.values()),
    )
    acceptance = AcceptanceSpec(
        id="photo-plate-acceptance-v1",
        criteria=(
            AcceptanceCriterion(
                id="one-solid",
                kind=AcceptanceKind.TOPOLOGY,
                check="solid_count",
                target="body",
                expected=1,
            ),
        ),
    )
    return ReconstructionProposal(
        observation=observation,
        design=design,
        acceptance=acceptance,
        evidence_bindings=bindings,
        clarification_answers=(),
        part_type="rectangular_plate",
        summary=summary,
    )


def _requirement(plan: ProposalCoveragePlan, path: str) -> ConsumerRequirement:
    return next(item for item in plan.requirements if item.consumer_path == path)


def test_derives_complete_plate_requirements_without_caller_subset() -> None:
    proposal = _proposal(derived_constraint_count=1)

    plan = derive_proposal_coverage_plan(proposal=proposal)

    assert tuple(inspect.signature(derive_proposal_coverage_plan).parameters) == ("proposal",)
    assert plan.proposal_digest == proposal.digest
    assert plan.design_digest == proposal.design_digest
    assert plan.observation_digest == proposal.observation.digest
    assert plan.acceptance_digest == proposal.acceptance_digest
    assert len(plan.requirements) == 10
    assert tuple(item.consumer_path for item in plan.requirements) == tuple(
        sorted(item.consumer_path for item in plan.requirements)
    )
    assert {item.kind for item in plan.requirements} == {
        CoverageConsumerKind.DESIGN_ROOT,
        CoverageConsumerKind.PARAMETER,
        CoverageConsumerKind.SKETCH,
        CoverageConsumerKind.GEOMETRY,
        CoverageConsumerKind.CONSTRAINT,
        CoverageConsumerKind.FEATURE,
        CoverageConsumerKind.PROGRAM_OPERATION,
    }
    constraint = _requirement(plan, "/design/sketches/0/constraints/0")
    assert constraint.mode is CoverageMode.DERIVED
    assert constraint.evidence_ids == ()
    assert constraint.dependency_paths == ("/design/sketches/0/geometries/0",)
    assert all(
        item.evidence_ids and item.claim_ids
        for item in plan.requirements
        if item.mode is CoverageMode.EVIDENCE_REQUIRED
    )


def test_optional_holes_are_all_enumerated_and_bind_expected_operation() -> None:
    proposal = _proposal(holes=3)

    plan = derive_proposal_coverage_plan(proposal=proposal)

    geometry_requirements = tuple(
        item for item in plan.requirements if item.kind is CoverageConsumerKind.GEOMETRY
    )
    assert len(geometry_requirements) == 7
    assert len(plan.requirements) == 15
    operation = ModelCommand(
        id="visual-adoption-create-design",
        op="create_parametric_design",
        args={"design": proposal.design.to_mapping()},
        source=ValueSource.MODEL,
    ).to_mapping()
    operation_requirement = _requirement(plan, "/program/operations/0")
    assert operation_requirement.mode is CoverageMode.PROGRAM_BINDING
    assert operation_requirement.dependency_paths == ("/design",)
    assert operation_requirement.payload_sha256 != plan.expected_operation_payload_sha256
    assert operation["op"] == "create_parametric_design"


def test_canonical_declaration_order_produces_same_plan() -> None:
    forward = derive_proposal_coverage_plan(proposal=_proposal())
    reversed_plan = derive_proposal_coverage_plan(proposal=_proposal(reverse_declarations=True))

    assert reversed_plan == forward
    assert reversed_plan.digest == forward.digest


def test_parameter_mutation_changes_consumer_operation_and_plan_digests() -> None:
    original = derive_proposal_coverage_plan(proposal=_proposal(length=8))
    changed = derive_proposal_coverage_plan(proposal=_proposal(length=9))

    original_parameter = _requirement(original, "/design/parameters/0")
    changed_parameter = _requirement(changed, "/design/parameters/0")
    assert changed_parameter.payload_sha256 != original_parameter.payload_sha256
    assert changed_parameter.digest != original_parameter.digest
    assert changed.expected_operation_payload_sha256 != original.expected_operation_payload_sha256
    assert changed.digest != original.digest


def test_proposal_only_mutation_is_still_bound_by_plan_digest() -> None:
    first = derive_proposal_coverage_plan(proposal=_proposal(summary="First summary"))
    second = derive_proposal_coverage_plan(proposal=_proposal(summary="Second summary"))

    assert first.design_digest == second.design_digest
    assert first.expected_operation_payload_sha256 == second.expected_operation_payload_sha256
    assert first.proposal_digest != second.proposal_digest
    assert first.digest != second.digest


def test_missing_direct_consumer_evidence_fails_closed() -> None:
    proposal = _proposal(missing_first_geometry_evidence=True)

    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal=proposal)

    assert caught.value.code is ProposalCoverageErrorCode.MISSING_COVERAGE
    assert caught.value.path == "/design/sketches/0/geometries/0"


def test_shared_evidence_cannot_cover_two_direct_consumers() -> None:
    proposal = _proposal(shared_geometry_evidence=True)

    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal=proposal)

    assert caught.value.code is ProposalCoverageErrorCode.AMBIGUOUS_COVERAGE
    assert caught.value.path == "/design/evidence"


def test_shared_claim_cannot_cover_two_direct_consumers() -> None:
    proposal = _proposal(shared_claim=True)

    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal=proposal)

    assert caught.value.code is ProposalCoverageErrorCode.AMBIGUOUS_COVERAGE
    assert caught.value.path == "/observation/claims"


def test_orphan_evidence_fails_closed() -> None:
    proposal = _proposal(orphan_evidence=True)

    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal=proposal)

    assert caught.value.code is ProposalCoverageErrorCode.ORPHAN_EVIDENCE
    assert caught.value.path == "/design/evidence"


def test_slot_profile_is_out_of_first_slice_envelope() -> None:
    proposal = _proposal()
    profile = proposal.design.sketches[0]
    slot = SketchGeometry(
        id=profile.geometries[0].id,
        kind=GeometryKind.SLOT,
        dimensions={"x1_mm": 0, "y1_mm": 0, "x2_mm": 40, "y2_mm": 0, "width_mm": 5},
        evidence_ids=profile.geometries[0].evidence_ids,
    )
    changed_profile = dataclasses.replace(profile, geometries=(slot,))
    changed_design = dataclasses.replace(proposal.design, sketches=(changed_profile,))
    changed = dataclasses.replace(
        proposal,
        design=changed_design,
        design_digest="",
        id="",
        digest="",
    )

    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal=changed)

    assert caught.value.code is ProposalCoverageErrorCode.OUT_OF_ENVELOPE
    assert caught.value.path == "/design/sketches"


def test_derived_parameter_is_out_of_first_slice_envelope() -> None:
    proposal = _proposal()
    existing = proposal.design.parameters[0]
    source_evidence = proposal.design.evidence[0]
    source = DesignParameter(
        id=_ir_id("parameter", 2),
        name="Source thickness",
        kind=ParameterKind.LENGTH,
        value=existing.value,
        unit=DesignUnit.MM,
        evidence_ids=(source_evidence.id,),
    )
    derived = dataclasses.replace(
        existing,
        public=False,
        expression=DerivedParameterExpression(terms={source.id: 1}),
    )
    pad = dataclasses.replace(
        proposal.design.features[0],
        parameters={"length": derived.id},
    )
    changed_design = dataclasses.replace(
        proposal.design,
        parameters=(derived, source),
        features=(pad,),
    )
    changed = dataclasses.replace(
        proposal,
        design=changed_design,
        design_digest="",
        id="",
        digest="",
    )

    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal=changed)

    assert caught.value.code is ProposalCoverageErrorCode.OUT_OF_ENVELOPE
    assert caught.value.path == "/design/parameters"


def test_first_slice_requirement_budget_is_fail_closed() -> None:
    proposal = _proposal(derived_constraint_count=58)

    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal=proposal)

    assert caught.value.code is ProposalCoverageErrorCode.BUDGET_EXCEEDED
    assert caught.value.path == "/requirements"
    assert MAX_FIRST_SLICE_CONSUMERS == 64


def test_plan_and_requirement_reject_tampered_digests() -> None:
    plan = derive_proposal_coverage_plan(proposal=_proposal())
    requirement = plan.requirements[0]

    with pytest.raises(ProposalCoverageError) as requirement_error:
        dataclasses.replace(requirement, digest="0" * 64)
    assert requirement_error.value.code is ProposalCoverageErrorCode.INTEGRITY_FAILURE

    with pytest.raises(ProposalCoverageError) as plan_error:
        dataclasses.replace(plan, digest="0" * 64)
    assert plan_error.value.code is ProposalCoverageErrorCode.INTEGRITY_FAILURE


def test_rejects_non_exact_proposal_type() -> None:
    with pytest.raises(ProposalCoverageError) as caught:
        derive_proposal_coverage_plan(proposal={})  # type: ignore[arg-type]

    assert caught.value.code is ProposalCoverageErrorCode.INVALID_INPUT
    assert caught.value.path == "/proposal"
