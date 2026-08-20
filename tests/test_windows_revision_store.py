from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibecad._file_compat import (
    WindowsPathCapability,
    ensure_private_directory,
    open_private_file,
    open_windows_directory_fd,
    set_private_dacl,
    validate_windows_path,
)
from vibecad.execution import revisions_windows
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionCopyCursor,
    RevisionSourceBinding,
    RevisionStoreError,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
    _open_worker_candidate_staging,
    _open_worker_revision,
    _project_key,
)
from vibecad.worker.proxy import FreeCadWorker
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only contract")

_PROJECT_ID = "project_0123456789abcdef0123456789abcdef"


def _store(tmp_path: Path) -> tuple[LocalRevisionStore, ResourceLeaseManager, Path]:
    root = tmp_path / "revision-store"
    locks = tmp_path / "revision-locks"
    root.mkdir()
    locks.mkdir()
    set_private_dacl(root)
    set_private_dacl(locks)
    manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    return (
        LocalRevisionStore(
            root,
            manager,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        ),
        manager,
        root,
    )


def test_windows_revision_lifecycle_and_worker_capabilities(tmp_path: Path) -> None:
    store, manager, _root = _store(tmp_path)
    with manager.acquire_project_write(_PROJECT_ID) as lease:
        head = store.initialize_empty_project(_PROJECT_ID, lease)
        revision_id = store.begin_revision(_PROJECT_ID, head, lease)
        candidate = _open_worker_candidate_staging(
            store,
            expected_head=head,
            revision_id=revision_id,
            lease=lease,
        )
        assert type(candidate[0]) is WindowsPathCapability
        assert type(candidate[1]) is WindowsPathCapability
        assert validate_windows_path(candidate[0], directory=True) == Path(candidate[0].path)
        assert validate_windows_path(candidate[1], directory=True) == Path(candidate[1].path)
        assert candidate[2] == Path(candidate[1].path).name
        assert candidate[3] == candidate[0].volume == candidate[1].volume
        staging = store.snapshot_revisions(_PROJECT_ID)
        assert staging.head == head
        assert [entry.id for entry in staging.revisions] == [head.revision_id]

        model = store.candidate_model_path(_PROJECT_ID, revision_id, lease)
        step = store.candidate_artifact_path(_PROJECT_ID, revision_id, "step", lease)
        # Public mutable paths are verbatim paths, so callers do not need a
        # special short checkout even when the test root already exceeds MAX_PATH.
        assert os.fspath(model).startswith("\\\\?\\")
        model.write_bytes(b"windows-fcstd")
        step.write_bytes(b"windows-step")
        sealed = store.seal_revision(_PROJECT_ID, revision_id, lease)
        prepared = store.snapshot_revisions(_PROJECT_ID)
        assert prepared.head == head
        assert [entry.id for entry in prepared.revisions] == [head.revision_id]

        revision = _open_worker_revision(store, expected_revision=sealed)
        assert type(revision[0]) is WindowsPathCapability
        assert type(revision[1]) is WindowsPathCapability
        assert revision[2] == Path(revision[1].path).name
        assert revision[3] == revision[0].volume == revision[1].volume
        assert validate_windows_path(revision[0], directory=True) == Path(revision[0].path)
        assert validate_windows_path(revision[1], directory=True) == Path(revision[1].path)

        committed = store.commit_revision(_PROJECT_ID, head, revision_id, lease)
        assert committed.generation == 1
        assert committed.revision_id == sealed.id
        assert store.load_head(_PROJECT_ID) == committed
        assert store.load_revision(_PROJECT_ID, sealed.id) == sealed
        observed = store.observe_model_source(_PROJECT_ID, sealed.id)
        assert observed.head == committed
        assert observed.revision == sealed
        assert observed.model_path.read_bytes() == b"windows-fcstd"
        assert observed.model_binding.size == len(b"windows-fcstd")
        projects = store.snapshot_projects()
        ancestry = store.snapshot_revisions(_PROJECT_ID)
        assert projects[0].revision_id == committed.revision_id
        assert ancestry.head == committed
        assert {entry.id for entry in ancestry.revisions} == {
            head.revision_id,
            committed.revision_id,
        }


