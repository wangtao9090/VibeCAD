# tests/test_annotate.py
"""annotate：相机方向/网格法向/标注 PNG。纯函数快测（不碰 FreeCAD）。"""
import math

import pytest

from vibecad.feedback import annotate

_TET_V = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
_TET_F = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]


def test_camera_direction_iso_unit():
    d = annotate.camera_direction("iso")
    assert abs(math.dist(d, (0, 0, 0)) - 1.0) < 1e-9
    assert d[2] > 0  # iso 从上方看


def test_camera_direction_top_is_up():
    d = annotate.camera_direction("top")
    assert d[2] > 0.99


def test_camera_direction_invalid():
    with pytest.raises(ValueError):
        annotate.camera_direction("bogus")


def test_mesh_normal_z_face():
    verts = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]
    n = annotate.mesh_normal(verts, [(0, 1, 2), (0, 2, 3)])
    assert abs(n[2] - 1.0) < 1e-9  # 朝 +Z


def test_mesh_normal_degenerate_returns_zero():
    assert annotate.mesh_normal([(0, 0, 0)] * 3, [(0, 1, 2)]) == (0.0, 0.0, 0.0)


def test_largest_triangle_centroid():
    # 两个三角：小的(面积50) + 大的(面积200) → 锚点应是大三角质心
    verts = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (20, 0, 0), (0, 20, 0)]
    facets = [(0, 1, 2), (0, 3, 4)]
    c = annotate.largest_triangle_centroid(verts, facets)
    assert c == pytest.approx((20 / 3, 20 / 3, 0.0))


def test_largest_triangle_centroid_prefers_camera_facing():
    # 背向大三角（绕向反转 → 法向 -Z，面积 200）+ 朝向小三角（法向 +Z，面积 50）
    verts = [(0, 0, 0), (20, 0, 0), (0, 20, 0), (30, 0, 0), (40, 0, 0), (30, 10, 0)]
    facets = [(0, 2, 1), (3, 4, 5)]
    cam = (0.0, 0.0, 1.0)
    # 传 cam：跳过背向大三角，锚点取朝相机的小三角质心
    c = annotate.largest_triangle_centroid(verts, facets, cam)
    assert c == pytest.approx((100 / 3, 10 / 3, 0.0))
    # 不传 cam：仍取全局最大（背向大三角）
    c0 = annotate.largest_triangle_centroid(verts, facets)
    assert c0 == pytest.approx((20 / 3, 20 / 3, 0.0))
    # 全部背向时退回全局最大（不返回空）
    c_back = annotate.largest_triangle_centroid(verts, [(0, 2, 1)], cam)
    assert c_back == pytest.approx((20 / 3, 20 / 3, 0.0))


def test_visibility_note_top_normal_from_front():
    # 顶面法向 (0,0,1)：front 视角不可见，但 iso/top 可见 → 注里要给出 top
    note = annotate.visibility_note((0, 0, 1), "front")
    assert "当前视角不可见" in note
    assert "top" in note


def test_visibility_note_bottom_never_visible():
    # 底面 (0,0,-1)：全部 5 个预设视角点积 ≤ 0 → 直说预设视角都看不见
    assert "预设视角均不可见" in annotate.visibility_note((0, 0, -1), "front")


def test_visibility_note_zero_normal():
    # 退化网格零法向 → 同样不能给死路提示
    assert "预设视角均不可见" in annotate.visibility_note((0.0, 0.0, 0.0), "iso")


def test_visibility_note_visible_returns_empty():
    assert annotate.visibility_note((0, 0, 1), "top") == ""


def test_annotated_png_smoke():
    png = annotate.annotated_png(
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[{"label": "A", "pos": (3, 3, 0), "visible": True},
                     {"label": "B", "pos": (0, 0, 5), "visible": False}],
        edge_labels=[{"label": "E1", "pos": (5, 0, 0),
                      "polyline": [(0, 0, 0), (10, 0, 0)]}],
        dims={"L": 10, "W": 10, "H": 10, "bbox": (0, 0, 0, 10, 10, 10)},
        view="iso")
    assert png.startswith(b"\x89PNG") and len(png) > 1000


def test_annotated_png_hidden_edge_smoke():
    # visible=False 的边走虚线弱化分支，同样要产出合法 PNG
    png = annotate.annotated_png(
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[],
        edge_labels=[{"label": "E2", "pos": (0, 5, 0), "visible": False,
                      "polyline": [(0, 0, 0), (0, 10, 0)]}],
        view="iso")
    assert png.startswith(b"\x89PNG") and len(png) > 1000


def test_annotated_png_empty_mesh_raises():
    with pytest.raises(ValueError):
        annotate.annotated_png(face_meshes=[], face_labels=[], edge_labels=[], view="iso")


def test_render_annotated_edges_of_negative_rejected():
    """edges_of=-1 必须 ValueError 含"越界"（Python 负索引会静默取最后一面，违反纪律）。
    校验发生在 silence_fd1 内、tessellate 之前（只需 shape.Faces 可被 len() 调用）。
    silence_fd1 仅用 os.dup，不 import FreeCAD，dev venv 可直接调用。"""

    class _FakeFace:
        """最小化 fake face：tessellate 前校验触发，不会真正调用这些方法。"""
        pass

    class _FakeShape:
        Faces = [_FakeFace()] * 6  # 6 个面：合法索引 0..5

    with pytest.raises(ValueError, match="越界"):
        annotate.render_annotated(_FakeShape(), mode="edges", edges_of=-1)


def test_render_annotated_faces_rejects_edges_of():
    """faces 模式下 edges_of 必须显式拒绝（静默忽略会让调用方误以为过滤生效）。
    校验在 faces 分支首行、碰 shape 之前触发——fake shape 不会被访问。"""

    class _FakeShape:
        pass

    with pytest.raises(ValueError, match="edges_of"):
        annotate.render_annotated(_FakeShape(), mode="faces", edges_of=0)


def test_module_import_purity():
    # 真正的纯净断言：annotate 模块对象自身的全局命名空间不含 matplotlib/FreeCAD
    assert not any(m in getattr(annotate, "__dict__", {}) for m in ("matplotlib", "FreeCAD"))
