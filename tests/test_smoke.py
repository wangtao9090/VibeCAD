"""脚手架冒烟测试：包可导入、ping 工具可用。

不依赖 FreeCAD 运行时 —— 仅验证 server 骨架自洽。
"""

from __future__ import annotations


def test_package_imports() -> None:
    import vibecad

    assert vibecad.__version__


def test_ping_tool() -> None:
    from vibecad.server import ping

    result = ping()
    assert result.startswith("vibecad ok")
