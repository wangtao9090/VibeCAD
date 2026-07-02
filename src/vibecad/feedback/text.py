"""文本诊断反馈（D5 最低级，全客户端兼容）。对 FreeCAD Part.Shape 输出结构化几何诊断。"""
from __future__ import annotations

from typing import Any

from vibecad.engine.session import Session


def describe_shape(shape: Any) -> dict[str, Any]:
    bb = shape.BoundBox
    return {
        "valid": shape.isValid(),
        "volume": shape.Volume,
        "bbox": {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength},
        "center_of_mass": _center_of_mass(shape),
        "solid_count": len(shape.Solids),
        "shell_count": len(shape.Shells),
    }


def describe_assembly(session: Session) -> dict[str, Any]:
    """装配摘要：per-part volume/bbox/placement + assembly_bbox + interference 清单。

    返回格式：
    {
      "parts": {
        "零件名": {"volume": float, "bbox": {x,y,z}, "placement": [x,y,z]},
        ...
      },
      "assembly_bbox": {"x": float, "y": float, "z": float},
      "interference": [{"parts": [a, b], "volume": float}, ...],
      "interference_skipped": bool  # 可比较零件对 < 2 时 True——检查未真正跑过
    }

    单零件模式（_parts 空）：返回 describe_shape(get_result_shape()) 原格式。
    """
    if not session._parts:
        from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
        with silence_fd1():
            return describe_shape(session.get_result_shape())

    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    from vibecad.tools.assembly import assert_no_interference  # noqa: PLC0415

    parts_info: dict[str, Any] = {}
    with silence_fd1():
        for name, info in session._parts.items():
            # 空零件显式注明（终审 M-2：此前 get_assembly_shape 对空零件先抛，
            # error 字段是永远送不到用户手里的死代码；现 get_assembly_shape
            # 跳过空零件，本 error 字段真送达）
            if not info["objects"]:
                parts_info[name] = {"error": f"零件 {name} 无几何"}
                continue
            try:
                shape = session.get_result_shape(name)
                bb = shape.BoundBox
                pl = info["container"].Placement
                parts_info[name] = {
                    "volume": shape.Volume,
                    "bbox": {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength},
                    "placement": [pl.Base.x, pl.Base.y, pl.Base.z],
                }
            except (RuntimeError, ValueError) as exc:
                parts_info[name] = {"error": str(exc)}

        # 全空装配时 get_assembly_shape 响亮抛错（server describe_part 结构化）
        assembly_shape = session.get_assembly_shape()
        abb = assembly_shape.BoundBox
        assembly_bbox = {"x": abb.XLength, "y": abb.YLength, "z": abb.ZLength}

    # 干涉清单（allow=True 放行，返回清单而非抛异常）
    interference = assert_no_interference(session, allow=True)

    return {
        "parts": parts_info,
        "assembly_bbox": assembly_bbox,
        "interference": interference,
        "interference_skipped": interference.interference_skipped,
    }


def _center_of_mass(shape: Any) -> list[float] | None:
    """Solid 直接有 CenterOfMass；布尔结果是 Part.Compound 无此属性，退到首个 Solid。"""
    com = getattr(shape, "CenterOfMass", None)
    if com is None:
        solids = getattr(shape, "Solids", None) or []
        if solids:
            com = solids[0].CenterOfMass
    return [com.x, com.y, com.z] if com is not None else None
