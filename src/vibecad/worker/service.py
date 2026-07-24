"""Child-side FreeCAD owner for the private Worker protocol."""

from __future__ import annotations

import array
import contextlib
import hashlib
import os
import re
import secrets
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path

from vibecad.execution.adapter import AdapterError
from vibecad.execution.candidate import ActiveCandidate, SessionBinding
from vibecad.execution.executor import (
    ExecutorError,
    ExecutorErrorCode,
    InProcessCadExecutor,
    _entity_observations,
    _export_session_step,
    _shape_observation,
)
from vibecad.execution.revisions import ProjectHead
from vibecad.freecad_env import prepare_freecad_import
from vibecad.interaction.cad import (
    ValidatedImportEvidence,
    ValidatedMaterializationEvidence,
)
from vibecad.worker.codec import (
    MAX_WORKER_REQUEST_BYTES,
    WorkerCodecError,
    WorkerWireErrorCode,
    decode_worker_request,
    encode_worker_response,
    error_response,
    success_response,
)
from vibecad.workflow.contracts import ModelProgram

_CANDIDATE = re.compile(r"worker_candidate_[0-9a-f]{32}\Z")
_WORKER_REVISION = re.compile(r"worker_revision_[0-9a-f]{32}\Z")
_SESSION = re.compile(r"worker_session_[0-9a-f]{32}\Z")
_PROGRAM = re.compile(r"worker_program_[0-9a-f]{32}\Z")
_PROJECT = re.compile(r"project_[0-9a-f]{32}\Z")
_REVISION = re.compile(r"revision_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_FILE = re.compile(r"(?:manifest\.json|model\.FCStd|model\.step)\Z")
_STAGE_NAME = re.compile(r"\.(?:import|normalized|stage|work)\.[0-9a-f]{32}\.FCStd\Z")
_MAX_SESSIONS = 6
_MAX_CANDIDATES = 8
_MAX_REVISIONS = 8
_MAX_DIRECTORY_ENTRIES = 64
_MAX_FILE_BYTES = 536_870_912
_READ_CHUNK_BYTES = 1_048_576


class _WorkerExecutor(InProcessCadExecutor):
    """Store-free subset of the existing trusted CAD implementation."""

    __slots__ = ()

    def __init__(self) -> None:
        pass


class _ServiceError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: WorkerWireErrorCode) -> None:
        self.code = code
        self.args = (code.value,)


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


@dataclass(slots=True)
class _Candidate:
    candidate_id: str
    project_id: str
    revision_id: str
    base_revision_id: str
    directory_fd: int
    directory_identity: _DirectoryIdentity
    model_identity: _Identity
    step_identity: _Identity


@dataclass(frozen=True, slots=True)
class _ExpectedFile:
    name: str
    sha256: str
    size_bytes: int


@dataclass(slots=True)
class _Revision:
    revision_id: str
    project_id: str
    store_revision_id: str
    model_name: str | None
    directory_fd: int
    directory_identity: _DirectoryIdentity
    entries: tuple[tuple[str, _Identity], ...]
    files: tuple[_ExpectedFile, ...]


@dataclass(slots=True)
class _Session:
    session_id: str
    capability_kind: str
    capability_id: str
    value: object


@dataclass(slots=True)
class _Program:
    program_id: str
    session_id: str
    candidate_id: str
    command_ids: tuple[str, ...]
    deadlines_ms: tuple[int, ...]
    execution: object
    next_index: int = 0


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


