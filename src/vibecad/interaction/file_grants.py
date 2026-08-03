"""One-shot, authenticated-session grants for managed checkout files."""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.interaction.checkouts import (
    CheckoutFileSnapshot,
    CheckoutSourceLiveness,
    CheckoutState,
)

FILE_GRANT_TTL_MS = 30_000
MAX_FILE_GRANTS_PER_SESSION = 8
MAX_ACTIVE_FILE_GRANTS = 64
MAX_RECENT_FILE_GRANT_IDS = 65_536

_DAEMON_RE = re.compile(r"daemon_[0-9a-f]{32}\Z")
_SESSION_RE = re.compile(r"session_[0-9a-f]{32}\Z")
_CHECKOUT_RE = re.compile(r"checkout_[0-9a-f]{32}\Z")
_GRANT_RE = re.compile(r"file_grant_[0-9a-f]{32}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[0-9a-f]{32}\Z")
_TTL_NS = FILE_GRANT_TTL_MS * 1_000_000
_TOKEN_ATTEMPTS = 8


def _new_token() -> str:
    return secrets.token_hex(16)


class FileGrantPurpose(StrEnum):
    OPEN_MANAGED_CHECKOUT = "open_managed_checkout"


class FileGrantErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    UNAVAILABLE = "unavailable"
    WRONG_PROCESS = "wrong_process"
    INVALID_STATE = "invalid_state"


_MESSAGES = {
    FileGrantErrorCode.INVALID_INPUT: "The file grant request is invalid.",
    FileGrantErrorCode.RESOURCE_EXHAUSTED: "The file grant capacity is exhausted.",
    FileGrantErrorCode.UNAVAILABLE: "The file grant is unavailable.",
    FileGrantErrorCode.WRONG_PROCESS: "The file grant broker belongs to another process.",
    FileGrantErrorCode.INVALID_STATE: "The file grant broker is not available.",
}


class FileGrantError(RuntimeError):
    __slots__ = ("code", "message")

    def __init__(self, code: FileGrantErrorCode) -> None:
        if type(code) is not FileGrantErrorCode:
            raise TypeError("code must be a FileGrantErrorCode")
        self.code = code
        self.message = _MESSAGES[code]
        super().__init__(self.message)


def _identifier(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise FileGrantError(FileGrantErrorCode.INVALID_INPUT)
    return value


def _purpose(value: object) -> FileGrantPurpose:
    if type(value) is not FileGrantPurpose:
        raise FileGrantError(FileGrantErrorCode.INVALID_INPUT)
    return value


def _clock_value(clock: Callable[[], int]) -> int:
    try:
        value = clock()
    except BaseException:
        raise FileGrantError(FileGrantErrorCode.UNAVAILABLE) from None
    if type(value) is not int or value < 0:
        raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
    return value


def _require_managed_path(path: object, checkout_id: str) -> Path:
    if type(path) is not type(Path("/")) or not path.is_absolute():
        raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
    if (
        ".." in path.parts
        or path.name != "model.FCStd"
        or path.parent.name != checkout_id
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in str(path))
    ):
        raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
    return path


def _validate_snapshot(
    value: object,
    *,
    checkout_id: str,
) -> CheckoutFileSnapshot:
    if type(value) is not CheckoutFileSnapshot:
        raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
    descriptor = value.descriptor
    if (
        descriptor.checkout_id != checkout_id
        or descriptor.state is not CheckoutState.OPEN
        or descriptor.source_liveness is not CheckoutSourceLiveness.LIVE
        or descriptor.local_path is None
    ):
        raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
    _require_managed_path(descriptor.local_path, checkout_id)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class FileGrantDescriptor:
    grant_id: str
    purpose: FileGrantPurpose
    expires_in_ms: int

    def __post_init__(self) -> None:
        _identifier(self.grant_id, _GRANT_RE)
        _purpose(self.purpose)
        if type(self.expires_in_ms) is not int or self.expires_in_ms != FILE_GRANT_TTL_MS:
            raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": 1,
            "grant_id": self.grant_id,
            "purpose": self.purpose.value,
            "expires_in_ms": self.expires_in_ms,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class FileGrantClaim:
    grant_id: str
    checkout_id: str
    purpose: FileGrantPurpose
    local_path: Path
    current_model_sha256: str
    current_size_bytes: int

    def __post_init__(self) -> None:
        _identifier(self.grant_id, _GRANT_RE)
        _identifier(self.checkout_id, _CHECKOUT_RE)
        _purpose(self.purpose)
        _require_managed_path(self.local_path, self.checkout_id)
        if (
            type(self.current_model_sha256) is not str
            or _DIGEST_RE.fullmatch(self.current_model_sha256) is None
            or type(self.current_size_bytes) is not int
            or self.current_size_bytes < 0
        ):
            raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)

    def to_mapping(self) -> dict[str, int | str]:
        return {
            "schema_version": 1,
            "grant_id": self.grant_id,
            "checkout_id": self.checkout_id,
            "purpose": self.purpose.value,
            "local_path": str(self.local_path),
            "current_model_sha256": self.current_model_sha256,
            "current_size_bytes": self.current_size_bytes,
        }