def test_windows_seal_retry_cleans_exact_bound_temp_after_process_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manager, root = _store(tmp_path)

    class SimulatedProcessCrash(BaseException):
        pass

    with manager.acquire_project_write(_PROJECT_ID) as lease:
        head = store.initialize_empty_project(_PROJECT_ID, lease)
        revision_id = store.begin_revision(_PROJECT_ID, head, lease)
        store.candidate_model_path(_PROJECT_ID, revision_id, lease).write_bytes(
            b"windows-crash-fcstd"
        )
        store.candidate_artifact_path(
            _PROJECT_ID,
            revision_id,
            "step",
            lease,
        ).write_bytes(b"windows-crash-step")
        original_write = revisions_windows._write_new_file
        crashed = False

        def write_then_crash(*args, **kwargs):
            nonlocal crashed
            written = original_write(*args, **kwargs)
            if not crashed and args[1] == "model.FCStd":
                crashed = True
                raise SimulatedProcessCrash
            return written

        with monkeypatch.context() as fault:
            fault.setattr(revisions_windows, "_write_new_file", write_then_crash)
            with pytest.raises(SimulatedProcessCrash):
                store.seal_revision(_PROJECT_ID, revision_id, lease)

        revisions = root / _project_key(_PROJECT_ID) / "revisions"
        temporary = tuple(
            path
            for path in revisions.iterdir()
            if path.name.startswith(".revision.") and path.name.endswith(".tmp")
        )
        assert len(temporary) == 1
        assert (temporary[0] / "model.FCStd").read_bytes() == b"windows-crash-fcstd"

        sealed = store.seal_revision(_PROJECT_ID, revision_id, lease)
        assert sealed.id == revision_id
        assert not any(
            path.name.startswith(".revision.") and path.name.endswith(".tmp")
            for path in revisions.iterdir()
        )


def test_windows_candidate_dacl_downgrade_fails_closed(tmp_path: Path) -> None:
    store, manager, _root = _store(tmp_path)
    with manager.acquire_project_write(_PROJECT_ID) as lease:
        head = store.initialize_empty_project(_PROJECT_ID, lease)
        revision_id = store.begin_revision(_PROJECT_ID, head, lease)
        opened = _open_worker_candidate_staging(
            store,
            expected_head=head,
            revision_id=revision_id,
            lease=lease,
        )
        candidate_path = Path(opened[1].path)
        changed = subprocess.run(
            ["icacls", os.fspath(candidate_path), "/inheritance:e"],
            check=False,
            capture_output=True,
            text=True,
        )
        if changed.returncode != 0:
            pytest.skip("icacls could not alter the test DACL")
        with pytest.raises(RevisionStoreError) as discovery:
            store.snapshot_projects()
        assert discovery.value.code is RevisionStoreErrorCode.UNSAFE_STORE
        with pytest.raises(RevisionStoreError) as captured:
            _open_worker_candidate_staging(
                store,
                expected_head=head,
                revision_id=revision_id,
                lease=lease,
            )
        assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE


