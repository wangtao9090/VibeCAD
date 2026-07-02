# Agent Orchestrator 最小原型 — 决策提案 + Implementation Plan（草案）

> **状态：Draft（草案）——本文档所有决策均为提案，待用户确认后生效，任何一条都可改。**
> 日期：2026-07-02
> 上游 Spec：`docs/superpowers/specs/2026-07-02-vibecad-agent-architecture-design.md`（Proposed）
> 范围锚点：架构文档 **阶段 2 — Agent Orchestrator 原型**（§11），验证标准对齐 §16 五条。
> **For agentic workers:** 本文档确认后按 superpowers:subagent-driven-development 或 superpowers:executing-plans 逐任务执行。Steps 使用 checkbox（`- [ ]`）语法跟踪。

---

# 第一部分：开放问题决策提案

> ⚠️ **草案：以下 7 条决策待用户确认后生效，任何一条都可改。**
> 逐条对应架构文档 §14 的 7 个开放问题。每条给出：推荐决策、理由（含代码现实依据）、被否选项的代价。

## 决策 1（§14.1）模型供应商与 BYOK：单 provider（Anthropic）+ 窄 Protocol 预留接口

**推荐决策：** 第一版只实现 `AnthropicProvider`；定义一个**窄** `ModelProvider` Protocol（`complete(messages, tools=None, schema=None) -> ModelReply`，`ModelReply` 携带文本/结构化 JSON/usage 三件事），不做流式、不做多厂商路由、不做完整抽象层。BYOK 第一版 = 读 `ANTHROPIC_API_KEY` 环境变量（天然 BYOK），模型名读 `VIBECAD_AGENT_MODEL`（带默认值），不做 key 管理 UI。

**理由：**
- 原型期 provider 的唯一消费方是 intent/planner/repairer 三个 prompt，接口面极小；抽象层没有第二个实现来校验，写了也只是猜。
- Anthropic tool-use 强制 JSON schema 输出，直接满足架构文档 §8.1 的"结构化输出"要求，planner/intent 不需要自己解析自由文本。
- usage（token/费用）记账进 TaskRun，满足 §8.6 成本可观测，不需要网关级功能。

**被否选项的代价：**
- *直接抽象多 provider*：无验证样本的抽象必然过度设计，拖慢闭环打通；且 OpenAI/本地模型的结构化输出机制不同，第一版就统一会把接口撑肥。
- *完全不留接口（直接 import anthropic 满天飞）*：后续换/加 provider 要全文件改。窄 Protocol 是两害相权的最小保险。

## 决策 2（§14.2）Agent 入口：MCP tool `agent_run`（复用现有 server 进程与测试基建）

**推荐决策：** 第一版入口是 server.py 新增一个 MCP 工具 `agent_run(request: str, output_dir: str | None = None) -> dict`，同步执行、返回 TaskRun 摘要（status / artifacts / run_dir / usage / summary）。不做独立 CLI、不做 HTTP。

**理由（代码现实，比架构文档更进一步）：**
- **FreeCAD 只能在 conda 运行时进程内 import**（`session.py` 首行纪律、`_runtime_guard`/换芯机制都为此而生）。`agent_run` 跑在 server 进程内，天然继承 R11 已解决的运行时引导、自动换芯、`_session` 生命周期——独立 CLI 要把这套全部重解一遍。
- 现有测试基建直接可用：`tests/test_server_*.py` 的范式就是**直接调用 server 模块级函数** + monkeypatch `_installer.is_ready` / `_in_conda_runtime`，`agent_run` 的单测零新基建。
- 分发面零新增：.mcpb / uvx 用户升级即获得 agent 能力，无新安装路径。
- 交互闭环成立：`needs_clarification` 结果由外部 AI 转述给用户，补参后重新调用 `agent_run`——MCP 工具的一问一答天然承载这个循环（见决策 7）。

**被否选项的代价：**
- *独立 CLI*：要自行处理 conda 进程引导 + 无现成分发渠道 + 无测试注入点，全是原型期不该碰的地基工程。
- *HTTP API*：引入服务生命周期/端口/鉴权问题，架构文档 §13 明确第一阶段不做平台化。

## 决策 3（§14.4）工具调用：in-process adapter，第一版不做 MCP self-call

**推荐决策：** `InProcessToolAdapter` 直接调用 `vibecad.server` 的模块级工具函数（`server.add_box(...)` 等），第一版**不做** MCP self-call backend。Adapter 接口按架构文档 §6.4 的 `CadToolAdapter` 定义，为阶段 3 的 MCP backend 留好位置。

