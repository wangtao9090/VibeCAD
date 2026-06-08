"""Vibe CAD MCP server 入口（FastMCP, stdio 起步）。

当前为脚手架占位：仅提供 ping 自检工具与 main() 入口。
运行时安装器、进程内引擎封装、语义工具层将按实现计划逐步填充
（见 docs/superpowers/specs/2026-06-08-vibecad-design.md 第 5 节路线图）。
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("vibecad")


@mcp.tool()
def ping() -> str:
    """连通性自检：返回 server 存活标记。"""
    return f"vibecad ok (v{_version()})"


def _version() -> str:
    from vibecad import __version__

    return __version__


def main() -> None:
    """console_scripts / uvx 入口，以 stdio 传输启动 server。"""
    mcp.run()


if __name__ == "__main__":
    main()
