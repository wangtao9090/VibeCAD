# VibeCAD Round 6a — 三视图拼图 + 每步自动回图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（逐任务 实现→审查→修复）。Steps 用 `- [ ]` 勾选。

**Goal:** 每次建模/特征指令成功后自动回一张 2×2 三视图拼图（半透明 front/right 透出内部孔 + top + 标注版 iso），用户"说一句看一眼"，标签当场刷新。

**Architecture:** ①annotate.py 抽两个共享件：`_draw_face_meshes`（单格绘制，rule-of-three 触发）与 `collect_annotation_data`（tessellate+指纹+注册表+表，faces 路径数据源）；②新 `feedback/multiview.py` 拼图；③server 层 `_attach_view` 统一包装六个工具的成功返回为 `[result_dict, Image]`，附图失败不连坐。

**Tech Stack:** matplotlib Agg subplots（已有）、FastMCP 混合 list 多内容（R5 已验证+契约测试锁死）。

**Spec:** `docs/superpowers/specs/2026-06-10-round6a-multiview-design.md`（spike 两版已人眼定稿：正交格 alpha=0.35、2×2 880×880、中英文标题）

---

## File Structure

```
src/vibecad/feedback/annotate.py   改   抽 _draw_face_meshes(ax,...) 与 collect_annotation_data(shape)；
                                        annotated_png/render_annotated 改用之（行为零变化，既有测试守护）
src/vibecad/feedback/multiview.py  新建 multiview_png[纯] + render_multiview(shape)
src/vibecad/feedback/__init__.py   改   re-export multiview
src/vibecad/server.py              改   render_part view="multi"；_attach_view；六工具接 _attach_view
tests/test_multiview.py            新建 纯函数快测 + server multi/自动附图快测在 test_server_round6.py
tests/test_server_round6.py        新建 六工具附图形态/不连坐/契约
tests/test_tools_features.py       改   @slow 追加（标签新鲜性）
.vibecad/blackbox_multiview.py     新建 黑盒（控制者跑+看图）
```

---

## Task 1：抽取 `_draw_face_meshes`（行为零变化）

**Files:** Modify `src/vibecad/feedback/annotate.py`

- [ ] **Step 1: 抽取**——把 `annotated_png` 中"逐面画三角集 + 设轴范围/aspect/视角"段（现 111-139 行）抽为模块级函数，加 `alpha` 参数：

```python
def _draw_face_meshes(ax, face_meshes: list[dict], *, view: str, alpha: float = 1.0) -> bool:
    """在单个 3D axes 上画逐面网格（palette 着色+朗伯明暗）并设轴范围/视角。
    alpha<1 为半透明（X 光正交视图用，边线同步弱化）。
    返回是否画出了任何几何（False=全部空网格，由调用方决定如何报错）。"""
    import numpy as np  # noqa: PLC0415
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # noqa: PLC0415

    light = np.array([0.35, 0.30, 0.88])
    light = light / np.linalg.norm(light)
    edge_rgba = (0, 0, 0, 0.05 if alpha < 1.0 else 0.12)
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
            tris.append([verts[i] for i in f])
            cols.append((*(base * shade), alpha))
        ax.add_collection3d(Poly3DCollection(
            tris, facecolors=cols, edgecolor=edge_rgba, linewidths=0.2))
    if not all_pts:
        return False
    pts = np.asarray(all_pts)
    mins, maxs = pts.min(0), pts.max(0)
    pad = _PAD_RATIO * float((maxs - mins).max() or 1.0)
    ax.set_xlim(mins[0] - pad, maxs[0] + pad)
    ax.set_ylim(mins[1] - pad, maxs[1] + pad)
    ax.set_zlim(mins[2] - pad, maxs[2] + pad)
    # aspect 与 limits 同口径（都含 pad），否则非立方体各向异性拉伸（孔变椭圆）
    ax.set_box_aspect(tuple(((maxs[i] - mins[i]) + 2 * pad) or 1 for i in range(3)))
    ax.view_init(*_VIEWS[view])
    ax.set_axis_off()
    return True
```

