# Vibe CAD

**AI 对话式 CAD —— FreeCAD MCP 连接器（Chat-native 零安装）**

给一年只设计几次东西的人：免费的 FreeCAD + 一个让你永远不用学 FreeCAD 的 AI。
在任意 MCP 客户端（Claude Desktop / Cowork / OpenClaw / Cursor…）中，用自然语言完成
中等复杂度的参数化设计与多零件装配，交付可制造文件（3D 打印 3MF/STL、CNC STEP、激光 DXF）。

> **状态**：脚手架阶段（M1 启动前）。设计文档见
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
uv run vibecad     # 启动 MCP server（stdio）
uv run pytest      # 跑测试
uv run ruff check  # 静态检查
```

> FreeCAD 运行时由 `vibecad.runtime`（D4）按需获取，与本 venv 隔离。

## License

[MIT](LICENSE)
