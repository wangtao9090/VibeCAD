# VibeCAD 自建 Agent 架构设计与关键考量

> 状态：Proposed  
> 日期：2026-07-02  
> 目标：帮助理解 agent 构建的核心知识，并把这些知识落到 VibeCAD 后续“自己调用模型、自己调度 CAD 工具”的长期架构上。  
> 结论：短期继续打磨 MCP tools / skills；长期新增 VibeCAD Agent Orchestrator，复用现有 CAD 工具层、几何验证、渲染和评估资产。

---

## 1. 背景与问题

VibeCAD 当前已经完成了一个强工具层：通过 MCP 暴露 CAD 语义工具，底层由 FreeCAD 负责建模、装配、渲染和导出。现有形态更接近：

```text
Claude / Codex / Cursor 等外部 Agent
→ VibeCAD MCP Tools
→ VibeCAD CAD 工具层
→ FreeCAD 引擎
```

这条路线是正确的，因为 CAD agent 最难的不是“让模型说话”，而是构建一组稳定、可验证、可回滚的工具。VibeCAD 现有的 `new_document`、`add_box`、`add_hole`、`modify_part`、`render_part`、`export_part`、装配 DSL、几何断言、每步回图，已经是未来自建 agent 的关键地基。

下一阶段需要回答的问题是：如果 VibeCAD 不再只作为外部 agent 的工具，而是自己调用模型、自己规划、自己执行和恢复，应采用什么架构？

本文档给出一个长期目标架构：**VibeCAD 自建 Agent Orchestrator，复用现有 MCP / CAD 工具层，把 skills / specs / plans 沉淀为 agent playbook 和项目记忆。**

---

## 2. Agent 基础概念

在本项目语境里，agent 不是“一个更长的 prompt”，而是一套能循环推进任务的系统：

```text
理解目标 → 规划步骤 → 调用工具 → 观察结果 → 验证结果 → 修复失败 → 完成交付
```

关键组成如下。

| 模块 | 职责 | 在 VibeCAD 中的含义 |
|---|---|---|
| Intent Parser | 理解用户需求并结构化 | 把“做一个安装板”转为尺寸、对象、制造目标、缺失参数 |
| Planner | 生成可执行计划 | 输出 `new_document`、`add_box`、`add_hole`、`export_part` 等步骤 |
| Executor | 执行工具调用 | 通过 MCP 或内部 Python adapter 调用现有 CAD 工具 |
| Verifier / Critic | 检查结果是否符合目标 | 用几何断言、尺寸、孔数量、干涉、导出文件验证 |
| Repairer | 失败恢复 | 参数修正、换方案、回滚、重新标注、询问用户 |
| Memory | 保存上下文 | 当前零件、活动文档、标签、用户偏好、项目约束 |
| Eval Harness | 衡量 agent 能力 | 首次成功率、闭环成功率、成本、失败恢复率、导出成功率 |

普通聊天 agent 可以容忍解释含糊；CAD agent 不行。CAD 输出要被制造、打印或装配，所以 VibeCAD 的核心原则是：

```text
模型负责语言理解和决策。
工具负责确定性 CAD 操作。
验证器负责判断几何结果是否可信。
状态机负责决定继续、重试、回滚或询问用户。
```

---

## 3. A/B 两种形态的区别

### 3.1 B：外部模型 + VibeCAD MCP 工具

当前更接近 B：

```text
用户
→ Claude / Codex / Cursor
→ 调用 VibeCAD MCP tools
→ VibeCAD 执行 CAD 操作
→ 外部 agent 根据返回结果决定下一步
```

优点：

- 上手快，直接借用外部 agent 的推理能力。
- VibeCAD 专注工具正确性。
- MCP tools、文档、skills 能快速迭代。
- 不需要自己处理模型 API、成本、流式输出、上下文压缩。

缺点：

