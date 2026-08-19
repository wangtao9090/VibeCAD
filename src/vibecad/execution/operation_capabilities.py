"""Project the trusted operation registry into executable capability metadata.

The registry already binds validated model-program fields to reviewed adapter
handlers.  This projection describes that binding without invoking a handler.
Operations are ``executable`` by default; a release/conformance gate must
provide an exact receipt before an individual descriptor becomes ``verified``.
"""

from __future__ import annotations

import hashlib
import json

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
from vibecad.execution.registry import (
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    ResultSlotMetadata,
    RiskClass,
)

_REGISTRY_RECEIPT_DOMAIN = b"vibecad-operation-capability-registry-v1\0"
_MODULE_CAPABILITY_ID = "vibecad.module.execution.registry"


def operation_capability_id(operation: str) -> str:
    if type(operation) is not str or not operation:
        raise CapabilityCatalogError(CapabilityCatalogErrorCode.INVALID_INPUT, "operation")
    return f"vibecad.operation.{operation}"


def _field_mapping(value: FieldMetadata) -> dict[str, object]:
    return {
        "allowed_units": list(value.allowed_units),
        "enum_values": list(value.enum_values),
        "handler_parameter": value.handler_parameter,
        "name": value.name,
        "referenced_value_shape": (
            None if value.referenced_value_shape is None else value.referenced_value_shape.value
        ),
        "required": value.required,
        "value_shape": value.value_shape.value,
    }


def _slot_mapping(value: ResultSlotMetadata) -> dict[str, object]:
    return {
        "allowed_units": list(value.allowed_units),
        "enum_values": list(value.enum_values),
        "name": value.name,
        "result_field": value.result_field,
        "value_shape": value.value_shape.value,
    }


def _operation_mapping(value: OperationMetadata) -> dict[str, object]:
    return {
        "argument_fields": [_field_mapping(item) for item in value.argument_fields],
        "description": value.description,
        "direct_exposed": value.direct_exposed,
        "evidence_required": value.evidence_required,
        "execution_profiles": [item.value for item in value.execution_profiles],
        "handler_name": value.handler_name,
        "maximum_freecad_version_exclusive": list(value.maximum_freecad_version_exclusive),
        "minimum_freecad_version": list(value.minimum_freecad_version),
        "operation": value.operation,
        "preservation_fields": list(value.preservation_fields),
        "requires_gui_main_thread": value.requires_gui_main_thread,
        "resource_budget": {
            "max_created_objects": value.resource_budget.max_created_objects,
            "max_result_bytes": value.resource_budget.max_result_bytes,
            "max_runtime_ms": value.resource_budget.max_runtime_ms,
        },
        "result_slots": [_slot_mapping(item) for item in value.result_slots],
        "risk_class": value.risk_class.value,
        "target_fields": [_field_mapping(item) for item in value.target_fields],
    }