`annotated_png` 对应段替换为：

```python
        ax = fig.add_subplot(111, projection="3d")
        if not _draw_face_meshes(ax, face_meshes, view=view):
            raise ValueError("空网格：所有面 tessellation 均为空")
```

（删除原 light 初始化与循环段；`io/matplotlib/plt` 的 import 与标签/尺寸段不动。）

- [ ] **Step 2: 回归**

Run: `uv run pytest tests/test_annotate.py tests/test_naming.py -q && uv run pytest -q && uv run ruff check .`
Expected: 全 PASS（164 passed——纯重构零行为变化）

- [ ] **Step 3: commit** `refactor(annotate): extract _draw_face_meshes for multiview reuse (rule of three)`

---

## Task 2：`collect_annotation_data` 抽取 + `feedback/multiview.py`

**Files:** Modify `src/vibecad/feedback/annotate.py`; Create `src/vibecad/feedback/multiview.py`, `tests/test_multiview.py`; Modify `src/vibecad/feedback/__init__.py`

- [ ] **Step 1: 失败测试 tests/test_multiview.py**

```python
# tests/test_multiview.py
"""multiview：2×2 拼图纯函数快测（不碰 FreeCAD）。"""
import pytest
from vibecad.feedback import multiview

_TET_V = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
_TET_F = [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]


def test_multiview_png_smoke():
    png = multiview.multiview_png(
        face_meshes=[{"verts": _TET_V, "facets": _TET_F}],
        face_labels=[{"label": "A", "pos": (3, 3, 0), "visible": True}],
        dims={"L": 10, "W": 10, "H": 10, "bbox": (0, 0, 0, 10, 10, 10)})
    assert png.startswith(b"\x89PNG") and len(png) > 5000  # 4 格拼图显著大于单格


def test_multiview_png_empty_mesh_raises():
    with pytest.raises(ValueError):
        multiview.multiview_png(face_meshes=[], face_labels=[], dims=None)


def test_module_import_purity():
    assert not any(m in getattr(multiview, "__dict__", {})
                   for m in ("matplotlib", "FreeCAD"))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_multiview.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 在 annotate.py 抽 `collect_annotation_data`**——把 `render_annotated` 中"bbox + 逐面 tessellate/指纹/锚点/法向 + 全量边指纹 + faces 标签/可见性/表/注册表 + dims"的数据生产部分抽出（faces 路径数据源；edges 模式的绘制数据 polyline/edge_adj/draw_set 仍留在 render_annotated）：

```python
def collect_annotation_data(shape: Any, *, view: str = "iso") -> dict:
    """逐面 tessellate + 指纹/锚点/可见性 + 全量注册表 + faces 标签表 + 尺寸。
    render_annotated(mode='faces') 与 multiview.render_multiview 的共享数据源。
    返回 {face_meshes, face_labels, table, faces_reg, edges_reg, dims}。"""
    from vibecad.engine import naming  # noqa: PLC0415
    from vibecad.freecad_env import silence_fd1  # noqa: PLC0415

    cam = camera_direction(view)
    with silence_fd1():
        bb = shape.BoundBox
        bbox = (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)
        face_meshes, face_info = [], []
        for f in shape.Faces:
            verts, facets = f.tessellate(0.1)
            pts = [(p.x, p.y, p.z) for p in verts]
            face_meshes.append({"verts": pts, "facets": facets})
            face_info.append({"fp": naming.face_fingerprint(f),
                              "anchor": largest_triangle_centroid(pts, facets, cam),
                              "normal": mesh_normal(pts, facets)})
        if not any(fm["verts"] for fm in face_meshes):
            raise RuntimeError("几何断言失败：形状无法镶嵌为网格（空 tessellation）")
        edges_reg_list = [naming.edge_fingerprint(e) for e in shape.Edges]
    face_names = naming.face_labels(len(face_info))
    table: dict[str, str] = {}
    faces_reg: dict[str, dict] = {}
    face_labels = []
    for lab, info in zip(face_names, face_info, strict=True):
        visible = (sum(a * b for a, b in zip(info["normal"], cam, strict=True)) > _VIS_DOT)
        faces_reg[lab] = info["fp"]
        face_labels.append({"label": lab, "pos": info["anchor"], "visible": visible})
        table[lab] = naming.face_summary(info["fp"], bbox) + visibility_note(info["normal"], view)
    edges_reg = dict(zip(naming.edge_labels(len(edges_reg_list)), edges_reg_list, strict=True))
    dims = {"L": bbox[3] - bbox[0], "W": bbox[4] - bbox[1], "H": bbox[5] - bbox[2],
            "bbox": bbox}
    return {"face_meshes": face_meshes, "face_labels": face_labels, "table": table,
            "faces_reg": faces_reg, "edges_reg": edges_reg, "dims": dims}
