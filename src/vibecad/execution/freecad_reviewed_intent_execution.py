"""Trusted product bridge for executing Reviewed FreeCAD intents.

This module is the narrow seam between an authority-free Reviewed lowering
pipeline and already-reviewed native rules.  Model input selects only the
exact semantic operation published by the current formal catalog.  Each route
separately binds that public identity to the complete reviewed-manifest
identity.  The native ``TypeId``, adapter, proof policy, plan decoder, and
callable are selected exclusively by a static family descriptor owned by this
module.

Adding a family means registering one trusted descriptor and an explicit
operation allowlist below.  It never adds model-provided callables or MCP
tools.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import InitVar, dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vibecad import __version__
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityExecutionProfile,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    _build_fingerprint,
    _freecad_version,
    _platform_id,
)
from vibecad.execution.freecad_part_curve_reviewed_execution import (
    PART_CURVE_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_part_datum_reviewed_execution import (
    PART_DATUM_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_part_offset_projection_reviewed_execution import (
    PART_OFFSET_RESULT_INVARIANTS,
    PART_OFFSET_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_part_profile_surface_reviewed_execution import (
    PART_PROFILE_SURFACE_RESULT_INVARIANTS,
    PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_partdesign_boolean_reviewed_execution import (
    PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_partdesign_dressup_transform_reviewed_execution import (
    PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_partdesign_pattern_reviewed_execution import (
    PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_partdesign_primitive_reviewed_execution import (
    PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS,
    PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_partdesign_promotion_reviewed_execution import (
    PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_reviewed_part_csg_execution import (
    PART_CSG_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_reviewed_release_attestation import (
    decode_freecad_reviewed_release_attestation,
    validate_freecad_reviewed_release_attestation,
)
from vibecad.execution.freecad_reviewed_release_attestation_resource import (
    FreeCadPackagedReviewedReleaseAttestation,
    load_current_packaged_freecad_reviewed_release_attestation,
)
from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BackendLoweringResult,
    BridgeBudget,
    BridgeDisposition,
    BridgeTermRef,
    DocumentRef,
    ProducerBinding,
    ProducerDescriptor,
    ProofAssertion,
    ProofBundle,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_part_core_adapter import (
    PART_CORE_MANIFEST,
    PART_CORE_STRUCTURE_TERM,
    build_part_core_adapter,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PFG_SELECTOR_FEATURE_NODE,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.intent_bridge.trusted_proof_policy import (
    RuleEndpointSignature,
    TrustedRuleEvaluation,
    TrustedRuleEvaluatorDescriptor,
    TrustedRulePolicy,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_part_core_rules import (
    PartCoreBackendPlan,
    PartCoreExecutionBindings,
    PartCoreOperation,
    apply_part_core_plan,
)
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1

_ROUTE_CONTRACT_DOMAIN = b"vibecad-reviewed-product-route-v1\0"
_PROOF_TERM_DOMAIN = b"vibecad-reviewed-product-proof-term-v1\0"
_PROOF_EVALUATOR_DOMAIN = b"vibecad-reviewed-product-proof-evaluator-v1\0"


class ReviewedIntentExecutionErrorCode(StrEnum):
    """Fixed bridge failures; rejected input is never reflected."""

    INVALID_INPUT = "invalid_input"
    UNKNOWN_ROUTE = "unknown_route"
    NOT_VERIFIED = "not_verified"
    INTEGRITY_FAILURE = "integrity_failure"
    LOWERING_FAILED = "lowering_failed"
    EXECUTION_FAILED = "execution_failed"


class ReviewedIntentExecutionError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: ReviewedIntentExecutionErrorCode) -> None:
        if type(code) is not ReviewedIntentExecutionErrorCode:
            raise TypeError("code must be a ReviewedIntentExecutionErrorCode")
        self.code = code
        super().__init__(f"reviewed intent execution failed ({code.value})")


def _fail(code: ReviewedIntentExecutionErrorCode) -> None:
    raise ReviewedIntentExecutionError(code)


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReviewedFamilyNativeExecution:
    """Family callback result; shared code validates product-side invariants."""

    object: object = field(repr=False, compare=False)
    receipt: object
    owned_objects: tuple[object, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        owned = self.owned_objects
        if type(owned) is not tuple:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if not owned:
            owned = (self.object,)
            object.__setattr__(self, "owned_objects", owned)
        if (
            self.object is None
            or self.receipt is None
            or owned[0] is not self.object
            or any(item is None for item in owned)
            or len({id(item) for item in owned}) != len(owned)
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReviewedFamilyExecutionContext:
    """Executor-owned state supplied to one static family callback."""

    session: object = field(repr=False, compare=False)
    document: object = field(repr=False, compare=False)
    source_results: tuple[object, ...] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.session is None
            or self.document is None
            or type(self.source_results) is not tuple
            or len(self.source_results) > 8
            or any(item is None for item in self.source_results)
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


class _ReviewedProductResultKind(StrEnum):
    """Closed product-side observation contract selected by a static route."""

    SOLID = "solid"
    VALID_SHAPE = "valid_shape"
    REFERENCE = "reference"


class _ReviewedFormalSemanticBinding(StrEnum):
    """Static public-formal to full-manifest semantic binding mode."""

    FULL_IDENTITY = "full_identity"
    LEGACY_TERM_ID = "legacy_term_id"


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReviewedProductResultContract:
    """Exact primary/closure contract owned by one reviewed operation."""

    operation_id: str
    result_kind: _ReviewedProductResultKind
    owned_type_ids: tuple[str, ...]
    semantic_roles: tuple[SemanticRole, ...]
    source_count: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.operation_id) is not str
            or not self.operation_id
            or type(self.result_kind) is not _ReviewedProductResultKind
            or type(self.owned_type_ids) is not tuple
            or not self.owned_type_ids
            or any(type(item) is not str or not item for item in self.owned_type_ids)
            or type(self.semantic_roles) is not tuple
            or len(self.semantic_roles) != len(self.owned_type_ids)
            or any(type(item) is not SemanticRole for item in self.semantic_roles)
            or (
                self.source_count is not None
                and (type(self.source_count) is not int or not 0 <= self.source_count <= 8)
            )
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)

    def validate(
        self,
        operation: ReviewedOperationSpec,
        primary: object,
        owned: tuple[object, ...],
    ) -> None:
        if (
            type(operation) is not ReviewedOperationSpec
            or operation.operation_id != self.operation_id
            or operation.native_type_id != self.owned_type_ids[0]
            or type(owned) is not tuple
            or len(owned) != len(self.owned_type_ids)
            or not owned
            or owned[0] is not primary
            or len({id(item) for item in owned}) != len(owned)
            or any(
                getattr(item, "TypeId", None) != expected
                for item, expected in zip(owned, self.owned_type_ids, strict=True)
            )
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if self.result_kind is _ReviewedProductResultKind.REFERENCE:
            try:
                is_valid = getattr(primary, "isValid", None)
                if (
                    not callable(is_valid)
                    or is_valid() is not True
                    or tuple(primary.State) != ("Up-to-date",)
                ):
                    raise ValueError
            except (Exception, SystemExit):
                _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
            return
        try:
            shape = primary.Shape
            is_null = getattr(shape, "isNull", None)
            if (
                shape is None
                or (callable(is_null) and is_null() is not False)
                or shape.isValid() is not True
            ):
                raise ValueError
            if self.result_kind is _ReviewedProductResultKind.SOLID and (
                len(shape.Solids) != 1 or float(shape.Volume) <= 0.0
            ):
                raise ValueError
        except (Exception, SystemExit, TypeError, ValueError, OverflowError):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReviewedDynamicProductResolution:
    """One content-bound result contract emitted by a sealed resolver."""

    resolver_id: str
    resolver_version: str
    resolver_contract_sha256: str
    plan_sha256: str
    plan_content_sha256: str
    contract: _ReviewedProductResultContract

    def __post_init__(self) -> None:
        identifiers = (self.resolver_id, self.resolver_version)
        digests = (
            self.resolver_contract_sha256,
            self.plan_sha256,
            self.plan_content_sha256,
        )
        if (
            any(
                type(item) is not str
                or not 1 <= len(item) <= 128
                or not item[0].isalnum()
                or any(
                    not (character.isascii() and (character.isalnum() or character in "._:-"))
                    for character in item
                )
                for item in identifiers
            )
            or any(
                type(item) is not str
                or len(item) != 64
                or any(character not in "0123456789abcdef" for character in item)
                for item in digests
            )
            or type(self.contract) is not _ReviewedProductResultContract
            or self.contract.source_count is not None
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


_DynamicOwnershipResolverCallback = Callable[
    [object, _ReviewedFamilyNativeExecution],
    _ReviewedProductResultContract,
]


@dataclass(frozen=True, slots=True, kw_only=True)
class _ReviewedDynamicOwnershipResolverDescriptor:
    """Static authority and identity for one plan-bound ownership resolver."""

    resolver_id: str
    resolver_version: str
    resolver_contract_sha256: str
    operation_ids: tuple[str, ...]
    resolve_ownership: _DynamicOwnershipResolverCallback = field(
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        identifiers = (self.resolver_id, self.resolver_version)
        if (
            any(
                type(item) is not str
                or not 1 <= len(item) <= 128
                or not item[0].isalnum()
                or any(
                    not (character.isascii() and (character.isalnum() or character in "._:-"))
                    for character in item
                )
                for item in identifiers
            )
            or type(self.resolver_contract_sha256) is not str
            or len(self.resolver_contract_sha256) != 64
            or any(
                character not in "0123456789abcdef" for character in self.resolver_contract_sha256
            )
            or type(self.operation_ids) is not tuple
            or not self.operation_ids
            or len(set(self.operation_ids)) != len(self.operation_ids)
            or any(
                type(item) is not str or not item or len(item) > 128 for item in self.operation_ids
            )
            or not callable(self.resolve_ownership)
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)

    def handles(self, operation: ReviewedOperationSpec) -> bool:
        if type(operation) is not ReviewedOperationSpec:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        return operation.operation_id in self.operation_ids

    def resolve(
        self,
        plan: object,
        plan_document: DocumentRef,
        operation: ReviewedOperationSpec,
        execution: _ReviewedFamilyNativeExecution,
    ) -> _ReviewedDynamicProductResolution:
        if (
            type(plan_document) is not DocumentRef
            or type(operation) is not ReviewedOperationSpec
            or type(execution) is not _ReviewedFamilyNativeExecution
            or not self.handles(operation)
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        try:
            canonical = plan.canonical_bytes
            semantic_plan_sha256 = plan.plan_sha256
            plan_operation = plan.operation
            receipt = execution.receipt
            receipt_operation = receipt.operation
        except (Exception, SystemExit):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if (
            type(canonical) is not bytes
            or len(canonical) != plan_document.size_bytes
            or not hmac.compare_digest(
                hashlib.sha256(canonical).hexdigest(),
                plan_document.content_sha256,
            )
            or type(semantic_plan_sha256) is not str
            or not hmac.compare_digest(
                semantic_plan_sha256,
                plan_document.document_digest,
            )
            or getattr(plan_operation, "value", None) != operation.operation_id
            or getattr(receipt_operation, "value", None) != operation.operation_id
            or getattr(receipt, "plan_sha256", None) != plan_document.document_digest
            or getattr(receipt, "plan_content_sha256", None) != plan_document.content_sha256
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        try:
            contract = self.resolve_ownership(plan, execution)
        except ReviewedIntentExecutionError:
            raise
        except (Exception, SystemExit):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if (
            type(contract) is not _ReviewedProductResultContract
            or contract.operation_id != operation.operation_id
            or contract.source_count is not None
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        contract.validate(operation, execution.object, execution.owned_objects)
        return _ReviewedDynamicProductResolution(
            resolver_id=self.resolver_id,
            resolver_version=self.resolver_version,
            resolver_contract_sha256=self.resolver_contract_sha256,
            plan_sha256=plan_document.document_digest,
            plan_content_sha256=plan_document.content_sha256,
            contract=contract,
        )


_AdapterFactory = Callable[[PlanSink], ExactReviewedFamilyAdapter]
_PlanValidator = Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
_NativeExecutor = Callable[
    [
        object,
        object,
        bytes,
        DocumentRef,
        ReviewedOperationSpec,
        _ReviewedFamilyExecutionContext,
    ],
    _ReviewedFamilyNativeExecution,
]


@dataclass(frozen=True, slots=True, kw_only=True, eq=False)
class _ReviewedIntentFamilyDescriptor:
    """Private static callbacks and contracts for one Reviewed family."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    adapter_factory: _AdapterFactory = field(repr=False, compare=False)
    validate_plan: _PlanValidator = field(repr=False, compare=False)
    execute_plan: _NativeExecutor = field(repr=False, compare=False)
    product_results: tuple[_ReviewedProductResultContract, ...]
    minimum_sources: int = 0
    maximum_sources: int = 0
    formal_semantic_binding: _ReviewedFormalSemanticBinding = (
        _ReviewedFormalSemanticBinding.FULL_IDENTITY
    )
    dynamic_ownership_resolver: _ReviewedDynamicOwnershipResolverDescriptor | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if (
            type(self.manifest) is not FamilyBatchManifest
            or type(self.subject_type_term) is not BridgeTermRef
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
            or type(self.product_results) is not tuple
            or any(
                type(item) is not _ReviewedProductResultContract for item in self.product_results
            )
            or any(
                not any(
                    operation.operation_id == item.operation_id
                    for operation in self.manifest.operations
                )
                for item in self.product_results
            )
            or not any(term == self.subject_type_term for term in self.manifest.request_terms)
            or type(self.minimum_sources) is not int
            or type(self.maximum_sources) is not int
            or not 0 <= self.minimum_sources <= self.maximum_sources <= 8
            or type(self.formal_semantic_binding) is not _ReviewedFormalSemanticBinding
            or (
                self.dynamic_ownership_resolver is not None
                and type(self.dynamic_ownership_resolver)
                is not _ReviewedDynamicOwnershipResolverDescriptor
            )
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        keys = tuple((item.operation_id, item.source_count) for item in self.product_results)
        if len(set(keys)) != len(keys) or any(
            item.source_count is not None
            and not self.minimum_sources <= item.source_count <= self.maximum_sources
            for item in self.product_results
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        for operation_id in {item.operation_id for item in self.product_results}:
            variants = tuple(
                item for item in self.product_results if item.operation_id == operation_id
            )
            if any(item.source_count is None for item in variants) and (
                len(variants) != 1 or variants[0].source_count is not None
            ):
                _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        resolver = self.dynamic_ownership_resolver
        if not self.product_results and resolver is None:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if resolver is not None and (
            any(
                not any(
                    operation.operation_id == operation_id for operation in self.manifest.operations
                )
                for operation_id in resolver.operation_ids
            )
            or any(item.operation_id in resolver.operation_ids for item in self.product_results)
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)

    def dynamic_resolver_for(
        self,
        operation: ReviewedOperationSpec,
    ) -> _ReviewedDynamicOwnershipResolverDescriptor | None:
        if type(operation) is not ReviewedOperationSpec:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        resolver = self.dynamic_ownership_resolver
        if resolver is None or not resolver.handles(operation):
            return None
        return resolver

    def build_adapter(self, sink: PlanSink) -> ExactReviewedFamilyAdapter:
        try:
            adapter = self.adapter_factory(sink)
        except ReviewedIntentExecutionError:
            raise
        except (Exception, SystemExit):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if (
            not isinstance(adapter, ExactReviewedFamilyAdapter)
            or adapter.manifest != self.manifest
            or adapter.descriptor != self.manifest.adapter
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        return adapter

    def accept_plan(
        self,
        plan: object,
        receipt: ReviewedPlanReceipt,
        operation: ReviewedOperationSpec,
    ) -> None:
        try:
            accepted = self.validate_plan(plan, receipt, operation)
        except ReviewedIntentExecutionError:
            raise
        except (Exception, SystemExit):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if accepted is not None:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)

    def apply_plan(
        self,
        document: object,
        plan: object,
        payload: bytes,
        plan_document: DocumentRef,
        operation: ReviewedOperationSpec,
        context: _ReviewedFamilyExecutionContext,
    ) -> _ReviewedFamilyNativeExecution:
        if (
            type(context) is not _ReviewedFamilyExecutionContext
            or context.document is not document
            or not self.minimum_sources <= len(context.source_results) <= self.maximum_sources
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if self.dynamic_resolver_for(operation) is None:
            self.product_result(operation, context=context)
        try:
            result = self.execute_plan(
                document,
                plan,
                payload,
                plan_document,
                operation,
                context,
            )
        except ReviewedIntentExecutionError:
            raise
        except (Exception, SystemExit):
            _fail(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
        if type(result) is not _ReviewedFamilyNativeExecution:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        return result

    def product_result(
        self,
        operation: ReviewedOperationSpec,
        *,
        context: _ReviewedFamilyExecutionContext | None = None,
    ) -> _ReviewedProductResultContract:
        if type(operation) is not ReviewedOperationSpec or (
            context is not None and type(context) is not _ReviewedFamilyExecutionContext
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if self.dynamic_resolver_for(operation) is not None:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        matching = tuple(
            item for item in self.product_results if item.operation_id == operation.operation_id
        )
        if (
            context is not None
            and not self.minimum_sources <= len(context.source_results) <= self.maximum_sources
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if len(matching) == 1 and matching[0].source_count is None:
            return matching[0]
        if context is None:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        selected = tuple(
            item for item in matching if item.source_count == len(context.source_results)
        )
        if len(selected) != 1:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        return selected[0]

    def accept_product_result(
        self,
        operation: ReviewedOperationSpec,
        primary: object,
        owned: tuple[object, ...],
        *,
        context: _ReviewedFamilyExecutionContext | None = None,
    ) -> _ReviewedProductResultContract:
        contract = self.product_result(operation, context=context)
        contract.validate(operation, primary, owned)
        return contract

    def resolve_dynamic_product_result(
        self,
        plan: object,
        plan_document: DocumentRef,
        operation: ReviewedOperationSpec,
        execution: _ReviewedFamilyNativeExecution,
    ) -> _ReviewedDynamicProductResolution | None:
        resolver = self.dynamic_resolver_for(operation)
        if resolver is None:
            return None
        return resolver.resolve(plan, plan_document, operation, execution)


def _validate_part_core_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(plan) is not PartCoreBackendPlan
        or type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or plan.operation.value != operation.operation_id
        or plan.sources
        or plan.plan_sha256 != receipt.plan_document.document_digest
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _execute_part_core_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: _ReviewedFamilyExecutionContext,
) -> _ReviewedFamilyNativeExecution:
    if (
        type(plan) is not PartCoreBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or plan.operation.value != operation.operation_id
        or plan.sources
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.source_results
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    receipt = apply_part_core_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=PartCoreExecutionBindings(
            document=document,
            body_id=plan.body_id,
            sources=(),
        ),
    )
    try:
        result = document.getObject(receipt.object_name)
    except (Exception, SystemExit):
        _fail(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
    return _ReviewedFamilyNativeExecution(object=result, receipt=receipt)


_REVIEWED_PART_PRIMITIVE_OPERATIONS: Final = (
    PartCoreOperation.BOX,
    PartCoreOperation.CONE,
    PartCoreOperation.CYLINDER,
    PartCoreOperation.ELLIPSOID,
    PartCoreOperation.PRISM,
    PartCoreOperation.SPHERE,
    PartCoreOperation.TORUS,
    PartCoreOperation.WEDGE,
)


def _singleton_product_results(
    manifest: FamilyBatchManifest,
    operation_ids: tuple[str, ...],
    *,
    result_kind: _ReviewedProductResultKind,
    semantic_role: SemanticRole,
) -> tuple[_ReviewedProductResultContract, ...]:
    return tuple(
        _ReviewedProductResultContract(
            operation_id=operation_id,
            result_kind=result_kind,
            owned_type_ids=(
                next(
                    item.native_type_id
                    for item in manifest.operations
                    if item.operation_id == operation_id
                ),
            ),
            semantic_roles=(semantic_role,),
        )
        for operation_id in operation_ids
    )


_PART_CORE_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_CORE_MANIFEST,
    subject_type_term=_bridge_term(PART_CORE_STRUCTURE_TERM),
    adapter_factory=build_part_core_adapter,
    validate_plan=_validate_part_core_plan,
    execute_plan=_execute_part_core_plan,
    product_results=_singleton_product_results(
        PART_CORE_MANIFEST,
        tuple(item.value for item in _REVIEWED_PART_PRIMITIVE_OPERATIONS),
        result_kind=_ReviewedProductResultKind.SOLID,
        semantic_role=SemanticRole.PRIMITIVE,
    ),
)

_PART_CURVE_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_CURVE_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PART_CURVE_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PART_CURVE_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PART_CURVE_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PART_CURVE_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=_singleton_product_results(
        PART_CURVE_REVIEWED_FAMILY_SPEC.manifest,
        PART_CURVE_REVIEWED_FAMILY_SPEC.operation_ids,
        result_kind=_ReviewedProductResultKind.VALID_SHAPE,
        semantic_role=SemanticRole.PRIMITIVE,
    ),
)

_PART_CSG_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_CSG_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PART_CSG_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PART_CSG_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PART_CSG_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PART_CSG_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=_singleton_product_results(
        PART_CSG_REVIEWED_FAMILY_SPEC.manifest,
        PART_CSG_REVIEWED_FAMILY_SPEC.operation_ids,
        result_kind=_ReviewedProductResultKind.SOLID,
        semantic_role=SemanticRole.FEATURE,
    ),
    minimum_sources=2,
    maximum_sources=2,
)

_PART_DATUM_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_DATUM_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PART_DATUM_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PART_DATUM_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PART_DATUM_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PART_DATUM_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=tuple(
        _ReviewedProductResultContract(
            operation_id=operation_id,
            result_kind=_ReviewedProductResultKind.REFERENCE,
            owned_type_ids=(
                next(
                    item.native_type_id
                    for item in PART_DATUM_REVIEWED_FAMILY_SPEC.manifest.operations
                    if item.operation_id == operation_id
                ),
                *(
                    (
                        "App::Line",
                        "App::Line",
                        "App::Line",
                        "App::Plane",
                        "App::Plane",
                        "App::Plane",
                        "App::Point",
                    )
                    if operation_id == "local_coordinate_system"
                    else ()
                ),
            ),
            semantic_roles=(
                (SemanticRole.SUPPORT,) * 8
                if operation_id == "local_coordinate_system"
                else (SemanticRole.SUPPORT,)
            ),
        )
        for operation_id in PART_DATUM_REVIEWED_FAMILY_SPEC.operation_ids
    ),
)

_PART_PROFILE_SURFACE_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=tuple(
        _ReviewedProductResultContract(
            operation_id=operation_id,
            result_kind=(
                _ReviewedProductResultKind.SOLID
                if next(
                    invariant
                    for operation, invariant in PART_PROFILE_SURFACE_RESULT_INVARIANTS.items()
                    if operation.value == operation_id
                ).solid_count
                == 1
                else _ReviewedProductResultKind.VALID_SHAPE
            ),
            owned_type_ids=(
                next(
                    item.native_type_id
                    for item in PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.manifest.operations
                    if item.operation_id == operation_id
                ),
            ),
            semantic_roles=(SemanticRole.FEATURE,),
        )
        for operation_id in PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.operation_ids
    ),
    minimum_sources=1,
    maximum_sources=8,
)

_PART_OFFSET_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_OFFSET_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PART_OFFSET_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PART_OFFSET_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PART_OFFSET_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PART_OFFSET_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=tuple(
        _ReviewedProductResultContract(
            operation_id=operation_id,
            result_kind=(
                _ReviewedProductResultKind.SOLID
                if next(
                    invariant
                    for operation, invariant in PART_OFFSET_RESULT_INVARIANTS.items()
                    if operation.value == operation_id
                ).solid_count
                == 1
                else _ReviewedProductResultKind.VALID_SHAPE
            ),
            owned_type_ids=(
                next(
                    item.native_type_id
                    for item in PART_OFFSET_REVIEWED_FAMILY_SPEC.manifest.operations
                    if item.operation_id == operation_id
                ),
            ),
            semantic_roles=(SemanticRole.FEATURE,),
        )
        for operation_id in PART_OFFSET_REVIEWED_FAMILY_SPEC.operation_ids
    ),
    minimum_sources=1,
    maximum_sources=2,
)

