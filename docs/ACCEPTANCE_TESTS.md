# VibeCAD 0.9.0 发布验收测试

本清单验证当前 Agent-first 产品：持久化 Project/Task/Revision/Draft/Artifact/Release、38 个公开工具、
direct operation 与 ModelProgram 的统一 Task Kernel，以及可验证的 FCStd/STEP/PDF/ZIP 资源交付。

放行结论必须区分：

- **protocol/package host-ready**：本地 raw/typed MCP、Skill 包、受管 FreeCAD 与打包后会话全部通过；
- **host-verified**：真实第二宿主使用外部模型执行同一任务并通过。

0.9.0 保留完整 protocol/package gate，并要求 Codex、Claude、WorkBuddy 分别执行同一发布包 smoke。
WorkBuddy 的既有 GLM-5.2/GLM-5V-Turbo 证据只增加其专属兼容路径覆盖，不能替代另外两个宿主。
0.8.0 已交付的 Guided Photo V3 继续作为回归门：它只覆盖通过拍摄/尺度/完整性门且独立确认关键
尺寸的受限机械零件，Task 前安全停止和可编辑参数探针与正例同样必须通过。0.9.0 的新增门是
S41 派生参数联动与 S42 语义 Fillet/Chamfer，包括重开验证、参数修改和原子失败回滚。

MR0-C01..C04 已交付并验收内部 multi-runtime foundation；它继续作为 0.9.0 的架构回归，不扩大
当前 FreeCAD-only 产品支持或公共 runtime schema。

MR1-P00 已冻结
[`Revision durable-v2 迁移合同`](orchestrated/vibecad-durable-v2.md)，但没有实现 Revision v2
reader/writer、
inventory、activation 或 migration。其 future acceptance 与当前 0.9.0 host-ready gate 分开，
不得把文档合同或 managed-checkout 自己的 schema v2 写成 Revision durable-v2 PASS。

## 1. 冻结产品口径

### 1.1 公开工具

运行时 `tools/list` 与 MCPB manifest 必须同序公开以下 38 个唯一名称：

| 类别 | 工具 |
|---|---|
| 运行时 | `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime` |
| 能力 | `get_capabilities` |
| 项目与版本 | `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions`, `revert_project` |
| 任务 | `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task`, `cancel_task` |
| 审核 | `accept_draft`, `reject_draft` |
| 交付 | `get_artifact_manifest`, `export_task_artifacts`, `create_release`, `get_release`, `approve_release` |
| 图片重建生命周期 | `create_reconstruction`, `get_reconstruction`, `run_reconstruction`, `answer_reconstruction`, `adopt_reconstruction`, `reject_reconstruction`, `delete_reconstruction` |
| direct CAD | `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part` |

每个工具必须有非空、单行、有界说明，严格输入 schema 和 annotations。MCP discovery 不重复可选
output schema，但服务端内部必须继续使用完整冻结 output schema 验证每次结果。

### 1.2 一个写入权威

direct operation 必须编译为 ModelCommand/ModelProgram 并调用同一个 Task API。任何写操作都必须经历：

```text
immutable base revision
  → project lease
  → isolated candidate checkout
  → FreeCAD execution
  → observations + deterministic verification
  → auto_commit 或 durable review
  → commit / reject / rollback / recovery
```

不得存在 public direct handler 原地改写用户文件、绕过 revision/verifier 或另建状态机。

application-owned parent FreeCAD compatibility adapter 可以接收现有 `LocalRevisionStore` 与 lease
capability，但只用于 Kernel 已分配、budget-bounded 的 candidate/revision validation、checkpoint、
export 和 evidence；它不得建立独立 Task store、Accept/Reject 或 commit/HEAD authority。child
Worker、Workbench client 和 reconstruction/simulation Provider 不得接收任何 store/lease object、
daemon credential 或提交能力。Provider 只能读取 sealed Revision/immutable Artifact 并返回 immutable
artifact/proposal；设计采纳必须新建 reviewed CAD Task。

### 1.3 当前支持边界

- 项目可以是 `empty`，或导入非空、对象全为 `Part::Box` / `Part::Cylinder` 的
  `import_fcstd` envelope；
- `empty` 项目可通过严格 ParametricDesignIR 创建全约束 Sketcher 与 Pad/Pocket/Hole/Revolve，
  并使用派生参数、原生 slot 和语义 Fillet/Chamfer；
- 宿主多模态路径支持冻结的单视图、多视图和 Guided Photo V3 包络；尺度、遮挡、冲突或隐藏结构
  不足时必须在 Task 前澄清或安全停止；
- CAD Worker 验证 headless profile；FreeCAD Workbench Alpha 另有 interactive profile；
- CAD candidate 交付 FCStd 与 STEP；已接受 Revision 还能生成 PDF、BOM、manifest、验证报告和
  摘要批准的 Release ZIP；
- FreeCAD 是唯一连接的 CAD adapter；fake runtime/adapter 只能作为 conformance fixture，不能进入
  capability/product support 声明；
- MR0 内部 runtime/artifact/selector contract 不改变 38 tools、六个 direct operations 或公开 `SelectorV1`；
  durable Revision/Candidate/manifest/recovery writer 仍固定 FCStd/STEP v1；MR1-P00 只冻结 future
  migration contract，不创建 v2 byte；
- active cancellation 由受管、可终止 FreeCAD Worker 和持久化 `reconcile` 路径收口；空闲取消仍不得启动
  CAD/runtime 或取得 project write lease；
- G1 FreeCAD Qt Workbench Alpha 支持 HEAD/draft preview、verdict、精确 object/feature selector 与
  Accept/Reject；默认路径是受管 FreeCAD，user-FreeCAD 只覆盖一个指纹绑定的 macOS FreeCAD 1.1.3
  本机试点；