```

`render_annotated` 的 faces 模式改为调用它（mode=="edges" 路径的边绘制数据采集与现状一致——以现文件实际结构为准做最小重构，**注册表全量契约与 edges_of/visible 行为不得变化**，既有快/慢测试守护）。

- [ ] **Step 4: 实现 src/vibecad/feedback/multiview.py**

```python
# src/vibecad/feedback/multiview.py
"""三视图拼图（Round 6a）：2×2 = 半透明 front/right（X 光透出内部孔）+ top + 标注版 iso。
multiview_png 纯函数；render_multiview 才碰 FreeCAD Shape。"""
from __future__ import annotations

from typing import Any

from vibecad.feedback.annotate import _draw_face_meshes, collect_annotation_data

# (标题, 视角名, alpha)——spike 定稿：正交格 0.35 半透明，top/iso 不透明
_GRID = [("front 正视", "front", 0.35), ("top 俯视", "top", 1.0),
         ("right 侧视", "right", 0.35), ("iso 立体", "iso", 1.0)]


def multiview_png(*, face_meshes: list[dict], face_labels: list[dict],
                  dims: dict | None, size: tuple[int, int] = (880, 880)) -> bytes:
    """2×2 拼图 → PNG bytes。iso 格画标签+尺寸线，其余格纯几何。纯 matplotlib。"""
    if not face_meshes:
        raise ValueError("空网格：无任何面可渲染（可能是 tessellate 失败或形状退化）")
    import io  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    fig = plt.figure(figsize=(size[0] / 100, size[1] / 100), dpi=100)
    try:
        drew_any = False
        for k, (title, view, alpha) in enumerate(_GRID):
            ax = fig.add_subplot(2, 2, k + 1, projection="3d")
            drew = _draw_face_meshes(ax, face_meshes, view=view, alpha=alpha)
            drew_any = drew_any or drew
            ax.set_title(title, fontsize=10, pad=2)
            if view != "iso" or not drew:
                continue
            for fl in face_labels:  # iso 格：标签（仅可见面）
                if not fl.get("visible"):
                    continue
                ax.text(*fl["pos"], fl["label"], fontsize=10, fontweight="bold",
                        ha="center", va="center", zorder=99,
                        bbox=dict(boxstyle="round,pad=0.22", fc="white", ec="#333", alpha=0.92))
            if dims:  # iso 格：包围盒尺寸线
                x0, y0, z0, x1, y1, z1 = dims["bbox"]
                m = max(x1 - x0, y1 - y0, z1 - z0) * 0.08
                for p0, p1, txt in (
                        ((x0, y0 - m, z0), (x1, y0 - m, z0), f"L={dims['L']:g}"),
                        ((x1 + m, y0, z0), (x1 + m, y1, z0), f"W={dims['W']:g}"),
                        ((x0 - m, y0 - m, z0), (x0 - m, y0 - m, z1), f"H={dims['H']:g}")):
                    ax.plot(*zip(p0, p1, strict=False), color="#555", lw=1)
                    mid = tuple((p0[i] + p1[i]) / 2 for i in range(3))
                    ax.text(*mid, txt, fontsize=8, color="#333", zorder=99,
                            bbox=dict(boxstyle="round,pad=0.15", fc="#f5f5f5",
                                      ec="none", alpha=0.9))
        if not drew_any:
            raise ValueError("空网格：所有面 tessellation 均为空")
        fig.subplots_adjust(left=0.01, right=0.99, top=0.95, bottom=0.01,
                            wspace=0.02, hspace=0.08)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
    finally:
        plt.close(fig)
    return buf.getvalue()


