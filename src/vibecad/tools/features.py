"""面/边级特征工具（Round 5）：消费标签注册表的指代（"A 面打孔"、"E3 倒角"）。
纪律：参数校验 → 事务（内含标签指纹解析，过期即 LabelExpiredError；recompute → 几何断言）
→ 结构化 dict。校验必须先于一切 session 访问；解析在事务内，失败随事务一并回滚。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session


def _inplane_axes(n) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """法向 n → 面内正交单位基 (e1, e2)：取与 n 最不平行的全局轴投影（offset 方向直观）。"""
    nx, ny, nz = float(n[0]), float(n[1]), float(n[2])
    axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    g = min(axes, key=lambda a: abs(a[0] * nx + a[1] * ny + a[2] * nz))
    d = g[0] * nx + g[1] * ny + g[2] * nz
    e1 = (g[0] - d * nx, g[1] - d * ny, g[2] - d * nz)
    ln = math.sqrt(sum(c * c for c in e1))
    e1 = (e1[0] / ln, e1[1] / ln, e1[2] / ln)
    e2 = (ny * e1[2] - nz * e1[1], nz * e1[0] - nx * e1[2], nx * e1[1] - ny * e1[0])
    return e1, e2


def _outward_normal(shape: Any, face: Any):
    """面的单位外法向（normalAt 不保证定向——用实体内点探针校正）。返回 FreeCAD.Vector。
    探针锚点取面三角剖分的最大三角形质心（必落材料上）：CenterOfMass/参数中点在带孔面
    （如打孔后的环形面）会落在孔开口上，探针两侧都是空气、isInside 恒 False、校正失效
    ——与 annotate.largest_triangle_centroid 标签锚点同方案。法向同在锚点处取，
    保证探针沿的正是锚点处的法向（曲面上两点法向不同）。"""
    import FreeCAD  # noqa: PLC0415

    from vibecad.feedback.annotate import largest_triangle_centroid  # noqa: PLC0415
    verts, facets = face.tessellate(0.1)
    if facets:
        anchor = FreeCAD.Vector(*largest_triangle_centroid(verts, facets))
    else:  # 退化面剖分为空：退回 CenterOfMass（无孔可落，旧行为即正确）
        anchor = face.CenterOfMass
    u, v = face.Surface.parameter(anchor)
    n = face.normalAt(u, v)
    n.normalize()
    solid = shape.Solids[0] if getattr(shape, "Solids", None) else shape
    probe = anchor + n * 0.01
    if solid.isInside(probe, 1e-6, False):
        n = -n
    return n


def _count_full_cylinder_faces(shape: Any, radius: float) -> int:
    """数半径匹配（1e-6）且 u 参数跨满 2π（容差 1e-3）的圆柱面——完整圆孔的成形判据。
    增量判据（cut 前后各数一次，after >= before+1）：存在性判据会被同径旧孔放行
    新孔的越界缺口（安装孔阵列是常见操作，终审 CRITICAL-3）。"""
    n = 0
    for f in shape.Faces:
        s = f.Surface
        if type(s).__name__ != "Cylinder" or abs(s.Radius - radius) > 1e-6:
            continue
        u0, u1 = f.ParameterRange[0], f.ParameterRange[1]
        if abs((u1 - u0) - 2 * math.pi) < 1e-3:
            n += 1
    return n


def add_hole(session: Session, face: str, diameter: float,
             depth: float | None = None, offset=(0.0, 0.0)) -> dict[str, Any]:
    """在指定面（标签）打圆孔：depth=None 通孔；offset 为面内毫米坐标（原点=面心）。"""
    if not face or not isinstance(face, str):
        raise ValueError("face 必须是非空字符串（面标签，如 'A'）")
    if not math.isfinite(diameter) or diameter <= 0:  # NaN 与 <=0 比较恒 False，须显式拒绝
        raise ValueError(f"diameter 必须是 > 0 的有限数字（得到 {diameter}）")
    if depth is not None and (not math.isfinite(depth) or depth <= 0):
        raise ValueError(f"depth 必须是 > 0 的有限数字或省略表示通孔（得到 {depth}）")
    if (not isinstance(offset, (list, tuple)) or len(offset) != 2
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                       and math.isfinite(c) for c in offset)):
        raise ValueError(f"offset 必须是 2 个有限数字 (u, v)（得到 {offset!r}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("add_hole"):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            idx = session.resolve_face(face)
            base_obj = session.get_result_object()
            shape = base_obj.Shape
            face_obj = shape.Faces[idx]
            surface = type(face_obj.Surface).__name__
            if surface != "Plane":
                raise ValueError(f"标签 {face} 是 {surface}，只能在平面上打孔")
            n = _outward_normal(shape, face_obj)
            e1, e2 = _inplane_axes((n.x, n.y, n.z))
            c = face_obj.CenterOfMass
            lift = 0.5  # 从面外 0.5mm 起钻，避免共面布尔
            bx = c.x + e1[0] * offset[0] + e2[0] * offset[1] + n.x * lift
            by = c.y + e1[1] * offset[0] + e2[1] * offset[1] + n.y * lift
            bz = c.z + e1[2] * offset[0] + e2[2] * offset[1] + n.z * lift
            length = (depth + lift) if depth is not None \
                else shape.BoundBox.DiagonalLength + 2 * lift
            base_vol = shape.Volume
            cyl_before = _count_full_cylinder_faces(shape, diameter / 2.0)
            cyl = session.doc.addObject("Part::Cylinder", "HoleTool")
            cyl.Radius, cyl.Height = diameter / 2.0, length
            cyl.Placement = FreeCAD.Placement(
                FreeCAD.Vector(bx, by, bz),
                FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), FreeCAD.Vector(-n.x, -n.y, -n.z)))
            cut = session.doc.addObject("Part::Cut", "Hole")
            cut.Base, cut.Tool = base_obj, cyl
            session.doc.recompute()
            session.assert_valid_solid(cut.Shape)
            if cut.Shape.Volume >= base_vol - 1e-6:
                raise RuntimeError(
                    f"几何断言失败：打孔未移除任何材料（base={base_vol:.3f}, "
                    f"cut={cut.Shape.Volume:.3f}）——offset/depth 可能让孔落在零件之外")
            if len(cut.Shape.Solids) != 1:
                raise RuntimeError(
                    f"几何断言失败：打孔把零件切成 {len(cut.Shape.Solids)} 块"
                    "——offset 可能越过零件边缘")
            if _count_full_cylinder_faces(cut.Shape, diameter / 2.0) < cyl_before + 1:
                raise RuntimeError(
                    "几何断言失败：未形成完整圆孔（孔可能与零件边缘相交成开口缺口，"
                    "或与已有孔/特征重叠）"
                    "——请调整 offset")
            if depth is not None:
                # 盲孔体积核算：超深打穿/侧向越界少切都会让移除量低于名义圆柱体积
                removed = base_vol - cut.Shape.Volume
                expected = math.pi * (diameter / 2.0) ** 2 * depth
                if removed < expected * 0.99 - 1e-6:
                    raise RuntimeError(
                        f"几何断言失败：盲孔实际移除体积 {removed:.3f} < 期望 {expected:.3f}"
                        "——depth 可能超出材料厚度（已打穿）、孔越界，"
                        "或与已有孔/特征重叠，请减小 depth 或检查 offset")
            result = {"ok": True, "name": cut.Name, "volume": cut.Shape.Volume,
                      "hole": {"face": face, "diameter": diameter,
                               "depth": depth if depth is not None else "through",
                               "offset": list(offset)},
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
    return result


def _edge_feature(session: Session, edges, value: float, *, kind: str,
                  type_name: str, obj_label: str, value_field: str) -> dict[str, Any]:
    if not isinstance(edges, (list, tuple)) or not edges \
            or not all(isinstance(e, str) and e for e in edges):
        raise ValueError(f"edges 必须是非空字符串列表（边标签，如 ['E1','E2']）（得到 {edges!r}）")
    if not math.isfinite(value) or value <= 0:  # NaN 与 <=0 比较恒 False，须显式拒绝
        raise ValueError(f"{value_field} 必须是 > 0 的有限数字（得到 {value}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction(kind):
        with silence_fd1():
            # 去重：OCCT 对重复边静默去重，面数断言须按唯一边数计
            idxs = list(dict.fromkeys(session.resolve_edge(e) for e in edges))
            base_obj = session.get_result_object()
            faces_before = len(base_obj.Shape.Faces)
            vol_before = base_obj.Shape.Volume
            feat = session.doc.addObject(type_name, obj_label)
            feat.Base = base_obj
            feat.Edges = [(i + 1, value, value) for i in idxs]  # 1-based (idx, r1, r2)
            session.doc.recompute()
            session.assert_valid_solid(feat.Shape)
            # 每条唯一边应恰好产生 1 个新面（实测 4 边 fillet 恰 +4）
            if len(feat.Shape.Faces) < faces_before + len(idxs):
                raise RuntimeError(
                    f"几何断言失败：{kind} 新面数不足（期望 ≥ {faces_before + len(idxs)}，"
                    f"实际 {len(feat.Shape.Faces)}）——OCCT 可能对部分所选边失败：{edges}")
            if abs(feat.Shape.Volume - vol_before) < 1e-9:
                raise RuntimeError(f"几何断言失败：{kind} 后体积无变化——所选边可能无效：{edges}")
            result = {"ok": True, "name": feat.Name, "volume": feat.Shape.Volume,
                      kind: {value_field: value, "edges": list(edges)},
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='edges') 查看最新标注"}
    return result


def fillet_edges(session: Session, edges, radius: float) -> dict[str, Any]:
    """对指定边（标签列表）做圆角。"""
    return _edge_feature(session, edges, radius, kind="fillet",
                         type_name="Part::Fillet", obj_label="Fillet", value_field="radius")


def chamfer_edges(session: Session, edges, size: float) -> dict[str, Any]:
    """对指定边（标签列表）做倒角。"""
    return _edge_feature(session, edges, size, kind="chamfer",
                         type_name="Part::Chamfer", obj_label="Chamfer", value_field="size")
