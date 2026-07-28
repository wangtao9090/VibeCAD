from __future__ import annotations

import threading
from collections.abc import Callable

from PySide import QtCore

from .dock import ReviewDock
from .gateway import KernelGateway

__all__ = (
    "activate_workbench",
    "deactivate_workbench",
    "workbench_snapshot",
)

_session: _WorkbenchSession | None = None
_last_snapshot: dict[str, object] | None = None
_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _qt_enum(nested_name: str, member_name: str) -> object:
    nested = getattr(QtCore.Qt, nested_name, None)
    if nested is not None:
        value = getattr(nested, member_name, None)
        if value is not None:
            return value
    value = getattr(QtCore.Qt, member_name, None)
    if value is None:
        raise RuntimeError(f"Qt enum is unavailable: {nested_name}.{member_name}")
    return value


_QUEUED_CONNECTION = _qt_enum("ConnectionType", "QueuedConnection")
_RIGHT_DOCK_WIDGET_AREA = _qt_enum("DockWidgetArea", "RightDockWidgetArea")


def _best_effort(action: Callable[..., object], *args: object) -> bool:
    try:
        action(*args)
    except BaseException:
        return False
    return True


class _GatewayWorker(QtCore.QObject):
    event_ready = QtCore.Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.gateway = KernelGateway()
        self.thread_id: int | None = None

    @property
    def client_construction_count(self) -> int:
        return self.gateway.client_construction_count

    @QtCore.Slot(object)
    def dispatch(self, command: object) -> None:
        self.thread_id = threading.get_ident()
        self.event_ready.emit(self.gateway.handle(command))


