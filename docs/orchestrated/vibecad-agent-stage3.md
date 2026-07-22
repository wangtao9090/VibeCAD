# VibeCAD Agent-first Stage 3 编排计划

- Campaign: vibecad-agent-stage3
- Revision: S3-R3
- Control addendum: S3-R3.1
- Active control addendum: S3-R3.2
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

### S3-R3.2 — dependency-order correction after S3-3

S3-3 的真实实现和两路只读架构复审证明，原 S3-4 把 direct MCP generation 放在
Application API、durable review、project bootstrap 和 verified artifact delivery 之前，形成了
依赖倒置：direct adapter 无法在这些依赖不存在时满足 S3-D03 的 task/draft/verdict/artifact
等价契约。S3-R3.2 只修复 S3-4 至 S3-8 的拓扑顺序，不改变 S3-D01 至 S3-D08、八个生产
语义提交预算、产品结果、信任边界或外部权限，因此不需要新的产品级批准。

替换顺序固定为：bounded Application/Task API contracts → durable draft/review → isolated
AgentApplication/runtime/bootstrap → verified artifact + MCP/direct manifest cutover → skill/E2E。
尚无 domain service 的 project/review/artifact 接口不得在 S3-4 伪装成可调用 placeholder；它们
只共享统一 envelope 规则，并在各自依赖完成后公开。Direct public MCP 固定从 S3-4 后移到
S3-7；AR-1 固定在 S3-7 后、S3-8 文档冻结前执行。

下表是 S3-R3.2 的 active future execution partition；第 5 节原 S3-4 至 S3-8 表格和对应小节
作为 S3-A01 绑定历史原样保留，但其未来执行顺序与 scope 由本表显式 supersede。S3-1 至
S3-3 的完成事实不受影响：

| ID | Active prewritten English commit | Active semantic scope | Active gate |
|---|---|---|---|
| S3-4 | `feat(application): add bounded task API contracts` | 统一 envelope、strict ingress、Task adapter、registry capability projection；无 MCP/runtime/FreeCAD | task API、budget/error/import、capability determinism |
| S3-5 | `feat(workflow): add durable draft review` | 原 immutable draft、review policy、Accept/Reject、lease/CAS/restart scope | 原 workflow/candidate/restart matrix |
| S3-6 | `feat(application): compose isolated task project runtime` | AgentApplication、CadExecutionPort、revision-zero/import bootstrap、lazy per-project runtime、durable data root、uninstall-preserves-data、managed checkout/IPC G0 | application/lazy-import/restart/isolation/runtime-data/capability |
| S3-7 | `feat(mcp): publish verified agent CAD surface` | verified artifact materialization first；stable controls + registry direct tools + atomic manifest/legacy-public cutover；S3-6 runtime-data cumulative regression | export/path/server/manifest/equivalence + uninstall regression |
| S3-8 | `feat(agent): package skill and complete stage 3 acceptance` | 原 skill、docs、version/distribution、真实 E2E scope；AR-1 先行 | full/package/managed/conformance |

Active S3-6 的 data-root isolation 与 `uninstall_runtime` preserves data 是 bootstrap 的前置交付，
不能等到 S3-7 才实现；原 S3-7 小节中的同名 bullet 在 active partition 中只作为累计回归门。
Active AR-1 也由原 S3-5 后的位置改为 S3-7 完成后执行；原小节位置仅保留历史。

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
- S3-R3.2 active correction: 上一句的未来 cutover stage superseded 为 S3-7；原句只作为
  S3-A01-bound history 保留。
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
| S3-RES-10 | 已有对象的 SelectorV1 已稳定，但默认 mutator 的 `SelectorV1 | ResultRef` target union 与同一 program 的 create→modify 中间态 preservation 尚未绑定 | S3-3 | planned | 六个首批 operation、固定 handler binding 与 command-level preservation gate 通过 |

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

### S3-2 completion record

实际修改范围严格落在 Packet S3-2A allowlist：

- `src/vibecad/engine/session.py`
- `src/vibecad/execution/__init__.py`
- `src/vibecad/execution/adapter.py`
- `src/vibecad/execution/executor.py`
- `src/vibecad/execution/registry.py`
- `src/vibecad/execution/selectors.py`
- `src/vibecad/validation/__init__.py`
- `src/vibecad/validation/checks.py`
- `src/vibecad/validation/contracts.py`
- `src/vibecad/validation/engine.py`
- `src/vibecad/workflow/program.py`
- `tests/test_acceptance_verifier.py`
- `tests/test_candidate_revision.py`
- `tests/test_engine_session.py`
- `tests/test_execution_adapter.py`
- `tests/test_model_program.py`
- `tests/test_object_selectors.py`
- `tests/test_program_executor.py`
- `tests/test_revision_store.py`
- `tests/test_task_kernel_integration.py`
- `docs/orchestrated/vibecad-agent-stage3.md`

Genuine RED 首先以 3 个目标失败证明旧实现仍接受不完整 selector、不能编译 required
preservation criterion，且 sealed evidence 未绑定 FCStd reload/per-object observation。最终独立
review 另发现缺失 `VibeCADFeatureId` 会被误解为合法 `None`；补充的逐字段删除 RED 精确得到
`1 failed, 4 passed`，证明只有该缺口被命中。修复后五个 required identity 字段均缺一即
`MALFORMED_IDENTITY`，不把未经完整 normalization 的 legacy 对象纳入 managed selector。

SelectorV1 Level A 现在使用 project/revision/object/feature UUID、TypeId、semantic role 与
provenance 的完整 exact match；不回退 Name、Label、对象顺序或 FaceN/EdgeN。identity 以四个
locked persistent FreeCAD 属性跨 recompute、checkpoint、close/load 保持。commit authority
从 sealed FCStd reload 后采集按实体 observation，并把 base-vs-sealed preservation、artifact
hash 与 durable revision recheck 一起绑定；zero、duplicate、stale、wrong-project 和 partial
managed inventory 全部 fail closed，失败不会推进 HEAD。

#### Append-only residual disposition

| Update | Evidence | State |
|---|---|---|
| S3-RES-07-C1 | SelectorV1 Level A、recompute/close-load、sealed reload、zero/duplicate/stale 与 base preservation 真实 FreeCAD gate 均通过 | closed by S3-E04 |
| S3-RES-10-O1 | default operation target union 与 create→modify command-level preservation 明确留给 S3-3，不影响 S3-2 已有对象契约 | open / planned |

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E04 / 2026-07-21T04:43:17Z | S3-A01；S3-2A；独立 executor review PASS；最终独立 review 的 1 个 Important 经 genuine RED 关闭后 PASS | S3-2 semantic commit containing this snapshot / not authorized | initial RED 3 targeted failures；review RED 1 failed/4 passed；focused 994 passed, 3 deselected；full 2426 passed, 87 deselected；managed FreeCAD 6 passed；full Ruff passed；diff check passed | S3-RES-01, S3-RES-04, S3-RES-10；closes S3-RES-07 | S3-S04 | completed |

### Recovery snapshot S3-S04

1. **Completed milestones:** repository VibeCAD；branch `codex/agent-stage3`；S3-1 与 S3-2
   production contracts complete；本 snapshot 与 S3-2 生产改动由预写本地语义 commit
   `feat(validation): add stable object selectors and preservation checks` 固化；push remains
   not authorized / S3-RES-01。
2. **Next steps:** 从已提交 S3-2 HEAD 签发 Packet S3-3A；RED 必须分别命中默认 registry
   仍非首批六操作、mutator 仍不接受 `SelectorV1 | ResultRef` 封闭 target union，以及
   `ModelCommand.preserve` 尚未约束 create→modify 中间态；若三者精确命中则实现固定 handler
   binding，随后跑 focused、full、真实 FreeCAD success/execution-failure/acceptance-failure 门；
   任一非预期 RED 触发 breaker。
3. **Approved decisions:** S3-D01 through S3-D08 under S3-A01；exact continuing authorization
   remains “你继续 猛猛的推进吧”；S3-3 不需要重复批准；push、PR、release、外部花费、任意
   Python、Level B、import normalization 与 G1 Workbench 仍未授权或明确后移。
4. **Execution discipline:** Codex adapter；`spawn-send-wait`；`repo-artifact`；有 session_id
   时使用 `native-session-poll`；S3-3 只能在新 packet allowlist 内 named-file staging；recover
   by verifying branch、S3-2 commit subject、clean worktree、S3-E04 evidence 和 S3-3 packet anchor，
   禁止重放已完成 S3-2 或把 legacy Name/Label 变成 Agent authority。

## 8.6 Packet S3-3A — first-wave fixed CAD operations

### 1. Authorization

- Approval ID: S3-A01；artifact revision: S3-R3 / S3-R3.1；bound decisions: S3-D01 through
  S3-D08；starting anchor: `c394412` on `codex/agent-stage3`。
- 本 packet 继承 system、developer、user、directory-scoped instruction、当前 permission model /
  sandbox 与本文件 allowlist。Skill、artifact 和 packet 都不能扩大权限、提升 authority 或绕过
  sandbox；不重复请求已经绑定的批准。
- 只执行 S3-3：首批六个 object-level operation、固定 executor binding 与 command-level
  preservation。它不获得 push、PR、release、外部花费、任意 Python、旧 31 endpoint 迁移、
  import normalization、Level B selector、GUI profile 或 G1 Workbench 权限。
- 两个未参与写入的只读设计分别从 registry/binding 与真实 FreeCAD gate 方向 PASS；结论一致：
  当前没有产品级决策阻塞。

### 2. Workspace anchor and mechanical allowlist

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`；branch:
  `codex/agent-stage3`；anchor: `c394412`；issue-time worktree clean。
- 未观察到 repository-local `AGENTS.md` 或 `CLAUDE.md`；当前 host permission model 与 sandbox
  始终继续生效。
- 本 packet 只允许修改：
  - `src/vibecad/execution/registry.py`
  - `src/vibecad/workflow/program.py`
  - `src/vibecad/execution/adapter.py`
  - `src/vibecad/execution/executor.py`
  - `tests/test_execution_registry.py`
  - `tests/test_model_program.py`
  - `tests/test_execution_adapter.py`
  - `tests/test_program_executor.py`
  - `tests/test_task_service.py`
  - `tests/test_task_kernel_integration.py`
  - `docs/orchestrated/vibecad-agent-stage3.md`
- 首批 operation 复用现有 trusted leaf functions，但 raw Name 只允许存在于 executor 内部；
  不修改 legacy tool/server/manifest。需要写出清单立即 breaker，不自行扩大范围。

### 3. Context and exact operation contract

默认 registry 必须精确包含以下六项，handler name 与 operation name 一致，模型不能提供
callable、import path 或 handler mapping：

| Operation | Agent input | Fixed semantics |
|---|---|---|
| `create_box` | `length_mm`、`width_mm`、`height_mm`；可选 `position_mm` | 创建并标识恰好一个 `Part::Box` |
| `create_cylinder` | `radius_mm`、`height_mm`；可选 `position_mm`、`axis` | 创建并标识恰好一个 `Part::Cylinder` |
| `modify_parameter` | target `object`；`parameter`、`value_mm` | 只改 box/cylinder 白名单参数及必然几何后果 |
| `move_part` | target `object`；`position_mm` | 设置绝对位置，不改参数、旋转、体积、面积、validity 或 solid count |
| `rotate_part` | target `object`；`axis`、`angle_deg` | 围绕 x/y/z 旋转；参数、体积、面积、validity 和 solid count 不变 |
| `inspect_model` | 无 target/args | 返回按 object_id 排序的 per-object observation 与完整 managed aggregate |

Registry 增加封闭 shape：strict `OBJECT_ID`、`ENTITY_TARGET` 与 `ANGLE_DEGREES`。
`ENTITY_TARGET` 只接受两种 exact mapping：与 program base revision 绑定的 `SelectorV1`，或
指向 dependency closure 内 `OBJECT_ID` result slot 的 `ResultRef`。不接受裸字符串、Name、
Label、对象顺序、部分 selector 或跨 project/revision selector。创建结果的 `object` slot 从
top-level `object_id` 提取；新建对象在同一 program 内只用 ResultRef，提交后才生成新 revision
绑定的 persistent selector。

所有长度使用 mm、角度使用 degree；`position_mm` 是有限 vector3；`axis` 仅 x/y/z；
`angle_deg` 为 `(-360, 360)` 内非零有限数；`parameter` 仅 length/width/height/radius，并由
fixed handler 继续执行 TypeId×parameter 矩阵。

每条命令执行前后都从 live candidate 采集完整 identified inventory：

- create 只允许新增一个受管 primitive；已有实体逐项不变；identity provenance 绑定
  `command.source` 和 `command.id`；结果由实际 object/entity observation 重建，不信任旧 tool dict。
- mutator 先按 selector/object_id 唯一解析，再把内部 `obj.Name` 交给 trusted leaf；执行后固定
  检查 identity、目标字段、非目标字段和所有非目标实体。`ModelCommand.preserve` 只能增加检查，
  不能削弱固定不变量；create/inspect 的非空 preserve 在 handler 前拒绝。
- move 必须精确达到请求位置且保持原 quaternion；rotate 必须产生 placement 变化；两者都保持
  parameters、volume、area、valid_shape 和 solid_count。modify 保持 placement 与所有非目标参数。
- inspect 前后 inventory 必须完全一致。handler result 保存 strict before/after 或 inspection trace，
  供 durable StepResult 诊断；commit authority 仍只来自 sealed/reloaded CandidateEvidence。
- aggregate observation 与 STEP export 必须覆盖完整 managed primitive inventory，不能沿用单一
  legacy result root 而静默遗漏第二个对象。

### 4. Steps and objective gates

1. 在 allowlist 内写三个 genuine focused RED：
   - default registry 仍只有三项，而不是上述精确六项；
   - default mutator 仍只接受 ResultRef<string>，不能接受 `SelectorV1 | ResultRef<OBJECT_ID>`；
   - `create_box → modify_parameter(preserve=("length",))` 当前仍会成功，证明 command-level
     preservation 尚未执行。
   setup/import/syntax failure 不计 RED；任一失败未命中预期路径立即 breaker。
2. 实现最小 registry/program/adapter/executor correction。不得加入 boolean、PartDesign、assembly、
   face/edge selector、动态 handler、public MCP generation 或 GUI 代码。
3. Focused GREEN：
   `PYTHONPATH=src uv run pytest -q tests/test_execution_registry.py tests/test_model_program.py
   tests/test_execution_adapter.py tests/test_program_executor.py tests/test_task_service.py`。
4. Managed FreeCAD G3：至少覆盖六操作成功、base Selector 与 same-program ResultRef、双对象
   aggregate/STEP/reload、execution preservation failure rollback、acceptance failure rollback；失败场景
   必须证明 HEAD/manifest/generation/base hash/live slot 不变、后续 command 未执行且无 retry。
5. 累计 gate：全仓 pytest、全仓 Ruff、`git diff --check`；至少一位未参与写入 agent 做最终
   read-only diff review。Baseline：上述六个现有 focused/integration 文件在 anchor 上为
   `400 passed, 6 deselected in 3.00s`。

### 5. Execution discipline and circuit breakers

- Revalidated profile：approval `native-plan`；delegation `spawn-send-wait`；persistence
  `repo-artifact`；process `native-session-poll`；adapter `Codex`；model tier `standard` for
  implementation、`deep` for independent review（选择不可用时只记录 fallback，不降低 gate）。
- `live capability declarations`: update_plan；spawn_agent/send_message/followup_task/wait_agent；
  apply_patch/exec_command/write_stdin 均在当前 tool declarations 中。
- `observable behavior`: 本阶段已观察到 native plan update、spawn/send/wait completion、named-file
  commit，以及 exec_command 返回 session_id 后由 write_stdin 轮询到真实 exit 0。
- `environment identity`: Codex Desktop controller `/root`；workspace
  `/Users/wangtao/Documents/DevProject/vibecad`；未使用 repo 内容证明 host capability。
- `public configuration`: filesystem unrestricted、approval policy never；这些只约束当前执行，
  不证明用户批准，也不扩大外部 authority。
- 同一生产文件同一时刻只由 controller 写；sub-agent 只做独立分析/review。长进程一旦返回
  session_id，只轮询原 session。unexpected red、out-of-allowlist write、Name authority、遗漏 managed
  object 的 artifact、preservation 可被模型削弱或需要改变 S3-D01..D08 都立即 breaker。

### 6. Delivery boundary

- 预写本地语义 commit：`feat(execution): migrate first-wave CAD operations`。
- 只做 named-file staging、本地 commit、ledger 和 recovery snapshot；controller 保留 acceptance /
  revert 权。Push 固定 `not authorized` / S3-RES-01；不创建 PR 或 release。
- S3-3 完成只表示 ModelProgram/Task Kernel 可执行首批六操作；公共 direct MCP 工具仍属于 S3-4，
  不提前宣称用户已经能直接看到六个新 MCP endpoint。

### 7. Final report contract

完成时追加 actual file list、RED/GREEN/full/Ruff/managed FreeCAD 数字证据、独立 review、commit
hash/push state、residual disposition 与四节 recovery snapshot。工作树必须 clean；S3-RES-10 只有在
target union 与 command-level preservation 均通过真实门后才能关闭。

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E05 / 2026-07-21T04:49:12Z | S3-A01；S3-3A two-way read-only design PASS；independent packet review PASS | c394412 / not authorized | clean anchor；focused/integration baseline 400 passed, 6 deselected；registry and G3 gate designs converged；seven-section/allowlist/scope review PASS | S3-RES-01, S3-RES-08..10 | S3-S05 | packet-issued |

### Recovery snapshot S3-S05

1. **Completed:** S3-2 committed at `c394412`；worktree clean；Packet S3-3A issued from two
   independent read-only designs and one independent packet review PASS；baseline 400 passed /
   6 deselected；push not authorized。
2. **Next:** commit this control packet；write the three exact REDs；if they match, implement only
   fixed six-operation registry/bindings/preservation；run focused, full, managed FreeCAD and review；
   if any branch condition mismatches, stop at the named breaker rather than broadening scope。
3. **Approved decisions:** S3-D01..D08 under S3-A01；exact authorization “你继续 猛猛的推进吧”；
   no repeated approval for this packet；S3-4 direct MCP、S3-7 import normalization、G1 and external
   actions remain outside。
4. **Recovery:** verify `codex/agent-stage3`、HEAD `c394412`、packet allowlist and clean worktree；
   verify no active test session before RED；use only named-file staging；if packet control commit is
   present, anchor implementation to that commit and never replay S3-2。

### S3-3 completion evidence

Actual write set stayed inside Packet S3-3A:

- production: `src/vibecad/execution/registry.py`、`src/vibecad/workflow/program.py`、
  `src/vibecad/execution/executor.py`；
- focused tests: `tests/test_execution_registry.py`、`tests/test_model_program.py`、
  `tests/test_execution_adapter.py`、`tests/test_program_executor.py`；
- managed gate: `tests/test_task_kernel_integration.py`；
- orchestration record: `docs/orchestrated/vibecad-agent-stage3.md`。

Evidence chain:

1. The three packet REDs failed for their intended reasons: the default registry exposed only three
   operations, ENTITY_TARGET could not bind the Selector/OBJECT_ID union, and command preservation
   did not reject `create_box -> modify_parameter(preserve=("length",))`.
2. Independent review produced two more genuine REDs: custom-registry metadata could reach the fixed
   executor without default-authority rebinding, and duplicate preserve fields were not rejected
   before CAD execution. Both now fail at the closed validation boundary.
3. Managed FreeCAD produced and closed two numeric/runtime REDs: OCC rigid-motion roundoff required a
   mixed derived-geometry tolerance while parameter/placement checks remain strict; FreeCAD's
   `Part.Compound` has no aggregate `CenterOfMass`, so the observation boundary now derives the
   volume-weighted center from its solids and fails closed on an invalid aggregate.
4. Final review produced and closed three Important findings: rotation now rejects an otherwise-correct
   quaternion with an extra translation; rollback asserts the durable base revision/hash and live
   baseline; and rotation uses the live target `Shape.BoundBox` center rather than assuming its center
   of mass is the pivot. The last case has both a non-concentric fake regression and a real imported
   180-degree Cylinder TaskService -> commit -> FCStd/STEP reload gate.
5. Final focused gate: `431 passed, 1 deselected`; full gate: `2457 passed, 89 deselected` with the two
   pre-existing macOS `fork()` deprecation warnings; managed Task Kernel plus adapter gate:
   `8 passed, 75 deselected`; full Ruff、`git diff --check` and pycompile passed.
6. Two independent read-only reviewers found no Critical or Important issue. Non-blocking hardening for
   a later cumulative gate is real FreeCAD x/y cylinder orientation, repeated non-identity rotations,
   and an explicit cross-FCStd/STEP aggregate-center assertion; current unit and managed geometry,
   identity, artifact and rollback gates pass.

| Residual disposition | Result |
|---|---|
| S3-RES-10-C1 | closed: default `SelectorV1 \| ResultRef<OBJECT_ID>` targets, six fixed handlers and real command-level preservation rollback passed |
| S3-RES-01 | remains: push is not authorized |
| S3-RES-04, S3-RES-08, S3-RES-09 | remain in their planned later stages; S3-3 did not broaden into face/edge selectors, runtime budget enforcement or GUI profiles |

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E06 / 2026-07-21T05:30:46Z | S3-A01；S3-3A；two independent final read-only reviews PASS after closing three final-review Important findings | S3-3 semantic commit containing this snapshot / not authorized | packet RED 3；review RED 2；managed runtime RED 2；final-review findings 3 closed；focused 431 passed, 1 deselected；full 2457 passed, 89 deselected；managed 8 passed, 75 deselected；Ruff/diff/pycompile PASS | S3-RES-01, S3-RES-04, S3-RES-08..09；closes S3-RES-10 | S3-S06 | completed |

### Recovery snapshot S3-S06

1. **Completed:** S3-3 implements the six-operation Agent-first execution wave, default-authority
   rebinding, stable target union, strict observation-derived results, managed aggregate export and
   real preservation/acceptance rollback; all objective gates and two independent reviews passed。
2. **Next:** create the S3-4 control packet from committed S3-3 HEAD; implement only public bounded
   Task API envelopes/lifecycle tools and capability discovery named by the plan; do not replay S3-3
   or expose arbitrary Python/FreeCAD execution。
3. **Approved decisions:** S3-D01..D08 under S3-A01 and the user's standing instruction to continue
   without internal approvals; direct public MCP work starts only in S3-4; push/PR/release remain
   unauthorized。
4. **Recovery:** verify branch `codex/agent-stage3`, the local S3-3 semantic commit, clean worktree and
   no active test process; rerun the focused gate plus managed G3 before any S3-4 mutation; if the
   semantic commit is absent, use Packet S3-3A and this evidence record rather than replaying earlier
   stages。

### S3-S06-C1 — active future-stage correction

S3-R3.2 supersedes only the future-stage references in S3-3A/S3-S05/S3-S06: bounded Task API contracts
start in S3-4, while direct public MCP/manifest cutover starts in S3-7 after draft, runtime/bootstrap and
verified artifact dependencies exist. Historical packet/snapshot wording above remains unchanged evidence.

## 8.7 Packet S3-4A — bounded Task API contracts

### 1. Authorization

- Approval ID: S3-A01；artifact revision: S3-R3 / S3-R3.1 / S3-R3.2；bound decisions:
  S3-D01 through S3-D08；starting anchor: `479d182` on `codex/agent-stage3`。
- 本 packet 继承 system、developer、user、directory-scoped instruction、当前 permission model /
  sandbox 与本文件 allowlist；它不能扩大权限或把依赖顺序纠正解释成新产品 scope。
- 只执行 S3-4：transport-neutral public envelope、strict bounded request/ModelProgram JSON ingress、
  executable conforming TaskServicePort adapter 和 immutable registry capability projection。它不获得
  MCP/server/manifest、filesystem/runtime/FreeCAD、project bootstrap、draft/review、artifact
  materialization、direct CAD tool、push、PR、release 或外部花费权限。
- 两个未参与写入的只读架构复审一致确认 S3-R3.2 与本边界不改变产品定位，当前没有用户
  产品级决策阻塞。调研对 decoded mapping 与 inner `program_json` 的边界有分歧；controller
  选择 bounded JSON string，因为 transport decoder 会丢失 duplicate-key 事实，而 S3-7 MCP
  必须能够在构造 ModelProgram 前拒绝它。

### 2. Workspace anchor and mechanical allowlist

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`；branch:
  `codex/agent-stage3`；anchor: `479d182`；issue-time worktree only contains this S3-R3.2 documentation
  correction。
- 未观察到 repository-local `AGENTS.md` 或 `CLAUDE.md`；当前 host permission model 与 sandbox
  始终继续生效。
- S3-R3.2 control commit 允许且只允许修改：
  - `docs/orchestrated/vibecad-agent-stage3.md`
  - `docs/AGENT_ARCHITECTURE.md`
- S3-4 semantic implementation 允许且只允许修改：
  - `src/vibecad/application/__init__.py`（new）
  - `src/vibecad/application/task_api.py`（new）
  - `tests/test_task_api.py`（new）
  - `docs/orchestrated/vibecad-agent-stage3.md`
- 不修改 workflow service/state/program/store、registry metadata、server、manifest 或 legacy Session。
  需要写出清单立即 breaker，不自行扩大范围。

### 3. Context and exact public contract

Application adapter 使用 injected internal `TaskServicePort`。Port 的方法返回 exact
`StoredTaskRun | TaskServicePortFailure`，其中 failure code 与内部 TaskService error taxonomy
一一对应；任意 raise 都视为 unexpected internal error。S3-4 只对 conforming injected port 可执行，
不 import concrete TaskService/executor/engine/runtime/server/MCP/FreeCAD；S3-6 负责把 concrete
TaskService exception/result 显式桥接为该 neutral port contract。第一版可执行方法精确为：

