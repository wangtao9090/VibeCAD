"""Private product callbacks for the reviewed Sketcher family.

The existing reviewed Sketch rules do not create a ``Sketcher::SketchObject``.
All twenty operations update one already-managed sketch in place.  This module
therefore contributes the family-owned pieces needed by the shared
``UPDATE_PRIMARY`` seam: exact route/plan binding, authenticated source use,
content-bound result adoption, and a reversible opaque snapshot.  It does not
register routes or expose native geometry/constraint indices to the model.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge import freecad_sketch_intent_adapter as sketch_adapter
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_sketch_intent_adapter import (
    REVIEWED_SKETCH_FAMILY_MANIFEST,
    FreeCADReviewedSketchAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import SKETCH_ROOT_SEMANTIC_TYPE_TERM
from vibecad.parametric import freecad_sketch_intent_rules as sketch_rules
from vibecad.parametric.freecad_sketch_intent_rules import (
    REVIEWED_SKETCH_NATIVE_TYPE_ID,
    ReviewedSketchBackendPlan,
    ReviewedSketchConformanceReceipt,
    ReviewedSketchExecutionBindings,
    ReviewedSketchOperation,
    apply_reviewed_sketch_plan,
    decode_reviewed_sketch_backend_plan,
)
from vibecad.validation import EntityObservation

_STATE_DIGEST_DOMAIN = b"vibecad.reviewed-sketch-product-state.v1\0"
_SHAPE_DIGEST_DOMAIN = b"vibecad.reviewed-sketch-null-shape.v1\0"
_METADATA_PROPERTY = "VibeCADReviewedSketchIntent"


def _integrity_failure() -> None:
    # Lazy imports keep shared-dispatcher -> family initialization acyclic.
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


def _semantic_operation(operation: ReviewedOperationSpec) -> str:
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    return f"{namespace}/{version}/{term_id}@{digest}"


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _canonical_sha256(value: object) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _integrity_failure()
    if len(payload) > 512 * 1024:
        _integrity_failure()
    return hashlib.sha256(_STATE_DIGEST_DOMAIN + payload).hexdigest()


def _finite(value: object) -> float:
    if type(value) not in {int, float}:
        _integrity_failure()
    result = float(value)
    if not math.isfinite(result):
        _integrity_failure()
    result = round(result, 9)
    return 0.0 if result == 0.0 else result


def _bridge_term(term: BridgeTermRef) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


REVIEWED_SKETCH_PRODUCT_OPERATIONS: Final = tuple(ReviewedSketchOperation)
REVIEWED_SKETCH_CREATE_OPERATIONS: Final[tuple[ReviewedSketchOperation, ...]] = ()
REVIEWED_SKETCH_UPDATE_PRIMARY_OPERATIONS: Final = REVIEWED_SKETCH_PRODUCT_OPERATIONS
REVIEWED_SKETCH_SHARED_REGISTRATION_READY: Final = False
REVIEWED_SKETCH_SHARED_REGISTRATION_BLOCKERS: Final = (
    "manifest-missing-sketch-root-semantic-type-term",
    "generic-product-wire-is-pfg-v2-not-sketch-intent-graph",
    "generic-proof-selector-is-pfg-feature-not-sketch-geometry-or-constraint",
    "no-reviewed-sketch-object-create-producer",
)

# The verified 1.0.0 manifest above remains immutable.  This compatibility
# manifest adds only the codec-owned root semantic type required by the shared
# intent-binding descriptor.  It deliberately has a distinct family version
# and digest, and it does not claim a new verification receipt.
REVIEWED_SKETCH_REGISTRATION_MANIFEST: Final = FamilyBatchManifest(
    family_id=REVIEWED_SKETCH_FAMILY_MANIFEST.family_id,
    family_version="1.0.1",
    adapter=REVIEWED_SKETCH_FAMILY_MANIFEST.adapter,
    backend_engine=REVIEWED_SKETCH_FAMILY_MANIFEST.backend_engine,
    backend_version=REVIEWED_SKETCH_FAMILY_MANIFEST.backend_version,
    backend_build_id=REVIEWED_SKETCH_FAMILY_MANIFEST.backend_build_id,
    rule_id=REVIEWED_SKETCH_FAMILY_MANIFEST.rule_id,
    rule_contract_sha256=REVIEWED_SKETCH_FAMILY_MANIFEST.rule_contract_sha256,
    intent_role_term=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_role_term,
    intent_schema_term=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_schema_term,
    intent_media_type=REVIEWED_SKETCH_FAMILY_MANIFEST.intent_media_type,
    capability_role_term=REVIEWED_SKETCH_FAMILY_MANIFEST.capability_role_term,
    capability_schema_term=REVIEWED_SKETCH_FAMILY_MANIFEST.capability_schema_term,
    capability_media_type=REVIEWED_SKETCH_FAMILY_MANIFEST.capability_media_type,
    plan_role_term=REVIEWED_SKETCH_FAMILY_MANIFEST.plan_role_term,
    plan_schema_term=REVIEWED_SKETCH_FAMILY_MANIFEST.plan_schema_term,
    plan_media_type=REVIEWED_SKETCH_FAMILY_MANIFEST.plan_media_type,
    request_terms=(
        *REVIEWED_SKETCH_FAMILY_MANIFEST.request_terms,
        _bridge_term(SKETCH_ROOT_SEMANTIC_TYPE_TERM),
    ),
    operations=REVIEWED_SKETCH_FAMILY_MANIFEST.operations,
    max_plan_bytes=REVIEWED_SKETCH_FAMILY_MANIFEST.max_plan_bytes,
)
REVIEWED_SKETCH_REGISTRATION_MATERIAL_READY: Final = True
REVIEWED_SKETCH_REGISTRATION_MANIFEST_HAS_VERIFICATION_RECEIPT: Final = False
REVIEWED_SKETCH_PUBLIC_POSITIVE_READY: Final = False
REVIEWED_SKETCH_PUBLIC_POSITIVE_BLOCKERS: Final = ("no-reviewed-sketch-object-create-producer",)

_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in REVIEWED_SKETCH_FAMILY_MANIFEST.operations}
)
_PRODUCT_IDENTITIES: Final = MappingProxyType(
    {
        (
            f"{REVIEWED_SKETCH_FAMILY_MANIFEST.family_id}.{operation.value}",
            _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
        ): _OPERATIONS_BY_ID[operation.value]
        for operation in REVIEWED_SKETCH_PRODUCT_OPERATIONS
    }
)
REVIEWED_SKETCH_PRODUCT_IDENTITIES: Final = tuple(_PRODUCT_IDENTITIES)


class FreeCADReviewedSketchRegistrationAdapter(ExactReviewedFamilyAdapter):
    """Exact lowerer bound to the compatibility registration manifest."""

    __slots__ = ()

    def __init__(self, sink: PlanSink) -> None:
        super().__init__(
            REVIEWED_SKETCH_REGISTRATION_MANIFEST,
            sink,
            build_plan=sketch_adapter._build_plan,  # noqa: SLF001
            decode_plan=decode_reviewed_sketch_backend_plan,
            validate_binding=sketch_adapter._validate_binding,  # noqa: SLF001
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchRegistrationProductResult:
    """Family-local material for one shared UPDATE_PRIMARY contract."""

    operation_id: str
    result_kind: str = "reference"
    owned_type_ids: tuple[str, ...] = (REVIEWED_SKETCH_NATIVE_TYPE_ID,)
    semantic_roles: tuple[SemanticRole, ...] = (SemanticRole.FEATURE,)
    source_count: int = 1
    execution_mode: str = "update_primary"
    primary_is_source: bool = True

    def __post_init__(self) -> None:
        operation = next(
            (
                item
                for item in REVIEWED_SKETCH_REGISTRATION_MANIFEST.operations
                if item.operation_id == self.operation_id
            ),
            None,
        )
        if (
            operation is None
            or operation.native_type_id != REVIEWED_SKETCH_NATIVE_TYPE_ID
            or self.result_kind != "reference"
            or self.owned_type_ids != (REVIEWED_SKETCH_NATIVE_TYPE_ID,)
            or self.semantic_roles != (SemanticRole.FEATURE,)
            or self.source_count != 1
            or self.execution_mode != "update_primary"
            or self.primary_is_source is not True
        ):
            _integrity_failure()


REVIEWED_SKETCH_REGISTRATION_PRODUCT_RESULTS: Final = tuple(
    ReviewedSketchRegistrationProductResult(operation_id=operation.value)
    for operation in REVIEWED_SKETCH_PRODUCT_OPERATIONS
)


class ReviewedSketchOwnerKind(StrEnum):
    DOCUMENT_ROOT = "document_root"
    PARTDESIGN_BODY = "partdesign_body"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchOwnerClosure:
    kind: ReviewedSketchOwnerKind
    body: object | None = field(default=None, repr=False, compare=False)
    body_name: str | None = None
    body_group_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not ReviewedSketchOwnerKind:
            _integrity_failure()
        if self.kind is ReviewedSketchOwnerKind.DOCUMENT_ROOT:
            if self.body is not None or self.body_name is not None or self.body_group_names:
                _integrity_failure()
            return
        if (
            self.body is None
            or type(self.body_name) is not str
            or not self.body_name
            or type(self.body_group_names) is not tuple
            or not self.body_group_names
            or any(type(item) is not str or not item for item in self.body_group_names)
        ):
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchShapeFacts:
    is_null: bool
    is_valid: bool
    shape_type: str | None
    vertex_count: int
    edge_count: int
    wire_count: int
    face_count: int
    solid_count: int
    open_vertex_count: int
    result_shape_sha256: str
    closed_profile: bool

    def __post_init__(self) -> None:
        counts = (
            self.vertex_count,
            self.edge_count,
            self.wire_count,
            self.face_count,
            self.solid_count,
            self.open_vertex_count,
        )
        if (
            type(self.is_null) is not bool
            or type(self.is_valid) is not bool
            or (self.shape_type is not None and type(self.shape_type) is not str)
            or any(type(item) is not int or not 0 <= item <= 1_000_000 for item in counts)
            or not _is_sha256(self.result_shape_sha256)
            or type(self.closed_profile) is not bool
            or (
                self.closed_profile
                and (
                    self.is_null
                    or not self.is_valid
                    or self.wire_count != 1
                    or self.edge_count < 1
                    or self.face_count != 0
                    or self.solid_count != 0
                    or self.open_vertex_count != 0
                )
            )
        ):
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchOpaqueState:
    """Process-local native values retained only until adoption commits."""

    document: object = field(repr=False, compare=False)
    sketch: object = field(repr=False, compare=False)
    document_objects: tuple[object, ...] = field(repr=False, compare=False)
    owner: ReviewedSketchOwnerClosure = field(repr=False, compare=False)
    sketch_id: str
    shape: ReviewedSketchShapeFacts
    geometries: tuple[object, ...] = field(repr=False, compare=False)
    constraints: tuple[object, ...] = field(repr=False, compare=False)
    construction: tuple[bool, ...]
    constraint_names: tuple[str, ...]
    constraint_active: tuple[bool, ...]
    metadata_present: bool
    metadata_value: str | None = field(repr=False)
    state_sha256: str

    def __post_init__(self) -> None:
        if (
            self.document is None
            or self.sketch is None
            or type(self.document_objects) is not tuple
            or not self.document_objects
            or type(self.owner) is not ReviewedSketchOwnerClosure
            or type(self.sketch_id) is not str
            or not self.sketch_id
            or type(self.shape) is not ReviewedSketchShapeFacts
            or type(self.geometries) is not tuple
            or type(self.constraints) is not tuple
            or type(self.construction) is not tuple
            or len(self.construction) != len(self.geometries)
            or any(type(item) is not bool for item in self.construction)
            or type(self.constraint_names) is not tuple
            or len(self.constraint_names) != len(self.constraints)
            or any(type(item) is not str for item in self.constraint_names)
            or type(self.constraint_active) is not tuple
            or len(self.constraint_active) != len(self.constraints)
            or any(type(item) is not bool for item in self.constraint_active)
            or type(self.metadata_present) is not bool
            or (
                self.metadata_present
                and (type(self.metadata_value) is not str or not self.metadata_value)
            )
            or (not self.metadata_present and self.metadata_value is not None)
            or not _is_sha256(self.state_sha256)
        ):
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchPrimaryUpdateSnapshot:
    """Standalone equivalent of the future shared UPDATE_PRIMARY snapshot."""

    primary: object = field(repr=False, compare=False)
    owned_objects: tuple[object, ...] = field(repr=False, compare=False)
    state_sha256: str
    rollback_state: ReviewedSketchOpaqueState = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self.primary is None
            or len(self.owned_objects) != 1
            or self.owned_objects[0] is not self.primary
            or not _is_sha256(self.state_sha256)
            or type(self.rollback_state) is not ReviewedSketchOpaqueState
            or self.rollback_state.sketch is not self.primary
            or not hmac.compare_digest(self.state_sha256, self.rollback_state.state_sha256)
        ):
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchOwnershipClosure:
    """Current shape/state and ownership bound to one native rule receipt."""

    native_receipt: ReviewedSketchConformanceReceipt
    owner: ReviewedSketchOwnerClosure
    shape: ReviewedSketchShapeFacts
    state_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.native_receipt) is not ReviewedSketchConformanceReceipt
            or type(self.owner) is not ReviewedSketchOwnerClosure
            or type(self.shape) is not ReviewedSketchShapeFacts
            or not _is_sha256(self.state_sha256)
        ):
            _integrity_failure()

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def receipt_sha256(self) -> str:
        return self.native_receipt.receipt_sha256

    @property
    def sketch_id(self) -> str:
        return self.native_receipt.sketch_id

    @property
    def object_name(self) -> str:
        return self.native_receipt.sketch_object_name

    @property
    def result_shape_sha256(self) -> str:
        return self.shape.result_shape_sha256

    @property
    def closed_profile(self) -> bool:
        return self.shape.closed_profile

    def validate_native_result(self, document: object, sketch: object) -> None:
        captured = capture_reviewed_sketch_native_state(
            document,
            sketch,
            sketch_id=self.sketch_id,
        )
        if (
            sketch.Name != self.object_name
            or not _same_owner(captured.owner, self.owner)
            or captured.shape != self.shape
            or not hmac.compare_digest(captured.state_sha256, self.state_sha256)
        ):
            _integrity_failure()

    def validate_profile_source(self, document: object, sketch: object) -> None:
        self.validate_native_result(document, sketch)
        if (
            not self.closed_profile
            or self.owner.kind is not ReviewedSketchOwnerKind.PARTDESIGN_BODY
        ):
            _integrity_failure()

    def validate_adoption(self, document: object, sketch: object, observation: object) -> None:
        self.validate_native_result(document, sketch)
        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != REVIEWED_SKETCH_NATIVE_TYPE_ID
            or observation.semantic_role != SemanticRole.FEATURE.value
            or (
                self.closed_profile
                and (observation.valid_shape is not True or observation.solid_count != 0)
            )
        ):
            _integrity_failure()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewedSketchBoundExecution:
    object: object = field(repr=False, compare=False)
    receipt: ReviewedSketchOwnershipClosure
    state_sha256: str

    def __post_init__(self) -> None:
        if (
            self.object is None
            or type(self.receipt) is not ReviewedSketchOwnershipClosure
            or not hmac.compare_digest(self.receipt.state_sha256, self.state_sha256)
        ):
            _integrity_failure()


def resolve_reviewed_sketch_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve only one complete static identity; aliases remain inert."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    return _PRODUCT_IDENTITIES.get((operation_id, semantic_operation))


def reviewed_sketch_adapter_factory(sink: PlanSink) -> ExactReviewedFamilyAdapter:
    """Expose the existing exact lowerer, not the non-executable facade."""

    return FreeCADReviewedSketchAdapter(sink)._inner  # noqa: SLF001


def reviewed_sketch_registration_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    """Build the exact adapter bound to the compatibility manifest."""

    return FreeCADReviewedSketchRegistrationAdapter(sink)


def reviewed_sketch_registration_intent_binding() -> object:
    """Return the shared codec/selector binding at the explicit handoff."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _sketch_intent_binding,
    )

    binding = _sketch_intent_binding()
    try:
        valid = (
            binding.root_subject_type_term == SKETCH_ROOT_SEMANTIC_TYPE_TERM
            and binding.schema_term == REVIEWED_SKETCH_REGISTRATION_MANIFEST.intent_schema_term
            and binding.media_type == REVIEWED_SKETCH_REGISTRATION_MANIFEST.intent_media_type
            and all(
                binding.subject_type_for(operation) == operation.semantic_term
                and operation.semantic_term in REVIEWED_SKETCH_REGISTRATION_MANIFEST.request_terms
                for operation in REVIEWED_SKETCH_REGISTRATION_MANIFEST.operations
            )
        )
    except (Exception, SystemExit):
        valid = False
    if not valid:
        _integrity_failure()
    return binding


