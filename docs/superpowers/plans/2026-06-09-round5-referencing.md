# VibeCAD Round 5 — 可指代性（标注渲染 + 标签注册表 + 首批特征工具）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（逐任务 实现→审查→修复）。Steps 用 `- [ ]` 勾选。

**Goal:** 用户能对着标注渲染图精确指代几何（"在 A 面打孔"、"E3 倒角"），并由首批面/边特征工具（add_hole / fillet_edges / chamfer_edges）真正消费这些指代。

**Architecture:** 三层——①`feedback/annotate.py` 标注绘制（逐面 tessellate → 可见面贴标签 + 尺寸线，纯函数可快测）；②`engine/naming.py` + Session 注册表（标签→几何指纹快照，执行前容差匹配找回，对不上抛 `LabelExpiredError`，解 FreeCAD 索引重排）；③`tools/features.py` 特征工具（消费标签，继承"体积严格减少/面数增加"几何断言纪律）。

**Tech Stack:** matplotlib Agg（已有）、FreeCAD Part::Fillet/Part::Chamfer/Part::Cut、FastMCP 多 content 返回（Image+JSON，spike 验证）。

**Spec:** `docs/superpowers/specs/2026-06-09-round5-referencing-design.md`

---

## File Structure

```
src/vibecad/engine/naming.py     新建  指纹/匹配/语义名/summary + LabelExpiredError（纯逻辑，不 import FreeCAD）
src/vibecad/engine/session.py    改    _labels 注册表 + set_labels/resolve_face/resolve_edge/get_result_object
src/vibecad/feedback/annotate.py 新建  camera_direction/mesh_normal/annotated_png[纯] + render_annotated[tessellate]
src/vibecad/tools/features.py    新建  add_hole/fillet_edges/chamfer_edges + _outward_normal/_inplane_axes
src/vibecad/server.py            改    render_part 加 annotate/edges_of；新工具 add_hole/fillet_edges/chamfer_edges
tests/test_naming.py             新建  快测：指纹/匹配/过期/语义名
tests/test_annotate.py           新建  快测：相机方向/网格法向/annotated_png 出 PNG
tests/test_tools_features.py     新建  快测：参数校验；慢测：真机 add_hole/fillet/标签过期
tests/test_server_round5.py      新建  快测：新工具 mock 委托 + 握手纯净回归
tests/test_runtime_integration.py 改   追加 test_annotated_feature_flow（端到端）
.vibecad/spike_annotate.py       新建  Task 0 原型脚本（不进 src）
.vibecad/blackbox_annotate.py    新建  黑盒：真协议标注→打孔→新图
```

---

## Task 0：Spike——标注原型图（人眼检验）+ FastMCP 多内容验证

**Files:** Create `.vibecad/spike_annotate.py`

- [ ] **Step 1: FastMCP 列表返回验证**（dev venv，读源码定结论）

Run: `uv run python -c "import mcp.server.fastmcp.server as s, inspect; print(inspect.getsource(s._convert_to_content))"`
目标：确认 FastMCP 对工具返回 `list` 的转换行为（预期：list/tuple 逐元素递归转换——`Image`→ImageContent、`str`→TextContent，即支持混合多 content）。若不支持混合列表 → 回退方案：`render_part` 只回 Image，标签表经新只读工具 `get_labels()` 返回（dict）。**把结论写进本文件此处再继续。**

> **✅ 已验证（2026-06-09，mcp SDK in .venv）**：`mcp/server/fastmcp/utilities/func_metadata.py::_convert_to_content` 对 `list | tuple` 做 `chain.from_iterable` 递归逐元素转换——`Image`→ImageContent、`str`→TextContent。**混合列表 `[Image, json_str]` 原生支持，主方案成立，无需回退。**

- [ ] **Step 2: 写原型脚本**（真机管线，box(40,30,20)+顶面正中 ⌀12 孔 → 逐面 tessellate → matplotlib 面标签+尺寸线 → /tmp/spike_annotate_iso.png）

```python
# .vibecad/spike_annotate.py — Round5 Task0 spike：面标注原型（在 conda env python 中跑）
import os, sys, math
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))
from vibecad.freecad_env import prepare_freecad_import, silence_fd1
prepare_freecad_import()
with silence_fd1():
    import FreeCAD, Part
    doc = FreeCAD.newDocument("spike")
    box = doc.addObject("Part::Box", "Box"); box.Length, box.Width, box.Height = 40, 30, 20
    cyl = doc.addObject("Part::Cylinder", "Cyl"); cyl.Radius, cyl.Height = 6, 40
    cyl.Placement = FreeCAD.Placement(FreeCAD.Vector(20, 15, -10), FreeCAD.Rotation())
    cut = doc.addObject("Part::Cut", "Cut"); cut.Base, cut.Tool = box, cyl
    doc.recompute()
    shape = cut.Shape
    faces = []
    for i, f in enumerate(shape.Faces):
        verts, facets = f.tessellate(0.1)
        c = f.CenterOfMass
        faces.append({"i": i, "verts": [(p.x, p.y, p.z) for p in verts], "facets": facets,
                      "center": (c.x, c.y, c.z), "surface": type(f.Surface).__name__})
import numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
ELEV, AZIM = 25, -60
e, a = math.radians(ELEV), math.radians(AZIM)
cam = np.array([math.cos(e)*math.cos(a), math.cos(e)*math.sin(a), math.sin(e)])
light = np.array([0.35, 0.30, 0.88]); light /= np.linalg.norm(light)
palette = [(0.32,0.55,0.90),(0.36,0.62,0.83),(0.30,0.50,0.95),(0.40,0.58,0.86),(0.34,0.52,0.92),(0.38,0.60,0.88)]
fig = plt.figure(figsize=(5.6, 5.6), dpi=100); ax = fig.add_subplot(111, projection="3d")
all_pts = []
labels = []
for k, fd in enumerate(faces):
    varr = np.asarray(fd["verts"]); all_pts.extend(fd["verts"])
    tris, cols, nsum = [], [], np.zeros(3)
    base = np.array(palette[k % len(palette)])
    for f3 in fd["facets"]:
        a3, b3, c3 = varr[f3[0]], varr[f3[1]], varr[f3[2]]
        n = np.cross(b3 - a3, c3 - a3); nsum += n
        ln = float(np.linalg.norm(n))
        shade = 0.45 + 0.55*abs(float(n @ light)/ln) if ln > 1e-12 else 1.0
        tris.append([fd["verts"][i] for i in f3]); cols.append((*(base*shade), 1.0))
    ax.add_collection3d(Poly3DCollection(tris, facecolors=cols, edgecolor=(0,0,0,0.12), linewidths=0.2))
    ln = float(np.linalg.norm(nsum))
    visible = ln > 1e-9 and float(nsum/ln @ cam) > 0.05
    labels.append((chr(65+k), fd["center"], visible, fd["surface"]))
pts = np.asarray(all_pts)
mins, maxs = pts.min(0), pts.max(0)
ax.set_xlim(mins[0], maxs[0]); ax.set_ylim(mins[1], maxs[1]); ax.set_zlim(mins[2], maxs[2])
ax.set_box_aspect(tuple((maxs-mins) if (maxs-mins).all() else 1))
ax.view_init(ELEV, AZIM); ax.set_axis_off()
for lab, pos, vis, surf in labels:
    if vis:
        ax.text(*pos, lab, fontsize=11, fontweight="bold", ha="center", va="center",
                bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#333", alpha=0.92), zorder=99)
# 尺寸线（包围盒三向）
L, W, H = maxs - mins
m = max(L, W, H) * 0.08
for (p0, p1, txt) in [
    ((mins[0], mins[1]-m, mins[2]), (maxs[0], mins[1]-m, mins[2]), f"L={L:g}"),
    ((maxs[0]+m, mins[1], mins[2]), (maxs[0]+m, maxs[1], mins[2]), f"W={W:g}"),
    ((mins[0]-m, mins[1]-m, mins[2]), (mins[0]-m, mins[1]-m, maxs[2]), f"H={H:g}")]:
    ax.plot(*zip(p0, p1), color="#555", lw=1)
    mid = [(p0[i]+p1[i])/2 for i in range(3)]
    ax.text(*mid, txt, fontsize=8, color="#333",
            bbox=dict(boxstyle="round,pad=0.15", fc="#f5f5f5", ec="none", alpha=0.9))
fig.savefig("/tmp/spike_annotate_iso.png", bbox_inches="tight"); print("WROTE /tmp/spike_annotate_iso.png")
for lab, pos, vis, surf in labels:
    print(lab, surf, "visible" if vis else "hidden", [round(x,1) for x in pos])
```

