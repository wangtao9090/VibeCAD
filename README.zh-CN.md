# VibeCAD

[English](README.md) | **[简体中文](README.zh-CN.md)**

[![CI](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml/badge.svg)](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)

VibeCAD 是面向 Claude、Codex、WorkBuddy 等宿主 Agent 的 FreeCAD 专家 Agent。它把设计意图转换为持久化项目、
受约束的 CAD 操作、可审核草案，以及经过验证的 FCStd/STEP 资源。

VibeCAD 不内置或转售大模型。推理使用用户自己的宿主模型及其订阅或 API 配额；VibeCAD 负责 CAD
合同、隔离执行、确定性验证、恢复与交付。

## VibeCAD 当前提供什么

- 从设计意图到持久 CAD 项目和版本化结果的 Agent-native 路径；
- 可实际运行的 FreeCAD Workbench Alpha，支持项目/任务发现、HEAD 与草案预览、verdict、Accept
  和 Reject；
- 确定性的 Task Kernel 执行：隔离候选、明确审核策略、经过验证的 FCStd/STEP 制品、恢复与安全重放；
- 已验证的 WorkBuddy 5.3.5 本地 stdio 路径，覆盖严格 schema、重启恢复、精确 Release 批准以及
  PDF/ZIP 的原生 MCP Blob 读取；
- VibeCAD 自行管理 FreeCAD 运行时，用户无需预先配置兼容的系统 FreeCAD。

## 体验 FreeCAD Workbench Alpha

最简单的安装方式是把下面这句话交给编码 Agent：

> 请从 https://github.com/wangtao9090/VibeCAD 安装并启动 VibeCAD FreeCAD
> Workbench Alpha。使用 tag `v0.6.1`，
> 克隆到持久目录，构建 wheel，通过 `uv tool install --force` 安装，保留 checkout
> 和构建出的 wheel，最后运行 `vibecad --freecad`。不要安装或回退到系统版 FreeCAD。

Agent 应执行以下可复现步骤：

```bash
git clone https://github.com/wangtao9090/VibeCAD.git VibeCAD
git -C VibeCAD checkout v0.6.1
cd VibeCAD
uv build --wheel
uv tool install --force dist/vibecad-0.6.1-py3-none-any.whl
vibecad --freecad
```

安装说明：

- 使用这个 Alpha 期间，保留持久 checkout、wheel 及其原路径；
- 不搜索 `PATH`、`/Applications`、普通 FreeCAD `Mod` 目录，也不安装系统 FreeCAD 作为后备；
  `vibecad --freecad` 只使用经过验证的受管运行时；
- 首次启动允许下载约 2–3 GB 的锁定运行时文件，后续启动复用；
- 成功判据是受管 FreeCAD 打开，VibeCAD Workbench 与审核 Dock 均已激活；若失败，报告 launcher
  的精确错误并停止，不切换运行时，也不自行发明其他安装路径。

当前 Dock 可以列出项目和任务、刷新所选状态、打开相互独立的受管 HEAD 与草案预览文档、展示审核
结论、捕获精确的完整对象或 feature `SelectorV1`，并对新鲜草案执行 Accept 或 Reject。当前尚不
宣称 face/edge 子元素选择能力。

当前 P1 源码还增加了 Agent review 结束后的顺序手工收尾：**Open Editable HEAD** 创建非权威工作
副本，普通 **Save** 只保存在本地，**Checkpoint Edit** 验证并发布新 Revision，**Discard Edit** 不会
发布任何内容。Agent preview 与 editable HEAD 相互排斥；系统不做自动 merge 或 rebase。

上述受管启动器仍是默认与回退路径。另有一个刻意收窄的 macOS 试点，可把同一个薄 Workbench 安装
到用户显式指定的 FreeCAD：

```bash
vibecad --freecad-app /Applications/FreeCAD.app --doctor
vibecad --freecad-app /Applications/FreeCAD.app --install-addon
# 可逆清理
vibecad --freecad-app /Applications/FreeCAD.app --uninstall-addon
```

这不是通用的系统 FreeCAD 支持。当前本地证据只准入精确指纹绑定的 macOS FreeCAD 1.1.3、内嵌
CPython 3.11 与 PySide6 6.8.3；doctor 对其他主机 fail closed。安装的 addon 不持有 daemon secret，
selector 构造与唯一解析由受管 Python bridge 和受管模式共用的同一 Task Kernel 完成。

## 当前 Agent-first 工作流

