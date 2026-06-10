# VibeCAD Round 7 — reposition + 孔阵列 + 草图拉伸 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（逐任务 实现→审查→修复）。Steps 用 `- [ ]` 勾选。

**Goal:** 已有对象可移动/旋转、孔可线性/圆形阵列、自由轮廓可 pad/pocket——单零件建模能力补齐，每步自动回工程图。

**Architecture:** ①把 R6b modify 的完整性快照/断言抽为共享 `tools/_integrity.py`（5 个消费方，rule-of-three 远超）；②`tools/transform.py` move/rotate（rotate 绕 BoundBox 中心）；③`features.add_hole` 加 `pattern`（逐孔链式 Cut——与既有快照判据零兼容成本）；④`tools/sketch.py` profile DSL（rect/circle/polygon/slot）→ wire → Face → extrude → Fuse/Cut。

**Tech Stack:** FreeCAD Part API（makePolygon/Face/extrude/Placement 旋转中心）、R6b 断言纪律全套、_attach_view 自动回图。

**Spec:** `docs/superpowers/specs/2026-06-10-round7-features-design.md`

---

## File Structure

```
src/vibecad/tools/_integrity.py  新建  从 modify.py 抽共享：孔完整性快照/比对 + 漂移/Touched/单solid 断言
src/vibecad/tools/modify.py      改    改用 _integrity（行为零变化，既有 19 场景取证守护）
src/vibecad/tools/transform.py   新建  move_part / rotate_part
src/vibecad/tools/features.py    改    add_hole 加 pattern（单孔路径零变化）
src/vibecad/tools/sketch.py      新建  _profile_area[纯] + _make_wire + extrude_profile
src/vibecad/server.py            改    move_part/rotate_part/extrude_profile 工具 + add_hole pattern 透传
tests/test_tools_transform.py    新建  校验矩阵
tests/test_tools_sketch.py       新建  校验矩阵 + 面积纯函数
tests/test_tools_features.py     改    pattern 校验快测 + @slow 8 条
tests/test_server_round7.py      新建  三工具委托/附图/pattern 透传
.vibecad/spike_r7.py             新建  Task 0 spike（slot wire/rotate 中心/无基体 pad）
.vibecad/blackbox_r7.py          新建  黑盒（控制者跑+看图）
```

---

## Task 0：Spike——三个真机疑点（控制者或 implementer 均可执行）

**Files:** Create `.vibecad/spike_r7.py`

- [ ] **Step 1**: 真机脚本（`.vibecad-test-runtime/mamba/envs/vibecad/bin/python`，sys.path 插 src + prepare_freecad_import）验证三点并打印结论：
  1. **slot wire**：两直线+两半圆弧（`Part.Arc`/`Part.makeCircle` 片段）拼 `Part.Wire` 是否闭合（`wire.isClosed()`）→ `Part.Face(wire)` 面积 == L·W+π(W/2)²。备选：`Part.makePolygon` 矩形 + 两端 `Part.Edge(Part.Arc(p1, pm, p2))` 三点弧。
  2. **rotate 绕中心**：`FreeCAD.Placement(FreeCAD.Vector(), rot, center)`（三参带旋转中心）`.multiply(obj.Placement)` 后 box(40,30,20) 绕 BoundBox 中心转 z 90°——断言 BoundBox 变 30×40×20 且中心不动。
  3. **无基体 pad**：空文档 `Part::Feature` + Shape 赋值 → recompute → get_result_object 能否选中（fallback 路径）。
- [x] **Step 2**: 跑通并把三个结论回填本计划此处。

> **✅ Spike 结论（2026-06-10 真机一次通过）**：
> 1. **slot wire**：`Part.LineSegment(p1,p2).toShape()` ×2 + `Part.Arc(p_start, p_mid, p_end).toShape()` 三点弧 ×2（弧中点在 ±(L/2+r, 0)）→ `Part.Wire(edges)` 闭合，`Part.Face(wire).Area` 与 L·W+πr² 精确匹配（210.2655）。
> 2. **rotate 绕中心**：`obj.Placement = FreeCAD.Placement(FreeCAD.Vector(), rot, center).multiply(obj.Placement)`——box 40×30×20 绕 z 转 90° 后 bbox 30×40×20、中心 (20,15,10) 不动。计划 Task 1 代码的写法即正确。
> 3. **无基体 pad**：`feat = doc.addObject("Part::Feature", "Profile"); feat.Shape = solid; doc.recompute()` 体积/有效性精确（1051.3274, valid=True）。
- [ ] **Step 3**: commit（仅本计划文档的结论回填；spike 脚本在 gitignore 内）