def reviewed_sketch_registration_product_results() -> tuple[object, ...]:
    """Materialize exact private shared contracts without an import cycle."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedProductExecutionMode,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    return tuple(
        _ReviewedProductResultContract(
            operation_id=item.operation_id,
            result_kind=_ReviewedProductResultKind.REFERENCE,
            owned_type_ids=item.owned_type_ids,
            semantic_roles=item.semantic_roles,
            source_count=item.source_count,
            execution_mode=_ReviewedProductExecutionMode.UPDATE_PRIMARY,
        )
        for item in REVIEWED_SKETCH_REGISTRATION_PRODUCT_RESULTS
    )


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    *,
    manifest: FamilyBatchManifest = REVIEWED_SKETCH_FAMILY_MANIFEST,
) -> ReviewedSketchBackendPlan:
    if (
        type(plan) is not ReviewedSketchBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or type(manifest) is not FamilyBatchManifest
        or manifest
        not in (
            REVIEWED_SKETCH_FAMILY_MANIFEST,
            REVIEWED_SKETCH_REGISTRATION_MANIFEST,
        )
        or operation not in manifest.operations
        or plan.operation not in REVIEWED_SKETCH_PRODUCT_OPERATIONS
        or plan.operation.value != operation.operation_id
        or plan.adapter_contract_sha256 != manifest.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != manifest.manifest_sha256
        or plan.operation_specification_sha256 != operation.specification_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    try:
        decoded = decode_reviewed_sketch_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_reviewed_sketch_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    if (
        type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != REVIEWED_SKETCH_FAMILY_MANIFEST.manifest_sha256
        or receipt.adapter != REVIEWED_SKETCH_FAMILY_MANIFEST.adapter
    ):
        _integrity_failure()
    checked = _validate_plan_contract(
        plan,
        receipt.plan_document,
        operation,
        manifest=REVIEWED_SKETCH_FAMILY_MANIFEST,
    )
    if (
        checked.request_digest != receipt.request_digest
        or checked.source_artifact_id != receipt.source_document.artifact_id
        or checked.source_graph_id != receipt.source_document.document_id
        or checked.source_graph_sha256 != receipt.source_document.document_digest
        or checked.source_content_sha256 != receipt.source_document.content_sha256
    ):
        _integrity_failure()


def validate_reviewed_sketch_registration_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind one compatibility plan to its exact registration receipt."""

    if (
        type(receipt) is not ReviewedPlanReceipt
        or type(operation) is not ReviewedOperationSpec
        or receipt.operation != operation
        or receipt.manifest_sha256 != REVIEWED_SKETCH_REGISTRATION_MANIFEST.manifest_sha256
        or receipt.adapter != REVIEWED_SKETCH_REGISTRATION_MANIFEST.adapter
    ):
        _integrity_failure()
    checked = _validate_plan_contract(
        plan,
        receipt.plan_document,
        operation,
        manifest=REVIEWED_SKETCH_REGISTRATION_MANIFEST,
    )
    if (
        checked.request_digest != receipt.request_digest
        or checked.source_artifact_id != receipt.source_document.artifact_id
        or checked.source_graph_id != receipt.source_document.document_id
        or checked.source_graph_sha256 != receipt.source_document.document_digest
        or checked.source_content_sha256 != receipt.source_document.content_sha256
    ):
        _integrity_failure()


