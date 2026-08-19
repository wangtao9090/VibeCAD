from __future__ import annotations

import inspect
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vibecad.execution import freecad_current_managed_verification as current_verification
from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_current_managed_verification import (
    CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT,
    CURRENT_MANAGED_VERIFICATION_NATIVE_TYPE_COUNT,
    CURRENT_MANAGED_VERIFICATION_PROMOTION_OPERATION_COUNT,
    CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT,
    build_current_managed_freecad_reviewed_verification_set_for_maintainers,
)
from vibecad.execution.freecad_legacy_reviewed_verification import (
    LEGACY_REVIEWED_CASE_MANIFESTS,
    LEGACY_REVIEWED_FAMILY_MANIFESTS,
    PARTDESIGN_REFERENCE_V2_VERIFICATION_FAMILY_MANIFEST,
)
from vibecad.execution.freecad_part_a_verification import (
    PART_CORE_REVIEWED_HOST_CASE_MANIFEST,
    PART_CURVE_REVIEWED_HOST_CASE_MANIFEST,
)
from vibecad.execution.freecad_reviewed_verification import (
    ReviewedConformanceEvidenceKind,
    build_promotion_verification_binding,
    build_reviewed_family_conformance_case_manifest,
    build_reviewed_verification_receipt,
)
from vibecad.execution.freecad_reviewed_verification_part_b import (
    PART_B_FAMILY_MANIFESTS,
    PART_B_REVIEWED_CASE_MANIFESTS,
)
from vibecad.execution.freecad_reviewed_verification_runtime import (
    FreeCadManagedReviewedVerificationSet,
)
from vibecad.execution.freecad_reviewed_verification_wave_d import (
    WAVE_D_FAMILY_MANIFESTS,
    WAVE_D_REVIEWED_CASE_MANIFESTS,
    WaveDManagedVerificationBatch,
)
from vibecad.execution.freecad_sketch_bootstrap_verification import (
    SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST,
)
from vibecad.execution.freecad_sketch_flatface_bootstrap_verification import (
    FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST,
)
from vibecad.execution.freecad_wave_c_verification import (
    APP_REVIEWED_HOST_CASE_MANIFEST,
    SKETCH_REVIEWED_HOST_CASE_MANIFEST,
    WAVE_C_FAMILY_MANIFESTS,
)
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_part_curve_adapter import PART_CURVE_MANIFEST
from vibecad.intent_bridge.freecad_sketch_bootstrap_adapter import (
    SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
)
from vibecad.intent_bridge.freecad_sketch_flatface_bootstrap_adapter import (
    FLATFACE_SKETCH_FAMILY_MANIFEST,
)

_MANAGED_BUILD_SHA256 = "7" * 64
_EXPECTED_CALL_ORDER = (
    "part_a_core",
    "part_a_curves",
    "part_b",
    "wave_c",
    "wave_d",
    "sketch_bootstrap",
    "flatface_sketch",
    "legacy",
)


class _FakeFreeCad:
    GuiUp = 0

    def __init__(self, documents: dict[str, object] | None = None) -> None:
        self.documents = dict(documents or {})
        self.closed: list[str] = []

    def listDocuments(self) -> dict[str, object]:  # noqa: N802
        return dict(self.documents)

    def closeDocument(self, name: str) -> None:  # noqa: N802
        self.closed.append(name)
        self.documents.pop(name, None)


def _backend() -> CapabilityBackend:
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_MANAGED_BUILD_SHA256,
        platform_id="macos.arm64",
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )


def _all_manifests_in_execution_order():
    return (
        PART_CORE_MANIFEST,
        PART_CURVE_MANIFEST,
        *PART_B_FAMILY_MANIFESTS,
        *WAVE_C_FAMILY_MANIFESTS,
        *WAVE_D_FAMILY_MANIFESTS,
        SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
        FLATFACE_SKETCH_FAMILY_MANIFEST,
        *LEGACY_REVIEWED_FAMILY_MANIFESTS,
    )


def _managed_receipt(manifest):
    synthetic = build_reviewed_family_conformance_case_manifest(manifest)
    case_manifest = verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=manifest,
        cases=synthetic.cases,
    )
    backend = _backend()

    def execute(case, challenge_sha256):
        return f"{case.case_sha256}:{challenge_sha256}:maintainer-test".encode("ascii")

    host = verification._ReviewedConformanceHost._create(  # noqa: SLF001
        runtime_backend=backend,
        case_manifest_sha256=case_manifest.case_manifest_sha256,
        evidence_kind=ReviewedConformanceEvidenceKind.MANAGED_FREECAD,
        verifier_id="vcad.test.current-managed-verification",
        verifier_version="1.0.0",
        execute_case=execute,
        guard=lambda: None,
        revalidate=lambda: backend,
        builder_token=verification._HOST_BUILDER_TOKEN,  # noqa: SLF001
    )
    return build_reviewed_verification_receipt(
        manifest=manifest,
        case_manifest=case_manifest,
        host=host,
    )


