# VibeCAD Round 6b — modify_part 参数修改 + 同径孔定位 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（逐任务 实现→审查→修复）。Steps 用 `- [ ]` 勾选。

**Goal:** 用户说"长度改 45 / 孔改大到 ⌀10"，参数化重算 + 工程图尺寸数字当场变化；同径多孔每孔都有定位尺寸。

**Architecture:** ①`tools/modify.py`：白名单参数修改（普通属性 setattr；Fillet/Chamfer 重写 Edges 元组）+ `list_parameters`（参数清单）；②server `modify_part` 工具 + `_attach_view` 附 `parts` 字段；③multiview 定位尺寸循环与 ⌀ 去重解耦。

**Tech Stack:** FreeCAD 参数化依赖链 recompute（既有）、UndoMode 回滚（R5）、_attach_view 自动回图（R6a）。

**Spec:** `docs/superpowers/specs/2026-06-10-round6b-modify-design.md`

---

## File Structure

```
src/vibecad/tools/modify.py     新建  _WHITELIST + modify_part + list_parameters
src/vibecad/server.py           改    modify_part 工具；_attach_view 增 parts 字段
src/vibecad/feedback/multiview.py 改  定位尺寸循环移出 ⌀ 去重循环
tests/test_tools_modify.py      新建  白名单校验矩阵 + list_parameters fake
tests/test_server_round6.py     改    modify_part 委托 + parts 字段
tests/test_multiview.py         改    多圆定位快测
tests/test_tools_features.py    改    @slow 追加（参数重算/回滚/同径双孔定位）
.vibecad/blackbox_modify.py     新建  黑盒（控制者跑+看图）
```

---

## Task 1：`tools/modify.py`

**Files:** Create `src/vibecad/tools/modify.py`, `tests/test_tools_modify.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_tools_modify.py
"""modify：白名单校验矩阵 + list_parameters。快测（fake 对象，不碰 FreeCAD）。"""
import math

import pytest

from vibecad.tools import modify


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"name": "", "parameter": "length", "value": 45}, "name"),
    ({"name": "Box", "parameter": "", "value": 45}, "parameter"),
    ({"name": "Box", "parameter": "length", "value": 0}, "value"),
    ({"name": "Box", "parameter": "length", "value": -5}, "value"),
    ({"name": "Box", "parameter": "length", "value": float("nan")}, "value"),
    ({"name": "Box", "parameter": "length", "value": float("inf")}, "value"),
])
def test_modify_part_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        modify.modify_part(_NoopSession(), **kwargs)


class _FakeObj:
    def __init__(self, name, type_id, **attrs):
        self.Name = name
        self.TypeId = type_id
        for k, v in attrs.items():
            setattr(self, k, v)


class _FakeDoc:
    def __init__(self, objects):
        self.Objects = list(objects)


def test_list_parameters_whitelist_only():
    doc = _FakeDoc([
        _FakeObj("Box", "Part::Box", Length=40.0, Width=30.0, Height=20.0),
        _FakeObj("HoleTool", "Part::Cylinder", Radius=4.0, Height=42.0),
        _FakeObj("Cut", "Part::Cut"),  # 非白名单类型 → 不出现
        _FakeObj("Fillet", "Part::Fillet", Edges=[(3, 2.0, 2.0), (7, 2.0, 2.0)]),
    ])
    out = modify.list_parameters(doc)
    assert out == {
        "Box": {"length": 40.0, "width": 30.0, "height": 20.0},
        "HoleTool": {"radius": 4.0, "height": 42.0},
        "Fillet": {"radius": 2.0},
    }


def test_list_parameters_empty_doc():
    assert modify.list_parameters(_FakeDoc([])) == {}
```

