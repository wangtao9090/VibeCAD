# VibeCAD Round 2 — 语义建模 Walking Skeleton Implementation Plan（v1）

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（逐任务 实现→spec/质量审查→修复）。Steps 用 `- [ ]` 勾选。

**Goal:** 在已落地的运行时之上，打通产品核心闭环——**自然语言 → 参数化单零件 → 可制造文件（STEP/STL）**。交付 `engine` 进程内 FreeCAD 封装（文档/事务/recompute/几何断言/checkpoint）+ Walking Skeleton 语义工具（new_document/add_box/add_cylinder/boolean_cut）+ 导出（STEP/STL）+ 文本诊断反馈，并以**真机慢测试**端到端验证。

**范围（垂直切片，已锁定）：** 仅 Part 参数化图元（`Part::Box`/`Part::Cylinder`）+ 参数化布尔（`Part::Cut`）+ 导出 + 文本诊断。**不含**：glTF/软渲染、装配、规则引擎、零件库、agent 接入、草图/拉伸/旋转。

**复用 Round 1（已合入 main）：** `server.py` 的 `_in_conda_runtime()`/`_installer.is_ready()`/三态守卫模式、`runtime/*`（`paths.active_runtime_python()`、`status._PREP`）、`tests/test_runtime_integration.py` 的真机慢测试范式（`@slow` + `VIBECAD_RUN_INTEGRATION=1` + conda env python 子进程）。

---

## Context（为什么做这件事）

Round 1 已实机验证地基 A1/A2/A3（进程内 import FreeCAD / micromamba 装 freecad / re-exec 进 conda python 跑 server）。但这只证明了"能跑 FreeCAD"，还没证明**产品价值闭环**。Round 2 用最小垂直切片证明：在受限语义词汇表（spec §1.2 LLM 能力悬崖内）下，能造出一个参数化零件并导出可制造文件——这是 M1 单零件 MVP 的骨架。延续 spec §2.4 实机固化的工程纪律：①创建后必 recompute ②`recompute()`/`solve()` 返回值不可信 ⇒ **强制几何断言** ③每操作一个事务+undo。

---

## Architecture

```
MCP Client ──stdio──> server.py @mcp.tool(add_box,…)         [握手进程；守卫 ready+in_conda_runtime]
                          │ 委托
                          ▼
                       tools/modeling.py  add_box(session,…)  [参数校验(纯 Python) → 事务]
                          │
                          ▼
                       engine/session.py  Session             [单活动文档 + 事务 + recompute + 几何断言 + checkpoint]
                          │  懒加载（仅 conda runtime 进程）
                          ▼
                       freecad_env.py  prepare_freecad_import() + silence_fd1()
                          │
                          ▼  import FreeCAD, Part（在 silence_fd1 内）
                       doc.addObject("Part::Box") → recompute → assert_valid_solid → commit → checkpoint(.FCStd)
```

**纪律：** server 模块级**仍不 import FreeCAD**（保握手秒回）；engine/tools 懒加载 FreeCAD（函数内，先 `prepare_freecad_import()`，在 `silence_fd1()` 内 import）；所有进程内 FreeCAD 工作都包在 `silence_fd1()`（防 OCCT 写 fd1 污染 JSON-RPC）。`_session` 在 server 模块级单例，跨 MCP 调用维持同一文档（单零件先行）。

---

## File Structure

```
src/vibecad/
├── freecad_env.py     新建  prepare_freecad_import() + silence_fd1()（纯 stdlib，不 import FreeCAD/mcp）
├── engine/
│   ├── __init__.py    改    re-export Session
│   └── session.py     新建  Session：文档生命周期 + 事务 + recompute + assert_valid_solid + checkpoint
├── tools/
│   ├── __init__.py    改    re-export modeling/export
│   ├── modeling.py    新建  new_document/add_box/add_cylinder/boolean_cut（参数化对象）
│   └── export.py      新建  export_part（STEP/STL）
├── feedback/
│   ├── __init__.py    改    re-export text
│   └── text.py        新建  describe_shape（volume/bbox/com/solid_count/validity）
└── server.py          改    import freecad_env; 注册 6 新 @mcp.tool + 模块级 _session
tests/
├── conftest.py        新建  session 级 runtime_env fixture（复用同一 FreeCAD env，避免重下）
├── test_freecad_env.py / test_engine_session.py / test_tools_modeling.py /
│   test_tools_export.py / test_feedback_text.py / test_server_new_tools.py   新建
└── test_runtime_integration.py  改  追加 test_walking_skeleton 端到端慢测试
pyproject.toml / .github/workflows/ci.yml / README.md  改
.gitignore  改  忽略 .vibecad-test-runtime/
```

