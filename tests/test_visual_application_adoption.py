from __future__ import annotations

import dataclasses
import threading
from types import SimpleNamespace

import pytest

from tests.test_visual_service import _head, _invocation, _observation, _proposal
from vibecad.application.agent import AgentApplication
from vibecad.application.project_api import ProjectCurrentResult
from vibecad.application.task_api import (
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.application.visual_adoption import (
    ApplicationVisualAdoptionError,
    ApplicationVisualAdoptionErrorCode,
    ApplicationVisualAdoptionPort,
)
from vibecad.execution.revisions import ProjectHead, RevisionRef
from vibecad.visual.adoption import (
    VisualAdoptionAbsenceReceipt,
    VisualAdoptionPort,
    VisualAdoptionReceipt,
    VisualAdoptionWithdrawalReceipt,
    build_visual_adoption_request,
)
from vibecad.visual.drafts import BaseHeadBinding, derive_adoption_identity
from vibecad.workflow.state import (
    ReasoningOwner,
    ReviewPolicy,
    TaskEvent,
    TaskStatus,
    new_task_run,
    task_creation_identity,
    transition_task,
)
from vibecad.workflow.store import StoredTaskRun


def _request():
    image_set = SimpleNamespace(
        id="image_set_" + "7" * 32,
        manifest_sha256="8" * 64,
    )
    proposal = _proposal(_observation(_invocation(image_set)))
    head = _head()
    adoption_key, adoption_intent = derive_adoption_identity(
        proposal.observation.reconstruction_id,
        proposal.digest,
        head.sha256,
    )
    return build_visual_adoption_request(
        reconstruction_id=proposal.observation.reconstruction_id,
        adoption_key_sha256=adoption_key,
        adoption_intent_sha256=adoption_intent,
        base_head=head,
        proposal=proposal,
    )


def _project_current(binding=None) -> ProjectCurrentResult:
    selected = binding or _head()
    head = ProjectHead(
        project_id=selected.project_id,
        generation=selected.generation,
        revision_id=selected.revision_id,
        manifest_sha256=selected.manifest_sha256,
    )
    revision = RevisionRef(
        id=selected.revision_id,
        project_id=selected.project_id,
        base_revision=None,
        manifest_sha256=selected.manifest_sha256,
        model=None,
        artifacts=(),
    )
    return ProjectCurrentResult(
        project_id=selected.project_id,
        head=head,
        revision=revision,
    )


class _Backend:
    def __init__(self) -> None:
        self.current = _project_current()
        self.tasks: dict[str, StoredTaskRun] = {}
        self.create_calls: list[dict[str, object]] = []
        self.submit_calls: list[dict[str, object]] = []
        self.cancel_calls: list[dict[str, object]] = []


class _Application:
    def __init__(self, backend: _Backend) -> None:
        self._backend = backend

    def get_project(self, *, project_id: str):
        if self._backend.current.project_id != project_id:
            raise RuntimeError("not found")
        return self._backend.current

    def get_task(self, *, task_id: str):
        return self._backend.tasks.get(
            task_id,
            TaskServicePortFailure(code=TaskServicePortErrorCode.NOT_FOUND),
        )

    def create_task(
        self,
        *,
        create_key: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
    ):
        self._backend.create_calls.append(
            {
                "create_key": create_key,
                "project_id": project_id,
                "reasoning_owner": reasoning_owner,
                "review_policy": review_policy,
            }
        )
        task_id, creation_digest = task_creation_identity(create_key)
        existing = self._backend.tasks.get(task_id)
        if existing is not None:
            return existing
        task = transition_task(
            new_task_run(
                task_id=task_id,
                project_id=project_id,
                base_revision=self._backend.current.head.revision_id,
                reasoning_owner=reasoning_owner,
                review_policy=review_policy,
                creation_digest=creation_digest,
            ),
            TaskEvent.REQUEST_PLAN,
        )
        stored = StoredTaskRun(generation=0, task_run=task)
        self._backend.tasks[task_id] = stored
        return stored

    def submit_model_program(self, *, task_id: str, expected_generation: int, program):
        self._backend.submit_calls.append(
            {
                "task_id": task_id,
                "expected_generation": expected_generation,
                "program": program,
            }
        )
        stored = self._backend.tasks[task_id]
        if stored.generation != expected_generation:
            return TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
        submitted = StoredTaskRun(
            generation=stored.generation + 1,
            task_run=transition_task(
                stored.task_run,
                TaskEvent.SUBMIT_PROGRAM,
                program=program,
            ),
        )
        self._backend.tasks[task_id] = submitted
        return submitted

    def cancel_task(self, *, task_id: str, expected_generation: int):
        self._backend.cancel_calls.append(
            {
                "task_id": task_id,
                "expected_generation": expected_generation,
            }
        )
        stored = self._backend.tasks[task_id]
        if stored.generation != expected_generation:
            return TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT)
        cancelled = StoredTaskRun(
            generation=stored.generation + 1,
            task_run=transition_task(stored.task_run, TaskEvent.REQUEST_CANCEL),
        )
        self._backend.tasks[task_id] = cancelled
        return cancelled


