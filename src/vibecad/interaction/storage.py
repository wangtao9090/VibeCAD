"""Small fail-closed filesystem primitives for local interaction state."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os as _native_os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from vibecad import _file_compat as fcntl


class _StorageOS:
    """Module-local Win32 dir-fd adapter with native POSIX delegation."""

    __slots__ = ("_native", "__dict__")

    def __init__(self, native) -> None:
        self._native = native

    def __getattr__(self, name: str):
        if sys.platform == "win32" and name in {
            "O_CLOEXEC",
            "O_DIRECTORY",
            "O_NOFOLLOW",
        }:
            return 0
        return getattr(self._native, name)

    @staticmethod
    def _normalized(path: object) -> Path:
        rendered = _native_os.fspath(path)  # type: ignore[arg-type]
        if sys.platform == "win32":
            rendered = rendered.replace("/", "\\")
            if rendered.startswith("\\\\?\\UNC\\"):
                rendered = "\\\\" + rendered[8:]
            elif rendered.startswith("\\\\?\\"):
                rendered = rendered[4:]
        return Path(_native_os.path.abspath(rendered))

    @staticmethod
    def _child(directory_fd: int, name: object) -> Path:
        if type(directory_fd) is not int or directory_fd < 0 or type(name) not in {str, bytes}:
            raise OSError(errno.EINVAL, "invalid relative Windows filesystem operation")
        if type(name) is bytes:
            try:
                rendered = name.decode("ascii")
            except UnicodeError:
                raise OSError(errno.EINVAL, "invalid Windows child name") from None
        else:
            rendered = name
        if (
            not rendered
            or rendered in {".", ".."}
            or Path(rendered).is_absolute()
            or "/" in rendered
            or "\\" in rendered
            or Path(rendered).name != rendered
        ):
            raise OSError(errno.EINVAL, "invalid Windows child name")
        parent = fcntl.capture_windows_fd(directory_fd, directory=True)
        return Path(parent.path) / rendered

    @classmethod
    def _path(cls, path: object, directory_fd: int | None) -> Path:
        return cls._normalized(path) if directory_fd is None else cls._child(directory_fd, path)

    @staticmethod
    def _capture(path: Path):
        value = _native_os.lstat(fcntl.windows_extended_path(path))
        if stat.S_ISDIR(value.st_mode):
            capability = fcntl.capture_windows_path(path, directory=True)
        elif stat.S_ISREG(value.st_mode):
            capability = fcntl.capture_windows_path(path, directory=False)
        else:
            raise OSError(errno.EACCES, "unsafe Windows filesystem object")
        return value, capability

    def lstat(self, path: object, *, dir_fd: int | None = None):
        if sys.platform != "win32":
            if dir_fd is None:
                return self._native.lstat(path)
            return self._native.lstat(path, dir_fd=dir_fd)
        return self._capture(self._path(path, dir_fd))[0]

    def stat(
        self,
        path: object,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ):
        if sys.platform != "win32":
            return self._native.stat(
                path,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )
        return self._capture(self._path(path, dir_fd))[0]

    def fstat(self, descriptor: int):
        value = self._native.fstat(descriptor)
        if sys.platform == "win32":
            if stat.S_ISDIR(value.st_mode):
                fcntl.capture_windows_fd(descriptor, directory=True)
            elif stat.S_ISREG(value.st_mode):
                fcntl.capture_windows_fd(descriptor, directory=False)
            else:
                raise OSError(errno.EACCES, "unsafe Windows descriptor object")
        return value

    def open(
        self,
        path: object,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if sys.platform != "win32":
            if dir_fd is None:
                return self._native.open(path, flags, mode)
            return self._native.open(path, flags, mode, dir_fd=dir_fd)
        target = self._path(path, dir_fd)
        try:
            existing = _native_os.lstat(fcntl.windows_extended_path(target))
        except FileNotFoundError:
            existing = None
        write_flags = self._native.O_WRONLY | self._native.O_RDWR
        if existing is not None and stat.S_ISDIR(existing.st_mode):
            if flags & write_flags:
                raise OSError(errno.EISDIR, "directory is not writable through a CRT fd")
            return fcntl.open_windows_directory_fd(target)
        writable = bool(flags & write_flags)
        parent = fcntl.capture_windows_path(target.parent, directory=True)
        descriptor, _capability = fcntl.open_private_file(
            target,
            create=bool(flags & self._native.O_CREAT),
            read_write=writable,
            exclusive=bool(flags & self._native.O_EXCL),
            share_delete=not writable,
            expected_parent=parent,
        )
        try:
            if flags & self._native.O_TRUNC:
                if not writable:
                    raise OSError(errno.EINVAL, "read-only descriptor cannot truncate")
                self._native.ftruncate(descriptor, 0)
            if flags & self._native.O_APPEND:
                self._native.lseek(descriptor, 0, self._native.SEEK_END)
            return descriptor
        except BaseException:
            self._native.close(descriptor)
            raise

    def listdir(self, path: object = "."):
        if sys.platform != "win32" or type(path) is not int:
            return self._native.listdir(path)
        before = fcntl.capture_windows_fd(path, directory=True)
        names = self._native.listdir(fcntl.windows_extended_path(Path(before.path)))
        after = fcntl.capture_windows_fd(
            path,
            directory=True,
            generation_token=before.generation_token,
        )
        if before != after:
            raise OSError(errno.EACCES, "Windows directory identity changed")
        if any(
            type(name) is not str or not name or name in {".", ".."} or "/" in name or "\\" in name
            for name in names
        ):
            raise OSError(errno.EACCES, "invalid Windows directory entry")
        return names

    def mkdir(
        self,
        path: object,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if sys.platform != "win32":
            if dir_fd is None:
                self._native.mkdir(path, mode)
            else:
                self._native.mkdir(path, mode, dir_fd=dir_fd)
            return
        target = self._path(path, dir_fd)
        parent = fcntl.capture_windows_path(target.parent, directory=True)
        fcntl.ensure_private_directory(
            target,
            expected_parent=parent,
            exclusive=True,
        )

    def chmod(
        self,
        path: object,
        mode: int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if sys.platform != "win32":
            self._native.chmod(
                path,
                mode,
                dir_fd=dir_fd,
                follow_symlinks=follow_symlinks,
            )
            return
        target = self._path(path, dir_fd)
        fcntl.set_private_dacl(target)
        self._capture(target)

    def fchmod(self, descriptor: int, mode: int) -> None:
        if sys.platform != "win32":
            self._native.fchmod(descriptor, mode)
            return
        value = self._native.fstat(descriptor)
        fcntl.capture_windows_fd(descriptor, directory=stat.S_ISDIR(value.st_mode))

    def fsync(self, descriptor: int) -> None:
        if sys.platform != "win32":
            self._native.fsync(descriptor)
            return
        value = self._native.fstat(descriptor)
        if stat.S_ISDIR(value.st_mode):
            before = fcntl.capture_windows_fd(descriptor, directory=True)
            after = fcntl.capture_windows_fd(
                descriptor,
                directory=True,
                generation_token=before.generation_token,
            )
            if before != after:
                raise OSError(errno.EACCES, "Windows directory identity changed")
            return
        self._native.fsync(descriptor)

    def pread(self, descriptor: int, length: int, offset: int) -> bytes:
        return fcntl.pread(descriptor, length, offset)

    def unlink(self, path: object, *, dir_fd: int | None = None) -> None:
        if sys.platform != "win32":
            self._native.unlink(path, dir_fd=dir_fd)
            return
        target = self._path(path, dir_fd)
        parent = fcntl.capture_windows_path(target.parent, directory=True)
        expected = fcntl.capture_windows_path(target, directory=False)
        fcntl.delete_windows_file(target, parent=parent, expected=expected)

    def rmdir(self, path: object, *, dir_fd: int | None = None) -> None:
        if sys.platform != "win32":
            self._native.rmdir(path, dir_fd=dir_fd)
            return
        target = self._path(path, dir_fd)
        parent = fcntl.capture_windows_path(target.parent, directory=True)
        expected = fcntl.capture_windows_path(target, directory=True)
        fcntl.delete_windows_directory(target, parent=parent, expected=expected)

    @classmethod
    def _move_paths(
        cls,
        source: object,
        destination: object,
        source_fd: int | None,
        destination_fd: int | None,
    ) -> tuple[Path, Path]:
        return cls._path(source, source_fd), cls._path(destination, destination_fd)

    def replace(
        self,
        source: object,
        destination: object,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if sys.platform != "win32":
            self._native.replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            return
        source_path, destination_path = self._move_paths(
            source,
            destination,
            src_dir_fd,
            dst_dir_fd,
        )
        source_parent = fcntl.capture_windows_path(source_path.parent, directory=True)
        destination_parent = fcntl.capture_windows_path(destination_path.parent, directory=True)
        expected_source = fcntl.capture_windows_path(source_path, directory=False)
        try:
            expected_destination = fcntl.capture_windows_path(destination_path, directory=False)
        except FileNotFoundError:
            expected_destination = None
        fcntl.replace_windows_file(
            source_path,
            destination_path,
            source_parent=source_parent,
            destination_parent=destination_parent,
            expected_source=expected_source,
            expected_destination=expected_destination,
        )

    def rename(
        self,
        source: object,
        destination: object,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if sys.platform != "win32":
            self._native.rename(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            return
        source_path, destination_path = self._move_paths(
            source,
            destination,
            src_dir_fd,
            dst_dir_fd,
        )
        source_parent = fcntl.capture_windows_path(source_path.parent, directory=True)
        destination_parent = fcntl.capture_windows_path(destination_path.parent, directory=True)
        source_info = _native_os.lstat(fcntl.windows_extended_path(source_path))
        if stat.S_ISDIR(source_info.st_mode):
            expected_source = fcntl.capture_windows_path(source_path, directory=True)
            fcntl.rename_windows_directory(
                source_path,
                destination_path,
                source_parent=source_parent,
                destination_parent=destination_parent,
                expected_source=expected_source,
            )
        elif stat.S_ISREG(source_info.st_mode):
            expected_source = fcntl.capture_windows_path(source_path, directory=False)
            fcntl.rename_windows_file(
                source_path,
                destination_path,
                source_parent=source_parent,
                destination_parent=destination_parent,
                expected_source=expected_source,
            )
        else:
            raise OSError(errno.EACCES, "unsafe Windows rename source")


os = _StorageOS(_native_os)

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


def _identity_epoch(info: os.stat_result) -> int:
    """Use NTFS birth time; ChangeTime can advance merely by closing a handle."""

    return int(info.st_birthtime_ns) if sys.platform == "win32" else info.st_ctime_ns


def _require_capabilities() -> None:
    if sys.platform == "win32" and os.name == "nt":
        return
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(type(getattr(os, name, None)) is not int for name in required):
        raise StorageFailure("required local storage capability is unavailable")
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise StorageFailure("required dir-fd capability is unavailable")


class SafeRoot:
    """An identity-pinned, owned 0700 local directory."""

    __slots__ = ("_capability", "identity", "path", "uid")

    def __init__(self, path: str | os.PathLike[str]) -> None:
        _require_capabilities()
        candidate = Path(path)
        if not candidate.is_absolute():
            raise StorageFailure("storage root must be absolute")
        if sys.platform == "win32" and os.name == "nt":
            try:
                absolute = Path(os.path.abspath(candidate))
                if absolute != candidate:
                    raise OSError("storage root is not normalized")
                capability = fcntl.capture_windows_path(absolute, directory=True)
                fcntl.validate_windows_path(capability, directory=True)
            except (OSError, TypeError, ValueError):
                raise StorageFailure("storage root is unavailable") from None
            self.path = absolute
            # Windows has no trustworthy POSIX uid projection.  The owner SID
            # and protected DACL live in the pinned native capability instead.
            self.uid = None
            self._capability = capability
            self.identity = (capability.volume, capability.file_id)
            return
        try:
            canonical = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise StorageFailure("storage root is unavailable") from None
        if canonical != candidate:
            raise StorageFailure("storage root must not traverse links")
        self.path = candidate
        self.uid = os.geteuid()
        self._capability = None
        fd = self.open()
        try:
            info = os.fstat(fd)
            self.identity = (info.st_dev, info.st_ino)
        finally:
            if not _close(fd):
                raise StorageFailure("storage root close failed")

    def open(self) -> int:
        if sys.platform == "win32" and os.name == "nt":
            fd = -1
            try:
                fd = fcntl.open_windows_directory_fd(self.path)
                current = fcntl.capture_windows_fd(
                    fd,
                    directory=True,
                    generation_token=self._capability.generation_token,
                )
                if current != self._capability:
                    raise OSError("storage root identity changed")
                return fd
            except (OSError, TypeError, ValueError):
                if fd >= 0:
                    _close(fd)
                raise StorageFailure("storage root is unsafe") from None
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
        if sys.platform == "win32" and os.name == "nt":
            fd = -1
            try:
                if (
                    type(name) is not str
                    or not name
                    or name in {".", ".."}
                    or "/" in name
                    or "\\" in name
                ):
                    raise OSError("invalid Windows directory name")
                parent = fcntl.capture_windows_fd(parent_fd, directory=True)
                path = Path(parent.path) / name
                fd = fcntl.open_windows_directory_fd(path)
                capability = fcntl.capture_windows_fd(fd, directory=True)
                if capability.volume != self.identity[0] or (
                    expected_identity is not None
                    and expected_identity != (capability.volume, capability.file_id)
                ):
                    raise OSError("storage directory identity changed")
                return fd, os.fstat(fd)
            except (OSError, TypeError, ValueError):
                if fd >= 0:
                    _close(fd)
                raise StorageFailure("storage directory is unsafe") from None
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

    def entry_exists(self, parent_fd: int, name: str) -> bool:
        """Observe name presence without treating an unsafe object as absence."""

        if sys.platform == "win32" and os.name == "nt":
            try:
                parent = fcntl.capture_windows_fd(parent_fd, directory=True)
                path = os._child(parent_fd, name)
                exists = _native_os.path.lexists(fcntl.windows_extended_path(path))
                current = fcntl.capture_windows_fd(
                    parent_fd,
                    directory=True,
                    generation_token=parent.generation_token,
                )
                if current != parent:
                    raise OSError("storage directory identity changed")
                return exists
            except (OSError, TypeError, ValueError):
                raise StorageFailure("storage entry presence is unsafe") from None
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError:
            raise StorageFailure("storage entry presence failed") from None
        return True

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
        if sys.platform == "win32" and os.name == "nt":
            return (
                stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 <= info.st_size <= maximum
            )
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
            _identity_epoch(expected),
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
            _identity_epoch(current),
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
            _identity_epoch(expected),
        )
        current_binding = (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_uid,
            current.st_gid,
            current.st_nlink,
            current.st_mtime_ns,
            _identity_epoch(current),
        )
        if sys.platform == "win32":
            unsafe = (
                not stat.S_ISDIR(current.st_mode)
                or current.st_dev != self.identity[0]
                # Child creation legitimately advances directory timestamps on
                # NTFS.  File ID/type plus the adapter's owner/protected-DACL
                # validation are the stable directory authority.
                or current_binding[:5] != expected_binding[:5]
            )
        else:
            unsafe = (
                not stat.S_ISDIR(current.st_mode)
                or current.st_uid != self.uid
                or stat.S_IMODE(current.st_mode) != 0o700
                or current.st_dev != self.identity[0]
                or current_binding != expected_binding
            )
        if unsafe:
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
        if sys.platform == "win32" and os.name == "nt":
            lock_adapter = fcntl
        else:
            try:
                import fcntl as lock_adapter
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
        self._adapter = lock_adapter

    @contextlib.contextmanager
    def hold(self):
        if os.getpid() != self._pid:
            raise StorageFailure("fork-inherited checkout lock capability is invalid")
        thread_id = threading.get_ident()
        with _LOCK_REGISTRY_GUARD:
            if self._entry.owner_thread == thread_id:
                raise StorageFailure("checkout mutation lock is non-reentrant")
        self._entry.mutex.acquire()
        if sys.platform == "win32" and os.name == "nt":
            fd = -1
            acquired = False
            try:
                with _LOCK_REGISTRY_GUARD:
                    self._entry.owner_thread = thread_id
                fd, capability = fcntl.open_private_file(
                    self._root.path / "checkout-store.lock",
                    create=True,
                    read_write=True,
                    expected_parent=self._root._capability,
                )
                with _LOCK_REGISTRY_GUARD:
                    self._entry.active_fd = fd
                self._adapter.flock(fd, self._adapter.LOCK_EX)
                acquired = True
                current = fcntl.capture_windows_fd(
                    fd,
                    directory=False,
                    generation_token=capability.generation_token,
                )
                if current != capability:
                    raise StorageFailure("checkout lock entry changed")
                yield
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
            return
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
