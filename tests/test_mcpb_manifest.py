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
    "query_freecad_runtime_capabilities",
    "create_project",
    "get_project",
    "list_projects",
    "list_revisions",
    "compare_revisions",
    "revert_project",
    "create_task",
    "list_tasks",
    "get_task",
    "get_task_events",
    "submit_model_program",
    "resume_task",
    "cancel_task",
    "accept_draft",
    "reject_draft",
    "get_artifact_manifest",
    "export_task_artifacts",
    "create_release",
    "get_release",
    "approve_release",
    "create_reconstruction",
    "get_reconstruction",
    "run_reconstruction",
    "answer_reconstruction",
    "adopt_reconstruction",
    "reject_reconstruction",
    "delete_reconstruction",
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
    descriptions = dict(declared)
    assert descriptions["compare_revisions"] == ("比较同一项目两个已提交版本的谱系、清单和制品差异")
    assert descriptions["revert_project"] == (
        "复制历史已提交版本，创建基于当前 HEAD 的经验证待审核草案"
    )
    assert descriptions["get_artifact_manifest"] == (
        "读取任务版本的验证绑定、制品清单和现有交付资源"
    )
    assert descriptions["cancel_task"] == "请求取消指定任务并返回持久化状态"
    assert descriptions["create_release"] == "为已验收版本生成可预览的机械交付包草稿"
    assert descriptions["approve_release"] == "批准精确摘要绑定的不可变机械交付包"


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


def test_manifest_claims_only_verified_agent_surface_on_supported_platforms():
    manifest = _manifest()
    claims = f"{manifest['description']}\n{manifest['long_description']}"
    assert manifest["compatibility"]["platforms"] == ["darwin", "win32"]
    for required in (
        "持久化项目",
        "ModelProgram",
        "FCStd",
        "STEP",
        "Sketcher",
        "PartDesign",
        "2–16",
        "create_box",
        "rotate_part",
        "Windows x86-64",
    ):
        assert required in claims
    for unsupported in (
        "圆角",
        "装配",
        "干涉",
        "STL",
        "Workbench",
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
        ".codex",
        ".agents",
        ".workbuddy",
        "__pycache__",
        ".pytest_cache",
        "tests/",
        "docs/",
        ".github/",
        ".vibecad",
        "dist/",
        "CAD_Theory_Course_*.md",
    ):
        assert pattern in ignore, f".mcpbignore 缺 {pattern}"


def test_packaged_readme_describes_the_current_agent_first_surface():
    english_path = ROOT / "README.md"
    chinese_path = ROOT / "README.zh-CN.md"
    assert english_path.is_file()
    assert chinese_path.is_file()

    english_readme = " ".join(english_path.read_text(encoding="utf-8").split())
    chinese_readme = " ".join(chinese_path.read_text(encoding="utf-8").split())
    assert "[简体中文](README.zh-CN.md)" in english_readme
    assert "[English](README.md)" in chinese_readme

    for required in (
        "Current Agent-first Workflow",
        "user's own host model",
        "An FCStd import must be non-empty",
        "`Part::Box`",
        "`Part::Cylinder`",
        "create_project",
        "submit_model_program",
        "cancel_task",
        "accept_draft",
        "export_task_artifacts",
        "notifications/cancelled",
        "0.9.0",
        "39 tools",
        "daemon",
        "Task Kernel",
        "G1",
        "P1",
        "P2",
        "FreeCAD Workbench Alpha",
        "exact object/feature selector capture",
        "Open Editable HEAD",
        "Save** stays local",
        "Checkpoint Edit",
        "there is no automatic merge or rebase",
        "universal photo reconstruction, and simulation are not currently supported",
        "multimodal host can pilot bounded image-to-CAD",
        "VibeCAD does not need the host's model credential",
        "defaults to the deterministic fake Provider",
        "create_reconstruction",
        "Direct WorkBuddy attachment ingress into VibeCAD's sealed store remains unverified",
        "39-tool discovery",
        "WorkBuddy (verified)",
    ):
        assert required in english_readme

    for required in (
        "当前 Agent-first 工作流",
        "用户自己的宿主模型",
        "FCStd 导入必须非空",
        "`Part::Box`",
        "`Part::Cylinder`",
        "create_project",
        "submit_model_program",
        "cancel_task",
        "accept_draft",
        "export_task_artifacts",
        "notifications/cancelled",
        "0.9.0",
        "39 个工具",
        "daemon",
        "Task Kernel",
        "G1",
        "P1",
        "P2",
        "FreeCAD Workbench Alpha",
        "精确 object/feature selector 捕获",
        "Open Editable HEAD",
        "Save** 只保存在本地",
        "Checkpoint Edit",
        "系统不做自动 merge 或 rebase",
        "普适照片重建或 simulation",
        "多模态宿主可通过普通待审核 Task 流程试点受控 image-to-CAD",
        "VibeCAD 不需要 宿主模型凭据",
        "默认使用 deterministic fake Provider",
        "create_reconstruction",
        "WorkBuddy 附件直接进入 VibeCAD sealed store 仍未验证",
        "39-tool discovery",
        "WorkBuddy（已验证）",
    ):
        assert required in chinese_readme
    for removed_endpoint in (
        "`smoke_cad`",
        "`new_document`",
        "`add_hole`",
        "`fillet_edges`",
        "`render_part`",
        "`new_part`",
        "`export_part`",
    ):
        assert removed_endpoint not in english_readme
        assert removed_endpoint not in chinese_readme

    with (ROOT / "pyproject.toml").open("rb") as handle:
        assert tomllib.load(handle)["project"]["readme"] == "README.md"

    roadmap = (ROOT / "docs/PRODUCT_CAPABILITY_ROADMAP.md").read_text(encoding="utf-8")
    normalized_roadmap = " ".join(roadmap.replace("\n> ", " ").split())
    for required in (
        "0.9.0",
        "39-tool 公共 MCP、durable review/release/visual",
        "host-neutral skill",
        "P0-B core backend（已完成）",
        "durable active cancellation",
        "G1 Workbench Alpha 已完成",
    ):
        assert required in normalized_roadmap
    assert "S3-8" in normalized_roadmap
    assert "P0-A" in normalized_roadmap
    assert "宿主 skill 和 FreeCAD 交互插件尚未交付" not in normalized_roadmap
