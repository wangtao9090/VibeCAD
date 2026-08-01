from __future__ import annotations

import secrets

from PySide import QtCore, QtWidgets

from .state import (
    PreviewProjection,
    ProjectionError,
    _preview_projection,
    project_page_from_mapping,
    project_summary_from_detail_mapping,
    task_page_from_mapping,
    task_summary_from_detail_mapping,
)

__all__ = ("ReviewDock",)

_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_MAX_PENDING_REQUESTS = 1024
_HOSTED_RESTRICTED_COMMAND_KINDS = frozenset(("preview_close", "review", "close"))
_COMMAND_KINDS = frozenset(
    (
        "connect",
        "list_projects",
        "list_tasks",
        "refresh_project",
        "refresh_task",
        "preview_open",
        "preview_refresh",
        "preview_close",
        "review",
        "close",
    )
)
_EVENT_KEYS = {
    "connected": frozenset(
        (
            "schema_version",
            "request_id",
            "kind",
            "daemon_id",
            "worker_thread_id",
        )
    ),
    "projects": frozenset(("schema_version", "request_id", "kind", "response")),
    "tasks": frozenset(("schema_version", "request_id", "kind", "response")),
    "project": frozenset(("schema_version", "request_id", "kind", "response")),
    "task": frozenset(("schema_version", "request_id", "kind", "response")),
    "preview_opened": frozenset(("schema_version", "request_id", "kind", "response")),
    "preview_refreshed": frozenset(("schema_version", "request_id", "kind", "response")),
    "preview_closed": frozenset(("schema_version", "request_id", "kind", "response")),
    "review": frozenset(("schema_version", "request_id", "kind", "response")),
    "closed": frozenset(("schema_version", "request_id", "kind")),
    "error": frozenset(
        (
            "schema_version",
            "request_id",
            "kind",
            "operation",
            "code",
            "outcome",
        )
    ),
}
_ERROR_CODES = frozenset(
    (
        "invalid_input",
        "unavailable",
        "internal_error",
        "closed",
        "wrong_process",
        "incompatible_kernel",
    )
)
_EVENT_OPERATIONS = {
    "connected": "connect",
    "projects": "list_projects",
    "tasks": "list_tasks",
    "project": "refresh_project",
    "task": "refresh_task",
    "preview_opened": "preview_open",
    "preview_refreshed": "preview_refresh",
    "preview_closed": "preview_close",
    "review": "review",
    "closed": "close",
}