**理由（此处基于代码现实反驳架构文档 §6.4"短期优先 MCP backend"的建议）：**
- **状态分裂是硬伤**：`_session` 是 server 进程的内存态（活动文档、`_parts` 注册表、标签快照全在其中）。MCP self-call 要 spawn 第二个 server 进程，它有**自己的空 `_session`**——用户在对话里已建的文档 agent 看不见，agent 建的文档对话也看不见。原型期这是最大的正确性风险，不是"多一层握手"那么轻。
- 架构文档偏好 MCP backend 的理由是"最能复用工具 schema、返回结构、测试资产"——但 in-process 直调 server 层函数复用的是**同一份代码**：`@mcp.tool` 装饰器（FastMCP）注册后返回原函数，`_runtime_guard`、异常→结构化 `{ok: False}`、`_attach_view` 附带的 `labels`/`parts`/`view_file` 全部原样拿到；工具 schema 可从 FastMCP 注册表读出注入 planner prompt。复用度打平，复杂度少一个进程 + 一次 stdio 握手 + 图片跨进程传输。
- 现有测试就是这么调的（见决策 2），in-process 路径是已被验证的路径。

**被否选项的代价：**
- *MCP self-call*：双进程双 `_session` 状态分裂（需要额外发明跨进程会话同步才能修复）、启动/握手/换芯逻辑翻倍、Image 内容序列化开销。原型期全是纯支出。
- 推迟的收益要记账：MCP backend 对"agent 与工具分离部署""对接第三方 MCP 工具"仍有长期价值——留在阶段 3，由统一 `CadToolAdapter` 接口保底。

## 决策 4（§14.3）TaskRun 存储：JSON 文件目录（目录即 telemetry）

**推荐决策：** `<VIBECAD_HOME>/agent_runs/<run_id>/` 目录：`run.json`（TaskRun 全量，按架构文档 §9 数据模型）+ `steps/NNN-<tool>.json`（逐步增量落盘）+ artifacts 引用（view PNG 路径复用 `feedback/persist.py` 的 views 落盘，导出文件在 output_dir）。不引入 SQLite。

**理由：**
- 目录即 telemetry：eval runner 直接 `json.load` 读结果做断言，调试直接 `cat`，符合项目"纯 stdlib、可解释"纪律（uninstall/persist 皆此风格）。
- 单 server 单 agent 串行运行，无并发写需求；逐步增量落盘保证进程崩溃后 run 记录仍可读（可回放性 §5.1）。
- 与 `views/` 目录的滚动保留先例一致，可加同样的滚动清理。

**被否选项的代价：**
- *SQLite*：schema 迁移负担 + 调试不如肉眼可读 + 解决的是不存在的并发问题。等出现"跨 run 查询/统计"的真实需求（阶段 4 memory / 阶段 5 大规模 eval）再迁，届时 JSON 也能一次性导入。

## 决策 5（§14.5）图片理解：第一版模型不看图，verifier 只用几何事实

**推荐决策：** 原型期模型（planner/repairer）**不接收任何图片**；面/边指认消费 `render_part(annotate='faces')` 返回的**标签表文本**（标签→尺寸/归属描述）；verifier 是纯确定性代码，消费 `describe_part`（bbox/体积/实体数/有效性）、工具返回（`volume`/`ok`/`message`）、导出文件事实（存在 + 非零，export 层已有 `_assert_written` 后置断言）。

**理由：**
- 架构文档 §8.4 的第一原则就是"可信度来自几何验证，不来自模型自信"；现有工具返回的结构化事实已足够覆盖原型 4 类任务的全部验证（见第三部分断言列表）。
- 每步图片进上下文 = token 成本线性膨胀（§8.2 明确"图片优先转成结构化标签和几何事实"）。
- `view_file` 路径已逐步记录进 TaskRun——视觉复核的钩子已留好，后续想加视觉模型检查工程图时数据现成。

**被否选项的代价：**
- *多模态 verifier*：验证结论主观化、不可回归、成本高。CAD 恰恰是少数能全靠数值验证的领域，放弃这个优势没有道理。

## 决策 6（§14.6）用户确认粒度：制造参数缺失必问，低风险默认值可假设但必须申明

