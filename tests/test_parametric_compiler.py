"""Focused FreeCAD-bound compiler and parametric observation tests."""

from __future__ import annotations

import math
import os
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.parametric.compiler as compiler_module
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.parametric import (
    BodyDefinition,
    ConstraintKind,
    DesignEvidence,
    DesignEvidenceOrigin,
    DesignEvidenceStatus,
    DesignParameter,
    DesignUnit,
    EdgeTreatmentFeature,
    EdgeTreatmentKind,
    EdgeTreatmentTarget,
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
    SemanticEdgeReference,
    SemanticEdgeRole,
    SketchConstraint,
    SketchGeometry,
    SketchPlane,
    SketchReference,
    SketchRole,
    UnitSystem,
)
from vibecad.parametric.compiler import (
    ParametricCompileError,
    ParametricCompileErrorCode,
    ParametricEntityFact,
    compile_design_sketches,
    stabilize_parametric_session,
)


def _id(kind: str, value: int) -> str:
    return f"ir_{kind}_{value:032x}"


EVIDENCE = _id("evidence", 1)
WIDTH = _id("parameter", 1)
HEIGHT = _id("parameter", 2)
DEPTH = _id("parameter", 3)
SKETCH = _id("sketch", 1)
BOTTOM = _id("geometry", 1)
RIGHT = _id("geometry", 2)
TOP = _id("geometry", 3)
LEFT = _id("geometry", 4)


def _reference(target: str, point: ReferencePoint) -> SketchReference:
    return SketchReference(target=target, point=point)


def _rectangle_design() -> ParametricDesignIR:
    geometries = (
        SketchGeometry(
            id=BOTTOM,
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": 0, "y1_mm": 0, "x2_mm": 60, "y2_mm": 0},
        ),
        SketchGeometry(
            id=RIGHT,
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": 60, "y1_mm": 0, "x2_mm": 60, "y2_mm": 40},
        ),
        SketchGeometry(
            id=TOP,
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": 60, "y1_mm": 40, "x2_mm": 0, "y2_mm": 40},
        ),
        SketchGeometry(
            id=LEFT,
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": 0, "y1_mm": 40, "x2_mm": 0, "y2_mm": 0},
        ),
    )
    constraint_specs = (
        (
            ConstraintKind.COINCIDENT,
            (_reference(BOTTOM, ReferencePoint.END), _reference(RIGHT, ReferencePoint.START)),
            None,
        ),
        (
            ConstraintKind.COINCIDENT,
            (_reference(RIGHT, ReferencePoint.END), _reference(TOP, ReferencePoint.START)),
            None,
        ),
        (
            ConstraintKind.COINCIDENT,
            (_reference(TOP, ReferencePoint.END), _reference(LEFT, ReferencePoint.START)),
            None,
        ),
        (
            ConstraintKind.COINCIDENT,
            (_reference(LEFT, ReferencePoint.END), _reference(BOTTOM, ReferencePoint.START)),
            None,
        ),
        (ConstraintKind.HORIZONTAL, (_reference(BOTTOM, ReferencePoint.WHOLE),), None),
        (ConstraintKind.VERTICAL, (_reference(RIGHT, ReferencePoint.WHOLE),), None),
        (ConstraintKind.HORIZONTAL, (_reference(TOP, ReferencePoint.WHOLE),), None),
        (ConstraintKind.VERTICAL, (_reference(LEFT, ReferencePoint.WHOLE),), None),
        (ConstraintKind.LENGTH, (_reference(BOTTOM, ReferencePoint.WHOLE),), WIDTH),
        (ConstraintKind.LENGTH, (_reference(RIGHT, ReferencePoint.WHOLE),), HEIGHT),
        (
            ConstraintKind.COINCIDENT,
            (
                _reference(BOTTOM, ReferencePoint.START),
                _reference("@origin", ReferencePoint.CENTER),
            ),
            None,
        ),
    )
    constraints = tuple(
        SketchConstraint(
            id=_id("constraint", index + 1),
            kind=kind,
            references=references,
            parameter_id=parameter_id,
            evidence_ids=((EVIDENCE,) if parameter_id is not None else ()),
        )
        for index, (kind, references, parameter_id) in enumerate(constraint_specs)
    )
    sketch = ParametricSketch(
        id=SKETCH,
        name="Constrained rectangle",
        role=SketchRole.PROFILE,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=geometries,
        constraints=constraints,
        evidence_ids=(EVIDENCE,),
    )
    parameters = tuple(
        DesignParameter(
            id=parameter_id,
            name=name,
            kind=ParameterKind.LENGTH,
            value=value,
            unit=DesignUnit.MM,
            evidence_ids=(EVIDENCE,),
            minimum=0.1,
            maximum=1_000,
        )
        for parameter_id, name, value in (
            (WIDTH, "Width", 60),
            (HEIGHT, "Height", 40),
            (DEPTH, "Depth", 8),
        )
    )
    return ParametricDesignIR(
        id=_id("design", 1),
        name="Rectangle",
        units=UnitSystem(),
        body=BodyDefinition(id=_id("body", 1), name="Rectangle body"),
        evidence=(
            DesignEvidence(
                id=EVIDENCE,
                status=DesignEvidenceStatus.CONFIRMED,
                origin=DesignEvidenceOrigin.USER,
                source_refs=("test:rectangle",),
            ),
        ),
        parameters=parameters,
        datum_planes=(),
        sketches=(sketch,),
        features=(
            PartDesignFeature(
                id=_id("feature", 1),
                name="Pad",
                kind=FeatureKind.PAD,
                sketch_id=SKETCH,
                base_feature_id=None,
                parameters={"length": DEPTH},
                evidence_ids=(EVIDENCE,),
                extent=FeatureExtent.LENGTH,
            ),
        ),
    )


def _edge_treatment_design(
    kind: EdgeTreatmentKind = EdgeTreatmentKind.FILLET,
) -> ParametricDesignIR:
    base = _rectangle_design()
    start_id = _id("parameter", 20)
    end_id = start_id if kind is EdgeTreatmentKind.CHAMFER else _id("parameter", 21)
    extra_parameters = (
        DesignParameter(
            id=start_id,
            name="Edge treatment start",
            kind=ParameterKind.LENGTH,
            value=1,
            unit=DesignUnit.MM,
            evidence_ids=(EVIDENCE,),
            minimum=0.1,
            maximum=100,
        ),
    )
    if end_id != start_id:
        extra_parameters += (
            DesignParameter(
                id=end_id,
                name="Edge treatment end",
                kind=ParameterKind.LENGTH,
                value=3,
                unit=DesignUnit.MM,
                evidence_ids=(EVIDENCE,),
                minimum=0.1,
                maximum=100,
            ),
        )
    return replace(
        base,
        parameters=base.parameters + extra_parameters,
        edge_treatments=(
            EdgeTreatmentFeature(
                id=_id("feature", 2),
                name="Edge treatment",
                kind=kind,
                base_feature_id=base.features[-1].id,
                targets=(
                    EdgeTreatmentTarget(
                        edge=SemanticEdgeReference(
                            source_feature_id=base.features[-1].id,
                            geometry_id=BOTTOM,
                            role=SemanticEdgeRole.SWEEP,
                            point=ReferencePoint.START,
                        ),
                        start_parameter_id=start_id,
                        end_parameter_id=end_id,
                    ),
                ),
                evidence_ids=(EVIDENCE,),
            ),
        ),
    )


