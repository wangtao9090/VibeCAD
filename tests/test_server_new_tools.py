"""The former broad server tool catalog is a closed Agent projection."""

from __future__ import annotations

import anyio
import pytest
from mcp.shared.exceptions import McpError

import vibecad.server as server

EXPECTED_NAMES = (
    "ping",
    "get_runtime_status",
    "ensure_runtime",
    "uninstall_runtime",
    "get_capabilities",
    "create_project",
    "get_project",
    "list_projects",
    "list_revisions",
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_events",
    "submit_model_program",
    "resume_task",
    "accept_draft",
    "reject_draft",
    "export_task_artifacts",
    "create_box",
    "create_cylinder",
    "inspect_model",
    "modify_parameter",
    "move_part",
    "rotate_part",
)


def test_live_tool_catalog_is_the_exact_agent_projection() -> None:
    assert tuple(tool.name for tool in anyio.run(server._handle_list_tools).tools) == EXPECTED_NAMES


def test_removed_legacy_name_is_closed_before_application_access(monkeypatch) -> None:
    calls: list[str] = []

    class ClosedSlot:
        def get(self):
            calls.append("get")
            raise AssertionError("application must remain unopened")

    monkeypatch.setattr(server, "_application_slot", ClosedSlot())
    with pytest.raises(McpError) as caught:
        anyio.run(server._handle_call_tool, "add_box", {})

    assert (caught.value.error.code, caught.value.error.message) == (
        -32602,
        "Tool name is not available.",
    )
    assert calls == []
