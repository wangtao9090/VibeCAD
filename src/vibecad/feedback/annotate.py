# src/vibecad/feedback/annotate.py
"""标注渲染：逐面网格 + 面/边标签 + 包围盒尺寸线（matplotlib Agg，函数内 import）。
annotated_png 为纯函数（吃普通 list 数据）；render_annotated 才碰 FreeCAD Shape。
已知限界：标签锚点只做朝相机三角形过滤，不做完整遮挡检测——凹腔/被其他几何
挡住的面，其标签仍可能穿透叠画在前景上（可见性表注是权威，图为辅助）。"""
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


def largest_triangle_centroid(verts: list, facets: list,
                              cam: tuple | None = None) -> tuple[float, float, float]:
    """面最大三角形的质心——面标签锚点（spike 定稿：CenterOfMass 在带孔面会落孔上）。
    cam 给定时优先取"朝相机"（三角形法向·cam > 0）的最大三角形质心——曲面（如圆柱面）
    的全局最大三角形可能在背面，锚点落背面会被前景遮挡；无朝相机三角形退回全局最大。
    退化/空网格返回首顶点或 (0,0,0)。"""
    best = None
    best_area = -1.0
    best_facing = None
    best_facing_area = -1.0
    for f in facets:
        a, b, c = verts[f[0]], verts[f[1]], verts[f[2]]
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        area = math.sqrt(nx * nx + ny * ny + nz * nz)
        if area > best_area:
            best_area = area
            best = tuple((a[i] + b[i] + c[i]) / 3 for i in range(3))
        if (cam is not None and area > best_facing_area
                and nx * cam[0] + ny * cam[1] + nz * cam[2] > 0):
            best_facing_area = area
            best_facing = tuple((a[i] + b[i] + c[i]) / 3 for i in range(3))
    if best_facing is not None:
        return best_facing
    if best is None:
        return tuple(verts[0]) if verts else (0.0, 0.0, 0.0)
    return best


def visibility_note(normal: tuple, current_view: str) -> str:
    """面法向 → 表注文本。当前视角可见返回 ""；不可见则计算哪些预设视角可见
    （而非静态死路文案——底面/孔壁在全部预设视角都不可见时直说）。"""
    def _dot(view: str) -> float:
        return sum(a * b for a, b in zip(normal, camera_direction(view), strict=True))

    if _dot(current_view) > _VIS_DOT:
        return ""
    seen = [v for v in sorted(_VIEWS) if _dot(v) > _VIS_DOT]
    if seen:
        return f"（当前视角不可见，在 {'/'.join(seen)} 视角可见）"
    return "（预设视角均不可见，请直接用本描述指代）"


def _draw_face_meshes(ax, face_meshes: list[dict], *, view: str, alpha: float = 1.0) -> bool:
    """在单个 3D axes 上画逐面网格（palette 着色+朗伯明暗）并设轴范围/视角。
    alpha<1 为半透明（X 光正交视图用，边线同步弱化）。
    返回是否画出了任何几何（False=全部空网格，由调用方决定如何报错）。"""
    import numpy as np  # noqa: PLC0415
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: PLC0415

    light = np.array([0.35, 0.30, 0.88])
    light = light / np.linalg.norm(light)
    edge_rgba = (0, 0, 0, 0.05 if alpha < 1.0 else 0.12)
    all_pts: list = []
    for k, fm in enumerate(face_meshes):
        verts, facets = fm["verts"], fm["facets"]
        if not verts or not facets:
            continue
        all_pts.extend(verts)
        varr = np.asarray(verts, dtype=float)
        # 优先使用 collect_annotation_data 预分配的颜色（装配模式按零件轮换）；
        # 否则退回全局 palette（兼容外部直接构造 face_meshes 的调用方）
        base = np.array(fm.get("_color", _PALETTE[k % len(_PALETTE)]))
        tris, cols = [], []
        for f in facets:
            a, b, c = varr[f[0]], varr[f[1]], varr[f[2]]
            n = np.cross(b - a, c - a)
            ln = float(np.linalg.norm(n))
            shade = 0.45 + 0.55 * abs(float(n @ light) / ln) if ln > 1e-12 else 1.0
            tris.append([verts[i] for i in f])
            cols.append((*(base * shade), alpha))
        ax.add_collection3d(Poly3DCollection(
            tris, facecolors=cols, edgecolor=edge_rgba, linewidths=0.2))
    if not all_pts:
        return False
    pts = np.asarray(all_pts)
    mins, maxs = pts.min(0), pts.max(0)
    pad = _PAD_RATIO * float((maxs - mins).max() or 1.0)
    ax.set_xlim(mins[0] - pad, maxs[0] + pad)
    ax.set_ylim(mins[1] - pad, maxs[1] + pad)
    ax.set_zlim(mins[2] - pad, maxs[2] + pad)
    # aspect 与 limits 同口径（都含 pad），否则非立方体各向异性拉伸（孔变椭圆）
    ax.set_box_aspect(tuple(((maxs[i] - mins[i]) + 2 * pad) or 1 for i in range(3)))
    ax.view_init(*_VIEWS[view])
    ax.set_axis_off()
    return True


