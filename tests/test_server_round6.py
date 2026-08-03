"""Round-6 legacy modeling adapters are replaced by registry-derived tools."""

from __future__ import annotations

import anyio

import vibecad.server as server


def test_round6_old_modeling_names_are_absent_and_agent_names_are_present() -> None:
    names = [tool.name for tool in anyio.run(server._handle_list_tools).tools]
    assert "add_box" not in names
    assert "modify_part" not in names
    assert "create_box" in names
    assert "modify_parameter" in names


def test_round6_image_normalizer_is_not_a_public_boundary() -> None:
    assert not hasattr(server, "_attach_view")
    assert not hasattr(server, "_modeling")
    assert not hasattr(server, "_modify")
