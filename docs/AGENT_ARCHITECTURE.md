# VibeCAD Agent 架构与开发路线

> 状态：Accepted，按实现事实更新
>
> 日期：2026-07-18
>
> 当前实现基线：VibeCAD 0.4.0，Stage 2 内部窄切片完成
>
> 文档角色：Agent 定位、已实现边界和后续阶段的决策真源
>
> 当前项目通用架构见 [`ARCHITECTURE.md`](ARCHITECTURE.md)

## 1. 结论先行

VibeCAD 的目标定位是一个 **CAD 专家 Agent**，不是通用自主 Agent，也不是模型供应商。它负责把外部模型或用户给出的设计意图收敛成受控程序，在隔离候选版本中执行，以 FreeCAD/OCCT 的事实独立验收，然后提交或回滚。

截至 2026-07-18，准确的产品状态是：

| 层次 | 当前状态 | 用户现在能否直接使用 |
|---|---|---|
| 现有低层 FastMCP | 已有 31 个低层 CAD/运行时工具 | 能，但它们仍走原有 Session 路径 |
| 确定性 Task Kernel | TK1–TK9 已形成内部 Python 组合，并通过真实 FreeCAD 三场景 | 只能由代码直接组合调用 |
| 任务级 MCP | 尚未注册 `create_task` 等高层工具 | 不能 |
| Codex/Claude 等宿主 skill | 尚未交付 | 不能 |
| Sampling / BYOK | 只有枚举预留，没有 backend 或模型调用 | 不能 |
| 自动 repair / replan / retry | 未实现，当前为零次语义重试 | 不能 |
| 照片、视频、STL、仿真 Provider | 只有架构预留，源码中没有 Provider 包 | 不能 |
| 任意 Python/FreeCAD 代码 Worker | 未来实验方向，当前不存在 | 不能 |

因此，Stage 2 完成的是“专家 Agent 的确定性内部内核”，不是已经可被 Claude、Codex 直接调用的完整 Agent 产品。下一产品阶段是 Stage 3：把这套内核安全地暴露为任务级 MCP 和宿主无关 skill。

## 2. 已锁定的产品定位

```text
用户 / 外部 Agent：理解目标、处理自然语言和歧义、生成受控计划
VibeCAD：校验计划、隔离执行、收集事实、验收、版本化和恢复
FreeCAD / OCCT：几何计算、文档重算、格式导入导出
```

锁定约束：

| 决策 | 结论 |
|---|---|
| 模型商业模式 | 用户自带宿主订阅或 API 授权；短期不采购、补贴或转售模型 Token |
| 首要宿主 | 欧美优先 Claude Code、Codex；亚洲补充 WorkBuddy/CodeBuddy 等支持标准 MCP 的宿主 |
| 核心协议 | 标准 MCP + 宿主无关 skill；不为每个 Agent 复制 CAD 业务逻辑 |
| 主执行路径 | 版本化 `ModelProgram` + 固定语义操作，不执行模型任意代码 |
| 提交权 | 模型和工具返回都不能自证成功；确定性 verifier 拥有提交权 |
| 版本策略 | 所有修改先进入隔离 candidate；通过后才原子推进 HEAD |
| 照片/STL/仿真 | 后续调用现有外部引擎，通过 artifact Provider 接入，不自研底层引擎 |
| 任意代码 | 表达上限高，但只可能成为未来隔离 Worker，永不作为默认主路径 |

## 3. 当前已经实现的内部闭环

当前 `TaskService` 是 direct-module-only 的同步服务，只接受外部已经生成的 `ModelProgram`。一次提交执行一个确定性尝试，不调用模型，不自动修改计划。

```mermaid
flowchart LR
    P["外部预制 ModelProgram"] --> F["预算、schema、program、acceptance preflight"]
    F --> T["TaskRun CAS"]
    T --> L["Project write lease + HEAD/base recheck"]
    L --> C["从 committed FCStd 建立隔离 candidate Session"]
    C --> E["四个固定 CAD operation"]
    E --> K["fresh checkpoint + reload"]
    K --> X["受控 STEP export"]
    X --> S["seal + immutable reload"]
    S --> O["可信几何与 artifact observation"]
    O --> V["确定性 Acceptance verifier"]
    V -->|"全部 required 通过"| H["原子推进 HEAD + 发布 committed Session"]
    V -->|"执行或验收失败"| R["丢弃/结算 candidate，HEAD 与 baseline 不变"]
```

