# src/vibecad/feedback/annotate.py
"""标注渲染：逐面网格 + 面/边标签 + 包围盒尺寸线（matplotlib Agg，函数内 import）。
annotated_png 为纯函数（吃普通 list 数据）；render_annotated 才碰 FreeCAD Shape。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.feedback.render import _VIEWS

_PALETTE = [(0.32, 0.55, 0.90), (0.36, 0.62, 0.83), (0.30, 0.50, 0.95),
            (0.40, 0.58, 0.86), (0.34, 0.52, 0.92), (0.38, 0.60, 0.88)]
_VIS_DOT = 0.05  # 面法向·相机方向 > 阈值才视为可见（spike 定稿）
_PAD_RATIO = 0.14  # 轴 limits 留白比例（spike 定稿：防尺寸线文字被视锥裁剪）


def camera_direction(view: str) -> tuple[float, float, float]:
    """matplotlib view_init(elev, azim) 对应的单位相机方向（指向相机）。"""
    if view not in _VIEWS:
        raise ValueError(f"view 必须是 {sorted(_VIEWS)} 之一（得到 {view!r}）")
    e, a = (math.radians(d) for d in _VIEWS[view])
    return (math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))


def mesh_normal(verts: list, facets: list) -> tuple[float, float, float]:
    """三角网面积加权平均法向（单位向量；退化网格返回零向量）。"""
    sx = sy = sz = 0.0
    for f in facets:
        ax, ay, az = verts[f[0]]
        bx, by, bz = verts[f[1]]
        cx, cy, cz = verts[f[2]]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        sx += uy * vz - uz * vy
        sy += uz * vx - ux * vz
        sz += ux * vy - uy * vx
    ln = math.sqrt(sx * sx + sy * sy + sz * sz)
    if ln < 1e-12:
        return (0.0, 0.0, 0.0)
    return (sx / ln, sy / ln, sz / ln)


def largest_triangle_centroid(verts: list, facets: list) -> tuple[float, float, float]:
    """面最大三角形的质心——面标签锚点（spike 定稿：CenterOfMass 在带孔面会落孔上）。
    退化/空网格返回首顶点或 (0,0,0)。"""
    best = None
    best_area = -1.0
    for f in facets:
        a, b, c = verts[f[0]], verts[f[1]], verts[f[2]]
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        area = math.sqrt(nx * nx + ny * ny + nz * nz)
        if area > best_area:
            best_area = area
            best = tuple((a[i] + b[i] + c[i]) / 3 for i in range(3))
    if best is None:
        return tuple(verts[0]) if verts else (0.0, 0.0, 0.0)
    return best


def annotated_png(*, face_meshes: list[dict], face_labels: list[dict],
                  edge_labels: list[dict], view: str = "iso",
                  size: tuple[int, int] = (560, 560), dims: dict | None = None) -> bytes:
    """逐面网格 + 标签 → PNG bytes。纯 matplotlib，不碰 FreeCAD。"""
    camera_direction(view)  # 校验 view
    if not face_meshes:
        raise ValueError("空网格：无任何面可渲染（可能是 tessellate 失败或形状退化）")
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: PLC0415

    light = np.array([0.35, 0.30, 0.88])
    light = light / np.linalg.norm(light)
    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    try:
        ax = fig.add_subplot(111, projection="3d")
        all_pts: list = []
        for k, fm in enumerate(face_meshes):
            verts, facets = fm["verts"], fm["facets"]
            if not verts or not facets:
                continue
            all_pts.extend(verts)
            varr = np.asarray(verts, dtype=float)
            base = np.array(_PALETTE[k % len(_PALETTE)])
            tris, cols = [], []
            for f in facets:
                a, b, c = varr[f[0]], varr[f[1]], varr[f[2]]
                n = np.cross(b - a, c - a)
                ln = float(np.linalg.norm(n))
                shade = 0.45 + 0.55 * abs(float(n @ light) / ln) if ln > 1e-12 else 1.0
                tris.append([verts[i] for i in f])
                cols.append((*(base * shade), 1.0))
            ax.add_collection3d(Poly3DCollection(
                tris, facecolors=cols, edgecolor=(0, 0, 0, 0.12), linewidths=0.2))
        if not all_pts:
            raise ValueError("空网格：所有面 tessellation 均为空")
        pts = np.asarray(all_pts)
        mins, maxs = pts.min(0), pts.max(0)
        pad = _PAD_RATIO * float((maxs - mins).max() or 1.0)
        ax.set_xlim(mins[0] - pad, maxs[0] + pad)
        ax.set_ylim(mins[1] - pad, maxs[1] + pad)
        ax.set_zlim(mins[2] - pad, maxs[2] + pad)
        ax.set_box_aspect(tuple((maxs[i] - mins[i]) or 1 for i in range(3)))
        ax.view_init(*_VIEWS[view])
        ax.set_axis_off()
        for el in edge_labels:
            poly = el.get("polyline") or []
            if len(poly) >= 2:
                ax.plot(*zip(*poly, strict=False), color="#e07020", lw=2.0, zorder=50)
            ax.text(*el["pos"], el["label"], fontsize=9, color="#7a3500", fontweight="bold",
                    ha="center", va="center", zorder=99,
                    bbox=dict(boxstyle="round,pad=0.2", fc="#fff3e6", ec="#e07020", alpha=0.95))
        for fl in face_labels:
            if not fl.get("visible"):
                continue
            ax.text(*fl["pos"], fl["label"], fontsize=11, fontweight="bold",
                    ha="center", va="center", zorder=99,
                    bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#333", alpha=0.92))
        if dims:
            x0, y0, z0, x1, y1, z1 = dims["bbox"]
            m = max(x1 - x0, y1 - y0, z1 - z0) * 0.08
            for p0, p1, txt in (
                    ((x0, y0 - m, z0), (x1, y0 - m, z0), f"L={dims['L']:g}"),
                    ((x1 + m, y0, z0), (x1 + m, y1, z0), f"W={dims['W']:g}"),
                    ((x0 - m, y0 - m, z0), (x0 - m, y0 - m, z1), f"H={dims['H']:g}")):
                ax.plot(*zip(p0, p1, strict=False), color="#555", lw=1)
                mid = tuple((p0[i] + p1[i]) / 2 for i in range(3))
                ax.text(*mid, txt, fontsize=8, color="#333", zorder=99,
                        bbox=dict(boxstyle="round,pad=0.15", fc="#f5f5f5", ec="none", alpha=0.9))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
    finally:
        plt.close(fig)
    return buf.getvalue()


def render_annotated(shape: Any, *, mode: str = "faces", edges_of: int | None = None,
                     view: str = "iso") -> tuple[bytes, dict, dict, dict]:
    """FreeCAD Shape → (png, labels_table, faces_registry, edges_registry)。
    mode='faces'：全部面标注 + 尺寸线；mode='edges'：边标注（edges_of=面索引则只标该面的边）。"""
    from vibecad.engine import naming  # noqa: PLC0415
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    if mode not in ("faces", "edges"):
        raise ValueError(f"annotate 必须是 'faces' 或 'edges'（得到 {mode!r}）")
    cam = camera_direction(view)
    with silence_fd1():
        bb = shape.BoundBox
        bbox = (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)
        face_meshes, face_info = [], []
        for f in shape.Faces:
            verts, facets = f.tessellate(0.1)
            pts = [(p.x, p.y, p.z) for p in verts]
            face_meshes.append({"verts": pts, "facets": facets})
            face_info.append({"fp": naming.face_fingerprint(f),
                              "anchor": largest_triangle_centroid(pts, facets),
                              "normal": mesh_normal(pts, facets)})
        if not any(fm["verts"] for fm in face_meshes):
            raise RuntimeError("几何断言失败：形状无法镶嵌为网格（空 tessellation）")
        if mode == "edges" and edges_of is not None:
            edge_objs = list(shape.Faces[edges_of].Edges)
        elif mode == "edges":
            edge_objs = list(shape.Edges)
        else:
            edge_objs = []
        edge_info = []
        for e in edge_objs:
            mid = e.CenterOfMass
            edge_info.append({"fp": naming.edge_fingerprint(e),
                              "pos": (mid.x, mid.y, mid.z),
                              "polyline": [(p.x, p.y, p.z) for p in e.discretize(24)]})
    table: dict[str, str] = {}
    faces_reg: dict[str, dict] = {}
    face_labels_out = []
    if mode == "faces":
        names = naming.face_labels(len(face_info))
        for lab, info in zip(names, face_info, strict=True):
            visible = (sum(a * b for a, b in zip(info["normal"], cam, strict=True)) > _VIS_DOT)
            faces_reg[lab] = info["fp"]
            face_labels_out.append({"label": lab, "pos": info["anchor"], "visible": visible})
            note = "" if visible else "（当前视角不可见，换 top/front/right 试）"
            table[lab] = naming.face_summary(info["fp"], bbox) + note
    edges_reg: dict[str, dict] = {}
    edge_labels_out = []
    if mode == "edges":
        names = naming.edge_labels(len(edge_info))
        for lab, info in zip(names, edge_info, strict=True):
            edges_reg[lab] = info["fp"]
            edge_labels_out.append({"label": lab, "pos": info["pos"], "polyline": info["polyline"]})
            table[lab] = naming.edge_summary(info["fp"])
    dims = {"L": bbox[3] - bbox[0], "W": bbox[4] - bbox[1], "H": bbox[5] - bbox[2],
            "bbox": bbox} if mode == "faces" else None
    png = annotated_png(face_meshes=face_meshes, face_labels=face_labels_out,
                        edge_labels=edge_labels_out, view=view, dims=dims)
    return png, table, faces_reg, edges_reg
