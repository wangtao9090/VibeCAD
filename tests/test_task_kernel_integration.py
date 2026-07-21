"""Opt-in end-to-end TaskService gates against the installed FreeCAD runtime."""

from __future__ import annotations

import json
import math
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
import math
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
from vibecad.execution.selectors import (
    EntityIdentity,
    EntityKind,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.tools.modeling import add_box, new_document
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.service import TaskService, TaskServiceError, TaskServiceErrorCode
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy
from vibecad.workflow.store import TaskRunStore, TaskStoreRootTrust

CASE = __CASE__
ROOT = Path(__WORK_ROOT__)
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
TASK_ID = "task_0123456789abcdef0123456789abcdef"
REVIEW_ACCEPT_TASK_ID = "task_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
REVIEW_STALE_TASK_ID = "task_cccccccccccccccccccccccccccccccc"
SEED_OBJECT_ID = "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SEED_FEATURE_ID = "feature_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

SIX_OPERATION_VOLUME = 12.0 * 20.0 * 30.0 + math.pi * 2.0**2 * 5.0
SIX_OPERATION_BBOX = (46.0, 13.0, 30.0)
SELECTOR_VOLUME = 15.0 * 5.0 * 4.0
SELECTOR_BBOX = (5.0, 15.0, 4.0)
PARTIAL_CYLINDER_VOLUME = 300.0 * math.pi
PARTIAL_CYLINDER_BBOX = (10.0, 20.0, 6.0)


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
    preserve: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
) -> ModelCommand:
    return ModelCommand(
        id=command_id,
        op=op,
        target={} if target is None else target,
        args={} if args is None else args,
        preserve=preserve,
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


def acceptance(
    expected_volume: float,
    expected_bbox: tuple[float, float, float],
    expected_solids: int,
) -> AcceptanceSpec:
    return AcceptanceSpec(
        id="acceptance-task-kernel-real",
        criteria=(
            criterion(
                "volume",
                AcceptanceKind.GEOMETRY,
                "volume",
                "body",
                expected_volume,
                tolerance=1e-7,
                parameters={"unit": "mm^3"},
            ),
            criterion(
                "bbox",
                AcceptanceKind.GEOMETRY,
                "bbox",
                "body",
                expected_bbox,
                tolerance=1e-7,
                parameters={"unit": "mm"},
            ),
            criterion("valid", AcceptanceKind.TOPOLOGY, "valid_shape", "body", True),
            criterion(
                "solids",
                AcceptanceKind.TOPOLOGY,
                "solid_count",
                "body",
                expected_solids,
            ),
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


def seed_identity(object_type: str = "Part::Box") -> EntityIdentity:
    return EntityIdentity(
        object_id=SEED_OBJECT_ID,
        feature_id=SEED_FEATURE_ID,
        object_type=object_type,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.SYSTEM,
            operation_id="selector-seed",
        ),
    )


def program(base_revision: str, *, task_id: str = TASK_ID) -> ModelProgram:
    if CASE == "partial_cylinder_rotation":
        selector = seed_identity("Part::Cylinder").to_selector(
            project_id=PROJECT_ID,
            revision_id=base_revision,
            entity_kind=EntityKind.OBJECT,
        ).to_mapping()
        return ModelProgram(
            task_id=task_id,
            base_revision=base_revision,
            operations=(
                command(
                    "rotate",
                    "rotate_part",
                    target={"object": selector},
                    args={"axis": "z", "angle_deg": 90},
                ),
                command("inspect", "inspect_model", depends_on=("rotate",)),
            ),
            acceptance=acceptance(
                PARTIAL_CYLINDER_VOLUME,
                PARTIAL_CYLINDER_BBOX,
                1,
            ),
        )

    if CASE == "selector_success":
        selector = seed_identity().to_selector(
            project_id=PROJECT_ID,
            revision_id=base_revision,
            entity_kind=EntityKind.OBJECT,
        ).to_mapping()
        return ModelProgram(
            task_id=task_id,
            base_revision=base_revision,
            operations=(
                command(
                    "modify",
                    "modify_parameter",
                    target={"object": selector},
                    args={"parameter": "length", "value_mm": 15},
                ),
                command(
                    "move",
                    "move_part",
                    target={"object": selector},
                    args={"position_mm": (20, 0, 0)},
                    depends_on=("modify",),
                ),
                command(
                    "rotate",
                    "rotate_part",
                    target={"object": selector},
                    args={"axis": "z", "angle_deg": 90},
                    depends_on=("move",),
                ),
                command("inspect", "inspect_model", depends_on=("rotate",)),
            ),
            acceptance=acceptance(SELECTOR_VOLUME, SELECTOR_BBOX, 1),
        )

    parameter = "length"
    modify_target = "box"
    expected_volume = (
        SIX_OPERATION_VOLUME + 1.0
        if CASE == "verification_failure"
        else SIX_OPERATION_VOLUME
    )
    return ModelProgram(
        task_id=task_id,
        base_revision=base_revision,
        operations=(
            command(
                "box",
                "create_box",
                args={
                    "length_mm": 10,
                    "width_mm": 20,
                    "height_mm": 30,
                    "position_mm": (0, 0, 0),
                },
            ),
            command(
                "cylinder",
                "create_cylinder",
                args={
                    "radius_mm": 2,
                    "height_mm": 5,
                    "position_mm": (30, 0, 0),
                    "axis": "z",
                },
            ),
            command(
                "modify",
                "modify_parameter",
                target={
                    "object": {"command_id": modify_target, "slot": "object"}
                },
                args={"parameter": parameter, "value_mm": 12},
                preserve=("length",) if CASE == "execution_failure" else (),
                depends_on=(modify_target,),
            ),
            command(
                "move",
                "move_part",
                target={
                    "object": {"command_id": "cylinder", "slot": "object"}
                },
                args={"position_mm": (40, 5, 0)},
                depends_on=("cylinder",),
            ),
            command(
                "rotate",
                "rotate_part",
                target={
                    "object": {"command_id": modify_target, "slot": "object"}
                },
                args={"axis": "z", "angle_deg": 90},
                depends_on=("modify",),
            ),
            command(
                "inspect",
                "inspect_model",
                depends_on=("move", "rotate"),
            ),
        ),
        acceptance=acceptance(expected_volume, SIX_OPERATION_BBOX, 2),
    )


def shape_geometry(shape: object) -> dict[str, object]:
    center = getattr(shape, "CenterOfMass", None)
    return {
        "volume": float(shape.Volume),
        "area": float(shape.Area),
        "bbox": [
            float(shape.BoundBox.XLength),
            float(shape.BoundBox.YLength),
            float(shape.BoundBox.ZLength),
        ],
        "center_of_mass": (
            None
            if center is None
            else [float(center.x), float(center.y), float(center.z)]
        ),
        "valid": bool(shape.isValid()),
        "solid_count": len(shape.Solids),
    }


def managed_shape(session: object) -> object:
    pairs = tuple(session.list_object_identities())
    shapes = tuple(
        obj.Shape
        for obj, identity in pairs
        if identity.object_type in {"Part::Box", "Part::Cylinder"}
    )
    if not shapes:
        raise AssertionError("managed model contains no primitive shape")
    if len(shapes) == 1:
        return shapes[0]
    import Part

    return Part.makeCompound(list(shapes))


def geometry(session: object) -> dict[str, object]:
    return shape_geometry(managed_shape(session))


def entity_facts(session: object) -> list[dict[str, object]]:
    return [
        {
            "name": obj.Name,
            "type": obj.TypeId,
            "object_id": identity.object_id,
            "feature_id": identity.feature_id,
        }
        for obj, identity in session.list_object_identities()
    ]


def step_geometry(path: Path) -> dict[str, object]:
    import Part

    return shape_geometry(Part.read(str(path)))


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


def review_composition(head):
    base_ref = revision_store.load_revision(PROJECT_ID, head.revision_id)
    if base_ref.model is None:
        baseline = executor.create_empty(revision_id=head.revision_id)
    else:
        baseline = executor.load_fcstd(
            revision_store.revision_model_path(PROJECT_ID, head.revision_id)
        )
    binding = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=head.revision_id,
        session=baseline,
    )
    review_slot = SessionSlot(binding)
    review_coordinator = CandidateCoordinator(
        store=revision_store,
        snapshot_port=executor,
        session_slot=review_slot,
    )
    review_service = TaskService(
        task_store=task_store,
        revision_store=revision_store,
        lease_manager=manager,
        coordinator=review_coordinator,
        executor=executor,
    )
    return baseline, review_slot, review_service


def review_task_summary(stored):
    task = stored.task_run
    return {
        "id": task.id,
        "generation": stored.generation,
        "status": task.status.value,
        "review_policy": task.review_policy.value,
        "candidate_revision": task.candidate_revision,
        "committed_revision": task.committed_revision,
        "draft": None if task.draft is None else task.draft.to_mapping(),
        "transitions": [record.event.value for record in task.transitions],
    }


if CASE in {"review_restart_prepare", "review_restart_decide"}:
    review_baseline = None
    review_slot = None
    review_payload = {}
    try:
        if CASE == "review_restart_prepare":
            with manager.acquire_project_write(PROJECT_ID) as lease:
                review_base_head = revision_store.initialize_empty_project(PROJECT_ID, lease)
            review_baseline, review_slot, review_service = review_composition(review_base_head)
            prepared_tasks = []
            for review_task_id in (REVIEW_ACCEPT_TASK_ID, REVIEW_STALE_TASK_ID):
                created = review_service.create_task(
                    task_id=review_task_id,
                    project_id=PROJECT_ID,
                    reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                    review_policy=ReviewPolicy.REQUIRE_REVIEW,
                )
                awaiting = review_service.submit_model_program(
                    task_id=review_task_id,
                    expected_generation=created.generation,
                    program=program(
                        review_base_head.revision_id,
                        task_id=review_task_id,
                    ),
                )
                durable_awaiting = review_service.get_task(task_id=review_task_id)
                if durable_awaiting != awaiting or awaiting.task_run.draft is None:
                    raise AssertionError("review draft was not durably published")
                prepared_tasks.append(review_task_summary(awaiting))
            review_payload = {
                "case": CASE,
                "pid": os.getpid(),
                "base_head": review_base_head.to_mapping(),
                "final_head": revision_store.load_head(PROJECT_ID).to_mapping(),
                "tasks": prepared_tasks,
                "task_record_count": len(tuple(tasks_root.glob("*.json"))),
            }
        else:
            review_base_head = revision_store.load_head(PROJECT_ID)
            review_baseline, review_slot, review_service = review_composition(review_base_head)
            accept_before = review_service.get_task(task_id=REVIEW_ACCEPT_TASK_ID)
            stale_before = review_service.get_task(task_id=REVIEW_STALE_TASK_ID)
            accept_draft = accept_before.task_run.draft
            stale_draft = stale_before.task_run.draft
            if accept_draft is None or stale_draft is None:
                raise AssertionError("durable review draft is missing after restart")

            accepted = review_service.accept_draft(
                task_id=REVIEW_ACCEPT_TASK_ID,
                draft_id=accept_draft.id,
                expected_generation=accept_before.generation,
            )
            head_after_accept = revision_store.load_head(PROJECT_ID)

            stale_error = None
            try:
                review_service.accept_draft(
                    task_id=REVIEW_STALE_TASK_ID,
                    draft_id=stale_draft.id,
                    expected_generation=stale_before.generation,
                )
            except TaskServiceError as error:
                stale_error = error.code.value
            if stale_error != TaskServiceErrorCode.CONFLICT.value:
                raise AssertionError("stale draft acceptance did not return conflict")
            stale_after_conflict = review_service.get_task(task_id=REVIEW_STALE_TASK_ID)
            head_after_conflict = revision_store.load_head(PROJECT_ID)

            rejected = review_service.reject_draft(
                task_id=REVIEW_STALE_TASK_ID,
                draft_id=stale_draft.id,
                expected_generation=stale_before.generation,
            )
            durable_accepted = review_service.get_task(task_id=REVIEW_ACCEPT_TASK_ID)
            durable_rejected = review_service.get_task(task_id=REVIEW_STALE_TASK_ID)
            review_payload = {
                "case": CASE,
                "pid": os.getpid(),
                "base_head": review_base_head.to_mapping(),
                "accept_before": review_task_summary(accept_before),
                "stale_before": review_task_summary(stale_before),
                "accepted": review_task_summary(accepted),
                "durable_accepted": review_task_summary(durable_accepted),
                "head_after_accept": head_after_accept.to_mapping(),
                "stale_error": stale_error,
                "stale_after_conflict": review_task_summary(stale_after_conflict),
                "head_after_conflict": head_after_conflict.to_mapping(),
                "rejected": review_task_summary(rejected),
                "durable_rejected": review_task_summary(durable_rejected),
                "final_head": revision_store.load_head(PROJECT_ID).to_mapping(),
                "task_record_count": len(tuple(tasks_root.glob("*.json"))),
            }
    finally:
        review_current = None
        if review_slot is not None:
            try:
                review_current = review_slot.current().session
            except Exception:
                review_current = None
        close_best_effort(review_current)
        if review_baseline is not None and review_current is not review_baseline:
            close_best_effort(review_baseline)
    print("TK9_RESULT=" + json.dumps(review_payload, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0)


seed_session = None
baseline_session = None
probe_session = None
slot = None
payload = {}
try:
    if CASE == "empty_success":
        with manager.acquire_project_write(PROJECT_ID) as lease:
            head_before = revision_store.initialize_empty_project(PROJECT_ID, lease)
        base_ref_before = revision_store.load_revision(PROJECT_ID, head_before.revision_id)
        base_model_path = None
        base_digest_before = None
        baseline_session = executor.create_empty(revision_id=head_before.revision_id)
    else:
        seed_session = Session()
        new_document(seed_session, name="TaskKernelBaseline")
        if CASE == "partial_cylinder_rotation":
            with seed_session._transaction("seed-imported-partial-cylinder"):
                seed_object = seed_session.doc.addObject(
                    "Part::Cylinder",
                    "ImportedPartialCylinder",
                )
                seed_object.Radius = 10
                seed_object.Height = 6
                seed_object.Angle = 180
                seed_session.doc.recompute()
                seed_session.set_result_object(seed_object)
                seed_session.attach_object_identity(
                    seed_object,
                    seed_identity("Part::Cylinder"),
                )
        elif CASE == "selector_success":
            seeded = add_box(
                seed_session,
                length=10,
                width=5,
                height=4,
                position=(0, 0, 0),
            )
            seed_object = seed_session.get_object(seeded["name"])
            with seed_session._transaction("attach-selector-seed"):
                seed_session.attach_object_identity(seed_object, seed_identity())
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
        review_policy=ReviewPolicy.AUTO_COMMIT,
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
    base_digest_after = None if base_model_path is None else sha256(base_model_path)
    current_binding = slot.current()

    candidate_ref = None
    if task.candidate_revision is not None:
        try:
            candidate_ref = revision_store.load_revision(
                PROJECT_ID,
                task.candidate_revision,
            )
        except RevisionStoreError as error:
            if error.code is not RevisionStoreErrorCode.NOT_FOUND:
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
    reload_entities = None
    step_reload_geometry = None
    if candidate_ref is not None:
        candidate_model = revision_store.revision_model_path(PROJECT_ID, candidate_ref.id)
        probe_session = executor.load_fcstd(candidate_model)
        reload_geometry = geometry(probe_session)
        reload_entities = entity_facts(probe_session)
        step_ref = next(
            artifact for artifact in candidate_ref.artifacts if artifact.format == "step"
        )
        step_path = revision_store.revision_artifact_path(
            PROJECT_ID,
            candidate_ref.id,
            step_ref.id,
        )
        step_reload_geometry = step_geometry(step_path)

    slot_geometry = (
        geometry(current_binding.session)
        if task.status.value == "succeeded"
        else None
    )
    slot_entities = (
        entity_facts(current_binding.session)
        if task.status.value == "succeeded"
        else None
    )

    baseline_usable = None
    if task.status.value == "failed":
        current_binding.session.doc.recompute()
        expected_object_count = 1 if CASE == "selector_success" else 0
        baseline_usable = (
            current_binding is baseline_binding
            and current_binding.session.doc is not None
            and not current_binding.session.is_dirty()
            and len(current_binding.session.doc.Objects) == expected_object_count
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
        "base_model_was_empty": base_ref_before.model is None,
        "base_model_digest_unchanged": base_digest_before == base_digest_after,
        "candidate_revision": task.candidate_revision,
        "committed_revision": task.committed_revision,
        "candidate_revision_durable": candidate_ref is not None,
        "slot_is_baseline": current_binding is baseline_binding,
        "slot_revision": current_binding.revision_id,
        "slot_session_open": current_binding.session.doc is not None,
        "slot_geometry": slot_geometry,
        "slot_entities": slot_entities,
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
        "reload_entities": reload_entities,
        "step_reload_geometry": step_reload_geometry,
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


_SELECTOR_PRESERVATION_CHILD = r"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, __SOURCE__)

from vibecad.engine.session import Session
from vibecad.execution.candidate import CandidateCoordinator, SessionBinding, SessionSlot
from vibecad.execution.executor import InProcessCadExecutor
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreRootTrust,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    EntityKind,
    Provenance,
    ProvenanceSource,
    SelectorError,
    SelectorErrorCode,
    SelectorV1,
    SemanticRole,
    resolve_selector,
)
from vibecad.tools.modeling import add_box, new_document
from vibecad.tools.modify import modify_part
from vibecad.workflow.lease import LeaseRootTrust, ResourceLeaseManager

ROOT = Path(__WORK_ROOT__)
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
TARGET_OBJECT_ID = "object_11111111111111111111111111111111"
TARGET_FEATURE_ID = "feature_11111111111111111111111111111111"
CONTROL_OBJECT_ID = "object_22222222222222222222222222222222"
CONTROL_FEATURE_ID = "feature_22222222222222222222222222222222"
IDENTITY_PROPERTIES = frozenset(
    {
        "VibeCADObjectId",
        "VibeCADFeatureId",
        "VibeCADSemanticRole",
        "VibeCADProvenance",
    }
)


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


def close_best_effort(session: object | None) -> None:
    if session is None:
        return
    try:
        if session.doc is not None:
            session.close_document()
    except Exception:
        pass


def identity(object_id: str, feature_id: str, operation_id: str) -> EntityIdentity:
    return EntityIdentity(
        object_id=object_id,
        feature_id=feature_id,
        object_type="Part::Box",
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(
            source=ProvenanceSource.SYSTEM,
            operation_id=operation_id,
        ),
    )


def parameter(entity: object, name: str) -> float:
    values = {item.name: float(item.value) for item in entity.parameters}
    return values[name]


def identified_objects(session: object) -> tuple[object, ...]:
    return tuple(
        obj
        for obj in session.doc.Objects
        if IDENTITY_PROPERTIES.issubset(set(obj.PropertiesList))
    )


secure_dir(ROOT)
locks_root = secure_dir(ROOT / "locks")
revisions_root = secure_dir(ROOT / "revisions")
manager = ResourceLeaseManager(locks_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
store = LocalRevisionStore(
    revisions_root,
    manager,
    trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
)
executor = InProcessCadExecutor(store=store)

seed_session = None
baseline_session = None
payload = {}
try:
    with manager.acquire_project_write(PROJECT_ID) as lease:
        seed_session = Session()
        new_document(seed_session, "SelectorPreservationBase")
        target_name = add_box(
            seed_session,
            length=10,
            width=5,
            height=4,
            position=(0, 0, 0),
        )["name"]
        control_name = add_box(
            seed_session,
            length=7,
            width=6,
            height=3,
            position=(30, 0, 0),
        )["name"]
        target_identity = identity(
            TARGET_OBJECT_ID,
            TARGET_FEATURE_ID,
            "seed-target",
        )
        control_identity = identity(
            CONTROL_OBJECT_ID,
            CONTROL_FEATURE_ID,
            "seed-control",
        )
        with seed_session._transaction("attach-stable-identities"):
            seed_session.attach_object_identity(
                seed_session.get_object(target_name),
                target_identity,
            )
            seed_session.attach_object_identity(
                seed_session.get_object(control_name),
                control_identity,
            )
            seed_session.doc.recompute()

        baseline_source = ROOT / "base.FCStd"
        executor.checkpoint_fcstd(seed_session, baseline_source)
        baseline_source.chmod(0o600)
        head = store.import_trusted_fcstd(PROJECT_ID, baseline_source, lease)
        base_path = store.revision_model_path(PROJECT_ID, head.revision_id)
        base_digest = sha256(base_path)
        executor.close(seed_session)
        seed_session = None

        baseline_session = executor.load_fcstd(base_path)
        baseline_binding = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=head.revision_id,
            session=baseline_session,
        )
        slot = SessionSlot(baseline_binding)
        coordinator = CandidateCoordinator(
            store=store,
            snapshot_port=executor,
            session_slot=slot,
        )

        target_selector_mapping = target_identity.to_selector(
            project_id=PROJECT_ID,
            revision_id=head.revision_id,
            entity_kind=EntityKind.FEATURE,
        ).to_mapping()
        control_selector_mapping = control_identity.to_selector(
            project_id=PROJECT_ID,
            revision_id=head.revision_id,
            entity_kind=EntityKind.FEATURE,
        ).to_mapping()

        # The wire contract is closed: no legacy Name fallback or partial selector is accepted.
        malformed_codes = []
        for malformed in (
            {key: value for key, value in target_selector_mapping.items() if key != "provenance"},
            {**target_selector_mapping, "Name": target_name},
        ):
            try:
                SelectorV1.from_mapping(malformed)
            except SelectorError as error:
                malformed_codes.append(error.code.value)
            else:
                raise AssertionError("malformed selector was accepted")

        active = coordinator.begin(
            project_id=PROJECT_ID,
            expected_head=head,
            lease=lease,
        )
        target_selector = SelectorV1.from_mapping(target_selector_mapping)
        target = resolve_selector(
            target_selector,
            identified_objects(active.binding.session),
            project_id=PROJECT_ID,
            revision_id=active.base_head.revision_id,
        )
        resolved_identity = active.binding.session.read_object_identity(target)
        if resolved_identity != target_identity:
            raise AssertionError("selector resolved the wrong identity")
        modify_part(active.binding.session, target.Name, "length", 15)

        control_selector = SelectorV1.from_mapping(control_selector_mapping)
        control = resolve_selector(
            control_selector,
            identified_objects(active.binding.session),
            project_id=PROJECT_ID,
            revision_id=active.base_head.revision_id,
        )
        if float(control.Length) != 7.0:
            raise AssertionError("control object changed during targeted modification")

        checkpointed = coordinator.checkpoint(candidate=active, lease=lease)
        executor.export_step(candidate=checkpointed, lease=lease)
        sealed = coordinator.seal(candidate=checkpointed, lease=lease)
        evidence = executor.collect_evidence(candidate=sealed)
        snapshot = evidence.snapshot
        entities = {item.object_id: item for item in snapshot.entities}
        preservations = {item.target: item for item in snapshot.preservations}
        target_observation = entities[TARGET_OBJECT_ID]
        control_observation = entities[CONTROL_OBJECT_ID]
        target_preservation = preservations[TARGET_FEATURE_ID]
        control_preservation = preservations[CONTROL_FEATURE_ID]

        sealed_target = resolve_selector(
            target_selector,
            identified_objects(sealed.binding.session),
            project_id=PROJECT_ID,
            revision_id=sealed.base_head.revision_id,
        )
        sealed_identity = sealed.binding.session.read_object_identity(sealed_target)

        success_facts = {
            "candidate_revision": sealed.revision.id,
            "snapshot_revision": snapshot.candidate_revision,
            "selector_revision": target_selector.revision_id,
            "base_revision": sealed.base_head.revision_id,
            "target_length": parameter(target_observation, "length"),
            "control_length": parameter(control_observation, "length"),
            "target_preserved": target_preservation.preserved,
            "target_changed_fields": list(target_preservation.changed_fields),
            "target_digest_changed": (
                target_preservation.before_digest != target_preservation.after_digest
            ),
            "control_preserved": control_preservation.preserved,
            "control_changed_fields": list(control_preservation.changed_fields),
            "control_digest_stable": (
                control_preservation.before_digest == control_preservation.after_digest
            ),
            "sealed_identity_stable": sealed_identity == target_identity,
            "entity_ids": sorted(entities),
            "malformed_codes": malformed_codes,
        }

        success_rollback = coordinator.rollback(candidate=sealed, lease=lease)
        if success_rollback.head_committed:
            raise AssertionError("evidence-only candidate unexpectedly advanced HEAD")
        if store.load_head(PROJECT_ID) != head or slot.current() is not baseline_binding:
            raise AssertionError("success rollback did not restore the base authority")

        failure_codes = []
        head_stable_after_failure = []
        slot_stable_after_failure = []
        for case in ("stale", "zero", "duplicate"):
            failed_candidate = coordinator.begin(
                project_id=PROJECT_ID,
                expected_head=head,
                lease=lease,
            )
            try:
                if case == "stale":
                    raw_selector = {
                        **target_selector_mapping,
                        "revision_id": "revision_ffffffffffffffffffffffffffffffff",
                    }
                    expected_code = SelectorErrorCode.STALE_REVISION
                elif case == "zero":
                    raw_selector = {
                        **target_selector_mapping,
                        "object_id": "object_ffffffffffffffffffffffffffffffff",
                    }
                    expected_code = SelectorErrorCode.ZERO_MATCH
                else:
                    raw_selector = target_selector_mapping
                    expected_code = SelectorErrorCode.DUPLICATE_IDENTITY
                    duplicate_source = resolve_selector(
                        SelectorV1.from_mapping(control_selector_mapping),
                        identified_objects(failed_candidate.binding.session),
                        project_id=PROJECT_ID,
                        revision_id=failed_candidate.base_head.revision_id,
                    )
                    failed_candidate.binding.session.doc.copyObject(duplicate_source)
                    failed_candidate.binding.session.doc.recompute()
                try:
                    resolve_selector(
                        SelectorV1.from_mapping(raw_selector),
                        identified_objects(failed_candidate.binding.session),
                        project_id=PROJECT_ID,
                        revision_id=failed_candidate.base_head.revision_id,
                    )
                except SelectorError as error:
                    if error.code is not expected_code:
                        raise AssertionError(
                            f"{case} selector returned {error.code.value}"
                        ) from error
                    failure_codes.append(error.code.value)
                else:
                    raise AssertionError(f"{case} selector failure was accepted")
            finally:
                rollback = coordinator.rollback(candidate=failed_candidate, lease=lease)
            head_stable_after_failure.append(
                not rollback.head_committed and store.load_head(PROJECT_ID) == head
            )
            slot_stable_after_failure.append(slot.current() is baseline_binding)

        payload = {
            **success_facts,
            "failure_codes": failure_codes,
            "head_stable_after_failure": head_stable_after_failure,
            "slot_stable_after_failure": slot_stable_after_failure,
            "base_digest_unchanged": sha256(base_path) == base_digest,
            "final_head": store.load_head(PROJECT_ID).to_mapping(),
            "base_head": head.to_mapping(),
            "baseline_open": baseline_binding.session.doc is not None,
        }
finally:
    close_best_effort(baseline_session)
    close_best_effort(seed_session)

print("S3_SELECTOR_RESULT=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
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


def _run_review_restart_case(
    existing_freecad_python: str,
    tmp_path: Path,
) -> dict[str, dict[str, object]]:
    source = Path(__file__).resolve().parent.parent / "src"
    work_root = tmp_path / "review-restart"
    work_root.mkdir(mode=0o700)
    work_root.chmod(0o700)

    def run_phase(case: str) -> dict[str, object]:
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
        assert process.returncode == 0, process.stderr + "\n" + process.stdout
        lines = [
            line
            for line in process.stdout.splitlines()
            if line.startswith("TK9_RESULT=")
        ]
        assert len(lines) == 1, process.stdout
        payload = json.loads(lines[0].removeprefix("TK9_RESULT="))
        assert payload["case"] == case
        return payload

    prepared = run_phase("review_restart_prepare")
    decided = run_phase("review_restart_decide")
    assert prepared["pid"] != decided["pid"]
    return {"prepared": prepared, "decided": decided}


def _run_selector_preservation_case(
    existing_freecad_python: str,
    tmp_path: Path,
) -> dict[str, object]:
    source = Path(__file__).resolve().parent.parent / "src"
    work_root = tmp_path / "selector-preservation"
    work_root.mkdir(mode=0o700)
    work_root.chmod(0o700)
    code = (
        _SELECTOR_PRESERVATION_CHILD.replace("__SOURCE__", repr(str(source)))
        .replace("__WORK_ROOT__", repr(str(work_root)))
    )
    process = subprocess.run(
        [existing_freecad_python, "-c", code],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert process.returncode == 0, process.stderr + "\n" + process.stdout
    lines = [
        line
        for line in process.stdout.splitlines()
        if line.startswith("S3_SELECTOR_RESULT=")
    ]
    assert len(lines) == 1, process.stdout
    return json.loads(lines[0].removeprefix("S3_SELECTOR_RESULT="))


def _assert_rollback(payload: dict[str, object]) -> None:
    assert payload["status"] == "failed"
    assert payload["durable_roundtrip"] is True
    assert payload["committed_revision"] is None
    assert payload["final_head"] == payload["base_head"]
    assert payload["base_revision_unchanged"] is True
    assert payload["base_model_digest_unchanged"] is True
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


def _assert_six_operation_geometry(value: dict[str, object]) -> None:
    expected_volume = 7200.0 + 20.0 * math.pi
    assert value["volume"] == pytest.approx(expected_volume)
    assert value["area"] == pytest.approx(2400.0 + 28.0 * math.pi)
    assert value["bbox"] == pytest.approx([46.0, 13.0, 30.0])
    assert value["valid"] is True
    assert value["solid_count"] == 2


def _assert_selector_geometry(value: dict[str, object]) -> None:
    assert value["volume"] == pytest.approx(300.0)
    assert value["area"] == pytest.approx(310.0)
    assert value["bbox"] == pytest.approx([5.0, 15.0, 4.0])
    assert value["valid"] is True
    assert value["solid_count"] == 1


def _assert_two_managed_entities(payload: dict[str, object]) -> None:
    slot_entities = payload["slot_entities"]
    reload_entities = payload["reload_entities"]
    assert len(slot_entities) == 2
    assert len(reload_entities) == 2
    assert {item["type"] for item in slot_entities} == {"Part::Box", "Part::Cylinder"}
    assert {item["object_id"] for item in slot_entities} == {
        item["object_id"] for item in reload_entities
    }
    assert {item["feature_id"] for item in slot_entities} == {
        item["feature_id"] for item in reload_entities
    }


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
    assert payload["step_oks"] == [True, True, True, True, True, True]
    assert payload["step_operations"] == [
        "box",
        "cylinder",
        "modify",
        "move",
        "rotate",
        "inspect",
    ]
    values = payload["step_values"]
    assert values[0]["after"]["volume_mm3"] == pytest.approx(6000.0)
    assert values[1]["after"]["volume_mm3"] == pytest.approx(20.0 * math.pi)
    assert values[2]["after"]["volume_mm3"] == pytest.approx(7200.0)
    assert values[3]["after"]["volume_mm3"] == pytest.approx(20.0 * math.pi)
    assert values[4]["after"]["volume_mm3"] == pytest.approx(7200.0)
    assert values[2]["object_id"] == values[0]["object_id"]
    assert values[3]["object_id"] == values[1]["object_id"]
    assert values[4]["object_id"] == values[0]["object_id"]
    assert values[4]["after"]["placement"][:3] == pytest.approx([16.0, 4.0, 0.0])
    assert {item["object_id"] for item in values[5]["entities"]} == {
        values[0]["object_id"],
        values[1]["object_id"],
    }
    assert values[5]["shape"]["volume_mm3"] == pytest.approx(
        7200.0 + 20.0 * math.pi
    )
    assert payload["report_passed"] == [True]
    assert len(payload["verdicts"]) == 10
    assert {item["outcome"] for item in payload["verdicts"]} == {"pass"}
    assert [item["format"] for item in payload["artifacts"]] == ["fcstd", "step"]
    _assert_artifact_lineage(payload)
    _assert_two_managed_entities(payload)
    _assert_six_operation_geometry(payload["slot_geometry"])
    _assert_six_operation_geometry(payload["reload_geometry"])
    _assert_six_operation_geometry(payload["step_reload_geometry"])
    assert payload["transitions"][-1] == "commit"
    _assert_layout(payload, journal_state="committed", manifest_count=2)


@pytest.mark.slow
def test_real_task_kernel_bootstraps_empty_project_and_resolves_result_ref(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(existing_freecad_python, tmp_path, "empty_success")
    assert payload["base_model_was_empty"] is True
    assert payload["status"] == "succeeded"
    assert payload["candidate_revision"] == payload["committed_revision"]
    assert payload["candidate_revision"] == payload["final_head"]["revision_id"]
    assert payload["slot_is_baseline"] is False
    assert payload["step_oks"] == [True, True, True, True, True, True]
    assert payload["step_operations"] == [
        "box",
        "cylinder",
        "modify",
        "move",
        "rotate",
        "inspect",
    ]
    values = payload["step_values"]
    assert values[0]["object_id"].startswith("object_")
    assert values[1]["object_id"].startswith("object_")
    assert values[0]["object_id"] != values[1]["object_id"]
    assert values[2]["object_id"] == values[0]["object_id"]
    assert values[3]["object_id"] == values[1]["object_id"]
    assert values[4]["object_id"] == values[0]["object_id"]
    _assert_artifact_lineage(payload)
    _assert_two_managed_entities(payload)
    _assert_six_operation_geometry(payload["slot_geometry"])
    _assert_six_operation_geometry(payload["reload_geometry"])
    _assert_six_operation_geometry(payload["step_reload_geometry"])
    assert payload["transitions"][-1] == "commit"
    _assert_layout(payload, journal_state="committed", manifest_count=2)


@pytest.mark.slow
def test_real_review_drafts_survive_restart_and_serialize_same_base_decisions(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    result = _run_review_restart_case(existing_freecad_python, tmp_path)
    prepared = result["prepared"]
    decided = result["decided"]
    prepared_tasks = prepared["tasks"]
    assert len(prepared_tasks) == 2
    accept_before, stale_before = prepared_tasks
    base_head = prepared["base_head"]

    assert prepared["final_head"] == base_head
    assert prepared["task_record_count"] == 2
    assert {item["status"] for item in prepared_tasks} == {"awaiting_user_review"}
    assert {item["review_policy"] for item in prepared_tasks} == {"require_review"}
    assert accept_before["draft"]["id"] != stale_before["draft"]["id"]
    assert accept_before["candidate_revision"] != stale_before["candidate_revision"]
    for item in prepared_tasks:
        draft = item["draft"]
        assert draft["task_id"] == item["id"]
        assert draft["revision_id"] == item["candidate_revision"]
        assert draft["base_revision"] == base_head["revision_id"]
        assert draft["base_generation"] == base_head["generation"]
        assert draft["base_manifest_sha256"] == base_head["manifest_sha256"]
        assert item["committed_revision"] is None
        assert item["transitions"][-2:] == ["prepare_review", "publish_draft"]

    assert decided["base_head"] == base_head
    assert decided["accept_before"] == accept_before
    assert decided["stale_before"] == stale_before
    accepted = decided["accepted"]
    assert decided["durable_accepted"] == accepted
    assert accepted["status"] == "succeeded"
    assert accepted["draft"] == accept_before["draft"]
    assert accepted["committed_revision"] == accept_before["draft"]["revision_id"]
    assert accepted["generation"] == accept_before["generation"] + 2
    assert accepted["transitions"][-2:] == ["accept_draft", "commit"]

    committed_head = decided["head_after_accept"]
    assert committed_head["project_id"] == base_head["project_id"]
    assert committed_head["generation"] == base_head["generation"] + 1
    assert committed_head["revision_id"] == accept_before["draft"]["revision_id"]
    assert committed_head["manifest_sha256"] == accept_before["draft"]["manifest_sha256"]

    assert decided["stale_error"] == "conflict"
    assert decided["stale_after_conflict"] == stale_before
    assert decided["head_after_conflict"] == committed_head
    rejected = decided["rejected"]
    assert decided["durable_rejected"] == rejected
    assert rejected["status"] == "rejected"
    assert rejected["draft"] == stale_before["draft"]
    assert rejected["committed_revision"] is None
    assert rejected["generation"] == stale_before["generation"] + 1
    assert rejected["transitions"][-1] == "reject_draft"
    assert decided["final_head"] == committed_head
    assert decided["task_record_count"] == 2


@pytest.mark.slow
def test_real_task_kernel_rolls_back_after_partial_execution_failure(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(existing_freecad_python, tmp_path, "execution_failure")
    _assert_rollback(payload)
    assert payload["candidate_revision"] is not None
    assert payload["candidate_revision_durable"] is False
    assert payload["step_oks"] == [True, True, False]
    assert payload["step_operations"] == ["box", "cylinder", "modify"]
    assert payload["step_values"][0]["after"]["volume_mm3"] == pytest.approx(6000.0)
    assert payload["step_values"][1]["after"]["volume_mm3"] == pytest.approx(
        20.0 * math.pi
    )
    assert payload["step_error_codes"][:2] == [None, None]
    assert payload["step_error_codes"][2] == "unexpected_tool_exception"
    assert payload["step_values"][2] is None
    assert payload["step_operations"].count("modify") == 1
    assert not {"move", "rotate", "inspect"} & set(payload["step_operations"])
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
    assert payload["step_oks"] == [True, True, True, True, True, True]
    assert payload["step_values"][0]["after"]["volume_mm3"] == pytest.approx(6000.0)
    assert payload["step_values"][1]["after"]["volume_mm3"] == pytest.approx(
        20.0 * math.pi
    )
    assert payload["step_values"][2]["after"]["volume_mm3"] == pytest.approx(7200.0)
    assert payload["report_passed"] == [False]
    assert len(payload["verdicts"]) == 10
    failed = [item for item in payload["verdicts"] if item["outcome"] == "fail"]
    assert [item["id"] for item in failed] == ["volume"]
    assert failed[0]["expected"] == pytest.approx(7201.0 + 20.0 * math.pi)
    assert failed[0]["observed"] == pytest.approx(7200.0 + 20.0 * math.pi)
    assert payload["last_error"] == "acceptance_verification_failed"
    assert [item["format"] for item in payload["artifacts"]] == ["fcstd", "step"]
    _assert_artifact_lineage(payload)
    _assert_six_operation_geometry(payload["reload_geometry"])
    _assert_six_operation_geometry(payload["step_reload_geometry"])
    assert "fail_verification" in payload["transitions"]
    _assert_layout(payload, journal_state="not_committed", manifest_count=2)


@pytest.mark.slow
def test_real_task_kernel_resolves_revision_bound_selector_targets(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(existing_freecad_python, tmp_path, "selector_success")
    assert payload["status"] == "succeeded"
    assert payload["candidate_revision"] == payload["committed_revision"]
    assert payload["candidate_revision"] == payload["final_head"]["revision_id"]
    assert payload["final_head"]["generation"] == payload["base_head"]["generation"] + 1
    assert payload["slot_is_baseline"] is False
    assert payload["step_oks"] == [True, True, True, True]
    assert payload["step_operations"] == ["modify", "move", "rotate", "inspect"]
    values = payload["step_values"]
    assert [value["object_id"] for value in values[:3]] == [
        "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ] * 3
    assert values[0]["after"]["volume_mm3"] == pytest.approx(300.0)
    assert values[1]["after"]["placement"][:3] == pytest.approx([20.0, 0.0, 0.0])
    assert values[2]["after"]["placement"][:3] == pytest.approx([30.0, -5.0, 0.0])
    assert values[2]["after"]["placement"] != values[1]["after"]["placement"]
    assert [item["object_id"] for item in values[3]["entities"]] == [
        "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ]
    assert payload["report_passed"] == [True]
    assert {item["outcome"] for item in payload["verdicts"]} == {"pass"}
    assert [item["format"] for item in payload["artifacts"]] == ["fcstd", "step"]
    _assert_artifact_lineage(payload)
    assert [item["object_id"] for item in payload["slot_entities"]] == [
        "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ]
    assert payload["slot_entities"] == payload["reload_entities"]
    _assert_selector_geometry(payload["slot_geometry"])
    _assert_selector_geometry(payload["reload_geometry"])
    _assert_selector_geometry(payload["step_reload_geometry"])
    assert payload["transitions"][-1] == "commit"
    _assert_layout(payload, journal_state="committed", manifest_count=2)


@pytest.mark.slow
def test_real_task_kernel_rotates_imported_partial_cylinder_about_bound_box_center(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(existing_freecad_python, tmp_path, "partial_cylinder_rotation")
    assert payload["status"] == "succeeded"
    assert payload["candidate_revision"] == payload["committed_revision"]
    assert payload["candidate_revision"] == payload["final_head"]["revision_id"]
    assert payload["final_head"]["generation"] == payload["base_head"]["generation"] + 1
    assert payload["slot_is_baseline"] is False
    assert payload["step_oks"] == [True, True]
    assert payload["step_operations"] == ["rotate", "inspect"]

    rotation = payload["step_values"][0]
    before = rotation["before"]
    after = rotation["after"]
    expected_parameters = {"angle": 180.0, "height": 6.0, "radius": 10.0}
    assert before["object_type"] == after["object_type"] == "Part::Cylinder"
    assert {item["name"]: item["value"] for item in before["parameters"]} == (
        expected_parameters
    )
    assert {item["name"]: item["value"] for item in after["parameters"]} == (
        expected_parameters
    )
    assert before["placement"][:3] == pytest.approx([0.0, 0.0, 0.0])
    assert after["placement"][:3] == pytest.approx([5.0, 5.0, 0.0])
    assert after["placement"][3:] == pytest.approx(
        [0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)]
    )
    assert before["center_of_mass_mm"] == pytest.approx(
        [0.0, 40.0 / (3.0 * math.pi), 3.0]
    )
    assert before["center_of_mass_mm"] != pytest.approx([0.0, 5.0, 3.0])
    assert after["center_of_mass_mm"] == pytest.approx(
        [5.0 - 40.0 / (3.0 * math.pi), 5.0, 3.0]
    )
    assert before["volume_mm3"] == pytest.approx(300.0 * math.pi)
    assert after["volume_mm3"] == pytest.approx(300.0 * math.pi)
    assert before["bbox_mm"] == pytest.approx([20.0, 10.0, 6.0])
    assert after["bbox_mm"] == pytest.approx([10.0, 20.0, 6.0])
    assert before["valid_shape"] is after["valid_shape"] is True
    assert before["solid_count"] == after["solid_count"] == 1

    assert payload["report_passed"] == [True]
    assert {item["outcome"] for item in payload["verdicts"]} == {"pass"}
    assert [item["format"] for item in payload["artifacts"]] == ["fcstd", "step"]
    _assert_artifact_lineage(payload)
    assert payload["slot_entities"] == payload["reload_entities"]
    assert [item["type"] for item in payload["slot_entities"]] == ["Part::Cylinder"]
    for geometry in (
        payload["slot_geometry"],
        payload["reload_geometry"],
        payload["step_reload_geometry"],
    ):
        assert geometry["volume"] == pytest.approx(300.0 * math.pi)
        assert geometry["bbox"] == pytest.approx([10.0, 20.0, 6.0])
        assert geometry["valid"] is True
        assert geometry["solid_count"] == 1
    assert payload["transitions"][-1] == "commit"
    _assert_layout(payload, journal_state="committed", manifest_count=2)


@pytest.mark.slow
def test_real_selector_preservation_is_reload_bound_and_failure_isolated(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_selector_preservation_case(existing_freecad_python, tmp_path)

    assert payload["snapshot_revision"] == payload["candidate_revision"]
    assert payload["candidate_revision"] != payload["base_revision"]
    assert payload["selector_revision"] == payload["base_revision"]
    assert payload["target_length"] == pytest.approx(15.0)
    assert payload["control_length"] == pytest.approx(7.0)
    assert payload["target_preserved"] is False
    assert payload["target_digest_changed"] is True
    assert "parameters.length" in payload["target_changed_fields"]
    assert payload["control_preserved"] is True
    assert payload["control_changed_fields"] == []
    assert payload["control_digest_stable"] is True
    assert payload["sealed_identity_stable"] is True
    assert payload["entity_ids"] == [
        "object_11111111111111111111111111111111",
        "object_22222222222222222222222222222222",
    ]
    assert payload["malformed_codes"] == ["missing_field", "unknown_field"]

    assert payload["failure_codes"] == [
        "stale_revision",
        "zero_match",
        "duplicate_identity",
    ]
    assert payload["head_stable_after_failure"] == [True, True, True]
    assert payload["slot_stable_after_failure"] == [True, True, True]
    assert payload["final_head"] == payload["base_head"]
    assert payload["base_digest_unchanged"] is True
    assert payload["baseline_open"] is True
