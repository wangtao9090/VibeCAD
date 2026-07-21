"""Trusted in-process CAD execution and observation boundary tests."""

from __future__ import annotations

import json
import math
import os
import zipfile
from pathlib import Path

import pytest

import vibecad.execution.executor as executor_module
from vibecad.execution.candidate import (
    ActiveCandidate,
    CadSnapshotPort,
    CheckpointedCandidate,
    SealedCandidate,
    SessionBinding,
)
from vibecad.execution.executor import (
    CandidateEvidence,
    ExecutorError,
    ExecutorErrorCode,
    InProcessCadExecutor,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram
from vibecad.workflow.state import TaskArtifactRef

PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
CANDIDATE_REVISION = "revision_11111111111111111111111111111111"
MODEL_ID = "artifact_0123456789abcdef0123456789abcdef"
STEP_ID = "artifact_11111111111111111111111111111111"
DIGEST = "a" * 64


class _FakeVector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _FakeBoundBox:
    XLength = 12.0
    YLength = 20.0
    ZLength = 30.0


class _FakeShape:
    Volume = 7200.0
    Area = 2400.0
    BoundBox = _FakeBoundBox()
    CenterOfMass = _FakeVector(6.0, 10.0, 15.0)

    def __init__(self, *, export_error: BaseException | None = None) -> None:
        self.Solids = (object(),)
        self.export_error = export_error
        self.export_calls: list[str] = []

    def isValid(self) -> bool:
        return True

    def exportStep(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self.export_calls.append(path)
        if self.export_error is not None:
            raise self.export_error
        Path(path).write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")


class _FakeDocument:
    def __init__(self) -> None:
        self.recompute_calls = 0
        self.save_calls: list[str] = []

    def recompute(self) -> None:
        self.recompute_calls += 1

    def saveCopy(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self.save_calls.append(path)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Document.xml", "<Document />")


class _FakeSession:
    def __init__(self, shape: _FakeShape | None = None) -> None:
        self.doc = _FakeDocument()
        self.shape = shape or _FakeShape()
        self.persist_calls = 0
        self.loaded: list[Path] = []
        self.opened: list[str] = []
        self.close_calls = 0
        self.shape_calls = 0

    def load_document(self, path: Path) -> object:
        self.loaded.append(path)
        return self.doc

    def open_document(self, name: str) -> object:
        self.opened.append(name)
        return self.doc

    def persist_state(self) -> None:
        self.persist_calls += 1

    def close_document(self) -> None:
        self.close_calls += 1

    def get_assembly_shape(self) -> _FakeShape:
        self.shape_calls += 1
        return self.shape


def _store() -> LocalRevisionStore:
    return object.__new__(LocalRevisionStore)


def _lease(*, project_id: str = PROJECT_ID, released: bool = False) -> ProjectWriteLease:
    lease = object.__new__(ProjectWriteLease)
    object.__setattr__(lease, "project_id", project_id)
    object.__setattr__(lease, "released", released)
    return lease


def _head() -> ProjectHead:
    return ProjectHead(
        project_id=PROJECT_ID,
        generation=1,
        revision_id=BASE_REVISION,
        manifest_sha256=DIGEST,
    )


def _active(session: object, root: Path) -> ActiveCandidate:
    return ActiveCandidate(
        project_id=PROJECT_ID,
        base_head=_head(),
        binding=SessionBinding(
            project_id=PROJECT_ID,
            revision_id=CANDIDATE_REVISION,
            session=session,
        ),
        model_path=root / "model.FCStd",
        step_path=root / "model.step",
    )


def _checkpointed(session: object, root: Path) -> CheckpointedCandidate:
    active = _active(session, root)
    return CheckpointedCandidate(
        project_id=active.project_id,
        base_head=active.base_head,
        binding=active.binding,
        model_path=active.model_path,
        step_path=active.step_path,
    )


def _artifact(path: Path, artifact_id: str, artifact_format: str) -> RevisionArtifactRef:
    import hashlib

    raw = path.read_bytes()
    return RevisionArtifactRef(
        id=artifact_id,
        name=path.name,
        format=artifact_format,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


def _sealed(session: object, model_path: Path, step_path: Path) -> SealedCandidate:
    model = _artifact(model_path, MODEL_ID, "fcstd")
    step = _artifact(step_path, STEP_ID, "step")
    revision = RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256=DIGEST,
        model=model,
        artifacts=(step,),
    )
    return SealedCandidate(
        project_id=PROJECT_ID,
        base_head=_head(),
        revision=revision,
        binding=SessionBinding(
            project_id=PROJECT_ID,
            revision_id=CANDIDATE_REVISION,
            session=session,
        ),
    )


def _write_artifacts(root: Path) -> tuple[Path, Path]:
    model = root / "model.FCStd"
    step = root / "model.step"
    with zipfile.ZipFile(model, "w") as archive:
        archive.writestr("Document.xml", "<Document />")
    step.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=A;\nENDSEC;\nEND-ISO-10303-21;\n")
    return model, step


def _command(
    command_id: str,
    op: str,
    *,
    args: dict[str, object] | None = None,
    target: dict[str, object] | None = None,
    depends_on: tuple[str, ...] = (),
) -> ModelCommand:
    return ModelCommand(
        id=command_id,
        op=op,
        target={} if target is None else target,
        args={} if args is None else args,
        depends_on=depends_on,
        preserve=(),
        source=ValueSource.MODEL,
    )


def _program() -> ModelProgram:
    return ModelProgram(
        task_id="task-executor",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length": 10, "width": 20, "height": 30},
            ),
            _command(
                "modify",
                "modify_parameter",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"parameter": "length", "value": 12},
                depends_on=("box",),
            ),
            _command("inspect", "inspect_model", depends_on=("modify",)),
        ),
        acceptance=AcceptanceSpec(id="acceptance-executor", criteria=()),
    )


