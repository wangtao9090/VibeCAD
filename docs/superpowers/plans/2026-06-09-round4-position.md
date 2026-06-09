# VibeCAD Round 4 — 位置控制（Position Control）实施记录

**目标：** 补上「改对地方」——让图元能放到指定位置，从而能在零件**正中/指定处**打孔，而非只能在原点（角上）。配合 Round 3 的渲染，用户既看得见、又能把特征放对位置。

**范围（MVP，闭关自主执行）：** `add_box`/`add_cylinder` 增 `position`；`add_cylinder` 增 `axis`（x/y/z 轴向，配合 position 可贯穿任意面）。**不含**：移动已有对象的 reposition 工具、任意旋转、阵列、装配。

## 实现

- `tools/modeling.py`：
  - `_validate_position(position)`（纯函数，校验 3 数字元组）+ `_AXIS` 表（z/x/y → 旋转轴+角度）。
  - `add_box(..., position=(0,0,0))`：设 `obj.Placement = FreeCAD.Placement(Vector(*position), Rotation())`。
  - `add_cylinder(..., position=(0,0,0), axis="z")`：设 Placement 含 `Rotation(轴, 角)` 让圆柱沿 x/y/z。
  - 返回 dict 增 `position`（cylinder 另增 `axis`）。延续每步几何断言纪律。
- `server.py`：`add_box(position=[x,y,z])`、`add_cylinder(position=[x,y,z], axis="z")` 工具参数透传（list→tuple）。

## 验收（macOS arm64 实机）

- 快测试 **94 passed** + ruff clean + 握手纯净（server 不 import FreeCAD/matplotlib）。
- 真机慢测试：`test_positioned_centered_through_hole`（modeling）+ `test_positioned_part`（端到端，CI 覆盖）——box(20³) 居中圆柱贯穿 → cut 体积 = 8000−π·r²·20（**挖掉整段圆柱**，证明居中且贯穿）。
- **黑盒（真实 MCP 协议）**：`add_box(40,30,20)` + `add_cylinder(6,40,position=[20,15,-10],axis=z)`（顶面正中、贯穿）→ `boolean_cut` → cut 体积 21738 ≈ 24000−π·36·20。`render_part` 回图，**亲眼确认孔在顶面正中**（对比 Round 3 角上孔是质变）。

## 结论

「看得见（Round 3）+ 改对地方（Round 4）」两半合上：用户可用自然语言把孔/特征放到指定位置，并即时看到放对了。下一步候选：reposition/旋转、更多特征（圆角/阵列/草图拉伸）、FreeCADGui 高质量渲染、装配。
