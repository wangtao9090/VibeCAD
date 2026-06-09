"""D2 语义工具层：纯语义工具为主路径（白名单 execute_code 仅逃生舱）。

Walking Skeleton 首批：new_document / add_box / add_cylinder / boolean_cut /
export(STEP/STL)。每工具一个事务 + 几何断言 + 规则检查。

子模块（按需导入，不强依赖 FreeCAD）：
    modeling  —— 参数化 Part 图元 + 布尔（D2 本次实现）
    export    —— STEP/STL 导出（Task 5 添加）
"""
from vibecad.tools import (
    export,  # noqa: F401
    modeling,  # noqa: F401  re-export, importable without FreeCAD/mcp
)
