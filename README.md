# Vibe CAD

**AI 对话式 CAD —— FreeCAD MCP 连接器（Chat-native 零安装）**

给一年只设计几次东西的人：免费的 FreeCAD + 一个让你永远不用学 FreeCAD 的 AI。
在任意 MCP 客户端（Claude Desktop / Cowork / OpenClaw / Cursor…）中，用自然语言完成
中等复杂度的参数化设计与多零件装配，交付可制造文件（3D 打印 3MF/STL、CNC STEP、激光 DXF）。

> **状态**：M1 运行时安装器已落地（地基假设 A1/A2/A3 本机 macOS arm64 实跑验证通过）。设计文档见
> [`docs/superpowers/specs/2026-06-08-vibecad-design.md`](docs/superpowers/specs/2026-06-08-vibecad-design.md)。

## 首发形态：Chat-native 零安装

用户无需安装 FreeCAD —— MCP server 首次运行自动拉取无头 FreeCAD 运行时（micromamba），
每步回传文本诊断 + 软渲染图 + 可旋转 glTF 交互预览。装 FreeCAD 解锁 Live 模式是升级而非门槛。

## 架构（方案 B：进程内自建）

- **MCP 框架**：Python 官方 MCP SDK（FastMCP），stdio 起步
- **几何引擎**：conda-forge FreeCAD 1.1+ 进程内 `import`
- **运行时分发**：micromamba 自动安装（全平台矩阵）
- **工具面**：纯语义工具（`add_hole`/`fillet_edges`/`assemble`/`fasten`），每工具事务 + 几何断言 + 规则检查
- **反馈三级**：glTF artifact（主）/ 软渲染图 / 纯文本诊断

## 开发

```bash
uv sync            # 安装 server 依赖（不含 FreeCAD）
uv run vibecad     # 启动 MCP server（stdio，先跑纯 stdlib launcher）
uv run pytest      # 跑单元测试（slow 集成测试默认跳过）
uv run ruff check  # 静态检查
```

### FreeCAD 运行时（首次使用）

server 启动即可握手；FreeCAD 运行时（约 2-3GB）按需后台获取，不阻塞握手：

1. 调用 `ensure_runtime` —— 未就绪则后台开始安装（micromamba → conda env `python=3.12 freecad=1.1.0` → pip 装 server → 冒烟），立即返回 `started`。
2. 轮询 `get_runtime_status` 至 `phase=ready`；若返回 `needs_reconnect=true`，表示运行时已就绪但当前会话仍在引导解释器，**请重连本 MCP server**。
3. 重连后调用 `smoke_cad` —— 进程内造 10×10×10 Box、导出 STEP，返回体积/包围盒（证 A1/A3）。

环境变量：
- `VIBECAD_HOME` —— 运行时落盘根目录（默认平台数据目录）
- `VIBECAD_AUTO_INSTALL=1` —— server 启动即自动后台安装（默认需显式 `ensure_runtime`）
- `VIBECAD_FREECAD_ENV=<conda env 路径>` —— 复用现成的 FreeCAD env（只校验不自建）
- `VIBECAD_PIP_SPEC=<本地源/包名>` —— 预发布期指向本地仓库源
- 集成测试：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow`（真实下载 2-3GB）

> 陈旧安装锁：若安装异常中断，手动删除 `<VIBECAD_HOME>/.install.lock`。
> FreeCAD 运行时由 `vibecad.runtime`（D4）按需获取，与本 venv 隔离。

## License

[MIT](LICENSE)
