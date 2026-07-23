"""Deterministic tests for exclusive workflow resource leases."""

from __future__ import annotations

import ast
import builtins
import errno
import hashlib
import inspect
import io
import json
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import MutableMapping
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.workflow.lease as lease_module
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ProjectWriteLease,
    ResourceLease,
    ResourceLeaseManager,
)

PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
OTHER_PROJECT_ID = "project_11111111111111111111111111111111"
RESOURCE_ID = "project-write:project_0123456789abcdef0123456789abcdef"
OTHER_RESOURCE_ID = "project-write:project_11111111111111111111111111111111"
RESOURCE_DOMAIN = b"vibecad-resource-lease-v1\0"
TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="live POSIX lease contract")


def _expected_key(resource_id: str) -> str:
    return hashlib.sha256(RESOURCE_DOMAIN + resource_id.encode("utf-8")).hexdigest()


def _lock_path(root: Path, resource_id: str) -> Path:
    return root / f"{_expected_key(resource_id)}.lock"


def _assert_lease_error(caught, code: LeaseErrorCode) -> LeaseError:
    assert type(caught.value) is LeaseError
    assert caught.value.code is code
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert getattr(caught.value, "__notes__", None) in (None, [])
    assert caught.value.args == (caught.value.message,)
    assert {"code", "message"} <= set(vars(caught.value))
    assert set(vars(caught.value)) <= {"code", "message", "resource_key"}
    resource_key = getattr(caught.value, "resource_key", None)
    assert resource_key is None or TOKEN_RE.fullmatch(resource_key)
    assert caught.value.message
    assert len(caught.value.message) <= 256
    assert caught.value.message.isprintable()
    assert len(caught.value.message.splitlines()) == 1
    return caught.value


@pytest.fixture
def lease_root(tmp_path: Path) -> Path:
    root = tmp_path / "leases"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return root


def _manager(root: Path) -> ResourceLeaseManager:
    return ResourceLeaseManager(root, trust=LeaseRootTrust.TRUSTED_LOCAL)


class _RecordingAdapter:
    platform_key = "recording"

    def __init__(
        self,
        *,
        acquire_error: LeaseErrorCode | None = None,
        release_error: LeaseErrorCode | None = None,
        events: list[tuple[str, int]] | None = None,
        acquire_entered: threading.Event | None = None,
        acquire_continue: threading.Event | None = None,
    ) -> None:
        self.acquire_error = acquire_error
        self.release_error = release_error
        self.events = events
        self.acquire_entered = acquire_entered
        self.acquire_continue = acquire_continue
        self.acquire_calls: list[int] = []
        self.release_calls: list[int] = []

    def acquire(self, fd: int) -> None:
        self.acquire_calls.append(fd)
        if self.events is not None:
            self.events.append(("lock", fd))
        if self.acquire_entered is not None:
            self.acquire_entered.set()
        if self.acquire_continue is not None and not self.acquire_continue.wait(timeout=5):
            raise AssertionError("adapter acquire continuation timed out")
        if self.acquire_error is not None:
            code = self.acquire_error
            self.acquire_error = None
            raise LeaseError(code)

    def release(self, fd: int) -> None:
        self.release_calls.append(fd)
        if self.events is not None:
            self.events.append(("unlock", fd))
        if self.release_error is not None:
            code = self.release_error
            self.release_error = None
            raise LeaseError(code)


class _CloseOnlyReleaseAdapter:
    """Delegate locking, but prove process-exit release through descriptor closure."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.platform_key = delegate.platform_key
        self.acquire_calls: list[int] = []
        self.release_calls: list[int] = []

    def acquire(self, fd: int) -> None:
        self.acquire_calls.append(fd)
        self.delegate.acquire(fd)

    def release(self, fd: int) -> None:
        self.release_calls.append(fd)


class _RegistryLockProbe:
    """RLock-compatible probe that exposes ownership to guarded test state."""

    def __init__(self) -> None:
        self._delegate = threading.RLock()
        self._local = threading.local()
        self.acquire_calls = 0

    @property
    def held_by_current_thread(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def acquire(self, *args, **kwargs) -> bool:
        acquired = self._delegate.acquire(*args, **kwargs)
        if acquired:
            self.acquire_calls += 1
            self._local.depth = getattr(self._local, "depth", 0) + 1
        return acquired

    def release(self) -> None:
        depth = getattr(self._local, "depth", 0)
        if depth <= 0:
            raise AssertionError("registry lock released without ownership")
        if depth == 1:
            assert getattr(self._local, "pending_key", None) is None
        self._local.depth = depth - 1
        self._delegate.release()

    def expect_insert(self, key: object) -> None:
        assert self.held_by_current_thread
        pending = getattr(self._local, "pending_key", None)
        assert pending in (None, key)
        self._local.pending_key = key

    def consume_insert(self, key: object) -> None:
        assert self.held_by_current_thread
        pending = getattr(self._local, "pending_key", None)
        assert pending == key
        self._local.pending_key = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class _GuardedReservations(MutableMapping):
    """Mapping that rejects mutations outside the exact exported registry lock."""

    def __init__(self, registry_lock: _RegistryLockProbe) -> None:
        self.registry_lock = registry_lock
        self._values: dict[object, object] = {}
        self.mutations: list[tuple[str, object]] = []
        self.allow_enumeration = False

    def _require_lock(self) -> None:
        assert self.registry_lock.held_by_current_thread

    def __getitem__(self, key):
        self._require_lock()
        try:
            return self._values[key]
        except KeyError:
            self.registry_lock.expect_insert(key)
            raise

    def __setitem__(self, key, value) -> None:
        self._require_lock()
        self.registry_lock.consume_insert(key)
        self.mutations.append(("set", key))
        self._values[key] = value

    def __contains__(self, key) -> bool:
        self._require_lock()
        try:
            self[key]
        except KeyError:
            return False
        return True

    def __delitem__(self, key) -> None:
        self._require_lock()
        self.mutations.append(("delete", key))
        del self._values[key]

    def __iter__(self):
        self._require_lock()
        assert self.allow_enumeration
        return iter(self._values)

    def __len__(self) -> int:
        self._require_lock()
        assert self.allow_enumeration
        return len(self._values)

    def snapshot(self) -> dict[object, object]:
        with self.registry_lock:
            return dict(self._values)


class _FakeFcntl:
    LOCK_EX = 0x02
    LOCK_NB = 0x04
    LOCK_UN = 0x08

    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, int]] = []

    def flock(self, fd: int, operation: int) -> None:
        self.calls.append((fd, operation))
        if self.error is not None:
            raise self.error


class _FakeMsvcrt:
    LK_NBLCK = 11
    LK_UNLCK = 12

    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, int, int, int]] = []

    def locking(self, fd: int, mode: int, size: int) -> None:
        self.calls.append((fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR)))
        if self.error is not None:
            raise self.error


_CHILD_SOURCE = r"""
import json
import os
import sys
from pathlib import Path

import vibecad.workflow.lease as lease_module
from vibecad.workflow.lease import LeaseError, LeaseRootTrust, ResourceLeaseManager

root = Path(sys.argv[1])
resource_id = sys.argv[2]
mode = sys.argv[3]
manager = ResourceLeaseManager(root, trust=LeaseRootTrust.TRUSTED_LOCAL)
try:
    lease = manager.acquire(resource_id)
except LeaseError as exc:
    print(
        json.dumps(
            {
                "phase": "error",
                "code": exc.code.value,
                "module": lease_module.__file__,
            }
        ),
        flush=True,
    )
    raise SystemExit(3)

print(
    json.dumps(
        {
            "phase": "acquired",
            "key": lease.resource_key,
            "module": lease_module.__file__,
        }
    ),
    flush=True,
)
if mode == "try":
    lease.release(owner_token=lease.owner_token)
    print(json.dumps({"phase": "released"}), flush=True)
    raise SystemExit(0)
command = json.loads(sys.stdin.readline())["command"]
if command == "release":
    lease.release(owner_token=lease.owner_token)
    print(json.dumps({"phase": "released"}), flush=True)
elif command == "crash":
    os._exit(0)
else:
    raise SystemExit(4)
"""


def _child_command(root: Path, resource_id: str, mode: str) -> list[str]:
    return [sys.executable, "-u", "-c", _CHILD_SOURCE, str(root), resource_id, mode]


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    repository = Path(__file__).resolve().parents[1]
    environment["PYTHONPATH"] = str(repository / "src")
    return environment


def _read_json_line(stream, deadline: float) -> dict[str, object]:
    lines: queue.Queue[str] = queue.Queue(maxsize=1)

    def read() -> None:
        lines.put(stream.readline())

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AssertionError("subprocess handshake deadline expired")
    try:
        line = lines.get(timeout=remaining)
    except queue.Empty as exc:
        raise AssertionError("subprocess handshake timed out") from exc
    if not line:
        raise AssertionError("subprocess closed before handshake")
    value = json.loads(line)
    assert type(value) is dict
    return value


def _run_child_try(root: Path, resource_id: str, deadline: float):
    remaining = deadline - time.monotonic()
    assert remaining > 0
    return subprocess.run(
        _child_command(root, resource_id, "try"),
        input="",
        capture_output=True,
        text=True,
        env=_child_environment(),
        timeout=remaining,
        check=False,
    )


def _read_fd_with_deadline(fd: int, maximum: int, deadline: float) -> bytes:
    values: queue.Queue[bytes] = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: values.put(os.read(fd, maximum)), daemon=True)
    reader.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AssertionError("pipe handshake deadline expired")
    try:
        return values.get(timeout=remaining)
    except queue.Empty as exc:
        raise AssertionError("pipe handshake timed out") from exc


def _waitpid_with_deadline(child_pid: int, deadline: float) -> tuple[int, int]:
    values: queue.Queue[tuple[int, int]] = queue.Queue(maxsize=1)
    waiter = threading.Thread(
        target=lambda: values.put(os.waitpid(child_pid, 0)),
        daemon=True,
    )
    waiter.start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        remaining = 0
    cleanup_reserve = min(1.0, remaining / 2)
    try:
        return values.get(timeout=max(0, remaining - cleanup_reserve))
    except queue.Empty:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        remaining = max(0, deadline - time.monotonic())
        try:
            return values.get(timeout=remaining)
        except queue.Empty as final_exc:
            raise AssertionError("child reap timed out after forced termination") from final_exc


def _parse_child_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


def _wrong_token(token: str) -> str:
    replacement = "0" if token[0] != "0" else "1"
    return replacement + token[1:]


_STORAGE_OS_PROBES = (
    "access",
    "chdir",
    "chmod",
    "chown",
    "close",
    "creat",
    "dup",
    "dup2",
    "fstat",
    "fstatvfs",
    "fsync",
    "getcwd",
    "getcwdb",
    "geteuid",
    "get_inheritable",
    "getpid",
    "getxattr",
    "link",
    "listdir",
    "listxattr",
    "lstat",
    "makedirs",
    "mkdir",
    "mkfifo",
    "mknod",
    "open",
    "pathconf",
    "readlink",
    "removexattr",
    "rename",
    "replace",
    "rmdir",
    "scandir",
    "setxattr",
    "set_inheritable",
    "stat",
    "statvfs",
    "symlink",
    "unlink",
    "umask",
)


def _install_storage_failure_spies(patch, callback) -> None:
    for name in _STORAGE_OS_PROBES:
        if hasattr(lease_module.os, name):
            patch.setattr(lease_module.os, name, callback)
    for name in (
        "exists",
        "is_dir",
        "is_file",
        "is_symlink",
        "iterdir",
        "lstat",
        "open",
        "read_bytes",
        "read_text",
        "resolve",
        "stat",
        "write_bytes",
        "write_text",
    ):
        patch.setattr(Path, name, callback)
    patch.setattr(io, "open", callback)
    patch.setattr(builtins, "open", callback)
    patch.setattr(socket, "socket", callback)


def test_public_exports_are_exact():
    assert set(lease_module.__all__) == {
        "LeaseError",
        "LeaseErrorCode",
        "LeaseRootTrust",
        "ProjectWriteLease",
        "ResourceLease",
        "ResourceLeaseManager",
    }


def test_public_enums_are_exact_closed_sets():
    assert {name: item.value for name, item in LeaseRootTrust.__members__.items()} == {
        "TRUSTED_LOCAL": "trusted_local"
    }
    assert {name: item.value for name, item in LeaseErrorCode.__members__.items()} == {
        "INVALID_RESOURCE": "invalid_resource",
        "INVALID_OWNER": "invalid_owner",
        "UNTRUSTED_ROOT": "untrusted_root",
        "UNSAFE_ROOT": "unsafe_root",
        "UNSAFE_LOCK_ENTRY": "unsafe_lock_entry",
        "CONTENDED": "contended",
        "WRONG_OWNER": "wrong_owner",
        "ALREADY_RELEASED": "already_released",
        "WRONG_PROCESS": "wrong_process",
        "INVALID_LEASE": "invalid_lease",
        "UNSUPPORTED_PLATFORM": "unsupported_platform",
        "LOCK_UNAVAILABLE": "lock_unavailable",
        "IO_ERROR": "io_error",
    }


def test_lease_module_imports_without_cad_mcp_model_or_runtime_modules():
    code = """
import sys
import vibecad.workflow.lease
banned = {'FreeCAD', 'Part', 'mcp', 'anthropic', 'openai', 'vibecad.runtime.status'}
loaded = sorted(name for name in banned if name in sys.modules)
assert not loaded, loaded
print('workflow lease pure import OK')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_child_environment(),
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "workflow lease pure import OK" in result.stdout


def test_error_code_requires_exact_enum_type():
    class CodeProxy:
        @property
        def __class__(self):
            return LeaseErrorCode

    with pytest.raises(TypeError, match="code must be a LeaseErrorCode"):
        LeaseError(CodeProxy())


def test_error_messages_are_fixed_and_custom_text_cannot_be_injected():
    for code in LeaseErrorCode:
        first = LeaseError(code)
        second = LeaseError(code)
        assert type(first) is LeaseError
        assert first.code is code
        assert first.message == second.message
        assert str(first) == str(second) == first.message
        assert first.args == (first.message,)
        assert set(vars(first)) <= {"code", "message", "resource_key"}
    with pytest.raises(TypeError):
        LeaseError(LeaseErrorCode.IO_ERROR, "SENSITIVE_CUSTOM_MESSAGE")
    with pytest.raises(TypeError):
        LeaseError(LeaseErrorCode.IO_ERROR, message="SENSITIVE_CUSTOM_MESSAGE")


def test_error_resource_key_is_optional_validated_and_the_only_dynamic_field():
    resource_key = "a" * 64
    error = LeaseError(LeaseErrorCode.CONTENDED, resource_key=resource_key)
    assert error.resource_key == resource_key
    assert set(vars(error)) == {"code", "message", "resource_key"}

    class KeyString(str):
        pass

    for invalid in ("short", "A" * 64, "g" * 64, KeyString(resource_key)):
        with pytest.raises(ValueError, match="resource_key must be 64 lowercase hex characters"):
            LeaseError(LeaseErrorCode.IO_ERROR, resource_key=invalid)


@POSIX_ONLY
def test_sensitive_roots_resources_and_tokens_are_never_reflected(tmp_path: Path, lease_root: Path):
    resource_values = [
        "SENSITIVE_RESOURCE_ALPHA_" + "a" * 240,
        "SENSITIVE_RESOURCE_BRAVO_" + "b" * 240,
    ]
    resource_messages: list[str] = []
    manager = _manager(lease_root)
    for value in resource_values:
        with pytest.raises(LeaseError) as caught:
            manager.acquire(value)
        error = _assert_lease_error(caught, LeaseErrorCode.INVALID_RESOURCE)
        assert value not in str(error)
        assert value[:24] not in str(error)
        resource_messages.append(error.message)
    assert len(set(resource_messages)) == 1

    root_messages: list[str] = []
    for name in ("SENSITIVE_ROOT_ALPHA", "SENSITIVE_ROOT_BRAVO"):
        missing = tmp_path / name
        with pytest.raises(LeaseError) as caught:
            _manager(missing)
        error = _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
        assert str(missing) not in str(error)
        assert name not in str(error)
        root_messages.append(error.message)
    assert len(set(root_messages)) == 1

    lease = manager.acquire(RESOURCE_ID)
    invalid_owner_messages: list[str] = []
    for owner in ("SENSITIVE_OWNER_ALPHA", "SENSITIVE_OWNER_BRAVO"):
        with pytest.raises(LeaseError) as caught:
            lease.release(owner_token=owner)
        error = _assert_lease_error(caught, LeaseErrorCode.INVALID_OWNER)
        assert owner not in str(error)
        assert lease.owner_token not in str(error)
        assert lease.owner_token[:16] not in str(error)
        invalid_owner_messages.append(error.message)
    assert len(set(invalid_owner_messages)) == 1

    wrong_owner_messages: list[str] = []
    for owner in ("a" * 64, "b" * 64):
        with pytest.raises(LeaseError) as caught:
            lease.release(owner_token=owner)
        error = _assert_lease_error(caught, LeaseErrorCode.WRONG_OWNER)
        assert owner not in str(error)
        assert owner[:16] not in str(error)
        assert lease.owner_token not in str(error)
        assert lease.owner_token[:16] not in str(error)
        wrong_owner_messages.append(error.message)
    assert len(set(wrong_owner_messages)) == 1
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_manager_requires_explicit_trusted_local_root(lease_root: Path):
    with pytest.raises(LeaseError) as caught:
        ResourceLeaseManager(lease_root, trust=None)
    _assert_lease_error(caught, LeaseErrorCode.UNTRUSTED_ROOT)
    assert list(lease_root.iterdir()) == []


@POSIX_ONLY
def test_trust_requires_exact_enum_member_before_root_work(monkeypatch, lease_root: Path):
    class TrustProxy:
        @property
        def __class__(self):
            return LeaseRootTrust

    touched: list[str] = []

    def storage_work(*_args, **_kwargs):
        touched.append("storage")
        raise AssertionError("trust validation performed storage work")

    def adapter_work():
        touched.append("adapter")
        raise AssertionError("trust validation selected a platform adapter")

    with monkeypatch.context() as patch:
        _install_storage_failure_spies(patch, storage_work)
        patch.setattr(lease_module, "_new_platform_adapter", adapter_work)
        for invalid in (None, TrustProxy(), LeaseRootTrust.TRUSTED_LOCAL.value):
            with pytest.raises(LeaseError) as caught:
                ResourceLeaseManager(lease_root, trust=invalid)
            _assert_lease_error(caught, LeaseErrorCode.UNTRUSTED_ROOT)
    assert touched == []
    assert list(lease_root.iterdir()) == []


