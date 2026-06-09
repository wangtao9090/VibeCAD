"""平台检测：映射到 conda subdir。纯 stdlib。"""

from __future__ import annotations

import platform as _platform
import sys


class UnsupportedPlatformError(RuntimeError):
    """无 conda-forge freecad 构建（win-arm64、ppc64le 等）。"""


MICROMAMBA_ASSET: dict[str, str] = {
    "linux-64": "micromamba-linux-64", "linux-aarch64": "micromamba-linux-aarch64",
    "osx-64": "micromamba-osx-64", "osx-arm64": "micromamba-osx-arm64",
    "win-64": "micromamba-win-64.exe",
}


def _machine() -> str:
    return _platform.machine()


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def conda_subdir() -> str:
    machine = _machine().lower()
    if is_macos():
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-64"
    if sys.platform.startswith("linux"):
        if machine in ("aarch64", "arm64"):
            return "linux-aarch64"
        if machine in ("x86_64", "amd64"):
            return "linux-64"
        raise UnsupportedPlatformError(f"Linux {machine} 无 freecad 构建")
    if is_windows():
        if machine in ("amd64", "x86_64"):
            return "win-64"
        raise UnsupportedPlatformError(f"Windows {machine} 无 freecad 构建（win-arm64 暂不支持）")
    raise UnsupportedPlatformError(f"未知平台 {sys.platform}/{machine}")
