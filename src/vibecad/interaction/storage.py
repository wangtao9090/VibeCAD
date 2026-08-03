"""Small fail-closed filesystem primitives for local interaction state."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

__all__ = ("CheckoutMutationLock", "SafeRoot", "StorageFailure")

_COPY_CHUNK_BYTES = 65_536
_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[tuple[int, int, str], _ProcessLock] = {}
_LOCK_REGISTRY_PID = os.getpid()
_FORKED_WITH_ACTIVE_LOCK = False


class StorageFailure(OSError):
    pass


@dataclass(slots=True)
class _ProcessLock:
    mutex: threading.Lock
    owner_thread: int | None = None
    active_fd: int | None = None


def _reset_checkout_locks_after_fork() -> None:
    """Discard thread locks and inherited flock descriptors in a fork child."""

    global _FORKED_WITH_ACTIVE_LOCK
    global _LOCK_REGISTRY
    global _LOCK_REGISTRY_GUARD
    global _LOCK_REGISTRY_PID

    inherited = tuple(_LOCK_REGISTRY.values())
    _FORKED_WITH_ACTIVE_LOCK = any(
        entry.mutex.locked() or entry.owner_thread is not None for entry in inherited
    )
    for entry in inherited:
        if entry.active_fd is not None:
            _close(entry.active_fd)
    _LOCK_REGISTRY_GUARD = threading.Lock()
    _LOCK_REGISTRY = {}
    _LOCK_REGISTRY_PID = os.getpid()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_checkout_locks_after_fork)


def _close(fd: int) -> bool:
    try:
        os.close(fd)
    except OSError:
        return False
    return True


def _require_capabilities() -> None:
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(type(getattr(os, name, None)) is not int for name in required):
        raise StorageFailure("required local storage capability is unavailable")
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise StorageFailure("required dir-fd capability is unavailable")


class SafeRoot:
    """An identity-pinned, owned 0700 local directory."""

    __slots__ = ("identity", "path", "uid")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        _require_capabilities()
        candidate = Path(path)
        if not candidate.is_absolute():
            raise StorageFailure("storage root must be absolute")
        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise StorageFailure("storage root is unavailable") from None
        if canonical != candidate:
            raise StorageFailure("storage root must not traverse links")
        self.path = candidate
        self.uid = os.geteuid()
        fd = self.open()
        try:
            info = os.fstat(fd)
            self.identity = (info.st_dev, info.st_ino)
        finally:
            if not _close(fd):
                raise StorageFailure("storage root close failed")

    def open(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags)
            info = os.fstat(fd)
        except OSError:
            raise StorageFailure("storage root is unsafe") from None
        mode = stat.S_IMODE(info.st_mode)
        expected = getattr(self, "identity", None)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or mode != 0o700
            or (expected is not None and expected != (info.st_dev, info.st_ino))
        ):
            _close(fd)
            raise StorageFailure("storage root is unsafe")
        return fd

    def open_directory_at(
        self,
        parent_fd: int,
        name: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> tuple[int, os.stat_result]:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
            info = os.fstat(fd)
        except OSError:
            raise StorageFailure("storage directory is unsafe") from None
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != self.uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_dev != self.identity[0]
            or (expected_identity is not None and expected_identity != (info.st_dev, info.st_ino))
        ):
            _close(fd)
            raise StorageFailure("storage directory is unsafe")
        return fd, info

    def read_file_at(
        self,
        parent_fd: int,
        name: str,
        *,
        maximum: int,
    ) -> tuple[bytes, os.stat_result]:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
            before = os.fstat(fd)
        except OSError:
            raise StorageFailure("storage file is unsafe") from None
        try:
            if not self.regular_file(before, maximum=maximum):
                raise StorageFailure("storage file is unsafe")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(_COPY_CHUNK_BYTES, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise StorageFailure("storage file exceeds budget")
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise StorageFailure("storage file changed during read")
            current = self.verify_file_entry(
                parent_fd,
                name,
                expected=after,
                maximum=maximum,
            )
            return b"".join(chunks), current
        finally:
            if not _close(fd):
                raise StorageFailure("storage file close failed")

    def regular_file(self, info: os.stat_result, *, maximum: int) -> bool:
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == self.uid
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and info.st_dev == self.identity[0]
            and 0 <= info.st_size <= maximum
        )

    def verify_file_entry(
        self,
        parent_fd: int,
        name: str,
        *,
        expected: os.stat_result,
        maximum: int,
    ) -> os.stat_result:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise StorageFailure("storage file entry is unsafe") from None
        expected_binding = (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_uid,
            expected.st_gid,
            expected.st_nlink,
            expected.st_size,
            expected.st_mtime_ns,
            expected.st_ctime_ns,
        )
        current_binding = (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_uid,
            current.st_gid,
            current.st_nlink,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        if not self.regular_file(current, maximum=maximum) or current_binding != expected_binding:
            raise StorageFailure("storage file entry changed")
        return current

    def verify_directory_entry(
        self,
        parent_fd: int,
        name: str,
        *,
        expected: os.stat_result,
    ) -> os.stat_result:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            raise StorageFailure("storage directory entry is unsafe") from None
        expected_binding = (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_uid,
            expected.st_gid,
            expected.st_nlink,
            expected.st_mtime_ns,
            expected.st_ctime_ns,
        )
        current_binding = (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_uid,
            current.st_gid,
            current.st_nlink,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )
        if (
            not stat.S_ISDIR(current.st_mode)
            or current.st_uid != self.uid
            or stat.S_IMODE(current.st_mode) != 0o700
            or current.st_dev != self.identity[0]
            or current_binding != expected_binding
        ):
            raise StorageFailure("storage directory entry changed")
        return current

    def atomic_write(self, root_fd: int, name: str, raw: bytes, *, token: str) -> None:
        temp_name = f".{name}.{token}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = -1
        created = False
        try:
            fd = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
            created = True
            info = os.fstat(fd)
            if not self.regular_file(info, maximum=len(raw)):
                raise StorageFailure("temporary storage file is unsafe")
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise StorageFailure("storage write failed")
                view = view[written:]
            os.fsync(fd)
            if not _close(fd):
                fd = -1
                raise StorageFailure("storage file close failed")
            fd = -1
            os.replace(temp_name, name, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            created = False
            os.fsync(root_fd)
        except OSError as exc:
            raise StorageFailure("atomic storage write failed") from exc
        finally:
            if fd >= 0:
                _close(fd)
            if created:
                with contextlib.suppress(OSError):
                    os.unlink(temp_name, dir_fd=root_fd)

    def hash_open_file(
        self,
        parent_fd: int,
        name: str,
        *,
        maximum: int,
    ) -> tuple[str, int, os.stat_result]:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
            before = os.fstat(fd)
        except OSError:
            raise StorageFailure("storage file is unsafe") from None
        try:
            if not self.regular_file(before, maximum=maximum):
                raise StorageFailure("storage file is unsafe")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(fd, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise StorageFailure("storage file exceeds budget")
                digest.update(chunk)
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise StorageFailure("storage file changed during hash")
            current = self.verify_file_entry(
                parent_fd,
                name,
                expected=after,
                maximum=maximum,
            )
            return digest.hexdigest(), total, current
        finally:
            if not _close(fd):
                raise StorageFailure("storage file close failed")


class CheckoutMutationLock:
    """One non-reentrant process mutex plus an OS-released file lock."""

    __slots__ = ("_adapter", "_entry", "_pid", "_root")

    def __init__(self, root: SafeRoot, name: str = "checkout-store.lock") -> None:
        if os.getpid() != _LOCK_REGISTRY_PID:
            # Fallback for runtimes without register_at_fork support.
            _reset_checkout_locks_after_fork()
        if _FORKED_WITH_ACTIVE_LOCK:
            raise StorageFailure("checkout locking is unavailable after an unsafe fork")
        if type(root) is not SafeRoot:
            raise TypeError("root must be a SafeRoot")
        if name != "checkout-store.lock":
            raise StorageFailure("checkout lock name is fixed")
        try:
            import fcntl
        except ImportError:
            raise StorageFailure("cross-process checkout locking is unavailable") from None
        key = (root.identity[0], root.identity[1], name)
        with _LOCK_REGISTRY_GUARD:
            entry = _LOCK_REGISTRY.get(key)
            if entry is None:
                entry = _ProcessLock(threading.Lock())
                _LOCK_REGISTRY[key] = entry
        self._root = root
        self._entry = entry
        self._pid = os.getpid()
        self._adapter = fcntl

    @contextlib.contextmanager
    def hold(self):
        if os.getpid() != self._pid:
            raise StorageFailure("fork-inherited checkout lock capability is invalid")
        thread_id = threading.get_ident()
        with _LOCK_REGISTRY_GUARD:
            if self._entry.owner_thread == thread_id:
                raise StorageFailure("checkout mutation lock is non-reentrant")
        self._entry.mutex.acquire()
        fd = -1
        acquired = False
        try:
            with _LOCK_REGISTRY_GUARD:
                self._entry.owner_thread = thread_id
            root_fd = self._root.open()
            try:
                flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
                fd = os.open("checkout-store.lock", flags, 0o600, dir_fd=root_fd)
                with _LOCK_REGISTRY_GUARD:
                    self._entry.active_fd = fd
                info = os.fstat(fd)
                if not self._root.regular_file(info, maximum=1):
                    raise StorageFailure("checkout lock entry is unsafe")
                self._adapter.flock(fd, self._adapter.LOCK_EX)
                acquired = True
                current = os.stat("checkout-store.lock", dir_fd=root_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise StorageFailure("checkout lock entry changed")
                yield
            finally:
                if not _close(root_fd):
                    raise StorageFailure("checkout lock root close failed")
        except OSError as exc:
            raise StorageFailure("checkout mutation lock failed") from exc
        finally:
            if fd >= 0:
                if acquired:
                    with contextlib.suppress(OSError):
                        self._adapter.flock(fd, self._adapter.LOCK_UN)
                _close(fd)
            with _LOCK_REGISTRY_GUARD:
                self._entry.active_fd = None
                self._entry.owner_thread = None
            self._entry.mutex.release()
