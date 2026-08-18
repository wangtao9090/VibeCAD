"""Maintainer-only orchestration for the complete managed FreeCAD gate.

This module deliberately has no production-runtime integration.  Importing it
does not import FreeCAD, and the sole entry point accepts an already prepared
managed FreeCAD module.  It runs the frozen reviewed verification families in
one process and returns only the opaque, in-memory verification set built by
the central coverage join.  It never persists evidence, changes capability
status, or grants CAD execution authority.

The entry point is intentionally unsuitable for a public capability query:
the 125-operation, seven-facet matrix is a maintainer/CI conformance gate, not
an on-demand discovery path.
"""

from __future__ import annotations

import threading
from typing import Final

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
    current_freecad_intent_promotion_specs,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_legacy_reviewed_verification import (
    LEGACY_REVIEWED_FAMILY_MANIFESTS,
    build_managed_freecad_legacy_reviewed_verification_receipts,
)
from vibecad.execution.freecad_part_a_verification import (
    build_part_core_managed_verification,
    build_part_curve_managed_verification,
)
from vibecad.execution.freecad_reviewed_family_capabilities import (
    CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS,
)
from vibecad.execution.freecad_reviewed_verification import ReviewedVerificationReceipt
from vibecad.execution.freecad_reviewed_verification_part_b import (
    PART_B_FAMILY_MANIFESTS,
    build_managed_freecad_part_b_verification_receipts,
)
from vibecad.execution.freecad_reviewed_verification_runtime import (
    FreeCadManagedReviewedVerificationSet,
    build_managed_reviewed_verification_set,
)
from vibecad.execution.freecad_reviewed_verification_wave_d import (
    WAVE_D_FAMILY_MANIFESTS,
    WaveDManagedVerificationBatch,
    build_managed_freecad_wave_d_verification,
)
from vibecad.execution.freecad_sketch_bootstrap_verification import (
    build_sketch_bootstrap_managed_verification,
)
from vibecad.execution.freecad_wave_c_verification import (
    WAVE_C_FAMILY_MANIFESTS,
    build_sketch_and_app_managed_verification,
)
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_part_curve_adapter import PART_CURVE_MANIFEST
from vibecad.intent_bridge.freecad_sketch_bootstrap_adapter import (
    SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
)
from vibecad.intent_bridge.reviewed_family_engine import FamilyBatchManifest

CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT: Final = 20
CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT: Final = 125
CURRENT_MANAGED_VERIFICATION_PROMOTION_OPERATION_COUNT: Final = 104
CURRENT_MANAGED_VERIFICATION_NATIVE_TYPE_COUNT: Final = 102

_CURRENT_REVIEWED_FAMILY_COUNT: Final = 12
_CURRENT_LEGACY_FAMILY_COUNT: Final = 8
_VERIFICATION_LOCK = threading.Lock()


