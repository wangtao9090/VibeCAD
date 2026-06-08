"""报错翻译：把 FreeCAD / OCCT 的底层异常翻译成用户可读的诊断。

脚手架占位 —— 具体翻译规则随语义工具层一并建设。
"""

from __future__ import annotations


class VibeCADError(Exception):
    """Vibe CAD 领域异常基类。"""


class RuntimeNotReadyError(VibeCADError):
    """FreeCAD 运行时尚未就绪（未检测到且自动安装未完成）。"""


class GeometryAssertionError(VibeCADError):
    """几何断言失败（solve() 返回值不可信，须以几何断言为准）。"""
