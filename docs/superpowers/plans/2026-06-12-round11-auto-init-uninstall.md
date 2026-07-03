# Round 11 — 安装初始化全自动 + 干净卸载 + 视觉落盘 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 .mcpb 装好之后的体验修成"零手工、零重连、Cowork 能看图、Remove 即全删"。

**Architecture:** ① manifest `mcp_config.env` 两行（`VIBECAD_AUTO_INSTALL=1` 自动装 + `VIBECAD_HOME=${__dirname}/runtime` 运行时进扩展目录 → Remove extension 连带全删）；② 换芯：bootstrap server 检测就绪后自退，spike 先验方案 D（宿主自动重启），不达标上方案 C（launcher 升级为监督进程：stdio 逐行透传 + exit 75 换 conda python 重启 + 重放 initialize 握手）；③ 卸载：标记文件 + 自退 + 重启后 bootstrap 执行删除（全平台一致避 Windows 文件锁），辅助 MCP 工具两段式确认 + CLI 救援；④ 每步工程图落盘 `view_file` + `render_part(save_to)`。

**Tech Stack:** 纯 stdlib（supervisor/uninstall/persist）、FastMCP、mcpb CLI 2.1.2。

**Spec:** `docs/superpowers/specs/2026-06-12-round11-auto-init-uninstall-design.md`

**模型分配（用户指定）：** Task 0 / Task 3（supervisor 协议代码）→ opus/fable 级；其余任务 → sonnet 级。

---

## File Structure

```
manifest.json                     改  env 两行 + uninstall_runtime 工具 + 版本 0.3.0
src/vibecad/
├── launcher.py                   改  D：开头 perform_pending_uninstall + --uninstall CLI
│                                     C：main 委托 supervisor.main（入口签名不变）
├── supervisor.py                 新  （仅 C 分支）监督进程：透传/换芯/握手重放，纯 stdlib
├── server.py                     改  _schedule_swap 自退钩子、guard 文案带进度、
│                                     needs_reconnect 恒 false、uninstall_runtime 工具、
│                                     _attach_view 落盘 view_file、render_part save_to
├── runtime/uninstall.py          新  标记/直删/护栏，纯 stdlib
├── feedback/persist.py           新  save_view 落盘 + 滚动保留 20 张
└── __init__.py                   改  0.3.0
pyproject.toml                    改  0.3.0
tests/
├── test_persist.py               新
├── test_uninstall.py             新
├── test_supervisor.py            新  （仅 C 分支）假 server 换芯+重放
├── fake_server.py                新  （仅 C 分支）测试夹具
├── test_server_round11.py        新  guard 文案/两段式卸载/save_to/view_file
├── test_mcpb_manifest.py         改  工具数 23
└── test_runtime_integration.py   改  追加换芯端到端慢测
docs/  USER_GUIDE / ACCEPTANCE_TESTS / DIRECTORY_SUBMISSION / README 改版
.vibecad/spike-r11/               新  spike 扩展（不进发布包）
```

---

## Task 0：Spike 硬门——真机三验（模型：opus/fable）

**目的：** 一个 spike 扩展同时回答三个问题，结论决定后续分支：
- **Q1**：`${__dirname}` 在 `mcp_config.env` 值中是否真机展开？（否 → manifest 不用 `VIBECAD_HOME=${__dirname}/runtime`，卸载退回两步式）
- **Q2（D 合格线）**：server 自退后宿主是否自动重启进程，同会话工具调用无任何手工操作即成功？（是 → Task 3 走 D 分支 20 行；否 → C 分支监督进程）
- **Q3**：扩展升级（装更高版本 .mcpb）时扩展目录内的数据文件是否保留？（否 → 升级即重下 2-3GB，需在手册警示并权衡 Q1 方案）

**Files:** Create: `.vibecad/spike-r11/manifest.json`, `.vibecad/spike-r11/spike_server.py`, `.vibecad/spike-r11/pyproject.toml`, `.vibecad/spike-r11/.mcpbignore`

- [ ] **Step 1: 写 spike server**（极小 FastMCP，3 个工具）