**推荐决策：** 原型期二分法：
- **必问（返回 `needs_clarification`，不猜、不执行写操作）**：关键尺寸、孔径、导出格式缺失，以及用户显式给定的参数撞上几何守卫（如孔位越界）——改用户给的数字 = 改需求，必须问。
- **可假设（继续执行，写进 `assumptions` 并在 final summary 申明）**：单位为 mm、孔深省略=通孔、位置未说=居中、文档名未说=自动命名。
- 原型**不做** `awaiting_confirmation` 中途暂停态：`agent_run` 是单次调用，缺参在 intent 阶段就拦下整个 run（fail-fast），不存在"执行到一半停下等确认"。写操作全部限于 Normal Write 级白名单工具（§8.3），无 Risky Write，policy gate 推迟。

**理由：** 与架构文档 §8.5 的"需要确认清单"一致；fail-fast 版本把确认压缩到 run 边界，实现成本最低且行为可预测。**被否选项的代价：** *全都问* → `agent_run` 退化成表单，体验差；*全都猜* → 制造错误文件，违反 CAD agent 底线；*做中途暂停态* → 需要 run 挂起/恢复持久化机制，原型期范围爆炸。

## 决策 7（§14.7）eval 任务集：第一版 10 个高质量任务

**推荐决策：** 10 个任务（清单见第三部分），全部带机器可判定断言，来源于 `docs/ACCEPTANCE_TESTS.md` 已人工校准过的几何数值（A4–A8 的体积/尺寸口径直接转译）。

**理由：** 验收文档的数值已经过真机校准（含 slot 语义分歧这类必须人工裁定的口径），转译成本低、信号密度高；10 个任务覆盖 4 类任务型 + 3 类失败恢复，足以回答"原型是否成立"。**被否选项的代价：** *30–50 个* → 断言质量摊薄（大量任务只是参数变体，不增加信息量）、单轮 eval 成本高（每任务多次模型调用）、失败分析被淹没。任务集扩张放在阶段 5，且应由原型期暴露的失败模式驱动，而不是预先编造。

---

# 第二部分：最小原型 Implementation Plan（草案）

**Goal:** 打通阶段 2 单零件任务闭环：一句自然语言 → intent JSON → plan JSON → in-process 调现有 CAD 工具 → 确定性几何验证 → 失败自动恢复（三类）→ 导出 + 摘要；并用 10 个 eval 任务可重复度量。

**Architecture:** `agent_run` MCP 工具（server 进程内）→ `agent/loop.py` 状态机（created→parsing→planning→executing→verifying→repairing→finalizing）→ `AnthropicProvider` 结构化输出 → `InProcessToolAdapter` 白名单直调 server 工具函数 → `verify.py` 纯代码几何断言 → TaskRun JSON 目录落盘。

**Tech Stack:** 现有栈 + `anthropic` SDK（**放 optional-dependencies `agent` extra**，主包/.mcpb 分发不膨胀）；其余纯 stdlib（state/verify/repair/toolbox 无新依赖）；eval 任务集用 JSON（不引入 pyyaml）。

**Spec:** `docs/superpowers/specs/2026-07-02-vibecad-agent-architecture-design.md`

**模型分配建议：** Task 0 / Task 5 / Task 6（prompt 契约、修复策略、主循环状态机）→ 高阶模型；其余任务 → sonnet 级。

## 模块裁剪：从架构文档 14 个模块砍到 7 个文件

架构文档 §4.2 列了 14 个模块，原型只建闭环必需的最小子集：

```
src/vibecad/agent/
├── __init__.py
├── state.py      # TaskRun / Step dataclass + JSON 增量落盘（兼任 telemetry）
├── model.py      # ModelProvider Protocol + AnthropicProvider（单 provider）
├── prompts.py    # intent / planner / repairer prompt 模板 + 输出 JSON schema
├── toolbox.py    # InProcessToolAdapter：白名单 + 直调 server 函数 + 结果归一化
├── verify.py     # 确定性验证器（bbox/体积/导出文件）——纯代码，不调模型
├── repair.py     # 失败分类器 + 三类恢复策略
└── loop.py       # 状态机主循环（合并 orchestrator/intent/planner/executor 职责）
evals/
├── tasks.json    # 10 个 eval 任务（第三部分）
└── run_evals.py  # eval runner：逐任务 agent_run → 断言 → 汇总报告 JSON
```

**砍掉的模块与理由（7 个）：**

