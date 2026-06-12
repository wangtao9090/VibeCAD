"""mcpb 桌面扩展入口：宿主 uv 环境中拉起 VibeCAD MCP server（stdio）。

Claude Desktop 以 `uv run --directory <ext> mcpb_entry.py` 启动本文件；
launcher.main() 先在引导解释器跑 server（运行时未就绪时提供 ensure_runtime），
运行时就绪后 re-exec 进 conda FreeCAD 解释器（与 uvx 入口同路径）。
"""
from vibecad.launcher import main

if __name__ == "__main__":
    main()
