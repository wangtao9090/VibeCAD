"""Opt-in end-to-end TaskService gates against the installed FreeCAD runtime."""

from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path

import pytest

from vibecad.runtime import status as runtime_status


def _freecad_child_environment(
    python: str,
    *,
    source: Path | None = None,
) -> dict[str, str]:
    """Build every real FreeCAD child environment through the runtime seam."""

    prefix = Path(python).parent.parent.resolve()
    environment = runtime_status.freecad_process_environment(os.environ)
    python_paths = [str(prefix / "lib")]
    if source is not None:
        python_paths.append(str(source))
    inherited = environment.get("PYTHONPATH")
    if inherited:
        python_paths.append(inherited)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    return environment


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


@pytest.fixture(scope="session")
def current_managed_freecad_python() -> str:
    """Require the already-installed, current managed generation; never repair it."""

    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the Agent-first real gate")

    from vibecad.runtime import paths, spec

    if paths.user_override_env() is not None:
        pytest.fail("Agent-first acceptance requires the current managed runtime, not an override")
    prefix = paths.env_prefix().expanduser().resolve(strict=False)
    selected = paths.active_runtime_prefix().expanduser().resolve(strict=False)
    requested = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    python = paths.env_python_for(prefix)
    if requested and Path(requested).expanduser().resolve(strict=False) != python.resolve(
        strict=False
    ):
        pytest.fail("VIBECAD_MANAGED_FREECAD_PYTHON is not the selected managed generation")
    if selected != prefix or not python.is_file():
        pytest.fail("install the current managed runtime before running Agent-first acceptance")
    if runtime_status.read_prefix_receipt(prefix) != spec.expected_receipt():
        pytest.fail("the selected managed runtime receipt is not current")
    if not runtime_status.runtime_ready() or not runtime_status.verify_runtime(python):
        pytest.fail("the selected managed runtime failed the exact current-runtime probe")
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
    _initialize_candidate_file_limit_runtime,
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


_initialize_candidate_file_limit_runtime()
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
        runtime_head=head,
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
                sha256(baseline_source),
                baseline_source.stat().st_size,
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
        runtime_head=head_before,
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
    _initialize_candidate_file_limit_runtime,
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


_initialize_candidate_file_limit_runtime()
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
        head = store.import_trusted_fcstd(
            PROJECT_ID,
            baseline_source,
            sha256(baseline_source),
            baseline_source.stat().st_size,
            lease,
        )
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


_APPLICATION_CHILD = r"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, __SOURCE__)
os.environ.pop("VIBECAD_HOME", None)
os.environ["VIBECAD_FREECAD_ENV"] = __EXPECTED_PREFIX__

from vibecad.application.agent import AgentApplication
from vibecad.execution.executor import InProcessCadExecutor
from vibecad.interaction.checkouts import HeadCheckoutSource
from vibecad.runtime import paths as runtime_paths
from vibecad.runtime import status as runtime_status
from vibecad.runtime.installer import RuntimeInstaller
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy
from vibecad.workflow.store import StoredTaskRun

PHASE = __PHASE__
STATE = json.loads(__STATE__)
ROOT = Path(__WORK_ROOT__)
EXPECTED_PREFIX = Path(__EXPECTED_PREFIX__)
DATA_ROOT = ROOT / "data"
REVIEW_TASK_ID = "task_dddddddddddddddddddddddddddddddd"
IMPORT_TASK_ID = "task_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


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
    operation: str,
    *,
    args: dict[str, object] | None = None,
) -> ModelCommand:
    return ModelCommand(
        id=command_id,
        op=operation,
        target={},
        args={} if args is None else args,
        preserve=(),
        source=ValueSource.MODEL,
        depends_on=(),
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
    acceptance_id: str,
    *,
    volume: float,
    bbox: tuple[float, float, float],
    solids: int,
) -> AcceptanceSpec:
    return AcceptanceSpec(
        id=acceptance_id,
        criteria=(
            criterion(
                "volume",
                AcceptanceKind.GEOMETRY,
                "volume",
                "body",
                volume,
                tolerance=1e-7,
                parameters={"unit": "mm^3"},
            ),
            criterion(
                "bbox",
                AcceptanceKind.GEOMETRY,
                "bbox",
                "body",
                bbox,
                tolerance=1e-7,
                parameters={"unit": "mm"},
            ),
            criterion("valid", AcceptanceKind.TOPOLOGY, "valid_shape", "body", True),
            criterion("solids", AcceptanceKind.TOPOLOGY, "solid_count", "body", solids),
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


def empty_program(task_id: str, base_revision: str) -> ModelProgram:
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
        ),
        acceptance=acceptance(
            "acceptance-application-empty",
            volume=6000.0,
            bbox=(10.0, 20.0, 30.0),
            solids=1,
        ),
    )


def imported_program(task_id: str, base_revision: str) -> ModelProgram:
    return ModelProgram(
        task_id=task_id,
        base_revision=base_revision,
        operations=(command("inspect", "inspect_model"),),
        acceptance=acceptance(
            "acceptance-application-import",
            volume=6000.0 + 20.0 * math.pi,
            bbox=(32.0, 22.0, 30.0),
            solids=2,
        ),
    )


def require_task(value: object) -> StoredTaskRun:
    if type(value) is not StoredTaskRun:
        raise AssertionError("AgentApplication returned a task-service failure")
    return value


def create_import_source(path: Path) -> None:
    import FreeCAD

    document = FreeCAD.newDocument("S36PhotoImport")
    try:
        box = document.addObject("Part::Box", "PhotoBox")
        box.Length = 10
        box.Width = 20
        box.Height = 30
        cylinder = document.addObject("Part::Cylinder", "PhotoCylinder")
        cylinder.Radius = 2
        cylinder.Height = 5
        cylinder.Placement.Base.x = 30
        document.recompute()
        document.saveAs(str(path))
    finally:
        FreeCAD.closeDocument(document.Name)
    path.chmod(0o600)


def import_entities(app: AgentApplication, project_id: str, revision_id: str):
    port = InProcessCadExecutor(store=app._revision_store)
    session = port.load_fcstd(
        app._revision_store.revision_model_path(project_id, revision_id)
    )
    try:
        return [
            {
                "object_id": identity.object_id,
                "feature_id": identity.feature_id,
                "object_type": identity.object_type,
                "source": identity.provenance.source.value,
                "volume": float(obj.Shape.Volume),
            }
            for obj, identity in session.list_object_identities()
        ]
    finally:
        port.close(session)


secure_dir(ROOT)
legacy_prefix = runtime_paths.legacy_env_prefix()
legacy_sentinel = legacy_prefix / ".vibecad_ready"
sentinel_before = (
    legacy_sentinel.stat().st_dev,
    legacy_sentinel.stat().st_ino,
    legacy_sentinel.stat().st_size,
    sha256(legacy_sentinel),
)
installer_calls = []


