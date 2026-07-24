# VibeCAD 0.6.0 本地交付候选用户手册

VibeCAD 是由 Claude、Codex 等宿主 Agent 调用的 FreeCAD 专家 Agent。你描述设计目标，宿主负责理解
与规划，VibeCAD 负责把受支持的 CAD 操作放进可恢复、可审核、可验证的项目流程，并交付 FCStd 与
STEP 资源。

当前版本适合验证 Agent-first 主链和完成长方体、圆柱体的创建、检查、参数修改、移动与旋转。它
还不是完整的机械 CAD 工作台，不能把任意照片、网格或复杂模型自动还原成参数化草图。

## 1. 使用前先理解三个角色

- **你**决定设计目标、尺寸、哪些事实必须保持，以及是否接受草案。
- **宿主 Agent**使用你的模型订阅或 API 配额，调用 VibeCAD 的公开工具并管理任务上下文。
- **VibeCAD**拥有 CAD 执行权：它在隔离副本中调用 FreeCAD，验证候选结果，记录 revision/draft，
  再发布或拒绝。

VibeCAD 不出售模型 token，也不会从 MCPB 中获得 Claude/Codex 的订阅额度。模型消耗由宿主账户
负责。

## 2. 当前能做什么

公开面固定为 28 个工具：

| 类别 | 工具 |
|---|---|
| 运行时 | `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime` |
| 能力发现 | `get_capabilities` |
| 项目与版本 | `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions`, `revert_project` |
| 任务 | `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task`, `cancel_task` |
| 审核 | `accept_draft`, `reject_draft` |
| 交付 | `get_artifact_manifest`, `export_task_artifacts` |
| 单步 CAD | `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part` |

工具名称不是能力说明书。宿主每次开始工作都应调用 `get_capabilities`，以返回的 operation、输入
schema、execution profile、预算和版本范围为准。

当前项目来源只有两种：

- `empty`：创建带 revision zero 的空项目；
- `import_fcstd`：导入一个非空 FCStd，且文件中每个对象都必须是 `Part::Box` 或
  `Part::Cylinder`。

空 FCStd、混合对象或任何其他对象类型会被拒绝。当前不支持通用 FCStd、STEP 或 STL import。

## 3. 安装 MCP 服务

当前 MCPB 产品声明仅覆盖 macOS（Darwin）。拿到 `VibeCAD.mcpb` 后，在支持 MCPB 的宿主中安装并
启用它，然后重启或重新加载宿主连接。不同宿主的安装入口可能不同，以宿主当前界面为准。

第一次启动时，扩展会准备隔离 Python 环境，并按需下载约 2–3 GB 的 FreeCAD 运行时。可以让宿主
调用 `get_runtime_status` 查看阶段；需要显式启动或重试时调用 `ensure_runtime`。运行时 ready 后，
`ping` 应返回当前 VibeCAD 版本。

macOS 默认数据根通常位于：

```text
~/Library/Application Support/VibeCAD/
```

其中 runtime 与 data 分开。卸载受管引擎时先调用 `uninstall_runtime(confirm=false)` 查看范围，再
由你确认后调用 `uninstall_runtime(confirm=true)`；项目、revision、draft 与 artifact 数据必须保留。

## 4. 单独安装 Agent Skill

MCPB 内带有 Skill 的归档副本，但安装 MCPB **不等于激活 Skill**。把仓库中的
`skills/vibecad-agent/` 或独立资产 `vibecad-agent-skill-0.6.0.zip` 解压得到的同名目录，整体复制
或链接到一个宿主发现路径：

| 宿主 | 用户级 | 项目级 |
|---|---|---|
| Codex 当前测试安装路径 | `$CODEX_HOME/skills/vibecad-agent`，默认 `$HOME/.codex/skills/vibecad-agent` | — |
| Codex 已发布发现路径 | `$HOME/.agents/skills/vibecad-agent` | `.agents/skills/vibecad-agent` |
| Claude Code | `$HOME/.claude/skills/vibecad-agent` | `.claude/skills/vibecad-agent` |

复制后重启或重新加载宿主，让它重新发现 Skill。Python wheel 与受管 runtime 只提供服务端，不包含
Skill，也不会替宿主修改 Skill 目录。

当前本地证据证明协议、包和 Skill 结构 `host-ready`；尚未调用第二个 Claude/Codex 外部模型执行
真实跨宿主验收，因此不要把当前状态描述成 `host-verified`。

