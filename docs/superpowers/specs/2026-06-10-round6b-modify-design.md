# Round 6b 设计：modify_part 参数修改 + 同径孔阵列定位尺寸

**日期**：2026-06-10 ｜ **状态**：设计已批准（用户确认+闭关授权）｜ **基线**：main（446a99a，R1-R6a 全部已合入）

## 1. 问题与需求

1. **modify_part**：R5 需求评估定下"『B 拉长 5 公分』走参数修改而非几何拉伸"（对象是参数化的，改参数+重算精确且稳健），工具留到本轮。这是方案 B 选 FreeCAD 的核心红利兑现：**改一个参数，依赖链（布尔/打孔/圆角）自动重算**。
2. **同径孔阵列定位**（R6a 终审 backlog）：multiview 定位尺寸循环挂在 ⌀ 半径去重循环内——同径双孔只有首孔有定位尺寸（最常见的多孔形态读不到第二孔位置）。

## 2. 设计

### 2.1 `modify_part(name, parameter, value)`（tools/modify.py 新模块 + server 工具）

- **通用单参数形态**：`modify_part("Box", "length", 45)`、`modify_part("HoleTool", "radius", 5)`。
- **参数白名单**（防误改 Placement/表达式等；键大小写不敏感，内部映射 FreeCAD 属性名）：
  | TypeId | 可改参数 |
  |---|---|
  | Part::Box | length→Length, width→Width, height→Height |
  | Part::Cylinder | radius→Radius, height→Height |
  | Part::Fillet | radius（重写 Edges 元组的 r1/r2） |
  | Part::Chamfer | size（同上） |
- 校验：name 非空 str 且对象存在（不存在时错误消息列出文档现有对象名）；parameter 在该对象类型的白名单（不在时列出可改集）；value 有限正数。
- **几何断言纪律**：事务内设参 → recompute → **回读参数确认生效** → `assert_valid_solid(结果对象.Shape)`（下游链整体有效）→ 体积有变化断言（参数改变必然变体积；容差 1e-9，改回原值的 no-op 由"value 等于当前值"前置拒绝："参数已是该值"）。下游 OCCT 失败（孔径超界、圆角过大）→ 响亮报错 + UndoMode 真回滚。
- 成功后走 server `_attach_view` 自动回图（标签/工程图尺寸当场刷新——改 45 马上看到 45），返回含 labels/parts。

### 2.2 参数可见性：result 增 `parts` 字段

- `_attach_view` 成功路径（与 labels 并列）增 `"parts": {对象名: {参数: 当前值}}`——仅含白名单参数，由新纯辅助 `list_parameters(doc)` 生成（tools/modify.py 内，遍历 doc.Objects 按 TypeId 查白名单读值）。
- AI 每步都看到当前参数清单，"孔改大到 10"直接翻译为 `modify_part("HoleTool", "radius", 5)`，无需额外往返。
- `modify_part` 自身成功 result 含修改摘要 `{"modified": {"name":…, "parameter":…, "from":…, "to":…}}`。

### 2.3 同径孔定位解耦（feedback/multiview.py）

定位尺寸循环移出 ⌀ 去重循环：**每个可见整圆都给定位尺寸**（位置按孔标）；⌀ 标注维持按半径去重（直径按径标）。

## 3. 测试与验收

- **快测**：白名单校验矩阵（未知对象/未知参数/非正数/NaN/同值 no-op 拒绝，错误消息含候选列表）；list_parameters fake doc；server 委托+附图形态。
- **真机慢测**：①box(40,30,20)+居中孔 → modify_part length 45 → 体积 == 45·30·20 − π r² 20（孔随重算保持贯穿）且旧面标签过期、回读 Length==45；②孔径 4→5 体积精确变化；③fillet radius 2→3 体积/面数正确；④下游失败回滚（孔径改到 50 超界 → 响亮失败 + 文档对象数/结果对象不变 + 改回合法值可恢复）；⑤同径双孔 → multiview top 两孔各有定位尺寸（坐标断言）。
- **黑盒**：真协议「add_box→add_hole→modify_part(length,45)→modify_part(hole radius,5)」每步收工程图，**人眼确认尺寸数字 40→45、⌀8→⌀10 当场变化**。

## 4. 风险

1. Fillet/Chamfer 的 Edges 元组重写：边索引保持不变只改 r 值——若 OCCT 对新半径失败，断言+回滚兜底。
2. 参数修改后指纹全体过期是预期行为（自动回图当场刷新）——慢测①钉住。
3. parts 清单的对象名是 FreeCAD 内部名（Box/HoleTool/Fillet）——MVP 接受，别名/中文名留后续。

## 5. 范围纪律（不做）

reposition/旋转、阵列复制、按面/边标签反查所属对象、参数表达式（相对值由 AI 客户端换算）、对象重命名/别名。
