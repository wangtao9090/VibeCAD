# VibeCAD Agent-first Stage 3 编排计划

- Campaign: vibecad-agent-stage3
- Revision: S3-R3
- Control addendum: S3-R3.1
- Status: executing
- Prepared: 2026-07-20
- Repository anchor: codex/task-kernel-phase2@ca8ca57ebb8d91eaab4220fd7f3beb05f64c7fb4
- Target branch: codex/agent-stage3
- Target product version: 0.5.0 Beta
- External push, PR, release and marketplace publication: not authorized by this plan

## 0. Capability profile、批准与执行适配

### CP-S3-20260720

    approval: native-plan
    delegation: spawn-send-wait
    persistence: repo-artifact
    process: native-session-poll

允许的能力证据分类如下，其他来源不作为能力证明。每个 profile field 都逐项记录证据
或 none observed：

| Evidence source | approval | delegation | persistence | process |
|---|---|---|---|---|
| live capability declarations | update_plan 已声明 | spawn_agent、send_message、followup_task、wait_agent 已声明 | apply_patch 与 workspace exec 已声明 | exec_command 声明可返回 session_id，write_stdin 声明可轮询原 session |
| observable behavior | 本会话已观察到 update_plan 成功 | 已观察到 spawn、send、wait 和 agent completion | 已观察到 apply_patch 与本地 Git commit 成功；未使用仓库内容本身作为能力证据 | 已观察到 bounded exec 同步完成；live session 尚未在本轮触发 |
| environment identity | none observed（identity: Codex Desktop / controller /root） | none observed（identity: Codex Desktop / controller /root） | none observed（workspace identity: /Users/wangtao/Documents/DevProject/vibecad） | none observed（host identity: Codex Desktop） |
| public configuration | none observed（approval policy never 不证明 native-plan 或用户批准） | none observed | none observed（filesystem permission profile: unrestricted） | none observed |

适配器选择：Codex adapter。native plan 只投影本文件；本文件是跨会话真源。长命令若
exec_command 返回 session_id，必须用 write_stdin 轮询原 session；否则使用一次有界
blocking command。禁止 detached process、重复启动和 marker polling。

### Authorization S3-A01

- Bound artifact: S3-R3 at commit
  0fd6947879d313225350f28e1b531fd14c96ddd8。
- Bound decisions: S3-D01 至 S3-D08。
- Bound scope: 本文件第 3 至第 8 节的 operation、提交序列、gates、allowlist 与
  completion definition。
- User wording, 2026-07-20: “你继续 猛猛的推进吧”。
- Interpretation: 授权在 S3-R3 既定边界内连续进行本地实现、测试、独立 review、
  修复和语义提交；不重复请求同一批准。
- Excluded authority: 远端 push、PR、release、marketplace、外部模型/API 花费、用户
  数据删除和 G1 Workbench 本体仍未授权。

S3-R3.1 只追加更严格的 capability evidence、per-commit gate、allowlist、ledger 和
recovery 控制，不改变 S3-R3 产品决策、实现范围或外部权限，因此不扩大 S3-A01，也不
重开产品批准 gate。S3-R3.1 提交后成为 task packet 的稳定控制锚点。

S3-R3 完整取代从未执行的 S3-R2。S3-R2 中以下假设已经废止，不能再作为批准或
实施依据：

- 公共设计面必须精确等于 12 个工具；
- 原 31 个工具的交互形态和能力实现都应一起退役；
- 首版只能有 3 个 ModelProgram operation；
- 所有 CAD operation 都必须 headless；
- macOS 首发范围等于长期产品架构范围；
- 固定七个提交和旧文件 allowlist。

替代架构基线见 docs/PRODUCT_CAPABILITY_ROADMAP.md。

## 1. Stage 3 产品结果

Stage 3 不再把 VibeCAD 做成一个只接收批量程序的 headless CAD server。它交付：

1. 一个项目、任务、Revision、Draft、Artifact 和恢复语义的 Task Kernel；
2. 一个版本化 CAD operation registry；
3. 两种同源调用方式：
   - ModelProgram：适合复杂、多步骤、整体候选验收；
   - 由 registry 生成的直接 CAD 工具：适合明确、单步和交互操作；
4. 可扩展的 FreeCAD execution profile：
   - headless；
   - offscreen_gui；
   - interactive_gui；
5. FreeCAD Workbench 所需的 G0 接入缝：
   CadExecutionPort、durable draft/review、managed checkout 和选择器契约。

Stage 3 只验证 headless 真实执行，并为 interactive_gui 提供诚实的 capability
contract 和组合接口；它不虚假宣称 Workbench 已交付。Workbench G1 是紧随本阶段
架构复审之后的独立实现阶段。

## 2. 不变的系统约束

### S3-D01 — 一个写入权威

所有修改，无论来自直接工具、ModelProgram、未来 Workbench 或 Provider，都必须
进入同一个 Task Kernel：

    request
    → bind immutable base revision
    → acquire project lease
    → isolated candidate
    → execute
    → seal observations and artifacts
    → deterministic verification
    → auto commit or durable review
    → commit / reject / rollback / recover

禁止任何公共工具直接调用 module-global Session 并原地改写用户文件。退役的是旧
Session 语义和验证旁路，不是直接 CAD 工具这种有价值的交互形态。

### S3-D02 — 工具数不是产品边界

公共 manifest 由两部分组成：

    stable control tools
    union
    operation registry entries where direct_exposed = true

不再存在 literal 12 allowlist。每个新增 CAD operation 通过统一准入门后，才可被
ModelProgram 使用；其中适合直接调用的 operation 再自动生成薄 MCP adapter。

Stage 3 的控制面基线为：