---

## Task 1：`tools/_integrity.py` 抽取 + `tools/transform.py`

**Files:** Create `src/vibecad/tools/_integrity.py`, `src/vibecad/tools/transform.py`, `tests/test_tools_transform.py`; Modify `src/vibecad/tools/modify.py`

- [ ] **Step 1: 抽取共享守卫**——读 src/vibecad/tools/modify.py 现状（7fc6a3d 终态），把以下职责搬到 `_integrity.py` 模块级函数（modify.py 改为调用，**行为零变化**，既有快/慢测+三套取证脚本守护）：

```python
# src/vibecad/tools/_integrity.py
"""特征完整性共享守卫（R7 抽取自 modify.py）：孔完整性快照/比对、结果对象漂移、
Touched 账本、单 solid。消费方：modify/transform/features(pattern)/sketch。
判据沿革见 R6b 计划实施记录（虚构不变量教训：基线必须取改前实际状态）。"""
# 函数签名（实现从 modify.py 现状代码平移，保留全部注释/留档）：
# def cut_tool_radii(doc) -> list[float]            # 文档中 Part::Cut.Tool 圆柱半径列表
# def hole_count_snapshot(shape, radii) -> dict[float, int]   # 改前实际完整圆柱面计数基线
# def assert_holes_intact(shape, expected: dict[float, int])  # 逐半径 >= 期望，否则 RuntimeError
# def assert_single_solid(shape, context: str)                # 单 solid
# def assert_not_touched(obj, parameter_desc: str)            # Touched 账本
# def assert_result_not_drifted(session, before_name: str)    # 结果对象漂移
```

（精确签名以 modify.py 现状代码的自然切分为准——目标是 modify/transform 两处调用同一实现，不复制。抽取后 `uv run pytest -q` 与 `VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_tools_features.py -q` 必须全绿。）

- [ ] **Step 2: transform 失败测试**

```python
# tests/test_tools_transform.py
"""transform：校验矩阵快测（不碰 FreeCAD）。"""
import pytest

from vibecad.tools import transform


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"name": "", "position": [0, 0, 0]}, "name"),
    ({"name": "Box", "position": [0, 0]}, "position"),
    ({"name": "Box", "position": ["a", 0, 0]}, "position"),
    ({"name": "Box", "position": [float("nan"), 0, 0]}, "position"),
])
def test_move_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        transform.move_part(_NoopSession(), **kwargs)


@pytest.mark.parametrize("kwargs,msg", [
    ({"name": "", "axis": "z", "angle": 90}, "name"),
    ({"name": "Box", "axis": "w", "angle": 90}, "axis"),
    ({"name": "Box", "axis": "z", "angle": 0}, "angle"),
    ({"name": "Box", "axis": "z", "angle": 360}, "angle"),
    ({"name": "Box", "axis": "z", "angle": float("nan")}, "angle"),
])
def test_rotate_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        transform.rotate_part(_NoopSession(), **kwargs)
```

- [ ] **Step 3: 实现 transform.py**

