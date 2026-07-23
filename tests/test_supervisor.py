"""监督进程测试（Round 11 C 分支 + 双路审查修复）。

黑盒部分：subprocess 起 `python -m vibecad`（launcher → supervisor），
VIBECAD_SUPERVISOR_TEST_CMD 注入假 server（tests/fake_server.py），验证：
透传、换芯 gen=1→2、握手重放响应被丢弃、换芯后请求继续成功、正常退出码 0、
真崩溃码原样透传、stdin EOF 干净退出无孤儿、换芯 spawn 前执行 pending-uninstall、
换芯循环护栏（C1）、握手超时强杀无孤儿（C2）。

单元部分：runtime_swappable 换芯判据（C1 单一真源）、_server_cmd 三态、
泵线程崩溃兜底与非法 id 记账降级（C3）、EOF/换芯竞态（I2）、宽限回收升级
kill（I3）、卸载未完成响亮警告（I1）、VIBECAD_SUPERVISED 注入（I4）、
不注入 TEST_CMD 的真实 _server_cmd 集成（T3）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from vibecad import mcp_transport, supervisor
from vibecad.runtime.status import _pid_alive

FAKE_SERVER = str(Path(__file__).resolve().parent / "fake_server.py")


# --- 黑盒：launcher → supervisor → fake server ---


@pytest.fixture
def sup_factory(tmp_path):
    """起黑盒 supervisor 进程；测试结束兜底 kill，绝不留进程。"""
    procs: list[subprocess.Popen] = []

    def factory(extra_env: dict | None = None) -> subprocess.Popen:
        env = {
            **os.environ,
            "VIBECAD_SUPERVISOR_TEST_CMD": json.dumps([sys.executable, FAKE_SERVER]),
            "VIBECAD_FAKE_GEN_FILE": str(tmp_path / "gen"),
            "VIBECAD_HOME": str(tmp_path / "home"),  # 隔离：绝不触碰真实运行时目录
            **(extra_env or {}),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "vibecad"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
        )
        procs.append(proc)
        return proc

    yield factory
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()


def _readline(proc: subprocess.Popen, timeout: float = 15.0) -> bytes:
    """带超时读一行：supervisor 卡死时测试快速失败而非挂起整个会话。"""
    box: dict[str, bytes] = {}
    t = threading.Thread(target=lambda: box.setdefault("line", proc.stdout.readline()), daemon=True)
    t.start()
    t.join(timeout)
    if "line" not in box:
        proc.kill()
        pytest.fail(f"supervisor 未在 {timeout}s 内产出响应行")
    return box["line"]


def _send(proc: subprocess.Popen, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj).encode() + b"\n")
    proc.stdin.flush()


def _handshake(proc: subprocess.Popen) -> dict:
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "blackbox-test", "version": "1"},
            },
        },
    )
    response = json.loads(_readline(proc))
    _send(
        proc,
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
    )
    return response


def _rpc(
    proc: subprocess.Popen,
    id_: int,
    method: str,
    params: dict | None = None,
) -> dict:
    request = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        request["params"] = params
    _send(proc, request)
    return json.loads(_readline(proc))


def test_passthrough_and_swap(sup_factory):
    """透传 + 换芯 gen=1→2 + 握手重放响应被丢弃 + 换芯后请求零感知成功 + 正常退出 0。"""
    sup = sup_factory({"VIBECAD_FAKE_SWAP_TOOL": "get_runtime_status"})
    assert _handshake(sup)["result"]["serverInfo"]["version"] == "1"

    assert _rpc(sup, 1, "ping")["result"]["gen"] == 1  # 换芯前
    resp = _rpc(
        sup,
        2,
        "tools/call",
        {"name": "get_runtime_status", "arguments": {}},
    )
    # 客户端从未见到第二份 initialize 响应（重放响应被监督进程丢弃）——
    # rpc(2) 直接读到 id=2 的响应即证明
    assert resp["id"] == 2
    assert resp["result"]["gen"] == 2  # 换芯后零感知成功
    sup.stdin.close()
    assert sup.wait(timeout=15) == 0


def test_stdin_eof_clean_exit_no_orphan(sup_factory, tmp_path):
    """要点①：宿主关闭（stdin EOF）→ supervisor 连同子进程干净退出，绝不留孤儿。"""
    pid_file = tmp_path / "fake.pid"
    sup = sup_factory({"VIBECAD_FAKE_PID_FILE": str(pid_file)})
    _handshake(sup)  # 同步点：子进程已起且已落 PID
    child_pid = int(pid_file.read_text())
    assert _pid_alive(child_pid)

    sup.stdin.close()
    assert sup.wait(timeout=15) == 0  # supervisor 等子进程收尾后自身退出
    assert not _pid_alive(child_pid)  # 子进程无孤儿残留


def test_real_crash_code_passthrough(sup_factory):
    """真崩溃（非 SWAP_EXIT）：退出码原样透传，不掩盖、不重启。"""
    sup = sup_factory({"VIBECAD_FAKE_CRASH_METHOD": "ping"})
    _handshake(sup)
    _send(sup, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
    assert sup.wait(timeout=15) == 3


def test_pending_uninstall_runs_before_respawn(sup_factory, tmp_path):
    """要点③：supervisor 换芯重启不经 launcher.main，每次 spawn 前必须执行
    perform_pending_uninstall——运行中落下的卸载标记在换芯重启时被兑现，
    但只删除有所有权证据的运行时，保留模糊 legacy 内容。"""
    home = tmp_path / "home"
    (home / "runtime").mkdir(parents=True)  # 新版固定运行时目标：可授权删除
    (home / "runtime" / "owned.bin").write_bytes(b"runtime")
    (home / "mamba").mkdir(parents=True)  # 只有路径名不能证明 legacy 所有权
    (home / "status.json").write_text("{}")
    sup = sup_factory({"VIBECAD_FAKE_SWAP_TOOL": "get_runtime_status"})
    _handshake(sup)
    assert home.exists()  # 启动清理不背锅：此时尚无标记

    (home / ".uninstall_requested").touch()  # 模拟运行中 request_uninstall 落标记
    response = _rpc(
        sup,
        1,
        "tools/call",
        {"name": "get_runtime_status", "arguments": {}},
    )
    assert response["result"]["gen"] == 2  # 同步点：重启已完成
    assert not (home / "runtime").exists()  # spawn 前已删掉可证明归属的运行时
    assert not (home / ".uninstall_requested").exists()
    assert (home / "mamba").is_dir()
    assert (home / "status.json").read_text() == "{}"

    sup.stdin.close()
    assert sup.wait(timeout=15) == 0


def test_swap_loop_guard_blackbox(sup_factory, tmp_path):
    """C1②黑盒：新子进程一起来又自杀（换芯判据两侧不一致的坏形态）——循环护栏
    拦住无限重启，supervisor 以非零码（SWAP_EXIT）响亮退出，绝不静默循环。"""
    sup = sup_factory({"VIBECAD_FAKE_SWAP_ON_START": "1"})
    assert sup.wait(timeout=15) == supervisor.SWAP_EXIT
    # 重启在护栏限制内停下（首启 + 有限次重启），不是无限循环
    assert int((tmp_path / "gen").read_text()) == supervisor._SWAP_LOOP_LIMIT


def test_swap_handshake_hang_exits_nonzero_no_orphan(sup_factory, tmp_path):
    """C2 黑盒：换芯后新子进程收到重放 initialize 既不响应也不退出（conda 首启慢/
    import 死锁形态）——握手超时强杀新子进程、以非零码退出，无孤儿（PID 探活）。"""
    pid_file = tmp_path / "fake.pid"
    sup = sup_factory(
        {
            "VIBECAD_FAKE_HANG": "1",
            "VIBECAD_FAKE_SWAP_TOOL": "get_runtime_status",
            "VIBECAD_FAKE_PID_FILE": str(pid_file),
            "VIBECAD_TEST_HANDSHAKE_TIMEOUT": "3",  # 调小 deadline：黑盒不等 30s
        }
    )
    assert _handshake(sup)["result"]["serverInfo"]["version"] == "1"
    _send(
        sup,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "get_runtime_status", "arguments": {}},
        },
    )

    assert sup.wait(timeout=15) == 1  # 换芯失败：非零码响亮退出
    assert int((tmp_path / "gen").read_text()) == 2  # 新子进程确实起过（挂死的是它）
    assert not _pid_alive(int(pid_file.read_text()))  # 挂死子进程已被强杀，无孤儿


def test_default_keyed_create_task_is_replayed_after_response_loss(
    sup_factory,
    tmp_path,
):
    """响应前换芯后，幂等 create_task 以原 payload 重放并拿到新代响应。"""
    tool_log = tmp_path / "tools.log"
    sup = sup_factory(
        {
            "VIBECAD_FAKE_SWAP_TOOL": "create_task",
            "VIBECAD_FAKE_TOOL_LOG": str(tool_log),
        }
    )
    _handshake(sup)
    response = _rpc(
        sup,
        7,
        "tools/call",
        {"name": "create_task", "arguments": {}},
    )
    assert response["id"] == 7
    assert response["result"]["gen"] == 2
    assert response["result"]["tool"] == "create_task"
    assert _rpc(sup, 8, "ping")["result"]["gen"] == 2
    assert tool_log.read_text(encoding="utf-8").splitlines() == [
        "1:create_task",
        "2:create_task",
    ]

    sup.stdin.close()
    assert sup.wait(timeout=15) == 0


def test_unsafe_pending_request_gets_unknown_outcome_and_is_not_replayed(
    sup_factory,
    tmp_path,
):
    tool_log = tmp_path / "tools.log"
    sup = sup_factory(
        {
            "VIBECAD_FAKE_SWAP_TOOL": "unsafe_tool",
            "VIBECAD_FAKE_TOOL_LOG": str(tool_log),
        }
    )
    _handshake(sup)
    response = _rpc(
        sup,
        7,
        "tools/call",
        {"name": "unsafe_tool", "arguments": {}},
    )
    assert response == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32003,
            "message": "Tool outcome is unknown; inspect durable state before retry.",
        },
    }
    assert _rpc(sup, 8, "ping")["result"]["gen"] == 2
    assert tool_log.read_text(encoding="utf-8").splitlines() == ["1:unsafe_tool"]

    sup.stdin.close()
    assert sup.wait(timeout=15) == 0


# --- 单元：runtime_swappable 换芯判据（C1 单一真源） ---


@pytest.fixture(autouse=True)
def _no_test_cmd_leak(monkeypatch):
    """单元测试默认不受外部 VIBECAD_SUPERVISOR_TEST_CMD 污染。"""
    monkeypatch.delenv("VIBECAD_SUPERVISOR_TEST_CMD", raising=False)


def test_runtime_swappable_true_when_sentinel_and_python(monkeypatch, tmp_path):
    from vibecad.runtime import paths, status

    py = tmp_path / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(status, "runtime_ready", lambda: True)
    assert supervisor.runtime_swappable() is True


def test_runtime_swappable_false_when_python_missing(monkeypatch, tmp_path):
    """C1 核心：哨兵在而 conda python 缺失（半删/杀毒隔离/卸载半失败）→ 不可换芯。"""
    from vibecad.runtime import paths, status

    monkeypatch.setattr(paths, "active_runtime_python", lambda: tmp_path / "nope")
    monkeypatch.setattr(status, "runtime_ready", lambda: True)
    assert supervisor.runtime_swappable() is False


def test_runtime_swappable_false_when_not_ready(monkeypatch, tmp_path):
    from vibecad.runtime import paths, status

    py = tmp_path / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(status, "runtime_ready", lambda: False)
    assert supervisor.runtime_swappable() is False


def test_runtime_swappable_oserror_is_loud_and_false(monkeypatch, capsys):
    """N3：判据探测 OSError → 按不可换芯处理，且 stderr 记一行原因（不静默）。"""
    from vibecad.runtime import status

    def _boom():
        raise OSError("permission denied")

    monkeypatch.setattr(status, "runtime_ready", _boom)
    assert supervisor.runtime_swappable() is False
    assert "permission denied" in capsys.readouterr().err


# --- 单元：_server_cmd 三态判据（与 runtime_swappable 同一真源） ---


def test_server_cmd_test_override(monkeypatch):
    monkeypatch.setenv("VIBECAD_SUPERVISOR_TEST_CMD", json.dumps(["fakepy", "srv.py"]))
    assert supervisor._server_cmd() == ["fakepy", "srv.py"]


def test_server_cmd_ready_uses_conda_python(monkeypatch, tmp_path):
    from vibecad.runtime import paths, status

    py = tmp_path / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(status, "runtime_ready", lambda: True)
    assert supervisor._server_cmd() == [str(py), "-B", "-m", "vibecad.server"]


def test_server_cmd_not_ready_bootstraps(monkeypatch, tmp_path):
    from vibecad.runtime import paths, status

    py = tmp_path / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    monkeypatch.setattr(paths, "active_runtime_python", lambda: py)
    monkeypatch.setattr(status, "runtime_ready", lambda: False)
    assert supervisor._server_cmd() == [sys.executable, "-m", "vibecad.server"]


def test_server_cmd_ready_but_python_missing_bootstraps(monkeypatch, tmp_path):
    from vibecad.runtime import paths, status

    monkeypatch.setattr(paths, "active_runtime_python", lambda: tmp_path / "nope")
    monkeypatch.setattr(status, "runtime_ready", lambda: True)
    assert supervisor._server_cmd() == [sys.executable, "-m", "vibecad.server"]


# --- 单元：spawn 接线（卸载清理 / VIBECAD_SUPERVISED / 真实 _server_cmd 集成） ---


def test_spawn_runs_pending_uninstall_before_popen(monkeypatch):
    """要点③接线：每次 _spawn 先清理待删标记，再选解释器起子进程（删 home 连带清掉
    就绪哨兵，_server_cmd 才会安全落 bootstrap）。"""
    order: list[str] = []
    monkeypatch.setattr(
        supervisor.uninstall,
        "perform_pending_uninstall",
        lambda: order.append("uninstall") or True,
    )
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *a, **kw: order.append("spawn") or "proc",
    )
    assert supervisor.Supervisor()._spawn() == "proc"
    assert order == ["uninstall", "spawn"]


def test_spawn_injects_supervised_env(monkeypatch):
    """I4：supervisor 拉起的子进程带 VIBECAD_SUPERVISED=1——server 据此判断
    「自杀后有人重启」；裸 server 无此标记则回退提示重连、绝不自杀。"""
    captured: dict = {}
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.setattr(supervisor.uninstall, "perform_pending_uninstall", lambda: True)
    monkeypatch.setattr(
        supervisor,
        "_server_selection",
        lambda *, allow_runtime=True: supervisor._ServerSelection(
            (sys.executable, "-m", "vibecad.server"),
            uses_runtime=False,
        ),
    )
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *a, **kw: captured.update(kw) or "proc",
    )
    assert supervisor.Supervisor()._spawn() == "proc"
    assert captured["env"]["VIBECAD_SUPERVISED"] == "1"
    assert "PYTHONDONTWRITEBYTECODE" not in captured["env"]


def test_spawn_runtime_env_seam_receives_exact_base_after_selection(monkeypatch):
    """managed/external runtime 子进程才经私有 FreeCAD 目录 seam，且顺序固定。"""
    from vibecad.runtime import status

    order: list[str] = []
    captured: dict[str, object] = {}
    selection = supervisor._ServerSelection(
        ("runtime-python", "-B", "-m", "vibecad.server"),
        uses_runtime=True,
    )
    monkeypatch.setattr(
        supervisor,
        "run_pending_uninstall",
        lambda: order.append("uninstall") or True,
    )
    monkeypatch.setattr(
        supervisor,
        "_server_selection",
        lambda *, allow_runtime=True: (
            order.append("select") or captured.update(allow_runtime=allow_runtime) or selection
        ),
    )

    def _runtime_environment(base):
        order.append("environment")
        captured["base"] = base
        return {**base, "FREECAD_USER_HOME": "/private/runtime/freecad-user/home"}

    monkeypatch.setattr(status, "freecad_process_environment", _runtime_environment)
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda command, **kwargs: (
            order.append("spawn") or captured.update(command=command, kwargs=kwargs) or "proc"
        ),
    )

    assert supervisor.Supervisor()._spawn() == "proc"
    expected_base = {
        **os.environ,
        "VIBECAD_SUPERVISED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert order == ["uninstall", "select", "environment", "spawn"]
    assert captured["allow_runtime"] is True
    assert captured["base"] == expected_base
    assert captured["command"] == list(selection.command)
    assert captured["kwargs"]["env"] == {
        **expected_base,
        "FREECAD_USER_HOME": "/private/runtime/freecad-user/home",
    }


@pytest.mark.skipif(sys.platform == "win32", reason="test uses a POSIX executable symlink")
def test_external_runtime_spawn_does_not_mutate_prefix_or_create_pycache(
    monkeypatch,
    tmp_path,
) -> None:
    """Runtime env injection protects even imports from an external prefix."""

    from vibecad.runtime import status

    prefix = tmp_path / "external"
    runtime_python = prefix / "bin" / "python"
    site_packages = prefix / "site-packages"
    runtime_python.parent.mkdir(parents=True)
    site_packages.mkdir()
    runtime_python.symlink_to(sys.executable)
    (site_packages / "external_probe.py").write_text("VALUE = 1\n", encoding="utf-8")

    def _snapshot() -> dict[str, tuple[str, bytes | str | None]]:
        snapshot: dict[str, tuple[str, bytes | str | None]] = {}
        for path in prefix.rglob("*"):
            relative = path.relative_to(prefix).as_posix()
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
            elif path.is_dir():
                snapshot[relative] = ("directory", None)
            else:
                snapshot[relative] = ("file", path.read_bytes())
        return snapshot

    before = _snapshot()
    code = "import sys; sys.path.insert(0, sys.argv[1]); import external_probe"
    selection = supervisor._ServerSelection(
        (str(runtime_python), "-c", code, str(site_packages)),
        uses_runtime=True,
    )
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.setattr(supervisor, "run_pending_uninstall", lambda: True)
    monkeypatch.setattr(
        supervisor,
        "_server_selection",
        lambda *, allow_runtime=True: selection,
    )
    captured: dict[str, str] = {}

    def _environment(base):
        captured.update(base)
        return dict(base)

    monkeypatch.setattr(status, "freecad_process_environment", _environment)

    child = supervisor.Supervisor()._spawn()
    child.stdin.close()
    assert child.wait(timeout=10) == 0
    child.stdout.close()

    assert captured["PYTHONDONTWRITEBYTECODE"] == "1"
    assert _snapshot() == before
    assert not any(path.name == "__pycache__" for path in prefix.rglob("*"))


def test_run_pending_uninstall_warns_when_marker_left(monkeypatch, tmp_path, capsys):
    """I1：删除未完成（perform 返回 False 且标记仍在，如 Windows 文件锁/杀毒占用）
    → stderr 一行警告，不再静默让用户以为已卸载干净。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".uninstall_requested").touch()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(supervisor.uninstall, "perform_pending_uninstall", lambda: False)

    assert supervisor.run_pending_uninstall() is False

    err = capsys.readouterr().err
    assert "卸载未完成" in err and str(home) in err


