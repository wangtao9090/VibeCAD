"""mcpb manifest 守卫：版本三处同步、工具表与 server 注册一致、uv 配置、内容物排除。快测。"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    return json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))


def test_version_synced_three_ways():
    m = _manifest()
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    py_ver = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)
    init = (ROOT / "src/vibecad/__init__.py").read_text(encoding="utf-8")
    init_ver = re.search(r'__version__ = "([^"]+)"', init).group(1)
    assert m["version"] == py_ver == init_ver


def test_manifest_tools_match_server_registry():
    """manifest.tools 必须与 server 注册的工具完全一致（防加工具忘更新 manifest）。"""
    import vibecad.server as server
    registered = {t.name for t in server.mcp._tool_manager.list_tools()}
    declared = {t["name"] for t in _manifest()["tools"]}
    assert declared == registered, (
        f"manifest 缺 {registered - declared}，多 {declared - registered}")


def test_manifest_uv_type_entry_and_mcp_config():
    m = _manifest()
    assert m["server"]["type"] == "uv"
    assert (ROOT / m["server"]["entry_point"]).exists()
    assert m["server"]["mcp_config"]["command"] == "uv"  # CLI 2.1.2 必填（审查实锤）
    assert m["server"]["entry_point"] in " ".join(m["server"]["mcp_config"]["args"])
    assert "user_config" not in m          # 设计：零配置表单
    assert (ROOT / m["icon"]).exists()     # 安装弹窗/扩展列表展示


def test_mcpbignore_excludes_heavy_dirs():
    ignore = (ROOT / ".mcpbignore").read_text(encoding="utf-8")
    for pattern in (".venv", ".claude", "__pycache__", ".pytest_cache",
                    "tests/", "docs/", ".github/", ".vibecad", "dist/"):
        assert pattern in ignore, f".mcpbignore 缺 {pattern}"