```python
# src/vibecad/tools/transform.py
"""reposition 工具（Round 7）：移动/旋转已有图元对象，依赖链自动重算。
纪律：校验 → 事务 → 改 Placement → recompute → 完整性守卫（_integrity）→ 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session
from vibecad.tools import _integrity

_MOVABLE = ("Part::Box", "Part::Cylinder")  # Cut/Fillet/Chamfer 跟随 Base，不可直接 repos
_AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}


def _validate_position(position) -> None:
    if (not isinstance(position, (list, tuple)) or len(position) != 3
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                       and math.isfinite(c) for c in position)):
        raise ValueError(f"position 必须是 3 个有限数字 (x, y, z)（得到 {position!r}）")


def _movable_obj(session: Session, name: str):
    obj = session.get_object(name)
    if getattr(obj, "TypeId", "") not in _MOVABLE:
        names = [o.Name for o in session.doc.Objects
                 if getattr(o, "TypeId", "") in _MOVABLE]
        raise ValueError(
            f"对象 {name!r}（{getattr(obj, 'TypeId', '?')}）不可直接移动/旋转"
            f"（布尔/圆角结果跟随其图元）——可操作对象：{names or '（无）'}")
    return obj


def _reposition(session: Session, name: str, apply, op: str) -> dict[str, Any]:
    """共享骨架：守卫快照 → apply(obj, FreeCAD) 改 Placement → recompute → 全套断言。
    注：reposition 后结果体积允许变化（移动孔刀具改变相交区是合法目的），
    不做体积断言；越界/切空/缺口由孔完整性快照与单 solid 断言兜住。"""
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction(op):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            obj = _movable_obj(session, name)
            before_name = session.get_result_object().Name
            shape_before = session.get_result_shape()
            radii = _integrity.cut_tool_radii(session.doc)
            counts = _integrity.hole_count_snapshot(shape_before, radii)
            apply(obj, FreeCAD)
            session.doc.recompute()
            _integrity.assert_not_touched(obj, op)
            _integrity.assert_result_not_drifted(session, before_name)
            shape = session.get_result_shape()
            session.assert_valid_solid(shape)
            _integrity.assert_single_solid(shape, op)
            _integrity.assert_holes_intact(shape, counts)
            pl = obj.Placement
            result = {"ok": True, "name": obj.Name, "volume": shape.Volume,
                      op: {"position": [pl.Base.x, pl.Base.y, pl.Base.z]},
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
    return result


def move_part(session: Session, name: str, position) -> dict[str, Any]:
    """把图元移动到绝对位置（依赖链自动重算）。"""
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串（对象名，见 parts 字段）")
    _validate_position(position)

    def _apply(obj, FreeCAD):
        pl = obj.Placement
        pl.Base = FreeCAD.Vector(*[float(c) for c in position])
        obj.Placement = pl

    return _reposition(session, name, _apply, "move")


def rotate_part(session: Session, name: str, axis: str = "z", angle: float = 90.0) -> dict[str, Any]:
    """绕全局轴、以对象 BoundBox 几何中心为旋转中心旋转（角度制）。"""
    if not name or not isinstance(name, str):
        raise ValueError("name 必须是非空字符串（对象名，见 parts 字段）")
    if axis not in _AXES:
        raise ValueError(f"axis 必须是 x/y/z（得到 {axis!r}）")
    if (not isinstance(angle, (int, float)) or isinstance(angle, bool)
            or not math.isfinite(angle) or angle == 0 or not -360 < angle < 360):
        raise ValueError(f"angle 必须是 (-360, 360) 内非零角度（得到 {angle!r}）")

    def _apply(obj, FreeCAD):
        bb = obj.Shape.BoundBox
        center = FreeCAD.Vector((bb.XMin + bb.XMax) / 2, (bb.YMin + bb.YMax) / 2,
                                (bb.ZMin + bb.ZMax) / 2)
        rot = FreeCAD.Rotation(FreeCAD.Vector(*_AXES[axis]), float(angle))
        # 绕 center 旋转 = Placement(零平移, rot, center) 左乘（Task 0 spike 验证写法）
        obj.Placement = FreeCAD.Placement(FreeCAD.Vector(), rot, center).multiply(obj.Placement)

    return _reposition(session, name, _apply, "rotate")
```