- 当前不支持通用 FCStd、STEP/STL import、任意 Python/FreeCAD code、通用 user-FreeCAD 兼容性、
  交互式 face/edge selector、视频重建或 simulation；普通照片只支持 Guided Photo V3 的冻结包络，
  不宣称纯照片精密测量、隐藏结构恢复或普适逆向工程。

机械详细设计、预检与仿真的
[`调研报告`](MECHANICAL_DESIGN_VALIDATION_RESEARCH.md)不提供任何 acceptance PASS，也不把其中的
P1/P1.5/P2 建议变为当前承诺。

### 1.4 MR0 foundation conformance（C01..C04 已验收，独立于 0.6.1 放行）

| 合同 | C01..C04 已接受的证据 | 当前状态 | 不能据此宣称 |
|---|---|---|---|
| generic lifecycle | immutable runtime identity/version/capability；Task-correlated sealed invocation；budget/deadline；start/status/cancel/health/reconcile；immutable artifact/provenance/diagnostics/evidence；deterministic fake 实际执行 lifecycle，再由 caller 提供 observed result transcript；common layer 不导入 CAD/FreeCAD/Qt/FEA | 内部 contract/conformance PASS | 通用 CAD command、generic result retrieval、仿真或重建 schema |
| CAD domain | capability planner 在 mutation 前精确选择 native、disclosed mapping、explicit approximation、unsupported 或 namespaced extension | 内部 planning contract PASS | 所有 runtime 语义等价或自动降级 |
| registry/conformance | 两个 deterministic fake CAD identity 可独立 register/plan/route；未声明 capability fail closed；generic fake 另行证明 execute/cancel/reconcile lifecycle | 内部 fixture/conformance PASS | 第二 CAD adapter、engine 或产品支持 |
| FreeCAD adapter | default composition 只注册并选择 FreeCAD；现有 lifecycle、error、source safety、FCStd/STEP、cancel/recovery compatibility 与真实 managed gates 保持通过 | FreeCAD-only PASS | 新 operation、auto-discovery、通用外部 FreeCAD 支持或第二 adapter |
| authority negative | parent compatibility adapter 的私有 store/lease capability 只限 bounded validation/checkpoint/export/evidence；control/adapter public surface 的 Task/Accept/Reject/commit/HEAD-like authority fail closed；child Worker/provider/Workbench 无提交能力 | 结构与 composition boundary PASS；不是 OS sandbox | 第二 scheduler、提交路径或恶意 provider 隔离 |
| artifact/selector | runtime-qualified profile 与 concrete artifact 的 runtime/kind/media 精确匹配；semantic `SelectorV1` 始终权威，native locator 仅为可选 runtime/revision-qualified evidence；真实 byte/digest 仍由 domain verifier 核验 | 内部 qualification PASS | 公开 runtime schema、SelectorV1 wire 变化、face/edge 支持或 conformance 已验证 artifact bytes |
| D14 durable split | C01..C04 不改变 `RevisionRef`、Candidate/store/manifest/recovery schema；FreeCAD 仍持久化固定 `model.FCStd`/`model.step` | MR0 preservation PASS；MR1-P00 contract frozen，P01..activation OPEN | Revision/Candidate/manifest/store 已泛化、v2 已实现或第二 native format 可持久化 |

上述 accepted evidence 落在 C04 commit
`7c98e36c77ea748b2c33274d00d0f895ef3d8102`，其 exact conformance suite 为
`97 passed`。实现与测试入口见
[`CAD_RUNTIME_ADAPTER_GUIDE.md`](CAD_RUNTIME_ADAPTER_GUIDE.md)。本表不是 0.6.1 release matrix；
MR0 conformance 不能关闭 §7 的真实第二宿主 residual，也不能产生 tag、release、G1 或 host-verified
结论。

### 1.5 MR1 durable-v2 migration acceptance（合同已冻结，实现未开始）

本节投影
[`Revision durable-v2 迁移合同`](orchestrated/vibecad-durable-v2.md) 的 future gate；所有结果当前均为
NOT RUN，不能加入 §2 的 0.6.1 放行 PASS：

| ID | Future gate | 必须证明 | 当前状态 |
|---|---|---|---|
| MR1-G00 | P00 文档一致性 | v1 immutable、absent-profile exact legacy FreeCAD FCStd/STEP、reader-before-writer、mixed ancestry、downgrade fail-closed、full-root preflight、backup/restore/rollback 与 readiness fence 在 canonical docs 一致；无 v2 implementation claim | contract frozen |
| MR1-G01 | byte-exact v1 corpus | generation zero、sealed Revision/HEAD/journal、Task/Draft、artifact/bootstrap、checkout v1/v2 record 等 indexed fixture encoding 的 byte/hash/size 固定；normal test 无 update-golden | NOT RUN |
| MR1-G02 | strict codec seam | reader strict dispatch v1，unknown v2/profile/hybrid fail closed；reader/writer 与所有 v1 corpus byte 保持不变 | NOT RUN |
| MR1-G03 | full-root observational inventory | 从 `data/` root identity 开始观察 `locks/`，再扫描 `projects/`、`tasks/`、`bootstrap/`、`checkouts/`、`artifacts/`；mutation-negative、path-free、bounded；只输出 `structurally_ready` 与 blocker/token | NOT RUN |
| MR1-READ | future dual-reader | exact v1 + v2 reader 已独立 gated/deployed，writer 仍 byte-exact v1；unknown/profile/hybrid fail closed；read/list/compare/export/preview mutation-negative | NOT AUTHORIZED |
| MR1-ACT | later fenced activation | 在 MR1-READ 后，daemon quiesced、approved global writer/maintenance fence、第二次 full scan、capacity、verified backup/restore 后才可输出 `activation_ready` 并切换 new-write-v2 | NOT AUTHORIZED |
| MR1-BETA | shared non-disposable beta | v1 byte identity、mixed v1→v2 ancestry、restart/reconcile、G1 opaque preview/review、artifact URI、downgrade fail-closed、interrupted activation/restore/rollback 全部在 exact integrated build 通过 | NOT RUN |

