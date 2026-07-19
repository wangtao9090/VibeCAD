"""Opt-in end-to-end TaskService gates against the installed FreeCAD runtime."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def existing_freecad_python() -> str:
    """Select an existing ready runtime without invoking any installer."""

    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    prefix_value = os.environ.get("VIBECAD_FREECAD_ENV")
    if not prefix_value:
        pytest.fail("set VIBECAD_FREECAD_ENV to an existing ready FreeCAD environment")
    prefix = Path(prefix_value).expanduser()
    python = prefix / "bin" / "python"
    sentinel = prefix / ".vibecad_ready"
    if not python.is_file():
        pytest.fail("VIBECAD_FREECAD_ENV does not contain bin/python")
    if not sentinel.is_file():
        pytest.fail("VIBECAD_FREECAD_ENV does not contain the ready sentinel")
    return str(python)


_CHILD = r"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, __SOURCE__)

from vibecad.engine.session import Session
from vibecad.execution.candidate import CandidateCoordinator, SessionBinding, SessionSlot
from vibecad.execution.executor import InProcessCadExecutor
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreError,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
)
from vibecad.tools.modeling import new_document
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.service import TaskService
from vibecad.workflow.state import ReasoningOwner
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust

CASE = __CASE__
ROOT = Path(__WORK_ROOT__)
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
TASK_ID = "task_0123456789abcdef0123456789abcdef"


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def command(
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
        preserve=(),
        source=ValueSource.MODEL,
        depends_on=depends_on,
    )


def criterion(
    criterion_id: str,
    kind: AcceptanceKind,
    check: str,
    target: str,
    expected: object,
    *,
    tolerance: float | None = None,
    parameters: dict[str, object] | None = None,
) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=criterion_id,
        kind=kind,
        check=check,
        target=target,
        expected=expected,
        tolerance=tolerance,
        parameters={} if parameters is None else parameters,
        required=True,
    )


def acceptance(expected_volume: float) -> AcceptanceSpec:
    return AcceptanceSpec(
        id="acceptance-task-kernel-real",
        criteria=(
            criterion(
                "volume",
                AcceptanceKind.GEOMETRY,
                "volume",
                "body",
                expected_volume,
                tolerance=0.0,
                parameters={"unit": "mm^3"},
            ),
            criterion(
                "bbox",
                AcceptanceKind.GEOMETRY,
                "bbox",
                "body",
                (12.0, 20.0, 30.0),
                tolerance=0.0,
                parameters={"unit": "mm"},
            ),
            criterion("valid", AcceptanceKind.TOPOLOGY, "valid_shape", "body", True),
            criterion("solids", AcceptanceKind.TOPOLOGY, "solid_count", "body", 1),
            criterion("step-exists", AcceptanceKind.ARTIFACT, "exists", "export", True),
            criterion(
                "step-non-empty",
                AcceptanceKind.ARTIFACT,
                "non_empty",
                "export",
                True,
            ),
            criterion("step-format", AcceptanceKind.ARTIFACT, "format", "export", "step"),
            criterion("model-exists", AcceptanceKind.ARTIFACT, "exists", "model", True),
            criterion(
                "model-non-empty",
                AcceptanceKind.ARTIFACT,
                "non_empty",
                "model",
                True,
            ),
            criterion("model-format", AcceptanceKind.ARTIFACT, "format", "model", "fcstd"),
        ),
    )


def program(base_revision: str) -> ModelProgram:
    target = "MissingBox" if CASE == "execution_failure" else "Box"
    expected_volume = 7201.0 if CASE == "verification_failure" else 7200.0
    return ModelProgram(
        task_id=TASK_ID,
        base_revision=base_revision,
        operations=(
            command(
                "box",
                "create_box",
                args={"length": 10, "width": 20, "height": 30},
            ),
            command(
                "modify",
                "modify_parameter",
                target={"object": target},
                args={"parameter": "length", "value": 12},
                depends_on=("box",),
            ),
            command("inspect", "inspect_model", depends_on=("modify",)),
        ),
        acceptance=acceptance(expected_volume),
    )


def geometry(session: object) -> dict[str, object]:
    shape = session.get_assembly_shape()
    return {
        "volume": float(shape.Volume),
        "area": float(shape.Area),
        "bbox": [
            float(shape.BoundBox.XLength),
            float(shape.BoundBox.YLength),
            float(shape.BoundBox.ZLength),
        ],
        "center_of_mass": [
            float(shape.CenterOfMass.x),
            float(shape.CenterOfMass.y),
            float(shape.CenterOfMass.z),
        ],
        "valid": bool(shape.isValid()),
        "solid_count": len(shape.Solids),
    }


def close_best_effort(session: object | None) -> None:
    if session is None:
        return
    try:
        if session.doc is not None:
            session.close_document()
    except Exception:
        pass


secure_dir(ROOT)
locks_root = secure_dir(ROOT / "locks")
revisions_root = secure_dir(ROOT / "revisions")
tasks_root = secure_dir(ROOT / "tasks")
manager = ResourceLeaseManager(locks_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
revision_store = LocalRevisionStore(
    revisions_root,
    manager,
    trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
)
task_store = TaskRunStore(tasks_root, manager, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
executor = InProcessCadExecutor(store=revision_store)

seed_session = Session()
baseline_session = None
probe_session = None
slot = None
payload = {}
try:
    new_document(seed_session, name="TaskKernelBaseline")
    baseline_source = ROOT / "baseline.FCStd"
    executor.checkpoint_fcstd(seed_session, baseline_source)
    baseline_source.chmod(0o600)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        head_before = revision_store.import_trusted_fcstd(
            PROJECT_ID,
            baseline_source,
            lease,
        )
    base_ref_before = revision_store.load_revision(PROJECT_ID, head_before.revision_id)
    base_model_path = revision_store.revision_model_path(PROJECT_ID, head_before.revision_id)
    base_digest_before = sha256(base_model_path)
    executor.close(seed_session)
    seed_session = None
    baseline_session = executor.load_fcstd(base_model_path)

    baseline_binding = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=head_before.revision_id,
        session=baseline_session,
    )
    slot = SessionSlot(baseline_binding)
    coordinator = CandidateCoordinator(
        store=revision_store,
        snapshot_port=executor,
        session_slot=slot,
    )
    service = TaskService(
        task_store=task_store,
        revision_store=revision_store,
        lease_manager=manager,
        coordinator=coordinator,
        executor=executor,
    )

    created = service.create_task(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
    )
    terminal = service.submit_model_program(
        task_id=TASK_ID,
        expected_generation=created.generation,
        program=program(head_before.revision_id),
    )
    durable = service.get_task(task_id=TASK_ID)
    task = terminal.task_run
    head_after = revision_store.load_head(PROJECT_ID)
    base_ref_after = revision_store.load_revision(PROJECT_ID, head_before.revision_id)
    base_digest_after = sha256(base_model_path)
    current_binding = slot.current()

    candidate_ref = None
    if task.candidate_revision is not None:
        try:
            candidate_ref = revision_store.load_revision(
                PROJECT_ID,
                task.candidate_revision,
            )
        except RevisionStoreError as error:
            if not (
                CASE == "execution_failure"
                and error.code is RevisionStoreErrorCode.NOT_FOUND
            ):
                raise

    artifact_files = []
    for artifact in task.artifacts:
        if artifact.format == "fcstd":
            path = revision_store.revision_model_path(PROJECT_ID, artifact.candidate_revision)
        else:
            path = revision_store.revision_artifact_path(
                PROJECT_ID,
                artifact.candidate_revision,
                artifact.id,
            )
        artifact_files.append(
            {
                "id": artifact.id,
                "name": artifact.name,
                "format": artifact.format,
                "exists": path.is_file(),
                "size": path.stat().st_size if path.is_file() else 0,
                "expected_size": artifact.size_bytes,
                "sha256": sha256(path) if path.is_file() else None,
                "expected_sha256": artifact.sha256,
                "under_root": path.resolve().is_relative_to(ROOT.resolve()),
            }
        )

    revision_artifacts = []
    if candidate_ref is not None:
        for artifact in (candidate_ref.model, *candidate_ref.artifacts):
            if artifact is None:
                continue
            revision_artifacts.append(
                {
                    "id": artifact.id,
                    "name": artifact.name,
                    "format": artifact.format,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                }
            )

    reload_geometry = None
    if candidate_ref is not None:
        candidate_model = revision_store.revision_model_path(PROJECT_ID, candidate_ref.id)
        probe_session = executor.load_fcstd(candidate_model)
        reload_geometry = geometry(probe_session)

    slot_geometry = geometry(current_binding.session) if CASE == "success" else None

    baseline_usable = None
    if CASE != "success":
        current_binding.session.doc.recompute()
        baseline_usable = (
            current_binding is baseline_binding
            and current_binding.session.doc is not None
            and not current_binding.session.is_dirty()
            and len(current_binding.session.doc.Objects) == 0
        )

    head_files = tuple(revisions_root.rglob("HEAD.json"))
    journal_files = tuple(revisions_root.rglob("journal.json"))
    manifest_files = tuple(revisions_root.rglob("manifest.json"))
    candidate_files = tuple(
        path
        for path in revisions_root.rglob("*")
        if path.is_file() and "candidates" in path.parts
    )
    runtime_files = tuple(path for path in ROOT.rglob("*") if path.is_file())
    layout = {
        "head_count": len(head_files),
        "journal_count": len(journal_files),
        "journal_states": [
            json.loads(path.read_text(encoding="utf-8"))["state"]
            for path in journal_files
        ],
        "manifest_count": len(manifest_files),
        "candidate_file_count": len(candidate_files),
        "task_record_count": len(tuple(tasks_root.glob("*.json"))),
        "all_files_under_root": all(
            path.resolve().is_relative_to(ROOT.resolve()) for path in runtime_files
        ),
    }

    payload = {
        "case": CASE,
        "status": task.status.value,
        "generation": terminal.generation,
        "durable_roundtrip": durable == terminal,
        "base_head": head_before.to_mapping(),
        "final_head": head_after.to_mapping(),
        "base_revision_unchanged": base_ref_before == base_ref_after,
        "base_model_digest_unchanged": base_digest_before == base_digest_after,
        "candidate_revision": task.candidate_revision,
        "committed_revision": task.committed_revision,
        "candidate_revision_durable": candidate_ref is not None,
        "slot_is_baseline": current_binding is baseline_binding,
        "slot_revision": current_binding.revision_id,
        "slot_session_open": current_binding.session.doc is not None,
        "slot_geometry": slot_geometry,
        "baseline_usable": baseline_usable,
        "step_oks": [record.result.ok for record in task.steps],
        "step_operations": [record.result.operation_id for record in task.steps],
        "step_values": [record.result.to_mapping()["value"] for record in task.steps],
        "step_error_codes": [
            None if record.result.error is None else record.result.error.code
            for record in task.steps
        ],
        "last_error": None if task.last_error is None else task.last_error.code,
        "report_passed": [report.passed for report in task.verification_reports],
        "verdicts": [
            {
                "id": verdict.criterion_id,
                "outcome": verdict.outcome.value,
                "expected": verdict.expected,
                "observed": verdict.observed,
            }
            for report in task.verification_reports
            for verdict in report.verdicts
        ],
        "artifacts": [artifact.to_mapping() for artifact in task.artifacts],
        "artifact_files": artifact_files,
        "revision_artifacts": revision_artifacts,
        "transitions": [record.event.value for record in task.transitions],
        "reload_geometry": reload_geometry,
        "layout": layout,
    }
finally:
    close_best_effort(probe_session)
    current_session = None
    if slot is not None:
        try:
            current_session = slot.current().session
        except Exception:
            current_session = None
    close_best_effort(current_session)
    if baseline_session is not None and current_session is not baseline_session:
        close_best_effort(baseline_session)
    close_best_effort(seed_session)

print("TK9_RESULT=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
"""


