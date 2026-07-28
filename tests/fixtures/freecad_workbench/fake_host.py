from __future__ import annotations

import queue
import threading
import time
from types import ModuleType

_MAIN_THREAD_ID = threading.get_ident()


def _require_main_thread() -> None:
    if threading.get_ident() != _MAIN_THREAD_ID:
        raise RuntimeError("fake widget thread authority violation")


def _find_qt_children(
    owner: object,
    child_type: type[object],
    name: str | None = None,
) -> list[object]:
    matches: list[object] = []
    for child in getattr(owner, "_qt_children", ()):
        if isinstance(child, child_type) and (
            not name or getattr(child, "object_name", "") == name
        ):
            matches.append(child)
        matches.extend(_find_qt_children(child, child_type, name))
    return matches


class FakeWorkbench:
    pass


class FakeFreeCADGui(ModuleType):
    def __init__(
        self,
        *,
        fail_first_add: bool = False,
        fail_first_dock_add: bool = False,
    ) -> None:
        super().__init__("FreeCADGui")
        self.added_workbenches: list[object] = []
        self.add_attempts = 0
        self._fail_first_add = fail_first_add
        self.main_window = FakeMainWindow(fail_first_add=fail_first_dock_add)

    def addWorkbench(self, workbench: object) -> None:
        _require_main_thread()
        self.add_attempts += 1
        if self._fail_first_add and self.add_attempts == 1:
            raise RuntimeError("synthetic addWorkbench failure")
        self.added_workbenches.append(workbench)

    def getMainWindow(self) -> FakeMainWindow:
        _require_main_thread()
        return self.main_window


def make_fake_freecad_gui(
    *,
    fail_first_add: bool = False,
    fail_first_dock_add: bool = False,
) -> FakeFreeCADGui:
    return FakeFreeCADGui(
        fail_first_add=fail_first_add,
        fail_first_dock_add=fail_first_dock_add,
    )


class FakeMainWindow:
    def __init__(self, *, fail_first_add: bool = False) -> None:
        self._qt_children: list[object] = []
        self.docks: list[object] = []
        self.add_attempts = 0
        self._fail_first_add = fail_first_add

    def addDockWidget(self, _area: object, dock: object) -> None:
        _require_main_thread()
        self.add_attempts += 1
        if self._fail_first_add and self.add_attempts == 1:
            raise RuntimeError("synthetic addDockWidget failure")
        self.docks.append(dock)

    def removeDockWidget(self, dock: object) -> None:
        _require_main_thread()
        self.docks.remove(dock)

    def children(self) -> list[object]:
        _require_main_thread()
        return list(self._qt_children)

    def findChildren(
        self,
        child_type: type[object],
        name: str | None = None,
    ) -> list[object]:
        _require_main_thread()
        return _find_qt_children(self, child_type, name)


class FakeLocalAgentClient:
    def __init__(self) -> None:
        self.daemon_id = "daemon_" + "a" * 32
        self.calls: list[tuple[str, dict[str, object], int]] = []
        self.created_thread_id = threading.get_ident()
        self.closed_thread_id: int | None = None
        self.close_call_count = 0
        self.review_failure = False
        self.last_tasks_response: dict[str, object] | None = None
        self.projects_response: dict[str, object] | None = None
        self.tasks_response: dict[str, object] | None = None

    def _record(self, name: str, request: dict[str, object]) -> None:
        if threading.get_ident() != self.created_thread_id:
            raise RuntimeError("fake client thread authority violation")
        self.calls.append((name, request, threading.get_ident()))

    def ping(self) -> dict[str, object]:
        self._record("ping", {})
        return {"schema_version": 1, "status": "ready"}

    def list_projects_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("list_projects", request)
        if self.projects_response is not None:
            return self.projects_response
        response = {
            "schema_version": 1,
            "ok": True,
            "result": {
                "schema_version": 1,
                "projects": [
                    {
                        "schema_version": 1,
                        "project_id": "project_" + "1" * 32,
                        "generation": 2,
                        "revision_id": "revision_" + "2" * 32,
                        "manifest_sha256": "3" * 64,
                    }
                ],
                "next_cursor": None,
            },
            "error": None,
        }
        return response

    def list_tasks_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("list_tasks", request)
        if self.tasks_response is not None:
            self.last_tasks_response = self.tasks_response
            return self.tasks_response
        response = {
            "schema_version": 1,
            "ok": True,
            "result": {
                "tasks": [
                    _fake_task("1", status="awaiting_user_review"),
                    _fake_task("2", status="active"),
                ],
                "next_cursor": None,
            },
            "error": None,
        }
        self.last_tasks_response = response
        return response

    def get_project_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("get_project", request)
        return {"schema_version": 1, "ok": True, "result": {}, "error": None}

    def get_task_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("get_task", request)
        return {"schema_version": 1, "ok": True, "result": {}, "error": None}

    def accept_draft_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("accept_draft", request)
        if self.review_failure:
            raise RuntimeError("synthetic uncertain review")
        return {"schema_version": 1, "ok": True, "result": {}, "error": None}

    def reject_draft_request(self, request: dict[str, object]) -> dict[str, object]:
        self._record("reject_draft", request)
        return {"schema_version": 1, "ok": True, "result": {}, "error": None}

    def close(self) -> None:
        if threading.get_ident() != self.created_thread_id:
            raise RuntimeError("fake client thread authority violation")
        self.close_call_count += 1
        self.closed_thread_id = threading.get_ident()