def _with_constant_fillet(
    base: ParametricDesignIR,
    *,
    geometry_id: str,
    role: SemanticEdgeRole,
    point: ReferencePoint,
) -> ParametricDesignIR:
    radius_id = _id("parameter", 90)
    source_feature = base.features[-1]
    return replace(
        base,
        parameters=base.parameters
        + (
            DesignParameter(
                id=radius_id,
                name="Constant fillet radius",
                kind=ParameterKind.LENGTH,
                value=1,
                unit=DesignUnit.MM,
                evidence_ids=(base.evidence[0].id,),
                minimum=0.1,
                maximum=100,
            ),
        ),
        edge_treatments=(
            EdgeTreatmentFeature(
                id=_id("feature", 90),
                name="Constant fillet",
                kind=EdgeTreatmentKind.FILLET,
                base_feature_id=source_feature.id,
                targets=(
                    EdgeTreatmentTarget(
                        edge=SemanticEdgeReference(
                            source_feature_id=source_feature.id,
                            geometry_id=geometry_id,
                            role=role,
                            point=point,
                        ),
                        start_parameter_id=radius_id,
                        end_parameter_id=radius_id,
                    ),
                ),
                evidence_ids=(base.evidence[0].id,),
            ),
        ),
    )


def _multi_edge_fillet_design() -> ParametricDesignIR:
    base = _edge_treatment_design()
    radius_id = _id("parameter", 22)
    parameter = DesignParameter(
        id=radius_id,
        name="Second edge radius",
        kind=ParameterKind.LENGTH,
        value=2,
        unit=DesignUnit.MM,
        evidence_ids=(EVIDENCE,),
        minimum=0.1,
        maximum=100,
    )
    treatment = base.edge_treatments[0]
    return replace(
        base,
        parameters=base.parameters + (parameter,),
        edge_treatments=(
            replace(
                treatment,
                targets=treatment.targets
                + (
                    EdgeTreatmentTarget(
                        edge=SemanticEdgeReference(
                            source_feature_id=base.features[-1].id,
                            geometry_id=BOTTOM,
                            role=SemanticEdgeRole.SWEEP,
                            point=ReferencePoint.END,
                        ),
                        start_parameter_id=radius_id,
                        end_parameter_id=radius_id,
                    ),
                ),
            ),
        ),
    )


def _revolve_edge_treatment_design() -> ParametricDesignIR:
    base = _rectangle_design()
    parameters = tuple(
        replace(
            parameter,
            name="Revolve angle",
            kind=ParameterKind.ANGLE,
            value=360,
            unit=DesignUnit.DEG,
            minimum=1,
            maximum=360,
        )
        if parameter.id == DEPTH
        else parameter
        for parameter in base.parameters
    )
    feature = replace(
        base.features[0],
        name="Revolve",
        kind=FeatureKind.REVOLVE,
        parameters={"angle": DEPTH},
        extent=None,
        axis="@sketch_y",
    )
    revolved = replace(base, parameters=parameters, features=(feature,))
    return _with_constant_fillet(
        revolved,
        geometry_id=BOTTOM,
        role=SemanticEdgeRole.SWEEP,
        point=ReferencePoint.END,
    )


def _slot_design(*, vertical: bool = False) -> ParametricDesignIR:
    base = _rectangle_design()
    slot = SketchGeometry(
        id=BOTTOM,
        kind=GeometryKind.SLOT,
        dimensions=(
            {
                "x1_mm": 30,
                "y1_mm": 5,
                "x2_mm": 30,
                "y2_mm": 35,
                "width_mm": 6,
            }
            if vertical
            else {
                "x1_mm": 15,
                "y1_mm": 20,
                "x2_mm": 45,
                "y2_mm": 20,
                "width_mm": 6,
            }
        ),
        evidence_ids=(EVIDENCE,),
    )
    sketch = replace(
        base.sketches[0],
        name="Horizontal slot profile",
        geometries=(slot,),
        constraints=(),
    )
    return replace(base, name="Native slot", sketches=(sketch,))


def _multi_hole_design() -> ParametricDesignIR:
    base = _rectangle_design()
    diameter_id = _id("parameter", 20)
    x_left_id = _id("parameter", 21)
    x_right_id = _id("parameter", 22)
    y_id = _id("parameter", 23)
    location_ids = (_id("geometry", 20), _id("geometry", 21))
    parameters = base.parameters + tuple(
        DesignParameter(
            id=parameter_id,
            name=name,
            kind=ParameterKind.LENGTH,
            value=value,
            unit=DesignUnit.MM,
            evidence_ids=(EVIDENCE,),
            minimum=0.1,
            maximum=1_000,
            public=public,
        )
        for parameter_id, name, value, public in (
            (diameter_id, "Hole diameter", 6, True),
            (x_left_id, "Left hole X", 15, True),
            (x_right_id, "Right hole X", 45, True),
            (y_id, "Hole Y", 20, True),
        )
    )
    origin = _reference("@origin", ReferencePoint.CENTER)
    constraints: list[SketchConstraint] = []
    for index, (geometry_id, x_parameter_id) in enumerate(
        zip(location_ids, (x_left_id, x_right_id), strict=True)
    ):
        center = _reference(geometry_id, ReferencePoint.CENTER)
        constraints.extend(
            (
                SketchConstraint(
                    id=_id("constraint", 30 + index * 3),
                    kind=ConstraintKind.DIAMETER,
                    references=(_reference(geometry_id, ReferencePoint.WHOLE),),
                    parameter_id=diameter_id,
                    evidence_ids=(EVIDENCE,),
                ),
                SketchConstraint(
                    id=_id("constraint", 31 + index * 3),
                    kind=ConstraintKind.DISTANCE_X,
                    references=(origin, center),
                    parameter_id=x_parameter_id,
                    evidence_ids=(EVIDENCE,),
                ),
                SketchConstraint(
                    id=_id("constraint", 32 + index * 3),
                    kind=ConstraintKind.DISTANCE_Y,
                    references=(origin, center),
                    parameter_id=y_id,
                    evidence_ids=(EVIDENCE,),
                ),
            )
        )
    hole_sketch = ParametricSketch(
        id=_id("sketch", 20),
        name="Two mounting holes",
        role=SketchRole.HOLE_LOCATIONS,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=tuple(
            SketchGeometry(
                id=geometry_id,
                kind=GeometryKind.CIRCLE,
                dimensions={"cx_mm": x, "cy_mm": 20, "radius_mm": 3},
                evidence_ids=(EVIDENCE,),
            )
            for geometry_id, x in zip(location_ids, (15, 45), strict=True)
        ),
        constraints=tuple(constraints),
        evidence_ids=(EVIDENCE,),
    )
    hole = PartDesignFeature(
        id=_id("feature", 20),
        name="Two through holes",
        kind=FeatureKind.HOLE,
        sketch_id=hole_sketch.id,
        base_feature_id=base.features[-1].id,
        parameters={"diameter": diameter_id},
        evidence_ids=(EVIDENCE,),
        extent=FeatureExtent.THROUGH_ALL,
        location_geometry_ids=location_ids,
        reversed=True,
    )
    return replace(
        base,
        parameters=parameters,
        sketches=base.sketches + (hole_sketch,),
        features=base.features + (hole,),
    )


