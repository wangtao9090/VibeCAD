from vibecad.runtime import paths


def test_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "vc"))
    assert paths.vibecad_home() == tmp_path / "vc"


def test_env_layout_unix(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: False)
    assert paths.env_prefix() == tmp_path / "mamba" / "envs" / "vibecad"
    assert paths.env_python() == tmp_path / "mamba" / "envs" / "vibecad" / "bin" / "python"
    assert paths.ready_sentinel() == tmp_path / "mamba" / "envs" / "vibecad" / ".vibecad_ready"


def test_env_python_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: True)
    assert paths.env_python().name == "python.exe"
    assert paths.micromamba_path().name == "micromamba.exe"
    env = tmp_path / "mamba" / "envs" / "vibecad"
    assert paths.freecadcmd_path() == env / "Library" / "bin" / "FreeCADCmd.exe"


def test_active_runtime_prefers_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(paths.platform, "is_windows", lambda: False)
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(tmp_path / "myenv"))
    assert paths.active_runtime_prefix() == tmp_path / "myenv"
    assert paths.active_runtime_python() == tmp_path / "myenv" / "bin" / "python"
    assert paths.ready_sentinel() == tmp_path / "myenv" / ".vibecad_ready"