| 被砍模块 | 理由 |
|---|---|
| `reviewer.py` | 原型期"挡坏计划"由两层确定性代码承担：toolbox 白名单 + 参数 schema 校验（不存在的工具/缺参/坏类型直接拒），加 planner prompt 内置自检清单。第二次模型调用的边际收益在 4 类简单任务上不值一个模块。 |
| `memory.py` | 单任务闭环无跨任务记忆；会话内状态就在 `_session` 与 TaskRun 里。阶段 4 再建。 |
| `context.py` | 原型上下文 = 当前目标 + 计划 + 最近工具结果摘要，一个函数的事，不需要检索/压缩框架。 |
| `summarizer.py` | final summary 由 loop 末尾模板拼装（几何事实 + artifacts + assumptions），不调模型。 |
| `telemetry.py` | 决策 4：TaskRun JSON 目录即 telemetry，`state.py` 落盘时顺手记 usage/失败原因。 |
| `intent.py` / `planner.py` / `executor.py`（独立文件） | 三者合并进 `loop.py`（prompt 在 `prompts.py`）。原型期各自不足百行，拆文件只增加跳转成本；等装配任务/Plan IR（§6.2 长期项）逼出复杂度再拆。 |
| `evals.py`（包内） | eval runner 放 `evals/run_evals.py` 脚本（与 `tasks.json` 同目录），不进运行时包——它是开发资产不是产品功能。 |

**状态机裁剪：** 保留 `created / parsing / needs_clarification / planning / executing / verifying / repairing / finalizing / succeeded / failed`；砍 `reviewing_plan`（无 reviewer）、`awaiting_confirmation`（决策 6 fail-fast）、`cancelled`（同步调用无取消通道）。

**失败恢复裁剪（对齐架构文档 §16 验证标准 4，只覆盖三类）：**

| 失败类型 | 恢复策略 | 对应 §5.2 |
|---|---|---|
| 标签过期（`LabelExpiredError` 文案特征："未知面标签/尚未在标注图中展示"） | 自动插入 `render_part(annotate='faces')` 重标注 → 用新标签表重写该步参数 → 重试（≤2 次） | "自动重新渲染/重新标注，再继续" |
| 参数缺失 | intent 阶段拦截 → `needs_clarification` + 问题清单，不执行写操作 | "询问用户，不猜关键制造参数" |
| 几何断言失败（"几何断言失败/开口缺口/孔间重叠…"，工具层已自动回滚） | repairer 分类：agent 自己推导的参数 → 修正重试一次；用户显式给定的参数 → `needs_clarification`（决策 6） | "回滚，换更保守方案" |

其余类型（干涉、工具不支持、连续失败）统一走"响亮失败 + 失败链路写入 TaskRun"，不做专门恢复。硬限制沿用 §5.2：单 step 重试 ≤2，单 task repair ≤3 轮，另加 `max_tool_calls=12`、`max_model_calls=8`。

**工具白名单（planner 可见，9 个）：** `new_document` / `add_box` / `add_cylinder` / `add_hole` / `extrude_profile` / `modify_part` / `render_part` / `describe_part` / `export_part`。装配四件套、transform、boolean_cut、fillet/chamfer 不进白名单（超出 4 类任务范围；boolean 语义已被 add_hole/extrude_profile pocket 覆盖）。

---

## Task 0：Prompt / 结构化输出 Spike（硬门）

**目的：** 用真实 API 手跑验证两件事，结论决定 Task 6 的 prompt 契约：
- **Q1**：Anthropic tool-use 强制 JSON schema 能否稳定产出架构文档 §6.1/§6.2 形状的 intent/plan JSON（3 条样例需求：E01 简单板 / E05 阵列孔 / E08 缺参）？缺参样例是否可靠触发 `missing_info` 非空？
- **Q2**：单任务闭环的模型调用成本量级（intent+plan+1 次 repair 的 token/费用），校准 `max_model_calls` 与 eval 的 `max_cost_usd` 限额。

**Files:** Create: `.vibecad/spike-agent/spike_prompts.py`（一次性脚本，不进发布包）

- [ ] **Step 1:** 写 spike 脚本：硬编码 3 条需求 + intent/plan JSON schema，直调 anthropic SDK，打印 JSON 与 usage。
- [ ] **Step 2:** 真跑 3 条（需 `ANTHROPIC_API_KEY`），人工检查 JSON 形状/缺参识别/成本。
- [ ] **Step 3:** 结论回填本节"Spike 结果"，锁定 `prompts.py` 的 schema 与限额。**硬门：Q1 不达标不得进 Task 6。**

### Spike 结果（执行后回填）

- Q1 结构化输出稳定性：＿＿
- Q2 单任务成本量级：＿＿

---

## Task 1：`state.py` — TaskRun/Step + JSON 增量落盘

**Files:** Create: `src/vibecad/agent/__init__.py`, `src/vibecad/agent/state.py`, `tests/test_agent_state.py`