- Runtime：ping、get_runtime_status、ensure_runtime、uninstall_runtime；
- Capability：get_capabilities；
- Project：create_project、get_project；
- Task：create_task、get_task、submit_model_program、resume_task；
- Review：accept_draft、reject_draft；
- Artifact：export_task_artifacts。

后续的 list_projects、list_tasks、cancel_task、get_task_events、
list_revisions、compare_revisions、revert_project 和高级 observation 在后续能力批次
加入，不为了追求工具数量塞入本阶段。

### S3-D03 — 直接工具与批量程序同源

直接 CAD 工具不是第二套 handler。adapter 必须：

1. 从 operation metadata 生成严格 schema；
2. 把调用编译为一个或多个 ModelCommand；
3. 绑定 project、base revision、预算、idempotency key 和 commit policy；
4. 进入同一个 candidate、verifier、Revision 和恢复流程；
5. 返回相同的 task、draft、verdict 和 artifact envelope。

相同 base revision 和相同 operation 序列，通过逐步直接调用与单个 ModelProgram
执行后必须产生等价结果；差异只允许来自明确的 commit/review policy。

### S3-D04 — Execution profile 是能力声明

每个 operation 明确声明支持的 profile、FreeCAD 版本范围、是否需要 GUI 主线程、
风险等级和资源预算。路由器不能静默从 interactive_gui 降级为 headless，也不能
因为当前只在 macOS 验证就把 OS 写成 operation 语义。

0.5.0 的安装包可以只对当前有真实证据的平台作发行声明；这是发布矩阵，不是 Agent
架构约束。

### S3-D05 — Durable review 是插件前置

验证成功后，任务可以根据 policy：

- auto_commit：立即以 HEAD CAS 提交；
- require_review：封存 immutable draft，状态变为 awaiting_user_review，并释放
  project lease。

Accept 重新取得 lease，并以 draft 的 base revision 对当前 HEAD 做 CAS。base 已
变化时返回 stale-base conflict，不提交。Reject 只改变 draft/task 状态，HEAD 从未
变化。进程重启后 awaiting_user_review 必须可以恢复。

### S3-D06 — 选择器分层实现

Stage 3 实现 object/feature 级 SelectorV1 Level A：

- project_id 和 revision_id；
- 持久 object/feature UUID；
- object type、semantic role、provenance；
- expected cardinality；
- 可选 result_ref。

完整 face/edge SelectorV1 Level B 后移到交互单零件阶段，届时再加入 mapped element、
几何/邻接 fingerprint、pick point 和歧义候选。Stage 3 不公开依赖 Face6/Edge8 的
工具。

### S3-D07 — 插件是交互端，不是第二个 Agent

Workbench 不保存 Claude/Codex token，不复制任务状态机，也不拥有 commit 权。它
负责：

- 连接 managed project；
- 将 GUI selection 转换为 SelectorV1；
- 在独立 Preview Document 中展示 draft；
- 显示 verifier 证据；
- Accept、Reject、Revise；
- 把手工修改封装为 checkpoint，再交给 Kernel 验证和发布。

Stage 3 只交付 CadExecutionPort、review/checkout 语义和 IPC protocol contract。
Python Workbench、Qt Dock、可运行的 local Kernel daemon、认证本地 IPC、GUI
main-thread adapter 和可视 diff 属于 G1/G2。

managed checkout 中的手工编辑只存在于临时副本。发布时必须把它作为新 candidate
重新 checkpoint、seal、observe 和 verify；任何旧 draft verdict 都立即失效。

### S3-D08 — 原 31 项是能力库存

- Runtime 4 项继续作为稳定控制面；
- smoke_cad 仅用于内部安装诊断；
- lifecycle 6 项迁移到 project/revision/import/export 语义；
- describe/measure/render 迁移为绑定 revision 的可信 observation；
- 16 个 CAD mutator 分批进入 registry；
- set_active_part 永久删除，目标必须显式。

没有实际用户，因此不保留旧 endpoint 行为兼容；但不机械删除仍可复用的 engine、
tools、feedback 和验证实现。

## 3. Stage 3 首批 operation

首批只选择不依赖 face/edge 拓扑重定位的 object-level 能力：

| Operation | Profile | Direct tool | Stage 3 验证 |
|---|---|---:|---|
| create_box | headless | 是 | 尺寸、bbox、volume、实体数、reload |
| create_cylinder | headless | 是 | 半径/高度、bbox、volume、实体数、reload |
| modify_parameter | headless | 是 | 显式 object UUID、属性前后值、preservation |
| move_part | headless | 是 | placement、几何不变量、preservation |
| rotate_part | headless | 是 | placement、几何不变量、preservation |
| inspect_model | headless | 是 | revision-bound、只读、per-object observation |

create_document 不属于 ModelProgram；空项目或导入项目在 create_project 时生成 revision
zero。Stage 3 不承诺把全部 31 项迁完。

每个创建命令返回不可伪造的 result handle。后续命令可通过 result_ref 引用它，而不是
猜测标签。ValueShape 至少扩展为：

- bounded string、boolean、integer、finite number 和 unit quantity；
- enum、vector2、vector3；
- result_ref；
- object selector；
- 严格、封闭的复合结构。

## 4. Runtime composition

    server.py
    └── AgentApplication
        ├── public control adapters
        ├── generated direct-operation adapters
        ├── Task Kernel
        │   ├── ResourceLeaseManager
        │   ├── TaskRunStore
        │   ├── RevisionStore
        │   ├── DraftStore
        │   └── ArtifactStore
        ├── versioned operation registry
        ├── deterministic verifier
        └── CadExecutionPort
            ├── InProcessHeadlessAdapter
            └── future InteractiveWorkbenchAdapter

Application API 和 handler 尽量只依赖 FreeCAD App。Gui selection、高亮和 panel 留在插件
层。FreeCAD 调用继续受进程级 CAD gate 保护；project lease 和 HEAD CAS 负责跨进程
决胜。

