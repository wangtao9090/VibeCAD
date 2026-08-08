"""Focused tests for source-normalized evidence overlay plans."""

from __future__ import annotations

import dataclasses

import pytest

from tests.test_visual_evidence import _image_set
from vibecad.visual.contracts import ImageSet
from vibecad.visual.evidence import (
    MAX_EVIDENCE_FEATURES,
    BoundFeatureEvidence,
    BoundVisualEvidence,
    NormalizedEvidencePoint,
)
from vibecad.visual.geometry_fit import PrimitiveFamily
from vibecad.visual.metrology import PixelPoint
from vibecad.visual.overlay import (
    EvidenceOverlayItem,
    OverlayEvidenceStatus,
    OverlayGeometryKind,
    VisualOverlayError,
    VisualOverlayErrorCode,
    VisualOverlayPlan,
    build_evidence_overlay,
)


def _feature(
    family: PrimitiveFamily,
    local_feature_id: str,
    *,
    uncertainty_px: float = 0.95,
) -> BoundFeatureEvidence:
    points = (
        NormalizedEvidencePoint(x=0.1, y=0.2),
        NormalizedEvidencePoint(x=0.5, y=0.6),
        NormalizedEvidencePoint(x=0.8, y=0.9),
    )
    return BoundFeatureEvidence(
        local_feature_id=local_feature_id,
        source_index=0,
        provider_image_id="provider_image_" + "1" * 32,
        family=family,
        claim_ids=("visual_claim_" + "2" * 32,),
        normalized_points=points,
        pixel_points=tuple(
            PixelPoint(
                x_px=point.x * 95,
                y_px=point.y * 63,
                uncertainty_px=uncertainty_px,
            )
            for point in points
        ),
    )


def _evidence(image_set: ImageSet, features: tuple[BoundFeatureEvidence, ...]):
    return BoundVisualEvidence(
        reconstruction_id="reconstruction_" + "3" * 32,
        generation=1,
        image_set_id=image_set.id,
        image_set_manifest_sha256=image_set.manifest_sha256,
        image_batch_manifest_sha256="4" * 64,
        observation_id="visual_observation_" + "5" * 32,
        observation_digest="6" * 64,
        features=features,
    )


def test_family_order_semantics_select_only_safe_overlay_geometry() -> None:
    image_set, _raws = _image_set((96, 64))
    evidence = _evidence(
        image_set,
        tuple(
            _feature(family, f"feature.{index}")
            for index, family in enumerate(
                (
                    PrimitiveFamily.LINE,
                    PrimitiveFamily.CIRCLE,
                    PrimitiveFamily.ARC,
                    PrimitiveFamily.ROTATED_RECTANGLE,
                )
            )
        ),
    )

    result = build_evidence_overlay(evidence, image_set)

    assert tuple(item.geometry_kind for item in result.items) == (
        OverlayGeometryKind.LANDMARK_POINTS,
        OverlayGeometryKind.LANDMARK_POINTS,
        OverlayGeometryKind.ORDERED_POLYLINE,
        OverlayGeometryKind.CLOSED_POLYGON,
    )
    assert all(
        item.evidence_status is OverlayEvidenceStatus.PROVIDER_PROPOSED for item in result.items
    )
    assert result.items[0].points == evidence.features[0].normalized_points
    assert result.items[0].uncertainty_radius_norm == pytest.approx(0.01)
    assert result.observation_digest == evidence.observation_digest


def test_overlay_rejects_image_binding_and_point_pair_mismatch() -> None:
    image_set, _raws = _image_set((96, 64))
    other_image_set, _other_raws = _image_set((97, 64))
    feature = _feature(PrimitiveFamily.LINE, "edge")
    evidence = _evidence(image_set, (feature,))

    with pytest.raises(VisualOverlayError) as image_mismatch:
        build_evidence_overlay(evidence, other_image_set)
    assert image_mismatch.value.code is VisualOverlayErrorCode.BINDING_MISMATCH

    mismatched_points = dataclasses.replace(feature)
    object.__setattr__(mismatched_points, "pixel_points", feature.pixel_points[:-1])
    with pytest.raises(VisualOverlayError) as point_mismatch:
        build_evidence_overlay(
            dataclasses.replace(evidence, features=(mismatched_points,)),
            image_set,
        )
    assert point_mismatch.value.code is VisualOverlayErrorCode.BINDING_MISMATCH


def test_overlay_replays_bound_feature_order_without_reclassification() -> None:
    image_set, _raws = _image_set((96, 64))
    features = (
        _feature(PrimitiveFamily.ARC, "z.feature"),
        _feature(PrimitiveFamily.LINE, "a.feature"),
    )
    evidence = _evidence(image_set, features)

    result = build_evidence_overlay(evidence, image_set)

    assert tuple(item.local_feature_id for item in result.items) == ("a.feature", "z.feature")
    assert tuple(item.family for item in result.items) == (
        PrimitiveFamily.LINE,
        PrimitiveFamily.ARC,
    )


def test_forged_overlay_budget_and_types_fail_before_item_work(monkeypatch) -> None:
    image_set, _raws = _image_set((96, 64))
    feature = _feature(PrimitiveFamily.LINE, "edge")
    evidence = _evidence(image_set, ())
    object.__setattr__(
        evidence,
        "features",
        tuple(
            dataclasses.replace(feature, local_feature_id=f"edge.{index}")
            for index in range(MAX_EVIDENCE_FEATURES + 1)
        ),
    )

    def must_not_build(*args, **kwargs):
        raise AssertionError("over-budget overlay reached item construction")

    monkeypatch.setattr("vibecad.visual.overlay._overlay_item", must_not_build)
    with pytest.raises(VisualOverlayError) as over_budget:
        build_evidence_overlay(evidence, image_set)
    assert over_budget.value.code is VisualOverlayErrorCode.BUDGET_EXCEEDED

    with pytest.raises(VisualOverlayError) as wrong_type:
        build_evidence_overlay(object(), image_set)  # type: ignore[arg-type]
    assert wrong_type.value.code is VisualOverlayErrorCode.INVALID_INPUT


def test_overlay_value_objects_reject_forged_geometry_and_bindings() -> None:
    image_set, _raws = _image_set((96, 64))
    item = build_evidence_overlay(
        _evidence(image_set, (_feature(PrimitiveFamily.LINE, "edge"),)),
        image_set,
    ).items[0]

    with pytest.raises(VisualOverlayError) as geometry:
        dataclasses.replace(item, geometry_kind=OverlayGeometryKind.CLOSED_POLYGON)
    assert geometry.value.code is VisualOverlayErrorCode.INVALID_INPUT

    with pytest.raises(VisualOverlayError) as invalid_claim:
        EvidenceOverlayItem(
            source_index=item.source_index,
            local_feature_id=item.local_feature_id,
            family=item.family,
            geometry_kind=item.geometry_kind,
            evidence_status=item.evidence_status,
            claim_ids=("invalid",),
            points=item.points,
            uncertainty_radius_norm=item.uncertainty_radius_norm,
        )
    assert invalid_claim.value.code is VisualOverlayErrorCode.INVALID_INPUT

    with pytest.raises(VisualOverlayError) as invalid_plan:
        VisualOverlayPlan(
            image_set_id=image_set.id,
            image_set_manifest_sha256=image_set.manifest_sha256,
            image_batch_manifest_sha256="4" * 64,
            observation_id="not-an-observation",
            observation_digest="6" * 64,
            items=(item,),
        )
    assert invalid_plan.value.code is VisualOverlayErrorCode.INVALID_INPUT
