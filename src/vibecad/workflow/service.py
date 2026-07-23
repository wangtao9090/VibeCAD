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
    CandidateReservationReconciliation,
    CandidateReservationStatus,
    CommitJournal,
    CommitJournalState,
    LocalRevisionStore,
    ProjectHead,
    ReconciliationResult,
    ReconciliationStatus,
    RevisionAncestrySnapshot,
    RevisionRef,
    RevisionSnapshotEntry,
    RevisionStoreError,
    RevisionStoreErrorCode,
    _candidate_file_limit,
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
from vibecad.workflow.revert import (
    BoundRevert,
    RevertProgramError,
    RevertProgramErrorCode,
    build_revert_binding,
    parse_bound_revert_task,
    require_matching_revert_task,
    revert_payload_matches_source,
    revert_task_identity,
)
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


_CANCELLATION_RESULT_STATUSES = frozenset(
    {
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLING,
        TaskStatus.CANCELLED,
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.CLEANUP_REQUIRED,
        TaskStatus.SUCCEEDED,
    }
)
_CANCELLATION_REQUEST_EVENTS = frozenset(
    {
        TaskEvent.REQUEST_CANCEL,
        TaskEvent.REQUEST_ACTIVE_CANCEL,
    }
)


def _task_has_event(task: TaskRun, event: TaskEvent) -> bool:
    return any(record.event is event for record in task.transitions)


def _has_cancellation_origin(task: TaskRun) -> bool:
    return task.status in _CANCELLATION_RESULT_STATUSES and any(
        record.event in _CANCELLATION_REQUEST_EVENTS for record in task.transitions
    )


def _has_started_cancellation(task: TaskRun) -> bool:
    return _has_cancellation_origin(task) and _task_has_event(
        task,
        TaskEvent.START_CANCELLATION,
    )


def _load_revert_source_from_store(
    revision_store: LocalRevisionStore,
    *,
    project_id: str,
    source_revision: str,
    head: ProjectHead,
) -> RevisionRef:
    if type(source_revision) is not str:
        _raise(TaskServiceErrorCode.INVALID_INPUT)
    if source_revision == head.revision_id:
        _raise(TaskServiceErrorCode.INVALID_INPUT)
    try:
        ancestry = revision_store.snapshot_revisions(project_id)
    except RevisionStoreError as error:
        if error.code is RevisionStoreErrorCode.NOT_FOUND:
            _raise(TaskServiceErrorCode.NOT_FOUND)
        _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
    except Exception:
        _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
    if not (
        type(ancestry) is RevisionAncestrySnapshot
        and ancestry.project_id == project_id
        and ancestry.head == head
        and type(ancestry.revisions) is tuple
    ):
        _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
    matches = tuple(
        entry
        for entry in ancestry.revisions
        if type(entry) is RevisionSnapshotEntry and entry.id == source_revision
    )
    if len(matches) != 1:
        _raise(TaskServiceErrorCode.NOT_FOUND)
    entry = matches[0]
    if entry.base_revision is None:
        _raise(TaskServiceErrorCode.INVALID_INPUT)
    try:
        source = revision_store.load_revision(project_id, source_revision)
    except RevisionStoreError as error:
        if error.code is RevisionStoreErrorCode.NOT_FOUND:
            _raise(TaskServiceErrorCode.NOT_FOUND)
        _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
    except Exception:
        _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
    if not (
        type(source) is RevisionRef
        and source.id == entry.id
        and source.project_id == entry.project_id == project_id
        and source.base_revision == entry.base_revision
        and source.manifest_sha256 == entry.manifest_sha256
        and source.model is not None
        and type(source.artifacts) is tuple
        and len(source.artifacts) == 1
    ):
        _raise(TaskServiceErrorCode.INVALID_INPUT)
    return source


def _cancellation_descends_from(before: StoredTaskRun, after: StoredTaskRun) -> bool:
    prior = before.task_run
    current = after.task_run
    return (
        after.generation > before.generation
        and _has_cancellation_origin(current)
        and prior.id == current.id
        and prior.project_id == current.project_id
        and prior.base_revision == current.base_revision
        and prior.reasoning_owner is current.reasoning_owner
        and prior.review_policy is current.review_policy
        and prior.creation_digest == current.creation_digest
        and prior.program == current.program
        and (
            prior.candidate_revision is None
            or prior.candidate_revision == current.candidate_revision
        )
        and (prior.draft is None or prior.draft == current.draft)
        and current.steps[: len(prior.steps)] == prior.steps
        and current.verification_reports[: len(prior.verification_reports)]
        == prior.verification_reports
        and current.artifacts[: len(prior.artifacts)] == prior.artifacts
        and current.transitions[: len(prior.transitions)] == prior.transitions
    )


