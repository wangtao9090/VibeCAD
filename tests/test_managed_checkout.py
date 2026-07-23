from __future__ import annotations

import hashlib
import multiprocessing
import os
import signal
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.execution.revisions as revisions_module
import vibecad.interaction.checkouts as checkout_module
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionStoreError,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
)
from vibecad.interaction.checkouts import (
    MAX_CHECKOUT_FILE_BYTES,
    MAX_CHECKOUT_TEMP_ENTRIES,
    MAX_CHECKOUT_TOTAL_BYTES,
    MAX_CLOSED_TOMBSTONES,
    MAX_OPEN_CHECKOUTS,
    CheckoutError,
    CheckoutErrorCode,
    CheckoutSourceLiveness,
    CheckoutState,
    CheckoutStoreRootTrust,
    DraftCheckoutSource,
    HeadCheckoutSource,
    ManagedCheckoutStore,
)
from vibecad.interaction.storage import CheckoutMutationLock, SafeRoot, StorageFailure
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReviewDraft, ReviewPolicy, TaskStatus
from vibecad.workflow.store import (
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
    TaskStoreRootTrust,
)

PROJECT_ID = "project_" + "1" * 32
OPEN_KEY = "checkout_open_" + "2" * 32
MODEL_BYTES = b"FCStd sample bytes"


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _store_from_base(base: Path) -> ManagedCheckoutStore:
    leases = ResourceLeaseManager(base / "locks", trust=LeaseRootTrust.TRUSTED_LOCAL)
    revisions = LocalRevisionStore(
        base / "projects",
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    tasks = TaskRunStore(
        base / "tasks",
        leases,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    return ManagedCheckoutStore(
        base / "checkouts",
        base / "locks",
        revisions,
        tasks,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )


def _advance_head(
    revisions: LocalRevisionStore,
    head: ProjectHead,
    *,
    payload: bytes = b"advanced model",
) -> ProjectHead:
    leases = revisions._lease_manager
    with leases.acquire_project_write(PROJECT_ID) as lease:
        revision_id = revisions.begin_revision(PROJECT_ID, head, lease)
        model = revisions.candidate_model_path(PROJECT_ID, revision_id, lease)
        step = revisions.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        model.write_bytes(payload)
        step.write_bytes(b"ISO-10303-21;ENDSEC;")
        os.chmod(model, 0o600)
        os.chmod(step, 0o600)
        revision = revisions.seal_revision(PROJECT_ID, revision_id, lease)
        return revisions.commit_revision(PROJECT_ID, head, revision.id, lease)


def _draft_source(
    revisions: LocalRevisionStore,
    head: ProjectHead,
    *,
    generation: int = 7,
):
    task_id = "task_" + "3" * 32
    leases = revisions._lease_manager
    with leases.acquire_project_write(PROJECT_ID) as lease:
        revision_id = revisions.begin_revision(PROJECT_ID, head, lease)
        model = revisions.candidate_model_path(PROJECT_ID, revision_id, lease)
        step = revisions.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        model.write_bytes(b"verified draft model")
        step.write_bytes(b"ISO-10303-21;DRAFT;ENDSEC;")
        os.chmod(model, 0o600)
        os.chmod(step, 0o600)
        revision = revisions.seal_revision(PROJECT_ID, revision_id, lease)
        revisions.rollback_revision(PROJECT_ID, revision_id, lease)
    draft = ReviewDraft(
        id="draft_" + revision_id.removeprefix("revision_"),
        task_id=task_id,
        project_id=PROJECT_ID,
        base_revision=head.revision_id,
        base_generation=head.generation,
        base_manifest_sha256=head.manifest_sha256,
        revision_id=revision_id,
        manifest_sha256=revision.manifest_sha256,
        verification_id="verification_" + "4" * 32,
        acceptance_id="acceptance-review",
        observation_digest="5" * 64,
    )
    stored = SimpleNamespace(
        generation=generation,
        task_run=SimpleNamespace(
            id=task_id,
            project_id=PROJECT_ID,
            base_revision=head.revision_id,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
            status=TaskStatus.AWAITING_USER_REVIEW,
            draft=draft,
        ),
    )
    source = DraftCheckoutSource(
        task_id=task_id,
        draft_id=draft.id,
        expected_generation=generation,
    )
    return source, draft, stored, revision


def _concurrent_open_worker(
    base: str,
    open_key: str,
    maximum_open: int,
    start,
    results,
) -> None:
    checkout_module.MAX_OPEN_CHECKOUTS = maximum_open
    store = _store_from_base(Path(base))
    start.wait(10)
    try:
        descriptor = store.open(open_key, HeadCheckoutSource(project_id=PROJECT_ID))
    except CheckoutError as error:
        results.put(("error", error.code.value))
    else:
        results.put(("ok", descriptor.checkout_id))


def _crash_open_worker(base: str, open_key: str, stage: str) -> None:
    def crash(_self, current: str) -> None:
        if current == stage:
            os._exit(91)

    checkout_module.ManagedCheckoutStore._fault = crash
    store = _store_from_base(Path(base))
    store.open(open_key, HeadCheckoutSource(project_id=PROJECT_ID))


def _crash_close_worker(base: str, checkout_id: str, stage: str) -> None:
    def crash(_self, current: str) -> None:
        if current == stage:
            os._exit(92)

    checkout_module.ManagedCheckoutStore._fault = crash
    store = _store_from_base(Path(base))
    store.close(checkout_id)


@pytest.fixture
def checkout_rig(tmp_path: Path):
    lock_root = tmp_path / "locks"
    revision_root = tmp_path / "projects"
    task_root = tmp_path / "tasks"
    checkout_root = tmp_path / "checkouts"
    for root in (lock_root, revision_root, task_root, checkout_root):
        _mkdir(root)
    leases = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    revisions = LocalRevisionStore(
        revision_root,
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    tasks = TaskRunStore(task_root, leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    source = tmp_path / "sample.FCStd"
    source.write_bytes(MODEL_BYTES)
    os.chmod(source, 0o600)
    raw = source.read_bytes()
    with leases.acquire_project_write(PROJECT_ID) as lease:
        head = revisions.import_trusted_fcstd(
            PROJECT_ID,
            source,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            lease,
        )
    store = ManagedCheckoutStore(
        checkout_root,
        lock_root,
        revisions,
        tasks,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )
    return store, revisions, head, checkout_root


@pytest.fixture
def empty_checkout_rig(tmp_path: Path):
    lock_root = tmp_path / "locks"
    revision_root = tmp_path / "projects"
    task_root = tmp_path / "tasks"
    checkout_root = tmp_path / "checkouts"
    for root in (lock_root, revision_root, task_root, checkout_root):
        _mkdir(root)
    leases = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    revisions = LocalRevisionStore(
        revision_root,
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    tasks = TaskRunStore(task_root, leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    with leases.acquire_project_write(PROJECT_ID) as lease:
        head = revisions.initialize_empty_project(PROJECT_ID, lease)
    store = ManagedCheckoutStore(
        checkout_root,
        lock_root,
        revisions,
        tasks,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )
    return store, revisions, head, checkout_root


def test_open_copies_head_to_non_authoritative_single_link(checkout_rig) -> None:
    store, revisions, head, _root = checkout_rig

    descriptor = store.open(
        OPEN_KEY,
        HeadCheckoutSource(project_id=PROJECT_ID),
    )

    source = revisions.revision_model_path(PROJECT_ID, head.revision_id)
    assert descriptor.state is CheckoutState.OPEN
    assert descriptor.authoritative is False
    assert descriptor.dirty is False
    assert descriptor.local_path.read_bytes() == source.read_bytes()
    assert descriptor.local_path.stat().st_ino != source.stat().st_ino
    assert descriptor.local_path.stat().st_nlink == 1
    assert descriptor.source_head == head
    assert descriptor.source_liveness.value == "live"
    wire = descriptor.to_wire_mapping()
    assert wire.keys() == {
        "checkout_id",
        "open_key",
        "state",
        "authoritative",
        "dirty",
        "source",
        "initial_model_sha256",
        "current_model_sha256",
        "current_size_bytes",
    }
    local = descriptor.to_local_mapping()
    assert local == wire | {
        "source_head": head.to_mapping(),
        "source_liveness": "live",
    }
    assert "local_path" not in local
    assert "source_binding" not in local


def test_key_first_replay_keeps_original_source_and_observes_safe_edit(checkout_rig) -> None:
    store, revisions, head, _root = checkout_rig
    source = HeadCheckoutSource(project_id=PROJECT_ID)
    first = store.open(OPEN_KEY, source)
    replacement = first.local_path.with_suffix(".replacement")
    replacement.write_bytes(b"safely edited model")
    os.chmod(replacement, 0o600)
    os.replace(replacement, first.local_path)

    replay = store.open(OPEN_KEY, source)

    assert replay.checkout_id == first.checkout_id
    assert replay.source.revision_id == head.revision_id
    assert replay.dirty is True
    assert replay.current_model_sha256 != replay.initial_model_sha256
    assert revisions.load_head(PROJECT_ID) == head


def test_open_survives_restart_and_close_is_terminal_idempotent(checkout_rig) -> None:
    store, revisions, _head, root = checkout_rig
    first = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    restarted = ManagedCheckoutStore(
        root,
        store.lock_root,
        revisions,
        store.task_store,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )

    assert restarted.get(first.checkout_id).checkout_id == first.checkout_id
    closed = restarted.close(first.checkout_id)
    replay = restarted.close(first.checkout_id)

    assert closed.state is CheckoutState.CLOSED
    assert replay.to_wire_mapping() == closed.to_wire_mapping()
    assert not first.local_path.exists()


@pytest.mark.parametrize("replacement_kind", ["symlink", "hardlink"])
def test_replay_rejects_link_replacement(checkout_rig, replacement_kind: str) -> None:
    store, _revisions, _head, root = checkout_rig
    first = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    external = root.parent / "external.FCStd"
    external.write_bytes(b"tamper")
    os.chmod(external, 0o600)
    first.local_path.unlink()
    if replacement_kind == "symlink":
        first.local_path.symlink_to(external)
    else:
        os.link(external, first.local_path)

    with pytest.raises(CheckoutError) as raised:
        store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))

    assert raised.value.code is CheckoutErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "replacement_kind",
    ["model_symlink", "model_inode", "checkout_symlink"],
)
def test_get_revalidates_returned_local_path_after_hash(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
) -> None:
    store, _revisions, _head, root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    original_hash = SafeRoot.hash_open_file
    replaced = False

    def replace_after_hash(self, parent_fd, name, *, maximum):
        nonlocal replaced
        result = original_hash(self, parent_fd, name, maximum=maximum)
        if name == "model.FCStd" and not replaced:
            replaced = True
            if replacement_kind == "model_symlink":
                external = root.parent / "external-model.FCStd"
                external.write_bytes(b"outside")
                os.chmod(external, 0o600)
                opened.local_path.unlink()
                opened.local_path.symlink_to(external)
            elif replacement_kind == "model_inode":
                replacement = opened.local_path.with_suffix(".new-inode")
                replacement.write_bytes(b"different inode")
                os.chmod(replacement, 0o600)
                os.replace(replacement, opened.local_path)
            else:
                moved = root.parent / f"{opened.checkout_id}.moved"
                opened.local_path.parent.rename(moved)
                external = root.parent / "external-checkout"
                external.mkdir(mode=0o700)
                os.chmod(external, 0o700)
                external_model = external / "model.FCStd"
                external_model.write_bytes(b"outside checkout")
                os.chmod(external_model, 0o600)
                (root / opened.checkout_id).symlink_to(external, target_is_directory=True)
        return result

    monkeypatch.setattr(SafeRoot, "hash_open_file", replace_after_hash)
    with pytest.raises(CheckoutError) as raised:
        store.get(opened.checkout_id)

    assert replaced is True
    assert raised.value.code is CheckoutErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("operation", ["get", "replay", "fresh"])
def test_open_descriptor_rebinds_the_live_checkout_root_after_hash(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store, _revisions, _head, root = checkout_rig
    source = HeadCheckoutSource(project_id=PROJECT_ID)
    opened = None if operation == "fresh" else store.open(OPEN_KEY, source)
    original_hash = SafeRoot.hash_open_file
    detached = root.with_name(f"{root.name}-detached")
    forged_bytes = b"forged checkout root"
    swapped = False

    def replace_root_after_hash(self, parent_fd, name, *, maximum):
        nonlocal swapped
        result = original_hash(self, parent_fd, name, maximum=maximum)
        if self is store._root and name == "model.FCStd" and not swapped:  # noqa: SLF001
            swapped = True
            checkout_names = tuple(
                entry.name
                for entry in root.iterdir()
                if entry.name.startswith("checkout_") and entry.is_dir()
            )
            assert len(checkout_names) == 1
            checkout_id = checkout_names[0]
            root.rename(detached)
            root.mkdir(mode=0o700)
            os.chmod(root, 0o700)
            forged_checkout = root / checkout_id
            forged_checkout.mkdir(mode=0o700)
            os.chmod(forged_checkout, 0o700)
            forged_model = forged_checkout / "model.FCStd"
            forged_model.write_bytes(forged_bytes)
            os.chmod(forged_model, 0o600)
        return result

    monkeypatch.setattr(SafeRoot, "hash_open_file", replace_root_after_hash)
    with pytest.raises(CheckoutError) as raised:
        if operation == "get":
            assert opened is not None
            store.get(opened.checkout_id)
        else:
            store.open(OPEN_KEY, source)

    assert swapped is True
    assert raised.value.code is CheckoutErrorCode.INTEGRITY_FAILURE
    assert tuple(path.read_bytes() for path in root.glob("checkout_*/model.FCStd")) == (
        forged_bytes,
    )
    assert tuple(path.read_bytes() for path in detached.glob("checkout_*/model.FCStd")) == (
        MODEL_BYTES,
    )


def test_get_revalidates_metadata_entry_after_trusted_read(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    metadata = opened.local_path.parent / "metadata.json"
    external = root.parent / "external-metadata.json"
    external.write_bytes(b"{}")
    os.chmod(external, 0o600)
    original_read = SafeRoot.read_file_at
    metadata_reads = 0

    def replace_after_read(self, parent_fd, name, *, maximum):
        nonlocal metadata_reads
        result = original_read(self, parent_fd, name, maximum=maximum)
        if name == "metadata.json":
            metadata_reads += 1
            if metadata_reads == 2:
                metadata.unlink()
                metadata.symlink_to(external)
        return result

    monkeypatch.setattr(SafeRoot, "read_file_at", replace_after_read)
    with pytest.raises(CheckoutError) as raised:
        store.get(opened.checkout_id)

    assert metadata_reads == 2
    assert raised.value.code is CheckoutErrorCode.INTEGRITY_FAILURE


def test_same_key_with_different_intent_conflicts(checkout_rig) -> None:
    store, _revisions, _head, _root = checkout_rig
    store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))

    with pytest.raises(CheckoutError) as raised:
        store.open(
            OPEN_KEY,
            HeadCheckoutSource(project_id="project_" + "9" * 32),
        )

    assert raised.value.code is CheckoutErrorCode.CONFLICT


def test_fixed_checkout_budgets_are_frozen() -> None:
    assert MAX_CHECKOUT_FILE_BYTES == 536_870_912
    assert MAX_CHECKOUT_TOTAL_BYTES == 2_147_483_648
    assert MAX_OPEN_CHECKOUTS == 8
    assert MAX_CHECKOUT_TEMP_ENTRIES == 8
    assert MAX_CLOSED_TOMBSTONES == 1_024


@pytest.mark.parametrize(
    ("maximum", "expected"),
    [(len(MODEL_BYTES), None), (len(MODEL_BYTES) - 1, CheckoutErrorCode.RESOURCE_EXHAUSTED)],
)
def test_file_budget_n_and_n_plus_one(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
    maximum: int,
    expected: CheckoutErrorCode | None,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    monkeypatch.setattr(checkout_module, "MAX_CHECKOUT_FILE_BYTES", maximum)

    if expected is None:
        descriptor = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
        assert descriptor.current_size_bytes == maximum
    else:
        with pytest.raises(CheckoutError) as raised:
            store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
        assert raised.value.code is expected


def test_total_budget_admits_n_then_rejects_n_plus_one(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    monkeypatch.setattr(checkout_module, "MAX_CHECKOUT_TOTAL_BYTES", len(MODEL_BYTES) * 2)

    first = store.open("checkout_open_" + "a" * 32, HeadCheckoutSource(project_id=PROJECT_ID))
    second = store.open("checkout_open_" + "b" * 32, HeadCheckoutSource(project_id=PROJECT_ID))
    assert first.current_size_bytes + second.current_size_bytes == len(MODEL_BYTES) * 2

    with pytest.raises(CheckoutError) as raised:
        store.open("checkout_open_" + "c" * 32, HeadCheckoutSource(project_id=PROJECT_ID))
    assert raised.value.code is CheckoutErrorCode.RESOURCE_EXHAUSTED


def test_atomic_edit_n_plus_one_fails_closed_until_explicit_close_recovers_capacity(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, root = checkout_rig
    source = HeadCheckoutSource(project_id=PROJECT_ID)
    first_key = "checkout_open_" + "a" * 32
    second_key = "checkout_open_" + "b" * 32
    maximum = len(MODEL_BYTES) * 2
    monkeypatch.setattr(checkout_module, "MAX_CHECKOUT_TOTAL_BYTES", maximum)
    source_path = revisions.revision_model_path(PROJECT_ID, head.revision_id)
    source_bytes = source_path.read_bytes()

    first = store.open(first_key, source)
    second = store.open(second_key, source)
    assert first.current_size_bytes + second.current_size_bytes == maximum
    assert store.get(first.checkout_id).current_size_bytes == len(MODEL_BYTES)

    replacement = first.local_path.with_suffix(".atomic-edit")
    replacement.write_bytes(MODEL_BYTES + b"x")
    os.chmod(replacement, 0o600)
    os.replace(replacement, first.local_path)

    restarted = ManagedCheckoutStore(
        root,
        store.lock_root,
        revisions,
        store.task_store,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )
    for operation in (
        lambda: restarted.get(first.checkout_id),
        lambda: restarted.get(second.checkout_id),
        lambda: restarted.open(first_key, source),
    ):
        with pytest.raises(CheckoutError) as raised:
            operation()
        assert raised.value.code is CheckoutErrorCode.RESOURCE_EXHAUSTED
        assert raised.value.descriptor is None

    assert first.local_path.parent.exists()
    assert second.local_path.parent.exists()
    assert not list(root.glob("closed_checkout_*.json"))
    assert source_path.read_bytes() == source_bytes
    assert revisions.load_head(PROJECT_ID) == head

    closed = restarted.close(first.checkout_id)
    assert closed.state is CheckoutState.CLOSED
    assert closed.dirty is True
    assert closed.current_size_bytes == len(MODEL_BYTES) + 1
    assert not first.local_path.parent.exists()
    assert second.local_path.parent.exists()
    assert source_path.read_bytes() == source_bytes
    assert revisions.load_head(PROJECT_ID) == head

    recovered = ManagedCheckoutStore(
        root,
        store.lock_root,
        revisions,
        store.task_store,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )
    assert recovered.get(second.checkout_id).state is CheckoutState.OPEN
    closed_replay = recovered.open(first_key, source)
    assert closed_replay.to_wire_mapping() == closed.to_wire_mapping()
    assert recovered.close(first.checkout_id).to_wire_mapping() == closed.to_wire_mapping()


def test_closed_replay_precedes_other_open_aggregate_exhaustion(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, _head, root = checkout_rig
    source = HeadCheckoutSource(project_id=PROJECT_ID)
    closed_key = "checkout_open_" + "a" * 32
    second_key = "checkout_open_" + "b" * 32
    third_key = "checkout_open_" + "c" * 32
    monkeypatch.setattr(
        checkout_module,
        "MAX_CHECKOUT_TOTAL_BYTES",
        len(MODEL_BYTES) * 2,
    )

    closed_open = store.open(closed_key, source)
    closed = store.close(closed_open.checkout_id)
    second = store.open(second_key, source)
    third = store.open(third_key, source)
    replacement = second.local_path.with_suffix(".atomic-edit")
    replacement.write_bytes(MODEL_BYTES + b"x")
    os.chmod(replacement, 0o600)
    os.replace(replacement, second.local_path)
    second_bytes = second.local_path.read_bytes()
    third_bytes = third.local_path.read_bytes()

    restarted = ManagedCheckoutStore(
        root,
        store.lock_root,
        revisions,
        store.task_store,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )
    get_replay = restarted.get(closed.checkout_id)
    open_replay = restarted.open(closed_key, source)
    close_replay = restarted.close(closed.checkout_id)

    assert get_replay.to_wire_mapping() == closed.to_wire_mapping()
    assert open_replay.to_wire_mapping() == closed.to_wire_mapping()
    assert close_replay.to_wire_mapping() == closed.to_wire_mapping()
    with pytest.raises(CheckoutError) as raised:
        restarted.open(
            closed_key,
            HeadCheckoutSource(project_id="project_" + "9" * 32),
        )
    assert raised.value.code is CheckoutErrorCode.CONFLICT
    assert second.local_path.parent.exists()
    assert third.local_path.parent.exists()
    assert second.local_path.read_bytes() == second_bytes
    assert third.local_path.read_bytes() == third_bytes


def test_open_budget_admits_n_then_rejects_n_plus_one(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    monkeypatch.setattr(checkout_module, "MAX_OPEN_CHECKOUTS", 2)
    store.open("checkout_open_" + "a" * 32, HeadCheckoutSource(project_id=PROJECT_ID))
    store.open("checkout_open_" + "b" * 32, HeadCheckoutSource(project_id=PROJECT_ID))

    with pytest.raises(CheckoutError) as raised:
        store.open("checkout_open_" + "c" * 32, HeadCheckoutSource(project_id=PROJECT_ID))
    assert raised.value.code is CheckoutErrorCode.RESOURCE_EXHAUSTED


@pytest.mark.parametrize(
    ("maximum_temps", "expected"),
    [(2, None), (1, CheckoutErrorCode.RESOURCE_EXHAUSTED)],
)
def test_temp_budget_reserves_n_then_rejects_n_plus_one(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
    maximum_temps: int,
    expected: CheckoutErrorCode | None,
) -> None:
    store, _revisions, _head, root = checkout_rig
    abandoned = root / (".checkout_" + "f" * 32 + ".tmp")
    abandoned.mkdir(mode=0o700)
    os.chmod(abandoned, 0o700)
    monkeypatch.setattr(checkout_module, "MAX_CHECKOUT_TEMP_ENTRIES", maximum_temps)

    if expected is None:
        descriptor = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
        assert descriptor.state is CheckoutState.OPEN
    else:
        with pytest.raises(CheckoutError) as raised:
            store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
        assert raised.value.code is expected


def test_tombstone_reservation_never_prevents_existing_close(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    monkeypatch.setattr(checkout_module, "MAX_CLOSED_TOMBSTONES", 2)
    first = store.open("checkout_open_" + "a" * 32, HeadCheckoutSource(project_id=PROJECT_ID))
    store.close(first.checkout_id)
    second = store.open("checkout_open_" + "b" * 32, HeadCheckoutSource(project_id=PROJECT_ID))

    # closed + open is now at the reservation ceiling, but the existing open can close.
    assert store.close(second.checkout_id).state is CheckoutState.CLOSED
    with pytest.raises(CheckoutError) as raised:
        store.open("checkout_open_" + "c" * 32, HeadCheckoutSource(project_id=PROJECT_ID))
    assert raised.value.code is CheckoutErrorCode.RESOURCE_EXHAUSTED


def test_key_first_replay_keeps_original_source_but_recomputes_advanced_head(
    checkout_rig,
) -> None:
    store, revisions, original_head, _root = checkout_rig
    first = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    advanced = _advance_head(revisions, original_head)
    assert advanced.revision_id != original_head.revision_id

    replay = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    assert replay.checkout_id == first.checkout_id
    assert replay.source.revision_id == original_head.revision_id
    assert replay.source_head == original_head
    assert replay.source_liveness.value == "stale"
    assert store.get(first.checkout_id).source_liveness.value == "stale"
    fresh = store.open(
        "checkout_open_" + "7" * 32,
        HeadCheckoutSource(project_id=PROJECT_ID),
    )
    assert fresh.checkout_id != first.checkout_id
    assert fresh.source_head == advanced
    assert fresh.source.revision_id == advanced.revision_id
    assert fresh.source_liveness.value == "live"


def test_head_generation_advance_is_stale_even_if_revision_identity_repeats(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, original_head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    repeated = ProjectHead(
        project_id=PROJECT_ID,
        generation=original_head.generation + 1,
        revision_id=original_head.revision_id,
        manifest_sha256=original_head.manifest_sha256,
    )
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_head",
        lambda _self, _project_id: repeated,
    )

    observed = store.get(opened.checkout_id)

    assert observed.source_head == original_head
    assert observed.source_liveness.value == "stale"


def test_live_head_liveness_uses_a_bounded_revision_validation_budget(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, _head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    original = revisions_module._validate_revision_content
    calls = 0

    def counted(revision_fd, root_device, revision):
        nonlocal calls
        calls += 1
        return original(revision_fd, root_device, revision)

    monkeypatch.setattr(revisions_module, "_validate_revision_content", counted)
    observed = store.get(opened.checkout_id)

    assert observed.source_liveness.value == "live"
    assert calls == 2


def test_source_replacement_between_observation_and_copy_cannot_rebind_checkout(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    original = ManagedCheckoutStore._publish_open
    replaced = False

    def replace_before_copy(
        self,
        root_fd,
        open_key,
        intent,
        resolved,
        model_path,
        source_head,
        expected_source_binding,
    ):
        nonlocal replaced
        replacement = model_path.parent / ".same-bytes-replacement.FCStd"
        replacement.write_bytes(model_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, model_path)
        replaced = True
        return original(
            self,
            root_fd,
            open_key,
            intent,
            resolved,
            model_path,
            source_head,
            expected_source_binding,
        )

    monkeypatch.setattr(ManagedCheckoutStore, "_publish_open", replace_before_copy)

    with pytest.raises(CheckoutError) as raised:
        store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))

    assert replaced is True
    assert raised.value.code is CheckoutErrorCode.INTEGRITY_FAILURE


def test_closed_checkout_recomputes_liveness_after_restart(
    checkout_rig,
) -> None:
    store, revisions, original_head, root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    closed = store.close(opened.checkout_id)
    assert closed.source_liveness.value == "live"
    _advance_head(revisions, original_head)
    restarted = ManagedCheckoutStore(
        root,
        store.lock_root,
        revisions,
        store.task_store,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )

    observed = restarted.get(opened.checkout_id)

    assert observed.state is CheckoutState.CLOSED
    assert observed.source_head == original_head
    assert observed.source_liveness.value == "stale"
    with pytest.raises(CheckoutError) as raised:
        restarted.require_live(opened.checkout_id)
    assert raised.value.code is CheckoutErrorCode.CONFLICT


@pytest.mark.parametrize(
    "store_failure",
    [
        RevisionStoreError(RevisionStoreErrorCode.NOT_FOUND),
        RevisionStoreError(RevisionStoreErrorCode.CORRUPT_RECORD),
        OSError("unavailable"),
    ],
)
def test_head_store_uncertainty_projects_recovery_without_hiding_historical_bytes(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
    store_failure: Exception,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    historical = opened.local_path.read_bytes()

    def fail(_self, _project_id, _revision_id):
        raise store_failure

    monkeypatch.setattr(LocalRevisionStore, "observe_model_source", fail)
    observed = store.get(opened.checkout_id)

    assert observed.source_liveness.value == "recovery_required"
    assert observed.local_path.read_bytes() == historical
    with pytest.raises(CheckoutError) as raised:
        store.require_live(opened.checkout_id)
    assert raised.value.code.value == "recovery_required"


def test_legacy_checkout_without_full_head_binding_fails_liveness_closed(
    checkout_rig,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    metadata = opened.local_path.parent / "metadata.json"
    value = checkout_module._strict_json(metadata.read_bytes())
    value.pop("checksum")
    value.pop("source_head")
    binding = value.pop("source_binding")
    value.update(
        {
            "source_device": binding["dev"],
            "source_inode": binding["ino"],
            "source_size": binding["size"],
            "source_mtime_ns": binding["mtime_ns"],
        }
    )
    value["schema_version"] = 1
    metadata.write_bytes(
        checkout_module._encode_record(
            value,
            checkout_module._RECORD_DOMAIN,
        )
    )
    os.chmod(metadata, 0o600)

    observed = store.get(opened.checkout_id)

    assert observed.source_head is None
    assert observed.source_liveness.value == "recovery_required"
    closed = store.close(opened.checkout_id)
    assert closed.state is CheckoutState.CLOSED
    assert closed.source_liveness.value == "recovery_required"
    assert store.get(opened.checkout_id).source_liveness.value == "recovery_required"


@pytest.mark.parametrize(
    "replacement",
    ["same_bytes", "in_place", "unlink", "symlink", "hardlink"],
)
def test_authoritative_source_replacement_projects_recovery_and_still_closes(
    checkout_rig,
    replacement: str,
) -> None:
    store, revisions, head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    source = revisions.revision_model_path(PROJECT_ID, head.revision_id)
    checkout_bytes = opened.local_path.read_bytes()
    external = source.parent.parent / f"{replacement}.FCStd"
    external.write_bytes(source.read_bytes())
    os.chmod(external, 0o600)

    if replacement == "same_bytes":
        os.replace(external, source)
    elif replacement == "in_place":
        source.write_bytes(b"changed immutable source")
        os.chmod(source, 0o600)
    elif replacement == "unlink":
        source.unlink()
    elif replacement == "symlink":
        source.unlink()
        source.symlink_to(external)
    else:
        source.unlink()
        os.link(external, source)

    observed = store.get(opened.checkout_id)

    assert observed.source_liveness.value == "recovery_required"
    assert observed.local_path.read_bytes() == checkout_bytes
    closed = store.close(opened.checkout_id)
    assert closed.state is CheckoutState.CLOSED
    assert closed.source_liveness.value == "recovery_required"


def test_source_binding_uses_ctime_not_only_legacy_identity_tuple(
    checkout_rig,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    metadata = opened.local_path.parent / "metadata.json"
    value = checkout_module._strict_json(metadata.read_bytes())
    value.pop("checksum")
    value["source_binding"]["ctime_ns"] += 1
    metadata.write_bytes(
        checkout_module._encode_record(
            value,
            checkout_module._RECORD_DOMAIN_V2,
        )
    )
    os.chmod(metadata, 0o600)

    observed = store.get(opened.checkout_id)

    assert observed.source_binding.ctime_ns != opened.source_binding.ctime_ns
    assert observed.source_liveness.value == "recovery_required"


def test_revision_directory_swap_during_validation_never_returns_live(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    source = revisions.revision_model_path(PROJECT_ID, head.revision_id)
    original = revisions_module._validate_revision_content
    moved = source.parent.with_name(source.parent.name + ".detached")
    swapped = False

    def swap_after_read(revision_fd, root_device, revision):
        nonlocal swapped
        result = original(revision_fd, root_device, revision)
        if revision.id == head.revision_id and not swapped:
            swapped = True
            source.parent.rename(moved)
            source.parent.mkdir(mode=0o700)
            os.chmod(source.parent, 0o700)
            forged = source.parent / "model.FCStd"
            forged.write_bytes(b"invalid replacement")
            os.chmod(forged, 0o600)
        return result

    monkeypatch.setattr(revisions_module, "_validate_revision_content", swap_after_read)
    observed = store.get(opened.checkout_id)

    assert swapped is True
    assert observed.source_liveness.value == "recovery_required"


def test_head_advance_during_source_validation_never_returns_live(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    original = ManagedCheckoutStore._validate_bound_source
    advanced = None

    def advance_after_validation(self, source, source_head, source_binding):
        nonlocal advanced
        result = original(self, source, source_head, source_binding)
        if self is store and advanced is None:
            advanced = _advance_head(revisions, head)
        return result

    monkeypatch.setattr(
        ManagedCheckoutStore,
        "_validate_bound_source",
        advance_after_validation,
    )
    observed = store.get(opened.checkout_id)

    assert advanced is not None
    assert observed.source_liveness.value == "stale"


def test_legacy_closed_tombstone_recomputes_as_recovery(
    checkout_rig,
) -> None:
    store, _revisions, _head, root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    store.close(opened.checkout_id)
    tombstone = root / f"closed_{opened.checkout_id}.json"
    value = checkout_module._strict_json(tombstone.read_bytes())
    value.pop("checksum")
    value.pop("source_head")
    value.pop("source_binding")
    value["schema_version"] = 1
    tombstone.write_bytes(
        checkout_module._encode_record(
            value,
            checkout_module._TOMBSTONE_DOMAIN,
        )
    )
    os.chmod(tombstone, 0o600)

    observed = store.get(opened.checkout_id)

    assert observed.state is CheckoutState.CLOSED
    assert observed.source_head is None
    assert observed.source_liveness.value == "recovery_required"


def test_live_draft_guard_rejects_dirty_and_non_draft_checkouts(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, draft, stored, _revision = _draft_source(revisions, head)
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: stored)
    opened = store.open(OPEN_KEY, source)

    accepted = store.require_acceptance(
        opened.checkout_id,
        task_id=source.task_id,
        draft_id=source.draft_id,
        expected_generation=source.expected_generation,
    )
    assert accepted.source_liveness.value == "live"
    replacement = opened.local_path.with_suffix(".dirty")
    replacement.write_bytes(b"manual unverified edit")
    os.chmod(replacement, 0o600)
    os.replace(replacement, opened.local_path)
    with pytest.raises(CheckoutError) as dirty:
        store.require_acceptance(
            opened.checkout_id,
            task_id=source.task_id,
            draft_id=draft.id,
            expected_generation=source.expected_generation,
        )
    assert dirty.value.code is CheckoutErrorCode.CONFLICT

    head_checkout = store.open(
        "checkout_open_" + "8" * 32,
        HeadCheckoutSource(project_id=PROJECT_ID),
    )
    with pytest.raises(CheckoutError) as wrong_kind:
        store.require_acceptance(
            head_checkout.checkout_id,
            task_id=source.task_id,
            draft_id=draft.id,
            expected_generation=source.expected_generation,
        )
    assert wrong_kind.value.code is CheckoutErrorCode.CONFLICT


def test_empty_project_first_draft_remains_live_across_restart_then_becomes_stale(
    empty_checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, root = empty_checkout_rig
    assert revisions.load_revision(PROJECT_ID, head.revision_id).model is None
    source, _draft, stored, _revision = _draft_source(revisions, head)
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: stored)

    opened = store.open(OPEN_KEY, source)

    assert opened.source_liveness is CheckoutSourceLiveness.LIVE
    restarted = ManagedCheckoutStore(
        root,
        store.lock_root,
        revisions,
        store.task_store,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )
    assert restarted.get(opened.checkout_id).source_liveness is CheckoutSourceLiveness.LIVE
    _advance_head(revisions, head)
    assert restarted.get(opened.checkout_id).source_liveness is CheckoutSourceLiveness.STALE


@pytest.mark.parametrize("terminal", [TaskStatus.SUCCEEDED, TaskStatus.REJECTED])
def test_draft_accept_or_reject_revokes_existing_checkout_and_blocks_guard(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
    terminal: TaskStatus,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, draft, stored, _revision = _draft_source(revisions, head)
    current = {"stored": stored}
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: current["stored"])
    opened = store.open(OPEN_KEY, source)
    current["stored"] = SimpleNamespace(
        generation=stored.generation + 1,
        task_run=SimpleNamespace(
            id=source.task_id,
            project_id=PROJECT_ID,
            base_revision=head.revision_id,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
            status=terminal,
            draft=draft,
        ),
    )

    observed = store.get(opened.checkout_id)

    assert observed.source_liveness.value == "revoked"
    with pytest.raises(CheckoutError) as raised:
        store.require_acceptance(
            opened.checkout_id,
            task_id=source.task_id,
            draft_id=source.draft_id,
            expected_generation=source.expected_generation,
        )
    assert raised.value.code is CheckoutErrorCode.CONFLICT
    closed = store.close(opened.checkout_id)
    assert closed.state is CheckoutState.CLOSED
    assert closed.source_liveness.value == "revoked"


def test_draft_authority_change_during_source_validation_never_returns_live(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, draft, stored, _revision = _draft_source(revisions, head)
    current = {"stored": stored}
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: current["stored"])
    opened = store.open(OPEN_KEY, source)
    original = ManagedCheckoutStore._validate_bound_source

    def reject_after_validation(self, resolved, source_head, source_binding):
        result = original(self, resolved, source_head, source_binding)
        if self is store and current["stored"] is stored:
            current["stored"] = SimpleNamespace(
                generation=stored.generation + 1,
                task_run=SimpleNamespace(
                    id=source.task_id,
                    project_id=PROJECT_ID,
                    base_revision=head.revision_id,
                    review_policy=ReviewPolicy.REQUIRE_REVIEW,
                    status=TaskStatus.REJECTED,
                    draft=draft,
                ),
            )
        return result

    monkeypatch.setattr(
        ManagedCheckoutStore,
        "_validate_bound_source",
        reject_after_validation,
    )
    observed = store.get(opened.checkout_id)

    assert observed.source_liveness.value == "revoked"
    with pytest.raises(CheckoutError) as raised:
        store.require_acceptance(
            opened.checkout_id,
            task_id=source.task_id,
            draft_id=source.draft_id,
            expected_generation=source.expected_generation,
        )
    assert raised.value.code is CheckoutErrorCode.CONFLICT


def test_draft_task_store_uncertainty_projects_recovery_and_blocks_acceptance(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, _draft, stored, _revision = _draft_source(revisions, head)
    current = {"stored": stored}

    def load(_self, _task_id):
        value = current["stored"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(TaskRunStore, "load", load)
    opened = store.open(OPEN_KEY, source)
    current["stored"] = TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)

    observed = store.get(opened.checkout_id)

    assert observed.source_liveness.value == "recovery_required"
    with pytest.raises(CheckoutError) as raised:
        store.require_acceptance(
            opened.checkout_id,
            task_id=source.task_id,
            draft_id=source.draft_id,
            expected_generation=source.expected_generation,
        )
    assert raised.value.code is CheckoutErrorCode.RECOVERY_REQUIRED


def test_draft_generation_change_cannot_revive_old_checkout(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, draft, stored, _revision = _draft_source(revisions, head)
    current = {"stored": stored}
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: current["stored"])
    opened = store.open(OPEN_KEY, source)
    current["stored"] = SimpleNamespace(
        generation=stored.generation + 2,
        task_run=SimpleNamespace(
            id=source.task_id,
            project_id=PROJECT_ID,
            base_revision=head.revision_id,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
            status=TaskStatus.AWAITING_USER_REVIEW,
            draft=draft,
        ),
    )

    assert store.get(opened.checkout_id).source_liveness.value == "revoked"


def test_draft_base_head_advance_is_stale_and_blocks_acceptance(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, _draft, stored, _revision = _draft_source(revisions, head)
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: stored)
    opened = store.open(OPEN_KEY, source)
    _advance_head(revisions, head)

    observed = store.get(opened.checkout_id)

    assert observed.source_liveness.value == "stale"
    with pytest.raises(CheckoutError) as raised:
        store.require_acceptance(
            opened.checkout_id,
            task_id=source.task_id,
            draft_id=source.draft_id,
            expected_generation=source.expected_generation,
        )
    assert raised.value.code is CheckoutErrorCode.CONFLICT


def test_non_review_authoritative_draft_cannot_open_with_current_generation(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, draft, stored, _revision = _draft_source(revisions, head)
    rejected = SimpleNamespace(
        generation=stored.generation + 1,
        task_run=SimpleNamespace(
            id=source.task_id,
            project_id=PROJECT_ID,
            base_revision=head.revision_id,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
            status=TaskStatus.REJECTED,
            draft=draft,
        ),
    )
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: rejected)

    with pytest.raises(CheckoutError) as raised:
        store.open(
            OPEN_KEY,
            DraftCheckoutSource(
                task_id=source.task_id,
                draft_id=source.draft_id,
                expected_generation=rejected.generation,
            ),
        )

    assert raised.value.code is CheckoutErrorCode.CONFLICT


def test_draft_checkout_edit_and_close_never_mutate_source_or_head(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, revisions, head, _root = checkout_rig
    source, draft, stored, revision = _draft_source(revisions, head)
    task_id = source.task_id
    monkeypatch.setattr(TaskRunStore, "load", lambda _self, _task_id: stored)
    source_path = revisions.revision_model_path(PROJECT_ID, revision.id)
    immutable_bytes = source_path.read_bytes()

    opened = store.open(
        OPEN_KEY,
        source,
    )
    replacement = opened.local_path.with_suffix(".edit")
    replacement.write_bytes(b"manual draft edit")
    os.chmod(replacement, 0o600)
    os.replace(replacement, opened.local_path)
    closed = store.close(opened.checkout_id)

    assert closed.dirty is True
    assert closed.source.task_id == task_id
    assert closed.source.task_generation == source.expected_generation
    assert closed.source_liveness.value == "live"
    assert source_path.read_bytes() == immutable_bytes
    assert revisions.load_head(PROJECT_ID) == head


def test_metadata_tamper_and_directory_escape_fail_closed(checkout_rig) -> None:
    store, _revisions, _head, root = checkout_rig
    first = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    metadata = first.local_path.parent / "metadata.json"
    raw = metadata.read_bytes()
    metadata.write_bytes(raw.replace(b'"created_ns":', b'"created_ns":1,"forged":'))
    os.chmod(metadata, 0o600)
    with pytest.raises(CheckoutError) as raised:
        store.get(first.checkout_id)
    assert raised.value.code is CheckoutErrorCode.INTEGRITY_FAILURE

    # A fresh store/root proves the checkout directory itself cannot escape confinement.
    second_key = "checkout_open_" + "d" * 32
    # Restore authentic metadata so close/open scans can proceed.
    metadata.write_bytes(raw)
    os.chmod(metadata, 0o600)
    second = store.open(second_key, HeadCheckoutSource(project_id=PROJECT_ID))
    escaped = root.parent / "escaped"
    escaped.mkdir(mode=0o700)
    moved = root.parent / (second.checkout_id + ".moved")
    second.local_path.parent.rename(moved)
    (root / second.checkout_id).symlink_to(escaped, target_is_directory=True)
    with pytest.raises(CheckoutError) as raised:
        store.get(second.checkout_id)
    assert raised.value.code is CheckoutErrorCode.INTEGRITY_FAILURE


def _fork_context():
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        pytest.skip("bounded checkout process tests require fork")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is unavailable")
def test_fork_while_checkout_lock_is_held_fails_closed_without_child_deadlock(
    tmp_path: Path,
) -> None:
    lock_root = tmp_path / "locks"
    _mkdir(lock_root)
    lock = CheckoutMutationLock(SafeRoot(lock_root))
    entered = threading.Event()
    release = threading.Event()

    def hold_in_parent_thread() -> None:
        with lock.hold():
            entered.set()
            assert release.wait(timeout=5)

    holder = threading.Thread(target=hold_in_parent_thread)
    holder.start()
    assert entered.wait(timeout=2)
    child = os.fork()
    if child == 0:
        signal.alarm(2)
        try:
            CheckoutMutationLock(SafeRoot(lock_root))
        except StorageFailure:
            os._exit(0)
        os._exit(1)

    try:
        _, child_status = os.waitpid(child, 0)
    finally:
        release.set()
        holder.join(timeout=3)
    assert not holder.is_alive()
    assert os.waitstatus_to_exitcode(child_status) == 0


def test_two_process_same_key_publishes_one_descriptor(checkout_rig) -> None:
    _store, _revisions, _head, root = checkout_rig
    context = _fork_context()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_open_worker,
            args=(str(root.parent), OPEN_KEY, 8, start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
    observed = [results.get(timeout=2) for _ in processes]
    assert [kind for kind, _ in observed] == ["ok", "ok"]
    assert len({value for _, value in observed}) == 1


def test_two_process_different_keys_do_not_over_admit(checkout_rig) -> None:
    _store, _revisions, _head, root = checkout_rig
    context = _fork_context()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_open_worker,
            args=(str(root.parent), "checkout_open_" + digit * 32, 1, start, results),
        )
        for digit in ("a", "b")
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(10)
        assert not process.is_alive()
        assert process.exitcode == 0
    observed = sorted(results.get(timeout=2) for _ in processes)
    assert [kind for kind, _value in observed] == ["error", "ok"]
    assert observed[0][1] == "resource_exhausted"
    assert observed[1][1].startswith("checkout_") and len(observed[1][1]) == 41


@pytest.mark.parametrize(
    "stage",
    ["after_file_fsync", "after_copy", "after_metadata_publish", "after_directory_fsync"],
)
def test_open_process_death_before_publication_converges(
    checkout_rig,
    stage: str,
) -> None:
    store, _revisions, _head, root = checkout_rig
    context = _fork_context()
    process = context.Process(target=_crash_open_worker, args=(str(root.parent), OPEN_KEY, stage))
    process.start()
    process.join(10)
    assert not process.is_alive()
    assert process.exitcode == 91

    descriptor = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    assert descriptor.state is CheckoutState.OPEN
    assert descriptor.local_path.read_bytes() == MODEL_BYTES


def test_open_response_loss_after_publication_replays(checkout_rig) -> None:
    store, _revisions, _head, root = checkout_rig
    context = _fork_context()
    process = context.Process(
        target=_crash_open_worker,
        args=(str(root.parent), OPEN_KEY, "after_checkout_publish"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 91

    first = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    replay = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    assert replay.checkout_id == first.checkout_id


@pytest.mark.parametrize(
    "stage",
    ["after_tombstone_publish", "after_checkout_entry_unlink", "after_checkout_delete"],
)
def test_close_process_death_converges_to_terminal_tombstone(
    checkout_rig,
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, root = checkout_rig
    monkeypatch.setattr(checkout_module, "MAX_OPEN_CHECKOUTS", 1)
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    context = _fork_context()
    process = context.Process(
        target=_crash_close_worker,
        args=(str(root.parent), opened.checkout_id, stage),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 92

    closed = store.get(opened.checkout_id)
    assert closed.state is CheckoutState.CLOSED
    assert not opened.local_path.parent.exists()
    replacement = store.open(
        "checkout_open_" + "e" * 32,
        HeadCheckoutSource(project_id=PROJECT_ID),
    )
    assert replacement.state is CheckoutState.OPEN
    assert store.close(opened.checkout_id).to_wire_mapping() == closed.to_wire_mapping()


def test_post_tombstone_delete_failure_preserves_closed_descriptor(checkout_rig) -> None:
    store, revisions, head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    extra = opened.local_path.parent / "unexpected.txt"
    extra.write_bytes(b"must not be guessed away")
    os.chmod(extra, 0o600)

    with pytest.raises(CheckoutError) as raised:
        store.close(opened.checkout_id)
    assert raised.value.code is CheckoutErrorCode.CLEANUP_REQUIRED
    assert raised.value.descriptor is not None
    assert raised.value.descriptor.state is CheckoutState.CLOSED
    assert raised.value.descriptor.source_liveness.value == "live"
    _advance_head(revisions, head)

    with pytest.raises(CheckoutError) as replay:
        store.get(opened.checkout_id)
    assert replay.value.code is CheckoutErrorCode.CLEANUP_REQUIRED
    assert replay.value.descriptor.to_wire_mapping() == raised.value.descriptor.to_wire_mapping()
    assert replay.value.descriptor.source_liveness.value == "stale"

    extra.unlink()
    closed = store.get(opened.checkout_id)
    assert closed.state is CheckoutState.CLOSED
    assert closed.source_liveness.value == "stale"
    assert not opened.local_path.parent.exists()


def test_tombstone_publish_uncertainty_returns_fresh_liveness(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, _root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    original = SafeRoot.atomic_write
    injected = False

    def publish_then_fail(self, parent_fd, name, raw, *, token):
        nonlocal injected
        result = original(self, parent_fd, name, raw, token=token)
        if self is store._root and name.startswith("closed_checkout_") and not injected:
            injected = True
            raise StorageFailure("post-publication failure")
        return result

    monkeypatch.setattr(SafeRoot, "atomic_write", publish_then_fail)
    with pytest.raises(CheckoutError) as raised:
        store.close(opened.checkout_id)

    assert injected is True
    assert raised.value.code is CheckoutErrorCode.DURABILITY_UNCERTAIN
    assert raised.value.descriptor is not None
    assert raised.value.descriptor.state is CheckoutState.CLOSED
    assert raised.value.descriptor.source_liveness.value == "live"
    observed = store.get(opened.checkout_id)
    assert observed.state is CheckoutState.CLOSED
    assert observed.source_liveness.value == "live"


def test_expired_crash_close_pair_is_deleted_never_resurrected(
    checkout_rig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _revisions, _head, root = checkout_rig
    opened = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    monkeypatch.setattr(checkout_module, "CLOSED_TOMBSTONE_TTL_SECONDS", -1)
    context = _fork_context()
    process = context.Process(
        target=_crash_close_worker,
        args=(str(root.parent), opened.checkout_id, "after_tombstone_publish"),
    )
    process.start()
    process.join(10)
    assert process.exitcode == 92

    with pytest.raises(CheckoutError) as raised:
        store.get(opened.checkout_id)
    assert raised.value.code is CheckoutErrorCode.NOT_FOUND
    assert not opened.local_path.parent.exists()
    replay_after_retention = store.open(OPEN_KEY, HeadCheckoutSource(project_id=PROJECT_ID))
    assert replay_after_retention.checkout_id != opened.checkout_id
