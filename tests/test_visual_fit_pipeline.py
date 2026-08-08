"""Focused tests for the authority-free visual fit composition."""

from __future__ import annotations

from pathlib import Path

import pytest

import vibecad.visual.fit_pipeline as fit_pipeline_module
from vibecad.visual.evidence import (
    BoundFeatureEvidence,
    BoundVisualEvidence,
    NormalizedEvidencePoint,
)
from vibecad.visual.fit_pipeline import (
    EvidenceFeatureFitStatus,
    EvidenceFeatureFitUnknownReason,
    FeatureFitPolicy,
    SourcePlanarCalibration,
    VisualFitPipelineError,
    VisualFitPipelineErrorCode,
    fit_bound_visual_evidence,
)
from vibecad.visual.geometry_fit import (
    GeometryFitError,
    GeometryFitErrorCode,
    GeometryFitStatus,
    PrimitiveFamily,
)
from vibecad.visual.metrology import (
    PixelPoint,
    PlanarLandmark,
    PlanePoint,
    calibrate_planar_homography,
)

_CLAIM_ID = "visual_claim_" + "1" * 32
_PROVIDER_IMAGE_ID = "provider_image_" + "2" * 32
_IMAGE_SET_MANIFEST = "5" * 64


def _calibration(*, uncertain: bool = False):
    uncertainty = 0.1 if uncertain else 0.0
    return calibrate_planar_homography(
        tuple(
            PlanarLandmark(
                pixel=PixelPoint(x_px=x_px, y_px=y_px, uncertainty_px=uncertainty),
                plane=PlanePoint(x_mm=x_mm, y_mm=y_mm),
            )
            for x_px, y_px, x_mm, y_mm in (
                (0.0, 0.0, 0.0, 0.0),
                (100.0, 0.0, 10.0, 0.0),
                (100.0, 100.0, 10.0, 10.0),
                (0.0, 100.0, 0.0, 10.0),
            )
        )
    )


def _feature(
    *,
    local_feature_id: str = "width_edge",
    pixels: tuple[PixelPoint, ...] = (
        PixelPoint(x_px=20.0, y_px=50.0, uncertainty_px=0.1),
        PixelPoint(x_px=80.0, y_px=50.0, uncertainty_px=0.1),
    ),
) -> BoundFeatureEvidence:
    return BoundFeatureEvidence(
        local_feature_id=local_feature_id,
        source_index=0,
        provider_image_id=_PROVIDER_IMAGE_ID,
        family=PrimitiveFamily.LINE,
        claim_ids=(_CLAIM_ID,),
        normalized_points=tuple(
            NormalizedEvidencePoint(x=point.x_px / 100.0, y=point.y_px / 100.0) for point in pixels
        ),
        pixel_points=pixels,
    )


def _evidence(*features: BoundFeatureEvidence) -> BoundVisualEvidence:
    selected = features or (_feature(),)
    return BoundVisualEvidence(
        reconstruction_id="reconstruction_" + "3" * 32,
        generation=1,
        image_set_id="image_set_" + "4" * 32,
        image_set_manifest_sha256=_IMAGE_SET_MANIFEST,
        image_batch_manifest_sha256="6" * 64,
        observation_id="visual_observation_" + "7" * 32,
        observation_digest="8" * 64,
        features=selected,
    )


def _source_calibration(*, uncertain: bool = False) -> SourcePlanarCalibration:
    return SourcePlanarCalibration(
        source_index=0,
        image_set_manifest_sha256=_IMAGE_SET_MANIFEST,
        provider_image_id=_PROVIDER_IMAGE_ID,
        frame_id="front_plane",
        calibration=_calibration(uncertain=uncertain),
    )


def _policy(local_feature_id: str = "width_edge") -> FeatureFitPolicy:
    return FeatureFitPolicy(
        source_index=0,
        local_feature_id=local_feature_id,
        residual_tolerance_mm=0.05,
    )