- [ ] **Step 4: 全绿**：`uv run pytest -q && uv run ruff check .`（modify 抽取回归含 19 场景慢测可后置到 Task 5 统跑，但 features 慢测先跑一遍：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_tools_features.py -q`）
- [ ] **Step 5: commit ×2**：`refactor(modify): extract shared integrity guards to tools/_integrity` + `feat(transform): move_part/rotate_part with full integrity guards`

---

## Task 2：`add_hole` 阵列 `pattern`

**Files:** Modify `src/vibecad/tools/features.py`; Modify `tests/test_tools_features.py`（快测）

- [ ] **Step 1: 失败测试**（快测校验矩阵，追加）

```python
@pytest.mark.parametrize("pattern,msg", [
    ({"type": "grid"}, "type"),
    ({"type": "linear"}, "count"),
    ({"type": "linear", "count": 1, "spacing": 10}, "count"),
    ({"type": "linear", "count": 51, "spacing": 10}, "count"),
    ({"type": "linear", "count": 4, "spacing": 0}, "spacing"),
    ({"type": "linear", "count": 4, "spacing": 10, "direction": [0, 0]}, "direction"),
    ({"type": "circular", "count": 6}, "radius"),
    ({"type": "circular", "count": 6, "radius": -1}, "radius"),
])
def test_add_hole_pattern_validation(pattern, msg):
    with pytest.raises(ValueError, match=msg):
        features.add_hole(_NoopSession(), face="A", diameter=6, pattern=pattern)
```

- [ ] **Step 2: 实现**——`add_hole(session, face, diameter, depth=None, offset=(0.0, 0.0), pattern=None)`：
  1. 新增 `_validate_pattern(pattern) -> list[tuple[float, float]]`（纯函数，返回**相对 offset 的孔心增量列表**，含 (0,0) 首孔）：linear → `[i*S*ndu, i*S*ndv]`；circular → `[R*cos(2πi/N), R*sin(2πi/N)]`。校验在任何 session 访问前。
  2. 主体重构：现有"单孔几何创建+Cut"段提为内部 `_drill(session, base_obj, face_data, uv, diameter, depth) -> cut_obj`（face_data=一次解析好的 (n, e1, e2, c, lift, length)）；主体先解析 face/法向/基/孔心列表（全部绝对坐标一次算好），事务内循环 `base_obj = _drill(...)` 链式 Cut，**循环后统一一次 recompute**，然后全套断言：assert_valid_solid + 单 solid + **完整圆柱面计数（该径）== 改前 + count**（精确等于，不是 >=——阵列要么全成要么全无）+ 体积严格减少 + 漂移守卫。
  3. `pattern=None` 走 count=1 的同一路径（行为零变化，既有单孔快/慢测守护）。
  4. result：pattern 时 `"holes": {"count": N, "pattern": {...}, "diameter": d}`，单孔保持现有 `"hole"` 字段（向后兼容）。
- [ ] **Step 3: 全绿 + commit** `feat(features): linear/circular hole patterns via chained cuts (all-or-nothing)`

---

## Task 3：`tools/sketch.py` 轮廓拉伸

**Files:** Create `src/vibecad/tools/sketch.py`, `tests/test_tools_sketch.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_tools_sketch.py
"""sketch：profile DSL 校验矩阵 + 面积纯函数。快测（不碰 FreeCAD）。"""
import math

import pytest

from vibecad.tools import sketch


class _NoopSession:
    pass


@pytest.mark.parametrize("profile,msg", [
    ("rect", "profile"),
    ({"type": "blob"}, "type"),
    ({"type": "rect", "length": 10}, "width"),
    ({"type": "rect", "length": 0, "width": 5}, "length"),
    ({"type": "circle"}, "radius"),
    ({"type": "polygon", "points": [[0, 0], [1, 0]]}, "points"),
    ({"type": "polygon", "points": [[0, 0], [1, 0], ["x", 1]]}, "points"),
    ({"type": "slot", "length": 10}, "width"),
])
def test_extrude_profile_validation(profile, msg):
    with pytest.raises(ValueError, match=msg):
        sketch.extrude_profile(_NoopSession(), profile=profile, height=5)


@pytest.mark.parametrize("kwargs,msg", [
    ({"profile": {"type": "circle", "radius": 3}, "height": 0}, "height"),
    ({"profile": {"type": "circle", "radius": 3}, "height": 5, "operation": "carve"}, "operation"),
])
def test_extrude_args_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        sketch.extrude_profile(_NoopSession(), **kwargs)


