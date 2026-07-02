"""launcher 单元测试：--uninstall CLI 分支、启动清理、对监督进程的委托。

原三态判断（已在 conda / ready re-exec / bootstrap）随 Round 11 C 分支迁入
supervisor._server_cmd，等价单元测试见 tests/test_supervisor.py（判据不变）。
"""
import types

import pytest

from vibecad import launcher


@pytest.fixture(autouse=True)
def sup_stub(monkeypatch):
    """替身监督进程：默认返回码 0；记录调用序，绝不真起子进程。"""
    state = {"code": 0, "calls": []}

    class _FakeSupervisor:
        def run(self):
            state["calls"].append("supervisor.run")
            return state["code"]

    monkeypatch.setattr(launcher, "supervisor", types.SimpleNamespace(
        Supervisor=_FakeSupervisor,
        run_pending_uninstall=lambda: state["calls"].append("run_pending_uninstall"),
    ))
    return state


# --- 委托监督进程（Round 11 C 分支） ---


def test_main_delegates_to_supervisor_and_exits_zero(monkeypatch, sup_stub):
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad"])

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 0
    assert sup_stub["calls"] == ["run_pending_uninstall", "supervisor.run"]


def test_main_propagates_supervisor_exit_code(monkeypatch, sup_stub):
    """server 真崩溃码经 supervisor 返回后，launcher 原样透传给宿主。"""
    sup_stub["code"] = 3
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad"])

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 3


def test_main_runs_pending_uninstall_before_supervisor(monkeypatch, sup_stub):
    """I1：launcher 委托 supervisor.run_pending_uninstall（含「卸载未完成」响亮
    警告），且必须先于监督进程启动执行。"""
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad"])

    with pytest.raises(SystemExit):
        launcher.main()

    assert sup_stub["calls"] == ["run_pending_uninstall", "supervisor.run"]


# --- --uninstall CLI 分支（Task 4 Step 4） ---


def test_uninstall_flag_calls_uninstall_now_and_exits(monkeypatch, sup_stub, capsys):
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall", "--yes"])
    called = {}

    def _fake_uninstall_now():
        called["called"] = True
        return {"ok": True}
    monkeypatch.setattr(launcher.uninstall, "uninstall_now", _fake_uninstall_now)

    launcher.main()

    assert called.get("called") is True
    assert sup_stub["calls"] == []  # --uninstall 后不应继续走启动逻辑（不起监督进程）
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


def test_uninstall_tty_eof_treated_as_cancel(monkeypatch, capsys):
    """伪 TTY 下 stdin EOF（Ctrl-D / 管道关闭）不应 traceback，应视为取消。"""
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall"])
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)

    def _eof(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    called = {}
    monkeypatch.setattr(
        launcher.uninstall, "uninstall_now",
        lambda: called.setdefault("called", True),
    )

    launcher.main()  # 不应抛出

    assert "called" not in called
    assert "已取消" in capsys.readouterr().out


def test_uninstall_failure_exits_nonzero(monkeypatch):
    """护栏拒删等 ok:false 结果应以非零退出码结束，供脚本化调用方判断。"""
    monkeypatch.setattr(launcher.sys, "argv", ["vibecad", "--uninstall", "--yes"])
    monkeypatch.setattr(
        launcher.uninstall, "uninstall_now",
        lambda: {"ok": False, "message": "目录不含 VibeCAD 安装产物，拒绝删除"},
    )

    with pytest.raises(SystemExit) as exc:
        launcher.main()

    assert exc.value.code == 1
