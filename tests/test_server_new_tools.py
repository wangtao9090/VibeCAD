import vibecad.server as srv


def _ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: True)


def test_add_box_guard_not_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    monkeypatch.setattr(
        srv.status,
        "read_status",
        lambda: srv.status.RuntimeStatus(phase=srv.status.Phase.NOT_STARTED),
    )
    r = srv.add_box(10, 10, 10)
    assert r["ok"] is False and "ensure_runtime" in r["message"]


def test_add_box_guard_bootstrap_schedules_swap(monkeypatch):
    """Round 11：ready+bootstrap 不再要求手动重连，而是安排自退换芯后结构化拒绝。"""
    monkeypatch.setenv("VIBECAD_SUPERVISED", "1")            # I4：受监督才允许自杀
    monkeypatch.setattr(srv, "runtime_swappable", lambda: True)  # C1：判据通过
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)
    calls = []
    monkeypatch.setattr(srv, "_schedule_swap", lambda delay=1.0: calls.append(delay))
    r = srv.add_box(10, 10, 10)
    assert r["ok"] is False and "自动切换" in r["message"]
    assert len(calls) == 1


def test_new_document_delegates(monkeypatch):
    _ready(monkeypatch)
    seen = {}

    def _fake_new_document(s, name, *, discard_unsaved=False):
        seen["a"] = (s, name, discard_unsaved)
        return {"ok": True, "name": name}

    monkeypatch.setattr(srv._modeling, "new_document", _fake_new_document)
    r = srv.new_document("P")
    assert r == {"ok": True, "name": "P"}
    assert seen["a"] == (srv._session, "P", False)


def test_add_box_delegates(monkeypatch):
    _ready(monkeypatch)
    seen = {}

    def _fake_add_box(s, length, w, h, position):
        seen["a"] = (length, w, h)
        return {"ok": True}

    monkeypatch.setattr(srv._modeling, "add_box", _fake_add_box)
    # Round 8 升格：_attach_view 调用 get_assembly_shape()
    monkeypatch.setattr(srv._session, "get_assembly_shape", lambda: object())
    monkeypatch.setattr(srv._session, "get_result_shape", lambda: object())
    # Round 8 升格：render_multiview 接受 part_map 关键字参数
    monkeypatch.setattr(srv._multiview, "render_multiview",
                        lambda shape, part_map=None: (b"\x89PNG", {}, {}, {}))
    monkeypatch.setattr(srv._session, "set_labels", lambda f, e, shown=None: None)
    out = srv.add_box(10, 20, 30)
    assert isinstance(out, list) and out[0]["ok"] is True  # 成功路径返回 [dict, Image]
    assert seen["a"] == (10, 20, 30)


def test_add_cylinder_delegates(monkeypatch):
    _ready(monkeypatch)
    seen = {}
    def _fake_add_cylinder(s, r, h, position, axis):
        seen.setdefault("a", (r, h))
        return {"ok": True}

    monkeypatch.setattr(srv._modeling, "add_cylinder", _fake_add_cylinder)
    srv.add_cylinder(5, 12)
    assert seen["a"] == (5, 12)


def test_add_box_forwards_position(monkeypatch):
    _ready(monkeypatch)
    seen = {}

    def _fake_add_box_pos(s, ln, w, h, position):
        seen.setdefault("a", (ln, w, h, position))
        return {"ok": True}

    monkeypatch.setattr(srv._modeling, "add_box", _fake_add_box_pos)
    srv.add_box(10, 20, 30, position=[1, 2, 3])
    assert seen["a"] == (10, 20, 30, (1, 2, 3))


def test_add_box_empty_list_forwards_as_empty_tuple(monkeypatch):
    """空列表不应静默转为原点 (0,0,0)，而应原样转为 () 交给 _validate_position 拒绝。"""
    _ready(monkeypatch)
    seen = {}

    def _fake_add_box_empty(s, ln, w, h, position):
        seen["position"] = position
        return {"ok": True}

    monkeypatch.setattr(srv._modeling, "add_box", _fake_add_box_empty)
    srv.add_box(10, 20, 30, position=[])
    assert seen["position"] == (), f"expected empty tuple, got {seen['position']!r}"


def test_add_cylinder_forwards_position_axis(monkeypatch):
    _ready(monkeypatch)
    seen = {}

    def _fake_add_cyl_pos(s, r, h, position, axis):
        seen.setdefault("a", (r, h, position, axis))
        return {"ok": True}

    monkeypatch.setattr(srv._modeling, "add_cylinder", _fake_add_cyl_pos)
    srv.add_cylinder(5, 12, position=[1, 1, 0], axis="x")
    assert seen["a"] == (5, 12, (1, 1, 0), "x")


def test_add_cylinder_default_axis_z(monkeypatch):
    _ready(monkeypatch)
    seen = {}

    def _fake_add_cyl_axis(s, r, h, position, axis):
        seen.setdefault("a", axis)
        return {"ok": True}

    monkeypatch.setattr(srv._modeling, "add_cylinder", _fake_add_cyl_axis)
    srv.add_cylinder(5, 12)
    assert seen["a"] == "z"