def test_profile_area_formulas():
    assert sketch._profile_area({"type": "rect", "length": 20, "width": 10}) == 200
    assert sketch._profile_area({"type": "circle", "radius": 3}) == pytest.approx(9 * math.pi)
    assert sketch._profile_area({"type": "slot", "length": 10, "width": 4}) == \
        pytest.approx(40 + math.pi * 4)
    # shoelace：直角三角形 (0,0)(10,0)(0,8) → 40
    assert sketch._profile_area({"type": "polygon",
                                 "points": [[0, 0], [10, 0], [0, 8]]}) == pytest.approx(40)
```

- [ ] **Step 2: 实现**——结构：

```python
# src/vibecad/tools/sketch.py 关键骨架（完整校验/事务/断言纪律同 transform）
_PROFILE_REQUIRED = {"rect": ("length", "width"), "circle": ("radius",),
                     "polygon": ("points",), "slot": ("length", "width")}

def _validate_profile(profile) -> None: ...   # dict/type/各 type 必填参数>0 有限/points≥3 且每点 2 数字

def _profile_area(profile) -> float:
    t = profile["type"]
    if t == "rect":
        return float(profile["length"]) * float(profile["width"])
    if t == "circle":
        return math.pi * float(profile["radius"]) ** 2
    if t == "slot":  # length=两半圆心距（直段长），总长=length+width
        w = float(profile["width"])
        return float(profile["length"]) * w + math.pi * (w / 2) ** 2
    pts = profile["points"]  # shoelace
    s = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
            for i in range(len(pts)))
    return abs(s) / 2

def _make_face(profile, Part, FreeCAD):
    """局部 XY 平面、原点居中的 Part.Face（slot 弧构造按 Task 0 spike 结论）。"""
    # rect: 四角 makePolygon 闭合；circle: Part.Wire([Part.makeCircle(r) 的 Edge])
    # polygon: makePolygon(points+首点)；slot: 两直线+两个三点弧（Part.Arc）→ Part.Wire
    # 返回 Part.Face(wire)，构造失败让异常上抛（几何断言纪律：响亮）

def extrude_profile(session, profile, height, face=None, offset=(0.0, 0.0), operation="pad"):
    # 校验全部先行（profile/height>0 有限/offset 2 数字/operation in (pad,pocket)）
    # 事务内：
    #   face=None → 平面原点(0,0,0)、normal=+Z、e1/e2=X/Y；pocket 且文档无结果对象 → ValueError
    #   face=标签 → resolve_face + Plane 校验 + _outward_normal + _inplane_axes + 面心（复用 features 的私有函数：从 features import _outward_normal, _inplane_axes——抽到 _integrity 或互相 import，以 ruff 干净为准）
    #   local_face = _make_face(...)；solid = local_face.extrude(Vector(0,0,height))
    #   Placement：rotation Z→(+n pad / -n pocket)，base=放置点（pad 贴面起、pocket 从面起向内——与 add_hole 的 lift 同理：pocket 起点抬 lift=0.5 向内 depth+lift；pad 起点沉 0?贴面即可，fuse 共面无碍——spike 已证 Feature 用法）
    #   feature = doc.addObject("Part::Feature", "Profile"); feature.Shape = solid_placed
    #   无基体 pad → feature 即结果；有基体 → Part::Fuse / Part::Cut 文档对象
    #   recompute → 完整性守卫全套（_integrity）+ 体积断言：
    #     pad: vol_after - vol_before ≈ area*height（容差 1%+1e-6；无基体时 vol_after ≈ area*height）
    #     pocket: 严格减少 + 单 solid + 孔完整性不退化（精确 ≈ 留慢测受控场景断言）
    # result: {"ok", "name", "volume", "extrude": {"profile": type, "area": ..., "height": ..., "operation": ...}, "parametric": False, labels_stale/hint}
