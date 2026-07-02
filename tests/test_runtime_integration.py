"""真实安装并验证 A1/A2/A3（慢，下载 2-3GB）。
运行：VIBECAD_RUN_INTEGRATION=1 uv run pytest -m slow tests/test_runtime_integration.py -v -s
"""
import contextlib
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from vibecad.runtime import paths, status
from vibecad.runtime.installer import RuntimeInstaller

pytestmark = pytest.mark.slow
_RUN = os.environ.get("VIBECAD_RUN_INTEGRATION") == "1"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


@pytest.mark.skipif(not _RUN, reason="set VIBECAD_RUN_INTEGRATION=1")
def test_install_and_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path / "vchome"))
    monkeypatch.setenv("VIBECAD_PIP_SPEC", _REPO)  # 绝对路径装本地源（M8）
    phases = []
    RuntimeInstaller(on_progress=lambda s: phases.append(s.phase.value)).install()  # A2
    assert status.runtime_ready() is True
    assert status.health_check(paths.env_python()) is True  # A1（subprocess 级）

    # A1 进程内 import 经 conda env python 子进程执行后回读（不在 pytest 进程 import）
    step = str(tmp_path / "vc_smoke.step")
    code = (
        status._PREP +  # M-C：Windows DLL 兜底，否则 win CI 集成 import 必红
        "import FreeCAD, Part; b=Part.makeBox(10,10,10);"
        f"assert abs(b.Volume-1000.0)<1e-6; b.exportStep({step!r}); print('OK')"
    )
    r = subprocess.run([str(paths.env_python()), "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.path.exists(step)
    assert phases[-1] == "ready"


def test_walking_skeleton(runtime_env, tmp_path):
    """端到端 Walking Skeleton：建模→布尔→导出→诊断（真实 FreeCAD）。"""
    out = str(tmp_path)
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import os\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling, export\n"
        + "from vibecad.feedback.text import describe_shape\n"
        + "s = Session(); modeling.new_document(s, 'WalkingSkeleton')\n"
        + "box = modeling.add_box(s, 30, 30, 30)\n"
        + "assert box['ok'] and abs(box['volume'] - 27000) < 1e-2, box\n"
        + "cyl = modeling.add_cylinder(s, 8, 40)\n"
        + "assert cyl['ok'] and cyl['volume'] > 0\n"
        + "cut = modeling.boolean_cut(s, box['name'], cyl['name'])\n"
        + "assert cut['ok'] and 0 < cut['volume'] < 27000, cut\n"
        + f"r = export.export_part(s, {out!r})\n"
        + "assert os.path.getsize(r['step']) > 0 and os.path.getsize(r['stl']) > 0\n"
        + "d = describe_shape(s.get_object(cut['name']).Shape)\n"
        + "assert d['valid'] and d['solid_count'] == 1\n"
        + "assert abs(d['volume'] - cut['volume']) < 1e-4\n"
        + "print('SKELETON_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "SKELETON_OK" in p.stdout


def test_render_and_gltf(runtime_env, tmp_path):
    """端到端视觉：建模→布尔→render_png(PNG)→export_gltf(glb)（真实 FreeCAD）。"""
    glb = str(tmp_path / "p.glb")
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "from pygltflib import GLTF2\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.render import render_png\n"
        + "from vibecad.feedback.gltf import export_gltf\n"
        + "s = Session(); modeling.new_document(s, 'Visual')\n"
        + "b = modeling.add_box(s, 30, 20, 10)\n"
        + "c = modeling.add_cylinder(s, 5, 30)\n"
        + "cut = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "shape = s.get_object(cut['name']).Shape\n"
        + "png = render_png(shape, view='iso')\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000, len(png)\n"
        + f"gp = export_gltf(shape, {glb!r})\n"
        + "g = GLTF2().load(gp)\n"
        + "assert Path(gp).stat().st_size > 0 and len(g.meshes[0].primitives) > 0\n"
        + "print('VISUAL_OK')\n"
    )
    pr = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert pr.returncode == 0, pr.stderr
    assert "VISUAL_OK" in pr.stdout


def test_positioned_part(runtime_env, tmp_path):
    """端到端：position 参数造居中贯穿孔 + 渲染（真实 FreeCAD）。"""
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import math\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + "from vibecad.feedback.render import render_png\n"
        + "s = Session(); modeling.new_document(s, 'Plate')\n"
        + "b = modeling.add_box(s, 40, 30, 20)\n"
        + "c = modeling.add_cylinder(s, 6, 40, position=(20, 15, -10), axis='z')\n"
        + "cut = modeling.boolean_cut(s, b['name'], c['name'])\n"
        + "expected = 24000 - math.pi * 36 * 20\n"  # 居中贯穿 → 挖掉 box 内整段圆柱
        + "assert abs(cut['volume'] - expected) < 40, (cut['volume'], expected)\n"
        + "png = render_png(s.get_object(cut['name']).Shape)\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000\n"
        + "print('POSITIONED_OK')\n"
    )
    pr = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert pr.returncode == 0, pr.stderr
    assert "POSITIONED_OK" in pr.stdout


