"""导出可制造文件：STEP + STL（从活动结果 Shape）。"""
from __future__ import annotations

import os
from typing import Any

from vibecad.engine.session import Session


def _assert_written(path: str) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"导出文件未生成或为空：{path}")


def export_part(session: Session, output_dir: str, *, fmt: str = "both") -> dict[str, Any]:
    if fmt not in ("step", "stl", "gltf", "both", "all"):
        raise ValueError(f"fmt 必须是 step/stl/gltf/both/all（得到 {fmt!r}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    os.makedirs(output_dir, exist_ok=True)
    step_path = stl_path = None
    with silence_fd1():
        shape = session.get_result_shape()
        doc_name = session.doc.Name
        if fmt in ("step", "both", "all"):
            step_path = os.path.join(output_dir, f"{doc_name}.step")
            shape.exportStep(step_path)
            _assert_written(step_path)
        if fmt in ("stl", "both", "all"):
            stl_path = os.path.join(output_dir, f"{doc_name}.stl")
            shape.exportStl(stl_path)
            _assert_written(stl_path)
    gltf_path = None
    if fmt in ("gltf", "all"):
        from vibecad.feedback import gltf as _gltf  # noqa: PLC0415
        gltf_path = os.path.join(output_dir, f"{doc_name}.glb")
        _gltf.export_gltf(shape, gltf_path, doc_name=doc_name)
        _assert_written(gltf_path)
    return {"ok": True, "step": step_path, "stl": stl_path, "gltf": gltf_path}