def _install_store_paths(
    monkeypatch: pytest.MonkeyPatch,
    sealed: SealedCandidate,
    model_path: Path,
    step_path: Path,
) -> list[str]:
    calls: list[str] = []

    def load_revision(self: LocalRevisionStore, project_id: str, revision_id: str) -> RevisionRef:
        del self
        calls.append("load")
        assert (project_id, revision_id) == (PROJECT_ID, CANDIDATE_REVISION)
        return sealed.revision

    def revision_model_path(self: LocalRevisionStore, project_id: str, revision_id: str) -> Path:
        del self
        calls.append("model_path")
        assert (project_id, revision_id) == (PROJECT_ID, CANDIDATE_REVISION)
        return model_path

    def revision_artifact_path(
        self: LocalRevisionStore,
        project_id: str,
        revision_id: str,
        artifact_id: str,
    ) -> Path:
        del self
        calls.append("step_path")
        assert (project_id, revision_id, artifact_id) == (
            PROJECT_ID,
            CANDIDATE_REVISION,
            STEP_ID,
        )
        return step_path

    monkeypatch.setattr(LocalRevisionStore, "load_revision", load_revision)
    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", revision_model_path)
    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", revision_artifact_path)
    return calls


def test_public_contract_and_fixed_redacted_errors() -> None:
    assert executor_module.__all__ == [
        "ExecutorErrorCode",
        "ExecutorError",
        "CandidateEvidence",
        "InProcessCadExecutor",
    ]
    assert {item.value for item in ExecutorErrorCode} == {
        "invalid_input",
        "invalid_candidate",
        "invalid_lease",
        "cad_failure",
        "artifact_failure",
        "integrity_failure",
    }
    for code in ExecutorErrorCode:
        error = ExecutorError(code)
        assert error.to_mapping() == {
            "schema_version": SCHEMA_VERSION,
            "code": code.value,
            "message": error.message,
        }
        assert "secret" not in str(error)
        json.dumps(error.to_mapping())
    with pytest.raises(TypeError):
        ExecutorError("secret")  # type: ignore[arg-type]


def test_constructor_requires_exact_revision_store() -> None:
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=object())  # type: ignore[arg-type]
    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT


