from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.windows_contract

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "windows-qualification.yml"
PLAN = ROOT / "docs" / "WINDOWS_QUALIFICATION.md"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_windows_qualification_splits_hosted_w0_from_standard_user_native_w1() -> None:
    workflow = _workflow()
    triggers = workflow[True]
    assert set(triggers) == {"workflow_dispatch"}
    choices = triggers["workflow_dispatch"]["inputs"]["phase"]["options"]
    assert choices == ["contracts", "managed-runtime", "product", "attestation"]
    assert workflow["jobs"]["contracts"]["runs-on"] == "windows-2022"
    assert workflow["jobs"]["managed"]["runs-on"] == [
        "self-hosted",
        "Windows",
        "X64",
        "vibecad-w1-standard-user-disposable",
    ]
    assert all("windows-latest" not in job["runs-on"] for job in workflow["jobs"].values())


def test_windows_gate_is_strictly_ordered_and_cleans_its_runtime() -> None:
    workflow = _workflow()
    managed = workflow["jobs"]["managed"]
    assert managed["needs"] == "contracts"
    source = WORKFLOW.read_text(encoding="utf-8")
    assert '-m "windows_contract and not slow"' in source
    assert "tests/test_windows_package_cache.py" in source
    assert "tests/test_windows_job_runner.py" in source
    assert "tests/test_local_daemon.py" in source
    assert "tests/test_uninstall.py" in source
    assert "inputs.phase != 'contracts'" in source
    assert "inputs.phase == 'product' || inputs.phase == 'attestation'" in source
    assert "inputs.phase == 'attestation'" in source
    assert "from vibecad.freecad_env import prepare_freecad_import" in source
    assert "prepare_freecad_import(); import FreeCAD" in source
    assert "test_real_managed_freecad_round_trips_headless_model" in source
    assert "test_real_managed_worker_round_trips_parametric_design" not in source
    assert "generate_freecad_reviewed_release_attestation.py --check" in source
    product = _step(managed, "Reviewed product gate")["run"]
    assert product.index("Remove-Item Env:VIBECAD_FREECAD_ENV") < product.index(
        "uv run --frozen pytest"
    )
    assert "VIBECAD_MANAGED_FREECAD_PYTHON" in product
    attestation = _step(managed, "Windows reviewed-attestation check")["run"]
    assert attestation.index("Remove-Item Env:VIBECAD_FREECAD_ENV") < attestation.index(
        ".github/scripts/generate_freecad_reviewed_release_attestation.py --check"
    )
    assert "& $env:VIBECAD_MANAGED_FREECAD_PYTHON -I" in attestation
    assert "uv run --frozen python" not in attestation
    assert "from vibecad.runtime.uninstall import uninstall_now" in source
    cleanup = _step(managed, "Remove only the managed runtime and verify complete isolation")["run"]
    assert cleanup.index("Remove-Item Env:VIBECAD_FREECAD_ENV") < cleanup.index(
        "from vibecad.runtime.uninstall import uninstall_now"
    )
    assert "VIBECAD_EXPECTED_HOME" in cleanup
    assert "qualification-canary.txt" in cleanup
    assert "refusing to clean a default VIBECAD_HOME" in cleanup
    assert "isolated managed runtime remains" in source
    assert "durable data was removed" in source


