"""标记/直删/护栏。全部 monkeypatch VIBECAD_HOME → tmp，不碰真实目录。"""
from vibecad.runtime import uninstall


def test_request_marks_and_perform_deletes(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "mamba").mkdir(parents=True)
    (home / "mamba" / "big.bin").write_bytes(b"x" * 1024)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    assert uninstall.request_uninstall()["marked"]
    assert uninstall.perform_pending_uninstall() is True
    assert not home.exists()


def test_perform_noop_without_marker(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    assert uninstall.perform_pending_uninstall() is False
    assert tmp_path.exists()


def test_uninstall_now_reports_size(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "envs").mkdir(parents=True)
    (home / "envs" / "f.bin").write_bytes(b"x" * 2048)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    info = uninstall.uninstall_now()
    assert info["ok"] and not home.exists() and info["freed_mb"] >= 0


def test_override_env_never_deleted(monkeypatch, tmp_path):
    """VIBECAD_FREECAD_ENV 用户自带 env 在 home 之外——删除 home 不得波及。"""
    home, override = tmp_path / "home", tmp_path / "user-env"
    home.mkdir()
    override.mkdir()
    (override / "keep").write_text("x")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    uninstall.request_uninstall()
    uninstall.perform_pending_uninstall()
    assert not home.exists() and (override / "keep").exists()
