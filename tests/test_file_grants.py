from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

import vibecad.interaction.file_grants as file_grants_module
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreRootTrust,
)
from vibecad.interaction.checkouts import (
    CheckoutStoreRootTrust,
    HeadCheckoutSource,
    ManagedCheckoutStore,
)
from vibecad.interaction.file_grants import (
    FILE_GRANT_TTL_MS,
    MAX_ACTIVE_FILE_GRANTS,
    MAX_FILE_GRANTS_PER_SESSION,
    MAX_RECENT_FILE_GRANT_IDS,
    FileGrantBroker,
    FileGrantError,
    FileGrantErrorCode,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.store import (
    TaskRunStore,
    TaskStoreRootTrust,
)

PROJECT_ID = "project_" + "1" * 32
DAEMON_ID = "daemon_" + "2" * 32
SESSION_ONE = "session_" + "3" * 32
SESSION_TWO = "session_" + "4" * 32


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


class _TokenSource:
    def __init__(self) -> None:
        self._number = 0

    def __call__(self) -> str:
        self._number += 1
        return f"{self._number:032x}"


@pytest.fixture
def checkout_store(tmp_path: Path) -> ManagedCheckoutStore:
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
    content = b"FCStd grant sample"
    source.write_bytes(content)
    os.chmod(source, 0o600)
    with leases.acquire_project_write(PROJECT_ID) as lease:
        revisions.import_trusted_fcstd(
            PROJECT_ID,
            source,
            hashlib.sha256(content).hexdigest(),
            len(content),
            lease,
        )
    return ManagedCheckoutStore(
        checkout_root,
        lock_root,
        revisions,
        tasks,
        trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
    )


def _opened(store: ManagedCheckoutStore, number: int = 1):
    return store.open(
        f"checkout_open_{number:032x}",
        HeadCheckoutSource(project_id=PROJECT_ID),
    )


def _broker(*, clock: Callable[[], int] | None = None, token_hex=None) -> FileGrantBroker:
    return FileGrantBroker(
        DAEMON_ID,
        clock_ns=(lambda: 1_000) if clock is None else clock,
        token_hex=_TokenSource() if token_hex is None else token_hex,
    )


def _mint(
    broker: FileGrantBroker,
    store: ManagedCheckoutStore,
    checkout_id: str,
    *,
    session_id: str = SESSION_ONE,
):
    return broker.mint(
        session_id=session_id,
        checkout_id=checkout_id,
        capture=lambda: store.capture_live_file(checkout_id),
    )


def _claim(
    broker: FileGrantBroker,
    store: ManagedCheckoutStore,
    grant_id: str,
    *,
    session_id: str = SESSION_ONE,
):
    return broker.claim(
        session_id=session_id,
        grant_id=grant_id,
        require_same=store.require_same_live_file,
    )


def _unavailable(operation: Callable[[], object]) -> None:
    with pytest.raises(FileGrantError) as raised:
        operation()
    assert raised.value.code is FileGrantErrorCode.UNAVAILABLE


def test_mint_claim_has_exact_contract_mapping_and_is_one_shot(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()

    snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)

    assert descriptor.to_mapping() == {
        "schema_version": 1,
        "grant_id": "file_grant_" + "0" * 31 + "1",
        "purpose": "open_managed_checkout",
        "expires_in_ms": FILE_GRANT_TTL_MS,
    }
    assert snapshot.path == opened.local_path
    assert broker.active_grants == 1

    claim = _claim(broker, checkout_store, descriptor.grant_id)

    assert claim.to_mapping() == {
        "schema_version": 1,
        "grant_id": descriptor.grant_id,
        "checkout_id": opened.checkout_id,
        "purpose": "open_managed_checkout",
        "local_path": str(opened.local_path),
        "current_model_sha256": opened.current_model_sha256,
        "current_size_bytes": opened.current_size_bytes,
    }
    assert broker.active_grants == 0
    _unavailable(lambda: _claim(broker, checkout_store, descriptor.grant_id))


def test_cross_session_claim_does_not_consume_owner_grant(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()
    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)

    _unavailable(
        lambda: _claim(
            broker,
            checkout_store,
            descriptor.grant_id,
            session_id=SESSION_TWO,
        )
    )
    assert broker.active_grants == 1

    owner_claim = _claim(broker, checkout_store, descriptor.grant_id)

    assert owner_claim.checkout_id == opened.checkout_id
    assert broker.active_grants == 0


def test_claim_expires_at_the_exact_monotonic_ttl_boundary(
    checkout_store: ManagedCheckoutStore,
) -> None:
    now = {"value": 1_000}
    broker = _broker(clock=lambda: now["value"])
    opened = _opened(checkout_store)
    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)

    now["value"] += FILE_GRANT_TTL_MS * 1_000_000 - 1
    assert _claim(broker, checkout_store, descriptor.grant_id).checkout_id == opened.checkout_id

    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)
    now["value"] += FILE_GRANT_TTL_MS * 1_000_000

    _unavailable(lambda: _claim(broker, checkout_store, descriptor.grant_id))
    assert broker.active_grants == 0


