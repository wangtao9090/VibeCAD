# VibeCAD Round 8 — 装配 DSL（架构轮）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（逐任务 实现→审查→修复）。Steps 用 `- [ ]` 勾选。

**Goal:** 多零件装配：new_part/活动零件（单零件用户零感知）、place_part 零件级位姿、align_parts 面贴面、干涉响亮断言、装配工程图与 STEP。

**Architecture:** Session 加零件注册表（`_parts` 空=R7 原行为完全不变——兼容性硬线"既有 227 快测零修改"）；承载形态 Task 0 spike 定（App::Part 容器 vs 平铺分组+链尾 Placement）；`tools/assembly.py` 承载新工具与干涉守卫；multiview/describe/export 吃 `get_assembly_shape()`（单零件时等价原 shape）。

**Tech Stack:** FreeCAD App::Part/Placement 数学、既有 _integrity 守卫体系、TechDraw HLR（吃 compound）。

**Spec:** `docs/superpowers/specs/2026-06-10-round8-assembly-design.md`

---

## File Structure

```
src/vibecad/engine/session.py    改   _parts/_active_part 注册表 + new_part/set_active_part +
                                      get_result_object(part=)/get_assembly_shape()（_parts 空走原路径）
src/vibecad/tools/assembly.py    新建 place_part / align_parts / assert_no_interference
src/vibecad/tools/_integrity.py  改   单 solid 断言装配语义（每零件单 solid）；孔守卫吃装配 shape
src/vibecad/feedback/multiview.py 改  iso 格按零件分色；正交格吃装配 compound
src/vibecad/feedback/text.py     改   describe 装配摘要
src/vibecad/tools/export.py      改   装配 STEP + split per-part
src/vibecad/tools/modify.py      改   list_parameters 按零件分组（parts 字段新形态）
src/vibecad/server.py            改   new_part/place_part/align_parts 工具；_attach_view 吃装配 shape
tests/test_session_parts.py     新建  注册表 fake 测试
tests/test_tools_assembly.py    新建  校验矩阵 + align 数学纯函数
tests/test_tools_features.py    改   @slow 6 条装配场景
.vibecad/spike_r8.py            新建  Task 0
.vibecad/blackbox_r8.py         新建  黑盒（控制者）
```

---

## Task 0：Spike——承载形态与 align 数学（真机）

**Files:** Create `.vibecad/spike_r8.py`

- [ ] **Step 1**: 真机验证四点（`.vibecad-test-runtime/mamba/envs/vibecad/bin/python`）：
  1. **App::Part 容器**：`doc.addObject("App::Part", "P1")` → `p1.addObject(box)`（box 入组）→ 组内 box+cylinder+Cut 链 recompute 正常？`p1.Placement = Placement(Vector(100,0,0), Rotation())` 后：`cut.Shape` 是否仍是局部坐标（预期是）；全局 shape 取法 `cut.Shape.transformed(p1.Placement.toMatrix())`（或 `getGlobalPlacement`）的正确性（BoundBox 平移 100 验证）。
  2. **TechDraw 投影吃 compound**：两个错位 box 的 `Part.makeCompound([s1, s2])` → `TechDraw.projectEx(compound, ...)` 出边正常（数量/坐标）。
  3. **干涉计算**：重叠 box 对 `s1.common(s2).Volume` 精确（重叠区体积）；不接触对 == 0。
  4. **align 数学**：手算场景——盖板（独立 box 60×40×5，任意初始位姿）底面贴底板顶面：`R = FreeCAD.Rotation(nm_vec, neg_nt_vec)`（最短弧把 moving 法向转到 -target 法向）；`T = anchor - R.multVec(moving_center)`；`Placement(T, R)` 应用后验证 moving 底面与 target 顶面共面（z 相等）且面心 XY 对齐。
- [x] **Step 2**: 结论回填本计划此处。

> **✅ Spike 结论（2026-06-10 真机一次通过）——选定 App::Part 容器方案**：
> 1. 容器内特征链（Box+Cyl+Cut）recompute 正常（valid, vol=11497.35）；`p.addObject(obj)` 入组。
> 2. 容器 `Placement` 移动后 `cut.Shape` **保持局部坐标**（XMin=0）；全局 shape 两种取法均可：`cut.Shape.transformed(p.Placement.toMatrix())`（XMin=100 ✓）与 `obj.getGlobalPlacement()`（Base.x=100 ✓）。get_assembly_shape 用 transformed 法合成 compound。
> 3. `TechDraw.projectEx` 吃 `Part.makeCompound` 正常（双 box 8 可见边，x 0..70）。
> 4. 干涉 `a.common(b).Volume` 精确（重叠 200/分离 0）。
> 5. align 数学：`FreeCAD.Rotation(nm, nt.negative())` 最短弧（倒扣 180° ✓）+ `T = anchor - R.multVec(moving_center)` 落点精确 (30,20,10)。
- [ ] **Step 3**: commit（计划结论回填）