def test_run_pending_uninstall_quiet_when_no_marker(monkeypatch, tmp_path, capsys):
    """I1 反向：无标记的常规启动（返回 False 但无标记）→ 保持静默，不制造噪音。"""
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(supervisor.uninstall, "perform_pending_uninstall", lambda: False)

    assert supervisor.run_pending_uninstall() is True

    assert capsys.readouterr().err == ""


def test_spawn_partial_uninstall_forces_bootstrap_without_runtime_seam(
    monkeypatch,
    tmp_path,
) -> None:
    """A retained marker forbids selecting or recreating a residual runtime."""

    from vibecad.runtime import paths, status

    home = tmp_path / "home"
    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_AUTO_INSTALL", "1")
    runtime_python = paths.active_runtime_python()
    runtime_python.parent.mkdir(parents=True)
    runtime_python.touch()
    status.write_runtime_receipt()
    marker = home / ".uninstall_requested"
    marker.touch()
    monkeypatch.setattr(supervisor.uninstall, "perform_pending_uninstall", lambda: False)
    seam_calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        status,
        "freecad_process_environment",
        lambda base: seam_calls.append(base) or base,
    )
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda command, **kwargs: captured.update(command=command, kwargs=kwargs) or "proc",
    )

    assert supervisor.Supervisor()._spawn() == "proc"

    assert captured["command"] == [sys.executable, "-m", "vibecad.server"]
    assert captured["kwargs"]["env"]["VIBECAD_AUTO_INSTALL"] == "0"
    assert seam_calls == []
    assert marker.exists()
    assert not (home / "runtime" / "freecad-user").exists()