def test_annotated_feature_flow(runtime_env, tmp_path):
    """端到端指代闭环（Round 5）：标注→顶面标签打孔→重新标注→导出 STEP（真实 FreeCAD）。"""
    out = str(tmp_path)
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "import math, os\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.feedback.annotate import render_annotated\n"
        + "from vibecad.tools import export, features, modeling\n"
        + "s = Session(); modeling.new_document(s, 'R5Flow')\n"
        + "modeling.add_box(s, 40, 30, 20)\n"
        + "png, table, freg, ereg = render_annotated(s.get_result_shape(), mode='faces')\n"
        + "assert png[:4] == b'\\x89PNG' and len(png) > 2000, len(png)\n"
        + "s.set_labels(freg, ereg)\n"
        + "top = next(lab for lab, d in table.items() if '顶面' in d)\n"
        + "r = features.add_hole(s, top, 8)\n"
        + "expected = 24000 - math.pi * 16 * 20\n"  # ⌀8 通孔
        + "assert r['ok'] and abs(r['volume'] - expected) < 1.0, (r['volume'], expected)\n"
        + "png2, t2, freg2, ereg2 = render_annotated(s.get_result_shape(), mode='faces')\n"
        + "assert len(s.get_result_shape().Faces) > 6, len(s.get_result_shape().Faces)\n"
        + f"e = export.export_part(s, {out!r}, fmt='step')\n"
        + "assert os.path.getsize(e['step']) > 0\n"
        + "print('R5_FLOW_OK')\n"
    )
    p = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    assert "R5_FLOW_OK" in p.stdout


# --- Task 6：换芯端到端（C 分支：监督进程，同一管道零感知） ---
#
# 与上面几条慢测不同，这里不经 runtime_env 拿到的 conda python 直接 -c 跑代码
# （那条路径完全绕开 server.py 的 MCP 工具层，永远碰不到 _try_schedule_swap），
# 而是真起 `python -m vibecad`（宿主解释器）→ launcher.main → supervisor.Supervisor
# 常驻 → 首次 spawn 走 bootstrap 解释器（就绪哨兵此刻被藏起，_server_cmd 判定
# runtime_swappable()=False）。走完 initialize 握手后恢复哨兵，再调
# get_runtime_status 触发 server 侧 _try_schedule_swap → 1 秒后 os._exit(75) →
# supervisor 见 SWAP_EXIT 换 conda python 重启子进程 + 重放握手——全程只有一条
# stdio 管道，客户端（这条测试）不重连、不重新握手，直接在同一管道上继续调用。


def _read_line(proc: subprocess.Popen, timeout: float = 15.0) -> bytes:
    """带超时读一行：换芯/握手卡死时测试快速失败而非挂起整个 CI（对齐
    test_supervisor.py 的 _readline 兜底范式）。"""
    box: dict[str, bytes] = {}
    t = threading.Thread(
        target=lambda: box.setdefault("line", proc.stdout.readline()), daemon=True
    )
    t.start()
    t.join(timeout)
    if "line" not in box:
        proc.kill()
        pytest.fail(f"supervisor 未在 {timeout}s 内产出响应行（可能卡死于换芯/握手）")
    return box["line"]


def _send(proc: subprocess.Popen, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj).encode() + b"\n")
    proc.stdin.flush()


