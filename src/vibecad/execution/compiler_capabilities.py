"""Capability projection for the currently reviewed CAD compiler families."""

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
    CapabilityRelation,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityTermRef,
    CapabilityVerificationRef,
    ExternalCapabilityRef,
)
from vibecad.freeform.contracts import FreeformFeatureKind
from vibecad.parametric.compiler import (
    _EDGE_TREATMENT_TYPE_IDS,
    _FEATURE_TYPE_IDS,
)
from vibecad.parametric.contracts import EdgeTreatmentKind, FeatureKind

_COMPILER_RECEIPT_DOMAIN = b"vibecad-current-compiler-capabilities-v1\0"
_PARAMETRIC_MODULE_ID = "vibecad.module.parametric.compiler"
_FREEFORM_MODULE_ID = "vibecad.module.freeform.compiler"

_FREEFORM_NATIVE_BINDINGS = {
    FreeformFeatureKind.LOFT: "Part.makeLoft",
    FreeformFeatureKind.SWEEP: "Part.Wire.makePipeShell",
}


def parametric_feature_capability_id(kind: FeatureKind) -> str:
    if type(kind) is not FeatureKind:
        raise CapabilityCatalogError(CapabilityCatalogErrorCode.INVALID_INPUT, "kind")
    return f"vibecad.compiler.parametric.feature.{kind.value}"


def edge_treatment_capability_id(kind: EdgeTreatmentKind) -> str:
    if type(kind) is not EdgeTreatmentKind:
        raise CapabilityCatalogError(CapabilityCatalogErrorCode.INVALID_INPUT, "kind")
    return f"vibecad.compiler.parametric.edge_treatment.{kind.value}"


def freeform_feature_capability_id(kind: FreeformFeatureKind) -> str:
    if type(kind) is not FreeformFeatureKind:
        raise CapabilityCatalogError(CapabilityCatalogErrorCode.INVALID_INPUT, "kind")
    return f"vibecad.compiler.freeform.feature.{kind.value}"


_TERM_SPECS = {
    "fact.compiler.contract_family": "fact/compiler-contract-family",
    "fact.compiler.contract_value": "fact/compiler-contract-value",
    "relation.compiler.targets_native": "relation/compiler-targets-native",
    "semantic.compiler.edge_treatment": "semantic/compiler-edge-treatment",
    "semantic.compiler.freeform_feature": "semantic/compiler-freeform-feature",
    "semantic.compiler.module": "semantic/compiler-module",
    "semantic.compiler.parametric_feature": "semantic/compiler-parametric-feature",
}


def _terms() -> tuple[CapabilityTermRef, ...]:
    return tuple(
        CapabilityTermRef(
            term_ref_id=term_ref_id,
            namespace="vcad.compiler.capability",
            vocabulary_version="1.0",
            term_id=term_id,
            term_definition_sha256=hashlib.sha256(
                f"vcad.compiler.capability/1.0/{term_id}".encode("ascii")
            ).hexdigest(),
        )
        for term_ref_id, term_id in sorted(_TERM_SPECS.items())
    )


def _compiler_facts(family: str, value: str) -> tuple[CapabilityFact, ...]:
    return (
        CapabilityFact(
            key_term_ref_id="fact.compiler.contract_family",
            value=family,
        ),
        CapabilityFact(
            key_term_ref_id="fact.compiler.contract_value",
            value=value,
        ),
    )


def _source_mapping() -> list[dict[str, str]]:
    result = [
        {
            "capability_id": parametric_feature_capability_id(kind),
            "contract_family": "parametric_feature",
            "contract_value": kind.value,
            "native_identifier": native_identifier,
        }
        for kind, native_identifier in sorted(
            _FEATURE_TYPE_IDS.items(), key=lambda item: item[0].value
        )
    ]
    result.extend(
        {
            "capability_id": edge_treatment_capability_id(kind),
            "contract_family": "edge_treatment",
            "contract_value": kind.value,
            "native_identifier": native_identifier,
        }
        for kind, native_identifier in sorted(
            _EDGE_TREATMENT_TYPE_IDS.items(), key=lambda item: item[0].value
        )
    )
    result.extend(
        {
            "capability_id": freeform_feature_capability_id(kind),
            "contract_family": "freeform_feature",
            "contract_value": kind.value,
            "native_identifier": native_identifier,
        }
        for kind, native_identifier in sorted(
            _FREEFORM_NATIVE_BINDINGS.items(), key=lambda item: item[0].value
        )
    )
    return sorted(result, key=lambda item: item["capability_id"])


