"""导出可制造文件：STEP + STL（从活动结果 Shape）。"""
from __future__ import annotations

import os
from typing import Any

from vibecad.engine.session import Session


def export_part(session: Session, output_dir: str, *, fmt: str = "both") -> dict[str, Any]:
    if fmt not in ("step", "stl", "both"):
        raise ValueError(f"fmt 必须是 step/stl/both（得到 {fmt!r}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    os.makedirs(output_dir, exist_ok=True)
    step_path = stl_path = None
    with silence_fd1():
        shape = session.get_result_shape()
        doc_name = session.doc.Name
        if fmt in ("step", "both"):
            step_path = os.path.join(output_dir, f"{doc_name}.step")
            shape.exportStep(step_path)
        if fmt in ("stl", "both"):
            stl_path = os.path.join(output_dir, f"{doc_name}.stl")
            shape.exportStl(stl_path)
    return {"ok": True, "step": step_path, "stl": stl_path}