| Method | Exact request | Fixed behavior |
|---|---|---|
| `create_task` | schema version + project_id | API 生成 `task_<32hex>`；固定 external_plan；一次 create，碰撞 conflict、零 retry |
| `get_task` | schema version + task_id | 只读 StoredTaskRun；不初始化 CAD |
| `submit_model_program` | schema version + task_id + expected_generation + `program_json` | bounded duplicate-aware decode → ModelProgram → 单次 service submit |
| `resume_task` | schema version + task_id + expected_generation | PROGRAM_READY→continue；active/recovery/cleanup→reconcile；terminal→幂等 read；CREATED/needs plan/input→invalid_state |
| `get_capabilities` | schema version only | 从 registry 排序投影 schema/profile/risk/budget/result/preservation；不声称 runtime 可用 |

每个方法只接收 exact built-in JSON-compatible request mapping；拒绝 unknown/missing field、custom
Mapping/container subclass、tuple/bytes、cycle/alias、非法 Unicode、非有限数和 unsafe integer。
`program_json` 是 UTF-8 字符串，raw budget 精确 512 KiB，decode 前检查最大 depth 64；decode
使用 duplicate-key hook、finite/length-bounded number hooks，随后检查最大 8192 nodes、64 KiB
string 和 256-byte key，再调用 `ModelProgram.from_mapping()`。create/get/resume/capabilities 的完整
canonical request 最大 4096 bytes。Submit 将不含 `program_json` 的 metadata canonical mapping
限制为 4096 bytes、raw `program_json` 限制为 524288 UTF-8 bytes，并把两者逻辑总和限制为
528384 bytes；三个边界都做 N/N+1 测试。TaskService 仍在 S3-6 bridge 后复核 canonical
ModelProgram 最大 524288 bytes。

Response 始终是 exact four-field envelope：

```json
{"schema_version":1,"ok":true,"result":{},"error":null}
```

或：

```json
{"schema_version":1,"ok":false,"result":null,"error":{"schema_version":1,"code":"invalid_input","path":"/program_json","message":"The request is invalid."}}
```

Task result 精确包含 `generation`、`TaskRun.next_action.value` 和 `task_run.to_mapping()`。
Public routing 固定为：`submit_program|provide_input` → `submit_model_program`；
`validate_program|reconcile|cleanup` → `resume_task`；`wait` → `get_task`；`none` → stop；
`request_plan` 对成功 create 结果不可达。所有 public errors 使用 bounded canonical path 和按 code
固定的 message。Closed code set 精确为 `missing_field`、`unknown_field`、`unsupported_version`、
`invalid_type`、`invalid_value`、`budget_exceeded`、`invalid_input`、
`unsupported_reasoning_owner`、`invalid_state`、`not_found`、`conflict`、`store_failure`、
`lease_unavailable`、`recovery_required`、`internal_error`。Outer/JSON decode error path 固定从
`/program_json` 开始，ModelProgram nested path 重定位到 `/program_json...`；port service/internal
error path 固定 `""`。TaskServicePortFailure 全量显式映射，unexpected exception 或非 exact
StoredTaskRun 只返回 `internal_error`，不得向外抛出或反射原异常、路径、request value。
`resume_task` 先按 generation 读 durable state，再只分派一次；它不规划、不 repair、不做语义 retry。

Capability result exact keys 为 `registry_schema_version` 和 `operations`。Operations 按 operation
name 排序；target/argument fields、result slots 和 preservation fields 都按 public name 排序。
每个 operation 只包含 public field name/value shape/required/enum/unit/ref shape、risk、evidence、
execution profile、FreeCAD version range、GUI main-thread flag、exact resource-budget mapping、
direct_exposed、public result slot 和 preservation fields；明确排除 `handler_name`、
`handler_parameter`、`result_field`、callable、source/import path、installed runtime claim。

### 4. Steps and objective gates

1. 写三个 genuine RED wave：
   - `find_spec("vibecad.application.task_api")` 当前为 none；
   - skeleton 后 exact five-method request/envelope、API-owned ID/external_plan、StoredTaskRun 映射和
     resume 13-state dispatch（CREATED included）和尚未冻结的 next_action routing 不存在；
   - duplicate key、depth/nodes/UTF-8/NaN/unsafe integer/size、nested program error path、port failure
     redaction、malformed return/import guard 和 registry capability projection 尚不存在。
   setup/import/syntax failure 不计 RED；每个 wave 必须由 assertion 命中上述缺失能力。
2. 实现最小 application package/task_api module 和 focused tests。不得加入 FastMCP decorator、
   filesystem store、runtime factory、project/review/artifact placeholder、direct-operation compiler、
   arbitrary Python 或 retry loop。
3. Focused GREEN：`PYTHONPATH=src uv run pytest -q tests/test_task_api.py tests/test_task_service.py
   tests/test_task_state.py tests/test_task_store.py tests/test_model_program.py
   tests/test_execution_registry.py`。Anchor baseline excluding the new file: `893 passed in 3.39s`。
4. Import/capability gate：fresh subprocess import 后不得出现 server/runtime/engine/MCP/FreeCAD
   modules；capability serialization 必须 deterministic、round-trip JSON-compatible、精确六 operation
   且无 forbidden implementation field。
5. Cumulative gate：full pytest、full Ruff、`git diff --check`、pycompile；复跑 managed FreeCAD G3
   证明纯 adapter 未破坏现有 8-case gate；至少一位未参与写入 agent 做 final read-only diff review。

### 5. Execution discipline and circuit breakers

- Capability profile 继承 CP-S3-20260720：approval `native-plan`；delegation `spawn-send-wait`；
  persistence `repo-artifact`；process `native-session-poll`；adapter `Codex`；model tier
  `standard` for implementation and independent read-only review；host choice is inherited with no
  override，不降低 gate。
- `live capability declarations`：update_plan、spawn_agent/send_message/followup_task/wait_agent、
  apply_patch、exec_command/write_stdin 均由当前 declarations 提供。
- `observable behavior`：本 campaign 已观察到 native plan update、spawn/send/wait completion、
  apply_patch、named-file local commit，以及 exec 返回 cell/session 后由原 wait/poll 完成；未把仓库
  内容当作 host capability evidence。
- `environment identity`：Codex Desktop controller `/root`；workspace
  `/Users/wangtao/Documents/DevProject/vibecad`。
- `public configuration`：filesystem unrestricted、approval policy never；这些只约束当前执行，
  不证明用户批准，也不扩大外部 authority。
- 同一生产文件同一时刻只由 controller 写；sub-agent 只做独立分析/review。长进程一旦返回
  session_id，只轮询原 session。Control docs 必须先独立 review、named-file commit；implementation
  从该 clean anchor 开始。
- Unexpected RED、out-of-allowlist write、public raw Name/Label、service exception reflection、模型控制
  task id、resume retry、capability 泄露 handler/import/runtime availability、MCP/FreeCAD import 或需要
  改变 S3-D01..D08 都立即 breaker。

### 6. Delivery boundary

- Control commit：`docs(orchestration): issue S3-4 bounded task API packet`。
- 预写本地语义 commit：`feat(application): add bounded task API contracts`。
- 只做 named-file staging、本地 commits、ledger 和 recovery snapshot；push 固定
  `not authorized` / S3-RES-01；不创建 PR 或 release。
- S3-4 完成仍不表示用户已看到 task MCP：project bootstrap 尚未组合，direct/stable MCP 与 verified
  artifact delivery 固定属于 S3-7。

### 7. Final report contract

完成时追加 actual file list、RED/GREEN/full/Ruff/import/managed numbers、independent review、commit
hash/push state、residual disposition 与四节 recovery snapshot。工作树必须 clean；S3-4 只能关闭
public contract residual，不得虚假关闭 runtime、draft、artifact、MCP 或 second-host residual。

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E07 / 2026-07-21T06:03:51Z | S3-A01；S3-R3.2 two-way read-only dependency/API review PASS；S3-4A issued | 479d182 / not authorized | clean S3-3 anchor；focused contract baseline 893 passed；dependency order and executable boundary converged | S3-RES-01, S3-RES-03..06, S3-RES-08..09 | S3-S07 | packet-issued |

### Recovery snapshot S3-S07

1. **Completed:** S3-3 semantic commit `479d182`；S3-R3.2 dependency correction and Packet S3-4A
   prepared from two independent read-only reviews；baseline 893 passed；push not authorized。
2. **Next:** independently review and commit the two control docs；from that clean anchor write the three
   exact RED waves；implement only bounded Task API/capability contracts；run focused/full/import/managed
   gates and final review；then local semantic commit。
3. **Approved decisions:** S3-D01..D08 under S3-A01；S3-R3.2 is a topology correction, not a new
   product decision；direct MCP starts in S3-7；project/draft/runtime/artifact remain later packets；no
   repeated user approval is required。
4. **Recovery:** verify branch `codex/agent-stage3`、HEAD `479d182` or the subsequent S3-4 control
   commit、the exact allowlists and no active test process；if control commit exists, never replay S3-3；
   if semantic commit exists, use its completion evidence rather than rerunning implementation from packet。

### S3-4 completion evidence

Actual semantic files are exactly `src/vibecad/application/__init__.py`、
`src/vibecad/application/task_api.py`、`tests/test_task_api.py` and this append-only evidence update。
No workflow service/state/store/program、registry metadata、server、manifest、runtime、engine、MCP or
legacy Session file changed。

Evidence chain:

1. Three packet RED waves failed for the intended missing behavior: package discovery raised the explicit
   absent-package assertion；the five-method skeleton raised `NotImplementedError` at the callable envelope；
   capability projection was empty、unknown outer fields were accepted and duplicate-aware submit ingress
   was absent (`3 failed, 1 passed`)。No collection、setup or syntax failure was counted as RED。
2. The neutral adapter now owns `task_<32hex>` generation and `external_plan`、returns exact four-field
   envelopes、maps all eight port failures without importing concrete TaskService、projects durable
   generation/next_action/TaskRun and dispatches all thirteen resume states with one read plus at most one
   continue/reconcile call。Terminal resume is idempotent；collision and semantic retry count are zero。
3. Strict ingress enforces exact scalar-only outer requests、4096-byte canonical metadata、524288-byte raw
   program JSON and 528384-byte logical submit budgets；inner JSON is duplicate-aware、depth/node/string/key
   bounded、finite/safe-number checked and relocates ModelProgram paths below `/program_json`。A multibyte
   N+1 case proves the logical-sum branch while remaining within the code-point precheck。
4. Independent review generated and closed real adversarial REDs: an injected private `_ApiFailure` could
   escape as a trusted public error；a forged exact port failure could cross-cast a public code；deep outer
   input could recurse and grow a path beyond 256 bytes；and oversized text could allocate before its budget
   check。Raw factory/port calls are now isolated、port mapping is exhaustive、outer validation is iterative
   and scalar-only、overflow paths use fixed sentinels、and UTF-8/canonical accounting is bounded and streaming。
5. The first managed rerun exposed a host identity issue before CAD import (`8 failed`): this Codex process
   could not resolve UID 501 through the macOS user directory even though `HOME` existed。Using FreeCAD's
   supported existing-directory `FREECAD_USER_HOME=/Users/wangtao` override restored the same installed
   FreeCAD 1.1 runtime；the final managed Task Kernel + adapter gate is `8 passed, 75 deselected`。No runtime
   install or production code change was required。
6. Final gates: Task API `131 passed`；packet focused suite `1024 passed`；full suite `2588 passed,
   89 deselected` with the two pre-existing macOS `fork()` deprecation warnings；full Ruff、
   `git diff --check`、pycompile、fresh import/capability and forbidden-module checks all PASS。Two independent
   final read-only reviews report no remaining Critical、Important or Minor finding。

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E08 / 2026-07-21T07:03:26Z | S3-A01；Packet S3-4A；two independent final read-only reviews PASS after closing four adversarial boundary findings | S3-4 semantic commit containing this snapshot / not authorized | RED 1 + 1 + 3；review REDs closed；Task API 131；focused 1024；full 2588/89；managed 8/75；Ruff/diff/pycompile/import PASS | S3-RES-01..06, S3-RES-08..09；bounded Task API slice closed | S3-S08 | completed |

### Recovery snapshot S3-S08

1. **Completed:** S3-4 control commit `013414d`；bounded transport-neutral Task API、strict JSON ingress、
   neutral port contract、all-state resume routing and six-operation capability projection；all final gates
   and two independent reviews PASS；push not authorized。
2. **Next:** commit the exact S3-4 semantic allowlist；start active S3-5 durable draft/review packet from the
   clean semantic anchor；freeze immutable draft、review policy、Accept/Reject、lease/CAS and restart matrix
   before production mutation。
3. **Approved decisions:** S3-D01 through S3-D08 under S3-A01；S3-R3.2 active order；S3-5 does not expose
   MCP/runtime/project bootstrap/artifact delivery and requires no repeated product approval。
4. **Recovery:** verify branch `codex/agent-stage3`、HEAD `013414d` or the subsequent S3-4 semantic commit、
   no active pytest process and exact S3-4 allowlist；if semantic commit exists, do not replay RED or managed
   gates，continue from this snapshot into S3-5 design/review。

## 8.8 Packet S3-5A — durable draft/review

### 1. Authorization and dependency finding

- Approval ID: S3-A01；artifact revision: S3-R3 / S3-R3.1 / S3-R3.2；bound decisions:
  S3-D01 through S3-D08；starting anchor: `1c792aa` on `codex/agent-stage3`。
- 本 packet 只执行 S3-5：explicit review policy、immutable durable draft identity、review-aware
  TaskRun/state/service、Accept/Reject、sealed revision detach/re-prepare、lease reacquisition、exact HEAD
  CAS、restart/replay recovery，以及 S3-4 transport-neutral API 的窄幅七方法扩展。它不获得
  MCP/server/manifest、runtime/project bootstrap、artifact delivery、Workbench/IPC、manual revise、
  provider、push、PR、release 或外部花费权限。
- 三路只读审计一致确认：现有 sealed `RevisionRef`、Task generation CAS、project lease、HEAD CAS 和
  reconciliation 可复用，但现有唯一 `PREPARED` commit journal、进程内 `SealedCandidate` handle 和
  one-shot `VerificationReceipt` 都不能冒充 durable draft。长期保留 `PREPARED` 会阻塞同项目后续
  写入，并令 stale-base 分支不可达；跨重启持久化 receipt/Session/lease 又破坏当前 capability
  边界。因此 S3-5 必须先把 verified sealed revision 从活动提交事务安全脱离，Accept 时再在新 lease
  下重新准备、重新观察和重新验证。
- 这不是新产品决策：S3-D05 已明确 auto-commit 与 require-review 两条路径、Accept 的 HEAD CAS、
  Reject 不改 HEAD 和 restart recovery。S3-5 不引入 policy 默认值、自动 rebase、reviewer 权限、
  comment、expiry/retention/GC；这些才需要未来产品判断。

