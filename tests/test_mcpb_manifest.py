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


def test_manifest_env_auto_install_only():
    """Round 11：宿主一拉起即自动后台装运行时。Spike Q3 已否决 VIBECAD_HOME=
    ${__dirname}/runtime（升级会清目录重建，见 plan Spike 结果节）——env 只留
    VIBECAD_AUTO_INSTALL，运行时路径保持默认（扩展目录外）。"""
    m = _manifest()
    env = m["server"]["mcp_config"]["env"]
    assert env == {"VIBECAD_AUTO_INSTALL": "1"}


def test_manifest_uninstall_runtime_destructive_hint():
    """uninstall_runtime 必须显式标注 destructiveHint=True（目录上架审查关注卸载）。"""
    import vibecad.server as server

    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    assert tools["uninstall_runtime"].annotations.destructiveHint is True


def test_manifest_long_description_mentions_clean_uninstall():
    """long_description 需带"卸载零残留"卖点（Round 11 设计明细 3.3）。"""
    m = _manifest()
    assert "残留" in m["long_description"] or "全删" in m["long_description"]


def test_mcpbignore_excludes_heavy_dirs():
    ignore = (ROOT / ".mcpbignore").read_text(encoding="utf-8")
    for pattern in (".venv", ".claude", "__pycache__", ".pytest_cache",
                    "tests/", "docs/", ".github/", ".vibecad", "dist/"):
        assert pattern in ignore, f".mcpbignore 缺 {pattern}"