def test_explicit_calibration_and_policy_map_then_fit_line() -> None:
    evidence = _evidence()

    report = fit_bound_visual_evidence(
        evidence=evidence,
        calibrations=(_source_calibration(),),
        policies=(_policy(),),
    )

    assert report.observation_digest == evidence.observation_digest
    assert len(report.feature_fits) == 1
    fitted = report.feature_fits[0]
    assert fitted.status is EvidenceFeatureFitStatus.FITTED
    assert fitted.unknown_reason is None
    assert fitted.frame_id == "front_plane"
    assert fitted.calibration_sha256 == _source_calibration().calibration_sha256
    assert fitted.fit_result is not None
    assert fitted.fit_result.status is GeometryFitStatus.FITTED
    assert tuple(
        coordinate for point in fitted.plane_points for coordinate in (point.x_mm, point.y_mm)
    ) == pytest.approx((2.0, 5.0, 8.0, 5.0))


def test_unrequested_and_missing_calibration_are_explicit_unknowns() -> None:
    unrequested = fit_bound_visual_evidence(
        evidence=_evidence(),
        calibrations=(),
        policies=(),
    ).feature_fits[0]
    missing = fit_bound_visual_evidence(
        evidence=_evidence(),
        calibrations=(),
        policies=(_policy(),),
    ).feature_fits[0]

    assert unrequested.unknown_reason is EvidenceFeatureFitUnknownReason.NOT_REQUESTED
    assert missing.unknown_reason is EvidenceFeatureFitUnknownReason.MISSING_CALIBRATION
    assert unrequested.fit_result is missing.fit_result is None
    assert unrequested.plane_points == missing.plane_points == ()
    assert unrequested.calibration_sha256 is missing.calibration_sha256 is None


def test_domain_and_calibration_eligibility_fail_closed_per_feature() -> None:
    outside = _evidence(
        _feature(
            pixels=(
                PixelPoint(x_px=0.0, y_px=50.0, uncertainty_px=0.5),
                PixelPoint(x_px=80.0, y_px=50.0, uncertainty_px=0.1),
            )
        )
    )

    outside_fit = fit_bound_visual_evidence(
        evidence=outside,
        calibrations=(_source_calibration(),),
        policies=(_policy(),),
    ).feature_fits[0]
    ineligible_fit = fit_bound_visual_evidence(
        evidence=_evidence(),
        calibrations=(_source_calibration(uncertain=True),),
        policies=(_policy(),),
    ).feature_fits[0]

    assert outside_fit.unknown_reason is EvidenceFeatureFitUnknownReason.OUTSIDE_CALIBRATION_DOMAIN
    assert (
        ineligible_fit.unknown_reason
        is EvidenceFeatureFitUnknownReason.CALIBRATION_NOT_DECISION_ELIGIBLE
    )
    assert outside_fit.frame_id == ineligible_fit.frame_id == "front_plane"
    assert outside_fit.calibration_sha256 == _source_calibration().calibration_sha256


def test_geometry_unknown_keeps_mapped_points_but_not_a_fitted_primitive() -> None:
    evidence = _evidence(_feature(pixels=(PixelPoint(x_px=50.0, y_px=50.0, uncertainty_px=0.1),)))

    result = fit_bound_visual_evidence(
        evidence=evidence,
        calibrations=(_source_calibration(),),
        policies=(_policy(),),
    ).feature_fits[0]

    assert result.status is EvidenceFeatureFitStatus.UNKNOWN
    assert result.unknown_reason is EvidenceFeatureFitUnknownReason.FIT_REJECTED
    assert result.fit_result is not None
    assert result.fit_result.status is GeometryFitStatus.UNKNOWN
    assert len(result.plane_points) == 1


def test_geometry_numerical_failure_becomes_unknown_with_calibration_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fit(_request):
        raise GeometryFitError(GeometryFitErrorCode.NUMERICAL_FAILURE)

    monkeypatch.setattr(fit_pipeline_module, "fit_declared_geometry", fail_fit)

    result = fit_bound_visual_evidence(
        evidence=_evidence(),
        calibrations=(_source_calibration(),),
        policies=(_policy(),),
    ).feature_fits[0]

    assert result.status is EvidenceFeatureFitStatus.UNKNOWN
    assert result.unknown_reason is EvidenceFeatureFitUnknownReason.FIT_FAILURE
    assert result.fit_result is None
    assert result.plane_points
    assert result.calibration_sha256 == _source_calibration().calibration_sha256