def _fake_task(digit: str, *, status: str) -> dict[str, object]:
    return {
        "task_id": "task_" + digit * 32,
        "project_id": "project_" + "1" * 32,
        "generation": 3,
        "base_revision": "revision_" + digit * 32,
        "reasoning_owner": "server",
        "review_policy": "required",
        "status": status,
        "next_action": "review",
        "candidate_revision": "revision_" + "4" * 32,
        "committed_revision": None,
        "draft_id": "draft_" + "5" * 32,
    }


class _BoundSignal:
    def __init__(self, owner: object) -> None:
        self._owner = owner
        self._connections: list[tuple[object, object]] = []

    def connect(self, slot: object, connection_type: object = None) -> None:
        self._connections.append((slot, connection_type))

    def emit(self, *args: object) -> None:
        for slot, connection_type in tuple(self._connections):
            receiver = getattr(slot, "__self__", None)
            target = getattr(receiver, "_qt_thread", None)
            if connection_type == _Qt.ConnectionType.QueuedConnection:
                if isinstance(target, _QThread):
                    target.post(slot, args)
                else:
                    _MAIN_EVENTS.put((slot, args))
            else:
                slot(*args)


class _Signal:
    def __init__(self, *_types: object) -> None:
        self._name = ""

    def __set_name__(self, _owner: type[object], name: str) -> None:
        self._name = f"__signal_{name}"

    def __get__(self, instance: object, _owner: type[object]) -> object:
        if instance is None:
            return self
        signal = getattr(instance, self._name, None)
        if signal is None:
            signal = _BoundSignal(instance)
            setattr(instance, self._name, signal)
        return signal


class _QObject:
    def __init__(self, parent: object = None) -> None:
        self._qt_thread: _QThread | None = None
        self._qt_parent: object | None = None
        self._qt_children: list[object] = []
        self.delete_scheduled = False
        self.deleted = False
        if parent is not None:
            self.setParent(parent)

    def moveToThread(self, thread: _QThread) -> None:
        self._qt_thread = thread

    def setParent(self, parent: object | None) -> None:
        previous = self._qt_parent
        if previous is parent:
            return
        if previous is not None:
            previous_children = previous._qt_children
            previous_children[:] = [child for child in previous_children if child is not self]
        self._qt_parent = parent
        if parent is not None:
            parent._qt_children.append(self)

    def parent(self) -> object | None:
        return self._qt_parent

    def children(self) -> list[object]:
        return list(self._qt_children)

    def findChildren(
        self,
        child_type: type[object],
        name: str | None = None,
    ) -> list[object]:
        return _find_qt_children(self, child_type, name)

    def deleteLater(self) -> None:
        self.delete_scheduled = True


