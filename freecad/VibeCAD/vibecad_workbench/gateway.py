from __future__ import annotations

import math
import threading
from collections.abc import Callable
from contextlib import suppress
from enum import Enum

__all__ = ("KernelGateway",)

_MAX_DEPTH = 8
_MAX_NODES = 10_000
_MAX_STRING = 4096
_MAX_KEY = 128
_MAX_CONTAINER = 1000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_PROJECT_ID_PREFIX = "project_"
_TASK_ID_PREFIX = "task_"
_DRAFT_ID_PREFIX = "draft_"
_DAEMON_ID_PREFIX = "daemon_"
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


class KernelGateway:
    def __init__(self, client_factory: Callable[[], object] | None = None) -> None:
        self._client_factory = client_factory
        self._client: object | None = None
        self._owner_thread_id: int | None = None
        self._closed = False
        self._client_construction_count = 0

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
        client, self._client = self._client, None
        if client is not None:
            client.close()

    def handle(self, command: object) -> dict[str, object]:
        self._claim_thread()
        try:
            data = _validate_command(command)
        except (TypeError, ValueError):
            request_id, operation = _recover(command)
            return _error(request_id, operation, "invalid_input")
        kind = data["kind"]
        request_id = data["request_id"]
        assert type(kind) is str
        assert type(request_id) is int
        if kind == "close":
            try:
                self._close_client()
            except BaseException:
                pass
            self._closed = True
            return _event("closed", request_id)
        if self._closed:
            return _error(request_id, kind, "closed")
        if kind != "connect" and self._client is None:
            return _error(request_id, kind, "unavailable")
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
                with suppress(BaseException):
                    self._close_client()
                self._closed = True
                return _error(request_id, kind, "closed", "unknown_outcome")
            return _error(request_id, kind, _known_error_code(error))
