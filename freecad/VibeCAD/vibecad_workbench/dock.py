from __future__ import annotations

from PySide import QtCore, QtWidgets

from .state import ProjectionError, project_page_from_mapping, task_page_from_mapping

__all__ = ("ReviewDock",)

_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_COMMAND_KINDS = frozenset(
    (
        "connect",
        "list_projects",
        "list_tasks",
        "refresh_project",
        "refresh_task",
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


class ReviewDock(QtWidgets.QDockWidget):
    request = QtCore.Signal(object)

    def __init__(self, parent: object | None = None) -> None:
        super().__init__("VibeCAD Review", parent)
        self.setObjectName("VibeCADReviewDock")
        self._sequence = 0
        self._pending: dict[int, tuple[str, object]] = {}
        self._project_epoch = 0
        self._selection_epoch = 0
        self._task_load_epoch = 0
        self._project_ids: list[str] = []
        self._task_ids: list[str] = []
        self._loading_project_ids: list[str] = []
        self._loading_task_ids: list[str] = []
        self._loading_all_task_ids: list[str] = []
        self._project_cursors: set[str] = set()
        self._task_cursors: set[str] = set()

        container = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(container)
        self.status_label = QtWidgets.QLabel("Disconnected", container)
        self.project_selector = QtWidgets.QComboBox(container)
        self.task_selector = QtWidgets.QComboBox(container)
        self.refresh_button = QtWidgets.QPushButton("Refresh", container)
        self.status_label.setObjectName("VibeCADConnectionStatus")
        self.project_selector.setObjectName("VibeCADProjectSelector")
        self.task_selector.setObjectName("VibeCADReviewTaskSelector")
        self.refresh_button.setObjectName("VibeCADRefresh")
        for widget in (
            self.status_label,
            self.project_selector,
            self.task_selector,
            self.refresh_button,
        ):
            layout.addWidget(widget)
        self.setWidget(container)
        self.refresh_button.clicked.connect(self.refresh)
        self.project_selector.currentIndexChanged.connect(self._project_changed)

    def _next_request(self, expected_kind: str, context: object) -> int:
        request_id = self._sequence
        if request_id > _MAX_SAFE_INTEGER:
            self._sequence = 0
            request_id = 0
            self._pending.clear()
        self._sequence = request_id + 1
        self._pending[request_id] = (expected_kind, context)
        return request_id

    def _send(
        self,
        expected_kind: str,
        kind: str,
        *,
        context: object = None,
        **payload: object,
    ) -> int:
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

    def request_close(self) -> int:
        self.status_label.setText("Closing")
        return self._send("closed", "close")

    def refresh(self) -> None:
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
        self._request_tasks(project_id, None)

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
        self._task_cursors = set()
        self.task_selector.clear()

    def _project_changed(self, index: int) -> None:
        self._selection_epoch += 1
        self._clear_tasks()
        if 0 <= index < len(self._project_ids):
            self._request_tasks(self._project_ids[index], None)

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
            if not self._valid_event(event):
                self._fail()
                return
            assert type(event) is dict
            kind = event["kind"]
            request_id = event["request_id"]
            assert type(kind) is str
            assert type(request_id) is int
            if kind == "error":
                self._pending.pop(request_id, None)
                self._fail()
                return
            pending = self._pending.pop(request_id, None)
            if pending is None or pending[0] != kind:
                return
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
                    task.task_id
                    for task in page.tasks
                    if task.project_id == context[0] and task.status == "awaiting_user_review"
                ]
                if self._loading_task_ids and filtered:
                    if filtered[0] <= self._loading_task_ids[-1]:
                        raise ProjectionError("invalid public mapping")
                self._loading_task_ids.extend(filtered)
                if page.next_cursor is not None:
                    self._request_tasks(
                        context[0],
                        page.next_cursor,
                        context=context,
                    )
                    return
                self.task_selector.clear()
                self._task_ids = list(self._loading_task_ids)
                for task_id in self._task_ids:
                    self.task_selector.addItem(task_id)
                if self._task_ids:
                    self.task_selector.setCurrentIndex(0)
            elif kind == "project":
                if context != (self.current_project_id(), self._selection_epoch):
                    return
                if not self._authenticated_ok(event["response"]):
                    self._fail()
            elif kind == "task":
                if context != (
                    self.current_project_id(),
                    self.current_task_id(),
                    self._selection_epoch,
                ):
                    return
                if not self._authenticated_ok(event["response"]):
                    self._fail()
            elif kind == "review":
                if not self._authenticated_ok(event["response"]):
                    self._fail()
            elif kind == "closed":
                self.status_label.setText("Closed")
        except (AssertionError, KeyError, ProjectionError, TypeError, ValueError):
            self._fail()
