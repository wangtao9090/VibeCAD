"""Plan-bound owned-closure resolver for Reviewed PartDesign MultiTransform."""

from __future__ import annotations

import hashlib
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import SemanticRole
from vibecad.parametric import freecad_partdesign_dressup_transform_rules as dressup_rules
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256,
    MultiTransformParameters,
    MultiTransformStep,
    MultiTransformStepKind,
    PartDesignDressupTransformBackendPlan,
    PartDesignDressupTransformConformanceReceipt,
    PartDesignDressupTransformOperation,
)

_RESOLVER_ID = "freecad.partdesign.multi-transform.owned-closure"
_RESOLVER_VERSION = "1.0.0"
_RESOLVER_CONTRACT_DOMAIN = b"vibecad.partdesign.multi-transform-owned-closure.v1\0"

_STEP_TYPE_IDS: Final = MappingProxyType(
    {
        kind: dressup_rules._NATIVE_STEP_SPECS[kind].type_id  # noqa: SLF001
        for kind in MultiTransformStepKind
    }
)
_PRIMARY_TYPE_ID: Final = dressup_rules._NATIVE_SPECS[  # noqa: SLF001
    PartDesignDressupTransformOperation.MULTI_TRANSFORM
].type_id
_RESOLVER_CONTRACT_SHA256: Final = hashlib.sha256(
    b"\0".join(
        (
            _RESOLVER_CONTRACT_DOMAIN,
            PARTDESIGN_DRESSUP_TRANSFORM_RULE_CONTRACT_SHA256.encode("ascii"),
            _PRIMARY_TYPE_ID.encode("ascii"),
            SemanticRole.FEATURE.value.encode("ascii"),
            b"2..8;ordered;children=support",
            *(
                f"{kind.value}:{_STEP_TYPE_IDS[kind]}".encode("ascii")
                for kind in MultiTransformStepKind
            ),
        )
    )
).hexdigest()


def _fail() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _native_receipt(value: object) -> PartDesignDressupTransformConformanceReceipt:
    if type(value) is PartDesignDressupTransformConformanceReceipt:
        return value
    native = getattr(value, "native_receipt", None)
    if type(native) is not PartDesignDressupTransformConformanceReceipt:
        _fail()
    return native


def resolve_partdesign_multitransform_dynamic_ownership(
    plan: object,
    execution: object,
) -> object:
    """Derive one exact ordered closure from reviewed step kinds only."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    if (
        type(plan) is not PartDesignDressupTransformBackendPlan
        or plan.operation is not PartDesignDressupTransformOperation.MULTI_TRANSFORM
        or type(plan.parameters) is not MultiTransformParameters
        or type(execution) is not _ReviewedFamilyNativeExecution
    ):
        _fail()
    steps = plan.parameters.steps
    if (
        type(steps) is not tuple
        or not 2 <= len(steps) <= 8
        or any(type(step) is not MultiTransformStep for step in steps)
        or any(type(step.kind) is not MultiTransformStepKind for step in steps)
    ):
        _fail()
    try:
        child_type_ids = tuple(_STEP_TYPE_IDS[step.kind] for step in steps)
    except (KeyError, TypeError):
        _fail()
    owned = execution.owned_objects
    receipt = _native_receipt(execution.receipt)
    expected_type_ids = (_PRIMARY_TYPE_ID, *child_type_ids)
    if (
        execution.object is not owned[0]
        or len(owned) != len(expected_type_ids)
        or receipt.plan_sha256 != plan.plan_sha256
        or receipt.operation is not PartDesignDressupTransformOperation.MULTI_TRANSFORM
        or receipt.object_names != tuple(getattr(item, "Name", None) for item in owned)
        or any(
            getattr(item, "TypeId", None) != expected
            for item, expected in zip(owned, expected_type_ids, strict=True)
        )
    ):
        _fail()
    return _ReviewedProductResultContract(
        operation_id=PartDesignDressupTransformOperation.MULTI_TRANSFORM.value,
        result_kind=_ReviewedProductResultKind.SOLID,
        owned_type_ids=expected_type_ids,
        semantic_roles=(
            SemanticRole.FEATURE,
            *(SemanticRole.SUPPORT for _step in steps),
        ),
    )


def build_partdesign_multitransform_dynamic_ownership_resolver() -> object:
    """Build the sealed descriptor after the shared seam finishes defining its types."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedDynamicOwnershipResolverDescriptor,
    )

    return _ReviewedDynamicOwnershipResolverDescriptor(
        resolver_id=_RESOLVER_ID,
        resolver_version=_RESOLVER_VERSION,
        resolver_contract_sha256=_RESOLVER_CONTRACT_SHA256,
        operation_ids=(PartDesignDressupTransformOperation.MULTI_TRANSFORM.value,),
        resolve_ownership=resolve_partdesign_multitransform_dynamic_ownership,
    )


__all__ = ()
