"""Verified artifact API, durable store, task gate, and resource tests."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import threading
import time
import zipfile
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

import vibecad.application.artifacts as artifacts_module
from vibecad.application.agent import AgentApplication
from vibecad.application.artifacts import (
    ARTIFACT_COPY_CHUNK_BYTES,
    ArtifactApi,
    ArtifactCopyCursor,
    ArtifactDependencyError,
    ArtifactDependencyErrorCode,
    ArtifactDependencyFailure,
    ArtifactEligibility,
    ArtifactExportRequest,
    ArtifactMaterializationService,
    ArtifactRequestPhase,
    ArtifactResourceError,
    ArtifactResourceErrorCode,
    ArtifactResourceReader,
    ArtifactServiceErrorCode,
    ArtifactServicePortFailure,
    ArtifactSourceKind,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactStoreErrorCode,
    LocalArtifactAuthority,
)
from vibecad.application.task_api import (
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionArtifactRef,
    RevisionCopyCursor,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
)
from vibecad.interaction.cad import CadExecutionPort, ValidatedMaterializationEvidence
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ResourceLeaseManager,
)
from vibecad.workflow.state import (
    CriterionOutcome,
    CriterionVerdict,
    ReasoningOwner,
    ReviewDraft,
    ReviewPolicy,
    TaskArtifactRef,
    TaskEvent,
    TaskStatus,
    VerificationReport,
    append_artifact,
    new_task_run,
    transition_task,
)
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
    TaskStoreRootTrust,
)

TASK_ID = "task_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
REVISION_ID = "revision_11111111111111111111111111111111"
DRAFT_ID = "draft_11111111111111111111111111111111"
MODEL_ID = "artifact_0123456789abcdef0123456789abcdef"
STEP_ID = "artifact_11111111111111111111111111111111"
EXPORT_KEY = "export_0123456789abcdef0123456789abcdef"
GENERATION = 17
MANIFEST = "a" * 64


def _fcstd_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("Document.xml", "<Document />")
    return stream.getvalue()


MODEL_BYTES = _fcstd_bytes()
STEP_BYTES = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


def _refs() -> tuple[RevisionArtifactRef, RevisionArtifactRef]:
    return (
        RevisionArtifactRef(
            id=MODEL_ID,
            name="model.FCStd",
            format="fcstd",
            sha256=hashlib.sha256(MODEL_BYTES).hexdigest(),
            size_bytes=len(MODEL_BYTES),
        ),
        RevisionArtifactRef(
            id=STEP_ID,
            name="model.step",
            format="step",
            sha256=hashlib.sha256(STEP_BYTES).hexdigest(),
            size_bytes=len(STEP_BYTES),
        ),
    )


def _revision() -> RevisionRef:
    model, step = _refs()
    return RevisionRef(
        id=REVISION_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256=MANIFEST,
        model=model,
        artifacts=(step,),
    )


def _report() -> VerificationReport:
    return VerificationReport(
        id="verification_0123456789abcdef0123456789abcdef",
        acceptance_id="artifact-acceptance",
        candidate_revision=REVISION_ID,
        manifest_sha256=MANIFEST,
        observation_digest="b" * 64,
        passed=True,
        verdicts=(
            CriterionVerdict(
                criterion_id="artifact",
                required=True,
                outcome=CriterionOutcome.PASS,
                message="Artifact checks passed.",
            ),
        ),
    )


def _task(*, draft: bool = False, status: TaskStatus | None = None) -> StoredTaskRun:
    policy = ReviewPolicy.REQUIRE_REVIEW if draft else ReviewPolicy.AUTO_COMMIT
    task = new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=policy,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    task = transition_task(
        task,
        TaskEvent.SUBMIT_PROGRAM,
        program=ModelProgram(
            task_id=TASK_ID,
            base_revision=BASE_REVISION,
            operations=(),
            acceptance=AcceptanceSpec(id="artifact-acceptance", criteria=()),
        ),
    )
    task = transition_task(task, TaskEvent.START_VALIDATION)
    task = transition_task(
        task,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=REVISION_ID,
    )
    for ref in _refs():
        task = append_artifact(
            task,
            TaskArtifactRef(
                id=ref.id,
                name=ref.name,
                format=ref.format,
                sha256=ref.sha256,
                size_bytes=ref.size_bytes,
                candidate_revision=REVISION_ID,
            ),
        )
    task = transition_task(task, TaskEvent.COMPLETE_EXECUTION)
    if draft:
        review = ReviewDraft(
            id=DRAFT_ID,
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            base_revision=BASE_REVISION,
            base_generation=0,
            base_manifest_sha256="c" * 64,
            revision_id=REVISION_ID,
            manifest_sha256=MANIFEST,
            verification_id="verification_0123456789abcdef0123456789abcdef",
            acceptance_id="artifact-acceptance",
            observation_digest="b" * 64,
        )
        task = transition_task(
            task,
            TaskEvent.PREPARE_REVIEW,
            verification=_report(),
            draft=review,
        )
        task = transition_task(task, TaskEvent.PUBLISH_DRAFT)
    else:
        task = transition_task(task, TaskEvent.PASS_VERIFICATION, verification=_report())
        task = transition_task(
            task,
            TaskEvent.COMMIT,
            committed_revision=REVISION_ID,
        )
    if status is not None:
        object.__setattr__(task, "status", status)
    return StoredTaskRun(generation=GENERATION, task_run=task)


class _Gate(AbstractContextManager[None]):
    def __init__(self, calls: list[str], failure: ArtifactDependencyErrorCode | None) -> None:
        self.calls = calls
        self.failure = failure

    def __enter__(self) -> None:
        self.calls.append("gate_enter")
        if self.failure is not None:
            raise ArtifactDependencyError(self.failure)
        return None

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.calls.append("gate_exit")
        return False


class _Authority:
    def __init__(self, *, draft: bool = False) -> None:
        self.stored = _task(draft=draft)
        self.revision = _revision()
        self.calls: list[str] = []
        self.exists: bool | ArtifactDependencyFailure = True
        self.gate_failure: ArtifactDependencyErrorCode | None = None
        self.copy_failure: ArtifactDependencyFailure | None = None
        self.load_count = 0
        self.after_load: callable | None = None

    def task_exists(self, *, task_id: str):
        assert task_id == TASK_ID
        self.calls.append("task_exists")
        return self.exists

    def acquire_export_gate(self, *, task_id: str):
        assert task_id == TASK_ID
        self.calls.append("gate_requested")
        return _Gate(self.calls, self.gate_failure)

    def load_task(self, *, task_id: str):
        assert task_id == TASK_ID
        self.calls.append("load_task")
        self.load_count += 1
        if self.after_load is not None:
            self.after_load()
        return self.stored

    def load_revision(self, *, project_id: str, revision_id: str):
        assert (project_id, revision_id) == (PROJECT_ID, REVISION_ID)
        self.calls.append("load_revision")
        return self.revision

    def copy_authoritative(
        self,
        *,
        eligibility: ArtifactEligibility,
        destination_directory_fd: int,
        cursors: tuple[ArtifactCopyCursor, ...],
        chunk_bytes: int,
    ):
        self.calls.append("copy")
        assert eligibility.artifacts == _refs()
        assert chunk_bytes == ARTIFACT_COPY_CHUNK_BYTES
        if self.copy_failure is not None:
            return self.copy_failure
        cursor_by_name = {item.name: item for item in cursors}
        for name, content in (("model.FCStd", MODEL_BYTES), ("model.step", STEP_BYTES)):
            cursor = cursor_by_name.get(name)
            offset = 0 if cursor is None else cursor.size_bytes
            if cursor is not None:
                assert cursor.sha256 == hashlib.sha256(content[:offset]).hexdigest()
            flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, 0o600, dir_fd=destination_directory_fd)
            try:
                os.fchmod(fd, 0o600)
                os.lseek(fd, offset, os.SEEK_SET)
                os.ftruncate(fd, offset)
                remaining = memoryview(content[offset:])
                while remaining:
                    written = os.write(fd, remaining[:chunk_bytes])
                    remaining = remaining[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        return None


class _Cad(CadExecutionPort):
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []
        self.failure: BaseException | None = None

    def validate_materialization(
        self,
        *,
        fcstd: Path,
        step: Path,
    ) -> ValidatedMaterializationEvidence:
        self.calls.append((fcstd, step))
        if self.failure is not None:
            raise self.failure
        model = fcstd.read_bytes()
        exchanged = step.read_bytes()
        return ValidatedMaterializationEvidence(
            fcstd_sha256=hashlib.sha256(model).hexdigest(),
            fcstd_size_bytes=len(model),
            step_sha256=hashlib.sha256(exchanged).hexdigest(),
            step_size_bytes=len(exchanged),
        )


def _request(*, export_key: str = EXPORT_KEY, draft: bool = False) -> dict[str, object]:
    return {
        "schema_version": 1,
        "export_key": export_key,
        "task_id": TASK_ID,
        "expected_generation": GENERATION,
        "revision_id": REVISION_ID,
        "draft_id": DRAFT_ID if draft else None,
    }


def _composition(tmp_path: Path, *, draft: bool = False):
    authority = _Authority(draft=draft)
    cad = _Cad()
    store = ArtifactStore(root=tmp_path / "artifacts")
    service = ArtifactMaterializationService(store=store, authority=authority, cad=cad)
    return ArtifactApi(port=service), service, store, authority, cad


def test_export_materializes_exact_pair_then_replays_without_ports(tmp_path: Path) -> None:
    api, _service, store, authority, cad = _composition(tmp_path)

    first = api.export_task_artifacts(_request())

    assert first["ok"] is True
    result = first["result"]
    assert set(result) == {
        "schema_version",
        "export_key",
        "materialization_id",
        "source_kind",
        "task_id",
        "task_generation",
        "project_id",
        "revision_id",
        "manifest_sha256",
        "authoritative",
        "artifacts",
    }
    assert result["source_kind"] == "committed"
    assert result["authoritative"] is False
    assert [item["name"] for item in result["artifacts"]] == ["model.FCStd", "model.step"]
    assert all("path" not in item for item in result["artifacts"])
    assert authority.calls == [
        "task_exists",
        "gate_requested",
        "gate_enter",
        "load_task",
        "load_revision",
        "copy",
        "load_task",
        "load_revision",
        "load_task",
        "load_revision",
        "gate_exit",
    ]
    assert cad.calls == [(Path("model.FCStd"), Path("model.step"))]
    record = next((store.root / "requests").iterdir())
    assert json.loads(record.read_text())["body"]["phase"] == ArtifactRequestPhase.PUBLISHED

    authority.calls.clear()
    cad.calls.clear()
    authority.exists = ArtifactDependencyFailure(code=ArtifactDependencyErrorCode.INTERNAL_ERROR)
    replay = ArtifactStore(root=store.root)
    replay_service = ArtifactMaterializationService(
        store=replay,
        authority=authority,
        cad=cad,
    )

    second = ArtifactApi(port=replay_service).export_task_artifacts(_request())

    assert second == first
    assert authority.calls == []
    assert cad.calls == []


def test_resource_requires_published_binding_and_returns_canonical_bounded_content(
    tmp_path: Path,
) -> None:
    api, _service, store, _authority, _cad = _composition(tmp_path)
    response = api.export_task_artifacts(_request())
    artifact = response["result"]["artifacts"][0]

    content = store.read_resource(artifact["resource_uri"])

    assert content.uri == artifact["resource_uri"]
    assert content.mime_type == "application/vnd.freecad.fcstd"
    assert base64.b64decode(content.blob) == MODEL_BYTES
    guessed = artifact["resource_uri"].replace(MODEL_ID, "artifact_" + "f" * 32)
    with pytest.raises(ArtifactResourceError) as caught:
        store.read_resource(guessed)
    assert caught.value.code is ArtifactResourceErrorCode.UNAVAILABLE
    assert guessed not in str(caught.value)


def _artifact_tree_snapshot(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        value = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        content = path.read_bytes() if stat.S_ISREG(value.st_mode) else None
        snapshot[relative] = (
            stat.S_IFMT(value.st_mode),
            stat.S_IMODE(value.st_mode),
            value.st_dev,
            value.st_ino,
            value.st_uid,
            value.st_gid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            content,
        )
    return snapshot


@pytest.mark.parametrize(
    ("artifact_index", "mime_type", "expected_content"),
    [
        (0, "application/vnd.freecad.fcstd", MODEL_BYTES),
        (1, "model/step", STEP_BYTES),
    ],
)
def test_resource_reader_is_strictly_read_only_and_returns_exact_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact_index: int,
    mime_type: str,
    expected_content: bytes,
) -> None:
    api, _service, store, _authority, cad = _composition(tmp_path)
    response = api.export_task_artifacts(_request())
    artifact = response["result"]["artifacts"][artifact_index]
    root = store.root
    root_value = root.stat()
    store.close()
    cad.calls.clear()
    before = _artifact_tree_snapshot(root)
    real_open = artifacts_module.os.open

    def readonly_open(path, flags, mode=0o777, *, dir_fd=None):
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        assert flags & write_flags == 0
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("resource reader attempted a mutation or lock")

    monkeypatch.setattr(artifacts_module.os, "open", readonly_open)
    for name in ("mkdir", "unlink", "rmdir", "rename", "replace", "chmod", "fchmod", "fsync"):
        monkeypatch.setattr(artifacts_module.os, name, forbidden)
    monkeypatch.setattr(artifacts_module.fcntl, "flock", forbidden)

    reader = ArtifactResourceReader(
        root=root,
        expected_root_identity=(root_value.st_dev, root_value.st_ino),
    )
    content = reader.read_resource(artifact["resource_uri"])

    assert content.uri == artifact["resource_uri"]
    assert content.mime_type == mime_type
    assert base64.b64decode(content.blob) == expected_content
    assert cad.calls == []
    assert _artifact_tree_snapshot(root) == before


@pytest.mark.parametrize("kind", ["missing", "unsafe", "wrong_identity"])
def test_resource_reader_rejects_missing_or_unsafe_root_without_creating_it(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "artifact-root"
    expected = None
    if kind != "missing":
        root.mkdir(mode=0o700)
        if kind == "unsafe":
            root.chmod(0o755)
        else:
            value = root.stat()
            expected = (value.st_dev, value.st_ino + 1)
    before = _artifact_tree_snapshot(tmp_path)

    with pytest.raises(ArtifactResourceError) as caught:
        ArtifactResourceReader(root=root, expected_root_identity=expected)

    assert caught.value.code is ArtifactResourceErrorCode.UNAVAILABLE
    assert _artifact_tree_snapshot(tmp_path) == before
    if kind == "missing":
        assert not root.exists()


@pytest.mark.parametrize("damage", ["missing", "symlink"])
def test_resource_reader_requires_published_binding_and_rejects_unsafe_artifact(
    tmp_path: Path,
    damage: str,
) -> None:
    api, _service, store, _authority, _cad = _composition(tmp_path)
    response = api.export_task_artifacts(_request())
    artifact = response["result"]["artifacts"][0]
    root = store.root
    store.close()
    reader = ArtifactResourceReader(root=root)
    guessed = artifact["resource_uri"].replace(MODEL_ID, "artifact_" + "f" * 32)

    with pytest.raises(ArtifactResourceError) as missing:
        reader.read_resource(guessed)

    assert missing.value.code is ArtifactResourceErrorCode.UNAVAILABLE
    materialization = root / "materializations" / response["result"]["materialization_id"]
    if damage == "missing":
        shutil.rmtree(materialization)
    else:
        source = tmp_path / "foreign.FCStd"
        source.write_bytes(MODEL_BYTES)
        target = materialization / "model.FCStd"
        target.unlink()
        target.symlink_to(source)

    with pytest.raises(ArtifactResourceError) as unsafe:
        reader.read_resource(artifact["resource_uri"])

    assert unsafe.value.code is ArtifactResourceErrorCode.UNAVAILABLE


def test_resource_reader_fails_closed_when_selected_file_changes_during_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api, _service, store, _authority, _cad = _composition(tmp_path)
    response = api.export_task_artifacts(_request())
    artifact = response["result"]["artifacts"][0]
    root = store.root
    target = root / "materializations" / response["result"]["materialization_id"] / "model.FCStd"
    store.close()
    reader = ArtifactResourceReader(root=root)
    real_encode = artifacts_module.base64.b64encode
    raced = False

    def racing_encode(value: bytes) -> bytes:
        nonlocal raced
        if not raced:
            raced = True
            target.write_bytes(b"X" + MODEL_BYTES[1:])
            target.chmod(0o600)
        return real_encode(value)

    monkeypatch.setattr(artifacts_module.base64, "b64encode", racing_encode)

    with pytest.raises(ArtifactResourceError) as caught:
        reader.read_resource(artifact["resource_uri"])

    assert raced is True
    assert caught.value.code is ArtifactResourceErrorCode.UNAVAILABLE


@pytest.mark.parametrize(
    ("value", "code", "path"),
    [
        ({**_request(), "output_path": "/secret"}, "unknown_field", "/output_path"),
        (
            {key: value for key, value in _request().items() if key != "draft_id"},
            "missing_field",
            "/draft_id",
        ),
        ({**_request(), "expected_generation": True}, "invalid_type", "/expected_generation"),
        ({**_request(), "revision_id": "revision_BAD"}, "invalid_value", "/revision_id"),
    ],
)
def test_ingress_is_strict_before_any_port(
    tmp_path: Path,
    value: object,
    code: str,
    path: str,
) -> None:
    api, _service, _store, authority, cad = _composition(tmp_path)

    response = api.export_task_artifacts(value)

    assert response["ok"] is False
    assert response["error"]["code"] == code
    assert response["error"]["path"] == path
    assert authority.calls == []
    assert cad.calls == []


def test_same_key_different_intent_conflicts_before_task_or_cad(tmp_path: Path) -> None:
    api, _service, _store, authority, cad = _composition(tmp_path)
    assert api.export_task_artifacts(_request())["ok"] is True
    authority.calls.clear()
    cad.calls.clear()
    changed = {**_request(), "expected_generation": GENERATION + 1}

    response = api.export_task_artifacts(changed)

    assert response["error"]["code"] == "conflict"
    assert authority.calls == []
    assert cad.calls == []


def test_draft_requires_awaiting_state_and_projects_distinct_identity(tmp_path: Path) -> None:
    api, _service, _store, _authority, _cad = _composition(tmp_path, draft=True)

    response = api.export_task_artifacts(_request(draft=True))

    assert response["ok"] is True
    assert response["result"]["source_kind"] == "draft"
    assert response["result"]["materialization_id"].startswith("materialization_")


def test_task_probe_precedes_gate_and_gate_failure_has_zero_copy(tmp_path: Path) -> None:
    api, _service, _store, authority, cad = _composition(tmp_path)
    authority.gate_failure = ArtifactDependencyErrorCode.LEASE_UNAVAILABLE

    response = api.export_task_artifacts(_request())

    assert response["error"]["code"] == "lease_unavailable"
    assert authority.calls == ["task_exists", "gate_requested", "gate_enter"]
    assert cad.calls == []


def test_transient_cad_failure_retains_copied_phase_and_retry_resumes(tmp_path: Path) -> None:
    api, _service, store, authority, cad = _composition(tmp_path)
    from vibecad.execution.executor import ExecutorError, ExecutorErrorCode

    cad.failure = ExecutorError(ExecutorErrorCode.CAD_FAILURE)
    failed = api.export_task_artifacts(_request())
    record = next((store.root / "requests").iterdir())

    assert failed["error"]["code"] == "cad_failure"
    assert json.loads(record.read_text())["body"]["phase"] == ArtifactRequestPhase.COPIED
    cad.failure = None
    authority.calls.clear()

    resumed = api.export_task_artifacts(_request())

    assert resumed["ok"] is True
    assert "copy" not in authority.calls


def test_final_eligibility_conflict_removes_materialized_directory(tmp_path: Path) -> None:
    api, _service, store, authority, _cad = _composition(tmp_path)

    def change_on_third_load() -> None:
        if authority.load_count == 3:
            authority.stored = StoredTaskRun(
                generation=GENERATION + 1,
                task_run=authority.stored.task_run,
            )

    authority.after_load = change_on_third_load

    response = api.export_task_artifacts(_request())

    assert response["error"]["code"] == "conflict"
    records = tuple((store.root / "requests").iterdir())
    assert len(records) == 1
    assert json.loads(records[0].read_text())["body"]["phase"] == ArtifactRequestPhase.REJECTED
    assert list((store.root / "materializations").iterdir()) == []
    assert [
        entry for entry in store.root.iterdir() if entry.name.startswith(".materialization_")
    ] == []


@pytest.mark.parametrize("transition", ("accept", "reject"))
def test_published_draft_export_linearizes_before_agent_review_transition(
    transition: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    app = AgentApplication.open(data_root=home / "data")
    created = app._task_store.create(_task(draft=True).task_run)  # noqa: SLF001
    assert created.generation == 0

    local = LocalArtifactAuthority(
        task_store=app._task_store,  # noqa: SLF001
        revision_store=app._revision_store,  # noqa: SLF001
        lease_manager=app._lease_manager,  # noqa: SLF001
    )
    copy_source = _Authority(draft=True)

    class SharedAuthority:
        def task_exists(self, *, task_id: str):
            return local.task_exists(task_id=task_id)

        def acquire_export_gate(self, *, task_id: str):
            return local.acquire_export_gate(task_id=task_id)

        def load_task(self, *, task_id: str):
            return local.load_task(task_id=task_id)

        def load_revision(self, *, project_id: str, revision_id: str):
            assert (project_id, revision_id) == (PROJECT_ID, REVISION_ID)
            return _revision()

        def copy_authoritative(self, **kwargs):
            return copy_source.copy_authoritative(**kwargs)

    delivery = ArtifactStore(root=tmp_path / "delivery")
    service = ArtifactMaterializationService(
        store=delivery,
        authority=SharedAuthority(),
        cad=_Cad(),
    )
    api = ArtifactApi(port=service)
    request = {**_request(draft=True), "expected_generation": 0}
    final_reloaded = threading.Event()
    allow_publish = threading.Event()
    original_write = ArtifactStore._write_record

    def pause_before_publish(self, record):
        if self is delivery and record.phase is ArtifactRequestPhase.PUBLISHED:
            final_reloaded.set()
            assert allow_publish.wait(timeout=5)
        return original_write(self, record)

    monkeypatch.setattr(ArtifactStore, "_write_record", pause_before_publish)

    if transition == "accept":

        def accept_without_cad(self, method: str, **kwargs):
            assert self is app
            assert method == "accept_draft"
            stored = self._task_store.load(kwargs["task_id"])
            assert stored.generation == kwargs["expected_generation"]
            assert stored.task_run.draft is not None
            assert stored.task_run.draft.id == kwargs["draft_id"]
            accepting = transition_task(stored.task_run, TaskEvent.ACCEPT_DRAFT)
            succeeded = transition_task(
                accepting,
                TaskEvent.COMMIT,
                committed_revision=REVISION_ID,
            )
            return self._task_store.compare_and_set(
                stored.task_run.id,
                stored.generation,
                succeeded,
            )

        monkeypatch.setattr(AgentApplication, "_cad_method", accept_without_cad)

    def invoke_review_transition():
        method = app.accept_draft if transition == "accept" else app.reject_draft
        return method(
            task_id=TASK_ID,
            draft_id=DRAFT_ID,
            expected_generation=0,
        )

    responses: list[dict[str, object]] = []
    export_errors: list[BaseException] = []

    def export() -> None:
        try:
            responses.append(api.export_task_artifacts(request))
        except BaseException as error:
            export_errors.append(error)

    worker = threading.Thread(target=export)
    worker.start()
    assert final_reloaded.wait(timeout=5)
    before = app._task_store.load(TASK_ID)  # noqa: SLF001
    before_tree = _artifact_tree_snapshot(app._layout.tasks)  # noqa: SLF001
    try:
        blocked = invoke_review_transition()
        during = app._task_store.load(TASK_ID)  # noqa: SLF001
        during_tree = _artifact_tree_snapshot(app._layout.tasks)  # noqa: SLF001
    finally:
        allow_publish.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert export_errors == []
    assert len(responses) == 1 and responses[0]["ok"] is True
    expected_block = TaskServicePortFailure(code=TaskServicePortErrorCode.LEASE_UNAVAILABLE)
    blocked_correctly = (
        blocked == expected_block and during == before and during_tree == before_tree
    )

    completed = None
    replay = None
    if blocked_correctly:
        completed = invoke_review_transition()
        replay = api.export_task_artifacts(request)

    delivery.close()
    app.close()

    assert blocked == expected_block
    assert during == before
    assert during_tree == before_tree
    assert type(app._artifact_authority) is LocalArtifactAuthority  # noqa: SLF001
    assert app._artifact_store is None  # noqa: SLF001
    assert app._artifact_service is None  # noqa: SLF001
    assert app._artifact_api is None  # noqa: SLF001
    assert app._cad_validation_port is None  # noqa: SLF001
    assert app._runtimes == {}  # noqa: SLF001
    assert type(completed) is StoredTaskRun
    expected_status = TaskStatus.SUCCEEDED if transition == "accept" else TaskStatus.REJECTED
    assert completed.task_run.status is expected_status
    assert replay == responses[0]


def test_checksums_and_materialized_hash_are_fail_closed(tmp_path: Path) -> None:
    api, _service, store, _authority, _cad = _composition(tmp_path)
    response = api.export_task_artifacts(_request())
    assert response["ok"] is True
    record = next((store.root / "requests").iterdir())
    encoded = json.loads(record.read_text())
    encoded["body"]["phase"] = ArtifactRequestPhase.REJECTED
    record.write_text(json.dumps(encoded))
    os.chmod(record, 0o600)

    replay = api.export_task_artifacts(_request())

    assert replay["error"]["code"] == "integrity_failure"


def test_resource_uri_grammar_and_read_limit_are_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api, _service, store, _authority, _cad = _composition(tmp_path)
    response = api.export_task_artifacts(_request())
    uri = response["result"]["artifacts"][0]["resource_uri"]
    for invalid in (uri.upper(), uri + "?x=1", uri.replace("artifact_", "artifact_%")):
        with pytest.raises(ArtifactResourceError) as caught:
            store.read_resource(invalid)
        assert caught.value.code is ArtifactResourceErrorCode.INVALID_IDENTIFIER
        assert invalid not in str(caught.value)
    encoded_calls = []
    real_encode = artifacts_module.base64.b64encode

    def observed_encode(value: bytes) -> bytes:
        encoded_calls.append(len(value))
        return real_encode(value)

    monkeypatch.setattr(artifacts_module.base64, "b64encode", observed_encode)
    at_limit = store.read_resource(uri)
    assert encoded_calls == [len(MODEL_BYTES)]
    assert len(at_limit.blob) == 4 * ((len(MODEL_BYTES) + 2) // 3)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_RESOURCE_BYTES", len(MODEL_BYTES) - 1)
    with pytest.raises(ArtifactResourceError) as caught:
        store.read_resource(uri)
    assert caught.value.code is ArtifactResourceErrorCode.READ_LIMIT
    assert encoded_calls == [len(MODEL_BYTES)]


def test_resource_peak_admission_formula_has_exact_64_mib_boundary() -> None:
    maximum = artifacts_module.MAX_ARTIFACT_RESOURCE_BYTES
    encoded = artifacts_module.MAX_ARTIFACT_RESOURCE_BASE64_BYTES

    assert 4 * ((maximum + 2) // 3) == encoded
    assert (
        artifacts_module._resource_incremental_allocation_bound(maximum)
        <= artifacts_module.MAX_ARTIFACT_RESOURCE_INCREMENTAL_BYTES
    )
    with pytest.raises(ArtifactResourceError) as caught:
        artifacts_module._resource_incremental_allocation_bound(maximum + 1)
    assert caught.value.code is ArtifactResourceErrorCode.READ_LIMIT


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin thread-local cwd contract")
def test_cad_relative_paths_remain_bound_during_live_pathname_swap(tmp_path: Path) -> None:
    api, _service, store, authority, cad = _composition(tmp_path)
    observed: list[bytes] = []

    def swapped_validation(*, fcstd: Path, step: Path) -> ValidatedMaterializationEvidence:
        assert (fcstd, step) == (Path("model.FCStd"), Path("model.step"))
        temporary = next(
            entry for entry in store.root.iterdir() if entry.name.startswith(".materialization_")
        )
        moved = store.root / ".moved-for-test"
        temporary.rename(moved)
        temporary.mkdir(mode=0o700)
        (temporary / "model.FCStd").write_bytes(b"attacker")
        (temporary / "model.step").write_bytes(b"attacker")
        try:
            observed.extend((fcstd.read_bytes(), step.read_bytes()))
        finally:
            shutil.rmtree(temporary)
            moved.rename(temporary)
        return ValidatedMaterializationEvidence(
            fcstd_sha256=hashlib.sha256(observed[0]).hexdigest(),
            fcstd_size_bytes=len(observed[0]),
            step_sha256=hashlib.sha256(observed[1]).hexdigest(),
            step_size_bytes=len(observed[1]),
        )

    cad.validate_materialization = swapped_validation  # type: ignore[method-assign]

    response = api.export_task_artifacts(_request())

    assert response["ok"] is True
    assert observed == [MODEL_BYTES, STEP_BYTES]
    assert "copy" in authority.calls


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin thread-local cwd contract")
def test_thread_cwd_restore_failure_returns_recovery_and_never_publishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api, _service, store, _authority, _cad = _composition(tmp_path)
    real = artifacts_module._darwin_thread_fchdir

    def fail_restore(fd: int) -> None:
        if fd == -1:
            raise artifacts_module.ArtifactStoreError(
                artifacts_module.ArtifactStoreErrorCode.RECOVERY_REQUIRED
            )
        real(fd)

    monkeypatch.setattr(artifacts_module, "_darwin_thread_fchdir", fail_restore)

    try:
        response = api.export_task_artifacts(_request())
    finally:
        real(-1)

    assert response["error"]["code"] == "recovery_required"
    record = next((store.root / "requests").iterdir())
    assert json.loads(record.read_text())["body"]["phase"] == ArtifactRequestPhase.COPIED


def test_record_and_store_capacity_n_plus_one_do_not_create_second_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_REQUESTS", 1)
    api, _service, store, _authority, _cad = _composition(tmp_path)
    assert api.export_task_artifacts(_request())["ok"] is True

    response = api.export_task_artifacts(
        _request(export_key="export_11111111111111111111111111111111")
    )

    assert response["error"]["code"] == "resource_exhausted"
    assert len(tuple((store.root / "requests").iterdir())) == 1


def test_materialization_count_n_plus_one_fails_before_copy_or_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_MATERIALIZATIONS", 1)
    api, _service, store, _authority, _cad = _composition(tmp_path)
    assert api.export_task_artifacts(_request())["ok"] is True
    draft_authority = _Authority(draft=True)
    draft_cad = _Cad()
    draft_api = ArtifactApi(
        port=ArtifactMaterializationService(
            store=store,
            authority=draft_authority,
            cad=draft_cad,
        )
    )

    response = draft_api.export_task_artifacts(
        _request(
            export_key="export_11111111111111111111111111111111",
            draft=True,
        )
    )

    assert response["error"]["code"] == "resource_exhausted"
    assert "copy" not in draft_authority.calls
    assert draft_cad.calls == []
    assert len(tuple((store.root / "requests").iterdir())) == 1
    assert len(tuple((store.root / "materializations").iterdir())) == 1


def _ordinary_bytes(root: Path) -> int:
    return sum(
        entry.stat().st_size
        for entry in root.rglob("*")
        if entry.is_file() and not entry.is_symlink()
    )


def _reserved_record_size(request: ArtifactExportRequest, eligibility: ArtifactEligibility) -> int:
    materialization_id = artifacts_module._materialization_id(eligibility)
    record = artifacts_module._RequestRecord(
        phase=ArtifactRequestPhase.RESERVED,
        export_key=request.export_key,
        request_digest=artifacts_module._request_digest(request),
        eligibility=eligibility,
        materialization_id=materialization_id,
        delivery_manifest_sha256=artifacts_module._delivery_manifest_digest(eligibility),
        temporary_name=f".{materialization_id}.{'f' * 32}.tmp",
    )
    return len(artifacts_module._record_envelope(record))


def test_store_byte_admission_exact_n_and_n_plus_one_are_pre_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api, service, store, authority, _cad = _composition(tmp_path)
    assert api.export_task_artifacts(_request())["ok"] is True
    second_key = "export_11111111111111111111111111111111"
    second_request = ArtifactExportRequest(
        export_key=second_key,
        task_id=TASK_ID,
        expected_generation=GENERATION,
        revision_id=REVISION_ID,
        draft_id=None,
    )
    eligibility = service._eligibility(second_request)
    assert type(eligibility) is ArtifactEligibility
    exact = (
        _ordinary_bytes(store.root)
        + sum(item.size_bytes for item in eligibility.artifacts)
        + 2 * artifacts_module.MAX_ARTIFACT_RECORD_BYTES
    )
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_STORE_BYTES", exact)

    at_n = api.export_task_artifacts(_request(export_key=second_key))

    assert at_n["ok"] is True
    third_key = "export_22222222222222222222222222222222"
    third_request = ArtifactExportRequest(
        export_key=third_key,
        task_id=TASK_ID,
        expected_generation=GENERATION,
        revision_id=REVISION_ID,
        draft_id=None,
    )
    third_eligibility = service._eligibility(third_request)
    assert type(third_eligibility) is ArtifactEligibility
    n_plus_one = (
        _ordinary_bytes(store.root)
        + sum(item.size_bytes for item in third_eligibility.artifacts)
        + 2 * artifacts_module.MAX_ARTIFACT_RECORD_BYTES
    )
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_STORE_BYTES", n_plus_one - 1)
    authority.calls.clear()

    over = api.export_task_artifacts(_request(export_key=third_key))

    assert over["error"]["code"] == "resource_exhausted"
    assert "copy" not in authority.calls
    assert len(tuple((store.root / "requests").iterdir())) == 2


def test_temporary_n_plus_one_fails_before_new_request_or_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_TEMPORARIES", 1)
    api, _service, store, authority, _cad = _composition(tmp_path)
    authority.copy_failure = ArtifactDependencyFailure(
        code=ArtifactDependencyErrorCode.STORE_FAILURE
    )
    assert api.export_task_artifacts(_request())["error"]["code"] == "store_failure"
    authority.calls.clear()

    second = api.export_task_artifacts(
        _request(export_key="export_11111111111111111111111111111111")
    )

    assert second["error"]["code"] == "resource_exhausted"
    assert "copy" not in authority.calls
    assert len(tuple((store.root / "requests").iterdir())) == 1
    assert (
        len(
            tuple(
                entry
                for entry in store.root.iterdir()
                if entry.name.startswith(".materialization_")
            )
        )
        == 1
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin descriptor namespace contract")
def test_root_swap_after_lock_never_mutates_replacement_tree(tmp_path: Path) -> None:
    api, _service, store, authority, _cad = _composition(tmp_path)
    original_copy = authority.copy_authoritative
    root = store.root
    moved = root.with_name("artifacts-pinned")
    replacement_marker = b"replacement-root"

    def swap_then_copy(**kwargs):
        root.rename(moved)
        root.mkdir(mode=0o700)
        (root / "requests").mkdir(mode=0o700)
        (root / "materializations").mkdir(mode=0o700)
        (root / ".artifact-mutation.lock").write_bytes(replacement_marker)
        (root / ".artifact-mutation.lock").chmod(0o600)
        return original_copy(**kwargs)

    authority.copy_authoritative = swap_then_copy  # type: ignore[method-assign]
    try:
        response = api.export_task_artifacts(_request())
        assert response["error"]["code"] == "integrity_failure"
        assert (root / ".artifact-mutation.lock").read_bytes() == replacement_marker
        assert list((root / "requests").iterdir()) == []
        assert list((root / "materializations").iterdir()) == []
    finally:
        shutil.rmtree(root)
        moved.rename(root)


def test_lock_release_failure_poison_is_durable_for_store_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api, _service, _store, _authority, _cad = _composition(tmp_path)
    real_flock = artifacts_module.fcntl.flock

    def fail_unlock(fd: int, operation: int) -> None:
        if operation == artifacts_module.fcntl.LOCK_UN:
            raise OSError
        real_flock(fd, operation)

    monkeypatch.setattr(artifacts_module.fcntl, "flock", fail_unlock)

    first = api.export_task_artifacts(_request())
    second = api.export_task_artifacts(_request())

    assert first["error"]["code"] == "recovery_required"
    assert second["error"]["code"] == "recovery_required"


def _open_fd_count() -> int:
    for location in (Path("/dev/fd"), Path("/proc/self/fd")):
        if location.is_dir():
            return len(tuple(location.iterdir()))
    pytest.skip("The platform has no inspectable process descriptor directory.")


def test_store_close_is_idempotent_releases_every_owned_fd_and_prevents_reuse(
    tmp_path: Path,
) -> None:
    before = _open_fd_count()
    store = ArtifactStore(root=(tmp_path / "artifacts").resolve())
    assert _open_fd_count() == before + 3

    store.close()
    store.close()

    assert _open_fd_count() == before
    with pytest.raises(ArtifactStoreError) as captured:
        store.lookup_terminal(
            request=ArtifactExportRequest(
                export_key=EXPORT_KEY,
                task_id=TASK_ID,
                expected_generation=GENERATION,
                revision_id=REVISION_ID,
                draft_id=None,
            )
        )
    assert captured.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED


def test_constructor_failure_closes_every_partially_opened_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    before = _open_fd_count()
    real_open = artifacts_module.os.open

    def fail_materializations_open(path, flags, mode=0o777, *, dir_fd=None):
        if path == "materializations":
            raise OSError
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts_module.os, "open", fail_materializations_open)
    for _ in range(16):
        with pytest.raises(ArtifactStoreError) as captured:
            ArtifactStore(root=(tmp_path / "artifacts").resolve())
        assert captured.value.code is ArtifactStoreErrorCode.IO_ERROR

    assert _open_fd_count() == before


def test_close_failure_retires_all_numbers_and_poison_is_durable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(root=(tmp_path / "artifacts").resolve())
    victim = store._requests_fd
    real_close = artifacts_module.os.close

    def fail_one_close(fd: int) -> None:
        if fd == victim:
            raise OSError
        real_close(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifacts_module.os, "close", fail_one_close)
        with pytest.raises(ArtifactStoreError) as first:
            store.close()
        with pytest.raises(ArtifactStoreError) as replay:
            store.close()

    assert first.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED
    assert replay.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED
    assert (store._root_fd, store._requests_fd, store._materializations_fd) == (-1, -1, -1)
    real_close(victim)


def test_wrong_process_close_does_not_consume_owner_descriptors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(root=(tmp_path / "artifacts").resolve())
    descriptors = (store._root_fd, store._requests_fd, store._materializations_fd)
    owner = artifacts_module.os.getpid()

    with monkeypatch.context() as scoped:
        scoped.setattr(artifacts_module.os, "getpid", lambda: owner + 1)
        with pytest.raises(ArtifactStoreError) as captured:
            store.close()

    assert captured.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED
    for fd in descriptors:
        os.fstat(fd)
    store.close()


def test_service_and_api_failures_are_fixed_and_do_not_reflect_exceptions(tmp_path: Path) -> None:
    class FailingPort:
        def export_task_artifacts(self, *, request: ArtifactExportRequest):
            del request
            raise RuntimeError("/secret/private.FCStd")

    response = ArtifactApi(port=FailingPort()).export_task_artifacts(_request())

    assert response["error"] == {
        "schema_version": 1,
        "code": "internal_error",
        "path": "",
        "message": "The request could not be completed.",
    }
    assert "secret" not in json.dumps(response)


def test_exact_protocol_and_result_values_are_frozen() -> None:
    eligibility = ArtifactEligibility(
        source_kind=ArtifactSourceKind.COMMITTED,
        task_id=TASK_ID,
        task_generation=GENERATION,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        manifest_sha256=MANIFEST,
        draft_id=None,
        artifacts=_refs(),
    )
    assert ArtifactRequestPhase.__members__ == {
        name: ArtifactRequestPhase[name]
        for name in (
            "RESERVED",
            "STAGING",
            "COPIED",
            "VALIDATED",
            "MATERIALIZED",
            "PUBLISHED",
            "CLEANUP_REQUIRED",
            "REJECTED",
        )
    }
    assert eligibility.source_kind is ArtifactSourceKind.COMMITTED
    request = ArtifactExportRequest(
        export_key=EXPORT_KEY,
        task_id=TASK_ID,
        expected_generation=GENERATION,
        revision_id=REVISION_ID,
        draft_id=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        request.export_key = "export_" + "f" * 32  # type: ignore[misc]


def test_dependency_failure_value_cannot_carry_private_details() -> None:
    failure = ArtifactDependencyFailure(code=ArtifactDependencyErrorCode.STORE_FAILURE)
    assert failure == ArtifactDependencyFailure(code=ArtifactDependencyErrorCode.STORE_FAILURE)
    assert not hasattr(failure, "path")
    assert (
        ArtifactServicePortFailure(code=ArtifactServiceErrorCode.STORE_FAILURE).code
        is ArtifactServiceErrorCode.STORE_FAILURE
    )


def _request_value(export_key: str = EXPORT_KEY) -> ArtifactExportRequest:
    return ArtifactExportRequest(
        export_key=export_key,
        task_id=TASK_ID,
        expected_generation=GENERATION,
        revision_id=REVISION_ID,
        draft_id=None,
    )


def _eligibility_with_sizes(model_size: int, step_size: int) -> ArtifactEligibility:
    return ArtifactEligibility(
        source_kind=ArtifactSourceKind.COMMITTED,
        task_id=TASK_ID,
        task_generation=GENERATION,
        project_id=PROJECT_ID,
        revision_id=REVISION_ID,
        manifest_sha256=MANIFEST,
        draft_id=None,
        artifacts=(
            RevisionArtifactRef(
                id=MODEL_ID,
                name="model.FCStd",
                format="fcstd",
                sha256="b" * 64,
                size_bytes=model_size,
            ),
            RevisionArtifactRef(
                id=STEP_ID,
                name="model.step",
                format="step",
                sha256="c" * 64,
                size_bytes=step_size,
            ),
        ),
    )


def test_nonterminal_reservation_ceilings_accumulate_before_new_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(root=(tmp_path / "artifacts").resolve())
    eligibility = _eligibility_with_sizes(96 * 1024, 96 * 1024)
    first = _request_value()
    second = _request_value("export_11111111111111111111111111111111")
    ceiling = sum(item.size_bytes for item in eligibility.artifacts) + 2 * 64 * 1024
    with store._lock():
        store._reserve(first, eligibility)
    ordinary = _ordinary_bytes(store.root)
    first_record_size = next((store.root / "requests").iterdir()).stat().st_size
    monkeypatch.setattr(
        artifacts_module,
        "MAX_ARTIFACT_STORE_BYTES",
        ordinary - first_record_size + ceiling + ceiling - 1,
    )

    with store._lock(), pytest.raises(ArtifactStoreError) as captured:
        store._reserve(second, eligibility)

    assert captured.value.code is ArtifactStoreErrorCode.RESOURCE_EXHAUSTED
    assert len(tuple((store.root / "requests").iterdir())) == 1


def test_materialize_never_overwrites_preexisting_empty_collision(tmp_path: Path) -> None:
    api, service, store, _authority, cad = _composition(tmp_path)
    eligibility = service._eligibility(_request_value())
    assert type(eligibility) is ArtifactEligibility
    collision = store.root / "materializations" / artifacts_module._materialization_id(eligibility)
    observed_inode: list[int] = []
    real_validate = cad.validate_materialization

    def collide_then_validate(**kwargs):
        collision.mkdir(mode=0o700)
        observed_inode.append(collision.stat().st_ino)
        return real_validate(**kwargs)

    cad.validate_materialization = collide_then_validate  # type: ignore[method-assign]

    response = api.export_task_artifacts(_request())

    assert response["error"]["code"] == "integrity_failure"
    assert collision.is_dir()
    assert collision.stat().st_ino == observed_inode[0]
    assert list(collision.iterdir()) == []


def test_reserved_exact_temporary_is_adopted_before_global_slot_n_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ArtifactStore(root=(tmp_path / "artifacts").resolve())
    request = _request_value()
    eligibility = _eligibility_with_sizes(1024, 1024)
    with store._lock():
        record = store._reserve(request, eligibility)
    os.mkdir(record.temporary_name, 0o700, dir_fd=store._root_fd)
    os.fsync(store._root_fd)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_TEMPORARIES", 1)

    with store._lock():
        staged = store._ensure_staging(record)

    assert staged.phase is ArtifactRequestPhase.STAGING
    assert staged.temporary_identity is not None


@pytest.mark.parametrize("primary", [RuntimeError("x"), KeyboardInterrupt(), SystemExit()])
def test_working_fd_close_failure_is_recovery_for_all_primary_throwables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    primary: BaseException,
) -> None:
    api, _service, store, authority, _cad = _composition(tmp_path)
    real_copy = authority.copy_authoritative
    real_close = artifacts_module.os.close
    armed = False

    def fail_copy(**kwargs):
        nonlocal armed
        real_copy(**kwargs)
        armed = True
        raise primary

    def close_then_fail(fd: int) -> None:
        if armed:
            try:
                opened = os.fstat(fd)
                candidate = next(
                    item
                    for item in store.root.iterdir()
                    if item.name.startswith(".materialization_")
                ).stat()
            except (OSError, StopIteration):
                pass
            else:
                if (opened.st_dev, opened.st_ino) == (candidate.st_dev, candidate.st_ino):
                    real_close(fd)
                    raise OSError
        real_close(fd)

    authority.copy_authoritative = fail_copy  # type: ignore[method-assign]
    monkeypatch.setattr(artifacts_module.os, "close", close_then_fail)

    response = api.export_task_artifacts(_request())

    assert response["error"]["code"] == "recovery_required"
    assert api.export_task_artifacts(_request())["error"]["code"] == "recovery_required"


def test_repeated_working_fd_close_faults_leave_live_fd_set_stable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    before = _open_fd_count()
    real_close = artifacts_module.os.close
    fault_identity: list[tuple[int, int] | None] = [None]

    def close_then_fail(fd: int) -> None:
        try:
            opened = os.fstat(fd)
        except OSError:
            real_close(fd)
            return
        if fault_identity[0] == (opened.st_dev, opened.st_ino):
            fault_identity[0] = None
            real_close(fd)
            raise OSError
        real_close(fd)

    monkeypatch.setattr(artifacts_module.os, "close", close_then_fail)
    for index in range(16):
        api, _service, store, authority, _cad = _composition(tmp_path / str(index))
        real_copy = authority.copy_authoritative

        def fail_copy(_copy=real_copy, **kwargs):
            _copy(**kwargs)
            opened = os.fstat(kwargs["destination_directory_fd"])
            fault_identity[0] = (opened.st_dev, opened.st_ino)
            raise KeyboardInterrupt

        authority.copy_authoritative = fail_copy  # type: ignore[method-assign]
        assert api.export_task_artifacts(_request())["error"]["code"] == "recovery_required"
        store.close()

    assert fault_identity == [None]
    assert _open_fd_count() == before


def test_api_never_invokes_hostile_string_equality_before_exact_type_checks() -> None:
    calls = 0

    class EvilStr(str):
        def __eq__(self, other):
            nonlocal calls
            calls += 1
            raise RuntimeError("must not compare")

        __hash__ = str.__hash__

    request = _request()
    request[EvilStr("hostile")] = request.pop("draft_id")
    ingress = ArtifactApi(port=object()).export_task_artifacts(request)  # type: ignore[arg-type]
    assert ingress["error"]["code"] == "invalid_type"
    assert calls == 0

    forged = artifacts_module._result(_request_value(), _eligibility_with_sizes(1024, 1024))
    object.__setattr__(forged, "export_key", EvilStr(forged.export_key))

    class Port:
        def export_task_artifacts(self, *, request):
            del request
            return forged

    result = ArtifactApi(port=Port()).export_task_artifacts(_request())
    assert result["error"]["code"] == "internal_error"
    assert calls == 0


def _orphan_marker_bytes(name: str, created_ns: int, identity: os.stat_result) -> bytes:
    body = {
        "schema_version": 1,
        "temporary_name": name,
        "created_ns": str(created_ns),
        "identity": {
            "dev": str(identity.st_dev),
            "ino": str(identity.st_ino),
            "uid": str(identity.st_uid),
            "mode": str(stat.S_IMODE(identity.st_mode)),
        },
    }
    canonical = json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    envelope = {
        "schema_version": 1,
        "body": body,
        "body_sha256": hashlib.sha256(
            b"vibecad-artifact-temp-creation-v1\0" + canonical
        ).hexdigest(),
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _expired_orphan(store: ArtifactStore, suffix: str) -> tuple[str, Path]:
    eligibility = _eligibility_with_sizes(1024, 1024)
    name = f".{artifacts_module._materialization_id(eligibility)}.{suffix * 32}.tmp"
    orphan = store.root / name
    orphan.mkdir(mode=0o700)
    created_ns = time.time_ns() - (86_400 + 1) * 1_000_000_000
    marker = orphan / ".creation.json"
    marker.write_bytes(_orphan_marker_bytes(name, created_ns, orphan.stat()))
    marker.chmod(0o600)
    return name, orphan


def _artifact_tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    snapshot: list[tuple[object, ...]] = []
    for entry in sorted(root.rglob("*")):
        value = entry.stat(follow_symlinks=False)
        relative = entry.relative_to(root).as_posix()
        digest = hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None
        snapshot.append(
            (
                relative,
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                digest,
            )
        )
    return tuple(snapshot)


def _observe_cleanup_receipt_creates(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    created: list[str] = []
    real_open = artifacts_module.os.open

    def observe(path, flags, mode=0o777, *, dir_fd=None):
        if str(path).startswith(".cleanup_") and flags & os.O_CREAT:
            created.append(str(path))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts_module.os, "open", observe)
    return created


def test_expired_unbound_marker_orphan_and_record_remnant_converge(tmp_path: Path) -> None:
    store = ArtifactStore(root=(tmp_path / "artifacts").resolve())
    eligibility = _eligibility_with_sizes(1024, 1024)
    materialization = artifacts_module._materialization_id(eligibility)
    orphan_name = f".{materialization}.{'d' * 32}.tmp"
    orphan = store.root / orphan_name
    orphan.mkdir(mode=0o700)
    created_ns = time.time_ns() - (86_400 + 1) * 1_000_000_000
    marker = orphan / ".creation.json"
    marker.write_bytes(_orphan_marker_bytes(orphan_name, created_ns, orphan.stat()))
    marker.chmod(0o600)

    request = _request_value()
    record = artifacts_module._RequestRecord(
        phase=ArtifactRequestPhase.RESERVED,
        export_key=request.export_key,
        request_digest=artifacts_module._request_digest(request),
        eligibility=eligibility,
        materialization_id=materialization,
        delivery_manifest_sha256=artifacts_module._delivery_manifest_digest(eligibility),
        temporary_name=f".{materialization}.{'c' * 32}.tmp",
    )
    request_name = store._request_name(request.export_key)
    remnant = store.root / "requests" / f".{request_name}.{'b' * 32}.tmp"
    remnant.write_bytes(artifacts_module._record_envelope(record))
    remnant.chmod(0o600)
    old = time.time() - 86_401
    os.utime(remnant, (old, old), follow_symlinks=False)

    assert store.lookup_terminal(request=request) is None
    assert not orphan.exists()
    assert not remnant.exists()


@pytest.mark.parametrize("marker_kind", ["missing", "corrupt", "future"])
def test_orphan_cleanup_requires_valid_expired_durable_creation_identity(
    tmp_path: Path,
    marker_kind: str,
) -> None:
    store = ArtifactStore(root=(tmp_path / "artifacts").resolve())
    eligibility = _eligibility_with_sizes(1024, 1024)
    name = f".{artifacts_module._materialization_id(eligibility)}.{'a' * 32}.tmp"
    orphan = store.root / name
    orphan.mkdir(mode=0o700)
    if marker_kind != "missing":
        created_ns = (
            time.time_ns() + 1_000_000_000
            if marker_kind == "future"
            else time.time_ns() - (86_400 + 1) * 1_000_000_000
        )
        raw = _orphan_marker_bytes(name, created_ns, orphan.stat())
        if marker_kind == "corrupt":
            raw += b"x"
        marker = orphan / ".creation.json"
        marker.write_bytes(raw)
        marker.chmod(0o600)

    with pytest.raises(ArtifactStoreError) as captured:
        store.lookup_terminal(request=_request_value())

    assert captured.value.code in {
        ArtifactStoreErrorCode.INTEGRITY_FAILURE,
        ArtifactStoreErrorCode.RECOVERY_REQUIRED,
    }
    assert orphan.exists()


def test_cleanup_receipt_capacity_failure_is_pre_create_and_zero_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    _name, orphan = _expired_orphan(store, "4")
    before = _artifact_tree_snapshot(root)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_STORE_BYTES", _ordinary_bytes(root))
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_TEMPORARIES", 1)
    creates = _observe_cleanup_receipt_creates(monkeypatch)

    with pytest.raises(ArtifactStoreError) as captured:
        store.lookup_terminal(request=_request_value())

    assert captured.value.code is ArtifactStoreErrorCode.RESOURCE_EXHAUSTED
    assert creates == []
    assert _artifact_tree_snapshot(root) == before
    assert (orphan / ".creation.json").is_file()
    store.close()


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_cleanup_receipt_byte_publication_peak_n_minus_one_n_and_n_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delta: int,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    name, orphan = _expired_orphan(store, "3")
    receipt, _size = store._inspect_unbound_temporary(
        name,
        artifacts_module._identity(orphan.stat()),
        time.time_ns(),
    )
    receipt_size = len(artifacts_module._cleanup_receipt_envelope(receipt))
    publication_peak = _ordinary_bytes(root) + 2 * receipt_size
    monkeypatch.setattr(
        artifacts_module,
        "MAX_ARTIFACT_STORE_BYTES",
        publication_peak + delta,
    )
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_TEMPORARIES", 3)
    before = _artifact_tree_snapshot(root)
    creates = _observe_cleanup_receipt_creates(monkeypatch)

    if delta < 0:
        with pytest.raises(ArtifactStoreError) as captured:
            store.lookup_terminal(request=_request_value())
        assert captured.value.code is ArtifactStoreErrorCode.RESOURCE_EXHAUSTED
        assert creates == []
        assert _artifact_tree_snapshot(root) == before
    else:
        assert store.lookup_terminal(request=_request_value()) is None
        assert len(creates) == 1
        assert not orphan.exists()
    store.close()


@pytest.mark.parametrize("temporary_cap", [2, 3, 4])
def test_cleanup_receipt_temporary_peak_n_minus_one_n_and_n_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    temporary_cap: int,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    name, orphan = _expired_orphan(store, "2")
    receipt, _size = store._inspect_unbound_temporary(
        name,
        artifacts_module._identity(orphan.stat()),
        time.time_ns(),
    )
    receipt_size = len(artifacts_module._cleanup_receipt_envelope(receipt))
    monkeypatch.setattr(
        artifacts_module,
        "MAX_ARTIFACT_STORE_BYTES",
        _ordinary_bytes(root) + 2 * receipt_size,
    )
    monkeypatch.setattr(
        artifacts_module,
        "MAX_ARTIFACT_TEMPORARIES",
        temporary_cap,
    )
    before = _artifact_tree_snapshot(root)
    creates = _observe_cleanup_receipt_creates(monkeypatch)

    if temporary_cap < 3:
        with pytest.raises(ArtifactStoreError) as captured:
            store.lookup_terminal(request=_request_value())
        assert captured.value.code is ArtifactStoreErrorCode.RESOURCE_EXHAUSTED
        assert creates == []
        assert _artifact_tree_snapshot(root) == before
    else:
        assert store.lookup_terminal(request=_request_value()) is None
        assert len(creates) == 1
        assert not orphan.exists()
    store.close()


def test_existing_cleanup_receipt_recovers_at_exact_current_capacity_without_create(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    name, orphan = _expired_orphan(store, "1")
    receipt, _size = store._inspect_unbound_temporary(
        name,
        artifacts_module._identity(orphan.stat()),
        time.time_ns(),
    )
    with store._lock():
        store._publish_cleanup_receipt(receipt)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_STORE_BYTES", _ordinary_bytes(root))
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_TEMPORARIES", 2)
    creates = _observe_cleanup_receipt_creates(monkeypatch)

    assert store.lookup_terminal(request=_request_value()) is None

    assert creates == []
    assert not orphan.exists()
    assert not any("cleanup_" in entry.name for entry in root.iterdir())
    store.close()


def test_cleanup_receipt_publish_race_reaches_but_never_exceeds_admitted_peak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    name, orphan = _expired_orphan(store, "0")
    receipt, _size = store._inspect_unbound_temporary(
        name,
        artifacts_module._identity(orphan.stat()),
        time.time_ns(),
    )
    raw = artifacts_module._cleanup_receipt_envelope(receipt)
    byte_cap = _ordinary_bytes(root) + 2 * len(raw)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_STORE_BYTES", byte_cap)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_TEMPORARIES", 3)
    observed_peak: list[tuple[int, int]] = []

    def race_with_matching_final(
        source_parent_fd: int,
        source_name: str,
        destination_parent_fd: int,
        destination_name: str,
    ) -> None:
        del source_parent_fd, source_name, destination_parent_fd
        destination = root / destination_name
        destination.write_bytes(raw)
        destination.chmod(0o600)
        temporary_entries = sum(
            entry.name not in {"requests", "materializations", ".artifact-mutation.lock"}
            for entry in root.iterdir()
        )
        observed_peak.append((_ordinary_bytes(root), temporary_entries))
        raise FileExistsError

    monkeypatch.setattr(
        artifacts_module,
        "_rename_directory_noreplace",
        race_with_matching_final,
    )

    assert store.lookup_terminal(request=_request_value()) is None

    assert observed_peak == [(byte_cap, 3)]
    assert not orphan.exists()
    assert not any("cleanup_" in entry.name for entry in root.iterdir())
    store.close()


def test_corrupt_request_record_stops_scanner_before_orphan_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    _name, orphan = _expired_orphan(store, "f")
    request_path = root / "requests" / store._request_name(_request_value().export_key)
    request_path.write_bytes(b"{")
    request_path.chmod(0o600)
    before = _artifact_tree_snapshot(root)
    creates = _observe_cleanup_receipt_creates(monkeypatch)

    with pytest.raises(ArtifactStoreError) as captured:
        store.lookup_terminal(request=_request_value())

    assert captured.value.code is ArtifactStoreErrorCode.INTEGRITY_FAILURE
    assert creates == []
    assert _artifact_tree_snapshot(root) == before
    assert (orphan / ".creation.json").is_file()
    store.close()


def test_orphan_marker_unlink_then_rmdir_failure_resumes_from_durable_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    eligibility = _eligibility_with_sizes(1024, 1024)
    name = f".{artifacts_module._materialization_id(eligibility)}.{'9' * 32}.tmp"
    orphan = root / name
    orphan.mkdir(mode=0o700)
    created_ns = time.time_ns() - (86_400 + 1) * 1_000_000_000
    marker = orphan / ".creation.json"
    marker.write_bytes(_orphan_marker_bytes(name, created_ns, orphan.stat()))
    marker.chmod(0o600)
    real_rmdir = artifacts_module.os.rmdir

    def fail_target(path, *, dir_fd=None):
        if path == name:
            raise OSError
        return real_rmdir(path, dir_fd=dir_fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifacts_module.os, "rmdir", fail_target)
        with pytest.raises(ArtifactStoreError) as captured:
            store.lookup_terminal(request=_request_value())

    assert captured.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED
    assert orphan.is_dir()
    assert list(orphan.iterdir()) == []
    receipts = tuple(entry for entry in root.iterdir() if entry.name.startswith("cleanup_"))
    assert len(receipts) == 1
    store.close()

    restarted = ArtifactStore(root=root)
    assert restarted.lookup_terminal(request=_request_value()) is None
    assert not orphan.exists()
    assert not receipts[0].exists()
    restarted.close()


@pytest.mark.parametrize(
    "boundary",
    ["create", "write", "partial_write", "file_fsync", "publish_fsync"],
)
def test_cleanup_receipt_publication_crash_boundaries_converge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    _name, orphan = _expired_orphan(store, "8")
    real_open = artifacts_module.os.open
    real_write = artifacts_module.os.write
    real_fsync = artifacts_module.os.fsync
    real_store_fsync = artifacts_module._fsync_fd
    failed = False
    write_calls = 0

    def fail_receipt_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal failed
        if boundary == "create" and not failed and str(path).startswith(".cleanup_"):
            failed = True
            raise OSError
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def fail_receipt_write(fd: int, data: bytes) -> int:
        nonlocal failed, write_calls
        if boundary == "write" and not failed:
            failed = True
            raise OSError
        if boundary == "partial_write":
            write_calls += 1
            if write_calls == 1:
                return real_write(fd, data[: len(data) // 2])
            if not failed:
                failed = True
                raise OSError
        return real_write(fd, data)

    def fail_receipt_fsync(fd: int) -> None:
        nonlocal failed
        if boundary == "file_fsync" and not failed and stat.S_ISREG(os.fstat(fd).st_mode):
            failed = True
            raise OSError
        real_fsync(fd)

    def fail_publish_fsync(fd: int) -> None:
        nonlocal failed
        published = any(entry.name.startswith("cleanup_") for entry in root.iterdir())
        if boundary == "publish_fsync" and not failed and published:
            failed = True
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        real_store_fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifacts_module.os, "open", fail_receipt_open)
        scoped.setattr(artifacts_module.os, "write", fail_receipt_write)
        scoped.setattr(artifacts_module.os, "fsync", fail_receipt_fsync)
        scoped.setattr(artifacts_module, "_fsync_fd", fail_publish_fsync)
        with pytest.raises(ArtifactStoreError) as captured:
            store.lookup_terminal(request=_request_value())

    assert failed is True
    assert captured.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED
    assert orphan.exists()
    store.close()

    restarted = ArtifactStore(root=root)
    assert restarted.lookup_terminal(request=_request_value()) is None
    assert not orphan.exists()
    assert not any("cleanup_" in entry.name for entry in root.iterdir())
    restarted.close()


@pytest.mark.parametrize("boundary", ["marker_unlink", "receipt_unlink"])
def test_cleanup_unlink_crash_boundaries_converge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    boundary: str,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    _name, orphan = _expired_orphan(store, "7")
    real_unlink = artifacts_module.os.unlink
    failed = False

    def fail_unlink(path, *, dir_fd=None):
        nonlocal failed
        is_target = (
            path == ".creation.json"
            if boundary == "marker_unlink"
            else str(path).startswith("cleanup_")
        )
        if not failed and is_target:
            failed = True
            raise OSError
        return real_unlink(path, dir_fd=dir_fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifacts_module.os, "unlink", fail_unlink)
        with pytest.raises(ArtifactStoreError) as captured:
            store.lookup_terminal(request=_request_value())

    assert failed is True
    assert captured.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED
    if boundary == "marker_unlink":
        assert (orphan / ".creation.json").is_file()
    else:
        assert not orphan.exists()
    assert sum(entry.name.startswith("cleanup_") for entry in root.iterdir()) == 1
    store.close()

    restarted = ArtifactStore(root=root)
    assert restarted.lookup_terminal(request=_request_value()) is None
    assert not orphan.exists()
    assert not any("cleanup_" in entry.name for entry in root.iterdir())
    restarted.close()


def test_cleanup_receipt_unlink_then_parent_fsync_failure_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    _name, orphan = _expired_orphan(store, "6")
    real_fsync = artifacts_module._fsync_fd
    failed = False

    def fail_after_receipt_unlink(fd: int) -> None:
        nonlocal failed
        receipt_exists = any(entry.name.startswith("cleanup_") for entry in root.iterdir())
        if not failed and not orphan.exists() and not receipt_exists:
            failed = True
            raise ArtifactStoreError(ArtifactStoreErrorCode.RECOVERY_REQUIRED)
        real_fsync(fd)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifacts_module, "_fsync_fd", fail_after_receipt_unlink)
        with pytest.raises(ArtifactStoreError) as captured:
            store.lookup_terminal(request=_request_value())

    assert failed is True
    assert captured.value.code is ArtifactStoreErrorCode.RECOVERY_REQUIRED
    assert not orphan.exists()
    assert not any("cleanup_" in entry.name for entry in root.iterdir())
    store.close()

    restarted = ArtifactStore(root=root)
    assert restarted.lookup_terminal(request=_request_value()) is None
    restarted.close()


@pytest.mark.parametrize("receipt_kind", ["corrupt", "mismatch", "future"])
def test_cleanup_receipt_corrupt_mismatch_and_future_states_fail_closed(
    tmp_path: Path,
    receipt_kind: str,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(root=root)
    name, orphan = _expired_orphan(store, "5")
    receipt, _size = store._inspect_unbound_temporary(
        name,
        artifacts_module._identity(orphan.stat()),
        time.time_ns(),
    )
    if receipt_kind == "mismatch":
        receipt = artifacts_module._CleanupReceipt(
            phase=receipt.phase,
            temporary_name=receipt.temporary_name,
            directory=artifacts_module._DirectoryBinding(
                dev=receipt.directory.dev,
                ino=receipt.directory.ino + 1,
                uid=receipt.directory.uid,
                mode=receipt.directory.mode,
                nlink=receipt.directory.nlink,
            ),
            marker_sha256=receipt.marker_sha256,
            created_ns=receipt.created_ns,
        )
    elif receipt_kind == "future":
        receipt = artifacts_module._CleanupReceipt(
            phase=receipt.phase,
            temporary_name=receipt.temporary_name,
            directory=receipt.directory,
            marker_sha256=receipt.marker_sha256,
            created_ns=time.time_ns() + 1_000_000_000,
        )
    raw = artifacts_module._cleanup_receipt_envelope(receipt)
    if receipt_kind == "corrupt":
        raw += b"x"
    receipt_path = root / artifacts_module._cleanup_receipt_name(name)
    receipt_path.write_bytes(raw)
    receipt_path.chmod(0o600)

    with pytest.raises(ArtifactStoreError) as captured:
        store.lookup_terminal(request=_request_value())

    expected = (
        ArtifactStoreErrorCode.RECOVERY_REQUIRED
        if receipt_kind == "future"
        else ArtifactStoreErrorCode.INTEGRITY_FAILURE
    )
    assert captured.value.code is expected
    assert receipt_path.is_file()
    assert (orphan / ".creation.json").is_file()
    store.close()


def test_missing_published_materialization_is_integrity_and_resource_unavailable(
    tmp_path: Path,
) -> None:
    api, _service, store, _authority, _cad = _composition(tmp_path)
    first = api.export_task_artifacts(_request())
    assert first["ok"] is True
    artifact = first["result"]["artifacts"][0]
    materialization = store.root / "materializations" / first["result"]["materialization_id"]
    shutil.rmtree(materialization)

    replay = api.export_task_artifacts(_request())

    assert replay["error"]["code"] == "integrity_failure"
    with pytest.raises(ArtifactResourceError) as captured:
        store.read_resource(artifact["resource_uri"])
    assert captured.value.code is ArtifactResourceErrorCode.UNAVAILABLE


# Concrete task/revision/lease authority composition.
AUTHORITY_TASK_ID = "task_0123456789abcdef0123456789abcdef"
AUTHORITY_MISSING_TASK_ID = "task_11111111111111111111111111111111"
AUTHORITY_PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
AUTHORITY_REVISION_ID = "revision_0123456789abcdef0123456789abcdef"
AUTHORITY_BASE_REVISION = "revision_11111111111111111111111111111111"
AUTHORITY_DRAFT_ID = "draft_0123456789abcdef0123456789abcdef"
AUTHORITY_MODEL_ID = "artifact_0123456789abcdef0123456789abcdef"
AUTHORITY_STEP_ID = "artifact_11111111111111111111111111111111"
AUTHORITY_MANIFEST = "c" * 64
AUTHORITY_MODEL_BYTES = b"model-bytes"
AUTHORITY_STEP_BYTES = b"step-bytes"


class AuthorityExplosiveInput:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def _explode(self, name: str):
        self.calls.append(name)
        raise AssertionError("untrusted protocol must not execute")

    def __str__(self) -> str:
        return self._explode("__str__")

    def __eq__(self, other: object) -> bool:
        del other
        return self._explode("__eq__")

    def __iter__(self):
        return self._explode("__iter__")

    def __len__(self) -> int:
        return self._explode("__len__")

    def __index__(self) -> int:
        return self._explode("__index__")

    def __bool__(self) -> bool:
        return self._explode("__bool__")


def _authority_mkdir(path: Path) -> None:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


@pytest.fixture
def artifact_authority_parts(
    tmp_path: Path,
) -> tuple[
    LocalArtifactAuthority,
    TaskRunStore,
    LocalRevisionStore,
    ResourceLeaseManager,
]:
    lock_root = tmp_path / "locks"
    task_root = tmp_path / "tasks"
    revision_root = tmp_path / "revisions"
    for root in (lock_root, task_root, revision_root):
        _authority_mkdir(root)
    leases = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    tasks = TaskRunStore(
        task_root,
        leases,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    revisions = LocalRevisionStore(
        revision_root,
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    authority = LocalArtifactAuthority(
        task_store=tasks,
        revision_store=revisions,
        lease_manager=leases,
    )
    return authority, tasks, revisions, leases


def _authority_task():
    return new_task_run(
        task_id=AUTHORITY_TASK_ID,
        project_id=AUTHORITY_PROJECT_ID,
        base_revision=AUTHORITY_BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )


def _authority_refs() -> tuple[RevisionArtifactRef, RevisionArtifactRef]:
    return (
        RevisionArtifactRef(
            id=AUTHORITY_MODEL_ID,
            name="model.FCStd",
            format="fcstd",
            sha256=hashlib.sha256(AUTHORITY_MODEL_BYTES).hexdigest(),
            size_bytes=len(AUTHORITY_MODEL_BYTES),
        ),
        RevisionArtifactRef(
            id=AUTHORITY_STEP_ID,
            name="model.step",
            format="step",
            sha256=hashlib.sha256(AUTHORITY_STEP_BYTES).hexdigest(),
            size_bytes=len(AUTHORITY_STEP_BYTES),
        ),
    )


def _authority_revision() -> RevisionRef:
    model, step = _authority_refs()
    return RevisionRef(
        id=AUTHORITY_REVISION_ID,
        project_id=AUTHORITY_PROJECT_ID,
        base_revision=AUTHORITY_BASE_REVISION,
        manifest_sha256=AUTHORITY_MANIFEST,
        model=model,
        artifacts=(step,),
    )


def _authority_eligibility(*, draft: bool = False) -> ArtifactEligibility:
    return ArtifactEligibility(
        source_kind=ArtifactSourceKind.DRAFT if draft else ArtifactSourceKind.COMMITTED,
        task_id=AUTHORITY_TASK_ID,
        task_generation=0,
        project_id=AUTHORITY_PROJECT_ID,
        revision_id=AUTHORITY_REVISION_ID,
        manifest_sha256=AUTHORITY_MANIFEST,
        draft_id=AUTHORITY_DRAFT_ID if draft else None,
        artifacts=_authority_refs(),
    )


def _authority_failure(code: ArtifactDependencyErrorCode) -> ArtifactDependencyFailure:
    return ArtifactDependencyFailure(code=code)


def _authority_task_error(code: TaskStoreErrorCode) -> TaskStoreError:
    if code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
        return TaskStoreError(code, committed_generation=1)
    return TaskStoreError(code)


def _authority_revision_error(code: RevisionStoreErrorCode) -> RevisionStoreError:
    if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
        return RevisionStoreError(code, head_committed=True)
    return RevisionStoreError(code)


def test_constructor_requires_exact_local_dependencies_before_reflection(
    artifact_authority_parts,
) -> None:
    _authority, tasks, revisions, leases = artifact_authority_parts
    explosive = AuthorityExplosiveInput()
    for kwargs in (
        dict(task_store=explosive, revision_store=revisions, lease_manager=leases),
        dict(task_store=tasks, revision_store=explosive, lease_manager=leases),
        dict(task_store=tasks, revision_store=revisions, lease_manager=explosive),
    ):
        with pytest.raises(TypeError):
            LocalArtifactAuthority(**kwargs)  # type: ignore[arg-type]
    assert explosive.calls == []


def test_constructor_requires_stores_and_authority_to_share_exact_lease_manager(
    artifact_authority_parts,
    tmp_path: Path,
) -> None:
    _authority, tasks, revisions, leases = artifact_authority_parts
    other_lock_root = tmp_path / "other-locks"
    _authority_mkdir(other_lock_root)
    other_leases = ResourceLeaseManager(
        other_lock_root,
        trust=LeaseRootTrust.TRUSTED_LOCAL,
    )

    with pytest.raises(TypeError):
        LocalArtifactAuthority(
            task_store=tasks,
            revision_store=revisions,
            lease_manager=other_leases,
        )

    same_manager = LocalArtifactAuthority(
        task_store=tasks,
        revision_store=revisions,
        lease_manager=leases,
    )
    assert type(same_manager) is LocalArtifactAuthority


def test_real_task_store_exists_and_load_are_exact(artifact_authority_parts) -> None:
    authority, tasks, _revisions, _leases = artifact_authority_parts

    assert authority.task_exists(task_id=AUTHORITY_TASK_ID) is False
    assert authority.load_task(task_id=AUTHORITY_TASK_ID) == _authority_failure(
        ArtifactDependencyErrorCode.NOT_FOUND
    )
    created = tasks.create(_authority_task())

    assert authority.task_exists(task_id=AUTHORITY_TASK_ID) is True
    loaded = authority.load_task(task_id=AUTHORITY_TASK_ID)
    assert type(loaded) is StoredTaskRun
    assert loaded == created


def test_forged_task_ids_have_zero_lock_tree_effect_and_no_protocol_dispatch(
    artifact_authority_parts,
    tmp_path: Path,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    lock_root = tmp_path / "locks"
    before = tuple(sorted(path.name for path in lock_root.iterdir()))
    explosive = AuthorityExplosiveInput()

    assert authority.task_exists(task_id=explosive) == _authority_failure(  # type: ignore[arg-type]
        ArtifactDependencyErrorCode.INTERNAL_ERROR
    )
    assert authority.load_task(task_id=explosive) == _authority_failure(  # type: ignore[arg-type]
        ArtifactDependencyErrorCode.INTERNAL_ERROR
    )
    with pytest.raises(ArtifactDependencyError) as caught:
        authority.acquire_export_gate(task_id=explosive)  # type: ignore[arg-type]

    assert caught.value.code is ArtifactDependencyErrorCode.INTERNAL_ERROR
    assert explosive.calls == []
    assert tuple(sorted(path.name for path in lock_root.iterdir())) == before


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        (TaskStoreErrorCode.INVALID_ID, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (TaskStoreErrorCode.ALREADY_EXISTS, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (TaskStoreErrorCode.CONFLICT, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (TaskStoreErrorCode.CORRUPT_RECORD, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (TaskStoreErrorCode.RECORD_TOO_LARGE, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (TaskStoreErrorCode.UNSAFE_STORE, ArtifactDependencyErrorCode.STORE_FAILURE),
        (TaskStoreErrorCode.LOCK_UNAVAILABLE, ArtifactDependencyErrorCode.LEASE_UNAVAILABLE),
        (TaskStoreErrorCode.IO_ERROR, ArtifactDependencyErrorCode.STORE_FAILURE),
        (TaskStoreErrorCode.DURABILITY_UNCERTAIN, ArtifactDependencyErrorCode.RECOVERY_REQUIRED),
        (TaskStoreErrorCode.RESOURCE_EXHAUSTED, ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED),
    ),
)
def test_task_store_error_taxonomy_is_closed(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    code: TaskStoreErrorCode,
    expected: ArtifactDependencyErrorCode,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    monkeypatch.setattr(
        TaskRunStore,
        "load",
        lambda self, task_id: (_ for _ in ()).throw(_authority_task_error(code)),
    )

    assert authority.task_exists(task_id=AUTHORITY_TASK_ID) == _authority_failure(expected)
    assert authority.load_task(task_id=AUTHORITY_TASK_ID) == _authority_failure(expected)


@pytest.mark.parametrize("raised", (RuntimeError("private"), KeyboardInterrupt(), SystemExit()))
def test_task_store_unknown_throwables_never_escape(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    raised: BaseException,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    monkeypatch.setattr(
        TaskRunStore,
        "load",
        lambda self, task_id: (_ for _ in ()).throw(raised),
    )

    assert authority.task_exists(task_id=AUTHORITY_TASK_ID) == _authority_failure(
        ArtifactDependencyErrorCode.INTERNAL_ERROR
    )
    assert authority.load_task(task_id=AUTHORITY_TASK_ID) == _authority_failure(
        ArtifactDependencyErrorCode.INTERNAL_ERROR
    )


def test_corrupt_exact_stored_task_is_integrity_without_hostile_dispatch(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    task = _authority_task()
    stored = StoredTaskRun(generation=0, task_run=task)
    explosive = AuthorityExplosiveInput()
    object.__setattr__(task, "id", explosive)
    monkeypatch.setattr(TaskRunStore, "load", lambda self, task_id: stored)

    assert authority.task_exists(task_id=AUTHORITY_TASK_ID) == _authority_failure(
        ArtifactDependencyErrorCode.INTEGRITY_FAILURE
    )
    assert authority.load_task(task_id=AUTHORITY_TASK_ID) == _authority_failure(
        ArtifactDependencyErrorCode.INTEGRITY_FAILURE
    )
    assert explosive.calls == []


def test_real_per_task_export_gate_is_bounded_and_contended(artifact_authority_parts) -> None:
    authority, tasks, _revisions, _leases = artifact_authority_parts
    tasks.create(_authority_task())

    with authority.acquire_export_gate(task_id=AUTHORITY_TASK_ID) as entered:
        assert entered is None
        with pytest.raises(ArtifactDependencyError) as caught:
            with authority.acquire_export_gate(task_id=AUTHORITY_TASK_ID):
                raise AssertionError("contended gate body must not execute")
        assert caught.value.code is ArtifactDependencyErrorCode.LEASE_UNAVAILABLE

    with authority.acquire_export_gate(task_id=AUTHORITY_TASK_ID):
        pass


def test_missing_task_gate_never_creates_caller_derived_lease(
    artifact_authority_parts, tmp_path
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    lock_root = tmp_path / "locks"
    before = tuple(sorted((path.name, path.stat().st_ino) for path in lock_root.iterdir()))

    with pytest.raises(ArtifactDependencyError) as caught:
        with authority.acquire_export_gate(task_id=AUTHORITY_MISSING_TASK_ID):
            raise AssertionError("missing task gate body must not execute")

    assert caught.value.code is ArtifactDependencyErrorCode.NOT_FOUND
    assert tuple(sorted((path.name, path.stat().st_ino) for path in lock_root.iterdir())) == before


def test_gate_acquire_and_release_failures_are_sanitized(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, tasks, _revisions, leases = artifact_authority_parts
    tasks.create(_authority_task())
    original_acquire = ResourceLeaseManager.acquire

    def contended(self, resource_id):
        if resource_id == f"artifact-export:{AUTHORITY_TASK_ID}":
            raise LeaseError(LeaseErrorCode.CONTENDED)
        return original_acquire(self, resource_id)

    monkeypatch.setattr(ResourceLeaseManager, "acquire", contended)
    with pytest.raises(ArtifactDependencyError) as acquired:
        with authority.acquire_export_gate(task_id=AUTHORITY_TASK_ID):
            pass
    assert acquired.value.code is ArtifactDependencyErrorCode.LEASE_UNAVAILABLE

    monkeypatch.setattr(ResourceLeaseManager, "acquire", original_acquire)
    original_release = ResourceLeaseManager.release

    def release_then_fail(self, lease, *, owner_token):
        original_release(self, lease, owner_token=owner_token)
        raise LeaseError(LeaseErrorCode.IO_ERROR)

    monkeypatch.setattr(ResourceLeaseManager, "release", release_then_fail)
    with pytest.raises(ArtifactDependencyError) as released:
        with authority.acquire_export_gate(task_id=AUTHORITY_TASK_ID):
            pass
    assert released.value.code is ArtifactDependencyErrorCode.STORE_FAILURE


def test_real_revision_store_load_is_forwarded(artifact_authority_parts) -> None:
    authority, _tasks, revisions, leases = artifact_authority_parts
    with leases.acquire_project_write(AUTHORITY_PROJECT_ID) as lease:
        head = revisions.initialize_empty_project(AUTHORITY_PROJECT_ID, lease)

    loaded = authority.load_revision(project_id=AUTHORITY_PROJECT_ID, revision_id=head.revision_id)

    assert type(loaded) is RevisionRef
    assert loaded.id == head.revision_id
    assert loaded.project_id == AUTHORITY_PROJECT_ID
    assert authority.load_revision(
        project_id=AUTHORITY_PROJECT_ID, revision_id=AUTHORITY_REVISION_ID
    ) == _authority_failure(ArtifactDependencyErrorCode.NOT_FOUND)


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        (RevisionStoreErrorCode.INVALID_IDENTIFIER, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (RevisionStoreErrorCode.INVALID_INPUT, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (RevisionStoreErrorCode.NOT_FOUND, ArtifactDependencyErrorCode.NOT_FOUND),
        (RevisionStoreErrorCode.ALREADY_EXISTS, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (RevisionStoreErrorCode.CONFLICT, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (RevisionStoreErrorCode.CORRUPT_RECORD, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (RevisionStoreErrorCode.CORRUPT_CONTENT, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (RevisionStoreErrorCode.BUDGET_EXCEEDED, ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED),
        (RevisionStoreErrorCode.RESOURCE_EXHAUSTED, ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED),
        (RevisionStoreErrorCode.UNSAFE_STORE, ArtifactDependencyErrorCode.STORE_FAILURE),
        (RevisionStoreErrorCode.INVALID_LEASE, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (RevisionStoreErrorCode.IO_ERROR, ArtifactDependencyErrorCode.STORE_FAILURE),
        (
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            ArtifactDependencyErrorCode.RECOVERY_REQUIRED,
        ),
        (RevisionStoreErrorCode.RECOVERY_REQUIRED, ArtifactDependencyErrorCode.RECOVERY_REQUIRED),
        (RevisionStoreErrorCode.CLEANUP_REQUIRED, ArtifactDependencyErrorCode.RECOVERY_REQUIRED),
    ),
)
def test_revision_store_error_taxonomy_is_closed(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    code: RevisionStoreErrorCode,
    expected: ArtifactDependencyErrorCode,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: (_ for _ in ()).throw(
            _authority_revision_error(code)
        ),
    )

    assert authority.load_revision(
        project_id=AUTHORITY_PROJECT_ID, revision_id=AUTHORITY_REVISION_ID
    ) == _authority_failure(expected)


def test_revision_load_rejects_hostile_inputs_before_reflection(artifact_authority_parts) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    explosive = AuthorityExplosiveInput()

    assert authority.load_revision(
        project_id=explosive,  # type: ignore[arg-type]
        revision_id=AUTHORITY_REVISION_ID,
    ) == _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
    assert authority.load_revision(
        project_id=AUTHORITY_PROJECT_ID,
        revision_id=explosive,  # type: ignore[arg-type]
    ) == _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
    assert explosive.calls == []


def test_corrupt_exact_revision_output_is_integrity_without_hostile_dispatch(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    revision = _authority_revision()
    explosive = AuthorityExplosiveInput()
    object.__setattr__(revision, "project_id", explosive)
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: revision,
    )

    assert authority.load_revision(
        project_id=AUTHORITY_PROJECT_ID,
        revision_id=AUTHORITY_REVISION_ID,
    ) == _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
    assert authority.copy_authoritative(
        eligibility=_authority_eligibility(),
        destination_directory_fd=0,
        cursors=(),
        chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
    ) == _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
    assert explosive.calls == []


@pytest.mark.parametrize("draft", (False, True))
def test_real_revision_store_copy_supports_committed_and_sealed_draft_authority(
    artifact_authority_parts,
    tmp_path: Path,
    draft: bool,
) -> None:
    authority, _tasks, revisions, leases = artifact_authority_parts
    with leases.acquire_project_write(AUTHORITY_PROJECT_ID) as lease:
        head = revisions.initialize_empty_project(AUTHORITY_PROJECT_ID, lease)
        revision_id = revisions.begin_revision(AUTHORITY_PROJECT_ID, head, lease)
        model_path = revisions.candidate_model_path(AUTHORITY_PROJECT_ID, revision_id, lease)
        step_path = revisions.candidate_artifact_path(
            AUTHORITY_PROJECT_ID, revision_id, "step", lease
        )
        model_path.write_bytes(AUTHORITY_MODEL_BYTES)
        step_path.write_bytes(AUTHORITY_STEP_BYTES)
        sealed = revisions.seal_revision(AUTHORITY_PROJECT_ID, revision_id, lease)
        if not draft:
            committed = revisions.commit_revision(AUTHORITY_PROJECT_ID, head, revision_id, lease)
            assert committed.revision_id == revision_id
    assert sealed.model is not None
    assert type(sealed.artifacts) is tuple and len(sealed.artifacts) == 1
    eligibility = ArtifactEligibility(
        source_kind=ArtifactSourceKind.DRAFT if draft else ArtifactSourceKind.COMMITTED,
        task_id=AUTHORITY_TASK_ID,
        task_generation=0,
        project_id=AUTHORITY_PROJECT_ID,
        revision_id=revision_id,
        manifest_sha256=sealed.manifest_sha256,
        draft_id=AUTHORITY_DRAFT_ID if draft else None,
        artifacts=(sealed.model, sealed.artifacts[0]),
    )
    destination = tmp_path / "delivery"
    _authority_mkdir(destination)
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        assert (
            authority.copy_authoritative(
                eligibility=eligibility,
                destination_directory_fd=fd,
                cursors=(),
                chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
            )
            is None
        )
        os.fstat(fd)
    finally:
        os.close(fd)

    assert (destination / "model.FCStd").read_bytes() == AUTHORITY_MODEL_BYTES
    assert (destination / "model.step").read_bytes() == AUTHORITY_STEP_BYTES
    assert (destination / "model.FCStd").stat().st_nlink == 1
    assert (destination / "model.step").stat().st_nlink == 1


@pytest.mark.parametrize("draft", (False, True))
def test_copy_authoritative_reloads_exact_revision_converts_cursors_and_borrows_fd(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    draft: bool,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    revision = _authority_revision()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: revision,
    )

    def copy(
        self,
        *,
        expected_revision,
        destination_directory_fd,
        cursors,
        chunk_bytes,
    ):
        calls.append(
            (
                expected_revision,
                destination_directory_fd,
                cursors,
                chunk_bytes,
            )
        )

    monkeypatch.setattr(LocalRevisionStore, "copy_revision_artifacts_at", copy)
    destination = tmp_path / "destination"
    _authority_mkdir(destination)
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    cursors = (
        ArtifactCopyCursor(
            name="model.FCStd",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )
    try:
        assert (
            authority.copy_authoritative(
                eligibility=_authority_eligibility(draft=draft),
                destination_directory_fd=fd,
                cursors=cursors,
                chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
            )
            is None
        )
        os.fstat(fd)
    finally:
        os.close(fd)

    assert len(calls) == 1
    expected_revision, passed_fd, converted, chunk = calls[0]
    assert expected_revision is revision
    assert passed_fd == fd
    assert converted == (
        RevisionCopyCursor(
            name="model.FCStd",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )
    assert chunk == ARTIFACT_COPY_CHUNK_BYTES


@pytest.mark.parametrize("mismatch", ("project", "revision", "manifest", "model", "step"))
def test_copy_authoritative_rejects_loaded_revision_binding_drift(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mismatch: str,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    revision = _authority_revision()
    model, step = _authority_refs()
    values = {
        "id": AUTHORITY_REVISION_ID,
        "project_id": AUTHORITY_PROJECT_ID,
        "base_revision": AUTHORITY_BASE_REVISION,
        "manifest_sha256": AUTHORITY_MANIFEST,
        "model": model,
        "artifacts": (step,),
    }
    if mismatch == "project":
        values["project_id"] = "project_11111111111111111111111111111111"
    elif mismatch == "revision":
        values["id"] = "revision_22222222222222222222222222222222"
    elif mismatch == "manifest":
        values["manifest_sha256"] = "d" * 64
    elif mismatch == "model":
        values["model"] = RevisionArtifactRef(
            id=model.id,
            name=model.name,
            format=model.format,
            sha256="d" * 64,
            size_bytes=model.size_bytes,
        )
    else:
        values["artifacts"] = (
            RevisionArtifactRef(
                id=step.id,
                name=step.name,
                format=step.format,
                sha256="d" * 64,
                size_bytes=step.size_bytes,
            ),
        )
    drifted = RevisionRef(**values)
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: drifted,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        LocalRevisionStore,
        "copy_revision_artifacts_at",
        lambda self, **kwargs: calls.append("copy"),
    )
    destination = tmp_path / "destination"
    _authority_mkdir(destination)
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        result = authority.copy_authoritative(
            eligibility=_authority_eligibility(),
            destination_directory_fd=fd,
            cursors=(),
            chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
        )
    finally:
        os.close(fd)

    assert revision != drifted
    assert result == _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
    assert calls == []


@pytest.mark.parametrize(
    "field",
    ("eligibility", "destination", "cursors", "cursor_item", "chunk"),
)
def test_copy_authoritative_rejects_wrong_exact_types_before_reflection(
    artifact_authority_parts,
    tmp_path: Path,
    field: str,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    explosive = AuthorityExplosiveInput()
    eligibility: object = _authority_eligibility()
    destination: object = 0
    cursors: object = ()
    chunk: object = ARTIFACT_COPY_CHUNK_BYTES
    if field == "eligibility":
        eligibility = explosive
    elif field == "destination":
        destination = explosive
    elif field == "cursors":
        cursors = explosive
    elif field == "cursor_item":
        cursors = (explosive,)
    else:
        chunk = explosive

    assert authority.copy_authoritative(
        eligibility=eligibility,  # type: ignore[arg-type]
        destination_directory_fd=destination,  # type: ignore[arg-type]
        cursors=cursors,  # type: ignore[arg-type]
        chunk_bytes=chunk,  # type: ignore[arg-type]
    ) == _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
    assert explosive.calls == []


@pytest.mark.parametrize(
    ("destination_fd", "chunk_bytes"),
    ((-1, ARTIFACT_COPY_CHUNK_BYTES), (0, 0), (0, ARTIFACT_COPY_CHUNK_BYTES - 1)),
)
def test_copy_authoritative_rejects_noncanonical_fd_or_chunk_before_store(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    destination_fd: int,
    chunk_bytes: int,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    calls: list[str] = []
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: calls.append("load"),
    )

    assert authority.copy_authoritative(
        eligibility=_authority_eligibility(),
        destination_directory_fd=destination_fd,
        cursors=(),
        chunk_bytes=chunk_bytes,
    ) == _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
    assert calls == []


@pytest.mark.parametrize(
    "cursor",
    (
        ArtifactCopyCursor(name="other", size_bytes=0, sha256="a" * 64),
        ArtifactCopyCursor(name="model.FCStd", size_bytes=-1, sha256="a" * 64),
        ArtifactCopyCursor(
            name="model.FCStd", size_bytes=len(AUTHORITY_MODEL_BYTES) + 1, sha256="a" * 64
        ),
        ArtifactCopyCursor(name="model.FCStd", size_bytes=0, sha256="A" * 64),
    ),
)
def test_copy_authoritative_rejects_malformed_cursor_without_store_dispatch(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    cursor: ArtifactCopyCursor,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    calls: list[str] = []
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: calls.append("load"),
    )

    result = authority.copy_authoritative(
        eligibility=_authority_eligibility(),
        destination_directory_fd=0,
        cursors=(cursor,),
        chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
    )

    assert result == _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
    assert calls == []


def test_copy_authoritative_rejects_step_cursor_after_partial_model_before_store(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    calls: list[str] = []
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: calls.append("load"),
    )
    monkeypatch.setattr(
        LocalRevisionStore,
        "copy_revision_artifacts_at",
        lambda self, **kwargs: calls.append("copy"),
    )
    model_prefix = AUTHORITY_MODEL_BYTES[:1]
    cursors = (
        ArtifactCopyCursor(
            name="model.FCStd",
            size_bytes=len(model_prefix),
            sha256=hashlib.sha256(model_prefix).hexdigest(),
        ),
        ArtifactCopyCursor(
            name="model.step",
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
        ),
    )

    result = authority.copy_authoritative(
        eligibility=_authority_eligibility(),
        destination_directory_fd=0,
        cursors=cursors,
        chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
    )

    assert result == _authority_failure(ArtifactDependencyErrorCode.INTEGRITY_FAILURE)
    assert calls == []


@pytest.mark.parametrize(
    ("code", "expected"),
    (
        (RevisionStoreErrorCode.INVALID_INPUT, ArtifactDependencyErrorCode.INTERNAL_ERROR),
        (RevisionStoreErrorCode.CONFLICT, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (RevisionStoreErrorCode.CORRUPT_RECORD, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (RevisionStoreErrorCode.CORRUPT_CONTENT, ArtifactDependencyErrorCode.INTEGRITY_FAILURE),
        (RevisionStoreErrorCode.RESOURCE_EXHAUSTED, ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED),
        (RevisionStoreErrorCode.UNSAFE_STORE, ArtifactDependencyErrorCode.STORE_FAILURE),
        (RevisionStoreErrorCode.IO_ERROR, ArtifactDependencyErrorCode.STORE_FAILURE),
        (RevisionStoreErrorCode.RECOVERY_REQUIRED, ArtifactDependencyErrorCode.RECOVERY_REQUIRED),
    ),
)
def test_copy_store_error_taxonomy_and_borrowed_fd(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    code: RevisionStoreErrorCode,
    expected: ArtifactDependencyErrorCode,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: _authority_revision(),
    )
    monkeypatch.setattr(
        LocalRevisionStore,
        "copy_revision_artifacts_at",
        lambda self, **kwargs: (_ for _ in ()).throw(_authority_revision_error(code)),
    )
    destination = tmp_path / "destination"
    _authority_mkdir(destination)
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        result = authority.copy_authoritative(
            eligibility=_authority_eligibility(),
            destination_directory_fd=fd,
            cursors=(),
            chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
        )
        os.fstat(fd)
    finally:
        os.close(fd)

    assert result == _authority_failure(expected)


def test_copy_unknown_throwables_are_internal_and_fd_remains_borrowed(
    artifact_authority_parts,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    authority, _tasks, _revisions, _leases = artifact_authority_parts
    monkeypatch.setattr(
        LocalRevisionStore,
        "load_revision",
        lambda self, project_id, revision_id: _authority_revision(),
    )
    monkeypatch.setattr(
        LocalRevisionStore,
        "copy_revision_artifacts_at",
        lambda self, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    destination = tmp_path / "destination"
    _authority_mkdir(destination)
    fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        assert authority.copy_authoritative(
            eligibility=_authority_eligibility(),
            destination_directory_fd=fd,
            cursors=(),
            chunk_bytes=ARTIFACT_COPY_CHUNK_BYTES,
        ) == _authority_failure(ArtifactDependencyErrorCode.INTERNAL_ERROR)
        os.fstat(fd)
    finally:
        os.close(fd)