class _WorkbenchSession(QtCore.QObject):
    _partial_close = QtCore.Signal(object)

    def __init__(self, main_window: object) -> None:
        super().__init__()
        self.main_window = main_window
        self.main_thread_id = threading.get_ident()
        self.worker_thread_id: int | None = None
        self.daemon_id: str | None = None
        self.heartbeat_count = 0
        self.lifecycle = "starting"
        self._dock_added = False
        self._dock_parented = False
        self._dock_delete_attempted = False
        self._thread_started = False
        self._thread_retired = False
        self._close_request_id: int | None = None
        self.dock: ReviewDock | None = None
        self.thread: object | None = None
        self.worker: _GatewayWorker | None = None
        try:
            self.dock = ReviewDock(main_window)
            self._dock_parented = True
            self.thread = QtCore.QThread()
            self.worker = _GatewayWorker()
            self.worker.moveToThread(self.thread)
            self.dock.request.connect(
                self.worker.dispatch,
                _QUEUED_CONNECTION,
            )
            self._partial_close.connect(
                self.worker.dispatch,
                _QUEUED_CONNECTION,
            )
            self.worker.event_ready.connect(
                self._receive,
                _QUEUED_CONNECTION,
            )
            self.thread.finished.connect(self.worker.deleteLater)
            self.thread.finished.connect(
                self._finished,
                _QUEUED_CONNECTION,
            )
            self.main_window.addDockWidget(
                _RIGHT_DOCK_WIDGET_AREA,
                self.dock,
            )
            self._dock_added = True
            self.thread.start()
            self._thread_started = True
            self.dock.start()
        except BaseException:
            self.lifecycle = "stopping"
            if self._thread_started:
                self._detach_dock(schedule_delete=False)
                self._queue_partial_close()
            else:
                self._dispose_unstarted()

    @property
    def client_construction_count(self) -> int:
        worker = self.worker
        return 0 if worker is None else worker.client_construction_count

    @property
    def _dock_count(self) -> int:
        return 1 if self._dock_added or self._dock_parented else 0

    def _detach_dock(self, *, schedule_delete: bool = True) -> None:
        dock = self.dock
        if dock is None or self._dock_delete_attempted:
            return
        _best_effort(dock.hide)
        if self._dock_added and _best_effort(
            self.main_window.removeDockWidget,
            dock,
        ):
            self._dock_added = False
        if self._dock_parented and _best_effort(dock.setParent, None):
            self._dock_parented = False
        if schedule_delete and not self._dock_count and not self._dock_delete_attempted:
            self._dock_delete_attempted = True
            _best_effort(dock.deleteLater)

    def _dispose_unstarted(self) -> None:
        self._detach_dock()
        if self.worker is not None:
            _best_effort(self.worker.deleteLater)
        if self.thread is not None:
            _best_effort(self.thread.deleteLater)
        self._thread_retired = True
        self.lifecycle = "stopping" if self._dock_count else "inactive"
        self.worker_thread_id = None
        self.daemon_id = None

    def _retry_retired_cleanup(self) -> bool:
        if self.lifecycle != "stopping" or not self._thread_retired:
            return False
        self._detach_dock()
        if self._dock_count:
            return False
        self.lifecycle = "inactive"
        return True

    def _queue_partial_close(self) -> None:
        self._close_request_id = _MAX_SAFE_INTEGER
        self._partial_close.emit(
            {
                "schema_version": 1,
                "request_id": self._close_request_id,
                "kind": "close",
            }
        )

    def _is_expected_closed(self, event: object) -> bool:
        return (
            type(event) is dict
            and set(event) == {"schema_version", "request_id", "kind"}
            and type(event.get("schema_version")) is int
            and event.get("schema_version") == 1
            and type(event.get("request_id")) is int
            and event.get("request_id") == self._close_request_id
            and type(event.get("kind")) is str
            and event.get("kind") == "closed"
        )

    @QtCore.Slot(object)
    def _receive(self, event: object) -> None:
        self.heartbeat_count += 1
        worker = self.worker
        if worker is not None:
            self.worker_thread_id = worker.thread_id
        if type(event) is dict and event.get("kind") == "connected":
            daemon_id = event.get("daemon_id")
            worker_thread_id = event.get("worker_thread_id")
            if type(daemon_id) is str and type(worker_thread_id) is int:
                self.daemon_id = daemon_id
                self.worker_thread_id = worker_thread_id
                if self.lifecycle == "starting":
                    self.lifecycle = "active"
        expected_closed = self._is_expected_closed(event)
        dock = self.dock
        if dock is not None and (self.lifecycle != "stopping" or expected_closed):
            dock.handle_event(event)
        if expected_closed:
            thread = self.thread
            if thread is not None:
                thread.quit()

    def close_async(self) -> None:
        if self.lifecycle not in {"starting", "active"}:
            return
        self.lifecycle = "stopping"
        dock = self.dock
        if dock is not None:
            self._close_request_id = dock.request_close()

    @QtCore.Slot()
    def _finished(self) -> None:
        global _session
        self._thread_retired = True
        self._thread_started = False
        self._detach_dock()
        if self.thread is not None:
            _best_effort(self.thread.deleteLater)
        self.lifecycle = "stopping" if self._dock_count else "inactive"
        self.worker_thread_id = None
        self.daemon_id = None
        if _session is self:
            _remember(self, dock_count=self._dock_count)
            if not self._dock_count:
                _session = None

    def snapshot(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "lifecycle": self.lifecycle,
            "dock_count": self._dock_count,
            "main_thread_id": self.main_thread_id,
            "worker_thread_id": self.worker_thread_id,
            "daemon_id": self.daemon_id,
            "heartbeat_count": self.heartbeat_count,
            "client_construction_count": self.client_construction_count,
        }


def _inactive_snapshot(main_thread_id: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "lifecycle": "inactive",
        "dock_count": 0,
        "main_thread_id": main_thread_id,
        "worker_thread_id": None,
        "daemon_id": None,
        "heartbeat_count": 0,
        "client_construction_count": 0,
    }


def _remember(session: _WorkbenchSession, *, dock_count: int) -> None:
    global _last_snapshot
    _last_snapshot = session.snapshot()
    _last_snapshot["dock_count"] = dock_count
    _last_snapshot["lifecycle"] = session.lifecycle


def activate_workbench() -> None:
    global _session
    if _session is not None:
        session = _session
        if not session._retry_retired_cleanup():
            _remember(session, dock_count=session._dock_count)
            return
        _remember(session, dock_count=0)
        _session = None
    freecad_gui = __import__("FreeCADGui")
    session = _WorkbenchSession(freecad_gui.getMainWindow())
    _session = session if session.lifecycle != "inactive" else None
    _remember(session, dock_count=session._dock_count)


def deactivate_workbench() -> None:
    if _session is not None:
        _session.close_async()
        _remember(_session, dock_count=_session._dock_count)


def workbench_snapshot() -> dict[str, object]:
    global _last_snapshot
    if _session is not None:
        return dict(_session.snapshot())
    if _last_snapshot is None:
        _last_snapshot = _inactive_snapshot(threading.get_ident())
    return dict(_last_snapshot)