def test_boolean_cut_delegates(monkeypatch):
    _ready(monkeypatch)
    seen = {}
    monkeypatch.setattr(srv._modeling, "boolean_cut",
                        lambda s, b, t: seen.setdefault("a", (b, t)) or {"ok": True})
    srv.boolean_cut("Box", "Cyl")
    assert seen["a"] == ("Box", "Cyl")


def test_new_boolean_tools_delegate(monkeypatch):
    _ready(monkeypatch)
    seen = []
    monkeypatch.setattr(srv, "_attach_view", lambda result, tool: result)
    monkeypatch.setattr(
        srv._modeling, "boolean_fuse",
        lambda s, b, t: seen.append(("fuse", s, b, t)) or {"ok": True})
    monkeypatch.setattr(
        srv._modeling, "boolean_common",
        lambda s, b, t: seen.append(("common", s, b, t)) or {"ok": True})
    assert srv.boolean_fuse("Box", "Box001")["ok"] is True
    assert srv.boolean_common("Box", "Box001")["ok"] is True
    assert [row[0] for row in seen] == ["fuse", "common"]
    assert all(row[1] is srv._session for row in seen)


def test_project_tools_delegate(monkeypatch, tmp_path):
    _ready(monkeypatch)
    seen = []
    monkeypatch.setattr(
        srv._project, "save_project",
        lambda s, path, overwrite=True:
        seen.append(("save", s, path, overwrite)) or {"ok": True})
    monkeypatch.setattr(
        srv._project, "open_project",
        lambda s, path, discard_unsaved=False:
        seen.append(("open", s, path, discard_unsaved))
        or {"ok": True, "has_result": False})
    assert srv.save_project(str(tmp_path / "a.FCStd"), overwrite=False)["ok"] is True
    assert srv.open_project(str(tmp_path / "a.FCStd"), discard_unsaved=True)["ok"] is True
    assert seen[0][0] == "save" and seen[0][-1] is False
    assert seen[1][0] == "open" and seen[1][-1] is True


def test_history_delete_and_measure_delegate(monkeypatch):
    _ready(monkeypatch)
    monkeypatch.setattr(srv, "_attach_view", lambda result, tool: result)
    monkeypatch.setattr(
        srv._project, "delete_object",
        lambda s, name, cascade=False:
        {"ok": True, "has_result": False, "name": name, "cascade": cascade})
    monkeypatch.setattr(
        srv._project, "undo", lambda s: {"ok": True, "has_result": False})
    monkeypatch.setattr(
        srv._project, "redo", lambda s: {"ok": True, "has_result": False})
    monkeypatch.setattr(
        srv._measure, "measure",
        lambda s, **kwargs: {"ok": True, **kwargs})
    assert srv.delete_object("Box", cascade=True)["cascade"] is True
    assert srv.undo()["ok"] is True and srv.redo()["ok"] is True
    out = srv.measure(kind="distance", first="A", second="B")
    assert out["kind"] == "distance" and out["first"] == "A"


def test_export_part_delegates(monkeypatch):
    _ready(monkeypatch)
    seen = {}
    # Round 8 升格：export_part 新增 split 参数
    monkeypatch.setattr(srv._export, "export_part",
                        lambda s, out, *, fmt, split=False:
                        seen.setdefault("a", (out, fmt)) or {"ok": True})
    srv.export_part("/tmp/x", fmt="step")
    assert seen["a"] == ("/tmp/x", "step")


def test_describe_part_delegates(monkeypatch):
    _ready(monkeypatch)
    monkeypatch.setattr(srv._session, "get_result_shape", lambda: "SHAPE")
    monkeypatch.setattr(srv._feedback_text, "describe_shape",
                        lambda shape: {"valid": True, "shape": shape})
    r = srv.describe_part()
    assert r["valid"] is True and r["shape"] == "SHAPE"


def test_describe_part_guard_not_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    assert srv.describe_part()["ok"] is False


def test_render_part_guard_not_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    assert srv.render_part()["ok"] is False


def test_render_part_returns_image(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: True)
    monkeypatch.setattr(srv._session, "get_result_shape", lambda: "SHAPE")
    seen = {}
    monkeypatch.setattr(srv._render, "render_png",
                        lambda shape, view="iso": seen.setdefault("view", view) or b"\x89PNG")
    from mcp.server.fastmcp import Image
    out = srv.render_part(view="front")
    assert isinstance(out, Image)
    assert seen["view"] == "front"


def test_export_part_failure_is_structured(monkeypatch):
    """export_part 内部抛出 RuntimeError 时，server 应返回结构化 ok:False dict，而非抛出异常。"""
    _ready(monkeypatch)

    # Round 8 升格：export_part 新增 split 参数
    def _boom(s, out, *, fmt, split=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv._export, "export_part", _boom)
    r = srv.export_part("/tmp/x")
    assert r["ok"] is False
    assert "boom" in r["message"]
