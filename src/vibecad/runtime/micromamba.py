"""下载并校验 micromamba 单文件二进制。纯 stdlib（urllib，自动跟随 302）。"""
from __future__ import annotations

import hashlib
import os
import stat
import urllib.request
from pathlib import Path

from vibecad._file_compat import (
    WindowsPathCapability,
    capture_windows_fd,
    capture_windows_path,
    delete_windows_file,
    open_private_file,
    protect_windows_path,
    replace_windows_file,
    validate_windows_path,
)
from vibecad.runtime import platform

MICROMAMBA_VERSION = "2.8.0-0"  # transaction/linking engine
# Windows uses this reviewed build only for ``create --download-only`` so the
# extracted cache stays flat; the current engine still performs the offline link.
WINDOWS_FLAT_CACHE_VERSION = "2.5.0-2"
_RELEASES = "https://github.com/mamba-org/micromamba-releases/releases/download"
_PINNED_SHA256 = {
    (MICROMAMBA_VERSION, "win-64"): (
        "bd77a64b9ca1c57c10e30cf54561776010d72f065305dbcf92311e7358b61322"
    ),
    (WINDOWS_FLAT_CACHE_VERSION, "win-64"): (
        "baf6d56a31a63a75f0c77bdce27fefea57b1bdc8aa25b50a1be45696c0e737e7"
    ),
}


class ChecksumError(RuntimeError):
    """micromamba sha256 与官方校验和不符。"""


def download_url(
    subdir: str | None = None,
    *,
    version: str = MICROMAMBA_VERSION,
) -> str:
    subdir = subdir or platform.conda_subdir()
    return f"{_RELEASES}/{version}/{platform.MICROMAMBA_ASSET[subdir]}"


def _sha256_url(
    subdir: str | None = None,
    *,
    version: str = MICROMAMBA_VERSION,
) -> str:
    subdir = subdir or platform.conda_subdir()
    # B1: 校验和资源名按 subdir 拼，绝不含二进制的 .exe 后缀
    return f"{_RELEASES}/{version}/micromamba-{subdir}.sha256"


def expected_sha256(
    subdir: str | None = None,
    *,
    version: str = MICROMAMBA_VERSION,
) -> str | None:
    """Return a reviewed embedded digest when this platform build is qualified."""

    return _PINNED_SHA256.get((version, subdir or platform.conda_subdir()))


def _download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, open(target, "wb") as fh:  # noqa: S310
        while chunk := resp.read(1 << 20):
            fh.write(chunk)


def _fetch_text(url: str) -> str:
    with urllib.request.urlopen(url) as resp:  # noqa: S310
        return resp.read().decode("utf-8", "replace")


def _download_to_fd(url: str, descriptor: int) -> None:
    with urllib.request.urlopen(url) as response:  # noqa: S310 - pinned release URL
        while chunk := response.read(1 << 20):
            offset = 0
            while offset < len(chunk):
                written = os.write(descriptor, chunk[offset:])
                if written <= 0:
                    raise OSError("micromamba download made no progress")
                offset += written


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1 << 20):
        digest.update(chunk)
    return digest.hexdigest().lower()


def _same_windows_generation(
    left: WindowsPathCapability,
    right: WindowsPathCapability,
) -> bool:
    return (
        left.volume,
        left.file_id,
        left.owner_sid,
        left.security_sha256,
        left.generation_token,
    ) == (
        right.volume,
        right.file_id,
        right.owner_sid,
        right.security_sha256,
        right.generation_token,
    )


def _expected_digest(subdir: str, *, version: str) -> str:
    expected = expected_sha256(subdir, version=version)
    if expected is None:
        expected = _fetch_text(_sha256_url(subdir, version=version)).split()[0].strip().lower()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ChecksumError("micromamba checksum response is invalid")
    return expected


def _ensure_micromamba_windows(dest: Path, *, subdir: str, version: str) -> Path:
    """Download and publish one exact private Win32 file generation."""

    dest = Path(os.path.abspath(dest))
    dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent = protect_windows_path(dest.parent, directory=True)
    expected = _expected_digest(subdir, version=version)
    destination: WindowsPathCapability | None = None
    if os.path.lexists(dest):
        try:
            destination = protect_windows_path(dest, directory=False)
            descriptor, _opened = open_private_file(
                dest,
                create=False,
                read_write=False,
                expected_parent=parent,
            )
            try:
                opened = capture_windows_fd(
                    descriptor,
                    directory=False,
                    generation_token=destination.generation_token,
                )
                if opened != destination:
                    raise OSError("micromamba destination identity changed")
                if _sha256_fd(descriptor) == expected:
                    validate_windows_path(destination, directory=False)
                    return dest
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ChecksumError("existing micromamba destination is unsafe") from exc

    temporary = dest.with_name(dest.name + ".part")
    temporary_capability: WindowsPathCapability | None = None
    descriptor = -1
    try:
        if os.path.lexists(temporary):
            try:
                stale = protect_windows_path(temporary, directory=False)
                delete_windows_file(temporary, parent=parent, expected=stale)
            except OSError as exc:
                raise ChecksumError("existing micromamba staging entry is unsafe") from exc
        descriptor, temporary_capability = open_private_file(
            temporary,
            create=True,
            read_write=True,
            exclusive=True,
            expected_parent=parent,
        )
        _download_to_fd(download_url(subdir, version=version), descriptor)
        os.fsync(descriptor)
        current = capture_windows_fd(
            descriptor,
            directory=False,
            generation_token=temporary_capability.generation_token,
        )
        if current != temporary_capability or _sha256_fd(descriptor) != expected:
            raise ChecksumError(f"micromamba sha256 不符（subdir={subdir}）")
        os.close(descriptor)
        descriptor = -1
        published = replace_windows_file(
            temporary,
            dest,
            source_parent=parent,
            expected_source=temporary_capability,
            expected_destination=destination,
        )
        if not _same_windows_generation(published, temporary_capability):
            raise ChecksumError("published micromamba File ID changed")
        validate_windows_path(parent, directory=True)
        return dest
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_capability is not None and os.path.lexists(temporary):
            try:
                live = capture_windows_path(
                    temporary,
                    directory=False,
                    generation_token=temporary_capability.generation_token,
                )
                if _same_windows_generation(live, temporary_capability):
                    delete_windows_file(temporary, parent=parent, expected=live)
            except OSError:
                pass


def _sha256_ok(path: Path, subdir: str | None, *, version: str) -> bool:
    expected = expected_sha256(subdir, version=version)
    if expected is None:
        expected = _fetch_text(_sha256_url(subdir, version=version)).split()[0].strip().lower()
    actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
    return actual == expected


def ensure_micromamba(
    dest: Path,
    *,
    subdir: str | None = None,
    version: str = MICROMAMBA_VERSION,
) -> Path:
    """幂等：若 dest 已存在且 sha256 合法直接用；否则下载到 .part 校验后原子改名。"""
    sd = subdir or platform.conda_subdir()
    if platform.is_windows():
        return _ensure_micromamba_windows(dest, subdir=sd, version=version)
    if dest.exists() and dest.stat().st_size > 0 and _sha256_ok(
        dest,
        sd,
        version=version,
    ):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        _download(download_url(sd, version=version), tmp)
        if not _sha256_ok(tmp, sd, version=version):
            raise ChecksumError(f"micromamba sha256 不符（subdir={sd}）")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    if not platform.is_windows():
        os.chmod(dest, dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest
