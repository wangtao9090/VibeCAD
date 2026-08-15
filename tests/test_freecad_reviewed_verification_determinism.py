"""Static guards for release-attestable Reviewed FreeCAD observations."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_reviewed_receipt_observations_exclude_raw_fcstd_serialization_identity() -> None:
    execution = ROOT / "src/vibecad/execution"
    part_b = (execution / "freecad_reviewed_verification_part_b.py").read_text(encoding="utf-8")
    wave_d = (execution / "freecad_reviewed_verification_wave_d.py").read_text(encoding="utf-8")
    legacy = (execution / "freecad_legacy_reviewed_verification.py").read_text(encoding="utf-8")

    assert '"file_size_bytes"' not in part_b
    assert '"file_size_bytes"' not in wave_d
    assert 'save_facts = {"size_bytes"' not in legacy
    assert "save_facts = _stable_fcstd_save_facts(saved)" in legacy
    assert legacy.count("save_facts = _stable_fcstd_save_facts(saved)") == 8