MR1-G03 必须复用 Application 已 pin 的 layout 和 future non-creating snapshot hooks；调用会创建缺失
layout child 的 opener，或取得可能首次创建 persistent lock file 的 catalog/quota/resource lease，
都使 no-mutation gate 失败。`<64hex>.lock` 在 release 后仍可存在，presence 不是 active lease 证据；
只有 future quiescence + global fence 能证明 `activation_ready` 所需的 writer exclusion。
同一 live tree 的 before/after gate 必须保持 mtime/ctime/device/inode identity 不变；isolated
backup/restore 则比较 logical path、kind/mode/uid/size/hash/record/reference closure，并要求 restored
file 与 live/backup inode 独立，不能错误要求 restore 后 ctime/inode 相同。

缺 profile 的 record 只有在 strict Revision v1 FCStd/STEP invariants 全部成立时，才映射到固定 legacy
FreeCAD profile。Future durable profile 必须是 versioned CAD-domain value；不得通过序列化内部
`CadArtifactProfile`、`RuntimeDescriptor`、capability/metadata 或 adapter state 构造。Managed checkout
open/tombstone 当前 reader 接受自己的 v1/v2 且 writer 写自己的 v2，只证明这个 record family 的
兼容顺序，不关闭任何 MR1 gate。

任何历史 v1 byte 改变、eager/in-place rewrite、unknown inventory entry、data/capacity loss、
未独立验证的 backup、ambiguous restore/rollback、旧 writer 在 v2 root 上 mutation，或把
`structurally_ready` 提升为 `activation_ready` 都是 release breaker。G1 只可在明确 disposable 或
已独立 export/verify 的 v1 data 上称 alpha；承诺用户项目升级存续前必须通过 `MR1-BETA`。

## 2. 放行总表

| ID | Gate | 通过标准 | 结果 |
|---|---|---|---|
| G01 | 版本与协议身份 | tag/source/pyproject/manifest/FreeCAD package/lock = 0.9.0；server epoch = 4；MCP/FreeCAD/Python pin 不漂移 | ☐ |
| G02 | 公开面 | 精确 38 个唯一工具；说明与 manifest 完全一致；固定 discovery frame ≤ 32,768 bytes | ☐ |
| G03 | 内部校验 | discovery 不发 output schema，但正常与异常 CallToolResult 仍受冻结 output validator 约束 | ☐ |
| G04 | 命名空间 | direct 与稳定名称碰撞、direct 重名都在 schema/dispatch/effect 前 fail closed | ☐ |
| G05 | Skill | canonical Skill 通过校验；示例、恢复表和限制与 live schema 一致 | ☐ |
| G06 | 分发 | sdist/MCPB/Skill zip 含同一 Skill tree；wheel/installed Python 不含 Skill | ☐ |
| G07 | 普通测试 | 全量 non-slow pytest、Ruff、changed-Python format/pycompile、offline lock、diff check 通过 | ☐ |
| G08 | 受管 FreeCAD | Darwin slow matrix 通过；安装只同步 0.9.0/epoch 4，不重建现有引擎 | ☐ |
| G09 | Agent E2E | empty/import、direct/program、review/cancel/restart/conflict、artifact/resource 与负例通过 | ☐ |
| G10 | 数据保护 | runtime uninstall 与持久取消不删除/改写项目数据；执行和导出不污染源文件或暴露任意路径 | ☐ |
| G11 | 打包后会话 | 从全新解包 MCPB 启动并复跑 discovery、真实 CAD 与资源读取 | ☐ |
| G12 | 独立审查 | 至少两路 settled-diff review；所有 Critical/Important 关闭 | ☐ |
| G13 | Workbench Alpha | managed Workbench 完成 preview/selector/review；指纹绑定 user-FreeCAD 试点经薄 bridge 完成同一非空 Reject 或 Accept 流程 | ☐ |
| G14 | 三宿主发布包 | Codex、Claude、WorkBuddy 分别完成恢复、建模和 ResourceLink/资源读取；WorkBuddy 另过兼容适配门 | ☐ |
| G15 | Guided Photo V3 | 三个公开正例各形成可编辑 review draft；缺尺寸和多物体负例均在 Task 前停止；真实 FreeCAD 证明 DoF、BRep、单实体和参数修改 | ☐ |
| G16 | 参数联动与边处理 | 派生仿射表达式和语义 Fillet/Chamfer 通过 focused、完整回归、重开编辑及真实 FreeCAD runtime 门；歧义与无效几何原子失败 | ☐ |

## 3. 自动化与打包 Gate

### G01：身份一致性

检查：

1. tag、`src/vibecad/__init__.py`、`pyproject.toml`、`manifest.json`、FreeCAD `package.xml`、
   `uv.lock` 与 wheel/sdist metadata 都是 `0.9.0`；
2. runtime receipt、status 与 server handshake 使用同一 VibeCAD 版本；
3. private server epoch 为 4，runtime receipt 的 public-surface digest 绑定 description、input/output
   enforcement schema 与 annotations；当前 SHA-256 为
   `cb336a972554881bdf400a8699d8004cceeac877b2e52afb0659c78fb37f701d`；