def _object_sequence(document: object) -> tuple[object, ...]:
    try:
        values = tuple(document.Objects)
    except (Exception, SystemExit):
        _integrity_failure()
    if not values or len({id(item) for item in values}) != len(values):
        _integrity_failure()
    return values


def _owner_closure(
    document: object,
    sketch: object,
    objects: tuple[object, ...],
) -> ReviewedSketchOwnerClosure:
    try:
        parent = sketch.getParentGeoFeatureGroup()
    except (AttributeError, TypeError):
        parent = None
    except (Exception, SystemExit):
        _integrity_failure()
    bodies = tuple(item for item in objects if getattr(item, "TypeId", None) == "PartDesign::Body")
    containing = tuple(body for body in bodies if any(sketch is item for item in tuple(body.Group)))
    if parent is None and not containing:
        return ReviewedSketchOwnerClosure(kind=ReviewedSketchOwnerKind.DOCUMENT_ROOT)
    if (
        len(containing) != 1
        or parent is not containing[0]
        or getattr(parent, "Document", None) is not document
        or document.getObject(parent.Name) is not parent
    ):
        _integrity_failure()
    group = tuple(parent.Group)
    return ReviewedSketchOwnerClosure(
        kind=ReviewedSketchOwnerKind.PARTDESIGN_BODY,
        body=parent,
        body_name=parent.Name,
        body_group_names=tuple(item.Name for item in group),
    )


