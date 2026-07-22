"""Atomic Agent MCP surface over the pinned low-level SDK server.

Discovery is deliberately inert.  The concrete application composition root is
imported and opened only after a domain request passes both public-schema
validation and the managed-runtime guard.
"""

from __future__ import annotations

import atexit
import importlib
import json
import logging
import math
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping

import anyio
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from vibecad import __version__, mcp_transport
from vibecad import freecad_env as _freecad_env
from vibecad.application.public_surface import public_tool_specs
from vibecad.runtime import paths, status
from vibecad.runtime import uninstall as _uninstall
from vibecad.runtime.installer import RuntimeInstaller
from vibecad.supervisor import runtime_swappable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_prepare_freecad_import = _freecad_env.prepare_freecad_import
_silence_fd1 = _freecad_env.silence_fd1

_SCHEMA_VERSION = 1
_TOOL_REQUEST_INVALID = (-32602, "Tool request is invalid.")
_TOOL_NAME_UNAVAILABLE = (-32602, "Tool name is not available.")
_TOOL_INTERNAL_ERROR = (-32603, "Tool request could not be completed.")
_RESOURCE_INVALID_IDENTIFIER = (-32602, "Artifact resource identifier is invalid.")
_RESOURCE_UNAVAILABLE = (-32002, "Artifact resource is unavailable.")
_RESOURCE_READ_LIMIT = (-32001, "Artifact resource exceeds the read limit.")
_RESOURCE_RUNTIME_UNAVAILABLE = (-32004, "The managed CAD runtime is not active.")
_RESOURCE_INTERNAL_ERROR = (-32603, "Artifact resource could not be read.")

_ERROR_MESSAGES = {
    "missing_field": "A required request field is missing.",
    "unknown_field": "The request contains an unknown field.",
    "unsupported_version": "The request schema version is not supported.",
    "invalid_type": "A request value has an invalid type.",
    "invalid_value": "A request value is invalid.",
    "budget_exceeded": "The request exceeds a resource budget.",
    "runtime_failure": "The runtime operation failed.",
    "store_failure": "The runtime state operation failed.",
    "recovery_required": "The runtime operation requires recovery.",
    "runtime_unavailable": "The managed CAD runtime is not active.",
    "internal_error": "The request could not be completed.",
}

_RESOURCE_TEMPLATE = "vibecad://artifact/{materialization_id}/{artifact_id}"
_RESOURCE_URI = re.compile(
    r"^vibecad://artifact/materialization_[0-9a-f]{64}/artifact_[0-9a-f]{32}$",
    re.ASCII,
)
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$", re.ASCII)
_MAX_JSON_DEPTH = 64
_MAX_JSON_NODES = 65_536
_MAX_JSON_KEY_BYTES = 256
_MAX_JSON_STRING_BYTES = 1_048_576
_MAX_TOOL_ARGUMENT_BYTES = 2_097_152
_MAX_TOOL_RESULT_BYTES = 100_663_296
_MAX_SAFE_JSON_INTEGER = 9_007_199_254_740_991


class _DiscardOnlyHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        del record


class _SdkPathFilter(logging.Filter):
    _vibecad_sdk_path_filter = True

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            path = os.path.normcase(os.path.realpath(record.pathname))
            components = path.replace("\\", "/").split("/")
        except BaseException:
            return False
        return "mcp" not in components


def _silence_sdk_namespace() -> None:
    logger = logging.getLogger("mcp")
    logger.handlers[:] = [_DiscardOnlyHandler()]
    logger.propagate = False
    logger.disabled = False
    for name, child in logging.root.manager.loggerDict.items():
        if name.startswith("mcp.") and isinstance(child, logging.Logger):
            child.handlers.clear()
            child.propagate = True
            child.disabled = True
    root = logging.getLogger()
    path_filter = next(
        (item for item in root.filters if getattr(item, "_vibecad_sdk_path_filter", False) is True),
        None,
    )
    if path_filter is None:
        path_filter = _SdkPathFilter()
        root.addFilter(path_filter)
    for handler in root.handlers:
        if not any(
            getattr(item, "_vibecad_sdk_path_filter", False) is True for item in handler.filters
        ):
            handler.addFilter(path_filter)


mcp = FastMCP("vibecad")
mcp._mcp_server.version = __version__
_silence_sdk_namespace()


def _thaw_json(value: object) -> object:
    """Recursively copy frozen public metadata into SDK-serializable JSON values."""

    if value is None or type(value) in {str, int, float, bool}:
        return value
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) in {tuple, list}:
        return [_thaw_json(item) for item in value]
    raise TypeError("public metadata contains a non-JSON value")


def _validation_schema(value: object) -> object:
    """Copy a public schema while making anchored Python regexes truly terminal."""

    thawed = _thaw_json(value)

    def harden(current: object) -> object:
        if type(current) is list:
            return [harden(item) for item in current]
        if type(current) is dict:
            result = {key: harden(item) for key, item in current.items()}
            pattern = result.get("pattern")
            if type(pattern) is str and pattern.startswith("^") and pattern.endswith("$"):
                result["pattern"] = pattern[:-1] + r"\Z"
            return result
        return current

    return harden(thawed)