Stage 3 的进程拓扑是 MCP server 在进程内拥有 AgentApplication 和
InProcessHeadlessAdapter。G1 保持 Application API 不变，把 Kernel 生命周期提取到
独立 local daemon，供 MCP adapter 和 Workbench 通过认证 IPC 共同访问；插件不会尝试
连接或控制某个临时 MCP server 进程。

运行时与数据目录保持分离：

    VIBECAD_HOME/
    ├── runtime/
    └── data/

uninstall_runtime 只能删除 runtime，不能删除 data 中的 project、revision、draft 或
artifact。

## 5. 语义提交序列

每个提交必须先有失败测试，再有最小实现、累计回归、真实 FreeCAD 证据和独立只读
review。普通测试缺陷由 controller 自主关闭，不形成用户批准中断。

| ID | Prewritten English commit message | Exact semantic scope | Independent gate | Revert boundary |
|---|---|---|---|---|
| S3-1 | `feat(execution): add typed result references and execution profiles` | registry/program/adapter/executor contracts；移除 program create_document | registry + model_program + adapter + executor focused tests；Ruff | `git revert <S3-1>` |
| S3-2 | `feat(validation): add stable object selectors and preservation checks` | object/feature UUID、SelectorV1 Level A、per-object observation、preservation | naming + validation + candidate tests；real FCStd recompute/reload | `git revert <S3-2>` |
| S3-3 | `feat(execution): migrate first-wave CAD operations` | 首批六个 object-level operation 与固定 bindings | operation/tool/executor tests；managed FreeCAD success/failure | `git revert <S3-3>` |
| S3-4 | `feat(mcp): generate direct CAD tools from operation metadata` | registry-derived schema/adapters/manifest；direct 与 program 同源 | server/manifest contract；direct-vs-program equivalence | `git revert <S3-4>` |
| S3-5 | `feat(workflow): add durable draft review` | draft store/state/service、Accept/Reject、lease/CAS/recovery | workflow/candidate/restart matrix；Reject/stale HEAD invariants | `git revert <S3-5>` |
| S3-6 | `feat(application): compose task kernel execution ports` | AgentApplication、CadExecutionPort、managed checkout、IPC protocol contract | application composition + lazy-import + capability tests | `git revert <S3-6>` |
| S3-7 | `feat(application): add verified artifacts and agent MCP controls` | verified copy-out、runtime/data split、稳定控制面与 manifest | export/path/runtime/server integration；uninstall preserves data | `git revert <S3-7>` |
| S3-8 | `feat(agent): package skill and complete stage 3 acceptance` | skill、docs、version、distribution consistency、真实 E2E | full pytest + Ruff + package/manifest + managed FreeCAD + conformance | `git revert <S3-8>` |

### S3-1 — program contracts、result handle 与 execution profile

- 扩展 ValueShape 和严格 schema；
- 让每个 command result 可被后续 result_ref 引用；
- operation metadata 增加 profile、风险、预算和 direct_exposed；
- 移除 ModelProgram 中的 document lifecycle。

### S3-2 — stable object selector 与细粒度 verifier

- 持久 object/feature UUID；
- SelectorV1 Level A 解析；
- per-object sealed observation；
- preservation check 和 reload 后验证；
- 零命中、多命中、伪造和 stale revision 全部 fail closed。

### S3-3 — first-wave operation registry

- 接入 create_box、create_cylinder、modify_parameter、move_part、
  rotate_part、inspect_model；
- 统一 executor binding；
- 真实 FreeCAD 成功、执行失败和验收失败测试。

### S3-4 — generated direct tools

- 从 registry 生成直接 MCP schema 和 adapter；
- 证明所有写入都进入 Task Kernel；
- 证明直接调用与 ModelProgram 结果等价；
- manifest 不包含旧 Session 旁路。

### S3-5 — durable draft/review

- immutable draft artifact 和 awaiting_user_review；
- accept/reject、lease release/reacquire、HEAD CAS；
- crash/restart recovery、duplicate response 和 stale-base 测试；
- 保留 auto_commit policy。

### AR-1 — 产品架构复审

在 S3-5 后、插件和公共文档冻结前执行。复审必须回答：

- 用户能否从 Claude/Codex 创建、修改、查看证据并安全提交；
- 直接工具是否真的只是 Task Kernel 的薄适配器；
- durable review 是否足以支持 FreeCAD 插件预览；
- object selector、observation 和 verifier 是否达到 G1 前置要求；
- 下一批能力应优先进入 P1，还是先补平台可靠性。

只有复审要求改变产品定位、信任边界、公共契约或阶段范围时才向用户请求产品决策。
代码缺陷、测试失败、命名调整和文档修正由 controller 自主处理。

### S3-6 — Application API 与 CadExecutionPort G0

- 组合 AgentApplication，不在 server.py 复制状态机；
- 建立 headless adapter 和 future interactive adapter 接口；
- managed checkout 只处理 immutable revision/draft 的副本；
- 冻结可供 G1 daemon 使用的 IPC protocol，但本阶段不宣称可运行 IPC；
- capability discovery 诚实报告已验证和未实现 profile。

### S3-7 — verified export、runtime/data 与公共 MCP

- 只导出 committed revision 或明确 draft 的内容寻址 artifact；
- 固定输出名、原子复制、hash 校验、拒绝 symlink/hardlink/managed path；
- runtime uninstall 保留 data；
- 发布稳定控制面和 registry 派生直接工具；
- 更新 MCPB manifest，但平台声明只依据真实测试。

### S3-8 — skill、文档与真实端到端验收

- host-neutral VibeCAD Agent skill；
- capability discovery、直接工具、ModelProgram、review 和恢复示例；
- 更新 architecture、user guide、acceptance tests 和版本；
- 在 managed FreeCAD 上完成真实 headless E2E；
- 记录 interactive_gui 为 planned，不伪造通过；
- 完成 AR-1 结论闭环和下一阶段 G1/P1 计划。