def _fail(path: str) -> None:
    raise CapabilityCatalogError(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _manifest_identities(
    manifests: tuple[FamilyBatchManifest, ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((item.family_id, item.manifest_sha256) for item in manifests))


def _current_exact_inputs() -> tuple[tuple[FamilyBatchManifest, ...], tuple, tuple]:
    scheduled_reviewed = (
        PART_CORE_MANIFEST,
        PART_CURVE_MANIFEST,
        *PART_B_FAMILY_MANIFESTS,
        *WAVE_C_FAMILY_MANIFESTS,
        *WAVE_D_FAMILY_MANIFESTS,
        SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
    )
    reviewed = CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS
    legacy = LEGACY_REVIEWED_FAMILY_MANIFESTS
    manifests = (*scheduled_reviewed, *legacy)
    if (
        len(scheduled_reviewed) != _CURRENT_REVIEWED_FAMILY_COUNT
        or len(reviewed) != _CURRENT_REVIEWED_FAMILY_COUNT
        or _manifest_identities(scheduled_reviewed) != _manifest_identities(reviewed)
        or len(legacy) != _CURRENT_LEGACY_FAMILY_COUNT
        or len(manifests) != CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT
        or len(_manifest_identities(manifests)) != len(set(_manifest_identities(manifests)))
        or sum(len(item.operations) for item in manifests)
        != CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT
    ):
        _fail("current_managed_verification/manifests")

    formal_specs = current_freecad_intent_capability_specs()
    promotion_specs = current_freecad_intent_promotion_specs()
    if (
        len(formal_specs) != CURRENT_MANAGED_VERIFICATION_FORMAL_OPERATION_COUNT
        or len(promotion_specs) != CURRENT_MANAGED_VERIFICATION_PROMOTION_OPERATION_COUNT
        or len({item.native_type_id for item in promotion_specs})
        != CURRENT_MANAGED_VERIFICATION_NATIVE_TYPE_COUNT
        or any(item.verification is not None for item in (*formal_specs, *promotion_specs))
    ):
        _fail("current_managed_verification/capability_specs")
    return manifests, formal_specs, promotion_specs


def _receipt_from_pair(
    value: object,
    *,
    path: str,
) -> ReviewedVerificationReceipt:
    if type(value) is not tuple or len(value) != 2:
        _fail(path)
    receipt, binding = value
    if (
        type(receipt) is not ReviewedVerificationReceipt
        or type(binding) is not FreeCadPromotionVerificationBinding
        or binding.test_receipt_sha256 != receipt.test_receipt_sha256
        or binding.test_contract_sha256 != receipt.test_contract_sha256
        or binding.runtime_build_sha256 != receipt.contract.runtime_backend.build_fingerprint_sha256
    ):
        _fail(path)
    return receipt


def _require_receipts(
    value: object,
    *,
    count: int,
    path: str,
) -> tuple[ReviewedVerificationReceipt, ...]:
    if (
        type(value) is not tuple
        or len(value) != count
        or any(type(item) is not ReviewedVerificationReceipt for item in value)
    ):
        _fail(path)
    return value


def _collect_current_receipts(*, freecad: object) -> tuple[ReviewedVerificationReceipt, ...]:
    core = _receipt_from_pair(
        build_part_core_managed_verification(freecad),
        path="current_managed_verification/part_a/core",
    )
    curves = _receipt_from_pair(
        build_part_curve_managed_verification(freecad),
        path="current_managed_verification/part_a/curves",
    )
    part_b = _require_receipts(
        build_managed_freecad_part_b_verification_receipts(freecad=freecad),
        count=4,
        path="current_managed_verification/part_b",
    )
    wave_c_raw = build_sketch_and_app_managed_verification(freecad=freecad)
    if type(wave_c_raw) is not tuple or len(wave_c_raw) != 2:
        _fail("current_managed_verification/wave_c")
    wave_c = tuple(
        _receipt_from_pair(
            item,
            path=f"current_managed_verification/wave_c/{index}",
        )
        for index, item in enumerate(wave_c_raw)
    )
    wave_d_batch = build_managed_freecad_wave_d_verification(freecad=freecad)
    if type(wave_d_batch) is not WaveDManagedVerificationBatch:
        _fail("current_managed_verification/wave_d")
    wave_d = _require_receipts(
        wave_d_batch.receipts,
        count=3,
        path="current_managed_verification/wave_d",
    )
    sketch_bootstrap = _receipt_from_pair(
        build_sketch_bootstrap_managed_verification(freecad=freecad),
        path="current_managed_verification/sketch_bootstrap",
    )
    legacy_raw = build_managed_freecad_legacy_reviewed_verification_receipts(freecad=freecad)
    if type(legacy_raw) is not tuple or len(legacy_raw) != 8:
        _fail("current_managed_verification/legacy")
    legacy = tuple(
        _receipt_from_pair(
            item,
            path=f"current_managed_verification/legacy/{index}",
        )
        for index, item in enumerate(legacy_raw)
    )
    receipts = (core, curves, *part_b, *wave_c, *wave_d, sketch_bootstrap, *legacy)
    if len(receipts) != CURRENT_MANAGED_VERIFICATION_RECEIPT_COUNT:
        _fail("current_managed_verification/receipts")
    return receipts


def _snapshot_documents(freecad: object) -> dict[str, object]:
    try:
        documents = freecad.listDocuments()
    except BaseException:
        _fail("current_managed_verification/documents")
    if type(documents) is not dict:
        _fail("current_managed_verification/documents")
    return dict(documents)


def _close_documents_created_after(
    *,
    freecad: object,
    before: dict[str, object],
) -> None:
    try:
        current = freecad.listDocuments()
        if type(current) is not dict:
            _fail("current_managed_verification/cleanup")
        owned = tuple((name, document) for name, document in current.items() if name not in before)
        for name, document in owned:
            latest = freecad.listDocuments()
            if type(latest) is not dict:
                _fail("current_managed_verification/cleanup")
            if latest.get(name) is document:
                freecad.closeDocument(name)
    except CapabilityCatalogError:
        raise
    except BaseException:
        _fail("current_managed_verification/cleanup")


def build_current_managed_freecad_reviewed_verification_set_for_maintainers(
    *,
    freecad: object,
) -> FreeCadManagedReviewedVerificationSet:
    """Run the exact current 125-by-seven maintainer/CI verification matrix.

    The caller must inject the already authenticated, headless managed FreeCAD
    module.  All twenty family verifiers execute sequentially against that
    same object.  The returned set is ephemeral metadata: this function has no
    storage, public-query, promotion, adapter-dispatch, or execution seam.
    """

    if freecad is None:
        _fail("current_managed_verification/freecad")
    if not _VERIFICATION_LOCK.acquire(blocking=False):
        _fail("current_managed_verification/concurrent_verification")
    before: dict[str, object] | None = None
    try:
        before = _snapshot_documents(freecad)
        manifests, formal_specs, promotion_specs = _current_exact_inputs()
        receipts = _collect_current_receipts(freecad=freecad)
        if tuple(item.contract.family_manifest_sha256 for item in receipts) != tuple(
            item.manifest_sha256 for item in manifests
        ):
            _fail("current_managed_verification/receipt_order")
        runtime_backend = receipts[0].contract.runtime_backend
        return build_managed_reviewed_verification_set(
            runtime_backend=runtime_backend,
            receipts=receipts,
            manifests=manifests,
            formal_specs=formal_specs,
            promotion_specs=promotion_specs,
        )
    finally:
        try:
            if before is not None:
                _close_documents_created_after(freecad=freecad, before=before)
        finally:
            _VERIFICATION_LOCK.release()


__all__ = ()