---

## Task 1：Session 多零件注册表（兼容性硬线）

**Files:** Modify `src/vibecad/engine/session.py`; Create `tests/test_session_parts.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_session_parts.py
"""Session 多零件注册表：纯逻辑快测（fake，不碰 FreeCAD）。
铁律：_parts 为空（从未 new_part）时一切行为与 R7 完全一致。"""
import pytest

from vibecad.engine.session import Session


def test_single_part_mode_unchanged():
    s = Session()
    assert s.active_part is None          # 未进入多零件模式
    assert s.part_names() == []


def test_new_part_requires_document():
    s = Session()
    with pytest.raises(RuntimeError, match="无活动文档"):
        s.new_part("盖板")


def test_new_part_duplicate_rejected(monkeypatch):
    s = Session()
    monkeypatch.setattr(s, "_require_doc", lambda: None, raising=False)
    monkeypatch.setattr(s, "_register_part_container", lambda name: object(), raising=False)
    s._register_first_part_if_needed = lambda: None
    s.new_part("盖板")
    with pytest.raises(ValueError, match="已存在"):
        s.new_part("盖板")


def test_set_active_unknown_part():
    s = Session()
    with pytest.raises(ValueError, match="不存在"):
        s.set_active_part("幽灵")
```

（fake 钩子名以实现的自然切分为准微调——意图：注册表逻辑可无 FreeCAD 快测。）

- [ ] **Step 2: 实现**（按 Task 0 选型；以下为**平铺分组回退方案**的骨架，App::Part 可行时容器引用替换 obj 集合管理，查找逻辑同构）：
  - `__init__` 加 `self._parts: dict[str, dict] = {}`（PartInfo: `{"objects": set[对象Name], "placement": (pos, rot) | None 或容器引用}`）、`self._active_part: str | None = None`。
  - `new_part(name)`：校验 doc 存在/name 非空唯一；**若文档已有对象且 _parts 为空**（单零件模式造过东西）→ 先把全部既有对象归入隐式零件 `"Part1"`（result 告知调用方）；建新零件记录并置 active。
  - `set_active_part(name)` / `active_part` property / `part_names()`。
  - 对象归属：所有 addObject 路径走 Session 的注册钩子（最小侵入：tools 层创建对象后调用 `session.claim(obj)`？——改动面太大。**采用差集法**：Session 在事务开始记 doc.Objects 快照，提交时把新增对象自动归入当前活动零件——`_transaction` 已是全工具必经之路，零工具改动）。
  - `get_result_object(part=None)`：_parts 空 → 原全文档逻辑不动；非空 → 在 `part or _active_part` 的对象集内做同款查找（结果类型表/fallback 一致）。
  - `get_assembly_shape()`：_parts 空 → `get_result_shape()`；非空 → 各零件结果 shape 应用其位姿后 `Part.makeCompound`。
  - 标签注册表：`set_labels/resolve_*` 的快照存储加 part 键维度（`_labels[part_name or "__single__"]`）；resolve 默认活动零件。
- [ ] **Step 3: 硬验收线**

Run: `uv run pytest -q && uv run ruff check .`
Expected: **既有 227 条零修改全部通过** + 新注册表测试通过。任何既有测试需要改动 = 设计违约，回头改实现而不是改测试。

- [ ] **Step 4: commit** `feat(session): multi-part registry with implicit single-part compatibility`

---

## Task 2：`tools/assembly.py`

