# VibeCAD Round 3 — 视觉反馈层（PNG 软渲染 + glTF 工件）Implementation Plan（v1）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（逐任务 实现→spec/质量审查→修复）。Steps 用 `- [ ]` 勾选。

**Goal:** 让用户**看得见**正在造的零件，从而能迭代式画图。交付：① `render_part` —— 每步可调，回传 **PNG 软渲染图**（MCP `ImageContent`，当前客户端内联可见）；② **glTF 工件导出**（逐面 tessellate + 面级 extras，为未来 App 交互拾取铺路）。这是把 Round 2 的"能造但看不见"补成"看得见、能迭代"的 MVP 视觉层。

**范围（已锁定）：** PNG 软渲染（matplotlib Agg，无 GPU）+ glTF 导出（pygltflib 逐面）。**不含**：FreeCADGui 离屏高质量渲染、位置控制、新建模特征、装配、agent 接入（均留后续）。

**复用 Round 1/2（已落地）：** `engine/session.Session`（`get_result_shape`/`doc`）、`freecad_env.silence_fd1`、server 三态守卫 `_runtime_guard`、`tools/export` 的导出范式、`tests/conftest.py::runtime_env` fixture、`@slow` 子进程真机测试范式（`status._PREP` + `sys.path.insert(0, <repo>/src)`）。

---

## Context（为什么做这件事）

Round 2 黑盒演示证明用户能"说人话造出带孔方块并导出 STEP/STL"，但**只有文本诊断、看不到图**——没有可视化就没有迭代式设计，CAD 不可用。用户明确：**视觉反馈是 MVP**。传输层现实（已实机确认）：MCP 标准客户端**内联渲染 PNG（`ImageContent`）**但**不内联渲染 glTF**。故本轮 PNG 软渲染打底（当下即可见、可迭代），glTF 工件并行导出（带面级元数据，供未来会渲 glTF 的 App/客户端交互旋转拾取）。

**渲染路径已实机探明**（`.vibecad-test-runtime` env）：matplotlib **已随 freecad 装入**，**matplotlib Agg（纯 CPU 无 GPU）渲染 tessellate 网格 → PNG 成功**（box-with-hole 21KB）；trimesh 离屏渲染**失败**（缺 pyglet/GL）；pygltflib **在 env**（本就是依赖）；`Shape.tessellate(0.1)` 正常。**故 PNG=matplotlib Agg，glTF=逐面 tessellate + pygltflib。** 渲染质量为 MVP 级（看清形状/朝向/比例，非照片级；FreeCADGui 高质量离屏留后续）。

---

## Architecture

```
MCP Client ──> server.render_part(view="iso")  [守卫 → 返回 mcp Image(PNG)]
                  │
                  ▼  feedback/render.py
               render_png(shape) → shape.tessellate(tol) → mesh_to_png(verts,facets,view)  [matplotlib Agg, 纯 CPU]
                  │（mesh_to_png 纯函数，不碰 FreeCAD，dev venv 可单测）
                  ▼  PNG bytes → mcp.server.fastmcp.Image(data=…, format="png")

MCP Client ──> server.export_part(out, fmt="all")  [新增 gltf]
                  │
                  ▼  feedback/gltf.py
               export_gltf(shape, path) → 逐 face tessellate → build_glb(parts[{verts,facets,extras}]) [pygltflib]
                  │（build_glb 纯函数，dev venv 可单测）
                  ▼  .glb（每 face 一 primitive，extras={face_index, geom_type}）
```

**纪律延续：** server 模块级不 import FreeCAD（保握手）；render/gltf 的真实几何部分懒加载、包在 `silence_fd1`；纯函数（`mesh_to_png`/`build_glb`）只吃普通 list 数据 → dev venv 快单测；真实 tessellate 走 `@slow`。

---

## File Structure

```
src/vibecad/feedback/
├── render.py   新建  mesh_to_png(verts,facets,*,view,size)[纯] + render_png(shape,*,view)[tessellate]
├── gltf.py     新建  build_glb(parts)[纯,pygltflib] + export_gltf(shape,path,*,doc_name)[逐面]
└── __init__.py 改    re-export render/gltf
src/vibecad/server.py   改  render_part(view) 返回 Image；export_part fmt 增 "gltf"/"all"
tests/  test_render.py / test_gltf.py 新建；test_server_new_tools.py 改（render_part/export glTF）；test_runtime_integration.py 改（追加 render+gltf 端到端）
pyproject.toml   改  deps 增 matplotlib>=3.5（松约束，conda 满足，避免 ABI 雷）
README.md / docs/ / 飞书   改
```

