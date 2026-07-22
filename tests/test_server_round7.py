"""Round-7 names retained by Agent use the new declarative schemas."""

from __future__ import annotations

import anyio

import vibecad.server as server


def test_move_and_rotate_are_registry_tools_not_legacy_handlers() -> None:
    tools = {tool.name: tool for tool in anyio.run(server._handle_list_tools).tools}
    assert {"move_part", "rotate_part"} <= set(tools)
    for name in ("move_part", "rotate_part"):
        assert set(tools[name].inputSchema["properties"]) == {
            "schema_version",
            "task_id",
            "expected_generation",
            "target",
            "arguments",
            "preserve",
            "acceptance_json",
        }
        assert tools[name].inputSchema["additionalProperties"] is False


def test_round7_removed_feature_names_stay_absent() -> None:
    names = {tool.name for tool in anyio.run(server._handle_list_tools).tools}
    assert names.isdisjoint({"extrude_profile", "add_hole"})
    assert not hasattr(server, "extrude_profile")