def test_port_captures_head_and_creates_one_exact_review_task() -> None:
    request = _request()
    backend = _Backend()
    application = _Application(backend)
    port = ApplicationVisualAdoptionPort(application=application)

    assert isinstance(port, VisualAdoptionPort)
    assert port.inspect_head(request.base_head.project_id) == request.base_head
    before = backend.current.head

    receipt = port.ensure_review_task(request)

    assert type(receipt) is VisualAdoptionReceipt
    assert receipt.task_id == request.task_id
    assert receipt.adoption_intent_sha256 == request.adoption_intent_sha256
    assert receipt.base_head_sha256 == request.base_head.sha256
    assert receipt.program_sha256 == request.program_sha256
    assert backend.current.head == before
    assert backend.create_calls == [
        {
            "create_key": request.task_create_key,
            "project_id": request.base_head.project_id,
            "reasoning_owner": ReasoningOwner.EXTERNAL_PLAN,
            "review_policy": ReviewPolicy.REQUIRE_REVIEW,
        }
    ]
    assert backend.submit_calls == [
        {
            "task_id": request.task_id,
            "expected_generation": 0,
            "program": request.program,
        }
    ]
    stored = backend.tasks[request.task_id]
    assert stored.task_run.project_id == request.base_head.project_id
    assert stored.task_run.base_revision == request.base_head.revision_id
    assert stored.task_run.reasoning_owner is ReasoningOwner.EXTERNAL_PLAN
    assert stored.task_run.review_policy is ReviewPolicy.REQUIRE_REVIEW
    assert stored.task_run.program == request.program

    replay = port.ensure_review_task(request)

    assert replay == receipt
    assert len(backend.create_calls) == 1
    assert len(backend.submit_calls) == 1


def test_head_mismatch_fails_before_task_creation() -> None:
    request = _request()
    backend = _Backend()
    changed = dataclasses.replace(
        request.base_head,
        generation=request.base_head.generation + 1,
        sha256="",
    )
    backend.current = _project_current(changed)
    port = ApplicationVisualAdoptionPort(application=_Application(backend))

    with pytest.raises(ApplicationVisualAdoptionError) as caught:
        port.ensure_review_task(request)

    assert caught.value.code is ApplicationVisualAdoptionErrorCode.CONFLICT
    assert backend.tasks == {}
    assert backend.create_calls == []
    assert backend.submit_calls == []


