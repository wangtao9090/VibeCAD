# src/vibecad/feedback/multiview.py
"""三视图拼图（Round 6a）：2×2 = 半透明 front/right（X 光透出内部孔）+ top + 标注版 iso。
multiview_png 纯函数；render_multiview 才碰 FreeCAD Shape。"""
from __future__ import annotations

from typing import Any

from vibecad.feedback.annotate import _draw_face_meshes, collect_annotation_data

# (标题, 视角名, alpha)——spike 定稿：正交格 0.35 半透明，top/iso 不透明
_GRID = [("front 正视", "front", 0.35), ("top 俯视", "top", 1.0),
         ("right 侧视", "right", 0.35), ("iso 立体", "iso", 1.0)]


def multiview_png(*, face_meshes: list[dict], face_labels: list[dict],
                  dims: dict | None, size: tuple[int, int] = (880, 880)) -> bytes:
    """2×2 拼图 → PNG bytes。iso 格画标签+尺寸线，其余格纯几何。纯 matplotlib。"""
    if not face_meshes:
        raise ValueError("空网格：无任何面可渲染（可能是 tessellate 失败或形状退化）")
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    try:
        drew_any = False
        for k, (title, view, alpha) in enumerate(_GRID):
            ax = fig.add_subplot(2, 2, k + 1, projection="3d")
            drew = _draw_face_meshes(ax, face_meshes, view=view, alpha=alpha)
            drew_any = drew_any or drew
            ax.set_title(title, fontsize=10, pad=2)
            if view != "iso" or not drew:
                continue
            for fl in face_labels:  # iso 格：标签（仅可见面）
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
            raise ValueError("空网格：所有面 tessellation 均为空")
        fig.subplots_adjust(left=0.01, right=0.99, top=0.95, bottom=0.01,
                            wspace=0.02, hspace=0.08)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
    finally:
        plt.close(fig)
    return buf.getvalue()


def render_multiview(shape: Any) -> tuple[bytes, dict, dict, dict]:
    """FreeCAD Shape → (png, labels_table, faces_reg, edges_reg)。
    标签语义与 render_annotated(mode='faces', view='iso') 完全一致（注册表全量）。"""
    data = collect_annotation_data(shape, view="iso")
    png = multiview_png(face_meshes=data["face_meshes"], face_labels=data["face_labels"],
                        dims=data["dims"])
    return png, data["table"], data["faces_reg"], data["edges_reg"]