_PUBLIC_SPECS = public_tool_specs()
_SPEC_BY_NAME = {item.name: item for item in _PUBLIC_SPECS}
_PUBLIC_TOOLS = tuple(
    types.Tool(
        name=item.name,
        description=item.description,
        inputSchema=_thaw_json(item.input_schema),
        annotations=types.ToolAnnotations(
            readOnlyHint=item.annotations.read_only,
            destructiveHint=item.annotations.destructive,
            idempotentHint=item.annotations.idempotent,
            openWorldHint=item.annotations.open_world,
        ),
    )
    for item in _PUBLIC_SPECS
)
_INPUT_SCHEMAS = {item.name: _validation_schema(item.input_schema) for item in _PUBLIC_SPECS}
_OUTPUT_SCHEMAS = {item.name: _validation_schema(item.output_schema) for item in _PUBLIC_SPECS}
_INPUT_VALIDATORS = {name: Draft202012Validator(schema) for name, schema in _INPUT_SCHEMAS.items()}
_OUTPUT_VALIDATORS = {
    name: Draft202012Validator(schema) for name, schema in _OUTPUT_SCHEMAS.items()
}

_STABLE_DOMAIN_FACADES = {
    "create_project": "create_project_request",
    "get_project": "get_project_request",
    "create_task": "create_task_request",
    "get_task": "get_task_request",
    "submit_model_program": "submit_model_program_request",
    "resume_task": "resume_task_request",
    "accept_draft": "accept_draft_request",
    "reject_draft": "reject_draft_request",
    "export_task_artifacts": "export_task_artifacts_request",
}
_CONTROL_NAMES = frozenset(
    {
        "ping",
        "get_runtime_status",
        "ensure_runtime",
        "uninstall_runtime",
        "get_capabilities",
    }
)
_DIRECT_NAMES = frozenset(_SPEC_BY_NAME) - _CONTROL_NAMES - frozenset(_STABLE_DOMAIN_FACADES)

_APPLICATION_METHODS = (
    "close",
    "create_project_request",
    "get_project_request",
    "create_task_request",
    "get_task_request",
    "submit_model_program_request",
    "resume_task_request",
    "accept_draft_request",
    "reject_draft_request",
    "export_task_artifacts_request",
    "invoke_direct_operation_request",
    "read_artifact_resource",
)


