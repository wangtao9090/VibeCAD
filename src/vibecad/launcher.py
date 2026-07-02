"""A3 引导壳：决定在哪个 python 跑 server。纯 stdlib，禁 import mcp/FreeCAD 等重依赖。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from vibecad.runtime import paths, status, uninstall


def _run_server() -> None:
    from vibecad.server import main as server_main  # 延迟 import（两 env 均已装 mcp）
    server_main()


def _reexec_into(env_py: Path) -> None:
    args = [str(env_py), "-m", "vibecad.server"]
    if sys.platform == "win32":
        sys.exit(subprocess.run(args).returncode)  # Windows 无真 exec
    os.execv(str(env_py), args)


def _cli_uninstall() -> None:
    """`vibecad --uninstall` 救援命令：不依赖 MCP 客户端，直接命令行触发直删。
    TTY 下无 --yes 需二次确认；非 TTY（CI/管道）或带 --yes 直接执行，避免卡死等待输入。"""
    home = paths.vibecad_home()
    if "--yes" not in sys.argv and sys.stdin.isatty():
        try:
            ans = input(f"将删除 {home}（全部 CAD 运行时）。确认？[y/N] ")
        except (EOFError, KeyboardInterrupt):  # Ctrl-D / Ctrl-C 视为取消，不 traceback
            ans = "n"
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消")
            return
    info = uninstall.uninstall_now()
    print(json.dumps(info, ensure_ascii=False))
    if not info.get("ok"):
        sys.exit(1)  # 护栏拒删/删除未完成：脚本化调用方靠退出码判断


def main() -> None:
    if "--uninstall" in sys.argv:
        _cli_uninstall()
        return
    uninstall.perform_pending_uninstall()  # 标记→重启后清理链路的接收端；无标记则是无操作
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
