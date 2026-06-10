# Round 7 设计：reposition + 孔阵列 + 草图拉伸

**日期**：2026-06-10 ｜ **状态**：设计已批准（用户确认拆分：R7 三特性单零件轮，装配 DSL 留 R8）｜ **基线**：main + PR #7（R6b modify——本轮依赖其断言纪律与 parts 机制，分支基于 #7 合并后的 main 或栈于 feat/round6b-modify，收尾按 #7 状态定）

## 1. 范围与定位

单零件架构内的三个建模能力跃升：①已有对象可移动/旋转（reposition）；②孔可阵列（线性/圆形）；③自由轮廓拉伸 pad/pocket（从"图元组合"到"自由形状"的跨越）。装配 DSL（多零件架构变更）独立为 Round 8。

## 2. 设计

### 2.1 reposition（`tools/transform.py` 新模块）

- `move_part(session, name, position)` —— **绝对定位**（与 add_box position 语义一致；相对位移由 AI 客户端换算）：`obj.Placement.Base = Vector(*position)`，保留现有 Rotation。
- `rotate_part(session, name, axis, angle)` —— 绕全局轴（x/y/z）、以**对象 BoundBox 几何中心**为旋转中心（用户说"把方块转 90°"的直觉是绕自身中心，而 Box 的 Placement.Base 是角点）：实现用 `Placement(center_to_origin) → Rotation → Placement(origin_to_center)` 复合，或等价的 `obj.Placement = Placement(Vector(), rot, center) * obj.Placement`（FreeCAD Placement 支持旋转中心参数，实现时真机验证取稳的写法）。angle 单位度，范围 (-360, 360) 非零。
- 可操作对象白名单：`Part::Box` / `Part::Cylinder`（图元；Cut/Fillet/Chamfer 跟随 Base 不可直接 repos——错误消息说明并列出可操作对象）。
- **断言纪律（R6b 全套继承）**：事务内改 Placement → recompute → Touched 账本断言 → 结果对象不漂移 → `assert_valid_solid` → 单 solid → **孔完整性快照比对**（移动孔刀具使孔出界/变缺口 → 响亮拒绝回滚；移动基体同理）→ 体积断言：**移动图元后结果体积允许变化**（孔刀具位置变了相交区变化是合法的——如把孔从中心移到角部），但移动后体积若归零/结果漂移则拒。reposition 不做"体积不变"断言（与 modify 不同，注释写明原因）。

### 2.2 孔阵列（`tools/features.py` 的 `add_hole` 扩展 `pattern` 参数）

- `add_hole(session, face, diameter, depth=None, offset=(0,0), pattern=None)`：
  - `pattern=None`：单孔，现行为零变化。
  - `{"type": "linear", "count": N, "spacing": S, "direction": [du, dv]}`——面内 e1/e2 坐标系方向向量（默认 `[1, 0]`），第 i 孔中心 = offset + i·S·normalize(direction)，i=0..N-1（首孔即 offset 处）。
  - `{"type": "circular", "count": N, "radius": R}`——以 offset（默认面心）为圆心、半径 R 均布 N 孔（角度 360/N 递增，0° 起）。
  - 校验：count 整数 2..50；spacing/radius > 0 有限；direction 二维非零。
- **实现：逐孔链式 Cut**（每孔一个 cylinder + 一个 Part::Cut，与现有单孔结构完全一致——R6b 的孔完整性快照天然兼容，零判据改造）。事务内循环创建，**最后统一一次 recompute** 后做全部断言。
- 断言：完整圆柱面计数（该径）+= count；体积严格减少；单 solid；孔间重叠（spacing < diameter）或越界 → 完整面计数不达标 → 响亮拒绝整体回滚（要么全成要么全无）。
- result：`"holes": {"count": N, "pattern": {...}, "diameter": d}`。

### 2.3 草图拉伸（`tools/sketch.py` 新模块）