def test_successful_pending_uninstall_suppresses_auto_install_for_later_generations(
    monkeypatch,
    tmp_path,
) -> None:
    """A confirmed uninstall must not be undone by the replacement bootstrap."""

    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".uninstall_requested"
    marker.touch()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_AUTO_INSTALL", "1")
    environments: list[dict[str, str]] = []

    def _converge() -> bool:
        marker.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(supervisor, "run_pending_uninstall", _converge)
    monkeypatch.setattr(
        supervisor,
        "_server_selection",
        lambda *, allow_runtime=True: supervisor._ServerSelection(
            (sys.executable, "-m", "vibecad.server"),
            uses_runtime=False,
        ),
    )
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda _command, **kwargs: environments.append(kwargs["env"]) or "proc",
    )

    sup = supervisor.Supervisor()
    assert sup._spawn() == "proc"
    assert not marker.exists()
    assert environments[-1]["VIBECAD_AUTO_INSTALL"] == "0"

    # The marker is now gone, but this supervisor still belongs to the same
    # confirmed-uninstall session and every later generation must stay inert.
    assert sup._spawn() == "proc"
    assert environments[-1]["VIBECAD_AUTO_INSTALL"] == "0"


def test_spawn_real_cmd_uninstall_marker_falls_back_to_bootstrap(monkeypatch, tmp_path):
    """T3 集成（不注入 VIBECAD_SUPERVISOR_TEST_CMD，走真实 _server_cmd 耦合）：
    待删标记 + 完整就绪运行时（强哨兵/ready 哨兵/conda python 齐备）→ _spawn 先
    兑现授权运行时删除，保留模糊 legacy 字节；真实 _server_cmd 随之落回
    bootstrap 解释器——绝不引用已删的 conda python。"""
    from vibecad.runtime import paths, status

    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    py = paths.active_runtime_python()
    py.parent.mkdir(parents=True)
    py.touch()
    status.write_runtime_receipt()
    (home / "status.json").write_text("{}")  # 强哨兵：护栏认定是我们的目录
    (home / ".uninstall_requested").touch()  # 待删标记
    captured: dict = {}
    seam_calls: list[dict[str, str]] = []
    monkeypatch.setattr(
        status,
        "freecad_process_environment",
        lambda base: seam_calls.append(base) or base,
    )
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda cmd, **kw: captured.update(cmd=cmd) or "proc",
    )

    assert supervisor.Supervisor()._spawn() == "proc"

    assert home.exists()
    assert not (home / "runtime").exists()  # 待删标记已兑现，授权运行时消失
    assert seam_calls == []  # bootstrap 不得重建刚被卸载的 runtime/freecad-user
    assert not (home / ".uninstall_requested").exists()
    assert (home / "status.json").read_text() == "{}"
    assert captured["cmd"][0] == sys.executable  # 落回 bootstrap（无自杀循环隐患）


def test_spawn_real_cmd_ready_uses_conda_python(monkeypatch, tmp_path):
    """T3 对照：无待删标记且运行时完整 → 真实 _server_cmd 选 conda python。"""
    from vibecad.runtime import paths, status

    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    py = paths.active_runtime_python()
    py.parent.mkdir(parents=True)
    py.touch()
    status.write_runtime_receipt()
    captured: dict = {}
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda cmd, **kw: captured.update(cmd=cmd) or "proc",
    )

    supervisor.Supervisor()._spawn()

    assert captured["cmd"][0] == str(py)


def test_spawn_real_cmd_old_server_receipt_bootstraps(monkeypatch, tmp_path):
    """新版入口遇到旧 server receipt 时必须留在 bootstrap，绝不能加载旧工具表。"""
    from vibecad.runtime import paths, spec

    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    py = paths.active_runtime_python()
    py.parent.mkdir(parents=True)
    py.touch()
    receipt = {**spec.expected_receipt(), "vibecad_version": "0.3.0"}
    paths.ready_sentinel().write_text(json.dumps(receipt), encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda cmd, **kw: captured.update(cmd=cmd) or "proc",
    )

    supervisor.Supervisor()._spawn()

    assert captured["cmd"] == [sys.executable, "-m", "vibecad.server"]


def test_spawn_real_cmd_previous_positive_epoch_receipt_bootstraps(monkeypatch, tmp_path):
    """入口遇到上一私有 epoch receipt 时留在 bootstrap，等待原位 server 同步。"""
    from vibecad.runtime import paths, spec

    monkeypatch.delenv("VIBECAD_FREECAD_ENV", raising=False)
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    py = paths.active_runtime_python()
    py.parent.mkdir(parents=True)
    py.touch()
    receipt = {
        **spec.expected_receipt(),
        "server_package_epoch": spec.SERVER_PACKAGE_EPOCH - 1,
    }
    paths.ready_sentinel().write_text(json.dumps(receipt), encoding="utf-8")
    captured: dict = {}
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda cmd, **kw: captured.update(cmd=cmd) or "proc",
    )

    supervisor.Supervisor()._spawn()

    assert captured["cmd"] == [sys.executable, "-m", "vibecad.server"]


# --- 单元：泵线程与 run() 骨架 ---


class _FakePipe(io.BytesIO):
    """可关闭后仍读到已写内容的假管道。close 幂等——对齐真实 BufferedWriter 语义
    （EOF 收尾与换芯 EOF 分支可能先后各关一次）。"""

    def close(self) -> None:
        if not self.closed:
            self.closed_value = self.getvalue()
            super().close()


def _fake_stdin(monkeypatch, data: bytes):
    """把 supervisor 的 stdin 换成真 OS 管道（_stdin_lines 走原始 fd，需要 fileno）。"""
    r, w = os.pipe()
    if data:
        os.write(w, data)
    os.close(w)  # 写完即关：读端见 EOF
    stdin_file = os.fdopen(r, "rb", buffering=0)
    monkeypatch.setattr(supervisor.sys, "stdin", types.SimpleNamespace(buffer=stdin_file))
    return stdin_file


@pytest.fixture
def unclosed_stdin(monkeypatch):
    """把 supervisor 的 stdin 换成**不关写端**的 OS 管道：client 泵阻塞等待，
    _client_eof 不会提前置位（供需要走换芯路径的 run() 单测使用）。"""
    r, w = os.pipe()
    stdin_file = os.fdopen(r, "rb", buffering=0)
    monkeypatch.setattr(supervisor.sys, "stdin", types.SimpleNamespace(buffer=stdin_file))
    yield
    os.close(w)  # 收尾：让 daemon 泵线程读到 EOF 退出
    stdin_file.close()


class _SwapChild:
    """wait() 恒返回 SWAP_EXIT 的假子进程：模拟一起来就自杀换芯。"""

    def __init__(self):
        self.stdin = _FakePipe()
        self.stdout = io.BytesIO(b"")

    def wait(self):
        return supervisor.SWAP_EXIT

    def poll(self):
        return supervisor.SWAP_EXIT


