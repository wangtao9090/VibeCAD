# 调查报告：add_hole 二次打孔后 CenterOfMass 漂移 0.44mm（R6a 遗留 chip task_8be60959）

- 调查日期：2026-07-03
- 调查方式：真机探针（.vibecad-test-runtime conda env，真 FreeCAD 内核），纯只读，未改生产代码
- 结论先行：**H1 成立——这是真 bug（几何真错位），不是 H0（物理正确的质心变化被误判）。且更严重：这个 bug 曾在 2026-06-10 被修复过（commit `63d99ee`），但修复只留在一条从未合并的孤立分支上，当前 main 分支仍然带着这个 bug。**

## 1. 背景与线索

`grep docs/superpowers/ -e "0.44" -e "质心" -e "漂移"` 未直接命中字面记录（task_8be60959 这个 chip id 本身在仓库、`.git` 历史、`~/.claude/projects/.../memory` 中都找不到一手落盘记录，视为已不可考的会话态 chip）。但顺着 `git log --oneline --all -- src/vibecad/tools/features.py` 找到了内容和数值完全吻合的一手证据：

```
commit 63d99ee  fix(features): add_hole offset origin no longer drifts on holed faces

    face_obj.CenterOfMass is the centroid after subtracting existing hole
    areas: a second hole on an already-holed face lands off-target silently
    (real-machine: box(40,30,20) top, d8 blind at (10,15), then d6 through
    at offset=[10,5] landed at (30.44, 20) instead of (30, 20) — symmetric
    hole patterns come out asymmetric with no error).
```

这条 commit message 里的场景、数值（30.44 vs 期望 30）与 chip 描述的"add_hole 二次打孔后漂移 0.44mm"完全对应，可确认这就是同一现象的原始触发场景与根因分析。

**关键发现**：用 `git merge-base --is-ancestor 63d99ee HEAD` 验证，`63d99ee` **不是**当前 main HEAD（`e07465b`）的祖先；`git branch --all --contains 63d99ee` 显示它只存在于孤立分支 `claude/crazy-proskuriakova-0b0ee4`（仓库里还留有对应的废弃 worktree 目录）。这条分支和 main 在 R6a 中期（`68385ff` 附近）发生了历史分叉——大概率是当时并行开了多个 worktree 做同一批 R6a/R7 工作，最终 main 选择了另一条谱系合并，`63d99ee` 这个修复被"忘在"了没被采纳的那条分支上。当前 main 的 `src/vibecad/tools/features.py` 第 84、150 行**仍是修复前的代码**（`face_obj.CenterOfMass` 而非 `_param_mid`/`ParameterRange` 中点），`tests/test_tools_features.py` 里也没有 `63d99ee` 引入的回归测试。

## 2. 复现

场景与原始 commit 完全一致：`box(40,30,20)`，顶面先打 ⌀8 盲孔 `depth=10, offset=[-10,0]`（圆心应精确落在 `(10,15)`），重新标注后再打 ⌀6 通孔 `offset=[10,5]`（圆心应精确落在 `(30,20)`）。

真机探针（`.vibecad-test-runtime` conda env，`sys.path` 注入 `src`，跑 `vibecad.tools.features.add_hole` 真实事务）实测：

| 步骤 | 期望 | 实测 |
|---|---|---|
| 孔1（⌀8 盲孔）圆心 | (10, 15) | (10.0, 15.0) ✅ 精确 |
| 孔2（⌀6 通孔）圆心 | (30, 20) | **(30.4372, 20.0)** ❌ 偏离 0.4372mm |
| 整体 CenterOfMass（打孔前） | (20, 15, 10) | (20.0, 15.0, 10.0) ✅ |
| 整体 CenterOfMass（孔1后） | — | (20.213920, 15.0, 9.893040) |
| 整体 CenterOfMass（孔2后，实际） | — | (19.961820, 14.876703, 9.890403) |

数值与原始 commit 描述的 "30.44" 完全对应（精确值 30.43719207948，四舍五入即 30.44，偏差 0.437192mm ≈ 报告中的"0.44mm"）。

## 3. 手算推导（H0 验证）

零假设 H0：整体质心移动是物理正确的，"漂移"只是当时把正常现象误判为异常。

用组合体质心公式（均匀密度，体积代替质量）：

```
V_remain = V_box − V_hole1 − V_hole2
C_remain = (V_box·C_box − V_hole1·C_hole1 − V_hole2·C_hole2) / V_remain
```