---

## Task 1：PNG 软渲染 `feedback/render.py` + matplotlib 依赖

**Files:** Create `src/vibecad/feedback/render.py`, `tests/test_render.py`; Modify `src/vibecad/feedback/__init__.py`, `pyproject.toml`

**接口：**
```python
_VIEWS = {"iso": (25, -60), "front": (0, -90), "top": (89, -90), "right": (0, 0), "back": (0, 90)}

def mesh_to_png(verts: list[tuple[float,float,float]], facets: list[tuple[int,int,int]],
                *, view: str = "iso", size: tuple[int,int] = (440, 440)) -> bytes:
    # matplotlib.use("Agg")；Poly3DCollection(tris, facecolor=…, edgecolor=…)；
    # 按 verts 包围盒等比设轴 + set_box_aspect；ax.view_init(*_VIEWS[view])；savefig(BytesIO,'png')→bytes
    # view 非法 → ValueError

def render_png(shape, *, view: str = "iso", size=(440,440)) -> bytes:
    # from vibecad.freecad_env import silence_fd1; with silence_fd1():
    #   v,f = shape.tessellate(0.1); pts=[(p.x,p.y,p.z) for p in v]; return mesh_to_png(pts,f,view=view,size=size)
```
**pyproject:** dependencies 增 `"matplotlib>=3.5",  # 软渲染（matplotlib Agg，无 GPU）；松约束让 conda 满足`。

- [ ] **Step 1: 失败测试** — 纯单元（dev venv，需 matplotlib，故 T1 同时加 dep 并 `uv sync`）：`mesh_to_png` 给一个四面体（4 verts/4 facets）→ 返回 bytes、以 `b"\x89PNG"` 开头、len>1000；`view` 非法抛 ValueError。 + **slow**（用 `runtime_env`）：真实 box(20³) `render_png` → PNG bytes 以 PNG magic 开头且 >2KB。
- [ ] **Step 2: `UV_DEFAULT_INDEX=…tsinghua uv sync`（拉 matplotlib）→ 实现 → 绿 + ruff**
- [ ] **Step 3: commit** `feat(feedback): PNG soft-render via matplotlib Agg (no-GPU)`

---

## Task 2：glTF 导出 `feedback/gltf.py`

**Files:** Create `src/vibecad/feedback/gltf.py`, `tests/test_gltf.py`; Modify `feedback/__init__.py`

**接口：**
```python
def build_glb(parts: list[dict]) -> bytes:
    """parts=[{"verts":[(x,y,z)...], "facets":[(i,j,k)...], "extras":{...}}]；
    用 pygltflib 组装单 buffer + 每 part 一 mesh primitive（POSITION accessor + indices），
    primitive.extras 写入该 part 的 extras（面级元数据）。返回 .glb bytes。"""

def export_gltf(shape, path: str, *, doc_name: str = "part") -> str:
    # with silence_fd1(): 逐 face：fv,ff = face.tessellate(0.1)（带顶点偏移合并到全局索引）；
    #   extras={"face_index": i, "geom_type": type(face.Surface).__name__}
    # data = build_glb(parts); Path(path).write_bytes(data); return path
```
> pygltflib 组装要点：把所有 face 的顶点 concat 进一个 binary blob，POSITION/indices 各建 accessor+bufferView；每 face 一 primitive 引用自己的 index 区间 + 自己的 extras。glb = `GLTF2(...).set_binary_blob(...)` → `.save_to_bytes()` 或 `gltf_to_glb`。

- [ ] **Step 1: 失败测试** — 纯单元（pygltflib 已是依赖）：`build_glb` 给 2 个 part（各一三角）→ 返回 bytes、以 glTF magic `b"glTF"` 开头；用 `pygltflib.GLTF2` 从 bytes 回读 → mesh/primitive 数 == 2 且 primitive.extras 含 face_index。 + **slow**：真实 box(10³) `export_gltf` → .glb 文件存在且 >0、`GLTF2().load` 可读、primitive 数 == 6（立方体 6 面）。
- [ ] **Step 2-3: 实现 + 绿 + ruff**（懒 import 标 `# noqa: PLC0415`）
- [ ] **Step 4: commit** `feat(feedback): glTF (.glb) export, per-face primitives with extras`