def test_executor_is_the_candidate_coordinator_snapshot_port() -> None:
    executor = InProcessCadExecutor(store=_store())
    assert isinstance(executor, CadSnapshotPort)
    assert issubclass(InProcessCadExecutor, CadSnapshotPort)


def test_validate_program_reuses_authentic_validator() -> None:
    validated = InProcessCadExecutor(store=_store()).validate_program(_program())
    assert type(validated) is ValidatedProgram
    validated.require_authentic()
    assert tuple(command.handler_name for command in validated.commands) == (
        "add_box",
        "modify_part",
        "describe_part",
    )


def test_create_load_checkpoint_and_close_use_public_session_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    made: list[_FakeSession] = []

    def factory() -> _FakeSession:
        session = _FakeSession()
        made.append(session)
        return session

    monkeypatch.setattr(executor_module, "_Session", factory)
    executor = InProcessCadExecutor(store=_store())
    empty = executor.create_empty(revision_id=CANDIDATE_REVISION)
    source = tmp_path / "source.FCStd"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Document.xml", "<Document />")
    loaded = executor.load_fcstd(source)
    checkpoint = tmp_path / "candidate.FCStd"
    executor.checkpoint_fcstd(loaded, checkpoint)
    executor.close(loaded)

    assert empty is made[0]
    assert made[0].opened == ["VibeCADCandidate_11111111111111111111111111111111"]
    assert loaded is made[1]
    assert made[1].loaded == [source]
    assert made[1].doc.recompute_calls == 1
    assert made[1].persist_calls == 1
    assert len(made[1].doc.save_calls) == 1
    fresh_checkpoint = Path(made[1].doc.save_calls[0])
    assert fresh_checkpoint != checkpoint
    assert fresh_checkpoint.parent == checkpoint.parent
    assert fresh_checkpoint.suffix.lower() == ".fcstd"
    assert not fresh_checkpoint.exists()
    assert checkpoint.is_file()
    if os.name == "posix":
        assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert made[1].close_calls == 1


def test_create_empty_bootstraps_a_trusted_document_outside_the_model_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptySession:
        def __init__(self) -> None:
            self.close_calls = 0
            self.opened: list[str] = []

        def open_document(self, name: str) -> object:
            self.opened.append(name)
            return object()

        def close_document(self) -> None:
            self.close_calls += 1

    session = EmptySession()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)

    created = InProcessCadExecutor(store=_store()).create_empty(
        revision_id=CANDIDATE_REVISION,
    )

    assert created is session
    assert session.opened == [
        "VibeCADCandidate_11111111111111111111111111111111",
    ]
    assert session.close_calls == 0


def test_create_empty_closes_failed_bootstrap_and_redacts_runtime_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenSession:
        def __init__(self) -> None:
            self.close_calls = 0

        def open_document(self, name: str) -> object:
            del name
            raise RuntimeError("secret-bootstrap-detail")

        def close_document(self) -> None:
            self.close_calls += 1

    session = BrokenSession()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).create_empty(
            revision_id=CANDIDATE_REVISION,
        )

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert "secret" not in str(caught.value)
    assert session.close_calls == 1


def test_create_empty_rejects_invalid_revision_before_constructing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden() -> object:
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr(executor_module, "_Session", forbidden)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).create_empty(revision_id="untrusted")

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert calls == 0