def test_existing_deterministic_task_mismatch_fails_closed() -> None:
    request = _request()
    backend = _Backend()
    _task_id, creation_digest = task_creation_identity(request.task_create_key)
    wrong = transition_task(
        new_task_run(
            task_id=request.task_id,
            project_id=request.base_head.project_id,
            base_revision="revision_" + "9" * 32,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
            creation_digest=creation_digest,
        ),
        TaskEvent.REQUEST_PLAN,
    )
    backend.tasks[request.task_id] = StoredTaskRun(generation=0, task_run=wrong)
    port = ApplicationVisualAdoptionPort(application=_Application(backend))

    with pytest.raises(ApplicationVisualAdoptionError) as caught:
        port.ensure_review_task(request)

    assert caught.value.code is ApplicationVisualAdoptionErrorCode.CONFLICT
    assert backend.create_calls == []
    assert backend.submit_calls == []


def test_restart_reconcile_observes_exact_task_without_duplicate_effect() -> None:
    request = _request()
    backend = _Backend()
    first = ApplicationVisualAdoptionPort(application=_Application(backend))
    expected = first.ensure_review_task(request)
    create_count = len(backend.create_calls)
    submit_count = len(backend.submit_calls)

    restarted = ApplicationVisualAdoptionPort(application=_Application(backend))
    reconciled = restarted.reconcile_review_task(request)

    assert reconciled == expected
    assert len(backend.create_calls) == create_count
    assert len(backend.submit_calls) == submit_count
    assert len(backend.tasks) == 1