def test_windows_store_root_file_id_replacement_fails_closed(tmp_path: Path) -> None:
    store, manager, root = _store(tmp_path)
    with manager.acquire_project_write(_PROJECT_ID) as lease:
        store.initialize_empty_project(_PROJECT_ID, lease)
    displaced = tmp_path / "displaced-revision-store"
    root.rename(displaced)
    root.mkdir()
    set_private_dacl(root)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(_PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    with pytest.raises(RevisionStoreError) as discovery:
        store.snapshot_projects()
    assert discovery.value.code is RevisionStoreErrorCode.UNSAFE_STORE


def test_windows_revision_snapshot_rejects_directory_reparse_point(tmp_path: Path) -> None:
    store, manager, root = _store(tmp_path)
    with manager.acquire_project_write(_PROJECT_ID) as lease:
        store.initialize_empty_project(_PROJECT_ID, lease)
    moved = root.with_name(root.name + ".moved")
    root.rename(moved)
    linked = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", os.fspath(root), os.fspath(moved)],
        check=False,
        capture_output=True,
        text=True,
    )
    if linked.returncode != 0:
        moved.rename(root)
        pytest.skip(
            "directory junction creation is unavailable: "
            + (linked.stdout + linked.stderr).strip()
        )
    try:
        with pytest.raises(RevisionStoreError) as captured:
            store.snapshot_projects()
        assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    finally:
        os.rmdir(root)
        moved.rename(root)


def test_windows_copy_revision_artifacts_supports_exact_resume(tmp_path: Path) -> None:
    store, manager, _root = _store(tmp_path)
    model_raw = b"windows resumable model"
    step_raw = b"ISO-10303-21;WINDOWS;ENDSEC;"
    with manager.acquire_project_write(_PROJECT_ID) as lease:
        head = store.initialize_empty_project(_PROJECT_ID, lease)
        revision_id = store.begin_revision(_PROJECT_ID, head, lease)
        store.candidate_model_path(_PROJECT_ID, revision_id, lease).write_bytes(model_raw)
        store.candidate_artifact_path(
            _PROJECT_ID,
            revision_id,
            "step",
            lease,
        ).write_bytes(step_raw)
        sealed = store.seal_revision(_PROJECT_ID, revision_id, lease)

    destination = tmp_path / "artifact-copy"
    destination_capability = ensure_private_directory(destination)
    prefix = model_raw[:7]
    prefix_fd, _prefix_capability = open_private_file(
        destination / "model.FCStd",
        expected_parent=destination_capability,
        exclusive=True,
    )
    try:
        os.write(prefix_fd, prefix)
        os.fsync(prefix_fd)
    finally:
        os.close(prefix_fd)
    directory_fd = open_windows_directory_fd(destination)
    try:
        store.copy_revision_artifacts_at(
            expected_revision=sealed,
            destination_directory_fd=directory_fd,
            cursors=(
                RevisionCopyCursor(
                    name="model.FCStd",
                    size_bytes=len(prefix),
                    sha256=hashlib.sha256(prefix).hexdigest(),
                ),
            ),
            chunk_bytes=5,
        )
    finally:
        os.close(directory_fd)
    assert (destination / "model.FCStd").read_bytes() == model_raw
    assert (destination / "model.step").read_bytes() == step_raw


def _source_binding(info: os.stat_result) -> RevisionSourceBinding:
    return RevisionSourceBinding(
        dev=info.st_dev,
        ino=info.st_ino,
        mode=info.st_mode,
        uid=info.st_uid,
        nlink=info.st_nlink,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_birthtime_ns,
    )


