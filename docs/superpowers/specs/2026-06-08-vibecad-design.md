# Vibe CAD — 整合需求澄清与架构设计文档（Spec v1）

> 本文档把两份飞书调研资料消化、澄清、结构化，并固化三项执行决策，作为项目正式起点。
> 详细的逐周实现计划（writing-plans 产出）见 `docs/superpowers/plans/`。

---

## Context（为什么做这件事）

已完成两份高质量调研：
- **《Vibe CAD 最终调研报告 v3》**（飞书 `XDzwdFSEUo0eYtxSaDMcXcpnnze`）— 市场/用户/技术可行性/商业模式/风险/M1–M4 行动计划。
- **《Vibe CAD 实现方案选型》**（飞书 `IQdFdq12Uo9em1xzltkc9JlLnCe`）— 竞品源码逆向、6 个技术决策点 D1–D6、三方案对比、推荐方案 B、技术栈清单、M1 工程分解。

两份资料分别完成了**需求论证**与**方案选型**，但内容分散、结论交叉。本文档将其整合成单一权威 spec，并补齐三个执行层决策，让项目从"调研"正式进入"可落地工程"。

**一句话定位**：给一年只设计几次东西的人——免费的 FreeCAD + 一个让你永远不用学 FreeCAD 的 AI。免费版 AI 帮你做一个零件，付费版 AI 帮你做一个产品。

**本机环境现状（已探测，2026-06-08）**：`uv`/`uvx` 0.10.9 ✅、Homebrew ✅、macOS arm64（osx-arm64）✅；**无任何 FreeCAD、无 conda/mamba/micromamba** ❌。→ 架构基石「FreeCAD 进程内 import」在本机尚未可验证，这恰好确认了 M1 第一步必须是运行时获取。

---

## 1. 需求澄清（从调研提炼）

### 1.1 做什么 / 为谁
为 FreeCAD 开发一个**开源 MCP 连接器**，让**不会 CAD 的低频个人用户**在任意 MCP 客户端（Claude Desktop / Cowork / OpenClaw / Cursor…）中，通过**自然语言**完成中等复杂度的**参数化设计 + 多零件装配**，交付**可制造文件**（3D 打印 3MF/STL、CNC STEP、激光 DXF）。

**首发形态 = Chat-native 零安装**：用户无需安装 FreeCAD，MCP server 首次运行自动拉取无头 FreeCAD 运行时；每步回传文本诊断 + 渲染图 + 可旋转 glTF 交互预览。装 FreeCAD 解锁 Live 模式（实时视图/点选/幽灵预览）是**升级而非门槛**。

**目标用户三同心圆**（按楔子强度）：① Etsy/微卖家（付费意愿最强，Fusion 个人版 $1,000 收入红线痛点）→ ② 桌面激光切割（参数化 DXF 工具空白，付费习惯成熟）→ ③ 功能件/维修/外壳/homelab 党（漏斗最大）。Cosplay/有机雕刻**不做**（B-rep 不对口）。

### 1.2 能力边界（In / Out of Scope）
| | 范围内 ✅ | 范围外 ❌ |
|---|---|---|
| 建模 | 参数化单零件（草图/拉伸/旋转/孔/圆角/倒角/阵列） | 复杂自由曲面、扫掠/放样为主的有机造型 |
| 装配 | 简单装配：基本关节（Fixed/Revolute/Slider）、销-孔配合、紧固件联接 | 大型装配、复杂运动学仿真 |
| 制造 | 交付 STEP/STL/3MF/DXF | CAM 刀路生成（**永不入范围**，纪律红线） |

能力边界刻意卡在 **LLM 能力悬崖之内**：简单零件 LLM 可用（无效率 ~11%），复杂特征崩溃（扫掠/放样无效率 68–93%）→ 用**受限词汇表 + 语义工具**把模型框在悬崖安全侧。

### 1.3 成功判据（M1 量化门槛，源自文档）
- 30–50 任务集**首次成功率 ≥ 70%**
- **闭环（带反馈重试）成功率 ≥ 90%**
- **单任务 token 成本 < $0.5**
- 全平台矩阵（win-64 / osx-64 / **osx-arm64** / linux-64 / linux-aarch64）运行时可自动获取

---

## 2. 架构方案（锁定方案 B：Chat-native 优先自建）

