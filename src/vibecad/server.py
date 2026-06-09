"""VibeCAD MCP server（FastMCP, stdio）。握手必须秒回：模块级不 import FreeCAD、不下载。"""
from __future__ import annotations

import os
import sys
import threading
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from vibecad import __version__
from vibecad.engine.session import Session
from vibecad.feedback import render as _render
from vibecad.feedback import text as _feedback_text
from vibecad.freecad_env import (
    prepare_freecad_import as _prepare_freecad_import,
)
from vibecad.freecad_env import (
    silence_fd1 as _silence_fd1,
)
from vibecad.runtime import paths, status
from vibecad.runtime.installer import RuntimeInstaller
from vibecad.tools import export as _export
from vibecad.tools import modeling as _modeling

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # m10：杜绝隐式拉起 GUI

mcp = FastMCP("vibecad")
_installer = RuntimeInstaller()  # 进度由 installer 落 status.json，server 读盘
_session = Session()  # 跨 MCP 调用维持同一活动文档（单零件先行）；构造不 import FreeCAD
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


def _runtime_guard() -> dict[str, Any] | None:
    if not _installer.is_ready():
        return {"ok": False, "message": "FreeCAD 运行时未就绪，请先调用 ensure_runtime"}
    if not _in_conda_runtime():
        _msg = "运行时已就绪，但当前会话运行在引导解释器中，请重连本 MCP server 后再试"
        return {"ok": False, "message": _msg}
    return None


@mcp.tool()
def new_document(name: str) -> dict[str, Any]:
    """新建一个 CAD 文档（单零件工作区）。"""
    return _runtime_guard() or _modeling.new_document(_session, name)


@mcp.tool()
def add_box(length: float, width: float, height: float) -> dict[str, Any]:
    """添加参数化长方体（mm）。返回对象名与体积。"""
    return _runtime_guard() or _modeling.add_box(_session, length, width, height)


@mcp.tool()
def add_cylinder(radius: float, height: float) -> dict[str, Any]:
    """添加参数化圆柱（mm）。返回对象名与体积。"""
    return _runtime_guard() or _modeling.add_cylinder(_session, radius, height)


@mcp.tool()
def boolean_cut(base_name: str, tool_name: str) -> dict[str, Any]:
    """布尔差集：从 base 减去 tool，返回结果对象名与体积。"""
    return _runtime_guard() or _modeling.boolean_cut(_session, base_name, tool_name)


@mcp.tool()
def export_part(output_dir: str, fmt: str = "both") -> dict[str, Any]:
    """导出当前结果为 STEP/STL/glTF（fmt: step|stl|gltf|both|all）到 output_dir。"""
    return _runtime_guard() or _export.export_part(_session, output_dir, fmt=fmt)


@mcp.tool()
def describe_part() -> dict[str, Any]:
    """返回当前结果零件的文本诊断（体积/包围盒/质心/实体数/有效性）。"""
    guard = _runtime_guard()
    if guard:
        return guard
    with _silence_fd1():
        return _feedback_text.describe_shape(_session.get_result_shape())


@mcp.tool()
def render_part(view: str = "iso") -> Any:
    """渲染当前零件为 PNG 预览图（view: iso|front|top|right|back），MCP 客户端内联显示。"""
    guard = _runtime_guard()
    if guard:
        return guard
    try:
        with _silence_fd1():
            shape = _session.get_result_shape()
        png = _render.render_png(shape, view=view)
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": f"渲染失败：{exc}"}
    return Image(data=png, format="png")


def main() -> None:
    if _auto_install_enabled():
        _spawn_install()
    mcp.run()


def _auto_install_enabled() -> bool:
    return os.environ.get("VIBECAD_AUTO_INSTALL", "") not in ("", "0", "false", "False")


if __name__ == "__main__":
    main()
