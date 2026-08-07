"""Deterministic validation and hashing for bounded mesh operation plans."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

from vibecad.organic.contracts import (
    MAX_OUTPUT_TRIANGLES,
    MeshOperation,
    MeshOperationKind,
    MeshOperationPlan,
    SealedMeshSource,
)


class MeshPlanErrorCode(StrEnum):
    DUPLICATE_OPERATION_ID = "duplicate_operation_id"
    DUPLICATE_OPERATION_KIND = "duplicate_operation_kind"
    MISSING_REQUIRED_CLEANUP = "missing_required_cleanup"
    INVALID_OPERATION_ORDER = "invalid_operation_order"
    CONFLICTING_SIMPLIFIERS = "conflicting_simplifiers"
    TRIANGLE_BUDGET_EXCEEDED = "triangle_budget_exceeded"
    NON_REDUCING_SIMPLIFIER = "non_reducing_simplifier"


class MeshPlanError(ValueError):
    def __init__(self, code: MeshPlanErrorCode) -> None:
        if type(code) is not MeshPlanErrorCode:
            raise TypeError("code must be an exact MeshPlanErrorCode")
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class MeshPlanSummary:
    plan_sha256: str
    estimated_output_triangles: int
    operation_count: int


_ORDER = {
    MeshOperationKind.REMOVE_DUPLICATE_VERTICES: 0,
    MeshOperationKind.MERGE_CLOSE_VERTICES: 1,
    MeshOperationKind.REMOVE_DUPLICATE_TRIANGLES: 2,
    MeshOperationKind.REMOVE_DEGENERATE_TRIANGLES: 3,
    MeshOperationKind.REMOVE_UNREFERENCED_VERTICES: 4,
    MeshOperationKind.ORIENT_NORMALS: 5,
    MeshOperationKind.TAUBIN_SMOOTH: 6,
    MeshOperationKind.QUADRIC_DECIMATE: 7,
    MeshOperationKind.VERTEX_CLUSTER: 7,
    MeshOperationKind.MIRROR: 8,
    MeshOperationKind.VOXEL_REMESH: 9,
    MeshOperationKind.SUBDIVISION_SURFACE: 10,
}

_REQUIRED = {
    MeshOperationKind.REMOVE_DUPLICATE_VERTICES,
    MeshOperationKind.REMOVE_DUPLICATE_TRIANGLES,
    MeshOperationKind.REMOVE_DEGENERATE_TRIANGLES,
    MeshOperationKind.REMOVE_UNREFERENCED_VERTICES,
    MeshOperationKind.ORIENT_NORMALS,
}


def _operation_payload(operation: MeshOperation) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": operation.kind.value,
        "operation_id": operation.operation_id,
    }
    if operation.distance_mm is not None:
        result["distance_mm"] = operation.distance_mm
    if operation.iterations is not None:
        result["iterations"] = operation.iterations
    if operation.target_triangles is not None:
        result["target_triangles"] = operation.target_triangles
    if operation.axis is not None:
        result["axis"] = operation.axis.value
    if operation.level is not None:
        result["level"] = operation.level
    return result


def mesh_operation_plan_digest(plan: MeshOperationPlan) -> str:
    if type(plan) is not MeshOperationPlan:
        raise TypeError("plan must be an exact MeshOperationPlan")
    payload = {
        "expected_boundary_loops": plan.expected_boundary_loops,
        "operations": [_operation_payload(operation) for operation in plan.operations],
        "profile": plan.profile.value,
        "schema_version": plan.schema_version,
    }
    raw = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"vibecad-mesh-operation-plan-v1\0" + raw).hexdigest()


def validate_mesh_operation_plan(
    source: SealedMeshSource,
    plan: MeshOperationPlan,
) -> MeshPlanSummary:
    if type(source) is not SealedMeshSource:
        raise TypeError("source must be an exact SealedMeshSource")
    if type(plan) is not MeshOperationPlan:
        raise TypeError("plan must be an exact MeshOperationPlan")

    operation_ids = [operation.operation_id for operation in plan.operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise MeshPlanError(MeshPlanErrorCode.DUPLICATE_OPERATION_ID)
    kinds = [operation.kind for operation in plan.operations]
    if len(kinds) != len(set(kinds)):
        raise MeshPlanError(MeshPlanErrorCode.DUPLICATE_OPERATION_KIND)
    if not _REQUIRED.issubset(kinds):
        raise MeshPlanError(MeshPlanErrorCode.MISSING_REQUIRED_CLEANUP)
    if {
        MeshOperationKind.QUADRIC_DECIMATE,
        MeshOperationKind.VERTEX_CLUSTER,
    }.issubset(kinds):
        raise MeshPlanError(MeshPlanErrorCode.CONFLICTING_SIMPLIFIERS)

    order = [_ORDER[kind] for kind in kinds]
    if order != sorted(order):
        raise MeshPlanError(MeshPlanErrorCode.INVALID_OPERATION_ORDER)

    estimated = source.triangle_count
    for operation in plan.operations:
        if operation.kind in {
            MeshOperationKind.QUADRIC_DECIMATE,
            MeshOperationKind.VERTEX_CLUSTER,
        }:
            assert operation.target_triangles is not None
            if operation.target_triangles >= estimated:
                raise MeshPlanError(MeshPlanErrorCode.NON_REDUCING_SIMPLIFIER)
            estimated = operation.target_triangles
        elif operation.kind is MeshOperationKind.MIRROR:
            estimated *= 2
        elif operation.kind is MeshOperationKind.VOXEL_REMESH:
            assert operation.target_triangles is not None
            estimated = operation.target_triangles
        elif operation.kind is MeshOperationKind.SUBDIVISION_SURFACE:
            assert operation.level is not None
            estimated *= 4**operation.level
        if estimated > MAX_OUTPUT_TRIANGLES:
            raise MeshPlanError(MeshPlanErrorCode.TRIANGLE_BUDGET_EXCEEDED)

    return MeshPlanSummary(
        plan_sha256=mesh_operation_plan_digest(plan),
        estimated_output_triangles=estimated,
        operation_count=len(plan.operations),
    )