@POSIX_ONLY
def test_trust_and_owner_tokens_are_keyword_only(lease_root: Path):
    with pytest.raises(TypeError):
        ResourceLeaseManager(lease_root, LeaseRootTrust.TRUSTED_LOCAL)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    with pytest.raises(TypeError):
        lease.release(lease.owner_token)
    with pytest.raises(TypeError):
        manager.release(lease, lease.owner_token)
    assert not lease.released
    lease.release(owner_token=lease.owner_token)


def test_public_call_signatures_are_exact():
    empty = inspect.Parameter.empty
    positional = inspect.Parameter.POSITIONAL_OR_KEYWORD
    keyword_only = inspect.Parameter.KEYWORD_ONLY

    def shape(callable_object):
        return [
            (parameter.name, parameter.kind, parameter.default)
            for parameter in inspect.signature(callable_object).parameters.values()
        ]

    assert shape(ResourceLeaseManager) == [
        ("lock_root", positional, empty),
        ("trust", keyword_only, empty),
    ]
    assert shape(ResourceLeaseManager.acquire) == [
        ("self", positional, empty),
        ("resource_id", positional, empty),
    ]
    assert shape(ResourceLeaseManager.acquire_project_write) == [
        ("self", positional, empty),
        ("project_id", positional, empty),
    ]
    assert shape(ResourceLeaseManager.release) == [
        ("self", positional, empty),
        ("lease", positional, empty),
        ("owner_token", keyword_only, empty),
    ]
    assert shape(ResourceLease.release) == [
        ("self", positional, empty),
        ("owner_token", keyword_only, empty),
    ]
    assert shape(ResourceLease.require_current) == [
        ("self", positional, empty),
    ]
    assert shape(ProjectWriteLease.release) == shape(ResourceLease.release)
    assert shape(ProjectWriteLease.require_current) == shape(ResourceLease.require_current)


@POSIX_ONLY
def test_require_current_is_read_only_for_generic_and_project_leases(lease_root: Path):
    manager = _manager(lease_root)
    generic = manager.acquire(RESOURCE_ID)
    project = manager.acquire_project_write(PROJECT_ID)
    generic_path = _lock_path(lease_root, RESOURCE_ID)
    project_path = _lock_path(lease_root, PROJECT_ID)
    before = {
        "generic": (
            generic.released,
            generic.resource_key,
            generic.owner_token,
            generic._fd,
            os.fstat(generic._fd),
            generic_path.stat(),
        ),
        "project": (
            project.released,
            project.resource_key,
            project.owner_token,
            project.project_id,
            project._fd,
            os.fstat(project._fd),
            project_path.stat(),
        ),
    }

    assert generic.require_current() is None
    assert project.require_current() is None
    assert generic.require_current() is None
    assert project.require_current() is None

    assert before == {
        "generic": (
            generic.released,
            generic.resource_key,
            generic.owner_token,
            generic._fd,
            os.fstat(generic._fd),
            generic_path.stat(),
        ),
        "project": (
            project.released,
            project.resource_key,
            project.owner_token,
            project.project_id,
            project._fd,
            os.fstat(project._fd),
            project_path.stat(),
        ),
    }
    project.release(owner_token=project.owner_token)
    generic.release(owner_token=generic.owner_token)


@POSIX_ONLY
def test_require_current_rejects_released_and_forged_leases(lease_root: Path):
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    lease.release(owner_token=lease.owner_token)
    with pytest.raises(LeaseError) as released:
        lease.require_current()
    _assert_lease_error(released, LeaseErrorCode.ALREADY_RELEASED)

    for forged in (object.__new__(ResourceLease), object.__new__(ProjectWriteLease)):
        with pytest.raises(LeaseError) as invalid:
            forged.require_current()
        _assert_lease_error(invalid, LeaseErrorCode.INVALID_LEASE)


@POSIX_ONLY
@pytest.mark.parametrize("field", ["issuer", "seal"])
def test_require_current_rejects_changed_issuer_or_seal(
    lease_root: Path,
    field: str,
):
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    original_issuer = lease._issuer
    original_seal = lease._seal
    if field == "issuer":
        object.__setattr__(lease, "_issuer", _manager(lease_root))
    else:
        object.__setattr__(lease, "_seal", object())
    try:
        with pytest.raises(LeaseError) as caught:
            lease.require_current()
        _assert_lease_error(caught, LeaseErrorCode.INVALID_LEASE)
    finally:
        object.__setattr__(lease, "_issuer", original_issuer)
        object.__setattr__(lease, "_seal", original_seal)
        lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_require_current_rejects_process_reservation_replacement(lease_root: Path):
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    key = lease._registry_key
    original = lease_module._PROCESS_RESERVATIONS[key]
    replacement = lease_module._Reservation(lease.owner_token)
    replacement.fd = lease._fd
    with lease_module._PROCESS_REGISTRY_LOCK:
        lease_module._PROCESS_RESERVATIONS[key] = replacement
    try:
        with pytest.raises(LeaseError) as caught:
            lease.require_current()
        _assert_lease_error(caught, LeaseErrorCode.INVALID_LEASE)
    finally:
        with lease_module._PROCESS_REGISTRY_LOCK:
            lease_module._PROCESS_RESERVATIONS[key] = original
    assert lease.require_current() is None
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_require_current_rejects_root_rename_and_recreation(tmp_path: Path):
    root = tmp_path / "leases"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    moved = tmp_path / "moved-leases"
    lease = _manager(root).acquire(RESOURCE_ID)
    root.rename(moved)
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    try:
        with pytest.raises(LeaseError) as caught:
            lease.require_current()
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
    finally:
        root.rmdir()
        moved.rename(root)
    assert lease.require_current() is None
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_require_current_rejects_lock_unlink_and_rebind(lease_root: Path):
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    path = _lock_path(lease_root, RESOURCE_ID)
    original_fd_stat = os.fstat(lease._fd)
    path.unlink()
    path.write_bytes(b"replacement")
    path.chmod(0o600)
    replacement = path.stat()
    assert (replacement.st_dev, replacement.st_ino) != (
        original_fd_stat.st_dev,
        original_fd_stat.st_ino,
    )

    with pytest.raises(LeaseError) as caught:
        lease.require_current()
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_require_current_rejects_changed_lock_metadata(lease_root: Path):
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    path = _lock_path(lease_root, RESOURCE_ID)
    path.chmod(0o640)
    try:
        with pytest.raises(LeaseError) as caught:
            lease.require_current()
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    finally:
        path.chmod(0o600)
    assert lease.require_current() is None
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
@pytest.mark.parametrize("mutation", ["close", "replace"])
def test_require_current_rejects_closed_or_replaced_held_fd(
    tmp_path: Path,
    lease_root: Path,
    mutation: str,
):
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    held_fd = lease._fd
    replacement_fd = -1
    if mutation == "close":
        os.close(held_fd)
    else:
        replacement = tmp_path / "replacement-fd"
        replacement.write_bytes(b"replacement")
        replacement.chmod(0o600)
        replacement_fd = os.open(replacement, os.O_RDWR | os.O_CLOEXEC)
        os.dup2(replacement_fd, held_fd)
    try:
        with pytest.raises(LeaseError) as caught:
            lease.require_current()
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    finally:
        try:
            lease.release(owner_token=lease.owner_token)
        except LeaseError as cleanup:
            assert mutation == "close"
            assert cleanup.code is LeaseErrorCode.IO_ERROR
        if replacement_fd >= 0:
            os.close(replacement_fd)


@POSIX_ONLY
def test_require_current_rejects_fork_inherited_lease(lease_root: Path):
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            try:
                lease.require_current()
            except LeaseError as error:
                result = error.code.value
            else:
                result = "unexpected-success"
            os.write(write_fd, result.encode("ascii"))
        finally:
            os.close(write_fd)
            os._exit(0)

    os.close(write_fd)
    deadline = time.monotonic() + 5
    try:
        result = _read_fd_with_deadline(read_fd, 128, deadline).decode("ascii")
        waited, status_value = _waitpid_with_deadline(child_pid, deadline)
    finally:
        os.close(read_fd)
    assert waited == child_pid
    assert os.waitstatus_to_exitcode(status_value) == 0
    assert result == LeaseErrorCode.WRONG_PROCESS.value
    assert lease.require_current() is None
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
@pytest.mark.parametrize("value", [None, 7, True, object()], ids=("none", "int", "bool", "object"))
def test_root_rejects_invalid_exact_types(value):
    with pytest.raises(LeaseError) as caught:
        ResourceLeaseManager(value, trust=LeaseRootTrust.TRUSTED_LOCAL)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)


@POSIX_ONLY
def test_root_must_be_absolute(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative = Path("leases")
    relative.mkdir(mode=0o700)
    with pytest.raises(LeaseError) as caught:
        ResourceLeaseManager(relative, trust=LeaseRootTrust.TRUSTED_LOCAL)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)


@POSIX_ONLY
def test_root_must_exist(tmp_path: Path):
    missing = tmp_path / "missing-sensitive-root"
    with pytest.raises(LeaseError) as caught:
        _manager(missing)
    error = _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
    assert str(missing) not in str(error)
    assert not missing.exists()


@POSIX_ONLY
def test_root_rejects_non_directory(tmp_path: Path):
    path = tmp_path / "file"
    path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(LeaseError) as caught:
        _manager(path)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)


@POSIX_ONLY
def test_root_rejects_final_symlink(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(LeaseError) as caught:
        _manager(link)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
    assert list(target.iterdir()) == []


@POSIX_ONLY
def test_root_rejects_ancestor_symlink(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    root = target / "leases"
    root.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(LeaseError) as caught:
        _manager(alias / "leases")
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
    assert list(root.iterdir()) == []


@POSIX_ONLY
@pytest.mark.parametrize("unsafe_mode", [0o755, 0o640], ids=("world-readable", "non-executable"))
def test_root_requires_private_mode_without_repair(lease_root: Path, unsafe_mode: int):
    inode = lease_root.stat().st_ino
    lease_root.chmod(unsafe_mode)
    try:
        with pytest.raises(LeaseError) as caught:
            _manager(lease_root)
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
        result = lease_root.stat()
        assert result.st_ino == inode
        assert stat.S_IMODE(result.st_mode) == unsafe_mode
        assert list(lease_root.iterdir()) == []
    finally:
        lease_root.chmod(0o700)


@POSIX_ONLY
def test_root_requires_current_euid_from_open_directory_fd(monkeypatch, lease_root: Path):
    expected = lease_root.stat()
    real_fstat = lease_module.os.fstat
    hits = 0

    def wrong_owner_fstat(fd: int):
        nonlocal hits
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == (expected.st_dev, expected.st_ino):
            hits += 1
            values = list(result)
            values[4] = os.geteuid() + 1
            return os.stat_result(values)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(lease_module.os, "fstat", wrong_owner_fstat)
        with pytest.raises(LeaseError) as caught:
            _manager(lease_root)
        error = _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
    assert hits >= 1
    assert str(lease_root) not in str(error)
    assert list(lease_root.iterdir()) == []
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_new_lock_entry_fsyncs_the_pinned_root_directory(monkeypatch, lease_root: Path):
    real_fstat = lease_module.os.fstat
    expected = lease_root.stat()
    synced: list[tuple[int, int, int]] = []

    def recording_fsync(fd: int) -> None:
        result = real_fstat(fd)
        synced.append((result.st_dev, result.st_ino, result.st_mode))

    monkeypatch.setattr(lease_module.os, "fsync", recording_fsync)
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    assert any(
        (device, inode) == (expected.st_dev, expected.st_ino) and stat.S_ISDIR(mode)
        for device, inode, mode in synced
    )
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_root_fsync_failure_rolls_back_without_deleting_entry(monkeypatch, lease_root: Path):
    manager = _manager(lease_root)
    expected = lease_root.stat()
    real_fstat = lease_module.os.fstat
    real_close = lease_module._close_lock_fd
    closed: list[int] = []
    calls = 0

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def fail_once(fd: int) -> None:
        nonlocal calls
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) != (expected.st_dev, expected.st_ino):
            return
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "sensitive native fsync details")

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    monkeypatch.setattr(lease_module.os, "fsync", fail_once)
    with pytest.raises(LeaseError) as caught:
        manager.acquire(RESOURCE_ID)
    error = _assert_lease_error(caught, LeaseErrorCode.IO_ERROR)
    assert "sensitive native fsync details" not in str(error)
    assert calls == 1
    path = _lock_path(lease_root, RESOURCE_ID)
    assert path.is_file()
    inode = path.stat().st_ino
    assert len(closed) == 1
    lease = manager.acquire(RESOURCE_ID)
    assert path.stat().st_ino == inode
    lease.release(owner_token=lease.owner_token)
    assert len(closed) == 2


@POSIX_ONLY
def test_root_identity_is_pinned_across_acquisition(tmp_path: Path):
    root = tmp_path / "leases"
    root.mkdir(mode=0o700)
    manager = _manager(root)
    old = tmp_path / "old-leases"
    root.rename(old)
    root.mkdir(mode=0o700)
    with pytest.raises(LeaseError) as caught:
        manager.acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
    assert list(root.iterdir()) == []


@POSIX_ONLY
def test_root_device_identity_is_pinned_across_acquisition(monkeypatch, lease_root: Path):
    expected = lease_root.stat()
    manager = _manager(lease_root)
    real_fstat = lease_module.os.fstat
    hits = 0

    def changed_device_fstat(fd: int):
        nonlocal hits
        result = real_fstat(fd)
        if (result.st_dev, result.st_ino) == (expected.st_dev, expected.st_ino):
            hits += 1
            values = list(result)
            values[2] = result.st_dev + 1
            return os.stat_result(values)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(lease_module.os, "fstat", changed_device_fstat)
        with pytest.raises(LeaseError) as caught:
            manager.acquire(RESOURCE_ID)
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_ROOT)
    assert hits >= 1
    assert list(lease_root.iterdir()) == []
    lease = manager.acquire(RESOURCE_ID)
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
@pytest.mark.parametrize(
    "value",
    [
        None,
        1,
        True,
        "",
        "   ",
        "line\nbreak",
        "carriage\rreturn",
        "tab\tcharacter",
        "line\u2028separator",
        "paragraph\u2029separator",
        "nul\x00byte",
        "x" * 257,
        "é" * 129,
        "bad\ud800",
    ],
    ids=(
        "none",
        "int",
        "bool",
        "empty",
        "blank",
        "newline",
        "carriage-return",
        "tab",
        "line-separator",
        "paragraph-separator",
        "control",
        "ascii-over-bytes",
        "multibyte-over-bytes",
        "invalid-unicode",
    ),
)
def test_resource_identifier_is_exact_bounded_text(lease_root: Path, value):
    manager = _manager(lease_root)
    with pytest.raises(LeaseError) as caught:
        manager.acquire(value)
    error = _assert_lease_error(caught, LeaseErrorCode.INVALID_RESOURCE)
    assert repr(value) not in str(error)
    assert list(lease_root.iterdir()) == []


@POSIX_ONLY
def test_resource_identifier_rejects_string_subclass(lease_root: Path):
    class ResourceString(str):
        pass

    with pytest.raises(LeaseError) as caught:
        _manager(lease_root).acquire(ResourceString(RESOURCE_ID))
    _assert_lease_error(caught, LeaseErrorCode.INVALID_RESOURCE)


