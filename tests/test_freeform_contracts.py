from __future__ import annotations

import copy

import pytest

from vibecad.freeform.contracts import (
    MAX_CURVE_CONTROL_POINTS,
    MAX_FREEFORM_CURVES,
    MAX_FREEFORM_GUIDES,
    MAX_FREEFORM_IR_BYTES,
    MAX_FREEFORM_SECTIONS,
    CurveRole,
    FreeformContractError,
    FreeformDesign,
    FreeformErrorCode,
    FreeformFeature,
    FreeformFeatureKind,
    Point3D,
    SplineCurve,
    SplineKind,
)


def _curve(
    suffix: str,
    z_mm: float,
    *,
    role: CurveRole = CurveRole.SECTION,
    kind: SplineKind = SplineKind.BSPLINE,
) -> SplineCurve:
    points = (
        Point3D(0, 0, z_mm),
        Point3D(10, 0, z_mm),
        Point3D(10, 10, z_mm),
        Point3D(0, 10, z_mm),
        Point3D(0, 0, z_mm),
    )
    return SplineCurve(
        f"freeform_curve_{suffix * 32}",
        f"section-{suffix}",
        role,
        kind,
        2,
        points,
        (0, 0.5, 1),
        (3, 2, 3),
        (1, 1, 1, 1, 1) if kind is SplineKind.NURBS else (),
        role is CurveRole.SECTION,
    )


def _design() -> FreeformDesign:
    first = _curve("a", 0)
    second = _curve("b", 10, kind=SplineKind.NURBS)
    return FreeformDesign(
        f"freeform_design_{'c' * 32}",
        "bounded loft",
        (first, second),
        FreeformFeature(
            f"freeform_feature_{'d' * 32}",
            "loft result",
            FreeformFeatureKind.LOFT,
            (first.id, second.id),
        ),
    )


def test_round_trip_is_canonical_and_digest_is_stable() -> None:
    design = _design()
    restored = FreeformDesign.from_mapping(copy.deepcopy(design.to_mapping()))

    assert restored == design
    assert restored.to_canonical_json() == design.to_canonical_json()
    assert restored.digest == design.digest
    assert len(design.digest) == 64


def test_nurbs_requires_one_positive_weight_per_control_point() -> None:
    mapping = _curve("a", 0, kind=SplineKind.NURBS).to_mapping()
    mapping["weights"] = [1, 1, 0, 1, 1]

    with pytest.raises(FreeformContractError) as raised:
        SplineCurve.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.INVALID_VALUE
    assert raised.value.path == "/weights"


def test_closed_sections_and_open_guides_are_enforced() -> None:
    mapping = _curve("a", 0).to_mapping()
    mapping["closed"] = False

    with pytest.raises(FreeformContractError) as raised:
        SplineCurve.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.INVALID_ROLE


def test_knot_multiplicity_must_match_degree_and_pole_count() -> None:
    mapping = _curve("a", 0).to_mapping()
    mapping["multiplicities"] = [3, 1, 3]

    with pytest.raises(FreeformContractError) as raised:
        SplineCurve.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.INVALID_VALUE
    assert raised.value.path == "/multiplicities"


def test_loft_rejects_guides_and_sweep_requires_exactly_one() -> None:
    section = _curve("a", 0)
    guide_mapping = section.to_mapping()
    guide_mapping.update(
        {
            "id": f"freeform_curve_{'b' * 32}",
            "name": "guide-b",
            "role": "guide",
            "closed": False,
        }
    )
    guide_points = list(guide_mapping["control_points"])
    guide_points[-1] = Point3D(0, 0, 20).to_mapping()
    guide_mapping["control_points"] = guide_points
    guide = SplineCurve.from_mapping(guide_mapping)

    with pytest.raises(FreeformContractError):
        FreeformFeature(
            f"freeform_feature_{'d' * 32}",
            "bad loft",
            FreeformFeatureKind.LOFT,
            (section.id, section.id),
            (guide.id,),
        )

    sweep = FreeformFeature(
        f"freeform_feature_{'e' * 32}",
        "sweep",
        FreeformFeatureKind.SWEEP,
        (section.id,),
        (guide.id,),
    )
    design = FreeformDesign(f"freeform_design_{'f' * 32}", "sweep design", (section, guide), sweep)
    assert design.feature.kind is FreeformFeatureKind.SWEEP