_PARTDESIGN_PROMOTION_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=_singleton_product_results(
        PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.manifest,
        PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.operation_ids,
        result_kind=_ReviewedProductResultKind.SOLID,
        semantic_role=SemanticRole.FEATURE,
    ),
    minimum_sources=1,
    maximum_sources=8,
    formal_semantic_binding=_ReviewedFormalSemanticBinding.LEGACY_TERM_ID,
)

_PARTDESIGN_PRIMITIVE_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=tuple(
        _ReviewedProductResultContract(
            operation_id=contract.operation.value,
            result_kind=_ReviewedProductResultKind.SOLID,
            owned_type_ids=variant.owned_type_ids,
            semantic_roles=variant.semantic_roles,
            source_count=variant.source_count,
        )
        for contract in PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS.values()
        for variant in contract.closure_variants
    ),
    minimum_sources=0,
    maximum_sources=1,
    formal_semantic_binding=_ReviewedFormalSemanticBinding.LEGACY_TERM_ID,
)

_PARTDESIGN_PATTERN_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=_singleton_product_results(
        PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.manifest,
        PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.operation_ids,
        result_kind=_ReviewedProductResultKind.SOLID,
        semantic_role=SemanticRole.FEATURE,
    ),
    minimum_sources=PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.minimum_sources,
    maximum_sources=PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.maximum_sources,
    formal_semantic_binding=_ReviewedFormalSemanticBinding.LEGACY_TERM_ID,
)