- 主控逻辑在外部客户端，不完全可控。
- 不同客户端的工具调用能力、图像展示能力和上下文策略不同。
- 难以统一实现长期记忆、成本控制、自动评估和恢复策略。
- 产品主动性受限：VibeCAD 是“被调用工具”，不是完整 agent 应用。

### 3.2 A：VibeCAD 自建 Agent

长期目标是 A：

```text
用户
→ VibeCAD Agent
→ VibeCAD 调用模型
→ VibeCAD 规划 / 执行 / 验证 / 恢复
→ VibeCAD CAD 工具层
→ FreeCAD 引擎
```

优点：

- agent 行为完全可控，可统一规划、验证、恢复、记忆和评估。
- 可跨入口复用：MCP、Web、桌面 App、HTTP API 都能接入同一 agent。
- 能积累 VibeCAD 自己的 CAD agent playbook 和任务数据。
- 更适合商业化：成本、配额、BYOK、模型选择、Pro 能力边界都可控。

代价：

- 需要实现模型网关、上下文管理、状态机、工具编排、观测和 eval。
- 要处理失败恢复、用户确认、安全边界、隐私和成本控制。
- 架构复杂度显著高于“只做 MCP 工具”。

### 3.3 推荐路线

推荐走中间路线：

```text
阶段 1：外部 Agent + VibeCAD MCP Tools
阶段 2：VibeCAD Agent Orchestrator + MCP Tool Adapter
阶段 3：统一 Tool Adapter，支持 MCP backend 与 in-process backend
阶段 4：把 skills/specs/plans 沉淀为内部 playbook 与项目记忆
阶段 5：建立端到端 Agent Eval
```

这条路线保留当前工作的价值，同时逐步学习和掌握完整 agent 架构。

---

## 4. 目标架构

### 4.1 总体分层

```text
┌─────────────────────────────────────────────────────────┐
│ 1. User Interface Layer                                 │
│    MCP / Web / Desktop / HTTP API / CLI                 │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│ 2. Agent Orchestrator                                   │
│    Task State Machine / Run Loop / Policy Gate          │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│ 3. Model Layer                                          │
│    Provider Adapter / Structured Output / Cost Tracking │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│ 4. Reasoning Modules                                    │
│    Intent / Planner / Reviewer / Executor / Verifier    │
│    Repairer / Memory / Summarizer                       │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│ 5. Tool Adapter Layer                                   │
│    MCP Tool Adapter / In-Process Tool Adapter           │
└───────────────────────┬─────────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────────┐
│ 6. CAD Runtime Layer                                    │
│    Session / Modeling / Assembly / Feedback / Export    │
│    Geometry Assertions / Rollback / FreeCAD Runtime     │
└─────────────────────────────────────────────────────────┘
```

核心边界：

- 模型不直接输出 FreeCAD 任意代码作为主路径。
- 模型只选择受控语义工具。
- CAD 工具层必须继续做参数校验、事务、回滚和几何断言。
- Verifier 不能只相信模型自评，必须使用工具返回和几何事实。
- Agent Orchestrator 通过状态机推进任务，而不是一次 prompt 走到底。

### 4.2 推荐包结构

未来实现时可新增 `src/vibecad/agent/`，不打乱现有 `tools/`、`engine/`、`feedback/`、`runtime/`。

```text
src/vibecad/
├── agent/
│   ├── orchestrator.py       # agent 主循环与状态机
│   ├── state.py              # TaskRun / Step / Observation / Artifact
│   ├── model.py              # 模型 provider 抽象
│   ├── prompts.py            # planner/reviewer/verifier/repairer prompt 模板
│   ├── intent.py             # 需求结构化
│   ├── planner.py            # 计划生成
│   ├── reviewer.py           # 执行前计划审查
│   ├── executor.py           # 执行计划步骤
│   ├── verifier.py           # 几何与任务目标验证
│   ├── repair.py             # 失败分类与恢复策略
│   ├── memory.py             # session/project/user memory
│   ├── context.py            # 上下文检索、压缩和装配
│   ├── evals.py              # agent 任务集评估入口
│   └── telemetry.py          # run log、成本、工具调用、失败原因
├── tools/                    # 现有确定性 CAD 工具层
├── engine/                   # 现有 FreeCAD session
├── feedback/                 # 现有渲染、标注、glTF、文本诊断
└── runtime/                  # 现有运行时安装与状态
```

