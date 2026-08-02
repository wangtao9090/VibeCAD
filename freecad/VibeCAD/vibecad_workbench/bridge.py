"""Self-contained client for the external managed-Python daemon bridge."""

from __future__ import annotations

import hashlib
import json
import math
import os
import select
import stat
import subprocess
import time
from enum import Enum
from pathlib import Path

__all__ = ("ExternalBridgeClient", "external_client_factory")

_PROTOCOL = "vibecad-freecad-bridge"
_PROTOCOL_VERSION = 1
_MAX_FRAME_BYTES = 1_048_576
_MAX_DEPTH = 12
_MAX_NODES = 10_000
_MAX_STRING = 4096
_MAX_KEY = 128
_MAX_CONTAINER = 1000
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_CALL_TIMEOUT_SECONDS = 35.0
_HANDSHAKE_TIMEOUT_SECONDS = 45.0
_CONFIG_NAME = "bridge.json"
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "protocol",
        "protocol_version",
        "package_version",
        "python_path",
        "python_sha256",
        "python_target",
    }
)
_ERROR_CODES = frozenset(
    {
        "invalid_input",
        "unavailable",
        "internal_error",
        "closed",
        "wrong_process",
        "incompatible_kernel",
    }
)
_ENVIRONMENT_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TMPDIR",
        "VIBECAD_HOME",
    }
)


class _BridgeProtocolError(ValueError):
    pass


class BridgeClientErrorCode(Enum):
    INVALID_INPUT = "invalid_input"
    UNAVAILABLE = "unavailable"
    INTERNAL_ERROR = "internal_error"
    CLOSED = "closed"
    WRONG_PROCESS = "wrong_process"
    INCOMPATIBLE_KERNEL = "incompatible_kernel"


class BridgeClientError(RuntimeError):
    __slots__ = ("code",)

    def __init__(self, code: BridgeClientErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _plain(value: object, *, depth: int = 0, budget: list[int] | None = None) -> object:
    if budget is None:
        budget = [_MAX_NODES]
    budget[0] -= 1
    if budget[0] < 0 or depth > _MAX_DEPTH:
        raise _BridgeProtocolError
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise _BridgeProtocolError
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _BridgeProtocolError
        return value
    if type(value) is str:
        if len(value.encode("utf-8")) > _MAX_STRING:
            raise _BridgeProtocolError
        return value
    if type(value) is list:
        if len(value) > _MAX_CONTAINER:
            raise _BridgeProtocolError
        return [_plain(item, depth=depth + 1, budget=budget) for item in value]
    if type(value) is dict:
        if len(value) > _MAX_CONTAINER or any(
            type(key) is not str or len(key.encode("utf-8")) > _MAX_KEY for key in value
        ):
            raise _BridgeProtocolError
        return {key: _plain(item, depth=depth + 1, budget=budget) for key, item in value.items()}
    raise _BridgeProtocolError


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise _BridgeProtocolError
        result[key] = value
    return result


def _encode_frame(value: object) -> bytes:
    copied = _plain(value)
    if type(copied) is not dict:
        raise _BridgeProtocolError
    try:
        payload = json.dumps(
            copied,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError):
        raise _BridgeProtocolError from None
    if not payload or len(payload) > _MAX_FRAME_BYTES:
        raise _BridgeProtocolError
    return len(payload).to_bytes(4, "big") + payload


def _read_exact(stream: object, size: int, *, deadline: float) -> bytes:
    result = bytearray()
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _BridgeProtocolError
        try:
            ready, _, _ = select.select([stream], [], [], remaining)
        except (AttributeError, OSError, TypeError, ValueError):
            ready = [stream]
        if not ready:
            raise _BridgeProtocolError
        chunk = stream.read(size - len(result))
        if not chunk:
            raise _BridgeProtocolError
        result.extend(chunk)
    return bytes(result)


def _read_frame(stream: object, *, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    size = int.from_bytes(_read_exact(stream, 4, deadline=deadline), "big")
    if not 0 < size <= _MAX_FRAME_BYTES:
        raise _BridgeProtocolError
    payload = _read_exact(stream, size, deadline=deadline)
    try:
        value = json.loads(payload.decode("ascii"), object_pairs_hook=_json_object)
    except (_BridgeProtocolError, UnicodeError, ValueError, json.JSONDecodeError):
        raise _BridgeProtocolError from None
    copied = _plain(value)
    if type(copied) is not dict:
        raise _BridgeProtocolError
    return copied


def _write_frame(stream: object, value: object) -> None:
    stream.write(_encode_frame(value))
    stream.flush()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _has_private_user_ancestor(path: Path) -> bool:
    for ancestor in path.parents:
        try:
            info = ancestor.lstat()
            if ancestor != ancestor.resolve(strict=True) or not stat.S_ISDIR(info.st_mode):
                return False
        except OSError:
            return False
        if info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0:
            return True
    return False


def _configuration(path: Path) -> dict[str, object]:
    try:
        if path != path.resolve(strict=True):
            raise OSError
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise OSError
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_object)
        copied = _plain(value)
        if type(copied) is not dict or set(copied) != _CONFIG_KEYS:
            raise _BridgeProtocolError
        package_version = copied["package_version"]
        python_sha256 = copied["python_sha256"]
        if (
            copied["schema_version"] != 1
            or copied["protocol"] != _PROTOCOL
            or copied["protocol_version"] != _PROTOCOL_VERSION
            or type(package_version) is not str
            or not package_version
            or len(package_version) > 64
            or type(python_sha256) is not str
            or len(python_sha256) != 64
            or any(character not in "0123456789abcdef" for character in python_sha256)
        ):
            raise _BridgeProtocolError
        python = Path(copied["python_path"])
        python_target = Path(copied["python_target"])
        if (
            type(copied["python_path"]) is not str
            or type(copied["python_target"]) is not str
            or not python.is_absolute()
            or not python_target.is_absolute()
            or python.parent != python.parent.resolve(strict=True)
            or python_target != python_target.resolve(strict=True)
            or python.resolve(strict=True) != python_target
        ):
            raise OSError
        entry = python.lstat()
        executable = python_target.lstat()
        if (
            entry.st_uid not in {0, os.getuid()}
            or not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode))
            or (
                python != python_target
                and (not stat.S_ISLNK(entry.st_mode) or python.parent != python_target.parent)
            )
            or not stat.S_ISREG(executable.st_mode)
            or executable.st_uid not in {0, os.getuid()}
            or stat.S_IMODE(executable.st_mode) & 0o002
            or (
                stat.S_IMODE(executable.st_mode) & 0o020
                and not _has_private_user_ancestor(python_target)
            )
            or not os.access(python_target, os.X_OK)
            or _sha256(python_target) != python_sha256
        ):
            raise OSError
        return copied
    except (KeyError, OSError, TypeError, UnicodeError, ValueError, _BridgeProtocolError):
        raise BridgeClientError(BridgeClientErrorCode.UNAVAILABLE) from None


