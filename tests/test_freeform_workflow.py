from __future__ import annotations

import copy

import pytest

from vibecad.execution.revisions import ProjectHead, RevisionArtifactRef, RevisionRef
from vibecad.freeform.contracts import (
    CurveRole,
    FreeformContractError,
    FreeformDesign,
    FreeformFeature,
    FreeformFeatureKind,
    Point3D,
    SplineCurve,
    SplineKind,
)
from vibecad.workflow.freeform_create import (
    FreeformCreateError,
    build_freeform_create_binding,
    parse_bound_freeform_create_task,
)
from vibecad.workflow.program import ProgramValidationError, validate_model_program
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    new_task_run,
    transition_task,
)

PROJECT_ID = "project_" + "1" * 32
BASE_REVISION = "revision_" + "2" * 32
CREATE_KEY = "task_create_" + "3" * 32


def _section(suffix: str, z_mm: float) -> SplineCurve:
    return SplineCurve(
        f"freeform_curve_{suffix * 32}",
        f"section-{suffix}",
        CurveRole.SECTION,
        SplineKind.BSPLINE,
        2,
        (
            Point3D(-5, -5, z_mm),
            Point3D(5, -5, z_mm),
            Point3D(5, 5, z_mm),
            Point3D(-5, 5, z_mm),
            Point3D(-5, -5, z_mm),
        ),
        (0, 0.5, 1),
        (3, 2, 3),
        (),
        True,
    )


def _design() -> FreeformDesign:
    first = _section("a", 0)
    second = _section("b", 10)
    return FreeformDesign(
        "freeform_design_" + "c" * 32,
        "workflow loft",
        (first, second),
        FreeformFeature(
            "freeform_feature_" + "d" * 32,
            "result loft",
            FreeformFeatureKind.LOFT,
            (first.id, second.id),
        ),
    )


def _large_section(suffix: str, count: int, z_mm: float) -> SplineCurve:
    points = [Point3D(0, 0, z_mm)]
    points.extend(Point3D(index, index % 7, z_mm) for index in range(1, count - 1))
    points.append(points[0])
    interior = count - 3
    return SplineCurve(
        f"freeform_curve_{suffix * 32}",
        f"large-{suffix}",
        CurveRole.SECTION,
        SplineKind.BSPLINE,
        2,
        tuple(points),
        tuple(range(interior + 2)),
        (3,) + (1,) * interior + (3,),
        (),
        True,
    )


def _large_design(counts: tuple[int, ...]) -> FreeformDesign:
    curves = tuple(
        _large_section(chr(ord("a") + index), count, index * 10)
        for index, count in enumerate(counts)
    )
    return FreeformDesign(
        "freeform_design_" + "e" * 32,
        "large integration design",
        curves,
        FreeformFeature(
            "freeform_feature_" + "f" * 32,
            "large loft",
            FreeformFeatureKind.LOFT,
            tuple(curve.id for curve in curves),
        ),
    )


def _empty_head() -> ProjectHead:
    return ProjectHead(
        project_id=PROJECT_ID,
        generation=0,
        revision_id=BASE_REVISION,
        manifest_sha256="4" * 64,
    )


def _empty_revision() -> RevisionRef:
    return RevisionRef(
        id=BASE_REVISION,
        project_id=PROJECT_ID,
        base_revision=None,
        manifest_sha256="4" * 64,
        model=None,
        artifacts=(),
    )


def _bound_task():
    binding = build_freeform_create_binding(
        create_key=CREATE_KEY,
        project_id=PROJECT_ID,
        expected_head=_empty_head(),
        empty_revision=_empty_revision(),
        design=_design(),
    )
    task = new_task_run(
        task_id=binding.task_id,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
        creation_digest=binding.creation_digest,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    return binding, transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=binding.program)


def test_reserved_freeform_binding_round_trips_exact_design_and_authority() -> None:
    binding, task = _bound_task()

    parsed = parse_bound_freeform_create_task(task)

    assert parsed == binding
    assert binding.design == _design()
    assert binding.design_digest == _design().digest
    assert binding.expected_head == _empty_head()
    assert binding.empty_revision == _empty_revision()
    assert binding.program.operations[0].op == "system.create_freeform_design"
    assert binding.program.operations[0].args["design_sha256"] == _design().digest
    with pytest.raises(ProgramValidationError):
        validate_model_program(binding.program)


def test_reserved_freeform_binding_rejects_nonempty_or_advanced_head() -> None:
    nonempty = RevisionRef(
        id=BASE_REVISION,
        project_id=PROJECT_ID,
        base_revision=None,
        manifest_sha256="4" * 64,
        model=RevisionArtifactRef(
            id="artifact_" + "5" * 32,
            name="model.FCStd",
            format="fcstd",
            sha256="6" * 64,
            size_bytes=1,
        ),
        artifacts=(),
    )
    advanced = ProjectHead(
        project_id=PROJECT_ID,
        generation=1,
        revision_id=BASE_REVISION,
        manifest_sha256="4" * 64,
    )

    with pytest.raises(FreeformCreateError):
        build_freeform_create_binding(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            expected_head=advanced,
            empty_revision=_empty_revision(),
            design=_design(),
        )
    with pytest.raises(FreeformCreateError):
        build_freeform_create_binding(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            expected_head=_empty_head(),
            empty_revision=nonempty,
            design=_design(),
        )


def test_reserved_freeform_parser_recomputes_design_digest() -> None:
    binding, task = _bound_task()
    mapping = binding.program.to_mapping()
    mapping["operations"][0]["args"]["design_sha256"] = "f" * 64  # type: ignore[index]
    forged = type(binding.program).from_mapping(mapping)
    forged_task = transition_task(
        new_task_run(
            task_id=binding.task_id,
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
            creation_digest=binding.creation_digest,
        ),
        TaskEvent.REQUEST_PLAN,
    )
    forged_task = transition_task(forged_task, TaskEvent.SUBMIT_PROGRAM, program=forged)

    assert parse_bound_freeform_create_task(forged_task) is None
    assert parse_bound_freeform_create_task(task) == binding


def test_reserved_freeform_integration_budget_is_stricter_than_ir_budget() -> None:
    mapping = copy.deepcopy(_design().to_mapping())
    # Exact duplicate curves are rejected by the design contract before the
    # private integration path can spend work materializing them.
    mapping["curves"] = mapping["curves"] * 16
    with pytest.raises(FreeformContractError):
        FreeformDesign.from_mapping(mapping)

    with pytest.raises(FreeformCreateError):
        build_freeform_create_binding(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            expected_head=_empty_head(),
            empty_revision=_empty_revision(),
            design=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("counts", ((43, 43, 43), (55, 55)))
def test_reserved_freeform_integration_rejects_control_point_or_node_overage(
    counts: tuple[int, ...],
) -> None:
    design = _large_design(counts)

    with pytest.raises(FreeformCreateError) as caught:
        build_freeform_create_binding(
            create_key=CREATE_KEY,
            project_id=PROJECT_ID,
            expected_head=_empty_head(),
            empty_revision=_empty_revision(),
            design=design,
        )

    assert caught.value.code.value == "budget_exceeded"