def _capture_candidate_entries(directory_fd: int) -> tuple[_Identity, _Identity]:
    try:
        directory = os.fstat(directory_fd)
        model = os.stat("model.FCStd", dir_fd=directory_fd, follow_symlinks=False)
        step = os.stat("model.step", dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
    if (
        not _private_file(model)
        or not _private_file(step)
        or model.st_dev != directory.st_dev
        or step.st_dev != directory.st_dev
    ):
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    return _identity(model), _identity(step)


def _stable_file_identity(value: _Identity) -> tuple[int, int, int, int, int]:
    return (value.dev, value.ino, value.mode, value.uid, value.nlink)


@contextlib.contextmanager
def _directory_cwd(
    directory_fd: int,
    expected_identity: _DirectoryIdentity,
):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    previous = -1
    try:
        current = os.fstat(directory_fd)
        if _directory_identity(current) != expected_identity or not _private_directory(current):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        previous = os.open(".", flags)
        os.fchdir(directory_fd)
        if _directory_identity(os.stat(".", follow_symlinks=False)) != expected_identity:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        yield
    finally:
        if previous >= 0:
            try:
                os.fchdir(previous)
            finally:
                os.close(previous)


def _candidate_cwd(candidate: _Candidate):
    return _directory_cwd(candidate.directory_fd, candidate.directory_identity)


def _revision_cwd(revision: _Revision):
    return _directory_cwd(revision.directory_fd, revision.directory_identity)


def _capture_private_entries(directory_fd: int) -> tuple[tuple[str, _Identity], ...]:
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
        values: list[tuple[str, _Identity]] = []
        for name in names:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _private_file(current) or current.st_dev != directory.st_dev:
                raise OSError
            values.append((name, _identity(current)))
        return tuple(values)
    except OSError:
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None


def _capture_revision_entries(revision: _Revision) -> tuple[tuple[str, _Identity], ...]:
    entries = _capture_private_entries(revision.directory_fd)
    if tuple(name for name, _entry in entries) != tuple(item.name for item in revision.files):
        raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    with _revision_cwd(revision):
        for expected, (name, identity) in zip(revision.files, entries, strict=True):
            digest, size, hashed = _hash_relative(name)
            if hashed != identity or digest != expected.sha256 or size != expected.size_bytes:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
    return entries


def _hash_relative(name: str) -> tuple[str, int, _Identity]:
    digest = hashlib.sha256()
    size = 0
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = -1
    result = None
    error = None
    try:
        fd = os.open(name, flags)
        before = os.fstat(fd)
        if not _private_file(before) or before.st_size <= 0:
            raise OSError
        while True:
            chunk = os.read(fd, _READ_CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_FILE_BYTES:
                raise OSError
            digest.update(chunk)
        after = os.fstat(fd)
        live = os.stat(name, follow_symlinks=False)
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
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            close_failed = True
    if error is not None:
        if not isinstance(error, Exception):
            raise error
        raise _ServiceError(WorkerWireErrorCode.ARTIFACT_FAILURE) from None
    if close_failed or result is None:
        raise _ServiceError(WorkerWireErrorCode.ARTIFACT_FAILURE)
    return result


def _exact_mapping(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    return value


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    return value


def _expected_files(value: object) -> tuple[_ExpectedFile, ...]:
    if type(value) is not list or not 1 <= len(value) <= 3:
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    result: list[_ExpectedFile] = []
    for raw in value:
        fields = _exact_mapping(raw, {"name", "sha256", "size_bytes"})
        name = _identifier(fields["name"], _REVISION_FILE)
        digest = _identifier(fields["sha256"], _DIGEST)
        size = fields["size_bytes"]
        if type(size) is not int or not 1 <= size <= _MAX_FILE_BYTES:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        result.append(
            _ExpectedFile(
                name=name,
                sha256=digest,
                size_bytes=size,
            )
        )
    names = tuple(item.name for item in result)
    if (
        names != tuple(sorted(names))
        or len(names) != len(set(names))
        or "manifest.json" not in names
    ):
        raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
    return tuple(result)


def _evidence_mapping(value: object) -> dict[str, object]:
    if type(value) is not ValidatedImportEvidence:
        raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
    return {
        "sha256": value.sha256,
        "size_bytes": value.size_bytes,
    }


def _materialization_mapping(value: object) -> dict[str, object]:
    if type(value) is not ValidatedMaterializationEvidence:
        raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
    return {
        "fcstd_sha256": value.fcstd_sha256,
        "fcstd_size_bytes": value.fcstd_size_bytes,
        "step_sha256": value.step_sha256,
        "step_size_bytes": value.step_size_bytes,
    }


def _outcome_mapping(outcome: object) -> dict[str, object]:
    from vibecad.execution.results import NormalizedToolOutcome

    if type(outcome) is not NormalizedToolOutcome:
        raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
    diagnostic = outcome.diagnostic
    return {
        "result": outcome.result.to_mapping(),
        "diagnostic": None if diagnostic is None else diagnostic.value,
    }


def _executor_code(error: ExecutorError) -> WorkerWireErrorCode:
    return {
        ExecutorErrorCode.INVALID_INPUT: WorkerWireErrorCode.INVALID_INPUT,
        ExecutorErrorCode.INVALID_CANDIDATE: WorkerWireErrorCode.INVALID_CANDIDATE,
        ExecutorErrorCode.INVALID_LEASE: WorkerWireErrorCode.INVALID_INPUT,
        ExecutorErrorCode.CAD_FAILURE: WorkerWireErrorCode.CAD_FAILURE,
        ExecutorErrorCode.ARTIFACT_FAILURE: WorkerWireErrorCode.ARTIFACT_FAILURE,
        ExecutorErrorCode.INTEGRITY_FAILURE: WorkerWireErrorCode.INTEGRITY_FAILURE,
    }[error.code]


class WorkerService:
    __slots__ = (
        "_candidates",
        "_engine",
        "_generation_id",
        "_programs",
        "_revisions",
        "_sessions",
        "_shutdown",
    )

    def __init__(self, generation_id: str) -> None:
        if (
            type(generation_id) is not str
            or re.fullmatch(r"worker_generation_[0-9a-f]{32}", generation_id) is None
        ):
            raise ValueError("invalid generation")
        self._generation_id = generation_id
        self._engine = _WorkerExecutor()
        self._candidates: dict[str, _Candidate] = {}
        self._revisions: dict[str, _Revision] = {}
        self._sessions: dict[str, _Session] = {}
        self._programs: dict[str, _Program] = {}
        self._shutdown = False

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown

    def _candidate(self, candidate_id: object) -> _Candidate:
        identifier = _identifier(candidate_id, _CANDIDATE)
        candidate = self._candidates.get(identifier)
        if candidate is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return candidate

    def _session(self, session_id: object) -> _Session:
        identifier = _identifier(session_id, _SESSION)
        session = self._sessions.get(identifier)
        if session is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return session

    def _revision(self, revision_id: object) -> _Revision:
        identifier = _identifier(revision_id, _WORKER_REVISION)
        revision = self._revisions.get(identifier)
        if revision is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return revision

    def _program(self, program_id: object) -> _Program:
        identifier = _identifier(program_id, _PROGRAM)
        program = self._programs.get(identifier)
        if program is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return program

    def _require_pair(
        self,
        *,
        session_id: object,
        candidate_id: object,
    ) -> tuple[_Session, _Candidate]:
        session = self._session(session_id)
        candidate = self._candidate(candidate_id)
        if (
            session.capability_kind != "candidate"
            or session.capability_id != candidate.candidate_id
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return session, candidate

    def _require_observation_pair(
        self,
        *,
        session_id: object,
        capability_kind: object,
        capability_id: object,
    ) -> tuple[_Session, _Candidate | _Revision]:
        session = self._session(session_id)
        if capability_kind == "candidate":
            capability = self._candidate(capability_id)
            identifier = capability.candidate_id
        elif capability_kind == "revision":
            capability = self._revision(capability_id)
            identifier = capability.revision_id
        else:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        if session.capability_kind != capability_kind or session.capability_id != identifier:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        return session, capability

    def _bind(self, params: object, descriptors: tuple[int, ...]) -> dict[str, object]:
        fields = _exact_mapping(
            params,
            {
                "candidate_id",
                "project_id",
                "revision_id",
                "base_revision_id",
            },
        )
        if len(descriptors) != 1 or len(self._candidates) >= _MAX_CANDIDATES:
            raise _ServiceError(
                WorkerWireErrorCode.INVALID_REQUEST
                if len(descriptors) != 1
                else WorkerWireErrorCode.RESOURCE_EXHAUSTED
            )
        candidate_id = _identifier(fields["candidate_id"], _CANDIDATE)
        project_id = _identifier(fields["project_id"], _PROJECT)
        revision_id = _identifier(fields["revision_id"], _REVISION)
        base_revision_id = _identifier(fields["base_revision_id"], _REVISION)
        if candidate_id in self._candidates:
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        if base_revision_id == revision_id:
            raise _ServiceError(WorkerWireErrorCode.INVALID_CANDIDATE)
        descriptor = descriptors[0]
        try:
            os.set_inheritable(descriptor, False)
            directory = os.fstat(descriptor)
            if not _private_directory(directory):
                raise OSError
            entries = set(os.listdir(descriptor))
            if not {"model.FCStd", "model.step"}.issubset(entries) or not entries.issubset(
                {
                    "model.FCStd",
                    "model.step",
                    "seed-intent.json",
                    "seed-binding.json",
                }
            ):
                raise OSError
            model, step = _capture_candidate_entries(descriptor)
        except (OSError, _ServiceError):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        candidate = _Candidate(
            candidate_id=candidate_id,
            project_id=project_id,
            revision_id=revision_id,
            base_revision_id=base_revision_id,
            directory_fd=descriptor,
            directory_identity=_directory_identity(directory),
            model_identity=model,
            step_identity=step,
        )
        self._candidates[candidate_id] = candidate
        return {"candidate_id": candidate_id}

    def _release_candidate(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"candidate_id"})
        candidate = self._candidate(fields["candidate_id"])
        if any(
            session.capability_kind == "candidate"
            and session.capability_id == candidate.candidate_id
            for session in self._sessions.values()
        ) or any(
            program.candidate_id == candidate.candidate_id for program in self._programs.values()
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        try:
            os.close(candidate.directory_fd)
        except OSError:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        self._candidates.pop(candidate.candidate_id, None)
        return {"candidate_id": candidate.candidate_id}

    def _bind_revision(
        self,
        params: object,
        descriptors: tuple[int, ...],
    ) -> dict[str, object]:
        fields = _exact_mapping(
            params,
            {
                "revision_id",
                "project_id",
                "store_revision_id",
                "model_name",
                "files",
            },
        )
        if len(descriptors) != 1 or len(self._revisions) >= _MAX_REVISIONS:
            raise _ServiceError(
                WorkerWireErrorCode.INVALID_REQUEST
                if len(descriptors) != 1
                else WorkerWireErrorCode.RESOURCE_EXHAUSTED
            )
        revision_id = _identifier(fields["revision_id"], _WORKER_REVISION)
        project_id = _identifier(fields["project_id"], _PROJECT)
        store_revision_id = _identifier(fields["store_revision_id"], _REVISION)
        model_name = fields["model_name"]
        if model_name is not None and model_name != "model.FCStd":
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        files = _expected_files(fields["files"])
        names = tuple(item.name for item in files)
        if (
            revision_id in self._revisions
            or (model_name is None and "model.FCStd" in names)
            or (model_name is not None and "model.FCStd" not in names)
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        descriptor = descriptors[0]
        try:
            os.set_inheritable(descriptor, False)
            directory = os.fstat(descriptor)
            if not _private_directory(directory):
                raise OSError
            revision = _Revision(
                revision_id=revision_id,
                project_id=project_id,
                store_revision_id=store_revision_id,
                model_name=model_name,
                directory_fd=descriptor,
                directory_identity=_directory_identity(directory),
                entries=(),
                files=files,
            )
            entries = _capture_revision_entries(revision)
            revision.entries = entries
        except (OSError, _ServiceError):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        self._revisions[revision_id] = revision
        return {"revision_id": revision_id}

    def _release_revision(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"revision_id"})
        revision = self._revision(fields["revision_id"])
        if any(
            session.capability_kind == "revision" and session.capability_id == revision.revision_id
            for session in self._sessions.values()
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        try:
            os.close(revision.directory_fd)
        except OSError:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        self._revisions.pop(revision.revision_id, None)
        return {"revision_id": revision.revision_id}

    def _load_revision(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"revision_id"})
        revision = self._revision(fields["revision_id"])
        if len(self._sessions) >= _MAX_SESSIONS:
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        with _revision_cwd(revision):
            current = _capture_revision_entries(revision)
            if current != revision.entries:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            if revision.model_name is None:
                value = self._engine.create_empty(
                    revision_id=revision.store_revision_id,
                )
            else:
                value = self._engine.load_fcstd(Path(revision.model_name))
            try:
                revalidated = _capture_revision_entries(revision)
            except BaseException as error:
                try:
                    self._engine.close(value)
                except BaseException:
                    raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
                if not isinstance(error, Exception):
                    raise
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
            if revalidated != current:
                try:
                    self._engine.close(value)
                except BaseException:
                    raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        session_id = f"worker_session_{secrets.token_hex(16)}"
        self._sessions[session_id] = _Session(
            session_id=session_id,
            capability_kind="revision",
            capability_id=revision.revision_id,
            value=value,
        )
        return {"session_id": session_id}

    def _create_empty(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"candidate_id"})
        candidate = self._candidate(fields["candidate_id"])
        if len(self._sessions) >= _MAX_SESSIONS:
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        value = self._engine.create_empty(revision_id=candidate.revision_id)
        session_id = f"worker_session_{secrets.token_hex(16)}"
        self._sessions[session_id] = _Session(
            session_id=session_id,
            capability_kind="candidate",
            capability_id=candidate.candidate_id,
            value=value,
        )
        return {"session_id": session_id}

    def _load(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"candidate_id"})
        candidate = self._candidate(fields["candidate_id"])
        if len(self._sessions) >= _MAX_SESSIONS:
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        with _candidate_cwd(candidate):
            current_model, current_step = _capture_candidate_entries(candidate.directory_fd)
            if (
                current_model != candidate.model_identity
                or current_step != candidate.step_identity
                or current_model.size <= 0
            ):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            value = self._engine.load_fcstd(Path("model.FCStd"))
        session_id = f"worker_session_{secrets.token_hex(16)}"
        self._sessions[session_id] = _Session(
            session_id=session_id,
            capability_kind="candidate",
            capability_id=candidate.candidate_id,
            value=value,
        )
        return {"session_id": session_id}

    def _checkpoint(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"session_id", "candidate_id"})
        session, candidate = self._require_pair(
            session_id=fields["session_id"],
            candidate_id=fields["candidate_id"],
        )
        with _candidate_cwd(candidate):
            current_model, current_step = _capture_candidate_entries(candidate.directory_fd)
            if current_model != candidate.model_identity or current_step != candidate.step_identity:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            try:
                self._engine.checkpoint_fcstd(session.value, Path("model.FCStd"))
                model, step = _capture_candidate_entries(candidate.directory_fd)
                if step != current_step:
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                digest, size, hashed = _hash_relative("model.FCStd")
                if hashed != model:
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            except BaseException:
                raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        candidate.model_identity = model
        candidate.step_identity = step
        return {"sha256": digest, "size_bytes": size}

    def _close_session(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"session_id"})
        session = self._session(fields["session_id"])
        try:
            self._engine.close(session.value)
        except BaseException:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        self._programs = {
            key: value
            for key, value in self._programs.items()
            if value.session_id != session.session_id
        }
        self._sessions.pop(session.session_id, None)
        return {"session_id": session.session_id}

    def _observe(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(
            params,
            {"session_id", "capability_kind", "capability_id"},
        )
        session, capability = self._require_observation_pair(
            session_id=fields["session_id"],
            capability_kind=fields["capability_kind"],
            capability_id=fields["capability_id"],
        )
        if any(item.session_id == session.session_id for item in self._programs.values()):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        if type(capability) is _Candidate:
            context = _candidate_cwd(capability)
            before = _capture_candidate_entries(capability.directory_fd)
            if before != (capability.model_identity, capability.step_identity):
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        else:
            context = _revision_cwd(capability)
            before = _capture_revision_entries(capability)
            if before != capability.entries:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        try:
            with context:
                objects = tuple(session.value.doc.Objects)  # type: ignore[attr-defined]
                shape = None if not objects else _shape_observation(session.value)
                entities = _entity_observations(session.value)
        except _ServiceError:
            raise
        except BaseException:
            raise _ServiceError(WorkerWireErrorCode.CAD_FAILURE) from None
        if type(capability) is _Candidate:
            after = _capture_candidate_entries(capability.directory_fd)
        else:
            after = _capture_revision_entries(capability)
        if after != before:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        return {
            "shape": None if shape is None else shape.to_mapping(),
            "entities": [item.to_mapping() for item in entities],
        }

    def _validation_directory(
        self,
        descriptors: tuple[int, ...],
    ) -> tuple[int, _DirectoryIdentity, tuple[tuple[str, _Identity], ...]]:
        if len(descriptors) != 1:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        descriptor = descriptors[0]
        try:
            os.set_inheritable(descriptor, False)
            current = os.fstat(descriptor)
            if not _private_directory(current):
                raise OSError
            entries = _capture_private_entries(descriptor)
        except (OSError, _ServiceError):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE) from None
        return descriptor, _directory_identity(current), entries

    def _validate_import(
        self,
        params: object,
        descriptors: tuple[int, ...],
        *,
        normalize: bool,
    ) -> dict[str, object]:
        fields = _exact_mapping(params, {"name"})
        name = _identifier(fields["name"], _STAGE_NAME)
        descriptor, identity, before = self._validation_directory(descriptors)
        before_mapping = dict(before)
        target_before = before_mapping.get(name)
        if target_before is None or target_before.size <= 0:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        with _directory_cwd(descriptor, identity):
            if normalize:
                evidence = self._engine.validate_import(Path(name))
            else:
                evidence = self._engine.revalidate_normalized_import(Path(name))
            digest, size, target_after_hash = _hash_relative(name)
        after = _capture_private_entries(descriptor)
        after_mapping = dict(after)
        target_after = after_mapping.get(name)
        if (
            target_after is None
            or target_after_hash != target_after
            or set(after_mapping) != set(before_mapping)
            or any(
                after_mapping[entry_name] != entry_identity
                for entry_name, entry_identity in before
                if entry_name != name
            )
            or (not normalize and target_after != target_before)
        ):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        result = _evidence_mapping(evidence)
        if result != {"sha256": digest, "size_bytes": size}:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        return result

    def _validate_materialization(
        self,
        params: object,
        descriptors: tuple[int, ...],
    ) -> dict[str, object]:
        _exact_mapping(params, set())
        descriptor, identity, before = self._validation_directory(descriptors)
        before_mapping = dict(before)
        if (
            before_mapping.get("model.FCStd") is None
            or before_mapping.get("model.step") is None
            or before_mapping["model.FCStd"].size <= 0
            or before_mapping["model.step"].size <= 0
        ):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        with _directory_cwd(descriptor, identity):
            evidence = self._engine.validate_materialization(
                fcstd=Path("model.FCStd"),
                step=Path("model.step"),
            )
            fcstd_sha256, fcstd_size, fcstd_identity = _hash_relative("model.FCStd")
            step_sha256, step_size, step_identity = _hash_relative("model.step")
        after = _capture_private_entries(descriptor)
        if (
            after != before
            or fcstd_identity != before_mapping["model.FCStd"]
            or step_identity != before_mapping["model.step"]
        ):
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        result = _materialization_mapping(evidence)
        if result != {
            "fcstd_sha256": fcstd_sha256,
            "fcstd_size_bytes": fcstd_size,
            "step_sha256": step_sha256,
            "step_size_bytes": step_size,
        }:
            raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
        return result

    def _begin_program(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(
            params,
            {"session_id", "candidate_id", "program"},
        )
        session, candidate = self._require_pair(
            session_id=fields["session_id"],
            candidate_id=fields["candidate_id"],
        )
        if any(item.session_id == session.session_id for item in self._programs.values()):
            raise _ServiceError(WorkerWireErrorCode.RESOURCE_EXHAUSTED)
        try:
            source = ModelProgram.from_mapping(fields["program"])
            validated = self._engine.validate_program(source)
            binding = SessionBinding(
                project_id=candidate.project_id,
                revision_id=candidate.revision_id,
                session=session.value,
            )
            active = ActiveCandidate(
                project_id=candidate.project_id,
                base_head=ProjectHead(
                    project_id=candidate.project_id,
                    generation=0,
                    revision_id=candidate.base_revision_id,
                    manifest_sha256="0" * 64,
                ),
                binding=binding,
                model_path=Path("model.FCStd"),
                step_path=Path("model.step"),
            )
            execution = self._engine._prepare_program_execution(
                program=validated,
                candidate=active,
            )
            command_ids = tuple(item.id for item in validated.commands)
            deadlines = tuple(item.resource_budget.max_runtime_ms for item in validated.commands)
        except (ExecutorError, AdapterError):
            raise
        except Exception:
            raise _ServiceError(WorkerWireErrorCode.INVALID_INPUT) from None
        program_id = f"worker_program_{secrets.token_hex(16)}"
        self._programs[program_id] = _Program(
            program_id=program_id,
            session_id=session.session_id,
            candidate_id=candidate.candidate_id,
            command_ids=command_ids,
            deadlines_ms=deadlines,
            execution=execution,
        )
        return {
            "program_id": program_id,
            "command_ids": list(command_ids),
            "command_deadlines_ms": list(deadlines),
        }

    def _execute_command(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"program_id", "index"})
        program = self._program(fields["program_id"])
        index = fields["index"]
        if (
            type(index) is not int
            or index != program.next_index
            or index < 0
            or index >= len(program.command_ids)
        ):
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        try:
            outcome = program.execution.step()
        except BaseException:
            self._programs.pop(program.program_id, None)
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        done = program.execution.done
        result = {
            "index": index,
            "command_id": program.command_ids[index],
            "runtime_limit_ms": program.deadlines_ms[index],
            "done": done,
            "outcome": _outcome_mapping(outcome),
        }
        program.next_index += 1
        if done:
            self._programs.pop(program.program_id, None)
        return result

    def _export_step(self, params: object) -> dict[str, object]:
        fields = _exact_mapping(params, {"session_id", "candidate_id"})
        session, candidate = self._require_pair(
            session_id=fields["session_id"],
            candidate_id=fields["candidate_id"],
        )
        if any(item.session_id == session.session_id for item in self._programs.values()):
            raise _ServiceError(WorkerWireErrorCode.INVALID_HANDLE)
        with _candidate_cwd(candidate):
            current_model, current_step = _capture_candidate_entries(candidate.directory_fd)
            if current_model != candidate.model_identity or current_step != candidate.step_identity:
                raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            try:
                _export_session_step(
                    session=session.value,
                    model_path=Path("model.FCStd"),
                    step_path=Path("model.step"),
                )
                model, step = _capture_candidate_entries(candidate.directory_fd)
                if model != current_model or _stable_file_identity(step) != _stable_file_identity(
                    current_step
                ):
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
                digest, size, hashed = _hash_relative("model.step")
                if hashed != step:
                    raise _ServiceError(WorkerWireErrorCode.INTEGRITY_FAILURE)
            except BaseException:
                raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR) from None
        candidate.model_identity = model
        candidate.step_identity = step
        return {"sha256": digest, "size_bytes": size}

    def _ready(self, params: object) -> dict[str, object]:
        _exact_mapping(params, set())
        prepare_freecad_import()
        import FreeCAD  # noqa: PLC0415
        import Part  # noqa: F401, PLC0415

        version = FreeCAD.Version()
        freecad_version = ".".join(version[:3])
        return {
            "worker_pid": os.getpid(),
            "python_version": ".".join(map(str, os.sys.version_info[:3])),
            "freecad_version": freecad_version,
        }

    def _shutdown_worker(self, params: object) -> dict[str, object]:
        _exact_mapping(params, set())
        failed = False
        self._programs.clear()
        for session in tuple(self._sessions.values()):
            try:
                self._engine.close(session.value)
            except Exception:
                failed = True
        self._sessions.clear()
        for candidate in tuple(self._candidates.values()):
            try:
                os.close(candidate.directory_fd)
            except OSError:
                failed = True
        self._candidates.clear()
        for revision in tuple(self._revisions.values()):
            try:
                os.close(revision.directory_fd)
            except OSError:
                failed = True
        self._revisions.clear()
        if failed:
            raise _ServiceError(WorkerWireErrorCode.INTERNAL_ERROR)
        self._shutdown = True
        return {"closed": True}

    def dispatch(
        self,
        method: str,
        params: object,
        descriptors: tuple[int, ...],
    ) -> dict[str, object]:
        descriptor_methods = {
            "candidate.bind",
            "revision.bind",
            "validation.validate_import",
            "validation.revalidate_import",
            "validation.validate_materialization",
        }
        if (method in descriptor_methods) != bool(descriptors):
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        handlers = {
            "worker.ready": self._ready,
            "candidate.bind": lambda value: self._bind(value, descriptors),
            "candidate.release": self._release_candidate,
            "revision.bind": lambda value: self._bind_revision(value, descriptors),
            "revision.release": self._release_revision,
            "session.create_empty": self._create_empty,
            "session.load_fcstd": self._load,
            "session.load_revision": self._load_revision,
            "session.checkpoint_fcstd": self._checkpoint,
            "session.observe": self._observe,
            "session.close": self._close_session,
            "program.begin": self._begin_program,
            "program.execute_command": self._execute_command,
            "session.export_step": self._export_step,
            "validation.validate_import": lambda value: self._validate_import(
                value,
                descriptors,
                normalize=True,
            ),
            "validation.revalidate_import": lambda value: self._validate_import(
                value,
                descriptors,
                normalize=False,
            ),
            "validation.validate_materialization": lambda value: self._validate_materialization(
                value, descriptors
            ),
            "worker.shutdown": self._shutdown_worker,
        }
        handler = handlers.get(method)
        if handler is None:
            raise _ServiceError(WorkerWireErrorCode.INVALID_REQUEST)
        return handler(params)

    def close(self) -> None:
        self._programs.clear()
        for session in tuple(self._sessions.values()):
            with contextlib.suppress(Exception):
                self._engine.close(session.value)
        self._sessions.clear()
        for candidate in tuple(self._candidates.values()):
            with contextlib.suppress(OSError):
                os.close(candidate.directory_fd)
        self._candidates.clear()
        for revision in tuple(self._revisions.values()):
            with contextlib.suppress(OSError):
                os.close(revision.directory_fd)
        self._revisions.clear()