def _shape_facts(sketch: object) -> ReviewedSketchShapeFacts:
    try:
        shape = sketch.Shape
        is_null = bool(shape.isNull())
        is_valid = bool(shape.isValid()) if not is_null else False
        shape_type = None if is_null else str(shape.ShapeType)
        vertex_count = len(shape.Vertexes)
        edge_count = len(shape.Edges)
        wires = tuple(shape.Wires)
        face_count = len(shape.Faces)
        solid_count = len(shape.Solids)
        open_vertex_count = len(tuple(sketch.OpenVertices))
        if is_null:
            digest = hashlib.sha256(_SHAPE_DIGEST_DOMAIN).hexdigest()
        else:
            raw = shape.exportBrepToString().encode("utf-8")
            if not raw:
                _integrity_failure()
            digest = hashlib.sha256(raw).hexdigest()
        closed_profile = (
            not is_null
            and is_valid
            and len(wires) == 1
            and edge_count >= 1
            and face_count == 0
            and solid_count == 0
            and open_vertex_count == 0
            and wires[0].isClosed() is True
        )
    except (Exception, SystemExit, UnicodeError, OverflowError):
        _integrity_failure()
    return ReviewedSketchShapeFacts(
        is_null=is_null,
        is_valid=is_valid,
        shape_type=shape_type,
        vertex_count=vertex_count,
        edge_count=edge_count,
        wire_count=len(wires),
        face_count=face_count,
        solid_count=solid_count,
        open_vertex_count=open_vertex_count,
        result_shape_sha256=digest,
        closed_profile=closed_profile,
    )


def _placement_fingerprint(value: object) -> tuple[float, ...]:
    try:
        matrix = value.toMatrix()
        fields = tuple(
            _finite(getattr(matrix, name))
            for name in (
                "A11",
                "A12",
                "A13",
                "A14",
                "A21",
                "A22",
                "A23",
                "A24",
                "A31",
                "A32",
                "A33",
                "A34",
                "A41",
                "A42",
                "A43",
                "A44",
            )
        )
    except (Exception, SystemExit):
        _integrity_failure()
    return fields


