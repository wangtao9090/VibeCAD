"""Nominal CadExecutionPort and trusted import-normalization tests."""

from __future__ import annotations

import hashlib
import os
import threading
import zipfile
from pathlib import Path

import pytest

import vibecad.execution.executor as executor_module
from vibecad.execution.candidate import (
    CadSnapshotPort,
    CheckpointedCandidate,
    SessionBinding,
)
from vibecad.execution.executor import (
    CandidateEvidence as ExecutorCandidateEvidence,
)
from vibecad.execution.executor import (
    ExecutorError,
    ExecutorErrorCode,
    InProcessCadExecutor,
)
from vibecad.execution.registry import ExecutionProfile
from vibecad.execution.revisions import LocalRevisionStore, ProjectHead, RevisionRef
from vibecad.execution.worker_port import WorkerCadExecutionPort
from vibecad.interaction.cad import (
    MAX_ADMITTED_CREATED_OBJECTS,
    MAX_ADMITTED_RESULT_BYTES,
    MAX_ADMITTED_RUNTIME_MS,
    CadCapabilityStatus,
    CadExecutionPort,
    CadProfileCapability,
    CandidateEvidence,
    ValidatedImportEvidence,
    ValidatedMaterializationEvidence,
)
from vibecad.validation import EntityObservation, EntityParameterObservation
from vibecad.worker.generation import (
    WorkerError,
    WorkerErrorCode,
    WorkerGenerationState,
)
from vibecad.workflow.lease import ProjectWriteLease

_PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
_BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
_CANDIDATE_REVISION = "revision_11111111111111111111111111111111"
_DIGEST = "a" * 64
_VALID_STEP = b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


def _store() -> LocalRevisionStore:
    return object.__new__(LocalRevisionStore)


def _fcstd(path: Path, document: str = "<Document />") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Document.xml", document)


class _StepExportShape:
    def __init__(self, writer) -> None:
        self._writer = writer
        self.calls: list[Path] = []

    def exportStep(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
        target = Path(path)
        self.calls.append(target)
        self._writer(target)


class _StepExportSession:
    def __init__(self, shape: _StepExportShape) -> None:
        self._shape = shape
        self.shape_calls = 0

    def get_assembly_shape(self) -> _StepExportShape:
        self.shape_calls += 1
        return self._shape


def _step_export_candidate(
    tmp_path: Path,
    session: _StepExportSession,
) -> CheckpointedCandidate:
    return CheckpointedCandidate(
        project_id=_PROJECT_ID,
        base_head=ProjectHead(
            project_id=_PROJECT_ID,
            generation=1,
            revision_id=_BASE_REVISION,
            manifest_sha256=_DIGEST,
        ),
        binding=SessionBinding(
            project_id=_PROJECT_ID,
            revision_id=_CANDIDATE_REVISION,
            session=session,
        ),
        model_path=tmp_path / "model.FCStd",
        step_path=tmp_path / "model.step",
    )


def _step_export_lease() -> ProjectWriteLease:
    lease = object.__new__(ProjectWriteLease)
    object.__setattr__(lease, "project_id", _PROJECT_ID)
    object.__setattr__(lease, "released", False)
    return lease


def _install_step_path(
    monkeypatch: pytest.MonkeyPatch,
    candidate: CheckpointedCandidate,
) -> None:
    monkeypatch.setattr(
        LocalRevisionStore,
        "candidate_artifact_path",
        lambda *args, **kwargs: candidate.step_path,
    )


def _step_placeholder(path: Path) -> None:
    path.touch(mode=0o600)
    path.chmod(0o600)


def _step_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_mode,
        value.st_nlink,
    )


def test_nominal_port_extends_snapshot_port_and_reports_only_headless_verified() -> None:
    executor = InProcessCadExecutor(store=_store())

    assert isinstance(executor, CadExecutionPort)
    assert isinstance(executor, CadSnapshotPort)
    assert issubclass(InProcessCadExecutor, CadExecutionPort)
    assert executor.execution_profile is ExecutionProfile.HEADLESS
    assert executor.capabilities == (
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
    assert ExecutorCandidateEvidence is CandidateEvidence
    assert (
        MAX_ADMITTED_RUNTIME_MS,
        MAX_ADMITTED_CREATED_OBJECTS,
        MAX_ADMITTED_RESULT_BYTES,
    ) == (30_000, 1, 262_144)


def test_worker_port_is_lazy_and_releases_revision_capability_after_last_session() -> None:
    store = _store()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    capability = object()
    session = object()

    class Worker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object | None]] = []

        def bind_revision(self, *, store, revision):
            assert store is not None
            self.calls.append(("bind_revision", revision))
            return capability

        def load_revision(self, value):
            assert value is capability
            self.calls.append(("load_revision", value))
            return session

        def close_session(self, value):
            assert value is session
            self.calls.append(("close_session", value))

        def release_revision(self, value):
            assert value is capability
            self.calls.append(("release_revision", value))

        def close(self):
            self.calls.append(("close", None))

    worker = Worker()
    starts: list[Path] = []
    port = WorkerCadExecutionPort(
        store=store,
        worker_factory=lambda *, source_root: (
            starts.append(source_root),
            worker,
        )[1],
    )

    assert starts == []
    assert port.open_revision(store=store, revision=revision) is session
    assert len(starts) == 1
    port.close(session)
    port.close_generation()

    assert [name for name, _value in worker.calls] == [
        "bind_revision",
        "load_revision",
        "close_session",
        "release_revision",
        "close",
    ]