def _registry_receipt(registry: OperationRegistry) -> str:
    body = [_operation_mapping(registry.operations[name]) for name in sorted(registry)]
    raw = json.dumps(
        body,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(_REGISTRY_RECEIPT_DOMAIN + raw).hexdigest()


_TERM_SPECS = {
    "fact.operation.description": "fact/operation-description",
    "fact.operation.direct_exposed": "fact/operation-direct-exposed",
    "fact.operation.evidence_required": "fact/operation-evidence-required",
    "fact.operation.fields": "fact/operation-fields",
    "fact.operation.freecad_version_range": "fact/operation-freecad-version-range",
    "fact.operation.handler_binding": "fact/operation-handler-binding",
    "fact.operation.preservation_fields": "fact/operation-preservation-fields",
    "fact.operation.requires_gui_main_thread": "fact/operation-requires-gui-main-thread",
    "fact.operation.resource_budget": "fact/operation-resource-budget",
    "fact.operation.result_slots": "fact/operation-result-slots",
    "semantic.vibecad.execution_module": "semantic/vibecad-execution-module",
    "semantic.vibecad.operation": "semantic/vibecad-operation",
}


def _terms() -> tuple[CapabilityTermRef, ...]:
    return tuple(
        CapabilityTermRef(
            term_ref_id=term_ref_id,
            namespace="vcad.operation.capability",
            vocabulary_version="1.0",
            term_id=term_id,
            term_definition_sha256=hashlib.sha256(
                f"vcad.operation.capability/1.0/{term_id}".encode("ascii")
            ).hexdigest(),
        )
        for term_ref_id, term_id in sorted(_TERM_SPECS.items())
    )


def _risk(value: RiskClass) -> CapabilityRiskClass:
    return {
        RiskClass.READ_ONLY: CapabilityRiskClass.READ_ONLY,
        RiskClass.MUTATING: CapabilityRiskClass.MUTATING,
        RiskClass.DESTRUCTIVE: CapabilityRiskClass.DESTRUCTIVE,
    }[value]


def _facts(value: OperationMetadata) -> tuple[CapabilityFact, ...]:
    return (
        CapabilityFact(
            key_term_ref_id="fact.operation.description",
            value=value.description,
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.direct_exposed",
            value=value.direct_exposed,
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.evidence_required",
            value=value.evidence_required,
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.fields",
            value={
                "arguments": [_field_mapping(item) for item in value.argument_fields],
                "targets": [_field_mapping(item) for item in value.target_fields],
            },
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.freecad_version_range",
            value={
                "maximum_exclusive": list(value.maximum_freecad_version_exclusive),
                "minimum": list(value.minimum_freecad_version),
            },
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.handler_binding",
            value=value.handler_name,
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.preservation_fields",
            value=list(value.preservation_fields),
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.requires_gui_main_thread",
            value=value.requires_gui_main_thread,
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.resource_budget",
            value={
                "max_created_objects": value.resource_budget.max_created_objects,
                "max_result_bytes": value.resource_budget.max_result_bytes,
                "max_runtime_ms": value.resource_budget.max_runtime_ms,
            },
        ),
        CapabilityFact(
            key_term_ref_id="fact.operation.result_slots",
            value=[_slot_mapping(item) for item in value.result_slots],
        ),
    )


def build_operation_capability_catalog(
    *,
    registry: OperationRegistry,
    backend: CapabilityBackend,
    verification_by_operation: dict[str, CapabilityVerificationRef] | None = None,
) -> CapabilityCatalogSegment:
    """Build an exact executable/verified projection of one reviewed registry."""

    if type(registry) is not OperationRegistry:
        raise CapabilityCatalogError(CapabilityCatalogErrorCode.INVALID_INPUT, "registry")
    if type(backend) is not CapabilityBackend:
        raise CapabilityCatalogError(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
    verification = {} if verification_by_operation is None else verification_by_operation
    if type(verification) is not dict or not all(
        type(name) is str and type(receipt) is CapabilityVerificationRef
        for name, receipt in verification.items()
    ):
        raise CapabilityCatalogError(
            CapabilityCatalogErrorCode.INVALID_INPUT,
            "verification_by_operation",
        )
    unknown = set(verification) - set(registry.operations)
    if unknown:
        raise CapabilityCatalogError(
            CapabilityCatalogErrorCode.UNKNOWN_REFERENCE,
            "verification_by_operation",
        )
    receipt_sha256 = _registry_receipt(registry)
    descriptors = [
        CapabilityDescriptor(
            capability_id=_MODULE_CAPABILITY_ID,
            kind=CapabilityKind.MODULE,
            native_identifier="vibecad.execution.registry",
            declaring_module_id=_MODULE_CAPABILITY_ID,
            status=CapabilitySupportStatus.REPRESENTABLE,
            risk_class=CapabilityRiskClass.READ_ONLY,
            semantic_term_ref_ids=("semantic.vibecad.execution_module",),
        )
    ]
    for name in sorted(registry):
        metadata = registry.operations[name]
        operation_verification = verification.get(name)
        descriptors.append(
            CapabilityDescriptor(
                capability_id=operation_capability_id(name),
                kind=CapabilityKind.OPERATION,
                native_identifier=name,
                declaring_module_id=_MODULE_CAPABILITY_ID,
                status=(
                    CapabilitySupportStatus.VERIFIED
                    if operation_verification is not None
                    else CapabilitySupportStatus.EXECUTABLE
                ),
                risk_class=_risk(metadata.risk_class),
                semantic_term_ref_ids=("semantic.vibecad.operation",),
                facts=_facts(metadata),
                execution_profiles=tuple(
                    CapabilityExecutionProfile(profile.value)
                    for profile in metadata.execution_profiles
                ),
                lifecycle_stages=(
                    CapabilityLifecycleStage.INSPECT
                    if metadata.risk_class is RiskClass.READ_ONLY
                    else CapabilityLifecycleStage.EXECUTE,
                ),
                verification=operation_verification,
            )
        )
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"vibecad.operations.{receipt_sha256[:32]}",
        backend=backend,
        discovery_receipt_sha256=receipt_sha256,
        discovery_algorithm_id="vcad.operation.registry.projection",
        discovery_algorithm_version="1.0",
        terms=_terms(),
        descriptors=tuple(descriptors),
    )


__all__ = ()
