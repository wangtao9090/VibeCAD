"""下载并校验 micromamba 单文件二进制。纯 stdlib（urllib，自动跟随 302）。"""
from __future__ import annotations

import hashlib
import os
import stat
import urllib.request
from pathlib import Path

from vibecad.runtime import platform

MICROMAMBA_VERSION = "2.8.0-0"  # pin（与 freecad/python 一致纳入可控升级）
_BASE = f"https://github.com/mamba-org/micromamba-releases/releases/download/{MICROMAMBA_VERSION}"


class ChecksumError(RuntimeError):
    """micromamba sha256 与官方校验和不符。"""


def download_url(subdir: str | None = None) -> str:
    subdir = subdir or platform.conda_subdir()
    return f"{_BASE}/{platform.MICROMAMBA_ASSET[subdir]}"


def _sha256_url(subdir: str | None = None) -> str:
    subdir = subdir or platform.conda_subdir()
    # B1: 校验和资源名按 subdir 拼，绝不含二进制的 .exe 后缀
    return f"{_BASE}/micromamba-{subdir}.sha256"


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, open(target, "wb") as fh:  # noqa: S310
        while chunk := resp.read(1 << 20):
            fh.write(chunk)


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "replace")


def _sha256_ok(path: Path, subdir: str | None) -> bool:
    expected = _fetch_text(_sha256_url(subdir)).split()[0].strip().lower()  # 单/双字段都鲁棒
    actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    return actual == expected


def ensure_micromamba(dest: Path, *, subdir: str | None = None) -> Path:
    """幂等：若 dest 已存在且 sha256 合法直接用；否则下载到 .part 校验后原子改名。"""
    sd = subdir or platform.conda_subdir()
    if dest.exists() and dest.stat().st_size > 0 and _sha256_ok(dest, sd):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        _download(download_url(sd), tmp)
        if not _sha256_ok(tmp, sd):
            raise ChecksumError(f"micromamba sha256 不符（subdir={sd}）")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    if not platform.is_windows():
        os.chmod(dest, dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest
