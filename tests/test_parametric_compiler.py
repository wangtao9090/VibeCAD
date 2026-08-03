"""Focused FreeCAD-bound compiler and parametric observation tests."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.parametric.compiler as compiler_module
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


def test_compiler_rejects_non_ir_before_loading_cad_runtime() -> None:
    with pytest.raises(ParametricCompileError) as caught:
        compile_design_sketches(object(), object())

    assert caught.value.code is ParametricCompileErrorCode.INVALID_INPUT
    assert caught.value.path == "/design"


def test_compiler_fails_closed_on_slot_before_cad_mutation() -> None:
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
    assert caught.value.path == "/sketches/0/geometries/0/kind"
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


def test_subtractive_profiles_reject_multiple_wires_until_each_cut_is_proven() -> None:
    for kind in (FeatureKind.POCKET, FeatureKind.HOLE):
        with pytest.raises(ParametricCompileError) as caught:
            compiler_module._require_supported_feature_profile(
                kind,
                2,
                path="/features/1",
            )

        assert caught.value.code is ParametricCompileErrorCode.UNSUPPORTED
        assert caught.value.path == "/features/1"

    compiler_module._require_supported_feature_profile(FeatureKind.PAD, 2)
    compiler_module._require_supported_feature_profile(FeatureKind.REVOLVE, 2)
    compiler_module._require_supported_feature_profile(FeatureKind.HOLE, 1)


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
