import ast
import hashlib
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_CONFORMANCE = _ROOT / "src/vibecad/runtime/conformance.py"
_CAD_CONFORMANCE = _ROOT / "src/vibecad/interaction/cad_conformance.py"


def test_runtime_imports_without_mcp():
    # 模拟 launcher 在无 mcp 的临时 env：import runtime 子模块不得拉 mcp
    code = (
        "import sys, importlib;"
        "import vibecad.runtime.paths, vibecad.runtime.status, vibecad.runtime.platform,"
        " vibecad.runtime.micromamba, vibecad.runtime.installer;"
        "assert 'mcp' not in sys.modules, 'runtime 不应拉起 mcp';"
        "print('pure-stdlib OK')"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "pure-stdlib OK" in r.stdout


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            result.add(node.module)
    return result


def test_conformance_source_has_pure_layered_imports_and_no_assert_or_pytest():
    runtime_tree = ast.parse(
        _RUNTIME_CONFORMANCE.read_text(encoding="utf-8"),
        filename=str(_RUNTIME_CONFORMANCE),
    )
    cad_tree = ast.parse(
        _CAD_CONFORMANCE.read_text(encoding="utf-8"),
        filename=str(_CAD_CONFORMANCE),
    )
    runtime_vibecad_imports = {
        name for name in _imports(_RUNTIME_CONFORMANCE) if name.startswith("vibecad.")
    }
    cad_vibecad_imports = {
        name for name in _imports(_CAD_CONFORMANCE) if name.startswith("vibecad.")
    }

    assert runtime_vibecad_imports <= {
        "vibecad.runtime.contracts",
        "vibecad.runtime.registry",
    }
    assert cad_vibecad_imports <= {
        "vibecad.interaction.cad_runtime",
        "vibecad.runtime.conformance",
        "vibecad.runtime.contracts",
    }
    assert not any(
        isinstance(node, ast.Assert) for tree in (runtime_tree, cad_tree) for node in ast.walk(tree)
    )
    assert "pytest" not in _imports(_RUNTIME_CONFORMANCE)
    assert "pytest" not in _imports(_CAD_CONFORMANCE)


def test_conformance_imports_load_no_forbidden_runtime_or_product_modules():
    code = r"""
import sys

import vibecad.interaction.cad_runtime
import vibecad.runtime.contracts

before = set(sys.modules)
import vibecad.runtime.conformance
import vibecad.interaction.cad_conformance

forbidden = (
    "FreeCAD",
    "PySide",
    "PyQt",
    "vibecad.application",
    "vibecad.execution.worker_port",
    "vibecad.interaction.checkouts",
    "vibecad.public",
    "vibecad.revision",
    "vibecad.store",
    "vibecad.task",
    "vibecad.workflow",
)
loaded = sorted(
    name
    for name in set(sys.modules) - before
    if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
)
if loaded:
    raise SystemExit(repr(loaded))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(_ROOT / "src")},
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_conformance_works_under_optimized_mode_without_assert_semantics():
    code = r"""
from vibecad.interaction.cad_conformance import evaluate_cad_runtime_conformance
from vibecad.runtime.conformance import evaluate_runtime_conformance

runtime_report = evaluate_runtime_conformance((object(),))
cad_report = evaluate_cad_runtime_conformance((object(),))
runtime_codes = tuple(item.code for item in runtime_report.findings)
cad_codes = tuple(item.code for item in cad_report.findings)
if runtime_report.conforms or runtime_codes != ("invalid_case",):
    raise SystemExit(("runtime", runtime_codes))
if cad_report.conforms or cad_codes != ("cad_invalid_case",):
    raise SystemExit(("cad", cad_codes))
"""
    completed = subprocess.run(
        [sys.executable, "-O", "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(_ROOT / "src")},
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_conformance_does_not_require_package_initializer_edits():
    runtime_initializer = (_ROOT / "src/vibecad/runtime/__init__.py").read_bytes()
    interaction_initializer = (_ROOT / "src/vibecad/interaction/__init__.py").read_bytes()
    # The invariant is source content, not Git's Windows checkout conversion.
    # Keep the canonical LF pins used on macOS/Linux while comparing the same
    # source bytes on a CRLF Windows worktree.
    if sys.platform == "win32":
        runtime_initializer = runtime_initializer.replace(b"\r\n", b"\n")
        interaction_initializer = interaction_initializer.replace(b"\r\n", b"\n")
    assert (
        hashlib.sha256(runtime_initializer).hexdigest()
        == "217184fec30d06cbe7f79f0c54589462f2ef1f23afb4ec75c36d37e02b86dee1"
    )
    assert (
        hashlib.sha256(interaction_initializer).hexdigest()
        == "f1e9b6e50b2042c09dff60d024a6fbf53ee09f2507b6b66dfa0423de9ae776a5"
    )
