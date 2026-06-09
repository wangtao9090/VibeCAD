from pathlib import Path

from vibecad import launcher


def test_already_in_runtime_runs_server(monkeypatch, tmp_path):
    py = tmp_path / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(launcher.paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(launcher.sys, "executable", str(py))
    started = {}
    monkeypatch.setattr(launcher, "_run_server", lambda: started.setdefault("server", True))
    launcher.main()
    assert started["server"]


def test_ready_reexecs(monkeypatch, tmp_path):
    py = tmp_path / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(launcher.paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(launcher.sys, "executable", "/uv/tmp/python")
    monkeypatch.setattr(launcher.status, "runtime_ready", lambda: True)
    reexec = {}
    monkeypatch.setattr(launcher, "_reexec_into", lambda p: reexec.setdefault("py", Path(p)))
    launcher.main()
    assert reexec["py"] == py


def test_not_ready_bootstraps(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.paths, "active_runtime_python", lambda: tmp_path / "nope")
    monkeypatch.setattr(launcher.sys, "executable", "/uv/tmp/python")
    monkeypatch.setattr(launcher.status, "runtime_ready", lambda: False)
    started = {}
    monkeypatch.setattr(launcher, "_run_server", lambda: started.setdefault("bootstrap", True))
    launcher.main()
    assert started["bootstrap"]


def test_reexec_posix_uses_execv(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    called = {}
    monkeypatch.setattr(launcher.os, "execv", lambda p, a: called.setdefault("execv", (p, a)))
    launcher._reexec_into(tmp_path / "bin" / "python")
    assert called["execv"][1][1:] == ["-m", "vibecad.server"]


def test_reexec_windows_uses_subprocess(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.sys, "platform", "win32")

    class R:
        returncode = 0
    monkeypatch.setattr(launcher.subprocess, "run", lambda a: R())
    raised = False
    try:
        launcher._reexec_into(tmp_path / "python.exe")
    except SystemExit as e:
        raised = e.code == 0
    assert raised
