# VibeCAD

[![CI](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml/badge.svg)](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)

VibeCAD 是面向 Claude、Codex 等宿主 Agent 的 FreeCAD 专家 Agent。它把设计意图转换为持久化项目、
受约束的 CAD 操作、可审核草案，以及经过验证的 FCStd/STEP 资源。

VibeCAD 不内置或转售大模型。推理使用用户自己的宿主模型及其订阅或 API 配额；VibeCAD 负责 CAD
合同、隔离执行、确定性验证、恢复与交付。

## 当前 Agent-first 工作流

```text
用户与宿主 Agent
  → get_capabilities 读取实际能力
  → create_project 创建空项目或受控导入 FCStd
  → create_task 绑定项目版本与审核策略
  → 调用一个 direct operation，或提交多步骤 ModelProgram
  → Task Kernel 在隔离 checkout 中执行并验证候选版本
  → auto_commit 发布，或 require_review 等待 Accept/Reject
  → export_task_artifacts 返回 FCStd/STEP ResourceLink
  → resources/read 读取并核对交付资源
```

direct operation 与 ModelProgram 不是两套执行系统。direct operation 只是把一次明确操作编译成
单命令 ModelProgram；两条路径都进入同一个 Task Kernel，共享 project lease、不可变 base revision、
候选 checkout、验证、draft、commit、reject、rollback 与恢复语义。

当前只能从空项目或一个 FCStd 文件开始；其中 FCStd 导入必须非空，且其中每个对象都必须是
`Part::Box` 或 `Part::Cylinder`。混合或其他对象类型会被拒绝。通用 FCStd 导入属于 P1；STEP/STL
导入、逆向工程和仿真 尚未接入。照片/视频到网格或 STL、2D 草图识别等前置引擎可以在以后作为
外部工具连接，VibeCAD 聚焦可编辑 CAD 的中间编排与验证。

## 当前公开能力（0.6.0 本地交付候选）

MCPB manifest 与运行时投影同一份冻结合同，当前公开 28 个工具。每个工具都有简短说明、严格输入
schema 与副作用标记；宿主应先调用 `get_capabilities`，不能根据工具数量或模型常识猜能力。

| 类别 | 工具 |
|---|---|
| 服务与运行时 | `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime` |
| 能力发现 | `get_capabilities` |
| 项目与版本 | `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions`, `revert_project` |
| 任务与草案 | `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task`, `cancel_task`, `accept_draft`, `reject_draft` |
| 交付 | `get_artifact_manifest`, `export_task_artifacts` |
| direct operation | `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part` |

一次成功的 `export_task_artifacts` 返回规范结果及两个有类型的 `ResourceLink`：

- FCStd：`application/vnd.freecad.fcstd`；
- STEP：`model/step`。

宿主只能通过返回的 URI 调用 `resources/read` 获取二进制内容，并核对格式、大小与 SHA-256。接口不
提供任意路径导出或任意文件读取。

## 为什么不让模型直接执行 FreeCAD Python

FreeCAD 是几何引擎和执行环境，但“代码能运行”不等于“设计符合意图”。当前主路径只接受版本化、
有限操作集、有限预算的 ModelProgram；不接受模型生成的任意 Python/FreeCAD code，也不把它作为
失败时的后备通道。

Task Kernel 为每次写操作提供以下保证：

- 输入通过严格 schema、选择器、预算与 AcceptanceSpec 校验；
- 执行发生在隔离候选副本，不原地修改用户源文件；
- 结果绑定 base revision、task generation、验证证据和不可变 revision；
- `auto_commit` 仅在验证通过且 HEAD 未漂移时发布；
- `require_review` 产生持久化 draft，Accept 才发布，Reject 不改变 HEAD；
- 交付物在导出和读取时再次校验状态、来源、哈希与大小。

调用 `create_task` 前必须生成并持久保留一个 `task_create_` request key。若响应结果未知，
使用完全相同的 key、项目与审核策略重放 `create_task`；Task Kernel 会返回同一个任务的当前
generation，不会产生第二个任务。

`cancel_task` 首次调用必须使用刚读取的 task generation。它会把 `created`、`needs_plan`、
`program_ready` 或 `needs_input` 空闲任务以 CAS 立即持久化为 `cancelled`；取消响应未知时可重放
完全相同的请求，并取得同一取消结果。执行中的任务会持久化其取消状态；当当前任务返回
`next_action=reconcile` 时，宿主只能先读取任务，再用刚返回的 generation 调用一次 `resume_task`，
不能猜测 Worker 是否已经停止或伪造未来 generation。等待审核的 draft 必须用 `reject_draft`。

空闲取消只改变任务记录，不启动 CAD/runtime，不构造 artifact/export 组件，不取得 project write
lease，也不改变项目 HEAD、源文件或交付目录。MCP `notifications/cancelled` 只取消一个 transport
request，不是持久任务取消。

只有项目 id 未知时才用 `list_projects` 分页发现项目，然后用 `get_project` 读取当前权威 HEAD。
`list_revisions` 只返回该项目当前 HEAD 的已提交祖先；结果按 canonical revision id 排序，不是时间
顺序，应从返回的 `head` 沿 `base_revision` 恢复提交链。draft、candidate 与 abandoned revision 不会
作为已提交历史返回。任一分页 cursor 返回 `conflict` 时，丢弃 cursor 并从第一页重启。