def _support_fingerprint(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    try:
        items = tuple(value)
        entries = (
            (items,)
            if len(items) == 2 and hasattr(items[0], "Name") and type(items[1]) in {list, tuple}
            else items
        )
        result = tuple(
            (
                item[0].Name,
                tuple(item[1]),
            )
            for item in entries
        )
    except (Exception, SystemExit, TypeError):
        _integrity_failure()
    if any(
        type(name) is not str
        or not name
        or type(labels) is not tuple
        or any(type(label) is not str or not label for label in labels)
        for name, labels in result
    ):
        _integrity_failure()
    return result


def _native_copy(value: object) -> object:
    try:
        copier = getattr(value, "copy", None)
        cloned = copier() if callable(copier) else copy.copy(value)
    except (Exception, SystemExit):
        _integrity_failure()
    if cloned is None or cloned is value:
        _integrity_failure()
    return cloned


def capture_reviewed_sketch_native_state(
    document: object,
    sketch: object,
    *,
    sketch_id: str,
) -> ReviewedSketchOpaqueState:
    """Capture one exact process-local rollback capsule and stable state digest."""

    try:
        objects = _object_sequence(document)
        if (
            type(sketch_id) is not str
            or not sketch_id
            or getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or sketch.Document is not document
            or sketch.TypeId != REVIEWED_SKETCH_NATIVE_TYPE_ID
            or document.getObject(sketch.Name) is not sketch
            or not any(sketch is item for item in objects)
            or not sketch.isValid()
            or tuple(sketch.State) != ("Up-to-date",)
        ):
            _integrity_failure()
        _, dof, fully_constrained = sketch_rules._stabilized_solver_facts(  # noqa: SLF001
            document,
            sketch,
        )
        metadata, _ = sketch_rules._validated_metadata(sketch, sketch_id)  # noqa: SLF001
        native_signature = sketch_rules._native_state_signature(sketch)  # noqa: SLF001
        geometry = tuple(sketch.Geometry)
        constraints = tuple(sketch.Constraints)
        construction = tuple(bool(sketch.getConstruction(index)) for index in range(len(geometry)))
        constraint_names = tuple(item.Name for item in constraints)
        if any(type(item) is not str for item in constraint_names):
            _integrity_failure()
        constraint_active = tuple(
            bool(sketch.getActive(index)) for index in range(len(constraints))
        )
        properties = tuple(sketch.PropertiesList)
        metadata_present = _METADATA_PROPERTY in properties
        metadata_value = getattr(sketch, _METADATA_PROPERTY) if metadata_present else None
        if metadata_present and type(metadata_value) is not str:
            _integrity_failure()
        metadata_sha256 = hashlib.sha256(
            json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        ).hexdigest()
        owner = _owner_closure(document, sketch, objects)
        shape = _shape_facts(sketch)
        map_mode = str(getattr(sketch, "MapMode", "Deactivated"))
        support = _support_fingerprint(getattr(sketch, "Support", None))
        placement = _placement_fingerprint(sketch.Placement)
        diagnostics = tuple(
            tuple(getattr(sketch, name))
            for name in (
                "ConflictingConstraints",
                "RedundantConstraints",
                "PartiallyRedundantConstraints",
                "MalformedConstraints",
            )
        )
        if any(item for item in diagnostics):
            _integrity_failure()
        object_facts = tuple((item.Name, item.TypeId) for item in objects)
        state_sha256 = _canonical_sha256(
            {
                "sketch": {
                    "name": sketch.Name,
                    "type_id": sketch.TypeId,
                    "sketch_id": sketch_id,
                    "owner_kind": owner.kind.value,
                    "body_name": owner.body_name,
                    "body_group_names": list(owner.body_group_names),
                    "map_mode": map_mode,
                    "support": [[name, list(labels)] for name, labels in support],
                    "placement": list(placement),
                },
                "document_objects": [list(item) for item in object_facts],
                "native_geometry_sha256s": list(native_signature[0]),
                "native_constraint_sha256s": list(native_signature[1]),
                "construction": list(construction),
                "constraint_names": list(constraint_names),
                "constraint_active": list(constraint_active),
                "solver": {
                    "dof": dof,
                    "fully_constrained": fully_constrained,
                    "diagnostics": [list(item) for item in diagnostics],
                },
                "metadata_present": metadata_present,
                "metadata_sha256": metadata_sha256,
                "shape": {
                    "is_null": shape.is_null,
                    "is_valid": shape.is_valid,
                    "shape_type": shape.shape_type,
                    "vertices": shape.vertex_count,
                    "edges": shape.edge_count,
                    "wires": shape.wire_count,
                    "faces": shape.face_count,
                    "solids": shape.solid_count,
                    "open_vertices": shape.open_vertex_count,
                    "shape_sha256": shape.result_shape_sha256,
                    "closed_profile": shape.closed_profile,
                },
            }
        )
        opaque_geometry = tuple(_native_copy(item) for item in geometry)
        opaque_constraints = tuple(_native_copy(item) for item in constraints)
    except (Exception, SystemExit) as error:
        from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
            ReviewedIntentExecutionError,
        )

        if isinstance(error, ReviewedIntentExecutionError):
            raise
        _integrity_failure()
    return ReviewedSketchOpaqueState(
        document=document,
        sketch=sketch,
        document_objects=objects,
        owner=owner,
        sketch_id=sketch_id,
        shape=shape,
        geometries=opaque_geometry,
        constraints=opaque_constraints,
        construction=construction,
        constraint_names=constraint_names,
        constraint_active=constraint_active,
        metadata_present=metadata_present,
        metadata_value=metadata_value,
        state_sha256=state_sha256,
    )


def _same_object_sequence(actual: object, expected: tuple[object, ...]) -> bool:
    return (
        type(actual) is tuple
        and len(actual) == len(expected)
        and all(item is prior for item, prior in zip(actual, expected, strict=True))
    )


def _same_owner(
    actual: ReviewedSketchOwnerClosure,
    expected: ReviewedSketchOwnerClosure,
) -> bool:
    return actual == expected and actual.body is expected.body


