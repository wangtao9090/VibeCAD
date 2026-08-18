from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "windows-qualification.yml"
PLAN = ROOT / "docs" / "WINDOWS_QUALIFICATION.md"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_windows_qualification_is_manual_and_uses_a_pinned_x64_runner() -> None:
    workflow = _workflow()
    triggers = workflow[True]
    assert set(triggers) == {"workflow_dispatch"}
    choices = triggers["workflow_dispatch"]["inputs"]["phase"]["options"]
    assert choices == ["contracts", "managed-runtime", "product", "attestation"]
    assert workflow["jobs"]["contracts"]["runs-on"] == "windows-2022"
    assert workflow["jobs"]["managed"]["runs-on"] == "windows-2022"
    assert all(job["runs-on"] != "windows-latest" for job in workflow["jobs"].values())


def test_windows_gate_is_strictly_ordered_and_cleans_its_runtime() -> None:
    workflow = _workflow()
    managed = workflow["jobs"]["managed"]
    assert managed["needs"] == "contracts"
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "inputs.phase != 'contracts'" in source
    assert "inputs.phase == 'product' || inputs.phase == 'attestation'" in source
    assert "inputs.phase == 'attestation'" in source
    assert "generate_freecad_reviewed_release_attestation.py --check" in source
    assert "from vibecad.runtime.uninstall import uninstall_now" in source
    assert "isolated VIBECAD_HOME remains" in source


def test_plan_requires_native_dual_boot_and_does_not_claim_windows_support() -> None:
    plan = PLAN.read_text(encoding="utf-8")
    assert "not yet a Windows support claim" in plan
    assert "native dual-boot" in plan
    assert "windows.x86_64" in plan
    assert "SCM_RIGHTS" in plan
    assert "Job Object" in plan
    assert "reparse-point" in plan
    assert "FlatFace Sketch → Hole" in plan
    assert "Do not copy a dirty worktree" in plan


def test_release_is_not_activated_by_the_plan_only_commit() -> None:
    release = RELEASE.read_text(encoding="utf-8")
    assert "windows-qualification" not in release
    assert "Windows becomes a release blocker only in the activation commit" in PLAN.read_text(
        encoding="utf-8"
    )
