"""Low-level authenticated client for the local Task Kernel."""

from __future__ import annotations

import array
import contextlib
import hashlib
import os
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

from vibecad._file_compat import (
    WindowsExternalFileCapability,
    WindowsPathCapability,
    capture_windows_external_fd,
    capture_windows_fd,
    delete_windows_directory,
    delete_windows_directory_capability,
    delete_windows_file,
    ensure_private_directory,
    open_private_file,
    open_windows_directory_fd,
    open_windows_external_file,
    validate_windows_external_file,
    validate_windows_path,
    windows_extended_path,
)
from vibecad._file_compat import (
    pread as portable_pread,
)
from vibecad.application.visual_ingress import (
    VisualIngressError,
    bind_visual_staging_locator,
    parse_seal_image_set_request,
    validate_seal_result,
)
from vibecad.daemon.local_identity import LocalIdentityError, require_same_user_peer
from vibecad.daemon.state import (
    DAEMON_ENDPOINT_NAME,
    DaemonError,
    DaemonErrorCode,
    PublishedDaemonState,
    read_boot_state,
)
from vibecad.daemon.windows_ipc import (
    HANDLE_ENVELOPE_BYTES,
    WindowsNamedPipeConnection,
    connect_named_pipe,
    encode_handle_envelope,
    named_pipe_name,
    require_expected_peer,
)
from vibecad.interaction.protocol_v2 import (
    MAX_V2_FRAME_PAYLOAD_BYTES,
    V2_FRAME_HEADER_BYTES,
    V2_HANDSHAKE_TIMEOUT_SECONDS,
    V2_IDLE_TIMEOUT_SECONDS,
    V2ClientConnection,
    V2ErrorCode,
    V2ProtocolError,
    V2Response,
    bind_v2_import_locator,
    encode_v2_frame,
)
from vibecad.visual.contracts import MAX_IMAGE_SET_SOURCE_BYTES, MAX_IMAGE_SOURCE_BYTES

_MAX_IMPORT_BYTES = 512 * 1024 * 1024
_MAX_ANCILLARY_DESCRIPTORS = 8
_CMSG_SPACE = getattr(socket, "CMSG_SPACE", None)
_MSG_CTRUNC = getattr(socket, "MSG_CTRUNC", 0)
_SCM_RIGHTS = getattr(socket, "SCM_RIGHTS", None)
_SOL_SOCKET = socket.SOL_SOCKET


class _ConnectionClosed(Exception):
    pass


class LocalImportSourceError(DaemonError):
    """The local import descriptor was rejected before/after a known response."""

    def __init__(self) -> None:
        super().__init__(DaemonErrorCode.UNAVAILABLE)


class LocalVisualSourceError(DaemonError):
    """The local visual ingress was rejected without exposing source metadata."""

    def __init__(self) -> None:
        super().__init__(DaemonErrorCode.UNAVAILABLE)


def _close_descriptors(descriptors: list[int]) -> None:
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
            if level != _SOL_SOCKET or kind != _SCM_RIGHTS:
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
        if malformed or flags & _MSG_CTRUNC:
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
        return descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