在三方案（A=fork neka-nat / **B=自建** / C=CadQuery 双引擎）中选定 **B**，理由：与三阶段路线图同构、差异化深度上限最高、单栈维护、无历史包袱；新增验证「FreeCAD 可进程内 import（0.1s，含 Assembly）」消除了原最大架构顾虑。吸收 A 的零件库工具代码（MIT 可抄）、C 的评测基准（CadQuery 任务集翻译成语义工具调用做对照组）。

### 2.1 总体架构（双模式共享同一工具层）
```
任意 MCP 客户端 (Claude Desktop / Cowork / OpenClaw / Cursor …)
   │ MCP (stdio 起步，预留 streamable HTTP)
本地 MCP server (uvx 一键)
   ├─【Chat-native · 首发】进程内 import FreeCAD（无头；首次自动拉运行时）
   │     反馈 = 文本诊断 + 软渲染图 + glTF 交互 artifact（聊天内可旋转）
   │     选择 = 标注编号截图 · 撤销 = 文档事务 + .FCStd checkpoint
   │     ※ 同套代码即未来 Docker 自部署版 / 云版
   └─【Live · 升级档，M3】localhost TCP/JSON → FreeCAD 内插件（自动安装器）
         解锁：实时视图、点选拾取、幽灵预览、GUI 撤销
```
**纪律**：显示通道（glTF）与交付通道（STEP/3MF/DXF）分离；领域逻辑只有一份；agent 侧逻辑可移植；CAM 刀路永不入范围。

### 2.2 六个关键技术决策（最终选定）
| 决策 | 选定 | 说明 |
|---|---|---|
| **D1 进程模型** | **D1a 进程内 import FreeCAD** | 零 RPC/零序列化；崩溃用「每操作前 .FCStd checkpoint + 崩溃自动重启恢复」兜底。D1b 子进程 RPC 留作崩溃率超标时的演进；D1c Live 插件 RPC 走 M3 |
| **D2 中间表示** | **纯语义工具为主路径** | `add_hole`/`fillet_edges`/`assemble`/`fasten` 级结构化参数，可强制每工具事务+几何断言+规则检查；白名单 `execute_code` 仅作逃生舱 |
| **D3 框架/语言** | **Python + 官方 MCP SDK（FastMCP）** | 与进程内 FreeCAD 同语言同进程；uvx 一键分发；学 Blender Lab 设计模式但保持自身 MIT |
| **D4 运行时分发** | **micromamba 自动安装 conda-forge freecad** | 探测序：①本机已有 FreeCAD ≥1.0 直接用 → ②micromamba 装 conda-forge freecad（~1.5–2GB，装到用户数据目录）→ 全平台矩阵。**已决定：第一步即把它做对** |
| **D5 视图反馈** | **三级：glTF artifact（主）/ 软渲染图 / 纯文本** | glTF 服务端零渲染依赖（three.js 在客户端渲）；软渲染走 trimesh/osmesa 无 GPU 依赖（512–768px，400–1000 token/张）；纯文本=几何诊断（体积/包围盒/干涉/自由度），全客户端兼容 |
| **D6 glTF + 面级元数据** | **自写导出器** | 逐面 `Shape.tessellate()` → `pygltflib` 组装 → primitive `extras` 写 `{part, face, geom_type, params}`；换来 artifact 内点选、未来 App 拾取、装配面引用展示（约 1 周工作量） |

### 2.3 工具面设计（差异化所在，官方连接器均无）
- **语义建模**：`add_hole` 级别，不直接暴露 Sketcher
- **装配 DSL + fasten 联接配方**：基准优先引用、solve 后强制几何断言（对标 SolidWorks Smart Fasteners）
- **选区上下文**：标注编号截图 → 用户选号
- **事务纪律**：每操作一个事务 + undo
- **规则引擎**：谓词库 + YAML 规则（带标准出处 + 工艺 profile），**警告不拦截**
- **分级反馈 / 报错翻译 / 零件库生成器**（标准件永不"画"，脚本调用 Fasteners WB / FCGear）
- **白名单 `execute_code`**：仅逃生舱，Plan 确认

**空间决策四档**（AI 干语言和选择题，内核干坐标和精度，用户只点头摇头）：工程惯例默认 + 标注截图确认 → 幽灵预览 → 用户点选 + 自动吸附 → 事后参数滑块。

### 2.4 已固化的工程规范（来自 P0-1 实机验证 8/8）
① 关节引用须双子元素（传错静默失败）② `solve()` 返回值不可信 ⇒ 强制几何断言 ③ 创建前必须 `recompute` ④ 面索引按几何类型检索（缓解 TNP 拓扑命名问题）。

