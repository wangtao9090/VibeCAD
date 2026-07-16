"""R10：serverInfo.version 报包版本 + 工具行为标注（目录上架硬要求）。快测。"""
import vibecad
import vibecad.server as server


def test_server_reports_package_version():
    """serverInfo.version 必须报 vibecad 包版本而非 mcp SDK 版本（R9 发现）。"""
    opts = server.mcp._mcp_server.create_initialization_options()
    assert opts.server_version == vibecad.__version__


def test_all_tools_have_annotations():
    """每个工具必须带 ToolAnnotations（readOnlyHint 至少显式声明）。"""
    tools = server.mcp._tool_manager.list_tools()
    missing = [t.name for t in tools
               if t.annotations is None or t.annotations.readOnlyHint is None]
    assert not missing, f"缺 annotations 的工具：{missing}"


def test_tool_annotation_safety_mapping():
    """31 个工具的读写、破坏性、幂等和联网提示必须与真实副作用一致。"""
    expected = {
        "ping": (True, None, None, False),
        "describe_part": (True, None, None, False),
        "get_runtime_status": (False, False, True, False),
        "ensure_runtime": (False, False, True, True),
        "smoke_cad": (False, True, True, False),
        "export_part": (False, True, True, False),
        "render_part": (False, True, True, False),
        "uninstall_runtime": (False, True, True, False),
        "new_document": (False, True, False, False),
        "save_project": (False, True, True, False),
        "open_project": (False, True, False, False),
        "delete_object": (False, True, False, False),
        "undo": (False, True, False, False),
        "redo": (False, True, False, False),
        "add_box": (False, False, False, False),
        "add_cylinder": (False, False, False, False),
        "boolean_cut": (False, False, False, False),
        "boolean_fuse": (False, False, False, False),
        "boolean_common": (False, False, False, False),
        "measure": (True, None, None, False),
        "add_hole": (False, False, False, False),
        "fillet_edges": (False, False, False, False),
        "chamfer_edges": (False, False, False, False),
        "modify_part": (False, False, False, False),
        "move_part": (False, False, False, False),
        "rotate_part": (False, False, False, False),
        "extrude_profile": (False, False, False, False),
        "new_part": (False, False, False, False),
        "set_active_part": (False, False, False, False),
        "place_part": (False, False, False, False),
        "align_parts": (False, False, False, False),
    }
    actual = {
        tool.name: (
            tool.annotations.readOnlyHint,
            tool.annotations.destructiveHint,
            tool.annotations.idempotentHint,
            tool.annotations.openWorldHint,
        )
        for tool in server.mcp._tool_manager.list_tools()
    }
    assert actual == expected