class _ApplicationSlot:
    """One PID-bound, single-flight application composition slot."""

    def __init__(self, opener: Callable[[], object]) -> None:
        if not callable(opener):
            raise TypeError("application opener must be callable")
        self._opener = opener
        self._condition = threading.Condition()
        self._pid = os.getpid()
        self._state = "UNOPENED"
        self._application: object | None = None
        self._generation = 0
        self._failed_generation = -1
        self._clean_close = True

    @property
    def state(self) -> str:
        if os.getpid() != self._pid:
            return "CLOSED"
        with self._condition:
            return self._state

    def _check_pid(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeError("application slot is unavailable in this process")

    @staticmethod
    def _close_candidate(candidate: object) -> bool:
        try:
            close = getattr(candidate, "close", None)
            if not callable(close):
                return False
            close()
        except BaseException:
            return False
        return True

    @staticmethod
    def _valid(candidate: object) -> bool:
        return all(callable(getattr(candidate, name, None)) for name in _APPLICATION_METHODS)

    def get(self) -> object:
        self._check_pid()
        waited_for: int | None = None
        with self._condition:
            while True:
                self._check_pid()
                if self._state == "READY":
                    assert self._application is not None
                    return self._application
                if self._state in {"CLOSING", "CLOSED"}:
                    raise RuntimeError("application slot is closed")
                if self._state == "OPENING":
                    if waited_for is None:
                        waited_for = self._generation
                    self._condition.wait()
                    if self._failed_generation == waited_for:
                        raise RuntimeError("application open failed")
                    continue
                self._state = "OPENING"
                self._generation += 1
                generation = self._generation
                break

        candidate: object | None = None
        cleanup_failed = False
        try:
            candidate = self._opener()
            try:
                valid = self._valid(candidate)
            except BaseException:
                cleanup_failed = candidate is not None and not self._close_candidate(candidate)
                raise RuntimeError("application open failed") from None
            if not valid:
                if candidate is not None:
                    cleanup_failed = not self._close_candidate(candidate)
                raise RuntimeError("application open failed")
        except BaseException:
            with self._condition:
                if self._state == "OPENING" and self._generation == generation:
                    self._failed_generation = generation
                    if cleanup_failed:
                        self._clean_close = False
                        self._state = "CLOSED"
                    else:
                        self._state = "UNOPENED"
                    self._condition.notify_all()
            raise RuntimeError("application open failed") from None

        with self._condition:
            if self._state != "OPENING" or self._generation != generation:
                self._close_candidate(candidate)
                raise RuntimeError("application slot is closed")
            self._application = candidate
            self._state = "READY"
            self._condition.notify_all()
            return candidate

    def close(self) -> bool:
        self._check_pid()
        with self._condition:
            while self._state == "OPENING":
                self._condition.wait()
            if self._state == "CLOSED":
                return self._clean_close
            if self._state == "CLOSING":
                while self._state == "CLOSING":
                    self._condition.wait()
                return self._state == "CLOSED" and self._clean_close
            candidate = self._application
            self._application = None
            self._state = "CLOSING"
            self._condition.notify_all()

        closed = candidate is None or self._close_candidate(candidate)
        with self._condition:
            self._clean_close = self._clean_close and closed
            self._state = "CLOSED"
            self._condition.notify_all()
        return closed


_application_effect_entered = threading.Event()
_runtime_transition_lock = threading.Lock()


def _open_agent_application() -> object:
    if not _enter_application_effect():
        raise RuntimeError("application admission is closed")
    from vibecad.application.agent import AgentApplication

    return AgentApplication.open(data_root=paths.data_root())


def _initialize_application_process_runtime() -> None:
    """Initialize main-thread-only application policy before owned workers exist."""

    from vibecad.execution.revisions import _initialize_candidate_file_limit_runtime

    _initialize_candidate_file_limit_runtime()


_application_slot = _ApplicationSlot(_open_agent_application)


def _close_application_at_exit() -> None:
    try:
        _application_slot.close()
    except BaseException:
        pass


atexit.register(_close_application_at_exit)

_installer = RuntimeInstaller()
_install_thread: threading.Thread | None = None
_install_lock = threading.Lock()
_active_owned_runner: mcp_transport.OwnedStdioRunner | None = None
_active_owned_runner_lock = threading.Lock()


def _in_conda_runtime() -> bool:
    try:
        return os.path.realpath(sys.executable) == os.path.realpath(paths.active_runtime_python())
    except OSError:
        return False


def _supervised() -> bool:
    return os.environ.get("VIBECAD_SUPERVISED") == "1"


def _try_schedule_swap(*, uninstall: bool = False) -> bool:
    if not _supervised() or (not uninstall and not runtime_swappable()):
        return False
    with _runtime_transition_lock:
        if not uninstall and _application_effect_entered.is_set():
            return False
        runner = _active_owned_runner
        if runner is not None:
            return runner.request_uninstall_exit() if uninstall else runner.request_swap()
        return False


def _enter_application_effect() -> bool:
    """Linearize application entry against a normal runtime-ready swap."""

    with _runtime_transition_lock:
        runner = _active_owned_runner
        if runner is not None and not runner.lifecycle.application_may_enter:
            return False
        _application_effect_entered.set()
        return True


def _safe_install() -> None:
    try:
        _installer.install()
    except Exception:
        return
    if not _in_conda_runtime():
        _try_schedule_swap()


def _spawn_install() -> None:
    global _install_thread
    with _install_lock:
        if _install_thread is not None and _install_thread.is_alive():
            return
        worker = threading.Thread(
            target=_safe_install,
            name="vibecad-install",
            daemon=True,
        )
        _install_thread = worker
        worker.start()


def ping() -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "service": "vibecad",
        "version": __version__,
    }


def _runtime_status_message(
    phase: status.Phase,
    recovery: status.RecoveryKind,
) -> str:
    if recovery is status.RecoveryKind.UPGRADE_REQUIRED:
        return "The CAD engine is reusable, but the server package requires an update."
    if recovery is status.RecoveryKind.REPAIR_REQUIRED and phase is status.Phase.READY:
        return "The managed CAD runtime requires repair."
    return {
        status.Phase.NOT_STARTED: "The managed CAD runtime is not installed.",
        status.Phase.DOWNLOADING_MICROMAMBA: "The runtime installer is downloading its bootstrap.",
        status.Phase.CREATING_ENV: "The runtime installer is creating the CAD environment.",
        status.Phase.INSTALLING_PIP: "The runtime installer is synchronizing the server package.",
        status.Phase.VERIFYING: "The runtime installer is verifying the CAD environment.",
        status.Phase.READY: "The managed CAD runtime is ready.",
        status.Phase.FAILED: "The managed CAD runtime installation failed.",
    }[phase]


def get_runtime_status() -> dict[str, object]:
    current = status.read_status()
    phase = current.phase if type(current.phase) is status.Phase else status.Phase.NOT_STARTED
    percent = current.percent
    if type(percent) not in {int, float} or type(percent) is bool or not math.isfinite(percent):
        percent = 0.0
    percent = min(100.0, max(0.0, float(percent)))
    compatible = status.runtime_ready()
    recovery = status.RecoveryKind.READY if compatible else status.runtime_recovery_kind()
    receipt = status.read_runtime_receipt()
    installed = receipt.get("vibecad_version") if type(receipt) is dict else None
    if type(installed) is not str or _VERSION.fullmatch(installed) is None:
        installed = None
    needs_reconnect = False
    if compatible and not _in_conda_runtime():
        needs_reconnect = not _try_schedule_swap()
    return {
        "schema_version": _SCHEMA_VERSION,
        "phase": phase.value,
        "percent": percent,
        "message": _runtime_status_message(phase, recovery),
        "error": ("Runtime installation failed." if phase is status.Phase.FAILED else None),
        "runtime_compatible": compatible,
        "runtime_action": recovery.value,
        "installed_version": installed,
        "required_version": __version__,
        "needs_reconnect": needs_reconnect,
    }