class _KillableChild:
    """kill() 后 wait() 才返回的假子进程（run() 正阻塞在 wait 上）。"""

    def __init__(self):
        self.stdin = _FakePipe()
        self.stdout = io.BytesIO(b"")
        self._dead = threading.Event()

    def kill(self):
        self._dead.set()

    def wait(self):
        assert self._dead.wait(timeout=10), "子进程未被 kill，run() 将永挂"
        return -9

    def poll(self):
        return -9 if self._dead.is_set() else None


def test_swap_exit_after_client_eof_returns_clean(monkeypatch):
    """骨架修正：宿主已关 stdin 后子进程才以 SWAP_EXIT 退出（timer 与 EOF 竞态）——
    不得再重启新子进程（新子进程无人喂 stdin，会永远挂起成孤儿），应视为干净收尾。"""
    _fake_stdin(monkeypatch, b"")
    sup = supervisor.Supervisor()
    sup._client_eof.set()
    spawned: list[int] = []

    monkeypatch.setattr(sup, "_spawn", lambda: spawned.append(1) or _SwapChild())
    assert sup.run() == 0
    assert spawned == [1]  # 只有首次 spawn，绝无换芯复活


def test_swap_loop_guard_stops_restart_storm(unclosed_stdin, monkeypatch, capsys):
    """C1②：连续 SWAP_EXIT 间隔均 <5s → 判定自杀重启循环：stderr 写明原因、
    停止换芯、按真退出透传非零码——绝不无限循环零日志。"""
    sup = supervisor.Supervisor()
    spawned: list[int] = []
    monkeypatch.setattr(sup, "_spawn", lambda: spawned.append(1) or _SwapChild())

    assert sup.run() == supervisor.SWAP_EXIT  # 非零码响亮退出
    assert len(spawned) == supervisor._SWAP_LOOP_LIMIT  # 有限次后停下
    assert "换芯循环" in capsys.readouterr().err


def test_replay_handshake_timeout_kills_hung_child(monkeypatch, capsys):
    """C2：新子进程不死也不响应重放的 initialize → deadline 强杀、返回失败，
    绝不让宿主全部请求无限挂死。"""
    monkeypatch.setattr(supervisor, "_HANDSHAKE_TIMEOUT_SECONDS", 0.2)
    sup = supervisor.Supervisor()
    assert sup._protocol.accept(_initialize_payload(0)).kind is supervisor._ClientActionKind.FORWARD
    r, w = os.pipe()  # stdout 永不给数据：模拟挂死
    hung_stdout = os.fdopen(r, "rb")
    killed: list[int] = []

    def _kill():
        killed.append(1)
        os.close(w)  # kill 后 stdout EOF（读线程随之退出）

    child = types.SimpleNamespace(
        stdin=_FakePipe(), stdout=hung_stdout, kill=_kill, poll=lambda: None
    )
    try:
        assert sup._replay_handshake(child) is False
        assert killed == [1]
        assert "握手超时" in capsys.readouterr().err
    finally:
        if not killed:
            with contextlib.suppress(OSError):
                os.close(w)
        hung_stdout.close()


class _DeadPipe(io.BytesIO):
    def write(self, data):
        raise BrokenPipeError("child died")


def test_replay_handshake_survives_instantly_dead_child():
    """骨架修正：新子进程秒死（写端 BrokenPipe + stdout 秒 EOF）时握手重放不得抛
    异常，且按「握手已结束」返回 True——真退出码由 run() 主循环如实拿到并透传。"""
    sup = supervisor.Supervisor()
    assert sup._protocol.accept(_initialize_payload(0)).kind is supervisor._ClientActionKind.FORWARD
    child = types.SimpleNamespace(stdin=_DeadPipe(), stdout=io.BytesIO(b""), poll=lambda: 1)
    assert sup._replay_handshake(child) is True


def test_replay_handshake_rejects_malformed_initialize_response(monkeypatch):
    sup = supervisor.Supervisor()
    assert sup._protocol.accept(_initialize_payload(0)).kind is supervisor._ClientActionKind.FORWARD
    killed: list[int] = []
    malformed = _payload(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "result": {},
            "error": {"code": -32603, "message": "must-not-pass"},
        }
    )
    output = io.BytesIO()
    monkeypatch.setattr(supervisor.sys, "stdout", types.SimpleNamespace(buffer=output))
    child = types.SimpleNamespace(
        stdin=_FakePipe(),
        stdout=io.BytesIO(malformed + b"\n"),
        kill=lambda: killed.append(1),
        poll=lambda: None,
    )

    assert sup._replay_handshake(child) is False
    assert killed == [1]
    assert output.getvalue() == b""


def test_replay_handshake_initialize_error_never_advances_to_initialized() -> None:
    sup = supervisor.Supervisor()
    assert sup._protocol.accept(_initialize_payload(0)).kind is supervisor._ClientActionKind.FORWARD
    prepared = sup._protocol.prepare_response(_initialize_response_payload(0))
    sup._protocol.acknowledge_response(prepared)
    assert sup._protocol.accept(_initialized_payload()).kind is supervisor._ClientActionKind.FORWARD
    killed: list[int] = []
    child_stdin = _FakePipe()
    child = types.SimpleNamespace(
        stdin=child_stdin,
        stdout=io.BytesIO(
            _payload(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "error": {"code": -32603, "message": "initialize failed"},
                }
            )
            + b"\n"
        ),
        kill=lambda: killed.append(1),
        poll=lambda: None,
    )

    assert sup._replay_handshake(child) is False
    assert killed == [1]
    assert _initialized_payload() + b"\n" not in child_stdin.getvalue()


def test_replay_handshake_live_child_stdout_eof_is_failure() -> None:
    sup = supervisor.Supervisor()
    assert sup._protocol.accept(_initialize_payload(0)).kind is supervisor._ClientActionKind.FORWARD
    killed: list[int] = []
    child = types.SimpleNamespace(
        stdin=_FakePipe(),
        stdout=io.BytesIO(b""),
        kill=lambda: killed.append(1),
        poll=lambda: None,
    )

    assert sup._replay_handshake(child) is False
    assert killed == [1]