@pytest.fixture(scope="module")
def managed_receipts():
    return tuple(_managed_receipt(item) for item in _all_manifests_in_execution_order())


def _install_valid_builders(
    monkeypatch: pytest.MonkeyPatch,
    managed_receipts,
    calls: list[str] | None = None,
) -> None:
    call_log = calls if calls is not None else []
    receipts = iter(managed_receipts)
    core = next(receipts)
    curves = next(receipts)
    part_b = tuple(next(receipts) for _ in range(4))
    wave_c = tuple(next(receipts) for _ in range(2))
    wave_d = tuple(next(receipts) for _ in range(3))
    sketch_bootstrap = next(receipts)
    flatface_sketch = next(receipts)
    legacy = tuple(next(receipts) for _ in range(8))
    with pytest.raises(StopIteration):
        next(receipts)

    def core_builder(freecad):
        call_log.append("part_a_core")
        return core, build_promotion_verification_binding(core)

    def curve_builder(freecad):
        call_log.append("part_a_curves")
        return curves, build_promotion_verification_binding(curves)

    def part_b_builder(*, freecad):
        call_log.append("part_b")
        return part_b

    def wave_c_builder(*, freecad):
        call_log.append("wave_c")
        return tuple((item, build_promotion_verification_binding(item)) for item in wave_c)

    def wave_d_builder(*, freecad):
        call_log.append("wave_d")
        return WaveDManagedVerificationBatch(
            receipts=wave_d,
            promotion_bindings=tuple(build_promotion_verification_binding(item) for item in wave_d),
        )

    def sketch_bootstrap_builder(*, freecad):
        call_log.append("sketch_bootstrap")
        return (
            sketch_bootstrap,
            build_promotion_verification_binding(sketch_bootstrap),
        )

    def flatface_sketch_builder(*, freecad):
        call_log.append("flatface_sketch")
        return (
            flatface_sketch,
            build_promotion_verification_binding(flatface_sketch),
        )

    def legacy_builder(*, freecad):
        call_log.append("legacy")
        return tuple((item, build_promotion_verification_binding(item)) for item in legacy)

    monkeypatch.setattr(
        current_verification,
        "build_part_core_managed_verification",
        core_builder,
    )
    monkeypatch.setattr(
        current_verification,
        "build_part_curve_managed_verification",
        curve_builder,
    )
    monkeypatch.setattr(
        current_verification,
        "build_managed_freecad_part_b_verification_receipts",
        part_b_builder,
    )
    monkeypatch.setattr(
        current_verification,
        "build_sketch_and_app_managed_verification",
        wave_c_builder,
    )
    monkeypatch.setattr(
        current_verification,
        "build_managed_freecad_wave_d_verification",
        wave_d_builder,
    )
    monkeypatch.setattr(
        current_verification,
        "build_sketch_bootstrap_managed_verification",
        sketch_bootstrap_builder,
    )
    monkeypatch.setattr(
        current_verification,
        "build_flatface_sketch_managed_verification",
        flatface_sketch_builder,
    )
    monkeypatch.setattr(
        current_verification,
        "build_managed_freecad_legacy_reviewed_verification_receipts",
        legacy_builder,
    )


def test_current_inventory_is_exactly_twenty_one_receipts_and_126_by_seven_cases() -> None:
    case_manifests = (
        PART_CORE_REVIEWED_HOST_CASE_MANIFEST,
        PART_CURVE_REVIEWED_HOST_CASE_MANIFEST,
        *PART_B_REVIEWED_CASE_MANIFESTS,
        SKETCH_REVIEWED_HOST_CASE_MANIFEST,
        APP_REVIEWED_HOST_CASE_MANIFEST,
        *WAVE_D_REVIEWED_CASE_MANIFESTS,
        SKETCH_BOOTSTRAP_REVIEWED_HOST_CASE_MANIFEST,
        FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST,
        *LEGACY_REVIEWED_CASE_MANIFESTS,
    )
    assert len(_all_manifests_in_execution_order()) == len(case_manifests) == 21
    assert sum(len(item.cases) for item in case_manifests) == 126 * 7
    assert CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT == 21
    assert CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT == 126
    assert CURRENT_MANAGED_VERIFICATION_PROMOTION_OPERATION_COUNT == 104
    assert CURRENT_MANAGED_VERIFICATION_NATIVE_TYPE_COUNT == 102