---

## 关键约定（两处强化，务必遵守）

**1. 慢测试 import 路径 = `<repo>/src`（不是 `<repo>`）。** 包在 `src/` 布局下。所有慢测试子进程代码前缀：
```python
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")
# 子进程代码：status._PREP + f"import sys; sys.path.insert(0, {_SRC!r})\n" + "<真实 engine/tools 代码>"
```
插 `<repo>/src` 到最前，让 env python import **最新源码**（而非 env 里 pip 装的快照），dev 改完即测、无需重装。

**2. `tests/conftest.py` 提供 session 级 `runtime_env` fixture，所有 Round-2 慢测试复用同一 FreeCAD env（关键：避免每条慢测试重下 2-3GB）：**
```python
# tests/conftest.py
import os, pytest
from pathlib import Path

@pytest.fixture(scope="session")
def runtime_env():
    """返回就绪的 conda env python 路径；复用既有 env，必要时只装一次。
    优先级：VIBECAD_FREECAD_ENV override > 固定缓存 VIBECAD_HOME=<repo>/.vibecad-test-runtime。"""
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1")
    repo = Path(__file__).resolve().parent.parent
    if not os.environ.get("VIBECAD_FREECAD_ENV"):
        os.environ.setdefault("VIBECAD_HOME", str(repo / ".vibecad-test-runtime"))
    from vibecad.runtime import paths, status
    from vibecad.runtime.installer import RuntimeInstaller
    if not status.runtime_ready():
        RuntimeInstaller().install()          # 幂等：sentinel 就绪则秒回
    assert status.runtime_ready()
    return str(paths.active_runtime_python())
```
> Round-2 建模慢测试用此 fixture（**复用 env**）；Round-1 的 `test_install_and_smoke` 仍保持自己 tmp 装一次（它的职责就是测安装路径，勿动）。CI 里每个 runtime-integration job 由 fixture 装一次、被多条慢测试复用。本地反复跑只在首次装（约 4 分钟），后续秒进。`.vibecad-test-runtime/` 加入 `.gitignore`。

---

## Task 1：共享 FreeCAD 引导 `freecad_env.py` + server 重构

**Files:** Create `src/vibecad/freecad_env.py`, `tests/test_freecad_env.py`; Modify `src/vibecad/server.py`, `tests/test_server_tools.py`

> 解循环依赖：engine 需要 `prepare_freecad_import`/`silence_fd1` 但不能 import server。把这两个函数从 server.py 迁到 `freecad_env.py`（纯 stdlib），server 改为 `from vibecad.freecad_env import prepare_freecad_import as _prepare_freecad_import, silence_fd1 as _silence_fd1`，删除本地定义。

- [ ] **Step 1: 失败测试**（纯单元，无 FreeCAD）
  - `test_prepare_adds_lib_to_syspath`：monkeypatch `vibecad.freecad_env.sys.prefix`=tmp → 调用后 `<tmp>/lib`（非 win）在 `sys.path`；含 win 分支（monkeypatch `sys.platform="win32"` → `Library/bin` 注入）。
  - `test_silence_fd1_restores`：进出 contextmanager 后 fd1 正常。
  - `test_server_reexports`：`vibecad.server._prepare_freecad_import is vibecad.freecad_env.prepare_freecad_import`。
  - 迁移 `tests/test_server_tools.py::test_prepare_freecad_import_adds_module_dir`：monkeypatch 目标改 `vibecad.freecad_env.sys`。
