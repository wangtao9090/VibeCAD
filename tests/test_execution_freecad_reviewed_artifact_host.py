"""Host-owned reviewed artifact snapshot FD tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pickle
from pathlib import Path

import pytest

from vibecad.execution.freecad_reviewed_artifact_host import (
    REVIEWED_ARTIFACT_MANIFEST_NAME,
    REVIEWED_ARTIFACT_SNAPSHOT_KIND,
    TaskInputSnapshotError,
    TaskInputSnapshotErrorCode,
    TaskInputSnapshotLease,
)
from vibecad.execution.freecad_reviewed_artifact_inputs import (
    ReviewedArtifactCatalogRecord,
    ReviewedArtifactCatalogSnapshot,
)

_TASK_ID = "task_artifact_snapshot"
_PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
_BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
_RUN_ID = "run_0123456789abcdef0123456789abcdef"
_PAYLOAD = b"sealed-cad-input"


def _snapshot() -> ReviewedArtifactCatalogSnapshot:
    record = ReviewedArtifactCatalogRecord(
        artifact_id="artifact_input_1",
        content_sha256=hashlib.sha256(_PAYLOAD).hexdigest(),
        size_bytes=len(_PAYLOAD),
        media_type="image/png",
        role_term_ref_id="reference_image",
        schema_term_ref_id="image_png_v1",
        document_id="document_0123456789abcdef0123456789abcdef",
        family_id="freecad_imageplane",
        operation_ids=("imageplane_create",),
        maximum_bytes=1024,
    )
    return ReviewedArtifactCatalogSnapshot(
        task_id=_TASK_ID,
        project_id=_PROJECT_ID,
        base_revision=_BASE_REVISION,
        run_id=_RUN_ID,
        records=(record,),
    )


def _canonical_manifest(snapshot: ReviewedArtifactCatalogSnapshot) -> bytes:
    return json.dumps(
        snapshot.to_mapping(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _snapshot_directory(tmp_path: Path, snapshot: ReviewedArtifactCatalogSnapshot) -> Path:
    root = tmp_path / "snapshot"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    manifest = root / REVIEWED_ARTIFACT_MANIFEST_NAME
    manifest.write_bytes(_canonical_manifest(snapshot))
    manifest.chmod(0o600)
    payload = root / snapshot.records[0].artifact_id
    payload.write_bytes(_PAYLOAD)
    payload.chmod(0o600)
    return root


def _open_directory(root: Path) -> int:
    return os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )


def test_snapshot_lease_validates_exact_directory_and_duplicates_capability(tmp_path: Path) -> None:
    snapshot = _snapshot()
    root = _snapshot_directory(tmp_path, snapshot)
    source_fd = _open_directory(root)
    try:
        lease = TaskInputSnapshotLease(snapshot=snapshot, directory_fd=source_fd)
    finally:
        os.close(source_fd)

    expected = {
        "base_revision": _BASE_REVISION,
        "catalog_sha256": snapshot.catalog_sha256,
        "kind": REVIEWED_ARTIFACT_SNAPSHOT_KIND,
        "project_id": _PROJECT_ID,
        "run_id": _RUN_ID,
        "schema_version": 1,
        "task_id": _TASK_ID,
    }
    assert lease.descriptor_mapping() == expected
    duplicate = lease.duplicate_directory_fd()
    assert os.get_inheritable(duplicate) is False
    assert (os.fstat(duplicate).st_dev, os.fstat(duplicate).st_ino) == (
        os.stat(root).st_dev,
        os.stat(root).st_ino,
    )
    os.close(duplicate)

    with pytest.raises(TypeError):
        copy.copy(lease)
    with pytest.raises(TypeError):
        copy.deepcopy(lease)
    with pytest.raises(TypeError):
        pickle.dumps(lease)

    lease.close()
    lease.close()
    with pytest.raises(TaskInputSnapshotError) as caught:
        lease.duplicate_directory_fd()
    assert caught.value.code is TaskInputSnapshotErrorCode.CLOSED


@pytest.mark.parametrize("tamper", ["missing", "extra", "payload", "mode", "manifest"])
def test_snapshot_lease_rejects_nonexact_or_tampered_directory(
    tmp_path: Path,
    tamper: str,
) -> None:
    snapshot = _snapshot()
    root = _snapshot_directory(tmp_path, snapshot)
    payload = root / snapshot.records[0].artifact_id
    if tamper == "missing":
        payload.unlink()
    elif tamper == "extra":
        extra = root / "unlisted"
        extra.write_bytes(b"x")
        extra.chmod(0o600)
    elif tamper == "payload":
        payload.write_bytes(b"wrong-cad-input!")
    elif tamper == "mode":
        payload.chmod(0o640)
    else:
        (root / REVIEWED_ARTIFACT_MANIFEST_NAME).write_bytes(b"{}")
    descriptor = _open_directory(root)
    try:
        with pytest.raises(TaskInputSnapshotError) as caught:
            TaskInputSnapshotLease(snapshot=snapshot, directory_fd=descriptor)
    finally:
        os.close(descriptor)
    assert caught.value.code is TaskInputSnapshotErrorCode.INTEGRITY_FAILURE
