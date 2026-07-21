"""Transactional TaskService orchestration tests."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import vibecad.workflow.service as service_module
from vibecad.execution.candidate import (
    CandidateCommitStatus,
    CandidateCoordinator,
    CandidateError,
    CandidateErrorCode,
    CandidateReconcileResult,
    CandidateReconcileStatus,
    CandidateRollbackResult,
    CandidateRollbackStatus,
    SessionBinding,
)
from vibecad.execution.executor import CandidateEvidence, InProcessCadExecutor
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.execution.revisions import (
    CommitJournal,
    CommitJournalState,
    LocalRevisionStore,
    ProjectHead,
    ReconciliationResult,
    ReconciliationStatus,
    RevisionArtifactRef,
    RevisionRef,
)
from vibecad.validation import (
    ArtifactObservation,
    ObservationSnapshot,
    ShapeObservation,
)
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ErrorCategory,
    ModelCommand,
    ModelProgram,
    StepError,
    StepResult,
    ValueSource,
)
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ProjectWriteLease,
    ResourceLeaseManager,
)
from vibecad.workflow.program import validate_model_program
from vibecad.workflow.service import (
    TaskService,
    TaskServiceError,
    TaskServiceErrorCode,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskArtifactRef,
    TaskEvent,
    TaskStatus,
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
BASE_PARENT_REVISION = "revision_00000000000000000000000000000000"
CANDIDATE_REVISION = "revision_11111111111111111111111111111111"
DESCENDANT_REVISION = "revision_22222222222222222222222222222222"
MODEL_ID = "artifact_0123456789abcdef0123456789abcdef"
STEP_ID = "artifact_11111111111111111111111111111111"
BASE_MANIFEST = "a" * 64
CANDIDATE_MANIFEST = "b" * 64
MODEL_HASH = "c" * 64
STEP_HASH = "d" * 64


def _error(
    code: str = "injected_failure",
    *,
    needs_input: bool = False,
    category: ErrorCategory = ErrorCategory.RUNTIME,
) -> StepError:
    return StepError(
        category=category,
        code=code,
        message="The task operation failed.",
        retryable=False,
        needs_input=needs_input,
        related_objects=(),
        diagnostic_artifacts=(),
    )


def _program(
    *,
    expected_volume: float = 7200.0,
    acceptance: AcceptanceSpec | None = None,
    operations: int = 2,
) -> ModelProgram:
    commands = []
    for index in range(operations):
        commands.append(
            ModelCommand(
                id=f"inspect-{index}",
                op="inspect_model",
                target={},
                args={},
                depends_on=() if index == 0 else (f"inspect-{index - 1}",),
                preserve=(),
                source=ValueSource.MODEL,
            )
        )
    if acceptance is None:
        acceptance = AcceptanceSpec(
            id="acceptance-service",
            criteria=(
                AcceptanceCriterion(
                    id="volume",
                    kind=AcceptanceKind.GEOMETRY,
                    check="volume",
                    target="body",
                    expected=expected_volume,
                    tolerance=0.0,
                    parameters={"unit": "mm^3"},
                    required=True,
                ),
            ),
        )
    return ModelProgram(
        task_id=TASK_ID,
        base_revision=BASE_REVISION,
        operations=tuple(commands),
        acceptance=acceptance,
    )


def _base_head() -> ProjectHead:
    return ProjectHead(
        project_id=PROJECT_ID,
        generation=1,
        revision_id=BASE_REVISION,
        manifest_sha256=BASE_MANIFEST,
    )


def _revision() -> RevisionRef:
    return RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256=CANDIDATE_MANIFEST,
        model=RevisionArtifactRef(
            id=MODEL_ID,
            name="model.FCStd",
            format="fcstd",
            sha256=MODEL_HASH,
            size_bytes=100,
        ),
        artifacts=(
            RevisionArtifactRef(
                id=STEP_ID,
                name="model.step",
                format="step",
                sha256=STEP_HASH,
                size_bytes=200,
            ),
        ),
    )


def _base_revision_ref() -> RevisionRef:
    candidate = _revision()
    return RevisionRef(
        id=BASE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_PARENT_REVISION,
        manifest_sha256=BASE_MANIFEST,
        model=candidate.model,
        artifacts=candidate.artifacts,
    )


def _candidate_head() -> ProjectHead:
    return ProjectHead(
        project_id=PROJECT_ID,
        generation=2,
        revision_id=CANDIDATE_REVISION,
        manifest_sha256=CANDIDATE_MANIFEST,
    )


def _descendant_revision() -> RevisionRef:
    candidate = _revision()
    return RevisionRef(
        id=DESCENDANT_REVISION,
        project_id=PROJECT_ID,
        base_revision=CANDIDATE_REVISION,
        manifest_sha256="e" * 64,
        model=candidate.model,
        artifacts=candidate.artifacts,
    )


def _descendant_head() -> ProjectHead:
    return ProjectHead(
        project_id=PROJECT_ID,
        generation=3,
        revision_id=DESCENDANT_REVISION,
        manifest_sha256="e" * 64,
    )


def _evidence() -> CandidateEvidence:
    snapshot = ObservationSnapshot(
        candidate_revision=CANDIDATE_REVISION,
        shapes=(
            ShapeObservation(
                target="body",
                volume_mm3=7200.0,
                area_mm2=2400.0,
                bbox_mm=(12.0, 20.0, 30.0),
                center_of_mass_mm=(6.0, 10.0, 15.0),
                valid_shape=True,
                solid_count=1,
            ),
        ),
        artifacts=(
            ArtifactObservation(target="export", exists=True, non_empty=True, format="step"),
            ArtifactObservation(target="model", exists=True, non_empty=True, format="fcstd"),
        ),
    )
    return CandidateEvidence(
        snapshot=snapshot,
        artifacts=(
            TaskArtifactRef(
                id=MODEL_ID,
                name="model.FCStd",
                format="fcstd",
                sha256=MODEL_HASH,
                size_bytes=100,
                candidate_revision=CANDIDATE_REVISION,
            ),
            TaskArtifactRef(
                id=STEP_ID,
                name="model.step",
                format="step",
                sha256=STEP_HASH,
                size_bytes=200,
                candidate_revision=CANDIDATE_REVISION,
            ),
        ),
    )


class _MemoryTaskStore(TaskRunStore):
    def __init__(self, log: list[str], lease_manager: ResourceLeaseManager) -> None:
        self.records: dict[str, StoredTaskRun] = {}
        self.log = log
        self._lease_manager = lease_manager
        self.load_error: Exception | None = None
        self.create_uncertain = False
        self.persist_create_uncertain = True
        self.fail_status: TaskStatus | None = None
        self.fail_code: TaskStoreErrorCode | None = None
        self.fail_occurrence = 1
        self.status_cas_counts: dict[TaskStatus, int] = {}
        self.uncertain_status: TaskStatus | None = None
        self.uncertain_occurrence = 1
        self.persist_uncertain = True

    def load(self, task_id: str) -> StoredTaskRun:
        self.log.append("task.load")
        if self.load_error is not None:
            raise self.load_error
        try:
            return self.records[task_id]
        except KeyError:
            raise TaskStoreError(TaskStoreErrorCode.NOT_FOUND) from None

    def create(self, task_run: object) -> StoredTaskRun:
        self.log.append("task.create")
        if task_run.id in self.records:  # type: ignore[union-attr]
            raise TaskStoreError(TaskStoreErrorCode.ALREADY_EXISTS)
        stored = StoredTaskRun(generation=0, task_run=task_run)  # type: ignore[arg-type]
        if not self.create_uncertain or self.persist_create_uncertain:
            self.records[task_run.id] = stored  # type: ignore[union-attr]
        if self.create_uncertain:
            raise TaskStoreError(
                TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                committed_generation=0,
            )
        return stored

    def compare_and_set(
        self,
        task_id: str,
        expected_generation: int,
        task_run: object,
    ) -> StoredTaskRun:
        self.log.append(f"task.cas:{task_run.status.value}")  # type: ignore[union-attr]
        current = self.records[task_id]
        if current.generation != expected_generation:
            raise TaskStoreError(TaskStoreErrorCode.CONFLICT)
        status = task_run.status  # type: ignore[union-attr]
        self.status_cas_counts[status] = self.status_cas_counts.get(status, 0) + 1
        if status is self.fail_status and self.status_cas_counts[status] == self.fail_occurrence:
            raise TaskStoreError(self.fail_code or TaskStoreErrorCode.CONFLICT)
        stored = StoredTaskRun(
            generation=expected_generation + 1,
            task_run=task_run,  # type: ignore[arg-type]
        )
        uncertain_now = (
            status is self.uncertain_status
            and self.status_cas_counts[status] == self.uncertain_occurrence
        )
        if not uncertain_now or self.persist_uncertain:
            self.records[task_id] = stored
        if uncertain_now:
            raise TaskStoreError(
                TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                committed_generation=stored.generation,
            )
        return stored


class _LeaseIssuer:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.error = False

    def release(self, lease: ProjectWriteLease, *, owner_token: str) -> None:
        assert owner_token == lease.owner_token
        self.log.append("lease.release")
        if self.error:
            raise LeaseError(LeaseErrorCode.IO_ERROR)
        object.__setattr__(lease, "released", True)


class _LeaseManager(ResourceLeaseManager):
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.error: LeaseError | None = None
        self.issuer = _LeaseIssuer(log)

    def acquire_project_write(self, project_id: str) -> ProjectWriteLease:
        self.log.append("lease.acquire")
        if self.error is not None:
            raise self.error
        lease = object.__new__(ProjectWriteLease)
        object.__setattr__(lease, "_issuer", self.issuer)
        object.__setattr__(lease, "owner_token", "owner-token")
        object.__setattr__(lease, "released", False)
        object.__setattr__(lease, "project_id", project_id)
        return lease


class _RevisionStore(LocalRevisionStore):
    def __init__(self, log: list[str], lease_manager: ResourceLeaseManager) -> None:
        self.log = log
        self._lease_manager = lease_manager
        self.head = _base_head()
        self.revisions = {
            BASE_REVISION: _base_revision_ref(),
            CANDIDATE_REVISION: _revision(),
        }

    def load_head(self, project_id: str) -> ProjectHead:
        self.log.append("revision.head")
        assert project_id == PROJECT_ID
        return self.head

    def load_revision(self, project_id: str, revision_id: str) -> RevisionRef:
        self.log.append(f"revision.load:{revision_id}")
        assert project_id == PROJECT_ID
        return self.revisions[revision_id]


class _Coordinator(CandidateCoordinator):
    def __init__(
        self,
        log: list[str],
        revisions: _RevisionStore,
        executor: InProcessCadExecutor,
    ) -> None:
        self.log = log
        self.revisions = revisions
        self._store = revisions
        self._snapshot_port = executor
        self.begin_error: CandidateError | None = None
        self.failure_stage: str | None = None
        self.terminal_failure_stage: str | None = None
        self.terminal_error: CandidateError | None = None
        self.terminalized = False
        self.rollback_status = CandidateRollbackStatus.NOT_COMMITTED
        self.commit_status = CandidateCommitStatus.COMMITTED
        self.reconcile_status = CandidateReconcileStatus.CLEAN
        self.reconcile_live_revision: str | None = None
        self.reconcile_journal_candidate: str | None = None
        self.reconcile_journal_manifest: str | None = None
        self.commit_calls = 0
        self.rollback_calls = 0
        self.rollback_invocations = 0
        self.publish_review_calls = 0
        self.reopen_review_calls = 0
        self.prepare_review_calls = 0
        self.discard_review_calls = 0
        self.receipt = None
        self.review_live_binding = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=BASE_REVISION,
            session=object(),
        )
        self.publish_review_override: object | None = None

    def _maybe_fail(self, stage: str) -> None:
        if self.terminal_failure_stage == stage:
            self.terminalized = True
            self.rollback_calls += 1
            raise self.terminal_error or CandidateError(CandidateErrorCode.CAD_FAILURE)
        if self.failure_stage == stage:
            raise RuntimeError(f"secret-{stage}")

    def begin(self, *, project_id: str, expected_head: ProjectHead, lease: object) -> object:
        del lease
        self.log.append("candidate.begin")
        if self.begin_error is not None:
            raise self.begin_error
        self._maybe_fail("begin")
        return SimpleNamespace(
            project_id=project_id,
            base_head=expected_head,
            binding=SessionBinding(
                project_id=project_id,
                revision_id=CANDIDATE_REVISION,
                session=object(),
            ),
            stage="active",
        )

    def checkpoint(self, *, candidate: object, lease: object) -> object:
        del lease
        self.log.append("candidate.checkpoint")
        self._maybe_fail("checkpoint")
        return SimpleNamespace(**{**candidate.__dict__, "stage": "checkpointed"})

    def seal(self, *, candidate: object, lease: object) -> object:
        del lease
        self.log.append("candidate.seal")
        self._maybe_fail("seal")
        return SimpleNamespace(
            project_id=candidate.project_id,
            base_head=candidate.base_head,
            binding=candidate.binding,
            revision=_revision(),
            stage="sealed",
        )

    def rollback(self, *, candidate: object, lease: object) -> object:
        del candidate, lease
        self.log.append("candidate.rollback")
        self.rollback_invocations += 1
        if self.terminalized:
            raise CandidateError(CandidateErrorCode.ALREADY_TERMINAL)
        self.rollback_calls += 1
        return SimpleNamespace(
            status=self.rollback_status,
            head=self.revisions.head,
            head_committed=False,
            cleanup_required=self.rollback_status is CandidateRollbackStatus.CLEANUP_REQUIRED,
            recovery_required=self.rollback_status is CandidateRollbackStatus.RECOVERY_REQUIRED,
        )

    def publish_review(
        self,
        *,
        candidate: object,
        receipt: object,
        compiled: object,
        snapshot: object,
        lease: object,
    ) -> object:
        del compiled, snapshot, lease
        self.log.append("candidate.publish_review")
        self.publish_review_calls += 1
        self.receipt = receipt
        self.rollback_calls += 1
        if self.publish_review_override is not None:
            return self.publish_review_override
        if self.rollback_status is CandidateRollbackStatus.NOT_COMMITTED:
            journal = CommitJournal(
                id="transaction_0123456789abcdef0123456789abcdef",
                project_id=PROJECT_ID,
                expected_head=self.revisions.head,
                candidate_revision=candidate.revision.id,
                manifest_sha256=candidate.revision.manifest_sha256,
                state=CommitJournalState.NOT_COMMITTED,
            )
            reconciliation = ReconciliationResult(
                project_id=PROJECT_ID,
                status=ReconciliationStatus.NOT_COMMITTED,
                head=self.revisions.head,
                journal=journal,
            )
            return CandidateRollbackResult(
                status=CandidateRollbackStatus.NOT_COMMITTED,
                head=self.revisions.head,
                live_binding=self.review_live_binding,
                reconciliation=reconciliation,
                head_committed=False,
                slot_promoted=False,
                cleanup_required=False,
                recovery_required=False,
                cleanup_binding=None,
            )
        return SimpleNamespace(
            status=self.rollback_status,
            head=self.revisions.head,
            head_committed=False,
            cleanup_required=self.rollback_status is CandidateRollbackStatus.CLEANUP_REQUIRED,
            recovery_required=self.rollback_status is CandidateRollbackStatus.RECOVERY_REQUIRED,
        )

    def reopen_review(
        self,
        *,
        project_id: str,
        base_head: ProjectHead,
        revision: RevisionRef,
        lease: object,
    ) -> object:
        del lease
        self.log.append("candidate.reopen_review")
        self.reopen_review_calls += 1
        self._maybe_fail("reopen_review")
        return SimpleNamespace(
            project_id=project_id,
            base_head=base_head,
            revision=revision,
            binding=SessionBinding(
                project_id=project_id,
                revision_id=revision.id,
                session=object(),
            ),
            stage="review_open",
        )

    def prepare_review(self, *, candidate: object, lease: object) -> object:
        del lease
        self.log.append("candidate.prepare_review")
        self.prepare_review_calls += 1
        self._maybe_fail("prepare_review")
        return SimpleNamespace(**{**candidate.__dict__, "stage": "sealed"})

    def discard_review(self, *, candidate: object, lease: object) -> None:
        del candidate, lease
        self.log.append("candidate.discard_review")
        self.discard_review_calls += 1
        self._maybe_fail("discard_review")

    def commit(
        self,
        *,
        candidate: object,
        receipt: object,
        compiled: object,
        snapshot: object,
        lease: object,
    ) -> object:
        del compiled, snapshot, lease
        self.log.append("candidate.commit")
        self.commit_calls += 1
        self.receipt = receipt
        self._maybe_fail("commit")
        self.revisions.head = _candidate_head()
        return SimpleNamespace(
            status=self.commit_status,
            head=_candidate_head(),
            revision=candidate.revision,
            head_committed=True,
            slot_promoted=True,
            cleanup_required=(
                self.commit_status is CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED
            ),
            recovery_required=(
                self.commit_status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
            ),
        )

    def reconcile(self, *, project_id: str, lease: object) -> object:
        del project_id, lease
        self.log.append("candidate.reconcile")
        if self.reconcile_status in {
            CandidateReconcileStatus.CLEAN,
            CandidateReconcileStatus.NOT_COMMITTED,
            CandidateReconcileStatus.COMMITTED,
        }:
            head = self.revisions.head
            journal = None
            reconciliation_status = ReconciliationStatus.CLEAN
            head_committed = True
            slot_promoted = True
            if self.reconcile_status is CandidateReconcileStatus.NOT_COMMITTED:
                reconciliation_status = ReconciliationStatus.NOT_COMMITTED
                candidate_revision = self.reconcile_journal_candidate or CANDIDATE_REVISION
                manifest_sha256 = self.reconcile_journal_manifest or CANDIDATE_MANIFEST
                if head.revision_id == candidate_revision:
                    candidate_revision = DESCENDANT_REVISION
                    manifest_sha256 = "e" * 64
                journal = CommitJournal(
                    id="transaction_abcdefabcdefabcdefabcdefabcdefab",
                    project_id=PROJECT_ID,
                    expected_head=head,
                    candidate_revision=candidate_revision,
                    manifest_sha256=manifest_sha256,
                    state=CommitJournalState.NOT_COMMITTED,
                )
                head_committed = False
                slot_promoted = False
            elif self.reconcile_status is CandidateReconcileStatus.COMMITTED:
                reconciliation_status = ReconciliationStatus.COMMITTED
                expected_head = _base_head()
                if head == _base_head():
                    expected_head = ProjectHead(
                        project_id=PROJECT_ID,
                        generation=0,
                        revision_id=BASE_PARENT_REVISION,
                        manifest_sha256="0" * 64,
                    )
                journal = CommitJournal(
                    id="transaction_fedcbafedcbafedcbafedcbafedcbafe",
                    project_id=PROJECT_ID,
                    expected_head=expected_head,
                    candidate_revision=head.revision_id,
                    manifest_sha256=head.manifest_sha256,
                    state=CommitJournalState.COMMITTED,
                )
            reconciliation = ReconciliationResult(
                project_id=PROJECT_ID,
                status=reconciliation_status,
                head=head,
                journal=journal,
            )
            return CandidateReconcileResult(
                status=self.reconcile_status,
                head=head,
                live_binding=SessionBinding(
                    project_id=PROJECT_ID,
                    revision_id=self.reconcile_live_revision or head.revision_id,
                    session=object(),
                ),
                reconciliation=reconciliation,
                head_committed=head_committed,
                slot_promoted=slot_promoted,
                cleanup_required=False,
                recovery_required=False,
                cleanup_binding=None,
            )
        return SimpleNamespace(
            status=self.reconcile_status,
            head=self.revisions.head,
            head_committed=self.reconcile_status
            in {CandidateReconcileStatus.CLEAN, CandidateReconcileStatus.COMMITTED},
            slot_promoted=(
                True
                if self.reconcile_status
                in {CandidateReconcileStatus.CLEAN, CandidateReconcileStatus.COMMITTED}
                else False
            ),
            cleanup_required=self.reconcile_status is CandidateReconcileStatus.CLEANUP_REQUIRED,
            recovery_required=self.reconcile_status is CandidateReconcileStatus.RECOVERY_REQUIRED,
            live_binding=SessionBinding(
                project_id=PROJECT_ID,
                revision_id=self.reconcile_live_revision or self.revisions.head.revision_id,
                session=object(),
            ),
        )


class _Executor(InProcessCadExecutor):
    def __init__(self, log: list[str], revisions: LocalRevisionStore) -> None:
        self.log = log
        self._store = revisions
        self.outcomes: tuple[NormalizedToolOutcome, ...] | None = None
        self.failure_stage: str | None = None

    def _maybe_fail(self, stage: str) -> None:
        if self.failure_stage == stage:
            raise RuntimeError(f"secret-{stage}")

    def validate_program(self, program: ModelProgram):
        self.log.append("executor.validate")
        self._maybe_fail("validate")
        return validate_model_program(program)

    def execute_program(self, *, program: object, candidate: object):
        self.log.append("executor.execute")
        self._maybe_fail("execute")
        if self.outcomes is not None:
            return self.outcomes
        return tuple(
            NormalizedToolOutcome(
                result=StepResult(
                    ok=True,
                    value={"index": index, "ratio": 1.5},
                    elapsed_ms=0.25,
                    operation_id=command.id,
                    revision=candidate.binding.revision_id,
                )
            )
            for index, command in enumerate(program.commands)
        )

    def export_step(self, *, candidate: object, lease: object) -> None:
        del candidate, lease
        self.log.append("executor.export")
        self._maybe_fail("export")

    def collect_evidence(self, *, candidate: object) -> CandidateEvidence:
        del candidate
        self.log.append("executor.collect")
        self._maybe_fail("collect")
        return _evidence()


class _Rig:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.leases = _LeaseManager(self.log)
        self.tasks = _MemoryTaskStore(self.log, self.leases)
        self.revisions = _RevisionStore(self.log, self.leases)
        self.executor = _Executor(self.log, self.revisions)
        self.coordinator = _Coordinator(self.log, self.revisions, self.executor)
        self.service = TaskService(
            task_store=self.tasks,
            revision_store=self.revisions,
            lease_manager=self.leases,
            coordinator=self.coordinator,
            executor=self.executor,
        )

    def create(
        self,
        review_policy: ReviewPolicy = ReviewPolicy.AUTO_COMMIT,
    ) -> StoredTaskRun:
        return self.service.create_task(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=review_policy,
        )

    def run(
        self,
        program: ModelProgram | None = None,
        *,
        review_policy: ReviewPolicy = ReviewPolicy.AUTO_COMMIT,
    ) -> StoredTaskRun:
        created = self.create(review_policy)
        return self.service.submit_model_program(
            task_id=TASK_ID,
            expected_generation=created.generation,
            program=_program() if program is None else program,
        )


def _real_task_store_rig(tmp_path: Path) -> SimpleNamespace:
    log: list[str] = []
    lock_root = tmp_path / "locks"
    task_root = tmp_path / "tasks"
    lock_root.mkdir(mode=0o700)
    task_root.mkdir(mode=0o700)
    leases = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    tasks = TaskRunStore(task_root, leases, trust=TaskStoreRootTrust.TRUSTED_LOCAL)
    revisions = _RevisionStore(log, leases)
    executor = _Executor(log, revisions)
    coordinator = _Coordinator(log, revisions, executor)
    service = TaskService(
        task_store=tasks,
        revision_store=revisions,
        lease_manager=leases,
        coordinator=coordinator,
        executor=executor,
    )
    return SimpleNamespace(
        log=log,
        leases=leases,
        tasks=tasks,
        revisions=revisions,
        executor=executor,
        coordinator=coordinator,
        service=service,
    )


def _failed_outcomes() -> tuple[NormalizedToolOutcome, ...]:
    return (
        NormalizedToolOutcome(
            result=StepResult(
                ok=True,
                value={"mutated": True},
                elapsed_ms=0,
                operation_id="inspect-0",
                revision=CANDIDATE_REVISION,
            )
        ),
        NormalizedToolOutcome(
            result=StepResult(
                ok=False,
                value=None,
                elapsed_ms=0,
                operation_id="inspect-1",
                revision=CANDIDATE_REVISION,
                error=_error(),
            )
        ),
    )


def _store_executing(rig: _Rig) -> StoredTaskRun:
    stored = _store_validating(rig)
    task = transition_task(
        stored.task_run,
        TaskEvent.VALIDATE_PROGRAM,
        candidate_revision=CANDIDATE_REVISION,
    )
    return rig.tasks.compare_and_set(TASK_ID, stored.generation, task)


def _store_validating(rig: _Rig) -> StoredTaskRun:
    created = rig.create()
    task = transition_task(created.task_run, TaskEvent.SUBMIT_PROGRAM, program=_program())
    stored = rig.tasks.compare_and_set(TASK_ID, created.generation, task)
    task = transition_task(stored.task_run, TaskEvent.START_VALIDATION)
    return rig.tasks.compare_and_set(TASK_ID, stored.generation, task)


def test_public_contract_and_fixed_errors() -> None:
    assert service_module.__all__ == (
        "TaskServiceErrorCode",
        "TaskServiceError",
        "TaskService",
    )
    assert {item.value for item in TaskServiceErrorCode} == {
        "invalid_input",
        "unsupported_reasoning_owner",
        "invalid_state",
        "not_found",
        "conflict",
        "store_failure",
        "lease_unavailable",
        "recovery_required",
    }
    for code in TaskServiceErrorCode:
        error = TaskServiceError(code)
        assert error.code is code
        assert error.to_mapping()["code"] == code.value
        assert "secret" not in str(error)


def test_wrapped_store_failure_has_no_sensitive_exception_chain() -> None:
    rig = _Rig()
    rig.tasks.load_error = RuntimeError("secret-path\nsecret-detail")
    with pytest.raises(TaskServiceError) as caught:
        rig.service.get_task(task_id=TASK_ID)
    error = caught.value
    assert error.code is TaskServiceErrorCode.STORE_FAILURE
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "secret" not in str(error)
    assert "secret" not in repr(error.to_mapping())


def test_constructor_requires_trusted_concrete_ports() -> None:
    rig = _Rig()
    with pytest.raises(TaskServiceError) as caught:
        TaskService(
            task_store=object(),  # type: ignore[arg-type]
            revision_store=rig.revisions,
            lease_manager=rig.leases,
            coordinator=rig.coordinator,
            executor=rig.executor,
        )
    assert caught.value.code is TaskServiceErrorCode.INVALID_INPUT

    other_revisions = _RevisionStore(rig.log, rig.leases)
    with pytest.raises(TaskServiceError) as caught:
        TaskService(
            task_store=rig.tasks,
            revision_store=other_revisions,
            lease_manager=rig.leases,
            coordinator=rig.coordinator,
            executor=rig.executor,
        )
    assert caught.value.code is TaskServiceErrorCode.INVALID_INPUT


def test_create_task_binds_current_head_and_requests_external_plan() -> None:
    rig = _Rig()
    stored = rig.create()
    assert stored.generation == 0
    assert stored.task_run.status is TaskStatus.NEEDS_PLAN
    assert stored.task_run.base_revision == BASE_REVISION
    assert stored.task_run.reasoning_owner is ReasoningOwner.EXTERNAL_PLAN
    assert rig.log == ["revision.head", "task.create"]


def test_create_durability_uncertain_accepts_only_exact_generation_zero_readback() -> None:
    rig = _Rig()
    rig.tasks.create_uncertain = True
    stored = rig.create()
    assert stored.generation == 0
    assert stored.task_run.status is TaskStatus.NEEDS_PLAN

    mismatch = _Rig()
    mismatch.tasks.create_uncertain = True
    mismatch.tasks.persist_create_uncertain = False
    with pytest.raises(TaskServiceError) as caught:
        mismatch.create()
    assert caught.value.code is TaskServiceErrorCode.STORE_FAILURE


@pytest.mark.parametrize("owner", [ReasoningOwner.MCP_SAMPLING, ReasoningOwner.BYOK])
def test_create_rejects_non_external_reasoning_without_store_or_project(
    owner: ReasoningOwner,
) -> None:
    rig = _Rig()
    with pytest.raises(TaskServiceError) as caught:
        rig.service.create_task(
            task_id=TASK_ID,
            project_id=PROJECT_ID,
            reasoning_owner=owner,
            review_policy=ReviewPolicy.AUTO_COMMIT,
        )
    assert caught.value.code is TaskServiceErrorCode.UNSUPPORTED_REASONING_OWNER
    assert rig.log == []


def test_happy_path_is_transactional_and_persists_all_evidence() -> None:
    rig = _Rig()
    stored = rig.run()
    task = stored.task_run
    assert task.status is TaskStatus.SUCCEEDED
    assert task.candidate_revision == CANDIDATE_REVISION
    assert task.committed_revision == CANDIDATE_REVISION
    assert len(task.steps) == 2
    assert len(task.artifacts) == 2
    assert len(task.verification_reports) == 1
    assert task.verification_reports[0].passed is True
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0
    assert rig.coordinator.receipt is not None
    ordered = [
        "executor.validate",
        "lease.acquire",
        "revision.head",
        "candidate.begin",
        "executor.execute",
        "candidate.checkpoint",
        "executor.export",
        "candidate.seal",
        "executor.collect",
        "candidate.commit",
        "lease.release",
    ]
    positions = [
        (
            rig.log.index(item, rig.log.index("lease.acquire"))
            if item == "revision.head"
            else rig.log.index(item)
        )
        for item in ordered
    ]
    assert positions == sorted(positions)
    assert rig.log.index("task.cas:committing") < rig.log.index("candidate.commit")


def test_real_task_store_round_trips_float_rich_submit_to_terminal(
    tmp_path: Path,
) -> None:
    rig = _real_task_store_rig(tmp_path)
    created = rig.service.create_task(
        task_id=TASK_ID,
        project_id=PROJECT_ID,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.AUTO_COMMIT,
    )
    terminal = rig.service.submit_model_program(
        task_id=TASK_ID,
        expected_generation=created.generation,
        program=_program(),
    )
    assert terminal.task_run.status is TaskStatus.SUCCEEDED
    assert terminal.task_run.program == _program()
    assert terminal.task_run.verification_reports[0].verdicts[0].observed == 7200.0
    assert rig.service.get_task(task_id=TASK_ID) == terminal


def test_invalid_acceptance_is_durable_needs_input_without_project_or_cad() -> None:
    rig = _Rig()
    empty = AcceptanceSpec(id="acceptance-empty", criteria=())
    stored = rig.run(_program(acceptance=empty))
    assert stored.task_run.status is TaskStatus.NEEDS_INPUT
    assert stored.task_run.candidate_revision is None
    assert stored.task_run.last_error is not None
    assert stored.task_run.last_error.category is ErrorCategory.VALIDATION
    assert "lease.acquire" not in rig.log
    assert "candidate.begin" not in rig.log
    assert "executor.execute" not in rig.log


def test_oversized_program_is_rejected_before_program_validation_or_project_access() -> None:
    rig = _Rig()
    template = _program()
    oversized = ModelProgram(
        task_id=TASK_ID,
        base_revision=BASE_REVISION,
        operations=(
            ModelCommand(
                id="oversized",
                op="inspect_model",
                target={},
                args={"payload": "x" * (600 * 1024)},
                preserve=(),
                source=ValueSource.MODEL,
                depends_on=(),
            ),
        ),
        acceptance=template.acceptance,
    )
    created = rig.create()
    with pytest.raises(TaskServiceError) as caught:
        rig.service.submit_model_program(
            task_id=TASK_ID,
            expected_generation=created.generation,
            program=oversized,
        )
    assert caught.value.code is TaskServiceErrorCode.INVALID_INPUT
    assert rig.tasks.records[TASK_ID] == created
    assert "executor.validate" not in rig.log
    assert "lease.acquire" not in rig.log
    assert not any(item.startswith("task.cas:") for item in rig.log)
    assert "candidate.begin" not in rig.log


@pytest.mark.parametrize(
    "payload",
    [
        "x" * 4097,
        "x" * 65537,
        [None] * 5000,
        [None] * 8192,
        (lambda: None)(),
    ],
    ids=(
        "state-string",
        "store-string",
        "state-node-count",
        "store-node-count",
        "record-depth",
    ),
)
def test_store_envelope_rejects_unreadable_program_before_submit(
    payload: object,
) -> None:
    if payload is None:
        payload = "leaf"
        for _ in range(60):
            payload = {"nested": payload}
    rig = _Rig()
    template = _program()
    program = ModelProgram(
        task_id=TASK_ID,
        base_revision=BASE_REVISION,
        operations=(
            ModelCommand(
                id="envelope-limit",
                op="inspect_model",
                target={},
                args={"payload": payload},
                preserve=(),
                source=ValueSource.MODEL,
                depends_on=(),
            ),
        ),
        acceptance=template.acceptance,
    )
    created = rig.create()
    with pytest.raises(TaskServiceError) as caught:
        rig.service.submit_model_program(
            task_id=TASK_ID,
            expected_generation=created.generation,
            program=program,
        )
    assert caught.value.code is TaskServiceErrorCode.INVALID_INPUT
    assert rig.tasks.records[TASK_ID] == created
    assert "executor.validate" not in rig.log
    assert "lease.acquire" not in rig.log
    assert not any(item.startswith("task.cas:") for item in rig.log)


def test_lease_contention_and_head_drift_are_pre_candidate_needs_input() -> None:
    for mode in ("lease", "head"):
        rig = _Rig()
        created = rig.create()
        if mode == "lease":
            rig.leases.error = LeaseError(LeaseErrorCode.CONTENDED)
        else:
            rig.revisions.head = ProjectHead(
                project_id=PROJECT_ID,
                generation=2,
                revision_id="revision_22222222222222222222222222222222",
                manifest_sha256="e" * 64,
            )
        stored = rig.service.submit_model_program(
            task_id=TASK_ID,
            expected_generation=created.generation,
            program=_program(),
        )
        assert stored.task_run.status is TaskStatus.NEEDS_INPUT
        assert stored.task_run.candidate_revision is None
        assert stored.task_run.last_error is not None
        assert stored.task_run.last_error.category is ErrorCategory.CONFLICT
        assert "candidate.begin" not in rig.log
        assert "executor.execute" not in rig.log


def test_execution_failure_rolls_back_after_durable_rolling_back() -> None:
    rig = _Rig()
    rig.executor.outcomes = _failed_outcomes()
    stored = rig.run()
    task = stored.task_run
    assert task.status is TaskStatus.FAILED
    assert len(task.steps) == 2
    assert task.steps[0].result.value == {"mutated": True}
    assert rig.coordinator.rollback_calls == 1
    assert rig.coordinator.commit_calls == 0
    rollback_cas = rig.log.index("task.cas:rolling_back")
    rollback_call = rig.log.index("candidate.rollback")
    assert rollback_cas < rollback_call
    assert "candidate.checkpoint" not in rig.log


def test_candidate_needs_input_error_still_ends_single_attempt_as_failed() -> None:
    rig = _Rig()
    rig.executor.outcomes = (
        NormalizedToolOutcome(
            result=StepResult(
                ok=False,
                value=None,
                elapsed_ms=0,
                operation_id="inspect-0",
                revision=CANDIDATE_REVISION,
                error=_error("label_expired", needs_input=True),
            )
        ),
    )
    stored = rig.run(_program(operations=1))
    assert stored.task_run.status is TaskStatus.FAILED
    assert stored.task_run.candidate_revision == CANDIDATE_REVISION
    assert rig.coordinator.rollback_calls == 1


def test_required_verification_failure_records_report_and_never_commits() -> None:
    rig = _Rig()
    stored = rig.run(_program(expected_volume=999.0))
    task = stored.task_run
    assert task.status is TaskStatus.FAILED
    assert len(task.verification_reports) == 1
    assert task.verification_reports[0].passed is False
    assert rig.coordinator.commit_calls == 0
    assert rig.coordinator.rollback_calls == 1
    assert rig.log.index("task.cas:rolling_back") < rig.log.index("candidate.rollback")


@pytest.mark.parametrize("stage", ["checkpoint", "export", "seal", "collect"])
def test_post_publication_failures_rollback_without_reflecting_secret(stage: str) -> None:
    rig = _Rig()
    if stage in {"checkpoint", "seal"}:
        rig.coordinator.failure_stage = stage
    else:
        rig.executor.failure_stage = stage
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.FAILED
    assert rig.coordinator.rollback_calls == 1
    assert rig.coordinator.commit_calls == 0
    assert "secret" not in stored.task_run.last_error.message


@pytest.mark.parametrize("stage", ["checkpoint", "seal"])
def test_coordinator_internal_abort_is_not_repeated_as_a_second_rollback(stage: str) -> None:
    rig = _Rig()
    rig.coordinator.terminal_failure_stage = stage
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.FAILED
    assert rig.coordinator.rollback_calls == 1
    assert rig.coordinator.rollback_invocations <= 1
    assert rig.coordinator.commit_calls == 0


@pytest.mark.parametrize(
    ("terminal_error", "task_status"),
    [
        (
            CandidateError(CandidateErrorCode.CLEANUP_REQUIRED, cleanup_required=True),
            TaskStatus.CLEANUP_REQUIRED,
        ),
        (
            CandidateError(CandidateErrorCode.RECOVERY_REQUIRED, recovery_required=True),
            TaskStatus.RECOVERY_REQUIRED,
        ),
    ],
)
def test_coordinator_internal_abort_preserves_attention(
    terminal_error: CandidateError,
    task_status: TaskStatus,
) -> None:
    rig = _Rig()
    rig.coordinator.terminal_failure_stage = "checkpoint"
    rig.coordinator.terminal_error = terminal_error
    stored = rig.run()
    assert stored.task_run.status is task_status
    assert rig.coordinator.rollback_calls == 1
    assert rig.coordinator.rollback_invocations == 1
    assert rig.coordinator.commit_calls == 0


def test_invalid_verifier_output_never_reaches_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _Rig()
    monkeypatch.setattr(
        service_module,
        "verify_acceptance",
        lambda *_args, **_kwargs: SimpleNamespace(report=object(), receipt=object()),
    )
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.FAILED
    assert rig.coordinator.rollback_calls == 1
    assert rig.coordinator.commit_calls == 0


def test_commit_exception_after_receipt_authority_never_rolls_back_or_recommits() -> None:
    rig = _Rig()
    rig.coordinator.failure_stage = "commit"
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0


@pytest.mark.parametrize(
    ("commit_status", "task_status"),
    [
        (CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED, TaskStatus.CLEANUP_REQUIRED),
        (CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED, TaskStatus.RECOVERY_REQUIRED),
    ],
)
def test_post_head_attention_never_rolls_back(
    commit_status: CandidateCommitStatus,
    task_status: TaskStatus,
) -> None:
    rig = _Rig()
    rig.coordinator.commit_status = commit_status
    stored = rig.run()
    assert stored.task_run.status is task_status
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0
    assert rig.revisions.head.revision_id == CANDIDATE_REVISION


@pytest.mark.parametrize(
    ("commit_status", "reconcile_status", "task_status"),
    [
        (
            CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED,
            CandidateReconcileStatus.CLEANUP_REQUIRED,
            TaskStatus.CLEANUP_REQUIRED,
        ),
        (
            CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED,
            CandidateReconcileStatus.RECOVERY_REQUIRED,
            TaskStatus.RECOVERY_REQUIRED,
        ),
    ],
)
def test_exact_candidate_head_does_not_bypass_unresolved_reconcile_attention(
    commit_status: CandidateCommitStatus,
    reconcile_status: CandidateReconcileStatus,
    task_status: TaskStatus,
) -> None:
    rig = _Rig()
    rig.coordinator.commit_status = commit_status
    attention = rig.run()
    rig.coordinator.reconcile_status = reconcile_status
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=attention.generation,
    )
    assert reconciled.task_run.status is task_status
    assert reconciled.task_run.committed_revision is None
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0


def test_committing_cas_conflict_prevents_commit_and_rolls_back_candidate() -> None:
    rig = _Rig()
    rig.tasks.fail_status = TaskStatus.COMMITTING
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.FAILED
    assert rig.coordinator.commit_calls == 0
    assert rig.coordinator.rollback_calls == 1


def test_post_head_attention_cas_conflict_never_rolls_back() -> None:
    rig = _Rig()
    rig.coordinator.commit_status = CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED
    rig.tasks.fail_status = TaskStatus.CLEANUP_REQUIRED
    with pytest.raises(TaskServiceError) as caught:
        rig.run()
    assert caught.value.code is TaskServiceErrorCode.RECOVERY_REQUIRED
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0
    assert rig.revisions.head.revision_id == CANDIDATE_REVISION
    assert rig.tasks.records[TASK_ID].task_run.status is TaskStatus.COMMITTING


def test_final_task_cas_conflict_after_head_commit_requires_reconcile_without_recommit() -> None:
    rig = _Rig()
    rig.tasks.fail_status = TaskStatus.SUCCEEDED
    rig.tasks.fail_code = TaskStoreErrorCode.CONFLICT
    created = rig.create()
    with pytest.raises(TaskServiceError) as caught:
        rig.service.submit_model_program(
            task_id=TASK_ID,
            expected_generation=created.generation,
            program=_program(),
        )
    assert caught.value.code is TaskServiceErrorCode.RECOVERY_REQUIRED
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0
    durable = rig.tasks.records[TASK_ID]
    assert durable.task_run.status is TaskStatus.COMMITTING
    rig.tasks.fail_status = None
    rig.coordinator.reconcile_status = CandidateReconcileStatus.COMMITTED
    recovered = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=durable.generation,
    )
    assert recovered.task_run.status is TaskStatus.SUCCEEDED
    assert rig.coordinator.commit_calls == 1


@pytest.mark.parametrize(
    "reconcile_status",
    [CandidateReconcileStatus.CLEAN, CandidateReconcileStatus.NOT_COMMITTED],
)
def test_reconcile_uses_revision_ancestry_not_a_later_project_journal(
    reconcile_status: CandidateReconcileStatus,
) -> None:
    rig = _Rig()
    rig.tasks.fail_status = TaskStatus.SUCCEEDED
    rig.tasks.fail_code = TaskStoreErrorCode.CONFLICT
    created = rig.create()
    with pytest.raises(TaskServiceError) as caught:
        rig.service.submit_model_program(
            task_id=TASK_ID,
            expected_generation=created.generation,
            program=_program(),
        )
    assert caught.value.code is TaskServiceErrorCode.RECOVERY_REQUIRED
    durable = rig.tasks.records[TASK_ID]
    assert durable.task_run.status is TaskStatus.COMMITTING

    rig.tasks.fail_status = None
    rig.revisions.revisions[DESCENDANT_REVISION] = _descendant_revision()
    rig.revisions.head = _descendant_head()
    rig.coordinator.reconcile_status = reconcile_status
    recovered = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=durable.generation,
    )
    assert recovered.task_run.status is TaskStatus.SUCCEEDED
    assert recovered.task_run.committed_revision == CANDIDATE_REVISION
    assert rig.coordinator.commit_calls == 1


def test_reconcile_fails_closed_when_revision_ancestry_cannot_be_proved() -> None:
    rig = _Rig()
    rig.coordinator.commit_status = CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
    attention = rig.run()
    rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
    rig.revisions.head = _descendant_head()
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=attention.generation,
    )
    assert reconciled.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert reconciled.task_run.committed_revision is None


def test_reconcile_never_marks_uncommitted_when_base_revision_is_missing() -> None:
    rig = _Rig()
    stored = _store_executing(rig)
    del rig.revisions.revisions[BASE_REVISION]
    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=stored.generation,
    )
    assert reconciled.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert reconciled.task_run.committed_revision is None


@pytest.mark.parametrize("head_mode", ["candidate", "descendant"])
def test_reconcile_never_confirms_committed_through_a_missing_base_revision(
    head_mode: str,
) -> None:
    rig = _Rig()
    rig.coordinator.commit_status = CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
    attention = rig.run()
    del rig.revisions.revisions[BASE_REVISION]
    if head_mode == "descendant":
        rig.revisions.revisions[DESCENDANT_REVISION] = _descendant_revision()
        rig.revisions.head = _descendant_head()
    rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=attention.generation,
    )
    assert reconciled.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert reconciled.task_run.committed_revision is None


def test_settled_reconcile_with_session_head_divergence_stays_recovery_required() -> None:
    rig = _Rig()
    rig.coordinator.commit_status = CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
    attention = rig.run()
    rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
    rig.coordinator.reconcile_live_revision = BASE_REVISION
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=attention.generation,
    )
    assert reconciled.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert reconciled.task_run.committed_revision is None


def test_post_head_success_durability_uncertain_exact_readback_does_not_recommit() -> None:
    rig = _Rig()
    rig.tasks.uncertain_status = TaskStatus.SUCCEEDED
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.SUCCEEDED
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0


def test_post_head_success_durability_uncertain_mismatch_requires_reconcile_only() -> None:
    rig = _Rig()
    rig.tasks.uncertain_status = TaskStatus.SUCCEEDED
    rig.tasks.persist_uncertain = False
    with pytest.raises(TaskServiceError) as caught:
        rig.run()
    assert caught.value.code is TaskServiceErrorCode.RECOVERY_REQUIRED
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0
    assert rig.revisions.head.revision_id == CANDIDATE_REVISION
    assert rig.tasks.records[TASK_ID].task_run.status is TaskStatus.COMMITTING


def test_project_lease_release_failure_is_operational_not_false_task_recovery() -> None:
    rig = _Rig()
    rig.leases.issuer.error = True
    with pytest.raises(TaskServiceError) as caught:
        rig.run()
    assert caught.value.code is TaskServiceErrorCode.LEASE_UNAVAILABLE
    durable = rig.tasks.records[TASK_ID]
    assert durable.task_run.status is TaskStatus.SUCCEEDED
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0


def test_task_store_durability_uncertain_is_accepted_only_by_exact_readback() -> None:
    rig = _Rig()
    rig.tasks.uncertain_status = TaskStatus.PROGRAM_READY
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.SUCCEEDED
    assert rig.coordinator.commit_calls == 1


def test_candidate_publish_durability_uncertain_exact_readback_executes_once() -> None:
    rig = _Rig()
    rig.tasks.uncertain_status = TaskStatus.EXECUTING
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.SUCCEEDED
    assert rig.log.count("executor.execute") == 1
    assert rig.coordinator.rollback_calls == 0


def test_candidate_publish_durability_uncertain_mismatch_aborts_without_command() -> None:
    rig = _Rig()
    rig.tasks.uncertain_status = TaskStatus.EXECUTING
    rig.tasks.persist_uncertain = False
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.NEEDS_INPUT
    assert stored.task_run.candidate_revision is None
    assert rig.coordinator.rollback_calls == 1
    assert "executor.execute" not in rig.log


def test_task_store_durability_uncertain_rejects_nonmatching_readback() -> None:
    rig = _Rig()
    rig.tasks.uncertain_status = TaskStatus.PROGRAM_READY
    rig.tasks.persist_uncertain = False
    with pytest.raises(TaskServiceError) as caught:
        rig.run()
    assert caught.value.code is TaskServiceErrorCode.STORE_FAILURE
    assert "lease.acquire" not in rig.log
    assert "candidate.begin" not in rig.log


def test_unpublished_candidate_publish_conflict_rolls_back_before_release_and_runs_no_command() -> (
    None
):
    rig = _Rig()
    rig.tasks.fail_status = TaskStatus.EXECUTING
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.NEEDS_INPUT
    assert stored.task_run.candidate_revision is None
    assert rig.coordinator.rollback_calls == 1
    assert "executor.execute" not in rig.log
    assert rig.log.index("candidate.begin") < rig.log.index("candidate.rollback")
    assert rig.log.index("candidate.rollback") < rig.log.index("lease.release")


def test_candidate_published_step_cas_conflict_enters_rolling_back_and_rolls_back_once() -> None:
    rig = _Rig()
    rig.tasks.fail_status = TaskStatus.EXECUTING
    rig.tasks.fail_occurrence = 2
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.FAILED
    assert stored.task_run.candidate_revision == CANDIDATE_REVISION
    assert rig.coordinator.rollback_calls == 1
    assert rig.coordinator.commit_calls == 0
    assert rig.log.index("task.cas:rolling_back") < rig.log.index("candidate.rollback")


@pytest.mark.parametrize(
    ("rollback_status", "task_status"),
    [
        (CandidateRollbackStatus.CLEANUP_REQUIRED, TaskStatus.CLEANUP_REQUIRED),
        (CandidateRollbackStatus.RECOVERY_REQUIRED, TaskStatus.RECOVERY_REQUIRED),
    ],
)
def test_rollback_attention_is_durable_and_reconcile_not_committed_finishes_failed(
    rollback_status: CandidateRollbackStatus,
    task_status: TaskStatus,
) -> None:
    rig = _Rig()
    rig.executor.outcomes = _failed_outcomes()
    rig.coordinator.rollback_status = rollback_status
    attention = rig.run()
    assert attention.task_run.status is task_status
    assert rig.coordinator.rollback_calls == 1

    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=attention.generation,
    )
    assert reconciled.task_run.status is TaskStatus.FAILED
    assert rig.coordinator.rollback_calls == 1


def test_stale_expected_generation_rejects_before_preflight_or_project() -> None:
    rig = _Rig()
    rig.create()
    rig.log.clear()
    with pytest.raises(TaskServiceError) as caught:
        rig.service.submit_model_program(
            task_id=TASK_ID,
            expected_generation=99,
            program=_program(),
        )
    assert caught.value.code is TaskServiceErrorCode.CONFLICT
    assert rig.log == ["task.load"]


def test_begin_attention_without_published_candidate_is_durable_and_retryable() -> None:
    rig = _Rig()
    rig.coordinator.begin_error = CandidateError(
        CandidateErrorCode.CLEANUP_REQUIRED,
        cleanup_required=True,
    )
    stored = rig.run()
    assert stored.task_run.status is TaskStatus.CLEANUP_REQUIRED
    assert stored.task_run.candidate_revision is None
    assert rig.coordinator.rollback_calls == 0
    rig.coordinator.begin_error = None
    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=stored.generation,
    )
    assert reconciled.task_run.status is TaskStatus.PROGRAM_READY
    assert reconciled.task_run.candidate_revision is None
    assert reconciled.task_run.last_error is None


def test_pre_candidate_clean_reconcile_confirms_only_the_pre_candidate_attempt() -> None:
    rig = _Rig()
    rig.coordinator.begin_error = CandidateError(
        CandidateErrorCode.RECOVERY_REQUIRED,
        recovery_required=True,
    )
    attention = rig.run()
    assert attention.task_run.status is TaskStatus.RECOVERY_REQUIRED
    rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=attention.generation,
    )
    assert reconciled.task_run.status is TaskStatus.PROGRAM_READY
    assert reconciled.task_run.candidate_revision is None


@pytest.mark.parametrize("mode", ["clean_base", "committed_wrong_manifest"])
def test_reconcile_never_confirms_success_without_exact_candidate_head(mode: str) -> None:
    rig = _Rig()
    rig.coordinator.commit_status = CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
    attention = rig.run()
    assert attention.task_run.status is TaskStatus.RECOVERY_REQUIRED
    if mode == "clean_base":
        rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
        rig.revisions.head = _base_head()
    else:
        rig.coordinator.reconcile_status = CandidateReconcileStatus.COMMITTED
        rig.revisions.head = ProjectHead(
            project_id=PROJECT_ID,
            generation=2,
            revision_id=CANDIDATE_REVISION,
            manifest_sha256="f" * 64,
        )
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=attention.generation,
    )
    assert reconciled.task_run.status is not TaskStatus.SUCCEEDED
    assert reconciled.task_run.committed_revision is None
    assert rig.coordinator.commit_calls == 1
    assert rig.coordinator.rollback_calls == 0


def test_crash_stale_candidate_state_is_durably_marked_before_reconcile_side_effects() -> None:
    rig = _Rig()
    stored = _store_executing(rig)
    rig.log.clear()
    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=stored.generation,
    )
    assert reconciled.task_run.status is TaskStatus.FAILED
    assert rig.log.index("lease.acquire") < rig.log.index("task.cas:recovery_required")
    assert rig.log.index("task.cas:recovery_required") < rig.log.index("candidate.reconcile")


@pytest.mark.parametrize(
    "reconcile_status",
    [CandidateReconcileStatus.CLEAN, CandidateReconcileStatus.NOT_COMMITTED],
    ids=("before-begin", "private-begin-before-publish"),
)
def test_crash_in_validating_program_recovers_the_pre_candidate_attempt(
    reconcile_status: CandidateReconcileStatus,
) -> None:
    rig = _Rig()
    stored = _store_validating(rig)
    rig.log.clear()
    rig.coordinator.reconcile_status = reconcile_status
    reconciled = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=stored.generation,
    )
    assert reconciled.task_run.status is TaskStatus.PROGRAM_READY
    assert reconciled.task_run.candidate_revision is None
    assert reconciled.task_run.last_error is None
    assert rig.log.index("lease.acquire") < rig.log.index("task.cas:recovery_required")
    assert rig.log.index("task.cas:recovery_required") < rig.log.index("candidate.reconcile")
    assert rig.log.index("candidate.reconcile") < rig.log.index("task.cas:program_ready")


def test_reconcile_lease_contention_does_not_reclassify_an_active_writer() -> None:
    rig = _Rig()
    stored = _store_executing(rig)
    rig.leases.error = LeaseError(LeaseErrorCode.CONTENDED)
    with pytest.raises(TaskServiceError) as caught:
        rig.service.reconcile_task(
            task_id=TASK_ID,
            expected_generation=stored.generation,
        )
    assert caught.value.code is TaskServiceErrorCode.LEASE_UNAVAILABLE
    assert rig.tasks.records[TASK_ID] == stored
    assert "candidate.reconcile" not in rig.log


def test_cleanup_attention_can_escalate_to_recovery_before_candidate() -> None:
    rig = _Rig()
    created = rig.create()
    submitted = transition_task(created.task_run, TaskEvent.SUBMIT_PROGRAM, program=_program())
    validating = transition_task(submitted, TaskEvent.START_VALIDATION)
    cleanup = transition_task(
        validating,
        TaskEvent.REQUIRE_CLEANUP,
        error=_error("cleanup_required"),
    )
    recovery = transition_task(
        cleanup,
        TaskEvent.REQUIRE_RECOVERY,
        error=_error("recovery_required"),
    )
    assert recovery.status is TaskStatus.RECOVERY_REQUIRED
    assert recovery.candidate_revision is None


def test_get_missing_and_invalid_continue_state_are_fixed() -> None:
    rig = _Rig()
    with pytest.raises(TaskServiceError) as missing:
        rig.service.get_task(task_id=TASK_ID)
    assert missing.value.code is TaskServiceErrorCode.NOT_FOUND
    created = rig.create()
    with pytest.raises(TaskServiceError) as invalid:
        rig.service.continue_task(task_id=TASK_ID, expected_generation=created.generation)
    assert invalid.value.code is TaskServiceErrorCode.INVALID_STATE


def test_durable_review_service_contract_is_explicit() -> None:
    state_module = __import__("vibecad.workflow.state", fromlist=["ReviewPolicy"])
    assert hasattr(state_module, "ReviewPolicy")
    assert hasattr(state_module, "ReviewDraft")
    create_parameters = inspect.signature(TaskService.create_task).parameters
    assert "review_policy" in create_parameters
    assert hasattr(TaskService, "accept_draft")
    assert hasattr(TaskService, "reject_draft")


def test_require_review_detaches_and_releases_before_publishing_awaiting() -> None:
    rig = _Rig()
    stored = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    task = stored.task_run
    assert task.status is TaskStatus.AWAITING_USER_REVIEW
    assert task.review_policy is ReviewPolicy.REQUIRE_REVIEW
    assert task.draft is not None
    assert task.draft.revision_id == CANDIDATE_REVISION
    assert task.draft.base_revision == BASE_REVISION
    assert task.draft.base_generation == _base_head().generation
    assert task.draft.base_manifest_sha256 == BASE_MANIFEST
    assert task.committed_revision is None
    assert rig.revisions.head == _base_head()
    assert rig.coordinator.publish_review_calls == 1
    assert rig.coordinator.commit_calls == 0
    assert rig.log.index("task.cas:preparing_review") < rig.log.index(
        "candidate.publish_review"
    )
    assert rig.log.index("candidate.publish_review") < rig.log.index("lease.release")
    assert rig.log.index("lease.release") < rig.log.index(
        "task.cas:awaiting_user_review"
    )


def test_attribute_compatible_detach_claim_cannot_publish_a_draft() -> None:
    rig = _Rig()
    rig.coordinator.publish_review_override = SimpleNamespace(
        status=CandidateRollbackStatus.NOT_COMMITTED,
        head=_base_head(),
        head_committed=False,
        cleanup_required=False,
        recovery_required=False,
    )

    stored = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)

    assert stored.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert stored.task_run.draft is not None
    assert stored.task_run.committed_revision is None
    assert rig.revisions.head == _base_head()
    assert rig.coordinator.commit_calls == 0
    assert "task.cas:awaiting_user_review" not in rig.log


def test_reject_is_head_neutral_and_same_decision_replay_is_read_only() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.log.clear()
    rejected = rig.service.reject_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert rejected.task_run.status is TaskStatus.REJECTED
    assert rejected.task_run.committed_revision is None
    assert rig.revisions.head == _base_head()
    assert "lease.acquire" not in rig.log
    generation = rejected.generation
    replay = rig.service.reject_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert replay == rejected
    assert replay.generation == generation
    with pytest.raises(TaskServiceError) as opposite:
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )
    assert opposite.value.code is TaskServiceErrorCode.CONFLICT


def test_accept_reacquires_reverifies_commits_once_and_replays_read_only() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.log.clear()
    accepted = rig.service.accept_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert accepted.task_run.status is TaskStatus.SUCCEEDED
    assert accepted.task_run.committed_revision == CANDIDATE_REVISION
    assert accepted.task_run.draft == awaiting.task_run.draft
    assert rig.coordinator.reopen_review_calls == 1
    assert rig.coordinator.prepare_review_calls == 1
    assert rig.coordinator.commit_calls == 1
    assert rig.log.index("lease.acquire") < rig.log.index("task.cas:accepting_draft")
    assert rig.log.index("task.cas:accepting_draft") < rig.log.index(
        "candidate.reopen_review"
    )
    assert rig.log.index("executor.collect") < rig.log.index("candidate.prepare_review")
    assert rig.log.index("candidate.prepare_review") < rig.log.index("candidate.commit")
    assert rig.log.index("candidate.commit") < rig.log.index("lease.release")
    replay = rig.service.accept_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert replay == accepted
    assert rig.coordinator.commit_calls == 1
    with pytest.raises(TaskServiceError) as opposite:
        rig.service.reject_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )
    assert opposite.value.code is TaskServiceErrorCode.CONFLICT


def test_stale_draft_accept_changes_no_task_journal_or_head() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.revisions.revisions[DESCENDANT_REVISION] = _descendant_revision()
    rig.revisions.head = _descendant_head()
    rig.log.clear()
    with pytest.raises(TaskServiceError) as caught:
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )
    assert caught.value.code is TaskServiceErrorCode.CONFLICT
    assert rig.tasks.records[TASK_ID] == awaiting
    assert rig.revisions.head == _descendant_head()
    assert rig.coordinator.reopen_review_calls == 0
    assert rig.coordinator.prepare_review_calls == 0
    assert rig.coordinator.commit_calls == 0


def test_review_release_failure_never_publishes_awaiting_and_resume_recovers() -> None:
    rig = _Rig()
    rig.leases.issuer.error = True
    with pytest.raises(TaskServiceError) as caught:
        rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert caught.value.code is TaskServiceErrorCode.LEASE_UNAVAILABLE
    preparing = rig.tasks.records[TASK_ID]
    assert preparing.task_run.status is TaskStatus.PREPARING_REVIEW
    assert not any(item == "task.cas:awaiting_user_review" for item in rig.log)
    rig.leases.issuer.error = False
    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    recovered = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=preparing.generation,
    )
    assert recovered.task_run.status is TaskStatus.AWAITING_USER_REVIEW
    final_release = max(index for index, item in enumerate(rig.log) if item == "lease.release")
    final_publish = max(
        index
        for index, item in enumerate(rig.log)
        if item == "task.cas:awaiting_user_review"
    )
    assert final_release < final_publish


@pytest.mark.parametrize("journal_mode", ["missing", "wrong_candidate", "wrong_manifest"])
def test_preparing_review_requires_its_exact_terminal_journal(journal_mode: str) -> None:
    rig = _Rig()
    rig.leases.issuer.error = True
    with pytest.raises(TaskServiceError):
        rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    preparing = rig.tasks.records[TASK_ID]
    assert preparing.task_run.status is TaskStatus.PREPARING_REVIEW
    rig.leases.issuer.error = False
    if journal_mode == "missing":
        rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
    else:
        rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
        if journal_mode == "wrong_candidate":
            rig.coordinator.reconcile_journal_candidate = DESCENDANT_REVISION
        else:
            rig.coordinator.reconcile_journal_manifest = "e" * 64

    recovered = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=preparing.generation,
    )

    assert recovered.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert recovered.task_run.committed_revision is None
    assert rig.revisions.head == _base_head()
    assert "task.cas:awaiting_user_review" not in rig.log


def test_accept_response_loss_reconciles_exact_head_without_second_commit() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.tasks.fail_status = TaskStatus.SUCCEEDED
    with pytest.raises(TaskServiceError) as caught:
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )
    assert caught.value.code is TaskServiceErrorCode.RECOVERY_REQUIRED
    accepting = rig.tasks.records[TASK_ID]
    assert accepting.task_run.status is TaskStatus.ACCEPTING_DRAFT
    assert rig.revisions.head == _candidate_head()
    assert rig.coordinator.commit_calls == 1
    rig.tasks.fail_status = None
    rig.coordinator.reconcile_status = CandidateReconcileStatus.COMMITTED
    rig.coordinator.reconcile_live_revision = CANDIDATE_REVISION
    recovered = rig.service.accept_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert recovered.task_run.status is TaskStatus.SUCCEEDED
    assert recovered.task_run.committed_revision == CANDIDATE_REVISION
    assert rig.coordinator.commit_calls == 1


def test_accept_response_loss_reconciles_through_a_later_descendant_without_recommit() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.tasks.fail_status = TaskStatus.SUCCEEDED
    with pytest.raises(TaskServiceError):
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )
    accepting = rig.tasks.records[TASK_ID]
    assert accepting.task_run.status is TaskStatus.ACCEPTING_DRAFT
    assert rig.coordinator.commit_calls == 1
    rig.tasks.fail_status = None
    rig.revisions.revisions[DESCENDANT_REVISION] = _descendant_revision()
    rig.revisions.head = _descendant_head()
    rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
    rig.coordinator.reconcile_live_revision = DESCENDANT_REVISION

    recovered = rig.service.accept_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )

    assert recovered.task_run.status is TaskStatus.SUCCEEDED
    assert recovered.task_run.committed_revision == CANDIDATE_REVISION
    assert rig.revisions.head == _descendant_head()
    assert rig.coordinator.commit_calls == 1


@pytest.mark.parametrize("terminal_mode", ["absent", "unrelated", "prior_committed"])
def test_accepting_restart_allows_any_terminal_base_transaction(
    terminal_mode: str,
) -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    accepting = StoredTaskRun(
        generation=awaiting.generation + 1,
        task_run=transition_task(awaiting.task_run, TaskEvent.ACCEPT_DRAFT),
    )
    rig.tasks.records[TASK_ID] = accepting
    if terminal_mode == "absent":
        rig.coordinator.reconcile_status = CandidateReconcileStatus.CLEAN
    elif terminal_mode == "unrelated":
        rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
        rig.coordinator.reconcile_journal_candidate = DESCENDANT_REVISION
        rig.coordinator.reconcile_journal_manifest = "e" * 64
    else:
        rig.coordinator.reconcile_status = CandidateReconcileStatus.COMMITTED

    recovered = rig.service.accept_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )

    assert recovered.task_run.status is TaskStatus.SUCCEEDED
    assert recovered.task_run.committed_revision == CANDIDATE_REVISION
    assert rig.coordinator.reopen_review_calls == 1
    assert rig.coordinator.prepare_review_calls == 1
    assert rig.coordinator.commit_calls == 1


def test_preprepare_recovery_with_unrelated_terminal_journal_returns_to_review() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.executor.failure_stage = "collect"
    with pytest.raises(TaskServiceError):
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )
    recovery = rig.tasks.records[TASK_ID]
    assert recovery.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert rig.coordinator.commit_calls == 0
    rig.executor.failure_stage = None
    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    rig.coordinator.reconcile_journal_candidate = DESCENDANT_REVISION
    rig.coordinator.reconcile_journal_manifest = "e" * 64

    resumed = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=recovery.generation,
    )

    assert resumed.task_run.status is TaskStatus.AWAITING_USER_REVIEW
    assert resumed.task_run.last_error is None
    assert rig.revisions.head == _base_head()
    assert rig.coordinator.commit_calls == 0


def test_preparing_restart_never_publishes_a_missing_draft_revision() -> None:
    rig = _Rig()
    rig.leases.issuer.error = True
    with pytest.raises(TaskServiceError):
        rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    preparing = rig.tasks.records[TASK_ID]
    assert preparing.task_run.status is TaskStatus.PREPARING_REVIEW
    rig.leases.issuer.error = False
    rig.revisions.revisions.pop(CANDIDATE_REVISION)
    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    recovered = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=preparing.generation,
    )
    assert recovered.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert recovered.task_run.last_error is not None
    assert recovered.task_run.last_error.code == "draft_integrity_failed"
    assert rig.revisions.head == _base_head()


@pytest.mark.parametrize("failure", ["reopen_review", "collect", "prepare_review"])
def test_prepared_accept_integrity_failures_never_mutate_head(failure: str) -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    if failure == "collect":
        rig.executor.failure_stage = failure
    else:
        rig.coordinator.failure_stage = failure
    with pytest.raises(TaskServiceError) as caught:
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )
    assert caught.value.code is TaskServiceErrorCode.RECOVERY_REQUIRED
    durable = rig.tasks.records[TASK_ID]
    assert durable.task_run.status is TaskStatus.RECOVERY_REQUIRED
    assert durable.task_run.last_error is not None
    assert durable.task_run.last_error.code == "draft_integrity_failed"
    assert rig.revisions.head == _base_head()
    assert rig.coordinator.commit_calls == 0


def test_review_decision_wrong_draft_and_stale_generation_are_side_effect_free() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    for operation in (rig.service.accept_draft, rig.service.reject_draft):
        with pytest.raises(TaskServiceError) as wrong:
            operation(
                task_id=TASK_ID,
                draft_id="draft_22222222222222222222222222222222",
                expected_generation=awaiting.generation,
            )
        assert wrong.value.code is TaskServiceErrorCode.CONFLICT
        with pytest.raises(TaskServiceError) as stale:
            operation(
                task_id=TASK_ID,
                draft_id=awaiting.task_run.draft.id,
                expected_generation=awaiting.generation - 1,
            )
        assert stale.value.code is TaskServiceErrorCode.CONFLICT
    assert rig.tasks.records[TASK_ID] == awaiting
    assert rig.revisions.head == _base_head()


def test_accepting_task_cas_conflict_precedes_all_review_cad_work() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.tasks.fail_status = TaskStatus.ACCEPTING_DRAFT
    rig.log.clear()

    with pytest.raises(TaskServiceError) as caught:
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )

    assert caught.value.code is TaskServiceErrorCode.CONFLICT
    assert rig.tasks.records[TASK_ID] == awaiting
    assert rig.revisions.head == _base_head()
    assert rig.coordinator.reopen_review_calls == 0
    assert rig.coordinator.prepare_review_calls == 0
    assert rig.coordinator.commit_calls == 0


def test_publish_draft_durability_uncertain_mismatch_resumes_from_preparing() -> None:
    rig = _Rig()
    rig.tasks.uncertain_status = TaskStatus.AWAITING_USER_REVIEW
    rig.tasks.persist_uncertain = False

    with pytest.raises(TaskServiceError) as caught:
        rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)

    assert caught.value.code is TaskServiceErrorCode.STORE_FAILURE
    preparing = rig.tasks.records[TASK_ID]
    assert preparing.task_run.status is TaskStatus.PREPARING_REVIEW
    assert rig.revisions.head == _base_head()
    rig.tasks.uncertain_status = None
    rig.coordinator.reconcile_status = CandidateReconcileStatus.NOT_COMMITTED
    resumed = rig.service.reconcile_task(
        task_id=TASK_ID,
        expected_generation=preparing.generation,
    )
    assert resumed.task_run.status is TaskStatus.AWAITING_USER_REVIEW


def test_accepting_draft_durability_uncertain_exact_readback_commits_once() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.tasks.uncertain_status = TaskStatus.ACCEPTING_DRAFT

    accepted = rig.service.accept_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )

    assert accepted.task_run.status is TaskStatus.SUCCEEDED
    assert accepted.task_run.committed_revision == CANDIDATE_REVISION
    assert rig.coordinator.commit_calls == 1


def test_reviewed_success_durability_uncertain_mismatch_never_recommits() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.tasks.uncertain_status = TaskStatus.SUCCEEDED
    rig.tasks.persist_uncertain = False

    with pytest.raises(TaskServiceError) as caught:
        rig.service.accept_draft(
            task_id=TASK_ID,
            draft_id=awaiting.task_run.draft.id,
            expected_generation=awaiting.generation,
        )

    assert caught.value.code is TaskServiceErrorCode.RECOVERY_REQUIRED
    accepting = rig.tasks.records[TASK_ID]
    assert accepting.task_run.status is TaskStatus.ACCEPTING_DRAFT
    assert rig.revisions.head == _candidate_head()
    assert rig.coordinator.commit_calls == 1
    rig.tasks.uncertain_status = None
    rig.coordinator.reconcile_status = CandidateReconcileStatus.COMMITTED
    rig.coordinator.reconcile_live_revision = CANDIDATE_REVISION
    recovered = rig.service.accept_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert recovered.task_run.status is TaskStatus.SUCCEEDED
    assert rig.coordinator.commit_calls == 1


def test_reject_durability_uncertain_exact_readback_and_replay_are_idempotent() -> None:
    rig = _Rig()
    awaiting = rig.run(review_policy=ReviewPolicy.REQUIRE_REVIEW)
    assert awaiting.task_run.draft is not None
    rig.tasks.uncertain_status = TaskStatus.REJECTED
    rejected = rig.service.reject_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert rejected.task_run.status is TaskStatus.REJECTED
    assert rig.revisions.head == _base_head()
    replay = rig.service.reject_draft(
        task_id=TASK_ID,
        draft_id=awaiting.task_run.draft.id,
        expected_generation=awaiting.generation,
    )
    assert replay == rejected