def forbidden_install(_self):
    installer_calls.append("install")
    raise AssertionError("AgentApplication invoked the runtime installer")


RuntimeInstaller.install = forbidden_install
runtime_facts = {
    "expected_prefix": str(EXPECTED_PREFIX),
    "legacy_prefix": str(legacy_prefix),
    "active_prefix": str(runtime_paths.active_runtime_prefix()),
    "interpreter_under_prefix": Path(sys.executable).resolve().is_relative_to(
        legacy_prefix.resolve()
    ),
    "external_receipt": runtime_status.legacy_external_receipt(legacy_prefix),
    "receipt_state": runtime_status.runtime_receipt_state().value,
    "runtime_ready": runtime_status.runtime_ready(),
}

app = None
payload = {}
try:
    app = AgentApplication.open(data_root=DATA_ROOT)
    if PHASE == "prepare":
        empty = app.bootstrap_empty()
        if not (
            empty.head.generation == 0
            and empty.revision.base_revision is None
            and empty.revision.model is None
        ):
            raise AssertionError("empty bootstrap did not publish exact revision zero")

        source = ROOT / "photo-source.FCStd"
        create_import_source(source)
        source_before = sha256(source)
        imported = app.bootstrap_import(source=source)
        source_after = sha256(source)
        entities = import_entities(
            app,
            imported.head.project_id,
            imported.head.revision_id,
        )
        if not (
            imported.head.generation == 0
            and imported.revision.base_revision is None
            and imported.revision.model is not None
            and source_before == source_after
        ):
            raise AssertionError("import bootstrap did not preserve its revision-zero contract")

        created_review = require_task(
            app.create_task(
                task_id=REVIEW_TASK_ID,
                project_id=empty.head.project_id,
                reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                review_policy=ReviewPolicy.REQUIRE_REVIEW,
            )
        )
        awaiting = require_task(
            app.submit_model_program(
                task_id=REVIEW_TASK_ID,
                expected_generation=created_review.generation,
                program=empty_program(REVIEW_TASK_ID, empty.head.revision_id),
            )
        )
        if awaiting.task_run.draft is None:
            raise AssertionError("review draft was not published")

        created_import = require_task(
            app.create_task(
                task_id=IMPORT_TASK_ID,
                project_id=imported.head.project_id,
                reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                review_policy=ReviewPolicy.AUTO_COMMIT,
            )
        )
        imported_terminal = require_task(
            app.submit_model_program(
                task_id=IMPORT_TASK_ID,
                expected_generation=created_import.generation,
                program=imported_program(IMPORT_TASK_ID, imported.head.revision_id),
            )
        )

        empty_runtime = app._runtimes[empty.head.project_id]
        imported_runtime = app._runtimes[imported.head.project_id]
        empty_binding = empty_runtime._coordinator._session_slot.current()
        imported_binding = imported_runtime._coordinator._session_slot.current()
        isolated = (
            empty_runtime is not imported_runtime
            and empty_runtime._coordinator is not imported_runtime._coordinator
            and empty_runtime.service._executor is not imported_runtime.service._executor
            and empty_binding.session is not imported_binding.session
            and empty_binding.project_id == empty.head.project_id
            and imported_binding.project_id == imported.head.project_id
        )

        checkout_head_before = app._revision_store.load_head(imported.head.project_id)
        checkout_source = app._revision_store.revision_model_path(
            imported.head.project_id,
            checkout_head_before.revision_id,
        )
        checkout_source_before = sha256(checkout_source)
        checkout = app.open_checkout(
            open_key="checkout_open_ffffffffffffffffffffffffffffffff",
            source=HeadCheckoutSource(project_id=imported.head.project_id),
        )
        if checkout.local_path is None:
            raise AssertionError("managed checkout did not expose its local copy")
        edit_port = InProcessCadExecutor(store=app._revision_store)
        edit_session = edit_port.load_fcstd(checkout.local_path)
        edited_model = ROOT / "freecad-checkout-edit.FCStd"
        try:
            edited_box = next(
                obj for obj in edit_session.doc.Objects if obj.TypeId == "Part::Box"
            )
            edited_box.Length = 14
            edit_session.doc.recompute()
            edit_session.doc.saveAs(str(edited_model))
        finally:
            edit_port.close(edit_session)
        edited_model.chmod(0o600)
        os.replace(edited_model, checkout.local_path)
        dirty = app.get_checkout(checkout_id=checkout.checkout_id)
        checkout_source_after = sha256(checkout_source)
        checkout_head_after = app._revision_store.load_head(imported.head.project_id)
        closed_checkout = app.close_checkout(checkout_id=checkout.checkout_id)

        payload = {
            "phase": PHASE,
            "pid": os.getpid(),
            "runtime": runtime_facts,
            "empty": {
                "head": empty.head.to_mapping(),
                "revision": empty.revision.to_mapping(),
            },
            "imported": {
                "head": imported.head.to_mapping(),
                "revision": imported.revision.to_mapping(),
                "entities": entities,
                "source_unchanged": source_before == source_after,
            },
            "isolation": {
                "isolated": isolated,
                "runtime_count": len(app._runtimes),
            },
            "import_task": {
                "status": imported_terminal.task_run.status.value,
                "committed_revision": imported_terminal.task_run.committed_revision,
            },
            "checkout": {
                "dirty": dirty.dirty,
                "source_unchanged": checkout_source_before == checkout_source_after,
                "head_unchanged": checkout_head_before == checkout_head_after,
                "closed_state": closed_checkout.state.value,
                "closed_path": closed_checkout.local_path,
                "wire_has_path": "local_path" in dirty.to_wire_mapping(),
            },
            "restart": {
                "project_id": empty.head.project_id,
                "task_id": REVIEW_TASK_ID,
                "draft_id": awaiting.task_run.draft.id,
                "generation": awaiting.generation,
                "draft_revision": awaiting.task_run.draft.revision_id,
                "head_before": empty.head.to_mapping(),
            },
        }
    elif PHASE == "accept":
        before = require_task(app.get_task(task_id=STATE["task_id"]))
        if not (
            before.generation == STATE["generation"]
            and before.task_run.draft is not None
            and before.task_run.draft.id == STATE["draft_id"]
        ):
            raise AssertionError("durable draft did not survive Application restart")
        accepted = require_task(
            app.accept_draft(
                task_id=STATE["task_id"],
                draft_id=STATE["draft_id"],
                expected_generation=before.generation,
            )
        )
        final_head = app._revision_store.load_head(STATE["project_id"])
        entities = import_entities(
            app,
            final_head.project_id,
            final_head.revision_id,
        )
        payload = {
            "phase": PHASE,
            "pid": os.getpid(),
            "runtime": runtime_facts,
            "before_generation": before.generation,
            "accepted": {
                "status": accepted.task_run.status.value,
                "committed_revision": accepted.task_run.committed_revision,
                "generation": accepted.generation,
            },
            "final_head": final_head.to_mapping(),
            "entities": entities,
        }
    else:
        raise AssertionError("unknown Application integration phase")