def test_first_generation_initialize_has_one_absolute_deadline(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(supervisor, "_HANDSHAKE_TIMEOUT_SECONDS", 0.05)
    read_fd, write_fd = os.pipe()
    stdin_read, stdin_write = os.pipe()
    stdin_file = os.fdopen(stdin_read, "rb", buffering=0)
    monkeypatch.setattr(supervisor.sys, "stdin", types.SimpleNamespace(buffer=stdin_file))
    initialized_written = threading.Event()
    dead = threading.Event()

    class _ObservedInput(_FakePipe):
        def write(self, data: bytes) -> int:
            initialized_written.set()
            return super().write(data)

    class _HungInitialChild:
        def __init__(self) -> None:
            self.stdin = _ObservedInput()
            self.stdout = os.fdopen(read_fd, "rb", buffering=0)

        def kill(self) -> None:
            if not dead.is_set():
                dead.set()
                with contextlib.suppress(OSError):
                    os.close(write_fd)

        def wait(self) -> int:
            assert dead.wait(timeout=2), "initial handshake was not terminated"
            return -9

        def poll(self) -> int | None:
            return -9 if dead.is_set() else None

    child = _HungInitialChild()
    sup = supervisor.Supervisor()
    monkeypatch.setattr(sup, "_spawn", lambda: child)
    os.write(stdin_write, _initialize_payload(0) + b"\n")
    result: list[int] = []
    runner = threading.Thread(target=lambda: result.append(sup.run()), daemon=True)
    runner.start()
    try:
        assert initialized_written.wait(timeout=1)
        runner.join(timeout=0.5)
        assert not runner.is_alive(), "first-generation initialize exceeded its deadline"
        assert result == [1]
        assert dead.is_set()
        assert "握手超时" in capsys.readouterr().err
    finally:
        child.kill()
        with contextlib.suppress(OSError):
            os.close(stdin_write)
        runner.join(timeout=2)
        stdin_file.close()
        child.stdout.close()


def test_exited_child_with_inherited_stdout_writer_cannot_block_join(
    unclosed_stdin,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(supervisor, "_CHILD_STDOUT_DRAIN_SECONDS", 0.05, raising=False)
    read_fd, inherited_write_fd = os.pipe()
    child = types.SimpleNamespace(
        stdin=_FakePipe(),
        stdout=os.fdopen(read_fd, "rb", buffering=0),
        wait=lambda: 0,
        poll=lambda: 0,
        kill=lambda: None,
    )
    sup = supervisor.Supervisor()
    monkeypatch.setattr(sup, "_spawn", lambda: child)
    result: list[int] = []
    runner = threading.Thread(target=lambda: result.append(sup.run()), daemon=True)
    runner.start()
    try:
        runner.join(timeout=0.5)
        assert not runner.is_alive(), "descendant-held stdout kept pump.join unbounded"
        assert result == [1]
        assert "stdout" in capsys.readouterr().err
    finally:
        with contextlib.suppress(OSError):
            os.close(inherited_write_fd)
        runner.join(timeout=2)
        child.stdout.close()


def test_replay_handshake_deadline_includes_blocked_child_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(supervisor, "_HANDSHAKE_TIMEOUT_SECONDS", 0.05)
    sup = supervisor.Supervisor()
    assert sup._protocol.accept(_initialize_payload(0)).kind is supervisor._ClientActionKind.FORWARD
    killed: list[int] = []
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b"x" * 65_536)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    blocked_stdin = os.fdopen(write_fd, "wb", buffering=0)
    child = types.SimpleNamespace(
        stdin=blocked_stdin,
        stdout=io.BytesIO(b""),
        kill=lambda: killed.append(1),
        poll=lambda: None,
    )

    try:
        started = time.monotonic()
        assert sup._replay_handshake(child) is False
        elapsed = time.monotonic() - started

        assert killed == [1]
        assert elapsed < 0.5
        assert "child stdin write timed out" in capsys.readouterr().err
    finally:
        blocked_stdin.close()
        os.close(read_fd)


def test_normal_ingress_full_production_pipe_has_deadline_and_releases_generation_lock(
    monkeypatch,
    capsys,
) -> None:
    """A full production child pipe cannot block swap/generation ownership forever."""

    monkeypatch.setattr(
        supervisor,
        "_INGRESS_WRITE_TIMEOUT_SECONDS",
        0.2,
        raising=False,
    )
    sup = supervisor.Supervisor()
    sup._protocol, _, _ = _ready_protocol()
    secret = "normal-ingress-private-secret"
    request = _payload(
        {
            "jsonrpc": "2.0",
            "id": 73,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {"note": secret}},
        }
    )
    stdin_file = _fake_stdin(monkeypatch, request + b"\n")

    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b"x" * 65_536)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    blocked_stdin = os.fdopen(write_fd, "wb", buffering=0)
    killed = threading.Event()
    child = types.SimpleNamespace(
        stdin=blocked_stdin,
        stdout=io.BytesIO(b""),
        kill=killed.set,
        poll=lambda: -9 if killed.is_set() else None,
    )
    sup._child = child
    monkeypatch.setattr(sup, "_reap_after_grace", lambda _child: None)

    client = threading.Thread(
        target=sup._pump_client_lines,
        name=secret,
    )
    client.start()
    deadline = time.monotonic() + 1.0
    while sup._protocol.pending_count == 0 and time.monotonic() < deadline:
        time.sleep(0.001)

    lock_acquired = sup._child_lock.acquire(timeout=0.05)
    if lock_acquired:
        sup._child_lock.release()
    try:
        client.join(timeout=0.5)
        assert lock_acquired, "normal ingress held generation lock across pipe write"
        assert not client.is_alive(), "normal ingress exceeded its absolute write deadline"
        assert killed.is_set()
        assert sup._protocol_failed.is_set()
        rendered = capsys.readouterr().err
        assert "child stdin write timed out" in rendered
        assert secret not in rendered
    finally:
        if client.is_alive():
            os.read(read_fd, 1_048_576)
            client.join(timeout=2)
        if not blocked_stdin.closed:
            blocked_stdin.close()
        os.close(read_fd)
        stdin_file.close()


def test_normal_ingress_stale_generation_failure_does_not_kill_replacement(
    monkeypatch,
) -> None:
    """A retired generation's write result cannot poison its replacement."""

    sup = supervisor.Supervisor()
    sup._protocol, _, _ = _ready_protocol()
    request = _payload({"jsonrpc": "2.0", "id": 74, "method": "ping", "params": {}})
    stdin_file = _fake_stdin(monkeypatch, request + b"\n")
    old_kills: list[int] = []
    new_kills: list[int] = []
    old = types.SimpleNamespace(
        stdin=_FakePipe(),
        stdout=io.BytesIO(b""),
        kill=lambda: old_kills.append(1),
        poll=lambda: None,
    )
    new = types.SimpleNamespace(
        stdin=_FakePipe(),
        stdout=io.BytesIO(b""),
        kill=lambda: new_kills.append(1),
        poll=lambda: None,
    )
    sup._child = old
    sup._child_generation = 4
    entered = threading.Event()
    release = threading.Event()

    def _retired_write(child, payload, *, deadline):
        assert child is old
        assert payload == request
        assert deadline > time.monotonic()
        entered.set()
        assert release.wait(timeout=2)
        return supervisor._ChildWriteResult.FAILED

    monkeypatch.setattr(sup, "_write_child_payload_until", _retired_write)
    monkeypatch.setattr(sup, "_reap_after_grace", lambda _child: None)
    client = threading.Thread(target=sup._pump_client_lines)
    client.start()
    assert entered.wait(timeout=2)
    with sup._child_lock:
        sup._child = new
        sup._child_generation = 5
    release.set()
    client.join(timeout=2)

    assert not client.is_alive()
    assert old_kills == []
    assert new_kills == []
    assert not sup._protocol_failed.is_set()
    assert sup._protocol.pending_count == 1
    stdin_file.close()


def test_child_pump_crash_kills_child_and_exits_nonzero(unclosed_stdin, monkeypatch, capsys):
    """C3①：child→client 泵崩溃 → 不再是「子进程 stdout 无人读、pipe 写满全宿主
    冻结」——kill 子进程、run() 以非零码退出、stderr 只有固定故障文本。"""
    sup = supervisor.Supervisor()
    secret = "child-pump-private-secret"

    def _boom(ch):
        raise RuntimeError(secret)

    monkeypatch.setattr(sup, "_pump_child_lines", _boom)
    monkeypatch.setattr(sup, "_spawn", lambda: _KillableChild())

    assert sup.run() == 1
    err = capsys.readouterr().err
    assert "child→client pump_failed" in err
    assert secret not in err
    assert "RuntimeError" not in err


def test_client_pump_crash_kills_child_and_exits_nonzero(unclosed_stdin, monkeypatch, capsys):
    """C3①：client→child 泵（daemon）崩溃 → EOF 收尾链路失效也不留孤儿对——
    兜底 kill 子进程 + run() 非零码退出。"""
    sup = supervisor.Supervisor()
    secret = "client-pump-private-secret"

    def _boom():
        raise RuntimeError(secret)

    monkeypatch.setattr(sup, "_pump_client_lines", _boom)
    monkeypatch.setattr(sup, "_spawn", lambda: _KillableChild())

    assert sup.run() == 1
    err = capsys.readouterr().err
    assert "client→child pump_failed" in err
    assert secret not in err
    assert "RuntimeError" not in err


def test_live_child_stdout_eof_is_fixed_transport_failure(
    unclosed_stdin,
    monkeypatch,
    capsys,
) -> None:
    """A child that closes stdout but stays alive is killed instead of hanging run()."""

    monkeypatch.setattr(
        supervisor,
        "_CHILD_STDOUT_EOF_GRACE_SECONDS",
        0.05,
        raising=False,
    )
    code = "import os, time\nos.close(1)\ntime.sleep(30)\n"
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    sup = supervisor.Supervisor()
    monkeypatch.setattr(sup, "_spawn", lambda: child)
    result: dict[str, int] = {}
    runner = threading.Thread(target=lambda: result.setdefault("code", sup.run()))
    runner.start()
    runner.join(timeout=1)
    try:
        assert not runner.is_alive(), "live stdout EOF left run() blocked in child.wait()"
        assert result == {"code": 1}
        assert child.poll() is not None
        rendered = capsys.readouterr().err
        assert "child→client pump_failed" in rendered
        assert "Traceback" not in rendered
    finally:
        if child.poll() is None:
            child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=2)
        runner.join(timeout=2)


def test_stdout_eof_during_normal_exit_is_not_transport_failure(
    unclosed_stdin,
    monkeypatch,
    capsys,
) -> None:
    """A short close-to-exit race stays a normal child exit, not a false failure."""

    monkeypatch.setattr(
        supervisor,
        "_CHILD_STDOUT_EOF_GRACE_SECONDS",
        0.2,
        raising=False,
    )
    code = "import os, time\nos.close(1)\ntime.sleep(0.02)\n"
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    sup = supervisor.Supervisor()
    monkeypatch.setattr(sup, "_spawn", lambda: child)

    assert sup.run() == 0
    assert not sup._pump_failed.is_set()
    assert "pump_failed" not in capsys.readouterr().err


def test_swap_replay_write_has_bounded_deadline(
    unclosed_stdin,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(supervisor, "_HANDSHAKE_TIMEOUT_SECONDS", 0.05)
    sup = supervisor.Supervisor(idempotent_tools=frozenset({"safe_tool"}))
    state, _, _ = _ready_protocol(idempotent_tools=frozenset({"safe_tool"}))
    sup._protocol = state
    replay = _payload(
        {
            "jsonrpc": "2.0",
            "id": 88,
            "method": "tools/call",
            "params": {"name": "safe_tool", "arguments": {}},
        }
    )
    assert sup._protocol.accept(replay).kind is supervisor._ClientActionKind.FORWARD

    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b"x" * 65_536)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    blocked_stdin = os.fdopen(write_fd, "wb", buffering=0)
    killed = threading.Event()

    class _BlockedGeneration:
        stdin = blocked_stdin
        stdout = io.BytesIO(b"")

        @staticmethod
        def kill() -> None:
            killed.set()

        @staticmethod
        def poll():
            return -9 if killed.is_set() else None

        @staticmethod
        def wait(timeout=None):
            if timeout is not None:
                assert killed.wait(timeout=timeout)
                return -9
            assert killed.wait(timeout=5)
            return -9

    new = _BlockedGeneration()
    children = [_SwapChild(), new]
    monkeypatch.setattr(sup, "_spawn", lambda: children.pop(0))
    monkeypatch.setattr(sup, "_replay_handshake", lambda _child: True)

    try:
        started = time.monotonic()
        assert sup.run() == 1
        assert time.monotonic() - started < 0.5
        assert killed.is_set()
        assert "child stdin write timed out" in capsys.readouterr().err
    finally:
        blocked_stdin.close()
        os.close(read_fd)


