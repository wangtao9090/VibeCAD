# tests/test_server_round6.py
"""server Round6a：multi 视图、六工具自动附图、附图失败不连坐、协议契约。"""
import json

import pytest


@pytest.fixture()
def server(monkeypatch):
    import vibecad.server as srv
    monkeypatch.setattr(srv, "_runtime_guard", lambda: None)
    return srv


def _mock_multiview(server, monkeypatch, png=b"\x89PNG mv"):
    monkeypatch.setattr(server._multiview, "render_multiview",
                        lambda shape: (png, {"A": "顶面"}, {"A": {}}, {"E1": {}}))


def test_render_part_view_multi(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    recorded = {}
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: recorded.update(s=shown))
    out = server.render_part(view="multi")
    assert isinstance(out, list) and isinstance(out[0], Image)
    assert json.loads(out[1])["labels"]["A"] == "顶面"
    assert recorded["s"] == {"A"}


def test_render_part_view_multi_with_annotate_rejected(server, monkeypatch):
    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    out = server.render_part(view="multi", annotate="faces")
    assert out["ok"] is False  # multi 已含标注 iso 格，组合无意义须显式拒绝


def test_add_box_attaches_view(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc, session=None: {"Part1": {"Box": {"length": 40.0}}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)
    out = server.add_box(length=40, width=30, height=20)
    assert isinstance(out, list) and isinstance(out[1], Image)
    body = out[0]
    assert body["ok"] is True and body["labels"] == {"A": "顶面"}
    assert "labels_stale" not in body and "hint" not in body
    assert "parts" in body  # 断言升格：每步结果附 parts 字段（新形态 {零件: {对象: {参数}}}）


def test_attach_view_render_failure_not_fatal(server, monkeypatch):
    """附图失败不连坐：操作 ok:True 保留 + render_error + 退回 stale 提示。
    异常用 TypeError 验证宽抓（事务后纯展示阶段任何异常都不得连坐——
    实测 TechDraw 会抛 TypeError，窄抓 RuntimeError/ValueError 会漏）。"""
    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})

    def _boom(shape):
        raise TypeError("渲染炸了")

    class _Shape:
        pass

    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    monkeypatch.setattr(server._multiview, "render_multiview", _boom)
    out = server.add_box(length=40, width=30, height=20)
    assert isinstance(out, dict) and out["ok"] is True
    assert "渲染炸了" in out["render_error"] and out["labels_stale"] is True
    assert "TypeError" in out["render_error"]  # 异常类型名进 render_error 便于排障


def test_attach_view_failure_preserves_tool_hint(server, monkeypatch):
    """setdefault 语义：tools 层自带的 hint（如 fillet 的 annotate='edges'）
    不被附图失败的兜底 faces 提示覆盖降级。"""
    monkeypatch.setattr(server._features, "fillet_edges",
                        lambda session, edges, radius:
                        {"ok": True, "volume": 100.0, "labels_stale": True,
                         "hint": "几何已变更，调用 render_part(annotate='edges') 查看最新边标注"})

    def _boom(shape):
        raise RuntimeError("渲染炸了")

    monkeypatch.setattr(server._session, "get_result_shape", lambda: object())
    monkeypatch.setattr(server._multiview, "render_multiview", _boom)
    out = server.fillet_edges(edges=["E1"], radius=2)
    assert isinstance(out, dict) and out["ok"] is True
    assert "annotate='edges'" in out["hint"]  # tools 层 edges hint 保留，不被降级成 faces
    assert out["labels_stale"] is True and "渲染炸了" in out["render_error"]


def test_failed_tool_attaches_nothing(server, monkeypatch):
    def _fail(session, length, width, height, position):
        raise ValueError("length 必须 > 0")

    monkeypatch.setattr(server._modeling, "add_box", _fail)
    out = server.add_box(length=-1, width=30, height=20)
    assert isinstance(out, dict) and out["ok"] is False