```text
用户文本或图片与宿主多模态 Agent
  → 宿主把可见事实区分为 confirmed、inferred 与 unknown
  → 缺少绝对尺度等阻塞信息时先询问，不静默猜测
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
导入、逆向工程和仿真尚未接入。图片请求由 Codex、Claude、WorkBuddy 或其他宿主已有的多模态模型
使用自己的订阅/API 授权完成理解，再把受控 ModelProgram 送入普通 Task Kernel。VibeCAD 不需要
宿主模型凭据，也不会把同一图片再次上传给第二个模型。

## 当前公开能力（开发分支）

MCPB manifest 与运行时投影同一份冻结合同，当前公开 38 个工具。每个工具都有简短说明、严格输入
schema 与副作用标记；宿主应先调用 `get_capabilities`，不能根据工具数量或模型常识猜能力。

| 类别 | 工具 |
|---|---|
| 服务与运行时 | `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime` |
| 能力发现 | `get_capabilities` |
| 项目与版本 | `create_project`, `get_project`, `list_projects`, `list_revisions`, `compare_revisions`, `revert_project` |
| 任务与草案 | `create_task`, `list_tasks`, `get_task`, `get_task_events`, `submit_model_program`, `resume_task`, `cancel_task`, `accept_draft`, `reject_draft` |
| 交付 | `get_artifact_manifest`, `export_task_artifacts`, `create_release`, `get_release`, `approve_release` |
| 视觉重建 | `create_reconstruction`, `get_reconstruction`, `run_reconstruction`, `answer_reconstruction`, `adopt_reconstruction`, `reject_reconstruction`, `delete_reconstruction` |
| direct operation | `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part` |

这七个视觉重建工具是可选的 VibeCAD-managed 生命周期，用于 sealed 本地 ImageSet、持久恢复、澄清
问答，以及采纳为普通待审核 CAD Task；默认 composition 仍是 deterministic fake。非 MCP 本地主机
适配器可通过一个已认证的 staging-directory descriptor 封存一至十六张 JPEG/PNG；JSON wire 不包含
路径、文件名、base64 或图片字节。宿主多模态模型已经能看图时不需要这条 store/provider 路径。
WorkBuddy 附件直接进入 VibeCAD sealed store 仍未验证，MCP 接口也不接受图片路径、base64 内容或
visual Resource URI。

一次成功的 `export_task_artifacts` 返回规范结果及两个有类型的 `ResourceLink`：

- FCStd：`application/vnd.freecad.fcstd`；
- STEP：`model/step`。

宿主只能通过返回的 URI 调用 `resources/read` 获取二进制内容，并核对格式、大小与 SHA-256。接口不
提供任意路径导出或任意文件读取。

对于已经验收的 Revision，`create_release` 会生成可预览的 A3 装配 PDF、扁平 BOM、manifest、
validation report 和不可变的七文件交付 ZIP。宿主必须先向用户展示精确 ZIP SHA-256，随后才能调用
`approve_release`；只有批准后的 Release 才公开 ZIP ResourceLink。Release 批准独立于 Revision
验收，且绝不改变项目 HEAD。

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
| WorkBuddy | — | `.codebuddy/skills/vibecad-agent` |

发布资产中的 `vibecad-agent-skill-0.6.1.zip` 解压后只有一个顶层 `vibecad-agent/` 目录，可整体复制
到上述任一路径。Python wheel 包含服务端和 FreeCAD Workbench 插件，受管运行时包含匹配的服务端
环境；两者都不会自动激活 Agent Skill。

### WorkBuddy（已验证）

安装已发布 CLI，把独立 Skill 目录复制到 `.codebuddy/skills/vibecad-agent`，并在项目
`.mcp.json` 中注册本地 stdio 服务。`command` 应使用 `command -v vibecad` 返回的绝对路径，避免
GUI 依赖 shell 继承的 `PATH`：

```json
{
  "mcpServers": {
    "vibecad": {
      "command": "/absolute/path/to/vibecad",
      "args": [],
      "env": {"VIBECAD_AUTO_INSTALL": "1"}
    }
  }
}
```

WorkBuddy 提示时批准这个项目级服务，等待运行时 ready 后再开始或恢复任务。WorkBuddy 会把二进制
`resources/read` 结果原生保存到项目 `.mcp-resources/`，因此 PDF 与批准后的 ZIP 不需要文件路径
适配层。GLM-5.2 已通过标准多轮任务，但目前只是暂定默认模型：自主 CAD 任务的 allowed tools 应
排除运行时维护工具，`uninstall_runtime` 必须保留显式用户确认。

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

0.6.1 已验证 MCP 协议、Skill 包结构、FCStd/STEP 与 Release ResourceLink、受管 FreeCAD E2E、
31-tool discovery，以及一次真实 WorkBuddy/GLM-5.2 多轮交付。因此在声明的 WorkBuddy 5.3.5
边界内可以称为 `host-verified`。这不等于所有 WorkBuddy 模型都已认证；Kimi-K3、MiniMax-M3 与
DeepSeek-V4-Flash 仍是后续对比候选。

## 架构边界与路线

当前领域链路是 MCP transport/server → same-user authenticated local daemon → single Agent application
→ Task Kernel → CAD execution port → managed killable FreeCAD Worker；public Workbench client 同样从
daemon 进入该 Application/Task Kernel。运行时维护和无状态 discovery 仍由 MCP server 本地处理，不是
第二条领域写入路径。daemon 提供同用户认证及受限的一次性 file grant，不形成第二套提交系统。

S3-8、P0-B core、package/managed-runtime 收口、有界 G1 Workbench Alpha、P1 顺序编辑与 P2
刚性机械交付与首个 WorkBuddy 宿主集成都已在 0.6.1 完成：

- **P0-B core（后端完成）**：任务/项目/版本发现、文件级比较、verified forward revert、取消/reconcile、
  认证 daemon、file grant、source liveness 与受管可终止 FreeCAD Worker 都进入同一 Task Kernel；
- **G1（Alpha 完成）**：真实 FreeCAD Qt Workbench UI 已具备 preview、verdict、精确
  object/feature selector 捕获与 Accept/Reject；一个指纹绑定的外部 FreeCAD 1.1.3 试点已有证据，
  受管模式仍是默认路径；
- **P1/G2（有界完成）**：当前源码已实现窄范围的顺序 editable HEAD/手工 checkpoint；Sketcher/PartDesign、
  受控导入和更广的单零件生产能力仍待后续完成；
- **P2（有界完成）**：2–10 零件刚性装配、干涉验证、扁平 BOM、确定性装配 PDF、不可变 Release
  批准与精确交付 ZIP；原生 joints、可编辑制造图、GD&T、PLM 与企业交付链仍待后续；
- **WorkBuddy（已验证）**：WorkBuddy 5.3.5 + GLM-5.2 已完成严格本地 stdio 工具调用、持久任务/
  重启恢复、精确摘要批准与原生 PDF/ZIP Blob 读取；更广模型对比属于后续证据，不阻塞本次发布。

G1 Workbench Alpha 已把真实 FreeCAD Qt UI 与确定性的受管启动器打入安装包。它具备恰好一个
Workbench 与 Dock、daemon-backed refresh、相互独立的 HEAD/草案预览、verdict、精确
object/feature selector 捕获、Accept/Reject 与异步 client/thread shutdown。daemon 是可复用的受管
后台服务，更新与卸载会通过认证维护路径将其退休。薄外部试点通过一个有界受管 Python bridge 复用
这些状态机，不增加第二写入权威。当前仍不支持 face/edge 选择、STEP/STL import、普适照片重建或
simulation。多模态宿主可通过普通待审核 Task 流程试点受控 image-to-CAD；独立的 VibeCAD-managed
视觉生命周期仍默认使用 deterministic fake Provider，direct cloud transport 是可选非默认路径。

进一步阅读（源代码仓库）：
[用户手册](https://github.com/wangtao9090/VibeCAD/blob/main/docs/USER_GUIDE.md)、
[验收测试](https://github.com/wangtao9090/VibeCAD/blob/main/docs/ACCEPTANCE_TESTS.md)、
[整体架构](https://github.com/wangtao9090/VibeCAD/blob/main/docs/ARCHITECTURE.md)、
[Agent 架构](https://github.com/wangtao9090/VibeCAD/blob/main/docs/AGENT_ARCHITECTURE.md) 和
[产品能力路线图](https://github.com/wangtao9090/VibeCAD/blob/main/docs/PRODUCT_CAPABILITY_ROADMAP.md)。
产品定位、开源策略、多 CAD Backend、AutoCAD/国产 CAD 路线和评测体系的统一决策见
[综合产品与技术战略](https://github.com/wangtao9090/VibeCAD/blob/main/docs/PRODUCT_STRATEGY.md)。

## License

[MIT](LICENSE)
