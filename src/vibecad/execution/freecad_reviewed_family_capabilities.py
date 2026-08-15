"""Project reviewed-family manifests into formal FreeCAD capability specs.

The reviewed-family engine deliberately keeps lowering and native execution
private.  This module is the one metadata-only bridge used by the runtime
capability catalog: a complete, content-bound family manifest becomes a set of
ordinary :class:`FreeCadIntentCapabilitySpec` records.  Adding another family
therefore extends one manifest registry; it does not add a new capability
schema, promotion algorithm, MCP tool, or backend dispatch path.

Importing this module is side-effect free and never imports FreeCAD.
"""

from __future__ import annotations

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityExecutionProfile,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
)
from vibecad.execution.freecad_intent_capabilities import FreeCadIntentCapabilitySpec
from vibecad.intent_bridge.freecad_app_family_adapter import APP_FAMILY_MANIFEST
from vibecad.intent_bridge.freecad_imageplane_adapter import IMAGEPLANE_MANIFEST
from vibecad.intent_bridge.freecad_part_core_adapter import PART_CORE_MANIFEST
from vibecad.intent_bridge.freecad_part_curve_adapter import PART_CURVE_MANIFEST
from vibecad.intent_bridge.freecad_part_datum_adapter import PART_DATUM_MANIFEST
from vibecad.intent_bridge.freecad_part_dressup_adapter import PART_DRESSUP_MANIFEST
from vibecad.intent_bridge.freecad_part_file_import_adapter import PART_FILE_IMPORT_MANIFEST
from vibecad.intent_bridge.freecad_part_offset_projection_adapter import (
    PART_OFFSET_MANIFEST,
)
from vibecad.intent_bridge.freecad_part_profile_surface_adapter import (
    PART_PROFILE_SURFACE_MANIFEST,
)
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    PARTDESIGN_RESIDUAL_MANIFEST,
)
from vibecad.intent_bridge.freecad_sketch_intent_adapter import (
    REVIEWED_SKETCH_FAMILY_MANIFEST,
)
from vibecad.intent_bridge.reviewed_family_engine import FamilyBatchManifest

MAX_REVIEWED_CAPABILITY_FAMILIES = 32

_LIFECYCLE = (
    CapabilityLifecycleStage.EXECUTE,
    CapabilityLifecycleStage.CREATE,
    CapabilityLifecycleStage.EDIT,
    CapabilityLifecycleStage.RECOMPUTE,
    CapabilityLifecycleStage.SAVE,
    CapabilityLifecycleStage.REOPEN,
)

# This tuple is the sole static registry for reviewed-family capability
# projection.  Every entry is already a canonical, content-addressed manifest;
# registry membership means reviewed, not runtime-verified.  VERIFIED still
# requires a separately persisted test receipt bound to the exact runtime.
CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS = tuple(
    sorted(
        (
            APP_FAMILY_MANIFEST,
            IMAGEPLANE_MANIFEST,
            PART_CORE_MANIFEST,
            PART_CURVE_MANIFEST,
            PART_DATUM_MANIFEST,
            PART_DRESSUP_MANIFEST,
            PART_FILE_IMPORT_MANIFEST,
            PART_OFFSET_MANIFEST,
            PART_PROFILE_SURFACE_MANIFEST,
            PARTDESIGN_RESIDUAL_MANIFEST,
            REVIEWED_SKETCH_FAMILY_MANIFEST,
        ),
        key=lambda item: item.family_id,
    )
)


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _semantic_operation(identity: tuple[str, str, str, str]) -> str:
    """Encode the complete ontology identity in the existing spec field."""

    namespace, vocabulary_version, term_id, definition_sha256 = identity
    return f"{namespace}/{vocabulary_version}/{term_id}@{definition_sha256}"


def build_reviewed_family_capability_specs(
    manifests: tuple[FamilyBatchManifest, ...],
) -> tuple[FreeCadIntentCapabilitySpec, ...]:
    """Return deterministic formal specs for exact reviewed-family manifests.

    Family and operation identifiers are combined to form a stable public
    capability identifier.  Native TypeIds remain static reviewed metadata;
    they are still only promoted when exact runtime discovery proves them.
    """

    if type(manifests) is not tuple or not manifests:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifests")
    if len(manifests) > MAX_REVIEWED_CAPABILITY_FAMILIES:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "manifests")
    if not all(type(item) is FamilyBatchManifest for item in manifests):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifests")

    ordered = tuple(sorted(manifests, key=lambda item: item.family_id))
    if len({item.family_id for item in ordered}) != len(ordered) or len(
        {item.manifest_sha256 for item in ordered}
    ) != len(ordered):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifests/family")
    backend_profiles = {(item.backend_engine, item.backend_version) for item in ordered}
    if len(backend_profiles) != 1 or next(iter(backend_profiles))[0] != "FreeCAD":
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "manifests/backend")

    specs: list[FreeCadIntentCapabilitySpec] = []
    semantic_identities: set[tuple[str, str, str, str]] = set()
    semantic_operations: set[str] = set()
    native_adapters: dict[str, tuple[str, str, str]] = {}
    for manifest in ordered:
        adapter_identity = (
            manifest.adapter.adapter_id,
            manifest.adapter.adapter_version,
            manifest.adapter.adapter_contract_sha256,
        )
        for operation in manifest.operations:
            semantic_identity = operation.semantic_term.semantic_identity
            semantic_operation = _semantic_operation(semantic_identity)
            if (
                semantic_identity in semantic_identities
                or semantic_operation in semantic_operations
            ):
                _fail(
                    CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                    "manifests/operations/semantic_term",
                )
            semantic_identities.add(semantic_identity)
            semantic_operations.add(semantic_operation)
            prior_adapter = native_adapters.setdefault(
                operation.native_type_id,
                adapter_identity,
            )
            if prior_adapter != adapter_identity:
                _fail(
                    CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
                    "manifests/operations/native_type_id",
                )
            specs.append(
                FreeCadIntentCapabilitySpec(
                    operation_id=f"{manifest.family_id}.{operation.operation_id}",
                    semantic_operation=semantic_operation,
                    native_type_id=operation.native_type_id,
                    adapter_id=manifest.adapter.adapter_id,
                    adapter_version=manifest.adapter.adapter_version,
                    adapter_contract_sha256=manifest.adapter.adapter_contract_sha256,
                    rule_id=manifest.rule_id,
                    rule_contract_sha256=manifest.rule_contract_sha256,
                    risk_class=CapabilityRiskClass.MUTATING,
                    execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
                    lifecycle_stages=_LIFECYCLE,
                )
            )

    if len({item.operation_id for item in specs}) != len(specs):
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "manifests/operations/operation_id")
    return tuple(sorted(specs, key=lambda item: item.operation_id))


def current_freecad_reviewed_family_capability_specs() -> tuple[FreeCadIntentCapabilitySpec, ...]:
    """Project the exact current Reviewed registry without claiming VERIFIED."""

    return build_reviewed_family_capability_specs(CURRENT_FREECAD_REVIEWED_FAMILY_MANIFESTS)


__all__ = ()