```python
# .vibecad/spike-r11/spike_server.py
"""R11 spike：验 env 展开 / 自退宿主行为 / 升级数据保留。"""
import os
import sys
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("vibecad-spike-r11")
MARK = Path(os.environ.get("VIBECAD_HOME", "/tmp/spike-no-env")) / "persist.mark"


@mcp.tool()
def spike_env() -> dict:
    """回显 env 展开结果与进程身份（Q1 + 复活确认）。"""
    return {
        "VIBECAD_HOME": os.environ.get("VIBECAD_HOME", "<未设置>"),
        "AUTO": os.environ.get("VIBECAD_AUTO_INSTALL", "<未设置>"),
        "pid": os.getpid(),
        "exe": sys.executable,
        "version": "0.0.1",   # 升级验证时改 0.0.2
    }


@mcp.tool()
def spike_mark() -> dict:
    """在 VIBECAD_HOME 写标记文件（Q3 升级保留验证）。"""
    MARK.parent.mkdir(parents=True, exist_ok=True)
    MARK.write_text("r11")
    return {"marked": str(MARK)}


@mcp.tool()
def spike_check_mark() -> dict:
    """读标记（升级后调：在 → 数据保留；不在 → 升级清目录）。"""
    return {"exists": MARK.exists(), "path": str(MARK)}


@mcp.tool()
def spike_exit() -> str:
    """1 秒后自退 exit(0)（Q2：宿主重启行为）。"""
    threading.Timer(1.0, os._exit, args=(0,)).start()
    return "将于 1 秒后自退，请继续调用 spike_env 看进程是否自动复活"


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 2: 写 spike manifest**（要点：`env` 带 `${__dirname}`；其余仿正式 manifest 的 uv 配置）

```json
{
  "manifest_version": "0.4",
  "name": "vibecad-spike-r11",
  "display_name": "VibeCAD Spike R11",
  "version": "0.0.1",
  "description": "R11 spike: env expansion / self-exit / upgrade persistence",
  "author": { "name": "Wang Tao", "email": "wangtao9090@gmail.com" },
  "server": {
    "type": "uv",
    "entry_point": "spike_server.py",
    "mcp_config": {
      "command": "uv",
      "args": ["run", "--directory", "${__dirname}", "spike_server.py"],
      "env": {
        "VIBECAD_AUTO_INSTALL": "1",
        "VIBECAD_HOME": "${__dirname}/runtime"
      }
    }
  },
  "compatibility": { "platforms": ["darwin"], "runtimes": { "python": ">=3.12" } }
}
```

pyproject.toml 仅依赖 `mcp>=1.2`；`.mcpbignore` 排除 `*.mcpb`。

- [ ] **Step 3: 打包两个版本** — `npx -y @anthropic-ai/mcpb@2.1.2 validate` + `pack` 出 `SpikeR11-0.0.1.mcpb`；把 manifest/spike_env 的 version 改 0.0.2 再 pack 出 `SpikeR11-0.0.2.mcpb`。
- [ ] **Step 4: 用户真机执行**（给用户清晰操作卡，**不得代操作用户本机配置**）：
  1. 装 0.0.1 → 调 `spike_env`：记录 VIBECAD_HOME 是否为扩展目录绝对路径（**Q1**）、pid。
  2. 调 `spike_mark` → 调 `spike_exit` → 等 5 秒 → 再调 `spike_env`：是否无手工操作成功返回新 pid（**Q2**）。
  3. 双击装 0.0.2（升级）→ 调 `spike_check_mark`：标记是否还在（**Q3**）+ `spike_env` 确认 version=0.0.2。
- [ ] **Step 5: 结论落盘** — 三个 Q 的结论写进本计划"Spike 结果"节并 commit；按结论锁定 Task 3 分支与 Task 5 manifest 内容。**硬门：Q2 无结论不得进 Task 3。**

### Spike 结果（执行后回填）

> **2026-07-02 文档级预判**（证据：mcpb 官方 MANIFEST.md + 本机 Claude Desktop 1.17377.2 app.asar 反编译源码 + 社区 issues；真机三验为确认性验证，操作卡见 `2026-07-02-r11-spike-user-steps.md`。真机若推翻任一条，按本节条件分支回退）。

- Q1 `${__dirname}` env 展开：**是（✅ 2026-07-03 真机确认）**——真机 spike_env 返回 `VIBECAD_HOME=/Users/…/Claude Extensions/local.mcpb.wang-tao.vibecad-spike-r11/runtime` 绝对路径，非字面量。（预判依据：宿主替换函数对 mcp_config 整个对象递归替换，env 与 args 同一路径无字段区分。已知平台风险：Windows MSIX 版有"展开出错路径"bug（Windows-MCP#209），Windows 验收时注意。）
- Q2 自退后宿主行为（D 合格线）：**否（中高置信，真机补最后一步中）→ 锁定 C 分支（监督进程）**——宿主源码 onclose 意外退出路径仅记日志+状态 Failed+toast，无任何 relaunch；auto-reconnect 机制只覆盖宿主主动 shutdown（设置变更/升级/卸载）场景；claude-code#61052/#54136/#59274 三个独立 issue 一致证实。真机注：首轮观察到的 pid 42359→42630 复活伴随 version 0.0.1→0.0.2，属升级触发的宿主主动重启（auto-reconnect 路径），不构成自退复活证据；纯自退判定（0.0.2 内 spike_exit→等 5s→spike_env）待用户补测。**无论补测结果如何均不影响实现：C 分支 supervisor 不依赖宿主重启行为。**
- Q3 升级数据保留：**否（✅ 2026-07-03 真机确认）**——0.0.1 写入的 persist.mark 升级 0.0.2 后 exists:false，且 path 与升级前逐字相同（扩展 ID 未变、目录未漂移），排除"目录漂移"干扰项，确系宿主升级时整目录清空重建。**结论（已落实现）：VIBECAD_HOME 不进 manifest env（放弃 `${__dirname}/runtime` 方案），运行时保持默认的扩展目录外路径；Task 5 env 只留 VIBECAD_AUTO_INSTALL。**

**C 分支实现要点（调研派生，实现时必须落地）**：①监督进程在宿主关闭（stdin EOF/transport close）时必须连同子进程干净退出——升级流程是先 shutdown server 再删目录，supervisor 残留会持有已删目录句柄成孤儿进程；②宿主主动重启场景（升级/设置变更）已有 auto-reconnect，supervisor 不自行处理；③**卸载 marker 清理接线**：supervisor 重启子进程不经 launcher.main，须在每次 spawn 子进程前调用 `uninstall.perform_pending_uninstall()`（纯 stdlib 合规），否则 uninstall_runtime 两段式的"重启后清理"在 C 分支下悬空。

---

## Task 1：视觉落盘 `feedback/persist.py`（模型：sonnet）

**Files:** Create: `src/vibecad/feedback/persist.py`, `tests/test_persist.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_persist.py
"""save_view：落盘/序号递增/滚动清理/文件名消毒。不依赖 FreeCAD。"""
from vibecad.feedback import persist