class _FrameReader:
    __slots__ = ()

    @staticmethod
    def _part(
        connection: socket.socket | WindowsNamedPipeConnection,
        size: int,
        *,
        deadline: float | None,
        fragment_idle_seconds: float | None,
    ) -> bytes:
        result = bytearray()
        if isinstance(connection, WindowsNamedPipeConnection):
            while len(result) < size:
                if deadline is None:
                    connection.settimeout(None)
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    connection.settimeout(remaining)
                fragment = connection.recv(size - len(result))
                if not fragment:
                    raise _ConnectionClosed
                result.extend(fragment)
                if fragment_idle_seconds is not None:
                    deadline = time.monotonic() + fragment_idle_seconds
            return bytes(result)
        if _CMSG_SPACE is None or not hasattr(connection, "recvmsg"):
            raise DaemonError(DaemonErrorCode.UNSUPPORTED_PLATFORM)
        ancillary_size = _CMSG_SPACE(_MAX_ANCILLARY_DESCRIPTORS * array.array("i").itemsize)
        while len(result) < size:
            if deadline is None:
                connection.settimeout(None)
            else:
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
            descriptors = _received_descriptors(ancillary, flags)
            if descriptors:
                _close_descriptors(descriptors)
                raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
            if not fragment:
                raise _ConnectionClosed
            result.extend(fragment)
            if fragment_idle_seconds is not None:
                deadline = time.monotonic() + fragment_idle_seconds
        return bytes(result)

    def receive(
        self,
        connection: socket.socket | WindowsNamedPipeConnection,
        *,
        deadline: float | None,
        fragment_idle_seconds: float | None = None,
    ) -> bytes:
        header = self._part(
            connection,
            V2_FRAME_HEADER_BYTES,
            deadline=deadline,
            fragment_idle_seconds=fragment_idle_seconds,
        )
        declared = int.from_bytes(header, "big")
        if declared == 0:
            raise V2ProtocolError(V2ErrorCode.MALFORMED_FRAME)
        if declared > MAX_V2_FRAME_PAYLOAD_BYTES:
            raise V2ProtocolError(V2ErrorCode.FRAME_TOO_LARGE)
        payload_deadline = deadline
        if fragment_idle_seconds is not None:
            payload_deadline = time.monotonic() + fragment_idle_seconds
        return self._part(
            connection,
            declared,
            deadline=payload_deadline,
            fragment_idle_seconds=fragment_idle_seconds,
        )


def _send(
    connection: socket.socket | WindowsNamedPipeConnection,
    payload: bytes,
    *,
    deadline: float,
) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection.settimeout(remaining)
    connection.sendall(encode_v2_frame(payload))


def _send_descriptor(
    connection: socket.socket | WindowsNamedPipeConnection,
    payload: bytes,
    descriptor: int,
    *,
    deadline: float,
) -> None:
    if isinstance(connection, WindowsNamedPipeConnection):
        wrapped = encode_handle_envelope(payload, descriptor)
        if len(wrapped) > MAX_V2_FRAME_PAYLOAD_BYTES + HANDLE_ENVELOPE_BYTES:
            raise V2ProtocolError(V2ErrorCode.FRAME_TOO_LARGE)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection.settimeout(remaining)
        connection.sendall(len(wrapped).to_bytes(V2_FRAME_HEADER_BYTES, "big") + wrapped)
        return
    if sys.platform != "darwin" or _SCM_RIGHTS is None:
        raise DaemonError(DaemonErrorCode.UNSUPPORTED_PLATFORM)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    connection.settimeout(remaining)
    frame = encode_v2_frame(payload)
    rights = array.array("i", [descriptor])
    sent = connection.sendmsg(
        [frame],
        [(_SOL_SOCKET, _SCM_RIGHTS, rights)],
    )
    if sent <= 0 or sent > len(frame):
        raise OSError
    if sent < len(frame):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection.settimeout(remaining)
        connection.sendall(frame[sent:])