def _recv_header_with_descriptors(
    connection: socket.socket,
) -> tuple[bytes, tuple[int, ...]]:
    header = bytearray()
    descriptors: list[int] = []
    ancillary_size = socket.CMSG_SPACE(array.array("i", range(4)).itemsize * 4)
    try:
        while len(header) < 4:
            fragment, ancillary, flags, _address = connection.recvmsg(
                4 - len(header),
                ancillary_size,
            )
            if not fragment:
                raise EOFError
            header.extend(fragment)
            unexpected = False
            for level, kind, data in ancillary:
                if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                    unexpected = True
                    continue
                received = array.array("i")
                if len(data) % received.itemsize:
                    unexpected = True
                    continue
                received.frombytes(data)
                descriptors.extend(received)
            if (
                unexpected
                or flags & (getattr(socket, "MSG_CTRUNC", 0) | getattr(socket, "MSG_TRUNC", 0))
                or len(descriptors) > 1
            ):
                raise WorkerCodecError("unexpected ancillary data")
    except BaseException:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    return bytes(header), tuple(descriptors)


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        fragment = connection.recv(remaining)
        if not fragment:
            raise EOFError
        chunks.append(fragment)
        remaining -= len(fragment)
    return b"".join(chunks)


def _recv_request(connection: socket.socket) -> tuple[dict[str, object], tuple[int, ...]]:
    header, descriptors = _recv_header_with_descriptors(connection)
    size = struct.unpack(">I", header)[0]
    if size <= 0 or size > MAX_WORKER_REQUEST_BYTES:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise WorkerCodecError("invalid Worker request frame")
    try:
        request = decode_worker_request(_recv_exact(connection, size))
    except BaseException:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    return request, descriptors


