"""Runtime swap/lifecycle controls retained across the Agent cutover."""

from __future__ import annotations

import threading

import pytest

import vibecad.server as server
from vibecad.runtime import status


@pytest.fixture(autouse=True)
def _isolated_process_latches(monkeypatch):
    monkeypatch.delenv("VIBECAD_SUPERVISED", raising=False)
    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(server, "_active_owned_runner", None)


def test_swap_without_owned_runner_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")
    monkeypatch.setattr(server, "runtime_swappable", lambda: True)

    assert server._try_schedule_swap() is False
    assert server._try_schedule_swap(uninstall=True) is False


def test_normal_swap_is_forbidden_after_application_effect(monkeypatch) -> None:
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")
    monkeypatch.setattr(server, "runtime_swappable", lambda: True)

    class Runner:
        def request_swap(self) -> bool:
            pytest.fail("swap admission must be rejected before reaching the runner")

    monkeypatch.setattr(server, "_active_owned_runner", Runner())
    server._application_effect_entered.set()

    assert server._try_schedule_swap() is False


def test_runtime_status_schedules_only_from_bootstrap(monkeypatch) -> None:
    class Runner:
        calls = 0

        def request_swap(self) -> bool:
            self.calls += 1
            return True

    runner = Runner()
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")
    monkeypatch.setattr(server, "runtime_swappable", lambda: True)
    monkeypatch.setattr(server, "_in_conda_runtime", lambda: False)
    monkeypatch.setattr(server, "_active_owned_runner", runner)
    monkeypatch.setattr(server.status, "runtime_ready", lambda: True)
    monkeypatch.setattr(
        server.status,
        "read_status",
        lambda: status.RuntimeStatus(phase=status.Phase.READY, percent=100),
    )
    monkeypatch.setattr(server.status, "read_runtime_receipt", lambda: None)
    result = server.get_runtime_status()

    assert result["needs_reconnect"] is False
    assert runner.calls == 1


def test_owned_runner_latch_replaces_timer_for_production_swap(monkeypatch) -> None:
    class Runner:
        calls: list[str] = []

        def request_swap(self) -> bool:
            self.calls.append("swap")
            return True

        def request_uninstall_exit(self) -> bool:
            self.calls.append("uninstall")
            return True

    runner = Runner()
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")
    monkeypatch.setattr(server, "runtime_swappable", lambda: True)
    monkeypatch.setattr(server, "_active_owned_runner", runner)

    assert server._try_schedule_swap() is True
    assert server._try_schedule_swap(uninstall=True) is True
    assert runner.calls == ["swap", "uninstall"]


def test_confirmed_uninstall_closes_application_and_uses_exception_swap(monkeypatch) -> None:
    class Slot:
        close_calls = 0

        def close(self):
            self.close_calls += 1
            return True

    slot = Slot()
    monkeypatch.setattr(server, "_application_slot", slot)
    monkeypatch.setattr(
        server._uninstall,
        "preview_uninstall",
        lambda: {"ok": True, "size_mb": 2.5},
    )
    monkeypatch.setattr(
        server._uninstall,
        "request_uninstall",
        lambda: {"ok": True, "marked": True},
    )
    swap_arguments: list[bool] = []
    monkeypatch.setattr(
        server,
        "_try_schedule_swap",
        lambda *, uninstall=False: swap_arguments.append(uninstall) or True,
    )

    result = server.uninstall_runtime(confirm=True)

    assert result == {
        "schema_version": 1,
        "status": "marked",
        "confirm_required": False,
        "estimated_size_bytes": 2_500_000,
        "data_preserved": True,
        "message": "The managed CAD runtime is marked for removal; durable data is preserved.",
    }
    assert slot.close_calls == 1
    assert swap_arguments == [True]


def test_confirmed_uninstall_defers_exact_application_close_to_owned_runner(monkeypatch) -> None:
    class Slot:
        close_calls = 0

        def close(self):
            self.close_calls += 1
            return True

    class Runner:
        def request_uninstall_exit(self) -> bool:
            return True

    slot = Slot()
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")
    monkeypatch.setattr(server, "_active_owned_runner", Runner())
    monkeypatch.setattr(server, "runtime_swappable", lambda: True)
    monkeypatch.setattr(server, "_application_slot", slot)
    monkeypatch.setattr(
        server._uninstall,
        "preview_uninstall",
        lambda: {"ok": True, "size_mb": 1},
    )
    monkeypatch.setattr(
        server._uninstall,
        "request_uninstall",
        lambda: {"ok": True, "marked": True},
    )

    result = server.uninstall_runtime(confirm=True)

    assert result["status"] == "marked"
    assert slot.close_calls == 0


def test_uninstall_preview_never_exposes_a_local_path(monkeypatch) -> None:
    monkeypatch.setattr(
        server._uninstall,
        "preview_uninstall",
        lambda: {
            "ok": True,
            "size_mb": 1.25,
            "path": "/private/secret/runtime",
            "paths": ["/private/secret/runtime"],
        },
    )

    result = server.uninstall_runtime(confirm=False)

    assert result["status"] == "preview"
    assert result["estimated_size_bytes"] == 1_250_000
    assert "path" not in result
    assert "secret" not in str(result)


@pytest.mark.parametrize("value", ["", "0", "false", "False"])
def test_auto_install_enabled_rejects_falsy_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("VIBECAD_AUTO_INSTALL", value)
    assert server._auto_install_enabled() is False


def test_main_runs_owned_stdio_and_optional_installer(monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    monkeypatch.setenv("VIBECAD_AUTO_INSTALL", "1")
    monkeypatch.setattr(
        server,
        "_run_owned_stdio",
        lambda *, auto_install=False: calls.append(("owned", auto_install)),
    )
    monkeypatch.setattr(
        server.mcp,
        "run",
        lambda: pytest.fail("SDK stdio loop must stay unused"),
    )

    server.main()

    assert calls == [("owned", True)]
