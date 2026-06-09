"""VibeCAD MCP server（FastMCP, stdio）。握手必须秒回：模块级不 import FreeCAD、不下载。"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP

from vibecad import __version__
from vibecad.runtime import paths, status
from vibecad.runtime.installer import RuntimeInstaller

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # m10：杜绝隐式拉起 GUI

mcp = FastMCP("vibecad")
_installer = RuntimeInstaller()  # 进度由 installer 落 status.json，server 读盘
_install_thread: threading.Thread | None = None


def _in_conda_runtime() -> bool:
    """当前进程是否就是 conda 运行时 python（决定能否进程内 import FreeCAD）。"""
    try:
        return os.path.realpath(sys.executable) == os.path.realpath(paths.active_runtime_python())
    except OSError:
        return False


@mcp.tool()
def ping() -> str:
    """连通性自检。"""
    return f"vibecad ok (v{__version__})"


@mcp.tool()
def get_runtime_status() -> dict[str, Any]:
    """查询 FreeCAD 运行时安装进度（跨进程读 status.json）。"""
    d = status.read_status().to_dict()
    d["needs_reconnect"] = status.runtime_ready() and not _in_conda_runtime()
    return d


def _spawn_install() -> None:
    global _install_thread
    if _install_thread and _install_thread.is_alive():
        return
    _install_thread = threading.Thread(target=_safe_install, name="vibecad-install", daemon=True)
    _install_thread.start()


def _safe_install() -> None:
    try:
        _installer.install()
    except Exception:  # noqa: BLE001 - 失败态已落 status.json
        pass


def _ensure_runtime_impl() -> dict[str, Any]:
    if _installer.is_ready():
        msg = "FreeCAD 运行时已就绪"
        if not _in_conda_runtime():
            msg += "；当前会话运行在引导解释器，请重连本 MCP server 后即可使用 CAD 能力"
        return {"status": "ready", "message": msg}
    if _install_thread and _install_thread.is_alive():
        return {"status": "in_progress", "message": "安装进行中，请轮询 get_runtime_status"}
    _spawn_install()
    return {
        "status": "started",
        "message": "已开始后台安装 FreeCAD 运行时（约 2-3GB），请轮询 get_runtime_status",
    }


@mcp.tool()
def ensure_runtime() -> dict[str, Any]:
    """确保 FreeCAD 运行时就绪：未就绪则后台开始安装并立即返回，用 get_runtime_status 轮询。"""
    return _ensure_runtime_impl()


def _prepare_freecad_import() -> None:
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
def _silence_fd1():
    """M-A：FreeCAD/OCCT 会向 fd1 写初始化/进度，污染 MCP JSON-RPC 通道。
    dup2 把 fd1 临时指向 fd2（stderr）保护协议帧（redirect_stdout 拦不住 C++ 层直写 fd1）。"""
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(saved)


def _build_box_and_export() -> dict[str, Any]:
    import tempfile
    _prepare_freecad_import()
    out = os.path.join(tempfile.gettempdir(), "vibecad_smoke.step")
    with _silence_fd1():
        import FreeCAD  # noqa: PLC0415 - 懒加载：仅 conda runtime 进程内 import
        import Part  # noqa: PLC0415
        box = Part.makeBox(10, 10, 10)
        box.exportStep(out)
        bb = box.BoundBox
        result = {"ok": True, "volume": box.Volume, "bbox": [bb.XLength, bb.YLength, bb.ZLength],
                  "step": out, "freecad_version": list(FreeCAD.Version())}
    return result


@mcp.tool()
def smoke_cad() -> dict[str, Any]:
    """地基验证：进程内造 10×10×10 Box，导出 STEP，返回体积/包围盒/路径。"""
    if not _installer.is_ready():
        return {"ok": False, "message": "FreeCAD 运行时未就绪，请先调用 ensure_runtime"}
    if not _in_conda_runtime():
        _msg = "运行时已就绪，但当前会话运行在引导解释器中，请重连本 MCP server 后再调用 smoke_cad"
        return {"ok": False, "message": _msg}
    return _build_box_and_export()


def main() -> None:
    if _auto_install_enabled():
        _spawn_install()
    mcp.run()


def _auto_install_enabled() -> bool:
    return os.environ.get("VIBECAD_AUTO_INSTALL", "") not in ("", "0", "false", "False")


if __name__ == "__main__":
    main()