def _multi_view_l_bracket_design() -> ParametricDesignIR:
    evidence_id = _id("evidence", 35)
    parameter_specs = (
        ("width", "Overall width", 50),
        ("depth", "Overall depth", 40),
        ("length", "Extrusion length", 60),
        ("thickness", "Leg thickness", 8),
        ("hole_diameter", "Hole diameter", 6),
        ("hole_a_x", "Horizontal hole A X", 22),
        ("hole_a_z", "Horizontal hole A Z", 18),
        ("hole_b_x", "Horizontal hole B X", 36),
        ("hole_b_z", "Horizontal hole B Z", 42),
        ("hole_c_y", "Vertical hole Y", 24),
        ("hole_c_z", "Vertical hole Z", 30),
    )
    parameter_ids = {
        key: _id("parameter", 100 + index)
        for index, (key, _name, _value) in enumerate(parameter_specs)
    }
    parameters = tuple(
        DesignParameter(
            id=parameter_ids[key],
            name=name,
            kind=ParameterKind.LENGTH,
            value=value,
            unit=DesignUnit.MM,
            evidence_ids=(evidence_id,),
            minimum=0.1,
            maximum=1_000,
        )
        for key, name, value in parameter_specs
    )

    profile_ids = tuple(_id("geometry", 100 + index) for index in range(6))
    profile_points = (
        (0, 0, 50, 0),
        (50, 0, 50, 8),
        (50, 8, 8, 8),
        (8, 8, 8, 40),
        (8, 40, 0, 40),
        (0, 40, 0, 0),
    )
    profile_geometry = tuple(
        SketchGeometry(
            id=geometry_id,
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": x1, "y1_mm": y1, "x2_mm": x2, "y2_mm": y2},
            evidence_ids=(evidence_id,),
        )
        for geometry_id, (x1, y1, x2, y2) in zip(profile_ids, profile_points, strict=True)
    )
    profile_constraints: list[SketchConstraint] = []

    def add_constraint(
        kind: ConstraintKind,
        references: tuple[SketchReference, ...],
        parameter_key: str | None = None,
    ) -> None:
        profile_constraints.append(
            SketchConstraint(
                id=_id("constraint", 100 + len(profile_constraints)),
                kind=kind,
                references=references,
                parameter_id=(None if parameter_key is None else parameter_ids[parameter_key]),
                evidence_ids=(evidence_id,),
            )
        )

    for index, geometry_id in enumerate(profile_ids):
        add_constraint(
            ConstraintKind.COINCIDENT,
            (
                _reference(geometry_id, ReferencePoint.END),
                _reference(profile_ids[(index + 1) % len(profile_ids)], ReferencePoint.START),
            ),
        )
    for geometry_id in (profile_ids[0], profile_ids[2], profile_ids[4]):
        add_constraint(
            ConstraintKind.HORIZONTAL,
            (_reference(geometry_id, ReferencePoint.WHOLE),),
        )
    for geometry_id in (profile_ids[1], profile_ids[3], profile_ids[5]):
        add_constraint(
            ConstraintKind.VERTICAL,
            (_reference(geometry_id, ReferencePoint.WHOLE),),
        )
    add_constraint(
        ConstraintKind.COINCIDENT,
        (
            _reference(profile_ids[0], ReferencePoint.START),
            _reference("@origin", ReferencePoint.CENTER),
        ),
    )
    for geometry_id, parameter_key in (
        (profile_ids[0], "width"),
        (profile_ids[5], "depth"),
        (profile_ids[1], "thickness"),
        (profile_ids[4], "thickness"),
    ):
        add_constraint(
            ConstraintKind.LENGTH,
            (_reference(geometry_id, ReferencePoint.WHOLE),),
            parameter_key,
        )
    profile_sketch = ParametricSketch(
        id=_id("sketch", 100),
        name="L bracket profile",
        role=SketchRole.PROFILE,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=profile_geometry,
        constraints=tuple(profile_constraints),
        evidence_ids=(evidence_id,),
    )

    def hole_sketch(
        *,
        sketch_number: int,
        plane: OriginPlane,
        name: str,
        locations: tuple[tuple[str, str, str], ...],
    ) -> tuple[ParametricSketch, tuple[str, ...]]:
        geometries: list[SketchGeometry] = []
        constraints: list[SketchConstraint] = []
        location_ids: list[str] = []
        origin = _reference("@origin", ReferencePoint.CENTER)
        for index, (x_key, y_key, _label) in enumerate(locations):
            geometry_id = _id("geometry", sketch_number * 10 + index)
            location_ids.append(geometry_id)
            geometries.append(
                SketchGeometry(
                    id=geometry_id,
                    kind=GeometryKind.CIRCLE,
                    dimensions={
                        "cx_mm": next(
                            value for key, _name, value in parameter_specs if key == x_key
                        ),
                        "cy_mm": next(
                            value for key, _name, value in parameter_specs if key == y_key
                        ),
                        "radius_mm": 3,
                    },
                    evidence_ids=(evidence_id,),
                )
            )
            center = _reference(geometry_id, ReferencePoint.CENTER)
            for kind, references, parameter_key in (
                (
                    ConstraintKind.DIAMETER,
                    (_reference(geometry_id, ReferencePoint.WHOLE),),
                    "hole_diameter",
                ),
                (ConstraintKind.DISTANCE_X, (origin, center), x_key),
                (ConstraintKind.DISTANCE_Y, (origin, center), y_key),
            ):
                constraints.append(
                    SketchConstraint(
                        id=_id(
                            "constraint",
                            sketch_number * 10 + index * 3 + len(constraints) % 3,
                        ),
                        kind=kind,
                        references=references,
                        parameter_id=parameter_ids[parameter_key],
                        evidence_ids=(evidence_id,),
                    )
                )
        return (
            ParametricSketch(
                id=_id("sketch", sketch_number),
                name=name,
                role=SketchRole.HOLE_LOCATIONS,
                plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=plane),
                geometries=tuple(geometries),
                constraints=tuple(constraints),
                evidence_ids=(evidence_id,),
            ),
            tuple(location_ids),
        )

    horizontal_holes, horizontal_location_ids = hole_sketch(
        sketch_number=110,
        plane=OriginPlane.XZ,
        name="Horizontal leg holes",
        locations=(
            ("hole_a_x", "hole_a_z", "A"),
            ("hole_b_x", "hole_b_z", "B"),
        ),
    )
    vertical_hole, vertical_location_ids = hole_sketch(
        sketch_number=120,
        plane=OriginPlane.YZ,
        name="Vertical leg hole",
        locations=(("hole_c_y", "hole_c_z", "C"),),
    )
    feature_ids = tuple(_id("feature", 100 + index) for index in range(3))
    return ParametricDesignIR(
        id=_id("design", 35),
        name="Three-view L bracket",
        units=UnitSystem(),
        body=BodyDefinition(id=_id("body", 35), name="L bracket body"),
        evidence=(
            DesignEvidence(
                id=evidence_id,
                status=DesignEvidenceStatus.CROSS_VIEW_DERIVED,
                origin=DesignEvidenceOrigin.MULTI_VIEW,
                source_refs=(
                    "fixture:visual-cad-l-bracket-front:0",
                    "fixture:visual-cad-l-bracket-right:1",
                    "fixture:visual-cad-l-bracket-top:2",
                ),
                description="Confirmed dimensions reconciled across three orthographic views.",
            ),
        ),
        parameters=parameters,
        datum_planes=(),
        sketches=(profile_sketch, horizontal_holes, vertical_hole),
        features=(
            PartDesignFeature(
                id=feature_ids[0],
                name="L bracket pad",
                kind=FeatureKind.PAD,
                sketch_id=profile_sketch.id,
                base_feature_id=None,
                parameters={"length": parameter_ids["length"]},
                evidence_ids=(evidence_id,),
                extent=FeatureExtent.LENGTH,
            ),
            PartDesignFeature(
                id=feature_ids[1],
                name="Two horizontal through holes",
                kind=FeatureKind.HOLE,
                sketch_id=horizontal_holes.id,
                base_feature_id=feature_ids[0],
                parameters={"diameter": parameter_ids["hole_diameter"]},
                evidence_ids=(evidence_id,),
                extent=FeatureExtent.THROUGH_ALL,
                location_geometry_ids=horizontal_location_ids,
                reversed=False,
            ),
            PartDesignFeature(
                id=feature_ids[2],
                name="Vertical through hole",
                kind=FeatureKind.HOLE,
                sketch_id=vertical_hole.id,
                base_feature_id=feature_ids[1],
                parameters={"diameter": parameter_ids["hole_diameter"]},
                evidence_ids=(evidence_id,),
                extent=FeatureExtent.THROUGH_ALL,
                location_geometry_ids=vertical_location_ids,
                reversed=True,
            ),
        ),
    )


