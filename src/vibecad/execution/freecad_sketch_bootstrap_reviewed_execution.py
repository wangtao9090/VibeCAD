"""Family-only execution callbacks for reviewed Sketch CREATE bootstrap.

Nothing in this module registers a public route.  It closes the family-owned
execution handoff: exactly zero sources, one native rule, an exact newly-created
closure, and a receipt whose primary object is the Body-owned closed Sketch.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_sketch_bootstrap_adapter import (
    FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR,
    SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM,
    SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM,
    SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM,
    SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
    SKETCH_BOOTSTRAP_OPERATION_SPEC,
    SKETCH_BOOTSTRAP_XY_PLANE_TERM,
    sketch_bootstrap_reviewed_adapter_factory,
    validate_sketch_bootstrap_reviewed_plan,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric import freecad_sketch_bootstrap_rules as bootstrap_rules
from vibecad.parametric.freecad_sketch_bootstrap_rules import (
    SKETCH_BOOTSTRAP_NATIVE_TYPE_ID,
    SketchBootstrapBackendPlan,
    SketchBootstrapConformanceReceipt,
    SketchBootstrapExecutionBindings,
    SketchBootstrapRuleError,
    apply_sketch_bootstrap_plan,
    decode_sketch_bootstrap_backend_plan,
)

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.freecad-sketch-bootstrap-ownership.v1\0"


class SketchBootstrapExecutionError(RuntimeError):
    """Redacted family-only execution failure."""


def _integrity_failure() -> None:
    raise SketchBootstrapExecutionError("sketch bootstrap integrity failure")


def _execution_failure() -> None:
    raise SketchBootstrapExecutionError("sketch bootstrap execution failed")


def _term_identity(term: object) -> tuple[str, str, str, str]:
    try:
        result = (
            term.namespace,
            term.vocabulary_version,
            term.term_id,
            term.term_definition_sha256,
        )
    except Exception:
        _integrity_failure()
    return result


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


SKETCH_BOOTSTRAP_REVIEWED_PRODUCT_IDENTITIES: Final = (
    (
        f"{SKETCH_BOOTSTRAP_FAMILY_MANIFEST.family_id}."
        f"{SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id}",
        _semantic_operation(SKETCH_BOOTSTRAP_OPERATION_SPEC),
    ),
)


@dataclass(frozen=True, slots=True)
class SketchBootstrapProductContract:
    minimum_sources: int = 0
    maximum_sources: int = 0
    lifecycle: str = "create"
    primary_native_type_id: str = SKETCH_BOOTSTRAP_NATIVE_TYPE_ID
    owned_object_count: int = 10

    def __post_init__(self) -> None:
        if (
            self.minimum_sources != 0
            or self.maximum_sources != 0
            or self.lifecycle != "create"
            or self.primary_native_type_id != SKETCH_BOOTSTRAP_NATIVE_TYPE_ID
            or self.owned_object_count != 10
        ):
            _integrity_failure()

    def validate_sources(self, source_results: object) -> None:
        if type(source_results) is not tuple or source_results:
            _integrity_failure()

    def validate_owned(self, primary: object, owned: object) -> None:
        if (
            primary is None
            or type(owned) is not tuple
            or len(owned) != self.owned_object_count
            or owned[0] is not primary
            or len({id(item) for item in owned}) != len(owned)
        ):
            _integrity_failure()


SKETCH_BOOTSTRAP_PRODUCT_CONTRACT: Final = SketchBootstrapProductContract()
SKETCH_BOOTSTRAP_PRODUCT_CONTRACTS: Final = MappingProxyType(
    {SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id: SKETCH_BOOTSTRAP_PRODUCT_CONTRACT}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchBootstrapOwnershipReceipt:
    """Content-bound ownership of Sketch primary plus Body/Origin support closure."""

    native_receipt: SketchBootstrapConformanceReceipt
    object: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    origin_closure: tuple[object, ...] = field(repr=False, compare=False)
    ownership_identity: tuple[str, str, str, str]
    plane_identity: tuple[str, str, str, str]
    profile_identity: tuple[str, str, str, str]
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.native_receipt) is not SketchBootstrapConformanceReceipt
            or self.object is None
            or self.body is None
            or type(self.origin_closure) is not tuple
            or len(self.origin_closure) != 8
            or any(
                type(item) is not tuple
                or len(item) != 4
                or any(type(value) is not str for value in item)
                for item in (
                    self.ownership_identity,
                    self.plane_identity,
                    self.profile_identity,
                )
            )
        ):
            _integrity_failure()
        body = "\0".join(
            (
                self.native_receipt.receipt_sha256,
                *self.ownership_identity,
                *self.plane_identity,
                *self.profile_identity,
            )
        ).encode("utf-8")
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_OWNERSHIP_DIGEST_DOMAIN + body).hexdigest(),
        )

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def body_name(self) -> str:
        return self.native_receipt.body_name

    @property
    def state_sha256(self) -> str:
        return self.native_receipt.state_sha256

    @property
    def shape_sha256(self) -> str:
        return self.native_receipt.shape_sha256

    @property
    def geometry_sha256(self) -> str:
        return self.native_receipt.geometry_sha256

    @property
    def constraint_sha256(self) -> str:
        return self.native_receipt.constraint_sha256

    def owned_objects(self, result: object) -> tuple[object, ...]:
        if result is not self.object:
            _integrity_failure()
        # Product convention keeps the primary first.  Document sequence is
        # independently frozen by native_receipt.closure_names.
        return (self.object, self.body, *self.origin_closure)

    def validate_native_result(self, document: object, result: object) -> None:
        try:
            origin = bootstrap_rules._origin_closure(self.body)  # noqa: SLF001
            closure_in_document_order = (self.body, *origin, self.object)
            current = tuple(document.Objects)
            visible = all(
                any(item is existing for existing in current) for item in closure_in_document_order
            )
            xy_plane = origin[4]
            shape_sha256 = bootstrap_rules._shape_sha256(self.object)  # noqa: SLF001
            geometry_sha256 = bootstrap_rules._canonical_digest(  # noqa: SLF001
                bootstrap_rules._GEOMETRY_DIGEST_DOMAIN,  # noqa: SLF001
                bootstrap_rules._native_geometry_facts(self.object),  # noqa: SLF001
            )
            constraint_sha256 = bootstrap_rules._canonical_digest(  # noqa: SLF001
                bootstrap_rules._CONSTRAINT_DIGEST_DOMAIN,  # noqa: SLF001
                bootstrap_rules._constraint_facts(self.object),  # noqa: SLF001
            )
            state_sha256 = bootstrap_rules._state_digest(  # noqa: SLF001
                self.body, self.object, xy_plane
            )
            valid = (
                visible
                and self.object.Document is document
                and self.body.Document is document
                and self.object.TypeId == SKETCH_BOOTSTRAP_NATIVE_TYPE_ID
                and tuple(self.body.Group) == (self.object,)
                and self.body.Tip is self.object
                and tuple(item.Name for item in closure_in_document_order)
                == self.native_receipt.closure_names
                and hmac.compare_digest(shape_sha256, self.shape_sha256)
                and hmac.compare_digest(geometry_sha256, self.geometry_sha256)
                and hmac.compare_digest(constraint_sha256, self.constraint_sha256)
                and hmac.compare_digest(state_sha256, self.state_sha256)
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()
        SKETCH_BOOTSTRAP_PRODUCT_CONTRACT.validate_owned(result, self.owned_objects(result))


@dataclass(frozen=True, slots=True, kw_only=True)
class SketchBootstrapNativeExecution:
    object: object = field(repr=False, compare=False)
    receipt: SketchBootstrapOwnershipReceipt
    owned_objects: tuple[object, ...] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.receipt) is not SketchBootstrapOwnershipReceipt:
            _integrity_failure()
        SKETCH_BOOTSTRAP_PRODUCT_CONTRACT.validate_owned(self.object, self.owned_objects)


def resolve_sketch_bootstrap_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    identity = SKETCH_BOOTSTRAP_REVIEWED_PRODUCT_IDENTITIES[0]
    return (
        SKETCH_BOOTSTRAP_OPERATION_SPEC if (operation_id, semantic_operation) == identity else None
    )


def _decode_execution_plan(
    plan: object,
    payload: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> SketchBootstrapBackendPlan:
    if (
        type(plan) is not SketchBootstrapBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or operation != SKETCH_BOOTSTRAP_OPERATION_SPEC
        or plan.source_count != 0
        or plan.adapter_contract_sha256
        != FREECAD_SKETCH_BOOTSTRAP_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        or (
            (
                plan.operation_identity.namespace,
                plan.operation_identity.vocabulary_version,
                plan.operation_identity.term_id,
                plan.operation_identity.term_definition_sha256,
            ),
            (
                plan.ownership_identity.namespace,
                plan.ownership_identity.vocabulary_version,
                plan.ownership_identity.term_id,
                plan.ownership_identity.term_definition_sha256,
            ),
            (
                plan.plane_identity.namespace,
                plan.plane_identity.vocabulary_version,
                plan.plane_identity.term_id,
                plan.plane_identity.term_definition_sha256,
            ),
            (
                plan.profile_identity.namespace,
                plan.profile_identity.vocabulary_version,
                plan.profile_identity.term_id,
                plan.profile_identity.term_definition_sha256,
            ),
        )
        != (
            _term_identity(SKETCH_BOOTSTRAP_CREATE_OPERATION_TERM),
            _term_identity(SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM),
            _term_identity(SKETCH_BOOTSTRAP_XY_PLANE_TERM),
            _term_identity(SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM),
        )
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(payload).hexdigest() != plan_document.content_sha256
        or len(payload) != plan_document.size_bytes
        or payload != plan.canonical_bytes
    ):
        _integrity_failure()
    try:
        decoded = decode_sketch_bootstrap_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except SketchBootstrapRuleError:
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def execute_sketch_bootstrap_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
) -> SketchBootstrapNativeExecution:
    SKETCH_BOOTSTRAP_PRODUCT_CONTRACT.validate_sources(source_results)
    checked = _decode_execution_plan(plan, payload, plan_document, operation)
    try:
        before, snapshots = bootstrap_rules._snapshot_document(document)  # noqa: SLF001
    except SketchBootstrapRuleError:
        _integrity_failure()
    try:
        native_receipt = apply_sketch_bootstrap_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
            bindings=SketchBootstrapExecutionBindings(
                document=document,
                body_id=checked.body_id,
            ),
        )
        body = document.getObject(native_receipt.body_name)
        result = document.getObject(native_receipt.object_name)
        if body is None or result is None:
            _integrity_failure()
        origin = bootstrap_rules._origin_closure(body)  # noqa: SLF001
        ownership = SketchBootstrapOwnershipReceipt(
            native_receipt=native_receipt,
            object=result,
            body=body,
            origin_closure=origin,
            ownership_identity=_term_identity(SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM),
            plane_identity=_term_identity(SKETCH_BOOTSTRAP_XY_PLANE_TERM),
            profile_identity=_term_identity(SKETCH_BOOTSTRAP_CLOSED_CIRCLE_PROFILE_TERM),
        )
        ownership.validate_native_result(document, result)
        owned = ownership.owned_objects(result)
    except KeyboardInterrupt:
        if not bootstrap_rules._restore_document(  # noqa: SLF001
            document, before, snapshots
        ):
            _execution_failure()
        raise
    except BaseException:
        if not bootstrap_rules._restore_document(  # noqa: SLF001
            document, before, snapshots
        ):
            _execution_failure()
        _execution_failure()
    return SketchBootstrapNativeExecution(
        object=result,
        receipt=ownership,
        owned_objects=owned,
    )


@dataclass(frozen=True, slots=True)
class SketchBootstrapReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_with_sources: Callable[
        [
            object,
            object,
            bytes,
            DocumentRef,
            ReviewedOperationSpec,
            tuple[object, ...],
        ],
        SketchBootstrapNativeExecution,
    ]
    product_contracts: MappingProxyType
    shared_registration_ready: bool = False


SKETCH_BOOTSTRAP_REVIEWED_FAMILY_SPEC: Final = SketchBootstrapReviewedFamilySpec(
    manifest=SKETCH_BOOTSTRAP_FAMILY_MANIFEST,
    subject_type_term=BridgeTermRef(
        term_ref_id=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.term_ref_id,
        namespace=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.namespace,
        vocabulary_version=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.vocabulary_version,
        term_id=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.term_id,
        term_definition_sha256=SKETCH_BOOTSTRAP_BODY_OWNERSHIP_TERM.term_definition_sha256,
    ),
    operation_ids=(SKETCH_BOOTSTRAP_OPERATION_SPEC.operation_id,),
    adapter_factory=sketch_bootstrap_reviewed_adapter_factory,
    validate_plan=validate_sketch_bootstrap_reviewed_plan,
    execute_with_sources=execute_sketch_bootstrap_reviewed_plan_with_sources,
    product_contracts=SKETCH_BOOTSTRAP_PRODUCT_CONTRACTS,
)


__all__ = [
    "SKETCH_BOOTSTRAP_PRODUCT_CONTRACT",
    "SKETCH_BOOTSTRAP_PRODUCT_CONTRACTS",
    "SKETCH_BOOTSTRAP_REVIEWED_FAMILY_SPEC",
    "SKETCH_BOOTSTRAP_REVIEWED_PRODUCT_IDENTITIES",
    "SketchBootstrapExecutionError",
    "SketchBootstrapNativeExecution",
    "SketchBootstrapOwnershipReceipt",
    "SketchBootstrapProductContract",
    "SketchBootstrapReviewedFamilySpec",
    "execute_sketch_bootstrap_reviewed_plan_with_sources",
    "resolve_sketch_bootstrap_reviewed_operation",
]
