from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import multiprocessing
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

import vibecad.daemon as daemon_api
import vibecad.daemon.client as daemon_client
import vibecad.daemon.local_identity as local_identity
import vibecad.daemon.service as daemon_service
from vibecad.application.agent import AgentApplication
from vibecad.daemon.local_identity import (
    LocalIdentityError,
    LocalIdentityErrorCode,
    PeerIdentity,
    darwin_peer_identity,
    require_same_user_peer,
)
from vibecad.execution.revisions import ProjectHead, RevisionSourceBinding
from vibecad.interaction import protocol_v2
from vibecad.interaction.checkouts import (
    CheckoutDescriptor,
    CheckoutFileSnapshot,
    CheckoutSourceLiveness,
    CheckoutState,
    ResolvedCheckoutSource,
    _CheckoutEntryBinding,
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


class _FakeCheckoutDescriptor:
    def __init__(self, checkout_id: str) -> None:
        self.checkout_id = checkout_id

    def to_local_mapping(self) -> dict[str, object]:
        return {
            "checkout_id": self.checkout_id,
            "open_key": "checkout_open_" + "1" * 32,
            "state": "open",
            "source": {
                "kind": "head",
                "project_id": "project_" + "2" * 32,
                "revision_id": "revision_" + "3" * 32,
                "manifest_sha256": "4" * 64,
                "model_sha256": "5" * 64,
                "size_bytes": 12,
                "task_id": None,
                "draft_id": None,
                "task_generation": None,
            },
            "dirty": False,
            "initial_model_sha256": "5" * 64,
            "current_model_sha256": "5" * 64,
            "current_size_bytes": 12,
            "source_head": {
                "schema_version": 1,
                "project_id": "project_" + "2" * 32,
                "generation": 0,
                "revision_id": "revision_" + "3" * 32,
                "manifest_sha256": "4" * 64,
            },
            "source_liveness": "live",
        }


class _FakeDaemonApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.closed = 0
        self.checkout_id = "checkout_" + "6" * 32
        self._checkout_root = Path(tempfile.mkdtemp(prefix="vibecad-c10-checkout-"))
        self._checkout_root.chmod(0o700)
        checkout_directory = self._checkout_root / self.checkout_id
        checkout_directory.mkdir(mode=0o700)
        self._model_path = checkout_directory / "model.FCStd"
        self._model_path.write_bytes(b"fake managed FreeCAD model\n")
        self._model_path.chmod(0o600)
        self._snapshot = self._make_snapshot()

    @staticmethod
    def _binding(path: Path) -> _CheckoutEntryBinding:
        value = path.stat()
        return _CheckoutEntryBinding(
            dev=value.st_dev,
            ino=value.st_ino,
            mode=value.st_mode,
            uid=value.st_uid,
            gid=value.st_gid,
            nlink=value.st_nlink,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
        )

    def _make_snapshot(self) -> CheckoutFileSnapshot:
        model = self._model_path.read_bytes()
        digest = hashlib.sha256(model).hexdigest()
        model_stat = self._model_path.stat()
        source = ResolvedCheckoutSource(
            kind="head",
            project_id="project_" + "2" * 32,
            revision_id="revision_" + "3" * 32,
            manifest_sha256="4" * 64,
            model_sha256=digest,
            size_bytes=len(model),
        )
        head = ProjectHead(
            project_id=source.project_id,
            generation=0,
            revision_id=source.revision_id,
            manifest_sha256=source.manifest_sha256,
        )
        source_binding = RevisionSourceBinding(
            dev=model_stat.st_dev,
            ino=model_stat.st_ino,
            mode=model_stat.st_mode,
            uid=model_stat.st_uid,
            nlink=model_stat.st_nlink,
            size=model_stat.st_size,
            mtime_ns=model_stat.st_mtime_ns,
            ctime_ns=model_stat.st_ctime_ns,
        )
        descriptor = CheckoutDescriptor(
            checkout_id=self.checkout_id,
            open_key="checkout_open_" + "1" * 32,
            state=CheckoutState.OPEN,
            dirty=False,
            source=source,
            initial_model_sha256=digest,
            current_model_sha256=digest,
            current_size_bytes=len(model),
            local_path=self._model_path,
            source_head=head,
            source_binding=source_binding,
            source_liveness=CheckoutSourceLiveness.LIVE,
        )
        return CheckoutFileSnapshot(
            descriptor=descriptor,
            root_binding=self._binding(self._checkout_root),
            directory_binding=self._binding(self._model_path.parent),
            file_binding=self._binding(self._model_path),
            model_sha256=digest,
            size_bytes=len(model),
            path=self._model_path,
        )

    def get_capabilities_request(self, request: object) -> dict[str, object]:
        self.calls.append(("get_capabilities", request))
        return {
            "schema_version": 1,
            "ok": True,
            "result": {"registry_schema_version": 1, "operations": []},
            "error": None,
        }

    def create_project_request(self, request: object) -> dict[str, object]:
        self.calls.append(("create_project", request))
        return {
            "schema_version": 1,
            "ok": True,
            "result": {"kind": "empty"},
            "error": None,
        }

    def open_checkout(self, *, open_key: str, source: object) -> _FakeCheckoutDescriptor:
        self.calls.append(("checkout.open", (open_key, source)))
        return _FakeCheckoutDescriptor(self.checkout_id)

    def get_checkout(self, *, checkout_id: str) -> _FakeCheckoutDescriptor:
        self.calls.append(("checkout.get", checkout_id))
        return _FakeCheckoutDescriptor(checkout_id)

    def close_checkout(self, *, checkout_id: str) -> _FakeCheckoutDescriptor:
        self.calls.append(("checkout.close", checkout_id))
        return _FakeCheckoutDescriptor(checkout_id)

    def capture_checkout_file(self, *, checkout_id: str) -> CheckoutFileSnapshot:
        self.calls.append(("checkout.capture", checkout_id))
        assert checkout_id == self.checkout_id
        return self._snapshot

    def require_same_checkout_file(
        self,
        snapshot: CheckoutFileSnapshot,
    ) -> CheckoutFileSnapshot:
        self.calls.append(("checkout.require_same", snapshot.descriptor.checkout_id))
        assert snapshot == self._snapshot
        return self._snapshot

    def invoke_direct_operation_request(
        self,
        operation: object,
        request: object,
    ) -> dict[str, object]:
        self.calls.append((str(operation), request))
        return {
            "schema_version": 1,
            "ok": True,
            "result": {"operation": operation},
            "error": None,
        }

    def close(self) -> None:
        self.closed += 1
        shutil.rmtree(self._checkout_root, ignore_errors=True)


class _BlockingCheckoutCloseApplication(_FakeDaemonApplication):
    def __init__(self) -> None:
        super().__init__()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()

    def close_checkout(self, *, checkout_id: str) -> _FakeCheckoutDescriptor:
        self.close_entered.set()
        assert self.close_release.wait(5)
        return super().close_checkout(checkout_id=checkout_id)


def _c09_root() -> tuple[Path, Path]:
    base = _short_private_root()
    return base, base / "data"


def _fake_daemon_factory(log: list[object]):
    def factory(*, layout, lease_manager):
        application = _FakeDaemonApplication()
        log.append((layout, lease_manager, application))
        return application

    return factory


def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_checkout_close_revokes_a_grant_minted_during_the_store_close() -> None:
    application = _BlockingCheckoutCloseApplication()
    facade = daemon_api.LocalKernelFacade(
        application,
        daemon_id="daemon_" + "1" * 32,
    )
    close_results: list[object] = []

    def close_checkout() -> None:
        try:
            close_results.append(facade._checkout_close({"checkout_id": application.checkout_id}))
        except BaseException as error:
            close_results.append(error)

    worker = threading.Thread(target=close_checkout)
    worker.start()
    assert application.close_entered.wait(5)
    try:
        opened = facade._checkout_open(
            "session_" + "2" * 32,
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {
                    "kind": "head",
                    "project_id": "project_" + "2" * 32,
                },
            },
        )
        grant_id = opened["file_grant"]["grant_id"]
        assert facade._file_grants.active_grants == 1
    finally:
        application.close_release.set()
        worker.join(5)

    try:
        assert not worker.is_alive()
        assert len(close_results) == 1
        assert not isinstance(close_results[0], BaseException)
        assert facade._file_grants.active_grants == 0
        with pytest.raises(protocol_v2.V2ProtocolError) as raised:
            facade._file_grant_claim(
                "session_" + "2" * 32,
                {"grant_id": grant_id},
            )
        assert raised.value.code is protocol_v2.V2ErrorCode.UNAVAILABLE
    finally:
        facade.close()
        application.close()


