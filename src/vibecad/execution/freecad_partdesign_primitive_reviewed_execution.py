"""Private product callbacks for the reviewed PartDesign primitive family.

The existing adapter and native rule remain the semantic and mutation
authorities.  This module adds only the product bridge: a modern static family
manifest, exact route identities, same-run base authentication, and a
content-bound ownership closure for the resulting Body feature.

No native Body, Tip, object name, or TypeId is accepted from model-controlled
input.  A first additive feature creates a new Body only after all plan and
source checks pass.  Every other operation consumes one engine-owned Reviewed
PartDesign primitive result and derives its Body solely from that opaque
result's ownership receipt.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge import freecad_partdesign_primitive_adapter as primitive_adapter
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_partdesign_primitive_adapter import (
    FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
    PRIMITIVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    PRIMITIVE_CAPABILITY_SCHEMA_TERM,
    PRIMITIVE_INTENT_DOCUMENT_ROLE_TERM,
    PRIMITIVE_OPERATION_TERMS,
    PRIMITIVE_PLAN_DOCUMENT_ROLE_TERM,
    PRIMITIVE_PLAN_SCHEMA_TERM,
    PRIMITIVE_REQUEST_TERMS,
    PRIMITIVE_STRUCTURE_TERM,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanDraft,
    ReviewedPlanReceipt,
)
from vibecad.parametric import freecad_partdesign_primitive_rules as primitive_rules
from vibecad.parametric.feature_graph_v2 import (
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES,
    PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID,
    PARTDESIGN_PRIMITIVE_PLAN_MEDIA_TYPE,
    PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
    PARTDESIGN_PRIMITIVE_RULE_ID,
    AuthenticatedPrimitiveObject,
    PartDesignPrimitiveBackendPlan,
    PartDesignPrimitiveConformanceReceipt,
    PartDesignPrimitiveExecutionBindings,
    PartDesignPrimitiveOperation,
    apply_partdesign_primitive_plan,
    decode_partdesign_primitive_backend_plan,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.partdesign-primitive-ownership.v1\0"
_BODY_NAME_PREFIX = "ReviewedPartDesignBody"
_BODY_HELPER_TYPE_IDS: Final = (
    "PartDesign::Body",
    "App::Origin",
    "App::Line",
    "App::Line",
    "App::Line",
    "App::Plane",
    "App::Plane",
    "App::Plane",
    "App::Point",
)
_BODY_HELPER_ROLES: Final = (SemanticRole.PART, *(SemanticRole.SUPPORT,) * 8)
_ORIGIN_FEATURE_ROLES: Final = (
    "X_Axis",
    "Y_Axis",
    "Z_Axis",
    "XY_Plane",
    "XZ_Plane",
    "YZ_Plane",
    "Origin",
)


def _integrity_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _execution_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _shape_sha256(item: object) -> str:
    try:
        raw = item.Shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _native_spec(operation: PartDesignPrimitiveOperation):
    # The rules module owns the one semantic-operation -> native-code table.
    # Reusing it here avoids a second TypeId/property authority table.
    return primitive_rules._NATIVE_PRIMITIVE_SPECS[operation]  # noqa: SLF001


PARTDESIGN_PRIMITIVE_OPERATION_SPECS: Final = tuple(
    ReviewedOperationSpec(
        operation_id=terms.operation.value,
        semantic_term=_bridge_term(terms.operation_term),
        native_type_id=_native_spec(terms.operation).type_id,
        native_operation=_native_spec(terms.operation).object_prefix,
        native_property_names=tuple(
            {
                "BaseFeature",
                "MapMode",
                "Placement",
                "Refine",
                "Shape",
                *(item.property_name for item in _native_spec(terms.operation).parameters),
                *(name for name, _value in _native_spec(terms.operation).fixed_properties),
            }
        ),
    )
    for terms in PRIMITIVE_OPERATION_TERMS
)

PARTDESIGN_PRIMITIVE_REQUEST_TERMS: Final = tuple(
    {
        item.term_ref_id: item for item in (*PRIMITIVE_REQUEST_TERMS, PFG_SELECTOR_FEATURE_NODE)
    }.values()
)

PARTDESIGN_PRIMITIVE_MANIFEST: Final = FamilyBatchManifest(
    family_id="partdesign",
    family_version="1.0.0",
    adapter=FREECAD_PARTDESIGN_PRIMITIVE_ADAPTER_DESCRIPTOR,
    backend_engine="FreeCAD",
    backend_version="1.1.0",
    backend_build_id=hashlib.sha256(
        PARTDESIGN_PRIMITIVE_FREECAD_ENGINE_BUILD_ID.encode("ascii")
    ).hexdigest(),
    rule_id=PARTDESIGN_PRIMITIVE_RULE_ID,
    rule_contract_sha256=PARTDESIGN_PRIMITIVE_RULE_CONTRACT_SHA256,
    intent_role_term=PRIMITIVE_INTENT_DOCUMENT_ROLE_TERM,
    intent_schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    intent_media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    capability_role_term=PRIMITIVE_CAPABILITY_DOCUMENT_ROLE_TERM,
    capability_schema_term=PRIMITIVE_CAPABILITY_SCHEMA_TERM,
    capability_media_type=("application/vnd.vibecad.freecad-partdesign-primitive-capability+json"),
    plan_role_term=PRIMITIVE_PLAN_DOCUMENT_ROLE_TERM,
    plan_schema_term=PRIMITIVE_PLAN_SCHEMA_TERM,
    plan_media_type=PARTDESIGN_PRIMITIVE_PLAN_MEDIA_TYPE,
    request_terms=PARTDESIGN_PRIMITIVE_REQUEST_TERMS,
    operations=PARTDESIGN_PRIMITIVE_OPERATION_SPECS,
    max_plan_bytes=MAX_PARTDESIGN_PRIMITIVE_PLAN_BYTES,
)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PARTDESIGN_PRIMITIVE_MANIFEST.operations}
)
_OPERATION_TERMS_BY_ID: Final = MappingProxyType(
    {item.operation.value: item for item in PRIMITIVE_OPERATION_TERMS}
)

PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_OPERATIONS: Final = tuple(PartDesignPrimitiveOperation)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{PARTDESIGN_PRIMITIVE_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_OPERATIONS
    }
)
PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPrimitiveOwnedClosureContract:
    """One exact newly-owned closure selected only by operation/source count."""

    source_count: int
    owned_type_ids: tuple[str, ...]
    semantic_roles: tuple[SemanticRole, ...]

    def __post_init__(self) -> None:
        if (
            type(self.source_count) is not int
            or self.source_count not in {0, 1}
            or type(self.owned_type_ids) is not tuple
            or not self.owned_type_ids
            or type(self.semantic_roles) is not tuple
            or len(self.semantic_roles) != len(self.owned_type_ids)
            or any(type(item) is not str or not item for item in self.owned_type_ids)
            or any(type(item) is not SemanticRole for item in self.semantic_roles)
            or self.semantic_roles[0] is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def validate_owned(self, primary: object, owned: tuple[object, ...]) -> None:
        if (
            primary is None
            or type(owned) is not tuple
            or not owned
            or owned[0] is not primary
            or len(owned) != len(self.owned_type_ids)
            or len({id(item) for item in owned}) != len(owned)
            or any(
                getattr(item, "TypeId", None) != expected
                for item, expected in zip(owned, self.owned_type_ids, strict=True)
            )
        ):
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPrimitiveProductContract:
    """Static source cardinality and closure variants for one operation."""

    operation: PartDesignPrimitiveOperation
    native_type_id: str
    minimum_sources: int
    maximum_sources: int
    closure_variants: tuple[PartDesignPrimitiveOwnedClosureContract, ...]
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        expected_additive = self.operation.value.startswith("additive_")
        expected_counts = (0, 1) if expected_additive else (1,)
        if (
            type(self.operation) is not PartDesignPrimitiveOperation
            or self.native_type_id != _native_spec(self.operation).type_id
            or (self.minimum_sources, self.maximum_sources)
            != ((0, 1) if expected_additive else (1, 1))
            or type(self.closure_variants) is not tuple
            or tuple(item.source_count for item in self.closure_variants) != expected_counts
            or any(
                type(item) is not PartDesignPrimitiveOwnedClosureContract
                or item.owned_type_ids[0] != self.native_type_id
                for item in self.closure_variants
            )
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def closure_for_sources(self, source_count: int) -> PartDesignPrimitiveOwnedClosureContract:
        matching = tuple(
            item for item in self.closure_variants if item.source_count == source_count
        )
        if len(matching) != 1:
            _integrity_failure()
        return matching[0]


PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS: Final = MappingProxyType(
    {
        operation: PartDesignPrimitiveProductContract(
            operation=operation,
            native_type_id=_native_spec(operation).type_id,
            minimum_sources=0 if operation.value.startswith("additive_") else 1,
            maximum_sources=1,
            closure_variants=(
                *(
                    (
                        PartDesignPrimitiveOwnedClosureContract(
                            source_count=0,
                            owned_type_ids=(
                                _native_spec(operation).type_id,
                                *_BODY_HELPER_TYPE_IDS,
                            ),
                            semantic_roles=(SemanticRole.FEATURE, *_BODY_HELPER_ROLES),
                        ),
                    )
                    if operation.value.startswith("additive_")
                    else ()
                ),
                PartDesignPrimitiveOwnedClosureContract(
                    source_count=1,
                    owned_type_ids=(_native_spec(operation).type_id,),
                    semantic_roles=(SemanticRole.FEATURE,),
                ),
            ),
        )
        for operation in PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_OPERATIONS
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPrimitiveResultInvariant:
    operation: PartDesignPrimitiveOperation
    native_type_id: str
    semantic_role: SemanticRole = SemanticRole.FEATURE

    def __post_init__(self) -> None:
        if (
            type(self.operation) is not PartDesignPrimitiveOperation
            or self.native_type_id != _native_spec(self.operation).type_id
            or self.semantic_role is not SemanticRole.FEATURE
        ):
            _integrity_failure()

    def validate_native_result(
        self,
        document: object,
        result: object,
        body: object,
        base: object | None,
        receipt: PartDesignPrimitiveConformanceReceipt,
        *,
        result_shape_sha256: str,
    ) -> None:
        try:
            shape = result.Shape
            group = tuple(body.Group)
            expected_group = (result,) if base is None else (base, result)
            valid = (
                type(receipt) is PartDesignPrimitiveConformanceReceipt
                and receipt.operation is self.operation
                and result.Document is document
                and body.Document is document
                and document.getObject(receipt.object_name) is result
                and any(result is item for item in tuple(document.Objects))
                and any(body is item for item in tuple(document.Objects))
                and result.Name == receipt.object_name
                and result.TypeId == self.native_type_id
                and body.TypeId == "PartDesign::Body"
                and body.Tip is result
                and result.BaseFeature is base
                and group == expected_group
                and result.isValid()
                and tuple(result.State) == ("Up-to-date",)
                and not shape.isNull()
                and shape.isValid()
                and len(shape.Solids) == 1
                and math.isfinite(float(shape.Volume))
                and float(shape.Volume) > 1e-9
                and hmac.compare_digest(result_shape_sha256, _shape_sha256(result))
                and math.isclose(
                    float(shape.Volume), receipt.after_volume_mm3, rel_tol=0.0, abs_tol=1e-7
                )
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adopted_observation(self, observation: object) -> None:
        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != self.native_type_id
            or observation.semantic_role != self.semantic_role.value
            or observation.valid_shape is not True
            or observation.solid_count != 1
            or observation.volume_mm3 is None
            or observation.volume_mm3 <= 1e-9
        ):
            _integrity_failure()


PARTDESIGN_PRIMITIVE_RESULT_INVARIANTS: Final = MappingProxyType(
    {
        operation: PartDesignPrimitiveResultInvariant(
            operation=operation,
            native_type_id=_native_spec(operation).type_id,
        )
        for operation in PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_OPERATIONS
    }
)


def _validate_body_helpers(document: object, body: object) -> tuple[object, ...]:
    try:
        origin = body.Origin
        helpers = tuple(origin.OriginFeatures)
        closure = (body, origin, *helpers)
        type_ids = tuple(item.TypeId for item in closure)
        document_objects = tuple(document.Objects)
        valid = (
            type_ids == _BODY_HELPER_TYPE_IDS
            and origin.Document is document
            and tuple(origin.Group) == ()
            and all(item.Document is document for item in closure)
            and all(any(item is existing for existing in document_objects) for item in closure)
            and len({id(item) for item in closure}) == len(closure)
            and all(
                helper.Role == role
                and tuple(helper.InList) == (origin,)
                and helper.isValid()
                and tuple(helper.State) == ("Up-to-date",)
                for helper, role in zip(helpers, _ORIGIN_FEATURE_ROLES, strict=True)
            )
        )
    except (Exception, SystemExit):
        valid = False
        closure = ()
    if not valid:
        _integrity_failure()
    return closure


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignReviewedBaseBinding:
    """Engine-owned, family-neutral authority for one usable Body Tip.

    Other reviewed PartDesign product families may return this exact private
    record from their ownership receipt.  It carries no public wire fields and
    never resolves a Body by name, Tip order, or document scan.
    """

    object: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    body_closure: tuple[object, ...] = field(repr=False, compare=False)
    result_shape_sha256: str

    def __post_init__(self) -> None:
        if (
            self.object is None
            or self.body is None
            or type(self.body_closure) is not tuple
            or not self.body_closure
            or self.body_closure[0] is not self.body
            or not _is_sha256(self.result_shape_sha256)
        ):
            _integrity_failure()

    def validate(self, document: object) -> None:
        current_closure = _validate_body_helpers(document, self.body)
        try:
            shape = self.object.Shape
            valid = (
                len(current_closure) == len(self.body_closure)
                and all(
                    current is original
                    for current, original in zip(current_closure, self.body_closure, strict=True)
                )
                and self.object.Document is document
                and self.body.Document is document
                and self.object.TypeId.startswith("PartDesign::")
                and self.object.TypeId != "PartDesign::Body"
                and self.body.TypeId == "PartDesign::Body"
                and self.body.Tip is self.object
                and tuple(self.body.Group) == (self.object,)
                and self.object.BaseFeature is None
                and self.object.isValid()
                and tuple(self.object.State) == ("Up-to-date",)
                and not shape.isNull()
                and shape.isValid()
                and len(shape.Solids) == 1
                and math.isfinite(float(shape.Volume))
                and float(shape.Volume) > 1e-9
                and hmac.compare_digest(self.result_shape_sha256, _shape_sha256(self.object))
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignPrimitiveOwnershipClosure:
    """Opaque same-run authority for result freshness, Body, Tip, and helpers."""

    invariant: PartDesignPrimitiveResultInvariant
    native_receipt: PartDesignPrimitiveConformanceReceipt
    object: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    base: object | None = field(default=None, repr=False, compare=False)
    body_closure: tuple[object, ...] = field(repr=False, compare=False)
    created_body: bool
    base_shape_sha256: str | None
    result_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        expected_base_digest = None if self.base is None else _shape_sha256(self.base)
        if (
            type(self.invariant) is not PartDesignPrimitiveResultInvariant
            or type(self.native_receipt) is not PartDesignPrimitiveConformanceReceipt
            or self.native_receipt.operation is not self.invariant.operation
            or self.object is None
            or getattr(self.object, "Name", None) != self.native_receipt.object_name
            or self.body is None
            or type(self.body_closure) is not tuple
            or not self.body_closure
            or self.body_closure[0] is not self.body
            or type(self.created_body) is not bool
            or self.created_body != (self.base is None)
            or self.base_shape_sha256 != expected_base_digest
            or not _is_sha256(self.result_shape_sha256)
        ):
            _integrity_failure()
        body = "\0".join(
            (
                self.native_receipt.receipt_sha256,
                self.invariant.operation.value,
                self.body.Name,
                "created" if self.created_body else "existing",
                self.base_shape_sha256 or "none",
                self.result_shape_sha256,
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
    def operation(self) -> PartDesignPrimitiveOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    @property
    def native_type_id(self) -> str:
        return self.invariant.native_type_id

    @property
    def semantic_role(self) -> SemanticRole:
        return self.invariant.semantic_role

    @property
    def partdesign_base_binding(self) -> PartDesignReviewedBaseBinding:
        return PartDesignReviewedBaseBinding(
            object=self.object,
            body=self.body,
            body_closure=self.body_closure,
            result_shape_sha256=self.result_shape_sha256,
        )

    def owned_objects(self, result: object) -> tuple[object, ...]:
        if result is not self.object:
            _integrity_failure()
        return (result, *self.body_closure) if self.created_body else (result,)

    def validate_native_result(self, document: object, result: object) -> None:
        current_body_closure = _validate_body_helpers(document, self.body)
        if (
            len(current_body_closure) != len(self.body_closure)
            or any(
                current is not original
                for current, original in zip(current_body_closure, self.body_closure, strict=True)
            )
            or (
                self.base is not None
                and not hmac.compare_digest(self.base_shape_sha256 or "", _shape_sha256(self.base))
            )
        ):
            _integrity_failure()
        self.invariant.validate_native_result(
            document,
            result,
            self.body,
            self.base,
            self.native_receipt,
            result_shape_sha256=self.result_shape_sha256,
        )

    def validate_adoption(
        self,
        document: object,
        result: object,
        observation: object,
    ) -> None:
        self.validate_native_result(document, result)
        contract = PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[self.operation]
        contract.closure_for_sources(0 if self.created_body else 1).validate_owned(
            result,
            self.owned_objects(result),
        )
        self.invariant.validate_adopted_observation(observation)

    def validate_adopted_observation(self, observation: object) -> None:
        self.invariant.validate_adopted_observation(observation)


def resolve_partdesign_primitive_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def _build_reviewed_plan(
    document: DocumentRef,
    payload: bytes,
    request_digest: str,
    manifest: FamilyBatchManifest,
) -> ReviewedPlanDraft:
    if manifest is not PARTDESIGN_PRIMITIVE_MANIFEST:
        _integrity_failure()
    graph: ParametricFeatureGraphV2 = decode_parametric_feature_graph_v2(
        payload,
        expected_sha256=document.document_digest,
    )
    plan, subject = primitive_adapter._build_plan(  # noqa: SLF001
        document,
        payload,
        graph,
        request_digest,
    )
    terms = _OPERATION_TERMS_BY_ID.get(plan.operation.value)
    if terms is None:
        _integrity_failure()
    return ReviewedPlanDraft(
        payload=plan.canonical_bytes,
        semantic_plan_sha256=plan.plan_sha256,
        operation_term=_bridge_term(terms.operation_term),
        subjects=(subject,),
    )


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartDesignPrimitiveBackendPlan:
    if (
        type(plan) is not PartDesignPrimitiveBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PARTDESIGN_PRIMITIVE_MANIFEST.operations
        or plan.operation not in PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_OPERATIONS
        or _OPERATIONS_BY_ID.get(plan.operation.value) != operation
        or plan.adapter_contract_sha256
        != PARTDESIGN_PRIMITIVE_MANIFEST.adapter.adapter_contract_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    contract = PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[plan.operation]
    source_count = 0 if plan.base is None else 1
    if not contract.minimum_sources <= source_count <= contract.maximum_sources:
        _integrity_failure()
    try:
        decoded = decode_partdesign_primitive_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_partdesign_primitive_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != PARTDESIGN_PRIMITIVE_MANIFEST.manifest_sha256
        or receipt.adapter != PARTDESIGN_PRIMITIVE_MANIFEST.adapter
    ):
        _integrity_failure()
    checked = _validate_plan_contract(plan, receipt.plan_document, operation)
    if (
        checked.lowering_request_sha256 != receipt.request_digest
        or checked.source_artifact_id != receipt.source_document.artifact_id
        or checked.source_graph_id != receipt.source_document.document_id
        or checked.source_graph_sha256 != receipt.source_document.document_digest
        or checked.source_content_sha256 != receipt.source_document.content_sha256
    ):
        _integrity_failure()


def partdesign_primitive_reviewed_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    return ExactReviewedFamilyAdapter(
        PARTDESIGN_PRIMITIVE_MANIFEST,
        sink,
        build_plan=_build_reviewed_plan,
        decode_plan=decode_partdesign_primitive_backend_plan,
        validate_binding=validate_partdesign_primitive_reviewed_plan,
    )


def _decode_execution_plan(
    plan: object,
    payload: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartDesignPrimitiveBackendPlan:
    if type(payload) is not bytes:
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_partdesign_primitive_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    return checked


def _read_identity(session: object, item: object) -> EntityIdentity:
    try:
        identity = session.read_object_identity(item)
    except (AttributeError, KeyError, TypeError, ValueError):
        _integrity_failure()
    if type(identity) is not EntityIdentity:
        _integrity_failure()
    return identity


def _authenticated_base_binding(
    document: object,
    plan: PartDesignPrimitiveBackendPlan,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> tuple[object | None, AuthenticatedPrimitiveObject | None]:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedNativeExecutionResult,
    )

    expected_count = 0 if plan.base is None else 1
    if (
        session is None
        or getattr(session, "doc", None) is not document
        or type(source_results) is not tuple
        or len(source_results) != expected_count
    ):
        _integrity_failure()
    if plan.base is None:
        return None, None
    source = source_results[0]
    if type(source) is not ReviewedNativeExecutionResult:
        _integrity_failure()
    item = source.object
    ownership = source.native_receipt
    base_binding = getattr(ownership, "partdesign_base_binding", None)
    if type(base_binding) is not PartDesignReviewedBaseBinding or base_binding.object is not item:
        _integrity_failure()
    body = base_binding.body
    try:
        feature_identity = _read_identity(session, item)
        body_identity = _read_identity(session, body)
        document_objects = tuple(document.Objects)
        receipt_operation = getattr(ownership, "operation", None)
        valid = (
            source.route.operation in source.route.manifest.operations
            and receipt_operation.value == source.route.operation.operation_id
            and getattr(ownership, "plan_sha256", None) == source.plan_sha256
            and getattr(ownership, "object_name", None) == item.Name
            and item.Document is document
            and body.Document is document
            and any(item is existing for existing in document_objects)
            and any(body is existing for existing in document_objects)
            and item.TypeId == source.route.operation.native_type_id
            and feature_identity.object_type == item.TypeId
            and feature_identity.feature_id is not None
            and feature_identity.semantic_role is SemanticRole.FEATURE
            and feature_identity.provenance.source is ProvenanceSource.MODEL
            and feature_identity.provenance.operation_id == "apply_reviewed_intent"
            and body_identity.object_type == "PartDesign::Body"
            and body_identity.feature_id is not None
            and body_identity.semantic_role is SemanticRole.PART
            and body_identity.provenance.source is ProvenanceSource.MODEL
            and body_identity.provenance.operation_id == "apply_reviewed_intent"
            and body.Tip is item
            and tuple(body.Group) == (item,)
        )
    except (Exception, SystemExit):
        valid = False
    if not valid:
        _integrity_failure()
    base_binding.validate(document)
    for helper in base_binding.body_closure[1:]:
        identity = _read_identity(session, helper)
        if (
            identity.object_type != helper.TypeId
            or identity.feature_id is None
            or identity.semantic_role is not SemanticRole.SUPPORT
            or identity.provenance.source is not ProvenanceSource.MODEL
            or identity.provenance.operation_id != "apply_reviewed_intent"
        ):
            _integrity_failure()
    return body, AuthenticatedPrimitiveObject(
        object=item,
        node_id=plan.base.node_id,
        result_id=plan.base.result_id,
    )


def _document_snapshot(document: object) -> tuple[object, ...]:
    try:
        before = tuple(document.Objects)
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or len({id(item) for item in before}) != len(before)
        ):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        _integrity_failure()
    return before


def _added_objects(document: object, before: tuple[object, ...]) -> tuple[object, ...]:
    try:
        after = tuple(document.Objects)
    except (AttributeError, TypeError):
        _integrity_failure()
    return tuple(item for item in after if not any(item is existing for existing in before))


def _restore_document(document: object, before: tuple[object, ...]) -> None:
    try:
        added = _added_objects(document, before)
        bodies = tuple(
            item for item in added if getattr(item, "TypeId", None) == "PartDesign::Body"
        )
        non_bodies = tuple(item for item in added if not any(item is body for body in bodies))
        # Removing a Body first lets FreeCAD remove its protected Origin closure
        # atomically.  The residual pass is needed only for hosts/fakes that do
        # not cascade container deletion.
        for item in (*bodies, *reversed(non_bodies)):
            name = getattr(item, "Name", None)
            if type(name) is str and document.getObject(name) is item:
                document.removeObject(name)
        document.recompute()
        current = tuple(document.Objects)
        restored = len(current) == len(before) and all(
            actual is original for actual, original in zip(current, before, strict=True)
        )
    except (Exception, SystemExit):
        restored = False
    if not restored:
        _integrity_failure()


def _create_reviewed_body(
    document: object,
    plan: PartDesignPrimitiveBackendPlan,
    before: tuple[object, ...],
) -> tuple[object, tuple[object, ...]]:
    name = f"{_BODY_NAME_PREFIX}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(name) is not None:
            raise ValueError
        body = document.addObject("PartDesign::Body", name)
        document.recompute()
        closure = _validate_body_helpers(document, body)
        added = _added_objects(document, before)
        if (
            tuple(body.Group) != ()
            or body.Tip is not None
            or len(added) != len(closure)
            or any(item is not expected for item, expected in zip(added, closure, strict=True))
        ):
            raise ValueError
    except BaseException as error:
        _restore_document(document, before)
        if isinstance(error, KeyboardInterrupt):
            raise
        _execution_failure()
    return body, closure


def execute_partdesign_primitive_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> object:
    checked = _decode_execution_plan(plan, payload, plan_document, operation)
    contract = PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[checked.operation]
    source_count = 0 if checked.base is None else 1
    if (
        type(source_results) is not tuple
        or len(source_results) != source_count
        or not contract.minimum_sources <= source_count <= contract.maximum_sources
    ):
        _integrity_failure()
    body, authenticated_base = _authenticated_base_binding(
        document,
        checked,
        source_results,
        session=session,
    )
    before = _document_snapshot(document)
    body_closure: tuple[object, ...]
    if body is None:
        body, body_closure = _create_reviewed_body(document, checked, before)
    else:
        body_closure = _validate_body_helpers(document, body)
    base = None if authenticated_base is None else authenticated_base.object
    try:
        receipt = apply_partdesign_primitive_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
            bindings=PartDesignPrimitiveExecutionBindings(
                document=document,
                body=body,
                body_id=checked.body_id,
                base=authenticated_base,
            ),
        )
        result = document.getObject(receipt.object_name)
        ownership = PartDesignPrimitiveOwnershipClosure(
            invariant=PARTDESIGN_PRIMITIVE_RESULT_INVARIANTS[checked.operation],
            native_receipt=receipt,
            object=result,
            body=body,
            base=base,
            body_closure=body_closure,
            created_body=base is None,
            base_shape_sha256=None if base is None else _shape_sha256(base),
            result_shape_sha256=_shape_sha256(result),
        )
        ownership.validate_native_result(document, result)
        owned = ownership.owned_objects(result)
        contract.closure_for_sources(source_count).validate_owned(result, owned)
        expected_added = (*body_closure, result) if base is None else (result,)
        added = _added_objects(document, before)
        if len(added) != len(expected_added) or any(
            item is not expected for item, expected in zip(added, expected_added, strict=True)
        ):
            _integrity_failure()
    except KeyboardInterrupt:
        _restore_document(document, before)
        raise
    except BaseException:
        _restore_document(document, before)
        _execution_failure()

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(
        object=result,
        receipt=ownership,
        owned_objects=owned,
    )


def execute_partdesign_primitive_reviewed_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyExecutionContext,
    )

    if (
        document is None
        or type(payload) is not bytes
        or type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
    ):
        _integrity_failure()
    return execute_partdesign_primitive_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


@dataclass(frozen=True, slots=True)
class PartDesignPrimitiveReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]
    product_contracts: MappingProxyType


PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC: Final = PartDesignPrimitiveReviewedFamilySpec(
    manifest=PARTDESIGN_PRIMITIVE_MANIFEST,
    subject_type_term=_bridge_term(PRIMITIVE_STRUCTURE_TERM),
    operation_ids=tuple(item.value for item in PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_OPERATIONS),
    adapter_factory=partdesign_primitive_reviewed_adapter_factory,
    validate_plan=validate_partdesign_primitive_reviewed_plan,
    execute_plan=execute_partdesign_primitive_reviewed_plan,
    product_contracts=PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS,
)


__all__ = [
    "PARTDESIGN_PRIMITIVE_MANIFEST",
    "PARTDESIGN_PRIMITIVE_OPERATION_SPECS",
    "PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS",
    "PARTDESIGN_PRIMITIVE_RESULT_INVARIANTS",
    "PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC",
    "PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_IDENTITIES",
    "PARTDESIGN_PRIMITIVE_REVIEWED_PRODUCT_OPERATIONS",
    "PartDesignPrimitiveOwnedClosureContract",
    "PartDesignPrimitiveOwnershipClosure",
    "PartDesignPrimitiveProductContract",
    "PartDesignPrimitiveResultInvariant",
    "PartDesignPrimitiveReviewedFamilySpec",
    "PartDesignReviewedBaseBinding",
    "execute_partdesign_primitive_reviewed_plan",
    "execute_partdesign_primitive_reviewed_plan_with_sources",
    "partdesign_primitive_reviewed_adapter_factory",
    "resolve_partdesign_primitive_reviewed_operation",
    "validate_partdesign_primitive_reviewed_plan",
]
