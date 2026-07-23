"""Application-owned CAD port backed by one killable FreeCAD Worker generation."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

from vibecad.execution.candidate import (
    ActiveCandidate,
    CheckpointedCandidate,
    SealedCandidate,
)
from vibecad.execution.errors import ExecutorError, ExecutorErrorCode
from vibecad.execution.registry import ExecutionProfile
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionRef,
)
from vibecad.interaction.cad import (
    CadCapabilityStatus,
    CadExecutionPort,
    CadProfileCapability,
    CandidateEvidence,
    ValidatedImportEvidence,
    ValidatedMaterializationEvidence,
)
from vibecad.validation import (
    ArtifactObservation,
    EntityObservation,
    ObservationSnapshot,
    ShapeObservation,
    compare_entity_preservation,
)
from vibecad.worker.generation import (
    WorkerError,
    WorkerErrorCode,
    WorkerGenerationState,
)
from vibecad.workflow.contracts import ModelProgram
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram, validate_model_program
from vibecad.workflow.state import TaskArtifactRef

_SOURCE_ROOT = Path(__file__).resolve().parents[2]
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)


@dataclass(slots=True)
class _Capability:
    kind: str
    key: tuple[str, str]
    value: object
    sessions: set[object] = field(default_factory=set)
    base_head: ProjectHead | None = None
    lease: ProjectWriteLease | None = None
    revision: RevisionRef | None = None


def _executor_code(error: WorkerError) -> ExecutorErrorCode:
    if error.code is WorkerErrorCode.INVALID_INPUT:
        return ExecutorErrorCode.INVALID_INPUT
    if error.code in {
        WorkerErrorCode.INVALID_HANDLE,
        WorkerErrorCode.INVALID_CANDIDATE,
    }:
        return ExecutorErrorCode.INVALID_CANDIDATE
    if error.code is WorkerErrorCode.ARTIFACT_FAILURE:
        return ExecutorErrorCode.ARTIFACT_FAILURE
    if error.code is WorkerErrorCode.INTEGRITY_FAILURE:
        return ExecutorErrorCode.INTEGRITY_FAILURE
    return ExecutorErrorCode.CAD_FAILURE


def _fixed_error(code: ExecutorErrorCode) -> ExecutorError:
    return ExecutorError(code)


def _default_worker_factory(*, source_root: Path):
    from vibecad.worker.proxy import FreeCadWorker

    return FreeCadWorker.start_managed(source_root=source_root)


def _preservations(
    before: tuple[EntityObservation, ...],
    after: tuple[EntityObservation, ...],
):
    before_by_id = {item.object_id: item for item in before}
    after_by_id = {item.object_id: item for item in after}
    result = []
    for object_id in sorted(set(before_by_id) | set(after_by_id)):
        old = before_by_id.get(object_id)
        new = after_by_id.get(object_id)
        reference = old if old is not None else new
        if reference is None:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        targets = (reference.object_id,) + (
            (reference.feature_id,) if reference.feature_id is not None else ()
        )
        result.extend(compare_entity_preservation(old, new, target=target) for target in targets)
    return tuple(sorted(result, key=lambda item: item.target))


def _serialized(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._operation_lock:
            return method(self, *args, **kwargs)

    return guarded


class WorkerCadExecutionPort(CadExecutionPort):
    """Complete trusted CAD port for one application-owned Worker generation."""

    __slots__ = (
        "_capabilities",
        "_closed",
        "_generation_lost",
        "_lock",
        "_operation_lock",
        "_retired_sessions",
        "_sessions",
        "_source_root",
        "_start_in_flight",
        "_store",
        "_worker",
        "_worker_factory",
    )

    def __init__(
        self,
        *,
        store: LocalRevisionStore,
        worker_factory: Callable[..., object] = _default_worker_factory,
        source_root: Path = _SOURCE_ROOT,
    ) -> None:
        if (
            type(store) is not LocalRevisionStore
            or not callable(worker_factory)
            or type(source_root) is not type(Path("/"))
            or not source_root.is_absolute()
        ):
            raise TypeError("invalid Worker CAD port composition")
        self._store = store
        self._worker_factory = worker_factory
        self._source_root = source_root
        self._worker = None
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._closed = False
        self._generation_lost = False
        self._start_in_flight = False
        self._capabilities: dict[tuple[str, str, str], _Capability] = {}
        self._sessions: dict[object, _Capability] = {}
        self._retired_sessions: set[object] = set()

    @property
    def execution_profile(self) -> ExecutionProfile:
        return ExecutionProfile.HEADLESS

    @property
    def capabilities(self) -> tuple[CadProfileCapability, ...]:
        return (
            CadProfileCapability(
                profile=ExecutionProfile.HEADLESS,
                status=CadCapabilityStatus.VERIFIED,
                available=True,
                requires_gui_main_thread=False,
            ),
            CadProfileCapability(
                profile=ExecutionProfile.OFFSCREEN_GUI,
                status=CadCapabilityStatus.PLANNED,
                available=False,
                requires_gui_main_thread=True,
            ),
            CadProfileCapability(
                profile=ExecutionProfile.INTERACTIVE_GUI,
                status=CadCapabilityStatus.PLANNED,
                available=False,
                requires_gui_main_thread=True,
            ),
        )

    @property
    def generation_lost(self) -> bool:
        with self._lock:
            return self._generation_lost

    def _start_worker(self):
        with self._lock:
            if self._closed or self._generation_lost:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)
            worker = self._worker
            if worker is not None:
                return worker
            self._start_in_flight = True
        try:
            worker = self._worker_factory(source_root=self._source_root)
        except WorkerError as error:
            with self._lock:
                self._generation_lost = True
                self._start_in_flight = False
            raise _fixed_error(_executor_code(error)) from None
        except Exception:
            with self._lock:
                self._generation_lost = True
                self._start_in_flight = False
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        except BaseException:
            with self._lock:
                self._generation_lost = True
                self._start_in_flight = False
            raise
        with self._lock:
            self._start_in_flight = False
            if worker is not None and not self._closed and not self._generation_lost:
                self._worker = worker
                return worker
            self._generation_lost = True
            if worker is not None and self._worker is None:
                self._worker = worker
        if worker is not None:
            self._terminate_retired_worker(worker)
        raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)

    def _retire_worker(self, worker: object | None) -> None:
        with self._lock:
            if worker is not None and self._worker is not worker:
                return
            self._generation_lost = True
            if worker is not None:
                self._worker = worker
            self._retired_sessions.update(self._sessions)
            self._sessions.clear()
            self._capabilities.clear()

    def _terminate_retired_worker(self, worker: object) -> bool:
        terminate = getattr(worker, "terminate", None)
        if not callable(terminate):
            return False
        try:
            terminate()
        except BaseException:
            return False
        if getattr(worker, "state", None) is not WorkerGenerationState.DEAD:
            return False
        with self._lock:
            if self._worker is worker:
                self._worker = None
        return True

    @staticmethod
    def _close_retired_worker(worker: object) -> None:
        close = getattr(worker, "close", None)
        if not callable(close):
            return
        try:
            close()
        except BaseException:
            pass

    def _call(self, worker: object, method: str, *args, **kwargs):
        try:
            return getattr(worker, method)(*args, **kwargs)
        except WorkerError as error:
            if error.code in {
                WorkerErrorCode.START_FAILED,
                WorkerErrorCode.GENERATION_LOST,
                WorkerErrorCode.CLOSED,
            }:
                self._retire_worker(worker)
                self._terminate_retired_worker(worker)
            raise _fixed_error(_executor_code(error)) from None
        except ExecutorError:
            raise
        except Exception:
            self._retire_worker(worker)
            self._terminate_retired_worker(worker)
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None

    def _require_store(self, store: object) -> None:
        if store is not self._store:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)

    def _register_session(
        self,
        *,
        worker: object,
        state: _Capability,
        session: object,
    ) -> object:
        with self._lock:
            if (
                self._worker is not worker
                or self._closed
                or self._generation_lost
                or session is None
                or session in self._sessions
            ):
                self._retired_sessions.add(session)
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)
            state.sessions.add(session)
            self._sessions[session] = state
            return session

    def _capability_key(self, kind: str, project_id: str, revision_id: str):
        return (kind, project_id, revision_id)

    @_serialized
    def open_candidate(
        self,
        *,
        store: LocalRevisionStore,
        base_head: ProjectHead,
        revision_id: str,
        lease: ProjectWriteLease,
        empty: bool,
    ) -> object:
        self._require_store(store)
        if (
            type(base_head) is not ProjectHead
            or type(revision_id) is not str
            or type(lease) is not ProjectWriteLease
            or type(empty) is not bool
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        worker = self._start_worker()
        key = self._capability_key("candidate", base_head.project_id, revision_id)
        with self._lock:
            if key in self._capabilities:
                raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        capability = self._call(
            worker,
            "bind_candidate",
            store=store,
            lease=lease,
            base_head=base_head,
            revision_id=revision_id,
        )
        state = _Capability(
            kind="candidate",
            key=(base_head.project_id, revision_id),
            value=capability,
            base_head=base_head,
            lease=lease,
        )
        with self._lock:
            if self._worker is not worker or key in self._capabilities:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)
            self._capabilities[key] = state
        try:
            session = self._call(
                worker,
                "create_empty" if empty else "load_fcstd",
                capability,
            )
            return self._register_session(
                worker=worker,
                state=state,
                session=session,
            )
        except Exception:
            self._release_empty_capability(worker, state)
            raise

    @_serialized
    def reload_candidate(
        self,
        *,
        store: LocalRevisionStore,
        base_head: ProjectHead,
        revision_id: str,
        lease: ProjectWriteLease,
    ) -> object:
        self._require_store(store)
        key = self._capability_key("candidate", base_head.project_id, revision_id)
        with self._lock:
            state = self._capabilities.get(key)
            worker = self._worker
            if (
                state is None
                or state.base_head != base_head
                or state.lease is not lease
                or worker is None
            ):
                raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        session = self._call(worker, "load_fcstd", state.value)
        return self._register_session(worker=worker, state=state, session=session)

    @_serialized
    def open_revision(
        self,
        *,
        store: LocalRevisionStore,
        revision: RevisionRef,
    ) -> object:
        self._require_store(store)
        if type(revision) is not RevisionRef:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        worker = self._start_worker()
        key = self._capability_key("revision", revision.project_id, revision.id)
        with self._lock:
            state = self._capabilities.get(key)
        if state is None:
            capability = self._call(
                worker,
                "bind_revision",
                store=store,
                revision=revision,
            )
            state = _Capability(
                kind="revision",
                key=(revision.project_id, revision.id),
                value=capability,
                revision=revision,
            )
            with self._lock:
                if self._worker is not worker:
                    raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)
                existing = self._capabilities.setdefault(key, state)
                if existing is not state:
                    raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        elif state.revision != revision:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            session = self._call(worker, "load_revision", state.value)
            return self._register_session(worker=worker, state=state, session=session)
        except Exception:
            self._release_empty_capability(worker, state)
            raise

    def create_empty(self, *, revision_id: str) -> object:
        del revision_id
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)

    def load_fcstd(self, path: Path) -> object:
        del path
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)

    @_serialized
    def checkpoint_fcstd(self, session: object, path: Path) -> None:
        with self._lock:
            state = self._sessions.get(session)
            worker = self._worker
        if state is None or state.kind != "candidate" or worker is None:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        assert state.base_head is not None and state.lease is not None
        try:
            trusted = self._store.candidate_model_path(
                state.base_head.project_id,
                state.key[1],
                state.lease,
            )
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if type(path) is not type(Path("/")) or path != trusted:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        self._call(
            worker,
            "checkpoint",
            session=session,
            candidate=state.value,
        )

    @_serialized
    def close(self, session: object) -> None:
        with self._lock:
            if session in self._retired_sessions:
                self._retired_sessions.remove(session)
                return
            state = self._sessions.get(session)
            worker = self._worker
        if state is None or worker is None:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            self._call(worker, "close_session", session)
        except ExecutorError:
            if self.generation_lost:
                return
            raise
        with self._lock:
            if self._sessions.pop(session, None) is not state:
                raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
            state.sessions.discard(session)
        self._release_empty_capability(worker, state)

    def _release_empty_capability(self, worker: object, state: _Capability) -> None:
        with self._lock:
            if state.sessions:
                return
            key = self._capability_key(state.kind, state.key[0], state.key[1])
            if self._capabilities.get(key) is not state:
                return
        method = "release_candidate" if state.kind == "candidate" else "release_revision"
        try:
            self._call(worker, method, state.value)
        except ExecutorError:
            if self.generation_lost:
                return
            raise
        with self._lock:
            if not state.sessions and self._capabilities.get(key) is state:
                del self._capabilities[key]

    def validate_program(self, program: ModelProgram) -> ValidatedProgram:
        try:
            return validate_model_program(program)
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None

    @_serialized
    def execute_program(
        self,
        *,
        program: ValidatedProgram,
        candidate: ActiveCandidate,
    ) -> tuple[NormalizedToolOutcome, ...]:
        if type(candidate) is not ActiveCandidate:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        with self._lock:
            state = self._sessions.get(candidate.binding.session)
            worker = self._worker
        if (
            state is None
            or state.kind != "candidate"
            or worker is None
            or state.key != (candidate.project_id, candidate.binding.revision_id)
            or state.base_head != candidate.base_head
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        validated = (
            program if type(program) is ValidatedProgram else self.validate_program(program)  # type: ignore[arg-type]
        )
        result = self._call(
            worker,
            "execute_program",
            program=validated,
            candidate=state.value,
            session=candidate.binding.session,
        )
        if type(result) is not tuple or not all(
            type(item) is NormalizedToolOutcome for item in result
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        return result

    @_serialized
    def export_step(
        self,
        *,
        candidate: CheckpointedCandidate,
        lease: ProjectWriteLease,
    ) -> None:
        if type(candidate) is not CheckpointedCandidate or type(lease) is not ProjectWriteLease:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        with self._lock:
            state = self._sessions.get(candidate.binding.session)
            worker = self._worker
        if (
            state is None
            or state.kind != "candidate"
            or worker is None
            or state.lease is not lease
            or state.key != (candidate.project_id, candidate.binding.revision_id)
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        self._call(
            worker,
            "export_step",
            session=candidate.binding.session,
            candidate=state.value,
        )

    def _open_directory(self, path: Path) -> tuple[int, str]:
        if type(path) is not type(Path("/")) or path.name in {"", ".", ".."}:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        parent = path.parent
        descriptor = -1
        try:
            descriptor = os.open(parent, _DIRECTORY_FLAGS)
            os.set_inheritable(descriptor, False)
            if os.get_inheritable(descriptor):
                raise OSError
            return descriptor, path.name
        except OSError:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None

    @_serialized
    def validate_import(self, path: Path) -> ValidatedImportEvidence:
        worker = self._start_worker()
        descriptor, name = self._open_directory(path)
        try:
            result = self._call(
                worker,
                "validate_import",
                directory_fd=descriptor,
                name=name,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if type(result) is not ValidatedImportEvidence:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        return result

    @_serialized
    def revalidate_normalized_import(self, path: Path) -> ValidatedImportEvidence:
        worker = self._start_worker()
        descriptor, name = self._open_directory(path)
        try:
            result = self._call(
                worker,
                "revalidate_normalized_import",
                directory_fd=descriptor,
                name=name,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if type(result) is not ValidatedImportEvidence:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        return result

    @_serialized
    def validate_materialization(
        self,
        *,
        fcstd: Path,
        step: Path,
    ) -> ValidatedMaterializationEvidence:
        if (
            type(fcstd) is not type(Path("/"))
            or type(step) is not type(Path("/"))
            or fcstd.parent != step.parent
            or fcstd.name != "model.FCStd"
            or step.name != "model.step"
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        worker = self._start_worker()
        descriptor, _name = self._open_directory(fcstd)
        try:
            result = self._call(
                worker,
                "validate_materialization",
                directory_fd=descriptor,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        if type(result) is not ValidatedMaterializationEvidence:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        return result

    def _observe_revision(
        self,
        worker: object,
        revision: RevisionRef,
        *,
        include_shape: bool,
    ) -> tuple[ShapeObservation | None, tuple[EntityObservation, ...]]:
        session = self.open_revision(store=self._store, revision=revision)
        with self._lock:
            state = self._sessions.get(session)
        if state is None:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            shape, entities = self._call(
                worker,
                "observe",
                session=session,
                capability=state.value,
            )
            if not include_shape:
                shape = None
            return shape, entities
        finally:
            self.close(session)

    @_serialized
    def collect_evidence(self, *, candidate: SealedCandidate) -> CandidateEvidence:
        if type(candidate) is not SealedCandidate:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        with self._lock:
            state = self._sessions.get(candidate.binding.session)
            worker = self._worker
        if (
            state is None
            or state.kind != "revision"
            or worker is None
            or state.key != (candidate.project_id, candidate.revision.id)
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        try:
            durable = self._store.load_revision(
                candidate.project_id,
                candidate.revision.id,
            )
            base = self._store.load_revision(
                candidate.project_id,
                candidate.base_head.revision_id,
            )
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if (
            durable != candidate.revision
            or durable.base_revision != base.id
            or base.manifest_sha256 != candidate.base_head.manifest_sha256
            or durable.model is None
            or len(durable.artifacts) != 1
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        model = durable.model
        step = durable.artifacts[0]
        if (model.name, model.format, step.name, step.format) != (
            "model.FCStd",
            "fcstd",
            "model.step",
            "step",
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        live_shape, live_entities = self._call(
            worker,
            "observe",
            session=candidate.binding.session,
            capability=state.value,
        )
        reloaded_shape, reloaded_entities = self._observe_revision(
            worker,
            durable,
            include_shape=True,
        )
        if live_shape is None or reloaded_shape != live_shape or reloaded_entities != live_entities:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        before_entities: tuple[EntityObservation, ...] = ()
        if base.model is not None:
            _unused_shape, before_entities = self._observe_revision(
                worker,
                base,
                include_shape=False,
            )
        try:
            final_durable = self._store.load_revision(candidate.project_id, durable.id)
            final_base = self._store.load_revision(candidate.project_id, base.id)
            preservations = _preservations(before_entities, live_entities)
        except ExecutorError:
            raise
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if final_durable != durable or final_base != base:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        snapshot = ObservationSnapshot(
            candidate_revision=durable.id,
            shapes=(live_shape,),
            artifacts=(
                ArtifactObservation(
                    target="export",
                    exists=True,
                    non_empty=True,
                    format="step",
                ),
                ArtifactObservation(
                    target="model",
                    exists=True,
                    non_empty=True,
                    format="fcstd",
                ),
            ),
            entities=live_entities,
            preservations=preservations,
        )
        artifacts = (
            TaskArtifactRef(
                id=model.id,
                name=model.name,
                format=model.format,
                sha256=model.sha256,
                size_bytes=model.size_bytes,
                candidate_revision=durable.id,
            ),
            TaskArtifactRef(
                id=step.id,
                name=step.name,
                format=step.format,
                sha256=step.sha256,
                size_bytes=step.size_bytes,
                candidate_revision=durable.id,
            ),
        )
        return CandidateEvidence(snapshot=snapshot, artifacts=artifacts)

    def terminate_generation(self) -> None:
        with self._lock:
            worker = self._worker
            start_in_flight = self._start_in_flight
            self._generation_lost = True
            self._retired_sessions.update(self._sessions)
            self._sessions.clear()
            self._capabilities.clear()
        if worker is None:
            if start_in_flight:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)
            return
        try:
            worker.terminate()
        except WorkerError as error:
            raise _fixed_error(_executor_code(error)) from None
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        if getattr(worker, "state", None) is not WorkerGenerationState.DEAD:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)
        with self._lock:
            if self._worker is worker:
                self._worker = None

    def close_generation(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            worker = self._worker
            self._worker = None
            self._retired_sessions.update(self._sessions)
            self._sessions.clear()
            self._capabilities.clear()
        if worker is None:
            return
        try:
            worker.close()
        except WorkerError as error:
            self._terminate_retired_worker(worker)
            if error.code in {
                WorkerErrorCode.CLOSED,
                WorkerErrorCode.GENERATION_LOST,
            }:
                return
            raise _fixed_error(_executor_code(error)) from None
        except Exception:
            self._terminate_retired_worker(worker)
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None


__all__ = ("WorkerCadExecutionPort",)