4. MCP 保持 1.27.2、Python 保持 3.12、FreeCAD 保持 1.1.0；
5. `uv lock --offline` 不产生非预期差异。

任何一个身份不一致都阻断放行。

### G02：38-tool discovery

对固定 JSON-RPC request id `1` 获取完整 `tools/list`，用 sorted keys、compact separators、
`ensure_ascii=false` 序列化，并计入末尾 LF。预期：

- 名称与 §1.1 精确同序，唯一且无额外工具；
- 每项 description 非空、单行、可打印且在长度预算内；
- 每项包含 input schema 和 annotations；
- discovery 项不包含 optional output schema；
- 完整 UTF-8 tools/list frame 不超过 32,768 bytes；
- `manifest.json` 的 `(name, description)` 与 PublicToolSpec 逐项完全一致。

0.9.0 计入 JSON-RPC envelope 和末尾 LF 的完整固定 frame 必须低于 32,768-byte
上限；其 contract digest 和完整 frame 必须由当前 public-surface contract 与自动化 gate 重新计算、
逐字节核对，不能沿用旧版本记录。
`cancel_task` 必须位于 `resume_task` 后、`accept_draft` 前，description 固定为
`请求取消指定任务并返回持久化状态`，输入精确为 `schema_version`、`task_id`、
`expected_generation`，annotations 精确为 `(false, true, true, false)`。

再注入一个 direct operation，分别尝试命名为稳定控制名和已有 direct 名。两次都必须在 public
projection 阶段以固定内部错误拒绝，不能产生重复 discovery、路由歧义或任何副作用。

### G03：服务端结果校验

即使 discovery 省略 output schema，也必须验证：

- 正常结果同时返回 canonical JSON text 与完全匹配的 `structuredContent`；
- handler 返回缺字段、额外字段、错误类型或超预算结果时，服务端返回固定 internal error；
- 失败 envelope 同样经过 schema 校验；
- 直接工具与稳定 facade 走同一结果封装边界。

### G04：Skill 与分发矩阵

canonical source 是 `skills/vibecad-agent/`。执行 Skill validator，并检查：

- frontmatter 只有 `name` 与 `description`，`agents/openai.yaml` 可解析；
- 正文列出精确 38 个工具（包括七个 reconstruction 工具），先 `get_capabilities`，包含 project/task/review/artifact/release/visual 流程；
- direct/ModelProgram、SelectorV1、AcceptanceSpec、ResultRef、generation 与恢复表和实际 schema 一致；
- cancellation 段明确空闲取消的同请求重放、review 使用 reject、active cancellation 的持久化
  `reconcile` 语义，以及 `notifications/cancelled` 的 transport-only 语义；当返回
  `next_action=reconcile` 时，只能以当前 generation 调一次 `resume_task`，不得猜测终态；
- 明确要求 unknown-outcome `create_task` 用相同 create key 与不可变意图重放，并禁止换 key
  恢复、已退役 endpoint、任意 code 与未支持能力；
- 安装路径覆盖 Codex 当前测试路径、Codex 已发布 user/repo 路径和 Claude Code user/repo 路径；
- MCPB 内存在 Skill 不被描述成已经 activation，文档要求 restart/reload。

从干净输出目录分别构建 wheel、sdist、MCPB 与 `vibecad-agent-skill-0.9.0.zip`。预期矩阵：

| 渠道 | 包含 Skill | 规则 |
|---|---:|---|
| repository source | 是 | canonical tree |
| sdist | 是 | relative files 与 source byte-identical |
| MCPB | 是 | 归档用途，不自动 activation |
| standalone Skill zip | 是 | 唯一顶层目录为 `vibecad-agent/` |
| wheel | 否 | server-only |
| installed Python | 否 | server-only |

记录每棵 Skill tree 与 standalone zip 的 SHA-256。检查 archive path、symlink、RECORD，确保测试、docs、
cache、runtime 和非预期文件没有混入；MCPB 中 README 和 Skill 是明确例外。

### G05：Release workflow

发布工作流必须在 PyPI publish 与 GitHub Release 之前完成：

1. Ruff 与 non-slow pytest；
2. wheel/sdist/MCPB/Skill zip 构建和包审计；
3. macOS managed-runtime Agent slow matrix；
4. 上传一次已经过 gate 的 archive。

publisher 只能下载并发布已 gate 的 archive，不得重建。GitHub Release 同时附上 `VibeCAD.mcpb` 与
`vibecad-agent-skill-0.9.0.zip`，且仍需要明确的 environment/tag 授权；`VCAD-A09` 已提供本次授权。

## 4. 真实受管 FreeCAD Agent Matrix

以下场景使用真实受管 FreeCAD，不得用 fake engine 代替。除明确要求 restart 的场景，每条都记录
project id、base/head revision、task id、generation、next_action、draft id、verdict id、artifact id 与
关键 hash。

### E01：能力发现与空项目

1. runtime ready 后调用 `get_capabilities(schema_version=1)`；
2. 验证精确六个 public direct operation，profile 为当前支持的 headless，FreeCAD/version/budget 与
   registry 一致；
3. 用新 `create_key` 调 `create_project(kind=empty)`；
4. 用同一 create key 重放一次，必须幂等返回同一项目；
5. `get_project` 返回 revision zero，源项目不被就地修改。

失败标准：根据工具数猜 operation、同一 create key 生成两个项目、revision zero 缺失或项目数据只在
进程内存在。

### E02：direct + auto_commit