### 2. Workspace anchor and mechanical allowlist

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`；branch:
  `codex/agent-stage3`；anchor: `1c792aa`；issue-time worktree clean；focused baseline is
  `1180 passed` for revision/candidate/task-state/task-store/task-service/task-API suites。
- 未观察到 repository-local `AGENTS.md` 或 `CLAUDE.md`；当前 host permission model、sandbox 与
  system/developer/user constraints 始终继续生效。
- S3-5 control commit 只允许修改 `docs/orchestrated/vibecad-agent-stage3.md`。
- S3-5 semantic implementation 只允许修改：
  - `src/vibecad/workflow/state.py`
  - `src/vibecad/workflow/service.py`
  - `src/vibecad/execution/revisions.py`
  - `src/vibecad/execution/candidate.py`
  - `src/vibecad/application/task_api.py`
  - `src/vibecad/workflow/__init__.py`、`src/vibecad/execution/__init__.py`、
    `src/vibecad/application/__init__.py`，仅在公开 export 必需时修改
  - `tests/test_task_state.py`
  - `tests/test_task_store.py`
  - `tests/test_revision_store.py`
  - `tests/test_candidate_revision.py`
  - `tests/test_task_service.py`
  - `tests/test_task_api.py`
  - `tests/test_task_kernel_integration.py`
  - `docs/orchestrated/vibecad-agent-stage3.md`
- 不修改 workflow contract/program/lease/store implementation、validation receipt model、registry、
  server、manifest、runtime、engine 或 legacy Session。TaskRunStore 继续是 task+draft decision 的唯一
  generation-CAS authority；不得新增独立 mutable DraftStore 造成双写 split-brain。需要写出清单
  立即 breaker，不自行扩大范围。

### 3. Exact durable domain contract

新增 closed enum `ReviewPolicy = auto_commit | require_review`。Public `create_task` 必须显式提供
`review_policy`；TaskService/new TaskRun 也必须收到 exact enum，创建后不可在 submit/resume 中改变。
是否自动改变 HEAD 是调用语义，不允许隐藏默认值。

TaskRun 新增 immutable `review_policy` 与 `draft: ReviewDraft | null`。`ReviewDraft` 是 frozen、
schema-v1、path-free durable value；它本身不保存 mutable decision，exact fields 为：

| Field | Binding |
|---|---|
| `id` | `draft_<32hex>`，由 `revision_<32hex>` 同 suffix 一一派生 |
| `task_id`, `project_id` | owning TaskRun identity |
| `base_revision`, `base_generation`, `base_manifest_sha256` | 可精确重建 draft base `ProjectHead` 的三元组 |
| `revision_id`, `manifest_sha256` | immutable sealed draft revision |
| `verification_id`, `acceptance_id`, `observation_digest` | exact passing verifier report identity |

Draft 必须与 TaskRun candidate、program acceptance、passing report、revision manifest、task/project
和 base revision 精确互绑。Immutable CAD bytes 继续只由 RevisionStore 保存；TaskRun 内 draft ref 与
decision status 由同一次 TaskRunStore generation CAS 保存。严禁序列化 compiled acceptance、snapshot、
receipt、lease、Session、binding 或 candidate handle。

TaskStatus 精确增加 `preparing_review`、`awaiting_user_review`、`accepting_draft` 和 terminal
`rejected`；TaskEvent 精确增加 `prepare_review`、`publish_draft`、`accept_draft`、`reject_draft`、
`abort_accept`、`confirm_draft_uncommitted`；NextAction 精确增加 `review_draft`。Transitions：

```text
VERIFYING --prepare_review(pass report + ReviewDraft)--> PREPARING_REVIEW
PREPARING_REVIEW --publish_draft--> AWAITING_USER_REVIEW
AWAITING_USER_REVIEW --accept_draft--> ACCEPTING_DRAFT
AWAITING_USER_REVIEW --reject_draft--> REJECTED
ACCEPTING_DRAFT --commit/confirm_committed--> SUCCEEDED
ACCEPTING_DRAFT --abort_accept--> AWAITING_USER_REVIEW
PREPARING_REVIEW|ACCEPTING_DRAFT --require_recovery/cleanup--> RECOVERY_REQUIRED|CLEANUP_REQUIRED
review-origin RECOVERY_REQUIRED|CLEANUP_REQUIRED --confirm_draft_uncommitted--> AWAITING_USER_REVIEW
```

`preparing_review` 与 `accepting_draft` 的 next action 是 `reconcile`；`awaiting_user_review` 是
`review_draft`；`rejected` 是 `none`。Rejected 是正常用户决定：保留 draft/report/artifact evidence、
`committed_revision=null`、`last_error=null`，不得伪装成 failed。Auto-commit TaskRun 永远无 draft，
继续走原 `PASS_VERIFICATION -> COMMITTING -> SUCCEEDED`。`pass_verification` 事件只允许
auto-commit；`prepare_review` 只允许 require-review。Auto-commit 禁止任何 review event/status/draft；
require-review 禁止从 VERIFYING 进入原 COMMITTING，且一旦 `prepare_review` 出现，draft identity
必须永久存在且不得改变。Review status、rejected 和 reviewed success 都必须有 matching draft + passing
report provenance；`confirm_draft_uncommitted` 只允许 transition history 能证明 attention 源自
preparing/accepting review，且 exact HEAD 仍是 draft base、journal 已 terminal not-committed。进入
recovery/cleanup 必须持久化 fixed `last_error`；回到 awaiting 时清除它。未知 HEAD/journal/ancestry
保持 recovery-required，不允许用普通 publish/abort event 绕过。

### 4. Revision/candidate transaction contract

Require-review 在原 lease 内按固定顺序执行：sealed evidence verification passes → TaskRun CAS to
`preparing_review` with exact report+draft → coordinator consumes/validates the one-shot receipt and settles
the old `PREPARED` journal as terminal `NOT_COMMITTED` without deleting the sealed Revision → closes only
the candidate Session and keeps baseline/HEAD unchanged → returns durable `preparing_review` to the outer
service scope → service successfully releases project lease → only then TaskRun CAS to
`awaiting_user_review`。Awaiting 必须意味着旧 active commit transaction 已 terminal 且本次 service 已
成功释放 lease；release failure leaves durable preparing and returns `lease_unavailable`，不得先发布
awaiting。若 detach、release 或 final task CAS 不确定，durable `preparing_review` 由 resume/reconcile
收敛；restart 后失效的旧进程 lease 不被当作持久状态，仍须在新的 reconcile lease 释放成功后才 publish。

RevisionStore 新增一个窄 primitive，用新 transaction id 把 existing immutable revision 重新准备为
commit candidate。它必须在同一 live project lease 下：验证 current HEAD exact equals supplied full
base head；验证现有 journal absent 或与 current HEAD 匹配且 terminal；重新加载 revision 并验证
project/base/manifest/artifact integrity；最后 atomic durable replace 为 matching `PREPARED` journal。
Stale base、nonterminal unrelated journal、revision/manifest mismatch 在任何 journal/HEAD mutation 前
fail closed。现有 `commit_revision` 仍是唯一 HEAD mutation primitive。

CandidateCoordinator 新增 restart-safe reopen path：从 exact durable base head + revision ref，在新 lease
和 current baseline Session 下加载新的 isolated draft Session，先签发 process-local review handle，但
**不写 PREPARED journal**。Accept 随后重新 collect immutable evidence、compile stored acceptance、运行
verifier并取得 fresh one-shot receipt；fresh report 必须 exact 等于 TaskRun 中持久报告。只有这些检查
全部通过，coordinator 才调用上述 prepare primitive，把 handle 推进为 commit-capable sealed handle，再
调用现有 commit。Persisted report 只能作为审计/binding truth，不能自充跨进程 commit authority。

Pre-prepare 的 reopen/evidence/compile/verify/report-equality 失败必须关闭 isolated draft Session、保持
journal 与 HEAD 不变，并把 accepting task 以 fixed review-integrity error 推进 recovery-required；它不
自动重试。PREPARED 写入后到 HEAD 前的任何失败必须立即 reconcile：若 exact old base + terminal
NOT_COMMITTED 可证明，则 `abort_accept`（或 attention 后 `confirm_draft_uncommitted`）回到 awaiting；
若 exact draft HEAD 可证明则 succeeded；cleanup ambiguity 进入 cleanup-required；其他情况进入
recovery-required。任何路径都不能留下 accepting + unknown nonterminal journal 后返回 public success。

### 5. Exact service/API behavior

S3-4 的 transport-neutral边界窄幅 supersede method-count，不推翻 envelope、budget、error、import 或
capability 约束。TaskApi 的 exact seven methods 为 create/get/submit/resume/capabilities/accept/reject；
TaskServicePort 的 exact seven methods 为 create/get/submit/continue/reconcile/accept/reject，其中 create
接收 exact `ReviewPolicy`。新增 public methods：

```text
accept_draft({schema_version, task_id, draft_id, expected_generation})
reject_draft({schema_version, task_id, draft_id, expected_generation})
```

两者返回现有 exact task result `{generation,next_action,task_run}` 和 exact four-field envelope。
Create request exact fields 改为 `{schema_version,project_id,review_policy}`；unknown/missing/type/value 和
4096-byte outer budget 保持不变。Draft id 使用 `draft_<32hex>` strict ingress；不新增 public error code，
stale base/decision race/generation race 使用现有 `conflict`，wrong state 使用 `invalid_state`，unexpected
port/result/exception 仍只映射 fixed `internal_error`。

Accept fixed order：load exact task/draft → same-decision terminal replay check → require exact expected
generation while awaiting → acquire project lease → load current HEAD and compare the entire persisted base
triple before any task/journal mutation → CAS task to `accepting_draft` → reopen/reobserve/reverify/prepare/
commit → CAS succeeded → release lease。Stale base keeps TaskRun generation/status、journal and HEAD entirely
unchanged，仍可 Reject/inspect。Reject does not acquire project lease or touch RevisionStore/HEAD；it only CASes
awaiting task to rejected。

Idempotency key is semantic `(task_id,draft_id,decision)`。Request 先完成 outer type/range 校验，再按下表
固定路由；一旦 task 存在 draft，mismatched `draft_id` 一律 `conflict`，不存在 draft 则一律
`invalid_state`：

| Durable state（matching draft） | Accept | Reject | expected_generation |
|---|---|---|---|
| `awaiting_user_review` | execute Accept | execute Reject | 必须 exact current，否则 `conflict` |
| `accepting_draft` | same-decision replay → one reconcile, never recommit | `conflict` | replay 忽略 stale generation |
| reviewed `succeeded` | read-only same-decision success | `conflict` | terminal replay 忽略 stale generation |
| `rejected` | `conflict` | read-only same-decision success | terminal replay 忽略 stale generation |
| `preparing_review` | `invalid_state` | `invalid_state` | 不执行 decision |
| any other lifecycle | `invalid_state` | `invalid_state` | 不执行 decision |

Accept 的 public success 必须是 terminal `succeeded`；若 one reconcile 仍不能证明 exact committed draft，
service 返回 fixed recovery/conflict failure，而不是把 in-progress 状态伪装为成功。Same-decision terminal
replay 不增加 generation；opposite decision 永远 `conflict`。Every HEAD mutation is at-most-once；no semantic
CAD execution or model retry is introduced。

API 对 conforming port 的 semantic postcondition 也固定：create 必须返回 generation zero、matching
project、external-plan owner、needs-plan status 和 requested review policy；Accept success 必须保留 matching
draft、status succeeded、`committed_revision == draft.revision_id` 和 require-review policy；Reject success
必须保留 matching draft、status rejected、null committed revision 和 require-review policy。任意 exact
StoredTaskRun 虽结构合法但不满足对应 postcondition，一律 fixed `internal_error`，不得返回 `ok:true`。

`resume_task` routes `preparing_review` and `accepting_draft` to internal reconcile；it returns rejected as a
terminal read and refuses to choose for `awaiting_user_review` with `invalid_state`。Its existing one-read plus
at-most-one service dispatch bound remains。Capability projection remains exactly the six operation entries；
S3-5 does not claim runtime, MCP registration or artifact delivery。

### 6. Recovery matrix and objective gates

Recovery is decided only from task generation/status、full draft base、revision ancestry/manifest、HEAD and
journal truth：

| Crash/replay point | Required convergence |
|---|---|
| task is preparing, old HEAD + PREPARED/NOT_COMMITTED sealed draft | settle terminal journal, preserve revision, publish awaiting exactly once |
| task is awaiting after process restart | exact draft/report/artifacts reload；no lease retained；Accept/Reject callable |
| Accept CAS persisted, HEAD still base | reconcile transaction and continue the already-authorized accept only after fresh exact reverify |
| HEAD equals exact draft but final task response/CAS lost | prove revision/report/ancestry, mark succeeded without second HEAD commit |
| HEAD differs from both exact base and exact draft | no commit；recovery-required/conflict, never infer success |
| Reject response lost / repeated | same rejected task returned read-only, HEAD unchanged |
| two drafts share a base and one commits | second Accept is stale-base conflict with no mutation；second Reject still works |

Genuine RED waves must prove: state schema/status/policy/draft invariants absent；revision re-prepare and coordinator
reopen absent；service auto/review split and durable detach absent；public create policy + Accept/Reject/replay
absent。Setup/import/syntax failure does not count RED。Implementation then must pass：

1. focused state/store/revision/candidate/service/API suite，including N/N+1 and malformed mapping coverage；
2. real restart with a new TaskRunStore/RevisionStore/lease manager/coordinator/executor composition at
   `awaiting_user_review`，then Accept and Reject branches；
3. auto-commit regression、lease release/reacquire、stale full-HEAD、same/opposite duplicate decision、task CAS
   race、prepare/HEAD/final-task durability-uncertain and unknown lineage fail-closed tests；
4. full pytest、full Ruff、`git diff --check`、pycompile、fresh Application import forbidden-module gate；
5. managed FreeCAD Task Kernel integration rerun with the installed environment and supported
   `FREECAD_USER_HOME=/Users/wangtao` override；at least two independent final read-only reviews。

### 7. Execution discipline, delivery and breakers

- Capability profile、live capability declarations、host identity、public configuration and no-background-
  session fabrication rules remain CP-S3-20260720 / Packet S3-4A。Sub-agents may own disjoint allowlisted files
  after this packet is independently reviewed；no two writers may edit the same production file concurrently。
- Control docs are independently reviewed and committed before RED/production mutation。Unexpected baseline
  failure、out-of-allowlist write、awaiting with nonterminal journal、stale HEAD mutation、serialized receipt/
  Session/lease、draft double-store、hidden policy default、Accept without fresh exact verification、Reject
  touching HEAD、second HEAD commit、MCP/FreeCAD import through public API or need to change S3-D01..D08 are
  immediate breakers。
- Control commit: `docs(orchestration): issue S3-5 durable review packet`。Prewritten local semantic commit:
  `feat(workflow): add durable draft review`。Only named-file staging/local commits are authorized；push remains
  `not authorized` / S3-RES-01；no PR or release。
- Completion evidence must append exact file list、RED/GREEN/full/Ruff/import/managed numbers、crash/replay
  matrix、independent reviews、commit hash/push state and four-part recovery snapshot。S3-5 closes only durable
  review residuals；runtime/project bootstrap/artifact/MCP/Workbench/second-host remain later stages。

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E09 / 2026-07-21T07:29:18Z | S3-A01；three-way dependency audit；two independent packet reviews PASS after closing state/recovery/API findings | 1c792aa / not authorized | clean anchor；focused baseline 1180；journal/receipt audit；4 state Important + 2 API Important + 2 Minor closed；diff check PASS | S3-RES-01..06, S3-RES-08..09；durable review implementation pending | S3-S09 | packet-issued |

### Recovery snapshot S3-S09

1. **Completed:** S3-4 semantic commit `1c792aa`；S3-5A freezes explicit review policy、immutable
   task-owned draft ref、review state machine、detach/re-prepare transaction、release-before-awaiting、fresh
   verification、Accept/Reject replay and exact public postconditions；two independent packet reviews PASS。
2. **Next:** commit this docs-only control packet；from the clean control anchor create genuine disjoint RED
   waves for state/store、revision/candidate、service/restart and TaskApi；implement only the S3-5 allowlist；
   run focused/full/import/managed gates and two final read-only reviews；then local semantic commit。
3. **Approved decisions:** S3-D01 through S3-D08 under S3-A01；explicit no-default policy and seven-method
   Application contract are consequences of S3-D05, not new product scope；no repeated user approval needed；
   push/PR/release remain unauthorized。
4. **Recovery:** verify branch `codex/agent-stage3`、HEAD `1c792aa` or the subsequent S3-5 control commit、
   only this document modified before control commit and no active test process；do not preserve a PREPARED
   journal as draft or serialize process capabilities；if semantic commit exists, use its completion evidence
   instead of replaying RED。

### S3-5 completion evidence

Actual write set stayed inside Packet S3-5A:

- production: `src/vibecad/application/task_api.py`、`src/vibecad/execution/candidate.py`、
  `src/vibecad/execution/revisions.py`、`src/vibecad/workflow/service.py`、
  `src/vibecad/workflow/state.py`；
- focused and managed gates: `tests/test_candidate_revision.py`、`tests/test_revision_store.py`、
  `tests/test_task_api.py`、`tests/test_task_kernel_integration.py`、`tests/test_task_service.py`、
  `tests/test_task_state.py`、`tests/test_task_store.py`；
- orchestration record: `docs/orchestrated/vibecad-agent-stage3.md`。

Evidence chain:

1. Four genuine RED waves failed for their intended missing contracts: state/store `4`、
   revision/candidate `13`、Task API `10` and service `1`; none was an import, syntax or setup failure.
2. The implementation adds an explicit no-default `auto_commit | require_review` policy, an immutable
   task-owned `ReviewDraft`, review-aware persisted state transitions and exact seven-method service/API
   boundaries. A reviewed success requires durable `accept_draft` provenance; forged direct and persisted
   round trips fail closed.
3. Awaiting review is now durably detached from the original transaction. Accept reacquires a valid local
   lease, reopens an isolated immutable revision, recollects evidence, recompiles stored acceptance, obtains
   a fresh one-shot receipt, prepares a fresh transaction and commits only under the exact full base HEAD.
   Lease authority is validated before loading CAD or consuming the receipt.
4. Durability-uncertain directory fsync、project-fd close、root-fd close、review publish、Accept finalization
   and Reject finalization paths converge from journal/HEAD/task truth. A failed re-prepare remains
   HEAD-neutral and settles as terminal `NOT_COMMITTED`; coordinator/session handles close exactly once.
5. Final adversarial review findings were closed with regressions: spoofed detach results cannot publish;
   missing/CLEAN/wrong candidate or manifest journals cannot publish `preparing_review`; a single later
   project journal cannot strand another detached draft; missing Accept provenance cannot forge success;
   a foreign lease cannot touch CAD/store or burn a receipt; and an Accept response lost before a later
   legitimate descendant commit proves ancestry and succeeds without a second commit. Sibling/divergent
   ancestry remains recovery-required.
6. A real managed-FreeCAD two-process gate creates two same-base drafts in process one, reconstructs all
   stores/lease/coordinator/executor/service objects from disk in process two, accepts draft A, proves draft
   B Accept is stale and mutation-free, and still rejects B. Result: `8 passed in 10.97s`.
7. Final focused cumulative gate: `1447 passed`; revision/candidate focused subset: `397 passed`; service
   subset: `92 passed`. Final full gate: `2855 passed, 90 deselected` with the two pre-existing macOS
   `fork()` deprecation warnings. Full Ruff、`git diff --check`、pycompile and fresh-Application import gate
   (`1 passed`) all passed.
8. Two fresh, independent, non-writing reviewers each returned PASS after the response-loss/descendant
   recovery correction. Both confirmed stale/sibling fail-closed behavior, exact durable acceptance
   provenance, ancestry/report/manifest proof and no recommit path; neither found a remaining Critical or
   Important issue.

Crash/replay convergence:

| Durable observation | Convergence and mutation authority |
|---|---|
| preparing + exact own terminal `NOT_COMMITTED` journal | release lease, publish awaiting exactly once |
| preparing + missing/CLEAN/mismatched journal | recovery-required; never publish a draft claim |
| accepting + exact base + absent/unrelated/prior-committed terminal transaction | fresh reopen/reverify/re-prepare; no stale capability reuse |
| recovery/cleanup + base + any matching terminal `NOT_COMMITTED` journal | return to awaiting; a single later project journal cannot strand another detached draft |
| exact draft or legitimate descendant HEAD after Accept response loss | prove revision ancestry/manifest/passing report; mark succeeded without recommit |
| stale full base, sibling or divergent HEAD | conflict/recovery; task/journal/HEAD remain mutation-free |
| repeated Reject or same terminal decision | return the same terminal task read-only; opposite decision conflicts |
| re-prepare fsync/fd-close uncertainty | settle terminal `NOT_COMMITTED`; preserve exact base HEAD |
| foreign or forged project lease | reject before CAD load, revision mutation or receipt consumption |

S3-5 closes the durable draft/review slice and its restart/lease/CAS obligations. S3-RES-01 through
S3-RES-06 and S3-RES-08 through S3-RES-09 remain assigned to later local-runtime、artifact/MCP、second-host、
profile and external-publication stages; S3-5 does not claim any of those deliveries.

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E10 / 2026-07-21T08:35:10Z | S3-A01；S3-5A；two independent final reviews PASS after all Important findings were closed | S3-5 semantic commit containing this snapshot / not authorized | RED 4/13/10/1；focused 1447；full 2855/90；managed 8；Ruff/diff/pycompile/import PASS | S3-RES-01..06, S3-RES-08..09；durable review slice closed | S3-S10 | completed |

### Recovery snapshot S3-S10

1. **Completed:** S3-5 delivers explicit review policy, immutable durable drafts, release-before-awaiting,
   restart-safe fresh verification, exact full-HEAD/ancestry recovery, mutation-free stale/reject behavior
   and real two-process managed-FreeCAD double-draft acceptance; all objective gates and two independent
   final reviews passed。
2. **Next:** issue Packet S3-6A for isolated `AgentApplication` composition、`CadExecutionPort`、revision-zero
   and import bootstrap、lazy per-project runtime、durable data root、uninstall-preserves-data、managed
   checkout and IPC G0; do not expose public MCP or implement G1 Workbench in S3-6。
3. **Approved decisions:** S3-D01 through S3-D08 under S3-A01 and the standing continuous-execution
   authorization; push、PR、release、marketplace、external spend and G1 Workbench remain unauthorized。
4. **Recovery:** verify branch `codex/agent-stage3`、the local S3-5 semantic commit subject
   `feat(workflow): add durable draft review`、a clean worktree and S3-E10 evidence; do not replay S3-5.
   Continue by auditing S3-6 dependencies and issuing its independently reviewed docs-only control packet。

## 8.9 Packet S3-6A — isolated AgentApplication/runtime/bootstrap and Workbench G0

### 1. Authorization and converged dependency audit

- Approval ID: S3-A01；artifact revision: S3-R3 / S3-R3.1 / S3-R3.2；bound decisions:
  S3-D01 through S3-D08；starting anchor: `4c7347a` on `codex/agent-stage3`。
- 本 packet 只执行 active S3-6：runtime/data split、bounded legacy-runtime adoption、revision-zero
  and normalized FCStd bootstrap、store-only task catalog、lazy per-project `AgentApplication`、nominal
  trusted `CadExecutionPort`、same-process global CAD gate、bounded managed checkout and non-runnable IPC
  G0 contract。它不获得 public MCP/server registration、manifest cutover、verified external artifact
  delivery、runnable daemon/socket/pipe、authentication、Workbench UI、selection/highlight、checkout
  publish/manual revise、arbitrary Python、push、PR、release 或外部花费权限。
- 三路独立只读审计从 Application composition、runtime/data/uninstall 和 IPC/Workbench seam 方向一致
  确认现有 Task/Revision/Candidate/Verifier 可复用，但五个边界必须先关闭：一个
  `CandidateCoordinator/SessionSlot` 只能绑定一个 project；当前 `TaskService` 尚无 concrete public-port
  bridge；`import_trusted_fcstd` 在语义验证前会发布 HEAD；project lease 不会串行不同 project 的
  same-process FreeCAD；当前 uninstall 会删除整个 `VIBECAD_HOME`。这些是 S3-D01、D04、D06、D08
  与 active S3-6 的直接工程后果，不改变产品定位或公共信任边界。
- 当前可用 FreeCAD 位于 legacy prefix
  `/Users/wangtao/Library/Application Support/VibeCAD/mamba/envs/vibecad`。Conda/FreeCAD 脚本包含
  absolute prefix，严禁 rename/move。S3-6 只允许精确验证后原位复用；新安装进入新 runtime root，
  不下载第二套已存在且健康的 FreeCAD。

### 2. Workspace anchor and mechanical allowlist

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`；branch:
  `codex/agent-stage3`；anchor `4c7347a`；issue-time worktree clean。Anchor full gate is
  `2855 passed, 90 deselected`；managed FreeCAD is `8 passed`。
- S3-6 control commit 只允许修改本文件。S3-6 semantic implementation 只允许修改或新增：
  - `src/vibecad/runtime/paths.py`
  - `src/vibecad/runtime/installer.py`
  - `src/vibecad/runtime/status.py`
  - `src/vibecad/runtime/uninstall.py`
  - `src/vibecad/server.py`，仅同步既有 uninstall preview 的 preserve-data 事实
  - `src/vibecad/workflow/catalog.py`（new）
  - `src/vibecad/workflow/service.py`
  - `src/vibecad/workflow/program.py`
  - `src/vibecad/execution/adapter.py`
  - `src/vibecad/execution/candidate.py`
  - `src/vibecad/execution/executor.py`
  - `src/vibecad/execution/revisions.py`，仅增加 evidence-bound revision-zero import primitive
  - `src/vibecad/application/__init__.py`
  - `src/vibecad/application/task_api.py`，仅增加 closed `resource_exhausted` port/API failure
  - `src/vibecad/application/data.py`（new）
  - `src/vibecad/application/project.py`（new）
  - `src/vibecad/application/agent.py`（new）
  - `src/vibecad/interaction/__init__.py`（new）
  - `src/vibecad/interaction/cad.py`（new）
  - `src/vibecad/interaction/storage.py`（new）
  - `src/vibecad/interaction/checkouts.py`（new）
  - `src/vibecad/interaction/protocol.py`（new）
  - `tests/test_paths.py`
  - `tests/test_installer.py`
  - `tests/test_status.py`
  - `tests/test_uninstall.py`
  - `tests/test_launcher_uninstall_integration.py`
  - `tests/test_server_round11.py`
  - `tests/test_runtime_purity.py`
  - `tests/test_task_catalog.py`（new）
  - `tests/test_task_service.py`
  - `tests/test_model_program.py`
  - `tests/test_execution_adapter.py`
  - `tests/test_program_executor.py`
  - `tests/test_candidate_revision.py`
  - `tests/test_revision_store.py`
  - `tests/test_agent_application.py`（new）
  - `tests/test_task_api.py`
  - `tests/test_project_bootstrap.py`（new）
  - `tests/test_cad_execution_port.py`（new）
  - `tests/test_interaction_protocol.py`（new）
  - `tests/test_managed_checkout.py`（new）
  - `tests/test_task_kernel_integration.py`
  - `docs/orchestrated/vibecad-agent-stage3.md`
- 不修改 `manifest.json`、MCP registration、launcher/supervisor lifecycle、legacy CAD tool public surface、
  Revision/Task durable schemas、selector level、acceptance/verifier semantics 或 G1 code。RevisionStore
  只允许把现有 private project-initialization primitive 收紧为 mandatory sha256/size evidence-bound copy；
  不新增 public import authority，也不改变 revision record/HEAD schema。

### 3. Runtime/data and legacy-adoption contract

Fixed layout：

```text
VIBECAD_HOME/
├── runtime/                       # replaceable/uninstallable
│   ├── bin/micromamba
│   ├── mamba/envs/vibecad
│   ├── status.json
│   ├── install.log
│   ├── .install.lock
│   └── external-runtime.json      # optional receipt; never written into override env
├── data/                          # durable; runtime actions are zero-write here
│   ├── locks/
│   ├── tasks/
│   ├── projects/
│   ├── bootstrap/
│   └── checkouts/
├── .uninstall_requested          # outside runtime so partial cleanup can retry
└── views/                         # legacy user output preserved in S3-6
```

`runtime_root()`、`data_root()`、`lease_root()`、`task_store_root()`、`revision_store_root()`、
`bootstrap_root()` and `checkout_root()` are fixed children of the existing `vibecad_home()`；S3-6 does
not add a second environment-controlled data deletion boundary。Final store roots must be absolute、current
uid、0700、non-symlink and opened no-follow；records/FCStd are 0600 ordinary single-link files。Existing
unsafe roots are rejected, never silently chmod-repaired。

New managed installs use `runtime/mamba/envs/vibecad`。If new runtime is absent and exact legacy
`home/mamba/envs/vibecad` exists, the installer may adopt it in place only when all of these hold：canonical
prefix is the fixed non-symlink child of a safe root；the in-env receipt is canonical JSON with
`runtime_kind:managed`、matching Python/FreeCAD pins and a bounded VibeCAD version；and subprocess verification
matches that receipt。Exact healthy is reused；same owned engine with only server version stale may receive
pip-only sync。A legacy tree with missing/malformed/unowned receipt is preserved untouched while a new runtime
is created；an invalid legacy tree may be removed only when the valid managed receipt plus safe root identity
prove ownership。Path/name、`VibeCAD` basename、`mamba/` presence、status/log/lock alone never prove ownership。
No legacy environment is moved or renamed。

The installed legacy prefix named in §1 currently carries a valid `runtime_kind:external` receipt。That exact
case is selected as read-only external reuse when the new runtime and explicit override are absent：subprocess
verification must pass，then `runtime/external-runtime.json` binds its canonical prefix/device/inode and exact
engine/server evidence。It is never pip-synced、deleted or rewritten。If it becomes unhealthy it is preserved
and a new managed runtime may be installed。A test targets the actual canonical legacy-prefix contract with an
external-kind receipt and makes every download/create/pip command fail if called，proving selection without a
second engine install。

Explicit `VIBECAD_FREECAD_ENV` is external and is never deleted or rewritten。After exact subprocess
verification, its receipt is atomically stored at `runtime/external-runtime.json` and binds the canonical
override prefix、prefix device/inode、Python version、FreeCAD version and VibeCAD server version。Status accepts
it only while all identity fields still match。Install/status/uninstall tests hash and stat the entire override
tree before/after and require identical entries、inodes and bytes；the external receipt is the only permitted
write and remains below replaceable `runtime/`。

Direct、pending and preview uninstall operate on fixed runtime targets only：new `runtime/` unconditionally
after root-safety checks；legacy env/micromamba only with the exact managed-receipt + safe-root ownership proof
above；legacy status/log/lock only when their own bounded record or exact fixed-file identity proves they were
managed。Unowned/ambiguous legacy and every unknown file are preserved and reported, never guessed away。They
preserve `data/` and `views/` byte for byte；marker clears only when every authorized runtime target is gone，
while preserved ambiguous legacy is not treated as an authorized target。Symlink、parent replacement、
runtime/data alias or partial authorized removal fails closed/retries。Preview reports authorized runtime
size/path、preserved ambiguous paths and `data_preserved:true` without counting data bytes。Installer repair、
status、swap and uninstall cause zero writes below `data/` except Application initialization explicitly creating
the fixed data layout。

### 4. Store-only catalog, bootstrap and AgentApplication contract

`TaskCatalogService` is the sole no-CAD implementation of `create_task`、`get_task` and `reject_draft`，
extracted from the current TaskService behavior rather than copied. `TaskService` delegates those methods and
retains the CAD lifecycle methods。Create atomically reads and validates the full current `ProjectHead` and
persists only its immutable `revision_id` as the existing TaskRun `base_revision`；it does not claim that the
Task durable schema stores generation/manifest。Every CAD execution later rereads the full HEAD under its own
write lease and first validates it against the runtime's exact full baseline；method-specific lineage rules then
apply。Only new-candidate submit/continue require `HEAD.revision_id == Task.base_revision`；initial Accept requires
the exact draft base HEAD；reconcile and ACCEPTING_DRAFT response-loss replay retain the existing durable
journal/revision-ancestry rules and must not require current HEAD to equal the older Task base。Reject remains one
TaskRun generation CAS with no project lease、RevisionStore mutation or CAD load。A concrete bridge maps only
closed Catalog/TaskService errors into S3-4 `TaskServicePortFailure`; unknown exceptions remain fixed
`internal_error` at TaskApi。

`AgentApplication` implements the exact seven-method `TaskServicePort` and owns one shared trusted
`ResourceLeaseManager`、`TaskRunStore`、`LocalRevisionStore` and one process-wide CAD gate。Its constructor,
capability read、task get/create/reject and empty-project bootstrap do not import FreeCAD or create a Session。
`submit/continue/reconcile/accept` first load durable TaskRun to derive project_id, never trust a caller-supplied
route, then enter the global gate and resolve a lazy project runtime：

```text
project_id -> full durable ProjectHead
           + isolated baseline SessionBinding/SessionSlot
           + project-only CandidateCoordinator
           + TaskService using the same exact CadExecutionPort
```

Every project has distinct Session/Slot/Coordinator。`MAX_PROJECT_RUNTIMES = 4`；a deterministic LRU may evict
only an idle、clean runtime after closing it，and request 5 with four non-evictable runtimes returns fixed
`resource_exhausted` without opening another document。S3-6 adds exactly that value to the closed
`TaskServicePortErrorCode`/`TaskApiErrorCode` mapping with a fixed non-reflective message；no other TaskApi shape
changes。N=4/N+1=5 tests are mandatory。

The non-reentrant lease ordering is frozen and never nests：process-wide CAD gate → acquire a short project
lease → reread full HEAD and build/refresh a provisional runtime bound to that exact triple → release → invoke
its TaskService → TaskService reacquires its own project lease and, before any CAD handler、candidate directory
or journal side effect, rereads the full HEAD and compares exact generation/revision/manifest to the provisional
runtime head。It then applies the method-specific base/ancestry rules above，so accepted-draft/descendant
reconciliation remains recoverable。A different-process commit in the release/reacquire gap therefore returns
the existing pre-candidate conflict before a fresh candidate，or enters the existing journal/ancestry recovery
for an already-published transaction；it never runs against the stale Session。Application evicts the stale
provisional runtime before the next call and explicitly closes any baseline Session already opened during
provisional build。No code path calls TaskService while Application still owns the short lease，and no
"seamless refresh" is claimed。Tests force a full-HEAD advance in that exact gap for both a fresh candidate and
ACCEPTING_DRAFT response-loss replay，proving no CAD **handler**、candidate、journal、revision or HEAD mutation，
proving stale baseline close/eviction，and proving correct committed-lineage recovery after rebuild。The test
does not falsely claim that provisional Session load/create never occurred。

Application captures creator pid、serializes all same-process CAD calls across projects、closes owned sessions
best effort without changing HEAD and rejects fork-inherited process capabilities。No module-global
AgentApplication or Session is created。S3-6 only claims macOS AgentApplication execution；on Windows/Linux the
application capability is statically `unsupported_platform` and construction fails before creating data or CAD
state until S3-RES-02 closes。Pure protocol/value modules remain importable there。

Project bootstrap has two distinct typed entry points and server-generated `project_<32hex>` ids：

1. empty: acquire project lease → `initialize_empty_project` → exact generation-zero HEAD、base null、model
   null；durability uncertainty succeeds only after exact readback；no FreeCAD import；
2. FCStd import: no-follow bounded copy of external ordinary single-link source into private
   `data/bootstrap` → load/recompute through trusted CadExecutionPort → reject empty/unsupported/malformed/
   partial/duplicate identities → preserve complete valid identities and attach imported object/feature UUIDs
   only to otherwise-untagged `Part::Box`/`Part::Cylinder` → checkpoint normalized internal FCStd → reload and
   validate exact identities/geometry → bind staged sha256/size → call an evidence-required RevisionStore
   import primitive with that exact pair。The store reopens no-follow、copies into a private temporary project
   while hashing/counting the copied bytes、checks source before/after identity and requires copied sha256/size
   to equal the supplied evidence before the project rename/HEAD publication。A swap or mutation after semantic
   validation therefore fails without a visible project；byte-identical replacement is harmless。Only then may
   generation-zero publication occur → exact readback → close and remove staging。

Any failure before the atomic project rename leaves no visible project/HEAD and closes or records only private
staging cleanup。After generation-zero publication, exact HEAD/revision/model readback makes the bootstrap a
semantic success that is never rolled back：a later Session-close or staging-delete failure returns
`cleanup_required:true` with the successful project descriptor and leaves a bounded durable cleanup record under
`data/bootstrap` for idempotent retry。Response-loss success is accepted only when generation-zero
revision/model digest/size exactly match the bootstrap evidence。A RED replaces staging after CadExecutionPort
validation but before the store copy and must observe no project directory/HEAD；separate crash tests after
publication prove retained success plus cleanup convergence。S3-6 import is not public MCP ingress and does not
claim arbitrary legacy FreeCAD normalization。

### 5. CadExecutionPort, budgets, checkout and IPC G0

