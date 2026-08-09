from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest
from PIL import Image

from tests.test_visual_calibration_authority import _basis, _sealed_inputs
from tests.test_visual_preflight import _parts, _save, _seal
from tests.test_visual_proposal_coverage import _proposal as coverage_proposal
from vibecad.application.proposal_evidence_evaluator import (
    ConsumerClosureReason,
    ProposalEvidenceDecision,
    ProposalEvidenceEvaluationError,
    ProposalEvidenceEvaluationErrorCode,
    ProposalEvidenceEvaluationReport,
    evaluate_proposal_evidence,
)
from vibecad.visual.calibration_authority import ConfirmedPlanarLandmark
from vibecad.visual.evidence import NormalizedEvidencePoint, ProviderFeatureEvidence
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.provider_images import (
    ProviderDetailCrop,
    prepare_provider_image_batch,
)
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
    records = (
        ("origin", 0.05, 0.05, 0.0, 0.0),
        ("positive-x", 0.95, 0.05, 40.0, 0.0),
        ("positive-y", 0.05, 0.95, 0.0, 30.0),
        ("opposite", 0.95, 0.95, 40.0, 30.0),
        ("outer-00", 0.0, 0.0, -40 / 18, -30 / 18),
        ("outer-10", 1.0, 0.0, 40 + 40 / 18, -30 / 18),
        ("outer-01", 0.0, 1.0, -40 / 18, 30 + 30 / 18),
        ("outer-11", 1.0, 1.0, 40 + 40 / 18, 30 + 30 / 18),
    )
    return tuple(
        ConfirmedPlanarLandmark(
            landmark_id=identifier,
            confirmation_id=f"confirm-{identifier}",
            normalized_x=normalized_x,
            normalized_y=normalized_y,
            localization_uncertainty_norm=0.0,
            x_mm=x_mm,
            y_mm=y_mm,
        )
        for identifier, normalized_x, normalized_y, x_mm, y_mm in records
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


def _claim(proposal: ReconstructionProposal, name: str) -> str:
    return next(item.id for item in proposal.observation.claims if item.name == f"coverage.{name}")


def _feature(
    *,
    provider_image_id: str,
    local_id: str,
    family: PrimitiveFamily,
    claims: tuple[str, ...],
    points: tuple[tuple[float, float], ...],
    uncertainty: float = 0.000001,
) -> ProviderFeatureEvidence:
    return ProviderFeatureEvidence(
        local_feature_id=local_id,
        source_index=0,
        provider_image_id=provider_image_id,
        family=family,
        points=tuple(NormalizedEvidencePoint(x=x, y=y) for x, y in points),
        localization_uncertainty_norm=uncertainty,
        claim_ids=claims,
    )


def _case(
    tmp_path: Path,
    *,
    holes: int = 0,
    confirm_pad: bool = True,
    length_claim: int = 8,
    blank: bool = False,
):
    _root, _locks, store = _parts(tmp_path)
    source = tmp_path / "source.png"
    _save(source, blank=blank)
    with Image.open(source) as image:
        image.resize((1001, 751), Image.Resampling.NEAREST).save(source, format="PNG")
    image_set = _seal(store, (source,))
    record, normalized = store.read_provider_images_exact(
        image_set.id,
        image_set.manifest_sha256,
    )
    profile = _sealed_inputs()[1].profile
    batch = prepare_provider_image_batch(
        image_set=record,
        normalized_images=normalized,
        profile=profile,
        detail_crops=(),
    )
    proposal = _proposal(
        image_set_id=record.id,
        manifest=record.manifest_sha256,
        holes=holes,
        confirm_pad=confirm_pad,
        length_claim=length_claim,
    )
    features = [
        _feature(
            provider_image_id=batch.parts[0].id,
            local_id="plate-profile",
            family=PrimitiveFamily.ROTATED_RECTANGLE,
            claims=tuple(
                _claim(proposal, name)
                for name in ("profile", "edge0", "edge1", "edge2", "edge3")
            ),
            points=(
                (0.05, 0.05),
                (0.95, 0.05),
                (0.95, 0.95),
                (0.05, 0.95),
            ),
        )
    ]
    for index in range(holes):
        geometry = proposal.design.sketches[1].geometries[index]
        cx = 0.05 + 0.9 * float(geometry.dimensions["cx_mm"]) / 40
        cy = 0.05 + 0.9 * float(geometry.dimensions["cy_mm"]) / 30
        rx, ry = 0.9 * 2 / 40, 0.9 * 2 / 30
        features.append(
            _feature(
                provider_image_id=batch.parts[0].id,
                local_id=f"hole-{index}",
                family=PrimitiveFamily.CIRCLE,
                claims=tuple(
                    _claim(proposal, name)
                    for name in ("diameter", "locations", f"hole{index}")
                ),
                points=((cx + rx, cy), (cx, cy + ry), (cx - rx, cy), (cx, cy - ry)),
            )
        )
    return (
        proposal,
        store,
        batch,
        tuple(features),
        _metric_landmarks(),
        dataclasses.replace(_basis(), frame_id="front-plane"),
    )


def _evaluate(case):
    proposal, store, batch, features, landmarks, basis = case
    return evaluate_proposal_evidence(
        proposal=proposal,
        visual_input_store=store,
        image_batch=batch,
        provider_features=features,
        calibration_landmarks=landmarks,
        metric_basis=basis,
    )


def test_entry_recomputes_complete_authority_free_plate(tmp_path: Path) -> None:
    parameters = inspect.signature(evaluate_proposal_evidence).parameters
    assert tuple(parameters) == (
        "proposal",
        "visual_input_store",
        "image_batch",
        "provider_features",
        "calibration_landmarks",
        "metric_basis",
    )
    assert not {
        "capture_quality",
        "evidence",
        "fit_report",
        "calibration_receipts",
        "plan",
        "policies",
        "tolerance",
        "clarification_facts",
    } & set(parameters)

    report = _evaluate(_case(tmp_path))

    assert report.decision is ProposalEvidenceDecision.COMPLETE
    assert report.coverage_plan_digest
    assert not report.task_adoption_eligible
    with pytest.raises(TypeError):
        ProposalEvidenceEvaluationReport()  # type: ignore[call-arg]


@pytest.mark.parametrize("holes", [1, 3, 16])
def test_exact_raw_circle_path_supports_holes(tmp_path: Path, holes: int) -> None:
    report = _evaluate(_case(tmp_path, holes=holes))

    assert report.decision is ProposalEvidenceDecision.COMPLETE
    assert len([item for item in report.consumers if item.mode.value == "circle_fit"]) == holes


def test_blank_sealed_image_is_recomputed_as_unreadable(tmp_path: Path) -> None:
    report = _evaluate(_case(tmp_path, blank=True))

    assert report.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.CAPTURE_UNREADABLE in report.reasons
    with pytest.raises(TypeError):
        evaluate_proposal_evidence(
            **{},
            capture_quality=object(),  # type: ignore[call-arg]
        )


def test_shifted_raw_rectangle_is_fitted_then_rejected(tmp_path: Path) -> None:
    proposal, store, batch, features, landmarks, basis = _case(tmp_path)
    shifted = dataclasses.replace(
        features[0],
        points=tuple(
            NormalizedEvidencePoint(x=x, y=y)
            for x, y in ((0.1, 0.1), (0.9, 0.1), (0.9, 0.9), (0.1, 0.9))
        ),
    )

    report = _evaluate((proposal, store, batch, (shifted,), landmarks, basis))

    assert report.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.NUMERIC_MISMATCH in report.reasons


def test_collapsed_raw_rectangle_is_fitted_as_unknown(tmp_path: Path) -> None:
    proposal, store, batch, features, landmarks, basis = _case(tmp_path)
    collapsed = dataclasses.replace(
        features[0],
        points=(features[0].points[0],) * 4,
    )

    report = _evaluate((proposal, store, batch, (collapsed,), landmarks, basis))

    assert report.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.FIT_UNKNOWN in report.reasons


def test_alternative_crop_batch_is_not_accepted(tmp_path: Path) -> None:
    proposal, store, batch, features, landmarks, basis = _case(tmp_path)
    image_set, normalized = store.read_provider_images_exact(
        proposal.observation.image_set_id,
        proposal.observation.image_set_manifest_sha256,
    )
    profile = dataclasses.replace(
        batch.profile,
        max_image_parts=2,
        supports_detail_crops=True,
    )
    alternate = prepare_provider_image_batch(
        image_set=image_set,
        normalized_images=normalized,
        profile=profile,
        detail_crops=(
            ProviderDetailCrop(
                source_index=0,
                left=0,
                top=0,
                right=0.5,
                bottom=0.5,
                label="alternate",
            ),
        ),
    )

    with pytest.raises(ProposalEvidenceEvaluationError) as caught:
        _evaluate((proposal, store, alternate, features, landmarks, basis))
    assert caught.value.code is ProposalEvidenceEvaluationErrorCode.BINDING_MISMATCH


def test_missing_pad_confirmation_and_wrong_length_are_unknown(tmp_path: Path) -> None:
    (tmp_path / "missing").mkdir()
    (tmp_path / "mismatch").mkdir()
    missing = _evaluate(_case(tmp_path / "missing", confirm_pad=False))
    mismatch = _evaluate(_case(tmp_path / "mismatch", length_claim=9))

    assert missing.decision is mismatch.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.MISSING_EXPLICIT_CONFIRMATION in missing.reasons
    assert ConsumerClosureReason.NUMERIC_MISMATCH in mismatch.reasons


def test_high_raw_uncertainty_is_unknown(tmp_path: Path) -> None:
    proposal, store, batch, features, landmarks, basis = _case(tmp_path)
    uncertain = dataclasses.replace(features[0], localization_uncertainty_norm=0.003)

    report = _evaluate((proposal, store, batch, (uncertain,), landmarks, basis))

    assert report.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.UNCERTAINTY_EXCEEDED in report.reasons


def test_line_only_raw_profile_never_completes(tmp_path: Path) -> None:
    proposal, store, batch, features, landmarks, basis = _case(tmp_path)
    line = dataclasses.replace(
        features[0],
        family=PrimitiveFamily.LINE,
        points=features[0].points[:2],
    )

    report = _evaluate((proposal, store, batch, (line,), landmarks, basis))

    assert report.decision is ProposalEvidenceDecision.UNKNOWN
    assert ConsumerClosureReason.LINE_ONLY_CANNOT_PROVE_ENDPOINTS in report.reasons