- [ ] **Step 2: 跑红 → 实现**（剪切两函数，去 `_` 前缀；server 用 `as` 别名保持内部命名）
- [ ] **Step 3: 绿 + ruff**：`uv run pytest -q`（应 41+新增、全绿）、`uv run ruff check .`
- [ ] **Step 4: commit** `refactor: extract freecad_env (prepare_import + silence_fd1) shared by server+engine`

---

## Task 2：Session 骨架 `engine/session.py`（纯单元，mock FreeCAD）

**Files:** Create `src/vibecad/engine/session.py`; Modify `src/vibecad/engine/__init__.py`; Create `tests/test_engine_session.py`

> 先实现不需要真实 FreeCAD 的部分：构造、事务包装、几何断言。`Session.__init__` 不 import FreeCAD。

**接口：**
```python
class Session:
    def __init__(self, checkpoint_dir: Path | None = None): self._doc=None; self._loaded=False; ...
    @property
    def doc(self): return self._doc                      # 未 open 前 None
    def _ensure_freecad(self): ...                       # 首次：prepare_freecad_import()，置 _loaded
    @contextlib.contextmanager
    def _transaction(self, label: str): ...              # openTransaction/commitTransaction；异常 abortTransaction
    def assert_valid_solid(self, shape) -> None:         # not isValid → RuntimeError("几何断言…")；Volume<=0 → RuntimeError("体积为零")；isNull 同理
    def get_object(self, name: str): ...                 # doc.getObject(name)；缺失 raise KeyError
```

- [ ] **Step 1: 失败测试**（mock `FakeDoc` 记录 open/commit/abort 调用）：`test_session_starts_without_freecad`（`doc is None`，构造不 import FreeCAD）、`test_transaction_calls_open_commit`、`test_transaction_aborts_on_exception`、`test_assert_valid_solid_raises_on_invalid/zero_volume`、`test_get_object_missing_raises`。
- [ ] **Step 2-3: 实现 + 绿 + ruff**（`_transaction` try/except/finally；`assert_valid_solid` 查 `isValid()` & `not isNull()` & `Volume>0`）
- [ ] **Step 4: commit** `feat(engine): Session skeleton (transaction + geometric assertion)`

---

## Task 3：conftest fixture + Session 真机生命周期（slow）

**Files:** Create `tests/conftest.py`; Modify `src/vibecad/engine/session.py`（`open_document`/`close_document`/`_checkpoint`/`get_result_shape`）, `tests/test_engine_session.py`, `.gitignore`

> 实现真实文档生命周期 + checkpoint。新增 `runtime_env` fixture（见上）。

**实现要点（FreeCAD API）：** `open_document(name)` → `import FreeCAD`（在 `silence_fd1` 内，先 `_ensure_freecad`）→ `self._doc = FreeCAD.newDocument(name)`；`close_document` → `FreeCAD.closeDocument(doc.Name)` + 清空；`_checkpoint` → 每次 `commitTransaction` 后 `doc.saveAs(str(checkpoint_dir/f"{doc.Name}.FCStd"))`（首存后续可 `doc.save()`）；`get_result_shape()` → 遍历 `doc.Objects`，优先末个 `Part::Cut/Fuse/Common`，否则末个有 `.Shape` 的 solid，皆无 raise RuntimeError。

- [ ] **Step 1: 慢测试** `test_session_open_close_checkpoint(runtime_env)`：子进程在 env python 跑 `Session().open_document('t'); assert doc; saveAs 落 .FCStd; close`，校验 checkpoint 文件存在。
- [ ] **Step 2-3: 实现 + 绿**（`uv run pytest -q` 仍全绿；`VIBECAD_RUN_INTEGRATION=1 ... -m slow tests/test_engine_session.py` 绿）
- [ ] **Step 4: commit** `feat(engine): real FreeCAD document lifecycle + .FCStd checkpoint`

---

## Task 4：建模工具 `tools/modeling.py`

**Files:** Create `src/vibecad/tools/modeling.py`; Modify `src/vibecad/tools/__init__.py`; Create `tests/test_tools_modeling.py`