def test_worker_port_keeps_generation_after_typed_cad_failure() -> None:
    store = _store()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    capability = object()
    session = object()

    class Worker:
        def __init__(self) -> None:
            self.load_calls = 0
            self.release_calls = 0

        def bind_revision(self, *, store, revision):
            del store, revision
            return capability

        def load_revision(self, value):
            assert value is capability
            self.load_calls += 1
            if self.load_calls == 1:
                raise WorkerError(WorkerErrorCode.CAD_FAILURE)
            return session

        def close_session(self, value):
            assert value is session

        def release_revision(self, value):
            assert value is capability
            self.release_calls += 1

        def close(self):
            return None

    worker = Worker()
    starts = 0

    def start_worker(*, source_root):
        nonlocal starts
        del source_root
        starts += 1
        return worker

    port = WorkerCadExecutionPort(store=store, worker_factory=start_worker)
    with pytest.raises(ExecutorError) as caught:
        port.open_revision(store=store, revision=revision)
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert port.generation_lost is False

    assert port.open_revision(store=store, revision=revision) is session
    port.close(session)
    port.close_generation()
    assert starts == 1
    assert worker.release_calls == 2


@pytest.mark.parametrize(
    "failure",
    (
        WorkerError(WorkerErrorCode.GENERATION_LOST),
        RuntimeError("unexpected proxy failure"),
    ),
)
def test_worker_port_retires_generation_after_transport_or_unknown_failure(
    failure: Exception,
) -> None:
    store = _store()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    capability = object()

    class Worker:
        def __init__(self) -> None:
            self.terminate_calls = 0

        def bind_revision(self, *, store, revision):
            del store, revision
            return capability

        def load_revision(self, value):
            assert value is capability
            raise failure

        def terminate(self):
            self.terminate_calls += 1

    worker = Worker()
    starts = 0

    def start_worker(*, source_root):
        nonlocal starts
        del source_root
        starts += 1
        return worker

    port = WorkerCadExecutionPort(store=store, worker_factory=start_worker)
    with pytest.raises(ExecutorError) as caught:
        port.open_revision(store=store, revision=revision)

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert port.generation_lost is True
    with pytest.raises(ExecutorError) as retired:
        port.open_revision(store=store, revision=revision)
    assert retired.value.code is ExecutorErrorCode.CAD_FAILURE
    assert starts == 1
    assert worker.terminate_calls == 1


def test_worker_port_terminates_after_unknown_generation_close_failure() -> None:
    store = _store()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    capability = object()
    session = object()

    class Worker:
        def __init__(self) -> None:
            self.terminate_calls = 0

        def bind_revision(self, *, store, revision):
            del store, revision
            return capability

        def load_revision(self, value):
            assert value is capability
            return session

        def close(self):
            raise RuntimeError("unknown close failure")

        def terminate(self):
            self.terminate_calls += 1

    worker = Worker()
    port = WorkerCadExecutionPort(
        store=store,
        worker_factory=lambda *, source_root: worker,
    )
    assert port.open_revision(store=store, revision=revision) is session

    with pytest.raises(ExecutorError) as caught:
        port.close_generation()

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert worker.terminate_calls == 1
    with pytest.raises(ExecutorError) as closed:
        port.open_revision(store=store, revision=revision)
    assert closed.value.code is ExecutorErrorCode.CAD_FAILURE


def test_worker_port_serializes_capability_binding_and_last_session_release() -> None:
    store = _store()
    head = ProjectHead(
        project_id=_PROJECT_ID,
        generation=0,
        revision_id=_BASE_REVISION,
        manifest_sha256=_DIGEST,
    )
    lease = object.__new__(ProjectWriteLease)
    candidate_capability = object()
    candidate_session = object()
    bind_entered = threading.Event()
    release_bind = threading.Event()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    revision_capability = object()
    revision_sessions = (object(), object())
    first_close_entered = threading.Event()
    release_first_close = threading.Event()

    class Worker:
        def __init__(self) -> None:
            self.candidate_bind_calls = 0
            self.revision_load_calls = 0
            self.revision_release_calls = 0
            self.close_session_calls = 0

        def bind_candidate(self, **_kwargs):
            self.candidate_bind_calls += 1
            bind_entered.set()
            assert release_bind.wait(timeout=3)
            return candidate_capability

        def create_empty(self, value):
            assert value is candidate_capability
            return candidate_session

        def release_candidate(self, value):
            assert value is candidate_capability

        def bind_revision(self, **_kwargs):
            return revision_capability

        def load_revision(self, value):
            assert value is revision_capability
            session = revision_sessions[self.revision_load_calls]
            self.revision_load_calls += 1
            return session

        def close_session(self, value):
            self.close_session_calls += 1
            if value is revision_sessions[0]:
                first_close_entered.set()
                assert release_first_close.wait(timeout=3)

        def release_revision(self, value):
            assert value is revision_capability
            self.revision_release_calls += 1

        def close(self):
            return None

    worker = Worker()
    port = WorkerCadExecutionPort(
        store=store,
        worker_factory=lambda *, source_root: worker,
    )
    candidate_results: list[object] = []
    candidate_errors: list[ExecutorError] = []

    def open_candidate() -> None:
        try:
            candidate_results.append(
                port.open_candidate(
                    store=store,
                    base_head=head,
                    revision_id=_CANDIDATE_REVISION,
                    lease=lease,
                    empty=True,
                )
            )
        except ExecutorError as error:
            candidate_errors.append(error)

    first = threading.Thread(target=open_candidate)
    second = threading.Thread(target=open_candidate)
    first.start()
    assert bind_entered.wait(timeout=2)
    second.start()
    release_bind.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert candidate_results == [candidate_session]
    assert [error.code for error in candidate_errors] == [ExecutorErrorCode.INVALID_CANDIDATE]
    assert worker.candidate_bind_calls == 1
    port.close(candidate_session)

    first_revision = port.open_revision(store=store, revision=revision)
    second_revision = port.open_revision(store=store, revision=revision)
    closers = [
        threading.Thread(target=port.close, args=(first_revision,)),
        threading.Thread(target=port.close, args=(second_revision,)),
    ]
    closers[0].start()
    assert first_close_entered.wait(timeout=2)
    closers[1].start()
    release_first_close.set()
    for closer in closers:
        closer.join(timeout=3)

    assert all(not closer.is_alive() for closer in closers)
    assert worker.revision_release_calls == 1
    port.close_generation()


