from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_ROOT = _REPO_ROOT / "freecad" / "VibeCAD"
_EXPECTED_FILES = {
    "Init.py",
    "InitGui.py",
    "package.xml",
    "vibecad_workbench/__init__.py",
    "vibecad_workbench/state.py",
}


def _docstring_only_module(path: Path) -> ast.Module:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    assert len(module.body) == 1
    statement = module.body[0]
    assert isinstance(statement, ast.Expr)
    assert isinstance(statement.value, ast.Constant)
    assert type(statement.value.value) is str
    return module


def test_classic_addon_layout_is_complete() -> None:
    files = {
        path.relative_to(_ADDON_ROOT).as_posix()
        for path in _ADDON_ROOT.rglob("*")
        if path.is_file()
    }

    assert _EXPECTED_FILES <= files


def test_package_xml_declares_local_vibecad_workbench() -> None:
    root = ET.parse(_ADDON_ROOT / "package.xml").getroot()

    assert root.tag == "package"
    assert root.attrib == {"format": "1"}
    assert [child.tag for child in root] == [
        "name",
        "description",
        "version",
        "maintainer",
        "license",
        "url",
        "content",
    ]
    assert root.findtext("name") == "VibeCAD"
    assert root.findtext("description") == "Thin-client FreeCAD workbench for VibeCAD."
    assert root.findtext("version") == "0.6.0"
    maintainer = root.find("maintainer")
    assert maintainer is not None
    assert maintainer.attrib == {"email": "wangtao9090@gmail.com"}
    assert maintainer.text == "Wang Tao"
    assert root.findtext("license") == "MIT"
    repository = root.find("url")
    assert repository is not None
    assert repository.attrib == {"type": "repository"}
    assert repository.text == "https://github.com/wangtao9090/VibeCAD"
    content = root.find("content")
    assert content is not None
    assert content.attrib == {}
    assert [child.tag for child in content] == ["workbench"]
    workbench = content.find("workbench")
    assert workbench is not None
    assert workbench.attrib == {}
    assert [child.tag for child in workbench] == ["classname"]
    assert workbench.findtext("classname") == "VibeCADWorkbench"


def test_init_and_workbench_package_imports_are_side_effect_free() -> None:
    for path in (
        _ADDON_ROOT / "Init.py",
        _ADDON_ROOT / "vibecad_workbench" / "__init__.py",
    ):
        module = _docstring_only_module(path)
        namespace = {"__name__": "_vibecad_docstring_test", "__file__": str(path)}
        exec(compile(module, str(path), "exec"), namespace)
        assert set(namespace) == {"__builtins__", "__doc__", "__file__", "__name__"}
