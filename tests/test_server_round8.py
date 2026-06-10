# tests/test_server_round8.py
"""server Round8：new_part/place_part/align_parts 三工具 +
describe_part 装配分流 + export_part split + 握手纯净回归。"""
import importlib
import sys

import pytest


@pytest.fixture()
def server(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: None)
    return srv


def _mock_multiview(server, monkeypatch, png=b"\x89PNG mv"):
    """Round 8：render_multiview 接受 part_map 关键字参数。"""
    monkeypatch.setattr(server._multiview, "render_multiview",
                        lambda shape, part_map=None: (png, {"A": "顶面"}, {"A": {}}, {"E1": {}}))


def _mock_assembly_shape(server, monkeypatch, shape=None):
    """同时 mock get_assembly_shape 和 get_result_shape。"""
    if shape is None:
        class _Shape:
            pass
        shape = _Shape()
    monkeypatch.setattr(server._session, "get_assembly_shape", lambda: shape)
    monkeypatch.setattr(server._session, "get_result_shape", lambda: shape)


# ─── new_part：委托 + 附图 ─────────────────────────────────────────────────────


def test_new_part_delegates_and_attaches(server, monkeypatch):
    """new_part 委托给 session.new_part，成功后返回 [dict, Image]（含标签表）。"""
    from mcp.server.fastmcp import Image

    monkeypatch.setattr(server._session, "new_part",
                        lambda name: {"part": name, "implicit_part": None})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc, session=None: {"盖板": {}})
    _mock_assembly_shape(server, monkeypatch)
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)

    out = server.new_part("盖板")
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True
    assert body["part"] == "盖板"
    assert body["labels"] == {"A": "顶面"}


def test_new_part_failure_structured(server, monkeypatch):
    """new_part 委托抛 ValueError 时返回结构化失败。"""
    monkeypatch.setattr(server._session, "new_part",
                        lambda name: (_ for _ in ()).throw(ValueError("盖板 已存在")))
    out = server.new_part("盖板")
    assert out["ok"] is False and "已存在" in out["message"]


# ─── place_part：委托 + 附图 ──────────────────────────────────────────────────


def test_place_part_delegates_and_attaches(server, monkeypatch):
    """place_part 委托给 _assembly.place_part，成功后返回 [dict, Image]。"""
    from mcp.server.fastmcp import Image

    monkeypatch.setattr(server._assembly, "place_part",
                        lambda session, part, position=None, rotation_axis=None, angle=None:
                        {"ok": True, "part": part,
                         "placement": {"position": position},
                         "interference": [],
                         "labels_stale": True,
                         "hint": "零件位置已更新，调用 render_part(annotate='faces') 查看最新标注"})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc, session=None: {"底板": {}})
    _mock_assembly_shape(server, monkeypatch)
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)

    out = server.place_part(part="底板", position=[10.0, 20.0, 0.0])
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True and body["part"] == "底板"
    assert "labels_stale" not in body and "hint" not in body


def test_place_part_failure_structured(server, monkeypatch):
    """place_part 委托抛 ValueError 时返回结构化失败（含 axis 关键字）。"""
    def _boom(session, part, position=None, rotation_axis=None, angle=None):
        raise ValueError("axis 必须是 x/y/z（得到 'w'）")

    monkeypatch.setattr(server._assembly, "place_part", _boom)
    out = server.place_part(part="底板", rotation_axis="w", angle=45)
    assert out["ok"] is False and "axis" in out["message"]


# ─── align_parts：委托 + 附图 ─────────────────────────────────────────────────


def test_align_parts_delegates_and_attaches(server, monkeypatch):
    """align_parts 委托给 _assembly.align_parts，成功后返回 [dict, Image]。"""
    from mcp.server.fastmcp import Image

    monkeypatch.setattr(server._assembly, "align_parts",
                        lambda session, moving_part, moving_face, target_part, target_face,
                        offset=(0.0, 0.0), gap=0.0, allow_interference=False:
                        {"ok": True, "moving_part": moving_part, "target_part": target_part,
                         "placement": {"position": [0, 0, 10.0], "rotation_angle_deg": 0},
                         "gap": gap, "interference": [],
                         "labels_stale": True,
                         "hint": "装配对齐完成，调用 render_part(annotate='faces') 查看最新标注"})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc, session=None: {"底板": {}, "盖板": {}})
    _mock_assembly_shape(server, monkeypatch)
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)

    out = server.align_parts(moving_part="盖板", moving_face="A",
                             target_part="底板", target_face="B")
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True
    assert body["moving_part"] == "盖板" and body["target_part"] == "底板"
    assert "labels_stale" not in body and "hint" not in body


