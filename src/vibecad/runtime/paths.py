"""Runtime and durable-data path resolution (stdlib only, cross-platform)."""

from __future__ import annotations

import contextlib
import json
import os
import stat
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


def runtime_root() -> Path:
    """Replaceable binaries, receipts, logs and installer coordination."""
    return vibecad_home() / "runtime"


def data_root() -> Path:
    """Durable user/project state. Runtime maintenance must never write here."""
    return vibecad_home() / "data"


def lease_root() -> Path:
    return data_root() / "locks"


def task_store_root() -> Path:
    return data_root() / "tasks"


def revision_store_root() -> Path:
    return data_root() / "projects"


def bootstrap_root() -> Path:
    return data_root() / "bootstrap"


def checkout_root() -> Path:
    return data_root() / "checkouts"


def mamba_root_prefix() -> Path:
    return runtime_root() / "mamba"


def env_prefix() -> Path:
    return mamba_root_prefix() / "envs" / "vibecad"


def legacy_mamba_root_prefix() -> Path:
    """Pre-S3 managed prefix. It is never renamed into the new runtime root."""
    return vibecad_home() / "mamba"


def legacy_env_prefix() -> Path:
    return legacy_mamba_root_prefix() / "envs" / "vibecad"


def env_python_for(prefix: Path) -> Path:
    return prefix / "python.exe" if platform.is_windows() else prefix / "bin" / "python"


def env_python() -> Path:
    return env_python_for(env_prefix())


def user_override_env() -> Path | None:
    v = os.environ.get("VIBECAD_FREECAD_ENV")
    return Path(v).expanduser() if v else None


def external_runtime_receipt() -> Path:
    return runtime_root() / "external-runtime.json"


def _read_bounded_regular_text(path: Path, maximum: int = 4096) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = -1
    try:
        fd = os.open(path, flags)
        before = os.fstat(fd)
        getuid = getattr(os, "geteuid", None)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size > maximum
            or before.st_nlink != 1
            or (not platform.is_windows() and stat.S_IMODE(before.st_mode) & 0o022)
            or (hasattr(before, "st_uid") and getuid is not None and before.st_uid != getuid())
        ):
            return None
        raw = os.read(fd, maximum + 1)
        if len(raw) != before.st_size or os.read(fd, 1):
            return None
        after = os.fstat(fd)
        live = os.stat(path, follow_symlinks=False)
        identity = lambda value: (  # noqa: E731
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            stat.S_IFMT(value.st_mode),
        )
        if identity(before) != identity(after) or identity(after) != identity(live):
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def _bound_external_prefix() -> Path | None:
    """Return the identity-bound external prefix without importing status.

    Full receipt validation belongs to :mod:`runtime.status`; this small parser is
    deliberately conservative and exists only so launcher path selection can use a
    previously verified legacy/external runtime.
    """
    receipt_path = external_runtime_receipt()
    try:
        raw = _read_bounded_regular_text(receipt_path)
        if raw is None:
            return None
        receipt = json.loads(raw)
        prefix_raw = receipt.get("prefix") if isinstance(receipt, dict) else None
        if not isinstance(prefix_raw, str) or not prefix_raw:
            return None
        prefix = Path(prefix_raw)
        if not prefix.is_absolute() or prefix.is_symlink():
            return None
        info = prefix.stat()
        if info.st_dev != receipt.get("prefix_device") or info.st_ino != receipt.get(
            "prefix_inode"
        ):
            return None
        return prefix
    except (OSError, TypeError, ValueError):
        return None


def bound_external_prefix() -> Path | None:
    """Return the identity-pinned external prefix selected by the local receipt."""

    return _bound_external_prefix()


def active_runtime_prefix() -> Path:
    """Select override, new managed runtime, then bounded legacy fallback."""
    if override := user_override_env():
        return override
    current = env_prefix()
    if os.path.lexists(current):
        return current
    if external := _bound_external_prefix():
        return external
    legacy = legacy_env_prefix()
    if os.path.lexists(legacy):
        return legacy
    return current


def active_runtime_python() -> Path:
    return env_python_for(active_runtime_prefix())


def ready_sentinel() -> Path:
    """Receipt selected for the active prefix without writing external trees."""
    active = active_runtime_prefix()
    if user_override_env() is not None:
        return external_runtime_receipt()
    if (external := _bound_external_prefix()) is not None and active == external:
        return external_runtime_receipt()
    return active / ".vibecad_ready"


def status_file() -> Path:
    return runtime_root() / "status.json"


def install_lock() -> Path:
    return runtime_root() / ".install.lock"


def maintenance_lock() -> Path:
    """Stable runtime-maintenance lock that survives replacement of ``runtime``."""
    return vibecad_home() / ".runtime-maintenance.lock"


def removal_record() -> Path:
    """Durable identity record for a target atomically parked during uninstall."""
    return vibecad_home() / ".runtime-removal.json"


def install_log() -> Path:
    return runtime_root() / "install.log"


def freecadcmd_path() -> Path:
    env = active_runtime_prefix()
    return (
        env / "Library" / "bin" / "FreeCADCmd.exe"
        if platform.is_windows()
        else env / "bin" / "freecadcmd"
    )


def micromamba_path() -> Path:
    return runtime_root() / "bin" / ("micromamba.exe" if platform.is_windows() else "micromamba")


def legacy_micromamba_path() -> Path:
    return vibecad_home() / "bin" / ("micromamba.exe" if platform.is_windows() else "micromamba")