def test_client_to_child_rejects_invalid_frame_without_forward_or_secret(monkeypatch):
    """严格入口用固定 parse error 关闭；原始帧既不透传也不进日志/响应。"""
    secret = b"invalid-private-frame-secret"
    stdin_file = _fake_stdin(monkeypatch, secret + b"\n")
    out = io.BytesIO()
    monkeypatch.setattr(supervisor.sys, "stdout", types.SimpleNamespace(buffer=out))
    sup = supervisor.Supervisor()
    child = types.SimpleNamespace(stdin=_FakePipe(), stdout=io.BytesIO(b""), poll=lambda: 0)
    sup._child = child
    monkeypatch.setattr(sup, "_reap_after_grace", lambda _child: None)
    sup._client_to_child()

    assert child.stdin.closed_value == b""
    assert json.loads(out.getvalue()) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
    assert secret not in out.getvalue()
    assert sup._client_eof.is_set()
    assert sup._protocol_failed.is_set()
    assert sup._protocol.pending_count == 0
    stdin_file.close()


def test_child_to_client_rejects_invalid_response_id_without_forward_or_secret(
    monkeypatch,
    capsys,
):
    """非法 child response id 触发固定 pump failure，不透传原始响应。"""
    out = io.BytesIO()
    monkeypatch.setattr(supervisor.sys, "stdout", types.SimpleNamespace(buffer=out))
    secret = "child-response-private-secret"
    line = _payload({"jsonrpc": "2.0", "id": [1, 2], "result": {"secret": secret}}) + b"\n"
    sup = supervisor.Supervisor()
    child = types.SimpleNamespace(stdout=io.BytesIO(line))

    sup._child_to_client(child)

    assert out.getvalue() == b""
    assert sup._pump_failed.is_set()
    err = capsys.readouterr().err
    assert "child→client pump_failed" in err
    assert secret not in err
    assert "ResponseProtocolError" not in err


def test_eof_during_swap_reaps_new_child(unclosed_stdin, monkeypatch):
    """I2：EOF 恰在换芯窗口内到达 → 对新子进程不仅补关 stdin，还挂宽限回收——
    不守规矩的新子进程也绝不成孤儿。"""
    sup = supervisor.Supervisor()

    class _New:
        def __init__(self):
            self.stdin = _FakePipe()
            self.stdout = io.BytesIO(b"")

        def wait(self):
            return 0

        def poll(self):
            return 0

    new = _New()
    children = [_SwapChild(), new]
    monkeypatch.setattr(sup, "_spawn", lambda: children.pop(0))
    # 时序注入：EOF 恰在 spawn new 之后、锁内检查之前到达（replay 窗口内）
    monkeypatch.setattr(sup, "_replay_handshake", lambda ch: sup._client_eof.set() or True)
    reaped: list = []
    monkeypatch.setattr(sup, "_reap_after_grace", lambda ch: reaped.append(ch))

    assert sup.run() == 0
    assert reaped == [new]  # 新子进程也挂了宽限回收
    assert new.stdin.closed  # stdin 已补关令其自然收尾


@pytest.mark.skipif(sys.platform == "win32", reason="Windows terminate 已是强杀，无 kill 升级路径")
def test_reap_after_grace_escalates_to_kill(monkeypatch):
    """I3：宽限后 terminate 仍不死（子进程忽略 SIGTERM）→ 再等升级 kill()——
    调小宽限常量走真实兜底路径（此前该分支零覆盖）。"""
    monkeypatch.setattr(supervisor, "_EOF_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(supervisor, "_TERM_KILL_GRACE_SECONDS", 0.2)
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "while True:\n"
        "    time.sleep(1)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    try:
        assert proc.stdout.readline().strip() == b"ready"  # 同步点：SIGTERM 已被忽略
        supervisor.Supervisor()._reap_after_grace(proc)
        assert proc.wait(timeout=10) == -9  # SIGKILL：升级路径生效
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


# --- S3-7：有界 supervisor 协议状态机 ---


def _payload(message: dict[str, object]) -> bytes:
    return json.dumps(
        message,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _initialize_payload(request_id: int | str = "initialize") -> bytes:
    return _payload(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "supervisor-test", "version": "1"},
            },
        }
    )


def _initialize_response_payload(request_id: int | str = "initialize") -> bytes:
    return _payload(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "supervisor-test", "version": "1"},
            },
        }
    )


def _initialized_payload() -> bytes:
    return _payload(
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
    )


def _ready_protocol(
    *,
    idempotent_tools: frozenset[str] = frozenset({"safe_tool"}),
):
    state = supervisor._SupervisorProtocolState(
        idempotent_tools=idempotent_tools,
    )
    initialize = _initialize_payload()
    initialized = _initialized_payload()
    assert state.accept(initialize).kind is supervisor._ClientActionKind.FORWARD
    prepared = state.prepare_response(_initialize_response_payload())
    state.acknowledge_response(prepared)
    assert state.accept(initialized).kind is supervisor._ClientActionKind.FORWARD
    return state, initialize, initialized


def test_protocol_accepts_exact_handshake_once_outside_pending_budget() -> None:
    out_of_order = supervisor._SupervisorProtocolState(
        idempotent_tools=frozenset(),
    )
    rejected = out_of_order.accept(_initialized_payload())
    assert rejected.kind is supervisor._ClientActionKind.REJECT
    assert rejected.close is True
    assert out_of_order.handshake_frame_count == 0

    state, initialize, initialized = _ready_protocol()
    assert state.handshake_complete is True
    assert state.handshake_frame_count == 2
    assert state.handshake_retained_bytes == len(initialize) + len(initialized)
    assert state.handshake_retained_bytes <= 2 * mcp_transport.MAX_REQUEST_FRAME_BYTES
    assert state.pending_count == 0

    duplicate = state.accept(_initialize_payload("duplicate"))
    assert duplicate.kind is supervisor._ClientActionKind.REJECT
    assert duplicate.response == {
        "jsonrpc": "2.0",
        "id": "duplicate",
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    assert state.handshake_frame_count == 2

    third = state.accept(_initialized_payload())
    assert third.kind is supervisor._ClientActionKind.REJECT
    assert third.close is True
    assert state.handshake_frame_count == 2


def test_protocol_requires_flushed_initialize_response_before_initialized_or_work() -> None:
    state = supervisor._SupervisorProtocolState(idempotent_tools=frozenset())
    assert state.accept(_initialize_payload(70)).kind is supervisor._ClientActionKind.FORWARD

    premature_initialized = state.accept(_initialized_payload())
    assert premature_initialized.kind is supervisor._ClientActionKind.REJECT
    assert premature_initialized.close is True
    assert state.handshake_complete is False
    assert state.handshake_frame_count == 1

    premature_work = state.accept(
        _payload({"jsonrpc": "2.0", "id": 71, "method": "tools/list", "params": {}})
    )
    assert premature_work.kind is supervisor._ClientActionKind.REJECT
    assert premature_work.close is True
    assert state.pending_count == 0

    prepared = state.prepare_response(_initialize_response_payload(70))
    assert state.accept(_initialized_payload()).kind is supervisor._ClientActionKind.REJECT
    state.acknowledge_response(prepared)
    assert state.accept(_initialized_payload()).kind is supervisor._ClientActionKind.FORWARD
    assert state.handshake_complete is True


def test_initialize_flush_and_ack_are_atomic_against_initialized_accept(monkeypatch) -> None:
    sup = supervisor.Supervisor()
    assert (
        sup._protocol.accept(_initialize_payload(72)).kind is supervisor._ClientActionKind.FORWARD
    )
    flushed = threading.Event()
    release_flush = threading.Event()

    def _blocked_completed_flush(_payload: bytes) -> bool:
        flushed.set()
        assert release_flush.wait(timeout=5)
        return True

    monkeypatch.setattr(sup, "_write_client_payload", _blocked_completed_flush)
    child = types.SimpleNamespace(
        stdout=io.BytesIO(
            _payload(
                {
                    "jsonrpc": "2.0",
                    "id": 72,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "serverInfo": {"name": "fake", "version": "1"},
                    },
                }
            )
            + b"\n"
        )
    )
    pump = threading.Thread(target=sup._pump_child_lines, args=(child,))
    pump.start()
    assert flushed.wait(timeout=5)

    accepted: list[supervisor._ClientAction] = []

    def _accept_initialized() -> None:
        with sup._state_lock:
            accepted.append(sup._protocol.accept(_initialized_payload()))

    client = threading.Thread(target=_accept_initialized)
    client.start()
    client.join(timeout=0.1)
    assert client.is_alive(), "initialized overtook initialize flush acknowledgement"

    release_flush.set()
    pump.join(timeout=5)
    client.join(timeout=5)
    assert not pump.is_alive()
    assert not client.is_alive()
    assert accepted[0].kind is supervisor._ClientActionKind.FORWARD
    assert sup._protocol.handshake_complete is True