def test_reconcile_absence_is_read_only_and_exact_partial_task_is_completed() -> None:
    request = _request()
    backend = _Backend()
    port = ApplicationVisualAdoptionPort(application=_Application(backend))

    absent = port.reconcile_review_task(request)

    assert type(absent) is VisualAdoptionAbsenceReceipt
    assert absent.task_id == request.task_id
    assert backend.create_calls == []
    assert backend.submit_calls == []

    application = _Application(backend)
    created = application.create_task(
        create_key=request.task_create_key,
        project_id=request.base_head.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    assert type(created) is StoredTaskRun
    create_count = len(backend.create_calls)

    reconciled = port.reconcile_review_task(request)

    assert type(reconciled) is VisualAdoptionReceipt
    assert len(backend.create_calls) == create_count
    assert backend.submit_calls == [
        {
            "task_id": request.task_id,
            "expected_generation": 0,
            "program": request.program,
        }
    ]


def test_stale_head_exact_partial_task_is_cancelled_and_withdrawn_idempotently() -> None:
    request = _request()
    backend = _Backend()
    application = _Application(backend)
    created = application.create_task(
        create_key=request.task_create_key,
        project_id=request.base_head.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    assert type(created) is StoredTaskRun
    changed = dataclasses.replace(
        request.base_head,
        generation=request.base_head.generation + 1,
        sha256="",
    )
    backend.current = _project_current(changed)
    port = ApplicationVisualAdoptionPort(application=application)

    withdrawn = port.reconcile_review_task(request)

    assert type(withdrawn) is VisualAdoptionWithdrawalReceipt
    assert withdrawn.task_id == request.task_id
    assert withdrawn.cancelled_generation == 1
    assert backend.cancel_calls == [
        {
            "task_id": request.task_id,
            "expected_generation": 0,
        }
    ]
    assert backend.submit_calls == []
    stored = backend.tasks[request.task_id]
    assert stored.generation == 1
    assert stored.task_run.status is TaskStatus.CANCELLED
    assert stored.task_run.program is None

    replay = ApplicationVisualAdoptionPort(application=application).reconcile_review_task(request)

    assert replay == withdrawn
    assert len(backend.cancel_calls) == 1
    assert backend.submit_calls == []


def test_stale_head_never_cancels_an_exact_complete_program_task() -> None:
    request = _request()
    backend = _Backend()
    application = _Application(backend)
    port = ApplicationVisualAdoptionPort(application=application)
    expected = port.ensure_review_task(request)
    changed = dataclasses.replace(
        request.base_head,
        generation=request.base_head.generation + 1,
        sha256="",
    )
    backend.current = _project_current(changed)

    reconciled = port.reconcile_review_task(request)

    assert reconciled == expected
    assert backend.cancel_calls == []
    assert backend.tasks[request.task_id].task_run.program == request.program


def test_partial_task_is_not_cancelled_when_current_head_is_unavailable() -> None:
    request = _request()
    backend = _Backend()
    seed = _Application(backend)
    created = seed.create_task(
        create_key=request.task_create_key,
        project_id=request.base_head.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    assert type(created) is StoredTaskRun

    class UnavailableHeadApplication(_Application):
        def get_project(self, *, project_id: str):
            raise RuntimeError("temporarily unavailable")

    port = ApplicationVisualAdoptionPort(application=UnavailableHeadApplication(backend))

    assert port.reconcile_review_task(request) is None
    assert backend.cancel_calls == []
    assert backend.submit_calls == []
    assert backend.tasks[request.task_id] == created


def test_concurrent_exact_submit_wins_cancel_cas_and_is_never_withdrawn() -> None:
    request = _request()
    backend = _Backend()
    seed = _Application(backend)
    created = seed.create_task(
        create_key=request.task_create_key,
        project_id=request.base_head.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    assert type(created) is StoredTaskRun
    changed = dataclasses.replace(
        request.base_head,
        generation=request.base_head.generation + 1,
        sha256="",
    )
    backend.current = _project_current(changed)

    class SubmitWinsApplication(_Application):
        def cancel_task(self, *, task_id: str, expected_generation: int):
            submitted = self.submit_model_program(
                task_id=task_id,
                expected_generation=expected_generation,
                program=request.program,
            )
            assert type(submitted) is StoredTaskRun
            return super().cancel_task(
                task_id=task_id,
                expected_generation=expected_generation,
            )

    port = ApplicationVisualAdoptionPort(application=SubmitWinsApplication(backend))

    reconciled = port.reconcile_review_task(request)

    assert type(reconciled) is VisualAdoptionReceipt
    assert backend.tasks[request.task_id].task_run.program == request.program
    assert backend.tasks[request.task_id].task_run.status is TaskStatus.PROGRAM_READY
    assert backend.tasks[request.task_id].generation == 1


def test_lost_cancel_response_is_recovered_by_exact_readback() -> None:
    request = _request()
    backend = _Backend()
    seed = _Application(backend)
    created = seed.create_task(
        create_key=request.task_create_key,
        project_id=request.base_head.project_id,
        reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
        review_policy=ReviewPolicy.REQUIRE_REVIEW,
    )
    assert type(created) is StoredTaskRun
    changed = dataclasses.replace(
        request.base_head,
        generation=request.base_head.generation + 1,
        sha256="",
    )
    backend.current = _project_current(changed)

    class LostResponseApplication(_Application):
        def cancel_task(self, *, task_id: str, expected_generation: int):
            super().cancel_task(
                task_id=task_id,
                expected_generation=expected_generation,
            )
            raise RuntimeError("response lost after durable cancel")

    port = ApplicationVisualAdoptionPort(application=LostResponseApplication(backend))

    withdrawn = port.reconcile_review_task(request)

    assert type(withdrawn) is VisualAdoptionWithdrawalReceipt
    assert withdrawn.cancelled_generation == 1
    assert backend.tasks[request.task_id].task_run.status is TaskStatus.CANCELLED
    assert backend.tasks[request.task_id].task_run.program is None


def test_real_application_cancels_only_the_exact_stale_head_partial_task(
    tmp_path,
    monkeypatch,
) -> None:
    application = AgentApplication.open(data_root=tmp_path / "data")
    try:
        project = application.bootstrap_empty()
        current = application.get_project(project_id=project.head.project_id)
        assert type(current) is ProjectCurrentResult
        base_head = BaseHeadBinding(
            project_id=current.head.project_id,
            generation=current.head.generation,
            revision_id=current.head.revision_id,
            manifest_sha256=current.head.manifest_sha256,
        )
        image_set = SimpleNamespace(
            id="image_set_" + "7" * 32,
            manifest_sha256="8" * 64,
        )
        proposal = _proposal(_observation(_invocation(image_set)))
        adoption_key, adoption_intent = derive_adoption_identity(
            proposal.observation.reconstruction_id,
            proposal.digest,
            base_head.sha256,
        )
        request = build_visual_adoption_request(
            reconstruction_id=proposal.observation.reconstruction_id,
            adoption_key_sha256=adoption_key,
            adoption_intent_sha256=adoption_intent,
            base_head=base_head,
            proposal=proposal,
        )
        created = application.create_task(
            create_key=request.task_create_key,
            project_id=request.base_head.project_id,
            reasoning_owner=ReasoningOwner.EXTERNAL_PLAN,
            review_policy=ReviewPolicy.REQUIRE_REVIEW,
        )
        assert type(created) is StoredTaskRun
        changed = ProjectCurrentResult(
            project_id=current.project_id,
            head=dataclasses.replace(current.head, generation=current.head.generation + 1),
            revision=current.revision,
        )

        def changed_current(self, *, project_id: str):
            assert project_id == changed.project_id
            return changed

        monkeypatch.setattr(AgentApplication, "get_project", changed_current)
        port = ApplicationVisualAdoptionPort(application=application)

        withdrawn = port.reconcile_review_task(request)

        assert type(withdrawn) is VisualAdoptionWithdrawalReceipt
        stored = application.get_task(task_id=request.task_id)
        assert type(stored) is StoredTaskRun
        assert stored.generation == 1
        assert stored.task_run.status is TaskStatus.CANCELLED
        assert stored.task_run.program is None
    finally:
        application.close()


def test_reconcile_waits_for_inflight_ensure_before_proving_absence() -> None:
    request = _request()
    backend = _Backend()
    create_entered = threading.Event()
    allow_create = threading.Event()

    class PausingApplication(_Application):
        def create_task(self, **kwargs):
            create_entered.set()
            if not allow_create.wait(timeout=5):
                raise RuntimeError("test create timeout")
            return super().create_task(**kwargs)

    port = ApplicationVisualAdoptionPort(application=PausingApplication(backend))
    outcomes: dict[str, object] = {}
    reconcile_started = threading.Event()
    reconcile_finished = threading.Event()

    def ensure() -> None:
        outcomes["ensure"] = port.ensure_review_task(request)

    def reconcile() -> None:
        reconcile_started.set()
        outcomes["reconcile"] = port.reconcile_review_task(request)
        reconcile_finished.set()

    ensure_thread = threading.Thread(target=ensure)
    reconcile_thread = threading.Thread(target=reconcile)
    ensure_thread.start()
    assert create_entered.wait(timeout=5)
    reconcile_thread.start()
    assert reconcile_started.wait(timeout=5)
    assert not reconcile_finished.wait(timeout=0.1)

    allow_create.set()
    ensure_thread.join(timeout=5)
    reconcile_thread.join(timeout=5)

    assert not ensure_thread.is_alive()
    assert not reconcile_thread.is_alive()
    assert type(outcomes["ensure"]) is VisualAdoptionReceipt
    assert outcomes["reconcile"] == outcomes["ensure"]
    assert len(backend.create_calls) == 1
    assert len(backend.submit_calls) == 1


def test_inspect_head_rejects_incoherent_application_snapshot() -> None:
    request = _request()
    backend = _Backend()
    backend.current = ProjectCurrentResult(
        project_id=backend.current.project_id,
        head=backend.current.head,
        revision=dataclasses.replace(
            backend.current.revision,
            id="revision_" + "a" * 32,
        ),
    )
    port = ApplicationVisualAdoptionPort(application=_Application(backend))

    with pytest.raises(ApplicationVisualAdoptionError) as caught:
        port.inspect_head(request.base_head.project_id)

    assert caught.value.code is ApplicationVisualAdoptionErrorCode.INTEGRITY_FAILURE