def render_multiview(shape: Any) -> tuple[bytes, dict, dict, dict]:
    """FreeCAD Shape → (png, labels_table, faces_reg, edges_reg)。
    标签语义与 render_annotated(mode='faces', view='iso') 完全一致（注册表全量）。"""
    data = collect_annotation_data(shape, view="iso")
    png = multiview_png(face_meshes=data["face_meshes"], face_labels=data["face_labels"],
                        dims=data["dims"])
    return png, data["table"], data["faces_reg"], data["edges_reg"]
```

`feedback/__init__.py` import 元组加 `multiview,  # noqa: F401`。

- [ ] **Step 5: 全绿 + ruff**

Run: `uv run pytest -q && uv run ruff check .`
Expected: 全 PASS（含 annotate 既有测试零回归）

- [ ] **Step 6: commit** `feat(multiview): 2x2 translucent-ortho + annotated-iso composite render`

---

## Task 3：server `render_part(view="multi")`

**Files:** Modify `src/vibecad/server.py`; Create `tests/test_server_round6.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_server_round6.py
"""server Round6a：multi 视图、六工具自动附图、附图失败不连坐、协议契约。"""
import json
import sys

import pytest


@pytest.fixture()
def server(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: None)
    return srv


def _mock_multiview(server, monkeypatch, png=b"\x89PNG mv"):
    monkeypatch.setattr(server._multiview, "render_multiview",
                        lambda shape: (png, {"A": "顶面"}, {"A": {}}, {"E1": {}}))


def test_render_part_view_multi(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    recorded = {}
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: recorded.update(s=shown))
    out = server.render_part(view="multi")
    assert isinstance(out, list) and isinstance(out[0], Image)
    assert json.loads(out[1])["labels"]["A"] == "顶面"
    assert recorded["s"] == {"A"}


def test_render_part_view_multi_with_annotate_rejected(server, monkeypatch):
    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    out = server.render_part(view="multi", annotate="faces")
    assert out["ok"] is False  # multi 已含标注 iso 格，组合无意义须显式拒绝
```

- [ ] **Step 2: 确认失败** `uv run pytest tests/test_server_round6.py -q` → FAIL

- [ ] **Step 3: 实现**——server.py 顶部加 `from vibecad.feedback import multiview as _multiview`；render_part 开头（守卫与 edges_of 校验之后）加 multi 分支：

```python
    if view == "multi":
        if annotate is not None or edges_of is not None:
            return {"ok": False,
                    "message": "view='multi' 已含标注 iso 格，不能与 annotate/edges_of 组合"}
        try:
            with _silence_fd1():
                shape = _session.get_result_shape()
                png, table, faces_reg, edges_reg = _multiview.render_multiview(shape)
            _session.set_labels(faces_reg, edges_reg, shown=set(table.keys()))
            return [Image(data=png, format="png"),
                    json.dumps({"ok": True, "labels": table}, ensure_ascii=False)]
        except (RuntimeError, ValueError) as exc:
            return {"ok": False, "message": f"渲染失败：{exc}"}
```

docstring 的 view 取值说明加 `multi`（三视图+标注 iso 拼图）。

- [ ] **Step 4: 全绿** `uv run pytest -q && uv run ruff check .`
- [ ] **Step 5: commit** `feat(server): render_part view=multi (composite multiview)`

---

## Task 4：六工具每步自动附图（`_attach_view`，失败不连坐）