---

## 5. Agent 主循环与状态机

### 5.1 单次任务运行

一次 agent run 应该是可追踪、可恢复、可回放的。

```text
User Request
→ parse_intent
→ retrieve_context
→ draft_plan
→ review_plan
→ execute_step
→ observe_result
→ verify_step
→ continue / repair / clarify / finalize
```

推荐状态：

| 状态 | 含义 |
|---|---|
| `created` | 收到用户请求，创建 TaskRun |
| `parsing` | 结构化用户意图 |
| `needs_clarification` | 缺少关键参数，需要问用户 |
| `planning` | 生成操作计划 |
| `reviewing_plan` | 审查计划是否安全、完整、可执行 |
| `executing` | 调用 CAD 工具 |
| `verifying` | 检查几何与任务目标 |
| `repairing` | 失败恢复，可能重试或换方案 |
| `awaiting_confirmation` | 危险或不可逆步骤前等待用户确认 |
| `finalizing` | 导出、总结、返回文件 |
| `succeeded` | 完成 |
| `failed` | 多次恢复失败或能力边界外 |
| `cancelled` | 用户取消 |

### 5.2 失败恢复原则

失败不应全部交给用户。应先分类：

| 失败类型 | 处理策略 |
|---|---|
| 参数缺失 | 询问用户，不猜关键制造参数 |
| 参数格式错误 | 自动修正并重试一次 |
| 标签过期 | 自动重新渲染/重新标注，再继续 |
| 几何断言失败 | 回滚，换更保守方案 |
| 干涉或装配冲突 | 报告冲突量，提出 gap/offset/尺寸修改方案 |
| 工具不支持 | 解释能力边界，给替代方案 |
| 连续失败 | 停止，汇报当前状态和失败链路 |

建议设置硬限制：

- 单个 step 最多自动重试 2 次。
- 单个 task 最多 repair 3 轮。
- 任何导出、删除、大范围替换前都需要 policy gate 判断是否确认。

---

## 6. 核心模块设计

### 6.1 Intent Parser

目标：把自然语言变成结构化任务，不执行工具。

示例输入：

```text
做一个 60×40×10 的底板，四角打孔，导出 STL。
```

示例输出：

```json
{
  "task_type": "create_part",
  "objects": [
    {
      "name": "base_plate",
      "kind": "box",
      "dimensions_mm": [60, 40, 10]
    }
  ],
  "features": [
    {
      "kind": "corner_holes",
      "count": 4,
      "diameter_mm": null
    }
  ],
  "deliverables": ["stl"],
  "missing_info": ["hole_diameter_mm"]
}
```

关键策略：

- 尺寸、孔径、材料、公差、导出格式属于制造相关信息，缺失时倾向于问用户。
- 对明显低风险默认值可以给建议，但要标明是假设。
- 用户含糊表达应转成多个候选 intent，让 planner 选择最小可行方案。

### 6.2 Planner

目标：产出可审查的 CAD 操作计划。

计划不是自由文本，而应是结构化步骤：

```json
{
  "goal": "create a base plate with four corner holes and export STL",
  "assumptions": ["unit is millimeter"],
  "steps": [
    {"tool": "new_document", "args": {"name": "base_plate"}},
    {"tool": "add_box", "args": {"length": 60, "width": 40, "height": 10}},
    {"tool": "render_part", "args": {"annotate": "faces"}},
    {"tool": "add_hole", "args": {"face": "top", "diameter": 5, "pattern": "four_corners"}},
    {"tool": "describe_part", "args": {}},
    {"tool": "export_part", "args": {"fmt": "stl"}}
  ],
  "verification": [
    "bbox is 60x40x10",
    "hole count is 4",
    "exported STL exists"
  ]
}
```