def test_align_parts_failure_structured(server, monkeypatch):
    """align_parts 委托抛 RuntimeError（干涉）时返回结构化失败。"""
    def _boom(session, moving_part, moving_face, target_part, target_face,
              offset=(0.0, 0.0), gap=0.0, allow_interference=False):
        raise RuntimeError("装配干涉：盖板↔底板(12.000mm³)——零件重叠")

    monkeypatch.setattr(server._assembly, "align_parts", _boom)
    out = server.align_parts(moving_part="盖板", moving_face="A",
                             target_part="底板", target_face="B")
    assert out["ok"] is False and "干涉" in out["message"]


# ─── 守卫拦截（三个新工具及 describe_part）────────────────────────────────────


def test_runtime_guard_blocks_assembly_tools(monkeypatch):
    """未就绪时三个新装配工具均被守卫拦截（ok:False 立即返回）。"""
    import vibecad.server as srv

    def _not_ready():
        return {"ok": False, "message": "FreeCAD 运行时未就绪，请先调用 ensure_runtime"}

    monkeypatch.setattr(srv, "_runtime_guard", _not_ready)

    out_new = srv.new_part("盖板")
    assert out_new["ok"] is False and "未就绪" in out_new["message"]

    out_place = srv.place_part(part="底板", position=[0.0, 0.0, 0.0])
    assert out_place["ok"] is False and "未就绪" in out_place["message"]

    out_align = srv.align_parts(moving_part="盖板", moving_face="A",
                                target_part="底板", target_face="B")
    assert out_align["ok"] is False and "未就绪" in out_align["message"]


# ─── describe_part 装配分流 ───────────────────────────────────────────────────


def test_describe_part_assembly_mode_dispatches(server, monkeypatch):
    """装配模式（_parts 非空）：describe_part 返回 describe_assembly 结果。"""
    # 临时替换 _parts 属性（只测分流逻辑）
    monkeypatch.setattr(type(server._session), "_parts",
                        property(lambda self: {"底板": {}, "盖板": {}}),
                        raising=False)
    assembly_result = {"parts": {"底板": {"volume": 24000}, "盖板": {"volume": 12000}},
                       "assembly_bbox": {"x": 60, "y": 40, "z": 15},
                       "interference": []}
    monkeypatch.setattr(server._feedback_text, "describe_assembly",
                        lambda session: assembly_result)

    out = server.describe_part()
    assert "parts" in out and "assembly_bbox" in out
    assert out["parts"]["底板"]["volume"] == 24000


def test_describe_part_single_mode_unchanged(server, monkeypatch):
    """单零件模式（_parts 空）：describe_part 保持原格式（volume/bbox/...）。"""
    monkeypatch.setattr(type(server._session), "_parts",
                        property(lambda self: {}),
                        raising=False)
    monkeypatch.setattr(server._session, "get_result_shape",
                        lambda: type("S", (), {"Volume": 24000,
                                               "BoundBox": type("B", (), {
                                                   "XLength": 60, "YLength": 40, "ZLength": 20})(),
                                               "isValid": lambda self: True,
                                               "Solids": [object()],
                                               "Shells": [],
                                               "CenterOfMass": type("C", (), {
                                                   "x": 30, "y": 20, "z": 10})()})())
    out = server.describe_part()
    assert "volume" in out and out["volume"] == 24000


# ─── export_part split 参数透传 ───────────────────────────────────────────────


def test_export_part_split_passthrough(server, monkeypatch):
    """export_part split=True 参数透传到 _export.export_part。"""
    recorded = {}

    def _mock_export(session, output_dir, *, fmt, split=False):
        recorded["split"] = split
        recorded["fmt"] = fmt
        return {"ok": True, "step": ["/tmp/A.step", "/tmp/B.step"], "stl": None, "gltf": None}

    monkeypatch.setattr(server._export, "export_part", _mock_export)
    out = server.export_part("/tmp", fmt="step", split=True)
    assert out["ok"] is True
    assert recorded["split"] is True
    assert recorded["fmt"] == "step"
    # split=True 时 step 字段为列表
    assert isinstance(out["step"], list)


# ─── 握手纯净回归（放文件末尾）────────────────────────────────────────────────


def test_server_reload_no_freecad_in_sys_modules_r8():
    """reload server（含装配工具）后 FreeCAD/matplotlib 不在 sys.modules——握手必须秒回。"""
    for mod in list(sys.modules.keys()):
        if mod == "FreeCAD" or mod.startswith("FreeCAD.") or mod == "matplotlib" \
                or mod.startswith("matplotlib."):
            del sys.modules[mod]

    import vibecad.server
    importlib.reload(vibecad.server)

    assert "FreeCAD" not in sys.modules, "server 模块级不得 import FreeCAD（握手秒回纪律）"
    assert "matplotlib" not in sys.modules, "server 模块级不得 import matplotlib"
