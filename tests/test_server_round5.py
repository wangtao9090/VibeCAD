"""server Round5：annotate 渲染双内容、特征工具委托与失败结构化、握手纯净回归。"""
import json
import sys

import pytest


@pytest.fixture()
def server(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: None)
    return srv


def test_render_part_annotate_returns_image_and_table(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(
        server._annotate, "render_annotated",
        lambda shape, mode, edges_of, view: (b"\x89PNG fake", {"A": "顶面"}, {"A": {}}, {}))
    recorded = {}
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: recorded.update(
                            f=faces, e=edges, s=shown))
    out = server.render_part(view="iso", annotate="faces")
    assert isinstance(out, list) and isinstance(out[0], Image)
    table = json.loads(out[1])
    assert table["labels"]["A"] == "顶面" and recorded["f"] == {"A": {}}
    assert recorded["s"] == {"A"}  # shown=本次标签表实际展示的键集合


def test_render_part_no_annotate_unchanged(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(server._render, "render_png", lambda shape, view: b"\x89PNG plain")
    out = server.render_part(view="front")
    assert isinstance(out, Image)


def test_render_part_annotate_invalid_value(server, monkeypatch):
    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    out = server.render_part(view="iso", annotate="bogus")
    assert out["ok"] is False


def test_render_part_edges_of_resolves_face_label(server, monkeypatch):
    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(server._session, "resolve_face", lambda label: 3)
    seen = {}

    def _fake_annotated(shape, mode, edges_of, view):
        seen["edges_of"] = edges_of
        return (b"\x89PNG fake", {}, {}, {})

    monkeypatch.setattr(server._annotate, "render_annotated", _fake_annotated)
    monkeypatch.setattr(server._session, "set_labels", lambda faces, edges, shown=None: None)
    out = server.render_part(view="iso", annotate="edges", edges_of="A")
    assert isinstance(out, list) and seen["edges_of"] == 3


def test_add_hole_delegates(server, monkeypatch):
    monkeypatch.setattr(server._features, "add_hole",
                        lambda session, face, diameter, depth, offset:
                        {"ok": True, "face": face, "diameter": diameter})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: object())
    monkeypatch.setattr(server._multiview, "render_multiview",
                        lambda shape: (b"\x89PNG", {}, {}, {}))
    monkeypatch.setattr(server._session, "set_labels", lambda f, e, shown=None: None)
    out = server.add_hole(face="A", diameter=8)
    assert isinstance(out, list)  # 成功路径返回 [dict, Image]
    assert out[0]["ok"] is True and out[0]["face"] == "A"


def test_add_hole_label_expired_structured(server, monkeypatch):
    from vibecad.engine.naming import LabelExpiredError

    def _boom(session, face, diameter, depth, offset):
        raise LabelExpiredError("标签 A 已过期")

    monkeypatch.setattr(server._features, "add_hole", _boom)
    out = server.add_hole(face="A", diameter=8)
    assert out["ok"] is False and "过期" in out["message"]


def test_fillet_and_chamfer_delegate(server, monkeypatch):
    monkeypatch.setattr(server._features, "fillet_edges",
                        lambda session, edges, radius: {"ok": True, "edges": edges})
    monkeypatch.setattr(server._features, "chamfer_edges",
                        lambda session, edges, size: {"ok": True, "edges": edges})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: object())
    monkeypatch.setattr(server._multiview, "render_multiview",
                        lambda shape: (b"\x89PNG", {}, {}, {}))
    monkeypatch.setattr(server._session, "set_labels", lambda f, e, shown=None: None)
    fillet_out = server.fillet_edges(edges=["E1"], radius=2)
    chamfer_out = server.chamfer_edges(edges=["E2"], size=1)
    # 成功路径返回 [dict, Image]
    assert isinstance(fillet_out, list) and fillet_out[0]["ok"] is True
    assert isinstance(chamfer_out, list) and chamfer_out[0]["ok"] is True


def test_render_part_edges_of_without_annotate_rejected(server, monkeypatch):
    """edges_of 不带 annotate='edges' 必须显式拒绝，不得静默忽略呈成功形态。"""
    monkeypatch.setattr(server._session, "get_result_shape", lambda: object())
    out = server.render_part(view="iso", edges_of="A")
    assert out["ok"] is False and "edges_of" in out["message"]
    out2 = server.render_part(view="iso", annotate="faces", edges_of="A")
    assert out2["ok"] is False and "edges_of" in out2["message"]


def test_mcp_call_tool_render_annotate_contract(server, monkeypatch):
    """锁死协议契约：list 返回 → [ImageContent, TextContent(json)]。mcp 升级的回归保险。"""
    import anyio
    from mcp.types import ImageContent, TextContent

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(
        server._annotate, "render_annotated",
        lambda shape, mode, edges_of, view: (b"\x89PNGx", {"A": "顶面"}, {"A": {}}, {}))
    monkeypatch.setattr(server._session, "set_labels", lambda faces, edges, shown=None: None)
    blocks = anyio.run(lambda: server.mcp.call_tool("render_part", {"annotate": "faces"}))
    # mcp 1.27 实测：call_tool 直接返回 content blocks 列表（tuple 兜底兼容未来变化）
    content = blocks[0] if isinstance(blocks, tuple) else blocks
    assert isinstance(content[0], ImageContent)
    assert isinstance(content[1], TextContent)
    assert json.loads(content[1].text)["ok"] is True


def test_mcp_call_tool_render_failure_contract(server, monkeypatch):
    import anyio
    from mcp.types import TextContent

    def _boom():
        raise RuntimeError("无活动文档")

    monkeypatch.setattr(server._session, "get_result_shape", _boom)
    blocks = anyio.run(lambda: server.mcp.call_tool("render_part", {"annotate": "faces"}))
    content = blocks[0] if isinstance(blocks, tuple) else blocks
    payload = json.loads([b for b in content if isinstance(b, TextContent)][0].text)
    assert payload["ok"] is False


def test_feature_tools_guarded(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: {"ok": False, "message": "not ready"})
    assert srv.add_hole(face="A", diameter=8)["ok"] is False
    assert srv.fillet_edges(edges=["E1"], radius=2)["ok"] is False
    assert srv.chamfer_edges(edges=["E1"], size=1)["ok"] is False
    assert srv.render_part(annotate="faces")["ok"] is False


def test_handshake_purity_unchanged():
    for mod in ("FreeCAD", "matplotlib"):
        sys.modules.pop(mod, None)
    import importlib

    import vibecad.server as srv
    importlib.reload(srv)
    assert "FreeCAD" not in sys.modules and "matplotlib" not in sys.modules