def _durable_candidate_lineage(
    revision_store: LocalRevisionStore,
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
            revision = revision_store.load_revision(
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
        if revision.base_revision is None:
            return (_CandidateLineage.UNKNOWN, None)
        current_revision = revision.base_revision
    return (_CandidateLineage.UNKNOWN, None)


class TaskServiceErrorCode(StrEnum):
    """Stable failures returned by the internal orchestration boundary."""

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_REASONING_OWNER = "unsupported_reasoning_owner"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    STORE_FAILURE = "store_failure"
    LEASE_UNAVAILABLE = "lease_unavailable"
    RESOURCE_EXHAUSTED = "resource_exhausted"
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
    TaskServiceErrorCode.RESOURCE_EXHAUSTED: "The application resource capacity is exhausted.",
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
    TaskCatalogErrorCode.RESOURCE_EXHAUSTED: TaskServiceErrorCode.RESOURCE_EXHAUSTED,
    TaskCatalogErrorCode.RECOVERY_REQUIRED: TaskServiceErrorCode.RECOVERY_REQUIRED,
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
    if isinstance(value, RevisionStoreError):
        if value.code is RevisionStoreErrorCode.RECOVERY_REQUIRED:
            return TaskEvent.REQUIRE_RECOVERY
        if value.code is RevisionStoreErrorCode.CLEANUP_REQUIRED:
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

    def revert_project(
        self,
        *,
        revert_key: str,
        project_id: str,
        source_revision: str,
        expected_head: str,
    ) -> StoredTaskRun:
        """Create or replay one verified forward restore through normal review."""

        replay = self._load_revert_replay(
            revert_key=revert_key,
            project_id=project_id,
            source_revision=source_revision,
            expected_head=expected_head,
        )
        if replay is not None:
            stored, binding = replay
            if stored.task_run.status is not TaskStatus.PROGRAM_READY:
                return stored
            return self._continue_bound_revert(stored, binding)

        lease = self._acquire(project_id)
        result: StoredTaskRun | None = None
        caught: TaskServiceError | None = None
        try:
            try:
                replay = self._load_revert_replay(
                    revert_key=revert_key,
                    project_id=project_id,
                    source_revision=source_revision,
                    expected_head=expected_head,
                )
                if replay is not None:
                    stored, binding = replay
                    if stored.task_run.status is TaskStatus.PROGRAM_READY:
                        result = self._continue_bound_revert_with_lease(
                            stored,
                            binding,
                            lease,
                        )
                    else:
                        result = stored
                else:
                    head = self._guard_runtime_head(project_id)
                    if head.revision_id != expected_head:
                        _raise(TaskServiceErrorCode.CONFLICT)
                    source = self._load_revert_source(
                        project_id=project_id,
                        source_revision=source_revision,
                        head=head,
                    )
                    try:
                        binding = build_revert_binding(
                            revert_key=revert_key,
                            project_id=project_id,
                            source_revision=source,
                            expected_head=head,
                        )
                    except RevertProgramError as error:
                        _raise(
                            TaskServiceErrorCode.INVALID_INPUT
                            if error.code is RevertProgramErrorCode.INVALID_INPUT
                            else TaskServiceErrorCode.CONFLICT
                        )
                    stored = _catalog_call(
                        lambda: self._catalog.create_revert_task(
                            revert_key=revert_key,
                            project_id=project_id,
                            source_revision=source,
                            expected_head=head,
                        )
                    )
                    parsed = parse_bound_revert_task(stored)
                    if parsed != binding:
                        _raise(TaskServiceErrorCode.CONFLICT)
                    if stored.task_run.status is TaskStatus.PROGRAM_READY:
                        result = self._continue_bound_revert_with_lease(
                            stored,
                            binding,
                            lease,
                        )
                    else:
                        result = stored
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
        if result is None:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return self._publish_review_if_prepared(result)

    def _load_revert_replay(
        self,
        *,
        revert_key: str,
        project_id: str,
        source_revision: str,
        expected_head: str,
    ) -> tuple[StoredTaskRun, BoundRevert] | None:
        try:
            task_id, _creation_digest = revert_task_identity(revert_key)
        except RevertProgramError:
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        try:
            stored = self._catalog.get_task(task_id=task_id)
        except TaskCatalogError as error:
            if error.code is TaskCatalogErrorCode.NOT_FOUND:
                return None
            raise TaskServiceError(_CATALOG_ERROR_MAP[error.code]) from None
        try:
            binding = require_matching_revert_task(
                stored,
                revert_key=revert_key,
                project_id=project_id,
                source_revision=source_revision,
                expected_head=expected_head,
            )
        except RevertProgramError as error:
            _raise(
                TaskServiceErrorCode.INVALID_INPUT
                if error.code is RevertProgramErrorCode.INVALID_INPUT
                else TaskServiceErrorCode.CONFLICT
            )
        return (stored, binding)

    def _load_revert_source(
        self,
        *,
        project_id: str,
        source_revision: str,
        head: ProjectHead,
    ) -> RevisionRef:
        return _load_revert_source_from_store(
            self._revision_store,
            project_id=project_id,
            source_revision=source_revision,
            head=head,
        )

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
        binding = parse_bound_revert_task(stored)
        if binding is not None:
            return self._continue_bound_revert(stored, binding)
        preflight = self._preflight(program)
        if preflight is None:
            return self._reject_validation(stored)
        compiled, validated = preflight
        return self._continue_preflighted(stored, compiled, validated)

    def _continue_bound_revert(
        self,
        stored: StoredTaskRun,
        binding: BoundRevert,
    ) -> StoredTaskRun:
        lease = self._acquire(binding.project_id)
        result: StoredTaskRun | None = None
        caught: TaskServiceError | None = None
        try:
            try:
                result = self._continue_bound_revert_with_lease(stored, binding, lease)
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
        if result is None:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return self._publish_review_if_prepared(result)

    def _continue_bound_revert_with_lease(
        self,
        stored: StoredTaskRun,
        binding: BoundRevert,
        lease: object,
    ) -> StoredTaskRun:
        if (
            stored.task_run.status is not TaskStatus.PROGRAM_READY
            or parse_bound_revert_task(stored) != binding
        ):
            _raise(TaskServiceErrorCode.INVALID_STATE)
        head = self._guard_runtime_head(binding.project_id)
        if head != binding.expected_head:
            _raise(TaskServiceErrorCode.CONFLICT)
        source = self._load_revert_source(
            project_id=binding.project_id,
            source_revision=binding.source_revision.id,
            head=head,
        )
        if source != binding.source_revision:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        try:
            compiled = compile_acceptance_spec(binding.program.acceptance)
        except Exception:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        revision_id = self._reserve_candidate(
            project_id=binding.project_id,
            expected_head=head,
            reservation_key=binding.reservation_key,
            lease=lease,
        )
        try:
            validating = self._cas(
                stored,
                transition_task(stored.task_run, TaskEvent.START_VALIDATION),
            )
        except (TaskServiceError, TaskStateError) as error:
            clean = self._cancel_unused_reservation(
                project_id=binding.project_id,
                expected_head=head,
                revision_id=revision_id,
                reservation_key=binding.reservation_key,
                lease=lease,
            )
            if not clean:
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
            if isinstance(error, TaskServiceError):
                cancellation = self._concurrent_cancellation(stored)
                if cancellation is not None:
                    return cancellation
                raise error
            _raise(TaskServiceErrorCode.INVALID_STATE)
        return self._run_bound_revert_with_lease(
            validating,
            binding,
            source,
            compiled,
            lease,
            head,
            revision_id=revision_id,
        )

    def _publish_review_if_prepared(self, stored: StoredTaskRun) -> StoredTaskRun:
        if stored.task_run.status is not TaskStatus.PREPARING_REVIEW:
            return stored
        try:
            return self._cas(
                stored,
                transition_task(stored.task_run, TaskEvent.PUBLISH_DRAFT),
            )
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(stored)
            if cancellation is not None:
                return cancellation
            raise
        except TaskStateError:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)

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

    @classmethod
    def reconcile_cancellation(
        cls,
        *,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
        lease_manager: ResourceLeaseManager,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun:
        """Settle a durable cancellation without constructing a CAD runtime."""

        if not (
            isinstance(task_store, TaskRunStore)
            and isinstance(revision_store, LocalRevisionStore)
            and isinstance(lease_manager, ResourceLeaseManager)
            and getattr(task_store, "_lease_manager", None) is lease_manager
            and getattr(revision_store, "_lease_manager", None) is lease_manager
        ):
            _raise(TaskServiceErrorCode.INVALID_INPUT)
        expected = _expected_generation(expected_generation)
        catalog = TaskCatalogService(
            task_store=task_store,
            revision_store=revision_store,
        )
        stored = _catalog_call(lambda: catalog.load_expected(task_id, expected))
        task = stored.task_run
        if task.status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED}:
            if _has_cancellation_origin(task):
                return stored
            _raise(TaskServiceErrorCode.INVALID_STATE)
        if task.status is TaskStatus.CANCEL_REQUESTED:
            return stored
        if not (
            _has_started_cancellation(task)
            and task.status
            in {
                TaskStatus.CANCELLING,
                TaskStatus.RECOVERY_REQUIRED,
                TaskStatus.CLEANUP_REQUIRED,
            }
        ):
            _raise(TaskServiceErrorCode.INVALID_STATE)

        lease = None
        try:
            lease = lease_manager.acquire_project_write(task.project_id)
        except Exception:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
        if lease is None:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)

        result: StoredTaskRun | None = None
        caught: TaskServiceError | None = None
        try:
            try:
                if task.candidate_revision is None:
                    binding = parse_bound_revert_task(task)
                    reservation_key = binding.reservation_key if binding is not None else task.id
                    reconciliation = revision_store.reconcile_candidate_reservation(
                        task.project_id,
                        task.base_revision,
                        reservation_key,
                        lease,
                    )
                else:
                    reconciliation = revision_store.reconcile(
                        task.project_id,
                        lease,
                    )
            except RevisionStoreError as error:
                event = _attention_event(error) or TaskEvent.REQUIRE_RECOVERY
                result = cls._persist_cancellation_attention(
                    catalog,
                    stored,
                    event,
                )
            except Exception:
                result = cls._persist_cancellation_attention(
                    catalog,
                    stored,
                    TaskEvent.REQUIRE_RECOVERY,
                )
            else:
                if task.candidate_revision is None:
                    result = cls._apply_pre_candidate_cancellation_reconcile(
                        catalog,
                        revision_store,
                        stored,
                        reconciliation,
                    )
                else:
                    result = cls._apply_cancellation_reconcile(
                        catalog,
                        revision_store,
                        stored,
                        reconciliation,
                    )
        except TaskServiceError as error:
            caught = error
        except Exception:
            caught = TaskServiceError(TaskServiceErrorCode.RECOVERY_REQUIRED)
        finally:
            release_failed = cls._release(lease)
        if release_failed:
            _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
        if caught is not None:
            raise caught from None
        if type(result) is not StoredTaskRun:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return result

    @staticmethod
    def _persist_cancellation_transition(
        catalog: TaskCatalogService,
        stored: StoredTaskRun,
        event: TaskEvent,
        *,
        committed_revision: str | None = None,
    ) -> StoredTaskRun:
        try:
            desired = transition_task(
                stored.task_run,
                event,
                committed_revision=committed_revision,
            )
        except TaskStateError:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        try:
            return _catalog_call(
                lambda: catalog._persist_cancellation_transition(  # noqa: SLF001
                    stored,
                    desired,
                    required_event=event,
                )
            )
        except TaskServiceError as error:
            try:
                latest = _catalog_call(lambda: catalog.get_task(task_id=stored.task_run.id))
            except TaskServiceError:
                raise error from None
            if _cancellation_descends_from(stored, latest):
                return latest
            raise error

    @classmethod
    def _persist_cancellation_attention(
        cls,
        catalog: TaskCatalogService,
        stored: StoredTaskRun,
        event: TaskEvent,
    ) -> StoredTaskRun:
        task = stored.task_run
        if task.status is TaskStatus.RECOVERY_REQUIRED:
            return stored
        if task.status is TaskStatus.CLEANUP_REQUIRED:
            if event is TaskEvent.REQUIRE_CLEANUP:
                return stored
            event = TaskEvent.REQUIRE_RECOVERY
        try:
            desired = transition_task(
                task,
                event,
                error=_attention_error(event),
            )
        except TaskStateError:
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        try:
            return _catalog_call(
                lambda: catalog._persist_cancellation_transition(  # noqa: SLF001
                    stored,
                    desired,
                    required_event=event,
                )
            )
        except TaskServiceError as error:
            try:
                latest = _catalog_call(lambda: catalog.get_task(task_id=stored.task_run.id))
            except TaskServiceError:
                raise error from None
            if _cancellation_descends_from(stored, latest):
                return latest
            raise error

    @classmethod
    def _apply_cancellation_reconcile(
        cls,
        catalog: TaskCatalogService,
        revision_store: LocalRevisionStore,
        stored: StoredTaskRun,
        result: object,
    ) -> StoredTaskRun:
        task = stored.task_run
        if type(result) is not ReconciliationResult:
            return cls._persist_cancellation_attention(
                catalog,
                stored,
                TaskEvent.REQUIRE_RECOVERY,
            )
        if result.status is ReconciliationStatus.CLEANUP_REQUIRED:
            return cls._persist_cancellation_attention(
                catalog,
                stored,
                TaskEvent.REQUIRE_CLEANUP,
            )
        try:
            durable_head = revision_store.load_head(task.project_id)
        except Exception:
            durable_head = None
        if not (
            type(durable_head) is ProjectHead
            and result.project_id == task.project_id
            and result.head == durable_head
            and durable_head.project_id == task.project_id
        ):
            return cls._persist_cancellation_attention(
                catalog,
                stored,
                TaskEvent.REQUIRE_RECOVERY,
            )
        if cls._cancellation_commit_is_exact(
            revision_store,
            task,
            result,
            durable_head,
        ):
            assert task.candidate_revision is not None
            return cls._persist_cancellation_transition(
                catalog,
                stored,
                TaskEvent.CONFIRM_COMMITTED,
                committed_revision=task.candidate_revision,
            )
        if cls._cancellation_is_proven_uncommitted(
            revision_store,
            task,
            result,
            durable_head,
        ):
            return cls._persist_cancellation_transition(
                catalog,
                stored,
                TaskEvent.CONFIRM_CANCELLED,
            )
        return cls._persist_cancellation_attention(
            catalog,
            stored,
            TaskEvent.REQUIRE_RECOVERY,
        )

    @classmethod
    def _apply_pre_candidate_cancellation_reconcile(
        cls,
        catalog: TaskCatalogService,
        revision_store: LocalRevisionStore,
        stored: StoredTaskRun,
        result: object,
    ) -> StoredTaskRun:
        task = stored.task_run
        if type(result) is not CandidateReservationReconciliation:
            return cls._persist_cancellation_attention(
                catalog,
                stored,
                TaskEvent.REQUIRE_RECOVERY,
            )
        if result.status is CandidateReservationStatus.CLEANUP_REQUIRED:
            return cls._persist_cancellation_attention(
                catalog,
                stored,
                TaskEvent.REQUIRE_CLEANUP,
            )
        try:
            durable_head = revision_store.load_head(task.project_id)
        except Exception:
            durable_head = None
        if not (
            task.candidate_revision is None
            and result.status
            in {
                CandidateReservationStatus.ABSENT,
                CandidateReservationStatus.NOT_COMMITTED,
            }
            and type(durable_head) is ProjectHead
            and result.project_id == task.project_id
            and result.head == durable_head
            and durable_head.project_id == task.project_id
        ):
            return cls._persist_cancellation_attention(
                catalog,
                stored,
                TaskEvent.REQUIRE_RECOVERY,
            )
        return cls._persist_cancellation_transition(
            catalog,
            stored,
            TaskEvent.CONFIRM_CANCELLED,
        )

    @staticmethod
    def _cancellation_commit_is_exact(
        revision_store: LocalRevisionStore,
        task: TaskRun,
        result: ReconciliationResult,
        durable_head: ProjectHead,
    ) -> bool:
        candidate_revision = task.candidate_revision
        if (
            candidate_revision is None
            or candidate_revision == task.base_revision
            or result.status
            not in {
                ReconciliationStatus.CLEAN,
                ReconciliationStatus.COMMITTED,
                ReconciliationStatus.NOT_COMMITTED,
            }
            or (
                task.review_policy is ReviewPolicy.REQUIRE_REVIEW
                and not _task_has_event(task, TaskEvent.ACCEPT_DRAFT)
            )
        ):
            return False
        lineage, candidate = _durable_candidate_lineage(
            revision_store,
            task,
            durable_head,
        )
        draft = task.draft
        return (
            lineage is _CandidateLineage.COMMITTED
            and type(candidate) is RevisionRef
            and candidate.id == candidate_revision
            and candidate.project_id == task.project_id
            and candidate.base_revision == task.base_revision
            and (
                task.review_policy is not ReviewPolicy.REQUIRE_REVIEW
                or (
                    draft is not None
                    and draft.project_id == task.project_id
                    and draft.base_revision == task.base_revision
                    and draft.revision_id == candidate_revision
                    and draft.manifest_sha256 == candidate.manifest_sha256
                )
            )
            and any(
                report.passed
                and report.candidate_revision == candidate_revision
                and report.manifest_sha256 == candidate.manifest_sha256
                for report in task.verification_reports
            )
        )

    @staticmethod
    def _cancellation_is_proven_uncommitted(
        revision_store: LocalRevisionStore,
        task: TaskRun,
        result: ReconciliationResult,
        durable_head: ProjectHead,
    ) -> bool:
        if task.candidate_revision is None and result.status is ReconciliationStatus.CLEAN:
            return result.journal is None
        if task.candidate_revision is None:
            lineage_is_uncommitted = durable_head.revision_id == task.base_revision
        else:
            lineage, _candidate = _durable_candidate_lineage(
                revision_store,
                task,
                durable_head,
            )
            lineage_is_uncommitted = lineage is _CandidateLineage.NOT_COMMITTED
        if not lineage_is_uncommitted:
            return False
        if result.status is ReconciliationStatus.CLEAN:
            return task.candidate_revision is None and result.journal is None
        journal = result.journal
        return (
            result.status is ReconciliationStatus.NOT_COMMITTED
            and type(journal) is CommitJournal
            and journal.project_id == task.project_id
            and journal.state is CommitJournalState.NOT_COMMITTED
            and journal.expected_head == durable_head
            and journal.expected_head.revision_id == task.base_revision
            and (
                task.candidate_revision is None
                or journal.candidate_revision == task.candidate_revision
            )
        )

    def reconcile_task(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun:
        stored = self._load_expected(task_id, expected_generation)
        if _has_cancellation_origin(stored.task_run):
            return type(self).reconcile_cancellation(
                task_store=self._task_store,
                revision_store=self._revision_store,
                lease_manager=self._lease_manager,
                task_id=task_id,
                expected_generation=stored.generation,
            )
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
                # Cancellation requests do not acquire the project lease and can
                # win the attention CAS after the entry load above.
                if _has_cancellation_origin(stored.task_run):
                    if stored.task_run.status is TaskStatus.CANCEL_REQUESTED:
                        result = stored
                    else:
                        try:
                            result = self._revision_store.reconcile(
                                stored.task_run.project_id,
                                lease,
                            )
                        except RevisionStoreError as error:
                            stored = self._persist_cancellation_attention(
                                self._catalog,
                                stored,
                                _attention_event(error) or TaskEvent.REQUIRE_RECOVERY,
                            )
                            result = error
                        except Exception as error:
                            stored = self._persist_cancellation_attention(
                                self._catalog,
                                stored,
                                TaskEvent.REQUIRE_RECOVERY,
                            )
                            result = error
                        else:
                            stored = self._apply_cancellation_reconcile(
                                self._catalog,
                                self._revision_store,
                                stored,
                                result,
                            )
                else:
                    result = self._coordinator.reconcile(
                        project_id=stored.task_run.project_id,
                        lease=lease,
                    )
                if _has_cancellation_origin(stored.task_run):
                    pass
                elif self._has_review_origin(stored.task_run):
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

    def _concurrent_cancellation(
        self,
        stored: StoredTaskRun,
    ) -> StoredTaskRun | None:
        try:
            latest = self._load(stored.task_run.id)
        except TaskServiceError:
            return None
        if _cancellation_descends_from(stored, latest):
            return latest
        return None

    def _reject_validation(self, stored: StoredTaskRun) -> StoredTaskRun:
        current = stored
        try:
            validating = self._cas(
                stored,
                transition_task(stored.task_run, TaskEvent.START_VALIDATION),
            )
            current = validating
            return self._cas(
                validating,
                transition_task(
                    validating.task_run,
                    TaskEvent.REJECT_PROGRAM,
                    error=_VALIDATION_ERROR,
                ),
            )
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(current)
            if cancellation is not None:
                return cancellation
            raise
        except TaskStateError:
            _raise(TaskServiceErrorCode.INVALID_STATE)

    def _reserve_candidate(
        self,
        *,
        project_id: str,
        expected_head: ProjectHead,
        reservation_key: str,
        lease: object,
    ) -> str:
        try:
            revision_id = self._coordinator.reserve_candidate(
                project_id=project_id,
                expected_head=expected_head,
                reservation_key=reservation_key,
                lease=lease,
            )
        except CandidateError as error:
            if error.code is CandidateErrorCode.RESOURCE_EXHAUSTED:
                _raise(TaskServiceErrorCode.RESOURCE_EXHAUSTED)
            if error.code is CandidateErrorCode.CONFLICT:
                _raise(TaskServiceErrorCode.CONFLICT)
            if error.code is CandidateErrorCode.INVALID_LEASE:
                _raise(TaskServiceErrorCode.LEASE_UNAVAILABLE)
            if _attention_event(error) is not None:
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
            _raise(TaskServiceErrorCode.STORE_FAILURE)
        except Exception:
            _raise(TaskServiceErrorCode.STORE_FAILURE)
        if type(revision_id) is not str:
            _raise(TaskServiceErrorCode.STORE_FAILURE)
        return revision_id

    def _cancel_unused_reservation(
        self,
        *,
        project_id: str,
        expected_head: ProjectHead,
        revision_id: str,
        reservation_key: str,
        lease: object,
    ) -> bool:
        try:
            result = self._coordinator.cancel_reservation(
                project_id=project_id,
                expected_head=expected_head,
                revision_id=revision_id,
                reservation_key=reservation_key,
                lease=lease,
            )
        except Exception:
            return False
        return bool(
            getattr(result, "status", None) is CandidateRollbackStatus.NOT_COMMITTED
            and getattr(result, "head", None) == expected_head
            and getattr(result, "cleanup_required", None) is False
            and getattr(result, "recovery_required", None) is False
        )

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
                reservation_key = stored.task_run.id
                revision_id = self._reserve_candidate(
                    project_id=project_id,
                    expected_head=head,
                    reservation_key=reservation_key,
                    lease=lease,
                )
                try:
                    current = stored
                    if submitted is not None:
                        current = self._cas(stored, submitted)
                    validating = self._cas(
                        current,
                        transition_task(current.task_run, TaskEvent.START_VALIDATION),
                    )
                except (TaskServiceError, TaskStateError) as error:
                    clean = self._cancel_unused_reservation(
                        project_id=project_id,
                        expected_head=head,
                        revision_id=revision_id,
                        reservation_key=reservation_key,
                        lease=lease,
                    )
                    if not clean:
                        caught = TaskServiceError(TaskServiceErrorCode.RECOVERY_REQUIRED)
                    elif isinstance(error, TaskServiceError):
                        cancellation = self._concurrent_cancellation(current)
                        if cancellation is not None:
                            result = cancellation
                        else:
                            caught = error
                    else:
                        caught = TaskServiceError(TaskServiceErrorCode.INVALID_STATE)
                else:
                    result = self._run_with_lease(
                        validating,
                        compiled,
                        validated,
                        lease,
                        head,
                        revision_id=revision_id,
                        reservation_key=reservation_key,
                    )
            except TaskServiceError as error:
                if caught is None:
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
            except TaskServiceError:
                cancellation = self._concurrent_cancellation(result)
                if cancellation is not None:
                    return cancellation
                raise
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
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(stored)
            if cancellation is not None:
                return cancellation
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        except TaskStateError:
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
        revert = parse_bound_revert_task(task)
        if revert is not None:
            try:
                source = self._revision_store.load_revision(
                    task.project_id,
                    revert.source_revision.id,
                )
            except Exception:
                source = None
            if not (
                revert.expected_head == expected_head
                and type(source) is RevisionRef
                and source == revert.source_revision
                and revert_payload_matches_source(
                    revision,
                    source,
                    expected_head=expected_head,
                )
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

    @staticmethod
    def _revert_evidence_matches(
        evidence: CandidateEvidence,
        revision: RevisionRef,
        source: RevisionRef,
        expected_head: ProjectHead,
    ) -> bool:
        if not (
            type(evidence) is CandidateEvidence
            and revert_payload_matches_source(
                revision,
                source,
                expected_head=expected_head,
            )
            and revision.model is not None
            and len(evidence.artifacts) == 2
        ):
            return False
        revision_artifacts = (revision.model,) + revision.artifacts
        return all(
            task_artifact.candidate_revision == revision.id
            and task_artifact.id == revision_artifact.id
            and task_artifact.name == revision_artifact.name
            and task_artifact.format == revision_artifact.format
            and task_artifact.sha256 == revision_artifact.sha256
            and task_artifact.size_bytes == revision_artifact.size_bytes
            for task_artifact, revision_artifact in zip(
                evidence.artifacts,
                revision_artifacts,
                strict=True,
            )
        )

    def _run_bound_revert_with_lease(
        self,
        validating: StoredTaskRun,
        binding: BoundRevert,
        source: RevisionRef,
        compiled: CompiledAcceptance,
        lease: object,
        head: ProjectHead,
        *,
        revision_id: str,
    ) -> StoredTaskRun:
        task = validating.task_run
        if (
            parse_bound_revert_task(task) != binding
            or head != binding.expected_head
            or source != binding.source_revision
        ):
            if not self._cancel_unused_reservation(
                project_id=task.project_id,
                expected_head=head,
                revision_id=revision_id,
                reservation_key=binding.reservation_key,
                lease=lease,
            ):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
            return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)

        try:
            active = self._coordinator.begin_seeded_reserved(
                project_id=task.project_id,
                expected_head=head,
                revision_id=revision_id,
                reservation_key=binding.reservation_key,
                source_revision=source,
                lease=lease,
            )
        except CandidateError as error:
            event = _attention_event(error)
            if event is not None:
                return self._persist_attention(validating, event)
            if error.code is CandidateErrorCode.CONFLICT:
                return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)
            if error.code is CandidateErrorCode.RESOURCE_EXHAUSTED:
                _raise(TaskServiceErrorCode.RESOURCE_EXHAUSTED)
            return self._reject_pre_candidate(validating, _BEGIN_FAILURE)
        except Exception:
            return self._reject_pre_candidate(validating, _BEGIN_FAILURE)

        try:
            executing = self._cas(
                validating,
                transition_task(
                    validating.task_run,
                    TaskEvent.VALIDATE_PROGRAM,
                    candidate_revision=active.binding.revision_id,
                ),
            )
        except (TaskServiceError, TaskStateError) as error:
            return self._abort_unpublished(validating, active, lease, error)

        current = executing
        current_candidate = active
        try:
            current_candidate = self._coordinator.adopt_materialized(
                candidate=current_candidate,
                source_revision=source,
                lease=lease,
            )
            current_candidate = self._coordinator.seal(
                candidate=current_candidate,
                lease=lease,
            )
            revision = current_candidate.revision
            reloaded = self._revision_store.load_revision(task.project_id, revision.id)
            if (
                type(reloaded) is not RevisionRef
                or reloaded != revision
                or revision.id == source.id
                or not revert_payload_matches_source(
                    revision,
                    source,
                    expected_head=head,
                )
            ):
                raise ValueError
            evidence = self._executor.collect_evidence(candidate=current_candidate)
            if not self._revert_evidence_matches(evidence, revision, source, head):
                raise ValueError
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
        if current.task_run.review_policy is not ReviewPolicy.REQUIRE_REVIEW:
            return self._fail_published(
                current,
                current_candidate,
                lease,
                _VERIFICATION_FAILURE,
                _VERIFICATION_FAILURE,
            )

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

    def _run_with_lease(
        self,
        validating: StoredTaskRun,
        compiled: CompiledAcceptance,
        validated: object,
        lease: object,
        head: ProjectHead,
        *,
        revision_id: str,
        reservation_key: str,
    ) -> StoredTaskRun:
        task = validating.task_run
        if head.revision_id != task.base_revision:
            if not self._cancel_unused_reservation(
                project_id=task.project_id,
                expected_head=head,
                revision_id=revision_id,
                reservation_key=reservation_key,
                lease=lease,
            ):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
            return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)

        try:
            active = self._coordinator.begin_reserved(
                project_id=task.project_id,
                expected_head=head,
                revision_id=revision_id,
                reservation_key=reservation_key,
                lease=lease,
            )
        except CandidateError as error:
            event = _attention_event(error)
            if event is not None:
                return self._persist_attention(validating, event)
            if error.code is CandidateErrorCode.CONFLICT:
                return self._reject_pre_candidate(validating, _PRE_CANDIDATE_CONFLICT)
            if error.code is CandidateErrorCode.RESOURCE_EXHAUSTED:
                _raise(TaskServiceErrorCode.RESOURCE_EXHAUSTED)
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
            with _candidate_file_limit(self._revision_store):
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
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(stored)
            if cancellation is not None:
                return cancellation
            raise
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
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(stored)
            if cancellation is not None:
                return cancellation
            raise
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
        if _cancellation_descends_from(validating, latest):
            if event is None:
                return latest
            if latest.task_run.status is TaskStatus.CANCEL_REQUESTED:
                latest = _catalog_call(
                    lambda: self._catalog.start_cancellation(
                        task_id=latest.task_run.id,
                        expected_generation=latest.generation,
                    )
                )
            if latest.task_run.status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED}:
                return latest
            return self._persist_cancellation_attention(
                self._catalog,
                latest,
                event,
            )
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
        if _cancellation_descends_from(stored, latest):
            return latest
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
        cause_event = _attention_event(cause)
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

        if rollback_clean and cause_event is None:
            try:
                return self._cas(
                    current,
                    transition_task(current.task_run, TaskEvent.COMPLETE_ROLLBACK),
                )
            except (TaskServiceError, TaskStateError):
                _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        return self._published_attention(
            current,
            cause_event or rollback_event or TaskEvent.REQUIRE_RECOVERY,
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
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(stored)
            if cancellation is not None:
                return cancellation
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        except TaskStateError:
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
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(committing)
            if cancellation is not None:
                return cancellation
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        except TaskStateError:
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
        except TaskServiceError:
            cancellation = self._concurrent_cancellation(committing)
            if cancellation is not None:
                return cancellation
            _raise(TaskServiceErrorCode.RECOVERY_REQUIRED)
        except TaskStateError:
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
        return _durable_candidate_lineage(
            self._revision_store,
            task,
            durable_head,
        )

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
