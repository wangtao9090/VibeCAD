"""Evaluator-only target designs for the public Guided Photo v1 fixtures."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from vibecad.parametric import (
    BodyDefinition,
    ConstraintKind,
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
    ReferencePoint,
    SketchConstraint,
    SketchGeometry,
    SketchPlane,
    SketchReference,
    SketchRole,
    UnitSystem,
)


@dataclass(frozen=True, slots=True)
class GuidedPhotoTarget:
    case_id: str
    design: ParametricDesignIR
    depth_parameter_id: str
    expected_bbox_mm: tuple[float, float, float]
    expected_volume_mm3: float
    volume_relative_tolerance: float


def _id(case_id: str, kind: str, token: str) -> str:
    digest = hashlib.sha256(f"guided-photo-v1\0{case_id}\0{kind}\0{token}".encode()).hexdigest()
    return f"ir_{kind}_{digest[:32]}"


def _ref(target: str, point: ReferencePoint) -> SketchReference:
    return SketchReference(target=target, point=point)


class _DesignBuilder:
    def __init__(self, case_id: str) -> None:
        self.case_id = case_id
        self.evidence_id = _id(case_id, "evidence", "confirmed")
        self.parameters: list[DesignParameter] = []
        self._constraint_index = 0

    def parameter(
        self,
        token: str,
        name: str,
        value: float,
        *,
        public: bool = True,
        minimum: float = -1_000.0,
        maximum: float = 1_000.0,
    ) -> str:
        parameter_id = _id(self.case_id, "parameter", token)
        self.parameters.append(
            DesignParameter(
                id=parameter_id,
                name=name,
                kind=ParameterKind.LENGTH,
                value=value,
                unit=DesignUnit.MM,
                evidence_ids=(self.evidence_id,),
                minimum=minimum,
                maximum=maximum,
                public=public,
            )
        )
        return parameter_id

    def constraint(
        self,
        sketch_token: str,
        kind: ConstraintKind,
        references: tuple[SketchReference, ...],
        parameter_id: str | None = None,
    ) -> SketchConstraint:
        self._constraint_index += 1
        return SketchConstraint(
            id=_id(
                self.case_id,
                "constraint",
                f"{sketch_token}-{self._constraint_index}",
            ),
            kind=kind,
            references=references,
            parameter_id=parameter_id,
            evidence_ids=(self.evidence_id,),
        )

    def evidence(self, *fixture_refs: str) -> DesignEvidence:
        return DesignEvidence(
            id=self.evidence_id,
            status=DesignEvidenceStatus.CONFIRMED,
            origin=DesignEvidenceOrigin.USER,
            source_refs=(*fixture_refs, "user:confirmed-measurements"),
            description="Geometry-affecting values were explicitly confirmed by the user.",
        )


def _circle_sketch(
    builder: _DesignBuilder,
    *,
    token: str,
    name: str,
    role: SketchRole,
    diameter_id: str,
    diameter: float,
    center_x_id: str,
    center_x: float,
    center_y_id: str,
    center_y: float,
) -> tuple[ParametricSketch, str]:
    geometry_id = _id(builder.case_id, "geometry", f"{token}-circle")
    center = _ref(geometry_id, ReferencePoint.CENTER)
    origin = _ref("@origin", ReferencePoint.CENTER)
    sketch = ParametricSketch(
        id=_id(builder.case_id, "sketch", token),
        name=name,
        role=role,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=(
            SketchGeometry(
                id=geometry_id,
                kind=GeometryKind.CIRCLE,
                dimensions={"cx_mm": center_x, "cy_mm": center_y, "radius_mm": diameter / 2},
                evidence_ids=(builder.evidence_id,),
            ),
        ),
        constraints=(
            builder.constraint(
                token,
                ConstraintKind.DIAMETER,
                (_ref(geometry_id, ReferencePoint.WHOLE),),
                diameter_id,
            ),
            builder.constraint(
                token,
                ConstraintKind.DISTANCE_X,
                (origin, center),
                center_x_id,
            ),
            builder.constraint(
                token,
                ConstraintKind.DISTANCE_Y,
                (origin, center),
                center_y_id,
            ),
        ),
        evidence_ids=(builder.evidence_id,),
    )
    return sketch, geometry_id


def _rectangle_sketch(
    builder: _DesignBuilder,
    *,
    token: str,
    name: str,
    width_id: str,
    width: float,
    height_id: str,
    height: float,
    x_id: str,
    x: float,
    y_id: str,
    y: float,
) -> ParametricSketch:
    geometry_ids = tuple(_id(builder.case_id, "geometry", f"{token}-{side}") for side in range(4))
    points = (
        (x, y, x + width, y),
        (x + width, y, x + width, y + height),
        (x + width, y + height, x, y + height),
        (x, y + height, x, y),
    )
    geometries = tuple(
        SketchGeometry(
            id=geometry_id,
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": x1, "y1_mm": y1, "x2_mm": x2, "y2_mm": y2},
            evidence_ids=(builder.evidence_id,),
        )
        for geometry_id, (x1, y1, x2, y2) in zip(geometry_ids, points, strict=True)
    )
    constraints: list[SketchConstraint] = []
    for index, geometry_id in enumerate(geometry_ids):
        following = geometry_ids[(index + 1) % len(geometry_ids)]
        constraints.append(
            builder.constraint(
                token,
                ConstraintKind.COINCIDENT,
                (
                    _ref(geometry_id, ReferencePoint.END),
                    _ref(following, ReferencePoint.START),
                ),
            )
        )
    for index, geometry_id in enumerate(geometry_ids):
        constraints.append(
            builder.constraint(
                token,
                ConstraintKind.HORIZONTAL if index % 2 == 0 else ConstraintKind.VERTICAL,
                (_ref(geometry_id, ReferencePoint.WHOLE),),
            )
        )
    constraints.extend(
        (
            builder.constraint(
                token,
                ConstraintKind.LENGTH,
                (_ref(geometry_ids[0], ReferencePoint.WHOLE),),
                width_id,
            ),
            builder.constraint(
                token,
                ConstraintKind.LENGTH,
                (_ref(geometry_ids[1], ReferencePoint.WHOLE),),
                height_id,
            ),
            builder.constraint(
                token,
                ConstraintKind.DISTANCE_X,
                (
                    _ref("@origin", ReferencePoint.CENTER),
                    _ref(geometry_ids[0], ReferencePoint.START),
                ),
                x_id,
            ),
            builder.constraint(
                token,
                ConstraintKind.DISTANCE_Y,
                (
                    _ref("@origin", ReferencePoint.CENTER),
                    _ref(geometry_ids[0], ReferencePoint.START),
                ),
                y_id,
            ),
        )
    )
    return ParametricSketch(
        id=_id(builder.case_id, "sketch", token),
        name=name,
        role=SketchRole.PROFILE,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=geometries,
        constraints=tuple(constraints),
        evidence_ids=(builder.evidence_id,),
    )


def _rounded_rectangle_sketch(
    builder: _DesignBuilder,
    *,
    token: str,
    name: str,
    width_id: str,
    width: float,
    height_id: str,
    height: float,
    radius_id: str,
    radius: float,
    offset_x: float = 0,
    offset_y: float = 0,
) -> ParametricSketch:
    left_arc_x = offset_x + radius
    right_arc_x = offset_x + width - radius
    bottom_arc_y = offset_y + radius
    top_arc_y = offset_y + height - radius
    right_x = offset_x + width
    top_y = offset_y + height
    straight_width = width - 2 * radius
    straight_height = height - 2 * radius
    labels = ("bottom", "br", "right", "tr", "top", "tl", "left", "bl")
    geometry_ids = {label: _id(builder.case_id, "geometry", f"{token}-{label}") for label in labels}
    geometries = (
        SketchGeometry(
            id=geometry_ids["bottom"],
            kind=GeometryKind.LINE,
            dimensions={
                "x1_mm": left_arc_x,
                "y1_mm": offset_y,
                "x2_mm": right_arc_x,
                "y2_mm": offset_y,
            },
            evidence_ids=(builder.evidence_id,),
        ),
        SketchGeometry(
            id=geometry_ids["br"],
            kind=GeometryKind.ARC,
            dimensions={
                "cx_mm": right_arc_x,
                "cy_mm": bottom_arc_y,
                "radius_mm": radius,
                "start_angle_deg": 270,
                "sweep_angle_deg": 90,
            },
            evidence_ids=(builder.evidence_id,),
        ),
        SketchGeometry(
            id=geometry_ids["right"],
            kind=GeometryKind.LINE,
            dimensions={
                "x1_mm": right_x,
                "y1_mm": bottom_arc_y,
                "x2_mm": right_x,
                "y2_mm": top_arc_y,
            },
            evidence_ids=(builder.evidence_id,),
        ),
        SketchGeometry(
            id=geometry_ids["tr"],
            kind=GeometryKind.ARC,
            dimensions={
                "cx_mm": right_arc_x,
                "cy_mm": top_arc_y,
                "radius_mm": radius,
                "start_angle_deg": 0,
                "sweep_angle_deg": 90,
            },
            evidence_ids=(builder.evidence_id,),
        ),
        SketchGeometry(
            id=geometry_ids["top"],
            kind=GeometryKind.LINE,
            dimensions={
                "x1_mm": right_arc_x,
                "y1_mm": top_y,
                "x2_mm": left_arc_x,
                "y2_mm": top_y,
            },
            evidence_ids=(builder.evidence_id,),
        ),
        SketchGeometry(
            id=geometry_ids["tl"],
            kind=GeometryKind.ARC,
            dimensions={
                "cx_mm": left_arc_x,
                "cy_mm": top_arc_y,
                "radius_mm": radius,
                "start_angle_deg": 90,
                "sweep_angle_deg": 90,
            },
            evidence_ids=(builder.evidence_id,),
        ),
        SketchGeometry(
            id=geometry_ids["left"],
            kind=GeometryKind.LINE,
            dimensions={
                "x1_mm": offset_x,
                "y1_mm": top_arc_y,
                "x2_mm": offset_x,
                "y2_mm": bottom_arc_y,
            },
            evidence_ids=(builder.evidence_id,),
        ),
        SketchGeometry(
            id=geometry_ids["bl"],
            kind=GeometryKind.ARC,
            dimensions={
                "cx_mm": left_arc_x,
                "cy_mm": bottom_arc_y,
                "radius_mm": radius,
                "start_angle_deg": 180,
                "sweep_angle_deg": 90,
            },
            evidence_ids=(builder.evidence_id,),
        ),
    )
    coordinate_ids: dict[float, str] = {}

    def coordinate(value: float) -> str:
        canonical = float(value)
        if canonical not in coordinate_ids:
            label = str(canonical).replace("-", "neg-").replace(".", "-")
            coordinate_ids[canonical] = builder.parameter(
                f"{token}-derived-{label}",
                f"{name} derived coordinate {canonical:g}",
                canonical,
                public=False,
            )
        return coordinate_ids[canonical]

    origin = _ref("@origin", ReferencePoint.CENTER)
    line_specs = {
        "bottom": (ConstraintKind.HORIZONTAL, straight_width, left_arc_x, offset_y),
        "right": (ConstraintKind.VERTICAL, straight_height, right_x, bottom_arc_y),
        "top": (ConstraintKind.HORIZONTAL, straight_width, right_arc_x, top_y),
        "left": (ConstraintKind.VERTICAL, straight_height, offset_x, top_arc_y),
    }
    constraints: list[SketchConstraint] = []
    for line, (orientation, length, start_x, start_y) in line_specs.items():
        whole = _ref(geometry_ids[line], ReferencePoint.WHOLE)
        start = _ref(geometry_ids[line], ReferencePoint.START)
        constraints.extend(
            (
                builder.constraint(token, orientation, (whole,)),
                builder.constraint(token, ConstraintKind.LENGTH, (whole,), coordinate(length)),
                builder.constraint(
                    token, ConstraintKind.DISTANCE_X, (origin, start), coordinate(start_x)
                ),
                builder.constraint(
                    token, ConstraintKind.DISTANCE_Y, (origin, start), coordinate(start_y)
                ),
            )
        )

    arc_specs = {
        "br": (
            right_arc_x,
            bottom_arc_y,
            ConstraintKind.DISTANCE_X,
            right_arc_x,
            ConstraintKind.DISTANCE_Y,
            bottom_arc_y,
        ),
        "tr": (
            right_arc_x,
            top_arc_y,
            ConstraintKind.DISTANCE_Y,
            top_arc_y,
            ConstraintKind.DISTANCE_X,
            right_arc_x,
        ),
        "tl": (
            left_arc_x,
            top_arc_y,
            ConstraintKind.DISTANCE_X,
            left_arc_x,
            ConstraintKind.DISTANCE_Y,
            top_arc_y,
        ),
        "bl": (
            left_arc_x,
            bottom_arc_y,
            ConstraintKind.DISTANCE_Y,
            bottom_arc_y,
            ConstraintKind.DISTANCE_X,
            left_arc_x,
        ),
    }
    for arc, (cx, cy, start_kind, start_value, end_kind, end_value) in arc_specs.items():
        whole = _ref(geometry_ids[arc], ReferencePoint.WHOLE)
        center = _ref(geometry_ids[arc], ReferencePoint.CENTER)
        start = _ref(geometry_ids[arc], ReferencePoint.START)
        end = _ref(geometry_ids[arc], ReferencePoint.END)
        constraints.extend(
            (
                builder.constraint(token, ConstraintKind.RADIUS, (whole,), radius_id),
                builder.constraint(
                    token, ConstraintKind.DISTANCE_X, (origin, center), coordinate(cx)
                ),
                builder.constraint(
                    token, ConstraintKind.DISTANCE_Y, (origin, center), coordinate(cy)
                ),
                builder.constraint(token, start_kind, (origin, start), coordinate(start_value)),
                builder.constraint(token, end_kind, (origin, end), coordinate(end_value)),
            )
        )
    return ParametricSketch(
        id=_id(builder.case_id, "sketch", token),
        name=name,
        role=SketchRole.PROFILE,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=geometries,
        constraints=tuple(constraints),
        evidence_ids=(builder.evidence_id,),
    )


def washer_target() -> GuidedPhotoTarget:
    case_id = "guided-photo-washer-ready"
    builder = _DesignBuilder(case_id)
    outer_id = builder.parameter("outer-diameter", "Outer diameter", 20, minimum=0.1)
    inner_id = builder.parameter("inner-diameter", "Inner diameter", 10.5, minimum=0.1)
    depth_id = builder.parameter("depth", "Thickness", 2, minimum=0.1)
    center_x_id = builder.parameter("center-x", "Centre X", 0, public=False)
    center_y_id = builder.parameter("center-y", "Centre Y", 0, public=False)
    outer, _ = _circle_sketch(
        builder,
        token="outer",
        name="Outer profile",
        role=SketchRole.PROFILE,
        diameter_id=outer_id,
        diameter=20,
        center_x_id=center_x_id,
        center_x=0,
        center_y_id=center_y_id,
        center_y=0,
    )
    bore, bore_geometry = _circle_sketch(
        builder,
        token="bore",
        name="Through bore",
        role=SketchRole.HOLE_LOCATIONS,
        diameter_id=inner_id,
        diameter=10.5,
        center_x_id=center_x_id,
        center_x=0,
        center_y_id=center_y_id,
        center_y=0,
    )
    pad_id = _id(case_id, "feature", "pad")
    hole_id = _id(case_id, "feature", "hole")
    design = ParametricDesignIR(
        id=_id(case_id, "design", "target"),
        name="Guided photo washer",
        units=UnitSystem(),
        body=BodyDefinition(id=_id(case_id, "body", "main"), name="Washer body"),
        evidence=(
            builder.evidence(
                "fixture:guided-photo-washer-outer:0",
                "fixture:guided-photo-washer-inner:1",
            ),
        ),
        parameters=tuple(builder.parameters),
        datum_planes=(),
        sketches=(outer, bore),
        features=(
            PartDesignFeature(
                id=pad_id,
                name="Washer pad",
                kind=FeatureKind.PAD,
                sketch_id=outer.id,
                base_feature_id=None,
                parameters={"length": depth_id},
                evidence_ids=(builder.evidence_id,),
                extent=FeatureExtent.LENGTH,
            ),
            PartDesignFeature(
                id=hole_id,
                name="Through bore",
                kind=FeatureKind.HOLE,
                sketch_id=bore.id,
                base_feature_id=pad_id,
                parameters={"diameter": inner_id},
                evidence_ids=(builder.evidence_id,),
                extent=FeatureExtent.THROUGH_ALL,
                location_geometry_ids=(bore_geometry,),
                reversed=True,
            ),
        ),
    )
    return GuidedPhotoTarget(case_id, design, depth_id, (20, 20, 2), 455.1382356888213, 1e-8)


def calibration_block_target() -> GuidedPhotoTarget:
    case_id = "guided-photo-calibration-block-ready"
    builder = _DesignBuilder(case_id)
    outer_width_id = builder.parameter("outer-width", "Outer width", 30, minimum=0.1)
    outer_height_id = builder.parameter("outer-height", "Outer height", 20, minimum=0.1)
    depth_id = builder.parameter("overall-depth", "Overall depth", 10, minimum=0.1)
    pocket_width_id = builder.parameter("pocket-width", "Pocket width", 20, minimum=0.1)
    pocket_height_id = builder.parameter("pocket-height", "Pocket height", 10, minimum=0.1)
    pocket_depth_id = builder.parameter("pocket-depth", "Pocket depth", 7, minimum=0.1)
    outer_x_id = builder.parameter("outer-x", "Outer lower-left X", 0, public=False)
    outer_y_id = builder.parameter("outer-y", "Outer lower-left Y", 0, public=False)
    pocket_x_id = builder.parameter("pocket-x", "Pocket lower-left X", 5, public=False)
    pocket_y_id = builder.parameter("pocket-y", "Pocket lower-left Y", 5, public=False)
    outer = _rectangle_sketch(
        builder,
        token="outer",
        name="Outer rectangle",
        width_id=outer_width_id,
        width=30,
        height_id=outer_height_id,
        height=20,
        x_id=outer_x_id,
        x=0,
        y_id=outer_y_id,
        y=0,
    )
    pocket = _rectangle_sketch(
        builder,
        token="pocket",
        name="Centred blind pocket",
        width_id=pocket_width_id,
        width=20,
        height_id=pocket_height_id,
        height=10,
        x_id=pocket_x_id,
        x=5,
        y_id=pocket_y_id,
        y=5,
    )
    pad_id = _id(case_id, "feature", "pad")
    pocket_id = _id(case_id, "feature", "pocket")
    design = ParametricDesignIR(
        id=_id(case_id, "design", "target"),
        name="Guided photo calibration block",
        units=UnitSystem(),
        body=BodyDefinition(id=_id(case_id, "body", "main"), name="Calibration block body"),
        evidence=(builder.evidence("fixture:guided-photo-calibration-block:0"),),
        parameters=tuple(builder.parameters),
        datum_planes=(),
        sketches=(outer, pocket),
        features=(
            PartDesignFeature(
                id=pad_id,
                name="Outer pad",
                kind=FeatureKind.PAD,
                sketch_id=outer.id,
                base_feature_id=None,
                parameters={"length": depth_id},
                evidence_ids=(builder.evidence_id,),
                extent=FeatureExtent.LENGTH,
            ),
            PartDesignFeature(
                id=pocket_id,
                name="Blind pocket",
                kind=FeatureKind.POCKET,
                sketch_id=pocket.id,
                base_feature_id=pad_id,
                parameters={"length": pocket_depth_id},
                evidence_ids=(builder.evidence_id,),
                extent=FeatureExtent.LENGTH,
                reversed=True,
            ),
        ),
    )
    return GuidedPhotoTarget(case_id, design, depth_id, (30, 20, 10), 4600, 1e-8)


def fan_spacer_target() -> GuidedPhotoTarget:
    case_id = "guided-photo-fan-spacer-ready"
    builder = _DesignBuilder(case_id)
    outer_width_id = builder.parameter("outer-width", "Outer width", 120, minimum=0.1)
    outer_height_id = builder.parameter("outer-height", "Outer height", 120, minimum=0.1)
    depth_id = builder.parameter("depth", "Spacer depth", 5, minimum=0.1)
    outer_radius_id = builder.parameter("outer-radius", "Outer corner radius", 8, minimum=0.1)
    aperture_width_id = builder.parameter("aperture-width", "Aperture width", 116, minimum=0.1)
    aperture_height_id = builder.parameter("aperture-height", "Aperture height", 116, minimum=0.1)
    aperture_radius_id = builder.parameter(
        "aperture-radius", "Aperture corner radius", 34, minimum=0.1
    )
    hole_diameter_id = builder.parameter("hole-diameter", "Mount hole diameter", 5, minimum=0.1)
    outer = _rounded_rectangle_sketch(
        builder,
        token="outer",
        name="Outer rounded square",
        width_id=outer_width_id,
        width=120,
        height_id=outer_height_id,
        height=120,
        radius_id=outer_radius_id,
        radius=8,
    )
    aperture = _rounded_rectangle_sketch(
        builder,
        token="aperture",
        name="Rounded square aperture",
        width_id=aperture_width_id,
        width=116,
        height_id=aperture_height_id,
        height=116,
        radius_id=aperture_radius_id,
        radius=34,
        offset_x=2,
        offset_y=2,
    )
    hole_positions = ((7.5, 7.5), (112.5, 7.5), (112.5, 112.5), (7.5, 112.5))
    hole_geometries: list[SketchGeometry] = []
    hole_constraints: list[SketchConstraint] = []
    location_ids: list[str] = []
    for index, (x, y) in enumerate(hole_positions):
        geometry_id = _id(case_id, "geometry", f"hole-{index}")
        x_id = builder.parameter(f"hole-{index}-x", f"Mount hole {index + 1} X", x, public=False)
        y_id = builder.parameter(f"hole-{index}-y", f"Mount hole {index + 1} Y", y, public=False)
        center = _ref(geometry_id, ReferencePoint.CENTER)
        origin = _ref("@origin", ReferencePoint.CENTER)
        hole_geometries.append(
            SketchGeometry(
                id=geometry_id,
                kind=GeometryKind.CIRCLE,
                dimensions={"cx_mm": x, "cy_mm": y, "radius_mm": 2.5},
                evidence_ids=(builder.evidence_id,),
            )
        )
        hole_constraints.extend(
            (
                builder.constraint(
                    "holes",
                    ConstraintKind.DIAMETER,
                    (_ref(geometry_id, ReferencePoint.WHOLE),),
                    hole_diameter_id,
                ),
                builder.constraint("holes", ConstraintKind.DISTANCE_X, (origin, center), x_id),
                builder.constraint("holes", ConstraintKind.DISTANCE_Y, (origin, center), y_id),
            )
        )
        location_ids.append(geometry_id)
    holes = ParametricSketch(
        id=_id(case_id, "sketch", "holes"),
        name="Four mounting holes",
        role=SketchRole.HOLE_LOCATIONS,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=tuple(hole_geometries),
        constraints=tuple(hole_constraints),
        evidence_ids=(builder.evidence_id,),
    )
    pad_id = _id(case_id, "feature", "pad")
    pocket_id = _id(case_id, "feature", "pocket")
    hole_id = _id(case_id, "feature", "holes")
    design = ParametricDesignIR(
        id=_id(case_id, "design", "target"),
        name="Guided photo 120 mm fan spacer",
        units=UnitSystem(),
        body=BodyDefinition(id=_id(case_id, "body", "main"), name="Fan spacer body"),
        evidence=(builder.evidence("fixture:guided-photo-fan-spacer:0"),),
        parameters=tuple(builder.parameters),
        datum_planes=(),
        sketches=(outer, aperture, holes),
        features=(
            PartDesignFeature(
                id=pad_id,
                name="Outer rounded pad",
                kind=FeatureKind.PAD,
                sketch_id=outer.id,
                base_feature_id=None,
                parameters={"length": depth_id},
                evidence_ids=(builder.evidence_id,),
                extent=FeatureExtent.LENGTH,
            ),
            PartDesignFeature(
                id=pocket_id,
                name="Through aperture",
                kind=FeatureKind.POCKET,
                sketch_id=aperture.id,
                base_feature_id=pad_id,
                parameters={},
                evidence_ids=(builder.evidence_id,),
                extent=FeatureExtent.THROUGH_ALL,
                reversed=True,
            ),
            PartDesignFeature(
                id=hole_id,
                name="Four through mounting holes",
                kind=FeatureKind.HOLE,
                sketch_id=holes.id,
                base_feature_id=pocket_id,
                parameters={"diameter": hole_diameter_id},
                evidence_ids=(builder.evidence_id,),
                extent=FeatureExtent.THROUGH_ALL,
                location_geometry_ids=tuple(location_ids),
                reversed=True,
            ),
        ),
    )
    return GuidedPhotoTarget(case_id, design, depth_id, (120, 120, 5), 9021.7705078125, 0.015)


def guided_photo_targets() -> tuple[GuidedPhotoTarget, ...]:
    return washer_target(), fan_spacer_target(), calibration_block_target()
