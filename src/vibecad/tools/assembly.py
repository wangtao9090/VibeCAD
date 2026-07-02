# src/vibecad/tools/assembly.py
"""装配工具（Round 8）：零件级位姿与面贴面对齐 + 干涉守卫。
纪律：校验 → 事务 → 变换 → recompute → 干涉断言（响亮，allow_interference 豁免）
→ 每零件完整性守卫 → 结构化 dict。

_align_placement 是纯 Python 向量数学（不依赖 FreeCAD），可快测。
旋转使用四元数/罗德里格斯公式实现最短弧：
  - 一般情况：绕 cross(a, b) 轴，angle = arccos(dot(a, b))
  - 反平行（a ≈ -b）：选确定轴（优先 X，若 a 接近 X 则用 Y）绕其旋转 180°
两路真机验证场景：z 共面精确 + 倒扣 180° 翻转，见 tests/test_tools_assembly.py。
"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session

_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


# ---------------------------------------------------------------------------
# 向量运算（纯 Python，不依赖 FreeCAD）
# ---------------------------------------------------------------------------

def _dot(a, b) -> float:
    return float(a[0]) * float(b[0]) + float(a[1]) * float(b[1]) + float(a[2]) * float(b[2])


def _cross(a, b) -> tuple[float, float, float]:
    ax, ay, az = float(a[0]), float(a[1]), float(a[2])
    bx, by, bz = float(b[0]), float(b[1]), float(b[2])
    return (ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx)


def _norm(v) -> float:
    return math.sqrt(_dot(v, v))


def _normalize(v) -> tuple[float, float, float]:
    n = _norm(v)
    if n < 1e-12:
        raise ValueError(f"零向量无法单位化：{v!r}")
    return (float(v[0]) / n, float(v[1]) / n, float(v[2]) / n)


def _quat_from_axis_angle(axis, angle_deg: float) -> tuple[float, float, float, float]:
    """(w, x, y, z) 单位四元数，axis 必须已单位化。"""
    half = math.radians(angle_deg) / 2.0
    s = math.sin(half)
    return (math.cos(half), float(axis[0]) * s, float(axis[1]) * s, float(axis[2]) * s)


def _quat_mul(q1, q2) -> tuple[float, float, float, float]:
    """四元数乘法 q1 * q2（Hamilton 积）。"""
    w1, x1, y1, z1 = q1
    w2, x2, y2, z2 = q2
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def _quat_rotate(q, v) -> tuple[float, float, float]:
    """用四元数 q 旋转向量 v：q * (0,v) * q^{-1}。"""
    w, qx, qy, qz = q
    vx, vy, vz = float(v[0]), float(v[1]), float(v[2])
    # q * (0,v) * q*
    # 直接展开：t = 2 * cross(q_vec, v) + 2w*q_vec？用标准公式
    qv = (0.0, vx, vy, vz)
    q_conj = (w, -qx, -qy, -qz)
    _, rx, ry, rz = _quat_mul(_quat_mul(q, qv), q_conj)
    return (rx, ry, rz)


def _shortest_arc_rotation(
        a, b
) -> tuple[tuple[float, float, float], float]:
    """最短弧把单位向量 a 转到单位向量 b。
    返回 (axis, angle_deg)。

    反平行（dot < -1+1e-9）选确定轴：优先取 X 轴，若 a 与 X 轴平行（|ax|>0.9）
    则取 Y 轴，保证确定性（见模块文档）。
    """
    a = _normalize(a)
    b = _normalize(b)
    d = max(-1.0, min(1.0, _dot(a, b)))

    if abs(d - 1.0) < 1e-9:
        # a ≈ b，恒等旋转，轴取任意（不影响角度=0）
        return ((1.0, 0.0, 0.0), 0.0)

    if d < -1.0 + 1e-9:
        # 反平行：180° 绕确定垂直轴
        if abs(float(a[0])) < 0.9:
            perp = _normalize(_cross(a, (1.0, 0.0, 0.0)))
        else:
            perp = _normalize(_cross(a, (0.0, 1.0, 0.0)))
        return (perp, 180.0)

    axis = _normalize(_cross(a, b))
    angle_deg = math.degrees(math.acos(d))
    return (axis, angle_deg)


# ---------------------------------------------------------------------------
# 核心纯数学函数
# ---------------------------------------------------------------------------

def _align_placement(
        *,
        moving_normal,
        moving_center,
        target_normal,
        target_center,
        target_e1,
        target_e2,
        offset,
        gap: float,
) -> tuple[tuple[float, float, float], tuple[tuple[float, float, float], float]]:
    """纯数学：返回 (平移向量, (旋转轴, 角度_deg))。

    旋转 = 最短弧把 moving_normal 转到 -target_normal（面贴面：法向对向）；
    锚点 = target_center + target_normal*gap + e1*offset[0] + e2*offset[1]；
    平移 = 锚点 - R(moving_center)。
    纯 Python 向量运算，不依赖 FreeCAD（可快测）。

    调用方责任：面贴面只控制法向自转（法向自转不指定——若需精确朝向请先
    place_part 调整法向自转后再 align，docstring 契约）。
    """
    mn = _normalize(moving_normal)
    tn = _normalize(target_normal)
    # 目标：moving_normal → -target_normal
    target_dir = (-tn[0], -tn[1], -tn[2])
    axis, angle_deg = _shortest_arc_rotation(mn, target_dir)

    q = _quat_from_axis_angle(axis, angle_deg)
    rotated_mc = _quat_rotate(q, moving_center)

    # 锚点 = target_center + tn*gap + e1*off0 + e2*off1
    e1 = _normalize(target_e1)
    e2 = _normalize(target_e2)
    off0, off1 = float(offset[0]), float(offset[1])
    gf = float(gap)
    anchor = (
        float(target_center[0]) + tn[0] * gf + e1[0] * off0 + e2[0] * off1,
        float(target_center[1]) + tn[1] * gf + e1[1] * off0 + e2[1] * off1,
        float(target_center[2]) + tn[2] * gf + e1[2] * off0 + e2[2] * off1,
    )
    translation = (
        anchor[0] - rotated_mc[0],
        anchor[1] - rotated_mc[1],
        anchor[2] - rotated_mc[2],
    )
    return translation, (axis, angle_deg)


class InterferenceReport(list):
    """assert_no_interference 的返回值：仍是原干涉清单 list（含 note 条目，
    对 == []/索引/迭代/真值判断完全透明），额外挂 interference_skipped 标记
    （R8 移交项）——可比较的非空零件数 < 2（单零件模式，或零件不足两个非空）
    时，干涉检查根本没有执行任何布尔比较，避免调用方把"没跑"误读成"跑了但
    无干涉"（两者都表现为清单为空，语义却完全不同）。
    """

    def __init__(self, iterable=(), *, interference_skipped: bool):
        super().__init__(iterable)
        self.interference_skipped = interference_skipped


# ---------------------------------------------------------------------------
# 干涉守卫
# ---------------------------------------------------------------------------

def assert_no_interference(session: Session, *, allow: bool = False,
                            context: str = "") -> InterferenceReport:
    """对每对零件全局 shape 计算 common().Volume > 1e-6 → 干涉。
    allow=False 时抛 RuntimeError（报零件对+干涉量）；
    allow=True 时放行并返回干涉清单 [{"parts": [a, b], "volume": v}]。
    单零件模式（_parts 空）直接返回空列表。

    返回值是 InterferenceReport（list 子类），额外带 .interference_skipped：
    可比较的非空零件数 < 2 时为 True——检查没有真的跑过一次布尔比较，调用方
    （place_part/align_parts 的 result、describe_assembly）借此把该信号透传到
    结果 dict 的 interference_skipped 字段，不与"跑了但无干涉"混淆。

    "绝不静默"纪律（终审 C-B：此前 except Exception → vol=0.0 让 9000mm³ 真实
    重叠在内部状态异常时被静默放行——最关键守卫上的唯一破口）：
    - 空零件（objects 空，new_part 后未建几何）：无 shape 可相交，跳过该零件的
      全部配对，但在返回清单尾部追加 {"parts": [X], "volume": None, "note":
      "零件 X 无几何，干涉未检查"} 显式注明——消费方（align/place 的 result、
      describe_assembly）原样送达，绝不静默 []。note 条目不参与 allow=False
      的拒绝判定（无确证干涉不能拒）。
    - get_result_shape 的 RuntimeError（有对象却无 solid = 状态异常）直接上抛；
    - OCC 布尔崩溃窄抓 Part.OCCError 转 RuntimeError 报"干涉检查无法完成"。
    """
    if not session._parts:
        return InterferenceReport([], interference_skipped=True)

    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with silence_fd1():
        import Part  # noqa: PLC0415

    names = [n for n in session._parts if session._parts[n]["objects"]]
    skipped = [n for n in session._parts if not session._parts[n]["objects"]]
    interferences: list[dict] = []

    with silence_fd1():
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                sa = session.get_result_shape(a).transformed(
                    session._parts[a]["container"].Placement.toMatrix())
                sb = session.get_result_shape(b).transformed(
                    session._parts[b]["container"].Placement.toMatrix())
                try:
                    vol = sa.common(sb).Volume
                except Part.OCCError as exc:
                    raise RuntimeError(
                        f"干涉检查无法完成（{a}↔{b}）：{exc}"
                        "——几何状态异常，请检查零件") from exc

                if vol > 1e-6:
                    interferences.append({"parts": [a, b], "volume": round(vol, 6)})

    if interferences and not allow:
        pairs = ", ".join(f"{it['parts'][0]}↔{it['parts'][1]}({it['volume']:.3f}mm³)"
                          for it in interferences)
        ctx = f"（{context}）" if context else ""
        raise RuntimeError(
            f"装配干涉{ctx}：{pairs}——零件重叠，请调整位置或使用 allow_interference=True 放行")
    for n in skipped:
        interferences.append({"parts": [n], "volume": None,
                              "note": f"零件 {n} 无几何，干涉未检查"})
    return InterferenceReport(interferences, interference_skipped=len(names) < 2)


# ---------------------------------------------------------------------------
# place_part
# ---------------------------------------------------------------------------

def place_part(
        session: Session,
        part: str,
        position=None,
        rotation_axis: str | None = None,
        angle: float | None = None,
) -> dict[str, Any]:
    """设置零件绝对位置 and/or 绕零件全局 BoundBox 中心叠加旋转。

    校验：
    - part 非空字符串
    - position/rotation_axis+angle 至少给一项
    - position 3 个有限数字
    - rotation_axis x/y/z
    - angle 非零，(-360, 360) 内有限数字
    """
    # --- 校验 ---
    if not part or not isinstance(part, str):
        raise ValueError("part 必须是非空字符串")
    if position is None and (rotation_axis is None and angle is None):
        raise ValueError("至少提供 position 或 rotation_axis+angle 之一")
    if position is not None:
        if (not isinstance(position, (list, tuple)) or len(position) != 3
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                           and math.isfinite(c) for c in position)):
            raise ValueError(f"position 必须是 3 个有限数字 (x,y,z)（得到 {position!r}）")
    if rotation_axis is not None or angle is not None:
        if rotation_axis not in _AXES:
            raise ValueError(f"axis 必须是 x/y/z（得到 {rotation_axis!r}）")
        if (angle is None or not isinstance(angle, (int, float)) or isinstance(angle, bool)
                or not math.isfinite(angle) or angle == 0
                or not -360 < angle < 360):
            raise ValueError(f"angle 必须是 (-360, 360) 内非零有限角度（得到 {angle!r}）")

    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    with session._transaction(f"place_part:{part}"):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415

            if part not in session._parts:
                raise ValueError(f"零件 {part!r} 不存在（已有零件：{list(session._parts)}）")

            container = session._parts[part]["container"]
            pl = container.Placement

            # 绝对 position：直接设置 Base
            if position is not None:
                pl = FreeCAD.Placement(
                    FreeCAD.Vector(*[float(c) for c in position]),
                    pl.Rotation,
                )

            # 旋转：绕零件全局 BoundBox 中心叠加（与 R7 transform spike 验证写法一致）
            if rotation_axis is not None:
                # 取全局 BoundBox（先把局部 shape 应用当前 Placement）
                result_shape = session.get_result_shape(part)
                global_shape = result_shape.transformed(pl.toMatrix())
                bb = global_shape.BoundBox
                center = FreeCAD.Vector(
                    (bb.XMin + bb.XMax) / 2,
                    (bb.YMin + bb.YMax) / 2,
                    (bb.ZMin + bb.ZMax) / 2,
                )
                axis_vec = FreeCAD.Vector(*_AXES[rotation_axis])
                rot = FreeCAD.Rotation(axis_vec, float(angle))
                # 左乘（绕 center 旋转）
                pl = FreeCAD.Placement(FreeCAD.Vector(), rot, center).multiply(pl)

            container.Placement = pl
            session.doc.recompute()

            # 完整性守卫：每零件 single_solid + valid（空零件跳过——与干涉守卫/渲染一致，
            # 否则 new_part 后未建几何的零件会让无关操作误炸且错误归因）
            for pname in session._parts:
                if not session._parts[pname]["objects"]:
                    continue
                shape = session.get_result_shape(pname)
                session.assert_valid_solid(shape)
                _assert_single_solid_for_part(shape, pname, f"place_part:{part}")

            # 干涉断言
            interferences = assert_no_interference(session, context=f"place_part:{part}")

            final_pl = container.Placement
            return {
                "ok": True,
                "part": part,
                "placement": {
                    "position": [final_pl.Base.x, final_pl.Base.y, final_pl.Base.z],
                    "axis": rotation_axis,
                    "angle": angle,
                },
                "interference": interferences,
                "interference_skipped": interferences.interference_skipped,
                "labels_stale": True,
                "hint": "零件位置已更新，调用 render_part(annotate='faces') 查看最新标注",
            }


# ---------------------------------------------------------------------------
# align_parts
# ---------------------------------------------------------------------------

def align_parts(
        session: Session,
        moving_part: str,
        moving_face: str,
        target_part: str,
        target_face: str,
        offset=(0.0, 0.0),
        gap: float = 0.0,
        allow_interference: bool = False,
) -> dict[str, Any]:
    """面贴面对齐：moving_part 的 moving_face 贴向 target_part 的 target_face。

    校验：
    - moving_part/moving_face/target_part/target_face 非空
    - moving_part != target_part（不同零件）
    - gap 有限数字
    流程：校验 → 事务 → resolve_face（跨零件标签）→ 取全局法向/面心（经容器
    Placement 变换）→ _align_placement → 结果 Placement 左乘到 moving 容器
    现有 Placement → recompute → 干涉断言 → 每零件完整性守卫 → result。
    """
    # --- 校验 ---
    if not moving_part or not isinstance(moving_part, str):
        raise ValueError("moving_part 必须是非空字符串")
    if not moving_face or not isinstance(moving_face, str):
        raise ValueError("moving_face 必须是非空字符串")
    if not target_part or not isinstance(target_part, str):
        raise ValueError("target_part 必须是非空字符串")
    if not target_face or not isinstance(target_face, str):
        raise ValueError("target_face 必须是非空字符串")
    if moving_part == target_part:
        raise ValueError(f"moving_part 与 target_part 必须是不同零件（都是 {moving_part!r}）")
    if (not isinstance(gap, (int, float)) or isinstance(gap, bool)
            or not math.isfinite(gap)):
        raise ValueError(f"gap 必须是有限数字（得到 {gap!r}）")

    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    from vibecad.tools.features import _inplane_axes, _outward_normal  # noqa: PLC0415

    with session._transaction(f"align_parts:{moving_part}→{target_part}"):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415

            # ---- 解析面（各自零件命名空间）----
            m_face_idx = session.resolve_face(moving_face, part=moving_part)
            t_face_idx = session.resolve_face(target_face, part=target_part)

            m_local_shape = session.get_result_shape(moving_part)
            t_local_shape = session.get_result_shape(target_part)

            m_face = m_local_shape.Faces[m_face_idx]
            t_face = t_local_shape.Faces[t_face_idx]

            m_container = session._parts[moving_part]["container"]
            t_container = session._parts[target_part]["container"]
            m_pl = m_container.Placement
            t_pl = t_container.Placement

            # ---- 取全局法向/面心（局部坐标 → 经容器 Placement 变换）----
            # 法向：Rotation.multVec（不平移，方向量）
            m_local_n = _outward_normal(m_local_shape, m_face)
            t_local_n = _outward_normal(t_local_shape, t_face)
            m_global_n = m_pl.Rotation.multVec(m_local_n)
            t_global_n = t_pl.Rotation.multVec(t_local_n)
            m_global_n.normalize()
            t_global_n.normalize()

            # 面心：全量 Placement.multVec（含平移）
            m_local_c = m_face.CenterOfMass
            t_local_c = t_face.CenterOfMass
            m_global_c = m_pl.multVec(m_local_c)
            t_global_c = t_pl.multVec(t_local_c)

            # target 面内正交基（用全局法向，跨零件 offset 方向直观）
            t_n_tuple = (t_global_n.x, t_global_n.y, t_global_n.z)
            e1_tup, e2_tup = _inplane_axes(t_n_tuple)

            # ---- 纯数学对齐 ----
            translation, (rot_axis, rot_angle_deg) = _align_placement(
                moving_normal=(m_global_n.x, m_global_n.y, m_global_n.z),
                moving_center=(m_global_c.x, m_global_c.y, m_global_c.z),
                target_normal=(t_global_n.x, t_global_n.y, t_global_n.z),
                target_center=(t_global_c.x, t_global_c.y, t_global_c.z),
                target_e1=e1_tup,
                target_e2=e2_tup,
                offset=offset,
                gap=float(gap),
            )

            # ---- 构建新 Placement 并左乘到 moving 容器现有 Placement ----
            t_vec = FreeCAD.Vector(*translation)
            if rot_angle_deg != 0.0:
                rot_vec = FreeCAD.Vector(*rot_axis)
                rot = FreeCAD.Rotation(rot_vec, rot_angle_deg)
            else:
                rot = FreeCAD.Rotation()

            # 新 Placement = (translation, rotation) 左乘现有 Placement
            # 说明：_align_placement 返回的是全局增量（绝对 delta）
            # 左乘 = delta_pl * m_pl
            delta_pl = FreeCAD.Placement(t_vec, rot)
            m_container.Placement = delta_pl.multiply(m_pl)
            session.doc.recompute()

            # ---- 完整性守卫 ----（空零件跳过，理由同 place_part）
            for pname in session._parts:
                if not session._parts[pname]["objects"]:
                    continue
                shape = session.get_result_shape(pname)
                session.assert_valid_solid(shape)
                _assert_single_solid_for_part(
                    shape, pname, f"align_parts:{moving_part}→{target_part}")

            # ---- 干涉断言 ----
            interferences = assert_no_interference(
                session,
                allow=allow_interference,
                context=f"align_parts:{moving_part}→{target_part}",
            )

            final_pl = m_container.Placement
            return {
                "ok": True,
                "moving_part": moving_part,
                "target_part": target_part,
                "placement": {
                    "position": [final_pl.Base.x, final_pl.Base.y, final_pl.Base.z],
                    "rotation_angle_deg": rot_angle_deg,
                },
                "gap": gap,
                "interference": interferences,
                "interference_skipped": interferences.interference_skipped,
                "labels_stale": True,
                "hint": "装配对齐完成，调用 render_part(annotate='faces') 查看最新标注",
            }


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _assert_single_solid_for_part(shape: Any, part_name: str, context: str) -> None:
    """装配模式下每零件独立断言单 solid。"""
    n = len(shape.Solids)
    if n != 1:
        raise RuntimeError(
            f"几何断言失败（{context}）：零件 {part_name!r} 被操作切成 {n} 块"
            "——操作可能使孔或特征越过零件边缘")