短期可以允许 planner 输出工具名和参数；长期可引入更稳定的中间表示：

```text
User intent → CAD Plan IR → Tool calls
```

这样能减少模型直接拼工具参数的错误。

### 6.3 Plan Reviewer

目标：执行前挡住坏计划。

Reviewer 检查：

- 是否调用不存在的工具。
- 参数是否完整且单位清楚。
- 是否跳过必要验证。
- 是否违反能力边界，例如要求复杂自由曲面。
- 是否需要用户确认。
- 是否有制造常识风险，例如过薄壁厚、孔距边太近。

Reviewer 不需要特别“聪明”，它的价值是保守、稳定、可解释。

### 6.4 Executor

目标：把计划步骤变成实际工具调用。

推荐抽象：

```python
class CadToolAdapter:
    def list_tools(self) -> list[ToolSpec]: ...
    def call_tool(self, name: str, arguments: dict) -> ToolResult: ...
```

两个 backend：

```text
McpCadToolAdapter
  通过本地 MCP server 调用现有 tools。

InProcessCadToolAdapter
  直接 import vibecad.tools / vibecad.engine 调用内部模块。
```

短期优先 MCP backend，因为它最能复用现有工具 schema、返回结构和测试资产。中期再引入 in-process backend，减少自调 MCP 的复杂度和性能损耗。

### 6.5 Geometry Verifier

目标：每步用几何事实验证，而不是相信模型说“看起来对”。

验证来源：

- 工具返回的 `ok`、`message`、`parts`、`labels`。
- `describe_part` 的包围盒、体积、实体数、有效性。
- `render_part` / 每步回图的标注表。
- 装配干涉检查。
- 导出文件存在性、格式、大小。
- 未来可加 glTF / STEP 结构检查。

典型验证：

| 目标 | 验证方式 |
|---|---|
| 尺寸正确 | bbox 数值接近目标尺寸 |
| 孔存在 | 特征清单、体积变化、工程图标注或面/边变化 |
| 四角孔 | 孔数量、位置与边距 |
| 圆角成功 | 边数量/曲面变化、工具返回、渲染 |
| 装配不干涉 | assembly interference guard |
| 导出成功 | 文件路径存在，格式匹配 |

### 6.6 Repairer

目标：把失败转成下一步动作。

Repairer 输入：

```text
当前计划、失败 step、工具错误、几何状态、重试次数、用户目标
```

输出：

```text
retry same step / revise args / replan / ask user / abort
```

示例：

- `标签已过期` → 自动调用 `render_part(annotate="faces")`，更新标签后重试。
- `孔落到零件外` → 缩小 offset 或询问用户。
- `圆角失败` → 尝试更小半径，或提示“当前边不适合该圆角半径”。
- `装配干涉` → 给出 gap/offset 修改建议，不默认强行 `allow_interference`。

### 6.7 Memory

Memory 不应一开始做成复杂知识库。建议三层渐进：

| 类型 | 内容 | 存储建议 |
|---|---|---|
| Session Memory | 当前文档、活动零件、最近标签、已执行步骤、artifact 路径 | TaskRun JSON / SQLite |
| Project Memory | 项目需求、约束、版本、设计决策、导出记录 | 项目目录中的 metadata |
| User Preference Memory | 单位、默认材料、打印机、公差、常用导出格式 | 本地配置，需用户可查看/删除 |

原则：

- Memory 必须可解释，不能变成不可见的“模型记住了”。
- 用户偏好要区分“本次假设”和“长期偏好”。
- 与制造相关的长期偏好需要用户确认后保存。

