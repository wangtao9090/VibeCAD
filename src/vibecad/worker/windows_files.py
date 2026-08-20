"""Windows path-capability operations used by the Worker protocol.

This module is imported only from explicit ``sys.platform == "win32"``
branches.  The POSIX Worker continues to use directory descriptors and
``SCM_RIGHTS``; Windows instead reopens an absolute path and proves that the
volume, File ID, owner and protected DACL still match the wire capability.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vibecad import _file_compat
from vibecad._file_compat import WindowsPathCapability


@dataclass(frozen=True, slots=True, eq=False)
class WindowsEntryIdentity:
    """Stable identity and mutation evidence for one protected regular file."""

    capability: WindowsPathCapability
    size: int
    mtime_ns: int
    ctime_ns: int

    def _comparison_key(self) -> tuple[object, ...]:
        capability = self.capability
        return (
            os.path.normcase(capability.path),
            capability.volume,
            capability.file_id,
            capability.owner_sid,
            capability.security_sha256,
            self.size,
            self.mtime_ns,
            self.ctime_ns,
        )

    def __eq__(self, other: object) -> bool:
        return (
            type(other) is WindowsEntryIdentity
            and self._comparison_key() == other._comparison_key()
        )

    def __hash__(self) -> int:
        return hash(self._comparison_key())


def capability_from_mapping(value: object) -> WindowsPathCapability:
    """Decode a canonical wire mapping and reject it outside Windows."""

    if os.name != "nt":
        raise OSError("Windows path capabilities are unavailable")
    return WindowsPathCapability.from_mapping(value)


def validate_directory(capability: WindowsPathCapability) -> Path:
    """Validate and return the exact directory named by ``capability``."""

    return _file_compat.validate_windows_path(capability, directory=True)


def _entry_path(directory: Path, name: str) -> Path:
    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or Path(name).name != name
    ):
        raise OSError("unsafe Windows directory entry name")
    return directory / name


def _open_verified_file(
    path: Path,
    expected: WindowsPathCapability,
) -> tuple[int, os.stat_result]:
    descriptor = -1
    try:
        descriptor = os.open(
            _file_compat.windows_extended_path(path),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        os.set_inheritable(descriptor, False)
        if os.get_inheritable(descriptor):
            raise OSError("Windows capability descriptor is inheritable")
        opened = _file_compat.capture_windows_fd(
            descriptor,
            directory=False,
            generation_token=expected.generation_token,
        )
        if opened != expected:
            raise OSError("Windows entry identity changed while opening")
        return descriptor, os.fstat(descriptor)
    except BaseException:
        if descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise


def capture_entry(
    directory_capability: WindowsPathCapability,
    name: str,
    *,
    generation_token: str | None = None,
) -> WindowsEntryIdentity:
    """Capture one non-reparse, private file beneath a validated directory."""

    directory = validate_directory(directory_capability)
    path = _entry_path(directory, name)
    expected = _file_compat.capture_windows_path(
        path,
        directory=False,
        generation_token=generation_token,
    )
    descriptor = -1
    try:
        descriptor, current = _open_verified_file(path, expected)
        result = WindowsEntryIdentity(
            capability=expected,
            size=current.st_size,
            mtime_ns=current.st_mtime_ns,
            ctime_ns=current.st_ctime_ns,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _file_compat.validate_windows_path(expected, directory=False)
    validate_directory(directory_capability)
    return result


def validate_entry(identity: WindowsEntryIdentity) -> Path:
    """Revalidate a captured entry, including content-mutation metadata."""

    if type(identity) is not WindowsEntryIdentity:
        raise TypeError("identity must be a WindowsEntryIdentity")
    path = _file_compat.validate_windows_path(identity.capability, directory=False)
    descriptor = -1
    try:
        descriptor, current = _open_verified_file(path, identity.capability)
        if (
            current.st_size != identity.size
            or current.st_mtime_ns != identity.mtime_ns
            or current.st_ctime_ns != identity.ctime_ns
        ):
            raise OSError("Windows entry content identity changed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _file_compat.validate_windows_path(identity.capability, directory=False)
    return path


def capture_entries(
    directory_capability: WindowsPathCapability,
    *,
    maximum_entries: int,
) -> tuple[tuple[str, WindowsEntryIdentity], ...]:
    """Capture a complete, stable directory listing and every file identity."""

    if type(maximum_entries) is not int or maximum_entries < 0:
        raise ValueError("invalid maximum_entries")
    directory = validate_directory(directory_capability)
    names = tuple(sorted(os.listdir(_file_compat.windows_extended_path(directory))))
    if (
        len(names) > maximum_entries
        or len(names) != len(set(names))
        or any(type(name) is not str for name in names)
    ):
        raise OSError("unsafe Windows directory listing")
    entries = tuple((name, capture_entry(directory_capability, name)) for name in names)
    if tuple(sorted(os.listdir(_file_compat.windows_extended_path(directory)))) != names:
        raise OSError("Windows directory listing changed")
    for _name, identity in entries:
        validate_entry(identity)
    validate_directory(directory_capability)
    return entries


def hash_entry(
    directory_capability: WindowsPathCapability,
    name: str,
    *,
    maximum_bytes: int,
    expected: WindowsEntryIdentity | None = None,
) -> tuple[str, int, WindowsEntryIdentity]:
    """Hash an entry through a handle that is matched to its path capability."""

    if type(maximum_bytes) is not int or maximum_bytes < 0:
        raise ValueError("invalid maximum_bytes")
    directory = validate_directory(directory_capability)
    path = _entry_path(directory, name)
    identity = capture_entry(directory_capability, name) if expected is None else expected
    if type(identity) is not WindowsEntryIdentity:
        raise TypeError("expected must be a WindowsEntryIdentity")
    if Path(identity.capability.path) != path:
        raise OSError("Windows entry is outside the capability directory")
    descriptor = -1
    try:
        descriptor, before = _open_verified_file(path, identity.capability)
        if (
            before.st_size != identity.size
            or before.st_mtime_ns != identity.mtime_ns
            or before.st_ctime_ns != identity.ctime_ns
            or before.st_size < 0
            or before.st_size > maximum_bytes
        ):
            raise OSError("Windows entry identity changed before hashing")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, min(1_048_576, maximum_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum_bytes:
                raise OSError("Windows entry exceeds its byte budget")
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
            or size != before.st_size
        ):
            raise OSError("Windows entry changed while hashing")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    validate_entry(identity)
    validate_directory(directory_capability)
    return digest.hexdigest(), size, identity


def read_entry(
    directory_capability: WindowsPathCapability,
    name: str,
    *,
    maximum_bytes: int,
    expected: WindowsEntryIdentity,
) -> bytes:
    """Read an expected entry with before/after identity validation."""

    digest, size, identity = hash_entry(
        directory_capability,
        name,
        maximum_bytes=maximum_bytes,
        expected=expected,
    )
    del digest
    path = validate_entry(identity)
    descriptor = -1
    try:
        descriptor, before = _open_verified_file(path, identity.capability)
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        if (
            len(payload) != size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise OSError("Windows entry changed while reading")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    validate_entry(identity)
    validate_directory(directory_capability)
    return payload


@contextmanager
def directory_cwd(capability: WindowsPathCapability) -> Iterator[None]:
    """Pin a capability directory without relying on the Win32 current directory.

    ``SetCurrentDirectoryW`` remains bounded by legacy path rules even when file
    APIs accept a verbatim path.  Worker call sites therefore pass verbatim
    absolute entry paths to FreeCAD while this delete-denying HANDLE keeps the
    containing directory identity stable across the call.
    """

    directory = validate_directory(capability)
    handle = _file_compat.open_windows_directory_handle(
        directory,
        inheritable=False,
        deny_delete=True,
    )
    try:
        _file_compat.validate_windows_handle_path(
            handle,
            directory,
            directory=True,
            expected=capability,
        )
        yield
        _file_compat.validate_windows_handle_path(
            handle,
            directory,
            directory=True,
            expected=capability,
        )
    finally:
        _file_compat.close_windows_handle(handle)


@contextmanager
def cad_directory_cwd(capability: WindowsPathCapability) -> Iterator[None]:
    """Enter one private short CAD bridge for APIs requiring a relative path."""

    directory = validate_directory(capability)
    if len(os.fspath(directory)) >= 240:
        raise OSError("Windows CAD bridge exceeds the legacy path budget")
    original = Path.cwd()
    handle = _file_compat.open_windows_directory_handle(
        directory,
        inheritable=False,
        deny_delete=True,
    )
    changed = False
    try:
        _file_compat.validate_windows_handle_path(
            handle,
            directory,
            directory=True,
            expected=capability,
        )
        os.chdir(directory)
        changed = True
        yield
        _file_compat.validate_windows_handle_path(
            handle,
            directory,
            directory=True,
            expected=capability,
        )
    finally:
        if changed:
            os.chdir(original)
        _file_compat.close_windows_handle(handle)


def entry_path(capability: WindowsPathCapability, name: str) -> Path:
    """Return one validated child as an internal verbatim absolute path."""

    directory = validate_directory(capability)
    path = _entry_path(directory, name)
    _file_compat.capture_windows_path(path, directory=False)
    validate_directory(capability)
    return Path(_file_compat.windows_extended_path(path))


@contextmanager
def cad_staging_directory(
    *,
    parent_capability: WindowsPathCapability | None = None,
) -> Iterator[WindowsPathCapability]:
    """Create one short private CAD bridge beneath a pinned private parent.

    Worker callers keep using their isolated ``HOME``.  In-process product
    execution can instead provide the already-protected ``FREECAD_USER_TEMP``
    capability, avoiding both a global environment mutation and the original
    long revision path.
    """

    if parent_capability is None:
        home = Path(os.path.abspath(os.environ["HOME"]))
        parent = _file_compat.capture_windows_path(home, directory=True)
    else:
        if type(parent_capability) is not WindowsPathCapability:
            raise TypeError("parent_capability must be a WindowsPathCapability")
        parent = parent_capability
        home = validate_directory(parent)
    capability: WindowsPathCapability | None = None
    for _ in range(16):
        directory = home / f"cad-bridge-{secrets.token_hex(16)}"
        if len(os.fspath(directory)) >= 220:
            raise OSError("Windows CAD bridge exceeds the legacy path budget")
        try:
            capability = _file_compat.ensure_private_directory(
                directory,
                expected_parent=parent,
                exclusive=True,
            )
        except FileExistsError:
            continue
        break
    if capability is None:
        raise OSError("Windows CAD bridge name budget exhausted")
    try:
        handle = _file_compat.open_windows_directory_handle(
            directory,
            inheritable=False,
            deny_delete=True,
        )
    except BaseException:
        try:
            _file_compat.delete_windows_directory_capability(capability)
        except BaseException:
            pass
        raise
    try:
        _file_compat.validate_windows_handle_path(
            handle,
            directory,
            directory=True,
            expected=capability,
        )
        yield capability
    finally:
        try:
            _file_compat.validate_windows_handle_path(
                handle,
                directory,
                directory=True,
                expected=capability,
            )
            directory = validate_directory(capability)
            names = tuple(os.listdir(_file_compat.windows_extended_path(directory)))
            if len(names) > 8 or len(names) != len(set(names)):
                raise OSError("Windows CAD bridge cleanup inventory is unsafe")
            for name in names:
                child = _entry_path(directory, name)
                _file_compat.set_private_dacl(child)
                expected = _file_compat.capture_windows_path(child, directory=False)
                _file_compat.delete_windows_file(
                    child,
                    parent=capability,
                    expected=expected,
                )
            _file_compat.validate_windows_handle_path(
                handle,
                directory,
                directory=True,
                expected=capability,
            )
        finally:
            _file_compat.close_windows_handle(handle)
        _file_compat.delete_windows_directory(
            directory,
            parent=parent,
            expected=capability,
        )


def cad_output_path(capability: WindowsPathCapability, name: str) -> Path:
    """Return a short, absent output path inside one pinned CAD bridge."""

    directory = validate_directory(capability)
    path = _entry_path(directory, name)
    try:
        os.lstat(_file_compat.windows_extended_path(path))
    except FileNotFoundError:
        return path
    raise OSError("Windows CAD bridge output already exists")


def reserve_cad_output(
    capability: WindowsPathCapability,
    name: str,
) -> WindowsEntryIdentity:
    """Create one empty private placeholder for a FreeCAD export API."""

    directory = validate_directory(capability)
    path = _entry_path(directory, name)
    descriptor = -1
    expected: WindowsPathCapability | None = None
    try:
        descriptor, expected = _file_compat.open_private_file(
            path,
            create=True,
            read_write=True,
            exclusive=True,
            expected_parent=capability,
        )
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    assert expected is not None
    result = capture_entry(
        capability,
        name,
        generation_token=expected.generation_token,
    )
    validate_directory(capability)
    return result


def capture_cad_output(
    capability: WindowsPathCapability,
    name: str,
) -> WindowsEntryIdentity:
    """Seal a FreeCAD-created bridge output with VibeCAD's private DACL."""

    directory = validate_directory(capability)
    path = _entry_path(directory, name)
    _file_compat.set_private_dacl(path)
    result = capture_entry(capability, name)
    validate_directory(capability)
    return result