@POSIX_ONLY
@pytest.mark.parametrize("value", ["x" * 256, "é" * 128], ids=("ascii", "multibyte"))
def test_resource_identifier_accepts_exact_byte_boundary(lease_root: Path, value: str):
    lease = _manager(lease_root).acquire(value)
    assert lease.resource_key == _expected_key(value)
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_resource_identifier_preserves_spaces_and_unicode_normalization(lease_root: Path):
    values = ["x", " x ", "é", "e\u0301"]
    manager = _manager(lease_root)
    leases: list[ResourceLease] = []
    try:
        for value in values:
            leases.append(manager.acquire(value))
        expected_keys = [_expected_key(value) for value in values]
        assert [lease.resource_key for lease in leases] == expected_keys
        assert len(set(expected_keys)) == len(values)
        assert {path.name for path in lease_root.iterdir()} == {
            f"{resource_key}.lock" for resource_key in expected_keys
        }
    finally:
        for lease in reversed(leases):
            lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_path_like_resource_identifiers_are_always_hashed(
    monkeypatch, lease_root: Path, tmp_path: Path
):
    relative_sentinel = tmp_path / "outside-sentinel"
    absolute_sentinel = tmp_path / "outside-absolute"
    relative_sentinel.write_bytes(b"relative-sentinel")
    absolute_sentinel.write_bytes(b"absolute-sentinel")
    relative_sentinel.chmod(0o600)
    absolute_sentinel.chmod(0o600)
    values = ["../outside-sentinel", str(absolute_sentinel), "nested/resource"]
    assert all(len(value.encode("utf-8")) <= 256 for value in values)
    sentinels = (relative_sentinel, absolute_sentinel)
    before = {
        path: (
            path.stat().st_dev,
            path.stat().st_ino,
            path.stat().st_mode,
            path.stat().st_nlink,
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in sentinels
    }
    root_stat = lease_root.stat()
    root_identity = (root_stat.st_dev, root_stat.st_ino)
    forbidden_targets = {
        os.path.normpath(str(lease_root / values[0])),
        os.path.normpath(values[1]),
        os.path.normpath(str(lease_root / values[2])),
    }
    selected: list[tuple[str, str]] = []
    real_open = lease_module.os.open
    real_stat = lease_module.os.stat
    real_lstat = lease_module.os.lstat
    real_chmod = lease_module.os.chmod
    real_close = lease_module.os.close
    real_dup = lease_module.os.dup
    real_dup2 = lease_module.os.dup2
    real_fstat = lease_module.os.fstat
    real_path_open = Path.open
    real_io_open = io.open
    real_builtin_open = builtins.open
    real_path_probes = {
        name: getattr(lease_module.os, name)
        for name in (
            "access",
            "chdir",
            "getxattr",
            "listdir",
            "listxattr",
            "pathconf",
            "readlink",
            "removexattr",
            "scandir",
            "setxattr",
            "statvfs",
        )
        if hasattr(lease_module.os, name)
    }
    opened_paths: dict[int, str] = {}
    unresolved_paths: list[tuple[str, str]] = []
    mutation_probes: list[str] = []
    alternate_entry_creations: list[str] = []

    def resolved_path(path, dir_fd) -> str | None:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return None
        text = os.fsdecode(os.fspath(path))
        if os.path.isabs(text):
            return os.path.normpath(text)
        if dir_fd in opened_paths:
            return os.path.normpath(os.path.join(opened_paths[dir_fd], text))
        if dir_fd is not None:
            parent = real_fstat(dir_fd)
            if (parent.st_dev, parent.st_ino) == root_identity:
                return os.path.normpath(str(lease_root / text))
        return None

    def record_selection(operation: str, path, *, dir_fd=None) -> None:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        text = os.fsdecode(os.fspath(path))
        candidates = {os.path.normpath(text)}
        resolved = resolved_path(path, dir_fd)
        if resolved is not None:
            candidates.add(resolved)
        else:
            unresolved_paths.append((operation, text))
        if text in values or not candidates.isdisjoint(forbidden_targets):
            selected.append((operation, text))

    def record_alternate_entry_creation(operation: str, path) -> None:
        if not isinstance(path, (str, bytes, os.PathLike)):
            return
        text = os.fsdecode(os.fspath(path))
        absolute = os.path.normpath(os.path.abspath(text))
        parent, filename = os.path.split(absolute)
        if (
            parent == os.path.normpath(str(lease_root))
            and filename.endswith(".lock")
            and TOKEN_RE.fullmatch(filename[:-5])
        ):
            alternate_entry_creations.append(operation)

    def recording_open(path, *args, **kwargs):
        dir_fd = kwargs.get("dir_fd")
        record_selection("open", path, dir_fd=dir_fd)
        resolved = resolved_path(path, dir_fd)
        fd = real_open(path, *args, **kwargs)
        if resolved is not None:
            opened_paths[fd] = resolved
        return fd

    def recording_close(fd: int) -> None:
        real_close(fd)
        opened_paths.pop(fd, None)

    def recording_dup(fd: int) -> int:
        result = real_dup(fd)
        if fd in opened_paths:
            opened_paths[result] = opened_paths[fd]
        return result

    def recording_dup2(fd: int, target: int, *args, **kwargs) -> int:
        resolved = opened_paths.get(fd)
        result = real_dup2(fd, target, *args, **kwargs)
        opened_paths.pop(target, None)
        if resolved is not None:
            opened_paths[result] = resolved
        return result

    def recording_stat(path, *args, **kwargs):
        record_selection("stat", path, dir_fd=kwargs.get("dir_fd"))
        return real_stat(path, *args, **kwargs)

    def recording_lstat(path, *args, **kwargs):
        record_selection("lstat", path, dir_fd=kwargs.get("dir_fd"))
        return real_lstat(path, *args, **kwargs)

    def recording_chmod(path, *args, **kwargs):
        record_selection("chmod", path, dir_fd=kwargs.get("dir_fd"))
        return real_chmod(path, *args, **kwargs)

    def recording_path_open(path, *args, **kwargs):
        record_selection("Path.open", path)
        record_alternate_entry_creation("Path.open", path)
        return real_path_open(path, *args, **kwargs)

    def recording_io_open(path, *args, **kwargs):
        record_selection("io.open", path)
        record_alternate_entry_creation("io.open", path)
        return real_io_open(path, *args, **kwargs)

    def recording_builtin_open(path, *args, **kwargs):
        record_selection("builtins.open", path)
        record_alternate_entry_creation("builtins.open", path)
        return real_builtin_open(path, *args, **kwargs)

    def recording_path_probe(operation: str, function, *, mutates: bool = False):
        def probe(path, *args, **kwargs):
            record_selection(operation, path, dir_fd=kwargs.get("dir_fd"))
            if mutates:
                mutation_probes.append(operation)
                raise AssertionError("lock path mutation is forbidden")
            return function(path, *args, **kwargs)

        return probe

    expected_names: set[str] = set()
    with monkeypatch.context() as patch:
        patch.setattr(lease_module.os, "open", recording_open)
        patch.setattr(lease_module.os, "stat", recording_stat)
        patch.setattr(lease_module.os, "lstat", recording_lstat)
        patch.setattr(lease_module.os, "chmod", recording_chmod)
        patch.setattr(lease_module.os, "close", recording_close)
        patch.setattr(lease_module.os, "dup", recording_dup)
        patch.setattr(lease_module.os, "dup2", recording_dup2)
        patch.setattr(
            lease_module.os,
            "supports_dir_fd",
            set(lease_module.os.supports_dir_fd) | {recording_open, recording_stat},
        )
        patch.setattr(
            lease_module.os,
            "supports_follow_symlinks",
            set(lease_module.os.supports_follow_symlinks) | {recording_stat},
        )
        patch.setattr(Path, "open", recording_path_open)
        patch.setattr(io, "open", recording_io_open)
        patch.setattr(builtins, "open", recording_builtin_open)
        for name, function in real_path_probes.items():
            patch.setattr(
                lease_module.os,
                name,
                recording_path_probe(
                    f"os.{name}",
                    function,
                    mutates=name in {"removexattr", "setxattr"},
                ),
            )
        manager = _manager(lease_root)
        for value in values:
            lease = manager.acquire(value)
            expected_key = _expected_key(value)
            expected_names.add(f"{expected_key}.lock")
            assert lease.resource_key == expected_key
            assert {path.name for path in lease_root.iterdir()} == expected_names
            lease.release(owner_token=lease.owner_token)
    assert selected == []
    assert unresolved_paths == []
    assert mutation_probes == []
    assert alternate_entry_creations == []
    assert relative_sentinel.read_bytes() == b"relative-sentinel"
    assert absolute_sentinel.read_bytes() == b"absolute-sentinel"
    for path in sentinels:
        after = path.stat()
        assert (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ) == before[path]


@POSIX_ONLY
def test_resource_key_owner_token_and_lock_path_are_coordinator_owned(lease_root: Path):
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    assert type(lease) is ResourceLease
    assert lease.resource_key == _expected_key(RESOURCE_ID)
    assert TOKEN_RE.fullmatch(lease.owner_token)
    assert not lease.released
    assert [path.name for path in lease_root.iterdir()] == [f"{lease.resource_key}.lock"]
    assert RESOURCE_ID not in str(_lock_path(lease_root, RESOURCE_ID))
    mode = stat.S_IMODE(_lock_path(lease_root, RESOURCE_ID).stat().st_mode)
    assert mode == 0o600
    lease.release(owner_token=lease.owner_token)
    assert lease.released


@POSIX_ONLY
@pytest.mark.parametrize(
    "project_id",
    [
        "project_0123456789ABCDEF0123456789abcdef",
        "project_0123",
        "task_0123456789abcdef0123456789abcdef",
        PROJECT_ID + "0",
    ],
    ids=("uppercase", "short", "wrong-prefix", "long"),
)
def test_project_write_lease_requires_canonical_project_id(lease_root: Path, project_id: str):
    with pytest.raises(LeaseError) as caught:
        _manager(lease_root).acquire_project_write(project_id)
    _assert_lease_error(caught, LeaseErrorCode.INVALID_RESOURCE)


@POSIX_ONLY
def test_project_identifier_requires_exact_string_type_before_storage(lease_root: Path):
    class ProjectString(str):
        pass

    class ProjectProxy:
        @property
        def __class__(self):
            return str

    manager = _manager(lease_root)
    for value in (None, True, ProjectString(PROJECT_ID), ProjectProxy(), "project_" + "z" * 32):
        with pytest.raises(LeaseError) as caught:
            manager.acquire_project_write(value)
        _assert_lease_error(caught, LeaseErrorCode.INVALID_RESOURCE)
    assert list(lease_root.iterdir()) == []


@POSIX_ONLY
def test_project_write_lease_preserves_canonical_identity(lease_root: Path):
    lease = _manager(lease_root).acquire_project_write(PROJECT_ID)
    assert type(lease) is ProjectWriteLease
    assert lease.project_id == PROJECT_ID
    assert lease.resource_key == _expected_key(PROJECT_ID)
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_process_registry_blocks_second_manager_before_adapter(monkeypatch, lease_root: Path):
    acquire_entered = threading.Event()
    acquire_continue = threading.Event()
    adapter = _RecordingAdapter(
        acquire_entered=acquire_entered,
        acquire_continue=acquire_continue,
    )
    registry_lock = _RegistryLockProbe()
    reservations = _GuardedReservations(registry_lock)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    monkeypatch.setattr(lease_module, "_PROCESS_REGISTRY_LOCK", registry_lock)
    monkeypatch.setattr(lease_module, "_PROCESS_RESERVATIONS", reservations)
    manager_a = _manager(lease_root)
    manager_b = _manager(lease_root)
    root = lease_root.stat()
    registry_key = (
        adapter.platform_key,
        root.st_dev,
        root.st_ino,
        _expected_key(RESOURCE_ID),
    )
    held = threading.Event()
    release = threading.Event()
    failures: list[BaseException] = []

    def hold() -> None:
        try:
            with manager_a.acquire(RESOURCE_ID):
                held.set()
                if not release.wait(timeout=5):
                    raise AssertionError("holder release handshake timed out")
        except Exception as exc:
            failures.append(exc)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        assert acquire_entered.wait(timeout=5)
        assert lease_module._PROCESS_RESERVATIONS is reservations
        assert set(reservations.snapshot()) == {registry_key}
        assert [key for _, key in reservations.mutations] == [registry_key]
        with pytest.raises(LeaseError) as caught:
            manager_b.acquire(RESOURCE_ID)
        _assert_lease_error(caught, LeaseErrorCode.CONTENDED)
        assert len(adapter.acquire_calls) == 1
        assert [key for _, key in reservations.mutations] == [registry_key]
        acquire_continue.set()
        assert held.wait(timeout=5)
    finally:
        acquire_continue.set()
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert failures == []
    assert reservations.snapshot() == {}
    assert [key for _, key in reservations.mutations] == [registry_key] * 2
    lease = manager_b.acquire(RESOURCE_ID)
    assert len(adapter.acquire_calls) == 2
    assert set(reservations.snapshot()) == {registry_key}
    assert [key for _, key in reservations.mutations] == [registry_key] * 3
    lease.release(owner_token=lease.owner_token)
    assert reservations.snapshot() == {}
    assert [key for _, key in reservations.mutations] == [registry_key] * 4
    assert registry_lock.acquire_calls >= 5


@POSIX_ONLY
def test_simultaneous_first_acquisitions_have_one_atomic_winner(monkeypatch, lease_root: Path):
    acquire_entered = threading.Event()
    acquire_continue = threading.Event()
    adapter = _RecordingAdapter(
        acquire_entered=acquire_entered,
        acquire_continue=acquire_continue,
    )
    registry_lock = _RegistryLockProbe()
    reservations = _GuardedReservations(registry_lock)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    monkeypatch.setattr(lease_module, "_PROCESS_REGISTRY_LOCK", registry_lock)
    monkeypatch.setattr(lease_module, "_PROCESS_RESERVATIONS", reservations)
    managers = (_manager(lease_root), _manager(lease_root))
    start = threading.Barrier(3)
    contended = threading.Event()
    held = threading.Event()
    release = threading.Event()
    outcomes: list[object] = []
    failures: list[BaseException] = []

    def acquire(manager: ResourceLeaseManager) -> None:
        try:
            start.wait(timeout=5)
            lease = manager.acquire(RESOURCE_ID)
        except LeaseError as exc:
            outcomes.append(exc.code)
            if exc.code is LeaseErrorCode.CONTENDED:
                contended.set()
        except BaseException as exc:
            failures.append(exc)
        else:
            outcomes.append("acquired")
            held.set()
            if not release.wait(timeout=5):
                failures.append(AssertionError("winner release handshake timed out"))
            lease.release(owner_token=lease.owner_token)

    threads = tuple(
        threading.Thread(target=acquire, args=(manager,), daemon=True) for manager in managers
    )
    for thread in threads:
        thread.start()
    try:
        start.wait(timeout=5)
        assert acquire_entered.wait(timeout=5)
        assert contended.wait(timeout=5)
        acquire_continue.set()
        assert held.wait(timeout=5)
    finally:
        acquire_continue.set()
        release.set()
        for thread in threads:
            thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert sorted(str(outcome) for outcome in outcomes) == ["acquired", "contended"]
    assert len(adapter.acquire_calls) == 1
    assert reservations.snapshot() == {}
    assert [operation for operation, _ in reservations.mutations] == ["set", "delete"]


@POSIX_ONLY
def test_other_process_may_publish_safe_first_lock_between_missing_probe_and_open(
    monkeypatch,
    lease_root: Path,
):
    original = lease_module._entry_path_state
    published = False

    def publish_after_missing(filename: str, root_fd: int):
        nonlocal published
        value = original(filename, root_fd)
        if value is None and not published:
            published = True
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            fd = os.open(filename, flags, 0o600, dir_fd=root_fd)
            try:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(root_fd)
        return value

    monkeypatch.setattr(lease_module, "_entry_path_state", publish_after_missing)
    manager = _manager(lease_root)

    lease = manager.acquire(RESOURCE_ID)

    assert published is True
    assert _lock_path(lease_root, RESOURCE_ID).is_file()
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_same_manager_reentrant_acquisition_is_contended(lease_root: Path):
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    with pytest.raises(LeaseError) as caught:
        manager.acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.CONTENDED)
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
def test_generic_and_project_write_apis_share_one_exclusion_domain(monkeypatch, lease_root: Path):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    generic_manager = _manager(lease_root)
    project_manager = _manager(lease_root)

    generic = generic_manager.acquire(PROJECT_ID)
    with pytest.raises(LeaseError) as project_contended:
        project_manager.acquire_project_write(PROJECT_ID)
    _assert_lease_error(project_contended, LeaseErrorCode.CONTENDED)
    assert len(adapter.acquire_calls) == 1
    generic.release(owner_token=generic.owner_token)

    project = project_manager.acquire_project_write(PROJECT_ID)
    with pytest.raises(LeaseError) as generic_contended:
        generic_manager.acquire(PROJECT_ID)
    _assert_lease_error(generic_contended, LeaseErrorCode.CONTENDED)
    assert len(adapter.acquire_calls) == 2
    project.release(owner_token=project.owner_token)


@POSIX_ONLY
def test_different_resources_and_roots_are_independent(tmp_path: Path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir(mode=0o700)
    root_b.mkdir(mode=0o700)
    manager_a = _manager(root_a)
    manager_b = _manager(root_a)
    manager_c = _manager(root_b)
    lease_a = manager_a.acquire(RESOURCE_ID)
    lease_b = manager_b.acquire(OTHER_RESOURCE_ID)
    lease_c = manager_c.acquire(RESOURCE_ID)
    lease_c.release(owner_token=lease_c.owner_token)
    lease_b.release(owner_token=lease_b.owner_token)
    lease_a.release(owner_token=lease_a.owner_token)


@POSIX_ONLY
def test_process_registry_key_includes_adapter_platform(monkeypatch, lease_root: Path):
    adapter_a = _RecordingAdapter()
    adapter_b = _RecordingAdapter()
    adapter_a.platform_key = "fake-platform-a"
    adapter_b.platform_key = "fake-platform-b"
    adapters = iter((adapter_a, adapter_b))
    real_drop = lease_module._drop_process_reservation
    dropped_keys: list[object] = []

    def drop_process_reservation(key, owner_token: str) -> None:
        dropped_keys.append(key)
        real_drop(key, owner_token)

    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: next(adapters))
    monkeypatch.setattr(
        lease_module,
        "_drop_process_reservation",
        drop_process_reservation,
    )
    manager_a = _manager(lease_root)
    manager_b = _manager(lease_root)
    lease_a = manager_a.acquire(RESOURCE_ID)
    lease_b = manager_b.acquire(RESOURCE_ID)
    assert len(adapter_a.acquire_calls) == 1
    assert len(adapter_b.acquire_calls) == 1
    lease_b.release(owner_token=lease_b.owner_token)
    lease_a.release(owner_token=lease_a.owner_token)
    root = lease_root.stat()
    resource_key = _expected_key(RESOURCE_ID)
    assert dropped_keys == [
        (adapter_b.platform_key, root.st_dev, root.st_ino, resource_key),
        (adapter_a.platform_key, root.st_dev, root.st_ino, resource_key),
    ]


@POSIX_ONLY
def test_process_registry_key_uses_root_inode_across_rename(monkeypatch, tmp_path: Path):
    root = tmp_path / "original-root"
    root.mkdir(mode=0o700)
    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    manager_before = _manager(root)
    lease = manager_before.acquire(RESOURCE_ID)
    moved = tmp_path / "moved-root"
    root.rename(moved)
    manager_after = _manager(moved)
    with pytest.raises(LeaseError) as caught:
        manager_after.acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.CONTENDED)
    assert len(adapter.acquire_calls) == 1
    lease.release(owner_token=lease.owner_token)
    reacquired = manager_after.acquire(RESOURCE_ID)
    assert len(adapter.acquire_calls) == 2
    reacquired.release(owner_token=reacquired.owner_token)


@POSIX_ONLY
def test_adapter_acquire_failure_rolls_back_process_reservation(monkeypatch, lease_root: Path):
    path = _lock_path(lease_root, RESOURCE_ID)
    contents = b"persistent-before-adapter-error"
    path.write_bytes(contents)
    path.chmod(0o600)
    inode = path.stat().st_ino
    adapter = _RecordingAdapter(acquire_error=LeaseErrorCode.IO_ERROR)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    real_drop = lease_module._drop_process_reservation
    closed: list[int] = []
    dropped: list[str] = []

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)
        if len(closed) == 1:
            raise OSError(errno.EIO, "sensitive acquire cleanup close details")

    def drop_process_reservation(key, owner_token: str) -> None:
        dropped.append(owner_token)
        real_drop(key, owner_token)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    monkeypatch.setattr(lease_module, "_drop_process_reservation", drop_process_reservation)
    manager = _manager(lease_root)
    with pytest.raises(LeaseError) as caught:
        manager.acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.IO_ERROR)
    assert closed == [adapter.acquire_calls[0]]
    assert len(dropped) == 1
    assert adapter.release_calls == []
    assert path.stat().st_ino == inode
    assert path.read_bytes() == contents
    lease = manager.acquire(RESOURCE_ID)
    assert len(adapter.acquire_calls) == 2
    assert path.stat().st_ino == inode
    assert path.read_bytes() == contents
    lease.release(owner_token=lease.owner_token)
    assert len(dropped) == 2


@POSIX_ONLY
@pytest.mark.parametrize("owner", [None, 1, True, "short", "g" * 64, "A" * 64])
def test_release_rejects_invalid_owner_tokens(monkeypatch, lease_root: Path, owner):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    closed: list[int] = []

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    for release in (
        lambda: lease.release(owner_token=owner),
        lambda: manager.release(lease, owner_token=owner),
    ):
        with pytest.raises(LeaseError) as caught:
            release()
        _assert_lease_error(caught, LeaseErrorCode.INVALID_OWNER)
    assert not lease.released
    assert adapter.release_calls == []
    assert closed == []
    lease.release(owner_token=lease.owner_token)
    assert len(adapter.release_calls) == 1
    assert len(closed) == 1


