from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
from pathlib import Path

import pytest

from vibecad import freecad_bridge

_ADDON_ROOT = Path(__file__).resolve().parent.parent / "freecad" / "VibeCAD"


class _Client:
    daemon_id = "daemon_" + "a" * 32

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.closed = False

    def ping(self) -> dict[str, object]:
        self.calls.append(("ping", {}))
        return {"schema_version": 1, "status": "ready"}

    def list_projects_request(self, request: object) -> dict[str, object]:
        self.calls.append(("list_projects", request))
        return {"schema_version": 1, "projects": [], "next_cursor": None}

    def checkpoint_checkout(
        self,
        *,
        checkpoint_key: object,
        checkout_id: object,
    ) -> dict[str, object]:
        self.calls.append(
            (
                "checkpoint_checkout",
                {"checkpoint_key": checkpoint_key, "checkout_id": checkout_id},
            )
        )
        return {
            "schema_version": 1,
            "generation": 4,
            "next_action": "done",
            "task_run": {"status": "succeeded"},
        }

    def close(self) -> None:
        self.calls.append(("close", {}))
        self.closed = True


def _frame(value: object) -> bytes:
    return freecad_bridge.encode_bridge_frame(value)


def _frames(stream: io.BytesIO) -> list[dict[str, object]]:
    stream.seek(0)
    values: list[dict[str, object]] = []
    while stream.tell() < len(stream.getvalue()):
        values.append(freecad_bridge.read_bridge_frame(stream))
    return values


def test_bridge_handshake_and_closed_method_allowlist() -> None:
    nonce = "b" * 32
    client = _Client()
    source = io.BytesIO(
        b"".join(
            (
                _frame(
                    {
                        "schema_version": 1,
                        "kind": "ready",
                        "protocol": "vibecad-freecad-bridge",
                        "protocol_version": 1,
                        "nonce": nonce,
                    }
                ),
                _frame(
                    {
                        "schema_version": 1,
                        "kind": "request",
                        "request_id": 1,
                        "method": "ping",
                        "params": {},
                    }
                ),
                _frame(
                    {
                        "schema_version": 1,
                        "kind": "request",
                        "request_id": 2,
                        "method": "list_projects",
                        "params": {"request": {"schema_version": 1, "limit": 50, "cursor": None}},
                    }
                ),
                _frame(
                    {
                        "schema_version": 1,
                        "kind": "request",
                        "request_id": 3,
                        "method": "close",
                        "params": {},
                    }
                ),
            )
        )
    )
    target = io.BytesIO()

    assert (
        freecad_bridge.serve_bridge(
            source,
            target,
            client_factory=lambda: client,
            nonce_factory=lambda: nonce,
        )
        == 0
    )

    hello, ready, ping, projects, closed = _frames(target)
    assert hello == {
        "schema_version": 1,
        "kind": "hello",
        "protocol": "vibecad-freecad-bridge",
        "protocol_version": 1,
        "package_version": "0.6.0",
        "daemon_id": client.daemon_id,
        "nonce": nonce,
    }
    assert ready == {
        "schema_version": 1,
        "kind": "ready",
        "protocol": "vibecad-freecad-bridge",
        "protocol_version": 1,
    }
    assert ping["ok"] is True and ping["request_id"] == 1
    assert projects["ok"] is True and projects["request_id"] == 2
    assert closed["ok"] is True and closed["request_id"] == 3
    assert client.calls == [
        ("ping", {}),
        ("list_projects", {"schema_version": 1, "limit": 50, "cursor": None}),
        ("close", {}),
    ]
    assert client.closed is True


