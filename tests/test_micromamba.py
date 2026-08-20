import hashlib
import os
import sys

import pytest

from vibecad.runtime import micromamba as mm

pytestmark = pytest.mark.windows_contract


def test_download_url_and_sha_url():
    assert mm.download_url("osx-arm64").endswith("/micromamba-osx-arm64")
    assert mm.download_url("win-64").endswith("/micromamba-win-64.exe")
    assert f"/{mm.MICROMAMBA_VERSION}/" in mm.download_url("win-64")
    assert f"/{mm.WINDOWS_FLAT_CACHE_VERSION}/" in mm.download_url(
        "win-64",
        version=mm.WINDOWS_FLAT_CACHE_VERSION,
    )
    # B1: sha256 URL 永不含 .exe
    assert mm._sha256_url("win-64").endswith("/micromamba-win-64.sha256")
    assert ".exe.sha256" not in mm._sha256_url("win-64")


def test_download_verify_atomic(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"
    payload = b"fake-binary"
    digest = hashlib.sha256(payload).hexdigest()
    written = {}

    if sys.platform == "win32":

        def fake_dl_fd(url, descriptor):
            os.write(descriptor, payload)
            written["target"] = mm.capture_windows_fd(descriptor, directory=False)

        monkeypatch.setattr(mm, "_download_to_fd", fake_dl_fd)
    else:

        def fake_dl(url, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            written["target"] = target

        monkeypatch.setattr(mm, "_download", fake_dl)
    monkeypatch.setattr(mm, "_fetch_text", lambda url: digest)  # m6: 单字段裸 hash
    out = mm.ensure_micromamba(dest, subdir="osx-arm64")
    assert out.read_bytes() == payload
    if sys.platform == "win32":
        assert written["target"].path.endswith(".part")
        assert mm.capture_windows_path(out, directory=False).owner_sid.startswith("S-1-")
    else:
        assert written["target"].name.endswith(".part")  # 下载先落 .part
    assert not (tmp_path / "bin" / "micromamba.part").exists()  # 已原子改名


def test_existing_file_reverified(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"good")
    monkeypatch.setattr(mm, "_fetch_text", lambda url: hashlib.sha256(b"good").hexdigest())
    if sys.platform == "win32":
        monkeypatch.setattr(
            mm,
            "_download_to_fd",
            lambda u, d: pytest.fail("should not download valid existing"),
        )
    else:
        monkeypatch.setattr(
            mm, "_download", lambda u, t: pytest.fail("should not download valid existing")
        )
    assert mm.ensure_micromamba(dest, subdir="osx-arm64") == dest
    if sys.platform == "win32":
        assert mm.capture_windows_path(dest, directory=False).owner_sid.startswith("S-1-")


def test_version_specific_download_and_checksum_urls_match(monkeypatch, tmp_path):
    dest = tmp_path / "micromamba-flat-cache"
    payload = b"flat-cache-engine"
    seen = []

    if sys.platform == "win32":

        def download_fd(url, descriptor):
            seen.append(url)
            os.write(descriptor, payload)

        monkeypatch.setattr(mm, "_download_to_fd", download_fd)
    else:

        def download(url, target):
            seen.append(url)
            target.write_bytes(payload)

        monkeypatch.setattr(mm, "_download", download)
    monkeypatch.setitem(
        mm._PINNED_SHA256,
        (mm.WINDOWS_FLAT_CACHE_VERSION, "win-64"),
        hashlib.sha256(payload).hexdigest(),
    )
    monkeypatch.setattr(
        mm,
        "_fetch_text",
        lambda url: pytest.fail("qualified Windows hashes must not be fetched at runtime"),
    )

    mm.ensure_micromamba(
        dest,
        subdir="win-64",
        version=mm.WINDOWS_FLAT_CACHE_VERSION,
    )

    assert len(seen) == 1
    assert all(f"/{mm.WINDOWS_FLAT_CACHE_VERSION}/" in url for url in seen)


def test_qualified_windows_binary_hashes_are_embedded() -> None:
    assert mm.expected_sha256("win-64") == (
        "bd77a64b9ca1c57c10e30cf54561776010d72f065305dbcf92311e7358b61322"
    )
    assert mm.expected_sha256(
        "win-64",
        version=mm.WINDOWS_FLAT_CACHE_VERSION,
    ) == "baf6d56a31a63a75f0c77bdce27fefea57b1bdc8aa25b50a1be45696c0e737e7"


def test_checksum_mismatch(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"

    if sys.platform == "win32":

        def fake_dl_bad_fd(u, descriptor):
            os.write(descriptor, b"x")

        monkeypatch.setattr(mm, "_download_to_fd", fake_dl_bad_fd)
    else:

        def fake_dl_bad(u, t):
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_bytes(b"x")

        monkeypatch.setattr(mm, "_download", fake_dl_bad)
    monkeypatch.setattr(mm, "_fetch_text", lambda url: "deadbeef")
    with pytest.raises(mm.ChecksumError):
        mm.ensure_micromamba(dest, subdir="osx-arm64")
    assert not (tmp_path / "bin" / "micromamba.part").exists()  # 失败清理 .part