@POSIX_ONLY
def test_owner_token_requires_exact_string_before_release_work(monkeypatch, lease_root: Path):
    class OwnerString(str):
        pass

    class OwnerProxy:
        @property
        def __class__(self):
            return str

    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    closed: list[int] = []

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    for owner in (OwnerString(lease.owner_token), OwnerProxy()):
        with pytest.raises(LeaseError) as lease_error:
            lease.release(owner_token=owner)
        _assert_lease_error(lease_error, LeaseErrorCode.INVALID_OWNER)
        with pytest.raises(LeaseError) as manager_error:
            manager.release(lease, owner_token=owner)
        _assert_lease_error(manager_error, LeaseErrorCode.INVALID_OWNER)
    assert adapter.release_calls == []
    assert closed == []
    lease.release(owner_token=lease.owner_token)
    assert len(adapter.release_calls) == 1
    assert len(closed) == 1


@POSIX_ONLY
def test_wrong_owner_cannot_unlock_or_close(monkeypatch, lease_root: Path):
    real_unlock = lease_module._PosixFileLock.release
    unlock_calls: list[int] = []

    def recording_unlock(adapter, fd: int) -> None:
        unlock_calls.append(fd)
        real_unlock(adapter, fd)

    monkeypatch.setattr(lease_module._PosixFileLock, "release", recording_unlock)
    real_close = lease_module._close_lock_fd
    closed: list[int] = []

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    wrong_owner = _wrong_token(lease.owner_token)
    for release in (
        lambda: lease.release(owner_token=wrong_owner),
        lambda: manager.release(lease, owner_token=wrong_owner),
    ):
        with pytest.raises(LeaseError) as caught:
            release()
        error = _assert_lease_error(caught, LeaseErrorCode.WRONG_OWNER)
        assert wrong_owner not in str(error)
        assert wrong_owner[:16] not in str(error)
    assert unlock_calls == []
    assert closed == []
    deadline = time.monotonic() + 5
    contended = _run_child_try(lease_root, RESOURCE_ID, deadline)
    assert contended.returncode == 3, contended.stderr
    assert _parse_child_lines(contended.stdout)[0]["code"] == LeaseErrorCode.CONTENDED.value
    manager.release(lease, owner_token=lease.owner_token)
    assert len(unlock_calls) == 1
    assert len(closed) == 1


@POSIX_ONLY
def test_wrong_manager_and_nonlease_are_rejected_before_release(monkeypatch, lease_root: Path):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    closed: list[int] = []

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    manager_a = _manager(lease_root)
    manager_b = _manager(lease_root)
    lease = manager_a.acquire(RESOURCE_ID)
    with pytest.raises(LeaseError) as wrong_manager:
        manager_b.release(lease, owner_token=lease.owner_token)
    _assert_lease_error(wrong_manager, LeaseErrorCode.INVALID_LEASE)
    with pytest.raises(LeaseError) as nonlease:
        manager_a.release(object(), owner_token=lease.owner_token)
    _assert_lease_error(nonlease, LeaseErrorCode.INVALID_LEASE)

    class LeaseProxy:
        @property
        def __class__(self):
            return ResourceLease

    with pytest.raises(LeaseError) as proxy:
        manager_a.release(LeaseProxy(), owner_token=lease.owner_token)
    _assert_lease_error(proxy, LeaseErrorCode.INVALID_LEASE)
    for forged in (object.__new__(ResourceLease), object.__new__(ProjectWriteLease)):
        with pytest.raises(LeaseError) as forged_error:
            manager_a.release(forged, owner_token=lease.owner_token)
        _assert_lease_error(forged_error, LeaseErrorCode.INVALID_LEASE)
    assert adapter.release_calls == []
    assert closed == []
    lease.release(owner_token=lease.owner_token)
    assert len(adapter.release_calls) == 1
    assert len(closed) == 1


def test_public_lease_types_cannot_be_constructed_or_forged_directly():
    with pytest.raises(TypeError):
        ResourceLease()
    with pytest.raises(TypeError):
        ProjectWriteLease()


@POSIX_ONLY
def test_double_release_is_stable_and_unlocks_once(monkeypatch, lease_root: Path):
    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    closed: list[int] = []

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    lease.release(owner_token=lease.owner_token)
    with pytest.raises(LeaseError) as caught:
        lease.release(owner_token=lease.owner_token)
    _assert_lease_error(caught, LeaseErrorCode.ALREADY_RELEASED)
    with pytest.raises(LeaseError) as manager_caught:
        manager.release(lease, owner_token=lease.owner_token)
    _assert_lease_error(manager_caught, LeaseErrorCode.ALREADY_RELEASED)
    assert len(adapter.release_calls) == 1
    assert len(closed) == 1


@POSIX_ONLY
def test_context_manager_releases_normally_and_after_same_exception(lease_root: Path):
    manager = _manager(lease_root)
    acquired = manager.acquire(RESOURCE_ID)
    with acquired as normal:
        assert normal is acquired
        assert not normal.released
    assert normal.released

    marker = RuntimeError("body failed")
    with pytest.raises(RuntimeError) as caught:
        with manager.acquire(RESOURCE_ID) as failed:
            raise marker
    assert caught.value is marker
    assert failed.released
    reacquired = manager.acquire(RESOURCE_ID)
    reacquired.release(owner_token=reacquired.owner_token)


@POSIX_ONLY
def test_parallel_tokens_are_unique_and_public_fields_are_immutable(lease_root: Path):
    manager = _manager(lease_root)
    first = manager.acquire(RESOURCE_ID)
    second = manager.acquire(OTHER_RESOURCE_ID)
    project = manager.acquire_project_write(PROJECT_ID)
    assert len({first.owner_token, second.owner_token, project.owner_token}) == 3
    for lease, name, value in (
        (first, "resource_key", "0" * 64),
        (first, "owner_token", "0" * 64),
        (first, "released", True),
        (project, "project_id", OTHER_PROJECT_ID),
    ):
        with pytest.raises((AttributeError, TypeError)):
            setattr(lease, name, value)
    assert not first.released
    assert not second.released
    assert not project.released
    project.release(owner_token=project.owner_token)
    second.release(owner_token=second.owner_token)
    first.release(owner_token=first.owner_token)


@POSIX_ONLY
def test_release_error_closes_fd_deregisters_and_preserves_inode(monkeypatch, lease_root: Path):
    events: list[tuple[str, int]] = []
    adapter = _RecordingAdapter(release_error=LeaseErrorCode.IO_ERROR, events=events)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    real_drop = lease_module._drop_process_reservation

    def close_lock_fd(fd: int) -> None:
        events.append(("close", fd))
        real_close(fd)

    def drop_process_reservation(key, owner_token: str) -> None:
        events.append(("deregister", adapter.acquire_calls[-1]))
        real_drop(key, owner_token)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    monkeypatch.setattr(lease_module, "_drop_process_reservation", drop_process_reservation)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    path = _lock_path(lease_root, RESOURCE_ID)
    inode = path.stat().st_ino
    fd = adapter.acquire_calls[-1]
    with pytest.raises(LeaseError) as caught:
        lease.release(owner_token=lease.owner_token)
    _assert_lease_error(caught, LeaseErrorCode.IO_ERROR)
    assert lease.released
    assert events[-3:] == [("unlock", fd), ("close", fd), ("deregister", fd)]
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF
    assert path.is_file()
    assert path.stat().st_ino == inode
    calls_before_repeat = (
        list(adapter.acquire_calls),
        list(adapter.release_calls),
        list(events),
    )
    with pytest.raises(LeaseError) as repeated:
        manager.release(lease, owner_token=lease.owner_token)
    _assert_lease_error(repeated, LeaseErrorCode.ALREADY_RELEASED)
    assert (
        adapter.acquire_calls,
        adapter.release_calls,
        events,
    ) == calls_before_repeat
    reacquired = manager.acquire(RESOURCE_ID)
    assert path.stat().st_ino == inode
    reacquired.release(owner_token=reacquired.owner_token)


@POSIX_ONLY
def test_successful_release_orders_unlock_close_then_deregister(monkeypatch, lease_root: Path):
    events: list[tuple[str, int]] = []
    adapter = _RecordingAdapter(events=events)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    real_drop = lease_module._drop_process_reservation

    def close_lock_fd(fd: int) -> None:
        events.append(("close", fd))
        real_close(fd)

    def drop_process_reservation(key, owner_token: str) -> None:
        events.append(("deregister", adapter.acquire_calls[-1]))
        real_drop(key, owner_token)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    monkeypatch.setattr(lease_module, "_drop_process_reservation", drop_process_reservation)
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    fd = adapter.acquire_calls[-1]
    lease.release(owner_token=lease.owner_token)
    assert lease.released
    assert events[-3:] == [("unlock", fd), ("close", fd), ("deregister", fd)]


@POSIX_ONLY
def test_close_failure_still_deregisters_and_allows_reacquisition(monkeypatch, lease_root: Path):
    events: list[tuple[str, int]] = []
    adapter = _RecordingAdapter(events=events)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    real_drop = lease_module._drop_process_reservation
    close_calls = 0

    def close_lock_fd(fd: int) -> None:
        nonlocal close_calls
        close_calls += 1
        events.append(("close", fd))
        real_close(fd)
        if close_calls == 1:
            raise OSError(errno.EIO, "sensitive close details")

    def drop_process_reservation(key, owner_token: str) -> None:
        events.append(("deregister", adapter.acquire_calls[-1]))
        real_drop(key, owner_token)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    monkeypatch.setattr(lease_module, "_drop_process_reservation", drop_process_reservation)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    path = _lock_path(lease_root, RESOURCE_ID)
    inode = path.stat().st_ino
    fd = adapter.acquire_calls[-1]
    with pytest.raises(LeaseError) as caught:
        lease.release(owner_token=lease.owner_token)
    error = _assert_lease_error(caught, LeaseErrorCode.IO_ERROR)
    assert "sensitive close details" not in str(error)
    assert lease.released
    assert events[-3:] == [("unlock", fd), ("close", fd), ("deregister", fd)]
    calls_before_repeat = (
        list(adapter.acquire_calls),
        list(adapter.release_calls),
        list(events),
        close_calls,
    )
    for release in (
        lambda: lease.release(owner_token=lease.owner_token),
        lambda: manager.release(lease, owner_token=lease.owner_token),
    ):
        with pytest.raises(LeaseError) as repeated:
            release()
        _assert_lease_error(repeated, LeaseErrorCode.ALREADY_RELEASED)
    assert (
        adapter.acquire_calls,
        adapter.release_calls,
        events,
        close_calls,
    ) == calls_before_repeat
    reacquired = manager.acquire(RESOURCE_ID)
    assert path.stat().st_ino == inode
    reacquired.release(owner_token=reacquired.owner_token)


@POSIX_ONLY
def test_release_keeps_lock_entry_and_inode(lease_root: Path):
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    path = _lock_path(lease_root, RESOURCE_ID)
    inode = path.stat().st_ino
    lease.release(owner_token=lease.owner_token)
    assert path.is_file()
    assert path.stat().st_ino == inode
    with pytest.raises(RuntimeError):
        with manager.acquire(RESOURCE_ID):
            raise RuntimeError("context failed")
    assert path.is_file()
    assert path.stat().st_ino == inode


@POSIX_ONLY
@pytest.mark.parametrize(
    "contents",
    [
        b"pid=999999 expires=4102444800 arbitrary diagnostics",
        b"pid=1 expires=0 stale=true",
        b"\x00\xffarbitrary-binary-owner-text",
        json.dumps(
            {"pid": os.getpid(), "expires": 0, "owner_token": "sensitive-owner"},
            sort_keys=True,
        ).encode("utf-8"),
    ],
    ids=("future", "expired", "binary", "json-current-pid"),
)
def test_preexisting_regular_contents_are_non_authoritative(lease_root: Path, contents: bytes):
    path = _lock_path(lease_root, RESOURCE_ID)
    path.write_bytes(contents)
    path.chmod(0o600)
    os.utime(path, (1, 1), follow_symlinks=False)
    before = path.stat()
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    lease.release(owner_token=lease.owner_token)
    assert path.read_bytes() == contents
    after = path.stat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns


@POSIX_ONLY
def test_symlink_lock_entry_does_not_touch_target(lease_root: Path, tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(b"sentinel")
    target_before = target.stat()
    path = _lock_path(lease_root, RESOURCE_ID)
    path.symlink_to(target)
    link_before = path.lstat()
    with pytest.raises(LeaseError) as caught:
        _manager(lease_root).acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    assert target.read_bytes() == b"sentinel"
    target_after = target.stat()
    assert (
        target_after.st_dev,
        target_after.st_ino,
        target_after.st_mode,
        target_after.st_nlink,
        target_after.st_size,
        target_after.st_mtime_ns,
    ) == (
        target_before.st_dev,
        target_before.st_ino,
        target_before.st_mode,
        target_before.st_nlink,
        target_before.st_size,
        target_before.st_mtime_ns,
    )
    assert path.is_symlink()
    link_after = path.lstat()
    assert (link_after.st_dev, link_after.st_ino, link_after.st_mode) == (
        link_before.st_dev,
        link_before.st_ino,
        link_before.st_mode,
    )


@POSIX_ONLY
def test_directory_lock_entry_is_rejected(lease_root: Path):
    path = _lock_path(lease_root, RESOURCE_ID)
    path.mkdir(mode=0o700)
    before = path.stat()
    with pytest.raises(LeaseError) as caught:
        _manager(lease_root).acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    after = path.stat()
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    assert list(path.iterdir()) == []


@POSIX_ONLY
def test_fifo_lock_entry_is_rejected_without_blocking(lease_root: Path):
    path = _lock_path(lease_root, RESOURCE_ID)
    os.mkfifo(path, mode=0o600)
    before = path.lstat()
    with pytest.raises(LeaseError) as caught:
        _manager(lease_root).acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    after = path.lstat()
    assert stat.S_ISFIFO(after.st_mode)
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)


@POSIX_ONLY
def test_socket_lock_entry_is_rejected(tmp_path: Path):
    suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:12]
    short_root = Path(os.path.realpath("/tmp")) / f"vc-{suffix}"
    path = _lock_path(short_root, RESOURCE_ID)
    server = None
    root_created = False
    try:
        short_root.mkdir(mode=0o700)
        root_created = True
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        before = path.lstat()
        with pytest.raises(LeaseError) as caught:
            _manager(short_root).acquire(RESOURCE_ID)
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
        after = path.lstat()
        assert stat.S_ISSOCK(after.st_mode)
        assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    finally:
        if server is not None:
            server.close()
        if root_created and os.path.lexists(path):
            path.unlink()
        if root_created:
            short_root.rmdir()


