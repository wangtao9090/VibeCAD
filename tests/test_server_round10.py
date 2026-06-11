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


def test_readonly_classification():
    """只读工具集合显式锁死（防误标）。"""
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    readonly = {n for n, t in tools.items() if t.annotations.readOnlyHint}
    assert readonly == {"ping", "get_runtime_status", "describe_part", "render_part"}
