# src/vibecad/tools/_integrity.py
"""特征完整性共享守卫（R7 抽取自 modify.py）：孔完整性快照/比对、结果对象漂移、
Touched 账本、单 solid、孔密封内腔探针。
消费方：modify（快照/比对/漂移/Touched/单solid）、transform（全套+密封探针）、
features.add_hole（快照/比对——跨径既有孔保护）、sketch（单solid/Touched/孔完整性
+ pad 密封探针）。
判据沿革见 R6b 计划实施记录（虚构不变量教训：基线必须取改前实际状态）。

Round 8 装配语义（终审修订：守卫锚定"被操作零件"而非"活动零件"）：
- assert_solid_integrity：统一入口，_parts 空走原 assert_single_solid；装配模式
  直接断言传入 shape（新几何承载者，差集归属尚未发生）+ 逐一回取其余零件
  （owner 跳过、空零件跳过）。
- assert_no_sealed_holes：装配模式必须传 owner_names（owner 零件的对象名集合）
  ——刀具 Placement 与零件 shape 同在零件局部坐标系，跨零件混用探针会把 B 的
  盲孔打在 A 的材料上误报（终审 C-E）。
- assert_result_not_drifted：part 透传到 get_result_object（终审 C-D 同源）。
- cut_tool_radii / hole_count_snapshot：基线 = owner shape 的实际计数，跨零件
  半径的基线天然为 0（无误报、不漏检自己的孔），radii 全文档扫描可接受。
- 单零件模式（_parts 空）行为零变化。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session


def cut_tool_radii(doc: Any) -> list[float]:
    """文档中 Part::Cut.Tool 圆柱半径列表（去重用 set，保留浮点精度 round 6 位）。"""
    radii: list[float] = []
    for o in getattr(doc, "Objects", []):
        if getattr(o, "TypeId", "") != "Part::Cut":
            continue
        tool = getattr(o, "Tool", None)
        if tool is None or getattr(tool, "TypeId", "") != "Part::Cylinder":
            continue
        radii.append(round(float(tool.Radius), 6))
    return radii


def hole_count_snapshot(shape: Any, radii: list[float]) -> dict[float, int]:
    """改前实际完整圆柱面计数基线（逐半径计数，不能用虚构不变量——见 R6b 留档）。

    基线 = 改前 shape 中每个刀具半径的【实际】完整圆柱面数——不能用
    "每径完整面数 >= 刀具数"的虚构不变量：boolean_cut（合法注册工具）的
    圆柱刀具半嵌边缘开半圆槽时，完整面从创建起就是 0，虚构不变量会让含
    半圆槽的文档上任何操作全量误拒（且错误消息指向不存在的孔）。
    """
    return {rk: _count_full_cylinder_faces(shape, rk) for rk in set(radii)}


def _count_full_cylinder_faces(shape: Any, radius: float) -> int:
    """数半径匹配（1e-6）且 u 参数跨满 2π（容差 1e-3）的圆柱面——完整圆孔的成形判据。
    增量判据（cut 前后各数一次，after >= before+1）：存在性判据会被同径旧孔放行
    新孔的越界缺口（安装孔阵列是常见操作，终审 CRITICAL-3）。"""
    n = 0
    for f in shape.Faces:
        s = f.Surface
        if type(s).__name__ != "Cylinder" or abs(s.Radius - radius) > 1e-6:
            continue
        u0, u1 = f.ParameterRange[0], f.ParameterRange[1]
        if abs((u1 - u0) - 2 * math.pi) < 1e-3:
            n += 1
    return n


def assert_holes_intact(shape: Any, expected: dict[float, int]) -> None:
    """逐半径断言完整圆柱面数 >= 期望基线，否则响亮拒绝（RuntimeError）。

    使用 >= 而非 == 是因为 modify_part 可能调整桶（桶迁移逻辑在调用处维护）；
    transform/pattern 时调用处不做桶迁移，直接用原始基线。
    """
    for rk, n_expect in expected.items():
        if _count_full_cylinder_faces(shape, rk) < n_expect:
            raise RuntimeError(
                f"几何断言失败：操作破坏了 ⌀{2 * rk:g} 孔的完整性"
                "（孔可能变成开口缺口或被移出零件）"
                "——请检查新参数与孔位的关系")


def assert_single_solid(shape: Any, context: str) -> None:
    """断言形状只有 1 个 solid，否则响亮拒绝（RuntimeError）。

    单实体断言（审查 E4：⌀32 刀具横穿 30 宽零件把件切成两半仍 ok:True）。
    单零件模式直接调此函数；装配模式请用 assert_solid_integrity。
    """
    n_solids = len(shape.Solids)
    if n_solids != 1:
        raise RuntimeError(
            f"几何断言失败：{context} 把零件切成 {n_solids} 块"
            "——新操作可能让孔/特征越过零件边缘")


def assert_solid_integrity(session: Session, shape: Any, context: str,
                           part: str | None = None) -> None:
    """统一入口：_parts 空（单零件模式）调 assert_single_solid(shape, context)。

    装配模式（_parts 非空，终审 C-A 修复语义）：
    - **传入的 shape 必须直接断言**——它承载操作的新几何结果，而差集归属发生在
      事务收尾（_claim_new_objects），断言时新对象尚未进 owner 的 objects 集合，
      经 get_result_shape(owner) 回取拿到的是旧结果（飞地 pad 切成 2 块漏检）、
      对空零件甚至直接崩（"零件 X 中无有效 solid"——装配模式无基体 pad 不可用）。
    - part = 承载 shape 的零件名（owner）；None 默认活动零件（sketch 等
      活动零件操作）。其余零件逐一回取断言；objects 为空的零件（new_part 后
      尚未建几何）跳过——空零件没有完整性可言，不应让别的零件操作误拒。
    """
    if not session._parts:
        assert_single_solid(shape, context)
        return
    owner = part if part is not None else session._active_part
    n_solids = len(shape.Solids)
    if n_solids != 1:
        raise RuntimeError(
            f"几何断言失败：{context} 把零件 {owner!r} 切成 {n_solids} 块"
            "——新操作可能让孔/特征越过零件边缘")
    for part_name, info in session._parts.items():
        if part_name == owner or not info["objects"]:
            continue
        p_shape = session.get_result_shape(part_name)
        n = len(p_shape.Solids)
        if n != 1:
            raise RuntimeError(
                f"几何断言失败：{context} 把零件 {part_name!r} 切成 {n} 块"
                "——新操作可能让孔/特征越过零件边缘")


def assert_not_touched(obj: Any, parameter_desc: str) -> None:
    """断言对象不处于 Touched 状态，否则响亮拒绝（RuntimeError）。

    依赖链执行账本断言（复审 D：recompute 失灵时全部几何断言凭旧几何
    通过 + 体积不变放行 = 静默失败粉饰成 ok+note）。真机取证：primitive
    的 Shape 在 setattr 后【即时重建】（box.Length=45 后未 recompute
    box.Shape.BoundBox.XLength 已是 45），被改对象的 Shape 级几何回读
    对 primitive 恒过、无判别力——FreeCAD 自己的 touched 账本才是
    "链已执行"的可信信号：setattr/Edges 重写后对象置 Touched（Box/
    Cylinder/Fillet 真机均已验证），正常 recompute 后必为 Up-to-date，
    保持 Touched 即 recompute 未执行或未覆盖该对象。
    """
    if "Touched" in getattr(obj, "State", []):
        raise RuntimeError(
            f"几何断言失败：{parameter_desc} 后对象 {obj.Name} 仍处 "
            "Touched 状态——依赖链可能未重算（recompute 未执行或失败）")


def assert_no_sealed_holes(doc: Any, shape: Any,
                           owner_names: set[str] | None = None) -> None:
    """孔端面探针：检测孔是否被操作完全封闭成内腔（不可加工）→ 响亮拒绝。

    对每个 Part::Cut 的圆柱 Tool：取其轴线两端点各向外延 0.6mm（> 钻入
    lift 0.5，确保正常通孔的探针落在零件外）的探针点，solid.isInside 检测：
    - 两端探针都在材料内 = 孔口被完全封死、孔变成密封内腔 → RuntimeError；
    - 一端在材料内（通孔变盲孔）→ 放行（探针无法低成本区分"本来就是盲孔"
      与"通孔被封一端"，且盲孔本身合法——该盲区由调用方 docstring 注明）。

    owner_names（装配模式必传，终审 C-E）：只遍历 Name 在该集合内的 Part::Cut
    ——刀具 Placement 与零件 shape 同在零件局部坐标系，跨零件混用会把 B 零件
    盲孔的探针点打在 A 零件的材料上（B 容器摆远后局部坐标恰落 A 体内）误报。
    None = 单零件模式，全文档遍历（R7 行为不变）。
    消费方：transform._reposition（移动基体/刀具封死孔口）、modify_part（改基体
    尺寸埋孔）、sketch.extrude_profile pad 路径（pad 盖住孔口同属密封腔家族）。
    """
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with silence_fd1():
        import FreeCAD  # noqa: PLC0415
    solid = shape.Solids[0] if getattr(shape, "Solids", None) else shape
    for o in getattr(doc, "Objects", []):
        if getattr(o, "TypeId", "") != "Part::Cut":
            continue
        if owner_names is not None and o.Name not in owner_names:
            continue  # 其它零件的孔与本 shape 不同坐标系，探针无意义（见 docstring）
        tool = getattr(o, "Tool", None)
        if tool is None or getattr(tool, "TypeId", "") != "Part::Cylinder":
            continue
        pl = tool.Placement
        axis = pl.Rotation.multVec(FreeCAD.Vector(0, 0, 1))  # 圆柱局部 +Z = 钻入方向
        h = float(tool.Height)
        p_entry = pl.Base - axis * 0.6              # 钻入端外延（正常时在零件外）
        p_bottom = pl.Base + axis * (h + 0.6)       # 钻底端外延（通孔时在零件外）
        if solid.isInside(p_entry, 1e-6, False) and solid.isInside(p_bottom, 1e-6, False):
            r = float(tool.Radius)
            raise RuntimeError(
                f"几何断言失败：操作使 ⌀{2 * r:g} 孔被完全封闭成内腔（不可加工）"
                "——请调整位置")


def assert_result_not_drifted(session: Session, before_name: str,
                              part: str | None = None) -> None:
    """断言结果对象名未改变，否则响亮拒绝（RuntimeError）。

    结果对象漂移断言（审查 E5 CRITICAL：刀具吞件 → Cut 体积归 0 →
    get_result_object fallback 漂移到刀具圆柱 → 谎报刀具体积污染会话）。
    part = 被操作对象所属零件（owner，终审 C-D）：装配模式必须在 owner 的
    对象集内查结果对象——按活动零件查会拿错零件的结果名导致假漂移/漏漂移。
    """
    result_obj = session.get_result_object(part)
    if result_obj.Name != before_name:
        raise RuntimeError(
            f"几何断言失败：操作导致结果对象从 {before_name} 漂移为 "
            f"{result_obj.Name}——操作可能吞掉了整个零件，请检查参数是否合理")