def _maximum_feature_design() -> ParametricDesignIR:
    base = _rectangle_design()
    radius_id = _id("parameter", 10)
    depth_id = _id("parameter", 11)
    parameters = [
        DesignParameter(
            id=radius_id,
            name="Radius",
            kind=ParameterKind.LENGTH,
            value=10,
            unit=DesignUnit.MM,
            evidence_ids=(EVIDENCE,),
            minimum=0.1,
            maximum=1_000,
        ),
        DesignParameter(
            id=depth_id,
            name="Depth",
            kind=ParameterKind.LENGTH,
            value=8,
            unit=DesignUnit.MM,
            evidence_ids=(EVIDENCE,),
            minimum=0.1,
            maximum=1_000,
        ),
    ]
    sketches: list[ParametricSketch] = []
    features: list[PartDesignFeature] = []
    previous_feature_id = None
    for design_index in range(1, 9):
        x_offset = (design_index - 1) * 15
        y_offset = 0 if design_index == 1 else 1
        geometry_id = _id("geometry", design_index)
        center = SketchReference(target=geometry_id, point=ReferencePoint.CENTER)
        origin = SketchReference(target="@origin", point=ReferencePoint.CENTER)
        constraints = [
            SketchConstraint(
                id=_id("constraint", design_index * 8 + 1),
                kind=ConstraintKind.RADIUS,
                references=(SketchReference(target=geometry_id, point=ReferencePoint.WHOLE),),
                parameter_id=radius_id,
                evidence_ids=(EVIDENCE,),
            )
        ]
        if design_index == 1:
            constraints.append(
                SketchConstraint(
                    id=_id("constraint", design_index * 8 + 2),
                    kind=ConstraintKind.COINCIDENT,
                    references=(center, origin),
                )
            )
        else:
            x_parameter = replace(
                parameters[0],
                id=_id("parameter", 100 + design_index * 2),
                name=f"Offset X {design_index}",
                value=x_offset,
                public=False,
            )
            y_parameter = replace(
                parameters[0],
                id=_id("parameter", 101 + design_index * 2),
                name=f"Offset Y {design_index}",
                value=y_offset,
                public=False,
            )
            parameters.extend((x_parameter, y_parameter))
            constraints.extend(
                (
                    SketchConstraint(
                        id=_id("constraint", design_index * 8 + 2),
                        kind=ConstraintKind.DISTANCE_X,
                        references=(origin, center),
                        parameter_id=x_parameter.id,
                        evidence_ids=(EVIDENCE,),
                    ),
                    SketchConstraint(
                        id=_id("constraint", design_index * 8 + 3),
                        kind=ConstraintKind.DISTANCE_Y,
                        references=(origin, center),
                        parameter_id=y_parameter.id,
                        evidence_ids=(EVIDENCE,),
                    ),
                )
            )
        sketch_id = _id("sketch", design_index)
        feature_id = _id("feature", design_index)
        sketches.append(
            ParametricSketch(
                id=sketch_id,
                name=f"Constrained circle {design_index}",
                role=SketchRole.PROFILE,
                plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
                geometries=(
                    SketchGeometry(
                        id=geometry_id,
                        kind=GeometryKind.CIRCLE,
                        dimensions={
                            "cx_mm": x_offset,
                            "cy_mm": y_offset,
                            "radius_mm": 10,
                        },
                    ),
                ),
                constraints=tuple(constraints),
                evidence_ids=(EVIDENCE,),
            )
        )
        features.append(
            PartDesignFeature(
                id=feature_id,
                name=f"Pad {design_index}",
                kind=FeatureKind.PAD,
                sketch_id=sketch_id,
                base_feature_id=previous_feature_id,
                parameters={"length": depth_id},
                evidence_ids=(EVIDENCE,),
                extent=FeatureExtent.LENGTH,
            )
        )
        previous_feature_id = feature_id
    return replace(
        base,
        parameters=tuple(parameters),
        sketches=tuple(sketches),
        features=tuple(features),
    )


def test_compiler_rejects_non_ir_before_loading_cad_runtime() -> None:
    with pytest.raises(ParametricCompileError) as caught:
        compile_design_sketches(object(), object())

    assert caught.value.code is ParametricCompileErrorCode.INVALID_INPUT
    assert caught.value.path == "/design"


def test_slot_profile_expands_to_four_native_edges() -> None:
    design = _rectangle_design()
    encoded = design.to_mapping()
    encoded["sketches"][0]["geometries"][0] = {
        "schema_version": 1,
        "id": BOTTOM,
        "kind": "slot",
        "dimensions": {
            "x1_mm": 0,
            "y1_mm": 0,
            "x2_mm": 60,
            "y2_mm": 0,
            "width_mm": 5,
        },
        "construction": False,
        "evidence_ids": [],
    }
    encoded["sketches"][0]["geometries"] = [encoded["sketches"][0]["geometries"][0]]
    encoded["sketches"][0]["constraints"] = []
    slot_design = ParametricDesignIR.from_mapping(encoded)

    assert compiler_module._expected_profile_edge_count(slot_design.sketches[0]) == 4


def test_compiler_fails_closed_on_oblique_slot_before_cad_mutation() -> None:
    design = _rectangle_design()
    encoded = design.to_mapping()
    encoded["sketches"][0]["geometries"][0] = {
        "schema_version": 1,
        "id": BOTTOM,
        "kind": "slot",
        "dimensions": {
            "x1_mm": 0,
            "y1_mm": 0,
            "x2_mm": 60,
            "y2_mm": 10,
            "width_mm": 5,
        },
        "construction": False,
        "evidence_ids": [],
    }
    encoded["sketches"][0]["geometries"] = [encoded["sketches"][0]["geometries"][0]]
    encoded["sketches"][0]["constraints"] = []
    slot_design = ParametricDesignIR.from_mapping(encoded)

    class EmptyDocument:
        Objects: tuple[object, ...] = ()
        UndoMode = 1

    class EmptySession:
        doc = EmptyDocument()
        transaction_started = False

        @contextmanager
        def _transaction(self, _label: str, *, claim_new_objects: bool):
            assert claim_new_objects is False
            self.transaction_started = True
            yield

    session = EmptySession()
    with pytest.raises(ParametricCompileError) as caught:
        compile_design_sketches(session, slot_design)

    assert caught.value.code is ParametricCompileErrorCode.UNSUPPORTED
    assert caught.value.path == "/sketches/0/geometries/0/dimensions"
    assert session.transaction_started is False