def test_fast_managed_receipts_close_exact_coverage_in_sequential_order(
    monkeypatch: pytest.MonkeyPatch,
    managed_receipts,
) -> None:
    calls: list[str] = []
    _install_valid_builders(monkeypatch, managed_receipts, calls)
    freecad = _FakeFreeCad()

    result = build_current_managed_freecad_reviewed_verification_set_for_maintainers(
        freecad=freecad
    )

    assert type(result) is FreeCadManagedReviewedVerificationSet
    assert tuple(calls) == _EXPECTED_CALL_ORDER
    assert len(result.receipt_sha256) == 21
    assert len(result.formal_operations) == 126
    assert len(result.native_types) == len(result.verification_by_native_type) == 102
    bootstrap_receipt = managed_receipts[11]
    bootstrap_operation = next(
        item
        for item in result.formal_operations
        if item.operation_id == "freecad_sketch_bootstrap.create_body_owned_closed_circle"
    )
    assert bootstrap_operation.test_receipt_sha256 == bootstrap_receipt.test_receipt_sha256
    assert (
        result.verification_by_native_type["Sketcher::SketchObject"].test_receipt_sha256
        != bootstrap_receipt.test_receipt_sha256
    )
    flatface_receipt = managed_receipts[12]
    flatface_operation = next(
        item
        for item in result.formal_operations
        if item.operation_id
        == "freecad_sketch_flatface_bootstrap.create_closed_circle_on_unique_zmax_planar_face"
    )
    assert flatface_operation.test_receipt_sha256 == flatface_receipt.test_receipt_sha256
    assert (
        result.verification_by_native_type["Sketcher::SketchObject"].test_receipt_sha256
        != flatface_receipt.test_receipt_sha256
    )
    reference_receipt = next(
        item
        for item in managed_receipts
        if item.contract.family_manifest_sha256
        == PARTDESIGN_REFERENCE_V2_VERIFICATION_FAMILY_MANIFEST.manifest_sha256
    )
    reference_operation_ids = {
        item.operation_id
        for item in PARTDESIGN_REFERENCE_V2_VERIFICATION_FAMILY_MANIFEST.operations
    }
    assert {
        item.operation_id
        for item in result.formal_operations
        if item.test_receipt_sha256 == reference_receipt.test_receipt_sha256
    } == reference_operation_ids
    assert (
        reference_receipt.contract.adapter_contract_sha256
        == PARTDESIGN_REFERENCE_V2_VERIFICATION_FAMILY_MANIFEST.adapter.adapter_contract_sha256
    )
    assert (
        reference_receipt.contract.rule_contract_sha256
        == PARTDESIGN_REFERENCE_V2_VERIFICATION_FAMILY_MANIFEST.rule_contract_sha256
    )
    assert result.runtime_backend == _backend()
    assert freecad.listDocuments() == {}
    assert freecad.closed == []


def test_receipt_and_current_catalog_tampering_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    managed_receipts,
) -> None:
    _install_valid_builders(monkeypatch, managed_receipts)
    core = managed_receipts[0]
    monkeypatch.setattr(
        current_verification,
        "build_part_curve_managed_verification",
        lambda freecad: (core, build_promotion_verification_binding(core)),
    )
    with pytest.raises(CapabilityCatalogError) as receipt_error:
        build_current_managed_freecad_reviewed_verification_set_for_maintainers(
            freecad=_FakeFreeCad()
        )
    assert receipt_error.value.path == "current_managed_verification/receipt_order"

    executed = False

    def unexpected_builder(freecad):
        nonlocal executed
        executed = True
        raise AssertionError("verification started after inventory drift")

    monkeypatch.setattr(
        current_verification,
        "CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS",
        current_verification.CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS[:-1],
    )
    monkeypatch.setattr(
        current_verification,
        "build_part_core_managed_verification",
        unexpected_builder,
    )
    with pytest.raises(CapabilityCatalogError) as inventory_error:
        build_current_managed_freecad_reviewed_verification_set_for_maintainers(
            freecad=_FakeFreeCad()
        )
    assert inventory_error.value.path == "current_managed_verification/manifests"
    assert executed is False


