# Round 6a 设计：三视图拼图 + 每步自动回图

**日期**：2026-06-10 ｜ **状态**：设计已批准（用户确认方案 A + 单独小轮）｜ **依赖**：Round 5（feat/round5-referencing，PR #5 OPEN——本轮基于其上开发，PR 策略收尾时按 #5 状态定）

## 1. 问题与需求

1. **单 iso 渲染图的信息缺口**：深度歧义（孔有多深/是否贯穿看不出）、底面/背面不可见。用户每次指令后要"更直观的感受"。
2. **核心循环依赖 AI 自觉**：当前"说一句→看一眼"要靠 AI 客户端记得调 render_part；labels_stale 也只是提示。应该**每步建模/特征指令成功后自动回图**。

## 2. Spike 实证结论（v1-v5 五版真实管线原型，已人眼检验；用户定稿 v5 工程图样式）

- v1/v2（半透明"X 光"方案）被用户否决：**要求真工程图样式**——线框、虚线隐藏线、带尺寸，让用户能读懂尺寸与形状。
- **v3-v5 工程图路径实证**：FreeCAD `TechDraw.projectEx`（OCCT HLR 隐藏线消除）**headless 完全可用**，返回 10 组边（0-4 可见类 hard/smooth/sewn/outline/iso，5-9 对应隐藏类；孔壁轮廓母线在 outline 组）。关键工程结论：
  1. **投影局部坐标系 per-view 变换**（实测定稿）：top `(x,y)→(x,y)`；front/right `(x,y)→(y,-x)`——否则视图旋转 90°。
  2. **尺寸从投影 2D 包围盒推导**（不用模型参数常量）——任意形状通用，且天然正确。
  3. **圆检测**：投影边 Curve 为 Circle → 红色点划中心线十字 + `⌀{2r}` 引线标注 + 首圆定位尺寸（圆心到包围盒两方向）。
  4. **三正交格统一比例**（共用最大跨度），"长对正高平齐"的简化实现。
- v5 成品：front/right 孔壁两条竖直虚线（标准工程图孔表达）、top 含 ⌀12+定位 20/15+总尺寸、~41KB。

## 3. 设计

### 3.1 拼图布局（`feedback/multiview.py` 新模块）

```
┌─────────────┬─────────────┐
│ front 正视   │ right 侧视  │   三格工程图线框：可见边实线 #222/1.4、
│ (工程图)     │ (工程图)    │   隐藏边虚线 #888/0.9、圆中心线红点划、
├─────────────┼─────────────┤   尺寸界线+双箭头+数字（bbox 推导）、⌀ 引线
│ top 俯视     │ iso 立体    │   iso：面片渲染+标注（面标签 A/B/C + 尺寸线）
│ (工程图)     │ (标注)      │   各格中英文小标题（CJK 字体 fallback）
└─────────────┴─────────────┘
```

- `project_view(shape, direction, tf) -> {"vis": [...], "hid": [...], "circles": [(cx,cy,r,visible)]}`：HLR 投影 + 2D 变换（FreeCAD 路径，调用方保证 silence_fd1）。
- `draw_engineering_view(ax, view_data, title) -> bbox|None`：2D 工程图格绘制（纯 matplotlib）。
- `multiview_png(*, eng_views, face_meshes, face_labels, dims, size) -> bytes`：纯函数（吃投影折线/圆数据 + iso 网格数据），dev venv 可快测。
- `render_multiview(shape) -> (png, table, faces_reg, edges_reg)`：真实入口；iso 格数据走 `collect_annotation_data(view="iso")`（标签表/注册表语义与 render_annotated(mode="faces") 完全一致：注册表全量契约、shown=表键）；三正交格走 `project_view`。
- 多孔标注策略：每格 ⌀ 标注按半径去重（同径只标一次）；定位尺寸只标首个可见圆（防拥挤）。
- iso 格绘制复用 `annotate._draw_face_meshes`（已抽取）。

### 3.2 server 集成

1. `render_part(view="multi")`：view 取值扩展 `multi`——返回 `[Image(拼图), json({"ok": True, "labels": 表})]`，并 `set_labels(faces_reg, edges_reg, shown=表键)`。与 annotate="faces" 的双内容协议一致。
2. **每步自动附图**：`add_box / add_cylinder / boolean_cut / add_hole / fillet_edges / chamfer_edges` 六个工具**成功路径**返回值升级为 `[result_dict, Image(拼图)]`：
   - result_dict 增 `"labels": {标签表}`（替代 labels_stale/hint——标签已当场刷新，不再是 stale 提示）；
   - 同时 `set_labels` 刷新（shown=新表键）。
   - **附图失败不连坐**（静默失败敏感点，显式设计）：特征/建模本身已成功提交，渲染异常时 catch 住，result_dict 改回老语义（`labels_stale: True + hint` + `"render_error": "<原因>"`），仍返回纯 dict——绝不因附图失败把成功操作报成 ok:False，也绝不静默吞掉渲染错误。
   - 失败路径（ok:False）不附图，结构化错误不变。

### 3.3 性能与 YAGNI

- 一次 tessellate 四格复用；matplotlib 单 fig 四 subplot 一次 savefig。预估每步 +1~2s、~120KB base64（AI 客户端 ~1-2k tokens/步），低频个人用户可接受。
- 不做：bottom/left 视图、隐藏线/轮廓线、配置开关、edges 标注格、glTF 联动。

## 4. 测试与验收

- **快测**：multiview_png 纯函数（PNG magic、空网格抛 ValueError）；_draw_face_meshes 抽取后 annotate 既有 14+ 测试零回归；server 六工具 mock 委托返回 [dict, Image] 形态 + 附图失败不连坐路径（mock 渲染抛错 → ok:True + render_error + labels_stale）；MCP 协议契约扩展（工具返回 [dict, Image] → [TextContent(json), ImageContent]）。
- **真机慢测**：render_multiview 出图 >5KB + 标签表与 render_annotated(faces) 等价；add_hole 自动附图后 `resolve_face(新表标签)` 立即可用（标签新鲜性——本轮核心行为）；附图后 shown 门控语义不破（未展示边标签仍被拒）。
- **黑盒终验**：真协议跑「new_document→add_box→add_hole」每步收到 `[json, image]` 双内容，存盘拼图**人眼确认**：四格齐全、半透明格透出孔带、iso 格标签+尺寸线清晰。
- 黑盒同时回归 R5 流程（annotate="faces" 路径不回归）。

## 5. 风险

1. HLR 投影耗时与稳健性（复杂件可能秒级；圆柱轮廓的离散噪声——v5 实测 right 视图有一条多余虚线，语义无害可接受）——慢测记录耗时。
2. 工具返回形态变化（dict → [dict, Image]）对客户端兼容性——FastMCP 混合 list 已验证+黑盒跑通；structuredContent 因 `-> Any` 不生成（与 render_part annotate 同型，已知契约）。注意 server 工具签名注解需改 `-> Any`。
3. 渲染时间累计——每工具 +1~2s，可接受；若实测超 3s 记录并在计划阶段决定是否降图幅。

## 6. 范围纪律（本轮不做）

modify_part / 凹腔遮挡检测 / 切线缝合边标注 / FreeCADGui / bottom·left 视图 / 配置开关。