def test_mcp_contract_tool_with_image(server, monkeypatch):
    """协议契约：[dict, Image] → [TextContent(json), ImageContent]；
    并锁死六工具 -> Any 签名后 structuredContent 为 None 的现状。"""
    import anyio
    from mcp.types import TextContent

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    content = anyio.run(lambda: server.mcp.call_tool("add_box",
                                                     {"length": 40, "width": 30, "height": 20}))
    # structuredContent 契约固化：-> Any 签名无 output_schema，mcp 1.x 实跑
    # call_tool 直接返回 content list（不带 structured 元组）；若 mcp 升级改为
    # 恒返回 (content, structured) 元组，structured 也必须为 None/空 dict——
    # 否则客户端对六工具返回的展示行为会悄悄变化。
    structured = None
    if isinstance(content, tuple):
        content, structured = content
    assert structured in (None, {})
    kinds = [type(c).__name__ for c in content]
    assert "ImageContent" in kinds and "TextContent" in kinds
    payload = json.loads([c for c in content if isinstance(c, TextContent)][0].text)
    assert payload["ok"] is True


def test_modify_part_delegates_and_attaches(server, monkeypatch):
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._modify, "modify_part",
                        lambda session, name, parameter, value:
                        {"ok": True, "modified": {"name": name, "parameter": parameter,
                                                  "from": 40.0, "to": value}})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc, session=None: {"Part1": {"Box": {"length": 45.0}}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)
    out = server.modify_part(name="Box", parameter="length", value=45)
    assert isinstance(out, list) and isinstance(out[1], Image)
    # 断言升格：新形态 {零件: {对象: {参数}}}
    assert out[0]["modified"]["to"] == 45
    assert out[0]["parts"] == {"Part1": {"Box": {"length": 45.0}}}


def test_attach_view_includes_parts(server, monkeypatch):
    from mcp.server.fastmcp import Image  # noqa: F401

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})
    monkeypatch.setattr(server._modify, "list_parameters",
                        lambda doc, session=None: {"Part1": {"Box": {"length": 40.0}}})
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)
    out = server.add_box(length=40, width=30, height=20)
    # 断言升格：新形态 {零件: {对象: {参数}}}
    assert out[0]["parts"] == {"Part1": {"Box": {"length": 40.0}}}


def test_modify_part_failure_structured(server, monkeypatch):
    def _boom(session, name, parameter, value):
        raise ValueError("参数 length 已是 45")

    monkeypatch.setattr(server._modify, "modify_part", _boom)
    out = server.modify_part(name="Box", parameter="length", value=45)
    assert out["ok"] is False and "已是" in out["message"]


def test_attach_view_parts_failure_not_fatal(server, monkeypatch):
    """parts 清单失败不连坐：渲染成功路径保留（labels+Image 照常），parts 兜底空 dict
    ——不得把已成功的渲染降级到 render_error 路径（语义矛盾）。"""
    from mcp.server.fastmcp import Image

    class _Shape:
        pass

    monkeypatch.setattr(server._modeling, "add_box",
                        lambda session, length, width, height, position:
                        {"ok": True, "name": "Box", "volume": 24000.0})

    def _boom(doc, session=None):
        raise RuntimeError("参数清单炸了")

    monkeypatch.setattr(server._modify, "list_parameters", _boom)
    monkeypatch.setattr(server._session, "get_result_shape", lambda: _Shape())
    _mock_multiview(server, monkeypatch)
    monkeypatch.setattr(server._session, "set_labels",
                        lambda faces, edges, shown=None: None)
    monkeypatch.setattr(type(server._session), "doc", property(lambda self: object()),
                        raising=False)
    out = server.add_box(length=40, width=30, height=20)
    assert isinstance(out, list) and isinstance(out[1], Image)
    assert out[0]["parts"] == {} and out[0]["labels"] == {"A": "顶面"}
    assert "render_error" not in out[0]