**接口（每个 = 参数校验 → 事务 → 参数化对象 → recompute → assert_valid_solid → dict）：**
```python
def new_document(session, name) -> dict       # {"ok":True,"name":name}
def add_box(session, length, width, height)   # Part::Box；{"ok":True,"name":obj.Name,"volume":obj.Shape.Volume}
def add_cylinder(session, radius, height)     # Part::Cylinder；同上
def boolean_cut(session, base_name, tool_name)# Part::Cut，cut.Base=get_object(base_name), cut.Tool=get_object(tool_name)
```
**FreeCAD API：** `obj=doc.addObject("Part::Box","Box"); obj.Length/Width/Height=…; doc.recompute(); session.assert_valid_solid(obj.Shape)`。布尔：`cut.Base=base; cut.Tool=tool`（赋 **doc 对象**非字符串）；添加 Cut 前 `doc.recompute()` 防守，添加后再 recompute + 断言。返回 `obj.Name` 供后续引用。

- [ ] **Step 1: 失败测试** — 纯单元（mock session，无 FreeCAD）：`add_box/add_cylinder` 拒 0/负（`ValueError` 含字段名）、`boolean_cut` 拒空 name、`new_document` 返回 dict。 + **slow**（用 `runtime_env`）：`add_box(10,20,30)` → `volume≈6000` 且 `get_object(name).Length==10`（证参数化）；`boolean_cut`：box(10³) − cyl(r3,h15) → `0 < volume < 1000`。
- [ ] **Step 2-3: 实现 + 绿 + ruff**（懒 import 标 `# noqa: PLC0415`）
- [ ] **Step 4: commit** `feat(tools): walking-skeleton modeling (box/cylinder/cut, parametric)`

---

## Task 5：导出工具 `tools/export.py`

**Files:** Create `src/vibecad/tools/export.py`; Modify `tools/__init__.py`; Create `tests/test_tools_export.py`

```python
def export_part(session, output_dir, *, fmt="both") -> dict  # fmt∈{"step","stl","both"}；{"ok":True,"step":path|None,"stl":path|None}
```
**FreeCAD API：** `shape = session.get_result_shape()`；STEP `shape.exportStep(path)`；STL `shape.exportStl(path)`（Part.Shape 直接有；若版本缺失，回退 `import Mesh; Mesh.export([obj], path)`——集成测试确认）。文件名用 `doc.Name`。

- [ ] **Step 1: 失败测试** — 纯单元：拒非法 fmt（`ValueError`）、文件名后缀正确（mock get_result_shape）。 + **slow**：建 box(20³) → `export_part` → STEP/STL 均存在且 size>0。
- [ ] **Step 2-3: 实现 + 绿 + ruff**
- [ ] **Step 4: commit** `feat(tools): export_part (STEP + STL)`

---

## Task 6：文本诊断 `feedback/text.py`

**Files:** Create `src/vibecad/feedback/text.py`; Modify `feedback/__init__.py`; Create `tests/test_feedback_text.py`

```python
def describe_shape(shape) -> dict  # {valid, volume, bbox:{x,y,z}, center_of_mass:[x,y,z], solid_count, shell_count}
```
**注意：** `shape.CenterOfMass` 是 `Base.Vector` → `[v.x,v.y,v.z]` 才可 JSON 序列化；`shape.Solids`/`shape.Shells` 取 `len`。

- [ ] **Step 1: 失败测试** — 纯单元（FakeShape）：返回含全部 key、值正确。 + **slow**：box(10³) → `valid True`、`volume≈1000`、`solid_count==1`。
- [ ] **Step 2-3: 实现 + 绿 + ruff**
- [ ] **Step 4: commit** `feat(feedback): text diagnostics (describe_shape)`

---

## Task 7：server 注册新工具 `server.py`（纯单元守卫）

**Files:** Modify `src/vibecad/server.py`; Create `tests/test_server_new_tools.py`