def test_worker_port_cancellation_does_not_wait_for_first_generation_start() -> None:
    store = _store()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    factory_entered = threading.Event()
    release_factory = threading.Event()
    cancellation_returned = threading.Event()
    errors: list[ExecutorError] = []
    cancellation_errors: list[ExecutorError] = []

    class Worker:
        def __init__(self) -> None:
            self.state = WorkerGenerationState.READY
            self.terminate_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.state = WorkerGenerationState.DEAD

    worker = Worker()

    def start_worker(*, source_root):
        del source_root
        factory_entered.set()
        assert release_factory.wait(timeout=3)
        return worker

    port = WorkerCadExecutionPort(store=store, worker_factory=start_worker)

    def open_revision() -> None:
        try:
            port.open_revision(store=store, revision=revision)
        except ExecutorError as error:
            errors.append(error)

    opener = threading.Thread(target=open_revision)
    opener.start()
    assert factory_entered.wait(timeout=2)

    def cancel_generation() -> None:
        try:
            port.terminate_generation()
        except ExecutorError as error:
            cancellation_errors.append(error)
        finally:
            cancellation_returned.set()

    canceller = threading.Thread(target=cancel_generation)
    canceller.start()

    assert cancellation_returned.wait(timeout=1)
    release_factory.set()
    opener.join(timeout=3)
    canceller.join(timeout=3)

    assert not opener.is_alive()
    assert not canceller.is_alive()
    assert [error.code for error in errors] == [ExecutorErrorCode.CAD_FAILURE]
    assert [error.code for error in cancellation_errors] == [ExecutorErrorCode.CAD_FAILURE]
    assert worker.terminate_calls == 1
    assert worker.state is WorkerGenerationState.DEAD
    assert port.generation_lost is True


@pytest.mark.parametrize(
    "incomplete_state",
    (WorkerGenerationState.READY, WorkerGenerationState.CLEANUP_REQUIRED),
)
def test_worker_port_rejects_silent_incomplete_termination_and_retries_handle(
    incomplete_state: WorkerGenerationState,
) -> None:
    class Worker:
        def __init__(self) -> None:
            self.state = WorkerGenerationState.READY
            self.terminate_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.state = incomplete_state

    worker = Worker()
    port = WorkerCadExecutionPort(
        store=_store(),
        worker_factory=lambda *, source_root: worker,
    )
    assert port._start_worker() is worker

    for expected_calls in (1, 2):
        with pytest.raises(ExecutorError) as caught:
            port.terminate_generation()
        assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
        assert worker.terminate_calls == expected_calls
        assert worker.state is incomplete_state


def test_worker_port_retries_termination_after_exception_until_dead() -> None:
    class Worker:
        def __init__(self) -> None:
            self.state = WorkerGenerationState.READY
            self.terminate_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            if self.terminate_calls == 1:
                raise RuntimeError("first termination failed")
            self.state = WorkerGenerationState.DEAD

    worker = Worker()
    port = WorkerCadExecutionPort(
        store=_store(),
        worker_factory=lambda *, source_root: worker,
    )
    assert port._start_worker() is worker

    with pytest.raises(ExecutorError) as caught:
        port.terminate_generation()
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE

    port.terminate_generation()
    assert worker.terminate_calls == 2
    assert worker.state is WorkerGenerationState.DEAD


def test_worker_port_retains_call_loss_handle_until_dead_termination() -> None:
    store = _store()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    capability = object()

    class Worker:
        def __init__(self) -> None:
            self.state = WorkerGenerationState.READY
            self.terminate_calls = 0

        def bind_revision(self, *, store, revision):
            del store, revision
            return capability

        def load_revision(self, value):
            assert value is capability
            raise WorkerError(WorkerErrorCode.GENERATION_LOST)

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.state = (
                WorkerGenerationState.CLEANUP_REQUIRED
                if self.terminate_calls == 1
                else WorkerGenerationState.DEAD
            )

    worker = Worker()
    port = WorkerCadExecutionPort(
        store=store,
        worker_factory=lambda *, source_root: worker,
    )

    with pytest.raises(ExecutorError) as caught:
        port.open_revision(store=store, revision=revision)
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert port.generation_lost is True
    assert worker.terminate_calls == 1
    assert worker.state is WorkerGenerationState.CLEANUP_REQUIRED

    port.terminate_generation()
    assert worker.terminate_calls == 2
    assert worker.state is WorkerGenerationState.DEAD


def test_worker_port_start_race_requires_later_dead_proof_and_never_restarts() -> None:
    store = _store()
    revision = RevisionRef(
        id=_BASE_REVISION,
        project_id=_PROJECT_ID,
        base_revision=None,
        manifest_sha256=_DIGEST,
        model=None,
        artifacts=(),
    )
    factory_entered = threading.Event()
    release_factory = threading.Event()
    opener_errors: list[ExecutorError] = []

    class Worker:
        def __init__(self) -> None:
            self.state = WorkerGenerationState.READY
            self.terminate_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.state = (
                WorkerGenerationState.CLEANUP_REQUIRED
                if self.terminate_calls == 1
                else WorkerGenerationState.DEAD
            )

    worker = Worker()
    starts = 0

    def start_worker(*, source_root):
        nonlocal starts
        del source_root
        starts += 1
        factory_entered.set()
        assert release_factory.wait(timeout=3)
        return worker

    port = WorkerCadExecutionPort(store=store, worker_factory=start_worker)

    def open_revision() -> None:
        try:
            port.open_revision(store=store, revision=revision)
        except ExecutorError as error:
            opener_errors.append(error)

    opener = threading.Thread(target=open_revision)
    opener.start()
    assert factory_entered.wait(timeout=2)

    with pytest.raises(ExecutorError) as caught:
        port.terminate_generation()
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE

    release_factory.set()
    opener.join(timeout=3)
    assert not opener.is_alive()
    assert [error.code for error in opener_errors] == [ExecutorErrorCode.CAD_FAILURE]
    assert worker.terminate_calls == 1
    assert worker.state is WorkerGenerationState.CLEANUP_REQUIRED

    port.terminate_generation()
    assert worker.terminate_calls == 2
    assert worker.state is WorkerGenerationState.DEAD
    with pytest.raises(ExecutorError) as retired:
        port.open_revision(store=store, revision=revision)
    assert retired.value.code is ExecutorErrorCode.CAD_FAILURE
    assert starts == 1


