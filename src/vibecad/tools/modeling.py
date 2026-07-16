"""Walking Skeleton 语义建模工具（D2）：参数化 Part 图元 + 布尔。
每工具 = 参数校验 → 事务 → 参数化对象 → recompute → 几何断言 → 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session
from vibecad.tools._integrity import assert_solid_integrity

_AXIS = {"z": ((0.0, 0.0, 1.0), 0.0), "x": ((0.0, 1.0, 0.0), 90.0), "y": ((1.0, 0.0, 0.0), -90.0)}


def _validate_position(position) -> None:
    if (not isinstance(position, (list, tuple)) or len(position) != 3
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
                       for c in position)):
        raise ValueError(f"position 必须是 3 个有限数字 (x, y, z)（得到 {position!r}）")


def _stale_hint(session: Session) -> dict[str, Any]:
    """几何变更使既有标注标签过期——成功 result 必须提示重标注（终审 Important-3）。"""
    if getattr(session, "_labels", None):
        return {"labels_stale": True,
                "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
    return {}


def new_document(
    session: Session, name: str, *, discard_unsaved: bool = False,
) -> dict[str, Any]:
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串")
    if not isinstance(discard_unsaved, bool):
        raise ValueError("discard_unsaved 必须是 bool")
    if session.doc is not None and session.is_dirty() and not discard_unsaved:
        raise ValueError(
            "当前项目有未保存修改；请先调用 save_project，或明确传 "
            "discard_unsaved=true 后再新建")
    doc = session.open_document(name)
    return {"ok": True, "name": doc.Name}


def add_box(
    session: Session, length: float, width: float, height: float,
    position=(0.0, 0.0, 0.0),
) -> dict[str, Any]:
    for field, value in (("length", length), ("width", width), ("height", height)):
        if value <= 0:
            raise ValueError(f"{field} 必须 > 0（得到 {value}）")
    _validate_position(position)
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("add_box"):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            obj = session.doc.addObject("Part::Box", "Box")
            obj.Length, obj.Width, obj.Height = length, width, height
            obj.Placement = FreeCAD.Placement(FreeCAD.Vector(*position), FreeCAD.Rotation())
            session.doc.recompute()
            session.assert_valid_solid(obj.Shape)
            session.set_result_object(obj)
            result = {
                "ok": True, "name": obj.Name,
                "volume": obj.Shape.Volume, "position": list(position),
                **_stale_hint(session),
            }
    return result


def add_cylinder(
    session: Session, radius: float, height: float,
    position=(0.0, 0.0, 0.0), axis="z",
) -> dict[str, Any]:
    for field, value in (("radius", radius), ("height", height)):
        if value <= 0:
            raise ValueError(f"{field} 必须 > 0（得到 {value}）")
    _validate_position(position)
    if axis not in _AXIS:
        raise ValueError(f"axis 必须是 x/y/z（得到 {axis!r}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("add_cylinder"):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            obj = session.doc.addObject("Part::Cylinder", "Cylinder")
            obj.Radius, obj.Height = radius, height
            rot_axis, angle = _AXIS[axis]
            obj.Placement = FreeCAD.Placement(
                FreeCAD.Vector(*position), FreeCAD.Rotation(FreeCAD.Vector(*rot_axis), angle))
            session.doc.recompute()
            session.assert_valid_solid(obj.Shape)
            session.set_result_object(obj)
            result = {"ok": True, "name": obj.Name, "volume": obj.Shape.Volume,
                      "position": list(position), "axis": axis, **_stale_hint(session)}
    return result


def _validate_boolean_names(base_name: str, tool_name: str) -> None:
    if not base_name or not isinstance(base_name, str):
        raise ValueError("base_name 必须是非空字符串")
    if not tool_name or not isinstance(tool_name, str):
        raise ValueError("tool_name 必须是非空字符串")
    if base_name == tool_name:
        raise ValueError("base_name 与 tool_name 不能是同一对象")


def _solid_object(session: Session, name: str) -> Any:
    try:
        obj = session.get_object(name)
    except KeyError as exc:
        raise ValueError(f"对象 {name!r} 不存在") from exc
    if not hasattr(obj, "Shape"):
        raise ValueError(f"对象 {name!r} 没有可参与布尔运算的 Shape")
    session.assert_valid_solid(obj.Shape)
    return obj


def _boolean_owner(session: Session, base_name: str, tool_name: str) -> str | None:
    """布尔结果必须留在两个 operand 的共同 owner；不跨装配零件偷做局部运算。"""
    if not session._parts:
        return None
    base_owner = session.owner_of(base_name)
    tool_owner = session.owner_of(tool_name)
    if base_owner is None or tool_owner is None:
        raise RuntimeError("布尔对象未归属任何零件——项目状态异常，请重新打开项目")
    if base_owner != tool_owner:
        raise ValueError(
            f"布尔运算要求两个对象属于同一零件（{base_name!r} 属于 {base_owner!r}，"
            f"{tool_name!r} 属于 {tool_owner!r}）")
    return base_owner


def boolean_cut(session: Session, base_name: str, tool_name: str) -> dict[str, Any]:
    _validate_boolean_names(base_name, tool_name)
    owner = _boolean_owner(session, base_name, tool_name)
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("boolean_cut", part=owner):
        with silence_fd1():
            base = _solid_object(session, base_name)
            tool = _solid_object(session, tool_name)
            session.doc.recompute()  # 防守：确保 Base/Tool 已算
            base_vol = base.Shape.Volume
            cut = session.doc.addObject("Part::Cut", "Cut")
            cut.Base = base
            cut.Tool = tool
            session.doc.recompute()
            session.assert_valid_solid(cut.Shape)
            assert_solid_integrity(session, cut.Shape, "boolean_cut", part=owner)
            if cut.Shape.Volume >= base_vol - 1e-6:
                raise RuntimeError(
                    f"几何断言失败：布尔差集未移除任何材料（base={base_vol:.3f}, "
                    f"cut={cut.Shape.Volume:.3f}）——tool '{tool_name}' 可能因 position/axis "
                    f"未与 base '{base_name}' 相交"
                )
            session.set_result_object(cut, part=owner)
            result = {"ok": True, "name": cut.Name, "volume": cut.Shape.Volume,
                      **_stale_hint(session)}
    return result


def _boolean_combine(
    session: Session, base_name: str, tool_name: str, *, operation: str,
) -> dict[str, Any]:
    _validate_boolean_names(base_name, tool_name)
    owner = _boolean_owner(session, base_name, tool_name)
    if operation not in ("fuse", "common"):
        raise ValueError(f"未知布尔运算 {operation!r}")
    type_name = "Part::Fuse" if operation == "fuse" else "Part::Common"
    label = "Fuse" if operation == "fuse" else "Common"
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction(f"boolean_{operation}", part=owner):
        with silence_fd1():
            base = _solid_object(session, base_name)
            tool = _solid_object(session, tool_name)
            session.doc.recompute()
            base_vol, tool_vol = float(base.Shape.Volume), float(tool.Shape.Volume)
            obj = session.doc.addObject(type_name, label)
            obj.Base, obj.Tool = base, tool
            session.doc.recompute()
            session.assert_valid_solid(obj.Shape)
            assert_solid_integrity(session, obj.Shape, f"boolean_{operation}", part=owner)
            volume = float(obj.Shape.Volume)
            tol = max(base_vol, tool_vol, 1.0) * 1e-7
            if operation == "fuse":
                if volume < max(base_vol, tool_vol) - tol:
                    raise RuntimeError(
                        "几何断言失败：并集体积小于较大输入体积，运算可能丢失材料"
                        f"（{volume:.3f} < {max(base_vol, tool_vol):.3f}）")
                if volume > base_vol + tool_vol + tol:
                    raise RuntimeError(
                        "几何断言失败：并集体积大于两个输入体积之和"
                        f"（{volume:.3f} > {base_vol + tool_vol:.3f}）")
            elif volume > min(base_vol, tool_vol) + tol:
                raise RuntimeError(
                    "几何断言失败：交集体积大于较小输入体积"
                    f"（{volume:.3f} > {min(base_vol, tool_vol):.3f}）")
            session.set_result_object(obj, part=owner)
            result = {
                "ok": True,
                "name": obj.Name,
                "operation": operation,
                "base": base_name,
                "tool": tool_name,
                "volume": volume,
                **_stale_hint(session),
            }
    return result


def boolean_fuse(session: Session, base_name: str, tool_name: str) -> dict[str, Any]:
    """合并两个相交或相接的同零件 solid；断开多实体会被完整性守卫拒绝。"""
    return _boolean_combine(session, base_name, tool_name, operation="fuse")


def boolean_common(session: Session, base_name: str, tool_name: str) -> dict[str, Any]:
    """取两个同零件 solid 的实体交集；无体积交集会被几何断言拒绝。"""
    return _boolean_combine(session, base_name, tool_name, operation="common")
