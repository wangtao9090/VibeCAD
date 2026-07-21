"""Store-only task catalog with no CAD runtime dependency."""

from __future__ import annotations

from enum import StrEnum

from vibecad.execution.revisions import LocalRevisionStore, ProjectHead
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewDraft,
    ReviewPolicy,
    TaskEvent,
    TaskRun,
    TaskStateError,
    TaskStatus,
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
    STORE_FAILURE = "store_failure"


_ERROR_MESSAGES = {
    TaskCatalogErrorCode.INVALID_INPUT: "The task catalog input is invalid.",
    TaskCatalogErrorCode.UNSUPPORTED_REASONING_OWNER: (
        "The requested reasoning owner is not supported."
    ),
    TaskCatalogErrorCode.INVALID_STATE: "The task is not ready for this operation.",
    TaskCatalogErrorCode.NOT_FOUND: "The task record was not found.",
    TaskCatalogErrorCode.CONFLICT: "The task record changed concurrently.",
    TaskCatalogErrorCode.STORE_FAILURE: "The task record operation failed.",
}


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
    if type(value) is not int or value < 0:
        _raise(TaskCatalogErrorCode.INVALID_INPUT)
    return value


class TaskCatalogService:
    """Own create/get/reject and shared task-record CAS without loading CAD."""

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
        task_id: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
    ) -> StoredTaskRun:
        if type(reasoning_owner) is not ReasoningOwner or type(review_policy) is not ReviewPolicy:
            _raise(TaskCatalogErrorCode.INVALID_INPUT)
        if reasoning_owner is not ReasoningOwner.EXTERNAL_PLAN:
            _raise(TaskCatalogErrorCode.UNSUPPORTED_REASONING_OWNER)
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

    def get_task(self, *, task_id: str) -> StoredTaskRun:
        return self._load(task_id)

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
