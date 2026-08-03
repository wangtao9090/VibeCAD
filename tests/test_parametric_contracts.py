"""Focused contracts for editable parametric design intent."""

from __future__ import annotations

import dataclasses
import math
from copy import deepcopy

import pytest

from vibecad.parametric import (
    MAX_DESIGN_PARAMETERS,
    BodyDefinition,
    ConstraintKind,
    DatumPlane,
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
    ParametricContractError,
    ParametricDesignIR,
    ParametricErrorCode,
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


def _id(kind: str, value: int) -> str:
    return f"ir_{kind}_{value:032x}"


DESIGN = _id("design", 1)
BODY = _id("body", 1)
EVIDENCE = _id("evidence", 1)
WIDTH = _id("parameter", 1)
HEIGHT = _id("parameter", 2)
PAD_DEPTH = _id("parameter", 3)
SKETCH = _id("sketch", 1)
BOTTOM = _id("geometry", 1)
RIGHT = _id("geometry", 2)
TOP = _id("geometry", 3)
LEFT = _id("geometry", 4)
HORIZONTAL = _id("constraint", 1)
WIDTH_CONSTRAINT = _id("constraint", 2)
PAD = _id("feature", 1)


def _line(
    geometry_id: str,
    x1: int | float,
    y1: int | float,
    x2: int | float,
    y2: int | float,
) -> SketchGeometry:
    return SketchGeometry(
        id=geometry_id,
        kind=GeometryKind.LINE,
        dimensions={"x1_mm": x1, "y1_mm": y1, "x2_mm": x2, "y2_mm": y2},
        evidence_ids=(EVIDENCE,),
    )


def _parameter(
    parameter_id: str,
    name: str,
    value: int | float,
    *,
    kind: ParameterKind = ParameterKind.LENGTH,
    unit: DesignUnit = DesignUnit.MM,
) -> DesignParameter:
    return DesignParameter(
        id=parameter_id,
        name=name,
        kind=kind,
        value=value,
        unit=unit,
        minimum=0.1,
        maximum=1_000,
        evidence_ids=(EVIDENCE,),
    )


def _design() -> ParametricDesignIR:
    parameters = (
        _parameter(WIDTH, "Width", 60),
        _parameter(HEIGHT, "Height", 40),
        _parameter(PAD_DEPTH, "Pad depth", 8),
    )
    geometries = (
        _line(BOTTOM, -30, -20, 30, -20),
        _line(RIGHT, 30, -20, 30, 20),
        _line(TOP, 30, 20, -30, 20),
        _line(LEFT, -30, 20, -30, -20),
    )
    constraints = (
        SketchConstraint(
            id=HORIZONTAL,
            kind=ConstraintKind.HORIZONTAL,
            references=(SketchReference(target=BOTTOM, point=ReferencePoint.WHOLE),),
        ),
        SketchConstraint(
            id=WIDTH_CONSTRAINT,
            kind=ConstraintKind.LENGTH,
            references=(SketchReference(target=BOTTOM, point=ReferencePoint.WHOLE),),
            parameter_id=WIDTH,
            evidence_ids=(EVIDENCE,),
        ),
    )
    sketch = ParametricSketch(
        id=SKETCH,
        name="Base profile",
        role=SketchRole.PROFILE,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=geometries,
        constraints=constraints,
        evidence_ids=(EVIDENCE,),
    )
    feature = PartDesignFeature(
        id=PAD,
        name="Base pad",
        kind=FeatureKind.PAD,
        sketch_id=SKETCH,
        base_feature_id=None,
        parameters={"length": PAD_DEPTH},
        evidence_ids=(EVIDENCE,),
        extent=FeatureExtent.LENGTH,
    )
    return ParametricDesignIR(
        id=DESIGN,
        name="Mounting plate",
        units=UnitSystem(),
        body=BodyDefinition(id=BODY, name="Mounting plate body"),
        evidence=(
            DesignEvidence(
                id=EVIDENCE,
                status=DesignEvidenceStatus.CONFIRMED,
                origin=DesignEvidenceOrigin.USER,
                source_refs=("request:mounting-plate",),
            ),
        ),
        parameters=parameters,
        datum_planes=(),
        sketches=(sketch,),
        features=(feature,),
    )


def test_parametric_design_round_trips_and_has_a_stable_digest() -> None:
    design = _design()

    encoded = design.to_mapping()
    reconstructed = ParametricDesignIR.from_mapping(encoded)

    assert reconstructed == design
    assert reconstructed.canonical_bytes == design.canonical_bytes
    assert reconstructed.digest == design.digest
    assert len(design.digest) == 64
    assert set(encoded) == {
        "schema_version",
        "id",
        "name",
        "units",
        "body",
        "evidence",
        "parameters",
        "datum_planes",
        "sketches",
        "features",
    }


def test_unordered_collections_have_one_canonical_order() -> None:
    design = _design()
    encoded = design.to_mapping()
    encoded["parameters"].reverse()
    encoded["sketches"][0]["geometries"].reverse()
    encoded["sketches"][0]["constraints"].reverse()

    reordered = ParametricDesignIR.from_mapping(encoded)

    assert reordered == design
    assert reordered.digest == design.digest


def test_parametric_contracts_are_frozen_and_snapshot_input_mappings() -> None:
    dimensions = {"x1_mm": 0, "y1_mm": 0, "x2_mm": 20, "y2_mm": 0}
    geometry = SketchGeometry(
        id=_id("geometry", 16),
        kind=GeometryKind.LINE,
        dimensions=dimensions,
    )
    dimensions["x2_mm"] = 999

    assert geometry.dimensions["x2_mm"] == 20
    with pytest.raises(TypeError):
        geometry.dimensions["x1_mm"] = 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        geometry.kind = GeometryKind.CIRCLE


def test_wire_contract_rejects_unknown_fields_and_versions_at_exact_paths() -> None:
    encoded = _design().to_mapping()
    encoded["unexpected"] = True

    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.UNKNOWN_FIELD
    assert caught.value.path == "/unexpected"

    encoded = _design().to_mapping()
    encoded["parameters"][0]["schema_version"] = 2
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.UNSUPPORTED_VERSION
    assert caught.value.path == "/parameters/0/schema_version"


def test_ir_identity_is_separate_from_selector_identity() -> None:
    with pytest.raises(ParametricContractError) as caught:
        BodyDefinition(id="object_00000000000000000000000000000001", name="Bad")
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/id"

    with pytest.raises(ParametricContractError):
        PartDesignFeature(
            id="feature_00000000000000000000000000000001",
            name="Bad",
            kind=FeatureKind.PAD,
            sketch_id=SKETCH,
            base_feature_id=None,
            parameters={"length": PAD_DEPTH},
            evidence_ids=(EVIDENCE,),
            extent=FeatureExtent.LENGTH,
        )


@pytest.mark.parametrize("value", [True, math.nan, math.inf, -math.inf])
def test_geometry_seed_rejects_non_numeric_or_nonfinite_values(value: object) -> None:
    with pytest.raises(ParametricContractError) as caught:
        SketchGeometry(
            id=_id("geometry", 17),
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": value, "y1_mm": 0, "x2_mm": 20, "y2_mm": 0},
        )
    assert caught.value.code in {
        ParametricErrorCode.INVALID_TYPE,
        ParametricErrorCode.INVALID_VALUE,
    }
    assert caught.value.path == "/dimensions/x1_mm"


def test_geometry_seed_cannot_hide_a_parameter_reference() -> None:
    encoded = _design().to_mapping()
    encoded["sketches"][0]["geometries"][0]["dimensions"]["x1_mm"] = {"parameter_id": WIDTH}

    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.INVALID_TYPE
    assert caught.value.path == "/sketches/0/geometries/0/dimensions/x1_mm"


def test_geometry_seed_numbers_are_canonical_and_non_degenerate() -> None:
    geometry = SketchGeometry(
        id=_id("geometry", 18),
        kind=GeometryKind.LINE,
        dimensions={"x1_mm": -0.0, "y1_mm": 0.0, "x2_mm": 20.0, "y2_mm": 0},
    )
    assert geometry.dimensions == {"x1_mm": 0, "y1_mm": 0, "x2_mm": 20, "y2_mm": 0}

    with pytest.raises(ParametricContractError) as caught:
        SketchGeometry(
            id=_id("geometry", 19),
            kind=GeometryKind.LINE,
            dimensions={"x1_mm": 1, "y1_mm": 1, "x2_mm": 1, "y2_mm": 1},
        )
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/dimensions"


def test_only_executable_evidence_statuses_are_admitted() -> None:
    encoded = _design().to_mapping()
    encoded["evidence"][0]["status"] = "assumed"

    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/evidence/0/status"

    encoded = _design().to_mapping()
    encoded["parameters"][0]["evidence_ids"] = [_id("evidence", 99)]
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.UNKNOWN_REFERENCE
    assert caught.value.path == "/parameters/0/evidence_ids/0"


def test_datum_plane_is_orthonormal_and_must_resolve() -> None:
    with pytest.raises(ParametricContractError) as caught:
        DatumPlane(
            id=_id("datum", 1),
            name="Bad datum",
            origin_mm=(0, 0, 0),
            normal=(0, 0, 1),
            x_axis=(0, 0, 1),
            evidence_ids=(EVIDENCE,),
        )
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/x_axis"

    encoded = _design().to_mapping()
    encoded["sketches"][0]["plane"] = {
        "schema_version": 1,
        "kind": "datum",
        "origin": None,
        "datum_id": _id("datum", 99),
    }
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.UNKNOWN_REFERENCE
    assert caught.value.path == "/sketches/0/plane/datum_id"


def test_constraint_cardinality_parameter_evidence_and_units_are_strict() -> None:
    with pytest.raises(ParametricContractError) as caught:
        SketchConstraint(
            id=_id("constraint", 17),
            kind=ConstraintKind.HORIZONTAL,
            references=(),
        )
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/references"

    with pytest.raises(ParametricContractError) as caught:
        SketchConstraint(
            id=_id("constraint", 18),
            kind=ConstraintKind.LENGTH,
            references=(SketchReference(target=BOTTOM, point=ReferencePoint.WHOLE),),
            parameter_id=WIDTH,
        )
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/evidence_ids"

    encoded = _design().to_mapping()
    encoded["sketches"][0]["constraints"][1]["parameter_id"] = HEIGHT
    encoded["parameters"][1]["kind"] = "angle"
    encoded["parameters"][1]["unit"] = "deg"
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/sketches/0/constraints/1/parameter_id"


def test_dimensional_constraints_and_revolve_angles_have_executable_domains() -> None:
    encoded = _design().to_mapping()
    encoded["parameters"][0]["value"] = -1
    encoded["parameters"][0]["minimum"] = -10
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/sketches/0/constraints/1/parameter_id"

    encoded = _design().to_mapping()
    encoded["parameters"][1].update(
        {"kind": "angle", "unit": "deg", "value": 361, "maximum": 1_000}
    )
    encoded["features"][0].update(
        {
            "kind": "revolve",
            "parameters": {"angle": HEIGHT},
            "extent": None,
            "axis": "@sketch_x",
        }
    )
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/features/0/parameters/angle"


def test_constraint_geometry_compatibility_is_closed() -> None:
    point_id = _id("geometry", 40)
    with pytest.raises(ParametricContractError) as caught:
        ParametricSketch(
            id=_id("sketch", 40),
            name="Invalid parallel constraint",
            role=SketchRole.REFERENCE,
            plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
            geometries=(
                _line(_id("geometry", 41), 0, 0, 10, 0),
                SketchGeometry(
                    id=point_id,
                    kind=GeometryKind.POINT,
                    dimensions={"x_mm": 0, "y_mm": 0},
                ),
            ),
            constraints=(
                SketchConstraint(
                    id=_id("constraint", 40),
                    kind=ConstraintKind.PARALLEL,
                    references=(
                        SketchReference(
                            target=_id("geometry", 41),
                            point=ReferencePoint.WHOLE,
                        ),
                        SketchReference(target=point_id, point=ReferencePoint.WHOLE),
                    ),
                ),
            ),
        )
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/constraints/0/references/1/target"


def test_hole_consumes_every_nonconstruction_location_circle() -> None:
    design = _design()
    hole_sketch_id = _id("sketch", 2)
    first_location = _id("geometry", 30)
    hole_sketch = ParametricSketch(
        id=hole_sketch_id,
        name="Hole locations",
        role=SketchRole.HOLE_LOCATIONS,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=(
            SketchGeometry(
                id=first_location,
                kind=GeometryKind.CIRCLE,
                dimensions={"cx_mm": -10, "cy_mm": 0, "radius_mm": 1},
            ),
            SketchGeometry(
                id=_id("geometry", 31),
                kind=GeometryKind.CIRCLE,
                dimensions={"cx_mm": 10, "cy_mm": 0, "radius_mm": 1},
            ),
        ),
        constraints=(),
        evidence_ids=(EVIDENCE,),
    )
    hole = PartDesignFeature(
        id=_id("feature", 2),
        name="Mounting holes",
        kind=FeatureKind.HOLE,
        sketch_id=hole_sketch_id,
        base_feature_id=PAD,
        parameters={"diameter": WIDTH},
        evidence_ids=(EVIDENCE,),
        extent=FeatureExtent.THROUGH_ALL,
        location_geometry_ids=(first_location,),
    )

    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR(
            id=design.id,
            name=design.name,
            units=design.units,
            body=design.body,
            evidence=design.evidence,
            parameters=design.parameters,
            datum_planes=design.datum_planes,
            sketches=(*design.sketches, hole_sketch),
            features=(*design.features, hole),
        )
    assert caught.value.code is ParametricErrorCode.INVALID_VALUE
    assert caught.value.path == "/features/1/location_geometry_ids"


def test_cross_reference_errors_preserve_wire_collection_indexes() -> None:
    encoded = _design().to_mapping()
    encoded["parameters"][0]["evidence_ids"] = [EVIDENCE, _id("evidence", 0)]
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.UNKNOWN_REFERENCE
    assert caught.value.path == "/parameters/0/evidence_ids/1"

    encoded = _design().to_mapping()
    encoded["sketches"][0]["constraints"].reverse()
    encoded["sketches"][0]["constraints"][0]["evidence_ids"] = [_id("evidence", 99)]

    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.UNKNOWN_REFERENCE
    assert caught.value.path == "/sketches/0/constraints/0/evidence_ids/0"


def test_hole_reference_errors_preserve_wire_indexes_after_canonicalization() -> None:
    design = _design()
    hole_sketch_id = _id("sketch", 3)
    location_id = _id("geometry", 50)
    hole_sketch = ParametricSketch(
        id=hole_sketch_id,
        name="One hole location",
        role=SketchRole.HOLE_LOCATIONS,
        plane=SketchPlane(kind=PlaneKind.ORIGIN, origin=OriginPlane.XY),
        geometries=(
            SketchGeometry(
                id=location_id,
                kind=GeometryKind.CIRCLE,
                dimensions={"cx_mm": 0, "cy_mm": 0, "radius_mm": 1},
            ),
        ),
        constraints=(),
        evidence_ids=(EVIDENCE,),
    )
    hole = PartDesignFeature(
        id=_id("feature", 3),
        name="One hole",
        kind=FeatureKind.HOLE,
        sketch_id=hole_sketch_id,
        base_feature_id=PAD,
        parameters={"diameter": WIDTH},
        evidence_ids=(EVIDENCE,),
        extent=FeatureExtent.THROUGH_ALL,
        location_geometry_ids=(location_id,),
    )
    valid = ParametricDesignIR(
        id=design.id,
        name=design.name,
        units=design.units,
        body=design.body,
        evidence=design.evidence,
        parameters=design.parameters,
        datum_planes=design.datum_planes,
        sketches=(*design.sketches, hole_sketch),
        features=(*design.features, hole),
    ).to_mapping()
    valid["features"][1]["location_geometry_ids"] = [
        location_id,
        _id("geometry", 0),
    ]

    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(valid)
    assert caught.value.code is ParametricErrorCode.UNKNOWN_REFERENCE
    assert caught.value.path == "/features/1/location_geometry_ids/1"


def test_all_geometry_and_feature_variants_have_closed_shapes() -> None:
    geometries = (
        SketchGeometry(
            id=_id("geometry", 20),
            kind=GeometryKind.CIRCLE,
            dimensions={"cx_mm": 0, "cy_mm": 0, "radius_mm": 4},
        ),
        SketchGeometry(
            id=_id("geometry", 21),
            kind=GeometryKind.ARC,
            dimensions={
                "cx_mm": 0,
                "cy_mm": 0,
                "radius_mm": 4,
                "start_angle_deg": 0,
                "sweep_angle_deg": 180,
            },
        ),
        SketchGeometry(
            id=_id("geometry", 22),
            kind=GeometryKind.SLOT,
            dimensions={"x1_mm": -5, "y1_mm": 0, "x2_mm": 5, "y2_mm": 0, "width_mm": 2},
        ),
    )
    assert (
        tuple(SketchGeometry.from_mapping(item.to_mapping()) for item in geometries) == geometries
    )

    features = (
        PartDesignFeature(
            id=_id("feature", 20),
            name="Pocket",
            kind=FeatureKind.POCKET,
            sketch_id=SKETCH,
            base_feature_id=PAD,
            parameters={},
            evidence_ids=(EVIDENCE,),
            extent=FeatureExtent.THROUGH_ALL,
        ),
        PartDesignFeature(
            id=_id("feature", 21),
            name="Hole",
            kind=FeatureKind.HOLE,
            sketch_id=SKETCH,
            base_feature_id=PAD,
            parameters={"diameter": WIDTH},
            evidence_ids=(EVIDENCE,),
            extent=FeatureExtent.THROUGH_ALL,
            location_geometry_ids=(_id("geometry", 20),),
        ),
        PartDesignFeature(
            id=_id("feature", 22),
            name="Revolve",
            kind=FeatureKind.REVOLVE,
            sketch_id=SKETCH,
            base_feature_id=None,
            parameters={"angle": HEIGHT},
            evidence_ids=(EVIDENCE,),
            axis="@sketch_x",
        ),
    )
    assert tuple(PartDesignFeature.from_mapping(item.to_mapping()) for item in features) == features

    bad_circle = geometries[0].to_mapping()
    bad_circle["dimensions"].pop("radius_mm")
    with pytest.raises(ParametricContractError) as caught:
        SketchGeometry.from_mapping(bad_circle)
    assert caught.value.path == "/dimensions"


def test_feature_references_and_single_body_order_fail_closed() -> None:
    encoded = _design().to_mapping()
    encoded["features"][0]["parameters"]["length"] = _id("parameter", 99)
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.UNKNOWN_REFERENCE
    assert caught.value.path == "/features/0/parameters/length"

    encoded = _design().to_mapping()
    encoded["features"][0]["kind"] = "pocket"
    encoded["features"][0]["extent"] = "length"
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.INVALID_ORDER
    assert caught.value.path == "/features/0/kind"

    encoded = _design().to_mapping()
    encoded["features"][0]["base_feature_id"] = _id("feature", 99)
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.INVALID_ORDER
    assert caught.value.path == "/features/0/base_feature_id"


def test_duplicate_ids_and_contract_budgets_fail_closed() -> None:
    encoded = _design().to_mapping()
    encoded["parameters"].append(deepcopy(encoded["parameters"][0]))
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR.from_mapping(encoded)
    assert caught.value.code is ParametricErrorCode.DUPLICATE_ID
    assert caught.value.path == "/parameters/3/id"

    parameters = tuple(
        DesignParameter(
            id=_id("parameter", index + 100),
            name=f"P{index}",
            kind=ParameterKind.LENGTH,
            value=1,
            unit=DesignUnit.MM,
            evidence_ids=(EVIDENCE,),
        )
        for index in range(MAX_DESIGN_PARAMETERS + 1)
    )
    with pytest.raises(ParametricContractError) as caught:
        ParametricDesignIR(
            id=_id("design", 2),
            name="Over budget",
            units=UnitSystem(),
            body=BodyDefinition(id=_id("body", 2), name="Body"),
            evidence=_design().evidence,
            parameters=parameters,
            datum_planes=(),
            sketches=_design().sketches,
            features=_design().features,
        )
    assert caught.value.code is ParametricErrorCode.BUDGET_EXCEEDED
    assert caught.value.path == "/parameters"