def _run_case(
    existing_freecad_python: str,
    tmp_path: Path,
    case: str,
) -> dict[str, object]:
    source = Path(__file__).resolve().parent.parent / "src"
    work_root = tmp_path / case
    work_root.mkdir(mode=0o700)
    work_root.chmod(0o700)
    code = (
        _CHILD.replace("__SOURCE__", repr(str(source)))
        .replace("__WORK_ROOT__", repr(str(work_root)))
        .replace("__CASE__", repr(case))
    )
    process = subprocess.run(
        [existing_freecad_python, "-c", code],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    lines = [line for line in process.stdout.splitlines() if line.startswith("TK9_RESULT=")]
    assert len(lines) == 1, process.stdout
    payload = json.loads(lines[0].removeprefix("TK9_RESULT="))
    assert payload["case"] == case
    assert payload["durable_roundtrip"] is True
    assert payload["base_revision_unchanged"] is True
    assert payload["base_model_digest_unchanged"] is True
    return payload


def _assert_rollback(payload: dict[str, object]) -> None:
    assert payload["status"] == "failed"
    assert payload["committed_revision"] is None
    assert payload["final_head"] == payload["base_head"]
    assert payload["slot_is_baseline"] is True
    assert payload["slot_revision"] == payload["base_head"]["revision_id"]
    assert payload["slot_session_open"] is True
    assert payload["slot_geometry"] is None
    assert payload["baseline_usable"] is True
    assert payload["transitions"][-1] == "complete_rollback"


def _assert_layout(
    payload: dict[str, object],
    *,
    journal_state: str,
    manifest_count: int,
) -> None:
    layout = payload["layout"]
    assert layout == {
        "head_count": 1,
        "journal_count": 1,
        "journal_states": [journal_state],
        "manifest_count": manifest_count,
        "candidate_file_count": 0,
        "task_record_count": 1,
        "all_files_under_root": True,
    }


def _assert_artifact_lineage(payload: dict[str, object]) -> None:
    task_artifacts = [
        {key: item[key] for key in ("id", "name", "format", "sha256", "size_bytes")}
        for item in payload["artifacts"]
    ]
    assert task_artifacts == payload["revision_artifacts"]
    assert all(item["under_root"] for item in payload["artifact_files"])
    assert all(
        item["exists"]
        and item["size"] > 0
        and item["size"] == item["expected_size"]
        and item["sha256"] == item["expected_sha256"]
        for item in payload["artifact_files"]
    )


def _assert_box_geometry(value: dict[str, object]) -> None:
    assert value["volume"] == pytest.approx(7200.0)
    assert value["area"] == pytest.approx(2400.0)
    assert value["bbox"] == pytest.approx([12.0, 20.0, 30.0])
    assert value["center_of_mass"] == pytest.approx([6.0, 10.0, 15.0])
    assert value["valid"] is True
    assert value["solid_count"] == 1


@pytest.mark.slow
def test_real_task_kernel_commits_verified_candidate(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(existing_freecad_python, tmp_path, "success")
    assert payload["status"] == "succeeded"
    assert payload["candidate_revision"] == payload["committed_revision"]
    assert payload["candidate_revision"] == payload["final_head"]["revision_id"]
    assert payload["final_head"]["generation"] == payload["base_head"]["generation"] + 1
    assert payload["slot_is_baseline"] is False
    assert payload["slot_revision"] == payload["candidate_revision"]
    assert payload["slot_session_open"] is True
    assert payload["step_oks"] == [True, True, True]
    assert payload["step_operations"] == ["box", "modify", "inspect"]
    assert payload["step_values"][0]["volume"] == pytest.approx(6000.0)
    assert payload["step_values"][1]["volume"] == pytest.approx(7200.0)
    assert payload["report_passed"] == [True]
    assert len(payload["verdicts"]) == 10
    assert {item["outcome"] for item in payload["verdicts"]} == {"pass"}
    assert [item["format"] for item in payload["artifacts"]] == ["fcstd", "step"]
    _assert_artifact_lineage(payload)
    _assert_box_geometry(payload["slot_geometry"])
    _assert_box_geometry(payload["reload_geometry"])
    assert payload["transitions"][-1] == "commit"
    _assert_layout(payload, journal_state="committed", manifest_count=2)


@pytest.mark.slow
def test_real_task_kernel_rolls_back_after_partial_execution_failure(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(existing_freecad_python, tmp_path, "execution_failure")
    _assert_rollback(payload)
    assert payload["candidate_revision"] is not None
    assert payload["candidate_revision_durable"] is False
    assert payload["step_oks"] == [True, False]
    assert payload["step_operations"] == ["box", "modify"]
    assert payload["step_values"][0]["volume"] == pytest.approx(6000.0)
    assert payload["step_error_codes"][0] is None
    assert payload["step_error_codes"][1] == "unexpected_tool_exception"
    assert payload["last_error"] == "unexpected_tool_exception"
    assert payload["report_passed"] == []
    assert payload["artifacts"] == []
    assert payload["revision_artifacts"] == []
    assert payload["reload_geometry"] is None
    assert "fail_execution" in payload["transitions"]
    _assert_layout(payload, journal_state="not_committed", manifest_count=1)


@pytest.mark.slow
def test_real_task_kernel_rolls_back_failed_verification_with_diagnostics(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(existing_freecad_python, tmp_path, "verification_failure")
    _assert_rollback(payload)
    assert payload["candidate_revision"] is not None
    assert payload["candidate_revision_durable"] is True
    assert payload["step_oks"] == [True, True, True]
    assert payload["step_values"][0]["volume"] == pytest.approx(6000.0)
    assert payload["step_values"][1]["volume"] == pytest.approx(7200.0)
    assert payload["report_passed"] == [False]
    assert len(payload["verdicts"]) == 10
    failed = [item for item in payload["verdicts"] if item["outcome"] == "fail"]
    assert [item["id"] for item in failed] == ["volume"]
    assert failed[0]["expected"] == pytest.approx(7201.0)
    assert failed[0]["observed"] == pytest.approx(7200.0)
    assert payload["last_error"] == "acceptance_verification_failed"
    assert [item["format"] for item in payload["artifacts"]] == ["fcstd", "step"]
    _assert_artifact_lineage(payload)
    _assert_box_geometry(payload["reload_geometry"])
    assert "fail_verification" in payload["transitions"]
    _assert_layout(payload, journal_state="not_committed", manifest_count=2)
