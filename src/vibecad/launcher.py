"""A3 引导壳：决定在哪个 python 跑 server。纯 stdlib，禁 import mcp/trimesh/FreeCAD。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from vibecad.runtime import paths, status


def _run_server() -> None:
    from vibecad.server import main as server_main  # 延迟 import（两 env 均已装 mcp）
    server_main()


def _reexec_into(env_py: Path) -> None:
    args = [str(env_py), "-m", "vibecad.server"]
    if sys.platform == "win32":
        sys.exit(subprocess.run(args).returncode)  # Windows 无真 exec
    os.execv(str(env_py), args)


def main() -> None:
    runtime_py = paths.active_runtime_python()
    try:
        in_runtime = Path(sys.executable).resolve() == Path(runtime_py).resolve()
    except OSError:
        in_runtime = False
    if in_runtime:
        _run_server()                          # 已在 conda python（re-exec 后二次进入）
    elif status.runtime_ready() and Path(runtime_py).exists():
        _reexec_into(runtime_py)               # 哨兵就绪 → 交棒
    else:
        _run_server()                          # bootstrap：未就绪，只起轻量 server
