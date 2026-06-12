"""卸载：标记 + 删除 vibecad_home 整目录。纯 stdlib。

删除范围永远 = vibecad_home()（托管运行时/micromamba/status/日志/views）。
VIBECAD_FREECAD_ENV 用户自带 env 在 home 之外，天然不在范围内——绝不触碰。
运行中删除走「标记 → server 自退 → 重启后 bootstrap 执行删除」：全平台一致，
避开 Windows 对运行中文件的锁。
VIBECAD_HOME 是外部输入：所有删除点先过 _assert_safe_to_delete 护栏。"""
from __future__ import annotations

import shutil
from pathlib import Path

from vibecad.runtime import paths

_MARKER_NAME = ".uninstall_requested"


def uninstall_marker() -> Path:
    return paths.vibecad_home() / _MARKER_NAME


def _assert_safe_to_delete(home: Path) -> None:
    """删除前自校验：VIBECAD_HOME 是外部输入，删错目录不可逆——三道闸：
    ① 拒 symlink/根/家目录/过浅路径；② 只删「像我们的」目录（安装产物特征；
    卸载标记不算特征——它是 request_uninstall 自己写的，算则循环论证）；
    ③ 空目录放行（删空目录无危害；「只含标记」视同空——标记是我们写的，
    删之无损，且不为其余内容背书：有任何用户文件即非空、照样拒删）。"""
    resolved = home.expanduser().resolve()
    if home.is_symlink():
        raise ValueError(f"拒绝删除符号链接：{home}")
    if resolved == Path(resolved.anchor) or resolved == Path.home().resolve():
        raise ValueError(f"拒绝删除危险路径：{resolved}")
    if len(resolved.parts) < 3:
        raise ValueError(f"拒绝删除过浅路径：{resolved}")
    markers = ("status.json", "mamba", "views", "install.log", "bin")
    contents = [p for p in resolved.iterdir() if p.name != _MARKER_NAME]
    ours = (resolved.name == "VibeCAD"
            or any((resolved / f).exists() for f in markers)
            or not contents)
    if not ours:
        raise ValueError(f"目录不含 VibeCAD 安装产物，拒绝删除：{resolved}")


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
    try:
        _assert_safe_to_delete(home)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
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
    try:
        _assert_safe_to_delete(home)
    except ValueError:
        return False
    shutil.rmtree(home, ignore_errors=True)
    return not home.exists()


def uninstall_now() -> dict:
    """直删（CLI / 无运行中 server 场景）。"""
    home = paths.vibecad_home()
    if not home.exists():
        return {"ok": True, "message": f"{home} 不存在，无需卸载"}
    try:
        _assert_safe_to_delete(home)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    size = dir_size_mb(home)
    shutil.rmtree(home, ignore_errors=True)
    if home.exists():
        return {"ok": False, "freed_mb": 0,
                "message": (f"删除未完成：可能已删除部分文件，残留于 {home}；"
                            "请关闭正在使用运行时的程序后重试，或手动删除该目录")}
    return {"ok": True, "freed_mb": round(size, 1), "path": str(home),
            "message": f"已删除 {home}（释放约 {size / 1000:.1f} GB）"}
