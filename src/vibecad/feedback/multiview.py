# src/vibecad/feedback/multiview.py
"""三视图拼图（Round 6a，spike v5 工程图定稿）：2×2 = front/right/top 三格 HLR 工程图
线框（可见边实线、隐藏边虚线、圆心红点划十字、bbox 推导尺寸、⌀ 引线）+ 标注版 iso。
multiview_png / draw_engineering_view 纯 matplotlib；project_view / render_multiview
才碰 FreeCAD（TechDraw HLR），函数内 import。"""
from __future__ import annotations

from typing import Any

from vibecad.feedback.annotate import _draw_face_meshes, collect_annotation_data

_CJK_FONTS = ["Heiti TC", "PingFang SC", "Arial Unicode MS",
              "Noto Sans CJK SC", "WenQuanYi Zen Hei", "sans-serif"]

# per-view (投影方向, 2D 变换)——spike v3-v5 实测定稿：tf 把 projectEx 投影局部坐标
# 变换为直觉绘图坐标（横=宽、竖=高）。top 局部 x=X、y=Y，恒等即可；
# front(0,-1,0)/right(1,0,0) 局部坐标系旋了 90°，需 (x,y)→(y,-x) 旋正，
# 否则视图横竖颠倒。
_VIEW_TFS = {
    "top": ((0.0, 0.0, 1.0), lambda x, y: (x, y)),
    "front": ((0.0, -1.0, 0.0), lambda x, y: (y, -x)),
    "right": ((1.0, 0.0, 0.0), lambda x, y: (y, -x)),
}

# (subplot 序号, 视图名, 标题)——2×2 布局：front | right / top | iso
_ENG_GRID = [(1, "front", "front 正视"), (2, "right", "right 侧视"),
             (3, "top", "top 俯视")]


def project_view(shape: Any, direction: tuple[float, float, float], tf) -> dict:
    """HLR 投影单方向 → {"vis": 可见折线, "hid": 隐藏折线, "circles": [(cx,cy,r,visible)]}。
    projectEx 返回 10 组边：0-4 可见类（hard/smooth/sewn/outline/iso）、5-9 对应隐藏类。
    Line 只取两端点，曲线 discretize(48)。FreeCAD 路径——调用方保证 silence_fd1。"""
    import FreeCAD  # noqa: PLC0415
    import TechDraw  # noqa: PLC0415

    groups = TechDraw.projectEx(shape, FreeCAD.Vector(*direction))
    vis_polys: list = []
    hid_polys: list = []
    circles: list = []
    for gi in range(10):  # 0-4 可见类，5-9 隐藏类
        polys = vis_polys if gi < 5 else hid_polys
        for e in groups[gi].Edges:
            n_pts = 2 if type(e.Curve).__name__ == "Line" else 48
            pts = e.discretize(n_pts)
            polys.append([tf(p.x, p.y) for p in pts])
            if type(e.Curve).__name__ == "Circle":
                c = e.Curve.Center
                circles.append((*tf(c.x, c.y), e.Curve.Radius, gi < 5))
    return {"vis": vis_polys, "hid": hid_polys, "circles": circles}


def draw_engineering_view(ax, view_data: dict, title: str) -> tuple | None:
    """单格 2D 工程图：可见边实线 #222、隐藏边虚线 #888、圆心红点划中心线十字。
    返回视图 2D 包围盒 (x0, y0, x1, y1)；无任何折线返回 None（调用方决定如何处理）。"""
    vis, hid, circles = view_data["vis"], view_data["hid"], view_data["circles"]
    for poly in hid:
        xs, ys = zip(*poly, strict=False)
        ax.plot(xs, ys, color="#888", lw=0.9, linestyle=(0, (5, 3)))
    for poly in vis:
        xs, ys = zip(*poly, strict=False)
        ax.plot(xs, ys, color="#222", lw=1.4)
    for cx, cy, r, _v in circles:
        m = r * 1.4  # 中心线十字
        ax.plot([cx - m, cx + m], [cy, cy], color="#b33", lw=0.6, linestyle=(0, (8, 3, 2, 3)))
        ax.plot([cx, cx], [cy - m, cy + m], color="#b33", lw=0.6, linestyle=(0, (8, 3, 2, 3)))
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(title, fontsize=11, fontfamily=_CJK_FONTS)
    pts = [p for poly in vis + hid for p in poly]
    if not pts:
        return None
    xs2 = [p[0] for p in pts]
    ys2 = [p[1] for p in pts]
    return (min(xs2), min(ys2), max(xs2), max(ys2))


def _dim_h(ax, x0: float, x1: float, y: float, text: str, off: float) -> None:
    """水平尺寸：尺寸界线 + 双箭头 + 数字（off 正负决定标注在上/下方）。"""
    for x in (x0, x1):
        ax.plot([x, x], [y, y + off], color="#555", lw=0.6)
    ax.annotate("", (x0, y + off), (x1, y + off),
                arrowprops=dict(arrowstyle="<->", lw=0.8, color="#333"))
    ax.text((x0 + x1) / 2, y + off + (1.2 if off > 0 else -1.2), text,
            ha="center", va="bottom" if off > 0 else "top", fontsize=9, color="#111")


def _dim_v(ax, y0: float, y1: float, x: float, text: str, off: float) -> None:
    """竖直尺寸：尺寸界线 + 双箭头 + 数字（off 正负决定标注在右/左侧）。"""
    for y in (y0, y1):
        ax.plot([x, x + off], [y, y], color="#555", lw=0.6)
    ax.annotate("", (x + off, y0), (x + off, y1),
                arrowprops=dict(arrowstyle="<->", lw=0.8, color="#333"))
    ax.text(x + off + (1.2 if off > 0 else -1.2), (y0 + y1) / 2, text,
            ha="left" if off > 0 else "right", va="center", fontsize=9, color="#111")