def _crash_daemon_process(data_root: str, ready) -> None:
    factories: list[object] = []
    daemon = daemon_api.LocalKernelDaemon.start(
        data_root=Path(data_root),
        application_factory=_fake_daemon_factory(factories),
    )
    secret = (daemon.run_root / daemon_api.DAEMON_SECRET_NAME).read_bytes()
    ready.send((daemon.daemon_id, hashlib.sha256(secret).hexdigest()))
    ready.close()
    while True:
        time.sleep(60)


def _recv_v2_frame(connection: socket.socket) -> bytes:
    header = b""
    while len(header) < protocol_v2.V2_FRAME_HEADER_BYTES:
        chunk = connection.recv(protocol_v2.V2_FRAME_HEADER_BYTES - len(header))
        if not chunk:
            raise EOFError
        header += chunk
    size = int.from_bytes(header, "big")
    payload = b""
    while len(payload) < size:
        chunk = connection.recv(size - len(payload))
        if not chunk:
            raise EOFError
        payload += chunk
    return payload


def test_c09_public_contract_is_closed_and_contains_no_grant_surface() -> None:
    assert daemon_api.DAEMON_AUTHORITY == DAEMON_AUTHORITY
    assert daemon_api.DAEMON_DIRECTORY_NAME == "daemon"
    assert daemon_api.DAEMON_ENDPOINT_NAME == "kernel.sock"
    assert daemon_api.DAEMON_RECEIPT_NAME == "receipt.json"
    assert daemon_api.DAEMON_SECRET_NAME == "boot-secret"
    assert daemon_api.ALLOWED_APPLICATION_OPERATIONS == frozenset(
        {
            "accept_draft",
            "cancel_task",
            "compare_revisions",
            "create_box",
            "create_cylinder",
            "create_project",
            "create_task",
            "export_task_artifacts",
            "get_artifact_manifest",
            "get_capabilities",
            "get_project",
            "get_task",
            "get_task_events",
            "inspect_model",
            "list_projects",
            "list_revisions",
            "list_tasks",
            "modify_parameter",
            "move_part",
            "reject_draft",
            "resume_task",
            "revert_project",
            "rotate_part",
            "submit_model_program",
        }
    )
    assert not any("grant" in name or "path" in name for name in daemon_api.__all__)


