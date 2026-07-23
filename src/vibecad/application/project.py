"""Secure project bootstrap values and lazy runtime construction seams."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from vibecad.execution.revisions import (
    ProjectHead,
    RevisionRef,
    RevisionSourceBinding,
    _candidate_file_limit,
)
from vibecad.interaction.storage import SafeRoot, StorageFailure

__all__ = ("ProjectBootstrapResult",)

MAX_BOOTSTRAP_FILE_BYTES = 536_870_912
_COPY_CHUNK_BYTES = 1024 * 1024
_CLEANUP_RECORD_BYTES = 16_384
_PROJECT_ID = re.compile(r"project_[0-9a-f]{32}\Z")
_STAGE_NAME = re.compile(r"\.import\.[0-9a-f]{32}\.FCStd\Z")
_CLEANUP_NAME = re.compile(r"cleanup_[0-9a-f]{32}\.json\Z")


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _ordinary_single_file(value: os.stat_result, *, allow_empty: bool = False) -> bool:
    minimum = 0 if allow_empty else 1
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_nlink == 1
        and minimum <= value.st_size <= MAX_BOOTSTRAP_FILE_BYTES
    )


def _close(fd: int) -> bool:
    try:
        os.close(fd)
    except OSError:
        return False
    return True


def _require_pinned_root(root: SafeRoot, root_fd: int) -> None:
    try:
        value = os.fstat(root_fd)
    except OSError:
        raise StorageFailure("bootstrap root is unsafe") from None
    if (
        not stat.S_ISDIR(value.st_mode)
        or stat.S_IMODE(value.st_mode) != 0o700
        or value.st_uid != root.uid
        or (value.st_dev, value.st_ino) != root.identity
    ):
        raise StorageFailure("bootstrap root is unsafe")


def _confirm_live_root(root: SafeRoot) -> None:
    live_fd = root.open()
    if not _close(live_fd):
        raise StorageFailure("bootstrap root close failed")


def _borrow_root(
    root_path: Path,
    *,
    safe_root: SafeRoot | None,
    root_fd: int | None,
) -> tuple[SafeRoot, int, bool]:
    if safe_root is None and root_fd is None:
        selected = SafeRoot(root_path)
        selected_fd = selected.open()
        owns_fd = True
    elif type(safe_root) is SafeRoot and type(root_fd) is int:
        selected = safe_root
        selected_fd = root_fd
        owns_fd = False
    else:
        raise StorageFailure("bootstrap root binding is invalid")
    if selected.path != root_path:
        if owns_fd:
            _close(selected_fd)
        raise StorageFailure("bootstrap root binding is invalid")
    try:
        _require_pinned_root(selected, selected_fd)
    except Exception:
        if owns_fd:
            _close(selected_fd)
        raise
    return selected, selected_fd, owns_fd


def _descriptor_root_path(root: SafeRoot, root_fd: int) -> Path:
    """Resolve the current path of a pinned directory descriptor and verify identity."""

    _require_pinned_root(root, root_fd)
    if os.name != "posix":
        raise StorageFailure("stable descriptor paths are unavailable")
    try:
        if sys.platform == "darwin":
            import fcntl  # noqa: PLC0415

            raw = fcntl.fcntl(root_fd, fcntl.F_GETPATH, b"\0" * 1024)
            encoded = raw.split(b"\0", 1)[0]
            if not encoded:
                raise OSError
            current = Path(os.fsdecode(encoded))
        else:
            link = Path(f"/proc/self/fd/{root_fd}")
            current = Path(os.readlink(link))
    except (AttributeError, OSError, UnicodeError):
        raise StorageFailure("stable descriptor paths are unavailable") from None
    if not current.is_absolute() or current != root.path:
        raise StorageFailure("bootstrap root identity changed")
    try:
        live = current.lstat()
    except OSError:
        raise StorageFailure("bootstrap root identity changed") from None
    if (
        not stat.S_ISDIR(live.st_mode)
        or (live.st_dev, live.st_ino) != root.identity
        or live.st_uid != root.uid
        or stat.S_IMODE(live.st_mode) != 0o700
    ):
        raise StorageFailure("bootstrap root identity changed")
    return current


def _current_directory_matches(root: SafeRoot) -> bool:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    current_fd = -1
    try:
        current_fd = os.open(".", flags)
        current = os.fstat(current_fd)
        return (
            stat.S_ISDIR(current.st_mode)
            and (current.st_dev, current.st_ino) == root.identity
            and current.st_uid == root.uid
            and stat.S_IMODE(current.st_mode) == 0o700
        )
    except OSError:
        return False
    finally:
        if current_fd >= 0:
            _close(current_fd)


def _darwin_thread_fchdir(fd: int) -> None:
    """Set or clear Darwin's calling-thread-only working directory."""

    if sys.platform != "darwin" or type(fd) is not int:
        raise StorageFailure("thread-local descriptor paths are unavailable")
    try:
        import ctypes  # noqa: PLC0415

        library = ctypes.CDLL(None, use_errno=True)
        operation = library.pthread_fchdir_np
        operation.argtypes = [ctypes.c_int]
        operation.restype = ctypes.c_int
        ctypes.set_errno(0)
        if operation(fd) != 0:
            raise OSError
    except (AttributeError, OSError):
        raise StorageFailure("thread-local descriptor paths are unavailable") from None


