from __future__ import annotations

import dataclasses
import inspect
import math

import pytest

from tests.test_visual_calibration_authority import _basis, _landmarks, _sealed_inputs
from tests.test_visual_proposal_coverage import _proposal as coverage_proposal
from vibecad.application.proposal_evidence_evaluator import (
    ConsumerClosureReason,
    ProposalEvidenceDecision,
    ProposalEvidenceEvaluationError,
    ProposalEvidenceEvaluationErrorCode,
    ProposalEvidenceEvaluationReport,
    evaluate_proposal_evidence,
)
from vibecad.visual.calibration_authority import (
    ConfirmedPlanarLandmark,
    build_in_memory_planar_calibration_receipt,
)
from vibecad.visual.capture_quality import (
    CaptureFrameMetrics,
    CaptureQualityDecision,
    CaptureQualityReport,
)
from vibecad.visual.evidence import (
    BoundFeatureEvidence,
    BoundVisualEvidence,
    NormalizedEvidencePoint,
)
from vibecad.visual.fit_pipeline import (
    EvidenceFeatureFit,
    EvidenceFeatureFitStatus,
    SourcePlanarCalibration,
    VisualEvidenceFitReport,
)
from vibecad.visual.geometry_fit import (
    GeometryFitRequest,
    PrimitiveFamily,
    fit_declared_geometry,
)
from vibecad.visual.metrology import PixelPoint, PlanePoint
from vibecad.visual.reconstruction import (
    ReconstructionProposal,
    VisualClaim,
    VisualClaimStatus,
    VisualClaimUnit,
    VisualObservation,
    clarification_answer_for_question,
    clarification_question_for_claim,
    visual_invocation_identity,
)


def _metric_landmarks() -> tuple[ConfirmedPlanarLandmark, ...]:
    base = _landmarks()
    plane = ((0.0, 0.0), (40.0, 0.0), (0.0, 30.0), (40.0, 30.0))
    return tuple(
        dataclasses.replace(item, x_mm=x_mm, y_mm=y_mm)
        for item, (x_mm, y_mm) in zip(base, plane, strict=True)
    )