def test_windows_import_at_survives_store_restart(tmp_path: Path) -> None:
    store, manager, root = _store(tmp_path)
    payload = b"windows trusted generation zero"
    digest = hashlib.sha256(payload).hexdigest()
    source_directory = tmp_path / "trusted-source"
    source_parent = ensure_private_directory(source_directory)
    source_path = source_directory / "validated.FCStd"
    source_fd, _source_capability = open_private_file(
        source_path,
        expected_parent=source_parent,
        exclusive=True,
    )
    try:
        os.write(source_fd, payload)
        os.fsync(source_fd)
        binding = _source_binding(os.fstat(source_fd))
    finally:
        os.close(source_fd)

    source_parent_fd = open_windows_directory_fd(source_directory)
    try:
        with manager.acquire_project_write(_PROJECT_ID) as lease:
            head = store.import_trusted_fcstd_at(
                _PROJECT_ID,
                source_parent_fd=source_parent_fd,
                source_name=source_path.name,
                expected_binding=binding,
                expected_sha256=digest,
                expected_size=len(payload),
                lease=lease,
            )
    finally:
        os.close(source_parent_fd)

    assert head.generation == 0
    assert store.load_head(_PROJECT_ID) == head
    revision = store.load_revision(_PROJECT_ID, head.revision_id)
    assert revision.model is not None
    assert revision.model.sha256 == digest
    assert store.revision_model_path(_PROJECT_ID, head.revision_id).read_bytes() == payload

    restarted_manager = ResourceLeaseManager(
        tmp_path / "revision-locks",
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    restarted_store = LocalRevisionStore(
        root,
        restarted_manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    restarted_head = restarted_store.load_head(_PROJECT_ID)
    restarted_revision = restarted_store.load_revision(
        _PROJECT_ID,
        restarted_head.revision_id,
    )
    assert restarted_head == head
    assert restarted_revision == revision
    assert (
        restarted_store.revision_model_path(
            _PROJECT_ID,
            restarted_head.revision_id,
        ).read_bytes()
        == payload
    )


def test_windows_import_at_rejects_same_content_file_id_replacement(
    tmp_path: Path,
) -> None:
    store, manager, _root = _store(tmp_path)
    payload = b"same content, different Windows File ID"
    source_directory = tmp_path / "trusted-source"
    source_parent = ensure_private_directory(source_directory)
    source_path = source_directory / "validated.FCStd"
    source_fd, _source_capability = open_private_file(
        source_path,
        expected_parent=source_parent,
        exclusive=True,
    )
    try:
        os.write(source_fd, payload)
        os.fsync(source_fd)
        original_binding = _source_binding(os.fstat(source_fd))
    finally:
        os.close(source_fd)

    source_parent_fd = open_windows_directory_fd(source_directory)
    displaced = source_directory / "displaced.FCStd"
    source_path.rename(displaced)
    replacement_fd, _replacement_capability = open_private_file(
        source_path,
        expected_parent=source_parent,
        exclusive=True,
    )
    try:
        os.write(replacement_fd, payload)
        os.fsync(replacement_fd)
    finally:
        os.close(replacement_fd)

    try:
        with manager.acquire_project_write(_PROJECT_ID) as lease:
            with pytest.raises(RevisionStoreError) as captured:
                store.import_trusted_fcstd_at(
                    _PROJECT_ID,
                    source_parent_fd=source_parent_fd,
                    source_name=source_path.name,
                    expected_binding=original_binding,
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                    expected_size=len(payload),
                    lease=lease,
                )
    finally:
        os.close(source_parent_fd)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT
    with pytest.raises(RevisionStoreError) as missing:
        store.load_head(_PROJECT_ID)
    assert missing.value.code is RevisionStoreErrorCode.NOT_FOUND


@pytest.mark.slow
def test_real_windows_worker_binds_revision_store_capability(tmp_path: Path) -> None:
    configured = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not configured:
        pytest.skip("managed FreeCAD Python was not supplied")
    python = Path(configured)
    if not python.is_file():
        pytest.skip("managed FreeCAD Python is unavailable")
    store, manager, _root = _store(tmp_path)
    worker = FreeCadWorker.start(
        python=python,
        source_root=Path(__file__).parents[1] / "src",
    )
    try:
        with manager.acquire_project_write(_PROJECT_ID) as lease:
            head = store.initialize_empty_project(_PROJECT_ID, lease)
            revision_id = store.begin_revision(_PROJECT_ID, head, lease)
            candidate = worker.bind_candidate(
                store=store,
                lease=lease,
                base_head=head,
                revision_id=revision_id,
            )
            session = worker.create_empty(candidate)
            worker.close_session(session)
            worker.release_candidate(candidate)
    finally:
        worker.close()
