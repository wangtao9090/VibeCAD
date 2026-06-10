"""D5+D6 三级反馈：glTF artifact（主）/ 软渲染图 / 纯文本诊断。

- 纯文本：几何诊断（体积 / 包围盒 / 干涉 / 自由度），全客户端兼容，校验主力
- 软渲染：matplotlib Agg（纯 CPU 无 GPU）+ 逐面朗伯明暗，MCP ImageContent 内联可见
- glTF 导出器（D6）：逐面 Shape.tessellate() → pygltflib，primitive extras
  写入 {part, face, geom_type, params}

子模块可独立导入，无需 FreeCAD/MCP 运行时。
"""
from vibecad.feedback import (
    gltf,  # noqa: F401
    render,  # noqa: F401
    text,  # noqa: F401
)