- [ ] **Step 3: 真机跑 + 人眼看图**

Run: `ENV_PY=$(ls .vibecad-test-runtime/envs/*/bin/python | head -1); "$ENV_PY" .vibecad/spike_annotate.py`，然后 **Read /tmp/spike_annotate_iso.png 亲眼检验**：标签是否清晰、互不重叠、只出现在可见面、尺寸线可读、孔壁面（Cylinder）在 hidden 列表。
Expected: 顶面（带孔平面）标签可见；样式不行就地调（字号/图幅/标签框），把定稿参数（字号 11、size 560×560、palette、可见性阈值 0.05）回填 Task 3。

- [ ] **Step 4: commit** `chore(spike): annotated-render prototype + FastMCP multi-content conclusion`

---

## Task 1：`engine/naming.py` 指纹核心（纯逻辑，快测）

**Files:** Create `src/vibecad/engine/naming.py`, `tests/test_naming.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_naming.py
"""naming：指纹/容差匹配/过期错误/语义名。全部纯逻辑快测（fake 几何对象）。"""
import pytest
from vibecad.engine import naming
from vibecad.engine.naming import LabelExpiredError


class _Vec:
    def __init__(self, x, y, z): self.x, self.y, self.z = x, y, z


class Plane:  # 类名即 surface 判定依据（type(face.Surface).__name__）
    def __init__(self, axis=(0, 0, 1)): self.Axis = _Vec(*axis)


class Cylinder:
    def __init__(self, radius=6.0, axis=(0, 0, 1)):
        self.Radius = radius; self.Axis = _Vec(*axis)


class FakeFace:
    def __init__(self, surface, area, center):
        self.Surface = surface; self.Area = area; self.CenterOfMass = _Vec(*center)


class Line:
    pass


class FakeEdge:
    def __init__(self, curve, length, mid):
        self.Curve = curve; self.Length = length; self.CenterOfMass = _Vec(*mid)


def _top(area=1200.0, center=(20, 15, 20)):
    return FakeFace(Plane((0, 0, 1)), area, center)


def test_face_fingerprint_fields():
    fp = naming.face_fingerprint(_top())
    assert fp["surface"] == "Plane" and fp["area"] == 1200.0
    assert fp["center"] == (20.0, 15.0, 20.0) and fp["axis"] == (0.0, 0.0, 1.0)


def test_face_fingerprint_cylinder_radius():
    fp = naming.face_fingerprint(FakeFace(Cylinder(6.0), 753.98, (20, 15, 10)))
    assert fp["surface"] == "Cylinder" and fp["radius"] == 6.0


def test_match_face_unique_hit():
    fp = naming.face_fingerprint(_top())
    faces = [FakeFace(Plane((0, 0, 1)), 600.0, (20, 15, 0)), _top()]
    assert naming.match_face(fp, faces) == 1


def test_match_face_axis_sign_insensitive():
    fp = naming.face_fingerprint(FakeFace(Plane((0, 0, -1)), 1200.0, (20, 15, 20)))
    assert naming.match_face(fp, [_top()]) == 0  # Plane.Axis 定向不稳，反号视为同面


def test_match_face_expired_when_area_changed():
    fp = naming.face_fingerprint(_top(area=1200.0))
    with pytest.raises(LabelExpiredError):  # 打孔后顶面面积变小 → 过期
        naming.match_face(fp, [_top(area=1086.9)])


def test_match_face_ambiguous_raises():
    fp = naming.face_fingerprint(_top())
    with pytest.raises(LabelExpiredError):
        naming.match_face(fp, [_top(), _top()])


def test_edge_fingerprint_and_match():
    fp = naming.edge_fingerprint(FakeEdge(Line(), 40.0, (20, 0, 0)))
    edges = [FakeEdge(Line(), 30.0, (0, 15, 0)), FakeEdge(Line(), 40.0, (20, 0, 0))]
    assert naming.match_edge(fp, edges) == 1
    with pytest.raises(LabelExpiredError):
        naming.match_edge(fp, edges[:1])


def test_semantic_name_top_bottom():
    bbox = (0, 0, 0, 40, 30, 20)
    assert naming.semantic_name(naming.face_fingerprint(_top()), bbox) == "顶面"
    bot = FakeFace(Plane((0, 0, -1)), 1200.0, (20, 15, 0))
    assert naming.semantic_name(naming.face_fingerprint(bot), bbox) == "底面"
    hole = FakeFace(Cylinder(), 750.0, (20, 15, 10))
    assert naming.semantic_name(naming.face_fingerprint(hole), bbox) is None


def test_face_summary_readable():
    s = naming.face_summary(naming.face_fingerprint(_top()), (0, 0, 0, 40, 30, 20))
    assert "顶面" in s and "平面" in s
    s2 = naming.face_summary(naming.face_fingerprint(FakeFace(Cylinder(6.0), 750.0, (20, 15, 10))),
                             (0, 0, 0, 40, 30, 20))
    assert "圆柱面" in s2 and "6" in s2


def test_face_labels_sequence():
    assert naming.face_labels(3) == ["A", "B", "C"]
    assert naming.face_labels(28)[26] == "AA"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_naming.py -q`
Expected: FAIL（ModuleNotFoundError: vibecad.engine.naming）

- [ ] **Step 3: 实现**

