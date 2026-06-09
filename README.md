# Vibe CAD

**AI 对话式 CAD —— FreeCAD MCP 连接器（Chat-native 零安装）**

给一年只设计几次东西的人：免费的 FreeCAD + 一个让你永远不用学 FreeCAD 的 AI。
在任意 MCP 客户端（Claude Desktop / Cowork / OpenClaw / Cursor…）中，用自然语言完成
中等复杂度的参数化设计与多零件装配，交付可制造文件（3D 打印 3MF/STL、CNC STEP、激光 DXF）。

> **状态**：M1 运行时安装器 + Round 2 语义建模 Walking Skeleton 已落地（A1/A2/A3 地基 + 产品核心闭环 box→cut→导出 STEP/STL 本机 macOS arm64 实跑验证通过）。设计文档见
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

## 语义建模工具（Round 2）

运行时就绪并重连后，用以下语义工具完成「自然语言 → 参数化单零件 → 可制造文件」：

| 工具 | 作用 |
|---|---|
| `new_document(name)` | 新建单零件工作文档 |
| `add_box(length, width, height, position=[x,y,z])` | 参数化长方体（Part::Box，mm）；`position` 放置位置（默认原点） |
| `add_cylinder(radius, height, position=[x,y,z], axis="z")` | 参数化圆柱（Part::Cylinder，mm）；`position` 放置位置、`axis=x\|y\|z` 轴向（可贯穿不同面，配合居中 position 打正中孔） |
| `boolean_cut(base_name, tool_name)` | 布尔差集（Part::Cut）：从 base 减去 tool |
| `export_part(output_dir, fmt="both")` | 导出可制造文件 STEP/STL（fmt: step\|stl\|gltf\|both\|all；all 含 glTF） |
| `describe_part()` | 文本诊断：体积/包围盒/质心/实体数/有效性 |
| `render_part(view="iso")` | **PNG 预览图**（view: iso\|front\|top\|right\|back），MCP 客户端内联显示 |

每个工具 = 参数校验 → 文档事务 → 参数化对象 → recompute → **几何断言**（`recompute()` 返回值不可信，几何断言是唯一可信成功判据）→ 结构化返回。所有工具守卫「运行时就绪 + 已重连进 conda 解释器」。

开发态集成测试复用持久 FreeCAD env：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow`（首次自动装到 `.vibecad-test-runtime/`，约 4 分钟，后续秒进）。

### 视觉反馈（Round 3）

- `render_part` 回传 **PNG 软渲染图**（matplotlib Agg，无 GPU），作为 MCP `ImageContent` 在 Claude Desktop 等客户端**内联可见**——用户据此迭代式画图。
- `export_part(fmt="gltf"/"all")` 导出 **glTF（.glb）工件**（逐面 primitive + 面级 extras），供未来会渲 glTF 的 App/客户端交互旋转拾取。
- 注：当前为 MVP 级渲染（看清形状/朝向/比例，非照片级）；FreeCADGui 高质量离屏渲染留后续。

## License

[MIT](LICENSE)
