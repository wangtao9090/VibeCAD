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
                        lambda faces, edges: recorded.update(f=faces, e=edges))
    out = server.render_part(view="iso", annotate="faces")
    assert isinstance(out, list) and isinstance(out[0], Image)
    table = json.loads(out[1])
    assert table["labels"]["A"] == "顶面" and recorded["f"] == {"A": {}}


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
    monkeypatch.setattr(server._session, "set_labels", lambda faces, edges: None)
    out = server.render_part(view="iso", annotate="edges", edges_of="A")
    assert isinstance(out, list) and seen["edges_of"] == 3


def test_add_hole_delegates(server, monkeypatch):
    monkeypatch.setattr(server._features, "add_hole",
                        lambda session, face, diameter, depth, offset:
                        {"ok": True, "face": face, "diameter": diameter})
    out = server.add_hole(face="A", diameter=8)
    assert out["ok"] is True and out["face"] == "A"


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
    assert server.fillet_edges(edges=["E1"], radius=2)["ok"] is True
    assert server.chamfer_edges(edges=["E2"], size=1)["ok"] is True


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