1. 在 E01 项目上创建 `review_policy=auto_commit` 的任务；
2. `get_task` 后，用返回 generation 调 `create_box` 创建 60 × 40 × 10 mm 盒子；
3. AcceptanceSpec 至少验证 dimensions、bbox、volume、solid count、valid shape 与 reload；
4. 任务成功后 `get_project` 的 HEAD 指向新 revision；
5. `inspect_model` 返回 revision-bound object/feature facts。

预期体积 24,000 mm³、bbox 60 × 40 × 10 mm、一个有效 solid。任何“调用成功但验收失败仍提交”均为
阻断缺陷。

### E03：多步骤 ModelProgram

从独立 base 创建任务，用 `submit_model_program` 提交至少两个受支持命令，并通过 ResultRef 在后续
命令中引用前序结果，不猜 FreeCAD label。预期：

- program schema、命令数、JSON bytes、operation budget 均被执行前校验；
- 任一步失败时整个候选不发布，不留下半成品；
- 所有 AcceptanceSpec 通过后才按 review policy 进入 commit 或 draft；
- sealed observations、step records 和最终 revision 在重启后可读取。

### E04：direct 与 ModelProgram 等价

从内容相同的两个 base revision 构造相同 operation 序列：一边使用逐步 direct operation，一边提交
一个 ModelProgram，并使用相同 AcceptanceSpec 与 commit policy。比较：

- 最终几何 facts、参数、placement、bbox、volume、solid count、validity；
- verifier outcome 与 artifact 内容；
- task/draft/verdict/artifact envelope 的语义字段。

除明确的 task/revision/id、时间和 policy 差异外，结果必须等价。若 direct 绕过 program validator、
candidate 或 verifier，立即阻断。

### E05：require_review、Reject 与 Accept

#### Reject 分支

1. 创建 `require_review` 任务并生成验证通过的 draft；
2. 确认项目 lease 已释放、HEAD 未变化；
3. 重启 server，`get_task` 仍返回同一 immutable draft/verdict；
4. 用当前 id/generation 调 `reject_draft`；
5. 确认 task/draft 记录为 rejected，HEAD 仍未变化。

#### Accept 分支

1. 创建另一个 `require_review` 任务并生成 draft；
2. 重启后展示 exact draft/verdict/evidence；
3. 用当前 id/generation 调 `accept_draft`；
4. Kernel 重新取得 lease、重新验证，并用 base revision 对 HEAD 做 CAS；
5. Accept 成功后 HEAD 指向 draft revision，任务进入成功终态。

Accept/Reject 用错 draft id、task id 或 generation 必须 fail closed。

### E06：stale generation 与 stale base

- 用旧 generation 调任一写操作：返回 conflict，不产生新 candidate 或副作用；随后 `get_task` 获取
  最新 generation/next_action；
- 先创建 draft，再用另一任务推进同一项目 HEAD；接受旧 draft 时返回 stale-base conflict，不发布
  旧 draft；
- 重启后重复检查，冲突事实必须持久化且 HEAD 唯一。

### E06-R：真实 FreeCAD verified forward revert

在已有至少两个 committed revision 的同一 ancestry 上：

1. 选择一个非 HEAD 的完整历史 revision，并读取当前权威 HEAD；
2. 用唯一 `revert_key`、该 `source_revision` 和精确 `expected_head` 调 `revert_project`；
3. 真实 FreeCAD Worker 必须基于当前 HEAD 生成经过 reload/STEP/verifier 的 immutable draft，准备阶段
   HEAD 不变；
4. `get_task` 必须读回同一 task generation 与 draft；导出的 FCStd/STEP 真实 reload 后，几何必须与
   历史 source revision 一致；
5. Accept 后生成新的 forward revision，其 `base_revision` 是调用时 HEAD、id 不等于 source revision，
   新 HEAD 指向该 revision；Reject 分支由确定性测试证明 HEAD 不变。

Darwin `public-agent-matrix` release target 必须执行这条路径，不能只以 fake coordinator、API schema 或
38-tool discovery 代替真实 CAD 证据。相同 key 与相同意图重放返回同一任务；变更 source 或
expected-head 必须 conflict。

### E07：受支持 FCStd import

准备三类真实 FCStd：

1. 非空且所有对象均为 `Part::Box` / `Part::Cylinder`；
2. 可由 FreeCAD 正常打开、但不含任何对象的空 FCStd 文档；
3. 至少包含一个其他类型，或与 Box/Cylinder 混合。

仅第 1 类可以 `create_project(kind=import_fcstd)` 成功，并生成可 reload 的 revision zero；随后
`inspect_model`、参数修改、移动/旋转与导出可正常工作。第 2、3 类必须在导入边界被固定
`invalid_input` 错误拒绝，不创建可见项目、不修改源 FCStd，也不尝试任意 Python/FreeCAD code。

### E08：FCStd/STEP ResourceLink

对 E02 committed revision 和一个符合资格的 draft 先调用 `get_artifact_manifest`。尚无 delivery 时
必须返回 `materialized=false`、零 ResourceLink，且 artifact 目录、task、revision 与 CAD 状态完全
不变；再调用 `export_task_artifacts`。每个成功结果都必须在 canonical text/structured envelope 后
恰好追加两个 ResourceLink：

| format | MIME | 断言 |
|---|---|---|
| `fcstd` | `application/vnd.freecad.fcstd` | URI/name/size 与 validated result 完全一致 |
| `step` | `model/step` | URI/name/size 与 validated result 完全一致 |

对每个 URI 调 `resources/read`，核对 format、byte size 与 SHA-256，并真实 reload FCStd、解析 STEP。
同一 `export_key` 重放必须幂等；同一历史 committed revision 在项目 HEAD 前进后仍可读取。
再次调用 `get_artifact_manifest` 必须只读验证 task/revision/verification/delivery 绑定，返回
`materialized=true`、同一 delivery-manifest digest 和同一两个 ResourceLink，不得再次运行 CAD、
复制或物化。