## 6. 强制验收门

| Gate | 必须证明 |
|---|---|
| G1 Manifest | 公共 manifest 等于固定控制面加 registry 派生工具，不含 literal 12 假设 |
| G2 One writer | 每个直接 mutator 都进入 Task Kernel；无 module-global Session 公共旁路 |
| G3 Equivalence | 相同 base 和 operation 序列的 direct 与 ModelProgram 结果等价 |
| G4 Failure atomicity | schema、执行、验收、export 失败都不改变 HEAD |
| G5 Review | Reject 不改 HEAD；Accept stale base 不提交；重启可恢复 awaiting review |
| G6 Stable refs | result_ref 和 object UUID 可跨 recompute、checkpoint、reload 使用 |
| G7 Preservation | 非目标对象和声明属性被逐项验证，而不是只看 aggregate volume |
| G8 Artifact | FCStd/STEP hash、reload、revision binding 和 copy-out 均验证 |
| G9 Profiles | headless 真实通过；interactive 只报告 contract/seam，不虚假可用 |
| G10 Runtime data | uninstall/repair/swap 不改变 data 下任何 project/revision/draft |
| G11 Regression | 全量单测、安装测试、Task Kernel 回归和真实 FreeCAD E2E 通过 |
| G12 Safety | 任意 Python、动态 handler、文件替换和非 allowlisted operation 均被拒绝 |

## 6.1 Manual validation matrix

| ID | 环境与场景 | 预期观察 | Evidence owner | 用户是否需要在场 |
|---|---|---|---|---|
| MV-S3-1 | managed FreeCAD 1.1，首批 operation | success、execution failure、verification failure 均与 HEAD/rollback 契约一致 | controller | 否 |
| MV-S3-2 | managed FreeCAD 1.1，checkpoint/STEP/reload | artifact hash 完整，reload 后几何与 per-object observation 一致 | controller | 否 |
| MV-S3-3 | 进程重启，awaiting_user_review | draft 可恢复；Accept/Reject 与 stale HEAD 行为确定 | controller | 否 |
| MV-S3-4 | Codex 与另一兼容 MCP 宿主 conformance | 同一 program 的状态、verdict 和几何语义等价 | controller / S3-RES-06 | 否，若第二宿主授权不可用则保留 residual |
| MV-S3-5 | interactive_gui capability discovery | 只报告 planned/unsupported，不声称 Workbench 可运行 | controller | 否 |

## 6.2 Budget 与 circuit breakers

- Commit budget：8 个生产语义提交 S3-1 至 S3-8；本次架构文档提交 S3-E00 不计入。
- Stage limit：不得扩大到 G1 Workbench 本体、完整 face/edge selector、外部 daemon
  实现、PLM/云服务、任意 Python 或模型供应。
- 每个行为提交必须有 genuine focused RED、最小 GREEN、累计回归、独立只读 review、
  named-file staging 和本地 commit。
- 远端 push 因 S3-A01 未授权而记录为 residual；不能把 skill 当作外部写权限。
- 以下可观察条件立即触发 circuit breaker：
  - 未预测的测试失败或预期 RED 未命中目标代码路径；
  - 写出第 8 节 allowlist；
  - 需要第二条 commit authority、动态 handler 或任意代码；
  - 真实 FreeCAD 结果与 sealed observation、HEAD 或 rollback 契约不一致；
  - 同一长命令的进程状态不明确或可能重复执行；
  - 生产提交数将超过 8，或需要改变 S3-D01 至 S3-D08。

breaker 只在上述产品/执行边界被触发时停止；普通可定位实现缺陷在同一 packet 内修复。

## 7. 恢复与并发矩阵

至少覆盖：

- before execute、during execute、after checkpoint、after STEP、after seal、
  after verify、before commit 和 after commit response loss；
- direct tool retry 与 ModelProgram retry；
- task generation conflict、project lease conflict 和 HEAD conflict；
- draft 创建后崩溃、Accept 响应丢失、Reject 重放；
- runtime maintenance 与 CAD execution 竞争；
- FreeCAD exception、无效 shape、artifact hash mismatch 和 reload failure。

所有恢复由 durable task generation、journal、revision ancestry、draft metadata 和
content hash 决定，不能依赖内存 Session。

## 8. 允许修改的范围

Stage 3 的全局机械 allowlist 为以下 repository-relative 路径；每个 task packet 必须从中
进一步收窄：

- `src/vibecad/workflow/`
- `src/vibecad/execution/`
- `src/vibecad/validation/`
- `src/vibecad/application/`（新）
- `src/vibecad/interaction/`（仅 G0 contract；新）
- `src/vibecad/engine/naming.py`
- `src/vibecad/engine/session.py`
- `src/vibecad/tools/modeling.py`
- `src/vibecad/tools/modify.py`
- `src/vibecad/tools/transform.py`
- `src/vibecad/runtime/installer.py`
- `src/vibecad/runtime/paths.py`
- `src/vibecad/runtime/status.py`
- `src/vibecad/runtime/uninstall.py`
- `src/vibecad/launcher.py`
- `src/vibecad/server.py`
- `src/vibecad/__init__.py`
- `skills/vibecad-agent/`（新）
- `tests/`
- `docs/`
- `README.md`
- `pyproject.toml`
- `manifest.json`
- `mcpb_entry.py`
- `.mcpbignore`
- `.github/workflows/release.yml`

若实现需要改变模型供应方式、引入 VibeCAD 自售 token、绕过 Task Kernel、把插件变成
第二个状态权威、执行任意 Python、接入外部 PLM/云服务或扩展到 G1 Workbench 本体，
必须先做产品级 scope revision。

## 8.1 Expected impact