**Files:** Modify `src/vibecad/server.py`; Modify `tests/test_server_round6.py`

- [ ] **Step 1: 失败测试**（追加）

```python
def test_add_box_attaches_view(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    out = server.add_box(length=40, width=30, height=20)
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True and body["labels"] == {"A": "顶面"}
    assert "labels_stale" not in body and "hint" not in body


def test_attach_view_render_failure_not_fatal(server, monkeypatch):
    """附图失败不连坐：操作 ok:True 保留 + render_error + 退回 stale 提示。"""
    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})

    def _boom(shape):
        raise RuntimeError("渲染炸了")

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(server._multiview, "render_multiview", _boom)
    out = server.add_box(length=40, width=30, height=20)
    assert isinstance(out, dict) and out["ok"] is True
    assert "渲染炸了" in out["render_error"] and out["labels_stale"] is True


def test_failed_tool_attaches_nothing(server, monkeypatch):
    def _fail(session, length, width, height, position):
        raise ValueError("length 必须 > 0")

    monkeypatch.setattr(server._modeling, "add_box", _fail)
    out = server.add_box(length=-1, width=30, height=20)
    assert isinstance(out, dict) and out["ok"] is False


def test_mcp_contract_tool_with_image(server, monkeypatch):
    """协议契约：[dict, Image] → [TextContent(json), ImageContent]。"""
    import anyio
    from mcp.types import ImageContent, TextContent

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    content = anyio.run(lambda: server.mcp.call_tool("add_box",
                                                     {"length": 40, "width": 30, "height": 20}))
    if isinstance(content, tuple):
        content = content[0]
    kinds = [type(c).__name__ for c in content]
    assert "ImageContent" in kinds and "TextContent" in kinds
    payload = json.loads([c for c in content if isinstance(c, TextContent)][0].text)
    assert payload["ok"] is True
```

- [ ] **Step 2: 确认失败** → FAIL（add_box 仍返回纯 dict）

- [ ] **Step 3: 实现**——server.py 加 `_attach_view` 并接到六工具（add_box/add_cylinder/boolean_cut/add_hole/fillet_edges/chamfer_edges 的成功路径；签名返回注解改 `-> Any`）：

```python
def _attach_view(result: dict[str, Any]) -> Any:
    """成功结果附三视图拼图 + 当场刷新标签表；附图失败不连坐（保留操作成功 +
    render_error + 退回 labels_stale 提示）——绝不因附图失败把成功操作报成失败，
    也绝不静默吞掉渲染错误。"""
    if not isinstance(result, dict) or not result.get("ok"):
        return result
    try:
        with _silence_fd1():
            shape = _session.get_result_shape()
            png, table, faces_reg, edges_reg = _multiview.render_multiview(shape)
        _session.set_labels(faces_reg, edges_reg, shown=set(table.keys()))
        result.pop("labels_stale", None)
        result.pop("hint", None)
        result["labels"] = table
        return [result, Image(data=png, format="png")]
    except (RuntimeError, ValueError) as exc:
        result["labels_stale"] = True
        result["hint"] = "几何已变更，调用 render_part(annotate='faces') 查看最新标注"
        result["render_error"] = f"自动渲染失败：{exc}"
        return result
```

六工具改法一致（以 add_box 为例，其余同型）：

```python
@mcp.tool()
def add_box(length: float, width: float, height: float,
            position: list[float] | None = None) -> Any:
    """参数化长方体（mm）；position 放置位置（默认原点）。成功后自动附三视图拼图。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        result = _modeling.add_box(_session, length, width, height,
                                   tuple(position) if position is not None else (0.0, 0.0, 0.0))
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"创建失败：{exc}"}
    return _attach_view(result)
```

（既有六工具的参数透传/错误前缀以现文件为准，只把成功 return 包上 `_attach_view`；docstring 加"成功后自动附三视图拼图"。）

