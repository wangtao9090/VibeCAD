"""Round-5 legacy rendering/features are private after the Agent cutover."""

from __future__ import annotations

import anyio

import vibecad.server as server


def test_round5_legacy_render_and_feature_endpoints_are_not_registered() -> None:
    names = {tool.name for tool in anyio.run(server._handle_list_tools).tools}
    assert names.isdisjoint(
        {
            "render_part",
            "add_hole",
            "fillet_edges",
            "chamfer_edges",
        }
    )


def test_round5_cutover_has_no_legacy_adapter_state() -> None:
    assert not hasattr(server, "render_part")
    assert not hasattr(server, "add_hole")
    assert not hasattr(server, "_features")
    assert not hasattr(server, "_session")
