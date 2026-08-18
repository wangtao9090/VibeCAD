"""Private Reviewed product bridge for the three PartDesign residual intents.

The formal adapter already owns the exact semantic graph and native rule.  This
module adds only the product-side seam: it authenticates retained same-run
results, derives one unambiguous managed Body from their native parent
relations, freezes the current Tip and source shapes before mutation, and
returns a content-bound ownership receipt for the single new Body child.

Hole deliberately remains strict.  Its profile must already be a one-circle
``FlatFace`` Sketch attached to the authenticated base.  The current Sketch
CREATE contract makes an XY-plane profile and therefore cannot satisfy that
precondition by itself; the mismatch is rejected before the native rule runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import EntityIdentity, ProvenanceSource, SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_parametric_adapter import PlanSink
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    PARTDESIGN_RESIDUAL_MANIFEST,
    RESIDUAL_STRUCTURE_TERM,
    FreeCADPartDesignResidualAdapter,
)
from vibecad.intent_bridge.reviewed_family_engine import (
    ExactReviewedFamilyAdapter,
    FamilyBatchManifest,
    ReviewedOperationSpec,
    ReviewedPlanReceipt,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_partdesign_residual_rules import (
    PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS,
    AuthenticatedResidualObject,
    PartDesignResidualBackendPlan,
    PartDesignResidualConformanceReceipt,
    PartDesignResidualExecutionBindings,
    PartDesignResidualOperation,
    apply_partdesign_residual_plan,
    decode_partdesign_residual_backend_plan,
)
from vibecad.validation import EntityObservation

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.partdesign-residual-product-ownership.v1\0"
_RESULT_STATE_DIGEST_DOMAIN = b"vibecad.partdesign-residual-product-state.v1\0"
_FACE = re.compile(r"Face([1-9][0-9]{0,3})\Z")


def _integrity_failure() -> None:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        ReviewedIntentExecutionError,
        ReviewedIntentExecutionErrorCode,
    )

    raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)


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


_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PARTDESIGN_RESIDUAL_MANIFEST.operations}
)
PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS: Final = tuple(PartDesignResidualOperation)
PARTDESIGN_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES: Final = tuple(
    (
        f"{PARTDESIGN_RESIDUAL_MANIFEST.family_id}.{operation.value}",
        _semantic_operation(_OPERATIONS_BY_ID[operation.value]),
    )
    for operation in PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS
)


class PartDesignResidualSourceRole(StrEnum):
    BASE = "base"
    PROFILE = "profile"
    BODY_ANCHOR = "body_anchor"


PARTDESIGN_RESIDUAL_REQUIRED_SOURCE_ROLES: Final = MappingProxyType(
    {
        PartDesignResidualOperation.HOLE.value: (
            (PartDesignResidualSourceRole.BASE, PartDesignResidualSourceRole.PROFILE),
        ),
        PartDesignResidualOperation.REVOLUTION.value: (
            (PartDesignResidualSourceRole.PROFILE,),
            (PartDesignResidualSourceRole.BASE, PartDesignResidualSourceRole.PROFILE),
        ),
        PartDesignResidualOperation.COORDINATE_SYSTEM.value: (
            (PartDesignResidualSourceRole.BODY_ANCHOR,),
        ),
    }
)


class PartDesignResidualProductKind(StrEnum):
    SOLID = "solid"
    REFERENCE = "reference"


@dataclass(frozen=True, slots=True)
class PartDesignResidualProductContract:
    operation: PartDesignResidualOperation
    result_kind: PartDesignResidualProductKind
    semantic_role: SemanticRole
    source_counts: tuple[int, ...]
    native_type_id: str

    def __post_init__(self) -> None:
        expected_kind = (
            PartDesignResidualProductKind.REFERENCE
            if self.operation is PartDesignResidualOperation.COORDINATE_SYSTEM
            else PartDesignResidualProductKind.SOLID
        )
        expected_role = (
            SemanticRole.SUPPORT
            if self.operation is PartDesignResidualOperation.COORDINATE_SYSTEM
            else SemanticRole.FEATURE
        )
        expected_counts = tuple(
            len(item) for item in PARTDESIGN_RESIDUAL_REQUIRED_SOURCE_ROLES[self.operation.value]
        )
        if (
            type(self.operation) is not PartDesignResidualOperation
            or self.result_kind is not expected_kind
            or self.semantic_role is not expected_role
            or self.source_counts != expected_counts
            or self.native_type_id != PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[self.operation]
        ):
            _integrity_failure()


PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS: Final = MappingProxyType(
    {
        operation.value: PartDesignResidualProductContract(
            operation=operation,
            result_kind=(
                PartDesignResidualProductKind.REFERENCE
                if operation is PartDesignResidualOperation.COORDINATE_SYSTEM
                else PartDesignResidualProductKind.SOLID
            ),
            semantic_role=(
                SemanticRole.SUPPORT
                if operation is PartDesignResidualOperation.COORDINATE_SYSTEM
                else SemanticRole.FEATURE
            ),
            source_counts=tuple(
                len(item) for item in PARTDESIGN_RESIDUAL_REQUIRED_SOURCE_ROLES[operation.value]
            ),
            native_type_id=PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[operation],
        )
        for operation in PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS
    }
)


def resolve_partdesign_residual_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve one route only by its complete formal identity."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    try:
        index = PARTDESIGN_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES.index(
            (operation_id, semantic_operation)
        )
    except ValueError:
        return None
    operation = PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS[index]
    return _OPERATIONS_BY_ID[operation.value]


def partdesign_residual_reviewed_adapter_factory(
    sink: PlanSink,
) -> ExactReviewedFamilyAdapter:
    return FreeCADPartDesignResidualAdapter(sink)


def _validate_plan_contract(
    plan: object,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
) -> PartDesignResidualBackendPlan:
    if (
        type(plan) is not PartDesignResidualBackendPlan
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation not in PARTDESIGN_RESIDUAL_MANIFEST.operations
        or plan.operation.value != operation.operation_id
        or plan.adapter_contract_sha256
        != PARTDESIGN_RESIDUAL_MANIFEST.adapter.adapter_contract_sha256
        or plan.manifest_sha256 != PARTDESIGN_RESIDUAL_MANIFEST.manifest_sha256
        or plan.plan_sha256 != plan_document.document_digest
        or hashlib.sha256(plan.canonical_bytes).hexdigest() != plan_document.content_sha256
        or len(plan.canonical_bytes) != plan_document.size_bytes
    ):
        _integrity_failure()
    try:
        decoded = decode_partdesign_residual_backend_plan(
            plan.canonical_bytes,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def validate_partdesign_residual_reviewed_plan(
    plan: object,
    receipt: ReviewedPlanReceipt,
    operation: ReviewedOperationSpec,
) -> None:
    """Bind the canonical residual plan to its exact lowering receipt."""

    if (
        type(receipt) is not ReviewedPlanReceipt
        or receipt.operation != operation
        or receipt.manifest_sha256 != PARTDESIGN_RESIDUAL_MANIFEST.manifest_sha256
        or receipt.adapter != PARTDESIGN_RESIDUAL_MANIFEST.adapter
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


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _shape_sha256(item: object) -> str:
    try:
        raw = item.Shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def _valid_solid(item: object) -> bool:
    try:
        shape = item.Shape
        return (
            item.isValid() is True
            and tuple(item.State) == ("Up-to-date",)
            and shape.isNull() is False
            and shape.isValid() is True
            and len(tuple(shape.Solids)) == 1
            and math.isfinite(float(shape.Volume))
            and float(shape.Volume) > 1e-9
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _valid_closed_profile(item: object) -> bool:
    try:
        wires = tuple(item.Shape.Wires)
        return (
            item.TypeId == "Sketcher::SketchObject"
            and item.isValid() is True
            and tuple(item.State) == ("Up-to-date",)
            and len(wires) == 1
            and wires[0].isClosed() is True
            and tuple(item.OpenVertices) == ()
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _parent_body(item: object) -> object:
    try:
        resolver = item.getParentGeoFeatureGroup
        if not callable(resolver):
            raise ValueError
        body = resolver()
        if body is None:
            raise ValueError
    except (Exception, SystemExit):
        _integrity_failure()
    return body


def _source_receipt_shape_sha256(source: object) -> str:
    receipt = source.native_receipt
    candidates = tuple(
        item
        for item in (
            getattr(receipt, "result_shape_sha256", None),
            getattr(receipt, "shape_sha256", None),
        )
        if _is_sha256(item)
    )
    if len(set(candidates)) != 1:
        _integrity_failure()
    return candidates[0]


def _authenticate_source(
    document: object,
    session: object,
    source: object,
    *,
    run_token: object,
) -> tuple[object, object, str]:
    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        CURRENT_REVIEWED_INTENT_ROUTES,
        ReviewedIntentRoute,
        ReviewedNativeExecutionResult,
    )

    if type(source) is not ReviewedNativeExecutionResult or run_token is None:
        _integrity_failure()
    item = source.object
    receipt = source.native_receipt
    try:
        identity = session.read_object_identity(item)
        body = _parent_body(item)
        body_identity = session.read_object_identity(body)
        document_objects = tuple(document.Objects)
        retained = source._is_retained_for_run(run_token)  # noqa: SLF001
        receipt_name = getattr(
            receipt,
            "object_name",
            getattr(receipt, "sketch_object_name", None),
        )
    except (Exception, SystemExit):
        _integrity_failure()
    shape_sha256 = _source_receipt_shape_sha256(source)
    if (
        type(source.route) is not ReviewedIntentRoute
        or type(identity) is not EntityIdentity
        or type(body_identity) is not EntityIdentity
        or getattr(session, "doc", None) is not document
        or not retained
        or not any(source.route is route for route in CURRENT_REVIEWED_INTENT_ROUTES)
        or source.route.operation not in source.route.manifest.operations
        or source.object is not source.owned_objects[0]
        or len({id(owned) for owned in source.owned_objects}) != len(source.owned_objects)
        or any(
            getattr(owned, "Document", None) is not document
            or not any(owned is current for current in document_objects)
            for owned in source.owned_objects
        )
        or getattr(item, "Document", None) is not document
        or not any(item is current for current in document_objects)
        or getattr(item, "TypeId", None) != source.route.operation.native_type_id
        or receipt_name != getattr(item, "Name", None)
        or getattr(receipt, "plan_sha256", None) != source.plan_sha256
        or not _is_sha256(source.plan_sha256)
        or not _is_sha256(source.plan_content_sha256)
        or not _is_sha256(getattr(receipt, "receipt_sha256", None))
        or not hmac.compare_digest(_shape_sha256(item), shape_sha256)
        or identity.object_type != item.TypeId
        or identity.feature_id is None
        or identity.semantic_role is not SemanticRole.FEATURE
        or identity.provenance.source is not ProvenanceSource.MODEL
        or identity.provenance.operation_id != "apply_reviewed_intent"
        or not source.semantic_roles
        or source.semantic_roles[0] is not identity.semantic_role
        or source.result_kind.value not in {"solid", "valid_shape"}
        or getattr(body, "Document", None) is not document
        or getattr(body, "TypeId", None) != "PartDesign::Body"
        or not any(body is current for current in document_objects)
        or body_identity.object_type != "PartDesign::Body"
        or body_identity.feature_id is None
        or body_identity.semantic_role is not SemanticRole.PART
        or body_identity.provenance.source is not ProvenanceSource.MODEL
        or body_identity.provenance.operation_id != "apply_reviewed_intent"
        or not any(item is child for child in tuple(body.Group))
    ):
        _integrity_failure()
    return item, body, shape_sha256


def _source_roles(plan: PartDesignResidualBackendPlan) -> tuple[PartDesignResidualSourceRole, ...]:
    if plan.operation is PartDesignResidualOperation.HOLE:
        result = (PartDesignResidualSourceRole.BASE, PartDesignResidualSourceRole.PROFILE)
    elif plan.operation is PartDesignResidualOperation.REVOLUTION:
        result = (
            (PartDesignResidualSourceRole.PROFILE,)
            if plan.base is None
            else (PartDesignResidualSourceRole.BASE, PartDesignResidualSourceRole.PROFILE)
        )
    else:
        result = (PartDesignResidualSourceRole.BODY_ANCHOR,)
    if result not in PARTDESIGN_RESIDUAL_REQUIRED_SOURCE_ROLES[plan.operation.value]:
        _integrity_failure()
    return result


def _hole_profile_matches(profile: object, base: object) -> bool:
    try:
        support = tuple(profile.AttachmentSupport)
        subelements = tuple(support[0][1]) if len(support) == 1 else ()
        return (
            profile.MapMode == "FlatFace"
            and len(support) == 1
            and support[0][0] is base
            and len(subelements) == 1
            and _FACE.fullmatch(subelements[0]) is not None
            and int(profile.GeometryCount) == 1
            and profile.Geometry[0].TypeId == "Part::GeomCircle"
            and not bool(profile.getConstruction(0))
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class _AuthenticatedResidualProductBindings:
    execution: PartDesignResidualExecutionBindings
    source_objects: tuple[object, ...] = field(repr=False, compare=False)
    source_shape_sha256s: tuple[str, ...]
    prior_tip: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            type(self.execution) is not PartDesignResidualExecutionBindings
            or type(self.source_objects) is not tuple
            or not 1 <= len(self.source_objects) <= 2
            or len({id(item) for item in self.source_objects}) != len(self.source_objects)
            or type(self.source_shape_sha256s) is not tuple
            or len(self.source_shape_sha256s) != len(self.source_objects)
            or any(not _is_sha256(item) for item in self.source_shape_sha256s)
            or self.prior_tip is None
        ):
            _integrity_failure()


def _authenticated_bindings(
    document: object,
    plan: PartDesignResidualBackendPlan,
    source_results: tuple[object, ...],
    *,
    session: object,
    run_token: object,
) -> _AuthenticatedResidualProductBindings:
    roles = _source_roles(plan)
    if (
        session is None
        or getattr(session, "doc", None) is not document
        or type(source_results) is not tuple
        or len(source_results) != len(roles)
    ):
        _integrity_failure()
    authenticated = tuple(
        _authenticate_source(
            document,
            session,
            source,
            run_token=run_token,
        )
        for source in source_results
    )
    objects = tuple(item[0] for item in authenticated)
    bodies = tuple(item[1] for item in authenticated)
    if any(body is not bodies[0] for body in bodies[1:]):
        _integrity_failure()
    body = bodies[0]
    by_role = dict(zip(roles, objects, strict=True))
    base = by_role.get(PartDesignResidualSourceRole.BASE)
    profile = by_role.get(PartDesignResidualSourceRole.PROFILE)
    anchor = by_role.get(PartDesignResidualSourceRole.BODY_ANCHOR)
    try:
        prior_tip = body.Tip
        group = tuple(body.Group)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        prior_tip is None
        or any(not any(item is child for child in group) for item in objects)
        or (profile is not None and prior_tip is not profile)
        or (anchor is not None and prior_tip is not anchor)
        or (base is not None and not _valid_solid(base))
        or (profile is not None and not _valid_closed_profile(profile))
        or (
            plan.operation is PartDesignResidualOperation.HOLE
            and (base is None or profile is None or not _hole_profile_matches(profile, base))
        )
    ):
        _integrity_failure()
    return _AuthenticatedResidualProductBindings(
        execution=PartDesignResidualExecutionBindings(
            document=document,
            body=body,
            body_id=plan.body_id,
            base=(
                None
                if base is None
                else AuthenticatedResidualObject(
                    object=base,
                    node_id=plan.base.node_id,  # type: ignore[union-attr]
                    result_id=plan.base.result_id,  # type: ignore[union-attr]
                )
            ),
            profile=(
                None
                if profile is None
                else AuthenticatedResidualObject(
                    object=profile,
                    node_id=plan.profile.node_id,  # type: ignore[union-attr]
                    result_id=plan.profile.result_id,  # type: ignore[union-attr]
                )
            ),
        ),
        source_objects=objects,
        source_shape_sha256s=tuple(item[2] for item in authenticated),
        prior_tip=prior_tip,
    )


def _placement_facts(item: object) -> tuple[float, ...]:
    try:
        placement = item.Placement
        result = (
            float(placement.Base.x),
            float(placement.Base.y),
            float(placement.Base.z),
            *(float(value) for value in tuple(placement.Rotation.Q)),
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        _integrity_failure()
    if len(result) != 7 or not all(math.isfinite(value) for value in result):
        _integrity_failure()
    return result


def _result_state_sha256(
    operation: PartDesignResidualOperation,
    body: object,
    result: object,
    prior_tip: object,
) -> str:
    if operation is PartDesignResidualOperation.COORDINATE_SYSTEM:
        try:
            facts: object = {
                "type_id": result.TypeId,
                "map_mode": result.MapMode,
                "attachment_support_count": len(tuple(result.AttachmentSupport)),
                "placement": _placement_facts(result),
                "body_tip_preserved": body.Tip is prior_tip,
                "body_group_index": next(
                    index for index, item in enumerate(tuple(body.Group)) if item is result
                ),
            }
        except (Exception, SystemExit, StopIteration):
            _integrity_failure()
    else:
        facts = {
            "type_id": result.TypeId,
            "shape_sha256": _shape_sha256(result),
            "body_tip_is_result": body.Tip is result,
            "body_group_index": next(
                index for index, item in enumerate(tuple(body.Group)) if item is result
            ),
        }
    try:
        raw = json.dumps(
            facts,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (Exception, SystemExit, TypeError, ValueError, UnicodeError):
        _integrity_failure()
    return hashlib.sha256(_RESULT_STATE_DIGEST_DOMAIN + raw).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignResidualOwnershipClosure:
    """Receipt for one exact Body child created by the residual native rule."""

    native_receipt: PartDesignResidualConformanceReceipt
    plan: PartDesignResidualBackendPlan = field(repr=False)
    bindings: PartDesignResidualExecutionBindings = field(repr=False, compare=False)
    prior_tip: object = field(repr=False, compare=False)
    plan_content_sha256: str
    source_shape_sha256s: tuple[str, ...]
    result_shape_sha256: str | None
    result_state_sha256: str
    semantic_role: SemanticRole
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        contract = PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS.get(
            getattr(self.native_receipt, "operation", None).value
            if type(getattr(self.native_receipt, "operation", None)) is PartDesignResidualOperation
            else ""
        )
        if (
            type(self.native_receipt) is not PartDesignResidualConformanceReceipt
            or type(self.plan) is not PartDesignResidualBackendPlan
            or self.plan.plan_sha256 != self.native_receipt.plan_sha256
            or self.plan.operation is not self.native_receipt.operation
            or type(self.bindings) is not PartDesignResidualExecutionBindings
            or contract is None
            or self.semantic_role is not contract.semantic_role
            or not _is_sha256(self.plan_content_sha256)
            or type(self.source_shape_sha256s) is not tuple
            or len(self.source_shape_sha256s) not in contract.source_counts
            or any(not _is_sha256(item) for item in self.source_shape_sha256s)
            or (
                contract.result_kind is PartDesignResidualProductKind.SOLID
                and not _is_sha256(self.result_shape_sha256)
            )
            or (
                contract.result_kind is PartDesignResidualProductKind.REFERENCE
                and self.result_shape_sha256 is not None
            )
            or not _is_sha256(self.result_state_sha256)
        ):
            _integrity_failure()
        body = b"\0".join(
            (
                _OWNERSHIP_DIGEST_DOMAIN,
                self.native_receipt.receipt_sha256.encode("ascii"),
                self.plan_content_sha256.encode("ascii"),
                self.plan.body_id.encode("utf-8"),
                self.plan.node_id.encode("utf-8"),
                self.plan.result_id.encode("utf-8"),
                (self.result_shape_sha256 or "none").encode("ascii"),
                self.result_state_sha256.encode("ascii"),
                self.semantic_role.value.encode("ascii"),
                *(item.encode("ascii") for item in self.source_shape_sha256s),
            )
        )
        object.__setattr__(self, "receipt_sha256", hashlib.sha256(body).hexdigest())

    @property
    def plan_sha256(self) -> str:
        return self.native_receipt.plan_sha256

    @property
    def operation(self) -> PartDesignResidualOperation:
        return self.native_receipt.operation

    @property
    def object_name(self) -> str:
        return self.native_receipt.object_name

    def validate_native_result(self, document: object, result: object) -> None:
        body = self.bindings.body
        try:
            valid = (
                self.bindings.document is document
                and getattr(result, "Document", None) is document
                and document.getObject(self.object_name) is result
                and getattr(result, "Name", None) == self.object_name
                and getattr(result, "TypeId", None)
                == PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[self.operation]
                and any(result is item for item in tuple(document.Objects))
                and any(result is item for item in tuple(body.Group))
                and result.isValid() is True
                and tuple(result.State) == ("Up-to-date",)
                and hmac.compare_digest(
                    _result_state_sha256(self.operation, body, result, self.prior_tip),
                    self.result_state_sha256,
                )
            )
            if self.operation is PartDesignResidualOperation.COORDINATE_SYSTEM:
                valid = valid and body.Tip is self.prior_tip
            else:
                valid = (
                    valid
                    and body.Tip is result
                    and _valid_solid(result)
                    and hmac.compare_digest(
                        _shape_sha256(result),
                        self.result_shape_sha256,
                    )
                )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adoption(
        self,
        document: object,
        result: object,
        observation: object,
    ) -> None:
        self.validate_native_result(document, result)
        contract = PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS[self.operation.value]
        if (
            type(observation) is not EntityObservation
            or observation.feature_id is None
            or observation.object_type != contract.native_type_id
            or observation.semantic_role != contract.semantic_role.value
            or (
                contract.result_kind is PartDesignResidualProductKind.SOLID
                and (
                    observation.valid_shape is not True
                    or observation.solid_count != 1
                    or observation.volume_mm3 is None
                    or observation.volume_mm3 <= 1e-9
                )
            )
        ):
            _integrity_failure()


def _result_links_match(
    plan: PartDesignResidualBackendPlan,
    bindings: PartDesignResidualExecutionBindings,
    result: object,
    prior_tip: object,
) -> bool:
    try:
        if plan.operation is PartDesignResidualOperation.COORDINATE_SYSTEM:
            return (
                bindings.body.Tip is prior_tip
                and result.MapMode == "Deactivated"
                and tuple(result.AttachmentSupport) == ()
            )
        profile = bindings.profile
        if profile is None:
            return False
        return (
            bindings.body.Tip is result
            and result.Profile[0] is profile.object
            and tuple(result.Profile[1]) == ()
            and result.BaseFeature is (None if bindings.base is None else bindings.base.object)
        )
    except (Exception, SystemExit):
        return False


def execute_partdesign_residual_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
    run_token: object,
) -> object:
    """Apply one residual operation from exact retained same-run sources."""

    if document is None or type(payload) is not bytes:
        _integrity_failure()
    checked = _validate_plan_contract(plan, plan_document, operation)
    try:
        decoded = decode_partdesign_residual_backend_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != checked:
        _integrity_failure()
    authenticated = _authenticated_bindings(
        document,
        checked,
        source_results,
        session=session,
        run_token=run_token,
    )
    bindings = authenticated.execution
    try:
        before = tuple(document.Objects)
        before_group = tuple(bindings.body.Group)
    except (Exception, SystemExit):
        _integrity_failure()
    receipt = apply_partdesign_residual_plan(
        payload,
        expected_content_sha256=plan_document.content_sha256,
        expected_plan_sha256=plan_document.document_digest,
        bindings=bindings,
    )
    try:
        after = tuple(document.Objects)
        after_group = tuple(bindings.body.Group)
        added = tuple(item for item in after if not any(item is old for old in before))
        result = added[0] if len(added) == 1 else None
        current_source_sha256s = tuple(_shape_sha256(item) for item in authenticated.source_objects)
    except (Exception, SystemExit):
        _integrity_failure()
    if (
        type(receipt) is not PartDesignResidualConformanceReceipt
        or receipt.operation is not checked.operation
        or receipt.plan_sha256 != checked.plan_sha256
        or receipt.native_type_id != operation.native_type_id
        or len(after) != len(before) + 1
        or any(
            current is not old for current, old in zip(after[: len(before)], before, strict=True)
        )
        or result is not after[-1]
        or getattr(result, "Name", None) != receipt.object_name
        or getattr(result, "Document", None) is not document
        or getattr(result, "TypeId", None) != operation.native_type_id
        or after_group != (*before_group, result)
        or current_source_sha256s != authenticated.source_shape_sha256s
        or not _result_links_match(checked, bindings, result, authenticated.prior_tip)
        or (
            checked.operation is not PartDesignResidualOperation.COORDINATE_SYSTEM
            and not _valid_solid(result)
        )
    ):
        _integrity_failure()
    state_sha256 = _result_state_sha256(
        checked.operation,
        bindings.body,
        result,
        authenticated.prior_tip,
    )
    ownership = PartDesignResidualOwnershipClosure(
        native_receipt=receipt,
        plan=checked,
        bindings=bindings,
        prior_tip=authenticated.prior_tip,
        plan_content_sha256=plan_document.content_sha256,
        source_shape_sha256s=authenticated.source_shape_sha256s,
        result_shape_sha256=(
            None
            if checked.operation is PartDesignResidualOperation.COORDINATE_SYSTEM
            else _shape_sha256(result)
        ),
        result_state_sha256=state_sha256,
        semantic_role=PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS[checked.operation.value].semantic_role,
    )
    ownership.validate_native_result(document, result)

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedFamilyNativeExecution,
    )

    return _ReviewedFamilyNativeExecution(object=result, receipt=ownership)


def execute_partdesign_residual_reviewed_plan(
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
        type(context) is not _ReviewedFamilyExecutionContext
        or context.document is not document
        or context.run_token is None
    ):
        _integrity_failure()
    return execute_partdesign_residual_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
        run_token=context.run_token,
    )


@dataclass(frozen=True, slots=True)
class PartDesignResidualReviewedFamilySpec:
    manifest: FamilyBatchManifest
    subject_type_term: BridgeTermRef
    adapter_factory: object = field(repr=False, compare=False)
    validate_plan: object = field(repr=False, compare=False)
    execute_plan: object = field(repr=False, compare=False)
    operation_ids: tuple[str, ...]
    minimum_sources: int
    maximum_sources: int

    def __post_init__(self) -> None:
        if (
            self.manifest is not PARTDESIGN_RESIDUAL_MANIFEST
            or self.subject_type_term != _bridge_term(RESIDUAL_STRUCTURE_TERM)
            or not callable(self.adapter_factory)
            or not callable(self.validate_plan)
            or not callable(self.execute_plan)
            or self.operation_ids
            != tuple(item.value for item in PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS)
            or self.minimum_sources != 1
            or self.maximum_sources != 2
        ):
            _integrity_failure()


PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC: Final = PartDesignResidualReviewedFamilySpec(
    manifest=PARTDESIGN_RESIDUAL_MANIFEST,
    subject_type_term=_bridge_term(RESIDUAL_STRUCTURE_TERM),
    adapter_factory=partdesign_residual_reviewed_adapter_factory,
    validate_plan=validate_partdesign_residual_reviewed_plan,
    execute_plan=execute_partdesign_residual_reviewed_plan,
    operation_ids=tuple(item.value for item in PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS),
    minimum_sources=1,
    maximum_sources=2,
)


def build_partdesign_residual_reviewed_family_descriptor() -> object:
    """Build the private descriptor without registering any CURRENT route."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedIntentFamilyDescriptor,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    return _ReviewedIntentFamilyDescriptor(
        manifest=PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC.manifest,
        subject_type_term=PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC.subject_type_term,
        adapter_factory=PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC.adapter_factory,
        validate_plan=PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC.validate_plan,
        execute_plan=PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC.execute_plan,
        product_results=tuple(
            _ReviewedProductResultContract(
                operation_id=operation.value,
                result_kind=(
                    _ReviewedProductResultKind.REFERENCE
                    if operation is PartDesignResidualOperation.COORDINATE_SYSTEM
                    else _ReviewedProductResultKind.SOLID
                ),
                owned_type_ids=(PARTDESIGN_RESIDUAL_NATIVE_TYPE_IDS[operation],),
                semantic_roles=(
                    PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS[operation.value].semantic_role,
                ),
                source_count=source_count,
            )
            for operation in PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS
            for source_count in PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS[operation.value].source_counts
        ),
        minimum_sources=PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC.minimum_sources,
        maximum_sources=PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC.maximum_sources,
        requires_same_run_sources=True,
    )


__all__ = [
    "PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS",
    "PARTDESIGN_RESIDUAL_REQUIRED_SOURCE_ROLES",
    "PARTDESIGN_RESIDUAL_REVIEWED_FAMILY_SPEC",
    "PARTDESIGN_RESIDUAL_REVIEWED_OPERATIONS",
    "PARTDESIGN_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES",
    "PartDesignResidualOwnershipClosure",
    "PartDesignResidualProductContract",
    "PartDesignResidualProductKind",
    "PartDesignResidualReviewedFamilySpec",
    "PartDesignResidualSourceRole",
    "build_partdesign_residual_reviewed_family_descriptor",
    "execute_partdesign_residual_reviewed_plan",
    "execute_partdesign_residual_reviewed_plan_with_sources",
    "partdesign_residual_reviewed_adapter_factory",
    "resolve_partdesign_residual_reviewed_operation",
    "validate_partdesign_residual_reviewed_plan",
]