def test_compiler_rejects_ir_constraints_on_atomic_slot_before_cad_mutation() -> None:
    design = _slot_design()
    slot_constraint = SketchConstraint(
        id=_id("constraint", 70),
        kind=ConstraintKind.DISTANCE_X,
        references=(
            _reference("@origin", ReferencePoint.CENTER),
            _reference(BOTTOM, ReferencePoint.CENTER),
        ),
        parameter_id=WIDTH,
        evidence_ids=(EVIDENCE,),
    )
    design = replace(
        design,
        sketches=(replace(design.sketches[0], constraints=(slot_constraint,)),),
    )

    class EmptySession:
        doc = SimpleNamespace(Objects=(), UndoMode=1)
        transaction_started = False

        @contextmanager
        def _transaction(self, _label: str, *, claim_new_objects: bool):
            self.transaction_started = True
            yield

    session = EmptySession()
    with pytest.raises(ParametricCompileError) as caught:
        compile_design_sketches(session, design)

    assert caught.value.code is ParametricCompileErrorCode.UNSUPPORTED
    assert caught.value.path == "/sketches/0/constraints/0/references/1/target"
    assert session.transaction_started is False


def test_compiler_rejects_a_session_without_atomic_undo() -> None:
    class NonAtomicDocument:
        Objects: tuple[object, ...] = ()
        UndoMode = 0

    class NonAtomicSession:
        doc = NonAtomicDocument()

        @contextmanager
        def _transaction(self, _label: str, *, claim_new_objects: bool):
            yield

    with pytest.raises(ParametricCompileError) as caught:
        compile_design_sketches(NonAtomicSession(), _rectangle_design())

    assert caught.value.code is ParametricCompileErrorCode.INVALID_INPUT
    assert caught.value.path == "/session"


def test_point_whole_reference_maps_to_freecad_point_code() -> None:
    geometry = SketchGeometry(
        id=BOTTOM,
        kind=GeometryKind.POINT,
        dimensions={"x_mm": 0, "y_mm": 0},
    )
    reference = SketchReference(target=BOTTOM, point=ReferencePoint.WHOLE)

    assert compiler_module._point_code(reference, geometry) == 1


def test_solver_failures_cannot_leave_the_compiler_as_success() -> None:
    facts = compiler_module.SketchSolverFacts(
        solve_result=-3,
        dof=3,
        fully_constrained=False,
        geometry_count=1,
        constraint_count=2,
        conflicting_constraint_count=2,
        redundant_constraint_count=0,
        malformed_constraint_count=0,
    )

    with pytest.raises(ParametricCompileError) as caught:
        compiler_module._require_solver_success(facts)

    assert caught.value.code is ParametricCompileErrorCode.SOLVER_FAILURE


def test_stabilization_solves_all_sketches_before_recompute_and_shape_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    sketch_a = SimpleNamespace(name="sketch-a")
    sketch_b = SimpleNamespace(name="sketch-b")
    body = SimpleNamespace(name="body")
    carrier = SimpleNamespace(name="parameters")
    feature = SimpleNamespace(name="feature")
    records = (
        (body, {"kind": "body"}),
        (carrier, {"kind": "parameters"}),
        (sketch_a, {"kind": "sketch"}),
        (sketch_b, {"kind": "sketch"}),
        (feature, {"kind": "feature"}),
    )
    solver = compiler_module.SketchSolverFacts(
        solve_result=0,
        dof=0,
        fully_constrained=True,
        geometry_count=4,
        constraint_count=9,
        conflicting_constraint_count=0,
        redundant_constraint_count=0,
        malformed_constraint_count=0,
    )

    monkeypatch.setattr(compiler_module, "_parametric_records", lambda _document: records)
    monkeypatch.setattr(
        compiler_module,
        "_validate_sketch_metadata",
        lambda obj, _data: events.append(f"solve:{obj.name}") or solver,
    )
    monkeypatch.setattr(
        compiler_module,
        "_validate_parametric_graph",
        lambda _records: events.append("graph"),
    )
    monkeypatch.setattr(
        compiler_module,
        "parametric_entity_facts",
        lambda obj: events.append(f"facts:{obj.name}") or (),
    )
    document = SimpleNamespace(recompute=lambda: events.append("recompute"))

    stabilize_parametric_session(SimpleNamespace(doc=document))

    assert events == [
        "solve:sketch-a",
        "solve:sketch-b",
        "recompute",
        "graph",
        "facts:body",
        "facts:parameters",
        "facts:feature",
    ]


@pytest.mark.slow
@pytest.mark.parametrize("vertical", (False, True), ids=("horizontal", "vertical"))
def test_real_slot_compiles_to_fully_constrained_editable_native_geometry(
    vertical: bool,
) -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    session = Session()
    session.open_document(f"ParametricNativeSlot{'Vertical' if vertical else 'Horizontal'}")
    try:
        design = _slot_design(vertical=vertical)
        compiled = compiler_module.compile_parametric_design(session, design)
        stabilize_parametric_session(session)

        sketch = compiled.sketches[0]
        assert sketch.geometry_indices[BOTTOM] == (0, 1, 2, 3)
        assert sketch.solver.geometry_count == 4
        assert sketch.solver.constraint_count == 14
        assert sketch.solver.dof == 0
        assert sketch.solver.fully_constrained is True
        assert tuple(item.TypeId for item in sketch.object.Geometry) == (
            "Part::GeomLineSegment",
            "Part::GeomArcOfCircle",
            "Part::GeomLineSegment",
            "Part::GeomArcOfCircle",
        )
        expected_area = 6 * 30 + math.pi * 3**2
        assert float(compiled.body.Shape.Volume) == pytest.approx(expected_area * 8, abs=1e-6)

        radius_constraint = next(
            index
            for index, constraint in enumerate(sketch.object.Constraints)
            if constraint.Type == "Radius"
        )
        sketch.object.setDatum(radius_constraint, 4.0)
        stabilize_parametric_session(session)
        edited_slot_area = 8 * 30 + math.pi * 4**2
        assert float(compiled.body.Shape.Volume) == pytest.approx(
            edited_slot_area * 8,
            abs=1e-6,
        )

        edit = compiler_module.modify_parametric_parameter(
            session,
            design,
            body=compiled.body,
            parameter_id=DEPTH,
            value=10,
        )
        stabilize_parametric_session(session)
        assert (edit.before_value, edit.after_value) == (8, 10)
        assert float(compiled.body.Shape.Volume) == pytest.approx(
            edited_slot_area * 10,
            abs=1e-6,
        )
        slot_facts = {
            fact.name: fact.value for fact in compiler_module.parametric_entity_facts(sketch.object)
        }
        assert slot_facts["parametric.dof"] == 0
        assert slot_facts["parametric.fully_constrained"] is True
    finally:
        session.close_document()