@pytest.mark.parametrize(
    "value",
    [None, 7, Path("relative"), Path("/"), Path("/tmp/../escape")],
)
def test_daemon_run_root_rejects_invalid_or_ambiguous_roots(value: object) -> None:
    with pytest.raises(daemon_api.DaemonError) as raised:
        daemon_api.daemon_run_root(value)
    assert raised.value.code is daemon_api.DaemonErrorCode.INVALID_ROOT


@DARWIN_ONLY
def test_daemon_start_publishes_private_bound_state_and_close_cleans_exact_entries() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        assert daemon.state is daemon_api.LocalKernelState.RUNNING
        assert len(factories) == 1
        layout, lease_manager, application = factories[0]
        assert layout.root == data_root
        assert lease_manager._root_identity == layout.identity_for(layout.locks)
        run_root = daemon.run_root
        assert run_root == data_root / "daemon"
        assert stat.S_IMODE(run_root.lstat().st_mode) == 0o700
        assert run_root.lstat().st_uid == os.geteuid()

        endpoint = run_root / daemon_api.DAEMON_ENDPOINT_NAME
        receipt_path = run_root / daemon_api.DAEMON_RECEIPT_NAME
        secret_path = run_root / daemon_api.DAEMON_SECRET_NAME
        endpoint_stat = endpoint.lstat()
        receipt_stat = receipt_path.lstat()
        secret_stat = secret_path.lstat()
        assert stat.S_ISSOCK(endpoint_stat.st_mode)
        assert stat.S_IMODE(endpoint_stat.st_mode) == 0o600
        assert endpoint_stat.st_uid == os.geteuid()
        assert endpoint_stat.st_nlink == 1
        for value in (receipt_stat, secret_stat):
            assert stat.S_ISREG(value.st_mode)
            assert stat.S_IMODE(value.st_mode) == 0o600
            assert value.st_uid == os.geteuid()
            assert value.st_nlink == 1
        secret = secret_path.read_bytes()
        raw_receipt = receipt_path.read_bytes()
        parsed = json.loads(raw_receipt)
        assert len(secret) == 32
        assert secret not in raw_receipt
        assert parsed["daemon_id"] == daemon.daemon_id
        assert parsed["secret_sha256"] == hashlib.sha256(secret).hexdigest()
        assert raw_receipt == json.dumps(
            parsed,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        assert daemon.receipt.endpoint.dev == endpoint_stat.st_dev
        assert daemon.receipt.endpoint.ino == endpoint_stat.st_ino
        assert daemon.receipt.run_root_dev == run_root.lstat().st_dev
        assert daemon.receipt.run_root_ino == run_root.lstat().st_ino

        daemon.close()
        assert daemon.state is daemon_api.LocalKernelState.CLOSED
        assert application.closed == 1
        assert not endpoint.exists()
        assert not receipt_path.exists()
        assert not secret_path.exists()
        daemon.close()
        assert application.closed == 1
    finally:
        if daemon is not None and daemon.state is not daemon_api.LocalKernelState.CLOSED:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_real_client_authenticates_and_claims_one_path_bound_checkout_grant() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    client = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        ping = client.call("kernel.ping", {})
        assert ping.error is None
        assert ping.result == {
            "schema_version": 1,
            "daemon_id": daemon.daemon_id,
            "status": "ready",
            "protocol": {"major": 2, "minor": 0},
        }
        capabilities = client.call(
            "application.call",
            {
                "operation": "get_capabilities",
                "request": {"schema_version": 1},
            },
        )
        assert capabilities.error is None
        assert capabilities.result["ok"] is True
        opened = client.call(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
        )
        assert opened.error is None
        assert opened.result["checkout_id"] == "checkout_" + "6" * 32
        assert "local_path" not in json.dumps(opened.result)
        grant = opened.result["file_grant"]
        assert set(grant) == {
            "schema_version",
            "grant_id",
            "purpose",
            "expires_in_ms",
        }
        assert grant["schema_version"] == 1
        assert grant["grant_id"].startswith("file_grant_")
        assert grant["purpose"] == "open_managed_checkout"
        assert grant["expires_in_ms"] == 30_000
        fetched = client.call(
            "checkout.get",
            {"checkout_id": "checkout_" + "6" * 32},
        )
        assert fetched.error is None
        assert "local_path" not in json.dumps(fetched.result)
        assert "file_grant" not in fetched.result
        claim = client.call(
            "file_grant.claim",
            {"grant_id": grant["grant_id"]},
        )
        application = factories[0][2]
        assert claim.error is None
        assert claim.result == {
            "schema_version": 1,
            "grant_id": grant["grant_id"],
            "checkout_id": application.checkout_id,
            "purpose": "open_managed_checkout",
            "local_path": str(application._model_path),
            "current_model_sha256": application._snapshot.model_sha256,
            "current_size_bytes": application._snapshot.size_bytes,
        }
        replay = client.call(
            "file_grant.claim",
            {"grant_id": grant["grant_id"]},
        )
        assert replay.result is None
        assert replay.error["code"] == "unavailable"
        closed = client.call(
            "checkout.close",
            {"checkout_id": "checkout_" + "6" * 32},
        )
        assert fetched.result["source_liveness"] == "live"
        assert closed.result["checkout_id"] == "checkout_" + "6" * 32
        assert "local_path" not in json.dumps(closed.result)
        assert "file_grant" not in closed.result
    finally:
        if client is not None:
            client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_file_grant_is_session_bound_without_consuming_the_owner_grant() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    owner = None
    other = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        owner = daemon_api.LocalKernelClient.connect(daemon.run_root)
        other = daemon_api.LocalKernelClient.connect(daemon.run_root)
        opened = owner.call(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
        )
        grant_id = opened.result["file_grant"]["grant_id"]
        rejected = other.call("file_grant.claim", {"grant_id": grant_id})
        assert rejected.result is None
        assert rejected.error["code"] == "unavailable"
        claimed = owner.call("file_grant.claim", {"grant_id": grant_id})
        assert claimed.error is None
        assert claimed.result["local_path"] == str(factories[0][2]._model_path)
    finally:
        if owner is not None:
            owner.close()
        if other is not None:
            other.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_default_daemon_claims_a_real_managed_checkout_file() -> None:
    base, data_root = _c09_root()
    project_id = "project_" + "7" * 32
    content = b"real managed checkout through the local daemon\n"
    seeded = None
    daemon = None
    client = None
    claimed_path = None
    try:
        source = base / "source.FCStd"
        source.write_bytes(content)
        source.chmod(0o600)
        seeded = AgentApplication.open(data_root=data_root)
        with seeded._lease_manager.acquire_project_write(project_id) as lease:
            seeded._revision_store.import_trusted_fcstd(
                project_id,
                source,
                hashlib.sha256(content).hexdigest(),
                len(content),
                lease,
            )
        seeded.close()
        seeded = None
        daemon = daemon_api.LocalKernelDaemon.start(data_root=data_root)
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        opened = client.call(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "7" * 32,
                "source": {"kind": "head", "project_id": project_id},
            },
        )
        assert opened.error is None
        assert "local_path" not in json.dumps(opened.result)
        claim = client.call(
            "file_grant.claim",
            {"grant_id": opened.result["file_grant"]["grant_id"]},
        )
        assert claim.error is None
        claimed_path = Path(claim.result["local_path"])
        assert claimed_path.read_bytes() == content
        assert claimed_path.parent.parent == data_root / "checkouts"
        assert stat.S_IMODE(claimed_path.stat().st_mode) == 0o600

        closed = client.call(
            "checkout.close",
            {"checkout_id": opened.result["checkout_id"]},
        )
        assert closed.error is None
        assert not claimed_path.exists()
    finally:
        if seeded is not None:
            seeded.close()
        if client is not None:
            client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_file_grant_disconnect_and_checkout_close_revoke_unclaimed_grants() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    owner = None
    verifier = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        owner = daemon_api.LocalKernelClient.connect(daemon.run_root)
        opened = owner.call(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
        )
        disconnected_grant = opened.result["file_grant"]["grant_id"]
        owner.close()
        owner = None
        _wait_until(lambda: daemon.active_connections == 0)
        assert daemon._facade._file_grants.active_grants == 0

        verifier = daemon_api.LocalKernelClient.connect(daemon.run_root)
        rejected = verifier.call(
            "file_grant.claim",
            {"grant_id": disconnected_grant},
        )
        assert rejected.result is None
        assert rejected.error["code"] == "unavailable"

        opened = verifier.call(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "2" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
        )
        close_response = verifier.call(
            "checkout.close",
            {"checkout_id": opened.result["checkout_id"]},
        )
        assert close_response.error is None
        assert "local_path" not in json.dumps(close_response.result)
        rejected = verifier.call(
            "file_grant.claim",
            {"grant_id": opened.result["file_grant"]["grant_id"]},
        )
        assert rejected.result is None
        assert rejected.error["code"] == "unavailable"
    finally:
        if owner is not None:
            owner.close()
        if verifier is not None:
            verifier.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_daemon_close_clears_its_unclaimed_file_grants() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    client = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        opened = client.call(
            "checkout.open",
            {
                "open_key": "checkout_open_" + "1" * 32,
                "source": {"kind": "head", "project_id": "project_" + "2" * 32},
            },
        )
        assert opened.error is None
        assert daemon._facade._file_grants.active_grants == 1
        daemon.close()
        assert daemon.state is daemon_api.LocalKernelState.CLOSED
        assert daemon._facade._file_grants.active_grants == 0
        assert factories[0][2].closed == 1
    finally:
        if client is not None:
            client.close()
        if daemon is not None and daemon.state is not daemon_api.LocalKernelState.CLOSED:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_second_checkout_open_rotates_the_prior_session_grant() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    client = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        params = {
            "open_key": "checkout_open_" + "1" * 32,
            "source": {"kind": "head", "project_id": "project_" + "2" * 32},
        }
        first = client.call("checkout.open", params)
        second = client.call("checkout.open", params)
        first_grant = first.result["file_grant"]["grant_id"]
        second_grant = second.result["file_grant"]["grant_id"]
        assert first_grant != second_grant
        rejected = client.call("file_grant.claim", {"grant_id": first_grant})
        assert rejected.result is None
        assert rejected.error["code"] == "unavailable"
        claimed = client.call("file_grant.claim", {"grant_id": second_grant})
        assert claimed.error is None
        assert claimed.result["grant_id"] == second_grant
    finally:
        if client is not None:
            client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_import_source_path_and_unknown_operation_fail_before_application() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    client = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        application = factories[0][2]
        imported = client.call(
            "application.call",
            {
                "operation": "create_project",
                "request": {
                    "schema_version": 1,
                    "create_key": "project_create_" + "1" * 32,
                    "kind": "import_fcstd",
                    "source_path": "/tmp/user.FCStd",
                },
            },
        )
        empty_with_path = client.call(
            "application.call",
            {
                "operation": "create_project",
                "request": {
                    "schema_version": 1,
                    "create_key": "project_create_" + "2" * 32,
                    "kind": "empty",
                    "source_path": "/tmp/user.FCStd",
                },
            },
        )
        reflection = client.call(
            "application.call",
            {
                "operation": "__getattribute__",
                "request": {},
            },
        )
        unknown = client.call(
            "application.call",
            {
                "operation": "unknown_operation",
                "request": {},
            },
        )
        assert imported.error["code"] == "unavailable"
        assert empty_with_path.error["code"] == "unavailable"
        assert reflection.error["code"] == "invalid_request"
        assert unknown.error["code"] == "unknown_method"
        assert application.calls == []
    finally:
        if client is not None:
            client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_second_daemon_is_contended_before_application_factory_and_restart_recovers() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    first = None
    second = None
    try:
        factory = _fake_daemon_factory(factories)
        first = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=factory,
        )
        with pytest.raises(daemon_api.DaemonError) as raised:
            daemon_api.LocalKernelDaemon.start(
                data_root=data_root,
                application_factory=factory,
            )
        assert raised.value.code is daemon_api.DaemonErrorCode.CONTENDED
        assert len(factories) == 1
        first.close()
        second = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=factory,
        )
        assert second.daemon_id != first.daemon_id
        assert len(factories) == 2
    finally:
        if second is not None:
            second.close()
        if first is not None and first.state is not daemon_api.LocalKernelState.CLOSED:
            first.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_crash_leftovers_are_recovered_before_a_new_application_is_opened() -> None:
    base, data_root = _c09_root()
    context = multiprocessing.get_context("spawn")
    parent_ready, child_ready = context.Pipe(duplex=False)
    process = context.Process(
        target=_crash_daemon_process,
        args=(str(data_root), child_ready),
    )
    replacement = None
    factories: list[object] = []
    try:
        process.start()
        child_ready.close()
        assert parent_ready.poll(10)
        first_daemon_id, first_secret_sha256 = parent_ready.recv()
        process.terminate()
        process.join(timeout=10)
        assert process.exitcode is not None

        replacement = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        replacement_secret = (replacement.run_root / daemon_api.DAEMON_SECRET_NAME).read_bytes()
        assert replacement.daemon_id != first_daemon_id
        assert hashlib.sha256(replacement_secret).hexdigest() != first_secret_sha256
        assert len(factories) == 1
    finally:
        parent_ready.close()
        child_ready.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=5)
        if replacement is not None:
            replacement.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_receipt_publish_failure_cleans_incomplete_state_and_releases_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    replacement = None
    original = SafeRoot.atomic_write
    failed = False

    def fail_receipt_once(
        self: SafeRoot,
        root_fd: int,
        name: str,
        raw: bytes,
        *,
        token: str,
    ) -> None:
        nonlocal failed
        if name == daemon_api.DAEMON_RECEIPT_NAME and not failed:
            failed = True
            raise StorageFailure("injected receipt publish failure")
        original(self, root_fd, name, raw, token=token)

    try:
        monkeypatch.setattr(SafeRoot, "atomic_write", fail_receipt_once)
        with pytest.raises(daemon_api.DaemonError):
            daemon_api.LocalKernelDaemon.start(
                data_root=data_root,
                application_factory=_fake_daemon_factory(factories),
            )
        assert failed is True
        assert len(factories) == 1
        assert factories[0][2].closed == 1
        run_root = data_root / daemon_api.DAEMON_DIRECTORY_NAME
        assert run_root.is_dir()
        assert tuple(run_root.iterdir()) == ()

        monkeypatch.setattr(SafeRoot, "atomic_write", original)
        replacement = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        assert replacement.state is daemon_api.LocalKernelState.RUNNING
        assert len(factories) == 2
    finally:
        if replacement is not None:
            replacement.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_authority_rebind_fails_service_before_application_dispatch() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = daemon_api.LocalKernelDaemon.start(
        data_root=data_root,
        application_factory=_fake_daemon_factory(factories),
    )
    application = factories[0][2]
    authority_path = data_root / "locks" / f"{daemon._authority.resource_key}.lock"
    try:
        authority_path.unlink()
        descriptor = os.open(
            authority_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        _wait_until(lambda: daemon.state is daemon_api.LocalKernelState.FAILED)
        assert application.calls == []
        with pytest.raises(daemon_api.DaemonError) as raised:
            daemon.close()
        assert raised.value.code is daemon_api.DaemonErrorCode.RECOVERY_REQUIRED
        assert daemon._authority.released is False
    finally:
        if daemon._authority.released is False:
            daemon._authority.release(owner_token=daemon._authority.owner_token)
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_run_root_replacement_fails_service_without_deleting_replacement() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = daemon_api.LocalKernelDaemon.start(
        data_root=data_root,
        application_factory=_fake_daemon_factory(factories),
    )
    moved = daemon.run_root.with_name("daemon-moved")
    try:
        daemon.run_root.rename(moved)
        daemon.run_root.mkdir(mode=0o700)
        _wait_until(lambda: daemon.state is daemon_api.LocalKernelState.FAILED)
        assert factories[0][2].calls == []
        with pytest.raises(daemon_api.DaemonError) as raised:
            daemon.close()
        assert raised.value.code is daemon_api.DaemonErrorCode.RECOVERY_REQUIRED
        assert daemon.run_root.is_dir()
        assert tuple(daemon.run_root.iterdir()) == ()
        assert (moved / daemon_api.DAEMON_RECEIPT_NAME).is_file()
        assert daemon._authority.released is False
    finally:
        if daemon._authority.released is False:
            daemon._authority.release(owner_token=daemon._authority.owner_token)
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
@pytest.mark.parametrize("entry_name", ["kernel.sock", "receipt.json", "boot-secret"])
def test_live_entry_replacement_fails_before_dispatch_and_preserves_replacement(
    entry_name: str,
) -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = daemon_api.LocalKernelDaemon.start(
        data_root=data_root,
        application_factory=_fake_daemon_factory(factories),
    )
    client = daemon_api.LocalKernelClient.connect(daemon.run_root)
    replacement_listener = None
    path = daemon.run_root / entry_name
    try:
        if entry_name == daemon_api.DAEMON_ENDPOINT_NAME:
            path.unlink()
            replacement_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement_listener.bind(str(path))
            path.chmod(0o600)
            replacement_listener.listen(1)
        else:
            raw = path.read_bytes()
            replacement = daemon.run_root / f".{entry_name}.replacement"
            descriptor = os.open(
                replacement,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            try:
                assert os.write(descriptor, raw) == len(raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(replacement, path)
        replacement_identity = (path.lstat().st_dev, path.lstat().st_ino)

        with pytest.raises(daemon_api.DaemonError):
            client.call("kernel.ping", {})
        _wait_until(lambda: daemon.state is daemon_api.LocalKernelState.FAILED)
        assert factories[0][2].calls == []
        with pytest.raises(daemon_api.DaemonError) as raised:
            daemon.close()
        assert raised.value.code is daemon_api.DaemonErrorCode.RECOVERY_REQUIRED
        assert (path.lstat().st_dev, path.lstat().st_ino) == replacement_identity
        assert daemon._authority.released is False
    finally:
        client.close()
        if replacement_listener is not None:
            replacement_listener.close()
        if daemon._authority.released is False:
            daemon._authority.release(owner_token=daemon._authority.owner_token)
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_peer_identity_is_checked_before_protocol_state_is_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    observations: list[str] = []
    constructions: list[object] = []
    daemon = None
    connection = None

    def reject_peer(_connection: object) -> None:
        observations.append("peer")
        raise LocalIdentityError(LocalIdentityErrorCode.DIFFERENT_USER)

    class UnexpectedProtocol:
        def __init__(self, *_args, **_kwargs) -> None:
            constructions.append(object())

    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        monkeypatch.setattr(daemon_service, "require_same_user_peer", reject_peer)
        monkeypatch.setattr(daemon_service, "V2ServerConnection", UnexpectedProtocol)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2)
        connection.connect(str(daemon.run_root / daemon_api.DAEMON_ENDPOINT_NAME))
        _wait_until(lambda: observations == ["peer"])
        assert connection.recv(1) == b""
        assert constructions == []
        assert daemon.state is daemon_api.LocalKernelState.RUNNING
    finally:
        if connection is not None:
            connection.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_connection_limit_rejects_ninth_peer_and_recovers_capacity() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    held: list[socket.socket] = []
    client = None
    ninth = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        endpoint = str(daemon.run_root / daemon_api.DAEMON_ENDPOINT_NAME)
        for _ in range(protocol_v2.MAX_V2_CONNECTIONS):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(2)
            connection.connect(endpoint)
            _recv_v2_frame(connection)
            held.append(connection)
        _wait_until(lambda: daemon.active_connections == protocol_v2.MAX_V2_CONNECTIONS)

        ninth = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ninth.settimeout(2)
        ninth.connect(endpoint)
        assert ninth.recv(1) == b""

        held.pop().close()
        _wait_until(lambda: daemon.active_connections == protocol_v2.MAX_V2_CONNECTIONS - 1)
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        assert client.call("kernel.ping", {}).result["status"] == "ready"
    finally:
        if ninth is not None:
            ninth.close()
        for connection in held:
            connection.close()
        if client is not None:
            client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_handshake_deadline_is_absolute_across_fragmented_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    raw = None
    healthy = None
    try:
        monkeypatch.setattr(daemon_service, "V2_HANDSHAKE_TIMEOUT_SECONDS", 0.12)
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.settimeout(1)
        raw.connect(str(daemon.run_root / daemon_api.DAEMON_ENDPOINT_NAME))
        challenge = _recv_v2_frame(raw)
        protocol = protocol_v2.V2ClientConnection(
            (daemon.run_root / daemon_api.DAEMON_SECRET_NAME).read_bytes(),
            expected_daemon_id=daemon.daemon_id,
        )
        framed = protocol_v2.encode_v2_frame(protocol.answer_challenge(challenge))
        sent = 0
        for byte in framed:
            try:
                raw.sendall(bytes((byte,)))
            except (BrokenPipeError, ConnectionResetError):
                break
            sent += 1
            time.sleep(0.03)
            if sent >= 8:
                break
        _wait_until(lambda: daemon.active_connections == 0, timeout=1)
        assert sent < len(framed)
        assert daemon.state is daemon_api.LocalKernelState.RUNNING

        monkeypatch.setattr(
            daemon_service,
            "V2_HANDSHAKE_TIMEOUT_SECONDS",
            protocol_v2.V2_HANDSHAKE_TIMEOUT_SECONDS,
        )
        healthy = daemon_api.LocalKernelClient.connect(daemon.run_root)
        assert healthy.call("kernel.ping", {}).result["status"] == "ready"
    finally:
        if raw is not None:
            raw.close()
        if healthy is not None:
            healthy.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


class _BlockingDaemonApplication(_FakeDaemonApplication):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def get_capabilities_request(self, request: object) -> dict[str, object]:
        self.started.set()
        assert self.release.wait(5)
        return super().get_capabilities_request(request)

    def close(self) -> None:
        assert self.release.is_set()
        super().close()


class _FatalDaemonApplication(_FakeDaemonApplication):
    def get_capabilities_request(self, request: object) -> dict[str, object]:
        self.calls.append(("fatal_get_capabilities", request))
        raise SystemExit(71)


class _SlowDaemonApplication(_FakeDaemonApplication):
    def get_capabilities_request(self, request: object) -> dict[str, object]:
        time.sleep(0.25)
        return super().get_capabilities_request(request)


@DARWIN_ONLY
def test_transport_idle_timeout_does_not_become_a_handler_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, data_root = _c09_root()
    application = _SlowDaemonApplication()
    daemon = None
    client = None

    def factory(*, layout, lease_manager):
        assert lease_manager._root_identity == layout.identity_for(layout.locks)
        return application

    try:
        monkeypatch.setattr(daemon_service, "V2_IDLE_TIMEOUT_SECONDS", 0.1)
        monkeypatch.setattr(daemon_client, "V2_IDLE_TIMEOUT_SECONDS", 0.1)
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=factory,
        )
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        started = time.monotonic()
        response = client.call(
            "application.call",
            {
                "operation": "get_capabilities",
                "request": {"schema_version": 1},
            },
        )
        assert time.monotonic() - started >= 0.2
        assert response.error is None
        assert response.result["ok"] is True
        assert len(application.calls) == 1
        assert daemon.state is daemon_api.LocalKernelState.RUNNING
    finally:
        if client is not None:
            client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_unexpected_handler_base_exception_fails_daemon_and_retains_authority() -> None:
    base, data_root = _c09_root()
    application = _FatalDaemonApplication()
    daemon = None
    client = None

    def factory(*, layout, lease_manager):
        assert lease_manager._root_identity == layout.identity_for(layout.locks)
        return application

    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=factory,
        )
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        with pytest.raises(daemon_api.DaemonError) as raised:
            client.call(
                "application.call",
                {
                    "operation": "get_capabilities",
                    "request": {"schema_version": 1},
                },
            )
        assert raised.value.code is daemon_api.DaemonErrorCode.UNAVAILABLE
        _wait_until(lambda: daemon.state is daemon_api.LocalKernelState.FAILED)
        assert len(application.calls) == 1
        assert application.closed == 0
        assert daemon._authority.released is False
        with pytest.raises(daemon_api.DaemonError) as contended:
            daemon_api.LocalKernelDaemon.start(
                data_root=data_root,
                application_factory=factory,
            )
        assert contended.value.code is daemon_api.DaemonErrorCode.CONTENDED

        daemon.close()
        assert daemon.state is daemon_api.LocalKernelState.CLOSED
        assert application.closed == 1
        assert daemon._authority.released is True
    finally:
        if client is not None:
            client.close()
        if daemon is not None and daemon.state is not daemon_api.LocalKernelState.CLOSED:
            with contextlib.suppress(daemon_api.DaemonError):
                daemon.close()
        if daemon is not None and daemon._authority.released is False:
            daemon._authority.release(owner_token=daemon._authority.owner_token)
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_inflight_handler_makes_shutdown_retain_authority_until_drained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, data_root = _c09_root()
    application = _BlockingDaemonApplication()
    daemon = None
    client = None
    call_errors: list[BaseException] = []
    call_thread = None

    def factory(*, layout, lease_manager):
        assert lease_manager._root_identity == layout.identity_for(layout.locks)
        return application

    def call() -> None:
        try:
            client.call(
                "application.call",
                {
                    "operation": "get_capabilities",
                    "request": {"schema_version": 1},
                },
            )
        except BaseException as error:
            call_errors.append(error)

    try:
        monkeypatch.setattr(daemon_service, "_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=factory,
        )
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        call_thread = threading.Thread(target=call)
        call_thread.start()
        assert application.started.wait(2)

        with pytest.raises(daemon_api.DaemonError) as raised:
            daemon.close()
        assert raised.value.code is daemon_api.DaemonErrorCode.RECOVERY_REQUIRED
        assert daemon.state is daemon_api.LocalKernelState.FAILED
        assert daemon._authority.released is False
        assert application.closed == 0
        with pytest.raises(daemon_api.DaemonError) as contended:
            daemon_api.LocalKernelDaemon.start(
                data_root=data_root,
                application_factory=factory,
            )
        assert contended.value.code is daemon_api.DaemonErrorCode.CONTENDED

        application.release.set()
        call_thread.join(timeout=2)
        assert not call_thread.is_alive()
        assert len(call_errors) == 1
        daemon.close()
        assert daemon.state is daemon_api.LocalKernelState.CLOSED
        assert daemon._authority.released is True
        assert application.closed == 1
    finally:
        application.release.set()
        if call_thread is not None:
            call_thread.join(timeout=2)
        if client is not None:
            client.close()
        if daemon is not None and daemon.state is not daemon_api.LocalKernelState.CLOSED:
            with contextlib.suppress(daemon_api.DaemonError):
                daemon.close()
        if daemon is not None and daemon._authority.released is False:
            daemon._authority.release(owner_token=daemon._authority.owner_token)
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_client_closes_socket_when_final_boot_state_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    created: list[socket.socket] = []
    original_socket = socket.socket
    original_read = daemon_client.read_boot_state
    reads = 0

    def track_socket(*args, **kwargs) -> socket.socket:
        connection = original_socket(*args, **kwargs)
        created.append(connection)
        return connection

    class ClientSocketModule:
        AF_UNIX = socket.AF_UNIX
        SOCK_STREAM = socket.SOCK_STREAM
        SHUT_RDWR = socket.SHUT_RDWR
        socket = staticmethod(track_socket)

    def fail_final_read(run_root: object):
        nonlocal reads
        reads += 1
        if reads == 3:
            raise daemon_api.DaemonError(daemon_api.DaemonErrorCode.RECOVERY_REQUIRED)
        return original_read(run_root)

    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        monkeypatch.setattr(daemon_client, "socket", ClientSocketModule)
        monkeypatch.setattr(daemon_client, "read_boot_state", fail_final_read)
        with pytest.raises(daemon_api.DaemonError) as raised:
            daemon_api.LocalKernelClient.connect(daemon.run_root)
        assert raised.value.code is daemon_api.DaemonErrorCode.AUTHENTICATION_FAILED
        assert reads == 3
        assert len(created) == 1
        assert created[0].fileno() == -1
        assert daemon.state is daemon_api.LocalKernelState.RUNNING
    finally:
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


