from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

__all__ = ("KernelGateway",)

_MAX_DEPTH = 12
_MAX_NODES = 10_000
_MAX_STRING = 4096
_MAX_KEY = 128
_MAX_CONTAINER = 1000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_PROJECT_ID_PREFIX = "project_"
_TASK_ID_PREFIX = "task_"
_DRAFT_ID_PREFIX = "draft_"
_CHECKOUT_ID_PREFIX = "checkout_"
_OPEN_KEY_PREFIX = "checkout_open_"
_DAEMON_ID_PREFIX = "daemon_"
_REPLAY_CAPACITY = 64
_MAX_CHECKOUT_AUTHORITIES = 8
_UNBOUND_WIRE_CAPABILITY = object()
_PUBLIC_COMMAND_KINDS = frozenset(
    (
        "connect",
        "list_projects",
        "list_tasks",
        "refresh_project",
        "refresh_task",
        "preview_open",
        "preview_refresh",
    )
)
_RESTRICTED_COMMAND_KINDS = frozenset(("preview_close", "review", "close"))
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
_COMMAND_KEYS = {
    "connect": frozenset(("schema_version", "request_id", "kind")),
    "list_projects": frozenset(("schema_version", "request_id", "kind", "cursor")),
    "list_tasks": frozenset(("schema_version", "request_id", "kind", "cursor")),
    "refresh_project": frozenset(("schema_version", "request_id", "kind", "project_id")),
    "refresh_task": frozenset(("schema_version", "request_id", "kind", "task_id")),
    "preview_open": frozenset(("schema_version", "request_id", "kind", "source", "open_key")),
    "preview_refresh": frozenset(("schema_version", "request_id", "kind", "checkout_id")),
    "preview_close": frozenset(
        (
            "schema_version",
            "request_id",
            "kind",
            "checkout_id",
            "document_absent",
        )
    ),
    "review": frozenset(
        (
            "schema_version",
            "request_id",
            "kind",
            "decision",
            "task_id",
            "draft_id",
            "expected_generation",
        )
    ),
    "close": frozenset(("schema_version", "request_id", "kind")),
}


def _invalid_command() -> ValueError:
    return ValueError("invalid gateway command")


