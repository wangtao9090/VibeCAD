# VibeCAD 0.5.0 Beta 验收测试

本清单验证当前 Agent-first 产品：持久化 Project/Task/Revision/Draft/Artifact、26 个公开工具、direct
operation 与 ModelProgram 的统一 Task Kernel，以及可验证的 FCStd/STEP 资源交付。

放行结论必须区分：

- **protocol/package host-ready**：本地 raw/typed MCP、Skill 包、受管 FreeCAD 与打包后会话全部通过；
- **host-verified**：真实 Claude/Codex 等第二宿主使用外部模型执行同一任务并通过。

0.5.0 Beta 的当前放行范围是前者。未授权外部模型/API 消耗，因此不能把本清单的本地模拟、测试
double 或当前控制器执行写成 host-verified 证据。

## 1. 冻结产品口径

### 1.1 公开工具

运行时 `tools/list` 与 MCPB manifest 必须同序公开以下 26 个唯一名称：

| 类别 | 工具 |
|---|---|
| 运行时 | `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime` |
| 能力 | `get_capabilities` |
| 项目与版本 | `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions` |
| 任务 | `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task` |
| 审核 | `accept_draft`, `reject_draft` |
| 交付 | `get_artifact_manifest`, `export_task_artifacts` |
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

### 1.3 当前支持边界

- 项目可以是 `empty`，或导入非空、对象全为 `Part::Box` / `Part::Cylinder` 的
  `import_fcstd` envelope；
- 当前只验证 headless execution profile；
- 成功交付只有 FCStd 与 STEP；
- 当前不支持通用 FCStd、STEP/STL import、任意 Python/FreeCAD code、Workbench、face/edge selector、
  photo/video reconstruction 或 simulation。

## 2. 放行总表

| ID | Gate | 通过标准 | 结果 |
|---|---|---|---|
| G01 | 版本与协议身份 | source/pyproject/manifest/package = 0.5.0；server epoch = 4；MCP/FreeCAD/Python pin 不漂移 | ☐ |
| G02 | 公开面 | 精确 26 个唯一工具；说明与 manifest 完全一致；固定 discovery frame ≤ 32,768 bytes | ☐ |
| G03 | 内部校验 | discovery 不发 output schema，但正常与异常 CallToolResult 仍受冻结 output validator 约束 | ☐ |
| G04 | 命名空间 | direct 与稳定名称碰撞、direct 重名都在 schema/dispatch/effect 前 fail closed | ☐ |
| G05 | Skill | canonical Skill 通过校验；示例、恢复表和限制与 live schema 一致 | ☐ |
| G06 | 分发 | sdist/MCPB/Skill zip 含同一 Skill tree；wheel/installed Python 不含 Skill | ☐ |
| G07 | 普通测试 | 全量 non-slow pytest、Ruff、changed-Python format/pycompile、offline lock、diff check 通过 | ☐ |
| G08 | 受管 FreeCAD | Darwin slow matrix 通过；安装只同步 0.5.0/epoch 4，不重建现有引擎 | ☐ |
| G09 | Agent E2E | empty/import、direct/program、review/restart/conflict、artifact/resource 与负例通过 | ☐ |
| G10 | 数据保护 | runtime uninstall 不删除 durable data；执行和导出不污染源文件或暴露任意路径 | ☐ |
| G11 | 打包后会话 | 从全新解包 MCPB 启动并复跑 discovery、真实 CAD 与资源读取 | ☐ |
| G12 | 独立审查 | 至少两路 settled-diff review；所有 Critical/Important 关闭 | ☐ |

## 3. 自动化与打包 Gate

### G01：身份一致性

检查：

1. `src/vibecad/__init__.py`、`pyproject.toml`、`manifest.json`、wheel/sdist metadata 都是 `0.5.0`；
2. runtime receipt、status 与 server handshake 使用同一 VibeCAD 版本；
3. private server epoch 为 4，public-surface digest 绑定 description、input/output enforcement schema 与
   annotations；
4. MCP 保持 1.27.2、Python 保持 3.12、FreeCAD 保持 1.1.0；
5. `uv lock --offline` 不产生非预期差异。

任何一个身份不一致都阻断放行。

### G02：26-tool discovery