---

## 3. 执行决策（已拍板）

| 决策项 | 选定 | 对计划的影响 |
|---|---|---|
| **首轮交付** | 先产出整合**设计文档（本 spec）** | 详细逐周实现计划走下一轮 writing-plans |
| **运行时策略** | **第一步即实现 D4 自动安装器**（全平台探测 + micromamba） | M1 的第一个里程碑 = 把"零安装"运行时获取机制做对并跑通进程内 import，而非手动装后补 |
| **工程脚手架** | **标准开源起步**：git + MIT LICENSE + uv（pyproject）+ pytest + ruff | 第一天就立规矩，按开源连接器标准生长 |

---

## 4. 工程脚手架与技术栈

### 4.1 项目结构（开源 MIT，uv 管理）
```
vibecad/                      # 仓库根（名称见 §6 开放问题）
├── pyproject.toml            # uv 项目；console_scripts 入口供 uvx 调用
├── LICENSE                   # MIT
├── README.md
├── docs/superpowers/
│   ├── specs/                # 本 spec
│   └── plans/                # writing-plans 产出的逐周实现计划
├── src/vibecad/
│   ├── server.py             # FastMCP 入口（stdio；预留 HTTP）
│   ├── runtime/              # D4 运行时安装器（检测/micromamba/平台矩阵）
│   ├── engine/               # 进程内 FreeCAD 封装（D1a：文档/事务/checkpoint/崩溃恢复）
│   ├── tools/                # 语义工具层（D2：建模/装配 DSL/导出/零件库）
│   ├── feedback/             # 三级反馈（文本诊断 / 软渲染 / glTF 导出器 D6）
│   ├── rules/                # 规则引擎（YAML 规则 + OCCT 谓词）
│   └── errors.py             # 报错翻译
└── tests/
    ├── harness/              # pytest 评测 harness（任务集 + token 计量）
    └── tasks/                # 30–50 任务集
```

### 4.2 技术栈清单（方案 B）
| 层 | 选型 | 备注 |
|---|---|---|
| MCP 框架 | Python `mcp` SDK（FastMCP） | stdio 起步，streamable HTTP 留接口 |
| 几何引擎 | conda-forge freecad 1.1+ 进程内 import | P0 + 实现方案文档双重声称已验证 |
| 运行时安装器 | micromamba（单文件二进制，全平台） | 装至用户数据目录 |
| glTF 导出 | 自写：`Shape.tessellate` + `pygltflib`（面名 extras） | ~1 周 |
| 渲染回退 | trimesh 软渲染 / osmesa | 无 GPU 依赖 |
| 规则引擎 | YAML 规则 + ~10 个 OCCT 谓词函数 | 规则带标准出处 |
| 标准件 | FreeCAD Fasteners WB（脚本调用）+ FCGear | 随运行时一并安装 |
| 事务/恢复 | FreeCAD 文档事务 + 每 N 步 .FCStd checkpoint | 进程内崩溃兜底 |
| license（M4） | ed25519 离线签名 + R2 交付门 | Pro 模块按需下载 |
| 评测 | pytest harness + 30–50 任务集（含 token 计量） | 兼回归测试 |
| 工程化 | uv + ruff + pytest + GitHub Actions（平台矩阵 CI） | 标准开源起步 |

---

## 5. 里程碑路线图

### M1（4–6 周）Chat-native 单零件 MVP — 零安装
按文档分解，**周1 调整为「自动安装器优先」**：
1. **周1**：D4 运行时安装器（检测 / micromamba / 平台矩阵 CI）+ FastMCP 骨架 + 进程内引擎封装（含 checkpoint/恢复）→ **架构基石跑通**
2. **周2–3**：语义建模工具 v1（草图/拉伸/旋转/孔/圆角/阵列，每工具事务+断言）+ 报错翻译
3. **周3–4**：分级反馈（文本诊断 / 软渲染图 / glTF artifact 导出器）+ 参数暴露（VarSet）
4. **周4–5**：导出（STEP/STL/3MF/DXF 基础）+ 零件库接入（Fasteners）
5. **周5–6**：任务集评测跑分 + token 计量 + 文档/演示视频 → 内测发布（GitHub + uvx）

> **里程碑判据**：见 §1.3 成功判据。

