# tests/test_multiview.py
"""multiview：2×2 拼图纯函数快测（不碰 FreeCAD）。"""
import pytest

from vibecad.feedback import multiview

_TET_V = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
_TET_F = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]


def test_multiview_png_smoke():
    png = multiview.multiview_png(
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[{"label": "A", "pos": (3, 3, 0), "visible": True}],
        dims={"L": 10, "W": 10, "H": 10, "bbox": (0, 0, 0, 10, 10, 10)})
    assert png.startswith(b"\x89PNG") and len(png) > 5000  # 4 格拼图显著大于单格


def test_multiview_png_empty_mesh_raises():
    with pytest.raises(ValueError):
        multiview.multiview_png(face_meshes=[], face_labels=[], dims=None)


def test_module_import_purity():
    assert not any(m in getattr(multiview, "__dict__", {})
                   for m in ("matplotlib", "FreeCAD"))
