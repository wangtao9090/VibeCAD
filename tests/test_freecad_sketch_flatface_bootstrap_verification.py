from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution import freecad_sketch_flatface_bootstrap_verification as flatface
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedConformanceEvidenceKind,
    ReviewedConformanceFacet,
    ReviewedVerificationReceipt,
)
from vibecad.intent_bridge.freecad_sketch_flatface_bootstrap_adapter import (
    FLATFACE_SKETCH_FAMILY_MANIFEST,
)

_BUILD_SHA256 = hashlib.sha256(b"flatface-sketch-managed-test").hexdigest()


def _backend() -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_BUILD_SHA256,
        platform_id="macos.arm64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def _evidence() -> dict[ReviewedConformanceFacet, dict[str, object]]:
    return {facet: {"independent_facet": facet.value} for facet in ReviewedConformanceFacet}


def test_reviewed_host_manifest_is_exactly_one_operation_by_seven_facets() -> None:
    operation = FLATFACE_SKETCH_FAMILY_MANIFEST.operations[0]
    manifest = flatface.FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST

    assert manifest.family_manifest_sha256 == FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256
    assert len(manifest.cases) == 7
    assert {item.operation_id for item in manifest.cases} == {operation.operation_id}
    assert {item.operation_specification_sha256 for item in manifest.cases} == {
        operation.specification_sha256
    }
    assert {item.facet for item in manifest.cases} == set(ReviewedConformanceFacet)
    assert len({item.case_sha256 for item in manifest.cases}) == 7
    assert len({item.case_contract_sha256 for item in manifest.cases}) == 7


def test_executor_runs_native_matrix_once_but_binds_each_case_and_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def run(_self):
        nonlocal calls
        calls += 1
        return _evidence()

    monkeypatch.setattr(flatface._FlatFaceSketchExecutor, "_run", run)  # noqa: SLF001
    executor = flatface._FlatFaceSketchExecutor(object())  # noqa: SLF001
    observations = []
    for index, case in enumerate(flatface.FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST.cases):
        challenge = hashlib.sha256(f"challenge-{index}".encode("ascii")).hexdigest()
        decoded = json.loads(executor(case, challenge))
        assert decoded["case_sha256"] == case.case_sha256
        assert decoded["case_contract_sha256"] == case.case_contract_sha256
        assert decoded["challenge_sha256"] == challenge
        assert decoded["facet"] == case.facet.value
        assert decoded["evidence"] == {"independent_facet": case.facet.value}
        observations.append(decoded["observation_sha256"])
    assert calls == 1
    assert len(set(observations)) == 7

    foreign = replace(
        flatface.FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST.cases[0],
        case_id="flatface_sketch.foreign.create",
    )
    with pytest.raises(CapabilityCatalogError):
        executor(foreign, "a" * 64)


def test_managed_builder_returns_exact_receipt_and_binding_without_caller_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        flatface._FlatFaceSketchExecutor,  # noqa: SLF001
        "_run",
        lambda _self: _evidence(),
    )

    def host_factory(*, freecad, case_manifest, execute_case, verifier_id, verifier_version):
        assert freecad is fake_freecad
        assert case_manifest is flatface.FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST
        assert verifier_id == flatface.FLATFACE_SKETCH_VERIFIER_ID
        assert verifier_version == flatface.FLATFACE_SKETCH_VERIFIER_VERSION
        backend = _backend()
        return verification._ReviewedConformanceHost._create(  # noqa: SLF001
            runtime_backend=backend,
            case_manifest_sha256=case_manifest.case_manifest_sha256,
            evidence_kind=ReviewedConformanceEvidenceKind.MANAGED_FREECAD,
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            execute_case=execute_case,
            guard=lambda: None,
            revalidate=lambda: backend,
            builder_token=verification._HOST_BUILDER_TOKEN,  # noqa: SLF001
        )

    fake_freecad = object()
    monkeypatch.setattr(flatface, "build_managed_freecad_conformance_host", host_factory)

    receipt, binding = flatface.build_flatface_sketch_managed_verification(freecad=fake_freecad)

    assert type(receipt) is ReviewedVerificationReceipt
    assert type(binding) is FreeCadPromotionVerificationBinding
    assert (
        receipt.contract.family_manifest_sha256 == FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256
    )
    assert receipt.contract.case_manifest_sha256 == (
        flatface.FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST.case_manifest_sha256
    )
    assert len(receipt.results) == 7
    assert binding.test_receipt_sha256 == receipt.test_receipt_sha256
    assert binding.test_contract_sha256 == receipt.test_contract_sha256
    assert binding.runtime_build_sha256 == _BUILD_SHA256


def test_api_is_keyword_only_import_safe_and_has_no_runtime_or_persistence_wiring() -> None:
    signature = inspect.signature(flatface.build_flatface_sketch_managed_verification)
    assert tuple(signature.parameters) == ("freecad",)
    assert signature.parameters["freecad"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(flatface)
    assert "from vibecad.server" not in source
    assert "from vibecad.store" not in source
    assert "write_receipt" not in source

    source_root = Path(__file__).parents[1] / "src"
    code = """
import sys
import vibecad.execution.freecad_sketch_flatface_bootstrap_verification
assert 'FreeCAD' not in sys.modules
assert 'FreeCADGui' not in sys.modules
assert 'Part' not in sys.modules
print('FLATFACE_SKETCH_VERIFICATION_IMPORT_OK')
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
    assert "FLATFACE_SKETCH_VERIFICATION_IMPORT_OK" in completed.stdout