- `V_box = 40×30×20 = 24000`，`C_box = (20, 15, 10)`
- 孔1（⌀8 盲孔，深10，从顶面 z=20 钻入到 z=10）：`V1 = π·4²·10 = 502.654825`，圆柱段质心 z 中点 = 15，圆心 xy = (10,15)（精确，无漂移，因为顶面此时无孔）
- 孔2（⌀6 通孔，贯穿 z=20→0）：`V2 = π·3²·20 = 565.486678`，质心 z = 10

**Case A**（假设孔2落在期望坐标 (30,20)）：`C_remain = (19.972601, 14.876703, 9.890403)`，`V_remain = 22931.858498`

**Case B**（孔2落在实测坐标 (30.43719207948, 20)）：`C_remain = (19.961820, 14.876703, 9.890403)`，`V_remain = 22931.858498`

FreeCAD 实测：`Volume=22931.858498, CenterOfMass=(19.961820, 14.876703, 9.890403)`

**Case B 与 FreeCAD 实测在 volume、x/y/z 三个质心分量上误差均为 0.000000（6 位小数吻合）。** 这证明 FreeCAD 的布尔运算和质量属性积分本身完全正确——如果孔2真的钻在 (30,20)，质心会是 Case A 的 (19.9726, ...)；但因为孔2实际钻在了 (30.4372,20)，质心才变成了 Case B 的 (19.9618, ...)。两个 Case 之间质心 x 差 0.0108mm，这才是"挖孔位置差 0.44mm 导致质心连带偏差"的量级——本身并不大，但**根源是孔位置错了，不是"打孔后质心该不该变"的问题**。H0 不成立：这不是一次对"正常质心变化"的误判，因为真正需要解释的从来不是"质心变没变"，而是"第二个孔的圆心为什么没有钻在用户请求的坐标上"。

## 4. 根因定位（H1 验证）

直接对比 `add_hole` 生产代码用作 offset 原点的 `face_obj.CenterOfMass`，与 `63d99ee` 引入的稳定基准 `face.ParameterRange` 中点，在"已打过一个孔的顶面"上的取值：

| 基准 | 值 |
|---|---|
| `face_obj.CenterOfMass`（当前 `features.py:150` 用的原点） | (20.437192, 15.0, 20.0) |
| `face.ParameterRange` 中点（`valueAt((u0+u1)/2, (v0+v1)/2)`） | (20.0, 15.0, 20.0) |
| 面真正几何中心（40×30 矩形中心） | (20.0, 15.0, 20.0) |

顶面打过 ⌀8 盲孔后，面积从 1200 减到 1149.7346（扣掉 π·4²=50.2655），**面积质心**（`CenterOfMass`，即形心，等于对面积积分算出来的"平均位置"）因为挖去了偏心的一块面积而系统性地往孔的反方向偏了 0.437192mm——这是解析几何一阶矩公式 `Δ = -(A_hole·offset_hole) / (A_face - A_hole)` 的精确预测值（探针4逐个数值验证，5 组不同板厚/孔径/偏移方向全部与理论公式吻合到 6 位小数）。

`add_hole` 把这个已经偏移的面积质心当成"面心=offset 原点"，于是：

```
offset=[10,5] 相对 CenterOfMass(20.4372,15.0) → 绝对坐标 (30.437192, 20.0)  ← 实测孔2圆心
offset=[10,5] 相对 ParameterRange中点(20.0,15.0) → 绝对坐标 (30.0, 20.0)   ← 期望孔2圆心
```

链条完全对上：**面积质心受已有孔洞面积影响偏移 → add_hole 把它当 offset 原点 → 第二孔绝对坐标继承这个偏移 → 孔本身钻错位置 → 下游整体质心也跟着继承这个位置误差**。

`src/vibecad/tools/features.py` 中还有一处同根因的用法（`_outward_normal` 第 84 行的内部探针 `probe = face.CenterOfMass + n * 0.01`），理论上也可能因面质心落到已有孔洞附近而探针点判断内外错误，但当前测试场景未触发（因为探针容差 0.01mm 通常远小于面尺度）；`63d99ee` 已一并把它换成 `ParameterRange` 中点。

## 5. 排除的其它子假设

| 子假设 | 验证方法 | 结果 |
|---|---|---|
| Cut 链用错中间体 | 打印每次 `Part::Cut.Base/.Tool` 引用链 | `Hole001.Base == Hole.Name`，链条正确，非根因 |
| 整体 Placement 被扰动 | 打印每次打孔前后 `shape.Placement` | 全程 `(0,0,0)/(0,0,0)`，未被扰动，非根因 |
| 与"哪个孔先打"有关 | 交换顺序：先打 ⌀6(10,5)（面净）再打 ⌀8(-10,0)（面已带孔） | 先打的孔精确 (30,20)；后打的孔偏到 (9.7587,14.8793)（期望 (10,15)），偏差方向对称反转。证明现象只取决于"该面是否已有孔洞面积"，与哪个孔先钻无关 |

