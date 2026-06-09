"""文本诊断反馈（D5 最低级，全客户端兼容）。对 FreeCAD Part.Shape 输出结构化几何诊断。"""
from __future__ import annotations

from typing import Any


def describe_shape(shape: Any) -> dict[str, Any]:
    bb = shape.BoundBox
    com = shape.CenterOfMass
    return {
        "valid": shape.isValid(),
        "volume": shape.Volume,
        "bbox": {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength},
        "center_of_mass": [com.x, com.y, com.z],
        "solid_count": len(shape.Solids),
        "shell_count": len(shape.Shells),
    }