- [ ] **Step 1: 失败测试**（不依赖 FreeCAD，monkeypatch `VIBECAD_HOME` → tmp_path）

```python
# tests/test_agent_state.py（代表性用例）
def test_taskrun_persists_incrementally(monkeypatch, tmp_path):
    """new_run 建目录写 run.json；append_step 落 steps/001-*.json 并刷新 run.json。"""
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    run = state.TaskRun.new("做一个底板")
    run.append_step(state.Step(tool="add_box", args={"length": 60}, result={"ok": True}))
    d = tmp_path / "agent_runs" / run.id
    assert (d / "steps" / "001-add_box.json").exists()
    assert json.loads((d / "run.json").read_text())["status"] == "created"


def test_taskrun_reload_roundtrip(monkeypatch, tmp_path):
    """load(run_id) 读回等价对象——eval runner 与崩溃回放的基础。"""
```

- [ ] **Step 2:** 确认失败 → 实现：dataclass 字段按架构文档 §9（id/status/user_request/intent/plan/steps/artifacts/usage/assumptions/repair_log/created_at/updated_at）；`run_id = "run_" + 时间戳 + 4hex`；每次状态变更全量重写 run.json（小文件，简单优先）。
- [ ] **Step 3:** 测试过 + ruff。
- [ ] **Step 4: Commit** — `feat(agent): TaskRun/Step state with incremental JSON persistence`

---

## Task 2：`model.py` — ModelProvider Protocol + AnthropicProvider

**Files:** Create: `src/vibecad/agent/model.py`, `tests/test_agent_model.py`; Modify: `pyproject.toml`（`[project.optional-dependencies] agent = ["anthropic>=…"]`）

- [ ] **Step 1: 失败测试**（mock anthropic client，不打真网）

```python
def test_complete_with_schema_returns_parsed_json(fake_client):
    """schema 路径走 tool-use 强制输出；返回 ModelReply(parsed=dict, usage 记账)。"""


def test_missing_api_key_raises_configuration_error():
    """无 ANTHROPIC_API_KEY → 结构化 ConfigurationError（供 agent_run 转 {ok:False}），不 traceback。"""
```

- [ ] **Step 2:** 实现：`ModelProvider` Protocol（`complete(messages, schema=None) -> ModelReply`）；`AnthropicProvider` 懒 import anthropic（未装 extra 时报"请安装 vibecad[agent]"）；模型名读 `VIBECAD_AGENT_MODEL`；usage 累计（tokens/estimated_cost）。**key 不写入任何日志与 TaskRun**（§8.7）。
- [ ] **Step 3:** 测试过 + ruff。
- [ ] **Step 4: Commit** — `feat(agent): ModelProvider protocol + Anthropic provider with structured output`

---

## Task 3：`toolbox.py` — InProcessToolAdapter（白名单 + 结果归一化）

**Files:** Create: `src/vibecad/agent/toolbox.py`, `tests/test_agent_toolbox.py`

- [ ] **Step 1: 失败测试**（复用 `tests/test_server_new_tools.py` 的 monkeypatch 范式：patch `server._installer.is_ready`→True、`server._in_conda_runtime`→True、render 链→假 PNG）

```python
def test_call_tool_normalizes_attach_view_result(monkeypatch):
    """server.add_box 返回 [dict, Image] → 归一化为纯 dict（Image 剥离，view_file 保留）。"""


def test_call_tool_rejects_non_whitelisted():
    """align_parts 不在白名单 → 响亮 ToolNotAllowed，不触碰 server。"""


def test_render_part_json_payload_parsed(monkeypatch):
    """render_part(annotate='faces') 的 [Image, json_str] → {'ok': True, 'labels': {...}}。"""


def test_tool_specs_cover_whitelist():
    """list_tools() 输出 9 个白名单工具的 name+签名描述（注入 planner prompt 的原料）。"""
```

- [ ] **Step 2:** 实现：白名单表 `{name: server 函数}`（`@mcp.tool` 注册后模块名仍绑定原函数，直调即复用 guard/结构化错误/`_attach_view`）；归一化规则——`[dict, Image]` 取 dict、`[Image, json_str]` 解析 json、纯 dict 原样、`Image` 单独返回视为无结构化载荷错误；`ToolResult(ok, payload, error_kind)`，`error_kind` 由 message 文案分类（label_expired / geometry_assert / validation / runtime_not_ready），供 repair 消费。
- [ ] **Step 3:** 测试过 + ruff。
- [ ] **Step 4: Commit** — `feat(agent): in-process tool adapter with whitelist and result normalization`