def _environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if name in _ENVIRONMENT_ALLOWLIST
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


class ExternalBridgeClient:
    """LocalAgentClient-compatible proxy owning one exact bridge child."""

    __slots__ = ("_process", "_input", "_output", "_closed", "_request_id", "daemon_id")

    def __init__(self, process: object, *, daemon_id: str) -> None:
        self._process = process
        self._input = process.stdin
        self._output = process.stdout
        self._closed = False
        self._request_id = 0
        self.daemon_id = daemon_id

    @classmethod
    def open(cls, config_path: object) -> ExternalBridgeClient:
        path = Path(config_path)
        config = _configuration(path)
        python = Path(config["python_path"])
        python_target = Path(config["python_target"])
        before = (python.lstat(), python_target.lstat())
        process = None
        try:
            process = subprocess.Popen(
                [str(python), "-I", "-m", "vibecad.freecad_bridge"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=_environment(),
                bufsize=0,
                start_new_session=True,
            )
            if process.stdin is None or process.stdout is None:
                raise _BridgeProtocolError
            after = (python.lstat(), python_target.lstat())
            if before != after or _sha256(python_target) != config["python_sha256"]:
                raise _BridgeProtocolError
            hello = _read_frame(process.stdout, timeout=_HANDSHAKE_TIMEOUT_SECONDS)
            if set(hello) != {
                "schema_version",
                "kind",
                "protocol",
                "protocol_version",
                "package_version",
                "daemon_id",
                "nonce",
            }:
                raise _BridgeProtocolError
            daemon_id = hello["daemon_id"]
            nonce = hello["nonce"]
            if (
                hello["schema_version"] != 1
                or hello["kind"] != "hello"
                or hello["protocol"] != _PROTOCOL
                or hello["protocol_version"] != _PROTOCOL_VERSION
                or hello["package_version"] != config["package_version"]
                or type(daemon_id) is not str
                or len(daemon_id) != 39
                or not daemon_id.startswith("daemon_")
                or any(character not in "0123456789abcdef" for character in daemon_id[7:])
                or type(nonce) is not str
                or len(nonce) != 32
                or any(character not in "0123456789abcdef" for character in nonce)
            ):
                raise _BridgeProtocolError
            _write_frame(
                process.stdin,
                {
                    "schema_version": 1,
                    "kind": "ready",
                    "protocol": _PROTOCOL,
                    "protocol_version": _PROTOCOL_VERSION,
                    "nonce": nonce,
                },
            )
            ready = _read_frame(process.stdout, timeout=_HANDSHAKE_TIMEOUT_SECONDS)
            if ready != {
                "schema_version": 1,
                "kind": "ready",
                "protocol": _PROTOCOL,
                "protocol_version": _PROTOCOL_VERSION,
            }:
                raise _BridgeProtocolError
            return cls(process, daemon_id=daemon_id)
        except BaseException:
            if process is not None:
                _retire_process(process)
            raise BridgeClientError(BridgeClientErrorCode.UNAVAILABLE) from None

    def _call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        if self._closed:
            raise BridgeClientError(BridgeClientErrorCode.CLOSED)
        self._request_id += 1
        if self._request_id > _MAX_SAFE_INTEGER:
            raise BridgeClientError(BridgeClientErrorCode.INTERNAL_ERROR)
        request_id = self._request_id
        try:
            _write_frame(
                self._input,
                {
                    "schema_version": 1,
                    "kind": "request",
                    "request_id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            response = _read_frame(self._output, timeout=_CALL_TIMEOUT_SECONDS)
            if (
                type(response) is not dict
                or response.get("schema_version") != 1
                or response.get("kind") != "response"
                or response.get("request_id") != request_id
                or type(response.get("ok")) is not bool
            ):
                raise _BridgeProtocolError
            if response["ok"] is True:
                if (
                    set(response)
                    != {
                        "schema_version",
                        "kind",
                        "request_id",
                        "ok",
                        "result",
                    }
                    or type(response["result"]) is not dict
                ):
                    raise _BridgeProtocolError
                return response["result"]
            if (
                set(response)
                != {
                    "schema_version",
                    "kind",
                    "request_id",
                    "ok",
                    "error",
                }
                or type(response["error"]) is not dict
            ):
                raise _BridgeProtocolError
            error = response["error"]
            if set(error) != {"code"} or error["code"] not in _ERROR_CODES:
                raise _BridgeProtocolError
            raise BridgeClientError(BridgeClientErrorCode(error["code"]))
        except BridgeClientError:
            raise
        except BaseException:
            self._closed = True
            _retire_process(self._process)
            raise BridgeClientError(BridgeClientErrorCode.UNAVAILABLE) from None

    def ping(self) -> dict[str, object]:
        return self._call("ping", {})

    def list_projects_request(self, request: object) -> dict[str, object]:
        return self._call("list_projects", {"request": request})

    def list_tasks_request(self, request: object) -> dict[str, object]:
        return self._call("list_tasks", {"request": request})

    def get_project_request(self, request: object) -> dict[str, object]:
        return self._call("get_project", {"request": request})

    def get_task_request(self, request: object) -> dict[str, object]:
        return self._call("get_task", {"request": request})

    def accept_draft_request(self, request: object) -> dict[str, object]:
        return self._call("accept_draft", {"request": request})

    def reject_draft_request(self, request: object) -> dict[str, object]:
        return self._call("reject_draft", {"request": request})

    def open_checkout(self, *, open_key: object, source: object) -> dict[str, object]:
        return self._call("open_checkout", {"open_key": open_key, "source": source})

    def get_checkout(self, *, checkout_id: object) -> dict[str, object]:
        return self._call("get_checkout", {"checkout_id": checkout_id})

    def checkpoint_checkout(
        self,
        *,
        checkpoint_key: object,
        checkout_id: object,
    ) -> dict[str, object]:
        return self._call(
            "checkpoint_checkout",
            {
                "checkpoint_key": checkpoint_key,
                "checkout_id": checkout_id,
            },
        )

    def close_checkout(self, *, checkout_id: object) -> dict[str, object]:
        return self._call("close_checkout", {"checkout_id": checkout_id})

    def claim_file_grant(self, *, grant_id: object) -> dict[str, object]:
        return self._call("claim_file_grant", {"grant_id": grant_id})

    def resolve_selector_request(self, request: object) -> dict[str, object]:
        return self._call("resolve_selector", {"request": request})

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._call("close", {})
        finally:
            self._closed = True
            _retire_process(self._process, terminate=False)


def _retire_process(process: object, *, terminate: bool = True) -> None:
    for stream_name in ("stdin", "stdout"):
        stream = getattr(process, stream_name, None)
        if stream is not None:
            try:
                stream.close()
            except BaseException:
                pass
    try:
        if terminate and process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        return
    except BaseException:
        pass
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
    except BaseException:
        pass


def external_client_factory(addon_root: object | None = None) -> object | None:
    root = (
        Path(__file__).resolve().parent.parent
        if addon_root is None
        else Path(addon_root).resolve(strict=True)
    )
    config = root / _CONFIG_NAME
    if not os.path.lexists(config):
        return None
    _configuration(config)
    return lambda: ExternalBridgeClient.open(config)