## 5. 第一次设计：创建一个可审核盒子

在安装 MCP 服务和 Skill 后，可以向宿主表达：

> 用 VibeCAD 新建一个空项目，创建 60 × 40 × 10 mm 的长方体。我要先看验证结论再决定是否接受；
> 成功后交付 FCStd 和 STEP。

宿主应完成以下流程：

```text
get_runtime_status / ensure_runtime（仅在需要时）
  → get_capabilities
  → create_project(kind=empty，保留 create_key)
  → create_task(review_policy=require_review)
  → get_task
  → create_box（含 AcceptanceSpec）
  → get_task 并展示 draft/verdict/evidence
  → accept_draft 或 reject_draft
  → get_artifact_manifest
  → 尚无交付时才调用 export_task_artifacts
  → 对两个 ResourceLink 调 resources/read
```

你需要关注的不是“FreeCAD 命令执行成功”，而是证据是否满足设计意图，例如：

- 长、宽、高分别为 60、40、10 mm；
- bounding box 与尺寸一致；
- solid 数量和 shape validity 符合预期；
- 体积为 24,000 mm³（允许 AcceptanceSpec 中明确的数值容差）；
- 重新加载 FCStd 后事实保持一致。

如果选择 Reject，项目 HEAD 不变；如果选择 Accept，Kernel 会重新取得 lease、复核 draft 的 base
revision 与当前 HEAD，并在没有 stale-base conflict 时发布。

## 6. 自动提交与人工审核

创建任务时必须显式选择：

- `auto_commit`：候选验证通过且 HEAD 未漂移后直接成为新 revision；
- `require_review`：候选被封存为不可变 draft，展示 verdict/evidence，等待你明确 Accept 或 Reject。

尺寸变更、结构变化或来源不确定时建议使用 `require_review`。重复、低风险且验收条件已经稳定的单步
操作可以使用 `auto_commit`。

审核时必须核对当前 `task_id`、`draft_id`、`generation`、base revision、verdict 与 evidence。不要根据
一段自然语言总结接受草案，也不要接受旧对话中记住的 draft id。

## 7. direct operation 与 ModelProgram 怎么选

以下场景优先 direct operation：

- 创建一个盒子或圆柱；
- 检查当前任务绑定的模型事实；
- 对一个由稳定 SelectorV1 指定的对象改尺寸、移动或旋转。

多步骤、步骤间需要 ResultRef、或希望把整个候选一次性验收时，使用
`submit_model_program`。ModelProgram 是受限 JSON 合同，不是 Python 代码。

两种方式共享同一个 Task Kernel：direct operation 也会构造 ModelCommand/ModelProgram，进入同一
候选 checkout、验证、draft 与 commit 流程。宿主不得把 direct operation 变成原地修改文件的旁路。

## 8. 修改已有对象时的规则

不要猜 FreeCAD 标签、对象下标、面号或边号。先用 `inspect_model` 或任务证据拿到 revision-bound
SelectorV1，再把完整选择器传给：

- `modify_parameter`：当前可改 `length`、`width`、`height` 或 `radius`；
- `move_part`：设置对象位置；
- `rotate_part`：绕 x/y/z 轴旋转非零角度。

SelectorV1 绑定 project/revision、持久 object/feature id、对象类型、semantic role、provenance 与
expected cardinality。revision 已变化时必须重新获取选择器，不能沿用旧值。当前只支持 object/feature
级选择；不支持 face/edge 选择。

修改请求还应显式说明 preservation 条件，例如移动时保持尺寸、体积、solid count 与 valid shape，
改长度时保持未要求变化的宽、高和对象身份。AcceptanceSpec 是可执行验收合同，不能省略成“看起来
差不多”。

## 9. 持久任务的恢复规则

每次写操作都必须使用服务端刚返回的 `expected_generation`。遇到冲突、超时或中断时，如果已经知道
`task_id`，第一步调用 `get_task`，并用返回的 status/generation/next_action 替换本地记忆。