```python
# src/vibecad/engine/naming.py
"""标签注册表的指纹核心：面/边几何指纹 + 容差匹配（解 FreeCAD 重算后索引重排）。
纯逻辑模块——不 import FreeCAD（吃 duck-typed 几何对象），dev venv 可快测。"""
from __future__ import annotations

import string
from typing import Any


class LabelExpiredError(ValueError):
    """标签指代的几何已变更或无法唯一匹配——需重新标注。"""


def face_fingerprint(face: Any) -> dict:
    surface = type(face.Surface).__name__
    c = face.CenterOfMass
    fp: dict[str, Any] = {
        "kind": "Face", "surface": surface, "area": float(face.Area),
        "center": (float(c.x), float(c.y), float(c.z)), "axis": None,
    }
    ax = getattr(face.Surface, "Axis", None)  # Plane 法向 / Cylinder 轴向
    if ax is not None:
        fp["axis"] = (float(ax.x), float(ax.y), float(ax.z))
    if surface == "Cylinder":
        fp["radius"] = float(face.Surface.Radius)
    return fp


def edge_fingerprint(edge: Any) -> dict:
    c = edge.CenterOfMass
    return {"kind": "Edge", "curve": type(edge.Curve).__name__,
            "length": float(edge.Length),
            "midpoint": (float(c.x), float(c.y), float(c.z))}


def _vec_close(a, b, tol: float) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _axis_match(a, b) -> bool:
    if (a is None) != (b is None):
        return False
    if a is None:
        return True
    return _vec_close(a, b, 1e-6) or _vec_close([-x for x in a], b, 1e-6)  # 定向不稳，反号同面


def match_face(fp: dict, faces: list[Any], *, tol: float = 1e-3) -> int:
    """在当前 faces 中找指纹唯一匹配的索引；0 或多个命中 → LabelExpiredError。"""
    hits = []
    for i, f in enumerate(faces):
        cand = face_fingerprint(f)
        if cand["surface"] != fp["surface"]:
            continue
        if abs(cand["area"] - fp["area"]) > max(tol, 1e-3 * abs(fp["area"])):
            continue
        if not _vec_close(cand["center"], fp["center"], tol):
            continue
        if not _axis_match(cand["axis"], fp["axis"]):
            continue
        hits.append(i)
    if len(hits) != 1:
        raise LabelExpiredError(
            f"面标签无法唯一匹配当前几何（命中 {len(hits)} 个）——几何可能已变更，"
            "请重新调用 render_part(annotate='faces') 获取最新标注")
    return hits[0]


def match_edge(fp: dict, edges: list[Any], *, tol: float = 1e-3) -> int:
    hits = []
    for i, e in enumerate(edges):
        cand = edge_fingerprint(e)
        if cand["curve"] != fp["curve"]:
            continue
        if abs(cand["length"] - fp["length"]) > max(tol, 1e-3 * abs(fp["length"])):
            continue
        if not _vec_close(cand["midpoint"], fp["midpoint"], tol):
            continue
        hits.append(i)
    if len(hits) != 1:
        raise LabelExpiredError(
            f"边标签无法唯一匹配当前几何（命中 {len(hits)} 个）——几何可能已变更，"
            "请重新调用 render_part(annotate='edges') 获取最新标注")
    return hits[0]


_SIDES = {0: ("左面", "右面"), 1: ("前面", "后面"), 2: ("底面", "顶面")}


def semantic_name(fp: dict, bbox: tuple) -> str | None:
    """轴对齐平面且贴包围盒边界 → 顶/底/前/后/左/右面；其余 None。
    bbox=(xmin,ymin,zmin,xmax,ymax,zmax)。"""
    if fp["surface"] != "Plane" or not fp["axis"]:
        return None
    ax = fp["axis"]
    i = max(range(3), key=lambda k: abs(ax[k]))
    if abs(ax[i]) < 0.99:
        return None
    lo, hi = bbox[i], bbox[i + 3]
    span = max(hi - lo, 1e-9)
    c = fp["center"][i]
    if abs(c - hi) <= 1e-6 + 1e-3 * span:
        return _SIDES[i][1]
    if abs(c - lo) <= 1e-6 + 1e-3 * span:
        return _SIDES[i][0]
    return None


def face_summary(fp: dict, bbox: tuple) -> str:
    """指纹 → 给 AI/用户读的一行描述（标签表内容）。"""
    sem = semantic_name(fp, bbox)
    if fp["surface"] == "Plane":
        head = f"{sem}·平面" if sem else "平面"
        return f"{head} 面积{fp['area']:.0f}mm² 中心{tuple(round(v, 1) for v in fp['center'])}"
    if fp["surface"] == "Cylinder":
        return f"圆柱面 r={fp.get('radius', 0):g}mm 中心{tuple(round(v, 1) for v in fp['center'])}"
    return f"{fp['surface']} 面积{fp['area']:.0f}mm²"


def edge_summary(fp: dict) -> str:
    kind = {"Line": "直线边", "Circle": "圆边"}.get(fp["curve"], fp["curve"])
    return f"{kind} 长{fp['length']:.1f}mm 中点{tuple(round(v, 1) for v in fp['midpoint'])}"


def face_labels(n: int) -> list[str]:
    """A..Z, AA, AB…（面标签序列）。"""
    out = []
    for i in range(n):
        s, k = "", i
        while True:
            s = string.ascii_uppercase[k % 26] + s
            k = k // 26 - 1
            if k < 0:
                break
        out.append(s)
    return out


def edge_labels(n: int) -> list[str]:
    return [f"E{i + 1}" for i in range(n)]
```

- [ ] **Step 4: 跑测试至绿 + ruff**

Run: `uv run pytest tests/test_naming.py -q && uv run ruff check .`
Expected: 全 PASS

- [ ] **Step 5: commit** `feat(naming): face/edge fingerprints + tolerant matching + LabelExpiredError`

---

## Task 2：Session 标签注册表

**Files:** Modify `src/vibecad/engine/session.py`（加在类尾部）, Create 测试加进 `tests/test_naming.py`（Session 部分用 fake）

- [ ] **Step 1: 失败测试**（追加到 tests/test_naming.py）

```python
# --- Session 注册表（fake shape，不碰 FreeCAD）---
from vibecad.engine.session import Session


class _FakeShape:
    def __init__(self, faces=(), edges=()):
        self.Faces = list(faces); self.Edges = list(edges)


def _session_with_shape(monkeypatch, shape):
    s = Session.__new__(Session)  # 绕开 FreeCAD 初始化
    s._labels = None
    monkeypatch.setattr(Session, "get_result_shape", lambda self: shape, raising=False)
    return s


def test_resolve_face_roundtrip(monkeypatch):
    top = _top()
    s = _session_with_shape(monkeypatch, _FakeShape(faces=[_top(area=600.0, center=(1, 1, 1)), top]))
    s.set_labels({"A": naming.face_fingerprint(top)}, {})
    assert s.resolve_face("A") == 1


def test_resolve_face_unknown_label(monkeypatch):
    s = _session_with_shape(monkeypatch, _FakeShape())
    s.set_labels({}, {})
    with pytest.raises(LabelExpiredError):
        s.resolve_face("Z")


def test_resolve_without_labels_raises(monkeypatch):
    s = _session_with_shape(monkeypatch, _FakeShape())
    with pytest.raises(LabelExpiredError):
        s.resolve_face("A")


def test_resolve_edge_roundtrip(monkeypatch):
    e = FakeEdge(Line(), 40.0, (20, 0, 0))
    s = _session_with_shape(monkeypatch, _FakeShape(edges=[e]))
    s.set_labels({}, {"E1": naming.edge_fingerprint(e)})
    assert s.resolve_edge("E1") == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_naming.py -q -k resolve`
Expected: FAIL（Session 无 set_labels）

- [ ] **Step 3: 实现**（session.py 的 Session 类追加；`__init__` 中加 `self._labels: dict | None = None`）

```python
    # ---- Round 5：标签注册表（标注快照 → 指纹解析）----
    def set_labels(self, faces: dict, edges: dict) -> None:
        """存最近一次标注快照：{label: fingerprint}。"""
        self._labels = {"faces": dict(faces), "edges": dict(edges)}

    def resolve_face(self, label: str) -> int:
        """面标签 → 当前结果形状的面索引；快照缺失/标签未知/匹配失败均抛 LabelExpiredError。"""
        from vibecad.engine import naming  # noqa: PLC0415
        if not self._labels or label not in self._labels["faces"]:
            raise naming.LabelExpiredError(
                f"未知面标签 {label!r}——请先调用 render_part(annotate='faces') 获取标注")
        return naming.match_face(self._labels["faces"][label], self.get_result_shape().Faces)

    def resolve_edge(self, label: str) -> int:
        from vibecad.engine import naming  # noqa: PLC0415
        if not self._labels or label not in self._labels["edges"]:
            raise naming.LabelExpiredError(
                f"未知边标签 {label!r}——请先调用 render_part(annotate='edges') 获取标注")
        return naming.match_edge(self._labels["edges"][label], self.get_result_shape().Edges)

    def get_result_object(self):
        """当前结果文档对象（最后一个带有效 Shape 的对象）——特征工具的 Base。"""
        for obj in reversed(self.doc.Objects):
            shape = getattr(obj, "Shape", None)
            if shape is not None and not shape.isNull():
                return obj
        raise RuntimeError("文档中没有可用零件——请先创建几何（add_box / add_cylinder）")
```