### 6.8 Eval Harness

Agent eval 是一等公民，不是最后补的测试。

建议指标：

| 指标 | 含义 |
|---|---|
| 首次成功率 | 无修复情况下完成任务的比例 |
| 闭环成功率 | 经过验证和修复后完成任务的比例 |
| 平均工具调用次数 | 越低越好，但不能牺牲验证 |
| 平均 token / 成本 | 控制模型调用成本 |
| 几何错误率 | 目标尺寸、孔、装配、导出是否错误 |
| 澄清问题次数 | 是否频繁打扰用户 |
| 自动恢复成功率 | 失败后 agent 自救能力 |
| 导出成功率 | STEP/STL/3MF/DXF 是否落地 |

VibeCAD 有天然优势：CAD 任务可以用几何事实验证，比普通文本 agent 更容易做可靠 eval。

---

## 7. MCP / Skill / Docs 的复用方式

### 7.1 MCP Tools 复用

现有 MCP tools 未来可变成内部工具协议：

```text
当前：
External Agent → VibeCAD MCP Tools → FreeCAD

未来：
VibeCAD Agent → Tool Adapter → VibeCAD MCP Tools / Python Tools → FreeCAD
```

可直接复用的资产：

- 工具名称与参数 schema。
- 结构化返回。
- 错误信息。
- 每步回图。
- 几何断言。
- 事务和回滚。
- 单元测试与验收场景。

因此，前期继续做 MCP 不是绕路，而是在定义未来 agent 的工具协议。

### 7.2 Skills 复用

skills 的本质是外部 agent 的操作手册。未来可转化成 VibeCAD 自建 agent 的 playbook：

```text
Skill → Planner prompt / Reviewer checklist / Repair policy / Safety policy
```

示例：

- “先规划再执行” → planner/reviewer gate。
- “修改后测试验证” → verifier 必经状态。
- “失败后不静默吞错” → repair policy。
- “高风险操作前确认” → confirmation policy。

### 7.3 Docs / Specs / Plans 复用

`docs/superpowers/specs`、`docs/superpowers/plans`、`docs/USER_GUIDE.md` 可以成为项目记忆。

不要每次把全部文档塞进上下文。应采用检索式上下文：

```text
用户任务
→ 检索相关 spec / plan / tool docs
→ 注入少量相关片段
→ planner 生成计划
```

### 7.4 Tests 复用

现有测试验证工具正确性；未来 eval 验证 agent 能力。

示例 eval：

```yaml
id: base_plate_four_corner_holes
prompt: 做一个 60x40x10 的安装板，四角打 5mm 孔，导出 STL
expected:
  bbox_mm: [60, 40, 10]
  holes:
    count: 4
    diameter_mm: 5
  export:
    fmt: stl
limits:
  max_tool_calls: 12
  max_repair_rounds: 2
  max_cost_usd: 0.20
```

---

## 8. 关键技术考量点

### 8.1 模型选择与模型网关

不要把业务逻辑绑死到某个模型厂商。建议做统一 model adapter：

```text
ModelProvider
  - complete(messages, schema=None)
  - stream(messages)
  - count_tokens(messages)
  - estimate_cost(usage)
```

需要支持：

- OpenAI / Anthropic / 本地模型的 provider 抽象。
- BYOK：用户带自己的 key。
- 模型路由：简单解析用便宜模型，复杂规划/修复用强模型。
- 结构化输出：planner/reviewer 输出 JSON schema。
- 成本统计：每个 TaskRun 记录 token、模型、费用。

短期不用追求复杂路由。先做到 provider 抽象和成本日志，后续再优化。

### 8.2 上下文管理

CAD agent 的上下文容易膨胀：用户需求、工具 schema、历史步骤、图片描述、标签表、文档知识都可能很大。

建议分层：