**Files:** Create `src/vibecad/tools/assembly.py`, `tests/test_tools_assembly.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_tools_assembly.py
"""assembly：校验矩阵 + align 位姿数学纯函数。快测。"""
import math

import pytest

from vibecad.tools import assembly


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"part": "", "position": [0, 0, 0]}, "part"),
    ({"part": "盖板"}, "至少"),
    ({"part": "盖板", "position": [0, 0]}, "position"),
    ({"part": "盖板", "rotation_axis": "w", "angle": 90}, "axis"),
    ({"part": "盖板", "rotation_axis": "z", "angle": 0}, "angle"),
])
def test_place_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        assembly.place_part(_NoopSession(), **kwargs)


@pytest.mark.parametrize("kwargs,msg", [
    ({"moving_part": "", "moving_face": "A", "target_part": "底板", "target_face": "F"}, "moving_part"),
    ({"moving_part": "盖板", "moving_face": "", "target_part": "底板", "target_face": "F"}, "moving_face"),
    ({"moving_part": "盖板", "moving_face": "A", "target_part": "盖板", "target_face": "F"}, "不同"),
    ({"moving_part": "盖板", "moving_face": "A", "target_part": "底板", "target_face": "F",
      "gap": float("nan")}, "gap"),
])
def test_align_parts_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        assembly.align_parts(_NoopSession(), **kwargs)


def test_align_placement_math():
    """纯数学：盖板底面(法向 -Z，面心 (30,20,100)) 贴底板顶面(法向 +Z，面心 (30,20,10))。
    期望：旋转为恒等（-Z 已对 -(+Z)），平移 z: 100→10。"""
    pos, rot_axis_angle = assembly._align_placement(
        moving_normal=(0, 0, -1), moving_center=(30, 20, 100),
        target_normal=(0, 0, 1), target_center=(30, 20, 10),
        target_e1=(1, 0, 0), target_e2=(0, 1, 0), offset=(0, 0), gap=0.0)
    assert pos == pytest.approx((0, 0, -90))          # 平移量
    assert rot_axis_angle[1] == pytest.approx(0.0)    # 无旋转


def test_align_placement_math_flip():
    """盖板底面法向 +Z（倒扣着）→ 需翻转 180°。"""
    _pos, (axis, angle) = assembly._align_placement(
        moving_normal=(0, 0, 1), moving_center=(0, 0, 0),
        target_normal=(0, 0, 1), target_center=(0, 0, 10),
        target_e1=(1, 0, 0), target_e2=(0, 1, 0), offset=(0, 0), gap=0.0)
    assert abs(angle) == pytest.approx(180.0)
```

- [ ] **Step 2: 实现**——关键结构：

```python
# src/vibecad/tools/assembly.py
"""装配工具（Round 8）：零件级位姿与面贴面对齐 + 干涉守卫。
纪律：校验 → 事务 → 变换 → recompute → 干涉断言（响亮，allow_interference 豁免）
→ 每零件完整性守卫 → 结构化 dict。"""

def _align_placement(*, moving_normal, moving_center, target_normal, target_center,
                     target_e1, target_e2, offset, gap):
    """纯数学：返回 (平移向量, (旋转轴, 角度))。
    旋转 = 最短弧把 moving_normal 转到 -target_normal（绕法向自转不指定，
    需精确朝向再 place_part 调整——docstring 契约）；
    锚点 = target_center + target_normal*gap + e1*offset[0] + e2*offset[1]；
    平移 = 锚点 - R(moving_center)。纯 Python 向量运算（quaternion/罗德里格斯公式），
    不依赖 FreeCAD（可快测）。"""

def assert_no_interference(session, *, allow=False, context=""):
    """对每对零件 common 体积 > 1e-6 → RuntimeError 报零件对与干涉量；
    allow=True 时放行并返回干涉清单（写进 result）。"""

def place_part(session, part, position=None, rotation_axis=None, angle=None): ...
    # 校验（至少给 position 或 rotation 之一）→ 事务 → 零件位姿更新（按 Task 0 选型：
    # App::Part.Placement 或 Session 位姿记录）→ recompute → 干涉断言 + 每零件
    # 完整性（_integrity 装配语义）→ result {placed: {...}, interference: []}

def align_parts(session, moving_part, moving_face, target_part, target_face,
                offset=(0.0, 0.0), gap=0.0, allow_interference=False): ...
    # 校验（两零件不同/标签非空/gap 有限）→ 事务 → 解析两面（resolve_face(part=) 跨
    # 零件标签）→ 取全局法向/面心（含现有位姿变换！面几何是局部坐标——先变换到全局再算）
    # → _align_placement → 应用为 moving 零件新位姿（叠加）→ recompute → 干涉断言 → result
```

（`align` 的全局坐标细节是实现难点：face 解析自零件局部 shape，法向/面心须经零件当前位姿变换到全局——Task 0 spike 的全局 shape 取法在此复用；实现时真机验证 z 共面精确再 commit。）

- [ ] **Step 3: 全绿 + commit** `feat(assembly): place_part/align_parts with interference guard`

---

## Task 3：标签命名空间 + _integrity 装配语义

**Files:** Modify `src/vibecad/engine/session.py`（标签 part 维度——若 Task 1 未含）、`src/vibecad/tools/_integrity.py`

- [ ] **Step 1**: `_integrity` 适配：`assert_single_solid` → 装配模式下按零件断言（每零件结果 shape 单 solid；装配 compound 天然多 solid 不算违例）；`cut_tool_radii/hole_count_snapshot/assert_no_sealed_holes` 吃装配范围（遍历全部零件的 Cut 链）——**单零件模式行为零变化**（_parts 空走原逻辑，与 Session 同款开关）。
- [ ] **Step 2**: 既有快/慢测回归（227 零修改 + features 慢测抽查）：`uv run pytest -q` + `VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_tools_features.py -q`
- [ ] **Step 3**: commit `feat(integrity): per-part assertions under assembly mode`

---

## Task 4：multiview/describe/export 装配适配

