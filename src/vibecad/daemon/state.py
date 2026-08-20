"""Identity-pinned run state for the authenticated local Task Kernel."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.application.data import ApplicationDataError, ApplicationDataLayout
from vibecad.interaction.protocol_v2 import (
    MAX_V2_CONNECTIONS,
    V2_PROTOCOL,
    V2_VERSION,
)
from vibecad.interaction.storage import SafeRoot, StorageFailure

if sys.platform == "win32":
    from vibecad._file_compat import (
        WindowsPathCapability,
        capture_windows_fd,
        capture_windows_path,
        delete_windows_file,
        ensure_private_directory,
        open_private_file,
        rename_windows_file,
        validate_windows_path,
    )
    from vibecad.daemon.windows_ipc import (
        WindowsNamedPipeListener,
        connect_named_pipe,
        named_pipe_name,
    )

DAEMON_AUTHORITY = "vibecad.kernel-daemon.authority.v1"
DAEMON_DIRECTORY_NAME = "daemon"
DAEMON_ENDPOINT_NAME = "kernel.sock"
DAEMON_RECEIPT_NAME = "receipt.json"
DAEMON_SECRET_NAME = "boot-secret"

_SCHEMA_VERSION = 1
_MAX_RECEIPT_BYTES = 8_192
_SECRET_BYTES = hashlib.sha256().digest_size
_MAX_SAFE_INTEGER = 2**63 - 1
_DAEMON_ID = re.compile(r"daemon_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TEMP_ENTRY = re.compile(r"\.(?:receipt\.json|boot-secret)\.[0-9a-f]{32}\.tmp\Z")
_FIXED_ENTRIES = frozenset(
    {
        DAEMON_ENDPOINT_NAME,
        DAEMON_RECEIPT_NAME,
        DAEMON_SECRET_NAME,
    }
)


class DaemonErrorCode(StrEnum):
    INVALID_ROOT = "invalid_root"
    UNSAFE_ROOT = "unsafe_root"
    CONTENDED = "contended"
    RECOVERY_REQUIRED = "recovery_required"
    IO_ERROR = "io_error"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    WRONG_PROCESS = "wrong_process"
    INVALID_STATE = "invalid_state"
    AUTHENTICATION_FAILED = "authentication_failed"
    UNAVAILABLE = "unavailable"


_MESSAGES = {
    DaemonErrorCode.INVALID_ROOT: "The local daemon root is invalid.",
    DaemonErrorCode.UNSAFE_ROOT: "The local daemon root is unsafe.",
    DaemonErrorCode.CONTENDED: "Another local Task Kernel owns this data root.",
    DaemonErrorCode.RECOVERY_REQUIRED: "The local daemon state requires recovery.",
    DaemonErrorCode.IO_ERROR: "The local daemon state operation failed.",
    DaemonErrorCode.UNSUPPORTED_PLATFORM: "The local daemon platform is unsupported.",
    DaemonErrorCode.WRONG_PROCESS: "The local daemon belongs to another process.",
    DaemonErrorCode.INVALID_STATE: "The local daemon is not in the required state.",
    DaemonErrorCode.AUTHENTICATION_FAILED: "Local daemon authentication failed.",
    DaemonErrorCode.UNAVAILABLE: "The local daemon is unavailable.",
}


class DaemonError(RuntimeError):
    __slots__ = ("code", "message")

    def __init__(self, code: DaemonErrorCode) -> None:
        if type(code) is not DaemonErrorCode:
            raise TypeError("code must be a DaemonErrorCode")
        self.code = code
        self.message = _MESSAGES[code]
        super().__init__(self.message)


def _integer(value: object, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or not minimum <= value <= _MAX_SAFE_INTEGER:
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class DaemonFileBinding:
    dev: int
    ino: int
    mode: int
    uid: int
    gid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        for value in (
            self.dev,
            self.ino,
            self.mode,
            self.uid,
            self.gid,
            self.nlink,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
        ):
            _integer(value)

    def to_mapping(self) -> dict[str, int]:
        return {
            "dev": self.dev,
            "ino": self.ino,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "nlink": self.nlink,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DaemonFileBinding:
        fields = {
            "dev",
            "ino",
            "mode",
            "uid",
            "gid",
            "nlink",
            "size",
            "mtime_ns",
            "ctime_ns",
        }
        if type(value) is not dict or set(value) != fields:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        return cls(**value)


@dataclass(frozen=True, slots=True, kw_only=True)
class DaemonEndpointBinding:
    dev: int
    ino: int
    mode: int
    uid: int
    gid: int
    nlink: int
    ctime_ns: int

    def __post_init__(self) -> None:
        for value in (
            self.dev,
            self.ino,
            self.mode,
            self.uid,
            self.gid,
            self.nlink,
            self.ctime_ns,
        ):
            _integer(value)

    def to_mapping(self) -> dict[str, int]:
        return {
            "dev": self.dev,
            "ino": self.ino,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "nlink": self.nlink,
            "ctime_ns": self.ctime_ns,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DaemonEndpointBinding:
        fields = {"dev", "ino", "mode", "uid", "gid", "nlink", "ctime_ns"}
        if type(value) is not dict or set(value) != fields:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        return cls(**value)


@dataclass(frozen=True, slots=True, kw_only=True)
class DaemonReceipt:
    daemon_id: str
    pid: int
    started_ns: int
    run_root_dev: int
    run_root_ino: int
    data_root_dev: int
    data_root_ino: int
    lock_root_dev: int
    lock_root_ino: int
    endpoint: DaemonEndpointBinding
    secret: DaemonFileBinding
    secret_sha256: str

    def __post_init__(self) -> None:
        if type(self.daemon_id) is not str or _DAEMON_ID.fullmatch(self.daemon_id) is None:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        _integer(self.pid, positive=True)
        for value in (
            self.started_ns,
            self.run_root_dev,
            self.run_root_ino,
            self.data_root_dev,
            self.data_root_ino,
            self.lock_root_dev,
            self.lock_root_ino,
        ):
            _integer(value)
        if (
            type(self.endpoint) is not DaemonEndpointBinding
            or type(self.secret) is not DaemonFileBinding
            or type(self.secret_sha256) is not str
            or _DIGEST.fullmatch(self.secret_sha256) is None
        ):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "protocol": {
                "name": V2_PROTOCOL,
                "major": V2_VERSION[0],
                "minor": V2_VERSION[1],
            },
            "daemon_id": self.daemon_id,
            "pid": self.pid,
            "started_ns": self.started_ns,
            "endpoint_name": DAEMON_ENDPOINT_NAME,
            "run_root": {"dev": self.run_root_dev, "ino": self.run_root_ino},
            "data_root": {"dev": self.data_root_dev, "ino": self.data_root_ino},
            "lock_root": {"dev": self.lock_root_dev, "ino": self.lock_root_ino},
            "endpoint": self.endpoint.to_mapping(),
            "secret": self.secret.to_mapping(),
            "secret_sha256": self.secret_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DaemonReceipt:
        fields = {
            "schema_version",
            "protocol",
            "daemon_id",
            "pid",
            "started_ns",
            "endpoint_name",
            "run_root",
            "data_root",
            "lock_root",
            "endpoint",
            "secret",
            "secret_sha256",
        }
        if type(value) is not dict or set(value) != fields:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        protocol = value["protocol"]
        if (
            value["schema_version"] != _SCHEMA_VERSION
            or value["endpoint_name"] != DAEMON_ENDPOINT_NAME
            or type(protocol) is not dict
            or protocol
            != {
                "name": V2_PROTOCOL,
                "major": V2_VERSION[0],
                "minor": V2_VERSION[1],
            }
        ):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)

        def identity(name: str) -> tuple[int, int]:
            candidate = value[name]
            if type(candidate) is not dict or set(candidate) != {"dev", "ino"}:
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            return _integer(candidate["dev"]), _integer(candidate["ino"])

        run_dev, run_ino = identity("run_root")
        data_dev, data_ino = identity("data_root")
        lock_dev, lock_ino = identity("lock_root")
        return cls(
            daemon_id=value["daemon_id"],
            pid=value["pid"],
            started_ns=value["started_ns"],
            run_root_dev=run_dev,
            run_root_ino=run_ino,
            data_root_dev=data_dev,
            data_root_ino=data_ino,
            lock_root_dev=lock_dev,
            lock_root_ino=lock_ino,
            endpoint=DaemonEndpointBinding.from_mapping(value["endpoint"]),
            secret=DaemonFileBinding.from_mapping(value["secret"]),
            secret_sha256=value["secret_sha256"],
        )


@dataclass(frozen=True, slots=True)
class PublishedDaemonState:
    root: SafeRoot
    receipt: DaemonReceipt
    receipt_raw: bytes
    receipt_binding: DaemonFileBinding
    secret: bytes


def daemon_run_root(data_root: object) -> Path:
    if type(data_root) is str:
        root = Path(data_root)
    elif type(data_root) is type(Path("/")):
        root = data_root
    else:
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    if not root.is_absolute() or ".." in root.parts or root == Path(root.anchor):
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    return root / DAEMON_DIRECTORY_NAME


def _canonical_receipt(receipt: DaemonReceipt) -> bytes:
    try:
        return json.dumps(
            receipt.to_mapping(),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None


def _parse_receipt(raw: bytes) -> DaemonReceipt:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_RECEIPT_BYTES:
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    try:
        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_invalid_json_constant,
        )
    except (UnicodeError, ValueError, TypeError):
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
    receipt = DaemonReceipt.from_mapping(value)
    if _canonical_receipt(receipt) != raw:
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    return receipt


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_json_constant(_raw: str) -> object:
    raise ValueError


def _file_binding(value: os.stat_result) -> DaemonFileBinding:
    return DaemonFileBinding(
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


def _endpoint_binding(value: os.stat_result) -> DaemonEndpointBinding:
    return DaemonEndpointBinding(
        dev=value.st_dev,
        ino=value.st_ino,
        mode=value.st_mode,
        uid=value.st_uid,
        gid=value.st_gid,
        nlink=value.st_nlink,
        ctime_ns=value.st_ctime_ns,
    )


def _same_file(value: os.stat_result, binding: DaemonFileBinding) -> bool:
    return _file_binding(value) == binding


def _windows_root_capability(root: SafeRoot) -> WindowsPathCapability:
    """Recapture the exact private run root through its native File ID."""

    try:
        capability = capture_windows_path(root.path, directory=True)
        if (capability.volume, capability.file_id) != root.identity:
            raise OSError("daemon run root identity changed")
        return capability
    except (OSError, TypeError, ValueError):
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None


def _windows_stable_file_stat(value: os.stat_result) -> tuple[int, ...]:
    """Return real stable NT file metadata, excluding path-varying ChangeTime."""

    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        int(getattr(value, "st_birthtime_ns", value.st_ctime_ns)),
        int(getattr(value, "st_file_attributes", 0)),
        int(getattr(value, "st_reparse_tag", 0)),
    )


def _safe_endpoint(root: SafeRoot, value: os.stat_result) -> bool:
    if sys.platform == "win32":
        attributes = getattr(value, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return (
            stat.S_ISREG(value.st_mode)
            and not attributes & reparse
            and value.st_nlink == 1
            and value.st_dev == root.identity[0]
            and 0 < value.st_size <= 256
        )
    return (
        stat.S_ISSOCK(value.st_mode)
        and value.st_uid == root.uid
        and stat.S_IMODE(value.st_mode) == 0o600
        and value.st_nlink == 1
        and value.st_dev == root.identity[0]
    )


def _stat_entry(
    root_fd: int,
    name: str,
    *,
    root: SafeRoot | None = None,
) -> os.stat_result | None:
    try:
        if sys.platform == "win32":
            if type(root) is not SafeRoot:
                raise DaemonError(DaemonErrorCode.INVALID_STATE)
            return os.stat(root.path / name, follow_symlinks=False)
        return os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise DaemonError(DaemonErrorCode.UNSAFE_ROOT) from None


def _require_endpoint(
    root: SafeRoot,
    *,
    expected: DaemonEndpointBinding | None = None,
) -> DaemonEndpointBinding:
    if sys.platform == "win32":
        try:
            root_capability = _windows_root_capability(root)
            endpoint_path = root.path / DAEMON_ENDPOINT_NAME
            capability = capture_windows_path(endpoint_path, directory=False)
            current = endpoint_path.stat(follow_symlinks=False)
            if (
                capability.volume != root_capability.volume
                or (capability.volume, capability.file_id)
                != (current.st_dev, current.st_ino)
                or not _safe_endpoint(root, current)
            ):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            binding = _endpoint_binding(current)
            if expected is not None and binding != expected:
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            return binding
        except DaemonError:
            raise
        except OSError:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
    root_fd = -1
    try:
        root_fd = root.open()
        current = _stat_entry(root_fd, DAEMON_ENDPOINT_NAME, root=root)
        if current is None or not _safe_endpoint(root, current):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        binding = _endpoint_binding(current)
        if expected is not None and binding != expected:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        return binding
    except StorageFailure:
        raise DaemonError(DaemonErrorCode.UNSAFE_ROOT) from None
    finally:
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                raise DaemonError(DaemonErrorCode.IO_ERROR) from None


def prepare_run_root(layout: ApplicationDataLayout) -> SafeRoot:
    if type(layout) is not ApplicationDataLayout:
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    if sys.platform == "win32":
        try:
            layout.require_current(layout.root)
            layout.require_current(layout.locks)
            path = daemon_run_root(layout.root)
            data_root = capture_windows_path(layout.root, directory=True)
            if (data_root.volume, data_root.file_id) != layout.identity_for(layout.root):
                raise DaemonError(DaemonErrorCode.UNSAFE_ROOT)
            capability = ensure_private_directory(
                path,
                expected_parent=data_root,
            )
            run_root = SafeRoot(path)
            if run_root.identity != (capability.volume, capability.file_id):
                raise DaemonError(DaemonErrorCode.UNSAFE_ROOT)
            layout.require_current(layout.root)
            layout.require_current(layout.locks)
            return run_root
        except DaemonError:
            raise
        except (ApplicationDataError, StorageFailure, OSError):
            raise DaemonError(DaemonErrorCode.UNSAFE_ROOT) from None
    data_root = None
    root_fd = -1
    child_fd = -1
    try:
        layout.require_current(layout.root)
        layout.require_current(layout.locks)
        data_root = SafeRoot(layout.root)
        root_fd = data_root.open()
        created = False
        try:
            os.mkdir(DAEMON_DIRECTORY_NAME, 0o700, dir_fd=root_fd)
            created = True
        except FileExistsError:
            pass
        child_fd, child_stat = data_root.open_directory_at(root_fd, DAEMON_DIRECTORY_NAME)
        if created:
            os.fsync(root_fd)
        run_root = SafeRoot(daemon_run_root(layout.root))
        if run_root.identity != (child_stat.st_dev, child_stat.st_ino):
            raise DaemonError(DaemonErrorCode.UNSAFE_ROOT)
        layout.require_current(layout.root)
        layout.require_current(layout.locks)
        return run_root
    except DaemonError:
        raise
    except (ApplicationDataError, StorageFailure, OSError):
        raise DaemonError(DaemonErrorCode.UNSAFE_ROOT) from None
    finally:
        for fd in (child_fd, root_fd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)


def bind_endpoint(root: SafeRoot) -> tuple[object, DaemonEndpointBinding]:
    if type(root) is not SafeRoot:
        raise DaemonError(DaemonErrorCode.UNSAFE_ROOT)
    if sys.platform == "win32":
        listener = None
        marker = root.path / DAEMON_ENDPOINT_NAME
        marker_capability = None
        published = False
        try:
            if os.path.lexists(marker):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            parent = _windows_root_capability(root)
            name = named_pipe_name(root.path)
            listener = WindowsNamedPipeListener(name)
            raw = name.encode("ascii")
            descriptor, marker_capability = open_private_file(
                marker,
                create=True,
                read_write=True,
                exclusive=True,
                expected_parent=parent,
            )
            try:
                os.set_inheritable(descriptor, False)
                written = os.write(descriptor, raw)
                if written != len(raw):
                    raise OSError("short daemon endpoint marker write")
                os.fsync(descriptor)
                opened = os.fstat(descriptor)
                recaptured = capture_windows_fd(
                    descriptor,
                    directory=False,
                    generation_token=marker_capability.generation_token,
                )
                if recaptured != marker_capability:
                    raise OSError("daemon endpoint marker identity changed")
            finally:
                os.close(descriptor)
            validate_windows_path(marker_capability, directory=False)
            current = marker.stat(follow_symlinks=False)
            if (
                not _safe_endpoint(root, current)
                or (marker_capability.volume, marker_capability.file_id)
                != (current.st_dev, current.st_ino)
                or _windows_stable_file_stat(current)
                != _windows_stable_file_stat(opened)
                or listener.get_inheritable()
            ):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            binding = _endpoint_binding(current)
            published = True
            return listener, binding
        except DaemonError:
            raise
        except (OSError, StorageFailure):
            raise DaemonError(DaemonErrorCode.IO_ERROR) from None
        finally:
            if not published:
                if listener is not None:
                    with contextlib.suppress(OSError):
                        listener.close()
                if marker_capability is not None:
                    with contextlib.suppress(OSError):
                        delete_windows_file(
                            marker,
                            parent=_windows_root_capability(root),
                            expected=marker_capability,
                        )
    endpoint_path = root.path / DAEMON_ENDPOINT_NAME
    try:
        encoded = os.fsencode(endpoint_path)
    except (TypeError, UnicodeError):
        raise DaemonError(DaemonErrorCode.INVALID_ROOT) from None
    if not encoded or len(encoded) > 103:
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    root_fd = -1
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    published = False
    try:
        listener.set_inheritable(False)
        root_fd = root.open()
        if _stat_entry(root_fd, DAEMON_ENDPOINT_NAME, root=root) is not None:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        previous_umask = os.umask(0o177)
        try:
            listener.bind(str(endpoint_path))
        finally:
            os.umask(previous_umask)
        os.chmod(
            DAEMON_ENDPOINT_NAME,
            0o600,
            dir_fd=root_fd,
            follow_symlinks=False,
        )
        listener.listen(MAX_V2_CONNECTIONS)
        listener.settimeout(0.2)
        current = _stat_entry(root_fd, DAEMON_ENDPOINT_NAME, root=root)
        if current is None or not _safe_endpoint(root, current) or listener.get_inheritable():
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        binding = _endpoint_binding(current)
        os.fsync(root_fd)
        published = True
        return listener, binding
    except DaemonError:
        raise
    except (OSError, StorageFailure):
        raise DaemonError(DaemonErrorCode.IO_ERROR) from None
    finally:
        if root_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(root_fd)
        if not published:
            with contextlib.suppress(OSError):
                listener.close()


def _read_private(
    root: SafeRoot,
    name: str,
    *,
    maximum: int,
) -> tuple[bytes, DaemonFileBinding]:
    if sys.platform == "win32":
        path = root.path / name
        descriptor = -1
        try:
            parent = _windows_root_capability(root)
            descriptor, capability = open_private_file(
                path,
                create=False,
                read_write=False,
                expected_parent=parent,
            )
            os.set_inheritable(descriptor, False)
            before = os.fstat(descriptor)
            attributes = getattr(before, "st_file_attributes", 0)
            reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if (
                not stat.S_ISREG(before.st_mode)
                or attributes & reparse
                or before.st_nlink != 1
                or not 0 < before.st_size <= maximum
                or before.st_dev != root.identity[0]
            ):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            chunks = bytearray()
            while len(chunks) <= maximum:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(chunks)))
                if not chunk:
                    break
                chunks.extend(chunk)
            after = os.fstat(descriptor)
            recaptured = capture_windows_fd(
                descriptor,
                directory=False,
                generation_token=capability.generation_token,
            )
            validate_windows_path(capability, directory=False)
            if (
                not chunks
                or len(chunks) > maximum
                or _windows_stable_file_stat(after)
                != _windows_stable_file_stat(before)
                or recaptured != capability
            ):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            return bytes(chunks), _file_binding(after)
        except DaemonError:
            raise
        except OSError:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
        finally:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
    root_fd = -1
    try:
        root_fd = root.open()
        raw, value = root.read_file_at(root_fd, name, maximum=maximum)
        return raw, _file_binding(value)
    except StorageFailure:
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
    finally:
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                raise DaemonError(DaemonErrorCode.IO_ERROR) from None


def _write_private(root: SafeRoot, name: str, payload: bytes) -> None:
    """Publish one immutable private state file without replacing a winner."""

    if sys.platform != "win32":
        root_fd = root.open()
        try:
            root.atomic_write(root_fd, name, payload, token=secrets.token_hex(16))
            return
        finally:
            os.close(root_fd)
    token = secrets.token_hex(16)
    temporary = root.path / f".{name}.{token}.tmp"
    target = root.path / name
    descriptor = -1
    temporary_capability = None
    published = False
    try:
        if os.path.lexists(target):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        parent = _windows_root_capability(root)
        descriptor, temporary_capability = open_private_file(
            temporary,
            create=True,
            read_write=True,
            exclusive=True,
            expected_parent=parent,
        )
        os.set_inheritable(descriptor, False)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short private state write")
            view = view[written:]
        os.fsync(descriptor)
        recaptured = capture_windows_fd(
            descriptor,
            directory=False,
            generation_token=temporary_capability.generation_token,
        )
        if recaptured != temporary_capability:
            raise OSError("private state temporary identity changed")
        os.close(descriptor)
        descriptor = -1
        published_capability = rename_windows_file(
            temporary,
            target,
            source_parent=parent,
            expected_source=temporary_capability,
        )
        validate_windows_path(published_capability, directory=False)
        published = True
    except DaemonError:
        raise
    except OSError:
        raise DaemonError(DaemonErrorCode.IO_ERROR) from None
    finally:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if not published and temporary_capability is not None:
            with contextlib.suppress(OSError):
                delete_windows_file(
                    temporary,
                    parent=_windows_root_capability(root),
                    expected=temporary_capability,
                )


def publish_boot_state(
    *,
    root: SafeRoot,
    layout: ApplicationDataLayout,
    daemon_id: str,
    started_ns: int,
    endpoint: DaemonEndpointBinding,
) -> PublishedDaemonState:
    if (
        type(root) is not SafeRoot
        or type(layout) is not ApplicationDataLayout
        or type(daemon_id) is not str
        or _DAEMON_ID.fullmatch(daemon_id) is None
        or type(endpoint) is not DaemonEndpointBinding
    ):
        raise DaemonError(DaemonErrorCode.INVALID_STATE)
    _integer(started_ns)
    secret = secrets.token_bytes(_SECRET_BYTES)
    root_fd = -1
    try:
        layout.require_current(layout.root)
        layout.require_current(layout.locks)
        _require_endpoint(root, expected=endpoint)
        if sys.platform != "win32":
            root_fd = root.open()
        if any(
            (
                os.path.lexists(root.path / name)
                if sys.platform == "win32"
                else _stat_entry(root_fd, name, root=root) is not None
            )
            for name in (DAEMON_SECRET_NAME, DAEMON_RECEIPT_NAME)
        ):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        _write_private(root, DAEMON_SECRET_NAME, secret)
        secret_raw, secret_binding = _read_private(
            root,
            DAEMON_SECRET_NAME,
            maximum=_SECRET_BYTES,
        )
        if secret_raw != secret or secret_binding.size != _SECRET_BYTES:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        data_identity = layout.identity_for(layout.root)
        lock_identity = layout.identity_for(layout.locks)
        receipt = DaemonReceipt(
            daemon_id=daemon_id,
            pid=os.getpid(),
            started_ns=started_ns,
            run_root_dev=root.identity[0],
            run_root_ino=root.identity[1],
            data_root_dev=data_identity[0],
            data_root_ino=data_identity[1],
            lock_root_dev=lock_identity[0],
            lock_root_ino=lock_identity[1],
            endpoint=endpoint,
            secret=secret_binding,
            secret_sha256=hashlib.sha256(secret).hexdigest(),
        )
        receipt_raw = _canonical_receipt(receipt)
        _write_private(root, DAEMON_RECEIPT_NAME, receipt_raw)
        persisted_raw, receipt_binding = _read_private(
            root,
            DAEMON_RECEIPT_NAME,
            maximum=_MAX_RECEIPT_BYTES,
        )
        if persisted_raw != receipt_raw or _parse_receipt(persisted_raw) != receipt:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        state = PublishedDaemonState(
            root=root,
            receipt=receipt,
            receipt_raw=receipt_raw,
            receipt_binding=receipt_binding,
            secret=secret,
        )
        require_published_state(state, layout=layout)
        return state
    except DaemonError:
        raise
    except (ApplicationDataError, StorageFailure, OSError):
        raise DaemonError(DaemonErrorCode.IO_ERROR) from None
    finally:
        if root_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(root_fd)


def require_published_state(
    state: PublishedDaemonState,
    *,
    layout: ApplicationDataLayout,
) -> None:
    if type(state) is not PublishedDaemonState or type(layout) is not ApplicationDataLayout:
        raise DaemonError(DaemonErrorCode.INVALID_STATE)
    try:
        layout.require_current(layout.root)
        layout.require_current(layout.locks)
    except ApplicationDataError:
        raise DaemonError(DaemonErrorCode.UNSAFE_ROOT) from None
    if (
        state.root.identity != (state.receipt.run_root_dev, state.receipt.run_root_ino)
        or layout.identity_for(layout.root)
        != (state.receipt.data_root_dev, state.receipt.data_root_ino)
        or layout.identity_for(layout.locks)
        != (state.receipt.lock_root_dev, state.receipt.lock_root_ino)
    ):
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    _require_endpoint(state.root, expected=state.receipt.endpoint)
    secret, secret_binding = _read_private(
        state.root,
        DAEMON_SECRET_NAME,
        maximum=_SECRET_BYTES,
    )
    receipt_raw, receipt_binding = _read_private(
        state.root,
        DAEMON_RECEIPT_NAME,
        maximum=_MAX_RECEIPT_BYTES,
    )
    if (
        secret != state.secret
        or secret_binding != state.receipt.secret
        or hashlib.sha256(secret).hexdigest() != state.receipt.secret_sha256
        or receipt_raw != state.receipt_raw
        or receipt_binding != state.receipt_binding
        or _parse_receipt(receipt_raw) != state.receipt
    ):
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)


def read_boot_state(run_root: object) -> PublishedDaemonState:
    if type(run_root) is str:
        path = Path(run_root)
    elif type(run_root) is type(Path("/")):
        path = run_root
    else:
        raise DaemonError(DaemonErrorCode.INVALID_ROOT)
    try:
        root = SafeRoot(path)
    except StorageFailure:
        raise DaemonError(DaemonErrorCode.UNSAFE_ROOT) from None
    first_raw, first_binding = _read_private(
        root,
        DAEMON_RECEIPT_NAME,
        maximum=_MAX_RECEIPT_BYTES,
    )
    receipt = _parse_receipt(first_raw)
    if root.identity != (receipt.run_root_dev, receipt.run_root_ino):
        raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED)
    endpoint = _require_endpoint(root, expected=receipt.endpoint)
    secret, secret_binding = _read_private(
        root,
        DAEMON_SECRET_NAME,
        maximum=_SECRET_BYTES,
    )
    second_raw, second_binding = _read_private(
        root,
        DAEMON_RECEIPT_NAME,
        maximum=_MAX_RECEIPT_BYTES,
    )
    if (
        endpoint != receipt.endpoint
        or secret_binding != receipt.secret
        or len(secret) != _SECRET_BYTES
        or hashlib.sha256(secret).hexdigest() != receipt.secret_sha256
        or second_raw != first_raw
        or second_binding != first_binding
    ):
        raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED)
    return PublishedDaemonState(
        root=root,
        receipt=receipt,
        receipt_raw=first_raw,
        receipt_binding=first_binding,
        secret=secret,
    )


def _endpoint_accepts(root: SafeRoot) -> bool:
    if sys.platform == "win32":
        connection = None
        try:
            connection = connect_named_pipe(named_pipe_name(root.path), timeout=0.1)
            return True
        except (TimeoutError, FileNotFoundError, OSError):
            return False
        finally:
            if connection is not None:
                with contextlib.suppress(OSError):
                    connection.close()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(0.1)
        connection.connect(str(root.path / DAEMON_ENDPOINT_NAME))
        return True
    except (TimeoutError, ConnectionRefusedError, FileNotFoundError, OSError):
        return False
    finally:
        with contextlib.suppress(OSError):
            connection.close()


def _unlink_exact(
    root: SafeRoot,
    root_fd: int,
    name: str,
    *,
    file_binding: DaemonFileBinding | None = None,
    endpoint_binding: DaemonEndpointBinding | None = None,
) -> None:
    if sys.platform == "win32":
        path = root.path / name
        parent = _windows_root_capability(root)
        try:
            capability = capture_windows_path(path, directory=False)
            current = path.stat(follow_symlinks=False)
            if (capability.volume, capability.file_id) != (
                current.st_dev,
                current.st_ino,
            ):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            if file_binding is not None:
                _raw, observed = _read_private(
                    root,
                    name,
                    maximum=max(file_binding.size, 1),
                )
                if observed != file_binding:
                    raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            elif endpoint_binding is not None:
                if (
                    not _safe_endpoint(root, current)
                    or _endpoint_binding(current) != endpoint_binding
                ):
                    raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            else:
                raise DaemonError(DaemonErrorCode.INVALID_STATE)
            delete_windows_file(path, parent=parent, expected=capability)
            return
        except DaemonError:
            raise
        except OSError:
            raise DaemonError(DaemonErrorCode.IO_ERROR) from None
    current = _stat_entry(root_fd, name, root=root)
    if current is None:
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    if file_binding is not None:
        if not root.regular_file(current, maximum=max(file_binding.size, 1)):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        if not _same_file(current, file_binding):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    elif endpoint_binding is not None:
        if not _safe_endpoint(root, current) or _endpoint_binding(current) != endpoint_binding:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    else:
        raise DaemonError(DaemonErrorCode.INVALID_STATE)
    try:
        os.unlink(name, dir_fd=root_fd)
    except OSError:
        raise DaemonError(DaemonErrorCode.IO_ERROR) from None


def cleanup_published_state(state: PublishedDaemonState) -> None:
    if type(state) is not PublishedDaemonState:
        raise DaemonError(DaemonErrorCode.INVALID_STATE)
    root_fd = -1
    try:
        if sys.platform != "win32":
            root_fd = state.root.open()
        _unlink_exact(
            state.root,
            root_fd,
            DAEMON_RECEIPT_NAME,
            file_binding=state.receipt_binding,
        )
        if sys.platform != "win32":
            os.fsync(root_fd)
        _unlink_exact(
            state.root,
            root_fd,
            DAEMON_SECRET_NAME,
            file_binding=state.receipt.secret,
        )
        _unlink_exact(
            state.root,
            root_fd,
            DAEMON_ENDPOINT_NAME,
            endpoint_binding=state.receipt.endpoint,
        )
        if sys.platform != "win32":
            os.fsync(root_fd)
    except StorageFailure:
        raise DaemonError(DaemonErrorCode.UNSAFE_ROOT) from None
    finally:
        if root_fd >= 0:
            try:
                os.close(root_fd)
            except OSError:
                raise DaemonError(DaemonErrorCode.IO_ERROR) from None


def _remove_incomplete_entry(root: SafeRoot, root_fd: int, name: str) -> None:
    if sys.platform == "win32":
        path = root.path / name
        try:
            parent = _windows_root_capability(root)
            capability = capture_windows_path(path, directory=False)
            current = path.stat(follow_symlinks=False)
            if (capability.volume, capability.file_id) != (
                current.st_dev,
                current.st_ino,
            ):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            if name == DAEMON_ENDPOINT_NAME:
                if not _safe_endpoint(root, current):
                    raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            else:
                maximum = (
                    _SECRET_BYTES if name == DAEMON_SECRET_NAME else _MAX_RECEIPT_BYTES
                )
                if not root.regular_file(current, maximum=maximum):
                    raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            delete_windows_file(path, parent=parent, expected=capability)
            return
        except DaemonError:
            raise
        except OSError:
            raise DaemonError(DaemonErrorCode.IO_ERROR) from None
    current = _stat_entry(root_fd, name, root=root)
    if current is None:
        return
    if name == DAEMON_ENDPOINT_NAME:
        if not _safe_endpoint(root, current):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    else:
        maximum = _SECRET_BYTES if name == DAEMON_SECRET_NAME else _MAX_RECEIPT_BYTES
        if not root.regular_file(current, maximum=maximum):
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
    try:
        os.unlink(name, dir_fd=root_fd)
    except OSError:
        raise DaemonError(DaemonErrorCode.IO_ERROR) from None


def recover_stale_state(
    root: SafeRoot,
    *,
    layout: ApplicationDataLayout,
) -> None:
    if type(root) is not SafeRoot or type(layout) is not ApplicationDataLayout:
        raise DaemonError(DaemonErrorCode.INVALID_STATE)
    root_fd = -1
    try:
        layout.require_current(layout.root)
        layout.require_current(layout.locks)
        if sys.platform == "win32":
            names = tuple(entry.name for entry in os.scandir(root.path))
        else:
            root_fd = root.open()
            names = tuple(entry.name for entry in os.scandir(root_fd))
        unknown = tuple(
            name
            for name in names
            if name not in _FIXED_ENTRIES and _TEMP_ENTRY.fullmatch(name) is None
        )
        if unknown:
            raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        for name in names:
            if _TEMP_ENTRY.fullmatch(name) is not None:
                _remove_incomplete_entry(root, root_fd, name)
        names = {
            name
            for name in _FIXED_ENTRIES
            if _stat_entry(root_fd, name, root=root) is not None
        }
        if not names:
            if sys.platform != "win32":
                os.fsync(root_fd)
            return
        if DAEMON_ENDPOINT_NAME in names and _endpoint_accepts(root):
            raise DaemonError(DaemonErrorCode.CONTENDED)
        if DAEMON_RECEIPT_NAME in names:
            if names != _FIXED_ENTRIES:
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            receipt_raw, receipt_binding = _read_private(
                root,
                DAEMON_RECEIPT_NAME,
                maximum=_MAX_RECEIPT_BYTES,
            )
            secret, secret_binding = _read_private(
                root,
                DAEMON_SECRET_NAME,
                maximum=_SECRET_BYTES,
            )
            receipt = _parse_receipt(receipt_raw)
            endpoint = _stat_entry(root_fd, DAEMON_ENDPOINT_NAME, root=root)
            if (
                endpoint is None
                or not _safe_endpoint(root, endpoint)
                or _endpoint_binding(endpoint) != receipt.endpoint
                or secret_binding != receipt.secret
                or hashlib.sha256(secret).hexdigest() != receipt.secret_sha256
                or root.identity != (receipt.run_root_dev, receipt.run_root_ino)
                or layout.identity_for(layout.root)
                != (receipt.data_root_dev, receipt.data_root_ino)
                or layout.identity_for(layout.locks)
                != (receipt.lock_root_dev, receipt.lock_root_ino)
            ):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            _unlink_exact(
                root,
                root_fd,
                DAEMON_RECEIPT_NAME,
                file_binding=receipt_binding,
            )
            if sys.platform != "win32":
                os.fsync(root_fd)
            _unlink_exact(
                root,
                root_fd,
                DAEMON_SECRET_NAME,
                file_binding=receipt.secret,
            )
            _unlink_exact(
                root,
                root_fd,
                DAEMON_ENDPOINT_NAME,
                endpoint_binding=receipt.endpoint,
            )
        else:
            for name in (DAEMON_SECRET_NAME, DAEMON_ENDPOINT_NAME):
                if name in names:
                    _remove_incomplete_entry(root, root_fd, name)
        if sys.platform != "win32":
            os.fsync(root_fd)
        layout.require_current(layout.root)
        layout.require_current(layout.locks)
    except DaemonError:
        raise
    except (ApplicationDataError, StorageFailure, OSError):
        raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
    finally:
        if root_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(root_fd)


__all__ = (
    "DAEMON_AUTHORITY",
    "DAEMON_DIRECTORY_NAME",
    "DAEMON_ENDPOINT_NAME",
    "DAEMON_RECEIPT_NAME",
    "DAEMON_SECRET_NAME",
    "DaemonEndpointBinding",
    "DaemonError",
    "DaemonErrorCode",
    "DaemonFileBinding",
    "DaemonReceipt",
    "PublishedDaemonState",
    "bind_endpoint",
    "cleanup_published_state",
    "daemon_run_root",
    "prepare_run_root",
    "publish_boot_state",
    "read_boot_state",
    "recover_stale_state",
    "require_published_state",
)
