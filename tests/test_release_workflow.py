import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / ".github" / "scripts" / "check_release_versions.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _write_version_fixture(root: Path, version: str = "0.4.0") -> None:
    (root / "src" / "vibecad").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "vibecad"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "manifest.json").write_text(
        json.dumps({"version": version}), encoding="utf-8"
    )
    (root / "src" / "vibecad" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )


def _run_guard(root: Path, tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), tag, "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_version_guard_accepts_four_matching_versions(tmp_path):
    _write_version_fixture(tmp_path)
    result = _run_guard(tmp_path, "v0.4.0")
    assert result.returncode == 0, result.stderr
    assert "校验通过" in result.stdout


@pytest.mark.parametrize(
    ("location", "replacement", "expected_name"),
    [
        ("tag", "v0.4.1", "tag=0.4.1"),
        ("pyproject", 'version = "0.4.1"', "pyproject.toml=0.4.1"),
        ("manifest", "0.4.1", "manifest.json=0.4.1"),
        ("source", '__version__ = "0.4.1"\n', "vibecad.__version__=0.4.1"),
    ],
)
def test_release_version_guard_rejects_each_mismatch(
    tmp_path, location, replacement, expected_name
):
    _write_version_fixture(tmp_path)
    tag = "v0.4.0"
    if location == "tag":
        tag = replacement
    elif location == "pyproject":
        path = tmp_path / "pyproject.toml"
        path.write_text(
            path.read_text(encoding="utf-8").replace('version = "0.4.0"', replacement),
            encoding="utf-8",
        )
    elif location == "manifest":
        (tmp_path / "manifest.json").write_text(
            json.dumps({"version": replacement}), encoding="utf-8"
        )
    else:
        (tmp_path / "src" / "vibecad" / "__init__.py").write_text(
            replacement, encoding="utf-8"
        )

    result = _run_guard(tmp_path, tag)
    assert result.returncode == 1
    assert expected_name in result.stderr


def test_release_workflow_gates_both_artifacts_with_stdlib_script():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert 'python3 .github/scripts/check_release_versions.py "$GITHUB_REF_NAME"' in workflow
    assert re.search(r"(?m)^  pypi:\n    needs: version-guard$", workflow)
    assert re.search(r"(?m)^  mcpb:\n    needs: version-guard$", workflow)


def test_release_workflow_uses_explicit_least_privilege_permissions():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "permissions:\n  contents: read\n\njobs:" in workflow
    assert re.search(
        r"(?m)^  pypi:.*?^    permissions:\n"
        r"      contents: read\n"
        r"      id-token: write$",
        workflow,
        flags=re.DOTALL,
    )
    assert re.search(
        r"(?m)^  mcpb:.*?^    permissions:\n"
        r"      contents: write(?:[ \t]+#.*)?$",
        workflow,
        flags=re.DOTALL,
    )
    assert re.search(
        r"(?m)^  mcpb:.*?^      - uses: actions/checkout@v4\n"
        r"        with:\n          persist-credentials: false$",
        workflow,
        flags=re.DOTALL,
    )


def test_current_repository_versions_pass_release_guard():
    with (ROOT / "pyproject.toml").open("rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    result = _run_guard(ROOT, f"v{version}")
    assert result.returncode == 0, result.stderr