def annotated_png(*, face_meshes: list[dict], face_labels: list[dict],
                  edge_labels: list[dict], view: str = "iso",
                  size: tuple[int, int] = (560, 560), dims: dict | None = None) -> bytes:
    """逐面网格 + 标签 → PNG bytes。纯 matplotlib，不碰 FreeCAD。"""
    camera_direction(view)  # 校验 view
    if not face_meshes:
        raise ValueError("空网格：无任何面可渲染（可能是 tessellate 失败或形状退化）")
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    try:
        ax = fig.add_subplot(111, projection="3d")
        if not _draw_face_meshes(ax, face_meshes, view=view):
            raise ValueError("空网格：所有面 tessellation 均为空")
        for el in edge_labels:
            poly = el.get("polyline") or []
            e_vis = el.get("visible", True)  # 缺省可见（向后兼容）
            if len(poly) >= 2:
                style = {} if e_vis else {"linestyle": ":", "alpha": 0.35}  # 背面边虚线弱化
                ax.plot(*zip(*poly, strict=False), color="#e07020", lw=2.0, zorder=50, **style)
            ax.text(*el["pos"], el["label"], fontsize=9, color="#7a3500", fontweight="bold",
                    ha="center", va="center", zorder=99,
                    bbox=dict(boxstyle="round,pad=0.2", fc="#fff3e6", ec="#e07020",
                              alpha=0.95 if e_vis else 0.45))
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


