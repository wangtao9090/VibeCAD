# src/vibecad/tools/modify.py
"""参数修改工具（Round 6b）：改一个参数，FreeCAD 依赖链（布尔/孔/圆角）自动重算
——方案 B 选 FreeCAD 的核心红利。纪律：校验 → 事务 → 设参 → recompute →
回读确认生效 → 几何断言 → 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session
from vibecad.tools.features import _count_full_cylinder_faces

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
            if abs(old - value) < 1e-9:
                raise ValueError(f"参数 {key} 已是 {value:g}，无需修改")
            result_obj_before = session.get_result_object()
            before_name = result_obj_before.Name
            result_before = result_obj_before.Shape.Volume
            # 孔完整性快照（审查 E1-E4：改参把孔变开槽/移出零件/把件切两半全被
            # ok:True 误报）。文档不变量：每把作为 Part::Cut.Tool 的圆柱刀具，结果
            # 形状中其半径的完整圆柱面数 >= 该半径刀具数（add_hole 的增量断言
            # _count_full_cylinder_faces >= before+1 在每次打孔时已建立此不变量）。
            # 修改参数后此不变量必须保持。
            # 推演（改的就是孔自身半径，如 HoleTool radius 4→5）：旧半径 4 的完整
            # 面计数会 -1 而新半径 5 的 +1 是【预期】行为——所以期望计数按
            # 【改后预期半径】构建：被改刀具按新值 value 计入、其余刀具按当前半径
            # 计入。同径双刀具改一把（4→5，另一把仍 4）自然得到 {5:1, 4:1}，逐
            # 半径断言互不干扰；按改前 shape 实际计数搬运反而会在该场景重复计数。
            expected_counts: dict[float, int] = {}
            for o in session.doc.Objects:
                if getattr(o, "TypeId", "") != "Part::Cut":
                    continue
                tool = getattr(o, "Tool", None)
                if tool is None or getattr(tool, "TypeId", "") != "Part::Cylinder":
                    continue
                r_exp = float(value) if (tool.Name == obj.Name and key == "radius") \
                    else float(tool.Radius)
                rk = round(r_exp, 6)
                expected_counts[rk] = expected_counts.get(rk, 0) + 1
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
            # 结果对象漂移断言（审查 E5 CRITICAL：刀具吞件 → Cut 体积归 0 →
            # get_result_object fallback 漂移到刀具圆柱 → 谎报刀具体积污染会话）
            result_obj = session.get_result_object()
            if result_obj.Name != before_name:
                raise RuntimeError(
                    f"几何断言失败：参数修改导致结果对象从 {before_name} 漂移为 "
                    f"{result_obj.Name}——修改可能吞掉了整个零件，请检查 value 是否过大")
            shape = result_obj.Shape
            session.assert_valid_solid(shape)
            # 单实体断言（审查 E4：⌀32 刀具横穿 30 宽零件把件切成两半仍 ok:True）
            n_solids = len(shape.Solids)
            if n_solids != 1:
                raise RuntimeError(
                    f"几何断言失败：参数修改把零件切成 {n_solids} 块"
                    "——新尺寸可能让孔/特征越过零件边缘")
            # 孔完整性断言（快照推演见上）
            for rk, n_expect in expected_counts.items():
                if _count_full_cylinder_faces(shape, rk) < n_expect:
                    raise RuntimeError(
                        f"几何断言失败：参数修改破坏了 ⌀{2 * rk:g} 孔的完整性"
                        "（孔可能变成开口缺口或被移出零件）"
                        "——请检查新尺寸与孔位的关系")
            result = {"ok": True,
                      "modified": {"name": obj.Name, "parameter": key,
                                   "from": old, "to": float(value)},
                      "volume": shape.Volume,
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
            # 体积不变是合法情形（审查 E6：通孔在零件外的部分加长）——参数回读
            # 断言+对象漂移断言+孔完整性断言已接管"链未重算"的兜底，不再误拒
            if abs(shape.Volume - result_before) < 1e-9:
                result["note"] = "该修改未改变最终几何（参数已生效，例如通孔在零件外的部分加长）"
    return result