def test_static_facade_routes_all_literal_operations_to_fixed_application_methods() -> None:
    request_operations = {
        "accept_draft": "accept_draft_request",
        "cancel_task": "cancel_task_request",
        "compare_revisions": "compare_revisions_request",
        "create_project": "create_project_request",
        "create_task": "create_task_request",
        "export_task_artifacts": "export_task_artifacts_request",
        "get_artifact_manifest": "get_artifact_manifest_request",
        "get_capabilities": "get_capabilities_request",
        "get_project": "get_project_request",
        "get_task": "get_task_request",
        "get_task_events": "get_task_events_request",
        "list_projects": "list_projects_request",
        "list_revisions": "list_revisions_request",
        "list_tasks": "list_tasks_request",
        "reject_draft": "reject_draft_request",
        "resume_task": "resume_task_request",
        "revert_project": "revert_project_request",
        "submit_model_program": "submit_model_program_request",
    }
    direct_operations = {
        "create_box",
        "create_cylinder",
        "inspect_model",
        "modify_parameter",
        "move_part",
        "rotate_part",
    }
    assert set(request_operations) | direct_operations == set(
        daemon_api.ALLOWED_APPLICATION_OPERATIONS
    )

    for operation, method_name in request_operations.items():
        application = Mock()
        expected = {"operation": operation}
        getattr(application, method_name).return_value = expected
        facade = daemon_api.LocalKernelFacade(
            application,
            daemon_id="daemon_" + "1" * 32,
        )
        request = {} if operation == "create_project" else {"value": operation}
        assert facade._application_call({"operation": operation, "request": request}) is expected
        getattr(application, method_name).assert_called_once_with(request)
        assert len(application.method_calls) == 1
        assert application.method_calls[0][0] == method_name

    for operation in direct_operations:
        application = Mock()
        expected = {"operation": operation}
        application.invoke_direct_operation_request.return_value = expected
        facade = daemon_api.LocalKernelFacade(
            application,
            daemon_id="daemon_" + "1" * 32,
        )
        request = {"value": operation}
        assert facade._application_call({"operation": operation, "request": request}) is expected
        application.invoke_direct_operation_request.assert_called_once_with(
            operation,
            request,
        )


