# tests/test_multiview.py
"""multiview：2×2 工程图拼图纯函数快测（不碰 FreeCAD/TechDraw）。"""
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
