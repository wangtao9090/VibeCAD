"""Walking Skeleton 语义建模工具（D2）：参数化 Part 图元 + 布尔。
每工具 = 参数校验 → 事务 → 参数化对象 → recompute → 几何断言 → 结构化 dict。"""
from __future__ import annotations

from typing import Any

from vibecad.engine.session import Session


def new_document(session: Session, name: str) -> dict[str, Any]:
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串")
    session.open_document(name)
    return {"ok": True, "name": name}


def add_box(session: Session, length: float, width: float, height: float) -> dict[str, Any]:
    for field, value in (("length", length), ("width", width), ("height", height)):
        if value <= 0:
            raise ValueError(f"{field} 必须 > 0（得到 {value}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("add_box"):
        with silence_fd1():
            obj = session.doc.addObject("Part::Box", "Box")
            obj.Length, obj.Width, obj.Height = length, width, height
            session.doc.recompute()
            session.assert_valid_solid(obj.Shape)
            result = {"ok": True, "name": obj.Name, "volume": obj.Shape.Volume}
    return result


def add_cylinder(session: Session, radius: float, height: float) -> dict[str, Any]:
    for field, value in (("radius", radius), ("height", height)):
        if value <= 0:
            raise ValueError(f"{field} 必须 > 0（得到 {value}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("add_cylinder"):
        with silence_fd1():
            obj = session.doc.addObject("Part::Cylinder", "Cylinder")
            obj.Radius, obj.Height = radius, height
            session.doc.recompute()
            session.assert_valid_solid(obj.Shape)
            result = {"ok": True, "name": obj.Name, "volume": obj.Shape.Volume}
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
            cut = session.doc.addObject("Part::Cut", "Cut")
            cut.Base = base
            cut.Tool = tool
            session.doc.recompute()
            session.assert_valid_solid(cut.Shape)
            result = {"ok": True, "name": cut.Name, "volume": cut.Shape.Volume}
    return result