| `next_action` | 宿主动作 |
|---|---|
| `request_plan` | 当前公开 `create_task` 不应返回它；调用 `get_task` 一次，若仍存在则停止并报告内部状态不一致。 |
| `submit_program` / `provide_input` | 用返回的 generation 调一个匹配的 direct operation，或调用 `submit_model_program` 提交修正后的受限程序。 |
| `validate_program` / `reconcile` / `cleanup` | 用返回的 generation 调用一次 `resume_task`；冲突后回到 `get_task` 重新判断。 |
| `wait` | 不紧密轮询；先 `get_task`，中断后若持久状态仍可恢复，最多调用一次 `resume_task`。 |
| `review_draft` | 展示精确 draft/verdict/evidence，只允许用当前 id 与 generation 调 `accept_draft` 或 `reject_draft`。 |
| `none` | 停止修改；只有成功且符合资格的状态才能导出，`cancelled`、失败或拒绝任务不得导出。 |

调用 `create_task` 前生成并保留一个 `task_create_[0-9a-f]{32}` key。结果未知时，以完全相同的 key、
project id 和 review policy 重放；服务会返回同一任务的当前 generation。恢复已有但 id 未知的任务时，
用 `list_tasks` 分页选定摘要，再调用 `get_task`；快照 cursor 冲突时从第一页重启。`get_task_events`
仅用于审计持久化 transition，cursor 失效时同样从第一页重启。

如果连项目 id 也未知，只在此时分页调用 `list_projects`，选定摘要后用 `get_project` 获取当前权威
HEAD。需要查看已提交历史时调用 `list_revisions`：它只返回当前 HEAD 的 committed ancestry，数组按
canonical revision id 而不是提交时间排序，应从返回的 `head` 沿每项 `base_revision` 恢复链。
draft、candidate 和 abandoned revision 不会返回。项目或 revision cursor 发生 `conflict` 时丢弃它，
从第一页重新开始；这些读取不会启动 CAD/runtime 或取得项目写 lease。

需要核对两个 committed revision 时调用 `compare_revisions`。它会验证谱系和实际 FCStd/STEP
presence、SHA-256 与大小，但不会声称已经理解几何、实体或参数语义；这些语义差异固定报告为
`unsupported`。

### 9.1 用历史 revision 创建可审核的 forward revert

需要回到某个历史版本时，先用 `list_revisions` 确认它仍属于当前 HEAD ancestry，再读取最新
`get_project`。调用 `revert_project` 时必须保留一个唯一 `revert_create_...` key，并同时提交历史
`source_revision` 与刚读取的 `expected_head`。服务不会改写历史，也不会把旧 FCStd 原地覆盖到当前
项目；它以当前 HEAD 为 base，把历史模型复制进新的隔离候选，经过真实 FreeCAD reload、STEP 和
verifier 后返回 immutable draft。

HEAD 在 draft 准备期间保持不变。宿主应读取返回的 task/draft/verdict/evidence，必要时通过普通资源
流程检查 FCStd/STEP，然后只调用该 draft 的 `accept_draft` 或 `reject_draft`。Accept 发布的是一个
以调用时 HEAD 为父 revision 的新 forward commit；Reject 不改变 HEAD。响应未知时以同一 revert key
和完全相同的 source/expected-head 重放，不能生成另一个 key 或直接替换项目文件。

### 9.2 持久取消与执行中取消

当任务尚未进入 active CAD 执行，或停在等待输入的状态而你决定放弃时，先调用 `get_task`，再用
刚返回的 `task_id` 与 generation 调 `cancel_task`。当前可立即取消的状态只有 `created`、
`needs_plan`、`program_ready` 和 `needs_input`；成功后任务持久化为 `cancelled`、generation 增加，
`next_action` 变为 `none`。

若取消响应丢失或客户端重启，重放完全相同的取消请求即可取回同一结果，不要生成新的任务或伪造
未来 generation。多个相同并发请求也只能产生一个 `request_cancel` transition。等待人工审核的
任务必须使用 `reject_draft`，不能用取消代替审核决定。

对空闲任务，`cancel_task` 不会启动 FreeCAD 或 runtime，不会构造 artifact/export 服务，不取得 project
write lease，也不会修改项目 HEAD、源文件或交付目录。对已开始执行的任务，取消请求和 Worker 处理结果
同样会持久化。若 `get_task` 返回 `next_action=reconcile`，先采用该响应中的当前 generation，然后最多
调用一次 `resume_task`；冲突或未知结果后再次 `get_task`，绝不猜测 Worker 是否已经停止、绝不伪造未来
generation。MCP `notifications/cancelled` 只取消一次 transport request，不会持久化 TaskRun 状态，不能
代替 `cancel_task`。