- `extrude_profile(session, profile, height, face=None, offset=(0,0), operation="pad")`：
  - **profile DSL**（2D，单位 mm，局部坐标原点=放置点）：
    | type | 参数 | 面积公式（断言用） |
    |---|---|---|
    | rect | length, width | L·W |
    | circle | radius | πr² |
    | polygon | points（≥3 个 [x,y]，自动闭合） | shoelace 公式 |
    | slot | length（两圆心距）, width | L·W + π(W/2)² |
  - **放置**：`face="A"`（R5 面标签）→ 解析+平面校验 → 轮廓置于该面上（面心 + offset 面内坐标，使用 features 的 `_inplane_axes` 同款 e1/e2）；`face=None` → 全局 XY 平面 z=0（空文档起步建任意底板）。
  - **operation**：`"pad"` 加料（沿面外法向拉 height，与基体 `Part::Fuse`；无基体时直接成为首个零件）；`"pocket"` 减料（沿面内法向挖深 height，`Part::Cut`）。
  - **实现**：profile → Part Wire（polygon 用 makePolygon；rect 四线段；circle 整圆边；slot 两直线+两半圆弧）→ `Part.Face(wire)` → `face.extrude(vector)` → 静态 shape 经 `Part::Feature` 包装 → Fuse/Cut 文档对象。
  - **已知取舍（注明）**：extrude 产物是静态 shape 非参数化——`modify_part` 不可改其尺寸（parts 清单不含它），改尺寸需删除重建，参数化草图留后续；profile 自交多边形不做预检，靠 Face 构造失败/几何断言兜底响亮报错。
- 断言：pad → 体积**严格增加** ≈ 面积×height（容差 1%，浮空 pad 导致双 solid → 单 solid 断言拒）；pocket → 体积**严格减少**（≈ 面积×depth 当完全嵌入；部分越界时按"严格减少+单 solid+孔完整性快照不退化"）；Touched/漂移/有效性全套。

### 2.4 server 集成

新工具 `move_part` / `rotate_part` / `extrude_profile`（三态守卫+结构化失败+`_attach_view` 自动回工程图）；`add_hole` 增 `pattern` 参数透传。工具数 15 → 18。

## 3. 测试与验收

- **快测**：transform/sketch 校验矩阵（白名单/axis/angle/pattern 各字段/profile DSL 各 type 缺参/points<3）；profile 面积纯函数（shoelace/slot 公式）单测；server 委托与附图形态。
- **真机慢测**：①move 孔刀具到新位置体积重算正确+移出界拒绝回滚；②rotate box 90° 后 BoundBox 轴交换；③linear 4 孔阵列体积 = V₀−4πr²t、完整面 +4、重叠 spacing 拒绝；④circular 6 孔；⑤pad rect 体积精确增加；⑥pocket slot 体积精确减少；⑦polygon pocket（三角形）；⑧浮空 pad 双 solid 拒绝。
- **黑盒人眼**：「底板 → 顶面 4 孔线性阵列 → 顶面中央 slot pocket → 移动一个图元」每步收工程图，确认阵列孔距/槽形/⌀ 标注齐全。
- 两路终审（code-reviewer + silent-failure-hunter）。

## 4. 风险

1. slot 圆弧 wire 构造（两直线+两半圆的端点拼接顺序）——真机先 spike 验证 wire 闭合。
2. pad 与基体不相交（浮空）→ Fuse 结果 compound 双 solid——断言已设计。
3. 链式 Cut 阵列对象数（N≤50 上限）——recompute 一次性，性能慢测记录。
4. extrude 非参数化与 parts 清单的预期差——result 注明 `"parametric": false`。

## 5. 范围纪律（不做）

装配 DSL（R8）/参数化草图（SketchObject）/阵列特征对象（Draft Array）/轮廓自交预检/extrude 的 modify 支持/锥度拉伸/路径扫掠。