def _ensure_runtime_impl() -> dict[str, object]:
    if _installer.is_ready():
        if not _in_conda_runtime():
            _try_schedule_swap()
        return {"status": "ready", "message": "The managed CAD runtime is ready."}
    with _install_lock:
        active = _install_thread is not None and _install_thread.is_alive()
    if active:
        return {
            "status": "in_progress",
            "message": "The managed CAD runtime installation is in progress.",
        }
    _spawn_install()
    return {
        "status": "started",
        "message": "The managed CAD runtime installation has started.",
    }


def _estimated_uninstall_bytes(preview: object) -> int:
    if type(preview) is not dict:
        return 0
    size = preview.get("size_mb")
    if type(size) not in {int, float} or type(size) is bool or not math.isfinite(size):
        return 0
    return min(_MAX_SAFE_JSON_INTEGER, max(0, int(float(size) * 1_000_000)))


def uninstall_runtime(confirm: bool = False) -> dict[str, object]:
    preview = _uninstall.preview_uninstall()
    if type(preview) is not dict or preview.get("ok") is not True:
        raise RuntimeError("runtime uninstall preview failed")
    estimated = _estimated_uninstall_bytes(preview)
    if not confirm:
        return {
            "schema_version": _SCHEMA_VERSION,
            "status": "preview",
            "confirm_required": True,
            "estimated_size_bytes": estimated,
            "data_preserved": True,
            "message": "Confirm to remove only the managed CAD runtime; durable data is preserved.",
        }
    requested = _uninstall.request_uninstall()
    if type(requested) is not dict or requested.get("ok") is not True:
        raise RuntimeError("runtime uninstall request failed")
    marked = requested.get("marked") is True
    already_clean = requested.get("already_clean") is True
    if not marked and not already_clean:
        raise RuntimeError("runtime uninstall response is invalid")
    runner = _active_owned_runner
    if runner is None:
        if not _application_slot.close():
            raise RuntimeError("application close failed")
        if marked:
            _try_schedule_swap(uninstall=True)
    elif marked and not _try_schedule_swap(uninstall=True):
        # The marker is durable at this point.  Keeping this process alive and
        # returning recovery_required is safer than closing the application or
        # claiming a swap which the owned transport cannot perform.
        runner.request_uninstall_recovery()
        raise RuntimeError("runtime uninstall requires recovery")
    return {
        "schema_version": _SCHEMA_VERSION,
        "status": "marked" if marked else "already_clean",
        "confirm_required": False,
        "estimated_size_bytes": estimated,
        "data_preserved": True,
        "message": (
            "The managed CAD runtime is marked for removal; durable data is preserved."
            if marked
            else "No managed CAD runtime remains; durable data is preserved."
        ),
    }


def _failure(code: str, path: str = "") -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": _SCHEMA_VERSION,
            "code": code,
            "path": path,
            "message": _ERROR_MESSAGES[code],
        },
    }


def _success(result: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "ok": True,
        "result": result,
        "error": None,
    }


def _runtime_unavailable() -> dict[str, object]:
    return _failure("runtime_unavailable")


def _application_runtime_guard() -> dict[str, object] | None:
    try:
        ready = _installer.is_ready()
        active = ready and _in_conda_runtime()
        if not active:
            if ready:
                _try_schedule_swap()
            return _runtime_unavailable()
    except BaseException:
        return _runtime_unavailable()
    return None


