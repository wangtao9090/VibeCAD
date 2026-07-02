"""A3 引导壳：CLI 分支（--uninstall 救援）+ 待删清理，随后交棒监督进程常驻。

Round 11 C 分支：原三态判断（在哪个 python 跑 server）迁入 supervisor._server_cmd，
本模块只做进程入口的一次性事务；退出码 = server 真退出码，原样透传给宿主。
纯 stdlib，禁 import mcp/FreeCAD 等重依赖。"""
from __future__ import annotations

import json
import sys

from vibecad import supervisor
from vibecad.runtime import paths, uninstall


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
    # 标记→重启后清理链路的接收端（无标记则是无操作）。supervisor._spawn 每次也会
    # 清理（换芯重启不经此处），这里保留是让 CLI/异常路径不依赖 supervisor 内部时序。
    # 委托 supervisor.run_pending_uninstall：删除未完成时 stderr 响亮警告（I1）。
    supervisor.run_pending_uninstall()
    sys.exit(supervisor.Supervisor().run())