负例：

- failed/ineligible task、错误 draft/revision、stale generation 不返回 ResourceLink；
- 未物化的 `get_artifact_manifest`、`ping`、`get_task` 与 direct operation 等其他结果不返回 ResourceLink；
- 伪造 artifact id、URI traversal、未知 format、超大读取或任意本地路径都被拒绝；
- structured result 声称的 format、URI、name 或 size 不匹配时，服务端固定 internal error，不能制造链接。

### E09：任务恢复表

为所有实际 `next_action` 分支做状态注入或真实中断：

| 返回值 | 唯一允许动作 |
|---|---|
| `request_plan` | `get_task` 一次；若仍存在，停止并报告内部状态不一致。 |
| `submit_program` / `provide_input` | 当前 generation 下调用匹配 direct operation，或提交修正的 `submit_model_program`。 |
| `validate_program` / `reconcile` / `cleanup` | 当前 generation 下调用一次 `resume_task`；冲突后 `get_task`。 |
| `wait` | 非紧密 `get_task`；持久状态仍可恢复时，最多一次 `resume_task`。 |
| `review_draft` | 展示 exact draft/verdict，只调用当前 `accept_draft` 或 `reject_draft`。 |
| `none` | 停止修改；只在成功且 eligible 时导出。 |

已知 task id 的未知响应或 conflict，第一恢复动作必须是 `get_task`。专门模拟
`create_task` unknown-outcome 且没有 task id：宿主必须用完全相同的 create key、project id 与
review policy 重放，并拿回同一个任务的当前 generation；不得生成新 key。

项目 id 未知时必须分页 `list_projects` 后调用 `get_project`；正常已知 id 不应强制全库扫描。
`list_revisions` 必须只返回当前 HEAD 的完整 committed ancestry，并按 canonical revision id 排序；
验收端应从 `head` 沿 `base_revision` 复原时间链，不能把数组顺序解释为提交时间。draft、candidate
与 abandoned revision 不得出现。两类 cursor 的 snapshot `conflict` 都必须从第一页重启，且读取
路径不得导入 FreeCAD、构造 runtime 或取得 project write lease。

对 ancestry 中的 same、正向祖先和反向祖先组合调用 `compare_revisions`，必须重新核对 manifest、
FCStd 与 STEP 的 presence/hash/size，并正确报告 base、manifest 和 artifact descriptor 差异。
generation-zero 合法无文件要与“manifest 声明但 payload 缺失”区分；同尺寸篡改和缺失必须
`integrity_failure`。`semantic_diff` 必须固定为 `unsupported`，不得输出几何、实体或参数差异结论。

### E09A：持久取消与 active reconcile

使用严格 `{schema_version, task_id, expected_generation}` 请求覆盖：

1. `created`、`needs_plan`、`program_ready`、`needs_input` 分别以当前 generation 调
   `cancel_task`，都只增加一个 generation，进入 `cancelled`，返回 `next_action=none`，且历史中
   恰好追加一个 `request_cancel`；
2. 丢弃成功响应并重启 server，再以原 generation 重放完全相同的请求，必须返回同一持久结果，
   不追加第二个 transition；至少 16 路相同并发取消也必须收敛为同一结果；
3. stale/future generation、未知 task、`recovery_required` / `cleanup_required`、
   `succeeded` / `failed` / `rejected` 分别返回冻结的 conflict/invalid-state/recovery error，且任务、
   HEAD 与文件树不变；
4. `awaiting_user_review` 不能取消，只能用当前 draft id/generation 调 `reject_draft`；
5. 对 active FreeCAD hang/kill、未提交证明、`cancel_requested`、`cancelling` 与最终终态分别验证可跨
   重启恢复；若任务返回 `next_action=reconcile`，只用当前 generation 调一次 `resume_task`，再读取任务
   取得事实终态，不得把请求送达或 transport cancellation 误写成已取消；
6. 取消前后比较 project HEAD/tree、源文件和 artifact tree；断言不取得 project write lease，不创建
   CAD/runtime/artifact/export 组件，也不调用 FreeCAD；
7. 单独发送 MCP `notifications/cancelled`，断言它只影响 transport request，TaskRun generation、
   status 和 transition 完全不变；
8. 认证 local daemon、共享 Application/Task Kernel、一次性 file grant 与受管可终止 Worker 必须在
   相同的公开任务语义下通过；它们不是第二套写入、审核或提交系统。

### E10：输入、预算与安全负例

逐项验证：

- JSON 非对象、未知字段、缺字段、错误类型、重复 key、NaN/Infinity、过深、过多节点、超长字符串；
- 超大 ModelProgram、命令数/结果引用/AcceptanceSpec/资源预算超限；
- 未知 operation、未知或已退役工具名、稳定/direct 命名碰撞；
- 伪造 project/task/revision/draft/artifact id；
- SelectorV1 绑定错误 revision、对象类型、provenance 或 cardinality；
- 任意 Python/FreeCAD code、STEP/STL import、通用 user-FreeCAD/face-edge/photo/simulation 请求。

所有负例应返回稳定、去敏的错误 envelope，不执行 CAD 副作用，不泄露绝对内部路径、环境变量、
token、secret、堆栈或用户文件内容。

MR0-C01..C04 已接受的独立 conformance 覆盖 undeclared capability、runtime/profile 与
artifact runtime/kind/media mismatch、只有 native locator 而没有 semantic selector、forbidden
commit/HEAD-like public authority，以及 deterministic fake identity 的 bounded admission/plan/route。
fake identity 仍只存在于 fixture，不能投影成产品 support。C04 没有把第二 native format 写入固定
durable store；该路径在 D14 下刻意未接入，并继续由 MR1-P00 migration contract /
`MRG1-RES-01A` 阻断。P00 没有改变该实现事实。