def test_step_export_accepts_only_exact_owned_empty_placeholder_and_preserves_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shape = _StepExportShape(lambda path: path.write_bytes(_VALID_STEP))
    session = _StepExportSession(shape)
    candidate = _step_export_candidate(tmp_path, session)
    _step_placeholder(candidate.step_path)
    before = os.lstat(candidate.step_path)
    _install_step_path(monkeypatch, candidate)

    InProcessCadExecutor(store=_store()).export_step(
        candidate=candidate,
        lease=_step_export_lease(),
    )

    after = os.lstat(candidate.step_path)
    assert _step_identity(after) == _step_identity(before)
    assert 0 < after.st_size <= executor_module._MAX_ARTIFACT_BYTES
    assert candidate.step_path.read_bytes() == _VALID_STEP
    assert session.shape_calls == 1
    assert shape.calls == [candidate.step_path]


def test_step_export_rejects_missing_placeholder_before_freecad(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shape = _StepExportShape(lambda path: path.write_bytes(_VALID_STEP))
    session = _StepExportSession(shape)
    candidate = _step_export_candidate(tmp_path, session)
    _install_step_path(monkeypatch, candidate)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(
            candidate=candidate,
            lease=_step_export_lease(),
        )

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.shape_calls == 0
    assert shape.calls == []
    assert not candidate.step_path.exists()


@pytest.mark.parametrize(
    "entry_kind",
    ("nonempty", "symlink", "hardlink", "directory", "wrong_mode", "wrong_owner"),
)
def test_step_export_rejects_non_placeholder_entry_before_freecad(
    entry_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shape = _StepExportShape(lambda path: path.write_bytes(_VALID_STEP))
    session = _StepExportSession(shape)
    candidate = _step_export_candidate(tmp_path, session)
    outside = tmp_path / "outside.step"
    outside.write_bytes(b"outside-sentinel")
    outside.chmod(0o600)
    if entry_kind == "nonempty":
        candidate.step_path.write_bytes(b"existing-sentinel")
        candidate.step_path.chmod(0o600)
    elif entry_kind == "symlink":
        candidate.step_path.symlink_to(outside)
    elif entry_kind == "hardlink":
        os.link(outside, candidate.step_path)
    elif entry_kind == "directory":
        candidate.step_path.mkdir(mode=0o700)
    else:
        _step_placeholder(candidate.step_path)
        if entry_kind == "wrong_mode":
            candidate.step_path.chmod(0o644)
        else:
            monkeypatch.setattr(executor_module.os, "geteuid", lambda: os.getuid() + 1)
    _install_step_path(monkeypatch, candidate)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(
            candidate=candidate,
            lease=_step_export_lease(),
        )

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.shape_calls == 0
    assert shape.calls == []
    assert outside.read_bytes() == b"outside-sentinel"


@pytest.mark.parametrize("drift", ("replace", "mode", "hardlink"))
def test_step_export_rejects_placeholder_identity_drift_after_freecad(
    drift: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def write_with_drift(path: Path) -> None:
        if drift == "replace":
            replacement = path.with_name("replacement.step")
            replacement.write_bytes(_VALID_STEP)
            replacement.chmod(0o600)
            os.replace(replacement, path)
            return
        path.write_bytes(_VALID_STEP)
        if drift == "mode":
            path.chmod(0o644)
        else:
            os.link(path, path.with_name("second-link.step"))

    shape = _StepExportShape(write_with_drift)
    session = _StepExportSession(shape)
    candidate = _step_export_candidate(tmp_path, session)
    _step_placeholder(candidate.step_path)
    before = os.lstat(candidate.step_path)
    _install_step_path(monkeypatch, candidate)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(
            candidate=candidate,
            lease=_step_export_lease(),
        )

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.shape_calls == 1
    assert shape.calls == [candidate.step_path]
    after = os.lstat(candidate.step_path)
    if drift == "replace":
        assert (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        assert candidate.step_path.read_bytes() == _VALID_STEP


@pytest.mark.parametrize("failure", ("empty", "invalid_envelope", "over_bound"))
def test_step_export_rejects_invalid_placeholder_output(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = {
        "empty": b"",
        "invalid_envelope": b"not-step",
        "over_bound": _VALID_STEP,
    }[failure]
    shape = _StepExportShape(lambda path: path.write_bytes(payload))
    session = _StepExportSession(shape)
    candidate = _step_export_candidate(tmp_path, session)
    _step_placeholder(candidate.step_path)
    _install_step_path(monkeypatch, candidate)
    if failure == "over_bound":
        monkeypatch.setattr(executor_module, "_MAX_ARTIFACT_BYTES", len(_VALID_STEP) - 1)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(
            candidate=candidate,
            lease=_step_export_lease(),
        )

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.shape_calls == 1
    assert shape.calls == [candidate.step_path]


def test_validated_import_evidence_is_exact_and_immutable() -> None:
    evidence = ValidatedImportEvidence(sha256="a" * 64, size_bytes=1)
    assert evidence.sha256 == "a" * 64
    with pytest.raises((AttributeError, TypeError)):
        evidence.size_bytes = 2  # type: ignore[misc]
    with pytest.raises(ValueError):
        ValidatedImportEvidence(sha256="A" * 64, size_bytes=1)
    with pytest.raises(ValueError):
        ValidatedImportEvidence(sha256="a" * 64, size_bytes=True)  # type: ignore[arg-type]


def test_validate_materialization_is_read_only_and_returns_both_byte_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def __init__(self) -> None:
            self.recompute_calls = 0

        def recompute(self) -> None:
            self.recompute_calls += 1

    class Session:
        def __init__(self) -> None:
            self.doc = Document()
            self.closed = 0

        def load_document(self, path: Path) -> object:
            assert path.name == "model.FCStd"
            return self.doc

        def close_document(self) -> None:
            self.closed += 1

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(executor_module, "_shape_observation", lambda value: object())
    monkeypatch.setattr(executor_module, "_entity_observations", lambda value: ())
    forbidden = []
    for name in ("checkpoint_fcstd", "export_step", "validate_import"):
        monkeypatch.setattr(
            InProcessCadExecutor,
            name,
            lambda *args, _name=name, **kwargs: forbidden.append(_name),
        )
    fcstd = tmp_path / "model.FCStd"
    step = tmp_path / "model.step"
    _fcstd(fcstd)
    step.write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    before = (fcstd.read_bytes(), step.read_bytes())

    evidence = InProcessCadExecutor(store=_store()).validate_materialization(
        fcstd=fcstd,
        step=step,
    )

    assert evidence == ValidatedMaterializationEvidence(
        fcstd_sha256=hashlib.sha256(before[0]).hexdigest(),
        fcstd_size_bytes=len(before[0]),
        step_sha256=hashlib.sha256(before[1]).hexdigest(),
        step_size_bytes=len(before[1]),
    )
    assert (fcstd.read_bytes(), step.read_bytes()) == before
    assert session.doc.recompute_calls == 1
    assert session.closed == 1
    assert forbidden == []


def test_validated_materialization_evidence_is_exact_and_immutable() -> None:
    evidence = ValidatedMaterializationEvidence(
        fcstd_sha256="a" * 64,
        fcstd_size_bytes=1,
        step_sha256="b" * 64,
        step_size_bytes=2,
    )
    with pytest.raises((AttributeError, TypeError)):
        evidence.step_size_bytes = 3  # type: ignore[misc]
    with pytest.raises(ValueError):
        ValidatedMaterializationEvidence(
            fcstd_sha256="A" * 64,
            fcstd_size_bytes=1,
            step_sha256="b" * 64,
            step_size_bytes=2,
        )
    with pytest.raises(ValueError):
        ValidatedMaterializationEvidence(
            fcstd_sha256="a" * 64,
            fcstd_size_bytes=True,  # type: ignore[arg-type]
            step_sha256="b" * 64,
            step_size_bytes=2,
        )


def test_validate_materialization_load_failure_closes_and_preserves_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Session:
        def __init__(self) -> None:
            self.closed = 0

        def load_document(self, path: Path) -> object:
            del path
            raise RuntimeError("private load failure")

        def close_document(self) -> None:
            self.closed += 1

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    fcstd = tmp_path / "model.FCStd"
    step = tmp_path / "model.step"
    _fcstd(fcstd)
    step.write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    before = (fcstd.read_bytes(), step.read_bytes())

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_materialization(fcstd=fcstd, step=step)

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert session.closed == 1
    assert (fcstd.read_bytes(), step.read_bytes()) == before


def test_validate_materialization_recompute_and_close_failures_preserve_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def __init__(self) -> None:
            self.fail_recompute = True

        def recompute(self) -> None:
            if self.fail_recompute:
                raise RuntimeError("private recompute failure")

    class Session:
        def __init__(self) -> None:
            self.doc = Document()
            self.fail_close = False
            self.closed = 0

        def load_document(self, path: Path) -> object:
            del path
            return self.doc

        def close_document(self) -> None:
            self.closed += 1
            if self.fail_close:
                raise RuntimeError("private close failure")

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(executor_module, "_shape_observation", lambda value: object())
    monkeypatch.setattr(executor_module, "_entity_observations", lambda value: ())
    fcstd = tmp_path / "model.FCStd"
    step = tmp_path / "model.step"
    _fcstd(fcstd)
    step.write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    before = (fcstd.read_bytes(), step.read_bytes())

    with pytest.raises(ExecutorError) as recompute:
        InProcessCadExecutor(store=_store()).validate_materialization(fcstd=fcstd, step=step)
    assert recompute.value.code is ExecutorErrorCode.CAD_FAILURE
    assert session.closed == 1
    assert (fcstd.read_bytes(), step.read_bytes()) == before

    session.doc.fail_recompute = False
    session.fail_close = True
    with pytest.raises(ExecutorError) as close:
        InProcessCadExecutor(store=_store()).validate_materialization(fcstd=fcstd, step=step)
    assert close.value.code is ExecutorErrorCode.CAD_FAILURE
    assert session.closed == 2
    assert (fcstd.read_bytes(), step.read_bytes()) == before


def test_validate_materialization_detects_same_byte_inode_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fcstd = tmp_path / "model.FCStd"
    step = tmp_path / "model.step"
    _fcstd(fcstd)
    step.write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")
    original = fcstd.read_bytes()

    class Document:
        def recompute(self) -> None:
            replacement = fcstd.with_suffix(".replacement")
            replacement.write_bytes(original)
            replacement.chmod(0o600)
            replacement.replace(fcstd)

    class Session:
        doc = Document()
        closed = 0

        def load_document(self, path: Path) -> object:
            del path
            return self.doc

        def close_document(self) -> None:
            self.closed += 1

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(executor_module, "_shape_observation", lambda value: object())
    monkeypatch.setattr(executor_module, "_entity_observations", lambda value: ())

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_materialization(fcstd=fcstd, step=step)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert session.closed == 1


def test_validate_materialization_rejects_invalid_layout_before_cad(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []
    monkeypatch.setattr(executor_module, "_Session", lambda: calls.append("session"))
    fcstd = tmp_path / "other.FCStd"
    step = tmp_path / "model.step"
    _fcstd(fcstd)
    step.write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_materialization(fcstd=fcstd, step=step)

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert calls == []


def test_validate_import_normalizes_box_identity_checkpoints_and_reloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    saved_identities: list[object] = []
    made: list[ImportSession] = []

    class ImportObject:
        Name = "Box"
        TypeId = "Part::Box"
        geometry_marker = (10.0, 20.0, 30.0)

    class ImportDocument:
        def __init__(self, obj: ImportObject) -> None:
            self.Objects = (obj,)
            self.recompute_calls = 0
            self.transactions: list[str] = []
            self.commits = 0
            self.aborts = 0

        def recompute(self) -> None:
            self.recompute_calls += 1

        def openTransaction(self, name: str) -> None:  # noqa: N802
            self.transactions.append(name)

        def commitTransaction(self) -> None:  # noqa: N802
            self.commits += 1

        def abortTransaction(self) -> None:  # noqa: N802
            self.aborts += 1

        def saveCopy(self, path: str) -> None:  # noqa: N802
            _fcstd(Path(path))

    class ImportSession:
        freecad_version = (1, 1)

        def __init__(self) -> None:
            self.obj = ImportObject()
            self.doc = ImportDocument(self.obj)
            self.identities: list[object] = []
            self.close_calls = 0
            self.persist_calls = 0
            made.append(self)

        def load_document(self, path: Path) -> object:
            del path
            self.identities = list(saved_identities)
            return self.doc

        def list_object_identities(self) -> tuple[tuple[object, object], ...]:
            return tuple((self.obj, identity) for identity in self.identities)

        def attach_object_identity(self, obj: object, identity: object) -> object:
            assert obj is self.obj
            self.identities.append(identity)
            saved_identities[:] = self.identities
            return identity

        def read_object_identity(self, obj: object) -> object:
            assert obj is self.obj
            return self.identities[0]

        def persist_state(self) -> None:
            self.persist_calls += 1
            saved_identities[:] = self.identities

        def close_document(self) -> None:
            self.close_calls += 1

    def observations(session: ImportSession) -> tuple[object, ...]:
        return tuple(
            (
                identity.object_id,
                identity.feature_id,
                identity.object_type,
                session.obj.geometry_marker,
            )
            for _, identity in session.list_object_identities()
        )

    monkeypatch.setattr(executor_module, "_Session", ImportSession)
    monkeypatch.setattr(executor_module, "_entity_observations", observations)
    staging = tmp_path / "bootstrap.FCStd"
    _fcstd(staging)

    evidence = InProcessCadExecutor(store=_store()).validate_import(staging)

    assert type(evidence) is ValidatedImportEvidence
    raw = staging.read_bytes()
    assert evidence == ValidatedImportEvidence(
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )
    assert len(made) == 2
    assert made[0].doc.recompute_calls >= 1
    assert made[0].doc.transactions == ["VibeCAD Import Identity Normalization"]
    assert made[0].doc.commits == 1
    assert made[0].persist_calls == 1
    assert made[0].close_calls == 1
    assert made[1].doc.recompute_calls >= 1
    assert made[1].close_calls == 1
    identity = saved_identities[0]
    assert identity.object_id.startswith("object_")
    assert identity.feature_id.startswith("feature_")
    assert identity.object_type == "Part::Box"
    assert identity.provenance.source.value == "imported"
    assert identity.provenance.operation_id is None


def test_validate_import_rejects_empty_document_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class EmptyDocument:
        Objects: tuple[object, ...] = ()

        def recompute(self) -> None:
            return None

    class EmptySession:
        def __init__(self) -> None:
            self.doc = EmptyDocument()
            self.close_calls = 0

        def load_document(self, path: Path) -> object:
            del path
            return self.doc

        def list_object_identities(self) -> tuple[object, ...]:
            return ()

        def close_document(self) -> None:
            self.close_calls += 1

    made = EmptySession()
    monkeypatch.setattr(executor_module, "_Session", lambda: made)
    staging = tmp_path / "empty.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert made.close_calls == 1


def test_validate_import_rejects_unobserved_app_featurepython_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ImportObject:
        TypeId = "Part::Box"

    class UnobservedObject:
        TypeId = "App::FeaturePython"

    class Document:
        Objects = (ImportObject(), UnobservedObject())

        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

        def list_object_identities(self) -> tuple[object, ...]:
            return ()

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )

    def observations(session: object) -> tuple[str, ...]:
        executor_module._import_objects(session)
        return ("box-observation",)

    monkeypatch.setattr(executor_module, "_normalize_import_identities", observations)
    monkeypatch.setattr(executor_module, "_validated_import_observations", observations)
    staging = tmp_path / "unobserved-app-object.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert len(closed) == 1


def test_validate_import_rejects_reload_identity_or_geometry_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: ("identity-a", "geometry-a"),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: ("identity-a", "geometry-b"),
    )
    staging = tmp_path / "drift.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert len(closed) == 2


def test_validate_import_accepts_bounded_occ_reload_noise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    def observed(center_y: float) -> tuple[EntityObservation, ...]:
        return (
            EntityObservation(
                object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                feature_id="feature_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                object_type="Part::Cylinder",
                semantic_role="primitive",
                provenance={"source": "imported", "operation_id": None},
                placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
                volume_mm3=62.83185307179586,
                area_mm2=87.96459430051421,
                bbox_mm=(4.0, 4.0, 5.0),
                center_of_mass_mm=(30.0, center_y, 2.5),
                valid_shape=True,
                solid_count=1,
            ),
        )

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: observed(4.376390559497447e-17),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: observed(6.91504145611255e-17),
    )
    staging = tmp_path / "occ-noise.FCStd"
    _fcstd(staging)

    evidence = InProcessCadExecutor(store=_store()).validate_import(staging)

    assert type(evidence) is ValidatedImportEvidence
    assert len(closed) == 2


def test_validate_import_rejects_direct_parameter_drift_within_geometry_tolerance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    def observed(length: float) -> tuple[EntityObservation, ...]:
        return (
            EntityObservation(
                object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                feature_id="feature_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                object_type="Part::Box",
                semantic_role="primitive",
                provenance={"source": "imported", "operation_id": None},
                placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
                parameters=(
                    EntityParameterObservation(
                        name="length",
                        value=length,
                        unit="mm",
                    ),
                ),
                volume_mm3=1.0,
                area_mm2=6.0,
                bbox_mm=(1.0, 1.0, 1.0),
                center_of_mass_mm=(0.5, 0.5, 0.5),
                valid_shape=True,
                solid_count=1,
            ),
        )

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: observed(1.0),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: observed(1.0000000005),
    )
    staging = tmp_path / "parameter-drift.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert len(closed) == 2


def test_validate_import_rejects_large_coordinate_translation_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    def observed(x: float) -> tuple[EntityObservation, ...]:
        return (
            EntityObservation(
                object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                feature_id="feature_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                object_type="Part::Box",
                semantic_role="primitive",
                provenance={"source": "imported", "operation_id": None},
                placement=(x, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
                volume_mm3=1.0,
                area_mm2=6.0,
                bbox_mm=(1.0, 1.0, 1.0),
                center_of_mass_mm=(x + 0.5, 0.5, 0.5),
                valid_shape=True,
                solid_count=1,
            ),
        )

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(InProcessCadExecutor, "load_fcstd", lambda self, path: next(sessions))
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: observed(1_000_000_000_000.0),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: observed(1_000_000_000_001.0),
    )
    staging = tmp_path / "large-placement-drift.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert len(closed) == 2


def test_validate_import_rejects_staging_swap_during_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    staging = tmp_path / "swap.FCStd"
    _fcstd(staging)
    sessions = iter((Session(), Session()))
    close_calls = 0

    def close(self: object, session: object) -> None:
        nonlocal close_calls
        del self, session
        close_calls += 1
        if close_calls == 2:
            _fcstd(staging, "<Swapped />")

    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(InProcessCadExecutor, "close", close)
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: ("identity", "geometry"),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: ("identity", "geometry"),
    )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert close_calls == 2


def test_revalidate_normalized_import_is_read_only_and_orders_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    trace: list[str] = []

    class Document:
        def recompute(self) -> None:
            trace.append("recompute")

    class Session:
        doc = Document()

        def load_document(self, path: Path) -> object:
            assert path == Path("normalized.FCStd")
            trace.append("load")
            return self.doc

        def close_document(self) -> None:
            trace.append("close")

        def persist_state(self) -> None:
            raise AssertionError("revalidation must not persist")

    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    _fcstd(artifact)
    raw = artifact.read_bytes()
    before = os.lstat(artifact)
    real_read = executor_module._read_artifact

    def read(path: object, artifact_format: str) -> object:
        trace.append(f"hash:{artifact_format}")
        return real_read(path, artifact_format)

    monkeypatch.setattr(executor_module, "_read_artifact", read)
    monkeypatch.setattr(executor_module, "_Session", Session)
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: trace.append("observe") or ("normalized",),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: (_ for _ in ()).throw(AssertionError("must not normalize")),
    )
    for name in ("checkpoint_fcstd", "validate_import", "export_step"):
        monkeypatch.setattr(
            InProcessCadExecutor,
            name,
            lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(
                AssertionError(f"revalidation must not call {_name}")
            ),
        )

    evidence = InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    after = os.lstat(artifact)
    assert evidence == ValidatedImportEvidence(
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )
    assert trace == ["hash:fcstd", "load", "recompute", "observe", "close", "hash:fcstd"]
    assert artifact.read_bytes() == raw
    assert executor_module._stat_identity(after) == executor_module._stat_identity(before)