注意：若 session.py 已有等价的"取结果对象"私有方法（`get_result_shape` 的实现内部），让 `get_result_shape` 改为 `return self.get_result_object().Shape` 消重复（DRY），保持原行为与报错语义。

- [ ] **Step 4: 跑测试至绿 + ruff**

Run: `uv run pytest tests/test_naming.py -q && uv run ruff check . && uv run pytest -q`
Expected: 全 PASS（含既有套件无回归）

- [ ] **Step 5: commit** `feat(session): label registry (set_labels/resolve_face/resolve_edge) + get_result_object`

---

## Task 3：`feedback/annotate.py` 标注绘制

**Files:** Create `src/vibecad/feedback/annotate.py`, `tests/test_annotate.py`; Modify `src/vibecad/feedback/__init__.py`（re-export annotate）

- [ ] **Step 1: 失败测试**

```python
# tests/test_annotate.py
"""annotate：相机方向/网格法向/标注 PNG。纯函数快测（不碰 FreeCAD）。"""
import math

import pytest
from vibecad.feedback import annotate

_TET_V = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
_TET_F = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]


def test_camera_direction_iso_unit():
    d = annotate.camera_direction("iso")
    assert abs(math.dist(d, (0, 0, 0)) - 1.0) < 1e-9
    assert d[2] > 0  # iso 从上方看


def test_camera_direction_top_is_up():
    d = annotate.camera_direction("top")
    assert d[2] > 0.99


def test_camera_direction_invalid():
    with pytest.raises(ValueError):
        annotate.camera_direction("bogus")


def test_mesh_normal_z_face():
    verts = [(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]
    n = annotate.mesh_normal(verts, [(0, 1, 2), (0, 2, 3)])
    assert abs(n[2] - 1.0) < 1e-9  # 朝 +Z


def test_mesh_normal_degenerate_returns_zero():
    assert annotate.mesh_normal([(0, 0, 0)] * 3, [(0, 1, 2)]) == (0.0, 0.0, 0.0)


def test_annotated_png_smoke():
    png = annotate.annotated_png(
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[{"label": "A", "pos": (3, 3, 0), "visible": True},
                     {"label": "B", "pos": (0, 0, 5), "visible": False}],
        edge_labels=[{"label": "E1", "pos": (5, 0, 0),
                      "polyline": [(0, 0, 0), (10, 0, 0)]}],
        dims={"L": 10, "W": 10, "H": 10, "bbox": (0, 0, 0, 10, 10, 10)},
        view="iso")
    assert png.startswith(b"\x89PNG") and len(png) > 1000


def test_annotated_png_empty_mesh_raises():
    with pytest.raises(ValueError):
        annotate.annotated_png(face_meshes=[], face_labels=[], edge_labels=[], view="iso")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_annotate.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**（样式参数以 Task 0 spike 定稿为准；以下为基准实现）

> **Task 0 spike 样式定稿（2026-06-09，已人眼检验两版）**：①面标签锚点用**该面最大三角形的质心**（不是 `CenterOfMass`——带孔面质心落在孔上会压住孔），`render_annotated` 内从 face mesh 算；②轴 limits 三向各留 **14% pad**（防尺寸线文字被视锥裁剪）；③**尺寸文本也要 `zorder=99`**（否则被零件棱线穿过）；④字号 11/白底圆角框/可见性阈值 0.05/palette 6 色循环维持基准实现不变。spike 实测可见性判定全对（左/后/底/孔壁 hidden，前/顶/右 visible）。

```python
# src/vibecad/feedback/annotate.py
"""标注渲染：逐面网格 + 面/边标签 + 包围盒尺寸线（matplotlib Agg，函数内 import）。
annotated_png 为纯函数（吃普通 list 数据）；render_annotated 才碰 FreeCAD Shape。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.feedback.render import _VIEWS

_PALETTE = [(0.32, 0.55, 0.90), (0.36, 0.62, 0.83), (0.30, 0.50, 0.95),
            (0.40, 0.58, 0.86), (0.34, 0.52, 0.92), (0.38, 0.60, 0.88)]
_VIS_DOT = 0.05  # 面法向·相机方向 > 阈值才视为可见（spike 定稿）


def camera_direction(view: str) -> tuple[float, float, float]:
    """matplotlib view_init(elev, azim) 对应的单位相机方向（指向相机）。"""
    if view not in _VIEWS:
        raise ValueError(f"view 必须是 {sorted(_VIEWS)} 之一（得到 {view!r}）")
    e, a = (math.radians(d) for d in _VIEWS[view])
    return (math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))


def mesh_normal(verts: list, facets: list) -> tuple[float, float, float]:
    """三角网面积加权平均法向（单位向量；退化网格返回零向量）。"""
    sx = sy = sz = 0.0
    for f in facets:
        ax, ay, az = verts[f[0]]; bx, by, bz = verts[f[1]]; cx, cy, cz = verts[f[2]]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        sx += uy * vz - uz * vy; sy += uz * vx - ux * vz; sz += ux * vy - uy * vx
    ln = math.sqrt(sx * sx + sy * sy + sz * sz)
    if ln < 1e-12:
        return (0.0, 0.0, 0.0)
    return (sx / ln, sy / ln, sz / ln)


def annotated_png(*, face_meshes: list[dict], face_labels: list[dict],
                  edge_labels: list[dict], view: str = "iso",
                  size: tuple[int, int] = (560, 560), dims: dict | None = None) -> bytes:
    """逐面网格 + 标签 → PNG bytes。纯 matplotlib，不碰 FreeCAD。"""
    camera_direction(view)  # 校验 view
    if not face_meshes:
        raise ValueError("空网格：无任何面可渲染（可能是 tessellate 失败或形状退化）")
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: PLC0415

    light = np.array([0.35, 0.30, 0.88]); light = light / np.linalg.norm(light)
    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    try:
        ax = fig.add_subplot(111, projection="3d")
        all_pts: list = []
        for k, fm in enumerate(face_meshes):
            verts, facets = fm["verts"], fm["facets"]
            if not verts or not facets:
                continue
            all_pts.extend(verts)
            varr = np.asarray(verts, dtype=float)
            base = np.array(_PALETTE[k % len(_PALETTE)])
            tris, cols = [], []
            for f in facets:
                a, b, c = varr[f[0]], varr[f[1]], varr[f[2]]
                n = np.cross(b - a, c - a)
                ln = float(np.linalg.norm(n))
                shade = 0.45 + 0.55 * abs(float(n @ light) / ln) if ln > 1e-12 else 1.0
                tris.append([verts[i] for i in f]); cols.append((*(base * shade), 1.0))
            ax.add_collection3d(Poly3DCollection(
                tris, facecolors=cols, edgecolor=(0, 0, 0, 0.12), linewidths=0.2))
        if not all_pts:
            raise ValueError("空网格：所有面 tessellation 均为空")
        pts = np.asarray(all_pts)
        mins, maxs = pts.min(0), pts.max(0)
        ax.set_xlim(mins[0], maxs[0]); ax.set_ylim(mins[1], maxs[1]); ax.set_zlim(mins[2], maxs[2])
        ax.set_box_aspect(tuple((maxs[i] - mins[i]) or 1 for i in range(3)))
        ax.view_init(*_VIEWS[view]); ax.set_axis_off()
        for el in edge_labels:
            poly = el.get("polyline") or []
            if len(poly) >= 2:
                ax.plot(*zip(*poly), color="#e07020", lw=2.0, zorder=50)
            ax.text(*el["pos"], el["label"], fontsize=9, color="#7a3500", fontweight="bold",
                    ha="center", va="center", zorder=99,
                    bbox=dict(boxstyle="round,pad=0.2", fc="#fff3e6", ec="#e07020", alpha=0.95))
        for fl in face_labels:
            if not fl.get("visible"):
                continue
            ax.text(*fl["pos"], fl["label"], fontsize=11, fontweight="bold",
                    ha="center", va="center", zorder=99,
                    bbox=dict(boxstyle="round,pad=0.25", fc="white", ec="#333", alpha=0.92))
        if dims:
            x0, y0, z0, x1, y1, z1 = dims["bbox"]
            m = max(x1 - x0, y1 - y0, z1 - z0) * 0.08
            for p0, p1, txt in (
                    ((x0, y0 - m, z0), (x1, y0 - m, z0), f"L={dims['L']:g}"),
                    ((x1 + m, y0, z0), (x1 + m, y1, z0), f"W={dims['W']:g}"),
                    ((x0 - m, y0 - m, z0), (x0 - m, y0 - m, z1), f"H={dims['H']:g}")):
                ax.plot(*zip(p0, p1), color="#555", lw=1)
                mid = tuple((p0[i] + p1[i]) / 2 for i in range(3))
                ax.text(*mid, txt, fontsize=8, color="#333",
                        bbox=dict(boxstyle="round,pad=0.15", fc="#f5f5f5", ec="none", alpha=0.9))
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
    finally:
        plt.close(fig)
    return buf.getvalue()


def render_annotated(shape: Any, *, mode: str = "faces", edges_of: int | None = None,
                     view: str = "iso") -> tuple[bytes, dict, dict, dict]:
    """FreeCAD Shape → (png, labels_table, faces_registry, edges_registry)。
    mode='faces'：全部面标注 + 尺寸线；mode='edges'：边标注（edges_of=面索引则只标该面的边）。"""
    from vibecad.engine import naming  # noqa: PLC0415
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    if mode not in ("faces", "edges"):
        raise ValueError(f"annotate 必须是 'faces' 或 'edges'（得到 {mode!r}）")
    cam = camera_direction(view)
    with silence_fd1():
        bb = shape.BoundBox
        bbox = (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)
        face_meshes, face_info = [], []
        for f in shape.Faces:
            verts, facets = f.tessellate(0.1)
            pts = [(p.x, p.y, p.z) for p in verts]
            face_meshes.append({"verts": pts, "facets": facets})
            c = f.CenterOfMass
            face_info.append({"fp": naming.face_fingerprint(f), "center": (c.x, c.y, c.z),
                              "normal": mesh_normal(pts, facets)})
        if not any(fm["verts"] for fm in face_meshes):
            raise RuntimeError("几何断言失败：形状无法镶嵌为网格（空 tessellation）")
        edge_objs = list(shape.Faces[edges_of].Edges) if (mode == "edges" and edges_of is not None) \
            else (list(shape.Edges) if mode == "edges" else [])
        edge_info = []
        for e in edge_objs:
            mid = e.CenterOfMass
            edge_info.append({"fp": naming.edge_fingerprint(e),
                              "pos": (mid.x, mid.y, mid.z),
                              "polyline": [(p.x, p.y, p.z) for p in e.discretize(24)]})
    table: dict[str, str] = {}
    faces_reg: dict[str, dict] = {}
    face_labels = []
    if mode == "faces":
        names = naming.face_labels(len(face_info))
        for lab, info in zip(names, face_info):
            visible = (sum(a * b for a, b in zip(info["normal"], cam)) > _VIS_DOT)
            faces_reg[lab] = info["fp"]
            face_labels.append({"label": lab, "pos": info["center"], "visible": visible})
            note = "" if visible else "（当前视角不可见，换 top/front/right 试）"
            table[lab] = naming.face_summary(info["fp"], bbox) + note
    edges_reg: dict[str, dict] = {}
    edge_labels = []
    if mode == "edges":
        names = naming.edge_labels(len(edge_info))
        for lab, info in zip(names, edge_info):
            edges_reg[lab] = info["fp"]
            edge_labels.append({"label": lab, "pos": info["pos"], "polyline": info["polyline"]})
            table[lab] = naming.edge_summary(info["fp"])
    dims = {"L": bbox[3] - bbox[0], "W": bbox[4] - bbox[1], "H": bbox[5] - bbox[2],
            "bbox": bbox} if mode == "faces" else None
    png = annotated_png(face_meshes=face_meshes, face_labels=face_labels,
                        edge_labels=edge_labels, view=view, dims=dims)
    return png, table, faces_reg, edges_reg
```

并在 `src/vibecad/feedback/__init__.py` 的 import 元组中加 `annotate,  # noqa: F401`。

- [ ] **Step 4: 跑测试至绿 + ruff**

Run: `uv run pytest tests/test_annotate.py tests/test_naming.py -q && uv run ruff check .`
Expected: 全 PASS

- [ ] **Step 5: commit** `feat(annotate): per-face annotated render (labels + visibility + dims) pure-function core`

---

## Task 4：`tools/features.py` 特征工具

**Files:** Create `src/vibecad/tools/features.py`, `tests/test_tools_features.py`（快测部分）

- [ ] **Step 1: 失败测试（参数校验快测，不碰 FreeCAD）**

```python
# tests/test_tools_features.py
"""features：参数校验快测（真机 happy-path 在 @slow 部分，Task 6 补）。"""
import pytest
from vibecad.tools import features


class _NoopSession:
    pass


@pytest.mark.parametrize("kwargs,msg", [
    ({"face": "", "diameter": 6}, "face"),
    ({"face": "A", "diameter": 0}, "diameter"),
    ({"face": "A", "diameter": -2}, "diameter"),
    ({"face": "A", "diameter": 6, "depth": 0}, "depth"),
    ({"face": "A", "diameter": 6, "offset": [1]}, "offset"),
    ({"face": "A", "diameter": 6, "offset": ["a", "b"]}, "offset"),
])
def test_add_hole_validation(kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        features.add_hole(_NoopSession(), **kwargs)


@pytest.mark.parametrize("fn,kwargs,msg", [
    (features.fillet_edges, {"edges": [], "radius": 2}, "edges"),
    (features.fillet_edges, {"edges": ["E1"], "radius": 0}, "radius"),
    (features.fillet_edges, {"edges": "E1", "radius": 2}, "edges"),
    (features.chamfer_edges, {"edges": [], "size": 1}, "edges"),
    (features.chamfer_edges, {"edges": ["E1"], "size": -1}, "size"),
])
def test_edge_features_validation(fn, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        fn(_NoopSession(), **kwargs)


def test_inplane_axes_orthonormal():
    e1, e2 = features._inplane_axes((0.0, 0.0, 1.0))
    assert abs(sum(a * b for a, b in zip(e1, e2))) < 1e-9
    assert abs(sum(a * a for a in e1) - 1) < 1e-9 and abs(e1[2]) < 1e-9


def test_inplane_axes_arbitrary_normal():
    import math
    n = (1 / math.sqrt(3),) * 3
    e1, e2 = features._inplane_axes(n)
    for e in (e1, e2):
        assert abs(sum(a * b for a, b in zip(e, n))) < 1e-9  # 与法向正交
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tools_features.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# src/vibecad/tools/features.py
"""面/边级特征工具（Round 5）：消费标签注册表的指代（"A 面打孔"、"E3 倒角"）。
纪律：参数校验 → 标签指纹解析（过期即 LabelExpiredError）→ 事务 → 几何断言 → 结构化 dict。"""
from __future__ import annotations

import math
from typing import Any

from vibecad.engine.session import Session


def _inplane_axes(n) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """法向 n → 面内正交单位基 (e1, e2)：取与 n 最不平行的全局轴投影（offset 方向直观）。"""
    nx, ny, nz = float(n[0]), float(n[1]), float(n[2])
    axes = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    g = min(axes, key=lambda a: abs(a[0] * nx + a[1] * ny + a[2] * nz))
    d = g[0] * nx + g[1] * ny + g[2] * nz
    e1 = (g[0] - d * nx, g[1] - d * ny, g[2] - d * nz)
    ln = math.sqrt(sum(c * c for c in e1))
    e1 = (e1[0] / ln, e1[1] / ln, e1[2] / ln)
    e2 = (ny * e1[2] - nz * e1[1], nz * e1[0] - nx * e1[2], nx * e1[1] - ny * e1[0])
    return e1, e2


def _outward_normal(shape: Any, face: Any):
    """面的单位外法向（normalAt 不保证定向——用实体内点探针校正）。返回 FreeCAD.Vector。"""
    u0, u1, v0, v1 = face.ParameterRange
    n = face.normalAt((u0 + u1) / 2, (v0 + v1) / 2)
    n.normalize()
    solid = shape.Solids[0] if getattr(shape, "Solids", None) else shape
    probe = face.CenterOfMass + n * 0.01
    if solid.isInside(probe, 1e-6, False):
        n = -n
    return n


def add_hole(session: Session, face: str, diameter: float,
             depth: float | None = None, offset=(0.0, 0.0)) -> dict[str, Any]:
    """在指定面（标签）打圆孔：depth=None 通孔；offset 为面内毫米坐标（原点=面心）。"""
    if not face or not isinstance(face, str):
        raise ValueError("face 必须是非空字符串（面标签，如 'A'）")
    if diameter <= 0:
        raise ValueError(f"diameter 必须 > 0（得到 {diameter}）")
    if depth is not None and depth <= 0:
        raise ValueError(f"depth 必须 > 0 或省略表示通孔（得到 {depth}）")
    if (not isinstance(offset, (list, tuple)) or len(offset) != 2
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                       and math.isfinite(c) for c in offset)):
        raise ValueError(f"offset 必须是 2 个有限数字 (u, v)（得到 {offset!r}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction("add_hole"):
        with silence_fd1():
            import FreeCAD  # noqa: PLC0415
            idx = session.resolve_face(face)
            base_obj = session.get_result_object()
            shape = base_obj.Shape
            face_obj = shape.Faces[idx]
            surface = type(face_obj.Surface).__name__
            if surface != "Plane":
                raise ValueError(f"标签 {face} 是 {surface}，只能在平面上打孔")
            n = _outward_normal(shape, face_obj)
            e1, e2 = _inplane_axes((n.x, n.y, n.z))
            c = face_obj.CenterOfMass
            lift = 0.5  # 从面外 0.5mm 起钻，避免共面布尔
            bx = c.x + e1[0] * offset[0] + e2[0] * offset[1] + n.x * lift
            by = c.y + e1[1] * offset[0] + e2[1] * offset[1] + n.y * lift
            bz = c.z + e1[2] * offset[0] + e2[2] * offset[1] + n.z * lift
            length = (depth + lift) if depth is not None \
                else shape.BoundBox.DiagonalLength + 2 * lift
            base_vol = shape.Volume
            cyl = session.doc.addObject("Part::Cylinder", "HoleTool")
            cyl.Radius, cyl.Height = diameter / 2.0, length
            cyl.Placement = FreeCAD.Placement(
                FreeCAD.Vector(bx, by, bz),
                FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), FreeCAD.Vector(-n.x, -n.y, -n.z)))
            cut = session.doc.addObject("Part::Cut", "Hole")
            cut.Base, cut.Tool = base_obj, cyl
            session.doc.recompute()
            session.assert_valid_solid(cut.Shape)
            if cut.Shape.Volume >= base_vol - 1e-6:
                raise RuntimeError(
                    f"几何断言失败：打孔未移除任何材料（base={base_vol:.3f}, "
                    f"cut={cut.Shape.Volume:.3f}）——offset/depth 可能让孔落在零件之外")
            result = {"ok": True, "name": cut.Name, "volume": cut.Shape.Volume,
                      "hole": {"face": face, "diameter": diameter,
                               "depth": depth if depth is not None else "through",
                               "offset": list(offset)},
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='faces') 查看最新标注"}
    return result


def _edge_feature(session: Session, edges, value: float, *, kind: str,
                  type_name: str, obj_label: str, value_field: str) -> dict[str, Any]:
    if not isinstance(edges, (list, tuple)) or not edges \
            or not all(isinstance(e, str) and e for e in edges):
        raise ValueError(f"edges 必须是非空字符串列表（边标签，如 ['E1','E2']）（得到 {edges!r}）")
    if value <= 0:
        raise ValueError(f"{value_field} 必须 > 0（得到 {value}）")
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415
    with session._transaction(kind):
        with silence_fd1():
            idxs = [session.resolve_edge(e) for e in edges]
            base_obj = session.get_result_object()
            faces_before = len(base_obj.Shape.Faces)
            vol_before = base_obj.Shape.Volume
            feat = session.doc.addObject(type_name, obj_label)
            feat.Base = base_obj
            feat.Edges = [(i + 1, value, value) for i in idxs]  # 1-based (idx, r1, r2)
            session.doc.recompute()
            session.assert_valid_solid(feat.Shape)
            if len(feat.Shape.Faces) <= faces_before:
                raise RuntimeError(
                    f"几何断言失败：{kind} 未产生新面（{faces_before} → "
                    f"{len(feat.Shape.Faces)}）——OCCT 可能对所选边失败：{edges}")
            if abs(feat.Shape.Volume - vol_before) < 1e-9:
                raise RuntimeError(f"几何断言失败：{kind} 后体积无变化——所选边可能无效：{edges}")
            result = {"ok": True, "name": feat.Name, "volume": feat.Shape.Volume,
                      kind: {value_field: value, "edges": list(edges)},
                      "labels_stale": True,
                      "hint": "几何已变更，调用 render_part(annotate='edges') 查看最新标注"}
    return result


def fillet_edges(session: Session, edges, radius: float) -> dict[str, Any]:
    """对指定边（标签列表）做圆角。"""
    return _edge_feature(session, edges, radius, kind="fillet",
                         type_name="Part::Fillet", obj_label="Fillet", value_field="radius")


def chamfer_edges(session: Session, edges, size: float) -> dict[str, Any]:
    """对指定边（标签列表）做倒角。"""
    return _edge_feature(session, edges, size, kind="chamfer",
                         type_name="Part::Chamfer", obj_label="Chamfer", value_field="size")
```

- [ ] **Step 4: 跑测试至绿 + ruff**

Run: `uv run pytest tests/test_tools_features.py -q && uv run ruff check .`
Expected: 全 PASS（校验在 resolve/import FreeCAD 之前，_NoopSession 不会被触碰）

- [ ] **Step 5: commit** `feat(features): add_hole/fillet_edges/chamfer_edges consuming label registry`

---

## Task 5：server 集成（render_part annotate + 3 新工具 + 握手回归）

**Files:** Modify `src/vibecad/server.py`; Create `tests/test_server_round5.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_server_round5.py
"""server Round5：annotate 渲染双内容、特征工具委托与失败结构化、握手纯净回归。"""
import json
import sys

import pytest


@pytest.fixture()
def server(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: None)
    return srv


def test_render_part_annotate_returns_image_and_table(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape: pass
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(
        server._annotate, "render_annotated",
        lambda shape, mode, edges_of, view: (b"\x89PNG fake", {"A": "顶面"}, {"A": {}}, {}))
    recorded = {}
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges: recorded.update(f=faces, e=edges))
    out = server.render_part(view="iso", annotate="faces")
    assert isinstance(out, list) and isinstance(out[0], Image)
    table = json.loads(out[1])
    assert table["labels"]["A"] == "顶面" and recorded["f"] == {"A": {}}


def test_render_part_annotate_invalid_value(server, monkeypatch):
    class _Shape: pass
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    out = server.render_part(view="iso", annotate="bogus")
    assert out["ok"] is False


def test_add_hole_delegates(server, monkeypatch):
    monkeypatch.setattr(server._features, "add_hole",
                        lambda session, face, diameter, depth, offset:
                        {"ok": True, "face": face, "diameter": diameter})
    out = server.add_hole(face="A", diameter=8)
    assert out["ok"] is True and out["face"] == "A"


def test_add_hole_label_expired_structured(server, monkeypatch):
    from vibecad.engine.naming import LabelExpiredError

    def _boom(session, face, diameter, depth, offset):
        raise LabelExpiredError("标签 A 已过期")
    monkeypatch.setattr(server._features, "add_hole", _boom)
    out = server.add_hole(face="A", diameter=8)
    assert out["ok"] is False and "过期" in out["message"]


def test_fillet_and_chamfer_delegate(server, monkeypatch):
    monkeypatch.setattr(server._features, "fillet_edges",
                        lambda session, edges, radius: {"ok": True, "edges": edges})
    monkeypatch.setattr(server._features, "chamfer_edges",
                        lambda session, edges, size: {"ok": True, "edges": edges})
    assert server.fillet_edges(edges=["E1"], radius=2)["ok"] is True
    assert server.chamfer_edges(edges=["E2"], size=1)["ok"] is True


def test_handshake_purity_unchanged():
    for mod in ("FreeCAD", "matplotlib"):
        sys.modules.pop(mod, None)
    import importlib

    import vibecad.server as srv
    importlib.reload(srv)
    assert "FreeCAD" not in sys.modules and "matplotlib" not in sys.modules
```

注：既有 server 测试若用其他方式取工具函数（如 `.fn`），保持一致；以仓库现状为准对齐取用方式。

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_server_round5.py -q`
Expected: FAIL（render_part 无 annotate 参数 / add_hole 不存在）

- [ ] **Step 3: 实现**（server.py：顶部 import 区加 `from vibecad.engine import naming as _naming`、`from vibecad.feedback import annotate as _annotate`、`from vibecad.tools import features as _features`；`import json`）

```python
@mcp.tool()
def render_part(view: str = "iso", annotate: str | None = None,
                edges_of: str | None = None) -> Any:
    """渲染当前零件 PNG（view: iso|front|top|right|back）。
    annotate='faces'：面标注图+标签表+尺寸线（之后可用面标签如 'A' 调 add_hole）；
    annotate='edges'：边标注图（edges_of='A' 只标 A 面的边；之后可调 fillet_edges/chamfer_edges）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        with _silence_fd1():
            shape = _session.get_result_shape()
        if annotate is None:
            png = _render.render_png(shape, view=view)
            return Image(data=png, format="png")
        ef_idx = _session.resolve_face(edges_of) if edges_of else None
        png, table, faces_reg, edges_reg = _annotate.render_annotated(
            shape, mode=annotate, edges_of=ef_idx, view=view)
        _session.set_labels(faces_reg, edges_reg)
        return [Image(data=png, format="png"),
                json.dumps({"ok": True, "labels": table}, ensure_ascii=False)]
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"渲染失败：{exc}"}


