# src/vibecad/tools/transform.py
"""reposition 工具（Round 7）：移动/旋转已有图元对象，依赖链自动重算。
纪律：校验 → 事务 → 改 Placement → recompute → 完整性守卫（_integrity）→ 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session
from vibecad.tools import _integrity
from vibecad.tools._integrity import assert_solid_integrity

_MOVABLE = ("Part::Box", "Part::Cylinder")  # Cut/Fillet/Chamfer 跟随 Base，不可直接 repos
_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


def _validate_position(position) -> None:
    if (not isinstance(position, (list, tuple)) or len(position) != 3
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                       and math.isfinite(c) for c in position)):
        raise ValueError(f"position 必须是 3 个有限数字 (x, y, z)（得到 {position!r}）")


def _movable_obj(session: Session, name: str):
    """查找可移动对象；不存在时用 KeyError→ValueError 同款错误（与 modify.py 一致）。"""
    try:
        obj = session.get_object(name)
    except KeyError as exc:
        names = [o.Name for o in session.doc.Objects
                 if getattr(o, "TypeId", "") in _MOVABLE]
        raise ValueError(
            f"对象 {name!r} 不存在——文档现有可操作对象：{names or '（无）'}") from exc
    if getattr(obj, "TypeId", "") not in _MOVABLE:
        names = [o.Name for o in session.doc.Objects
                 if getattr(o, "TypeId", "") in _MOVABLE]
        raise ValueError(
            f"对象 {name!r}（{getattr(obj, 'TypeId', '?')}）不可直接移动/旋转"
            f"（布尔/圆角结果跟随其图元）——可操作对象：{names or '（无）'}")
    return obj


def _reposition(session: Session, name: str, apply, op: str) -> dict[str, Any]:
    """共享骨架：守卫快照 → apply(obj, FreeCAD) 改 Placement → recompute → 全套断言。
    注：reposition 后结果体积允许变化（移动孔刀具改变相交区是合法目的），
    不做体积断言；越界/切空/缺口由孔完整性快照与单 solid 断言兜住；
    密封内腔（孔口被完全封死、不可加工）由孔端面探针断言兜住。
    已知盲区（I2 审查后确认放行）：通孔被移动封住一端变盲孔时放行不警示——
    孔端面探针只拒"两端都封死"的密封内腔；区分"本来就是盲孔"与"通孔被封一端"
    需追踪每孔的创建意图（成本高），且盲孔本身是合法几何，故放行。
    锚定纪律（终审 C-D）：装配模式全部快照/断言锚定**被操作对象所属零件**
    （owner）——active=B 时移 A 的 HoleTool，用 B 的 shape 做快照会让 A 的孔
    被静默吞掉还报 ok；owner 反查不到（_parts 非空但对象无归属）= 状态异常拒绝。"""
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction(op):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            obj = _movable_obj(session, name)
            owner = session.owner_of(obj.Name)
            if session._parts and owner is None:
                raise ValueError(
                    f"对象 {obj.Name!r} 不属于任何已注册零件"
                    f"（已有零件：{list(session._parts)}）——装配状态异常，拒绝操作")
            owner_names = session._parts[owner]["objects"] if owner is not None else None
            before_name = session.get_result_object(owner).Name
            shape_before = session.get_result_shape(owner)
            radii = _integrity.cut_tool_radii(session.doc)
            counts = _integrity.hole_count_snapshot(shape_before, radii)
            apply(obj, FreeCAD)
            session.doc.recompute()
            _integrity.assert_not_touched(obj, op)
            _integrity.assert_result_not_drifted(session, before_name, part=owner)
            shape = session.get_result_shape(owner)
            session.assert_valid_solid(shape)
            assert_solid_integrity(session, shape, op, part=owner)
            _integrity.assert_holes_intact(shape, counts)
            _integrity.assert_no_sealed_holes(session.doc, shape, owner_names=owner_names)
            pl = obj.Placement
            result = {"ok": True, "name": obj.Name, "volume": shape.Volume,
                      op: {"position": [pl.Base.x, pl.Base.y, pl.Base.z]},
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
    return result


def move_part(session: Session, name: str, position) -> dict[str, Any]:
    """把图元移动到绝对位置（依赖链自动重算）。同值 no-op 拒绝（与 modify 拉齐）。"""
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串（对象名，见 parts 字段）")
    _validate_position(position)

    def _apply(obj, FreeCAD):
        pl = obj.Placement
        new_base = FreeCAD.Vector(*[float(c) for c in position])
        if (pl.Base - new_base).Length < 1e-9:  # 同值 no-op 与 modify 拉齐
            raise ValueError(f"对象已在该位置 {list(position)!r}")
        pl.Base = new_base
        obj.Placement = pl

    return _reposition(session, name, _apply, "move")


def rotate_part(
        session: Session, name: str, axis: str = "z", angle: float = 90.0,
) -> dict[str, Any]:
    """绕全局轴、以对象 BoundBox 几何中心为旋转中心旋转（角度制）。"""
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串（对象名，见 parts 字段）")
    if axis not in _AXES:
        raise ValueError(f"axis 必须是 x/y/z（得到 {axis!r}）")
    if (not isinstance(angle, (int, float)) or isinstance(angle, bool)
            or not math.isfinite(angle) or angle == 0 or not -360 < angle < 360):
        raise ValueError(f"angle 必须是 (-360, 360) 内非零角度（得到 {angle!r}）")

    def _apply(obj, FreeCAD):
        bb = obj.Shape.BoundBox
        center = FreeCAD.Vector((bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2,
                                (bb.ZMin + bb.ZMax) / 2)
        rot = FreeCAD.Rotation(FreeCAD.Vector(*_AXES[axis]), float(angle))
        # 绕 center 旋转 = Placement(零平移, rot, center) 左乘（Task 0 spike 验证写法）
        obj.Placement = FreeCAD.Placement(FreeCAD.Vector(), rot, center).multiply(obj.Placement)

    return _reposition(session, name, _apply, "rotate")
