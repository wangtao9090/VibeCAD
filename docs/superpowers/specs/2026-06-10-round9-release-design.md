# Round 9 设计：接入与发布（Windows + macOS，Claude Cowork）

**日期**：2026-06-10 ｜ **状态**：设计已批准（用户决策：转公开仓 + PyPI；有 Windows 机器可手动验证）｜ **基线**：main（R1-R8 已合，PR #9 待合——本轮基于 #9 合并后的 main）

## 1. 背景与目标

八轮技术主链路完整，但 **CI 自 R1 起全部 run 停留 queued 零执行**（私有仓 Actions 不可用）——Windows/Linux 链路零验证；产品从未被真实 AI 客户端使用。本轮目标：**用户在 Claude Cowork 中完成配置和使用**，目标平台 Windows + macOS，交付用户手册与测试方案。

## 2. 工作流分解

### 2.1 公开化（前置）
- **敏感信息扫描**（公开前硬门禁）：全仓+git 历史扫 token/密钥/邮箱/内部 URL（飞书文档 ID 属私有链接无泄密风险但逐一确认）；`.vibecad/`、memory 不在仓库（已 gitignore）确认。
- `gh repo edit --visibility public`；补开源门面：README 顶部一句话英文简介 + badges（CI/license）、CONTRIBUTING 占位不做（YAGNI）。

### 2.2 CI 闭环（本轮技术重头，Windows 首验）
- 公开后 Actions 免费可用；推一个空 commit 触发全矩阵。
- **unit job（五平台快测）**预期先绿（纯 Python+mock）；**runtime-integration（三平台真装 2-3GB + 全量 slow）**是 Windows 链路首次真机检验。
- 预期 Windows 修复面（R1 设计已预判）：FreeCAD 扩展模块在 `Library/bin`/`Library/lib` 的 sys.path 注入、DLL 搜索路径（Python 3.8+ 需 `os.add_dll_directory`）、路径分隔符、micromamba .exe 下载与调用、**中文零件名文件导出的编码**（export split `_盖板.step`）。修复迭代直至三平台全绿。
- macOS/Linux 预期小修。CI 红→修→推循环由闭关 agent 持续跟进（每轮看日志定位）。

### 2.3 PyPI 发布
- pyproject 完善：version 0.1.0、description/readme/urls/classifiers/keywords；`uv build` 出 sdist+wheel 本地验证（`uvx --from dist/*.whl vibecad` 握手）。
- 发布通道：**GitHub Actions Trusted Publishing**（PyPI OIDC，免 token，公开仓标配）——建 `release.yml`（tag 触发）+ 指引用户在 PyPI 完成一次 pending publisher 授权（唯一需要用户的 PyPI 账号操作，手册附步骤）；备选：用户提供 API token 本地 `uv publish`。
- 发布后终验：干净环境 `uvx vibecad` 握手 + ensure_runtime 全流程。

### 2.4 用户手册（docs/USER_GUIDE.md，中文，面向最终用户）
1. 一分钟了解（这是什么/能做什么——配 R8 黑盒装配工程图截图）。
2. 安装前提：uv（mac：brew/官方脚本；Windows：powershell 一行）；磁盘 ≥5GB；首次运行下载 2-3GB 运行时的预期管理。
3. **Claude Cowork 配置**：添加自定义 MCP 连接器（stdio，command=`uvx vibecad`）的逐步操作（以 Cowork 当前 UI 为准书写+截图位预留；同时附 Claude Desktop/Code 的等价配置作参照）。
4. 首次对话：ensure_runtime → 轮询 → 重连 → smoke_cad 的引导话术（用户只需说"帮我准备好 CAD 环境"）。
5. 场景化用法（每场景一句示范话术+预期看到什么）：画第一个零件/在指定面打孔（标注图指代）/改尺寸/孔阵列/挖槽/装配两个零件/导出 3D 打印文件。
6. 故障排查：安装锁清理/needs_reconnect/标签过期含义/干涉拒绝含义/Windows 常见问题。

### 2.5 测试方案（docs/ACCEPTANCE_TESTS.md）
- **Cowork 对话验收清单**（用户执行，~12 场景可勾选）：每条 = 编号/你说的话/预期结果（图上看到什么、数字是什么）/通过标准。覆盖：握手、运行时安装、重连、第一个盒子（自动回图）、标注指代打孔、modify 尺寸（图上数字变化）、线性阵列、slot 挖槽、new_part+align 装配、干涉拒绝（保护行为可见）、导出 STEP/STL、错误恢复（说一个不可能的操作看响亮拒绝）。
- **Windows 手动验证附录**（用户的 Windows 机器）：uv 安装→uvx vibecad 握手→ensure_runtime 完整下载→冒烟→跑 3 条核心场景；问题上报格式。

### 2.6 用户真实接入（验收里程碑）
我备好「三步接入卡」（装 uv → Cowork 加连接器 → 第一句话），你在 Cowork 真实使用并按测试方案勾选；暴露的摩擦进修缮清单（本轮内修小项，大项记 R10）。

## 3. 验收

1. 三平台 CI 全绿（unit + runtime-integration）。
2. PyPI 包可 `uvx vibecad` 安装握手（或万事俱备仅差用户 PyPI 授权的"一键可发"状态）。
3. 用户手册+测试方案成文并随仓发布；飞书同步。
4. 用户在 Cowork 完成配置并跑通验收清单首批场景。

## 4. 风险

1. Windows runtime CI 修复轮次不可预估（conda-forge FreeCAD Windows 包行为未知）——预留多轮红绿迭代；用户 Windows 手动验证兜底。
2. Cowork MCP 配置 UI 细节可能与文档写法有出入——手册标注版本与"以实际 UI 为准"，用户接入时实时校正。
3. PyPI trusted publishing 首次配置需用户账号操作——手册给精确步骤，备选 token 通道。
4. 公开化不可逆——敏感扫描为硬门禁。

## 5. 范围纪律（不做）

英文完整文档（README 英文简介即可）/文档站（Markdown 仓内文档够用）/Linux 用户手册（CI 验证但不写手册）/视频教程/新建模功能。