> 模块级 `_session = Session()`（不触发 FreeCAD import）。注册 6 个 `@mcp.tool()`：`new_document/add_box/add_cylinder/boolean_cut/export_part/describe_part`，每个**复用 smoke_cad 同款三态守卫**（未就绪→ok False"未就绪"；就绪但非 conda→ok False"重连"；否则委托 tools/feedback）。`describe_part` = `feedback.text.describe_shape(_session.get_result_shape())`。

- [ ] **Step 1: 失败测试** — 纯单元（全 monkeypatch）：每工具的 `guard_not_ready`/`guard_needs_reconnect`/`delegates`（验参数透传，如 `add_box(10,20,30)` 调 `modeling.add_box(_session,10,20,30)`）。
- [ ] **Step 2-3: 实现 + 绿 + ruff**（`uv run pytest -q` 全绿；server 模块级仍不 import FreeCAD——加回归断言 `import vibecad.server; 'FreeCAD' not in sys.modules`）
- [ ] **Step 4: commit** `feat(server): register modeling/export/describe tools with runtime guards`

---

## Task 8：端到端 Walking Skeleton 慢测试

**Files:** Modify `tests/test_runtime_integration.py`（**追加** `test_walking_skeleton`，勿动 `test_install_and_smoke`）

- [ ] **Step 1: 慢测试** `test_walking_skeleton(runtime_env)`：env python 子进程跑**真实 engine+tools**：`new_document → add_box(30³) → add_cylinder(r8,h40) → boolean_cut → export_part → describe_shape`，断言 `box.volume≈27000`、`0<cut.volume<27000`、STEP/STL size>0、`describe.solid_count==1` 且 `≈cut.volume`，打印 `SKELETON_OK`。
- [ ] **Step 2: 本机实跑**（macOS arm64，复用 fixture env，首次约装 4 分钟）：
  ```bash
  VIBECAD_RUN_INTEGRATION=1 UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    uv run pytest -m slow -v -s
  ```
  Expected：全部 slow PASS（含 Round-1 install + Round-2 建模/导出/诊断/skeleton）。
- [ ] **Step 3: commit** `test: end-to-end walking skeleton (box-cut-export) on real FreeCAD`

---

## Task 9：CI + README + 计划入库 + 飞书

**Files:** Modify `.github/workflows/ci.yml`, `README.md`; Create `docs/superpowers/plans/2026-06-09-round2-modeling.md`

- [ ] **Step 1: CI** — `runtime-integration` job 的 `uv run pytest -m slow tests/test_runtime_integration.py …` 已自动跑端到端 skeleton（同文件）；其余 Round-2 慢测试通过 fixture 复用 env。确认 CI 命令覆盖到（必要时把 `-m slow` 扩到 `tests/test_engine_session.py tests/test_tools_*.py tests/test_feedback_text.py`，但靠 fixture 复用避免重下；或保持仅 integration 文件 + skeleton 已足够端到端）。
- [ ] **Step 2: README** — 新增「语义建模工具」段：`new_document/add_box/add_cylinder/boolean_cut/export_part/describe_part` 用法 + 重连说明。
- [ ] **Step 3: 计划入库** `docs/superpowers/plans/2026-06-09-round2-modeling.md`（本计划 + 实施记录）。
- [ ] **Step 4: 飞书同步**（过程文档文件夹 `DwYlfjYTelFG1RdTiFhc3NfWnAh`）：
  `lark-cli docs +create --api-version v2 --parent-token DwYlfjYTelFG1RdTiFhc3NfWnAh --content @docs/superpowers/plans/2026-06-09-round2-modeling.md --doc-format markdown --format json`（注：含 workflow 改动的分支推送须走 **SSH**）
- [ ] **Step 5: commit** `chore: round2 CI + README + plan docs`

---

## FreeCAD API 风险（集成测试必确认）

