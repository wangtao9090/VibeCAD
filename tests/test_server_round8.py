"""Round-8 Session assembly endpoints are removed atomically."""

from __future__ import annotations

import anyio

import vibecad.server as server


def test_round8_assembly_endpoints_are_not_public() -> None:
    names = {tool.name for tool in anyio.run(server._handle_list_tools).tools}
    assert names.isdisjoint(
        {
            "new_part",
            "set_active_part",
            "place_part",
            "align_parts",
            "export_part",
            "describe_part",
        }
    )


def test_round8_server_import_does_not_load_session_or_freecad() -> None:
    assert not hasattr(server, "_session")
    assert not hasattr(server, "_assembly")
    assert "FreeCAD" not in __import__("sys").modules
    assert "Part" not in __import__("sys").modules
