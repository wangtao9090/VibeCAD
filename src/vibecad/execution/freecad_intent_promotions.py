"""Build native TypeId promotion packs from reviewed intent capability specs.

Formal operation descriptors and native TypeIds serve different purposes.  A
formal descriptor says which semantic operation an adapter implements; a
promotion pack says that the exact discovered native TypeId is executable by
that adapter.  This module performs that one generic join without teaching the
capability projection any feature-specific semantics.

Verification is deliberately separate.  A native TypeId becomes ``verified``
only when the caller supplies the stronger build-, adapter-, test-contract-,
and receipt-bound verification record required by capability projection v2.
"""

from __future__ import annotations

import hashlib
import json

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityFact,
    CapabilitySupportStatus,
    CapabilityTermRef,
)
from vibecad.execution.freecad_capabilities import FreeCadNativeTypeCategory
from vibecad.execution.freecad_capability_projection_v2 import (
    FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
    FreeCadCapabilityPromotionEntry,
    FreeCadCapabilityPromotionPack,
    FreeCadCapabilitySemanticKind,
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_discovery_v2 import FreeCadPagedCapabilityCatalog
from vibecad.execution.freecad_intent_capabilities import (
    MAX_FREECAD_INTENT_CAPABILITY_SPECS,
    FreeCadIntentCapabilitySpec,
)

_PACK_ID_DOMAIN = b"vibecad-freecad-intent-promotion-pack-id-v1\0"
_LANE_ID_DOMAIN = b"vibecad-freecad-intent-promotion-lane-id-v1\0"

_TERM_SPECS = {
    "fact.intent.promotion.adapter": "fact/intent-promotion-adapter",
    "fact.intent.promotion.operations": "fact/intent-promotion-operations",
    "semantic.intent.native_execution": "semantic/intent-native-execution",
}


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _terms() -> tuple[CapabilityTermRef, ...]:
    return tuple(
        CapabilityTermRef(
            term_ref_id=term_ref_id,
            namespace="vcad.intent.native-promotion",
            vocabulary_version="1.0",
            term_id=term_id,
            term_definition_sha256=hashlib.sha256(
                f"vcad.intent.native-promotion/1.0/{term_id}".encode("ascii")
            ).hexdigest(),
        )
        for term_ref_id, term_id in sorted(_TERM_SPECS.items())
    )


def _adapter_identity(value: FreeCadIntentCapabilitySpec) -> tuple[str, str, str]:
    return value.adapter_id, value.adapter_version, value.adapter_contract_sha256


def _operation_mapping(value: FreeCadIntentCapabilitySpec) -> dict[str, str]:
    return {
        "operation_id": value.operation_id,
        "rule_contract_sha256": value.rule_contract_sha256,
        "rule_id": value.rule_id,
        "semantic_operation": value.semantic_operation,
    }


def _canonical(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs")
    if not raw or len(raw) > 2 * 1024 * 1024:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "specs")
    return raw


def _pack_identity(
    *,
    adapter_identity: tuple[str, str, str],
    specs: tuple[FreeCadIntentCapabilitySpec, ...],
    discovery: FreeCadPagedCapabilityCatalog,
) -> tuple[str, str]:
    adapter_id, adapter_version, adapter_contract = adapter_identity
    lane_sha256 = hashlib.sha256(
        _LANE_ID_DOMAIN
        + _canonical(
            {
                "adapter_contract_sha256": adapter_contract,
                "adapter_id": adapter_id,
                "adapter_version": adapter_version,
            }
        )
    ).hexdigest()
    pack_sha256 = hashlib.sha256(
        _PACK_ID_DOMAIN
        + _canonical(
            {
                "adapter": list(adapter_identity),
                "discovery_manifest_sha256": discovery.manifest.manifest_sha256,
                "discovery_snapshot_sha256": discovery.snapshot.snapshot_sha256,
                "operations": [_operation_mapping(item) for item in specs],
            }
        )
    ).hexdigest()
    return f"freecad.intent.lane.{lane_sha256[:32]}", f"freecad.intent.pack.{pack_sha256[:32]}"


def _validated_inputs(
    *,
    discovery: object,
    specs: object,
    verification_by_native_type: object,
) -> tuple[
    FreeCadPagedCapabilityCatalog,
    tuple[FreeCadIntentCapabilitySpec, ...],
    dict[str, FreeCadPromotionVerificationBinding],
]:
    if type(discovery) is not FreeCadPagedCapabilityCatalog:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "discovery")
    if (
        type(specs) is not tuple
        or not specs
        or not all(type(item) is FreeCadIntentCapabilitySpec for item in specs)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs")
    if len(specs) > MAX_FREECAD_INTENT_CAPABILITY_SPECS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "specs")
    if len({item.operation_id for item in specs}) != len(specs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs/operation_id")
    if len({item.semantic_operation for item in specs}) != len(specs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs/semantic_operation")
    if any(item.verification is not None for item in specs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs/verification")
    verification = {} if verification_by_native_type is None else verification_by_native_type
    if type(verification) is not dict or not all(
        type(key) is str and type(value) is FreeCadPromotionVerificationBinding
        for key, value in verification.items()
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification_by_native_type")
    native_by_id = {item.native_type_id: item for item in discovery.snapshot.registered_types}
    requested = {item.native_type_id for item in specs}
    if not requested <= set(native_by_id) or not set(verification) <= requested:
        _fail(CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "specs/native_type_id")
    if any(
        native_by_id[native_type_id].category is not FreeCadNativeTypeCategory.DOCUMENT_OBJECT
        for native_type_id in requested
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs/native_type_id")
    adapter_by_id: dict[str, tuple[str, str, str]] = {}
    for item in specs:
        identity = _adapter_identity(item)
        prior = adapter_by_id.setdefault(item.adapter_id, identity)
        if prior != identity:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "specs/adapter")
    return discovery, tuple(sorted(specs, key=lambda item: item.operation_id)), verification


def _entry(
    *,
    native_type_id: str,
    specs: tuple[FreeCadIntentCapabilitySpec, ...],
    verification: FreeCadPromotionVerificationBinding | None,
) -> FreeCadCapabilityPromotionEntry:
    first = specs[0]
    structural = {
        (item.risk_class, item.execution_profiles, item.lifecycle_stages) for item in specs
    }
    identities = {_adapter_identity(item) for item in specs}
    if len(structural) != 1 or len(identities) != 1:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "specs/native_type_id")
    target_status = (
        CapabilitySupportStatus.VERIFIED
        if verification is not None
        else CapabilitySupportStatus.EXECUTABLE
    )
    return FreeCadCapabilityPromotionEntry(
        native_type_id=native_type_id,
        semantic_kind=FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT,
        target_status=target_status,
        risk_class=first.risk_class,
        semantic_term_ref_ids=("semantic.intent.native_execution",),
        facts=(
            CapabilityFact(
                key_term_ref_id="fact.intent.promotion.adapter",
                value={
                    "contract_sha256": first.adapter_contract_sha256,
                    "id": first.adapter_id,
                    "version": first.adapter_version,
                },
            ),
            CapabilityFact(
                key_term_ref_id="fact.intent.promotion.operations",
                value=[_operation_mapping(item) for item in specs],
            ),
        ),
        execution_profiles=first.execution_profiles,
        lifecycle_stages=first.lifecycle_stages,
        verification=verification,
    )


def build_freecad_intent_capability_promotion_packs(
    *,
    discovery: FreeCadPagedCapabilityCatalog,
    specs: tuple[FreeCadIntentCapabilitySpec, ...],
    verification_by_native_type: dict[str, FreeCadPromotionVerificationBinding] | None = None,
) -> tuple[FreeCadCapabilityPromotionPack, ...]:
    """Promote exact discovered TypeIds through static reviewed adapter specs."""

    discovery, specs, verification = _validated_inputs(
        discovery=discovery,
        specs=specs,
        verification_by_native_type=verification_by_native_type,
    )
    by_native: dict[str, list[FreeCadIntentCapabilitySpec]] = {}
    for item in specs:
        by_native.setdefault(item.native_type_id, []).append(item)
    if any(len({_adapter_identity(item) for item in values}) != 1 for values in by_native.values()):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "specs/native_type_id")

    by_adapter: dict[tuple[str, str, str], list[FreeCadIntentCapabilitySpec]] = {}
    for item in specs:
        by_adapter.setdefault(_adapter_identity(item), []).append(item)
    packs: list[FreeCadCapabilityPromotionPack] = []
    for adapter_identity, values in sorted(by_adapter.items()):
        ordered = tuple(sorted(values, key=lambda item: item.operation_id))
        lane_id, pack_id = _pack_identity(
            adapter_identity=adapter_identity,
            specs=ordered,
            discovery=discovery,
        )
        native_ids = sorted({item.native_type_id for item in ordered})
        entries = tuple(
            _entry(
                native_type_id=native_type_id,
                specs=tuple(item for item in ordered if item.native_type_id == native_type_id),
                verification=verification.get(native_type_id),
            )
            for native_type_id in native_ids
        )
        packs.append(
            FreeCadCapabilityPromotionPack(
                schema_version=FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
                pack_id=pack_id,
                lane_id=lane_id,
                adapter_id=adapter_identity[0],
                adapter_version=adapter_identity[1],
                adapter_contract_sha256=adapter_identity[2],
                discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
                discovery_manifest_sha256=discovery.manifest.manifest_sha256,
                backend=discovery.snapshot.backend,
                terms=_terms(),
                entries=entries,
            )
        )
    return tuple(sorted(packs, key=lambda item: item.pack_sha256))


__all__ = ()
