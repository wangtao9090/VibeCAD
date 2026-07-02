"""server Round 11：视觉落盘——_attach_view 加 view_file + render_part 加 save_to（TDD）。

参照 test_server_new_tools.py / test_server_round6.py 的 monkeypatch 范式。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 公共辅助
# ---------------------------------------------------------------------------

def _ready(monkeypatch, server):
    monkeypatch.setattr(server._installer, "is_ready", lambda: True)
    monkeypatch.setattr(server, "_in_conda_runtime", lambda: True)


def _mock_multiview(server, monkeypatch, png=b"\x89PNG fake"):
    monkeypatch.setattr(
        server._multiview, "render_multiview",
        lambda shape, part_map=None: (png, {"A": "顶面"}, {"A": {}}, {"E1": {}}),
    )


def _mock_assembly(server, monkeypatch):
    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_assembly_shape", lambda: _Shape())
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(server._session, "set_labels", lambda f, e, shown=None: None)


# ---------------------------------------------------------------------------
# 测试 1：_attach_view 成功附图时 result 带 view_file（落盘路径，文件真实存在）
# ---------------------------------------------------------------------------

def test_attach_view_includes_view_file(monkeypatch, tmp_path):
    """成功附图时 result 带落盘绝对路径；文件真实存在、在 tmp_path/views 下。"""
    import vibecad.server as srv

    _ready(monkeypatch, srv)

    # VIBECAD_HOME → tmp_path，使 persist.save_view 落盘到可控目录
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))

    # mock 建模操作
    monkeypatch.setattr(
        srv._modeling, "add_box",
        lambda s, ln, w, h, position: {"ok": True, "name": "Box", "volume": 1000.0},
    )
    # mock 参数清单（_attach_view 内会调用）
    monkeypatch.setattr(
        srv._modify, "list_parameters",
        lambda doc, session=None: {},
    )
    # mock doc.Name
    mock_doc = type("FakeDoc", (), {"Name": "TestDoc"})()
    monkeypatch.setattr(type(srv._session), "doc", property(lambda self: mock_doc), raising=False)

    _mock_assembly(srv, monkeypatch)
    _mock_multiview(srv, monkeypatch)

    out = srv.add_box(10, 10, 10)

    # 返回 [result_dict, Image]
    assert isinstance(out, list) and len(out) == 2
    result = out[0]

    # result["view_file"] 以 .png 结尾
    assert "view_file" in result, f"view_file 字段缺失，result={result}"
    vf = result["view_file"]
    assert vf.endswith(".png"), f"view_file 不以 .png 结尾：{vf}"

    # 文件真实存在
    assert Path(vf).exists(), f"view_file 路径不存在：{vf}"

    # 在 tmp_path/views 下
    assert str(tmp_path / "views") in vf, f"view_file 不在 tmp_path/views 下：{vf}"


# ---------------------------------------------------------------------------
# 测试 2：落盘失败不连坐
# ---------------------------------------------------------------------------

def test_attach_view_persist_failure_not_fatal(monkeypatch):
    """落盘抛 OSError → 操作仍成功，带 view_file_error，Image 仍返回。"""
    from mcp.server.fastmcp import Image

    import vibecad.server as srv

    _ready(monkeypatch, srv)

    # patch persist.save_view 抛 OSError
    def _boom_save(png, doc, tool):
        raise OSError("磁盘满")

    monkeypatch.setattr(srv._persist, "save_view", _boom_save)

    monkeypatch.setattr(
        srv._modeling, "add_box",
        lambda s, ln, w, h, position: {"ok": True, "name": "Box", "volume": 1000.0},
    )
    monkeypatch.setattr(
        srv._modify, "list_parameters",
        lambda doc, session=None: {},
    )
    mock_doc = type("FakeDoc", (), {"Name": "TestDoc"})()
    monkeypatch.setattr(type(srv._session), "doc", property(lambda self: mock_doc), raising=False)

    _mock_assembly(srv, monkeypatch)
    _mock_multiview(srv, monkeypatch)

    out = srv.add_box(10, 10, 10)

    # 仍返回 [result, Image]（落盘失败不连坐）
    assert isinstance(out, list) and len(out) == 2
    assert isinstance(out[1], Image)
    result = out[0]
    assert result["ok"] is True

    # 带 view_file_error、无 view_file
    assert "view_file_error" in result, f"应有 view_file_error，result={result}"
    assert "view_file" not in result, f"不应有 view_file，result={result}"


# ---------------------------------------------------------------------------
# 测试 3：render_part(save_to=...) 文件写入 + 返回含 saved 字段
# ---------------------------------------------------------------------------

def test_render_part_save_to(monkeypatch, tmp_path):
    """render_part(save_to=...) → 文件写入 + 返回含 saved 字段。"""
    from mcp.server.fastmcp import Image

    import vibecad.server as srv

    _ready(monkeypatch, srv)

    fake_png = b"\x89PNG fake"
    monkeypatch.setattr(srv._render, "render_png",
                        lambda shape, view="iso": fake_png)
    _mock_assembly(srv, monkeypatch)

    out_path = str(tmp_path / "out" / "x.png")
    out = srv.render_part(view="iso", save_to=out_path)

    # 返回 [Image, json字符串]
    assert isinstance(out, list) and len(out) == 2, f"应返回 [Image, json]，实际：{out}"
    assert isinstance(out[0], Image)

    # json 里 saved == 该路径
    payload = json.loads(out[1])
    assert payload["ok"] is True
    assert "saved" in payload, f"json 里应有 saved 字段：{payload}"
    assert Path(payload["saved"]) == Path(out_path).expanduser()

    # 文件存在且内容为假 PNG
    assert Path(payload["saved"]).exists(), f"文件不存在：{payload['saved']}"
    assert Path(payload["saved"]).read_bytes() == fake_png


# ---------------------------------------------------------------------------
# 测试 4：save_to 指向不可写位置 → 渲染仍成功，带 save_error
# ---------------------------------------------------------------------------

def test_render_part_save_to_failure_not_fatal(monkeypatch, tmp_path):
    """save_to 指向不可写位置 → 渲染仍成功返回 Image，带 save_error。"""
    from mcp.server.fastmcp import Image

    import vibecad.server as srv

    _ready(monkeypatch, srv)

    fake_png = b"\x89PNG fake"
    monkeypatch.setattr(srv._render, "render_png",
                        lambda shape, view="iso": fake_png)
    _mock_assembly(srv, monkeypatch)

    # 不可写路径：已存在的文件作为目录名（OSError：父路径是文件，mkdir 失败）
    existing_file = tmp_path / "notadir"
    existing_file.write_bytes(b"x")
    bad_path = str(existing_file / "sub" / "x.png")

    out = srv.render_part(view="iso", save_to=bad_path)

    # 不传 save_to 时原来返回 Image；现在 save_to 失败时……
    # 根据计划：渲染仍成功返回 Image + save_error
    # 实现后可能是 [Image, json(save_error)] 或单独 Image with save_error
    # 计划说"渲染仍成功返回 Image，带 save_error"——断言 Image 在返回里
    if isinstance(out, list):
        imgs = [x for x in out if isinstance(x, Image)]
        assert len(imgs) >= 1, f"应包含 Image：{out}"
        # 找 json 里的 save_error
        jsons = [x for x in out if isinstance(x, str)]
        assert jsons, f"应有 json 字符串：{out}"
        payload = json.loads(jsons[0])
        assert "save_error" in payload, f"json 里应有 save_error：{payload}"
    else:
        # 单独返回 Image（也可接受：表示降级处理）
        assert isinstance(out, Image), f"应返回 Image，实际：{out}"
        # 但此时 save_error 信息丢失，断言不合要求——让测试按需失败以驱动实现
        raise AssertionError("应返回包含 save_error 的列表，实际返回单独 Image")


# ---------------------------------------------------------------------------
# Task 3 Step 1：server 自退换芯钩子（_schedule_swap + 四处触发点 + needs_reconnect）
# 纪律：所有测试 mock Timer / _schedule_swap，绝不让 os._exit 真跑起来杀掉测试进程。
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_supervised_leak(monkeypatch):
    """触发点测试默认不受外部 VIBECAD_SUPERVISED 污染（I4 分支由测试显式控制）。"""
    monkeypatch.delenv("VIBECAD_SUPERVISED", raising=False)


class _FakeTimer:
    """假 Timer：只记录 daemon/start，绝不真调度 os._exit。"""

    def __init__(self):
        self.daemon = False
        self.started = False

    def start(self):
        self.started = True


def _record_swap(monkeypatch, srv):
    """把 _schedule_swap 换成记录器，并给出默认可换芯环境（受监督 + 判据通过）：
    触发点测试只关心"是否安排了自退"；C1/I4 反向分支由具体测试覆盖默认值。"""
    calls = []
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")
    monkeypatch.setattr(srv, "runtime_swappable", lambda: True)
    monkeypatch.setattr(srv, "_schedule_swap", lambda delay=1.0: calls.append(delay))
    return calls


def test_schedule_swap_idempotent(monkeypatch):
    """ready+bootstrap 时安排一次延迟自退；重复调用不叠 Timer。"""
    import vibecad.server as srv

    timers = []

    def _fake_timer_cls(delay, func, args=()):
        t = _FakeTimer()
        timers.append((delay, func, args, t))
        return t

    monkeypatch.setattr(srv.threading, "Timer", _fake_timer_cls)
    monkeypatch.setattr(srv, "_swap_timer", None)

    srv._schedule_swap()
    srv._schedule_swap()

    assert len(timers) == 1, f"重复调用不应叠 Timer：{timers}"
    delay, func, args, t = timers[0]
    assert delay == 1.0
    assert func is srv.os._exit and args == (srv.SWAP_EXIT,)
    assert t.daemon is True and t.started is True


def test_runtime_status_triggers_swap_when_ready_in_bootstrap(monkeypatch, tmp_path):
    """get_runtime_status：ready 且非 conda → needs_reconnect 恒 False + 已安排自退。"""
    import vibecad.server as srv

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv.status, "runtime_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    d = srv.get_runtime_status()

    assert d["needs_reconnect"] is False, "自退换芯后客户端零感知，needs_reconnect 应恒 False"
    assert len(calls) == 1, "ready+bootstrap 应安排一次自退"


def test_runtime_status_no_swap_when_in_conda(monkeypatch, tmp_path):
    """get_runtime_status：ready 且已是 conda 解释器 → 不安排自退。"""
    import vibecad.server as srv

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv.status, "runtime_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: True)

    d = srv.get_runtime_status()

    assert d["needs_reconnect"] is False
    assert calls == [], "已在 conda 运行时不应安排自退"


def test_runtime_status_no_swap_when_not_ready(monkeypatch, tmp_path):
    """get_runtime_status：未就绪 → 不安排自退，needs_reconnect 恒 False。"""
    import vibecad.server as srv

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv.status, "runtime_ready", lambda: False)

    d = srv.get_runtime_status()

    assert d["needs_reconnect"] is False
    assert calls == [], "未就绪不应安排自退"


def test_runtime_guard_triggers_swap_in_bootstrap(monkeypatch):
    """_runtime_guard：ready+bootstrap → 结构化拒绝 + 已安排自退，不再要求手动重连。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    guard = srv._runtime_guard()

    assert guard is not None and guard["ok"] is False
    assert "自动切换" in guard["message"], f"应提示自动切换：{guard['message']}"
    assert "重连" not in guard["message"], "自退换芯后不应再要求用户手动重连"
    assert len(calls) == 1


