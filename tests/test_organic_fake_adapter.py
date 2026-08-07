from __future__ import annotations

import numpy as np
import pytest

from vibecad.organic.contracts import (
    DerivedArtifactKind,
    MeshJobRequest,
    MeshMediaType,
    MeshOperation,
    MeshOperationKind,
    MeshOperationPlan,
    MeshProfile,
    SealedMeshSource,
)
from vibecad.organic.fake_adapter import (
    DeterministicFakeOrganicAdapter,
    FakeArtifactPayload,
    FakeOrganicAdapterError,
    FakeOrganicAdapterErrorCode,
    FakeOrganicFixture,
)
from vibecad.organic.plan import validate_mesh_operation_plan
from vibecad.organic.validation import (
    SelfIntersectionEvidence,
    mesh_content_digest,
    validate_mesh,
)


def _request(source_sha256: str = "a" * 64) -> MeshJobRequest:
    operations = tuple(
        MeshOperation(
            operation_id="mesh_op_" + f"{index:x}" * 32,
            kind=kind,
        )
        for index, kind in enumerate(
            (
                MeshOperationKind.REMOVE_DUPLICATE_VERTICES,
                MeshOperationKind.REMOVE_DUPLICATE_TRIANGLES,
                MeshOperationKind.REMOVE_DEGENERATE_TRIANGLES,
                MeshOperationKind.REMOVE_UNREFERENCED_VERTICES,
                MeshOperationKind.ORIENT_NORMALS,
            ),
            start=1,
        )
    )
    return MeshJobRequest(
        mesh_job_id="mesh_job_" + "1" * 32,
        generation=1,
        source=SealedMeshSource(
            source_id="mesh_input_" + "2" * 32,
            sha256=source_sha256,
            media_type=MeshMediaType.PLY,
            byte_count=100,
            vertex_count=4,
            triangle_count=4,
            millimeters_per_unit=1,
        ),
        plan=MeshOperationPlan(
            profile=MeshProfile.CLOSED_SURFACE_V1,
            expected_boundary_loops=0,
            operations=operations,
        ),
    )


def _validation():
    vertices = np.array(((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    triangles = np.array(((0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)), dtype=np.int64)
    digest = mesh_content_digest(vertices, triangles)
    return validate_mesh(
        vertices,
        triangles,
        profile=MeshProfile.CLOSED_SURFACE_V1,
        expected_boundary_loops=0,
        self_intersection_evidence=SelfIntersectionEvidence(
            mesh_sha256=digest,
            intersection_count=0,
            engine="fixture-exact",
            engine_version="1.0",
        ),
    )


def _payloads() -> tuple[FakeArtifactPayload, ...]:
    values = (
        (DerivedArtifactKind.CONTROL_CAGE, "application/vnd.vibecad.mesh+ply", b"ply\nfixture"),
        (DerivedArtifactKind.EDITABLE_BLEND, "application/x-blender", b"BLENDER-fixture"),
        (DerivedArtifactKind.EVALUATED_GLB, "model/gltf-binary", b"glTF-fixture"),
        (DerivedArtifactKind.PREVIEW_PNG, "image/png", b"\x89PNG-fixture"),
        (DerivedArtifactKind.VALIDATION_REPORT, "application/json", b'{"status":"pass"}'),
    )
    return tuple(
        FakeArtifactPayload(kind=kind, media_type=media_type, content=content)
        for kind, media_type, content in values
    )


def _fixture(request: MeshJobRequest) -> FakeOrganicFixture:
    summary = validate_mesh_operation_plan(request.source, request.plan)
    return FakeOrganicFixture(
        source_sha256=request.source.sha256,
        plan_sha256=summary.plan_sha256,
        validation=_validation(),
        artifacts=_payloads(),
    )


def test_fake_adapter_returns_deterministic_authority_free_artifact_set() -> None:
    request = _request()
    fixture = _fixture(request)
    adapter = DeterministicFakeOrganicAdapter({fixture.plan_sha256: fixture})

    first = adapter.execute(request)
    second = adapter.execute(request)

    assert first == second
    assert first.authority == "derived_artifact_only"
    assert first.mesh_job_id == request.mesh_job_id
    assert first.source_sha256 == request.source.sha256
    assert len(first.artifacts) == 5
    assert adapter.execution_count == 2
    assert not hasattr(first, "revision_id")
    assert not hasattr(first, "head")


def test_fake_adapter_rejects_missing_fixture_and_source_mismatch() -> None:
    request = _request()
    with pytest.raises(FakeOrganicAdapterError) as missing:
        DeterministicFakeOrganicAdapter({}).execute(request)
    assert missing.value.code is FakeOrganicAdapterErrorCode.MISSING_FIXTURE

    fixture = _fixture(request)
    changed_request = _request("b" * 64)
    adapter = DeterministicFakeOrganicAdapter({fixture.plan_sha256: fixture})
    with pytest.raises(FakeOrganicAdapterError) as mismatch:
        adapter.execute(changed_request)
    assert mismatch.value.code is FakeOrganicAdapterErrorCode.SOURCE_MISMATCH


def test_fixture_requires_exact_five_artifact_kinds() -> None:
    request = _request()
    summary = validate_mesh_operation_plan(request.source, request.plan)
    with pytest.raises(FakeOrganicAdapterError) as invalid:
        FakeOrganicFixture(
            source_sha256=request.source.sha256,
            plan_sha256=summary.plan_sha256,
            validation=_validation(),
            artifacts=_payloads()[:-1],
        )
    assert invalid.value.code is FakeOrganicAdapterErrorCode.INVALID_FIXTURE


def test_adapter_rejects_nonfixture_mapping_values_without_reflecting_them() -> None:
    with pytest.raises(FakeOrganicAdapterError) as invalid:
        DeterministicFakeOrganicAdapter({"0" * 64: object()})
    assert invalid.value.code is FakeOrganicAdapterErrorCode.INVALID_FIXTURE
