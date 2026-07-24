"""Parent-side opaque capabilities for one store-free FreeCAD Worker."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from vibecad.execution.results import (
    NormalizedToolOutcome,
    ToolDiagnosticClass,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionRef,
    _open_worker_candidate_staging,
    _open_worker_revision,
)
from vibecad.interaction.cad import (
    ValidatedImportEvidence,
    ValidatedMaterializationEvidence,
)
from vibecad.validation import EntityObservation, ShapeObservation
from vibecad.worker.generation import (
    WorkerError,
    WorkerErrorCode,
    WorkerGenerationState,
    _WorkerProcess,
)
from vibecad.workflow.contracts import ModelProgram, StepResult
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram, validate_model_program

_CANDIDATE = re.compile(r"worker_candidate_[0-9a-f]{32}\Z")
_WORKER_REVISION = re.compile(r"worker_revision_[0-9a-f]{32}\Z")
_SESSION = re.compile(r"worker_session_[0-9a-f]{32}\Z")
_PROGRAM = re.compile(r"worker_program_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STAGE_NAME = re.compile(r"\.(?:import|normalized|stage|work)\.[0-9a-f]{32}\.FCStd\Z")
_MAX_FILE_BYTES = 536_870_912
_MAX_DIRECTORY_ENTRIES = 64
_READ_CHUNK_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class _Identity:
    dev: int
    ino: int
    mode: int
    uid: int
    nlink: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    dev: int
    ino: int
    mode: int
    uid: int
    gid: int


def _identity(value: os.stat_result) -> _Identity:
    return _Identity(
        dev=value.st_dev,
        ino=value.st_ino,
        mode=value.st_mode,
        uid=value.st_uid,
        nlink=value.st_nlink,
        size=value.st_size,
        mtime_ns=value.st_mtime_ns,
        ctime_ns=value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(
        dev=value.st_dev,
        ino=value.st_ino,
        mode=value.st_mode,
        uid=value.st_uid,
        gid=value.st_gid,
    )


def _private_directory(value: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.geteuid()
        and stat.S_IMODE(value.st_mode) == 0o700
    )


def _private_file(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == 0o600
        and 0 <= value.st_size <= _MAX_FILE_BYTES
    )


def _entries(
    directory_fd: int,
    *,
    root_device: int,
) -> tuple[_Identity, _Identity]:
    try:
        model = os.stat("model.FCStd", dir_fd=directory_fd, follow_symlinks=False)
        step = os.stat("model.step", dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None
    if (
        not _private_file(model)
        or not _private_file(step)
        or model.st_dev != root_device
        or step.st_dev != root_device
    ):
        raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE)
    return _identity(model), _identity(step)


def _stable_file_identity(value: _Identity) -> tuple[int, int, int, int, int]:
    return (value.dev, value.ino, value.mode, value.uid, value.nlink)


def _hash_entry(directory_fd: int, name: str) -> tuple[str, int, _Identity]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = -1
    result = None
    error = None
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if not _private_file(before) or before.st_size <= 0:
            raise OSError
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_FILE_BYTES:
                raise OSError
            digest.update(chunk)
        after = os.fstat(descriptor)
        live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(after) != _identity(live)
            or size != before.st_size
        ):
            raise OSError
        result = (digest.hexdigest(), size, _identity(after))
    except BaseException as caught:
        error = caught
    close_failed = False
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except OSError:
            close_failed = True
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None
    if close_failed or result is None:
        raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE)
    return result


def _private_entries(directory_fd: int) -> tuple[tuple[str, _Identity], ...]:
    try:
        directory = os.fstat(directory_fd)
        if not _private_directory(directory):
            raise OSError
        names = tuple(sorted(os.listdir(directory_fd)))
        if (
            len(names) > _MAX_DIRECTORY_ENTRIES
            or len(names) != len(set(names))
            or any(type(name) is not str or name in {".", ".."} for name in names)
        ):
            raise OSError
        result: list[tuple[str, _Identity]] = []
        for name in names:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _private_file(current) or current.st_dev != directory.st_dev:
                raise OSError
            result.append((name, _identity(current)))
        return tuple(result)
    except OSError:
        raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None


def _revision_files(
    directory_fd: int,
    revision: RevisionRef,
) -> tuple[tuple[str, str, int, _Identity], ...]:
    entries = _private_entries(directory_fd)
    expected = {"manifest.json": (revision.manifest_sha256, None)}
    if revision.model is not None:
        expected[revision.model.name] = (
            revision.model.sha256,
            revision.model.size_bytes,
        )
    for artifact in revision.artifacts:
        expected[artifact.name] = (artifact.sha256, artifact.size_bytes)
    if tuple(name for name, _entry in entries) != tuple(sorted(expected)):
        raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE)
    result: list[tuple[str, str, int, _Identity]] = []
    for name, identity in entries:
        digest, size, hashed = _hash_entry(directory_fd, name)
        expected_digest, expected_size = expected[name]
        if (
            hashed != identity
            or digest != expected_digest
            or (expected_size is not None and size != expected_size)
        ):
            raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE)
        result.append((name, digest, size, identity))
    return tuple(result)


def _pin_private_directory(directory_fd: int) -> tuple[int, _DirectoryIdentity]:
    if type(directory_fd) is not int or directory_fd < 0:
        raise WorkerError(WorkerErrorCode.INVALID_INPUT)
    pinned = -1
    try:
        pinned = os.dup(directory_fd)
        os.set_inheritable(pinned, False)
        current = os.fstat(pinned)
        if not _private_directory(current) or os.get_inheritable(pinned):
            raise OSError
        return pinned, _directory_identity(current)
    except OSError:
        if pinned >= 0:
            with contextlib.suppress(OSError):
                os.close(pinned)
        raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None


class _Opaque:
    __slots__ = ()

    def __copy__(self):
        raise TypeError("Worker capabilities cannot be copied")

    def __deepcopy__(self, memo):
        del memo
        raise TypeError("Worker capabilities cannot be copied")

    def __reduce__(self):
        raise TypeError("Worker capabilities cannot be serialized")

    def __reduce_ex__(self, protocol):
        del protocol
        raise TypeError("Worker capabilities cannot be serialized")


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class WorkerCandidate(_Opaque):
    generation_id: str
    candidate_id: str


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class WorkerRevision(_Opaque):
    generation_id: str
    capability_id: str


@dataclass(frozen=True, slots=True, eq=False, repr=False)
class WorkerSession(_Opaque):
    generation_id: str
    session_id: str


@dataclass(slots=True)
class _CandidateState:
    handle: WorkerCandidate
    candidates_fd: int
    candidate_name: str
    directory_fd: int
    candidates_identity: _DirectoryIdentity
    directory_identity: _DirectoryIdentity
    model_identity: _Identity
    step_identity: _Identity
    root_device: int
    store: LocalRevisionStore
    lease: ProjectWriteLease
    project_id: str
    revision_id: str
    base_head: ProjectHead


@dataclass(slots=True)
class _RevisionState:
    handle: WorkerRevision
    revisions_fd: int
    revision_name: str
    directory_fd: int
    revisions_identity: _DirectoryIdentity
    directory_identity: _DirectoryIdentity
    files: tuple[tuple[str, str, int, _Identity], ...]
    root_device: int
    store: LocalRevisionStore
    revision: RevisionRef


@dataclass(slots=True)
class _SessionState:
    handle: WorkerSession
    candidate: WorkerCandidate | None = None
    revision: WorkerRevision | None = None


class FreeCadWorker(_Opaque):
    """One killable generation owning FreeCAD and only candidate-directory FDs."""

    __slots__ = (
        "_candidates",
        "_closing",
        "_creator_pid",
        "_lifecycle_lock",
        "_operation_lock",
        "_process",
        "_revisions",
        "_sessions",
    )

    def __init__(self, process: _WorkerProcess) -> None:
        if type(process) is not _WorkerProcess:
            raise WorkerError(WorkerErrorCode.INVALID_INPUT)
        self._process = process
        self._creator_pid = os.getpid()
        self._closing = False
        self._operation_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._candidates: dict[WorkerCandidate, _CandidateState] = {}
        self._revisions: dict[WorkerRevision, _RevisionState] = {}
        self._sessions: dict[WorkerSession, _SessionState] = {}

    @classmethod
    def start(
        cls,
        *,
        python: Path,
        source_root: Path,
    ) -> FreeCadWorker:
        return cls(
            _WorkerProcess.spawn(
                python=python,
                source_root=source_root,
            )
        )

    @classmethod
    def start_managed(cls, *, source_root: Path) -> FreeCadWorker:
        """Start the exact active engine-compatible managed generation."""

        from vibecad.runtime import paths as runtime_paths
        from vibecad.runtime.status import (
            capture_runtime_generation_evidence,
            engine_compatible_generation,
        )

        worker = None
        try:
            prefix = runtime_paths.active_runtime_prefix()
            evidence = capture_runtime_generation_evidence(prefix)
            if not engine_compatible_generation(evidence):
                raise ValueError
            worker = cls.start(
                python=evidence.python,
                source_root=source_root,
            )
            if capture_runtime_generation_evidence(prefix) != evidence:
                raise ValueError
            return worker
        except BaseException as error:
            if worker is not None:
                worker.terminate()
            if not isinstance(error, Exception) or isinstance(error, WorkerError):
                raise
            raise WorkerError(WorkerErrorCode.START_FAILED) from None

    @property
    def generation_id(self) -> str:
        return self._process.generation_id

    @property
    def state(self) -> WorkerGenerationState:
        return self._process.state

    @property
    def pid(self) -> int:
        return self._process.pid

    def _ensure_process(self) -> None:
        state = self._process.state
        if os.getpid() != self._creator_pid:
            raise WorkerError(WorkerErrorCode.CLOSED)
        if self._closing or state is not WorkerGenerationState.READY:
            raise WorkerError(
                WorkerErrorCode.GENERATION_LOST
                if self._closing
                or state
                in {
                    WorkerGenerationState.TERMINATING,
                    WorkerGenerationState.DEAD,
                    WorkerGenerationState.CLEANUP_REQUIRED,
                }
                else WorkerErrorCode.CLOSED
            )

    def _invalidate(self) -> None:
        with self._lifecycle_lock:
            self._closing = True
            for state in tuple(self._candidates.values()):
                with contextlib.suppress(OSError):
                    os.close(state.directory_fd)
                with contextlib.suppress(OSError):
                    os.close(state.candidates_fd)
            self._candidates.clear()
            for state in tuple(self._revisions.values()):
                with contextlib.suppress(OSError):
                    os.close(state.directory_fd)
                with contextlib.suppress(OSError):
                    os.close(state.revisions_fd)
            self._revisions.clear()
            self._sessions.clear()

    def _request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout_ms: int,
        capability_fd: int | None = None,
    ) -> dict[str, object]:
        self._ensure_process()
        try:
            return self._process.request(
                method,
                params,
                timeout_ms=timeout_ms,
                capability_fd=capability_fd,
            )
        except BaseException as error:
            if (
                not isinstance(error, Exception)
                or self._process.state is not WorkerGenerationState.READY
            ):
                self._invalidate()
            raise

    def _protocol_loss(self) -> None:
        try:
            self._process.terminate()
        finally:
            self._invalidate()
        raise WorkerError(WorkerErrorCode.GENERATION_LOST)

    def _candidate_state(self, value: object) -> _CandidateState:
        if type(value) is not WorkerCandidate:
            raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
        state = self._candidates.get(value)
        if state is None or value.generation_id != self.generation_id or state.handle is not value:
            raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
        return state

    def _revision_state(self, value: object) -> _RevisionState:
        if type(value) is not WorkerRevision:
            raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
        state = self._revisions.get(value)
        if state is None or value.generation_id != self.generation_id or state.handle is not value:
            raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
        return state

    def _session_state(self, value: object) -> _SessionState:
        if type(value) is not WorkerSession:
            raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
        state = self._sessions.get(value)
        if state is None or value.generation_id != self.generation_id or state.handle is not value:
            raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
        return state

    def _require_candidate_authority(self, state: _CandidateState) -> None:
        opened = None
        candidates_fd = -1
        directory_fd = -1
        try:
            opened = _open_worker_candidate_staging(
                state.store,
                expected_head=state.base_head,
                revision_id=state.revision_id,
                lease=state.lease,
            )
            candidates_fd = opened[0]
            directory_fd = opened[1]
            os.close(directory_fd)
            directory_fd = -1
            os.close(candidates_fd)
            candidates_fd = -1
        except Exception:
            if opened is not None:
                if directory_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(directory_fd)
                if candidates_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(candidates_fd)
            raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None

    def _require_live_candidate(self, state: _CandidateState) -> None:
        with self._lifecycle_lock:
            self._ensure_process()
            self._require_candidate_authority(state)
            try:
                candidates = os.fstat(state.candidates_fd)
                descriptor = os.fstat(state.directory_fd)
                live = os.stat(
                    state.candidate_name,
                    dir_fd=state.candidates_fd,
                    follow_symlinks=False,
                )
            except Exception:
                raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None
            model, step = _entries(
                state.directory_fd,
                root_device=state.root_device,
            )
            if (
                _directory_identity(candidates) != state.candidates_identity
                or _directory_identity(descriptor) != state.directory_identity
                or _directory_identity(live) != state.directory_identity
                or not _private_directory(descriptor)
                or descriptor.st_dev != state.root_device
                or model != state.model_identity
                or step != state.step_identity
            ):
                raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE)

    def _require_live_revision(self, state: _RevisionState) -> None:
        with self._lifecycle_lock:
            self._ensure_process()
            fresh_revisions = -1
            fresh_directory = -1
            try:
                (
                    fresh_revisions,
                    fresh_directory,
                    fresh_name,
                    fresh_root_device,
                ) = _open_worker_revision(
                    state.store,
                    expected_revision=state.revision,
                )
                pinned_parent = os.fstat(state.revisions_fd)
                pinned_directory = os.fstat(state.directory_fd)
                pinned_live = os.stat(
                    state.revision_name,
                    dir_fd=state.revisions_fd,
                    follow_symlinks=False,
                )
                fresh_parent = os.fstat(fresh_revisions)
                fresh_value = os.fstat(fresh_directory)
                fresh_live = os.stat(
                    fresh_name,
                    dir_fd=fresh_revisions,
                    follow_symlinks=False,
                )
                files = _revision_files(state.directory_fd, state.revision)
                fresh_files = _revision_files(fresh_directory, state.revision)
                if (
                    fresh_name != state.revision_name
                    or fresh_root_device != state.root_device
                    or _directory_identity(pinned_parent) != state.revisions_identity
                    or _directory_identity(fresh_parent) != state.revisions_identity
                    or _directory_identity(pinned_directory) != state.directory_identity
                    or _directory_identity(pinned_live) != state.directory_identity
                    or _directory_identity(fresh_value) != state.directory_identity
                    or _directory_identity(fresh_live) != state.directory_identity
                    or files != state.files
                    or fresh_files != state.files
                ):
                    raise OSError
            except WorkerError:
                raise
            except Exception:
                raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None
            finally:
                if fresh_directory >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fresh_directory)
                if fresh_revisions >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fresh_revisions)

    def bind_candidate(
        self,
        *,
        store: LocalRevisionStore,
        lease: ProjectWriteLease,
        base_head: ProjectHead,
        revision_id: str,
    ) -> WorkerCandidate:
        if (
            type(store) is not LocalRevisionStore
            or type(lease) is not ProjectWriteLease
            or type(base_head) is not ProjectHead
            or type(revision_id) is not str
            or re.fullmatch(r"revision_[0-9a-f]{32}", revision_id) is None
            or revision_id == base_head.revision_id
        ):
            raise WorkerError(WorkerErrorCode.INVALID_INPUT)
        candidates_fd = -1
        descriptor = -1
        with self._operation_lock:
            self._ensure_process()
            try:
                (
                    candidates_fd,
                    descriptor,
                    candidate_name,
                    root_device,
                ) = _open_worker_candidate_staging(
                    store,
                    expected_head=base_head,
                    revision_id=revision_id,
                    lease=lease,
                )
                candidates = os.fstat(candidates_fd)
                captured = os.fstat(descriptor)
                live = os.stat(
                    candidate_name,
                    dir_fd=candidates_fd,
                    follow_symlinks=False,
                )
                if (
                    _directory_identity(captured) != _directory_identity(live)
                    or not _private_directory(captured)
                    or captured.st_dev != root_device
                ):
                    raise OSError
                model, step = _entries(
                    descriptor,
                    root_device=root_device,
                )
            except BaseException as error:
                if descriptor >= 0:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                if candidates_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(candidates_fd)
                if not isinstance(error, Exception):
                    raise
                raise WorkerError(WorkerErrorCode.INVALID_CANDIDATE) from None
            candidate_id = f"worker_candidate_{os.urandom(16).hex()}"
            try:
                result = self._request(
                    "candidate.bind",
                    {
                        "candidate_id": candidate_id,
                        "project_id": base_head.project_id,
                        "revision_id": revision_id,
                        "base_revision_id": base_head.revision_id,
                    },
                    timeout_ms=30_000,
                    capability_fd=descriptor,
                )
                if set(result) != {"candidate_id"} or result["candidate_id"] != candidate_id:
                    self._protocol_loss()
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                with contextlib.suppress(OSError):
                    os.close(candidates_fd)
                raise
            handle = WorkerCandidate(
                generation_id=self.generation_id,
                candidate_id=candidate_id,
            )
            state = _CandidateState(
                handle=handle,
                candidates_fd=candidates_fd,
                candidate_name=candidate_name,
                directory_fd=descriptor,
                candidates_identity=_directory_identity(candidates),
                directory_identity=_directory_identity(captured),
                model_identity=model,
                step_identity=step,
                root_device=root_device,
                store=store,
                lease=lease,
                project_id=base_head.project_id,
                revision_id=revision_id,
                base_head=base_head,
            )
            try:
                with self._lifecycle_lock:
                    self._ensure_process()
                    try:
                        self._require_live_candidate(state)
                    except WorkerError:
                        self._protocol_loss()
                    self._candidates[handle] = state
                    return handle
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                with contextlib.suppress(OSError):
                    os.close(candidates_fd)
                raise

    def bind_revision(
        self,
        *,
        store: LocalRevisionStore,
        revision: RevisionRef,
    ) -> WorkerRevision:
        if type(store) is not LocalRevisionStore or type(revision) is not RevisionRef:
            raise WorkerError(WorkerErrorCode.INVALID_INPUT)
        revisions_fd = -1
        descriptor = -1
        with self._operation_lock:
            self._ensure_process()
            try:
                (
                    revisions_fd,
                    descriptor,
                    revision_name,
                    root_device,
                ) = _open_worker_revision(
                    store,
                    expected_revision=revision,
                )
                revisions_stat = os.fstat(revisions_fd)
                directory_stat = os.fstat(descriptor)
                live_stat = os.stat(
                    revision_name,
                    dir_fd=revisions_fd,
                    follow_symlinks=False,
                )
                files = _revision_files(descriptor, revision)
                if (
                    _directory_identity(directory_stat) != _directory_identity(live_stat)
                    or not _private_directory(directory_stat)
                    or directory_stat.st_dev != root_device
                ):
                    raise OSError
            except BaseException as error:
                if descriptor >= 0:
                    with contextlib.suppress(OSError):
                        os.close(descriptor)
                if revisions_fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(revisions_fd)
                if not isinstance(error, Exception):
                    raise
                raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE) from None
            capability_id = f"worker_revision_{os.urandom(16).hex()}"
            file_mappings = [
                {
                    "name": name,
                    "sha256": digest,
                    "size_bytes": size,
                }
                for name, digest, size, _identity_value in files
            ]
            try:
                result = self._request(
                    "revision.bind",
                    {
                        "revision_id": capability_id,
                        "project_id": revision.project_id,
                        "store_revision_id": revision.id,
                        "model_name": (None if revision.model is None else revision.model.name),
                        "files": file_mappings,
                    },
                    timeout_ms=30_000,
                    capability_fd=descriptor,
                )
                if set(result) != {"revision_id"} or result["revision_id"] != capability_id:
                    self._protocol_loss()
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                with contextlib.suppress(OSError):
                    os.close(revisions_fd)
                raise
            handle = WorkerRevision(
                generation_id=self.generation_id,
                capability_id=capability_id,
            )
            state = _RevisionState(
                handle=handle,
                revisions_fd=revisions_fd,
                revision_name=revision_name,
                directory_fd=descriptor,
                revisions_identity=_directory_identity(revisions_stat),
                directory_identity=_directory_identity(directory_stat),
                files=files,
                root_device=root_device,
                store=store,
                revision=revision,
            )
            try:
                with self._lifecycle_lock:
                    self._ensure_process()
                    try:
                        self._require_live_revision(state)
                    except WorkerError:
                        self._protocol_loss()
                    self._revisions[handle] = state
                    return handle
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                with contextlib.suppress(OSError):
                    os.close(revisions_fd)
                raise

    def _new_session(
        self,
        *,
        candidate: WorkerCandidate,
        method: str,
    ) -> WorkerSession:
        with self._operation_lock:
            self._ensure_process()
            candidate_state = self._candidate_state(candidate)
            self._require_live_candidate(candidate_state)
            result = self._request(
                method,
                {"candidate_id": candidate.candidate_id},
                timeout_ms=30_000,
            )
            if (
                set(result) != {"session_id"}
                or type(result["session_id"]) is not str
                or _SESSION.fullmatch(result["session_id"]) is None
            ):
                self._protocol_loss()
            with self._lifecycle_lock:
                self._ensure_process()
                try:
                    self._require_live_candidate(candidate_state)
                except WorkerError:
                    self._protocol_loss()
                handle = WorkerSession(
                    generation_id=self.generation_id,
                    session_id=result["session_id"],  # type: ignore[arg-type]
                )
                self._sessions[handle] = _SessionState(
                    handle=handle,
                    candidate=candidate,
                )
                return handle

    def create_empty(self, candidate: WorkerCandidate) -> WorkerSession:
        return self._new_session(
            candidate=candidate,
            method="session.create_empty",
        )

    def load_fcstd(self, candidate: WorkerCandidate) -> WorkerSession:
        return self._new_session(
            candidate=candidate,
            method="session.load_fcstd",
        )

    def load_revision(self, revision: WorkerRevision) -> WorkerSession:
        with self._operation_lock:
            self._ensure_process()
            revision_state = self._revision_state(revision)
            self._require_live_revision(revision_state)
            result = self._request(
                "session.load_revision",
                {"revision_id": revision.capability_id},
                timeout_ms=30_000,
            )
            if (
                set(result) != {"session_id"}
                or type(result["session_id"]) is not str
                or _SESSION.fullmatch(result["session_id"]) is None
            ):
                self._protocol_loss()
            with self._lifecycle_lock:
                self._ensure_process()
                try:
                    self._require_live_revision(revision_state)
                except WorkerError:
                    self._protocol_loss()
                handle = WorkerSession(
                    generation_id=self.generation_id,
                    session_id=result["session_id"],  # type: ignore[arg-type]
                )
                self._sessions[handle] = _SessionState(
                    handle=handle,
                    revision=revision,
                )
                return handle

    def _require_pair(
        self,
        *,
        session: WorkerSession,
        candidate: WorkerCandidate,
    ) -> tuple[_SessionState, _CandidateState]:
        session_state = self._session_state(session)
        candidate_state = self._candidate_state(candidate)
        if session_state.candidate is not candidate:
            raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
        return session_state, candidate_state

    def execute_program(
        self,
        *,
        program: ModelProgram | ValidatedProgram,
        candidate: WorkerCandidate,
        session: WorkerSession,
    ) -> tuple[NormalizedToolOutcome, ...]:
        try:
            if type(program) is ModelProgram:
                validated = validate_model_program(program)
            elif type(program) is ValidatedProgram:
                program.require_authentic()
                validated = validate_model_program(program.program)
            else:
                raise TypeError
            source = validated.program
        except Exception:
            raise WorkerError(WorkerErrorCode.INVALID_INPUT) from None
        with self._operation_lock:
            self._ensure_process()
            _session_state, candidate_state = self._require_pair(
                session=session,
                candidate=candidate,
            )
            self._require_live_candidate(candidate_state)
            if source.base_revision != candidate_state.base_head.revision_id:
                raise WorkerError(WorkerErrorCode.INVALID_CANDIDATE)
            begin = self._request(
                "program.begin",
                {
                    "session_id": session.session_id,
                    "candidate_id": candidate.candidate_id,
                    "program": source.to_mapping(),
                },
                timeout_ms=30_000,
            )
            expected_ids = [command.id for command in validated.commands]
            expected_deadlines = [
                command.resource_budget.max_runtime_ms for command in validated.commands
            ]
            if (
                set(begin) != {"program_id", "command_ids", "command_deadlines_ms"}
                or type(begin["program_id"]) is not str
                or _PROGRAM.fullmatch(begin["program_id"]) is None
                or begin["command_ids"] != expected_ids
                or begin["command_deadlines_ms"] != expected_deadlines
            ):
                self._protocol_loss()
            try:
                self._require_live_candidate(candidate_state)
            except WorkerError:
                self._protocol_loss()
            program_id = begin["program_id"]
            outcomes: list[NormalizedToolOutcome] = []
            for index, (command_id, runtime_limit) in enumerate(
                zip(expected_ids, expected_deadlines, strict=True)
            ):
                try:
                    self._require_live_candidate(candidate_state)
                except WorkerError:
                    self._protocol_loss()
                response = self._request(
                    "program.execute_command",
                    {
                        "program_id": program_id,
                        "index": index,
                    },
                    timeout_ms=runtime_limit,
                )
                expected_done = index + 1 == len(expected_ids)
                if (
                    set(response)
                    != {
                        "index",
                        "command_id",
                        "runtime_limit_ms",
                        "done",
                        "outcome",
                    }
                    or response["index"] != index
                    or response["command_id"] != command_id
                    or response["runtime_limit_ms"] != runtime_limit
                    or type(response["done"]) is not bool
                    or type(response["outcome"]) is not dict
                ):
                    self._protocol_loss()
                outcome_raw = response["outcome"]
                try:
                    if set(outcome_raw) != {"result", "diagnostic"}:
                        raise ValueError
                    result = StepResult.from_mapping(outcome_raw["result"])
                    diagnostic_raw = outcome_raw["diagnostic"]
                    diagnostic = (
                        None if diagnostic_raw is None else ToolDiagnosticClass(diagnostic_raw)
                    )
                    outcome = NormalizedToolOutcome(
                        result=result,
                        diagnostic=diagnostic,
                    )
                except Exception:
                    self._protocol_loss()
                outcomes.append(outcome)
                if not outcome.result.ok:
                    if response["done"] is not True:
                        self._protocol_loss()
                    break
                if response["done"] is not expected_done:
                    self._protocol_loss()
            with self._lifecycle_lock:
                self._ensure_process()
                self._require_pair(session=session, candidate=candidate)
                try:
                    self._require_live_candidate(candidate_state)
                except WorkerError:
                    self._protocol_loss()
                return tuple(outcomes)

    def checkpoint(
        self,
        *,
        session: WorkerSession,
        candidate: WorkerCandidate,
    ) -> None:
        with self._operation_lock:
            self._ensure_process()
            _session_state, candidate_state = self._require_pair(
                session=session,
                candidate=candidate,
            )
            self._require_live_candidate(candidate_state)
            result = self._request(
                "session.checkpoint_fcstd",
                {
                    "session_id": session.session_id,
                    "candidate_id": candidate.candidate_id,
                },
                timeout_ms=30_000,
            )
            self._accept_artifact_result(
                candidate_state,
                result,
                name="model.FCStd",
            )

    def export_step(
        self,
        *,
        session: WorkerSession,
        candidate: WorkerCandidate,
    ) -> None:
        with self._operation_lock:
            self._ensure_process()
            _session_state, candidate_state = self._require_pair(
                session=session,
                candidate=candidate,
            )
            self._require_live_candidate(candidate_state)
            result = self._request(
                "session.export_step",
                {
                    "session_id": session.session_id,
                    "candidate_id": candidate.candidate_id,
                },
                timeout_ms=30_000,
            )
            self._accept_artifact_result(
                candidate_state,
                result,
                name="model.step",
            )

    def observe(
        self,
        *,
        session: WorkerSession,
        capability: WorkerCandidate | WorkerRevision,
    ) -> tuple[ShapeObservation | None, tuple[EntityObservation, ...]]:
        with self._operation_lock:
            self._ensure_process()
            session_state = self._session_state(session)
            if type(capability) is WorkerCandidate:
                candidate_state = self._candidate_state(capability)
                if session_state.candidate is not capability:
                    raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
                self._require_live_candidate(candidate_state)
                kind = "candidate"
                capability_id = capability.candidate_id
                revision_state = None
            elif type(capability) is WorkerRevision:
                revision_state = self._revision_state(capability)
                if session_state.revision is not capability:
                    raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
                self._require_live_revision(revision_state)
                kind = "revision"
                capability_id = capability.capability_id
                candidate_state = None
            else:
                raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
            result = self._request(
                "session.observe",
                {
                    "session_id": session.session_id,
                    "capability_kind": kind,
                    "capability_id": capability_id,
                },
                timeout_ms=30_000,
            )
            try:
                if (
                    set(result) != {"shape", "entities"}
                    or (result["shape"] is not None and type(result["shape"]) is not dict)
                    or type(result["entities"]) is not list
                ):
                    raise ValueError
                shape = (
                    None
                    if result["shape"] is None
                    else ShapeObservation.from_mapping(result["shape"])
                )
                entities = tuple(
                    EntityObservation.from_mapping(item) for item in result["entities"]
                )
                object_ids = tuple(item.object_id for item in entities)
                if object_ids != tuple(sorted(object_ids)) or len(object_ids) != len(
                    set(object_ids)
                ):
                    raise ValueError
            except Exception:
                self._protocol_loss()
            with self._lifecycle_lock:
                self._ensure_process()
                current = self._session_state(session)
                if current is not session_state:
                    self._protocol_loss()
                try:
                    if candidate_state is not None:
                        self._require_live_candidate(candidate_state)
                    else:
                        assert revision_state is not None
                        self._require_live_revision(revision_state)
                except WorkerError:
                    self._protocol_loss()
                return shape, entities

    def _accept_artifact_result(
        self,
        state: _CandidateState,
        result: dict[str, object],
        *,
        name: str,
    ) -> None:
        if (
            set(result) != {"sha256", "size_bytes"}
            or type(result["sha256"]) is not str
            or _DIGEST.fullmatch(result["sha256"]) is None
            or type(result["size_bytes"]) is not int
            or result["size_bytes"] <= 0
            or result["size_bytes"] > _MAX_FILE_BYTES
        ):
            self._protocol_loss()
        with self._lifecycle_lock:
            self._ensure_process()
            if self._candidates.get(state.handle) is not state:
                self._protocol_loss()
            try:
                self._require_candidate_authority(state)
                candidates = os.fstat(state.candidates_fd)
                descriptor = os.fstat(state.directory_fd)
                live = os.stat(
                    state.candidate_name,
                    dir_fd=state.candidates_fd,
                    follow_symlinks=False,
                )
                model, step = _entries(
                    state.directory_fd,
                    root_device=state.root_device,
                )
                digest, size, hashed = _hash_entry(state.directory_fd, name)
            except (OSError, WorkerError):
                self._protocol_loss()
            target = model if name == "model.FCStd" else step
            if (
                name not in {"model.FCStd", "model.step"}
                or _directory_identity(candidates) != state.candidates_identity
                or _directory_identity(descriptor) != state.directory_identity
                or _directory_identity(live) != state.directory_identity
                or hashed != target
                or digest != result["sha256"]
                or size != result["size_bytes"]
            ):
                self._protocol_loss()
            if name == "model.FCStd":
                if step != state.step_identity:
                    self._protocol_loss()
            elif model != state.model_identity or _stable_file_identity(
                step
            ) != _stable_file_identity(state.step_identity):
                self._protocol_loss()
            try:
                self._require_candidate_authority(state)
            except WorkerError:
                self._protocol_loss()
            state.model_identity = model
            state.step_identity = step

    def _validate_import_at(
        self,
        *,
        directory_fd: int,
        name: str,
        normalize: bool,
    ) -> ValidatedImportEvidence:
        if type(name) is not str or _STAGE_NAME.fullmatch(name) is None:
            raise WorkerError(WorkerErrorCode.INVALID_INPUT)
        with self._operation_lock:
            self._ensure_process()
            pinned, directory_identity = _pin_private_directory(directory_fd)
            try:
                before = _private_entries(pinned)
                before_mapping = dict(before)
                target_before = before_mapping.get(name)
                if target_before is None or target_before.size <= 0:
                    raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE)
                result = self._request(
                    ("validation.validate_import" if normalize else "validation.revalidate_import"),
                    {"name": name},
                    timeout_ms=30_000,
                    capability_fd=pinned,
                )
                try:
                    after_directory = os.fstat(pinned)
                    after = _private_entries(pinned)
                    after_mapping = dict(after)
                    target_after = after_mapping.get(name)
                    digest, size, hashed = _hash_entry(pinned, name)
                except (OSError, WorkerError):
                    self._protocol_loss()
                if (
                    _directory_identity(after_directory) != directory_identity
                    or target_after is None
                    or hashed != target_after
                    or set(after_mapping) != set(before_mapping)
                    or any(
                        after_mapping[entry_name] != entry_identity
                        for entry_name, entry_identity in before
                        if entry_name != name
                    )
                    or (not normalize and target_after != target_before)
                    or set(result) != {"sha256", "size_bytes"}
                    or type(result["sha256"]) is not str
                    or _DIGEST.fullmatch(result["sha256"]) is None
                    or type(result["size_bytes"]) is not int
                    or result["sha256"] != digest
                    or result["size_bytes"] != size
                ):
                    self._protocol_loss()
                try:
                    return ValidatedImportEvidence(
                        sha256=digest,
                        size_bytes=size,
                    )
                except ValueError:
                    self._protocol_loss()
            finally:
                with contextlib.suppress(OSError):
                    os.close(pinned)

    def validate_import(
        self,
        *,
        directory_fd: int,
        name: str,
    ) -> ValidatedImportEvidence:
        return self._validate_import_at(
            directory_fd=directory_fd,
            name=name,
            normalize=True,
        )

    def revalidate_normalized_import(
        self,
        *,
        directory_fd: int,
        name: str,
    ) -> ValidatedImportEvidence:
        return self._validate_import_at(
            directory_fd=directory_fd,
            name=name,
            normalize=False,
        )

    def validate_materialization(
        self,
        *,
        directory_fd: int,
    ) -> ValidatedMaterializationEvidence:
        with self._operation_lock:
            self._ensure_process()
            pinned, directory_identity = _pin_private_directory(directory_fd)
            try:
                before = _private_entries(pinned)
                before_mapping = dict(before)
                if (
                    before_mapping.get("model.FCStd") is None
                    or before_mapping.get("model.step") is None
                    or before_mapping["model.FCStd"].size <= 0
                    or before_mapping["model.step"].size <= 0
                ):
                    raise WorkerError(WorkerErrorCode.INTEGRITY_FAILURE)
                result = self._request(
                    "validation.validate_materialization",
                    {},
                    timeout_ms=30_000,
                    capability_fd=pinned,
                )
                try:
                    after_directory = os.fstat(pinned)
                    after = _private_entries(pinned)
                    fcstd_sha256, fcstd_size, fcstd_identity = _hash_entry(
                        pinned,
                        "model.FCStd",
                    )
                    step_sha256, step_size, step_identity = _hash_entry(
                        pinned,
                        "model.step",
                    )
                except (OSError, WorkerError):
                    self._protocol_loss()
                if (
                    _directory_identity(after_directory) != directory_identity
                    or after != before
                    or fcstd_identity != before_mapping["model.FCStd"]
                    or step_identity != before_mapping["model.step"]
                    or set(result)
                    != {
                        "fcstd_sha256",
                        "fcstd_size_bytes",
                        "step_sha256",
                        "step_size_bytes",
                    }
                    or type(result["fcstd_sha256"]) is not str
                    or _DIGEST.fullmatch(result["fcstd_sha256"]) is None
                    or type(result["fcstd_size_bytes"]) is not int
                    or type(result["step_sha256"]) is not str
                    or _DIGEST.fullmatch(result["step_sha256"]) is None
                    or type(result["step_size_bytes"]) is not int
                    or result["fcstd_sha256"] != fcstd_sha256
                    or result["fcstd_size_bytes"] != fcstd_size
                    or result["step_sha256"] != step_sha256
                    or result["step_size_bytes"] != step_size
                ):
                    self._protocol_loss()
                try:
                    return ValidatedMaterializationEvidence(
                        fcstd_sha256=fcstd_sha256,
                        fcstd_size_bytes=fcstd_size,
                        step_sha256=step_sha256,
                        step_size_bytes=step_size,
                    )
                except ValueError:
                    self._protocol_loss()
            finally:
                with contextlib.suppress(OSError):
                    os.close(pinned)

    def close_session(self, session: WorkerSession) -> None:
        with self._operation_lock:
            self._ensure_process()
            state = self._session_state(session)
            try:
                result = self._request(
                    "session.close",
                    {"session_id": session.session_id},
                    timeout_ms=5_000,
                )
            except WorkerError as error:
                if error.code is WorkerErrorCode.GENERATION_LOST:
                    raise
                self._protocol_loss()
            if set(result) != {"session_id"} or result["session_id"] != session.session_id:
                self._protocol_loss()
            with self._lifecycle_lock:
                self._ensure_process()
                if state.handle is not session or self._sessions.get(session) is not state:
                    self._protocol_loss()
                self._sessions.pop(session, None)

    def release_candidate(self, candidate: WorkerCandidate) -> None:
        with self._operation_lock:
            self._ensure_process()
            state = self._candidate_state(candidate)
            if any(item.candidate is candidate for item in self._sessions.values()):
                raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
            try:
                result = self._request(
                    "candidate.release",
                    {"candidate_id": candidate.candidate_id},
                    timeout_ms=5_000,
                )
            except WorkerError as error:
                if error.code is WorkerErrorCode.GENERATION_LOST:
                    raise
                self._protocol_loss()
            if set(result) != {"candidate_id"} or result["candidate_id"] != candidate.candidate_id:
                self._protocol_loss()
            with self._lifecycle_lock:
                self._ensure_process()
                if self._candidates.get(candidate) is not state:
                    self._protocol_loss()
                close_failed = False
                for name in ("directory_fd", "candidates_fd"):
                    descriptor = getattr(state, name)
                    try:
                        os.close(descriptor)
                    except OSError:
                        close_failed = True
                    else:
                        setattr(state, name, -1)
                if close_failed:
                    self._protocol_loss()
                self._candidates.pop(candidate, None)

    def release_revision(self, revision: WorkerRevision) -> None:
        with self._operation_lock:
            self._ensure_process()
            state = self._revision_state(revision)
            if any(item.revision is revision for item in self._sessions.values()):
                raise WorkerError(WorkerErrorCode.INVALID_HANDLE)
            try:
                result = self._request(
                    "revision.release",
                    {"revision_id": revision.capability_id},
                    timeout_ms=5_000,
                )
            except WorkerError as error:
                if error.code is WorkerErrorCode.GENERATION_LOST:
                    raise
                self._protocol_loss()
            if set(result) != {"revision_id"} or result["revision_id"] != revision.capability_id:
                self._protocol_loss()
            with self._lifecycle_lock:
                self._ensure_process()
                if self._revisions.get(revision) is not state:
                    self._protocol_loss()
                close_failed = False
                for name in ("directory_fd", "revisions_fd"):
                    descriptor = getattr(state, name)
                    try:
                        os.close(descriptor)
                    except OSError:
                        close_failed = True
                    else:
                        setattr(state, name, -1)
                if close_failed:
                    self._protocol_loss()
                self._revisions.pop(revision, None)

    def terminate(self) -> None:
        with self._lifecycle_lock:
            self._closing = True
        try:
            self._process.terminate()
        finally:
            self._invalidate()

    def close(self) -> None:
        if os.getpid() != self._creator_pid:
            raise WorkerError(WorkerErrorCode.CLOSED)
        with self._lifecycle_lock:
            self._closing = True
        try:
            self._process.close_gracefully()
        finally:
            self._invalidate()


__all__ = (
    "FreeCadWorker",
    "WorkerCandidate",
    "WorkerRevision",
    "WorkerSession",
)
