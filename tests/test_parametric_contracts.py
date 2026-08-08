"""Focused contracts for editable parametric design intent."""

from __future__ import annotations

import dataclasses
import math
from copy import deepcopy
from types import SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
from vibecad.execution.registry import ValueShape, _matches_value_shape
from vibecad.execution.selectors import SelectorV1, SemanticRole
from vibecad.parametric import (
    MAX_DESIGN_EVIDENCE,
    MAX_DESIGN_PARAMETERS,
    MAX_PATTERN_FEATURES,
    MAX_PATTERN_INSTANCES,
    BodyDefinition,
    ConstraintKind,
    DatumPlane,
    DerivedParameterExpression,
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
    MirrorPlane,
    OriginPlane,
    ParameterKind,
    ParametricContractError,
    ParametricDesignIR,
    ParametricErrorCode,
    ParametricSketch,
    PartDesignFeature,
    PatternDirection,
    PlaneKind,
    ReferencePoint,
    SemanticEdgeReference,
    SemanticEdgeRole,
    SemanticFaceReference,
    SemanticFaceRole,
    SketchConstraint,
    SketchGeometry,
    SketchPlane,
    SketchReference,
    SketchRole,
    UnitSystem,
)
from vibecad.parametric.compiler import ParametricEntityFact
from vibecad.workflow.contracts import (
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.program import (
    ProgramErrorCode,
    ProgramValidationError,
    validate_model_program,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskRun,
    new_task_run,
    transition_task,
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
FILLET = _id("feature", 2)
FILLET_START = _id("parameter", 4)
FILLET_END = _id("parameter", 5)
PATTERN_LENGTH = _id("parameter", 6)
PATTERN_ANGLE = _id("parameter", 7)
THICKNESS_VALUE = _id("parameter", 8)
DRAFT_ANGLE = _id("parameter", 9)


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


def _design_with_fillet() -> ParametricDesignIR:
    base = _design()
    return dataclasses.replace(
        base,
        parameters=base.parameters
        + (
            _parameter(FILLET_START, "Fillet start", 2),
            _parameter(FILLET_END, "Fillet end", 4),
        ),
        edge_treatments=(
            EdgeTreatmentFeature(
                id=FILLET,
                name="Variable edge fillet",
                kind=EdgeTreatmentKind.FILLET,
                base_feature_id=PAD,
                targets=(
                    EdgeTreatmentTarget(
                        edge=SemanticEdgeReference(
                            source_feature_id=PAD,
                            geometry_id=BOTTOM,
                            role=SemanticEdgeRole.SWEEP,
                            point=ReferencePoint.START,
                        ),
                        start_parameter_id=FILLET_START,
                        end_parameter_id=FILLET_END,
                    ),
                ),
                evidence_ids=(EVIDENCE,),
            ),
        ),
    )


def _design_with_pattern(kind: FeatureKind) -> ParametricDesignIR:
    base = _design()
    if kind is FeatureKind.LINEAR_PATTERN:
        parameter = _parameter(PATTERN_LENGTH, "Pattern length", 30)
        feature = PartDesignFeature(
            id=_id("feature", 10),
            name="Linear pattern",
            kind=kind,
            sketch_id=None,
            base_feature_id=PAD,
            parameters={"length": parameter.id},
            evidence_ids=(EVIDENCE,),
            source_feature_id=PAD,
            direction=PatternDirection.X_AXIS,
            occurrences=3,
        )
    elif kind is FeatureKind.CIRCULAR_PATTERN:
        parameter = _parameter(
            PATTERN_ANGLE,
            "Pattern angle",
            180,
            kind=ParameterKind.ANGLE,
            unit=DesignUnit.DEG,
        )
        feature = PartDesignFeature(
            id=_id("feature", 10),
            name="Circular pattern",
            kind=kind,
            sketch_id=None,
            base_feature_id=PAD,
            parameters={"angle": parameter.id},
            evidence_ids=(EVIDENCE,),
            source_feature_id=PAD,
            axis="@body_z",
            occurrences=4,
        )
    else:
        parameter = None
        feature = PartDesignFeature(
            id=_id("feature", 10),
            name="Mirror",
            kind=kind,
            sketch_id=None,
            base_feature_id=PAD,
            parameters={},
            evidence_ids=(EVIDENCE,),
            source_feature_id=PAD,
            mirror_plane=MirrorPlane.YZ_PLANE,
        )
    return dataclasses.replace(
        base,
        parameters=base.parameters + (() if parameter is None else (parameter,)),
        features=base.features + (feature,),
    )


def _design_with_surface_modifiers(*kinds: FeatureKind) -> ParametricDesignIR:
    base = _design()
    parameters = list(base.parameters)
    features = list(base.features)
    for index, kind in enumerate(kinds, 1):
        if kind is FeatureKind.DRAFT:
            parameter = _parameter(
                DRAFT_ANGLE,
                "Draft angle",
                5,
                kind=ParameterKind.ANGLE,
                unit=DesignUnit.DEG,
            )
            feature = PartDesignFeature(
                id=_id("feature", 40 + index),
                name="Native draft",
                kind=kind,
                sketch_id=None,
                base_feature_id=features[-1].id,
                parameters={"angle": parameter.id},
                evidence_ids=(EVIDENCE,),
                face_targets=(
                    SemanticFaceReference(
                        source_feature_id=PAD,
                        role=SemanticFaceRole.SWEEP,
                        geometry_id=BOTTOM,
                    ),
                ),
                neutral_plane=OriginPlane.XY,
            )
        else:
            parameter = _parameter(THICKNESS_VALUE, "Wall thickness", 1)
            feature = PartDesignFeature(
                id=_id("feature", 40 + index),
                name="Native thickness",
                kind=kind,
                sketch_id=None,
                base_feature_id=features[-1].id,
                parameters={"thickness": parameter.id},
                evidence_ids=(EVIDENCE,),
                face_targets=(
                    SemanticFaceReference(
                        source_feature_id=PAD,
                        role=SemanticFaceRole.SECTION_END,
                    ),
                ),
                reversed=False,
            )
        parameters.append(parameter)
        features.append(feature)
    return dataclasses.replace(base, parameters=tuple(parameters), features=tuple(features))


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


def test_edge_treatment_round_trips_without_changing_legacy_wire_shape() -> None:
    legacy = _design()
    design = _design_with_fillet()

    assert "edge_treatments" not in legacy.to_mapping()
    assert ParametricDesignIR.from_mapping(design.to_mapping()) == design
    treatment = design.to_mapping()["edge_treatments"][0]
    assert treatment["kind"] == "fillet"
    assert treatment["targets"][0]["edge"] == {
        "schema_version": 1,
        "source_feature_id": PAD,
        "geometry_id": BOTTOM,
        "role": "sweep",
        "point": "start",
    }


@pytest.mark.parametrize(
    ("kind", "semantic_field", "semantic_value"),
    (
        (FeatureKind.LINEAR_PATTERN, "direction", "x_axis"),
        (FeatureKind.CIRCULAR_PATTERN, "axis", "@body_z"),
        (FeatureKind.MIRROR, "mirror_plane", "yz_plane"),
    ),
)
def test_native_pattern_features_round_trip_with_stable_semantic_references(
    kind: FeatureKind,
    semantic_field: str,
    semantic_value: str,
) -> None:
    legacy = _design()
    design = _design_with_pattern(kind)
    encoded = design.to_mapping()
    pattern = encoded["features"][-1]

    assert ParametricDesignIR.from_mapping(encoded) == design
    assert set(legacy.to_mapping()["features"][0]) == {
        "schema_version",
        "id",
        "name",
        "kind",
        "sketch_id",
        "base_feature_id",
        "parameters",
        "evidence_ids",
        "extent",
        "axis",
        "location_geometry_ids",
        "reversed",
        "symmetric",
    }
    assert pattern["source_feature_id"] == PAD
    assert pattern[semantic_field] == semantic_value
    assert set(pattern) - set(legacy.to_mapping()["features"][0]) == {
        "source_feature_id",
        "direction",
        "mirror_plane",
        "occurrences",
    }


def test_pattern_contract_rejects_forward_pattern_sources_and_missing_wire_fields() -> None:
    design = _design_with_pattern(FeatureKind.LINEAR_PATTERN)
    encoded = design.to_mapping()
    encoded["features"][-1].pop("direction")

    with pytest.raises(ParametricContractError) as missing:
        ParametricDesignIR.from_mapping(encoded)

    assert missing.value.code is ParametricErrorCode.MISSING_FIELD
    assert missing.value.path == "/features/1/direction"

    legacy = _design().to_mapping()
    legacy["features"][0]["source_feature_id"] = PAD
    with pytest.raises(ParametricContractError) as extra:
        ParametricDesignIR.from_mapping(legacy)

    assert extra.value.code is ParametricErrorCode.UNKNOWN_FIELD
    assert extra.value.path == "/features/0/source_feature_id"

    pattern = design.features[-1]
    second = dataclasses.replace(
        pattern,
        id=_id("feature", 11),
        base_feature_id=pattern.id,
        source_feature_id=pattern.id,
    )
    with pytest.raises(ParametricContractError) as chained:
        dataclasses.replace(design, features=design.features + (second,))

    assert chained.value.code is ParametricErrorCode.INVALID_ORDER
    assert chained.value.path == "/features/2/source_feature_id"


def test_pattern_occurrence_and_total_feature_budgets_fail_closed() -> None:
    pattern = _design_with_pattern(FeatureKind.LINEAR_PATTERN).features[-1]
    with pytest.raises(ParametricContractError) as occurrence:
        dataclasses.replace(pattern, occurrences=17)

    assert occurrence.value.code is ParametricErrorCode.BUDGET_EXCEEDED
    assert occurrence.value.path == "/occurrences"

    design = _design_with_pattern(FeatureKind.MIRROR)
    features = [design.features[0]]
    for index in range(MAX_PATTERN_FEATURES + 1):
        features.append(
            dataclasses.replace(
                design.features[-1],
                id=_id("feature", 20 + index),
                base_feature_id=features[-1].id,
            )
        )
    with pytest.raises(ParametricContractError) as budget:
        dataclasses.replace(design, features=tuple(features))

    assert budget.value.code is ParametricErrorCode.BUDGET_EXCEEDED
    assert budget.value.path == "/features"

    design = _design_with_pattern(FeatureKind.LINEAR_PATTERN)
    features = [design.features[0]]
    for index in range(MAX_PATTERN_FEATURES):
        features.append(
            dataclasses.replace(
                design.features[-1],
                id=_id("feature", 30 + index),
                base_feature_id=features[-1].id,
                occurrences=MAX_PATTERN_INSTANCES // MAX_PATTERN_FEATURES + 1,
            )
        )
    with pytest.raises(ParametricContractError) as instances:
        dataclasses.replace(design, features=tuple(features))

    assert instances.value.code is ParametricErrorCode.BUDGET_EXCEEDED
    assert instances.value.path == "/features"


@pytest.mark.parametrize(
    ("kinds", "last_kind"),
    (
        ((FeatureKind.DRAFT,), "draft"),
        ((FeatureKind.THICKNESS,), "thickness"),
        ((FeatureKind.DRAFT, FeatureKind.THICKNESS), "thickness"),
    ),
)
def test_surface_modifiers_round_trip_with_semantic_faces_and_legacy_shape(
    kinds: tuple[FeatureKind, ...],
    last_kind: str,
) -> None:
    legacy = _design()
    design = _design_with_surface_modifiers(*kinds)
    encoded = design.to_mapping()
    modifier = encoded["features"][-1]

    assert ParametricDesignIR.from_mapping(encoded) == design
    assert set(legacy.to_mapping()["features"][0]) == {
        "schema_version",
        "id",
        "name",
        "kind",
        "sketch_id",
        "base_feature_id",
        "parameters",
        "evidence_ids",
        "extent",
        "axis",
        "location_geometry_ids",
        "reversed",
        "symmetric",
    }
    assert modifier["kind"] == last_kind
    assert set(modifier) - set(legacy.to_mapping()["features"][0]) == {
        "face_targets",
        "neutral_plane",
    }
    assert modifier["face_targets"][0]["source_feature_id"] == PAD
    assert "Face" not in repr(modifier)


def test_surface_modifier_face_contracts_reject_ambiguous_or_unsupported_targets() -> None:
    thickness = _design_with_surface_modifiers(FeatureKind.THICKNESS).features[-1]

    with pytest.raises(ParametricContractError) as missing_geometry:
        SemanticFaceReference(source_feature_id=PAD, role=SemanticFaceRole.SWEEP)
    assert missing_geometry.value.code is ParametricErrorCode.INVALID_VALUE
    assert missing_geometry.value.path == "/geometry_id"

    with pytest.raises(ParametricContractError) as section_geometry:
        SemanticFaceReference(
            source_feature_id=PAD,
            role=SemanticFaceRole.SECTION_END,
            geometry_id=TOP,
        )
    assert section_geometry.value.code is ParametricErrorCode.INVALID_VALUE
    assert section_geometry.value.path == "/geometry_id"

    duplicate = thickness.face_targets[0]
    with pytest.raises(ParametricContractError) as duplicate_target:
        dataclasses.replace(thickness, face_targets=(duplicate, duplicate))
    assert duplicate_target.value.code is ParametricErrorCode.DUPLICATE_ID
    assert duplicate_target.value.path == "/face_targets"

    with pytest.raises(ParametricContractError) as draft_section:
        dataclasses.replace(
            _design_with_surface_modifiers(FeatureKind.DRAFT).features[-1],
            face_targets=(
                SemanticFaceReference(
                    source_feature_id=PAD,
                    role=SemanticFaceRole.SECTION_START,
                ),
            ),
        )
    assert draft_section.value.code is ParametricErrorCode.INVALID_VALUE
    assert draft_section.value.path == "/face_targets"


def test_surface_modifier_budgets_and_tail_order_fail_closed() -> None:
    draft = _design_with_surface_modifiers(FeatureKind.DRAFT)
    with pytest.raises(ParametricContractError) as excessive_angle:
        dataclasses.replace(
            draft,
            parameters=tuple(
                dataclasses.replace(item, value=31) if item.id == DRAFT_ANGLE else item
                for item in draft.parameters
            ),
        )
    assert excessive_angle.value.code is ParametricErrorCode.INVALID_VALUE
    assert excessive_angle.value.path == "/features/1/parameters/angle"

    thickness = _design_with_surface_modifiers(FeatureKind.THICKNESS)
    targets = tuple(
        SemanticFaceReference(
            source_feature_id=PAD,
            role=SemanticFaceRole.SWEEP,
            geometry_id=_id("geometry", 100 + index),
        )
        for index in range(5)
    )
    with pytest.raises(ParametricContractError) as face_budget:
        dataclasses.replace(thickness.features[-1], face_targets=targets)
    assert face_budget.value.code is ParametricErrorCode.BUDGET_EXCEEDED
    assert face_budget.value.path == "/face_targets"

    pattern = _design_with_pattern(FeatureKind.MIRROR).features[-1]
    with pytest.raises(ParametricContractError) as interleaved:
        dataclasses.replace(
            thickness,
            features=(
                thickness.features[0],
                dataclasses.replace(pattern, base_feature_id=PAD),
                dataclasses.replace(
                    thickness.features[-1],
                    base_feature_id=pattern.id,
                ),
            ),
        )
    assert interleaved.value.code is ParametricErrorCode.INVALID_ORDER
    assert interleaved.value.path == "/features/2/kind"

    draft_then_thickness = _design_with_surface_modifiers(
        FeatureKind.DRAFT,
        FeatureKind.THICKNESS,
    )
    reversed_order = (
        draft_then_thickness.features[0],
        dataclasses.replace(
            draft_then_thickness.features[2],
            base_feature_id=PAD,
        ),
        dataclasses.replace(
            draft_then_thickness.features[1],
            base_feature_id=draft_then_thickness.features[2].id,
        ),
    )
    with pytest.raises(ParametricContractError) as order:
        dataclasses.replace(draft_then_thickness, features=reversed_order)
    assert order.value.code is ParametricErrorCode.INVALID_ORDER
    assert order.value.path == "/features/1/kind"


def test_chamfer_rejects_asymmetric_edge_distances_in_s42() -> None:
    treatment = _design_with_fillet().edge_treatments[0]

    with pytest.raises(ParametricContractError) as raised:
        dataclasses.replace(treatment, kind=EdgeTreatmentKind.CHAMFER)

    assert raised.value.code is ParametricErrorCode.INVALID_VALUE
    assert raised.value.path == "/targets"


def test_edge_treatment_accepts_sixteen_unique_targets_and_rejects_seventeen() -> None:
    treatment = _design_with_fillet().edge_treatments[0]
    targets = tuple(
        EdgeTreatmentTarget(
            edge=SemanticEdgeReference(
                source_feature_id=PAD,
                geometry_id=_id("geometry", 100 + index),
                role=SemanticEdgeRole.SWEEP,
                point=ReferencePoint.START,
            ),
            start_parameter_id=FILLET_START,
            end_parameter_id=FILLET_END,
        )
        for index in range(17)
    )

    assert len(dataclasses.replace(treatment, targets=targets[:16]).targets) == 16
    with pytest.raises(ParametricContractError) as raised:
        dataclasses.replace(treatment, targets=targets)

    assert raised.value.code is ParametricErrorCode.BUDGET_EXCEEDED
    assert raised.value.path == "/targets"


def test_derived_parameter_expression_round_trips_without_changing_legacy_shape() -> None:
    base = _design()
    derived = DesignParameter(
        id=_id("parameter", 4),
        name="Derived straight width",
        kind=ParameterKind.LENGTH,
        value=40,
        unit=DesignUnit.MM,
        evidence_ids=(EVIDENCE,),
        minimum=0.1,
        maximum=1_000,
        public=False,
        expression=DerivedParameterExpression(
            constant=-20,
            terms={WIDTH: 1},
        ),
    )
    design = dataclasses.replace(base, parameters=(*base.parameters, derived))

    encoded = design.to_mapping()
    reconstructed = ParametricDesignIR.from_mapping(encoded)

    assert reconstructed == design
    by_id = {item["id"]: item for item in encoded["parameters"]}
    assert "expression" not in by_id[WIDTH]
    assert by_id[derived.id]["expression"] == {
        "schema_version": 1,
        "constant": -20,
        "terms": {WIDTH: 1},
    }


@pytest.mark.parametrize(
    ("expression", "value", "public", "expected_code"),
    (
        (
            DerivedParameterExpression(terms={_id("parameter", 99): 1}),
            40,
            False,
            ParametricErrorCode.UNKNOWN_REFERENCE,
        ),
        (
            DerivedParameterExpression(terms={WIDTH: 1}),
            41,
            False,
            ParametricErrorCode.INVALID_VALUE,
        ),
        (
            DerivedParameterExpression(constant=-20, terms={WIDTH: 1}),
            40,
            True,
            ParametricErrorCode.INVALID_VALUE,
        ),
    ),
)
def test_derived_parameter_expression_fails_closed(
    expression: DerivedParameterExpression,
    value: int,
    public: bool,
    expected_code: ParametricErrorCode,
) -> None:
    base = _design()
    derived = DesignParameter(
        id=_id("parameter", 4),
        name="Derived straight width",
        kind=ParameterKind.LENGTH,
        value=value,
        unit=DesignUnit.MM,
        evidence_ids=(EVIDENCE,),
        minimum=0.1,
        maximum=1_000,
        public=public,
        expression=expression,
    )

    with pytest.raises(ParametricContractError) as caught:
        dataclasses.replace(base, parameters=(*base.parameters, derived))

    assert caught.value.code is expected_code


def test_derived_parameter_expression_cycle_fails_closed() -> None:
    base = _design()
    derived_id = _id("parameter", 4)
    source = dataclasses.replace(
        base.parameters[0],
        public=False,
        expression=DerivedParameterExpression(terms={derived_id: 1}),
    )
    derived = DesignParameter(
        id=derived_id,
        name="Cyclic derived width",
        kind=ParameterKind.LENGTH,
        value=60,
        unit=DesignUnit.MM,
        evidence_ids=(EVIDENCE,),
        minimum=0.1,
        maximum=1_000,
        public=False,
        expression=DerivedParameterExpression(terms={WIDTH: 1}),
    )

    with pytest.raises(ParametricContractError) as caught:
        dataclasses.replace(
            base,
            parameters=(source, *base.parameters[1:], derived),
        )

    assert caught.value.code is ParametricErrorCode.INVALID_ORDER


def _program_for_design(
    design: object,
    *,
    task_id: str = "task-parametric-design",
    base_revision: str = "revision-parametric-base",
) -> ModelProgram:
    return ModelProgram(
        task_id=task_id,
        base_revision=base_revision,
        operations=(
            ModelCommand(
                id="create-design",
                op="create_parametric_design",
                args={"design": design},
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-parametric-design", criteria=()),
    )


def _json_node_count(value: object) -> int:
    if type(value) is dict:
        return 1 + sum(1 + _json_node_count(item) for item in value.values())
    if type(value) is list:
        return 1 + sum(_json_node_count(item) for item in value)
    return 1


def _near_boundary_design() -> ParametricDesignIR:
    base = _design()
    evidence = (
        base.evidence[0],
        *(
            dataclasses.replace(
                base.evidence[0],
                id=_id("evidence", 1_000 + index),
                source_refs=tuple(f"source-{index}-{item}" for item in range(8)),
                description="Near-boundary task round-trip evidence",
            )
            for index in range(MAX_DESIGN_EVIDENCE - 1)
        ),
    )
    padding_parameters = tuple(
        dataclasses.replace(
            base.parameters[0],
            id=_id("parameter", 1_000 + index),
            name=f"Reserved parameter {index}",
            public=False,
        )
        for index in range(20)
    )
    return dataclasses.replace(
        base,
        evidence=evidence,
        parameters=(*base.parameters, *padding_parameters),
    )


def test_near_boundary_design_round_trips_through_durable_task_contract() -> None:
    task_id = "task_" + "a" * 32
    project_id = "project_" + "b" * 32
    base_revision = "revision_" + "c" * 32
    design = _near_boundary_design()
    design_mapping = design.to_mapping()
    program = _program_for_design(
        design_mapping,
        task_id=task_id,
        base_revision=base_revision,
    )

    validated = validate_model_program(ModelProgram.from_mapping(program.to_mapping()))
    task = new_task_run(
        task_id=task_id,
        project_id=project_id,
        base_revision=base_revision,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=program)
    restored = TaskRun.from_mapping(task.to_mapping())

    assert 3_400 <= _json_node_count(design_mapping) <= 3_500
    assert ParametricDesignIR.from_mapping(validated.commands[0].handler_kwargs["design"]) == design
    assert restored == task
    assert restored.program == program


def test_parametric_design_is_one_strict_frozen_model_program_value() -> None:
    design = _design()
    source = _program_for_design(design.to_mapping())
    restored = ModelProgram.from_mapping(source.to_mapping())

    validated = validate_model_program(restored)
    bound = validated.commands[0].handler_kwargs["design"]

    assert _matches_value_shape(bound, ValueShape.PARAMETRIC_DESIGN_IR)
    assert ParametricDesignIR.from_mapping(bound) == design
    assert validated.commands[0].handler_name == "create_parametric_design"
    assert validated.commands[0].preserve == ()
    with pytest.raises(TypeError):
        bound["name"] = "mutated"  # type: ignore[index]


def test_parametric_modify_binds_and_round_trips_complete_task_contract() -> None:
    task_id = "task_11111111111111111111111111111111"
    project_id = "project_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    revision_id = "revision_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    design = _near_boundary_design()
    selector = {
        "schema_version": 1,
        "project_id": project_id,
        "revision_id": revision_id,
        "entity_kind": "object",
        "object_id": "object_11111111111111111111111111111111",
        "feature_id": None,
        "object_type": "PartDesign::Body",
        "semantic_role": "part",
        "provenance": {"source": "model", "operation_id": "create-design"},
        "expected_cardinality": 1,
    }
    program = ModelProgram(
        task_id=task_id,
        base_revision=revision_id,
        operations=(
            ModelCommand(
                id="modify-depth",
                op="modify_parametric_parameter",
                target={"body": selector},
                args={"design": design.to_mapping(), "parameter_id": PAD_DEPTH, "value": 12.5},
                source=ValueSource.MODEL,
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-parametric-modify", criteria=()),
    )
    bound = validate_model_program(program).commands[0].handler_kwargs
    assert type(bound["target"]) is SelectorV1
    assert bound["target"].semantic_role is SemanticRole.PART
    assert ParametricDesignIR.from_mapping(bound["design"]) == design
    assert (bound["parameter_id"], bound["value"]) == (PAD_DEPTH, 12.5)

    task = new_task_run(
        task_id=task_id,
        project_id=project_id,
        base_revision=revision_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    task = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=program)

    restored = TaskRun.from_mapping(task.to_mapping())
    restored_command = restored.program.operations[0]

    assert restored == task
    assert 3_500 <= _json_node_count(restored.to_mapping()) <= 3_600
    assert ParametricDesignIR.from_mapping(restored_command.args["design"]) == design
    assert restored_command.target["body"] == selector
    assert (restored_command.args["parameter_id"], restored_command.args["value"]) == (
        PAD_DEPTH,
        12.5,
    )

    stale = program.to_mapping()
    stale["base_revision"] = "revision_cccccccccccccccccccccccccccccccc"
    with pytest.raises(ProgramValidationError) as caught:
        validate_model_program(ModelProgram.from_mapping(stale))
    assert caught.value.code is ProgramErrorCode.INVALID_VALUE_SHAPE
    assert caught.value.path == "/operations/0/target/body"

    immediate_edit = program.to_mapping()
    immediate_edit["operations"][0]["target"]["body"] = {
        "command_id": "create-design",
        "slot": "body",
    }
    with pytest.raises(ProgramValidationError) as caught:
        validate_model_program(ModelProgram.from_mapping(immediate_edit))
    assert caught.value.code is ProgramErrorCode.INVALID_VALUE_SHAPE
    assert caught.value.path == "/operations/0/target/body"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value.__setitem__("unexpected", True),
        lambda value: value.__setitem__("schema_version", 2),
        lambda value: value["features"][0].__setitem__("sketch_id", _id("sketch", 99)),
    ),
)
def test_model_program_rejects_malformed_parametric_design_at_atomic_field(
    mutate,
) -> None:
    encoded = _design().to_mapping()
    mutate(encoded)

    with pytest.raises(ProgramValidationError) as caught:
        validate_model_program(_program_for_design(encoded))

    assert caught.value.code is ProgramErrorCode.INVALID_VALUE_SHAPE
    assert caught.value.path == "/operations/0/args/design"


def test_managed_parametric_operation_adopts_body_and_edge_tail_without_echoing_ir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    design = _design_with_fillet()
    events: list[str] = []
    placement = SimpleNamespace(
        Base=SimpleNamespace(x=0.0, y=0.0, z=0.0),
        Rotation=SimpleNamespace(Q=(0.0, 0.0, 0.0, 1.0)),
    )
    shape = SimpleNamespace(
        Volume=19_200.0,
        Area=6_080.0,
        BoundBox=SimpleNamespace(XLength=60.0, YLength=40.0, ZLength=8.0),
        CenterOfMass=SimpleNamespace(x=0.0, y=0.0, z=4.0),
        Solids=(object(),),
        isNull=lambda: False,
        isValid=lambda: True,
    )
    body = SimpleNamespace(
        Name="Body",
        TypeId="PartDesign::Body",
        Placement=placement,
        Shape=shape,
    )
    feature = SimpleNamespace(
        Name="Pad",
        TypeId="PartDesign::Pad",
        Placement=placement,
        Shape=shape,
    )
    treatment = SimpleNamespace(
        Name="Fillet",
        TypeId="Part::Fillet",
        Placement=placement,
        Shape=shape,
    )

    class Session:
        freecad_version = (1, 1)

        def __init__(self) -> None:
            self.doc = SimpleNamespace(Objects=())
            self.identities: list[tuple[object, object]] = []
            self.result = None

        def attach_object_identity(self, obj: object, identity: object) -> object:
            events.append(f"adopt:{obj.TypeId}")  # type: ignore[attr-defined]
            self.identities.append((obj, identity))
            return identity

        def read_object_identity(self, obj: object) -> object:
            return next(identity for current, identity in self.identities if current is obj)

        def list_object_identities(self) -> tuple[tuple[object, object], ...]:
            return tuple(self.identities)

        def set_result_object(self, obj: object) -> None:
            events.append("result")
            self.result = obj

    session = Session()

    def compile_design(session_arg: object, checked: object, *, adopt) -> object:
        assert session_arg is session
        assert checked == design
        events.append("compile")
        compiled = SimpleNamespace(
            design_id=design.id,
            design_digest=design.digest,
            body=body,
            parameter_carrier=object(),
            sketches=(object(),),
            features=(SimpleNamespace(feature_id=design.features[0].id, object=feature),),
            edge_treatments=(
                SimpleNamespace(feature_id=design.edge_treatments[0].id, object=treatment),
            ),
            result_object=treatment,
        )
        session.doc.Objects = (body, feature, treatment)
        adopt(compiled)
        events.append("compiled")
        return compiled

    def facts(obj: object) -> tuple[ParametricEntityFact, ...]:
        common = (ParametricEntityFact("parametric.design_ir_digest", design.digest),)
        if obj is body:
            return common + (
                ParametricEntityFact("parametric.feature_count", 1),
                ParametricEntityFact("parametric.edge_treatment_count", 1),
                ParametricEntityFact("parametric.sketch_count", 1),
            )
        if obj is feature:
            return common + (
                ParametricEntityFact("parametric.feature.index", 0),
                ParametricEntityFact("parametric.feature.kind", "pad"),
                ParametricEntityFact("parametric.shape_valid", True),
                ParametricEntityFact("parametric.solid_count", 1),
            )
        return common + (
            ParametricEntityFact("parametric.edge_treatment.index", 0),
            ParametricEntityFact("parametric.edge_treatment.kind", "fillet"),
            ParametricEntityFact("parametric.edge_treatment.edge_count", 1),
            ParametricEntityFact("parametric.shape_valid", True),
            ParametricEntityFact("parametric.solid_count", 1),
        )

    monkeypatch.setattr(executor_module, "_compile_parametric_design", compile_design)
    monkeypatch.setattr(
        executor_module,
        "_stabilize_parametric_session",
        lambda _session: events.append("stabilize"),
    )
    monkeypatch.setattr(executor_module, "parametric_entity_facts", facts)

    result = executor_module._managed_create_parametric_design(
        session,
        executor_module._InvocationContext(
            operation_id="create-design",
            operation="create_parametric_design",
            preserve=(),
            source=ValueSource.MODEL,
        ),
        design=design.to_mapping(),
    )

    assert events == [
        "stabilize",
        "compile",
        "adopt:PartDesign::Body",
        "adopt:PartDesign::Pad",
        "adopt:Part::Fillet",
        "result",
        "compiled",
        "stabilize",
    ]
    assert session.result is treatment
    assert [identity.semantic_role.value for _, identity in session.identities] == [
        "part",
        "feature",
        "feature",
    ]
    assert session.identities[0][1].feature_id is None
    assert session.identities[1][1].feature_id.startswith("feature_")
    assert result["object_id"] == session.identities[0][1].object_id
    assert result["tip_object_id"] == session.identities[2][1].object_id
    assert len(result["feature_object_ids"]) == 2
    assert "design" not in result


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

    common_multi_profile_parameters = tuple(
        DesignParameter(
            id=_id("parameter", index + 100),
            name=f"Derived{index}",
            kind=ParameterKind.LENGTH,
            value=1,
            unit=DesignUnit.MM,
            evidence_ids=(EVIDENCE,),
        )
        for index in range(70)
    )
    near_boundary = _near_boundary_design()
    supported = dataclasses.replace(
        near_boundary,
        parameters=(*near_boundary.parameters, *common_multi_profile_parameters[:50]),
    )
    assert len(supported.parameters) == 73
    assert 3_500 < _json_node_count(supported.to_mapping()) < 8_192
    assert ParametricDesignIR.from_mapping(supported.to_mapping()) == supported

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