| 上下文 | 是否常驻 |
|---|---|
| 当前用户目标 | 常驻 |
| 当前计划和最近 N 步 | 常驻 |
| 当前 CAD 状态摘要 | 常驻 |
| 完整工具 schema | 按需注入 |
| 旧步骤详情 | 压缩为摘要 |
| docs/specs/plans | 检索后注入 |
| 图片 | 优先转成结构化标签和几何事实，不长期塞原图 |

关键原则：

- 模型看到的是“完成任务所需最小上下文”。
- Verifier 使用工具和几何事实，不依赖模型记忆。
- 每次状态变化都生成短摘要，避免上下文无限增长。

### 8.3 工具调用边界

主路径只允许语义工具，不允许任意 Python。

推荐分级：

| 工具等级 | 例子 | 策略 |
|---|---|---|
| Safe Read | `describe_part`、`render_part`、`get_runtime_status` | 可自由调用 |
| Normal Write | `add_box`、`add_hole`、`modify_part` | 计划内调用，失败回滚 |
| Risky Write | 大范围删除、覆盖导出、允许干涉装配 | 需要 policy gate |
| Escape Hatch | 任意代码执行 | 默认禁用；仅开发/专家模式 |

### 8.4 几何正确性

CAD agent 的可信度来自几何验证，不来自模型自信。

必须坚持：

- 每个写操作后验证。
- `recompute()` 不等于成功，几何断言才是成功。
- 标签会过期，过期就重新标注，不猜。
- 装配必须做干涉检查。
- 导出必须检查文件事实。
- 失败要保留可解释错误链路。

### 8.5 用户确认策略

确认不应过多，否则体验变差；也不能太少，否则容易制造错误文件。

建议需要确认的情况：

- 关键制造参数缺失，例如孔径、材料、公差。
- 操作不可逆或覆盖用户文件。
- agent 准备采用明显假设。
- 会改变已有设计意图的大改动。
- 工具要进入 escape hatch。

不需要确认的情况：

- 低风险默认视图渲染。
- 读取状态。
- 明确用户已经给出参数的普通建模步骤。
- 自动修复标签过期。

### 8.6 成本与延迟

自建 agent 后，成本变成产品责任。

建议：

- 每个 TaskRun 记录模型调用、token、耗时、工具调用次数。
- planner 一次生成多步计划，避免每个小工具都问大模型。
- Verifier 尽量使用确定性代码，不用模型判断几何。
- 简单修复用规则优先，复杂修复再调用模型。
- 允许用户设置成本/步数上限。

### 8.7 安全与隐私

VibeCAD 处理的是用户设计文件，可能有商业价值。

需要明确：

- 默认本地执行 CAD 工具。
- 用户 API key 不写入日志。
- TaskRun 日志可关闭或本地保存。
- 发送给模型的内容应可解释，避免上传不必要文件。
- 用户长期记忆可查看、导出、删除。
- escape hatch 默认关闭。

### 8.8 可观测性

每次 agent run 都要能回答：

```text
为什么这么做？
调用了哪些工具？
花了多少钱？
哪里失败过？
怎么恢复的？
最终文件在哪里？
```

建议记录：

- TaskRun ID。
- 用户原始请求。
- intent JSON。
- plan JSON。
- 每一步工具调用和返回。
- artifacts 路径。
- verification 结果。
- repair 记录。
- model usage。
- final summary。

这些日志既用于调试，也用于 eval 和产品改进。

---

## 9. 数据模型草案

### 9.1 TaskRun

```json
{
  "id": "run_...",
  "status": "executing",
  "user_request": "...",
  "intent": {},
  "plan": {},
  "steps": [],
  "artifacts": [],
  "memory_refs": [],
  "usage": {
    "tokens": 0,
    "estimated_cost_usd": 0.0
  },
  "created_at": "...",
  "updated_at": "..."
}
```

### 9.2 Step

