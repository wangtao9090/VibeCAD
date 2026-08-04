"""Trusted application bridge for adopting a visual reconstruction proposal.

The bridge deliberately wraps the process-owned application instead of exposing
task authority to a visual provider.  It creates one ordinary, deterministic
``EXTERNAL_PLAN`` task under ``REQUIRE_REVIEW`` and proves the exact project,
base revision, and hidden model program before returning an adoption receipt.
"""

from __future__ import annotations

import re
import threading
from enum import StrEnum

from vibecad.application.project_api import (
    ProjectCurrentResult,
    ProjectServicePortFailure,
)
from vibecad.application.task_api import (
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.visual.adoption import (
    VisualAdoptionAbsenceReceipt,
    VisualAdoptionReceipt,
    VisualAdoptionRequest,
    VisualAdoptionWithdrawalReceipt,
)
from vibecad.visual.drafts import BaseHeadBinding
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskStatus,
    task_creation_identity,
)
from vibecad.workflow.store import StoredTaskRun

__all__ = (
    "ApplicationVisualAdoptionError",
    "ApplicationVisualAdoptionErrorCode",
    "ApplicationVisualAdoptionPort",
)

_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_ABSENT = object()
_UNKNOWN = object()


class _ExactTaskState(StrEnum):
    PARTIAL = "partial"
    COMPLETE = "complete"
    WITHDRAWN = "withdrawn"


