from __future__ import annotations

import numpy as np
import pytest

from vibecad.organic.contracts import MeshProfile
from vibecad.organic.validation import (
    MeshValidationError,
    MeshValidationErrorCode,
    MeshValidationStatus,
    SelfIntersectionEvidence,
    mesh_content_digest,
    validate_mesh,
)


def _tetrahedron() -> tuple[np.ndarray, np.ndarray]:
    vertices = np.array(
        (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
        ),
        dtype=np.float64,
    )
    triangles = np.array(
        (
            (0, 2, 1),
            (0, 1, 3),
            (0, 3, 2),
            (1, 2, 3),
        ),
        dtype=np.int64,
    )
    return vertices, triangles


def _evidence(vertices: np.ndarray, triangles: np.ndarray) -> SelfIntersectionEvidence:
    return SelfIntersectionEvidence(
        mesh_sha256=mesh_content_digest(vertices, triangles),
        intersection_count=0,
        engine="fixture-exact",
        engine_version="1.0",
    )


def test_closed_tetrahedron_passes_with_bound_self_intersection_evidence() -> None:
    vertices, triangles = _tetrahedron()
    report = validate_mesh(
        vertices,
        triangles,
        profile=MeshProfile.CLOSED_SURFACE_V1,
        expected_boundary_loops=0,
        self_intersection_evidence=_evidence(vertices, triangles),
    )

    assert report.status is MeshValidationStatus.PASS
    assert report.issues == ()
    assert report.connected_components == 1
    assert report.boundary_edge_count == 0
    assert report.boundary_loop_count == 0
    assert report.non_manifold_edge_count == 0
    assert report.inconsistent_orientation_edge_count == 0
    assert report.signed_volume == pytest.approx(1 / 6)


def test_missing_self_intersection_evidence_is_indeterminate_not_pass() -> None:
    vertices, triangles = _tetrahedron()
    report = validate_mesh(
        vertices,
        triangles,
        profile=MeshProfile.CLOSED_SURFACE_V1,
        expected_boundary_loops=0,
    )
    assert report.status is MeshValidationStatus.INDETERMINATE
    assert report.issues == ("self_intersection_not_checked",)


def test_open_square_passes_only_with_one_declared_boundary_loop() -> None:
    vertices = np.array(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)))
    triangles = np.array(((0, 1, 2), (0, 2, 3)), dtype=np.int64)
    report = validate_mesh(
        vertices,
        triangles,
        profile=MeshProfile.OPEN_SURFACE_V1,
        expected_boundary_loops=1,
        self_intersection_evidence=_evidence(vertices, triangles),
    )
    assert report.status is MeshValidationStatus.PASS
    assert report.boundary_edge_count == 4
    assert report.boundary_loop_count == 1


def test_topology_defects_are_reported_deterministically() -> None:
    vertices, triangles = _tetrahedron()
    vertices = np.concatenate((vertices, np.array(((9.0, 9.0, 9.0),))), axis=0)
    triangles = np.concatenate(
        (
            triangles,
            triangles[:1],
            np.array(((0, 0, 1),), dtype=np.int64),
        ),
        axis=0,
    )
    report = validate_mesh(
        vertices,
        triangles,
        profile=MeshProfile.CLOSED_SURFACE_V1,
        expected_boundary_loops=0,
        self_intersection_evidence=_evidence(vertices, triangles),
    )
    assert report.status is MeshValidationStatus.FAIL
    assert report.duplicate_triangle_count == 1
    assert report.degenerate_triangle_count == 1
    assert report.unreferenced_vertex_count == 1
    assert "duplicate_triangles" in report.issues
    assert "degenerate_triangles" in report.issues
    assert "unreferenced_vertices" in report.issues


def test_mismatched_evidence_and_invalid_indices_fail_closed() -> None:
    vertices, triangles = _tetrahedron()
    wrong_evidence = SelfIntersectionEvidence(
        mesh_sha256="0" * 64,
        intersection_count=0,
        engine="fixture-exact",
        engine_version="1.0",
    )
    with pytest.raises(MeshValidationError) as mismatch:
        validate_mesh(
            vertices,
            triangles,
            profile=MeshProfile.CLOSED_SURFACE_V1,
            expected_boundary_loops=0,
            self_intersection_evidence=wrong_evidence,
        )
    assert mismatch.value.code is MeshValidationErrorCode.EVIDENCE_MISMATCH

    invalid = triangles.copy()
    invalid[0, 0] = len(vertices)
    with pytest.raises(MeshValidationError) as index:
        mesh_content_digest(vertices, invalid)
    assert index.value.code is MeshValidationErrorCode.INDEX_OUT_OF_RANGE


@pytest.mark.parametrize(
    "vertices",
    (
        [[0.0, 0.0, 0.0]],
        np.array(((0.0, 0.0),)),
        np.array(((float("nan"), 0.0, 0.0),)),
    ),
)
def test_invalid_vertex_inputs_are_rejected(vertices: object) -> None:
    triangles = np.array(((0, 0, 0),), dtype=np.int64)
    with pytest.raises(MeshValidationError):
        mesh_content_digest(vertices, triangles)
