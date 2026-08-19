"""Project reviewed intent adapters into formal FreeCAD capabilities.

This module is the single metadata seam between backend-neutral intent packs
and the FreeCAD capability projection.  A pack contributes immutable specs;
the builder emits ordinary executable capability descriptors whose
``native_identifier`` is the exact FreeCAD TypeId.  Discovery remains the
authority for whether that TypeId exists, and a descriptor is never marked
verified without an exact verification receipt.

The catalog is metadata only.  It does not import an adapter, select a rule,
lower an intent, execute FreeCAD, or grant adoption authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityFact,
    CapabilityKind,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityTermRef,
    CapabilityVerificationRef,
)

MAX_FREECAD_INTENT_CAPABILITY_SPECS = 192

_RECEIPT_DOMAIN = b"vibecad-freecad-intent-capability-specs-v1\0"
_MODULE_CAPABILITY_ID = "vibecad.module.intent.adapters"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,127}$")
_TERM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,191}$")
_TYPE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*::[A-Za-z][A-Za-z0-9_]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _text(value: object, path: str, *, maximum: int = 128, term: bool = False) -> str:
    pattern = _TERM if term else _IDENTIFIER
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if not value or size > maximum or pattern.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    if ".." in value or "//" in value:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return value


def _exact_enum_tuple(
    value: object,
    path: str,
    *,
    item_type: type,
    maximum: int,
) -> tuple:
    if (
        type(value) is not tuple
        or not value
        or len(value) > maximum
        or not all(type(item) is item_type for item in value)
        or len(set(value)) != len(value)
    ):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, path)
    return tuple(sorted(value, key=str))


@dataclass(frozen=True, slots=True, kw_only=True)
class FreeCadIntentCapabilitySpec:
    """One reviewed semantic operation implemented by one trusted adapter."""

    operation_id: str
    semantic_operation: str
    native_type_id: str
    adapter_id: str
    adapter_version: str
    adapter_contract_sha256: str
    rule_id: str
    rule_contract_sha256: str
    risk_class: CapabilityRiskClass = CapabilityRiskClass.MUTATING
    execution_profiles: tuple[CapabilityExecutionProfile, ...] = (
        CapabilityExecutionProfile.HEADLESS,
    )
    lifecycle_stages: tuple[CapabilityLifecycleStage, ...] = (CapabilityLifecycleStage.EXECUTE,)
    verification: CapabilityVerificationRef | None = None

    def __post_init__(self) -> None:
        _text(self.operation_id, "operation_id", maximum=96)
        _text(self.semantic_operation, "semantic_operation", maximum=192, term=True)
        _text(self.native_type_id, "native_type_id", maximum=192, term=True)
        if _TYPE_ID.fullmatch(self.native_type_id) is None:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "native_type_id")
        _text(self.adapter_id, "adapter_id")
        _text(self.adapter_version, "adapter_version")
        _digest(self.adapter_contract_sha256, "adapter_contract_sha256")
        _text(self.rule_id, "rule_id")
        _digest(self.rule_contract_sha256, "rule_contract_sha256")
        if type(self.risk_class) is not CapabilityRiskClass:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "risk_class")
        if self.risk_class is CapabilityRiskClass.UNKNOWN:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "risk_class")
        profiles = _exact_enum_tuple(
            self.execution_profiles,
            "execution_profiles",
            item_type=CapabilityExecutionProfile,
            maximum=3,
        )
        lifecycle = _exact_enum_tuple(
            self.lifecycle_stages,
            "lifecycle_stages",
            item_type=CapabilityLifecycleStage,
            maximum=16,
        )
        if CapabilityLifecycleStage.EXECUTE not in lifecycle:
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "lifecycle_stages")
        if (
            self.verification is not None
            and type(self.verification) is not CapabilityVerificationRef
        ):
            _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "verification")
        object.__setattr__(self, "execution_profiles", profiles)
        object.__setattr__(self, "lifecycle_stages", lifecycle)

    @property
    def capability_id(self) -> str:
        return f"vibecad.intent.operation.{self.operation_id}"


_TERM_SPECS = {
    "fact.intent.adapter_binding": "fact/intent-adapter-binding",
    "fact.intent.native_type": "fact/intent-native-type",
    "fact.intent.rule_binding": "fact/intent-rule-binding",
    "fact.intent.semantic_operation": "fact/intent-semantic-operation",
    "semantic.intent.module": "semantic/intent-adapter-module",
    "semantic.intent.operation": "semantic/intent-operation",
}


def _terms() -> tuple[CapabilityTermRef, ...]:
    return tuple(
        CapabilityTermRef(
            term_ref_id=term_ref_id,
            namespace="vcad.intent.capability",
            vocabulary_version="1.0",
            term_id=term_id,
            term_definition_sha256=hashlib.sha256(
                f"vcad.intent.capability/1.0/{term_id}".encode("ascii")
            ).hexdigest(),
        )
        for term_ref_id, term_id in sorted(_TERM_SPECS.items())
    )


def _verification_mapping(value: CapabilityVerificationRef | None) -> object:
    if value is None:
        return None
    return {
        "receipt_sha256": value.receipt_sha256,
        "receipt_size_bytes": value.receipt_size_bytes,
        "verifier_id": value.verifier_id,
        "verifier_version": value.verifier_version,
    }


def _spec_mapping(value: FreeCadIntentCapabilitySpec) -> dict[str, object]:
    return {
        "adapter": {
            "contract_sha256": value.adapter_contract_sha256,
            "id": value.adapter_id,
            "version": value.adapter_version,
        },
        "lifecycle_stages": [item.value for item in value.lifecycle_stages],
        "native_type_id": value.native_type_id,
        "operation_id": value.operation_id,
        "profiles": [item.value for item in value.execution_profiles],
        "risk_class": value.risk_class.value,
        "rule": {
            "contract_sha256": value.rule_contract_sha256,
            "id": value.rule_id,
        },
        "semantic_operation": value.semantic_operation,
        "verification": _verification_mapping(value.verification),
    }


def _receipt_sha256(specs: tuple[FreeCadIntentCapabilitySpec, ...]) -> str:
    try:
        raw = json.dumps(
            [_spec_mapping(item) for item in specs],
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs")
    return hashlib.sha256(_RECEIPT_DOMAIN + raw).hexdigest()


def _facts(value: FreeCadIntentCapabilitySpec) -> tuple[CapabilityFact, ...]:
    return (
        CapabilityFact(
            key_term_ref_id="fact.intent.adapter_binding",
            value={
                "contract_sha256": value.adapter_contract_sha256,
                "id": value.adapter_id,
                "version": value.adapter_version,
            },
        ),
        CapabilityFact(
            key_term_ref_id="fact.intent.native_type",
            value=value.native_type_id,
        ),
        CapabilityFact(
            key_term_ref_id="fact.intent.rule_binding",
            value={
                "contract_sha256": value.rule_contract_sha256,
                "id": value.rule_id,
            },
        ),
        CapabilityFact(
            key_term_ref_id="fact.intent.semantic_operation",
            value=value.semantic_operation,
        ),
    )


def build_freecad_intent_capability_catalog(
    *,
    backend: CapabilityBackend,
    specs: tuple[FreeCadIntentCapabilitySpec, ...],
) -> CapabilityCatalogSegment:
    """Build one deterministic formal catalog from reviewed adapter specs."""

    if type(backend) is not CapabilityBackend:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
    if type(specs) is not tuple or not specs:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs")
    if len(specs) > MAX_FREECAD_INTENT_CAPABILITY_SPECS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "specs")
    if not all(type(item) is FreeCadIntentCapabilitySpec for item in specs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs")
    ordered = tuple(sorted(specs, key=lambda item: item.operation_id))
    if len({item.operation_id for item in ordered}) != len(ordered):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs/operation_id")
    if len({item.semantic_operation for item in ordered}) != len(ordered):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "specs/semantic_operation")

    receipt_sha256 = _receipt_sha256(ordered)
    descriptors = [
        CapabilityDescriptor(
            capability_id=_MODULE_CAPABILITY_ID,
            kind=CapabilityKind.MODULE,
            native_identifier="vibecad.intent.adapters",
            declaring_module_id=_MODULE_CAPABILITY_ID,
            status=CapabilitySupportStatus.REPRESENTABLE,
            risk_class=CapabilityRiskClass.READ_ONLY,
            semantic_term_ref_ids=("semantic.intent.module",),
        )
    ]
    for item in ordered:
        descriptors.append(
            CapabilityDescriptor(
                capability_id=item.capability_id,
                kind=CapabilityKind.OPERATION,
                native_identifier=item.native_type_id,
                declaring_module_id=_MODULE_CAPABILITY_ID,
                status=(
                    CapabilitySupportStatus.VERIFIED
                    if item.verification is not None
                    else CapabilitySupportStatus.EXECUTABLE
                ),
                risk_class=item.risk_class,
                semantic_term_ref_ids=("semantic.intent.operation",),
                facts=_facts(item),
                execution_profiles=item.execution_profiles,
                lifecycle_stages=item.lifecycle_stages,
                verification=item.verification,
            )
        )
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"vibecad.intent.adapters.{receipt_sha256[:32]}",
        backend=backend,
        discovery_receipt_sha256=receipt_sha256,
        discovery_algorithm_id="vcad.intent.adapter.capability.projection",
        discovery_algorithm_version="1.0",
        terms=_terms(),
        descriptors=tuple(descriptors),
    )


__all__ = ()
