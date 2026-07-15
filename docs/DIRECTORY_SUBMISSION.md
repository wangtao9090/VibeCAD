# Directory Submission Materials（上架提交材料汇编）

> 用途：提交 Claude 官方连接器目录（Connectors Directory）审核时的逐字段材料来源，供后续「提交操作卡」直接引用。英文文案均可直接复制粘贴到提交表单。
>
> Purpose: source of truth for every field of the Connectors Directory submission form. English copy is paste-ready.

---

## 0. 提交前置条件核对（复核于 2026-07-15，基于已发布 v0.3.0）

| 项目 | 状态 | 说明 |
|---|---|---|
| GitHub Release v0.3.0 已发布 | ✅ | `releases/latest` 已指向 `releases/tag/v0.3.0`，正式 Release 页面 HTTP 200。 |
| `VibeCAD.mcpb` 资产可下载 | ✅ | Release 资产下载成功（125,452 bytes）；解包后 manifest 为 0.3.0、23 个工具，并通过 MCPB 2.1.2 schema 校验。 |
| PyPI 0.3.0 已发布 | ✅ | PyPI `info.version == 0.3.0`；wheel/sdist 与干净本地构建 SHA-256 一致，全新环境安装后可加载 23 个工具并成功 `ping`。 |
| Homepage / Repository 链接可达 | ✅ | `github.com/wangtao9090/VibeCAD` HTTP 200 |
| Privacy policy 链接可达 | ✅ | `github.com/.../blob/main/PRIVACY.md` HTTP 200 |
| PRIVACY.md 内容与第 5 节摘要一致 | ✅ | 本地处理 / 一次性运行时下载 / 无遥测三点逐句对应 |
| 工具数 23 与 manifest.json / README 一致 | ✅ | 正式版包含 2 个只读、6 个主动写文件/状态、15 个会话内建模工具，共 23 个工具。 |
| `docs/ACCEPTANCE_TESTS.md` 链接可达 | ✅ | GitHub 网页版 HTTP 200 |
| CI 测试数字准确 | ✅ | 主线 CI run 29397187277 全绿：415 条快测覆盖 Ubuntu x64/ARM、macOS、Windows；76 条真实 FreeCAD 慢测覆盖 Ubuntu、macOS、Windows。 |
| README 与本文档口径一致 | ✅ | README、manifest 与本文档均声明当前导出格式为 STEP / STL / glTF。 |

**结论：v0.3.0 发布与目录提交门禁已全部核验通过。** GitHub Release、`VibeCAD.mcpb`、PyPI、工具元数据和三平台 CI 均与本文档口径一致，材料可提交目录审核。

---

## 1. Basic Information（基本信息）

| Field | Value |
|---|---|
| Name | VibeCAD |
| One-line description | AI-native conversational CAD — an open-source MCP connector for FreeCAD (chat-native, zero-install). |
| Author | Wang Tao (wangtao9090@gmail.com) |
| Homepage / Repository | <https://github.com/wangtao9090/VibeCAD> |
| License | MIT |
| Privacy policy | <https://github.com/wangtao9090/VibeCAD/blob/main/PRIVACY.md> |

**Long description (paste-ready):**

> VibeCAD is for people who only design a few things a year: free FreeCAD plus an AI that means you never have to learn FreeCAD. Describe parts in plain language — every successful modeling step automatically returns an engineering three-view drawing, so you "say a sentence, see a drawing". Supports parametric edits (change a dimension and the drawing updates in place), hole patterns, free-profile extrusion, and multi-part assembly with automatic interference checking. Exports manufacturable files: STEP for CNC, STL for 3D printing, glTF for interactive preview. On first use it downloads the FreeCAD runtime (~2–3 GB, one time only) from official open-source mirrors (micromamba / conda-forge); all design data is processed and stored locally.

---

## 2. Tool Behavior Classification（23 个工具读写分类，文字版）

> 表单层文字说明用。manifest 不加 annotations（v0.4 schema `tools.items` 为 `additionalProperties: false`，加了 validate 必红）；运行时 `tools/list` 已带 ToolAnnotations。

**Read-only — 只读（2）**：不修改任何状态、不写盘、不访问开放网络（`openWorldHint=false`）。

| Tool | Behavior |
|---|---|
| `ping` | Connectivity check; returns server version. |
| `describe_part` | Text diagnostics of the current model: volume, bounding box, center of mass. |

**Primary file/state-changing — 主动写文件/状态（6）**：这些工具会安排监督进程换芯、安装或卸载运行时、写出文件，或刷新会话标签状态。

| Tool | Behavior |
|---|---|
| `get_runtime_status` | Reports install progress and, once the runtime is ready, may schedule an idempotent supervised process swap; local and non-destructive. |
| `ensure_runtime` | One-time download and install of the FreeCAD runtime (~2–3 GB) from official open-source mirrors (micromamba / conda-forge) into VibeCAD's own data directory. |
| `uninstall_runtime` | Previews or, after explicit confirmation, removes VibeCAD's runtime, logs, and view cache from its own data directory without removing the extension itself. |
| `smoke_cad` | Post-install smoke test: builds a 10×10×10 box in-process and exports a STEP file to verify the runtime. |
| `export_part` | Exports manufacturable files (STEP / STL / glTF) to the user-specified output directory. |
| `render_part` | Renders PNG previews / engineering drawings / label images, refreshes face or edge label state, and writes a PNG when `save_to` is supplied; this may overwrite an existing target PNG and is therefore potentially destructive. |