- [ ] **Step 4: 全绿**（**注意既有测试**：test_server_new_tools/test_server_round5 中六工具的旧断言假定纯 dict 返回——按新形态更新这些断言：取 `out[0]` 或对 `_attach_view` 加 mock；改动原则是更新断言形态、不削弱断言强度）

Run: `uv run pytest -q && uv run ruff check .`
Expected: 全 PASS

- [ ] **Step 5: commit** `feat(server): auto-attach multiview composite to modeling/feature tool results`

---

## Task 5：真机慢测 + 黑盒（控制者看图）

**Files:** Modify `tests/test_tools_features.py`（@slow 追加）；Create `.vibecad/blackbox_multiview.py`（gitignore，本地）

- [ ] **Step 1: 慢测**（用既有 `_run_in_env` helper 范式追加）

```python
@pytest.mark.slow
def test_render_multiview_real(runtime_env):
    out = _run_in_env(runtime_env, """
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview, annotate
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "mv")
        modeling.add_box(s, 40, 30, 20)
        png, table, faces_reg, edges_reg = multiview.render_multiview(s.get_result_shape())
        assert png.startswith(b"\\x89PNG") and len(png) > 5000
        # 标签语义与 render_annotated(faces) 等价
        _, t2, fr2, er2 = annotate.render_annotated(s.get_result_shape(), mode="faces", view="iso")
        assert table == t2 and faces_reg == fr2 and edges_reg == er2
        print("MULTIVIEW_REAL_OK", len(png))
    """)
    assert "MULTIVIEW_REAL_OK" in out


@pytest.mark.slow
def test_auto_view_labels_fresh_after_feature(runtime_env):
    """每步自动刷新后标签立即可指（本轮核心行为）+ shown 门控语义不破。"""
    out = _run_in_env(runtime_env, """
        from vibecad.engine.naming import LabelExpiredError
        from vibecad.engine.session import Session
        from vibecad.feedback import multiview
        from vibecad.tools import modeling, features
        s = Session()
        modeling.new_document(s, "fresh")
        modeling.add_box(s, 40, 30, 20)
        png, table, fr, er = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr, er, shown=set(table.keys()))
        top = next(lab for lab, d in table.items() if "顶面" in d)
        features.add_hole(s, top, diameter=8)
        # 模拟 server._attach_view 的刷新
        png2, t2, fr2, er2 = multiview.render_multiview(s.get_result_shape())
        s.set_labels(fr2, er2, shown=set(t2.keys()))
        top2 = next(lab for lab, d in t2.items() if "顶面" in d)
        assert s.resolve_face(top2) >= 0          # 新标签立即可指
        try:
            s.resolve_edge("E1")                   # 边标签未展示仍被拒（门控不破）
            raise SystemExit("EXPECTED LabelExpiredError")
        except LabelExpiredError:
            print("FRESH_LABELS_OK", top2)
    """)
    assert "FRESH_LABELS_OK" in out
```

- [ ] **Step 2: 跑全量 slow** `VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -q 2>&1 | tail -2` → 既有 28 + 新 2 全 PASS

- [ ] **Step 3: 黑盒脚本**（仿 .vibecad/blackbox_annotate.py）：`new_document → add_box(40,30,20)`（断言返回含 image，存 /tmp/blackbox_mv_1.png + labels 表）→ 从表选顶面 → `add_hole(⌀8)`（断言返回含 image，存 /tmp/blackbox_mv_2.png）→ `describe_part` 体积≈22994.7 → 打印 BLACKBOX_MULTIVIEW_OK。**此步由控制者亲自跑并 Read 两图人眼确认**：四格齐全、front/right 半透明格透出孔带、iso 格标签+尺寸线清晰、top 格孔在正中。

- [ ] **Step 4: commit** `test: multiview real-machine + fresh-labels-after-feature slow tests`

---

## Task 6：收尾 + 两路终审

**Files:** Modify `README.md`、本计划（回填实施记录）；飞书；memory

