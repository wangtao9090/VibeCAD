"""Frozen authority and provenance checks for the Guided Photo v1 fixture set."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from PIL import Image

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "guided_photo_v1"
POSITIVE_CASES = {
    "guided-photo-washer-ready",
    "guided-photo-fan-spacer-ready",
    "guided-photo-calibration-block-ready",
}
NEGATIVE_CASES = {
    "guided-photo-washer-missing-thickness",
    "guided-photo-clutter-out-of-envelope",
}


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_guided_photo_case_authorities_are_complete_and_disjoint() -> None:
    host = _load("host_inputs.json")
    expected = _load("expected_outcomes.json")
    evaluator = _load("evaluator_truth.json")

    host_cases = {case["case_id"]: case for case in host["cases"]}
    assert set(host_cases) == POSITIVE_CASES | NEGATIVE_CASES
    assert set(expected["cases"]) == set(host_cases)
    assert set(evaluator["cases"]) == POSITIVE_CASES
    assert evaluator["host_visible"] is False

    forbidden_host_keys = {
        "readiness",
        "task_expected",
        "expected_bbox_mm",
        "expected_volume_mm3",
        "reference_volume_mm3",
        "reference_sha256",
    }
    for case in host_cases.values():
        assert forbidden_host_keys.isdisjoint(case)
        assert all(ref.startswith(("fixture:", "user:")) for ref in case["source_refs"])
        assert all(not ref.startswith(("http://", "https://")) for ref in case["source_refs"])


def test_guided_photo_routing_contract_stops_both_negative_cases() -> None:
    expected = _load("expected_outcomes.json")["cases"]

    for case_id in POSITIVE_CASES:
        outcome = expected[case_id]
        assert outcome["readiness"] == "PHOTO_READY"
        assert outcome["task_expected"] is True
        assert outcome["review_policy"] == "require_review"
        assert outcome["required_feature_kinds"]

    missing = expected["guided-photo-washer-missing-thickness"]
    assert missing == {
        "readiness": "NEEDS_CAPTURE",
        "task_expected": False,
        "blocking_fact": "thickness_mm",
        "request_kind": "single_direct_measurement",
        "request": (
            "Measure the washer thickness in millimetres, preferably with the caliper "
            "square to the edge."
        ),
    }

    clutter = expected["guided-photo-clutter-out-of-envelope"]
    assert clutter == {
        "readiness": "OUT_OF_ENVELOPE",
        "task_expected": False,
        "reason": "multiple_dissimilar_objects_no_single_rigid_part",
    }


def test_guided_photo_images_are_normalized_metadata_free_and_manifested() -> None:
    host = _load("host_inputs.json")
    manifest = _load("source_manifest.json")
    records = {record["fixture"]: record for record in manifest["images"]}
    referenced = {image for case in host["cases"] for image in case["images"]}

    assert referenced == set(records)
    assert len({record["normalized_sha256"] for record in records.values()}) == len(records)

    for relative, record in records.items():
        path = (FIXTURE_ROOT / relative).resolve()
        assert path.is_relative_to(FIXTURE_ROOT.resolve())
        assert path.suffix == ".png"
        assert _sha256(path) == record["normalized_sha256"]
        assert record["source_page"].startswith("https://")
        assert record["license_url"].startswith("https://")
        with Image.open(path) as image:
            assert image.format == "PNG"
            assert image.mode == "RGB"
            assert list(image.size) == record["normalized_size_px"]
            assert max(image.size) <= 1600
            assert not image.info
            assert not image.getexif()


def test_guided_photo_hidden_truth_matches_public_reference_manifest() -> None:
    evaluator = _load("evaluator_truth.json")
    manifest = _load("source_manifest.json")
    references = {record["case_id"]: record for record in manifest["evaluator_references"]}

    assert set(references) == POSITIVE_CASES - {"guided-photo-washer-ready"}
    for case_id, reference in references.items():
        assert reference["committed"] is False
        assert reference["source_page"].startswith("https://")
        assert evaluator["cases"][case_id]["reference_sha256"] == reference["sha256"]

    assert not list(FIXTURE_ROOT.rglob("*.stl"))
    assert not list(FIXTURE_ROOT.rglob("*.step"))
    assert not list(FIXTURE_ROOT.rglob("*.fcstd"))


def test_guided_photo_analytic_truth_is_internally_consistent() -> None:
    cases = _load("evaluator_truth.json")["cases"]
    washer = cases["guided-photo-washer-ready"]
    expected_washer_volume = math.pi * (10.0**2 - 5.25**2) * 2.0
    assert math.isclose(
        washer["expected_volume_mm3"],
        expected_washer_volume,
        abs_tol=washer["volume_absolute_tolerance_mm3"],
    )

    block = cases["guided-photo-calibration-block-ready"]
    expected_block_volume = 30.0 * 20.0 * 10.0 - 20.0 * 10.0 * 7.0
    assert math.isclose(
        block["expected_volume_mm3"],
        expected_block_volume,
        abs_tol=block["volume_absolute_tolerance_mm3"],
    )

    required_checks = _load("evaluator_truth.json")["required_cad_checks"]
    assert required_checks == [
        "fully_constrained_sketches",
        "valid_brep",
        "single_solid",
        "confirmed_dimensions",
        "parameter_edit_probe",
        "head_unchanged_before_accept",
    ]
