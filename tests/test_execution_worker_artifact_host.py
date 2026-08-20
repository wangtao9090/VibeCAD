"""Worker CAD port host-side reviewed artifact snapshot tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import vibecad.execution.worker_port as worker_port_module
from vibecad import _file_compat
from vibecad.execution.candidate import ActiveCandidate, SessionBinding
from vibecad.execution.errors import ExecutorError, ExecutorErrorCode
from vibecad.execution.freecad_reviewed_artifact_host import (
    REVIEWED_ARTIFACT_MANIFEST_NAME,
    REVIEWED_ARTIFACT_SNAPSHOT_KIND,
    TaskInputSnapshotError,
    TaskInputSnapshotErrorCode,
    TaskInputSnapshotLease,
)
from vibecad.execution.freecad_reviewed_artifact_inputs import (
    ReviewedArtifactCatalogRecord,
    ReviewedArtifactCatalogSnapshot,
)
from vibecad.execution.revisions import LocalRevisionStore, ProjectHead
from vibecad.execution.worker_port import WorkerCadExecutionPort
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.program import ValidatedProgram, validate_model_program

_TASK_ID = "task_artifact_program"
_PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
_BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
_CANDIDATE_REVISION = "revision_11111111111111111111111111111111"
_PAYLOAD = b"sealed-worker-input"


def _program() -> ValidatedProgram:
    return validate_model_program(
        ModelProgram(
            task_id=_TASK_ID,
            base_revision=_BASE_REVISION,
            operations=(
                ModelCommand(
                    id="inspect",
                    op="inspect_model",
                    target={},
                    args={},
                    source=ValueSource.MODEL,
                ),
            ),
            acceptance=AcceptanceSpec(id="acceptance_artifact_program", criteria=()),
        )
    )


def _candidate(tmp_path: Path) -> ActiveCandidate:
    return ActiveCandidate(
        project_id=_PROJECT_ID,
        base_head=ProjectHead(
            project_id=_PROJECT_ID,
            generation=1,
            revision_id=_BASE_REVISION,
            manifest_sha256="a" * 64,
        ),
        binding=SessionBinding(
            project_id=_PROJECT_ID,
            revision_id=_CANDIDATE_REVISION,
            session=object(),
        ),
        model_path=tmp_path / "candidate.FCStd",
        step_path=tmp_path / "candidate.step",
    )


def _install_candidate(
    port: WorkerCadExecutionPort,
    worker: object,
    candidate: ActiveCandidate,
) -> None:
    state = worker_port_module._Capability(  # noqa: SLF001
        kind="candidate",
        key=(candidate.project_id, candidate.binding.revision_id),
        value=object(),
        base_head=candidate.base_head,
    )
    state.sessions.add(candidate.binding.session)
    port._worker = worker  # noqa: SLF001
    port._sessions[candidate.binding.session] = state  # noqa: SLF001


class _Preflight:
    def __init__(self, required: bool) -> None:
        self.required = required
        self.programs: list[ValidatedProgram] = []

    def requires_artifact_snapshot(self, program: ValidatedProgram) -> bool:
        self.programs.append(program)
        return self.required


class _Provider:
    def __init__(self, root: Path, *, wrong_task: bool = False) -> None:
        self.root = root
        self.wrong_task = wrong_task
        self.calls: list[tuple[str, str, str, str]] = []
        self.lease: TaskInputSnapshotLease | None = None

    def acquire(
        self,
        *,
        task_id: str,
        project_id: str,
        base_revision: str,
        run_id: str,
    ) -> TaskInputSnapshotLease:
        self.calls.append((task_id, project_id, base_revision, run_id))
        payload_sha = hashlib.sha256(_PAYLOAD).hexdigest()
        snapshot = ReviewedArtifactCatalogSnapshot(
            task_id="task_wrong" if self.wrong_task else task_id,
            project_id=project_id,
            base_revision=base_revision,
            run_id=run_id,
            records=(
                ReviewedArtifactCatalogRecord(
                    artifact_id="artifact_input_1",
                    content_sha256=payload_sha,
                    size_bytes=len(_PAYLOAD),
                    media_type="image/png",
                    role_term_ref_id="reference_image",
                    schema_term_ref_id="image_png_v1",
                    document_id="document_0123456789abcdef0123456789abcdef",
                    family_id="freecad_imageplane",
                    operation_ids=("imageplane_create",),
                    maximum_bytes=1024,
                ),
            ),
        )
        directory = self.root / f"snapshot_{len(self.calls)}"
        directory.mkdir(mode=0o700)
        directory.chmod(0o700)
        manifest = directory / REVIEWED_ARTIFACT_MANIFEST_NAME
        manifest.write_bytes(
            json.dumps(
                snapshot.to_mapping(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        )
        manifest.chmod(0o600)
        payload = directory / snapshot.records[0].artifact_id
        payload.write_bytes(_PAYLOAD)
        payload.chmod(0o600)
        if os.name == "nt":
            _file_compat.set_private_dacl(directory)
            _file_compat.set_private_dacl(manifest)
            _file_compat.set_private_dacl(payload)
            self.lease = TaskInputSnapshotLease(
                snapshot=snapshot,
                directory_capability=_file_compat.capture_windows_path(
                    directory,
                    directory=True,
                ),
            )
            return self.lease
        descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            self.lease = TaskInputSnapshotLease(snapshot=snapshot, directory_fd=descriptor)
        finally:
            os.close(descriptor)
        return self.lease


def _port(
    *,
    worker: object,
    candidate: ActiveCandidate,
    provider: object | None,
    preflight: _Preflight | None,
) -> WorkerCadExecutionPort:
    port = WorkerCadExecutionPort(
        store=object.__new__(LocalRevisionStore),
        worker_factory=lambda *, source_root: worker,
        task_input_snapshot_provider=provider,  # type: ignore[arg-type]
        task_input_preflight=preflight,
    )
    _install_candidate(port, worker, candidate)
    return port


def _assert_lease_closed(lease: TaskInputSnapshotLease) -> None:
    with pytest.raises(TaskInputSnapshotError) as closed:
        if os.name == "nt":
            lease.windows_capability_mapping()
        else:
            lease.duplicate_directory_fd()
    assert closed.value.code is TaskInputSnapshotErrorCode.CLOSED


def test_nonartifact_program_preserves_legacy_worker_call_and_skips_provider(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)

    class LegacyWorker:
        calls = 0

        def execute_program(self, *, program, candidate, session):
            del program, candidate, session
            self.calls += 1
            return ()

    class RejectingProvider:
        def acquire(self, **kwargs):
            del kwargs
            raise AssertionError("provider must not be called")

    worker = LegacyWorker()
    preflight = _Preflight(False)
    port = _port(
        worker=worker,
        candidate=candidate,
        provider=RejectingProvider(),
        preflight=preflight,
    )
    program = _program()

    assert port.execute_program(program=program, candidate=candidate) == ()
    assert worker.calls == 1
    assert preflight.programs == [program]


def test_artifact_program_requires_provider_before_worker_call(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)

    class Worker:
        calls = 0

        def execute_program(self, **kwargs):
            del kwargs
            self.calls += 1
            return ()

    worker = Worker()
    port = _port(
        worker=worker,
        candidate=candidate,
        provider=None,
        preflight=_Preflight(True),
    )
    with pytest.raises(ExecutorError) as caught:
        port.execute_program(program=_program(), candidate=candidate)
    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert worker.calls == 0


def test_artifact_program_sends_one_bound_descriptor_and_duplicate_fd_then_cleans(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    provider = _Provider(tmp_path)

    class Worker:
        received_fd = -1
        received_capability: dict[str, object] | None = None
        received_snapshot: dict[str, object] | None = None

        def execute_program(self, **kwargs):
            kwargs.pop("program")
            kwargs.pop("candidate")
            kwargs.pop("session")
            artifact_snapshot = kwargs.pop("artifact_snapshot")
            assert type(artifact_snapshot) is dict
            assert provider.lease is not None
            if os.name == "nt":
                capability_mapping = kwargs.pop("artifact_snapshot_capability")
                capability = _file_compat.WindowsPathCapability.from_mapping(capability_mapping)
                _file_compat.validate_windows_path(capability, directory=True)
                self.received_capability = capability_mapping
            else:
                artifact_snapshot_fd = kwargs.pop("artifact_snapshot_fd")
                assert type(artifact_snapshot_fd) is int
                assert artifact_snapshot_fd != provider.lease._directory_fd  # noqa: SLF001
                os.fstat(artifact_snapshot_fd)
                self.received_fd = artifact_snapshot_fd
            assert kwargs == {}
            self.received_snapshot = artifact_snapshot
            return ()

    worker = Worker()
    port = _port(
        worker=worker,
        candidate=candidate,
        provider=provider,
        preflight=_Preflight(True),
    )

    assert port.execute_program(program=_program(), candidate=candidate) == ()
    assert len(provider.calls) == 1
    task_id, project_id, base_revision, run_id = provider.calls[0]
    assert (task_id, project_id, base_revision) == (_TASK_ID, _PROJECT_ID, _BASE_REVISION)
    assert run_id.startswith("run_")
    assert worker.received_snapshot is not None
    assert worker.received_snapshot == {
        "base_revision": _BASE_REVISION,
        "catalog_sha256": provider.lease._snapshot.catalog_sha256,  # noqa: SLF001
        "kind": REVIEWED_ARTIFACT_SNAPSHOT_KIND,
        "project_id": _PROJECT_ID,
        "run_id": run_id,
        "schema_version": 1,
        "task_id": _TASK_ID,
    }
    if os.name == "nt":
        assert worker.received_capability is not None
    else:
        with pytest.raises(OSError):
            os.fstat(worker.received_fd)
    _assert_lease_closed(provider.lease)


def test_wrong_task_snapshot_fails_before_worker_and_closes_lease(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    provider = _Provider(tmp_path, wrong_task=True)

    class Worker:
        calls = 0

        def execute_program(self, **kwargs):
            del kwargs
            self.calls += 1
            return ()

    worker = Worker()
    port = _port(
        worker=worker,
        candidate=candidate,
        provider=provider,
        preflight=_Preflight(True),
    )
    with pytest.raises(ExecutorError) as caught:
        port.execute_program(program=_program(), candidate=candidate)
    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert worker.calls == 0
    assert provider.lease is not None
    _assert_lease_closed(provider.lease)


def test_worker_cancellation_still_closes_transferred_fd_and_provider_lease(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    provider = _Provider(tmp_path)

    class Worker:
        received_fd = -1
        received_capability: dict[str, object] | None = None

        def execute_program(self, **kwargs):
            if os.name == "nt":
                self.received_capability = kwargs["artifact_snapshot_capability"]
                capability = _file_compat.WindowsPathCapability.from_mapping(
                    self.received_capability
                )
                _file_compat.validate_windows_path(capability, directory=True)
            else:
                self.received_fd = kwargs["artifact_snapshot_fd"]
                os.fstat(self.received_fd)
            raise KeyboardInterrupt

    worker = Worker()
    port = _port(
        worker=worker,
        candidate=candidate,
        provider=provider,
        preflight=_Preflight(True),
    )
    with pytest.raises(KeyboardInterrupt):
        port.execute_program(program=_program(), candidate=candidate)
    if os.name == "nt":
        assert worker.received_capability is not None
    else:
        with pytest.raises(OSError):
            os.fstat(worker.received_fd)
    assert provider.lease is not None
    _assert_lease_closed(provider.lease)
