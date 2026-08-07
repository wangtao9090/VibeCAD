from __future__ import annotations

import pytest

from vibecad.organic.contracts import (
    MAX_SOURCE_BYTES,
    DerivedArtifact,
    DerivedArtifactKind,
    DerivedArtifactSet,
    MeshJobRequest,
    MeshMediaType,
    MeshOperation,
    MeshOperationKind,
    MeshOperationPlan,
    MeshProfile,
    MirrorAxis,
    OrganicContractError,
    OrganicContractErrorCode,
    SealedMeshSource,
)


def _source(**overrides) -> SealedMeshSource:
    values = {
        "source_id": "mesh_input_" + "1" * 32,
        "sha256": "a" * 64,
        "media_type": MeshMediaType.PLY,
        "byte_count": 1024,
        "vertex_count": 8,
        "triangle_count": 12,
        "millimeters_per_unit": 1.0,
    }
    values.update(overrides)
    return SealedMeshSource(**values)


def _operation(kind: MeshOperationKind, suffix: str, **parameters) -> MeshOperation:
    return MeshOperation(
        operation_id="mesh_op_" + suffix * 32,
        kind=kind,
        **parameters,
    )


def _plan() -> MeshOperationPlan:
    return MeshOperationPlan(
        profile=MeshProfile.CLOSED_SURFACE_V1,
        expected_boundary_loops=0,
        operations=(
            _operation(MeshOperationKind.REMOVE_DUPLICATE_VERTICES, "1"),
            _operation(MeshOperationKind.REMOVE_DUPLICATE_TRIANGLES, "2"),
            _operation(MeshOperationKind.REMOVE_DEGENERATE_TRIANGLES, "3"),
            _operation(MeshOperationKind.REMOVE_UNREFERENCED_VERTICES, "4"),
            _operation(MeshOperationKind.ORIENT_NORMALS, "5"),
        ),
    )


def _artifact(kind: DerivedArtifactKind, suffix: str) -> DerivedArtifact:
    media_types = {
        DerivedArtifactKind.CONTROL_CAGE: "application/vnd.vibecad.mesh+ply",
        DerivedArtifactKind.EDITABLE_BLEND: "application/x-blender",
        DerivedArtifactKind.EVALUATED_GLB: "model/gltf-binary",
        DerivedArtifactKind.PREVIEW_PNG: "image/png",
        DerivedArtifactKind.VALIDATION_REPORT: "application/json",
    }
    return DerivedArtifact(
        artifact_id="derived_artifact_" + suffix * 32,
        kind=kind,
        sha256=suffix * 64,
        byte_count=10,
        media_type=media_types[kind],
    )


def test_source_and_job_are_strict_bounded_and_authority_free() -> None:
    source = _source()
    request = MeshJobRequest(
        mesh_job_id="mesh_job_" + "f" * 32,
        generation=1,
        source=source,
        plan=_plan(),
    )

    assert source.millimeters_per_unit == 1.0
    assert request.correlation_semantics == "mesh_job_not_cad_task"
    assert not hasattr(request, "task_id")
    assert not hasattr(request, "revision_id")
    assert not hasattr(request, "head")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("byte_count", MAX_SOURCE_BYTES + 1),
        ("vertex_count", True),
        ("triangle_count", 0),
        ("millimeters_per_unit", float("nan")),
    ),
)
def test_source_rejects_invalid_values_and_budgets(field: str, value: object) -> None:
    with pytest.raises(OrganicContractError):
        _source(**{field: value})


@pytest.mark.parametrize(
    ("kind", "parameters"),
    (
        (MeshOperationKind.MERGE_CLOSE_VERTICES, {"distance_mm": 0.01}),
        (MeshOperationKind.TAUBIN_SMOOTH, {"iterations": 5}),
        (MeshOperationKind.QUADRIC_DECIMATE, {"target_triangles": 100}),
        (MeshOperationKind.VERTEX_CLUSTER, {"target_triangles": 100}),
        (MeshOperationKind.MIRROR, {"axis": MirrorAxis.X}),
        (
            MeshOperationKind.VOXEL_REMESH,
            {"distance_mm": 0.2, "target_triangles": 1000},
        ),
        (MeshOperationKind.SUBDIVISION_SURFACE, {"level": 2}),
    ),
)
def test_operation_kinds_accept_only_their_closed_parameter_shape(
    kind: MeshOperationKind,
    parameters: dict[str, object],
) -> None:
    operation = _operation(kind, "a", **parameters)
    assert operation.kind is kind

    with pytest.raises(OrganicContractError) as error:
        _operation(kind, "b")
    assert error.value.code is OrganicContractErrorCode.INVALID_INPUT


def test_plan_requires_profile_specific_boundary_declaration() -> None:
    with pytest.raises(OrganicContractError):
        MeshOperationPlan(
            profile=MeshProfile.CLOSED_SURFACE_V1,
            expected_boundary_loops=1,
            operations=_plan().operations,
        )
    open_plan = MeshOperationPlan(
        profile=MeshProfile.OPEN_SURFACE_V1,
        expected_boundary_loops=2,
        operations=_plan().operations,
    )
    assert open_plan.expected_boundary_loops == 2


def test_derived_artifact_set_requires_exact_five_outputs_and_sorts_them() -> None:
    artifacts = tuple(
        _artifact(kind, f"{index:x}")
        for index, kind in enumerate(reversed(tuple(DerivedArtifactKind)), start=1)
    )
    result = DerivedArtifactSet(
        mesh_job_id="mesh_job_" + "f" * 32,
        generation=2,
        source_sha256="a" * 64,
        plan_sha256="b" * 64,
        artifacts=artifacts,
    )
    assert result.authority == "derived_artifact_only"
    assert tuple(artifact.kind.value for artifact in result.artifacts) == tuple(
        sorted(kind.value for kind in DerivedArtifactKind)
    )

    with pytest.raises(OrganicContractError):
        DerivedArtifactSet(
            mesh_job_id="mesh_job_" + "f" * 32,
            generation=2,
            source_sha256="a" * 64,
            plan_sha256="b" * 64,
            artifacts=artifacts[:-1],
        )


def test_artifact_media_type_is_fixed_by_kind() -> None:
    with pytest.raises(OrganicContractError):
        DerivedArtifact(
            artifact_id="derived_artifact_" + "1" * 32,
            kind=DerivedArtifactKind.PREVIEW_PNG,
            sha256="a" * 64,
            byte_count=1,
            media_type="application/octet-stream",
        )