def collect_annotation_data(shape: Any, *, view: str = "iso",
                            part_map: dict[str, Any] | None = None) -> dict:
    """逐面 tessellate + 指纹/锚点/可见性 + 全量注册表 + faces 标签表 + 尺寸。
    render_annotated(mode='faces') 与 multiview.render_multiview 的共享数据源。
    返回 {face_meshes, face_labels, table, faces_reg, edges_reg, dims}。

    part_map={零件名: 全局 shape}（可选）：给定时对 compound shape 的每个面做零件归属
    判定——compound 面序 = 各零件 shape.Faces 按 part_map 迭代顺序拼接，palette 按零件
    基色轮换（零件内仍循环色差），标签表条目加"（零件：X）"后缀。
    未给定（None）：单零件模式，行为与旧版完全一致——面色取全局 palette，无零件后缀。
    """
    from vibecad.engine import naming  # noqa: PLC0415
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    # 零件调色板：每个零件用一个不同的基色组，零件内色差沿 _PALETTE 循环
    # 基色组：hue 按零件数均匀分布（3 个暖→冷色调），零件内循环 _PALETTE 做亮度差
    _PART_BASE_COLORS = [
        # 蓝调（默认单零件），红调，绿调，紫调，橙调
        [(0.32, 0.55, 0.90), (0.36, 0.62, 0.83), (0.30, 0.50, 0.95),
         (0.40, 0.58, 0.86), (0.34, 0.52, 0.92), (0.38, 0.60, 0.88)],
        [(0.90, 0.40, 0.38), (0.85, 0.45, 0.42), (0.88, 0.35, 0.35),
         (0.82, 0.50, 0.45), (0.86, 0.38, 0.40), (0.84, 0.43, 0.37)],
        [(0.35, 0.80, 0.45), (0.40, 0.75, 0.50), (0.32, 0.78, 0.42),
         (0.38, 0.82, 0.48), (0.36, 0.77, 0.44), (0.42, 0.79, 0.46)],
        [(0.70, 0.45, 0.88), (0.65, 0.50, 0.85), (0.72, 0.42, 0.90),
         (0.68, 0.48, 0.83), (0.66, 0.46, 0.87), (0.74, 0.44, 0.86)],
        [(0.90, 0.65, 0.25), (0.85, 0.70, 0.30), (0.88, 0.60, 0.22),
         (0.82, 0.68, 0.28), (0.86, 0.63, 0.24), (0.84, 0.67, 0.26)],
    ]

    cam = camera_direction(view)
    with silence_fd1():
        bb = shape.BoundBox
        bbox = (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)

        # 构建面的零件归属：face_idx → (零件名, 零件序号) 或 None（单零件模式）
        face_part_assignment: list[tuple[str, int] | None] = []
        if part_map is not None:
            part_names = list(part_map)
            for pi, pname in enumerate(part_names):
                p_shape = part_map[pname]
                for _ in p_shape.Faces:
                    face_part_assignment.append((pname, pi))
        else:
            # 单零件模式：face_part_assignment 保持空，后续按 None 处理
            face_part_assignment = []

        face_meshes, face_info = [], []
        for i, f in enumerate(shape.Faces):
            verts, facets = f.tessellate(0.1)
            pts = [(p.x, p.y, p.z) for p in verts]
            assignment = face_part_assignment[i] if i < len(face_part_assignment) else None
            face_meshes.append({"verts": pts, "facets": facets,
                                "_part_assignment": assignment})
            face_info.append({"fp": naming.face_fingerprint(f),
                              "anchor": largest_triangle_centroid(pts, facets, cam),
                              "normal": mesh_normal(pts, facets),
                              "_part_assignment": assignment})
        if not any(fm["verts"] for fm in face_meshes):
            raise RuntimeError("几何断言失败：形状无法镶嵌为网格（空 tessellation）")
        edges_reg_list = [naming.edge_fingerprint(e) for e in shape.Edges]

    # 为各面分配颜色（装配模式按零件轮换基色组，单零件模式用全局 _PALETTE）
    if part_map is not None:
        # 每零件维护自己的 palette 内计数器
        part_local_counter: dict[str, int] = {}
        for fm, _info in zip(face_meshes, face_info, strict=True):
            assignment = fm["_part_assignment"]
            if assignment is not None:
                pname, pi = assignment
                local_idx = part_local_counter.get(pname, 0)
                part_local_counter[pname] = local_idx + 1
                palette = _PART_BASE_COLORS[pi % len(_PART_BASE_COLORS)]
                fm["_color"] = palette[local_idx % len(palette)]
            else:
                fm["_color"] = _PALETTE[0]
    else:
        for k, fm in enumerate(face_meshes):
            fm["_color"] = _PALETTE[k % len(_PALETTE)]

    face_names = naming.face_labels(len(face_info))
    table: dict[str, str] = {}
    faces_reg: dict[str, dict] = {}
    face_labels = []
    for lab, info in zip(face_names, face_info, strict=True):
        visible = (sum(a * b for a, b in zip(info["normal"], cam, strict=True)) > _VIS_DOT)
        faces_reg[lab] = info["fp"]
        face_labels.append({"label": lab, "pos": info["anchor"], "visible": visible})
        summary = naming.face_summary(info["fp"], bbox) + visibility_note(info["normal"], view)
        # 装配模式：标签表条目加零件归属后缀
        if info["_part_assignment"] is not None:
            pname, _pi = info["_part_assignment"]
            summary += f"（零件：{pname}）"
        table[lab] = summary
    edges_reg = dict(zip(naming.edge_labels(len(edges_reg_list)), edges_reg_list, strict=True))
    dims = {"L": bbox[3] - bbox[0], "W": bbox[4] - bbox[1], "H": bbox[5] - bbox[2],
            "bbox": bbox}
    return {"face_meshes": face_meshes, "face_labels": face_labels, "table": table,
            "faces_reg": faces_reg, "edges_reg": edges_reg, "dims": dims}