_PARTDESIGN_BOOLEAN_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=_singleton_product_results(
        PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.manifest,
        PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.operation_ids,
        result_kind=_ReviewedProductResultKind.SOLID,
        semantic_role=SemanticRole.FEATURE,
    ),
    minimum_sources=PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.minimum_sources,
    maximum_sources=PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.maximum_sources,
    formal_semantic_binding=_ReviewedFormalSemanticBinding.LEGACY_TERM_ID,
)

from vibecad.execution.freecad_partdesign_multitransform_dynamic_ownership import (  # noqa: E402
    build_partdesign_multitransform_dynamic_ownership_resolver,
)

PARTDESIGN_MULTITRANSFORM_DYNAMIC_OWNERSHIP_RESOLVER: Final = (
    build_partdesign_multitransform_dynamic_ownership_resolver()
)

_PARTDESIGN_DRESSUP_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.execute_plan,
    product_results=_singleton_product_results(
        PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.manifest,
        tuple(
            operation_id
            for operation_id in PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.operation_ids
            if operation_id
            not in PARTDESIGN_MULTITRANSFORM_DYNAMIC_OWNERSHIP_RESOLVER.operation_ids
        ),
        result_kind=_ReviewedProductResultKind.SOLID,
        semantic_role=SemanticRole.FEATURE,
    ),
    minimum_sources=PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.minimum_sources,
    maximum_sources=PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.maximum_sources,
    formal_semantic_binding=_ReviewedFormalSemanticBinding.LEGACY_TERM_ID,
    dynamic_ownership_resolver=PARTDESIGN_MULTITRANSFORM_DYNAMIC_OWNERSHIP_RESOLVER,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedIntentRoute:
    """One exact formal route, independently bound to reviewed contracts."""

    operation_id: str
    semantic_operation: str
    family: _ReviewedIntentFamilyDescriptor = field(repr=False)
    manifest: FamilyBatchManifest
    operation: ReviewedOperationSpec
    subject_type_term: BridgeTermRef
    manifest_semantic_operation: str = field(init=False)
    route_contract_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.operation_id) is not str
            or type(self.semantic_operation) is not str
            or type(self.family) is not _ReviewedIntentFamilyDescriptor
            or type(self.manifest) is not FamilyBatchManifest
            or type(self.operation) is not ReviewedOperationSpec
            or type(self.subject_type_term) is not BridgeTermRef
            or self.manifest != self.family.manifest
            or self.subject_type_term != self.family.subject_type_term
            or self.operation not in self.manifest.operations
            or self.operation_id != f"{self.manifest.family_id}.{self.operation.operation_id}"
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        manifest_semantic_operation = _semantic_operation(self.operation)
        formal = tuple(
            item
            for item in current_freecad_intent_capability_specs()
            if item.operation_id == self.operation_id
        )
        if self.family.formal_semantic_binding is _ReviewedFormalSemanticBinding.FULL_IDENTITY:
            expected_formal_semantic = manifest_semantic_operation
        elif self.family.formal_semantic_binding is _ReviewedFormalSemanticBinding.LEGACY_TERM_ID:
            expected_formal_semantic = self.operation.semantic_term.term_id
        else:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        resolver = self.family.dynamic_resolver_for(self.operation)
        static_results = tuple(
            item
            for item in self.family.product_results
            if item.operation_id == self.operation.operation_id
        )
        if (
            len(formal) != 1
            or formal[0].semantic_operation != self.semantic_operation
            or self.semantic_operation != expected_formal_semantic
            or formal[0].native_type_id != self.operation.native_type_id
            or formal[0].adapter_id != self.manifest.adapter.adapter_id
            or formal[0].adapter_version != self.manifest.adapter.adapter_version
            or formal[0].adapter_contract_sha256 != self.manifest.adapter.adapter_contract_sha256
            or formal[0].rule_id != self.manifest.rule_id
            or formal[0].rule_contract_sha256 != self.manifest.rule_contract_sha256
            or (resolver is None and not static_results)
            or (resolver is not None and static_results)
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        object.__setattr__(
            self,
            "manifest_semantic_operation",
            manifest_semantic_operation,
        )
        body = "\0".join(
            (
                self.operation_id,
                self.semantic_operation,
                self.manifest_semantic_operation,
                self.family.formal_semantic_binding.value,
                self.manifest.manifest_sha256,
                self.manifest.adapter.adapter_id,
                self.manifest.adapter.adapter_version,
                self.manifest.adapter.adapter_contract_sha256,
                self.manifest.rule_id,
                self.manifest.rule_contract_sha256,
                self.operation.specification_sha256,
                *self.subject_type_term.semantic_identity,
                *(
                    (
                        "dynamic-ownership-resolver-v1",
                        resolver.resolver_id,
                        resolver.resolver_version,
                        resolver.resolver_contract_sha256,
                    )
                    if resolver is not None
                    else ()
                ),
            )
        ).encode("utf-8")
        object.__setattr__(
            self,
            "route_contract_sha256",
            hashlib.sha256(_ROUTE_CONTRACT_DOMAIN + body).hexdigest(),
        )


def _routes_for_family(
    family: _ReviewedIntentFamilyDescriptor,
    operation_ids: tuple[str, ...],
) -> tuple[ReviewedIntentRoute, ...]:
    if (
        type(family) is not _ReviewedIntentFamilyDescriptor
        or type(operation_ids) is not tuple
        or not operation_ids
        or any(type(item) is not str for item in operation_ids)
        or len(set(operation_ids)) != len(operation_ids)
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    operations = tuple(
        next(
            (item for item in family.manifest.operations if item.operation_id == operation_id),
            None,
        )
        for operation_id in operation_ids
    )
    if any(type(item) is not ReviewedOperationSpec for item in operations):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    formal_specs = current_freecad_intent_capability_specs()
    formal_by_operation = {
        operation.operation_id: tuple(
            item
            for item in formal_specs
            if item.operation_id == f"{family.manifest.family_id}.{operation.operation_id}"
        )
        for operation in operations
    }
    if any(len(items) != 1 for items in formal_by_operation.values()):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    return tuple(
        ReviewedIntentRoute(
            operation_id=f"{family.manifest.family_id}.{operation.operation_id}",
            semantic_operation=formal_by_operation[operation.operation_id][0].semantic_operation,
            family=family,
            manifest=family.manifest,
            operation=operation,
            subject_type_term=family.subject_type_term,
        )
        for operation in operations
    )


REVIEWED_PART_PRIMITIVE_ROUTES: Final = _routes_for_family(
    _PART_CORE_FAMILY,
    tuple(operation.value for operation in _REVIEWED_PART_PRIMITIVE_OPERATIONS),
)
REVIEWED_PART_BOX_ROUTE: Final = REVIEWED_PART_PRIMITIVE_ROUTES[0]
REVIEWED_PART_CURVE_ROUTES: Final = _routes_for_family(
    _PART_CURVE_FAMILY,
    PART_CURVE_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PART_CSG_ROUTES: Final = _routes_for_family(
    _PART_CSG_FAMILY,
    PART_CSG_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PART_DATUM_ROUTES: Final = _routes_for_family(
    _PART_DATUM_FAMILY,
    PART_DATUM_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PART_PROFILE_SURFACE_ROUTES: Final = _routes_for_family(
    _PART_PROFILE_SURFACE_FAMILY,
    PART_PROFILE_SURFACE_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PART_OFFSET_ROUTES: Final = _routes_for_family(
    _PART_OFFSET_FAMILY,
    PART_OFFSET_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PARTDESIGN_PROMOTION_ROUTES: Final = _routes_for_family(
    _PARTDESIGN_PROMOTION_FAMILY,
    PARTDESIGN_PROMOTION_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES: Final = _routes_for_family(
    _PARTDESIGN_PRIMITIVE_FAMILY,
    PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PARTDESIGN_PATTERN_ROUTES: Final = _routes_for_family(
    _PARTDESIGN_PATTERN_FAMILY,
    PARTDESIGN_PATTERN_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PARTDESIGN_BOOLEAN_ROUTES: Final = _routes_for_family(
    _PARTDESIGN_BOOLEAN_FAMILY,
    PARTDESIGN_BOOLEAN_REVIEWED_FAMILY_SPEC.operation_ids,
)
REVIEWED_PARTDESIGN_DRESSUP_ROUTES: Final = _routes_for_family(
    _PARTDESIGN_DRESSUP_FAMILY,
    PARTDESIGN_DRESSUP_REVIEWED_FAMILY_SPEC.operation_ids,
)
_REVIEWED_FAMILY_ROUTE_SETS: Final = (
    REVIEWED_PART_PRIMITIVE_ROUTES,
    REVIEWED_PART_CURVE_ROUTES,
    REVIEWED_PART_CSG_ROUTES,
    REVIEWED_PART_DATUM_ROUTES,
    REVIEWED_PART_PROFILE_SURFACE_ROUTES,
    REVIEWED_PART_OFFSET_ROUTES,
    REVIEWED_PARTDESIGN_PROMOTION_ROUTES,
    REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES,
    REVIEWED_PARTDESIGN_PATTERN_ROUTES,
    REVIEWED_PARTDESIGN_BOOLEAN_ROUTES,
    REVIEWED_PARTDESIGN_DRESSUP_ROUTES,
)
CURRENT_REVIEWED_INTENT_ROUTES: Final = tuple(
    route for family_routes in _REVIEWED_FAMILY_ROUTE_SETS for route in family_routes
)


def _index_routes(
    routes: tuple[ReviewedIntentRoute, ...],
) -> MappingProxyType:
    if (
        type(routes) is not tuple
        or not routes
        or any(type(route) is not ReviewedIntentRoute for route in routes)
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    indexed = {(item.operation_id, item.semantic_operation): item for item in routes}
    if (
        len(indexed) != len(routes)
        or len({item.operation_id for item in routes}) != len(routes)
        or len({item.semantic_operation for item in routes}) != len(routes)
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    return MappingProxyType(indexed)


_ROUTES_BY_IDENTITY: Final = _index_routes(CURRENT_REVIEWED_INTENT_ROUTES)


def route_reviewed_intent(value: object) -> ReviewedIntentRoute:
    """Resolve only an exact public identity against the static product table."""

    if type(value) is not ReviewedIntentProgramV1:
        _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)
    route = _ROUTES_BY_IDENTITY.get((value.operation_id, value.semantic_operation))
    if type(route) is not ReviewedIntentRoute:
        _fail(ReviewedIntentExecutionErrorCode.UNKNOWN_ROUTE)
    return route


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.reviewed-product-execution",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=hashlib.sha256(
            _PROOF_TERM_DOMAIN + term_id.encode("ascii")
        ).hexdigest(),
    )


_RULE_TERM: Final = _proof_term("rule_reviewed_product_target", "rule.reviewed-product-target")
_PREDICATE_TERM: Final = _proof_term(
    "predicate_reviewed_product_target",
    "predicate.reviewed-product-target",
)
_PREMISE_ROLE_TERM: Final = _proof_term(
    "role_reviewed_product_candidate",
    "proof-role.reviewed-product-candidate",
)
_CONCLUSION_ROLE_TERM: Final = _proof_term(
    "role_reviewed_product_validated",
    "proof-role.reviewed-product-validated",
)


class _ReviewedProductEvaluator:
    __slots__ = ("_descriptor", "_subject")

    def __init__(self, route: ReviewedIntentRoute, subject: SubjectRef) -> None:
        if type(route) is not ReviewedIntentRoute or type(subject) is not SubjectRef:
            _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)

        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=route.subject_type_term,
            )

        contract = hashlib.sha256(
            _PROOF_EVALUATOR_DOMAIN
            + route.route_contract_sha256.encode("ascii")
            + _RULE_TERM.term_definition_sha256.encode("ascii")
            + _PREDICATE_TERM.term_definition_sha256.encode("ascii")
        ).hexdigest()
        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="reviewed_product_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=contract,
            rule_term=_RULE_TERM,
            predicate_term=_PREDICATE_TERM,
            premises=(signature(_PREMISE_ROLE_TERM),),
            conclusions=(signature(_CONCLUSION_ROLE_TERM),),
        )
        self._subject = subject

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        if (
            type(evaluation) is not TrustedRuleEvaluation
            or len(evaluation.documents) != 1
            or len(evaluation.premises) != 1
            or len(evaluation.conclusions) != 1
            or evaluation.premises[0].subject != self._subject
            or evaluation.conclusions[0].subject != self._subject
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


class _ExactReader:
    __slots__ = ("_items",)

    def __init__(self, items: dict[str, tuple[DocumentRef, bytes]]) -> None:
        self._items = MappingProxyType(dict(items))

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        try:
            expected, payload = self._items[document.artifact_id]
        except (AttributeError, KeyError):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if (
            expected != document
            or type(maximum_bytes) is not int
            or maximum_bytes < len(payload)
            or len(payload) != document.size_bytes
            or not hmac.compare_digest(
                hashlib.sha256(payload).hexdigest(),
                document.content_sha256,
            )
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        return payload


class _ExactPlanSink:
    __slots__ = ("_items",)

    def __init__(self) -> None:
        self._items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        if type(document) is not DocumentRef or type(payload) is not bytes:
            _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)
        existing = self._items.get(document.artifact_id)
        if existing is not None and existing != (document, payload):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        staged = dict(self._items)
        staged[document.artifact_id] = (document, payload)
        self._items = staged
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        try:
            expected, payload = self._items[document.artifact_id]
        except (AttributeError, KeyError):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if expected != document or type(maximum_bytes) is not int or len(payload) > maximum_bytes:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        return payload


@dataclass(frozen=True, slots=True, kw_only=True)
class LoweredReviewedIntent:
    route: ReviewedIntentRoute
    result: BackendLoweringResult
    receipt: ReviewedPlanReceipt
    plan: object = field(repr=False)
    payload: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.route) is not ReviewedIntentRoute
            or type(self.result) is not BackendLoweringResult
            or type(self.receipt) is not ReviewedPlanReceipt
            or self.plan is None
            or type(self.payload) is not bytes
            or self.result.disposition is not BridgeDisposition.COMPLETE
            or self.result.plan_document != self.receipt.plan_document
            or self.receipt.operation != self.route.operation
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        self.route.family.accept_plan(self.plan, self.receipt, self.route.operation)


def lower_reviewed_intent(value: object) -> LoweredReviewedIntent:
    """Lower one exact PFG through the existing Reviewed adapter and proof gate."""

    if type(value) is not ReviewedIntentProgramV1:
        _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)
    route = route_reviewed_intent(value)
    graph = value.intent_graph
    if len(graph.graph_results) != 1:
        _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)
    subject = SubjectRef(
        artifact_id=f"artifact_reviewed_intent_{value.intent_content_sha256[:32]}",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id=graph.graph_results[0].node_id,
    )
    evaluator = _ReviewedProductEvaluator(route, subject)
    policy = TrustedRulePolicy(evaluators=(evaluator,))
    intent_payload = graph.canonical_bytes
    intent_document = DocumentRef(
        artifact_id=subject.artifact_id,
        role_term_ref_id=route.manifest.intent_role_term.term_ref_id,
        schema_term_ref_id=route.manifest.intent_schema_term.term_ref_id,
        document_id=graph.graph_id,
        document_digest=graph.graph_sha256,
        content_sha256=hashlib.sha256(intent_payload).hexdigest(),
        size_bytes=len(intent_payload),
        media_type=route.manifest.intent_media_type,
    )
    capability_document, capability_payload = route.manifest.capability_document()
    proof = ProofBundle(
        terms=(
            _RULE_TERM,
            _PREDICATE_TERM,
            _PREMISE_ROLE_TERM,
            _CONCLUSION_ROLE_TERM,
            route.subject_type_term,
            route.manifest.intent_role_term,
            route.manifest.intent_schema_term,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(intent_document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_reviewed_product_target",
                predicate_term_ref_id=_PREDICATE_TERM.term_ref_id,
                rule_term_ref_id=_RULE_TERM.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=_PREMISE_ROLE_TERM.term_ref_id,
                        subject=subject,
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=_CONCLUSION_ROLE_TERM.term_ref_id,
                        subject=subject,
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="reviewed_product_program",
                producer_version="1.0.0",
                producer_contract_sha256=route.route_contract_sha256,
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=value.program_sha256,
        ),
    )
    request = BackendLoweringRequest(
        adapter=route.manifest.adapter,
        terms=(
            *route.manifest.request_terms,
            _RULE_TERM,
            _PREDICATE_TERM,
            _PREMISE_ROLE_TERM,
            _CONCLUSION_ROLE_TERM,
        ),
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=proof,
        budget=BridgeBudget(
            max_input_bytes=len(intent_payload) + len(capability_payload),
            max_output_bytes=route.manifest.max_plan_bytes,
            max_subject_lookups=1,
            max_rule_applications=1,
        ),
    )
    reader = _ExactReader(
        {
            intent_document.artifact_id: (intent_document, intent_payload),
            capability_document.artifact_id: (capability_document, capability_payload),
        }
    )
    sink = _ExactPlanSink()
    adapter = route.family.build_adapter(sink)
    try:
        result, receipt = adapter.lower_with_receipt(
            request,
            artifacts=reader,
            codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
            proof_policy=policy,
        )
        plan, payload = adapter.read_plan(receipt)
    except ReviewedIntentExecutionError:
        raise
    except BaseException:
        _fail(ReviewedIntentExecutionErrorCode.LOWERING_FAILED)
    if (
        receipt.operation != route.operation
        or receipt.manifest_sha256 != route.manifest.manifest_sha256
        or receipt.adapter != route.manifest.adapter
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    return LoweredReviewedIntent(
        route=route,
        result=result,
        receipt=receipt,
        plan=plan,
        payload=payload,
    )


def _actual_backend(freecad: object) -> CapabilityBackend:
    try:
        version_fields = _freecad_version(freecad)
        gui_up = freecad.GuiUp
    except (AttributeError, CapabilityCatalogError, TypeError, ValueError):
        _fail(ReviewedIntentExecutionErrorCode.NOT_VERIFIED)
    if type(gui_up) is not int or gui_up != 0:
        _fail(ReviewedIntentExecutionErrorCode.NOT_VERIFIED)
    return CapabilityBackend(
        backend_id="freecad",
        backend_version=tuple(int(item) for item in version_fields[:3]),
        build_fingerprint_sha256=_build_fingerprint(version_fields),
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
        platform_id=_platform_id(),
    )


def require_reviewed_route_verified(
    route: object,
    *,
    freecad: object,
) -> CapabilityBackend:
    """Require the installed source-pinned attestation for the actual build.

    The complete discovery was performed when the packaged attestation was
    generated.  At execution time a document is already open, so this seam
    binds that source-pinned evidence to the actual platform and exact
    ``FreeCAD.Version`` build.  The selected native rule then independently
    revalidates the build, TypeId, property contract, and operation effect.
    """

    if type(route) is not ReviewedIntentRoute:
        _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)
    try:
        packaged = load_current_packaged_freecad_reviewed_release_attestation()
        if type(packaged) is not FreeCadPackagedReviewedReleaseAttestation:
            raise TypeError
        attestation = decode_freecad_reviewed_release_attestation(
            packaged.raw,
            expected_source_attestation_sha256=packaged.resource_sha256,
        )
        backend = _actual_backend(freecad)
        validated = validate_freecad_reviewed_release_attestation(
            attestation,
            expected_release_version=__version__,
            runtime_backend=backend,
            discovery_snapshot_sha256=attestation.discovery_snapshot_sha256,
            discovery_manifest_sha256=attestation.discovery_manifest_sha256,
            expected_source_attestation_sha256=packaged.resource_sha256,
        )
    except ReviewedIntentExecutionError:
        raise
    except BaseException:
        _fail(ReviewedIntentExecutionErrorCode.NOT_VERIFIED)
    formal = tuple(
        item
        for item in validated.verification_set.formal_operations
        if item.operation_id == route.operation_id
    )
    native = tuple(
        item
        for item in validated.verification_set.native_types
        if item.native_type_id == route.operation.native_type_id
    )
    if (
        len(formal) != 1
        or len(native) != 1
        or route.operation_id not in native[0].formal_operation_ids
        or native[0].verification.adapter_contract_sha256
        != route.manifest.adapter.adapter_contract_sha256
        or formal[0].test_receipt_sha256 != native[0].verification.test_receipt_sha256
    ):
        _fail(ReviewedIntentExecutionErrorCode.NOT_VERIFIED)
    return backend


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedNativeExecutionResult:
    route: ReviewedIntentRoute
    object: object = field(repr=False, compare=False)
    plan_sha256: str
    plan_content_sha256: str
    native_receipt: object
    owned_objects: tuple[object, ...] = field(default=(), repr=False, compare=False)
    _verified_execution_context: InitVar[_ReviewedFamilyExecutionContext | None] = None
    _verified_dynamic_resolution: InitVar[_ReviewedDynamicProductResolution | None] = None
    result_kind: _ReviewedProductResultKind = field(init=False)
    semantic_roles: tuple[SemanticRole, ...] = field(init=False)

    def __post_init__(
        self,
        _verified_execution_context: _ReviewedFamilyExecutionContext | None,
        _verified_dynamic_resolution: _ReviewedDynamicProductResolution | None,
    ) -> None:
        owned = self.owned_objects
        if type(owned) is not tuple:
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        if not owned:
            owned = (self.object,)
            object.__setattr__(self, "owned_objects", owned)
        if (
            type(self.route) is not ReviewedIntentRoute
            or self.object is None
            or type(self.plan_sha256) is not str
            or type(self.plan_content_sha256) is not str
            or len(self.plan_sha256) != 64
            or len(self.plan_content_sha256) != 64
            or self.native_receipt is None
            or getattr(self.native_receipt, "plan_sha256", None) != self.plan_sha256
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        resolver = self.route.family.dynamic_resolver_for(self.route.operation)
        if resolver is None:
            if _verified_dynamic_resolution is not None:
                _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
            contract = self.route.family.accept_product_result(
                self.route.operation,
                self.object,
                owned,
                context=_verified_execution_context,
            )
        else:
            resolution = _verified_dynamic_resolution
            if (
                type(_verified_execution_context) is not _ReviewedFamilyExecutionContext
                or not self.route.family.minimum_sources
                <= len(_verified_execution_context.source_results)
                <= self.route.family.maximum_sources
                or type(resolution) is not _ReviewedDynamicProductResolution
                or resolution.resolver_id != resolver.resolver_id
                or resolution.resolver_version != resolver.resolver_version
                or resolution.resolver_contract_sha256 != resolver.resolver_contract_sha256
                or resolution.plan_sha256 != self.plan_sha256
                or resolution.plan_content_sha256 != self.plan_content_sha256
            ):
                _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
            contract = resolution.contract
            contract.validate(self.route.operation, self.object, owned)
        object.__setattr__(self, "result_kind", contract.result_kind)
        object.__setattr__(self, "semantic_roles", contract.semantic_roles)


def execute_reviewed_intent_native(
    session: object,
    value: object,
    *,
    source_results: object = (),
) -> ReviewedNativeExecutionResult:
    """Execute one static route through its family-owned native authority seam."""

    if (
        session is None
        or type(value) is not ReviewedIntentProgramV1
        or type(source_results) is not tuple
    ):
        _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)
    route = route_reviewed_intent(value)
    if not route.family.minimum_sources <= len(source_results) <= route.family.maximum_sources:
        _fail(ReviewedIntentExecutionErrorCode.INVALID_INPUT)
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        document = session.doc
    except BaseException:
        _fail(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
    require_reviewed_route_verified(route, freecad=FreeCAD)
    lowered = lower_reviewed_intent(value)
    context = _ReviewedFamilyExecutionContext(
        session=session,
        document=document,
        source_results=source_results,
    )
    try:
        before = tuple(document.Objects)
        family_result = route.family.apply_plan(
            document,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            route.operation,
            context,
        )
        dynamic_resolution = route.family.resolve_dynamic_product_result(
            lowered.plan,
            lowered.result.plan_document,
            route.operation,
            family_result,
        )
        result = family_result.object
        after = tuple(document.Objects)
    except ReviewedIntentExecutionError:
        raise
    except BaseException:
        _fail(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
    added = tuple(item for item in after if not any(item is existing for existing in before))
    owned = family_result.owned_objects
    if (
        len(after) != len(before) + len(owned)
        or len(added) != len(owned)
        or len({id(item) for item in added}) != len(added)
        or {id(item) for item in added} != {id(item) for item in owned}
        or result is not owned[0]
        or getattr(result, "TypeId", None) != route.operation.native_type_id
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    return ReviewedNativeExecutionResult(
        route=route,
        object=result,
        plan_sha256=lowered.result.plan_document.document_digest,
        plan_content_sha256=lowered.result.plan_document.content_sha256,
        native_receipt=family_result.receipt,
        owned_objects=owned,
        _verified_execution_context=context,
        _verified_dynamic_resolution=dynamic_resolution,
    )


__all__ = [
    "CURRENT_REVIEWED_INTENT_ROUTES",
    "REVIEWED_PART_BOX_ROUTE",
    "REVIEWED_PART_CSG_ROUTES",
    "REVIEWED_PART_CURVE_ROUTES",
    "REVIEWED_PART_DATUM_ROUTES",
    "REVIEWED_PART_OFFSET_ROUTES",
    "REVIEWED_PART_PROFILE_SURFACE_ROUTES",
    "REVIEWED_PART_PRIMITIVE_ROUTES",
    "REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES",
    "REVIEWED_PARTDESIGN_BOOLEAN_ROUTES",
    "REVIEWED_PARTDESIGN_DRESSUP_ROUTES",
    "REVIEWED_PARTDESIGN_PATTERN_ROUTES",
    "REVIEWED_PARTDESIGN_PROMOTION_ROUTES",
    "LoweredReviewedIntent",
    "ReviewedIntentExecutionError",
    "ReviewedIntentExecutionErrorCode",
    "ReviewedIntentRoute",
    "ReviewedNativeExecutionResult",
    "execute_reviewed_intent_native",
    "lower_reviewed_intent",
    "require_reviewed_route_verified",
    "route_reviewed_intent",
]