@pytest.mark.parametrize(
    "path",
    (
        "normalized.FCStd",
        Path("/tmp/normalized.FCStd"),
        Path("nested/normalized.FCStd"),
        Path("../normalized.FCStd"),
        Path("normalized.fcstd"),
        Path("normalized.step"),
    ),
)
def test_revalidate_normalized_import_rejects_non_exact_private_basename_before_cad(
    monkeypatch: pytest.MonkeyPatch,
    path: object,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(executor_module, "_Session", lambda: calls.append("session"))

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(path)  # type: ignore[arg-type]

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert calls == []


def test_revalidate_normalized_import_rejects_path_subclass_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[str] = []

    class HostilePath(type(Path())):
        def is_absolute(self) -> bool:
            dispatched.append("is_absolute")
            raise AssertionError("a non-exact path must not be inspected")

    monkeypatch.setattr(executor_module, "_Session", lambda: dispatched.append("session"))

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(
            HostilePath("normalized.FCStd")
        )

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert dispatched == []


@pytest.mark.parametrize("shape", ("missing", "directory", "corrupt", "hardlink"))
def test_revalidate_normalized_import_maps_initial_file_failures_to_artifact_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    shape: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    if shape == "directory":
        artifact.mkdir()
    elif shape == "corrupt":
        artifact.write_bytes(b"not-an-fcstd")
    elif shape == "hardlink":
        _fcstd(artifact)
        os.link(artifact, Path("alias.FCStd"))
    calls: list[str] = []
    monkeypatch.setattr(executor_module, "_Session", lambda: calls.append("session"))

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert calls == []


@pytest.mark.parametrize("mutation", ("inode_swap", "content", "chmod", "hardlink"))
def test_revalidate_normalized_import_detects_all_file_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    _fcstd(artifact)
    original = artifact.read_bytes()

    class Document:
        def recompute(self) -> None:
            if mutation == "inode_swap":
                replacement = Path("replacement.FCStd")
                replacement.write_bytes(original)
                replacement.chmod(artifact.stat().st_mode)
                replacement.replace(artifact)
            elif mutation == "content":
                _fcstd(artifact, "<Changed />")
            elif mutation == "chmod":
                artifact.chmod(0o600 if artifact.stat().st_mode & 0o777 != 0o600 else 0o640)
            else:
                os.link(artifact, Path("alias.FCStd"))

    class Session:
        doc = Document()
        close_calls = 0

        def load_document(self, path: Path) -> object:
            del path
            return self.doc

        def close_document(self) -> None:
            self.close_calls += 1

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(executor_module, "_validated_import_observations", lambda value: ())

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert session.close_calls == 1


def test_revalidate_normalized_import_detects_digest_drift_after_stable_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    _fcstd(artifact)
    snapshots = iter(
        (
            executor_module._ArtifactSnapshot(sha256="a" * 64, size_bytes=10),
            executor_module._ArtifactSnapshot(sha256="b" * 64, size_bytes=10),
        )
    )

    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

        def load_document(self, path: Path) -> object:
            del path
            return self.doc

        def close_document(self) -> None:
            return None

    monkeypatch.setattr(executor_module, "_Session", Session)
    monkeypatch.setattr(executor_module, "_read_artifact", lambda path, kind: next(snapshots))
    monkeypatch.setattr(executor_module, "_validated_import_observations", lambda value: ())

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("fault", ("load", "recompute", "observe", "close", "fatal"))
def test_revalidate_normalized_import_maps_cad_faults_and_closes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fault: str,
) -> None:
    class FatalCadFault(BaseException):
        pass

    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    _fcstd(artifact)
    raw = artifact.read_bytes()

    class Document:
        def recompute(self) -> None:
            if fault == "recompute":
                raise RuntimeError("private recompute fault")
            if fault == "fatal":
                raise FatalCadFault

    class Session:
        doc = Document()
        close_calls = 0

        def load_document(self, path: Path) -> object:
            del path
            if fault == "load":
                raise RuntimeError("private load fault")
            return self.doc

        def close_document(self) -> None:
            self.close_calls += 1
            if fault == "close":
                raise RuntimeError("private close fault")

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)

    def observe(value: object) -> tuple[object, ...]:
        del value
        if fault == "observe":
            raise RuntimeError("private observation fault")
        return ()

    monkeypatch.setattr(executor_module, "_validated_import_observations", observe)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert session.close_calls == 1
    assert artifact.read_bytes() == raw