`CadExecutionPort` is a nominal trusted local Python capability, not a wire/duck-typed model input。It extends
the existing four-method `CadSnapshotPort` with fixed profile/capability、import validation、program validation、
execution、STEP export and sealed evidence collection。`CandidateEvidence` moves to this neutral contract and
is re-exported for compatibility。Application alone chooses the implementation；TaskService and
CandidateCoordinator prove they share the exact same port instance。Current `InProcessCadExecutor` reports only
headless verified；offscreen_gui and interactive_gui are planned/unavailable；no silent profile downgrade。

`BoundCommand` and adapter snapshots must retain exact `resource_budget`、minimum/maximum FreeCAD version and
`requires_gui_main_thread` metadata from the authentic registry。Before a handler, the port rejects unsupported
profile/version/thread and budgets exceeding the named admission ceiling：
`MAX_ADMITTED_RUNTIME_MS = 30_000`、`MAX_ADMITTED_CREATED_OBJECTS = 1`、
`MAX_ADMITTED_RESULT_BYTES = 262_144`。N and N+1 metadata tests exist for every ceiling。After each synchronous
handler it measures monotonic elapsed milliseconds、created-object delta and canonical normalized-result UTF-8
bytes before the next command/checkpoint/commit；the authentic command budget still applies when lower than the
ceiling。Any excess fails and rolls back the candidate。Elapsed enforcement is explicitly post-return：
same-process FreeCAD cannot safely interrupt a stuck C++ call, so S3-RES-05 crash/hang containment remains open。
Passing this closes S3-RES-08；S3-RES-09 remains open for real GUI E2E。

Managed checkout is a durable state machine under `data/checkouts` with
`MAX_CHECKOUT_FILE_BYTES = 536_870_912`、`MAX_CHECKOUT_TOTAL_BYTES = 2_147_483_648` and
`MAX_OPEN_CHECKOUTS = 8`。There is no automatic OPEN TTL：budget pressure returns fixed
`resource_exhausted` and never evicts user edits。Only unpublished temp/orphan entries older than
`ABANDONED_TEMP_TTL_SECONDS = 86_400` may be cleaned on restart；CLOSED tombstones are retained for
`CLOSED_TOMBSTONE_TTL_SECONDS = 2_592_000`；`MAX_CHECKOUT_TEMP_ENTRIES = 8` and
`MAX_CLOSED_TOMBSTONES = 1_024` bound them separately from OPEN entries。Total bytes include every OPEN/temp
model below the store；admission reserves the incoming copy before writing。N/N+1 file、total、OPEN、temp and
tombstone tests are mandatory。

One fixed non-reentrant `CheckoutMutationLock` at `data/locks/checkout-store.lock` combines an in-process mutex
with an OS-released cross-process exclusive lock and the same no-follow/root-identity checks as other stores。
It covers expired-record cleanup、open-key lookup、source resolution、all count/byte checks and reservations、
file/metadata publication、get-vs-close record reads、CLOSED tombstone publication and checkout deletion。
Every new OPEN reserves one future tombstone slot by requiring
`closed_tombstones + open_checkouts < MAX_CLOSED_TOMBSTONES` after expired cleanup；capacity may reject a new
open but can never prevent an existing OPEN from closing。Two-process same-key and different-key N/N+1 tests
prove single descriptor publication and no count/byte over-admission；process death releases only the lock，not
durable OPEN authority。

G0 source is an exact full committed HEAD or exact TaskRun generation + review draft binding。Caller supplies
`checkout_open_<32hex>` as a non-path idempotency key but no filesystem path；checkout id remains server-minted
`checkout_<32hex>`。The OPEN record separately binds canonical request intent and exact resolved source。Open
always looks up the key first：same key + byte-equivalent canonical intent replays the persisted descriptor
without resolving current HEAD again，even if HEAD advanced after the lost response，but it must run the exact
same current checkout metadata/confinement/no-follow/ordinary-file/link-count/size/rehash validation as Get
before returning any descriptor or trusted local path。A safe atomic edit therefore replays an updated
`dirty:true` descriptor；symlink/hardlink/escape/tamper returns `integrity_failure`。Same key + different intent
is conflict。A new key resolves and binds current exact source。Open copies the immutable
revision model through no-follow ordinary-file checks into a new 0700 checkout/0600 `model.FCStd`, fsyncs file
and directory、atomically publishes metadata，then reopens/rehashes and proves a different inode/single link。
Descriptor binds checkout/project/revision/manifest/model digest/full source identity and
`authoritative:false`。OPEN survives process restart unchanged。Get revalidates confinement and metadata；a
normal FreeCAD atomic replacement by a new ordinary 0600 single-link file inside the same checkout is accepted
as `dirty:true` after rehash，whereas symlink、hardlink、non-ordinary、oversize or escaped paths are
`integrity_failure`。Close first durably publishes a source-bound CLOSED tombstone and then deletes only that
checkout directory；retry during the tombstone retention window is terminal-idempotent and cannot recreate it。
Crash tests cover copy、file fsync、metadata publish、directory fsync、close tombstone and deletion。Editing or
closing checkout never changes TaskRun/journal/revision/HEAD；S3-6 has no publish path and never reuses a draft
verdict。Replay tests include same key after safe edit、symlink and hardlink replacement as well as HEAD advance。

IPC G0 implements only a strict raw-byte codec/value contract；there is no dispatcher、listener、socket/pipe or
authenticated transport。All five future method names are reserved and schema-tested：`initialize`、
`application.call`、`checkout.open`、`checkout.get`、`checkout.close`。Every actual G0 wire dispatch returns
fixed `unavailable` without calling Application or returning a filesystem path；in-process AgentApplication may
return a confined local path in its trusted Python checkout descriptor, but the wire projection never contains
`local_path`。This keeps path delivery and peer authorization a G1 decision。

The raw request is one BOM-free UTF-8 JSON object with exact outer keys
`protocol,version,request_id,method,params`；`protocol` is `vibecad.local`，`version` has exact keys
`major:1,minor:0` and `request_id` is `request_<32hex>`。Success has exact outer keys
`protocol,version,request_id,result,error` with object result and null error；failure has the same keys with null
result and exact error keys `code,message`。Exactly one JSON value is allowed；duplicate keys、trailing bytes、
NaN/Infinity、non-safe integers and unknown fields fail before method decode。Fixed codec budgets are
`MAX_PROTOCOL_REQUEST_BYTES = 589_824`、`MAX_PROTOCOL_RESPONSE_BYTES = 1_048_576`、
`MAX_PROTOCOL_DEPTH = 72`、`MAX_PROTOCOL_NODES = 10_240`、
`MAX_PROTOCOL_STRING_BYTES = 524_288`、`MAX_PROTOCOL_KEY_BYTES = 256` and safe integers
`[-(2**53-1), 2**53-1]`；each has exact N/N+1 and multibyte UTF-8 tests。

Method params/results are frozen as follows；every mapping rejects omitted/extra fields：

| Method | Exact params | Exact result |
|---|---|---|
| `initialize` | `client_name,client_version` bounded printable strings | `kernel_id,session_id,protocol_version,capabilities` |
| `application.call` | `kernel_id,session_id,operation,request`；operation is exactly `create_task,get_task,submit_model_program,resume_task,accept_draft,reject_draft,get_capabilities` | `response` containing the unchanged TaskApi `schema_version,ok,result,error` envelope |
| `checkout.open` | `kernel_id,session_id,open_key,source`；source is exact `kind:head,project_id` or `kind:draft,task_id,draft_id,expected_generation` | path-free checkout descriptor |
| `checkout.get` | `kernel_id,session_id,checkout_id` | path-free checkout descriptor |
| `checkout.close` | `kernel_id,session_id,checkout_id` | path-free CLOSED descriptor |

`client_name` is 1..64 UTF-8 bytes and `client_version` is 1..32，both printable single-line ASCII。The
path-free checkout descriptor has exact keys
`checkout_id,open_key,state,authoritative,dirty,source,initial_model_sha256,current_model_sha256,current_size_bytes`。
Its exact resolved `source` keys are
`kind,project_id,revision_id,manifest_sha256,model_sha256,size_bytes,task_id,draft_id,task_generation`；the last
three are null for committed HEAD and exact durable values for draft。`state` is `open|closed`，
`authoritative` is always false，and a CLOSED descriptor preserves the last hashes/size from its tombstone。

`kernel_<32hex>` and `session_<32hex>` are server-minted non-authoritative correlation values only，not channel
binding、authentication or capability。The initialize capability mapping is exact booleans
`application_dispatch:false,checkout_dispatch:false,authenticated_transport:false,local_path_delivery:false`。
The closed protocol error codes are `malformed_message,unsupported_version,unknown_method,budget_exceeded,
invalid_request,unavailable,internal_error` with fixed non-reflective messages；no exception text/path is copied。
FCStd bytes and Python/process capabilities never enter JSON。Unix socket vs Windows named pipe、peer
credential/ACL/bootstrap secret、daemon discovery and Qt dispatch remain G1 decisions；no JSON `auth_token`
field is frozen。

### 6. Genuine RED, recovery matrix and objective gates

Genuine RED waves must independently hit the missing contracts, never setup/import/syntax failures：

1. runtime/data paths + owned healthy legacy adoption + unowned legacy/external-override preservation +
   direct/pending uninstall byte/inode preservation；
2. store-only catalog and real AgentApplication seven-method bridge while FreeCAD modules are unavailable；
3. empty/import bootstrap, invalid/TOCTOU source, identity normalization and durability readback；
4. CadExecutionPort exact nominal surface, authentic metadata retention, version/thread/admission/post-return
   budgets and honest static profiles；
5. two-project lazy isolation、full-HEAD refresh、global CAD concurrency maximum one、cache bound and shutdown；
6. managed revision/draft checkout copy/replay/restart/confinement/no-link/tamper/edit/source immutability；
7. exact IPC roundtrip/version/budget/session/replay contracts with no server/socket/FreeCAD import。

Recovery requirements：

| Observation | Required convergence |
|---|---|
| runtime uninstall interrupted | marker retained；retry only fixed runtime targets；data unchanged |
| owned managed or exact healthy external-kind legacy, new runtime absent | reuse in place；external-kind is read-only；no rename/pip/delete/second engine install |
| empty/import publication durability uncertain | exact generation-zero HEAD/revision/digest readback；prepublish fixed failure or postpublish success + cleanup_required |
| bootstrap staging swaps after validation | store sha256/size mismatch；no project directory/HEAD；session/staging cleaned or safely recoverable |
| cached runtime HEAD differs in generation/revision/manifest | short lease rebuild；never reuse stale baseline；drain/close/rebuild or recovery failure |
| HEAD advances after refresh lease release | no CAD handler/candidate/durable mutation；close stale provisional Session；evict/rebuild；preserve committed ancestry recovery |
| Application restart with task/draft | rebuild from stores and exact HEAD；no in-memory authority required |
| checkout open response loss + HEAD advance | key-first canonical-intent replay returns original descriptor without re-resolve；different intent conflicts |
| two processes open/close concurrently | one fixed store lock；same key has one descriptor；capacity/tombstone reservation never over-admits or blocks existing close |
| restart/budget with OPEN checkout | preserve OPEN edits；only stale unpublished temp cleanup；return resource exhaustion rather than eviction |
| checkout edited/tampered | source remains immutable；get marks dirty or integrity failure；never commit |
| external override install/status/uninstall | external tree entries/inodes/bytes unchanged；only runtime receipt changes |
| CAD call overlaps another project | process gate keeps maximum active FreeCAD call count at one |

Focused gate includes all changed/new test files plus cumulative task API/service/program/adapter/candidate suites。
Managed FreeCAD must prove empty revision-zero commit、normalized Box/Cylinder import commit、two-project
isolation、draft checkout edit with source/HEAD unchanged、Application close/new-process Accept and exact legacy
runtime reuse without installer invocation。Then run full pytest、full Ruff、`git diff --check`、pycompile、fresh
Application capability/get/reject forbidden-module gates、runtime uninstall data-tree digest gate and at least
two independent final read-only reviews。

### 7. Delivery boundary and breakers

- Control commit: `docs(orchestration): issue S3-6 isolated application packet`。Prewritten local semantic
  commit: `feat(application): compose isolated task project runtime`。Only named-file staging/local commits are
  authorized；push remains `not authorized` / S3-RES-01。
- Immediate breakers：data deletion/write by runtime maintenance；second FreeCAD download despite an exact
  owned healthy legacy prefix；moving legacy env、deleting ambiguous legacy or writing external override；project
  visible before semantic import + store digest validation；nested/reentrant project lease；one global
  SessionSlot across projects；Reject/get/capability loading FreeCAD；missing global CAD gate；path supplied by
  checkout caller or emitted by wire codec；evicting OPEN checkout；checkout/revision hardlink；serialized
  lease/candidate/Session/receipt；checkout mutation without the fixed cross-process lock or future tombstone
  reservation；runtime/cache/protocol budgets without the frozen constants；claiming hard
  timeout、runnable IPC、authentication、interactive profile or public MCP；out-of-allowlist write；need to alter
  S3-D01..D08。
- Ordinary implementation/test defects, schema naming corrections and control-record review findings are
  closed autonomously inside this packet。Completion appends exact files、RED/GREEN/full/managed/import/runtime
  evidence、two reviews、residual disposition and S3-S12 recovery snapshot。

#### S3-6C1 test-only control correction

The first complete S3-6 gate exposed two stale assertions in `tests/test_supervisor.py` that still require
removing the whole `VIBECAD_HOME`.  That expectation predates the approved runtime/data split and conflicts
with this packet's immediate breaker against deleting ambiguous legacy content.  The semantic allowlist is
therefore corrected to include `tests/test_supervisor.py`, strictly limited to setup, names/comments and
assertions in `test_pending_uninstall_runs_before_respawn` and
`test_spawn_real_cmd_uninstall_marker_falls_back_to_bootstrap`.

Those two cases must prove that the pending marker is consumed, every owned fixed runtime target is removed,
the next supervisor generation safely selects bootstrap Python, and ambiguous `mamba/` or malformed legacy
`status.json` bytes remain unchanged.  This correction does not authorize changes to
`src/vibecad/supervisor.py`, supervisor lifecycle, uninstall production semantics or any other test.  It is a
mechanical stale-test correction under the packet's autonomous defect rule, not a change to S3-D01..D08.

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E11 / 2026-07-21T08:50:39Z | S3-A01；three-way S3-6 dependency audit + two independent final packet reviews PASS | 4c7347a / not authorized | clean S3-5 anchor；full 2855/90；managed 8；G0 one-file allowlist + diff check PASS；reviews closed secure-import TOCTOU、lease lineage、runtime ownership、checkout/IPC/budget findings with no remaining Critical/Important | S3-RES-01..06, S3-RES-08..09；S3-6 implementation pending | S3-S11 | control-ready |

### Recovery snapshot S3-S11

1. **Completed:** S3-5 semantic commit `4c7347a`；S3-6 three-way read-only audit converged on runtime/data
   preservation、store-only task control、secure normalized bootstrap、per-project runtimes、global CAD gate、
   trusted port、managed checkout and non-runnable IPC G0；two independent final packet reviews PASS after all
   Critical/Important findings were closed；worktree before this packet was clean。
2. **Next:** commit this docs-only control packet；write the seven genuine RED waves inside the exact allowlist；
   implement runtime/data safety first, then catalog/
   Application/bootstrap/port/checkout/protocol；run focused/full/managed gates and two final reviews。
3. **Approved decisions:** S3-D01 through S3-D08 under S3-A01；S3-6 engineering consequences require no
   repeated product approval；public MCP、manifest、external artifact delivery、G1 daemon/auth/Workbench、
   checkout publish、push/PR/release remain outside。
4. **Recovery:** verify branch `codex/agent-stage3`、HEAD `4c7347a` or the subsequent docs-only S3-6 control
   commit、only this document modified and no active tests；never move the installed legacy conda env；never
   initialize data under a deletable runtime target；if S3-6 semantic commit exists, use S3-S12 instead of
   replaying RED。

### S3-6 completion evidence

The semantic write set stayed inside Packet S3-6A and its S3-6C1 test-only correction.  The pre-existing
untracked `docs/CAD_BACKEND_RESEARCH.md` remained outside the packet and was not read, edited or staged.

- application and interaction runtime: `src/vibecad/application/agent.py`,
  `src/vibecad/application/data.py`, `src/vibecad/application/project.py`,
  `src/vibecad/application/task_api.py`, `src/vibecad/interaction/__init__.py`,
  `src/vibecad/interaction/cad.py`, `src/vibecad/interaction/checkouts.py`,
  `src/vibecad/interaction/protocol.py`, `src/vibecad/interaction/storage.py`;
- workflow and execution composition: `src/vibecad/workflow/catalog.py`,
  `src/vibecad/workflow/program.py`, `src/vibecad/workflow/service.py`,
  `src/vibecad/execution/adapter.py`, `src/vibecad/execution/candidate.py`,
  `src/vibecad/execution/executor.py`, `src/vibecad/execution/revisions.py`;
- replaceable runtime boundary: `src/vibecad/runtime/installer.py`,
  `src/vibecad/runtime/paths.py`, `src/vibecad/runtime/status.py`,
  `src/vibecad/runtime/uninstall.py`, `src/vibecad/server.py`;
- focused regression files: `tests/test_agent_application.py`,
  `tests/test_cad_execution_port.py`, `tests/test_candidate_revision.py`,
  `tests/test_execution_adapter.py`, `tests/test_installer.py`,
  `tests/test_interaction_protocol.py`, `tests/test_launcher_uninstall_integration.py`,
  `tests/test_managed_checkout.py`, `tests/test_model_program.py`, `tests/test_paths.py`,
  `tests/test_program_executor.py`, `tests/test_project_bootstrap.py`,
  `tests/test_revision_store.py`, `tests/test_server_round11.py`, `tests/test_status.py`,
  `tests/test_supervisor.py`, `tests/test_task_api.py`, `tests/test_task_catalog.py`,
  `tests/test_task_kernel_integration.py`, `tests/test_task_service.py`,
  `tests/test_uninstall.py`;
- orchestration record: `docs/orchestrated/vibecad-agent-stage3.md`.

Evidence chain:

1. The seven planned RED wave families each reached the intended missing contract: runtime/data ownership and
   uninstall preservation; store-only Application dispatch; empty/import generation-zero bootstrap;
   CadExecutionPort metadata/profile/version/thread/resource enforcement; per-project lazy isolation and the
   global CAD gate; managed checkout confinement/replay/budgets; and strict non-runnable IPC G0 values.  No
   setup, import or syntax error was accepted as RED.
2. `AgentApplication` now composes one durable data root, one shared task/revision/lease authority, isolated
   per-project Session/Slot/Coordinator runtimes, one process-wide CAD gate and a deterministic four-runtime
   clean-idle LRU. `TaskApi` exposes seven transport-neutral operations. CAD-bearing submit/resume/accept paths
   derive project routing from the durable TaskRun; create validates the caller-supplied project against its
   current full HEAD, get/reject are store-only, and capability reads are registry-only. These non-CAD paths
   remain FreeCAD-lazy.
3. Empty-project bootstrap publishes an exact generation-zero HEAD through empty initialization and exact
   readback; normalized FCStd import publishes only after private no-follow staging, semantic reload and an
   evidence-bound store copy. Pre-publication import swaps leave no project. A recordable post-publication
   staging-cleanup failure retains the successful project plus bounded retry state; inability to persist that
   cleanup record returns explicit durability uncertainty without rolling back the published project. Accept
   and CAD reconciliation/restart use the isolated runtime; Reject remains a store-only CAS and never creates
   a CAD runtime.
4. `CadExecutionPort` is the single trusted local CAD capability.  It enforces the closed profile, FreeCAD
   version, GUI-thread and admission budgets before handlers, then elapsed time, created-object and result-byte
   budgets after each synchronous handler.  This closes `S3-RES-08`; hard interruption/crash containment remains
   correctly assigned to `S3-RES-05`.
5. Managed checkout is path-free on the wire, non-authoritative, source/HEAD immutable and restart-safe.  A
   final correctness review exposed an authentic-digest/forged-`local_path` root-swap window: `get`, same-key
   replay and fresh publication produced `3 failed` before the fix.  Reopening the live SafeRoot and rebinding
   the exact checkout/model entry before every OPEN descriptor made the targeted cases `3 passed` and the full
   checkout file `39 passed`; an independent re-review returned PASS.
6. Runtime/data separation is cumulative across status, install, preview, direct/pending uninstall and server
   reporting. External-kind prefixes are identity/evidence-bound and read-only. Exact managed legacy prefixes
   are reused in place and may receive only the authorized server pip-only sync when stale; ambiguous legacy
   content, `data/` and `views/` are preserved. Status/receipt/log/lock/uninstall publication and removal use
   bounded no-follow FD-relative operations, atomic no-replace recovery and fixed-generation validation.
7. Runtime final review found that pinned-directory checks alone did not prevent absolute-path download,
   create/pip or verification from first touching a replacement tree.  Five race families (six parameterized
   cases) drove FD-relative download, pre-created exact env binding, a digest-validated private runner and
   evidence-bound Python probing; the final race gate is `6 passed` and replacement fingerprints/receipts remain
   unchanged.  A second review then found `preexec_fn=fchdir` unsafe in the real multi-threaded server parent;
   the genuine contract gate was `2 failed, 1 passed`.  A fixed stdlib helper now starts first, receives only
   `pass_fds` and argv, then performs `fchdir/execv` in its clean single-threaded process.  The final no-preexec
   gate is `3 passed`, with no production `preexec_fn` or shell execution.
8. Final objective gates on the settled tree: runtime aggregate `225 passed`; cumulative Agent/Application/
   bootstrap/CAD/checkout/protocol focused gate `1081 passed, 11 deselected`; full repository `3117 passed,
   92 deselected` with three existing macOS `fork()` deprecation warnings; real installed FreeCAD 1.1 Task
   Kernel integration `10 passed`.  Full Ruff, changed-file format check (`42 files already formatted`),
   `git diff --check`, compileall, fresh import/capability purity (`2 passed, 185 deselected`) and offline sdist/
   wheel build all PASS.
9. Independent final reviews cover both product architecture and the repaired boundaries.  Architecture/core
   composition and the checkout rebind returned PASS; installer R3 reran nine no-preexec/race cases plus
   installer/status `103 passed` and returned PASS.  No Critical, Important or Minor finding remains inside the
   approved S3-6 trust boundary.

Residual disposition appended at closeout:

| ID | Evidence / impact | Disposition / closure condition |
|---|---|---|
| S3-RES-08-C1 | profile/version/thread/admission and post-handler budgets are executable CadExecutionPort gates | closed by S3-E12 |
| S3-RES-02-O2 | Windows lacks the POSIX directory-FD guarantees used by installer/status/uninstall; compatibility fallback rejects visible aliases but is weaker | remains under S3-RES-02 until native Windows G3/junction/parent-replacement matrix passes |
| S3-RES-11 | a same-UID process can still replace a helper launcher or a single runner/Python entry inside an already pinned parent; after an OPEN descriptor returns it can also replace the local checkout Path before the caller opens it | accepted local-host trust boundary for G0; close with a descriptor-native worker/broker capability and handle/token-based checkout delivery, or an explicitly narrower threat model |
| S3-RES-12 | process death after parking an unhealthy managed env can retain an unreferenced private tree of up to the env size | defer; close with a durable identity-bound installer recovery journal and bounded restart cleanup |

The wide `src/vibecad/server.py` diff is mechanical Ruff formatting around the one approved uninstall-preview
preserve-data change; it is recorded rather than presented as additional server behavior.

| Entry | Decision / approval | Commit / push | Gate evidence | Residual | Snapshot | State |
|---|---|---|---|---|---|---|
| S3-E12 / 2026-07-21T13:07:56Z | S3-A01; Packet S3-6A/S3-6C1; architecture, checkout and installer R3 independent reviews PASS after all Critical/Important findings closed | S3-6 semantic commit containing this snapshot / not authorized | RED seven contract families; checkout 3; installer races 6; no-preexec 2/1 then 3 green; runtime 225; focused 1081/11; full 3117/92; managed FreeCAD 10; Ruff/format/diff/compile/import/build PASS | S3-RES-01..06, S3-RES-09, S3-RES-11..12 remain; closes S3-RES-08; S3-RES-02-O2 clarifies Windows | S3-S12 | completed |

### Recovery snapshot S3-S12

1. **Completed:** branch `codex/agent-stage3` from control anchor `c993431`; S3-6 composes the isolated expert
   Agent application, generation-zero bootstrap, durable review/restart path, trusted CadExecutionPort, managed
   checkout and non-runnable G0 protocol; runtime/data split and legacy/external reuse preserve user state.  All
   final objective gates and independent reviews PASS; push is not authorized.
2. **Next:** create the exact named-file local semantic commit
   `feat(application): compose isolated task project runtime`; then issue S3-7's independently reviewed packet
   for verified artifact materialization and thin MCP/Skill adapters over the same TaskApi/AgentApplication.
   Do not publish the old module-global Session surface or claim Workbench/authenticated IPC.
3. **Approved decisions:** S3-D01 through S3-D08 under S3-A01 and the standing continuous-execution authority;
   no product decision changed.  Public MCP/manifest cutover, external artifact delivery, G1 Workbench,
   push/PR/release/marketplace and external spend remain outside this packet.
4. **Recovery:** verify the local semantic commit subject above, final gate counts in S3-E12, no active pytest
   process and no staged/out-of-allowlist file.  The S3-6 allowlist must be clean after commit; preserve the
   unrelated untracked `docs/CAD_BACKEND_RESEARCH.md`.  Never move the installed legacy conda env, write an
   external override, initialize data under runtime, replay S3-6 RED implementation or expose G0 as runnable IPC.

## 8.10 Packet S3-7A — verified artifacts and atomic Agent MCP cutover

### 1. Authorization and unchanged product boundary

- Approval ID: S3-A01；artifact revision: S3-R3 / S3-R3.1 / S3-R3.2；bound decisions:
  S3-D01 through S3-D08。The user's standing instructions “你继续 猛猛的推进吧”、
  “好的 你持续推进  知道 TK9 完成” and “我睡觉去了  你可以持续推进” authorize
  continuous local implementation, tests, review, fixes and named-file commits inside that unchanged scope；
  no repeated internal-blocker approval is required。
