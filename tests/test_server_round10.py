"""SDK version and independently written annotation contract."""

from __future__ import annotations

import hashlib
import json

import anyio

import vibecad
import vibecad.server as server


def test_server_reports_package_version() -> None:
    options = server.mcp._mcp_server.create_initialization_options()
    assert options.server_version == vibecad.__version__


def test_tool_annotation_safety_mapping_is_independent_and_exact() -> None:
    expected = {
        "ping": (True, False, True, False),
        "get_runtime_status": (False, False, True, False),
        "ensure_runtime": (False, True, True, True),
        "uninstall_runtime": (False, True, True, False),
        "get_capabilities": (True, False, True, False),
        "create_project": (False, False, True, True),
        "get_project": (False, False, True, False),
        "create_task": (False, False, False, False),
        "get_task": (False, False, True, False),
        "submit_model_program": (False, True, True, False),
        "resume_task": (False, True, True, False),
        "accept_draft": (False, True, True, False),
        "reject_draft": (False, True, True, False),
        "export_task_artifacts": (False, False, True, False),
        "create_box": (False, False, True, False),
        "create_cylinder": (False, False, True, False),
        "inspect_model": (False, False, True, False),
        "modify_parameter": (False, True, True, False),
        "move_part": (False, True, True, False),
        "rotate_part": (False, True, True, False),
    }
    actual = {
        tool.name: (
            tool.annotations.readOnlyHint,
            tool.annotations.destructiveHint,
            tool.annotations.idempotentHint,
            tool.annotations.openWorldHint,
        )
        for tool in anyio.run(server._handle_list_tools).tools
    }
    assert actual == expected


def test_live_sdk_projection_matches_independent_frozen_digest_and_has_no_extras() -> None:
    tools = anyio.run(server._handle_list_tools).tools
    projection = [
        {
            "name": tool.name,
            "inputSchema": tool.inputSchema,
            "outputSchema": tool.outputSchema,
            "annotations": {
                "readOnlyHint": tool.annotations.readOnlyHint,
                "destructiveHint": tool.annotations.destructiveHint,
                "idempotentHint": tool.annotations.idempotentHint,
                "openWorldHint": tool.annotations.openWorldHint,
            },
        }
        for tool in tools
    ]
    raw = json.dumps(
        projection,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert hashlib.sha256(raw).hexdigest() == (
        "081b4175baa8081550cf617f694dfd46f0a711f726bba2db00b66e94252a7a75"
    )
    for tool in tools:
        assert tool.title is None
        assert tool.description is None
        assert tool.icons is None
        assert tool.meta is None
        assert tool.execution is None
        assert tool.annotations.title is None