def test_supervisor_stdin_framing_accepts_exact_n_and_rejects_n_plus_one(
    monkeypatch,
) -> None:
    class _RawStdin:
        @staticmethod
        def fileno() -> int:
            return 123

    monkeypatch.setattr(
        supervisor.sys,
        "stdin",
        types.SimpleNamespace(buffer=_RawStdin()),
    )

    def _read_events(raw: bytes):
        offset = 0
        requested: list[int] = []

        def _read(fd: int, size: int) -> bytes:
            nonlocal offset
            assert fd == 123
            requested.append(size)
            chunk = raw[offset : offset + size]
            offset += len(chunk)
            return chunk

        monkeypatch.setattr(supervisor.os, "read", _read)
        return list(supervisor._stdin_frames()), requested

    initialize = _initialize_payload()
    exact = initialize + b" " * (mcp_transport.MAX_REQUEST_FRAME_BYTES - len(initialize))
    exact_events, exact_reads = _read_events(exact + b"\n")
    assert exact_events == [mcp_transport.RequestFrame(exact)]
    assert all(size == mcp_transport.READ_CHUNK_BYTES for size in exact_reads)

    state = supervisor._SupervisorProtocolState(idempotent_tools=frozenset())
    assert state.accept(exact).kind is supervisor._ClientActionKind.FORWARD
    prepared = state.prepare_response(_initialize_response_payload())
    state.acknowledge_response(prepared)
    initialized = _initialized_payload()
    initialized_exact = initialized + b" " * (
        mcp_transport.MAX_REQUEST_FRAME_BYTES - len(initialized)
    )
    assert state.accept(initialized_exact).kind is supervisor._ClientActionKind.FORWARD
    assert state.handshake_retained_bytes == 4_194_304

    secret = b"oversize-request-private-secret"
    over = secret + b"x" * (mcp_transport.MAX_REQUEST_FRAME_BYTES + 1 - len(secret))
    over_events, over_reads = _read_events(over + b"\n")
    assert over_events == [
        mcp_transport.FrameFailure(
            {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
        )
    ]
    assert secret not in repr(over_events).encode()
    assert all(size == mcp_transport.READ_CHUNK_BYTES for size in over_reads)


def test_protocol_bounds_pending_ids_and_keeps_one_control_lane() -> None:
    state, _, _ = _ready_protocol()
    admitted: list[bytes] = []
    for request_id in range(mcp_transport.MAX_IN_FLIGHT):
        payload = _payload(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "ping",
                "params": {},
            }
        )
        admitted.append(payload)
        assert state.accept(payload).kind is supervisor._ClientActionKind.FORWARD
    assert state.pending_count == mcp_transport.MAX_IN_FLIGHT

    duplicate = state.accept(admitted[0])
    assert duplicate.kind is supervisor._ClientActionKind.RESPOND
    assert duplicate.response == {
        "jsonrpc": "2.0",
        "id": 0,
        "error": {"code": -32600, "message": "Invalid Request"},
    }

    ninth = state.accept(
        _payload(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "ping",
                "params": {},
            }
        )
    )
    assert ninth.kind is supervisor._ClientActionKind.RESPOND
    assert ninth.response == {
        "jsonrpc": "2.0",
        "id": 8,
        "error": {"code": -32005, "message": "Server is busy."},
    }

    cancellation = _payload(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 0, "reason": "do not retain this reason"},
        }
    )
    retained_before_cancel = state.pending_retained_bytes
    assert state.accept(cancellation).kind is supervisor._ClientActionKind.FORWARD
    assert state.pending_count == mcp_transport.MAX_IN_FLIGHT
    assert state.pending_retained_bytes == retained_before_cancel
    assert state.is_cancel_requested(0) is True
    assert b"do not retain this reason" not in repr(state._pending).encode()

    duplicate_handshake = state.accept(_initialized_payload())
    assert duplicate_handshake.kind is supervisor._ClientActionKind.REJECT
    assert duplicate_handshake.close is True
    assert state.pending_count == mcp_transport.MAX_IN_FLIGHT


def test_swap_plan_replays_only_frozen_safe_set_and_fails_unknown_outcome() -> None:
    state, _, _ = _ready_protocol(idempotent_tools=frozenset({"safe_tool"}))
    artifact_uri = "vibecad://artifact/materialization_" + "a" * 64 + "/artifact_" + "b" * 32
    requests = (
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/templates/list",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": artifact_uri},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "safe_tool", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "unsafe_tool", "arguments": {}},
        },
    )
    payloads = tuple(_payload(item) for item in requests)
    for payload in payloads:
        assert state.accept(payload).kind is supervisor._ClientActionKind.FORWARD
    cancellation = _payload(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 6},
        }
    )
    assert state.accept(cancellation).kind is supervisor._ClientActionKind.FORWARD

    plan = state.plan_swap()
    assert plan.replay_payloads == payloads[:-1]
    assert tuple(json.loads(item) for item in plan.cancellation_payloads) == (
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 6},
        },
    )
    assert b"reason" not in b"".join(plan.cancellation_payloads)
    assert len(plan.unknown_outcomes) == 1
    unknown = plan.unknown_outcomes[0]
    assert unknown.request_id == 7
    assert unknown.payload == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {
            "code": -32003,
            "message": "Tool outcome is unknown; inspect durable state before retry.",
        },
    }
    assert payloads[-1] not in plan.replay_payloads
    assert state.pending_count == len(payloads)
    state.acknowledge(unknown.request_id)
    assert state.pending_count == len(payloads) - 1


def test_cancel_tombstone_survives_until_final_response_write_ack() -> None:
    state, _, _ = _ready_protocol()
    request = _payload(
        {
            "jsonrpc": "2.0",
            "id": "same",
            "method": "tools/call",
            "params": {"name": "safe_tool", "arguments": {}},
        }
    )
    cancellation = _payload(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": "same"},
        }
    )
    assert state.accept(request).kind is supervisor._ClientActionKind.FORWARD
    assert state.accept(cancellation).kind is supervisor._ClientActionKind.FORWARD
    assert state.accept(request).response == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32600, "message": "Invalid Request"},
    }

    child_response = _payload(
        {"jsonrpc": "2.0", "id": "same", "result": {"private": "must-not-pass"}}
    )
    prepared = state.prepare_response(child_response)
    assert json.loads(prepared.payload) == {
        "jsonrpc": "2.0",
        "id": "same",
        "error": {"code": -32800, "message": "Request cancelled"},
    }
    assert state.pending_count == 1
    state.acknowledge_response(prepared)
    assert state.pending_count == 0
    assert state.accept(request).kind is supervisor._ClientActionKind.FORWARD


def test_cancel_after_response_prepare_is_dropped_until_ack_barrier() -> None:
    state, _, _ = _ready_protocol()
    request = _payload(
        {
            "jsonrpc": "2.0",
            "id": 91,
            "method": "tools/call",
            "params": {"name": "safe_tool", "arguments": {}},
        }
    )
    cancellation = _payload(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 91, "reason": "late-private-reason"},
        }
    )
    assert state.accept(request).kind is supervisor._ClientActionKind.FORWARD
    prepared = state.prepare_response(
        _payload({"jsonrpc": "2.0", "id": 91, "result": {"ok": True}})
    )

    late = state.accept(cancellation)
    assert late.kind is supervisor._ClientActionKind.DROP
    assert prepared.forward is True
    assert json.loads(prepared.payload)["result"] == {"ok": True}
    assert state.pending_count == 1
    assert b"late-private-reason" not in repr(state._pending).encode()

    state.acknowledge_response(prepared)
    assert state.pending_count == 0


def test_response_shape_id_membership_and_terminal_ack_are_strict() -> None:
    state = supervisor._SupervisorProtocolState(idempotent_tools=frozenset({"safe_tool"}))
    assert state.accept(_initialize_payload()).kind is supervisor._ClientActionKind.FORWARD
    same_as_initialize = _payload(
        {"jsonrpc": "2.0", "id": "initialize", "method": "ping", "params": {}}
    )
    duplicate_active_initialize = state.accept(same_as_initialize)
    assert duplicate_active_initialize.kind is supervisor._ClientActionKind.REJECT
    assert duplicate_active_initialize.close is True
    assert duplicate_active_initialize.response == {
        "jsonrpc": "2.0",
        "id": "initialize",
        "error": {"code": -32600, "message": "Invalid Request"},
    }

    initialize_response = _initialize_response_payload()
    prepared_initialize = state.prepare_response(initialize_response)
    assert prepared_initialize.initialize_response is True
    state.acknowledge_response(prepared_initialize)
    assert state.accept(_initialized_payload()).kind is supervisor._ClientActionKind.FORWARD
    assert state.accept(same_as_initialize).kind is supervisor._ClientActionKind.FORWARD

    terminal = _payload({"jsonrpc": "2.0", "id": "initialize", "result": {"ok": True}})
    prepared_terminal = state.prepare_response(terminal)
    state.acknowledge_response(prepared_terminal)

    for invalid in (
        terminal,
        _payload({"jsonrpc": "2.0", "id": "unknown", "result": {}}),
        _payload(
            {
                "jsonrpc": "2.0",
                "id": "unknown",
                "result": {},
                "error": {"code": -32603, "message": "invalid"},
            }
        ),
        _payload(
            {
                "jsonrpc": "2.0",
                "id": "unknown",
                "error": {"code": "not-an-integer", "message": "invalid"},
            }
        ),
    ):
        with pytest.raises(supervisor._ResponseProtocolError):
            state.prepare_response(invalid)

    notification = _payload(
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progress": 1},
        }
    )
    assert state.prepare_response(notification) == supervisor._PreparedResponse(
        notification,
        None,
    )


