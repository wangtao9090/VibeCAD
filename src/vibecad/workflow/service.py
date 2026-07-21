"""Transactional orchestration for one deterministic CAD task attempt.

The service deliberately owns ordering, durable state transitions, and recovery
decisions.  It does not own reasoning, repair a model program, retry semantic
CAD work, or expose a public MCP surface.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

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
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.execution.revisions import (
    CommitJournal,
    CommitJournalState,
    LocalRevisionStore,
    ProjectHead,
    ReconciliationResult,
    ReconciliationStatus,
    RevisionRef,
)
from vibecad.interaction.cad import CadExecutionPort, CandidateEvidence
from vibecad.validation import (
    CompiledAcceptance,
    VerificationResult,
    compile_acceptance_spec,
    verify_acceptance,
)
from vibecad.workflow.catalog import (
    TaskCatalogError,
    TaskCatalogErrorCode,
    TaskCatalogService,
)
from vibecad.workflow.contracts import ErrorCategory, ModelProgram, StepError
from vibecad.workflow.lease import LeaseError, ResourceLeaseManager
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewDraft,
    ReviewPolicy,
    TaskEvent,
    TaskRun,
    TaskStateError,
    TaskStatus,
    append_artifact,
    append_step_result,
    append_verification,
    transition_task,
)
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreError,
    TaskStoreErrorCode,
)

__all__ = (
    "TaskServiceErrorCode",
    "TaskServiceError",
    "TaskService",
)

_MAX_MODEL_PROGRAM_BYTES = 512 * 1024
_MAX_RECONCILE_LINEAGE_DEPTH = 256


class _CandidateLineage(StrEnum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    UNKNOWN = "unknown"


class TaskServiceErrorCode(StrEnum):
    """Stable failures returned by the internal orchestration boundary."""

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_REASONING_OWNER = "unsupported_reasoning_owner"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    STORE_FAILURE = "store_failure"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RECOVERY_REQUIRED = "recovery_required"


_ERROR_MESSAGES = {
    TaskServiceErrorCode.INVALID_INPUT: "The task service input is invalid.",
    TaskServiceErrorCode.UNSUPPORTED_REASONING_OWNER: (
        "The requested reasoning owner is not supported."
    ),
    TaskServiceErrorCode.INVALID_STATE: "The task is not ready for this operation.",
    TaskServiceErrorCode.NOT_FOUND: "The task record was not found.",
    TaskServiceErrorCode.CONFLICT: "The task record changed concurrently.",
    TaskServiceErrorCode.STORE_FAILURE: "The task record operation failed.",
    TaskServiceErrorCode.LEASE_UNAVAILABLE: "The project write lease is unavailable.",
    TaskServiceErrorCode.RECOVERY_REQUIRED: "The task requires explicit reconciliation.",
}


class TaskServiceError(ValueError):
    """Fixed, path-free, non-reflective service error."""

    __slots__ = ("code", "message", "schema_version")

    def __init__(self, code: TaskServiceErrorCode) -> None:
        if type(code) is not TaskServiceErrorCode:
            raise TypeError("code must be a TaskServiceErrorCode")
        self.schema_version = 1
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "message": self.message,
        }


_VALIDATION_ERROR = StepError(
    category=ErrorCategory.VALIDATION,
    code="program_validation_failed",
    message="The submitted model program is invalid.",
    retryable=False,
    needs_input=True,
    related_objects=(),
    diagnostic_artifacts=(),
)
_PRE_CANDIDATE_CONFLICT = StepError(
    category=ErrorCategory.CONFLICT,
    code="project_state_conflict",
    message="The project state changed before candidate execution.",
    retryable=False,
    needs_input=True,
    related_objects=(),
    diagnostic_artifacts=(),
)
_BEGIN_FAILURE = StepError(
    category=ErrorCategory.RUNTIME,
    code="candidate_begin_failed",
    message="The candidate could not be prepared.",
    retryable=False,
    needs_input=True,
    related_objects=(),
    diagnostic_artifacts=(),
)
_EXECUTION_FAILURE = StepError(
    category=ErrorCategory.RUNTIME,
    code="candidate_execution_failed",
    message="The candidate execution failed.",
    retryable=False,
    needs_input=False,
    related_objects=(),
    diagnostic_artifacts=(),
)
_VERIFICATION_FAILURE = StepError(
    category=ErrorCategory.POLICY,
    code="acceptance_verification_failed",
    message="The candidate did not satisfy the acceptance specification.",
    retryable=False,
    needs_input=False,
    related_objects=(),
    diagnostic_artifacts=(),
)
_CLEANUP_ERROR = StepError(
    category=ErrorCategory.RUNTIME,
    code="cleanup_required",
    message="Candidate cleanup requires explicit attention.",
    retryable=False,
    needs_input=False,
    related_objects=(),
    diagnostic_artifacts=(),
)
_RECOVERY_ERROR = StepError(
    category=ErrorCategory.RUNTIME,
    code="recovery_required",
    message="Candidate recovery requires explicit reconciliation.",
    retryable=False,
    needs_input=False,
    related_objects=(),
    diagnostic_artifacts=(),
)
_REVIEW_INTEGRITY_ERROR = StepError(
    category=ErrorCategory.POLICY,
    code="draft_integrity_failed",
    message="The durable draft no longer matches its verified evidence.",
    retryable=False,
    needs_input=False,
    related_objects=(),
    diagnostic_artifacts=(),
)


def _raise(code: TaskServiceErrorCode) -> None:
    raise TaskServiceError(code)


_CATALOG_ERROR_MAP = {
    TaskCatalogErrorCode.INVALID_INPUT: TaskServiceErrorCode.INVALID_INPUT,
    TaskCatalogErrorCode.UNSUPPORTED_REASONING_OWNER: (
        TaskServiceErrorCode.UNSUPPORTED_REASONING_OWNER
    ),
    TaskCatalogErrorCode.INVALID_STATE: TaskServiceErrorCode.INVALID_STATE,
    TaskCatalogErrorCode.NOT_FOUND: TaskServiceErrorCode.NOT_FOUND,
    TaskCatalogErrorCode.CONFLICT: TaskServiceErrorCode.CONFLICT,
    TaskCatalogErrorCode.STORE_FAILURE: TaskServiceErrorCode.STORE_FAILURE,
}


def _catalog_call(action):
    failure = None
    try:
        return action()
    except TaskCatalogError as error:
        failure = _CATALOG_ERROR_MAP[error.code]
    assert failure is not None
    raise TaskServiceError(failure)


def _expected_generation(value: object) -> int:
    if type(value) is not int or value < 0:
        _raise(TaskServiceErrorCode.INVALID_INPUT)
    return value


def _attention_event(value: object) -> TaskEvent | None:
    if isinstance(value, CandidateError):
        if value.recovery_required or value.code is CandidateErrorCode.RECOVERY_REQUIRED:
            return TaskEvent.REQUIRE_RECOVERY
        if value.cleanup_required or value.code is CandidateErrorCode.CLEANUP_REQUIRED:
            return TaskEvent.REQUIRE_CLEANUP
        return None
    if bool(getattr(value, "recovery_required", False)):
        return TaskEvent.REQUIRE_RECOVERY
    if bool(getattr(value, "cleanup_required", False)):
        return TaskEvent.REQUIRE_CLEANUP
    return None


def _attention_error(event: TaskEvent) -> StepError:
    return _RECOVERY_ERROR if event is TaskEvent.REQUIRE_RECOVERY else _CLEANUP_ERROR


class TaskService:
    """Compose trusted task, revision, lease, candidate, executor, and verifier ports."""

    __slots__ = (
        "_catalog",
        "_coordinator",
        "_executor",
        "_lease_manager",
        "_revision_store",
        "_runtime_head",
        "_runtime_stale",
        "_task_store",
    )

    def __init__(
        self,
        *,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
        lease_manager: ResourceLeaseManager,
        coordinator: CandidateCoordinator,
        executor: CadExecutionPort,
        runtime_head: ProjectHead,
    ) -> None:
        if not (
            isinstance(task_store, TaskRunStore)
            and isinstance(revision_store, LocalRevisionStore)
            and isinstance(lease_manager, ResourceLeaseManager)
            and isinstance(coordinator, CandidateCoordinator)
            and isinstance(executor, CadExecutionPort)
            and type(runtime_head) is ProjectHead
        ):
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        if not (
            getattr(task_store, "_lease_manager", None) is lease_manager
            and getattr(revision_store, "_lease_manager", None) is lease_manager
            and getattr(coordinator, "_store", None) is revision_store
            and getattr(coordinator, "_snapshot_port", None) is executor
        ):
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        self._task_store = task_store
        self._revision_store = revision_store
        self._lease_manager = lease_manager
        self._coordinator = coordinator
        self._executor = executor
        self._runtime_head = runtime_head
        self._runtime_stale = False
        self._catalog = TaskCatalogService(
            task_store=task_store,
            revision_store=revision_store,
        )

    @property
    def runtime_stale(self) -> bool:
        return self._runtime_stale

    @property
    def runtime_head(self) -> ProjectHead:
        return self._runtime_head

    def _guard_runtime_head(self, project_id: str) -> ProjectHead:
        try:
            current = self._revision_store.load_head(project_id)
        except Exception:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        if not (
            type(current) is ProjectHead
            and current == self._runtime_head
            and current.project_id == project_id
        ):
            self._runtime_stale = True
            _raise(TaskServiceErrorCode.CONFLICT)
        return current

    def create_task(
        self,
        *,
        task_id: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
    ) -> StoredTaskRun:
        return _catalog_call(
            lambda: self._catalog.create_task(
                task_id=task_id,
                project_id=project_id,
                reasoning_owner=reasoning_owner,
                review_policy=review_policy,
            )
        )

    def get_task(self, *, task_id: str) -> StoredTaskRun:
        return _catalog_call(lambda: self._catalog.get_task(task_id=task_id))

    def accept_draft(
        self,
        *,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> StoredTaskRun:
        expected = _expected_generation(expected_generation)
        stored = self._load(task_id)
        task = stored.task_run
        draft = task.draft
        if type(draft) is not ReviewDraft:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        if type(draft_id) is not str or draft.id != draft_id:
            _raise(TaskServiceErrorCode.CONFLICT)
        if task.status is TaskStatus.SUCCEEDED:
            if (
                task.review_policy is ReviewPolicy.REQUIRE_REVIEW
                and task.committed_revision == draft.revision_id
            ):
                return stored
            _raise(TaskServiceErrorCode.INVALID_STATE)
        if task.status is TaskStatus.REJECTED:
            _raise(TaskServiceErrorCode.CONFLICT)
        if task.status is TaskStatus.ACCEPTING_DRAFT:
            reconciled = self.reconcile_task(
                task_id=task_id,
                expected_generation=stored.generation,
            )
            if reconciled.task_run.status is TaskStatus.SUCCEEDED:
                return reconciled
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        if task.status is not TaskStatus.AWAITING_USER_REVIEW:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        if stored.generation != expected:
            _raise(TaskServiceErrorCode.CONFLICT)

        lease = self._acquire(task.project_id)
        result: StoredTaskRun | None = None
        caught: TaskServiceError | None = None
        try:
            try:
                head = self._guard_runtime_head(task.project_id)
                expected_head = self._draft_head(draft)
                if head != expected_head:
                    _raise(TaskServiceErrorCode.CONFLICT)
                accepting = self._cas(
                    stored,
                    transition_task(task, TaskEvent.ACCEPT_DRAFT),
                )
                result = self._accept_with_lease(accepting, expected_head, lease)
            except TaskServiceError as error:
                caught = error
            except Exception:
                caught = TaskServiceError(TaskServiceErrorCode.RECOVERY_REQUIRED)
        finally:
            release_failed = self._release(lease)
        if release_failed:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
        if caught is not None:
            raise caught from None
        if result is None or result.task_run.status is not TaskStatus.SUCCEEDED:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return result

    def reject_draft(
        self,
        *,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> StoredTaskRun:
        return _catalog_call(
            lambda: self._catalog.reject_draft(
                task_id=task_id,
                draft_id=draft_id,
                expected_generation=expected_generation,
            )
        )

    def submit_model_program(
        self,
        *,
        task_id: str,
        expected_generation: int,
        program: ModelProgram,
    ) -> StoredTaskRun:
        stored = self._load_expected(task_id, expected_generation)
        task = stored.task_run
        if task.status not in {TaskStatus.NEEDS_PLAN, TaskStatus.NEEDS_INPUT}:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        if task.candidate_revision is not None:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        if (
            type(program) is not ModelProgram
            or program.task_id != task.id
            or program.base_revision != task.base_revision
        ):
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        if not self._program_within_budget(program):
            _raise(TaskServiceErrorCode.INVALID_INPUT)

        try:
            submitted = transition_task(task, TaskEvent.SUBMIT_PROGRAM, program=program)
        except TaskStateError:
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        self._ensure_persistable(submitted, stored.generation + 1)
        preflight = self._preflight(program)
        if preflight is None:
            stored = self._cas(stored, submitted)
            return self._reject_validation(stored)
        compiled, validated = preflight
        return self._continue_preflighted(
            stored,
            compiled,
            validated,
            submitted=submitted,
        )

    def continue_task(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun:
        stored = self._load_expected(task_id, expected_generation)
        if stored.task_run.status is not TaskStatus.PROGRAM_READY:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        program = stored.task_run.program
        if type(program) is not ModelProgram:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        preflight = self._preflight(program)
        if preflight is None:
            return self._reject_validation(stored)
        compiled, validated = preflight
        return self._continue_preflighted(stored, compiled, validated)

    @staticmethod
    def _program_within_budget(program: ModelProgram) -> bool:
        try:
            encoded = json.dumps(
                program.to_mapping(),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            return len(encoded) <= _MAX_MODEL_PROGRAM_BYTES
        except Exception:
            return False

    def _ensure_persistable(self, task: TaskRun, generation: int) -> None:
        failure: TaskServiceErrorCode | None = None
        try:
            self._task_store.validate_record(task, generation)
        except TaskStoreError as error:
            if error.code in {
                TaskStoreErrorCode.CORRUPT_RECORD,
                TaskStoreErrorCode.RECORD_TOO_LARGE,
            }:
                failure = TaskServiceErrorCode.INVALID_INPUT
            else:
                failure = TaskServiceErrorCode.STORE_FAILURE
        except Exception:
            failure = TaskServiceErrorCode.STORE_FAILURE
        if failure is not None:
            _raise(failure)

    def _preflight(self, program: ModelProgram) -> tuple[CompiledAcceptance, object] | None:
        try:
            if not self._program_within_budget(program):
                return None
            compiled = compile_acceptance_spec(program.acceptance)
            validated = self._executor.validate_program(program)
        except Exception:
            return None
        return (compiled, validated)

    def reconcile_task(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun:
        stored = self._load_expected(task_id, expected_generation)
        if stored.task_run.status not in {
            TaskStatus.VALIDATING_PROGRAM,
            TaskStatus.EXECUTING,
            TaskStatus.VERIFYING,
            TaskStatus.COMMITTING,
            TaskStatus.PREPARING_REVIEW,
            TaskStatus.ACCEPTING_DRAFT,
            TaskStatus.ROLLING_BACK,
            TaskStatus.RECOVERY_REQUIRED,
            TaskStatus.CLEANUP_REQUIRED,
        }:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        lease = self._acquire(stored.task_run.project_id)
        result: object | None = None
        caught: TaskServiceError | None = None
        post_release_event: TaskEvent | None = None
        try:
            try:
                self._guard_runtime_head(stored.task_run.project_id)
                if stored.task_run.status in {
                    TaskStatus.VALIDATING_PROGRAM,
                    TaskStatus.EXECUTING,
                    TaskStatus.VERIFYING,
                    TaskStatus.COMMITTING,
                }:
                    stored = self._persist_attention(
                        stored,
                        TaskEvent.REQUIRE_RECOVERY,
                    )
                result = self._coordinator.reconcile(
                    project_id=stored.task_run.project_id,
                    lease=lease,
                )
                if self._has_review_origin(stored.task_run):
                    stored, post_release_event = self._apply_review_reconcile(
                        stored,
                        result,
                        lease,
                    )
                else:
                    stored = self._apply_reconcile(stored, result)
            except TaskServiceError as error:
                caught = error
            except Exception:
                caught = TaskServiceError(TaskServiceErrorCode.RECOVERY_REQUIRED)
        finally:
            release_failed = self._release(lease)
        if release_failed:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
        if caught is not None:
            raise caught from None
        assert result is not None
        if post_release_event is not None:
            try:
                stored = self._cas(
                    stored,
                    transition_task(stored.task_run, post_release_event),
                )
            except (TaskServiceError, TaskStateError):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return stored

    @staticmethod
    def _has_review_origin(task: TaskRun) -> bool:
        return type(task.draft) is ReviewDraft and any(
            record.event is TaskEvent.PREPARE_REVIEW for record in task.transitions
        )

    @staticmethod
    def _accepted_review_origin(task: TaskRun) -> bool:
        return any(record.event is TaskEvent.ACCEPT_DRAFT for record in task.transitions)

    def _apply_review_reconcile(
        self,
        stored: StoredTaskRun,
        result: object,
        lease: object,
    ) -> tuple[StoredTaskRun, TaskEvent | None]:
        task = stored.task_run
        draft = task.draft
        if type(draft) is not ReviewDraft:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        attention = _attention_event(result)
        if attention is not None:
            if (
                task.status is TaskStatus.RECOVERY_REQUIRED
                and attention is TaskEvent.REQUIRE_RECOVERY
            ):
                return (stored, None)
            if (
                task.status is TaskStatus.CLEANUP_REQUIRED
                and attention is TaskEvent.REQUIRE_CLEANUP
            ):
                return (stored, None)
            return (self._published_attention(stored, attention), None)
        try:
            durable_head = self._revision_store.load_head(task.project_id)
        except Exception:
            durable_head = None
        if type(result) is not CandidateReconcileResult:
            if task.status is TaskStatus.RECOVERY_REQUIRED:
                return (stored, None)
            return (
                self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY),
                None,
            )
        head = getattr(result, "head", None)
        live_binding = getattr(result, "live_binding", None)
        status = getattr(result, "status", None)
        stable_status = status in {
            CandidateReconcileStatus.CLEAN,
            CandidateReconcileStatus.COMMITTED,
            CandidateReconcileStatus.NOT_COMMITTED,
        }
        if not (
            stable_status
            and type(head) is ProjectHead
            and type(durable_head) is ProjectHead
            and head == durable_head
            and type(live_binding) is SessionBinding
            and live_binding.project_id == task.project_id
            and live_binding.revision_id == durable_head.revision_id
        ):
            if task.status is TaskStatus.RECOVERY_REQUIRED:
                return (stored, None)
            return (
                self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY),
                None,
            )
        base_head = self._draft_head(draft)
        if durable_head != base_head and self._accepted_review_origin(task):
            return (self._apply_reconcile(stored, result), None)
        if durable_head != base_head:
            if task.status is TaskStatus.RECOVERY_REQUIRED:
                return (stored, None)
            return (
                self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY),
                None,
            )
        if not self._draft_is_intact(task, draft):
            if task.status is TaskStatus.RECOVERY_REQUIRED:
                return (stored, None)
            return (
                self._review_attention(
                    stored,
                    TaskEvent.REQUIRE_RECOVERY,
                    integrity=True,
                ),
                None,
            )
        if task.status is TaskStatus.PREPARING_REVIEW:
            if not self._review_journal_is_terminal_uncommitted(
                task,
                draft,
                result,
                durable_head,
            ):
                return (
                    self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY),
                    None,
                )
            return (stored, TaskEvent.PUBLISH_DRAFT)
        if task.status is TaskStatus.ACCEPTING_DRAFT:
            if not self._review_base_transaction_is_terminal(
                task,
                result,
                durable_head,
            ):
                return (
                    self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY),
                    None,
                )
            return (self._accept_with_lease(stored, base_head, lease), None)
        if task.status in {TaskStatus.RECOVERY_REQUIRED, TaskStatus.CLEANUP_REQUIRED}:
            if not self._review_base_has_terminal_uncommitted(
                task,
                result,
                durable_head,
            ):
                if task.status is TaskStatus.RECOVERY_REQUIRED:
                    return (stored, None)
                return (
                    self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY),
                    None,
                )
            return (stored, TaskEvent.CONFIRM_DRAFT_UNCOMMITTED)
        return (
            self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY),
            None,
        )

    @staticmethod
    def _review_journal_is_terminal_uncommitted(
        task: TaskRun,
        draft: ReviewDraft,
        result: CandidateReconcileResult,
        durable_head: ProjectHead,
    ) -> bool:
        reconciliation = result.reconciliation
        journal = reconciliation.journal if type(reconciliation) is ReconciliationResult else None
        return (
            TaskService._review_base_has_terminal_uncommitted(
                task,
                result,
                durable_head,
            )
            and type(journal) is CommitJournal
            and journal.candidate_revision == draft.revision_id
            and journal.manifest_sha256 == draft.manifest_sha256
        )

    @staticmethod
    def _review_base_has_terminal_uncommitted(
        task: TaskRun,
        result: CandidateReconcileResult,
        durable_head: ProjectHead,
    ) -> bool:
        reconciliation = result.reconciliation
        journal = reconciliation.journal if type(reconciliation) is ReconciliationResult else None
        return (
            result.status is CandidateReconcileStatus.NOT_COMMITTED
            and result.head == durable_head
            and result.head_committed is False
            and result.slot_promoted is False
            and result.cleanup_required is False
            and result.recovery_required is False
            and result.cleanup_binding is None
            and type(result.live_binding) is SessionBinding
            and result.live_binding.project_id == task.project_id
            and result.live_binding.revision_id == durable_head.revision_id
            and type(reconciliation) is ReconciliationResult
            and reconciliation.project_id == task.project_id
            and reconciliation.status is ReconciliationStatus.NOT_COMMITTED
            and reconciliation.head == durable_head
            and type(journal) is CommitJournal
            and journal.project_id == task.project_id
            and journal.expected_head == durable_head
            and journal.state is CommitJournalState.NOT_COMMITTED
        )

    @staticmethod
    def _review_base_transaction_is_terminal(
        task: TaskRun,
        result: CandidateReconcileResult,
        durable_head: ProjectHead,
    ) -> bool:
        reconciliation = result.reconciliation
        if not (
            type(reconciliation) is ReconciliationResult
            and reconciliation.project_id == task.project_id
            and reconciliation.head == durable_head
        ):
            return False
        journal = reconciliation.journal
        if result.status is CandidateReconcileStatus.CLEAN:
            return (
                reconciliation.status is ReconciliationStatus.CLEAN
                and journal is None
                and result.head_committed is True
                and result.slot_promoted is True
            )
        if type(journal) is not CommitJournal:
            return False
        if not (
            journal.project_id == task.project_id
            and journal.state
            in {
                CommitJournalState.NOT_COMMITTED,
                CommitJournalState.COMMITTED,
            }
        ):
            return False
        if result.status is CandidateReconcileStatus.NOT_COMMITTED:
            return (
                reconciliation.status is ReconciliationStatus.NOT_COMMITTED
                and journal.state is CommitJournalState.NOT_COMMITTED
                and journal.expected_head == durable_head
                and result.head_committed is False
                and result.slot_promoted is False
            )
        if result.status is CandidateReconcileStatus.COMMITTED:
            return (
                reconciliation.status is ReconciliationStatus.COMMITTED
                and journal.state is CommitJournalState.COMMITTED
                and journal.candidate_revision == durable_head.revision_id
                and journal.manifest_sha256 == durable_head.manifest_sha256
                and result.head_committed is True
                and result.slot_promoted is True
            )
        return False

    def _load(self, task_id: str) -> StoredTaskRun:
        return _catalog_call(lambda: self._catalog.get_task(task_id=task_id))

    def _load_expected(self, task_id: str, generation: object) -> StoredTaskRun:
        return _catalog_call(lambda: self._catalog.load_expected(task_id, generation))

    def _cas(self, stored: StoredTaskRun, task: TaskRun) -> StoredTaskRun:
        return _catalog_call(lambda: self._catalog.compare_and_set(stored, task))

    def _reject_validation(self, stored: StoredTaskRun) -> StoredTaskRun:
        try:
            validating = self._cas(
                stored,
                transition_task(stored.task_run, TaskEvent.START_VALIDATION),
            )
            return self._cas(
                validating,
                transition_task(
                    validating.task_run,
                    TaskEvent.REJECT_PROGRAM,
                    error=_VALIDATION_ERROR,
                ),
            )
        except TaskServiceError:
            raise
        except TaskStateError:
            _raise(TaskServiceErrorCode.INVALID_STATE)

    def _continue_preflighted(
        self,
        stored: StoredTaskRun,
        compiled: CompiledAcceptance,
        validated: object,
        *,
        submitted: TaskRun | None = None,
    ) -> StoredTaskRun:
        project_id = stored.task_run.project_id
        lease = self._acquire(project_id)

        result: StoredTaskRun | None = None
        caught: TaskServiceError | None = None
        try:
            try:
                head = self._guard_runtime_head(project_id)
                current = stored
                if submitted is not None:
                    current = self._cas(stored, submitted)
                validating = self._cas(
                    current,
                    transition_task(current.task_run, TaskEvent.START_VALIDATION),
                )
                result = self._run_with_lease(
                    validating,
                    compiled,
                    validated,
                    lease,
                    head,
                )
            except TaskServiceError as error:
                caught = error
            except TaskStateError:
                caught = TaskServiceError(TaskServiceErrorCode.INVALID_STATE)
        finally:
            release_failed = self._release(lease)
        if release_failed:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
        if caught is not None:
            raise caught from None
        assert result is not None
        if result.task_run.status is TaskStatus.PREPARING_REVIEW:
            try:
                return self._cas(
                    result,
                    transition_task(result.task_run, TaskEvent.PUBLISH_DRAFT),
                )
            except TaskStateError:
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return result

    @staticmethod
    def _draft_head(draft: ReviewDraft) -> ProjectHead:
        if type(draft) is not ReviewDraft:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        try:
            return ProjectHead(
                project_id=draft.project_id,
                generation=draft.base_generation,
                revision_id=draft.base_revision,
                manifest_sha256=draft.base_manifest_sha256,
            )
        except Exception:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

    @staticmethod
    def _draft_report(task: TaskRun, draft: ReviewDraft) -> object:
        reports = tuple(
            report for report in task.verification_reports if report.id == draft.verification_id
        )
        if len(reports) != 1:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        report = reports[0]
        if not (
            report.passed
            and report.acceptance_id == draft.acceptance_id
            and report.candidate_revision == draft.revision_id
            and report.manifest_sha256 == draft.manifest_sha256
            and report.observation_digest == draft.observation_digest
        ):
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return report

    def _draft_is_intact(self, task: TaskRun, draft: ReviewDraft) -> bool:
        try:
            self._draft_report(task, draft)
            revision = self._revision_store.load_revision(
                task.project_id,
                draft.revision_id,
            )
        except Exception:
            return False
        if not (
            type(revision) is RevisionRef
            and revision.project_id == task.project_id
            and revision.id == draft.revision_id
            and revision.base_revision == draft.base_revision
            and revision.manifest_sha256 == draft.manifest_sha256
            and revision.model is not None
            and type(revision.artifacts) is tuple
        ):
            return False
        revision_artifacts = (revision.model,) + revision.artifacts
        if len(revision_artifacts) != len(task.artifacts):
            return False
        return all(
            task_artifact.candidate_revision == draft.revision_id
            and task_artifact.id == revision_artifact.id
            and task_artifact.name == revision_artifact.name
            and task_artifact.format == revision_artifact.format
            and task_artifact.sha256 == revision_artifact.sha256
            and task_artifact.size_bytes == revision_artifact.size_bytes
            for task_artifact, revision_artifact in zip(
                task.artifacts,
                revision_artifacts,
                strict=True,
            )
        )

    def _review_is_durably_detached(
        self,
        task: TaskRun,
        draft: ReviewDraft,
        published: object,
    ) -> bool:
        """Require one exact, terminal, read-backed detach fact before review."""

        if type(published) is not CandidateRollbackResult:
            return False
        reconciliation = published.reconciliation
        journal = reconciliation.journal if type(reconciliation) is ReconciliationResult else None
        try:
            durable_head = self._revision_store.load_head(task.project_id)
        except Exception:
            return False
        return (
            published.status is CandidateRollbackStatus.NOT_COMMITTED
            and published.head == self._draft_head(draft)
            and published.head == durable_head
            and published.head_committed is False
            and published.slot_promoted is False
            and published.cleanup_required is False
            and published.recovery_required is False
            and published.cleanup_binding is None
            and type(published.live_binding) is SessionBinding
            and published.live_binding.project_id == task.project_id
            and published.live_binding.revision_id == draft.base_revision
            and type(reconciliation) is ReconciliationResult
            and reconciliation.project_id == task.project_id
            and reconciliation.status is ReconciliationStatus.NOT_COMMITTED
            and reconciliation.head == durable_head
            and journal is not None
            and journal.project_id == task.project_id
            and journal.expected_head == durable_head
            and journal.candidate_revision == draft.revision_id
            and journal.manifest_sha256 == draft.manifest_sha256
            and journal.state is CommitJournalState.NOT_COMMITTED
            and self._draft_is_intact(task, draft)
        )

    def _review_attention(
        self,
        stored: StoredTaskRun,
        event: TaskEvent,
        *,
        integrity: bool = False,
    ) -> StoredTaskRun:
        error = _REVIEW_INTEGRITY_ERROR if integrity else _attention_error(event)
        try:
            return self._cas(
                stored,
                transition_task(stored.task_run, event, error=error),
            )
        except (TaskServiceError, TaskStateError):
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

    def _accept_with_lease(
        self,
        accepting: StoredTaskRun,
        expected_head: ProjectHead,
        lease: object,
    ) -> StoredTaskRun:
        task = accepting.task_run
        draft = task.draft
        if (
            task.status is not TaskStatus.ACCEPTING_DRAFT
            or type(draft) is not ReviewDraft
            or task.program is None
        ):
            _raise(TaskServiceErrorCode.INVALID_STATE)
        expected_report = self._draft_report(task, draft)
        try:
            revision = self._revision_store.load_revision(task.project_id, draft.revision_id)
        except Exception:
            return self._review_attention(
                accepting,
                TaskEvent.REQUIRE_RECOVERY,
                integrity=True,
            )
        if not (
            type(revision) is RevisionRef
            and revision.project_id == task.project_id
            and revision.id == draft.revision_id
            and revision.base_revision == draft.base_revision
            and revision.manifest_sha256 == draft.manifest_sha256
        ):
            return self._review_attention(
                accepting,
                TaskEvent.REQUIRE_RECOVERY,
                integrity=True,
            )

        review_candidate: object | None = None
        prepared = False
        try:
            review_candidate = self._coordinator.reopen_review(
                project_id=task.project_id,
                base_head=expected_head,
                revision=revision,
                lease=lease,
            )
            evidence = self._executor.collect_evidence(candidate=review_candidate)
            if type(evidence) is not CandidateEvidence or evidence.artifacts != task.artifacts:
                raise ValueError
            compiled = compile_acceptance_spec(task.program.acceptance)
            verification = verify_acceptance(
                compiled,
                evidence.snapshot,
                candidate_revision=revision.id,
                manifest_sha256=revision.manifest_sha256,
            )
            if (
                type(verification) is not VerificationResult
                or not verification.report.passed
                or verification.report != expected_report
                or verification.receipt is None
            ):
                raise ValueError
            review_candidate = self._coordinator.prepare_review(
                candidate=review_candidate,
                lease=lease,
            )
            if getattr(review_candidate, "revision", None) != revision:
                raise ValueError
            prepared = True
            result = self._coordinator.commit(
                candidate=review_candidate,
                receipt=verification.receipt,
                compiled=compiled,
                snapshot=evidence.snapshot,
                lease=lease,
            )
        except Exception:
            if not prepared:
                if review_candidate is not None:
                    try:
                        self._coordinator.discard_review(
                            candidate=review_candidate,
                            lease=lease,
                        )
                    except Exception as error:
                        event = _attention_event(error) or TaskEvent.REQUIRE_RECOVERY
                        return self._review_attention(accepting, event, integrity=True)
                return self._review_attention(
                    accepting,
                    TaskEvent.REQUIRE_RECOVERY,
                    integrity=True,
                )
            return self._post_receipt_attention(accepting)
        return self._finish_commit(accepting, review_candidate, result)

    def _run_with_lease(
        self,
        validating: StoredTaskRun,
        compiled: CompiledAcceptance,
        validated: object,
        lease: object,
        head: ProjectHead,
    ) -> StoredTaskRun:
        task = validating.task_run
        if head.revision_id != task.base_revision:
            return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)

        try:
            active = self._coordinator.begin(
                project_id=task.project_id,
                expected_head=head,
                lease=lease,
            )
        except CandidateError as error:
            event = _attention_event(error)
            if event is not None:
                return self._persist_attention(validating, event)
            if error.code is CandidateErrorCode.CONFLICT:
                return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)
            return self._reject_pre_candidate(validating, _BEGIN_FAILURE)
        except Exception:
            return self._reject_pre_candidate(validating, _BEGIN_FAILURE)

        try:
            revision_id = active.binding.revision_id
            executing = self._cas(
                validating,
                transition_task(
                    validating.task_run,
                    TaskEvent.VALIDATE_PROGRAM,
                    candidate_revision=revision_id,
                ),
            )
        except (TaskServiceError, TaskStateError) as error:
            return self._abort_unpublished(validating, active, lease, error)

        current = executing
        current_candidate = active
        try:
            outcomes = self._executor.execute_program(
                program=validated,
                candidate=active,
            )
            if type(outcomes) is not tuple or not all(
                type(item) is NormalizedToolOutcome for item in outcomes
            ):
                raise TypeError
            for outcome in outcomes:
                try:
                    current = self._cas(
                        current,
                        append_step_result(current.task_run, outcome.result),
                    )
                except (TaskServiceError, TaskStateError) as error:
                    return self._fail_published(
                        current,
                        current_candidate,
                        lease,
                        _EXECUTION_FAILURE,
                        error,
                    )
                if not outcome.result.ok:
                    error = outcome.result.error or _EXECUTION_FAILURE
                    return self._fail_published(current, current_candidate, lease, error, error)

            current_candidate = self._coordinator.checkpoint(
                candidate=current_candidate,
                lease=lease,
            )
            self._executor.export_step(candidate=current_candidate, lease=lease)
            current_candidate = self._coordinator.seal(
                candidate=current_candidate,
                lease=lease,
            )
            evidence = self._executor.collect_evidence(candidate=current_candidate)
            if type(evidence) is not CandidateEvidence:
                raise TypeError
            for artifact in evidence.artifacts:
                current = self._cas(current, append_artifact(current.task_run, artifact))
            current = self._cas(
                current,
                transition_task(current.task_run, TaskEvent.COMPLETE_EXECUTION),
            )
        except Exception as error:
            return self._fail_published(
                current,
                current_candidate,
                lease,
                _EXECUTION_FAILURE,
                error,
            )

        try:
            verification = verify_acceptance(
                compiled,
                evidence.snapshot,
                candidate_revision=current_candidate.revision.id,
                manifest_sha256=current_candidate.revision.manifest_sha256,
            )
            if type(verification) is not VerificationResult:
                raise TypeError
        except Exception as error:
            return self._fail_published(
                current,
                current_candidate,
                lease,
                _VERIFICATION_FAILURE,
                error,
            )

        if not verification.report.passed:
            try:
                current = self._cas(
                    current,
                    append_verification(current.task_run, verification.report),
                )
            except (TaskServiceError, TaskStateError) as error:
                return self._fail_published(
                    current,
                    current_candidate,
                    lease,
                    _VERIFICATION_FAILURE,
                    error,
                )
            return self._fail_published(
                current,
                current_candidate,
                lease,
                _VERIFICATION_FAILURE,
                _VERIFICATION_FAILURE,
            )

        if current.task_run.review_policy is ReviewPolicy.REQUIRE_REVIEW:
            revision = current_candidate.revision
            draft = ReviewDraft(
                id=f"draft_{revision.id.removeprefix('revision_')}",
                task_id=current.task_run.id,
                project_id=current.task_run.project_id,
                base_revision=head.revision_id,
                base_generation=head.generation,
                base_manifest_sha256=head.manifest_sha256,
                revision_id=revision.id,
                manifest_sha256=revision.manifest_sha256,
                verification_id=verification.report.id,
                acceptance_id=verification.report.acceptance_id,
                observation_digest=verification.report.observation_digest,
            )
            try:
                preparing = self._cas(
                    current,
                    transition_task(
                        current.task_run,
                        TaskEvent.PREPARE_REVIEW,
                        verification=verification.report,
                        draft=draft,
                    ),
                )
            except (TaskServiceError, TaskStateError) as error:
                return self._fail_published(
                    current,
                    current_candidate,
                    lease,
                    _VERIFICATION_FAILURE,
                    error,
                )
            try:
                published = self._coordinator.publish_review(
                    candidate=current_candidate,
                    receipt=verification.receipt,
                    compiled=compiled,
                    snapshot=evidence.snapshot,
                    lease=lease,
                )
            except Exception as error:
                event = _attention_event(error) or TaskEvent.REQUIRE_RECOVERY
                return self._review_attention(preparing, event)
            if not self._review_is_durably_detached(
                preparing.task_run,
                draft,
                published,
            ):
                event = _attention_event(published) or TaskEvent.REQUIRE_RECOVERY
                return self._review_attention(preparing, event)
            return preparing

        try:
            committing = self._cas(
                current,
                transition_task(
                    current.task_run,
                    TaskEvent.PASS_VERIFICATION,
                    verification=verification.report,
                ),
            )
        except (TaskServiceError, TaskStateError) as error:
            return self._fail_published(
                current,
                current_candidate,
                lease,
                _VERIFICATION_FAILURE,
                error,
            )

        try:
            commit_result = self._coordinator.commit(
                candidate=current_candidate,
                receipt=verification.receipt,
                compiled=compiled,
                snapshot=evidence.snapshot,
                lease=lease,
            )
        except Exception:
            return self._post_receipt_attention(committing)
        return self._finish_commit(committing, current_candidate, commit_result)

    def _reject_pre_candidate(
        self,
        stored: StoredTaskRun,
        error: StepError,
    ) -> StoredTaskRun:
        try:
            rejected = transition_task(
                stored.task_run,
                TaskEvent.REJECT_PROGRAM,
                error=error,
            )
            return self._cas(stored, rejected)
        except TaskStateError:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

    def _persist_attention(
        self,
        stored: StoredTaskRun,
        event: TaskEvent,
    ) -> StoredTaskRun:
        try:
            return self._cas(
                stored,
                transition_task(
                    stored.task_run,
                    event,
                    error=_attention_error(event),
                ),
            )
        except TaskStateError:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

    def _abort_unpublished(
        self,
        validating: StoredTaskRun,
        candidate: object,
        lease: object,
        cause: object,
    ) -> StoredTaskRun:
        del cause
        event: TaskEvent | None = None
        try:
            rollback = self._coordinator.rollback(candidate=candidate, lease=lease)
            event = _attention_event(rollback)
            if event is None and getattr(rollback, "status", None) is not (
                CandidateRollbackStatus.NOT_COMMITTED
            ):
                event = TaskEvent.REQUIRE_RECOVERY
        except CandidateError as error:
            event = _attention_event(error) or TaskEvent.REQUIRE_RECOVERY
        except Exception:
            event = TaskEvent.REQUIRE_RECOVERY

        try:
            latest = self._load(validating.task_run.id)
        except TaskServiceError:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        if (
            latest.task_run.status is not TaskStatus.VALIDATING_PROGRAM
            or latest.task_run.candidate_revision is not None
            or latest.task_run.program != validating.task_run.program
        ):
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        if event is not None:
            return self._persist_attention(latest, event)
        return self._reject_pre_candidate(latest, _PRE_CANDIDATE_CONFLICT)

    def _fail_published(
        self,
        stored: StoredTaskRun,
        candidate: object,
        lease: object,
        error: StepError,
        cause: object,
    ) -> StoredTaskRun:
        try:
            latest = self._load(stored.task_run.id)
        except TaskServiceError:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        if latest.generation < stored.generation:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        current = latest
        try:
            if current.task_run.status is TaskStatus.EXECUTING:
                current = self._cas(
                    current,
                    transition_task(
                        current.task_run,
                        TaskEvent.FAIL_EXECUTION,
                        error=error,
                    ),
                )
            elif current.task_run.status is TaskStatus.VERIFYING:
                current = self._cas(
                    current,
                    transition_task(
                        current.task_run,
                        TaskEvent.FAIL_VERIFICATION,
                        error=error,
                    ),
                )
            elif current.task_run.status is not TaskStatus.ROLLING_BACK:
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        except (TaskServiceError, TaskStateError):
            return self._published_attention(current, TaskEvent.REQUIRE_RECOVERY)

        rollback_event: TaskEvent | None = None
        rollback_clean = False
        try:
            rollback = self._coordinator.rollback(candidate=candidate, lease=lease)
            rollback_event = _attention_event(rollback)
            rollback_clean = getattr(rollback, "status", None) is (
                CandidateRollbackStatus.NOT_COMMITTED
            )
        except CandidateError as rollback_error:
            rollback_event = _attention_event(rollback_error)
            if rollback_error.code is CandidateErrorCode.ALREADY_TERMINAL and isinstance(
                cause, CandidateError
            ):
                rollback_event = _attention_event(cause)
                rollback_clean = rollback_event is None
        except Exception:
            rollback_event = TaskEvent.REQUIRE_RECOVERY

        if rollback_clean:
            try:
                return self._cas(
                    current,
                    transition_task(current.task_run, TaskEvent.COMPLETE_ROLLBACK),
                )
            except (TaskServiceError, TaskStateError):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return self._published_attention(
            current,
            rollback_event or TaskEvent.REQUIRE_RECOVERY,
        )

    def _published_attention(
        self,
        stored: StoredTaskRun,
        event: TaskEvent,
    ) -> StoredTaskRun:
        try:
            return self._cas(
                stored,
                transition_task(
                    stored.task_run,
                    event,
                    error=_attention_error(event),
                ),
            )
        except (TaskServiceError, TaskStateError):
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

    def _post_receipt_attention(self, committing: StoredTaskRun) -> StoredTaskRun:
        try:
            return self._cas(
                committing,
                transition_task(
                    committing.task_run,
                    TaskEvent.REQUIRE_RECOVERY,
                    error=_RECOVERY_ERROR,
                ),
            )
        except (TaskServiceError, TaskStateError):
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

    def _finish_commit(
        self,
        committing: StoredTaskRun,
        candidate: object,
        result: object,
    ) -> StoredTaskRun:
        candidate_revision = candidate.revision.id
        candidate_manifest = candidate.revision.manifest_sha256
        head = getattr(result, "head", None)
        try:
            durable_head = self._revision_store.load_head(committing.task_run.project_id)
        except Exception:
            durable_head = None
        if (
            type(head) is not ProjectHead
            or type(durable_head) is not ProjectHead
            or durable_head != head
            or not bool(getattr(result, "head_committed", False))
            or head.project_id != committing.task_run.project_id
            or head.revision_id != candidate_revision
            or head.manifest_sha256 != candidate_manifest
        ):
            return self._post_receipt_attention(committing)
        if getattr(result, "slot_promoted", None) is True:
            self._runtime_head = head
        else:
            self._runtime_stale = True
        status = getattr(result, "status", None)
        attention = _attention_event(result)
        try:
            if (
                status is CandidateCommitStatus.COMMITTED
                and attention is None
                and getattr(result, "slot_promoted", None) is True
            ):
                task = transition_task(
                    committing.task_run,
                    TaskEvent.COMMIT,
                    committed_revision=candidate_revision,
                )
            elif attention is TaskEvent.REQUIRE_CLEANUP:
                task = transition_task(
                    committing.task_run,
                    TaskEvent.REQUIRE_CLEANUP,
                    error=_CLEANUP_ERROR,
                )
            else:
                task = transition_task(
                    committing.task_run,
                    TaskEvent.REQUIRE_RECOVERY,
                    error=_RECOVERY_ERROR,
                )
            return self._cas(committing, task)
        except (TaskServiceError, TaskStateError):
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

    def _apply_reconcile(
        self,
        stored: StoredTaskRun,
        result: object,
    ) -> StoredTaskRun:
        task = stored.task_run
        status = getattr(result, "status", None)
        head = getattr(result, "head", None)
        attention = _attention_event(result)
        if attention is TaskEvent.REQUIRE_RECOVERY:
            if task.status is TaskStatus.RECOVERY_REQUIRED:
                return stored
            return self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY)
        if attention is TaskEvent.REQUIRE_CLEANUP:
            if task.status in {TaskStatus.CLEANUP_REQUIRED, TaskStatus.RECOVERY_REQUIRED}:
                return stored
            return self._published_attention(stored, TaskEvent.REQUIRE_CLEANUP)

        try:
            durable_head = self._revision_store.load_head(task.project_id)
        except Exception:
            durable_head = None
        head_is_durable = (
            type(head) is ProjectHead
            and type(durable_head) is ProjectHead
            and head == durable_head
            and head.project_id == task.project_id
        )
        live_binding = getattr(result, "live_binding", None)
        session_is_durable = (
            type(live_binding) is SessionBinding
            and type(durable_head) is ProjectHead
            and live_binding.project_id == task.project_id
            and live_binding.revision_id == durable_head.revision_id
        )
        if task.candidate_revision is None:
            if status not in {
                CandidateReconcileStatus.CLEAN,
                CandidateReconcileStatus.NOT_COMMITTED,
            } or not (
                head_is_durable and session_is_durable and head.revision_id == task.base_revision
            ):
                if task.status is TaskStatus.CLEANUP_REQUIRED:
                    return self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY)
                return stored
            try:
                return self._cas(
                    stored,
                    transition_task(task, TaskEvent.CONFIRM_PRE_CANDIDATE),
                )
            except (TaskServiceError, TaskStateError):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

        stable = (
            status
            in {
                CandidateReconcileStatus.CLEAN,
                CandidateReconcileStatus.COMMITTED,
                CandidateReconcileStatus.NOT_COMMITTED,
            }
            and session_is_durable
        )
        lineage, candidate_ref = self._candidate_lineage(task, durable_head)
        committed = (
            stable
            and head_is_durable
            and lineage is _CandidateLineage.COMMITTED
            and type(candidate_ref) is RevisionRef
            and any(
                report.passed
                and report.candidate_revision == task.candidate_revision
                and report.manifest_sha256 == candidate_ref.manifest_sha256
                for report in task.verification_reports
            )
        )
        if committed:
            try:
                event = (
                    TaskEvent.COMMIT
                    if task.status is TaskStatus.COMMITTING
                    else TaskEvent.CONFIRM_COMMITTED
                )
                return self._cas(
                    stored,
                    transition_task(
                        task,
                        event,
                        committed_revision=task.candidate_revision,
                    ),
                )
            except (TaskServiceError, TaskStateError):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

        if stable and head_is_durable and lineage is _CandidateLineage.NOT_COMMITTED:
            try:
                if task.status in {TaskStatus.RECOVERY_REQUIRED, TaskStatus.CLEANUP_REQUIRED}:
                    rolling = self._cas(
                        stored,
                        transition_task(task, TaskEvent.CONFIRM_UNCOMMITTED),
                    )
                elif task.status is TaskStatus.ROLLING_BACK:
                    rolling = stored
                else:
                    return self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY)
                return self._cas(
                    rolling,
                    transition_task(rolling.task_run, TaskEvent.COMPLETE_ROLLBACK),
                )
            except (TaskServiceError, TaskStateError):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        if task.status is TaskStatus.RECOVERY_REQUIRED:
            return stored
        return self._published_attention(stored, TaskEvent.REQUIRE_RECOVERY)

    def _candidate_lineage(
        self,
        task: TaskRun,
        durable_head: object,
    ) -> tuple[_CandidateLineage, RevisionRef | None]:
        candidate_revision = task.candidate_revision
        if (
            type(durable_head) is not ProjectHead
            or candidate_revision is None
            or candidate_revision == task.base_revision
        ):
            return (_CandidateLineage.UNKNOWN, None)
        current_revision = durable_head.revision_id
        visited: set[str] = set()
        candidate_ref: RevisionRef | None = None
        for _ in range(_MAX_RECONCILE_LINEAGE_DEPTH):
            if current_revision in visited:
                return (_CandidateLineage.UNKNOWN, None)
            visited.add(current_revision)
            try:
                revision = self._revision_store.load_revision(
                    task.project_id,
                    current_revision,
                )
            except Exception:
                return (_CandidateLineage.UNKNOWN, None)
            if not (
                type(revision) is RevisionRef
                and revision.id == current_revision
                and revision.project_id == task.project_id
            ):
                return (_CandidateLineage.UNKNOWN, None)
            if current_revision == durable_head.revision_id and (
                revision.manifest_sha256 != durable_head.manifest_sha256
            ):
                return (_CandidateLineage.UNKNOWN, None)
            if current_revision == task.base_revision:
                if candidate_ref is not None:
                    return (_CandidateLineage.COMMITTED, candidate_ref)
                return (_CandidateLineage.NOT_COMMITTED, None)
            if current_revision == candidate_revision:
                if revision.base_revision != task.base_revision:
                    return (_CandidateLineage.UNKNOWN, None)
                candidate_ref = revision
            current_revision = revision.base_revision
        return (_CandidateLineage.UNKNOWN, None)

    def _acquire(self, project_id: str) -> object:
        lease: object | None = None
        failed = False
        try:
            lease = self._lease_manager.acquire_project_write(project_id)
        except LeaseError:
            failed = True
        except Exception:
            failed = True
        if failed or lease is None:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
        return lease

    @staticmethod
    def _release(lease: Any) -> bool:
        try:
            lease.release(owner_token=lease.owner_token)
        except Exception:
            return True
        return False
