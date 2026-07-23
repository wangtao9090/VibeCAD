"""Store-only task catalog with no CAD runtime dependency."""

from __future__ import annotations

import time
from enum import StrEnum

from vibecad.execution.revisions import LocalRevisionStore, ProjectHead
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER
from vibecad.workflow.revert import (
    BoundRevert,
    RevertProgramError,
    RevertProgramErrorCode,
    build_revert_binding,
    parse_bound_revert_task,
)
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewDraft,
    ReviewPolicy,
    TaskEvent,
    TaskRun,
    TaskStateError,
    TaskStatus,
    new_task_run,
    task_creation_identity,
    transition_task,
)
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskSnapshotEntry,
    TaskStoreError,
    TaskStoreErrorCode,
)

__all__ = (
    "TaskCatalogErrorCode",
    "TaskCatalogError",
    "TaskCatalogService",
)


class TaskCatalogErrorCode(StrEnum):
    """Closed failures for store-only task operations."""

    INVALID_INPUT = "invalid_input"
    UNSUPPORTED_REASONING_OWNER = "unsupported_reasoning_owner"
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    STORE_FAILURE = "store_failure"
    RECOVERY_REQUIRED = "recovery_required"


_ERROR_MESSAGES = {
    TaskCatalogErrorCode.INVALID_INPUT: "The task catalog input is invalid.",
    TaskCatalogErrorCode.UNSUPPORTED_REASONING_OWNER: (
        "The requested reasoning owner is not supported."
    ),
    TaskCatalogErrorCode.INVALID_STATE: "The task is not ready for this operation.",
    TaskCatalogErrorCode.NOT_FOUND: "The task record was not found.",
    TaskCatalogErrorCode.CONFLICT: "The task record changed concurrently.",
    TaskCatalogErrorCode.RESOURCE_EXHAUSTED: "The task store capacity is exhausted.",
    TaskCatalogErrorCode.STORE_FAILURE: "The task record operation failed.",
    TaskCatalogErrorCode.RECOVERY_REQUIRED: "The task requires explicit reconciliation.",
}
_REPLAY_WAIT_SECONDS = 1.0
_REPLAY_RETRY_LIMIT = 512
_REPLAY_DEADLINE_GRACE_RETRY_LIMIT = 32
_REPLAY_LOAD_DELAY_SECONDS = 0.002
_REPLAY_DEADLINE_GRACE_DELAY_CAP_SECONDS = 0.05
_IDLE_CANCEL_STATUSES = frozenset(
    {
        TaskStatus.CREATED,
        TaskStatus.NEEDS_PLAN,
        TaskStatus.PROGRAM_READY,
        TaskStatus.NEEDS_INPUT,
    }
)
_ACTIVE_CANCEL_STATUSES = frozenset(
    {
        TaskStatus.VALIDATING_PROGRAM,
        TaskStatus.EXECUTING,
        TaskStatus.VERIFYING,
        TaskStatus.COMMITTING,
        TaskStatus.PREPARING_REVIEW,
        TaskStatus.ACCEPTING_DRAFT,
    }
)
_CANCELLATION_STATUSES = frozenset(
    {
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLING,
        TaskStatus.CANCELLED,
    }
)
_CANCELLATION_DESCENDANT_STATUSES = _CANCELLATION_STATUSES | frozenset(
    {
        TaskStatus.RECOVERY_REQUIRED,
        TaskStatus.CLEANUP_REQUIRED,
        TaskStatus.SUCCEEDED,
    }
)
_CANCELLATION_TAIL_EVENTS = frozenset(
    {
        TaskEvent.START_CANCELLATION,
        TaskEvent.REQUIRE_RECOVERY,
        TaskEvent.REQUIRE_CLEANUP,
        TaskEvent.CONFIRM_COMMITTED,
        TaskEvent.CONFIRM_CANCELLED,
    }
)
_CANCELLATION_REQUEST_EVENTS = frozenset(
    {
        TaskEvent.REQUEST_CANCEL,
        TaskEvent.REQUEST_ACTIVE_CANCEL,
    }
)


def _has_task_event(task: TaskRun, event: TaskEvent) -> bool:
    return any(record.event is event for record in task.transitions)