@DARWIN_ONLY
def test_default_application_composition_runs_without_freecad_eager_startup() -> None:
    base, data_root = _c09_root()
    daemon = None
    client = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(data_root=data_root)
        client = daemon_api.LocalKernelClient.connect(daemon.run_root)
        response = client.call(
            "application.call",
            {
                "operation": "get_capabilities",
                "request": {"schema_version": 1},
            },
        )
        assert response.error is None
        assert response.result["ok"] is True
        assert daemon.state is daemon_api.LocalKernelState.RUNNING
    finally:
        if client is not None:
            client.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


@DARWIN_ONLY
def test_client_eof_and_bad_secret_kill_only_connection_then_fresh_client_succeeds() -> None:
    base, data_root = _c09_root()
    factories: list[object] = []
    daemon = None
    first = None
    second = None
    try:
        daemon = daemon_api.LocalKernelDaemon.start(
            data_root=data_root,
            application_factory=_fake_daemon_factory(factories),
        )
        first = daemon_api.LocalKernelClient.connect(daemon.run_root)
        first.close()
        bad = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bad.settimeout(2)
        try:
            bad.connect(str(daemon.run_root / daemon_api.DAEMON_ENDPOINT_NAME))
            challenge = _recv_v2_frame(bad)
            impostor = protocol_v2.V2ClientConnection(
                b"x" * 32,
                expected_daemon_id=daemon.daemon_id,
            )
            authentication = impostor.answer_challenge(challenge)
            bad.sendall(protocol_v2.encode_v2_frame(authentication))
            with pytest.raises((EOFError, ConnectionResetError, BrokenPipeError, socket.timeout)):
                _recv_v2_frame(bad)
        finally:
            bad.close()
        second = daemon_api.LocalKernelClient.connect(daemon.run_root)
        assert second.call("kernel.ping", {}).result["status"] == "ready"
        assert daemon.state is daemon_api.LocalKernelState.RUNNING
    finally:
        if first is not None:
            first.close()
        if second is not None:
            second.close()
        if daemon is not None:
            daemon.close()
        shutil.rmtree(base, ignore_errors=True)


def test_c09_does_not_change_public_tool_count_or_protocol_method_set() -> None:
    from vibecad.application.public_surface import public_tool_specs

    assert len(public_tool_specs()) == 28
    assert tuple(spec.name for spec in public_tool_specs())[-6:] == (
        "create_box",
        "create_cylinder",
        "inspect_model",
        "modify_parameter",
        "move_part",
        "rotate_part",
    )
    assert not hasattr(daemon_service, "FileGrant")