def test_managed_gate_proves_legacy_path_and_environment_isolation() -> None:
    workflow = _workflow()
    managed = workflow["jobs"]["managed"]
    source = WORKFLOW.read_text(encoding="utf-8")
    bind = _step(managed, "Bind the disposable default-home qualification context")["run"]
    install = _step(managed, "Install the current managed FreeCAD runtime")["run"]
    transaction = _step(managed, "Verify the isolated package transaction")["run"]
    fingerprint = _step(managed, "Bind and fingerprint the managed runtime")["run"]
    assert "LongPathsEnabled=0" in source
    assert "LongPathsEnabled=1" not in source
    assert "Set-ItemProperty" not in source
    assert "W1 must run with a non-elevated token" in source
    assert "Test-Path Env:VIBECAD_HOME" in bind
    assert "Test-Path Env:VIBECAD_FREECAD_ENV" in bind
    assert "vibecad_home().resolve(strict=False)" in bind
    assert 'Join-Path $env:LOCALAPPDATA "VibeCAD"' in bind
    assert "did not select the real default LOCALAPPDATA VibeCAD home" in bind
    assert "default VIBECAD_HOME must not exist" in bind
    assert "_ensure_maintenance_write_root" in bind
    assert "ensure_private_directory" in bind
    assert "open_private_file" in bind
    assert "capture_windows_fd" in bind
    assert "validate_windows_path" in bind
    assert "ACL-protected home and data canary" in bind
    assert (
        bind.index("default VIBECAD_HOME must not exist")
        < bind.index("_ensure_maintenance_write_root")
        < bind.index("qualification-canary.txt")
    )
    assert 'New-Item -ItemType Directory -Force (Join-Path $defaultHome "data")' not in bind
    assert "WINDOWS_MAX_ENV_PREFIX_LENGTH" in bind
    assert "WINDOWS_REVIEWED_MAX_ENV_MEMBER" in bind
    assert "managedPrefix.Length + 1" in bind
    assert "VIBECAD_HOME=" not in source
    assert "CONDA_PKGS_DIRS" in source
    assert "MAMBA_ROOT_PREFIX" in source
    assert "XDG_CACHE_HOME" in source
    assert "Get-CimInstance Win32_Process" in install
    assert "ConvertTo-OperationEvidence" in install
    assert 'Test-CommandArgument $commandLine "--download-only"' in install
    assert 'Test-CommandArgument $commandLine "--offline"' in install
    assert "global_options_before_subcommand" in install
    assert "prefix_option_after_subcommand" in install
    assert "operation_option_after_subcommand" in install
    assert "micromamba-commands.json" in install
    assert "flat_cache_operation =" not in source
    assert "authoritative_link_operation =" not in source
    assert "observed_commands = $commands" in transaction
    assert "Get-FileHash" in fingerprint
    assert "subdir=sys.argv[1]" in fingerprint
    assert "expected_sha256(subdir" in fingerprint
    assert "micromamba-binaries.json" in fingerprint
    assert "conda-meta" in fingerprint
    assert "conda-metadata.json" in fingerprint
    assert 'Where-Object { $_ -ne "noarch" }' in fingerprint
    assert "conda_subdir = $platformSubdirs[0]" in fingerprint
    assert "'platform':'windows.x86_64'" not in fingerprint
    assert "viskores-1.1.1-cpu_h4b717ef_1.json" in source
    assert "the short physical package cache remains" in source
    assert "the 2.5 staging root remains" in source
    assert "the 2.5 download prefix remains" in source
    assert "the package-cache recovery record remains" in source
    assert "VIBECAD_OBSERVED_PACKAGE_CACHE" in source
    assert "the observed package-cache receipt schema is not 1" in source
    assert "the observed package-cache receipt was not active" in source
    assert "receipt.token" not in source
    assert "RedirectStandardOutput = $true" in install
    assert "RedirectStandardError = $true" in install
    assert "installer.stdout.log" in install
    assert "installer.stderr.log" in install
    assert "vibecad\\.runtime\\.windows_package_cache" in transaction
    assert "--cleanup-helper" in transaction
    assert "windows_job_runner\\.py" in transaction
    assert "--gate" in transaction
    cleanup = _step(managed, "Remove only the managed runtime and verify complete isolation")["run"]
    assert "windows_job_runner\\.py" in cleanup
    assert "--gate" in cleanup
    assert "a managed FreeCAD, Python, Worker, or micromamba process remains" in source


def test_plan_records_native_dual_boot_and_activated_windows_support() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    assert "completed for `windows.x86_64`" in plan
    assert "release-blocking gates are active" in plan
    assert "native dual-boot" in plan
    assert "windows.x86_64" in plan
    assert "SCM_RIGHTS" in plan
    assert "Job Object" in plan
    assert "reparse-point" in plan
    assert "LongPathsEnabled=0" in plan
    assert "LongPathsEnabled=1" not in plan
    assert "non-elevated" in plan
    assert "vibecad-w1-standard-user-disposable" in plan
    assert "GitHub-hosted Windows runners run as Administrator" in plan
    assert "real default" in plan
    assert "prefix past the reviewed 80-character budget" in plan
    assert "gh workflow run windows-qualification.yml" in plan
    assert "tests/test_windows_job_runner.py" in plan
    assert "config.cmd --ephemeral" in plan
    assert "windows_job_runner.py --gate" in plan
    assert "vibecad-runtime-$stamp" not in plan
    assert "$env:VIBECAD_HOME =" not in plan
    assert "synchronous ad-hoc installer invocation is" in plan
    assert "security boundary against a malicious process" in plan
    assert "hostile same-SID process" in plan
    assert "micromamba 2.5.0-2" in plan
    assert "micromamba 2.8.0-0" in plan
    assert "viskores=1.1.1=cpu_h4b717ef_1" in plan
    assert "FlatFace Sketch → Hole" in plan
    assert "Do not copy a dirty worktree" in plan


def test_release_activates_exact_windows_attestation_as_a_publisher_blocker() -> None:
    release = RELEASE.read_text(encoding="utf-8")
    assert "platform_id: windows.x86_64" in release
    assert "runner: windows-2022" in release
    assert "bootstrap_python: Scripts/python.exe" in release
    assert "quality and reviewed-attestation matrices depend on Windows" in PLAN.read_text(
        encoding="utf-8"
    )
