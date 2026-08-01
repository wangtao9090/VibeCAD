from __future__ import annotations

import threading
from copy import deepcopy

import pytest

from tests import test_freecad_workbench_controller as controller
from tests.fixtures.freecad_workbench.fake_host import (
    force_cleanup_workbench,
    pump_main_events,
)

TASK_ID = "task_" + "1" * 32
PROJECT_ID = "project_" + "1" * 32


def _task_detail(*, generation: int = 4) -> dict[str, object]:
    return {
        "schema_version": 1,
        "ok": True,
        "result": {
            "generation": generation,
            "next_action": "review",
            "task_run": {
                "schema_version": 1,
                "id": TASK_ID,
                "project_id": PROJECT_ID,
                "base_revision": "revision_" + "1" * 32,
                "reasoning_owner": "server",
                "review_policy": "required",
                "status": "awaiting_user_review",
                "creation_digest": "a" * 64,
                "program": None,
                "candidate_revision": "revision_" + "4" * 32,
                "committed_revision": None,
                "draft": {
                    "schema_version": 1,
                    "id": "draft_" + "4" * 32,
                    "task_id": TASK_ID,
                    "project_id": PROJECT_ID,
                    "base_revision": "revision_" + "1" * 32,
                    "base_generation": 1,
                    "base_manifest_sha256": "b" * 64,
                    "revision_id": "revision_" + "4" * 32,
                    "manifest_sha256": "c" * 64,
                    "verification_id": "verification_" + "d" * 32,
                    "acceptance_id": "acceptance-c03",
                    "observation_digest": "e" * 64,
                },
                "steps": [],
                "verification_reports": [],
                "artifacts": [],
                "last_error": None,
                "transitions": [],
            },
        },
        "error": None,
    }


