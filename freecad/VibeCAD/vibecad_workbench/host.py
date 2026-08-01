from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from PySide import QtCore

from .bridge import external_client_factory
from .dock import ReviewDock
from .gateway import (
    KernelGateway,
    _detach,
    _plain_wire,
    _PrivateWireCommand,
    _PrivateWireEvent,
)
from .preview import PreviewCoordinator, PreviewError
from .selection import (
    ManagedSelectionObserver,
    SelectionCaptureError,
    capture_managed_selector,
)
from .state import ProjectionError

__all__ = (
    "activate_workbench",
    "deactivate_workbench",
    "workbench_snapshot",
)

_session: _WorkbenchSession | None = None
_last_snapshot: dict[str, object] | None = None
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_CLEANUP_ID_RESERVE = 1024
_NORMAL_ID_MAX = _MAX_SAFE_INTEGER - _CLEANUP_ID_RESERVE
_MAX_PENDING_REQUESTS = 64
_LANE_NORMAL = "normal"
_LANE_PRIVATE = "private"
_EXPECTED_EVENTS = {
    "connect": "connected",
    "list_projects": "projects",
    "list_tasks": "tasks",
    "refresh_project": "project",
    "refresh_task": "task",
    "preview_open": "preview_opened",
    "preview_refresh": "preview_refreshed",
    "preview_close": "preview_closed",
    "review": "review",
    "close": "closed",
}
_RESTRICTED_OPERATIONS = frozenset(("preview_close", "review", "close"))


@dataclass(frozen=True, slots=True)
class _RefreshBarrier:
    generation: int
    cycle_id: int
    selection_stamp: tuple[object, ...]
    bindings: tuple[tuple[str, str, int], ...]


@dataclass(frozen=True, slots=True)
class _FreshToken:
    generation: int
    cycle_id: int
    selection_stamp: tuple[object, ...]
    source_kind: str
    checkout_id: str
    binding_identity: int
    descriptor: object


_PendingRequest = tuple[
    str,
    object,
    str,
    str,
    dict[str, object],
    bool,
    object,
]


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

    def __init__(self, capability: object) -> None:
        super().__init__()
        client_factory = external_client_factory()
        self.gateway = (
            KernelGateway()
            if client_factory is None
            else KernelGateway(client_factory=client_factory)
        )
        self.gateway._bind_wire_capability(capability)
        self.thread_id: int | None = None

    @property
    def client_construction_count(self) -> int:
        return self.gateway.client_construction_count

    @QtCore.Slot(object)
    def dispatch(self, command: object) -> None:
        self.thread_id = threading.get_ident()
        event = self.gateway.handle(command)
        if (
            type(command) is _PrivateWireCommand
            and command.capability is self.gateway._wire_capability
            and type(event) is dict
        ):
            event = _PrivateWireEvent(event, command.capability)
        self.event_ready.emit(event)


