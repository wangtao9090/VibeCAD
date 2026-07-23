"""Read-only committed revision comparison tests."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from vibecad.application.revision_compare import (
    RevisionCompareError,
    RevisionCompareErrorCode,
    RevisionCompareService,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreRootTrust,
)
from vibecad.workflow.lease import (
    LeaseRootTrust,
    ResourceLeaseManager,
)

PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
OTHER_PROJECT_ID = "project_11111111111111111111111111111111"


@pytest.fixture
def store_parts(
    tmp_path: Path,
) -> tuple[LocalRevisionStore, ResourceLeaseManager, Path]:
    store_root = tmp_path / "projects"
    lock_root = tmp_path / "locks"
    store_root.mkdir(mode=0o700)
    lock_root.mkdir(mode=0o700)
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    store = LocalRevisionStore(
        store_root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    return store, manager, store_root


def _commit(
    store: LocalRevisionStore,
    manager: ResourceLeaseManager,
    head,
    *,
    model: bytes,
    step: bytes,
):
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        store.candidate_model_path(PROJECT_ID, revision_id, lease).write_bytes(model)
        store.candidate_artifact_path(
            PROJECT_ID,
            revision_id,
            "step",
            lease,
        ).write_bytes(step)
        revision = store.seal_revision(PROJECT_ID, revision_id, lease)
        committed = store.commit_revision(PROJECT_ID, head, revision_id, lease)
    return revision, committed


def _lineage(store, manager):
    with manager.acquire_project_write(PROJECT_ID) as lease:
        base = store.initialize_empty_project(PROJECT_ID, lease)
    child, child_head = _commit(
        store,
        manager,
        base,
        model=b"child-fcstd",
        step=b"child-step",
    )
    grandchild, grandchild_head = _commit(
        store,
        manager,
        child_head,
        model=b"grandchild-fcstd",
        step=b"child-step",
    )
    return base, child, child_head, grandchild, grandchild_head


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int, int], ...]:
    result = []
    for path in sorted((root, *root.rglob("*"))):
        value = path.stat(follow_symlinks=False)
        relative = "." if path == root else str(path.relative_to(root))
        digest = ""
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.append((relative, digest, value.st_mode, value.st_size))
    return tuple(result)


def _artifact_change(result: dict[str, object], name: str) -> dict[str, object]:
    changes = result["artifact_changes"]
    assert type(changes) is list
    return next(item for item in changes if item["name"] == name)


def test_compare_exact_empty_to_descendant_artifact_diff(store_parts) -> None:
    store, manager, _root = store_parts
    base, _child, _child_head, grandchild, grandchild_head = _lineage(store, manager)

    result = RevisionCompareService(store=store).compare_revisions(
        project_id=PROJECT_ID,
        from_revision=base.revision_id,
        to_revision=grandchild.id,
    )

    assert set(result) == {
        "project_id",
        "head",
        "from_revision",
        "to_revision",
        "ancestry",
        "base_change",
        "revision_manifest",
        "artifact_changes",
        "semantic_diff",
    }
    assert result["project_id"] == PROJECT_ID
    assert result["head"] == grandchild_head
    assert result["from_revision"].id == base.revision_id
    assert result["to_revision"] == grandchild
    assert result["ancestry"] == {
        "verified": True,
        "relation": "from_ancestor_of_to",
    }
    assert result["base_change"] == {
        "changed": True,
        "from_base": None,
        "to_base": grandchild.base_revision,
    }
    assert result["revision_manifest"] == {
        "changed": True,
        "from_sha256": base.manifest_sha256,
        "to_sha256": grandchild.manifest_sha256,
    }
    model = _artifact_change(result, "model.FCStd")
    step = _artifact_change(result, "model.step")
    assert model["format"] == "fcstd"
    assert model["change"] == "added"
    assert model["from"] is None
    assert model["to"] == grandchild.model
    assert step["format"] == "step"
    assert step["change"] == "added"
    assert step["from"] is None
    assert step["to"] == grandchild.artifacts[0]
    assert result["semantic_diff"] == {
        "status": "unsupported",
        "scopes": ["geometry", "entity", "parameter"],
    }


@pytest.mark.parametrize(
    ("left", "right", "relation"),
    [
        ("base", "base", "same"),
        ("base", "child", "from_ancestor_of_to"),
        ("base", "grandchild", "from_ancestor_of_to"),
        ("child", "base", "to_ancestor_of_from"),
        ("grandchild", "child", "to_ancestor_of_from"),
    ],
)
def test_compare_ancestry_relation_matrix(
    store_parts,
    left: str,
    right: str,
    relation: str,
) -> None:
    store, manager, _root = store_parts
    base, child, _child_head, grandchild, _grandchild_head = _lineage(store, manager)
    identifiers = {
        "base": base.revision_id,
        "child": child.id,
        "grandchild": grandchild.id,
    }

    result = RevisionCompareService(store=store).compare_revisions(
        project_id=PROJECT_ID,
        from_revision=identifiers[left],
        to_revision=identifiers[right],
    )

    assert result["ancestry"] == {"verified": True, "relation": relation}
    if relation == "same":
        assert result["base_change"]["changed"] is False
        assert result["revision_manifest"]["changed"] is False
        assert all(item["change"] == "unchanged" for item in result["artifact_changes"])


def test_compare_reports_file_change_without_claiming_geometry_change(store_parts) -> None:
    store, manager, _root = store_parts
    _base, child, _child_head, grandchild, _grandchild_head = _lineage(store, manager)

    result = RevisionCompareService(store=store).compare_revisions(
        project_id=PROJECT_ID,
        from_revision=child.id,
        to_revision=grandchild.id,
    )

    model = _artifact_change(result, "model.FCStd")
    step = _artifact_change(result, "model.step")
    assert model["change"] == "modified"
    assert model["from"].sha256 != model["to"].sha256
    assert step["change"] == "modified"
    assert step["from"].sha256 == step["to"].sha256
    assert step["from"].id != step["to"].id
    assert result["semantic_diff"]["status"] == "unsupported"


@pytest.mark.parametrize("artifact_name", ["model.FCStd", "model.step"])
@pytest.mark.parametrize("mutation", ["missing", "same_size_tamper"])
def test_compare_rejects_missing_or_tampered_committed_payload(
    store_parts,
    artifact_name: str,
    mutation: str,
) -> None:
    store, manager, _root = store_parts
    base, child, _child_head, _grandchild, _grandchild_head = _lineage(store, manager)
    if artifact_name == "model.FCStd":
        path = store.revision_model_path(PROJECT_ID, child.id)
    else:
        path = store.revision_artifact_path(
            PROJECT_ID,
            child.id,
            child.artifacts[0].id,
        )
    if mutation == "missing":
        path.unlink()
    else:
        original = path.read_bytes()
        replacement = bytes((value + 1) % 256 for value in original)
        assert len(replacement) == len(original)
        path.write_bytes(replacement)
        os.chmod(path, 0o600)

    with pytest.raises(RevisionCompareError) as captured:
        RevisionCompareService(store=store).compare_revisions(
            project_id=PROJECT_ID,
            from_revision=base.revision_id,
            to_revision=child.id,
        )

    assert captured.value.code is RevisionCompareErrorCode.INTEGRITY_FAILURE


def test_compare_hides_prepared_orphan_and_cross_project_revision(store_parts) -> None:
    store, manager, _root = store_parts
    base, child, child_head, _grandchild, _grandchild_head = _lineage(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        orphan_id = store.begin_revision(PROJECT_ID, _grandchild_head, lease)
        store.candidate_model_path(PROJECT_ID, orphan_id, lease).write_bytes(b"orphan")
        store.candidate_artifact_path(PROJECT_ID, orphan_id, "step", lease).write_bytes(
            b"orphan-step"
        )
        store.seal_revision(PROJECT_ID, orphan_id, lease)
    with manager.acquire_project_write(OTHER_PROJECT_ID) as lease:
        other_head = store.initialize_empty_project(OTHER_PROJECT_ID, lease)

    service = RevisionCompareService(store=store)
    for unknown in (orphan_id, other_head.revision_id, "revision_" + "f" * 32):
        with pytest.raises(RevisionCompareError) as captured:
            service.compare_revisions(
                project_id=PROJECT_ID,
                from_revision=base.revision_id,
                to_revision=unknown,
            )
        assert captured.value.code is RevisionCompareErrorCode.NOT_FOUND

    result = service.compare_revisions(
        project_id=PROJECT_ID,
        from_revision=base.revision_id,
        to_revision=child.id,
    )
    assert result["to_revision"] == child
    assert result["head"].revision_id == _grandchild_head.revision_id
    assert child_head.generation == 1


def test_compare_is_read_only_and_does_not_take_project_write_lease(
    store_parts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, manager, root = store_parts
    base, child, _child_head, _grandchild, _grandchild_head = _lineage(store, manager)
    before = _tree_snapshot(root)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("project write lease is forbidden")

    monkeypatch.setattr(ResourceLeaseManager, "acquire_project_write", forbidden)
    result = RevisionCompareService(store=store).compare_revisions(
        project_id=PROJECT_ID,
        from_revision=base.revision_id,
        to_revision=child.id,
    )

    assert result["ancestry"]["verified"] is True
    assert _tree_snapshot(root) == before


def test_compare_rejects_invalid_input_without_touching_store(store_parts) -> None:
    store, _manager, root = store_parts
    before = _tree_snapshot(root)
    service = RevisionCompareService(store=store)

    for kwargs in (
        {
            "project_id": "project_BAD",
            "from_revision": "revision_" + "1" * 32,
            "to_revision": "revision_" + "2" * 32,
        },
        {
            "project_id": PROJECT_ID,
            "from_revision": True,
            "to_revision": "revision_" + "2" * 32,
        },
    ):
        with pytest.raises(RevisionCompareError) as captured:
            service.compare_revisions(**kwargs)
        assert captured.value.code is RevisionCompareErrorCode.INVALID_INPUT

    assert _tree_snapshot(root) == before
