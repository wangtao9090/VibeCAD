"""Walking Skeleton 语义建模工具（D2）：参数化 Part 图元 + 布尔。
每工具 = 参数校验 → 事务 → 参数化对象 → recompute → 几何断言 → 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session

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


def new_document(session: Session, name: str) -> dict[str, Any]:
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串")
    session.open_document(name)
    return {"ok": True, "name": name}


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
            result = {"ok": True, "name": obj.Name, "volume": obj.Shape.Volume,
                      "position": list(position), "axis": axis, **_stale_hint(session)}
    return result


def boolean_cut(session: Session, base_name: str, tool_name: str) -> dict[str, Any]:
    if not base_name or not isinstance(base_name, str):
        raise ValueError("base_name 必须是非空字符串")
    if not tool_name or not isinstance(tool_name, str):
        raise ValueError("tool_name 必须是非空字符串")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("boolean_cut"):
        with silence_fd1():
            base = session.get_object(base_name)
            tool = session.get_object(tool_name)
            session.doc.recompute()  # 防守：确保 Base/Tool 已算
            base_vol = base.Shape.Volume
            cut = session.doc.addObject("Part::Cut", "Cut")
            cut.Base = base
            cut.Tool = tool
            session.doc.recompute()
            session.assert_valid_solid(cut.Shape)
            if cut.Shape.Volume >= base_vol - 1e-6:
                raise RuntimeError(
                    f"几何断言失败：布尔差集未移除任何材料（base={base_vol:.3f}, "
                    f"cut={cut.Shape.Volume:.3f}）——tool '{tool_name}' 可能因 position/axis "
                    f"未与 base '{base_name}' 相交"
                )
            result = {"ok": True, "name": cut.Name, "volume": cut.Shape.Volume,
                      **_stale_hint(session)}
    return result