def _has_cancellation_request(task: TaskRun) -> bool:
    return task.status in _CANCELLATION_DESCENDANT_STATUSES and any(
        record.event in _CANCELLATION_REQUEST_EVENTS for record in task.transitions
    )


def _has_cancellation_start(task: TaskRun) -> bool:
    return _has_cancellation_request(task) and _has_task_event(
        task,
        TaskEvent.START_CANCELLATION,
    )


def _cancellation_payload_is_unchanged(before: TaskRun, after: TaskRun) -> bool:
    return (
        before.id == after.id
        and before.project_id == after.project_id
        and before.base_revision == after.base_revision
        and before.reasoning_owner is after.reasoning_owner
        and before.review_policy is after.review_policy
        and before.creation_digest == after.creation_digest
        and before.program == after.program
        and before.candidate_revision == after.candidate_revision
        and before.draft == after.draft
        and before.steps == after.steps
        and before.verification_reports == after.verification_reports
        and before.artifacts == after.artifacts
    )


def _is_cancellation_descendant(
    stored: StoredTaskRun,
    desired: TaskRun,
    *,
    required_event: TaskEvent,
) -> bool:
    task = stored.task_run
    prefix_length = len(desired.transitions)
    return (
        stored.generation >= 0
        and task.status in _CANCELLATION_DESCENDANT_STATUSES
        and _has_task_event(task, required_event)
        and _cancellation_payload_is_unchanged(desired, task)
        and len(task.transitions) >= prefix_length
        and task.transitions[:prefix_length] == desired.transitions
        and all(
            record.event in _CANCELLATION_TAIL_EVENTS for record in task.transitions[prefix_length:]
        )
    )


class _ReplayRetryBudget:
    __slots__ = ("_deadline", "_deadline_grace_remaining", "_retries_remaining")

    def __init__(self) -> None:
        self._deadline = time.monotonic() + _REPLAY_WAIT_SECONDS
        self._retries_remaining = _REPLAY_RETRY_LIMIT
        self._deadline_grace_remaining = _REPLAY_DEADLINE_GRACE_RETRY_LIMIT

    def wait_for_retry(self) -> bool:
        if time.monotonic() < self._deadline:
            if self._retries_remaining <= 0:
                return False
            self._retries_remaining -= 1
            delay = _REPLAY_LOAD_DELAY_SECONDS
        else:
            if self._deadline_grace_remaining <= 0:
                return False
            attempt = _REPLAY_DEADLINE_GRACE_RETRY_LIMIT - self._deadline_grace_remaining
            self._deadline_grace_remaining -= 1
            delay = min(
                _REPLAY_LOAD_DELAY_SECONDS * (2**attempt),
                _REPLAY_DEADLINE_GRACE_DELAY_CAP_SECONDS,
            )
        time.sleep(delay)
        return True


class TaskCatalogError(ValueError):
    """Fixed, non-reflective catalog failure."""

    __slots__ = ("code", "message", "schema_version")

    def __init__(self, code: TaskCatalogErrorCode) -> None:
        if type(code) is not TaskCatalogErrorCode:
            raise TypeError("code must be a TaskCatalogErrorCode")
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


def _raise(code: TaskCatalogErrorCode) -> None:
    raise TaskCatalogError(code)


def _expected_generation(value: object) -> int:
    if type(value) is not int or value < 0 or value > MAX_SAFE_JSON_INTEGER:
        _raise(TaskCatalogErrorCode.INVALID_INPUT)
    return value