### E11：卸载保留数据

1. 在已有 project/task/revision/draft/artifact 时调用 `uninstall_runtime(confirm=false)`；
2. 验证只返回预览，文件未删除；
3. 显式确认后完成 runtime 清理；
4. 比较前后 durable data tree/hash，必须完全保留；
5. 重新安装同版本 runtime 后，项目、任务、草案与 artifact resource 仍可恢复；
6. engine 外部目录与用户日常 FreeCAD 配置均不被污染或删除。

### E12：Workbench Alpha 与单主机外部试点

1. `vibecad --freecad` 打开受管 FreeCAD，并只激活一个 VibeCAD Workbench/Dock；Workbench 在 GUI
   主线程更新，daemon client 在既有私有 worker lane 运行；
2. 对非空 HEAD 创建 `require_review` draft，分别打开 HEAD 与 draft checkout；选择 draft 中一个带
   VibeCAD identity 的 object/feature，必须得到绑定当前 project/revision 的 canonical `SelectorV1`，
   且 managed selector core 对完整对象清单唯一解析回同一对象；
3. Reject 后 TaskRun 持久化为 `rejected`、generation 只按一次决策推进、HEAD 仍指向 base revision，
   两个 checkout 与 FreeCAD preview document 全部关闭；Accept 变体则必须继续满足现有 HEAD CAS；
4. user-FreeCAD 路径只允许显式绝对 `.app`，依次验证 `--doctor`、原子 `--install-addon` 和可逆
   `--uninstall-addon`；不搜索 `PATH`、不改 app/preferences/macros/其他 addon，也不接管外来或变异树；
5. 当前准入只绑定已观察的 macOS FreeCAD 1.1.3、CPython 3.11、PySide6 6.8.3 与 host fingerprint；
   不得把单点 PASS 扩写成通用兼容性；
6. 外部 addon 在 FreeCAD 进程内不导入完整 VibeCAD/daemon backend，不保存 daemon secret，也不持有
   store/lease/commit authority；它只通过一个版本化、有界、封闭方法集的 managed-Python bridge
   复用同一 public client。bridge 失败时必须 fail closed，并保留 `vibecad --freecad` 回退。

当前本地 G1-03 观察已满足第 2–3 项的非空 Reject 变体：canonical feature selector 指向 candidate
revision，Reject 后 task generation 为 10、HEAD 仍为 base revision，checkout/document 均为零。
该证据只关闭 Workbench 产品链路，不关闭 §7 的真实第二宿主或一般 release gate。

## 5. 打包后独立会话

从全新输出根解包 `VibeCAD.mcpb`，不引用 checkout 的 `src/` 或开发虚拟环境。运行一个 raw/typed MCP
client，至少覆盖：

1. initialize、38-tool discovery、artifact/release resource template；
2. runtime epoch/version 与 ready 状态；
3. `get_capabilities`；
4. empty project → task → real `create_box` → auto-commit；
5. 空闲 task → `cancel_task` → restart/replay 后仍为同一 `cancelled` 记录，且没有 CAD/runtime；
6. `export_task_artifacts` → 两个 ResourceLink → `resources/read`；
7. accepted Revision → Release draft/PDF/BOM preview → exact digest approval → ZIP `resources/read`；
8. malformed/oversize/unknown-name/no-secret 负例；
9. restart 后项目、task、Release 和资源仍存在。

记录包 hash、Skill tree hash、运行 Python/FreeCAD 身份、discovery frame bytes、每次资源 hash 与退出码。

## 6. Skill 行为前向测试

用新的、没有本项目对话记忆的控制器加载 canonical Skill，给出至少以下自然语言任务：

- “创建一个 60 × 40 × 10 mm 盒子，先审核再导出”；
- “从这个 FCStd 继续修改圆柱高度”；
- “把这个 STL 导入并执行任意 FreeCAD Python 修复”；
- “刚才创建任务的响应丢了，没有 task id，继续完成它”。

前两项必须先发现能力、正确建立 project/task、使用 generation 与验收合同，并通过 ResourceLink 读取
资源。后两项必须分别如实拒绝未支持/任意 code 路径，以及用原 create key 安全重放
unknown-outcome `create_task`，不得换 key 创建第二个任务。

这项只证明 Skill 指令可被当前控制器遵循；WorkBuddy host-verified 证据来自下面独立的真实调用。

## 7. 真实宿主验收

WorkBuddy 5.3.5 + GLM-5.2 已完成：31-tool 严格调用、generation 14 的持久任务、跨 CLI 进程恢复、
两组件验收、Release generation 0→1 的精确摘要批准、22,372-byte PDF 与 45,559-byte ZIP 原生
Blob 取回，以及 ZIP 七条目完整性校验。批准没有改变 Revision 或 HEAD。VibeCAD 侧修复了
`tools/call._meta` 兼容和 60-second Release Worker deadline。

