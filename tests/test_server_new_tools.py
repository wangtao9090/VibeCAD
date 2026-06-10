import vibecad.server as srv


def _ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: True)


def test_add_box_guard_not_ready(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: False)
    r = srv.add_box(10, 10, 10)
    assert r["ok"] is False and "未就绪" in r["message"]


def test_add_box_guard_needs_reconnect(monkeypatch):
    monkeypatch.setattr(srv._installer, "is_ready", lambda: True)
    monkeypatch.setattr(srv, "_in_conda_runtime", lambda: False)
    r = srv.add_box(10, 10, 10)
    assert r["ok"] is False and "重连" in r["message"]


def test_new_document_delegates(monkeypatch):
    _ready(monkeypatch)
    seen = {}

    def _fake_new_document(s, name):
        seen["a"] = (s, name)
        return {"ok": True, "name": name}

    monkeypatch.setattr(srv._modeling, "new_document", _fake_new_document)
    r = srv.new_document("P")
    assert r == {"ok": True, "name": "P"} and seen["a"][0] is srv._session and seen["a"][1] == "P"


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
