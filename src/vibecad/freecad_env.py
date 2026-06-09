"""Shared FreeCAD in-process import bootstrap.

Pure stdlib (contextlib, os, sys) — no FreeCAD imports, no MCP imports.
Used by both server.py and engine modules to avoid circular imports.
"""
from __future__ import annotations

import contextlib
import os
import sys


def prepare_freecad_import() -> None:
    """A1/M4：conda-forge 把 FreeCAD 模块装在 <prefix>/lib（Windows 为 Library/bin），
    须注入 sys.path 才能进程内 import；Windows 另把 Library/bin 注入 PATH/DLL 搜索路径。"""
    if sys.platform == "win32":
        libbin = os.path.join(sys.prefix, "Library", "bin")
        os.environ["PATH"] = libbin + os.pathsep + os.environ.get("PATH", "")
        try:
            os.add_dll_directory(libbin)
        except (OSError, AttributeError):
            pass
        mod_dirs = [libbin, os.path.join(sys.prefix, "Library", "lib")]
    else:
        mod_dirs = [os.path.join(sys.prefix, "lib")]
    for d in mod_dirs:
        if d not in sys.path:
            sys.path.insert(0, d)


@contextlib.contextmanager
def silence_fd1():
    """M-A：FreeCAD/OCCT 会向 fd1 写初始化/进度，污染 MCP JSON-RPC 通道。
    dup2 把 fd1 临时指向 fd2（stderr）保护协议帧（redirect_stdout 拦不住 C++ 层直写 fd1）。"""
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(saved)