- [ ] **Step 1: README**——工具表 render_part 行加 `view="multi"`；六工具行注"成功后自动附三视图拼图+标签表"；「可指代性」节后加「三视图与每步回图（Round 6a）」小节（2×2 布局、X 光半透明、自动附图、附图失败不连坐语义）。
- [ ] **Step 2: 快测+慢测最终全绿确认**。
- [ ] **Step 3: 两路终审**（小轮配置）：code-reviewer（集成缝隙/文档一致性）+ silent-failure-hunter（重点：_attach_view 的不连坐路径是否真不连坐、multi 组合拒绝、拼图四格与标签表一致性、附图后 shown 门控回归）。发现必修，修复后回归。
- [ ] **Step 4: push + PR**——若 PR #5 已合并：rebase 到 main 开新 PR base=main；若 #5 仍 OPEN：PR base=feat/round5-referencing 并在描述显著注明「**栈式 PR：先合 #5（合并时不要删除分支），再合本 PR**」（R4 教训：删 base 分支会让下游 PR 被自动关闭）。
- [ ] **Step 5: 飞书**——spec+本计划（含实施记录）同步 `DwYlfjYTelFG1RdTiFhc3NfWnAh`。
- [ ] **Step 6: memory 更新**（Round 6a 状态 + PR 栈注意事项）。

---

## 风险与对策

1. **半透明 painter's algorithm 伪影**——spike 已人眼可接受；黑盒看图终验，恶化则正交格 alpha 调参。
2. **既有 server 测试断言形态变化**（六工具 dict → [dict, Image]）——Task 4 Step 4 显式列出，更新断言不削弱强度。
3. **每步渲染耗时**——慢测记录 multiview 用时；若单次 >3s 记入实施记录，降级方案（拼图降为 720×720）留 Round 7 决定。
4. **collect_annotation_data 重构动 render_annotated**——faces/edges 两模式既有快测 30+ 与慢测 7 条守护；注册表全量契约不得变化。

## Verification