def render_annotated(shape: Any, *, mode: str = "faces", edges_of: int | None = None,
                     view: str = "iso") -> tuple[bytes, dict, dict, dict]:
    """FreeCAD Shape → (png, labels_table, faces_registry, edges_registry)。

    注册表（faces_reg/edges_reg）无论 mode 都全量注册（面+边指纹），保证
    "看面→看边→打孔"工作流中 Session.set_labels 整体覆盖不丢另一类标签，
    且标签序号不随 mode/edges_of 漂移。mode 与 edges_of 只决定图上画什么：
    faces 模式画面标签+尺寸线；edges 模式画边标签（edges_of 给定时只画该面
    的边，但标签号仍是全局边序号）。labels_table 只含当前画出来的标签项。"""
    from vibecad.engine import naming  # noqa: PLC0415
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    if mode not in ("faces", "edges"):
        raise ValueError(f"annotate 必须是 'faces' 或 'edges'（得到 {mode!r}）")
    cam = camera_direction(view)
    if mode == "faces":
        if edges_of is not None:
            # 静默忽略会让调用方误以为过滤生效，违反"绝不静默"纪律
            raise ValueError("edges_of 仅在 mode='edges' 时有效")
        # faces 模式：委托给共享数据源
        data = collect_annotation_data(shape, view=view)
        dims = data["dims"]
        png = annotated_png(face_meshes=data["face_meshes"],
                            face_labels=data["face_labels"],
                            edge_labels=[], view=view, dims=dims)
        return png, data["table"], data["faces_reg"], data["edges_reg"]
    # edges 模式：需要 polyline/edge_adj/draw_set，继续完整采集
    with silence_fd1():
        n_faces = len(shape.Faces)
        if edges_of is not None and not 0 <= edges_of < n_faces:
            # Python 负索引会静默取最后一个面，违反"绝不静默"纪律
            raise ValueError(f"edges_of 面索引越界（0..{n_faces - 1}，得到 {edges_of}）")
        face_meshes, face_info = [], []
        for f in shape.Faces:
            verts, facets = f.tessellate(0.1)
            pts = [(p.x, p.y, p.z) for p in verts]
            face_meshes.append({"verts": pts, "facets": facets})
            face_info.append({"fp": naming.face_fingerprint(f),
                              "anchor": largest_triangle_centroid(pts, facets, cam),
                              "normal": mesh_normal(pts, facets)})
        if not any(fm["verts"] for fm in face_meshes):
            raise RuntimeError("几何断言失败：形状无法镶嵌为网格（空 tessellation）")
        # 边指纹+中点无条件全量（全局序）——注册表全量 + 标签号稳定的前提（T3 契约）
        all_edges = list(shape.Edges)
        edge_info = []
        for e in all_edges:
            mid = e.CenterOfMass
            edge_info.append({"fp": naming.edge_fingerprint(e),
                              "pos": (mid.x, mid.y, mid.z)})
        # 以下重活只有 edges 模式的绘制会消费：polyline 离散 + O(E²) isSame 邻接
        edge_adj: list[list[int]] = [[] for _ in all_edges]
        draw_set: set[int] = set()
        for info, e in zip(edge_info, all_edges, strict=True):
            info["polyline"] = [(p.x, p.y, p.z) for p in e.discretize(24)]
        # edge → 相邻面索引（isSame 反向匹配）：边可见 = 任一相邻面可见
        for fi, f in enumerate(shape.Faces):
            for fe in f.Edges:
                for ei, e in enumerate(all_edges):
                    if fe.isSame(e):
                        edge_adj[ei].append(fi)
        for ei, adj in enumerate(edge_adj):
            if not adj:  # 流形 solid 不可能；静默会把该边误标"背面边"
                raise RuntimeError(
                    f"几何断言失败：边 {ei} 不属于任何面（非流形几何？）")
        # edges_of 过滤只影响绘制集合，标签号保持全局边序
        if edges_of is not None:
            target_edges = shape.Faces[edges_of].Edges
            draw_set = {ei for fe in target_edges
                        for ei, e in enumerate(all_edges) if fe.isSame(e)}
            if not draw_set and target_edges:
                raise RuntimeError(
                    f"几何断言失败：面 {edges_of} 的 {len(target_edges)} 条边"
                    "在全局边集中无一匹配（isSame 失效？）")
        else:
            draw_set = set(range(len(all_edges)))
    face_visible = [
        sum(a * b for a, b in zip(info["normal"], cam, strict=True)) > _VIS_DOT
        for info in face_info]
    # 注册表始终全量（与 mode 无关）
    face_names = naming.face_labels(len(face_info))
    faces_reg = {lab: info["fp"]
                 for lab, info in zip(face_names, face_info, strict=True)}
    edge_names = naming.edge_labels(len(edge_info))
    edges_reg = {lab: info["fp"]
                 for lab, info in zip(edge_names, edge_info, strict=True)}
    table: dict[str, str] = {}
    edge_labels_out = []
    for ei in sorted(draw_set):
        lab, info = edge_names[ei], edge_info[ei]
        e_vis = any(face_visible[fi] for fi in edge_adj[ei])
        edge_labels_out.append({"label": lab, "pos": info["pos"],
                                "polyline": info["polyline"], "visible": e_vis})
        note = "" if e_vis else "（背面边，谨慎指认）"
        table[lab] = naming.edge_summary(info["fp"]) + note
    png = annotated_png(face_meshes=face_meshes, face_labels=[],
                        edge_labels=edge_labels_out, view=view, dims=None)
    return png, table, faces_reg, edges_reg