TK9 已在现有 FreeCAD 环境中证明：

- 成功路径：空 committed FCStd → 创建 10×20×30 Box → 长度修改为 12 → inspect → checkpoint → STEP → seal → verify → commit。
- 重载事实：bbox 12×20×30 mm、体积 7200 mm³、面积 2400 mm²、质心 (6, 10, 15)、一个有效 solid。
- 执行失败：第一步已创建 6000 mm³ Box，第二步操作不存在对象而失败；candidate 不提交，HEAD 和 baseline 哈希不变。
- 验收失败：全部 CAD 操作和 artifact 生成成功，但 required volume 故意要求 7201 mm³；candidate 不提交，失败 verdict 与 artifact 诊断保留。
- 三个终态都留下可重读 TaskRun；candidate 目录不残留普通文件，HEAD、journal、manifest 和 artifact 哈希与终态一致。

## 4. 当前代码分层

### 4.1 当前公共入口

`src/vibecad/server.py` 注册 31 个低层 FastMCP 工具，包括项目、建模、修改、测量、渲染和运行时工具。它们早于 Task Kernel 存在，仍操作原有进程内 Session。

当前没有任务级 MCP 工具。`TaskService.create_task()`、`get_task()`、`submit_model_program()`、`continue_task()` 和 `reconcile_task()` 只是内部 Python 方法，没有在 `server.py` 注册。

在 Stage 3 完成共享 lease/session ownership 前，不能把 TaskService 与低层 mutating MCP 同时用于同一项目，否则会形成两条未同步写路径。Stage 3 必须选择并落实以下边界：

- mutating 低层工具也进入同一个 project lease/session authority；或
- legacy 低层写路径与 task-managed project 明确隔离。

只把五个方法套一层 MCP decorator 不算完成 Stage 3。

### 4.2 当前任务控制平面

已实现：

- 严格、版本化的 `TaskRun` 状态与 transition history。
- 有 generation 的 `TaskRunStore` compare-and-set 与原子持久化。
- 同项目排他写 lease。
- `LocalRevisionStore` 的 immutable revision、HEAD、journal 和 reconcile。
- `CandidateCoordinator` 的 Session 隔离、checkpoint、seal、commit、rollback/reconcile。
- `TaskService` 的单次同步事务编排。

未实现：

- 自然语言 Intent 解析、计划生成、模型调用和 usage 统计。
- 自动 repair/replan、语义 retry、取消或异步 job worker。
- 公共任务 envelope、MCP capability negotiation 和宿主接入。

### 4.3 当前 CAD 领域与执行平面

已实现的 `ModelProgram` operation 只有四个：

| operation | 固定 handler | 作用 |
|---|---|---|
| `create_document` | `new_document` | 新建文档 |
| `create_box` | `add_box` | 创建参数化 Box |
| `modify_parameter` | `modify_part` | 修改一个对象参数 |
| `inspect_model` | `describe_part` | 返回执行反馈 |

FCStd checkpoint、STEP export、seal、observation 和 commit 是 TaskService 固定内核步骤，不是模型可以排列或省略的 operation。

当前执行器是 same-process 的 `InProcessCadExecutor`，复用既有 `Session + tools + FreeCAD`。独立 FreeCAD Worker 仍是未来目标；当前不能宣称 OCCT 崩溃与控制平面已经进程隔离。

### 4.4 当前 verifier

提交权只来自 sealed candidate 的可信 observation。当前白名单是：

| 类别 | checks |
|---|---|
| geometry | `volume`、`area`、`bbox`、`center_of_mass` |
| topology | `valid_shape`、`solid_count` |
| artifact | `exists`、`non_empty`、`format` |

artifact `format` 当前只接受 `fcstd` 和 `step`。孔径/深度、preservation、装配干涉、视觉、制造规则和深层 STEP 语义检查尚未拥有 commit authority。

## 5. 当前契约事实

### 5.1 ReasoningOwner

schema 预留了：

```text
external_plan | mcp_sampling | byok
```

但当前 `TaskService.create_task()` 只接受 `external_plan`；另外两个值会返回固定的 unsupported 错误。源码中没有 Sampling backend、BYOK Provider、Keychain、模型 SDK、模型预算或嵌套推理。

