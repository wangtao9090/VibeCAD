from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import replace
from pathlib import Path

import pytest

import vibecad.execution.revisions as revisions_module
from vibecad.application.revision_discovery import (
    RevisionDiscoveryError,
    RevisionDiscoveryErrorCode,
    RevisionDiscoveryService,
)
from vibecad.execution.revisions import (
    CommitJournalState,
    LocalRevisionStore,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

PROJECT_A = "project_0123456789abcdef0123456789abcdef"
PROJECT_B = "project_11111111111111111111111111111111"
MISSING_REVISION = "revision_ffffffffffffffffffffffffffffffff"
PROJECT_PATH_DOMAIN = b"vibecad-revision-project-path-v1\0"
REVISION_PATH_DOMAIN = b"vibecad-revision-content-path-v1\0"
MANIFEST_CHECKSUM_DOMAIN = b"vibecad-revision-manifest-v1\0"
HEAD_CHECKSUM_DOMAIN = b"vibecad-project-head-v1\0"
JOURNAL_CHECKSUM_DOMAIN = b"vibecad-commit-journal-v1\0"
RESERVATION_CHECKSUM_DOMAIN = b"vibecad-revision-reservation-v1\0"


def _parts(tmp_path: Path):
    root = tmp_path / "revisions"
    locks = tmp_path / "locks"
    root.mkdir(mode=0o700)
    locks.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    os.chmod(locks, 0o700)
    manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    store = LocalRevisionStore(
        root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    return root, locks, manager, store


def _empty(store: LocalRevisionStore, manager: ResourceLeaseManager, project_id: str):
    with manager.acquire_project_write(project_id) as lease:
        return store.initialize_empty_project(project_id, lease)


def _next(store: LocalRevisionStore, manager: ResourceLeaseManager, project_id: str, head):
    with manager.acquire_project_write(project_id) as lease:
        revision_id = store.begin_revision(project_id, head, lease)
        store.candidate_model_path(project_id, revision_id, lease).write_bytes(b"model")
        store.candidate_artifact_path(project_id, revision_id, "step", lease).write_bytes(b"step")
        sealed = store.seal_revision(project_id, revision_id, lease)
        committed = store.commit_revision(project_id, head, revision_id, lease)
    return sealed, committed


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _checked(body: dict[str, object], domain: bytes) -> bytes:
    checksum = hashlib.sha256(domain + _canonical(body)).hexdigest()
    return _canonical({**body, "checksum": checksum})


def _path_key(domain: bytes, identifier: str) -> str:
    return hashlib.sha256(domain + identifier.encode("utf-8")).hexdigest()


def _project_dir(root: Path, project_id: str) -> Path:
    return root / _path_key(PROJECT_PATH_DOMAIN, project_id)


def _revision_dir(root: Path, project_id: str, revision_id: str) -> Path:
    return (
        _project_dir(root, project_id) / "revisions" / _path_key(REVISION_PATH_DOMAIN, revision_id)
    )


def _write_record(path: Path, body: dict[str, object], domain: bytes) -> bytes:
    raw = _checked(body, domain)
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return raw


def _rewrite_manifest(
    root: Path,
    project_id: str,
    revision_id: str,
    *,
    base_revision: str | None,
) -> str:
    path = _revision_dir(root, project_id, revision_id) / "manifest.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body.pop("checksum")
    body["base_revision"] = base_revision
    raw = _write_record(path, body, MANIFEST_CHECKSUM_DOMAIN)
    return hashlib.sha256(raw).hexdigest()


def _tree_state(root: Path) -> dict[str, tuple[int, ...] | bytes]:
    result: dict[str, tuple[int, ...] | bytes] = {}
    for path in sorted(root.rglob("*")):
        value = path.lstat()
        relative = str(path.relative_to(root))
        result[relative + ":stat"] = (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if stat.S_ISREG(value.st_mode):
            result[relative + ":bytes"] = path.read_bytes()
    return result


def _replace_directory(path: Path) -> Path:
    moved = path.with_name(path.name + ".moved")
    path.rename(moved)
    shutil.copytree(moved, path, copy_function=shutil.copy2)
    return moved


def _assert_error(error: RevisionDiscoveryError, code: RevisionDiscoveryErrorCode):
    assert error.code is code
    assert str(error) == code.value
    assert error.args == (code.value,)


def test_project_discovery_is_canonical_bounded_and_reopen_stable(tmp_path: Path):
    root, locks, manager, store = _parts(tmp_path)
    assert RevisionDiscoveryService(store=store).list_projects() == {
        "projects": [],
        "next_cursor": None,
    }
    head_b = _empty(store, manager, PROJECT_B)
    head_a = _empty(store, manager, PROJECT_A)
    service = RevisionDiscoveryService(store=store)

    first = service.list_projects(limit=1)
    assert first["projects"] == [
        {
            "project_id": PROJECT_A,
            "generation": 0,
            "revision_id": head_a.revision_id,
            "manifest_sha256": head_a.manifest_sha256,
        }
    ]
    assert re.fullmatch(r"project_list_cursor_[0-9a-f]{64}", first["next_cursor"])

    reopened_manager = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    reopened = LocalRevisionStore(
        root,
        reopened_manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    second = RevisionDiscoveryService(store=reopened).list_projects(
        limit=100,
        cursor=first["next_cursor"],
    )
    assert second == {
        "projects": [
            {
                "project_id": PROJECT_B,
                "generation": 0,
                "revision_id": head_b.revision_id,
                "manifest_sha256": head_b.manifest_sha256,
            }
        ],
        "next_cursor": None,
    }


def test_revision_discovery_lists_only_canonical_head_ancestry(tmp_path: Path):
    _root, _locks, manager, store = _parts(tmp_path)
    base = _empty(store, manager, PROJECT_A)
    sealed_b, head_b = _next(store, manager, PROJECT_A, base)
    sealed_c, head_c = _next(store, manager, PROJECT_A, head_b)

    result = RevisionDiscoveryService(store=store).list_revisions(
        project_id=PROJECT_A,
        limit=100,
    )
    assert result["project_id"] == PROJECT_A
    assert result["head"] == {
        "project_id": PROJECT_A,
        "generation": 2,
        "revision_id": head_c.revision_id,
        "manifest_sha256": head_c.manifest_sha256,
    }
    expected = {
        base.revision_id: (None, base.manifest_sha256),
        sealed_b.id: (base.revision_id, sealed_b.manifest_sha256),
        sealed_c.id: (sealed_b.id, sealed_c.manifest_sha256),
    }
    assert [entry["id"] for entry in result["revisions"]] == sorted(expected)
    for entry in result["revisions"]:
        base_revision, digest = expected[entry["id"]]
        assert entry == {
            "id": entry["id"],
            "project_id": PROJECT_A,
            "base_revision": base_revision,
            "manifest_sha256": digest,
        }
    assert result["next_cursor"] is None


def test_discovery_cursor_rejects_malformed_foreign_stale_and_cross_endpoint(
    tmp_path: Path,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    _empty(store, manager, PROJECT_B)
    service = RevisionDiscoveryService(store=store)
    project_cursor = service.list_projects(limit=1)["next_cursor"]
    revision_cursor = service.list_revisions(
        project_id=PROJECT_A,
        limit=1,
    )["next_cursor"]
    assert project_cursor is not None
    assert revision_cursor is None

    for cursor in (True, "", "project_list_cursor_" + "g" * 64):
        with pytest.raises(RevisionDiscoveryError) as captured:
            service.list_projects(cursor=cursor)
        _assert_error(captured.value, RevisionDiscoveryErrorCode.INVALID_INPUT)

    other_root = tmp_path / "other-root"
    other_locks = tmp_path / "other-locks"
    other_root.mkdir(mode=0o700)
    other_locks.mkdir(mode=0o700)
    other_manager = ResourceLeaseManager(
        other_locks,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    other_store = LocalRevisionStore(
        other_root,
        other_manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    _empty(other_store, other_manager, PROJECT_A)
    _empty(other_store, other_manager, PROJECT_B)
    with pytest.raises(RevisionDiscoveryError) as captured:
        RevisionDiscoveryService(store=other_store).list_projects(cursor=project_cursor)
    _assert_error(captured.value, RevisionDiscoveryErrorCode.CONFLICT)

    _sealed, _committed = _next(store, manager, PROJECT_A, head)
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_projects(cursor=project_cursor)
    _assert_error(captured.value, RevisionDiscoveryErrorCode.CONFLICT)

    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_revisions(project_id=PROJECT_A, cursor=project_cursor)
    _assert_error(captured.value, RevisionDiscoveryErrorCode.INVALID_INPUT)


@pytest.mark.parametrize("limit", (0, 101, True, None, 1.0))
def test_discovery_limit_is_exact_and_bounded(tmp_path: Path, limit: object):
    _root, _locks, _manager, store = _parts(tmp_path)
    service = RevisionDiscoveryService(store=store)
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_projects(limit=limit)
    _assert_error(captured.value, RevisionDiscoveryErrorCode.INVALID_INPUT)


def test_discovery_not_found_and_no_artifact_hashing(tmp_path: Path, monkeypatch):
    _root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    service = RevisionDiscoveryService(store=store)
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_revisions(project_id=PROJECT_B)
    _assert_error(captured.value, RevisionDiscoveryErrorCode.NOT_FOUND)

    def forbidden_hash(*_args, **_kwargs):
        raise AssertionError("discovery must not stream CAD bytes")

    monkeypatch.setattr(hashlib, "file_digest", forbidden_hash)
    result = service.list_revisions(project_id=PROJECT_A)
    assert result["head"]["revision_id"] == head.revision_id


def test_active_staging_and_prepared_old_head_are_valid_but_hidden(tmp_path: Path):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    service = RevisionDiscoveryService(store=store)
    with manager.acquire_project_write(PROJECT_A) as lease:
        staging_id = store.begin_revision(PROJECT_A, head, lease)
        staged = service.list_revisions(project_id=PROJECT_A)
        assert [item["id"] for item in staged["revisions"]] == [head.revision_id]
        assert staging_id not in _canonical(staged).decode("utf-8")

        store.candidate_model_path(PROJECT_A, staging_id, lease).write_bytes(b"model")
        store.candidate_artifact_path(PROJECT_A, staging_id, "step", lease).write_bytes(b"step")
        sealed = store.seal_revision(PROJECT_A, staging_id, lease)
        prepared = service.list_revisions(project_id=PROJECT_A)
        assert [item["id"] for item in prepared["revisions"]] == [head.revision_id]
        assert sealed.id not in _canonical(prepared).decode("utf-8")
    assert _revision_dir(root, PROJECT_A, sealed.id).is_dir()


def test_prepared_new_head_and_terminal_committed_are_valid_and_included(
    tmp_path: Path,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    sealed, committed = _next(store, manager, PROJECT_A, head)
    service = RevisionDiscoveryService(store=store)
    terminal = service.list_revisions(project_id=PROJECT_A)
    assert {item["id"] for item in terminal["revisions"]} == {
        head.revision_id,
        sealed.id,
    }

    journal_path = _project_dir(root, PROJECT_A) / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal.pop("checksum")
    journal["state"] = CommitJournalState.PREPARED.value
    _write_record(journal_path, journal, JOURNAL_CHECKSUM_DOMAIN)
    prepared_new = service.list_revisions(project_id=PROJECT_A)
    assert prepared_new["head"]["revision_id"] == committed.revision_id
    assert {item["id"] for item in prepared_new["revisions"]} == {
        head.revision_id,
        sealed.id,
    }


@pytest.mark.parametrize("sealed_candidate", (False, True))
def test_terminal_not_committed_is_valid_and_candidate_is_hidden(
    tmp_path: Path,
    sealed_candidate: bool,
):
    _root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    with manager.acquire_project_write(PROJECT_A) as lease:
        revision_id = store.begin_revision(PROJECT_A, head, lease)
        if sealed_candidate:
            store.candidate_model_path(PROJECT_A, revision_id, lease).write_bytes(b"model")
            store.candidate_artifact_path(PROJECT_A, revision_id, "step", lease).write_bytes(
                b"step"
            )
            store.seal_revision(PROJECT_A, revision_id, lease)
        store.rollback_revision(PROJECT_A, revision_id, lease)
    result = RevisionDiscoveryService(store=store).list_revisions(project_id=PROJECT_A)
    assert [item["id"] for item in result["revisions"]] == [head.revision_id]
    assert revision_id not in _canonical(result).decode("utf-8")


def test_rolled_back_sibling_is_hidden_after_a_different_child_commits(
    tmp_path: Path,
):
    _root, _locks, manager, store = _parts(tmp_path)
    head_a = _empty(store, manager, PROJECT_A)
    with manager.acquire_project_write(PROJECT_A) as lease:
        revision_b = store.begin_revision(PROJECT_A, head_a, lease)
        store.candidate_model_path(PROJECT_A, revision_b, lease).write_bytes(b"model-b")
        store.candidate_artifact_path(PROJECT_A, revision_b, "step", lease).write_bytes(b"step-b")
        store.seal_revision(PROJECT_A, revision_b, lease)
        store.rollback_revision(PROJECT_A, revision_b, lease)

        revision_c = store.begin_revision(PROJECT_A, head_a, lease)
        store.candidate_model_path(PROJECT_A, revision_c, lease).write_bytes(b"model-c")
        store.candidate_artifact_path(PROJECT_A, revision_c, "step", lease).write_bytes(b"step-c")
        store.seal_revision(PROJECT_A, revision_c, lease)
        committed = store.commit_revision(PROJECT_A, head_a, revision_c, lease)
    result = RevisionDiscoveryService(store=store).list_revisions(project_id=PROJECT_A)
    assert {item["id"] for item in result["revisions"]} == {
        head_a.revision_id,
        committed.revision_id,
    }
    assert revision_b not in _canonical(result).decode("utf-8")


def test_full_scan_rejects_late_project_corruption_before_serving_next_page(
    tmp_path: Path,
):
    root, _locks, manager, store = _parts(tmp_path)
    _empty(store, manager, PROJECT_A)
    _empty(store, manager, PROJECT_B)
    service = RevisionDiscoveryService(store=store)
    cursor = service.list_projects(limit=1)["next_cursor"]
    assert cursor is not None
    head_path = _project_dir(root, PROJECT_B) / "HEAD.json"
    head_path.write_bytes(b"{}")
    os.chmod(head_path, 0o600)
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_projects(limit=100, cursor=cursor)
    _assert_error(captured.value, RevisionDiscoveryErrorCode.INTEGRITY_FAILURE)


@pytest.mark.parametrize("fault", ("missing_base", "cycle", "generation"))
def test_discovery_rejects_invalid_graphs(tmp_path: Path, fault: str):
    root, _locks, manager, store = _parts(tmp_path)
    head_a = _empty(store, manager, PROJECT_A)
    sealed_b, head_b = _next(store, manager, PROJECT_A, head_a)
    if fault == "missing_base":
        _rewrite_manifest(
            root,
            PROJECT_A,
            sealed_b.id,
            base_revision=MISSING_REVISION,
        )
    elif fault == "cycle":
        sealed_c, _head_c = _next(store, manager, PROJECT_A, head_b)
        _rewrite_manifest(
            root,
            PROJECT_A,
            sealed_b.id,
            base_revision=sealed_c.id,
        )
    else:
        head_path = _project_dir(root, PROJECT_A) / "HEAD.json"
        bad_head = replace(head_b, generation=head_b.generation + 1)
        _write_record(head_path, bad_head.to_mapping(), HEAD_CHECKSUM_DOMAIN)
    with pytest.raises(RevisionDiscoveryError) as captured:
        RevisionDiscoveryService(store=store).list_revisions(project_id=PROJECT_A)
    _assert_error(captured.value, RevisionDiscoveryErrorCode.INTEGRITY_FAILURE)


def test_revision_cursor_allows_limit_change_and_rejects_cross_project(
    tmp_path: Path,
):
    _root, _locks, manager, store = _parts(tmp_path)
    head_a = _empty(store, manager, PROJECT_A)
    _next(store, manager, PROJECT_A, head_a)
    head_b = _empty(store, manager, PROJECT_B)
    _next(store, manager, PROJECT_B, head_b)
    service = RevisionDiscoveryService(store=store)
    first = service.list_revisions(project_id=PROJECT_A, limit=1)
    cursor = first["next_cursor"]
    assert cursor is not None
    second = service.list_revisions(
        project_id=PROJECT_A,
        limit=100,
        cursor=cursor,
    )
    assert len(first["revisions"]) + len(second["revisions"]) == 2
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_revisions(
            project_id=PROJECT_B,
            limit=100,
            cursor=cursor,
        )
    _assert_error(captured.value, RevisionDiscoveryErrorCode.CONFLICT)


def test_mutable_candidate_payload_is_excluded_from_cursor_snapshot(tmp_path: Path):
    _root, _locks, manager, store = _parts(tmp_path)
    head_a = _empty(store, manager, PROJECT_A)
    _empty(store, manager, PROJECT_B)
    with manager.acquire_project_write(PROJECT_A) as lease:
        revision_id = store.begin_revision(PROJECT_A, head_a, lease)
        candidate = store.candidate_model_path(PROJECT_A, revision_id, lease)
        service = RevisionDiscoveryService(store=store)
        first = service.list_projects(limit=1)
        assert first["next_cursor"] is not None
        candidate.write_bytes(b"mutable draft bytes")
        second = service.list_projects(limit=100, cursor=first["next_cursor"])
    assert [item["project_id"] for item in second["projects"]] == [PROJECT_B]


def test_hidden_sealed_orphan_changes_invalidate_revision_cursor(tmp_path: Path):
    root, _locks, manager, store = _parts(tmp_path)
    head_a = _empty(store, manager, PROJECT_A)
    with manager.acquire_project_write(PROJECT_A) as lease:
        revision_b = store.begin_revision(PROJECT_A, head_a, lease)
        store.candidate_model_path(PROJECT_A, revision_b, lease).write_bytes(b"model-b")
        store.candidate_artifact_path(PROJECT_A, revision_b, "step", lease).write_bytes(b"step-b")
        store.seal_revision(PROJECT_A, revision_b, lease)
        store.rollback_revision(PROJECT_A, revision_b, lease)
        revision_c = store.begin_revision(PROJECT_A, head_a, lease)
        store.candidate_model_path(PROJECT_A, revision_c, lease).write_bytes(b"model-c")
        store.candidate_artifact_path(PROJECT_A, revision_c, "step", lease).write_bytes(b"step-c")
        store.seal_revision(PROJECT_A, revision_c, lease)
        store.commit_revision(PROJECT_A, head_a, revision_c, lease)
    service = RevisionDiscoveryService(store=store)
    cursor = service.list_revisions(project_id=PROJECT_A, limit=1)["next_cursor"]
    assert cursor is not None

    orphan_manifest = _revision_dir(root, PROJECT_A, revision_b) / "manifest.json"
    body = json.loads(orphan_manifest.read_text(encoding="utf-8"))
    body.pop("checksum")
    body["model"]["sha256"] = "f" * 64
    _write_record(orphan_manifest, body, MANIFEST_CHECKSUM_DOMAIN)
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_revisions(
            project_id=PROJECT_A,
            limit=100,
            cursor=cursor,
        )
    _assert_error(captured.value, RevisionDiscoveryErrorCode.CONFLICT)


def test_unknown_temp_unsafe_and_inconsistent_reservation_fail_closed(
    tmp_path: Path,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    service = RevisionDiscoveryService(store=store)

    unknown = root / "unknown"
    unknown.write_bytes(b"x")
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_projects()
    _assert_error(captured.value, RevisionDiscoveryErrorCode.STORE_FAILURE)
    unknown.unlink()

    temporary = root / ".project.0123456789abcdef0123456789abcdef.tmp"
    temporary.mkdir(mode=0o700)
    with pytest.raises(RevisionDiscoveryError) as captured:
        service.list_projects()
    _assert_error(captured.value, RevisionDiscoveryErrorCode.RECOVERY_REQUIRED)
    temporary.rmdir()

    with manager.acquire_project_write(PROJECT_A) as lease:
        revision_id = store.begin_revision(PROJECT_A, head, lease)
        candidate = store.candidate_model_path(PROJECT_A, revision_id, lease)
        os.chmod(candidate, 0o622)
        with pytest.raises(RevisionDiscoveryError) as captured:
            service.list_projects()
        _assert_error(captured.value, RevisionDiscoveryErrorCode.STORE_FAILURE)
        os.chmod(candidate, 0o600)

        reservation_path = (
            root
            / ".revision-quota"
            / "reservations"
            / _path_key(REVISION_PATH_DOMAIN, revision_id)
            / "reservation.json"
        )
        reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
        reservation.pop("checksum")
        reservation["state"] = "published"
        _write_record(
            reservation_path,
            reservation,
            RESERVATION_CHECKSUM_DOMAIN,
        )
        with pytest.raises(RevisionDiscoveryError) as captured:
            service.list_projects()
        _assert_error(captured.value, RevisionDiscoveryErrorCode.RECOVERY_REQUIRED)


def test_discovery_is_read_only_uses_no_project_lease_and_never_hashes_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    sealed, _committed = _next(store, manager, PROJECT_A, head)
    model_path = _revision_dir(root, PROJECT_A, sealed.id) / "model.FCStd"
    original = model_path.read_bytes()
    model_path.write_bytes(bytes(reversed(original)))
    assert len(model_path.read_bytes()) == len(original)
    before = _tree_state(root)

    def forbidden_project_lease(*_args, **_kwargs):
        raise AssertionError("discovery must not acquire a project write lease")

    def forbidden_payload_hash(*_args, **_kwargs):
        raise AssertionError("discovery must not use revision payload validation")

    monkeypatch.setattr(
        ResourceLeaseManager,
        "acquire_project_write",
        forbidden_project_lease,
    )
    monkeypatch.setattr(revisions_module, "_load_revision_fd", forbidden_payload_hash)
    monkeypatch.setattr(
        revisions_module,
        "_validate_revision_content",
        forbidden_payload_hash,
    )
    result = RevisionDiscoveryService(store=store).list_revisions(project_id=PROJECT_A)
    assert result["head"]["revision_id"] == sealed.id
    assert _tree_state(root) == before


@pytest.mark.parametrize("fault", ("release", "root_close"))
def test_discovery_release_and_root_close_faults_are_store_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
):
    root, _locks, manager, store = _parts(tmp_path)
    _empty(store, manager, PROJECT_A)
    if fault == "release":
        monkeypatch.setattr(
            revisions_module,
            "_release_quota_lease",
            lambda _lease: RevisionStoreErrorCode.IO_ERROR,
        )
    else:
        original_close = revisions_module._close_fd
        root_identity = (root.stat().st_dev, root.stat().st_ino)

        def close_with_root_fault(file_fd: int):
            try:
                value = os.fstat(file_fd)
                identity = (value.st_dev, value.st_ino)
            except OSError:
                identity = None
            result = original_close(file_fd)
            return result or identity == root_identity

        monkeypatch.setattr(revisions_module, "_close_fd", close_with_root_fault)
    with pytest.raises(RevisionDiscoveryError) as captured:
        RevisionDiscoveryService(store=store).list_projects()
    _assert_error(captured.value, RevisionDiscoveryErrorCode.STORE_FAILURE)


@pytest.mark.parametrize("target", ("project", "revision", "root"))
def test_discovery_rejects_same_name_directory_replacement_races(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    project_path = _project_dir(root, PROJECT_A)
    revision_path = _revision_dir(root, PROJECT_A, head.revision_id)
    selected = {
        "project": project_path,
        "revision": revision_path,
        "root": root,
    }[target]
    selected_identity = (selected.stat().st_dev, selected.stat().st_ino)
    original_entries = revisions_module._discovery_entries
    replaced = False

    def replace_after_scan(directory_fd: int):
        nonlocal replaced
        result = original_entries(directory_fd)
        value = os.fstat(directory_fd)
        if not replaced and (value.st_dev, value.st_ino) == selected_identity:
            replaced = True
            _replace_directory(selected)
        return result

    monkeypatch.setattr(
        revisions_module,
        "_discovery_entries",
        replace_after_scan,
    )
    with pytest.raises(RevisionDiscoveryError) as captured:
        RevisionDiscoveryService(store=store).list_projects()
    _assert_error(captured.value, RevisionDiscoveryErrorCode.STORE_FAILURE)
    assert replaced


def test_discovery_pins_quota_reservations_across_full_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    with manager.acquire_project_write(PROJECT_A) as lease:
        store.begin_revision(PROJECT_A, head, lease)
    reservations_path = root / ".revision-quota" / "reservations"
    original_load = revisions_module._load_reservations
    replaced = False

    def replace_after_load(root_fd: int, root_device: int):
        nonlocal replaced
        result = original_load(root_fd, root_device)
        if not replaced:
            replaced = True
            moved = _replace_directory(reservations_path)
            shutil.rmtree(moved)
        return result

    monkeypatch.setattr(
        revisions_module,
        "_load_reservations",
        replace_after_load,
    )
    with pytest.raises(RevisionDiscoveryError) as captured:
        RevisionDiscoveryService(store=store).list_projects()
    _assert_error(captured.value, RevisionDiscoveryErrorCode.STORE_FAILURE)
    assert replaced


def test_discovery_performs_one_indexed_reservation_lookup_per_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _root, _locks, manager, store = _parts(tmp_path)
    project_ids = tuple(f"project_{index:032x}" for index in range(10, 14))
    for project_id in project_ids:
        head = _empty(store, manager, project_id)
        with manager.acquire_project_write(project_id) as lease:
            store.begin_revision(project_id, head, lease)
    original_lookup = revisions_module._discovery_reservations_for_project
    calls = 0

    def counted_lookup(index, project_id):
        nonlocal calls
        calls += 1
        return original_lookup(index, project_id)

    monkeypatch.setattr(
        revisions_module,
        "_discovery_reservations_for_project",
        counted_lookup,
    )
    result = RevisionDiscoveryService(store=store).list_projects(limit=100)
    assert len(result["projects"]) == len(project_ids)
    assert calls == len(project_ids)


@pytest.mark.parametrize("target", ("root", "project", "revision"))
def test_discovery_rejects_same_inode_member_insertion_after_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    selected = {
        "root": root,
        "project": _project_dir(root, PROJECT_A),
        "revision": _revision_dir(root, PROJECT_A, head.revision_id),
    }[target]
    selected_identity = (selected.stat().st_dev, selected.stat().st_ino)
    original_entries = revisions_module._discovery_entries
    inserted = False

    def insert_after_scan(directory_fd: int):
        nonlocal inserted
        result = original_entries(directory_fd)
        value = os.fstat(directory_fd)
        if not inserted and (value.st_dev, value.st_ino) == selected_identity:
            inserted = True
            late = selected / "late-unvalidated.bin"
            late.write_bytes(b"late")
            os.chmod(late, 0o600)
        return result

    monkeypatch.setattr(
        revisions_module,
        "_discovery_entries",
        insert_after_scan,
    )
    with pytest.raises(RevisionDiscoveryError) as captured:
        RevisionDiscoveryService(store=store).list_projects()
    _assert_error(captured.value, RevisionDiscoveryErrorCode.STORE_FAILURE)
    assert inserted


def test_candidate_payload_write_during_scan_does_not_change_directory_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root, _locks, manager, store = _parts(tmp_path)
    head = _empty(store, manager, PROJECT_A)
    with manager.acquire_project_write(PROJECT_A) as lease:
        revision_id = store.begin_revision(PROJECT_A, head, lease)
        candidate_path = store.candidate_model_path(PROJECT_A, revision_id, lease)
    candidate_directory = candidate_path.parent
    candidate_identity = (
        candidate_directory.stat().st_dev,
        candidate_directory.stat().st_ino,
    )
    original_entries = revisions_module._discovery_entries
    changed = False

    def write_payload_after_scan(directory_fd: int):
        nonlocal changed
        result = original_entries(directory_fd)
        value = os.fstat(directory_fd)
        if not changed and (value.st_dev, value.st_ino) == candidate_identity:
            changed = True
            candidate_path.write_bytes(b"draft payload may change")
        return result

    monkeypatch.setattr(
        revisions_module,
        "_discovery_entries",
        write_payload_after_scan,
    )
    result = RevisionDiscoveryService(store=store).list_projects()
    assert [item["project_id"] for item in result["projects"]] == [PROJECT_A]
    assert changed