def _validate_import_from_pinned_root(port, root: SafeRoot, root_fd: int, name: str):
    """Run a path-writing CAD import relative to a descriptor-pinned working directory."""

    if sys.platform != "darwin" or type(name) is not str or _STAGE_NAME.fullmatch(name) is None:
        raise StorageFailure("descriptor-relative CAD import is unavailable")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    previous_fd = -1
    previous_identity: tuple[int, int] | None = None
    entered = False
    primary: BaseException | None = None
    result = None
    restore_failed = False
    try:
        _descriptor_root_path(root, root_fd)
        previous_fd = os.open(".", flags)
        previous = os.fstat(previous_fd)
        previous_identity = (previous.st_dev, previous.st_ino)
        _darwin_thread_fchdir(root_fd)
        entered = True
        if not _current_directory_matches(root):
            raise StorageFailure("bootstrap working directory is unsafe")
        try:
            result = port.validate_import(Path(name))
        except BaseException as error:
            primary = error
        if primary is None and not _current_directory_matches(root):
            primary = StorageFailure("CAD import changed its working directory")
    except BaseException as error:
        primary = error
    finally:
        if entered:
            try:
                _darwin_thread_fchdir(-1)
            except StorageFailure:
                restore_failed = True
        if previous_fd >= 0:
            current_fd = -1
            try:
                current_fd = os.open(".", flags)
                current = os.fstat(current_fd)
                if (current.st_dev, current.st_ino) != previous_identity:
                    restore_failed = True
            except OSError:
                restore_failed = True
            finally:
                if current_fd >= 0 and not _close(current_fd):
                    restore_failed = True
            if not _close(previous_fd):
                restore_failed = True
    if restore_failed:
        raise StorageFailure("CAD import working directory restore failed") from primary
    if primary is not None:
        raise primary.with_traceback(primary.__traceback__)
    return result


def _corrupt_content() -> Exception:
    from vibecad.execution.revisions import RevisionStoreError, RevisionStoreErrorCode

    return RevisionStoreError(RevisionStoreErrorCode.CORRUPT_CONTENT)


def _source_path(value: object) -> Path:
    if type(value) is type(Path("/")):
        result = value
    elif type(value) is str:
        result = Path(value)
    else:
        raise ValueError("invalid project bootstrap source")
    if not result.is_absolute() or ".." in result.parts:
        raise ValueError("invalid project bootstrap source")
    return result


