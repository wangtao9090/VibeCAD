import pytest

from vibecad.runtime import platform as p


def test_known_platforms(monkeypatch):
    cases = [
        ("darwin", "arm64", "osx-arm64"), ("darwin", "x86_64", "osx-64"),
        ("linux", "x86_64", "linux-64"), ("linux", "aarch64", "linux-aarch64"),
        ("win32", "AMD64", "win-64"),
    ]
    for sysname, machine, expected in cases:
        monkeypatch.setattr(p.sys, "platform", sysname)
        monkeypatch.setattr(p, "_machine", lambda m=machine: m)
        assert p.conda_subdir() == expected


def test_unsupported(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "win32")
    monkeypatch.setattr(p, "_machine", lambda: "ARM64")
    with pytest.raises(p.UnsupportedPlatformError):
        p.conda_subdir()


def test_asset_table_complete():
    for s in ("linux-64", "linux-aarch64", "osx-64", "osx-arm64", "win-64"):
        assert s in p.MICROMAMBA_ASSET


def test_os_predicates(monkeypatch):
    monkeypatch.setattr(p.sys, "platform", "darwin")
    assert p.is_macos() and not p.is_windows()
    monkeypatch.setattr(p.sys, "platform", "win32")
    assert p.is_windows() and not p.is_macos()