### M2（+6 周）装配 + 激光
装配 DSL + 紧固件配方 + 规则引擎 12 条 + DXF 提前；标注图选号。

### M3（+6 周）Live 模式 + 发布
插件自动安装器解锁实时视图/点选/幽灵预览；三渠道发布；"零安装 vibe 出零件"演示视频为主物料。

### M4 商业化开闸
Pro 开闸（Polar + R2 交付门，ed25519 license）；Docker 自部署 +「OpenClaw + NAS」教程。

**商业三铁律**（贯穿全程）：① 不碰算力（全线 BYOK）② 不养内容产线（规则引擎=收敛知识集，不卖模板）③ 收费点只在"创建"环节（"你的文件永远 100% 属于你"）。免费=单零件全流程+两零件尝鲜+选区/撤销/报错翻译；Pro（$39 买断含一年更新）=装配 DSL 完整版+紧固件配方+AI 工程图+批量变体+完整规则引擎+BOM。

---

## 6. 风险、待验证假设与开放问题

### 6.1 需在 M1 第一周亲自复现的关键假设（spec 信任文档 P0 结论，但落地前必须本机/CI 复现）
- **A1 conda-forge freecad 可进程内 import + Assembly 可用**（D1a 基石）— 文档称已验证（0.1s）；M1 周1 在 osx-arm64 + CI 全平台复现。**若不成立 → 退 D1b 子进程 RPC**。
- **A2 micromamba 全平台单文件分发 + 装 conda-forge freecad 成功**（D4 基石）— 文档称 P0 全流程验证（含最冷门 linux-aarch64）；CI 平台矩阵作为硬门。
- **A3 uvx 分发一个依赖外部 conda 环境的 server** 的可行模式 — 需在周1 确定（server 自身走 uv，FreeCAD 运行时走 micromamba 用户数据目录，两者隔离）。

### 6.2 风险清单（含缓解）
复杂特征无效率高（限词汇表+闭环）· Assembly API 漂移（降级/隔离层）· Token 失控（分级反馈，目标 <$0.5）· 进程内 OCCT 崩溃带翻 server（checkpoint + 自动重启）· 官方/社区抢先（窗口 6–12 月，深度工具面非管道可追）· TNP 残留（基准引用+几何检索）· PrintPal 扩张（装配+本地+开源差异化，P0 跟踪逆向）。

### 6.3 开放问题（实现中决策，不阻塞本 spec）
- **项目正式名称**：目录暂用 `VibeCAD`；风险清单提示"名称被占（另起名）"→ 发布前需做名称可用性检查。
- **首发目标 MCP 客户端优先级**：建议 Claude Desktop 先（灯塔用户），其余兼容验证。
- **M1 第一批语义工具确切清单**：Walking Skeleton 建议 `new_document` / `add_box` / `add_cylinder` / `boolean_cut` / `export(STEP/STL)` 五个先打通端到端，再按周2–3 扩展。
- **Python 版本基线**：受 conda-forge freecad 绑定的 Python 版本约束（非系统 3.14）；周1 确定。

---

## 7. 后续步骤

1. ✅ 初始化开源脚手架：`git init` + MIT LICENSE + `uv` 项目 + ruff + pytest 骨架。
2. ✅ 落盘本 spec 并首次 commit。
3. ⏭ 调用 **writing-plans** skill，产出**详细逐周实现计划**，首个计划聚焦 **M1 周1：D4 自动安装器 + 进程内引擎封装 + FastMCP 骨架 + Walking Skeleton 五工具**，并在该计划中先验证 §6.1 的三个关键假设。

---

## Verification（如何验证本设计可落地）

本 spec 不产功能代码，其"可落地性"通过下一轮第一个实现计划的可执行性来检验：
1. **架构基石可复现**：osx-arm64 用 micromamba 装 conda-forge freecad，`python -c "import FreeCAD"` 成功且能创建一个 Box → 验证 A1/A2。
2. **端到端 Walking Skeleton 跑通**：FastMCP server 经 uvx 启动，在 Claude Desktop 中用自然语言调用 `add_box`+`boolean_cut`+`export`，落地一个 .step/.stl 文件，并返回文本几何诊断（体积/包围盒）。
3. **成功判据可度量**：pytest harness 能对任务集跑出首次/闭环成功率与单任务 token 成本三项数字。

满足 1–3 即证明方案 B 在本机环境真实成立，可放心推进 M1 全量。
