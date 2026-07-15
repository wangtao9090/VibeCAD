# Vibe CAD

[![CI](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml/badge.svg)](https://github.com/wangtao9090/VibeCAD/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/vibecad)](https://pypi.org/project/vibecad/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)

**AI-native conversational CAD — an open-source MCP connector for FreeCAD (chat-native, zero-install).**

**AI 对话式 CAD —— FreeCAD MCP 连接器（Chat-native 零安装）**

给一年只设计几次东西的人：免费的 FreeCAD + 一个让你永远不用学 FreeCAD 的 AI。
在任意 MCP 客户端（Claude Desktop / Cowork / OpenClaw / Cursor…）中，用自然语言完成
中等复杂度的参数化设计与多零件装配，交付可制造文件（3D 打印 STL、CNC STEP、glTF 交互预览）。

> **状态**：R1 运行时 → R2 语义建模 → R3 视觉反馈 → R4 位置控制 → R5 可指代性 → R6a 工程图+每步回图 → R6b 参数修改 → R7 阵列/拉伸/重定位 → R8 装配 DSL（多零件/贴面对齐/干涉守卫）→ **R11 自动安装 + 零重连换芯 + 干净卸载**（当前 **0.3.0**）——已全部本机实跑验证。设计文档见
> [`docs/superpowers/specs/2026-06-08-vibecad-design.md`](docs/superpowers/specs/2026-06-08-vibecad-design.md)。

## 快速开始（用户）

**Claude Desktop / Cowork（推荐）**：去 [Releases 最新版](https://github.com/wangtao9090/VibeCAD/releases/latest) 下载 [`VibeCAD.mcpb`](https://github.com/wangtao9090/VibeCAD/releases/latest)，**双击安装**——Claude Desktop 弹出安装窗口后点安装即可，零终端、零配置文件、无需预装 uv/Python（宿主自动准备隔离 Python 环境）。**装好后直接开始说话即用**：第一次对话建模引擎就已经在后台自动下载（约 2-3GB，仅一次），装好后当场自动切换、无需重启对话，期间可随时问"装到哪了"。逐步图文教程见 **[用户手册](docs/USER_GUIDE.md)**。

> 其他客户端（Claude Code / Cursor 等）走 stdio 方式：装好 [uv](https://docs.astral.sh/uv/) 后 `claude mcp add --transport stdio vibecad -- uvx vibecad`（已发布 [PyPI](https://pypi.org/project/vibecad/)），详见用户手册附录 A。

## 首发形态：Chat-native 零安装

用户无需自行安装 FreeCAD —— MCP server 首次运行会通过 micromamba 自动安装无头 FreeCAD 运行时。
每个成功建模步骤回传结构化文本结果和 PNG 软渲染图/工程图；如需 glTF，可调用
`export_part(fmt="gltf")` 或 `export_part(fmt="all")` 按需导出，再交给外部查看器使用。

## 架构（方案 B：进程内自建）

- **MCP 框架**：Python 官方 MCP SDK（FastMCP），stdio 起步
- **几何引擎**：conda-forge FreeCAD 1.1+ 进程内 `import`
- **运行时分发**：micromamba 自动安装（全平台矩阵）
- **工具面**：纯语义工具（`add_hole`/`fillet_edges`/`new_part`/`place_part`/`align_parts`），每工具事务 + 几何断言 + 规则检查
- **反馈**：每步结构化文本 + PNG 软渲染图/工程图；glTF 由 `export_part` 按需导出

## 开发

```bash
uv sync            # 安装 server 依赖（不含 FreeCAD）
uv run vibecad     # 启动 MCP server（stdio，先跑纯 stdlib launcher）
uv run pytest      # 跑单元测试（slow 集成测试默认跳过）
uv run ruff check  # 静态检查
```

### FreeCAD 运行时（首次使用）

server 启动即可握手；FreeCAD 运行时（约 2-3GB）按需后台获取，不阻塞握手：

1. 调用 `ensure_runtime` —— 未就绪则后台开始安装（micromamba → conda env `python=3.12 freecad=1.1.0` → pip 装 server → 冒烟），立即返回 `started`。`.mcpb` 扩展场景设了 `VIBECAD_AUTO_INSTALL=1`，server 启动即自动调用，无需显式触发。
2. 轮询 `get_runtime_status` 至 `phase=ready`。
3. 运行时就绪后监督进程（`vibecad.supervisor`）自动把子进程从引导解释器切到 conda 运行时解释器（"换芯"），**客户端零感知，不需要重连或重启对话**；切换完成后直接调用 `smoke_cad` —— 进程内造 10×10×10 Box、导出 STEP，返回体积/包围盒（证 A1/A3）。极少数不可换芯的场景（如裸 `python -m vibecad.server` 直跑、无监督进程）会诚实回退提示重连，非标准安装路径下才会遇到。

环境变量：
- `VIBECAD_HOME` —— 运行时落盘根目录（默认平台数据目录；卸载删除范围即此目录）
- `VIBECAD_AUTO_INSTALL=1` —— server 启动即自动后台安装（默认需显式 `ensure_runtime`；`.mcpb` manifest 固定开启，手动 `uvx vibecad` 场景默认关闭）
- `VIBECAD_FREECAD_ENV=<conda env 路径>` —— 复用现成的 FreeCAD env（只校验不自建）
- `VIBECAD_PIP_SPEC=<本地源/包名>` —— 预发布期指向本地仓库源
- 集成测试：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow`（真实下载 2-3GB）

> 陈旧安装锁：若安装异常中断，手动删除 `<VIBECAD_HOME>/.install.lock`。
> FreeCAD 运行时由 `vibecad.runtime`（D4）按需获取，与本 venv 隔离。
> 不再需要？调用 `uninstall_runtime`（两段式：先预览再 `confirm=true`）或命令行 `vibecad --uninstall`；详见下方「卸载」。

## 语义建模工具（Round 2）

运行时就绪后（就绪即自动切入 conda 解释器，无需重连），用以下语义工具完成「自然语言 → 参数化单零件 → 可制造文件」：

| 工具 | 作用 |
|---|---|
| `new_document(name)` | 新建单零件工作文档 |
| `add_box(length, width, height, position=[x,y,z])` | 参数化长方体（Part::Box，mm）；`position` 放置位置（默认原点） |
| `add_cylinder(radius, height, position=[x,y,z], axis="z")` | 参数化圆柱（Part::Cylinder，mm）；`position` 放置位置、`axis=x\|y\|z` 轴向（可贯穿不同面，配合居中 position 打正中孔） |
| `boolean_cut(base_name, tool_name)` | 布尔差集（Part::Cut）：从 base 减去 tool |
| `export_part(output_dir, fmt="both", split=False)` | 导出可制造文件 STEP/STL（fmt: step\|stl\|gltf\|both\|all；all 含 glTF）；`split=True` 装配体按零件拆分各导一份 |
| `describe_part()` | 文本诊断：体积/包围盒/质心/实体数/有效性 |
| `render_part(view="iso", annotate=None, edges_of=None, save_to=None)` | **PNG 预览图**（view: iso\|front\|top\|right\|back\|**multi**）。`view="multi"` 出**工程图三视图拼图**（线框+虚线隐藏线+尺寸+⌀+中心线 + 标注版 iso 格）；`annotate="faces"` 出**面标注图**（A/B/C 标签+尺寸线）+ 标签表；`annotate="edges"` 出边标注图（E1…，`edges_of="A"` 只画 A 面的边）；`save_to=<绝对路径>` 另存 PNG 到指定文件（每步建模自动落盘到 `view_file` 无需此参数，仅需要另存别处时才用） |
| `add_hole(face, diameter, depth=None, offset=[u,v], pattern=None)` | **在指定面打圆孔**（face=面标签）；depth 省略=通孔；offset 面内偏移；`pattern={"type":"linear","count":4,"spacing":10}` 或 `{"type":"circular","count":6,"radius":12}` **孔阵列**（全有全无） |
| `fillet_edges(edges, radius)` | **圆角**（edges=边标签列表，如 `["E1","E2"]`） |
| `chamfer_edges(edges, size)` | **倒角**（边标签列表） |
| `move_part(name, position)` | **移动图元**到绝对位置（孔刀具/基体；特征随依赖链重算，破坏孔完整性/封闭内腔的移动响亮拒绝） |
| `rotate_part(name, axis, angle)` | **旋转图元**（绕自身包围盒中心；适用于无特征链对象，带特征整体旋转留装配轮） |
| `extrude_profile(profile, height, face, offset, operation)` | **自由轮廓拉伸**：profile DSL（rect/circle/polygon/slot）在指定面 pad 加料/pocket 挖槽；体积双边核算（打穿/越界响亮拒绝） |
| `new_part(name)` | **新建装配零件**并设为活动零件（既有工具默认作用于活动零件；首个零件自动命名 Part1） |
| `place_part(part, position, rotation_axis, angle)` | **零件级位姿**：整个零件（含全部特征）移动/旋转——孔随零件走 |
| `align_parts(moving_part, moving_face, target_part, target_face, offset, gap, allow_interference)` | **面贴面对齐**（跨零件面标签指代）；装配后自动干涉检查，重叠响亮拒绝（allow 显式豁免压配） |
| `modify_part(name, parameter, value)` | **参数修改**（如 `("Box","length",45)`、`("HoleTool","radius",5)`）——FreeCAD 依赖链自动重算，工程图尺寸当场更新；可改对象与参数见每步返回的 `parts` 字段；带漂移/孔完整性/单实体几何断言，危险修改（吞件/孔变缺口/切两半）响亮拒绝并回滚 |
| `uninstall_runtime(confirm=False)` | **卸载 CAD 引擎**（删除全部已下载运行时，约 2-3GB）：不带 `confirm` 仅预览路径与大小，`confirm=true` 才真正执行删除；删除在幕后自动完成，无需手动重启（详见「卸载」） |

每个工具 = 参数校验 → 文档事务 → 参数化对象 → recompute → **几何断言**（`recompute()` 返回值不可信，几何断言是唯一可信成功判据）→ 结构化返回。所有工具守卫「运行时就绪 + 已切入 conda 解释器」，切换过程自动完成。

开发态集成测试复用持久 FreeCAD env：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow`（首次自动装到 `.vibecad-test-runtime/`，约 4 分钟，后续秒进）。

### 视觉反馈（Round 3）

- `render_part` 回传 **PNG 软渲染图**（matplotlib Agg，无 GPU），作为 MCP `ImageContent` 在 Claude Desktop 等客户端**内联可见**——用户据此迭代式画图。
- `export_part(fmt="gltf"/"all")` 导出 **glTF（.glb）工件**（逐面 primitive + 面级 extras），供未来会渲 glTF 的 App/客户端交互旋转拾取。
- 注：当前为 MVP 级渲染（看清形状/朝向/比例，非照片级）；FreeCADGui 高质量离屏渲染留后续。

### 可指代性：标注图 + 标签注册表（Round 5）

没有鼠标拾取的对话式 CAD，靠**标注渲染图**让用户精确指代几何：

- `render_part(annotate="faces")` → 图上可见面贴 **A/B/C 标签** + 包围盒尺寸线；同时返回**标签表**（`{"A": "顶面·平面 面积1200mm² …"}`）给 AI 读——用户说"顶面打孔"，AI 翻译成 `add_hole(face="A")`；不可见面注明在哪个视角可见（或"请直接用描述指代"）。
- `render_part(annotate="edges")` → 边标 E1/E2…（背面边虚线+表注），供 `fillet_edges`/`chamfer_edges` 指边。
- **标签过期保护（指纹校验）**：FreeCAD 的面/边索引在布尔后会重排——每次标注存几何指纹快照，特征工具执行前按指纹找回；几何已变对不上则**响亮报"标签已过期，请重新标注"，绝不静默猜面**。几何变更后的工具返回带 `labels_stale: true` 提示，引导刷新标注图。
- 事务回滚：任何特征失败（孔落空、OCCT 圆角失败、标签过期）都完整回滚，不残留垃圾对象。

### 工程图三视图与每步回图（Round 6a）

- **每次建模/特征指令成功后自动附一张工程图拼图**（`add_box`/`add_cylinder`/`boolean_cut`/`add_hole`/`fillet_edges`/`chamfer_edges` 返回 `[结果, 图]`）——用户"说一句看一眼"，无需手动调渲染；标签表当场刷新（`labels` 字段），不再需要 stale 提示。
- 拼图 2×2：**front/right/top 三格工程图**（FreeCAD TechDraw HLR 隐藏线消除：可见轮廓实线、隐藏轮廓虚线、圆孔红色点划中心线 + `⌀` 标注 + 定位尺寸、各视图总尺寸从投影自动推导）+ **iso 立体格**（面片渲染 + 面标签 + L/W/H 尺寸线）。
- 附图失败不连坐：建模操作已成功提交时渲染异常只追加 `render_error` 字段，绝不把成功操作报成失败。
- 每步返回还带 **`parts` 参数清单**（`{"Box": {"length": 40, …}, "HoleTool": {"radius": 4, …}}`）——AI 随时知道当前可改参数，"孔改大到 ⌀10"一步翻译成 `modify_part`。

### 装配（Round 8）

- **多零件**：当前共 23 个工具；其中建模工具经"活动零件"模型零感知工作（含 `set_active_part` 切换），单零件用户完全无变化。
- **`align_parts` 面贴面**：跨零件用面标签指代（"盖板底面贴到底板顶面"），gap 控制间隙；装配后**自动干涉检查**——零件重叠响亮报干涉量并回滚（`allow_interference` 显式豁免压配）。
- **守卫锚定被操作零件**：孔完整性/密封探针/单实体断言按对象所属零件计算（操作非活动零件同样受全套保护）。
- 装配工程图：iso 格零件分色、被遮挡特征虚线；`export_part(split=True)` 按零件拆分 STEP。

### 自动安装 + 零重连换芯 + 干净卸载（Round 11）

- **`.mcpb` 扩展装完即用**：manifest 固定 `VIBECAD_AUTO_INSTALL=1`，server 启动（即用户第一次开口）就自动后台开始下载引擎，不需要调用 `ensure_runtime`；进度随时可用 `get_runtime_status` 查（带百分比）。手动 `uvx vibecad` 场景默认不设该变量，仍需显式调用 `ensure_runtime` 触发。
- **零重连换芯**：运行时装好的瞬间，监督进程（`vibecad.supervisor`）自动把子进程从引导解释器切换到 conda 运行时解释器并重放握手，客户端全程无感知，**不需要重启对话或重连连接器**。仅极少数不可换芯的场景（裸 server 直跑、无监督进程）诚实回退提示手动重连。
- **干净卸载**：`uninstall_runtime` 两段式（先预览路径/大小，`confirm=true` 才真正删除，删除同样在幕后自动完成）；命令行救援 `vibecad --uninstall`（`--yes` 跳过确认，用于脚本化场景）。删除范围固定是 `VIBECAD_HOME` 整个目录（运行时+日志+视图缓存，约 2-3GB），不影响扩展本体——移除扩展本体请走客户端「设置 → Extensions → Remove」。
- **升级安全**：扩展升级（重装新版 `.mcpb`）只重建扩展目录本身，`VIBECAD_HOME` 默认不在扩展目录内，已下载的引擎不受影响，升级不用重新下载。

## 文档

- **[用户手册](docs/USER_GUIDE.md)** —— 安装、Claude Cowork 配置、场景化用法、卸载、故障排查
- **[验收测试方案](docs/ACCEPTANCE_TESTS.md)** —— 13 个对话场景清单 + Windows 手动验证

## License

[MIT](LICENSE)
