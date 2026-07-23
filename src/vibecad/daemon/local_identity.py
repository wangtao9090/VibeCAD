"""Fail-closed macOS peer identity for accepted local Unix sockets."""

from __future__ import annotations

import ctypes
import functools
import os
import socket
import sys
from dataclasses import dataclass
from enum import StrEnum

__all__ = (
    "LocalIdentityError",
    "LocalIdentityErrorCode",
    "PeerIdentity",
    "darwin_peer_identity",
    "require_same_user_peer",
)


class LocalIdentityErrorCode(StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INVALID_SOCKET = "invalid_socket"
    PEER_UNAVAILABLE = "peer_unavailable"
    DIFFERENT_USER = "different_user"


class LocalIdentityError(OSError):
    __slots__ = ("code", "message")

    def __init__(self, code: LocalIdentityErrorCode) -> None:
        if type(code) is not LocalIdentityErrorCode:
            raise TypeError("code must be a LocalIdentityErrorCode")
        self.code = code
        self.message = {
            LocalIdentityErrorCode.UNSUPPORTED_PLATFORM: (
                "Local peer identity is unsupported on this platform."
            ),
            LocalIdentityErrorCode.INVALID_SOCKET: (
                "Local peer identity requires a connected Unix stream socket."
            ),
            LocalIdentityErrorCode.PEER_UNAVAILABLE: "Local peer identity is unavailable.",
            LocalIdentityErrorCode.DIFFERENT_USER: (
                "The local connection belongs to a different operating-system user."
            ),
        }[code]
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    euid: int
    egid: int


@functools.lru_cache(maxsize=1)
def _load_getpeereid():
    if sys.platform != "darwin" or ctypes.sizeof(ctypes.c_uint32) != 4:
        raise LocalIdentityError(LocalIdentityErrorCode.UNSUPPORTED_PLATFORM)
    try:
        function = ctypes.CDLL(None, use_errno=True).getpeereid
    except (AttributeError, OSError):
        raise LocalIdentityError(LocalIdentityErrorCode.UNSUPPORTED_PLATFORM) from None
    function.argtypes = (
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    )
    function.restype = ctypes.c_int
    return function


def _connected_unix_stream_fd(connection: object) -> int:
    if not isinstance(connection, socket.socket):
        raise LocalIdentityError(LocalIdentityErrorCode.INVALID_SOCKET)
    try:
        descriptor = connection.fileno()
        socket_type = connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
        connection.getpeername()
    except OSError:
        raise LocalIdentityError(LocalIdentityErrorCode.INVALID_SOCKET) from None
    if (
        type(descriptor) is not int
        or descriptor < 0
        or connection.family != socket.AF_UNIX
        or socket_type != socket.SOCK_STREAM
    ):
        raise LocalIdentityError(LocalIdentityErrorCode.INVALID_SOCKET)
    return descriptor


def darwin_peer_identity(connection: object) -> PeerIdentity:
    if sys.platform != "darwin":
        raise LocalIdentityError(LocalIdentityErrorCode.UNSUPPORTED_PLATFORM)
    descriptor = _connected_unix_stream_fd(connection)
    function = _load_getpeereid()
    euid = ctypes.c_uint32()
    egid = ctypes.c_uint32()
    ctypes.set_errno(0)
    if function(descriptor, ctypes.byref(euid), ctypes.byref(egid)) != 0:
        raise LocalIdentityError(LocalIdentityErrorCode.PEER_UNAVAILABLE)
    if _connected_unix_stream_fd(connection) != descriptor:
        raise LocalIdentityError(LocalIdentityErrorCode.PEER_UNAVAILABLE)
    return PeerIdentity(euid=int(euid.value), egid=int(egid.value))


def require_same_user_peer(connection: object) -> PeerIdentity:
    identity = darwin_peer_identity(connection)
    try:
        current = os.geteuid()
    except AttributeError:
        raise LocalIdentityError(LocalIdentityErrorCode.UNSUPPORTED_PLATFORM) from None
    if type(current) is not int or identity.euid != current:
        raise LocalIdentityError(LocalIdentityErrorCode.DIFFERENT_USER)
    return identity
