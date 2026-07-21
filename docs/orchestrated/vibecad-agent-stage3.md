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
