"""Local fail-closed mesh validation for the first Mesh/SubD slice."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from vibecad.organic.contracts import (
    MAX_SOURCE_TRIANGLES,
    MAX_SOURCE_VERTICES,
    MeshProfile,
)


class MeshValidationErrorCode(StrEnum):
    INVALID_VERTICES = "invalid_vertices"
    INVALID_TRIANGLES = "invalid_triangles"
    BUDGET_EXCEEDED = "budget_exceeded"
    INDEX_OUT_OF_RANGE = "index_out_of_range"
    INVALID_EVIDENCE = "invalid_evidence"
    EVIDENCE_MISMATCH = "evidence_mismatch"


class MeshValidationError(ValueError):
    def __init__(self, code: MeshValidationErrorCode) -> None:
        if type(code) is not MeshValidationErrorCode:
            raise TypeError("code must be an exact MeshValidationErrorCode")
        self.code = code
        super().__init__(code.value)


class MeshValidationStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


def _bounded_text(value: object) -> str:
    if type(value) is not str:
        raise MeshValidationError(MeshValidationErrorCode.INVALID_EVIDENCE)
    try:
        raw = value.encode("utf-8")
    except UnicodeError as exc:
        raise MeshValidationError(MeshValidationErrorCode.INVALID_EVIDENCE) from exc
    if (
        not raw
        or len(raw) > 256
        or value.strip() != value
        or not value.isprintable()
        or len(value.splitlines()) != 1
    ):
        raise MeshValidationError(MeshValidationErrorCode.INVALID_EVIDENCE)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SelfIntersectionEvidence:
    mesh_sha256: str
    intersection_count: int
    engine: str
    engine_version: str

    def __post_init__(self) -> None:
        if (
            type(self.mesh_sha256) is not str
            or len(self.mesh_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.mesh_sha256)
        ):
            raise MeshValidationError(MeshValidationErrorCode.INVALID_EVIDENCE)
        if (
            type(self.intersection_count) is not int
            or not 0 <= self.intersection_count <= 2**53 - 1
        ):
            raise MeshValidationError(MeshValidationErrorCode.INVALID_EVIDENCE)
        object.__setattr__(self, "engine", _bounded_text(self.engine))
        object.__setattr__(self, "engine_version", _bounded_text(self.engine_version))


@dataclass(frozen=True, slots=True, kw_only=True)
class MeshValidationReport:
    profile: MeshProfile
    mesh_sha256: str
    status: MeshValidationStatus
    vertex_count: int
    triangle_count: int
    bounding_box_min: tuple[float, float, float]
    bounding_box_max: tuple[float, float, float]
    connected_components: int
    boundary_edge_count: int
    boundary_loop_count: int | None
    non_manifold_edge_count: int
    inconsistent_orientation_edge_count: int
    duplicate_triangle_count: int
    degenerate_triangle_count: int
    unreferenced_vertex_count: int
    signed_volume: float
    self_intersection_count: int | None
    issues: tuple[str, ...]


def _snapshot_arrays(
    vertices: object,
    triangles: object,
) -> tuple[np.ndarray, np.ndarray]:
    if type(vertices) is not np.ndarray or vertices.ndim != 2 or vertices.shape[1:] != (3,):
        raise MeshValidationError(MeshValidationErrorCode.INVALID_VERTICES)
    if vertices.dtype.kind not in "fiu" or vertices.dtype.kind == "b":
        raise MeshValidationError(MeshValidationErrorCode.INVALID_VERTICES)
    if not 0 < vertices.shape[0] <= MAX_SOURCE_VERTICES:
        raise MeshValidationError(MeshValidationErrorCode.BUDGET_EXCEEDED)
    vertex_snapshot = np.ascontiguousarray(vertices, dtype="<f8")
    if not np.isfinite(vertex_snapshot).all():
        raise MeshValidationError(MeshValidationErrorCode.INVALID_VERTICES)

    if type(triangles) is not np.ndarray or triangles.ndim != 2 or triangles.shape[1:] != (3,):
        raise MeshValidationError(MeshValidationErrorCode.INVALID_TRIANGLES)
    if triangles.dtype.kind not in "iu" or triangles.dtype.kind == "b":
        raise MeshValidationError(MeshValidationErrorCode.INVALID_TRIANGLES)
    if not 0 < triangles.shape[0] <= MAX_SOURCE_TRIANGLES:
        raise MeshValidationError(MeshValidationErrorCode.BUDGET_EXCEEDED)
    triangle_snapshot = np.ascontiguousarray(triangles, dtype="<i8")
    if triangle_snapshot.min() < 0 or triangle_snapshot.max() >= vertex_snapshot.shape[0]:
        raise MeshValidationError(MeshValidationErrorCode.INDEX_OUT_OF_RANGE)
    return vertex_snapshot, triangle_snapshot


def _mesh_digest(vertices: np.ndarray, triangles: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(b"vibecad-canonical-triangle-mesh-v1\0")
    digest.update(vertices.shape[0].to_bytes(8, "big"))
    digest.update(triangles.shape[0].to_bytes(8, "big"))
    digest.update(vertices.tobytes(order="C"))
    digest.update(triangles.tobytes(order="C"))
    return digest.hexdigest()


def mesh_content_digest(vertices: object, triangles: object) -> str:
    vertex_snapshot, triangle_snapshot = _snapshot_arrays(vertices, triangles)
    return _mesh_digest(vertex_snapshot, triangle_snapshot)


def _connected_components(vertex_count: int, triangles: np.ndarray) -> int:
    parent = np.arange(vertex_count, dtype=np.int64)
    rank = np.zeros(vertex_count, dtype=np.uint8)

    def find(value: int) -> int:
        root = value
        while int(parent[root]) != root:
            root = int(parent[root])
        while int(parent[value]) != value:
            next_value = int(parent[value])
            parent[value] = root
            value = next_value
        return root

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        if rank[left_root] < rank[right_root]:
            left_root, right_root = right_root, left_root
        parent[right_root] = left_root
        if rank[left_root] == rank[right_root]:
            rank[left_root] += 1

    for first, second, third in triangles.tolist():
        union(first, second)
        union(second, third)
    referenced = np.unique(triangles)
    return len({find(int(value)) for value in referenced})


def _boundary_loops(boundary_edges: np.ndarray) -> int | None:
    if boundary_edges.shape[0] == 0:
        return 0
    adjacency: dict[int, set[int]] = {}
    for first, second in boundary_edges.tolist():
        adjacency.setdefault(first, set()).add(second)
        adjacency.setdefault(second, set()).add(first)
    if any(len(neighbors) != 2 for neighbors in adjacency.values()):
        return None
    remaining = set(adjacency)
    components = 0
    while remaining:
        components += 1
        stack = [remaining.pop()]
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
    return components


def validate_mesh(
    vertices: object,
    triangles: object,
    *,
    profile: MeshProfile,
    expected_boundary_loops: int,
    self_intersection_evidence: SelfIntersectionEvidence | None = None,
) -> MeshValidationReport:
    if type(profile) is not MeshProfile:
        raise TypeError("profile must be an exact MeshProfile")
    if type(expected_boundary_loops) is not int or expected_boundary_loops < 0:
        raise ValueError("expected_boundary_loops must be a non-negative integer")
    if profile is MeshProfile.CLOSED_SURFACE_V1 and expected_boundary_loops != 0:
        raise ValueError("closed_surface_v1 requires zero expected boundary loops")
    if profile is MeshProfile.OPEN_SURFACE_V1 and not 1 <= expected_boundary_loops <= 128:
        raise ValueError("open_surface_v1 requires one to 128 expected boundary loops")
    if (
        self_intersection_evidence is not None
        and type(self_intersection_evidence) is not SelfIntersectionEvidence
    ):
        raise MeshValidationError(MeshValidationErrorCode.INVALID_EVIDENCE)

    vertex_snapshot, triangle_snapshot = _snapshot_arrays(vertices, triangles)
    mesh_sha256 = _mesh_digest(vertex_snapshot, triangle_snapshot)
    if (
        self_intersection_evidence is not None
        and self_intersection_evidence.mesh_sha256 != mesh_sha256
    ):
        raise MeshValidationError(MeshValidationErrorCode.EVIDENCE_MISMATCH)

    canonical_triangles = np.sort(triangle_snapshot, axis=1)
    _, duplicate_counts = np.unique(canonical_triangles, axis=0, return_counts=True)
    duplicate_triangle_count = int(np.sum(np.maximum(duplicate_counts - 1, 0)))

    first = vertex_snapshot[triangle_snapshot[:, 0]]
    second = vertex_snapshot[triangle_snapshot[:, 1]]
    third = vertex_snapshot[triangle_snapshot[:, 2]]
    diagonal = float(np.linalg.norm(vertex_snapshot.max(axis=0) - vertex_snapshot.min(axis=0)))
    area_tolerance = max(diagonal, 1.0) * 1e-12
    repeated_index = (
        (triangle_snapshot[:, 0] == triangle_snapshot[:, 1])
        | (triangle_snapshot[:, 1] == triangle_snapshot[:, 2])
        | (triangle_snapshot[:, 2] == triangle_snapshot[:, 0])
    )
    doubled_area = np.linalg.norm(np.cross(second - first, third - first), axis=1)
    degenerate_triangle_count = int(
        np.count_nonzero(repeated_index | (doubled_area <= area_tolerance))
    )

    referenced = np.unique(triangle_snapshot)
    unreferenced_vertex_count = int(vertex_snapshot.shape[0] - referenced.shape[0])

    directed_edges = np.concatenate(
        (
            triangle_snapshot[:, (0, 1)],
            triangle_snapshot[:, (1, 2)],
            triangle_snapshot[:, (2, 0)],
        ),
        axis=0,
    )
    undirected_edges = np.sort(directed_edges, axis=1)
    unique_edges, edge_inverse, edge_counts = np.unique(
        undirected_edges,
        axis=0,
        return_inverse=True,
        return_counts=True,
    )
    boundary_mask = edge_counts == 1
    boundary_edges = unique_edges[boundary_mask]
    boundary_edge_count = int(np.count_nonzero(boundary_mask))
    non_manifold_edge_count = int(np.count_nonzero(edge_counts > 2))
    directions = np.where(directed_edges[:, 0] < directed_edges[:, 1], 1, -1)
    direction_sums = np.bincount(edge_inverse, weights=directions, minlength=len(unique_edges))
    inconsistent_orientation_edge_count = int(
        np.count_nonzero((edge_counts == 2) & (direction_sums != 0))
    )
    boundary_loop_count = _boundary_loops(boundary_edges)
    connected_components = _connected_components(vertex_snapshot.shape[0], triangle_snapshot)
    signed_volume = float(np.einsum("ij,ij->i", first, np.cross(second, third)).sum() / 6.0)
    if not math.isfinite(signed_volume):
        raise MeshValidationError(MeshValidationErrorCode.INVALID_VERTICES)

    issues: set[str] = set()
    if duplicate_triangle_count:
        issues.add("duplicate_triangles")
    if degenerate_triangle_count:
        issues.add("degenerate_triangles")
    if unreferenced_vertex_count:
        issues.add("unreferenced_vertices")
    if connected_components != 1:
        issues.add("disconnected_components")
    if non_manifold_edge_count:
        issues.add("non_manifold_edges")
    if inconsistent_orientation_edge_count:
        issues.add("inconsistent_orientation")
    if boundary_loop_count is None:
        issues.add("boundary_is_not_closed_loops")
    elif boundary_loop_count != expected_boundary_loops:
        issues.add("boundary_loop_mismatch")
    if profile is MeshProfile.CLOSED_SURFACE_V1 and signed_volume <= 0.0:
        issues.add("non_positive_signed_volume")

    self_intersection_count: int | None = None
    if self_intersection_evidence is None:
        issues.add("self_intersection_not_checked")
    else:
        self_intersection_count = self_intersection_evidence.intersection_count
        if self_intersection_count:
            issues.add("self_intersections")

    if not issues:
        status = MeshValidationStatus.PASS
    elif issues == {"self_intersection_not_checked"}:
        status = MeshValidationStatus.INDETERMINATE
    else:
        status = MeshValidationStatus.FAIL

    return MeshValidationReport(
        profile=profile,
        mesh_sha256=mesh_sha256,
        status=status,
        vertex_count=int(vertex_snapshot.shape[0]),
        triangle_count=int(triangle_snapshot.shape[0]),
        bounding_box_min=tuple(float(value) for value in vertex_snapshot.min(axis=0)),
        bounding_box_max=tuple(float(value) for value in vertex_snapshot.max(axis=0)),
        connected_components=connected_components,
        boundary_edge_count=boundary_edge_count,
        boundary_loop_count=boundary_loop_count,
        non_manifold_edge_count=non_manifold_edge_count,
        inconsistent_orientation_edge_count=inconsistent_orientation_edge_count,
        duplicate_triangle_count=duplicate_triangle_count,
        degenerate_triangle_count=degenerate_triangle_count,
        unreferenced_vertex_count=unreferenced_vertex_count,
        signed_volume=signed_volume,
        self_intersection_count=self_intersection_count,
        issues=tuple(sorted(issues)),
    )