def test_unknown_and_wrong_role_references_fail_closed() -> None:
    mapping = _design().to_mapping()
    mapping["feature"]["section_ids"][0] = f"freeform_curve_{'e' * 32}"

    with pytest.raises(FreeformContractError) as raised:
        FreeformDesign.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.UNKNOWN_REFERENCE
    assert raised.value.path == "/feature/section_ids/0"


def test_unknown_fields_and_non_solid_features_are_rejected() -> None:
    mapping = _design().to_mapping()
    mapping["surprise"] = True
    with pytest.raises(FreeformContractError) as unknown:
        FreeformDesign.from_mapping(mapping)
    assert unknown.value.code is FreeformErrorCode.UNKNOWN_FIELD

    feature_mapping = _design().feature.to_mapping()
    feature_mapping["solid"] = False
    with pytest.raises(FreeformContractError) as non_solid:
        FreeformFeature.from_mapping(feature_mapping)
    assert non_solid.value.code is FreeformErrorCode.INVALID_VALUE


def test_hostile_unknown_field_names_still_return_bounded_contract_error() -> None:
    mapping = _design().to_mapping()
    mapping["bad/" + "x" * 600] = True

    with pytest.raises(FreeformContractError) as raised:
        FreeformDesign.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.UNKNOWN_FIELD
    assert raised.value.path == "/__unknown__"


@pytest.mark.parametrize(
    ("field", "values", "path"),
    (
        ("control_points", [{}] * (MAX_CURVE_CONTROL_POINTS + 1), "/curves/0/control_points"),
        ("weights", [1] * (MAX_CURVE_CONTROL_POINTS + 1), "/curves/0/weights"),
        ("knots", [0] * (MAX_CURVE_CONTROL_POINTS + 7), "/curves/0/knots"),
        (
            "multiplicities",
            [1] * (MAX_CURVE_CONTROL_POINTS + 7),
            "/curves/0/multiplicities",
        ),
    ),
)
def test_curve_sequences_reject_budget_before_nested_parsing(field, values, path) -> None:
    mapping = _curve("a", 0).to_mapping()
    mapping[field] = values

    with pytest.raises(FreeformContractError) as raised:
        SplineCurve.from_mapping(mapping, "/curves/0")

    assert raised.value.code is FreeformErrorCode.BUDGET_EXCEEDED
    assert raised.value.path == path


@pytest.mark.parametrize(
    ("kind", "field", "count", "path"),
    (
        ("loft", "section_ids", MAX_FREEFORM_SECTIONS + 1, "/feature/section_ids"),
        ("sweep", "guide_ids", MAX_FREEFORM_GUIDES + 1, "/feature/guide_ids"),
    ),
)
def test_feature_reference_sequences_are_bounded_before_tuple_creation(
    kind, field, count, path
) -> None:
    mapping = _design().feature.to_mapping()
    mapping["kind"] = kind
    mapping[field] = [f"freeform_curve_{'a' * 32}"] * count

    with pytest.raises(FreeformContractError) as raised:
        FreeformFeature.from_mapping(mapping, "/feature")

    assert raised.value.code is FreeformErrorCode.BUDGET_EXCEEDED
    assert raised.value.path == path


def test_curve_count_budget_precedes_curve_object_parsing() -> None:
    mapping = _design().to_mapping()
    mapping["curves"] = [None] * (MAX_FREEFORM_CURVES + 1)

    with pytest.raises(FreeformContractError) as raised:
        FreeformDesign.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.BUDGET_EXCEEDED
    assert raised.value.path == "/curves"


def test_total_control_point_budget_precedes_point_object_parsing() -> None:
    mapping = _design().to_mapping()
    raw_curve = _curve("a", 0).to_mapping()
    raw_curve["control_points"] = [{}] * MAX_CURVE_CONTROL_POINTS
    mapping["curves"] = [copy.deepcopy(raw_curve) for _ in range(9)]

    with pytest.raises(FreeformContractError) as raised:
        FreeformDesign.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.BUDGET_EXCEEDED
    assert raised.value.path == "/curves"


def test_encoded_ir_budget_precedes_nested_contract_parsing() -> None:
    mapping = _design().to_mapping()
    mapping["name"] = "x" * (MAX_FREEFORM_IR_BYTES + 1)

    with pytest.raises(FreeformContractError) as raised:
        FreeformDesign.from_mapping(mapping)

    assert raised.value.code is FreeformErrorCode.BUDGET_EXCEEDED
    assert raised.value.path == ""