---

## Task 4：`verify.py` — 确定性几何验证器

**Files:** Create: `src/vibecad/agent/verify.py`, `tests/test_agent_verify.py`

- [ ] **Step 1: 失败测试**（纯函数，输入伪造的 describe/工具返回 dict，不依赖 FreeCAD）

```python
def test_bbox_check_with_tolerance():
    """expect bbox [60,40,10] ±0.1 vs describe_part 返回 → pass/fail + 差值报告。"""


def test_volume_check_interval():
    """体积区间断言（打孔任务：基体体积 − n·π·r²·depth ± tol）。"""


def test_export_check_reads_file_facts(tmp_path):
    """导出验证 = 路径存在 + 大小非 0 + 扩展名匹配（复核 export 层 _assert_written）。"""
```

- [ ] **Step 2:** 实现：`check_bbox / check_volume / check_export / check_tool_ok` 四个纯函数 + `Verification(passed, facts, failures)` 汇总结构；孔数验证走**体积差法**（describe_part 无孔计数字段——预期体积区间由 plan 的 verification 节声明）。**不调模型**（§8.6"Verifier 尽量使用确定性代码"）。
- [ ] **Step 3:** 测试过 + ruff。
- [ ] **Step 4: Commit** — `feat(agent): deterministic geometry verifier (bbox/volume/export facts)`

---

## Task 5：`repair.py` — 失败分类 + 三类恢复策略

**Files:** Create: `src/vibecad/agent/repair.py`, `tests/test_agent_repair.py`

- [ ] **Step 1: 失败测试**（伪造 ToolResult 驱动，不依赖 FreeCAD/模型）

```python
def test_label_expired_yields_reannotate_then_retry():
    """error_kind=label_expired → RepairAction(insert=render_part(annotate='faces'),
    then=rewrite_args_from_new_labels, retry same step)；重试计数 +1。"""


def test_geometry_failure_on_user_given_param_asks_user():
    """孔越界且孔位来自用户显式参数 → RepairAction(kind='clarify')，不擅改需求。"""


def test_geometry_failure_on_agent_assumed_param_retries_once():
    """agent 自己推导的 offset 撞守卫 → 修正参数重试一次；再失败 → abort 并保留失败链路。"""


def test_hard_limits_stop_repair():
    """step 重试 >2 或 task repair >3 轮 → RepairAction(kind='abort')。"""
```

- [ ] **Step 2:** 实现：输入（失败 step、ToolResult.error_kind、该参数来源标记 user_given/assumed、重试计数）→ 输出 `RepairAction(kind: retry|revise|reannotate_retry|clarify|abort, …)`。参数来源标记由 intent 阶段生成（用户给的数字 vs assumptions）。规则优先、无模型调用；`revise` 需要新参数时才回调 planner（模型），且计入 `max_model_calls`。
- [ ] **Step 3:** 测试过 + ruff。
- [ ] **Step 4: Commit** — `feat(agent): failure classifier + three-way repair policy (labels/params/geometry)`

---

## Task 6：`loop.py` + `prompts.py` — 状态机主循环

**Files:** Create: `src/vibecad/agent/loop.py`, `src/vibecad/agent/prompts.py`, `tests/test_agent_loop.py`

- [ ] **Step 1: 失败测试**（FakeProvider 返回预录 intent/plan JSON；FakeAdapter 返回预录工具结果——全链路无网、无 FreeCAD）

```python
def test_happy_path_reaches_succeeded():
    """一句需求 → parsing→planning→executing→verifying→finalizing→succeeded；
    TaskRun 记录 intent/plan/steps/verification/usage/summary。"""


def test_missing_param_short_circuits_to_needs_clarification():
    """intent.missing_info 非空 → 不进 planning，无任何写工具调用，questions 列表返回。"""


def test_label_expired_mid_run_recovers():
    """FakeAdapter 第 3 步返回 label_expired → 自动重标注 → 重试成功 → 最终 succeeded；
    repair_log 记录一次 label_expired 恢复。"""


def test_tool_call_budget_enforced():
    """超 max_tool_calls=12 → failed，failure_reason='budget_exceeded'。"""
```

- [ ] **Step 2:** 实现：`run_agent(request, provider, adapter, output_dir) -> TaskRun`。流程 = §5.1（parse_intent → draft_plan → 逐步 execute+verify → repair 分流 → finalize 导出+模板摘要）；plan 按架构文档 §6.2 结构（steps + verification 声明，verification 直接喂 Task 4 的检查器）；每步先写 TaskRun 再执行（崩溃可回放）；prompt 模板按 Task 0 spike 结论定稿，白名单工具 schema 由 `toolbox.list_tools()` 注入。
- [ ] **Step 3:** 测试过 + ruff。
- [ ] **Step 4: Commit** — `feat(agent): orchestrator state machine loop with prompts`

