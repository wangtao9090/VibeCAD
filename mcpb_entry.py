"""mcpb 桌面扩展入口：宿主 uv 环境中拉起 VibeCAD MCP server（stdio）。

Claude Desktop 以 `uv run --directory <ext> mcpb_entry.py` 启动本文件；
launcher.main() 交棒监督进程（supervisor）：未就绪时子进程跑引导解释器的轻量
server，就绪后 server 自退换芯、supervisor 重启进 conda FreeCAD 解释器并重放
握手，客户端零感知（与 uvx 入口同路径）。
"""
from vibecad.launcher import main

if __name__ == "__main__":
    main()
