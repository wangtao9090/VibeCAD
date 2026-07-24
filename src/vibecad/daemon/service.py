"""Runnable authenticated, single-instance local Task Kernel daemon."""

from __future__ import annotations

import array
import contextlib
import os
import secrets
import signal
import socket
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from vibecad.application.data import (
    ApplicationDataError,
    ApplicationDataErrorCode,
    ApplicationDataLayout,
)
from vibecad.daemon.facade import LocalKernelFacade
from vibecad.daemon.local_identity import LocalIdentityError, require_same_user_peer
from vibecad.daemon.state import (
    DAEMON_AUTHORITY,
    DaemonError,
    DaemonErrorCode,
    DaemonReceipt,
    PublishedDaemonState,
    bind_endpoint,
    cleanup_published_state,
    prepare_run_root,
    publish_boot_state,
    recover_stale_state,
    require_published_state,
)
from vibecad.interaction.protocol_v2 import (
    MAX_V2_CONNECTIONS,
    MAX_V2_FRAME_PAYLOAD_BYTES,
    V2_FRAME_HEADER_BYTES,
    V2_HANDSHAKE_TIMEOUT_SECONDS,
    V2_IDLE_TIMEOUT_SECONDS,
    StaticV2Dispatcher,
    V2ErrorCode,
    V2ProtocolError,
    V2Request,
    V2ServerConnection,
    encode_v2_frame,
)
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ResourceLease,
    ResourceLeaseManager,
)

_MAX_ANCILLARY_DESCRIPTORS = 8
_ACCEPT_POLL_SECONDS = 0.2
_SHUTDOWN_TIMEOUT_SECONDS = 5.0
_STARTUP_MAINTENANCE_TIMEOUT_SECONDS = 15.0
_PUBLIC_DISPATCH_FAILURES = frozenset(
    {
        V2ErrorCode.UNKNOWN_METHOD,
        V2ErrorCode.INVALID_REQUEST,
        V2ErrorCode.RESOURCE_EXHAUSTED,
        V2ErrorCode.UNAVAILABLE,
        V2ErrorCode.INTERNAL_ERROR,
    }
)

_ApplicationFactory = Callable[..., object]


class LocalKernelState(StrEnum):
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"
    CLOSED = "closed"


class _ConnectionClosed(Exception):
    pass


def _close_descriptors(descriptors: list[int] | tuple[int, ...]) -> None:
    for descriptor in descriptors:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _received_descriptors(
    ancillary: list[tuple[int, int, bytes]],
    flags: int,
) -> list[int]:
    descriptors: list[int] = []
    malformed = False
    try:
        for level, kind, raw in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                malformed = True
                continue
            values = array.array("i")
            usable = len(raw) - (len(raw) % values.itemsize)
            if not raw or usable != len(raw):
                malformed = True
            if usable:
                values.frombytes(raw[:usable])
            descriptors.extend(values)
            if len(descriptors) > _MAX_ANCILLARY_DESCRIPTORS:
                raise V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
        for descriptor in descriptors:
            os.set_inheritable(descriptor, False)
        if malformed or flags & getattr(socket, "MSG_CTRUNC", 0):
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


class _FrameReader:
    __slots__ = ()

    @staticmethod
    def _part(
        connection: socket.socket,
        size: int,
        *,
        deadline: float,
    ) -> tuple[bytes, list[int]]:
        result = bytearray()
        descriptors: list[int] = []
        ancillary_size = socket.CMSG_SPACE(_MAX_ANCILLARY_DESCRIPTORS * array.array("i").itemsize)
        try:
            while len(result) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
                try:
                    fragment, ancillary, flags, _address = connection.recvmsg(
                        size - len(result),
                        ancillary_size,
                    )
                except TimeoutError:
                    raise TimeoutError from None
                descriptors.extend(_received_descriptors(ancillary, flags))
                if len(descriptors) > _MAX_ANCILLARY_DESCRIPTORS:
                    raise V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
                if not fragment:
                    raise _ConnectionClosed
                result.extend(fragment)
            return bytes(result), descriptors
        except BaseException:
            _close_descriptors(descriptors)
            raise

    def receive(
        self,
        connection: socket.socket,
        *,
        deadline: float,
        allow_descriptor: bool = False,
    ) -> tuple[bytes, tuple[int, ...]]:
        header, header_descriptors = self._part(
            connection,
            V2_FRAME_HEADER_BYTES,
            deadline=deadline,
        )
        declared = int.from_bytes(header, "big")
        if declared == 0:
            _close_descriptors(header_descriptors)
            raise V2ProtocolError(V2ErrorCode.MALFORMED_FRAME)
        if declared > MAX_V2_FRAME_PAYLOAD_BYTES:
            _close_descriptors(header_descriptors)
            raise V2ProtocolError(V2ErrorCode.FRAME_TOO_LARGE)
        try:
            payload, payload_descriptors = self._part(
                connection,
                declared,
                deadline=deadline,
            )
        except BaseException:
            _close_descriptors(header_descriptors)
            raise
        descriptors = header_descriptors + payload_descriptors
        if not allow_descriptor and descriptors:
            _close_descriptors(descriptors)
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return payload, tuple(descriptors)