---

## Task 7：server 注册 `agent_run` 工具（22 → 23）

**Files:** Modify: `src/vibecad/server.py`; Create/Modify: `tests/test_server_agent.py`, `tests/test_mcpb_manifest.py`（工具数）

- [ ] **Step 1: 失败测试**：`agent_run` 无 API key → `{ok: False}` 引导安装/配置（不 traceback）；runtime 未就绪 → 复用 `_runtime_guard` 文案；正常路径（mock loop.run_agent）→ 返回摘要 dict 含 status/run_dir/artifacts/usage；`test_all_tools_have_annotations` 自动覆盖（destructiveHint=False, readOnlyHint=False）。
- [ ] **Step 2:** 实现：薄壳——组装 `AnthropicProvider` + `InProcessToolAdapter`（复用全局 `_session`）+ `loop.run_agent`；docstring 教外部 AI 用法（含 needs_clarification 的补参重跑约定）。manifest.json tools 数组同步。
- [ ] **Step 3:** 全量快测 + ruff + `mcpb validate`。
- [ ] **Step 4: Commit** — `feat(server): agent_run MCP tool (in-process agent orchestrator entry)`

---

## Task 8：eval 任务集 + runner

**Files:** Create: `evals/tasks.json`（第三部分 10 任务的机器格式）, `evals/run_evals.py`, `tests/test_eval_assertions.py`（断言函数纯单测）

- [ ] **Step 1:** `tasks.json` 落盘：每任务 = `{id, prompt, expected: {status, bbox_mm?, volume_mm3?, export?, questions_mention?, forbidden_tools?, max_tool_calls, max_repair_rounds, max_cost_usd}}`（体积一律区间断言，E06 双语义双区间）。
- [ ] **Step 2:** runner：逐任务调 `agent_run`（真 runtime + 真 API key，慢测）→ 读 `agent_runs/<id>/run.json` + 导出文件事实 → 复用 `verify.py` 断言 → 汇总 `evals/report-<ts>.json`：首次成功率 / 闭环成功率 / 平均工具调用 / 平均成本 / 失败原因分布（§6.8 指标子集）。
- [ ] **Step 3:** 断言函数单测过（伪造 run.json）+ 本机实跑一轮 10 任务，报告存档。
- [ ] **Step 4: Commit** — `feat(evals): 10-task agent eval suite with machine-checkable assertions`

---

## Task 9：文档 + 收尾

- [ ] **Step 1:** README 工具表加 `agent_run`（一行）+ 本 plan 状态更新（Draft → In Progress/Done）；不写独立 AGENT 手册（原型期文档最小化）。
- [ ] **Step 2:** 飞书同步 + memory 更新（vibecad-status.md：agent 原型完成态 + eval 首轮数据）。
- [ ] **Step 3:** 最终汇报：对照下方验证标准逐条给证据。
- [ ] **Step 4: Commit** — `docs(agent): prototype status + eval baseline`

---

## 范围纪律

仅本计划十个任务（Task 0–9）。**不做**：多 provider 完整抽象与模型路由、MCP self-call backend（阶段 3）、装配/多零件任务、fillet/chamfer/transform 进白名单、Plan IR 中间表示、memory/context 检索（阶段 4）、reviewer 独立模型审查、`awaiting_confirmation` 中途交互态、SQLite、流式输出、视觉模型验证、CLI/HTTP 入口、成本路由、run 取消/并发、eval 任务扩到 30+（阶段 5）。

## Verification（对齐架构文档 §16 五条）

1. **最小 agent run 可执行**：真机 `agent_run("做一个 60×40×10 的底板，四角打 5mm 孔，导出 STL")` → run.json 含结构化 intent 与 plan。
2. **工具复用成立**：TaskRun steps 显示全部工具调用经 `InProcessToolAdapter` 落在现有 server 工具函数上（labels/parts/view_file 原样出现在 step result）。
3. **几何验证闭环成立**：每个写 step 后跟 verification 记录（bbox/体积/文件事实），且为确定性代码产物。
4. **失败恢复可见**：E08/E09/E10 三个 eval 任务分别证明缺参拦截、标签过期自动重标注、几何失败不猜不静默，repair_log 可读。
5. **eval 可重复运行**：`python evals/run_evals.py` 两轮输出同构报告（成功率/成本/工具调用数/失败原因），报告落盘可对比。
6. **快测**：`uv run pytest -q && uv run ruff check .` 全绿；新增测试全部不依赖真网/真 FreeCAD（mock 注入），慢测仅 eval 一处。

