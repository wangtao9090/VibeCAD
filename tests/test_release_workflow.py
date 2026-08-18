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
    (root / "freecad" / "VibeCAD").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "vibecad"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "manifest.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (root / "src" / "vibecad" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    (root / "freecad" / "VibeCAD" / "package.xml").write_text(
        f"<package><version>{version}</version></package>\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text(
        f'[[package]]\nname = "vibecad"\nversion = "{version}"\n', encoding="utf-8"
    )


def _run_guard(root: Path, tag: str, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), tag, "--root", str(root), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _initialize_release_repository(root: Path, tag: str = "v0.4.0") -> str:
    _write_version_fixture(root)
    _git(root, "init", "--quiet")
    _git(root, "config", "user.email", "release-test@example.invalid")
    _git(root, "config", "user.name", "Release Test")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "release fixture")
    _git(root, "tag", "-a", tag, "-m", tag)
    return _git(root, "rev-parse", "HEAD")


def test_release_version_guard_accepts_six_matching_versions(tmp_path):
    _write_version_fixture(tmp_path)
    result = _run_guard(tmp_path, "v0.4.0")
    assert result.returncode == 0, result.stderr
    assert "校验通过" in result.stdout


def test_release_version_guard_binds_tag_event_and_clean_checkout(tmp_path):
    commit = _initialize_release_repository(tmp_path)
    result = _run_guard(
        tmp_path,
        "v0.4.0",
        "--expected-ref",
        "refs/tags/v0.4.0",
        "--expected-object",
        commit,
        "--require-clean",
    )
    assert result.returncode == 0, result.stderr
    assert "Git tag/commit/checkout 已绑定" in result.stdout


def test_release_version_guard_rejects_tag_on_another_commit(tmp_path):
    _initialize_release_repository(tmp_path)
    (tmp_path / "marker").write_text("later\n", encoding="utf-8")
    _git(tmp_path, "add", "marker")
    _git(tmp_path, "commit", "--quiet", "-m", "later commit")
    commit = _git(tmp_path, "rev-parse", "HEAD")

    result = _run_guard(
        tmp_path,
        "v0.4.0",
        "--expected-ref",
        "refs/tags/v0.4.0",
        "--expected-object",
        commit,
        "--require-clean",
    )
    assert result.returncode == 1
    assert "未解析到同一 commit" in result.stderr


@pytest.mark.parametrize(
    ("expected_ref", "expected_object", "expected_error"),
    [
        ("refs/heads/main", None, "workflow ref"),
        (None, "0" * 40, "git rev-parse"),
    ],
)
def test_release_version_guard_rejects_forged_event_identity(
    tmp_path, expected_ref, expected_object, expected_error
):
    commit = _initialize_release_repository(tmp_path)
    result = _run_guard(
        tmp_path,
        "v0.4.0",
        "--expected-ref",
        expected_ref or "refs/tags/v0.4.0",
        "--expected-object",
        expected_object or commit,
        "--require-clean",
    )
    assert result.returncode == 1
    assert expected_error in result.stderr


def test_release_version_guard_rejects_dirty_checkout(tmp_path):
    commit = _initialize_release_repository(tmp_path)
    (tmp_path / "untracked").write_text("drift\n", encoding="utf-8")
    result = _run_guard(
        tmp_path,
        "v0.4.0",
        "--expected-ref",
        "refs/tags/v0.4.0",
        "--expected-object",
        commit,
        "--require-clean",
    )
    assert result.returncode == 1
    assert "clean worktree" in result.stderr