- This packet inherits every higher-priority system、developer and user instruction、the repository
  allowlist、the current host permission model and sandbox。The Skill、artifact and packet cannot grant or
  expand permissions、elevate authority or bypass those controls。
- S3-7 only delivers verified artifact materialization、durable project ingress、stable Agent controls、
  registry-derived direct tools and one atomic legacy-public/manifest cutover。It does not implement the
  S3-8 skill/version/docs acceptance、G1 daemon/authenticated IPC/Workbench、G2 GUI worker、SelectorV1 Level B、
  arbitrary Python、Provider/model calls、second-OS support、push、PR、release、marketplace or external spend。
- Four read-only audits independently covered authoritative artifact copy-out、MCP/direct compilation、the
  current endpoint/manifest inventory and create-project response-loss recovery。They found no product-level
  decision change。Engineering corrections below narrow public claims to what the current Task Kernel can
  prove；they do not reopen S3-A01。

### 2. Workspace anchor, baseline and mechanical allowlist

- Repository: `/Users/wangtao/Documents/DevProject/vibecad`；branch `codex/agent-stage3`；current anchor
  `505a224aaeade6f36afbe54a749c0fae75b4ed79`。The S3-6 semantic anchor is `cf0f9b9`; the only later commit is
  the user's independent `docs: add multi-CAD backend research report`, which does not change any S3-7 source
  or test dependency and must be preserved。
- No applicable repository-local `AGENTS.md` or `CLAUDE.md` was observed。Issue-time worktree is clean and no
  test process is active。
- A first baseline command named nonexistent `tests/test_runtime_paths.py` and exited 4 before collection；it is
  retained as setup evidence and is not a semantic RED。The corrected focused baseline is:

  ```text
  PYTHONPATH=src uv run pytest -q \
    tests/test_task_api.py tests/test_agent_application.py \
    tests/test_project_bootstrap.py tests/test_revision_store.py \
    tests/test_task_kernel_integration.py tests/test_runtime_purity.py \
    tests/test_uninstall.py tests/test_mcpb_manifest.py \
    tests/test_server_round10.py tests/test_server_tools.py
  # 487 passed, 10 deselected
  ```

- The S3-7 control commit may modify only this file。The S3-7 semantic implementation may modify or add only:
  - `pyproject.toml`
  - `uv.lock`
  - `README.md`
  - `docs/PRODUCT_CAPABILITY_ROADMAP.md`
  - `src/vibecad/application/__init__.py`
  - `src/vibecad/application/data.py`
  - `src/vibecad/application/project.py`
  - `src/vibecad/application/project_create.py`（new）
  - `src/vibecad/application/project_api.py`（new）
  - `src/vibecad/application/artifacts.py`（new）
  - `src/vibecad/application/task_api.py`
  - `src/vibecad/application/agent.py`
  - `src/vibecad/application/public_surface.py`（new）
  - `src/vibecad/execution/candidate.py`
  - `src/vibecad/execution/revisions.py`
  - `src/vibecad/execution/executor.py`
  - `src/vibecad/interaction/cad.py`
  - `src/vibecad/interaction/storage.py`
  - `src/vibecad/mcp_transport.py`（new）
  - `src/vibecad/runtime/installer.py`
  - `src/vibecad/runtime/spec.py`
  - `src/vibecad/runtime/status.py`
  - `src/vibecad/supervisor.py`
  - `src/vibecad/workflow/store.py`
  - `src/vibecad/workflow/catalog.py`
  - `src/vibecad/workflow/lease.py`
  - `src/vibecad/workflow/service.py`
  - `src/vibecad/server.py`
  - `.mcpbignore`
  - `manifest.json`
  - `tests/test_project_api.py`（new）
  - `tests/test_artifact_materialization.py`（new）
  - `tests/test_server_agent_surface.py`（new）
  - `tests/test_mcp_transport.py`（new）
  - `tests/test_project_bootstrap.py`
  - `tests/test_revision_store.py`
  - `tests/test_candidate_revision.py`
  - `tests/test_cad_execution_port.py`
  - `tests/test_program_executor.py`
  - `tests/test_agent_application.py`
  - `tests/test_task_api.py`
  - `tests/test_task_store.py`
  - `tests/test_task_catalog.py`
  - `tests/test_task_service.py`
  - `tests/test_workflow_lease.py`
  - `tests/test_task_kernel_integration.py`
  - `tests/test_mcpb_manifest.py`
  - `tests/test_server_tools.py`
  - `tests/test_server_round5.py`
  - `tests/test_server_round6.py`
  - `tests/test_server_round7.py`
  - `tests/test_server_round8.py`
  - `tests/test_server_round10.py`
  - `tests/test_server_round11.py`
  - `tests/test_server_new_tools.py`
  - `tests/test_runtime_integration.py`
  - `tests/test_runtime_purity.py`
  - `tests/test_status.py`
  - `tests/test_installer.py`
  - `tests/test_supervisor.py`
  - `tests/fake_server.py`
  - `tests/test_uninstall.py`
  - `tests/test_release_workflow.py`
  - `docs/orchestrated/vibecad-agent-stage3.md`
- Legacy server-adapter assertions may be removed or rewritten only after equivalent reusable engine/tool
  behavior is confirmed in its existing module tests。Low-level `workflow/state`、registry
  metadata、engine/tools/feedback、runtime installer and G0 protocol semantics are outside this packet；a
  genuine RED proving an unavoidable domain gap triggers the breaker rather than an opportunistic edit。

S3-R3.3 internal correctness correction：the cumulative S3-7 regression and a deterministic two-process
barrier proved that two legitimate first openers of one fixed lease entry can both observe the entry missing，
after which the second opener misclassifies the first opener's atomic creation as `unsafe_lock_entry`。This
breaks the already-approved fixed catalog/per-key/slot concurrency semantics in TaskRun and durable project
creation；it does not add a product capability or widen a trust boundary。Therefore the semantic allowlist adds
only `workflow/lease.py` and its existing focused test file for the minimum create-race correction。All existing
replacement、link、mode、owner and process-isolation tests remain mandatory；no other lease behavior changes。

S3-R3.5 internal correctness correction：the supervisor's strict MCP handshake and replay gate cannot be
exercised by the pre-S3-7 ad-hoc `swap`/`crash` messages because those messages are not valid MCP requests and
therefore must now be rejected before child dispatch。This is a test-fixture gap rather than a production or
product-scope change。The semantic allowlist consequently adds only `tests/fake_server.py`，whose replacement
controls use valid `initialize` and `tools/call` frames and record whether a non-replayable tool crossed a
generation boundary。All production supervisor and public-surface bounds remain unchanged。

S3-R3.6 internal correctness correction：MCPB packages `README.md` because the frozen local wheel build uses it
as project metadata，but the pre-cutover README still advertises removed legacy endpoints and unverified GUI、
assembly、render and multi-platform behavior。Changing only `manifest.json` would therefore ship a mixed public
surface inside one archive。The semantic allowlist adds `README.md` solely to describe the same 20-tool Darwin
Agent contract、BYO host-model boundary and G1/P1/P2 roadmap；the manifest test adds negative assertions for
removed endpoint names。No implementation capability or release claim is added。

S3-R3.7 internal correctness correction：the linked product roadmap still labeled task-level MCP and durable
draft/review as unimplemented S3-entry gaps。After the atomic S3-7 cutover this would contradict both the package
README and live surface even though G1/P1/P2 remain future work。The semantic allowlist adds only
`docs/PRODUCT_CAPABILITY_ROADMAP.md` to timestamp and label the S3-7/P0-A implementation state and preserve those
four items as historical Stage-3-entry rationale；a package-doc test freezes the current/future distinction。
No roadmap scope or phase ordering changes。

### 3. Exact public surface and one-writer cutover

The checked-in manifest and live `tools/list` are one deterministic ordered projection, never a literal-count
product boundary：

```text
stable controls in this order:
  ping
  get_runtime_status
  ensure_runtime
  uninstall_runtime
  get_capabilities
  create_project
  get_project
  create_task
  get_task
  submit_model_program
  resume_task
  accept_draft
  reject_draft
  export_task_artifacts

then registry operation names sorted where direct_exposed == true:
  create_box
  create_cylinder
  inspect_model
  modify_parameter
  move_part
  rotate_part
```

The current derived total happens to be twenty but no code、test or product document may use `20` as the
boundary。`smoke_cad` becomes a private installer/runtime probe。Every other legacy public Session endpoint is
removed from both registration and manifest in the same semantic commit。`move_part` and `rotate_part` retain
only their names；their schema、handler and state authority are replaced completely。

The bootstrap process has a pre-application runtime guard。Only `ping`、`get_runtime_status`、`ensure_runtime`、
`uninstall_runtime` and pure `get_capabilities` may execute there。Every project/task/review/export/direct call
and every resource read checks the guard before constructing/opening AgentApplication or touching Task/CAD；it
returns the exact fixed `runtime_unavailable` envelope/resource error with zero application-port calls。If the
managed runtime has become ready, the owned transport first flushes that fixed response and only then schedules
the supervised swap, so non-idempotent `create_task` is never pending across the normal swap。A process-wide
latch forbids a normal runtime-ready SWAP_EXIT after application opening/effect entry。Confirmed uninstall is the
sole exception：the owned transport enters `DRAINING`, blocks new application/resource admissions, boundedly
waits for every other slot/worker, closes the exact AgentApplication, flushes the marked-uninstall response and
only then exits for supervisor cleanup/bootstrap respawn。Drain/close/flush failure keeps the marker, returns fixed
`recovery_required` and does not exit。Barrier RED around readiness/flush proves every guarded call executes zero
times pre-swap and at most once after caller retry；uninstall versus create/direct/resource proves no request is
killed/replayed twice, durable data is preserved and only managed runtime is removed。

`server.py` must not import or construct `Session`, keep `_session`, decorate a legacy CAD handler or call
`vibecad.tools.*`。It owns only a PID-bound, thread-safe single-flight `AgentApplication` composition slot with
the exact states `UNOPENED -> OPENING -> READY -> CLOSING -> CLOSED`。Only one opener may run in one process；
concurrent first callers wait for and observe that same result。Open failure closes every partial instance and
atomically returns to `UNOPENED`。A fork/PID mismatch never uses an inherited instance and fails closed or resets
only after closing its own process-local partial state。Shutdown detaches the exact READY instance under the
slot lock, closes it exactly once outside the lock and leaves `CLOSED`, after which reopen is forbidden。The
module import、MCP initialize、`tools/list`、`resources/templates/list` and `resources/list` paths do not create
an application/runtime/data directory or load `FreeCAD`/`Part`。The lifecycle RED uses barriers for concurrent
open、open-failure retry、close/open races and wrong-process access, and accounts for a close receipt for every
constructed instance。

Public MCP annotations describe the whole adapter side effect rather than only CAD geometry risk。Every one of
the four hints is explicit；the exact independent expected table is:

| tool(s) | readOnly | destructive | idempotent | openWorld |
|---|---:|---:|---:|---:|
| `ping`, `get_capabilities` | true | false | true | false |
| `get_runtime_status` | false | false | true | false |
| `ensure_runtime` | false | true | true | true |
| `uninstall_runtime` | false | true | true | false |
| `create_project` | false | false | true | true |
| `get_project`, `get_task` | false | false | true | false |
| `create_task` | false | false | false | false |
| `submit_model_program`, `resume_task`, `accept_draft`, `reject_draft` | false | true | true | false |
| `export_task_artifacts` | false | false | true | false |
| `create_box`, `create_cylinder`, `inspect_model` | false | false | true | false |
| `modify_parameter`, `move_part`, `rotate_part` | false | true | true | false |

`get_project`/`get_task` are conservatively non-read-only because the full adapter may single-flight open and
recover durable application state；direct `inspect_model` is non-read-only because it writes TaskRun、candidate
and Revision evidence；`get_runtime_status` is non-read-only because it can schedule interpreter swap。One test
compares runtime `tools/list` field-for-field against this separately hand-written literal table；comparing two
projections made by the same generator is insufficient。

FastMCP's decorator/Pydantic layer is not the strict-ingress or exception-sanitization boundary。A
VibeCAD-owned low-level adapter authoritatively registers `tools/list` and `tools/call` on the SDK server；the
default FastMCP tool manager may help construct internal metadata but never receives a public call first。
`tools/list` returns the exact ordered public-surface projection with each complete
`additionalProperties:false` schema and the annotation table above。`tools/call` first resolves the name from
that closed projection, then validates the already protocol-parsed argument object itself：exact key set、exact
non-coercing types/identifiers/safe integers、nested schema and all byte/node/depth/count budgets。Unknown fields
are rejected rather than ignored and the selected API is invoked at most once only after ingress passes。
`acceptance_json` and every durable raw JSON record additionally use the same duplicate-key-rejecting decoder。
Unknown tool、schema、
validator、API and unexpected exceptions map to fixed path-free MCP error/envelope values。Exact successful
domain projections intentionally return their frozen persisted task/program/result/request-key fields；outside
those projections, error/control responses and logs echo no caller-derived parameter、handler exception、argument
repr、source path or URI。Logs contain only a fixed error code and random correlation id；JSON-RPC's validated
request `id` is the only unavoidable protocol echo in failures。No-reflection/no-secret tests therefore preserve
the exact successful domain projection while placing secrets in failing `params`/handler values/exceptions。
Public error `path` values likewise contain only independently frozen schema tokens and numeric indices；an
unknown key maps to its known enclosing path plus `/_unknown`, and a nested ContractValidationError is projected
through the schema with the first unknown token replaced。No caller key text is echoed。Secret-as-key RED covers
outer/direct、AcceptanceSpec and ModelProgram nesting。
After FastMCP construction and before the owned transport starts, the `mcp` namespace
logger receives one fixed discard-only handler with `propagate=False`, and every already-instantiated child is
disabled；present and future `mcp.*` loggers therefore cannot reach a root handler added later。A persistent
pathname filter on the root logger and existing handlers separately drops the SDK's direct root `logging.*`
calls。This covers pre-dispatch raw-message and session validation logging at DEBUG。
VibeCAD error logging uses one dedicated non-propagating logger and never logs request values。Real JSON-RPC RED
at root/SDK DEBUG proves an extra field keeps the API spy at zero, wrong type/oversize/
secret `source_path` cannot be found in response or captured logs, and `tools/list` matches the independent
literal contract field-for-field；protocol-invalid secret-bearing calls/URIs/notifications and forced handler
exceptions receive the same no-secret log assertion。

Every public tool returns the exact four-field schema-v1 envelope。The owned adapter constructs
`CallToolResult` directly：`structuredContent` is that envelope, `content` is exactly one
`TextContent(type="text", text=<compact canonical JSON of the same envelope>)`, and `isError` is exactly
`!envelope.ok`；each `Tool.outputSchema` is the independently frozen specialized envelope schema。No SDK result
normalizer or exception builder participates。

An unknown tool name uses fixed MCP `-32602` / `Tool name is not available.` with zero API access；a malformed
tools/call container rejected before one tool schema is selected uses `-32602` / `Tool request is invalid.`；a
known tool's argument/schema failure uses that tool's exact public error envelope/CallToolResult；an unexpected
adapter failure uses `-32603` / `Tool request could not be completed.`。None includes input or `str(e)`。

Runtime-control wire shapes are literal too。`ping` takes `{}` and its result is exactly
`{schema_version,service:"vibecad",version}`；`get_runtime_status` takes `{}` and returns exactly
`{schema_version,phase,percent,message,error,runtime_compatible,runtime_action,installed_version,
required_version,needs_reconnect}` with bounded enums/strings and no raw installer exception；`ensure_runtime`
takes `{}` and returns exactly `{schema_version,status,message}` where status is `started|in_progress|ready`；
`uninstall_runtime` takes exactly `{confirm:<bool>}` and returns exactly
`{schema_version,status,confirm_required,estimated_size_bytes,data_preserved:true,message}` where status is
`preview|marked|already_clean`。Its public result never exposes a local path。Control ingress/failure codes and
messages are exactly `missing_field,unknown_field,invalid_type,invalid_value,runtime_failure,store_failure,
recovery_required,internal_error`, each with a fixed path-free message except bounded ingress JSON pointer；they use the same
envelope/error shape。All schemas set `additionalProperties:false`。Wire RED covers ok/error `isError`、content/
structured byte equivalence and outputSchema independently。

The owned boundary also replaces the SDK's unbounded `stdio_server` and unbounded per-message `Server.run`
dispatch loop。It reads newline frames incrementally in at most 65,536-byte chunks with a 2,097,152-byte request
wire ceiling, strict UTF-8 and duplicate-key-rejecting JSON；an oversize/unterminated frame is drained with
constant memory, receives one fixed JSON-RPC parse error with null id when possible, then closes。Invalid UTF-8、
duplicate keys and malformed JSON use the same closed no-input error。Before object decoding, an incremental
lexical scan caps depth at 64、tokens/nodes at 65,536、keys at 256 UTF-8
bytes、each decoded string at 1,048,576 UTF-8 bytes and integers at safe-JSON range/16 decimal digits；finite
float decimal/exponent tokens are allowed up to 64 lexical bytes, while NaN/Infinity and ambiguous numeric forms
are rejected。These limits admit the 512-KiB ModelProgram and fractional dimension/position/angle ingress without
allowing a compact adversarial frame to expand beyond the aggregate memory proof。
Only bounded raw mappings then pass a VibeCAD-owned exact prevalidator for the complete supported ClientRequest/
ClientNotification union, including method、params and URI；any typed/AnyUrl/unknown-method failure is sanitized
there。Only objects guaranteed to survive the SDK's repeated typed validation reach pinned low-level handlers,
so `mcp.shared.session` never receives an invalid secret-bearing request。SDK stdio parsing never sees raw
unbounded input。
Response frames are capped at 100,663,296 bytes, enough for the bounded resource result and no more。

Eight pre-created in-flight slots apply before dispatching any request or notification；the reader backpressures
application dispatch when full and releases a slot only after response/drop plus actual worker cleanup。A
separate single slot permits at most one full-buffer `resources/read` at a time；N/N+1 cannot enter application/
CAD or allocate/read any payload, and that slot is held through result serialization and completed write。
Blocking CAD/file/tool work runs only through four pre-created workers behind the same eight-slot bounded queue；
the event loop never submits to an unbounded default executor。Disconnect/cancellation marks response suppression
but retains occupied work slots until workers return；only a pre-dispatch protocol error releases immediately。
The child-process request-path incremental allocation, including one 402,653,184-byte resource path plus seven
bounded frames/tasks, is at most 536,870,912 bytes；this is deliberately not mislabeled as absolute RSS because
the bounded four-runtime cache and loaded FreeCAD/Part have a preexisting baseline。

When all eight work slots are full, one separately bounded 2,097,152-byte control lane keeps reading frames。
It handles EOF or a fully prevalidated `notifications/cancelled`；a non-control request immediately receives fixed
`-32005` / `Server is busy.` with zero dispatch, and a non-control notification is dropped, leaving the lane free
for later cancellation。Cancellation marks `CANCEL_REQUESTED` but does not call the SDK's immediate responder
cancel or release work/worker/resource slots。Only after the synchronous worker and cleanup actually return does
the child emit exactly one fixed `-32800` / `Request cancelled` response for the original id, then release its
slots。The supervisor mirrors this one
control lane without retaining another replayable work request。Eight-full→ninth-work→cancel and
cancelled-blocking-CAD barriers prove liveness without early capacity reuse。

The child also reserves every active non-null JSON-RPC id before SDK dispatch；a duplicate id gets fixed
`-32600` / `Invalid Request` with zero handler access and cannot replace SDK/session state。The reservation is
released only after response/drop and actual worker/cleanup completion。On cancellation, the child id reservation
and supervisor `CANCEL_REQUESTED` tombstone remain until that final cancellation response；supervisor consumes it,
clears bounded pending state and forwards it unless the client disconnected。A cancel paused after create +
same-id resend, disconnect→worker-return and post-ack reuse RED prove no overlap or capacity leak。

The stdlib supervisor enforces the same 2,097,152-byte inbound frame ceiling and eight-request pending/
backpressure bound plus the same request lexical limits before forwarding or retaining replay bytes。Its
child-response reader uses bounded chunks
and rejects a line above 100,663,296 bytes before `json.loads`；one resource response has a measured supervisor
incremental peak cap of 402,653,184 bytes。Real supervised-stdio gates cover request/response N/N+1、invalid UTF-8、duplicate/
unterminated frames、request/resource floods、disconnect/cancel slot release and separate child/supervisor RSS
bounds；neither process logs secret frame content。

Supervisor accepts exactly one `initialize` then one `notifications/initialized`; duplicate/out-of-order
handshake frames are rejected。The two retained handshake frames use a separate fixed replay budget of exactly
two frames and at most 4,194,304 bytes；they do not consume the eight active request/id slots or their bounded
pending bytes。A third handshake frame or bytes above that separate budget are rejected before forwarding。
Duplicate in-flight JSON-RPC ids are rejected before forwarding and cannot overwrite `_pending`。Across a
SWAP_EXIT it may replay only the handshake plus this exact method-level safe set：`ping`、`tools/list`、
`resources/list`、`resources/templates/list` and
`resources/read` (whose bootstrap guard has zero application effect), union `tools/call` names whose frozen
annotation is idempotent。A pending
`create_task` or unclassified call is dropped and receives fixed `-32003` / `Tool outcome is unknown; inspect
durable state before retry.`；its bytes are never replayed。The normal bootstrap guard flush-before-swap rule
means this fallback is exceptional, but crash-after-create-before-response RED still proves at most one task。
Swap barriers in every discovery/resource method prove initial host negotiation remains replay-safe。

The same atomic cutover replaces manifest top-level `description` and `long_description` with only the proven
Darwin Agent surface：durable project/task/review、explicit ModelProgram、the six current direct operations and
verified FCStd/STEP resources。It must not claim hole、fillet、render/three-view、assembly/interference、STL、
Workbench、Windows or any removed legacy endpoint；negative package tests freeze those exclusions。The manifest
declares only `darwin`, the sole platform with real AgentApplication/managed-FreeCAD evidence。

Dependency-version closure is packaged lock evidence, not an ambient installed-environment assumption。The
MCPB intentionally does not vendor third-party wheel bytes；a first launch therefore needs network access to the
locked indexes，while a warm uv artifact cache may satisfy the same hashes without network。The semantic change pins
`mcp==1.27.2` in `pyproject.toml`, regenerates `uv.lock` offline, removes that lock from `.mcpbignore`, changes
the manifest launch to frozen mode and requires the unpacked MCPB to contain the byte-identical regenerated
lock。The exact manifest argv is
`["run","--frozen","--no-dev","--no-editable","--no-build-isolation","--directory","${__dirname}","mcpb_entry.py"]`。An unpacked-directory
gate runs with `--frozen`, asserts the live SDK version before initialize/tools and resource checks, and fails
on any lock/hash/version drift。Because public version remains 0.4.0 until S3-8, `runtime.spec` also increments a
private integer `SERVER_PACKAGE_EPOCH`。Every managed and external receipt/binding has exact
`server_package_epoch`、`mcp_version:"1.27.2"` and the canonical new `public_surface_sha256`；
`_VERIFY_SNIPPET` independently imports the installed epoch/MCP and recomputes that surface fingerprint before
issuing a receipt。A prior S3-6 same-version managed receipt therefore classifies as server mismatch, triggers the
existing pip-only package sync rather than an engine rebuild, is reverified and only then may supervisor swap。
An old external/override receipt is incompatible and is never auto-modified；an adopted legacy external env must
either pass the new verification and receive a new binding, cause creation of a fresh owned current env, or
return explicit repair—never stale-swap。Post-swap gates cover both the real pre-S3-7 managed shape and this
host's old external-legacy shape, prove current installed identity from `active_runtime_python`, exercise the
low-level surface and do not mask it with repository `PYTHONPATH`。S3-8 later performs the public version bump。
A future SDK/epoch/surface change needs its own lock update and compatibility evidence。

S3-R3.4 internal correctness correction：a real frozen launch under CPython 3.13.14 proved that its patched
`site` skips Hatchling's hidden `_editable_impl_vibecad.pth`，so the prior exact argv could resolve every locked
dependency and still fail at `mcpb_entry.py` with `ModuleNotFoundError: vibecad`。The manifest therefore adds
uv's `--no-editable` flag：the same locked source is installed as a normal wheel，the child imports the packaged
bytes without `PYTHONPATH`，and the managed-runtime identity check remains authoritative。A second clean-environment
gate then proved that normal wheel installation otherwise creates an isolated build environment and can require
an untracked network fetch of Hatchling。The package consequently freezes `hatchling==1.28.0` in the ordinary
environment and adds `--no-build-isolation`，so both application and build dependency identities come from the
packaged lock rather than an independent build resolver；artifact bytes still arrive through uv's normal locked
cache/network path。This changes neither the public surface nor any product/trust boundary；the manifest test
freezes the corrected argv and the final unpacked-package gate must launch it in a newly created environment。

S3-R3.8 test-contract correction：the real host's external legacy receipt predates the S3-7 package epoch，MCP
pin and public-surface fingerprint。It is therefore incompatible by construction and must fail closed；the old
integration assertion that silently adopted it as current is no longer valid。The deterministic legacy gate
selects that exact prefix through an explicit override，requires status and installer rejection，and proves no
install、pip、delete or external-tree/binding mutation occurred。A separate destructive-capable migration gate is
disabled unless all of `VIBECAD_RUN_INTEGRATION=1`、the exact
`VIBECAD_RUN_FRESH_MIGRATION=install-current-managed-preserve-external` confirmation、a separately repeated
`VIBECAD_FRESH_MIGRATION_HOME` equal to `VIBECAD_HOME`、the reviewed checkout as `VIBECAD_PIP_SPEC`、no
override and an initially absent current prefix are present。It may then create only the fresh owned current
runtime，requires the exact epoch/MCP/surface identity there，and proves the old external tree、binding and data
tree unchanged。The controller executes that one gate only after final review。