def test_bridge_rejects_unknown_method_without_dispatch() -> None:
    nonce = "c" * 32
    client = _Client()
    source = io.BytesIO(
        _frame(
            {
                "schema_version": 1,
                "kind": "ready",
                "protocol": "vibecad-freecad-bridge",
                "protocol_version": 1,
                "nonce": nonce,
            }
        )
        + _frame(
            {
                "schema_version": 1,
                "kind": "request",
                "request_id": 1,
                "method": "kernel.commit",
                "params": {},
            }
        )
        + _frame(
            {
                "schema_version": 1,
                "kind": "request",
                "request_id": 2,
                "method": "close",
                "params": {},
            }
        )
    )
    target = io.BytesIO()

    assert (
        freecad_bridge.serve_bridge(
            source,
            target,
            client_factory=lambda: client,
            nonce_factory=lambda: nonce,
        )
        == 0
    )

    values = _frames(target)
    assert values[2] == {
        "schema_version": 1,
        "kind": "response",
        "request_id": 1,
        "ok": False,
        "error": {"code": "invalid_input"},
    }
    assert client.calls == [("close", {})]


def test_bridge_dispatches_checkpoint_without_a_path_capability() -> None:
    client = _Client()
    result = freecad_bridge._dispatch(  # noqa: SLF001
        client,
        "checkpoint_checkout",
        {
            "checkpoint_key": "checkpoint_create_" + "4" * 32,
            "checkout_id": "checkout_" + "5" * 32,
        },
    )

    assert result["task_run"]["status"] == "succeeded"
    assert client.calls == [
        (
            "checkpoint_checkout",
            {
                "checkpoint_key": "checkpoint_create_" + "4" * 32,
                "checkout_id": "checkout_" + "5" * 32,
            },
        )
    ]
    with pytest.raises(freecad_bridge.BridgeProtocolError):
        freecad_bridge._dispatch(  # noqa: SLF001
            client,
            "checkpoint_checkout",
            {
                "checkpoint_key": "checkpoint_create_" + "4" * 32,
                "checkout_id": "checkout_" + "5" * 32,
                "local_path": "/tmp/model.FCStd",
            },
        )


