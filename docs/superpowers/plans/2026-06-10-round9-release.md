# VibeCAD Round 9 — 接入与发布 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps 用 `- [ ]` 勾选。本轮为工程+文档轮：T1/T2 由控制者主导（外向敏感操作与 CI 长循环监控），T3/T4/T5 可派 agent 并行。

**Goal:** 仓库公开 + CI 三平台首次闭环（Windows 首验）+ PyPI 可发布 + 用户手册/测试方案成文 → 用户在 Claude Cowork 完成配置使用。

**Architecture:** 公开化解锁免费 Actions → unit（五平台快测）先行验绿 → runtime-integration（三平台真装+全量 slow）红绿迭代修 Windows 链路 → PyPI trusted publishing 就绪 → 双文档交付 → 用户接入。

**Tech Stack:** GitHub Actions（公共仓）、PyPI Trusted Publishing（OIDC）、uv build/publish。

**Spec:** `docs/superpowers/specs/2026-06-10-round9-release-design.md`

---

## Task 1：敏感扫描 + 公开化（控制者执行，硬门禁）

- [ ] **Step 1: 敏感信息扫描**（公开前必须全部通过）：
  - `git log --all -p | grep -iE 'token|secret|password|api[_-]?key|ghp_|xox' `（排除误报词：LabelExpired 等）
  - 全文件扫邮箱/内网地址：`grep -rn "wangtao9090@\|192\.168\.\|10\.0\." --include="*.py" --include="*.md" .`（README/pyproject 的作者邮箱属正常公开信息，确认用户接受或改 noreply）
  - 确认 `.gitignore` 覆盖 `.vibecad/`、`.vibecad-test-runtime/`、`*.lock` 临时物；git 历史无误提交的二进制/凭证。
  - 飞书文档 ID（docs/ 多处）：私有租户链接，外人不可访问——确认保留（过程文档透明是开源加分项）或批量清理（决策：保留，无泄密面）。
- [ ] **Step 2: 公开门面**：README 顶部加一行英文简介 + CI badge（`[![CI](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml/badge.svg)]`）+ License badge；pyproject 检查 license/readme 字段。commit。
- [ ] **Step 3: 公开化**：`gh repo edit wangtao9090/VibeCAD --visibility public --accept-visibility-change-consequences`；确认 Actions 启用（`gh api repos/wangtao9090/VibeCAD/actions/permissions`）。

## Task 2：CI 三平台闭环（控制者监控 + 修复 agent 迭代）

- [ ] **Step 1**: 空 commit 触发或 `gh workflow run`；先盯 **unit job**（五平台快测，~5min）——预期绿（纯 Python）；红则按日志修（多半是平台路径/编码小问题）。
- [ ] **Step 2**: **runtime-integration**（ubuntu/macos/windows 真装 2-3GB + 全量 `-m slow`，45min 超时）。已预判 Windows 风险清单（修复 agent 按日志逐一核）：
  1. `status._PREP`/`prepare_freecad_import` 的 `Library/bin`/`Library/lib` 注入是否真实生效（R1 写了但从未验证）；
  2. Python 3.8+ Windows DLL 解析需 `os.add_dll_directory(Library/bin)`（仅 sys.path 不够——FreeCAD.pyd 依赖的 OCCT DLL）；
  3. micromamba Windows：.exe 下载 URL/解压（tar.bz2 in win64）/调用（无 shell）；
  4. 中文零件名导出（`asm_盖板.step`）的文件系统编码与 STEP writer 行为；
  5. 路径分隔符/长路径/临时目录权限；conftest 的 runtime env 路径。
  6. slot 弧/工程图渲染在 headless Windows 的 matplotlib 字体（CJK 字体 fallback 全缺→标题豆腐块但测试不看图，无碍）。
- [ ] **Step 3**: 红绿迭代：每轮 `gh run view <id> --log-failed` 取日志 → 派修复 agent（带日志+风险清单）→ 推送 → 重触发。直至三平台 runtime-integration 全绿。**每轮修复必须保持本机 302 fast + 65 slow 全绿**（平台修复不破 mac）。
- [ ] **Step 4**: 用户 Windows 机器手动验证作为 CI 的独立佐证（测试方案附录提供清单，可在 CI 绿后或并行做）。

## Task 3：PyPI 打包与发布通道（可派 agent，与 Task 2 并行）

- [ ] **Step 1**: pyproject 完善——version `0.1.0`、`description`（英文一句话）、`readme = "README.md"`、`license`、`authors`、`keywords = ["cad", "freecad", "mcp", "ai"]`、`classifiers`（Python 3.12+/MIT/Topic CAD）、`[project.urls]`（Homepage/Repository）。确认 `[project.scripts] vibecad = ...` 入口无恙。
- [ ] **Step 2**: 本地构建验证：`uv build` → `dist/*.whl` + sdist；干净验证 `uvx --from dist/vibecad-0.1.0-py3-none-any.whl vibecad` 握手（stdio 起得来即过，Ctrl-C 退）；`uv run --with dist/...whl python -c "import vibecad; print(vibecad.__version__)"`。
- [ ] **Step 3**: `.github/workflows/release.yml`（tag `v*` 触发：uv build → `pypa/gh-action-pypi-publish@release/v1`，OIDC trusted publishing，无 secrets）：

```yaml
name: Release
on:
  push:
    tags: ["v*"]
jobs:
  pypi:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - run: uv build
      - uses: pypa/gh-action-pypi-publish@release/v1
```