同一安装的 GLM-5V-Turbo 还完成 S35 多视图 outcome gate：三张 1,200 × 1,200 正投影视图解析为
50 × 40 × 60 mm、8 mm 双腿和三个 Ø6 通孔；经 strict adapter/compiler 修正首轮冗余约束与 Hole
方向后，Task `task_d17e24e1cad5f4f67a4c4408975100a7` 在 generation 9 到达
`awaiting_user_review`。bbox、`38681.4159868246 mm³` 体积、valid shape、single solid 全部
`pass`，HEAD 保持 base Revision。另一个只授予 Read 的运行把缺少拉伸深度和 50/45 mm 尺寸冲突
都分流为 `SAFE_FAILURE`，没有创建 Task。该证据证明固定多视图产品纵切片，但也明确说明
WorkBuddy 的首轮手写 IR 仍须由严格合同 fail closed，不能声称零修正成功率或普适照片重建。
结案前的独立只读复核还发现正例夹具原先的比例和孔位基准标注不够明确；修正为三图统一 4:1、
显式 `(X,Z)` / `(Y,Z)` 原点坐标后，新的无答案提示 GLM-5V-Turbo 复核返回 `PASS`、零冲突、零
blocking unknown。该复核只验证当前图片事实，不冒充新的 CAD 写入或第二个 end-to-end Task。

Codex、Claude、WorkBuddy 按以下同一矩阵分别执行；WorkBuddy 再增加 bounded submit 与其严格错误
呈现兼容门：

1. 安装同一 hash 的 MCPB 与 Skill；
2. 重启/重新加载并记录宿主版本；
3. 不提示工具名，只给“创建 60 × 40 × 10 mm 盒子、人工审核、交付 FCStd/STEP”的目标；
4. 核对宿主先发现 capability，正确路由 next_action，不猜 selector、不执行 arbitrary code；
5. 核对真实 Accept/Reject 与 ResourceLink/read；
6. 记录模型、计费来源、完整 tool trace、结果 hash 与失败重试。

每个 Profile 只有在自身场景真正通过后才能写为 `host-verified`；未运行或失败的宿主只能写
`host-ready`，不得由另一个宿主的 PASS 代替。

### 7.1 `VCAD-A06` v0.7.0 实际结果

三端均从 host-profile 候选 SHA-256 为
`6fd8e63db5d10181d81540339f7a2a9ca5622a5f11c2390db74a8d2540d93620` 的同一
`VibeCAD.mcpb` 启动，使用同一受管 FreeCAD 1.1.0 runtime 和 38-tool surface。宿主订阅、模型选择
与计费均由各宿主所有，未向 VibeCAD 提交 API key。

| 宿主 | 真实 Profile | 结果与持久证据 | Resource 证据 |
|---|---|---|---|
| Codex CLI 0.144.2 / GPT-5.6 Sol | 两个 fresh process；严格 stdio MCP | `task_bc815b51b8a2315b223a7dab502c53da` 从 generation 9 `review_draft` 恢复并在 generation 11 成功；HEAD=`revision_084df654692dea383ff7340484593b71` | FCStd 2,953 bytes / `a452e809...78528`；STEP 6,854 bytes / `e05fde00...699b`；均经 native MCP resource read |
| Claude Code 2.1.42 / Claude Opus 4.6 | 两个 `--no-session-persistence` fresh process；strict MCP config | `task_01b6e9cbd1506243f01e89d1b593f877` 在 generation 9 停于 `review_draft`，新进程恢复并在 generation 11 成功；HEAD=`revision_33eed04ed0c1419ab53bd6ae4f2b847f` | FCStd 2,954 bytes / `1a0c6804...8d35c`；STEP 6,854 bytes / `5dbc8808...11c1b`；均经 `ReadMcpResourceTool`/`resources/read` |
| WorkBuddy 5.3.5、CLI 2.115.0 / GLM-5.2-x | 两个 fresh process；`ToolSearch`/`DeferExecuteTool` | `task_c1e585cdb4c8fabfd2a61a3d9bdbe044` 从 generation 9 `review_draft` 恢复并在 generation 11 成功；HEAD=`revision_c17f8e40b3d3e14461870d4f6838d6a2` | FCStd 2,951 bytes / `07953cc9...fa53`；STEP 6,854 bytes / `11e52563...833`；均由 native `ReadMcpResource` 落盘后独立核对 |

真实 Codex discovery 还发现它会在 `tools/list` 请求参数中携带 `_meta.progressToken`。transport 现按
MCP request/notification metadata 边界接受并剥离受限的 `_meta` 对象，同时继续拒绝其它未知字段；
focused transport/server gate 为 194 passed。随后 GitHub macOS runner 证明读取临时 FreeCAD
pilot 可执行文件会合法改变 atime；稳定身份比较现明确绑定 dev/inode/mode/owner/size/mtime/ctime，
继续拒绝读取期间的内容变化，而不把 atime 当作篡改。该 delta 不触及 MCP、Task、Resource、Skill
或 CAD 执行路径，因此三宿主 outcome 证据按恢复纪律保留，不重新消耗三次模型建模；delta 后的最终
non-slow repository gate 为 `5,932 passed, 121 deselected`。从新 commit 重建的本地最终 MCPB
SHA-256 为 `a91552fa3e17f623c9b4ac11144eb49f5f36d87e7765f4c7d4b234ccf4e46447`，其全新解包
discovery、真实 FreeCAD、导出和两个 `resources/read` 集成门另为 `1 passed`。

## 8. 证据记录模板

```text
【Gate/场景】G__ / E__
【checkout/commit】
【package + skill SHA-256】
【VibeCAD / epoch / Python / FreeCAD / MCP】
【执行命令或用户原话】
【project / revision / task / generation / draft / artifact】
【预期】
【实际】
【ResourceLink URI / MIME / size / SHA-256】
【退出码与日志位置】
【结论】PASS / FAIL / NOT RUN
【残项或复测条件】
```

任何 Critical/Important 失败、版本/epoch/digest 漂移、源文件污染、HEAD 错误推进、错误 ResourceLink、
任意代码执行或数据丢失都阻断放行。外部宿主未执行应记录为未授权残项，不能伪造 PASS。