class _QThread(_QObject):
    finished = _Signal()

    def __init__(self) -> None:
        super().__init__()
        self._events: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="fake-qt-worker")
        self._thread.start()

    def _run(self) -> None:
        while True:
            event = self._events.get()
            if event is None:
                break
            slot, args = event
            slot(*args)
        self.finished.emit()

    def post(self, slot: object, args: tuple[object, ...]) -> None:
        self._events.put((slot, args))

    def quit(self) -> None:
        self._events.put(None)

    def isRunning(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float = 1.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


class _Widget(_QObject):
    def __init__(self, *_args: object) -> None:
        _require_main_thread()
        super().__init__(*_args)
        self.object_name = ""
        self.hidden = False

    def setObjectName(self, name: str) -> None:
        _require_main_thread()
        self.object_name = name

    def objectName(self) -> str:
        _require_main_thread()
        return self.object_name

    def setParent(self, parent: object | None) -> None:
        _require_main_thread()
        super().setParent(parent)

    def hide(self) -> None:
        _require_main_thread()
        self.hidden = True


class _QDockWidget(_Widget):
    def __init__(self, title: str, parent: object = None) -> None:
        super().__init__(parent)
        self.title = title
        self.widget: object | None = None

    def setWidget(self, widget: object) -> None:
        _require_main_thread()
        self.widget = widget


class _QWidget(_Widget):
    pass


class _QVBoxLayout:
    def __init__(self, parent: object) -> None:
        _require_main_thread()
        self.parent = parent
        self.widgets: list[object] = []

    def addWidget(self, widget: object) -> None:
        _require_main_thread()
        self.widgets.append(widget)


class _QLabel(_Widget):
    def __init__(self, text: str, parent: object) -> None:
        super().__init__(parent)
        self.text = text

    def setText(self, text: str) -> None:
        _require_main_thread()
        self.text = text


class _QComboBox(_Widget):
    currentIndexChanged = _Signal(int)

    def __init__(self, parent: object) -> None:
        super().__init__(parent)
        self.items: list[str] = []
        self.index = -1
        self.blocked = False

    def clear(self) -> None:
        _require_main_thread()
        self.items.clear()
        self.setCurrentIndex(-1)

    def addItem(self, item: str) -> None:
        _require_main_thread()
        self.items.append(item)
        if len(self.items) == 1:
            self.setCurrentIndex(0)

    def currentIndex(self) -> int:
        _require_main_thread()
        return self.index

    def setCurrentIndex(self, index: int) -> None:
        _require_main_thread()
        if self.index == index:
            return
        self.index = index
        if not self.blocked:
            self.currentIndexChanged.emit(index)

    def blockSignals(self, blocked: bool) -> None:
        _require_main_thread()
        self.blocked = blocked


class _QPushButton(_Widget):
    clicked = _Signal()

    def __init__(self, text: str, parent: object) -> None:
        super().__init__(parent)
        self.text = text

    def click(self) -> None:
        _require_main_thread()
        self.clicked.emit()


class _ConnectionType:
    QueuedConnection = object()


class _DockWidgetArea:
    RightDockWidgetArea = object()


class _QtNestedOnly:
    ConnectionType = _ConnectionType
    DockWidgetArea = _DockWidgetArea


class _QtFlatOnly:
    QueuedConnection = _ConnectionType.QueuedConnection
    RightDockWidgetArea = _DockWidgetArea.RightDockWidgetArea


class _Qt(_QtNestedOnly):
    QueuedConnection = _ConnectionType.QueuedConnection
    RightDockWidgetArea = _DockWidgetArea.RightDockWidgetArea


_MAIN_EVENTS: queue.Queue[tuple[object, tuple[object, ...]]] = queue.Queue()


def install_fake_pyside(
    *,
    nested_only: bool = False,
    flat_only: bool = False,
) -> ModuleType:
    if nested_only and flat_only:
        raise ValueError("fake Qt enum shape is ambiguous")
    module = ModuleType("PySide")
    module.QtCore = ModuleType("PySide.QtCore")
    module.QtCore.QObject = _QObject
    module.QtCore.QThread = _QThread
    module.QtCore.Signal = _Signal
    module.QtCore.Slot = lambda *_args: lambda function: function
    if nested_only:
        module.QtCore.Qt = _QtNestedOnly
    elif flat_only:
        module.QtCore.Qt = _QtFlatOnly
    else:
        module.QtCore.Qt = _Qt
    module.QtWidgets = ModuleType("PySide.QtWidgets")
    module.QtWidgets.QDockWidget = _QDockWidget
    module.QtWidgets.QWidget = _QWidget
    module.QtWidgets.QVBoxLayout = _QVBoxLayout
    module.QtWidgets.QLabel = _QLabel
    module.QtWidgets.QComboBox = _QComboBox
    module.QtWidgets.QPushButton = _QPushButton
    return module


def pump_main_events(
    predicate: object,
    *,
    timeout: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("fake Qt event deadline exceeded")
        try:
            slot, args = _MAIN_EVENTS.get(timeout=min(remaining, 0.05))
        except queue.Empty:
            continue
        slot(*args)