```json
{
  "index": 3,
  "kind": "tool_call",
  "tool": "add_hole",
  "args": {},
  "result": {},
  "verification": {},
  "status": "succeeded",
  "started_at": "...",
  "finished_at": "..."
}
```

### 9.3 Observation

```json
{
  "source": "tool_result",
  "summary": "Added four 5mm holes on top face.",
  "facts": {
    "bbox_mm": [60, 40, 10],
    "hole_count": 4
  },
  "artifacts": [".../views/base_plate/004-add_hole.png"]
}
```

---

## 10. 典型任务流程

### 10.1 创建单零件并导出

```text
用户：做一个 60×40×10 的底板，四角打 5mm 孔，导出 STL。

1. Intent Parser 提取尺寸、孔、导出格式。
2. Planner 生成 CAD 操作计划。
3. Reviewer 检查参数完整、工具存在、验证步骤完整。
4. Executor 调 `new_document`、`add_box`。
5. Verifier 检查 bbox。
6. Executor 调 `add_hole`。
7. Verifier 检查孔数量、孔径和位置。
8. Executor 调 `export_part(fmt="stl")`。
9. Verifier 检查 STL 文件存在。
10. Finalizer 返回图片、文件路径、关键尺寸摘要。
```

### 10.2 失败恢复：标签过期

```text
1. add_hole 返回标签过期。
2. Repairer 判断为可自动恢复。
3. Executor 调 `render_part(annotate="faces")`。
4. Memory 更新标签表。
5. Planner 或 Repairer 重写该 step 参数。
6. Executor 重试 add_hole。
```

### 10.3 失败恢复：孔径缺失

```text
1. Intent Parser 发现 “四角打孔” 未说明孔径。
2. Agent 不直接猜制造参数。
3. 状态进入 `needs_clarification`。
4. 问用户：孔径想要多少？如果不确定，我建议 5mm。
```

---

## 11. 演进路线

### 阶段 1：继续强化 MCP 工具层

目标：让工具边界足够稳定，给未来 agent 使用。

重点：

- 工具 schema 稳定。
- 错误返回结构化。
- 每步回图和几何事实更完整。
- 验收任务整理成可机器评估的 fixture。

### 阶段 2：Agent Orchestrator 原型

目标：VibeCAD 自己调用模型，但工具执行仍通过 MCP adapter。

最小闭环：

```text
用户一句话
→ intent JSON
→ plan JSON
→ 调现有 MCP tools
→ verify
→ final summary
```

建议只支持 3 到 5 个单零件任务，不急着覆盖全部能力。

### 阶段 3：统一 Tool Adapter

目标：同一 agent 可以选择 MCP backend 或 in-process backend。

收益：

- MCP backend 适合兼容和测试。
- In-process backend 适合产品内置和性能优化。
- Agent 主体不受工具调用方式影响。

### 阶段 4：Playbook 与 Memory

目标：把 skills/specs/plans 转化为内部策略。

重点：

- Planner prompt。
- Reviewer checklist。
- Repair policy。
- Project memory 检索。
- 用户偏好保存与删除。

### 阶段 5：Agent Eval 与持续改进

目标：用数据判断 agent 是否进步。

任务集：

- 单零件建模。
- 参数修改。
- 孔/槽/圆角。
- 两零件装配。
- 导出制造文件。
- 失败恢复场景。

指标：

- 首次成功率。
- 闭环成功率。
- 平均成本。
- 平均工具调用次数。
- 几何错误率。

---

## 12. 推荐决策

### Decision

VibeCAD 后续自建 agent 时，采用 **Agent Orchestrator + Tool Adapter + 现有 CAD Runtime** 的架构。

### Options Considered

#### Option A：继续只做 MCP 工具

| 维度 | 评估 |
|---|---|
| 复杂度 | 低 |
| 学习价值 | 中 |
| 产品控制力 | 低 |
| 复用当前成果 | 高 |

