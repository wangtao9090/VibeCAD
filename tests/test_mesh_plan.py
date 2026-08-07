from __future__ import annotations

import pytest

from vibecad.organic.contracts import (
    MeshMediaType,
    MeshOperation,
    MeshOperationKind,
    MeshOperationPlan,
    MeshProfile,
    MirrorAxis,
    SealedMeshSource,
)
from vibecad.organic.plan import (
    MeshPlanError,
    MeshPlanErrorCode,
    mesh_operation_plan_digest,
    validate_mesh_operation_plan,
)


def _source(triangle_count: int = 100) -> SealedMeshSource:
    return SealedMeshSource(
        source_id="mesh_input_" + "1" * 32,
        sha256="a" * 64,
        media_type=MeshMediaType.STL,
        byte_count=2048,
        vertex_count=80,
        triangle_count=triangle_count,
        millimeters_per_unit=1,
    )


def _op(kind: MeshOperationKind, index: int, **parameters) -> MeshOperation:
    return MeshOperation(
        operation_id="mesh_op_" + f"{index:x}" * 32,
        kind=kind,
        **parameters,
    )


def _required() -> tuple[MeshOperation, ...]:
    return (
        _op(MeshOperationKind.REMOVE_DUPLICATE_VERTICES, 1),
        _op(MeshOperationKind.REMOVE_DUPLICATE_TRIANGLES, 2),
        _op(MeshOperationKind.REMOVE_DEGENERATE_TRIANGLES, 3),
        _op(MeshOperationKind.REMOVE_UNREFERENCED_VERTICES, 4),
        _op(MeshOperationKind.ORIENT_NORMALS, 5),
    )


def _plan(operations: tuple[MeshOperation, ...]) -> MeshOperationPlan:
    return MeshOperationPlan(
        profile=MeshProfile.CLOSED_SURFACE_V1,
        expected_boundary_loops=0,
        operations=operations,
    )


def test_plan_digest_is_deterministic_and_triangle_estimate_is_bounded() -> None:
    plan = _plan(
        _required()
        + (
            _op(MeshOperationKind.QUADRIC_DECIMATE, 6, target_triangles=50),
            _op(MeshOperationKind.MIRROR, 7, axis=MirrorAxis.X),
            _op(MeshOperationKind.SUBDIVISION_SURFACE, 8, level=1),
        )
    )
    first = validate_mesh_operation_plan(_source(), plan)
    second = validate_mesh_operation_plan(_source(), plan)

    assert first == second
    assert first.plan_sha256 == mesh_operation_plan_digest(plan)
    assert first.estimated_output_triangles == 400
    assert first.operation_count == 8


def test_plan_rejects_missing_cleanup_and_noncanonical_order() -> None:
    with pytest.raises(MeshPlanError) as missing:
        validate_mesh_operation_plan(_source(), _plan(_required()[1:]))
    assert missing.value.code is MeshPlanErrorCode.MISSING_REQUIRED_CLEANUP

    reordered = (_required()[1], _required()[0], *_required()[2:])
    with pytest.raises(MeshPlanError) as order:
        validate_mesh_operation_plan(_source(), _plan(reordered))
    assert order.value.code is MeshPlanErrorCode.INVALID_OPERATION_ORDER


def test_plan_rejects_duplicate_id_kind_and_conflicting_simplifiers() -> None:
    duplicate_id = MeshOperation(
        operation_id=_required()[0].operation_id,
        kind=MeshOperationKind.MERGE_CLOSE_VERTICES,
        distance_mm=0.1,
    )
    with pytest.raises(MeshPlanError) as identifier:
        validate_mesh_operation_plan(
            _source(),
            _plan((_required()[0], duplicate_id, *_required()[1:])),
        )
    assert identifier.value.code is MeshPlanErrorCode.DUPLICATE_OPERATION_ID

    with pytest.raises(MeshPlanError) as kind:
        validate_mesh_operation_plan(
            _source(),
            _plan((*_required(), _op(MeshOperationKind.ORIENT_NORMALS, 9))),
        )
    assert kind.value.code is MeshPlanErrorCode.DUPLICATE_OPERATION_KIND

    with pytest.raises(MeshPlanError) as conflict:
        validate_mesh_operation_plan(
            _source(),
            _plan(
                _required()
                + (
                    _op(MeshOperationKind.QUADRIC_DECIMATE, 6, target_triangles=60),
                    _op(MeshOperationKind.VERTEX_CLUSTER, 7, target_triangles=50),
                )
            ),
        )
    assert conflict.value.code is MeshPlanErrorCode.CONFLICTING_SIMPLIFIERS


def test_plan_rejects_nonreducing_simplification_and_subdivision_explosion() -> None:
    with pytest.raises(MeshPlanError) as nonreducing:
        validate_mesh_operation_plan(
            _source(),
            _plan(
                _required() + (_op(MeshOperationKind.QUADRIC_DECIMATE, 6, target_triangles=100),)
            ),
        )
    assert nonreducing.value.code is MeshPlanErrorCode.NON_REDUCING_SIMPLIFIER

    with pytest.raises(MeshPlanError) as budget:
        validate_mesh_operation_plan(
            _source(20_000),
            _plan(
                _required()
                + (
                    _op(MeshOperationKind.MIRROR, 6, axis=MirrorAxis.Y),
                    _op(MeshOperationKind.SUBDIVISION_SURFACE, 7, level=3),
                )
            ),
        )
    assert budget.value.code is MeshPlanErrorCode.TRIANGLE_BUDGET_EXCEEDED