### 5.2 ModelProgram v1

下面是当前代码可以 round-trip 的真实结构，不是未来示意：

```json
{
  "schema_version": 1,
  "task_id": "task_11111111111111111111111111111111",
  "base_revision": "revision_22222222222222222222222222222222",
  "operations": [
    {
      "schema_version": 1,
      "id": "box",
      "op": "create_box",
      "target": {},
      "args": {"length": 10, "width": 20, "height": 30},
      "preserve": [],
      "source": "model",
      "depends_on": []
    },
    {
      "schema_version": 1,
      "id": "length",
      "op": "modify_parameter",
      "target": {"object": "Box"},
      "args": {"parameter": "length", "value": 12},
      "preserve": [],
      "source": "model",
      "depends_on": ["box"]
    }
  ],
  "acceptance": {
    "schema_version": 1,
    "id": "acceptance-box",
    "criteria": [
      {
        "schema_version": 1,
        "id": "volume",
        "kind": "geometry",
        "check": "volume",
        "target": "body",
        "expected": 7200,
        "tolerance": 0,
        "parameters": {"unit": "mm^3"},
        "required": true
      },
      {
        "schema_version": 1,
        "id": "solid",
        "kind": "topology",
        "check": "solid_count",
        "target": "body",
        "expected": 1,
        "tolerance": null,
        "parameters": {},
        "required": true
      }
    ]
  }
}
```

`preserve` 字段已存在于契约，但当前 verifier 没有通用 preservation check；不能把“字段可序列化”解释成“约束已经被证明”。

### 5.3 TaskRun v1

当前持久化字段只有：

- id、project id、base revision 和 reasoning owner。
- status、program、candidate revision、committed revision。
- step records、verification reports、artifact refs、last error 和 transitions。

AcceptanceSpec 通过 `program` 持久化；step 可以保存 elapsed time。TaskRun 当前不保存独立 goal、Intent、assumptions、approvals、模型 usage 或通用 repair history。

`workflow.contracts` 中已经有 versioned `Intent` 数据契约，但当前 TaskService 不接收它，TaskRun 也不持久化它；它尚未进入执行闭环。

当前也没有独立的通用 Artifact Store。任务保存 path-free artifact refs；FCStd/STEP 文件由 revision store 拥有。

## 6. 当前状态机与恢复语义

当前有 13 个 durable status，没有 `planning`、`repairing`、`cancelled` 或 `timed_out`：

```mermaid
stateDiagram-v2
    [*] --> created
    created --> needs_plan: request_plan
    needs_plan --> program_ready: submit_program
    needs_input --> program_ready: submit_program
    program_ready --> validating_program: start_validation
    validating_program --> executing: validate_program
    validating_program --> needs_input: reject_program
    executing --> verifying: complete_execution
    executing --> rolling_back: fail_execution
    verifying --> committing: pass_verification
    verifying --> rolling_back: fail_verification
    committing --> succeeded: commit
    rolling_back --> failed: complete_rollback
    validating_program --> recovery_required: require_recovery
    executing --> recovery_required: require_recovery
    verifying --> recovery_required: require_recovery
    committing --> recovery_required: require_recovery
    rolling_back --> recovery_required: require_recovery
    validating_program --> cleanup_required: require_cleanup
    executing --> cleanup_required: require_cleanup
    verifying --> cleanup_required: require_cleanup
    committing --> cleanup_required: require_cleanup
    rolling_back --> cleanup_required: require_cleanup
    cleanup_required --> recovery_required: require_recovery
    recovery_required --> succeeded: confirm_committed
    recovery_required --> rolling_back: confirm_uncommitted
    recovery_required --> program_ready: confirm_pre_candidate
    cleanup_required --> succeeded: confirm_committed
    cleanup_required --> rolling_back: confirm_uncommitted
    cleanup_required --> program_ready: confirm_pre_candidate
```

恢复边界必须精确理解：

