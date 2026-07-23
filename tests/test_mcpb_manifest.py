"""MCPB 守卫：冻结依赖、Agent-first 公开面与诚实的平台声明。"""

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_PUBLIC_TOOLS = (
    "ping",
    "get_runtime_status",
    "ensure_runtime",
    "uninstall_runtime",
    "get_capabilities",
    "create_project",
    "get_project",
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_events",
    "submit_model_program",
    "resume_task",
    "accept_draft",
    "reject_draft",
    "export_task_artifacts",
    "create_box",
    "create_cylinder",
    "inspect_model",
    "modify_parameter",
    "move_part",
    "rotate_part",
)


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
    """manifest 必须与独立冻结合同和 registry 投影同序同描述。"""
    from vibecad.application.public_surface import public_tool_specs

    declared = tuple((entry["name"], entry["description"]) for entry in _manifest()["tools"])
    projected = tuple((spec.name, spec.description) for spec in public_tool_specs())
    assert tuple(name for name, _description in declared) == EXPECTED_PUBLIC_TOOLS
    assert tuple(name for name, _description in projected) == EXPECTED_PUBLIC_TOOLS
    assert declared == projected


def test_manifest_tool_entries_are_unique_and_described():
    entries = _manifest()["tools"]
    names = [entry["name"] for entry in entries]
    assert len(names) == len(set(names))
    assert all(
        isinstance(entry.get("description"), str) and entry["description"].strip()
        for entry in entries
    )


def test_manifest_uv_type_entry_and_mcp_config():
    m = _manifest()
    assert m["server"]["type"] == "uv"
    assert (ROOT / m["server"]["entry_point"]).exists()
    assert m["server"]["mcp_config"]["command"] == "uv"  # CLI 2.1.2 必填（审查实锤）
    assert m["server"]["mcp_config"]["args"] == [
        "run",
        "--frozen",
        "--no-dev",
        "--no-editable",
        "--no-build-isolation",
        "--directory",
        "${__dirname}",
        "mcpb_entry.py",
    ]
    assert "user_config" not in m  # 设计：零配置表单
    assert (ROOT / m["icon"]).exists()  # 安装弹窗/扩展列表展示


def test_manifest_env_auto_install_only():
    """Round 11：宿主一拉起即自动后台装运行时。Spike Q3 已否决 VIBECAD_HOME=
    ${__dirname}/runtime（升级会清目录重建，见 plan Spike 结果节）——env 只留
    VIBECAD_AUTO_INSTALL，运行时路径保持默认（扩展目录外）。"""
    m = _manifest()
    env = m["server"]["mcp_config"]["env"]
    assert env == {"VIBECAD_AUTO_INSTALL": "1"}


def test_manifest_long_description_describes_two_step_uninstall():
    """运行时位于扩展目录外，描述必须给出准确的两步卸载流程。"""
    description = _manifest()["long_description"]
    assert "uninstall_runtime" in description
    assert "预览和确认两段式" in description
    assert "设置中移除扩展本体" in description
    assert "移除扩展即可连引擎一起删除" not in description


def test_manifest_claims_only_verified_agent_surface_on_darwin():
    manifest = _manifest()
    claims = f"{manifest['description']}\n{manifest['long_description']}"
    assert manifest["compatibility"]["platforms"] == ["darwin"]
    for required in (
        "持久化项目",
        "ModelProgram",
        "FCStd",
        "STEP",
        "create_box",
        "rotate_part",
    ):
        assert required in claims
    for unsupported in (
        "打孔",
        "圆角",
        "三视图",
        "装配",
        "干涉",
        "STL",
        "Workbench",
        "Windows",
    ):
        assert unsupported not in claims


def test_mcp_dependency_and_lock_are_exact_and_packaged():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    project = pyproject["project"]
    assert "mcp==1.27.2" in project["dependencies"]
    assert "hatchling==1.28.0" in project["dependencies"]
    assert pyproject["build-system"] == {
        "requires": ["hatchling==1.28.0"],
        "build-backend": "hatchling.build",
    }

    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "mcp"\nversion = "1.27.2"' in lock
    assert '{ name = "mcp", specifier = "==1.27.2" }' in lock
    assert 'name = "hatchling"\nversion = "1.28.0"' in lock
    assert '{ name = "hatchling", specifier = "==1.28.0" }' in lock

    ignored = {
        line.strip()
        for line in (ROOT / ".mcpbignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "uv.lock" not in ignored


def test_mcpbignore_excludes_heavy_dirs():
    ignore = (ROOT / ".mcpbignore").read_text(encoding="utf-8")
    for pattern in (
        ".venv",
        ".claude",
        "__pycache__",
        ".pytest_cache",
        "tests/",
        "docs/",
        ".github/",
        ".vibecad",
        "dist/",
    ):
        assert pattern in ignore, f".mcpbignore 缺 {pattern}"


def test_packaged_readme_describes_only_the_agent_first_surface():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    for required in (
        "当前 Agent-first 工作流",
        "用户自己的宿主模型",
        "FCStd 导入必须非空",
        "`Part::Box`",
        "`Part::Cylinder`",
        "create_project",
        "submit_model_program",
        "accept_draft",
        "export_task_artifacts",
        "G1",
        "P1",
        "P2",
        "G1 Workbench 尚未交付",
        "STEP/STL 导入、逆向工程和仿真 尚未接入",
    ):
        assert required in normalized_readme
    for removed_endpoint in (
        "`smoke_cad`",
        "`new_document`",
        "`add_hole`",
        "`fillet_edges`",
        "`render_part`",
        "`new_part`",
        "`export_part`",
    ):
        assert removed_endpoint not in normalized_readme

    roadmap = (ROOT / "docs/PRODUCT_CAPABILITY_ROADMAP.md").read_text(encoding="utf-8")
    normalized_roadmap = " ".join(roadmap.replace("\n> ", " ").split())
    for required in (
        "S3-8/P0-A",
        "22-tool 公共 MCP、durable review",
        "host-neutral skill",
        "P0-B core 正在执行",
    ):
        assert required in normalized_roadmap
    assert "宿主 skill 和 FreeCAD 交互插件尚未交付" not in normalized_roadmap