@POSIX_ONLY
def test_hardlinked_lock_entry_is_rejected_without_target_change(lease_root: Path, tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(b"sentinel")
    target.chmod(0o600)
    os.link(target, _lock_path(lease_root, RESOURCE_ID))
    target_before = target.stat()
    with pytest.raises(LeaseError) as caught:
        _manager(lease_root).acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    assert target.read_bytes() == b"sentinel"
    target_after = target.stat()
    assert (
        target_after.st_dev,
        target_after.st_ino,
        target_after.st_mode,
        target_after.st_nlink,
        target_after.st_size,
        target_after.st_mtime_ns,
    ) == (
        target_before.st_dev,
        target_before.st_ino,
        target_before.st_mode,
        target_before.st_nlink,
        target_before.st_size,
        target_before.st_mtime_ns,
    )


@POSIX_ONLY
@pytest.mark.parametrize("unsafe_mode", [0o644, 0o640], ids=("world-readable", "group-readable"))
def test_unsafe_lock_entry_mode_is_rejected_without_repair(lease_root: Path, unsafe_mode: int):
    path = _lock_path(lease_root, RESOURCE_ID)
    contents = b"existing-sensitive-content"
    path.write_bytes(contents)
    path.chmod(unsafe_mode)
    inode = path.stat().st_ino
    with pytest.raises(LeaseError) as caught:
        _manager(lease_root).acquire(RESOURCE_ID)
    _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    result = path.stat()
    assert result.st_ino == inode
    assert stat.S_IMODE(result.st_mode) == unsafe_mode
    assert path.read_bytes() == contents


@POSIX_ONLY
def test_posix_open_flags_are_fail_closed(monkeypatch, lease_root: Path):
    real_open = lease_module.os.open
    real_stat = lease_module.os.stat
    real_fstat = lease_module.os.fstat
    real_set_inheritable = lease_module.os.set_inheritable
    real_adapter_acquire = lease_module._PosixFileLock.acquire
    calls: list[
        tuple[
            object,
            int,
            int,
            int | None,
            tuple[int, int] | None,
            int,
            os.stat_result,
        ]
    ] = []
    locked_fds: list[int] = []
    inheritable_changes: list[tuple[int, bool]] = []
    root_stat = lease_root.stat()
    expected_root_identity = (root_stat.st_dev, root_stat.st_ino)
    expected_filename = f"{_expected_key(RESOURCE_ID)}.lock"
    entry_existed_before_open: list[bool] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        parent_identity = None
        if dir_fd is not None:
            parent = real_fstat(dir_fd)
            parent_identity = (parent.st_dev, parent.st_ino)
        decoded = os.fsdecode(os.fspath(path))
        if decoded == expected_filename and parent_identity == expected_root_identity:
            existed = True
            try:
                real_stat(path, dir_fd=dir_fd, follow_symlinks=False)
            except FileNotFoundError:
                existed = False
            entry_existed_before_open.append(existed)
        result = real_open(path, flags, mode, dir_fd=dir_fd)
        opened = real_fstat(result)
        assert not os.get_inheritable(result)
        calls.append((path, flags, mode, dir_fd, parent_identity, result, opened))
        return result

    def recording_set_inheritable(fd: int, inheritable: bool) -> None:
        inheritable_changes.append((fd, inheritable))
        assert inheritable is False
        real_set_inheritable(fd, inheritable)

    def recording_adapter_acquire(adapter, fd: int) -> None:
        locked_fds.append(fd)
        real_adapter_acquire(adapter, fd)

    monkeypatch.setattr(lease_module.os, "open", recording_open)
    monkeypatch.setattr(lease_module.os, "set_inheritable", recording_set_inheritable)
    monkeypatch.setattr(lease_module._PosixFileLock, "acquire", recording_adapter_acquire)
    monkeypatch.setattr(
        lease_module.os,
        "supports_dir_fd",
        set(lease_module.os.supports_dir_fd) | {recording_open},
    )
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    assert len(locked_fds) == 1
    assert not os.get_inheritable(locked_fds[0])
    lease.release(owner_token=lease.owner_token)
    required_root = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_calls = [item for item in calls if stat.S_ISDIR(item[6].st_mode)]
    assert directory_calls
    assert all(
        flags & required_root == required_root for _, flags, _, _, _, _, _ in directory_calls
    )
    assert all(flags & os.O_ACCMODE == os.O_RDONLY for _, flags, _, _, _, _, _ in directory_calls)
    expected_walk: list[tuple[str, tuple[int, int] | None]] = [("/", None)]
    parent_path = Path("/")
    for part in lease_root.parts[1:]:
        parent_stat = parent_path.stat()
        expected_walk.append((str(part), (parent_stat.st_dev, parent_stat.st_ino)))
        parent_path /= part
    observed_walk = [
        (os.fsdecode(os.fspath(path)), parent_identity)
        for path, _, _, _, parent_identity, _, _ in directory_calls
    ]
    assert len(observed_walk) % len(expected_walk) == 0
    assert all(
        observed_walk[index : index + len(expected_walk)] == expected_walk
        for index in range(0, len(observed_walk), len(expected_walk))
    )
    for index in range(0, len(directory_calls), len(expected_walk)):
        walk = directory_calls[index : index + len(expected_walk)]
        assert os.fsdecode(os.fspath(walk[0][0])) == "/"
        assert walk[0][3] is None
        assert all(
            item[3] is not None and not Path(os.fsdecode(os.fspath(item[0]))).is_absolute()
            for item in walk[1:]
        )
        assert all(
            current[3] == previous[5] for previous, current in zip(walk, walk[1:], strict=False)
        )
    required_entry = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    regular_calls = [item for item in calls if stat.S_ISREG(item[6].st_mode)]
    entry_calls = [
        item
        for item in regular_calls
        if stat.S_ISREG(item[6].st_mode)
        and os.fsdecode(os.fspath(item[0])) == expected_filename
        and item[4] == expected_root_identity
    ]
    assert entry_calls
    assert regular_calls == entry_calls
    assert entry_existed_before_open == [False]
    assert all(flags & required_entry == required_entry for _, flags, _, _, _, _, _ in entry_calls)
    assert all(flags & os.O_ACCMODE == os.O_RDWR for _, flags, _, _, _, _, _ in entry_calls)
    assert all(flags & (os.O_TRUNC | os.O_APPEND) == 0 for _, flags, _, _, _, _, _ in entry_calls)
    assert all(mode == 0o600 for _, _, mode, _, _, _, _ in entry_calls)
    assert all(dir_fd is not None for _, _, _, dir_fd, _, _, _ in entry_calls)
    assert all(
        not Path(os.fsdecode(os.fspath(path))).is_absolute()
        for path, _, _, _, _, _, _ in entry_calls
    )
    assert all(item[4] == expected_root_identity for item in entry_calls)
    for entry in entry_calls:
        entry_index = calls.index(entry)
        prior_directories = [item for item in calls[:entry_index] if stat.S_ISDIR(item[6].st_mode)]
        assert prior_directories
        assert entry[3] == prior_directories[-1][5]
    assert all(inheritable is False for _, inheritable in inheritable_changes)
    assert stat.S_IMODE(_lock_path(lease_root, RESOURCE_ID).stat().st_mode) == 0o600


@POSIX_ONLY
@pytest.mark.parametrize("identity_field", ["device", "inode"])
@pytest.mark.parametrize(
    ("phase", "stat_kind"),
    [
        ("pre", "path"),
        ("pre", "fd"),
        ("post", "path"),
        ("post", "fd"),
    ],
)
def test_entry_path_fd_identity_mismatch_fails_and_cleans_up(
    monkeypatch, lease_root: Path, identity_field: str, phase: str, stat_kind: str
):
    path = _lock_path(lease_root, RESOURCE_ID)
    contents = b"persistent-lock-entry"
    path.write_bytes(contents)
    path.chmod(0o600)
    inode = path.stat().st_ino
    events: list[tuple[str, int]] = []
    adapter = _RecordingAdapter(events=events)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    real_drop = lease_module._drop_process_reservation
    closed: list[int] = []

    def close_lock_fd(fd: int) -> None:
        events.append(("close", fd))
        closed.append(fd)
        real_close(fd)

    def drop_process_reservation(key, owner_token: str) -> None:
        event_fd = adapter.acquire_calls[-1] if adapter.acquire_calls else -1
        events.append(("deregister", event_fd))
        real_drop(key, owner_token)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    monkeypatch.setattr(lease_module, "_drop_process_reservation", drop_process_reservation)
    real_stat = lease_module.os.stat
    real_fstat = lease_module.os.fstat
    hits = 0
    filename = f"{_expected_key(RESOURCE_ID)}.lock"

    def swapped_stat(path, *args, **kwargs):
        nonlocal hits
        result = real_stat(path, *args, **kwargs)
        after_lock = bool(adapter.acquire_calls)
        if (
            stat_kind == "path"
            and os.fsdecode(os.fspath(path)) == filename
            and kwargs.get("dir_fd") is not None
            and after_lock == (phase == "post")
            and hits == 0
        ):
            hits += 1
            values = list(result)
            values[2 if identity_field == "device" else 1] += 1
            return os.stat_result(values)
        return result

    def swapped_fstat(fd: int):
        nonlocal hits
        result = real_fstat(fd)
        after_lock = bool(adapter.acquire_calls)
        if (
            stat_kind == "fd"
            and stat.S_ISREG(result.st_mode)
            and after_lock == (phase == "post")
            and hits == 0
        ):
            hits += 1
            values = list(result)
            values[2 if identity_field == "device" else 1] += 1
            return os.stat_result(values)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(lease_module.os, "stat", swapped_stat)
        patch.setattr(lease_module.os, "fstat", swapped_fstat)
        patch.setattr(
            lease_module.os,
            "supports_dir_fd",
            set(lease_module.os.supports_dir_fd) | {swapped_stat},
        )
        patch.setattr(
            lease_module.os,
            "supports_follow_symlinks",
            set(lease_module.os.supports_follow_symlinks) | {swapped_stat},
        )
        with pytest.raises(LeaseError) as caught:
            _manager(lease_root).acquire(RESOURCE_ID)
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    assert hits == 1
    if phase == "pre":
        assert adapter.acquire_calls == []
        assert adapter.release_calls == []
        assert len(closed) == (0 if stat_kind == "path" else 1)
    else:
        assert len(adapter.acquire_calls) == 1
        assert len(adapter.release_calls) == 1
        assert closed == [adapter.acquire_calls[0]]
        fd = adapter.acquire_calls[0]
        assert events[-3:] == [("unlock", fd), ("close", fd), ("deregister", fd)]
    assert path.is_file()
    assert path.stat().st_ino == inode
    assert path.read_bytes() == contents
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    assert path.stat().st_ino == inode
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
@pytest.mark.parametrize(
    ("phase", "stat_kind"),
    [("pre", "path"), ("pre", "fd"), ("post", "path"), ("post", "fd")],
)
def test_lock_entry_requires_current_euid_before_and_after_locking(
    monkeypatch, lease_root: Path, phase: str, stat_kind: str
):
    path = _lock_path(lease_root, RESOURCE_ID)
    contents = b"owned-entry"
    path.write_bytes(contents)
    path.chmod(0o600)
    inode = path.stat().st_ino
    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    closed: list[int] = []

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    real_stat = lease_module.os.stat
    real_fstat = lease_module.os.fstat
    filename = path.name
    hits = 0

    def wrong_owner(result):
        values = list(result)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    def recording_stat(path_value, *args, **kwargs):
        nonlocal hits
        result = real_stat(path_value, *args, **kwargs)
        after_lock = bool(adapter.acquire_calls)
        if (
            stat_kind == "path"
            and os.fsdecode(os.fspath(path_value)) == filename
            and kwargs.get("dir_fd") is not None
            and after_lock == (phase == "post")
            and hits == 0
        ):
            hits += 1
            return wrong_owner(result)
        return result

    def recording_fstat(fd: int):
        nonlocal hits
        result = real_fstat(fd)
        after_lock = bool(adapter.acquire_calls)
        if (
            stat_kind == "fd"
            and stat.S_ISREG(result.st_mode)
            and after_lock == (phase == "post")
            and hits == 0
        ):
            hits += 1
            return wrong_owner(result)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(lease_module.os, "stat", recording_stat)
        patch.setattr(lease_module.os, "fstat", recording_fstat)
        patch.setattr(
            lease_module.os,
            "supports_dir_fd",
            set(lease_module.os.supports_dir_fd) | {recording_stat},
        )
        patch.setattr(
            lease_module.os,
            "supports_follow_symlinks",
            set(lease_module.os.supports_follow_symlinks) | {recording_stat},
        )
        with pytest.raises(LeaseError) as caught:
            _manager(lease_root).acquire(RESOURCE_ID)
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    assert hits >= 1
    if phase == "pre":
        assert adapter.acquire_calls == []
        assert adapter.release_calls == []
        assert len(closed) == (0 if stat_kind == "path" else 1)
    else:
        assert len(adapter.acquire_calls) == 1
        assert len(adapter.release_calls) == 1
        assert closed == [adapter.acquire_calls[0]]
    assert path.stat().st_ino == inode
    assert path.read_bytes() == contents
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    assert path.stat().st_ino == inode
    lease.release(owner_token=lease.owner_token)


@POSIX_ONLY
@pytest.mark.parametrize("phase", ["pre", "post"])
@pytest.mark.parametrize(
    ("field", "stat_kind"),
    [
        ("mode", "path"),
        ("mode", "fd"),
        ("nlink", "path"),
        ("nlink", "fd"),
        ("kind", "path"),
        ("kind", "fd"),
    ],
)
def test_entry_metadata_is_validated_on_path_and_fd_before_and_after_lock(
    monkeypatch, lease_root: Path, phase: str, field: str, stat_kind: str
):
    path = _lock_path(lease_root, RESOURCE_ID)
    contents = b"metadata-entry"
    path.write_bytes(contents)
    path.chmod(0o600)
    inode = path.stat().st_ino
    adapter = _RecordingAdapter()
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    real_close = lease_module._close_lock_fd
    real_stat = lease_module.os.stat
    real_fstat = lease_module.os.fstat
    closed: list[int] = []
    filename = path.name
    hits = 0

    def close_lock_fd(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def unsafe_metadata(result):
        values = list(result)
        if field == "mode":
            values[0] = stat.S_IFREG | 0o640
        elif field == "nlink":
            values[3] = 2
        else:
            values[0] = stat.S_IFIFO | 0o600
        return os.stat_result(values)

    def recording_stat(path_value, *args, **kwargs):
        nonlocal hits
        result = real_stat(path_value, *args, **kwargs)
        after_lock = bool(adapter.acquire_calls)
        if (
            stat_kind == "path"
            and os.fsdecode(os.fspath(path_value)) == filename
            and kwargs.get("dir_fd") is not None
            and after_lock == (phase == "post")
            and hits == 0
        ):
            hits += 1
            return unsafe_metadata(result)
        return result

    def recording_fstat(fd: int):
        nonlocal hits
        result = real_fstat(fd)
        after_lock = bool(adapter.acquire_calls)
        if (
            stat_kind == "fd"
            and stat.S_ISREG(result.st_mode)
            and after_lock == (phase == "post")
            and hits == 0
        ):
            hits += 1
            return unsafe_metadata(result)
        return result

    monkeypatch.setattr(lease_module, "_close_lock_fd", close_lock_fd)
    with monkeypatch.context() as patch:
        patch.setattr(lease_module.os, "stat", recording_stat)
        patch.setattr(lease_module.os, "fstat", recording_fstat)
        patch.setattr(
            lease_module.os,
            "supports_dir_fd",
            set(lease_module.os.supports_dir_fd) | {recording_stat},
        )
        patch.setattr(
            lease_module.os,
            "supports_follow_symlinks",
            set(lease_module.os.supports_follow_symlinks) | {recording_stat},
        )
        with pytest.raises(LeaseError) as caught:
            _manager(lease_root).acquire(RESOURCE_ID)
        _assert_lease_error(caught, LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    assert hits == 1
    if phase == "pre":
        assert adapter.acquire_calls == []
        assert adapter.release_calls == []
        assert len(closed) == (0 if stat_kind == "path" else 1)
    else:
        assert len(adapter.acquire_calls) == 1
        assert len(adapter.release_calls) == 1
        assert closed == [adapter.acquire_calls[0]]
    assert path.stat().st_ino == inode
    assert path.read_bytes() == contents
    lease = _manager(lease_root).acquire(RESOURCE_ID)
    lease.release(owner_token=lease.owner_token)


def _capable_fake_posix_os():
    def fake_open(*_args, **_kwargs):
        return 17

    def fake_stat(*_args, **_kwargs):
        return object()

    return SimpleNamespace(
        O_RDONLY=0,
        O_DIRECTORY=1,
        O_NOFOLLOW=2,
        O_CLOEXEC=4,
        O_NONBLOCK=8,
        O_RDWR=16,
        O_CREAT=32,
        open=fake_open,
        stat=fake_stat,
        fstat=lambda *_args, **_kwargs: object(),
        fsync=lambda *_args, **_kwargs: None,
        geteuid=lambda: 501,
        getpid=lambda: 1234,
        register_at_fork=lambda **_kwargs: None,
        supports_dir_fd={fake_open, fake_stat},
        supports_follow_symlinks={fake_stat},
    )


def test_complete_posix_capability_profile_is_accepted():
    assert lease_module._require_posix_capabilities(_capable_fake_posix_os()) is None
    for attribute in ("open", "stat"):
        fake_os = _capable_fake_posix_os()
        advertised = getattr(fake_os, attribute)

        def same_name_decoy(*_args, **_kwargs):
            raise AssertionError("decoy capability must never be called")

        same_name_decoy.__name__ = advertised.__name__
        setattr(fake_os, attribute, same_name_decoy)
        with pytest.raises(LeaseError) as caught:
            lease_module._require_posix_capabilities(fake_os)
        _assert_lease_error(caught, LeaseErrorCode.LOCK_UNAVAILABLE)


@POSIX_ONLY
def test_manager_propagates_capability_failure_before_root_work(monkeypatch, lease_root: Path):
    checked: list[object] = []
    touched: list[str] = []

    def unavailable(os_module) -> None:
        checked.append(os_module)
        raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)

    def storage_work(*_args, **_kwargs):
        touched.append("storage")
        raise AssertionError("capability failure performed root work")

    monkeypatch.setattr(lease_module, "_require_posix_capabilities", unavailable)
    with monkeypatch.context() as patch:
        _install_storage_failure_spies(patch, storage_work)
        with pytest.raises(LeaseError) as caught:
            _manager(lease_root)
        _assert_lease_error(caught, LeaseErrorCode.LOCK_UNAVAILABLE)
    assert checked == [lease_module.os]
    assert touched == []
    assert list(lease_root.iterdir()) == []


@pytest.mark.parametrize(
    "missing",
    [
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_CLOEXEC",
        "O_NONBLOCK",
        "O_RDONLY",
        "O_RDWR",
        "O_CREAT",
        "open-dir-fd",
        "stat-dir-fd",
        "stat-no-follow",
        "fstat",
        "fsync",
        "geteuid",
        "getpid",
        "register_at_fork",
        "supports_dir_fd",
        "supports_follow_symlinks",
        "noncallable-open",
        "noncallable-stat",
        "noncallable-fstat",
        "noncallable-fsync",
        "noncallable-geteuid",
        "noncallable-getpid",
        "noncallable-register_at_fork",
        "nonset-supports_dir_fd",
        "nonset-supports_follow_symlinks",
        "nonint-O_RDONLY",
        "nonint-O_DIRECTORY",
        "nonint-O_NOFOLLOW",
        "nonint-O_CLOEXEC",
        "nonint-O_NONBLOCK",
        "nonint-O_RDWR",
        "nonint-O_CREAT",
    ],
)
def test_each_missing_posix_capability_fails_closed(missing: str):
    fake_os = _capable_fake_posix_os()
    if missing == "open-dir-fd":
        fake_os.supports_dir_fd.remove(fake_os.open)
    elif missing == "stat-dir-fd":
        fake_os.supports_dir_fd.remove(fake_os.stat)
    elif missing == "stat-no-follow":
        fake_os.supports_follow_symlinks.remove(fake_os.stat)
    elif missing.startswith("noncallable-"):
        attribute = missing.removeprefix("noncallable-")
        setattr(fake_os, attribute, None)
        if attribute == "open":
            fake_os.supports_dir_fd = {None, fake_os.stat}
        elif attribute == "stat":
            fake_os.supports_dir_fd = {fake_os.open, None}
            fake_os.supports_follow_symlinks = {None}
    elif missing.startswith("nonset-"):
        attribute = missing.removeprefix("nonset-")
        setattr(fake_os, attribute, tuple(getattr(fake_os, attribute)))
    elif missing.startswith("nonint-"):
        setattr(fake_os, missing.removeprefix("nonint-"), object())
    else:
        delattr(fake_os, missing)
    with pytest.raises(LeaseError) as caught:
        lease_module._require_posix_capabilities(fake_os)
    _assert_lease_error(caught, LeaseErrorCode.LOCK_UNAVAILABLE)


def test_posix_capability_flag_and_support_set_types_are_exact():
    class FlagInteger(int):
        pass

    class CapabilitySet(set):
        pass

    flag_names = (
        "O_RDONLY",
        "O_DIRECTORY",
        "O_NOFOLLOW",
        "O_CLOEXEC",
        "O_NONBLOCK",
        "O_RDWR",
        "O_CREAT",
    )
    for flag_name in flag_names:
        for invalid in (True, FlagInteger(1)):
            fake_os = _capable_fake_posix_os()
            setattr(fake_os, flag_name, invalid)
            with pytest.raises(LeaseError) as caught:
                lease_module._require_posix_capabilities(fake_os)
            _assert_lease_error(caught, LeaseErrorCode.LOCK_UNAVAILABLE)

    for support_name in ("supports_dir_fd", "supports_follow_symlinks"):
        fake_os = _capable_fake_posix_os()
        original = getattr(fake_os, support_name)
        setattr(fake_os, support_name, CapabilitySet(original))
        with pytest.raises(LeaseError) as caught:
            lease_module._require_posix_capabilities(fake_os)
        _assert_lease_error(caught, LeaseErrorCode.LOCK_UNAVAILABLE)


def test_windows_open_flags_are_binary_noninheritable_and_non_destructive():
    fake_os = SimpleNamespace(
        O_RDWR=1,
        O_BINARY=2,
        O_NOINHERIT=4,
        O_TRUNC=8,
        O_APPEND=16,
    )
    flags = lease_module._windows_open_flags(fake_os)
    required = fake_os.O_RDWR | fake_os.O_BINARY | fake_os.O_NOINHERIT
    assert flags & required == required
    assert flags & (fake_os.O_TRUNC | fake_os.O_APPEND) == 0


def test_posix_adapter_uses_nonblocking_exclusive_lock_and_exact_unlock():
    fake = _FakeFcntl()
    adapter = lease_module._PosixFileLock(fake)
    adapter.acquire(17)
    adapter.release(17)
    assert fake.calls == [
        (17, fake.LOCK_EX | fake.LOCK_NB),
        (17, fake.LOCK_UN),
    ]


@pytest.mark.parametrize(
    ("native_errno", "expected"),
    [
        (errno.EACCES, LeaseErrorCode.CONTENDED),
        (errno.EAGAIN, LeaseErrorCode.CONTENDED),
        (errno.ENOSYS, LeaseErrorCode.LOCK_UNAVAILABLE),
        (getattr(errno, "ENOTSUP", errno.ENOSYS), LeaseErrorCode.LOCK_UNAVAILABLE),
        (errno.EIO, LeaseErrorCode.IO_ERROR),
    ],
)
def test_posix_adapter_maps_only_declared_native_errors(native_errno, expected):
    fake = _FakeFcntl(OSError(native_errno, "native details"))
    with pytest.raises(LeaseError) as caught:
        lease_module._PosixFileLock(fake).acquire(17)
    error = _assert_lease_error(caught, expected)
    assert "native details" not in str(error)


@pytest.mark.parametrize(
    ("native_errno", "expected"),
    [
        (errno.EACCES, LeaseErrorCode.IO_ERROR),
        (errno.EAGAIN, LeaseErrorCode.IO_ERROR),
        (errno.ENOSYS, LeaseErrorCode.LOCK_UNAVAILABLE),
        (getattr(errno, "ENOTSUP", errno.ENOSYS), LeaseErrorCode.LOCK_UNAVAILABLE),
        (errno.EIO, LeaseErrorCode.IO_ERROR),
    ],
)
def test_posix_adapter_release_errors_are_operation_aware(native_errno, expected):
    fake = _FakeFcntl(OSError(native_errno, "native unlock details"))
    with pytest.raises(LeaseError) as caught:
        lease_module._PosixFileLock(fake).release(17)
    error = _assert_lease_error(caught, expected)
    assert fake.calls == [(17, fake.LOCK_UN)]
    assert "native unlock details" not in str(error)


def test_windows_adapter_locks_and_unlocks_exact_one_byte_at_zero(tmp_path: Path):
    path = tmp_path / "byte.lock"
    path.write_bytes(b"0")
    fd = os.open(path, os.O_RDWR)
    fake = _FakeMsvcrt()
    try:
        adapter = lease_module._WindowsFileLock(fake)
        os.lseek(fd, 0, os.SEEK_END)
        adapter.acquire(fd)
        os.lseek(fd, 0, os.SEEK_END)
        adapter.release(fd)
    finally:
        os.close(fd)
    assert fake.calls == [
        (fd, fake.LK_NBLCK, 1, 0),
        (fd, fake.LK_UNLCK, 1, 0),
    ]


@pytest.mark.parametrize(
    ("native_errno", "winerror", "expected"),
    [
        (errno.EACCES, None, LeaseErrorCode.CONTENDED),
        (errno.EIO, 33, LeaseErrorCode.CONTENDED),
        (errno.EIO, None, LeaseErrorCode.IO_ERROR),
    ],
)
def test_windows_adapter_maps_only_declared_native_errors(
    tmp_path: Path, native_errno, winerror, expected
):
    path = tmp_path / "byte.lock"
    path.write_bytes(b"0")
    fd = os.open(path, os.O_RDWR)
    native = OSError(native_errno, "native details")
    if winerror is not None:
        native.winerror = winerror
    fake = _FakeMsvcrt(native)
    try:
        with pytest.raises(LeaseError) as caught:
            lease_module._WindowsFileLock(fake).acquire(fd)
    finally:
        os.close(fd)
    error = _assert_lease_error(caught, expected)
    assert "native details" not in str(error)


@pytest.mark.parametrize(
    ("native_errno", "winerror"),
    [(errno.EACCES, None), (errno.EIO, 33), (errno.EIO, None)],
)
def test_windows_adapter_release_error_is_not_reported_as_contention(
    tmp_path: Path, native_errno, winerror
):
    path = tmp_path / "byte.lock"
    path.write_bytes(b"0")
    fd = os.open(path, os.O_RDWR)
    native = OSError(native_errno, "native unlock details")
    if winerror is not None:
        native.winerror = winerror
    fake = _FakeMsvcrt(native)
    try:
        with pytest.raises(LeaseError) as caught:
            lease_module._WindowsFileLock(fake).release(fd)
    finally:
        os.close(fd)
    error = _assert_lease_error(caught, LeaseErrorCode.IO_ERROR)
    assert fake.calls == [(fd, fake.LK_UNLCK, 1, 0)]
    assert "native unlock details" not in str(error)


@pytest.mark.parametrize("platform_name", ["win32", "unknown-host"])
def test_unverified_or_unknown_platform_selector_fails_closed(platform_name: str):
    with pytest.raises(LeaseError) as caught:
        lease_module._select_platform_adapter(platform_name)
    _assert_lease_error(caught, LeaseErrorCode.UNSUPPORTED_PLATFORM)


@POSIX_ONLY
def test_production_platform_factory_returns_exact_posix_adapter():
    assert type(lease_module._new_platform_adapter()) is lease_module._PosixFileLock


@pytest.mark.parametrize("platform_name", ["win32", "unknown-host"])
def test_production_platform_factory_has_no_unverified_fallback(monkeypatch, platform_name: str):
    monkeypatch.setattr(lease_module.sys, "platform", platform_name)
    with pytest.raises(LeaseError) as caught:
        lease_module._new_platform_adapter()
    _assert_lease_error(caught, LeaseErrorCode.UNSUPPORTED_PLATFORM)


@POSIX_ONLY
def test_live_posix_subprocess_contention_and_release(lease_root: Path):
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    deadline = time.monotonic() + 5
    contended = _run_child_try(lease_root, RESOURCE_ID, deadline)
    assert contended.returncode == 3, contended.stderr
    messages = _parse_child_lines(contended.stdout)
    assert messages[0]["phase"] == "error"
    assert messages[0]["code"] == LeaseErrorCode.CONTENDED.value
    assert Path(str(messages[0]["module"])).resolve() == Path(lease_module.__file__).resolve()
    lease.release(owner_token=lease.owner_token)

    acquired = _run_child_try(lease_root, RESOURCE_ID, deadline)
    assert acquired.returncode == 0, acquired.stderr
    messages = _parse_child_lines(acquired.stdout)
    assert [item["phase"] for item in messages] == ["acquired", "released"]
    assert messages[0]["key"] == _expected_key(RESOURCE_ID)


@POSIX_ONLY
def test_process_exit_releases_os_lock_without_replacing_inode(lease_root: Path):
    deadline = time.monotonic() + 5
    process = subprocess.Popen(
        _child_command(lease_root, RESOURCE_ID, "hold"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=_child_environment(),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        acquired = _read_json_line(process.stdout, deadline)
        assert acquired["phase"] == "acquired"
        assert Path(str(acquired["module"])).resolve() == Path(lease_module.__file__).resolve()
        path = _lock_path(lease_root, RESOURCE_ID)
        inode = path.stat().st_ino
        with pytest.raises(LeaseError) as caught:
            _manager(lease_root).acquire(RESOURCE_ID)
        _assert_lease_error(caught, LeaseErrorCode.CONTENDED)
        process.stdin.write(json.dumps({"command": "crash"}) + "\n")
        process.stdin.flush()
        remaining = max(0, deadline - time.monotonic())
        cleanup_reserve = min(1.0, remaining / 2)
        process.wait(timeout=max(0, remaining - cleanup_reserve))
        assert process.returncode == 0
        lease = _manager(lease_root).acquire(RESOURCE_ID)
        assert path.stat().st_ino == inode
        lease.release(owner_token=lease.owner_token)
    finally:
        if process.poll() is None:
            process.kill()
            remaining = max(0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise AssertionError("subprocess reap exceeded its deadline") from exc
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@POSIX_ONLY
def test_fork_child_never_unlocks_the_parent_process_lock(lease_root: Path):
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    ready_read, ready_write = os.pipe()
    exit_read, exit_write = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(ready_read)
        os.close(exit_write)
        try:
            os.write(ready_write, b"child-ready\n")
            os.read(exit_read, 1)
        finally:
            os.close(ready_write)
            os.close(exit_read)
            os._exit(0)

    os.close(ready_write)
    os.close(exit_read)
    deadline = time.monotonic() + 5
    child_status: tuple[int, int] | None = None
    cleanup_error: LeaseError | None = None
    try:
        assert _read_fd_with_deadline(ready_read, 128, deadline).strip() == b"child-ready"
        contended = _run_child_try(lease_root, RESOURCE_ID, deadline)
        assert contended.returncode == 3, contended.stderr
        assert _parse_child_lines(contended.stdout)[0]["code"] == LeaseErrorCode.CONTENDED.value
    finally:
        if not lease.released:
            try:
                lease.release(owner_token=lease.owner_token)
            except LeaseError as exc:
                cleanup_error = exc
        try:
            os.write(exit_write, b"x")
        except OSError:
            pass
        try:
            os.close(exit_write)
        except OSError:
            pass
        try:
            child_status = _waitpid_with_deadline(child_pid, deadline)
        finally:
            try:
                os.close(ready_read)
            except OSError:
                pass
        if cleanup_error is not None:
            raise cleanup_error
    assert child_status is not None
    waited, status_value = child_status
    assert waited == child_pid
    assert os.waitstatus_to_exitcode(status_value) == 0


@POSIX_ONLY
def test_fork_child_cannot_use_inherited_lease_to_unlock_parent(monkeypatch, lease_root: Path):
    adapter = _CloseOnlyReleaseAdapter(lease_module._new_platform_adapter())
    registry_lock = _RegistryLockProbe()
    reservations = _GuardedReservations(registry_lock)
    monkeypatch.setattr(lease_module, "_new_platform_adapter", lambda: adapter)
    monkeypatch.setattr(lease_module, "_PROCESS_REGISTRY_LOCK", registry_lock)
    monkeypatch.setattr(lease_module, "_PROCESS_RESERVATIONS", reservations)
    manager = _manager(lease_root)
    lease = manager.acquire(RESOURCE_ID)
    root = lease_root.stat()
    registry_key = (
        adapter.platform_key,
        root.st_dev,
        root.st_ino,
        _expected_key(RESOURCE_ID),
    )
    assert set(reservations.snapshot()) == {registry_key}
    parent_registry = lease_module._PROCESS_RESERVATIONS
    parent_registry_lock = lease_module._PROCESS_REGISTRY_LOCK
    ready_read, ready_write = os.pipe()
    exit_read, exit_write = os.pipe()
    deadline = time.monotonic() + 5
    lock_calls_before_fork = registry_lock.acquire_calls
    reservations.allow_enumeration = True
    child_pid = os.fork()
    if child_pid == 0:
        os.close(ready_read)
        os.close(exit_write)
        try:
            os.write(
                ready_write,
                json.dumps(
                    {
                        "phase": "forked-no-vibecad-api",
                        "registry_count": len(lease_module._PROCESS_RESERVATIONS),
                        "registry_replaced": (
                            lease_module._PROCESS_RESERVATIONS is not parent_registry
                        ),
                        "registry_lock_replaced": (
                            lease_module._PROCESS_REGISTRY_LOCK is not parent_registry_lock
                        ),
                    },
                    sort_keys=True,
                ).encode("ascii")
                + b"\n",
            )
            os.read(exit_read, 1)
            results: dict[str, str] = {}
            try:
                lease.release(owner_token="invalid-owner")
            except LeaseError as exc:
                results["lease_invalid_owner"] = exc.code.value
            else:
                results["lease_invalid_owner"] = "unexpected-success"
            try:
                manager.release(lease, owner_token="invalid-owner")
            except LeaseError as exc:
                results["manager_release_invalid_owner"] = exc.code.value
            else:
                results["manager_release_invalid_owner"] = "unexpected-success"
            try:
                lease.release(owner_token=lease.owner_token)
            except LeaseError as exc:
                results["lease_release"] = exc.code.value
            else:
                results["lease_release"] = "unexpected-success"
            try:
                inherited = manager.acquire(OTHER_RESOURCE_ID)
            except LeaseError as exc:
                results["manager_acquire"] = exc.code.value
            else:
                results["manager_acquire"] = "unexpected-success"
                inherited.release(owner_token=inherited.owner_token)
            results["adapter_acquire_count"] = str(len(adapter.acquire_calls))
            results["adapter_release_count"] = str(len(adapter.release_calls))
            payload = json.dumps(results, sort_keys=True).encode("ascii")
            os.write(ready_write, payload + b"\n")
            os.read(exit_read, 1)
            try:
                fresh_other = _manager(lease_root).acquire(OTHER_RESOURCE_ID)
            except LeaseError as exc:
                other_result = {"fresh_other_manager": exc.code.value}
            else:
                fresh_other.release(owner_token=fresh_other.owner_token)
                other_result = {"fresh_other_manager": "acquired-and-released"}
            os.write(
                ready_write,
                json.dumps(other_result, sort_keys=True).encode("ascii") + b"\n",
            )
            os.read(exit_read, 1)
            try:
                fresh = _manager(lease_root).acquire(RESOURCE_ID)
            except LeaseError as exc:
                resource_result = {"fresh_resource_manager": exc.code.value}
            else:
                fresh.release(owner_token=fresh.owner_token)
                resource_result = {"fresh_resource_manager": "acquired-and-released"}
            os.write(
                ready_write,
                json.dumps(resource_result, sort_keys=True).encode("ascii") + b"\n",
            )
        finally:
            os.close(ready_write)
            os.close(exit_read)
            os._exit(0)

    os.close(ready_write)
    os.close(exit_read)
    assert registry_lock.acquire_calls > lock_calls_before_fork
    assert not registry_lock.held_by_current_thread
    parent_lock_available = threading.Event()
    lock_probe_failures: list[BaseException] = []

    def acquire_parent_registry_lock() -> None:
        try:
            with registry_lock:
                parent_lock_available.set()
        except BaseException as exc:
            lock_probe_failures.append(exc)

    lock_probe = threading.Thread(target=acquire_parent_registry_lock, daemon=True)
    lock_probe.start()
    remaining = max(0, deadline - time.monotonic())
    assert parent_lock_available.wait(timeout=remaining)
    lock_probe.join(timeout=max(0, deadline - time.monotonic()))
    assert not lock_probe.is_alive()
    assert lock_probe_failures == []
    child_status: tuple[int, int] | None = None
    cleanup_error: LeaseError | None = None
    try:
        initial = json.loads(_read_fd_with_deadline(ready_read, 512, deadline))
        assert initial == {
            "phase": "forked-no-vibecad-api",
            "registry_count": 0,
            "registry_replaced": True,
            "registry_lock_replaced": True,
        }
        contended = _run_child_try(lease_root, RESOURCE_ID, deadline)
        assert contended.returncode == 3, contended.stderr
        assert _parse_child_lines(contended.stdout)[0]["code"] == LeaseErrorCode.CONTENDED.value
        lease.release(owner_token=lease.owner_token)
        assert adapter.release_calls == adapter.acquire_calls[:1]
        assert reservations.snapshot() == {}
        assert lease_module._PROCESS_RESERVATIONS is parent_registry
        assert lease_module._PROCESS_REGISTRY_LOCK is parent_registry_lock
        available_before_child_api = _run_child_try(lease_root, RESOURCE_ID, deadline)
        assert available_before_child_api.returncode == 0, available_before_child_api.stderr
        assert [
            item["phase"] for item in _parse_child_lines(available_before_child_api.stdout)
        ] == ["acquired", "released"]
        os.write(exit_write, b"x")
        payload = json.loads(_read_fd_with_deadline(ready_read, 512, deadline))
        assert payload == {
            "adapter_acquire_count": "1",
            "adapter_release_count": "0",
            "lease_invalid_owner": LeaseErrorCode.WRONG_PROCESS.value,
            "lease_release": LeaseErrorCode.WRONG_PROCESS.value,
            "manager_acquire": LeaseErrorCode.WRONG_PROCESS.value,
            "manager_release_invalid_owner": LeaseErrorCode.WRONG_PROCESS.value,
        }
        assert not _lock_path(lease_root, OTHER_RESOURCE_ID).exists()
        os.write(exit_write, b"x")
        child_other = json.loads(_read_fd_with_deadline(ready_read, 512, deadline))
        assert child_other == {"fresh_other_manager": "acquired-and-released"}
        os.write(exit_write, b"x")
        child_reacquired = json.loads(_read_fd_with_deadline(ready_read, 512, deadline))
        assert child_reacquired == {"fresh_resource_manager": "acquired-and-released"}
    finally:
        if not lease.released:
            try:
                lease.release(owner_token=lease.owner_token)
            except LeaseError as exc:
                cleanup_error = exc
        try:
            os.write(exit_write, b"xx")
        except OSError:
            pass
        try:
            os.close(exit_write)
        except OSError:
            pass
        try:
            child_status = _waitpid_with_deadline(child_pid, deadline)
        finally:
            try:
                os.close(ready_read)
            except OSError:
                pass
        if cleanup_error is not None:
            raise cleanup_error
    assert child_status is not None
    waited, status_value = child_status
    assert waited == child_pid
    assert os.waitstatus_to_exitcode(status_value) == 0


def test_source_contains_no_ttl_stale_reclaim_or_lock_entry_deletion():
    source = Path(lease_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_calls = {
        "__import__",
        "__dict__",
        "__getattribute__",
        "__globals__",
        "__base__",
        "__bases__",
        "__class__",
        "__mro__",
        "__reduce__",
        "__reduce_ex__",
        "__subclasses__",
        "_exit",
        "_flavour",
        "_getframe",
        "abort",
        "add",
        "access",
        "attrgetter",
        "chdir",
        "chmod",
        "chown",
        "clear",
        "copy",
        "copy2",
        "copyfile",
        "copyfileobj",
        "copymode",
        "copystat",
        "creat",
        "currentframe",
        "cwd",
        "defaultdict",
        "dict",
        "discard",
        "dup",
        "dup2",
        "eval",
        "environ",
        "exec",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "exists",
        "ftruncate",
        "f_builtins",
        "f_globals",
        "frame",
        "cr_frame",
        "fork",
        "forkpty",
        "gi_frame",
        "getattr_static",
        "getframeinfo",
        "getmembers",
        "getmembers_static",
        "getouterframes",
        "getxattr",
        "getmtime",
        "globals",
        "home",
        "import_module",
        "iterdir",
        "is_dir",
        "is_file",
        "is_symlink",
        "kill",
        "killpg",
        "link",
        "list",
        "load",
        "listdir",
        "listxattr",
        "loads",
        "lchown",
        "locals",
        "makedirs",
        "methodcaller",
        "mkdir",
        "mkfifo",
        "mkdtemp",
        "mkstemp",
        "mknod",
        "modules",
        "mro",
        "move",
        "partial",
        "parser",
        "pathconf",
        "popen",
        "pop",
        "posix_spawn",
        "posix_spawnp",
        "putenv",
        "pread",
        "read",
        "read_bytes",
        "readlink",
        "read_text",
        "readline",
        "readlines",
        "readv",
        "remove",
        "removedirs",
        "removexattr",
        "rename",
        "renames",
        "replace",
        "resolve",
        "rmdir",
        "rmtree",
        "scandir",
        "set",
        "setdefault",
        "set_inheritable",
        "setxattr",
        "stack",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "startfile",
        "statvfs",
        "suppress",
        "symlink",
        "system",
        "truncate",
        "tb_frame",
        "touch",
        "unlink",
        "unsetenv",
        "update",
        "umask",
        "utime",
        "WeakKeyDictionary",
        "WeakValueDictionary",
        "write",
        "write_bytes",
        "write_text",
        "writelines",
        "writev",
        "vars",
        "wait",
        "wait3",
        "wait4",
        "waitid",
        "waitpid",
    }
    forbidden_time_calls = {
        "clock_gettime",
        "fromtimestamp",
        "get_clock_info",
        "monotonic",
        "monotonic_ns",
        "now",
        "perf_counter",
        "process_time",
        "sleep",
        "thread_time",
        "time",
        "times",
        "today",
        "utcnow",
    }
    forbidden_stat_fields = {
        "st_atime",
        "st_atime_ns",
        "st_birthtime",
        "st_ctime",
        "st_ctime_ns",
        "st_mtime",
        "st_mtime_ns",
    }
    imported_modules: set[str] = set()
    imported_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
                imported_names[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_modules.add(module)
            for alias in node.names:
                full_name = f"{module}.{alias.name}" if module else alias.name
                imported_modules.add(full_name)
                imported_names[alias.asname or alias.name] = full_name

    unsafe_import_alias_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(alias.asname is not None for alias in node.names)
    ]

    direct_path_calls = {"lstat", "open", "stat"}
    capability_support_names = {
        "supports_dir_fd",
        "supports_follow_symlinks",
    }
    storage_primitive_names = set(_STORAGE_OS_PROBES) | {
        "fstatvfs",
        "getcwdb",
        "get_inheritable",
        "set_inheritable",
    }
    sensitive_call_names = (
        forbidden_calls
        | forbidden_time_calls
        | direct_path_calls
        | storage_primitive_names
        | capability_support_names
    )
    assignment_records: list[tuple[list[ast.expr], ast.expr]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignment_records.append((node.targets, node.value))
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                assignment_records.append(([node.target], node.value))

    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    assigned_qualified_names: dict[str, str] = {}
    assigned_strings: dict[str, str] = {}

    def qualified_name(value: ast.expr) -> str | None:
        if isinstance(value, ast.Name):
            if value.id in assigned_qualified_names:
                return assigned_qualified_names[value.id]
            if value.id in imported_names:
                return imported_names[value.id]
            if value.id in {"getattr", "open"}:
                return f"builtins.{value.id}"
            return None
        if isinstance(value, ast.Attribute):
            owner = qualified_name(value.value)
            if owner is not None:
                return f"{owner}.{value.attr}"
        return None

    def constant_string(value: ast.expr) -> str | None:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        if isinstance(value, ast.Name):
            return assigned_strings.get(value.id)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add):
            left = constant_string(value.left)
            right = constant_string(value.right)
            if left is not None and right is not None:
                return left + right
        if isinstance(value, ast.JoinedStr):
            parts: list[str] = []
            for part in value.values:
                if not isinstance(part, ast.Constant) or not isinstance(part.value, str):
                    return None
                parts.append(part.value)
            return "".join(parts)
        return None

    for _ in range(len(assignment_records) + 1):
        changed = False
        for targets, value in assignment_records:
            if not all(isinstance(target, ast.Name) for target in targets):
                continue
            resolved_name = qualified_name(value)
            resolved_string = constant_string(value)
            for target in targets:
                if (
                    resolved_name is not None
                    and assigned_qualified_names.get(target.id) != resolved_name
                ):
                    assigned_qualified_names[target.id] = resolved_name
                    changed = True
                if (
                    resolved_string is not None
                    and assigned_strings.get(target.id) != resolved_string
                ):
                    assigned_strings[target.id] = resolved_string
                    changed = True
        if not changed:
            break

    assigned_sensitive_aliases: dict[str, str] = {}

    def sensitive_name(value: ast.expr) -> str | None:
        if isinstance(value, ast.Attribute) and value.attr in sensitive_call_names:
            return value.attr
        if isinstance(value, ast.Name):
            if value.id in assigned_sensitive_aliases:
                return assigned_sensitive_aliases[value.id]
            resolved = imported_names.get(value.id, value.id).rsplit(".", 1)[-1]
            if resolved in sensitive_call_names:
                return resolved
        return None

    for _ in range(len(assignment_records) + 1):
        changed = False
        for targets, value in assignment_records:
            resolved = sensitive_name(value)
            if resolved is None or not all(isinstance(target, ast.Name) for target in targets):
                continue
            for target in targets:
                if assigned_sensitive_aliases.get(target.id) != resolved:
                    assigned_sensitive_aliases[target.id] = resolved
                    changed = True
        if not changed:
            break

    unsafe_sensitive_default_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        defaults = [*node.args.defaults, *(item for item in node.args.kw_defaults if item)]
        unsafe_sensitive_default_lines.extend(
            default.lineno for default in defaults if sensitive_name(default) is not None
        )

    calls: set[str] = set()
    bare_calls: set[str] = set()
    unsafe_non_os_path_call_lines: list[int] = []
    unsafe_captured_call_lines: list[int] = []
    unsafe_dynamic_lookup_lines: list[int] = []
    unsafe_indirect_call_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
            if node.func.attr in direct_path_calls:
                receiver = node.func.value
                if qualified_name(receiver) != "os":
                    unsafe_non_os_path_call_lines.append(node.lineno)
        elif isinstance(node.func, ast.Name):
            resolved = imported_names.get(node.func.id, node.func.id)
            call_name = assigned_sensitive_aliases.get(
                node.func.id,
                resolved.rsplit(".", 1)[-1],
            )
            calls.add(call_name)
            bare_calls.add(call_name)
            if node.func.id in assigned_sensitive_aliases:
                unsafe_captured_call_lines.append(node.lineno)
        else:
            unsafe_indirect_call_lines.append(node.lineno)
        if qualified_name(node.func) == "builtins.getattr":
            attribute_name = constant_string(node.args[1]) if len(node.args) >= 2 else None
            if attribute_name is None or attribute_name in (
                sensitive_call_names | {"BaseException", "Exception"}
            ):
                unsafe_dynamic_lookup_lines.append(node.lineno)

    unsafe_fcntl_flag_call_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fcntl"
        and qualified_name(node.func.value) == "fcntl"
    ]
    allowed_os_attributes = {
        "O_APPEND",
        "O_BINARY",
        "O_CLOEXEC",
        "O_CREAT",
        "O_DIRECTORY",
        "O_NOINHERIT",
        "O_NOFOLLOW",
        "O_NONBLOCK",
        "O_RDONLY",
        "O_RDWR",
        "O_TRUNC",
        "SEEK_SET",
        "close",
        "fstat",
        "fsync",
        "geteuid",
        "getpid",
        "lseek",
        "name",
        "open",
        "register_at_fork",
        "stat",
        "supports_dir_fd",
        "supports_follow_symlinks",
    }
    allowed_module_attributes = {
        "collections.abc.MutableMapping": set(),
        "collections.abc.Set": set(),
        "enum.StrEnum": set(),
        "errno": {"EACCES", "EAGAIN", "EIO", "ENOSYS", "ENOTSUP"},
        "fcntl": set(),
        "fcntl_module": {"LOCK_EX", "LOCK_NB", "LOCK_UN", "flock"},
        "hashlib": {"sha256"},
        "msvcrt_module": {"LK_NBLCK", "LK_UNLCK", "locking"},
        "os": allowed_os_attributes,
        "os_module": allowed_os_attributes,
        "pathlib.Path": set(),
        "re": {"compile"},
        "secrets": {"compare_digest", "token_hex"},
        "stat": {"S_IFMT", "S_IMODE", "S_ISDIR", "S_ISREG"},
        "sys": {"platform"},
        "threading": {"RLock"},
    }

    def module_receiver_name(value: ast.expr) -> str | None:
        if isinstance(value, ast.Name) and value.id in {
            "fcntl_module",
            "msvcrt_module",
            "os_module",
        }:
            return value.id
        return qualified_name(value)

    unsafe_module_attribute_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and module_receiver_name(node.value) in allowed_module_attributes
        and node.attr not in allowed_module_attributes[module_receiver_name(node.value)]
    ]

    module_object_names = {
        "MutableMapping",
        "Path",
        "Set",
        "StrEnum",
        "errno",
        "fcntl",
        "fcntl_module",
        "hashlib",
        "msvcrt_module",
        "os",
        "os_module",
        "re",
        "secrets",
        "stat",
        "sys",
        "threading",
    }

    def is_annotation_reference(value: ast.AST) -> bool:
        child = value
        parent = parents.get(value)
        while parent is not None:
            if isinstance(parent, ast.arg) and parent.annotation is child:
                return True
            if isinstance(parent, ast.AnnAssign) and parent.annotation is child:
                return True
            if (
                isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                and parent.returns is child
            ):
                return True
            if isinstance(parent, (ast.stmt, ast.Lambda)):
                return False
            child = parent
            parent = parents.get(parent)
        return False

    unsafe_module_object_load_lines: list[int] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Name)
            or not isinstance(node.ctx, ast.Load)
            or node.id not in module_object_names
        ):
            continue
        parent = parents.get(node)
        allowed = isinstance(parent, ast.Attribute) and parent.value is node
        if (
            node.id == "os"
            and isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "_require_posix_capabilities"
            and parent.args == [node]
        ):
            allowed = True
        if (
            node.id == "fcntl"
            and isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "_PosixFileLock"
            and parent.args == [node]
        ):
            allowed = True
        if node.id == "Path" and isinstance(parent, ast.Call) and parent.func is node:
            allowed = True
        if node.id == "StrEnum" and isinstance(parent, ast.ClassDef) and node in parent.bases:
            allowed = True
        if node.id in {"MutableMapping", "Path", "Set", "StrEnum"}:
            if is_annotation_reference(node):
                allowed = True
        if not allowed:
            unsafe_module_object_load_lines.append(node.lineno)

    def is_direct_os_primitive_call(value: ast.expr) -> bool:
        parent = parents.get(value)
        return (
            isinstance(value, ast.Attribute)
            and value.attr in (direct_path_calls | storage_primitive_names)
            and qualified_name(value.value) == "os"
            and isinstance(parent, ast.Call)
            and parent.func is value
        )

    def enclosing_function_name(value: ast.AST) -> str | None:
        parent = parents.get(value)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return parent.name
            parent = parents.get(parent)
        return None

    def is_capability_membership_reference(value: ast.expr) -> bool:
        if not isinstance(value, ast.Attribute) or value.attr not in {"open", "stat"}:
            return False
        if enclosing_function_name(value) != "_require_posix_capabilities":
            return False
        parent = parents.get(value)
        if (
            not isinstance(parent, ast.Compare)
            or parent.left is not value
            or len(parent.ops) != 1
            or not isinstance(parent.ops[0], (ast.In, ast.NotIn))
            or len(parent.comparators) != 1
        ):
            return False
        support_set = parent.comparators[0]
        if not isinstance(support_set, ast.Attribute):
            return False
        allowed_sets = (
            {"supports_dir_fd"}
            if value.attr == "open"
            else {"supports_dir_fd", "supports_follow_symlinks"}
        )
        return support_set.attr in allowed_sets and ast.dump(
            value.value, include_attributes=False
        ) == ast.dump(support_set.value, include_attributes=False)

    def is_capability_support_reference(value: ast.expr) -> bool:
        if (
            not isinstance(value, ast.Attribute)
            or value.attr not in capability_support_names
            or enclosing_function_name(value) != "_require_posix_capabilities"
        ):
            return False
        parent = parents.get(value)
        if (
            not isinstance(parent, ast.Compare)
            or len(parent.ops) != 1
            or not isinstance(parent.ops[0], (ast.In, ast.NotIn))
            or parent.comparators != [value]
            or not isinstance(parent.left, ast.Attribute)
            or parent.left.attr not in {"open", "stat"}
        ):
            return False
        allowed_left = {"open", "stat"} if value.attr == "supports_dir_fd" else {"stat"}
        return parent.left.attr in allowed_left and ast.dump(
            parent.left.value, include_attributes=False
        ) == ast.dump(value.value, include_attributes=False)

    def is_capability_support_type_reference(value: ast.expr) -> bool:
        if (
            not isinstance(value, ast.Attribute)
            or value.attr not in capability_support_names
            or enclosing_function_name(value) != "_require_posix_capabilities"
        ):
            return False
        type_call = parents.get(value)
        comparison = parents.get(type_call) if type_call is not None else None
        if (
            not isinstance(type_call, ast.Call)
            or not isinstance(type_call.func, ast.Name)
            or type_call.func.id != "type"
            or type_call.args != [value]
            or type_call.keywords != []
            or not isinstance(comparison, ast.Compare)
            or len(comparison.ops) != 1
            or not isinstance(comparison.ops[0], (ast.Is, ast.IsNot))
            or len(comparison.comparators) != 1
        ):
            return False
        other = comparison.comparators[0] if comparison.left is type_call else comparison.left
        return (
            isinstance(other, ast.Call)
            and isinstance(other.func, ast.Name)
            and other.func.id == "type"
            and len(other.args) == 1
            and isinstance(other.args[0], ast.Set)
            and other.keywords == []
        )

    def is_capability_callable_reference(value: ast.expr) -> bool:
        required_callables = {
            "fstat",
            "fsync",
            "geteuid",
            "getpid",
            "open",
            "register_at_fork",
            "stat",
        }
        if (
            not isinstance(value, ast.Attribute)
            or value.attr not in required_callables
            or enclosing_function_name(value) != "_require_posix_capabilities"
        ):
            return False
        parent = parents.get(value)
        return (
            isinstance(parent, ast.Call)
            and isinstance(parent.func, ast.Name)
            and parent.func.id == "callable"
            and parent.args == [value]
            and parent.keywords == []
        )

    unsafe_sensitive_reference_lines: list[int] = []
    unsafe_getattr_capture_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        resolved = qualified_name(node)
        leaf = resolved.rsplit(".", 1)[-1] if resolved is not None else None
        sensitive = (isinstance(node, ast.Attribute) and node.attr in sensitive_call_names) or (
            leaf in sensitive_call_names and resolved != "stat"
        )
        if sensitive and not (
            is_direct_os_primitive_call(node)
            or is_capability_membership_reference(node)
            or is_capability_support_reference(node)
            or is_capability_support_type_reference(node)
            or is_capability_callable_reference(node)
        ):
            unsafe_sensitive_reference_lines.append(node.lineno)
        if resolved == "builtins.getattr":
            parent = parents.get(node)
            if not (isinstance(parent, ast.Call) and parent.func is node):
                unsafe_getattr_capture_lines.append(node.lineno)

    broad_exception_aliases: dict[str, str] = {}

    def broad_exception_name(value: ast.expr) -> str | None:
        if isinstance(value, ast.Attribute) and value.attr in {"Exception", "BaseException"}:
            return value.attr
        if isinstance(value, ast.Name):
            if value.id in broad_exception_aliases:
                return broad_exception_aliases[value.id]
            resolved = imported_names.get(value.id, value.id).rsplit(".", 1)[-1]
            if resolved in {"Exception", "BaseException"}:
                return resolved
        if isinstance(value, ast.Tuple):
            resolved = {broad_exception_name(item) for item in value.elts}
            if "BaseException" in resolved:
                return "BaseException"
            if "Exception" in resolved:
                return "Exception"
        return None

    for _ in range(len(assignment_records) + 1):
        changed = False
        for targets, value in assignment_records:
            resolved = broad_exception_name(value)
            if resolved is None or not all(isinstance(target, ast.Name) for target in targets):
                continue
            for target in targets:
                if broad_exception_aliases.get(target.id) != resolved:
                    broad_exception_aliases[target.id] = resolved
                    changed = True
        if not changed:
            break

    unsafe_broad_exception_default_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        defaults = [*node.args.defaults, *(item for item in node.args.kw_defaults if item)]
        for default in defaults:
            if any(
                broad_exception_name(item) is not None
                for item in ast.walk(default)
                if isinstance(item, ast.expr)
            ):
                unsafe_broad_exception_default_lines.append(default.lineno)

    unsafe_broad_exception_reference_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute)) and broad_exception_name(node) is not None
    ]

    allowed_exception_names = {
        "AttributeError",
        "FileNotFoundError",
        "KeyError",
        "LeaseError",
        "OSError",
        "RuntimeError",
        "TypeError",
        "UnicodeEncodeError",
        "ValueError",
    }

    unsafe_allowed_exception_rebinding_lines = [
        node.lineno
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id in allowed_exception_names
        )
        or (isinstance(node, ast.arg) and node.arg in allowed_exception_names)
        or (isinstance(node, ast.ExceptHandler) and node.name in allowed_exception_names)
        or (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in allowed_exception_names
        )
        or (
            isinstance(node, ast.ClassDef)
            and node.name in allowed_exception_names
            and node.name != "LeaseError"
        )
        or (
            isinstance(node, (ast.Import, ast.ImportFrom))
            and any(
                (alias.asname or alias.name.rsplit(".", 1)[-1]) in allowed_exception_names
                for alias in node.names
            )
        )
    ]

    def exception_names(node: ast.expr | None) -> set[str]:
        if node is None:
            return {"bare-except"}
        if isinstance(node, ast.Name):
            if node.id in broad_exception_aliases:
                return {broad_exception_aliases[node.id]}
            resolved = imported_names.get(node.id, node.id)
            name = resolved.rsplit(".", 1)[-1]
            if name in allowed_exception_names | {"Exception", "BaseException"}:
                return {name}
            return {"unresolved-exception"}
        if isinstance(node, ast.Attribute):
            return {"unresolved-exception"}
        if isinstance(node, ast.Tuple):
            return set().union(*(exception_names(item) for item in node.elts))
        return {"unresolved-exception"}

    broad_handlers = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        for name in exception_names(node.type)
        if name
        in {
            "Exception",
            "BaseException",
            "bare-except",
            "unresolved-exception",
        }
    }
    broad_suppressed: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            call_name = node.func.attr
        elif isinstance(node.func, ast.Name):
            resolved = imported_names.get(node.func.id, node.func.id)
            call_name = resolved.rsplit(".", 1)[-1]
        else:
            continue
        if call_name == "suppress":
            for argument in node.args:
                broad_suppressed.update(exception_names(argument) & {"Exception", "BaseException"})

    forbidden_name_reference_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in {"__builtins__", "__import__"}
    ]

    def executes_during_import(value: ast.AST) -> bool:
        child = value
        parent = parents.get(value)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child in parent.body:
                    return False
            elif isinstance(parent, ast.Lambda) and child is parent.body:
                return False
            child = parent
            parent = parents.get(parent)
        return True

    def assigned_target_name(value: ast.expr) -> str | None:
        parent = parents.get(value)
        if isinstance(parent, ast.Assign) and parent.value is value:
            if len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
                return parent.targets[0].id
        if isinstance(parent, ast.AnnAssign) and parent.value is value:
            if isinstance(parent.target, ast.Name):
                return parent.target.id
        return None

    def is_allowed_at_fork_registration(node: ast.Call) -> bool:
        if qualified_name(node.func) != "os.register_at_fork":
            return False
        expected = {
            "before": "_prepare_for_fork",
            "after_in_parent": "_after_fork_parent",
            "after_in_child": "_after_fork_child",
        }
        observed = {
            keyword.arg: keyword.value.id
            for keyword in node.keywords
            if keyword.arg is not None and isinstance(keyword.value, ast.Name)
        }
        return observed == expected and isinstance(parents.get(node), ast.Expr)

    def is_at_fork_guard(node: ast.If) -> bool:
        test = node.test
        return (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Attribute)
            and qualified_name(test.left.value) == "os"
            and test.left.attr == "name"
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "posix"
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Call)
            and is_allowed_at_fork_registration(node.body[0].value)
            and node.orelse == []
        )

    unsafe_top_level_statement_lines: list[int] = []
    for index, node in enumerate(tree.body):
        allowed = isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Assign,
                ast.AnnAssign,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
            ),
        )
        if (
            index == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            allowed = True
        if isinstance(node, ast.If):
            allowed = is_at_fork_guard(node)
        if not allowed:
            unsafe_top_level_statement_lines.append(node.lineno)

    unsafe_decorator_lines = [
        decorator.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for decorator in node.decorator_list
    ]
    allowed_class_bases = {"ResourceLease", "StrEnum", "ValueError"}
    unsafe_class_definition_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and (
            node.keywords
            or any(
                not isinstance(base, ast.Name) or base.id not in allowed_class_bases
                for base in node.bases
            )
        )
    ]
    unsafe_nested_function_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and isinstance(
            parents.get(node),
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda),
        )
    ]
    unsafe_lambda_lines = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Lambda)]

    def is_constant_class_value(value: ast.expr) -> bool:
        if isinstance(value, ast.Constant):
            return True
        if isinstance(value, ast.Tuple):
            return all(is_constant_class_value(item) for item in value.elts)
        return False

    unsafe_class_body_lines: list[int] = []
    for class_node in (item for item in ast.walk(tree) if isinstance(item, ast.ClassDef)):
        for index, item in enumerate(class_node.body):
            allowed = isinstance(
                item,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Pass),
            )
            if (
                index == 0
                and isinstance(item, ast.Expr)
                and isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)
            ):
                allowed = True
            if isinstance(item, ast.Assign):
                allowed = is_constant_class_value(item.value)
            if isinstance(item, ast.AnnAssign) and item.value is not None:
                allowed = is_constant_class_value(item.value)
            if not allowed:
                unsafe_class_body_lines.append(item.lineno)

    unsafe_type_call_lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "type"
        and not isinstance(parents.get(node), ast.Compare)
    ]

    adapter_constructor_errors: list[str] = []
    for class_name, expected_argument in {
        "_PosixFileLock": "fcntl_module",
        "_WindowsFileLock": "msvcrt_module",
    }.items():
        class_node = next(
            (
                item
                for item in tree.body
                if isinstance(item, ast.ClassDef) and item.name == class_name
            ),
            None,
        )
        constructor = (
            next(
                (
                    item
                    for item in class_node.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "__init__"
                ),
                None,
            )
            if class_node is not None
            else None
        )
        if constructor is None:
            adapter_constructor_errors.append(class_name)
            continue
        arguments = constructor.args
        exact = (
            arguments.posonlyargs == []
            and [item.arg for item in arguments.args] == ["self", expected_argument]
            and arguments.vararg is None
            and arguments.kwonlyargs == []
            and arguments.kw_defaults == []
            and arguments.kwarg is None
            and arguments.defaults == []
        )
        if not exact:
            adapter_constructor_errors.append(class_name)

    unsafe_import_call_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not executes_during_import(node):
            continue
        resolved = qualified_name(node.func)
        target = assigned_target_name(node)
        allowed = (
            (
                resolved == "re.compile"
                and target in {"_OWNER_TOKEN_RE", "_PROJECT_ID_RE", "_RESOURCE_KEY_RE"}
            )
            or (resolved == "threading.RLock" and target == "_PROCESS_REGISTRY_LOCK")
            or is_allowed_at_fork_registration(node)
        )
        if not allowed:
            unsafe_import_call_lines.append(node.lineno)

    unsafe_compile_reference_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "compile":
            unsafe_compile_reference_lines.append(node.lineno)
            continue
        if not isinstance(node, ast.Attribute) or node.attr != "compile":
            continue
        parent = parents.get(node)
        allowed = (
            isinstance(parent, ast.Call)
            and parent.func is node
            and qualified_name(node) == "re.compile"
            and executes_during_import(parent)
            and assigned_target_name(parent)
            in {"_OWNER_TOKEN_RE", "_PROJECT_ID_RE", "_RESOURCE_KEY_RE"}
        )
        if not allowed:
            unsafe_compile_reference_lines.append(node.lineno)

    mutable_nodes = (
        ast.Dict,
        ast.DictComp,
        ast.List,
        ast.ListComp,
        ast.Set,
        ast.SetComp,
    )

    def contains_mutable_container(value: ast.expr) -> bool:
        return any(isinstance(item, mutable_nodes) for item in ast.walk(value))

    unsafe_mutable_assignment_lines: list[int] = []
    for targets, value in assignment_records:
        if not contains_mutable_container(value):
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        allowed = (
            len(names) == len(targets)
            and names == ["_PROCESS_RESERVATIONS"]
            and enclosing_function_name(value) in {None, "_after_fork_child"}
        )
        if not allowed:
            unsafe_mutable_assignment_lines.append(value.lineno)

    unsafe_mutable_default_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        defaults = [*node.args.defaults, *(item for item in node.args.kw_defaults if item)]
        unsafe_mutable_default_lines.extend(
            default.lineno for default in defaults if contains_mutable_container(default)
        )

    unsafe_shared_attribute_assignment_lines: list[int] = []
    for targets, value in assignment_records:
        if not isinstance(value, (ast.Call, *mutable_nodes)):
            continue
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            if not (isinstance(target.value, ast.Name) and target.value.id == "self"):
                unsafe_shared_attribute_assignment_lines.append(target.lineno)

    unsafe_global_names = {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global)
        for name in node.names
        if name not in {"_PROCESS_REGISTRY_LOCK", "_PROCESS_RESERVATIONS"}
    }

    unsafe_registry_lock_constructor_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if qualified_name(node.func) not in {"threading.Lock", "threading.RLock"}:
            continue
        if assigned_target_name(node) != "_PROCESS_REGISTRY_LOCK":
            unsafe_registry_lock_constructor_lines.append(node.lineno)

    authority_names = {"_PROCESS_REGISTRY_LOCK", "_PROCESS_RESERVATIONS"}

    def contains_authority_capture(value: ast.expr) -> bool:
        if isinstance(value, ast.Name):
            return value.id in authority_names
        if isinstance(value, ast.Attribute):
            return isinstance(value.value, ast.Name) and value.value.id in authority_names
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return any(contains_authority_capture(item) for item in value.elts)
        if isinstance(value, ast.Dict):
            return any(
                contains_authority_capture(item)
                for item in (*value.keys, *value.values)
                if item is not None
            )
        if isinstance(value, ast.Lambda):
            return any(
                isinstance(item, ast.Name) and item.id in authority_names
                for item in ast.walk(value)
            )
        return False

    unsafe_authority_alias_lines = [
        value.lineno for _, value in assignment_records if contains_authority_capture(value)
    ]
    unsafe_authority_default_lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        defaults = [*node.args.defaults, *(item for item in node.args.kw_defaults if item)]
        unsafe_authority_default_lines.extend(
            default.lineno
            for default in defaults
            if any(
                isinstance(item, ast.Name) and item.id in authority_names
                for item in ast.walk(default)
            )
        )

    function_nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required_authority_references = {
        "_reserve_process_reservation": {
            "_PROCESS_REGISTRY_LOCK",
            "_PROCESS_RESERVATIONS",
        },
        "_attach_process_reservation_fd": {
            "_PROCESS_REGISTRY_LOCK",
            "_PROCESS_RESERVATIONS",
        },
        "_drop_process_reservation": {
            "_PROCESS_REGISTRY_LOCK",
            "_PROCESS_RESERVATIONS",
        },
        "_require_current_lease": {
            "_PROCESS_REGISTRY_LOCK",
            "_PROCESS_RESERVATIONS",
        },
        "_prepare_for_fork": {"_PROCESS_REGISTRY_LOCK"},
        "_after_fork_parent": {"_PROCESS_REGISTRY_LOCK"},
        "_after_fork_child": {
            "_PROCESS_REGISTRY_LOCK",
            "_PROCESS_RESERVATIONS",
        },
    }
    missing_authority_references: dict[str, set[str]] = {}
    for name, required in required_authority_references.items():
        function = function_nodes.get(name)
        observed = (
            {item.id for item in ast.walk(function) if isinstance(item, ast.Name)}
            if function is not None
            else set()
        )
        missing = required - observed
        if missing:
            missing_authority_references[name] = missing

    allowed_lock_with_functions = {
        "_attach_process_reservation_fd",
        "_drop_process_reservation",
        "_require_current_lease",
        "_reserve_process_reservation",
        "release",
    }
    allowed_mapping_subscript_functions = {
        "_after_fork_child",
        "_attach_process_reservation_fd",
        "_drop_process_reservation",
        "_require_current_lease",
        "_reserve_process_reservation",
    }
    unsafe_authority_load_lines: list[int] = []
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Name)
            or not isinstance(node.ctx, ast.Load)
            or node.id not in authority_names
        ):
            continue
        parent = parents.get(node)
        function_name = enclosing_function_name(node)
        allowed = False
        if node.id == "_PROCESS_REGISTRY_LOCK":
            if (
                isinstance(parent, ast.withitem)
                and parent.context_expr is node
                and function_name in allowed_lock_with_functions
            ):
                allowed = True
            if (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and (
                    (function_name == "_prepare_for_fork" and parent.attr == "acquire")
                    or (function_name == "_after_fork_parent" and parent.attr == "release")
                )
            ):
                allowed = True
        else:
            if (
                isinstance(parent, ast.Subscript)
                and parent.value is node
                and function_name in allowed_mapping_subscript_functions
            ):
                allowed = True
            if (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and parent.attr == "values"
                and function_name == "_after_fork_child"
            ):
                allowed = True
            if (
                isinstance(parent, ast.For)
                and parent.iter is node
                and function_name == "_after_fork_child"
            ):
                allowed = True
        if not allowed:
            unsafe_authority_load_lines.append(node.lineno)

    def direct_lock_with(function: ast.AST) -> ast.With | None:
        for item in ast.walk(function):
            if not isinstance(item, ast.With):
                continue
            if any(
                isinstance(with_item.context_expr, ast.Name)
                and with_item.context_expr.id == "_PROCESS_REGISTRY_LOCK"
                for with_item in item.items
            ):
                return item
        return None

    def direct_mapping_contexts(value: ast.AST) -> set[type[ast.expr_context]]:
        return {
            type(item.ctx)
            for item in ast.walk(value)
            if isinstance(item, ast.Subscript)
            and isinstance(item.value, ast.Name)
            and item.value.id == "_PROCESS_RESERVATIONS"
        }

    def has_direct_lock_call(function: ast.AST, method: str) -> bool:
        return any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and item.func.attr == method
            and isinstance(item.func.value, ast.Name)
            and item.func.value.id == "_PROCESS_REGISTRY_LOCK"
            for item in ast.walk(function)
        )

    authority_operation_errors: list[str] = []
    for name, required_contexts in {
        "_reserve_process_reservation": {ast.Load, ast.Store},
        "_attach_process_reservation_fd": {ast.Load},
        "_drop_process_reservation": {ast.Load, ast.Del},
        "_require_current_lease": {ast.Load},
    }.items():
        function = function_nodes.get(name)
        locked = direct_lock_with(function) if function is not None else None
        contexts = direct_mapping_contexts(locked) if locked is not None else set()
        if locked is None or not required_contexts <= contexts:
            authority_operation_errors.append(name)
    attach = function_nodes.get("_attach_process_reservation_fd")
    attach_lock = direct_lock_with(attach) if attach is not None else None
    attach_opens_entry = attach_lock is not None and any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and qualified_name(item.func.value) == "os"
        and item.func.attr == "open"
        for item in ast.walk(attach_lock)
    )
    attach_sets_fd = attach_lock is not None and any(
        isinstance(item, ast.Attribute) and isinstance(item.ctx, ast.Store) and item.attr == "fd"
        for item in ast.walk(attach_lock)
    )
    if not attach_opens_entry or not attach_sets_fd:
        authority_operation_errors.append("_attach_process_reservation_fd-attach")
    reserve = function_nodes.get("_reserve_process_reservation")
    if reserve is not None:
        reserve_lock = direct_lock_with(reserve)
        has_key_error_branch = reserve_lock is not None and any(
            isinstance(item, ast.ExceptHandler)
            and isinstance(item.type, ast.Name)
            and item.type.id == "KeyError"
            for item in ast.walk(reserve_lock)
        )
        forbidden_enumeration = any(
            (
                isinstance(item, ast.Call)
                and isinstance(item.func, ast.Name)
                and item.func.id in {"iter", "len", "tuple"}
                and any(
                    isinstance(argument, ast.Name) and argument.id == "_PROCESS_RESERVATIONS"
                    for argument in item.args
                )
            )
            or (
                isinstance(item, ast.Attribute)
                and isinstance(item.value, ast.Name)
                and item.value.id == "_PROCESS_RESERVATIONS"
                and item.attr in {"items", "keys", "values"}
            )
            for item in ast.walk(reserve)
        )
        if not has_key_error_branch or forbidden_enumeration:
            authority_operation_errors.append("_reserve_process_reservation-atomic")
    prepare = function_nodes.get("_prepare_for_fork")
    if prepare is None or not has_direct_lock_call(prepare, "acquire"):
        authority_operation_errors.append("_prepare_for_fork")
    parent_callback = function_nodes.get("_after_fork_parent")
    if parent_callback is None or not has_direct_lock_call(parent_callback, "release"):
        authority_operation_errors.append("_after_fork_parent")
    child_callback = function_nodes.get("_after_fork_child")
    if child_callback is None:
        authority_operation_errors.append("_after_fork_child")
    else:
        child_names = {item.id for item in ast.walk(child_callback) if isinstance(item, ast.Name)}
        child_globals = {
            name
            for item in ast.walk(child_callback)
            if isinstance(item, ast.Global)
            for name in item.names
        }
        child_has_close = any(
            isinstance(item, ast.Call)
            and isinstance(item.func, ast.Attribute)
            and qualified_name(item.func.value) == "os"
            and item.func.attr == "close"
            for item in ast.walk(child_callback)
        )
        child_resets_mapping = any(
            isinstance(item, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "_PROCESS_RESERVATIONS"
                for target in (item.targets if isinstance(item, ast.Assign) else [item.target])
            )
            and isinstance(item.value, ast.Dict)
            for item in ast.walk(child_callback)
        )
        child_resets_lock = any(
            isinstance(item, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "_PROCESS_REGISTRY_LOCK"
                for target in (item.targets if isinstance(item, ast.Assign) else [item.target])
            )
            and isinstance(item.value, ast.Call)
            and qualified_name(item.value.func) == "threading.RLock"
            for item in ast.walk(child_callback)
        )
        if (
            not {"_PROCESS_REGISTRY_LOCK", "_PROCESS_RESERVATIONS"} <= child_names
            or child_globals != {"_PROCESS_REGISTRY_LOCK", "_PROCESS_RESERVATIONS"}
            or not child_has_close
            or not child_resets_mapping
            or not child_resets_lock
        ):
            authority_operation_errors.append("_after_fork_child")

    assert calls.isdisjoint(forbidden_calls)
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert attributes.isdisjoint(forbidden_calls)
    assert calls.isdisjoint(forbidden_time_calls)
    assert attributes.isdisjoint(forbidden_time_calls)
    assert bare_calls.isdisjoint(direct_path_calls)
    assert unsafe_non_os_path_call_lines == []
    assert unsafe_captured_call_lines == []
    assert unsafe_dynamic_lookup_lines == []
    assert unsafe_indirect_call_lines == []
    assert unsafe_fcntl_flag_call_lines == []
    assert unsafe_module_attribute_lines == []
    assert unsafe_module_object_load_lines == []
    assert unsafe_sensitive_reference_lines == []
    assert unsafe_getattr_capture_lines == []
    assert unsafe_sensitive_default_lines == []
    assert unsafe_top_level_statement_lines == []
    assert unsafe_decorator_lines == []
    assert unsafe_class_definition_lines == []
    assert unsafe_nested_function_lines == []
    assert unsafe_lambda_lines == []
    assert unsafe_class_body_lines == []
    assert unsafe_type_call_lines == []
    assert adapter_constructor_errors == []
    allowed_imports = {
        "__future__",
        "__future__.annotations",
        "collections.abc",
        "collections.abc.MutableMapping",
        "collections.abc.Set",
        "enum",
        "enum.StrEnum",
        "errno",
        "fcntl",
        "hashlib",
        "os",
        "pathlib",
        "pathlib.Path",
        "re",
        "secrets",
        "stat",
        "sys",
        "threading",
    }
    assert imported_modules <= allowed_imports
    assert unsafe_import_alias_lines == []
    assert attributes.isdisjoint(forbidden_stat_fields)
    assert "vibecad.runtime.status" not in imported_modules
    assert unsafe_broad_exception_default_lines == []
    assert unsafe_broad_exception_reference_lines == []
    assert unsafe_allowed_exception_rebinding_lines == []
    assert forbidden_name_reference_lines == []
    assert unsafe_import_call_lines == []
    assert unsafe_compile_reference_lines == []
    assert unsafe_mutable_assignment_lines == []
    assert unsafe_mutable_default_lines == []
    assert unsafe_shared_attribute_assignment_lines == []
    assert unsafe_global_names == set()
    assert unsafe_registry_lock_constructor_lines == []
    assert missing_authority_references == {}
    assert unsafe_authority_alias_lines == []
    assert unsafe_authority_default_lines == []
    assert unsafe_authority_load_lines == []
    assert authority_operation_errors == []
    assert broad_handlers == set()
    assert broad_suppressed == set()