```

- [ ] **Step 3: 全绿 + commit** `feat(sketch): profile DSL (rect/circle/polygon/slot) extrude pad/pocket`

---

## Task 4：server 集成

**Files:** Modify `src/vibecad/server.py`; Create `tests/test_server_round7.py`

- [ ] **Step 1: 失败测试**（test_server_round7.py：三工具守卫拦截/委托+附图形态（mock _multiview+_modify.list_parameters，参照 test_server_round6 套路）/add_hole pattern 透传（mock features.add_hole 记录 pattern 实参）/LabelExpired 结构化。每个工具一条委托+一条失败结构化，共 ~8 条——代码按 test_server_round6.py 同型编写，mock helper 复用其 `_mock_multiview` 模式。）
- [ ] **Step 2: 实现**——三个新工具（样板与 modify_part 完全同型，docstring 中文说明用途与参数来源）：

```python
@mcp.tool()
def move_part(name: str, position: list[float]) -> Any:
    """把图元移动到绝对位置 [x, y, z]（mm）——依赖链自动重算，成功后自动附三视图。
    可移动对象见 parts 字段（布尔/圆角结果跟随其图元，不可直接移动）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _transform.move_part(_session, name, tuple(position) if position else position)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"移动失败：{exc}"}
    return _attach_view(result)
```

（rotate_part(name, axis="z", angle=90)、extrude_profile(profile: dict, height: float, face: str | None = None, offset: list[float] | None = None, operation: str = "pad") 同型；add_hole 签名加 `pattern: dict | None = None` 透传。注意 move_part 的 position 在 tools 层校验，server 不预转 tuple 也可——以 tools 校验吃 list 为准（_validate_position 接受 list/tuple）。）

- [ ] **Step 3: 全绿 + commit** `feat(server): move_part/rotate_part/extrude_profile tools + hole pattern passthrough`

---

## Task 5：真机慢测（8 条）

**Files:** Modify `tests/test_tools_features.py`（@slow 追加，`_run_in_env` 范式）

- [ ] **Step 1**: 八条慢测（场景与断言）：

```python
@pytest.mark.slow
def test_move_hole_tool_relocates_hole(runtime_env):
    """移动孔刀具 → 孔到新位置（体积不变）；移出零件 → 拒绝+回滚。"""
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, features, transform
        s = Session()
        modeling.new_document(s, "mv")
        modeling.add_box(s, 40, 30, 20)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        features.add_hole(s, top, diameter=8)
        r = transform.move_part(s, "HoleTool", [10, 10, -7.425824035672481])
        assert abs(r["volume"] - (24000 - math.pi * 16 * 20)) < 1.0
        try:
            transform.move_part(s, "HoleTool", [200, 200, 0])
            raise SystemExit("EXPECTED rejection")
        except RuntimeError:
            pass
        assert abs(s.get_result_shape().Volume - r["volume"]) < 1e-6  # 回滚
        print("MOVE_OK")
    """)
    assert "MOVE_OK" in out


@pytest.mark.slow
def test_rotate_box_swaps_bbox(runtime_env):
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.tools import modeling, transform
        s = Session()
        modeling.new_document(s, "rot")
        modeling.add_box(s, 40, 30, 20)
        r = transform.rotate_part(s, "Box", axis="z", angle=90)
        bb = s.get_result_shape().BoundBox
        assert abs(bb.XLength - 30) < 1e-6 and abs(bb.YLength - 40) < 1e-6  # 轴交换
        cx = (bb.XMin + bb.XMax) / 2
        assert abs(cx - 20) < 1e-6  # 绕中心转：中心不动（原中心 x=20）
        print("ROTATE_OK")
    """)
    assert "ROTATE_OK" in out


@pytest.mark.slow
def test_linear_hole_pattern(runtime_env):
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.feedback.multiview import project_view, _VIEW_TFS
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "lin")
        modeling.add_box(s, 60, 30, 10)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        r = features.add_hole(s, top, diameter=6, offset=[-15, 0],
                              pattern={"type": "linear", "count": 4, "spacing": 10})
        expect = 60*30*10 - 4 * math.pi * 9 * 10
        assert abs(r["volume"] - expect) < 1.0
        assert r["holes"]["count"] == 4
        d, tf = _VIEW_TFS["top"]
        pv = project_view(s.get_result_shape(), d, tf)
        assert len([c for c in pv["circles"] if c[3]]) == 4
        print("LINEAR_OK")
    """)
    assert "LINEAR_OK" in out