S3-R3.9 internal correctness correction：the first real fresh-current migration reached the already validated
micromamba binary but micromamba rejected the per-attempt executable name `.vibecad-runner-*` as
`unknown MAMBA_EXE`；its executable basename must be exactly `mamba` or `micromamba`。The secure correction keeps
one fixed `.vibecad-runner` directory at mode `0700` under the pinned managed `env.parent` and stages the
checksum-bound file inside it with the exact basename `micromamba`，so execution remains relative to the pinned
env fd as `../.vibecad-runner/micromamba`。Both directory and file are checked for device、inode、owner、mode and
link-count validity before and after command dispatch；the source copy remains descriptor-pinned and SHA-256
bound。A complete crash remnant with that exact digest is safely adopted and reused；a partial/mismatched file or
any extra entry fails closed without deletion。

An adversarial cleanup review then rejected deleting a per-attempt directory：POSIX has no portable
identity-bound `rmdir` by an already-open directory fd，so a check followed by `rmdir(name)` retains a same-UID
replacement window。The fixed private directory therefore persists and cleanup unlinks only the exact validated
`micromamba` identity through its held directory fd；a replacement file or directory is never name-deleted。
Normal success and copy、digest or command failure leave the fixed directory empty。A process death or cleanup
syscall failure can leave at most its one fixed-name file；an exact file self-recovers on the next serialized
attempt，while an untrusted remnant blocks without creating another。The residual same-UID mutation window at
command path resolution remains the already-recorded S3-RES-11 local-host trust boundary and is detected by the
before/after identity gates；no broader-user or remote actor gains write access。This correction changes no public
tool、product capability or runtime ownership boundary，and the failed real prefix is outside this subtask's
mutation authority。

S3-R3.10 review correction：the first independent review of S3-R3.9 proved that validating the fixed runner
inode and then calling `unlink(name, dir_fd=...)` still has a same-UID check→unlink replacement window。The
reviewer deterministically replaced the validated name at the unlink seam；the replacement was deleted while the
original inode survived。That contradicts the claimed identity-bound cleanup and is an unexpected G1 red。The
minimum correction makes the checksum-bound fixed runner a persistent private runtime dependency instead of
attempting an unlink that POSIX cannot bind to an open file identity。Every later use must still validate the
private directory、the sole `micromamba` entry、owner、mode、link count、device/inode and SHA-256 before and after
dispatch；a mismatched or extra entry remains fail closed and untouched。Copy or validation failure may leave at
most the same fixed entry and must report failure rather than delete an unproven name。This removes the cleanup
TOCTOU without changing the already-recorded S3-RES-11 same-UID command-path trust boundary。

The exact semantic allowlist above adds only `src/vibecad/runtime/installer.py` for S3-R3.9/S3-R3.10；
`tests/test_installer.py` and this artifact were already named。This supersedes the earlier blanket statement that
all runtime-installer semantics were outside S3-7 only for the exact micromamba runner basename、staging and
validation path。Authorization remains S3-A01 plus the user's standing words “好你自我持续推进 不需要我做产品级
决策的时候就不要停” and “继续执行”：this is an internal correctness/recovery correction inside the unchanged
managed-runtime ownership and product boundary，not a new product decision。No other installer behavior、external
runtime mutation、data deletion、push、release or external spend is authorized。

S3-R3.11 real-CAD contract correction：the old external FreeCAD 1.1 regression lane exposed two independent
integration defects after the quota/journal implementation landed。First，the low-level `_CHILD` and
`_SELECTOR_PRESERVATION_CHILD` fixtures compose `LocalRevisionStore`/`CandidateCoordinator` directly but never
initialize the process-wide candidate file-limit signal policy；they therefore reject candidate begin before CAD
and report `needs_input/candidate_begin_failed`。The production `AgentApplication.open()` already initializes this
policy，so only those direct-composition fixtures add the explicit main-thread initialization。

Second，RevisionStore now reserves quota and namespace authority by creating exact owner-only zero-byte
`model.FCStd` and `model.step` placeholders，while the unchanged `InProcessCadExecutor.export_step()` rejects any
existing STEP target。A traced real run completed all six CAD operations and the FCStd checkpoint before rejecting
that valid reserved placeholder as `artifact_failure`；the same contradiction prevents AgentApplication review
draft publication。The minimum production correction accepts only the store-authorized exact empty ordinary
placeholder（regular file、current owner、mode `0600`、link count one、size zero），records its stable identity，lets
FreeCAD write that fixed target，then requires the same device/inode/owner/mode/link identity、bounded nonzero
size and valid STEP envelope before proceeding。A nonempty file、link、wrong owner/mode/count or identity drift
still fails closed；no unbounded temporary namespace or quota formula changes。Focused tests belong only in the
already-allowed `tests/test_cad_execution_port.py` and the direct fixtures in the already-allowed
`tests/test_task_kernel_integration.py`。The diagnostic compatibility probe made no repository change and proved
direct six-operation commit、Selector success/failure isolation and AgentApplication cross-process draft accept
all pass after these two exact corrections；the old external tree and receipt remained unchanged。This is an
internal contract repair under S3-A01 and the same standing user authorization，not a product-boundary change。

S3-R3.12 independent-review correction：the first read-only review of S3-R3.11 proved that
`InProcessCadExecutor.export_step()` still treated a missing reserved `model.step` as an acceptable legacy
case，letting FreeCAD create a new inode instead of proving that the RevisionStore-reserved placeholder exists。
That bypasses the frozen namespace/inode authority even though every present-placeholder check passes，so the
S3-R3.11 acceptance gate remains closed。The minimum correction maps `FileNotFoundError` to the same fixed
`ARTIFACT_FAILURE` before any shape access or CAD write，adds a focused missing-placeholder negative test，and
updates the pre-quota controlled-export unit fixture to create the exact owner-only empty placeholder now
required by the production contract。The semantic allowlist therefore adds only
`tests/test_program_executor.py`；`src/vibecad/execution/executor.py`、`tests/test_cad_execution_port.py` and
`tests/test_task_kernel_integration.py` were already named。This supersedes the old unit-test oracle only for
placeholder preparation and does not change the successful export behavior，public surface，quota formula or
rollback boundary。Authorization remains S3-A01 plus the user's standing words “好你自我持续推进 不需要我做产品级
决策的时候就不要停” and “继续执行”：this is a review-proven internal correctness fix，not a product-level
decision。No runtime install/delete，external tree mutation，push，release or external spend is delegated by
this correction。

S3-R3.13 pre-final concurrency correction：an independent settled-diff audit proved that the frozen §4.3
linearization contract was implemented on only one side。`ArtifactMaterializationService` holds the bounded
`artifact-export:<task_id>` gate through final eligibility reload and `PUBLISHED` readback，but
`AgentApplication.accept_draft()` and `reject_draft()` currently enter their TaskRun transition without the
same gate。Accept or Reject can therefore win after export's final draft reload but before publication，allowing
delivery of a draft whose authority already changed。Sequential and real-CAD happy-path gates cannot expose
this race，so final S3-7 acceptance remains closed despite those gates passing。

The minimum correction makes both AgentApplication review transitions acquire the existing
`LocalArtifactAuthority.acquire_export_gate(task_id=...)` capability before any catalog/CAD/project lease or
TaskRun CAS，hold it through the complete transition and result，then release it。Reject must remain no-CAD and
must not initialize the artifact store/materializer or CAD validation port merely to obtain the lightweight
authority capability。Gate contention is a fixed `LEASE_UNAVAILABLE` port failure before mutation；pre-entry
task/store/integrity/recovery failures map only to the existing closed task-port taxonomy；a release failure
after the body may have committed and therefore returns `RECOVERY_REQUIRED` rather than claiming a definite
non-effect。No new lock order is allowed：task artifact gate → process CAD gate → project/store locks。A genuine
deterministic RED must pause export after its final eligibility reload and prove both Accept and Reject cannot
cross the shared gate；after publication，the review transition may proceed and the already-published immutable
response remains replayable。Focused tests may change only the already-allowed
`tests/test_agent_application.py` and/or `tests/test_artifact_materialization.py`，with
`tests/test_task_kernel_integration.py` used only if a cross-process fixture is necessary。Production changes
are limited to the already-allowed `src/vibecad/application/agent.py`；the artifact gate implementation、TaskRun
state machine、public schemas and materialization protocol do not change。Authorization remains S3-A01 plus the
user's standing continuous-execution direction；this closes a review-proven internal atomicity defect and is
not a product-level decision。No runtime install/delete，external mutation，push，release or spend is delegated。

The cumulative task-service gate may additionally update only the already-allowed
`tests/test_task_service.py::test_application_accepting_gap_evicts_then_recovers_descendant_without_recommit`。
That pre-S3-7 synthetic fixture constructs `AgentApplication` with `object.__new__` and fake non-store
dependencies，so it must inject one explicit gate-capable authority returning a no-op context solely to retain
the test's later runtime-gap oracle。Production may not recognize that invalid composition or bypass the shared
gate；no other task-service expectation changes。

S3-R3.14 final managed-package identity correction：the fresh current-managed runtime was installed with
private `SERVER_PACKAGE_EPOCH == 1` before the late S3-R3.12/R3.13 source corrections。The current receipt and
installed package still satisfy version `0.4.0`、epoch 1、MCP 1.27.2 and the unchanged public-surface digest，
so `runtime_ready()` remains true even though the installed internal executor bytes are older than the settled
checkout。The supervisor deliberately starts `active_runtime_python -m vibecad.server` and therefore would run
that stale package；a checkout-PYTHONPATH real test or unpacked identity probe alone cannot prove the final
runtime child。This is the exact same-version server-package replacement case for which §4.2 introduced the
private epoch，and final S3-7 acceptance remains closed until it is advanced and synchronized。

After every source correction is frozen and independently review-PASS，advance only the private
`SERVER_PACKAGE_EPOCH` monotonically from 1 to 2；the public package/manifest version remains 0.4.0 and the
public-surface digest remains unchanged。Update the exact epoch assertions/receipt fixtures only in the
already-allowed `tests/test_status.py` and，if a genuine RED requires it，`tests/test_installer.py` or
`tests/test_supervisor.py`；production change is limited to the already-allowed
`src/vibecad/runtime/spec.py`。Then run the normal managed `RuntimeInstaller.install()` once with the reviewed
checkout as `VIBECAD_PIP_SPEC` and no override。It must classify the existing epoch-1 generation as a server
mismatch，take the existing pip-only sync path，preserve the current prefix inode and FreeCAD engine，write the
epoch-2 managed receipt only after exact verification，and never rebuild or delete the engine。Manual pip、
receipt-only rewriting or a second environment is forbidden。Post-sync evidence must come from the active
runtime Python with no checkout `PYTHONPATH` and prove epoch 2、MCP 1.27.2、surface digest、FreeCAD 1.1.0 and
the final corrected package behavior/bytes；the 44,109-byte legacy external/binding/data baseline must remain
exactly unchanged。Only then may the final current-managed and fresh-unpacked MCPB E2E gates run。Authorization
is S3-A01、the previously approved real managed migration and the user's standing continuous-execution
direction；this is a bounded upgrade of the owned managed server package，not a public version/product decision。
No external runtime mutation、engine reinstall/delete、user-data deletion、push、release or spend is authorized。
Any pre-existing `dist/` wheel or sdist is stale pre-freeze evidence and is forbidden as an install/package
source；the final build uses a new output directory and proves the wheel's epoch and packaged Python bytes
against the frozen checkout before packing MCPB。

S3-R3.15 managed-sync execution correction：the first controller invocation of the approved normal installer
added `PIP_NO_INDEX=1` and `PIP_NO_BUILD_ISOLATION=1` to keep the source sync offline。Pip 26.1.2 treats the
negative option's environment value inversely：a read-only parser probe observed `1/true -> build isolation`
and `0/false -> no build isolation`。The invocation therefore entered an isolated build environment and failed
before building or installing VibeCAD because no index could supply `hatchling==1.28.0`。It emitted only
`INSTALLING_PIP -> FAILED`；after termination the exact epoch-1 receipt (477-byte identity record)、the complete
45,772-byte current engine snapshot、the 44,109-byte legacy/binding/data snapshot and the installed epoch-1
`executor.py` digest all remained byte-for-byte unchanged，with no server/FreeCAD process left running。This is
a controller environment-flag error，not a product/source defect；manual pip、receipt rewriting and engine
replacement remain forbidden。

The failed attempt is retained as gate-red evidence and cannot count as the required sync。One recovery
invocation of the same normal `RuntimeInstaller.install()` is permitted only from those reverified unchanged
snapshots，with the same frozen checkout `VIBECAD_PIP_SPEC`、no override、`PIP_NO_INDEX=1` and the corrected
`PIP_NO_BUILD_ISOLATION=0`。The already-installed exact `hatchling==1.28.0` must make that invocation take the
same one-command pip-only path；any create/remove、second failure or snapshot mismatch is a circuit breaker。
This append-only recovery branch is an internal gate correction under S3-A01 and the user's standing continuous
execution direction；it changes no product decision、source bytes、public version、external authority or engine
scope。The final ledger must preserve both the failed invocation and the effective recovery evidence。

S3-R3.16 MCPB acceptance-environment correction：the first unpacked stdio invocation used a server-only
`VIBECAD_HOME` below `/tmp/vibecad-s3-7-final.*`。That path has the world-writable `/private/tmp` ancestor and
is intentionally outside the runtime's trusted-directory contract；the background external-runtime validation
therefore failed closed before creating the server home or receipt。The packaged process still proved initialize、
the exact 20-tool projection and resource template，then returned the fixed `runtime_unavailable` envelope for
the first application call；its 780-byte DEBUG stderr contained neither the submitted canary nor exception/path
text。This is rejected as acceptance evidence because the injected home was not a valid contract environment；
it is not evidence of an engine、server or package defect。

Recovery must unpack the same checksummed MCPB into a second fresh directory，repeat the pre-uv lock/source/
allowlist proof，and give only the packaged child a new home below macOS's user-private `$TMPDIR` with verified
owner-only ancestry while binding `VIBECAD_FREECAD_ENV` to the unchanged current managed prefix。The pytest
parent retains the default epoch-2 managed home and no override。If that valid environment still races or
returns `runtime_unavailable`，it becomes a genuine packaged auto-install/swap gate red and must be closed with
a focused test/source correction before any further package or full-suite gate；otherwise only the exact
`1 passed` result counts。The first unpack and stderr remain preserved。This append-only environment correction
is covered by S3-A01 and changes no product contract or external authority。

S3-R3.16a evidence correction：the `/tmp` rejection above is caused specifically by the macOS
`/tmp -> private/tmp` symlink component。The pinned directory traversal opens every component with
`O_NOFOLLOW|O_DIRECTORY` and therefore rejects the alias before creating `server-home`；the trusted-directory
contract separately permits the fixed root/current-user-owned sticky ancestor and the earlier
“world-writable ancestor” explanation is not the operative cause。The recovery's absolute no-symlink
`$TMPDIR` path remains correct。

S3-R3.17 packaged auto-install/swap acceptance correction：a second fresh unpack with byte-exact lock/source
and a real owner-0700 no-symlink `$TMPDIR` home reproduced `runtime_unavailable` at the immediate first
`create_project` call。No child home existed when pytest killed the process，proving the call raced the
background installer before it could publish the external receipt and request the transparent supervisor
swap。This is expected product fail-closed behavior during an asynchronous bootstrap window，but the acceptance
harness incorrectly assumed readiness was synchronous。The genuine RED is retained。

The smallest correction is test-only in the already-allowed `tests/test_runtime_integration.py`：retry exactly
the same `create_project` arguments and durable `create_key` under a 300-second bound，accept only the complete
fixed `runtime_unavailable` envelope before success，and use unique JSON-RPC ids。The public contract marks this
key-replayed operation idempotent，so supervisor replay or a lost response cannot create a second project；any
other error fails immediately。Production guard、auto-install、swap and package bytes remain unchanged。GREEN
requires a third fresh unpack of the same MCPB、a new owner-private child home、exact `1 passed` and final
receipt/runtime/legacy snapshot checks。This closes an acceptance-timing defect，not a product decision。

S3-R3.18 MCPB acceptance review correction：the nominal third child root still used `$TMPDIR`'s textual
`/var/folders/...` form；on macOS `/var -> private/var` is another symlink component，so it did not satisfy
S3-R3.16a's no-alias precondition。The preserved run made 1,017 sequential exact-key attempts (JSON-RPC ids
1000..2016)，received only the exact fixed `runtime_unavailable` envelope and failed after 305.41 seconds；no
child home or receipt was created and no application effect occurred。

An independent read-only review then found three Important false-green/bound issues in the test-only R3.17
change。They are closed before another real run as follows：

1. `VIBECAD_MCPB_EXTRA_ENV_JSON` is mandatory and has exactly the child `VIBECAD_HOME` and
   `VIBECAD_FREECAD_ENV` keys；the home must be nonexistent、canonical/no-symlink、below resolved `$TMPDIR`
   through owner-private directories，and the override must be the canonical current managed prefix。The final
   external receipt must exactly bind that prefix and epoch-2 identity。
2. Before uv creates `.venv`，the unpacked fixed top-level files and every `src/vibecad` Python name/hash must
   equal the frozen checkout；the installed identity probe also requires private epoch 2，not only public 0.4.0、
   MCP 1.27.2 and an in-bundle site-packages path。
3. The 300-second retry deadline is hard：each RPC receives only `min(60s, remaining)` and the final sleep is
   capped by remaining time。The high retry-id range is disjoint from all later fixed ids。

The same production MCPB remains byte-frozen because these corrections touch only the excluded integration
test and this ledger。GREEN now requires a fourth fresh unpack，a child root passed in its resolved
`/private/var/...` form，the exact test as `1 passed`，and independent re-review。No product/source-package
change or user decision is required。

S3-R3.19 swap-latch response correction：the fourth fresh/canonical-home run proved the external receipt was
published with epoch 2 and exact current-prefix device/inode，then failed after 50.11 seconds because one retry
during the owned child lifecycle's `SWAP_PENDING` window returned a JSON-RPC response without `result`。The
fixed transport contract routes a request that cannot be admitted in that latch state to exactly
`-32005 / Server is busy.` before allocating application work；stderr independently showed the second server
initialization，confirming the transparent swap occurred。This response is therefore a safe zero-effect
transient distinct from a server envelope。

The bounded test may retry only either the complete fixed `runtime_unavailable` envelope or the exact JSON-RPC
`{"code":-32005,"message":"Server is busy."}` for its current request id。Any unknown-outcome、other RPC
error or malformed response fails immediately。The same arguments/create key and disjoint unique ids remain
mandatory。A fifth fresh canonical unpack/home run and independent re-review are required；production/package
bytes remain unchanged。

S3-R3.20 owned-worker process-runtime correction：the fifth fresh/canonical-home run did not return the exact
busy transient described above。After the external epoch-2 receipt was durable and the swapped server had
initialized，its first application request returned exactly JSON-RPC `-32603 / Tool request could not be
completed.` in 7.28 seconds。The same create-key record was already PUBLISHED with generation-zero revision and
HEAD，so this is neither a zero-effect transient nor safe retry evidence；the harness correctly keeps every
generic/internal `-32603` as a hard failure。

Read-only source audit and an isolated thread reproduction found the deterministic cause：
`OwnedStdioRunner` dispatches the first domain request on a worker；the lazy `_ApplicationSlot.get()` opens
`AgentApplication` there；`AgentApplication.open()` calls `_initialize_candidate_file_limit_runtime()`，whose
process signal policy must be initialized from the Python main thread and intentionally fails on a worker。
Direct main-thread application/handler calls therefore passed while real stdio failed。A genuine focused RED
now drives one first application call through a real owned worker after resetting the process runtime and
observes `_initialized_pid is None` / outer internal error (`1 failed in 1.45s`)；a second RED requires any
initializer failure to occur before workers or input reading begin。

The minimum production correction is limited to the already-allowed `src/vibecad/server.py`：a lazy server-only
helper imports and invokes the existing candidate-file runtime initializer as the first operation of
`_run_owned_stdio()`，before constructing or publishing `OwnedStdioRunner`。It does not run at module import、
open data/CAD、weaken the runtime guard or remove `AgentApplication.open()`'s idempotent invariant check；an
initialization failure starts no worker and reads no client bytes。Focused GREEN must prove the application
opener still runs on a worker only after the main-thread process initialization，and import/discovery/control
paths remain inert。The independent acceptance review's two Minor hardening findings are also closed test-only：
the unpacked `src` and `src/vibecad` roots must be real non-symlink directories，and the final child home must
retain current UID plus no group/other permission bits。

Because this correction changes production package bytes after the epoch-2 managed sync，advance only the
private `SERVER_PACKAGE_EPOCH` from 2 to 3 after focused review-PASS，rebuild wheel/sdist/MCPB from a new output
root，and repeat the normal pip-only managed sync。The existing managed-prefix inode、FreeCAD engine snapshot、
legacy external/binding/data baseline and user data must remain exact；the public version、surface digest and MCP
version remain unchanged。Final evidence must come from installed epoch 3 with no checkout `PYTHONPATH` and a
sixth fresh canonical unpack/home run completing exactly `1 passed`。This bounded internal correctness repair
is authorized by S3-A01 and the user's standing continuous-execution direction；it changes no product boundary
and authorizes no push、release、external spend or data deletion。

S3-R3.20a evidence correction：R3.20 incorrectly attributed the child home's PUBLISHED
`project_create_ffff...` receipt to the failed E2E request。Filesystem identity and timestamp review proves that
record was created roughly 58 seconds later by the controller's explicit main-thread diagnostic and used a
different key from the E2E random nonce。The E2E `-32603` occurred before `ApplicationDataLayout.open()` and
therefore created no project/data record；only the failing child process's in-memory application-entry event was
set。The internal error remains a hard failure because it is deterministic broken product behavior and the
generic code does not guarantee zero effect in other failure locations，not because this particular request
published an effect。All root cause、minimum fix、epoch-3 and fresh-package requirements in R3.20 remain
unchanged。

S3-R3.21 worker-runtime GREEN and epoch-3 cut：the three focused regressions now pass in 1.56 seconds，including
the real subprocess/owned-worker lazy `AgentApplication` open and the initializer-failure zero-input boundary；
the cumulative server、owned transport and RevisionStore gate passes 465 tests in 11.88 seconds。Import/discovery
purity、Ruff and diff checks pass。One independent review found only a subprocess-isolation Minor；the test now
forces auto-install off、disables bytecode writes and bounds both response reads to five seconds，then the same
three tests pass。A second read-only review reports Critical/Important/Minor `0/0/0` and authorizes the private
epoch transition。

The checkout therefore advances `SERVER_PACKAGE_EPOCH` exactly 2 → 3 and updates only exact current/previous
epoch assertions；the public 0.4.0 version、MCP 1.27.2 and public-surface digest are unchanged。The pre-sync
epoch-2 managed receipt must now classify as `SERVER_MISMATCH / UPGRADE_REQUIRED` and cannot be final evidence。
No package build、runtime sync or external tree mutation has yet occurred at this ledger point；those remain the
next bounded gates from R3.20。

S3-R3.21a in-memory evidence clarification：the failed epoch-2 worker request changed no persistent data or
external effect，but R3.20a's word “only” was too narrow。In addition to setting the process-local
application-entry event，`_ApplicationSlot` incremented its in-memory generation/failed-generation counters and
returned to `UNOPENED` after the opener failure。This does not change the no-persistent-effect finding or make a
generic `-32603` retry-safe。The second independent settled-diff review otherwise reports Critical/Important
`0/0`，with only this Minor wording correction；its focused import-purity/main-to-worker/failure-before-input/
real-worker checks and static gates pass，and epoch 3 may proceed。

S3-R3.22 external-receipt oracle correction：the sixth fresh epoch-3 MCPB run completed transparent swap、the
20-tool/resource projection、empty project、task、real FreeCAD box、auto-commit、FCStd/STEP materialization、both
resource reads and all DEBUG negative/canary checks，then failed only its final byte-level external-receipt
assertion (`1 failed in 10.39s`)。The actual owner-0600 single-link receipt had the exact epoch-3、MCP、surface、
managed-prefix device/inode and Python/FreeCAD values，but was 412-byte sorted JSON with the production
serializer's default `": "` / `", "` whitespace；the test alone expected compact separators。

This is a test-oracle defect。`status._canonical_json()` and both managed/external durable receipt writers have
always used `json.dumps(..., sort_keys=True)`；existing receipt fixtures and the preserved epoch-2/legacy
baselines use the same bytes，while readers bind exact typed fields rather than a new compact format。Changing
production would create an unnecessary receipt-byte migration after a completely valid epoch-3 package sync。
The minimum correction removes only the test's compact `separators` argument and retains exact sorted-byte
equality、all fields and file identity/mode gates。No production/package byte changes，so epoch remains 3 and the
checksummed MCPB remains valid。GREEN requires a seventh fresh unpack and fresh canonical child home completing
exactly `1 passed`，followed by independent review；the sixth child's successful durable artifacts remain
isolated in its authorized temporary test home。

S3-R3.22a serializer-default review correction：after removing compact separators，the seventh fresh
unpack/home completed the entire MCPB gate as exactly `1 passed in 6.74s`，and both managed-engine/legacy
snapshots remained exact with the default data root absent。Independent review confirms Critical/Important
`0/0` and the test-oracle diagnosis，but found one portability Minor：the assertion still passed
`ensure_ascii=False` while production `_canonical_json()` uses the default `ensure_ascii=True`。The current
ASCII managed prefix masks that byte difference；a future verified Unicode external prefix would make the test
reject production's actual canonical bytes。The exact oracle therefore removes that argument too and uses only
`json.dumps(expected_external_receipt, sort_keys=True)`。This remains test-only；epoch 3、installed package and
MCPB bytes do not change。An eighth fresh unpack/home reruns the settled assertion before final acceptance。