def test_c03_refresh_task_projects_fresh_authority_before_review_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]

    def fresh_task(request: dict[str, object]) -> dict[str, object]:
        client._record("get_task", request)
        return _task_detail()

    client.get_task_request = fresh_task
    try:
        controller._fix04_refresh_cycle(host, dock, client)
        selected = dock._selected_task()

        assert dock._task_value(selected, "generation") == 4
        assert dock.preview_projection().review_eligible is False
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize(
    "failure",
    ("malformed", "identity", "terminal", "same-generation-conflict"),
)
def test_c03_invalid_fresh_task_cannot_leave_or_resurrect_review_authority(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    host, freecad, clients, _events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    response = _task_detail(generation=3)
    task_run = response["result"]["task_run"]
    assert type(task_run) is dict
    if failure == "malformed":
        task_run.pop("draft")
    elif failure == "identity":
        task_run["id"] = "task_" + "2" * 32
        task_run["project_id"] = "project_" + "2" * 32
    elif failure == "terminal":
        task_run["status"] = "rejected"
        response["result"]["next_action"] = "none"
    else:
        task_run["base_revision"] = "revision_" + "2" * 32
        draft = task_run["draft"]
        assert type(draft) is dict
        draft["base_revision"] = "revision_" + "2" * 32
    detached = deepcopy(response)

    def invalid_task(request: dict[str, object]) -> dict[str, object]:
        client._record("get_task", request)
        return detached

    client.get_task_request = invalid_task
    review_calls = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
    preview_error = controller.importlib.import_module("vibecad_workbench.preview").PreviewError
    try:
        controller._fix04_refresh_cycle(host, dock, client)

        assert dock._selected_task() is None
        assert dock.preview_projection().review_eligible is False
        assert session._review_tokens == {}
        with pytest.raises(preview_error):
            controller._fix04_request_review(session, decision="accept")
        controller._fix04_settle_worker(session)
        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == review_calls
        )
    finally:
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize("decision", ("accept", "reject"))
def test_c03_decision_buttons_dispatch_one_exact_private_review(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    host, freecad, clients, _events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    entered = threading.Event()
    release = threading.Event()
    client.review_entered = entered
    client.review_release = release
    try:
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        controller._fix04_refresh_cycle(host, dock, client)
        assert dock.accept_button.enabled is True
        assert dock.reject_button.enabled is True
        button = dock.accept_button if decision == "accept" else dock.reject_button
        other = dock.reject_button if decision == "accept" else dock.accept_button

        button.click()
        assert entered.wait(1.0)
        button.click()
        other.click()

        pending_ids = controller._fix04_pending_ids(session, dock, "review")
        assert len(pending_ids) == 1
        pending = session._pending[pending_ids[0]]
        assert pending[2] == "review"
        assert pending[3] != "normal"
        assert pending[4] == {
            "schema_version": 1,
            "request_id": pending_ids[0],
            "kind": "review",
            "decision": decision,
            "task_id": TASK_ID,
            "draft_id": "draft_" + "4" * 32,
            "expected_generation": 3,
        }
        assert sum(call[0] == f"{decision}_draft" for call in client.calls) == 1
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        local_pending = dock._review_pending
        assert local_pending is not None
        assert local_pending[0] == pending_ids[0]
        assert local_pending[1] == decision
        assert local_pending[2][:4] == (PROJECT_ID, TASK_ID, "draft_" + "4" * 32, 3)
        assert local_pending[3] == (
            dock._preview_checkouts["head"],
            dock._preview_checkouts["draft"],
        )

        release.set()
        controller._fix04_settle_worker(session)
        pump_main_events(lambda: not session._has_pending_review())
        commands = controller._fix04_authenticated_review_commands(session)
        assert len(commands) == 1
        assert commands[0] == pending[4]
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
    finally:
        release.set()
        force_cleanup_workbench(host, freecad)


@pytest.mark.parametrize("decision", ("accept", "reject"))
def test_c03_known_success_confirms_durable_state_and_cleans_captured_previews(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    host, freecad, clients, events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    controller._fix04_refresh_cycle(host, dock, client)
    captured = tuple(dock._preview_checkouts[kind] for kind in ("head", "draft"))
    task_reads = sum(call[0] == "get_task" for call in client.calls)
    project_reads = sum(call[0] == "get_project" for call in client.calls)
    event_offset = len(events)
    try:
        (dock.accept_button if decision == "accept" else dock.reject_button).click()
        pump_main_events(
            lambda: (
                sum(call[0] == "close_checkout" for call in client.calls) == 2
                and sum(call[0] == "get_task" for call in client.calls) == task_reads + 1
                and sum(call[0] == "get_project" for call in client.calls) == project_reads + 1
            )
        )

        assert sum(call[0] == f"{decision}_draft" for call in client.calls) == 1
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ] == list(captured)
        assert events[event_offset : event_offset + 4] == [
            "document.close",
            "document.close",
            f"checkout.close:{captured[0]}",
            f"checkout.close:{captured[1]}",
        ]
        assert freecad.documents == {}
        assert client.review_task_generation == 4
        assert client.review_task_status == ("succeeded" if decision == "accept" else "rejected")
        assert client.review_committed_revision == (
            "revision_" + "4" * 32 if decision == "accept" else None
        )
        assert client.review_project_revision == (
            "revision_" + "4" * 32 if decision == "accept" else "revision_" + "1" * 32
        )
        assert dock.review_status_label.text == f"Draft {decision}ed"
        assert dock._review_pending is None
        assert dock._selected_task() is None
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
    finally:
        force_cleanup_workbench(host, freecad)


def test_c03_unknown_review_outcome_is_terminal_without_replay_or_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    controller._fix04_refresh_cycle(host, dock, client)
    captured = tuple(dock._preview_checkouts[kind] for kind in ("head", "draft"))
    task_reads = sum(call[0] == "get_task" for call in client.calls)
    project_reads = sum(call[0] == "get_project" for call in client.calls)
    checkout_closes = sum(call[0] == "close_checkout" for call in client.calls)
    event_offset = len(events)
    client.review_failure = True
    try:
        dock.accept_button.click()
        pump_main_events(
            lambda: (
                dock.review_status_label.text == "Review outcome unknown"
                and sum(call[0] == "close_checkout" for call in client.calls) == checkout_closes + 2
            )
        )

        assert sum(call[0] == "accept_draft" for call in client.calls) == 1
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert sum(call[0] == "get_task" for call in client.calls) == task_reads
        assert sum(call[0] == "get_project" for call in client.calls) == project_reads
        closed = [call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"]
        assert closed == list(captured)
        assert events[event_offset : event_offset + 4] == [
            "document.close",
            "document.close",
            f"checkout.close:{captured[0]}",
            f"checkout.close:{captured[1]}",
        ]
        assert freecad.documents == {}
        assert dock._review_pending is None
        assert dock._review_confirmation is None
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        assert dock.preview_projection().recovery_required is True
        assert client.review_task_generation == 3
        assert client.review_task_status == "awaiting_user_review"
        assert client.review_committed_revision is None
        assert client.review_project_generation == 2
        assert client.review_project_revision == "revision_" + "1" * 32

        dock.accept_button.click()
        dock.reject_button.click()
        controller._fix04_settle_worker(session)
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert sum(call[0] == "get_task" for call in client.calls) == task_reads
        assert sum(call[0] == "get_project" for call in client.calls) == project_reads
    finally:
        force_cleanup_workbench(host, freecad)


def test_c03_effect_then_response_loss_remains_terminal_unknown_without_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    controller._fix04_refresh_cycle(host, dock, client)
    captured = tuple(dock._preview_checkouts[kind] for kind in ("head", "draft"))
    task_reads = sum(call[0] == "get_task" for call in client.calls)
    project_reads = sum(call[0] == "get_project" for call in client.calls)
    event_offset = len(events)
    client.review_effect_after_loss = True
    try:
        dock.accept_button.click()
        pump_main_events(
            lambda: (
                dock.review_status_label.text == "Review outcome unknown"
                and sum(call[0] == "close_checkout" for call in client.calls) == 2
            )
        )

        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert sum(call[0] == "get_task" for call in client.calls) == task_reads
        assert sum(call[0] == "get_project" for call in client.calls) == project_reads
        assert [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ] == list(captured)
        assert events[event_offset : event_offset + 4] == [
            "document.close",
            "document.close",
            f"checkout.close:{captured[0]}",
            f"checkout.close:{captured[1]}",
        ]
        assert client.review_task_generation == 4
        assert client.review_task_status == "succeeded"
        assert client.review_committed_revision == "revision_" + "4" * 32
        assert client.review_project_generation == 3
        assert client.review_project_revision == "revision_" + "4" * 32
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        assert dock.preview_projection().recovery_required is True

        dock.accept_button.click()
        dock.reject_button.click()
        controller._fix04_settle_worker(session)
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert sum(call[0] == "get_task" for call in client.calls) == task_reads
        assert sum(call[0] == "get_project" for call in client.calls) == project_reads
    finally:
        force_cleanup_workbench(host, freecad)


def test_c03_malformed_success_confirmation_fails_closed_after_preview_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    controller._fix04_refresh_cycle(host, dock, client)
    captured = tuple(dock._preview_checkouts[kind] for kind in ("head", "draft"))
    task_reads = sum(call[0] == "get_task" for call in client.calls)
    project_reads = sum(call[0] == "get_project" for call in client.calls)
    event_offset = len(events)
    malformed = _task_detail(generation=4)
    task_run = malformed["result"]["task_run"]
    assert type(task_run) is dict
    task_run.pop("draft")

    def malformed_task(request: dict[str, object]) -> dict[str, object]:
        client._record("get_task", request)
        return malformed

    client.get_task_request = malformed_task
    try:
        dock.accept_button.click()
        pump_main_events(
            lambda: (
                dock.review_status_label.text == "Review confirmation failed"
                and sum(call[0] == "close_checkout" for call in client.calls) == 2
                and sum(call[0] == "get_task" for call in client.calls) == task_reads + 1
                and sum(call[0] == "get_project" for call in client.calls) == project_reads + 1
            )
        )

        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ] == list(captured)
        assert events[event_offset : event_offset + 4] == [
            "document.close",
            "document.close",
            f"checkout.close:{captured[0]}",
            f"checkout.close:{captured[1]}",
        ]
        assert freecad.documents == {}
        assert dock._review_pending is None
        assert dock._review_confirmation is None
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        assert dock.preview_projection().recovery_required is True
    finally:
        force_cleanup_workbench(host, freecad)


def test_c03_ready_then_touched_document_clicks_no_review_and_enters_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    assert session.preview is not None
    dock = session.dock
    client = clients[0]
    controller._fix04_refresh_cycle(host, dock, client)
    captured = tuple(dock._preview_checkouts[kind] for kind in ("head", "draft"))
    head = session.preview.binding_for_checkout(captured[0])
    head.document.Modified = True
    review_calls = sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
    try:
        dock.accept_button.click()
        pump_main_events(lambda: sum(call[0] == "close_checkout" for call in client.calls) == 2)

        assert (
            sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
            == review_calls
        )
        assert [
            call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"
        ] == list(captured)
        assert dock.review_status_label.text == "Review outcome unknown"
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        assert dock.preview_projection().review_eligible is False
        assert dock.preview_projection().recovery_required is True
    finally:
        force_cleanup_workbench(host, freecad)


def test_c03_stale_review_completion_cannot_retire_current_pending_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    controller._fix04_refresh_cycle(host, dock, client)
    review_context = dock._review_context()
    assert review_context is not None
    selection_stamp, checkout_ids, revisions = review_context
    pending = (101, "accept", selection_stamp, checkout_ids, revisions)
    dock._review_pending = pending
    dock.review_status_label.setText("Review pending")
    dock._update_preview_actions()
    calls_before = list(client.calls)
    projection_before = dock.preview_projection()
    checkouts_before = dict(dock._preview_checkouts)
    try:
        dock._receive_host_review_completion(
            {
                "schema_version": 1,
                "request_id": 100,
                "kind": "error",
                "operation": "review",
                "code": "closed",
                "outcome": "unknown_outcome",
            },
            selection_stamp,
        )

        assert dock._review_pending is pending
        assert dock._review_confirmation is None
        assert dock.review_status_label.text == "Review pending"
        assert dock.preview_projection() == projection_before
        assert dock._preview_checkouts == checkouts_before
        assert client.calls == calls_before
        assert not any(call[0] in {"accept_draft", "reject_draft"} for call in client.calls)
        assert not any(call[0] == "close_checkout" for call in client.calls)
    finally:
        dock._review_pending = None
        force_cleanup_workbench(host, freecad)


def test_c03_known_success_attempts_both_captured_discards_when_first_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, freecad, clients, _events = controller._start_fail_cleanup_host(monkeypatch)
    session = host._session
    assert session is not None
    assert session.dock is not None
    dock = session.dock
    client = clients[0]
    controller._fix04_refresh_cycle(host, dock, client)
    captured = tuple(dock._preview_checkouts[kind] for kind in ("head", "draft"))
    discard = dock._review_host_discard
    assert callable(discard)
    attempts: list[str] = []

    def fail_first(checkout_id: str) -> None:
        attempts.append(checkout_id)
        if checkout_id == captured[0]:
            raise RuntimeError("synthetic first captured discard failure")
        discard(checkout_id)

    dock._review_host_discard = fail_first
    try:
        dock.accept_button.click()
        pump_main_events(
            lambda: (
                attempts == list(captured)
                and dock.review_status_label.text == "Review confirmation failed"
                and sum(call[0] == "close_checkout" for call in client.calls) == 1
            )
        )

        assert sum(call[0] == "accept_draft" for call in client.calls) == 1
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert [call[1]["checkout_id"] for call in client.calls if call[0] == "close_checkout"] == [
            captured[1]
        ]
        assert dock.accept_button.enabled is False
        assert dock.reject_button.enabled is False
        assert dock.preview_projection().recovery_required is True

        dock.accept_button.click()
        dock.reject_button.click()
        controller._fix04_settle_worker(session)
        assert sum(call[0] in {"accept_draft", "reject_draft"} for call in client.calls) == 1
        assert attempts == list(captured)
    finally:
        dock._review_host_discard = discard
        force_cleanup_workbench(host, freecad)