def test_duplicate_and_unknown_policy_bindings_fail_before_mapping() -> None:
    evidence = _evidence()
    calibration = _source_calibration()
    policy = _policy()

    with pytest.raises(VisualFitPipelineError) as duplicate_calibration:
        fit_bound_visual_evidence(
            evidence=evidence,
            calibrations=(calibration, calibration),
            policies=(policy,),
        )
    assert duplicate_calibration.value.code is VisualFitPipelineErrorCode.DUPLICATE_ID

    with pytest.raises(VisualFitPipelineError) as duplicate_policy:
        fit_bound_visual_evidence(
            evidence=evidence,
            calibrations=(calibration,),
            policies=(policy, policy),
        )
    assert duplicate_policy.value.code is VisualFitPipelineErrorCode.DUPLICATE_ID

    with pytest.raises(VisualFitPipelineError) as unknown_policy:
        fit_bound_visual_evidence(
            evidence=evidence,
            calibrations=(calibration,),
            policies=(_policy("missing_feature"),),
        )
    assert unknown_policy.value.code is VisualFitPipelineErrorCode.UNKNOWN_REFERENCE

    unrelated_calibration = SourcePlanarCalibration(
        source_index=1,
        image_set_manifest_sha256=_IMAGE_SET_MANIFEST,
        provider_image_id="provider_image_" + "b" * 32,
        frame_id="side_plane",
        calibration=_calibration(),
    )
    with pytest.raises(VisualFitPipelineError) as unknown_calibration:
        fit_bound_visual_evidence(
            evidence=evidence,
            calibrations=(unrelated_calibration,),
            policies=(),
        )
    assert unknown_calibration.value.code is VisualFitPipelineErrorCode.UNKNOWN_REFERENCE


def test_pipeline_budgets_are_checked_before_duplicate_scans() -> None:
    calibration = _source_calibration()
    policy = _policy()

    with pytest.raises(VisualFitPipelineError) as calibration_budget:
        fit_bound_visual_evidence(
            evidence=_evidence(),
            calibrations=(calibration,) * 17,
            policies=(),
        )
    assert calibration_budget.value.code is VisualFitPipelineErrorCode.BUDGET_EXCEEDED

    with pytest.raises(VisualFitPipelineError) as policy_budget:
        fit_bound_visual_evidence(
            evidence=_evidence(),
            calibrations=(),
            policies=(policy,) * 65,
        )
    assert policy_budget.value.code is VisualFitPipelineErrorCode.BUDGET_EXCEEDED


@pytest.mark.parametrize(
    "replacement",
    (
        {"image_set_manifest_sha256": "9" * 64},
        {"provider_image_id": "provider_image_" + "a" * 32},
    ),
)
def test_calibration_must_bind_exact_image_set_and_provider_overview(replacement) -> None:
    values = {
        "source_index": 0,
        "image_set_manifest_sha256": _IMAGE_SET_MANIFEST,
        "provider_image_id": _PROVIDER_IMAGE_ID,
        "frame_id": "front_plane",
        "calibration": _calibration(),
    }
    values.update(replacement)

    with pytest.raises(VisualFitPipelineError) as raised:
        fit_bound_visual_evidence(
            evidence=_evidence(),
            calibrations=(SourcePlanarCalibration(**values),),
            policies=(_policy(),),
        )

    assert raised.value.code is VisualFitPipelineErrorCode.BINDING_MISMATCH


def test_calibration_digest_is_recomputed_and_forgery_is_rejected() -> None:
    calibration = _source_calibration()
    replay = _source_calibration()

    assert calibration.calibration_sha256 == replay.calibration_sha256
    with pytest.raises(VisualFitPipelineError) as raised:
        SourcePlanarCalibration(
            source_index=0,
            image_set_manifest_sha256=_IMAGE_SET_MANIFEST,
            provider_image_id=_PROVIDER_IMAGE_ID,
            frame_id="front_plane",
            calibration=_calibration(),
            calibration_sha256="f" * 64,
        )
    assert raised.value.code is VisualFitPipelineErrorCode.BINDING_MISMATCH


def test_pipeline_has_no_task_storage_provider_or_cad_authority_imports() -> None:
    import vibecad.visual.fit_pipeline as module

    text = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "vibecad.kernel",
        "vibecad.visual.store",
        "vibecad.visual.provider",
        "vibecad.freecad",
        "vibecad.daemon",
        "vibecad.mcp",
    ):
        assert forbidden not in text