def _send(connection: socket.socket, payload: bytes, *, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection.settimeout(remaining)
    connection.sendall(encode_v2_frame(payload))


def _default_application_factory(
    *,
    layout: ApplicationDataLayout,
    lease_manager: ResourceLeaseManager,
) -> object:
    from vibecad.application.agent import AgentApplication

    return AgentApplication.from_captured_layout(
        layout=layout,
        lease_manager=lease_manager,
    )


def _close_application(application: object) -> bool:
    try:
        result = application.close()
    except BaseException:
        return False
    return result is None or result is True


def _close_facade(facade: LocalKernelFacade) -> bool:
    try:
        facade.close()
    except BaseException:
        return False
    return True


def _daemon_error(error: BaseException) -> DaemonError:
    if type(error) is DaemonError:
        return error
    if type(error) is LeaseError:
        if error.code is LeaseErrorCode.CONTENDED:
            return DaemonError(DaemonErrorCode.CONTENDED)
        if error.code is LeaseErrorCode.WRONG_PROCESS:
            return DaemonError(DaemonErrorCode.WRONG_PROCESS)
        if error.code is LeaseErrorCode.UNSUPPORTED_PLATFORM:
            return DaemonError(DaemonErrorCode.UNSUPPORTED_PLATFORM)
        if error.code in {
            LeaseErrorCode.UNSAFE_ROOT,
            LeaseErrorCode.UNSAFE_LOCK_ENTRY,
            LeaseErrorCode.INVALID_LEASE,
            LeaseErrorCode.ALREADY_RELEASED,
        }:
            return DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
        return DaemonError(DaemonErrorCode.IO_ERROR)
    if type(error) is ApplicationDataError:
        if error.code is ApplicationDataErrorCode.UNSUPPORTED_PLATFORM:
            return DaemonError(DaemonErrorCode.UNSUPPORTED_PLATFORM)
        if error.code in {
            ApplicationDataErrorCode.INVALID_ROOT,
            ApplicationDataErrorCode.UNSAFE_ROOT,
        }:
            return DaemonError(DaemonErrorCode.UNSAFE_ROOT)
        return DaemonError(DaemonErrorCode.IO_ERROR)
    return DaemonError(DaemonErrorCode.IO_ERROR)


def _dispatch_request(
    protocol: V2ServerConnection,
    payload: bytes,
    dispatcher: StaticV2Dispatcher,
    revalidate: Callable[[], None],
    descriptors: tuple[int, ...] = (),
) -> tuple[bytes, bool]:
    request: V2Request = protocol.admit_request(payload)
    revalidate()
    try:
        if len(descriptors) > 1:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        result = dispatcher.dispatch(
            request,
            descriptor=None if not descriptors else descriptors[0],
        )
    except V2ProtocolError as error:
        code = error.code if error.code in _PUBLIC_DISPATCH_FAILURES else V2ErrorCode.INTERNAL_ERROR
        return protocol.encode_failure(request, code), False
    except Exception:
        return protocol.encode_failure(request, V2ErrorCode.INTERNAL_ERROR), False
    try:
        return (
            protocol.encode_success(request, result),
            request.method == "kernel.retire",
        )
    except V2ProtocolError:
        return protocol.encode_failure(request, V2ErrorCode.INTERNAL_ERROR), False


class LocalKernelDaemon:
    """One process-owned listener, Application and authority lease."""

    __slots__ = (
        "_accept_thread",
        "_active_dispatches",
        "_admission_condition",
        "_application",
        "_authority",
        "_close_lock",
        "_connections",
        "_connections_lock",
        "_creator_pid",
        "_facade",
        "_fatal_error",
        "_layout",
        "_lease_manager",
        "_listener",
        "_published",
        "_retire_requested",
        "_state",
        "_state_lock",
        "_stop",
    )

    def __init__(
        self,
        *,
        layout: ApplicationDataLayout,
        lease_manager: ResourceLeaseManager,
        authority: ResourceLease,
        application: object,
        listener: socket.socket,
        published: PublishedDaemonState,
        facade: LocalKernelFacade,
        stop: threading.Event,
    ) -> None:
        self._layout = layout
        self._lease_manager = lease_manager
        self._authority = authority
        self._application = application
        self._listener = listener
        self._published = published
        self._facade = facade
        self._creator_pid = os.getpid()
        self._stop = stop
        self._retire_requested = threading.Event()
        self._admission_condition = threading.Condition()
        self._active_dispatches = 0
        self._state_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._connections_lock = threading.Lock()
        self._connections: dict[socket.socket, threading.Thread] = {}
        self._fatal_error: DaemonError | None = None
        self._state = LocalKernelState.RUNNING
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="vibecad-kernel-accept",
            daemon=True,
        )

    @classmethod
    def start(
        cls,
        *,
        data_root: object,
        application_factory: _ApplicationFactory = _default_application_factory,
        before_accept: Callable[[], None] | None = None,
    ) -> LocalKernelDaemon:
        if not callable(application_factory) or (
            before_accept is not None and not callable(before_accept)
        ):
            raise TypeError("application_factory must be callable")
        layout = None
        lease_manager = None
        authority = None
        run_root = None
        application = None
        listener = None
        published = None
        facade = None
        stop = threading.Event()
        instance = None
        try:
            layout = ApplicationDataLayout.open(data_root)
            lease_manager = ResourceLeaseManager(
                layout.locks,
                trust=LeaseRootTrust.TRUSTED_LOCAL,
            )
            if getattr(lease_manager, "_root_identity", None) != layout.identity_for(layout.locks):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            authority = lease_manager.acquire(DAEMON_AUTHORITY)
            authority.require_current()
            run_root = prepare_run_root(layout)
            recover_stale_state(run_root, layout=layout)
            authority.require_current()
            layout.require_current(layout.root)
            layout.require_current(layout.locks)
            application = application_factory(
                layout=layout,
                lease_manager=lease_manager,
            )
            if application is None or not callable(getattr(application, "close", None)):
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            authority.require_current()
            layout.require_current(layout.root)
            layout.require_current(layout.locks)
            listener, endpoint = bind_endpoint(run_root)
            authority.require_current()
            published = publish_boot_state(
                root=run_root,
                layout=layout,
                daemon_id="daemon_" + secrets.token_hex(16),
                started_ns=time.time_ns(),
                endpoint=endpoint,
            )
            authority.require_current()
            require_published_state(published, layout=layout)
            facade = LocalKernelFacade(
                application,
                daemon_id=published.receipt.daemon_id,
            )
            instance = cls(
                layout=layout,
                lease_manager=lease_manager,
                authority=authority,
                application=application,
                listener=listener,
                published=published,
                facade=facade,
                stop=stop,
            )
            instance._require_live_bindings()
            if before_accept is not None:
                before_accept()
            instance._accept_thread.start()
            return instance
        except BaseException as error:
            primary = _daemon_error(error)
            if instance is not None:
                instance._stop.set()
            if listener is not None:
                with contextlib.suppress(OSError):
                    listener.close()
            if facade is not None:
                _close_facade(facade)
            if application is not None:
                _close_application(application)
            if published is not None:
                with contextlib.suppress(DaemonError):
                    cleanup_published_state(published)
            elif run_root is not None and layout is not None:
                with contextlib.suppress(DaemonError):
                    recover_stale_state(run_root, layout=layout)
            if authority is not None and authority.released is False:
                with contextlib.suppress(LeaseError):
                    authority.release(owner_token=authority.owner_token)
            raise primary from None

    @property
    def state(self) -> LocalKernelState:
        with self._state_lock:
            return self._state

    @property
    def daemon_id(self) -> str:
        return self._published.receipt.daemon_id

    @property
    def receipt(self) -> DaemonReceipt:
        return self._published.receipt

    @property
    def run_root(self) -> Path:
        return self._published.root.path

    @property
    def active_connections(self) -> int:
        with self._connections_lock:
            return len(self._connections)

    def _ensure_process(self) -> None:
        if os.getpid() != self._creator_pid:
            raise DaemonError(DaemonErrorCode.WRONG_PROCESS)

    def _require_live_bindings(self) -> None:
        self._ensure_process()
        self._authority.require_current()
        require_published_state(self._published, layout=self._layout)
        with self._state_lock:
            if self._state is not LocalKernelState.RUNNING:
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    def _mark_failed(self, error: BaseException) -> None:
        failure = _daemon_error(error)
        with self._state_lock:
            if self._state is LocalKernelState.CLOSED:
                return
            self._fatal_error = failure
            self._state = LocalKernelState.FAILED
        self._request_stop()
        with contextlib.suppress(OSError):
            self._listener.close()
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)

    def _request_stop(self) -> None:
        with self._admission_condition:
            self._stop.set()
            self._admission_condition.notify_all()

    def _request_retire(self) -> None:
        self._retire_requested.set()
        self._request_stop()

    def _begin_dispatch(self) -> bool:
        with self._admission_condition:
            if self._stop.is_set():
                return False
            self._active_dispatches += 1
            return True

    def _finish_dispatch(self) -> None:
        failed = False
        with self._admission_condition:
            if self._active_dispatches <= 0:
                failed = True
            else:
                self._active_dispatches -= 1
                self._admission_condition.notify_all()
        if failed:
            self._mark_failed(DaemonError(DaemonErrorCode.RECOVERY_REQUIRED))

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            accepted = None
            try:
                self._require_live_bindings()
                try:
                    accepted, _address = self._listener.accept()
                except TimeoutError:
                    continue
                if self._stop.is_set():
                    accepted.close()
                    accepted = None
                    break
                self._require_live_bindings()
                require_same_user_peer(accepted)
                self._require_live_bindings()
                with self._connections_lock:
                    if len(self._connections) >= MAX_V2_CONNECTIONS:
                        accepted.close()
                        accepted = None
                        continue
                    worker = threading.Thread(
                        target=self._serve_connection,
                        args=(accepted, time.monotonic()),
                        name="vibecad-kernel-connection",
                        daemon=True,
                    )
                    self._connections[accepted] = worker
                    try:
                        worker.start()
                    except BaseException:
                        self._connections.pop(accepted, None)
                        raise
                accepted = None
            except LocalIdentityError:
                if accepted is not None:
                    with contextlib.suppress(OSError):
                        accepted.close()
            except (DaemonError, LeaseError, ApplicationDataError) as error:
                if not self._stop.is_set():
                    self._mark_failed(error)
                break
            except OSError as error:
                if not self._stop.is_set():
                    self._mark_failed(error)
                break
            except BaseException as error:
                if not self._stop.is_set():
                    self._mark_failed(error)
                break
            finally:
                if accepted is not None:
                    with contextlib.suppress(OSError):
                        accepted.close()

    def _serve_connection(self, connection: socket.socket, accepted_at: float) -> None:
        protocol = None
        session_id = None
        session_cleanup_error = None
        try:
            self._require_live_bindings()
            protocol = V2ServerConnection(
                self._published.secret,
                daemon_id=self.daemon_id,
            )
            deadline = accepted_at + V2_HANDSHAKE_TIMEOUT_SECONDS
            _send(connection, protocol.start(), deadline=deadline)
            authentication, _ = _FrameReader().receive(connection, deadline=deadline)
            self._require_live_bindings()
            authenticated = protocol.accept_auth(authentication)
            session_id = protocol.session_id
            dispatcher = self._facade.open_session(session_id)
            _send(connection, authenticated, deadline=deadline)
            reader = _FrameReader()
            while not self._stop.is_set():
                deadline = time.monotonic() + V2_IDLE_TIMEOUT_SECONDS
                payload, descriptors = reader.receive(
                    connection,
                    deadline=deadline,
                    allow_descriptor=True,
                )
                if not self._begin_dispatch():
                    _close_descriptors(descriptors)
                    break
                try:
                    self._require_live_bindings()
                    response, retire_after_send = _dispatch_request(
                        protocol,
                        payload,
                        dispatcher,
                        self._require_live_bindings,
                        descriptors,
                    )
                    self._require_live_bindings()
                    _send(
                        connection,
                        response,
                        deadline=time.monotonic() + V2_IDLE_TIMEOUT_SECONDS,
                    )
                    if retire_after_send:
                        self._request_retire()
                finally:
                    _close_descriptors(descriptors)
                    self._finish_dispatch()
        except DaemonError as error:
            if not self._stop.is_set():
                self._mark_failed(error)
        except (
            LocalIdentityError,
            OSError,
            TimeoutError,
            V2ProtocolError,
            _ConnectionClosed,
        ):
            pass
        except BaseException as error:
            if not self._stop.is_set():
                self._mark_failed(error)
        finally:
            if session_id is not None:
                try:
                    self._facade.close_session(session_id)
                except BaseException as error:
                    session_cleanup_error = error
            if protocol is not None:
                protocol.close()
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                connection.close()
            with self._connections_lock:
                self._connections.pop(connection, None)
            if session_cleanup_error is not None and not self._stop.is_set():
                self._mark_failed(session_cleanup_error)

    def wait(self, timeout: float | None = None) -> bool:
        self._ensure_process()
        return self._stop.wait(timeout)

    def close(self) -> None:
        self._ensure_process()
        with self._close_lock:
            with self._state_lock:
                if self._state is LocalKernelState.CLOSED:
                    return
            deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
            self._request_stop()
            with contextlib.suppress(OSError):
                self._listener.close()
            with self._admission_condition:
                if self._retire_requested.is_set():
                    while self._active_dispatches:
                        self._admission_condition.wait()
                    # The bounded cleanup window starts only after every
                    # admitted response has been sent and accounted for.
                    deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
                else:
                    while self._active_dispatches:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._admission_condition.wait(remaining)
                dispatches_drained = self._active_dispatches == 0
            with self._connections_lock:
                connections = tuple(self._connections)
                workers = tuple(self._connections.values())
            for connection in connections:
                with contextlib.suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
            if not dispatches_drained:
                with self._state_lock:
                    self._state = LocalKernelState.FAILED
                    self._fatal_error = DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            with self._state_lock:
                self._state = LocalKernelState.STOPPING
            if (
                self._accept_thread is not threading.current_thread()
                and self._accept_thread.is_alive()
            ):
                self._accept_thread.join(max(0.0, deadline - time.monotonic()))
            for worker in workers:
                if worker is threading.current_thread() or not worker.is_alive():
                    continue
                worker.join(max(0.0, deadline - time.monotonic()))
            with self._connections_lock:
                alive = tuple(worker for worker in self._connections.values() if worker.is_alive())
            if self._accept_thread.is_alive() or alive:
                with self._state_lock:
                    self._state = LocalKernelState.FAILED
                    self._fatal_error = DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            if not _close_facade(self._facade):
                with self._state_lock:
                    self._state = LocalKernelState.FAILED
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            if not _close_application(self._application):
                with self._state_lock:
                    self._state = LocalKernelState.FAILED
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED)
            try:
                self._authority.require_current()
                require_published_state(self._published, layout=self._layout)
                cleanup_published_state(self._published)
                self._authority.require_current()
                self._authority.release(owner_token=self._authority.owner_token)
            except (DaemonError, LeaseError, ApplicationDataError):
                with self._state_lock:
                    self._state = LocalKernelState.FAILED
                raise DaemonError(DaemonErrorCode.RECOVERY_REQUIRED) from None
            with self._state_lock:
                self._state = LocalKernelState.CLOSED