class _GrantState(StrEnum):
    MINTING = "minting"
    MINTED = "minted"
    CLAIMING = "claiming"


@dataclass(slots=True, eq=False)
class _GrantRecord:
    grant_id: str
    daemon_id: str
    session_id: str
    checkout_id: str
    purpose: FileGrantPurpose
    expires_ns: int
    state: _GrantState
    snapshot: CheckoutFileSnapshot | None = None


class FileGrantBroker:
    """Daemon-owned in-memory grant registry; no client path enters this API."""

    __slots__ = (
        "_clock_ns",
        "_closed",
        "_creator_pid",
        "_daemon_id",
        "_grants",
        "_lock",
        "_recent_id_order",
        "_recent_ids",
        "_session_checkout",
        "_token_hex",
    )

    def __init__(
        self,
        daemon_id: object,
        *,
        clock_ns: Callable[[], int] = time.monotonic_ns,
        token_hex: Callable[[], str] | None = None,
    ) -> None:
        if not callable(clock_ns) or (token_hex is not None and not callable(token_hex)):
            raise TypeError("file grant broker dependencies must be callable")
        self._daemon_id = _identifier(daemon_id, _DAEMON_RE)
        self._clock_ns = clock_ns
        self._token_hex = _new_token if token_hex is None else token_hex
        self._creator_pid = os.getpid()
        self._lock = threading.Lock()
        self._grants: dict[str, _GrantRecord] = {}
        self._session_checkout: dict[tuple[str, str, FileGrantPurpose], str] = {}
        self._recent_id_order: deque[str] = deque()
        self._recent_ids: set[str] = set()
        self._closed = False

    @property
    def active_grants(self) -> int:
        self._ensure_process()
        with self._lock:
            return len(self._grants)

    def _ensure_process(self) -> None:
        if os.getpid() != self._creator_pid:
            raise FileGrantError(FileGrantErrorCode.WRONG_PROCESS)

    def _ensure_open_locked(self) -> None:
        if self._closed:
            raise FileGrantError(FileGrantErrorCode.INVALID_STATE)

    def _drop_locked(self, record: _GrantRecord) -> None:
        if self._grants.get(record.grant_id) is not record:
            return
        del self._grants[record.grant_id]
        key = (record.session_id, record.checkout_id, record.purpose)
        if self._session_checkout.get(key) == record.grant_id:
            del self._session_checkout[key]

    def _clean_expired_locked(self, now_ns: int) -> None:
        for record in tuple(self._grants.values()):
            if now_ns >= record.expires_ns:
                self._drop_locked(record)

    def _new_id_locked(self) -> str:
        for _ in range(_TOKEN_ATTEMPTS):
            try:
                token = self._token_hex()
            except BaseException:
                raise FileGrantError(FileGrantErrorCode.UNAVAILABLE) from None
            if type(token) is not str or _TOKEN_RE.fullmatch(token) is None:
                raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
            grant_id = "file_grant_" + token
            if grant_id not in self._recent_ids and grant_id not in self._grants:
                while len(self._recent_id_order) >= MAX_RECENT_FILE_GRANT_IDS:
                    expired = self._recent_id_order.popleft()
                    self._recent_ids.remove(expired)
                self._recent_id_order.append(grant_id)
                self._recent_ids.add(grant_id)
                return grant_id
        raise FileGrantError(FileGrantErrorCode.RESOURCE_EXHAUSTED)

    def mint(
        self,
        *,
        session_id: object,
        checkout_id: object,
        capture: Callable[[], CheckoutFileSnapshot],
        purpose: FileGrantPurpose = FileGrantPurpose.OPEN_MANAGED_CHECKOUT,
    ) -> tuple[CheckoutFileSnapshot, FileGrantDescriptor]:
        self._ensure_process()
        canonical_session = _identifier(session_id, _SESSION_RE)
        canonical_checkout = _identifier(checkout_id, _CHECKOUT_RE)
        canonical_purpose = _purpose(purpose)
        if not callable(capture):
            raise FileGrantError(FileGrantErrorCode.INVALID_INPUT)
        now_ns = _clock_value(self._clock_ns)
        with self._lock:
            self._ensure_open_locked()
            self._clean_expired_locked(now_ns)
            key = (canonical_session, canonical_checkout, canonical_purpose)
            previous_id = self._session_checkout.get(key)
            if previous_id is not None:
                previous = self._grants.get(previous_id)
                if previous is not None:
                    self._drop_locked(previous)
            session_count = sum(
                record.session_id == canonical_session for record in self._grants.values()
            )
            if (
                session_count >= MAX_FILE_GRANTS_PER_SESSION
                or len(self._grants) >= MAX_ACTIVE_FILE_GRANTS
            ):
                raise FileGrantError(FileGrantErrorCode.RESOURCE_EXHAUSTED)
            record = _GrantRecord(
                grant_id=self._new_id_locked(),
                daemon_id=self._daemon_id,
                session_id=canonical_session,
                checkout_id=canonical_checkout,
                purpose=canonical_purpose,
                expires_ns=now_ns + _TTL_NS,
                state=_GrantState.MINTING,
            )
            self._grants[record.grant_id] = record
            self._session_checkout[key] = record.grant_id
        try:
            snapshot = _validate_snapshot(
                capture(),
                checkout_id=canonical_checkout,
            )
        except BaseException:
            with self._lock:
                self._drop_locked(record)
            raise
        with self._lock:
            self._ensure_open_locked()
            current_ns = _clock_value(self._clock_ns)
            if (
                self._grants.get(record.grant_id) is not record
                or record.state is not _GrantState.MINTING
                or current_ns >= record.expires_ns
            ):
                self._drop_locked(record)
                raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
            record.snapshot = snapshot
            record.expires_ns = current_ns + _TTL_NS
            record.state = _GrantState.MINTED
        return snapshot, FileGrantDescriptor(
            grant_id=record.grant_id,
            purpose=record.purpose,
            expires_in_ms=FILE_GRANT_TTL_MS,
        )

    def claim(
        self,
        *,
        session_id: object,
        grant_id: object,
        require_same: Callable[[CheckoutFileSnapshot], CheckoutFileSnapshot],
    ) -> FileGrantClaim:
        self._ensure_process()
        canonical_session = _identifier(session_id, _SESSION_RE)
        canonical_grant = _identifier(grant_id, _GRANT_RE)
        if not callable(require_same):
            raise FileGrantError(FileGrantErrorCode.INVALID_INPUT)
        now_ns = _clock_value(self._clock_ns)
        with self._lock:
            self._ensure_open_locked()
            self._clean_expired_locked(now_ns)
            record = self._grants.get(canonical_grant)
            if (
                record is None
                or record.daemon_id != self._daemon_id
                or record.session_id != canonical_session
                or record.state is not _GrantState.MINTED
                or record.snapshot is None
            ):
                raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
            record.state = _GrantState.CLAIMING
            expected = record.snapshot
        try:
            current = _validate_snapshot(
                require_same(expected),
                checkout_id=record.checkout_id,
            )
            if current != expected:
                raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
        except BaseException:
            with self._lock:
                self._drop_locked(record)
            raise
        with self._lock:
            self._ensure_open_locked()
            current_ns = _clock_value(self._clock_ns)
            if (
                self._grants.get(record.grant_id) is not record
                or record.state is not _GrantState.CLAIMING
                or current_ns >= record.expires_ns
            ):
                self._drop_locked(record)
                raise FileGrantError(FileGrantErrorCode.UNAVAILABLE)
            self._drop_locked(record)
        descriptor = current.descriptor
        return FileGrantClaim(
            grant_id=record.grant_id,
            checkout_id=record.checkout_id,
            purpose=record.purpose,
            local_path=descriptor.local_path,
            current_model_sha256=descriptor.current_model_sha256,
            current_size_bytes=descriptor.current_size_bytes,
        )

    def revoke_checkout(self, checkout_id: object) -> int:
        self._ensure_process()
        canonical = _identifier(checkout_id, _CHECKOUT_RE)
        with self._lock:
            self._ensure_open_locked()
            selected = tuple(
                record for record in self._grants.values() if record.checkout_id == canonical
            )
            for record in selected:
                self._drop_locked(record)
            return len(selected)

    def close_session(self, session_id: object) -> int:
        self._ensure_process()
        canonical = _identifier(session_id, _SESSION_RE)
        with self._lock:
            if self._closed:
                return 0
            selected = tuple(
                record for record in self._grants.values() if record.session_id == canonical
            )
            for record in selected:
                self._drop_locked(record)
            return len(selected)

    def close(self) -> None:
        self._ensure_process()
        with self._lock:
            if self._closed:
                return
            self._grants.clear()
            self._session_checkout.clear()
            self._recent_id_order.clear()
            self._recent_ids.clear()
            self._closed = True


__all__ = (
    "FILE_GRANT_TTL_MS",
    "MAX_ACTIVE_FILE_GRANTS",
    "MAX_FILE_GRANTS_PER_SESSION",
    "MAX_RECENT_FILE_GRANT_IDS",
    "FileGrantBroker",
    "FileGrantClaim",
    "FileGrantDescriptor",
    "FileGrantError",
    "FileGrantErrorCode",
    "FileGrantPurpose",
)
