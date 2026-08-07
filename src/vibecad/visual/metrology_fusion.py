"""Authority-free cross-view binding for planar dimension intervals.

Callers explicitly declare which local features in each view refer to the
same canonical features in one shared plane frame.  This module does not
infer matches, accept homographies, or average point estimates.  It delegates
interval decisions to :mod:`vibecad.visual.metrology`, and only
decision-eligible M10 estimates may contribute to consistency or conflict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from vibecad.visual.metrology import (
    DimensionEstimate,
    MultiViewDecision,
    ViewDimension,
    reconcile_multiview_dimensions,
)

MAX_FUSION_VIEWS = 16
MAX_FEATURE_BINDINGS = 64
MAX_FUSION_ID_BYTES = 64

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class FusionErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    DUPLICATE_ID = "duplicate_id"
    UNKNOWN_BINDING = "unknown_binding"
    BINDING_MISMATCH = "binding_mismatch"
    CROSS_FRAME_BINDING = "cross_frame_binding"


class FusionError(ValueError):
    """Stable fail-closed error that never reflects rejected identifiers."""

    def __init__(self, code: FusionErrorCode, path: str = "") -> None:
        if type(code) is not FusionErrorCode:
            raise TypeError("code must be an exact FusionErrorCode")
        if type(path) is not str or len(path.encode("utf-8")) > 256:
            raise ValueError("path must be a bounded string")
        self.code = code
        self.path = path
        super().__init__(code.value)


def _fail(code: FusionErrorCode, path: str = "") -> None:
    raise FusionError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(FusionErrorCode.INVALID_INPUT, path)
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        _fail(FusionErrorCode.INVALID_INPUT, path)
    if len(encoded) > MAX_FUSION_ID_BYTES or _ID.fullmatch(value) is None:
        _fail(FusionErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ViewFeatureBinding:
    """Caller-declared association between one local and canonical feature."""

    frame_id: str
    canonical_feature_id: str
    view_id: str
    local_feature_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "frame_id",
            "canonical_feature_id",
            "view_id",
            "local_feature_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), f"/{field_name}"),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundViewDimension:
    """One local two-feature distance candidate from one view."""

    observation_id: str
    view_id: str
    local_start_feature_id: str
    local_end_feature_id: str
    estimate: DimensionEstimate | None

    def __post_init__(self) -> None:
        for field_name in (
            "observation_id",
            "view_id",
            "local_start_feature_id",
            "local_end_feature_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), f"/{field_name}"),
            )
        if self.local_start_feature_id == self.local_end_feature_id:
            _fail(FusionErrorCode.INVALID_INPUT, "/local_end_feature_id")
        if self.estimate is not None and type(self.estimate) is not DimensionEstimate:
            _fail(FusionErrorCode.INVALID_INPUT, "/estimate")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundDimensionRequest:
    """A single canonical distance and its caller-declared view bindings."""

    frame_id: str
    dimension_id: str
    canonical_start_feature_id: str
    canonical_end_feature_id: str
    bindings: tuple[ViewFeatureBinding, ...]
    observations: tuple[BoundViewDimension, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "frame_id",
            "dimension_id",
            "canonical_start_feature_id",
            "canonical_end_feature_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), f"/{field_name}"),
            )
        if self.canonical_start_feature_id == self.canonical_end_feature_id:
            _fail(FusionErrorCode.INVALID_INPUT, "/canonical_end_feature_id")
        if type(self.bindings) is not tuple:
            _fail(FusionErrorCode.INVALID_INPUT, "/bindings")
        if len(self.bindings) > MAX_FEATURE_BINDINGS:
            _fail(FusionErrorCode.BUDGET_EXCEEDED, "/bindings")
        if any(type(item) is not ViewFeatureBinding for item in self.bindings):
            _fail(FusionErrorCode.INVALID_INPUT, "/bindings")
        if type(self.observations) is not tuple:
            _fail(FusionErrorCode.INVALID_INPUT, "/observations")
        if len(self.observations) > MAX_FUSION_VIEWS:
            _fail(FusionErrorCode.BUDGET_EXCEEDED, "/observations")
        if any(type(item) is not BoundViewDimension for item in self.observations):
            _fail(FusionErrorCode.INVALID_INPUT, "/observations")

        all_views = {item.view_id for item in self.bindings}
        all_views.update(item.view_id for item in self.observations)
        if len(all_views) > MAX_FUSION_VIEWS:
            _fail(FusionErrorCode.BUDGET_EXCEEDED, "/views")
        if any(item.frame_id != self.frame_id for item in self.bindings):
            _fail(FusionErrorCode.CROSS_FRAME_BINDING, "/bindings")

        local_keys = tuple((item.view_id, item.local_feature_id) for item in self.bindings)
        canonical_keys = tuple((item.view_id, item.canonical_feature_id) for item in self.bindings)
        if len(set(local_keys)) != len(local_keys):
            _fail(FusionErrorCode.DUPLICATE_ID, "/bindings")
        if len(set(canonical_keys)) != len(canonical_keys):
            _fail(FusionErrorCode.DUPLICATE_ID, "/bindings")

        observation_ids = tuple(item.observation_id for item in self.observations)
        observation_views = tuple(item.view_id for item in self.observations)
        if len(set(observation_ids)) != len(observation_ids):
            _fail(FusionErrorCode.DUPLICATE_ID, "/observations")
        if len(set(observation_views)) != len(observation_views):
            _fail(FusionErrorCode.DUPLICATE_ID, "/observations")


@dataclass(frozen=True, slots=True, kw_only=True)
class BoundDimensionDecision:
    """M10 interval decision plus deterministic observation provenance."""

    frame_id: str
    dimension_id: str
    decision: MultiViewDecision
    contributing_observation_ids: tuple[str, ...]
    unknown_observation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "frame_id", _identifier(self.frame_id, "/frame_id"))
        object.__setattr__(
            self,
            "dimension_id",
            _identifier(self.dimension_id, "/dimension_id"),
        )
        if type(self.decision) is not MultiViewDecision:
            _fail(FusionErrorCode.INVALID_INPUT, "/decision")
        for field_name in (
            "contributing_observation_ids",
            "unknown_observation_ids",
        ):
            values = getattr(self, field_name)
            if type(values) is not tuple or len(values) > MAX_FUSION_VIEWS:
                _fail(FusionErrorCode.INVALID_INPUT, f"/{field_name}")
            checked = tuple(
                _identifier(item, f"/{field_name}/{index}") for index, item in enumerate(values)
            )
            if len(set(checked)) != len(checked):
                _fail(FusionErrorCode.DUPLICATE_ID, f"/{field_name}")
            object.__setattr__(self, field_name, checked)
        if set(self.contributing_observation_ids) & set(self.unknown_observation_ids):
            _fail(FusionErrorCode.DUPLICATE_ID)


def _resolve_observation_bindings(
    request: BoundDimensionRequest,
) -> tuple[BoundViewDimension, ...]:
    by_local = {
        (binding.view_id, binding.local_feature_id): binding.canonical_feature_id
        for binding in request.bindings
    }
    expected = {
        request.canonical_start_feature_id,
        request.canonical_end_feature_id,
    }
    for index, observation in enumerate(request.observations):
        start = by_local.get((observation.view_id, observation.local_start_feature_id))
        end = by_local.get((observation.view_id, observation.local_end_feature_id))
        if start is None:
            _fail(FusionErrorCode.UNKNOWN_BINDING, f"/observations/{index}/local_start_feature_id")
        if end is None:
            _fail(FusionErrorCode.UNKNOWN_BINDING, f"/observations/{index}/local_end_feature_id")
        if {start, end} != expected:
            _fail(FusionErrorCode.BINDING_MISMATCH, f"/observations/{index}")
    return tuple(sorted(request.observations, key=lambda item: (item.view_id, item.observation_id)))


def reconcile_bound_dimension(request: BoundDimensionRequest) -> BoundDimensionDecision:
    """Reconcile one explicitly bound distance without fusing homographies."""

    if type(request) is not BoundDimensionRequest:
        _fail(FusionErrorCode.INVALID_INPUT, "/request")
    observations = _resolve_observation_bindings(request)
    dimensions = tuple(
        ViewDimension(
            view_id=observation.view_id,
            estimate=(
                observation.estimate
                if observation.estimate is not None and observation.estimate.decision_eligible
                else None
            ),
        )
        for observation in observations
    )
    decision = reconcile_multiview_dimensions(dimensions)
    observation_by_view = {item.view_id: item.observation_id for item in observations}
    return BoundDimensionDecision(
        frame_id=request.frame_id,
        dimension_id=request.dimension_id,
        decision=decision,
        contributing_observation_ids=tuple(
            observation_by_view[view_id] for view_id in decision.contributing_views
        ),
        unknown_observation_ids=tuple(
            observation_by_view[view_id] for view_id in decision.unknown_views
        ),
    )


__all__ = [
    "MAX_FEATURE_BINDINGS",
    "MAX_FUSION_ID_BYTES",
    "MAX_FUSION_VIEWS",
    "BoundDimensionDecision",
    "BoundDimensionRequest",
    "BoundViewDimension",
    "FusionError",
    "FusionErrorCode",
    "ViewFeatureBinding",
    "reconcile_bound_dimension",
]
