# Round 10 设计：一键安装（MCPB 桌面扩展）+ 官方目录上架

**日期**：2026-06-10 ｜ **状态**：设计已批准（用户决策：A 必做 + C 追加，"C 通过了就算白嫖"；方案 B 终端脚本不做）｜ **基线**：main（R1-R9 已合，PyPI 0.1.0 已发布）

## 1. 背景与目标

R9 实测暴露接入摩擦：Claude Desktop/Cowork 的 "Add custom connector" UI 只支持远程 URL，本地 stdio 服务器只能手工编辑 `claude_desktop_config.json`——且 GUI 应用 PATH 不含 `~/.local/bin`，必须写 uvx 绝对路径。用户明确要求：**"要有让用户方便的安装配置方式，不要用改配置文件的方式"**。

目标：最终用户（不懂命令行/不会编辑 JSON）通过**下载 + 双击**完成 VibeCAD 安装，目标平台 macOS + Windows。

## 2. 方案调研结论（2026-06 实查）

- **MCPB（MCP Bundle，`.mcpb`）**：Anthropic 官方一键安装格式——zip 包含 manifest.json + 服务器代码，用户双击后 Claude Desktop 弹窗确认安装；管理/卸载在 Settings → Extensions 图形界面。
- **`uv` 扩展类型（manifest_version 0.4+）**：包内只放 `pyproject.toml` + 源码（**不得**打包 server/lib 或 venv，约几百 KB），**宿主（Claude Desktop）自动用 uv 管理 Python 与依赖**——用户连 uv 都不用预装，比 R9 的接入方式还少一个前置。
- **官方连接器目录（Connectors Directory）**：提交审核（工具标注/隐私政策/可运行示例）后用户可在 Claude 设置内直接浏览安装；周期不可控，作追加不作本轮主交付。
- 三方案对比（A=MCPB / B=终端一键脚本 / C=目录上架）已向用户呈现，决策 **A+C**。

## 3. 用户最终体验（验收基准）

新用户三步：① GitHub Release 页（README 顶部引流）下载 `VibeCAD.mcpb` → ② 双击，Claude Desktop 弹窗"VibeCAD"，点安装 → ③ 新开对话说"帮我准备好 CAD 环境"。全程无终端、无配置文件、无 uv 预装。FreeCAD 运行时照旧首次对话自动下载 2-3GB（体验不变）。

## 4. 交付物

### 4.1 `.mcpb` 安装包（A 主体）
- 仓库根 `manifest.json`（mcpb 打包惯例，配 `.mcpbignore` 控制内容物）：`server.type = "uv"`、entry_point 指向 vibecad 启动入口、名称/描述/图标/作者/license、`compatibility.platforms = ["darwin", "win32"]`（linux 可加但不手测）、22 个工具列表与行为标注；**无 user_config**（零配置表单）。
- 打包脚本/CI 步骤：`mcpb pack` 产出 `VibeCAD.mcpb`；`mcpb validate` 进 CI 防 manifest 回归。
- 包内容物 = manifest.json + 仓库 `pyproject.toml` + `src/`（uv 类型规范），`.mcpbignore` 排除测试/文档/runtime 缓存。

### 4.2 发布流水线扩展
- `release.yml` 扩展：tag `v*` 触发现有 PyPI 发布 **+** `mcpb pack` **+** 自动创建 GitHub Release 并附 `VibeCAD.mcpb`。两渠道一次 tag 同步更新。
- 发布演练：打预发 tag 全链路走通后才宣布。

### 4.3 手册与验收方案改版
- USER_GUIDE 第二、三章合并为"下载 → 双击 → 开聊"主路径；"装 uv + 配置文件"降级为附录（Claude Code/Cursor 等其他 MCP 客户端用户）；更新故障排查（扩展更新方式=重新双击新版，Claude Desktop 暂无扩展自动更新）。
- ACCEPTANCE_TESTS A1/A2 入口同步改写（A1 = 双击安装后连接器可见）。
- README 快速开始改为下载按钮引流 + Release 链接。

### 4.4 官方目录上架材料（C 追加）
- `PRIVACY.md`：核心事实——所有几何数据本地处理不上传；首次运行从官方源（conda-forge/GitHub）下载 FreeCAD 运行时；不收集遥测。
- 工具行为标注（annotations：只读/写盘/破坏性）补齐 22 个工具。
- 按官方提交清单（工具标注/隐私政策/可运行示例/测试说明）提交审核。审核期间不阻塞 GitHub 渠道使用；**审核结果不作为本轮验收条件**。

## 5. 最大技术风险与 Task 0 先行验证

`uv` 扩展类型较新，**写任何实现前先做最小 spike 包真机验证**：
1. 手工 `mcpb pack` 一个最小 uv 类型包 → **用户自己双击安装**（遵守"不替用户配置本机"约定）→ 确认当前版 Claude Desktop 识别 uv 类型并能拉起 server。
2. 验证 vibecad launcher 的 re-exec（引导解释器 → conda python）在扩展宿主环境下正常（PATH/环境变量/工作目录差异）。
3. 任一不成立 → **回退方案：Node 包装器类型**（Claude Desktop 内置 Node 运行时，包装器 spawn Python；成熟但多一层）。
4. 顺带确认：未签名包的安装提示文案、Cowork 与 Desktop 扩展共享情况、扩展的日志位置（故障排查手册要写）。

## 6. 测试策略

- 快测：manifest 生成/内容校验逻辑 + `mcpb validate` 进 CI。
- 真机验收：用户双击安装后跑 ACCEPTANCE_TESTS A1-A4 首批场景（macOS + 用户 Windows 机器各一遍）。
- 发布演练：预发 tag 全链路（PyPI + GitHub Release + 下载 .mcpb 安装）走通。
- 既有硬线：302 fast + 65 slow 全绿不回归（mcpb 不改 server 代码路径；若 Task 0 暴露 launcher 适配需求则按几何断言纪律加测试）。

## 7. 验收

1. `VibeCAD.mcpb` 在 macOS + Windows 双击安装成功，连接器可见 22 工具，跑通 A1-A4。
2. 一次 tag 同步产出 PyPI 包 + GitHub Release 附件。
3. 手册/验收方案/README 改版入仓 + 飞书。
4. 上架材料齐备并完成提交（审核通过不作为验收条件）。

## 8. 范围纪律（不做）

终端一键脚本（B）／扩展自动更新机制（手册注明重新双击即可）／Linux 手动验证（CI 覆盖即可）／上架后的目录运营与推广／新建模功能／serverInfo.version 修正除外——顺手小项随轮带上（FastMCP version 参数改报包版本 0.1.x）。