@pytest.mark.parametrize(
    ("object_types", "expected_code"),
    [
        ((), ExecutorErrorCode.INVALID_INPUT),
        (("Part::Sphere",), ExecutorErrorCode.INVALID_INPUT),
        (("Part::Box", "Part::Sphere"), ExecutorErrorCode.INVALID_INPUT),
        (("Part::Box",), ExecutorErrorCode.CAD_FAILURE),
        (("Part::Box", "Part::Cylinder"), ExecutorErrorCode.CAD_FAILURE),
    ],
)
def test_revalidate_normalized_import_distinguishes_envelope_rejection_from_crash_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    object_types: tuple[str, ...],
    expected_code: ExecutorErrorCode,
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    _fcstd(artifact)

    class Object:
        def __init__(self, object_type: str) -> None:
            self.TypeId = object_type

    class Document:
        Objects = tuple(Object(object_type) for object_type in object_types)

        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()
        close_calls = 0

        def load_document(self, path: Path) -> object:
            assert path == artifact
            return self.doc

        def close_document(self) -> None:
            self.close_calls += 1

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda _session: (_ for _ in ()).throw(ExecutorError(ExecutorErrorCode.INVALID_INPUT)),
    )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    assert caught.value.code is expected_code
    assert session.close_calls == 1