@pytest.mark.slow
def test_linear_pattern_overlap_rejected(runtime_env):
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "ovl")
        modeling.add_box(s, 60, 30, 10)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        v0 = s.get_result_shape().Volume
        try:
            features.add_hole(s, top, diameter=6, pattern={"type": "linear", "count": 4, "spacing": 4})
            raise SystemExit("EXPECTED rejection (overlap)")
        except RuntimeError:
            pass
        assert abs(s.get_result_shape().Volume - v0) < 1e-6  # 全无（整体回滚）
        print("OVERLAP_REJECT_OK")
    """)
    assert "OVERLAP_REJECT_OK" in out


@pytest.mark.slow
def test_circular_hole_pattern(runtime_env):
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "cir")
        modeling.add_box(s, 60, 60, 10)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        r = features.add_hole(s, top, diameter=5,
                              pattern={"type": "circular", "count": 6, "radius": 18})
        expect = 36000 - 6 * math.pi * 6.25 * 10
        assert abs(r["volume"] - expect) < 1.0
        print("CIRCULAR_OK")
    """)
    assert "CIRCULAR_OK" in out


@pytest.mark.slow
def test_extrude_pad_rect(runtime_env):
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, sketch
        s = Session()
        modeling.new_document(s, "pad")
        modeling.add_box(s, 40, 30, 10)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        r = sketch.extrude_profile(s, {"type": "rect", "length": 20, "width": 10},
                                   height=5, face=top, operation="pad")
        assert abs(r["volume"] - (12000 + 1000)) < 12000 * 0.01
        print("PAD_OK")
    """)
    assert "PAD_OK" in out


@pytest.mark.slow
def test_extrude_pocket_slot_and_polygon(runtime_env):
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, sketch
        s = Session()
        modeling.new_document(s, "pkt")
        modeling.add_box(s, 60, 40, 20)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        slot_area = 20 * 8 + math.pi * 16
        r = sketch.extrude_profile(s, {"type": "slot", "length": 20, "width": 8},
                                   height=5, face=top, operation="pocket")
        assert abs((48000 - r["volume"]) - slot_area * 5) < slot_area * 5 * 0.01
        png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr2, er2, shown=set(t2.keys()))
        top2 = next(lab for lab, d in t2.items() if "顶面" in d)
        r2 = sketch.extrude_profile(s, {"type": "polygon",
                                        "points": [[-25, -15], [-15, -15], [-25, -7]]},
                                    height=3, face=top2, offset=[0, 0], operation="pocket")
        assert (r["volume"] - r2["volume"]) > 0  # 三角 pocket 继续减料
        print("POCKET_OK")
    """)
    assert "POCKET_OK" in out