def _send_response(connection: socket.socket, response: dict[str, object]) -> None:
    raw = encode_worker_response(response)
    connection.sendall(struct.pack(">I", len(raw)) + raw)


def serve_worker(connection: socket.socket, generation_id: str) -> int:
    service = WorkerService(generation_id)
    try:
        while True:
            try:
                request, descriptors = _recv_request(connection)
            except EOFError:
                return 0
            if request["generation_id"] != generation_id:
                return 2
            internal = False
            succeeded = False
            response: dict[str, object] | None = None
            try:
                result = service.dispatch(
                    request["method"],  # type: ignore[arg-type]
                    request["params"],
                    descriptors,
                )
                succeeded = True
            except _ServiceError as error:
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=error.code,
                )
                internal = error.code is WorkerWireErrorCode.INTERNAL_ERROR
            except ExecutorError as error:
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=_executor_code(error),
                )
            except AdapterError:
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=WorkerWireErrorCode.INVALID_INPUT,
                )
            except BaseException:
                response = error_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    code=WorkerWireErrorCode.INTERNAL_ERROR,
                )
                internal = True
            finally:
                if request["method"] not in {"candidate.bind", "revision.bind"} or not succeeded:
                    for descriptor in descriptors:
                        with contextlib.suppress(OSError):
                            os.close(descriptor)
            if response is None:
                response = success_response(
                    generation_id=generation_id,
                    request_id=request["request_id"],  # type: ignore[arg-type]
                    result=result,
                )
            _send_response(connection, response)
            if internal or service.shutdown_requested:
                return 0 if service.shutdown_requested and not internal else 3
    finally:
        service.close()


__all__ = ("WorkerService", "serve_worker")