def test_runtime_guard_passes_in_conda(monkeypatch):
    """_runtime_guard：ready 且已在 conda → 放行且不安排自退。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: True)

    assert srv._runtime_guard() is None
    assert calls == []


def test_ensure_runtime_triggers_swap_in_bootstrap(monkeypatch):
    """_ensure_runtime_impl：ready+bootstrap → status=ready + 已安排自退 + 不再提手动重连。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    out = srv._ensure_runtime_impl()

    assert out["status"] == "ready"
    assert "重连" not in out["message"], "自退换芯后不应再要求用户手动重连"
    assert len(calls) == 1


def test_ensure_runtime_no_swap_in_conda(monkeypatch):
    """_ensure_runtime_impl：ready 且已在 conda → 不安排自退。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: True)

    out = srv._ensure_runtime_impl()

    assert out["status"] == "ready"
    assert calls == []


def test_safe_install_success_triggers_swap(monkeypatch):
    """_safe_install：安装成功结束后自动安排自退——用户全程不开口也自动换芯。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv._installer, "install", lambda: None)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    srv._safe_install()

    assert len(calls) == 1, "安装成功后应安排自退换芯"


def test_safe_install_failure_no_swap(monkeypatch):
    """_safe_install：安装失败（异常已落 status.json）→ 不安排自退。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)

    def _boom():
        raise RuntimeError("安装失败")

    monkeypatch.setattr(srv._installer, "install", _boom)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    srv._safe_install()  # 不应抛出

    assert calls == [], "安装失败不应安排自退"


# ---------------------------------------------------------------------------
# C1：换芯判据单一真源（runtime_swappable）——哨兵在而 conda python 缺失时绝不自杀
# I4：裸 server（无 VIBECAD_SUPERVISED）自杀无人重启——回退诚实提示重连
# ---------------------------------------------------------------------------


def test_runtime_status_no_swap_when_conda_python_missing(monkeypatch, tmp_path):
    """C1：哨兵在而 conda python 缺失（runtime_swappable False）→ 绝不安排自杀
    （否则 supervisor 落 bootstrap、新 server 又自杀成无限循环）；诚实标记
    needs_reconnect=True。"""
    import vibecad.server as srv

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv, "runtime_swappable", lambda: False)  # 覆盖 helper 默认
    monkeypatch.setattr(srv.status, "runtime_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    d = srv.get_runtime_status()

    assert calls == [], "判据不通过时绝不安排自杀（防无限重启循环）"
    assert d["needs_reconnect"] is True


def test_runtime_guard_no_swap_when_conda_python_missing(monkeypatch):
    """C1：guard 触发点与 supervisor._server_cmd 同判据——不可换芯时不自杀，
    文案回退提示重连（诚实反馈）。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv, "runtime_swappable", lambda: False)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    guard = srv._runtime_guard()

    assert calls == []
    assert guard is not None and guard["ok"] is False
    assert "重连" in guard["message"]


