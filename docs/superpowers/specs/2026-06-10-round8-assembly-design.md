# Round 8 设计：装配 DSL（架构轮）

**日期**：2026-06-10 ｜ **状态**：设计已批准（用户确认+闭关授权）｜ **基线**：main c29609f（R1-R7 全部已合入）

## 1. 定位与架构变更

Session 从「单文档单零件单结果链」升级为「**多零件装配**」：每零件一条独立特征链 + 零件级位姿；既有 18 个工具通过"**当前活动零件**"模型保持向后零感知兼容。这是 D2 设计（assemble/fasten）的落地轮。

## 2. 设计

### 2.1 多零件模型（engine/session.py 架构升级）

- **零件注册表**：`Session._parts: dict[str, PartInfo]`（PartInfo = 名字 + 该零件的文档对象集 + 承载容器引用）；`Session._active_part: str | None`。
- **承载形态（Task 0 spike 定）**：优先 **App::Part 容器**（FreeCAD 原生：组级 Placement 变换整组、对象 addObject 进组；需 headless 验证：组内特征链 recompute/组级 Placement 对结果 shape 的作用/TechDraw 投影含变换）。**回退方案**：平铺分组（Session 记录 obj→part 归属映射，零件位姿 = 链尾结果对象的 Placement——R7 已知 Part::Cut 等对象自身 Placement 默认单位，设置即变换整体）。
- **既有语义迁移**：`get_result_object/get_result_shape` 加 `part=None` 参数（默认活动零件，在该零件对象集内查找——查找逻辑不变）；`new_document` 重置全部零件；单零件文档（从未调 new_part）的一切行为与 R7 完全一致（兼容性硬要求，全部既有快/慢测零修改通过为验收线）。
- **装配级 shape**：`get_assembly_shape()` = 各零件结果 shape（含位姿变换）的 compound——供渲染/导出/干涉。
- 标签注册表分零件命名空间：`set_labels/resolve_face/resolve_edge` 加 part 维度；标签表条目带零件归属（如 `"A": "顶面·平面 …（零件：底板）"`）；跨零件指代用 `face="A@盖板"` 语法或 align_parts 的 (part, face) 参数对（采用后者，标签语法不扩展——YAGNI）。

### 2.2 新工具（tools/assembly.py + server）

1. `new_part(name)`：创建命名零件并设为活动零件（name 唯一性校验；首个零件可隐式——单零件用户从不调它）。
2. `set_active_part(name)` / parts 字段升级：result 的 `parts` 从 `{对象: 参数}` 升级为 `{零件: {对象: 参数}}`（单零件时保持旧形态向后兼容，或统一新形态+README 注明——**采用统一新形态**，AI 客户端读结构无兼容包袱）。
3. `place_part(part, position=None, rotation_axis=None, angle=None)`：零件级位姿（至少给一项；rotation 绕零件 BoundBox 中心）——整链变换，孔随零件走（解 R7 rotate 单图元局限）。
4. `align_parts(moving_part, moving_face, target_part, target_face, offset=[u,v], gap=0.0)`：面贴面对齐——moving 零件被变换使其 moving_face 与 target_face 共面贴合（法向相对）、面心对齐后加面内 offset 与法向 gap。纯 Placement 数学（旋转对齐法向 + 平移），不做约束求解器。face 用各零件自己的标签（标签注册表分零件后天然支持）。
5. **干涉断言**（engine/_integrity 或 assembly 内）：任何 place/align 后对每对零件算 `shapeA.common(shapeB).Volume`——超容差（1e-6）→ RuntimeError 报干涉量与零件对，事务回滚；`allow_interference=True` 显式豁免（压配场景），result 记录干涉量。装配版"绝不静默"。

### 2.3 多零件视觉与交付

- multiview 升级：iso 格每零件不同色系（palette 按零件分组）+ 面标签全装配统一序（表带零件归属）；三正交格合并投影（全装配 HLR——TechDraw.projectEx 吃 compound）。
- describe_part → 装配摘要：per-part（体积/位姿/包围盒）+ 总包围盒 + 两两干涉状态。
- export_part → 装配 STEP（compound 单文件）+ `split=True` 可选 per-part 拆分导出（文件名带零件名）。

### 2.4 既有工具适配

- 18 个工具的内部 session 调用全部走"活动零件"语义（多数只需 get_result_object(part) 默认参数透传，改动集中在 Session 层）。
- `_attach_view`/标注/渲染入口改吃 `get_assembly_shape()`（单零件时 == 原行为）。
- `_integrity` 守卫的快照范围：孔完整性/密封探针按**全装配**计算（跨零件操作也受保护）；单 solid 断言改为"每零件单 solid"（装配天然多 solid）。

## 3. 测试与验收

- **快测**：assembly 校验矩阵（重名零件/未知零件/align 参数）；Session 多零件注册表 fake 测试；既有 227 条**零修改**通过（兼容性验收线）。
- **真机慢测**：①new_part ×2 各自建模互不干扰、活动切换；②place_part 整体移动旋转（含孔零件旋转——R7 被拒场景现在成功）；③align_parts 面贴面（盖板贴底板，位置精确断言）；④干涉拒绝+allow 豁免；⑤装配工程图与 STEP 导出非空；⑥单零件全流程回归（不调 new_part 走 R7 流程）。
- **黑盒人眼**：「底板 60×40×10 四角 4 孔 → new_part 盖板 60×40×5 → align 叠放 → 装配工程图（两零件可辨）→ 故意 gap=-2 叠入被干涉拒绝 → 装配 STEP 导出」。
- 两路终审（code-reviewer + silent-failure-hunter：重点兼容性回归面/干涉断言盲区/活动零件状态泄漏）。

## 4. 风险

1. App::Part headless 行为未知（spike 先行；回退方案已备）。
2. 兼容性回归面大（Session 是全部工具的地基）——"既有 227 快测零修改"是硬验收线，慢测全量回归必跑。
3. 合并投影的工程图在零件重叠视角下可读性（黑盒人眼定夺，必要时正交格按零件分色线条）。
4. 干涉计算 O(n²) 零件对——MVP 零件数少（≤10）不优化。

## 5. 范围纪律（不做）

约束求解器/运动学/爆炸图/BOM/螺纹紧固件库/零件间布尔/子装配嵌套/align 非平面贴合。