@pytest.mark.parametrize("fault", ("objects", "type_id"))
def test_revalidate_normalized_import_maps_envelope_inspection_faults_to_cad_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fault: str,
) -> None:
    class FatalCadFault(BaseException):
        pass

    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    _fcstd(artifact)
    raw = artifact.read_bytes()

    class Object:
        @property
        def TypeId(self) -> str:
            if fault == "type_id":
                raise FatalCadFault
            return "Part::Box"

    class Document:
        @property
        def Objects(self) -> tuple[Object, ...]:
            if fault == "objects":
                raise FatalCadFault
            return (Object(),)

        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()
        close_calls = 0

        def load_document(self, path: Path) -> object:
            assert path == artifact
            return self.doc

        def close_document(self) -> None:
            self.close_calls += 1

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda _session: (_ for _ in ()).throw(ExecutorError(ExecutorErrorCode.INVALID_INPUT)),
    )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert session.close_calls == 1
    assert artifact.read_bytes() == raw


@pytest.mark.parametrize(
    "object_types",
    ((), ("Part::Sphere",), ("Part::Box", "Part::Sphere")),
)
def test_revalidate_normalized_import_close_fault_overrides_invalid_envelope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    object_types: tuple[str, ...],
) -> None:
    monkeypatch.chdir(tmp_path)
    artifact = Path("normalized.FCStd")
    _fcstd(artifact)
    raw = artifact.read_bytes()

    class Object:
        def __init__(self, object_type: str) -> None:
            self.TypeId = object_type

    class Document:
        Objects = tuple(Object(object_type) for object_type in object_types)

        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()
        close_calls = 0

        def load_document(self, path: Path) -> object:
            assert path == artifact
            return self.doc

        def close_document(self) -> None:
            self.close_calls += 1
            raise RuntimeError("private close fault")

    session = Session()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda _session: (_ for _ in ()).throw(ExecutorError(ExecutorErrorCode.INVALID_INPUT)),
    )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).revalidate_normalized_import(artifact)

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert session.close_calls == 1
    assert artifact.read_bytes() == raw