def test_formal_coverage_drift_fails_before_any_receipt_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formal = current_freecad_intent_capability_specs()
    executed = False

    def unexpected_builder(freecad):
        nonlocal executed
        executed = True
        raise AssertionError("verification started after formal coverage drift")

    monkeypatch.setattr(
        current_verification,
        "current_freecad_intent_capability_specs",
        lambda: formal[:-1],
    )
    monkeypatch.setattr(
        current_verification,
        "build_part_core_managed_verification",
        unexpected_builder,
    )
    with pytest.raises(CapabilityCatalogError) as caught:
        build_current_managed_freecad_reviewed_verification_set_for_maintainers(
            freecad=_FakeFreeCad()
        )
    assert caught.value.path == "current_managed_verification/capability_specs"
    assert executed is False


def test_exception_cleanup_closes_only_documents_created_by_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preexisting = object()
    owned = object()
    freecad = _FakeFreeCad({"Preexisting": preexisting})

    def failing_builder(injected):
        assert injected is freecad
        injected.documents["Owned"] = owned
        raise RuntimeError("fixture failed")

    monkeypatch.setattr(
        current_verification,
        "build_part_core_managed_verification",
        failing_builder,
    )
    with pytest.raises(RuntimeError, match="fixture failed"):
        build_current_managed_freecad_reviewed_verification_set_for_maintainers(freecad=freecad)
    assert freecad.listDocuments() == {"Preexisting": preexisting}
    assert freecad.closed == ["Owned"]


def test_process_lock_rejects_concurrent_full_gate_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    managed_receipts,
) -> None:
    _install_valid_builders(monkeypatch, managed_receipts)
    original_core_builder = current_verification.build_part_core_managed_verification
    entered = threading.Event()
    release = threading.Event()
    result: list[FreeCadManagedReviewedVerificationSet] = []
    errors: list[BaseException] = []
    freecad = _FakeFreeCad()

    def blocking_core_builder(injected):
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("concurrency test timed out")
        return original_core_builder(injected)

    monkeypatch.setattr(
        current_verification,
        "build_part_core_managed_verification",
        blocking_core_builder,
    )

    def run_first() -> None:
        try:
            result.append(
                build_current_managed_freecad_reviewed_verification_set_for_maintainers(
                    freecad=freecad
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)
    started = time.monotonic()
    with pytest.raises(CapabilityCatalogError) as caught:
        build_current_managed_freecad_reviewed_verification_set_for_maintainers(freecad=freecad)
    elapsed = time.monotonic() - started
    assert caught.value.path == "current_managed_verification/concurrent_verification"
    assert elapsed < 0.5
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    assert len(result) == 1


def test_api_is_keyword_only_import_safe_and_has_no_runtime_or_persistence_wiring() -> None:
    signature = inspect.signature(
        build_current_managed_freecad_reviewed_verification_set_for_maintainers
    )
    assert tuple(signature.parameters) == ("freecad",)
    assert signature.parameters["freecad"].kind is inspect.Parameter.KEYWORD_ONLY
    source = inspect.getsource(current_verification)
    assert "from vibecad.server" not in source
    assert "from vibecad.store" not in source
    assert "apply_promotion" not in source
    assert "write_receipt" not in source

    source_root = Path(__file__).parents[1] / "src"
    code = """
import sys
import vibecad.execution.freecad_current_managed_verification
assert 'FreeCAD' not in sys.modules
assert 'FreeCADGui' not in sys.modules
print('CURRENT_MANAGED_VERIFICATION_IMPORT_OK')
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
    assert "CURRENT_MANAGED_VERIFICATION_IMPORT_OK" in completed.stdout


@pytest.mark.slow
def test_real_managed_freecad_full_126_by_7_verification_set() -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

    before_specs = current_freecad_intent_capability_specs()
    assert FreeCAD.GuiUp == 0
    assert FreeCAD.listDocuments() == {}
    assert "FreeCADGui" not in sys.modules

    result = build_current_managed_freecad_reviewed_verification_set_for_maintainers(
        freecad=FreeCAD
    )

    assert type(result) is FreeCadManagedReviewedVerificationSet
    assert len(result.receipt_sha256) == 21
    assert len(result.formal_operations) == 126
    assert len(result.native_types) == len(result.verification_by_native_type) == 102
    assert all(
        item.runtime_build_sha256 == result.runtime_backend.build_fingerprint_sha256
        for item in result.verification_by_native_type.values()
    )
    assert current_freecad_intent_capability_specs() == before_specs
    assert all(item.verification is None for item in before_specs)
    assert FreeCAD.listDocuments() == {}
    assert FreeCAD.GuiUp == 0
    assert "FreeCADGui" not in sys.modules
