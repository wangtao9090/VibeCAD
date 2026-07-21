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
import types
from pathlib import Path

import pytest

from vibecad import supervisor
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


def _rpc(proc: subprocess.Popen, id_: int, method: str) -> dict:
    _send(proc, {"jsonrpc": "2.0", "id": id_, "method": method})
    return json.loads(_readline(proc))


def test_passthrough_and_swap(sup_factory):
    """透传 + 换芯 gen=1→2 + 握手重放响应被丢弃 + 换芯后请求零感知成功 + 正常退出 0。"""
    sup = sup_factory()
    _send(sup, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert json.loads(_readline(sup))["result"]["gen"] == 1
    _send(sup, {"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert _rpc(sup, 1, "tools/call")["result"]["gen"] == 1  # 换芯前
    _send(sup, {"jsonrpc": "2.0", "method": "swap"})  # 触发 exit(75)
    resp = _rpc(sup, 2, "tools/call")
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
    _send(sup, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    _readline(sup)  # 同步点：子进程已起且已落 PID
    child_pid = int(pid_file.read_text())
    assert _pid_alive(child_pid)

    sup.stdin.close()
    assert sup.wait(timeout=15) == 0  # supervisor 等子进程收尾后自身退出
    assert not _pid_alive(child_pid)  # 子进程无孤儿残留


def test_real_crash_code_passthrough(sup_factory):
    """真崩溃（非 SWAP_EXIT）：退出码原样透传，不掩盖、不重启。"""
    sup = sup_factory()
    _send(sup, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    _readline(sup)
    _send(sup, {"jsonrpc": "2.0", "method": "crash"})  # fake server exit(3)
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
    sup = sup_factory()
    _send(sup, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    _readline(sup)
    assert home.exists()  # 启动清理不背锅：此时尚无标记

    (home / ".uninstall_requested").touch()  # 模拟运行中 request_uninstall 落标记
    _send(sup, {"jsonrpc": "2.0", "method": "swap"})
    assert _rpc(sup, 1, "ping")["result"]["gen"] == 2  # 同步点：重启已完成
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
            "VIBECAD_FAKE_PID_FILE": str(pid_file),
            "VIBECAD_TEST_HANDSHAKE_TIMEOUT": "3",  # 调小 deadline：黑盒不等 30s
        }
    )
    _send(sup, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    assert json.loads(_readline(sup))["result"]["gen"] == 1
    _send(sup, {"jsonrpc": "2.0", "method": "swap"})

    assert sup.wait(timeout=15) == 1  # 换芯失败：非零码响亮退出
    assert int((tmp_path / "gen").read_text()) == 2  # 新子进程确实起过（挂死的是它）
    assert not _pid_alive(int(pid_file.read_text()))  # 挂死子进程已被强杀，无孤儿


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
    assert supervisor._server_cmd() == [str(py), "-m", "vibecad.server"]


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
    monkeypatch.setattr(supervisor.uninstall, "perform_pending_uninstall", lambda: True)
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda *a, **kw: captured.update(kw) or "proc",
    )
    assert supervisor.Supervisor()._spawn() == "proc"
    assert captured["env"]["VIBECAD_SUPERVISED"] == "1"


def test_run_pending_uninstall_warns_when_marker_left(monkeypatch, tmp_path, capsys):
    """I1：删除未完成（perform 返回 False 且标记仍在，如 Windows 文件锁/杀毒占用）
    → stderr 一行警告，不再静默让用户以为已卸载干净。"""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".uninstall_requested").touch()
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setattr(supervisor.uninstall, "perform_pending_uninstall", lambda: False)

    supervisor.run_pending_uninstall()

    err = capsys.readouterr().err
    assert "卸载未完成" in err and str(home) in err


def test_run_pending_uninstall_quiet_when_no_marker(monkeypatch, tmp_path, capsys):
    """I1 反向：无标记的常规启动（返回 False 但无标记）→ 保持静默，不制造噪音。"""
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(supervisor.uninstall, "perform_pending_uninstall", lambda: False)

    supervisor.run_pending_uninstall()

    assert capsys.readouterr().err == ""


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
    monkeypatch.setattr(
        supervisor.subprocess,
        "Popen",
        lambda cmd, **kw: captured.update(cmd=cmd) or "proc",
    )

    assert supervisor.Supervisor()._spawn() == "proc"

    assert home.exists()
    assert not (home / "runtime").exists()  # 待删标记已兑现，授权运行时消失
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
    sup._handshake = [b'{"jsonrpc":"2.0","id":0,"method":"initialize"}\n']
    sup._init_id = 0
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
    sup._handshake = [
        b'{"jsonrpc":"2.0","id":0,"method":"initialize"}\n',
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
    ]
    sup._init_id = 0
    child = types.SimpleNamespace(stdin=_DeadPipe(), stdout=io.BytesIO(b""), poll=lambda: 1)
    assert sup._replay_handshake(child) is True


def test_child_pump_crash_kills_child_and_exits_nonzero(unclosed_stdin, monkeypatch, capsys):
    """C3①：child→client 泵崩溃 → 不再是「子进程 stdout 无人读、pipe 写满全宿主
    冻结」——kill 子进程、run() 以非零码退出、stderr 有 traceback。"""
    sup = supervisor.Supervisor()

    def _boom(ch):
        raise RuntimeError("child pump exploded")

    monkeypatch.setattr(sup, "_pump_child_lines", _boom)
    monkeypatch.setattr(sup, "_spawn", lambda: _KillableChild())

    assert sup.run() == 1
    err = capsys.readouterr().err
    assert "child pump exploded" in err and "RuntimeError" in err


def test_client_pump_crash_kills_child_and_exits_nonzero(unclosed_stdin, monkeypatch, capsys):
    """C3①：client→child 泵（daemon）崩溃 → EOF 收尾链路失效也不留孤儿对——
    兜底 kill 子进程 + run() 非零码退出。"""
    sup = supervisor.Supervisor()

    def _boom():
        raise RuntimeError("client pump exploded")

    monkeypatch.setattr(sup, "_pump_client_lines", _boom)
    monkeypatch.setattr(sup, "_spawn", lambda: _KillableChild())

    assert sup.run() == 1
    assert "client pump exploded" in capsys.readouterr().err


def test_client_to_child_forwards_unbookable_ids(monkeypatch):
    """C3②：非 JSON、非对象、以及 **JSON-RPC 非法 id（数组，不可哈希）** 行都只
    跳过记账，透传不中断、线程不崩（原用例未覆盖非法 id——修 L117 TypeError 入口）。"""
    lines = (
        b"[1,2,3]\n"
        b"not-json\n"
        b'{"jsonrpc":"2.0","id":[1,2],"method":"tools/call"}\n'  # 非法 id：数组
        b'{"jsonrpc":"2.0","id":null,"method":"x"}\n'  # null id：不记账
        b'{"jsonrpc":"2.0","id":7,"method":"tools/call"}\n'
    )
    _fake_stdin(monkeypatch, lines)
    sup = supervisor.Supervisor()
    child = types.SimpleNamespace(stdin=_FakePipe(), stdout=io.BytesIO(b""), poll=lambda: 0)
    sup._child = child
    sup._client_to_child()

    assert child.stdin.closed_value == lines  # 全部原样透传 + EOF 后关闭子进程 stdin
    assert sup._client_eof.is_set()
    assert list(sup._pending) == [7]  # 只有合法可哈希 id 进记账


def test_child_to_client_survives_unbookable_response_id(monkeypatch):
    """C3②：响应行 id 为数组（不可哈希）→ 销账降级跳过、照常透传，泵不崩
    （修 L150 _pending.pop TypeError 入口）。"""
    out = io.BytesIO()
    monkeypatch.setattr(supervisor.sys, "stdout", types.SimpleNamespace(buffer=out))
    lines = b'{"jsonrpc":"2.0","id":[1,2],"result":{}}\n{"jsonrpc":"2.0","id":7,"result":{}}\n'
    sup = supervisor.Supervisor()
    sup._pending[7] = b"x"
    child = types.SimpleNamespace(stdout=io.BytesIO(lines))

    sup._child_to_client(child)

    assert out.getvalue() == lines  # 两行原样透传
    assert sup._pending == {}  # 合法 id 正常销账
    assert not sup._pump_failed.is_set()  # 泵没有崩


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