class _PinnedImportSource:
    __slots__ = (
        "ancestor_fds",
        "ancestor_identities",
        "ancestor_names",
        "before",
        "fd",
        "final_name",
        "managed_identity",
    )

    def __init__(
        self,
        *,
        ancestor_fds: tuple[int, ...],
        ancestor_identities: tuple[tuple[int, ...], ...],
        ancestor_names: tuple[str, ...],
        final_name: str,
        fd: int,
        before: os.stat_result,
        managed_identity: tuple[int, int],
    ) -> None:
        self.ancestor_fds = ancestor_fds
        self.ancestor_identities = ancestor_identities
        self.ancestor_names = ancestor_names
        self.final_name = final_name
        self.fd = fd
        self.before = before
        self.managed_identity = managed_identity

    @staticmethod
    def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_gid,
        )

    @staticmethod
    def _file_identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_uid,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    @staticmethod
    def _safe_file(value: os.stat_result) -> bool:
        return (
            stat.S_ISREG(value.st_mode)
            and value.st_uid == os.geteuid()
            and value.st_nlink == 1
            and 0 < value.st_size <= _MAX_IMPORT_BYTES
        )

    def verify(self) -> None:
        if not (
            len(self.ancestor_fds) == len(self.ancestor_identities)
            and len(self.ancestor_names) + 1 == len(self.ancestor_fds)
        ):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        try:
            for descriptor, expected in zip(
                self.ancestor_fds,
                self.ancestor_identities,
                strict=True,
            ):
                current = os.fstat(descriptor)
                if (
                    self._directory_identity(current) != expected
                    or (current.st_dev, current.st_ino) == self.managed_identity
                ):
                    raise DaemonError(DaemonErrorCode.UNAVAILABLE)
            for index, name in enumerate(self.ancestor_names, start=1):
                entry = os.stat(
                    name,
                    dir_fd=self.ancestor_fds[index - 1],
                    follow_symlinks=False,
                )
                child = os.fstat(self.ancestor_fds[index])
                if not stat.S_ISDIR(entry.st_mode) or (
                    entry.st_dev,
                    entry.st_ino,
                ) != (child.st_dev, child.st_ino):
                    raise DaemonError(DaemonErrorCode.UNAVAILABLE)
            entry = os.stat(
                self.final_name,
                dir_fd=self.ancestor_fds[-1],
                follow_symlinks=False,
            )
            current = os.fstat(self.fd)
        except OSError:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
        if (
            not self._safe_file(current)
            or self._file_identity(entry) != self._file_identity(self.before)
            or self._file_identity(current) != self._file_identity(self.before)
        ):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    def close(self) -> None:
        _close_descriptors([self.fd, *reversed(self.ancestor_fds)])


class _PinnedWindowsImportSource:
    __slots__ = ("before", "capability", "fd", "managed_path")

    def __init__(
        self,
        *,
        fd: int,
        before: os.stat_result,
        capability: WindowsExternalFileCapability,
        managed_path: Path,
    ) -> None:
        self.fd = fd
        self.before = before
        self.capability = capability
        self.managed_path = managed_path

    @staticmethod
    def _identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_mode),
            int(value.st_nlink),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(getattr(value, "st_birthtime_ns", value.st_ctime_ns)),
        )

    @staticmethod
    def _safe_file(value: os.stat_result) -> bool:
        attributes = int(getattr(value, "st_file_attributes", 0) or 0)
        return (
            stat.S_ISREG(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and not bool(attributes & 0x400)
            and value.st_nlink == 1
            and 0 < value.st_size <= _MAX_IMPORT_BYTES
        )

    def verify(self) -> None:
        try:
            current_capability = capture_windows_external_fd(
                self.fd,
                generation_token=self.capability.generation_token,
            )
            validate_windows_external_file(self.capability)
            current = os.fstat(self.fd)
        except (OSError, TypeError, ValueError):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
        if (
            current_capability != self.capability
            or not self._safe_file(current)
            or self._identity(current) != self._identity(self.before)
            or _windows_path_within(Path(self.capability.path), self.managed_path)
        ):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    def close(self) -> None:
        _close_descriptors([self.fd])


def _windows_path_within(path: Path, parent: Path) -> bool:
    try:
        child_value = os.path.normcase(os.path.abspath(path))
        parent_value = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath((child_value, parent_value)) == parent_value
    except (OSError, TypeError, ValueError):
        return True


def _open_import_source(
    source_path: object,
    *,
    managed_identity: tuple[int, int],
    managed_path: Path | None = None,
) -> _PinnedImportSource | _PinnedWindowsImportSource:
    if sys.platform == "win32":
        if type(source_path) is not str or not source_path or "\0" in source_path:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        if not isinstance(managed_path, Path):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        path = Path(os.path.abspath(source_path))
        if not path.is_absolute() or os.path.normcase(os.fspath(path)) != os.path.normcase(
            os.path.abspath(source_path)
        ):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        fd = -1
        succeeded = False
        try:
            fd, capability = open_windows_external_file(path)
            before = os.fstat(fd)
            opened = _PinnedWindowsImportSource(
                fd=fd,
                before=before,
                capability=capability,
                managed_path=managed_path,
            )
            opened.verify()
            succeeded = True
            return opened
        except DaemonError:
            raise
        except (OSError, TypeError, ValueError):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
        finally:
            if not succeeded and fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
    if sys.platform != "darwin":
        raise DaemonError(DaemonErrorCode.UNSUPPORTED_PLATFORM)
    if type(source_path) is not str or not source_path.startswith("/"):
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)
    try:
        encoded = source_path.encode("utf-8")
    except UnicodeEncodeError:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
    if not encoded or len(encoded) > 4096 or b"\0" in encoded:
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)
    parts = source_path.split("/")[1:]
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise DaemonError(DaemonErrorCode.UNAVAILABLE)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    ancestor_fds: list[int] = []
    ancestor_identities: list[tuple[int, ...]] = []
    descriptor = -1
    succeeded = False
    try:
        root_fd = os.open("/", directory_flags)
        ancestor_fds.append(root_fd)
        root_info = os.fstat(root_fd)
        ancestor_identities.append(_PinnedImportSource._directory_identity(root_info))
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=ancestor_fds[-1])
            ancestor_fds.append(next_fd)
            current = os.fstat(next_fd)
            ancestor_identities.append(_PinnedImportSource._directory_identity(current))
            if (
                not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) == managed_identity
            ):
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        before_entry = os.stat(
            parts[-1],
            dir_fd=ancestor_fds[-1],
            follow_symlinks=False,
        )
        if not _PinnedImportSource._safe_file(before_entry):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=ancestor_fds[-1],
        )
        before = os.fstat(descriptor)
        if not _PinnedImportSource._safe_file(before) or _PinnedImportSource._file_identity(
            before
        ) != _PinnedImportSource._file_identity(before_entry):
            raise DaemonError(DaemonErrorCode.UNAVAILABLE)
        opened = _PinnedImportSource(
            ancestor_fds=tuple(ancestor_fds),
            ancestor_identities=tuple(ancestor_identities),
            ancestor_names=tuple(parts[:-1]),
            final_name=parts[-1],
            fd=descriptor,
            before=before,
            managed_identity=managed_identity,
        )
        opened.verify()
        succeeded = True
        return opened
    except DaemonError:
        raise
    except (OSError, ValueError):
        raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
    finally:
        if not succeeded:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            _close_descriptors(list(reversed(ancestor_fds)))


