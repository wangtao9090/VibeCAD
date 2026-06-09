"""运行时落盘路径解析。纯 stdlib，跨平台。env 独立于 uv cache。"""
from __future__ import annotations

import os
from pathlib import Path

from vibecad.runtime import platform


def vibecad_home() -> Path:
    if v := os.environ.get("VIBECAD_HOME"):
        return Path(v).expanduser()
    if platform.is_windows():
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "VibeCAD"
    if platform.is_macos():
        return Path.home() / "Library" / "Application Support" / "VibeCAD"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "VibeCAD"


def mamba_root_prefix() -> Path:
    return vibecad_home() / "mamba"


def env_prefix() -> Path:
    return mamba_root_prefix() / "envs" / "vibecad"


def env_python_for(prefix: Path) -> Path:
    return prefix / "python.exe" if platform.is_windows() else prefix / "bin" / "python"


def env_python() -> Path:
    return env_python_for(env_prefix())


def user_override_env() -> Path | None:
    v = os.environ.get("VIBECAD_FREECAD_ENV")
    return Path(v).expanduser() if v else None


def active_runtime_prefix() -> Path:
    """override 优先，否则托管 env。launcher 与 installer 统一以此为准。"""
    return user_override_env() or env_prefix()


def active_runtime_python() -> Path:
    return env_python_for(active_runtime_prefix())


def ready_sentinel() -> Path:
    """安装成功后写此哨兵；就绪探测读它（廉价，不 import FreeCAD）。"""
    return active_runtime_prefix() / ".vibecad_ready"


def status_file() -> Path:
    return vibecad_home() / "status.json"


def install_lock() -> Path:
    return vibecad_home() / ".install.lock"


def install_log() -> Path:
    return vibecad_home() / "install.log"


def freecadcmd_path() -> Path:
    env = active_runtime_prefix()
    return (
        env / "Library" / "bin" / "FreeCADCmd.exe"
        if platform.is_windows()
        else env / "bin" / "freecadcmd"
    )


def micromamba_path() -> Path:
    return vibecad_home() / "bin" / (
        "micromamba.exe" if platform.is_windows() else "micromamba"
    )
