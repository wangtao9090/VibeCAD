"""Crash, integrity, and lifecycle tests for the visual-review artifact store."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from tests.test_visual_review_artifacts import _artifact
from vibecad._file_compat import set_private_dacl
from vibecad.daemon.adapters import LocalAgentClient
from vibecad.visual.review_artifacts import encode_visual_review_artifact
from vibecad.visual.review_store import (
    VisualReviewArtifactStore,
    VisualReviewStoreError,
    VisualReviewStoreErrorCode,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    if os.name == "nt":
        set_private_dacl(path)
    return path


def _store(tmp_path: Path) -> tuple[VisualReviewArtifactStore, Path, Path]:
    root = _private_directory(tmp_path / "reviews")
    locks = _private_directory(tmp_path / "locks")
    manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    identity = root.stat()
    return (
        VisualReviewArtifactStore(
            root=root,
            expected_root_identity=(identity.st_dev, identity.st_ino),
            lease_manager=manager,
        ),
        root,
        locks,
    )


def _fresh(root: Path, locks: Path) -> VisualReviewArtifactStore:
    identity = root.stat()
    return VisualReviewArtifactStore(
        root=root,
        expected_root_identity=(identity.st_dev, identity.st_ino),
        lease_manager=ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL),
    )


def test_publish_load_list_resource_and_restart_replay(tmp_path: Path) -> None:
    store, root, locks = _store(tmp_path)
    first = _artifact()
    second = _artifact(source_index=1)

    assert store.publish(first) == first
    assert store.publish(first) == first
    assert store.publish(second) == second
    assert store.load_exact(first.observation_id, 0) == first
    assert store.list_exact(first.observation_id, first.observation_digest) == (first, second)
    resource = store.read_resource(second.resource_uri)
    assert resource.uri == second.resource_uri
    assert resource.data == second.overlay.png_bytes

    replay = _fresh(root, locks)
    assert replay.load_exact(first.observation_id, 0).record_sha256 == first.record_sha256
    assert replay.read_resource(first.resource_uri).data == first.overlay.png_bytes
    assert tuple(sorted(path.name for path in root.iterdir())) == (
        "review_" + first.observation_id.removeprefix("visual_observation_") + "_00.bin",
        "review_" + first.observation_id.removeprefix("visual_observation_") + "_01.bin",
    )


def test_local_agent_client_reads_visual_review_from_captured_root(tmp_path: Path) -> None:
    store, root, _locks = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)

    class Kernel:
        def call(self, *_args, **_kwargs):
            raise AssertionError("review resource reads must stay off the daemon protocol")

        def close(self) -> None:
            pass

    client = LocalAgentClient(Kernel(), visual_review_root=root)
    try:
        resource = client.read_visual_review_resource(artifact.resource_uri)
    finally:
        client.close()

    assert resource.uri == artifact.resource_uri
    assert resource.data == artifact.overlay.png_bytes
    assert resource.media_type == "image/png"


def test_publish_same_identity_with_different_record_is_conflict(tmp_path: Path) -> None:
    store, _root, _locks = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)

    with pytest.raises(VisualReviewStoreError) as caught:
        store.publish(dataclasses.replace(artifact, generation=artifact.generation + 1))
    assert caught.value.code is VisualReviewStoreErrorCode.CONFLICT


def test_valid_stage_rolls_forward_and_partial_stage_rolls_back(tmp_path: Path) -> None:
    store, root, _locks = _store(tmp_path)
    artifact = _artifact()
    suffix = artifact.observation_id.removeprefix("visual_observation_")
    valid_stage = root / f".stage_{suffix}_00_{'1' * 32}.tmp"
    valid_stage.write_bytes(encode_visual_review_artifact(artifact))
    valid_stage.chmod(0o600)
    if os.name == "nt":
        set_private_dacl(valid_stage)
    partial_stage = root / f".stage_{suffix}_01_{'2' * 32}.tmp"
    partial_stage.write_bytes(b"partial")
    partial_stage.chmod(0o600)
    if os.name == "nt":
        set_private_dacl(partial_stage)

    result = store.recover_pending()

    assert result.published_stages == 1
    assert result.removed_partial_stages == 1
    assert store.load_exact(artifact.observation_id, 0) == artifact
    assert not valid_stage.exists()
    assert not partial_stage.exists()


def test_publish_crash_after_fsync_stage_recovers_without_regeneration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.review_store as module

    store, root, locks = _store(tmp_path)
    artifact = _artifact()
    original = module._rename_noreplace

    def crash(_root_fd: int, source: str, destination: str) -> None:
        if source.startswith(".stage_"):
            raise OSError("injected")
        original(_root_fd, source, destination)

    monkeypatch.setattr(module, "_rename_noreplace", crash)
    with pytest.raises(VisualReviewStoreError) as caught:
        store.publish(artifact)
    assert caught.value.code is VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN
    assert any(path.name.startswith(".stage_") for path in root.iterdir())

    monkeypatch.setattr(module, "_rename_noreplace", original)
    replay = _fresh(root, locks)
    # A first read is also a bounded recovery point; no caller regenerates the
    # already durable stage or needs to know its private filename.
    assert replay.load_exact(artifact.observation_id, 0) == artifact
    assert not any(path.name.startswith(".stage_") for path in root.iterdir())


def test_delete_tombstone_removes_all_views_and_blocks_republication(tmp_path: Path) -> None:
    store, root, locks = _store(tmp_path)
    first = _artifact()
    second = _artifact(source_index=1)
    store.publish(first)
    store.publish(second)

    assert store.delete_observation_exact(first.observation_id, first.observation_digest) == 2
    assert store.delete_observation_exact(first.observation_id, first.observation_digest) == 0
    assert tuple(path.name for path in root.iterdir()) == (
        ".deleted_" + first.observation_id.removeprefix("visual_observation_") + ".json",
    )
    for operation in (
        lambda: store.load_exact(first.observation_id, 0),
        lambda: store.read_resource(first.resource_uri),
        lambda: store.list_exact(first.observation_id, first.observation_digest),
    ):
        with pytest.raises(VisualReviewStoreError) as deleted:
            operation()
        assert deleted.value.code is VisualReviewStoreErrorCode.DELETED
    with pytest.raises(VisualReviewStoreError) as publish:
        store.publish(first)
    assert publish.value.code is VisualReviewStoreErrorCode.DELETED

    replay = _fresh(root, locks)
    with pytest.raises(VisualReviewStoreError) as restarted:
        replay.load_exact(first.observation_id, 0)
    assert restarted.value.code is VisualReviewStoreErrorCode.DELETED


def test_delete_crash_after_tombstone_publication_finishes_on_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root, locks = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)
    original = VisualReviewArtifactStore._move_then_remove

    def crash(*_args, **_kwargs):
        raise OSError("injected")

    monkeypatch.setattr(VisualReviewArtifactStore, "_move_then_remove", crash)
    with pytest.raises(VisualReviewStoreError) as caught:
        store.delete_observation_exact(artifact.observation_id, artifact.observation_digest)
    assert caught.value.code is VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN
    assert any(path.name.startswith(".deleted_") for path in root.iterdir())
    assert any(path.name.startswith("review_") for path in root.iterdir())

    monkeypatch.setattr(VisualReviewArtifactStore, "_move_then_remove", original)
    replay = _fresh(root, locks)
    summary = replay.recover_pending()
    assert summary.completed_deletions == 1
    assert not any(path.name.startswith("review_") for path in root.iterdir())


def test_delete_crash_before_tombstone_rename_recovers_without_poisoning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.visual.review_store as module

    store, root, locks = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)
    original = module._rename_noreplace

    def crash(root_fd: int, source: str, destination: str) -> None:
        if source.startswith(".deleted_") and source.endswith(".tmp"):
            raise OSError("injected")
        original(root_fd, source, destination)

    monkeypatch.setattr(module, "_rename_noreplace", crash)
    with pytest.raises(VisualReviewStoreError) as caught:
        store.delete_observation_exact(artifact.observation_id, artifact.observation_digest)
    assert caught.value.code is VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN
    assert any(path.name.endswith(".tmp") for path in root.iterdir())
    assert any(path.name.startswith("review_") for path in root.iterdir())

    monkeypatch.setattr(module, "_rename_noreplace", original)
    replay = _fresh(root, locks)
    with pytest.raises(VisualReviewStoreError) as deleted:
        replay.load_exact(artifact.observation_id, 0)
    assert deleted.value.code is VisualReviewStoreErrorCode.DELETED
    assert not any(path.name.startswith("review_") for path in root.iterdir())


def test_delete_crash_after_final_rename_finishes_delete_file_on_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root, locks = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)
    original = VisualReviewArtifactStore._unlink

    def crash(root_fd: int, name: str) -> None:
        if name.startswith(".delete_"):
            raise VisualReviewStoreError(VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN)
        original(root_fd, name)

    monkeypatch.setattr(VisualReviewArtifactStore, "_unlink", staticmethod(crash))
    with pytest.raises(VisualReviewStoreError) as caught:
        store.delete_observation_exact(artifact.observation_id, artifact.observation_digest)
    assert caught.value.code is VisualReviewStoreErrorCode.DURABILITY_UNCERTAIN
    assert any(path.name.startswith(".delete_") for path in root.iterdir())

    monkeypatch.setattr(VisualReviewArtifactStore, "_unlink", staticmethod(original))
    replay = _fresh(root, locks)
    summary = replay.recover_pending()
    assert summary.completed_deletions == 1
    assert not any(path.name.startswith(".delete_") for path in root.iterdir())


def test_tombstone_digest_conflict_never_deletes_other_observation_state(tmp_path: Path) -> None:
    store, root, _locks = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)

    with pytest.raises(VisualReviewStoreError) as caught:
        store.delete_observation_exact(artifact.observation_id, "0" * 64)
    assert caught.value.code is VisualReviewStoreErrorCode.CONFLICT
    assert any(path.name.startswith("review_") for path in root.iterdir())
    assert not any(path.name.startswith(".deleted_") for path in root.iterdir())
    assert store.load_exact(artifact.observation_id, 0) == artifact


def test_forged_tombstone_temp_cannot_poison_a_valid_observation(tmp_path: Path) -> None:
    store, root, _locks = _store(tmp_path)
    artifact = _artifact()
    store.publish(artifact)
    suffix = artifact.observation_id.removeprefix("visual_observation_")
    forged = root / f".deleted_{suffix}_{'4' * 32}.tmp"
    forged.write_text(
        '{"observation_digest":"'
        + "0" * 64
        + '","observation_id":"'
        + artifact.observation_id
        + '","schema_version":1}',
        encoding="ascii",
    )
    forged.chmod(0o600)
    if os.name == "nt":
        set_private_dacl(forged)

    with pytest.raises(VisualReviewStoreError) as caught:
        store.recover_pending()
    assert caught.value.code is VisualReviewStoreErrorCode.CONFLICT
    assert not any(path.name.endswith(".json") for path in root.iterdir())
    assert any(path.name.startswith("review_") for path in root.iterdir())


def test_unknown_symlink_and_replaced_root_fail_closed(tmp_path: Path) -> None:
    store, root, _locks = _store(tmp_path)
    (root / "unknown").symlink_to("outside")
    with pytest.raises(VisualReviewStoreError) as unknown:
        store.recover_pending()
    assert unknown.value.code is VisualReviewStoreErrorCode.INTEGRITY_FAILURE
    (root / "unknown").unlink()

    held = root.with_name("held")
    root.rename(held)
    _private_directory(root)
    with pytest.raises(VisualReviewStoreError) as swapped:
        store.load_exact(_artifact().observation_id, 0)
    assert swapped.value.code is VisualReviewStoreErrorCode.STORE_FAILURE


def test_record_and_observation_budgets_apply_before_second_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root, _locks = _store(tmp_path)
    first = _artifact()
    second = _artifact(source_index=1)
    store.publish(first)
    monkeypatch.setattr("vibecad.visual.review_store.MAX_VISUAL_REVIEW_RECORDS", 1)

    with pytest.raises(VisualReviewStoreError) as caught:
        store.publish(second)
    assert caught.value.code is VisualReviewStoreErrorCode.BUDGET_EXCEEDED
    assert len(tuple(root.iterdir())) == 1


def test_recovery_does_not_publish_valid_stage_past_final_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, root, _locks = _store(tmp_path)
    artifact = _artifact()
    suffix = artifact.observation_id.removeprefix("visual_observation_")
    stage = root / f".stage_{suffix}_00_{'3' * 32}.tmp"
    stage.write_bytes(encode_visual_review_artifact(artifact))
    stage.chmod(0o600)
    if os.name == "nt":
        set_private_dacl(stage)
    monkeypatch.setattr("vibecad.visual.review_store.MAX_VISUAL_REVIEW_RECORDS", 0)

    with pytest.raises(VisualReviewStoreError) as caught:
        store.recover_pending()
    assert caught.value.code is VisualReviewStoreErrorCode.BUDGET_EXCEEDED
    assert stage.exists()
    assert not any(path.name.startswith("review_") for path in root.iterdir())


def test_invalid_resource_and_missing_record_are_bounded(tmp_path: Path) -> None:
    store, _root, _locks = _store(tmp_path)
    artifact = _artifact()
    with pytest.raises(VisualReviewStoreError) as invalid:
        store.read_resource("file:///secret")
    assert invalid.value.code is VisualReviewStoreErrorCode.INVALID_INPUT

    with pytest.raises(VisualReviewStoreError) as missing:
        store.load_exact(artifact.observation_id, 0)
    assert missing.value.code is VisualReviewStoreErrorCode.NOT_FOUND


def test_store_has_no_task_cad_provider_or_mcp_authority() -> None:
    import inspect

    import vibecad.visual.review_store as module

    source = inspect.getsource(module)
    for forbidden in (
        "vibecad.tasks",
        "vibecad.application",
        "vibecad.cad",
        "vibecad.worker",
        "vibecad.providers",
        "vibecad.mcp",
        "blender",
        "bpy",
    ):
        assert forbidden not in source


def test_store_constructor_requires_exact_root_identity_and_lease_manager(tmp_path: Path) -> None:
    root = _private_directory(tmp_path / "reviews")
    locks = _private_directory(tmp_path / "locks")
    manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    identity = root.stat()

    with pytest.raises(VisualReviewStoreError) as identity_error:
        VisualReviewArtifactStore(
            root=root,
            expected_root_identity=(identity.st_dev, identity.st_ino + 1),
            lease_manager=manager,
        )
    assert identity_error.value.code is VisualReviewStoreErrorCode.INTEGRITY_FAILURE

    with pytest.raises(TypeError):
        VisualReviewArtifactStore(
            root=root,
            expected_root_identity=(identity.st_dev, identity.st_ino),
            lease_manager=object(),  # type: ignore[arg-type]
        )


def test_recovery_summary_starts_empty(tmp_path: Path) -> None:
    store, _root, _locks = _store(tmp_path)
    summary = store.recover_pending()
    assert dataclasses.astuple(summary) == (0, 0, 0, 0)
