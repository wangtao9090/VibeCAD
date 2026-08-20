"""Secure, durable AgentApplication data layout."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from vibecad._file_compat import (
    WindowsPathCapability,
    capture_windows_path,
    ensure_private_directory,
    validate_windows_path,
)

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


def _windows_entry_is_alias(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except OSError:
        return True
    attributes = int(getattr(value, "st_file_attributes", 0))
    return stat.S_ISLNK(value.st_mode) or bool(attributes & 0x00000400)


def _ensure_windows_data_root(path: Path) -> WindowsPathCapability:
    """Create a missing private tail while refusing reparse ancestors."""

    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        if current == Path(current.anchor):
            raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT)
        missing.append(current)
        current = current.parent
    try:
        probe = Path(path.anchor)
        for part in current.parts[1:]:
            probe /= part
            value = os.lstat(probe)
            if not stat.S_ISDIR(value.st_mode) or _windows_entry_is_alias(probe):
                raise OSError("Windows data ancestor is unsafe")
        parent_capability: WindowsPathCapability | None = None
        for candidate in reversed(missing):
            parent_capability = ensure_private_directory(
                candidate,
                expected_parent=parent_capability,
            )
        capability = (
            parent_capability
            if missing
            else capture_windows_path(path, directory=True)
        )
        if capability is None:
            raise OSError("Windows data root identity is unavailable")
        validate_windows_path(capability, directory=True)
        return capability
    except (OSError, TypeError, ValueError):
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
    releases: Path
    visual_inputs: Path
    reconstruction_drafts: Path
    visual_reviews: Path
    _identities: tuple[tuple[int, int], ...]
    _windows_capabilities: tuple[WindowsPathCapability, ...] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def _fixed_paths(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.locks,
            self.tasks,
            self.projects,
            self.bootstrap,
            self.checkouts,
            self.artifacts,
            self.releases,
            self.visual_inputs,
            self.reconstruction_drafts,
            self.visual_reviews,
        )

    def identity_for(self, path: object) -> tuple[int, int]:
        """Return the directory identity captured by the descriptor-backed opener."""

        if type(path) is not type(Path("/")):
            raise ApplicationDataError(ApplicationDataErrorCode.INVALID_ROOT)
        paths = self._fixed_paths()
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
        if os.name == "nt" and sys.platform == "win32":
            capabilities = self._windows_capabilities
            fixed_paths = self._fixed_paths()
            if (
                type(capabilities) is not tuple
                or len(capabilities) != len(fixed_paths)
            ):
                raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT)
            try:
                index = fixed_paths.index(path)
                validate_windows_path(capabilities[index], directory=True)
            except (OSError, TypeError, ValueError):
                raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT) from None
            capability = capabilities[index]
            if (capability.volume, capability.file_id) != expected:
                raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT)
            return
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
        if sys.platform == "win32" and os.name == "nt":
            return cls._open_windows(root)
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
            "releases",
            "visual_inputs",
            "reconstruction_drafts",
            "visual_reviews",
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

    @classmethod
    def _open_windows(cls, root: object) -> ApplicationDataLayout:
        data_root = _path(root)
        root_capability = _ensure_windows_data_root(data_root)
        names = (
            "locks",
            "tasks",
            "projects",
            "bootstrap",
            "checkouts",
            "artifacts",
            "releases",
            "visual_inputs",
            "reconstruction_drafts",
            "visual_reviews",
        )
        capabilities: list[WindowsPathCapability] = [root_capability]
        try:
            for name in names:
                capabilities.append(
                    ensure_private_directory(
                        data_root / name,
                        expected_parent=root_capability,
                    )
                )
            for capability in capabilities:
                validate_windows_path(capability, directory=True)
            validate_windows_path(root_capability, directory=True)
        except (OSError, TypeError, ValueError):
            raise ApplicationDataError(ApplicationDataErrorCode.UNSAFE_ROOT) from None
        children = tuple(data_root / name for name in names)
        identities = tuple(
            (capability.volume, capability.file_id) for capability in capabilities
        )
        return cls(
            data_root,
            *children,
            identities,
            tuple(capabilities),
        )
