"""Secure, durable AgentApplication data layout."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

__all__ = (
    "ApplicationDataError",
    "ApplicationDataErrorCode",
    "ApplicationDataLayout",
)


class ApplicationDataErrorCode(StrEnum):
    INVALID_ROOT = "invalid_root"
    UNSAFE_ROOT = "unsafe_root"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    IO_ERROR = "io_error"


_MESSAGES = {
    ApplicationDataErrorCode.INVALID_ROOT: "The application data root is invalid.",
    ApplicationDataErrorCode.UNSAFE_ROOT: "The application data root is unsafe.",
    ApplicationDataErrorCode.UNSUPPORTED_PLATFORM: (
        "The application data platform is not supported."
    ),
    ApplicationDataErrorCode.IO_ERROR: "The application data layout could not be created.",
}


class ApplicationDataError(ValueError):
    __slots__ = ("code", "message")

    def __init__(self, code: ApplicationDataErrorCode) -> None:
        if type(code) is not ApplicationDataErrorCode:
            raise TypeError("code must be an ApplicationDataErrorCode")
        self.code = code
        self.message = _MESSAGES[code]
        super().__init__(self.message)


def _path(value: object) -> Path:
    if type(value) is str:
        result = Path(value)
    elif type(value) is type(Path("/")):
        result = value
    else:
        raise ApplicationDataError(ApplicationDataErrorCode.INVALID_ROOT)
    if not result.is_absolute() or ".." in result.parts or result == Path(result.anchor):
        raise ApplicationDataError(ApplicationDataErrorCode.INVALID_ROOT)
    return result


def _safe_directory(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) != 0o700:
        return False
    try:
        return value.st_uid == os.geteuid()
    except AttributeError:
        return False


def _directory_flags() -> int:
    try:
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    except AttributeError:
        raise ApplicationDataError(ApplicationDataErrorCode.UNSUPPORTED_PLATFORM) from None


def _open_absolute_directory(
    path: Path,
    *,
    create: bool,
    final_private: bool,
) -> tuple[int, os.stat_result]:
    fd = None
    try:
        fd = os.open(path.anchor, _directory_flags())
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            try:
                next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    # Another opener may have created the fixed component after
                    # our no-follow open observed it missing. Re-open and validate
                    # the winner instead of treating this benign race as unsafe.
                    pass
                next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
            value = os.fstat(fd)
            if not stat.S_ISDIR(value.st_mode):
                raise OSError
            if (
                final
                and final_private
                and not (stat.S_IMODE(value.st_mode) == 0o700 and value.st_uid == os.geteuid())
            ):
                raise PermissionError
        if fd is None:
            raise OSError
        return fd, os.fstat(fd)
    except PermissionError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT) from None
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT) from None


def _create_private(path: Path) -> None:
    fd, _ = _open_absolute_directory(path, create=True, final_private=True)
    try:
        os.close(fd)
    except OSError:
        raise ApplicationDataError(ApplicationDataErrorCode.IO_ERROR) from None


def _open_private_child(
    root_fd: int,
    root_stat: os.stat_result,
    name: str,
) -> tuple[int, os.stat_result]:
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        fd = os.open(name, _directory_flags(), dir_fd=root_fd)
        value = os.fstat(fd)
        if not (
            stat.S_ISDIR(value.st_mode)
            and stat.S_IMODE(value.st_mode) == 0o700
            and value.st_uid == os.geteuid()
            and value.st_dev == root_stat.st_dev
        ):
            os.close(fd)
            raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT)
        return fd, value
    except ApplicationDataError:
        raise
    except OSError:
        raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT) from None


@dataclass(frozen=True, slots=True)
class ApplicationDataLayout:
    root: Path
    locks: Path
    tasks: Path
    projects: Path
    bootstrap: Path
    checkouts: Path
    artifacts: Path
    _identities: tuple[tuple[int, int], ...]

    def identity_for(self, path: object) -> tuple[int, int]:
        """Return the directory identity captured by the descriptor-backed opener."""

        if type(path) is not type(Path("/")):
            raise ApplicationDataError(ApplicationDataErrorCode.INVALID_ROOT)
        paths = (
            self.root,
            self.locks,
            self.tasks,
            self.projects,
            self.bootstrap,
            self.checkouts,
            self.artifacts,
        )
        if (
            type(self._identities) is not tuple
            or len(self._identities) != len(paths)
            or any(
                type(identity) is not tuple
                or len(identity) != 2
                or not all(type(item) is int for item in identity)
                for identity in self._identities
            )
        ):
            raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT)
        for index, candidate in enumerate(paths):
            if path == candidate:
                return self._identities[index]
        raise ApplicationDataError(ApplicationDataErrorCode.INVALID_ROOT)

    def require_current(self, path: object) -> None:
        """Fail unless a fixed layout path still names its captured directory."""

        expected = self.identity_for(path)
        descriptor = None
        try:
            descriptor, value = _open_absolute_directory(
                path,
                create=False,
                final_private=True,
            )
            if (value.st_dev, value.st_ino) != expected:
                raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT)
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    raise ApplicationDataError(ApplicationDataErrorCode.IO_ERROR) from None

    @classmethod
    def open(cls, root: object) -> ApplicationDataLayout:
        if sys.platform != "darwin":
            raise ApplicationDataError(ApplicationDataErrorCode.UNSUPPORTED_PLATFORM)
        data_root = _path(root)
        _create_private(data_root)
        root_fd, root_stat = _open_absolute_directory(
            data_root,
            create=False,
            final_private=True,
        )
        names = (
            "locks",
            "tasks",
            "projects",
            "bootstrap",
            "checkouts",
            "artifacts",
        )
        child_fds = []
        child_identities: list[tuple[int, int]] = []
        try:
            for name in names:
                child_fd, child_stat = _open_private_child(root_fd, root_stat, name)
                child_fds.append(child_fd)
                child_identities.append((child_stat.st_dev, child_stat.st_ino))
            os.fsync(root_fd)
            check_fd, check_stat = _open_absolute_directory(
                data_root,
                create=False,
                final_private=True,
            )
            try:
                if (check_stat.st_dev, check_stat.st_ino) != (
                    root_stat.st_dev,
                    root_stat.st_ino,
                ):
                    raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT)
            finally:
                os.close(check_fd)
        except ApplicationDataError:
            raise
        except OSError:
            raise ApplicationDataError(ApplicationDataErrorCode.IO_ERROR) from None
        finally:
            for child_fd in child_fds:
                try:
                    os.close(child_fd)
                except OSError:
                    pass
            try:
                os.close(root_fd)
            except OSError:
                pass
        children = tuple(data_root / name for name in names)
        identities = ((root_stat.st_dev, root_stat.st_ino), *child_identities)
        return cls(data_root, *children, identities)
