"""Bounded file ingress for WorkBuddy ModelProgram submission.

WorkBuddy 5.3.5 can collapse a failed MCP ``CallToolResult`` into a generic
``-32603``.  This host adapter preserves the ordinary Task Kernel authority
while returning the exact local contract error on stdout.  It is intentionally
one operation, not a second workflow or a general-purpose command runner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from pathlib import Path

from vibecad import _file_compat
from vibecad.daemon.adapters import LocalAgentClient, LocalAgentClientError
from vibecad.parametric import ParametricContractError, ParametricDesignIR
from vibecad.workflow.contracts import ModelProgram
from vibecad.workflow.errors import MAX_SAFE_JSON_INTEGER, ContractValidationError
from vibecad.workflow.program import ProgramValidationError, validate_model_program

_SCHEMA_VERSION = 1
_MAX_PROGRAM_JSON_BYTES = 512 * 1_024
_MAX_REQUEST_FILE_BYTES = _MAX_PROGRAM_JSON_BYTES + 4_096
_READ_CHUNK_BYTES = 65_536
_REQUEST_NAME = re.compile(
    r"^\.vibecad-workbuddy-request(?:-[a-z0-9][a-z0-9_-]{0,47})?\.json$",
    re.ASCII,
)
_TASK_ID = re.compile(r"^task_[0-9a-f]{32}$", re.ASCII)
_PARAMETRIC_OPERATIONS = frozenset({"create_parametric_design", "modify_parametric_parameter"})


class _AdapterFailure(ValueError):
    __slots__ = ("code", "message", "path")

    def __init__(self, code: str, path: str, message: str) -> None:
        self.code = code
        self.path = path
        self.message = message
        super().__init__(message)


class _DuplicateField(ValueError):
    pass


def _error_envelope(code: str, path: str, message: str) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": _SCHEMA_VERSION,
            "code": code,
            "path": path,
            "message": message,
        },
    }


def _success_summary(envelope: object) -> dict[str, object]:
    if type(envelope) is not dict:
        raise _AdapterFailure("internal_error", "", "The VibeCAD response is invalid.")
    if envelope.get("ok") is not True:
        if (
            envelope.get("schema_version") == _SCHEMA_VERSION
            and envelope.get("result") is None
            and type(envelope.get("error")) is dict
        ):
            return dict(envelope)
        raise _AdapterFailure("internal_error", "", "The VibeCAD response is invalid.")
    result = envelope.get("result")
    task = result.get("task_run") if type(result) is dict else None
    if type(result) is not dict or type(task) is not dict:
        raise _AdapterFailure("internal_error", "", "The VibeCAD response is invalid.")
    draft = task.get("draft")
    reports = task.get("verification_reports")
    if draft is not None and type(draft) is not dict:
        raise _AdapterFailure("internal_error", "", "The VibeCAD response is invalid.")
    if type(reports) is not list:
        raise _AdapterFailure("internal_error", "", "The VibeCAD response is invalid.")
    latest_report = reports[-1] if reports else None
    if latest_report is not None and type(latest_report) is not dict:
        raise _AdapterFailure("internal_error", "", "The VibeCAD response is invalid.")
    return {
        "schema_version": _SCHEMA_VERSION,
        "ok": True,
        "result": {
            "task_id": task.get("id"),
            "project_id": task.get("project_id"),
            "generation": result.get("generation"),
            "status": task.get("status"),
            "next_action": result.get("next_action"),
            "base_revision": task.get("base_revision"),
            "candidate_revision": task.get("candidate_revision"),
            "committed_revision": task.get("committed_revision"),
            "draft_id": None if draft is None else draft.get("id"),
            "last_error": task.get("last_error"),
            "verification_passed": (None if latest_report is None else latest_report.get("passed")),
        },
        "error": None,
    }


def _safe_read_request(name: object) -> tuple[bytes, str]:
    if type(name) is not str or _REQUEST_NAME.fullmatch(name) is None:
        raise _AdapterFailure(
            "invalid_request_file",
            "/request_file",
            "Use a project-local .vibecad-workbuddy-request*.json file.",
        )
    if sys.platform == "win32":
        return _safe_read_request_windows(name)
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
    if any(type(getattr(os, item, None)) is not int for item in required):
        raise _AdapterFailure("unavailable", "/request_file", "Safe file ingress is unavailable.")
    directory_fd = -1
    file_fd = -1
    try:
        directory_fd = os.open(
            ".",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        directory = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or stat.S_IMODE(directory.st_mode) & 0o022
        ):
            raise _AdapterFailure(
                "unsafe_request_file",
                "/request_file",
                "The project directory is not safe for local request ingress.",
            )
        file_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or not 1 <= before.st_size <= _MAX_REQUEST_FILE_BYTES
        ):
            raise _AdapterFailure(
                "unsafe_request_file",
                "/request_file",
                "The request file is not a bounded owned regular file.",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(_READ_CHUNK_BYTES, _MAX_REQUEST_FILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_REQUEST_FILE_BYTES:
                raise _AdapterFailure(
                    "request_too_large",
                    "/request_file",
                    "The request file exceeds the supported size.",
                )
        after = os.fstat(file_fd)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

        def binding(value: os.stat_result) -> tuple[int, ...]:
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

        if binding(before) != binding(after) or binding(after) != binding(current):
            raise _AdapterFailure(
                "unsafe_request_file",
                "/request_file",
                "The request file changed during ingress.",
            )
        raw = b"".join(chunks)
        return raw, hashlib.sha256(raw).hexdigest()
    except _AdapterFailure:
        raise
    except OSError:
        raise _AdapterFailure(
            "unsafe_request_file",
            "/request_file",
            "The request file could not be opened safely.",
        ) from None
    finally:
        if file_fd >= 0:
            try:
                os.close(file_fd)
            except OSError:
                pass
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _safe_read_request_windows(name: str) -> tuple[bytes, str]:
    """Read one exact project-local Windows file through a pinned HANDLE.

    Windows CRT directory descriptors and POSIX owner/mode bits cannot express
    the original ingress authority.  The native file handle instead withholds
    delete sharing, rejects reparse points and multiple links, and pins the
    volume plus 128-bit File ID while bounded bytes are read.  A final-path
    comparison also rejects a junction in the supplied working-directory path.
    """

    expected = Path(os.path.abspath(name))
    file_fd = -1
    try:
        file_fd, capability = _file_compat.open_windows_external_file(expected)
        if os.path.normcase(capability.path) != os.path.normcase(os.fspath(expected)):
            raise _AdapterFailure(
                "unsafe_request_file",
                "/request_file",
                "The project request path contains an alias.",
            )
        before = os.fstat(file_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= _MAX_REQUEST_FILE_BYTES
        ):
            raise _AdapterFailure(
                "unsafe_request_file",
                "/request_file",
                "The request file is not a bounded single-link regular file.",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(
                file_fd,
                min(_READ_CHUNK_BYTES, _MAX_REQUEST_FILE_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_REQUEST_FILE_BYTES:
                raise _AdapterFailure(
                    "request_too_large",
                    "/request_file",
                    "The request file exceeds the supported size.",
                )
        after = os.fstat(file_fd)
        current_capability = _file_compat.capture_windows_external_fd(
            file_fd,
            generation_token=capability.generation_token,
        )
        _file_compat.validate_windows_external_file(capability)

        def binding(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_mode,
                value.st_nlink,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        if current_capability != capability or binding(before) != binding(after):
            raise _AdapterFailure(
                "unsafe_request_file",
                "/request_file",
                "The request file changed during ingress.",
            )
        raw = b"".join(chunks)
        return raw, hashlib.sha256(raw).hexdigest()
    except _AdapterFailure:
        raise
    except OSError:
        raise _AdapterFailure(
            "unsafe_request_file",
            "/request_file",
            "The request file could not be opened safely.",
        ) from None
    finally:
        if file_fd >= 0:
            try:
                os.close(file_fd)
            except OSError:
                pass


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateField
        result[key] = value
    return result


def _decode_request(raw: bytes) -> tuple[dict[str, object], dict[str, object]]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, RecursionError):
        raise _AdapterFailure(
            "invalid_request_file",
            "",
            "The request file must contain strict UTF-8 JSON without duplicate fields.",
        ) from None
    if type(value) is not dict:
        raise _AdapterFailure("invalid_type", "", "The request root must be an object.")
    allowed = {"schema_version", "task_id", "expected_generation", "program"}
    unknown = sorted(set(value) - allowed)
    missing = sorted(allowed - set(value))
    if unknown:
        raise _AdapterFailure("unknown_field", f"/{unknown[0]}", "The field is not supported.")
    if missing:
        raise _AdapterFailure("missing_field", f"/{missing[0]}", "A required field is missing.")
    if type(value["schema_version"]) is not int or value["schema_version"] != _SCHEMA_VERSION:
        raise _AdapterFailure(
            "unsupported_version",
            "/schema_version",
            "The schema version is not supported.",
        )
    if type(value["task_id"]) is not str or _TASK_ID.fullmatch(value["task_id"]) is None:
        raise _AdapterFailure("invalid_value", "/task_id", "The task id is invalid.")
    generation = value["expected_generation"]
    if type(generation) is not int or not 0 <= generation <= MAX_SAFE_JSON_INTEGER:
        raise _AdapterFailure(
            "invalid_value",
            "/expected_generation",
            "The expected generation is invalid.",
        )
    if type(value["program"]) is not dict:
        raise _AdapterFailure("invalid_type", "/program", "The program must be an object.")
    return value, value["program"]


def _preflight_program(
    request: dict[str, object],
    program_mapping: dict[str, object],
) -> str:
    try:
        program = ModelProgram.from_mapping(program_mapping)
        if program.task_id != request["task_id"]:
            raise _AdapterFailure(
                "invalid_value",
                "/program/task_id",
                "The ModelProgram task id must match the submission task id.",
            )
        operations = program_mapping.get("operations")
        if type(operations) is list:
            for index, operation in enumerate(operations):
                if type(operation) is not dict or operation.get("op") not in _PARAMETRIC_OPERATIONS:
                    continue
                arguments = operation.get("args")
                design = arguments.get("design") if type(arguments) is dict else None
                try:
                    ParametricDesignIR.from_mapping(design)
                except ParametricContractError as error:
                    raise _AdapterFailure(
                        error.code.value,
                        f"/program/operations/{index}/args/design{error.path}",
                        error.message,
                    ) from None
        validate_model_program(program)
        encoded = json.dumps(
            program_mapping,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(encoded.encode("utf-8")) > _MAX_PROGRAM_JSON_BYTES:
            raise _AdapterFailure(
                "budget_exceeded",
                "/program",
                "The ModelProgram exceeds the supported size.",
            )
        return encoded
    except _AdapterFailure:
        raise
    except ContractValidationError as error:
        raise _AdapterFailure(
            error.code.value,
            f"/program{error.path}",
            error.message,
        ) from None
    except ProgramValidationError as error:
        raise _AdapterFailure(
            error.code.value,
            f"/program{error.path}",
            error.message,
        ) from None
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _AdapterFailure(
            "invalid_model_program",
            "/program",
            "The ModelProgram is invalid.",
        ) from None


def submit_request_file(
    name: object,
    *,
    client_factory: Callable[[], LocalAgentClient] = LocalAgentClient.open,
) -> dict[str, object]:
    try:
        raw, digest = _safe_read_request(name)
        request, program = _decode_request(raw)
        program_json = _preflight_program(request, program)
        client = client_factory()
        try:
            envelope = client.submit_model_program_request(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "task_id": request["task_id"],
                    "expected_generation": request["expected_generation"],
                    "program_json": program_json,
                }
            )
        finally:
            client.close()
        result = _success_summary(envelope)
        result["request_sha256"] = digest
        return result
    except _AdapterFailure as error:
        return _error_envelope(error.code, error.path, error.message)
    except LocalAgentClientError as error:
        return _error_envelope(
            error.code.value,
            "",
            "The local VibeCAD Task Kernel is unavailable.",
        )
    except BaseException:
        return _error_envelope("internal_error", "", "The request could not be completed.")


def handle_cli(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] != "--workbuddy-submit":
        print(
            "usage: vibecad --workbuddy-submit .vibecad-workbuddy-request[-name].json",
            file=sys.stderr,
        )
        return 2
    result = submit_request_file(arguments[1])
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


__all__ = ("handle_cli", "submit_request_file")
