from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from vibecad.execution import freecad_part_a_verification as part_a
from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    collect_managed_freecad_discovery_v2,
)
from vibecad.execution.freecad_part_a_verification import (
    PART_A_VERIFIER_ID,
    PART_A_VERIFIER_VERSION,
    PART_CORE_REVIEWED_HOST_CASE_MANIFEST,
    PART_CURVE_REVIEWED_HOST_CASE_MANIFEST,
    build_part_core_managed_verification,
    build_part_curve_managed_verification,
)
from vibecad.execution.freecad_reviewed_verification import (
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedCaseManifestKind,
    ReviewedConformanceEvidenceKind,
    ReviewedConformanceFacet,
    build_reviewed_family_conformance_case_manifest,
    encode_reviewed_verification_receipt,
)
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_part_curve_adapter import PART_CURVE_MANIFEST


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
        (PART_CORE_MANIFEST, PART_CORE_REVIEWED_HOST_CASE_MANIFEST, 19),
        (PART_CURVE_MANIFEST, PART_CURVE_REVIEWED_HOST_CASE_MANIFEST, 9),
    ),
)
def test_part_a_reviewed_host_descriptors_are_exact_and_complete(
    family_manifest,
    case_manifest,
    operation_count: int,
) -> None:
    assert len(family_manifest.operations) == operation_count
    assert case_manifest.manifest_kind is ReviewedCaseManifestKind.REVIEWED_HOST
    assert case_manifest.family_manifest_sha256 == family_manifest.manifest_sha256
    assert len(case_manifest.cases) == operation_count * len(REQUIRED_REVIEWED_CONFORMANCE_FACETS)
    expected = {
        (operation.operation_id, facet)
        for operation in family_manifest.operations
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    }
    assert {(case.operation_id, case.facet) for case in case_manifest.cases} == expected
    assert len({case.case_contract_sha256 for case in case_manifest.cases}) == len(
        case_manifest.cases
    )
    assert all(
        case.operation_specification_sha256
        == next(
            operation.specification_sha256
            for operation in family_manifest.operations
            if operation.operation_id == case.operation_id
        )
        for case in case_manifest.cases
    )
    synthetic = build_reviewed_family_conformance_case_manifest(family_manifest)
    assert synthetic.manifest_kind is ReviewedCaseManifestKind.SYNTHETIC
    assert synthetic.case_manifest_sha256 != case_manifest.case_manifest_sha256
    assert {item.case_contract_sha256 for item in synthetic.cases}.isdisjoint(
        item.case_contract_sha256 for item in case_manifest.cases
    )


def test_reviewed_host_admission_is_content_bound_and_caller_cannot_report_pass() -> None:
    original = PART_CORE_REVIEWED_HOST_CASE_MANIFEST
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

    assert tuple(inspect.signature(build_part_core_managed_verification).parameters) == ("freecad",)
    assert tuple(inspect.signature(build_part_curve_managed_verification).parameters) == (
        "freecad",
    )
    with pytest.raises(CapabilityCatalogError) as core_error:
        build_part_core_managed_verification(object())
    assert core_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    with pytest.raises(CapabilityCatalogError) as curve_error:
        build_part_curve_managed_verification(object())
    assert curve_error.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert all(item.verification is None for item in current_freecad_intent_capability_specs())


def test_cleanup_closes_only_documents_still_owned_by_the_verifier() -> None:
    owned = object()
    replaced = object()
    stale_owned = object()
    external = object()

    class FakeFreeCad:
        def __init__(self) -> None:
            self.documents = {
                "Owned": owned,
                "ReusedName": replaced,
                "External": external,
            }
            self.closed: list[str] = []

        def listDocuments(self):
            return dict(self.documents)

        def closeDocument(self, name: str) -> None:
            self.closed.append(name)
            self.documents.pop(name)

    freecad = FakeFreeCad()
    part_a._close_owned_documents(  # noqa: SLF001
        freecad,
        {"Owned": owned, "ReusedName": stale_owned},
    )

    assert freecad.closed == ["Owned"]
    assert freecad.documents == {"ReusedName": replaced, "External": external}