@pytest.mark.slow
def test_real_variable_fillet_survives_parameter_edits_rollback_and_reopen(
    tmp_path,
    monkeypatch,
) -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    design = _edge_treatment_design()
    end_parameter_id = _id("parameter", 21)
    path = tmp_path / "s42-variable-fillet.FCStd"
    session = Session()
    session.open_document("S42VariableFillet")
    try:
        compiled = compiler_module.compile_parametric_design(session, design)
        stabilize_parametric_session(session)

        assert compiled.result_object is compiled.edge_treatments[-1].object
        assert compiled.result_object.TypeId == "Part::Fillet"
        assert len(tuple(compiled.result_object.Edges)) == 1
        assert tuple(compiled.result_object.Edges[0][1:]) == pytest.approx((1, 3))
        initial_volume = float(compiled.result_object.Shape.Volume)

        radius_edit = compiler_module.modify_parametric_parameter(
            session,
            design,
            body=compiled.body,
            parameter_id=end_parameter_id,
            value=4,
        )
        assert (radius_edit.before_value, radius_edit.after_value) == (3, 4)
        assert tuple(compiled.result_object.Edges[0][1:]) == pytest.approx((1, 4))
        assert float(compiled.result_object.Shape.Volume) != pytest.approx(initial_volume)

        depth_edit = compiler_module.modify_parametric_parameter(
            session,
            design,
            body=compiled.body,
            parameter_id=DEPTH,
            value=10,
        )
        assert (depth_edit.before_value, depth_edit.after_value) == (8, 10)
        assert tuple(compiled.result_object.Edges[0][1:]) == pytest.approx((1, 4))
        valid_volume = float(compiled.result_object.Shape.Volume)

        sweep_forward = compiler_module._sweep_forward
        monkeypatch.setattr(
            compiler_module,
            "_sweep_forward",
            lambda *args, **kwargs: not sweep_forward(*args, **kwargs),
        )
        with pytest.raises(ParametricCompileError):
            compiler_module.modify_parametric_parameter(
                session,
                design,
                body=compiled.body,
                parameter_id=end_parameter_id,
                value=5,
            )
        monkeypatch.setattr(compiler_module, "_sweep_forward", sweep_forward)
        stabilize_parametric_session(session)
        assert tuple(compiled.result_object.Edges[0][1:]) == pytest.approx((1, 4))
        assert float(compiled.result_object.Shape.Volume) == pytest.approx(valid_volume)

        with pytest.raises(ParametricCompileError):
            compiler_module.modify_parametric_parameter(
                session,
                design,
                body=compiled.body,
                parameter_id=end_parameter_id,
                value=101,
            )
        stabilize_parametric_session(session)
        assert tuple(compiled.result_object.Edges[0][1:]) == pytest.approx((1, 4))
        assert float(compiled.result_object.Shape.Volume) == pytest.approx(valid_volume)

        session.doc.saveAs(str(path))
    finally:
        session.close_document()

    reopened = Session()
    try:
        reopened.load_document(path)
        stabilize_parametric_session(reopened)
        fillets = tuple(obj for obj in reopened.doc.Objects if obj.TypeId == "Part::Fillet")
        assert len(fillets) == 1
        assert tuple(fillets[0].Edges[0][1:]) == pytest.approx((1, 4))
        assert fillets[0].Shape.isValid()
        assert len(tuple(fillets[0].Shape.Solids)) == 1
    finally:
        reopened.close_document()


@pytest.mark.slow
def test_real_symmetric_chamfer_compiles_as_native_tail() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    session = Session()
    session.open_document("S42SymmetricChamfer")
    try:
        compiled = compiler_module.compile_parametric_design(
            session,
            _edge_treatment_design(EdgeTreatmentKind.CHAMFER),
        )
        stabilize_parametric_session(session)
        assert compiled.result_object.TypeId == "Part::Chamfer"
        assert tuple(compiled.result_object.Edges[0][1:]) == pytest.approx((1, 1))
        assert compiled.result_object.Shape.isValid()
        assert len(tuple(compiled.result_object.Shape.Solids)) == 1
    finally:
        session.close_document()


@pytest.mark.slow
def test_real_multi_edge_fillet_preserves_independent_parameters() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    design = _multi_edge_fillet_design()
    session = Session()
    session.open_document("S42MultiEdgeFillet")
    try:
        compiled = compiler_module.compile_parametric_design(session, design)
        stabilize_parametric_session(session)
        before = tuple(tuple(item[1:]) for item in compiled.result_object.Edges)
        assert before[0] == pytest.approx((1, 3))
        assert before[1] == pytest.approx((2, 2))

        edit = compiler_module.modify_parametric_parameter(
            session,
            design,
            body=compiled.body,
            parameter_id=_id("parameter", 22),
            value=2.5,
        )
        assert (edit.before_value, edit.after_value) == (2, 2.5)
        after = tuple(tuple(item[1:]) for item in compiled.result_object.Edges)
        assert after[0] == pytest.approx((1, 3))
        assert after[1] == pytest.approx((2.5, 2.5))
    finally:
        session.close_document()


@pytest.mark.slow
@pytest.mark.parametrize("source_kind", ("pocket", "hole", "revolve"))
def test_real_constant_fillet_compiles_after_supported_partdesign_features(
    source_kind: str,
) -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    if source_kind == "pocket":
        from tests.guided_photo_designs import calibration_block_target

        base = calibration_block_target().design
        design = _with_constant_fillet(
            base,
            geometry_id=base.sketches[-1].geometries[0].id,
            role=SemanticEdgeRole.SECTION_END,
            point=ReferencePoint.WHOLE,
        )
    elif source_kind == "hole":
        base = _multi_hole_design()
        design = _with_constant_fillet(
            base,
            geometry_id=base.sketches[-1].geometries[0].id,
            role=SemanticEdgeRole.SECTION_END,
            point=ReferencePoint.WHOLE,
        )
    else:
        design = _revolve_edge_treatment_design()

    from vibecad.engine.session import Session

    session = Session()
    session.open_document(f"S42{source_kind.title()}Fillet")
    try:
        compiled = compiler_module.compile_parametric_design(session, design)
        stabilize_parametric_session(session)
        assert compiled.result_object.TypeId == "Part::Fillet"
        assert compiled.result_object.Shape.isValid()
        assert len(tuple(compiled.result_object.Shape.Solids)) == 1
    finally:
        session.close_document()


@pytest.mark.slow
def test_real_compiler_rolls_back_geometry_identity_and_result_root_when_adoption_fails() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    session = Session()
    session.open_document("ParametricAdoptionRollback")
    try:

        def reject(compiled: object) -> None:
            identity = EntityIdentity(
                object_id="object_" + "a" * 32,
                feature_id=None,
                object_type="PartDesign::Body",
                semantic_role=SemanticRole.PART,
                provenance=Provenance(
                    source=ProvenanceSource.MODEL,
                    operation_id="parametric-adoption",
                ),
            )
            session.attach_object_identity(compiled.body, identity)  # type: ignore[attr-defined]
            session.set_result_object(compiled.body)  # type: ignore[attr-defined]
            raise RuntimeError("reject adoption")

        with pytest.raises(ParametricCompileError) as caught:
            compiler_module.compile_parametric_design(
                session,
                _rectangle_design(),
                adopt=reject,
            )

        assert caught.value.code is ParametricCompileErrorCode.CAD_FAILURE
        assert tuple(session.doc.Objects) == ()
        assert session.list_object_identities() == ()
        assert session._result_roots == {}
    finally:
        session.close_document()


@pytest.mark.slow
def test_real_multi_location_hole_compiles_and_proves_every_cut() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    session = Session()
    session.open_document("ParametricMultiHole")
    try:
        compiled = compiler_module.compile_parametric_design(
            session,
            _multi_hole_design(),
        )
        stabilize_parametric_session(session)

        expected_volume = 60 * 40 * 8 - 2 * math.pi * 3**2 * 8
        assert float(compiled.body.Shape.Volume) == pytest.approx(expected_volume, abs=1e-6)
        facts = {
            fact.name: fact.value
            for fact in compiler_module.parametric_entity_facts(compiled.features[-1].object)
        }
        assert facts["parametric.profile.wire_count"] == 2
        assert facts["parametric.hole.location_count"] == 2
        assert facts["parametric.shape_valid"] is True
        assert facts["parametric.solid_count"] == 1
    finally:
        session.close_document()


def test_multi_view_l_bracket_ir_preserves_three_view_evidence_and_feature_order() -> None:
    design = _multi_view_l_bracket_design()

    assert ParametricDesignIR.from_mapping(design.to_mapping()) == design
    assert design.evidence[0].status is DesignEvidenceStatus.CROSS_VIEW_DERIVED
    assert design.evidence[0].origin is DesignEvidenceOrigin.MULTI_VIEW
    assert len(design.evidence[0].source_refs) == 3
    assert tuple(feature.kind for feature in design.features) == (
        FeatureKind.PAD,
        FeatureKind.HOLE,
        FeatureKind.HOLE,
    )
    assert tuple(len(feature.location_geometry_ids) for feature in design.features) == (
        0,
        2,
        1,
    )