def _same_boot_state(left: PublishedDaemonState, right: PublishedDaemonState) -> bool:
    return (
        left.receipt == right.receipt
        and left.receipt_raw == right.receipt_raw
        and left.receipt_binding == right.receipt_binding
        and left.secret == right.secret
        and left.root.identity == right.root.identity
        and left.root.path == right.root.path
    )


def _create_windows_visual_stage(
    _run_root: Path,
) -> tuple[Path, None, WindowsPathCapability, int]:
    """Create one isolated private stage without mutating daemon state."""

    parent_path = Path(os.path.abspath(tempfile.gettempdir()))
    for _attempt in range(16):
        path = parent_path / (".vibecad_visual_" + secrets.token_hex(16))
        try:
            stage = ensure_private_directory(
                path,
                exclusive=True,
            )
        except FileExistsError:
            continue
        descriptor = -1
        try:
            descriptor = open_windows_directory_fd(path)
            opened = capture_windows_fd(
                descriptor,
                directory=True,
                generation_token=stage.generation_token,
            )
            if opened != stage:
                raise OSError("Windows visual stage identity changed")
            return path, None, stage, descriptor
        except BaseException:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            with contextlib.suppress(OSError):
                delete_windows_directory_capability(stage)
            raise
    raise OSError("unable to reserve a private Windows visual stage")


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
        connection: socket.socket | WindowsNamedPipeConnection,
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

    @property
    def daemon_pid(self) -> int:
        return self._boot_state.receipt.pid

    @property
    def daemon_started_ns(self) -> int:
        """Published process-generation identity used to reject PID reuse."""

        return self._boot_state.receipt.started_ns

    @classmethod
    def connect(
        cls,
        run_root: object,
        *,
        timeout_seconds: object = V2_HANDSHAKE_TIMEOUT_SECONDS,
    ) -> LocalKernelClient:
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= V2_HANDSHAKE_TIMEOUT_SECONDS
        ):
            raise DaemonError(DaemonErrorCode.INVALID_STATE)
        try:
            boot_state = read_boot_state(run_root)
        except DaemonError:
            raise DaemonError(DaemonErrorCode.AUTHENTICATION_FAILED) from None
        connection: socket.socket | WindowsNamedPipeConnection | None = None
        protocol = None
        connected = False
        reader = _FrameReader()
        try:
            if sys.platform == "win32":
                connection = connect_named_pipe(
                    named_pipe_name(boot_state.root.path),
                    timeout=float(timeout_seconds),
                )
            else:
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.set_inheritable(False)
            deadline = time.monotonic() + float(timeout_seconds)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.settimeout(remaining)
            if sys.platform == "win32":
                require_expected_peer(
                    connection,
                    pid=boot_state.receipt.pid,
                    started_ns=boot_state.receipt.started_ns,
                )
            else:
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
            if not connected and connection is not None:
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
        return self._call(
            method,
            params,
            request_id=request_id,
            descriptor=None,
        )

    def retire(
        self,
        *,
        reason: object,
        request_id: object | None = None,
        timeout_seconds: object | None = None,
    ) -> V2Response:
        """Request authenticated retirement of this exact daemon instance."""

        if timeout_seconds is not None and (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= V2_IDLE_TIMEOUT_SECONDS
        ):
            raise DaemonError(DaemonErrorCode.INVALID_STATE)
        try:
            return self._call(
                "kernel.retire",
                {
                    "daemon_id": self.daemon_id,
                    "reason": reason,
                },
                request_id=request_id,
                descriptor=None,
                timeout_seconds=timeout_seconds,
            )
        finally:
            if sys.platform == "win32":
                # The server uses this one-shot close as native proof that the
                # Named Pipe retirement response was consumed before it calls
                # DisconnectNamedPipe during daemon teardown.
                self.close()

    def _call(
        self,
        method: object,
        params: object,
        *,
        request_id: object | None,
        descriptor: int | None,
        timeout_seconds: object | None = None,
    ) -> V2Response:
        self._ensure_live()
        absolute_deadline = (
            None if timeout_seconds is None else time.monotonic() + float(timeout_seconds)
        )
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
                deadline = (
                    time.monotonic() + V2_IDLE_TIMEOUT_SECONDS
                    if absolute_deadline is None
                    else absolute_deadline
                )
                if descriptor is None:
                    _send(self._connection, request, deadline=deadline)
                else:
                    _send_descriptor(
                        self._connection,
                        request,
                        descriptor,
                        deadline=deadline,
                    )
                response = self._reader.receive(
                    self._connection,
                    deadline=absolute_deadline,
                    fragment_idle_seconds=(
                        V2_IDLE_TIMEOUT_SECONDS if absolute_deadline is None else None
                    ),
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

    def import_project(
        self,
        request: object,
        *,
        source_path: object,
        request_id: object | None = None,
    ) -> V2Response:
        """Send one path-free import request with one pinned local descriptor."""

        self._ensure_live()
        try:
            managed = os.stat(
                self._boot_state.root.path.parent,
                follow_symlinks=False,
            )
        except OSError:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
        try:
            pinned = _open_import_source(
                source_path,
                managed_identity=(managed.st_dev, managed.st_ino),
                managed_path=Path(self._boot_state.root.path.parent),
            )
        except DaemonError as error:
            if error.code is DaemonErrorCode.UNSUPPORTED_PLATFORM:
                raise
            raise LocalImportSourceError from None
        try:
            try:
                pinned.verify()
                locator = bind_v2_import_locator(request, pinned.before)
            except (DaemonError, V2ProtocolError):
                raise LocalImportSourceError from None
            response = self._call(
                "project.import",
                {"request": request, "locator": locator},
                request_id=request_id,
                descriptor=pinned.fd,
            )
            with contextlib.suppress(DaemonError):
                # The authenticated response is already a known durable
                # outcome. A source change observed only after that boundary
                # must not be rewritten as a preflight invalid-input failure.
                pinned.verify()
            return response
        finally:
            pinned.close()

    def seal_visual_image_set(
        self,
        request: object,
        *,
        source_paths: object,
        request_id: object | None = None,
    ) -> V2Response:
        """Seal 1-16 local images through one path-free staging-directory FD."""

        self._ensure_live()
        try:
            canonical_request = parse_seal_image_set_request(request)
        except VisualIngressError:
            raise LocalVisualSourceError from None
        if type(source_paths) not in {list, tuple} or any(
            type(path) is not str for path in source_paths
        ):
            raise LocalVisualSourceError
        paths = tuple(source_paths)
        if len(paths) != len(canonical_request.inputs):
            raise LocalVisualSourceError
        if sys.platform == "win32":
            return self._seal_visual_image_set_windows(
                canonical_request,
                paths,
                request_id=request_id,
            )
        try:
            managed = os.stat(
                self._boot_state.root.path.parent,
                follow_symlinks=False,
            )
        except OSError:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None

        pinned_sources: list[_PinnedImportSource] = []
        stage_path: str | None = None
        stage_fd = -1
        staged_names: list[str] = []
        stage_identity: tuple[int, int] | None = None
        try:
            total_bytes = 0
            identities: set[tuple[int, int]] = set()
            for path in paths:
                try:
                    pinned = _open_import_source(
                        path,
                        managed_identity=(managed.st_dev, managed.st_ino),
                        managed_path=Path(self._boot_state.root.path.parent),
                    )
                except DaemonError as error:
                    if error.code is DaemonErrorCode.UNSUPPORTED_PLATFORM:
                        raise
                    raise LocalVisualSourceError from None
                pinned_sources.append(pinned)
                identity = (pinned.before.st_dev, pinned.before.st_ino)
                if identity in identities or pinned.before.st_size > MAX_IMAGE_SOURCE_BYTES:
                    raise LocalVisualSourceError
                identities.add(identity)
                total_bytes += pinned.before.st_size
                if total_bytes > MAX_IMAGE_SET_SOURCE_BYTES:
                    raise LocalVisualSourceError

            try:
                stage_path = tempfile.mkdtemp(prefix=".vibecad_visual_")
                original_stage = os.stat(stage_path, follow_symlinks=False)
                stage_identity = (original_stage.st_dev, original_stage.st_ino)
                if (
                    not stat.S_ISDIR(original_stage.st_mode)
                    or original_stage.st_uid != os.geteuid()
                    or stat.S_IMODE(original_stage.st_mode) != 0o700
                ):
                    raise LocalVisualSourceError
                stage_fd = os.open(
                    stage_path,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )
                stage_info = os.fstat(stage_fd)
                if (
                    (stage_info.st_dev, stage_info.st_ino) != stage_identity
                    or not stat.S_ISDIR(stage_info.st_mode)
                    or stage_info.st_uid != os.geteuid()
                    or stat.S_IMODE(stage_info.st_mode) != 0o700
                ):
                    raise LocalVisualSourceError
                staged_stats: list[os.stat_result] = []
                source_digests: list[str] = []
                for index, pinned in enumerate(pinned_sources):
                    pinned.verify()
                    name = f"source_{index}"
                    staged_names.append(name)
                    target = -1
                    try:
                        target = os.open(
                            name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                            0o600,
                            dir_fd=stage_fd,
                        )
                        os.fchmod(target, 0o600)
                        remaining = pinned.before.st_size
                        offset = 0
                        digest = hashlib.sha256()
                        while remaining:
                            chunk = os.pread(pinned.fd, min(64 * 1024, remaining), offset)
                            if not chunk:
                                raise LocalVisualSourceError
                            digest.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                written = os.write(target, view)
                                if written <= 0:
                                    raise OSError
                                view = view[written:]
                            remaining -= len(chunk)
                            offset += len(chunk)
                        pinned.verify()
                        os.fsync(target)
                        staged = os.fstat(target)
                        if (
                            not stat.S_ISREG(staged.st_mode)
                            or staged.st_uid != os.geteuid()
                            or staged.st_nlink != 1
                            or stat.S_IMODE(staged.st_mode) != 0o600
                            or staged.st_size != pinned.before.st_size
                        ):
                            raise LocalVisualSourceError
                        staged_stats.append(staged)
                        source_digests.append(digest.hexdigest())
                    finally:
                        if target >= 0:
                            with contextlib.suppress(OSError):
                                os.close(target)
                os.fsync(stage_fd)
                observed_names = os.listdir(stage_fd)
                if set(observed_names) != set(staged_names) or len(observed_names) != len(
                    staged_names
                ):
                    raise LocalVisualSourceError
                locator = bind_visual_staging_locator(
                    canonical_request,
                    os.fstat(stage_fd),
                    tuple(staged_stats),
                    tuple(source_digests),
                )
            except LocalVisualSourceError:
                raise
            except (OSError, VisualIngressError):
                raise LocalVisualSourceError from None

            response = self._call(
                "visual_inputs.seal",
                {
                    "request": canonical_request.to_mapping(),
                    "locator": locator,
                },
                request_id=request_id,
                descriptor=stage_fd,
            )
            if response.error is None:
                try:
                    validate_seal_result(response.result)
                except VisualIngressError:
                    self.close()
                    raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
            return response
        finally:
            active_error = sys.exc_info()[0] is not None
            cleanup_failed = False
            for pinned in reversed(pinned_sources):
                pinned.close()
            if stage_fd >= 0:
                for index in reversed(range(len(pinned_sources))):
                    try:
                        os.unlink(f"source_{index}", dir_fd=stage_fd)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        cleanup_failed = True
                try:
                    remaining_names = os.listdir(stage_fd)
                except OSError:
                    cleanup_failed = True
                else:
                    if remaining_names:
                        cleanup_failed = True
                try:
                    os.close(stage_fd)
                except OSError:
                    cleanup_failed = True
            if stage_path is not None and stage_identity is not None:
                try:
                    path_info = os.stat(stage_path, follow_symlinks=False)
                except OSError:
                    pass
                else:
                    if (path_info.st_dev, path_info.st_ino) == stage_identity:
                        with contextlib.suppress(OSError):
                            os.rmdir(stage_path)
            if cleanup_failed and not active_error:
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)

    def _seal_visual_image_set_windows(
        self,
        canonical_request,
        paths: tuple[str, ...],
        *,
        request_id: object | None,
    ) -> V2Response:
        """Win32 visual ingress using File-ID-pinned sources and a private stage."""

        managed_path = Path(os.path.abspath(self._boot_state.root.path.parent))
        try:
            managed = os.stat(managed_path, follow_symlinks=False)
        except OSError:
            raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None

        pinned_sources: list[_PinnedWindowsImportSource] = []
        stage_path: Path | None = None
        stage_parent: WindowsPathCapability | None = None
        stage_capability: WindowsPathCapability | None = None
        stage_fd = -1
        staged: list[tuple[Path, WindowsPathCapability]] = []
        try:
            total_bytes = 0
            identities: set[tuple[int, int]] = set()
            for path in paths:
                try:
                    pinned = _open_import_source(
                        path,
                        managed_identity=(managed.st_dev, managed.st_ino),
                        managed_path=managed_path,
                    )
                except DaemonError as error:
                    if error.code is DaemonErrorCode.UNSUPPORTED_PLATFORM:
                        raise
                    raise LocalVisualSourceError from None
                if type(pinned) is not _PinnedWindowsImportSource:
                    raise LocalVisualSourceError
                pinned_sources.append(pinned)
                identity = (pinned.capability.volume, pinned.capability.file_id)
                if identity in identities or pinned.before.st_size > MAX_IMAGE_SOURCE_BYTES:
                    raise LocalVisualSourceError
                identities.add(identity)
                total_bytes += pinned.before.st_size
                if total_bytes > MAX_IMAGE_SET_SOURCE_BYTES:
                    raise LocalVisualSourceError

            try:
                stage_path, stage_parent, stage_capability, stage_fd = _create_windows_visual_stage(
                    Path(self._boot_state.root.path.parent)
                )
                staged_stats: list[os.stat_result] = []
                source_digests: list[str] = []
                for index, pinned in enumerate(pinned_sources):
                    pinned.verify()
                    name = f"source_{index}"
                    target_path = stage_path / name
                    target = -1
                    target_capability = None
                    try:
                        target, target_capability = open_private_file(
                            target_path,
                            create=True,
                            read_write=True,
                            exclusive=True,
                            expected_parent=stage_capability,
                        )
                        remaining = pinned.before.st_size
                        offset = 0
                        digest = hashlib.sha256()
                        while remaining:
                            chunk = portable_pread(
                                pinned.fd,
                                min(64 * 1024, remaining),
                                offset,
                            )
                            if not chunk or len(chunk) > remaining:
                                raise LocalVisualSourceError
                            digest.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                written = os.write(target, view)
                                if written <= 0:
                                    raise OSError
                                view = view[written:]
                            remaining -= len(chunk)
                            offset += len(chunk)
                        pinned.verify()
                        os.fsync(target)
                        staged_stat = os.fstat(target)
                        recaptured = capture_windows_fd(
                            target,
                            directory=False,
                            generation_token=target_capability.generation_token,
                        )
                        attributes = int(getattr(staged_stat, "st_file_attributes", 0) or 0)
                        if (
                            recaptured != target_capability
                            or not stat.S_ISREG(staged_stat.st_mode)
                            or stat.S_ISLNK(staged_stat.st_mode)
                            or bool(attributes & 0x400)
                            or staged_stat.st_nlink != 1
                            or staged_stat.st_size != pinned.before.st_size
                        ):
                            raise LocalVisualSourceError
                        staged_stats.append(staged_stat)
                        source_digests.append(digest.hexdigest())
                        staged.append((target_path, target_capability))
                    finally:
                        if target >= 0:
                            with contextlib.suppress(OSError):
                                os.close(target)
                validate_windows_path(stage_capability, directory=True)
                observed_names = tuple(
                    entry.name for entry in os.scandir(windows_extended_path(stage_capability.path))
                )
                expected_names = tuple(f"source_{index}" for index in range(len(staged)))
                if len(observed_names) != len(expected_names) or set(observed_names) != set(
                    expected_names
                ):
                    raise LocalVisualSourceError
                locator = bind_visual_staging_locator(
                    canonical_request,
                    os.fstat(stage_fd),
                    tuple(staged_stats),
                    tuple(source_digests),
                )
            except LocalVisualSourceError:
                raise
            except (OSError, TypeError, ValueError, VisualIngressError):
                raise LocalVisualSourceError from None

            response = self._call(
                "visual_inputs.seal",
                {
                    "request": canonical_request.to_mapping(),
                    "locator": locator,
                },
                request_id=request_id,
                descriptor=stage_fd,
            )
            if response.error is None:
                try:
                    validate_seal_result(response.result)
                except VisualIngressError:
                    self.close()
                    raise DaemonError(DaemonErrorCode.UNAVAILABLE) from None
            return response
        finally:
            active_error = sys.exc_info()[0] is not None
            cleanup_failed = False
            for pinned in reversed(pinned_sources):
                pinned.close()
            if stage_capability is not None:
                for path, capability in reversed(staged):
                    try:
                        delete_windows_file(
                            path,
                            parent=stage_capability,
                            expected=capability,
                        )
                    except FileNotFoundError:
                        pass
                    except (OSError, TypeError, ValueError):
                        cleanup_failed = True
                try:
                    remaining_names = tuple(
                        entry.name
                        for entry in os.scandir(windows_extended_path(stage_capability.path))
                    )
                except (OSError, TypeError, ValueError):
                    cleanup_failed = True
                else:
                    if remaining_names:
                        cleanup_failed = True
            if stage_fd >= 0:
                try:
                    os.close(stage_fd)
                except OSError:
                    cleanup_failed = True
            if stage_path is not None and stage_capability is not None and not cleanup_failed:
                try:
                    if stage_parent is None:
                        delete_windows_directory_capability(stage_capability)
                    else:
                        delete_windows_directory(
                            stage_path,
                            parent=stage_parent,
                            expected=stage_capability,
                        )
                except (OSError, TypeError, ValueError):
                    cleanup_failed = True
            if cleanup_failed and not active_error:
                raise DaemonError(DaemonErrorCode.UNAVAILABLE)

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


__all__ = (
    "LocalImportSourceError",
    "LocalKernelClient",
    "LocalVisualSourceError",
)