def _runtime_guard() -> dict[str, object] | None:
    """Compatibility seam for tests; public calls use the exact envelope guard."""

    guarded = _application_runtime_guard()
    if guarded is None:
        return None
    return {
        "ok": False,
        "phase": status.read_status().phase.value,
        "message": _ERROR_MESSAGES["runtime_unavailable"],
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_budget_failure(
    value: object,
    *,
    maximum_bytes: int = _MAX_TOOL_ARGUMENT_BYTES,
) -> str | None:
    nodes = 0
    seen: set[int] = set()
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            return "budget_exceeded"
        if current is None or type(current) is bool:
            continue
        if type(current) is int:
            if abs(current) > _MAX_SAFE_JSON_INTEGER:
                return "invalid_value"
            continue
        if type(current) is float:
            if not math.isfinite(current):
                return "invalid_value"
            continue
        if type(current) is str:
            try:
                if len(current.encode("utf-8")) > _MAX_JSON_STRING_BYTES:
                    return "budget_exceeded"
            except UnicodeError:
                return "invalid_value"
            continue
        if type(current) not in {dict, list}:
            return "invalid_type"
        identity = id(current)
        if identity in seen:
            return "invalid_value"
        seen.add(identity)
        if type(current) is dict:
            for key, item in current.items():
                if type(key) is not str:
                    return "invalid_type"
                try:
                    if len(key.encode("utf-8")) > _MAX_JSON_KEY_BYTES:
                        return "budget_exceeded"
                except UnicodeError:
                    return "invalid_value"
                stack.append((item, depth + 1))
        else:
            stack.extend((item, depth + 1) for item in current)
    try:
        if len(_canonical_json(value).encode("utf-8")) > maximum_bytes:
            return "budget_exceeded"
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return "invalid_value"
    return None


def _schema_utf8_budget_path(
    value: object,
    schema: object,
    path: tuple[object, ...] = (),
) -> tuple[object, ...] | None:
    if type(schema) is not dict:
        return None
    alternatives = schema.get("anyOf")
    if type(alternatives) is list:
        for alternative in alternatives:
            if type(alternative) is not dict:
                continue
            expected = alternative.get("type")
            if (
                (expected == "null" and value is None)
                or (expected == "string" and type(value) is str)
                or (expected == "object" and type(value) is dict)
                or (expected == "array" and type(value) is list)
            ):
                return _schema_utf8_budget_path(value, alternative, path)
        return None
    if type(value) is str:
        maximum = schema.get("maxLength")
        if type(maximum) is int:
            try:
                if len(value.encode("utf-8")) > maximum:
                    return path
            except UnicodeError:
                return path
        return None
    if type(value) is dict:
        properties = schema.get("properties")
        if type(properties) is not dict:
            return None
        for key, item in value.items():
            selected = properties.get(key)
            if selected is None:
                continue
            failed = _schema_utf8_budget_path(item, selected, (*path, key))
            if failed is not None:
                return failed
        return None
    if type(value) is list:
        items = schema.get("items")
        for index, item in enumerate(value):
            failed = _schema_utf8_budget_path(item, items, (*path, index))
            if failed is not None:
                return failed
    return None


def _pointer(parts: tuple[object, ...], final: str | None = None) -> str:
    tokens: list[str] = []
    for item in parts:
        if type(item) is int:
            tokens.append(str(item))
        elif type(item) is str and _VERSION.fullmatch(item) is not None:
            tokens.append(item)
        elif type(item) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", item):
            tokens.append(item)
        else:
            tokens.append("_unknown")
            break
    if final is not None:
        tokens.append(final)
    path = "".join("/" + token.replace("~", "~0").replace("/", "~1") for token in tokens)
    try:
        return path if len(path.encode("utf-8")) <= 256 else "/_truncated"
    except UnicodeError:
        return "/_truncated"


def _schema_failure(name: str, arguments: dict[str, object]) -> dict[str, object] | None:
    budget = _json_budget_failure(arguments)
    if budget is not None:
        return _failure(budget)
    errors = list(_INPUT_VALIDATORS[name].iter_errors(arguments))
    if not errors:
        utf8_failure = _schema_utf8_budget_path(arguments, _INPUT_SCHEMAS[name])
        if utf8_failure is None:
            return None
        return _failure("budget_exceeded", _pointer(utf8_failure))
    priority = {
        "required": 0,
        "additionalProperties": 1,
        "type": 2,
        "maxLength": 3,
        "maxItems": 3,
        "maxProperties": 3,
    }
    error = min(
        errors,
        key=lambda item: (priority.get(item.validator, 4), tuple(map(str, item.absolute_path))),
    )
    parts = tuple(error.absolute_path)
    if error.validator == "required":
        required = tuple(error.validator_value)
        instance = error.instance if type(error.instance) is dict else {}
        missing = next((field for field in required if field not in instance), None)
        return _failure("missing_field", _pointer(parts, missing))
    if error.validator == "additionalProperties":
        return _failure("unknown_field", _pointer(parts, "_unknown"))
    if error.validator == "type":
        return _failure("invalid_type", _pointer(parts))
    if error.validator in {"maxLength", "maxItems", "maxProperties"}:
        return _failure("budget_exceeded", _pointer(parts))
    if error.validator == "const" and parts == ("schema_version",):
        return _failure("unsupported_version", "/schema_version")
    return _failure("invalid_value", _pointer(parts))


def _mcp_error(error: tuple[int, str]) -> McpError:
    code, message = error
    return McpError(types.ErrorData(code=code, message=message))


def _export_resource_links(envelope: dict[str, object]) -> list[types.ResourceLink]:
    result = envelope.get("result")
    if type(result) is not dict:
        raise TypeError("export result is invalid")
    materialization_id = result.get("materialization_id")
    artifacts = result.get("artifacts")
    if type(materialization_id) is not str or type(artifacts) is not list or len(artifacts) != 2:
        raise TypeError("export artifacts are invalid")
    expected = (
        ("model.FCStd", "fcstd", "application/vnd.freecad.fcstd"),
        ("model.step", "step", "model/step"),
    )
    fields = {
        "schema_version",
        "id",
        "name",
        "format",
        "sha256",
        "size_bytes",
        "resource_uri",
    }
    links: list[types.ResourceLink] = []
    seen_uris: set[str] = set()
    for artifact, (expected_name, expected_format, mime_type) in zip(
        artifacts, expected, strict=True
    ):
        if type(artifact) is not dict or set(artifact) != fields:
            raise TypeError("export artifact is invalid")
        artifact_id = artifact["id"]
        name = artifact["name"]
        artifact_format = artifact["format"]
        size_bytes = artifact["size_bytes"]
        resource_uri = artifact["resource_uri"]
        if (
            artifact["schema_version"] != _SCHEMA_VERSION
            or type(artifact_id) is not str
            or type(name) is not str
            or name != expected_name
            or type(artifact_format) is not str
            or artifact_format != expected_format
            or type(size_bytes) is not int
            or size_bytes <= 0
            or type(resource_uri) is not str
            or resource_uri != f"vibecad://artifact/{materialization_id}/{artifact_id}"
            or _RESOURCE_URI.fullmatch(resource_uri) is None
            or resource_uri in seen_uris
        ):
            raise ValueError("export artifact is invalid")
        seen_uris.add(resource_uri)
        links.append(
            types.ResourceLink(
                type="resource_link",
                name=name,
                uri=resource_uri,
                mimeType=mime_type,
                size=size_bytes,
            )
        )
    return links


def _call_result(name: str, envelope: object) -> types.CallToolResult:
    try:
        if (
            type(envelope) is not dict
            or _json_budget_failure(
                envelope,
                maximum_bytes=_MAX_TOOL_RESULT_BYTES,
            )
            is not None
            or not _OUTPUT_VALIDATORS[name].is_valid(envelope)
        ):
            raise _mcp_error(_TOOL_INTERNAL_ERROR)
        text = _canonical_json(envelope)
        content: list[types.TextContent | types.ResourceLink] = [
            types.TextContent(type="text", text=text)
        ]
        if name == "export_task_artifacts" and envelope["ok"] is True:
            content.extend(_export_resource_links(envelope))
    except McpError:
        raise
    except BaseException:
        raise _mcp_error(_TOOL_INTERNAL_ERROR) from None
    return types.CallToolResult(
        content=content,
        structuredContent=envelope,
        isError=envelope["ok"] is not True,
    )


def _control_envelope(name: str, arguments: dict[str, object]) -> dict[str, object]:
    try:
        if name == "ping":
            return _success(ping())
        if name == "get_runtime_status":
            return _success(get_runtime_status())
        if name == "ensure_runtime":
            result = _ensure_runtime_impl()
            return _success({"schema_version": _SCHEMA_VERSION, **result})
        if name == "uninstall_runtime":
            return _success(uninstall_runtime(confirm=arguments["confirm"]))
        if name == "get_capabilities":
            from vibecad.application.task_api import TaskApi

            return TaskApi(port=object()).get_capabilities(arguments)
    except (OSError, RuntimeError, ValueError):
        return _failure("recovery_required" if name == "uninstall_runtime" else "runtime_failure")
    except BaseException:
        return _failure("internal_error")
    raise _mcp_error(_TOOL_INTERNAL_ERROR)


async def _handle_list_tools() -> types.ListToolsResult:
    return types.ListToolsResult(tools=list(_PUBLIC_TOOLS))


async def _handle_list_resources() -> types.ListResourcesResult:
    return types.ListResourcesResult(resources=[])


async def _handle_list_resource_templates() -> types.ListResourceTemplatesResult:
    return types.ListResourceTemplatesResult(
        resourceTemplates=[types.ResourceTemplate(name="artifact", uriTemplate=_RESOURCE_TEMPLATE)]
    )


async def _handle_call_tool(
    name: object,
    arguments: object,
) -> types.CallToolResult:
    if type(name) is not str:
        raise _mcp_error(_TOOL_REQUEST_INVALID)
    if name not in _SPEC_BY_NAME:
        raise _mcp_error(_TOOL_NAME_UNAVAILABLE)
    if arguments is None:
        arguments = {}
    if type(arguments) is not dict:
        raise _mcp_error(_TOOL_REQUEST_INVALID)
    invalid = _schema_failure(name, arguments)
    if invalid is not None:
        return _call_result(name, invalid)
    if name in _CONTROL_NAMES:
        return _call_result(name, _control_envelope(name, arguments))

    guarded = _application_runtime_guard()
    if guarded is not None:
        return _call_result(name, guarded)
    if not _enter_application_effect():
        return _call_result(name, _runtime_unavailable())
    try:
        application = _application_slot.get()
        if name in _STABLE_DOMAIN_FACADES:
            facade = getattr(application, _STABLE_DOMAIN_FACADES[name])
            envelope = facade(arguments)
        elif name in _DIRECT_NAMES:
            envelope = application.invoke_direct_operation_request(name, arguments)
        else:
            raise RuntimeError("unreachable public tool")
    except McpError:
        raise
    except BaseException:
        raise _mcp_error(_TOOL_INTERNAL_ERROR) from None
    return _call_result(name, envelope)


async def _handle_read_resource(uri: object) -> types.ReadResourceResult:
    if type(uri) is not str or _RESOURCE_URI.fullmatch(uri) is None:
        raise _mcp_error(_RESOURCE_INVALID_IDENTIFIER)
    guarded = _application_runtime_guard()
    if guarded is not None:
        raise _mcp_error(_RESOURCE_RUNTIME_UNAVAILABLE)
    if not _enter_application_effect():
        raise _mcp_error(_RESOURCE_RUNTIME_UNAVAILABLE)
    try:
        artifact_module = importlib.import_module("vibecad.application.artifacts")
        ArtifactResourceError = artifact_module.ArtifactResourceError
        ArtifactResourceErrorCode = artifact_module.ArtifactResourceErrorCode
        if not (
            isinstance(ArtifactResourceError, type)
            and issubclass(ArtifactResourceError, BaseException)
        ):
            raise TypeError("invalid artifact resource error type")
    except BaseException:
        raise _mcp_error(_RESOURCE_INTERNAL_ERROR) from None
    try:
        application = _application_slot.get()
        content = application.read_artifact_resource(uri)
    except ArtifactResourceError as error:
        mapped = {
            ArtifactResourceErrorCode.INVALID_IDENTIFIER: _RESOURCE_INVALID_IDENTIFIER,
            ArtifactResourceErrorCode.UNAVAILABLE: _RESOURCE_UNAVAILABLE,
            ArtifactResourceErrorCode.READ_LIMIT: _RESOURCE_READ_LIMIT,
            ArtifactResourceErrorCode.RUNTIME_UNAVAILABLE: _RESOURCE_RUNTIME_UNAVAILABLE,
            ArtifactResourceErrorCode.INTERNAL_ERROR: _RESOURCE_INTERNAL_ERROR,
        }.get(error.code, _RESOURCE_INTERNAL_ERROR)
        raise _mcp_error(mapped) from None
    except McpError:
        raise
    except BaseException:
        raise _mcp_error(_RESOURCE_INTERNAL_ERROR) from None
    try:
        if not (
            type(getattr(content, "uri", None)) is str
            and _RESOURCE_URI.fullmatch(content.uri) is not None
            and content.uri == uri
            and type(getattr(content, "blob", None)) is str
            and type(getattr(content, "mime_type", None)) is str
            and content.mime_type in {"application/vnd.freecad.fcstd", "model/step"}
        ):
            raise _mcp_error(_RESOURCE_INTERNAL_ERROR)
        return types.ReadResourceResult(
            contents=[
                types.BlobResourceContents(
                    uri=content.uri,
                    blob=content.blob,
                    mimeType=content.mime_type,
                )
            ]
        )
    except McpError:
        raise
    except BaseException:
        raise _mcp_error(_RESOURCE_INTERNAL_ERROR) from None


async def _sdk_list_tools(_request: types.ListToolsRequest) -> types.ServerResult:
    return types.ServerResult(await _handle_list_tools())


async def _sdk_call_tool(request: types.CallToolRequest) -> types.ServerResult:
    return types.ServerResult(
        await _handle_call_tool(request.params.name, request.params.arguments)
    )


async def _sdk_list_resources(_request: types.ListResourcesRequest) -> types.ServerResult:
    return types.ServerResult(await _handle_list_resources())


async def _sdk_list_resource_templates(
    _request: types.ListResourceTemplatesRequest,
) -> types.ServerResult:
    return types.ServerResult(await _handle_list_resource_templates())


async def _sdk_read_resource(request: types.ReadResourceRequest) -> types.ServerResult:
    return types.ServerResult(await _handle_read_resource(str(request.params.uri)))


_sdk = mcp._mcp_server
_sdk.request_handlers[types.ListToolsRequest] = _sdk_list_tools
_sdk.request_handlers[types.CallToolRequest] = _sdk_call_tool
_sdk.request_handlers[types.ListResourcesRequest] = _sdk_list_resources
_sdk.request_handlers[types.ListResourceTemplatesRequest] = _sdk_list_resource_templates
_sdk.request_handlers[types.ReadResourceRequest] = _sdk_read_resource


def _model_json(value: object) -> dict[str, object]:
    dumped = value.model_dump(  # type: ignore[union-attr]
        by_alias=True,
        exclude_none=True,
        mode="json",
    )
    if type(dumped) is not dict:
        raise TypeError("SDK result is invalid")
    return dumped


def _rpc_sdk_result(
    request_id: int | str,
    result: types.ServerResult,
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": _model_json(result.root),
    }


def _manual_initialize(request: types.InitializeRequest) -> types.ServerResult:
    options = _sdk.create_initialization_options()
    capabilities = options.capabilities.model_copy(
        update={
            "completions": None,
            "logging": None,
            "prompts": None,
            "tasks": None,
        }
    )
    requested = request.params.protocolVersion
    protocol_version = (
        requested if requested in SUPPORTED_PROTOCOL_VERSIONS else types.LATEST_PROTOCOL_VERSION
    )
    return types.ServerResult(
        types.InitializeResult(
            protocolVersion=protocol_version,
            capabilities=capabilities,
            serverInfo=types.Implementation(
                name=options.server_name,
                version=options.server_version,
                websiteUrl=options.website_url,
                icons=options.icons,
            ),
            instructions=options.instructions,
        )
    )


def _owned_dispatch_descriptor(
    descriptor: mcp_transport.ClientMessageDescriptor,
) -> dict[str, object] | None:
    """Typed SDK dispatch after the owned lexical and structural boundary."""

    if not isinstance(descriptor, mcp_transport.ClientMessageDescriptor):
        raise TypeError("owned descriptor is invalid")
    if descriptor.is_notification:
        typed: dict[str, object] = {"method": descriptor.method}
        if descriptor.params:
            typed["params"] = dict(descriptor.params)
        types.ClientNotification.model_validate(typed)
        return None
    request_id = descriptor.request_id
    if request_id is None:
        raise TypeError("owned request id is missing")
    try:
        typed_request = types.ClientRequest.model_validate(
            {
                "method": descriptor.method,
                "params": dict(descriptor.params),
            }
        ).root
        if isinstance(typed_request, types.InitializeRequest):
            result = _manual_initialize(typed_request)
        else:
            handler = _sdk.request_handlers.get(type(typed_request))
            if handler is None:
                raise RuntimeError("owned SDK handler is unavailable")
            result = anyio.run(handler, typed_request)
            if not isinstance(result, types.ServerResult):
                raise TypeError("owned SDK result is invalid")
        return _rpc_sdk_result(request_id, result)
    except McpError as error:
        pair = (error.error.code, error.error.message)
        allowed = (
            {_TOOL_REQUEST_INVALID, _TOOL_NAME_UNAVAILABLE, _TOOL_INTERNAL_ERROR}
            if descriptor.method == "tools/call"
            else {
                _RESOURCE_INVALID_IDENTIFIER,
                _RESOURCE_UNAVAILABLE,
                _RESOURCE_READ_LIMIT,
                _RESOURCE_RUNTIME_UNAVAILABLE,
                _RESOURCE_INTERNAL_ERROR,
            }
            if descriptor.method == "resources/read"
            else set()
        )
        if pair not in allowed:
            return _owned_failure_response(descriptor)
        return mcp_transport.rpc_error_response(
            mcp_transport.FixedRpcError(*pair),
            request_id=request_id,
        )
    except BaseException:
        return _owned_failure_response(descriptor)


def _owned_failure_response(
    descriptor: mcp_transport.ClientMessageDescriptor,
) -> dict[str, object] | None:
    request_id = descriptor.request_id
    if request_id is None:
        return None
    if descriptor.method == "tools/call":
        error = mcp_transport.INTERNAL_ERROR
    elif descriptor.method == "resources/read":
        error = mcp_transport.FixedRpcError(*_RESOURCE_INTERNAL_ERROR)
    else:
        error = mcp_transport.GENERIC_INTERNAL_ERROR
    return mcp_transport.rpc_error_response(error, request_id=request_id)


def _uninstall_recovery_response(
    request_id: int | str | None,
) -> dict[str, object]:
    if request_id is None:
        return mcp_transport.rpc_error_response(mcp_transport.INTERNAL_ERROR)
    try:
        result = types.ServerResult(
            _call_result("uninstall_runtime", _failure("recovery_required"))
        )
        return _rpc_sdk_result(request_id, result)
    except BaseException:
        return mcp_transport.rpc_error_response(
            mcp_transport.INTERNAL_ERROR,
            request_id=request_id,
        )


def _read_stdio_chunk(maximum: int) -> bytes:
    if type(maximum) is not int or maximum < 1 or maximum > mcp_transport.READ_CHUNK_BYTES:
        raise ValueError("stdio read size is invalid")
    stream = sys.stdin.buffer
    reader = getattr(stream, "read1", None)
    if not callable(reader):
        reader = getattr(stream, "read", None)
    if not callable(reader):
        raise OSError("stdio input is unavailable")
    chunk = reader(maximum)
    if type(chunk) is not bytes:
        raise OSError("stdio input is invalid")
    return chunk


def _write_stdio_frame(frame: bytes) -> None:
    if (
        type(frame) is not bytes
        or not frame.endswith(b"\n")
        or len(frame) > mcp_transport.MAX_RESPONSE_FRAME_BYTES + 1
    ):
        raise OSError("stdio response is invalid")
    stream = sys.stdout.buffer
    if stream.write(frame) != len(frame):
        raise OSError("stdio response write is incomplete")
    stream.flush()


def _run_owned_stdio(*, auto_install: bool = False) -> None:
    global _active_owned_runner
    _initialize_application_process_runtime()
    lifecycle = mcp_transport.ProcessLifecycle()
    runner = mcp_transport.OwnedStdioRunner(
        dispatch=_owned_dispatch_descriptor,
        lifecycle=lifecycle,
        close_application=_application_slot.close,
        uninstall_recovery_response=_uninstall_recovery_response,
        exit_process=os._exit,
        failure_response=_owned_failure_response,
    )
    with _active_owned_runner_lock:
        if _active_owned_runner is not None:
            raise RuntimeError("owned stdio runner is already active")
        _active_owned_runner = runner
    try:
        runner.run(
            read_chunk=_read_stdio_chunk,
            write_frame=_write_stdio_frame,
            before_read=_spawn_install if auto_install else None,
        )
    finally:
        with _active_owned_runner_lock:
            if _active_owned_runner is runner:
                _active_owned_runner = None


def _auto_install_enabled() -> bool:
    return os.environ.get("VIBECAD_AUTO_INSTALL", "") not in {"", "0", "false", "False"}


def main() -> None:
    _run_owned_stdio(auto_install=_auto_install_enabled())


if __name__ == "__main__":
    main()
