"""mcpb 桌面扩展入口：宿主 uv 环境中拉起 VibeCAD MCP server（stdio）。

Claude Desktop 以 `uv run --directory <ext> mcpb_entry.py` 启动本文件；
launcher.main() 交棒监督进程（supervisor）：未就绪时子进程跑引导解释器的轻量
server，就绪后 server 自退换芯、supervisor 重启进 conda FreeCAD 解释器并重放
握手，客户端零感知（与 uvx 入口同路径）。
"""
import os
from pathlib import Path

# conda env 中的 server 必须来自当前扩展，而不是不确定的 PyPI latest。入口文件自行
# 解析目录，不依赖宿主对 ${__dirname} 的跨平台插值；显式用户配置仍可覆盖。
os.environ.setdefault("VIBECAD_PIP_SPEC", str(Path(__file__).resolve().parent))

from vibecad.launcher import main

if __name__ == "__main__":
    main()