---

## Task 3：server `render_part` 工具 + export glTF

**Files:** Modify `src/vibecad/server.py`; Modify `tests/test_server_new_tools.py`

> `render_part` 返回 `mcp.server.fastmcp.Image`（→ MCP ImageContent，客户端内联渲染）；`export_part` 的 fmt 增 `"gltf"` 与 `"all"`（step+stl+gltf）。

**实现要点：**
```python
from mcp.server.fastmcp import Image  # 顶部
from vibecad.feedback import render as _render, gltf as _gltf  # 顶部

@mcp.tool()
def render_part(view: str = "iso") -> Any:
    """渲染当前零件为 PNG 图（view: iso|front|top|right|back），客户端内联可见。"""
    guard = _runtime_guard()
    if guard:
        return guard
    with _silence_fd1():
        png = _render.render_png(_session.get_result_shape(), view=view)
    return Image(data=png, format="png")
```
`export_part` 扩展：`fmt in ("step","stl","gltf","both","all")`；gltf → `_gltf.export_gltf(shape, <out>/<doc>.glb)`；all → step+stl+gltf。返回 dict 增 `"gltf"` 字段。

- [ ] **Step 1: 失败测试** — 纯单元（monkeypatch）：`render_part` guard 未就绪→dict ok False；就绪+conda（mock `_render.render_png`→b"\x89PNG…"）→返回 `Image` 实例且其 data 为该 bytes；`export_part(fmt="all")` 委托含 gltf。 + 回归：`import vibecad.server; 'FreeCAD' not in sys.modules`（render/gltf 模块级不得拉 FreeCAD/matplotlib——matplotlib 懒加载在 mesh_to_png 内）。
- [ ] **Step 2-3: 实现 + 绿 + ruff**
- [ ] **Step 4: commit** `feat(server): render_part (PNG ImageContent) + export glTF`

> 注意：`feedback/render.py` 顶层**不要** `import matplotlib`（重、且保 server 导入轻）；matplotlib 只在 `mesh_to_png` 函数体内 import。Task 1 实现须遵守，Task 3 的握手回归会卡这点。

---

## Task 4：端到端视觉慢测试

**Files:** Modify `tests/test_runtime_integration.py`（追加 `test_render_and_gltf`，勿动既有）