def _restore_opaque_state(state: ReviewedSketchOpaqueState) -> None:
    document, sketch = state.document, state.sketch
    try:
        if (
            bool(document.HasPendingTransaction)
            or not _same_object_sequence(tuple(document.Objects), state.document_objects)
            or sketch.Document is not document
            or document.getObject(sketch.Name) is not sketch
            or not _same_owner(
                _owner_closure(document, sketch, state.document_objects),
                state.owner,
            )
        ):
            _integrity_failure()
        document.openTransaction("VibeCAD rollback reviewed sketch update")
        transaction_open = True
        constraint_count = int(sketch.ConstraintCount)
        if constraint_count:
            sketch.delConstraints(list(range(constraint_count)), False)
        for index in range(int(sketch.GeometryCount) - 1, -1, -1):
            sketch.delGeometry(index)
        for expected_index, (geometry, construction) in enumerate(
            zip(state.geometries, state.construction, strict=True)
        ):
            actual_index = sketch.addGeometry(_native_copy(geometry), construction)
            if actual_index != expected_index:
                _integrity_failure()
        for expected_index, (constraint, name, active) in enumerate(
            zip(
                state.constraints,
                state.constraint_names,
                state.constraint_active,
                strict=True,
            )
        ):
            actual_index = sketch.addConstraint(_native_copy(constraint))
            if actual_index != expected_index:
                _integrity_failure()
            sketch.renameConstraint(actual_index, name)
            if not active:
                sketch.setActive(actual_index, False)
        properties = tuple(sketch.PropertiesList)
        if state.metadata_present:
            if _METADATA_PROPERTY not in properties:
                sketch.addProperty(
                    "App::PropertyString",
                    _METADATA_PROPERTY,
                    "VibeCAD",
                    "Content-bound reviewed SketchIntentGraph bindings",
                )
            setattr(sketch, _METADATA_PROPERTY, state.metadata_value)
            sketch.setEditorMode(_METADATA_PROPERTY, 2)
        elif _METADATA_PROPERTY in properties:
            sketch.removeProperty(_METADATA_PROPERTY)
        document.recompute()
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        try:
            if "transaction_open" in locals() and transaction_open:
                document.abortTransaction()
            document.recompute()
        except BaseException:
            pass
        if isinstance(error, KeyboardInterrupt):
            raise
        _integrity_failure()
    restored = capture_reviewed_sketch_native_state(
        document,
        sketch,
        sketch_id=state.sketch_id,
    )
    if (
        not _same_object_sequence(restored.document_objects, state.document_objects)
        or not _same_owner(restored.owner, state.owner)
        or not hmac.compare_digest(restored.state_sha256, state.state_sha256)
    ):
        _integrity_failure()


def _local_update_snapshot(
    document: object,
    sketch: object,
    *,
    sketch_id: str,
) -> ReviewedSketchPrimaryUpdateSnapshot:
    state = capture_reviewed_sketch_native_state(document, sketch, sketch_id=sketch_id)
    return ReviewedSketchPrimaryUpdateSnapshot(
        primary=sketch,
        owned_objects=(sketch,),
        state_sha256=state.state_sha256,
        rollback_state=state,
    )


def _context_source(document: object, context: object) -> object:
    try:
        if context.document is not document or len(context.source_results) != 1:
            _integrity_failure()
        source = context.source_results[0]
        sketch = source.object
        if (
            sketch is None
            or len(source.owned_objects) != 1
            or source.owned_objects[0] is not sketch
            or getattr(sketch, "TypeId", None) != REVIEWED_SKETCH_NATIVE_TYPE_ID
        ):
            _integrity_failure()
    except (Exception, SystemExit, AttributeError, TypeError):
        _integrity_failure()
    return source


def _source_sketch_id(source: object) -> str:
    receipt = getattr(source, "native_receipt", None)
    value = getattr(receipt, "sketch_id", None)
    if type(value) is not str or not value:
        _integrity_failure()
    return value


