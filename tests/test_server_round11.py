"""server Round 11：视觉落盘——_attach_view 加 view_file + render_part 加 save_to（TDD）。

参照 test_server_new_tools.py / test_server_round6.py 的 monkeypatch 范式。
"""
from __future__ import annotations

import json
from pathlib import Path

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