@pytest.mark.parametrize(
    ("location", "replacement", "expected_name"),
    [
        ("tag", "v0.4.1", "tag=0.4.1"),
        ("pyproject", 'version = "0.4.1"', "pyproject.toml=0.4.1"),
        ("manifest", "0.4.1", "manifest.json=0.4.1"),
        ("source", '__version__ = "0.4.1"\n', "vibecad.__version__=0.4.1"),
        ("package_xml", "0.4.1", "freecad package.xml=0.4.1"),
        ("lock", "0.4.1", "uv.lock=0.4.1"),
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
    elif location == "source":
        (tmp_path / "src" / "vibecad" / "__init__.py").write_text(replacement, encoding="utf-8")
    elif location == "package_xml":
        (tmp_path / "freecad" / "VibeCAD" / "package.xml").write_text(
            f"<package><version>{replacement}</version></package>\n", encoding="utf-8"
        )
    else:
        (tmp_path / "uv.lock").write_text(
            f'[[package]]\nname = "vibecad"\nversion = "{replacement}"\n', encoding="utf-8"
        )

    result = _run_guard(tmp_path, tag)
    assert result.returncode == 1
    assert expected_name in result.stderr


def test_release_workflow_gates_publishers_with_version_quality_managed_and_package_jobs():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    guard = 'python3 .github/scripts/check_release_versions.py "$GITHUB_REF_NAME"'
    assert workflow.count(guard) == 2
    assert workflow.count('--expected-ref "$GITHUB_REF"') == 2
    assert workflow.count('--expected-object "$GITHUB_SHA"') == 2
    assert workflow.count("--require-clean") == 2
    assert re.search(r"(?m)^  quality:\n    needs: version-guard$", workflow)
    assert re.search(r"(?m)^  managed-agent:\n    needs: package-gate$", workflow)
    assert re.search(
        r"(?m)^  package-gate:\n"
        r"    needs: \[version-guard, quality\]$",
        workflow,
    )
    assert re.search(r"(?m)^  reviewed-attestation:\n    needs: package-gate$", workflow)
    assert re.search(
        r"(?m)^  pypi:\n"
        r"    needs: \[package-gate, managed-agent, reviewed-attestation\]$",
        workflow,
    )
    assert re.search(
        r"(?m)^  mcpb:\n"
        r"    needs: \[package-gate, managed-agent, reviewed-attestation\]$",
        workflow,
    )


def test_release_quality_gate_runs_on_the_supported_darwin_platform():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    quality = re.search(
        r"(?ms)^  quality:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert quality is not None
    assert re.search(r"(?m)^    runs-on: macos-latest$", quality.group("body"))


def test_release_workflow_executes_the_exact_built_artifacts_before_publish():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    package = re.search(
        r"(?ms)^  package-gate:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    managed = re.search(
        r"(?ms)^  managed-agent:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert package is not None and managed is not None
    package_body = package.group("body")
    managed_body = managed.group("body")

    assert "Python sources are not byte-identical across release channels" in package_body
    assert 'f"vibecad/_freecad/VibeCAD/' in package_body
    assert 'name.startswith("freecad/VibeCAD/")' in package_body
    assert "unexpected MCPB content" in package_body
    assert '"skills/vibecad-agent/"' in package_body
    assert '"src/vibecad/"' in package_body
    assert "fresh-install the exact wheel and sdist" in package_body
    assert 'uv pip install --python "$environment/bin/python" --no-deps "$artifact"' in package_body
    assert 'vibecad.__version__ == os.environ["EXPECTED_VERSION"]' in package_body
    assert (
        "spec.PUBLIC_SURFACE_SHA256 == "
        '"fa260ce63582a49bfb940bd65e013021e7387c44e50d4670e7bd83887f66f70d"'
    ) in package_body
    assert "assert len(public_tool_specs()) == 39" in package_body

    assert managed_body.count("actions/download-artifact@v4") == 2
    assert "name: python-distributions" in managed_body
    assert "name: github-release-assets" in managed_body
    assert "VIBECAD_PIP_SPEC: ${{ steps.package.outputs.wheel }}" in managed_body
    assert "exact packed MCPB stdio/resource gate" in managed_body
    assert '@anthropic-ai/mcpb@2.1.2 unpack "$GATED_MCPB" "$unpacked"' in managed_body
    assert (
        "tests/test_runtime_integration.py::test_unpacked_mcpb_agent_first_stdio_acceptance"
    ) in managed_body


def test_release_reviewed_attestation_gate_covers_exact_trusted_macos_platforms():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    gate = re.search(
        r"(?ms)^  reviewed-attestation:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    assert gate is not None
    body = gate.group("body")

    assert re.search(r"(?m)^    runs-on: \$\{\{ matrix\.runner \}\}$", body)
    assert re.search(
        r"(?m)^          - platform_id: macos\.x86_64\n"
        r"            runner: macos-15-intel\n"
        r"          - platform_id: macos\.arm64\n"
        r"            runner: macos-15$",
        body,
    )
    assert "fail-fast: false" in body
    assert 'runtime_home="$RUNNER_TEMP/vibecad-reviewed-attestation"' in body
    assert 'test ! -e "$runtime_home"' in body
    assert body.count("actions/download-artifact@v4") == 1
    assert "name: python-distributions" in body
    assert "[[ ${#wheels[@]} -eq 1 ]]" in body
    assert "VIBECAD_PIP_SPEC: ${{ steps.package.outputs.wheel }}" in body
    assert 'uv pip install --python "$bootstrap/bin/python" --no-deps "$VIBECAD_PIP_SPEC"' in body
    assert "RuntimeInstaller().install()" in body
    assert "from vibecad.execution.freecad_discovery_runtime_v2 import _platform_id" in body
    assert "assert actual == expected" in body
    packaged_loader = "load_current_packaged_freecad_reviewed_release_attestation"
    generator = ".github/scripts/generate_freecad_reviewed_release_attestation.py --check"
    assert body.count(packaged_loader) == 2
    assert f"packaged={packaged_loader}()" in body
    assert "decode_freecad_reviewed_release_attestation(packaged.raw" in body
    assert "assert installed.is_relative_to(home)" in body
    assert "assert attestation.runtime_backend.platform_id == expected" in body
    assert "assert len(verification.receipts) == 21" in body
    assert "assert len(verification.formal_operations) == 126" in body
    assert "assert len(verification.native_types) == 102" in body
    assert body.index(f"packaged={packaged_loader}()") < body.index(generator)
    assert '"$VIBECAD_MANAGED_FREECAD_PYTHON" -I' in body
    assert generator in body


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
        r"(?m)^  package-gate:.*?^      - uses: actions/checkout@v4\n"
        r"        with:\n          fetch-depth: 0\n          persist-credentials: false$",
        workflow,
        flags=re.DOTALL,
    )
    pypi = re.search(r"(?ms)^  pypi:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", workflow)
    mcpb = re.search(r"(?ms)^  mcpb:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)", workflow)
    assert pypi is not None and mcpb is not None
    for publisher in (pypi.group("body"), mcpb.group("body")):
        assert "actions/download-artifact@v4" in publisher
        assert "actions/checkout@v4" not in publisher
    assert 'gh release create "$GITHUB_REF_NAME"' in mcpb.group("body")
    assert '--repo "$GITHUB_REPOSITORY"' in mcpb.group("body")


def test_current_repository_versions_pass_release_guard():
    with (ROOT / "pyproject.toml").open("rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    result = _run_guard(ROOT, f"v{version}")
    assert result.returncode == 0, result.stderr
