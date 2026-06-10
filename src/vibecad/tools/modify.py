# src/vibecad/tools/modify.py
"""参数修改工具（Round 6b）：改一个参数，FreeCAD 依赖链（布尔/孔/圆角）自动重算
——方案 B 选 FreeCAD 的核心红利。纪律：校验 → 事务 → 设参 → recompute →
回读确认生效 → 几何断言 → 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session

# TypeId → {对外参数名(小写): FreeCAD 属性名}；None 表示值藏在 Edges 元组（Fillet/Chamfer）
_WHITELIST: dict[str, dict[str, str | None]] = {
    "Part::Box": {"length": "Length", "width": "Width", "height": "Height"},
    "Part::Cylinder": {"radius": "Radius", "height": "Height"},
    "Part::Fillet": {"radius": None},
    "Part::Chamfer": {"size": None},
}


def list_parameters(doc: Any) -> dict[str, dict[str, float]]:
    """文档对象 → 白名单参数当前值（附进每步 result 的 parts 字段，给 AI 读）。"""
    out: dict[str, dict[str, float]] = {}
    for obj in getattr(doc, "Objects", []):
        wl = _WHITELIST.get(getattr(obj, "TypeId", ""))
        if not wl:
            continue
        params: dict[str, float] = {}
        for key, attr in wl.items():
            if attr is None:
                edges = getattr(obj, "Edges", [])
                if edges:
                    params[key] = float(edges[0][1])
            else:
                params[key] = float(getattr(obj, attr))
        if params:
            out[obj.Name] = params
    return out


def modify_part(session: Session, name: str, parameter: str, value: float) -> dict[str, Any]:
    """修改对象的白名单参数并重算依赖链。"""
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串（对象名，见返回的 parts 字段）")
    if not parameter or not isinstance(parameter, str):
        raise ValueError("parameter 必须是非空字符串（可改参数见 parts 字段）")
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError(f"value 必须是 > 0 的有限数字（得到 {value!r}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("modify_part"):
        with silence_fd1():
            try:
                obj = session.get_object(name)
            except KeyError as exc:
                names = [o.Name for o in session.doc.Objects
                         if getattr(o, "TypeId", "") in _WHITELIST]
                raise ValueError(
                    f"对象 {name!r} 不存在——文档现有可改对象：{names or '（无）'}") from exc
            wl = _WHITELIST.get(getattr(obj, "TypeId", ""))
            if wl is None:
                names = [o.Name for o in session.doc.Objects
                         if getattr(o, "TypeId", "") in _WHITELIST]
                raise ValueError(
                    f"对象 {name!r}（{getattr(obj, 'TypeId', '?')}）不支持参数修改"
                    f"——可修改对象：{names or '（无）'}")
            key = parameter.lower()
            if key not in wl:
                raise ValueError(
                    f"对象 {name!r} 没有可改参数 {parameter!r}——可改：{sorted(wl)}")
            attr = wl[key]
            if attr is None:
                old = float(obj.Edges[0][1])
            else:
                old = float(getattr(obj, attr))
            if abs(old - value) < 1e-12:
                raise ValueError(f"参数 {key} 已是 {value:g}，无需修改")
            result_before = session.get_result_shape().Volume
            if attr is None:
                obj.Edges = [(idx, float(value), float(value))
                             for (idx, _r1, _r2) in obj.Edges]
            else:
                setattr(obj, attr, float(value))
            session.doc.recompute()
            # 回读确认参数生效（recompute 返回值不可信，几何断言才可信）
            now = float(obj.Edges[0][1]) if attr is None else float(getattr(obj, attr))
            if abs(now - value) > 1e-9:
                raise RuntimeError(
                    f"几何断言失败：参数 {key} 设为 {value:g} 后回读为 {now:g}")
            shape = session.get_result_shape()
            session.assert_valid_solid(shape)
            if abs(shape.Volume - result_before) < 1e-9:
                raise RuntimeError(
                    f"几何断言失败：参数 {key} {old:g}→{value:g} 后结果体积无变化"
                    "——下游依赖链可能未重算")
            result = {"ok": True,
                      "modified": {"name": obj.Name, "parameter": key,
                                   "from": old, "to": float(value)},
                      "volume": shape.Volume,
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
    return result