## 10. 获取 FCStd 与 STEP

只有成功且符合交付资格的 committed revision 或 draft 才能取得资源。先用
`get_artifact_manifest` 传入当前 task generation、revision id 和适用的 draft id。若返回
`materialized=true`，直接读取其中的两个 ResourceLink；若返回 `materialized=false`，再保留唯一
`export_key` 调用一次 `export_task_artifacts`。清单查询本身不会创建、复制、验证或清理交付文件。

成功结果必须包含两个有类型的 `ResourceLink`：

| 格式 | MIME | 读取方式 |
|---|---|---|
| FCStd | `application/vnd.freecad.fcstd` | 对返回 URI 调 `resources/read` |
| STEP | `model/step` | 对返回 URI 调 `resources/read` |

读取后核对 URI 对应的 format、name、size 和 SHA-256，再把资源交给用户。失败或未物化的清单不应
产生 ResourceLink；除 `export_task_artifacts` 外，已经物化的 `get_artifact_manifest` 是唯一会返回
同一两条链接的只读工具。
VibeCAD 不提供任意路径 copy-out；也不要让 Agent 浏览、公开或读取无关文件。
FCStd 导入时只能使用用户明确授权给 `create_project` 的源文件。

## 11. 当前明确不支持的能力

0.6.0 本地交付候选不支持：

- STEP/STL import、STL 到 STEP、照片/视频重建和 2D 草图识别；
- 通用 FCStd、Sketcher、PartDesign、孔、圆角、倒角、布尔、装配、BOM 与 TechDraw；
- FreeCAD Qt Workbench UI、可视 preview 或 face/edge 交互选择；
- simulation、碰撞求解或制造工艺验证；
- MCP Sampling（`mcp_sampling`）或 VibeCAD 自营 BYOK 模型后端；
- 模型生成并执行任意 Python/FreeCAD code。

需要这些能力时，宿主应如实报告当前不可用，或在得到明确授权后调用单独的外部引擎；不能伪装成
VibeCAD 已完成。

## 12. 故障排查

### 看不到工具

确认 MCPB 已启用并重新加载宿主。然后检查 `ping`。如果只能看到服务端而宿主不会遵循流程，再检查
Skill 是否复制到了正确目录；MCPB 安装不会自动激活 Skill。

### 工具可见但 CAD 不能执行

调用 `get_runtime_status`，记录 phase、percent 和 error。需要时调用 `ensure_runtime`。磁盘不足、网络
无法访问依赖源或安装被中断都可能让 runtime 未达到 ready。

### 请求提示 generation conflict

停止当前写操作，调用 `get_task`，使用服务端返回的新 generation 与 next_action 重新路由。不要只改
generation 后重放旧请求，因为 base revision、draft 或任务状态也可能已经变化。

### 取消失败或任务仍在运行

先用 `get_task` 刷新状态与 generation。`awaiting_user_review` 必须调用 `reject_draft`；若返回
`next_action=reconcile`，只用该响应的当前 generation 调用一次 `resume_task`，随后重新读取任务确认
最终状态。不要把请求已送出、transport 已取消或 Worker 正在收尾写成任务已经停止；不要用 MCP
`notifications/cancelled` 代替 `cancel_task`。

### 导出没有资源链接

确认任务已成功、revision/draft 符合导出资格、generation 最新，并检查结构化错误。失败调用不应
返回 ResourceLink；不要改用本地任意路径导出。

### 导入 FCStd 被拒绝

确认文件非空，且所有对象都精确属于 `Part::Box` 或 `Part::Cylinder`。当前拒绝其他对象属于产品
边界，不是让模型尝试任意 FreeCAD Python 的理由。

## 13. 当前交互边界

当前执行 profile 已验证 headless，并由受管、可终止的 FreeCAD Worker 执行。认证本地 daemon、同用户
IPC 与一次性 file grant 后端已经完成；它与 MCP 共享同一个 Application 和 Task Kernel，不是第二套写入
或提交系统。G1 仍需交付真实 FreeCAD Qt Workbench UI 的 preview、verdict、Accept/Reject 与
object/feature selection。

当前外部 Claude/Codex 模型调用尚未纳入本地放行证据。要做宿主实测，必须单独授权相应模型/token
消耗，并记录所用宿主版本、Skill hash、28-tool discovery 与完整任务结果。
