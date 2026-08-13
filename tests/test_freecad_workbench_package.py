from __future__ import annotations

import ast
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_ROOT = _REPO_ROOT / "freecad" / "VibeCAD"
_EXPECTED_FILES = {
    "Init.py",
    "InitGui.py",
    "package.xml",
    "vibecad_workbench/__init__.py",
    "vibecad_workbench/bridge.py",
    "vibecad_workbench/dock.py",
    "vibecad_workbench/gateway.py",
    "vibecad_workbench/host.py",
    "vibecad_workbench/preview.py",
    "vibecad_workbench/selection.py",
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
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }

    assert files == _EXPECTED_FILES


def test_wheel_and_sdist_contain_the_complete_addon(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    for target in ("wheel", "sdist"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hatchling",
                "build",
                "-t",
                target,
                "-d",
                str(output),
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    (wheel,) = output.glob("*.whl")
    (sdist,) = output.glob("*.tar.gz")

    with zipfile.ZipFile(wheel) as archive:
        packaged = {
            name.removeprefix("vibecad/_freecad/VibeCAD/")
            for name in archive.namelist()
            if name.startswith("vibecad/_freecad/VibeCAD/") and not name.endswith("/")
        }
    assert packaged == _EXPECTED_FILES

    with tarfile.open(sdist, "r:gz") as archive:
        source = {
            "/".join(name.split("/freecad/VibeCAD/", 1)[1:])
            for name in archive.getnames()
            if "/freecad/VibeCAD/" in name and not name.endswith("/")
        }
    assert source == _EXPECTED_FILES


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
    assert root.findtext("version") == "0.10.0"
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


def test_external_addon_transport_and_selection_import_without_vibecad_package() -> None:
    source = (
        "import sys; "
        f"sys.path.insert(0, {str(_ADDON_ROOT)!r}); "
        "import vibecad_workbench.bridge; "
        "import vibecad_workbench.selection; "
        "assert 'vibecad' not in sys.modules"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
