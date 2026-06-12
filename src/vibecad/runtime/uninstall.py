"""卸载：标记 + 删除 vibecad_home 整目录。纯 stdlib。

删除范围永远 = vibecad_home()（托管运行时/micromamba/status/日志/views）。
VIBECAD_FREECAD_ENV 用户自带 env 在 home 之外，天然不在范围内——绝不触碰。
运行中删除走「标记 → server 自退 → 重启后 bootstrap 执行删除」：全平台一致，
避开 Windows 对运行中文件的锁。"""
from __future__ import annotations

import shutil
from pathlib import Path

from vibecad.runtime import paths


def uninstall_marker() -> Path:
    return paths.vibecad_home() / ".uninstall_requested"


def dir_size_mb(d: Path) -> float:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total / 1e6


def request_uninstall() -> dict:
    home = paths.vibecad_home()
    if not home.exists():
        return {"ok": True, "already_clean": True, "message": "运行时目录不存在，无需卸载"}
    uninstall_marker().touch()
    return {"ok": True, "marked": True, "path": str(home)}


def perform_pending_uninstall() -> bool:
    """进程启动早期调用：有标记则删 home 整目录。返回是否执行了删除。"""
    home = paths.vibecad_home()
    try:
        if not uninstall_marker().exists():
            return False
    except OSError:
        return False
    shutil.rmtree(home, ignore_errors=True)
    return not home.exists()


def uninstall_now() -> dict:
    """直删（CLI / 无运行中 server 场景）。"""
    home = paths.vibecad_home()
    if not home.exists():
        return {"ok": True, "message": f"{home} 不存在，无需卸载"}
    size = dir_size_mb(home)
    shutil.rmtree(home, ignore_errors=True)
    if home.exists():
        return {"ok": False, "freed_mb": 0,
                "message": f"删除未完成（文件被占用？）：{home}；请关闭使用方后重试"}
    return {"ok": True, "freed_mb": round(size, 1), "path": str(home),
            "message": f"已删除 {home}（释放约 {size / 1000:.1f} GB）"}