@pytest.mark.parametrize(
    "invalid_response",
    (
        {"jsonrpc": "2.0", "id": "initialize", "error": {"code": -32603, "message": "failed"}},
        {"jsonrpc": "2.0", "id": "initialize", "result": {}},
    ),
)
def test_first_initialize_requires_exact_success_before_initialized(
    invalid_response: dict[str, object],
) -> None:
    state = supervisor._SupervisorProtocolState(idempotent_tools=frozenset())
    assert state.accept(_initialize_payload()).kind is supervisor._ClientActionKind.FORWARD

    with pytest.raises(supervisor._ResponseProtocolError):
        state.prepare_response(_payload(invalid_response))

    rejected = state.accept(_initialized_payload())
    assert rejected.kind is supervisor._ClientActionKind.REJECT
    assert rejected.close is True
    assert state.handshake_complete is False


def test_default_replay_tool_set_matches_public_idempotence_contract() -> None:
    from vibecad.application.public_surface import public_tool_specs

    expected = frozenset(spec.name for spec in public_tool_specs() if spec.annotations.idempotent)
    assert supervisor._DEFAULT_IDEMPOTENT_TOOLS == expected
    assert "create_task" in supervisor._DEFAULT_IDEMPOTENT_TOOLS
    assert "revert_project" in supervisor._DEFAULT_IDEMPOTENT_TOOLS
    assert "cancel_task" in supervisor._DEFAULT_IDEMPOTENT_TOOLS
    assert {
        "list_projects",
        "list_revisions",
        "compare_revisions",
        "list_tasks",
        "get_task_events",
        "get_artifact_manifest",
    } <= supervisor._DEFAULT_IDEMPOTENT_TOOLS


def test_response_reader_caps_before_decode_and_reads_in_bounded_chunks() -> None:
    class _Chunked:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw
            self.offset = 0
            self.requested: list[int] = []

        def read1(self, size: int) -> bytes:
            self.requested.append(size)
            chunk = self.raw[self.offset : self.offset + min(size, 3)]
            self.offset += len(chunk)
            return chunk

    exact_stream = _Chunked(b"12345678\nnext\n")
    exact = supervisor._BoundedResponseReader(limit=8, chunk_bytes=4)
    assert exact.read_frame(exact_stream) == b"12345678"
    assert exact.read_frame(exact_stream) == b"next"
    assert all(size <= 4 for size in exact_stream.requested)

    over_stream = _Chunked(b"123456789\n")
    over = supervisor._BoundedResponseReader(limit=8, chunk_bytes=4)
    with pytest.raises(supervisor._ResponseProtocolError):
        over.read_frame(over_stream)


def test_pump_panic_never_logs_exception_or_frame_text(capsys) -> None:
    secret = "supervisor-private-frame-secret"
    sup = supervisor.Supervisor()
    try:
        raise RuntimeError(secret)
    except RuntimeError:
        sup._pump_panic("child→client")

    rendered = capsys.readouterr().err
    assert "child→client" in rendered
    assert secret not in rendered
    assert "RuntimeError" not in rendered


def test_child_response_ack_does_not_wait_for_blocked_child_stdin(
    monkeypatch,
) -> None:
    sup = supervisor.Supervisor(idempotent_tools=frozenset({"safe_tool"}))
    state, _, _ = _ready_protocol(idempotent_tools=frozenset({"safe_tool"}))
    sup._protocol = state
    request = _payload(
        {
            "jsonrpc": "2.0",
            "id": 31,
            "method": "tools/call",
            "params": {"name": "safe_tool", "arguments": {}},
        }
    )
    assert sup._protocol.accept(request).kind is supervisor._ClientActionKind.FORWARD

    entered = threading.Event()
    release = threading.Event()

    class _BlockingInput:
        closed = False

        def write(self, data: bytes) -> int:
            entered.set()
            assert release.wait(timeout=5)
            return len(data)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    cancellation = _payload(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": 31},
        }
    )
    stdin_file = _fake_stdin(monkeypatch, cancellation + b"\n")
    output = io.BytesIO()
    monkeypatch.setattr(supervisor.sys, "stdout", types.SimpleNamespace(buffer=output))
    child = types.SimpleNamespace(
        stdin=_BlockingInput(),
        stdout=io.BytesIO(_payload({"jsonrpc": "2.0", "id": 31, "result": {"ok": True}}) + b"\n"),
        poll=lambda: None,
    )
    sup._child = child
    monkeypatch.setattr(sup, "_reap_after_grace", lambda _child: None)

    client = threading.Thread(target=sup._pump_client_lines)
    client.start()
    assert entered.wait(timeout=5)
    response = threading.Thread(target=sup._pump_child_lines, args=(child,))
    response.start()
    response.join(timeout=2)
    try:
        assert not response.is_alive(), "response pump waited on blocked child stdin"
        assert sup._protocol.pending_count == 0
        assert json.loads(output.getvalue()) == {
            "jsonrpc": "2.0",
            "id": 31,
            "error": {"code": -32800, "message": "Request cancelled"},
        }
    finally:
        release.set()
        client.join(timeout=5)
        stdin_file.close()


def test_swap_starts_new_response_pump_before_replay_writes(
    unclosed_stdin,
    monkeypatch,
) -> None:
    sup = supervisor.Supervisor()
    initialize = _initialize_payload(0)
    initialized = _initialized_payload()
    request = _payload({"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}})
    assert sup._protocol.accept(initialize).kind is supervisor._ClientActionKind.FORWARD
    prepared = sup._protocol.prepare_response(_initialize_response_payload(0))
    sup._protocol.acknowledge_response(prepared)
    assert sup._protocol.accept(initialized).kind is supervisor._ClientActionKind.FORWARD
    assert sup._protocol.accept(request).kind is supervisor._ClientActionKind.FORWARD
    sup._init_id = 0

    pump_started = threading.Event()

    class _ReplayInput(_FakePipe):
        def write(self, data: bytes) -> int:
            if data == request:
                assert pump_started.wait(timeout=2), "replay write preceded response pump"
            return super().write(data)

    class _NewChild:
        def __init__(self) -> None:
            self.stdin = _ReplayInput()
            self.stdout = io.BytesIO(
                _payload(
                    {
                        "jsonrpc": "2.0",
                        "id": 0,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "serverInfo": {"name": "fake", "version": "2"},
                        },
                    }
                )
                + b"\n"
            )

        @staticmethod
        def wait() -> int:
            return 0

        @staticmethod
        def poll() -> int:
            return 0

    first = _SwapChild()
    second = _NewChild()
    children = [first, second]
    monkeypatch.setattr(sup, "_spawn", lambda: children.pop(0))
    original_child_to_client = sup._child_to_client

    def _recording_pump(child) -> None:
        if child is second:
            pump_started.set()
        original_child_to_client(child)

    monkeypatch.setattr(sup, "_child_to_client", _recording_pump)

    assert sup.run() == 0
    assert pump_started.is_set()
    assert request + b"\n" in second.stdin.getvalue()


def test_disconnected_client_drops_response_then_releases_pending(monkeypatch) -> None:
    sup = supervisor.Supervisor(idempotent_tools=frozenset({"safe_tool"}))
    state, _, _ = _ready_protocol(idempotent_tools=frozenset({"safe_tool"}))
    sup._protocol = state
    request = _payload(
        {
            "jsonrpc": "2.0",
            "id": 44,
            "method": "tools/call",
            "params": {"name": "safe_tool", "arguments": {}},
        }
    )
    assert sup._protocol.accept(request).kind is supervisor._ClientActionKind.FORWARD

    class _Disconnected:
        def write(self, _data: bytes) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise AssertionError("flush must not follow failed write")

    monkeypatch.setattr(
        supervisor.sys,
        "stdout",
        types.SimpleNamespace(buffer=_Disconnected()),
    )
    child = types.SimpleNamespace(
        stdout=io.BytesIO(_payload({"jsonrpc": "2.0", "id": 44, "result": {"ok": True}}) + b"\n")
    )

    sup._pump_child_lines(child)

    assert sup._client_disconnected.is_set()
    assert sup._protocol.pending_count == 0


def test_child_pump_releases_reader_state_for_finished_generation() -> None:
    sup = supervisor.Supervisor()
    child = types.SimpleNamespace(stdout=io.BytesIO(b""), poll=lambda: 0)

    sup._child_to_client(child)

    assert id(child) not in sup._response_readers
