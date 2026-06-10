"""Vibe CAD —— FreeCAD MCP 连接器（Chat-native 零安装）。

包布局（详见 docs/superpowers/specs/2026-06-08-vibecad-design.md）：
- runtime/  D4 运行时安装器（检测 / micromamba / 平台矩阵）
- engine/   D1a 进程内 FreeCAD 封装（文档 / 事务 / checkpoint / 崩溃恢复）
- tools/    D2 语义工具层（建模 / 装配 DSL / 导出 / 零件库）
- feedback/ D5+D6 三级反馈（文本诊断 / 软渲染 / glTF 导出器）
- rules/    规则引擎（YAML 规则 + OCCT 谓词）
"""

__version__ = "0.1.0"  # 与 pyproject.toml 同步（发布版本）
