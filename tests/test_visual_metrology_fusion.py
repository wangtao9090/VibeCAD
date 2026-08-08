"""Focused tests for authority-free cross-view dimension binding."""

from __future__ import annotations

import pytest

from vibecad.visual.metrology import DimensionEstimate, MultiViewStatus
from vibecad.visual.metrology_fusion import (
    MAX_FEATURE_BINDINGS,
    MAX_FUSION_VIEWS,
    BoundDimensionRequest,
    BoundViewDimension,
    FusionError,
    FusionErrorCode,
    ViewFeatureBinding,
    reconcile_bound_dimension,
)


def _estimate(value: float, error: float, *, eligible: bool = True) -> DimensionEstimate:
    if not eligible:
        return DimensionEstimate(
            value_mm=value,
            lower_bound_mm=None,
            upper_bound_mm=None,
            calibration_error_bound_mm=None,
            point_error_bound_mm=error,
            error_bound_mm=None,
            decision_eligible=False,
        )
    return DimensionEstimate(
        value_mm=value,
        lower_bound_mm=max(0.0, value - error),
        upper_bound_mm=value + error,
        calibration_error_bound_mm=0.0,
        point_error_bound_mm=error,
        error_bound_mm=error,
        decision_eligible=True,
    )


def _bindings(*views: str) -> tuple[ViewFeatureBinding, ...]:
    return tuple(
        binding
        for view in views
        for binding in (
            ViewFeatureBinding(
                frame_id="frame-main",
                canonical_feature_id="feature-left",
                view_id=view,
                local_feature_id=f"{view}-left",
            ),
            ViewFeatureBinding(
                frame_id="frame-main",
                canonical_feature_id="feature-right",
                view_id=view,
                local_feature_id=f"{view}-right",
            ),
        )
    )


def _observation(
    view: str,
    estimate: DimensionEstimate | None,
    *,
    reversed: bool = False,
) -> BoundViewDimension:
    start = f"{view}-right" if reversed else f"{view}-left"
    end = f"{view}-left" if reversed else f"{view}-right"
    return BoundViewDimension(
        observation_id=f"observation-{view}",
        view_id=view,
        local_start_feature_id=start,
        local_end_feature_id=end,
        estimate=estimate,
    )


def _request(
    observations: tuple[BoundViewDimension, ...],
    *,
    bindings: tuple[ViewFeatureBinding, ...] | None = None,
) -> BoundDimensionRequest:
    views = tuple(item.view_id for item in observations)
    return BoundDimensionRequest(
        frame_id="frame-main",
        dimension_id="dimension-width",
        canonical_start_feature_id="feature-left",
        canonical_end_feature_id="feature-right",
        bindings=_bindings(*views) if bindings is None else bindings,
        observations=observations,
    )


def test_consistent_bound_views_reconcile_in_deterministic_view_order() -> None:
    front = _observation("front", _estimate(10.0, 1.0))
    right = _observation("right", _estimate(11.0, 1.0), reversed=True)
    first = reconcile_bound_dimension(_request((right, front)))
    second = reconcile_bound_dimension(_request((front, right)))

    assert first == second
    assert first.decision.status is MultiViewStatus.CONSISTENT
    assert first.decision.intersection_lower_mm == pytest.approx(10.0)
    assert first.decision.intersection_upper_mm == pytest.approx(11.0)
    assert first.decision.contributing_views == ("front", "right")
    assert first.contributing_observation_ids == (
        "observation-front",
        "observation-right",
    )
    assert first.unknown_observation_ids == ()


def test_disjoint_eligible_intervals_produce_conflict() -> None:
    decision = reconcile_bound_dimension(
        _request(
            (
                _observation("front", _estimate(9.0, 0.5)),
                _observation("right", _estimate(11.0, 0.5)),
            )
        )
    )

    assert decision.decision.status is MultiViewStatus.CONFLICT
    assert decision.decision.conflict_gap_mm == pytest.approx(1.0)


def test_advisory_and_missing_estimates_remain_unknown_not_conflict() -> None:
    decision = reconcile_bound_dimension(
        _request(
            (
                _observation("front", _estimate(9.0, 0.1)),
                _observation("right", _estimate(20.0, 0.1, eligible=False)),
                _observation("detail", None),
            )
        )
    )

    assert decision.decision.status is MultiViewStatus.UNKNOWN
    assert decision.decision.contributing_views == ("front",)
    assert decision.decision.unknown_views == ("detail", "right")
    assert decision.contributing_observation_ids == ("observation-front",)
    assert decision.unknown_observation_ids == (
        "observation-detail",
        "observation-right",
    )


def test_touching_eligible_intervals_are_consistent() -> None:
    decision = reconcile_bound_dimension(
        _request(
            (
                _observation("front", _estimate(9.0, 1.0)),
                _observation("right", _estimate(11.0, 1.0)),
            )
        )
    )

    assert decision.decision.status is MultiViewStatus.CONSISTENT
    assert decision.decision.intersection_lower_mm == pytest.approx(10.0)
    assert decision.decision.intersection_upper_mm == pytest.approx(10.0)


@pytest.mark.parametrize(
    "observations",
    (
        (),
        (_observation("front", None),),
        (_observation("front", _estimate(10.0, 0.2)),),
    ),
)
def test_fewer_than_two_eligible_views_are_unknown(observations) -> None:
    decision = reconcile_bound_dimension(_request(observations))

    assert decision.decision.status is MultiViewStatus.UNKNOWN