@pytest.mark.slow
def test_real_managed_freecad_part_a_builds_receipts_without_promoting_catalog() -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # noqa: PLC0415

    before_documents = FreeCAD.listDocuments()
    before_gui_up = FreeCAD.GuiUp
    before_gui_module = "FreeCADGui" in sys.modules
    before_capability_specs = current_freecad_intent_capability_specs()

    core_receipt, core_binding = build_part_core_managed_verification(FreeCAD)
    curve_receipt, curve_binding = build_part_curve_managed_verification(FreeCAD)
    after_discovery = collect_managed_freecad_discovery_v2(
        freecad=FreeCAD,
        probe_modules=FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
    )

    for receipt, binding, family_manifest, case_manifest in (
        (
            core_receipt,
            core_binding,
            PART_CORE_MANIFEST,
            PART_CORE_REVIEWED_HOST_CASE_MANIFEST,
        ),
        (
            curve_receipt,
            curve_binding,
            PART_CURVE_MANIFEST,
            PART_CURVE_REVIEWED_HOST_CASE_MANIFEST,
        ),
    ):
        assert receipt.case_manifest == case_manifest
        assert receipt.contract.evidence_kind is ReviewedConformanceEvidenceKind.MANAGED_FREECAD
        assert receipt.contract.family_manifest_sha256 == family_manifest.manifest_sha256
        assert (
            receipt.contract.adapter_contract_sha256
            == family_manifest.adapter.adapter_contract_sha256
        )
        assert receipt.contract.rule_contract_sha256 == family_manifest.rule_contract_sha256
        assert len(receipt.results) == len(family_manifest.operations) * len(
            REQUIRED_REVIEWED_CONFORMANCE_FACETS
        )
        assert {(item.operation_id, item.facet) for item in receipt.results} == {
            (operation.operation_id, facet)
            for operation in family_manifest.operations
            for facet in ReviewedConformanceFacet
        }
        assert (
            binding.runtime_build_sha256
            == receipt.contract.runtime_backend.build_fingerprint_sha256
        )
        assert binding.adapter_contract_sha256 == family_manifest.adapter.adapter_contract_sha256
        assert binding.test_contract_sha256 == receipt.test_contract_sha256
        assert binding.test_receipt_sha256 == receipt.test_receipt_sha256
        assert binding.test_receipt_size_bytes == receipt.test_receipt_size_bytes
        assert binding.verifier_id == PART_A_VERIFIER_ID
        assert binding.verifier_version == PART_A_VERIFIER_VERSION

        encoded = encode_reviewed_verification_receipt(receipt)
        decoded = json.loads(encoded)
        assert _canonical(decoded) == encoded
        assert decoded["test_receipt_sha256"] == receipt.test_receipt_sha256
        assert len(encoded) == receipt.test_receipt_size_bytes
        tampered = decoded | {"test_receipt_sha256": "0" * 64}
        assert _canonical(tampered) != encoded
        assert hashlib.sha256(_canonical(tampered)).digest() != hashlib.sha256(encoded).digest()

        assert receipt.contract.runtime_backend == after_discovery.snapshot.backend
        assert receipt.executable is False
        assert receipt.grants_execution_authority is False

    assert len(core_receipt.results) == 133
    assert len(curve_receipt.results) == 63
    assert after_discovery.manifest.type_count == 449
    assert before_documents == FreeCAD.listDocuments() == {}
    assert before_gui_up == FreeCAD.GuiUp == 0
    assert before_gui_module is False
    assert "FreeCADGui" not in sys.modules
    assert current_freecad_intent_capability_specs() == before_capability_specs
    assert all(item.verification is None for item in before_capability_specs)
