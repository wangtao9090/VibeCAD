"""Low-level authenticated client for the local Task Kernel."""

from __future__ import annotations

import collections
import os
import secrets
import socket
import threading
import time

from vibecad.daemon.local_identity import LocalIdentityError, require_same_user_peer
from vibecad.daemon.state import (
    DAEMON_ENDPOINT_NAME,
    DaemonError,
    DaemonErrorCode,
    PublishedDaemonState,
    read_boot_state,
)
from vibecad.interaction.protocol_v2 import (
    V2_HANDSHAKE_TIMEOUT_SECONDS,
    V2_IDLE_TIMEOUT_SECONDS,
    V2ClientConnection,
    V2FrameDecoder,
    V2ProtocolError,
    V2Response,
    encode_v2_frame,
)

_READ_CHUNK_BYTES = 65_536


class _ConnectionClosed(Exception):
    pass


class _FrameReader:
    __slots__ = ("_decoder", "_pending")

    def __init__(self) -> None:
        self._decoder = V2FrameDecoder()
        self._pending: collections.deque[bytes] = collections.deque()

    def receive(
        self,
        connection: socket.socket,
        *,
        deadline: float | None,
        fragment_idle_seconds: float | None = None,
    ) -> bytes:
        if self._pending:
            return self._pending.popleft()
        while True:
            if deadline is None:
                connection.settimeout(None)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.settimeout(remaining)
            try:
                fragment = connection.recv(_READ_CHUNK_BYTES)
            except TimeoutError:
                raise TimeoutError from None
            if not fragment:
                try:
                    self._decoder.finish()
                except V2ProtocolError:
                    raise
                raise _ConnectionClosed
            if fragment_idle_seconds is not None:
                deadline = time.monotonic() + fragment_idle_seconds
            frames = self._decoder.feed(fragment)
            self._pending.extend(frames)
            if self._pending:
                return self._pending.popleft()


def _send(connection: socket.socket, payload: bytes, *, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection.settimeout(remaining)
    connection.sendall(encode_v2_frame(payload))


def _same_boot_state(left: PublishedDaemonState, right: PublishedDaemonState) -> bool:
    return (
        left.receipt == right.receipt
        and left.receipt_raw == right.receipt_raw
        and left.receipt_binding == right.receipt_binding
        and left.secret == right.secret
        and left.root.identity == right.root.identity
        and left.root.path == right.root.path
    )


class LocalKernelClient:
    """One PID-bound authenticated connection; product adapters arrive in C13."""

    __slots__ = (
        "_boot_state",
        "_connection",
        "_creator_pid",
        "_lock",
        "_protocol",
        "_reader",
        "_closed",
    )

    def __init__(
        self,
        *,
        boot_state: PublishedDaemonState,
        connection: socket.socket,
        protocol: V2ClientConnection,
        reader: _FrameReader,
    ) -> None:
        self._boot_state = boot_state
        self._connection = connection
        self._protocol = protocol
        self._reader = reader
        self._creator_pid = os.getpid()
        self._lock = threading.Lock()
        self._closed = False

    @property
    def daemon_id(self) -> str:
        return self._boot_state.receipt.daemon_id

    @classmethod
    def connect(cls, run_root: object) -> LocalKernelClient:
        try:
            boot_state = read_boot_state(run_root)
        except DaemonError:
            raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED) from None
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        protocol = None
        connected = False
        reader = _FrameReader()
        try:
            connection.set_inheritable(False)
            deadline = time.monotonic() + V2_HANDSHAKE_TIMEOUT_SECONDS
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(remaining)
            connection.connect(str(boot_state.root.path / DAEMON_ENDPOINT_NAME))
            require_same_user_peer(connection)
            current = read_boot_state(boot_state.root.path)
            if not _same_boot_state(boot_state, current):
                raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED)
            protocol = V2ClientConnection(
                boot_state.secret,
                expected_daemon_id=boot_state.receipt.daemon_id,
            )
            challenge = reader.receive(connection, deadline=deadline)
            authentication = protocol.answer_challenge(challenge)
            _send(connection, authentication, deadline=deadline)
            authenticated = reader.receive(connection, deadline=deadline)
            protocol.accept_authenticated(authenticated)
            final = read_boot_state(boot_state.root.path)
            if not _same_boot_state(boot_state, final):
                raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED)
            client = cls(
                boot_state=boot_state,
                connection=connection,
                protocol=protocol,
                reader=reader,
            )
            connected = True
            return client
        except DaemonError:
            raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED) from None
        except (
            LocalIdentityError,
            OSError,
            TimeoutError,
            V2ProtocolError,
            _ConnectionClosed,
        ):
            raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED) from None
        finally:
            if not connected:
                try:
                    connection.close()
                except OSError:
                    pass

    def _ensure_live(self) -> None:
        if self._closed:
            raise DaemonError(DaemonErrorCode.INVALID_STATE)
        if os.getpid() != self._creator_pid:
            raise DaemonError(DaemonErrorCode.WRONG_PROCESS)

    def call(
        self,
        method: object,
        params: object,
        *,
        request_id: object | None = None,
    ) -> V2Response:
        self._ensure_live()
        with self._lock:
            self._ensure_live()
            if request_id is None:
                request_id = "request_" + secrets.token_hex(16)
            try:
                current = read_boot_state(self._boot_state.root.path)
                if not _same_boot_state(self._boot_state, current):
                    raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED)
                request = self._protocol.encode_request(
                    method,
                    params,
                    request_id=request_id,
                )
                _send(
                    self._connection,
                    request,
                    deadline=time.monotonic() + V2_IDLE_TIMEOUT_SECONDS,
                )
                response = self._reader.receive(
                    self._connection,
                    deadline=None,
                    fragment_idle_seconds=V2_IDLE_TIMEOUT_SECONDS,
                )
                return self._protocol.decode_response(response)
            except DaemonError:
                self.close()
                raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED) from None
            except (
                OSError,
                TimeoutError,
                V2ProtocolError,
                _ConnectionClosed,
            ):
                self.close()
                raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None

    def close(self) -> None:
        if os.getpid() != self._creator_pid:
            raise DaemonError(DaemonErrorCode.WRONG_PROCESS)
        if self._closed:
            return
        self._closed = True
        self._protocol.close()
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass

    def __enter__(self) -> LocalKernelClient:
        self._ensure_live()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


__all__ = ("LocalKernelClient",)
