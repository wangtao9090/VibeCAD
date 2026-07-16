"""确定性 CAD 语义工具层；模块级导入不触发 FreeCAD 加载。

建模、特征、变换、装配、项目生命周期、测量、渲染与导出均由 server 薄壳注册为
MCP 工具。写操作使用文档事务和几何断言，项目状态由 ``Session`` 统一维护。
"""
from vibecad.tools import (
    export,  # noqa: F401
    modeling,  # noqa: F401  re-export, importable without FreeCAD/mcp
)
