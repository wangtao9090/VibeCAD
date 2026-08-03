"""Stable runtime-control behavior after the atomic Agent cutover."""

from __future__ import annotations

import threading

import anyio

import vibecad.server as server
from vibecad import __version__
from vibecad.runtime import status


def test_ping_has_exact_agent_wire_shape() -> None:
    assert server.ping() == {
        "schema_version": 1,
        "service": "vibecad",
        "version": __version__,
    }


def test_runtime_status_has_exact_schema_shape(monkeypatch) -> None:
    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(server.status, "read_status", lambda: status.RuntimeStatus())
    monkeypatch.setattr(server.status, "runtime_ready", lambda: False)
    monkeypatch.setattr(
        server.status,
        "runtime_recovery_kind",
        lambda: status.RecoveryKind.REPAIR_REQUIRED,
    )
    monkeypatch.setattr(server.status, "read_runtime_receipt", lambda: None)

    result = server.get_runtime_status()

    assert set(result) == {
        "schema_version",
        "phase",
        "percent",
        "message",
        "error",
        "runtime_compatible",
        "runtime_action",
        "installed_version",
        "required_version",
        "needs_reconnect",
    }
    assert result["phase"] == "not_started"
    assert result["runtime_action"] == "repair_required"
    assert result["required_version"] == __version__


def test_stale_ready_reports_action_without_breaking_phase_schema(monkeypatch) -> None:
    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(
        server.status,
        "read_status",
        lambda: status.RuntimeStatus(phase=status.Phase.READY, percent=100),
    )
    monkeypatch.setattr(server.status, "runtime_ready", lambda: False)
    monkeypatch.setattr(
        server.status,
        "runtime_recovery_kind",
        lambda: status.RecoveryKind.UPGRADE_REQUIRED,
    )
    monkeypatch.setattr(
        server.status,
        "read_runtime_receipt",
        lambda: {"vibecad_version": "0.3.0"},
    )

    result = server.get_runtime_status()

    assert result["phase"] == "ready"
    assert result["runtime_action"] == "upgrade_required"
    assert result["installed_version"] == "0.3.0"


def test_ensure_runtime_ready_and_started_shapes(monkeypatch) -> None:
    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(server, "_in_conda_runtime", lambda: True)
    monkeypatch.setattr(server._installer, "is_ready", lambda: True)
    assert server._ensure_runtime_impl() == {
        "status": "ready",
        "message": "The managed CAD runtime is ready.",
    }

    started: list[bool] = []
    monkeypatch.setattr(server._installer, "is_ready", lambda: False)
    monkeypatch.setattr(server, "_install_thread", None)
    monkeypatch.setattr(server, "_spawn_install", lambda: started.append(True))
    assert server._ensure_runtime_impl()["status"] == "started"
    assert started == [True]


def test_runtime_guard_returns_exact_public_envelope(monkeypatch) -> None:
    monkeypatch.setattr(server._installer, "is_ready", lambda: False)

    result = server._application_runtime_guard()

    assert result == {
        "schema_version": 1,
        "ok": False,
        "result": None,
        "error": {
            "schema_version": 1,
            "code": "runtime_unavailable",
            "path": "",
            "message": "The managed CAD runtime is not active.",
        },
    }
    called = anyio.run(
        server._handle_call_tool,
        "get_project",
        {
            "schema_version": 1,
            "project_id": "project_" + "0" * 32,
        },
    )
    assert called.structuredContent == result