def capture_reviewed_sketch_update_state(
    document: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Future shared ``UPDATE_PRIMARY`` capture callback."""

    if operation not in REVIEWED_SKETCH_FAMILY_MANIFEST.operations:
        _integrity_failure()
    source = _context_source(document, context)
    local = _local_update_snapshot(
        document,
        source.object,
        sketch_id=_source_sketch_id(source),
    )
    try:
        from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
            _ReviewedPrimaryUpdateSnapshot,
        )
    except ImportError:
        return local
    return _ReviewedPrimaryUpdateSnapshot(
        primary=local.primary,
        owned_objects=local.owned_objects,
        state_sha256=local.state_sha256,
        rollback_state=local.rollback_state,
    )


def rollback_reviewed_sketch_update_state(
    document: object,
    snapshot: object,
    operation: ReviewedOperationSpec,
    context: object,
) -> None:
    """Restore the family capsule; shared code performs the final digest proof."""

    if operation not in REVIEWED_SKETCH_FAMILY_MANIFEST.operations:
        _integrity_failure()
    source = _context_source(document, context)
    try:
        state = snapshot.rollback_state
        if (
            type(state) is not ReviewedSketchOpaqueState
            or snapshot.primary is not source.object
            or len(snapshot.owned_objects) != 1
            or snapshot.owned_objects[0] is not source.object
            or state.document is not document
            or state.sketch is not source.object
            or not hmac.compare_digest(snapshot.state_sha256, state.state_sha256)
        ):
            _integrity_failure()
    except (Exception, SystemExit, AttributeError, TypeError):
        _integrity_failure()
    _restore_opaque_state(state)


def _authenticated_source(
    document: object,
    plan: ReviewedSketchBackendPlan,
    context: object,
) -> object:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedNativeExecutionResult,
    )

    source = _context_source(document, context)
    if type(source) is not ReviewedNativeExecutionResult:
        _integrity_failure()
    sketch = source.object
    try:
        identity = context.session.read_object_identity(sketch)
        receipt = source.native_receipt
        captured = capture_reviewed_sketch_native_state(
            document,
            sketch,
            sketch_id=plan.sketch_id,
        )
        valid = (
            type(identity) is EntityIdentity
            and context.session.doc is document
            and any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
            and source.route.operation.native_type_id == REVIEWED_SKETCH_NATIVE_TYPE_ID
            and source.semantic_roles == (SemanticRole.FEATURE,)
            and identity.object_type == REVIEWED_SKETCH_NATIVE_TYPE_ID
            and identity.feature_id is not None
            and identity.semantic_role is SemanticRole.FEATURE
            and identity.provenance.source is ProvenanceSource.MODEL
            and identity.provenance.operation_id is not None
            and getattr(receipt, "sketch_id", None) == plan.sketch_id
            and getattr(receipt, "object_name", getattr(receipt, "sketch_object_name", None))
            == sketch.Name
            and _is_sha256(source.state_sha256)
            and hmac.compare_digest(source.state_sha256, captured.state_sha256)
        )
    except (Exception, SystemExit, AttributeError, TypeError, ValueError, KeyError):
        valid = False
    if not valid:
        _integrity_failure()
    return sketch


def execute_reviewed_sketch_plan_on_bound_sketch(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    sketch: object,
    *,
    manifest: FamilyBatchManifest = REVIEWED_SKETCH_FAMILY_MANIFEST,
) -> ReviewedSketchBoundExecution:
    """Pure family hook after the engine has authenticated one source sketch."""

    checked = _validate_plan_contract(
        plan,
        plan_document,
        operation,
        manifest=manifest,
    )
    if document is None or type(payload) is not bytes:
        _integrity_failure()
    try:
        decoded = decode_reviewed_sketch_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    before = capture_reviewed_sketch_native_state(
        document,
        sketch,
        sketch_id=checked.sketch_id,
    )
    receipt = apply_reviewed_sketch_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=ReviewedSketchExecutionBindings(
            document=document,
            sketch=sketch,
            sketch_id=checked.sketch_id,
        ),
    )
    after = capture_reviewed_sketch_native_state(
        document,
        sketch,
        sketch_id=checked.sketch_id,
    )
    geometry_delta = {
        ReviewedSketchOperation.POINT: 1,
        ReviewedSketchOperation.LINE: 1,
        ReviewedSketchOperation.CIRCLE: 1,
        ReviewedSketchOperation.ARC: 1,
        ReviewedSketchOperation.SLOT: 4,
    }.get(checked.operation, 0)
    constraint_delta = (
        5 if checked.operation is ReviewedSketchOperation.SLOT else (0 if geometry_delta else 1)
    )
    expected_geometry_indices = tuple(
        range(len(before.geometries), len(before.geometries) + geometry_delta)
    )
    expected_constraint_indices = tuple(
        range(len(before.constraints), len(before.constraints) + constraint_delta)
    )
    if (
        type(receipt) is not ReviewedSketchConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.plan_sha256 != checked.plan_sha256
        or receipt.sketch_object_name != sketch.Name
        or receipt.sketch_id != checked.sketch_id
        or receipt.node_id != checked.node_id
        or receipt.node_sha256 != checked.node_sha256
        or receipt.geometry_indices != expected_geometry_indices
        or receipt.constraint_indices != expected_constraint_indices
        or len(after.geometries) != len(before.geometries) + geometry_delta
        or len(after.constraints) != len(before.constraints) + constraint_delta
        or not _same_object_sequence(after.document_objects, before.document_objects)
        or not _same_owner(after.owner, before.owner)
        or hmac.compare_digest(after.state_sha256, before.state_sha256)
    ):
        _integrity_failure()
    closure = ReviewedSketchOwnershipClosure(
        native_receipt=receipt,
        owner=after.owner,
        shape=_shape_facts(sketch),
        state_sha256=after.state_sha256,
    )
    closure.validate_native_result(document, sketch)
    return ReviewedSketchBoundExecution(
        object=sketch,
        receipt=closure,
        state_sha256=after.state_sha256,
    )


def execute_reviewed_sketch_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Shared-descriptor callback; public execution remains source-gated."""

    checked = _validate_plan_contract(plan, plan_document, operation)
    sketch = _authenticated_source(document, checked, context)
    result = execute_reviewed_sketch_plan_on_bound_sketch(
        document,
        checked,
        payload,
        plan_document,
        operation,
        sketch,
    )
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    fields = getattr(_ReviewedFamilyNativeExecution, "__dataclass_fields__", {})
    kwargs: dict[str, object] = {
        "object": result.object,
        "receipt": result.receipt,
        "owned_objects": (result.object,),
    }
    if "state_sha256" in fields:
        kwargs["state_sha256"] = result.state_sha256
    return _ReviewedFamilyNativeExecution(**kwargs)


def execute_reviewed_sketch_registration_plan(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    context: object,
) -> object:
    """Compatibility-manifest callback; still requires one managed Sketch."""

    checked = _validate_plan_contract(
        plan,
        plan_document,
        operation,
        manifest=REVIEWED_SKETCH_REGISTRATION_MANIFEST,
    )
    sketch = _authenticated_source(document, checked, context)
    result = execute_reviewed_sketch_plan_on_bound_sketch(
        document,
        checked,
        payload,
        plan_document,
        operation,
        sketch,
        manifest=REVIEWED_SKETCH_REGISTRATION_MANIFEST,
    )
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(
        object=result.object,
        receipt=result.receipt,
        owned_objects=(result.object,),
        state_sha256=result.state_sha256,
    )


@dataclass(frozen=True, slots=True)
class ReviewedSketchFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    create_operation_ids: tuple[str, ...]
    update_primary_operation_ids: tuple[str, ...]
    minimum_sources: int
    maximum_sources: int
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]
    capture_update_state: Callable[[object, ReviewedOperationSpec, object], object]
    rollback_update_state: Callable[[object, object, ReviewedOperationSpec, object], None]


REVIEWED_SKETCH_FAMILY_SPEC: Final = ReviewedSketchFamilySpec(
    manifest=REVIEWED_SKETCH_FAMILY_MANIFEST,
    subject_type_term=_bridge_term(SKETCH_ROOT_SEMANTIC_TYPE_TERM),
    operation_ids=tuple(item.value for item in REVIEWED_SKETCH_PRODUCT_OPERATIONS),
    create_operation_ids=(),
    update_primary_operation_ids=tuple(item.value for item in REVIEWED_SKETCH_PRODUCT_OPERATIONS),
    minimum_sources=1,
    maximum_sources=1,
    adapter_factory=reviewed_sketch_adapter_factory,
    validate_plan=validate_reviewed_sketch_plan,
    execute_plan=execute_reviewed_sketch_plan,
    capture_update_state=capture_reviewed_sketch_update_state,
    rollback_update_state=rollback_reviewed_sketch_update_state,
)