def test_safe_install_no_swap_when_not_swappable(monkeypatch):
    """C1：安装结束但判据不通过（如哨兵落了而 python 缺失）→ 不安排自杀。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.setattr(srv, "runtime_swappable", lambda: False)
    monkeypatch.setattr(srv._installer, "install", lambda: None)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    srv._safe_install()

    assert calls == []


def test_runtime_guard_unsupervised_falls_back_to_reconnect(monkeypatch):
    """I4：裸 server（无 VIBECAD_SUPERVISED）自杀后无人重启 = 服务凭空消失——
    不自杀，guard 文案回退提示重连。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.delenv("VIBECAD_SUPERVISED", raising=False)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    guard = srv._runtime_guard()

    assert calls == []
    assert guard is not None and guard["ok"] is False
    assert "重连" in guard["message"]


def test_runtime_status_unsupervised_reports_needs_reconnect(monkeypatch, tmp_path):
    """I4：裸 server 就绪后 needs_reconnect 诚实回退 True（不再谎报零感知）。"""
    import vibecad.server as srv

    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))
    calls = _record_swap(monkeypatch, srv)
    monkeypatch.delenv("VIBECAD_SUPERVISED", raising=False)
    monkeypatch.setattr(srv.status, "runtime_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    d = srv.get_runtime_status()

    assert calls == []
    assert d["needs_reconnect"] is True


def test_ensure_runtime_unsupervised_hints_reconnect(monkeypatch):
    """I4：ensure_runtime 在裸 server 下就绪 → 不自杀，消息提示重连。"""
    import vibecad.server as srv

    calls = _record_swap(monkeypatch, srv)
    monkeypatch.delenv("VIBECAD_SUPERVISED", raising=False)
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)

    out = srv._ensure_runtime_impl()

    assert out["status"] == "ready"
    assert "重连" in out["message"]
    assert calls == []


