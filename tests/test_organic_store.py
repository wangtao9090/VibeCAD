from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import vibecad.organic.store as store_module
from vibecad.interaction.storage import SafeRoot
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
from vibecad.organic.persistence import encode_organic_manifest
from vibecad.organic.plan import mesh_operation_plan_digest
from vibecad.organic.store import (
    OrganicArtifactStore,
    OrganicArtifactStoreError,
    OrganicArtifactStoreErrorCode,
    OrganicPayloadSource,
)

_SOURCE = b"ply\nsource\n"
_ARTIFACT_BYTES = {kind: f"{kind.value}\n".encode("ascii") for kind in DerivedArtifactKind}
_MEDIA_TYPES = {
    DerivedArtifactKind.CONTROL_CAGE: "application/vnd.vibecad.mesh+ply",
    DerivedArtifactKind.EDITABLE_BLEND: "application/x-blender",
    DerivedArtifactKind.EVALUATED_GLB: "model/gltf-binary",
    DerivedArtifactKind.PREVIEW_PNG: "image/png",
    DerivedArtifactKind.VALIDATION_REPORT: "application/json",
}


def _request(*, generation: int = 1, job_digit: str = "1") -> MeshJobRequest:
    kinds = (
        MeshOperationKind.REMOVE_DUPLICATE_VERTICES,
        MeshOperationKind.REMOVE_DUPLICATE_TRIANGLES,
        MeshOperationKind.REMOVE_DEGENERATE_TRIANGLES,
        MeshOperationKind.REMOVE_UNREFERENCED_VERTICES,
        MeshOperationKind.ORIENT_NORMALS,
    )
    return MeshJobRequest(
        mesh_job_id="mesh_job_" + job_digit * 32,
        generation=generation,
        source=SealedMeshSource(
            source_id="mesh_input_" + job_digit * 32,
            sha256=hashlib.sha256(_SOURCE).hexdigest(),
            media_type=MeshMediaType.PLY,
            byte_count=len(_SOURCE),
            vertex_count=4,
            triangle_count=4,
            millimeters_per_unit=1,
        ),
        plan=MeshOperationPlan(
            profile=MeshProfile.CLOSED_SURFACE_V1,
            operations=tuple(
                MeshOperation(
                    operation_id="mesh_op_" + f"{index:x}" * 32,
                    kind=kind,
                )
                for index, kind in enumerate(kinds, start=1)
            ),
            expected_boundary_loops=0,
        ),
    )