def _rpc(proc: subprocess.Popen, id_: int, method: str, params: dict | None = None,
         timeout: float = 15.0) -> dict:
    _send(proc, {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    return json.loads(_read_line(proc, timeout))


def _call_tool(proc: subprocess.Popen, id_: int, name: str, timeout: float = 15.0) -> dict:
    """封装 tools/call：返回顶层 JSON-RPC 响应（result/error）。"""
    return _rpc(proc, id_, "tools/call", {"name": name, "arguments": {}}, timeout=timeout)


def _tool_result_dict(resp: dict) -> dict:
    """FastMCP 的 tools/call 成功响应结构为
    {"result": {"content": [...], "structuredContent": {...} 或 isError}}——
    我们的工具都返回 dict，取 structuredContent（新版 SDK）或退回解析
    content[0].text 的 JSON（对齐 FastMCP 序列化两种可能形态，谁在场就用谁）。"""
    result = resp["result"]
    assert not result.get("isError"), resp
    if "structuredContent" in result:
        return result["structuredContent"]
    text = result["content"][0]["text"]
    return json.loads(text)


@pytest.mark.slow
def test_auto_swap_end_to_end(runtime_env, tmp_path):
    """换芯全链路（C 分支）：把就绪哨兵临时藏起 → 起 launcher（bootstrap 芯）→
    真实 MCP 握手 → 调 ping 确认 bootstrap 芯工作 → 恢复哨兵 → 调
    get_runtime_status 触发 _try_schedule_swap → server 1 秒后 exit(75) →
    supervisor 换芯重启进 conda 芯 + 握手重放（轮询直至新芯接管）→ 同一管道继续
    调 smoke_cad 成功——零感知换芯的最终证据。打印 SWAP_OK。
    """
    sentinel = paths.ready_sentinel()
    hidden = sentinel.with_name(sentinel.name + ".hidden-by-swap-test")
    assert sentinel.exists(), "runtime_env 应已保证就绪哨兵存在"

    # 步骤 1：把哨兵临时藏起，伪装"运行时未就绪"——逼 supervisor 首次 spawn 落
    # bootstrap 解释器（runtime_swappable()=False）。试结束必须恢复，用 try/finally。
    sentinel.rename(hidden)
    proc: subprocess.Popen | None = None
    try:
        # 步骤 2：subprocess 起 launcher（python -m vibecad，宿主解释器），stdio 管道。
        # 环境变量对齐 runtime_env fixture 已设置的 VIBECAD_HOME/VIBECAD_FREECAD_ENV
        # （os.environ 继承）；不开 VIBECAD_AUTO_INSTALL——运行时已就绪，不需要触发
        # 真实（重）安装，纯靠恢复哨兵 + get_runtime_status 驱动换芯。
        env = {**os.environ}
        env.pop("VIBECAD_AUTO_INSTALL", None)
        proc = subprocess.Popen(
            [sys.executable, "-m", "vibecad"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )

        # 真实 MCP JSON-RPC 行协议握手：initialize 带完整 params，收到响应后发
        # notifications/initialized（无 id，通知不等响应）。
        init_resp = _rpc(proc, 0, "initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "vibecad-swap-test", "version": "0.0.0"},
        })
        assert "result" in init_resp, init_resp
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 步骤 3：调 ping 确认 bootstrap 芯工作（bootstrap = 宿主解释器，非 conda）。
        ping_resp = _call_tool(proc, 1, "ping")
        assert "vibecad ok" in json.dumps(ping_resp), ping_resp

        # 步骤 4：恢复哨兵——运行时"变为"就绪，供下一步触发换芯判据。
        hidden.rename(sentinel)

        # 步骤 5：调 get_runtime_status → 触发 server 侧 _try_schedule_swap →
        # 1 秒后 os._exit(75) → supervisor 换芯重启进 conda 芯 + 握手重放。
        # 轮询而非裸 sleep：反复调 get_runtime_status，直到 needs_reconnect=False
        # 且 phase=ready（新芯已接管、同一管道继续可用）或超时。
        status_resp = _call_tool(proc, 2, "get_runtime_status")
        status_dict = _tool_result_dict(status_resp)
        assert status_dict["phase"] == "ready", status_dict
        assert status_dict["needs_reconnect"] is False, status_dict  # C 分支恒 False

        # 步骤 5+6 合并：以 smoke_cad 本身为换芯完成的探针——bootstrap 芯的
        # smoke_cad 被 guard 拦截（ok:False"正在自动切换"），conda 芯才能真跑
        # FreeCAD 成功。不能用"get_runtime_status 响应可解析"当判据：自退 Timer
        # 有 1 秒延迟，旧 bootstrap 芯在退出前照常响应，会被误判为换芯完成。
        # 同一管道轮询到 ok:True 即零感知换芯的最终证据（从未重连、从未重新握手）。
        deadline = time.monotonic() + 150.0  # 换芯 + 首次 import FreeCAD 都在窗口内
        next_id = 3
        smoke_dict: dict | None = None
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                resp = _call_tool(proc, next_id, "smoke_cad", timeout=90.0)
                candidate = _tool_result_dict(resp)
                if candidate.get("ok") is True:
                    smoke_dict = candidate
                    break
                last_err = AssertionError(f"仍在 bootstrap 芯：{candidate}")
            except Exception as exc:  # noqa: BLE001 - 换芯窗口内瞬时失败属预期，继续轮询
                last_err = exc
            finally:
                next_id += 1
            time.sleep(1.0)
        assert smoke_dict is not None, f"150s 内未完成零感知换芯：{last_err}"
        assert abs(smoke_dict["volume"] - 1000.0) < 1e-6, smoke_dict

        proc.stdin.close()
        assert proc.wait(timeout=15) == 0
        proc = None
        print("SWAP_OK")
    finally:
        # 全程读写要有超时兜底，绝不挂死：无论中途在哪一步失败都要收尾子进程。
        if proc is not None and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=10)
        # 哨兵必须恢复：不管测试成功/失败，绝不留一个"哨兵被藏起"的持久 env
        # 给后续测试或用户的真实运行时用。
        if hidden.exists() and not sentinel.exists():
            hidden.rename(sentinel)