1. **`Part::Cut` 的 Base/Tool** = `App::PropertyLink`，须赋 **doc 对象**（非名字）；cut 后 base/tool 在树中被消隐但 `cut.Shape` 有效。后续操作只引用 cut 的 Name。
2. **recompute 顺序**：每 `addObject` 后即 recompute；`boolean_cut` 添加 Cut 前再 recompute 防守。`recompute()` 返回值不可信 → 唯一可信成功判据 = `assert_valid_solid`（spec §2.4 规范②）。
3. **`Shape.exportStl`**：FreeCAD 1.1 的 Part.Shape 直接可用；若缺失回退 `Mesh.export`。慢测试仅验文件非零，mesh 质量留 Round 3。
4. **`FreeCAD.ActiveDocument` 全局**：Session 管理之；server 单例 `_session` 跨调用维持（预期）；慢测试经子进程隔离。
5. **checkpoint `saveAs` 需文档 open**：在 `commitTransaction` 后、`close_document` 前调用。
6. **Windows DLL/sys.path**：`prepare_freecad_import`（进程内）与 `status._PREP`（子进程）逻辑须对等；Task 1 含 win 分支单元覆盖；CI windows job 真验。

---

## Verification（端到端验收）

1. **单元层（快，全平台）**：`uv run ruff check . && uv run pytest -q` 全绿（freecad_env/session 骨架/modeling 参数校验/export 路径/feedback 结构/server 守卫，皆不碰真实 FreeCAD）。
2. **地基层（慢，本机 macOS arm64 必跑）**：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -v -s` → fixture 复用 env，真机跑通 `new_document→add_box→add_cylinder→boolean_cut→export(STEP/STL)→describe`，体积/包围盒/文件断言全过。**这是本轮达成的核心证据。**
3. **连接器层（手动，真实 MCP 客户端）**：`claude mcp add … uvx --from /Users/wangtao/DevProject/VibeCAD vibecad` → `ensure_runtime`→轮询→重连→`new_document("bracket")→add_box(50,30,20)→add_cylinder(8,25)→boolean_cut("Box","Cylinder")→export_part("/tmp/bracket")→describe_part`，STEP 可在 FreeCAD GUI 打开（方块被圆柱孔穿透）。
4. **CI**：五平台 unit 绿；ubuntu/macos/windows runtime-integration（含端到端 skeleton）绿。

满足 1–2（尤其 2）即证明产品核心闭环成立，可进入 Round 3（三级反馈 glTF / 更多语义特征 / 装配）。

---

## 范围纪律
仅 Part 图元 + 布尔 + 导出 + 文本诊断 + 6 工具；单活动文档（单零件先行）。无 glTF/软渲染、无装配、无规则引擎、无零件库、无 agent 接入、无草图/拉伸。

---

## 实施与验收记录（2026-06-09 · macOS arm64 实机）

Round 2 已按 9 个 TDD 任务全部实施（分支 `feat/round2-modeling`），快测试 **73 passed**、ruff clean、握手纯净保持（server 模块级不 import FreeCAD）。

**产品核心闭环已端到端真机验证通过**（`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow`，复用持久 `.vibecad-test-runtime` env，6 条慢测试 0.72s 全过）：
- engine 真机文档生命周期 + `.FCStd` checkpoint ✅
- `add_box`（Part::Box，参数化 `Length` 可回读）/ `add_cylinder` / `boolean_cut`（Part::Cut）真机体积断言 ✅
- `export_part` STEP/STL 文件非零 ✅；`describe_shape` 文本诊断 ✅
- 端到端 walking skeleton：`new_document → add_box(30³) → add_cylinder(r8,h40) → boolean_cut → export(STEP/STL) → describe`，体积/文件/诊断断言全过 ✅

**真机发现并修复**：`Part::Cut` 的 `.Shape` 是 `Part.Compound`（非 Solid），**无 `CenterOfMass` 属性**（但有 Volume/BoundBox/Solids）。单元测试用 FakeShape 带该属性而漏掉，靠端到端真机测试逮到。修复：`describe_shape` 的质心计算对 Compound 退到首个 Solid 的 `CenterOfMass`。再次印证「地基/边界假设必须拿真机砸」的价值。

**测试策略**：纯逻辑（参数校验/守卫/事务/dict 结构）走快单元（mock）；所有真实几何走 `@slow`，经 conda env python 子进程跑真实 engine+tools（`sys.path` 插 `<repo>/src` 用最新源码），session 级 `runtime_env` fixture 复用同一 FreeCAD env 避免重下 2-3GB。
