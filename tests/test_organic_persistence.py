from __future__ import annotations

import hashlib
import json

import pytest

from vibecad.organic.contracts import (
    DerivedArtifact,
    DerivedArtifactKind,
    DerivedArtifactSet,
    MeshJobRequest,
    MeshMediaType,
    MeshOperation,
    MeshOperationKind,
    MeshOperationPlan,
    MeshProfile,
    SealedMeshSource,
)
from vibecad.organic.persistence import (
    MAX_ORGANIC_MANIFEST_BYTES,
    OrganicPersistenceError,
    build_organic_manifest,
    decode_organic_manifest,
    encode_organic_manifest,
)
from vibecad.organic.plan import mesh_operation_plan_digest


def _request(*, generation: int = 7) -> MeshJobRequest:
    kinds = (
        MeshOperationKind.REMOVE_DUPLICATE_VERTICES,
        MeshOperationKind.REMOVE_DUPLICATE_TRIANGLES,
        MeshOperationKind.REMOVE_DEGENERATE_TRIANGLES,
        MeshOperationKind.REMOVE_UNREFERENCED_VERTICES,
        MeshOperationKind.ORIENT_NORMALS,
    )
    operations = tuple(
        MeshOperation(
            operation_id="mesh_op_" + f"{index:x}" * 32,
            kind=kind,
        )
        for index, kind in enumerate(kinds, start=1)
    )
    return MeshJobRequest(
        mesh_job_id="mesh_job_" + "1" * 32,
        generation=generation,
        source=SealedMeshSource(
            source_id="mesh_input_" + "2" * 32,
            sha256="a" * 64,
            media_type=MeshMediaType.PLY,
            byte_count=10,
            vertex_count=4,
            triangle_count=4,
            millimeters_per_unit=1,
        ),
        plan=MeshOperationPlan(
            profile=MeshProfile.CLOSED_SURFACE_V1,
            operations=operations,
            expected_boundary_loops=0,
        ),
    )


def _result(request: MeshJobRequest) -> DerivedArtifactSet:
    media = {
        DerivedArtifactKind.CONTROL_CAGE: "application/vnd.vibecad.mesh+ply",
        DerivedArtifactKind.EDITABLE_BLEND: "application/x-blender",
        DerivedArtifactKind.EVALUATED_GLB: "model/gltf-binary",
        DerivedArtifactKind.PREVIEW_PNG: "image/png",
        DerivedArtifactKind.VALIDATION_REPORT: "application/json",
    }
    artifacts = tuple(
        DerivedArtifact(
            artifact_id="derived_artifact_" + f"{index:x}" * 32,
            kind=kind,
            sha256=hashlib.sha256(kind.value.encode("ascii")).hexdigest(),
            byte_count=index,
            media_type=media[kind],
        )
        for index, kind in enumerate(DerivedArtifactKind, start=1)
    )
    return DerivedArtifactSet(
        mesh_job_id=request.mesh_job_id,
        generation=request.generation,
        source_sha256=request.source.sha256,
        plan_sha256=mesh_operation_plan_digest(request.plan),
        artifacts=artifacts,
    )


def test_manifest_is_canonical_deterministic_and_authority_free() -> None:
    request = _request()
    result = _result(request)
    first = encode_organic_manifest(request, result)
    second = encode_organic_manifest(request, result)
    restored = decode_organic_manifest(first)

    assert first == second
    assert restored.request == request
    assert restored.result == result
    assert restored == build_organic_manifest(request, result)
    assert restored.manifest_sha256 == hashlib.sha256(first).hexdigest()
    assert b'"authority":"derived_artifact_only"' in first
    assert b'"correlation_semantics":"mesh_job_not_cad_task"' in first
    assert b"task_id" not in first and b"revision" not in first and b"head" not in first


def test_manifest_rejects_noncanonical_duplicate_and_tampered_checksum() -> None:
    request = _request()
    raw = encode_organic_manifest(request, _result(request))
    parsed = json.loads(raw)

    with pytest.raises(OrganicPersistenceError):
        decode_organic_manifest(json.dumps(parsed, indent=2).encode("utf-8"))

    duplicate = raw[:-1] + b',"schema_version":1}'
    with pytest.raises(OrganicPersistenceError):
        decode_organic_manifest(duplicate)

    parsed["body_sha256"] = "0" * 64
    tampered = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with pytest.raises(OrganicPersistenceError):
        decode_organic_manifest(tampered)


def test_manifest_rejects_cross_generation_and_plan_binding() -> None:
    request = _request()
    result = _result(request)
    changed_generation = DerivedArtifactSet(
        mesh_job_id=result.mesh_job_id,
        generation=result.generation + 1,
        source_sha256=result.source_sha256,
        plan_sha256=result.plan_sha256,
        artifacts=result.artifacts,
    )
    with pytest.raises(OrganicPersistenceError):
        encode_organic_manifest(request, changed_generation)

    changed_plan = DerivedArtifactSet(
        mesh_job_id=result.mesh_job_id,
        generation=result.generation,
        source_sha256=result.source_sha256,
        plan_sha256="f" * 64,
        artifacts=result.artifacts,
    )
    with pytest.raises(OrganicPersistenceError):
        build_organic_manifest(request, changed_plan)


def test_manifest_decode_rejects_authority_or_payload_binding_changes() -> None:
    request = _request()
    value = json.loads(encode_organic_manifest(request, _result(request)))
    value["body"]["authority"] = "cad_revision"
    value["body_sha256"] = hashlib.sha256(
        b"vibecad-organic-generation-manifest-v1\0"
        + json.dumps(
            value["body"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with pytest.raises(OrganicPersistenceError):
        decode_organic_manifest(raw)


@pytest.mark.parametrize(
    "raw",
    (
        b"",
        b"[]",
        b'{"x":NaN}',
        b'{"x":"\\ud800"}',
        b"{" + b"x" * MAX_ORGANIC_MANIFEST_BYTES + b"}",
    ),
)
def test_manifest_parser_fails_closed_on_invalid_or_oversized_bytes(raw: bytes) -> None:
    with pytest.raises(OrganicPersistenceError):
        decode_organic_manifest(raw)