## 6. 多参数化重复（稳定性确认）

5 组不同板厚/孔径/offset 方向的独立探针（`box(40,30,20)` 与 `box(60,40,10)` 各若干组合），面积质心的"理论偏移"（一阶矩公式）与"实测偏移"逐组对比：

| 场景 | 理论 (dx,dy) | 实测 (dx,dy) | 差异 |
|---|---|---|---|
| 40×30×20, ⌀8, offset(-10,0) | (0.437192, 0) | (0.437192, 0) | 0 |
| 40×30×20, ⌀12, offset(-10,0) | (1.040547, 0) | (1.040547, 0) | 0 |
| 40×30×20, ⌀8, offset(0,-8) | (0, 0.349754) | (0, 0.349754) | 0 |
| 60×40×10, ⌀10, offset(-15,5) | (0.507481, -0.169160) | (0.507481, -0.169160) | 0 |
| 60×40×10, ⌀8, offset(-12,8) | (0.256704, -0.171136) | (0.256704, -0.171136) | 0 |

全部 6 位小数吻合，证明这是**确定性的系统偏差**（随孔面积、offset 距离线性/规律变化），不是浮点噪声或偶发现象——只要在已经打过孔的面上二次打孔且指定 offset，就会触发。

## 7. 与"整体质心"报告字段的区分（避免混淆）

任务描述里提到的"CenterOfMass 漂移"容易和另一处代码混淆：`src/vibecad/feedback/text.py:80-87` 的 `_center_of_mass(shape)`，被 `describe_shape()`（第15行）用来给用户报告**整个零件**（`Solid`/`Compound`）的质心。这个值理应随打孔挖空材料而移动——这是完全正确、不需要修复的物理事实，属于"报告值"。

本次调查的 bug 与它无关，是另一件事：`features.py` 里的 `face_obj.CenterOfMass`（**面**的质心，即形心/面积一阶矩），被用作 `add_hole` 的**offset 计算基准**（"计算基准"），本不该受该面上已有孔洞的影响——用户传入的 `offset` 是相对"面心"的偏移，面心该是固定的几何中心，不应该因为先打了一个孔就悄悄挪位置。`src/vibecad/tools/modify.py` 中未发现任何 CenterOfMass 相关代码，不涉及本 bug。

## 8. 结论与建议

**定性：真 bug（H1 几何真错位），且是回归/修复丢失，不是可以直接关闭的误报 chip。**

建议（按优先级）：

1. **立即重新应用 `63d99ee` 的修复到当前 main**：把 `src/vibecad/tools/features.py` 的 `_outward_normal`（第 78-87 行）与 `add_hole`（第 150 行）中的 `face_obj.CenterOfMass` 换成基于 `face.ParameterRange` 中点的稳定基准（`_param_mid` 辅助函数），可直接 cherry-pick `63d99ee` 或参照其 diff 手工重放（该 commit 位于孤立分支 `claude/crazy-proskuriakova-0b0ee4`，historical parent 与当前 main 有分叉，直接 `git cherry-pick 63d99ee` 大概率有冲突，建议按 diff 内容手工移植）。
2. 一并补回该 commit 里的回归测试 `test_add_hole_offset_origin_stable_after_existing_hole`（`tests/test_tools_features.py`，断言第二孔精确落在 (30,20)，容差 1e-3）。
3. **排查 worktree 分叉遗留问题**：本次调查过程中发现仓库存在两条并行孤立分支（`claude/crazy-proskuriakova-0b0ee4`、`claude/practical-merkle-ef4d4b`），均和 main 在 R6a 中期分叉，各自独立做了一部分工作后未合并。建议单独审查这两条分支相对 main 的 diff，确认是否还有其它类似"曾经修复、但丢在孤立分支上"的情况（本次只针对 CenterOfMass/质心相关问题做了聚焦排查，未做全量 diff 审计）。
4. 关闭 chip task_8be60959 时应标注"已重新定性为待修复的真实回归"，而非"误报关闭"。

## 9. 回归确认

`uv run pytest -m "not slow" -q` → `400 passed, 66 deselected in 5.33s`。本次调查全程只读，未修改任何生产代码，探针脚本位于本地 scratchpad（`/private/tmp/claude-501/.../scratchpad/centroid-probe/`），未提交。