def test_bridge_resolves_raw_identity_inventory_with_managed_selector_core() -> None:
    nonce = "f" * 32
    client = _Client()
    request = {
        "schema_version": 1,
        "project_id": "project_" + "1" * 32,
        "revision_id": "revision_" + "2" * 32,
        "selected_index": 1,
        "objects": [
            {
                "object_id": "object_" + "3" * 32,
                "feature_id": None,
                "object_type": "Part::Feature",
                "semantic_role": "part",
                "provenance": '{"operation_id":null,"source":"system"}',
            },
            {
                "object_id": "object_" + "4" * 32,
                "feature_id": "feature_" + "5" * 32,
                "object_type": "Part::Box",
                "semantic_role": "primitive",
                "provenance": '{"operation_id":"box","source":"model"}',
            },
        ],
    }
    source = io.BytesIO(
        _frame(
            {
                "schema_version": 1,
                "kind": "ready",
                "protocol": "vibecad-freecad-bridge",
                "protocol_version": 1,
                "nonce": nonce,
            }
        )
        + _frame(
            {
                "schema_version": 1,
                "kind": "request",
                "request_id": 1,
                "method": "resolve_selector",
                "params": {"request": request},
            }
        )
        + _frame(
            {
                "schema_version": 1,
                "kind": "request",
                "request_id": 2,
                "method": "close",
                "params": {},
            }
        )
    )
    target = io.BytesIO()

    assert (
        freecad_bridge.serve_bridge(
            source,
            target,
            client_factory=lambda: client,
            nonce_factory=lambda: nonce,
        )
        == 0
    )

    response = _frames(target)[2]
    assert response["ok"] is True
    result = response["result"]
    assert result["schema_version"] == 1
    assert result["selector"]["entity_kind"] == "feature"
    assert result["selector"]["object_id"] == "object_" + "4" * 32
    assert result["selector"]["feature_id"] == "feature_" + "5" * 32
    assert result["text"] == json.dumps(
        result["selector"],
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_bridge_frame_rejects_duplicate_keys_and_oversize() -> None:
    duplicate = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(freecad_bridge.BridgeProtocolError):
        freecad_bridge.read_bridge_frame(io.BytesIO(len(duplicate).to_bytes(4, "big") + duplicate))

    maximum = freecad_bridge.MAX_BRIDGE_FRAME_BYTES
    with pytest.raises(freecad_bridge.BridgeProtocolError):
        freecad_bridge.read_bridge_frame(io.BytesIO((maximum + 1).to_bytes(4, "big")))


def test_packaged_addon_allowlist_contains_bridge_and_selection() -> None:
    from vibecad import freecad_launcher

    assert "vibecad_workbench/bridge.py" in freecad_launcher._ADDON_FILES
    assert "vibecad_workbench/selection.py" in freecad_launcher._ADDON_FILES


class _RetainedBytesIO(io.BytesIO):
    def close(self) -> None:
        return None


class _BridgeProcess:
    def __init__(self, output: bytes) -> None:
        self.stdin = _RetainedBytesIO()
        self.stdout = _RetainedBytesIO(output)
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_addon_bridge_client_verifies_exact_child_and_proxies_methods(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.syspath_prepend(str(_ADDON_ROOT))
    for name in tuple(sys.modules):
        if name == "vibecad_workbench" or name.startswith("vibecad_workbench."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    bridge = importlib.import_module("vibecad_workbench.bridge")
    python = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256(python.read_bytes()).hexdigest()
    config = tmp_path / "bridge.json"
    config.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protocol": "vibecad-freecad-bridge",
                "protocol_version": 1,
                "package_version": "0.6.0",
                "python_path": str(python),
                "python_target": str(python),
                "python_sha256": digest,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    nonce = "d" * 32
    daemon_id = "daemon_" + "e" * 32
    output = b"".join(
        (
            _frame(
                {
                    "schema_version": 1,
                    "kind": "hello",
                    "protocol": "vibecad-freecad-bridge",
                    "protocol_version": 1,
                    "package_version": "0.6.0",
                    "daemon_id": daemon_id,
                    "nonce": nonce,
                }
            ),
            _frame(
                {
                    "schema_version": 1,
                    "kind": "ready",
                    "protocol": "vibecad-freecad-bridge",
                    "protocol_version": 1,
                }
            ),
            _frame(
                {
                    "schema_version": 1,
                    "kind": "response",
                    "request_id": 1,
                    "ok": True,
                    "result": {"schema_version": 1, "status": "ready"},
                }
            ),
            _frame(
                {
                    "schema_version": 1,
                    "kind": "response",
                    "request_id": 2,
                    "ok": True,
                    "result": {},
                }
            ),
        )
    )
    process = _BridgeProcess(output)
    launches: list[tuple[list[str], dict[str, object]]] = []

    def popen(command: list[str], **kwargs: object) -> _BridgeProcess:
        launches.append((command, kwargs))
        return process

    monkeypatch.setattr(bridge.subprocess, "Popen", popen)
    monkeypatch.setenv("PYTHONPATH", "must-not-cross")
    monkeypatch.setenv("LD_LIBRARY_PATH", "must-not-cross")

    client = bridge.ExternalBridgeClient.open(config)
    assert client.daemon_id == daemon_id
    assert client.ping() == {"schema_version": 1, "status": "ready"}
    client.close()

    assert len(launches) == 1
    command, kwargs = launches[0]
    assert command == [str(python), "-I", "-m", "vibecad.freecad_bridge"]
    assert "PYTHONPATH" not in kwargs["env"]
    assert "LD_LIBRARY_PATH" not in kwargs["env"]
    process.stdin.seek(0)
    sent = _frames(process.stdin)
    assert [value["kind"] for value in sent] == ["ready", "request", "request"]
    assert [value.get("method") for value in sent[1:]] == ["ping", "close"]
    assert process.terminated is False
    assert process.killed is False