def _result(request: MeshJobRequest, *, variant: bytes = b"") -> DerivedArtifactSet:
    artifacts = tuple(
        DerivedArtifact(
            artifact_id="derived_artifact_" + f"{index:x}" * 32,
            kind=kind,
            sha256=hashlib.sha256(_ARTIFACT_BYTES[kind] + variant).hexdigest(),
            byte_count=len(_ARTIFACT_BYTES[kind] + variant),
            media_type=_MEDIA_TYPES[kind],
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


def _store(tmp_path: Path) -> tuple[OrganicArtifactStore, Path]:
    root = tmp_path / "organic-store"
    root.mkdir(mode=0o700)
    return OrganicArtifactStore(SafeRoot(root)), root


@contextmanager
def _payloads(
    tmp_path: Path,
    request: MeshJobRequest,
    result: DerivedArtifactSet,
    *,
    variant: bytes = b"",
) -> Iterator[tuple[OrganicPayloadSource, tuple[OrganicPayloadSource, ...]]]:
    sequence = sum(1 for _ in tmp_path.glob("payloads-*"))
    payload_root = tmp_path / f"payloads-{request.generation}-{variant.hex()}-{sequence}"
    payload_root.mkdir(mode=0o700)
    entries = [(request.source.source_id, _SOURCE)] + [
        (artifact.artifact_id, _ARTIFACT_BYTES[artifact.kind] + variant)
        for artifact in result.artifacts
    ]
    opened: list[OrganicPayloadSource] = []
    try:
        for index, (payload_id, raw) in enumerate(entries):
            path = payload_root / str(index)
            path.write_bytes(raw)
            path.chmod(0o600)
            opened.append(
                OrganicPayloadSource(payload_id=payload_id, fd=os.open(path, os.O_RDONLY))
            )
        yield opened[0], tuple(opened[1:])
    finally:
        for item in opened:
            os.close(item.fd)


def _publish(
    store: OrganicArtifactStore,
    tmp_path: Path,
    request: MeshJobRequest,
    *,
    variant: bytes = b"",
):
    result = _result(request, variant=variant)
    with _payloads(tmp_path, request, result, variant=variant) as (source, artifacts):
        return store.publish(request, source, result, artifacts)


def _assert_code(code: OrganicArtifactStoreErrorCode, operation) -> None:
    with pytest.raises(OrganicArtifactStoreError) as failure:
        operation()
    assert failure.value.code is code


def test_publish_load_read_and_idempotent_replay(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    request = _request()
    first = _publish(store, tmp_path, request)
    second = _publish(store, tmp_path, request)

    assert first == second == store.load_exact(request.mesh_job_id, request.generation)
    preview = next(
        item for item in first.result.artifacts if item.kind is DerivedArtifactKind.PREVIEW_PNG
    )
    assert (
        store.read_payload_exact(
            request.mesh_job_id,
            request.generation,
            preview.artifact_id,
            preview.sha256,
        )
        == _ARTIFACT_BYTES[DerivedArtifactKind.PREVIEW_PNG]
    )


def test_same_generation_changed_manifest_conflicts(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    request = _request()
    _publish(store, tmp_path, request)

    _assert_code(
        OrganicArtifactStoreErrorCode.CONFLICT,
        lambda: _publish(store, tmp_path, request, variant=b"changed"),
    )


def test_recovery_publishes_complete_stage_and_removes_partial_stage(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    request = _request()
    _publish(store, tmp_path, request)
    final = root / f"mesh_generation_{'1' * 32}_{request.generation:016x}"
    complete_stage = root / f".stage_{'1' * 32}_{request.generation:016x}_{'a' * 32}"
    final.rename(complete_stage)
    partial_stage = root / f".stage_{'2' * 32}_{2:016x}_{'b' * 32}"
    partial_stage.mkdir(mode=0o700)
    partial = partial_stage / "source.ply"
    partial.write_bytes(_SOURCE)
    partial.chmod(0o600)

    summary = store.recover_pending()

    assert summary.published_stages == 1
    assert summary.removed_partial_stages == 1
    assert summary.completed_deletions == 0
    assert store.load_exact(request.mesh_job_id, request.generation).request == request


def test_recovery_removes_unpublished_manifest_and_tombstone_temporaries(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path)
    request = _request()
    _publish(store, tmp_path, request)
    stage = root / f".stage_{'2' * 32}_{2:016x}_{'a' * 32}"
    stage.mkdir(mode=0o700)
    manifest_temporary = stage / "manifest.tmp"
    manifest_temporary.write_bytes(b"partial")
    manifest_temporary.chmod(0o600)
    tombstone_temporary = root / f".deleted_{'1' * 32}_{1:016x}_{'b' * 32}.tmp"
    tombstone_temporary.write_bytes(b"partial")
    tombstone_temporary.chmod(0o600)

    summary = store.recover_pending()

    assert summary.removed_partial_stages == 1
    assert not stage.exists()
    assert not tombstone_temporary.exists()
    assert store.load_exact(request.mesh_job_id, request.generation).request == request


def test_publish_is_replayable_after_post_rename_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    request = _request()
    original_fsync = store_module._fsync
    original_rename = store_module._rename_directory_noreplace
    fail_next_fsync = False

    def fail_after_publish(fd: int) -> None:
        nonlocal fail_next_fsync
        if fail_next_fsync:
            fail_next_fsync = False
            raise OrganicArtifactStoreError(OrganicArtifactStoreErrorCode.DURABILITY_UNCERTAIN)
        original_fsync(fd)

    def observe_publish_rename(parent_fd: int, source: str, destination: str) -> None:
        nonlocal fail_next_fsync
        original_rename(parent_fd, source, destination)
        if source.startswith(".stage_") and destination.startswith("mesh_generation_"):
            fail_next_fsync = True

    monkeypatch.setattr(store_module, "_fsync", fail_after_publish)
    monkeypatch.setattr(store_module, "_rename_directory_noreplace", observe_publish_rename)
    _assert_code(
        OrganicArtifactStoreErrorCode.DURABILITY_UNCERTAIN,
        lambda: _publish(store, tmp_path, request),
    )
    monkeypatch.setattr(store_module, "_fsync", original_fsync)
    monkeypatch.setattr(store_module, "_rename_directory_noreplace", original_rename)

    assert _publish(store, tmp_path, request).request == request


def test_recovery_rejects_stage_name_manifest_mismatch(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    request = _request()
    _publish(store, tmp_path, request)
    final = root / f"mesh_generation_{'1' * 32}_{request.generation:016x}"
    final.rename(root / f".stage_{'1' * 32}_{2:016x}_{'a' * 32}")

    _assert_code(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE, store.recover_pending)


def test_delete_is_exact_durable_and_prevents_resurrection(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)

    store.delete_exact(request.mesh_job_id, request.generation, manifest.manifest_sha256)
    store.delete_exact(request.mesh_job_id, request.generation, manifest.manifest_sha256)
    _assert_code(
        OrganicArtifactStoreErrorCode.NOT_FOUND,
        lambda: store.load_exact(request.mesh_job_id, request.generation),
    )
    _assert_code(
        OrganicArtifactStoreErrorCode.CONFLICT,
        lambda: _publish(store, tmp_path, request),
    )


def test_delete_retry_recovers_after_tombstone_before_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)
    original_rename = store_module._rename_directory_noreplace

    def fail_rename(parent_fd: int, source: str, destination: str) -> None:
        if source.startswith("mesh_generation_"):
            raise OSError
        original_rename(parent_fd, source, destination)

    monkeypatch.setattr(store_module, "_rename_directory_noreplace", fail_rename)
    _assert_code(
        OrganicArtifactStoreErrorCode.DURABILITY_UNCERTAIN,
        lambda: store.delete_exact(
            request.mesh_job_id, request.generation, manifest.manifest_sha256
        ),
    )
    monkeypatch.setattr(store_module, "_rename_directory_noreplace", original_rename)

    store.delete_exact(request.mesh_job_id, request.generation, manifest.manifest_sha256)
    _assert_code(
        OrganicArtifactStoreErrorCode.NOT_FOUND,
        lambda: store.load_exact(request.mesh_job_id, request.generation),
    )


def test_recovery_allows_bounded_tombstone_overhead_at_full_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)
    used = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    monkeypatch.setattr(store_module, "MAX_ORGANIC_STORE_BYTES", used)
    original_rename = store_module._rename_directory_noreplace

    def fail_generation_rename(parent_fd: int, source: str, destination: str) -> None:
        if source.startswith("mesh_generation_"):
            raise OSError
        original_rename(parent_fd, source, destination)

    monkeypatch.setattr(store_module, "_rename_directory_noreplace", fail_generation_rename)
    _assert_code(
        OrganicArtifactStoreErrorCode.DURABILITY_UNCERTAIN,
        lambda: store.delete_exact(
            request.mesh_job_id, request.generation, manifest.manifest_sha256
        ),
    )
    monkeypatch.setattr(store_module, "_rename_directory_noreplace", original_rename)

    assert store.recover_pending().completed_deletions == 1


def test_delete_directory_swap_before_remove_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)
    original_remove = store_module._remove_directory

    def swap_before_remove(safe_root, root_fd, name, *, allowed, expected_identity=None):
        if name.startswith(".delete_"):
            deleting = root / name
            deleting.rename(root / "held-generation")
            deleting.mkdir(mode=0o700)
        return original_remove(
            safe_root,
            root_fd,
            name,
            allowed=allowed,
            expected_identity=expected_identity,
        )

    monkeypatch.setattr(store_module, "_remove_directory", swap_before_remove)
    _assert_code(
        OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE,
        lambda: store.delete_exact(
            request.mesh_job_id, request.generation, manifest.manifest_sha256
        ),
    )


@pytest.mark.parametrize("after_directory_rename", (False, True))
def test_recovery_completes_durable_delete_protocol(
    tmp_path: Path, *, after_directory_rename: bool
) -> None:
    store, root = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)
    tombstone = root / f".deleted_{'1' * 32}_{request.generation:016x}.json"
    tombstone.write_bytes(store_module._tombstone_bytes(manifest))
    tombstone.chmod(0o600)
    final = root / f"mesh_generation_{'1' * 32}_{request.generation:016x}"
    if after_directory_rename:
        final.rename(root / f".delete_{'1' * 32}_{request.generation:016x}")

    summary = store.recover_pending()

    assert summary.completed_deletions == 1
    assert not final.exists()


def test_recovery_finishes_partially_unlinked_delete_directory(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)
    tombstone = root / f".deleted_{'1' * 32}_{request.generation:016x}.json"
    tombstone.write_bytes(store_module._tombstone_bytes(manifest))
    tombstone.chmod(0o600)
    final = root / f"mesh_generation_{'1' * 32}_{request.generation:016x}"
    deleting = root / f".delete_{'1' * 32}_{request.generation:016x}"
    final.rename(deleting)
    (deleting / "preview.png").unlink()

    summary = store.recover_pending()

    assert summary.completed_deletions == 1
    assert not deleting.exists()


def test_recovery_rejects_tombstone_name_body_mismatch(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)
    store.delete_exact(request.mesh_job_id, request.generation, manifest.manifest_sha256)
    tombstone = root / f".deleted_{'1' * 32}_{request.generation:016x}.json"
    tombstone.rename(root / f".deleted_{'2' * 32}_{request.generation:016x}.json")

    _assert_code(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE, store.recover_pending)


def test_deleted_generation_counts_toward_retention_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = _store(tmp_path)
    request = _request()
    manifest = _publish(store, tmp_path, request)
    store.delete_exact(request.mesh_job_id, request.generation, manifest.manifest_sha256)
    monkeypatch.setattr(store_module, "MAX_ORGANIC_GENERATIONS_PER_JOB", 1)

    _assert_code(
        OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED,
        lambda: _publish(store, tmp_path, _request(generation=2)),
    )


def test_physical_store_budget_is_enforced_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path)
    request = _request()
    result = _result(request)
    required = (
        request.source.byte_count
        + sum(item.byte_count for item in result.artifacts)
        + len(encode_organic_manifest(request, result))
    )
    monkeypatch.setattr(store_module, "MAX_ORGANIC_STORE_BYTES", required - 1)

    _assert_code(
        OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED,
        lambda: _publish(store, tmp_path, request),
    )
    assert set(path.name for path in root.iterdir()) == {"organic-store.lock"}


def test_recovery_rejects_excess_temporary_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path)
    monkeypatch.setattr(store_module, "MAX_ORGANIC_TEMPORARIES", 1)
    for token in ("a", "b"):
        stage = root / f".stage_{'1' * 32}_{1:016x}_{token * 32}"
        stage.mkdir(mode=0o700)

    _assert_code(OrganicArtifactStoreErrorCode.BUDGET_EXCEEDED, store.recover_pending)