@pytest.mark.slow
def test_floating_pad_rejected(runtime_env):
    """pad 轮廓 offset 出零件且不接触 → Fuse 双 solid → 拒绝回滚。"""
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, sketch
        s = Session()
        modeling.new_document(s, "flt")
        modeling.add_box(s, 40, 30, 10)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        v0 = s.get_result_shape().Volume
        try:
            sketch.extrude_profile(s, {"type": "circle", "radius": 5}, height=5,
                                   face=top, offset=[100, 100], operation="pad")
            raise SystemExit("EXPECTED rejection (floating pad)")
        except RuntimeError:
            pass
        assert abs(s.get_result_shape().Volume - v0) < 1e-6
        print("FLOATING_REJECT_OK")
    """)
    assert "FLOATING_REJECT_OK" in out
```

（test_move 中 HoleTool 的 z 取当前实现的孔起点 z 值——动工时先打印一次真实值再写死，或断言放宽为"体积不变"。）

- [ ] **Step 2**: 全量 slow 跑通 + commit `test: R7 real-machine suite (reposition, patterns, extrude)`

---

## Task 6：黑盒 + 收尾 + 两路终审

- [ ] **Step 1: 黑盒**（控制者亲自跑+看图）`.vibecad/blackbox_r7.py`：真协议「add_box(60,30,10) → add_hole(顶面 ⌀6 offset=[-15,0] pattern linear 4×10) 存图 → extrude_profile(顶面 slot 20×8 pocket 深5) 存图 → move_part(HoleTool…) 或 rotate 演示 存图」每步断言收到 [json,image]；**Read 图人眼确认**：4 孔等距+各有定位、槽形轮廓与 ⌀ 标注、工程图整洁。
- [ ] **Step 2: README**——工具表加三行（move_part/rotate_part/extrude_profile）+ add_hole 行注 pattern；「Round 7」小节。
- [ ] **Step 3: 两路终审**：code-reviewer（集成缝隙）+ silent-failure-hunter（重点：pattern 全有全无、pad/pocket 体积断言盲区、reposition 无体积断言的风险面、_integrity 抽取等价性）。发现必修+复审。
- [ ] **Step 4: 收尾**：计划回填实施记录；push；PR（base 按 #7 状态：已合并→main，未合并→栈式注明不删分支）；飞书同步 spec+计划；memory 更新；最终汇报。

---

## 风险

1. slot 弧 wire 闭合（Task 0 spike 先验证）。
2. pad 体积断言对"轮廓部分悬空于面外但仍接触基体"的场景——增量仍 ≈ area*height（悬空段也是实体）；完全脱离才双 solid。慢测⑧钉浮空。
3. 阵列 N 个链式 Cut 一次 recompute 的性能（N=50 上限）——慢测记录 4/6 孔时长，50 孔留观察。
4. rotate 中心补偿的 Placement 复合顺序（spike 验证）。

## Verification

1. 快：`uv run pytest -q && uv run ruff check .` 全绿。
2. 慢：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow` 全绿（39 + 8 = 47 条）。
3. 黑盒人眼：阵列/槽/移动的工程图。
4. 两路终审通过 + PR。

---

## 实施记录与验收（2026-06-10，macOS arm64 实机）

**执行方式**：闭关 subagent-driven。Spike 三疑点一次通过（slot 三点弧 wire/rotate 旋转中心 Placement/bare Part::Feature）。

### 验收结果
- 快测 **227 passed** + ruff 清零；真机慢测 **51 passed**（含 R7 12 条新增：reposition/阵列/拉伸/四条审查取证固化）。
- **黑盒人眼**：4 孔线性阵列（每孔定位链式 15/25/35/45）→ slot pocket（跑道轮廓+隐藏线）→ move 孔重定位（定位链同步更新、孔出现在槽中央）→ pocket 打穿响亮被拒。
- 黑盒还实证了一个**断言正确拦截**：rotate 带特征链的基体 → 刀具不随动 → 孔完整性拒绝（rotate 单图元语义符合 spec；"带特征整体旋转"记 R8 装配轮候选）。

### 第七轮审查战果（2 Critical + 3 Important 全真机实锤，修复 `1cbab96`）
1. **C1**：有基体文档上 `extrude_profile(face=None)` 创建孤儿实体并劫持结果对象 → 孤儿守卫。
2. **C2**：add_hole 不保护其它半径既有孔（⌀20 同心孔吞 ⌀8 报 ok）→ 全径快照 + assert_holes_intact。
3. **I1**：pocket 打穿/pattern 盲孔超深静默放行（R6b 盲孔教训在新路径复发）→ 双边体积核算。
4. **I2**：reposition 轴向退化——孔被封死成**密封内腔**仍 ok → `assert_no_sealed_holes` 端面探针（transform+sketch pad 双接入）。
5. **I3**：非对称轮廓在侧面的取向不可预测（Rotation 最短弧）→ Matrix(e1,e2,n) 显式取向，profile 局部 X/Y 严格映射面内 u/v。

### 方法论沉淀
"体积/计数断言只看自己的语义桶"是惯犯模式的新变体（C2/I1 都是新特征路径绕开既有守卫）——**新工具接入时必须显式核对：既有几何语义守卫（全径快照/深度核算/密封探针）是否全部接线**，已沉淀为 _integrity 共享模块的接入清单注释。