class ReviewDock(QtWidgets.QDockWidget):
    request = QtCore.Signal(object)

    def __init__(self, parent: object | None = None) -> None:
        super().__init__("VibeCAD Review", parent)
        self.setObjectName("VibeCADReviewDock")
        self._sequence = 0
        self._pending: dict[int, tuple[str, object]] = {}
        self._retired_request_ids: set[int] = set()
        self._project_epoch = 0
        self._selection_epoch = 0
        self._task_selection_epoch = 0
        self._task_load_epoch = 0
        self._project_ids: list[str] = []
        self._task_ids: list[str] = []
        self._loading_project_ids: list[str] = []
        self._loading_task_ids: list[str] = []
        self._loading_all_task_ids: list[str] = []
        self._loading_tasks_by_id: dict[str, object] = {}
        self._tasks_by_id: dict[str, object] = {}
        self._invalidated_task_ids: set[str] = set()
        self._project_cursors: set[str] = set()
        self._task_cursors: set[str] = set()
        self._preview_checkouts: dict[str, str] = {}
        self._preview_pending_sources: set[str] = set()
        self._preview_epochs: dict[int, tuple[int, int]] = {}
        self._preview_eligible = False
        self._preview_recovery_required = False
        self._review_recovery_required = False
        self._host_transport: object | None = None
        self._review_host_submit: object | None = None
        self._review_host_discard: object | None = None
        self._review_pending: (
            tuple[
                int | None,
                str,
                tuple[object, ...],
                tuple[str, str],
                tuple[str, str],
            ]
            | None
        ) = None
        self._review_confirmation: dict[str, object] | None = None
        self._hosted_projection: tuple[int, str, object] | None = None

        container = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(container)
        self.status_label = QtWidgets.QLabel("Disconnected", container)
        self.preview_status_label = QtWidgets.QLabel("Preview closed", container)
        self.review_status_label = QtWidgets.QLabel("No review decision", container)
        self.project_selector = QtWidgets.QComboBox(container)
        self.task_selector = QtWidgets.QComboBox(container)
        self.refresh_button = QtWidgets.QPushButton("Refresh", container)
        self.open_head_button = QtWidgets.QPushButton("Open HEAD Preview", container)
        self.open_draft_button = QtWidgets.QPushButton("Open Draft Preview", container)
        self.accept_button = QtWidgets.QPushButton("Accept Draft", container)
        self.reject_button = QtWidgets.QPushButton("Reject Draft", container)
        self.status_label.setObjectName("VibeCADConnectionStatus")
        self.project_selector.setObjectName("VibeCADProjectSelector")
        self.task_selector.setObjectName("VibeCADReviewTaskSelector")
        self.refresh_button.setObjectName("VibeCADRefresh")
        self.open_head_button.setObjectName("VibeCADOpenHeadPreview")
        self.open_draft_button.setObjectName("VibeCADOpenDraftPreview")
        self.accept_button.setObjectName("VibeCADAcceptDraft")
        self.reject_button.setObjectName("VibeCADRejectDraft")
        for widget in (
            self.status_label,
            self.preview_status_label,
            self.review_status_label,
            self.project_selector,
            self.task_selector,
            self.refresh_button,
            self.open_head_button,
            self.open_draft_button,
            self.accept_button,
            self.reject_button,
        ):
            layout.addWidget(widget)
        self.setWidget(container)
        self.refresh_button.clicked.connect(self.refresh)
        self.open_head_button.clicked.connect(self.open_head_preview)
        self.open_draft_button.clicked.connect(self.open_draft_preview)
        self.accept_button.clicked.connect(self.accept_draft)
        self.reject_button.clicked.connect(self.reject_draft)
        self.project_selector.currentIndexChanged.connect(self._project_changed)
        self.task_selector.currentIndexChanged.connect(self._task_changed)
        self._update_preview_actions()

    def _bind_host_transport(self, transport: object) -> None:
        if (
            not callable(transport)
            or self._host_transport is not None
            or self._pending
            or self._sequence != 0
        ):
            raise RuntimeError("dock host transport cannot be bound")
        self._host_transport = transport

    def _bind_review_host(
        self,
        *,
        submit_review: object,
        discard_preview: object,
    ) -> None:
        if self._review_host_submit is not None or self._review_host_discard is not None:
            raise RuntimeError("review host is already bound")
        if not callable(submit_review) or not callable(discard_preview):
            raise RuntimeError("review host callbacks are invalid")
        self._review_host_submit = submit_review
        self._review_host_discard = discard_preview

    def _submit_host_review(
        self,
        *,
        decision: str,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> int:
        submit_review = self._review_host_submit
        if not callable(submit_review):
            raise ProjectionError("review host is unavailable")
        return submit_review(
            decision=decision,
            task_id=task_id,
            draft_id=draft_id,
            expected_generation=expected_generation,
        )

    def _discard_host_preview(self, checkout_id: str) -> None:
        discard_preview = self._review_host_discard
        if not callable(discard_preview):
            raise ProjectionError("review host is unavailable")
        discard_preview(checkout_id)

    def _project_host_preview_closed(self, checkout_id: object) -> None:
        if type(checkout_id) is not str:
            raise ProjectionError("invalid host preview projection")
        matched = [
            source_kind
            for source_kind, projected_checkout_id in self._preview_checkouts.items()
            if projected_checkout_id == checkout_id
        ]
        if not matched:
            return
        if len(matched) != 1:
            raise ProjectionError("invalid host preview projection")
        del self._preview_checkouts[matched[0]]
        self.set_preview_eligibility(False)
        self._update_preview_actions()

    def _project_host_preview_cycle_retired(self) -> None:
        if self._preview_checkouts or self._preview_pending_sources or self._preview_epochs:
            raise ProjectionError("preview cycle projection is not retired")
        self._preview_recovery_required = self._review_recovery_required
        self.set_preview_eligibility(False)
        self._update_preview_actions()

    def _receive_host_review_completion(
        self,
        event: object,
        context: object,
    ) -> None:
        pending = self._review_pending
        if pending is None or type(event) is not dict:
            return
        if event.get("request_id") != pending[0] or context != pending[2]:
            return
        if event.get("kind") == "error":
            self._enter_review_unknown()
            return
        if event.get("kind") != "review" or not self._authenticated_ok(event.get("response")):
            self._enter_review_unknown()
            return
        request_id, decision, selection_stamp, checkout_ids, revisions = pending
        project_id, task_id, draft_id, generation = selection_stamp[:4]
        base_revision, candidate_revision = revisions
        if (
            type(request_id) is not int
            or type(project_id) is not str
            or type(task_id) is not str
            or type(draft_id) is not str
            or type(generation) is not int
        ):
            self._fail_review_confirmation()
            return
        confirmation: dict[str, object] = {
            "review_request_id": request_id,
            "decision": decision,
            "project_id": project_id,
            "task_id": task_id,
            "draft_id": draft_id,
            "generation": generation,
            "base_revision": base_revision,
            "candidate_revision": candidate_revision,
            "task": None,
            "project": None,
        }
        self._review_confirmation = confirmation
        self.review_status_label.setText("Confirming review decision")
        try:
            self._send(
                "task",
                "refresh_task",
                context=("review-confirmation", request_id, "task"),
                task_id=task_id,
            )
            self._send(
                "project",
                "refresh_project",
                context=("review-confirmation", request_id, "project"),
                project_id=project_id,
            )
        except BaseException:
            self._fail_review_confirmation()
        finally:
            cleanup_failed = False
            for checkout_id in checkout_ids:
                try:
                    self._discard_host_preview(checkout_id)
                except BaseException:
                    cleanup_failed = True
            if cleanup_failed:
                self._fail_review_confirmation()

    def _enter_review_unknown(self) -> None:
        pending = self._review_pending
        checkout_ids = () if pending is None else tuple(pending[3])
        self._review_recovery_required = True
        self.review_status_label.setText("Review outcome unknown")
        self.set_preview_eligibility(False, recovery_required=True)
        self._review_confirmation = None
        self._review_pending = None
        self._update_preview_actions()
        for checkout_id in checkout_ids:
            try:
                self._discard_host_preview(checkout_id)
            except BaseException:
                pass

    @staticmethod
    def _review_confirmation_context(context: object, kind: str) -> bool:
        return (
            type(context) is tuple
            and len(context) == 3
            and context[0] == "review-confirmation"
            and type(context[1]) is int
            and context[2] == kind
        )

    def _fail_review_confirmation(self) -> None:
        self._review_recovery_required = True
        self._review_confirmation = None
        self._review_pending = None
        self.review_status_label.setText("Review confirmation failed")
        self.set_preview_eligibility(False, recovery_required=True)

    def _finish_review_confirmation(self) -> None:
        confirmation = self._review_confirmation
        if confirmation is None:
            return
        task = confirmation["task"]
        project = confirmation["project"]
        if task is None or project is None:
            return
        decision = confirmation["decision"]
        task_id = confirmation["task_id"]
        project_id = confirmation["project_id"]
        draft_id = confirmation["draft_id"]
        generation = confirmation["generation"]
        base_revision = confirmation["base_revision"]
        candidate_revision = confirmation["candidate_revision"]
        valid = (
            task.task_id == task_id
            and task.project_id == project_id
            and task.draft_id == draft_id
            and task.base_revision == base_revision
            and task.candidate_revision == candidate_revision
            and type(generation) is int
            and task.generation > generation
            and project.project_id == project_id
            and (
                decision == "accept"
                and task.status == "succeeded"
                and task.committed_revision == candidate_revision
                and project.revision_id == candidate_revision
                or decision == "reject"
                and task.status == "rejected"
                and task.committed_revision is None
                and project.revision_id == base_revision
            )
        )
        if not valid:
            self._fail_review_confirmation()
            return
        assert type(task_id) is str
        self._tasks_by_id.pop(task_id, None)
        self._invalidated_task_ids.add(task_id)
        self._review_confirmation = None
        self._review_pending = None
        self.review_status_label.setText(f"Draft {decision}ed")
        self._update_preview_actions()

    def _retire_request_id(self, request_id: int) -> None:
        self._retired_request_ids.clear()
        self._retired_request_ids.add(request_id)

    def _next_request(self, expected_kind: str, context: object) -> int:
        request_id = self._sequence
        if (
            type(request_id) is not int
            or not 0 <= request_id <= _MAX_SAFE_INTEGER
            or request_id in self._pending
            or request_id in self._retired_request_ids
            or len(self._pending) >= _MAX_PENDING_REQUESTS
        ):
            raise ProjectionError("request id authority exhausted")
        self._sequence = request_id + 1
        self._pending[request_id] = (expected_kind, context)
        return request_id

    def _hosted_request(
        self,
        expected_kind: str,
        kind: str,
        *,
        context: object,
        payload: dict[str, object],
    ) -> int:
        transport = self._host_transport
        if not callable(transport):
            raise ProjectionError("host transport is unavailable")
        intent = {
            "phase": "reserve",
            "expected_kind": expected_kind,
            "kind": kind,
            "context": context,
            "payload": dict(payload),
            "projected_cursor": self._sequence,
        }
        projected_cursor = self._sequence
        request_id = transport(intent)
        if (
            type(request_id) is not int
            or not 0 <= request_id <= _MAX_SAFE_INTEGER
            or request_id != projected_cursor
            or request_id in self._pending
            or request_id in self._retired_request_ids
            or len(self._pending) >= _MAX_PENDING_REQUESTS
        ):
            try:
                transport(
                    {
                        "phase": "cancel",
                        "request_id": request_id,
                        "if_not_enqueued": True,
                    }
                )
            except BaseException:
                pass
            raise ProjectionError("request id authority exhausted")
        self._sequence = request_id + 1
        self._pending[request_id] = (expected_kind, context)
        try:
            transport(
                {
                    "phase": "enqueue",
                    "request_id": request_id,
                }
            )
        except BaseException:
            try:
                transport(
                    {
                        "phase": "cancel",
                        "request_id": request_id,
                        "if_not_enqueued": True,
                    }
                )
            except BaseException:
                pass
            self._pending.pop(request_id, None)
            self._retire_request_id(request_id)
            raise
        return request_id

    def _send(
        self,
        expected_kind: str,
        kind: str,
        *,
        context: object = None,
        **payload: object,
    ) -> int:
        if self._host_transport is not None:
            if kind in _HOSTED_RESTRICTED_COMMAND_KINDS:
                raise ProjectionError("restricted command requires host authority")
            return self._hosted_request(
                expected_kind,
                kind,
                context=context,
                payload=dict(payload),
            )
        request_id = self._next_request(expected_kind, context)
        self.request.emit(
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": kind,
                **payload,
            }
        )
        return request_id

    def start(self) -> None:
        self.status_label.setText("Connecting")
        self._send("connected", "connect")

    def request_close(
        self,
        checkout_ids: tuple[str, ...] = (),
    ) -> int | None:
        if self._host_transport is not None:
            return None
        self.status_label.setText("Closing")
        for checkout_id in checkout_ids:
            self.request_preview_close(checkout_id)
        return self.request_client_close()

    def request_preview_close(
        self,
        checkout_id: str,
        *,
        document_absent: bool = False,
    ) -> int | None:
        if self._host_transport is not None:
            return None
        self.status_label.setText("Closing")
        return self._send(
            "preview_closed",
            "preview_close",
            context=checkout_id,
            checkout_id=checkout_id,
            document_absent=document_absent,
        )

    def request_client_close(self) -> int | None:
        if self._host_transport is not None:
            return None
        self.status_label.setText("Closing")
        return self._send("closed", "close")

    def request_review(
        self,
        *,
        decision: str,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> int | None:
        if self._host_transport is not None:
            return None
        return self._send(
            "review",
            "review",
            context=(task_id, draft_id, expected_generation),
            decision=decision,
            task_id=task_id,
            draft_id=draft_id,
            expected_generation=expected_generation,
        )

    def refresh(self) -> None:
        try:
            self._invalidated_task_ids.clear()
            checkout_ids = tuple(self._preview_checkouts.values())
            transport = self._host_transport
            if callable(transport):
                self.set_preview_eligibility(False)
                transport(
                    {
                        "phase": "refresh_begin",
                        "checkout_ids": checkout_ids,
                    }
                )
            elif checkout_ids:
                self.set_preview_eligibility(False)
            project_id = self.current_project_id()
            if project_id is None:
                self._request_projects(None)
                return
            self._send(
                "project",
                "refresh_project",
                context=(project_id, self._selection_epoch),
                project_id=project_id,
            )
            task_id = self.current_task_id()
            if task_id is not None:
                self._send(
                    "task",
                    "refresh_task",
                    context=(project_id, task_id, self._selection_epoch),
                    task_id=task_id,
                )
            for checkout_id in checkout_ids:
                self._send(
                    "preview_refreshed",
                    "preview_refresh",
                    context=checkout_id,
                    checkout_id=checkout_id,
                )
            self._request_tasks(project_id, None)
        except ProjectionError:
            self._fail()
            return

    def _request_projects(self, cursor: str | None) -> None:
        if cursor is None:
            self._project_epoch += 1
            self._loading_project_ids = []
            self._project_cursors = set()
        elif cursor in self._project_cursors:
            raise ProjectionError("invalid public mapping")
        else:
            self._project_cursors.add(cursor)
        self._send(
            "projects",
            "list_projects",
            context=self._project_epoch,
            cursor=cursor,
        )

    def _request_tasks(
        self,
        project_id: str,
        cursor: str | None,
        *,
        context: tuple[str, int, int] | None = None,
    ) -> None:
        if cursor is None:
            self._task_load_epoch += 1
            self._loading_task_ids = []
            self._loading_all_task_ids = []
            self._loading_tasks_by_id = {}
            self._task_cursors = set()
            context = (
                project_id,
                self._selection_epoch,
                self._task_load_epoch,
            )
        elif cursor in self._task_cursors:
            raise ProjectionError("invalid public mapping")
        else:
            self._task_cursors.add(cursor)
        assert context is not None
        self._send(
            "tasks",
            "list_tasks",
            context=context,
            cursor=cursor,
        )

    def current_project_id(self) -> str | None:
        index = self.project_selector.currentIndex()
        if 0 <= index < len(self._project_ids):
            return self._project_ids[index]
        return None

    def current_task_id(self) -> str | None:
        index = self.task_selector.currentIndex()
        if 0 <= index < len(self._task_ids):
            return self._task_ids[index]
        return None

    def _clear_tasks(self) -> None:
        self._task_ids = []
        self._loading_task_ids = []
        self._loading_all_task_ids = []
        self._loading_tasks_by_id = {}
        self._tasks_by_id = {}
        self._invalidated_task_ids.clear()
        self._task_cursors = set()
        self.task_selector.clear()
        self._update_preview_actions()

    def _project_changed(self, index: int) -> None:
        self.set_preview_eligibility(False)
        self._selection_epoch += 1
        self._clear_tasks()
        if 0 <= index < len(self._project_ids):
            self._request_tasks(self._project_ids[index], None)
        self._update_preview_actions()

    def _task_changed(self, _index: int) -> None:
        self._task_selection_epoch += 1
        self._update_preview_actions()

    @staticmethod
    def _task_value(task: object, name: str) -> object:
        if type(task) is dict:
            return task.get(name)
        return getattr(task, name, None)

    def _selected_task(self) -> object | None:
        task_id = self.current_task_id()
        return None if task_id is None else self._tasks_by_id.get(task_id)

    def _update_preview_actions(self) -> None:
        project_ready = (
            self.current_project_id() is not None
            and "head" not in self._preview_checkouts
            and "head" not in self._preview_pending_sources
        )
        task = self._selected_task()
        draft_ready = (
            task is not None
            and self._task_value(task, "status") == "awaiting_user_review"
            and type(self._task_value(task, "draft_id")) is str
            and type(self._task_value(task, "generation")) is int
            and "draft" not in self._preview_checkouts
            and "draft" not in self._preview_pending_sources
        )
        self.open_head_button.setEnabled(project_ready)
        self.open_draft_button.setEnabled(draft_ready)
        review_ready = self._review_context() is not None
        self.accept_button.setEnabled(review_ready)
        self.reject_button.setEnabled(review_ready)

    @staticmethod
    def _canonical_identifier(value: object, prefix: str) -> bool:
        return (
            type(value) is str
            and value.startswith(prefix)
            and len(value) == len(prefix) + 32
            and all(character in "0123456789abcdef" for character in value[len(prefix) :])
        )

    def _review_context(
        self,
    ) -> tuple[tuple[object, ...], tuple[str, str], tuple[str, str]] | None:
        task = self._selected_task()
        project_id = self.current_project_id()
        task_id = self.current_task_id()
        draft_id = self._task_value(task, "draft_id")
        generation = self._task_value(task, "generation")
        candidate_revision = self._task_value(task, "candidate_revision")
        base_revision = self._task_value(task, "base_revision")
        head_checkout = self._preview_checkouts.get("head")
        draft_checkout = self._preview_checkouts.get("draft")
        if (
            self._review_pending is not None
            or not self.preview_projection().review_eligible
            or set(self._preview_checkouts) != {"head", "draft"}
            or not self._canonical_identifier(project_id, "project_")
            or not self._canonical_identifier(task_id, "task_")
            or self._task_value(task, "task_id") != task_id
            or self._task_value(task, "project_id") != project_id
            or self._task_value(task, "status") != "awaiting_user_review"
            or not self._canonical_identifier(draft_id, "draft_")
            or type(generation) is not int
            or not 0 <= generation <= _MAX_SAFE_INTEGER
            or not self._canonical_identifier(candidate_revision, "revision_")
            or not self._canonical_identifier(base_revision, "revision_")
            or draft_id != f"draft_{candidate_revision.removeprefix('revision_')}"
            or not self._canonical_identifier(head_checkout, "checkout_")
            or not self._canonical_identifier(draft_checkout, "checkout_")
            or head_checkout == draft_checkout
        ):
            return None
        assert type(project_id) is str
        assert type(task_id) is str
        assert type(draft_id) is str
        assert type(base_revision) is str
        assert type(candidate_revision) is str
        assert type(head_checkout) is str
        assert type(draft_checkout) is str
        return (
            (
                project_id,
                task_id,
                draft_id,
                generation,
                self._selection_epoch,
                self._task_selection_epoch,
            ),
            (head_checkout, draft_checkout),
            (base_revision, candidate_revision),
        )

    def _submit_decision(self, decision: str) -> None:
        context = self._review_context()
        if context is None or decision not in ("accept", "reject"):
            return
        selection_stamp, checkout_ids, revisions = context
        task_id = selection_stamp[1]
        draft_id = selection_stamp[2]
        generation = selection_stamp[3]
        assert type(task_id) is str
        assert type(draft_id) is str
        assert type(generation) is int
        sentinel = (None, decision, selection_stamp, checkout_ids, revisions)
        self._review_pending = sentinel
        self.review_status_label.setText("Review pending")
        self._update_preview_actions()
        try:
            request_id = self._submit_host_review(
                decision=decision,
                task_id=task_id,
                draft_id=draft_id,
                expected_generation=generation,
            )
            if type(request_id) is not int or request_id < 0:
                raise ProjectionError("invalid review request authority")
        except BaseException:
            if self._review_pending is sentinel:
                self._enter_review_unknown()
            return
        self._review_pending = (request_id, decision, selection_stamp, checkout_ids, revisions)

    def accept_draft(self) -> None:
        self._submit_decision("accept")

    def reject_draft(self) -> None:
        self._submit_decision("reject")

    def _open_preview(self, source: dict[str, object]) -> None:
        kind = source["kind"]
        assert type(kind) is str
        if kind in self._preview_checkouts or kind in self._preview_pending_sources:
            return
        self._preview_pending_sources.add(kind)
        self._update_preview_actions()
        open_key = "checkout_open_" + secrets.token_hex(16)
        try:
            request_id = self._send(
                "preview_opened",
                "preview_open",
                context=(kind, dict(source), open_key),
                source=source,
                open_key=open_key,
            )
        except BaseException:
            self._preview_pending_sources.discard(kind)
            self._update_preview_actions()
            raise
        self._preview_epochs[request_id] = (
            self._selection_epoch,
            self._task_selection_epoch,
        )

    def open_head_preview(self) -> None:
        project_id = self.current_project_id()
        if project_id is None:
            return
        self._open_preview({"kind": "head", "project_id": project_id})

    def open_draft_preview(self) -> None:
        task = self._selected_task()
        if task is None or self._task_value(task, "status") != "awaiting_user_review":
            return
        task_id = self._task_value(task, "task_id")
        draft_id = self._task_value(task, "draft_id")
        generation = self._task_value(task, "generation")
        if type(task_id) is not str or type(draft_id) is not str or type(generation) is not int:
            return
        self._open_preview(
            {
                "kind": "draft",
                "task_id": task_id,
                "draft_id": draft_id,
                "expected_generation": generation,
            }
        )

    def set_preview_eligibility(
        self,
        eligible: bool,
        *,
        recovery_required: bool = False,
    ) -> None:
        if recovery_required:
            self._preview_recovery_required = True
        self._preview_eligible = eligible is True and not self._preview_recovery_required
        if self._preview_recovery_required:
            self.preview_status_label.setText("Preview recovery required")
        elif self._preview_eligible:
            self.preview_status_label.setText("Preview live")
        elif self._preview_checkouts:
            self.preview_status_label.setText("Preview review disabled")
        else:
            self.preview_status_label.setText("Preview closed")
        self._update_preview_actions()

    def preview_projection(self) -> PreviewProjection:
        return _preview_projection(
            head_open="head" in self._preview_checkouts,
            draft_open="draft" in self._preview_checkouts,
            requested_eligible=self._preview_eligible,
            recovery_required=self._preview_recovery_required,
        )

    def expected_preview_open(
        self,
        request_id: object,
    ) -> tuple[str, dict[str, object], str] | None:
        if type(request_id) is not int:
            return None
        pending = self._pending.get(request_id)
        if (
            pending is None
            or pending[0] != "preview_opened"
            or type(pending[1]) is not tuple
            or len(pending[1]) != 3
        ):
            return None
        kind, source, open_key = pending[1]
        if type(kind) is not str or type(source) is not dict or type(open_key) is not str:
            return None
        return kind, dict(source), open_key

    def expected_preview_refresh(self, request_id: object) -> str | None:
        if type(request_id) is not int:
            return None
        pending = self._pending.get(request_id)
        if pending is None or pending[0] != "preview_refreshed" or type(pending[1]) is not str:
            return None
        return pending[1]

    def _hosted_pending(
        self,
        request_id: object,
    ) -> tuple[str, object] | None:
        if self._host_transport is None or type(request_id) is not int:
            return None
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        return pending

    def _apply_hosted_event(
        self,
        event: object,
        *,
        expected_kind: str,
        context: object,
    ) -> bool:
        if self._host_transport is None or not self._valid_event(event) or type(event) is not dict:
            return False
        request_id = event["request_id"]
        kind = event["kind"]
        if (
            type(request_id) is not int
            or type(kind) is not str
            or self._pending.get(request_id) != (expected_kind, context)
            or (
                kind != expected_kind
                and not (
                    kind == "error" and event["operation"] == _EVENT_OPERATIONS.get(expected_kind)
                )
            )
        ):
            return False
        projection = (request_id, expected_kind, context)
        if self._hosted_projection is not None:
            return False
        self._hosted_projection = projection
        try:
            self.handle_event(event)
        finally:
            self._hosted_projection = None
        return request_id not in self._pending

    def _discard_hosted_pending(
        self,
        request_id: object,
        *,
        expected_kind: str,
        context: object,
    ) -> bool:
        if (
            self._host_transport is None
            or type(request_id) is not int
            or self._pending.get(request_id) != (expected_kind, context)
        ):
            return False
        pending = self._pending.pop(request_id)
        self._retire_request_id(request_id)
        self._preview_epochs.pop(request_id, None)
        if (
            pending[0] == "preview_opened"
            and type(pending[1]) is tuple
            and len(pending[1]) == 3
            and type(pending[1][0]) is str
        ):
            self._preview_pending_sources.discard(pending[1][0])
            self._update_preview_actions()
        return True

    def current_preview_open(
        self,
        request_id: object,
    ) -> tuple[str, dict[str, object], str] | None:
        context = self.expected_preview_open(request_id)
        if context is None or type(request_id) is not int:
            return None
        epochs = self._preview_epochs.get(request_id)
        if epochs is None:
            return None
        kind, source, _open_key = context
        if epochs[0] != self._selection_epoch or self.current_project_id() != (
            source.get("project_id")
            if kind == "head"
            else self._task_value(self._selected_task(), "project_id")
        ):
            return None
        if kind == "head":
            return context
        task = self._selected_task()
        if (
            epochs[1] != self._task_selection_epoch
            or self._task_value(task, "task_id") != source.get("task_id")
            or self._task_value(task, "draft_id") != source.get("draft_id")
            or self._task_value(task, "generation") != source.get("expected_generation")
        ):
            return None
        return context

    def discard_preview_open(self, request_id: object) -> None:
        if self._host_transport is not None:
            return
        if type(request_id) is not int:
            return
        pending = self._pending.get(request_id)
        if (
            pending is None
            or pending[0] != "preview_opened"
            or type(pending[1]) is not tuple
            or len(pending[1]) != 3
            or type(pending[1][0]) is not str
        ):
            return
        self._pending.pop(request_id, None)
        self._retire_request_id(request_id)
        self._preview_epochs.pop(request_id, None)
        self._preview_pending_sources.discard(pending[1][0])
        self._update_preview_actions()

    def pending_preview_open_count(self) -> int:
        return len(self._preview_epochs)

    def _fail(self) -> None:
        self.status_label.setText("Unavailable")

    @staticmethod
    def _valid_event(event: object) -> bool:
        if type(event) is not dict or any(type(key) is not str for key in event):
            return False
        kind = event.get("kind")
        request_id = event.get("request_id")
        if (
            type(event.get("schema_version")) is not int
            or event.get("schema_version") != 1
            or type(kind) is not str
            or kind not in _EVENT_KEYS
            or set(event) != _EVENT_KEYS[kind]
            or type(request_id) is not int
            or not -1 <= request_id <= _MAX_SAFE_INTEGER
        ):
            return False
        if kind != "error" and request_id < 0:
            return False
        if kind == "connected":
            return (
                type(event["daemon_id"]) is str
                and event["daemon_id"].startswith("daemon_")
                and len(event["daemon_id"]) == 39
                and all(character in "0123456789abcdef" for character in event["daemon_id"][7:])
                and type(event["worker_thread_id"]) is int
                and event["worker_thread_id"] >= 0
            )
        if kind == "error":
            return (
                type(event["operation"]) is str
                and event["operation"] in _COMMAND_KINDS | {"invalid"}
                and ((request_id == -1) == (event["operation"] == "invalid"))
                and type(event["code"]) is str
                and event["code"] in _ERROR_CODES
                and type(event["outcome"]) is str
                and event["outcome"] in ("known_failure", "unknown_outcome")
            )
        return True

    @staticmethod
    def _authenticated_ok(response: object) -> bool:
        return (
            type(response) is dict
            and all(type(key) is str for key in response)
            and set(response) == {"schema_version", "ok", "result", "error"}
            and type(response.get("schema_version")) is int
            and response.get("schema_version") == 1
            and response.get("ok") is True
            and response.get("error") is None
        )

    @QtCore.Slot(object)
    def handle_event(self, event: object) -> None:
        try:
            if self._host_transport is not None:
                projection = self._hosted_projection
                if (
                    projection is None
                    or type(event) is not dict
                    or event.get("request_id") != projection[0]
                    or self._pending.get(projection[0]) != (projection[1], projection[2])
                ):
                    return
            if not self._valid_event(event):
                self._fail()
                return
            assert type(event) is dict
            kind = event["kind"]
            request_id = event["request_id"]
            assert type(kind) is str
            assert type(request_id) is int
            if kind == "error":
                pending = self._pending.get(request_id)
                if pending is not None and _EVENT_OPERATIONS.get(pending[0]) != event["operation"]:
                    self._fail()
                    return
                pending = self._pending.pop(request_id, None)
                if pending is not None:
                    self._retire_request_id(request_id)
                if pending is not None and (
                    self._review_confirmation_context(pending[1], "task")
                    or self._review_confirmation_context(pending[1], "project")
                ):
                    self._fail_review_confirmation()
                    return
                if (
                    pending is not None
                    and pending[0] == "preview_opened"
                    and type(pending[1]) is tuple
                    and len(pending[1]) == 3
                    and type(pending[1][0]) is str
                ):
                    self._preview_epochs.pop(request_id, None)
                    self._preview_pending_sources.discard(pending[1][0])
                    self._update_preview_actions()
                self._fail()
                return
            pending = self._pending.pop(request_id, None)
            if pending is None or pending[0] != kind:
                return
            self._retire_request_id(request_id)
            self._preview_epochs.pop(request_id, None)
            context = pending[1]
            if kind == "connected":
                self.status_label.setText("Connected")
                self._request_projects(None)
            elif kind == "projects":
                if context != self._project_epoch:
                    return
                page = project_page_from_mapping(event["response"])
                page_ids = [project.project_id for project in page.projects]
                if self._loading_project_ids and page_ids:
                    if page_ids[0] <= self._loading_project_ids[-1]:
                        raise ProjectionError("invalid public mapping")
                self._loading_project_ids.extend(page_ids)
                if page.next_cursor is not None:
                    self._request_projects(page.next_cursor)
                    return
                self.project_selector.blockSignals(True)
                self.project_selector.clear()
                self._project_ids = list(self._loading_project_ids)
                for project_id in self._project_ids:
                    self.project_selector.addItem(project_id)
                if self._project_ids:
                    self.project_selector.setCurrentIndex(-1)
                self.project_selector.blockSignals(False)
                self._clear_tasks()
                if self._project_ids:
                    self.project_selector.setCurrentIndex(0)
            elif kind == "tasks":
                if context != (
                    self.current_project_id(),
                    self._selection_epoch,
                    self._task_load_epoch,
                ):
                    return
                page = task_page_from_mapping(event["response"])
                page_ids = [task.task_id for task in page.tasks]
                if self._loading_all_task_ids and page_ids:
                    if page_ids[0] <= self._loading_all_task_ids[-1]:
                        raise ProjectionError("invalid public mapping")
                self._loading_all_task_ids.extend(page_ids)
                filtered = [
                    task
                    for task in page.tasks
                    if (
                        task.project_id == context[0]
                        and task.status == "awaiting_user_review"
                        and task.task_id not in self._invalidated_task_ids
                    )
                ]
                filtered_ids = [task.task_id for task in filtered]
                if self._loading_task_ids and filtered_ids:
                    if filtered_ids[0] <= self._loading_task_ids[-1]:
                        raise ProjectionError("invalid public mapping")
                self._loading_task_ids.extend(filtered_ids)
                for task in filtered:
                    previous = self._tasks_by_id.get(task.task_id)
                    previous_generation = self._task_value(previous, "generation")
                    if type(previous_generation) is int and previous_generation > task.generation:
                        self._loading_tasks_by_id[task.task_id] = previous
                    elif previous_generation == task.generation and previous != task:
                        raise ProjectionError("invalid public mapping")
                    else:
                        self._loading_tasks_by_id[task.task_id] = task
                if page.next_cursor is not None:
                    self._request_tasks(
                        context[0],
                        page.next_cursor,
                        context=context,
                    )
                    return
                self.task_selector.blockSignals(True)
                try:
                    self.task_selector.clear()
                    self._task_ids = list(self._loading_task_ids)
                    self._tasks_by_id = dict(self._loading_tasks_by_id)
                    for task_id in self._task_ids:
                        self.task_selector.addItem(task_id)
                    if self._task_ids:
                        self.task_selector.setCurrentIndex(0)
                finally:
                    self.task_selector.blockSignals(False)
                self._update_preview_actions()
            elif kind == "project":
                if self._review_confirmation_context(context, "project"):
                    confirmation = self._review_confirmation
                    if confirmation is None or context[1] != confirmation["review_request_id"]:
                        return
                    try:
                        confirmation["project"] = project_summary_from_detail_mapping(
                            event["response"]
                        )
                    except ProjectionError:
                        self._fail_review_confirmation()
                        return
                    self._finish_review_confirmation()
                    return
                if context != (self.current_project_id(), self._selection_epoch):
                    return
                if not self._authenticated_ok(event["response"]):
                    self._fail()
            elif kind == "task":
                if self._review_confirmation_context(context, "task"):
                    confirmation = self._review_confirmation
                    if confirmation is None or context[1] != confirmation["review_request_id"]:
                        return
                    try:
                        confirmation["task"] = task_summary_from_detail_mapping(event["response"])
                    except ProjectionError:
                        self._fail_review_confirmation()
                        return
                    self._finish_review_confirmation()
                    return
                if context != (
                    self.current_project_id(),
                    self.current_task_id(),
                    self._selection_epoch,
                ):
                    return
                task_id = context[1]
                previous = self._tasks_by_id.pop(task_id, None)
                self._invalidated_task_ids.add(task_id)
                self.set_preview_eligibility(False)
                task = task_summary_from_detail_mapping(event["response"])
                previous_generation = self._task_value(previous, "generation")
                if (
                    task.task_id != task_id
                    or task.project_id != context[0]
                    or task.status != "awaiting_user_review"
                    or type(previous_generation) is not int
                    or task.generation < previous_generation
                    or (task.generation == previous_generation and task != previous)
                ):
                    raise ProjectionError("invalid public mapping")
                self._tasks_by_id[task.task_id] = task
                self._invalidated_task_ids.discard(task.task_id)
                self._update_preview_actions()
            elif kind == "preview_opened":
                response = event["response"]
                if type(context) is not tuple or len(context) != 3:
                    raise ProjectionError("invalid public mapping")
                source_kind, expected_source, expected_open_key = context
                if type(response) is not dict or type(response.get("descriptor")) is not dict:
                    raise ProjectionError("invalid public mapping")
                source = response.get("source")
                descriptor = response["descriptor"]
                if (
                    type(source) is not dict
                    or source != expected_source
                    or source.get("kind") != source_kind
                    or response.get("open_key") != expected_open_key
                ):
                    raise ProjectionError("invalid public mapping")
                checkout_id = descriptor.get("checkout_id")
                if type(checkout_id) is not str:
                    raise ProjectionError("invalid public mapping")
                self._preview_pending_sources.discard(source_kind)
                self._preview_checkouts[source_kind] = checkout_id
                self.set_preview_eligibility(False)
                self._update_preview_actions()
            elif kind == "preview_refreshed":
                response = event["response"]
                if type(response) is not dict or response.get("checkout_id") != context:
                    raise ProjectionError("invalid public mapping")
            elif kind == "preview_closed":
                response = event["response"]
                if type(response) is not dict or response.get("checkout_id") != context:
                    raise ProjectionError("invalid public mapping")
                self._preview_checkouts = {
                    kind: checkout_id
                    for kind, checkout_id in self._preview_checkouts.items()
                    if checkout_id != context
                }
                self.set_preview_eligibility(False)
                self._update_preview_actions()
            elif kind == "review":
                if not self._authenticated_ok(event["response"]):
                    self._fail()
            elif kind == "closed":
                self.status_label.setText("Closed")
        except (AssertionError, KeyError, ProjectionError, TypeError, ValueError):
            self._fail()