**Files:** Modify `src/vibecad/feedback/multiview.py`、`src/vibecad/feedback/text.py`、`src/vibecad/tools/export.py`、`src/vibecad/tools/modify.py`

- [ ] **Step 1**: multiview：`render_multiview(shape)` 的调用方（server）改传 `get_assembly_shape()`；iso 格分色——`collect_annotation_data` 增 per-part 着色信息（palette 基色按零件轮换，零件内仍循环色差）；标签表条目加零件归属后缀 `（零件：盖板）`（单零件模式不加，零变化）。正交格直接吃 compound（spike 已证 projectEx 支持）。
- [ ] **Step 2**: describe：装配摘要（`{"parts": {名: {volume, bbox, placement}}, "assembly_bbox": ..., "interference": [...]}`，单零件模式原格式不变）。export：STEP 写 compound；`split=True` 参数 per-part 文件（`<doc>_<part>.step`），每文件 `_assert_written`。modify.list_parameters 按零件分组（统一新形态 `{零件: {对象: {参数}}}`；单零件模式零件键用 `"Part1"`——**spec 决策：统一新形态**，server 测试断言同步更新（断言升格）。
- [ ] **Step 3**: 全绿 + commit `feat(feedback,export): assembly-aware rendering, describe, export`

---

## Task 5：server 集成

**Files:** Modify `src/vibecad/server.py`; Create `tests/test_server_round8.py`

- [ ] **Step 1**: 三个新工具（同型样板）：`new_part(name)`、`place_part(part, position=None, rotation_axis=None, angle=None)`、`align_parts(moving_part, moving_face, target_part, target_face, offset=None, gap=0.0, allow_interference=False)`；`_attach_view` 与 render_part 各入口改吃 `get_assembly_shape()`。工具数 18 → 21。
- [ ] **Step 2**: 测试（参照 round7 套路）：三工具委托/附图/失败结构化各一条、守卫一条、parts 新形态断言更新、握手纯净回归。
- [ ] **Step 3**: 全绿 + commit `feat(server): new_part/place_part/align_parts assembly tools`

---

## Task 6：真机慢测（6 条，tests/test_tools_features.py @slow 追加）

- [ ] **Step 1**: 六场景（`_run_in_env` 范式，body 顶格）：
  1. `test_two_parts_independent_modeling`：new_part×2 各自 add_box+孔互不干扰；set_active 切换后 modify 只影响活动零件。
  2. `test_place_part_moves_whole_chain`：带孔零件 place_part 旋转 90°——**R7 被拒场景现在成功**（孔随零件走，体积不变，孔完整性保持）。
  3. `test_align_parts_face_to_face`：盖板底面贴底板顶面——对齐后盖板底面全局 z == 底板顶面全局 z（1e-6）、面心 XY 对齐、gap=2 时间隙精确。
  4. `test_interference_rejected_and_allowed`：叠入 2mm → RuntimeError 报干涉量+回滚；allow_interference=True → 放行且 result 记录。
  5. `test_assembly_export_step`：装配 STEP 非空 + split=True 出两文件。
  6. `test_single_part_flow_regression`：不调 new_part 跑 R7 全流程（box→孔→modify→multiview）行为与既有一致。
- [ ] **Step 2**: 全量 slow 全绿 + commit `test: R8 assembly real-machine suite`

---

## Task 7：黑盒 + 两路终审 + 收尾

- [ ] **Step 1: 黑盒**（控制者）`.vibecad/blackbox_r8.py`：「底板 60×40×10 四角 4 孔（circular pattern 或 4×linear）→ new_part 盖板 60×40×5 → align 贴放 → 装配工程图存图（两零件可辨）→ gap=-2 故意叠入被拒 → 装配 STEP」；**人眼确认装配工程图**。
- [ ] **Step 2: 两路终审**：code-reviewer（兼容性缝隙/Session 改造等价性）+ silent-failure-hunter（重点：差集法对象归属的泄漏面、干涉断言盲区（薄面接触/共面零体积干涉）、活动零件状态在失败回滚后的残留、跨零件标签解析错位）。
- [ ] **Step 3: 收尾**：README（装配节+工具表）、计划回填、push、PR base=main、飞书、memory、汇报。

---

## 风险

1. App::Part headless（spike 先行+回退方案）。
2. 兼容性回归面（Session 是地基）——硬验收线"227 快测零修改"+全量慢测必跑。
3. align 的全局/局部坐标变换（spike 数学验证+真机 z 共面断言）。
4. 差集法对象归属：后台/嵌套事务的边界（事务是全工具唯一入口，嵌套不存在——审查重点核）。

## Verification

1. 快：`uv run pytest -q && uv run ruff check .` 全绿（既有 227 零修改）。
2. 慢：全量 `-m slow` 全绿（52 + 6 新）。
3. 黑盒人眼：装配工程图两零件可辨、干涉被拒。
4. 两路终审通过 + PR。
