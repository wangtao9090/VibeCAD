"""面/边级特征工具（Round 5）：消费标签注册表的指代（"A 面打孔"、"E3 倒角"）。
纪律：参数校验 → 事务（内含标签指纹解析，过期即 LabelExpiredError；recompute → 几何断言）
→ 结构化 dict。校验必须先于一切 session 访问；解析在事务内，失败随事务一并回滚。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session
from vibecad.tools._integrity import (  # 从 _integrity 共享（R7 抽取）
    _count_full_cylinder_faces,
    assert_holes_intact,
    cut_tool_radii,
    hole_count_snapshot,
)


def _validate_pattern(pattern) -> list[tuple[float, float]]:
    """纯函数：校验 pattern 字典并返回相对 offset 的孔心增量列表。

    linear: 第 i 孔 = i * spacing * normalize(direction)，i=0..count-1
    （首孔增量 (0,0)，即 offset 处）
    circular: 第 i 孔 = (R*cos(2πi/N), R*sin(2πi/N))，i=0..N-1（0° 起）
    （首孔增量 (R, 0)——offset 是圆心而非首孔位置，孔心均匀分布在半径 R 的圆上）
    """
    if not isinstance(pattern, dict):
        raise ValueError(f"pattern 必须是字典（得到 {pattern!r}）")
    t = pattern.get("type")
    if t not in ("linear", "circular"):
        raise ValueError(f"pattern.type 必须是 linear 或 circular（得到 {t!r}）")

    count = pattern.get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 2 or count > 50:
        raise ValueError(f"pattern.count 必须是整数 2..50（得到 {count!r}）")

    if t == "linear":
        spacing = pattern.get("spacing")
        if spacing is None or not isinstance(spacing, (int, float)) or isinstance(spacing, bool) \
                or not math.isfinite(spacing) or spacing <= 0:
            raise ValueError(f"pattern.spacing 必须是 > 0 的有限数字（得到 {spacing!r}）")
        direction = pattern.get("direction", [1, 0])
        if (not isinstance(direction, (list, tuple)) or len(direction) != 2
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                           and math.isfinite(c) for c in direction)):
            raise ValueError(f"pattern.direction 必须是 2 个有限数字（得到 {direction!r}）")
        du, dv = float(direction[0]), float(direction[1])
        norm = math.sqrt(du * du + dv * dv)
        if norm < 1e-9:
            raise ValueError(f"pattern.direction 不能是零向量（得到 {direction!r}）")
        ndu, ndv = du / norm, dv / norm
        return [(i * spacing * ndu, i * spacing * ndv) for i in range(count)]

    # circular
    radius = pattern.get("radius")
    if radius is None or not isinstance(radius, (int, float)) or isinstance(radius, bool) \
            or not math.isfinite(radius) or radius <= 0:
        raise ValueError(f"pattern.radius 必须是 > 0 的有限数字（得到 {radius!r}）")
    return [
        (float(radius) * math.cos(2 * math.pi * i / count),
         float(radius) * math.sin(2 * math.pi * i / count))
        for i in range(count)
    ]


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
    """面的单位外法向（normalAt 不保证定向——用实体内点探针校正）。返回 FreeCAD.Vector。"""
    u0, u1, v0, v1 = face.ParameterRange
    n = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
    n.normalize()
    solid = shape.Solids[0] if getattr(shape, "Solids", None) else shape
    probe = face.CenterOfMass + n * 0.01
    if solid.isInside(probe, 1e-6, False):
        n = -n
    return n


def _drill(session, base_obj, n, e1, e2, c, length, diameter, du, dv, FreeCAD):
    """在基体上打单孔（孔心面内增量 du/dv 相对面心 c），返回新 Cut 对象。
    不做 recompute，由调用方在循环后统一执行一次。
    n/e1/e2 为 FreeCAD.Vector 或 (x,y,z) 元组，c 为 FreeCAD.Vector（面心）。
    du/dv：面内 e1/e2 方向的偏移（相对 c，mm）。"""
    lift = 0.5  # 从面外 0.5mm 起钻，避免共面布尔
    nx, ny, nz = (n.x, n.y, n.z) if hasattr(n, "x") else (float(n[0]), float(n[1]), float(n[2]))
    e1x, e1y, e1z = (e1.x, e1.y, e1.z) if hasattr(e1, "x") else tuple(float(x) for x in e1)
    e2x, e2y, e2z = (e2.x, e2.y, e2.z) if hasattr(e2, "x") else tuple(float(x) for x in e2)
    bx = c.x + e1x * du + e2x * dv + nx * lift
    by = c.y + e1y * du + e2y * dv + ny * lift
    bz = c.z + e1z * du + e2z * dv + nz * lift
    cyl = session.doc.addObject("Part::Cylinder", "HoleTool")
    cyl.Radius, cyl.Height = diameter / 2.0, length
    cyl.Placement = FreeCAD.Placement(
        FreeCAD.Vector(bx, by, bz),
        FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), FreeCAD.Vector(-nx, -ny, -nz)))
    cut = session.doc.addObject("Part::Cut", "Hole")
    cut.Base, cut.Tool = base_obj, cyl
    return cut


def add_hole(session: Session, face: str, diameter: float,
             depth: float | None = None, offset=(0.0, 0.0),
             pattern=None) -> dict[str, Any]:
    """在指定面（标签）打圆孔（单孔或阵列）。
    depth=None 通孔；offset 为面内毫米坐标（原点=面心）。
    pattern=None 单孔（行为零变化）；pattern dict → linear/circular 阵列（全有全无）。
    """
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
    # pattern 校验在任何 session 访问前（纯函数；linear 首孔在 offset 处，
    # circular 首孔在圆周 (R,0) 处——offset 是圆心）
    if pattern is not None:
        deltas = _validate_pattern(pattern)  # 抛 ValueError 若非法
    else:
        deltas = [(0.0, 0.0)]  # 单孔：count=1 同路径

    count = len(deltas)
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
            e1_tup, e2_tup = _inplane_axes((n.x, n.y, n.z))
            c = face_obj.CenterOfMass
            lift = 0.5  # 从面外 0.5mm 起钻，避免共面布尔
            length = (depth + lift) if depth is not None \
                else shape.BoundBox.DiagonalLength + 2 * lift
            base_vol = shape.Volume
            cyl_before = _count_full_cylinder_faces(shape, diameter / 2.0)
            # C2：跨径既有孔保护——快照改前全部刀具径桶的完整圆柱面计数，
            # Cut 后 assert_holes_intact 防新孔咬毁其它半径的既有孔（同径由
            # 下方"== before+count"精确断言覆盖）
            existing_counts = hole_count_snapshot(shape, cut_tool_radii(session.doc))

            # 计算所有孔的绝对面内坐标（offset + delta）
            abs_offsets = [(float(offset[0]) + float(du),
                            float(offset[1]) + float(dv)) for du, dv in deltas]

            # 链式 Cut：逐孔创建，base_obj 向后传递
            last_cut = None
            for du_abs, dv_abs in abs_offsets:
                last_cut = _drill(session, base_obj, n, e1_tup, e2_tup, c,
                                  length, diameter, du_abs, dv_abs, FreeCAD)
                base_obj = last_cut  # 下一孔以本孔 Cut 为 Base

            # 统一一次 recompute（链式 Cut 全部就绪后）
            session.doc.recompute()

            # 全套断言
            cut_shape = last_cut.Shape
            session.assert_valid_solid(cut_shape)
            if cut_shape.Volume >= base_vol - 1e-6:
                raise RuntimeError(
                    f"几何断言失败：打孔未移除任何材料（base={base_vol:.3f}, "
                    f"cut={cut_shape.Volume:.3f}）——offset/depth 可能让孔落在零件之外")
            if len(cut_shape.Solids) != 1:
                raise RuntimeError(
                    f"几何断言失败：打孔把零件切成 {len(cut_shape.Solids)} 块"
                    "——offset 可能越过零件边缘")
            cyl_after = _count_full_cylinder_faces(cut_shape, diameter / 2.0)
            if cyl_after != cyl_before + count:
                raise RuntimeError(
                    f"几何断言失败：期望完整圆孔增加 {count} 个"
                    f"（{cyl_before}→{cyl_before + count}），实际 {cyl_after}"
                    "——孔可能与零件边缘相交成开口缺口、孔间重叠（spacing<diameter），"
                    "或与已有孔/特征重叠，请调整 offset/spacing/radius")
            # C2：其它半径既有孔不得被新孔咬毁（⌀6 阵列咬掉 ⌀8 孔壁仍单 solid，
            # 须由跨径完整性快照逮住）
            assert_holes_intact(cut_shape, existing_counts)
            if depth is not None:
                # 盲孔体积核算（单孔与阵列统一）：期望移除 = count·πr²·depth。
                # 孔间不重叠由上方精确计数断言兜底，故 count 个孔的移除量可直接相加；
                # 超深打穿/侧向越界少切都会让移除量低于名义值。
                removed = base_vol - cut_shape.Volume
                expected = count * math.pi * (diameter / 2.0) ** 2 * depth
                if removed < expected * 0.99 - 1e-6:
                    raise RuntimeError(
                        f"几何断言失败：盲孔实际移除体积 {removed:.3f} < 期望 {expected:.3f}"
                        "——depth 可能超出材料厚度（已打穿）、孔越界，"
                        "或与已有孔/特征重叠，请减小 depth 或检查 offset")

            if pattern is not None:
                result = {"ok": True, "name": last_cut.Name, "volume": cut_shape.Volume,
                          "holes": {"count": count, "pattern": pattern, "diameter": diameter},
                          "labels_stale": True,
                          "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
            else:
                result = {"ok": True, "name": last_cut.Name, "volume": cut_shape.Volume,
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
