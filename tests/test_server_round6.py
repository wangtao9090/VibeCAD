# tests/test_server_round6.py
"""server Round6a：multi 视图、六工具自动附图、附图失败不连坐、协议契约。"""
import json

import pytest


@pytest.fixture()
def server(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: None)
    return srv


def _mock_multiview(server, monkeypatch, png=b"\x89PNG mv"):
    monkeypatch.setattr(server._multiview, "render_multiview",
                        lambda shape: (png, {"A": "顶面"}, {"A": {}}, {"E1": {}}))


def test_render_part_view_multi(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    recorded = {}
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: recorded.update(s=shown))
    out = server.render_part(view="multi")
    assert isinstance(out, list) and isinstance(out[0], Image)
    assert json.loads(out[1])["labels"]["A"] == "顶面"
    assert recorded["s"] == {"A"}


def test_render_part_view_multi_with_annotate_rejected(server, monkeypatch):
    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    out = server.render_part(view="multi", annotate="faces")
    assert out["ok"] is False  # multi 已含标注 iso 格，组合无意义须显式拒绝


def test_add_box_attaches_view(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    out = server.add_box(length=40, width=30, height=20)
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True and body["labels"] == {"A": "顶面"}
    assert "labels_stale" not in body and "hint" not in body


def test_attach_view_render_failure_not_fatal(server, monkeypatch):
    """附图失败不连坐：操作 ok:True 保留 + render_error + 退回 stale 提示。"""
    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})

    def _boom(shape):
        raise RuntimeError("渲染炸了")

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(server._multiview, "render_multiview", _boom)
    out = server.add_box(length=40, width=30, height=20)
    assert isinstance(out, dict) and out["ok"] is True
    assert "渲染炸了" in out["render_error"] and out["labels_stale"] is True


def test_failed_tool_attaches_nothing(server, monkeypatch):
    def _fail(session, length, width, height, position):
        raise ValueError("length 必须 > 0")

    monkeypatch.setattr(server._modeling, "add_box", _fail)
    out = server.add_box(length=-1, width=30, height=20)
    assert isinstance(out, dict) and out["ok"] is False


def test_mcp_contract_tool_with_image(server, monkeypatch):
    """协议契约：[dict, Image] → [TextContent(json), ImageContent]。"""
    import anyio
    from mcp.types import TextContent

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    content = anyio.run(lambda: server.mcp.call_tool("add_box",
                                                     {"length": 40, "width": 30, "height": 20}))
    if isinstance(content, tuple):
        content = content[0]
    kinds = [type(c).__name__ for c in content]
    assert "ImageContent" in kinds and "TextContent" in kinds
    payload = json.loads([c for c in content if isinstance(c, TextContent)][0].text)
    assert payload["ok"] is True