class _HeadComparison(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class ApplicationVisualAdoptionErrorCode(StrEnum):
    """Bounded failures raised for definite application-contract violations."""

    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"
    INTEGRITY_FAILURE = "integrity_failure"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"


class ApplicationVisualAdoptionError(RuntimeError):
    """Path-free failure from the trusted adoption bridge."""

    __slots__ = ("code",)

    def __init__(self, code: ApplicationVisualAdoptionErrorCode) -> None:
        if type(code) is not ApplicationVisualAdoptionErrorCode:
            raise TypeError("code must be an exact ApplicationVisualAdoptionErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: ApplicationVisualAdoptionErrorCode) -> None:
    raise ApplicationVisualAdoptionError(code)


def _receipt(request: VisualAdoptionRequest) -> VisualAdoptionReceipt:
    return VisualAdoptionReceipt(
        task_id=request.task_id,
        adoption_intent_sha256=request.adoption_intent_sha256,
        base_head_sha256=request.base_head.sha256,
        program_sha256=request.program_sha256,
    )


def _absence_receipt(request: VisualAdoptionRequest) -> VisualAdoptionAbsenceReceipt:
    return VisualAdoptionAbsenceReceipt(
        task_id=request.task_id,
        adoption_intent_sha256=request.adoption_intent_sha256,
        base_head_sha256=request.base_head.sha256,
        program_sha256=request.program_sha256,
    )


def _withdrawal_receipt(
    request: VisualAdoptionRequest,
    *,
    cancelled_generation: int,
) -> VisualAdoptionWithdrawalReceipt:
    return VisualAdoptionWithdrawalReceipt(
        task_id=request.task_id,
        adoption_intent_sha256=request.adoption_intent_sha256,
        base_head_sha256=request.base_head.sha256,
        program_sha256=request.program_sha256,
        cancelled_generation=cancelled_generation,
    )


class ApplicationVisualAdoptionPort:
    """Application-owned implementation of the visual adoption authority.

    ``application`` is the process composition root.  Keeping it as a single
    injected object lets :class:`AgentApplication` satisfy the bridge directly
    without making this module import that concrete class and creating a
    composition-cycle dependency.
    """

    __slots__ = ("_adoption_lock", "_application")

    def __init__(self, *, application: object) -> None:
        required = (
            "get_project",
            "create_task",
            "get_task",
            "submit_model_program",
            "cancel_task",
        )
        if application is None or any(
            not callable(getattr(application, name, None)) for name in required
        ):
            raise TypeError("invalid visual adoption application composition")
        self._application = application
        # The local daemon dispatches independent connections on independent
        # threads.  Keep ensure/reconcile ordered so an absence receipt can
        # only be issued after an in-flight ensure attempt has settled.
        self._adoption_lock = threading.Lock()

    def inspect_head(self, project_id: str) -> BaseHeadBinding:
        """Capture one coherent, application-validated project HEAD."""

        if type(project_id) is not str or _PROJECT_ID.fullmatch(project_id) is None:
            _fail(ApplicationVisualAdoptionErrorCode.INVALID_INPUT)
        try:
            current = self._application.get_project(project_id=project_id)
        except Exception:
            _fail(ApplicationVisualAdoptionErrorCode.UPSTREAM_UNAVAILABLE)
        if type(current) is ProjectServicePortFailure:
            _fail(ApplicationVisualAdoptionErrorCode.UPSTREAM_UNAVAILABLE)
        if type(current) is not ProjectCurrentResult:
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        head = current.head
        revision = current.revision
        if not (
            current.project_id == project_id
            and head.project_id == project_id
            and revision.project_id == project_id
            and head.revision_id == revision.id
            and head.manifest_sha256 == revision.manifest_sha256
        ):
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        try:
            return BaseHeadBinding(
                project_id=head.project_id,
                generation=head.generation,
                revision_id=head.revision_id,
                manifest_sha256=head.manifest_sha256,
            )
        except Exception:
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)

    def _observe_task(self, task_id: str) -> StoredTaskRun | object:
        try:
            result = self._application.get_task(task_id=task_id)
        except Exception:
            return _UNKNOWN
        if type(result) is StoredTaskRun:
            return result
        if type(result) is TaskServicePortFailure:
            if result.code is TaskServicePortErrorCode.NOT_FOUND:
                return _ABSENT
            return _UNKNOWN
        _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)

    @staticmethod
    def _classify_task(
        request: VisualAdoptionRequest,
        stored: StoredTaskRun,
    ) -> _ExactTaskState:
        """Validate exact identity and classify its only admitted durable states."""

        try:
            expected_task_id, creation_digest = task_creation_identity(request.task_create_key)
        except Exception:
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        task = stored.task_run
        if not (
            expected_task_id == request.task_id
            and task.id == request.task_id
            and task.project_id == request.base_head.project_id
            and task.base_revision == request.base_head.revision_id
            and task.creation_digest == creation_digest
            and task.reasoning_owner is ReasoningOwner.EXTERNAL_PLAN
            and task.review_policy is ReviewPolicy.REQUIRE_REVIEW
        ):
            _fail(ApplicationVisualAdoptionErrorCode.CONFLICT)
        if task.program is None:
            if task.status is TaskStatus.NEEDS_PLAN and stored.generation == 0:
                return _ExactTaskState.PARTIAL
            if (
                task.status is TaskStatus.CANCELLED
                and stored.generation == 1
                and task.transitions
                and task.transitions[-1].event is TaskEvent.REQUEST_CANCEL
                and task.transitions[-1].from_status is TaskStatus.NEEDS_PLAN
                and task.transitions[-1].to_status is TaskStatus.CANCELLED
            ):
                return _ExactTaskState.WITHDRAWN
            _fail(ApplicationVisualAdoptionErrorCode.CONFLICT)
        if task.program != request.program:
            _fail(ApplicationVisualAdoptionErrorCode.CONFLICT)
        return _ExactTaskState.COMPLETE

    def _compare_head(self, request: VisualAdoptionRequest) -> _HeadComparison:
        try:
            current = self.inspect_head(request.base_head.project_id)
        except ApplicationVisualAdoptionError as error:
            if error.code is ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE:
                raise
            return _HeadComparison.UNKNOWN
        if current == request.base_head:
            return _HeadComparison.MATCH
        return _HeadComparison.MISMATCH

    def _matching_head(self, request: VisualAdoptionRequest) -> bool:
        return self._compare_head(request) is _HeadComparison.MATCH

    def _submit(
        self,
        request: VisualAdoptionRequest,
        stored: StoredTaskRun,
    ) -> VisualAdoptionReceipt | None:
        try:
            result = self._application.submit_model_program(
                task_id=request.task_id,
                expected_generation=stored.generation,
                program=request.program,
            )
        except Exception:
            result = _UNKNOWN
        if type(result) is StoredTaskRun:
            if self._classify_task(request, result) is not _ExactTaskState.COMPLETE:
                return None
            submitted = result
        elif type(result) is TaskServicePortFailure or result is _UNKNOWN:
            observed = self._observe_task(request.task_id)
            if type(observed) is not StoredTaskRun:
                return None
            if self._classify_task(request, observed) is not _ExactTaskState.COMPLETE:
                return None
            submitted = observed
        else:
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        # A REQUIRE_REVIEW submission must not advance committed project HEAD.
        # A mismatch may be an external edit, so preserve recovery instead of
        # making a false attribution or success claim.
        if not self._matching_head(request):
            return None
        if submitted.task_run.program != request.program:  # pragma: no cover - guarded above.
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        return _receipt(request)

    def _withdraw_partial_task(
        self,
        request: VisualAdoptionRequest,
        stored: StoredTaskRun,
    ) -> VisualAdoptionReceipt | VisualAdoptionWithdrawalReceipt | None:
        """Cancel only the exact generation-zero program-free Task and prove it."""

        if self._classify_task(request, stored) is not _ExactTaskState.PARTIAL:
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        try:
            result = self._application.cancel_task(
                task_id=request.task_id,
                expected_generation=stored.generation,
            )
        except Exception:
            result = _UNKNOWN
        if type(result) is StoredTaskRun:
            observed = result
        elif type(result) is TaskServicePortFailure or result is _UNKNOWN:
            observed = self._observe_task(request.task_id)
            if type(observed) is not StoredTaskRun:
                return None
        else:
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        state = self._classify_task(request, observed)
        if state is _ExactTaskState.COMPLETE:
            # A concurrent exact submit may win the generation-zero CAS.  It
            # must be adopted and is never cancelled as a withdrawal.
            return _receipt(request)
        if state is _ExactTaskState.WITHDRAWN:
            return _withdrawal_receipt(
                request,
                cancelled_generation=observed.generation,
            )
        return None

    def ensure_review_task(
        self,
        request: VisualAdoptionRequest,
    ) -> VisualAdoptionReceipt | None:
        """Idempotently create and submit the exact ordinary review task."""

        if type(request) is not VisualAdoptionRequest:
            _fail(ApplicationVisualAdoptionErrorCode.INVALID_INPUT)

        with self._adoption_lock:
            return self._ensure_review_task_locked(request)

    def _ensure_review_task_locked(
        self,
        request: VisualAdoptionRequest,
    ) -> VisualAdoptionReceipt | None:
        """Run one ensure attempt while absence proofs are fenced out."""

        existing = self._observe_task(request.task_id)
        if type(existing) is StoredTaskRun:
            state = self._classify_task(request, existing)
            if state is _ExactTaskState.COMPLETE:
                return _receipt(request)
            if state is _ExactTaskState.WITHDRAWN:
                _fail(ApplicationVisualAdoptionErrorCode.CONFLICT)
            if not self._matching_head(request):
                _fail(ApplicationVisualAdoptionErrorCode.CONFLICT)
            return self._submit(request, existing)
        if existing is _UNKNOWN:
            return None
        if existing is not _ABSENT:  # pragma: no cover - closed internal sentinel set.
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        if not self._matching_head(request):
            _fail(ApplicationVisualAdoptionErrorCode.CONFLICT)

        try:
            created = self._application.create_task(
                create_key=request.task_create_key,
                project_id=request.base_head.project_id,
                reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
                review_policy=ReviewPolicy.REQUIRE_REVIEW,
            )
        except Exception:
            created = _UNKNOWN
        if type(created) is not StoredTaskRun:
            if type(created) is not TaskServicePortFailure and created is not _UNKNOWN:
                _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
            observed = self._observe_task(request.task_id)
            if type(observed) is not StoredTaskRun:
                return None
            created = observed
        state = self._classify_task(request, created)
        if state is _ExactTaskState.COMPLETE:
            return _receipt(request)
        if state is _ExactTaskState.WITHDRAWN:
            _fail(ApplicationVisualAdoptionErrorCode.CONFLICT)
        return self._submit(request, created)

    def reconcile_review_task(
        self,
        request: VisualAdoptionRequest,
    ) -> (
        VisualAdoptionReceipt
        | VisualAdoptionAbsenceReceipt
        | VisualAdoptionWithdrawalReceipt
        | None
    ):
        """Reconcile a settled ensure attempt without recreating an absent Task."""

        if type(request) is not VisualAdoptionRequest:
            _fail(ApplicationVisualAdoptionErrorCode.INVALID_INPUT)

        with self._adoption_lock:
            return self._reconcile_review_task_locked(request)

    def _reconcile_review_task_locked(
        self,
        request: VisualAdoptionRequest,
    ) -> (
        VisualAdoptionReceipt
        | VisualAdoptionAbsenceReceipt
        | VisualAdoptionWithdrawalReceipt
        | None
    ):
        """Observe settled state and finish only an exact durable partial Task."""

        observed = self._observe_task(request.task_id)
        if observed is _ABSENT:
            # The adoption lock fences same-process ensure calls.  After a
            # process restart, the old process can no longer publish anything,
            # so exact NOT_FOUND is likewise a settled absence proof.
            return _absence_receipt(request)
        if observed is _UNKNOWN:
            return None
        if type(observed) is not StoredTaskRun:  # pragma: no cover - closed sentinel set.
            _fail(ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE)
        state = self._classify_task(request, observed)
        if state is _ExactTaskState.COMPLETE:
            return _receipt(request)
        if state is _ExactTaskState.WITHDRAWN:
            return _withdrawal_receipt(
                request,
                cancelled_generation=observed.generation,
            )
        # A crash may land after deterministic Task creation but before the
        # exact hidden program is submitted.  Completing that already durable,
        # generation-pinned Task is safe and does not replay an unknown create.
        head = self._compare_head(request)
        if head is _HeadComparison.MATCH:
            return self._submit(request, observed)
        if head is _HeadComparison.MISMATCH:
            return self._withdraw_partial_task(request, observed)
        return None