- HEAD 前：执行或 required verification 失败会结算 candidate；committed HEAD、baseline revision bytes 和 committed Session 不变。
- seal 后但 HEAD 前：immutable、未提交 revision 可以保留，用于证据与 reconcile；它不是 committed revision。
- HEAD 原子前移后：HEAD 绝不倒退。后续 TaskRun CAS、Session 发布或清理出现歧义时进入 recovery/cleanup，由 HEAD、manifest、revision ancestry、journal 和 live binding 共同判定。
- “rollback”绝不表示撤销已经提交的 HEAD。
- 当前没有自动语义重试。已发布 candidate 只有在 rollback 被明确证明完成后才终止为 `failed`；清理或持久化结果不确定时会停在 `cleanup_required` / `recovery_required`。clean failure 后的新计划需要新 TaskRun；validation rejection 需要外部重新提交 program，而 pre-candidate crash 只有在 reconcile 明确回到 `program_ready` 后才能显式继续。

## 7. 推理模式的当前与目标边界

### 7.1 External Plan：当前内部可用

```text
Claude / Codex / WorkBuddy / 用户代码
→ 生成 ModelProgram + AcceptanceSpec
→ VibeCAD 内部 TaskService 校验、执行和验收
```

这是 Stage 3 要公开的第一条主路径。VibeCAD 不持有模型 Key，也不产生嵌套模型调用。当前缺的是公共 MCP/skill 适配，不是内核执行能力。

### 7.2 MCP Sampling：未来可选

目标是由支持 Sampling 的宿主使用自己的模型授权返回受约束计划。落地前必须有 capability negotiation、用户授权、调用/输出/超时预算和 depth=1 限制。当前没有任何实现。

### 7.3 BYOK：未来独立入口

目标是用户直接配置 Provider API。Key 不得进入 MCP 参数、TaskRun、项目、日志、artifact 或 FreeCAD Worker。当前没有 secret store、Provider adapter 或参考模型后端。

同一 TaskRun 任何时候只能有一个 reasoning owner。外部已经提交 program 后，VibeCAD 不得为了“再确认”隐式调用 Sampling 或 BYOK。

## 8. 计划中的任务级 MCP 与 skill

Stage 3 的候选公共面来自现有内部方法：

| 计划工具 | 内部基础 | 说明 |
|---|---|---|
| `create_task` | 已有内部方法 | 绑定 project HEAD，创建 external-plan TaskRun |
| `submit_model_program` | 已有内部方法 | 按 expected generation 提交并同步执行一个尝试 |
| `continue_task` | 已有内部方法 | 显式继续 `program_ready` 的同一预制 program |
| `get_task` | 已有内部方法 | 只读获取 durable TaskRun |
| `reconcile_task` | 已有内部方法 | 处理 crash/HEAD/Session 不确定窗口；是否公开给普通宿主需在 Stage 3 决定 |

当前没有 `cancel_task`，也没有通用 `agent_run(request)`。取消、异步任务、长时间 worker 和流式进度应在存在真实需求后单独设计，不能在文档中提前宣称。

宿主 skill 应只教授：

- 如何从用户目标构造合法 ModelProgram/AcceptanceSpec。
- 如何读取 schema、固定错误、TaskStatus 和 next action。
- 如何在 generation conflict、needs_input、failed 和 recovery_required 时响应。
- 哪些能力尚不支持，不能退化为任意 Python。

skill 不复制状态机、revision 或 CAD 执行逻辑。

## 9. 未来 Provider 与高级代码通道

源码当前没有 `providers/` 包或以下 Protocol；这些只是目标端口：

```python
class SourceToMeshProvider(Protocol): ...
class MeshToCadProvider(Protocol): ...
class SimulationProvider(Protocol): ...
```

目标数据流：

```text
照片/视频引擎 → Mesh Artifact
Mesh/STL 逆向引擎 → STEP/BRep/参数化候选 Artifact
VibeCAD Task Kernel → 精细编辑、验证、版本化
Simulation Provider → 报告与场结果 Artifact
```

Provider 结果只能作为有来源、有哈希的输入或候选 artifact，不能直接获得 commit authority。

任意 Python/FreeCAD 代码的表达上限高，但当前没有代码 Worker。若未来实验，必须同时具备独立进程/沙箱、只读输入副本、CPU/内存/时间/import/输出限制、完整审计，以及与主路径相同的独立验收。它永不替代 ModelProgram 主路径。

## 10. 当前包边界

已存在：

```text
src/vibecad/
├── workflow/
│   ├── contracts.py
│   ├── errors.py
│   ├── program.py
│   ├── state.py
│   ├── store.py          # TaskRun persistence
│   ├── lease.py
│   └── service.py
├── execution/
│   ├── registry.py
│   ├── results.py
│   ├── adapter.py
│   ├── revisions.py      # Revision/HEAD/journal/artifact persistence
│   ├── candidate.py
│   └── executor.py
└── validation/
    ├── contracts.py
    ├── checks.py
    └── engine.py
```