- [ ] **Step 4**: 用户侧一次性授权指引（写进手册附录与汇报）：PyPI 注册/登录 → Account → Publishing → Add pending publisher（project `vibecad` / owner `wangtao9090` / repo `VibeCAD` / workflow `release.yml` / environment `pypi`）。完成后控制者打 tag `v0.1.0` 即自动发布；发布后干净机 `uvx vibecad` 终验。**未授权前的状态即"一键可发"，不阻塞其余交付。**

## Task 4：用户手册 `docs/USER_GUIDE.md`（派 agent）

- [ ] 按 spec §2.4 六章结构成文（中文，面向不懂 CAD/不懂命令行的最终用户，每步给精确命令/话术）：
  1. **这是什么**：两段话 + 能力清单（画零件/打孔改尺寸/装配/导出 3D 打印），配一张装配工程图示例（引用 docs/images/——把 R8 黑盒图存入仓库 docs/images/assembly-example.png）。
  2. **安装前提**：uv 安装（macOS `curl -LsSf https://astral.sh/uv/install.sh | sh`；Windows PowerShell `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`）；磁盘 ≥5GB；首次运行自动下载 FreeCAD 运行时 2-3GB（几分钟，仅一次）。
  3. **在 Claude Cowork 中配置**：添加自定义连接器/本地 MCP（stdio）——command `uvx`，args `vibecad`（PyPI 发布前的本地替代：`uvx --from /路径/VibeCAD vibecad`）；逐步骤编号，预留截图位 `<!-- screenshot -->`；附 Claude Desktop（claude_desktop_config.json 片段）与 Claude Code（`claude mcp add` 命令）等价配置。
  4. **第一次使用**：示范话术"帮我准备好 CAD 环境"→ AI 调 ensure_runtime/轮询/提示重连 → 重连后"画一个 60×40×10 的底板"。
  5. **场景手册**（每场景：你说的话/会看到什么/小贴士）：指定面打孔（看标注图说"在 F 面正中打 8mm 孔"）/改尺寸（"长度改成 80"）/孔阵列（"打 4 个孔间距 15"）/挖槽（"顶面挖个 20×8 的槽深 5"）/圆角（"E3 这条边倒 3mm 圆角"）/装配（"新建零件盖板…把盖板底面贴到底板顶面"）/导出（"导出 STL 给 3D 打印"）。
  6. **故障排查**：运行时下载中断（删 `.install.lock` 路径按平台写全）/needs_reconnect 含义/"标签已过期"是保护不是故障/"干涉"拒绝含义/Windows 防火墙与杀软提示/如何彻底重装（删 VIBECAD_HOME 目录）。
- [ ] 控制者审稿（产品视角逐章过）后 commit。

## Task 5：测试方案 `docs/ACCEPTANCE_TESTS.md`（派 agent，与 T4 并行）

- [ ] 按 spec §2.5 成文：
  - **A 部分 Cowork 对话验收（12 场景表格）**：列=编号/场景/你说的话/预期结果/通过☐。场景：A1 握手（连接器列表见 vibecad）/A2 运行时安装（几分钟进度）/A3 重连+冒烟/A4 第一个盒子（自动收到工程图，尺寸 60·40·10 可读）/A5 标注指代打孔（图上 ⌀8+定位居中）/A6 改尺寸（图上数字 60→80 当场变）/A7 线性阵列 4 孔（定位链等距）/A8 slot 挖槽（俯视跑道轮廓）/A9 装配（新零件+贴面，工程图两零件分色）/A10 干涉保护（"把盖板往下压 2mm"→ 拒绝并报干涉量）/A11 导出（STEP/STL 文件落盘路径）/A12 错误恢复（"在孔壁上打孔"→ 响亮拒绝且对话可继续）。
  - **B 部分 Windows 手动验证附录**：环境（Win10/11 x64）/步骤（装 uv→`uvx --from git+https://github.com/wangtao9090/VibeCAD vibecad` 或 PyPI 后 `uvx vibecad` 握手→Cowork 配置→跑 A2/A4/A5/A9/A11 五条核心）/已知预期差异（CJK 字体可能缺→图标题豆腐块不影响功能）/问题上报模板（场景号/说的话/看到的/期待的/server 日志位置）。
- [ ] 控制者审稿后 commit。

## Task 6：交付与收尾（控制者）

- [ ] 「三步接入卡」交付用户（消息形式）：①装 uv（一行命令）②Cowork 加连接器（command/args）③第一句话。等待用户实测反馈，摩擦小项当场修。
- [ ] README 链接两份新文档；飞书同步 spec/计划/USER_GUIDE/ACCEPTANCE_TESTS 四份；memory R9 状态；最终汇报（含 PyPI 授权指引与 Windows 验证清单指引）。

---

## 风险

1. Windows runtime CI 修复轮次未知（首验）——红绿循环预算放宽，用户 Windows 手动验证兜底。
2. Cowork UI 步骤与手册可能有出入——用户接入时实时校正手册。
3. PyPI 名称 `vibecad` 可能被占——Step 2 先 `pip index versions vibecad`/查 pypi.org 确认，被占则定备名（`vibecad-mcp`）并全链路改名。

## Verification

1. CI 三平台 unit+runtime 全绿（badge 绿）。
2. `uvx --from dist/*.whl vibecad` 本地握手 + release.yml 就绪（一键可发）。
3. USER_GUIDE/ACCEPTANCE_TESTS 成文入仓+飞书。
4. 用户 Cowork 接入跑通 A 部分首批场景。