优点：简单、稳定、短期价值明确。  
缺点：agent 主控在外部，难以建立 VibeCAD 自己的记忆、eval、成本控制和产品体验。

#### Option B：自建 Agent Orchestrator，复用现有工具层

| 维度 | 评估 |
|---|---|
| 复杂度 | 中 |
| 学习价值 | 高 |
| 产品控制力 | 高 |
| 复用当前成果 | 高 |

优点：能学习完整 agent 架构，同时最大化复用现有 MCP/tools/skills。  
缺点：需要新增模型层、状态机、上下文管理、观测和 eval。

#### Option C：完整独立 Agent App 一步到位

| 维度 | 评估 |
|---|---|
| 复杂度 | 高 |
| 学习价值 | 高 |
| 产品控制力 | 最高 |
| 复用当前成果 | 中 |

优点：长期产品形态最完整。  
缺点：范围过大，容易同时陷入 UI、账户、文件、协作、云执行、模型网关等问题，分散 agent 学习重点。

### Recommendation

选择 **Option B**。

理由：

- 现有 VibeCAD 最强资产是 CAD 工具层，应该复用。
- 学习 agent 构建的核心内容都在 Option B 中：模型调用、规划、工具执行、验证、恢复、记忆、eval。
- 不需要一开始承担完整 App 平台复杂度。
- 可以自然演进到未来 Web/Desktop/API 产品。

---

## 13. 不做什么

为了避免范围膨胀，以下内容不进入第一阶段自建 agent：

- 不做完整云端 CAD 执行平台。
- 不做账户、团队协作、权限系统。
- 不做任意 Python / FreeCAD 代码生成主路径。
- 不做复杂自由曲面 agent。
- 不做 CAM 刀路。
- 不把所有历史文档无脑塞进模型上下文。
- 不把用户偏好静默写入长期记忆。

---

## 14. 开放问题

1. **模型供应商与 BYOK 策略**：第一版是否只支持一个 provider，还是直接抽象多 provider？
2. **Agent 入口**：第一版是 MCP tool 暴露 `agent_run`，还是独立 CLI / HTTP？
3. **TaskRun 存储**：先 JSON 文件，还是直接 SQLite？
4. **MCP self-call 是否值得做**：原型阶段通过 MCP 调自己，还是直接实现 in-process adapter？
5. **图片理解是否需要模型参与**：短期优先用结构化几何事实，长期再考虑视觉模型检查工程图。
6. **用户确认粒度**：哪些制造假设必须问，哪些可以用默认建议？
7. **eval 任务集规模**：第一版 10 个高质量任务，还是直接整理 30-50 个任务？

---

## 15. 下一步建议

1. 保持当前 MCP tools 路线，继续把工具返回做得更结构化、更适合 agent 消费。
2. 新建一份 implementation plan，聚焦最小 Agent Orchestrator 原型。
3. 第一版只覆盖一个单零件任务闭环：理解需求 → 规划 → 执行 → 验证 → 导出。
4. 同步整理 5-10 个 agent eval 任务，避免只凭感觉判断 agent 是否有效。
5. 把现有 skills/specs/plans 中的流程规则整理成 planner/reviewer/verifier 的初版 playbook。

---

## 16. Verification

本文档不修改运行时代码。后续架构是否成立，应通过以下方式验证：

1. **最小 agent run 可执行**：一句自然语言请求能生成结构化 intent 和 plan。
2. **工具复用成立**：Agent Executor 能通过 Tool Adapter 调用现有 CAD 工具。
3. **几何验证闭环成立**：每个写操作后都有确定性 verification。
4. **失败恢复可见**：至少覆盖标签过期、缺参、几何失败三类恢复。
5. **eval 可重复运行**：同一任务集能输出成功率、成本、工具调用次数和失败原因。

满足以上条件，即证明 VibeCAD 可以从“外部 agent 调用的 CAD MCP 工具”演进为“自有 agent 主控的 CAD 应用”。