安全提示口径：`get_runtime_status` 是本地、非破坏、幂等的状态操作；`ensure_runtime` 幂等但会联网下载（唯一 `openWorldHint=true` 的工具）；`smoke_cad`、`export_part`、`render_part` 可能覆盖目标文件，`uninstall_runtime` 可确认删除运行时，因此这四个工具均标为“可能破坏、幂等、本地”。

**In-session model edits — 会话内模型修改（15）**：主要行为是修改会话内存中的 CAD 文档；重复调用会继续改变模型，故均为非幂等、本地、非破坏性提示。每个操作是事务（失败完整回滚），带几何断言守卫（危险修改响亮拒绝）。自动生成预览时可能刷新或写入 VibeCAD 自有视图缓存，这是次要副作用；不会主动导出到用户指定路径。

| Tool | Behavior |
|---|---|
| `new_document` | Creates a new single-part working document. |
| `add_box` | Adds a parametric box (mm). |
| `add_cylinder` | Adds a parametric cylinder (mm). |
| `boolean_cut` | Boolean subtraction (cuts tool from base). |
| `add_hole` | Drills a round hole on a labeled face; supports linear/circular patterns (all-or-nothing). |
| `fillet_edges` | Fillets labeled edges. |
| `chamfer_edges` | Chamfers labeled edges. |
| `modify_part` | Edits a parameter of an existing object; FreeCAD dependency chain recomputes automatically. |
| `move_part` | Moves a primitive to an absolute position. |
| `rotate_part` | Rotates a primitive around its bounding-box center. |
| `extrude_profile` | Free-profile extrusion (rect / circle / polygon / slot) as pad or pocket. |
| `new_part` | Creates a new assembly part and makes it active. |
| `set_active_part` | Switches the active part. |
| `place_part` | Part-level pose: moves/rotates a whole part with all its features. |
| `align_parts` | Face-to-face alignment across parts with automatic interference check (overlap is loudly rejected and rolled back). |

合计 2 + 6 + 15 = 23。

---

## 3. Runnable Examples（可运行示例，3 条连续剧本）

> 提炼自 [`docs/ACCEPTANCE_TESTS.md`](ACCEPTANCE_TESTS.md) 场景 A4–A6，三句构成一个连续对话剧本（盒子 → 打孔 → 改尺寸）。

1. **"Draw a 60×40×10 base plate." / 「画一个 60×40×10 的底板」**
   - Expected: an engineering three-view drawing (front / right / top + isometric) is returned automatically — no need to ask — with the dimensions 60, 40, 10 readable on the drawing.
   - 预期：无需索要，自动回一张工程图三视图拼图（front / right / top + iso 立体格），图上可读 60、40、10 三个尺寸数字。

2. **"Drill an 8mm hole centered on the top face." / 「在顶面正中打一个直径 8mm 的孔」**
   - Expected: the AI identifies the top face via a labeled annotation image, then the updated drawing shows the hole with a ⌀8 callout, centerlines, and centered locating dimensions.
   - 预期：AI 先出面标注图确认"顶面"，新工程图俯视格出现 ⌀8 标注 + 中心线，定位尺寸居中（30 和 20）。

3. **"Change the length to 80." / 「长度改成 80」**
   - Expected: a parametric edit — the length dimension on the drawing changes from 60 to 80 in place, and the hole is preserved (dependency chain recomputes automatically).
   - 预期：参数化修改——图上长度数字 60 当场变 80，孔等特征不丢（FreeCAD 依赖链自动重算）。

---

## 4. Testing Notes（测试说明）

- **Manual acceptance**: [`docs/ACCEPTANCE_TESTS.md`](ACCEPTANCE_TESTS.md) — 13 conversational scenarios (handshake / runtime install and uninstall / modeling / hole patterns / slot pocket / assembly / interference guard / export / error recovery), plus a Windows manual-verification appendix.
  人工验收：13 个对话场景清单 + Windows 手动验证附录。
- **Automated CI**: GitHub Actions — v0.3.0 passed 415 fast tests across Ubuntu x64/ARM, macOS, and Windows, plus 76 slow integration tests (real 2–3 GB runtime download and in-process FreeCAD modeling) across Ubuntu, macOS, and Windows.
  自动化 CI：v0.3.0 的 415 条快测已覆盖 Ubuntu x64/ARM、macOS、Windows；76 条慢速集成测试（真实运行时下载 + 进程内 FreeCAD 建模）已覆盖 Ubuntu、macOS、Windows，全部通过。

---

## 5. Privacy Policy（隐私政策链接）

<https://github.com/wangtao9090/VibeCAD/blob/main/PRIVACY.md>

要点：所有设计数据仅本地处理存储；唯一网络访问是首次使用时从官方开源镜像一次性下载 FreeCAD 运行时；无遥测、无统计、无账号信息收集。