def _proposal(
    *,
    image_set_id: str,
    manifest: str,
    holes: int = 0,
    confirm_pad: bool = True,
    length_claim: int = 8,
) -> ReconstructionProposal:
    base = coverage_proposal(holes=holes)
    replacements: dict[str, VisualClaim] = {}
    for claim in base.observation.claims:
        key = claim.name.removeprefix("coverage.")
        if key == "length":
            replacements[claim.id] = VisualClaim(
                name=claim.name,
                status=VisualClaimStatus.ASSUMED,
                source_indices=claim.source_indices,
                value=length_claim,
                unit=VisualClaimUnit.MM,
                description=claim.description,
            )
        elif key in {"pad", "hole_feature"} and (confirm_pad or key == "hole_feature"):
            replacements[claim.id] = VisualClaim(
                name=claim.name,
                status=VisualClaimStatus.ASSUMED,
                source_indices=claim.source_indices,
                value=True,
                unit=None,
                description=claim.description,
            )
        else:
            replacements[claim.id] = claim
    claims = tuple(replacements[item.id] for item in base.observation.claims)
    questions = tuple(
        clarification_question_for_claim(item, f"Confirm {item.name}")
        for item in claims
        if item.status is VisualClaimStatus.ASSUMED
    )
    answers = tuple(clarification_answer_for_question(item, True) for item in questions)
    observation = VisualObservation(
        reconstruction_id=base.observation.reconstruction_id,
        generation=base.observation.generation,
        image_set_id=image_set_id,
        image_set_manifest_sha256=manifest,
        invocation_id=visual_invocation_identity(
            base.observation.reconstruction_id,
            base.observation.generation,
            image_set_id,
            manifest,
        ),
        claims=claims,
        questions=questions,
    )
    claim_ids = {old: new.id for old, new in replacements.items()}
    evidence = tuple(
        dataclasses.replace(
            item,
            source_refs=tuple(claim_ids[value] for value in item.source_refs),
        )
        for item in base.design.evidence
    )
    sketches = base.design.sketches
    if holes:
        locations = sketches[1]
        circles = tuple(
            dataclasses.replace(
                item,
                dimensions={
                    "cx_mm": 8 + (index % 4) * 8,
                    "cy_mm": 6 + (index // 4) * 6,
                    "radius_mm": 2,
                },
            )
            for index, item in enumerate(locations.geometries)
        )
        sketches = (sketches[0], dataclasses.replace(locations, geometries=circles))
    design = dataclasses.replace(base.design, evidence=evidence, sketches=sketches)
    bindings = tuple(
        dataclasses.replace(
            item,
            claim_ids=tuple(claim_ids[value] for value in item.claim_ids),
        )
        for item in base.evidence_bindings
    )
    return ReconstructionProposal(
        observation=observation,
        design=design,
        acceptance=base.acceptance,
        evidence_bindings=bindings,
        clarification_answers=answers,
        part_type=base.part_type,
        summary=base.summary,
    )


def _capture() -> CaptureQualityReport:
    return CaptureQualityReport(
        decision=CaptureQualityDecision.READY,
        metrics=(
            CaptureFrameMetrics(
                source_index=0,
                width=101,
                height=101,
                mean_luminance=0.5,
                shadow_fraction=0.0,
                highlight_fraction=0.0,
                contrast_span=0.5,
                sharpness=0.5,
            ),
        ),
        findings=(),
        readable_source_indices=(0,),
        redundant_source_indices=(),
    )


def _claim(proposal: ReconstructionProposal, name: str) -> str:
    return next(item.id for item in proposal.observation.claims if item.name == f"coverage.{name}")


def _feature(
    *,
    provider_image_id: str,
    local_id: str,
    family: PrimitiveFamily,
    claims: tuple[str, ...],
    points: tuple[PlanePoint, ...],
) -> BoundFeatureEvidence:
    return BoundFeatureEvidence(
        local_feature_id=local_id,
        source_index=0,
        provider_image_id=provider_image_id,
        family=family,
        claim_ids=claims,
        normalized_points=tuple(
            NormalizedEvidencePoint(x=point.x_mm / 40, y=point.y_mm / 30)
            for point in points
        ),
        pixel_points=tuple(
            PixelPoint(x_px=point.x_mm * 2.5, y_px=point.y_mm * 100 / 30)
            for point in points
        ),
    )


def _fitted(
    feature: BoundFeatureEvidence,
    *,
    calibration_sha256: str,
    points: tuple[PlanePoint, ...],
) -> EvidenceFeatureFit:
    result = fit_declared_geometry(
        GeometryFitRequest(family=feature.family, points=points, residual_tolerance_mm=0.01)
    )
    return EvidenceFeatureFit(
        source_index=feature.source_index,
        provider_image_id=feature.provider_image_id,
        local_feature_id=feature.local_feature_id,
        family=feature.family,
        claim_ids=feature.claim_ids,
        frame_id="front-plane",
        calibration_sha256=calibration_sha256,
        status=EvidenceFeatureFitStatus.FITTED,
        plane_points=points,
        fit_result=result,
        unknown_reason=None,
    )


def _case(*, holes: int = 0, confirm_pad: bool = True, length_claim: int = 8):
    image_set, batch = _sealed_inputs()
    receipt = build_in_memory_planar_calibration_receipt(
        image_set=image_set,
        image_batch=batch,
        source_index=0,
        landmarks=_metric_landmarks(),
        metric_basis=dataclasses.replace(_basis(), frame_id="front-plane"),
    )
    proposal = _proposal(
        image_set_id=image_set.id,
        manifest=image_set.manifest_sha256,
        holes=holes,
        confirm_pad=confirm_pad,
        length_claim=length_claim,
    )
    source = SourcePlanarCalibration(
        source_index=0,
        image_set_manifest_sha256=image_set.manifest_sha256,
        provider_image_id=batch.parts[0].id,
        frame_id="front-plane",
        calibration=receipt.calibration,
    )
    rectangle_points = tuple(
        PlanePoint(x_mm=x, y_mm=y, uncertainty_mm=0.001)
        for x, y in ((0, 0), (40, 0), (40, 30), (0, 30))
    )
    rectangle = _feature(
        provider_image_id=batch.parts[0].id,
        local_id="plate-profile",
        family=PrimitiveFamily.ROTATED_RECTANGLE,
        claims=tuple(
            _claim(proposal, name) for name in ("profile", "edge0", "edge1", "edge2", "edge3")
        ),
        points=rectangle_points,
    )
    features = [rectangle]
    fits = [
        _fitted(
            rectangle,
            calibration_sha256=source.calibration_sha256,
            points=rectangle_points,
        )
    ]
    for index in range(holes):
        geometry = proposal.design.sketches[1].geometries[index]
        cx = float(geometry.dimensions["cx_mm"])
        cy = float(geometry.dimensions["cy_mm"])
        points = tuple(
            PlanePoint(
                x_mm=cx + 2 * math.cos(angle),
                y_mm=cy + 2 * math.sin(angle),
                uncertainty_mm=0.001,
            )
            for angle in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2)
        )
        feature = _feature(
            provider_image_id=batch.parts[0].id,
            local_id=f"hole-{index}",
            family=PrimitiveFamily.CIRCLE,
            claims=tuple(
                _claim(proposal, name) for name in ("diameter", "locations", f"hole{index}")
            ),
            points=points,
        )
        features.append(feature)
        fits.append(_fitted(feature, calibration_sha256=source.calibration_sha256, points=points))
    evidence = BoundVisualEvidence(
        reconstruction_id=proposal.observation.reconstruction_id,
        generation=proposal.observation.generation,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        image_batch_manifest_sha256=batch.manifest_sha256,
        observation_id=proposal.observation.id,
        observation_digest=proposal.observation.digest,
        features=tuple(features),
    )
    report = VisualEvidenceFitReport(
        reconstruction_id=evidence.reconstruction_id,
        generation=evidence.generation,
        image_set_id=evidence.image_set_id,
        image_set_manifest_sha256=evidence.image_set_manifest_sha256,
        image_batch_manifest_sha256=evidence.image_batch_manifest_sha256,
        observation_id=evidence.observation_id,
        observation_digest=evidence.observation_digest,
        feature_fits=tuple(fits),
    )
    return proposal, image_set, receipt, evidence, report


