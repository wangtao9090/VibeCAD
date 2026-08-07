"""Parametric CAD outcome checks for the Guided Photo v1 positive fixtures."""

from __future__ import annotations

import json
import math
import os

import pytest

from tests.guided_photo_designs import guided_photo_targets
from vibecad.parametric import FeatureKind, ParametricDesignIR
from vibecad.parametric.compiler import (
    ParametricCompileError,
    ParametricCompileErrorCode,
    compile_parametric_design,
    modify_parametric_parameter,
    parametric_entity_facts,
    stabilize_parametric_session,
)


def _json_node_count(value: object) -> int:
    if isinstance(value, dict):
        return 1 + sum(1 + _json_node_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_json_node_count(item) for item in value)
    return 1


def test_guided_photo_targets_are_strict_round_trippable_bounded_ir() -> None:
    targets = guided_photo_targets()

    assert {target.case_id for target in targets} == {
        "guided-photo-washer-ready",
        "guided-photo-fan-spacer-ready",
        "guided-photo-calibration-block-ready",
    }
    assert tuple(feature.kind for feature in targets[0].design.features) == (
        FeatureKind.PAD,
        FeatureKind.HOLE,
    )
    assert tuple(feature.kind for feature in targets[1].design.features) == (
        FeatureKind.PAD,
        FeatureKind.POCKET,
        FeatureKind.HOLE,
    )
    assert tuple(feature.kind for feature in targets[2].design.features) == (
        FeatureKind.PAD,
        FeatureKind.POCKET,
    )
    for target in targets:
        mapping = target.design.to_mapping()
        assert ParametricDesignIR.from_mapping(mapping) == target.design
        assert _json_node_count(mapping) <= 4_096
        assert len(json.dumps(mapping, separators=(",", ":"), sort_keys=True)) <= 65_536
        assert target.depth_parameter_id in {parameter.id for parameter in target.design.parameters}
        assert all(
            evidence.status.value == "confirmed" and evidence.origin.value == "user"
            for evidence in target.design.evidence
        )


@pytest.mark.slow
@pytest.mark.parametrize("target", guided_photo_targets(), ids=lambda target: target.case_id)
def test_real_guided_photo_target_is_editable_valid_single_solid(target) -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    session = Session()
    session.open_document("GuidedPhotoTarget")
    try:
        compiled = compile_parametric_design(session, target.design)
        stabilize_parametric_session(session)

        bounds = compiled.body.Shape.BoundBox
        actual_bbox = (float(bounds.XLength), float(bounds.YLength), float(bounds.ZLength))
        assert actual_bbox == pytest.approx(target.expected_bbox_mm, abs=1e-7)
        assert float(compiled.body.Shape.Volume) == pytest.approx(
            target.expected_volume_mm3,
            rel=target.volume_relative_tolerance,
            abs=1e-6,
        )
        assert compiled.body.Shape.isValid()
        assert len(tuple(compiled.body.Shape.Solids)) == 1
        assert all(binding.solver.fully_constrained for binding in compiled.sketches)
        assert all(binding.solver.dof == 0 for binding in compiled.sketches)

        before_volume = float(compiled.body.Shape.Volume)
        parameter = next(
            item for item in target.design.parameters if item.id == target.depth_parameter_id
        )
        edit = modify_parametric_parameter(
            session,
            target.design,
            body=compiled.body,
            parameter_id=parameter.id,
            value=float(parameter.value) + 1,
        )
        assert edit.before_value == pytest.approx(float(parameter.value))
        assert edit.after_value == pytest.approx(float(parameter.value) + 1)
        assert not math.isclose(float(compiled.body.Shape.Volume), before_volume, abs_tol=1e-6)
        assert compiled.body.Shape.isValid()
        assert len(tuple(compiled.body.Shape.Solids)) == 1
    finally:
        session.close_document()


@pytest.mark.slow
def test_real_fan_outer_width_drives_derived_rounded_rectangle_constraints() -> None:
    if not os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON"):
        pytest.skip("managed FreeCAD Python was not requested")

    from vibecad.engine.session import Session

    target = next(item for item in guided_photo_targets() if "fan-spacer" in item.case_id)
    source = next(item for item in target.design.parameters if item.name == "Outer width")
    radius = next(item for item in target.design.parameters if item.name == "Outer corner radius")
    derived = next(
        item
        for item in target.design.parameters
        if item.name == "Outer rounded square derived straight width"
    )
    assert derived.expression is not None
    assert source.id in derived.expression.terms

    session = Session()
    session.open_document("GuidedPhotoDerivedExpressions")
    try:
        compiled = compile_parametric_design(session, target.design)
        before_volume = float(compiled.body.Shape.Volume)

        width_edit = modify_parametric_parameter(
            session,
            target.design,
            body=compiled.body,
            parameter_id=source.id,
            value=130,
        )
        stabilize_parametric_session(session)

        bounds = compiled.body.Shape.BoundBox
        assert (
            float(bounds.XLength),
            float(bounds.YLength),
            float(bounds.ZLength),
        ) == pytest.approx(
            (130, 120, 5),
            abs=1e-7,
        )
        assert float(compiled.body.Shape.Volume) > before_volume
        assert compiled.body.Shape.isValid()
        assert len(tuple(compiled.body.Shape.Solids)) == 1
        assert width_edit.consumer_ids == (target.design.sketches[0].id,)
        facts = {
            item.name: item.value for item in parametric_entity_facts(compiled.parameter_carrier)
        }
        suffix = derived.id.rsplit("_", 1)[-1]
        assert facts[f"parametric.parameter.{suffix}"] == pytest.approx(114)

        radius_edit = modify_parametric_parameter(
            session,
            target.design,
            body=compiled.body,
            parameter_id=radius.id,
            value=10,
        )
        stabilize_parametric_session(session)
        facts = {
            item.name: item.value for item in parametric_entity_facts(compiled.parameter_carrier)
        }
        assert facts[f"parametric.parameter.{suffix}"] == pytest.approx(110)
        assert radius_edit.consumer_ids == (target.design.sketches[0].id,)
        assert compiled.body.Shape.isValid()
        assert len(tuple(compiled.body.Shape.Solids)) == 1

        volume_before_rejection = float(compiled.body.Shape.Volume)
        with pytest.raises(ParametricCompileError) as caught:
            modify_parametric_parameter(
                session,
                target.design,
                body=compiled.body,
                parameter_id=source.id,
                value=10,
            )
        assert caught.value.code is ParametricCompileErrorCode.INVALID_INPUT
        assert float(compiled.body.Shape.Volume) == pytest.approx(volume_before_rejection)
    finally:
        session.close_document()