@mcp.tool()
def add_hole(face: str, diameter: float, depth: float | None = None,
             offset: list[float] | None = None) -> dict[str, Any]:
    """在指定面打圆孔（face=面标签，来自 render_part(annotate='faces')）。
    depth 省略=通孔；offset=[u,v] 面内毫米偏移（省略=面正中）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        return _features.add_hole(_session, face, diameter,
                                  depth, tuple(offset) if offset is not None else (0.0, 0.0))
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"打孔失败：{exc}"}


@mcp.tool()
def fillet_edges(edges: list[str], radius: float) -> dict[str, Any]:
    """对边标签列表做圆角（标签来自 render_part(annotate='edges')）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        return _features.fillet_edges(_session, edges, radius)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"圆角失败：{exc}"}


@mcp.tool()
def chamfer_edges(edges: list[str], size: float) -> dict[str, Any]:
    """对边标签列表做倒角（标签来自 render_part(annotate='edges')）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        return _features.chamfer_edges(_session, edges, size)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"倒角失败：{exc}"}
```

> 若 Task 0 验证 FastMCP 不支持混合 list 返回：render_part 改回只返回 Image，新增只读工具 `get_labels() -> dict`（返回 `_session` 最近标签表——需要 Session 顺带存 table），并同步修改 Step 1 测试。

- [ ] **Step 4: 跑全套至绿 + ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: 全 PASS（含握手纯净回归——annotate/naming/features 模块级不得 import FreeCAD/matplotlib）

- [ ] **Step 5: commit** `feat(server): annotated render (image+labels) + add_hole/fillet_edges/chamfer_edges tools`

---

## Task 6：真机慢测 + 黑盒标注闭环

**Files:** Modify `tests/test_tools_features.py`（追加 @slow）, `tests/test_runtime_integration.py`(追加 1 条), Create `.vibecad/blackbox_annotate.py`

- [ ] **Step 1: 慢测试**（追加到 test_tools_features.py；沿用 conftest `runtime_env` fixture + env python 子进程范式，子进程脚本经 `status._PREP` 注入 + `sys.path.insert(0, <repo>/src)`，参考 tests/test_tools_modeling.py 既有 slow 测试的结构）

```python
# --- @slow 真机：标注→指代→特征 全链路 ---
import os
import subprocess
import sys
import textwrap

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


def _run_in_env(runtime_env, code: str) -> str:
    """在 conda env python 中跑代码片段（最新源码），返回 stdout。"""
    full = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {_SRC!r})
        from vibecad.freecad_env import prepare_freecad_import
        prepare_freecad_import()
        {textwrap.indent(textwrap.dedent(code), '        ').strip()}
    """)
    proc = subprocess.run([runtime_env["python"], "-c", full],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    return proc.stdout


@pytest.mark.slow
def test_annotate_then_add_hole_by_label(runtime_env):
    out = _run_in_env(runtime_env, """
        import math
        from vibecad.engine.session import Session
        from vibecad.feedback import annotate
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "r5")
        modeling.add_box(s, 40, 30, 20)
        png, table, faces_reg, edges_reg = annotate.render_annotated(
            s.get_result_shape(), mode="faces", view="iso")
        assert png.startswith(b"\\x89PNG") and len(png) > 2000
        s.set_labels(faces_reg, edges_reg)
        top = next(lab for lab, desc in table.items() if "顶面" in desc)
        r = features.add_hole(s, top, diameter=8)
        expect = 40*30*20 - math.pi * 16 * 20
        assert abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
        print("HOLE_BY_LABEL_OK", top, round(r["volume"], 1))
    """)
    assert "HOLE_BY_LABEL_OK" in out


@pytest.mark.slow
def test_stale_label_raises_unchanged_face_still_resolves(runtime_env):
    out = _run_in_env(runtime_env, """
        from vibecad.engine.naming import LabelExpiredError
        from vibecad.engine.session import Session
        from vibecad.feedback import annotate
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "stale")
        modeling.add_box(s, 40, 30, 20)
        png, table, faces_reg, _ = annotate.render_annotated(
            s.get_result_shape(), mode="faces", view="iso")
        s.set_labels(faces_reg, {})
        top = next(lab for lab, d in table.items() if "顶面" in d)
        bottom = next(lab for lab, d in table.items() if "底面" in d)
        features.add_hole(s, top, diameter=8)   # 顶面打孔 → 顶面面积变了
        try:
            s.resolve_face(top)
            raise SystemExit("EXPECTED LabelExpiredError for top")
        except LabelExpiredError:
            print("TOP_EXPIRED_OK")
        idx = s.resolve_face(bottom)             # 底面没动 → 仍可指
        print("BOTTOM_STILL_OK", idx)
    """)
    assert "TOP_EXPIRED_OK" in out and "BOTTOM_STILL_OK" in out


@pytest.mark.slow
def test_fillet_and_chamfer_by_edge_labels(runtime_env):
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import annotate
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "fil")
        modeling.add_box(s, 30, 30, 30)
        png, table, _, edges_reg = annotate.render_annotated(
            s.get_result_shape(), mode="edges", view="iso")
        s.set_labels({}, edges_reg)
        shape0 = s.get_result_shape()
        faces0 = len(shape0.Faces)
        # 取一条竖直边（中点 z=15）做圆角
        lab = next(lab for lab, d in table.items() if "15.0" in d and "直线边" in d)
        r = features.fillet_edges(s, [lab], radius=3)
        assert r["ok"] and len(s.get_result_shape().Faces) > faces0
        print("FILLET_OK", lab, r["volume"])
    """)
    assert "FILLET_OK" in out


@pytest.mark.slow
def test_add_hole_offset_outside_raises(runtime_env):
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import annotate
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "miss")
        modeling.add_box(s, 40, 30, 20)
        png, table, faces_reg, _ = annotate.render_annotated(
            s.get_result_shape(), mode="faces", view="iso")
        s.set_labels(faces_reg, {})
        top = next(lab for lab, d in table.items() if "顶面" in d)
        try:
            features.add_hole(s, top, diameter=8, offset=[500, 500])
            raise SystemExit("EXPECTED RuntimeError for off-part hole")
        except RuntimeError as exc:
            assert "未移除任何材料" in str(exc)
            print("OFF_PART_HOLE_RAISES_OK")
    """)
    assert "OFF_PART_HOLE_RAISES_OK" in out