def test_payload_tampering_and_unsafe_source_mode_fail_closed(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    request = _request()
    _publish(store, tmp_path, request)
    final = root / f"mesh_generation_{'1' * 32}_{request.generation:016x}"
    stored_source = final / "source.ply"
    stored_source.write_bytes(b"x" * len(_SOURCE))
    stored_source.chmod(0o600)
    _assert_code(
        OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE,
        lambda: store.load_exact(request.mesh_job_id, request.generation),
    )

    other_tmp = tmp_path / "other"
    other_tmp.mkdir()
    other_store, _ = _store(other_tmp)
    other_request = _request(job_digit="2")
    other_result = _result(other_request)
    with _payloads(tmp_path, other_request, other_result) as (source, artifacts):
        os.fchmod(source.fd, 0o644)
        _assert_code(
            OrganicArtifactStoreErrorCode.INVALID_INPUT,
            lambda: other_store.publish(other_request, source, other_result, artifacts),
        )


def test_directory_entry_swap_during_load_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, root = _store(tmp_path)
    request = _request()
    _publish(store, tmp_path, request)
    final = root / f"mesh_generation_{'1' * 32}_{request.generation:016x}"
    escaped = root / "escaped-generation"
    original_verify = store_module._verify_generation

    def swap_after_verify(safe_root, directory_fd):
        result = original_verify(safe_root, directory_fd)
        final.rename(escaped)
        final.mkdir(mode=0o700)
        return result

    monkeypatch.setattr(store_module, "_verify_generation", swap_after_verify)
    _assert_code(
        OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE,
        lambda: store.load_exact(request.mesh_job_id, request.generation),
    )


def test_unknown_root_entry_and_duplicate_tombstone_key_fail_closed(tmp_path: Path) -> None:
    store, root = _store(tmp_path)
    unknown = root / "unexpected"
    unknown.write_bytes(b"x")
    unknown.chmod(0o600)
    _assert_code(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE, store.recover_pending)
    unknown.unlink()

    request = _request()
    manifest = _publish(store, tmp_path, request)
    store.delete_exact(request.mesh_job_id, request.generation, manifest.manifest_sha256)
    tombstone = root / f".deleted_{'1' * 32}_{request.generation:016x}.json"
    raw = tombstone.read_bytes()
    tombstone.write_bytes(raw[:-1] + b',"schema_version":1}')
    tombstone.chmod(0o600)
    _assert_code(OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE, store.recover_pending)

    _assert_code(
        OrganicArtifactStoreErrorCode.INTEGRITY_FAILURE,
        lambda: store_module._decode_tombstone(b"[" * 20_000 + b"]" * 20_000),
    )


def test_missing_and_wrong_payload_bindings_are_rejected(tmp_path: Path) -> None:
    store, _ = _store(tmp_path)
    request = _request()
    result = _result(request)
    with _payloads(tmp_path, request, result) as (source, artifacts):
        _assert_code(
            OrganicArtifactStoreErrorCode.INVALID_INPUT,
            lambda: store.publish(request, source, result, artifacts[:-1]),
        )
        _assert_code(
            OrganicArtifactStoreErrorCode.INVALID_INPUT,
            lambda: store.publish(request, source, result, artifacts + (artifacts[0],)),
        )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_fork_inherited_store_is_rejected_and_new_store_does_not_deadlock(
    tmp_path: Path,
) -> None:
    store, root = _store(tmp_path)
    read_fd, write_fd = os.pipe()
    store._entry.mutex.acquire()
    process_id = os.fork()
    if process_id == 0:
        try:
            os.close(read_fd)
            try:
                store.recover_pending()
            except OrganicArtifactStoreError as error:
                inherited_rejected = error.code is OrganicArtifactStoreErrorCode.LEASE_UNAVAILABLE
            else:
                inherited_rejected = False
            fresh = OrganicArtifactStore(SafeRoot(root))
            fresh.recover_pending()
            os.write(write_fd, b"ok" if inherited_rejected else b"bad")
        finally:
            os._exit(0)
    os.close(write_fd)
    store._entry.mutex.release()
    try:
        assert os.read(read_fd, 3) == b"ok"
        _, status = os.waitpid(process_id, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        os.close(read_fd)


def test_missing_noreplace_syscall_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store_module.ctypes, "CDLL", lambda *_args, **_kwargs: object())
    with pytest.raises(OSError):
        store_module._rename_directory_noreplace(-1, "source", "destination")
