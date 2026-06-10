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