def test_missing_or_mismatched_feature_bindings_fail_closed() -> None:
    observation = _observation("front", _estimate(10.0, 0.1))
    missing = _bindings("front")[:1]
    mismatched = (
        missing[0],
        ViewFeatureBinding(
            frame_id="frame-main",
            canonical_feature_id="feature-other",
            view_id="front",
            local_feature_id="front-right",
        ),
    )

    with pytest.raises(FusionError) as unknown:
        reconcile_bound_dimension(_request((observation,), bindings=missing))
    with pytest.raises(FusionError) as mismatch:
        reconcile_bound_dimension(_request((observation,), bindings=mismatched))

    assert unknown.value.code is FusionErrorCode.UNKNOWN_BINDING
    assert mismatch.value.code is FusionErrorCode.BINDING_MISMATCH


def test_cross_frame_binding_fails_before_reconciliation() -> None:
    bindings = list(_bindings("front"))
    bindings[1] = ViewFeatureBinding(
        frame_id="frame-other",
        canonical_feature_id="feature-right",
        view_id="front",
        local_feature_id="front-right",
    )

    with pytest.raises(FusionError) as caught:
        _request((_observation("front", None),), bindings=tuple(bindings))

    assert caught.value.code is FusionErrorCode.CROSS_FRAME_BINDING


def test_duplicate_observation_id_or_view_fails_closed() -> None:
    front = _observation("front", None)
    same_id = BoundViewDimension(
        observation_id=front.observation_id,
        view_id="right",
        local_start_feature_id="right-left",
        local_end_feature_id="right-right",
        estimate=None,
    )
    same_view = BoundViewDimension(
        observation_id="observation-extra",
        view_id="front",
        local_start_feature_id="front-left",
        local_end_feature_id="front-right",
        estimate=None,
    )

    with pytest.raises(FusionError) as duplicate_id:
        _request((front, same_id))
    with pytest.raises(FusionError) as duplicate_view:
        _request((front, same_view))

    assert duplicate_id.value.code is FusionErrorCode.DUPLICATE_ID
    assert duplicate_view.value.code is FusionErrorCode.DUPLICATE_ID


def test_ambiguous_local_or_canonical_bindings_fail_closed() -> None:
    base = _bindings("front")
    duplicate_local = base + (
        ViewFeatureBinding(
            frame_id="frame-main",
            canonical_feature_id="feature-other",
            view_id="front",
            local_feature_id="front-left",
        ),
    )
    duplicate_canonical = base + (
        ViewFeatureBinding(
            frame_id="frame-main",
            canonical_feature_id="feature-left",
            view_id="front",
            local_feature_id="front-other",
        ),
    )

    with pytest.raises(FusionError) as local:
        _request((_observation("front", None),), bindings=duplicate_local)
    with pytest.raises(FusionError) as canonical:
        _request((_observation("front", None),), bindings=duplicate_canonical)

    assert local.value.code is FusionErrorCode.DUPLICATE_ID
    assert canonical.value.code is FusionErrorCode.DUPLICATE_ID


def test_view_and_binding_budgets_are_checked_before_content() -> None:
    too_many_observations = tuple(
        BoundViewDimension(
            observation_id=f"observation-{index}",
            view_id=f"view-{index}",
            local_start_feature_id=f"start-{index}",
            local_end_feature_id=f"end-{index}",
            estimate=None,
        )
        for index in range(MAX_FUSION_VIEWS + 1)
    )
    with pytest.raises(FusionError) as views:
        _request(too_many_observations, bindings=())

    bindings = tuple(
        ViewFeatureBinding(
            frame_id="frame-main",
            canonical_feature_id=f"feature-{feature}",
            view_id=f"view-{view}",
            local_feature_id=f"local-{view}-{feature}",
        )
        for view in range(MAX_FUSION_VIEWS)
        for feature in range(MAX_FEATURE_BINDINGS // MAX_FUSION_VIEWS)
    )
    accepted = _request((), bindings=bindings)
    with pytest.raises(FusionError) as binding_budget:
        _request(
            (),
            bindings=bindings
            + (
                ViewFeatureBinding(
                    frame_id="frame-main",
                    canonical_feature_id="feature-extra",
                    view_id="view-0",
                    local_feature_id="local-extra",
                ),
            ),
        )

    assert len(accepted.bindings) == MAX_FEATURE_BINDINGS
    assert views.value.code is FusionErrorCode.BUDGET_EXCEEDED
    assert binding_budget.value.code is FusionErrorCode.BUDGET_EXCEEDED


def test_more_than_sixteen_distinct_binding_views_fail_closed() -> None:
    bindings = tuple(
        ViewFeatureBinding(
            frame_id="frame-main",
            canonical_feature_id="feature-left",
            view_id=f"view-{index}",
            local_feature_id=f"local-{index}",
        )
        for index in range(MAX_FUSION_VIEWS + 1)
    )

    with pytest.raises(FusionError) as caught:
        _request((), bindings=bindings)

    assert caught.value.code is FusionErrorCode.BUDGET_EXCEEDED
    assert caught.value.path == "/views"


def test_request_requires_strict_tuple_and_exact_domain_types() -> None:
    with pytest.raises(FusionError) as bindings:
        BoundDimensionRequest(
            frame_id="frame-main",
            dimension_id="dimension-width",
            canonical_start_feature_id="feature-left",
            canonical_end_feature_id="feature-right",
            bindings=[],  # type: ignore[arg-type]
            observations=(),
        )
    with pytest.raises(FusionError) as estimate:
        BoundViewDimension(
            observation_id="observation-front",
            view_id="front",
            local_start_feature_id="front-left",
            local_end_feature_id="front-right",
            estimate=object(),  # type: ignore[arg-type]
        )

    assert bindings.value.code is FusionErrorCode.INVALID_INPUT
    assert estimate.value.code is FusionErrorCode.INVALID_INPUT