def _evaluate(case):
    proposal, image_set, receipt, evidence, report = case
    return evaluate_proposal_evidence(
        proposal=proposal,
        image_set=image_set,
        capture_quality=_capture(),
        evidence=evidence,
        fit_report=report,
        calibration_receipts=(receipt,),
        clarification_facts=proposal.clarification_answers,
    )


def test_api_derives_plan_and_seals_authority_free_report() -> None:
    parameters = inspect.signature(evaluate_proposal_evidence).parameters
    assert tuple(parameters) == (
        "proposal",
        "image_set",
        "capture_quality",
        "evidence",
        "fit_report",
        "calibration_receipts",
        "clarification_facts",
    )
    assert not {"plan", "requirements", "required_features", "tolerance"} & set(parameters)

    report = _evaluate(_case())

    assert report.decision is ProposalEvidenceDecision.COMPLETE
    assert report.coverage_plan_digest
    assert not report.task_adoption_eligible
    with pytest.raises(TypeError):
        ProposalEvidenceEvaluationReport()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        evaluate_proposal_evidence(**{}, plan=object())  # type: ignore[call-arg]


@pytest.mark.parametrize("holes", [1, 3, 16])
def test_exact_circle_path_supports_bounded_hole_counts(holes: int) -> None:
    report = _evaluate(_case(holes=holes))

    assert report.decision is ProposalEvidenceDecision.COMPLETE
    circle_closures = [item for item in report.consumers if item.mode.value == "circle_fit"]
    assert len(circle_closures) == holes


def test_missing_pad_confirmation_is_unknown() -> None:
    report = _evaluate(_case(confirm_pad=False))

    assert report.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.MISSING_EXPLICIT_CONFIRMATION in report.reasons


def test_confirmed_but_mismatched_pad_length_is_unknown() -> None:
    report = _evaluate(_case(length_claim=9))

    assert report.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.NUMERIC_MISMATCH in report.reasons


def test_forged_calibration_binding_raises_bounded_failure() -> None:
    proposal, image_set, receipt, evidence, report = _case()
    forged = dataclasses.replace(report.feature_fits[0], calibration_sha256="f" * 64)
    report = dataclasses.replace(report, feature_fits=(forged,))

    with pytest.raises(ProposalEvidenceEvaluationError) as caught:
        evaluate_proposal_evidence(
            proposal=proposal,
            image_set=image_set,
            capture_quality=_capture(),
            evidence=evidence,
            fit_report=report,
            calibration_receipts=(receipt,),
            clarification_facts=proposal.clarification_answers,
        )
    assert caught.value.code is ProposalEvidenceEvaluationErrorCode.BINDING_MISMATCH


def test_high_fit_uncertainty_is_unknown() -> None:
    proposal, image_set, receipt, evidence, report = _case()
    fitted = report.feature_fits[0]
    points = tuple(dataclasses.replace(item, uncertainty_mm=1.0) for item in fitted.plane_points)
    fitted = dataclasses.replace(fitted, plane_points=points)
    report = dataclasses.replace(report, feature_fits=(fitted,))

    result = _evaluate((proposal, image_set, receipt, evidence, report))

    assert result.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.UNCERTAINTY_EXCEEDED in result.reasons


def test_ambiguous_rectangle_fit_is_out_of_envelope() -> None:
    proposal, image_set, receipt, evidence, report = _case()
    duplicate_feature = dataclasses.replace(
        evidence.features[0],
        local_feature_id="plate-profile-duplicate",
    )
    duplicate_fit = dataclasses.replace(
        report.feature_fits[0],
        local_feature_id=duplicate_feature.local_feature_id,
    )
    evidence = dataclasses.replace(
        evidence,
        features=(*evidence.features, duplicate_feature),
    )
    report = dataclasses.replace(
        report,
        feature_fits=(*report.feature_fits, duplicate_fit),
    )

    result = _evaluate((proposal, image_set, receipt, evidence, report))

    assert result.decision is ProposalEvidenceDecision.OUT_OF_ENVELOPE
    assert ConsumerClosureReason.AMBIGUOUS_FIT in result.reasons


def test_line_only_profile_never_completes() -> None:
    proposal, image_set, receipt, evidence, report = _case()
    original = evidence.features[0]
    points = original.pixel_points[:2]
    line = dataclasses.replace(
        original,
        family=PrimitiveFamily.LINE,
        normalized_points=original.normalized_points[:2],
        pixel_points=points,
    )
    plane = report.feature_fits[0].plane_points[:2]
    fitted = _fitted(
        line,
        calibration_sha256=report.feature_fits[0].calibration_sha256 or "",
        points=plane,
    )
    evidence = dataclasses.replace(evidence, features=(line,))
    report = dataclasses.replace(report, feature_fits=(fitted,))

    result = _evaluate((proposal, image_set, receipt, evidence, report))

    assert result.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.LINE_ONLY_CANNOT_PROVE_ENDPOINTS in result.reasons
