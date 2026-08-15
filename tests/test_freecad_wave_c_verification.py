from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_reviewed_verification import (
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedCaseManifestKind,
    ReviewedConformanceEvidenceKind,
    encode_reviewed_verification_receipt,
)
from vibecad.execution.freecad_wave_c_verification import (
    APP_REVIEWED_HOST_CASE_MANIFEST,
    SKETCH_REVIEWED_HOST_CASE_MANIFEST,
    WAVE_C_VERIFIER_ID,
    WAVE_C_VERIFIER_VERSION,
    build_app_family_managed_verification,
)
from vibecad.intent_bridge.freecad_app_family_adapter import APP_FAMILY_MANIFEST
from vibecad.intent_bridge.freecad_sketch_intent_adapter import (
    REVIEWED_SKETCH_FAMILY_MANIFEST,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


@pytest.mark.parametrize(
    ("family_manifest", "case_manifest", "operation_count"),
    (
        (REVIEWED_SKETCH_FAMILY_MANIFEST, SKETCH_REVIEWED_HOST_CASE_MANIFEST, 20),
        (APP_FAMILY_MANIFEST, APP_REVIEWED_HOST_CASE_MANIFEST, 10),
    ),
)
def test_wave_c_descriptors_close_exact_reviewed_host_matrices(
    family_manifest,
    case_manifest,
    operation_count: int,
) -> None:
    assert len(family_manifest.operations) == operation_count
    assert case_manifest.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST
    assert case_manifest.family_manifest_sha256 == family_manifest.manifest_sha256
    assert len(case_manifest.cases) == operation_count * len(
        REQUIRED_REVIEWED_CONFORMANCE_FACETS
    )
    assert {(item.operation_id, item.facet) for item in case_manifest.cases} == {
        (operation.operation_id, facet)
        for operation in family_manifest.operations
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    }
    assert len({item.case_sha256 for item in case_manifest.cases}) == len(
        case_manifest.cases
    )
    assert len({item.case_contract_sha256 for item in case_manifest.cases}) == len(
        case_manifest.cases
    )
    for case in case_manifest.cases:
        operation = next(
            item
            for item in family_manifest.operations
            if item.operation_id == case.operation_id
        )
        assert case.operation_specification_sha256 == operation.specification_sha256


def test_wave_c_admission_is_content_bound_and_caller_cannot_claim_pass() -> None:
    original = SKETCH_REVIEWED_HOST_CASE_MANIFEST
    first = original.cases[0]
    with pytest.raises(CapabilityCatalogError) as caught:
        replace(
            original,
            cases=(
                replace(first, case_contract_sha256="f" * 64),
                *original.cases[1:],
            ),
        )
    assert caught.value.code is CapabilityCatalogErrorCode.INVALID_INPUT

    signature = inspect.signature(build_app_family_managed_verification)
    assert tuple(signature.parameters) == ("freecad",)
    assert signature.parameters["freecad"].kind is inspect.Parameter.KEYWORD_ONLY
    with pytest.raises(CapabilityCatalogError):
        build_app_family_managed_verification(freecad=object())
    assert all(item.verification is None for item in current_freecad_intent_capability_specs())


def test_wave_c_import_is_freecad_and_gui_free() -> None:
    source_root = Path(__file__).parents[1] / "src"
    code = """
import sys
import vibecad.execution.freecad_wave_c_verification
assert 'FreeCAD' not in sys.modules
assert 'FreeCADGui' not in sys.modules
print('WAVE_C_BOOTSTRAP_OK')
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(source_root)},
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "WAVE_C_BOOTSTRAP_OK" in completed.stdout


@pytest.mark.slow
def test_real_managed_freecad_app_family_10_by_7_receipt_batch() -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

    before_documents = FreeCAD.listDocuments()
    before_gui_up = FreeCAD.GuiUp
    before_gui_module = "FreeCADGui" in sys.modules
    before_capabilities = current_freecad_intent_capability_specs()

    receipt, binding = build_app_family_managed_verification(freecad=FreeCAD)
    assert receipt.case_manifest == APP_REVIEWED_HOST_CASE_MANIFEST
    assert receipt.contract.evidence_kind is ReviewedConformanceEvidenceKind.MANAGED_FREECAD
    assert receipt.contract.family_manifest_sha256 == APP_FAMILY_MANIFEST.manifest_sha256
    assert receipt.contract.adapter_contract_sha256 == (
        APP_FAMILY_MANIFEST.adapter.adapter_contract_sha256
    )
    assert receipt.contract.rule_contract_sha256 == APP_FAMILY_MANIFEST.rule_contract_sha256
    assert len(receipt.results) == 70
    assert binding.runtime_build_sha256 == (
        receipt.contract.runtime_backend.build_fingerprint_sha256
    )
    assert binding.adapter_contract_sha256 == APP_FAMILY_MANIFEST.adapter.adapter_contract_sha256
    assert binding.test_contract_sha256 == receipt.test_contract_sha256
    assert binding.test_receipt_sha256 == receipt.test_receipt_sha256
    assert binding.test_receipt_size_bytes == receipt.test_receipt_size_bytes
    assert binding.verifier_id == WAVE_C_VERIFIER_ID
    assert binding.verifier_version == WAVE_C_VERIFIER_VERSION
    assert receipt.executable is False
    assert receipt.grants_execution_authority is False

    encoded = encode_reviewed_verification_receipt(receipt)
    assert _canonical(json.loads(encoded)) == encoded
    assert len(encoded) == receipt.test_receipt_size_bytes

    assert before_documents == FreeCAD.listDocuments() == {}
    assert before_gui_up == FreeCAD.GuiUp == 0
    assert before_gui_module is False
    assert "FreeCADGui" not in sys.modules
    assert current_freecad_intent_capability_specs() == before_capabilities
    assert all(item.verification is None for item in before_capabilities)