def run_daemon() -> int:
    """Run the fixed production daemon until SIGINT/SIGTERM."""

    from vibecad.runtime import paths, status
    from vibecad.runtime.uninstall import uninstall_marker

    daemon = None
    startup_guard = None
    startup_guard_released = False
    stop = threading.Event()
    previous: dict[int, object] = {}

    def request_stop(_signum, _frame) -> None:
        stop.set()

    def release_startup_guard() -> None:
        nonlocal startup_guard_released
        if startup_guard is None or startup_guard_released:
            return
        startup_guard_released = True
        startup_guard.__exit__(None, None, None)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, request_stop)
        inherited = os.environ.pop(
            status.RUNTIME_MAINTENANCE_CLAIM_FD_ENV,
            None,
        )
        if inherited is None:
            startup_guard = status.runtime_maintenance_lock(
                timeout=_STARTUP_MAINTENANCE_TIMEOUT_SECONDS,
            )
        else:
            try:
                inherited_descriptor = int(inherited, 10)
            except (TypeError, ValueError):
                return 1
            if str(inherited_descriptor) != inherited:
                return 1
            startup_guard = status.inherited_runtime_maintenance_claim(
                inherited_descriptor,
            )
        startup_guard.__enter__()
        if os.path.lexists(uninstall_marker()):
            return 1
        daemon = LocalKernelDaemon.start(
            data_root=paths.data_root(),
            before_accept=release_startup_guard,
        )
        while not stop.wait(_ACCEPT_POLL_SECONDS):
            if daemon.state is LocalKernelState.FAILED:
                return 1
            if daemon.wait(0):
                break
        if daemon.state is LocalKernelState.FAILED:
            return 1
        daemon.close()
        return 0
    except (DaemonError, OSError, RuntimeError):
        return 1
    finally:
        if not startup_guard_released:
            with contextlib.suppress(RuntimeError):
                release_startup_guard()
        if daemon is not None and daemon.state is not LocalKernelState.CLOSED:
            with contextlib.suppress(DaemonError):
                daemon.close()
        for signum, handler in previous.items():
            with contextlib.suppress(ValueError):
                signal.signal(signum, handler)


__all__ = ("LocalKernelDaemon", "LocalKernelState", "run_daemon")
