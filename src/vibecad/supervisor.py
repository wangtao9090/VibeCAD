"""监督进程：宿主与 owned MCP server 之间的有界换芯中继。

宿主（MCP 客户端）对意外退出的 server 不做自动重启，因此由本进程常驻宿主与
server 子进程之间。入口先做有界 NDJSON framing 和闭合协议预检，再按请求 id 记账：

- 子进程以 SWAP_EXIT 自退 = 换芯请求 → 以 _server_cmd() 重新选择解释器（此刻就绪
  哨兵已落地 → conda python）重启子进程，重放 initialize/initialized 握手（丢弃重放
  的 initialize 响应——客户端已持有首次响应，重复响应是协议错误），并重发换芯窗口
  内未获响应且由公开工具注解证明幂等的请求——客户端全程零感知。非幂等请求绝不
  猜测重放，而是返回固定的 unknown-outcome 错误，要求调用方检查持久状态。
- 子进程以其他码退出 = 真退出/崩溃 → 原样透传退出码，绝不掩盖、绝不重启（宿主主动
  重启场景〔升级/设置变更〕宿主自有 auto-reconnect，本进程不越权处理）。
- 宿主关闭（stdin EOF）→ 关子进程 stdin 令其自然收尾，等其退出后自身退出，绝不留
  孤儿进程（升级流程先 shutdown 再删目录，残留进程会持有已删目录句柄）。
- 每次 spawn 子进程前执行 run_pending_uninstall()：换芯重启不经 launcher.main，
  卸载标记的「重启后清理」链路在此接线；删除未完成会在 stderr 响亮警告（I1）。

已知限制（N1）：换芯窗口内旧子进程尚未透传的服务端通知（无 id、不记账）随进程死亡
丢失，客户端方向的通知同样不重发——MCP 通知本为 fire-and-forget，有意接受。

纪律：绝不 import mcp/server/FreeCAD——依赖单向：server 从本模块 import
SWAP_EXIT / runtime_swappable，本模块只以子进程方式拉起 server。
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum

from vibecad import mcp_transport
from vibecad.runtime import uninstall

SWAP_EXIT = 75  # server 自退换芯约定码（借 sysexits EX_TEMPFAIL，避开常见错误码）
_EOF_GRACE_SECONDS = 15.0  # stdin EOF 后子进程未收尾的强制回收时限（防孤儿兜底）
_TERM_KILL_GRACE_SECONDS = 5.0  # terminate 后仍不死的升级 kill 时限（I3）
# 换芯握手期限（C2）：新子进程不死也不响应（conda 首启慢/杀毒扫描/import 死锁）时
# 绝不永等。VIBECAD_TEST_HANDSHAKE_TIMEOUT 仅测试注入（黑盒无法 monkeypatch 常量）。
_HANDSHAKE_TIMEOUT_SECONDS = float(os.environ.get("VIBECAD_TEST_HANDSHAKE_TIMEOUT", "") or 30.0)
# One already-admitted host frame gets one absolute child-stdin deadline.  A
# wedged child must not retain the normal ingress pump or block a later swap.
_INGRESS_WRITE_TIMEOUT_SECONDS = 30.0
# A normal process exit can close stdout just before ``wait()`` publishes its
# return code.  Give that race one short, fixed grace; a child still alive with
# no response channel is a terminal transport failure.
_CHILD_STDOUT_EOF_GRACE_SECONDS = 1.0
# A dead child can leave stdout open through an inheriting descendant.  The
# response pump gets one absolute drain bound before the supervisor fails
# closed instead of joining that pipe forever.
_CHILD_STDOUT_DRAIN_SECONDS = 30.0
# 换芯循环护栏（C1）：连续 SWAP_EXIT 间隔均小于窗口即判定自杀重启循环，停止换芯。
_SWAP_LOOP_WINDOW_SECONDS = 5.0
_SWAP_LOOP_LIMIT = 3

_HANDSHAKE_FRAME_LIMIT = 2
_HANDSHAKE_REPLAY_BYTES = 2 * mcp_transport.MAX_REQUEST_FRAME_BYTES
_SAFE_REPLAY_METHODS = frozenset(
    {
        "ping",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "resources/read",
    }
)
_DEFAULT_IDEMPOTENT_TOOLS = frozenset(
    {
        "ping",
        "get_runtime_status",
        "ensure_runtime",
        "uninstall_runtime",
        "get_capabilities",
        "create_project",
        "get_project",
        "create_task",
        "get_task",
        "submit_model_program",
        "resume_task",
        "accept_draft",
        "reject_draft",
        "export_task_artifacts",
        "create_box",
        "create_cylinder",
        "inspect_model",
        "modify_parameter",
        "move_part",
        "rotate_part",
    }
)
_UNKNOWN_OUTCOME = mcp_transport.FixedRpcError(
    -32003,
    "Tool outcome is unknown; inspect durable state before retry.",
)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _request_id_key(value: object) -> tuple[str, int | str] | None:
    if type(value) is int and abs(value) <= mcp_transport.MAX_SAFE_JSON_INTEGER:
        return ("integer", value)
    if type(value) is str:
        try:
            if len(value.encode("utf-8")) <= mcp_transport.MAX_JSON_STRING_BYTES:
                return ("string", value)
        except UnicodeEncodeError:
            pass
    return None


class _ResponseProtocolError(RuntimeError):
    """Fixed child-response failure which never retains response bytes."""

    def __init__(self) -> None:
        super().__init__("child response protocol failure")


class _DuplicateResponseKey(ValueError):
    pass


def _response_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateResponseKey
        result[key] = value
    return result


def _decode_response_payload(payload: bytes) -> dict[str, object]:
    try:
        if type(payload) is not bytes or len(payload) > mcp_transport.MAX_RESPONSE_FRAME_BYTES:
            raise ValueError
        decoded = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_response_pairs,
            parse_constant=lambda _token: (_ for _ in ()).throw(ValueError()),
        )
        if type(decoded) is not dict or decoded.get("jsonrpc") != "2.0":
            raise ValueError
    except (UnicodeError, ValueError, TypeError, RecursionError):
        pass
    else:
        return decoded
    raise _ResponseProtocolError from None


def _is_server_notification(message: dict[str, object]) -> bool:
    if "method" not in message:
        return False
    keys = frozenset(message)
    if (
        keys not in {frozenset({"jsonrpc", "method"}), frozenset({"jsonrpc", "method", "params"})}
        or type(message["method"]) is not str
        or ("params" in message and type(message["params"]) is not dict)
    ):
        raise _ResponseProtocolError
    return True


def _validated_response_id(message: dict[str, object]) -> int | str:
    keys = frozenset(message)
    result_keys = frozenset({"jsonrpc", "id", "result"})
    error_keys = frozenset({"jsonrpc", "id", "error"})
    if keys not in {result_keys, error_keys}:
        raise _ResponseProtocolError
    request_id = message["id"]
    if _request_id_key(request_id) is None:
        raise _ResponseProtocolError
    if keys == error_keys:
        error = message["error"]
        if type(error) is not dict:
            raise _ResponseProtocolError
        error_keys_present = frozenset(error)
        if (
            not frozenset({"code", "message"}) <= error_keys_present
            or not error_keys_present <= frozenset({"code", "message", "data"})
            or type(error.get("code")) is not int
            or type(error.get("message")) is not str
        ):
            raise _ResponseProtocolError
    return request_id  # type: ignore[return-value]


def _validated_initialize_response_id(message: dict[str, object]) -> int | str:
    """Require a successful, minimally exact MCP InitializeResult."""

    request_id = _validated_response_id(message)
    if "result" not in message:
        raise _ResponseProtocolError
    result = message["result"]
    if type(result) is not dict:
        raise _ResponseProtocolError
    required = frozenset({"protocolVersion", "capabilities", "serverInfo"})
    optional = frozenset({"instructions", "_meta"})
    if not required <= frozenset(result) <= required | optional:
        raise _ResponseProtocolError
    protocol_version = result["protocolVersion"]
    capabilities = result["capabilities"]
    server_info = result["serverInfo"]
    if (
        type(protocol_version) is not str
        or not protocol_version
        or len(protocol_version.encode("utf-8")) > 64
        or type(capabilities) is not dict
        or type(server_info) is not dict
    ):
        raise _ResponseProtocolError
    server_required = frozenset({"name", "version"})
    server_optional = frozenset({"title", "websiteUrl", "icons"})
    if not server_required <= frozenset(server_info) <= server_required | server_optional:
        raise _ResponseProtocolError
    for key in server_required:
        value = server_info[key]
        if type(value) is not str or not value:
            raise _ResponseProtocolError
        try:
            if len(value.encode("utf-8")) > mcp_transport.MAX_JSON_STRING_BYTES:
                raise _ResponseProtocolError
        except UnicodeEncodeError:
            raise _ResponseProtocolError from None
    if "instructions" in result and result["instructions"] is not None:
        if type(result["instructions"]) is not str:
            raise _ResponseProtocolError
    if "_meta" in result and result["_meta"] is not None:
        if type(result["_meta"]) is not dict:
            raise _ResponseProtocolError
    return request_id


class _BoundedResponseReader:
    """Read one child NDJSON response at a time without an unbounded readline."""

    def __init__(
        self,
        *,
        limit: int = mcp_transport.MAX_RESPONSE_FRAME_BYTES,
        chunk_bytes: int = mcp_transport.READ_CHUNK_BYTES,
    ) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError("response limit is invalid")
        if (
            type(chunk_bytes) is not int
            or chunk_bytes < 1
            or chunk_bytes > mcp_transport.READ_CHUNK_BYTES
        ):
            raise ValueError("response chunk size is invalid")
        self._limit = limit
        self._chunk_bytes = chunk_bytes
        self._buffer = bytearray()
        self._draining = False

    def _read_chunk(self, stream: object) -> bytes:
        reader = getattr(stream, "read1", None)
        if reader is None:
            reader = getattr(stream, "read", None)
        if not callable(reader):
            raise _ResponseProtocolError
        try:
            chunk = reader(self._chunk_bytes)
        except Exception:
            raise _ResponseProtocolError from None
        if type(chunk) is not bytes:
            raise _ResponseProtocolError
        return chunk

    def read_frame(self, stream: object) -> bytes | None:
        while True:
            if not self._draining:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    if newline > self._limit:
                        self._buffer.clear()
                        raise _ResponseProtocolError
                    payload = bytes(self._buffer[:newline])
                    del self._buffer[: newline + 1]
                    return payload

            chunk = self._read_chunk(stream)
            if not chunk:
                failed = self._draining or bool(self._buffer)
                self._buffer.clear()
                self._draining = False
                if failed:
                    raise _ResponseProtocolError
                return None

            if self._draining:
                if b"\n" in chunk:
                    self._draining = False
                    raise _ResponseProtocolError
                continue

            newline = chunk.find(b"\n")
            if newline >= 0:
                fragment = chunk[:newline]
                if len(self._buffer) + len(fragment) > self._limit:
                    self._buffer.clear()
                    raise _ResponseProtocolError
                self._buffer.extend(fragment)
                payload = bytes(self._buffer)
                self._buffer = bytearray(chunk[newline + 1 :])
                return payload

            available = self._limit - len(self._buffer)
            if len(chunk) <= available:
                self._buffer.extend(chunk)
            else:
                self._buffer.clear()
                self._draining = True


class _ClientActionKind(Enum):
    FORWARD = "forward"
    RESPOND = "respond"
    DROP = "drop"
    REJECT = "reject"


class _ChildWriteResult(Enum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class _ClientAction:
    kind: _ClientActionKind
    payload: bytes | None = None
    response: dict[str, object] | None = None
    close: bool = False


@dataclass(slots=True)
class _PendingRequest:
    request_id: int | str
    id_key: tuple[str, int | str]
    payload: bytes
    replay_safe: bool
    cancel_requested: bool = False
    response_prepared: bool = False


@dataclass(frozen=True, slots=True)
class _UnknownOutcome:
    request_id: int | str
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _SwapPlan:
    _replay: tuple[_PendingRequest, ...]
    unknown_outcomes: tuple[_UnknownOutcome, ...]

    @property
    def replay_payloads(self) -> tuple[bytes, ...]:
        return tuple(item.payload for item in self._replay)

    @property
    def cancellation_payloads(self) -> tuple[bytes, ...]:
        return tuple(
            _json_bytes(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": item.request_id},
                }
            )
            for item in self._replay
            if item.cancel_requested
        )


@dataclass(frozen=True, slots=True)
class _PreparedResponse:
    payload: bytes
    pending_key: tuple[str, int | str] | None
    forward: bool = True
    initialize_response: bool = False


class _HandshakePhase(Enum):
    INITIALIZE = "initialize"
    INITIALIZED = "initialized"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class _ServerSelection:
    command: tuple[str, ...]
    uses_runtime: bool


class _SupervisorProtocolState:
    """Finite supervisor-side handshake, pending-id, cancel and replay state."""

    def __init__(self, *, idempotent_tools: frozenset[str]) -> None:
        if type(idempotent_tools) is not frozenset or any(
            type(name) is not str or not name for name in idempotent_tools
        ):
            raise TypeError("idempotent tool set is invalid")
        self._idempotent_tools = idempotent_tools
        self._phase = _HandshakePhase.INITIALIZE
        self._handshake: list[bytes] = []
        self._init_id: int | str | None = None
        self._initialize_response_prepared = False
        self._initialize_response_complete = False
        self._pending: dict[tuple[str, int | str], _PendingRequest] = {}

    @property
    def handshake_complete(self) -> bool:
        return self._phase is _HandshakePhase.READY

    @property
    def handshake_frame_count(self) -> int:
        return len(self._handshake)

    @property
    def handshake_retained_bytes(self) -> int:
        return sum(len(payload) for payload in self._handshake)

    @property
    def handshake_payloads(self) -> tuple[bytes, ...]:
        return tuple(self._handshake)

    @property
    def init_id(self) -> int | str | None:
        return self._init_id

    @property
    def initialize_response_complete(self) -> bool:
        return self._initialize_response_complete

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pending_retained_bytes(self) -> int:
        return sum(len(pending.payload) for pending in self._pending.values())

    @staticmethod
    def _reject(
        request_id: int | str | None,
        *,
        close: bool,
    ) -> _ClientAction:
        response = None
        if request_id is not None:
            response = mcp_transport.rpc_error_response(
                mcp_transport.INVALID_REQUEST,
                request_id=request_id,
            )
        return _ClientAction(
            _ClientActionKind.REJECT,
            response=response,
            close=close,
        )

    def _replay_safe(self, descriptor: mcp_transport.ClientMessageDescriptor) -> bool:
        if descriptor.method in _SAFE_REPLAY_METHODS:
            return True
        if descriptor.method != "tools/call":
            return False
        name = descriptor.params.get("name")
        return type(name) is str and name in self._idempotent_tools

    def accept(self, payload: bytes) -> _ClientAction:
        descriptor = mcp_transport.decode_and_prevalidate(payload)
        if self._phase is _HandshakePhase.INITIALIZE:
            if descriptor.method != "initialize" or descriptor.request_id is None:
                return self._reject(descriptor.request_id, close=True)
            self._handshake.append(payload)
            self._init_id = descriptor.request_id
            self._phase = _HandshakePhase.INITIALIZED
            return _ClientAction(_ClientActionKind.FORWARD, payload=payload)

        if self._phase is _HandshakePhase.INITIALIZED:
            if (
                not self._initialize_response_complete
                or descriptor.method != "notifications/initialized"
                or not descriptor.is_notification
            ):
                return self._reject(descriptor.request_id, close=True)
            if (
                len(self._handshake) + 1 > _HANDSHAKE_FRAME_LIMIT
                or self.handshake_retained_bytes + len(payload) > _HANDSHAKE_REPLAY_BYTES
            ):
                return self._reject(descriptor.request_id, close=True)
            self._handshake.append(payload)
            self._phase = _HandshakePhase.READY
            return _ClientAction(_ClientActionKind.FORWARD, payload=payload)

        if descriptor.method == "initialize":
            return self._reject(descriptor.request_id, close=True)
        if descriptor.method == "notifications/initialized":
            return self._reject(descriptor.request_id, close=True)

        if descriptor.is_cancellation:
            target = descriptor.cancellation_target
            key = _request_id_key(target)
            if key is not None:
                pending = self._pending.get(key)
                if pending is not None:
                    if pending.response_prepared:
                        return _ClientAction(_ClientActionKind.DROP)
                    pending.cancel_requested = True
            return _ClientAction(_ClientActionKind.FORWARD, payload=payload)

        if descriptor.is_notification:
            if self.pending_count >= mcp_transport.MAX_IN_FLIGHT:
                return _ClientAction(_ClientActionKind.DROP)
            return _ClientAction(_ClientActionKind.FORWARD, payload=payload)

        request_id = descriptor.request_id
        assert request_id is not None
        key = _request_id_key(request_id)
        assert key is not None
        init_key = _request_id_key(self._init_id)
        initialize_id_active = key == init_key and not self._initialize_response_complete
        if initialize_id_active or key in self._pending:
            return _ClientAction(
                _ClientActionKind.RESPOND,
                response=mcp_transport.rpc_error_response(
                    mcp_transport.INVALID_REQUEST,
                    request_id=request_id,
                ),
            )
        if self.pending_count >= mcp_transport.MAX_IN_FLIGHT:
            return _ClientAction(
                _ClientActionKind.RESPOND,
                response=mcp_transport.rpc_error_response(
                    mcp_transport.SERVER_BUSY,
                    request_id=request_id,
                ),
            )
        self._pending[key] = _PendingRequest(
            request_id=request_id,
            id_key=key,
            payload=payload,
            replay_safe=self._replay_safe(descriptor),
        )
        return _ClientAction(_ClientActionKind.FORWARD, payload=payload)

    def is_cancel_requested(self, request_id: int | str) -> bool:
        key = _request_id_key(request_id)
        if key is None:
            return False
        pending = self._pending.get(key)
        return pending is not None and pending.cancel_requested

    def plan_swap(self) -> _SwapPlan:
        replay: list[_PendingRequest] = []
        unknown: list[_UnknownOutcome] = []
        for pending in self._pending.values():
            if pending.replay_safe:
                replay.append(pending)
            else:
                unknown.append(
                    _UnknownOutcome(
                        request_id=pending.request_id,
                        payload=mcp_transport.rpc_error_response(
                            _UNKNOWN_OUTCOME,
                            request_id=pending.request_id,
                        ),
                    )
                )
        return _SwapPlan(tuple(replay), tuple(unknown))

    def acknowledge(self, request_id: int | str) -> None:
        key = _request_id_key(request_id)
        if key is not None:
            self._pending.pop(key, None)

    def prepare_response(self, payload: bytes) -> _PreparedResponse:
        message = _decode_response_payload(payload)
        if _is_server_notification(message):
            return _PreparedResponse(payload, None)
        request_id = _validated_response_id(message)
        key = _request_id_key(request_id)
        assert key is not None
        pending = self._pending.get(key)
        if pending is None:
            init_key = _request_id_key(self._init_id)
            if (
                key != init_key
                or self._initialize_response_complete
                or self._initialize_response_prepared
            ):
                raise _ResponseProtocolError
            initialize_id = _validated_initialize_response_id(message)
            if _request_id_key(initialize_id) != init_key:
                raise _ResponseProtocolError
            self._initialize_response_prepared = True
            return _PreparedResponse(
                payload,
                None,
                initialize_response=True,
            )
        if pending.response_prepared:
            raise _ResponseProtocolError
        pending.response_prepared = True
        if pending.cancel_requested:
            payload = _json_bytes(
                mcp_transport.rpc_error_response(
                    mcp_transport.REQUEST_CANCELLED,
                    request_id=pending.request_id,
                )
            )
        return _PreparedResponse(payload, key)

    def acknowledge_response(self, prepared: _PreparedResponse) -> None:
        if prepared.initialize_response:
            if not self._initialize_response_prepared or self._initialize_response_complete:
                raise _ResponseProtocolError
            self._initialize_response_prepared = False
            self._initialize_response_complete = True
        if prepared.pending_key is not None:
            self._pending.pop(prepared.pending_key, None)


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
        print(f"vibecad-supervisor: 换芯判据探测失败，按不可换芯处理：{exc}", file=sys.stderr)
        return False


def _server_selection(*, allow_runtime: bool = True) -> _ServerSelection:
    """Choose one child command and remember whether it owns runtime dirs."""

    if override := os.environ.get("VIBECAD_SUPERVISOR_TEST_CMD"):  # 仅测试注入
        return _ServerSelection(tuple(json.loads(override)), uses_runtime=False)
    if allow_runtime and runtime_swappable():
        from vibecad.runtime import paths

        return _ServerSelection(
            (str(paths.active_runtime_python()), "-B", "-m", "vibecad.server"),
            uses_runtime=True,
        )
    return _ServerSelection(
        (sys.executable, "-m", "vibecad.server"),
        uses_runtime=False,
    )


def _server_cmd() -> list[str]:
    """选择 server 解释器：runtime_swappable()（哨兵在且 conda python 存在，与
    server 的换芯触发点同一判据）→ conda 解释器；否则当前（引导）解释器。每次
    spawn 现算——换芯窗口内哨兵刚落地，必须重读。conda env 找得到 vibecad 模块的
    机制：安装器已把 vibecad 连同 mcp pip 装进该 env（installer._do_install），并
    冒烟验证过。"""
    return list(_server_selection().command)


def run_pending_uninstall() -> bool:
    """执行待删清理，并把「删除未完成」变响亮（I1）：perform_pending_uninstall
    返回 False 且标记仍在 = rmtree 部分失败（Windows 文件锁/杀毒占用），静默吞掉
    会让用户以为已卸载干净。无标记的常规启动保持静默。返回 True 表示无
    pending marker，或该 marker 已完全收敛；False 要求本次 spawn 强制留在
    bootstrap，不得选择或重建残留 runtime。"""
    if uninstall.perform_pending_uninstall():
        return True
    try:
        marker = uninstall.uninstall_marker()
        marker_left = os.path.lexists(marker)
    except OSError:
        return False
    if marker_left:
        print(f"vibecad: 卸载未完成：残留于 {marker.parent}，下次启动重试", file=sys.stderr)
        return False
    return True


def _stdin_frames():
    """Read bounded request frames from the raw host fd in fixed-size chunks."""

    fd = sys.stdin.buffer.fileno()
    framer = mcp_transport.RequestLineFramer()
    while True:
        try:
            chunk = os.read(fd, mcp_transport.READ_CHUNK_BYTES)
        except OSError:
            chunk = b""
        if not chunk:
            break
        events = framer.feed(chunk)
        for event in events:
            yield event
            if isinstance(event, mcp_transport.FrameFailure):
                return
    yield from framer.finish()


class Supervisor:
    """单实例监督器：run() 阻塞直至 server 真退出，返回其退出码。"""

    def __init__(
        self,
        *,
        idempotent_tools: frozenset[str] = _DEFAULT_IDEMPOTENT_TOOLS,
    ) -> None:
        self._protocol = _SupervisorProtocolState(
            idempotent_tools=idempotent_tools,
        )
        self._state_lock = threading.Lock()
        # Child generation selection/writes and protocol bookkeeping use
        # different locks.  A full child stdin pipe must never prevent the
        # response pump from preparing and acknowledging a response.
        self._child_lock = threading.Lock()
        self._out_lock = threading.Lock()
        self._child: subprocess.Popen | None = None
        self._child_generation = 0
        self._client_eof = threading.Event()
        self._client_disconnected = threading.Event()
        self._pump_failed = threading.Event()
        self._protocol_failed = threading.Event()
        self._response_readers: dict[int, _BoundedResponseReader] = {}
        self._auto_install_suppressed = False
        self._initial_handshake_lock = threading.Lock()
        self._initial_handshake_watchdog_started = False
        self._initial_handshake_finished = threading.Event()

    def _spawn(self) -> subprocess.Popen:
        # 要点③：换芯重启不经 launcher.main，卸载标记清理在此接线；必须先清理再选
        # 解释器——删 home 连带清掉就绪哨兵，_server_cmd 才会安全落回 bootstrap。
        try:
            pending_uninstall = os.path.lexists(uninstall.uninstall_marker())
        except (OSError, RuntimeError, ValueError):
            pending_uninstall = True
        uninstall_converged = run_pending_uninstall()
        if pending_uninstall or not uninstall_converged:
            self._auto_install_suppressed = True
        selection = _server_selection(allow_runtime=uninstall_converged)
        env = {**os.environ, "VIBECAD_SUPERVISED": "1"}  # I4：告知 server 自杀有人重启
        if self._auto_install_suppressed:
            # The inherited MCPB manifest normally enables bootstrap install.
            # A confirmed uninstall owns the rest of this supervisor session,
            # whether cleanup converged or remains a recovery boundary.  Only
            # an explicit ensure_runtime call may reinstall after this latch.
            env["VIBECAD_AUTO_INSTALL"] = "0"
        if selection.uses_runtime:
            from vibecad.runtime import status

            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env = status.freecad_process_environment(env)
        return subprocess.Popen(
            list(selection.command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,  # stderr 继承，直通宿主日志
            env=env,
        )

    def _pump_panic(
        self,
        what: str,
        *,
        child: subprocess.Popen | None = None,
    ) -> None:
        """Fail loudly without retaining or rendering caller/exception data."""

        label = what if what in {"client→child", "child→client"} else "transport"
        print(
            f"vibecad-supervisor: {label} pump_failed; child terminated",
            file=sys.stderr,
        )
        self._pump_failed.set()
        ch = self._child if child is None else child
        if ch is not None:
            self._kill_child(ch)

    def _write_client_payload(self, payload: bytes) -> bool:
        if type(payload) is not bytes or len(payload) > mcp_transport.MAX_RESPONSE_FRAME_BYTES:
            raise _ResponseProtocolError
        if self._client_disconnected.is_set():
            return False
        with self._out_lock:
            if self._client_disconnected.is_set():
                return False
            try:
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.write(b"\n")
                sys.stdout.buffer.flush()
            except (OSError, ValueError):
                self._client_disconnected.set()
                return False
        return True

    def _write_client_response(self, response: dict[str, object]) -> bool:
        return self._write_client_payload(_json_bytes(response))

    @staticmethod
    def _write_child_payload(ch: subprocess.Popen, payload: bytes) -> bool:
        if type(payload) is not bytes or len(payload) > mcp_transport.MAX_REQUEST_FRAME_BYTES:
            return False
        if ch.stdin is None:
            return False
        try:
            ch.stdin.write(payload)
            ch.stdin.write(b"\n")
            ch.stdin.flush()
        except (OSError, ValueError):
            return False
        return True

    @staticmethod
    def _child_exited(ch: subprocess.Popen) -> bool:
        try:
            return ch.poll() is not None
        except BaseException:
            return False

    def _child_exited_after_stdout_eof(self, ch: subprocess.Popen) -> bool:
        deadline = time.monotonic() + _CHILD_STDOUT_EOF_GRACE_SECONDS
        while True:
            if self._child_exited(ch):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._child_exited(ch)
            time.sleep(min(0.01, remaining))

    @staticmethod
    def _kill_child(ch: subprocess.Popen) -> None:
        try:
            killer = getattr(ch, "kill", None)
            if callable(killer):
                killer()
        except BaseException:
            pass

    def _write_child_payload_until(
        self,
        ch: subprocess.Popen,
        payload: bytes,
        *,
        deadline: float,
    ) -> _ChildWriteResult:
        """Write one frame without a blocking writer thread.

        Production ``Popen.stdin`` exposes a file descriptor.  Temporarily using
        non-blocking ``os.write`` plus ``select`` makes the absolute deadline
        authoritative and leaves no writer thread to outlive a killed generation.
        In-memory test streams have no descriptor and are synchronously bounded by
        their implementation.
        """

        if type(payload) is not bytes or len(payload) > mcp_transport.MAX_REQUEST_FRAME_BYTES:
            return _ChildWriteResult.FAILED
        stream = ch.stdin
        if stream is None:
            return _ChildWriteResult.FAILED
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            try:
                return (
                    _ChildWriteResult.SUCCESS
                    if self._write_child_payload(ch, payload)
                    else _ChildWriteResult.FAILED
                )
            except BaseException:
                return _ChildWriteResult.FAILED
        try:
            was_blocking = os.get_blocking(fd)
            os.set_blocking(fd, False)
        except (OSError, TypeError, ValueError):
            return _ChildWriteResult.FAILED
        frame = payload + b"\n"
        view = memoryview(frame)
        offset = 0
        try:
            while offset < len(view):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_child(ch)
                    return _ChildWriteResult.TIMED_OUT
                try:
                    written = os.write(fd, view[offset:])
                except BlockingIOError:
                    try:
                        _readable, writable, _exceptional = select.select(
                            [],
                            [fd],
                            [],
                            remaining,
                        )
                    except (OSError, ValueError):
                        return _ChildWriteResult.FAILED
                    if not writable:
                        self._kill_child(ch)
                        return _ChildWriteResult.TIMED_OUT
                    continue
                except InterruptedError:
                    continue
                except OSError:
                    return _ChildWriteResult.FAILED
                if type(written) is not int or written <= 0:
                    return _ChildWriteResult.FAILED
                offset += written
            return _ChildWriteResult.SUCCESS
        finally:
            with contextlib.suppress(OSError, TypeError, ValueError):
                os.set_blocking(fd, was_blocking)

    @staticmethod
    def _report_child_write_timeout() -> None:
        print(
            "vibecad-supervisor: child stdin write timed out; child terminated",
            file=sys.stderr,
        )

    @staticmethod
    def _report_child_write_failure() -> None:
        print(
            "vibecad-supervisor: child stdin write failed; child terminated",
            file=sys.stderr,
        )

    def _generation_is_current(
        self,
        ch: subprocess.Popen | None,
        generation: int,
    ) -> bool:
        with self._child_lock:
            return self._child is ch and self._child_generation == generation

    def _arm_initial_handshake_deadline(
        self,
        ch: subprocess.Popen,
        generation: int,
        *,
        deadline: float,
    ) -> None:
        """Terminate a first-generation initialize that misses one absolute bound."""

        with self._initial_handshake_lock:
            if self._initial_handshake_watchdog_started:
                return
            self._initial_handshake_watchdog_started = True

        def _watch() -> None:
            if self._initial_handshake_finished.wait(max(0.0, deadline - time.monotonic())):
                return
            if (
                self._client_eof.is_set()
                or self._pump_failed.is_set()
                or self._protocol_failed.is_set()
                or not self._generation_is_current(ch, generation)
            ):
                return
            print(
                "vibecad-supervisor: 首次握手超时；child terminated",
                file=sys.stderr,
            )
            self._protocol_failed.set()
            self._kill_child(ch)

        threading.Thread(
            target=_watch,
            name="vibecad-initial-handshake-watchdog",
            daemon=True,
        ).start()

    def _join_response_pump(self, pump: threading.Thread) -> bool:
        pump.join(_CHILD_STDOUT_DRAIN_SECONDS)
        if not pump.is_alive():
            return True
        print(
            "vibecad-supervisor: child stdout drain timed out; response pump abandoned",
            file=sys.stderr,
        )
        self._pump_failed.set()
        return False

    def _response_reader(self, ch: subprocess.Popen) -> _BoundedResponseReader:
        key = id(ch)
        reader = self._response_readers.get(key)
        if reader is None:
            reader = _BoundedResponseReader()
            self._response_readers[key] = reader
        return reader

    def _client_to_child(self) -> None:
        try:
            self._pump_client_lines()
        except Exception:  # noqa: BLE001 - fixed top-level transport failure
            self._pump_panic("client→child")

    def _pump_client_lines(self) -> None:
        """Bound, validate, admit and forward host frames without raw reflection."""

        fatal = False
        for event in _stdin_frames():
            if isinstance(event, mcp_transport.FrameFailure):
                self._write_client_response(event.response)
                fatal = event.close
                break
            try:
                with self._child_lock:
                    with self._state_lock:
                        action = self._protocol.accept(event.payload)
                        first_initialize = (
                            action.kind is _ClientActionKind.FORWARD
                            and action.payload is not None
                            and self._protocol.handshake_frame_count == 1
                            and not self._protocol.initialize_response_complete
                            and action.payload == self._protocol.handshake_payloads[0]
                        )
                    ch = self._child
                    generation = self._child_generation
            except mcp_transport.TransportProtocolError as error:
                self._write_client_response(error.response)
                if error.close:
                    fatal = True
                    break
                continue
            if action.kind is _ClientActionKind.FORWARD and action.payload is not None:
                deadline = time.monotonic() + (
                    _HANDSHAKE_TIMEOUT_SECONDS
                    if first_initialize
                    else _INGRESS_WRITE_TIMEOUT_SECONDS
                )
                if first_initialize and ch is not None:
                    self._arm_initial_handshake_deadline(
                        ch,
                        generation,
                        deadline=deadline,
                    )
                if ch is None:
                    write_result = _ChildWriteResult.FAILED
                else:
                    write_result = self._write_child_payload_until(
                        ch,
                        action.payload,
                        deadline=deadline,
                    )
                if write_result is not _ChildWriteResult.SUCCESS and self._generation_is_current(
                    ch,
                    generation,
                ):
                    write_failed = write_result is _ChildWriteResult.TIMED_OUT
                    if write_failed:
                        self._report_child_write_timeout()
                    elif ch is None or not self._child_exited(ch):
                        self._report_child_write_failure()
                        if ch is not None:
                            self._kill_child(ch)
                        write_failed = True
                    if write_failed:
                        self._protocol_failed.set()
                        fatal = True
                        break
            if action.response is not None:
                self._write_client_response(action.response)
            if action.close:
                fatal = True
                break

        if fatal:
            self._protocol_failed.set()
        self._client_eof.set()
        with self._child_lock:
            ch = self._child
            if ch and ch.stdin:
                with contextlib.suppress(OSError, ValueError):
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
            if not self._child_exited_after_stdout_eof(ch):
                self._pump_panic("child→client", child=ch)
        except Exception:  # noqa: BLE001 - fixed top-level transport failure
            self._pump_panic("child→client", child=ch)
        finally:
            # A healthy installation can perform more than the rapid-swap
            # guard's short window over a long-lived host session.  Reader
            # state belongs to exactly one child generation; do not retain a
            # dead generation forever.
            self._response_readers.pop(id(ch), None)

    def _pump_child_lines(self, ch: subprocess.Popen) -> None:
        """Bound and validate child responses before forwarding and acknowledgement."""

        if ch.stdout is None:
            raise _ResponseProtocolError
        reader = self._response_reader(ch)
        while True:
            payload = reader.read_frame(ch.stdout)
            if payload is None:
                return
            with self._state_lock:
                prepared = self._protocol.prepare_response(payload)
                if prepared.initialize_response:
                    if prepared.forward:
                        self._write_client_payload(prepared.payload)
                    self._protocol.acknowledge_response(prepared)
                    self._initial_handshake_finished.set()
                    continue
            if prepared.forward:
                self._write_client_payload(prepared.payload)
            with self._state_lock:
                self._protocol.acknowledge_response(prepared)

    def _replay_handshake(self, ch: subprocess.Popen) -> bool:
        """向新子进程重放 initialize/initialized；其 initialize 响应丢弃（客户端持有
        的是首次握手响应，重复响应会被客户端视为协议错误）。

        返回 False = 新子进程在 _HANDSHAKE_TIMEOUT_SECONDS 内既不响应也不退出
        （conda 首启慢/杀毒扫描/import 死锁，C2），已被强杀——调用方按换芯失败以
        非零码收尾，绝不让宿主全部请求无限挂死。新子进程秒死（写入静默失败、
        stdout 秒 EOF）返回 True：run() 主循环随后如实拿到真退出码并透传。"""
        handshake = self._protocol.handshake_payloads
        init_id = self._protocol.init_id
        if not handshake:
            return True
        deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS
        initial_write = self._write_child_payload_until(
            ch,
            handshake[0],
            deadline=deadline,
        )
        if initial_write is _ChildWriteResult.TIMED_OUT:
            self._report_child_write_timeout()
            return False
        if initial_write is _ChildWriteResult.FAILED:
            if self._child_exited(ch):
                return True
            self._kill_child(ch)
            return False
        done = threading.Event()
        received = threading.Event()
        failed = threading.Event()

        def _drain() -> None:
            try:
                if ch.stdout is None:
                    raise _ResponseProtocolError
                reader = self._response_reader(ch)
                while True:
                    payload = reader.read_frame(ch.stdout)
                    if payload is None:
                        return
                    message = _decode_response_payload(payload)
                    if _is_server_notification(message):
                        self._write_client_payload(payload)
                        continue
                    response_key = _request_id_key(_validated_initialize_response_id(message))
                    init_key = _request_id_key(init_id)
                    if response_key != init_key:
                        raise _ResponseProtocolError
                    received.set()
                    return
            except Exception:  # noqa: BLE001 - converted to one fixed failure
                failed.set()
            finally:
                done.set()

        t = threading.Thread(target=_drain, daemon=True)
        t.start()
        t.join(max(0.0, deadline - time.monotonic()))
        if not done.is_set():
            print(
                "vibecad-supervisor: 换芯握手超时"
                f"（{_HANDSHAKE_TIMEOUT_SECONDS:.0f}s 内新子进程无响应），"
                "强杀新子进程并按换芯失败退出",
                file=sys.stderr,
            )
            self._kill_child(ch)
            return False
        if failed.is_set():
            self._kill_child(ch)
            return False
        if not received.is_set():
            if self._child_exited(ch):
                return True
            self._kill_child(ch)
            return False
        for extra in handshake[1:]:
            extra_write = self._write_child_payload_until(
                ch,
                extra,
                deadline=deadline,
            )
            if extra_write is _ChildWriteResult.SUCCESS:
                continue
            if extra_write is _ChildWriteResult.TIMED_OUT:
                self._report_child_write_timeout()
                return False
            if self._child_exited(ch):
                return True
            self._kill_child(ch)
            return False
        return True

    def run(self) -> int:
        self._child = self._spawn()
        self._child_generation = 1
        threading.Thread(target=self._client_to_child, daemon=True).start()
        rapid_swaps = 0
        last_swap: float | None = None
        ch = self._child
        pump = threading.Thread(target=self._child_to_client, args=(ch,), daemon=True)
        pump.start()
        while True:
            code = ch.wait()
            if not self._join_response_pump(pump):
                return 1
            if self._pump_failed.is_set():
                return 1  # 泵线程崩溃（C3）：子进程已被强杀，响亮退出
            if self._protocol_failed.is_set():
                return 1
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
                print(
                    f"vibecad-supervisor: 检测到换芯循环（连续 {rapid_swaps} 次 "
                    f"SWAP_EXIT 间隔 < {_SWAP_LOOP_WINDOW_SECONDS:.0f}s），停止换芯并以"
                    f"退出码 {code} 结束；请检查运行时完整性（如就绪哨兵在而 conda "
                    "python 缺失）",
                    file=sys.stderr,
                )
                return code
            with self._child_lock:
                with self._state_lock:
                    plan = self._protocol.plan_swap()
                for unknown in plan.unknown_outcomes:
                    self._write_client_response(unknown.payload)
                    with self._state_lock:
                        self._protocol.acknowledge(unknown.request_id)

                new = self._spawn()  # 此刻就绪哨兵已在 → conda python
                self._child = new
                self._child_generation += 1
                if not self._replay_handshake(new):
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        new.wait(timeout=_TERM_KILL_GRACE_SECONDS)
                    return 1

                # Start draining the new generation before writing replay work.
                # A child may produce a maximum-size first response before it
                # reads the remaining requests; without this concurrent pump,
                # its stdout and our stdin writes can fill both pipes and lock
                # the swap forever.
                next_pump = threading.Thread(
                    target=self._child_to_client,
                    args=(new,),
                    daemon=True,
                )
                next_pump.start()
                replay_deadline = time.monotonic() + _HANDSHAKE_TIMEOUT_SECONDS
                generation_died = False
                for payload in (*plan.replay_payloads, *plan.cancellation_payloads):
                    write_result = self._write_child_payload_until(
                        new,
                        payload,
                        deadline=replay_deadline,
                    )
                    if write_result is _ChildWriteResult.SUCCESS:
                        continue
                    if write_result is _ChildWriteResult.FAILED and self._child_exited(new):
                        generation_died = True
                        break
                    if write_result is _ChildWriteResult.TIMED_OUT:
                        self._report_child_write_timeout()
                    self._kill_child(new)
                    with contextlib.suppress(
                        OSError,
                        TypeError,
                        ValueError,
                        subprocess.TimeoutExpired,
                    ):
                        new.wait(timeout=_TERM_KILL_GRACE_SECONDS)
                    next_pump.join(_TERM_KILL_GRACE_SECONDS)
                    return 1
                if self._client_eof.is_set() and new.stdin:
                    # EOF 恰在换芯中到达（关的是旧子进程 stdin）：补关新子进程 stdin
                    # 令其随即收尾，并同样挂宽限回收（I2）——下一轮 wait() 以真退出码
                    # 结束，不守规矩的新子进程也绝不成孤儿。
                    with contextlib.suppress(OSError):
                        new.stdin.close()
                    self._reap_after_grace(new)
                ch = new
                pump = next_pump
                if generation_died:
                    continue
