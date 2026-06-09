"""文本诊断反馈（D5 最低级，全客户端兼容）。对 FreeCAD Part.Shape 输出结构化几何诊断。"""
from __future__ import annotations

from typing import Any


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


def _center_of_mass(shape: Any) -> list[float] | None:
    """Solid 直接有 CenterOfMass；布尔结果是 Part.Compound 无此属性，退到首个 Solid。"""
    com = getattr(shape, "CenterOfMass", None)
    if com is None:
        solids = getattr(shape, "Solids", None) or []
        if solids:
            com = solids[0].CenterOfMass
    return [com.x, com.y, com.z] if com is not None else None
