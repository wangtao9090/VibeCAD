# VibeCAD

[![CI](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml/badge.svg)](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)

VibeCAD 是一个面向 Claude、Codex 等宿主 Agent 的 FreeCAD 专家 Agent。它把自然语言设计意图
落成持久化项目、受约束的 CAD 操作、可审核草案和经过验证的 FCStd/STEP 交付物。

VibeCAD 不内置或转售大模型。推理使用用户自带宿主模型及其订阅或 API 配额；VibeCAD 负责 CAD
领域合同、执行、验证、恢复和交付。

## 当前 Agent-first 工作流

```text
宿主 Agent（用户自带模型）
  → 创建/读取持久化项目
  → 创建带验收条件的任务
  → 提交受约束 ModelProgram，或调用当前六个直接操作
  → FreeCAD 在隔离 checkout 中执行
  → 生成候选 revision 与验证证据
  → 用户接受或拒绝草案
  → 导出并读取校验过的 FCStd / STEP artifact
```

这个流程的目标是服务“从已有样品、草图或模型继续修改”的主要场景；当前只能从空项目或一个
FCStd 文件开始。照片/视频到网格、照片到 STL、2D 草图识别、STEP/STL 导入、逆向工程和仿真
尚未接入，未来可以作为外部引擎或受控导入器连接；VibeCAD 聚焦中间的可编辑 CAD 编排，而
不是自研这些底层引擎。

ModelProgram 是有版本、有限操作集、有限预算的 JSON 合同。任意 Python/FreeCAD 代码生成可以在
未来作为高上限专家通道研究，但不作为当前主执行路径。

## 当前公开能力（0.4.0）

MCPB manifest 与运行时 `tools/list` 由同一冻结合同投影，当前一共 20 个工具：

| 类别 | 工具 |
|---|---|
| 服务与运行时 | `ping`, `get_runtime_status`, `ensure_runtime`, `uninstall_runtime` |
| 能力发现 | `get_capabilities` |
| 项目 | `create_project`, `get_project` |
| 任务与草案 | `create_task`, `get_task`, `submit_model_program`, `resume_task`, `accept_draft`, `reject_draft` |
| 交付 | `export_task_artifacts` |
| 当前直接操作 | `create_box`, `create_cylinder`, `inspect_model`, `modify_parameter`, `move_part`, `rotate_part` |

经发布的交付物通过只读 MCP resource URI 返回。资源描述绑定项目、revision、任务代数、manifest、
内容哈希和大小；候选草案只有在状态与验证证据完全匹配时才可导出。

旧版工具实现仍可作为内部几何引擎积累被后续 Agent compiler 复用，但不再作为当前公开端点。
项目还没有需要兼容旧端点的实际用户，因此公开面以新的 Agent 工作流为准。

## 为什么不是“模型直接写 FreeCAD Python”

FreeCAD 很像 CAD 编译器和执行环境，但“代码能运行”不等于“设计符合意图”。VibeCAD 把一次设计
拆成可恢复的编译和验收过程：

- 输入必须满足公开 schema、选择器和预算；
- 执行发生在隔离 checkout，不修改用户原文件；
- 每次结果形成不可变 revision，并记录基线、候选和验证报告；
- `auto_commit` 在验证通过后发布新的项目 HEAD；`require_review` 只有显式 Accept 才发布，Reject 不改变 HEAD；
- FCStd/STEP 交付前再次校验来源、哈希、大小和任务状态；
- 任务、草案、项目创建和导出都有持久化恢复记录。

这种边界让 Claude、Codex 或其他 MCP Agent 可以组合更复杂的 workflow，同时让 VibeCAD 保持 CAD
专家责任：执行是否安全、结果是否可证、失败是否可回滚。

## 安装与运行时

当前 MCPB 产品声明只覆盖经过验证的 macOS（Darwin）路径。扩展首次启动需要网络来获取 `uv.lock`
锁定的 Python 包，并按需安装约 2–3 GB 的 FreeCAD 运行时；后续启动可复用缓存和已经验证的运行时。

受管运行时默认位于扩展目录之外的系统应用数据目录。在 macOS 上通常是：

```text
~/Library/Application Support/VibeCAD/
```

FreeCAD 子进程使用 VibeCAD 私有的用户配置、数据和临时目录，不读取或污染用户日常 FreeCAD 配置。
调用 `uninstall_runtime` 时先预览，再显式确认；它只清理受管运行时并保留项目数据。之后可在宿主
设置中移除扩展本体。

### 本地开发

```bash
uv sync --frozen
PYTHONPATH=src uv run --frozen pytest
uv run --frozen ruff check .
VIBECAD_AUTO_INSTALL=0 uv run --frozen python -m vibecad.server
```

FreeCAD 不属于普通 Python 依赖，由运行时安装器单独管理。真实运行时集成测试需要显式开启：

```bash
VIBECAD_RUN_INTEGRATION=1 PYTHONPATH=src uv run --frozen pytest -m slow
```

## 架构边界

核心分为五层：

1. MCP transport：严格 JSON-RPC framing、初始化、并发、取消和资源背压；
2. Agent application：项目、任务、草案、直接操作和交付用例；
3. Durable workflow：request key、journal、lease、quota、revision 和恢复；
4. CAD execution port：将受约束命令送入 FreeCAD checkout，并回收结构化事实；
5. Runtime supervisor：安装/验证引擎、进程换芯、有限重放和干净卸载。

当前实施合同见 [Stage 3 执行包](docs/orchestrated/vibecad-agent-stage3.md)，分期边界见
[产品能力路线图](docs/PRODUCT_CAPABILITY_ROADMAP.md)。[Agent 架构](docs/AGENT_ARCHITECTURE.md) 与
[整体架构](docs/ARCHITECTURE.md) 仍包含 S3-7 切换前的历史现状说明，将在紧随其后的 AR-1 中统一。

## 推进路线

当前顺序是 S3-7/P0-A → AR-1/S3-8 → P0-B → G1 → P1 → P2：

- **G1**：FreeCAD Workbench、认证 IPC、候选版本可视化审核和交互式设计；
- **P1**：单零件生产能力，补强 Sketcher/PartDesign、受控导入，以及 STL 到可测量、可布尔、
  部分可编辑 faceted STEP/BRep 的工作流（不宣称恢复原始草图和参数）；
- **P2**：装配、BOM、TechDraw、发布和企业交付链路。

CAD 之前的照片/视频重建适配器和 CAD 之后的仿真适配器会保留架构接口，但不阻塞当前中间过程。
分期能力与企业生产级工具清单见 [产品能力路线图](docs/PRODUCT_CAPABILITY_ROADMAP.md)，多 CAD/引擎
选择见 [后端调研](docs/CAD_BACKEND_RESEARCH.md)。

## 当前限制

- MCPB 首发验证平台为 macOS；Windows/Linux 不在当前产品声明内；
- 当前直接操作只有表中六项，孔、圆角、复杂草图、装配和工程图仍在后续阶段；
- 当前交付格式为 FCStd/STEP；STL 导入/逆向转换和照片重建尚未接入；
- 当前没有仿真、碰撞求解或制造工艺验证；
- G1 Workbench 尚未交付，目前主路径是受管的 Agent/headless 执行。

## License

[MIT](LICENSE)
