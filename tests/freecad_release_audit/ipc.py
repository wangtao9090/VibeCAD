"""Bounded length-prefixed IPC for child observations.

Children may write frames through this module. Only the controller converts
validated observations into JSONL records.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Callable

MAX_FRAME_BYTES = 16_384
_PREFIX = struct.Struct(">I")


class FrameError(RuntimeError):
    """Base class for malformed or incomplete framed observations."""


class FrameEOF(FrameError):
    """A frame ended after its first byte but before it was complete."""


class FrameOversize(FrameError):
    """A declared or supplied frame exceeds the configured bound."""


class FrameIOError(FrameError):
    """The frame transport returned an invalid read or write result."""


def _payload(payload: object, maximum: int) -> bytes:
    if type(maximum) is not int or not 0 < maximum <= MAX_FRAME_BYTES:
        raise ValueError("invalid frame bound")
    if type(payload) is not bytes:
        raise TypeError("frame payload must be bytes")
    if not 0 < len(payload) <= maximum:
        raise FrameOversize("frame payload is empty or oversized")
    return payload


def canonical_payload(value: object, *, maximum: int = MAX_FRAME_BYTES) -> bytes:
    """Return a canonical bounded JSON object suitable for framing."""
    if type(maximum) is not int or not 0 < maximum <= MAX_FRAME_BYTES:
        raise ValueError("invalid frame bound")
    if type(value) is not dict:
        raise TypeError("IPC observation must be an object")
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise FrameIOError("observation is not canonical JSON data") from error
    return _payload(raw, maximum)


def encode_frame(payload: object, *, maximum: int = MAX_FRAME_BYTES) -> bytes:
    """Prefix one bounded payload with its unsigned big-endian length."""
    raw = _payload(payload, maximum)
    return _PREFIX.pack(len(raw)) + raw


def write_frame(
    write: Callable[[bytes], object],
    payload: object,
    *,
    maximum: int = MAX_FRAME_BYTES,
) -> int:
    """Write one complete frame, accepting ordinary partial writes."""
    frame = encode_frame(payload, maximum=maximum)
    offset = 0
    while offset < len(frame):
        count = write(frame[offset:])
        if type(count) is not int or not 0 < count <= len(frame) - offset:
            raise FrameIOError("transport returned an invalid write count")
        offset += count
    return offset


class FrameReader:
    """Read complete frames while distinguishing clean and truncated EOF."""

    def __init__(
        self,
        read: Callable[[int], object],
        *,
        maximum: int = MAX_FRAME_BYTES,
    ) -> None:
        if type(maximum) is not int or not 0 < maximum <= MAX_FRAME_BYTES:
            raise ValueError("invalid frame bound")
        self._read = read
        self._maximum = maximum

    def _exact(self, size: int, *, clean_eof: bool = False) -> bytes | None:
        result = bytearray()
        while len(result) < size:
            chunk = self._read(size - len(result))
            if type(chunk) is not bytes or len(chunk) > size - len(result):
                raise FrameIOError("transport returned invalid bytes")
            if not chunk:
                if clean_eof and not result:
                    return None
                raise FrameEOF("unexpected EOF inside frame")
            result.extend(chunk)
        return bytes(result)

    def read_frame(self) -> bytes | None:
        """Return the next payload, or ``None`` for EOF between frames."""
        prefix = self._exact(_PREFIX.size, clean_eof=True)
        if prefix is None:
            return None
        size = _PREFIX.unpack(prefix)[0]
        if not 0 < size <= self._maximum:
            raise FrameOversize("declared frame size is empty or oversized")
        payload = self._exact(size)
        assert payload is not None
        return payload
