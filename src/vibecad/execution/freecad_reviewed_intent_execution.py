"""Trusted product bridge for executing Reviewed FreeCAD intents.

This module is the narrow seam between an authority-free Reviewed lowering
pipeline and already-reviewed native rules.  Model input selects only a
complete public semantic identity.  The native ``TypeId``, adapter, proof
policy, plan decoder, and callable are selected exclusively by a static family
descriptor owned by this module.

Adding a family means registering one trusted descriptor and an explicit
operation allowlist below.  It never adds model-provided callables or MCP
tools.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
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

    def __post_init__(self) -> None:
        if self.object is None or self.receipt is None:
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
    minimum_sources: int = 0
    maximum_sources: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.manifest) is not FamilyBatchManifest
            or type(self.subject_type_term) is not BridgeTermRef
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
            or not any(term == self.subject_type_term for term in self.manifest.request_terms)
            or type(self.minimum_sources) is not int
            or type(self.maximum_sources) is not int
            or not 0 <= self.minimum_sources <= self.maximum_sources <= 8
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)

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


_PART_CORE_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_CORE_MANIFEST,
    subject_type_term=_bridge_term(PART_CORE_STRUCTURE_TERM),
    adapter_factory=build_part_core_adapter,
    validate_plan=_validate_part_core_plan,
    execute_plan=_execute_part_core_plan,
)

_PART_CURVE_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_CURVE_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PART_CURVE_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PART_CURVE_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PART_CURVE_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PART_CURVE_REVIEWED_FAMILY_SPEC.execute_plan,
)

_PART_CSG_FAMILY: Final = _ReviewedIntentFamilyDescriptor(
    manifest=PART_CSG_REVIEWED_FAMILY_SPEC.manifest,
    subject_type_term=PART_CSG_REVIEWED_FAMILY_SPEC.subject_type_term,
    adapter_factory=PART_CSG_REVIEWED_FAMILY_SPEC.adapter_factory,
    validate_plan=PART_CSG_REVIEWED_FAMILY_SPEC.validate_plan,
    execute_plan=PART_CSG_REVIEWED_FAMILY_SPEC.execute_plan,
    minimum_sources=2,
    maximum_sources=2,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedIntentRoute:
    """One static product route from public identity to reviewed contracts."""

    operation_id: str
    semantic_operation: str
    family: _ReviewedIntentFamilyDescriptor = field(repr=False)
    manifest: FamilyBatchManifest
    operation: ReviewedOperationSpec
    subject_type_term: BridgeTermRef
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
            or self.semantic_operation != _semantic_operation(self.operation)
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        formal = tuple(
            item
            for item in current_freecad_intent_capability_specs()
            if item.operation_id == self.operation_id
        )
        if (
            len(formal) != 1
            or formal[0].semantic_operation != self.semantic_operation
            or formal[0].native_type_id != self.operation.native_type_id
            or formal[0].adapter_id != self.manifest.adapter.adapter_id
            or formal[0].adapter_version != self.manifest.adapter.adapter_version
            or formal[0].adapter_contract_sha256 != self.manifest.adapter.adapter_contract_sha256
            or formal[0].rule_id != self.manifest.rule_id
            or formal[0].rule_contract_sha256 != self.manifest.rule_contract_sha256
        ):
            _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
        body = "\0".join(
            (
                self.operation_id,
                self.semantic_operation,
                self.manifest.manifest_sha256,
                self.operation.specification_sha256,
                *self.subject_type_term.semantic_identity,
            )
        ).encode("utf-8")
        object.__setattr__(
            self,
            "route_contract_sha256",
            hashlib.sha256(_ROUTE_CONTRACT_DOMAIN + body).hexdigest(),
        )


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
    return tuple(
        ReviewedIntentRoute(
            operation_id=f"{family.manifest.family_id}.{operation.operation_id}",
            semantic_operation=_semantic_operation(operation),
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
_REVIEWED_FAMILY_ROUTE_SETS: Final = (
    REVIEWED_PART_PRIMITIVE_ROUTES,
    REVIEWED_PART_CURVE_ROUTES,
    REVIEWED_PART_CSG_ROUTES,
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

    def __post_init__(self) -> None:
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
        result = family_result.object
        after = tuple(document.Objects)
    except ReviewedIntentExecutionError:
        raise
    except BaseException:
        _fail(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
    added = tuple(item for item in after if not any(item is existing for existing in before))
    if (
        len(after) != len(before) + 1
        or len(added) != 1
        or result is not added[0]
        or getattr(result, "TypeId", None) != route.operation.native_type_id
    ):
        _fail(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)
    return ReviewedNativeExecutionResult(
        route=route,
        object=result,
        plan_sha256=lowered.result.plan_document.document_digest,
        plan_content_sha256=lowered.result.plan_document.content_sha256,
        native_receipt=family_result.receipt,
    )


__all__ = [
    "CURRENT_REVIEWED_INTENT_ROUTES",
    "REVIEWED_PART_BOX_ROUTE",
    "REVIEWED_PART_CURVE_ROUTES",
    "REVIEWED_PART_CSG_ROUTES",
    "REVIEWED_PART_PRIMITIVE_ROUTES",
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