def test_claim_ttl_starts_after_the_live_snapshot_capture_completes(
    checkout_store: ManagedCheckoutStore,
) -> None:
    now = {"value": 1_000}
    broker = _broker(clock=lambda: now["value"])
    opened = _opened(checkout_store)

    def slow_capture():
        now["value"] += 20_000_000_000
        return checkout_store.capture_live_file(opened.checkout_id)

    _snapshot, descriptor = broker.mint(
        session_id=SESSION_ONE,
        checkout_id=opened.checkout_id,
        capture=slow_capture,
    )
    now["value"] += FILE_GRANT_TTL_MS * 1_000_000 - 1

    assert _claim(broker, checkout_store, descriptor.grant_id).checkout_id == opened.checkout_id


def test_mint_replaces_unclaimed_grant_for_same_session_checkout(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()
    _first_snapshot, first = _mint(broker, checkout_store, opened.checkout_id)
    _second_snapshot, second = _mint(broker, checkout_store, opened.checkout_id)

    assert first.grant_id != second.grant_id
    assert broker.active_grants == 1
    _unavailable(lambda: _claim(broker, checkout_store, first.grant_id))
    assert _claim(broker, checkout_store, second.grant_id).checkout_id == opened.checkout_id


@pytest.mark.parametrize("action", ["session", "checkout", "broker"])
def test_close_paths_revoke_outstanding_grants(
    checkout_store: ManagedCheckoutStore,
    action: str,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()
    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)

    if action == "session":
        assert broker.close_session(SESSION_ONE) == 1
        _unavailable(lambda: _claim(broker, checkout_store, descriptor.grant_id))
    elif action == "checkout":
        assert broker.revoke_checkout(opened.checkout_id) == 1
        _unavailable(lambda: _claim(broker, checkout_store, descriptor.grant_id))
    else:
        broker.close()
        with pytest.raises(FileGrantError) as raised:
            _claim(broker, checkout_store, descriptor.grant_id)
        assert raised.value.code is FileGrantErrorCode.INVALID_STATE
    assert broker.active_grants == (0 if action != "broker" else 0)


def test_only_one_concurrent_claim_can_claim_a_grant(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()
    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def blocked_require_same(snapshot):
        entered.set()
        assert release.wait(5)
        return checkout_store.require_same_live_file(snapshot)

    def first_claim() -> None:
        try:
            results.append(
                broker.claim(
                    session_id=SESSION_ONE,
                    grant_id=descriptor.grant_id,
                    require_same=blocked_require_same,
                )
            )
        except BaseException as error:  # pragma: no cover - assertion below exercises it
            results.append(error)

    worker = threading.Thread(target=first_claim)
    worker.start()
    assert entered.wait(5)
    try:
        _unavailable(lambda: _claim(broker, checkout_store, descriptor.grant_id))
    finally:
        release.set()
        worker.join(5)

    assert len(results) == 1
    assert not isinstance(results[0], BaseException)
    assert results[0].checkout_id == opened.checkout_id
    assert broker.active_grants == 0


def test_claim_rejected_if_grant_is_revoked_while_liveness_check_blocks(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()
    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)
    entered = threading.Event()
    release = threading.Event()
    results: list[object] = []

    def blocked_require_same(snapshot):
        entered.set()
        assert release.wait(5)
        return checkout_store.require_same_live_file(snapshot)

    def claiming() -> None:
        try:
            results.append(
                broker.claim(
                    session_id=SESSION_ONE,
                    grant_id=descriptor.grant_id,
                    require_same=blocked_require_same,
                )
            )
        except BaseException as error:
            results.append(error)

    worker = threading.Thread(target=claiming)
    worker.start()
    assert entered.wait(5)
    assert broker.revoke_checkout(opened.checkout_id) == 1
    release.set()
    worker.join(5)

    assert len(results) == 1
    assert isinstance(results[0], FileGrantError)
    assert results[0].code is FileGrantErrorCode.UNAVAILABLE
    assert broker.active_grants == 0


def test_capture_failure_releases_reserved_grant_and_keeps_broker_usable(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()

    with pytest.raises(RuntimeError, match="capture failed"):
        broker.mint(
            session_id=SESSION_ONE,
            checkout_id=opened.checkout_id,
            capture=lambda: (_ for _ in ()).throw(RuntimeError("capture failed")),
        )

    assert broker.active_grants == 0
    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)
    assert _claim(broker, checkout_store, descriptor.grant_id).checkout_id == opened.checkout_id


def test_invalid_identifiers_are_rejected_before_callbacks_run(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = _broker()
    called = False

    def capture():
        nonlocal called
        called = True
        return checkout_store.capture_live_file(opened.checkout_id)

    with pytest.raises(FileGrantError) as raised:
        broker.mint(session_id="bad", checkout_id=opened.checkout_id, capture=capture)
    assert raised.value.code is FileGrantErrorCode.INVALID_INPUT
    assert called is False

    with pytest.raises(FileGrantError) as raised:
        broker.claim(
            session_id=SESSION_ONE,
            grant_id="file_grant_bad",
            require_same=checkout_store.require_same_live_file,
        )
    assert raised.value.code is FileGrantErrorCode.INVALID_INPUT


def test_token_collision_retries_without_reusing_an_issued_grant_id(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    tokens = iter(["a" * 32, "a" * 32, "b" * 32])
    broker = _broker(token_hex=lambda: next(tokens))
    _snapshot, first = _mint(broker, checkout_store, opened.checkout_id)
    _snapshot, second = _mint(broker, checkout_store, opened.checkout_id)

    assert first.grant_id == "file_grant_" + "a" * 32
    assert second.grant_id == "file_grant_" + "b" * 32


def test_recent_replay_window_is_bounded_without_a_lifetime_mint_limit(
    checkout_store: ManagedCheckoutStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MAX_RECENT_FILE_GRANT_IDS == 65_536
    monkeypatch.setattr(file_grants_module, "MAX_RECENT_FILE_GRANT_IDS", 2)
    opened = _opened(checkout_store)
    broker = _broker()

    grants = []
    for _ in range(4):
        _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)
        grants.append(descriptor.grant_id)
        _claim(broker, checkout_store, descriptor.grant_id)

    assert len(set(grants)) == 4
    assert broker.active_grants == 0


def test_recent_window_never_reuses_an_active_grant_identifier(
    checkout_store: ManagedCheckoutStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(file_grants_module, "MAX_RECENT_FILE_GRANT_IDS", 2)
    tokens = iter(["a" * 32, "b" * 32, "c" * 32, "a" * 32, "d" * 32])
    broker = _broker(token_hex=lambda: next(tokens))
    opened = _opened(checkout_store)
    _snapshot, active = _mint(broker, checkout_store, opened.checkout_id)

    for _ in range(2):
        _snapshot, transient = _mint(
            broker,
            checkout_store,
            opened.checkout_id,
            session_id=SESSION_TWO,
        )
        _claim(
            broker,
            checkout_store,
            transient.grant_id,
            session_id=SESSION_TWO,
        )

    _snapshot, newest = _mint(
        broker,
        checkout_store,
        opened.checkout_id,
        session_id=SESSION_TWO,
    )

    assert active.grant_id == "file_grant_" + "a" * 32
    assert newest.grant_id == "file_grant_" + "d" * 32
    assert _claim(broker, checkout_store, active.grant_id).grant_id == active.grant_id


def test_default_token_source_mints_a_128_bit_grant_identifier(
    checkout_store: ManagedCheckoutStore,
) -> None:
    opened = _opened(checkout_store)
    broker = FileGrantBroker(DAEMON_ID)

    _snapshot, descriptor = _mint(broker, checkout_store, opened.checkout_id)

    assert descriptor.grant_id.startswith("file_grant_")
    assert len(descriptor.grant_id) == len("file_grant_") + 32


def test_per_session_and_daemon_capacity_limits_are_enforced(
    checkout_store: ManagedCheckoutStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MAX_FILE_GRANTS_PER_SESSION == 8
    assert MAX_ACTIVE_FILE_GRANTS == 64
    monkeypatch.setattr(file_grants_module, "MAX_FILE_GRANTS_PER_SESSION", 2)
    monkeypatch.setattr(file_grants_module, "MAX_ACTIVE_FILE_GRANTS", 3)
    broker = _broker()
    opened = tuple(_opened(checkout_store, number) for number in range(1, 5))
    _mint(broker, checkout_store, opened[0].checkout_id)
    _mint(broker, checkout_store, opened[1].checkout_id)

    with pytest.raises(FileGrantError) as raised:
        _mint(broker, checkout_store, opened[2].checkout_id)
    assert raised.value.code is FileGrantErrorCode.RESOURCE_EXHAUSTED

    _mint(
        broker,
        checkout_store,
        opened[2].checkout_id,
        session_id=SESSION_TWO,
    )
    with pytest.raises(FileGrantError) as raised:
        broker.mint(
            session_id="session_" + "5" * 32,
            checkout_id=opened[3].checkout_id,
            capture=lambda: checkout_store.capture_live_file(opened[3].checkout_id),
        )
    assert raised.value.code is FileGrantErrorCode.RESOURCE_EXHAUSTED
