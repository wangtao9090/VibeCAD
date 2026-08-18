"""Private product handoff for the reviewed planar-mechanical PM1 family.

PM1 is one whole-model transaction.  Each formal operation selects a different
primary from that transaction, but every product result owns the complete
``Body -> Sketch -> Pad -> (Sketch -> Pocket)*`` closure, including the Body's
engine-created Origin helpers.  This module never presents the focused Sketch,
Pad, or Pocket as the transaction's only side effect.

The existing PM1 adapter requires a VisualFeatureGraph proof plus exact Sketch
and ParametricFeatureGraph intent documents.  ``ReviewedIntentProgramV1`` can
currently carry only one intent graph, so this handoff is deliberately not a
``FamilyBatchManifest`` and is not registered in ``CURRENT``.  A future wire may
use the operation specs and native callback here only after it preserves the
adapter's original three-document proof contract.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final

from vibecad.execution.selectors import SemanticRole
from vibecad.intent_bridge.contracts import BridgeTermRef, DocumentRef
from vibecad.intent_bridge.freecad_planar_mechanical_adapter import (
    FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
    PLANAR_PLAN_DOCUMENT_ROLE_TERM,
    PLANAR_PLAN_SCHEMA_TERM,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
)
from vibecad.intent_bridge.reviewed_family_engine import ReviewedOperationSpec
from vibecad.intent_bridge.sketch_intent_graph_codec import SKETCH_INTENT_GRAPH_MEDIA_TYPE
from vibecad.intent_bridge.visual_feature_graph_codec import VISUAL_FEATURE_GRAPH_MEDIA_TYPE
from vibecad.intent_rules.planar_mechanical_v1.terms import (
    PFG_OPERATION_ADD,
    PFG_OPERATION_REFERENCE_PROFILES,
    PFG_OPERATION_REMOVE,
)
from vibecad.parametric.feature_graph_v2 import SemanticTermRefV2
from vibecad.parametric.freecad_planar_mechanical_rules import (
    PLANAR_MECHANICAL_PLAN_MEDIA_TYPE,
    PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    PLANAR_MECHANICAL_RULE_ID,
    PlanarMechanicalBackendPlan,
    PlanarMechanicalConformanceReceipt,
    PlanarMechanicalExecutionBindings,
    apply_planar_mechanical_plan,
    decode_planar_mechanical_plan,
)

_OWNERSHIP_DIGEST_DOMAIN = b"vibecad.planar-mechanical-product-ownership.v1\0"
_HANDOFF_DIGEST_DOMAIN = b"vibecad.planar-mechanical-product-handoff.v1\0"
_LEGACY_MANIFEST_SHA256 = "4c1479a158c2bc15eb384fc51d3ec4b574e9e76b1c600a5de785d39ea6721feb"

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


PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS: Final = (
    ReviewedOperationSpec(
        operation_id="partdesign.planar-mechanical.reference-profiles",
        semantic_term=_bridge_term(PFG_OPERATION_REFERENCE_PROFILES),
        native_type_id="Sketcher::SketchObject",
        native_operation="reference_profiles",
        native_property_names=("Geometry", "MapMode", "Support"),
    ),
    ReviewedOperationSpec(
        operation_id="partdesign.planar-mechanical.add",
        semantic_term=_bridge_term(PFG_OPERATION_ADD),
        native_type_id="PartDesign::Pad",
        native_operation="add",
        native_property_names=("Length", "Midplane", "Profile", "Reversed", "Type"),
    ),
    ReviewedOperationSpec(
        operation_id="partdesign.planar-mechanical.remove",
        semantic_term=_bridge_term(PFG_OPERATION_REMOVE),
        native_type_id="PartDesign::Pocket",
        native_operation="remove",
        native_property_names=("Length", "Midplane", "Profile", "Reversed", "Type"),
    ),
)
_OPERATIONS_BY_ID: Final = MappingProxyType(
    {item.operation_id: item for item in PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS}
)


@dataclass(frozen=True, slots=True)
class PlanarMechanicalReviewedProductHandoff:
    """Static, non-routable bridge facts for a future multi-document wire."""

    operation_specs: tuple[ReviewedOperationSpec, ...]
    required_intent_media_types: tuple[str, ...]
    required_proof_media_types: tuple[str, ...]
    legacy_manifest_sha256: str
    adapter_contract_sha256: str
    rule_contract_sha256: str
    lowering_ready: bool = False
    minimum_source_results: int = 0
    maximum_source_results: int = 0
    handoff_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.operation_specs != PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS
            or self.required_intent_media_types
            != (SKETCH_INTENT_GRAPH_MEDIA_TYPE, PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE)
            or self.required_proof_media_types
            != (
                VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
                SKETCH_INTENT_GRAPH_MEDIA_TYPE,
                PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
            )
            or self.legacy_manifest_sha256 != _LEGACY_MANIFEST_SHA256
            or self.adapter_contract_sha256
            != FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256
            or self.rule_contract_sha256 != PLANAR_MECHANICAL_RULE_CONTRACT_SHA256
            or self.lowering_ready is not False
            or self.minimum_source_results != 0
            or self.maximum_source_results != 0
        ):
            _integrity_failure()
        body = "\0".join(
            (
                self.legacy_manifest_sha256,
                self.adapter_contract_sha256,
                PLANAR_MECHANICAL_RULE_ID,
                self.rule_contract_sha256,
                *(item.operation_id for item in self.operation_specs),
                *self.required_intent_media_types,
                *self.required_proof_media_types,
                "lowering-ready=false",
                "source-results=0",
                "owned-additions=11+2N",
            )
        ).encode("ascii")
        object.__setattr__(
            self,
            "handoff_sha256",
            hashlib.sha256(_HANDOFF_DIGEST_DOMAIN + body).hexdigest(),
        )


PLANAR_MECHANICAL_REVIEWED_PRODUCT_HANDOFF: Final = PlanarMechanicalReviewedProductHandoff(
    operation_specs=PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS,
    required_intent_media_types=(
        SKETCH_INTENT_GRAPH_MEDIA_TYPE,
        PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    ),
    required_proof_media_types=(
        VISUAL_FEATURE_GRAPH_MEDIA_TYPE,
        SKETCH_INTENT_GRAPH_MEDIA_TYPE,
        PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    ),
    legacy_manifest_sha256=_LEGACY_MANIFEST_SHA256,
    adapter_contract_sha256=(FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256),
    rule_contract_sha256=PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
)


def resolve_planar_mechanical_reviewed_operation(
    operation_id: object,
    semantic_operation: object,
) -> ReviewedOperationSpec | None:
    """Resolve only an exact formal id plus full content-bound term identity."""

    if type(operation_id) is not str or type(semantic_operation) is not str:
        return None
    operation = _OPERATIONS_BY_ID.get(operation_id)
    if operation is None:
        return None
    namespace, version, term_id, digest = operation.semantic_term.semantic_identity
    expected = f"{namespace}/{version}/{term_id}@{digest}"
    return operation if hmac.compare_digest(semantic_operation, expected) else None


def _shape_sha256(item: object) -> str:
    try:
        raw = item.Shape.exportBrepToString().encode("utf-8")
    except (Exception, SystemExit):
        _integrity_failure()
    if not raw:
        _integrity_failure()
    return hashlib.sha256(raw).hexdigest()


def _valid_shape(item: object, *, solid: bool) -> bool:
    try:
        shape = item.Shape
        return bool(
            item.isValid()
            and tuple(item.State) == ("Up-to-date",)
            and not shape.isNull()
            and shape.isValid()
            and (
                not solid
                or (
                    len(shape.Solids) == 1
                    and math.isfinite(float(shape.Volume))
                    and float(shape.Volume) > 0.0
                )
            )
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _validate_body_helpers(document: object, body: object) -> tuple[object, ...]:
    try:
        origin = body.Origin
        helpers = tuple(origin.OriginFeatures)
        closure = (body, origin, *helpers)
        document_objects = tuple(document.Objects)
        valid = (
            tuple(item.TypeId for item in closure) == _BODY_HELPER_TYPE_IDS
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


def _expected_visibility(
    *,
    pad: object,
    circle_sketches: tuple[object, ...],
    pockets: tuple[object, ...],
) -> bool:
    try:
        if not bool(pad.Visibility) == (not pockets):
            return False
        if any(not bool(item.Visibility) for item in circle_sketches):
            return False
        return all(
            bool(item.Visibility) == (index == len(pockets) - 1)
            for index, item in enumerate(pockets)
        )
    except (Exception, SystemExit):
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class PlanarMechanicalOwnershipClosure:
    """Content-bound ownership of every object created by one PM1 transaction."""

    operation: ReviewedOperationSpec
    plan: PlanarMechanicalBackendPlan
    native_receipt: PlanarMechanicalConformanceReceipt
    primary: object = field(repr=False, compare=False)
    body: object = field(repr=False, compare=False)
    body_closure: tuple[object, ...] = field(repr=False, compare=False)
    outer_sketch: object = field(repr=False, compare=False)
    pad: object = field(repr=False, compare=False)
    circle_sketches: tuple[object, ...] = field(repr=False, compare=False)
    pockets: tuple[object, ...] = field(repr=False, compare=False)
    primary_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        expected = _OPERATIONS_BY_ID.get(getattr(self.operation, "operation_id", None))
        if (
            type(self.operation) is not ReviewedOperationSpec
            or self.operation != expected
            or type(self.plan) is not PlanarMechanicalBackendPlan
            or type(self.native_receipt) is not PlanarMechanicalConformanceReceipt
            or self.native_receipt.plan_sha256 != self.plan.plan_sha256
            or self.primary is None
            or self.body is None
            or type(self.body_closure) is not tuple
            or len(self.body_closure) != len(_BODY_HELPER_TYPE_IDS)
            or self.body_closure[0] is not self.body
            or self.outer_sketch is None
            or self.pad is None
            or type(self.circle_sketches) is not tuple
            or type(self.pockets) is not tuple
            or len(self.circle_sketches) != len(self.plan.circles)
            or len(self.pockets) != len(self.plan.circles)
            or len(self.circle_sketches) != len(self.pockets)
            or not hmac.compare_digest(self.primary_shape_sha256, _shape_sha256(self.primary))
        ):
            _integrity_failure()
        expected_primary = {
            "partdesign.planar-mechanical.reference-profiles": self.outer_sketch,
            "partdesign.planar-mechanical.add": self.pad,
            "partdesign.planar-mechanical.remove": self.pockets[-1] if self.pockets else None,
        }[self.operation.operation_id]
        if self.primary is not expected_primary:
            _integrity_failure()
        body = "\0".join(
            (
                self.native_receipt.receipt_sha256,
                self.operation.operation_id,
                self.plan.plan_sha256,
                self.primary_shape_sha256,
                self.body.Name,
                self.outer_sketch.Name,
                self.pad.Name,
                *(item.Name for item in self.circle_sketches),
                *(item.Name for item in self.pockets),
            )
        ).encode("utf-8")
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_OWNERSHIP_DIGEST_DOMAIN + body).hexdigest(),
        )

    @property
    def plan_sha256(self) -> str:
        return self.plan.plan_sha256

    @property
    def object_name(self) -> str:
        return self.primary.Name

    def transaction_objects(self) -> tuple[object, ...]:
        members: list[object] = [*self.body_closure, self.outer_sketch, self.pad]
        for sketch, pocket in zip(self.circle_sketches, self.pockets, strict=True):
            members.extend((sketch, pocket))
        return tuple(members)

    def owned_objects(self, result: object) -> tuple[object, ...]:
        if result is not self.primary:
            _integrity_failure()
        additions = self.transaction_objects()
        return (result, *(item for item in additions if item is not result))

    def validate_native_result(self, document: object, result: object) -> None:
        if result is not self.primary:
            _integrity_failure()
        current_body_closure = _validate_body_helpers(document, self.body)
        try:
            objects = tuple(document.Objects)
            expected_group: list[object] = [self.outer_sketch, self.pad]
            for sketch, pocket in zip(self.circle_sketches, self.pockets, strict=True):
                expected_group.extend((sketch, pocket))
            final_feature = self.pockets[-1] if self.pockets else self.pad
            previous = self.pad
            chain_valid = True
            for sketch, pocket in zip(self.circle_sketches, self.pockets, strict=True):
                chain_valid = chain_valid and (
                    pocket.Profile[0] is sketch
                    and tuple(pocket.Profile[1]) == ()
                    and pocket.BaseFeature is previous
                )
                previous = pocket
            all_transaction_objects = self.transaction_objects()
            valid = (
                len(current_body_closure) == len(self.body_closure)
                and all(
                    current is original
                    for current, original in zip(
                        current_body_closure, self.body_closure, strict=True
                    )
                )
                and len(all_transaction_objects) == 11 + 2 * len(self.plan.circles)
                and len({id(item) for item in all_transaction_objects})
                == len(all_transaction_objects)
                and all(item.Document is document for item in all_transaction_objects)
                and all(
                    any(item is existing for existing in objects)
                    for item in all_transaction_objects
                )
                and self.body.TypeId == "PartDesign::Body"
                and tuple(self.body.Group) == tuple(expected_group)
                and self.body.Tip is final_feature
                and all(
                    item.isValid()
                    and tuple(item.State) == ("Up-to-date",)
                    and bool(item.Visibility)
                    for item in self.body_closure
                )
                and bool(self.outer_sketch.Visibility)
                and self.outer_sketch.TypeId == "Sketcher::SketchObject"
                and self.outer_sketch.isValid()
                and tuple(self.outer_sketch.State) == ("Up-to-date",)
                and self.pad.TypeId == "PartDesign::Pad"
                and self.pad.Profile[0] is self.outer_sketch
                and tuple(self.pad.Profile[1]) == ()
                and self.pad.Type == "Length"
                and math.isclose(
                    float(self.pad.Length),
                    self.plan.depth_mm,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                and not bool(self.pad.Midplane)
                and not bool(self.pad.Reversed)
                and bool(self.pad.Refine)
                and not bool(self.pad.AllowMultiFace)
                and _valid_shape(self.pad, solid=True)
                and all(
                    item.TypeId == "Sketcher::SketchObject"
                    and item.isValid()
                    and tuple(item.State) == ("Up-to-date",)
                    for item in self.circle_sketches
                )
                and all(
                    item.TypeId == "PartDesign::Pocket"
                    and item.Type == "ThroughAll"
                    and item.SideType == "One side"
                    and bool(item.AlongSketchNormal)
                    and not bool(item.UseCustomVector)
                    and math.isclose(float(item.Offset), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and math.isclose(float(item.Offset2), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and math.isclose(float(item.TaperAngle), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and math.isclose(float(item.TaperAngle2), 0.0, rel_tol=0.0, abs_tol=1e-12)
                    and bool(item.Reversed)
                    and bool(item.Refine)
                    and _valid_shape(item, solid=True)
                    for item in self.pockets
                )
                and chain_valid
                and _expected_visibility(
                    pad=self.pad,
                    circle_sketches=self.circle_sketches,
                    pockets=self.pockets,
                )
                and _valid_shape(
                    self.primary,
                    solid=self.operation.operation_id
                    != "partdesign.planar-mechanical.reference-profiles",
                )
                and hmac.compare_digest(
                    self.primary_shape_sha256,
                    _shape_sha256(self.primary),
                )
                and math.isclose(
                    float(final_feature.Shape.Volume),
                    self.plan.expected_volume_mm3,
                    rel_tol=0.0,
                    abs_tol=max(1e-6, self.plan.expected_volume_mm3 * 1e-8),
                )
                and math.isclose(
                    self.native_receipt.volume_mm3,
                    float(final_feature.Shape.Volume),
                    rel_tol=0.0,
                    abs_tol=max(1e-6, self.plan.expected_volume_mm3 * 1e-8),
                )
            )
        except (Exception, SystemExit):
            valid = False
        if not valid:
            _integrity_failure()

    def validate_adoption(self, document: object, result: object, observation: object) -> None:
        from vibecad.validation import EntityObservation  # noqa: PLC0415

        self.validate_native_result(document, result)
        reference = self.operation.operation_id == "partdesign.planar-mechanical.reference-profiles"
        try:
            valid = (
                type(observation) is EntityObservation
                and observation.feature_id is not None
                and observation.object_type == self.operation.native_type_id
                and observation.semantic_role
                == (SemanticRole.SUPPORT.value if reference else SemanticRole.FEATURE.value)
                and observation.valid_shape is True
                and (
                    reference
                    or (
                        observation.solid_count == 1
                        and observation.volume_mm3 is not None
                        and observation.volume_mm3 > 0.0
                    )
                )
            )
        except (Exception, SystemExit, TypeError, ValueError):
            valid = False
        if not valid:
            _integrity_failure()


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
        others = tuple(item for item in added if not any(item is body for body in bodies))
        for item in (*bodies, *reversed(others)):
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


def _checked_plan(
    plan: object,
    payload: object,
    plan_document: object,
    operation: object,
) -> PlanarMechanicalBackendPlan:
    expected_operation = _OPERATIONS_BY_ID.get(getattr(operation, "operation_id", None))
    if (
        type(plan) is not PlanarMechanicalBackendPlan
        or type(payload) is not bytes
        or type(plan_document) is not DocumentRef
        or type(operation) is not ReviewedOperationSpec
        or operation != expected_operation
        or plan.adapter_contract_sha256
        != FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        or plan_document.role_term_ref_id != PLANAR_PLAN_DOCUMENT_ROLE_TERM.term_ref_id
        or plan_document.schema_term_ref_id != PLANAR_PLAN_SCHEMA_TERM.term_ref_id
        or plan_document.media_type != PLANAR_MECHANICAL_PLAN_MEDIA_TYPE
        or plan_document.size_bytes != len(payload)
        or not hmac.compare_digest(
            plan_document.content_sha256,
            hashlib.sha256(payload).hexdigest(),
        )
        or not hmac.compare_digest(plan_document.document_digest, plan.plan_sha256)
        or not hmac.compare_digest(payload, plan.canonical_bytes)
        or (operation.operation_id == "partdesign.planar-mechanical.remove" and not plan.circles)
    ):
        _integrity_failure()
    try:
        decoded = decode_planar_mechanical_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
        )
    except (Exception, SystemExit):
        _integrity_failure()
    if decoded != plan:
        _integrity_failure()
    return plan


def _resolve_native_closure(
    document: object,
    plan: PlanarMechanicalBackendPlan,
    operation: ReviewedOperationSpec,
    receipt: PlanarMechanicalConformanceReceipt,
) -> PlanarMechanicalOwnershipClosure:
    try:
        body = document.getObject(receipt.body_name)
        outer = document.getObject(receipt.outer_sketch_name)
        pad = document.getObject(receipt.pad_name)
        circle_sketches = tuple(document.getObject(name) for name in receipt.circle_sketch_names)
        pockets = tuple(document.getObject(name) for name in receipt.pocket_names)
        primary = {
            "partdesign.planar-mechanical.reference-profiles": outer,
            "partdesign.planar-mechanical.add": pad,
            "partdesign.planar-mechanical.remove": pockets[-1] if pockets else None,
        }[operation.operation_id]
    except (Exception, SystemExit):
        _integrity_failure()
    if primary is None:
        _integrity_failure()
    return PlanarMechanicalOwnershipClosure(
        operation=operation,
        plan=plan,
        native_receipt=receipt,
        primary=primary,
        body=body,
        body_closure=_validate_body_helpers(document, body),
        outer_sketch=outer,
        pad=pad,
        circle_sketches=circle_sketches,
        pockets=pockets,
        primary_shape_sha256=_shape_sha256(primary),
    )


def execute_planar_mechanical_reviewed_plan_with_sources(
    document: object,
    plan: object,
    payload: bytes,
    plan_document: DocumentRef,
    operation: ReviewedOperationSpec,
    source_results: tuple[object, ...],
    *,
    session: object,
) -> object:
    """Execute one proven PM1 plan and return its full owned transaction closure."""

    checked = _checked_plan(plan, payload, plan_document, operation)
    if (
        document is None
        or type(source_results) is not tuple
        or source_results
        or session is None
        or getattr(session, "doc", None) is not document
    ):
        _integrity_failure()
    before = _document_snapshot(document)
    try:
        receipt = apply_planar_mechanical_plan(
            payload,
            expected_content_sha256=plan_document.content_sha256,
            expected_plan_sha256=plan_document.document_digest,
            bindings=PlanarMechanicalExecutionBindings(document=document),
        )
        if (
            type(receipt) is not PlanarMechanicalConformanceReceipt
            or receipt.plan_sha256 != checked.plan_sha256
            or len(receipt.circle_sketch_names) != len(checked.circles)
            or len(receipt.pocket_names) != len(checked.circles)
        ):
            _integrity_failure()
        ownership = _resolve_native_closure(document, checked, operation, receipt)
        ownership.validate_native_result(document, ownership.primary)
        added = _added_objects(document, before)
        expected_added = ownership.transaction_objects()
        if len(added) != len(expected_added) or any(
            item is not expected for item, expected in zip(added, expected_added, strict=True)
        ):
            _integrity_failure()
        owned = ownership.owned_objects(ownership.primary)
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
        object=ownership.primary,
        receipt=ownership,
        owned_objects=owned,
    )


def execute_planar_mechanical_reviewed_plan(
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

    if type(context) is not _ReviewedFamilyExecutionContext or context.document is not document:
        _integrity_failure()
    return execute_planar_mechanical_reviewed_plan_with_sources(
        document,
        plan,
        payload,
        plan_document,
        operation,
        context.source_results,
        session=context.session,
    )


def resolve_planar_mechanical_product_contract(
    plan: object,
    operation: object,
) -> object:
    """Build the exact dynamic owned-TypeId/role contract for one PM1 plan."""

    from vibecad.execution.freecad_reviewed_intent_execution import (  # noqa: PLC0415
        _ReviewedProductExecutionMode,
        _ReviewedProductResultContract,
        _ReviewedProductResultKind,
    )

    if (
        type(plan) is not PlanarMechanicalBackendPlan
        or type(operation) is not ReviewedOperationSpec
        or operation != _OPERATIONS_BY_ID.get(operation.operation_id)
        or (operation.operation_id == "partdesign.planar-mechanical.remove" and not plan.circles)
    ):
        _integrity_failure()
    additions_types: list[str] = [
        *_BODY_HELPER_TYPE_IDS,
        "Sketcher::SketchObject",
        "PartDesign::Pad",
    ]
    additions_roles: list[SemanticRole] = [
        SemanticRole.PART,
        *(SemanticRole.SUPPORT,) * 8,
        SemanticRole.SUPPORT,
        SemanticRole.FEATURE,
    ]
    for _circle in plan.circles:
        additions_types.extend(("Sketcher::SketchObject", "PartDesign::Pocket"))
        additions_roles.extend((SemanticRole.SUPPORT, SemanticRole.FEATURE))
    primary_index = {
        "partdesign.planar-mechanical.reference-profiles": 9,
        "partdesign.planar-mechanical.add": 10,
        "partdesign.planar-mechanical.remove": len(additions_types) - 1,
    }[operation.operation_id]
    owned_types = (
        additions_types[primary_index],
        *(item for index, item in enumerate(additions_types) if index != primary_index),
    )
    owned_roles = (
        additions_roles[primary_index],
        *(item for index, item in enumerate(additions_roles) if index != primary_index),
    )
    return _ReviewedProductResultContract(
        operation_id=operation.operation_id,
        result_kind=(
            _ReviewedProductResultKind.VALID_SHAPE
            if operation.operation_id == "partdesign.planar-mechanical.reference-profiles"
            else _ReviewedProductResultKind.SOLID
        ),
        owned_type_ids=tuple(owned_types),
        semantic_roles=tuple(owned_roles),
        source_count=None,
        execution_mode=_ReviewedProductExecutionMode.CREATE,
    )


__all__ = [
    "PLANAR_MECHANICAL_REVIEWED_OPERATION_SPECS",
    "PLANAR_MECHANICAL_REVIEWED_PRODUCT_HANDOFF",
    "PlanarMechanicalOwnershipClosure",
    "PlanarMechanicalReviewedProductHandoff",
    "execute_planar_mechanical_reviewed_plan",
    "execute_planar_mechanical_reviewed_plan_with_sources",
    "resolve_planar_mechanical_product_contract",
    "resolve_planar_mechanical_reviewed_operation",
]