def _detach(value: object, *, depth: int = 0, budget: list[int] | None = None) -> object:
    if budget is None:
        budget = [_MAX_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > _MAX_DEPTH:
        raise _invalid_command()
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise _invalid_command()
        return value
    if type(value) is str:
        if len(value) > _MAX_STRING:
            raise _invalid_command()
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _invalid_command()
        return value
    if type(value) is list:
        if len(value) > _MAX_CONTAINER:
            raise _invalid_command()
        return [_detach(item, depth=depth + 1, budget=budget) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_CONTAINER:
            raise _invalid_command()
        if any(type(key) is not str or len(key) > _MAX_KEY for key in value):
            raise _invalid_command()
        return {key: _detach(item, depth=depth + 1, budget=budget) for key, item in value.items()}
    raise _invalid_command()


def _freeze_wire(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType({key: _freeze_wire(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_wire(item) for item in value)
    return value


def _plain_wire(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_wire(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_plain_wire(item) for item in value]
    return value


class _WireMapping(Mapping[str, object]):
    payload: Mapping[str, object]
    capability: object

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)

    def __eq__(self, other: object) -> bool:
        if type(other) is dict:
            return _plain_wire(self.payload) == other
        if type(other) is type(self):
            return (
                _plain_wire(self.payload) == _plain_wire(other.payload)
                and self.capability is other.capability
            )
        if isinstance(other, Mapping):
            return _plain_wire(self.payload) == _plain_wire(other)
        return False

    __hash__ = None

    def _detach_payload(self) -> None:
        detached = _detach(self.payload)
        if type(detached) is not dict:
            raise _invalid_command()
        frozen = _freeze_wire(detached)
        assert isinstance(frozen, Mapping)
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True, eq=False)
class _PrivateWireCommand(_WireMapping):
    payload: Mapping[str, object]
    capability: object

    def __post_init__(self) -> None:
        self._detach_payload()


@dataclass(frozen=True, slots=True, eq=False)
class _PrivateWireEvent(_WireMapping):
    payload: Mapping[str, object]
    capability: object

    def __post_init__(self) -> None:
        self._detach_payload()


def _identifier(value: object, prefix: str) -> str:
    if (
        type(value) is not str
        or len(value) != len(prefix) + 32
        or not value.startswith(prefix)
        or any(character not in "0123456789abcdef" for character in value[len(prefix) :])
    ):
        raise _invalid_command()
    return value


def _request_id(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
        raise _invalid_command()
    return value


def _cursor(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value or len(value) > _MAX_STRING:
        raise _invalid_command()
    return value


def _preview_source(value: object) -> None:
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise _invalid_command()
    if value.get("kind") == "head":
        if set(value) != {"kind", "project_id"}:
            raise _invalid_command()
        _identifier(value["project_id"], _PROJECT_ID_PREFIX)
        return
    if value.get("kind") == "draft":
        if set(value) != {
            "kind",
            "task_id",
            "draft_id",
            "expected_generation",
        }:
            raise _invalid_command()
        _identifier(value["task_id"], _TASK_ID_PREFIX)
        _identifier(value["draft_id"], _DRAFT_ID_PREFIX)
        generation = value["expected_generation"]
        if type(generation) is not int or not 0 <= generation <= _MAX_SAFE_INTEGER:
            raise _invalid_command()
        return
    raise _invalid_command()


def _validate_command(command: object) -> dict[str, object]:
    copied = _detach(command)
    if type(copied) is not dict:
        raise _invalid_command()
    kind = copied.get("kind")
    if type(kind) is not str or kind not in _COMMAND_KEYS:
        raise _invalid_command()
    if (
        set(copied) != _COMMAND_KEYS[kind]
        or type(copied.get("schema_version")) is not int
        or copied.get("schema_version") != 1
    ):
        raise _invalid_command()
    _request_id(copied["request_id"])
    if kind in {"list_projects", "list_tasks"}:
        _cursor(copied["cursor"])
    if kind == "refresh_project":
        _identifier(copied["project_id"], _PROJECT_ID_PREFIX)
    if kind in {"refresh_task", "review"}:
        _identifier(copied["task_id"], _TASK_ID_PREFIX)
    if kind == "preview_open":
        _preview_source(copied["source"])
        _identifier(copied["open_key"], _OPEN_KEY_PREFIX)
    if kind in {"preview_refresh", "preview_close"}:
        _identifier(copied["checkout_id"], _CHECKOUT_ID_PREFIX)
    if kind == "preview_close" and copied["document_absent"] is not True:
        raise _invalid_command()
    if kind == "review":
        if copied["decision"] not in ("accept", "reject"):
            raise _invalid_command()
        _identifier(copied["draft_id"], _DRAFT_ID_PREFIX)
        generation = copied["expected_generation"]
        if type(generation) is not int or not 0 <= generation <= _MAX_SAFE_INTEGER:
            raise _invalid_command()
    return copied


def _recover(command: object) -> tuple[int, str]:
    if type(command) is dict:
        request_id = command.get("request_id")
        kind = command.get("kind")
        if (
            type(request_id) is int
            and 0 <= request_id <= _MAX_SAFE_INTEGER
            and type(kind) is str
            and kind in _COMMAND_KEYS
        ):
            return request_id, kind
    return -1, "invalid"


def _event(kind: str, request_id: int, **payload: object) -> dict[str, object]:
    event = _detach(
        {
            "schema_version": 1,
            "request_id": request_id,
            "kind": kind,
            **payload,
        }
    )
    assert type(event) is dict
    return event


def _error(
    request_id: int,
    operation: str,
    code: str,
    outcome: str = "known_failure",
) -> dict[str, object]:
    assert code in _ERROR_CODES
    assert outcome in ("known_failure", "unknown_outcome")
    return {
        "schema_version": 1,
        "request_id": request_id,
        "kind": "error",
        "operation": operation,
        "code": code,
        "outcome": outcome,
    }


def _known_error_code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    if isinstance(code, Enum) and type(code.value) is str and code.value in _ERROR_CODES:
        return code.value
    return "internal_error"


@dataclass(slots=True)
class _WorkerCheckout:
    checkout_id: str
    source: dict[str, object]
    open_key: str
    descriptor: dict[str, object] | None
    phase: str


@dataclass(slots=True)
class _RequestReplay:
    command: dict[str, object] | None
    event: dict[str, object]
    authenticated: bool


class KernelGateway:
    def __init__(
        self,
        client_factory: Callable[[], object] | None = None,
        wire_capability: object | None = None,
    ) -> None:
        self._client_factory = client_factory
        self._client: object | None = None
        self._owner_thread_id: int | None = None
        self._wire_capability = (
            _UNBOUND_WIRE_CAPABILITY if wire_capability is None else wire_capability
        )
        self._handled_command = False
        self._closed = False
        self._client_construction_count = 0
        self._checkouts: dict[str, _WorkerCheckout] = {}
        self._sticky_recovery = False
        self._open_uncertain = False
        self._client_close_failed = False
        self._replays: dict[int, _RequestReplay] = {}
        self._request_highwater = -1

    def _bind_wire_capability(self, capability: object) -> None:
        if (
            capability is None
            or self._handled_command
            or self._wire_capability is not _UNBOUND_WIRE_CAPABILITY
        ):
            raise RuntimeError("gateway wire capability is already bound")
        self._wire_capability = capability

    @property
    def owner_thread_id(self) -> int | None:
        return self._owner_thread_id

    @property
    def client_construction_count(self) -> int:
        return self._client_construction_count

    def _claim_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread_id is None:
            self._owner_thread_id = current
        elif self._owner_thread_id != current:
            raise RuntimeError("gateway thread authority violation")

    def _open_client(self) -> object:
        if self._client is not None:
            return self._client
        if self._closed:
            raise RuntimeError("gateway is closed")
        factory = self._client_factory
        if factory is None:
            from vibecad.daemon import LocalAgentClient

            factory = LocalAgentClient.open
        client = factory()
        self._client = client
        self._client_construction_count += 1
        return client

    def _close_client(self) -> None:
        client = self._client
        if client is not None:
            client.close()
            self._client = None

    def _retain_acquisition_failure(self, error: BaseException) -> None:
        if not getattr(error, "recovery_required", False):
            return
        checkout_id = getattr(error, "checkout_id", None)
        source = getattr(error, "source", None)
        open_key = getattr(error, "open_key", None)
        descriptor = getattr(error, "descriptor", None)
        if (
            type(checkout_id) is not str
            or type(source) is not dict
            or type(open_key) is not str
            or (descriptor is not None and type(descriptor) is not dict)
            or checkout_id in self._checkouts
            or len(self._checkouts) >= _MAX_CHECKOUT_AUTHORITIES
        ):
            self._sticky_recovery = True
            return
        self._checkouts[checkout_id] = _WorkerCheckout(
            checkout_id=checkout_id,
            source=source,
            open_key=open_key,
            descriptor=descriptor,
            phase=(
                "close_uncertain"
                if getattr(error, "cleanup_error", None) is not None
                else "recovery"
            ),
        )

    def _retain_acquired(self, response: dict[str, object]) -> None:
        descriptor = response["descriptor"]
        source = response["source"]
        open_key = response["open_key"]
        assert type(descriptor) is dict
        assert type(source) is dict
        assert type(open_key) is str
        checkout_id = descriptor["checkout_id"]
        assert type(checkout_id) is str
        if checkout_id in self._checkouts or len(self._checkouts) >= _MAX_CHECKOUT_AUTHORITIES:
            self._sticky_recovery = True
            raise RuntimeError("duplicate worker checkout authority")
        self._checkouts[checkout_id] = _WorkerCheckout(
            checkout_id=checkout_id,
            source=source,
            open_key=open_key,
            descriptor=descriptor,
            phase="offered",
        )

    @staticmethod
    def _close_checkout(
        client: object,
        record: _WorkerCheckout,
    ) -> dict[str, object]:
        from .preview import _validate_closed_descriptor

        response = client.close_checkout(checkout_id=record.checkout_id)
        return _validate_closed_descriptor(
            response,
            checkout_id=record.checkout_id,
            source=record.source,
            open_key=record.open_key,
            descriptor=record.descriptor,
        )

    @staticmethod
    def _reconcile_checkout(
        client: object,
        record: _WorkerCheckout,
    ) -> tuple[str, dict[str, object]]:
        from .preview import PreviewError, _descriptor, _validate_closed_descriptor

        response = client.get_checkout(checkout_id=record.checkout_id)
        try:
            closed = _validate_closed_descriptor(
                response,
                checkout_id=record.checkout_id,
                source=record.source,
                open_key=record.open_key,
                descriptor=record.descriptor,
            )
        except PreviewError:
            opened = _descriptor(
                response,
                requested=record.source,
                open_key=record.open_key,
            )
            if (
                opened["checkout_id"] != record.checkout_id
                or opened["state"] != "open"
                or record.descriptor is None
                or opened != record.descriptor
            ):
                raise
            return "open", opened
        return "closed", closed

    def _handle_checkout_close(
        self,
        client: object,
        record: _WorkerCheckout,
        request_id: int,
    ) -> dict[str, object]:
        if record.phase == "close_uncertain":
            try:
                state, response = self._reconcile_checkout(client, record)
            except BaseException as error:
                return _error(
                    request_id,
                    "preview_close",
                    _known_error_code(error),
                    "unknown_outcome",
                )
            if state == "closed":
                del self._checkouts[record.checkout_id]
                return _event("preview_closed", request_id, response=response)
            record.phase = "offered"
        try:
            response = self._close_checkout(client, record)
        except BaseException as error:
            record.phase = "close_uncertain"
            return _error(
                request_id,
                "preview_close",
                _known_error_code(error),
                "unknown_outcome",
            )
        del self._checkouts[record.checkout_id]
        return _event("preview_closed", request_id, response=response)

    def _handle_close(self, request_id: int) -> dict[str, object]:
        if self._closed:
            return _event("closed", request_id)
        if self._client_close_failed or self._open_uncertain:
            return _error(
                request_id,
                "close",
                "internal_error",
                "unknown_outcome",
            )
        client = self._client
        for checkout_id, record in tuple(self._checkouts.items()):
            if record.phase not in {"recovery", "close_uncertain"}:
                return _error(request_id, "close", "internal_error")
            if client is None:
                return _error(request_id, "close", "unavailable")
            result = self._handle_checkout_close(client, record, request_id)
            if result["kind"] == "error":
                return _error(
                    request_id,
                    "close",
                    str(result["code"]),
                    str(result["outcome"]),
                )
            assert checkout_id not in self._checkouts
        if self._checkouts or self._sticky_recovery:
            return _error(request_id, "close", "internal_error")
        try:
            self._close_client()
        except BaseException as error:
            self._client_close_failed = True
            return _error(
                request_id,
                "close",
                _known_error_code(error),
                "unknown_outcome",
            )
        self._closed = True
        return _event("closed", request_id)

    def _wire_event(
        self,
        event: dict[str, object],
        *,
        authenticated: bool,
    ) -> dict[str, object] | _PrivateWireEvent:
        if not authenticated:
            return event
        capability = self._wire_capability
        assert capability is not _UNBOUND_WIRE_CAPABILITY
        return _PrivateWireEvent(event, capability)

    def _store_replay(
        self,
        request_id: int,
        *,
        command: dict[str, object] | None,
        event: dict[str, object],
        authenticated: bool,
    ) -> None:
        self._replays[request_id] = _RequestReplay(
            command=command,
            event=event,
            authenticated=authenticated,
        )
        while len(self._replays) > _REPLAY_CAPACITY:
            del self._replays[next(iter(self._replays))]

    def handle(
        self,
        command: object,
    ) -> dict[str, object] | _PrivateWireEvent:
        self._claim_thread()
        self._handled_command = True
        authenticated = (
            type(command) is _PrivateWireCommand
            and self._wire_capability is not _UNBOUND_WIRE_CAPABILITY
            and command.capability is self._wire_capability
        )
        if authenticated:
            candidate = _plain_wire(command.payload)
            assert type(candidate) is dict
        else:
            candidate = command
        request_id, operation = _recover(candidate)
        replay = self._replays.get(request_id)
        if replay is not None:
            try:
                data = _validate_command(candidate)
            except (TypeError, ValueError):
                data = None
            representation_valid = (
                type(command) is dict and operation in _PUBLIC_COMMAND_KINDS
            ) or authenticated
            if (
                data is not None
                and representation_valid
                and replay.command == data
                and replay.authenticated is authenticated
            ):
                repeated = _detach(replay.event)
                assert type(repeated) is dict
                return self._wire_event(
                    repeated,
                    authenticated=authenticated,
                )
            conflict = _error(request_id, operation, "invalid_input")
            return self._wire_event(
                conflict,
                authenticated=authenticated,
            )
        if request_id >= 0:
            if request_id <= self._request_highwater:
                stale = _error(request_id, operation, "invalid_input")
                return self._wire_event(
                    stale,
                    authenticated=authenticated,
                )
            self._request_highwater = request_id
        try:
            data = _validate_command(candidate)
        except (TypeError, ValueError):
            invalid = _error(request_id, operation, "invalid_input")
            if request_id >= 0:
                self._store_replay(
                    request_id,
                    command=None,
                    event=invalid,
                    authenticated=authenticated,
                )
            return self._wire_event(
                invalid,
                authenticated=authenticated,
            )
        kind = data["kind"]
        request_id = data["request_id"]
        assert type(kind) is str
        assert type(request_id) is int
        representation_valid = (
            type(command) is dict and kind in _PUBLIC_COMMAND_KINDS
        ) or authenticated
        if not representation_valid:
            invalid = _error(request_id, kind, "invalid_input")
            self._store_replay(
                request_id,
                command=None,
                event=invalid,
                authenticated=authenticated,
            )
            return self._wire_event(
                invalid,
                authenticated=authenticated,
            )
        event = self._handle_validated(data)
        stored_command = _detach(data)
        stored_event = _detach(event)
        assert type(stored_command) is dict
        assert type(stored_event) is dict
        self._store_replay(
            request_id,
            command=stored_command,
            event=stored_event,
            authenticated=authenticated,
        )
        return self._wire_event(
            event,
            authenticated=authenticated,
        )

    def _handle_validated(self, data: dict[str, object]) -> dict[str, object]:
        kind = data["kind"]
        request_id = data["request_id"]
        assert type(kind) is str
        assert type(request_id) is int
        if kind == "close":
            return self._handle_close(request_id)
        if self._closed:
            return _error(request_id, kind, "closed")
        if kind != "connect" and self._client is None:
            return _error(request_id, kind, "unavailable")
        if kind == "preview_open" and (self._open_uncertain or self._sticky_recovery):
            return _error(
                request_id,
                kind,
                "internal_error",
                "unknown_outcome",
            )
        if kind == "preview_open" and len(self._checkouts) >= _MAX_CHECKOUT_AUTHORITIES:
            return _error(
                request_id,
                kind,
                "internal_error",
            )
        try:
            client = self._open_client()
            if kind == "connect":
                client.ping()
                daemon_id = _identifier(client.daemon_id, _DAEMON_ID_PREFIX)
                return _event(
                    "connected",
                    request_id,
                    daemon_id=daemon_id,
                    worker_thread_id=threading.get_ident(),
                )
            if kind == "list_projects":
                response = client.list_projects_request(
                    {
                        "schema_version": 1,
                        "limit": 50,
                        "cursor": data["cursor"],
                    }
                )
                return _event("projects", request_id, response=response)
            if kind == "list_tasks":
                response = client.list_tasks_request(
                    {
                        "schema_version": 1,
                        "limit": 50,
                        "cursor": data["cursor"],
                    }
                )
                return _event("tasks", request_id, response=response)
            if kind == "refresh_project":
                response = client.get_project_request(
                    {
                        "schema_version": 1,
                        "project_id": data["project_id"],
                    }
                )
                return _event("project", request_id, response=response)
            if kind == "refresh_task":
                response = client.get_task_request(
                    {"schema_version": 1, "task_id": data["task_id"]}
                )
                return _event("task", request_id, response=response)
            if kind == "preview_open":
                from .preview import PreviewCoordinator, PreviewError

                try:
                    response = PreviewCoordinator.acquire(
                        client,
                        source=data["source"],
                        open_key=data["open_key"],
                    )
                except PreviewError as error:
                    self._retain_acquisition_failure(error)
                    return _error(
                        request_id,
                        kind,
                        _known_error_code(error),
                        ("unknown_outcome" if error.recovery_required else "known_failure"),
                    )
                except BaseException as error:
                    self._open_uncertain = True
                    self._sticky_recovery = True
                    return _error(
                        request_id,
                        kind,
                        _known_error_code(error),
                        "unknown_outcome",
                    )
                self._retain_acquired(response)
                return _event("preview_opened", request_id, response=response)
            if kind == "preview_refresh":
                if data["checkout_id"] not in self._checkouts:
                    raise RuntimeError("unknown worker checkout authority")
                response = client.get_checkout(checkout_id=data["checkout_id"])
                return _event("preview_refreshed", request_id, response=response)
            if kind == "preview_close":
                checkout_id = data["checkout_id"]
                record = self._checkouts.get(checkout_id)
                if record is None:
                    raise RuntimeError("unknown worker checkout authority")
                return self._handle_checkout_close(client, record, request_id)
            assert kind == "review"
            method_name = (
                "accept_draft_request" if data["decision"] == "accept" else "reject_draft_request"
            )
            response = getattr(client, method_name)(
                {
                    "schema_version": 1,
                    "task_id": data["task_id"],
                    "draft_id": data["draft_id"],
                    "expected_generation": data["expected_generation"],
                }
            )
            return _event("review", request_id, response=response)
        except BaseException as error:
            if kind == "review":
                if self._checkouts or self._sticky_recovery:
                    self._sticky_recovery = True
                    return _error(
                        request_id,
                        kind,
                        _known_error_code(error),
                        "unknown_outcome",
                    )
                try:
                    self._close_client()
                except BaseException:
                    self._client_close_failed = True
                else:
                    self._closed = True
                return _error(request_id, kind, "closed", "unknown_outcome")
            return _error(request_id, kind, _known_error_code(error))