@pytest.mark.slow
def test_real_three_view_l_bracket_is_editable_and_dimension_complete() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    design = _multi_view_l_bracket_design()
    session = Session()
    session.open_document("ParametricThreeViewLBracket")
    try:
        compiled = compiler_module.compile_parametric_design(session, design)
        stabilize_parametric_session(session)

        expected_volume = (50 * 8 + 8 * (40 - 8)) * 60 - 3 * math.pi * 3**2 * 8
        assert float(compiled.body.Shape.Volume) == pytest.approx(expected_volume, abs=1e-6)
        bounds = compiled.body.Shape.BoundBox
        assert (float(bounds.XLength), float(bounds.YLength), float(bounds.ZLength)) == (
            50,
            40,
            60,
        )
        assert all(binding.solver.fully_constrained for binding in compiled.sketches)
        assert all(binding.solver.dof == 0 for binding in compiled.sketches)
        assert tuple(
            dict(
                (fact.name, fact.value)
                for fact in compiler_module.parametric_entity_facts(binding.object)
            )["parametric.hole.location_count"]
            for binding in compiled.features[1:]
        ) == (2, 1)

        diameter_id = next(
            parameter.id for parameter in design.parameters if parameter.name == "Hole diameter"
        )
        edit = compiler_module.modify_parametric_parameter(
            session,
            design,
            body=compiled.body,
            parameter_id=diameter_id,
            value=8,
        )
        assert edit.before_value == pytest.approx(6)
        assert edit.after_value == pytest.approx(8)
        expected_edited_volume = (50 * 8 + 8 * (40 - 8)) * 60 - 3 * math.pi * 4**2 * 8
        assert float(compiled.body.Shape.Volume) == pytest.approx(
            expected_edited_volume,
            abs=1e-6,
        )
        assert compiled.body.Shape.isValid()
        assert len(tuple(compiled.body.Shape.Solids)) == 1
    finally:
        session.close_document()


@pytest.mark.slow
def test_real_parametric_parameter_verifier_failure_rolls_back_same_transaction() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    design = _rectangle_design()
    session = Session()
    session.open_document("ParametricModifyRollback")
    try:
        compiled = compiler_module.compile_parametric_design(session, design)
        session.set_result_object(compiled.body)
        objects_before = tuple(session.doc.Objects)
        roots_before = dict(session._result_roots)
        volume_before = float(compiled.body.Shape.Volume)

        def reject(_edit: object) -> None:
            raise RuntimeError("reject verified edit")

        with pytest.raises(ParametricCompileError) as caught:
            compiler_module.modify_parametric_parameter(
                session,
                design,
                body=compiled.body,
                parameter_id=WIDTH,
                value=50,
                verify=reject,
            )

        assert caught.value.code is ParametricCompileErrorCode.CAD_FAILURE
        stabilize_parametric_session(session)
        assert tuple(session.doc.Objects) == objects_before
        assert session._result_roots == roots_before
        assert float(compiled.body.Shape.Volume) == pytest.approx(volume_before)
        width_property = compiler_module._parameter_property(
            next(item for item in design.parameters if item.id == WIDTH)
        )
        assert getattr(compiled.parameter_carrier, width_property).Value == pytest.approx(60)
    finally:
        session.close_document()


@pytest.mark.slow
def test_real_maximum_ir_uses_exact_26_object_operation_budget() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    session = Session()
    session.open_document("ParametricMaximumBudget")
    try:
        compiled = compiler_module.compile_parametric_design(
            session,
            _maximum_feature_design(),
        )

        assert len(compiled.sketches) == 8
        assert len(compiled.features) == 8
        assert len(tuple(session.doc.Objects)) == 26
    finally:
        session.close_document()


def test_solver_diagnostic_indexes_are_one_based() -> None:
    assert compiler_module._diagnostic_indexes((1, 2), 2) == (1, 2)
    with pytest.raises(ParametricCompileError):
        compiler_module._diagnostic_indexes((0,), 2)


def test_profile_closure_requires_every_compiled_edge_in_closed_wires() -> None:
    closed_wire = SimpleNamespace(isClosed=lambda: True, Edges=(object(),) * 4)
    closed_shape = SimpleNamespace(
        Edges=(object(),) * 4,
        Wires=(closed_wire,),
        isNull=lambda: False,
        isValid=lambda: True,
    )

    assert (
        compiler_module._require_profile_closure(
            SimpleNamespace(Shape=closed_shape),
            expected_edge_count=4,
        )
        == 1
    )

    open_wire = SimpleNamespace(isClosed=lambda: False, Edges=(object(),) * 4)
    with pytest.raises(ParametricCompileError) as caught:
        compiler_module._require_profile_closure(
            SimpleNamespace(
                Shape=SimpleNamespace(
                    Edges=(object(),) * 4,
                    Wires=(open_wire,),
                    isNull=lambda: False,
                    isValid=lambda: True,
                )
            ),
            expected_edge_count=4,
        )

    assert caught.value.code is ParametricCompileErrorCode.PROFILE_FAILURE


def test_multi_loop_pockets_remain_closed_while_holes_have_per_location_proof() -> None:
    with pytest.raises(ParametricCompileError) as caught:
        compiler_module._require_supported_feature_profile(
            FeatureKind.POCKET,
            2,
            path="/features/1",
        )

    assert caught.value.code is ParametricCompileErrorCode.UNSUPPORTED
    assert caught.value.path == "/features/1"

    compiler_module._require_supported_feature_profile(FeatureKind.PAD, 2)
    compiler_module._require_supported_feature_profile(FeatureKind.REVOLVE, 2)
    compiler_module._require_supported_feature_profile(FeatureKind.HOLE, 1)
    compiler_module._require_supported_feature_profile(FeatureKind.HOLE, 16)


def test_multi_hole_location_limit_fails_before_cad_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert compiler_module._MAX_COMPILED_HOLE_LOCATIONS == 16
    monkeypatch.setattr(compiler_module, "_MAX_COMPILED_HOLE_LOCATIONS", 1)

    class EmptySession:
        doc = SimpleNamespace(Objects=(), UndoMode=1)
        transaction_started = False

        @contextmanager
        def _transaction(self, _label: str, *, claim_new_objects: bool):
            self.transaction_started = True
            yield

    session = EmptySession()
    with pytest.raises(ParametricCompileError) as caught:
        compiler_module.compile_parametric_design(session, _multi_hole_design())

    assert caught.value.code is ParametricCompileErrorCode.UNSUPPORTED
    assert caught.value.path == "/features/1/location_geometry_ids"
    assert session.transaction_started is False


