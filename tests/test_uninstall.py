"""标记/直删/护栏。全部 monkeypatch VIBECAD_HOME → tmp，不碰真实目录。

例外：test_refuses_home_dir / test_refuses_root 故意指向危险路径——
被测行为正是护栏拒删，全程不应有任何删除发生。
"""
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
    (home / "mamba").mkdir(parents=True)
    (home / "mamba" / "f.bin").write_bytes(b"x" * 2048)
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


def test_refuses_home_dir(monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", "~")
    assert uninstall.uninstall_now()["ok"] is False


def test_refuses_root(monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", "/")
    assert uninstall.uninstall_now()["ok"] is False


def test_refuses_dir_without_sentinel(monkeypatch, tmp_path):
    """深路径但无 VibeCAD 安装产物（如用户文档目录）→ 拒删且文件无恙。"""
    d = tmp_path / "a" / "b" / "Documents"
    d.mkdir(parents=True)
    (d / "important.txt").write_text("user data")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    assert uninstall.uninstall_now()["ok"] is False
    assert (d / "important.txt").exists()


def test_request_refuses_unsafe_home_before_marking(monkeypatch, tmp_path):
    """request 也要在写标记前拦截（不能给被污染的目录埋雷）。"""
    d = tmp_path / "x" / "y" / "Docs"
    d.mkdir(parents=True)
    (d / "f.txt").write_text("z")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    info = uninstall.request_uninstall()
    assert info["ok"] is False and not (d / ".uninstall_requested").exists()


def test_mcpb_runtime_path_allowed(monkeypatch, tmp_path):
    """合法 mcpb 路径（名叫 runtime 但含 status.json）→ 放行不误伤。"""
    d = tmp_path / "Claude Extensions" / "local.mcpb.x" / "runtime"
    d.mkdir(parents=True)
    (d / "status.json").write_text("{}")
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    assert uninstall.uninstall_now()["ok"] is True and not d.exists()


def test_empty_dir_allowed(monkeypatch, tmp_path):
    """空 home（已清洁/未安装）→ 放行删除无危害。"""
    d = tmp_path / "deep" / "VibeCADHome"
    d.mkdir(parents=True)
    monkeypatch.setenv("VIBECAD_HOME", str(d))
    assert uninstall.uninstall_now()["ok"] is True