def test_checkpoint_rejects_silent_save_noop_and_preserves_existing_candidate(
    tmp_path: Path,
) -> None:
    class SilentDocument(_FakeDocument):
        def saveCopy(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
            self.save_calls.append(path)

    session = _FakeSession()
    session.doc = SilentDocument()
    checkpoint = tmp_path / "model.FCStd"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("Document.xml", "<Baseline />")
    baseline = checkpoint.read_bytes()

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert checkpoint.read_bytes() == baseline
    assert len(session.doc.save_calls) == 1
    fresh_checkpoint = Path(session.doc.save_calls[0])
    assert fresh_checkpoint != checkpoint
    assert fresh_checkpoint.parent == checkpoint.parent
    assert not fresh_checkpoint.exists()


def test_checkpoint_rejects_malformed_fresh_copy_and_preserves_existing_candidate(
    tmp_path: Path,
) -> None:
    class MalformedDocument(_FakeDocument):
        def saveCopy(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
            self.save_calls.append(path)
            Path(path).write_bytes(b"not-an-fcstd")

    session = _FakeSession()
    session.doc = MalformedDocument()
    checkpoint = tmp_path / "model.FCStd"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("Document.xml", "<Baseline />")
    baseline = checkpoint.read_bytes()

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert checkpoint.read_bytes() == baseline
    assert len(session.doc.save_calls) == 1
    assert not Path(session.doc.save_calls[0]).exists()


def test_checkpoint_replace_failure_preserves_existing_candidate_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    checkpoint = tmp_path / "model.FCStd"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("Document.xml", "<Baseline />")
    baseline = checkpoint.read_bytes()

    def reject_replace(source: Path, destination: Path) -> None:
        assert source != checkpoint
        assert destination == checkpoint
        raise OSError("replace failed")

    monkeypatch.setattr(executor_module.os, "replace", reject_replace)
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert checkpoint.read_bytes() == baseline
    assert len(session.doc.save_calls) == 1
    assert not Path(session.doc.save_calls[0]).exists()


def test_checkpoint_name_collisions_fail_closed_before_freecad_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = "0" * 32
    collision = tmp_path / f".vibecad-checkpoint-{token}.FCStd"
    collision.write_bytes(b"owned-collision")
    checkpoint = tmp_path / "model.FCStd"
    session = _FakeSession()
    monkeypatch.setattr(executor_module.secrets, "token_hex", lambda size: token)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.doc.save_calls == []
    assert collision.read_bytes() == b"owned-collision"
    assert not checkpoint.exists()


@pytest.mark.parametrize("method", ["load_fcstd", "checkpoint_fcstd", "close"])
def test_session_port_exceptions_are_redacted(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    made: list[_FakeSession] = []

    class Broken(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            made.append(self)

        def load_document(self, path: Path) -> object:
            del path
            raise RuntimeError("secret-source-path")

        def persist_state(self) -> None:
            raise RuntimeError("secret-document")

        def close_document(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secret-close")

    monkeypatch.setattr(executor_module, "_Session", Broken)
    executor = InProcessCadExecutor(store=_store())
    with pytest.raises(ExecutorError) as caught:
        if method == "load_fcstd":
            source = tmp_path / "source.FCStd"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Document.xml", "<Document />")
            executor.load_fcstd(source)
        elif method == "checkpoint_fcstd":
            executor.checkpoint_fcstd(Broken(), tmp_path / "model.FCStd")
        else:
            executor.close(Broken())
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert "secret" not in str(caught.value)
    if method == "load_fcstd":
        assert len(made) == 1
        assert made[0].close_calls == 1


def test_execute_program_binds_fixed_handlers_once_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    def add_box(session: object, **kwargs: object) -> dict[str, object]:
        calls.append(("add_box", (session, kwargs)))
        return {"ok": True, "name": "Box"}

    def modify_part(session: object, **kwargs: object) -> dict[str, object]:
        calls.append(("modify_part", (session, kwargs)))
        return {"ok": True}

    def describe_part(session: object) -> dict[str, object]:
        calls.append(("describe_part", session))
        return {"ok": True, "untrusted": "never acceptance evidence"}

    monkeypatch.setattr(executor_module, "_add_box", add_box)
    monkeypatch.setattr(executor_module, "_modify_part", modify_part)
    monkeypatch.setattr(executor_module, "_describe_part", describe_part)
    executor = InProcessCadExecutor(store=_store())
    validated = executor.validate_program(_program())
    session = _FakeSession()

    outcomes = executor.execute_program(
        program=validated,
        candidate=_active(session, tmp_path),
    )

    assert tuple(name for name, _ in calls) == (
        "add_box",
        "modify_part",
        "describe_part",
    )
    assert len(outcomes) == 3
    assert all(outcome.result.ok for outcome in outcomes)
    assert all(outcome.result.revision == CANDIDATE_REVISION for outcome in outcomes)
    assert all(
        (payload[0] if type(payload) is tuple else payload) is session for _, payload in calls
    )
    assert calls[0][1][1] == {  # type: ignore[index]
        "length": 10,
        "width": 20,
        "height": 30,
    }
    assert calls[1][1][1] == {  # type: ignore[index]
        "name": "Box",
        "parameter": "length",
        "value": 12,
    }


def test_execute_preflights_all_fixed_handlers_before_first_cad_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0

    def forbidden(*args: object, **kwargs: object) -> object:
        nonlocal calls
        del args, kwargs
        calls += 1
        return {"ok": True}

    monkeypatch.setattr(executor_module, "_add_box", forbidden)
    monkeypatch.setattr(executor_module, "_modify_part", forbidden)
    monkeypatch.setattr(executor_module, "_describe_part", None)
    executor = InProcessCadExecutor(store=_store())

    with pytest.raises(ExecutorError) as caught:
        executor.execute_program(
            program=executor.validate_program(_program()),
            candidate=_active(_FakeSession(), tmp_path),
        )

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert calls == 0


def test_execute_program_stops_on_first_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = {"box": 0, "modify": 0, "inspect": 0}

    def box(session: object, **kwargs: object) -> object:
        del session, kwargs
        calls["box"] += 1
        raise RuntimeError("secret-cad-detail")

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("execution did not stop")

    monkeypatch.setattr(executor_module, "_add_box", box)
    monkeypatch.setattr(executor_module, "_modify_part", forbidden)
    monkeypatch.setattr(executor_module, "_describe_part", forbidden)
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_program()),
        candidate=_active(_FakeSession(), tmp_path),
    )

    assert calls == {"box": 1, "modify": 0, "inspect": 0}
    assert len(outcomes) == 1
    assert outcomes[-1].result.ok is False
    assert "secret" not in json.dumps(outcomes[-1].result.to_mapping())


@pytest.mark.parametrize("candidate", [object(), None])
def test_execute_rejects_non_active_candidate_before_handlers(candidate: object) -> None:
    executor = InProcessCadExecutor(store=_store())
    with pytest.raises(ExecutorError) as caught:
        executor.execute_program(
            program=executor.validate_program(_program()),
            candidate=candidate,  # type: ignore[arg-type]
        )
    assert caught.value.code is ExecutorErrorCode.INVALID_CANDIDATE


def test_controlled_step_export_uses_only_store_derived_exact_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shape = _FakeShape()
    candidate = _checkpointed(_FakeSession(shape), tmp_path)

    def candidate_artifact_path(
        self: LocalRevisionStore,
        project_id: str,
        revision_id: str,
        artifact_format: str,
        lease: ProjectWriteLease,
    ) -> Path:
        del self
        assert (project_id, revision_id, artifact_format) == (
            PROJECT_ID,
            CANDIDATE_REVISION,
            "step",
        )
        assert lease.project_id == PROJECT_ID
        return candidate.step_path

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", candidate_artifact_path)
    InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())

    assert shape.export_calls == [str(candidate.step_path)]
    assert candidate.step_path.read_bytes().startswith(b"ISO-10303-21;")


def test_step_export_rejects_forged_path_before_shape_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    candidate = _checkpointed(session, tmp_path)

    def wrong_path(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        return tmp_path / "other.step"

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", wrong_path)
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())
    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert session.shape_calls == 0
    assert session.shape.export_calls == []


@pytest.mark.parametrize("entry_kind", ["symlink", "directory", "hardlink"])
def test_step_export_never_overwrites_unsafe_existing_entry(
    entry_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    candidate = _checkpointed(session, tmp_path)
    outside = tmp_path / "outside.step"
    outside.write_bytes(b"outside-sentinel")
    if entry_kind == "symlink":
        candidate.step_path.symlink_to(outside)
    elif entry_kind == "directory":
        candidate.step_path.mkdir()
    else:
        os.link(outside, candidate.step_path)
    monkeypatch.setattr(
        LocalRevisionStore,
        "candidate_artifact_path",
        lambda *args, **kwargs: candidate.step_path,
    )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.shape_calls == 0
    assert session.shape.export_calls == []
    assert outside.read_bytes() == b"outside-sentinel"


def test_wrong_candidate_stage_is_rejected_before_store_or_cad(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    active = _active(session, tmp_path)
    checkpointed = _checkpointed(session, tmp_path)
    sealed = _sealed(session, model_path, step_path)

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("store must not be touched for the wrong stage")

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", forbidden)
    monkeypatch.setattr(LocalRevisionStore, "load_revision", forbidden)
    executor = InProcessCadExecutor(store=_store())

    for wrong_export in (active, sealed):
        with pytest.raises(ExecutorError) as caught:
            executor.export_step(candidate=wrong_export, lease=_lease())  # type: ignore[arg-type]
        assert caught.value.code is ExecutorErrorCode.INVALID_CANDIDATE
    for wrong_collect in (active, checkpointed):
        with pytest.raises(ExecutorError) as caught:
            executor.collect_evidence(candidate=wrong_collect)  # type: ignore[arg-type]
        assert caught.value.code is ExecutorErrorCode.INVALID_CANDIDATE
    assert session.shape_calls == 0


@pytest.mark.parametrize(
    "lease",
    [_lease(project_id="project_22222222222222222222222222222222"), _lease(released=True)],
)
def test_step_export_rejects_wrong_or_released_lease_before_store(
    lease: ProjectWriteLease,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        LocalRevisionStore,
        "candidate_artifact_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("store touched")),
    )
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(
            candidate=_checkpointed(_FakeSession(), tmp_path),
            lease=lease,
        )
    assert caught.value.code is ExecutorErrorCode.INVALID_LEASE


def test_store_rejected_lease_maps_to_invalid_lease_before_shape_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    candidate = _checkpointed(session, tmp_path)

    def reject_lease(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_LEASE)

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", reject_lease)
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())

    assert caught.value.code is ExecutorErrorCode.INVALID_LEASE
    assert session.shape_calls == 0
    assert session.shape.export_calls == []


def test_step_export_failure_is_redacted_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shape = _FakeShape(export_error=RuntimeError("secret-export-path"))
    candidate = _checkpointed(_FakeSession(shape), tmp_path)
    monkeypatch.setattr(
        LocalRevisionStore,
        "candidate_artifact_path",
        lambda *args, **kwargs: candidate.step_path,
    )
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())
    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert shape.export_calls == [str(candidate.step_path)]
    assert "secret" not in str(caught.value)


def test_collect_evidence_is_geometry_owned_and_manifest_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    calls = _install_store_paths(monkeypatch, sealed, model_path, step_path)

    evidence = InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert type(evidence) is CandidateEvidence
    assert evidence.snapshot.candidate_revision == CANDIDATE_REVISION
    assert len(evidence.snapshot.shapes) == 1
    shape = evidence.snapshot.shapes[0]
    assert shape.target == "body"
    assert shape.volume_mm3 == 7200.0
    assert shape.area_mm2 == 2400.0
    assert shape.bbox_mm == (12.0, 20.0, 30.0)
    assert shape.center_of_mass_mm == (6.0, 10.0, 15.0)
    assert shape.valid_shape is True
    assert shape.solid_count == 1
    assert tuple(item.target for item in evidence.snapshot.artifacts) == ("export", "model")
    assert tuple(item.format for item in evidence.snapshot.artifacts) == ("step", "fcstd")
    assert all(item.exists is True for item in evidence.snapshot.artifacts)
    assert all(item.non_empty is True for item in evidence.snapshot.artifacts)
    assert tuple(item.id for item in evidence.artifacts) == (MODEL_ID, STEP_ID)
    assert tuple(item.name for item in evidence.artifacts) == ("model.FCStd", "model.step")
    assert tuple(item.format for item in evidence.artifacts) == ("fcstd", "step")
    assert tuple(item.sha256 for item in evidence.artifacts) == (
        sealed.revision.model.sha256,
        sealed.revision.artifacts[0].sha256,
    )
    assert tuple(item.size_bytes for item in evidence.artifacts) == (
        sealed.revision.model.size_bytes,
        sealed.revision.artifacts[0].size_bytes,
    )
    assert all(item.candidate_revision == CANDIDATE_REVISION for item in evidence.artifacts)
    assert calls == ["load", "model_path", "step_path", "load"]


def test_geometry_observation_copies_independent_non_box_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    shape = _FakeShape()
    shape.Volume = 321.25
    shape.Area = 777.5
    shape.BoundBox = _FakeBoundBox()
    shape.BoundBox.XLength = 3.5
    shape.BoundBox.YLength = 40.25
    shape.BoundBox.ZLength = 9.75
    shape.CenterOfMass = _FakeVector(-4.5, 8.25, 112.0)
    shape.Solids = (object(), object())
    shape.isValid = lambda: False  # type: ignore[method-assign]
    sealed = _sealed(_FakeSession(shape), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    observed = (
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed).snapshot.shapes[0]
    )

    assert observed.volume_mm3 == 321.25
    assert observed.area_mm2 == 777.5
    assert observed.bbox_mm == (3.5, 40.25, 9.75)
    assert observed.center_of_mass_mm == (-4.5, 8.25, 112.0)
    assert observed.valid_shape is False
    assert observed.solid_count == 2


def test_untrusted_inspect_result_cannot_supply_acceptance_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    monkeypatch.setattr(
        executor_module,
        "_add_box",
        lambda *args, **kwargs: {"ok": True, "name": "Box"},
    )
    monkeypatch.setattr(executor_module, "_modify_part", lambda *args, **kwargs: {"ok": True})
    monkeypatch.setattr(
        executor_module,
        "_describe_part",
        lambda *args, **kwargs: {
            "ok": True,
            "volume": -999,
            "bbox": {"x": 999, "y": 999, "z": 999},
            "valid": False,
            "solid_count": 0,
        },
    )
    executor = InProcessCadExecutor(store=_store())
    outcomes = executor.execute_program(
        program=executor.validate_program(_program()),
        candidate=_active(session, tmp_path),
    )
    assert outcomes[-1].result.ok is True

    evidence = executor.collect_evidence(candidate=sealed)

    observed = evidence.snapshot.shapes[0]
    assert observed.volume_mm3 == 7200.0
    assert observed.bbox_mm == (12.0, 20.0, 30.0)
    assert observed.valid_shape is True
    assert observed.solid_count == 1


@pytest.mark.parametrize("boundary", ["load", "model_path", "shape"])
def test_evidence_boundary_exceptions_are_fixed_and_redacted(
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)

    class SecretSession(_FakeSession):
        def get_assembly_shape(self) -> _FakeShape:
            self.shape_calls += 1
            raise RuntimeError("secret-geometry-detail")

    session = SecretSession() if boundary == "shape" else _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    if boundary == "load":
        monkeypatch.setattr(
            LocalRevisionStore,
            "load_revision",
            lambda *args: (_ for _ in ()).throw(RuntimeError("secret-store-record")),
        )
    else:
        _install_store_paths(monkeypatch, sealed, model_path, step_path)
        if boundary == "model_path":
            monkeypatch.setattr(
                LocalRevisionStore,
                "revision_model_path",
                lambda *args: (_ for _ in ()).throw(RuntimeError("secret-store-path")),
            )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    expected = (
        ExecutorErrorCode.CAD_FAILURE
        if boundary == "shape"
        else ExecutorErrorCode.INTEGRITY_FAILURE
    )
    assert caught.value.code is expected
    assert "secret" not in str(caught.value)
    assert "secret" not in json.dumps(caught.value.to_mapping())


@pytest.mark.parametrize(
    ("corrupt", "expected"),
    [
        ("fcstd", ExecutorErrorCode.ARTIFACT_FAILURE),
        ("step", ExecutorErrorCode.ARTIFACT_FAILURE),
        ("hash", ExecutorErrorCode.INTEGRITY_FAILURE),
    ],
)
def test_collect_evidence_rejects_format_or_hash_corruption(
    corrupt: str,
    expected: ExecutorErrorCode,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    if corrupt == "fcstd":
        model_path.write_bytes(b"not-a-FreeCAD-document")
    elif corrupt == "step":
        step_path.write_bytes(b"not-a-step-file")
    else:
        original = step_path.read_bytes()
        mutated = original.replace(b"#1=A;", b"#1=B;")
        assert len(mutated) == len(original)
        step_path.write_bytes(mutated)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    assert caught.value.code is expected


@pytest.mark.parametrize("bad_format", ["fcstd_without_document", "step_without_trailer"])
def test_format_detection_is_not_magic_or_prefix_only(
    bad_format: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    if bad_format == "fcstd_without_document":
        with zipfile.ZipFile(model_path, "w") as archive:
            archive.writestr("Other.xml", "<Other />")
    else:
        step_path.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n")
    sealed = _sealed(_FakeSession(), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE


def test_actual_artifact_mutation_after_manifest_read_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    monkeypatch.setattr(LocalRevisionStore, "load_revision", lambda *args: sealed.revision)
    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", lambda *args: model_path)

    def mutate_then_return(*args: object) -> Path:
        del args
        original = step_path.read_bytes()
        mutated = original.replace(b"#1=A;", b"#1=B;")
        assert len(mutated) == len(original)
        step_path.write_bytes(mutated)
        return step_path

    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", mutate_then_return)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE


def test_first_durable_revision_mismatch_rejects_before_paths_or_geometry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    mismatched = RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256="b" * 64,
        model=sealed.revision.model,
        artifacts=sealed.revision.artifacts,
    )
    monkeypatch.setattr(LocalRevisionStore, "load_revision", lambda *args: mismatched)

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("paths must not be resolved after a manifest mismatch")

    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", forbidden)
    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", forbidden)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert session.shape_calls == 0


def test_collect_evidence_detects_revision_mutation_between_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    mutated = RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256="b" * 64,
        model=sealed.revision.model,
        artifacts=sealed.revision.artifacts,
    )
    values = iter((sealed.revision, mutated))
    monkeypatch.setattr(LocalRevisionStore, "load_revision", lambda *args: next(values))
    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", lambda *args: model_path)
    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", lambda *args: step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("field", ["Volume", "Area", "BoundBox", "CenterOfMass", "Solids"])
def test_collect_evidence_rejects_malformed_or_nonfinite_shape_facts(
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    shape = _FakeShape()
    if field in {"Volume", "Area"}:
        setattr(shape, field, math.inf)
    elif field == "BoundBox":
        shape.BoundBox = object()
    elif field == "CenterOfMass":
        shape.CenterOfMass = _FakeVector(math.nan, 0, 0)
    else:
        shape.Solids = object()
    sealed = _sealed(_FakeSession(shape), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE


def test_candidate_evidence_is_immutable_and_validates_exact_types(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    evidence = InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    with pytest.raises((AttributeError, TypeError)):
        evidence.artifacts = ()  # type: ignore[misc]
    with pytest.raises(ExecutorError) as caught:
        CandidateEvidence(snapshot=object(), artifacts=())  # type: ignore[arg-type]
    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    wrong_revision = TaskArtifactRef(
        id=MODEL_ID,
        name="model.FCStd",
        format="fcstd",
        sha256=evidence.artifacts[0].sha256,
        size_bytes=evidence.artifacts[0].size_bytes,
        candidate_revision="revision_22222222222222222222222222222222",
    )
    with pytest.raises(ExecutorError) as mismatch:
        CandidateEvidence(snapshot=evidence.snapshot, artifacts=(wrong_revision,))
    assert mismatch.value.code is ExecutorErrorCode.INVALID_INPUT


def test_executor_has_no_configurable_handler_or_path_surface() -> None:
    executor = InProcessCadExecutor(store=_store())
    assert not hasattr(executor, "handlers")
    assert not hasattr(executor, "registry")
    assert not hasattr(executor, "output_dir")
    assert not hasattr(executor, "retry")
