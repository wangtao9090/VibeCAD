# src/vibecad/tools/modify.py
"""参数修改工具（Round 6b）：改一个参数，FreeCAD 依赖链（布尔/孔/圆角）自动重算
——方案 B 选 FreeCAD 的核心红利。纪律：校验 → 事务 → 设参 → recompute →
回读确认生效 → 几何断言 → 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session
from vibecad.tools._integrity import (
    assert_holes_intact,
    assert_no_sealed_holes,
    assert_not_touched,
    assert_result_not_drifted,
    assert_single_solid,
    cut_tool_radii,
    hole_count_snapshot,
)

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


def _assert_shape_reflects(obj: Any, key: str, value: float) -> None:
    """体积不变放行前的 Shape 级几何回读（复审 D 的纵深防御层）。

    真机取证注意：primitive 的 Shape 在 setattr 后【即时重建】（recompute 失灵时
    box.Shape.BoundBox.XLength 也已是新值），所以本回读对 primitive 抓不住
    "链未重算"——那由 modify_part 中的 State 账本断言负责（主判据）。本函数保留
    用于抓"参数写入但 Shape 重建出旧几何/空几何"的对象级异常（纵深防御）。

    判据选择（以稳为准）：
    - Part::Box：BoundBox 三边。本项目 add_box 只平移不旋转，BoundBox 轴对齐可靠；
      若未来引入旋转放置，此判据需换成局部坐标。
    - Part::Cylinder：遍历 Shape 找圆柱面。radius 比 Surface.Radius（旋转不变）；
      height 用圆柱面 ParameterRange 的 v 跨度（参数域是局部的，v 轴向跨度=高度，
      真机已验证旋转 137° 后 v-span 仍精确等于 Height）——BoundBox 对 add_hole
      沿面法向旋转放置的刀具轴向不可靠，不采用。
    """
    type_id = getattr(obj, "TypeId", "")
    actual: float | None = None
    if type_id == "Part::Box":
        bb = obj.Shape.BoundBox
        actual = {"length": bb.XLength, "width": bb.YLength, "height": bb.ZLength}[key]
    elif type_id == "Part::Cylinder":
        cyl_faces = [f for f in obj.Shape.Faces
                     if type(f.Surface).__name__ == "Cylinder"]
        if not cyl_faces:
            raise RuntimeError(
                "几何断言失败：参数已写入但圆柱对象的 Shape 中找不到圆柱面"
                "——依赖链可能未重算")
        if key == "radius":
            actual = float(cyl_faces[0].Surface.Radius)
        else:  # height
            pr = cyl_faces[0].ParameterRange
            actual = abs(float(pr[3]) - float(pr[2]))
    if actual is not None and abs(actual - value) > 1e-6:
        raise RuntimeError(
            f"几何断言失败：参数已写入但形状几何未更新（{key}={value:g} vs "
            f"实测 {actual:g}）——依赖链可能未重算")


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
            shape_before = result_obj_before.Shape
            result_before = shape_before.Volume
            # 孔完整性快照（审查 E1-E4：改参把孔变开槽/移出零件/把件切两半全被
            # ok:True 误报；复审 B：半圆槽误拒）。
            # 基线 = 改前 shape 中每个刀具半径的【实际】完整圆柱面数——不能用
            # "每径完整面数 >= 刀具数"的虚构不变量：boolean_cut（合法注册工具）的
            # 圆柱刀具半嵌边缘开半圆槽时，完整面从创建起就是 0，虚构不变量会让含
            # 半圆槽的文档上任何 modify_part 全量误拒（且错误消息指向不存在的孔）。
            # 桶迁移（改的就是孔自身半径，如 HoleTool radius 4→5）：旧半径桶 -1
            # （下限 0）、新半径桶 +1——但仅当旧桶 >0（该刀具旧半径实际贡献过完整
            # 面）才迁移：半嵌槽刀具改径不应凭空期望新半径出现完整面。
            # 推演：同径双刀改一把 {4:2} → {4:1, 5:1}；半圆槽基线 {5:0} 改 width
            # 不误拒；E1 缺口基线 {4:1} 改后实际 0 < 1 照拒。
            # 留档（复审 E 场景，Part::Fuse 同径凸台；当前无 fuse 工具不可达）：
            # 实际计数基线已把同径凸台侧面计入基线（真机取证：基线 {4:2}，孔变
            # 缺口降到 1 被抓——简单顶包已不可行），但"计数补偿"型顶包理论上仍
            # 可漏（一次修改同时让孔面消失、又让某残缺凸台面恢复完整，总数不变）。
            # Fuse 落地时本判据需升级为按刀具轴线匹配完整面，而非按半径全局计数。
            #
            # 用 cut_tool_radii / hole_count_snapshot 取快照（_integrity 共享逻辑），
            # 桶迁移（modify 特有：改孔刀具自身半径时迁移 expected_counts）仍在此处。
            all_radii = cut_tool_radii(session.doc)
            modified_tool_rk: float | None = None
            for o in session.doc.Objects:
                if getattr(o, "TypeId", "") != "Part::Cut":
                    continue
                tool = getattr(o, "Tool", None)
                if tool is None or getattr(tool, "TypeId", "") != "Part::Cylinder":
                    continue
                if tool.Name == obj.Name and key == "radius":
                    modified_tool_rk = round(float(tool.Radius), 6)
                    break
            expected_counts: dict[float, int] = hole_count_snapshot(shape_before, all_radii)
            if modified_tool_rk is not None \
                    and expected_counts.get(modified_tool_rk, 0) > 0:
                expected_counts[modified_tool_rk] -= 1
                new_rk = round(float(value), 6)
                expected_counts[new_rk] = expected_counts.get(new_rk, 0) + 1
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
            # 依赖链执行账本断言（复审 D：recompute 失灵时全部几何断言凭旧几何
            # 通过 + 体积不变放行 = 静默失败粉饰成 ok+note）。真机取证：primitive
            # 的 Shape 在 setattr 后【即时重建】（box.Length=45 后未 recompute
            # box.Shape.BoundBox.XLength 已是 45），被改对象的 Shape 级几何回读
            # 对 primitive 恒过、无判别力——FreeCAD 自己的 touched 账本才是
            # "链已执行"的可信信号：setattr/Edges 重写后对象置 Touched（Box/
            # Cylinder/Fillet 真机均已验证），正常 recompute 后必为 Up-to-date，
            # 保持 Touched 即 recompute 未执行或未覆盖该对象。
            assert_not_touched(obj, f"参数 {key}")
            # 结果对象漂移断言（审查 E5 CRITICAL：刀具吞件 → Cut 体积归 0 →
            # get_result_object fallback 漂移到刀具圆柱 → 谎报刀具体积污染会话）
            assert_result_not_drifted(session, before_name)
            result_obj = session.get_result_object()
            shape = result_obj.Shape
            session.assert_valid_solid(shape)
            # 单实体断言（审查 E4：⌀32 刀具横穿 30 宽零件把件切成两半仍 ok:True）
            assert_single_solid(shape, "参数修改")
            # 孔完整性断言（快照推演见上）
            assert_holes_intact(shape, expected_counts)
            # R7 终验移交项热修：改基体尺寸（如 height 加大）可把既有盲孔埋成
            # 密封内腔（孔完整面仍在、计数不变）——与 reposition 封孔同族，接同一探针
            assert_no_sealed_holes(session.doc, shape)
            result = {"ok": True,
                      "modified": {"name": obj.Name, "parameter": key,
                                   "from": old, "to": float(value)},
                      "volume": shape.Volume,
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
            # 体积不变是合法情形（审查 E6：通孔在零件外的部分加长）——但放行前
            # 必须做被改对象的 Shape 级几何回读（复审 D：recompute 失灵时属性回读
            # /漂移/孔完整性断言全部凭旧几何通过，放行+note 会粉饰静默失败）
            if abs(shape.Volume - result_before) < 1e-9:
                if attr is None:
                    # Fillet/Chamfer：值藏在 Edges 元组，Shape 级回读需逐边匹配
                    # 圆角面半径（复杂）。但圆角/倒角半径改变必然改变体积（移除/
                    # 添加量随半径单调），recompute 正常时它们到不了本分支——
                    # 能走到这里 = 链未重算，直接响亮拒绝（跳过回读会让假设性
                    # recompute 失灵场景被 note 粉饰）。
                    raise RuntimeError(
                        f"几何断言失败：{key} 修改后体积无变化——圆角/倒角半径"
                        "改变必然改变体积，依赖链可能未重算")
                _assert_shape_reflects(obj, key, float(value))
                result["note"] = "该修改未改变最终几何（参数已生效，例如通孔在零件外的部分加长）"
    return result