1. 快：`uv run pytest -q && uv run ruff check .` 全绿（multiview 纯函数、multi 视图、自动附图形态、不连坐、协议契约、握手纯净回归）。
2. 慢：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow` 全绿（multiview 真机、标签新鲜性、既有 28 条零回归）。
3. 黑盒：每步指令直接收到 `[json, image]`，**人眼确认拼图四格**。
4. 两路终审通过 + PR 就绪。

---

## Task 2-R（设计变更改造）：multiview 正交格从半透明改为 HLR 工程图

> 用户在 T2 交付后否决半透明方案，定稿 spike v5 工程图样式（spec §2/§3.1 已修订）。本任务改造 a64f1c0/d8fe797 已交付的 multiview.py。代码基准 = `.vibecad/spike_engineering5.py`（真实管线人眼定稿，本地未入库——其 project/draw_view/dim_h/dim_v/统一比例逻辑即实现蓝本）。

**Files:** Modify `src/vibecad/feedback/multiview.py`, `tests/test_multiview.py`

- [ ] **Step 1**: multiview.py 重构为四件套（spec §3.1 签名）：`_VIEW_TFS` 表（top 恒等 / front,right `(x,y)→(y,-x)`，附实测注释）；`project_view`（TechDraw.projectEx 函数内 import + noqa，组 0-4 可见/5-9 隐藏合并，Line discretize(2) 其余 48，圆收集 `(*tf(c.x,c.y), r, gi<5)`）；`draw_engineering_view`（实线 #222/1.4、虚线 #888/0.9 (0,(5,3))、圆心红点划十字、equal aspect、axis off、CJK 标题、返回 bbox 或 None）；尺寸函数 `_dim_h/_dim_v`（尺寸界线+双箭头+数字，来自 spike）；`multiview_png(*, eng_views, face_meshes, face_labels, dims, size=(920,760))`（三格工程图：bbox 总尺寸 + 每格 ⌀ 按半径去重 + 首可见圆定位尺寸 + 三格统一比例 span*1.45；iso 格 `_draw_face_meshes` + 标签 + 尺寸线同现状）；`render_multiview` 在 silence_fd1 内做三方向 project_view + collect_annotation_data(view="iso")。
- [ ] **Step 2**: 测试更新：`test_multiview_png_smoke` 改喂 fake eng_views（手工折线矩形+一条隐藏线+一个圆）+ 既有 iso 数据，断言 PNG>8000B；新增 `test_multiview_png_dims_from_bbox`（fake 矩形 40×20 → 不抛错即可，数字断言靠真机）；空 eng_views+空 face_meshes 抛 ValueError；模块纯净不变（TechDraw 不得模块级 import）。
- [ ] **Step 3**: `uv run pytest -q && uv run ruff check .` 全绿。
- [ ] **Step 4**: commit `feat(multiview): engineering-drawing orthographic views (HLR wireframe, dims, hidden lines)`

后续 Task 3/4/5/6 不变（render_multiview 对外签名未变）；Task 5 慢测增加：HLR 耗时打印 + top 视图投影含圆断言。

---

## 实施记录与验收（2026-06-10，macOS arm64 实机）

**执行方式**：闭关 subagent-driven。中途经历一次**用户方向修正**：T2 交付半透明"X 光"方案后用户否决，要求真工程图样式 → spike v3-v5 三版迭代（TechDraw.projectEx HLR headless 实证）→ Task 2-R 改造定稿。

### 验收结果
- 快测 **177 passed** + ruff 清零 + 模块纯净（multiview 模块级零 TechDraw/matplotlib/FreeCAD）。
- 真机慢测 **32 passed**：multiview 与 render_annotated 标签三向等价、标签新鲜性+shown 门控、**非对称凸台精确 2D 坐标断言**（钉死三视图变换符号）、fillet 弧不入 circles、HLR 全流程 302ms。
- **黑盒（真实 MCP 协议）**：`add_box` → 直接收到 `[json+工程图拼图]`（标签当场刷新无 stale）→ 按表选顶面 `add_hole(⌀8)` → 再收拼图 → 体积差 0.000。**两张图人眼确认**：三视图带尺寸/⌀8/中心线/定位 20·15/孔壁虚线，iso 格标签同步。

### 审查逮到的问题（已修复）
1. **2×CRITICAL（终审 silent-failure-hunter 真机实锤）**：①right 侧视图整体 180° 旋转（front/right 共用变换但两方向投影局部系手性不同；spike 用上下对称零件检验导致旋转不可分辨——"几何不撒谎、语义撒谎"）→ right tf 改 `(-y,x)`+坐标取证注释；②fillet 圆角弧被当整圆标 ⌀ 并劫持定位尺寸 → 2π 整圆判定（`bca59b6`）。
2. T3/T4 审查：_attach_view 不连坐承诺漏网（TypeError 穿透谎报失败诱发重试叠对象）→ 事务后纯展示阶段宽抓 Exception；labels_stale 双源矛盾 → setdefault 保留 tools 层语义（`f579ab2`）。
3. T1/T2 审查：CJK 标题字体计划转录丢失（方块字）→ 跨平台 fallback；faces 模式 edges_of 静默回归 → 显式拒绝（`d8fe797`）。
4. 其余：⌀ 只标可见整圆、每孔定位尺寸、multi 分支结构化宽抓、空格"（无投影）"占位、投影朝向约定声明（`bca59b6`）。

### 方法论沉淀（第五轮 CRITICAL 的新教训）
**对称零件人眼检验 + 弱断言（"含圆"）对朝向/语义类错误全盲**——线条都是真的，但标注/朝向把真线条解读错了。解法已固化为测试纪律：非对称取证体 + 精确 2D 坐标断言。

### 结论
「说一句，看一张读得懂的工程图」闭环成立：每步建模指令自动回 2×2 拼图（三视图带尺寸/孔径/隐藏线 + 标注 iso），标签当场刷新。HLR 302ms/件，拼图 ~45KB。