class _WorkbenchSession(QtCore.QObject):
    _dispatch = QtCore.Signal(object)

    def __init__(self, main_window: object, freecad_gui: object) -> None:
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
        self._thread_start_attempted = False
        self._thread_started = False
        self._thread_retired = False
        self._retirement_authorized = False
        self._close_request_id: int | None = None
        self._cleanup_request_id: int | None = None
        self._cleanup_checkout_id: str | None = None
        self._cleanup_retry_requested = False
        self._cleanup_reconcile_attempted = False
        self._client_close_requested = False
        self._wire_capability = object()
        self._pending: dict[int, _PendingRequest] = {}
        self._cleanup_cursor = _NORMAL_ID_MAX + 1
        self._open_recovery_required = False
        self._review_enqueue_ambiguous_id: int | None = None
        self._refresh_generation = 0
        self._refresh_barrier: _RefreshBarrier | None = None
        self._refresh_candidates: dict[str, _FreshToken] = {}
        self._review_tokens: dict[str, _FreshToken] = {}
        self._cleanup_cycle_id: int | None = None
        self._fresh_preview_descriptors: dict[str, object] = {}
        self.preview: PreviewCoordinator | None = None
        self.selection: ManagedSelectionObserver | None = None
        self.dock: ReviewDock | None = None
        self.thread: object | None = None
        self.worker: _GatewayWorker | None = None
        try:
            self.dock = ReviewDock(main_window)
            self._dock_parented = True
            self.thread = QtCore.QThread()
            self.worker = _GatewayWorker(self._wire_capability)
            self.worker.moveToThread(self.thread)
            self._dispatch.connect(
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
            self.dock._bind_host_transport(self._dock_transport)
            self.dock._bind_review_host(
                submit_review=self._request_review,
                discard_preview=self._discard_preview_binding,
            )
            self.selection = ManagedSelectionObserver(
                freecad_gui.Selection,
                capture=self._capture_selection,
                clear=self._clear_selection,
                reject=self._reject_selection,
            )
            self.selection.attach()
            self._thread_start_attempted = True
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

    def _clear_selection(self) -> None:
        dock = self.dock
        if dock is not None:
            dock._clear_selector()

    def _reject_selection(self) -> None:
        dock = self.dock
        if dock is not None:
            dock._reject_selector()

    def _capture_selection(
        self,
        selected_object: object,
        document: object,
        subelements: tuple[str, ...],
    ) -> None:
        dock = self.dock
        coordinator = self.preview
        if dock is None or coordinator is None or self.lifecycle != "active":
            self._reject_selection()
            return
        try:
            if selected_object.Document is not document:
                raise TypeError("invalid selected document")
            matches: list[tuple[str, object]] = []
            for source_kind, checkout_id in tuple(dock._preview_checkouts.items()):
                binding = coordinator._observe_local_binding(checkout_id)
                if binding.document is document:
                    matches.append((source_kind, binding))
            if len(matches) != 1:
                raise PreviewError("selection is not in one managed preview")
            source_kind, binding = matches[0]
            descriptor = dict(binding.descriptor)
            source = dict(descriptor["source"])
            project_id = source.get("project_id")
            revision_id = source.get("revision_id")
            if (
                source.get("kind") != source_kind
                or type(project_id) is not str
                or type(revision_id) is not str
                or project_id != dock.current_project_id()
                or dock._preview_checkouts.get(source_kind) != descriptor.get("checkout_id")
            ):
                raise PreviewError("selection binding is stale")
            captured = capture_managed_selector(
                selected_object=selected_object,
                document_objects=document.Objects,
                project_id=project_id,
                revision_id=revision_id,
                subelements=subelements,
            )
        except SelectionCaptureError as error:
            dock._reject_selector(unsupported=error.code == "unsupported_subelement")
            return
        except (KeyError, PreviewError, RuntimeError, TypeError, ValueError):
            dock._reject_selector()
            return
        dock._set_selector_capture(captured.text)

    def _detach_selection(self) -> None:
        selection = self.selection
        if selection is None:
            return
        if _best_effort(selection.detach) and not selection.attached:
            self.selection = None

    def _detach_dock(self, *, schedule_delete: bool = True) -> None:
        self._detach_selection()
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
        if self._thread_start_attempted:
            self._open_recovery_required = True
            dock = self.dock
            if dock is not None:
                try:
                    dock.set_preview_eligibility(
                        False,
                        recovery_required=True,
                    )
                except BaseException:
                    pass
            return
        self._detach_dock()
        if self.worker is not None:
            _best_effort(self.worker.deleteLater)
        if self.thread is not None:
            _best_effort(self.thread.deleteLater)
        self._thread_retired = True
        self._retirement_authorized = True
        self.lifecycle = "stopping" if self._dock_count else "inactive"
        self.worker_thread_id = None
        self.daemon_id = None

    def _retry_retired_cleanup(self) -> bool:
        if (
            self.lifecycle != "stopping"
            or not self._thread_retired
            or not self._retirement_authorized
        ):
            return False
        self._detach_dock()
        if self._dock_count:
            return False
        self.lifecycle = "inactive"
        return True

    def _transport_exhausted(self) -> None:
        self.lifecycle = "stopping"
        self._retire_refresh_authority_for_stopping()
        dock = self.dock
        coordinator = self.preview
        if coordinator is not None:
            try:
                if self._cleanup_cycle_id is None:
                    self._cleanup_cycle_id = coordinator._active_cycle_id()
                coordinator.close_documents()
            except PreviewError as error:
                if error.recovery_required:
                    self._open_recovery_required = True
                    if dock is not None:
                        dock.set_preview_eligibility(
                            False,
                            recovery_required=True,
                        )
                elif dock is not None:
                    dock.set_preview_eligibility(False)
        raise ProjectionError("host request authority exhausted")

    def _selection_stamp(self) -> tuple[object, ...]:
        dock = self.dock
        if dock is None:
            return (None, None, None, None, -1, -1)
        task = dock._selected_task()
        return (
            dock.current_project_id(),
            dock.current_task_id(),
            dock._task_value(task, "draft_id"),
            dock._task_value(task, "generation"),
            dock._selection_epoch,
            dock._task_selection_epoch,
        )

    def _invalidate_review_tokens(self) -> None:
        self._refresh_candidates.clear()
        self._review_tokens.clear()
        self._fresh_preview_descriptors.clear()
        dock = self.dock
        if dock is not None:
            dock.set_preview_eligibility(False)

    def _retire_refresh_authority_for_stopping(self) -> None:
        barrier = self._refresh_barrier
        if barrier is not None:
            for _source_kind, checkout_id, _binding_identity in barrier.bindings:
                self._poison_refresh_binding(checkout_id)
        self._invalidate_review_tokens()
        self._refresh_barrier = None

    def _begin_refresh(self, checkout_ids: tuple[str, ...]) -> None:
        if self._refresh_generation >= _MAX_SAFE_INTEGER or len(set(checkout_ids)) != len(
            checkout_ids
        ):
            self._transport_exhausted()
        self._refresh_generation += 1
        self._invalidate_review_tokens()
        self._refresh_barrier = None
        dock = self.dock
        coordinator = self.preview
        if dock is None or coordinator is None:
            return
        cycle_id = coordinator._active_cycle_id()
        if type(cycle_id) is not int:
            return
        bindings: list[tuple[str, str, int]] = []
        try:
            if tuple(dock._preview_checkouts.values()) != checkout_ids:
                return
            for source_kind, checkout_id in sorted(dock._preview_checkouts.items()):
                identity = coordinator._binding_identity(checkout_id)
                if identity[0] != cycle_id or identity[1] != checkout_id:
                    return
                bindings.append((source_kind, checkout_id, identity[2]))
        except (PreviewError, RuntimeError, TypeError, ValueError):
            return
        self._refresh_barrier = _RefreshBarrier(
            generation=self._refresh_generation,
            cycle_id=cycle_id,
            selection_stamp=self._selection_stamp(),
            bindings=tuple(bindings),
        )

    def _refresh_metadata(
        self,
        checkout_id: object,
    ) -> tuple[int, int, int, tuple[object, ...], str] | None:
        barrier = self._refresh_barrier
        if barrier is None or type(checkout_id) is not str:
            return None
        for source_kind, candidate, identity in barrier.bindings:
            if candidate == checkout_id:
                return (
                    barrier.generation,
                    barrier.cycle_id,
                    identity,
                    barrier.selection_stamp,
                    source_kind,
                )
        return None

    def _has_pending_refresh(self, generation: int | None = None) -> bool:
        for pending in self._pending.values():
            if pending[2] != "preview_refresh" or pending[5] is not True:
                continue
            metadata = pending[6]
            if generation is None:
                return True
            if type(metadata) is tuple and len(metadata) == 5 and metadata[0] == generation:
                return True
        return False

    def _has_pending_review(self) -> bool:
        return any(pending[2] == "review" for pending in self._pending.values())

    def _poison_refresh_binding(self, checkout_id: object) -> None:
        if type(checkout_id) is not str:
            return
        try:
            self._coordinator().poison_binding(checkout_id)
        except (PreviewError, RuntimeError, TypeError, ValueError):
            pass

    def _try_mint_review_tokens(self) -> bool:
        barrier = self._refresh_barrier
        dock = self.dock
        coordinator = self.preview
        if (
            barrier is None
            or dock is None
            or coordinator is None
            or self.lifecycle != "active"
            or self._has_pending_refresh(barrier.generation)
            or self._has_pending_review()
            or barrier.selection_stamp != self._selection_stamp()
            or {item[0] for item in barrier.bindings} != {"head", "draft"}
            or set(self._refresh_candidates) != {"head", "draft"}
            or dock._preview_checkouts
            != {
                source_kind: checkout_id for source_kind, checkout_id, _identity in barrier.bindings
            }
        ):
            if dock is not None:
                dock.set_preview_eligibility(False)
            return False
        tokens: dict[str, _FreshToken] = {}
        try:
            current_project_id = dock.current_project_id()
            task = dock._selected_task()
            candidate_revision = dock._task_value(task, "candidate_revision")
            base_revision = dock._task_value(task, "base_revision")
            if (
                type(current_project_id) is not str
                or type(candidate_revision) is not str
                or type(base_revision) is not str
                or not coordinator.aggregate_review_eligible(
                    expected_project_id=current_project_id,
                    expected_candidate_revision=candidate_revision,
                    expected_base_revision=base_revision,
                )
            ):
                raise PreviewError("invalid aggregate review authority")
            for source_kind, checkout_id, binding_identity in barrier.bindings:
                token = self._refresh_candidates[source_kind]
                identity = coordinator._binding_identity(checkout_id)
                binding = coordinator._validate_local_binding(checkout_id)
                if (
                    token.generation != barrier.generation
                    or token.cycle_id != barrier.cycle_id
                    or token.selection_stamp != barrier.selection_stamp
                    or token.checkout_id != checkout_id
                    or token.binding_identity != binding_identity
                    or identity
                    != (
                        barrier.cycle_id,
                        checkout_id,
                        binding_identity,
                    )
                    or id(binding) != binding_identity
                    or coordinator.validate_binding(
                        checkout_id,
                        token.descriptor,
                    )
                    is not binding
                ):
                    raise PreviewError("invalid shared refresh barrier")
                tokens[source_kind] = token
        except (PreviewError, RuntimeError, TypeError, ValueError) as error:
            for _kind, checkout_id, _identity in barrier.bindings:
                self._poison_refresh_binding(checkout_id)
            self._refresh_candidates.clear()
            self._review_tokens.clear()
            dock.set_preview_eligibility(
                False,
                recovery_required=(isinstance(error, PreviewError) and error.recovery_required),
            )
            return False
        self._review_tokens = tokens
        dock.set_preview_eligibility(True)
        return True

    def _reserve_dock_request(self, intent: dict[str, object]) -> int:
        if set(intent) != {
            "phase",
            "expected_kind",
            "kind",
            "context",
            "payload",
            "projected_cursor",
        }:
            raise ProjectionError("invalid host transport intent")
        expected_kind = intent["expected_kind"]
        operation = intent["kind"]
        context = intent["context"]
        payload = intent["payload"]
        request_id = intent["projected_cursor"]
        dock = self.dock
        if (
            type(expected_kind) is not str
            or type(operation) is not str
            or _EXPECTED_EVENTS.get(operation) != expected_kind
            or operation in _RESTRICTED_OPERATIONS
            or type(payload) is not dict
            or any(type(key) is not str for key in payload)
            or bool(set(payload) & {"schema_version", "request_id", "kind"})
            or type(request_id) is not int
            or dock is None
            or request_id != dock._sequence
            or not 0 <= request_id <= _NORMAL_ID_MAX
            or request_id in self._pending
            or len(self._pending) >= _MAX_PENDING_REQUESTS
            or self.lifecycle not in {"starting", "active"}
            or self._thread_retired
        ):
            self._transport_exhausted()
        command = _detach(
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": operation,
                **payload,
            }
        )
        assert type(command) is dict
        if operation == "preview_open":
            self._invalidate_review_tokens()
            self._refresh_barrier = None
        metadata: object = None
        if operation == "preview_refresh":
            metadata = self._refresh_metadata(context)
        self._pending[request_id] = (
            expected_kind,
            context,
            operation,
            _LANE_NORMAL,
            command,
            False,
            metadata,
        )
        return request_id

    def _reserve_private_request(
        self,
        expected_kind: str,
        operation: str,
        *,
        context: object = None,
        **payload: object,
    ) -> int:
        request_id = self._cleanup_cursor
        if (
            _EXPECTED_EVENTS.get(operation) != expected_kind
            or operation not in _RESTRICTED_OPERATIONS
            or type(request_id) is not int
            or not _NORMAL_ID_MAX < request_id <= _MAX_SAFE_INTEGER
            or request_id in self._pending
            or len(self._pending) >= _MAX_PENDING_REQUESTS
        ):
            self._transport_exhausted()
        self._cleanup_cursor = request_id + 1
        command = _detach(
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": operation,
                **payload,
            }
        )
        assert type(command) is dict
        self._pending[request_id] = (
            expected_kind,
            context,
            operation,
            _LANE_PRIVATE,
            command,
            False,
            None,
        )
        return request_id

    def _reserve_private_normal_request(
        self,
        expected_kind: str,
        operation: str,
        *,
        context: object = None,
        **payload: object,
    ) -> int:
        dock = self.dock
        request_id = None if dock is None else dock._sequence
        allowed = {
            ("review", "review"),
            ("preview_close", "preview_closed"),
        }
        if (
            (operation, expected_kind) not in allowed
            or operation not in _RESTRICTED_OPERATIONS
            or _EXPECTED_EVENTS.get(operation) != expected_kind
            or type(request_id) is not int
            or not 0 <= request_id <= _NORMAL_ID_MAX
            or request_id in self._pending
            or len(self._pending) >= _MAX_PENDING_REQUESTS
            or dock is None
            or self.lifecycle != "active"
        ):
            self._transport_exhausted()
        command = _detach(
            {
                "schema_version": 1,
                "request_id": request_id,
                "kind": operation,
                **payload,
            }
        )
        assert type(command) is dict
        self._pending[request_id] = (
            expected_kind,
            context,
            operation,
            _LANE_PRIVATE,
            command,
            False,
            None,
        )
        dock._sequence = request_id + 1
        return request_id

    def _enqueue_request(
        self,
        request_id: object,
        *,
        commit_before_emit: bool = False,
    ) -> None:
        if type(request_id) is not int:
            raise ProjectionError("invalid host transport request")
        pending = self._pending.get(request_id)
        if pending is None or pending[5] is not False:
            raise ProjectionError("invalid host transport request")
        command = _detach(pending[4])
        assert type(command) is dict
        wire: object = command
        if pending[3] == _LANE_PRIVATE:
            wire = _PrivateWireCommand(command, self._wire_capability)
        committed = (
            *pending[:5],
            True,
            pending[6],
        )
        if commit_before_emit:
            self._pending[request_id] = committed
        try:
            self._dispatch.emit(wire)
        except BaseException:
            raise
        if commit_before_emit:
            if self._pending.get(request_id) != committed:
                raise ProjectionError("host transport reservation changed")
            return
        if self._pending.get(request_id) is not pending:
            raise ProjectionError("host transport reservation changed")
        self._pending[request_id] = committed

    def _dock_transport(self, message: object) -> object:
        if type(message) is not dict or any(type(key) is not str for key in message):
            raise ProjectionError("invalid host transport message")
        phase = message.get("phase")
        if phase == "reserve":
            return self._reserve_dock_request(message)
        if phase == "enqueue":
            if set(message) != {"phase", "request_id"}:
                raise ProjectionError("invalid host transport message")
            self._enqueue_request(message["request_id"])
            return None
        if phase == "cancel":
            if (
                set(message) != {"phase", "request_id", "if_not_enqueued"}
                or message.get("if_not_enqueued") is not True
            ):
                raise ProjectionError("invalid host transport message")
            request_id = message["request_id"]
            pending = self._pending.get(request_id) if type(request_id) is int else None
            if pending is not None and pending[5] is False:
                self._pending.pop(request_id, None)
            return None
        if phase == "refresh_begin":
            checkout_ids = message.get("checkout_ids")
            if (
                set(message) != {"phase", "checkout_ids"}
                or type(checkout_ids) is not tuple
                or any(type(checkout_id) is not str for checkout_id in checkout_ids)
            ):
                raise ProjectionError("invalid host transport message")
            self._begin_refresh(checkout_ids)
            return None
        raise ProjectionError("invalid host transport message")

    def _queue_partial_close(self) -> None:
        try:
            self._client_close_requested = True
            self._close_request_id = self._reserve_private_request(
                "closed",
                "close",
            )
            self._enqueue_request(
                self._close_request_id,
                commit_before_emit=True,
            )
        except (ProjectionError, RuntimeError):
            self._open_recovery_required = True

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

    def _coordinator(self) -> PreviewCoordinator:
        if self.preview is None:
            self.preview = PreviewCoordinator(__import__("FreeCAD"))
        return self.preview

    @staticmethod
    def _preview_error(event: dict[str, object], operation: str) -> dict[str, object]:
        request_id = event.get("request_id")
        if type(request_id) is not int:
            request_id = -1
            operation = "invalid"
        return {
            "schema_version": 1,
            "request_id": request_id,
            "kind": "error",
            "operation": operation,
            "code": "invalid_input",
            "outcome": "known_failure",
        }

    def _zero_control_plane_authority(
        self,
        *,
        coordinator_cycle_id: int | None,
        allowed_close_request_id: int | None = None,
    ) -> bool:
        dock = self.dock
        coordinator = self.preview
        if (
            dock is None
            or self._open_recovery_required
            or self._cleanup_request_id is not None
            or self._cleanup_checkout_id is not None
            or self._cleanup_retry_requested
            or self._cleanup_reconcile_attempted
            or self._review_tokens
            or self._refresh_candidates
            or self._fresh_preview_descriptors
            or self._refresh_barrier is not None
            or self._review_enqueue_ambiguous_id is not None
            or dock._preview_checkouts
            or dock._preview_pending_sources
            or dock._preview_epochs
            or dock._pending
            or dock._hosted_projection is not None
        ):
            return False
        if allowed_close_request_id is None:
            if self._pending or self._client_close_requested or self._close_request_id is not None:
                return False
        else:
            pending = self._pending.get(allowed_close_request_id)
            if (
                type(allowed_close_request_id) is not int
                or set(self._pending) != {allowed_close_request_id}
                or pending is None
                or pending[0] != "closed"
                or pending[1] is not None
                or pending[2] != "close"
                or pending[3] != _LANE_PRIVATE
                or pending[5] is not True
                or not self._client_close_requested
                or self._close_request_id != allowed_close_request_id
            ):
                return False
        try:
            if type(coordinator_cycle_id) is int:
                return (
                    coordinator is not None
                    and self._cleanup_cycle_id == coordinator_cycle_id
                    and coordinator._active_cycle_id() == coordinator_cycle_id
                    and coordinator.cleanup_complete()
                    and coordinator._retired_cycle_ready(coordinator_cycle_id)
                )
            return (
                coordinator_cycle_id is None
                and self._cleanup_cycle_id is None
                and (
                    coordinator is None
                    or (coordinator.cleanup_complete() and coordinator._active_cycle_id() is None)
                )
            )
        except (PreviewError, RuntimeError, TypeError, ValueError):
            return False

    def _advance_cleanup(self) -> None:
        if self._cleanup_request_id is not None or self._client_close_requested:
            return
        dock = self.dock
        if dock is None:
            return
        if dock.pending_preview_open_count():
            return
        if any(
            pending[2] in {"preview_open", "preview_refresh"} for pending in self._pending.values()
        ):
            return
        coordinator = self.preview
        if coordinator is not None:
            if self._cleanup_cycle_id is None:
                self._cleanup_cycle_id = coordinator._active_cycle_id()
            ready = coordinator.ready_checkout_ids()
            if ready:
                checkout_id = ready[0]
                active_normal_reserve = self.lifecycle == "active"
                reserve = (
                    self._reserve_private_normal_request
                    if active_normal_reserve
                    else self._reserve_private_request
                )
                try:
                    request_id = reserve(
                        "preview_closed",
                        "preview_close",
                        context=checkout_id,
                        checkout_id=checkout_id,
                        document_absent=True,
                    )
                except ProjectionError:
                    if (
                        active_normal_reserve
                        and self.lifecycle == "stopping"
                        and not self._open_recovery_required
                    ):
                        return
                    self._open_recovery_required = True
                    return
                except (RuntimeError, TypeError, ValueError):
                    self._open_recovery_required = True
                    return
                self._cleanup_checkout_id = checkout_id
                self._cleanup_request_id = request_id
                try:
                    self._enqueue_request(
                        request_id,
                        commit_before_emit=True,
                    )
                except (ProjectionError, RuntimeError, TypeError, ValueError):
                    self._open_recovery_required = True
                return
            if not coordinator.cleanup_complete():
                return
            cycle_id = self._cleanup_cycle_id
            if type(cycle_id) is int:
                if not self._zero_control_plane_authority(
                    coordinator_cycle_id=cycle_id,
                ):
                    return
                try:
                    coordinator._finalize_retired_cycle(cycle_id)
                except (PreviewError, RuntimeError, TypeError, ValueError):
                    self._open_recovery_required = True
                    return
                self._cleanup_cycle_id = None
                try:
                    dock._project_host_preview_cycle_retired()
                except (ProjectionError, RuntimeError, TypeError, ValueError):
                    self._open_recovery_required = True
                    dock.set_preview_eligibility(
                        False,
                        recovery_required=True,
                    )
                    return
        if (
            self.lifecycle != "stopping"
            or self._open_recovery_required
            or self._review_enqueue_ambiguous_id is not None
            or any(pending[3] == _LANE_PRIVATE for pending in self._pending.values())
            or any(pending[5] is True for pending in self._pending.values())
            or not self._zero_control_plane_authority(
                coordinator_cycle_id=None,
            )
        ):
            return
        try:
            request_id = self._reserve_private_request(
                "closed",
                "close",
            )
            self._client_close_requested = True
            self._close_request_id = request_id
            self._enqueue_request(
                request_id,
                commit_before_emit=True,
            )
        except (ProjectionError, RuntimeError, TypeError, ValueError):
            self._open_recovery_required = True

    def _review_ready(
        self,
        *,
        decision: object,
        task_id: object,
        draft_id: object,
        expected_generation: object,
    ) -> bool:
        dock = self.dock
        coordinator = self.preview
        barrier = self._refresh_barrier
        tokens = self._review_tokens
        if (
            self.lifecycle != "active"
            or dock is None
            or coordinator is None
            or barrier is None
            or decision not in ("accept", "reject")
            or type(task_id) is not str
            or type(draft_id) is not str
            or type(expected_generation) is not int
            or self._has_pending_refresh()
            or self._has_pending_review()
            or set(tokens) != {"head", "draft"}
            or barrier.selection_stamp != self._selection_stamp()
        ):
            return False
        task = dock._selected_task()
        current_project_id = dock.current_project_id()
        if (
            dock.current_task_id() != task_id
            or dock._task_value(task, "task_id") != task_id
            or dock._task_value(task, "draft_id") != draft_id
            or dock._task_value(task, "generation") != expected_generation
            or dock._task_value(task, "status") != "awaiting_user_review"
        ):
            return False
        try:
            candidate_revision = dock._task_value(task, "candidate_revision")
            base_revision = dock._task_value(task, "base_revision")
            if (
                type(current_project_id) is not str
                or dock._task_value(task, "project_id") != current_project_id
                or type(candidate_revision) is not str
                or type(base_revision) is not str
                or not coordinator.aggregate_review_eligible(
                    expected_project_id=current_project_id,
                    expected_candidate_revision=candidate_revision,
                    expected_base_revision=base_revision,
                )
            ):
                raise PreviewError("invalid aggregate review authority")
            for source_kind, checkout_id, binding_identity in barrier.bindings:
                token = tokens[source_kind]
                identity = coordinator._binding_identity(checkout_id)
                binding = coordinator._validate_local_binding(checkout_id)
                if (
                    token.generation != barrier.generation
                    or token.cycle_id != barrier.cycle_id
                    or token.selection_stamp != barrier.selection_stamp
                    or token.checkout_id != checkout_id
                    or token.binding_identity != binding_identity
                    or identity
                    != (
                        barrier.cycle_id,
                        checkout_id,
                        binding_identity,
                    )
                    or id(binding) != binding_identity
                    or coordinator.validate_binding(
                        checkout_id,
                        token.descriptor,
                    )
                    is not binding
                ):
                    raise PreviewError("invalid review authority")
                if (
                    coordinator.attest_review_binding(
                        checkout_id,
                        token.descriptor,
                    )
                    is not binding
                ):
                    raise PreviewError("invalid final local file observation")
            draft_token = tokens["draft"]
            draft_binding = coordinator.binding_for_checkout(draft_token.checkout_id)
            source = dict(draft_binding.source)
            if (
                source.get("kind") != "draft"
                or source.get("task_id") != task_id
                or source.get("draft_id") != draft_id
                or source.get("expected_generation") != expected_generation
            ):
                raise PreviewError("invalid draft review authority")
        except (KeyError, PreviewError, RuntimeError, TypeError, ValueError):
            for token in tuple(tokens.values()):
                self._poison_refresh_binding(token.checkout_id)
            return False
        return True

    def _request_review(
        self,
        *,
        decision: str,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> int:
        if not self._review_ready(
            decision=decision,
            task_id=task_id,
            draft_id=draft_id,
            expected_generation=expected_generation,
        ):
            self._invalidate_review_tokens()
            raise PreviewError("fresh shared review authority required")
        context = self._selection_stamp()
        self._invalidate_review_tokens()
        request_id = self._reserve_private_normal_request(
            "review",
            "review",
            context=context,
            decision=decision,
            task_id=task_id,
            draft_id=draft_id,
            expected_generation=expected_generation,
        )
        try:
            self._enqueue_request(
                request_id,
                commit_before_emit=True,
            )
        except BaseException:
            pending = self._pending.get(request_id)
            if (
                pending is None
                or pending[2] != "review"
                or pending[3] != _LANE_PRIVATE
                or pending[5] is not True
                or self._review_enqueue_ambiguous_id is not None
            ):
                self._open_recovery_required = True
            else:
                self._review_enqueue_ambiguous_id = request_id
            self.lifecycle = "stopping"
            self._retire_refresh_authority_for_stopping()
            dock = self.dock
            if dock is not None:
                dock.set_preview_eligibility(
                    False,
                    recovery_required=True,
                )
            raise
        return request_id

    def _guard_preview_binding(self, checkout_id: object) -> None:
        coordinator = self._coordinator()
        try:
            binding = coordinator._observe_local_binding(checkout_id)
        except PreviewError:
            if type(checkout_id) is str:
                self._fresh_preview_descriptors.pop(checkout_id, None)
            raise
        canonical = dict(binding.descriptor)["checkout_id"]
        descriptor = self._fresh_preview_descriptors.pop(str(canonical), None)
        if descriptor is None:
            raise PreviewError("fresh preview descriptor required")
        coordinator.validate_binding(canonical, descriptor)

    def _discard_preview_binding(self, checkout_id: object) -> None:
        coordinator = self._coordinator()
        if self._cleanup_cycle_id is None:
            self._cleanup_cycle_id = coordinator._active_cycle_id()
        coordinator.binding_for_checkout(checkout_id)
        self._invalidate_review_tokens()
        self._refresh_barrier = None
        coordinator.discard_document(checkout_id)
        dock = self.dock
        if dock is not None:
            dock.set_preview_eligibility(False)
        self._advance_cleanup()

    def _wire_event(
        self,
        received: object,
    ) -> tuple[dict[str, object], str] | None:
        lane: str
        candidate: object
        if type(received) is dict:
            lane = _LANE_NORMAL
            candidate = received
        elif type(received) is _PrivateWireEvent and received.capability is self._wire_capability:
            lane = _LANE_PRIVATE
            candidate = _plain_wire(received.payload)
        else:
            return None
        try:
            event = _detach(candidate)
        except (TypeError, ValueError):
            return None
        if type(event) is not dict:
            return None
        return event, lane

    def _correlated_pending(
        self,
        event: dict[str, object],
        lane: str,
    ) -> tuple[int, _PendingRequest] | None:
        dock = self.dock
        if dock is None or not dock._valid_event(event):
            return None
        request_id = event["request_id"]
        kind = event["kind"]
        if type(request_id) is not int or type(kind) is not str:
            return None
        pending = self._pending.get(request_id)
        if (
            pending is None
            or pending[3] != lane
            or pending[5] is not True
            or (kind != pending[0] and not (kind == "error" and event["operation"] == pending[2]))
            or (lane == _LANE_NORMAL and pending[2] in _RESTRICTED_OPERATIONS and kind != "error")
        ):
            return None
        if lane == _LANE_NORMAL and dock._hosted_pending(request_id) != (
            pending[0],
            pending[1],
        ):
            return None
        return request_id, pending

    def _retire_pending(
        self,
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        if self._pending.get(request_id) is not pending:
            return False
        self._pending.pop(request_id)
        return True

    def _resume_cleanup_after_refresh_retirement(self, retired: bool) -> bool:
        if retired and self._cleanup_cycle_id is not None:
            self._advance_cleanup()
        return retired

    def _settle_cleanup_refresh_barrier(self) -> None:
        if (
            self._cleanup_cycle_id is None
            or self._refresh_barrier is None
            or any(pending[3] == _LANE_NORMAL for pending in self._pending.values())
        ):
            return
        self._refresh_barrier = None
        self._invalidate_review_tokens()

    def _discard_public_pending(
        self,
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        dock = self.dock
        return (
            dock is not None
            and dock._discard_hosted_pending(
                request_id,
                expected_kind=pending[0],
                context=pending[1],
            )
            and self._retire_pending(request_id, pending)
        )

    def _sticky_open_recovery(
        self,
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        self._open_recovery_required = True
        dock = self.dock
        if dock is not None:
            dock.set_preview_eligibility(
                False,
                recovery_required=True,
            )
        return self._discard_public_pending(request_id, pending)

    def _receive_preview_opened(
        self,
        event: dict[str, object],
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        dock = self.dock
        context = pending[1]
        response = event["response"]
        if (
            dock is None
            or type(context) is not tuple
            or len(context) != 3
            or type(context[0]) is not str
            or type(context[1]) is not dict
            or type(context[2]) is not str
            or type(response) is not dict
            or set(response) != {"source", "open_key", "descriptor", "claim"}
            or response.get("source") != context[1]
            or response.get("open_key") != context[2]
            or type(response.get("descriptor")) is not dict
        ):
            return self._sticky_open_recovery(request_id, pending)
        try:
            coordinator = self._coordinator()
            current = dock.current_preview_open(request_id)
            cleanup_cycle_id = self._cleanup_cycle_id
            cleanup_retirement_pending = False
            if type(cleanup_cycle_id) is int:
                cleanup_retirement_pending = coordinator._draining_cycle_without_bindings(
                    cleanup_cycle_id,
                )
            if self.lifecycle == "stopping" or current != context or cleanup_retirement_pending:
                coordinator.adopt_checkout(
                    response,
                    source=context[1],
                    open_key=context[2],
                )
                if not self._discard_public_pending(request_id, pending):
                    return False
                self._advance_cleanup()
                return True
            binding = coordinator.open(response)
            if self.lifecycle == "stopping" or dock.current_preview_open(request_id) != context:
                checkout_id = dict(binding.descriptor)["checkout_id"]
                coordinator.discard_document(checkout_id)
                if not self._discard_public_pending(request_id, pending):
                    return False
                self._advance_cleanup()
                return True
            applied = dock._apply_hosted_event(
                event,
                expected_kind=pending[0],
                context=context,
            )
            if not applied:
                checkout_id = dict(binding.descriptor)["checkout_id"]
                coordinator.discard_document(checkout_id)
                self._open_recovery_required = True
                dock.set_preview_eligibility(
                    False,
                    recovery_required=True,
                )
                return False
            self._invalidate_review_tokens()
            self._refresh_barrier = None
            return self._retire_pending(request_id, pending)
        except (PreviewError, RuntimeError, TypeError, ValueError) as error:
            if isinstance(error, PreviewError) and error.recovery_required:
                self._open_recovery_required = True
                dock.set_preview_eligibility(
                    False,
                    recovery_required=True,
                )
            else:
                dock.set_preview_eligibility(False)
            consumed = self._discard_public_pending(request_id, pending)
            if consumed:
                self._advance_cleanup()
            return consumed

    def _receive_preview_refreshed(
        self,
        event: dict[str, object],
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        dock = self.dock
        checkout_id = pending[1]
        metadata = pending[6]
        barrier = self._refresh_barrier
        if dock is None:
            return False
        if type(metadata) is not tuple or len(metadata) != 5:
            self._poison_refresh_binding(checkout_id)
            dock.set_preview_eligibility(False)
            retired = self._discard_public_pending(request_id, pending)
            return self._resume_cleanup_after_refresh_retirement(retired)
        generation, cycle_id, binding_identity, selection_stamp, source_kind = metadata
        if self.lifecycle != "active":
            retired = self._discard_public_pending(request_id, pending)
            return self._resume_cleanup_after_refresh_retirement(retired)
        current = (
            barrier is not None
            and generation == barrier.generation
            and cycle_id == barrier.cycle_id
            and selection_stamp == barrier.selection_stamp
            and selection_stamp == self._selection_stamp()
            and (
                source_kind,
                checkout_id,
                binding_identity,
            )
            in barrier.bindings
        )
        if not current:
            retired = self._discard_public_pending(request_id, pending)
            return self._resume_cleanup_after_refresh_retirement(retired)
        try:
            response = event["response"]
            if (
                type(checkout_id) is not str
                or type(source_kind) is not str
                or type(binding_identity) is not int
                or type(response) is not dict
                or response.get("checkout_id") != checkout_id
            ):
                raise PreviewError("invalid refresh response")
            coordinator = self._coordinator()
            binding = coordinator.binding_for_checkout(checkout_id)
            identity = coordinator._binding_identity(checkout_id)
            if (
                identity != (cycle_id, checkout_id, binding_identity)
                or id(binding) != binding_identity
                or not coordinator.review_eligible(binding, response)
            ):
                raise PreviewError("invalid refresh response")
            applied = dock._apply_hosted_event(
                event,
                expected_kind=pending[0],
                context=checkout_id,
            )
            if not applied:
                return False
            if not self._retire_pending(request_id, pending):
                return False
            descriptor = _detach(response)
            assert type(descriptor) is dict
            token = _FreshToken(
                generation=generation,
                cycle_id=cycle_id,
                selection_stamp=selection_stamp,
                source_kind=source_kind,
                checkout_id=checkout_id,
                binding_identity=binding_identity,
                descriptor=descriptor,
            )
            self._refresh_candidates[source_kind] = token
            self._fresh_preview_descriptors[checkout_id] = descriptor
            self._try_mint_review_tokens()
            return self._resume_cleanup_after_refresh_retirement(True)
        except (PreviewError, RuntimeError, TypeError, ValueError) as error:
            if type(checkout_id) is str:
                self._fresh_preview_descriptors.pop(checkout_id, None)
                self._poison_refresh_binding(checkout_id)
            if type(source_kind) is str:
                self._refresh_candidates.pop(source_kind, None)
                self._review_tokens.clear()
            dock.set_preview_eligibility(
                False,
                recovery_required=(isinstance(error, PreviewError) and error.recovery_required),
            )
            replacement = self._preview_error(event, "preview_refresh")
            applied = dock._apply_hosted_event(
                replacement,
                expected_kind=pending[0],
                context=checkout_id,
            )
            retired = applied and self._retire_pending(
                request_id,
                pending,
            )
            if retired:
                self._try_mint_review_tokens()
            return self._resume_cleanup_after_refresh_retirement(retired)

    def _receive_refresh_error(
        self,
        event: dict[str, object],
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        dock = self.dock
        metadata = pending[6]
        barrier = self._refresh_barrier
        if dock is None:
            return False
        if type(metadata) is not tuple or len(metadata) != 5:
            dock.set_preview_eligibility(False)
            retired = self._discard_public_pending(request_id, pending)
            return self._resume_cleanup_after_refresh_retirement(retired)
        generation, cycle_id, _binding_identity, selection_stamp, source_kind = metadata
        if self.lifecycle != "active":
            retired = self._discard_public_pending(request_id, pending)
            return self._resume_cleanup_after_refresh_retirement(retired)
        current = (
            barrier is not None
            and generation == barrier.generation
            and cycle_id == barrier.cycle_id
            and selection_stamp == barrier.selection_stamp
            and selection_stamp == self._selection_stamp()
        )
        if not current:
            retired = self._discard_public_pending(request_id, pending)
            return self._resume_cleanup_after_refresh_retirement(retired)
        if type(source_kind) is str:
            self._refresh_candidates.pop(source_kind, None)
        self._review_tokens.clear()
        if type(pending[1]) is str:
            self._fresh_preview_descriptors.pop(pending[1], None)
        dock.set_preview_eligibility(False)
        applied = dock._apply_hosted_event(
            event,
            expected_kind=pending[0],
            context=pending[1],
        )
        retired = applied and self._retire_pending(
            request_id,
            pending,
        )
        if retired:
            self._try_mint_review_tokens()
        return self._resume_cleanup_after_refresh_retirement(retired)

    def _receive_normal(
        self,
        event: dict[str, object],
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        dock = self.dock
        if dock is None:
            return False
        kind = event["kind"]
        if kind == "preview_opened":
            return self._receive_preview_opened(
                event,
                request_id,
                pending,
            )
        if kind == "preview_refreshed":
            return self._receive_preview_refreshed(
                event,
                request_id,
                pending,
            )
        if kind == "error" and pending[2] == "preview_refresh":
            return self._receive_refresh_error(
                event,
                request_id,
                pending,
            )
        if (
            kind == "error"
            and pending[2] == "preview_open"
            and event["outcome"] == "unknown_outcome"
        ):
            self._open_recovery_required = True
            dock.set_preview_eligibility(
                False,
                recovery_required=True,
            )
        applied = dock._apply_hosted_event(
            event,
            expected_kind=pending[0],
            context=pending[1],
        )
        if not applied:
            return False
        if kind == "connected":
            self.daemon_id = event["daemon_id"]
            self.worker_thread_id = event["worker_thread_id"]
            if self.lifecycle == "starting":
                self.lifecycle = "active"
        retired = self._retire_pending(request_id, pending)
        if retired and self.lifecycle == "stopping":
            self._advance_cleanup()
        return retired

    def _deliver_review_completion(
        self,
        dock: ReviewDock,
        event: dict[str, object],
        context: object,
    ) -> None:
        try:
            dock._receive_host_review_completion(event, context)
        except BaseException:
            self._open_recovery_required = True
            self.lifecycle = "stopping"
            dock.set_preview_eligibility(
                False,
                recovery_required=True,
            )

    def _receive_private(
        self,
        event: dict[str, object],
        request_id: int,
        pending: _PendingRequest,
    ) -> bool:
        operation = pending[2]
        kind = event["kind"]
        dock = self.dock
        if operation == "review":
            if dock is None:
                return False
            ambiguous_id = self._review_enqueue_ambiguous_id
            if ambiguous_id is not None and request_id != ambiguous_id:
                self._open_recovery_required = True
                self.lifecycle = "stopping"
                dock.set_preview_eligibility(
                    False,
                    recovery_required=True,
                )
                return False
            if kind == "review" and not dock._authenticated_ok(event["response"]):
                return False
            if kind == "error" and event["outcome"] == "unknown_outcome":
                if not self._retire_pending(request_id, pending):
                    return False
                self._open_recovery_required = True
                self.lifecycle = "stopping"
                dock.set_preview_eligibility(
                    False,
                    recovery_required=True,
                )
                self._deliver_review_completion(dock, event, pending[1])
                return True
            if ambiguous_id is not None and kind != "review":
                self._open_recovery_required = True
                self.lifecycle = "stopping"
                dock.set_preview_eligibility(
                    False,
                    recovery_required=True,
                )
                if not self._retire_pending(request_id, pending):
                    return False
                self._deliver_review_completion(dock, event, pending[1])
                return True
            if not self._retire_pending(request_id, pending):
                return False
            if ambiguous_id is not None:
                self._review_enqueue_ambiguous_id = None
            self._deliver_review_completion(dock, event, pending[1])
            if ambiguous_id is not None:
                self.close_async()
            return True
        if operation == "preview_close":
            checkout_id = pending[1]
            coordinator = self.preview
            if (
                request_id != self._cleanup_request_id
                or checkout_id != self._cleanup_checkout_id
                or type(checkout_id) is not str
                or coordinator is None
            ):
                return False
            if kind == "preview_closed":
                try:
                    coordinator.mark_checkout_closed(
                        checkout_id,
                        event["response"],
                    )
                except (PreviewError, RuntimeError, TypeError, ValueError):
                    self._open_recovery_required = True
                    if dock is not None:
                        dock.set_preview_eligibility(
                            False,
                            recovery_required=True,
                        )
                    return False
                if not self._retire_pending(request_id, pending):
                    return False
                if dock is None:
                    self._open_recovery_required = True
                    return False
                try:
                    dock._project_host_preview_closed(checkout_id)
                except (ProjectionError, RuntimeError, TypeError, ValueError):
                    self._open_recovery_required = True
                    dock.set_preview_eligibility(
                        False,
                        recovery_required=True,
                    )
                    return False
                self._cleanup_request_id = None
                self._cleanup_checkout_id = None
                self._cleanup_retry_requested = False
                self._cleanup_reconcile_attempted = False
                self._advance_cleanup()
                return True
            if not self._retire_pending(request_id, pending):
                return False
            self._cleanup_request_id = None
            self._cleanup_checkout_id = None
            if self._cleanup_reconcile_attempted:
                self._open_recovery_required = True
                if dock is not None:
                    dock.set_preview_eligibility(
                        False,
                        recovery_required=True,
                    )
                return True
            self._cleanup_reconcile_attempted = True
            self._cleanup_retry_requested = True
            self._advance_cleanup()
            return True
        if operation != "close":
            return False
        if kind == "closed":
            other_pending = any(candidate_id != request_id for candidate_id in self._pending)
            if (
                request_id != self._close_request_id
                or not self._client_close_requested
                or self._open_recovery_required
                or self._review_enqueue_ambiguous_id is not None
                or other_pending
                or not self._zero_control_plane_authority(
                    coordinator_cycle_id=None,
                    allowed_close_request_id=request_id,
                )
                or not self._retire_pending(request_id, pending)
            ):
                self._open_recovery_required = True
                if dock is not None:
                    dock.set_preview_eligibility(
                        False,
                        recovery_required=True,
                    )
                return False
            thread = self.thread
            if thread is not None:
                self._retirement_authorized = True
                thread.quit()
            return True
        if not self._retire_pending(request_id, pending):
            return False
        if kind == "error":
            self._open_recovery_required = True
            if dock is not None:
                dock.set_preview_eligibility(
                    False,
                    recovery_required=True,
                )
        return True

    @QtCore.Slot(object)
    def _receive(self, received: object) -> None:
        self.heartbeat_count += 1
        worker = self.worker
        if worker is not None:
            self.worker_thread_id = worker.thread_id
        represented = self._wire_event(received)
        if represented is None:
            return
        event, lane = represented
        correlated = self._correlated_pending(event, lane)
        if correlated is None:
            return
        request_id, pending = correlated
        if lane == _LANE_PRIVATE:
            self._receive_private(event, request_id, pending)
        else:
            retired = self._receive_normal(event, request_id, pending)
            if retired and self._cleanup_cycle_id is not None:
                self._settle_cleanup_refresh_barrier()
                self._advance_cleanup()

    def close_async(self) -> None:
        if self.lifecycle not in {"starting", "active", "stopping"}:
            return
        if self._thread_retired or self._client_close_requested:
            return
        self.lifecycle = "stopping"
        self._retire_refresh_authority_for_stopping()
        if self._cleanup_request_id is not None:
            self._cleanup_retry_requested = True
        dock = self.dock
        if dock is not None:
            coordinator = self.preview
            if coordinator is not None:
                try:
                    if self._cleanup_cycle_id is None:
                        self._cleanup_cycle_id = coordinator._active_cycle_id()
                    coordinator.close_documents()
                except PreviewError as error:
                    if error.recovery_required:
                        self._open_recovery_required = True
                        dock.set_preview_eligibility(
                            False,
                            recovery_required=True,
                        )
                    else:
                        dock.set_preview_eligibility(False)
                    return
            self._advance_cleanup()

    @QtCore.Slot()
    def _finished(self) -> None:
        global _session
        if self._thread_retired:
            return
        self._thread_retired = True
        self._thread_started = False
        if self.thread is not None:
            _best_effort(self.thread.deleteLater)
        if not self._retirement_authorized:
            self.lifecycle = "stopping"
            self.worker_thread_id = None
            self.daemon_id = None
            if _session is self:
                _remember(self, dock_count=self._dock_count)
            return
        self._detach_dock()
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
    session = _WorkbenchSession(freecad_gui.getMainWindow(), freecad_gui)
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