- [ ] **Step 2: 确认失败** `uv run pytest tests/test_tools_modify.py -q` → FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
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
            obj = session.get_object(name)
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
```

**动工前核对**：`session.get_object` 的不存在行为（读 src/vibecad/engine/session.py——若抛的错误不含现有对象名清单，在 modify_part 里 try/except 包装为带清单的 ValueError：`except 原类型: raise ValueError(f"对象 {name!r} 不存在——文档现有：{[o.Name for o in session.doc.Objects]}")`，以实际为准）。

- [ ] **Step 4: 全绿** `uv run pytest -q && uv run ruff check .`
- [ ] **Step 5: commit** `feat(modify): whitelist parametric modification + parameter listing`

---

## Task 2：server `modify_part` 工具 + `parts` 字段

**Files:** Modify `src/vibecad/server.py`; Modify `tests/test_server_round6.py`

- [ ] **Step 1: 失败测试**（追加 tests/test_server_round6.py）

```python
def test_modify_part_delegates_and_attaches(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._modify, "modify_part",
                        lambda session, name, parameter, value:
                        {"ok": True, "modified": {"name": name, "parameter": parameter,
                                                  "from": 40.0, "to": value}})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc: {"Box": {"length": 45.0}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)
    out = server.modify_part(name="Box", parameter="length", value=45)
    assert isinstance(out, list) and isinstance(out[1], Image)
    assert out[0]["modified"]["to"] == 45 and out[0]["parts"] == {"Box": {"length": 45.0}}


def test_attach_view_includes_parts(server, monkeypatch):
    from mcp.server.fastmcp import Image  # noqa: F401

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc: {"Box": {"length": 40.0}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)
    out = server.add_box(length=40, width=30, height=20)
    assert out[0]["parts"] == {"Box": {"length": 40.0}}


def test_modify_part_failure_structured(server, monkeypatch):
    def _boom(session, name, parameter, value):
        raise ValueError("参数 length 已是 45")

    monkeypatch.setattr(server._modify, "modify_part", _boom)
    out = server.modify_part(name="Box", parameter="length", value=45)
    assert out["ok"] is False and "已是" in out["message"]
```

（`_session.doc` 的 mock 方式以现有测试套路为准微调——若直接 monkeypatch 实例属性可行则用简单方式。）

- [ ] **Step 2: 确认失败** → FAIL（_modify 不存在）

- [ ] **Step 3: 实现**——server.py 顶部 `from vibecad.tools import modify as _modify`；`_attach_view` 成功路径 `result["labels"] = table` 之后加：

```python
        with _silence_fd1():
            result["parts"] = _modify.list_parameters(_session.doc)
