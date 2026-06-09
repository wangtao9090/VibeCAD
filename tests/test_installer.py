import pytest

from vibecad.runtime import installer as inst
from vibecad.runtime.status import Phase


def test_install_happy_path(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)   # 不短路
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda dest, **k: dest)
    ran = []
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: ran.append(cmd))
    monkeypatch.setattr(inst.status, "verify_runtime", lambda *a, **k: True)      # VERIFYING 过
    seen = []
    inst.RuntimeInstaller(on_progress=lambda s: seen.append(s.phase)).install()
    assert Phase.CREATING_ENV in seen and Phase.INSTALLING_PIP in seen and seen[-1] is Phase.READY
    create = " ".join(map(str, ran[0]))
    assert "create" in create and "python=3.12" in create and "freecad=1.1.0" in create
    assert inst.paths.ready_sentinel().exists()  # 写了哨兵


def test_is_ready_uses_sentinel(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.status, "runtime_ready", lambda: True)
    assert inst.RuntimeInstaller().is_ready() is True


def test_install_failed_on_smoke(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    monkeypatch.setattr(inst.RuntimeInstaller, "is_ready", lambda self: False)
    monkeypatch.setattr(inst.micromamba, "ensure_micromamba", lambda dest, **k: dest)
    monkeypatch.setattr(inst, "_run", lambda cmd, **k: None)
    monkeypatch.setattr(inst.status, "verify_runtime", lambda *a, **k: False)
    seen = []
    with pytest.raises(inst.InstallError):
        inst.RuntimeInstaller(on_progress=lambda s: seen.append(s)).install()
    assert seen[-1].phase is Phase.FAILED


def test_run_redirects_stdout(monkeypatch, tmp_path):
    # B2: 子进程绝不继承 fd1
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    captured = {}

    class P:
        returncode = 0
        stdout = "ok"
    def fake_run(cmd, **kw):
        captured.update(kw)
        return P()
    monkeypatch.setattr(inst.subprocess, "run", fake_run)
    inst._run(["echo", "hi"])
    assert captured["stdout"] is inst.subprocess.PIPE
    assert captured["stderr"] is inst.subprocess.STDOUT


def test_run_raises_on_nonzero(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))  # m-7：非零返回码须抛 InstallError

    class P:
        returncode = 1
        stdout = "boom"
    monkeypatch.setattr(inst.subprocess, "run", lambda cmd, **kw: P())
    with pytest.raises(inst.InstallError):
        inst._run(["false"])