S3-R3.23 epoch-3 and final-gate evidence：before sync，the checkout reported epoch 3 while the exact managed
receipt/installed package remained epoch 2 and classified `SERVER_MISMATCH / UPGRADE_REQUIRED / ready=false`；
the managed prefix device/inode stayed `16777221/14014428`。The 45,772-byte engine snapshot and 44,109-byte
legacy external/binding/data snapshot exactly matched their pre-migration baselines，and the default data root
was absent。One normal `RuntimeInstaller.install()` with the reviewed checkout、no override、offline pip and
correct no-build-isolation semantics took only the existing-engine pip-sync path；its install log records the
same wheel digest below，then status became READY and the canonical managed receipt became epoch 3。No create、
remove、engine rebuild or external-tree write occurred；both snapshots remain byte-exact after sync and every
real MCPB run。

The fresh offline package root produced wheel
`32a90786b4a44d04916e20fa37d6cb89f8b3a92c4dc3691b9bc18468a694c31d` and sdist
`fe7b204b48a7e9572022b861990055e3a37f12d3b2b54851076803f0876098c5`。Checkout、wheel、sdist and installed
site-packages contain the same 73 Python files with aggregate manifest
`fc7061857e70d921fb3d7a6fea3cfd415505708a26849fda15955c7cd793e8c4`；active installed identity is VibeCAD
0.4.0、epoch 3、MCP 1.27.2、FreeCAD 1.1.0 and contains the owned-worker initializer。The independent package
audit reports Critical/Important/Minor `0/0/0`，including wheel RECORD/CRC/path safety and valid safe sdist。
The two post-build sdist differences are only this append-only ledger and the excluded R3.22 integration-test
oracle；they are non-deployable evidence drift，not a recursive requirement that a build contain its own later
hash record。A public sdist release remains out of scope and would be rebuilt from its final tag。

MCPB 2.1.2 validate/pack/unpack produced 81 files、546,682 bytes and SHA-256
`bbb1f5dd792ab9e4bc200c3699a4054a3fd6e4868023265830c0836951aead62`；all fixed files、lock
`aa8fb8d9292e8501670baa97d7cf9d83d18c9c7b8e1ec5b55d125efb12472930` and 73 Python bytes equal checkout，
with no tests/docs/runtime/cache payload。The settled eighth fresh unpack/home completes exactly
`1 passed in 7.82s` through installed epoch identity、swap、20-tool/resource discovery、project/task、real box、
auto-commit、FCStd/STEP export/read and DEBUG negative secrecy。The separate managed Agent-first matrix passes
`1 in 10.29s`。Final non-slow regression is `3827 passed, 95 deselected` in 66.88 seconds；full Ruff、51 changed
Python format checks、offline lock check、MCPB validation and diff check all pass。Final settled-diff reviews are
the only remaining pre-commit gate。

S3-R3.24 final-review documentation correction：the architecture review found one README wording Minor：
`auto_commit` publishes HEAD automatically after verified success，while only `require_review` waits for explicit
Accept；Reject leaves HEAD unchanged。The adversarial review found one contract-description Minor：resource
authority does not avoid every materialized catalog allocation。Its first `_scan_locked()` phase materializes a
hard-bounded inventory of at most 4096 strict request records and fixed-count size/identity maps to prove store
integrity/quota，returns only scalar inventory and releases those temporary objects；the subsequent authority
scan reads one request record at a time and retains only the matching PUBLISHED binding。The documentation now
states that actual two-phase bound instead of the stronger false claim。Both are documentation-only closures；
public schemas、resource behavior、epoch/package/MCPB bytes and passing test evidence do not change。Because the
settled diff changed，all final reviews must bind this corrected state before staging。

S3-R3.24a package-evidence correction：R3.24's “package/MCPB bytes do not change” statement is false。README is
an MCPB fixed file and project-description input for wheel/sdist metadata；the corrected auto-commit text makes
the R3.23 archives stale even though all 73 Python bytes、runtime behavior and private epoch remain unchanged。
Final acceptance therefore rebuilds wheel/sdist and MCPB from this corrected checkout in a new output root，
revalidates fixed README/lock/73-Python parity and reruns the exact real unpacked MCPB test with another fresh
canonical child home。No managed runtime sync or epoch 4 is needed because installed production Python is still
byte-exact epoch 3；the superseding archive hashes and final review binding are recorded after that run。

S3-R3.25 corrected-README package GREEN：the new post-R3.24 output root rebuilds wheel
`19050242ee44b06c47c2a675ae5fb65439b0fe1887c38b10f34e13562103b551` and sdist
`2fb3592d14b3d280ab6b50674013eb24be8d33185104a4de98bf17e5059a8555`。Wheel metadata contains the exact
corrected README；selected sdist README、ledger-at-build、integration test and lock equal checkout。Both archives
still contain the same 73 Python files and aggregate
`fc7061857e70d921fb3d7a6fea3cfd415505708a26849fda15955c7cd793e8c4` with epoch 3。The superseding MCPB is
81 files、546,713 bytes、SHA-256
`3966f966aac57344126e5b78ebb8e7337fc7e669e20576b72263529b57f4e6dc`；all eight fixed files including README、
lock `aa8fb8d9292e8501670baa97d7cf9d83d18c9c7b8e1ec5b55d125efb12472930` and 73 Python files equal the
corrected checkout。The ninth fresh unpack/canonical-home real gate passes exactly `1 passed in 9.82s`，and the
45,772-byte managed-engine plus 44,109-byte legacy snapshots remain exact with default data absent。These three
archive hashes supersede only R3.23's pre-README-correction archive hashes；its installed epoch-3 and test
evidence remain valid。Final reviews must bind R3.25，not the superseded bundles。

The unpacked-package gate likewise accepts only a fresh unpack whose `uv.lock` bytes and SHA-256 equal the
checkout before uv creates an environment。Its real stdio session compares the complete ordered
`tools/list` name/input/output/annotation projection，sends protocol/tool/input/resource negative canaries under
root DEBUG logging，and proves neither responses nor stderr contain the canary。An absent unpack directory is
only a safe local skip，never acceptance evidence；the final controller record must show that the explicitly
selected unpacked-package test itself completed as exactly `1 passed`，not merely that pytest exited zero。

### 4. Frozen application contracts

#### 4.1 Durable project create/get

`ProjectApi` is independent of `TaskApi` and uses the same strict schema-v1 success/error envelope。Exact
requests are:

```json
{"schema_version":1,"create_key":"project_create_<32 lowercase hex>","kind":"empty"}
```

```json
{"schema_version":1,"create_key":"project_create_<32 lowercase hex>","kind":"import_fcstd","source_path":"/absolute/input.FCStd"}
```

```json
{"schema_version":1,"project_id":"project_<32 lowercase hex>"}
```

`source_path` is required only for `import_fcstd` and forbidden for `empty`；relative paths、`..`、URI、FIFO、
socket、directory、empty/oversize file and every alias of the managed VibeCAD data root are rejected before
FreeCAD。Ingress walks from an opened `/` descriptor one component at a time with
`O_DIRECTORY|O_NOFOLLOW` and opens the final entry with `openat(..., O_NOFOLLOW|O_NONBLOCK)`。It compares every
opened ancestor `(st_dev,st_ino)` with the already pinned data-root identity, checks descriptors again after
copy, and never relies on `resolve()`、case-folded text or string-prefix containment；parent symlink、case/data
alias、root swap、final symlink/hardlink、non-ordinary/non-single-link/wrong-UID input all fail closed。The raw
path is used only by that first bounded descriptor copy；it never appears in a response、log、CAD call or durable
record。A random 32-byte per-store HMAC-SHA256 key is created once in an identity-pinned 0600 single-link exact
envelope `{schema_version:1,key_hex:<64 lowercase hex>,key_id:<64 lowercase hex>}` where `key_id` is the
domain-separated SHA-256 of the decoded key；the file and directory are fsynced before the first `RESERVED`
record and every request binds that `key_id`。Key envelope/checksum identity is verified before comparing intent,
so a bit flip cannot be misreported as different intent。The key authenticates canonical request digests without
persisting the path and is never rotated/regenerated while any record exists；missing、replaced or corrupt key
with existing records fails `store_failure`/`recovery_required`。Concurrent first creation and restart/key-loss
RED freeze that lifecycle。

The durable per-key state machine below `data/bootstrap/requests` is:

```text
RESERVED -> STAGED -> VALIDATED -> CLEANUP_REQUIRED(outcome=PUBLISHED) -> PUBLISHED
     |          |           `----> CLEANUP_REQUIRED(outcome=REJECTED)  -> REJECTED
     |          `----------------> CLEANUP_REQUIRED(outcome=REJECTED)  -> REJECTED
     `---------------- empty ------------------------------------------> PUBLISHED
```

- `RESERVED` is created and fsynced before project publication and binds the create key、HMAC canonical-intent
  digest and exactly one server-generated random project id。Same-key concurrency can therefore have only one
  project-id winner。
- `STAGED` binds the exact immutable private input name、inode identity、digest and size after the external file
  was streamed once with before/after descriptor equality。Retries never reopen the external source。The STAGED
  inode is never passed to CAD because current `validate_import` mutates its input；each attempt descriptor-copies
  it into one record-bound nonce work file and CAD may mutate only that work file。
- `VALIDATED` binds the exact immutable normalized FCStd digest/size after successful work-file validation and
  rename。Recovery uses a new read-only normalized-FCStd seam；it never calls mutating `validate_import` again、
  changes project id or selects another normalized file。
- Deterministically malformed FCStd first durably records a path-free fixed `invalid_input` failure receipt and
  then performs identity-bound cleanup。Successful cleanup reaches `REJECTED`；cleanup failure reaches
  `CLEANUP_REQUIRED(outcome=REJECTED)` and returns fixed `recovery_required` until cleanup converges, then every
  replay returns the original fixed failure without source/CAD access。Transient I/O、durability、lease or CAD
  availability failures retain a resumable preterminal phase and never masquerade as deterministic rejection。
- Generation-zero publication is exact-read back before the record stores a terminal published outcome。If
  cleanup remains, `CLEANUP_REQUIRED(outcome=PUBLISHED)` already binds the immutable successful identity and
  generation-zero projection and returns it with `cleanup_required:true`；successful cleanup reaches
  `PUBLISHED` and only changes that flag to false。Replay verifies the exact base-null Revision/manifest/model
  exists without requiring current HEAD generation zero and never rewrites create key、kind、project id or
  `generation_zero`, even after current HEAD advances。
- Same key + byte-identical canonical request replays across processes；same key + different intent conflicts；
  different keys intentionally create distinct projects。If generation-zero publication completed before a
  lost response, recovery matches only the record-owned project and exact base-null publication and converges
  the phase forward；a later HEAD is never rolled back or replaced。

Cleanup occurs only after the outcome receipt is durable。It uses each record-bound name and `(dev,ino)` plus
unlink and parent fsync；a missing exact entry is converged, while a replacement/mismatch fails closed without
touching it。Every STAGED、normalized、nonce work and cleanup remnant counts until its identity-bound cleanup is
fsynced。No final replay depends on a staging file。

The request root and its fixed lock entries are descriptor-pinned private same-UID single-link objects。A fixed
catalog quota lock serializes request admission/count/bytes and the O_EXCL `RESERVED` winner；only after a
durable known-key record exists may code create/acquire its deterministic per-key lock。The catalog lock is never
held while waiting for that per-key lock；later phase updates hold the per-key lock and take the catalog lock only
for short record/quota CAS, never across CAD。OS lock release after process death permits exact-record recovery。
Concurrent same/different intent、different-key N/N+1、crash-held lock、hardlinked lock entry and root-swap RED
freeze these semantics and ensure forged keys cannot create unbounded lock files。

Every request/phase/failure record uses an exact
`{"schema_version":1,"body":{...},"body_sha256":"<64 lowercase hex>"}` envelope；the digest is a
domain-separated SHA-256 over canonical body bytes。Raw parsing rejects duplicate or unknown keys、wrong schema、
non-safe JSON integers、depth above 64、more than 8192 nodes、keys above 256 UTF-8 bytes and strings/raw records
above 64 KiB before any identifier becomes a lookup name。Records must be 0600 same-UID single-link files on the
pinned root/device；descriptor and
entry identity are checked before and after read, then checksum is verified before any path/id lookup。Bit flip、
valid-id substitution、duplicate key or malformed record returns `integrity_failure`/`store_failure` and is never
repaired by overwrite。

Project limits are request 8 KiB、path 4096 UTF-8 bytes、source 512 MiB、2 GiB across every request record、
STAGED、normalized、nonce work and cleanup-remnant byte regardless of phase、eight active creations and 4096
durable create records。“Active” means an OS-live per-key lease plus its current work/CAD attempt, not merely a
nonterminal record；a crash-released RESERVED record therefore remains replayable under the 4096-record bound
but cannot consume one of eight live slots forever。Eight crash-abandoned RESERVED keys do not block a ninth
independent empty create, while same-key retry retains its original project id。Admission checks N/N+1 while
holding the fixed catalog lock；exhaustion creates、evicts
and overwrites nothing。Published/rejected receipts are not automatically deleted in Stage 3 because that would
break replay。

The eight live slots are eight pre-created fixed authenticated OS-lock entries, not a scan of caller-derived
locks。After durable RESERVED and per-key ownership, an attempt waits boundedly for one deterministic free slot,
holds it only through that copy/CAD/publication/cleanup attempt and releases in reverse；process death releases it
automatically。The catalog lock is never held while waiting for per-key or slot ownership。Barrier/crash RED
proves eight simultaneous live attempts bound the ninth while eight abandoned RESERVED records consume zero
slots。

All responses use the exact TaskApi envelope
`{schema_version,ok,result,error}`。A successful create result has exactly
`{schema_version,create_key,kind,cleanup_required,project_id,generation_zero:{head,revision}}`；a successful get
result has exactly `{schema_version,project_id,current:{head,revision}}`。`head` is exactly
`{schema_version,project_id,generation,revision_id,manifest_sha256}`；`revision` is exactly
`{schema_version,id,project_id,base_revision,manifest_sha256,model,artifacts}`。`model` and each ordered artifact
are exactly `{schema_version,id,name,format,sha256,size_bytes}`；`model` is null only for an empty base-null
generation-zero Revision, otherwise it has that object shape。Every base-null generation-zero `artifacts` is
exactly `[]`; later revisions return their exact ordered authoritative set。Generation-zero has
`base_revision:null`。Create
therefore returns the original generation-zero
authority while get performs HEAD → Revision → HEAD and conflicts if either complete HEAD value differs。

Project ingress errors are exactly `missing_field,unknown_field,unsupported_version,invalid_type,invalid_value,
budget_exceeded`；semantic errors are exactly `invalid_input,not_found,conflict,lease_unavailable,
resource_exhausted,runtime_unavailable,integrity_failure,cad_failure,store_failure,recovery_required,
internal_error`。The error value
is exactly `{schema_version:1,code,path,message}` with a bounded JSON-pointer path only for schema ingress and a
fixed message per code。Unsafe/path/I/O store failures map to `store_failure`；uncertain publication or cleanup
maps to `recovery_required`；unknown/wrong-process failures map to `internal_error`。No path or exception text is
reflected。`runtime_unavailable` always uses empty path and `The managed CAD runtime is not active.`。

The same single `runtime_unavailable` code/message is added to TaskApi's closed public taxonomy solely for the
pre-application guard；active-process task/direct handlers never synthesize it from an internal exception。

Public task admission is bounded at the durable store, not in `server.py` memory。`TaskRunStore` permits exactly
1024 records, keeps the existing 2 MiB per-record bound and caps all physical ordinary bytes below the task root
at 2,147,483,648, including records、journal、live temp and crash remnants。One fixed
descriptor-authenticated cross-process catalog lock covers create count/byte admission；
the only nested order is catalog lock → existing/budget-admitted task lease for both create and replacement。
Create checks N/N+1 before the first per-id lock entry can be created, then holds catalog through the task lease
and durable write；replacement first requires physical `all_current_ordinary_bytes + new_temp_bytes <=
2,147,483,648` (the old record still exists before rename), separately validates final logical
`canonical_total-old+new`, and releases in reverse。
N/N+1 exhaustion creates、evicts、overwrites、lock entries and executes CAD nothing；barrier tests cover
create/replace inversion/deadlock and 10,000 over-cap create attempts。Waits are bounded/fail closed, catalog is
never acquired while holding a task lease, and neither lock is held across CAD。
Because all mutation is catalog-serialized, at most one identity-bound write temp may exist。After acquiring the
catalog lock, a fixed checksummed mutation-intent journal is fsynced in `RESERVED` with target、old/new digests
and one unpredictable temp name before create；after create/write it is fsynced as `STAGED` with exact
`(dev,ino,uid,mode,size,sha256)`。Crash between create and STAGED may adopt only the RESERVED name when strict
private-file metadata and expected new digest match；a mismatch is never unlinked。Rename/exact readback precedes
journal clear + parent fsync。Journal/temp bytes count physically；unknown/corrupt entries or more than one
remnant fail closed/resource-exhausted, nothing is silently deleted, and no new temp is made。Crash at journal、
write、file fsync、replace、readback and directory fsync plus aggregate N/N+1 RED cover this bound。
Before any caller-derived task lease name is opened, `load` performs a bounded no-create descriptor probe of the
task record；absence linearizes as earlier `not_found`, while presence is followed by lease acquisition and an
authoritative reread。Thus 10,000 well-formed forged task ids leave the lock/data tree byte-and-inode snapshot
unchanged, whereas an existing task still receives normal CAS serialization。`artifact-export:<task_id>` is
likewise acquired only after proving that exact task record exists, so public input cannot generate unbounded
lease files。The response-loss task orphan in S3-RES-14 remains discoverability work, but is capacity-bounded。

Task capacity propagation is exact：`TaskStoreErrorCode.RESOURCE_EXHAUSTED ->
TaskCatalogErrorCode.RESOURCE_EXHAUSTED -> TaskServiceErrorCode.RESOURCE_EXHAUSTED ->
TaskServicePortErrorCode.RESOURCE_EXHAUSTED ->` public `resource_exhausted`。Create and every replacement N/N+1
test the complete mapping；capacity is never collapsed into `store_failure` or a persisted rejected task。

Authoritative `LocalRevisionStore` has its own physical quota before this surface becomes public。Across every
ordinary file below `data/projects`—published/candidate model and STEP、manifests、HEAD、journals、quota records、
temps and cleanup remnants—the hard aggregate is 17,179,869,184 bytes。Counts are at most 4096 project dirs、
8192 immutable revision dirs、1024 candidate/reservation dirs and 65,536 ordinary files；existing 512-MiB/file
and 1-GiB/revision limits remain。Unknown/corrupt extras count physically and fail closed；over-quota preexisting
stores remain read-only but every mutation returns resource exhaustion。Nothing is evicted or overwritten。

One fixed descriptor-authenticated cross-process revision-quota lock protects strict checksummed reservation
records and count/byte admission。Every current mutation already owns the exact project write lease；the only
nested order is project write lease → revision quota lock → internal project/revision descriptors, never inverse。
The global lock is held only for bounded scan/reserve/phase/accounting transitions, not across CAD。Generation-zero
publication reserves 1,074,790,400 bytes；each candidate effect reserves 2,151,677,952 bytes, covering the full
1-GiB candidate plus simultaneous immutable commit copy and metadata。Every ordinary file is either unreserved
physical usage or bound to exactly one durable reservation whose observed bytes must remain within its ceiling；
admission requires `unreserved_bytes + sum(active_reservation_ceilings) <= 17,179,869,184` before making a name。

Reservation states bind all temp/candidate identities and progress before external CAD can write。Publication
under the quota lock converts exact actual immutable bytes to unreserved usage only after readback/fsync, then
releases remaining headroom；rollback/cleanup releases a reservation only after identity-bound deletion and
parent fsync。Crash keeps the reservation charged and recoverable, preventing repeated-crash disk growth。
Before any managed FreeCAD write, the process CAD gate verifies `RLIMIT_FSIZE` support/hard ceiling, installs a
startup main-thread `SIGXFSZ` ignore-to-EFBIG policy, sets the process soft limit to the effective value below
and restores the exact previous soft limit in `finally`。The effective soft value is
`min(previous_soft,536870912)` with infinity handled explicitly；neither soft nor hard limit is ever raised, and
a preexisting stricter host limit remains authoritative。Thus a candidate FCStd/STEP file cannot exceed the existing
per-file bound before post-write accounting；unsupported/wrong-process/restore failure fails closed and an
injected 536,870,913-byte writer must produce zero publication with a fixed resource/CAD failure。
Concurrent project/candidate N/N+1、publication duplication peak、released-candidate accounting、extra-file/
root corruption and every reserve/write/rename/fsync crash are fault-injected。A quota failure occurs before
candidate/gen-zero effect and maps to public `resource_exhausted`。

That mapping is end-to-end, not an Agent-side precheck：`CandidateErrorCode.RESOURCE_EXHAUSTED` preserves the
RevisionStore code and `TaskServiceErrorCode.RESOURCE_EXHAUSTED` passes it through Agent/TaskApi。For initial
submission, TaskService obtains the project lease and quota reservation before `SUBMIT_PROGRAM`/
`START_VALIDATION` CAS；capacity failure releases the lease and leaves task generation/bytes/inode unchanged。
A later CAS conflict/store-capacity failure cleans/fsyncs the unused reservation before returning its exact
error；cleanup uncertainty returns recovery_required with the reservation still charged。Existing PROGRAM_READY
continuation similarly returns capacity failure without a new task transition。It is never converted into a
persisted rejected task or generic reconciliation error。

#### 4.2 Registry-derived direct tools

Every direct operation has this exact strict outer contract, with registry-derived nested target/argument
schemas and no additional properties：

```json
{
  "schema_version": 1,
  "task_id": "task_<32 lowercase hex>",
  "expected_generation": 0,
  "target": {},
  "arguments": {},
  "preserve": [],
  "acceptance_json": "{\"schema_version\":1,...}"
}
```

All outer fields are required。The tool name fixes the operation；the request cannot provide project、base
revision、review policy、handler/import/code/path/output or commit authority。Create operations require empty
target；`inspect_model` requires empty target and arguments；the three existing-object mutators require one full
SelectorV1 Level A and never accept `result_ref` because a one-command direct call has no prior command result。

`acceptance_json` uses a duplicate-key-rejecting raw UTF-8 decoder with a 262,144-byte limit、depth 64、8192
nodes、64 KiB strings、256-byte keys、safe JSON integers and at most 128 acceptance criteria。Its canonical
AcceptanceSpec is at most 262,144 bytes；the canonical non-acceptance request portion is at most 4096 bytes and
the total logical request is at most 266,240 bytes。All registry-derived target/argument/preserve bounds and
outer exact-key/type checks run before any durable task read。The adapter then parses the explicit
AcceptanceSpec and constructs exactly:

```text
task/base revision = the exact durable task read at expected_generation
one ModelCommand(id="direct_operation", op=<tool name>, source=model,
                 target=<validated target>, args=<validated arguments>,
                 preserve=<validated list>, depends_on=[])
explicit caller AcceptanceSpec
```

After schema validation it performs one non-effecting durable task read, requires exact
`expected_generation`、status `NEEDS_PLAN` or `NEEDS_INPUT`、no candidate and the program's exact task/base
revision。For every direct mutator, SelectorV1 `project_id` and `revision_id` must equal that task's project and
base revision。Before any effecting port call, the adapter runs pure `compile_acceptance_spec(acceptance)` and
`validate_model_program(program)` and discards their outputs；TaskService remains authoritative and repeats its
own validation。Acceptance、selector、registry、arguments、preserve、graph or program-budget failure leaves the
task record generation/bytes/inode unchanged and the effecting port spy at zero。

Only then does it perform exactly one effecting `submit_model_program` through the injected
TaskServicePort。It does not create a task、resume semantically、retry or synthesize a permissive acceptance
rule。The effective durable effect key required by S3-D03 is `(task_id, expected_generation)`；a lost-response
replay may return conflict but cannot execute a second CAD effect。A future one-call create+execute convenience
requires a separate durable request catalog and is not smuggled into `server.py`。

Every direct tool returns exactly the TaskApi success/error envelope and the same complete stored-task projection
that `submit_model_program` returns for this compiled one-command program；it uses the closed TaskApi error
taxonomy unchanged and adds no direct-only verdict、draft or artifact response shape。

S3-D03-C1 records the executable equivalence claim precisely：each direct call is byte-equivalent at the
ModelProgram contract to the corresponding explicit one-command program；a sequence of direct tasks over
successive committed heads must have equivalent final geometry/verdict semantics to the same operation sequence
where selectors and policies permit, but revision ids/history are intentionally different。Stage 3 does not
claim a hidden multi-command plan builder or identical revision ids。

#### 4.3 Verified task artifacts and MCP resources

The export request accepts no output path、directory、filename、format list、overwrite flag or URL：

```json
{
  "schema_version": 1,
  "export_key": "export_<32 lowercase hex>",
  "task_id": "task_<32 lowercase hex>",
  "expected_generation": 17,
  "revision_id": "revision_<32 lowercase hex>",
  "draft_id": null
}
```

All six fields are required and `additionalProperties:false`；export/task/revision identifiers use their exact
lowercase grammars, `draft_id` is null or `draft_<32 lowercase hex>`, and `expected_generation` is a non-bool
safe nonnegative JSON integer。The canonical outer request is at most 8 KiB with depth 16、256 nodes、4096-byte
strings and 256-byte keys。Every ingress failure returns before task/store/CAD access, so an extra path、URL or
output field is rejected rather than ignored。

- Null draft requires exact `SUCCEEDED` + committed revision。
- Non-null draft requires exact `AWAITING_USER_REVIEW` + task/draft/generation/revision/manifest/verification/
  artifact bindings。
- Failed、rejected、active or merely detached/unproven revisions are never export authority。

The exact success envelope is `{schema_version:1,ok:true,result:<value>,error:null}` where `result` has exactly
`{schema_version,export_key,materialization_id,source_kind,task_id,task_generation,project_id,revision_id,
manifest_sha256,authoritative,artifacts}`。`source_kind` is `committed` or `draft`、`authoritative` is the literal
false, and `artifacts` contains exactly ordered FCStd then STEP entries, each exactly
`{schema_version,id,name,format,sha256,size_bytes,resource_uri}`。The names are `model.FCStd` and `model.step`；
the URI is `vibecad://artifact/<materialization_id>/<artifact_id>`。No local path is returned。