```

新工具（放 chamfer_edges 之后）：

```python
@mcp.tool()
def modify_part(name: str, parameter: str, value: float) -> Any:
    """修改参数化对象的参数（如 name='Box', parameter='length', value=45）——
    依赖链（布尔/孔/圆角）自动重算。可改对象与参数见每步返回的 parts 字段。
    成功后自动附三视图拼图（工程图尺寸当场更新）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _modify.modify_part(_session, name, parameter, value)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"参数修改失败：{exc}"}
    return _attach_view(result)
```

- [ ] **Step 4: 全绿**（工具数 14→15；既有 _attach_view 测试若因 parts 字段新增而红，按"断言升格"原则更新）
- [ ] **Step 5: commit** `feat(server): modify_part tool + parts parameter listing in every result`

---

## Task 3：multiview 同径孔定位解耦

**Files:** Modify `src/vibecad/feedback/multiview.py`; Modify `tests/test_multiview.py`

- [ ] **Step 1: 失败测试**（test_multiview.py 追加；fake eng_views 同径双圆）

```python
def test_same_radius_circles_both_positioned():
    """同径双孔：⌀ 按径去重只标一次，但定位尺寸两孔都要有（解耦回归）。
    纯函数层断言方式：渲染不抛错 + PNG 尺寸增长（精确坐标断言在真机慢测）。"""
    rect = [[(0, 0), (60, 0)], [(60, 0), (60, 40)], [(60, 40), (0, 40)], [(0, 40), (0, 0)]]
    circle1 = [[(15 + 5 * math.cos(t / 7.64 * 3.14159 / 24), 20 + 5 * math.sin(t / 7.64))
                for t in range(3)]]  # 简化折线即可，circles 列表才是断言对象
    eng = {"front": {"vis": rect, "hid": [], "circles": []},
           "right": {"vis": rect, "hid": [], "circles": []},
           "top": {"vis": rect + circle1,
                   "hid": [],
                   "circles": [(15.0, 20.0, 5.0, True), (45.0, 20.0, 5.0, True)]}}
    png = multiview.multiview_png(
        eng_views=eng,
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[], dims=None)
    assert png.startswith(b"\x89PNG")
```

（import math 若缺则补；本快测主要钉"双同径圆渲染路径不抛错"，定位行为的精确断言在 Task 4 真机慢测做。）

- [ ] **Step 2: 实现**——multiview.py 中"⌀ 标注 + 定位尺寸"段重构：当前定位尺寸在 `seen_radii` 去重循环内（同径第二孔 `continue` 跳过导致无定位）。改为两个独立循环：

```python
            vis_full = [(cx, cy, r) for cx, cy, r, vis in circles if vis]
            # ⌀ 标注：按半径去重（同径只标一次，直径按径标）
            seen_radii: set[float] = set()
            for cx, cy, r in vis_full:
                rk = round(r, 6)
                if rk in seen_radii:
                    continue
                seen_radii.add(rk)
                ax.annotate(...)  # 既有 ⌀ 引线代码原样
            # 定位尺寸：每个可见整圆都标（位置按孔标，与 ⌀ 去重解耦——
            # 同径孔阵列每孔位置都可读；多孔拥挤布置留 backlog）
            for cx, cy, r in vis_full:
                _dim_h(...)  # 既有定位代码原样（圆心到 bbox 两方向）
                _dim_v(...)
```

（以现文件实际代码为准做最小重构，⌀ 引线与定位尺寸的绘制语句不变，只改循环结构。）

- [ ] **Step 3: 全绿 + commit** `fix(multiview): decouple per-hole position dims from diameter dedup`

---

## Task 4：真机慢测 + 黑盒 + 收尾

**Files:** Modify `tests/test_tools_features.py`（@slow 追加）；Create `.vibecad/blackbox_modify.py`；README/计划回填/飞书/memory

- [ ] **Step 1: 慢测**（用 `_run_in_env` 范式追加 5 条）

```python
@pytest.mark.slow
def test_modify_box_length_recomputes_chain(runtime_env):
    """改 Box.length → Cut 链自动重算，体积精确；旧标签过期。"""
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.naming import LabelExpiredError
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, features, modify
        s = Session()
        modeling.new_document(s, "mod")
        modeling.add_box(s, 40, 30, 20)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        features.add_hole(s, top, diameter=8)
        r = modify.modify_part(s, "Box", "length", 45)
        expect = 45 * 30 * 20 - math.pi * 16 * 20
        assert abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
        assert r["modified"]["from"] == 40.0 and r["modified"]["to"] == 45.0
        try:
            s.resolve_face(top)
            raise SystemExit("EXPECTED stale label")
        except LabelExpiredError:
            print("MODIFY_CHAIN_OK", round(r["volume"], 1))
    """)
    assert "MODIFY_CHAIN_OK" in out


@pytest.mark.slow
def test_modify_hole_radius(runtime_env):
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, features, modify
        s = Session()
        modeling.new_document(s, "rad")
        modeling.add_box(s, 40, 30, 20)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        features.add_hole(s, top, diameter=8)
        r = modify.modify_part(s, "HoleTool", "radius", 5)
        expect = 24000 - math.pi * 25 * 20
        assert abs(r["volume"] - expect) < 1.0
        print("MODIFY_RADIUS_OK")
    """)
    assert "MODIFY_RADIUS_OK" in out


@pytest.mark.slow
def test_modify_fillet_radius(runtime_env):
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.session import Session
        from vibecad.feedback import annotate
        from vibecad.tools import modeling, features, modify
        s = Session()
        modeling.new_document(s, "fil")
        modeling.add_box(s, 30, 30, 30)
        png, table, fr, er = annotate.render_annotated(
            s.get_result_shape(), mode="edges", view="iso")
        s.set_labels(fr, er, shown=set(table.keys()))
        lab = next(lab for lab, d in table.items() if "15.0" in d and "直线边" in d)
        features.fillet_edges(s, [lab], radius=2)
        r = modify.modify_part(s, "Fillet", "radius", 3)
        expect = 27000 - 9 * 30 * (1 - math.pi / 4)
        assert abs(r["volume"] - expect) < 0.1, (r["volume"], expect)
        print("MODIFY_FILLET_OK")
    """)
    assert "MODIFY_FILLET_OK" in out


