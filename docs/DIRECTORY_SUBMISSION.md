# Directory Submission Materials（上架提交材料汇编）

> 用途：提交 Claude 官方连接器目录（Connectors Directory）审核时的逐字段材料来源，供后续「提交操作卡」直接引用。英文文案均可直接复制粘贴到提交表单。
>
> Purpose: source of truth for every field of the Connectors Directory submission form. English copy is paste-ready.

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

## 2. Tool Behavior Classification（22 个工具读写分类，文字版）

> 表单层文字说明用。manifest 不加 annotations（v0.4 schema `tools.items` 为 `additionalProperties: false`，加了 validate 必红）；运行时 `tools/list` 已带 ToolAnnotations。

**Read-only — 只读（4）**：不修改任何状态，不写盘。

| Tool | Behavior |
|---|---|
| `ping` | Connectivity check; returns server version. |
| `get_runtime_status` | Reports FreeCAD runtime install status (phase / percent). |
| `describe_part` | Text diagnostics of the current model: volume, bounding box, center of mass. |
| `render_part` | Renders PNG previews / three-view engineering drawings / annotated label images. |

**Writes to disk — 写盘（3）**：仅这三个工具会落盘（下载运行时 / 导出文件）。

| Tool | Behavior |
|---|---|
| `ensure_runtime` | One-time download and install of the FreeCAD runtime (~2–3 GB) from official open-source mirrors (micromamba / conda-forge) into VibeCAD's own data directory. |
| `smoke_cad` | Post-install smoke test: builds a 10×10×10 box in-process and exports a STEP file to verify the runtime. |
| `export_part` | Exports manufacturable files (STEP / STL / glTF) to the user-specified output directory. |

**In-session model edits — 会话内模型修改（15）**：只操作会话内存中的 CAD 文档；每个操作是事务（失败完整回滚），带几何断言守卫（危险修改响亮拒绝）；不读写用户文件。

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

合计 4 + 3 + 15 = 22。

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

- **Manual acceptance**: [`docs/ACCEPTANCE_TESTS.md`](ACCEPTANCE_TESTS.md) — 12 conversational scenarios (handshake / runtime install / modeling / hole patterns / slot pocket / assembly / interference guard / export / error recovery), plus a Windows manual-verification appendix.
  人工验收：12 个对话场景清单 + Windows 手动验证附录。
- **Automated CI**: GitHub Actions — 302 fast tests on every push plus 65 slow integration tests (real 2–3 GB runtime download and in-process FreeCAD modeling), across ubuntu / macos / windows.
  自动化 CI：302 条快测 + 65 条慢速集成测试（真实运行时下载 + 进程内 FreeCAD 建模），ubuntu / macos / windows 三平台矩阵。

---

## 5. Privacy Policy（隐私政策链接）

<https://github.com/wangtao9090/VibeCAD/blob/main/PRIVACY.md>

要点：所有设计数据仅本地处理存储；唯一网络访问是首次使用时从官方开源镜像一次性下载 FreeCAD 运行时；无遥测、无统计、无账号信息收集。