def _copy_to_private_staging(
    source: object,
    bootstrap_root: Path,
    *,
    safe_root: SafeRoot | None = None,
    root_fd: int | None = None,
) -> Path:
    selected_root, selected_fd, owns_fd = _borrow_root(
        bootstrap_root,
        safe_root=safe_root,
        root_fd=root_fd,
    )
    try:
        source_path = _source_path(source)
    except ValueError:
        if owns_fd:
            _close(selected_fd)
        raise
    try:
        before = source_path.lstat()
    except OSError:
        if owns_fd:
            _close(selected_fd)
        raise ValueError("invalid project bootstrap source") from None
    if not _ordinary_single_file(before):
        if owns_fd:
            _close(selected_fd)
        raise ValueError("invalid project bootstrap source")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        source_fd = os.open(source_path, flags)
    except OSError:
        if owns_fd:
            _close(selected_fd)
        raise ValueError("invalid project bootstrap source") from None
    stage_name = f".import.{secrets.token_hex(16)}.FCStd"
    stage = bootstrap_root / stage_name
    stage_fd = None
    failed = False
    try:
        opened = os.fstat(source_fd)
        if not _ordinary_single_file(opened) or _stat_identity(opened) != _stat_identity(before):
            raise ValueError("invalid project bootstrap source")
        stage_fd = os.open(
            stage_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=selected_fd,
        )
        remaining = opened.st_size
        while remaining:
            chunk = os.read(source_fd, min(_COPY_CHUNK_BYTES, remaining))
            if not chunk:
                raise ValueError("invalid project bootstrap source")
            view = memoryview(chunk)
            while view:
                written = os.write(stage_fd, view)
                if written <= 0:
                    raise OSError
                view = view[written:]
            remaining -= len(chunk)
        if _stat_identity(os.fstat(source_fd)) != _stat_identity(opened):
            raise ValueError("invalid project bootstrap source")
        os.fsync(stage_fd)
        staged = os.fstat(stage_fd)
        if (
            not selected_root.regular_file(
                staged,
                maximum=MAX_BOOTSTRAP_FILE_BYTES,
            )
            or staged.st_size != opened.st_size
        ):
            raise ValueError("invalid project bootstrap source")
        os.fsync(selected_fd)
        _confirm_live_root(selected_root)
    except (OSError, ValueError):
        failed = True
        raise ValueError("invalid project bootstrap source") from None
    finally:
        try:
            os.close(source_fd)
        except OSError:
            failed = True
        if stage_fd is not None:
            try:
                os.close(stage_fd)
            except OSError:
                failed = True
        if failed:
            try:
                os.unlink(stage_name, dir_fd=selected_fd)
                os.fsync(selected_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if owns_fd and not _close(selected_fd):
            failed = True
    if failed:
        raise ValueError("invalid project bootstrap source")
    return stage


def _remove_staging(
    path: Path,
    *,
    safe_root: SafeRoot | None = None,
    root_fd: int | None = None,
) -> bool:
    if type(path) is not type(Path("/")) or _STAGE_NAME.fullmatch(path.name) is None:
        return False
    try:
        _selected_root, selected_fd, owns_fd = _borrow_root(
            path.parent,
            safe_root=safe_root,
            root_fd=root_fd,
        )
    except OSError:
        return False
    removed = False
    try:
        try:
            os.unlink(path.name, dir_fd=selected_fd)
        except FileNotFoundError:
            pass
        os.fsync(selected_fd)
        removed = True
    except OSError:
        removed = False
    finally:
        if owns_fd and not _close(selected_fd):
            removed = False
    return removed


def _cleanup_record_path(root: Path, project_id: str) -> Path:
    if type(project_id) is not str or _PROJECT_ID.fullmatch(project_id) is None:
        raise ValueError("invalid project cleanup id")
    return root / f"cleanup_{project_id.removeprefix('project_')}.json"


def _write_cleanup_record(
    root: Path,
    *,
    project_id: str,
    stage: Path,
    published: bool,
    safe_root: SafeRoot | None = None,
    root_fd: int | None = None,
) -> None:
    if (
        type(published) is not bool
        or stage.parent != root
        or _STAGE_NAME.fullmatch(stage.name) is None
    ):
        raise OSError
    try:
        target = _cleanup_record_path(root, project_id)
    except ValueError:
        raise OSError from None
    record = {
        "project_id": project_id,
        "published": published,
        "schema_version": 1,
        "stage_name": stage.name,
    }
    raw = json.dumps(
        record,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(raw) > _CLEANUP_RECORD_BYTES:
        raise OSError
    try:
        selected_root, selected_fd, owns_fd = _borrow_root(
            root,
            safe_root=safe_root,
            root_fd=root_fd,
        )
    except OSError:
        raise OSError from None
    succeeded = False
    try:
        _confirm_live_root(selected_root)
        selected_root.atomic_write(
            selected_fd,
            target.name,
            raw,
            token=secrets.token_hex(16),
        )
        readback, current = selected_root.read_file_at(
            selected_fd,
            target.name,
            maximum=_CLEANUP_RECORD_BYTES,
        )
        if readback != raw or not selected_root.regular_file(
            current, maximum=_CLEANUP_RECORD_BYTES
        ):
            raise StorageFailure("cleanup record is unsafe")
        _confirm_live_root(selected_root)
        succeeded = True
    except OSError:
        raise OSError from None
    finally:
        if owns_fd and not _close(selected_fd):
            succeeded = False
    if not succeeded:
        raise OSError


def recover_bootstrap_cleanup(root: Path) -> None:
    """Best-effort convergence for private staging left by an earlier success."""

    try:
        safe_root = SafeRoot(root)
        root_fd = safe_root.open()
    except OSError:
        return
    try:
        try:
            names = tuple(sorted(os.listdir(root_fd)))
        except OSError:
            return
        for record_name in names:
            if _CLEANUP_NAME.fullmatch(record_name) is None:
                continue
            try:
                raw, record_info = safe_root.read_file_at(
                    root_fd,
                    record_name,
                    maximum=_CLEANUP_RECORD_BYTES,
                )
                mapping = json.loads(raw)
                if type(mapping) is not dict or set(mapping) != {
                    "project_id",
                    "published",
                    "schema_version",
                    "stage_name",
                }:
                    continue
                project_id = mapping["project_id"]
                stage_name = mapping["stage_name"]
                if (
                    type(mapping["schema_version"]) is not int
                    or mapping["schema_version"] != 1
                    or type(project_id) is not str
                    or _PROJECT_ID.fullmatch(project_id) is None
                    or type(mapping["published"]) is not bool
                    or type(stage_name) is not str
                    or _STAGE_NAME.fullmatch(stage_name) is None
                    or _cleanup_record_path(root, project_id).name != record_name
                ):
                    continue
                if not _remove_staging(
                    root / stage_name,
                    safe_root=safe_root,
                    root_fd=root_fd,
                ):
                    continue
                current = os.stat(
                    record_name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                if _stat_identity(current) != _stat_identity(record_info):
                    continue
                os.unlink(record_name, dir_fd=root_fd)
                os.fsync(root_fd)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        try:
            _confirm_live_root(safe_root)
        except OSError:
            return
    finally:
        _close(root_fd)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectBootstrapResult:
    head: ProjectHead
    revision: RevisionRef
    cleanup_required: bool = False

    def __post_init__(self) -> None:
        if not (
            type(self.head) is ProjectHead
            and type(self.revision) is RevisionRef
            and self.head.project_id == self.revision.project_id
            and self.head.revision_id == self.revision.id
            and self.head.manifest_sha256 == self.revision.manifest_sha256
            and type(self.cleanup_required) is bool
        ):
            raise TypeError("invalid project bootstrap result")


def verify_generation_zero(
    revision_store,
    project_id: str,
    evidence,
) -> ProjectBootstrapResult:
    head = revision_store.load_head(project_id)
    revision = revision_store.load_revision(project_id, head.revision_id)
    if not (
        type(head) is ProjectHead
        and type(revision) is RevisionRef
        and head.project_id == project_id
        and head.generation == 0
        and revision.project_id == project_id
        and revision.id == head.revision_id
        and revision.manifest_sha256 == head.manifest_sha256
        and revision.base_revision is None
        and revision.artifacts == ()
    ):
        raise ValueError("project bootstrap readback failed")
    if evidence is None:
        if revision.model is not None:
            raise ValueError("project bootstrap readback failed")
    elif not (
        revision.model is not None
        and revision.model.name == "model.FCStd"
        and revision.model.format == "fcstd"
        and revision.model.sha256 == evidence.sha256
        and revision.model.size_bytes == evidence.size_bytes
    ):
        raise ValueError("project bootstrap readback failed")
    return ProjectBootstrapResult(head=head, revision=revision)


def bootstrap_import_project(
    *,
    project_id: str,
    source: object,
    bootstrap_root: Path,
    revision_store,
    lease_manager,
    cad_port_factory,
) -> ProjectBootstrapResult:
    """Validate a private copy before evidence-bound generation-zero publication."""

    from vibecad.interaction.cad import CadExecutionPort, ValidatedImportEvidence

    try:
        safe_root = SafeRoot(bootstrap_root)
        root_fd = safe_root.open()
    except OSError:
        raise ValueError("project bootstrap root is unsafe") from None
    try:
        stage = _copy_to_private_staging(
            source,
            bootstrap_root,
            safe_root=safe_root,
            root_fd=root_fd,
        )
        published = False
        result = None
        release_cleanup_required = False
        primary: BaseException | None = None
        try:
            try:
                safe_root.hash_open_file(
                    root_fd,
                    stage.name,
                    maximum=MAX_BOOTSTRAP_FILE_BYTES,
                )
                _require_pinned_root(safe_root, root_fd)
                _confirm_live_root(safe_root)
            except OSError:
                raise _corrupt_content() from None
            port = cad_port_factory(revision_store=revision_store)
            if not isinstance(port, CadExecutionPort):
                raise TypeError("cad_port_factory returned an invalid port")
            with _candidate_file_limit(revision_store):
                evidence = _validate_import_from_pinned_root(
                    port,
                    safe_root,
                    root_fd,
                    stage.name,
                )
            try:
                _confirm_live_root(safe_root)
                after_sha256, after_size, after_info = safe_root.hash_open_file(
                    root_fd,
                    stage.name,
                    maximum=MAX_BOOTSTRAP_FILE_BYTES,
                )
                _require_pinned_root(safe_root, root_fd)
            except OSError:
                raise _corrupt_content() from None
            if type(evidence) is not ValidatedImportEvidence:
                raise TypeError("CAD import validation returned invalid evidence")
            if not (
                evidence.sha256 == after_sha256
                and evidence.size_bytes == after_size
                and stat.S_ISREG(after_info.st_mode)
                and stat.S_IMODE(after_info.st_mode) == 0o600
                and after_info.st_uid == safe_root.uid
                and after_info.st_nlink == 1
                and after_info.st_dev == safe_root.identity[0]
            ):
                raise _corrupt_content()
            lease = lease_manager.acquire_project_write(project_id)
            try:
                try:
                    revision_store.import_trusted_fcstd_at(
                        project_id,
                        source_parent_fd=root_fd,
                        source_name=stage.name,
                        expected_binding=RevisionSourceBinding(
                            dev=after_info.st_dev,
                            ino=after_info.st_ino,
                            mode=after_info.st_mode,
                            uid=after_info.st_uid,
                            nlink=after_info.st_nlink,
                            size=after_info.st_size,
                            mtime_ns=after_info.st_mtime_ns,
                            ctime_ns=after_info.st_ctime_ns,
                        ),
                        expected_sha256=evidence.sha256,
                        expected_size=evidence.size_bytes,
                        lease=lease,
                    )
                except Exception as error:
                    from vibecad.execution.revisions import (
                        RevisionStoreError,
                        RevisionStoreErrorCode,
                    )

                    recoverable = type(error) is RevisionStoreError and (
                        error.code is RevisionStoreErrorCode.ALREADY_EXISTS
                        or (
                            error.code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN
                            and getattr(error, "head_committed", False) is True
                        )
                    )
                    if not recoverable:
                        raise
                published = True
                result = verify_generation_zero(revision_store, project_id, evidence)
                try:
                    _confirm_live_root(safe_root)
                except StorageFailure:
                    release_cleanup_required = True
            finally:
                try:
                    lease.release(owner_token=lease.owner_token)
                except Exception:
                    if (
                        type(result) is not ProjectBootstrapResult
                        or type(getattr(lease, "released", None)) is not bool
                        or lease.released is not True
                    ):
                        raise
                    release_cleanup_required = True
        except BaseException as error:
            primary = error

        try:
            cleaned = _remove_staging(
                stage,
                safe_root=safe_root,
                root_fd=root_fd,
            )
        except Exception:
            cleaned = False
        cleanup_record_failed = False
        if not cleaned:
            try:
                _write_cleanup_record(
                    bootstrap_root,
                    project_id=project_id,
                    stage=stage,
                    published=published,
                    safe_root=safe_root,
                    root_fd=root_fd,
                )
            except Exception:
                cleanup_record_failed = True
        if cleanup_record_failed:
            from vibecad.execution.revisions import (
                RevisionStoreError,
                RevisionStoreErrorCode,
            )

            if published:
                cleanup_error = RevisionStoreError(
                    RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                    head_committed=True,
                )
            else:
                cleanup_error = RevisionStoreError(RevisionStoreErrorCode.CLEANUP_REQUIRED)
            if primary is not None:
                raise cleanup_error from primary
            raise cleanup_error
        if primary is not None:
            raise primary.with_traceback(primary.__traceback__)
        if type(result) is not ProjectBootstrapResult:
            raise ValueError("project bootstrap failed")
        if not cleaned or release_cleanup_required:
            return ProjectBootstrapResult(
                head=result.head,
                revision=result.revision,
                cleanup_required=True,
            )
        return result
    finally:
        _close(root_fd)


def _default_cad_port_factory(*, revision_store):
    from vibecad.execution.worker_port import WorkerCadExecutionPort

    return WorkerCadExecutionPort(store=revision_store)


class ProjectRuntime:
    """One isolated project Session/Slot/Coordinator/TaskService composition."""

    __slots__ = ("_closed", "_coordinator", "_project_id", "service")

    def __init__(self, *, project_id: str, coordinator, service) -> None:
        self._project_id = project_id
        self._coordinator = coordinator
        self.service = service
        self._closed = False

    @property
    def head(self) -> ProjectHead:
        return self.service.runtime_head

    @property
    def stale(self) -> bool:
        return self.service.runtime_stale

    def close(self) -> bool:
        if self._closed:
            return True
        try:
            closed = self._coordinator._close_runtime(project_id=self._project_id)
        except Exception:
            return False
        if type(closed) is not bool or not closed:
            return False
        self._closed = True
        return True


def build_project_runtime(
    *,
    project_id: str,
    head: ProjectHead,
    task_store,
    revision_store,
    lease_manager,
    cad_port,
) -> ProjectRuntime:
    """Build one provisional runtime while the caller holds its short lease."""

    from vibecad.execution.candidate import (
        CandidateCoordinator,
        SessionBinding,
        SessionSlot,
    )
    from vibecad.interaction.cad import CadExecutionPort
    from vibecad.workflow.service import TaskService

    if not (
        type(head) is ProjectHead
        and type(project_id) is str
        and head.project_id == project_id
        and isinstance(cad_port, CadExecutionPort)
    ):
        raise ValueError("invalid project runtime head")
    exact = revision_store.load_head(project_id)
    if type(exact) is not ProjectHead or exact != head:
        raise ValueError("project runtime head changed")
    revision = revision_store.load_revision(project_id, head.revision_id)
    if not (
        type(revision) is RevisionRef
        and revision.project_id == project_id
        and revision.id == head.revision_id
        and revision.manifest_sha256 == head.manifest_sha256
    ):
        raise ValueError("project runtime revision is invalid")
    port = cad_port
    session = None
    try:
        session = port.open_revision(
            store=revision_store,
            revision=revision,
        )
        binding = SessionBinding(
            project_id=project_id,
            revision_id=head.revision_id,
            session=session,
        )
        slot = SessionSlot(binding)
        coordinator = CandidateCoordinator(
            store=revision_store,
            snapshot_port=port,
            session_slot=slot,
        )
        service = TaskService(
            task_store=task_store,
            revision_store=revision_store,
            lease_manager=lease_manager,
            coordinator=coordinator,
            executor=port,
            runtime_head=head,
        )
        return ProjectRuntime(
            project_id=project_id,
            coordinator=coordinator,
            service=service,
        )
    except Exception:
        if session is not None:
            try:
                port.close(session)
            except Exception:
                pass
        raise
