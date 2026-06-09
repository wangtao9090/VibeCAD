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

    def _fake_add_box(s, length, w, h):
        seen["a"] = (length, w, h)
        return {"ok": True}

    monkeypatch.setattr(srv._modeling, "add_box", _fake_add_box)
    assert srv.add_box(10, 20, 30)["ok"] is True
    assert seen["a"] == (10, 20, 30)


def test_add_cylinder_delegates(monkeypatch):
    _ready(monkeypatch)
    seen = {}
    monkeypatch.setattr(srv._modeling, "add_cylinder",
                        lambda s, r, h: seen.setdefault("a", (r, h)) or {"ok": True})
    srv.add_cylinder(5, 12)
    assert seen["a"] == (5, 12)


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
    monkeypatch.setattr(srv._export, "export_part",
                        lambda s, out, *, fmt: seen.setdefault("a", (out, fmt)) or {"ok": True})
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

    def _boom(s, out, *, fmt):
        raise RuntimeError("boom")

    monkeypatch.setattr(srv._export, "export_part", _boom)
    r = srv.export_part("/tmp/x")
    assert r["ok"] is False
    assert "boom" in r["message"]