- 默认 ModelProgram 不再包含 create_document；project bootstrap 创建 revision zero。
- operation metadata 增加结果 slot、execution profile、direct exposure 与扩展值类型；
  现有 registry/program/adapter/executor 测试会按预先定义的新契约更新。
- 原 31 个 public server endpoint 在 S3-4 前仍是当前 0.4.0 事实；迁移时 endpoint
  行为可以改变，但其有价值的 CAD handler 不机械删除。
- Task Kernel、revision store、candidate、verifier 和现有 runtime installer 的未触及
  行为必须保持回归通过。
- S3-1 至 S3-3 会改变内部 ModelProgram schema/allowlist；当前没有实际用户，因此不
  承担旧内部 create_document program 的兼容，但必须保留清晰的 unsupported error。

## 8.2 Residual ledger

| ID | Evidence / impact | Owner | Disposition | Closure condition |
|---|---|---|---|---|
| S3-RES-01 | 远端 push 未由 S3-A01 授权；本地提交尚无远端恢复副本 | user/controller | defer | 用户明确授权 push |
| S3-RES-02 | Windows/Linux task store、安装与真实 FreeCAD matrix 未完成 | platform stage | defer | 对应平台 G3 matrix 通过 |
| S3-RES-03 | G1 Workbench、daemon 和认证 IPC 未实现 | G1 | planned | G1 产品范围批准并通过 GUI E2E |
| S3-RES-04 | face/edge SelectorV1 Level B 未实现 | P1 | planned | 歧义、高亮、recompute/reload matrix 通过 |
| S3-RES-05 | same-process FreeCAD 尚无独立 crash containment | P3 | defer | independent Worker 和 crash recovery gate 通过 |
| S3-RES-06 | 第二个兼容 MCP 宿主的授权与可用性未在本阶段证明 | release acceptance | defer | 同一 canonical program 的跨宿主 conformance 通过 |
| S3-RES-07 | S3-1 ResultRef 只在单次 program run 内稳定，尚不能引用已有 FCStd 对象 | S3-2 | planned | SelectorV1 Level A、重算与重载门禁通过 |
| S3-RES-08 | resource budget、FreeCAD version range 与 GUI main-thread 仍是声明性 metadata | S3-6 | planned | CadExecutionPort 在 handler 前强制预算、版本与线程约束 |
| S3-RES-09 | offscreen/interactive profile 已有封闭枚举，但当前真实 executor 只证明 headless | G0/G1 | planned | capability discovery 与对应 GUI worker/Workbench E2E 通过 |

## 8.3 Append-only execution ledger

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E00 / 2026-07-21T02:51:03Z | architecture review before S3-A01 | 0fd6947 / not authorized | G0: diff check exit 0；4 docs，926 insertions/50 deletions | S3-RES-01 | S3-S00 | completed |

### Recovery snapshot S3-S00

1. **Completed milestones:** repository VibeCAD；branch `codex/agent-stage3`；
   verified commit `0fd6947879d313225350f28e1b531fd14c96ddd8`；S3-R3 architecture
   committed；push not authorized；workspace had only the pending S3-R3.1 control addendum。
2. **Next steps:** commit S3-R3.1 control addendum；if independent plan gate passes, issue
   Packet S3-1A；if it fails, change only orchestration controls and re-review；after genuine RED
   implement S3-1；any unexpected focused/full gate red triggers the declared breaker。
3. **Approved decisions:** S3-D01 through S3-D08 under S3-A01；exact authorization:
   “你继续 猛猛的推进吧”；no repeat approval inside unchanged scope；push/PR/release/G1
   remain excluded。
4. **Execution discipline:** CP-S3-20260720；Codex adapter；spawn-send-wait；
   repo-artifact；native-session-poll when a session_id exists；global allowlist in section 8；
   per-commit gates in section 5；breakers in section 6.2；recover by verifying branch, HEAD,
   worktree, artifact revision and the last focused gate before executing the next packet。

## 8.4 Packet S3-1A — typed execution contract

### 1. Authorization

- Approval ID: S3-A01；control revision: S3-R3.1。
- Stable anchor: `90eec36ef0f5d44cddfcd1750f5187321db2c4e3`。
- 本 packet 只执行 S3-1，不重开已经批准的产品决策，不继承远端 push、PR、release、
  marketplace、外部模型花费或 G1 Workbench 权限。