`compare_revisions` 会重新校验两个 committed revision 的 manifest 与实际 FCStd/STEP 文件，只报告
谱系、文件是否存在以及标识、SHA-256、大小的差异；几何、实体和参数语义 diff 明确为
`unsupported`。交付前先调用只读的 `get_artifact_manifest`：已有经过验证的 PUBLISHED delivery 时
直接返回两个 ResourceLink；否则返回 `materialized=false`，且不会创建、复制或清理任何交付文件，
此时才调用 `export_task_artifacts`。

## 安装：MCP 服务与 Agent Skill 是两件事

当前 MCPB 产品声明只覆盖经过验证的 macOS（Darwin）路径。安装 `VibeCAD.mcpb` 会安装 MCP 服务，
但包内附带的 Skill 只是归档内容，不会自动激活。宿主必须单独复制或链接
`skills/vibecad-agent/`，再重启或重新加载宿主。

Skill 的发现路径如下：

| 宿主 | 用户级路径 | 项目级路径 |
|---|---|---|
| Codex 当前安装器路径 | `$CODEX_HOME/skills/vibecad-agent`；未设置时默认 `$HOME/.codex/skills/vibecad-agent` | — |
| Codex 已发布发现路径 | `$HOME/.agents/skills/vibecad-agent` | `.agents/skills/vibecad-agent` |
| Claude Code | `$HOME/.claude/skills/vibecad-agent` | `.claude/skills/vibecad-agent` |

发布资产中的 `vibecad-agent-skill-0.6.0.zip` 解压后只有一个顶层 `vibecad-agent/` 目录，可整体复制
到上述任一路径。Python wheel 和受管运行时只包含服务端，不包含或激活 Skill。

扩展首次启动需要联网获取锁定的 Python 包，并按需安装约 2–3 GB 的 FreeCAD 运行时；后续启动复用
已验证缓存。macOS 默认数据根通常是：

```text
~/Library/Application Support/VibeCAD/
```

运行时与项目数据分离。`uninstall_runtime` 先预览、再显式确认，只删除受管运行时并保留项目、
revision、draft 和 artifact 数据；扩展本体随后由宿主设置移除。

### 本地开发

```bash
uv sync --frozen
PYTHONPATH=src uv run --frozen pytest
uv run --frozen ruff check .
VIBECAD_AUTO_INSTALL=0 uv run --frozen python -m vibecad.server
```

FreeCAD 不属于普通 Python 依赖，由运行时安装器单独管理。真实运行时集成测试需显式开启：

```bash
VIBECAD_RUN_INTEGRATION=1 PYTHONPATH=src uv run --frozen pytest -m slow
```

## Host-ready 的准确含义

0.6.0 本地交付候选已验证 MCP 协议、Skill 包结构、FCStd/STEP ResourceLink、受管 FreeCAD E2E 与
28-tool discovery，因此可以称为 protocol/package `host-ready`。它仍是未发布候选；当前阶段没有消费
用户的 Claude/Codex 外部模型配额执行第二宿主验收，所以不能称为
`host-verified`；实际跨宿主模型调用仍是独立残项。

## 架构边界与路线

当前领域链路是 MCP transport/server → same-user authenticated local daemon → single Agent application
→ Task Kernel → CAD execution port → managed killable FreeCAD Worker；public Workbench client 同样从
daemon 进入该 Application/Task Kernel。运行时维护和无状态 discovery 仍由 MCP server 本地处理，不是
第二条领域写入路径。daemon 提供同用户认证及受限的一次性 file grant，不形成第二套提交系统。

S3-8 与 0.6.0 package/managed-runtime 本地候选收口均已完成；该候选尚未 tag 或发布。后续顺序为
G1 → P0-B hardening → P1/G2 → P2：

- **P0-B core（后端完成）**：任务/项目/版本发现、文件级比较、verified forward revert、取消/reconcile、
  认证 daemon、file grant、source liveness 与受管可终止 FreeCAD Worker 都进入同一 Task Kernel；
- **G1**：真实 FreeCAD Qt Workbench UI 中的 preview、verdict、Accept/Reject 与 object/feature selection；
- **P1/G2**：Sketcher/PartDesign、受控导入、单零件生产能力和手工 checkpoint；
- **P2**：装配、BOM、TechDraw、制造发布与企业交付链。

G1 Workbench 尚未交付（真实 FreeCAD Qt UI），当前主路径仍是受管 Agent/headless 执行。当前也不支持 face/edge 选择、
Workbench 交互、STEP/STL import、照片重建或 simulation。

进一步阅读（源代码仓库）：
[用户手册](https://github.com/wangtao9090/VibeCAD/blob/main/docs/USER_GUIDE.md)、
[验收测试](https://github.com/wangtao9090/VibeCAD/blob/main/docs/ACCEPTANCE_TESTS.md)、
[整体架构](https://github.com/wangtao9090/VibeCAD/blob/main/docs/ARCHITECTURE.md)、
[Agent 架构](https://github.com/wangtao9090/VibeCAD/blob/main/docs/AGENT_ARCHITECTURE.md) 和
[产品能力路线图](https://github.com/wangtao9090/VibeCAD/blob/main/docs/PRODUCT_CAPABILITY_ROADMAP.md)。

## License

[MIT](LICENSE)
