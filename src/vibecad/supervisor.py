"""监督进程：宿主与 server 之间的换芯中继（Round 11 C 分支）。

宿主（MCP 客户端）对意外退出的 server 不做自动重启（Spike Q2 结论），因此由本进程
常驻宿主与 server 子进程之间，按行透传 stdio（MCP stdio = ndjson，一行一条消息）：

- 子进程以 SWAP_EXIT 自退 = 换芯请求 → 以 _server_cmd() 重新选择解释器（此刻就绪
  哨兵已落地 → conda python）重启子进程，重放 initialize/initialized 握手（丢弃重放
  的 initialize 响应——客户端已持有首次响应，重复响应是协议错误），并重发换芯窗口
  内未获响应的请求——客户端全程零感知。重发安全的前提：换芯仅发生于 bootstrap
  阶段，窗口内悬空的只会是 bootstrap 工具调用（get_runtime_status / ensure_runtime /
  被 guard 结构化拒绝的 CAD 调用）；ensure_runtime 幂等但并非只读（未装会触发后台
  安装），重发不会叠加副作用。
- 子进程以其他码退出 = 真退出/崩溃 → 原样透传退出码，绝不掩盖、绝不重启（宿主主动
  重启场景〔升级/设置变更〕宿主自有 auto-reconnect，本进程不越权处理）。
- 宿主关闭（stdin EOF）→ 关子进程 stdin 令其自然收尾，等其退出后自身退出，绝不留
  孤儿进程（升级流程先 shutdown 再删目录，残留进程会持有已删目录句柄）。
- 每次 spawn 子进程前执行 run_pending_uninstall()：换芯重启不经 launcher.main，
  卸载标记的「重启后清理」链路在此接线；删除未完成会在 stderr 响亮警告（I1）。

已知限制（N1）：换芯窗口内旧子进程尚未透传的服务端通知（无 id、不记账）随进程死亡
丢失，客户端方向的通知同样不重发——MCP 通知本为 fire-and-forget，有意接受。

纪律：纯 stdlib，绝不 import mcp/server/FreeCAD——依赖单向：server 从本模块
import SWAP_EXIT / runtime_swappable，本模块只以子进程方式拉起 server。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from vibecad.runtime import uninstall

SWAP_EXIT = 75  # server 自退换芯约定码（借 sysexits EX_TEMPFAIL，避开常见错误码）
_EOF_GRACE_SECONDS = 15.0  # stdin EOF 后子进程未收尾的强制回收时限（防孤儿兜底）
_TERM_KILL_GRACE_SECONDS = 5.0  # terminate 后仍不死的升级 kill 时限（I3）
# 换芯握手期限（C2）：新子进程不死也不响应（conda 首启慢/杀毒扫描/import 死锁）时
# 绝不永等。VIBECAD_TEST_HANDSHAKE_TIMEOUT 仅测试注入（黑盒无法 monkeypatch 常量）。
_HANDSHAKE_TIMEOUT_SECONDS = float(
    os.environ.get("VIBECAD_TEST_HANDSHAKE_TIMEOUT", "") or 30.0)
# 换芯循环护栏（C1）：连续 SWAP_EXIT 间隔均小于窗口即判定自杀重启循环，停止换芯。
_SWAP_LOOP_WINDOW_SECONDS = 5.0
_SWAP_LOOP_LIMIT = 3


def runtime_swappable() -> bool:
    """换芯判据唯一真源（C1）：就绪哨兵在 **且** conda python 存在。

    server 的换芯触发点与本模块 _server_cmd 必须共用本判据——两侧不一致（如哨兵在
    而 conda python 被半删/杀毒隔离/卸载 rmtree 半失败）会形成「server 自杀 →
    supervisor 落 bootstrap → 新 server 又自杀」的无限重启循环。探测失败（OSError）
    按不可换芯处理并在 stderr 记一行原因（N3），绝不静默。"""
    from vibecad.runtime import paths, status  # 延迟 import：留给测试单独 stub 判据

    try:
        return status.runtime_ready() and paths.active_runtime_python().exists()
    except OSError as exc:
        print(f"vibecad-supervisor: 换芯判据探测失败，按不可换芯处理：{exc}",
              file=sys.stderr)
        return False


def _server_cmd() -> list[str]:
    """选择 server 解释器：runtime_swappable()（哨兵在且 conda python 存在，与
    server 的换芯触发点同一判据）→ conda 解释器；否则当前（引导）解释器。每次
    spawn 现算——换芯窗口内哨兵刚落地，必须重读。conda env 找得到 vibecad 模块的
    机制：安装器已把 vibecad 连同 mcp pip 装进该 env（installer._do_install），并
    冒烟验证过。"""
    if override := os.environ.get("VIBECAD_SUPERVISOR_TEST_CMD"):  # 仅测试注入
        return json.loads(override)
    if runtime_swappable():
        from vibecad.runtime import paths

        return [str(paths.active_runtime_python()), "-m", "vibecad.server"]
    return [sys.executable, "-m", "vibecad.server"]


def run_pending_uninstall() -> None:
    """执行待删清理，并把「删除未完成」变响亮（I1）：perform_pending_uninstall
    返回 False 且标记仍在 = rmtree 部分失败（Windows 文件锁/杀毒占用），静默吞掉
    会让用户以为已卸载干净。无标记的常规启动保持静默。"""
    if uninstall.perform_pending_uninstall():
        return
    try:
        marker = uninstall.uninstall_marker()
        marker_left = marker.exists()
    except OSError:
        return
    if marker_left:
        print(f"vibecad: 卸载未完成：残留于 {marker.parent}，下次启动重试",
              file=sys.stderr)


def _stdin_lines():
    """按行读宿主 stdin：走原始 fd 而非 sys.stdin 的 BufferedReader。daemon 泵线程若
    阻塞在 BufferedReader 锁内，server 真崩溃（宿主 stdin 仍开着）后解释器收尾会
    fatal（_enter_buffered_busy → SIGABRT），把真退出码替换成 -6；os.read 阻塞
    不持任何 Python 锁，进程可随时以真退出码干净退出。"""
    fd = sys.stdin.buffer.fileno()
    buf = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:  # fd 被关/失效：等同 EOF 收尾
            chunk = b""
        if not chunk:
            break
        buf += chunk
        while (i := buf.find(b"\n")) != -1:
            yield buf[: i + 1]
            buf = buf[i + 1:]
    if buf:
        yield buf  # 无换行的尾行也如实转发


def _parse(line: bytes) -> dict:
    """行 → JSON 对象；非 JSON / 非对象（如批量数组）一律回空 dict：
    只影响记账，透传照常，绝不让坏行打断泵线程。"""
    try:
        msg = json.loads(line)
    except ValueError:
        return {}
    return msg if isinstance(msg, dict) else {}


def _bookable_id(msg: dict) -> object | None:
    """可入账的 JSON-RPC id（str/int/float）；其他形态（数组/对象/null）返回 None
    = 只透传不记账（C3：数组/对象 id 不可哈希，绝不让泵线程 TypeError 崩掉）。"""
    i = msg.get("id")
    return i if isinstance(i, (str, int, float)) else None


class Supervisor:
    """单实例监督器：run() 阻塞直至 server 真退出，返回其退出码。"""

    def __init__(self) -> None:
        self._handshake: list[bytes] = []        # [initialize 行, initialized 行]
        self._init_id: object = None
        self._pending: dict[object, bytes] = {}  # 已转发未响应请求 {id: 原始行}
        self._wlock = threading.Lock()           # 串行化：记账 + child.stdin 写 + 换芯切换
        self._child: subprocess.Popen | None = None
        self._client_eof = threading.Event()     # 宿主已关 stdin：不再换芯重启
        self._pump_failed = threading.Event()    # 泵线程崩溃：run() 以非零码收尾（C3）

    def _spawn(self) -> subprocess.Popen:
        # 要点③：换芯重启不经 launcher.main，卸载标记清理在此接线；必须先清理再选
        # 解释器——删 home 连带清掉就绪哨兵，_server_cmd 才会安全落回 bootstrap。
        run_pending_uninstall()
        env = {**os.environ, "VIBECAD_SUPERVISED": "1"}  # I4：告知 server 自杀有人重启
        return subprocess.Popen(_server_cmd(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,  # stderr 继承，直通宿主日志
                                env=env)

    def _pump_panic(self, what: str) -> None:
        """泵线程崩溃兜底（C3）：静默半死比响亮崩溃更糟——child→client 泵死掉会让
        子进程 stdout 无人读、pipe 写满后整个宿主冻结；client→child 泵（daemon）
        死掉会让 EOF 收尾链路失效留下孤儿对。stderr 记 traceback、强杀当前子进程
        （令 run() 的 wait() 立刻返回）、置失败旗让 run() 以非零码退出。"""
        print(f"vibecad-supervisor: {what} 泵线程崩溃，强杀子进程并退出：\n"
              f"{traceback.format_exc()}", file=sys.stderr)
        self._pump_failed.set()
        ch = self._child
        if ch is not None:
            with contextlib.suppress(OSError):
                ch.kill()

    def _client_to_child(self) -> None:
        try:
            self._pump_client_lines()
        except Exception:  # noqa: BLE001 - 顶层兜底：见 _pump_panic
            self._pump_panic("client→child")

    def _pump_client_lines(self) -> None:
        """宿主 → 子进程：透传 + 握手/请求记账。记账与写入同锁：换芯时 run() 的
        pending 重发与本线程要么先记账后重发、要么先换芯后直写新子进程，
        两种交错都恰好送达一次，不双发。"""
        for line in _stdin_lines():
            with self._wlock:
                msg = _parse(line)
                method = msg.get("method")
                if method == "initialize":
                    self._handshake = [line]
                    self._init_id = msg.get("id")
                elif method == "notifications/initialized":
                    self._handshake.append(line)
                elif method and (bid := _bookable_id(msg)) is not None:
                    self._pending[bid] = line  # 请求记账（见响应即销账）
                ch = self._child
                if ch and ch.stdin:
                    with contextlib.suppress(OSError):  # 子进程刚死：换芯后由 pending 重发
                        ch.stdin.write(line)
                        ch.stdin.flush()
        # 宿主关闭（要点①）：先立旗——run() 见旗即使撞上 SWAP_EXIT 也不再重启；
        # 再关子进程 stdin 令其自然收尾（run() 正等着它退出，随后整个进程退出）。
        self._client_eof.set()
        with self._wlock:
            ch = self._child
            if ch and ch.stdin:
                with contextlib.suppress(OSError):
                    ch.stdin.close()
        if ch is not None:
            self._reap_after_grace(ch)

    def _reap_after_grace(self, ch: subprocess.Popen) -> None:
        """EOF 后兜底：子进程若不按 MCP 惯例随 stdin EOF 收尾，宽限期后 terminate；
        再等 _TERM_KILL_GRACE_SECONDS 仍不死则升级 kill()（I3：POSIX 上 SIGTERM
        可被忽略，需要这级强杀；Windows terminate 已是强杀，升级分支空转）。
        杜绝孤儿持有已删目录句柄。正常路径子进程秒退，此定时器空转。"""
        def _reap() -> None:
            if ch.poll() is not None:
                return
            with contextlib.suppress(OSError):
                ch.terminate()
            try:
                ch.wait(timeout=_TERM_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    ch.kill()
        t = threading.Timer(_EOF_GRACE_SECONDS, _reap)
        t.daemon = True
        t.start()

    def _child_to_client(self, ch: subprocess.Popen) -> None:
        try:
            self._pump_child_lines(ch)
        except Exception:  # noqa: BLE001 - 顶层兜底：见 _pump_panic
            self._pump_panic("child→client")

    def _pump_child_lines(self, ch: subprocess.Popen) -> None:
        """子进程 → 宿主：透传 + 响应销账。子进程退出（stdout EOF）自然结束。"""
        for line in ch.stdout:
            msg = _parse(line)
            if "method" not in msg and (bid := _bookable_id(msg)) is not None:
                self._pending.pop(bid, None)  # 响应已达客户端，销账
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    def _replay_handshake(self, ch: subprocess.Popen) -> bool:
        """向新子进程重放 initialize/initialized；其 initialize 响应丢弃（客户端持有
        的是首次握手响应，重复响应会被客户端视为协议错误）。

        返回 False = 新子进程在 _HANDSHAKE_TIMEOUT_SECONDS 内既不响应也不退出
        （conda 首启慢/杀毒扫描/import 死锁，C2），已被强杀——调用方按换芯失败以
        非零码收尾，绝不让宿主全部请求无限挂死。新子进程秒死（写入静默失败、
        stdout 秒 EOF）返回 True：run() 主循环随后如实拿到真退出码并透传。"""
        if not self._handshake:
            return True
        with contextlib.suppress(OSError):
            ch.stdin.write(self._handshake[0])
            ch.stdin.flush()
        done = threading.Event()

        def _drain() -> None:
            try:
                for line in ch.stdout:
                    msg = _parse(line)
                    if "id" in msg and msg["id"] == self._init_id and "method" not in msg:
                        break                      # 重放的 initialize 响应 → 丢弃
                    sys.stdout.buffer.write(line)  # 握手期其他输出（日志通知）照常透传
                    sys.stdout.buffer.flush()
            finally:
                done.set()

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        t.join(_HANDSHAKE_TIMEOUT_SECONDS)
        if not done.is_set():
            print("vibecad-supervisor: 换芯握手超时"
                  f"（{_HANDSHAKE_TIMEOUT_SECONDS:.0f}s 内新子进程无响应），"
                  "强杀新子进程并按换芯失败退出", file=sys.stderr)
            with contextlib.suppress(OSError):
                ch.kill()
            return False
        with contextlib.suppress(OSError):
            for extra in self._handshake[1:]:
                ch.stdin.write(extra)
            ch.stdin.flush()
        return True

    def run(self) -> int:
        self._child = self._spawn()
        threading.Thread(target=self._client_to_child, daemon=True).start()
        rapid_swaps = 0
        last_swap: float | None = None
        while True:
            ch = self._child
            pump = threading.Thread(target=self._child_to_client, args=(ch,))
            pump.start()
            code = ch.wait()
            pump.join()
            if self._pump_failed.is_set():
                return 1  # 泵线程崩溃（C3）：子进程已被强杀，响亮退出
            if code != SWAP_EXIT or self._client_eof.is_set():
                # 真退出/崩溃：如实透传，不掩盖；EOF 撞上换芯码视为干净收尾（此时
                # 重启的新子进程无人喂 stdin，只会挂成孤儿）。
                # 竞态说明（I5）：server 端 Timer 触发的 os._exit(SWAP_EXIT) 与真崩溃
                # 存在竞态——真崩溃恰在 Timer 到点时可能被 75 掩盖成一次「换芯」。
                # 有意接受：概率极低，且反复形态会被下方换芯循环护栏拦住并响亮退出。
                return 0 if code == SWAP_EXIT else code
            # C1 护栏：换芯判据两侧若失守（如哨兵在而 conda python 缺失），新 server
            # 一起来又自杀——绝不无限重启零日志，判定循环后按真退出透传非零码。
            now = time.monotonic()
            in_window = last_swap is not None and now - last_swap < _SWAP_LOOP_WINDOW_SECONDS
            rapid_swaps = rapid_swaps + 1 if in_window else 1
            last_swap = now
            if rapid_swaps >= _SWAP_LOOP_LIMIT:
                print(f"vibecad-supervisor: 检测到换芯循环（连续 {rapid_swaps} 次 "
                      f"SWAP_EXIT 间隔 < {_SWAP_LOOP_WINDOW_SECONDS:.0f}s），停止换芯并以"
                      f"退出码 {code} 结束；请检查运行时完整性（如就绪哨兵在而 conda "
                      "python 缺失）", file=sys.stderr)
                return code
            new = self._spawn()                # 换芯：此刻就绪哨兵已在 → conda python
            if not self._replay_handshake(new):
                with contextlib.suppress(subprocess.TimeoutExpired):
                    new.wait(timeout=_TERM_KILL_GRACE_SECONDS)  # kill 已发出：收尸防僵尸
                return 1                       # 换芯失败（C2）：非零码响亮退出
            with self._wlock:
                self._child = new
                # 重发换芯窗口悬空请求。重发安全性前提见模块 docstring：换芯仅发生于
                # bootstrap 阶段，悬空的只会是幂等的 bootstrap 工具调用。
                for line in list(self._pending.values()):
                    with contextlib.suppress(OSError):
                        new.stdin.write(line)
                with contextlib.suppress(OSError):
                    new.stdin.flush()
                if self._client_eof.is_set() and new.stdin:
                    # EOF 恰在换芯中到达（关的是旧子进程 stdin）：补关新子进程 stdin
                    # 令其随即收尾，并同样挂宽限回收（I2）——下一轮 wait() 以真退出码
                    # 结束，不守规矩的新子进程也绝不成孤儿。
                    with contextlib.suppress(OSError):
                        new.stdin.close()
                    self._reap_after_grace(new)
