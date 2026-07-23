"""Read-only artifact-manifest observation tests."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import vibecad.application.artifacts as artifacts_module
from vibecad.application.artifact_manifest import (
    ArtifactManifestError,
    ArtifactManifestErrorCode,
    ArtifactManifestService,
)
from vibecad.application.artifacts import (
    ArtifactExportRequest,
    ArtifactMaterializationService,
    ArtifactRequestPhase,
    ArtifactStore,
    LocalArtifactAuthority,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreRootTrust,
)
from vibecad.interaction.cad import CadExecutionPort, ValidatedMaterializationEvidence
from vibecad.workflow.contracts import AcceptanceSpec, ModelProgram
from vibecad.workflow.lease import (
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
    VerificationReport,
    append_artifact,
    new_task_run,
    transition_task,
)
from vibecad.workflow.store import (
    TaskRunStore,
    TaskStoreRootTrust,
)

PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
TASK_ID = "task_0123456789abcdef0123456789abcdef"
VERIFICATION_ID = "verification_0123456789abcdef0123456789abcdef"
EXPORT_KEY = "export_0123456789abcdef0123456789abcdef"
OBSERVATION_DIGEST = "b" * 64
ACCEPTANCE_ID = "artifact-manifest"


class _Cad(CadExecutionPort):
    def validate_materialization(
        self,
        *,
        fcstd: Path,
        step: Path,
    ) -> ValidatedMaterializationEvidence:
        model = fcstd.read_bytes()
        exchanged = step.read_bytes()
        return ValidatedMaterializationEvidence(
            fcstd_sha256=hashlib.sha256(model).hexdigest(),
            fcstd_size_bytes=len(model),
            step_sha256=hashlib.sha256(exchanged).hexdigest(),
            step_size_bytes=len(exchanged),
        )


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def _tree(root: Path) -> tuple[tuple[str, int, int, int, str], ...]:
    values = []
    for path in sorted((root, *root.rglob("*"))):
        value = path.stat(follow_symlinks=False)
        relative = "." if path == root else str(path.relative_to(root))
        digest = ""
        if path.is_file() and not path.is_symlink():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        values.append(
            (
                relative,
                value.st_mode,
                value.st_size,
                value.st_mtime_ns,
                digest,
            )
        )
    return tuple(values)


def _report(revision) -> VerificationReport:
    return VerificationReport(
        id=VERIFICATION_ID,
        acceptance_id=ACCEPTANCE_ID,
        candidate_revision=revision.id,
        manifest_sha256=revision.manifest_sha256,
        observation_digest=OBSERVATION_DIGEST,
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


def _task(*, base, revision, draft: bool):
    task = new_task_run(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        base_revision=base.revision_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=(ReviewPolicy.REQUIRE_REVIEW if draft else ReviewPolicy.AUTO_COMMIT),
    )
    task = transition_task(task, TaskEvent.REQUEST_PLAN)
    task = transition_task(
        task,
        TaskEvent.SUBMIT_PROGRAM,
        program=ModelProgram(
            task_id=TASK_ID,
            base_revision=base.revision_id,
            operations=(),
            acceptance=AcceptanceSpec(id=ACCEPTANCE_ID, criteria=()),
        ),
    )
    task = transition_task(task, TaskEvent.START_VALIDATION)
    task = transition_task(
        task,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=revision.id,
    )
    for ref in (revision.model, revision.artifacts[0]):
        assert ref is not None
        task = append_artifact(
            task,
            TaskArtifactRef(
                id=ref.id,
                name=ref.name,
                format=ref.format,
                sha256=ref.sha256,
                size_bytes=ref.size_bytes,
                candidate_revision=revision.id,
            ),
        )
    task = transition_task(task, TaskEvent.COMPLETE_EXECUTION)
    report = _report(revision)
    if draft:
        review = ReviewDraft(
            id=f"draft_{revision.id.removeprefix('revision_')}",
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            base_revision=base.revision_id,
            base_generation=base.generation,
            base_manifest_sha256=base.manifest_sha256,
            revision_id=revision.id,
            manifest_sha256=revision.manifest_sha256,
            verification_id=report.id,
            acceptance_id=report.acceptance_id,
            observation_digest=report.observation_digest,
        )
        task = transition_task(
            task,
            TaskEvent.PREPARE_REVIEW,
            verification=report,
            draft=review,
        )
        return transition_task(task, TaskEvent.PUBLISH_DRAFT)
    task = transition_task(task, TaskEvent.PASS_VERIFICATION, verification=report)
    return transition_task(
        task,
        TaskEvent.COMMIT,
        committed_revision=revision.id,
    )


def _parts(tmp_path: Path, *, draft: bool = False):
    locks = tmp_path / "locks"
    tasks_root = tmp_path / "tasks"
    projects = tmp_path / "projects"
    artifacts = tmp_path / "artifacts"
    for root in (locks, tasks_root, projects, artifacts):
        _mkdir(root)
    leases = ResourceLeaseManager(locks, trust=LeaseRootTrust.TRUSTED_LOCAL)
    tasks = TaskRunStore(
        tasks_root,
        leases,
        trust=TaskStoreRootTrust.TRUSTED_LOCAL,
    )
    revisions = LocalRevisionStore(
        projects,
        leases,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    model = b"manifest-model"
    step = b"manifest-step"
    with leases.acquire_project_write(PROJECT_ID) as lease:
        base = revisions.initialize_empty_project(PROJECT_ID, lease)
        revision_id = revisions.begin_revision(PROJECT_ID, base, lease)
        revisions.candidate_model_path(PROJECT_ID, revision_id, lease).write_bytes(model)
        revisions.candidate_artifact_path(
            PROJECT_ID,
            revision_id,
            "step",
            lease,
        ).write_bytes(step)
        revision = revisions.seal_revision(PROJECT_ID, revision_id, lease)
        if not draft:
            revisions.commit_revision(PROJECT_ID, base, revision.id, lease)
    stored = tasks.create(_task(base=base, revision=revision, draft=draft))
    service = ArtifactManifestService(
        task_store=tasks,
        revision_store=revisions,
        artifact_root=artifacts.resolve(),
        expected_artifact_root_identity=(
            artifacts.stat().st_dev,
            artifacts.stat().st_ino,
        ),
    )
    draft_id = f"draft_{revision.id.removeprefix('revision_')}" if draft else None
    return {
        "leases": leases,
        "tasks": tasks,
        "revisions": revisions,
        "artifacts": artifacts,
        "base": base,
        "revision": revision,
        "stored": stored,
        "service": service,
        "draft_id": draft_id,
    }


def _get(parts):
    return parts["service"].get_artifact_manifest(
        task_id=TASK_ID,
        expected_generation=parts["stored"].generation,
        revision_id=parts["revision"].id,
        draft_id=parts["draft_id"],
    )


def _verification_digest(report: VerificationReport) -> str:
    raw = json.dumps(
        report.to_mapping(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(b"vibecad-verification-report-v1\0" + raw).hexdigest()


@pytest.mark.parametrize("draft", (False, True))
def test_virgin_catalog_is_observed_without_any_write(
    tmp_path: Path,
    draft: bool,
) -> None:
    parts = _parts(tmp_path, draft=draft)
    before = _tree(parts["artifacts"])

    result = _get(parts)

    assert set(result) == {
        "source_kind",
        "task_id",
        "task_generation",
        "project_id",
        "revision_id",
        "draft_id",
        "manifest_sha256",
        "verification_id",
        "acceptance_id",
        "verification_digest",
        "observation_digest",
        "materialized",
        "materialization_id",
        "delivery_manifest_sha256",
        "artifacts",
    }
    assert result["source_kind"] == ("draft" if draft else "committed")
    assert result["task_id"] == TASK_ID
    assert result["task_generation"] == 0
    assert result["project_id"] == PROJECT_ID
    assert result["revision_id"] == parts["revision"].id
    assert result["draft_id"] == parts["draft_id"]
    assert result["manifest_sha256"] == parts["revision"].manifest_sha256
    assert result["verification_id"] == VERIFICATION_ID
    assert result["acceptance_id"] == ACCEPTANCE_ID
    assert result["verification_digest"] == _verification_digest(_report(parts["revision"]))
    assert result["observation_digest"] == OBSERVATION_DIGEST
    assert result["materialized"] is False
    assert result["materialization_id"] is None
    assert result["delivery_manifest_sha256"] is None
    assert [item["name"] for item in result["artifacts"]] == [
        "model.FCStd",
        "model.step",
    ]
    assert all(item["resource_uri"] is None for item in result["artifacts"])
    assert _tree(parts["artifacts"]) == before
    assert tuple(parts["artifacts"].iterdir()) == ()


def _publish(parts):
    authority = LocalArtifactAuthority(
        task_store=parts["tasks"],
        revision_store=parts["revisions"],
        lease_manager=parts["leases"],
    )
    store = ArtifactStore(
        root=parts["artifacts"].resolve(),
        expected_root_identity=(
            parts["artifacts"].stat().st_dev,
            parts["artifacts"].stat().st_ino,
        ),
    )
    materializer = ArtifactMaterializationService(
        store=store,
        authority=authority,
        cad=_Cad(),
    )
    request = ArtifactExportRequest(
        export_key=EXPORT_KEY,
        task_id=TASK_ID,
        expected_generation=parts["stored"].generation,
        revision_id=parts["revision"].id,
        draft_id=parts["draft_id"],
    )
    response = materializer.export_task_artifacts(request=request)
    store.close()
    return response


def test_published_catalog_returns_only_validated_resource_bindings(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    published = _publish(parts)
    before = _tree(parts["artifacts"])

    result = _get(parts)

    assert result["materialized"] is True
    assert result["materialization_id"] == published.materialization_id
    assert result["delivery_manifest_sha256"] is not None
    assert [item["resource_uri"] for item in result["artifacts"]] == [
        item.resource_uri for item in published.artifacts
    ]
    assert _tree(parts["artifacts"]) == before


def test_existing_empty_catalog_and_unpublished_materialization_return_no_uri(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    empty = ArtifactStore(
        root=parts["artifacts"].resolve(),
        expected_root_identity=(
            parts["artifacts"].stat().st_dev,
            parts["artifacts"].stat().st_ino,
        ),
    )
    empty.close()
    empty_before = _tree(parts["artifacts"])
    absent = _get(parts)
    assert absent["materialized"] is False
    assert all(item["resource_uri"] is None for item in absent["artifacts"])
    assert _tree(parts["artifacts"]) == empty_before

    _publish(parts)
    request_path = next((parts["artifacts"] / "requests").iterdir())
    record = artifacts_module._parse_record(request_path.read_bytes())
    request_path.write_bytes(
        artifacts_module._record_envelope(
            replace(
                record,
                phase=ArtifactRequestPhase.MATERIALIZED,
                response=None,
            )
        )
    )
    request_path.chmod(0o600)
    materialized_before = _tree(parts["artifacts"])

    unpublished = _get(parts)

    assert unpublished["materialized"] is False
    assert unpublished["materialization_id"] is None
    assert all(item["resource_uri"] is None for item in unpublished["artifacts"])
    assert _tree(parts["artifacts"]) == materialized_before


def test_source_revision_same_size_tamper_is_integrity_failure(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    model = parts["revisions"].revision_model_path(
        PROJECT_ID,
        parts["revision"].id,
    )
    original = model.read_bytes()
    model.write_bytes(bytes(reversed(original)))

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.INTEGRITY_FAILURE


def test_published_payload_tamper_is_not_downgraded_to_unmaterialized(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    published = _publish(parts)
    model = parts["artifacts"] / "materializations" / published.materialization_id / "model.FCStd"
    original = model.read_bytes()
    model.write_bytes(bytes(reversed(original)))
    model.chmod(0o600)

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.INTEGRITY_FAILURE


def test_published_request_digest_tamper_is_integrity_failure(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    _publish(parts)
    request = next((parts["artifacts"] / "requests").iterdir())
    record = artifacts_module._parse_record(request.read_bytes())
    request.write_bytes(
        artifacts_module._record_envelope(
            replace(
                record,
                request_digest="f" * 64,
            )
        )
    )
    request.chmod(0o600)

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.INTEGRITY_FAILURE


def test_published_materialization_directory_replacement_is_integrity_failure(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    published = _publish(parts)
    directory = parts["artifacts"] / "materializations" / published.materialization_id
    original = tmp_path / "original-materialization"
    directory.rename(original)
    shutil.copytree(original, directory)

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.INTEGRITY_FAILURE


def test_unrelated_malformed_materialization_invalidates_resource_binding(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    _publish(parts)
    unrelated = parts["artifacts"] / "materializations" / f"artifact_{'f' * 64}"
    _mkdir(unrelated)
    unexpected = unrelated / "unexpected"
    unexpected.write_bytes(b"unsafe catalog member")
    unexpected.chmod(0o600)

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.INTEGRITY_FAILURE


def test_catalog_over_global_byte_budget_is_resource_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = _parts(tmp_path)
    _publish(parts)
    monkeypatch.setattr(artifacts_module, "MAX_ARTIFACT_STORE_BYTES", 1)

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.RESOURCE_EXHAUSTED


def test_generation_and_identifier_validation_are_closed(tmp_path: Path) -> None:
    parts = _parts(tmp_path)
    cases = (
        (
            {
                "task_id": "task_bad",
                "expected_generation": 0,
                "revision_id": parts["revision"].id,
                "draft_id": None,
            },
            ArtifactManifestErrorCode.INVALID_INPUT,
        ),
        (
            {
                "task_id": TASK_ID,
                "expected_generation": 1,
                "revision_id": parts["revision"].id,
                "draft_id": None,
            },
            ArtifactManifestErrorCode.CONFLICT,
        ),
        (
            {
                "task_id": TASK_ID,
                "expected_generation": True,
                "revision_id": parts["revision"].id,
                "draft_id": None,
            },
            ArtifactManifestErrorCode.INVALID_INPUT,
        ),
        (
            {
                "task_id": TASK_ID,
                "expected_generation": 0,
                "revision_id": f"revision_{'f' * 32}",
                "draft_id": None,
            },
            ArtifactManifestErrorCode.INVALID_STATE,
        ),
    )
    for arguments, expected in cases:
        with pytest.raises(ArtifactManifestError) as captured:
            parts["service"].get_artifact_manifest(**arguments)
        assert captured.value.code is expected


def test_revision_snapshot_change_during_observation_is_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = _parts(tmp_path)
    original = LocalRevisionStore.snapshot_revisions
    calls = 0

    def changed(store, project_id):
        nonlocal calls
        calls += 1
        value = original(store, project_id)
        if calls == 2:
            return replace(value, state_sha256="f" * 64)
        return value

    monkeypatch.setattr(LocalRevisionStore, "snapshot_revisions", changed)

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.CONFLICT


def test_task_change_during_observation_is_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parts = _parts(tmp_path)
    original = TaskRunStore.load
    calls = 0

    def changed(store, task_id):
        nonlocal calls
        calls += 1
        value = original(store, task_id)
        if calls == 2:
            return replace(value, generation=value.generation + 1)
        return value

    monkeypatch.setattr(TaskRunStore, "load", changed)

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.CONFLICT


def test_published_delivery_manifest_missing_is_integrity_failure(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    published = _publish(parts)
    manifest = (
        parts["artifacts"] / "materializations" / published.materialization_id / "manifest.json"
    )
    manifest.unlink()

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.INTEGRITY_FAILURE


def test_request_remnant_requires_recovery_without_cleanup(
    tmp_path: Path,
) -> None:
    parts = _parts(tmp_path)
    _publish(parts)
    request = next((parts["artifacts"] / "requests").iterdir())
    remnant = request.with_name(f".{request.name}.{'f' * 32}.tmp")
    remnant.write_bytes(request.read_bytes())
    remnant.chmod(0o600)
    before = _tree(parts["artifacts"])

    with pytest.raises(ArtifactManifestError) as captured:
        _get(parts)

    assert captured.value.code is ArtifactManifestErrorCode.RECOVERY_REQUIRED
    assert remnant.is_file()
    assert _tree(parts["artifacts"]) == before
