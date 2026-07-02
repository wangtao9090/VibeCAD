from pathlib import Path

import pytest

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


# --- --uninstall CLI 分支 + 启动清理（Task 4 Step 4） ---


@pytest.fixture(autouse=True)
def _stub_normal_startup(monkeypatch, tmp_path):
    """未涉及 --uninstall 的用例默认不应真的走三态判断，统一 stub 掉。"""
    monkeypatch.setattr(launcher.paths, "active_runtime_python", lambda: tmp_path / "nope")
    monkeypatch.setattr(launcher.status, "runtime_ready", lambda: False)
    monkeypatch.setattr(launcher, "_run_server", lambda: None)


def test_uninstall_flag_calls_uninstall_now_and_exits(monkeypatch, capsys):
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall", "--yes"])
    called = {}

    def _fake_uninstall_now():
        called["called"] = True
        return {"ok": True}
    monkeypatch.setattr(launcher.uninstall, "uninstall_now", _fake_uninstall_now)
    reexec_called = {}
    monkeypatch.setattr(
        launcher, "_run_server", lambda: reexec_called.setdefault("server", True)
    )
    monkeypatch.setattr(
        launcher, "_reexec_into", lambda p: reexec_called.setdefault("reexec", True)
    )

    launcher.main()

    assert called.get("called") is True
    assert not reexec_called  # --uninstall 后不应继续走启动逻辑
    assert '"ok": true' in capsys.readouterr().out


def test_uninstall_yes_skips_tty_prompt(monkeypatch):
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall", "--yes"])
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)

    def _boom(*a, **kw):
        raise AssertionError("--yes 时不应询问确认")
    monkeypatch.setattr("builtins.input", _boom)
    monkeypatch.setattr(launcher.uninstall, "uninstall_now", lambda: {"ok": True})

    launcher.main()  # 不应抛出


def test_uninstall_non_tty_skips_prompt(monkeypatch):
    """非交互环境（无 --yes 但 stdin 非 TTY，如 CI/管道）也不应阻塞等待输入。"""
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall"])
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: False)

    def _boom(*a, **kw):
        raise AssertionError("非 TTY 不应调用 input")
    monkeypatch.setattr("builtins.input", _boom)
    called = {}

    def _fake_uninstall_now():
        called["called"] = True
        return {"ok": True}
    monkeypatch.setattr(launcher.uninstall, "uninstall_now", _fake_uninstall_now)

    launcher.main()

    assert called.get("called") is True


def test_uninstall_tty_prompt_declined_cancels(monkeypatch, capsys):
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall"])
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    called = {}
    monkeypatch.setattr(
        launcher.uninstall, "uninstall_now",
        lambda: called.setdefault("called", True),
    )

    launcher.main()

    assert "called" not in called
    assert "已取消" in capsys.readouterr().out


def test_uninstall_tty_prompt_accepted(monkeypatch):
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall"])
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    called = {}

    def _fake_uninstall_now():
        called["called"] = True
        return {"ok": True}
    monkeypatch.setattr(launcher.uninstall, "uninstall_now", _fake_uninstall_now)

    launcher.main()

    assert called.get("called") is True


def test_main_calls_perform_pending_uninstall(monkeypatch):
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad"])
    called = {}
    monkeypatch.setattr(
        launcher.uninstall, "perform_pending_uninstall",
        lambda: called.setdefault("called", True),
    )
    monkeypatch.setattr(launcher, "_run_server", lambda: called.setdefault("server", True))

    launcher.main()

    assert called.get("called") is True
    assert called.get("server") is True  # 清理后仍照常继续启动
