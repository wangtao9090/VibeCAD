"""Shared FreeCAD in-process import bootstrap.

Pure stdlib (contextlib, os, sys) — no FreeCAD imports, no MCP imports.
Used by both server.py and engine modules to avoid circular imports.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

_WINDOWS_DLL_DIRECTORY_HANDLES: dict[str, object] = {}
_WINDOWS_DLL_DIRECTORY_LOCK = threading.Lock()


def _windows_managed_path_entries(prefix: Path) -> tuple[Path, ...]:
    """Return the deterministic prefix entries installed by conda activation."""

    return (
        prefix,
        prefix / "Library" / "mingw-w64" / "bin",
        prefix / "Library" / "usr" / "bin",
        prefix / "Library" / "bin",
        prefix / "Scripts",
        prefix / "bin",
    )


def activate_windows_runtime_environment(
    base: Mapping[str, str],
    prefix: str | os.PathLike[str],
) -> dict[str, str]:
    """Build one explicit managed-runtime activation environment on Windows.

    The result mirrors the reviewed variables produced by the pinned
    micromamba environment without running a package-manager wrapper around
    every server, Worker, or GUI process.  The caller owns any additional
    environment minimisation and this function never mutates ``base``.
    """

    if sys.platform != "win32":
        return dict(base)
    if not isinstance(base, Mapping) or any(
        type(key) is not str or type(value) is not str for key, value in base.items()
    ):
        raise TypeError("base environment must be a string mapping")
    managed_prefix = Path(prefix)
    if not managed_prefix.is_absolute():
        raise ValueError("managed runtime prefix must be absolute")

    environment = dict(base)
    path_entries = [str(item) for item in _windows_managed_path_entries(managed_prefix)]
    path_entries.extend(item for item in environment.get("PATH", "").split(os.pathsep) if item)
    deduplicated: list[str] = []
    seen: set[str] = set()
    for item in path_entries:
        key = os.path.normcase(os.path.normpath(item))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(item)

    proj_root = managed_prefix / "Library" / "share" / "proj"
    environment.update(
        {
            "CONDA_DEFAULT_ENV": managed_prefix.name,
            "CONDA_PREFIX": str(managed_prefix),
            "CONDA_SHLVL": "1",
            "PATH": os.pathsep.join(deduplicated),
            "PROJ_DATA": str(proj_root),
            "PROJ_NETWORK": (
                "OFF" if (proj_root / "copyright_and_licenses.csv").is_file() else "ON"
            ),
            "SSL_CERT_DIR": str(managed_prefix / "Library" / "ssl" / "certs"),
            "SSL_CERT_FILE": str(managed_prefix / "Library" / "ssl" / "cacert.pem"),
            "XML_CATALOG_FILES": (managed_prefix / "etc" / "xml" / "catalog").as_uri(),
        }
    )
    return environment


def prepare_freecad_import() -> None:
    """A1/M4：conda-forge 把 FreeCAD 模块装在 <prefix>/lib（Windows 为 Library/bin），
    须注入 sys.path 才能进程内 import；Windows 另把 Library/bin 注入 PATH/DLL 搜索路径。"""
    if sys.platform == "win32":
        libbin = os.path.join(sys.prefix, "Library", "bin")
        os.environ.update(activate_windows_runtime_environment(os.environ, sys.prefix))
        key = os.path.normcase(os.path.normpath(libbin))
        with _WINDOWS_DLL_DIRECTORY_LOCK:
            if key not in _WINDOWS_DLL_DIRECTORY_HANDLES:
                try:
                    handle = os.add_dll_directory(libbin)
                except (OSError, AttributeError):
                    pass
                else:
                    # CPython removes the directory when this object is closed.
                    # Retain it for the lifetime of the FreeCAD-hosting process.
                    _WINDOWS_DLL_DIRECTORY_HANDLES[key] = handle
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
