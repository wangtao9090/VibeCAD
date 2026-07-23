from __future__ import annotations

import ctypes
import multiprocessing
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import vibecad.daemon.local_identity as local_identity
from vibecad.daemon.local_identity import (
    LocalIdentityError,
    LocalIdentityErrorCode,
    PeerIdentity,
    darwin_peer_identity,
    require_same_user_peer,
)
from vibecad.interaction.storage import SafeRoot, StorageFailure
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ResourceLeaseManager,
)

DARWIN_ONLY = pytest.mark.skipif(sys.platform != "darwin", reason="macOS peer-euid POC")
DAEMON_AUTHORITY = "vibecad.kernel-daemon.authority.v1"


def _lease_contender(
    lock_root: str,
    ready,
    start,
    results,
    release,
) -> None:
    manager = ResourceLeaseManager(
        Path(lock_root),
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    ready.put(os.getpid())
    if not start.wait(10):
        results.put(("timeout", os.getpid()))
        return
    try:
        lease = manager.acquire(DAEMON_AUTHORITY)
    except LeaseError as exc:
        results.put((exc.code.value, os.getpid()))
        return
    results.put(("won", os.getpid()))
    release.wait(10)
    lease.release(owner_token=lease.owner_token)


def _short_private_root() -> Path:
    root = Path(
        tempfile.mkdtemp(
            prefix="vibecad-c07-",
            dir=os.path.realpath(tempfile.gettempdir()),
        )
    )
    root.chmod(0o700)
    return root


def _endpoint_identity(root: SafeRoot, name: str) -> tuple[int, int]:
    root_fd = root.open()
    try:
        value = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if (
            not stat.S_ISSOCK(value.st_mode)
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_nlink != 1
            or value.st_dev != root.identity[0]
        ):
            raise StorageFailure("local daemon endpoint is unsafe")
        return value.st_dev, value.st_ino
    finally:
        os.close(root_fd)


def _require_endpoint_identity(
    root: SafeRoot,
    name: str,
    expected: tuple[int, int],
) -> None:
    if _endpoint_identity(root, name) != expected:
        raise StorageFailure("local daemon endpoint changed")


def _authority_race(lock_root: Path) -> list[tuple[str, int]]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    release = context.Event()
    processes = [
        context.Process(
            target=_lease_contender,
            args=(str(lock_root), ready, start, results, release),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        assert len({ready.get(timeout=10), ready.get(timeout=10)}) == 2
        start.set()
        outcomes = [results.get(timeout=10), results.get(timeout=10)]
        release.set()
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        return outcomes
    finally:
        release.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)


@DARWIN_ONLY
def test_darwin_getpeereid_observes_both_connected_unix_socket_peers() -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        expected = PeerIdentity(euid=os.geteuid(), egid=os.getegid())
        for _ in range(32):
            assert darwin_peer_identity(left) == expected
            assert darwin_peer_identity(right) == expected
            assert require_same_user_peer(left) == expected
            assert require_same_user_peer(right) == expected
    finally:
        left.close()
        right.close()


@DARWIN_ONLY
def test_darwin_getpeereid_accept_flow_observes_both_real_connection_peers() -> None:
    root = _short_private_root()
    endpoint = root / "peer.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    accepted: socket.socket | None = None
    try:
        listener.bind(str(endpoint))
        listener.listen(1)
        client.connect(str(endpoint))
        accepted, _address = listener.accept()
        expected = PeerIdentity(euid=os.geteuid(), egid=os.getegid())
        assert darwin_peer_identity(accepted) == expected
        assert darwin_peer_identity(client) == expected
        assert require_same_user_peer(accepted) == expected
    finally:
        if accepted is not None:
            accepted.close()
        client.close()
        listener.close()
        shutil.rmtree(root, ignore_errors=True)


@DARWIN_ONLY
def test_peer_identity_rejects_listener_unconnected_closed_and_wrong_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    pair = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    closed = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    closed.close()
    try:
        for candidate in (listener, closed):
            with pytest.raises(LocalIdentityError) as raised:
                darwin_peer_identity(candidate)
            assert raised.value.code is LocalIdentityErrorCode.INVALID_SOCKET
            assert not hasattr(raised.value, "native_error")

        monkeypatch.setattr(local_identity.os, "geteuid", lambda: os.getuid() + 1)
        with pytest.raises(LocalIdentityError) as raised:
            require_same_user_peer(pair[0])
        assert raised.value.code is LocalIdentityErrorCode.DIFFERENT_USER
    finally:
        listener.close()
        pair[0].close()
        pair[1].close()


@DARWIN_ONLY
def test_peer_identity_missing_symbol_and_native_failure_are_closed_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    loader = local_identity._load_getpeereid

    def native_failure(_fd, _euid, _egid) -> int:
        return -1

    try:
        monkeypatch.setattr(local_identity, "_load_getpeereid", lambda: native_failure)
        with pytest.raises(LocalIdentityError) as raised:
            darwin_peer_identity(left)
        assert raised.value.code is LocalIdentityErrorCode.PEER_UNAVAILABLE
    finally:
        left.close()
        right.close()

    monkeypatch.setattr(local_identity, "_load_getpeereid", loader)
    loader.cache_clear()

    class MissingSymbol:
        pass

    monkeypatch.setattr(local_identity.ctypes, "CDLL", lambda *_args, **_kwargs: MissingSymbol())
    with pytest.raises(LocalIdentityError) as raised:
        loader()
    assert raised.value.code is LocalIdentityErrorCode.UNSUPPORTED_PLATFORM
    loader.cache_clear()


@DARWIN_ONLY
def test_peer_identity_rejects_socket_state_change_after_native_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)

    def close_after_observation(_fd, euid_pointer, egid_pointer) -> int:
        ctypes.cast(
            euid_pointer,
            ctypes.POINTER(ctypes.c_uint32),
        ).contents.value = os.geteuid()
        ctypes.cast(
            egid_pointer,
            ctypes.POINTER(ctypes.c_uint32),
        ).contents.value = os.getegid()
        left.close()
        return 0

    try:
        monkeypatch.setattr(
            local_identity,
            "_load_getpeereid",
            lambda: close_after_observation,
        )
        with pytest.raises(LocalIdentityError) as raised:
            darwin_peer_identity(left)
        assert raised.value.code is LocalIdentityErrorCode.INVALID_SOCKET
    finally:
        left.close()
        right.close()


def test_peer_identity_has_no_non_darwin_or_secret_only_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        monkeypatch.setattr(local_identity.sys, "platform", "linux")
        with pytest.raises(LocalIdentityError) as raised:
            darwin_peer_identity(left)
        assert raised.value.code is LocalIdentityErrorCode.UNSUPPORTED_PLATFORM
    finally:
        left.close()
        right.close()


@DARWIN_ONLY
def test_endpoint_poc_pins_path_identity_not_listener_inode_and_rejects_rebind() -> None:
    root_path = _short_private_root()
    endpoint = root_path / "kernel.sock"
    first = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    second: socket.socket | None = None
    try:
        previous_umask = os.umask(0o177)
        try:
            first.bind(str(endpoint))
        finally:
            os.umask(previous_umask)
        endpoint.chmod(0o600)
        first.listen(1)
        root = SafeRoot(root_path)
        captured = _endpoint_identity(root, endpoint.name)
        descriptor = os.fstat(first.fileno())
        assert captured != (descriptor.st_dev, descriptor.st_ino)
        _require_endpoint_identity(root, endpoint.name, captured)

        endpoint.unlink()
        second = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second.bind(str(endpoint))
        endpoint.chmod(0o600)
        second.listen(1)
        assert _endpoint_identity(root, endpoint.name) != captured
        with pytest.raises(StorageFailure):
            _require_endpoint_identity(root, endpoint.name, captured)
    finally:
        first.close()
        if second is not None:
            second.close()
        shutil.rmtree(root_path, ignore_errors=True)


@DARWIN_ONLY
def test_endpoint_poc_rejects_private_root_rename_and_recreation() -> None:
    root_path = _short_private_root()
    moved = root_path.with_name(root_path.name + "-moved")
    root = SafeRoot(root_path)
    try:
        root_path.rename(moved)
        root_path.mkdir(mode=0o700)
        with pytest.raises(StorageFailure):
            root.open()
    finally:
        shutil.rmtree(root_path, ignore_errors=True)
        shutil.rmtree(moved, ignore_errors=True)


@DARWIN_ONLY
def test_daemon_authority_lease_has_one_concurrent_winner_and_recovers() -> None:
    root = _short_private_root()
    lock_root = root / "locks"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    initializer = ResourceLeaseManager(
        lock_root,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )
    initialized = initializer.acquire(DAEMON_AUTHORITY)
    initialized.release(owner_token=initialized.owner_token)
    try:
        outcomes = _authority_race(lock_root)
        assert sorted(outcome for outcome, _pid in outcomes) == ["contended", "won"]

        manager = ResourceLeaseManager(
            lock_root,
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        lease = manager.acquire(DAEMON_AUTHORITY)
        lease.release(owner_token=lease.owner_token)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@DARWIN_ONLY
def test_fresh_daemon_authority_race_has_exactly_one_owner_and_fails_closed() -> None:
    root = _short_private_root()
    lock_root = root / "locks"
    lock_root.mkdir(mode=0o700)
    lock_root.chmod(0o700)
    try:
        outcomes = _authority_race(lock_root)
        assert sum(outcome == "won" for outcome, _pid in outcomes) == 1
        loser = next(outcome for outcome, _pid in outcomes if outcome != "won")
        assert loser in {
            LeaseErrorCode.CONTENDED.value,
            LeaseErrorCode.IO_ERROR.value,
        }

        manager = ResourceLeaseManager(
            lock_root,
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        lease = manager.acquire(DAEMON_AUTHORITY)
        lease.release(owner_token=lease.owner_token)
    finally:
        shutil.rmtree(root, ignore_errors=True)
