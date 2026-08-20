"""Private per-document workspace for FreeCAD included-file properties.

FreeCAD's ``App::PropertyFileIncluded`` copies a source file into
``Document.TransientDir`` and serializes that retained copy into FCStd.  Some
managed FreeCAD builds leave ``TransientDir`` empty while others create an
empty runtime-owned cache directory with the document.  Every document must
therefore be bound either to that native directory inside an authenticated
private process root, or to one host-owned private fallback directory,
*before* a document is loaded or an included-file property is assigned.

This module owns only those temporary document directories.  It never accepts
a graph/model pathname, never deletes project or artifact-store inputs, and is
not a public persistence surface.  FCStd remains the durable container; the
workspace is the bounded live-document extraction area.
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad._file_compat import (
    WindowsPathCapability,
    capture_windows_path,
    ensure_private_directory,
    set_private_dacl,
    validate_windows_path,
)

_MAX_CLEANUP_ENTRIES = 4096
_MAX_CLEANUP_DEPTH = 16
_MAX_ERROR_PATH_BYTES = 384
_WINDOWS = sys.platform == "win32" and os.name == "nt"


class DocumentAssetWorkspaceErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    PRECONDITION_FAILED = "precondition_failed"
    ATTACH_FAILED = "attach_failed"
    CLEANUP_FAILED = "cleanup_failed"


class DocumentAssetWorkspaceError(ValueError):
    """Bounded stable failure at the host-owned document workspace seam."""

    def __init__(
        self,
        code: DocumentAssetWorkspaceErrorCode,
        path: str = "/",
    ) -> None:
        if type(code) is not DocumentAssetWorkspaceErrorCode:
            raise TypeError("code must be a DocumentAssetWorkspaceErrorCode")
        try:
            path_size = len(path.encode("utf-8")) if type(path) is str else 0
        except UnicodeError:
            path_size = _MAX_ERROR_PATH_BYTES + 1
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isprintable()
            or len(path.splitlines()) != 1
            or path_size > _MAX_ERROR_PATH_BYTES
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"document asset workspace error ({code.value}) at {path}")


def _fail(code: DocumentAssetWorkspaceErrorCode, path: str) -> None:
    raise DocumentAssetWorkspaceError(code, path)


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    path: Path
    device: int
    inode: int
    windows_capability: WindowsPathCapability | None = None


class _WorkspaceOwnership(StrEnum):
    VIBECAD_OWNED = "vibecad_owned"
    FREECAD_NATIVE_BORROWED = "freecad_native_borrowed"


@dataclass(frozen=True, slots=True)
class _DocumentWorkspace:
    document: object
    directory: _DirectoryIdentity
    ownership: _WorkspaceOwnership
    native_root: _DirectoryIdentity | None = None


def _private_directory_identity(
    value: object,
    path: str,
    *,
    error_code: DocumentAssetWorkspaceErrorCode,
) -> _DirectoryIdentity:
    if not isinstance(value, Path) or not value.is_absolute():
        _fail(DocumentAssetWorkspaceErrorCode.INVALID_INPUT, path)
    if _WINDOWS:
        absolute = Path(os.path.abspath(value))
        if absolute != value:
            _fail(error_code, path)
        try:
            capability = capture_windows_path(absolute, directory=True)
        except (OSError, TypeError, ValueError, RuntimeError):
            _fail(error_code, path)
        return _DirectoryIdentity(
            absolute,
            capability.volume,
            capability.file_id,
            capability,
        )
    try:
        info = value.lstat()
    except (OSError, ValueError, RuntimeError):
        _fail(error_code, path)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        _fail(error_code, path)
    return _DirectoryIdentity(value, info.st_dev, info.st_ino)


def _same_directory(identity: _DirectoryIdentity, path: str) -> bool:
    if _WINDOWS:
        capability = identity.windows_capability
        if capability is None:
            return False
        try:
            actual = Path(os.path.abspath(path))
            expected = Path(capability.path)
            if os.path.normcase(os.fspath(actual)) != os.path.normcase(
                os.fspath(expected)
            ):
                return False
            return validate_windows_path(capability, directory=True) == expected
        except (OSError, TypeError, ValueError, RuntimeError):
            return False
    try:
        actual = Path(path)
        info = actual.lstat()
    except (OSError, TypeError, ValueError, RuntimeError):
        return False
    return (
        actual == identity.path
        and not stat.S_ISLNK(info.st_mode)
        and stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_dev == identity.device
        and info.st_ino == identity.inode
    )


def _same_owned_directory(identity: _DirectoryIdentity, path: str) -> bool:
    """Match one owner-held inode after an allowed native rename."""

    if _WINDOWS:
        expected = identity.windows_capability
        if expected is None:
            return False
        try:
            actual_path = Path(os.path.abspath(path))
            actual = capture_windows_path(
                actual_path,
                directory=True,
                generation_token=expected.generation_token,
            )
        except (OSError, TypeError, ValueError, RuntimeError):
            return False
        return (
            actual.volume == expected.volume
            and actual.file_id == expected.file_id
            and actual.owner_sid == expected.owner_sid
            and actual.security_sha256 == expected.security_sha256
        )
    try:
        actual = Path(path)
        info = actual.lstat()
    except (OSError, TypeError, ValueError, RuntimeError):
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and not info.st_mode & 0o022
        and info.st_dev == identity.device
        and info.st_ino == identity.inode
    )


def _empty_owned_directory_identity(
    value: object,
    path: str,
    *,
    error_code: DocumentAssetWorkspaceErrorCode,
) -> _DirectoryIdentity:
    if type(value) is not str or not value:
        _fail(error_code, path)
    directory = Path(value)
    if not directory.is_absolute():
        _fail(error_code, path)
    try:
        info = directory.lstat()
        with os.scandir(directory) as entries:
            empty = next(entries, None) is None
    except (OSError, TypeError, ValueError, RuntimeError):
        _fail(error_code, path)
    if _WINDOWS:
        attributes = int(getattr(info, "st_file_attributes", 0))
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not empty
        ):
            _fail(error_code, path)
        try:
            # FreeCAD creates this empty child below an already-authenticated
            # process-private root.  Make its inherited ACL explicit before it
            # becomes a durable capability boundary.
            set_private_dacl(directory)
        except OSError:
            _fail(error_code, path)
        return _private_directory_identity(directory, path, error_code=error_code)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or not empty
    ):
        _fail(error_code, path)
    return _DirectoryIdentity(directory, info.st_dev, info.st_ino)


def _identity_exists_below_private_root(
    root: _DirectoryIdentity,
    identity: _DirectoryIdentity,
) -> bool:
    """Find one borrowed inode among bounded direct children without deleting it."""

    if not _same_directory(root, str(root.path)):
        _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/document/native_root")
    try:
        with os.scandir(root.path) as entries:
            for index, entry in enumerate(entries, start=1):
                if index > _MAX_CLEANUP_ENTRIES:
                    _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/document/native_root")
                info = entry.stat(follow_symlinks=False)
                if _WINDOWS and int(
                    getattr(info, "st_file_attributes", 0)
                ) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    continue
                if _WINDOWS:
                    expected = identity.windows_capability
                    if expected is None:
                        continue
                    try:
                        actual = capture_windows_path(
                            Path(entry.path),
                            directory=True,
                            generation_token=expected.generation_token,
                        )
                    except OSError:
                        continue
                    if (
                        actual.volume == expected.volume
                        and actual.file_id == expected.file_id
                    ):
                        return True
                    continue
                if info.st_dev == identity.device and info.st_ino == identity.inode:
                    return True
    except DocumentAssetWorkspaceError:
        raise
    except OSError:
        _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/document/native_root")
    return False


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _create_private_directory(
    parent: Path,
    prefix: str,
    *,
    expected_parent: WindowsPathCapability | None,
    error_code: DocumentAssetWorkspaceErrorCode,
    error_path: str,
) -> _DirectoryIdentity:
    """Create one unguessable private directory without an ACL exposure window."""

    if not _WINDOWS:
        try:
            directory = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
            os.chmod(directory, 0o700)
        except (OSError, ValueError, RuntimeError):
            _fail(error_code, error_path)
        return _private_directory_identity(
            directory,
            error_path,
            error_code=error_code,
        )
    for _ in range(16):
        directory = parent / f"{prefix}{secrets.token_hex(16)}"
        try:
            capability = ensure_private_directory(
                directory,
                expected_parent=expected_parent,
            )
        except FileExistsError:
            continue
        except (OSError, TypeError, ValueError, RuntimeError):
            _fail(error_code, error_path)
        return _DirectoryIdentity(
            directory,
            capability.volume,
            capability.file_id,
            capability,
        )
    _fail(error_code, error_path)


def _remove_private_tree(identity: _DirectoryIdentity) -> None:
    """Remove only one still-identical private workspace, with hard bounds."""

    root = identity.path
    if not _lexists(root):
        return
    if not _same_directory(identity, str(root)):
        _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/directory")
    seen = 0

    def remove(directory: Path, depth: int) -> None:
        nonlocal seen
        if depth > _MAX_CLEANUP_DEPTH:
            _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/cleanup")
        try:
            entries = tuple(os.scandir(directory))
        except OSError:
            _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/cleanup")
        for entry in entries:
            seen += 1
            if seen > _MAX_CLEANUP_ENTRIES:
                _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/cleanup")
            child = Path(entry.path)
            try:
                info = child.lstat()
                if _WINDOWS and int(
                    getattr(info, "st_file_attributes", 0)
                ) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    _fail(
                        DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED,
                        "/workspace/cleanup",
                    )
                if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    if not _WINDOWS and (
                        info.st_uid != os.geteuid() or info.st_mode & 0o077
                    ):
                        _fail(
                            DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED,
                            "/workspace/cleanup",
                        )
                    if _WINDOWS:
                        set_private_dacl(child)
                        child_identity = capture_windows_path(child, directory=True)
                    remove(child, depth + 1)
                    if _WINDOWS:
                        validate_windows_path(child_identity, directory=True)
                    child.rmdir()
                else:
                    if _WINDOWS:
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            _fail(
                                DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED,
                                "/workspace/cleanup",
                            )
                        set_private_dacl(child)
                        child_identity = capture_windows_path(child, directory=False)
                        validate_windows_path(child_identity, directory=False)
                    child.unlink()
            except DocumentAssetWorkspaceError:
                raise
            except OSError:
                _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/cleanup")

    remove(root, 0)
    try:
        root.rmdir()
    except OSError:
        _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/cleanup")
    if _lexists(root):
        _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/cleanup")


class DocumentAssetWorkspace:
    """Own exact private ``TransientDir`` lifetimes for Session documents.

    A supplied ``root`` is trusted host configuration and must already be an
    absolute, owner-only 0700 directory.  With no supplied root, an equally
    private per-Session parent is created lazily.  Builds that leave
    ``TransientDir`` empty receive a random 0700 child there.  Builds that
    create it eagerly are accepted only as a borrowed direct child of the
    authenticated 0700 ``FREECAD_USER_TEMP`` root.  The document object,
    ownership mode, directory path, device and inode stay bound until native
    close plus exact cleanup succeeds.
    """

    __slots__ = ("_configured_root", "_documents", "_owned_root")

    def __init__(self, root: Path | None = None) -> None:
        if root is not None:
            checked = _private_directory_identity(
                root,
                "/workspace/root",
                error_code=DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED,
            )
            self._configured_root: _DirectoryIdentity | None = checked
        else:
            self._configured_root = None
        self._owned_root: _DirectoryIdentity | None = None
        self._documents: list[_DocumentWorkspace] = []

    def _root(self) -> _DirectoryIdentity:
        configured = self._configured_root
        if configured is not None:
            if not _same_directory(configured, str(configured.path)):
                _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/workspace/root")
            return configured
        owned = self._owned_root
        if owned is None:
            parent = Path(os.path.abspath(tempfile.gettempdir()))
            owned = _create_private_directory(
                parent,
                ".vibecad-document-assets-",
                expected_parent=None,
                error_code=DocumentAssetWorkspaceErrorCode.ATTACH_FAILED,
                error_path="/workspace/root",
            )
            self._owned_root = owned
        elif not _same_directory(owned, str(owned.path)):
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/workspace/root")
        return owned

    def _record_for(self, document: object) -> _DocumentWorkspace | None:
        return next((item for item in self._documents if item.document is document), None)

    def attach(self, document: object) -> Path:
        """Attach when the document has no pre-existing native directory."""

        return self._attach(document, allow_native_directory=False)

    def attach_fresh_document(self, document: object) -> Path:
        """Attach a newly-created native document across FreeCAD builds.

        Some builds create an empty per-document cache directory eagerly.  It
        remains owned and deleted by FreeCAD, and is only borrowed when its
        process root and exact identity are private.  Loaded or populated
        documents are never eligible.
        """

        return self._attach(document, allow_native_directory=True)

    def _attach(
        self,
        document: object,
        *,
        allow_native_directory: bool,
    ) -> Path:
        """Bind one exact native directory or install a host-owned fallback."""

        if document is None:
            _fail(DocumentAssetWorkspaceErrorCode.INVALID_INPUT, "/document")
        existing = self._record_for(document)
        if existing is not None:
            return self.require_attached(document)
        try:
            current = document.TransientDir
        except (Exception, SystemExit):
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document/transient_dir")
        if type(current) is not str:
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document/transient_dir")
        if current:
            if not allow_native_directory:
                _fail(
                    DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED,
                    "/document/transient_dir",
                )
            try:
                objects = document.Objects
                file_name = document.FileName
            except (Exception, SystemExit):
                _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document")
            if (
                not isinstance(objects, (list, tuple))
                or objects
                or type(file_name) is not str
                or file_name
            ):
                _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document")
            native_root_value = os.environ.get("FREECAD_USER_TEMP")
            if type(native_root_value) is not str or not native_root_value:
                _fail(
                    DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED,
                    "/document/native_root",
                )
            native_root = _private_directory_identity(
                Path(native_root_value),
                "/document/native_root",
                error_code=DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED,
            )
            native_path = Path(os.path.abspath(current)) if _WINDOWS else Path(current)
            if native_path.parent != native_root.path:
                _fail(
                    DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED,
                    "/document/transient_dir",
                )
            native_directory = _empty_owned_directory_identity(
                current,
                "/document/transient_dir",
                error_code=DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED,
            )
            if native_directory.path.parent != native_root.path or not _same_directory(
                native_root, str(native_root.path)
            ):
                _fail(
                    DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED,
                    "/document/transient_dir",
                )
            record = _DocumentWorkspace(
                document,
                native_directory,
                _WorkspaceOwnership.FREECAD_NATIVE_BORROWED,
                native_root,
            )
            self._documents.append(record)
            return native_directory.path
        root = self._root()
        directory: Path | None = None
        identity: _DirectoryIdentity | None = None
        try:
            identity = _create_private_directory(
                root.path,
                ".document-",
                expected_parent=root.windows_capability,
                error_code=DocumentAssetWorkspaceErrorCode.ATTACH_FAILED,
                error_path="/workspace/directory",
            )
            directory = identity.path
            document.TransientDir = str(directory)
            if not _same_directory(identity, document.TransientDir):
                _fail(DocumentAssetWorkspaceErrorCode.ATTACH_FAILED, "/document/transient_dir")
        except DocumentAssetWorkspaceError:
            try:
                document.TransientDir = ""
            except (Exception, SystemExit):
                pass
            if identity is not None:
                try:
                    _remove_private_tree(identity)
                except DocumentAssetWorkspaceError:
                    pass
            elif directory is not None:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            self._remove_owned_root_if_empty()
            raise
        except (Exception, SystemExit):
            try:
                document.TransientDir = ""
            except (Exception, SystemExit):
                pass
            if identity is not None:
                try:
                    _remove_private_tree(identity)
                except DocumentAssetWorkspaceError:
                    pass
            elif directory is not None:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            self._remove_owned_root_if_empty()
            _fail(DocumentAssetWorkspaceErrorCode.ATTACH_FAILED, "/document/transient_dir")
        record = _DocumentWorkspace(
            document,
            identity,
            _WorkspaceOwnership.VIBECAD_OWNED,
        )
        self._documents.append(record)
        return identity.path

    def require_attached(self, document: object) -> Path:
        """Return the exact owned directory or fail closed on drift/tamper."""

        record = self._record_for(document)
        if record is None:
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document/workspace")
        try:
            current = document.TransientDir
        except (Exception, SystemExit):
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document/transient_dir")
        if type(current) is not str:
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document/transient_dir")
        if record.ownership is _WorkspaceOwnership.VIBECAD_OWNED:
            attached = _same_directory(record.directory, current)
        else:
            native_root = record.native_root
            actual = Path(current)
            attached = (
                native_root is not None
                and actual.parent == native_root.path
                and _same_directory(native_root, str(native_root.path))
                and _same_owned_directory(record.directory, current)
            )
        if not attached:
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document/transient_dir")
        if record.ownership is _WorkspaceOwnership.FREECAD_NATIVE_BORROWED:
            return Path(current)
        return record.directory.path

    def require_empty_unattached_fresh_document(self, document: object) -> None:
        """Authorize native close only when no filesystem deletion target exists."""

        if document is None or self._record_for(document) is not None:
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document")
        try:
            current = document.TransientDir
            objects = document.Objects
            file_name = document.FileName
        except (Exception, SystemExit):
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document")
        if (
            current != ""
            or not isinstance(objects, (list, tuple))
            or objects
            or type(file_name) is not str
            or file_name
        ):
            _fail(DocumentAssetWorkspaceErrorCode.PRECONDITION_FAILED, "/document")

    def release_after_close(self, document: object) -> None:
        """Forget one native-closed document after proving/removing its workspace."""

        record = self._record_for(document)
        if record is None:
            return
        if record.ownership is _WorkspaceOwnership.VIBECAD_OWNED:
            _remove_private_tree(record.directory)
        else:
            native_root = record.native_root
            if native_root is None or _identity_exists_below_private_root(
                native_root,
                record.directory,
            ):
                _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/document/transient_dir")
        self._documents.remove(record)
        self._remove_owned_root_if_empty()

    def _remove_owned_root_if_empty(self) -> None:
        owned = self._owned_root
        if owned is None or self._documents:
            return
        if not _lexists(owned.path):
            self._owned_root = None
            return
        if not _same_directory(owned, str(owned.path)):
            _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/root")
        try:
            owned.path.rmdir()
        except OSError:
            _fail(DocumentAssetWorkspaceErrorCode.CLEANUP_FAILED, "/workspace/root")
        self._owned_root = None


__all__ = [
    "DocumentAssetWorkspace",
    "DocumentAssetWorkspaceError",
    "DocumentAssetWorkspaceErrorCode",
]
