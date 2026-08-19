"""Focused tests for the authority-free ordinary-photo readiness gate."""

from __future__ import annotations

import dataclasses
import hashlib
import math

import pytest

from vibecad.visual.capture_quality import (
    CaptureFrameMetrics,
    CaptureQualityDecision,
    CaptureQualityFinding,
    CaptureQualityIssueCode,
    CaptureQualityReport,
    CaptureQualitySeverity,
)
from vibecad.visual.contracts import (
    NORMALIZATION_PROFILE,
    SOURCE_PNG_PROFILE,
    CalibrationEvidence,
    CalibrationKind,
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
from vibecad.visual.evidence import (
    MAX_EVIDENCE_FEATURES,
    BoundFeatureEvidence,
    BoundVisualEvidence,
    NormalizedEvidencePoint,
)
from vibecad.visual.fit_pipeline import (
    EvidenceFeatureFit,
    EvidenceFeatureFitStatus,
    EvidenceFeatureFitUnknownReason,
    VisualEvidenceFitReport,
)
from vibecad.visual.geometry_fit import (
    GeometryFitResult,
    GeometryFitStatus,
    LinePrimitive,
    PrimitiveFamily,
)
from vibecad.visual.metrology import PixelPoint, PlanePoint
from vibecad.visual.photo_readiness import (
    MAX_CAPTURE_QUALITY_FINDINGS,
    MAX_PHOTO_READINESS_FINDINGS,
    PhotoReadinessDecision,
    PhotoReadinessError,
    PhotoReadinessErrorCode,
    PhotoReadinessIssueCode,
    RequiredPhotoFeature,
    assess_photo_readiness,
)
from vibecad.visual.reconstruction import (
    VisualClaim,
    VisualClaimStatus,
    VisualObservation,
    clarification_question_for_claim,
    reconstruction_identity,
    visual_invocation_identity,
)

_IMAGE_CREATE_KEY = "image_set_create_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_RECONSTRUCTION_CREATE_KEY = "reconstruction_create_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _image_set(
    count: int = 1,
    *,
    scale_sources: tuple[int, ...] = (0,),
    same_object: bool = True,
    same_state: bool = True,
    same_scale: bool = True,
) -> ImageSet:
    image_set_id, create_digest = image_set_identity(_IMAGE_CREATE_KEY)
    inputs = []
    evidence = []
    for index in range(count):
        inputs.append(
            VisualInput(
                original=ImageRef(
                    id=visual_input_identity(_IMAGE_CREATE_KEY, index, "original"),
                    sha256=hashlib.sha256(f"original-{index}".encode()).hexdigest(),
                    size_bytes=1024,
                    mime=ImageMime.PNG,
                    width=640,
                    height=480,
                    profile=SOURCE_PNG_PROFILE,
                ),
                normalized=ImageRef(
                    id=visual_input_identity(_IMAGE_CREATE_KEY, index, "normalized"),
                    sha256=hashlib.sha256(f"normalized-{index}".encode()).hexdigest(),
                    size_bytes=2048,
                    mime=ImageMime.PNG,
                    width=640,
                    height=480,
                    profile=NORMALIZATION_PROFILE,
                ),
                view_role=ViewRole.FRONT if index == 0 else ViewRole.TOP,
                calibration_status=(
                    CalibrationStatus.EXPLICIT_SCALE
                    if index in scale_sources
                    else CalibrationStatus.UNKNOWN
                ),
            )
        )
        if index in scale_sources:
            evidence.append(
                CalibrationEvidence(
                    source_index=index,
                    kind=CalibrationKind.SCALE,
                    reference=f"ruler-{index}",
                    scale_mm_per_pixel=0.1,
                    focal_length_px=None,
                    principal_x_px=None,
                    principal_y_px=None,
                )
            )
    return ImageSet(
        id=image_set_id,
        create_key_digest=create_digest,
        inputs=tuple(inputs),
        unit="mm",
        dimension_hints=(),
        calibration_evidence=tuple(evidence),
        same_object=same_object,
        same_state=same_state,
        same_scale=same_scale,
        processing_authorization=ProcessingAuthorization.CLOUD_PROVIDER,
    )


def _quality(
    count: int = 1,
    *,
    readable: tuple[int, ...] | None = None,
    advisory: bool = False,
) -> CaptureQualityReport:
    readable_sources = tuple(range(count)) if readable is None else readable
    findings = ()
    decision = CaptureQualityDecision.READY
    if advisory:
        findings = (
            CaptureQualityFinding(
                code=CaptureQualityIssueCode.BLUR_RISK,
                severity=CaptureQualitySeverity.ADVISORY,
                source_indices=(0,),
            ),
        )
        decision = CaptureQualityDecision.RECAPTURE_RECOMMENDED
    if not readable_sources:
        findings = tuple(
            CaptureQualityFinding(
                code=CaptureQualityIssueCode.NO_VISUAL_SIGNAL,
                severity=CaptureQualitySeverity.UNREADABLE,
                source_indices=(index,),
            )
            for index in range(count)
        )
        decision = CaptureQualityDecision.STOP
    return CaptureQualityReport(
        decision=decision,
        metrics=tuple(
            CaptureFrameMetrics(
                source_index=index,
                width=640,
                height=480,
                mean_luminance=0.5,
                shadow_fraction=0.1,
                highlight_fraction=0.1,
                contrast_span=0.8,
                sharpness=0.1,
            )
            for index in range(count)
        ),
        findings=findings,
        readable_source_indices=readable_sources,
        redundant_source_indices=(),
    )


def _observation(
    image_set: ImageSet,
    *,
    status: VisualClaimStatus = VisualClaimStatus.CONFIRMED,
    blocking: bool = False,
) -> tuple[VisualObservation, VisualClaim]:
    claim = VisualClaim(
        name="outer.edge",
        status=status,
        source_indices=(0,),
        value=None if status in {VisualClaimStatus.UNKNOWN, VisualClaimStatus.CONFLICT} else True,
        blocking=blocking,
        description="Caller-visible edge evidence",
    )
    questions = ()
    if blocking:
        questions = (clarification_question_for_claim(claim, "Resolve this claim"),)
    reconstruction_id, _digest = reconstruction_identity(_RECONSTRUCTION_CREATE_KEY)
    return (
        VisualObservation(
            reconstruction_id=reconstruction_id,
            generation=1,
            image_set_id=image_set.id,
            image_set_manifest_sha256=image_set.manifest_sha256,
            invocation_id=visual_invocation_identity(
                reconstruction_id,
                1,
                image_set.id,
                image_set.manifest_sha256,
            ),
            claims=(claim,),
            questions=questions,
        ),
        claim,
    )


def _feature(
    claim: VisualClaim,
    *,
    source_index: int = 0,
    local_feature_id: str = "outer.edge",
    family: PrimitiveFamily = PrimitiveFamily.LINE,
) -> BoundFeatureEvidence:
    return BoundFeatureEvidence(
        local_feature_id=local_feature_id,
        source_index=source_index,
        provider_image_id=f"provider_image_{source_index + 1:032x}",
        family=family,
        claim_ids=(claim.id,),
        normalized_points=(
            NormalizedEvidencePoint(x=0.1, y=0.2),
            NormalizedEvidencePoint(x=0.9, y=0.2),
        ),
        pixel_points=(
            PixelPoint(x_px=64.0, y_px=96.0, uncertainty_px=0.2),
            PixelPoint(x_px=576.0, y_px=96.0, uncertainty_px=0.2),
        ),
    )


def _bound_evidence(
    observation: VisualObservation,
    *features: BoundFeatureEvidence,
) -> BoundVisualEvidence:
    return BoundVisualEvidence(
        reconstruction_id=observation.reconstruction_id,
        generation=observation.generation,
        image_set_id=observation.image_set_id,
        image_set_manifest_sha256=observation.image_set_manifest_sha256,
        image_batch_manifest_sha256="c" * 64,
        observation_id=observation.id,
        observation_digest=observation.digest,
        features=features,
    )


def _fit(feature: BoundFeatureEvidence, *, fitted: bool = True) -> EvidenceFeatureFit:
    if not fitted:
        return EvidenceFeatureFit(
            source_index=feature.source_index,
            provider_image_id=feature.provider_image_id,
            local_feature_id=feature.local_feature_id,
            family=feature.family,
            claim_ids=feature.claim_ids,
            frame_id=None,
            calibration_sha256=None,
            status=EvidenceFeatureFitStatus.UNKNOWN,
            plane_points=(),
            fit_result=None,
            unknown_reason=EvidenceFeatureFitUnknownReason.MISSING_CALIBRATION,
        )
    points = (
        PlanePoint(x_mm=0.0, y_mm=0.0, uncertainty_mm=0.02),
        PlanePoint(x_mm=10.0, y_mm=0.0, uncertainty_mm=0.02),
    )
    result = GeometryFitResult(
        family=feature.family,
        status=GeometryFitStatus.FITTED,
        primitive=LinePrimitive(
            anchor_x_mm=5.0,
            anchor_y_mm=0.0,
            direction_x=1.0,
            direction_y=0.0,
        ),
        rms_residual_mm=0.0,
        max_residual_mm=0.0,
        max_excess_residual_mm=0.0,
        unknown_reason=None,
        point_count=2,
    )
    return EvidenceFeatureFit(
        source_index=feature.source_index,
        provider_image_id=feature.provider_image_id,
        local_feature_id=feature.local_feature_id,
        family=feature.family,
        claim_ids=feature.claim_ids,
        frame_id=f"frame-{feature.source_index}",
        calibration_sha256=hashlib.sha256(
            f"calibration-{feature.source_index}".encode()
        ).hexdigest(),
        status=EvidenceFeatureFitStatus.FITTED,
        plane_points=points,
        fit_result=result,
        unknown_reason=None,
    )


def _fit_report(
    evidence: BoundVisualEvidence,
    *fits: EvidenceFeatureFit,
) -> VisualEvidenceFitReport:
    return VisualEvidenceFitReport(
        reconstruction_id=evidence.reconstruction_id,
        generation=evidence.generation,
        image_set_id=evidence.image_set_id,
        image_set_manifest_sha256=evidence.image_set_manifest_sha256,
        image_batch_manifest_sha256=evidence.image_batch_manifest_sha256,
        observation_id=evidence.observation_id,
        observation_digest=evidence.observation_digest,
        feature_fits=fits,
    )


def _ready_case(*, advisory: bool = False):
    image_set = _image_set()
    observation, claim = _observation(image_set)
    feature = _feature(claim)
    evidence = _bound_evidence(observation, feature)
    fit_report = _fit_report(evidence, _fit(feature))
    requirement = RequiredPhotoFeature(
        source_index=0,
        local_feature_id="outer.edge",
        family=PrimitiveFamily.LINE,
    )
    return image_set, _quality(advisory=advisory), observation, evidence, fit_report, requirement


def _assess(case, *, required_features=None):
    image_set, quality, observation, evidence, fit_report, requirement = case
    return assess_photo_readiness(
        image_set=image_set,
        capture_quality=quality,
        observation=observation,
        evidence=evidence,
        fit_report=fit_report,
        required_features=(requirement,) if required_features is None else required_features,
    )


def test_ready_is_deterministic_and_advisory_capture_findings_do_not_block() -> None:
    case = _ready_case(advisory=True)

    first = _assess(case)
    second = _assess(case)

    assert first == second
    assert first.decision is PhotoReadinessDecision.READY
    assert first.findings == ()
    assert first.digest == second.digest
    assert first.to_mapping()["digest"] == first.digest
    assert len(first.digest) == 64


def test_unreadable_required_source_needs_capture_but_redundancy_is_not_a_blocker() -> None:
    case = list(_ready_case())
    case[1] = _quality(readable=())

    report = _assess(tuple(case))

    assert report.decision is PhotoReadinessDecision.NEEDS_CAPTURE
    assert tuple(item.code for item in report.findings) == (
        PhotoReadinessIssueCode.CAPTURE_UNREADABLE,
    )


def test_missing_or_ambiguous_explicit_scale_requires_actionable_recapture() -> None:
    image_set = _image_set(2, scale_sources=(0,), same_scale=False)
    observation, claim = _observation(image_set)
    features = (
        _feature(claim, source_index=0, local_feature_id="edge.front"),
        _feature(claim, source_index=1, local_feature_id="edge.top"),
    )
    evidence = _bound_evidence(observation, *features)
    fit_report = _fit_report(evidence, *(_fit(item) for item in features))
    requirements = tuple(
        RequiredPhotoFeature(
            source_index=item.source_index,
            local_feature_id=item.local_feature_id,
            family=item.family,
        )
        for item in features
    )

    report = assess_photo_readiness(
        image_set=image_set,
        capture_quality=_quality(2),
        observation=observation,
        evidence=evidence,
        fit_report=fit_report,
        required_features=requirements,
    )

    assert report.decision is PhotoReadinessDecision.NEEDS_CAPTURE
    assert {item.code for item in report.findings} == {
        PhotoReadinessIssueCode.AMBIGUOUS_SCALE,
        PhotoReadinessIssueCode.MISSING_EXPLICIT_SCALE,
    }


@pytest.mark.parametrize(
    ("status", "blocking", "expected"),
    (
        (VisualClaimStatus.CONFLICT, False, PhotoReadinessIssueCode.PROVIDER_CONFLICT),
        (VisualClaimStatus.UNKNOWN, True, PhotoReadinessIssueCode.BLOCKING_UNKNOWN),
    ),
)
def test_provider_conflict_or_blocking_unknown_fails_closed(status, blocking, expected) -> None:
    image_set = _image_set()
    observation, claim = _observation(image_set, status=status, blocking=blocking)
    feature = _feature(claim)
    evidence = _bound_evidence(observation, feature)
    report = assess_photo_readiness(
        image_set=image_set,
        capture_quality=_quality(),
        observation=observation,
        evidence=evidence,
        fit_report=_fit_report(evidence, _fit(feature)),
        required_features=(
            RequiredPhotoFeature(
                source_index=0,
                local_feature_id=feature.local_feature_id,
                family=feature.family,
            ),
        ),
    )

    assert report.decision is (
        PhotoReadinessDecision.OUT_OF_ENVELOPE
        if status is VisualClaimStatus.CONFLICT
        else PhotoReadinessDecision.UNKNOWN
    )
    assert expected in {item.code for item in report.findings}


def test_missing_fit_and_family_mismatch_are_explicit_fail_closed_results() -> None:
    case = _ready_case()
    image_set, quality, observation, evidence, _fit_report_value, requirement = case

    missing = assess_photo_readiness(
        image_set=image_set,
        capture_quality=quality,
        observation=observation,
        evidence=evidence,
        fit_report=_fit_report(evidence),
        required_features=(requirement,),
    )
    mismatch = assess_photo_readiness(
        image_set=image_set,
        capture_quality=quality,
        observation=observation,
        evidence=evidence,
        fit_report=_fit_report(evidence, _fit(evidence.features[0])),
        required_features=(dataclasses.replace(requirement, family=PrimitiveFamily.CIRCLE),),
    )
    unknown = assess_photo_readiness(
        image_set=image_set,
        capture_quality=quality,
        observation=observation,
        evidence=evidence,
        fit_report=_fit_report(evidence, _fit(evidence.features[0], fitted=False)),
        required_features=(requirement,),
    )
    missing_evidence = assess_photo_readiness(
        image_set=image_set,
        capture_quality=quality,
        observation=observation,
        evidence=evidence,
        fit_report=_fit_report(evidence, _fit(evidence.features[0])),
        required_features=(dataclasses.replace(requirement, local_feature_id="hidden.edge"),),
    )

    assert missing.decision is PhotoReadinessDecision.UNKNOWN
    assert missing.findings[0].code is PhotoReadinessIssueCode.MISSING_REQUIRED_FIT
    assert mismatch.decision is PhotoReadinessDecision.OUT_OF_ENVELOPE
    assert mismatch.findings[0].code is PhotoReadinessIssueCode.REQUIRED_FAMILY_MISMATCH
    assert unknown.decision is PhotoReadinessDecision.UNKNOWN
    assert unknown.findings[0].code is PhotoReadinessIssueCode.REQUIRED_FIT_UNKNOWN
    assert missing_evidence.decision is PhotoReadinessDecision.UNKNOWN
    assert missing_evidence.findings[0].code is PhotoReadinessIssueCode.MISSING_REQUIRED_EVIDENCE


def test_cross_view_identity_is_never_inferred_and_out_of_envelope_wins() -> None:
    image_set = _image_set(
        2,
        scale_sources=(0,),
        same_object=False,
        same_state=False,
        same_scale=False,
    )
    observation, claim = _observation(image_set)
    features = (
        _feature(claim, source_index=0, local_feature_id="edge.front"),
        _feature(claim, source_index=1, local_feature_id="edge.top"),
    )
    evidence = _bound_evidence(observation, *features)

    report = assess_photo_readiness(
        image_set=image_set,
        capture_quality=_quality(2),
        observation=observation,
        evidence=evidence,
        fit_report=_fit_report(evidence, *(_fit(item) for item in features)),
        required_features=tuple(
            RequiredPhotoFeature(
                source_index=item.source_index,
                local_feature_id=item.local_feature_id,
                family=item.family,
            )
            for item in features
        ),
    )

    assert report.decision is PhotoReadinessDecision.OUT_OF_ENVELOPE
    assert {item.code for item in report.findings} == {
        PhotoReadinessIssueCode.AMBIGUOUS_SCALE,
        PhotoReadinessIssueCode.CROSS_VIEW_OBJECT_UNDECLARED,
        PhotoReadinessIssueCode.CROSS_VIEW_STATE_UNDECLARED,
        PhotoReadinessIssueCode.MISSING_EXPLICIT_SCALE,
    }


def test_binding_mismatch_malformed_nonfinite_and_budget_inputs_are_rejected() -> None:
    case = _ready_case()
    image_set, quality, observation, evidence, fit_report, requirement = case

    with pytest.raises(PhotoReadinessError) as non_tuple:
        assess_photo_readiness(
            image_set=image_set,
            capture_quality=quality,
            observation=observation,
            evidence=evidence,
            fit_report=fit_report,
            required_features=[requirement],  # type: ignore[arg-type]
        )
    assert non_tuple.value.code is PhotoReadinessErrorCode.INVALID_INPUT

    bad_quality = dataclasses.replace(
        quality,
        metrics=(dataclasses.replace(quality.metrics[0], sharpness=math.nan),),
    )
    with pytest.raises(PhotoReadinessError) as nonfinite:
        assess_photo_readiness(
            image_set=image_set,
            capture_quality=bad_quality,
            observation=observation,
            evidence=evidence,
            fit_report=fit_report,
            required_features=(requirement,),
        )
    assert nonfinite.value.code is PhotoReadinessErrorCode.INVALID_INPUT

    too_many_capture_findings = dataclasses.replace(
        quality,
        decision=CaptureQualityDecision.RECAPTURE_RECOMMENDED,
        findings=(
            CaptureQualityFinding(
                code=CaptureQualityIssueCode.BLUR_RISK,
                severity=CaptureQualitySeverity.ADVISORY,
                source_indices=(0,),
            ),
        )
        * (MAX_CAPTURE_QUALITY_FINDINGS + 1),
    )
    with pytest.raises(PhotoReadinessError) as capture_budget:
        assess_photo_readiness(
            image_set=image_set,
            capture_quality=too_many_capture_findings,
            observation=observation,
            evidence=evidence,
            fit_report=fit_report,
            required_features=(requirement,),
        )
    assert capture_budget.value.code is PhotoReadinessErrorCode.BUDGET_EXCEEDED

    fitted = fit_report.feature_fits[0]
    assert fitted.fit_result is not None
    assert type(fitted.fit_result.primitive) is LinePrimitive
    nonfinite_primitive = dataclasses.replace(
        fitted.fit_result,
        primitive=dataclasses.replace(fitted.fit_result.primitive, anchor_x_mm=math.inf),
    )
    bad_fit = dataclasses.replace(fitted, fit_result=nonfinite_primitive)
    with pytest.raises(PhotoReadinessError) as invalid_fit:
        assess_photo_readiness(
            image_set=image_set,
            capture_quality=quality,
            observation=observation,
            evidence=evidence,
            fit_report=_fit_report(evidence, bad_fit),
            required_features=(requirement,),
        )
    assert invalid_fit.value.code is PhotoReadinessErrorCode.INVALID_INPUT

    mismatched = dataclasses.replace(fit_report, image_batch_manifest_sha256="d" * 64)
    with pytest.raises(PhotoReadinessError) as binding:
        assess_photo_readiness(
            image_set=image_set,
            capture_quality=quality,
            observation=observation,
            evidence=evidence,
            fit_report=mismatched,
            required_features=(requirement,),
        )
    assert binding.value.code is PhotoReadinessErrorCode.BINDING_MISMATCH

    excessive = tuple(requirement for _ in range(MAX_EVIDENCE_FEATURES + 1))
    with pytest.raises(PhotoReadinessError) as budget:
        assess_photo_readiness(
            image_set=image_set,
            capture_quality=quality,
            observation=observation,
            evidence=evidence,
            fit_report=fit_report,
            required_features=excessive,
        )
    assert budget.value.code is PhotoReadinessErrorCode.BUDGET_EXCEEDED


def test_report_digest_rejects_tamper() -> None:
    report = _assess(_ready_case())

    with pytest.raises(PhotoReadinessError) as tamper:
        dataclasses.replace(report, digest="f" * 64)

    assert tamper.value.code is PhotoReadinessErrorCode.BINDING_MISMATCH

    with pytest.raises(PhotoReadinessError) as invalid_identifier:
        dataclasses.replace(report, image_set_id="not-an-image-set")
    assert invalid_identifier.value.code is PhotoReadinessErrorCode.INVALID_INPUT

    with pytest.raises(PhotoReadinessError) as duplicate_requirements:
        dataclasses.replace(
            report,
            required_features=report.required_features + report.required_features,
            digest="",
        )
    assert duplicate_requirements.value.code is PhotoReadinessErrorCode.INVALID_INPUT

    unreadable_case = list(_ready_case())
    unreadable_case[1] = _quality(readable=())
    excessive_finding = (_assess(tuple(unreadable_case)).findings[0],)
    with pytest.raises(PhotoReadinessError) as report_budget:
        dataclasses.replace(
            report,
            findings=excessive_finding * (MAX_PHOTO_READINESS_FINDINGS + 1),
            digest="",
        )
    assert report_budget.value.code is PhotoReadinessErrorCode.BUDGET_EXCEEDED

    with pytest.raises(PhotoReadinessError) as inconsistent_decision:
        dataclasses.replace(
            report,
            decision=PhotoReadinessDecision.UNKNOWN,
            digest="",
        )
    assert inconsistent_decision.value.code is PhotoReadinessErrorCode.INVALID_INPUT

    assert MAX_CAPTURE_QUALITY_FINDINGS == 5 * 16 - 1


def test_readiness_digest_binds_normalized_evidence_and_all_plane_points() -> None:
    case = _ready_case()
    baseline = _assess(case)
    image_set, quality, observation, evidence, fit_report, requirement = case

    feature = evidence.features[0]
    shifted_normalized = dataclasses.replace(
        feature,
        normalized_points=tuple(
            dataclasses.replace(point, x=min(1.0, point.x + 0.01))
            for point in feature.normalized_points
        ),
    )
    changed_evidence = dataclasses.replace(evidence, features=(shifted_normalized,))
    evidence_result = assess_photo_readiness(
        image_set=image_set,
        capture_quality=quality,
        observation=observation,
        evidence=changed_evidence,
        fit_report=fit_report,
        required_features=(requirement,),
    )

    fitted = fit_report.feature_fits[0]
    shifted_fit = dataclasses.replace(
        fitted,
        plane_points=tuple(
            dataclasses.replace(point, x_mm=point.x_mm + 100.0) for point in fitted.plane_points
        ),
    )
    changed_fit_report = dataclasses.replace(fit_report, feature_fits=(shifted_fit,))
    fit_result = assess_photo_readiness(
        image_set=image_set,
        capture_quality=quality,
        observation=observation,
        evidence=evidence,
        fit_report=changed_fit_report,
        required_features=(requirement,),
    )

    assert evidence_result.evidence_sha256 != baseline.evidence_sha256
    assert evidence_result.digest != baseline.digest
    assert fit_result.fit_report_sha256 != baseline.fit_report_sha256
    assert fit_result.digest != baseline.digest
