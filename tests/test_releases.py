"""Revision-bound CAD release package and approval tests."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest

import vibecad.application.releases as releases
from vibecad import _file_compat
from vibecad.application.releases import (
    ReleaseApi,
    ReleaseError,
    ReleaseErrorCode,
    ReleaseService,
    ReleaseStatus,
    ReleaseStore,
)
from vibecad.execution.revisions import (
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.interaction.cad import CadExecutionPort, ReleaseCadEvidence
from vibecad.validation import BomObservation, BomRowObservation
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.state import (
    CriterionOutcome,
    CriterionVerdict,
    ReasoningOwner,
    ReviewPolicy,
    TaskArtifactRef,
    TaskEvent,
    VerificationReport,
    append_artifact,
    new_task_run,
    transition_task,
)
from vibecad.workflow.store import StoredTaskRun, TaskStoreError, TaskStoreErrorCode

TASK_ID = "task_0123456789abcdef0123456789abcdef"
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
REVISION_ID = "revision_11111111111111111111111111111111"
MODEL_ID = "artifact_0123456789abcdef0123456789abcdef"
STEP_ID = "artifact_11111111111111111111111111111111"
CREATE_KEY = "release_create_0123456789abcdef0123456789abcdef"
APPROVAL_KEY = "release_approve_0123456789abcdef0123456789abcdef"
GENERATION = 17
MANIFEST = "a" * 64
MODEL_BYTES = b"PK\x03\x04immutable-fcstd"
STEP_BYTES = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PDF_BYTES = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
COMPONENT_ID = "object_11111111111111111111111111111111"


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
        acceptance_id="release-acceptance",
        candidate_revision=REVISION_ID,
        manifest_sha256=MANIFEST,
        observation_digest="b" * 64,
        passed=True,
        verdicts=(
            CriterionVerdict(
                criterion_id="release",
                required=True,
                outcome=CriterionOutcome.PASS,
                message="Release source passed.",
            ),
        ),
    )


def _task() -> StoredTaskRun:
    task = new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    task = transition_task(
        task,
        TaskEvent.SUBMIT_PROGRAM,
        program=ModelProgram(
            task_id=TASK_ID,
            base_revision=BASE_REVISION,
            operations=(),
            acceptance=AcceptanceSpec(id="release-acceptance", criteria=()),
        ),
    )
    task = transition_task(task, TaskEvent.START_VALIDATION)
    task = transition_task(task, TaskEvent.VALIDATE_PROGRAM, candidate_revision=REVISION_ID)
    for reference in _refs():
        task = append_artifact(
            task,
            TaskArtifactRef(
                id=reference.id,
                name=reference.name,
                format=reference.format,
                sha256=reference.sha256,
                size_bytes=reference.size_bytes,
                candidate_revision=REVISION_ID,
            ),
        )
    task = transition_task(task, TaskEvent.COMPLETE_EXECUTION)
    task = transition_task(task, TaskEvent.PASS_VERIFICATION, verification=_report())
    task = transition_task(task, TaskEvent.COMMIT, committed_revision=REVISION_ID)
    return StoredTaskRun(generation=GENERATION, task_run=task)


def _bom() -> BomObservation:
    return BomObservation(
        component_count=1,
        rows=(
            BomRowObservation(
                part_number="BRACKET-001",
                description="Mounting bracket",
                material="Aluminum 6061",
                density_kg_m3=2700,
                quantity=1,
                unit_mass_kg=0.0027,
                total_mass_kg=0.0027,
                component_ids=(COMPONENT_ID,),
                geometry_digest="1" * 64,
            ),
        ),
        total_quantity=1,
        total_mass_kg=0.0027,
        complete=True,
    )


class _TaskStore:
    def __init__(self) -> None:
        self.stored = _task()

    def load(self, task_id: str) -> StoredTaskRun:
        if task_id != TASK_ID:
            raise KeyError(task_id)
        return self.stored


class _RevisionStore:
    def __init__(self, source: Path) -> None:
        self.revision = _revision()
        self.model = source / "model.FCStd"
        self.step = source / "model.step"
        self.model.write_bytes(MODEL_BYTES)
        self.step.write_bytes(STEP_BYTES)
        os.chmod(self.model, 0o600)
        os.chmod(self.step, 0o600)
        if sys.platform == "win32":
            _file_compat.set_private_dacl(self.model)
            _file_compat.set_private_dacl(self.step)

    def load_revision(self, project_id: str, revision_id: str) -> RevisionRef:
        if (project_id, revision_id) != (PROJECT_ID, REVISION_ID):
            raise KeyError((project_id, revision_id))
        return self.revision

    def revision_model_path(self, project_id: str, revision_id: str) -> Path:
        self.load_revision(project_id, revision_id)
        return self.model

    def revision_artifact_path(
        self,
        project_id: str,
        revision_id: str,
        artifact_id: str,
    ) -> Path:
        self.load_revision(project_id, revision_id)
        if artifact_id != STEP_ID:
            raise KeyError(artifact_id)
        return self.step


class _Cad(CadExecutionPort):
    def __init__(self) -> None:
        self.calls: list[RevisionRef] = []

    def render_release(self, *, revision: RevisionRef) -> ReleaseCadEvidence:
        self.calls.append(revision)
        return ReleaseCadEvidence(
            revision_id=revision.id,
            bom=_bom(),
            drawing_pdf=PDF_BYTES,
            view_names=("front", "right", "top", "isometric"),
            balloon_items=((1, COMPONENT_ID),),
        )


def _composition(tmp_path: Path):
    root = tmp_path / "releases"
    root.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    if sys.platform == "win32":
        _file_compat.set_private_dacl(root)
        _file_compat.set_private_dacl(source)
    value = root.lstat()
    store = ReleaseStore(root=root, expected_identity=(value.st_dev, value.st_ino))
    cad = _Cad()
    service = ReleaseService(
        store=store,
        task_store=_TaskStore(),
        revision_store=_RevisionStore(source),
        cad=cad,
    )
    return ReleaseApi(service=service), service, store, cad


def _create_request() -> dict[str, object]:
    return {
        "schema_version": 1,
        "create_key": CREATE_KEY,
        "task_id": TASK_ID,
        "expected_generation": GENERATION,
        "revision_id": REVISION_ID,
    }


def test_release_package_is_exact_idempotent_and_approval_gated(tmp_path: Path) -> None:
    api, _service, store, cad = _composition(tmp_path)

    created = api.create_release(_create_request())
    replay = api.create_release(_create_request())

    assert created == replay
    assert created["ok"] is True
    result = created["result"]
    assert result["status"] == ReleaseStatus.DRAFT
    assert result["package"]["resource_uri"] is None
    assert len(cad.calls) == 1
    drawing = store.read_resource(result["drawing"]["resource_uri"])
    assert drawing.data == PDF_BYTES
    with pytest.raises(ReleaseError) as unavailable:
        store.read_resource(f"vibecad://release/{result['release_id']}/vibecad-release.zip")
    assert unavailable.value.code is ReleaseErrorCode.INVALID_STATE

    denied = api.approve_release(
        {
            "schema_version": 1,
            "release_id": result["release_id"],
            "expected_generation": 0,
            "expected_package_sha256": "f" * 64,
            "approval_key": APPROVAL_KEY,
        }
    )
    assert denied["error"]["code"] == "conflict"

    approved = api.approve_release(
        {
            "schema_version": 1,
            "release_id": result["release_id"],
            "expected_generation": 0,
            "expected_package_sha256": result["package"]["sha256"],
            "approval_key": APPROVAL_KEY,
        }
    )
    assert approved["result"]["status"] == ReleaseStatus.APPROVED
    assert approved["result"]["generation"] == 1
    package = store.read_resource(approved["result"]["package"]["resource_uri"])
    with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
        assert archive.namelist() == [
            "model.FCStd",
            "model.step",
            "bom.json",
            "bom.csv",
            "assembly-drawing.pdf",
            "manifest.json",
            "validation-report.json",
        ]
        assert archive.read("model.FCStd") == MODEL_BYTES
        assert archive.read("model.step") == STEP_BYTES
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["revision_id"] == REVISION_ID
        assert manifest["drawing"]["balloon_items"] == [[1, COMPONENT_ID]]
        bom = json.loads(archive.read("bom.json"))
        assert bom["items"][0]["item_number"] == 1


def test_release_store_detects_package_tampering(tmp_path: Path) -> None:
    api, _service, store, _cad = _composition(tmp_path)
    created = api.create_release(_create_request())
    release_id = created["result"]["release_id"]
    package = store._directory(release_id) / "vibecad-release.zip"  # noqa: SLF001
    raw = package.read_bytes()
    package.write_bytes(raw[:-1] + bytes((raw[-1] ^ 1,)))
    os.chmod(package, 0o600)

    with pytest.raises(ReleaseError) as caught:
        store.load(release_id)

    assert caught.value.code is ReleaseErrorCode.INTEGRITY_FAILURE


def test_release_store_detects_validation_report_tampering(tmp_path: Path) -> None:
    api, _service, store, _cad = _composition(tmp_path)
    created = api.create_release(_create_request())
    release_id = created["result"]["release_id"]
    report = store._directory(release_id) / "validation-report.json"  # noqa: SLF001
    report.write_bytes(b'{"passed":false}')
    os.chmod(report, 0o600)

    with pytest.raises(ReleaseError) as caught:
        store.load(release_id)

    assert caught.value.code is ReleaseErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("source", ["task", "revision"])
def test_release_source_corruption_is_not_flattened_to_not_found(
    tmp_path: Path,
    source: str,
) -> None:
    api, service, _store, cad = _composition(tmp_path)

    class CorruptTaskStore:
        def load(self, _task_id: str) -> StoredTaskRun:
            raise TaskStoreError(TaskStoreErrorCode.CORRUPT_RECORD)

    class CorruptRevisionStore:
        def load_revision(self, _project_id: str, _revision_id: str) -> RevisionRef:
            raise RevisionStoreError(RevisionStoreErrorCode.CORRUPT_CONTENT)

    if source == "task":
        service._task_store = CorruptTaskStore()  # noqa: SLF001
    else:
        service._revision_store = CorruptRevisionStore()  # noqa: SLF001

    result = api.create_release(_create_request())

    assert result["ok"] is False
    assert result["error"]["code"] == ReleaseErrorCode.INTEGRITY_FAILURE
    assert cad.calls == []


def test_release_rejects_source_pair_above_buffered_resource_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _service, _store, cad = _composition(tmp_path)
    monkeypatch.setattr(
        releases,
        "MAX_RELEASE_RESOURCE_BYTES",
        len(MODEL_BYTES) + len(STEP_BYTES) - 1,
    )

    result = api.create_release(_create_request())

    assert result["ok"] is False
    assert result["error"]["code"] == ReleaseErrorCode.RESOURCE_EXHAUSTED
    assert cad.calls == []
