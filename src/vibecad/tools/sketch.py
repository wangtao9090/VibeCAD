"""草图拉伸工具（Round 7）：profile DSL → Part.Face → extrude → Fuse/Cut。
纪律：参数校验 → 事务（face 解析+平面校验）→ recompute → 完整性守卫全套 → 结构化 dict。
已知取舍（注明）：extrude 产物是静态 shape 非参数化——modify_part 不可改其尺寸；
profile 自交多边形不做预检，靠 Face 构造失败/几何断言兜底。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session
from vibecad.tools._integrity import (
    assert_holes_intact,
    assert_no_sealed_holes,
    assert_not_touched,
    assert_single_solid,
    cut_tool_radii,
    hole_count_snapshot,
)
from vibecad.tools.features import _inplane_axes, _outward_normal

_PROFILE_REQUIRED: dict[str, tuple[str, ...]] = {
    "rect": ("length", "width"),
    "circle": ("radius",),
    "polygon": ("points",),
    "slot": ("length", "width"),
}


def _validate_profile(profile) -> None:
    """校验 profile 字典：dict + 合法 type + 各 type 必填参数 > 0 有限 / points ≥ 3 且每点 2 数。"""
    if not isinstance(profile, dict):
        raise ValueError(f"profile 必须是字典（得到 {profile!r}）")
    t = profile.get("type")
    if t not in _PROFILE_REQUIRED:
        raise ValueError(
            f"profile.type 必须是 rect/circle/polygon/slot（得到 {t!r}）")
    if t == "polygon":
        pts = profile.get("points")
        if not isinstance(pts, (list, tuple)) or len(pts) < 3:
            raise ValueError(
                f"polygon.points 必须是 ≥ 3 个 [x, y] 点列表（得到 {pts!r}）")
        for p in pts:
            if (not isinstance(p, (list, tuple)) or len(p) != 2
                    or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                               and math.isfinite(c) for c in p)):
                raise ValueError(
                    f"polygon.points 每个点必须是 2 个有限数字（得到 {p!r}）")
    else:
        for field in _PROFILE_REQUIRED[t]:
            v = profile.get(field)
            if v is None or not isinstance(v, (int, float)) or isinstance(v, bool) \
                    or not math.isfinite(v) or v <= 0:
                raise ValueError(
                    f"profile.{field} 必须是 > 0 的有限数字（得到 {v!r}）")


def _profile_area(profile: dict) -> float:
    """纯函数：计算 profile 面积（断言容差用）。"""
    t = profile["type"]
    if t == "rect":
        return float(profile["length"]) * float(profile["width"])
    if t == "circle":
        return math.pi * float(profile["radius"]) ** 2
    if t == "slot":
        # length = 两半圆心距（直段长），width = 轨道宽，总面积 = length*width + π*(width/2)²
        w = float(profile["width"])
        return float(profile["length"]) * w + math.pi * (w / 2) ** 2
    # polygon：shoelace 公式
    pts = profile["points"]
    n = len(pts)
    s = sum(
        pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
        for i in range(n)
    )
    return abs(s) / 2


def _make_face(profile: dict, Part, FreeCAD):
    """在局部 XY 平面（原点居中）构造 Part.Face。
    rect：四线段 makePolygon 闭合。
    circle：Part.makeCircle 整圆 Edge → Wire。
    polygon：makePolygon(points + 首点) 闭合。
    slot：两 LineSegment + 两三点 Arc（Task 0 spike 验证写法）→ Part.Wire。
    构造失败让异常上抛（几何断言纪律：响亮）。
    """
    t = profile["type"]
    if t == "rect":
        L, W = float(profile["length"]) / 2, float(profile["width"]) / 2
        pts = [
            FreeCAD.Vector(-L, -W, 0),
            FreeCAD.Vector(L, -W, 0),
            FreeCAD.Vector(L, W, 0),
            FreeCAD.Vector(-L, W, 0),
            FreeCAD.Vector(-L, -W, 0),  # 闭合
        ]
        wire = Part.makePolygon(pts)
        return Part.Face(wire)

    if t == "circle":
        r = float(profile["radius"])
        edge = Part.makeCircle(r)
        wire = Part.Wire([edge])
        return Part.Face(wire)

    if t == "polygon":
        pts_raw = profile["points"]
        pts = [FreeCAD.Vector(float(p[0]), float(p[1]), 0) for p in pts_raw]
        pts.append(pts[0])  # 闭合
        wire = Part.makePolygon(pts)
        return Part.Face(wire)

    # slot：length=两圆心距，width=轨道宽（即 2r）
    # 拓扑：左圆心(-L/2,0)、右圆心(L/2,0)，r=width/2
    # 两直线段（上下）+ 两个三点弧（左右）→ Wire 闭合
    L = float(profile["length"])
    r = float(profile["width"]) / 2
    # 关键点
    p_tr = FreeCAD.Vector(L / 2, r, 0)           # 右上
    p_br = FreeCAD.Vector(L / 2, -r, 0)          # 右下
    p_bl = FreeCAD.Vector(-L / 2, -r, 0)         # 左下
    p_tl = FreeCAD.Vector(-L / 2, r, 0)          # 左上
    p_rm = FreeCAD.Vector(L / 2 + r, 0, 0)       # 右弧中点（三点弧）
    p_lm = FreeCAD.Vector(-(L / 2 + r), 0, 0)    # 左弧中点（三点弧）
    edge_top = Part.LineSegment(p_tl, p_tr).toShape()
    edge_right = Part.Arc(p_tr, p_rm, p_br).toShape()
    edge_bot = Part.LineSegment(p_br, p_bl).toShape()
    edge_left = Part.Arc(p_bl, p_lm, p_tl).toShape()
    wire = Part.Wire([edge_top, edge_right, edge_bot, edge_left])
    return Part.Face(wire)


def extrude_profile(session: Session, profile, height: float,
                    face: str | None = None, offset=(0.0, 0.0),
                    operation: str = "pad") -> dict[str, Any]:
    """拉伸 profile 轮廓（pad 加料 / pocket 减料）。
    face=None → 全局 XY 平面 z=0（仅空文档建底板；文档已有零件时响亮拒绝防孤儿实体）。
    face=标签 → R5 面标签解析 + 平面校验 + e1/e2 坐标系放置。
    取向约定：profile 局部 X=面内 u（e1）、局部 Y=面内 v（e2）——rect 的 length 沿 e1、
    width 沿 e2，与 offset 的 (u, v) 同一坐标系（旋转用 e1/e2/n 显式矩阵，取向确定）。
    pad：沿面外法向拉 height，与基体 Part::Fuse（空文档直接成首个零件）。
    pocket：沿面内法向挖深 height，Part::Cut（无基体 ValueError）。
    断言：pad 体积增量 ≈ area×height（双边 1%+1e-6）+ 孔密封内腔探针（pad 盖住
    孔口即拒）；pocket 移除量 ≈ area×height（双边——打穿板厚/越出面即拒）+ 单 solid
    + 孔完整性快照不退化；Touched/有效性全套。
    注：不调 assert_result_not_drifted——pad/pocket 创建新结果对象（Fuse/Cut）
    是预期行为，"漂移"语义不适用（该断言针对刀具吞件后 fallback 漂移到刀具的场景）。
    """
    # ─── 参数校验（先于任何 session 访问）───
    _validate_profile(profile)
    if not isinstance(height, (int, float)) or isinstance(height, bool) \
            or not math.isfinite(height) or height <= 0:
        raise ValueError(f"height 必须是 > 0 的有限数字（得到 {height!r}）")
    if (not isinstance(offset, (list, tuple)) or len(offset) != 2
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                       and math.isfinite(c) for c in offset)):
        raise ValueError(f"offset 必须是 2 个有限数字 (u, v)（得到 {offset!r}）")
    if operation not in ("pad", "pocket"):
        raise ValueError(f"operation 必须是 pad 或 pocket（得到 {operation!r}）")

    area = _profile_area(profile)
    height_orig = float(height)  # 记录原始 height，pocket 路径会修改局部变量

    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("extrude_profile"):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            import Part  # noqa: PLC0415

            # ─── 确定放置平面 ───
            if face is None:
                if operation == "pocket":
                    raise ValueError(
                        "pocket 需要有基体对象（face=标签），无基体时无法减料")
                # C1：face=None 仅用于空文档建底板——文档已有零件时创建的是
                # 不相连孤儿实体且会劫持 get_result_object，必须响亮拒绝
                try:
                    session.get_result_object()
                    has_existing = True
                except RuntimeError:
                    has_existing = False
                if has_existing:
                    raise ValueError(
                        "文档已有零件，请用 face 指定放置面（如 face='A'）"
                        "——face=None 仅用于空文档建底板")
                # 全局 XY 平面，法向 +Z，基：原点 + offset
                nx, ny, nz = 0.0, 0.0, 1.0
                e1_tup = (1.0, 0.0, 0.0)
                e2_tup = (0.0, 1.0, 0.0)
                cx = float(offset[0])
                cy = float(offset[1])
                cz = 0.0
                base_vol = 0.0
                has_base = False
                base_obj = None
                hole_counts: dict[float, int] = {}
            else:
                if not isinstance(face, str) or not face:
                    raise ValueError("face 必须是非空字符串（面标签）")
                idx = session.resolve_face(face)
                base_obj = session.get_result_object()
                shape = base_obj.Shape
                face_obj = shape.Faces[idx]
                surface = type(face_obj.Surface).__name__
                if surface != "Plane":
                    raise ValueError(f"标签 {face} 是 {surface}，只能在平面上拉伸")
                n_vec = _outward_normal(shape, face_obj)
                nx, ny, nz = n_vec.x, n_vec.y, n_vec.z
                e1_tup, e2_tup = _inplane_axes((nx, ny, nz))
                c = face_obj.CenterOfMass
                cx = c.x + e1_tup[0] * float(offset[0]) + e2_tup[0] * float(offset[1])
                cy = c.y + e1_tup[1] * float(offset[0]) + e2_tup[1] * float(offset[1])
                cz = c.z + e1_tup[2] * float(offset[0]) + e2_tup[2] * float(offset[1])
                base_vol = shape.Volume
                has_base = True
                radii = cut_tool_radii(session.doc)
                hole_counts = hole_count_snapshot(shape, radii)

            # ─── 构造局部 Face，extrude → solid ───
            local_face = _make_face(profile, Part, FreeCAD)

            # extrude 方向：pad 沿 +normal（向外），pocket 沿 -normal（向内）
            # 与 add_hole 同理：起点在面外 lift=0.5mm 防共面布尔
            # pocket: 起点 = 面 + n*lift（面外），向 -n 拉 height+lift（穿过面 height_orig 深）
            #         与基体 Cut → 切深恰好 height_orig（面外 lift 段在材料外不切）
            lift = 0.5
            if operation == "pad":
                extrude_dir = FreeCAD.Vector(nx, ny, nz)
                origin = FreeCAD.Vector(cx, cy, cz)
                extrude_height = height_orig
            else:
                # pocket：起点在面外 +lift，向 -normal 拉 height_orig+lift
                extrude_dir = FreeCAD.Vector(-nx, -ny, -nz)
                origin = FreeCAD.Vector(
                    cx + nx * lift,
                    cy + ny * lift,
                    cz + nz * lift,
                )
                extrude_height = height_orig + lift

            # I3：取向必须确定——Rotation(Z→dir) 最短弧使非对称轮廓在侧面的取向
            # 不可控（n=-Z 时甚至数学不定）。用 e1/e2/n 显式构造旋转矩阵：
            # 旋转列 = (e1, e2, n)，即 profile 局部 X→面内 u（e1）、局部 Y→面内 v
            # （e2）、局部 Z→外法向 n（e2=n×e1 → e1×e2=n，右手系成立）。
            # rect 的 length 方向永远沿 e1（offset 的 u 方向），与 docstring 契约一致。
            m = FreeCAD.Matrix(
                e1_tup[0], e2_tup[0], nx, origin.x,
                e1_tup[1], e2_tup[1], ny, origin.y,
                e1_tup[2], e2_tup[2], nz, origin.z,
                0, 0, 0, 1)
            local_face.Placement = FreeCAD.Placement(m)
            solid = local_face.extrude(extrude_dir * extrude_height)

            # ─── 创建 Part::Feature 包装静态 solid ───
            feat = session.doc.addObject("Part::Feature", "Profile")
            feat.Shape = solid

            expected_delta = area * height_orig  # pad 增量 / pocket 移除量的名义值
            tol = expected_delta * 0.01 + 1e-6   # 双边容差 1%+1e-6（计划要求）

            if not has_base:
                # 无基体 pad：Feature 即结果（Task 0 spike 验证写法）
                session.doc.recompute()
                result_shape = feat.Shape
                result_name = feat.Name
                session.assert_valid_solid(result_shape)
                assert_single_solid(result_shape, "extrude_profile(pad, no base)")
                vol_after = result_shape.Volume
                if abs(vol_after - expected_delta) > tol:
                    raise RuntimeError(
                        f"几何断言失败：底板体积 {vol_after:.3f} ≠ 期望 "
                        f"area×height={expected_delta:.3f}（容差 1%）"
                        "——轮廓构造可能异常（如自交多边形）")
            else:
                # 有基体：Fuse（pad）或 Cut（pocket）
                if operation == "pad":
                    op_obj = session.doc.addObject("Part::Fuse", "PadFuse")
                    op_obj.Base = base_obj
                    op_obj.Tool = feat
                else:
                    op_obj = session.doc.addObject("Part::Cut", "Pocket")
                    op_obj.Base = base_obj
                    op_obj.Tool = feat

                session.doc.recompute()
                result_shape = op_obj.Shape
                result_name = op_obj.Name
                session.assert_valid_solid(result_shape)
                assert_not_touched(op_obj, "extrude_profile")
                # 不调 assert_result_not_drifted：pad/pocket 创建新结果对象
                # （Fuse/Cut 成为 get_result_object 最新节点）是预期行为，
                # "漂移"语义不适用——该断言针对刀具吞件后 fallback 漂移到刀具。
                assert_single_solid(result_shape, "extrude_profile")
                vol_after = result_shape.Volume

                if operation == "pad":
                    vol_increase = vol_after - base_vol
                    if abs(vol_increase - expected_delta) > tol:
                        raise RuntimeError(
                            f"几何断言失败：pad 体积增量 {vol_increase:.3f} ≠ 期望 "
                            f"area×height={expected_delta:.3f}（容差 1%）"
                            "——轮廓可能部分嵌入基体、悬空或越出面边缘")
                    # I2：pad 盖住既有孔口 → 孔变密封内腔（不可加工）→ 拒
                    assert_no_sealed_holes(session.doc, result_shape)
                else:
                    # pocket：移除量双边核算（I1——超深打穿板厚时实际移除量
                    # 低于名义值；意外多切则高于名义值）
                    removed = base_vol - vol_after
                    if removed < expected_delta * 0.99 - 1e-6:
                        raise RuntimeError(
                            f"几何断言失败：pocket 实际移除 {removed:.3f} < 期望 "
                            f"{expected_delta:.3f}——深度可能打穿板厚、轮廓越出面"
                            "或与既有孔重叠，请减小深度/调整轮廓")
                    if removed > expected_delta * 1.01 + 1e-6:
                        raise RuntimeError(
                            f"几何断言失败：pocket 实际移除 {removed:.3f} > 期望 "
                            f"{expected_delta:.3f}——切除量异常（轮廓或深度参数"
                            "与几何不符），请检查参数")
                    assert_holes_intact(result_shape, hole_counts)

    # ─── result dict ───
    return {
        "ok": True,
        "name": result_name,
        "volume": vol_after,
        "extrude": {
            "profile": profile.get("type"),
            "area": area,
            "height": height_orig,
            "operation": operation,
        },
        "parametric": False,
        "labels_stale": True,
        "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注",
    }