def _receipt_sha256() -> str:
    raw = json.dumps(
        _source_mapping(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(_COMPILER_RECEIPT_DOMAIN + raw).hexdigest()


def _native_by_identifier(
    catalog: CapabilityCatalogSegment | None,
    backend: CapabilityBackend,
) -> dict[str, CapabilityDescriptor]:
    if catalog is None:
        return {}
    if type(catalog) is not CapabilityCatalogSegment:
        raise CapabilityCatalogError(
            CapabilityCatalogErrorCode.INVALID_INPUT,
            "native_type_catalog",
        )
    if catalog.backend != backend:
        raise CapabilityCatalogError(
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
            "native_type_catalog/backend",
        )
    result: dict[str, CapabilityDescriptor] = {}
    for descriptor in catalog.descriptors:
        if descriptor.kind not in {
            CapabilityKind.NATIVE_TYPE,
            CapabilityKind.DOCUMENT_OBJECT,
            CapabilityKind.PROPERTY_TYPE,
            CapabilityKind.EXTENSION_TYPE,
        }:
            continue
        if descriptor.native_identifier in result:
            raise CapabilityCatalogError(
                CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                "native_type_catalog/descriptors",
            )
        result[descriptor.native_identifier] = descriptor
    return result


def _verification_map(
    value: dict[str, CapabilityVerificationRef] | None,
    known_ids: set[str],
) -> dict[str, CapabilityVerificationRef]:
    result = {} if value is None else value
    if type(result) is not dict or not all(
        type(key) is str and type(item) is CapabilityVerificationRef for key, item in result.items()
    ):
        raise CapabilityCatalogError(
            CapabilityCatalogErrorCode.INVALID_INPUT,
            "verification_by_capability",
        )
    if not set(result) <= known_ids:
        raise CapabilityCatalogError(
            CapabilityCatalogErrorCode.UNKNOWN_REFERENCE,
            "verification_by_capability",
        )
    return result


def build_current_compiler_capability_catalog(
    *,
    backend: CapabilityBackend,
    native_type_catalog: CapabilityCatalogSegment | None = None,
    verification_by_capability: dict[str, CapabilityVerificationRef] | None = None,
) -> CapabilityCatalogSegment:
    """Describe the current parametric and freeform compiler envelope."""

    if type(backend) is not CapabilityBackend:
        raise CapabilityCatalogError(CapabilityCatalogErrorCode.INVALID_INPUT, "backend")
    source = _source_mapping()
    known_ids = {item["capability_id"] for item in source}
    verification = _verification_map(verification_by_capability, known_ids)
    native_descriptors = _native_by_identifier(native_type_catalog, backend)
    descriptors = [
        CapabilityDescriptor(
            capability_id=_PARAMETRIC_MODULE_ID,
            kind=CapabilityKind.MODULE,
            native_identifier="vibecad.parametric.compiler",
            declaring_module_id=_PARAMETRIC_MODULE_ID,
            status=CapabilitySupportStatus.REPRESENTABLE,
            risk_class=CapabilityRiskClass.READ_ONLY,
            semantic_term_ref_ids=("semantic.compiler.module",),
        ),
        CapabilityDescriptor(
            capability_id=_FREEFORM_MODULE_ID,
            kind=CapabilityKind.MODULE,
            native_identifier="vibecad.freeform.compiler",
            declaring_module_id=_FREEFORM_MODULE_ID,
            status=CapabilitySupportStatus.REPRESENTABLE,
            risk_class=CapabilityRiskClass.READ_ONLY,
            semantic_term_ref_ids=("semantic.compiler.module",),
        ),
    ]
    relations: list[CapabilityRelation] = []
    external_by_id: dict[str, ExternalCapabilityRef] = {}
    for item in source:
        capability_id = item["capability_id"]
        family = item["contract_family"]
        native_identifier = item["native_identifier"]
        module_id = _FREEFORM_MODULE_ID if family == "freeform_feature" else _PARAMETRIC_MODULE_ID
        semantic_term = {
            "parametric_feature": "semantic.compiler.parametric_feature",
            "edge_treatment": "semantic.compiler.edge_treatment",
            "freeform_feature": "semantic.compiler.freeform_feature",
        }[family]
        native = native_descriptors.get(native_identifier)
        dependency_ids = ()
        if native is not None:
            external_by_id[native.capability_id] = ExternalCapabilityRef(
                capability_id=native.capability_id,
                descriptor_sha256=native.descriptor_sha256,
            )
            dependency_ids = (native.capability_id,)
            relation_sha = hashlib.sha256(
                f"{capability_id}\0{native.capability_id}".encode("ascii")
            ).hexdigest()
            relations.append(
                CapabilityRelation(
                    relation_id=f"vibecad.relation.compiler_native.{relation_sha[:32]}",
                    relation_term_ref_id="relation.compiler.targets_native",
                    source_capability_id=capability_id,
                    target_capability_ids=(native.capability_id,),
                )
            )
        receipt = verification.get(capability_id)
        descriptors.append(
            CapabilityDescriptor(
                capability_id=capability_id,
                kind=CapabilityKind.OPERATION,
                native_identifier=native_identifier,
                declaring_module_id=module_id,
                status=(
                    CapabilitySupportStatus.VERIFIED
                    if receipt is not None
                    else CapabilitySupportStatus.EXECUTABLE
                ),
                risk_class=CapabilityRiskClass.MUTATING,
                semantic_term_ref_ids=(semantic_term,),
                facts=_compiler_facts(family, item["contract_value"]),
                execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
                lifecycle_stages=(CapabilityLifecycleStage.EXECUTE,),
                dependency_ids=dependency_ids,
                verification=receipt,
            )
        )
    receipt_sha256 = _receipt_sha256()
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"vibecad.compilers.{receipt_sha256[:32]}",
        backend=backend,
        discovery_receipt_sha256=receipt_sha256,
        discovery_algorithm_id="vcad.compiler.capability.projection",
        discovery_algorithm_version="1.0",
        terms=_terms(),
        descriptors=tuple(descriptors),
        external_refs=tuple(external_by_id.values()),
        relations=tuple(relations),
    )


__all__ = ()