PNG = b"\x89PNG\r\n\x1a\nfake"


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))


def test_save_view_writes_and_increments(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    p1 = persist.save_view(PNG, "Demo", "add_box")
    p2 = persist.save_view(PNG, "Demo", "add_hole")
    assert p1.endswith("001-add_box.png") and p2.endswith("002-add_hole.png")
    assert (tmp_path / "views" / "Demo" / "001-add_box.png").read_bytes() == PNG


def test_save_view_rolls_old_files(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    for i in range(25):
        persist.save_view(PNG, "Doc", f"t{i}")
    files = sorted((tmp_path / "views" / "Doc").glob("*.png"))
    assert len(files) == 20 and files[0].name.startswith("006-")


def test_save_view_sanitizes_names(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    p = persist.save_view(PNG, "a/b:c", "x y")
    assert "/views/a_b_c/001-x_y.png" in p.replace("\\", "/")
```

- [ ] **Step 2: 跑测试确认失败** — `uv run pytest tests/test_persist.py -v` → FAIL（模块不存在）
- [ ] **Step 3: 实现**

```python
# src/vibecad/feedback/persist.py
"""每步工程图落盘：Cowork 等客户端不向用户渲染 ImageContent（2026-06-12 真机实证），
返回里的 view_file 路径是用户看图的通道（AI 可 `open <path>` 弹图）。纯 stdlib。"""
from __future__ import annotations

import re
from pathlib import Path

_KEEP = 20


def _sanitize(name: str) -> str:
    return re.sub(r"[^\w.-]", "_", name)[:64] or "untitled"


def views_dir(doc_name: str) -> Path:
    from vibecad.runtime import paths  # noqa: PLC0415 - 懒加载，保 feedback 包导入轻

    return paths.vibecad_home() / "views" / _sanitize(doc_name)


def save_view(png: bytes, doc_name: str, tool: str) -> str:
    """写 <home>/views/<doc>/<NNN>-<tool>.png，滚动保留最近 _KEEP 张，返回绝对路径。"""
    d = views_dir(doc_name)
    d.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in d.glob("*.png")
            if (m := re.match(r"(\d{3})-", p.name))]
    path = d / f"{max(nums, default=0) + 1:03d}-{_sanitize(tool)}.png"
    path.write_bytes(png)
    for old in sorted(d.glob("*.png"))[:-_KEEP]:
        old.unlink(missing_ok=True)
    return str(path)
```

- [ ] **Step 4: 测试过 + ruff** — `uv run pytest tests/test_persist.py -v && uv run ruff check .`
- [ ] **Step 5: Commit** — `feat(feedback): persist per-step view PNGs (Cowork doesn't render inline images)`

---

## Task 2：server 集成落盘——`_attach_view` view_file + `render_part(save_to)`（模型：sonnet）

**Files:** Modify: `src/vibecad/server.py`; Create: `tests/test_server_round11.py`

- [ ] **Step 1: 失败测试**（mock 模式参照 `tests/test_server_new_tools.py` 既有 monkeypatch 范式：patch `server._installer.is_ready`→True、`server._in_conda_runtime`→True、`server._multiview.render_multiview`→假 PNG+空表）

```python
# tests/test_server_round11.py 第一批
def test_attach_view_includes_view_file(monkeypatch, tmp_path):
    """成功附图时 result 带落盘绝对路径；文件真实存在。"""
    # ... 既有范式 mock 后调 server.add_box(10,10,10)
    result, image = out
    assert result["view_file"].endswith(".png")
    assert Path(result["view_file"]).exists()


def test_attach_view_persist_failure_not_fatal(monkeypatch):
    """落盘抛 OSError → 操作仍成功，带 view_file_error，Image 仍返回。"""


def test_render_part_save_to(monkeypatch, tmp_path):
    """render_part(save_to=...) → 文件写入 + 返回含 saved 字段。"""
```

- [ ] **Step 2: 确认失败** — `uv run pytest tests/test_server_round11.py -v` → FAIL
- [ ] **Step 3: 实现** — 三处改动：

①`_attach_view` 签名加 `tool: str`，渲染成功后落盘（不连坐）：

```python
def _attach_view(result: dict[str, Any], tool: str = "step") -> Any:
    ...  # png/table 产出后、return 前插入：
        try:
            doc_name = getattr(_session.doc, "Name", "untitled")
            result["view_file"] = _persist.save_view(png, doc_name, tool)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不连坐（与 render_error 同理）
            result["view_file_error"] = f"图已生成但落盘失败：{exc}"
        result["labels"] = table
        return [result, Image(data=png, format="png")]
```

16 处调用点逐一带上工具名：`return _attach_view(result, tool="add_box")` 等（add_box/add_cylinder/boolean_cut/add_hole/fillet_edges/chamfer_edges/move_part/rotate_part/extrude_profile/modify_part/new_part/set_active_part/place_part/align_parts…按现有调用点实名）。顶部 `from vibecad.feedback import persist as _persist`。

② `render_part` 加参数 `save_to: str | None = None`；三条成功路径（普通/multi/annotate）产出 png 后统一：

```python
def _maybe_save(png: bytes, save_to: str | None) -> dict[str, str]:
    if not save_to:
        return {}
    try:
        p = Path(save_to).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png)
        return {"saved": str(p)}
    except OSError as exc:
        return {"save_error": f"保存失败：{exc}"}
```

普通路径返回 `Image` → save_to 时改返回 `[Image, json.dumps({"ok": True, **saved})]`；multi/annotate 路径把 `**saved` 并进已有 dict。

③ 工具描述更新（教 AI 用路径）：`render_part` docstring 末尾加"save_to=绝对路径 可另存 PNG；每步自动附图的返回含 view_file 落盘路径——客户端不显示内嵌图时（如 Cowork），可直接 `open <view_file>` 给用户弹图"。

- [ ] **Step 4: 全量快测过 + ruff** — `uv run pytest -q && uv run ruff check .`（manifest 工具描述一致性测试若卡，同步改 manifest.json 中 render_part 的 description——见 Task 5 一并校验）
- [ ] **Step 5: Commit** — `feat(server): view_file on every step + render_part save_to`

---

## Task 3：换芯（按 Spike Q2 结论二选一；模型：opus/fable）

**共用部分（D/C 都要）：server 自退钩子。**

**Files:** Modify: `src/vibecad/server.py`; (C 分支) Create: `src/vibecad/supervisor.py`, `tests/test_supervisor.py`, `tests/fake_server.py`; Modify: `src/vibecad/launcher.py`

- [ ] **Step 1: server 自退钩子 + 失败测试**

```python
# tests/test_server_round11.py 追加
def test_schedule_swap_idempotent(monkeypatch):
    """ready+bootstrap 时安排一次延迟自退；重复调用不叠 Timer。"""
    timers = []
    monkeypatch.setattr(server.threading, "Timer",
                        lambda d, f, args=(): timers.append((d, f, args)) or _FakeTimer())
    server._swap_timer = None
    server._schedule_swap()
    server._schedule_swap()
    assert len(timers) == 1


def test_runtime_status_triggers_swap_when_ready_in_bootstrap(monkeypatch):
    """get_runtime_status：ready 且非 conda → needs_reconnect 恒 False + 已安排自退。"""
```

实现（server.py）：

```python
SWAP_EXIT = 75  # C 分支改为 from vibecad.supervisor import SWAP_EXIT；D 分支用 0
_swap_timer: threading.Timer | None = None


def _schedule_swap(delay: float = 1.0) -> None:
    """运行时就绪但本进程仍是引导解释器 → 延迟自退（给当前响应 flush 留时间）。
    幂等：只安排一次。D：宿主自动重启 → launcher re-exec 进 conda；
    C：监督进程见 SWAP_EXIT 即换 conda python 重启子进程并重放握手。"""
    global _swap_timer
    if _swap_timer is not None:
        return
    _swap_timer = threading.Timer(delay, os._exit, args=(SWAP_EXIT,))
    _swap_timer.daemon = True
    _swap_timer.start()
```

触发点四处：`get_runtime_status`（ready+bootstrap 分支）、`_ensure_runtime_impl`（ready 分支）、`_runtime_guard`（ready+bootstrap 分支）、`_safe_install` 安装成功结束后（**用户全程不开口也自动换芯**）。`needs_reconnect` 字段改恒 `False`。

- [ ] **Step 2: （仅 C 分支）supervisor 失败测试**

```python
# tests/fake_server.py
"""假 server：行式 JSON-RPC echo。首次启动自报 generation=1，被换芯重启后 generation=2
（用 VIBECAD_FAKE_GEN_FILE 计数文件区分）。收到 {"method":"swap"} 通知 → exit(75)。"""
import json
import os
import sys
from pathlib import Path

gen_file = Path(os.environ["VIBECAD_FAKE_GEN_FILE"])
gen = int(gen_file.read_text()) + 1 if gen_file.exists() else 1
gen_file.write_text(str(gen))

for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "swap":
        os._exit(75)
    if "id" in msg:
        out = {"jsonrpc": "2.0", "id": msg["id"],
               "result": {"gen": gen, "method": msg.get("method")}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
```

```python
# tests/test_supervisor.py
"""监督进程黑盒：subprocess 起 supervisor（VIBECAD_SUPERVISOR_TEST_CMD 注入假 server），
验证透传、换芯重启、initialize 重放、换芯后请求继续成功。"""
def test_passthrough_and_swap(tmp_path):
    env = {**os.environ,
           "VIBECAD_SUPERVISOR_TEST_CMD": json.dumps(
               [sys.executable, "tests/fake_server.py"]),
           "VIBECAD_FAKE_GEN_FILE": str(tmp_path / "gen")}
    sup = subprocess.Popen([sys.executable, "-m", "vibecad"],  # launcher→supervisor
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    def rpc(id_, method):
        sup.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id_, "method": method})
                        .encode() + b"\n")
        sup.stdin.flush()
        return json.loads(sup.stdout.readline())

    sup.stdin.write(b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}\n')
    sup.stdin.flush()
    assert json.loads(sup.stdout.readline())["result"]["gen"] == 1
    sup.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')

    assert rpc(1, "tools/call")["result"]["gen"] == 1          # 换芯前
    sup.stdin.write(b'{"jsonrpc":"2.0","method":"swap"}\n')    # 触发 exit(75)
    sup.stdin.flush()
    assert rpc(2, "tools/call")["result"]["gen"] == 2          # 换芯后零感知成功
    # 客户端从未见到第二份 initialize 响应（重放响应被监督进程丢弃）——
    # rpc(2) 直接读到 id=2 的响应即证明
    sup.stdin.close()
    assert sup.wait(timeout=10) == 0
```

- [ ] **Step 3: （仅 C 分支）实现 supervisor**

```python
# src/vibecad/supervisor.py
"""监督进程：spawn server 子进程并按行透传 stdio（MCP stdio = ndjson）；
子进程以 SWAP_EXIT 自退 = 换芯请求 → 换 conda python 重启子进程并重放
initialize 握手（丢弃重放响应）——客户端零感知。纯 stdlib，禁重依赖。"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading

SWAP_EXIT = 75


def _server_cmd() -> list[str]:
    if override := os.environ.get("VIBECAD_SUPERVISOR_TEST_CMD"):  # 仅测试注入
        return json.loads(override)
    from vibecad.runtime import paths, status  # noqa: PLC0415 - 每次重启时重读就绪态

    py = paths.active_runtime_python()
    try:
        if status.runtime_ready() and py.exists():
            return [str(py), "-m", "vibecad.server"]
    except OSError:
        pass
    return [sys.executable, "-m", "vibecad.server"]


class Supervisor:
    def __init__(self) -> None:
        self._handshake: list[bytes] = []      # [initialize 行, initialized 行]
        self._init_id: object = None
        self._pending: dict[object, bytes] = {}  # 已转发未响应请求 {id: 原始行}
        self._wlock = threading.Lock()         # child.stdin 写端原子切换
        self._child: subprocess.Popen | None = None

    def _spawn(self) -> subprocess.Popen:
        return subprocess.Popen(_server_cmd(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE)  # stderr 继承直通宿主日志

    def _client_to_child(self) -> None:
        for line in sys.stdin.buffer:
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                method = msg.get("method")
                if method == "initialize":
                    self._handshake = [line]
                    self._init_id = msg.get("id")
                elif method == "notifications/initialized":
                    self._handshake.append(line)
                elif "id" in msg and method:
                    self._pending[msg["id"]] = line  # 请求记账（响应回来即销）
            with self._wlock:
                ch = self._child
                if ch and ch.stdin:
                    with contextlib.suppress(OSError):  # 子进程刚死：换芯后重发 pending
                        ch.stdin.write(line)
                        ch.stdin.flush()
        with self._wlock:                      # 宿主关 stdin → 子进程自然收尾
            if self._child and self._child.stdin:
                with contextlib.suppress(OSError):
                    self._child.stdin.close()

    def _child_to_client(self, ch: subprocess.Popen) -> None:
        for line in ch.stdout:
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                if "id" in msg and "method" not in msg:
                    self._pending.pop(msg["id"], None)
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()

    def _replay_handshake(self, ch: subprocess.Popen) -> None:
        """新子进程重放 initialize/initialized；其 initialize 响应丢弃
        （客户端持有的是首次握手响应，重复响应会被客户端视为协议错误）。"""
        if not self._handshake:
            return
        ch.stdin.write(self._handshake[0])
        ch.stdin.flush()
        for line in ch.stdout:
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                if msg.get("id") == self._init_id and "method" not in msg:
                    break                      # 重放响应 → 丢弃
            sys.stdout.buffer.write(line)      # 握手期其他输出（日志通知）照常透传
            sys.stdout.buffer.flush()
        for extra in self._handshake[1:]:
            ch.stdin.write(extra)
        ch.stdin.flush()

    def run(self) -> int:
        self._child = self._spawn()
        threading.Thread(target=self._client_to_child, daemon=True).start()
        while True:
            ch = self._child
            pump = threading.Thread(target=self._child_to_client, args=(ch,))
            pump.start()
            code = ch.wait()
            pump.join()
            if code != SWAP_EXIT:
                return code                    # 真退出/崩溃：如实透传，不掩盖
            new = self._spawn()                # 换芯：此刻就绪哨兵已在 → conda python
            self._replay_handshake(new)
            with self._wlock:
                self._child = new
            for line in list(self._pending.values()):  # 重发换芯窗口悬空请求
                with contextlib.suppress(OSError):     # （窗口内只有只读轮询，幂等）
                    new.stdin.write(line)
            with contextlib.suppress(OSError):
                new.stdin.flush()


def main() -> None:
    sys.exit(Supervisor().run())
```

launcher.py 改为委托（入口签名不变，`python -m vibecad` / `uvx vibecad` / mcpb_entry 全部自动升级）：

```python
def main() -> None:
    from vibecad import supervisor
    supervisor.main()
```

（D 分支则 launcher 三态判断不动。卸载的启动清理与 `--uninstall` CLI 在 Task 4 Step 4 加进此 main 开头——本任务不引用 uninstall 模块，避免任务顺序断裂。）

- [ ] **Step 4: 测试过 + ruff** — `uv run pytest tests/test_supervisor.py tests/test_server_round11.py -v && uv run ruff check .`；`test_launcher.py` 既有用例若依赖旧三态逻辑，C 分支下改为对 `supervisor._server_cmd` 的等价断言（判据不变：ready+存在 → conda python，否则当前解释器）
- [ ] **Step 5: Commit** — `feat(core): zero-reconnect interpreter swap (supervisor + handshake replay)` 或 D 分支 `feat(server): self-restart on runtime ready (host auto-respawn)`

---

## Task 4：卸载（模型：sonnet）

**Files:** Create: `src/vibecad/runtime/uninstall.py`, `tests/test_uninstall.py`; Modify: `src/vibecad/server.py`, `src/vibecad/launcher.py`

- [ ] **Step 1: 失败测试**

```python
# tests/test_uninstall.py
"""标记/直删/护栏。全部 monkeypatch VIBECAD_HOME → tmp，不碰真实目录。"""
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
    (home / "envs").mkdir(parents=True)
    (home / "envs" / "f.bin").write_bytes(b"x" * 2048)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    info = uninstall.uninstall_now()
    assert info["ok"] and not home.exists() and info["freed_mb"] >= 0


def test_override_env_never_deleted(monkeypatch, tmp_path):
    """VIBECAD_FREECAD_ENV 用户自带 env 在 home 之外——删除 home 不得波及。"""
    home, override = tmp_path / "home", tmp_path / "user-env"
    home.mkdir(); override.mkdir(); (override / "keep").write_text("x")
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("VIBECAD_FREECAD_ENV", str(override))
    uninstall.request_uninstall()
    uninstall.perform_pending_uninstall()
    assert not home.exists() and (override / "keep").exists()
```

- [ ] **Step 2: 确认失败 → 实现**

```python
# src/vibecad/runtime/uninstall.py
"""卸载：标记 + 删除 vibecad_home 整目录。纯 stdlib。

删除范围永远 = vibecad_home()（托管运行时/micromamba/status/日志/views）。
VIBECAD_FREECAD_ENV 用户自带 env 在 home 之外，天然不在范围内——绝不触碰。
运行中删除走「标记 → server 自退 → 重启后 bootstrap 执行删除」：全平台一致，
避开 Windows 对运行中文件的锁。"""
from __future__ import annotations

import shutil
from pathlib import Path

from vibecad.runtime import paths


def uninstall_marker() -> Path:
    return paths.vibecad_home() / ".uninstall_requested"


def dir_size_mb(d: Path) -> float:
    total = 0
    for p in d.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total / 1e6


def request_uninstall() -> dict:
    home = paths.vibecad_home()
    if not home.exists():
        return {"ok": True, "already_clean": True, "message": "运行时目录不存在，无需卸载"}
    uninstall_marker().touch()
    return {"ok": True, "marked": True, "path": str(home)}


def perform_pending_uninstall() -> bool:
    """进程启动早期调用：有标记则删 home 整目录。返回是否执行了删除。"""
    home = paths.vibecad_home()
    try:
        if not uninstall_marker().exists():
            return False
    except OSError:
        return False
    shutil.rmtree(home, ignore_errors=True)
    return not home.exists()


def uninstall_now() -> dict:
    """直删（CLI / 无运行中 server 场景）。"""
    home = paths.vibecad_home()
    if not home.exists():
        return {"ok": True, "message": f"{home} 不存在，无需卸载"}
    size = dir_size_mb(home)
    shutil.rmtree(home, ignore_errors=True)
    if home.exists():
        return {"ok": False, "freed_mb": 0,
                "message": f"删除未完成（文件被占用？）：{home}；请关闭使用方后重试"}
    return {"ok": True, "freed_mb": round(size, 1), "path": str(home),
            "message": f"已删除 {home}（释放约 {size / 1000:.1f} GB）"}
```

- [ ] **Step 3: server 工具（两段式）+ 测试**

```python
# server.py 新工具（注册后全量 23 个）
// server.py 顶部新增：from vibecad.runtime import uninstall as _uninstall
//（paths 已有导入，沿用现名）
@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def uninstall_runtime(confirm: bool = False) -> dict[str, Any]:
    """卸载 CAD 引擎（删除全部已下载的运行时，约 2-3GB；扩展本体请在客户端设置里移除）。
    不带 confirm：仅预览将删除的路径与大小；confirm=true：执行删除（server 会自动重启完成清理，
    之后引擎需重新下载才能建模）。"""
    home = paths.vibecad_home()
    if not confirm:
        size = _uninstall.dir_size_mb(home) if home.exists() else 0.0
        return {"ok": True, "confirm_required": True, "path": str(home),
                "size_mb": round(size, 1),
                "message": "将删除以上目录；确认请再次调用 uninstall_runtime(confirm=true)"}
    info = _uninstall.request_uninstall()
    if info.get("marked"):
        _schedule_swap()   # 复用自退：重启后启动早期执行实际删除
        info["message"] = ("已计划删除：server 即将自动重启完成清理。"
                           "扩展本体如需移除，请在客户端设置（Extensions）里 Remove。")
    return info
```

测试（test_server_round11.py 追加）：不带 confirm → `confirm_required` 且目录原样；confirm=true → 标记文件存在 + 自退已安排（mock Timer）。

- [ ] **Step 4: launcher 接 CLI + 启动清理**（D/C 通用，代码见 Task 3 Step 3 的 launcher.main；`_cli_uninstall` 实现）：

```python
def _cli_uninstall() -> None:
    import json as _json
    from vibecad.runtime import paths, uninstall
    home = paths.vibecad_home()
    if "--yes" not in sys.argv and sys.stdin.isatty():
        ans = input(f"将删除 {home}（全部 CAD 运行时）。确认？[y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消")
            return
    print(_json.dumps(uninstall.uninstall_now(), ensure_ascii=False))
```

- [ ] **Step 5: 全量快测 + ruff + Commit** — `feat(runtime): clean uninstall (marker+restart, MCP tool, CLI rescue)`

---

## Task 5：manifest + guard 文案 + 版本 0.3.0（模型：sonnet）

**Files:** Modify: `manifest.json`, `src/vibecad/server.py`, `src/vibecad/__init__.py`, `pyproject.toml`, `tests/test_mcpb_manifest.py`, `tests/test_server_round10.py`

- [ ] **Step 1: manifest** — `mcp_config` 加 env（Q1 通过则含 VIBECAD_HOME；Q1 失败只留 AUTO_INSTALL）：

```json
"mcp_config": {
  "command": "uv",
  "args": ["run", "--no-dev", "--directory", "${__dirname}", "mcpb_entry.py"],
  "env": {
    "VIBECAD_AUTO_INSTALL": "1",
    "VIBECAD_HOME": "${__dirname}/runtime"
  }
}
```

tools 数组加 `uninstall_runtime`（描述与 server docstring 首行一致）、更新 `render_part` 描述（save_to）；`long_description` 加"卸载零残留：在设置里移除扩展即可连引擎一起删除"。版本三处同步 0.3.0（manifest/pyproject/`__init__.py`）+ `uv sync` 刷 uv.lock。`npx -y @anthropic-ai/mcpb@2.1.2 validate manifest.json` 过。

- [ ] **Step 2: guard 文案带进度 + needs_reconnect 恒 false + 测试**

```python
def _runtime_guard() -> dict[str, Any] | None:
    if not _installer.is_ready():
        st = _status.read_status()
        if st.phase == _status.Phase.FAILED:
            return {"ok": False, "phase": st.phase.value,
                    "message": f"CAD 引擎安装失败：{st.error or st.message}；"
                               "可调用 ensure_runtime 重试"}
        if st.phase == _status.Phase.NOT_STARTED:
            return {"ok": False, "phase": st.phase.value,
                    "message": "CAD 引擎未安装：调用 ensure_runtime 开始（约 2-3GB，仅一次）"}
        return {"ok": False, "phase": st.phase.value, "percent": st.percent,
                "message": f"正在准备 CAD 引擎：{st.message or st.phase.value}"
                           f"（{st.percent:.0f}%）。就绪后自动接管，无需任何手动操作，"
                           "可用 get_runtime_status 看进度"}
    if not _in_conda_runtime():
        _schedule_swap()
        return {"ok": False, "message": "引擎已就绪，正在自动切换（约 1 秒）——请直接重试刚才的操作"}
    return None
```

`get_runtime_status` 的 `needs_reconnect` 改恒 `False`（字段保留兼容旧文档），同分支调 `_schedule_swap()`。`smoke_cad` 的两条 guard 文案同步换。测试：guard 返回含 percent/phase；needs_reconnect 恒 False。

- [ ] **Step 3: 一致性测试更新** — `test_mcpb_manifest.py`：工具数随 registry 自动对齐（断言 23）；新增断言 env 含 `VIBECAD_AUTO_INSTALL`。`test_server_round10.py::test_all_tools_have_annotations` 自动覆盖新工具；`test_readonly_classification` 的 readonly 集合不变（uninstall_runtime 是 destructive）但需断言其 `destructiveHint is True`。
- [ ] **Step 4: 全量快测 + ruff + Commit** — `feat(mcpb): auto-install env + runtime-inside-extension + progress-aware guards; bump 0.3.0`

---

## Task 6：慢测——换芯端到端（不重装 runtime 的巧测）+ CI（模型：sonnet）

**Files:** Modify: `tests/test_runtime_integration.py`

- [ ] **Step 1: 慢测**（复用 `runtime_env` fixture 的持久 env，**不重新下载**）：

```python
@pytest.mark.slow
def test_auto_swap_end_to_end(runtime_env, tmp_path):
    """换芯全链路：把就绪哨兵临时藏起 → 起 server（bootstrap 芯）→ MCP 握手 →
    恢复哨兵 → get_runtime_status 触发自退 →（C：监督进程换芯 / D：手动重启进程模拟宿主）
    → 同一连接/新进程 smoke_cad 直接成功。打印 SWAP_OK。"""
    # 关键步骤：
    # 1. sentinel = paths.ready_sentinel()；sentinel.rename(hidden)  # 伪装未就绪
    # 2. subprocess 起 `python -m vibecad`（stdio 管道），完成 initialize 握手
    # 3. 调 ping 确认 bootstrap 芯（serverInfo 正常）
    # 4. hidden.rename(sentinel)  # 恢复就绪
    # 5. 调 get_runtime_status → 等 2 秒（自退+重启窗口）
    # 6. C 分支：同一管道继续调 smoke_cad → ok=True（零感知换芯的最终证据）
    #    D 分支：断言进程已退出(code==0)，重起进程重新握手 → smoke_cad ok=True
```

- [ ] **Step 2: 本机实跑全量慢测** — `VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -v -s`（首次会重建 `.vibecad-test-runtime`，约 2-3GB/4 分钟）
- [ ] **Step 3: Commit** — `test: zero-reconnect swap e2e on real runtime`

---

## Task 7：文档改版（模型：sonnet）

**Files:** Modify: `docs/USER_GUIDE.md`, `docs/ACCEPTANCE_TESTS.md`, `docs/DIRECTORY_SUBMISSION.md`, `README.md`

- [ ] **Step 1: USER_GUIDE** — 安装章节改三句话："下载 → 双击安装 → 直接开始说话（引擎自动在后台准备，约 2-3GB 仅一次；期间可随时问进度）"。删除"说帮我准备好 CAD 环境"步骤与 needs_reconnect 故障行。新增「卸载」章节：设置 → Extensions → Remove VibeCAD 即全删（运行时随扩展目录一起删除）；附 `uvx vibecad --uninstall` 救援命令（uvx 安装方式的运行时在全局目录）。新增「在 Cowork 里看图」说明：每步返回 view_file 路径，AI 会自动展示/可让 AI `open` 弹图。故障表更新（升级扩展会清运行时则在此警示——按 Q3 结论）。
- [ ] **Step 2: ACCEPTANCE_TESTS** — A1 改"装好后不说任何指令，直接问『进度怎么样』应看到自动安装进行中"；新增 A13 卸载验收（uninstall_runtime 两段式 + Remove extension 后目录消失）；A4 增加"Cowork 中用户能打开 view_file 看到工程图"。
- [ ] **Step 3: README + DIRECTORY_SUBMISSION** — README 快速开始改"双击安装即用（自动准备引擎）"；工具表加 `uninstall_runtime`、`render_part` 行补 save_to。DIRECTORY_SUBMISSION 工具数 23 + 卸载说明（上架审核关注卸载干净度，这是加分项）。
- [ ] **Step 4: Commit** — `docs(round11): auto-init flow, uninstall chapter, view_file guidance`

---

## Task 8：发布 0.3.0 + 真机验收（模型：sonnet；真机步骤需用户配合）

- [ ] **Step 1: rc 演练**（release.yml 的 mcpb job 首次真跑）：分支上把三处版本临时置 `0.3.0rc1` → `mcpb validate`（若 semver 校验拒绝 PEP440 的 rc 写法，manifest 改用 `0.3.0-rc.1` 并在该 rc 分支临时放宽 `test_version_synced_three_ways` 的相等断言为"前缀一致"——rc 分支不并回 main）→ tag `v0.3.0rc1` 推送 → 验证 PyPI 预发布 + GitHub Release 带 `--prerelease` + .mcpb 附件 → 下载 .mcpb 解包校验 env 两行存在。
- [ ] **Step 2: 用户真机验收**（测试 Mac，给操作卡）：删除旧 VibeCAD 扩展与 `~/Library/Application Support/VibeCAD` → 装 rc .mcpb → **不说任何安装指令**，直接问"进度怎么样"（验自动装）→ 等就绪 → 直接说"画一个 100×20×20 的 L 形拼接"（验零重连换芯 + 自动附图）→ 让 AI 打开 view_file（验 Cowork 看图）→ `uninstall_runtime` 两段式 → Remove extension → 抽查目录无残留。
- [ ] **Step 3: 清 rc + 正式发布** — `git push origin :v0.3.0rc1 && gh release delete v0.3.0rc1 --yes`；main 上版本定稿 0.3.0 → tag `v0.3.0` → PyPI 0.3.0 + 正式 Release；CI 全绿确认。
- [ ] **Step 4: Commit/收尾合并到 Task 9**

---

## Task 9：收尾（模型：sonnet）

- [ ] **Step 1:** 飞书同步（spec/plan/USER_GUIDE/验收 4 文档 → 过程文档文件夹 `DwYlfjYTelFG1RdTiFhc3NfWnAh`）
- [ ] **Step 2:** memory 更新（vibecad-status.md：R11 完成态、0.3.0 已发、上架材料就绪）
- [ ] **Step 3:** R10-T7 上架提交卡一并交付（Directory 提交字段 + PRIVACY 链接 + 23 工具）
- [ ] **Step 4:** 最终汇报（终态体验四件事逐条对照验收证据）

---

## Verification（端到端验收）

1. **快测**：`uv run ruff check . && uv run pytest -q` 全绿（persist/uninstall/supervisor/guard 文案/两段式/工具数 23/版本三同步）。
2. **慢测**：`VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow -v -s` → `SWAP_OK`（换芯端到端）+ 既有 65 条全过。
3. **真机（最终证据）**：测试 Mac 全新装 rc 包 → 自动下载 → 零重连画图 → Cowork 打开 view_file 看到工程图 → Remove extension 全删无残留。
4. **CI**：7 job 全绿；release：PyPI 0.3.0 + GitHub Release .mcpb。

## 范围纪律

仅本计划九个任务。不做：worker 子进程 RPC 架构（方案 A）、FreeCADGui 高质量渲染、运行时增量升级、Directory 实际提交（材料就绪即止，提交由用户操作）。