@pytest.mark.slow
def test_modify_downstream_failure_rolls_back(runtime_env):
    """孔径改到超界 → 下游断言失败 → 回滚（参数复原、会话可恢复）。"""
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, features, modify
        s = Session()
        modeling.new_document(s, "rb")
        modeling.add_box(s, 40, 30, 20)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        features.add_hole(s, top, diameter=8)
        try:
            modify.modify_part(s, "HoleTool", "radius", 50)  # 超出 40x30 零件
            raise SystemExit("EXPECTED failure")
        except (RuntimeError, ValueError) as exc:
            print("RB_RAISED", type(exc).__name__)
        import FreeCAD
        obj = s.get_object("HoleTool")
        assert abs(float(obj.Radius) - 4.0) < 1e-9, "回滚后参数应复原为 4"
        r = modify.modify_part(s, "HoleTool", "radius", 5)  # 会话可恢复
        assert r["ok"]
        print("MODIFY_ROLLBACK_OK")
    """)
    assert "MODIFY_ROLLBACK_OK" in out


@pytest.mark.slow
def test_same_radius_holes_both_have_position_dims(runtime_env):
    """同径双孔：project_view circles 两个圆 → multiview 不抛错；
    定位解耦后两孔均有定位尺寸（数据层断言：vis_full 两项均进定位循环——
    以渲染成功 + circles 数==2 钉住；像素级留人眼黑盒）。"""
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.feedback.multiview import project_view, _VIEW_TFS
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "twin")
        modeling.add_box(s, 60, 40, 10)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        features.add_hole(s, top, diameter=10, offset=[-15, 0])
        png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr2, er2, shown=set(t2.keys()))
        top2 = next(lab for lab, d in t2.items() if "顶面" in d)
        features.add_hole(s, top2, diameter=10, offset=[15, 0])
        d, tf = _VIEW_TFS["top"]
        pv = project_view(s.get_result_shape(), d, tf)
        full_vis = [c for c in pv["circles"] if c[3]]
        assert len(full_vis) == 2, full_vis
        png3, *_ = multiview.render_multiview(s.get_result_shape())
        assert png3.startswith(b"\\x89PNG")
        print("TWIN_HOLES_OK")
    """)
    assert "TWIN_HOLES_OK" in out
```

- [ ] **Step 2: 全量慢测** `VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -q` → 既有 32 + 新 5 全绿；commit `test: modify_part real-machine suite (chain recompute, rollback, twin-hole dims)`

- [ ] **Step 3: 黑盒**（控制者亲自跑+看图）`.vibecad/blackbox_modify.py`（仿 blackbox_multiview.py）：`new_document→add_box(40,30,20)`→`add_hole(顶面,⌀8)` 存图1→`modify_part("Box","length",45)` 断言 ok+parts.Box.length==45 存图2→`modify_part("HoleTool","radius",5)` 存图3→describe 体积==45·30·20−π·25·20。**Read 图2/图3 人眼确认**：工程图尺寸 40→45、⌀8→⌀10 当场变化、同径场景定位齐全。

- [ ] **Step 4: 收尾**——README 工具表加 modify_part 行 + parts 字段说明；计划回填实施记录；两路终审（code-reviewer + silent-failure-hunter，重点：白名单逃逸/参数生效断言/回滚/parts 泄漏敏感信息=无）；push + PR base=main；飞书同步 spec+计划；memory 更新。

---

## 风险

1. Fillet/Chamfer Edges 元组重写：边索引不变只改值；OCCT 新半径失败由断言+回滚兜底（慢测④）。
2. `value == 当前值` 的 no-op 拒绝用 1e-12 容差——用户重复指令会得到明确"已是该值"而非假成功。
3. modify 后全部标签过期是预期（自动回图刷新）——慢测①钉住。

## Verification

1. 快：`uv run pytest -q && uv run ruff check .` 全绿。
2. 慢：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow` 全绿（37 条）。
3. 黑盒人眼：工程图尺寸数字当场变化。
4. 两路终审通过 + PR。