```

- [ ] **Step 2: e2e 追加**（tests/test_runtime_integration.py 末尾追加；`_SRC` 等常量该文件已有）

```python
@pytest.mark.slow
def test_annotated_feature_flow(runtime_env, tmp_path):
    """R5 端到端：标注 → 按标签打孔 → 新标注 → 导出（指代闭环 + 几何断言）。"""
    out_dir = str(tmp_path / "out")
    code = textwrap.dedent(f"""
        import math, os, sys
        sys.path.insert(0, {_SRC!r})
        from vibecad.freecad_env import prepare_freecad_import
        prepare_freecad_import()
        from vibecad.engine.session import Session
        from vibecad.feedback import annotate
        from vibecad.tools import modeling, features, export
        s = Session()
        modeling.new_document(s, "r5flow")
        modeling.add_box(s, 40, 30, 20)
        png1, table, faces_reg, _ = annotate.render_annotated(
            s.get_result_shape(), mode="faces", view="iso")
        assert png1.startswith(b"\\x89PNG")
        s.set_labels(faces_reg, {{}})
        top = next(lab for lab, d in table.items() if "顶面" in d)
        r = features.add_hole(s, top, diameter=8)
        expect = 40*30*20 - math.pi * 16 * 20
        assert abs(r["volume"] - expect) < 1.0, (r["volume"], expect)
        png2, table2, faces_reg2, _ = annotate.render_annotated(
            s.get_result_shape(), mode="faces", view="iso")
        assert png2.startswith(b"\\x89PNG") and len(faces_reg2) > len(faces_reg)
        res = export.export_part(s, {out_dir!r}, fmt="step")
        assert os.path.getsize(res["step"]) > 0
        print("R5_FLOW_OK", top, round(r["volume"], 1))
    """)
    proc = subprocess.run([runtime_env["python"], "-c", code],
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
    assert "R5_FLOW_OK" in proc.stdout
```

注：若该文件的既有 slow 测试对子进程代码组织方式不同（如经 `status._PREP`），按既有写法对齐；export_part 的真实签名以 `tools/export.py` 为准（Round 2 实现）。

- [ ] **Step 3: 真机跑全部 slow**

Run: `VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -v 2>&1 | tail -15`
Expected: 既有 8 条 + 新 5 条全 PASS

- [ ] **Step 4: 黑盒脚本**（仿 .vibecad/blackbox_positioned.py：官方 mcp SDK stdio_client，ENV_PY + PYTHONPATH=src）：流程 `new_document → add_box(40,30,20) → render_part(annotate="faces")`（保存 PNG 到 /tmp/blackbox_annotated_1.png + 打印标签表）→ 从表选"顶面"标签 → `add_hole(face=<标签>, diameter=8)` → `render_part(annotate="faces")` 存 /tmp/blackbox_annotated_2.png → `describe_part` 体积≈21994。**跑完 Read 两张 PNG 人眼确认**：图 1 标签清晰、图 2 顶面正中有 ⌀8 孔。

- [ ] **Step 5: commit** `test: e2e annotated referencing flow (label→hole→fresh labels) + blackbox`

---

## Task 7：收尾——README + 计划入库 + 飞书 + memory

**Files:** Modify `README.md`、`docs/superpowers/plans/2026-06-09-round5-referencing.md`（回填实施记录）；飞书同步

- [ ] **Step 1: README** — 工具表更新：render_part 行加 annotate/edges_of 说明；新增 add_hole / fillet_edges / chamfer_edges 三行；「视觉反馈」节加"标注图：面标签 A/B/C + 边标签 E1.. + 尺寸线，标签表给 AI 翻译自然语言指代；几何变更后标签自动过期（指纹校验），杜绝指错面"。
- [ ] **Step 2: 状态行更新** — README 顶部状态行加 Round 5。
- [ ] **Step 3: 快测全绿确认** `uv run pytest -q && uv run ruff check .`
- [ ] **Step 4: commit + 推送 + PR** `git push -u origin feat/round5-referencing`（分支在动工前创建）；`gh pr create` 给出交付/验收/黑盒证据。**单分支栈，base=main**（吸取 Round 4 教训：不再栈式 PR）。
- [ ] **Step 5: 飞书** — 本计划（含实施记录）`lark-cli docs +create --api-version v2 --parent-token DwYlfjYTelFG1RdTiFhc3NfWnAh --content @docs/superpowers/plans/2026-06-09-round5-referencing.md --doc-format markdown`；spec 同样同步。
- [ ] **Step 6: memory 更新** — vibecad-status.md 记 Round 5 完成状态。

---

## 技术风险与对策

1. **FastMCP 混合 list 返回**（Image+str）——Task 0 Step 1 先验证；不行走 `get_labels()` 工具回退（Task 5 注明）。
2. **matplotlib 标签可读性/重叠**——Task 0 spike 人眼检验定稿样式再动工。
3. **Part::Fillet/Chamfer 的 Edges 属性格式**（`[(1-based idx, r1, r2)]`）与 OCCT 圆角脆弱——慢测覆盖；断言"面数增加+体积变化"逮失败并报边标签。
4. **`isInside` 在 Compound 上的可用性**——`_outward_normal` 已退到 `Solids[0]`；慢测 `test_annotate_then_add_hole_by_label` 实测覆盖。
5. **Plane.Axis 定向不稳**——指纹匹配允许轴反号（`_axis_match`）；外法向用 isInside 探针校正，不信 normalAt 定向。
6. **打孔后"未变面"的指纹中心其实也可能轻微变化**（布尔重建顶点）——匹配容差 1e-3mm 量级足够；慢测 `test_stale_label_raises_unchanged_face_still_resolves` 是此假设的真机检验，若失败按实测调容差。

## Verification（端到端验收）

1. **快**：`uv run pytest -q && uv run ruff check .` 全绿（naming 指纹/匹配、annotate 纯函数、features 校验、server 委托、握手纯净）。
2. **慢（真机）**：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow` 全绿——含"顶面标签打孔体积精确断言"、"打孔后旧标签过期+未变面仍可指"、"边标签圆角面数增加"、"孔落空响亮失败"。
3. **黑盒**：真实 MCP 协议跑标注→指代打孔→新标注，**人眼看图**确认标签清晰、孔在指定面正中。
4. **CI**：五平台 unit + 三平台 runtime-integration 绿。
