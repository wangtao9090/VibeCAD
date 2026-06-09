"""PNG 软渲染（matplotlib Agg，纯 CPU 无 GPU）。
matplotlib 仅在函数内 import（保 server 导入轻）。
"""
from __future__ import annotations

from typing import Any

_VIEWS = {"iso": (25, -60), "front": (0, -90), "top": (89, -90), "right": (0, 0), "back": (0, 90)}


def mesh_to_png(verts: list[tuple[float, float, float]], facets: list[tuple[int, int, int]],
                *, view: str = "iso", size: tuple[int, int] = (440, 440)) -> bytes:
    """三角网 → PNG bytes。纯函数（只用 matplotlib），不碰 FreeCAD。"""
    if view not in _VIEWS:
        raise ValueError(f"view 必须是 {sorted(_VIEWS)} 之一（得到 {view!r}）")
    if not verts or not facets:
        raise ValueError("空网格：无顶点或三角面，无法渲染（可能是 tessellate 失败或形状退化）")
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: PLC0415

    varr = np.asarray(verts, dtype=float)
    tris = [[verts[i] for i in f] for f in facets]
    # 逐面法向 → 朗伯明暗（纯 numpy，无 GPU），让 3D 形体与孔洞细节可读
    light = np.array([0.35, 0.30, 0.88])
    light = light / np.linalg.norm(light)
    base = np.array([0.32, 0.55, 0.90])
    facecolors = []
    for f in facets:
        a, b, c = varr[f[0]], varr[f[1]], varr[f[2]]
        n = np.cross(b - a, c - a)
        ln = float(np.linalg.norm(n))
        shade = 0.45 + 0.55 * abs(float(n @ light) / ln) if ln > 1e-12 else 1.0
        facecolors.append((*(base * shade), 1.0))
    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    try:
        ax = fig.add_subplot(111, projection="3d")
        ax.add_collection3d(Poly3DCollection(
            tris, facecolors=facecolors, edgecolor=(0, 0, 0, 0.15), linewidths=0.2))
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        ax.set_xlim(min(xs), max(xs))
        ax.set_ylim(min(ys), max(ys))
        ax.set_zlim(min(zs), max(zs))
        ax.set_box_aspect((max(xs) - min(xs) or 1, max(ys) - min(ys) or 1, max(zs) - min(zs) or 1))
        ax.view_init(*_VIEWS[view])
        ax.set_axis_off()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
    finally:
        plt.close(fig)
    return buf.getvalue()


def render_png(shape: Any, *, view: str = "iso", size: tuple[int, int] = (440, 440)) -> bytes:
    """渲染 FreeCAD Part.Shape 为 PNG（tessellate → mesh_to_png）。"""
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    with silence_fd1():
        verts, facets = shape.tessellate(0.1)
        if not verts or not facets:
            raise RuntimeError("几何断言失败：形状无法镶嵌为网格（空 tessellation）")
        pts = [(p.x, p.y, p.z) for p in verts]
    return mesh_to_png(pts, facets, view=view, size=size)