- [ ] **Step 1: 慢测试** `test_render_and_gltf(runtime_env, tmp_path)`：env python 子进程跑真实 engine+tools+feedback：`new_document→add_box(30,20,10)→add_cylinder(5,30)→boolean_cut`，然后 `render_png(shape)` 断言 PNG magic + bytes>2KB；`export_gltf(shape, <tmp>/p.glb)` 断言文件 >0 且 `pygltflib.GLTF2().load` 可读、primitive 数>0；打印 `VISUAL_OK`。
- [ ] **Step 2: 本机实跑**（复用 fixture env）：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -v -s`（全部 slow 应过）。**另存一张真实 PNG 到 /tmp 供人眼确认形状对**。
- [ ] **Step 3: commit** `test: e2e PNG render + glTF export on real FreeCAD`

---

## Task 5：依赖收尾 + README + 计划入库 + 飞书

**Files:** Modify `pyproject.toml`(若 T1 未含 uv.lock 提交则补)、`.github/workflows/ci.yml`(确认覆盖)、`README.md`；Create `docs/superpowers/plans/2026-06-09-round3-visual.md`

- [ ] **Step 1: CI** — 把 `test_render_and_gltf` 纳入 runtime-integration（它在 test_runtime_integration.py，已被现有 CI 命令覆盖；确认即可）。
- [ ] **Step 2: README** — 「语义建模工具」表增 `render_part(view)`（PNG 内联预览）与 export `gltf/all`；说明"PNG 当下内联可见、glTF 工件供未来 App 交互"。
- [ ] **Step 3: 计划入库** `docs/superpowers/plans/2026-06-09-round3-visual.md`（本计划 + 实施记录，含真机渲染探针结论）。
- [ ] **Step 4: 飞书同步** 过程文档文件夹 `DwYlfjYTelFG1RdTiFhc3NfWnAh`（含 workflow 改动则推送走 SSH）。
- [ ] **Step 5: commit** `chore: round3 deps + README + plan docs`

---

## 技术风险（实机已大幅 de-risk，集成测试仍须确认）

1. **matplotlib 在 env 的存在性**：探针确认已随 freecad 装入；松约束 `matplotlib>=3.5` 让 conda 满足、pip 不重装（避 ABI 雷，沿用 Round 1 numpy 教训）。若某平台 conda 未带，CI 会红 → 届时在 micromamba create 命令显式加 `matplotlib`。
2. **matplotlib Agg 必须无 GUI**：`matplotlib.use("Agg")` 在 import pyplot 前调用；server 已设 `QT_QPA_PLATFORM=offscreen`。
3. **MCP `Image` 返回**：`mcp.server.fastmcp.Image(data, format)` → ImageContent；客户端（Claude Desktop）内联显示。黑盒测试将真实验证客户端可见。
4. **glTF 逐面索引偏移**：每 face tessellate 的局部顶点索引需加全局偏移再 concat；slow 测试用 `GLTF2().load` 回读校验结构正确。
5. **render 质量**：MVP 级（matplotlib 多边形着色），够"看清形状/朝向"；FreeCADGui 离屏高质量留 Round 4。

---

## Verification（端到端验收）

1. **单元层（快）**：`uv run ruff check . && uv run pytest -q` 全绿（`mesh_to_png` 出 PNG、`build_glb` 出合法 glb、server render_part 返回 Image、握手纯净 server 不 import FreeCAD/matplotlib）。
2. **地基层（慢，本机）**：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -v -s` → 真机 `render_png` 出 PNG、`export_gltf` 出合法 .glb；另存一张 PNG 人眼确认形状。
3. **黑盒层（真实 MCP 客户端）**：复用 `.vibecad/blackbox_client.py` 增 `render_part`→拿到 ImageContent（base64 PNG，能存盘打开看图）+ `export_part(fmt="all")`→STEP/STL/glb 齐。**这是"用户能看见"的最终证据**。
4. **CI**：五平台 unit + 三平台 runtime-integration（含视觉端到端）绿。

满足 1–3（尤其 3 拿到可见 PNG）即证明"用户看得见、能迭代"成立，可进入 Round 4（位置控制 + 更多特征 / FreeCADGui 高质量渲染 / glTF 客户端交互）。

---

## 范围纪律
仅 PNG 软渲染（matplotlib Agg）+ glTF 导出（pygltflib 逐面）+ render_part 工具 + export glTF。无 FreeCADGui 高质量渲染、无位置控制、无新建模特征、无装配、无 agent 接入。

---

## 实施与验收记录（2026-06-09 · macOS arm64 实机）

Round 3 已按 5 个 TDD 任务全部实施（分支 `feat/round3-visual`），快测试 **84 passed**、ruff clean、握手纯净（server 不 import FreeCAD/matplotlib，二者懒加载在函数内）。

**渲染路径实机探针结论**：matplotlib Agg（纯 CPU 无 GPU）**可用**；trimesh 离屏**不可用**（缺 pyglet/GL）；pygltflib 在 env。故 PNG = matplotlib Agg + 逐面法向朗伯明暗；glTF = pygltflib 逐面 primitive + 面级 extras。

**真机慢测试全过**：`render_png` 出 PNG、`export_gltf` 出 6-primitive 合法 .glb、端到端 `render_and_gltf`。

**黑盒视觉验收（真实 MCP 协议）**：`render_part` 回传 `ImageContent`（image/png, ~21KB），用户在客户端**内联可见**；`export_part(fmt="all")` 产出 STEP 8246B + STL 61748B + glTF 9620B。**亲眼确认渲染图**：蓝色 3D 实体、顶面亮/侧面暗、左侧竖条曲面=圆柱切口，形状/朝向/比例/切口可读。

**迭代发现**：matplotlib 默认单色平面着色 → 3D 形体读不出细节（孔看不见）；加**逐面法向朗伯明暗**后形体可读。再次印证「看真实输出再修」的价值。

渲染为 **MVP 级**（顶面三角化有放射纹；孔在角上是因尚无位置控制）；FreeCADGui 高质量离屏渲染 + 图元位置控制留 Round 4。