The materialization suffix is a domain-separated SHA-256 over one canonical immutable source descriptor：schema
version、source kind、task id/generation、project/revision/revision-manifest and the ordered authoritative
artifact id/name/format/hash/size tuple；it never includes export key。Thus a draft later accepted as committed
receives a different identity even for byte-identical files；only an exactly identical descriptor may reuse an
exact-readback materialization。A digest collision or existing-directory descriptor mismatch fails closed and
is never overwritten。Materializations are immutable delivery copies；Revision Store remains sole authority。

Private layout：

```text
data/artifacts/
├── requests/<hash(export_key)>.json
├── materializations/materialization_<64hex>/
│   ├── model.FCStd
│   ├── model.step
│   └── manifest.json
└── .materialization_<64hex>.<nonce>.tmp/
```

Every request uses the same strict checksummed-record envelope and parser invariants frozen in §4.1。Its durable
state machine is:

```text
RESERVED -> STAGING -> COPIED -> VALIDATED -> MATERIALIZED -> PUBLISHED
    `-- deterministic conflict/invalid binding --> CLEANUP_REQUIRED -> REJECTED
```

Under the fixed authenticated artifact mutation lock and before any temporary directory or copy, `RESERVED` is
file/directory-fsynced and binds export key、full canonical request digest、the exact immutable eligibility/source
descriptor、ordered authoritative refs、deterministic materialization id、expected delivery-manifest digest and
one unpredictable record-owned temporary name/nonce。After the exact empty private directory is created and
fsynced, `STAGING` binds its `(dev,ino,uid,mode)` before any file write。Crash between mkdir and STAGING may adopt
only the RESERVED exact name when it is still an empty private safe directory；anything else is identity-bound
cleanup or fail-closed。Same key + different intent conflicts in every phase；same intent resumes only that
record-owned state。A partial STAGING copy is prefix-rehashed and continued only with exact directory/file
identity and unchanged authoritative descriptor, never forked into a second temp/materialization；mismatch enters
proven cleanup rather than overwrite。`COPIED` binds both complete private file identities/hash/sizes；
`VALIDATED` binds read-only CAD evidence；
`MATERIALIZED` binds the exact renamed/fsynced directory；`PUBLISHED` binds and exact-readbacks the frozen response。

Only deterministic ineligibility、invalid binding or stable conflict can record `REJECTED` after proven cleanup。
Transient store/I/O、lease、CAD availability or durability failures retain the exact resumable phase or return
`recovery_required`；they never masquerade as deterministic rejection。

Directory rename + directory fsync and request-record replace + request-directory fsync are separate durability
points, never described as one atomic action。After restart, a record may adopt only its exact descriptor-bound
temporary or materialization directory after manifest、inode、root and both hash/size readback；mismatch/collision
fails closed。A PUBLISHED replay exact-reads that immutable materialization and returns its frozen original
response without task/draft/CAD access, even after a draft was accepted or rejected；it never copies again。

Key-first terminal lookup briefly holds only the fixed artifact lock。For a new/nonterminal request, code releases
that lock, proves the task record exists without creating a caller-derived lease, acquires the bounded
`artifact-export:<task_id>` gate, then reacquires the artifact lock；the only nested order is task gate → artifact
lock → process CAD gate → internal store/project locks。The first export holds the task gate from its eligibility
read through copy、validation、final task+Revision reload、PUBLISHED fsync and exact readback。`AgentApplication`
accept/reject and every transition away from `AWAITING_USER_REVIEW` acquire that same task gate before TaskRun
CAS。Contention is bounded/fail-closed。A barrier paused after the final reload proves export versus Accept/Reject
has exactly one first linearization；after PUBLISHED, later task state never revokes the delivered copy。

A new descriptor-pinned RevisionStore seam opens authoritative source files no-follow and streams only into the
record-owned private temporary directory while hashing/counting；it never returns a source Path for reopen。
Source/destination must be same-UID ordinary 0600 single-link files on bound roots with distinct inodes；link、
root swap、copy-time mutation or manifest/hash/size mismatch fails closed。

Add exact `ValidatedMaterializationEvidence` containing ordered FCStd and STEP `(sha256,size_bytes)` values and
a distinct `CadExecutionPort.validate_materialization(*, fcstd: Path, step: Path) ->
ValidatedMaterializationEvidence` seam。From the descriptor-pinned private work directory it opens only the fixed
relative names, reloads/recomputes FCStd and validates the complete STEP envelope without checkpoint、save、
export、identity normalization or any byte mutation；`validate_import` is forbidden。The service then rehashes
both files and requires exact equality with returned evidence and authoritative Revision refs before rename。
Eligibility is re-read immediately before rename and PUBLISHED；change enters stable conflict/cleanup and cannot
publish。

Materialization budgets are 512 MiB per source file、1 GiB per pair、2 GiB total across every ordinary file
under the artifact store（published、temporary、manifests and requests）、eight temporary entries、4096 durable
export-request records、4096 published materializations、64 KiB per manifest/request record and 64 KiB copy
chunks。Admission holds the authenticated mutation lock and computes count/byte N/N+1 before creating a request
or temporary entry；exhaustion creates、evicts and overwrites nothing。Abandoned temporary cleanup uses a
86,400-second TTL only as an additional condition, never orphan proof。State-driven cleanup does not wait for TTL：
under the catalog lock it may delete the exact entry bound by the sole CLEANUP_REQUIRED owner only after its
failure/outcome receipt is durable and no other record/PUBLISHED descriptor shares it。Background orphan cleanup
requires TTL、no request binding、no active lease and durable creation identity。Both paths keep identity exact
through unlink/rmdir + parent fsync。A failed request cannot remove content shared by another same-descriptor
PUBLISHED request；published materializations are never silently evicted。Cleanup failure remains durable and
same-key recovery touches only record-owned entries。

`resources/templates/list` advertises exactly
`vibecad://artifact/{materialization_id}/{artifact_id}`, but `resources/read` is a VibeCAD-owned sanitized
low-level handler rather than FastMCP's default ResourceTemplate exception path。The accepted URI is exactly 141
ASCII bytes with scheme/host `vibecad://artifact/`, `materialization_<64 lowercase hex>` and
`artifact_<32 lowercase hex>` path segments and no query、fragment、percent encoding or alternate form。Before
payload allocation, under the bounded artifact catalog lock, the handler must prove at least one exact
checksummed `PUBLISHED` export request binds that materialization descriptor/manifest/artifact tuple；a plausible
directory or manifest alone is never authority。The bounded proof first performs the hard-bounded integrity/quota
inventory，which may materialize at most 4096 strict ≤64-KiB request records and fixed-count size/identity maps
before returning only scalar counts；those temporary objects are released before a second authority scan reads
one record at a time and retains only the matching PUBLISHED binding。It then descriptor-opens and revalidates
manifest、descriptor、artifact id/hash/size/inode/root and streams/hashes one no-follow fd with before/after
identity。Guessed URIs for
RESERVED/STAGING/MATERIALIZED/CLEANUP_REQUIRED/REJECTED or orphan directories return the fixed unavailable error
and zero bytes。

Raw content is at most 67,108,864 bytes and base64 text at most 89,478,488 bytes。The owned handler performs one
bounded read/hash and explicit base64 construction into `BlobResourceContents`；including raw、encoded bytes、
ASCII string、serialized message and 32 MiB bounded overhead, incremental peak allocation is at most
402,653,184 bytes。The preceding bounded inventory and payload-allocation phases do not overlap。
N/N+1 size and peak gates run against the real locked SDK；larger verified artifacts stay materialized but are
not read through MCP。Streaming/local broker delivery remains S3-RES-13。

Success is one `ReadResourceResult` containing exactly one `BlobResourceContents`：its `uri` is reconstructed
from the validated descriptor ids rather than raw-echoed, `blob` is the bounded base64 string, and `mimeType` is
exactly `application/vnd.freecad.fcstd` for FCStd or `model/step` for STEP。Only this successful canonical result
contains a URI；failures and logs never contain the submitted URI。

Resource errors are separate from export envelopes。A URI rejected by the owned prevalidator before SDK dispatch
uses fixed `-32602` / `Invalid request parameters`；a typed URI that fails the exact VibeCAD grammar uses
`-32602` with `Artifact resource identifier is invalid.`；unmatched/not-found/forged/integrity/
unsafe uses `-32002` with
`Artifact resource is unavailable.`；bootstrap guard uses `-32004` with
`The managed CAD runtime is not active.`；N/N+1 uses `-32001` with
`Artifact resource exceeds the read limit.`；an
unexpected internal failure uses `-32603` with `Artifact resource could not be read.`。The handler and SDK log
filter never reflect or log URI、path or `str(e)`；only fixed code + correlation id is allowed。

Artifact envelope ingress errors are exactly `missing_field,unknown_field,unsupported_version,invalid_type,
invalid_value,budget_exceeded`；semantic errors are exactly `invalid_input,not_found,invalid_state,conflict,
lease_unavailable,resource_exhausted,integrity_failure,cad_failure,store_failure,recovery_required,
runtime_unavailable,internal_error` with the same exact `{schema_version,code,path,message}` error shape。Internal unsafe/I/O maps to
`store_failure`；durability/cleanup maps to `recovery_required`；wrong-process/unknown maps to `internal_error`。
No exception text、URI or path is reflected。

### 5. RED waves, implementation order and objective gates

All behavior starts with a genuine focused RED hitting the intended missing contract。Syntax/setup/dependency
failure is rejected as RED evidence；the earlier nonexistent-file baseline remains separately recorded。

1. **Project ingress RED:** strict empty/import schema、key replay/conflict、all phase/receipt/checksum/key crash
   points、parent/final link/data-alias/root-swap and source mutation、fixed catalog/per-key/eight-slot barriers、
   4096/count/byte N/N+1、eight abandoned RESERVED then ninth create、create → HEAD N → same-key stable
   generation-zero replay、publish/failure-receipt before identity cleanup and double-HEAD current get。
2. **Task-store RED:** 1024/2-GiB record+temp N/N+1 under catalog→task lock order、all replace/fsync crashes、
   RESERVED/STAGED mutation journal and one proven remnant、10,000 forged gets/over-cap creates with identical
   lock tree、existing-task CAS、error propagation and inversion/deadlock barriers。
3. **Revision-quota RED:** project→revision-lock order、4096/8192/1024/65536 and 16-GiB N/N+1、gen-zero/
   candidate reservation ceilings、publication duplication peak、536,870,913-byte CAD writer、crash-held
   reservation/restart、cleanup release、preexisting-overquota read-only and corrupt/extra-entry fail-closed。
4. **Artifact RED:** exact eligibility and RESERVED/STAGING/COPIED/VALIDATED/MATERIALIZED/PUBLISHED/
   CLEANUP_REQUIRED/REJECTED phase/fsync crashes、
   descriptor-pinned source copy、link/root/mutation races、read-only FCStd/STEP evidence、final Accept/Reject
   barrier、restart/frozen replay、same-descriptor shared cleanup、request/materialization count-and-byte N/N+1、
   exact URI/error table and 64-MiB/base64/incremental-allocation N/N+1 after real FreeCAD export with all four
   runtime-cache slots populated。
5. **Direct compiler RED:** six registry-derived schemas、exact one-command projection、explicit acceptance、
   Selector-only direct target、real fractional create/move transport、invalid-before-port behavior、lost-response
   no-second-effect and direct/program equivalence。
6. **Atomic surface/transport RED:** exact CallToolResult/outputSchema、owned deterministic tools/resources、
   frame/lexical N/N+1、typed-prevalidation no-SDK-log path、eight/four/one slot barriers、duplicate active ids、
   response/RSS caps、supervisor handshake/pending bounds/non-replay create_task、unknown/extra/secret cases at
   DEBUG、legacy/manifest negatives、no Session path and lazy single-flight close/fork races。
7. **Runtime/package RED:** bootstrap guard flush-before-swap、pinned dependency/lock、old managed epoch pip-only
   sync、old external fail/fresh-owned
   migration、post-swap exact installed surface + MCP 1.27.2 and real project/direct operation rather than public
   `smoke_cad`；uninstall preserves every data child；MCPB validate/pack/unpack/frozen launch contents match。

Implementation order is TaskRun physical quota/journal → RevisionStore reservation/error propagation → project
request catalog → artifact materializer/resource → direct compiler/public metadata → bounded owned child/
supervisor transport and runtime epoch → one atomic server/manifest cutover。Prerequisite classes may exist
unregistered while the commit is in progress, but no intermediate staged/committed state may expose a mixed
old/new public surface。

Required final gates：

- focused project/artifact/API/server/revision/CadExecutionPort suites；
- cumulative task API、Task Kernel、review/restart、runtime/data/uninstall and underlying engine/tool suites；
- full `pytest`、full Ruff、changed-file format、`git diff --check`、compile/import purity and offline wheel/sdist；
- `mcpb validate`、pack、unpack allowlist、lock hash、frozen SDK 1.27.2 and real low-level initialize/tools/
  resources calls from the unpacked directory；
- installed managed FreeCAD for empty/import project、all six direct operations、program/direct equivalence、
  committed artifact and durable-draft artifact reload/hash/resource paths、Accept/Reject/restart and failure
  atomicity；
- at least two independent final read-only reviews after every finding is closed。

Acceptance evidence requires no sleeping-user presence：

| lane | exact observation | decisions | executor / user needed |
|---|---|---|---|
| deterministic fault suite | record/lock/quota/crash/TOCTOU gates above, exact frozen envelopes | S3-D01,D03,D05,D06 | controller / no |
| managed FreeCAD | old-runtime migration, empty/import, explicit program + six direct tools, committed/draft artifact reload/hash/resource, Accept/Reject/restart | S3-D01,D03,D04,D05,D06,D08 | controller / no |
| unpacked MCPB | pinned installed identity, initialize, independent exact tools/list, strict calls, resources/read, DEBUG no-secret logs, Darwin claim | S3-D02,D03,D04,D08 | controller / no |

Expected intentional test impact：old public adapter assertions in round 5–8/new-tools are superseded only at
the atomic cutover；their reusable modeling/feature/transform/assembly/render behavior remains covered by
existing module suites。Manifest platform expectation changes from Darwin+Windows to verified Darwin only。
No version bump occurs until S3-8。

### 6. Execution discipline, breakers and residuals

Capability profile remains CP-S3-20260720：approval `native-plan`；delegation `spawn-send-wait`；persistence
`repo-artifact`；process `native-session-poll` when `exec_command` yields a session, otherwise one bounded
blocking command。The selected adapter is Codex。

- `live capability declarations`: update_plan；spawn_agent/send_message/followup_task/wait_agent；apply_patch
  and workspace exec；exec_command session id plus write_stdin polling。
- `observable behavior`: this resumed controller observed native plan projection、spawn/follow-up/wait
  completion、bounded exec and polling the exact live pytest session to exit 0；named-file patch/commit behavior
  was previously observed in the same campaign。
- `environment identity`: Codex Desktop controller `/root`；workspace
  `/Users/wangtao/Documents/DevProject/vibecad`；no additional passive host identity observed。
- `public configuration`: filesystem permission profile is unrestricted and approval policy is never；these
  constrain execution but prove neither user authorization nor broader authority。

Immediate breakers：any public legacy Session/decorator survives；a direct CAD adapter or ModelProgram controls
code、handler、import/output path or commit policy；`create_project` may accept only its bounded one-shot
`source_path` under §4.1 and export never accepts a path；server synthesizes acceptance；same effect executes
twice after replay；project id changes for one create key；raw import path persists or reaches CAD；export uses
an unverified candidate、mutates task/
HEAD or aliases authoritative inode；resource read allocates past its bound；manifest claims Windows/G1/GUI；
mixed public cutover；unexpected gate red or expected-baseline mismatch；ambiguous long-process state；
out-of-allowlist write；need to change S3-D01..D08 or exceed the eighth semantic commit budget。

Residual additions：

| ID | Evidence / impact | Owner / disposition / closure |
|---|---|---|
| S3-RES-13 | MCP 1.27.2 BlobResourceContents buffers a complete base64 payload；Stage 3 owns and bounds that path at 64 MiB, so larger verified materializations cannot be delivered through this call | G1/P1；close with authenticated streaming/local broker or bounded chunk protocol plus cross-host E2E |
| S3-RES-14 | `create_task` still owns a random id without a caller request key；response loss can orphan a pre-CAD task, though it cannot duplicate CAD effects | P0-B；close with durable task-request catalog or list/recover tasks and exact replay tests |
| S3-RES-15 | TaskRun records/temps、project-create receipts、immutable RevisionStore payloads/reservations and artifact request/materialization records have finite hard quotas but no automatic retention/GC in Stage 3；valid use can exhaust them | P0-B；close all four stores with policy-governed list/archive/delete/GC that preserves HEAD、draft、replay and ancestry authority and passes crash/restart audit |
| S3-RES-16 | The persistent checksum-bound `.vibecad-runner/micromamba` intentionally fails closed when a later validated source digest differs；today recovery is an explicit runtime uninstall/reinstall rather than an in-place runner generation migration | P0-B runtime maintenance；close with a digest-versioned private runner generation or an identity-bound upgrade transaction plus interruption/replacement/uninstall gates |

Ordinary schema naming、test migration、fault-injection defects and review findings are closed autonomously
inside this packet。Only a breaker that changes product position、public trust boundary or approved scope waits
for the user。

### 7. Delivery, commit and recovery boundary

- Prewritten control commit: `docs(orchestration): issue S3-7 verified public surface packet`。
- Prewritten semantic commit: `feat(mcp): publish verified agent CAD surface`。
- Stage only exact named files；never use broad staging。Push remains not authorized / S3-RES-01。
- Before the control commit, two agents not writing this packet must independently review the seven-section
  contract、allowlist、trust boundaries、project/artifact replay、direct equivalence and gates。Before the semantic
  commit, two different read-only reviews inspect the settled diff and final gate evidence。
- Completion appends exact RED/GREEN counts、managed FreeCAD evidence、MCPB/package hashes、review closure、
  residual disposition、semantic commit hash/push state and Recovery Snapshot S3-S14。AR-1 starts only from the
  verified S3-7 semantic anchor；it may schedule P0-B/G1/P1 but cannot retroactively widen this packet。

| Evidence | Authorization / review | Anchor / push | Objective evidence | Residuals | Snapshot | Status |
|---|---|---|---|---|---|---|
| S3-E13 / 2026-07-21T16:32:00Z | S3-A01；four read-only dependency audits；independent architecture and adversarial final reviews PASS after every Critical/Important was closed | 505a224 / not authorized | corrected focused baseline 487 passed, 10 deselected；exact allowlist；packet diff-check PASS；no production mutation | S3-RES-01..06, S3-RES-09, S3-RES-11..15；S3-7 implementation pending | S3-S13 | control-ready |
| S3-E14 / 2026-07-21T16:36:41Z | S3-A01；post-control independent transport cross-check found and then re-reviewed the sole Important capacity contradiction | ec485ca / not authorized | handshake replay now has a separate exact two-frame / 4,194,304-byte budget and leaves all eight active request/id slots available；focused rereview PASS with no remaining Critical/Important | unchanged；S3-7 implementation pending | S3-S13 + this append-only correction | control-ready |
| S3-E15 / 2026-07-22T03:57:30Z | S3-A01；worker、receipt and package independent reviews Critical/Important/Minor `0/0/0` after closure；two final settled-diff reviews pending | precommit `19ae74f`；exact semantic subject pending / push not authorized | epoch-3 installed exact；managed Agent matrix PASS；fresh MCPB PASS；3827/95 full gate；package/MCPB hashes in R3.23；engine/legacy exact | S3-RES-01..06, S3-RES-09, S3-RES-11..16 unchanged | S3-S14-precommit | semantic-ready |
| S3-E16 / 2026-07-22T04:08:41Z | S3-A01；R3.24 final-review documentation findings closed；final reviews rebinding corrected archives | precommit `19ae74f`；exact semantic subject pending / push not authorized | R3.25 wheel/sdist/MCPB exact corrected README；ninth fresh MCPB PASS；3827/95 unchanged；engine/legacy exact | unchanged | S3-S14-precommit.1 | final-review |
| S3-E17 / 2026-07-22T04:13:53Z | S3-A01；architecture、delivery and adversarial final reviews each PASS with Critical/Important/Minor `0/0/0` on the settled R3.25 state | `b822fc5914fabe3d7ee4924dfcdce14e08f04ba7` / push not authorized | exact 58/58 named allowlist；3827 passed/95 deselected；managed Agent 1/10.29s；fresh MCPB 1/9.82s；Ruff/format/lock/diff PASS；R3.25 package hashes exact | S3-RES-01..06, S3-RES-09, S3-RES-11..16 unchanged；S3-7 closed | S3-S14 | completed |

### Recovery snapshot S3-S13

1. **Status:** S3-7A control packet is review-PASS and is the only worktree change；production remains at S3-6
   semantic commit `cf0f9b9` plus the user's independent research-doc commit `505a224`。
2. **Next:** commit only this control document with the prewritten subject；then execute the seven genuine RED
   waves and implementation order in §5 without exposing an intermediate mixed public surface。
3. **Authority:** S3-A01 and S3-D01..D08 permit continuous local RED/GREEN、tests、review、fix and named-file
   commits；push/PR/release/marketplace/external spend and G1/P1 remain unauthorized in this packet。
4. **Recovery:** verify branch `codex/agent-stage3`、find the exact control-commit subject、confirm no active test
   process and inspect the §2 semantic allowlist；if S3-7 semantic commit exists, use S3-S14 instead of replaying
   this control step。

### Recovery snapshot S3-S14-precommit

1. **Status:** the complete S3-7 implementation is settled in the exact §2 allowlist；managed epoch 3、real
   Agent-first and fresh MCPB gates are PASS，and no server/test process remains。The semantic commit does not yet
   exist；precommit HEAD is `19ae74f`。
2. **Next:** run two independent read-only final reviews against this exact diff/evidence，close every finding，
   stage only the named allowlist and create the local semantic commit with subject
   `feat(mcp): publish verified agent CAD surface`。Do not push。
3. **Authority:** S3-A01/S3-D01..D08 and the user's standing continuous-execution direction authorize these local
   gates、review、correction and named-file commits；push/PR/release/marketplace/external spend/data deletion remain
   unauthorized。
4. **Recovery:** verify branch、HEAD、clean process table、epoch-3 current receipt and the three R3.23 hashes；if
   the semantic subject exists，do not repeat runtime sync or real MCPB effects。Resolve the semantic hash from
   Git，then append the final S3-S14 review/hash record in a docs-only completion commit。

### Recovery snapshot S3-S14-precommit.1

1. **Status:** the complete S3-7 implementation remains settled in the exact §2 allowlist；the corrected README
   and resource description are included in the R3.25 wheel、sdist and MCPB，the ninth fresh MCPB gate is PASS，
   and managed epoch 3 plus the 3827/95 full gate remain valid。The semantic commit does not yet exist；precommit
   HEAD is `19ae74f`。
2. **Next:** finish at least two independent read-only reviews bound to this exact R3.25 diff and archive set，
   close every finding，then stage only the 58 named allowlist entries and create the local semantic commit with
   subject `feat(mcp): publish verified agent CAD surface`。Do not push。
3. **Authority:** S3-A01/S3-D01..D08 and the user's standing continuous-execution direction authorize these local
   gates、review、correction and named-file commits；push/PR/release/marketplace/external spend/data deletion remain
   unauthorized。
4. **Recovery:** verify branch、HEAD、clean process table、epoch-3 current receipt and R3.25 wheel
   `19050242ee44b06c47c2a675ae5fb65439b0fe1887c38b10f34e13562103b551`、sdist
   `2fb3592d14b3d280ab6b50674013eb24be8d33185104a4de98bf17e5059a8555` and MCPB
   `3966f966aac57344126e5b78ebb8e7337fc7e669e20576b72263529b57f4e6dc`；if the semantic subject exists，do not
   repeat runtime sync or real MCPB effects。Resolve the semantic hash from Git，then append the final S3-S14
   review/hash record in a docs-only completion commit。

### Recovery snapshot S3-S14

1. **Completed:** S3-7 is committed locally as
   `b822fc5914fabe3d7ee4924dfcdce14e08f04ba7` (`feat(mcp): publish verified agent CAD surface`) with exactly
   58 named allowlist entries。Managed epoch 3、the 20-tool Agent-first public surface、verified FCStd/STEP
   delivery、3827/95 full regression and the R3.25 wheel/sdist/MCPB evidence are settled；three independent final
   reviews report Critical/Important/Minor `0/0/0`。
2. **Next:** commit only this completion-ledger update with subject
   `docs(orchestration): record S3-7 semantic completion`，then begin AR-1 from the clean S3-7 semantic anchor。
   AR-1 must reconcile the architecture documents and freeze the S3-8 skill/version/real-E2E packet before any
   P0-B、G1、P1 or P2 implementation。
3. **Authority:** S3-A01/S3-D01..D08 and the user's standing continuous-execution direction authorize the local
   completion record、AR-1 read-only/product-architecture review and preparation of the S3-8 control packet。
   Push/PR/release/marketplace、external model/API spend、data deletion and later-stage implementation remain
   unauthorized until their applicable boundary is reached。
4. **Recovery:** verify branch `codex/agent-stage3` contains semantic commit
   `b822fc5914fabe3d7ee4924dfcdce14e08f04ba7` and this docs-only completion subject；the worktree must be clean and
   no server/test process active。Do not rerun epoch migration or the R3.25 package effects merely to resume。
   Reuse the recorded wheel `19050242ee44b06c47c2a675ae5fb65439b0fe1887c38b10f34e13562103b551`、
   sdist `2fb3592d14b3d280ab6b50674013eb24be8d33185104a4de98bf17e5059a8555` and MCPB
   `3966f966aac57344126e5b78ebb8e7337fc7e669e20576b72263529b57f4e6dc` as S3-7 evidence，and continue at AR-1。

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
