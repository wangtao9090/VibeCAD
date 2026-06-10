# Round 6a 设计：三视图拼图 + 每步自动回图

**日期**：2026-06-10 ｜ **状态**：设计已批准（用户确认方案 A + 单独小轮）｜ **依赖**：Round 5（feat/round5-referencing，PR #5 OPEN——本轮基于其上开发，PR 策略收尾时按 #5 状态定）

## 1. 问题与需求

1. **单 iso 渲染图的信息缺口**：深度歧义（孔有多深/是否贯穿看不出）、底面/背面不可见。用户每次指令后要"更直观的感受"。
2. **核心循环依赖 AI 自觉**：当前"说一句→看一眼"要靠 AI 客户端记得调 render_part；labels_stale 也只是提示。应该**每步建模/特征指令成功后自动回图**。

## 2. Spike 实证结论（两版真实管线原型，已人眼检验）

- 传统三视图对面片软渲染有坑：front/right 正交视图只见外轮廓，孔在内部不可见（工程图靠虚线隐藏线，面片渲染做不到）。
- **解法：正交视图半透明渲染（alpha≈0.35）**——孔壁透出为深色竖带：位置=孔位、宽度=孔径、贯通=通孔、半截=盲孔深度。"X 光视图"对普通用户比工程图虚线更直观。
- 2×2 拼图（880×880，~94KB）可读性良好；top 俯视对孔位布局表达力最强。

## 3. 设计

### 3.1 拼图布局（`feedback/multiview.py` 新模块）

```
┌─────────────┬─────────────┐
│ front 正视   │ top 俯视    │   front/right：半透明（X 光）
│ (半透明)     │ (不透明)    │   top：不透明（俯视下孔本就可见）
├─────────────┼─────────────┤   iso：标注版（面标签 A/B/C + 尺寸线）
│ right 侧视   │ iso 立体    │   各格中英文小标题
│ (半透明)     │ (标注)      │
└─────────────┴─────────────┘
```

- `multiview_png(face_meshes, *, face_labels, dims, size=(880, 880)) -> bytes`：纯函数（吃逐面网格数据，不碰 FreeCAD），dev venv 可快测。
- 绘制逻辑与 annotate.py 现已两份重复（render.py/annotate.py），本轮第三处出现——**rule of three 触发**：把单格绘制抽为 `annotate._draw_face_meshes(ax, face_meshes, *, alpha=1.0, light=...)` 共享 helper，annotate.annotated_png 与 multiview 复用；render.py 已交付不动。
- `render_multiview(shape) -> (png, table, faces_reg, edges_reg)`：真实入口（视角集固定 2×2，无 view 参数），逐面 tessellate 一次、四格复用；iso 格调用现有标注逻辑（标签锚点/可见性按 iso 视角算），标签表/注册表语义与 render_annotated(mode="faces") 完全一致（注册表全量契约、shown=表键）。

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

1. 半透明 painter's algorithm 伪影——spike 已人眼可接受；复杂件恶化时表注/iso 格兜底。
2. 工具返回形态变化（dict → [dict, Image]）对客户端兼容性——FastMCP 混合 list 已验证+黑盒跑通；structuredContent 因 `-> Any` 不生成（与 render_part annotate 同型，已知契约）。注意 server 工具签名注解需改 `-> Any`。
3. 渲染时间累计——每工具 +1~2s，可接受；若实测超 3s 记录并在计划阶段决定是否降图幅。

## 6. 范围纪律（本轮不做）

modify_part / 凹腔遮挡检测 / 切线缝合边标注 / FreeCADGui / bottom·left 视图 / 配置开关。
