"""导出可制造文件：STEP + STL（从活动结果 Shape）。Round 8：装配 shape + split per-part。"""
from __future__ import annotations

import os
from typing import Any

from vibecad.engine.session import Session


def _assert_written(path: str) -> None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"导出文件未生成或为空：{path}")


def export_part(session: Session, output_dir: str, *, fmt: str = "both",
                split: bool = False) -> dict[str, Any]:
    """导出当前结果为 STEP/STL/glTF。

    装配适配（Round 8）：
    - 默认吃 get_assembly_shape()（单零件模式与旧版完全等价：compound=单 solid shape）。
    - split=True 且多零件时：per-part 导出 STEP（<doc>_<零件名>.step），
      每文件 _assert_written 验证写入成功；返回的 "step" 字段变为文件路径列表。
      fmt 不含 step 时 split=True 被静默忽略（无需报错）。

    split=False（默认）：行为与 R7 完全一致（装配导出全 compound STEP）。
    """
    if fmt not in ("step", "stl", "gltf", "both", "all"):
        raise ValueError(f"fmt 必须是 step/stl/gltf/both/all（得到 {fmt!r}）")
    skipped_parts: list[str] = []
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    os.makedirs(output_dir, exist_ok=True)

    step_path: str | list[str] | None = None
    stl_path = None
    gltf_shape = None  # 延迟到 silence_fd1 外（gltf 模块自己 import FreeCAD）

    with silence_fd1():
        # Round 8：吃装配 shape（单零件模式等价）
        assembly_shape = session.get_assembly_shape()
        doc_name = session.doc.Name

        if fmt in ("step", "both", "all"):
            # split=True 且多零件：per-part 导出
            if split and session._parts:
                step_paths: list[str] = []
                skipped_parts.extend(n for n in session._parts
                                     if not session._parts[n]["objects"])
                for part_name in session._parts:
                    if not session._parts[part_name]["objects"]:
                        continue  # 空零件无几何可导，记入 skipped 字段
                    part_shape = session.get_result_shape(part_name).transformed(
                        session._parts[part_name]["container"].Placement.toMatrix())
                    # 文件名：<doc>_<零件名>.step（零件名中的空格/斜线替换为下划线）
                    safe_name = part_name.replace(" ", "_").replace("/", "_")
                    path = os.path.join(output_dir, f"{doc_name}_{safe_name}.step")
                    part_shape.exportStep(path)
                    _assert_written(path)
                    step_paths.append(path)
                step_path = step_paths
            else:
                step_path = os.path.join(output_dir, f"{doc_name}.step")
                assembly_shape.exportStep(step_path)
                _assert_written(step_path)

        if fmt in ("stl", "both", "all"):
            stl_path = os.path.join(output_dir, f"{doc_name}.stl")
            assembly_shape.exportStl(stl_path)
            _assert_written(stl_path)

        gltf_shape = assembly_shape  # 延迟 gltf，保持 silence_fd1 闭合后处理

    gltf_path = None
    if fmt in ("gltf", "all"):
        from vibecad.feedback import gltf as _gltf  # noqa: PLC0415
        gltf_path = os.path.join(output_dir, f"{doc_name}.glb")
        _gltf.export_gltf(gltf_shape, gltf_path, doc_name=doc_name)
        _assert_written(gltf_path)

    result = {"ok": True, "step": step_path, "stl": stl_path, "gltf": gltf_path}
    if skipped_parts:
        result["skipped_parts"] = skipped_parts  # 空零件无几何未导出（与守卫跳过语义一致）
    return result
