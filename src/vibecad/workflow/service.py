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
    CandidateReconcileStatus,
    CandidateRollbackStatus,
    SessionBinding,
)
from vibecad.execution.executor import CandidateEvidence, InProcessCadExecutor
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.execution.revisions import LocalRevisionStore, ProjectHead, RevisionRef
from vibecad.validation import (
    CompiledAcceptance,
    VerificationResult,
    compile_acceptance_spec,
    verify_acceptance,
)
from vibecad.workflow.contracts import ErrorCategory, ModelProgram, StepError
from vibecad.workflow.lease import LeaseError, ResourceLeaseManager
from vibecad.workflow.state import (
    ReasoningOwner,
    TaskEvent,
    TaskRun,
    TaskStateError,
    TaskStatus,
    append_artifact,
    append_step_result,
    append_verification,
    new_task_run,
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


def _raise(code: TaskServiceErrorCode) -> None:
    raise TaskServiceError(code)


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
        "_coordinator",
        "_executor",
        "_lease_manager",
        "_revision_store",
        "_task_store",
    )

    def __init__(
        self,
        *,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
        lease_manager: ResourceLeaseManager,
        coordinator: CandidateCoordinator,
        executor: InProcessCadExecutor,
    ) -> None:
        if not (
            isinstance(task_store, TaskRunStore)
            and isinstance(revision_store, LocalRevisionStore)
            and isinstance(lease_manager, ResourceLeaseManager)
            and isinstance(coordinator, CandidateCoordinator)
            and isinstance(executor, InProcessCadExecutor)
        ):
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        if not (
            getattr(task_store, "_lease_manager", None) is lease_manager
            and getattr(revision_store, "_lease_manager", None) is lease_manager
            and getattr(coordinator, "_store", None) is revision_store
            and getattr(coordinator, "_snapshot_port", None) is executor
            and getattr(executor, "_store", None) is revision_store
        ):
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        self._task_store = task_store
        self._revision_store = revision_store
        self._lease_manager = lease_manager
        self._coordinator = coordinator
        self._executor = executor

    def create_task(
        self,
        *,
        task_id: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
    ) -> StoredTaskRun:
        if type(reasoning_owner) is not ReasoningOwner:
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        if reasoning_owner is not ReasoningOwner.EXTERNAL_PLAN:
            _raise(TaskServiceErrorCode.UNSUPPORTED_REASONING_OWNER)
        task: TaskRun | None = None
        stored: StoredTaskRun | None = None
        failure: TaskServiceErrorCode | None = None
        uncertain_generation: int | None = None
        try:
            head = self._revision_store.load_head(project_id)
            if type(head) is not ProjectHead or head.project_id != project_id:
                failure = TaskServiceErrorCode.STORE_FAILURE
                head = None
            if head is None:
                raise RuntimeError
            task = new_task_run(
                task_id=task_id,
                project_id=project_id,
                base_revision=head.revision_id,
                reasoning_owner=reasoning_owner,
            )
            task = transition_task(task, TaskEvent.REQUEST_PLAN)
            stored = self._task_store.create(task)
        except TaskStoreError as error:
            if error.code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
                uncertain_generation = getattr(error, "committed_generation", None)
            elif error.code is TaskStoreErrorCode.ALREADY_EXISTS:
                failure = TaskServiceErrorCode.CONFLICT
            elif error.code is TaskStoreErrorCode.INVALID_ID:
                failure = TaskServiceErrorCode.INVALID_INPUT
            else:
                failure = TaskServiceErrorCode.STORE_FAILURE
        except TaskStateError:
            failure = TaskServiceErrorCode.INVALID_INPUT
        except Exception:
            failure = failure or TaskServiceErrorCode.STORE_FAILURE
        if uncertain_generation is not None and task is not None:
            try:
                readback = self._task_store.load(task.id)
            except Exception:
                readback = None
            if (
                uncertain_generation == 0
                and type(readback) is StoredTaskRun
                and readback.generation == 0
                and readback.task_run == task
            ):
                return readback
            failure = TaskServiceErrorCode.STORE_FAILURE
        if failure is not None:
            _raise(failure)
        assert stored is not None
        return stored

    def get_task(self, *, task_id: str) -> StoredTaskRun:
        return self._load(task_id)

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
        stored = self._cas(stored, submitted)
        if preflight is None:
            return self._reject_validation(stored)
        compiled, validated = preflight
        return self._continue_preflighted(stored, compiled, validated)

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
            TaskStatus.ROLLING_BACK,
            TaskStatus.RECOVERY_REQUIRED,
            TaskStatus.CLEANUP_REQUIRED,
        }:
            _raise(TaskServiceErrorCode.INVALID_STATE)
        lease = self._acquire(stored.task_run.project_id)
        result: object | None = None
        caught: TaskServiceError | None = None
        try:
            try:
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
        return stored

    def _load(self, task_id: str) -> StoredTaskRun:
        stored: StoredTaskRun | None = None
        failure: TaskServiceErrorCode | None = None
        try:
            stored = self._task_store.load(task_id)
        except TaskStoreError as error:
            if error.code is TaskStoreErrorCode.NOT_FOUND:
                failure = TaskServiceErrorCode.NOT_FOUND
            elif error.code is TaskStoreErrorCode.INVALID_ID:
                failure = TaskServiceErrorCode.INVALID_INPUT
            else:
                failure = TaskServiceErrorCode.STORE_FAILURE
        except Exception:
            failure = TaskServiceErrorCode.STORE_FAILURE
        if failure is not None:
            _raise(failure)
        if type(stored) is not StoredTaskRun:
            _raise(TaskServiceErrorCode.STORE_FAILURE)
        return stored

    def _load_expected(self, task_id: str, generation: object) -> StoredTaskRun:
        expected = _expected_generation(generation)
        stored = self._load(task_id)
        if stored.generation != expected:
            _raise(TaskServiceErrorCode.CONFLICT)
        return stored

    def _cas(self, stored: StoredTaskRun, task: TaskRun) -> StoredTaskRun:
        result: StoredTaskRun | None = None
        failure: TaskServiceErrorCode | None = None
        uncertain_generation: int | None = None
        try:
            result = self._task_store.compare_and_set(
                stored.task_run.id,
                stored.generation,
                task,
            )
        except TaskStoreError as error:
            if error.code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
                uncertain_generation = getattr(error, "committed_generation", None)
            elif error.code is TaskStoreErrorCode.CONFLICT:
                failure = TaskServiceErrorCode.CONFLICT
            else:
                failure = TaskServiceErrorCode.STORE_FAILURE
        except Exception:
            failure = TaskServiceErrorCode.STORE_FAILURE
        if uncertain_generation is not None:
            try:
                readback = self._task_store.load(stored.task_run.id)
            except Exception:
                readback = None
            if (
                uncertain_generation == stored.generation + 1
                and type(readback) is StoredTaskRun
                and readback.generation == uncertain_generation
                and readback.task_run == task
            ):
                return readback
            failure = TaskServiceErrorCode.STORE_FAILURE
        if failure is not None:
            _raise(failure)
        if type(result) is not StoredTaskRun:
            _raise(TaskServiceErrorCode.STORE_FAILURE)
        return result

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
    ) -> StoredTaskRun:
        try:
            validating = self._cas(
                stored,
                transition_task(stored.task_run, TaskEvent.START_VALIDATION),
            )
        except TaskStateError:
            _raise(TaskServiceErrorCode.INVALID_STATE)

        try:
            lease = self._acquire(validating.task_run.project_id)
        except TaskServiceError:
            return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)

        result: StoredTaskRun | None = None
        caught: TaskServiceError | None = None
        try:
            try:
                result = self._run_with_lease(
                    validating,
                    compiled,
                    validated,
                    lease,
                )
            except TaskServiceError as error:
                caught = error
        finally:
            release_failed = self._release(lease)
        if release_failed:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
        if caught is not None:
            raise caught from None
        assert result is not None
        return result

    def _run_with_lease(
        self,
        validating: StoredTaskRun,
        compiled: CompiledAcceptance,
        validated: object,
        lease: object,
    ) -> StoredTaskRun:
        task = validating.task_run
        try:
            head = self._revision_store.load_head(task.project_id)
        except Exception:
            return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)
        if type(head) is not ProjectHead or head.revision_id != task.base_revision:
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