对固定 JSON-RPC request id `1` 获取完整 `tools/list`，用 sorted keys、compact separators、
`ensure_ascii=false` 序列化，并计入末尾 LF。预期：

- 名称与 §1.1 精确同序，唯一且无额外工具；
- 每项 description 非空、单行、可打印且在长度预算内；
- 每项包含 input schema 和 annotations；
- discovery 项不包含 optional output schema；
- 完整 UTF-8 tools/list frame 不超过 32,768 bytes；
- `manifest.json` 的 `(name, description)` 与 PublicToolSpec 逐项完全一致。

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
- 正文列出精确 26 个工具，先 `get_capabilities`，包含 project/task/review/artifact 流程；
- direct/ModelProgram、SelectorV1、AcceptanceSpec、ResultRef、generation 与恢复表和实际 schema 一致；
- 明确要求 unknown-outcome `create_task` 用相同 create key 与不可变意图重放，并禁止换 key
  恢复、已退役 endpoint、任意 code 与未支持能力；
- 安装路径覆盖 Codex 当前测试路径、Codex 已发布 user/repo 路径和 Claude Code user/repo 路径；
- MCPB 内存在 Skill 不被描述成已经 activation，文档要求 restart/reload。

从干净输出目录分别构建 wheel、sdist、MCPB 与 `vibecad-agent-skill-0.5.0.zip`。预期矩阵：

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
`vibecad-agent-skill-0.5.0.zip`，且仍需要明确的 environment/tag 授权。本验收不执行 tag 或发布。

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

### E10：输入、预算与安全负例

逐项验证：

- JSON 非对象、未知字段、缺字段、错误类型、重复 key、NaN/Infinity、过深、过多节点、超长字符串；
- 超大 ModelProgram、命令数/结果引用/AcceptanceSpec/资源预算超限；
- 未知 operation、未知或已退役工具名、稳定/direct 命名碰撞；
- 伪造 project/task/revision/draft/artifact id；
- SelectorV1 绑定错误 revision、对象类型、provenance 或 cardinality；
- 任意 Python/FreeCAD code、STEP/STL import、Workbench/face-edge/photo/simulation 请求。

所有负例应返回稳定、去敏的错误 envelope，不执行 CAD 副作用，不泄露绝对内部路径、环境变量、
token、secret、堆栈或用户文件内容。

### E11：卸载保留数据

1. 在已有 project/task/revision/draft/artifact 时调用 `uninstall_runtime(confirm=false)`；
2. 验证只返回预览，文件未删除；
3. 显式确认后完成 runtime 清理；
4. 比较前后 durable data tree/hash，必须完全保留；
5. 重新安装同版本 runtime 后，项目、任务、草案与 artifact resource 仍可恢复；
6. engine 外部目录与用户日常 FreeCAD 配置均不被污染或删除。

## 5. 打包后独立会话

从全新输出根解包 `VibeCAD.mcpb`，不引用 checkout 的 `src/` 或开发虚拟环境。运行一个 raw/typed MCP
client，至少覆盖：

1. initialize、26-tool discovery、resource template；
2. runtime epoch/version 与 ready 状态；
3. `get_capabilities`；
4. empty project → task → real `create_box` → auto-commit；
5. `export_task_artifacts` → 两个 ResourceLink → `resources/read`；
6. malformed/oversize/unknown-name/no-secret 负例；
7. restart 后项目、task 和资源仍存在。

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

这项证明 Skill 指令可被当前控制器遵循，不等于外部 Claude/Codex host-verified。

## 7. 真实第二宿主验收（独立授权后执行）

若之后授权模型/token 消耗，在 Claude 与 Codex 中至少各选一个真实宿主：

1. 安装同一 hash 的 MCPB 与 Skill；
2. 重启/重新加载并记录宿主版本；
3. 不提示工具名，只给“创建 60 × 40 × 10 mm 盒子、人工审核、交付 FCStd/STEP”的目标；
4. 核对宿主先发现 capability，正确路由 next_action，不猜 selector、不执行 arbitrary code；
5. 核对真实 Accept/Reject 与 ResourceLink/read；
6. 记录模型、计费来源、完整 tool trace、结果 hash 与失败重试。

在此场景真正通过前，发布材料只能声称 host-ready。

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