finally:
    if app is not None:
        app.close()
        payload["application_closed"] = app._closed and not app._runtimes
    sentinel_after = (
        legacy_sentinel.stat().st_dev,
        legacy_sentinel.stat().st_ino,
        legacy_sentinel.stat().st_size,
        sha256(legacy_sentinel),
    )
    payload["installer_calls"] = installer_calls
    payload["legacy_runtime_unchanged"] = sentinel_before == sentinel_after

print("S3_APPLICATION_RESULT=" + json.dumps(payload, ensure_ascii=False, sort_keys=True))
"""


_RUNTIME_ADOPTION_CHILD = r"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, __SOURCE__)
os.environ.pop("VIBECAD_HOME", None)
os.environ["VIBECAD_FREECAD_ENV"] = __EXPECTED_PREFIX__

from vibecad.runtime import installer as installer_module
from vibecad.runtime import micromamba, paths, status
from vibecad.runtime.installer import InstallError, RuntimeInstaller

EXPECTED_PREFIX = Path(__EXPECTED_PREFIX__)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def signature(path: Path) -> tuple[int, int, int, int, str]:
    info = path.stat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        sha256(path),
    )


legacy = paths.legacy_env_prefix()
sentinel = legacy / ".vibecad_ready"
python = paths.env_python_for(legacy)
freecadcmd = legacy / "bin" / "freecadcmd"
before = {
    "sentinel": signature(sentinel),
    "python": signature(python.resolve()),
    "freecadcmd": signature(freecadcmd),
}
receipt_before = paths.external_runtime_receipt().is_file()
receipt_raw_before = paths.external_runtime_receipt().read_text(encoding="utf-8")
current_prefix_before = os.path.lexists(paths.env_prefix())
blocked_commands = []
verify_calls = []


def forbidden(name):
    def fail(*_args, **_kwargs):
        blocked_commands.append(name)
        raise AssertionError(f"runtime adoption invoked forbidden operation: {name}")

    return fail


original_verify = status.verify_runtime


def observed_verify(selected=None):
    verify_calls.append(str(selected or paths.active_runtime_python()))
    return original_verify(selected)


status.verify_runtime = observed_verify
installer_module._run = forbidden("command")
micromamba.ensure_micromamba = forbidden("micromamba")
RuntimeInstaller._install_server_package = forbidden("pip")
RuntimeInstaller._remove_managed_env = forbidden("delete")

probe_ready = status.verify_runtime(paths.env_python_for(legacy))
try:
    RuntimeInstaller().install()
except InstallError:
    install_failed_closed = True
else:
    raise AssertionError("pre-epoch external runtime was incorrectly accepted")

receipt_path = paths.external_runtime_receipt()
raw = receipt_path.read_text(encoding="utf-8")
receipt = json.loads(raw)
prefix_info = legacy.stat()
after = {
    "sentinel": signature(sentinel),
    "python": signature(python.resolve()),
    "freecadcmd": signature(freecadcmd),
}
payload = {
    "expected_prefix": str(EXPECTED_PREFIX),
    "legacy_prefix": str(legacy),
    "active_prefix": str(paths.active_runtime_prefix()),
    "receipt_before": receipt_before,
    "receipt_path": str(receipt_path),
    "receipt_canonical": raw == json.dumps(receipt, sort_keys=True),
    "receipt": receipt,
    "receipt_validated": status._validated_external_binding() is not None,
    "receipt_state": status.runtime_receipt_state().value,
    "runtime_ready": status.runtime_ready(),
    "install_failed_closed": install_failed_closed,
    "probe_ready": probe_ready,
    "verify_calls": verify_calls,
    "blocked_commands": blocked_commands,
    "external_runtime_unchanged": before == after,
    "external_receipt_unchanged": receipt_raw_before == raw,
    "current_prefix_unchanged": current_prefix_before == os.path.lexists(paths.env_prefix()),
    "old_receipt_missing_identity": all(
        key not in receipt
        for key in ("server_package_epoch", "mcp_version", "public_surface_sha256")
    ),
    "prefix_identity_still_matches": (
        receipt.get("prefix") == str(legacy)
        and receipt.get("prefix_device") == prefix_info.st_dev
        and receipt.get("prefix_inode") == prefix_info.st_ino
    ),
}
print("S3_RUNTIME_ADOPTION_RESULT=" + json.dumps(payload, sort_keys=True))
"""


_AGENT_FIRST_CHILD = r"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, __SOURCE__)

import FreeCAD
import Part

from vibecad.application.agent import AgentApplication
from vibecad.execution.executor import ExecutorError, InProcessCadExecutor
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ModelCommand,
    ModelProgram,
    ValueSource,
)

ROOT = Path(__WORK_ROOT__)
PHASE = __PHASE__
STATE = json.loads(__STATE__)
DATA = ROOT / "data"


def canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_path(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def committed_storage_snapshot() -> dict[str, tuple[int, str]]:
    # Exclude durable request/cleanup receipts and their zero-byte tombstones
    # while binding every authoritative user-model file.  A non-empty
    # quarantine still indicates incomplete cleanup and must fail the gate.
    roots = (
        DATA / "projects",
        DATA / "tasks",
        DATA / "checkouts",
        DATA / "artifacts",
        DATA / "bootstrap" / "staging",
        DATA / "bootstrap" / "work",
        DATA / "bootstrap" / "normalized",
    )
    observed = {}
    for root in roots:
        for path in root.rglob("*"):
            if path.is_symlink():
                raise AssertionError("committed storage contains a symlink")
            if path.is_file():
                if path.name.startswith(".quarantine-receipt."):
                    continue
                if path.name.startswith(".quarantine."):
                    if path.stat().st_size != 0:
                        raise AssertionError("project cleanup left a non-empty quarantine")
                    continue
                observed[path.relative_to(DATA).as_posix()] = (
                    path.stat().st_size,
                    digest_path(path),
                )
    return observed


def require(envelope: dict[str, object]) -> dict[str, object]:
    if not envelope.get("ok"):
        raise AssertionError("public request failed: " + canonical(envelope))
    result = envelope.get("result")
    if type(result) is not dict:
        raise AssertionError("public result is not an object")
    return result


def acceptance(*, expected_volume: float | None = None) -> AcceptanceSpec:
    criteria = [
        AcceptanceCriterion(
            id="valid-shape",
            kind=AcceptanceKind.TOPOLOGY,
            check="valid_shape",
            target="body",
            expected=True,
        )
    ]
    if expected_volume is not None:
        criteria.append(
            AcceptanceCriterion(
                id="expected-volume",
                kind=AcceptanceKind.GEOMETRY,
                check="volume",
                target="body",
                expected=expected_volume,
                tolerance=1e-7,
                parameters={"unit": "mm^3"},
            )
        )
    return AcceptanceSpec(id="agent-first-real", criteria=tuple(criteria))


ACCEPTANCE_JSON = canonical(acceptance().to_mapping())


def create_project(app: AgentApplication, key: str, kind: str, source: Path | None = None):
    request = {"schema_version": 1, "create_key": key, "kind": kind}
    if source is not None:
        request["source_path"] = str(source)
    return require(app.create_project_request(request))


def current(app: AgentApplication, project_id: str) -> dict[str, object]:
    return require(
        app.get_project_request({"schema_version": 1, "project_id": project_id})
    )["current"]["head"]


def create_task(app: AgentApplication, project_id: str, policy: str) -> dict[str, object]:
    return require(
        app.create_task_request(
            {
                "schema_version": 1,
                "project_id": project_id,
                "review_policy": policy,
            }
        )
    )


def selector(project_id: str, revision_id: str, observation: dict[str, object]):
    return {
        "schema_version": 1,
        "project_id": project_id,
        "revision_id": revision_id,
        "entity_kind": "object",
        "object_id": observation["object_id"],
        "feature_id": None,
        "object_type": observation["object_type"],
        "semantic_role": observation["semantic_role"],
        "provenance": observation["provenance"],
        "expected_cardinality": 1,
    }


def direct(
    app: AgentApplication,
    project_id: str,
    operation: str,
    *,
    target: dict[str, object] | None = None,
    arguments: dict[str, object] | None = None,
    preserve: list[str] | None = None,
    policy: str = "auto_commit",
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    created = create_task(app, project_id, policy)
    task = created["task_run"]
    base_head = current(app, project_id)
    if task["base_revision"] != base_head["revision_id"]:
        raise AssertionError("task did not bind the coherent public HEAD")
    request = {
        "schema_version": 1,
        "task_id": task["id"],
        "expected_generation": created["generation"],
        "target": {} if target is None else target,
        "arguments": {} if arguments is None else arguments,
        "preserve": [] if preserve is None else preserve,
        "acceptance_json": ACCEPTANCE_JSON,
    }
    terminal = require(app.invoke_direct_operation_request(operation, request))
    return base_head, request, terminal


def command(
    command_id: str,
    operation: str,
    *,
    target: dict[str, object] | None = None,
    arguments: dict[str, object] | None = None,
    preserve: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
) -> ModelCommand:
    return ModelCommand(
        id=command_id,
        op=operation,
        target={} if target is None else target,
        args={} if arguments is None else arguments,
        preserve=preserve,
        source=ValueSource.MODEL,
        depends_on=depends_on,
    )


def six_operation_program(task_id: str, revision_id: str) -> ModelProgram:
    return ModelProgram(
        task_id=task_id,
        base_revision=revision_id,
        operations=(
            command(
                "box",
                "create_box",
                arguments={
                    "length_mm": 10,
                    "width_mm": 20,
                    "height_mm": 30,
                    "position_mm": (0, 0, 0),
                },
            ),
            command(
                "cylinder",
                "create_cylinder",
                arguments={
                    "radius_mm": 2,
                    "height_mm": 5,
                    "position_mm": (30, 0, 0),
                    "axis": "z",
                },
            ),
            command(
                "modify",
                "modify_parameter",
                target={"object": {"command_id": "box", "slot": "object"}},
                arguments={"parameter": "length", "value_mm": 12},
                depends_on=("box",),
            ),
            command(
                "move",
                "move_part",
                target={"object": {"command_id": "cylinder", "slot": "object"}},
                arguments={"position_mm": (40, 5, 0)},
                depends_on=("cylinder",),
            ),
            command(
                "rotate",
                "rotate_part",
                target={"object": {"command_id": "box", "slot": "object"}},
                arguments={"axis": "z", "angle_deg": 90},
                depends_on=("modify",),
            ),
            command("inspect", "inspect_model", depends_on=("move", "rotate")),
        ),
        acceptance=acceptance(
            expected_volume=12.0 * 20.0 * 30.0 + math.pi * 2.0**2 * 5.0
        ),
    )


def submit_program(
    app: AgentApplication,
    project_id: str,
    program_factory,
    *,
    policy: str = "auto_commit",
) -> tuple[dict[str, object], dict[str, object]]:
    created = create_task(app, project_id, policy)
    task = created["task_run"]
    program = program_factory(task["id"], task["base_revision"])
    terminal = require(
        app.submit_model_program_request(
            {
                "schema_version": 1,
                "task_id": task["id"],
                "expected_generation": created["generation"],
                "program_json": canonical(program.to_mapping()),
            }
        )
    )
    return created, terminal


def geometry_from_shape(shape: object) -> dict[str, object]:
    return {
        "volume": float(shape.Volume),
        "bbox": [
            float(shape.BoundBox.XLength),
            float(shape.BoundBox.YLength),
            float(shape.BoundBox.ZLength),
        ],
        "valid": bool(shape.isValid()),
        "solids": len(shape.Solids),
    }


def fcstd_geometry(path: Path) -> dict[str, object]:
    document = FreeCAD.openDocument(str(path))
    try:
        shapes = [
            obj.Shape
            for obj in document.Objects
            if hasattr(obj, "Shape") and not obj.Shape.isNull()
        ]
        if not shapes:
            raise AssertionError("FCStd resource has no shape")
        shape = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
        return geometry_from_shape(shape)
    finally:
        FreeCAD.closeDocument(document.Name)


def verify_delivery(
    app: AgentApplication,
    result: dict[str, object],
    label: str,
) -> dict[str, object]:
    observed = []
    geometries = {}
    for artifact in result["artifacts"]:
        content = app.read_artifact_resource(artifact["resource_uri"])
        raw = base64.b64decode(content.blob, validate=True)
        if len(raw) != artifact["size_bytes"] or digest_bytes(raw) != artifact["sha256"]:
            raise AssertionError("resource bytes do not match the public artifact reference")
        destination = ROOT / f"{label}-{artifact['name']}"
        destination.write_bytes(raw)
        destination.chmod(0o600)
        if artifact["format"] == "fcstd":
            geometries["fcstd"] = fcstd_geometry(destination)
        elif artifact["format"] == "step":
            geometries["step"] = geometry_from_shape(Part.read(str(destination)))
        observed.append(
            {
                "uri": content.uri,
                "mime_type": content.mime_type,
                "format": artifact["format"],
                "size": len(raw),
                "sha256": digest_bytes(raw),
                "base64_roundtrip": base64.b64encode(raw).decode("ascii") == content.blob,
            }
        )
    if set(geometries) != {"fcstd", "step"}:
        raise AssertionError("delivery did not contain FCStd and STEP")
    if abs(geometries["fcstd"]["volume"] - geometries["step"]["volume"]) > 1e-6:
        raise AssertionError("FCStd and STEP reloads disagree")
    return {"resources": observed, "geometries": geometries}


def make_import_source() -> Path:
    source = ROOT / "public-import.FCStd"
    document = FreeCAD.newDocument("AgentFirstImportSource")
    try:
        box = document.addObject("Part::Box", "ImportedBox")
        box.Length = 8
        box.Width = 7
        box.Height = 6
        cylinder = document.addObject("Part::Cylinder", "ImportedCylinder")
        cylinder.Radius = 2
        cylinder.Height = 5
        cylinder.Placement.Base = FreeCAD.Vector(20, 0, 0)
        document.recompute()
        document.saveAs(str(source))
    finally:
        FreeCAD.closeDocument(document.Name)
    source.chmod(0o600)
    return source


def make_rejected_import_source(label: str, *, mixed: bool) -> Path:
    source = ROOT / f"{label}.FCStd"
    document = FreeCAD.newDocument(f"RejectedImport{label}")
    try:
        if mixed:
            box = document.addObject("Part::Box", "SupportedBox")
            box.Length = 3
            box.Width = 4
            box.Height = 5
        sphere = document.addObject("Part::Sphere", "UnsupportedSphere")
        sphere.Radius = 2
        document.recompute()
        document.saveAs(str(source))
    finally:
        FreeCAD.closeDocument(document.Name)
    source.chmod(0o600)
    return source


def revalidate_rejected_import_sources(
    app: AgentApplication,
    sources: dict[str, Path],
) -> dict[str, object]:
    before = {kind: digest_path(source) for kind, source in sources.items()}
    executor = InProcessCadExecutor(store=app._revision_store)
    codes = {}
    previous_cwd = Path.cwd()
    try:
        os.chdir(ROOT)
        for kind, source in sources.items():
            try:
                executor.revalidate_normalized_import(Path(source.name))
            except ExecutorError as error:
                codes[kind] = error.code.value
            else:
                codes[kind] = None
    finally:
        os.chdir(previous_cwd)
    return {
        "codes": codes,
        "sources_unchanged": before
        == {kind: digest_path(source) for kind, source in sources.items()},
    }


app = AgentApplication.open(data_root=DATA)
payload = {}
try:
    if PHASE == "prepare":
        source = make_import_source()
        source_before = digest_path(source)
        empty = create_project(
            app,
            "project_create_00000000000000000000000000000001",
            "empty",
        )
        imported = create_project(
            app,
            "project_create_00000000000000000000000000000002",
            "import_fcstd",
            source,
        )
        imported_current = require(
            app.get_project_request(
                {"schema_version": 1, "project_id": imported["project_id"]}
            )
        )
        if source_before != digest_path(source):
            raise AssertionError("public import changed its source")

        _import_base, _import_request, import_inspection = direct(
            app,
            imported["project_id"],
            "inspect_model",
        )
        supported_import_value = import_inspection["task_run"]["steps"][0]["result"][
            "value"
        ]

        unsupported_source = make_rejected_import_source("unsupported-only", mixed=False)
        mixed_source = make_rejected_import_source("mixed-supported-unsupported", mixed=True)
        rejected_sources = {
            "unsupported": unsupported_source,
            "mixed": mixed_source,
        }
        rejected_source_before = {
            "unsupported": digest_path(unsupported_source),
            "mixed": digest_path(mixed_source),
        }
        real_revalidation = revalidate_rejected_import_sources(app, rejected_sources)
        committed_before_rejections = committed_storage_snapshot()
        unsupported_rejection = app.create_project_request(
            {
                "schema_version": 1,
                "create_key": "project_create_00000000000000000000000000000004",
                "kind": "import_fcstd",
                "source_path": str(unsupported_source),
            }
        )
        committed_between_rejections = committed_storage_snapshot()
        mixed_rejection = app.create_project_request(
            {
                "schema_version": 1,
                "create_key": "project_create_00000000000000000000000000000005",
                "kind": "import_fcstd",
                "source_path": str(mixed_source),
            }
        )
        committed_after_rejections = committed_storage_snapshot()

        project_id = empty["project_id"]
        direct_records = []

        base, request, box_task = direct(
            app,
            project_id,
            "create_box",
            arguments={
                "length_mm": 10,
                "width_mm": 20,
                "height_mm": 30,
                "position_mm": [0, 0, 0],
            },
        )
        box_after = box_task["task_run"]["steps"][0]["result"]["value"]["after"]
        direct_records.append(("create_box", base, request, box_task))

        base, request, cylinder_task = direct(
            app,
            project_id,
            "create_cylinder",
            arguments={
                "radius_mm": 2,
                "height_mm": 5,
                "position_mm": [30, 0, 0],
                "axis": "z",
            },
        )
        cylinder_after = cylinder_task["task_run"]["steps"][0]["result"]["value"]["after"]
        direct_records.append(("create_cylinder", base, request, cylinder_task))

        box_selector = selector(project_id, current(app, project_id)["revision_id"], box_after)
        base, request, modify_task = direct(
            app,
            project_id,
            "modify_parameter",
            target={"object": box_selector},
            arguments={"parameter": "length", "value_mm": 12},
        )
        box_after = modify_task["task_run"]["steps"][0]["result"]["value"]["after"]
        direct_records.append(("modify_parameter", base, request, modify_task))

        cylinder_selector = selector(
            project_id,
            current(app, project_id)["revision_id"],
            cylinder_after,
        )
        base, request, move_task = direct(
            app,
            project_id,
            "move_part",
            target={"object": cylinder_selector},
            arguments={"position_mm": [40, 5, 0]},
        )
        cylinder_after = move_task["task_run"]["steps"][0]["result"]["value"]["after"]
        direct_records.append(("move_part", base, request, move_task))

        box_selector = selector(project_id, current(app, project_id)["revision_id"], box_after)
        base, request, rotate_task = direct(
            app,
            project_id,
            "rotate_part",
            target={"object": box_selector},
            arguments={"axis": "z", "angle_deg": 90},
        )
        box_after = rotate_task["task_run"]["steps"][0]["result"]["value"]["after"]
        direct_records.append(("rotate_part", base, request, rotate_task))

        base, request, inspect_task = direct(app, project_id, "inspect_model")
        direct_records.append(("inspect_model", base, request, inspect_task))
        direct_shape = inspect_task["task_run"]["steps"][0]["result"]["value"]["shape"]

        model_project = create_project(
            app,
            "project_create_00000000000000000000000000000003",
            "empty",
        )
        _model_created, model_task = submit_program(
            app,
            model_project["project_id"],
            six_operation_program,
        )
        model_shape = model_task["task_run"]["steps"][-1]["result"]["value"]["shape"]

        committed_export = require(
            app.export_task_artifacts_request(
                {
                    "schema_version": 1,
                    "export_key": "export_00000000000000000000000000000001",
                    "task_id": inspect_task["task_run"]["id"],
                    "expected_generation": inspect_task["generation"],
                    "revision_id": inspect_task["task_run"]["committed_revision"],
                    "draft_id": None,
                }
            )
        )
        committed_delivery = verify_delivery(app, committed_export, "committed")

        failure_head_before = current(app, project_id)
        failure_selector = selector(project_id, failure_head_before["revision_id"], box_after)
        _failure_base, _failure_request, failed_task = direct(
            app,
            project_id,
            "modify_parameter",
            target={"object": failure_selector},
            arguments={"parameter": "length", "value_mm": 13},
            preserve=["length"],
        )
        failure_head_after = current(app, project_id)

        draft_base, _, accept_draft_task = direct(
            app,
            project_id,
            "create_box",
            arguments={
                "length_mm": 3,
                "width_mm": 4,
                "height_mm": 5,
                "position_mm": [60, 0, 0],
            },
            policy="require_review",
        )
        _, _, reject_draft_task = direct(
            app,
            project_id,
            "create_box",
            arguments={
                "length_mm": 4,
                "width_mm": 5,
                "height_mm": 6,
                "position_mm": [80, 0, 0],
            },
            policy="require_review",
        )
        accept_draft = accept_draft_task["task_run"]["draft"]
        reject_draft = reject_draft_task["task_run"]["draft"]
        draft_export = require(
            app.export_task_artifacts_request(
                {
                    "schema_version": 1,
                    "export_key": "export_00000000000000000000000000000002",
                    "task_id": accept_draft_task["task_run"]["id"],
                    "expected_generation": accept_draft_task["generation"],
                    "revision_id": accept_draft["revision_id"],
                    "draft_id": accept_draft["id"],
                }
            )
        )
        draft_delivery = verify_delivery(app, draft_export, "draft")

        payload = {
            "phase": PHASE,
            "pid": os.getpid(),
            "projects": {
                "empty": empty,
                "imported": imported,
                "imported_current": imported_current,
                "import_source_unchanged": source_before == digest_path(source),
                "supported_import": {
                    "task_status": import_inspection["task_run"]["status"],
                    "entities": supported_import_value["entities"],
                },
                "rejected_imports": {
                    "unsupported": unsupported_rejection,
                    "mixed": mixed_rejection,
                    "real_revalidation": real_revalidation,
                    "sources_unchanged": rejected_source_before
                    == {
                        "unsupported": digest_path(unsupported_source),
                        "mixed": digest_path(mixed_source),
                    },
                    "committed_storage_unchanged": committed_before_rejections
                    == committed_between_rejections
                    == committed_after_rejections,
                },
            },
            "direct": {
                "operations": [record[0] for record in direct_records],
                "base_generations": [record[1]["generation"] for record in direct_records],
                "base_revisions": [record[1]["revision_id"] for record in direct_records],
                "statuses": [record[3]["task_run"]["status"] for record in direct_records],
                "selector_requests": [
                    record[2]["target"]["object"]
                    for record in direct_records
                    if record[2]["target"]
                ],
                "shape": direct_shape,
                "final_task": inspect_task,
            },
            "model_program": {
                "status": model_task["task_run"]["status"],
                "operations": [
                    item["op"] for item in model_task["task_run"]["program"]["operations"]
                ],
                "shape": model_shape,
            },
            "committed_export": committed_export,
            "committed_delivery": committed_delivery,
            "draft_export": draft_export,
            "draft_delivery": draft_delivery,
            "failure": {
                "status": failed_task["task_run"]["status"],
                "committed_revision": failed_task["task_run"]["committed_revision"],
                "step_oks": [
                    item["result"]["ok"] for item in failed_task["task_run"]["steps"]
                ],
                "head_before": failure_head_before,
                "head_after": failure_head_after,
            },
            "restart": {
                "project_id": project_id,
                "base_head": draft_base,
                "accept": {
                    "task_id": accept_draft_task["task_run"]["id"],
                    "generation": accept_draft_task["generation"],
                    "draft_id": accept_draft["id"],
                    "revision_id": accept_draft["revision_id"],
                },
                "reject": {
                    "task_id": reject_draft_task["task_run"]["id"],
                    "generation": reject_draft_task["generation"],
                    "draft_id": reject_draft["id"],
                    "revision_id": reject_draft["revision_id"],
                },
            },
        }
    elif PHASE == "decide":
        before = current(app, STATE["project_id"])
        accept_state = STATE["accept"]
        reject_state = STATE["reject"]
        accepted = require(
            app.accept_draft_request(
                {
                    "schema_version": 1,
                    "task_id": accept_state["task_id"],
                    "draft_id": accept_state["draft_id"],
                    "expected_generation": accept_state["generation"],
                }
            )
        )
        head_after_accept = current(app, STATE["project_id"])
        rejected = require(
            app.reject_draft_request(
                {
                    "schema_version": 1,
                    "task_id": reject_state["task_id"],
                    "draft_id": reject_state["draft_id"],
                    "expected_generation": reject_state["generation"],
                }
            )
        )
        head_after_reject = current(app, STATE["project_id"])
        payload = {
            "phase": PHASE,
            "pid": os.getpid(),
            "before": before,
            "accepted": accepted,
            "head_after_accept": head_after_accept,
            "rejected": rejected,
            "head_after_reject": head_after_reject,
        }
    else:
        raise AssertionError("unknown Agent-first phase")
finally:
    app.close()

print("S3_AGENT_FIRST_RESULT=" + canonical(payload))
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
        env=_freecad_child_environment(existing_freecad_python, source=source),
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
            env=_freecad_child_environment(existing_freecad_python, source=source),
        )
        assert process.returncode == 0, process.stderr + "\n" + process.stdout
        lines = [line for line in process.stdout.splitlines() if line.startswith("TK9_RESULT=")]
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
    code = _SELECTOR_PRESERVATION_CHILD.replace("__SOURCE__", repr(str(source))).replace(
        "__WORK_ROOT__", repr(str(work_root))
    )
    process = subprocess.run(
        [existing_freecad_python, "-c", code],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=_freecad_child_environment(existing_freecad_python, source=source),
    )
    assert process.returncode == 0, process.stderr + "\n" + process.stdout
    lines = [line for line in process.stdout.splitlines() if line.startswith("S3_SELECTOR_RESULT=")]
    assert len(lines) == 1, process.stdout
    return json.loads(lines[0].removeprefix("S3_SELECTOR_RESULT="))


def _run_application_restart_case(
    existing_freecad_python: str,
    tmp_path: Path,
) -> dict[str, dict[str, object]]:
    source = Path(__file__).resolve().parent.parent / "src"
    prefix = Path(existing_freecad_python).parent.parent.resolve()
    work_root = tmp_path / "agent-application"
    work_root.mkdir(mode=0o700)
    work_root.chmod(0o700)
    environment = _freecad_child_environment(existing_freecad_python, source=source)
    environment.pop("VIBECAD_HOME", None)

    def run_phase(
        phase: str,
        state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        code = (
            _APPLICATION_CHILD.replace("__SOURCE__", repr(str(source)))
            .replace("__WORK_ROOT__", repr(str(work_root)))
            .replace("__EXPECTED_PREFIX__", repr(str(prefix)))
            .replace("__PHASE__", repr(phase))
            .replace("__STATE__", repr(json.dumps(state or {}, sort_keys=True)))
        )
        process = subprocess.run(
            [existing_freecad_python, "-c", code],
            capture_output=True,
            text=True,
            timeout=360,
            check=False,
            env=environment,
        )
        assert process.returncode == 0, process.stderr + "\n" + process.stdout
        lines = [
            line
            for line in process.stdout.splitlines()
            if line.startswith("S3_APPLICATION_RESULT=")
        ]
        assert len(lines) == 1, process.stdout
        payload = json.loads(lines[0].removeprefix("S3_APPLICATION_RESULT="))
        assert payload["phase"] == phase
        return payload

    prepared = run_phase("prepare")
    accepted = run_phase("accept", prepared["restart"])
    assert prepared["pid"] != accepted["pid"]
    return {"prepared": prepared, "accepted": accepted}


def _run_legacy_runtime_adoption(existing_freecad_python: str) -> dict[str, object]:
    source = Path(__file__).resolve().parent.parent / "src"
    prefix = Path(existing_freecad_python).parent.parent.resolve()
    environment = _freecad_child_environment(existing_freecad_python, source=source)
    environment.pop("VIBECAD_HOME", None)
    code = _RUNTIME_ADOPTION_CHILD.replace("__SOURCE__", repr(str(source))).replace(
        "__EXPECTED_PREFIX__", repr(str(prefix))
    )
    process = subprocess.run(
        [existing_freecad_python, "-c", code],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
        env=environment,
    )
    assert process.returncode == 0, process.stderr + "\n" + process.stdout
    lines = [
        line
        for line in process.stdout.splitlines()
        if line.startswith("S3_RUNTIME_ADOPTION_RESULT=")
    ]
    assert len(lines) == 1, process.stdout
    return json.loads(lines[0].removeprefix("S3_RUNTIME_ADOPTION_RESULT="))


def _run_agent_first_acceptance(
    current_managed_freecad_python: str,
    tmp_path: Path,
) -> dict[str, dict[str, object]]:
    source = Path(__file__).resolve().parent.parent / "src"
    work_root = tmp_path / "agent-first-acceptance"
    work_root.mkdir(mode=0o700)
    work_root.chmod(0o700)
    environment = _freecad_child_environment(
        current_managed_freecad_python,
        source=source,
    )

    def run_phase(
        phase: str,
        state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        code = (
            _AGENT_FIRST_CHILD.replace("__SOURCE__", repr(str(source)))
            .replace("__WORK_ROOT__", repr(str(work_root)))
            .replace("__PHASE__", repr(phase))
            .replace("__STATE__", repr(json.dumps(state or {}, sort_keys=True)))
        )
        process = subprocess.run(
            [current_managed_freecad_python, "-c", code],
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
            env=environment,
        )
        assert process.returncode == 0, process.stderr + "\n" + process.stdout
        lines = [
            line
            for line in process.stdout.splitlines()
            if line.startswith("S3_AGENT_FIRST_RESULT=")
        ]
        assert len(lines) == 1, process.stdout
        result = json.loads(lines[0].removeprefix("S3_AGENT_FIRST_RESULT="))
        assert result["phase"] == phase
        return result

    prepared = run_phase("prepare")
    decided = run_phase("decide", prepared["restart"])
    assert prepared["pid"] != decided["pid"]
    return {"prepared": prepared, "decided": decided}


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
    current_managed_freecad_python: str,
    tmp_path: Path,
) -> None:
    payload = _run_case(current_managed_freecad_python, tmp_path, "success")
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
    assert values[5]["shape"]["volume_mm3"] == pytest.approx(7200.0 + 20.0 * math.pi)
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
    assert payload["step_values"][1]["after"]["volume_mm3"] == pytest.approx(20.0 * math.pi)
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
    assert payload["step_values"][1]["after"]["volume_mm3"] == pytest.approx(20.0 * math.pi)
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
    assert {item["name"]: item["value"] for item in before["parameters"]} == (expected_parameters)
    assert {item["name"]: item["value"] for item in after["parameters"]} == (expected_parameters)
    assert before["placement"][:3] == pytest.approx([0.0, 0.0, 0.0])
    assert after["placement"][:3] == pytest.approx([5.0, 5.0, 0.0])
    assert after["placement"][3:] == pytest.approx([0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)])
    assert before["center_of_mass_mm"] == pytest.approx([0.0, 40.0 / (3.0 * math.pi), 3.0])
    assert before["center_of_mass_mm"] != pytest.approx([0.0, 5.0, 3.0])
    assert after["center_of_mass_mm"] == pytest.approx([5.0 - 40.0 / (3.0 * math.pi), 5.0, 3.0])
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


@pytest.mark.slow
def test_real_pre_epoch_external_legacy_fails_closed_without_mutation(
    existing_freecad_python: str,
) -> None:
    payload = _run_legacy_runtime_adoption(existing_freecad_python)
    assert payload["legacy_prefix"] == payload["expected_prefix"]
    assert payload["active_prefix"] == payload["expected_prefix"]
    assert payload["receipt_path"].endswith("/runtime/external-runtime.json")
    assert payload["receipt_canonical"] is True
    assert payload["receipt_validated"] is False
    assert payload["receipt_state"] == "incompatible"
    assert payload["runtime_ready"] is False
    assert payload["install_failed_closed"] is True
    assert payload["probe_ready"] is False
    assert payload["external_runtime_unchanged"] is True
    assert payload["external_receipt_unchanged"] is True
    assert payload["current_prefix_unchanged"] is True
    assert payload["old_receipt_missing_identity"] is True
    assert payload["prefix_identity_still_matches"] is True
    assert payload["blocked_commands"] == []
    expected_python = str(Path(existing_freecad_python).parent.parent / "bin" / "python")
    assert payload["receipt_before"] is True
    assert payload["verify_calls"] == [expected_python]


@pytest.mark.slow
def test_real_agent_application_bootstrap_isolation_checkout_and_restart_accept(
    existing_freecad_python: str,
    tmp_path: Path,
) -> None:
    result = _run_application_restart_case(existing_freecad_python, tmp_path)
    prepared = result["prepared"]
    accepted = result["accepted"]

    for phase in (prepared, accepted):
        runtime = phase["runtime"]
        assert runtime["legacy_prefix"] == runtime["expected_prefix"]
        assert runtime["active_prefix"] == runtime["expected_prefix"]
        assert runtime["interpreter_under_prefix"] is True
        assert runtime["external_receipt"] is None
        assert runtime["receipt_state"] == "incompatible"
        assert runtime["runtime_ready"] is False
        assert phase["installer_calls"] == []
        assert phase["legacy_runtime_unchanged"] is True
        assert phase["application_closed"] is True

    empty = prepared["empty"]
    assert empty["head"]["generation"] == 0
    assert empty["head"]["revision_id"] == empty["revision"]["id"]
    assert empty["revision"]["base_revision"] is None
    assert empty["revision"]["model"] is None

    imported = prepared["imported"]
    assert imported["head"]["generation"] == 0
    assert imported["head"]["revision_id"] == imported["revision"]["id"]
    assert imported["revision"]["base_revision"] is None
    assert imported["revision"]["model"]["format"] == "fcstd"
    assert imported["source_unchanged"] is True
    assert {item["object_type"] for item in imported["entities"]} == {
        "Part::Box",
        "Part::Cylinder",
    }
    assert {item["source"] for item in imported["entities"]} == {"imported"}
    assert len({item["object_id"] for item in imported["entities"]}) == 2
    assert len({item["feature_id"] for item in imported["entities"]}) == 2
    assert sorted(item["volume"] for item in imported["entities"]) == pytest.approx(
        [20.0 * math.pi, 6000.0]
    )

    assert prepared["isolation"] == {"isolated": True, "runtime_count": 2}
    assert prepared["import_task"]["status"] == "succeeded"
    assert prepared["import_task"]["committed_revision"] is not None
    assert prepared["checkout"] == {
        "dirty": True,
        "source_unchanged": True,
        "head_unchanged": True,
        "closed_state": "closed",
        "closed_path": None,
        "wire_has_path": False,
    }

    restart = prepared["restart"]
    assert accepted["before_generation"] == restart["generation"]
    assert accepted["accepted"]["status"] == "succeeded"
    assert accepted["accepted"]["committed_revision"] == restart["draft_revision"]
    assert accepted["final_head"]["project_id"] == restart["project_id"]
    assert accepted["final_head"]["generation"] == 1
    assert accepted["final_head"]["revision_id"] == restart["draft_revision"]
    assert [item["object_type"] for item in accepted["entities"]] == ["Part::Box"]
    assert accepted["entities"][0]["volume"] == pytest.approx(6000.0)


@pytest.mark.slow
def test_real_agent_first_public_matrix_and_cross_process_review(
    current_managed_freecad_python: str,
    tmp_path: Path,
) -> None:
    """S3-7 acceptance: public Agent requests, artifacts, rollback, and restart review."""

    result = _run_agent_first_acceptance(current_managed_freecad_python, tmp_path)
    prepared = result["prepared"]
    decided = result["decided"]

    projects = prepared["projects"]
    assert projects["empty"]["kind"] == "empty"
    assert projects["empty"]["generation_zero"]["revision"]["model"] is None
    assert projects["imported"]["kind"] == "import_fcstd"
    assert projects["imported"]["generation_zero"]["revision"]["model"]["format"] == "fcstd"
    assert projects["imported_current"]["project_id"] == projects["imported"]["project_id"]
    assert projects["import_source_unchanged"] is True
    supported_import = projects["supported_import"]
    assert supported_import["task_status"] == "succeeded"
    assert len(supported_import["entities"]) == 2
    assert {item["object_type"] for item in supported_import["entities"]} == {
        "Part::Box",
        "Part::Cylinder",
    }
    assert {item["provenance"]["source"] for item in supported_import["entities"]} == {"imported"}

    rejected_imports = projects["rejected_imports"]
    for kind in ("unsupported", "mixed"):
        assert rejected_imports[kind] == {
            "schema_version": 1,
            "ok": False,
            "result": None,
            "error": {
                "schema_version": 1,
                "code": "invalid_input",
                "path": "",
                "message": "The project request is invalid.",
            },
        }
    assert rejected_imports["sources_unchanged"] is True
    assert rejected_imports["committed_storage_unchanged"] is True
    assert rejected_imports["real_revalidation"] == {
        "codes": {"unsupported": "invalid_input", "mixed": "invalid_input"},
        "sources_unchanged": True,
    }

    direct = prepared["direct"]
    assert direct["operations"] == [
        "create_box",
        "create_cylinder",
        "modify_parameter",
        "move_part",
        "rotate_part",
        "inspect_model",
    ]
    assert direct["statuses"] == ["succeeded"] * 6
    assert direct["base_generations"] == list(range(6))
    selectors = direct["selector_requests"]
    assert len(selectors) == 3
    assert [item["revision_id"] for item in selectors] == direct["base_revisions"][2:5]
    assert all(
        set(item)
        == {
            "schema_version",
            "project_id",
            "revision_id",
            "entity_kind",
            "object_id",
            "feature_id",
            "object_type",
            "semantic_role",
            "provenance",
            "expected_cardinality",
        }
        and item["entity_kind"] == "object"
        and item["feature_id"] is None
        and item["expected_cardinality"] == 1
        for item in selectors
    )

    model = prepared["model_program"]
    assert model["status"] == "succeeded"
    assert model["operations"] == [
        "create_box",
        "create_cylinder",
        "modify_parameter",
        "move_part",
        "rotate_part",
        "inspect_model",
    ]
    for key in ("volume_mm3", "area_mm2", "bbox_mm", "center_of_mass_mm"):
        assert model["shape"][key] == pytest.approx(direct["shape"][key])
    assert model["shape"]["valid_shape"] is direct["shape"]["valid_shape"] is True
    assert model["shape"]["solid_count"] == direct["shape"]["solid_count"] == 2

    for source_kind in ("committed", "draft"):
        exported = prepared[f"{source_kind}_export"]
        delivery = prepared[f"{source_kind}_delivery"]
        assert exported["source_kind"] == source_kind
        assert exported["authoritative"] is False
        assert [item["format"] for item in exported["artifacts"]] == ["fcstd", "step"]
        assert [item["format"] for item in delivery["resources"]] == ["fcstd", "step"]
        for reference, observed in zip(exported["artifacts"], delivery["resources"], strict=True):
            assert observed["uri"] == reference["resource_uri"]
            assert observed["size"] == reference["size_bytes"]
            assert observed["sha256"] == reference["sha256"]
            assert observed["base64_roundtrip"] is True
        reloaded = delivery["geometries"]
        assert reloaded["fcstd"]["valid"] is reloaded["step"]["valid"] is True
        assert reloaded["fcstd"]["volume"] == pytest.approx(reloaded["step"]["volume"])

    failure = prepared["failure"]
    assert failure["status"] == "failed"
    assert failure["committed_revision"] is None
    assert failure["step_oks"] == [False]
    assert failure["head_after"] == failure["head_before"]

    restart = prepared["restart"]
    assert decided["before"] == restart["base_head"]
    accepted = decided["accepted"]
    assert accepted["task_run"]["status"] == "succeeded"
    assert accepted["task_run"]["committed_revision"] == restart["accept"]["revision_id"]
    assert decided["head_after_accept"]["revision_id"] == restart["accept"]["revision_id"]
    rejected = decided["rejected"]
    assert rejected["task_run"]["status"] == "rejected"
    assert rejected["task_run"]["committed_revision"] is None
    assert decided["head_after_reject"] == decided["head_after_accept"]
