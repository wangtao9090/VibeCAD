"""Exclusive, process-aware leases for workflow resources."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import sys
import threading
from enum import StrEnum
from pathlib import Path

__all__ = (
    "LeaseError",
    "LeaseErrorCode",
    "LeaseRootTrust",
    "ProjectWriteLease",
    "ResourceLease",
    "ResourceLeaseManager",
)

_RESOURCE_DOMAIN = b"vibecad-resource-lease-v1\0"
_OWNER_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_ID_RE = re.compile(r"^project_[0-9a-f]{32}$")
_RESOURCE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class LeaseRootTrust(StrEnum):
    TRUSTED_LOCAL = "trusted_local"


class LeaseErrorCode(StrEnum):
    INVALID_RESOURCE = "invalid_resource"
    INVALID_OWNER = "invalid_owner"
    UNTRUSTED_ROOT = "untrusted_root"
    UNSAFE_ROOT = "unsafe_root"
    UNSAFE_LOCK_ENTRY = "unsafe_lock_entry"
    CONTENDED = "contended"
    WRONG_OWNER = "wrong_owner"
    ALREADY_RELEASED = "already_released"
    WRONG_PROCESS = "wrong_process"
    INVALID_LEASE = "invalid_lease"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    LOCK_UNAVAILABLE = "lock_unavailable"
    IO_ERROR = "io_error"


def _error_message(code: LeaseErrorCode) -> str:
    match code:
        case LeaseErrorCode.INVALID_RESOURCE:
            return "The resource identifier is invalid."
        case LeaseErrorCode.INVALID_OWNER:
            return "The lease owner token is invalid."
        case LeaseErrorCode.UNTRUSTED_ROOT:
            return "The lease root is not explicitly trusted."
        case LeaseErrorCode.UNSAFE_ROOT:
            return "The lease root is unsafe."
        case LeaseErrorCode.UNSAFE_LOCK_ENTRY:
            return "The lock entry is unsafe."
        case LeaseErrorCode.CONTENDED:
            return "The resource is already leased."
        case LeaseErrorCode.WRONG_OWNER:
            return "The lease belongs to a different owner."
        case LeaseErrorCode.ALREADY_RELEASED:
            return "The lease was already released."
        case LeaseErrorCode.WRONG_PROCESS:
            return "The lease belongs to a different process."
        case LeaseErrorCode.INVALID_LEASE:
            return "The lease is invalid for this manager."
        case LeaseErrorCode.UNSUPPORTED_PLATFORM:
            return "The platform is not supported for resource leases."
        case LeaseErrorCode.LOCK_UNAVAILABLE:
            return "The required file-lock capability is unavailable."
        case LeaseErrorCode.IO_ERROR:
            return "The lease operation failed because of an I/O error."


class LeaseError(ValueError):
    def __init__(self, code: LeaseErrorCode, *, resource_key: str | None = None) -> None:
        if type(code) is not LeaseErrorCode:
            raise TypeError("code must be a LeaseErrorCode")
        if resource_key is not None:
            if type(resource_key) is not str or _RESOURCE_KEY_RE.fullmatch(resource_key) is None:
                raise ValueError("resource_key must be 64 lowercase hex characters")
        message = _error_message(code)
        self.code = code
        self.message = message
        if resource_key is not None:
            self.resource_key = resource_key
        super().__init__(message)


class _PosixFileLock:
    __slots__ = ("_flock", "_lock_ex", "_lock_nb", "_lock_un")
    platform_key = "posix"

    def __init__(self, fcntl_module) -> None:
        self._flock = fcntl_module.flock
        self._lock_ex = fcntl_module.LOCK_EX
        self._lock_nb = fcntl_module.LOCK_NB
        self._lock_un = fcntl_module.LOCK_UN

    def acquire(self, fd: int) -> None:
        native_errno = None
        try:
            self._flock(fd, self._lock_ex | self._lock_nb)
        except OSError as exc:
            native_errno = exc.errno
        if native_errno is None:
            return
        if native_errno in (errno.EACCES, errno.EAGAIN):
            raise LeaseError(LeaseErrorCode.CONTENDED)
        if native_errno in (errno.ENOSYS, errno.ENOTSUP):
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        raise LeaseError(LeaseErrorCode.IO_ERROR)

    def release(self, fd: int) -> None:
        native_errno = None
        try:
            self._flock(fd, self._lock_un)
        except OSError as exc:
            native_errno = exc.errno
        if native_errno is None:
            return
        if native_errno in (errno.ENOSYS, errno.ENOTSUP):
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        raise LeaseError(LeaseErrorCode.IO_ERROR)


class _WindowsFileLock:
    __slots__ = ("_lock", "_lock_mode", "_unlock_mode")
    platform_key = "windows"

    def __init__(self, msvcrt_module) -> None:
        self._lock = msvcrt_module.locking
        self._lock_mode = msvcrt_module.LK_NBLCK
        self._unlock_mode = msvcrt_module.LK_UNLCK

    def acquire(self, fd: int) -> None:
        native_errno = None
        native_winerror = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            self._lock(fd, self._lock_mode, 1)
        except OSError as exc:
            native_errno = exc.errno
            native_winerror = getattr(exc, "winerror", None)
        if native_errno is None:
            return
        if native_errno == errno.EACCES or native_winerror == 33:
            raise LeaseError(LeaseErrorCode.CONTENDED)
        raise LeaseError(LeaseErrorCode.IO_ERROR)

    def release(self, fd: int) -> None:
        failed = False
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            self._lock(fd, self._unlock_mode, 1)
        except OSError:
            failed = True
        if failed:
            raise LeaseError(LeaseErrorCode.IO_ERROR)


def _windows_open_flags(os_module) -> int:
    return os_module.O_RDWR | os_module.O_BINARY | os_module.O_NOINHERIT


def _select_platform_adapter(platform_name: str):
    if platform_name in ("darwin", "linux"):
        import fcntl

        return _PosixFileLock(fcntl)
    raise LeaseError(LeaseErrorCode.UNSUPPORTED_PLATFORM)


def _new_platform_adapter():
    return _select_platform_adapter(sys.platform)


def _require_posix_capabilities(os_module) -> None:
    missing = False
    try:
        if (
            type(os_module.O_RDONLY) is not int
            or type(os_module.O_DIRECTORY) is not int
            or type(os_module.O_NOFOLLOW) is not int
            or type(os_module.O_CLOEXEC) is not int
            or type(os_module.O_NONBLOCK) is not int
            or type(os_module.O_RDWR) is not int
            or type(os_module.O_CREAT) is not int
        ):
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        if type(os_module.supports_dir_fd) is not type({None}):
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        if type(os_module.supports_follow_symlinks) is not type({None}):
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        if (
            not callable(os_module.open)
            or not callable(os_module.stat)
            or not callable(os_module.fstat)
            or not callable(os_module.fsync)
            or not callable(os_module.geteuid)
            or not callable(os_module.getpid)
            or not callable(os_module.register_at_fork)
        ):
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        if os_module.open not in os_module.supports_dir_fd:
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        if os_module.stat not in os_module.supports_dir_fd:
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
        if os_module.stat not in os_module.supports_follow_symlinks:
            raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)
    except AttributeError:
        missing = True
    if missing:
        raise LeaseError(LeaseErrorCode.LOCK_UNAVAILABLE)


class _Reservation:
    __slots__ = ("fd", "owner_token")

    def __init__(self, owner_token: str) -> None:
        self.owner_token = owner_token
        self.fd = None


_PROCESS_REGISTRY_LOCK = threading.RLock()
_PROCESS_RESERVATIONS = {}


def _reserve_process_reservation(key, reservation: _Reservation) -> bool:
    with _PROCESS_REGISTRY_LOCK:
        try:
            _PROCESS_RESERVATIONS[key]
        except KeyError:
            _PROCESS_RESERVATIONS[key] = reservation
            return True
        return False


def _attach_process_reservation_fd(
    key,
    owner_token: str,
    root_fd: int,
    filename: str,
) -> int:
    with _PROCESS_REGISTRY_LOCK:
        reservation = _PROCESS_RESERVATIONS[key]
        if reservation.owner_token != owner_token:
            raise LeaseError(LeaseErrorCode.IO_ERROR)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        fd = os.open(filename, flags, 0o600, dir_fd=root_fd)
        reservation.fd = fd
        return fd


def _drop_process_reservation(key, owner_token: str) -> None:
    with _PROCESS_REGISTRY_LOCK:
        reservation = _PROCESS_RESERVATIONS[key]
        if reservation.owner_token != owner_token:
            raise LeaseError(LeaseErrorCode.IO_ERROR)
        del _PROCESS_RESERVATIONS[key]


def _prepare_for_fork() -> None:
    _PROCESS_REGISTRY_LOCK.acquire()


def _after_fork_parent() -> None:
    _PROCESS_REGISTRY_LOCK.release()


def _after_fork_child() -> None:
    global _PROCESS_REGISTRY_LOCK, _PROCESS_RESERVATIONS
    for reservation_key in _PROCESS_RESERVATIONS:
        reservation = _PROCESS_RESERVATIONS[reservation_key]
        if reservation.fd is not None:
            try:
                os.close(reservation.fd)
            except OSError:
                pass
    _PROCESS_RESERVATIONS = {}
    _PROCESS_REGISTRY_LOCK = threading.RLock()


if os.name == "posix":
    os.register_at_fork(
        before=_prepare_for_fork,
        after_in_parent=_after_fork_parent,
        after_in_child=_after_fork_child,
    )


def _coerce_root(lock_root):
    if type(lock_root) is str:
        path = Path(lock_root)
    elif type(lock_root) is type(Path("/")):
        path = lock_root
    else:
        raise LeaseError(LeaseErrorCode.UNSAFE_ROOT)
    if not path.is_absolute() or ".." in path.parts:
        raise LeaseError(LeaseErrorCode.UNSAFE_ROOT)
    return tuple(path.parts)


def _root_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_root(parts, expected_identity=None):
    current_fd = None
    failed = False
    try:
        current_fd = os.open("/", _root_flags())
        for part in parts[1:]:
            next_fd = os.open(part, _root_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
    except OSError:
        failed = True
    if failed or current_fd is None:
        if current_fd is not None:
            try:
                os.close(current_fd)
            except OSError:
                pass
        raise LeaseError(LeaseErrorCode.UNSAFE_ROOT)
    root_stat = None
    stat_failed = False
    try:
        root_stat = os.fstat(current_fd)
    except OSError:
        stat_failed = True
    if stat_failed or root_stat is None:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise LeaseError(LeaseErrorCode.UNSAFE_ROOT)
    safe = (
        stat.S_ISDIR(root_stat.st_mode)
        and root_stat.st_uid == os.geteuid()
        and stat.S_IMODE(root_stat.st_mode) == 0o700
    )
    identity = (root_stat.st_dev, root_stat.st_ino)
    if expected_identity is not None and identity != expected_identity:
        safe = False
    if not safe:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise LeaseError(LeaseErrorCode.UNSAFE_ROOT)
    return current_fd, root_stat


def _validate_entry_metadata(entry_stat) -> bool:
    return (
        stat.S_ISREG(entry_stat.st_mode)
        and entry_stat.st_uid == os.geteuid()
        and entry_stat.st_nlink == 1
        and stat.S_IMODE(entry_stat.st_mode) == 0o600
    )


def _entry_path_state(filename: str, root_fd: int):
    missing = False
    failed = False
    result = None
    try:
        result = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        missing = True
    except OSError:
        failed = True
    if failed:
        raise LeaseError(LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    if missing:
        return None
    if result is None or not _validate_entry_metadata(result):
        raise LeaseError(LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    return result


def _preopen_entry_identity(filename: str, root_fd: int):
    first = _entry_path_state(filename, root_fd)
    # A different legitimate process may atomically create the same fixed lock
    # entry after our missing probe.  In that case the descriptor/path identity
    # checks after open are the authority; sampling the pathname a second time
    # here would misclassify the benign first-creator race as an unsafe swap.
    if first is None:
        return None
    second = _entry_path_state(filename, root_fd)
    if second is None:
        raise LeaseError(LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    first_identity = (first.st_dev, first.st_ino)
    second_identity = (second.st_dev, second.st_ino)
    if first_identity != second_identity:
        raise LeaseError(LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    return first_identity


def _validate_open_entry(
    filename: str,
    root_fd: int,
    fd: int,
    expected_identity,
) -> tuple[int, int]:
    path_stat = None
    fd_stat = None
    failed = False
    try:
        path_stat = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        fd_stat = os.fstat(fd)
    except OSError:
        failed = True
    if failed or path_stat is None or fd_stat is None:
        raise LeaseError(LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    path_identity = (path_stat.st_dev, path_stat.st_ino)
    fd_identity = (fd_stat.st_dev, fd_stat.st_ino)
    safe = (
        _validate_entry_metadata(path_stat)
        and _validate_entry_metadata(fd_stat)
        and path_identity == fd_identity
    )
    if expected_identity is not None and path_identity != expected_identity:
        safe = False
    if not safe:
        raise LeaseError(LeaseErrorCode.UNSAFE_LOCK_ENTRY)
    return path_identity


def _resource_key(resource_id) -> str:
    if type(resource_id) is not str:
        raise LeaseError(LeaseErrorCode.INVALID_RESOURCE)
    if not resource_id or resource_id.isspace() or not resource_id.isprintable():
        raise LeaseError(LeaseErrorCode.INVALID_RESOURCE)
    encoded = None
    invalid_encoding = False
    try:
        encoded = resource_id.encode("utf-8")
    except UnicodeEncodeError:
        invalid_encoding = True
    if invalid_encoding or encoded is None or not 1 <= len(encoded) <= 256:
        raise LeaseError(LeaseErrorCode.INVALID_RESOURCE)
    return hashlib.sha256(_RESOURCE_DOMAIN + encoded).hexdigest()


def _validate_project_id(project_id) -> str:
    if type(project_id) is not str or _PROJECT_ID_RE.fullmatch(project_id) is None:
        raise LeaseError(LeaseErrorCode.INVALID_RESOURCE)
    return project_id


def _validate_owner_token(owner_token) -> str:
    if type(owner_token) is not str or _OWNER_TOKEN_RE.fullmatch(owner_token) is None:
        raise LeaseError(LeaseErrorCode.INVALID_OWNER)
    return owner_token


def _close_lock_fd(fd: int) -> None:
    os.close(fd)


def _cleanup_failed_acquire(adapter, locked: bool, fd, key, owner_token: str):
    cleanup_error = None
    if locked:
        try:
            adapter.release(fd)
        except LeaseError as exc:
            cleanup_error = exc
    if fd is not None:
        close_failed = False
        try:
            _close_lock_fd(fd)
        except OSError:
            close_failed = True
        if cleanup_error is None and close_failed:
            cleanup_error = LeaseError(LeaseErrorCode.IO_ERROR)
    try:
        _drop_process_reservation(key, owner_token)
    except LeaseError as exc:
        if cleanup_error is None:
            cleanup_error = exc
    return cleanup_error


class ResourceLease:
    __slots__ = (
        "_entry_identity",
        "_fd",
        "_issuer",
        "_reservation",
        "_registry_key",
        "_seal",
        "owner_token",
        "released",
        "resource_key",
    )

    def __new__(cls):
        raise TypeError("leases can only be created by ResourceLeaseManager")

    def __setattr__(self, name, value) -> None:
        raise AttributeError("lease fields are immutable")

    def require_current(self) -> None:
        _require_current_lease(self)

    def release(self, *, owner_token) -> None:
        self._issuer.release(self, owner_token=owner_token)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release(owner_token=self.owner_token)


class ProjectWriteLease(ResourceLease):
    __slots__ = ("project_id",)


def _new_lease(
    lease_type,
    issuer,
    seal,
    resource_key: str,
    owner_token: str,
    fd: int,
    registry_key,
    reservation: _Reservation,
    entry_identity: tuple[int, int],
    project_id,
):
    lease = object.__new__(lease_type)
    object.__setattr__(lease, "_issuer", issuer)
    object.__setattr__(lease, "_seal", seal)
    object.__setattr__(lease, "_fd", fd)
    object.__setattr__(lease, "_registry_key", registry_key)
    object.__setattr__(lease, "_reservation", reservation)
    object.__setattr__(lease, "_entry_identity", entry_identity)
    object.__setattr__(lease, "resource_key", resource_key)
    object.__setattr__(lease, "owner_token", owner_token)
    object.__setattr__(lease, "released", False)
    if lease_type is ProjectWriteLease:
        object.__setattr__(lease, "project_id", project_id)
    return lease


class ResourceLeaseManager:
    __slots__ = (
        "_adapter",
        "_creator_pid",
        "_root_identity",
        "_root_parts",
        "_seal",
    )

    def __init__(self, lock_root, *, trust) -> None:
        if type(trust) is not LeaseRootTrust or trust is not LeaseRootTrust.TRUSTED_LOCAL:
            raise LeaseError(LeaseErrorCode.UNTRUSTED_ROOT)
        adapter = _new_platform_adapter()
        _require_posix_capabilities(os)
        creator_pid = os.getpid()
        parts = _coerce_root(lock_root)
        root_fd, root_stat = _open_root(parts)
        close_failed = False
        try:
            os.close(root_fd)
        except OSError:
            close_failed = True
        if close_failed:
            raise LeaseError(LeaseErrorCode.IO_ERROR)
        self._adapter = adapter
        self._creator_pid = creator_pid
        self._root_parts = parts
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self._seal = object()

    def _ensure_process(self) -> None:
        if os.getpid() != self._creator_pid:
            raise LeaseError(LeaseErrorCode.WRONG_PROCESS)

    def acquire(self, resource_id):
        self._ensure_process()
        resource_key = _resource_key(resource_id)
        return self._acquire_validated(resource_key, None)

    def acquire_project_write(self, project_id):
        self._ensure_process()
        canonical = _validate_project_id(project_id)
        resource_key = _resource_key(canonical)
        return self._acquire_validated(resource_key, canonical)

    def _acquire_validated(self, resource_key: str, project_id):
        root_fd, root_stat = _open_root(self._root_parts, self._root_identity)
        owner_token = secrets.token_hex(32)
        registry_key = (
            self._adapter.platform_key,
            root_stat.st_dev,
            root_stat.st_ino,
            resource_key,
        )
        reservation = _Reservation(owner_token)
        if not _reserve_process_reservation(registry_key, reservation):
            try:
                os.close(root_fd)
            except OSError:
                pass
            raise LeaseError(LeaseErrorCode.CONTENDED, resource_key=resource_key)
        filename = resource_key + ".lock"
        fd = None
        locked = False
        entry_identity = None
        primary_error = None
        try:
            expected_entry = _preopen_entry_identity(filename, root_fd)
            fd = _attach_process_reservation_fd(
                registry_key,
                owner_token,
                root_fd,
                filename,
            )
            _validate_open_entry(filename, root_fd, fd, expected_entry)
            if expected_entry is None:
                os.fsync(root_fd)
            self._adapter.acquire(fd)
            locked = True
            entry_identity = _validate_open_entry(filename, root_fd, fd, expected_entry)
        except LeaseError as exc:
            primary_error = exc
        except OSError:
            primary_error = LeaseError(LeaseErrorCode.IO_ERROR, resource_key=resource_key)
        root_close_failed = False
        try:
            os.close(root_fd)
        except OSError:
            root_close_failed = True
        if primary_error is not None or root_close_failed:
            _cleanup_failed_acquire(
                self._adapter,
                locked,
                fd,
                registry_key,
                owner_token,
            )
            if primary_error is not None:
                raise primary_error
            raise LeaseError(LeaseErrorCode.IO_ERROR, resource_key=resource_key)
        lease_type = ResourceLease if project_id is None else ProjectWriteLease
        return _new_lease(
            lease_type,
            self,
            self._seal,
            resource_key,
            owner_token,
            fd,
            registry_key,
            reservation,
            entry_identity,
            project_id,
        )

    def release(self, lease, *, owner_token) -> None:
        self._ensure_process()
        if type(lease) is not ResourceLease and type(lease) is not ProjectWriteLease:
            raise LeaseError(LeaseErrorCode.INVALID_LEASE)
        invalid_lease = False
        try:
            if lease._issuer is not self or lease._seal is not self._seal:
                invalid_lease = True
        except AttributeError:
            invalid_lease = True
        if invalid_lease:
            raise LeaseError(LeaseErrorCode.INVALID_LEASE)
        owner = _validate_owner_token(owner_token)
        with _PROCESS_REGISTRY_LOCK:
            if lease.released:
                raise LeaseError(LeaseErrorCode.ALREADY_RELEASED, resource_key=lease.resource_key)
            if not secrets.compare_digest(owner, lease.owner_token):
                raise LeaseError(LeaseErrorCode.WRONG_OWNER, resource_key=lease.resource_key)
            object.__setattr__(lease, "released", True)
            fd = lease._fd
            registry_key = lease._registry_key
        first_error = None
        try:
            self._adapter.release(fd)
        except LeaseError as exc:
            first_error = exc
        close_failed = False
        try:
            _close_lock_fd(fd)
        except OSError:
            close_failed = True
        if first_error is None and close_failed:
            first_error = LeaseError(LeaseErrorCode.IO_ERROR, resource_key=lease.resource_key)
        try:
            _drop_process_reservation(registry_key, lease.owner_token)
        except LeaseError as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error


def _require_current_lease(lease) -> None:
    if type(lease) is not ResourceLease and type(lease) is not ProjectWriteLease:
        raise LeaseError(LeaseErrorCode.INVALID_LEASE)
    invalid_lease = False
    try:
        issuer = lease._issuer
        seal = lease._seal
        released = lease.released
        resource_key = lease.resource_key
        owner_token = lease.owner_token
        fd = lease._fd
        registry_key = lease._registry_key
        reservation = lease._reservation
        entry_identity = lease._entry_identity
    except AttributeError:
        invalid_lease = True
    if invalid_lease:
        raise LeaseError(LeaseErrorCode.INVALID_LEASE)
    if type(issuer) is not ResourceLeaseManager or seal is not issuer._seal:
        raise LeaseError(LeaseErrorCode.INVALID_LEASE)
    issuer._ensure_process()
    if released is True:
        if type(resource_key) is not str or _RESOURCE_KEY_RE.fullmatch(resource_key) is None:
            raise LeaseError(LeaseErrorCode.INVALID_LEASE)
        raise LeaseError(LeaseErrorCode.ALREADY_RELEASED, resource_key=resource_key)
    if (
        released is not False
        or type(resource_key) is not str
        or _RESOURCE_KEY_RE.fullmatch(resource_key) is None
        or type(owner_token) is not str
        or _OWNER_TOKEN_RE.fullmatch(owner_token) is None
        or type(fd) is not int
        or fd < 0
        or type(registry_key) is not tuple
        or len(registry_key) != 4
        or type(reservation) is not _Reservation
        or type(entry_identity) is not tuple
        or len(entry_identity) != 2
        or any(type(item) is not int for item in entry_identity)
    ):
        raise LeaseError(LeaseErrorCode.INVALID_LEASE)
    with _PROCESS_REGISTRY_LOCK:
        try:
            current = _PROCESS_RESERVATIONS[registry_key]
        except KeyError:
            raise LeaseError(LeaseErrorCode.INVALID_LEASE) from None
        if (
            current is not reservation
            or reservation.owner_token != owner_token
            or reservation.fd != fd
        ):
            raise LeaseError(LeaseErrorCode.INVALID_LEASE)
        root_fd = None
        primary_error = None
        try:
            root_fd, root_stat = _open_root(issuer._root_parts, issuer._root_identity)
            expected_registry_key = (
                issuer._adapter.platform_key,
                root_stat.st_dev,
                root_stat.st_ino,
                resource_key,
            )
            if registry_key != expected_registry_key:
                raise LeaseError(LeaseErrorCode.INVALID_LEASE)
            _validate_open_entry(
                resource_key + ".lock",
                root_fd,
                fd,
                entry_identity,
            )
        except LeaseError as error:
            primary_error = error
        except OSError:
            primary_error = LeaseError(LeaseErrorCode.IO_ERROR)
        close_failed = False
        if root_fd is not None:
            try:
                os.close(root_fd)
            except OSError:
                close_failed = True
        if primary_error is not None:
            raise primary_error
        if close_failed:
            raise LeaseError(LeaseErrorCode.IO_ERROR)