def multiview_png(*, eng_views: dict, face_meshes: list[dict], face_labels: list[dict],
                  dims: dict | None, size: tuple[int, int] = (920, 760)) -> bytes:
    """2×2 拼图 → PNG bytes。front/right/top 三格工程图线框 + 尺寸标注，
    iso 格面片渲染 + 面标签 + 包围盒尺寸线。纯 matplotlib，不碰 FreeCAD。"""
    if not eng_views and not face_meshes:
        raise ValueError("空视图：无任何投影或网格可渲染（可能是 HLR/tessellate 失败或形状退化）")
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    try:
        drew_any = False
        bbs: dict[str, tuple] = {}
        axes2d: dict[str, Any] = {}
        for idx, key, title in _ENG_GRID:
            ax = fig.add_subplot(2, 2, idx)
            vd = eng_views.get(key)
            bb = draw_engineering_view(ax, vd, title) if vd else None
            if bb is not None:
                bbs[key] = bb
                axes2d[key] = ax
                drew_any = True
        # 尺寸线全部从各视图投影 2D 包围盒推导（与模型坐标解耦，任意形状通用）
        for key, (x0, y0, x1, y1) in bbs.items():
            ax = axes2d[key]
            _dim_h(ax, x0, x1, y0, f"{x1 - x0:g}", -6)
            _dim_v(ax, y0, y1, x0, f"{y1 - y0:g}", -6)
            circles = eng_views[key]["circles"]
            seen_radii: set[float] = set()  # ⌀ 按半径去重（同径只标一次，防多孔拥挤）
            for cx, cy, r, _vis in circles:
                rk = round(r, 6)
                if rk in seen_radii:
                    continue
                seen_radii.add(rk)
                ax.annotate(f"⌀{2 * r:g}", (cx + r * 0.707, cy + r * 0.707),
                            xytext=(cx + r + 9, cy + r + 7), fontsize=9, color="#111",
                            arrowprops=dict(arrowstyle="-", lw=0.7, color="#333"))
            # 定位尺寸只标首个可见圆（圆心到包围盒两方向）
            first = next(((cx, cy) for cx, cy, _r, v in circles if v), None)
            if first is not None:
                fx, fy = first
                _dim_h(ax, x0, fx, y1, f"{fx - x0:g}", 6)
                _dim_v(ax, y0, fy, x1, f"{fy - y0:g}", 6)
        # 三格统一比例（长对正/高平齐的简化版）：共用最大跨度
        if bbs:
            span = max(max(b[2] - b[0], b[3] - b[1]) for b in bbs.values()) * 1.45
            for key, (x0, y0, x1, y1) in bbs.items():
                cx2, cy2 = (x0 + x1) / 2, (y0 + y1) / 2
                axes2d[key].set_xlim(cx2 - span / 2, cx2 + span / 2)
                axes2d[key].set_ylim(cy2 - span / 2, cy2 + span / 2)
        # iso 格：面片渲染 + 面标签（仅可见面）+ 包围盒尺寸线
        ax = fig.add_subplot(2, 2, 4, projection="3d")
        drew_iso = _draw_face_meshes(ax, face_meshes, view="iso")
        drew_any = drew_any or drew_iso
        ax.set_title("iso 立体", fontsize=10, pad=2, fontfamily=_CJK_FONTS)
        if drew_iso:
            for fl in face_labels:
                if not fl.get("visible"):
                    continue
                ax.text(*fl["pos"], fl["label"], fontsize=10, fontweight="bold",
                        ha="center", va="center", zorder=99,
                        bbox=dict(boxstyle="round,pad=0.22", fc="white", ec="#333", alpha=0.92))
            if dims:  # iso 格：包围盒尺寸线
                x0, y0, z0, x1, y1, z1 = dims["bbox"]
                m = max(x1 - x0, y1 - y0, z1 - z0) * 0.08
                for p0, p1, txt in (
                        ((x0, y0 - m, z0), (x1, y0 - m, z0), f"L={dims['L']:g}"),
                        ((x1 + m, y0, z0), (x1 + m, y1, z0), f"W={dims['W']:g}"),
                        ((x0 - m, y0 - m, z0), (x0 - m, y0 - m, z1), f"H={dims['H']:g}")):
                    ax.plot(*zip(p0, p1, strict=False), color="#555", lw=1)
                    mid = tuple((p0[i] + p1[i]) / 2 for i in range(3))
                    ax.text(*mid, txt, fontsize=8, color="#333", zorder=99,
                            bbox=dict(boxstyle="round,pad=0.15", fc="#f5f5f5",
                                      ec="none", alpha=0.9))
        if not drew_any:
            raise ValueError("空视图：所有视图均为空（投影与 tessellation 均无几何）")
        fig.subplots_adjust(left=0.04, right=0.97, top=0.95, bottom=0.04,
                            wspace=0.10, hspace=0.14)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
    finally:
        plt.close(fig)
    return buf.getvalue()


def render_multiview(shape: Any) -> tuple[bytes, dict, dict, dict]:
    """FreeCAD Shape → (png, labels_table, faces_reg, edges_reg)。
    三正交格 HLR 投影 + iso 格标注数据；标签语义与 render_annotated(mode='faces',
    view='iso') 完全一致（注册表全量）。"""
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    with silence_fd1():
        eng_views = {key: project_view(shape, direction, tf)
                     for key, (direction, tf) in _VIEW_TFS.items()}
    data = collect_annotation_data(shape, view="iso")
    png = multiview_png(eng_views=eng_views, face_meshes=data["face_meshes"],
                        face_labels=data["face_labels"], dims=data["dims"])
    return png, data["table"], data["faces_reg"], data["edges_reg"]