### 2. Anchor 与机械 allowlist

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`。
- Branch: `codex/agent-stage3`；starting HEAD: `90eec36`。
- 未观察到 repository-local `AGENTS.md` 或 `CLAUDE.md`；system、developer、user、
  skill 和 permission 指令继续生效。
- 本 packet 只允许修改：
  - `src/vibecad/execution/registry.py`
  - `src/vibecad/execution/__init__.py`
  - `src/vibecad/workflow/program.py`
  - `src/vibecad/execution/adapter.py`
  - `src/vibecad/execution/executor.py`
  - `tests/test_execution_registry.py`
  - `tests/test_model_program.py`
  - `tests/test_execution_adapter.py`
  - `tests/test_program_executor.py`
  - `tests/test_task_kernel_integration.py`
  - `tests/test_candidate_revision.py`（仅 execution package public export contract）
  - `tests/test_revision_store.py`（仅 execution package public export contract）
  - `docs/orchestrated/vibecad-agent-stage3.md`

### 3. Contract 与不变量

- Wire result ref 固定为严格对象
  `{"command_id": "<dependency command>", "slot": "<declared slot>"}`；不接受额外字段、
  已解析值、任意 JSON pointer 或未声明 slot。
- ref source 必须在当前 command 的 transitive dependency closure 内，slot 的 concrete
  ValueShape 必须与目标 field 声明一致；self、forward、non-dependency、unknown、type
  mismatch 全部在 handler 解析前 fail closed。
- `create_box` 声明 `object` result slot，从成功结果的 `name` 字段提取 bounded string；
  adapter 在调用消费者前解析，并在 producer slot 缺失或 shape 错误时返回固定、不可反射
  的失败并停止。
- ExecutionProfile 固定为 `headless`、`offscreen_gui`、`interactive_gui`。每个 operation
  声明 profiles、FreeCAD version range、risk、resource budget、direct exposure 和 GUI
  main-thread requirement；S3-1 真实 executor 只显式请求 `headless`，不得静默降级。
- `create_document` 从默认 ModelProgram registry 移除。空 candidate 的固定 document
  lifecycle 属于 trusted executor；bootstrap 失败必须 best-effort close，不能泄漏半初始化
  Session。
- S3-1 建立 enum、integer、finite number、quantity、vector2/3、result_ref 等封闭 shape；
  SelectorV1 的稳定 object/feature 语义仍由 S3-2 实现，S3-1 不声称已交付。

### 4. Steps 与 gates

1. 在 packet allowlist 内写 genuine focused RED；RED 必须命中缺失的 metadata、ref、profile
   或 lifecycle 行为，setup/import/syntax failure 不计。
2. 实现最小 registry/program/adapter/executor correction，不加入首批其余 CAD handler、
   SelectorV1、direct MCP generation 或 GUI 代码。
3. GREEN：
   `PYTHONPATH=src uv run pytest -q tests/test_execution_registry.py tests/test_model_program.py
   tests/test_execution_adapter.py tests/test_program_executor.py`。
4. Integration regression：
   `PYTHONPATH=src uv run pytest -q tests/test_task_kernel_integration.py
   tests/test_candidate_revision.py::test_public_surface_signatures_and_closed_enums
   tests/test_revision_store.py::test_public_surface_is_direct_module_only_and_exact`；若真实
   FreeCAD gate 需要 managed runtime，则使用现有 ready environment，不安装第二套 runtime。
5. Static gate：`uv run ruff check` 仅覆盖本 packet 修改的 Python 文件。
6. 由未参与写入的 agent 做 read-only diff review；controller 关闭普通缺陷后才可 named-file
   stage 和本地语义 commit。

Baseline evidence：第一次无 `PYTHONPATH=src` 的命令以四个 `ModuleNotFoundError: vibecad`
退出 2，被判定为 setup failure；正确 baseline 以 exit 0 完成 `271 passed, 1 deselected in
2.44s`。

### 5. Execution discipline 与 breakers

- Adapter: Codex；delegation: spawn-send-wait；persistence: repo-artifact；process:
  native-session-poll。长命令若返回 session_id，只能轮询原 session。
- 同一生产文件在任一时刻只允许 controller 写入；sub-agent 只做只读分析或独立 review。
- 立即 breaker：预期 RED 未命中目标路径；结果 ref 能越过 dependency/type boundary；profile
  未在首个 handler 前拒绝；bootstrap failure 泄漏 document；focused/integration gate 出现
  不可解释回归；需要写出 allowlist；需要修改 S3-D01 至 S3-D08。

### 6. Delivery boundary

- 预写 commit：`feat(execution): add typed result references and execution profiles`。
- 只做 named-file staging 和本地 commit；controller 保留 commit acceptance 与 revert 权。
- Push 状态固定为 `not authorized`，归入 S3-RES-01。

### 7. Final report contract

完成时 ledger 必须追加：commit hash、RED/GREEN/集成/Ruff/独立 review 证据、实际修改文件、
残留风险、push 状态和新的 recovery snapshot；工作树必须无未解释修改。

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E01 / 2026-07-21T03:18:00Z | S3-A01；S3-R3.1 independent plan gate PASS | 90eec36 / not authorized | diff check exit 0；plan review PASS；focused baseline 271 passed, 1 deselected | S3-RES-01 | S3-S01 | packet-issued |

### Recovery snapshot S3-S01

1. **Completed:** S3-R3.1 committed at `90eec36`；independent plan gate PASS；S3-1A issued；
   correct focused baseline 271 passed, 1 deselected；push not authorized。
2. **Next:** commit this packet control record；write genuine S3-1 RED；implement the smallest
   contract correction；run focused, integration and Ruff gates；independent diff review；local
   semantic commit；append ledger and snapshot。
3. **Invariant:** one Task Kernel, model cannot own document lifecycle, refs only bind declared
   dependency result slots, profile selection is explicit, no silent GUI/headless fallback。
4. **Recovery:** verify branch `codex/agent-stage3`, HEAD, worktree and S3-1A allowlist before
   editing；do not relaunch any command whose original session is still live。

### S3-1 completion record

实际修改范围严格落在 Packet S3-1A allowlist：

- `src/vibecad/execution/__init__.py`
- `src/vibecad/execution/adapter.py`
- `src/vibecad/execution/executor.py`
- `src/vibecad/execution/registry.py`
- `src/vibecad/workflow/program.py`
- `tests/test_candidate_revision.py`
- `tests/test_execution_adapter.py`
- `tests/test_execution_registry.py`
- `tests/test_model_program.py`
- `tests/test_program_executor.py`
- `tests/test_revision_store.py`
- `tests/test_task_kernel_integration.py`
- `docs/orchestrated/vibecad-agent-stage3.md`

Genuine RED 以 3 个目标失败证明旧实现仍暴露 `create_document`、把 result ref 当普通字符串、
且空 candidate 未创建 trusted document；setup/import failure 未计入 RED。实现后的权威
registry 重绑定使用 `ValidatedProgram` 自身封存的 authority，并用 exact-type canonical
snapshot 在任何 handler、mapping 或 clock 访问前拒绝 metadata 改写，同时保留 custom
operation registry 的扩展路径。

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E02 / 2026-07-21T03:39:48Z | S3-A01；S3-1A；两次独立只读正确性 review PASS | S3-1 semantic commit containing this snapshot / not authorized | RED 3 targeted failures；focused 321 passed, 5 deselected；full 2315 passed, 85 deselected, 2 existing warnings；managed FreeCAD 5 passed, 73 deselected；Ruff all passed；diff check passed | S3-RES-01, S3-RES-07..09 | S3-S02 | completed |

### Recovery snapshot S3-S02

1. **Completed:** S3-1 typed result refs、execution profiles、trusted empty-document bootstrap、
   custom registry authority preservation 与 fail-closed canonical rebinding 已完成；两位未参与
   写入的 reviewer 均 PASS；本 snapshot 与生产改动由同一个本地语义 commit 固化。
2. **Evidence:** focused 321 passed / 5 deselected；全仓 2315 passed / 85 deselected / 2 个
   既有 fork deprecation warnings；真实 managed FreeCAD 5 passed / 73 deselected；全仓 Ruff、
   `git diff --check` 均通过；无活跃测试 session。
3. **Next:** 从已提交 S3-1 HEAD 签发 S3-2 packet；先写 SelectorV1 Level A 与 preservation
   的 genuine RED，再实现 object/feature UUID、per-object observation、recompute/reload gate；
   不在 S3-2 扩大到 face/edge Level B。
4. **Recovery:** verify branch `codex/agent-stage3`、最新 commit subject
   `feat(execution): add typed result references and execution profiles` 与 clean worktree；push 仍
   未授权；若 HEAD 或 worktree 不符，先按 named-file diff 审计，禁止重复执行已完成的 S3-1。

## 8.5 Packet S3-2A — stable entity selectors and preservation evidence

### 1. Authorization and anchor

- Approval ID: S3-A01；bound decisions: S3-D01 through S3-D08。
- Stable anchor: `dc8250e88c6dd084f0553223c6f63164e6cf4326`；branch:
  `codex/agent-stage3`；worktree clean at issue time。
- 本 packet 只执行 S3-2；不迁移 S3-3 的六个默认 operation，不获得 push、PR、release、
  外部花费、G1 Workbench 或 Level B face/edge selector 权限。
- 三个未参与写入的只读 design/test/runtime 分析一致通过；正确 baseline 为
  `369 passed, 5 deselected in 6.29s`。

### 2. Mechanical allowlist

本 packet 只允许修改：

- `src/vibecad/execution/selectors.py`（新）
- `src/vibecad/execution/__init__.py`
- `src/vibecad/execution/registry.py`
- `src/vibecad/execution/adapter.py`
- `src/vibecad/execution/executor.py`
- `src/vibecad/engine/session.py`
- `src/vibecad/workflow/program.py`
- `src/vibecad/workflow/service.py`
- `src/vibecad/validation/contracts.py`
- `src/vibecad/validation/checks.py`
- `src/vibecad/validation/engine.py`
- `src/vibecad/validation/__init__.py`
- `tests/test_object_selectors.py`（新）
- `tests/test_engine_session.py`
- `tests/test_execution_registry.py`
- `tests/test_model_program.py`
- `tests/test_execution_adapter.py`
- `tests/test_acceptance_verifier.py`
- `tests/test_program_executor.py`
- `tests/test_task_service.py`
- `tests/test_candidate_revision.py`（仅 execution/validation public surface）
- `tests/test_revision_store.py`（仅 execution public surface）
- `tests/test_task_kernel_integration.py`
- `docs/orchestrated/vibecad-agent-stage3.md`

若最小实现不需要其中某文件则不触碰；写出此清单立即 breaker。

### 3. SelectorV1 Level A contract

Persistent selector 是 exact-key schema；不接受额外字段、Name、Label、DocumentObject.ID、
FaceN、EdgeN 或未绑定 revision：

```json
{
  "schema_version": 1,
  "project_id": "project_<32 lowercase hex>",
  "revision_id": "revision_<32 lowercase hex>",
  "entity_kind": "object | feature",
  "object_id": "object_<32 lowercase hex>",
  "feature_id": "feature_<32 lowercase hex> | null",
  "object_type": "exact FreeCAD TypeId",
  "semantic_role": "part | primitive | feature | support",
  "provenance": {
    "source": "user | model | system | imported",
    "operation_id": "bounded command id | null"
  },
  "expected_cardinality": 1
}
```

- 每个受管 DocumentObject 的 object_id 全文档唯一；可建模对象另有全文档唯一 feature_id。
  recompute、原位参数修改和 Placement 修改保持两者；新 DocumentObject 生成新 ID。
- `entity_kind=object` 时 feature_id 必须为 null；`entity_kind=feature` 时 feature_id 必须存在。
  Level A 不实现跨 PartDesign feature chain 的逻辑 object identity 转移。
- FreeCAD 持久属性固定为 `VibeCADObjectId`、`VibeCADFeatureId`、
  `VibeCADSemanticRole` 和 `VibeCADProvenance`；普通受管写入使用 persistent、read-only、
  hidden、locked dynamic properties。resolver 仍把属性值视为不可信并重新严格解析。
- program preflight 要求 selector revision 等于 ModelProgram.base_revision；executor 在遍历
  document 前要求 project/revision 分别等于 ActiveCandidate.project_id/base_head.revision_id。
- resolver 按全部 identity/type/role/provenance 字段 exact match；0 hit、multiple hit、duplicate
  object/feature IDs、malformed identity、wrong project 和 stale revision 全部固定 fail closed，
  不修复、不重发 ID、不回退到名称或对象顺序。
- `result_ref` 不嵌套进 persistent selector。本批保留 S3-1 result-ref 契约；S3-3 把 operation
  target 定义为 `SelectorV1 | ResultRef` 封闭 union，并完成默认 registry migration。

### 4. Observation and preservation contract

- sealed ObservationSnapshot 增加按 object_id 排序、唯一的 per-entity observation；至少绑定
  identity、TypeId、role、provenance、白名单参数、规范 Placement、volume、area、bbox、
  center of mass、valid_shape 和 solid_count，全部进入 observation digest。
- executor 对 commit-authority observation 必须重载 sealed candidate FCStd 后采集，并与 live
  candidate 对应事实交叉检查；handler result、模型字段、图片或 Name 不能提供 trusted facts。
- validation 层提供纯确定性的 preservation comparison 与
  `AcceptanceKind.PRESERVATION / unchanged` check；missing before/after、identity/inventory 漂移、
  非目标对象变化或声明字段变化均不得得到 passing observation。
- S3-2 完整证明“已有 FCStd 对象”的 base-vs-sealed preservation。单个 program 内
  `create → modify` 的中间态 before/after capture 与默认 operation target union 在 S3-3
  固定 handler binding 时完成；本批不得声称已覆盖。
- 未经 isolated identity normalization 的外部/legacy FCStd 不公开 selector；import bootstrap
  在 S3-7 project ingress 完成。managed identity 一旦存在，非法或重复只拒绝，不静默重键。

### 5. RED, GREEN and managed gates

Genuine RED 固定为三个目标行为：旧 5-field selector 必须被完整 Level A schema 拒绝；required
preservation criterion 必须可编译；sealed evidence 必须是 reload-bound per-object observation。
import/setup/syntax failure 不计 RED。

Focused GREEN：

```text
PYTHONPATH=src uv run pytest -q tests/test_object_selectors.py tests/test_engine_session.py
tests/test_execution_registry.py tests/test_model_program.py tests/test_execution_adapter.py
tests/test_acceptance_verifier.py tests/test_program_executor.py tests/test_task_service.py
tests/test_candidate_revision.py tests/test_revision_store.py
```

Managed FreeCAD 使用唯一现有 ready runtime，不安装第二套环境；至少证明：双对象中只解析并
修改目标；对照对象逐字段不变；identity 在 recompute、checkpoint、close/load/recompute 后
保持；duplicate copied UUID、zero hit 和 stale revision 拒绝；失败不推进 HEAD。

完成前还必须通过全仓 pytest、全仓 Ruff、`git diff --check` 和至少一位未参与写入 agent 的
最终只读 review。

### 6. Delivery and recovery boundary

- 预写 commit：`feat(validation): add stable object selectors and preservation checks`。
- 只做 named-file staging 和本地 commit；push 固定 not authorized / S3-RES-01。
- 任一 unexpected regression、live/reload observation mismatch、identity 自动修复、Name fallback、
  selector 跨 project/revision 解析、Level B 偷渡或需要写出 allowlist 立即 breaker。

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E03 / 2026-07-21T03:52:07Z | S3-A01；S3-2A three-way design gate PASS | dc8250e / not authorized | clean anchor；focused baseline 369 passed, 5 deselected；selector/test/managed-runtime analyses converged | S3-RES-01, S3-RES-04, S3-RES-07 | S3-S03 | packet-issued |

### Recovery snapshot S3-S03

1. **Completed:** S3-1 committed at `dc8250e`；S3-2 selector、observation、preservation 和真实
   FreeCAD gate 已由三路只读分析收敛；Packet S3-2A issued；push not authorized。
2. **Next:** commit this packet record；写三个 genuine RED；实现最小 strict selector/property
   lifecycle/per-entity snapshot/preservation verifier；跑 focused、full、managed gates；独立 review；
   本地语义 commit。
3. **Do not broaden:** default operation target/result migration、create→modify intermediate trace、
   import normalization、face/edge fingerprint selector、Workbench/daemon 均不属于 S3-2。
4. **Recovery:** verify branch、clean worktree、HEAD `dc8250e` 和 packet allowlist；长命令若返回
   session_id 只能轮询原 session；不得重放已经完成的 S3-1 或安装第二套 FreeCAD。

## 9. 用户决策与持续执行规则

本修订依据已经明确的用户方向：

- 没有实际用户，不承担旧 endpoint 行为兼容；
- 原 31 项能力可以 Agent-first 化，而不是受固定工具数限制；
- VibeCAD 使用用户自带的 Claude/Codex 等宿主模型；
- VibeCAD 聚焦 CAD 编排，不自研照片重建、逆向和仿真底层引擎；
- headless 不是强约束，可以开发 FreeCAD 插件支持交互设计；
- 没有真正产品级决策时持续推进，不以内部 blocker 编号要求用户批准。

因此，S3-R3 内部实现、测试、修复、review 和文档可以连续进行。以下动作仍不由本计划
自动授权：

- push 到远端、创建 PR、发布版本或提交 marketplace；
- 产生外部模型/API 费用；
- 删除用户数据；
- 扩大到本计划明确后移的产品能力。

下一次需要用户参与的合理节点，是 AR-1 发现必须改变上述产品边界，或 G1 Workbench
需要确定首发交互范围时；不是每个测试 blocker 的关闭。

## 10. 完成定义

S3-R3 完成时，用户应能看到：

1. Claude/Codex 既能调用简单直接 CAD 工具，也能提交多步 ModelProgram；
2. 两条路径都生成隔离候选、可信证据和可恢复 Revision；
3. 用户可以选择 auto commit，或得到可跨重启接受/拒绝的 draft；
4. 原始 FCStd 永不被直接污染；
5. get_capabilities 能说明每项 operation 的 profile、schema 和限制；
6. managed FreeCAD 真实运行首批 operation 和恢复矩阵；
7. FreeCAD Workbench G1 有稳定 Kernel 接口可接，而不是重做任务系统。

后续按产品路线进入 G1/P1：Python Workbench、GUI 选择、候选预览、Accept/Reject、
参数微调、完整 SelectorV1、PartDesign 和 STL 到 faceted STEP。
