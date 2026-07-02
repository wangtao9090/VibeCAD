# tests/test_multiview.py
"""multiview：2×2 工程图拼图纯函数快测（不碰 FreeCAD/TechDraw）。"""
import math

import pytest

from vibecad.feedback import multiview

_TET_V = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
_TET_F = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]

# fake 工程图视图：40×20 矩形外框 + 一条隐藏线 + 一个可见圆
_RECT = [(0, 0), (40, 0), (40, 20), (0, 20), (0, 0)]
_FAKE_VIEW = {"vis": [_RECT], "hid": [[(10, 0), (10, 20)]],
              "circles": [(20, 10, 6, True)]}
_FAKE_ENG = {"front": _FAKE_VIEW, "right": _FAKE_VIEW, "top": _FAKE_VIEW}


def test_multiview_png_smoke():
    png = multiview.multiview_png(
        eng_views=_FAKE_ENG,
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[{"label": "A", "pos": (3, 3, 0), "visible": True}],
        dims={"L": 10, "W": 10, "H": 10, "bbox": (0, 0, 0, 10, 10, 10)})
    assert png.startswith(b"\x89PNG") and len(png) > 8000  # 4 格拼图显著大于单格


def test_multiview_png_dims_from_bbox():
    # fake 矩形 40×20：总尺寸/⌀/定位标注全部从投影 bbox 推导——不抛错即可
    # （标注数字正确性靠真机慢测/黑盒人眼验证）
    png = multiview.multiview_png(eng_views={"top": _FAKE_VIEW}, face_meshes=[],
                                  face_labels=[], dims=None)
    assert png.startswith(b"\x89PNG")


def test_multiview_png_multi_circle_positions():
    # 两个不同径可见整圆 + 一个隐藏同径整圆：每个去重后可见圆都有 ⌀+定位尺寸，
    # 隐藏圆不标注——不抛错即可（标注内容正确性靠真机/黑盒人眼验证）
    view = {"vis": [_RECT], "hid": [],
            "circles": [(12, 10, 4, True), (30, 10, 3, True), (12, 10, 4, False)]}
    png = multiview.multiview_png(eng_views={"top": view}, face_meshes=[],
                                  face_labels=[], dims=None)
    assert png.startswith(b"\x89PNG")


def test_multiview_png_empty_raises():
    with pytest.raises(ValueError):
        multiview.multiview_png(eng_views={}, face_meshes=[], face_labels=[], dims=None)


def test_module_import_purity():
    assert not any(m in getattr(multiview, "__dict__", {})
                   for m in ("matplotlib", "FreeCAD", "TechDraw"))


def test_same_radius_circles_both_positioned():
    """同径双孔：⌀ 按径去重只标一次，但定位尺寸两孔都要有（解耦回归）。
    纯函数层断言方式：渲染不抛错 + PNG 尺寸增长（精确坐标断言在真机慢测）。"""
    rect = [[(0, 0), (60, 0)], [(60, 0), (60, 40)], [(60, 40), (0, 40)], [(0, 40), (0, 0)]]
    circle1 = [[(15 + 5 * math.cos(t / 7.64 * 3.14159 / 24), 20 + 5 * math.sin(t / 7.64))
                for t in range(3)]]  # 简化折线即可，circles 列表才是断言对象
    eng = {"front": {"vis": rect, "hid": [], "circles": []},
           "right": {"vis": rect, "hid": [], "circles": []},
           "top": {"vis": rect + circle1,
                   "hid": [],
                   "circles": [(15.0, 20.0, 5.0, True), (45.0, 20.0, 5.0, True)]}}
    png = multiview.multiview_png(
        eng_views=eng,
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[], dims=None)
    assert png.startswith(b"\x89PNG")


def test_same_radius_circles_each_get_exact_position_dims(monkeypatch):
    """同径双孔定位尺寸精确坐标断言（R6a backlog 补测）：⌀ 标注按径去重只调用
    一次，但 _dim_h/_dim_v（定位尺寸）必须对两孔圆心各调用一次，且数值精确等于
    "圆心 - 视图包围盒基准边"。非对称取证体：两孔坐标不同，分别校验，不能靠
    单一断言"蒙对"。"""
    rect = [(0, 0), (60, 0), (60, 40), (0, 40), (0, 0)]
    # top 视图：bbox 由 vis 折线决定 → x0,y0,x1,y1 = 0,0,60,40；两个同径孔
    # 圆心 (15,20) 与 (45,20)，半径均为 5
    top_view = {"vis": [rect], "hid": [],
                "circles": [(15.0, 20.0, 5.0, True), (45.0, 20.0, 5.0, True)]}

    dim_h_calls: list[tuple] = []
    dim_v_calls: list[tuple] = []
    diameter_texts: list[str] = []
    orig_dim_h, orig_dim_v = multiview._dim_h, multiview._dim_v

    def fake_dim_h(ax, x0, x1, y, text, off):
        dim_h_calls.append((x0, x1, y, text, off))
        return orig_dim_h(ax, x0, x1, y, text, off)

    def fake_dim_v(ax, y0, y1, x, text, off):
        dim_v_calls.append((y0, y1, x, text, off))
        return orig_dim_v(ax, y0, y1, x, text, off)

    monkeypatch.setattr(multiview, "_dim_h", fake_dim_h)
    monkeypatch.setattr(multiview, "_dim_v", fake_dim_v)

    import matplotlib.axes  # noqa: PLC0415
    orig_annotate = matplotlib.axes.Axes.annotate

    def fake_annotate(self, text, *a, **kw):
        if text.startswith("⌀"):
            diameter_texts.append(text)
        return orig_annotate(self, text, *a, **kw)

    monkeypatch.setattr(matplotlib.axes.Axes, "annotate", fake_annotate)

    png = multiview.multiview_png(eng_views={"top": top_view}, face_meshes=[],
                                  face_labels=[], dims=None)
    assert png.startswith(b"\x89PNG")

    # ⌀ 标注按半径去重：同径两孔只应产生一次 ⌀ 标注调用（不能因为定位尺寸补全
    # 而误伤 ⌀ 去重，两者必须继续解耦）
    assert diameter_texts == ["⌀10"], diameter_texts

    # 定位尺寸的 off 恒为 +6（区别于总尺寸线 off=-6），据此筛出定位尺寸调用
    pos_h = [c for c in dim_h_calls if c[4] == 6]
    pos_v = [c for c in dim_v_calls if c[4] == 6]

    # 两孔水平定位：x0=0（基准边）固定，x1=圆心 cx，y=y1=40（顶边）
    assert (0, 15.0, 40, "15", 6) in pos_h, pos_h
    assert (0, 45.0, 40, "45", 6) in pos_h, pos_h
    assert len(pos_h) == 2, "同径两孔都应有水平定位尺寸，不能仅首孔"

    # 两孔竖直定位：y0=0（基准边）固定，y1=圆心 cy=20，x=x1=60（右边）
    assert (0, 20.0, 60, "20", 6) in pos_v, pos_v
    assert pos_v.count((0, 20.0, 60, "20", 6)) == 2, (
        "两孔圆心 y 相同但必须各自触发一次定位尺寸调用（不能因坐标重复被去重）")
    assert len(pos_v) == 2, "同径两孔都应有竖直定位尺寸，不能仅首孔"
