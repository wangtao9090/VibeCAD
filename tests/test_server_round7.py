# tests/test_server_round7.py
"""server Round7：move_part/rotate_part/extrude_profile 三工具 +
add_hole pattern 透传 + 握手纯净回归。"""
import importlib
import sys

import pytest


@pytest.fixture()
def server(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: None)
    return srv


def _mock_multiview(server, monkeypatch, png=b"\x89PNG mv"):
    monkeypatch.setattr(server._multiview, "render_multiview",
                        lambda shape: (png, {"A": "顶面"}, {"A": {}}, {"E1": {}}))


# ─── move_part：委托 + 附图形态 ───────────────────────────────────────────────


def test_move_part_delegates_and_attaches(server, monkeypatch):
    """move_part 把调用委托给 _transform.move_part，成功后返回 [dict, Image]。"""
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._transform, "move_part",
                        lambda session, name, position:
                        {"ok": True, "name": name, "volume": 24000.0,
                         "move": {"position": list(position)},
                         "labels_stale": True,
                         "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc: {"Box": {"length": 40.0}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)

    out = server.move_part(name="Box", position=[10.0, 5.0, 0.0])
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True
    assert "labels_stale" not in body and "hint" not in body
    assert body["labels"] == {"A": "顶面"}


def test_move_part_failure_structured(server, monkeypatch):
    """move_part 委托抛 ValueError 时返回结构化失败（含中文消息）。"""
    def _boom(session, name, position):
        raise ValueError("name 必须是非空字符串（对象名，见 parts 字段）")

    monkeypatch.setattr(server._transform, "move_part", _boom)
    out = server.move_part(name="", position=[0.0, 0.0, 0.0])
    assert isinstance(out, dict) and out["ok"] is False
    assert "name" in out["message"]


# ─── rotate_part：委托 + 附图形态 ─────────────────────────────────────────────


def test_rotate_part_delegates_and_attaches(server, monkeypatch):
    """rotate_part 把调用委托给 _transform.rotate_part，成功后返回 [dict, Image]。"""
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._transform, "rotate_part",
                        lambda session, name, axis, angle:
                        {"ok": True, "name": name, "volume": 24000.0,
                         "rotate": {"position": [20.0, 15.0, 10.0]},
                         "labels_stale": True,
                         "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc: {"Box": {"length": 40.0}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)

    out = server.rotate_part(name="Box", axis="z", angle=90)
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True
    assert "labels_stale" not in body and "hint" not in body
    assert body["labels"] == {"A": "顶面"}


def test_rotate_part_failure_structured(server, monkeypatch):
    """rotate_part 委托抛 ValueError 时返回结构化失败（含中文消息 axis）。"""
    def _boom(session, name, axis, angle):
        raise ValueError("axis 必须是 x/y/z（得到 'w'）")

    monkeypatch.setattr(server._transform, "rotate_part", _boom)
    out = server.rotate_part(name="Box", axis="w", angle=90)
    assert isinstance(out, dict) and out["ok"] is False
    assert "axis" in out["message"]


# ─── extrude_profile：委托 + 附图形态 ─────────────────────────────────────────


def test_extrude_profile_delegates_and_attaches(server, monkeypatch):
    """extrude_profile 把调用委托给 _sketch.extrude_profile，成功后返回 [dict, Image]。"""
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._sketch, "extrude_profile",
                        lambda session, profile, height, face, offset, operation:
                        {"ok": True, "name": "Profile", "volume": 13000.0,
                         "extrude": {"profile": "rect", "area": 200.0,
                                     "height": 5.0, "operation": operation},
                         "parametric": False,
                         "labels_stale": True,
                         "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc: {"Box": {"length": 40.0}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)

    out = server.extrude_profile(profile={"type": "rect", "length": 20, "width": 10},
                                 height=5, face="A", operation="pad")
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True
    assert "labels_stale" not in body and "hint" not in body
    assert body["labels"] == {"A": "顶面"}


def test_extrude_profile_failure_structured(server, monkeypatch):
    """extrude_profile 委托抛 ValueError 时返回结构化失败（含中文消息 operation）。"""
    def _boom(session, profile, height, face, offset, operation):
        raise ValueError("operation 必须是 pad 或 pocket（得到 'carve'）")

    monkeypatch.setattr(server._sketch, "extrude_profile", _boom)
    out = server.extrude_profile(profile={"type": "circle", "radius": 3},
                                 height=5, operation="carve")
    assert isinstance(out, dict) and out["ok"] is False
    assert "operation" in out["message"]


# ─── 守卫拦截（一条覆盖四工具路径验证守卫生效）──────────────────────────────────


def test_runtime_guard_blocks_new_tools(monkeypatch):
    """未就绪时三个新工具及 add_hole 均被守卫拦截（ok:False 立即返回）。"""
    import vibecad.server as srv

    def _not_ready():
        return {"ok": False, "message": "FreeCAD 运行时未就绪，请先调用 ensure_runtime"}

    monkeypatch.setattr(srv, "_runtime_guard", _not_ready)

    out_move = srv.move_part(name="Box", position=[0.0, 0.0, 0.0])
    assert out_move["ok"] is False and "未就绪" in out_move["message"]

    out_rotate = srv.rotate_part(name="Box", axis="z", angle=90)
    assert out_rotate["ok"] is False and "未就绪" in out_rotate["message"]

    out_extrude = srv.extrude_profile(profile={"type": "circle", "radius": 3}, height=5)
    assert out_extrude["ok"] is False and "未就绪" in out_extrude["message"]

    out_hole = srv.add_hole(face="A", diameter=6,
                            pattern={"type": "linear", "count": 3, "spacing": 10})
    assert out_hole["ok"] is False and "未就绪" in out_hole["message"]


# ─── add_hole pattern 透传 ────────────────────────────────────────────────────


def test_add_hole_pattern_passthrough(server, monkeypatch):
    """add_hole pattern 参数透传到 _features.add_hole，并且 mock 收到的 pattern 实参正确。"""
    class _Shape:
        pass

    recorded = {}

    def _mock_add_hole(session, face, diameter, depth, offset, pattern=None):
        recorded["pattern"] = pattern
        return {"ok": True, "name": "Hole", "volume": 23000.0,
                "holes": {"count": 3, "pattern": pattern, "diameter": diameter},
                "labels_stale": True,
                "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}

    monkeypatch.setattr(server._features, "add_hole", _mock_add_hole)
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc: {})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)

    pat = {"type": "linear", "count": 3, "spacing": 10}
    server.add_hole(face="A", diameter=6, pattern=pat)
    assert recorded["pattern"] == pat


# ─── 握手纯净回归（放文件末尾）────────────────────────────────────────────────


def test_server_reload_no_freecad_in_sys_modules():
    """reload server 后 FreeCAD 与 matplotlib 不在 sys.modules——握手必须秒回。"""
    # 先清理已缓存的 FreeCAD/matplotlib，确保纯净测试环境
    for mod in list(sys.modules.keys()):
        if mod == "FreeCAD" or mod.startswith("FreeCAD.") or mod == "matplotlib" \
                or mod.startswith("matplotlib."):
            del sys.modules[mod]

    import vibecad.server  # 若导入会触发 FreeCAD 则此行就会失败
    importlib.reload(vibecad.server)

    assert "FreeCAD" not in sys.modules, "server 模块级不得 import FreeCAD（握手秒回纪律）"
    assert "matplotlib" not in sys.modules, "server 模块级不得 import matplotlib"