class TaskCatalogService:
    """Own create/get/cancel/reject and shared task-record CAS without loading CAD."""

    __slots__ = ("_revision_store", "_task_store")

    def __init__(
        self,
        *,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
    ) -> None:
        if not (
            isinstance(task_store, TaskRunStore) and isinstance(revision_store, LocalRevisionStore)
        ):
            _raise(TaskCatalogErrorCode.INVALID_INPUT)
        self._task_store = task_store
        self._revision_store = revision_store

    def create_task(
        self,
        *,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
        task_id: str | None = None,
        create_key: str | None = None,
    ) -> StoredTaskRun:
        if type(reasoning_owner) is not ReasoningOwner or type(review_policy) is not ReviewPolicy:
            _raise(TaskCatalogErrorCode.INVALID_INPUT)
        if reasoning_owner is not ReasoningOwner.EXTERNAL_PLAN:
            _raise(TaskCatalogErrorCode.UNSUPPORTED_REASONING_OWNER)
        if create_key is not None:
            if task_id is not None:
                _raise(TaskCatalogErrorCode.INVALID_INPUT)
            try:
                task_id, creation_digest = task_creation_identity(create_key)
            except TaskStateError:
                _raise(TaskCatalogErrorCode.INVALID_INPUT)
            return self._create_keyed(
                task_id=task_id,
                creation_digest=creation_digest,
                project_id=project_id,
                reasoning_owner=reasoning_owner,
                review_policy=review_policy,
            )
        if task_id is None:
            _raise(TaskCatalogErrorCode.INVALID_INPUT)
        task: TaskRun | None = None
        stored: StoredTaskRun | None = None
        failure: TaskCatalogErrorCode | None = None
        uncertain_generation: int | None = None
        try:
            head = self._revision_store.load_head(project_id)
            if type(head) is not ProjectHead or head.project_id != project_id:
                failure = TaskCatalogErrorCode.STORE_FAILURE
                head = None
            if head is None:
                raise RuntimeError
            task = new_task_run(
                task_id=task_id,
                project_id=project_id,
                base_revision=head.revision_id,
                reasoning_owner=reasoning_owner,
                review_policy=review_policy,
            )
            task = transition_task(task, TaskEvent.REQUEST_PLAN)
            stored = self._task_store.create(task)
        except TaskStoreError as error:
            if error.code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
                uncertain_generation = getattr(error, "committed_generation", None)
            elif error.code is TaskStoreErrorCode.ALREADY_EXISTS:
                failure = TaskCatalogErrorCode.CONFLICT
            elif error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                failure = TaskCatalogErrorCode.RESOURCE_EXHAUSTED
            elif error.code is TaskStoreErrorCode.INVALID_ID:
                failure = TaskCatalogErrorCode.INVALID_INPUT
            else:
                failure = TaskCatalogErrorCode.STORE_FAILURE
        except TaskStateError:
            failure = TaskCatalogErrorCode.INVALID_INPUT
        except Exception:
            failure = failure or TaskCatalogErrorCode.STORE_FAILURE
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
            failure = TaskCatalogErrorCode.STORE_FAILURE
        if failure is not None:
            _raise(failure)
        if type(stored) is not StoredTaskRun:
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        return stored

    def create_revert_task(
        self,
        *,
        revert_key: str,
        project_id: str,
        source_revision: object,
        expected_head: object,
    ) -> StoredTaskRun:
        """Atomically create or exactly replay one system-bound revert task."""

        stored, _created_here = self.create_revert_task_with_disposition(
            revert_key=revert_key,
            project_id=project_id,
            source_revision=source_revision,
            expected_head=expected_head,
        )
        return stored

    def create_revert_task_with_disposition(
        self,
        *,
        revert_key: str,
        project_id: str,
        source_revision: object,
        expected_head: object,
    ) -> tuple[StoredTaskRun, bool]:
        """Return the exact task plus whether this call proved its creation."""

        try:
            binding = build_revert_binding(
                revert_key=revert_key,
                project_id=project_id,
                source_revision=source_revision,
                expected_head=expected_head,
            )
        except RevertProgramError as error:
            _raise(
                TaskCatalogErrorCode.INVALID_INPUT
                if error.code is RevertProgramErrorCode.INVALID_INPUT
                else TaskCatalogErrorCode.CONFLICT
            )
        return self._create_bound_task_with_disposition(binding)

    def _create_bound_task(self, binding: BoundRevert) -> StoredTaskRun:
        stored, _created_here = self._create_bound_task_with_disposition(binding)
        return stored

    def _create_bound_task_with_disposition(
        self,
        binding: BoundRevert,
    ) -> tuple[StoredTaskRun, bool]:
        retry_budget = _ReplayRetryBudget()
        task: TaskRun | None = None
        while True:
            existing = self._load_replay_candidate(
                binding.task_id,
                await_publication=False,
                retry_budget=retry_budget,
            )
            if existing is not None:
                return self._replay_bound_or_conflict(existing, binding), False
            if task is None:
                try:
                    task = new_task_run(
                        task_id=binding.task_id,
                        project_id=binding.project_id,
                        base_revision=binding.expected_head.revision_id,
                        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                        review_policy=ReviewPolicy.REQUIRE_REVIEW,
                        creation_digest=binding.creation_digest,
                    )
                    task = transition_task(task, TaskEvent.REQUEST_PLAN)
                    task = transition_task(
                        task,
                        TaskEvent.SUBMIT_PROGRAM,
                        program=binding.program,
                    )
                except TaskStateError:
                    _raise(TaskCatalogErrorCode.INVALID_INPUT)
            try:
                stored = self._task_store.create(task)
            except TaskStoreError as error:
                if error.code in {
                    TaskStoreErrorCode.ALREADY_EXISTS,
                    TaskStoreErrorCode.DURABILITY_UNCERTAIN,
                }:
                    readback = self._load_replay_candidate(
                        binding.task_id,
                        await_publication=True,
                        retry_budget=retry_budget,
                    )
                    if readback is None:
                        _raise(TaskCatalogErrorCode.STORE_FAILURE)
                    return self._replay_bound_or_conflict(readback, binding), False
                if error.code is TaskStoreErrorCode.LOCK_UNAVAILABLE:
                    if retry_budget.wait_for_retry():
                        continue
                    _raise(TaskCatalogErrorCode.STORE_FAILURE)
                if error.code is TaskStoreErrorCode.INVALID_ID:
                    _raise(TaskCatalogErrorCode.INVALID_INPUT)
                if error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                    _raise(TaskCatalogErrorCode.RESOURCE_EXHAUSTED)
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            except Exception:
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            if (
                type(stored) is not StoredTaskRun
                or stored.generation != 0
                or stored.task_run != task
            ):
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            return stored, True

    @staticmethod
    def _replay_bound_or_conflict(
        stored: StoredTaskRun,
        binding: BoundRevert,
    ) -> StoredTaskRun:
        parsed = parse_bound_revert_task(stored)
        if parsed != binding:
            _raise(TaskCatalogErrorCode.CONFLICT)
        return stored

    def _create_keyed(
        self,
        *,
        task_id: str,
        creation_digest: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
    ) -> StoredTaskRun:
        retry_budget = _ReplayRetryBudget()
        while True:
            try:
                existing = self._task_store.load(task_id)
            except TaskStoreError as error:
                if error.code is TaskStoreErrorCode.NOT_FOUND:
                    existing = None
                elif error.code is TaskStoreErrorCode.LOCK_UNAVAILABLE:
                    if not retry_budget.wait_for_retry():
                        _raise(TaskCatalogErrorCode.STORE_FAILURE)
                    continue
                elif error.code is TaskStoreErrorCode.INVALID_ID:
                    _raise(TaskCatalogErrorCode.INVALID_INPUT)
                elif error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                    _raise(TaskCatalogErrorCode.RESOURCE_EXHAUSTED)
                else:
                    _raise(TaskCatalogErrorCode.STORE_FAILURE)
            except Exception:
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            if existing is not None:
                return self._replay_or_conflict(
                    existing,
                    creation_digest=creation_digest,
                    project_id=project_id,
                    reasoning_owner=reasoning_owner,
                    review_policy=review_policy,
                )

            try:
                head = self._revision_store.load_head(project_id)
                if type(head) is not ProjectHead or head.project_id != project_id:
                    _raise(TaskCatalogErrorCode.STORE_FAILURE)
                task = transition_task(
                    new_task_run(
                        task_id=task_id,
                        project_id=project_id,
                        base_revision=head.revision_id,
                        reasoning_owner=reasoning_owner,
                        review_policy=review_policy,
                        creation_digest=creation_digest,
                    ),
                    TaskEvent.REQUEST_PLAN,
                )
            except TaskCatalogError:
                raise
            except TaskStateError:
                _raise(TaskCatalogErrorCode.INVALID_INPUT)
            except Exception:
                _raise(TaskCatalogErrorCode.STORE_FAILURE)

            try:
                return self._task_store.create(task)
            except TaskStoreError as error:
                if error.code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
                    readback = self._load_replay_candidate(
                        task_id,
                        await_publication=True,
                        retry_budget=retry_budget,
                    )
                    if readback is None:
                        _raise(TaskCatalogErrorCode.STORE_FAILURE)
                    return self._replay_or_conflict(
                        readback,
                        creation_digest=creation_digest,
                        project_id=project_id,
                        reasoning_owner=reasoning_owner,
                        review_policy=review_policy,
                    )
                if error.code is TaskStoreErrorCode.ALREADY_EXISTS:
                    readback = self._load_replay_candidate(
                        task_id,
                        await_publication=True,
                        retry_budget=retry_budget,
                    )
                    if readback is None:
                        _raise(TaskCatalogErrorCode.STORE_FAILURE)
                    return self._replay_or_conflict(
                        readback,
                        creation_digest=creation_digest,
                        project_id=project_id,
                        reasoning_owner=reasoning_owner,
                        review_policy=review_policy,
                    )
                if error.code is TaskStoreErrorCode.LOCK_UNAVAILABLE:
                    if not retry_budget.wait_for_retry():
                        _raise(TaskCatalogErrorCode.STORE_FAILURE)
                    continue
                if error.code is TaskStoreErrorCode.INVALID_ID:
                    _raise(TaskCatalogErrorCode.INVALID_INPUT)
                if error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                    _raise(TaskCatalogErrorCode.RESOURCE_EXHAUSTED)
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            except Exception:
                _raise(TaskCatalogErrorCode.STORE_FAILURE)

    def _load_replay_candidate(
        self,
        task_id: str,
        *,
        await_publication: bool,
        retry_budget: _ReplayRetryBudget | None = None,
    ) -> StoredTaskRun | None:
        if retry_budget is None:
            retry_budget = _ReplayRetryBudget()
        while True:
            try:
                stored = self._task_store.load(task_id)
                if type(stored) is not StoredTaskRun:
                    _raise(TaskCatalogErrorCode.STORE_FAILURE)
                return stored
            except TaskStoreError as error:
                transient = error.code in {
                    TaskStoreErrorCode.LOCK_UNAVAILABLE,
                    TaskStoreErrorCode.RESOURCE_EXHAUSTED,
                }
                if transient:
                    if not retry_budget.wait_for_retry():
                        _raise(
                            TaskCatalogErrorCode.RESOURCE_EXHAUSTED
                            if error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED
                            else TaskCatalogErrorCode.STORE_FAILURE
                        )
                    continue
                if await_publication and error.code is TaskStoreErrorCode.NOT_FOUND:
                    if not retry_budget.wait_for_retry():
                        return None
                    continue
                if error.code is TaskStoreErrorCode.NOT_FOUND and not await_publication:
                    return None
                if error.code is TaskStoreErrorCode.INVALID_ID:
                    _raise(TaskCatalogErrorCode.INVALID_INPUT)
                if error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                    _raise(TaskCatalogErrorCode.RESOURCE_EXHAUSTED)
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            except Exception:
                _raise(TaskCatalogErrorCode.STORE_FAILURE)

    @staticmethod
    def _replay_or_conflict(
        stored: StoredTaskRun,
        *,
        creation_digest: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
    ) -> StoredTaskRun:
        task = stored.task_run
        if (
            task.creation_digest != creation_digest
            or task.project_id != project_id
            or task.reasoning_owner is not reasoning_owner
            or task.review_policy is not review_policy
        ):
            _raise(TaskCatalogErrorCode.CONFLICT)
        return stored

    def get_task(self, *, task_id: str) -> StoredTaskRun:
        return self._load(task_id)

    def cancel_task(
        self,
        *,
        task_id: str,
        expected_generation: int,
        active: bool = False,
    ) -> StoredTaskRun:
        """Persist one cancellation request or replay its durable lineage."""

        if type(active) is not bool:
            _raise(TaskCatalogErrorCode.INVALID_INPUT)
        expected = _expected_generation(expected_generation)
        stored = self._load_replay_candidate(
            task_id,
            await_publication=False,
            retry_budget=_ReplayRetryBudget(),
        )
        if stored is None:
            _raise(TaskCatalogErrorCode.NOT_FOUND)
        status = stored.task_run.status
        if _has_cancellation_request(stored.task_run):
            if expected > stored.generation:
                _raise(TaskCatalogErrorCode.CONFLICT)
            return stored
        if stored.generation != expected:
            _raise(TaskCatalogErrorCode.CONFLICT)
        if status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
        }:
            _raise(TaskCatalogErrorCode.CONFLICT)
        if status in {
            TaskStatus.RECOVERY_REQUIRED,
            TaskStatus.CLEANUP_REQUIRED,
        }:
            _raise(TaskCatalogErrorCode.RECOVERY_REQUIRED)
        if status not in _IDLE_CANCEL_STATUSES | _ACTIVE_CANCEL_STATUSES:
            _raise(TaskCatalogErrorCode.INVALID_STATE)
        event = TaskEvent.REQUEST_CANCEL
        if active and status in _IDLE_CANCEL_STATUSES:
            event = TaskEvent.REQUEST_ACTIVE_CANCEL
        try:
            requested = transition_task(stored.task_run, event)
        except TaskStateError:
            _raise(TaskCatalogErrorCode.INVALID_STATE)
        return self._persist_cancellation_transition(
            stored,
            requested,
            required_event=event,
        )

    def start_cancellation(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun:
        """Persist the fenced start of an already durable active cancellation."""

        expected = _expected_generation(expected_generation)
        stored = self._load_replay_candidate(
            task_id,
            await_publication=False,
            retry_budget=_ReplayRetryBudget(),
        )
        if stored is None:
            _raise(TaskCatalogErrorCode.NOT_FOUND)
        if _has_cancellation_start(stored.task_run):
            if expected > stored.generation:
                _raise(TaskCatalogErrorCode.CONFLICT)
            return stored
        if stored.generation != expected:
            _raise(TaskCatalogErrorCode.CONFLICT)
        if (
            stored.task_run.status is not TaskStatus.CANCEL_REQUESTED
            or not _has_cancellation_request(stored.task_run)
        ):
            _raise(TaskCatalogErrorCode.INVALID_STATE)
        try:
            cancelling = transition_task(
                stored.task_run,
                TaskEvent.START_CANCELLATION,
            )
        except TaskStateError:
            _raise(TaskCatalogErrorCode.INVALID_STATE)
        return self._persist_cancellation_transition(
            stored,
            cancelling,
            required_event=TaskEvent.START_CANCELLATION,
        )

    def _persist_cancellation_transition(
        self,
        stored: StoredTaskRun,
        desired: TaskRun,
        *,
        required_event: TaskEvent,
    ) -> StoredTaskRun:
        mutation_budget = _ReplayRetryBudget()
        while True:
            try:
                result = self._task_store.compare_and_set(
                    stored.task_run.id,
                    stored.generation,
                    desired,
                )
            except TaskStoreError as error:
                if error.code is TaskStoreErrorCode.LOCK_UNAVAILABLE:
                    if mutation_budget.wait_for_retry():
                        continue
                    readback = self._load_replay_candidate(
                        stored.task_run.id,
                        await_publication=False,
                        retry_budget=_ReplayRetryBudget(),
                    )
                    if (
                        type(readback) is StoredTaskRun
                        and readback.generation >= stored.generation + 1
                        and _is_cancellation_descendant(
                            readback,
                            desired,
                            required_event=required_event,
                        )
                    ):
                        return readback
                    _raise(TaskCatalogErrorCode.STORE_FAILURE)
                if error.code is TaskStoreErrorCode.CONFLICT:
                    readback = self._load_replay_candidate(
                        stored.task_run.id,
                        await_publication=False,
                        retry_budget=_ReplayRetryBudget(),
                    )
                    if (
                        type(readback) is StoredTaskRun
                        and readback.generation >= stored.generation + 1
                        and _is_cancellation_descendant(
                            readback,
                            desired,
                            required_event=required_event,
                        )
                    ):
                        return readback
                    _raise(TaskCatalogErrorCode.CONFLICT)
                if error.code is TaskStoreErrorCode.DURABILITY_UNCERTAIN:
                    committed = getattr(error, "committed_generation", None)
                    readback = self._load_replay_candidate(
                        stored.task_run.id,
                        await_publication=True,
                        retry_budget=_ReplayRetryBudget(),
                    )
                    if (
                        committed == stored.generation + 1
                        and type(readback) is StoredTaskRun
                        and readback.generation >= committed
                        and _is_cancellation_descendant(
                            readback,
                            desired,
                            required_event=required_event,
                        )
                    ):
                        return readback
                    _raise(TaskCatalogErrorCode.STORE_FAILURE)
                if error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                    _raise(TaskCatalogErrorCode.RESOURCE_EXHAUSTED)
                if error.code is TaskStoreErrorCode.INVALID_ID:
                    _raise(TaskCatalogErrorCode.INVALID_INPUT)
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            except Exception:
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            if (
                type(result) is not StoredTaskRun
                or result.generation != stored.generation + 1
                or result.task_run != desired
            ):
                _raise(TaskCatalogErrorCode.STORE_FAILURE)
            return result

    def snapshot_tasks(self) -> tuple[TaskSnapshotEntry, ...]:
        try:
            stored = self._task_store.snapshot()
        except TaskStoreError as error:
            if error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                _raise(TaskCatalogErrorCode.RESOURCE_EXHAUSTED)
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        except Exception:
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        if type(stored) is not tuple or not all(type(item) is TaskSnapshotEntry for item in stored):
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        return stored

    def discovery_namespace(self) -> bytes:
        try:
            value = self._task_store.discovery_namespace()
        except Exception:
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        if type(value) is not bytes or len(value) != 32:
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        return value

    def reject_draft(
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
            _raise(TaskCatalogErrorCode.INVALID_STATE)
        if type(draft_id) is not str or draft.id != draft_id:
            _raise(TaskCatalogErrorCode.CONFLICT)
        if task.status is TaskStatus.REJECTED:
            return stored
        if task.status in {TaskStatus.ACCEPTING_DRAFT, TaskStatus.SUCCEEDED}:
            _raise(TaskCatalogErrorCode.CONFLICT)
        if task.status is not TaskStatus.AWAITING_USER_REVIEW:
            _raise(TaskCatalogErrorCode.INVALID_STATE)
        if stored.generation != expected:
            _raise(TaskCatalogErrorCode.CONFLICT)
        try:
            return self._compare_and_set(
                stored,
                transition_task(task, TaskEvent.REJECT_DRAFT),
            )
        except TaskStateError:
            _raise(TaskCatalogErrorCode.INVALID_STATE)

    def _load(self, task_id: str) -> StoredTaskRun:
        stored: StoredTaskRun | None = None
        failure: TaskCatalogErrorCode | None = None
        try:
            stored = self._task_store.load(task_id)
        except TaskStoreError as error:
            if error.code is TaskStoreErrorCode.NOT_FOUND:
                failure = TaskCatalogErrorCode.NOT_FOUND
            elif error.code is TaskStoreErrorCode.INVALID_ID:
                failure = TaskCatalogErrorCode.INVALID_INPUT
            elif error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                failure = TaskCatalogErrorCode.RESOURCE_EXHAUSTED
            else:
                failure = TaskCatalogErrorCode.STORE_FAILURE
        except Exception:
            failure = TaskCatalogErrorCode.STORE_FAILURE
        if failure is not None:
            _raise(failure)
        if type(stored) is not StoredTaskRun:
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        return stored

    def load_expected(self, task_id: str, generation: object) -> StoredTaskRun:
        expected = _expected_generation(generation)
        stored = self._load(task_id)
        if stored.generation != expected:
            _raise(TaskCatalogErrorCode.CONFLICT)
        return stored

    def compare_and_set(self, stored: StoredTaskRun, task: TaskRun) -> StoredTaskRun:
        return self._compare_and_set(stored, task)

    def _compare_and_set(self, stored: StoredTaskRun, task: TaskRun) -> StoredTaskRun:
        result: StoredTaskRun | None = None
        failure: TaskCatalogErrorCode | None = None
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
                failure = TaskCatalogErrorCode.CONFLICT
            elif error.code is TaskStoreErrorCode.RESOURCE_EXHAUSTED:
                failure = TaskCatalogErrorCode.RESOURCE_EXHAUSTED
            else:
                failure = TaskCatalogErrorCode.STORE_FAILURE
        except Exception:
            failure = TaskCatalogErrorCode.STORE_FAILURE
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
            failure = TaskCatalogErrorCode.STORE_FAILURE
        if failure is not None:
            _raise(failure)
        if type(result) is not StoredTaskRun:
            _raise(TaskCatalogErrorCode.STORE_FAILURE)
        return result