def publish_cad_output(
    source_directory: WindowsPathCapability,
    source: WindowsEntryIdentity,
    destination_directory: WindowsPathCapability,
    destination_name: str,
    *,
    expected_destination: WindowsEntryIdentity,
) -> WindowsEntryIdentity:
    """Copy one short CAD output and atomically replace an exact long-path entry."""

    if (
        type(source) is not WindowsEntryIdentity
        or type(expected_destination) is not WindowsEntryIdentity
    ):
        raise TypeError("CAD bridge identities are invalid")
    source_path = validate_entry(source)
    source_parent = validate_directory(source_directory)
    destination_parent = validate_directory(destination_directory)
    if source_path.parent != source_parent:
        raise OSError("CAD bridge source escaped its directory")
    destination = _entry_path(destination_parent, destination_name)
    if Path(expected_destination.capability.path) != destination:
        raise OSError("CAD bridge destination identity is mismatched")
    staging = destination_parent / f".worker-publish-{secrets.token_hex(16)}.tmp"
    destination_fd = -1
    source_fd = -1
    staging_capability: WindowsPathCapability | None = None
    try:
        destination_fd, staging_capability = _file_compat.open_private_file(
            staging,
            create=True,
            read_write=True,
            exclusive=True,
            expected_parent=destination_directory,
        )
        source_fd, before = _open_verified_file(source_path, source.capability)
        if (
            before.st_size != source.size
            or before.st_mtime_ns != source.mtime_ns
            or before.st_ctime_ns != source.ctime_ns
            or before.st_size < 0
        ):
            raise OSError("CAD bridge source changed before copying")
        copied = 0
        while True:
            chunk = os.read(source_fd, 1_048_576)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short CAD bridge write")
                copied += written
                view = view[written:]
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        if (
            copied != source.size
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_ctime_ns != before.st_ctime_ns
        ):
            raise OSError("CAD bridge source changed while copying")
        os.close(source_fd)
        source_fd = -1
        os.close(destination_fd)
        destination_fd = -1
        validate_entry(source)
        assert staging_capability is not None
        moved = _file_compat.replace_windows_file(
            staging,
            destination,
            source_parent=destination_directory,
            expected_source=staging_capability,
            expected_destination=expected_destination.capability,
        )
        result = capture_entry(
            destination_directory,
            destination_name,
            generation_token=moved.generation_token,
        )
        validate_directory(source_directory)
        validate_directory(destination_directory)
        return result
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        if destination_fd >= 0:
            os.close(destination_fd)
        if staging_capability is not None:
            try:
                _file_compat.delete_windows_file(
                    staging,
                    parent=destination_directory,
                    expected=staging_capability,
                )
            except FileNotFoundError:
                pass


def stage_cad_input(
    source_directory: WindowsPathCapability,
    source: WindowsEntryIdentity,
    destination_directory: WindowsPathCapability,
    name: str,
) -> WindowsEntryIdentity:
    """Copy one exact protected input into a private short CAD bridge."""

    placeholder = reserve_cad_output(destination_directory, name)
    return publish_cad_output(
        source_directory,
        source,
        destination_directory,
        name,
        expected_destination=placeholder,
    )


def stable_identity(identity: WindowsEntryIdentity) -> tuple[object, ...]:
    """Identity fields that remain stable across an authorized content rewrite."""

    if type(identity) is not WindowsEntryIdentity:
        raise TypeError("identity must be a WindowsEntryIdentity")
    capability = identity.capability
    return (
        capability.volume,
        capability.file_id,
        capability.owner_sid,
        capability.security_sha256,
    )