# ---------------------------------------------------------------------------
# Task 4 Step 3：uninstall_runtime 两段式 MCP 工具
# ---------------------------------------------------------------------------

def test_uninstall_runtime_preview_without_confirm(monkeypatch, tmp_path):
    """不带 confirm：仅预览——路径 + 大小 + confirm_required:true；目录/标记原样不动。"""
    import vibecad.server as srv

    home = tmp_path / "home"
    (home / "mamba").mkdir(parents=True)
    (home / "mamba" / "big.bin").write_bytes(b"x" * 2_000_000)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    calls = _record_swap(monkeypatch, srv)

    out = srv.uninstall_runtime()

    assert out["ok"] is True
    assert out["confirm_required"] is True
    assert out["path"] == str(home)
    assert out["size_mb"] > 0
    assert "confirm=true" in out["message"] or "confirm=True" in out["message"]
    assert home.exists()
    assert not srv._uninstall.uninstall_marker().exists()
    assert calls == []


def test_uninstall_runtime_confirm_supervised_schedules_swap(monkeypatch, tmp_path):
    """confirm=true + supervised + swappable：标记文件写入，自退已安排，message 提示自动重启。"""
    import vibecad.server as srv

    home = tmp_path / "home"
    (home / "mamba").mkdir(parents=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    calls = _record_swap(monkeypatch, srv)

    out = srv.uninstall_runtime(confirm=True)

    assert out["ok"] is True
    assert out.get("marked") is True
    assert srv._uninstall.uninstall_marker().exists()
    assert len(calls) == 1, "supervised 场景应安排一次自退"
    assert "自动重启" in out["message"] or "自动切换" in out["message"]
    assert "Remove" in out["message"] or "移除" in out["message"]


def test_uninstall_runtime_confirm_unsupervised_hints_manual_restart(monkeypatch, tmp_path):
    """confirm=true 裸跑（无 VIBECAD_SUPERVISED）：标记文件仍写入，但不自杀，
    message 提示需要手动重启 server 才能完成清理。"""
    import vibecad.server as srv

    home = tmp_path / "home"
    (home / "mamba").mkdir(parents=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    calls = _record_swap(monkeypatch, srv)
    monkeypatch.delenv("VIBECAD_SUPERVISED", raising=False)

    out = srv.uninstall_runtime(confirm=True)

    assert out["ok"] is True
    assert out.get("marked") is True
    assert srv._uninstall.uninstall_marker().exists()
    assert calls == [], "裸 server 不应自杀"
    assert "重启" in out["message"]


def test_uninstall_runtime_confirm_already_clean(monkeypatch, tmp_path):
    """home 不存在时 confirm=true：already_clean 原样返回，不写标记、不自退。"""
    import vibecad.server as srv

    home = tmp_path / "no-such-home"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    calls = _record_swap(monkeypatch, srv)

    out = srv.uninstall_runtime(confirm=True)

    assert out["ok"] is True and out.get("already_clean") is True
    assert not home.exists() and calls == []


def test_uninstall_runtime_repeat_confirm_idempotent(monkeypatch, tmp_path):
    """连续两次 confirm=true：marker touch 幂等、两次都成功报 marked——
    重复确认不报错也不产生额外副作用（Timer 幂等由 _schedule_swap 自身测试覆盖）。"""
    import vibecad.server as srv

    home = tmp_path / "home"
    (home / "mamba").mkdir(parents=True)
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    _record_swap(monkeypatch, srv)

    first = srv.uninstall_runtime(confirm=True)
    second = srv.uninstall_runtime(confirm=True)

    assert first.get("marked") and second.get("marked")
    assert srv._uninstall.uninstall_marker().exists()


def test_uninstall_runtime_guard_rejection_passthrough(monkeypatch, tmp_path):
    """护栏拒绝（如目录不含安装产物）：request_uninstall 返回 ok:false 原样透传，
    不触发自退。"""
    import vibecad.server as srv

    home = tmp_path / "home"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    _record_swap(monkeypatch, srv)
    monkeypatch.setattr(
        srv._uninstall, "request_uninstall",
        lambda: {"ok": False, "message": "目录不含 VibeCAD 安装产物，拒绝删除"},
    )

    out = srv.uninstall_runtime(confirm=True)

    assert out == {"ok": False, "message": "目录不含 VibeCAD 安装产物，拒绝删除"}


# ---------------------------------------------------------------------------
# Task 5 Step 2：_runtime_guard 未就绪侧文案带进度（phase/percent），
# ready+bootstrap 分支（已在上方覆盖）保持不变。
# ---------------------------------------------------------------------------

def test_runtime_guard_not_started_phase_message(monkeypatch):
    """NOT_STARTED：提示调用 ensure_runtime 开始，带 phase 字段。"""
    import vibecad.server as srv
    from vibecad.runtime import status as _status

    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    monkeypatch.setattr(
        srv.status, "read_status",
        lambda: _status.RuntimeStatus(phase=_status.Phase.NOT_STARTED),
    )

    guard = srv._runtime_guard()

    assert guard is not None and guard["ok"] is False
    assert guard["phase"] == "not_started"
    assert "ensure_runtime" in guard["message"]


def test_runtime_guard_failed_phase_message(monkeypatch):
    """FAILED：带 error/message 详情，提示可重试 ensure_runtime。"""
    import vibecad.server as srv
    from vibecad.runtime import status as _status

    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    monkeypatch.setattr(
        srv.status, "read_status",
        lambda: _status.RuntimeStatus(phase=_status.Phase.FAILED, error="磁盘空间不足"),
    )

    guard = srv._runtime_guard()

    assert guard is not None and guard["ok"] is False
    assert guard["phase"] == "failed"
    assert "磁盘空间不足" in guard["message"]
    assert "ensure_runtime" in guard["message"]


def test_runtime_guard_installing_phase_includes_percent(monkeypatch):
    """安装进行中（如 creating_env）：guard 带 percent 数值 + 阶段文案，AI 可直接转述进度。"""
    import vibecad.server as srv
    from vibecad.runtime import status as _status

    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    monkeypatch.setattr(
        srv.status, "read_status",
        lambda: _status.RuntimeStatus(
            phase=_status.Phase.CREATING_ENV, percent=40.0, message="creating_env"),
    )

    guard = srv._runtime_guard()

    assert guard is not None and guard["ok"] is False
    assert guard["phase"] == "creating_env"
    assert guard["percent"] == 40.0
    assert "40" in guard["message"]
    assert "get_runtime_status" in guard["message"]


# ---------------------------------------------------------------------------
# Task 5 Step 4：VIBECAD_AUTO_INSTALL 接线——manifest env 生效的落地点。
# server.main() 是 `python -m vibecad.server` 子进程入口（supervisor._server_cmd
# 拉起的真实路径）；auto-install env 必须在这里触发，而不是仅在 launcher/supervisor
# 层空转。
# ---------------------------------------------------------------------------

def test_main_spawns_install_when_auto_install_enabled(monkeypatch):
    import vibecad.server as srv

    monkeypatch.setenv("VIBECAD_AUTO_INSTALL", "1")
    monkeypatch.setattr(srv, "mcp", type("_M", (), {"run": staticmethod(lambda: None)})())
    calls = []
    monkeypatch.setattr(srv, "_spawn_install", lambda: calls.append(1))

    srv.main()

    assert calls == [1]


def test_main_skips_install_when_auto_install_disabled(monkeypatch):
    import vibecad.server as srv

    monkeypatch.delenv("VIBECAD_AUTO_INSTALL", raising=False)
    monkeypatch.setattr(srv, "mcp", type("_M", (), {"run": staticmethod(lambda: None)})())
    calls = []
    monkeypatch.setattr(srv, "_spawn_install", lambda: calls.append(1))

    srv.main()

    assert calls == []


def test_auto_install_enabled_rejects_falsy_values(monkeypatch):
    import vibecad.server as srv

    for falsy in ("0", "false", "False"):
        monkeypatch.setenv("VIBECAD_AUTO_INSTALL", falsy)
        assert srv._auto_install_enabled() is False, f"{falsy!r} 应视为关闭"
    monkeypatch.delenv("VIBECAD_AUTO_INSTALL", raising=False)
    assert srv._auto_install_enabled() is False, "未设置应默认关闭（仅 mcpb env 显式开启）"
    monkeypatch.setenv("VIBECAD_AUTO_INSTALL", "1")
    assert srv._auto_install_enabled() is True