@dataclass(frozen=True, slots=True)
class ReviewedSketchRegistrationSpec:
    """Complete, unregistered handoff material for the shared descriptor."""

    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    operation_ids: tuple[str, ...]
    product_identities: tuple[tuple[str, str], ...]
    create_operation_ids: tuple[str, ...]
    update_primary_operation_ids: tuple[str, ...]
    minimum_sources: int
    maximum_sources: int
    product_results: tuple[ReviewedSketchRegistrationProductResult, ...]
    adapter_factory: Callable[[PlanSink], ExactReviewedFamilyAdapter]
    validate_plan: Callable[[object, ReviewedPlanReceipt, ReviewedOperationSpec], None]
    execute_plan: Callable[
        [object, object, bytes, DocumentRef, ReviewedOperationSpec, object], object
    ]
    intent_binding_factory: Callable[[], object]
    product_results_factory: Callable[[], tuple[object, ...]]
    capture_update_state: Callable[[object, ReviewedOperationSpec, object], object]
    rollback_update_state: Callable[[object, object, ReviewedOperationSpec, object], None]
    compatibility_manifest_has_verification_receipt: bool
    public_positive_ready: bool
    public_positive_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            self.manifest is not REVIEWED_SKETCH_REGISTRATION_MANIFEST
            or self.subject_type_term != SKETCH_ROOT_SEMANTIC_TYPE_TERM
            or self.operation_ids
            != tuple(item.value for item in REVIEWED_SKETCH_PRODUCT_OPERATIONS)
            or self.product_identities != REVIEWED_SKETCH_PRODUCT_IDENTITIES
            or self.create_operation_ids
            or self.update_primary_operation_ids != self.operation_ids
            or (self.minimum_sources, self.maximum_sources) != (1, 1)
            or self.product_results is not REVIEWED_SKETCH_REGISTRATION_PRODUCT_RESULTS
            or any(
                item.execution_mode != "update_primary"
                or item.source_count != 1
                or item.owned_type_ids != (REVIEWED_SKETCH_NATIVE_TYPE_ID,)
                for item in self.product_results
            )
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
            or not callable(self.intent_binding_factory)
            or not callable(self.product_results_factory)
            or not callable(self.capture_update_state)
            or not callable(self.rollback_update_state)
            or self.compatibility_manifest_has_verification_receipt is not False
            or self.public_positive_ready is not False
            or self.public_positive_blockers != ("no-reviewed-sketch-object-create-producer",)
        ):
            _integrity_failure()


REVIEWED_SKETCH_REGISTRATION_SPEC: Final = ReviewedSketchRegistrationSpec(
    manifest=REVIEWED_SKETCH_REGISTRATION_MANIFEST,
    subject_type_term=_bridge_term(SKETCH_ROOT_SEMANTIC_TYPE_TERM),
    operation_ids=tuple(item.value for item in REVIEWED_SKETCH_PRODUCT_OPERATIONS),
    product_identities=REVIEWED_SKETCH_PRODUCT_IDENTITIES,
    create_operation_ids=(),
    update_primary_operation_ids=tuple(item.value for item in REVIEWED_SKETCH_PRODUCT_OPERATIONS),
    minimum_sources=1,
    maximum_sources=1,
    product_results=REVIEWED_SKETCH_REGISTRATION_PRODUCT_RESULTS,
    adapter_factory=reviewed_sketch_registration_adapter_factory,
    validate_plan=validate_reviewed_sketch_registration_plan,
    execute_plan=execute_reviewed_sketch_registration_plan,
    intent_binding_factory=reviewed_sketch_registration_intent_binding,
    product_results_factory=reviewed_sketch_registration_product_results,
    capture_update_state=capture_reviewed_sketch_update_state,
    rollback_update_state=rollback_reviewed_sketch_update_state,
    compatibility_manifest_has_verification_receipt=(
        REVIEWED_SKETCH_REGISTRATION_MANIFEST_HAS_VERIFICATION_RECEIPT
    ),
    public_positive_ready=REVIEWED_SKETCH_PUBLIC_POSITIVE_READY,
    public_positive_blockers=REVIEWED_SKETCH_PUBLIC_POSITIVE_BLOCKERS,
)


__all__ = [
    "REVIEWED_SKETCH_PUBLIC_POSITIVE_BLOCKERS",
    "REVIEWED_SKETCH_PUBLIC_POSITIVE_READY",
    "REVIEWED_SKETCH_REGISTRATION_MANIFEST",
    "REVIEWED_SKETCH_REGISTRATION_MANIFEST_HAS_VERIFICATION_RECEIPT",
    "REVIEWED_SKETCH_REGISTRATION_MATERIAL_READY",
    "REVIEWED_SKETCH_REGISTRATION_PRODUCT_RESULTS",
    "REVIEWED_SKETCH_REGISTRATION_SPEC",
    "REVIEWED_SKETCH_CREATE_OPERATIONS",
    "REVIEWED_SKETCH_FAMILY_SPEC",
    "REVIEWED_SKETCH_PRODUCT_IDENTITIES",
    "REVIEWED_SKETCH_PRODUCT_OPERATIONS",
    "REVIEWED_SKETCH_SHARED_REGISTRATION_BLOCKERS",
    "REVIEWED_SKETCH_SHARED_REGISTRATION_READY",
    "REVIEWED_SKETCH_UPDATE_PRIMARY_OPERATIONS",
    "FreeCADReviewedSketchRegistrationAdapter",
    "ReviewedSketchBoundExecution",
    "ReviewedSketchFamilySpec",
    "ReviewedSketchOpaqueState",
    "ReviewedSketchOwnerClosure",
    "ReviewedSketchOwnerKind",
    "ReviewedSketchOwnershipClosure",
    "ReviewedSketchPrimaryUpdateSnapshot",
    "ReviewedSketchRegistrationProductResult",
    "ReviewedSketchRegistrationSpec",
    "ReviewedSketchShapeFacts",
    "capture_reviewed_sketch_native_state",
    "capture_reviewed_sketch_update_state",
    "execute_reviewed_sketch_plan",
    "execute_reviewed_sketch_plan_on_bound_sketch",
    "execute_reviewed_sketch_registration_plan",
    "resolve_reviewed_sketch_operation",
    "reviewed_sketch_adapter_factory",
    "reviewed_sketch_registration_adapter_factory",
    "reviewed_sketch_registration_intent_binding",
    "reviewed_sketch_registration_product_results",
    "rollback_reviewed_sketch_update_state",
    "validate_reviewed_sketch_plan",
    "validate_reviewed_sketch_registration_plan",
]