---

# 第三部分：首批 10 个 eval 任务清单

> 断言口径：体积/尺寸容差沿用 `docs/ACCEPTANCE_TESTS.md` 判定口径（±1 mm³ 级舍入不计，下表统一放宽为 ±5 mm³ / bbox ±0.1 mm）；`export` 断言 = 文件存在且大小非 0。难度梯度：单体建模（E01–E02）→ 打孔（E03–E05）→ 挖槽（E06）→ 参数修改（E07）→ 失败恢复（E08–E10）。

| ID | 需求（一句话 prompt） | 机器可判定验收断言 |
|---|---|---|
| E01 | 做一个 60×40×10 的底板，导出 STEP | status=succeeded；bbox=[60,40,10]±0.1；volume=24000±5；`<out>/**.step` 存在非空 |
| E02 | 做一个半径 15、高 40 的圆柱，导出 STL | status=succeeded；bbox=[30,30,40]±0.1；volume=28274.3±5（π·225·40）；`.stl` 存在非空 |
| E03 | 做一个 60×40×10 的底板，顶面正中打一个直径 8 的通孔，导出 STEP | status=succeeded；volume=23497.3±5（24000−π·4²·10，A5 口径）；steps 含 add_hole ok；`.step` 存在非空 |
| E04 | 做一块 80×60×8 的安装板，四角各打一个直径 5 的通孔，孔心距两边各 10，导出 STL | status=succeeded；bbox=[80,60,8]±0.1；volume=37771.7±5（38400−4·π·2.5²·8）；`.stl` 存在非空 |
| E05 | 画一块 80×40×10 的板，顶面沿长边方向打一排 4 个直径 6 的通孔，间距 15，整排居中，导出 STEP | status=succeeded；volume=30869.0±5（32000−4·π·3²·10，A7 口径）；`.step` 存在非空 |
| E06 | 画一块 40×30×12 的块，顶面正中挖一条 20×8 的槽，深 5，导出 STEP | status=succeeded；steps 含 extrude_profile(slot, pocket) ok；volume∈{13348.7±10, 13668.7±10}（slot 圆心距/外形总长双语义均判过，A8 口径）；`.step` 存在非空 |
| E07 | 做一个 60×40×10 的底板，顶面正中打直径 8 通孔；然后把长度改成 80，导出 STEP | status=succeeded；bbox=[80,40,10]±0.1；volume=31497.3±5（32000−502.7，A6 口径：孔不丢）；steps 含 modify_part ok；`.step` 存在非空 |
| E08（缺参恢复） | 帮我做一个安装板，四角打孔，导出 STL | status=needs_clarification；questions 非空且提及"尺寸"与"孔径"；steps 中**无任何写工具调用**（new_document/add_box/add_hole 均不得出现）；无导出文件产生 |
| E09（标签过期恢复） | 画一块 60×40×10 的板，顶面正中打直径 6 通孔；把长度改成 80；再在新的顶面正中打一个直径 6 通孔，导出 STEP | status=succeeded 且无 needs_clarification；volume=31434.5±5（32000−2·π·3²·10，两孔心 x=30/40 距 10 不重叠）；若 steps 中出现 label_expired 失败，则 repair_log 含 label_expired 且紧随 render_part(annotate='faces') 重标注（自动恢复无用户介入）；`.step` 存在非空 |
| E10（几何失败恢复） | 做一块 40×40×10 的板，在顶面距长边 1 毫米处打一个直径 8 的孔 | status∈{needs_clarification, failed}；孔位是用户显式参数 → 不得擅改重试成功；failure/questions 文案提及孔越界/开口缺口原因；describe 复核 volume=16000±5（守卫回滚生效，几何完好）；无导出文件产生 |

**任务集与决策的呼应：** E08/E09/E10 一一对应第二部分的三类失败恢复（§16 验证标准 4）；E06 的双区间断言承接验收文档 A8 的"两种 slot 语义都几何正确"裁定；全部数值断言仅依赖 `describe_part`/工具返回/文件事实——印证决策 5（不需要视觉验证）。

---

*本文档为草案。第一部分 7 条决策与第二部分任务边界均待用户逐条确认后，方可转为正式 plan 并开工。*