def test_multi_hole_location_proof_rejects_a_declared_axis_without_removed_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Vector:
        def __init__(self, x: float, y: float, z: float) -> None:
            self.x = x
            self.y = y
            self.z = z

        @property
        def Length(self) -> float:
            return (self.x**2 + self.y**2 + self.z**2) ** 0.5

        def __add__(self, other: object) -> Vector:
            assert isinstance(other, Vector)
            return Vector(self.x + other.x, self.y + other.y, self.z + other.z)

    class Shape:
        BoundBox = SimpleNamespace(DiagonalLength=1_000.0)

        def __init__(self, *, cut_x: tuple[float, ...]) -> None:
            self.cut_x = cut_x

        def isInside(self, point: Vector, _tolerance: float, _boundary: bool) -> bool:
            # Exercise adaptive near-plane probes: the uniform 65-point scan has
            # no interior sample for this deliberately thin solid.
            inside_base = abs(point.x) <= 2 and abs(point.y) <= 2 and 0 < point.z < 0.01
            return inside_base and not any(abs(point.x - value) < 0.1 for value in self.cut_x)

    placement = SimpleNamespace(
        Rotation=SimpleNamespace(multVec=lambda value: value),
        multVec=lambda value: value,
    )
    sketch = SimpleNamespace(
        Placement=placement,
        Geometry=(
            SimpleNamespace(Center=Vector(-1, 0, 0)),
            SimpleNamespace(Center=Vector(1, 0, 0)),
        ),
    )
    monkeypatch.setattr(
        compiler_module,
        "_read_metadata",
        lambda _obj, *, required: {
            "geometries": [
                {
                    "id": _id("geometry", 20),
                    "indices": [0],
                    "type_ids": ["Part::GeomCircle"],
                    "construction": [False],
                },
                {
                    "id": _id("geometry", 21),
                    "indices": [1],
                    "type_ids": ["Part::GeomCircle"],
                    "construction": [False],
                },
            ]
        },
    )
    FreeCAD = SimpleNamespace(Vector=Vector)
    previous = SimpleNamespace(Shape=Shape(cut_x=()))
    result = SimpleNamespace(Shape=Shape(cut_x=(-1,)))

    with pytest.raises(ParametricCompileError) as caught:
        compiler_module._require_hole_location_cuts(
            FreeCAD,
            sketch,
            previous,
            result,
            location_geometry_ids=(_id("geometry", 20), _id("geometry", 21)),
            depth_mm=None,
            path="/features/1",
        )

    assert caught.value.code is ParametricCompileErrorCode.FEATURE_FAILURE
    assert caught.value.path == "/features/1/location_geometry_ids/1"


def test_revolution_axis_tokens_preserve_sketch_axes_and_construction_order() -> None:
    sketch = _rectangle_design().sketches[0]
    before = SketchGeometry(
        id=_id("geometry", 0),
        kind=GeometryKind.LINE,
        dimensions={"x1_mm": 0, "y1_mm": -10, "x2_mm": 0, "y2_mm": 50},
        construction=True,
    )
    target = SketchGeometry(
        id=_id("geometry", 5),
        kind=GeometryKind.LINE,
        dimensions={"x1_mm": -10, "y1_mm": 0, "x2_mm": 70, "y2_mm": 0},
        construction=True,
    )
    with_axes = replace(sketch, geometries=sketch.geometries + (target, before))

    assert compiler_module._revolution_axis_token(with_axes, "@sketch_x") == "H_Axis"
    assert compiler_module._revolution_axis_token(with_axes, "@sketch_y") == "V_Axis"
    assert compiler_module._revolution_axis_token(with_axes, target.id) == "Axis1"


def test_feature_parameter_bindings_exclude_dormant_through_all_dimensions() -> None:
    pad = _rectangle_design().features[0]
    pocket = PartDesignFeature(
        id=_id("feature", 2),
        name="Pocket",
        kind=FeatureKind.POCKET,
        sketch_id=SKETCH,
        base_feature_id=pad.id,
        parameters={},
        evidence_ids=(EVIDENCE,),
        extent=FeatureExtent.THROUGH_ALL,
    )
    hole = PartDesignFeature(
        id=_id("feature", 3),
        name="Hole",
        kind=FeatureKind.HOLE,
        sketch_id=SKETCH,
        base_feature_id=pocket.id,
        parameters={"diameter": WIDTH},
        evidence_ids=(EVIDENCE,),
        extent=FeatureExtent.THROUGH_ALL,
        location_geometry_ids=(BOTTOM,),
    )

    assert compiler_module._feature_parameter_bindings(pad) == (("length", DEPTH, "Length"),)
    assert compiler_module._feature_parameter_bindings(pocket) == ()
    assert compiler_module._feature_parameter_bindings(hole) == (("diameter", WIDTH, "Diameter"),)


def test_feature_parameter_bindings_allow_one_parameter_to_drive_two_targets() -> None:
    pad = _rectangle_design().features[0]
    hole = PartDesignFeature(
        id=_id("feature", 3),
        name="Hole",
        kind=FeatureKind.HOLE,
        sketch_id=SKETCH,
        base_feature_id=pad.id,
        parameters={"diameter": DEPTH, "depth": DEPTH},
        evidence_ids=(EVIDENCE,),
        extent=FeatureExtent.LENGTH,
        location_geometry_ids=(BOTTOM,),
    )

    assert compiler_module._feature_parameter_bindings(hole) == (
        ("diameter", DEPTH, "Diameter"),
        ("depth", DEPTH, "Depth"),
    )


def test_subtractive_features_cannot_succeed_without_removing_material() -> None:
    previous = SimpleNamespace(Shape=SimpleNamespace(Volume=100.0))
    no_op = SimpleNamespace(
        Shape=SimpleNamespace(
            Solids=(object(),),
            Volume=100.0,
            isNull=lambda: False,
            isValid=lambda: True,
        ),
        State=("Up-to-date",),
        getStatusString=lambda: "Valid",
    )

    with pytest.raises(ParametricCompileError) as caught:
        compiler_module._require_feature_shape(no_op, previous, FeatureKind.POCKET)

    assert caught.value.code is ParametricCompileErrorCode.FEATURE_FAILURE


def test_feature_shape_requires_explicit_exact_native_status() -> None:
    shape = SimpleNamespace(
        Solids=(object(),),
        Volume=100.0,
        isNull=lambda: False,
        isValid=lambda: True,
    )
    valid = SimpleNamespace(
        Shape=shape,
        State=("Up-to-date",),
        getStatusString=lambda: "Valid",
    )

    assert compiler_module._require_feature_shape(valid, None, FeatureKind.PAD) == 100.0

    for invalid in (
        SimpleNamespace(Shape=shape, State=("Up-to-date",)),
        SimpleNamespace(
            Shape=shape,
            State=("Up-to-date", "Up-to-date"),
            getStatusString=lambda: "Valid",
        ),
    ):
        with pytest.raises(ParametricCompileError) as caught:
            compiler_module._require_feature_shape(invalid, None, FeatureKind.PAD)

        assert caught.value.code is ParametricCompileErrorCode.FEATURE_FAILURE


def test_parametric_facts_join_entity_observation_without_changing_legacy_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stabilize_parametric_session(SimpleNamespace(doc=SimpleNamespace(Objects=(object(),))))
    monkeypatch.setattr(
        executor_module,
        "parametric_entity_facts",
        lambda _obj: (
            ParametricEntityFact("parametric.solver_ok", True),
            ParametricEntityFact("parametric.dof", 0),
        ),
    )
    placement = SimpleNamespace(
        Base=SimpleNamespace(x=0.0, y=0.0, z=0.0),
        Rotation=SimpleNamespace(Q=(0.0, 0.0, 0.0, 1.0)),
    )
    identity = SimpleNamespace(
        object_id="object_" + "a" * 32,
        feature_id="feature_" + "b" * 32,
        object_type="Sketcher::SketchObject",
        semantic_role=SimpleNamespace(value="feature"),
        provenance=SimpleNamespace(
            to_mapping=lambda: {"source": "model", "operation_id": "parametric"}
        ),
    )

    observation = executor_module._entity_observation(
        SimpleNamespace(Placement=placement),
        identity,
    )

    assert tuple((item.name, item.value) for item in observation.parameters) == (
        ("parametric.dof", 0),
        ("parametric.solver_ok", True),
    )