尚不存在：

```text
workflow/repair.py
reasoning/
providers/
独立 FreeCAD worker
任务级 MCP / host-neutral Agent skill
```

依赖方向继续保持：

```text
server / future UI → workflow
workflow → execution + validation
execution → existing semantic tools → engine/session → FreeCAD
future reasoning 不得依赖 FreeCAD
future FreeCAD worker 不得获得模型 Key
```

## 11. 开发路线与可见交付

| 阶段 | 状态 | 完成后用户能看到什么 |
|---|---|---|
| Stage 0 决策与基线 | 已完成 | 产品边界、模型策略和主路径明确 |
| Stage 1 稳定执行契约 | 已完成 | versioned contracts、registry、normalizer、program validation |
| Stage 2 确定性 Task Kernel | 已完成内部窄切片 | 真实 FreeCAD 成功提交及两类失败回滚可重复证明 |
| Stage 3 外部 Agent 主路径 | 下一阶段 | Claude/Codex 等可通过任务级 MCP + skill 调用 VibeCAD |
| Stage 4 Eval 与产品验收 | 未开始 | 固定任务集、跨宿主与几何一致性指标 |
| Stage 5 Sampling / BYOK | 未开始 | 可选使用宿主模型或用户 Provider Key |
| Stage 6 扩大 CAD 能力 | 未开始 | profile、孔/圆角、preservation、装配等进入受控 IR |
| Stage 7 外部重建/仿真/代码 Worker | 未开始 | 照片/STL/仿真作为 artifact Provider 接入 |

Stage 3 的开发顺序：

1. 先统一 FastMCP 与 TaskService 的 lease/session ownership，禁止双写路径。
2. 增加薄的任务级 MCP adapter，不复制 TaskService 逻辑。
3. 对入口做 schema、record、请求大小和 generation 预算；固定错误不泄露本地路径。
4. 编写宿主无关 skill，先覆盖 external-plan，不接模型。
5. 用至少两个宿主执行同一 conformance program，验证状态、artifact 和几何等价。
6. 再决定低层 mutating MCP 的兼容迁移方式；Sampling/BYOK 仍留在 Stage 5。

Stage 3 完成时，用户应能看到的最小产品闭环是：在 Claude Code 或 Codex 中提出一个当前白名单可表达的任务，宿主提交 ModelProgram，VibeCAD 返回 task id 和可重读终态；成功时获得 committed FCStd/STEP，失败时原项目不变并得到结构化原因。

## 12. 架构不变量

后续每次设计和评审必须检查：

1. 一个 TaskRun 是否始终只有一个 reasoning owner。
2. program 大小、持久化 envelope、schema、acceptance 和 operation 是否在 project mutation、candidate 创建和 FreeCAD 调用前完成 preflight。
3. 模型输出是否只能进入固定 operation registry，不能提供 handler、输出路径或任意代码。
4. StepResult、模型文本或渲染是否被错误地当作 commit 证据。
5. 修改是否发生在隔离 candidate，而不是 committed Session/revision。
6. HEAD 前移后是否只 reconcile、不倒退、不重复 CAD side effect。
7. public MCP 与低层工具是否共享唯一 lease/session authority。
8. 是否把未来 Sampling、BYOK、repair、Provider 或 Worker 写成当前能力。
9. 模型 Key 是否可能进入 FreeCAD、TaskRun、日志、artifact 或 MCP 参数。
10. 每项新增 CAD operation 是否同时具有参数契约、确定性 observation/verifier 和真实回归。

## 13. 目标成功标准

最终目标不是“模型能生成一段 FreeCAD 代码”，而是：

> 不同模型和不同宿主都能提交同一种受控设计意图；VibeCAD 在隔离候选版本上执行，使用几何与 artifact 事实证明结果，通过后提交，失败时保持 committed 项目不变，并留下可恢复、可重放、可审计的 TaskRun。

当前已经证明其中的内部确定性执行、验证、提交与回滚部分。跨宿主公共协议、自然语言推理、扩大 CAD 白名单和外部 Provider 仍需按上述阶段逐步交付。
