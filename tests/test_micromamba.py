import hashlib

import pytest

from vibecad.runtime import micromamba as mm


def test_download_url_and_sha_url():
    assert mm.download_url("osx-arm64").endswith("/micromamba-osx-arm64")
    assert mm.download_url("win-64").endswith("/micromamba-win-64.exe")
    # B1: sha256 URL 永不含 .exe
    assert mm._sha256_url("win-64").endswith("/micromamba-win-64.sha256")
    assert ".exe.sha256" not in mm._sha256_url("win-64")


def test_download_verify_atomic(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"
    payload = b"fake-binary"
    digest = hashlib.sha256(payload).hexdigest()
    written = {}

    def fake_dl(url, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written["target"] = target
    monkeypatch.setattr(mm, "_download", fake_dl)
    monkeypatch.setattr(mm, "_fetch_text", lambda url: digest)  # m6: 单字段裸 hash
    out = mm.ensure_micromamba(dest, subdir="osx-arm64")
    assert out.read_bytes() == payload
    assert written["target"].name.endswith(".part")  # 下载先落 .part
    assert not (tmp_path / "bin" / "micromamba.part").exists()  # 已原子改名


def test_existing_file_reverified(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"good")
    monkeypatch.setattr(mm, "_fetch_text", lambda url: hashlib.sha256(b"good").hexdigest())
    monkeypatch.setattr(
        mm, "_download", lambda u, t: pytest.fail("should not download valid existing")
    )
    assert mm.ensure_micromamba(dest, subdir="osx-arm64") == dest


def test_checksum_mismatch(tmp_path, monkeypatch):
    dest = tmp_path / "bin" / "micromamba"

    def fake_dl_bad(u, t):
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_bytes(b"x")

    monkeypatch.setattr(mm, "_download", fake_dl_bad)
    monkeypatch.setattr(mm, "_fetch_text", lambda url: "deadbeef")
    with pytest.raises(mm.ChecksumError):
        mm.ensure_micromamba(dest, subdir="osx-arm64")
    assert not (tmp_path / "bin" / "micromamba.part").exists()  # 失败清理 .part
