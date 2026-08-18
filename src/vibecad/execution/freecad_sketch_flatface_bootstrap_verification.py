"""Private managed verification for the reviewed FlatFace Sketch family.

The one-operation family remains formal-only and does not become an additional
native promotion owner.  This module supplies its exact seven-facet reviewed
host manifest and same-process managed-FreeCAD executor.  The older explicit
Sketch -> Hole gate remains available as a focused downstream compatibility
check; release verification uses the independently challenged facet matrix.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import threading
from pathlib import Path
from typing import Final

from vibecad.execution import freecad_reviewed_verification as verification
from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_reviewed_verification import (
    REVIEWED_VERIFICATION_SCHEMA_VERSION,
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceFacet,
    ReviewedVerificationReceipt,
    build_managed_freecad_conformance_host,
    build_promotion_verification_binding,
    build_reviewed_verification_receipt,
)
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    PARTDESIGN_RESIDUAL_MANIFEST,
)
from vibecad.intent_bridge.freecad_sketch_flatface_bootstrap_adapter import (
    FLATFACE_SKETCH_BODY_OWNERSHIP_TERM,
    FLATFACE_SKETCH_CREATE_OPERATION_TERM,
    FLATFACE_SKETCH_FAMILY_MANIFEST,
    FLATFACE_SKETCH_PROFILE_TERM,
    FLATFACE_SKETCH_SELECTOR_TERM,
    FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR,
)
from vibecad.parametric import freecad_sketch_flatface_bootstrap_rules as flatface_rules
from vibecad.parametric.freecad_partdesign_residual_rules import (
    AuthenticatedResidualObject,
    HoleExtent,
    PartDesignResidualBackendPlan,
    PartDesignResidualExecutionBindings,
    PartDesignResidualOperation,
    SemanticObjectSelection,
    apply_partdesign_residual_plan,
)
from vibecad.parametric.freecad_sketch_flatface_bootstrap_rules import (
    FlatFaceSketchBackendPlan,
    FlatFaceSketchExecutionBindings,
    FlatFaceSketchRuleError,
    FlatFaceSketchRuleErrorCode,
    FlatFaceSketchSemanticIdentity,
    apply_flatface_sketch_plan,
)

FLATFACE_SKETCH_HOLE_VERIFIER_ID: Final = "freecad-flatface-sketch-hole-gate-v1"
FLATFACE_SKETCH_HOLE_VERIFIER_VERSION: Final = "1.0.0"
FLATFACE_SKETCH_VERIFIER_ID: Final = "vcad.managed.freecad.flatface-sketch-conformance"
FLATFACE_SKETCH_VERIFIER_VERSION: Final = "1.0.0"

_CASE_CONTRACT_DOMAIN = b"vibecad-flatface-sketch-case-contract-v1\0"
_HARNESS_CONTRACT_DOMAIN = b"vibecad-flatface-sketch-harness-contract-v1\0"
_OBSERVATION_DOMAIN = b"vibecad-flatface-sketch-observation-v1\0"
_VERIFICATION_LOCK = threading.Lock()

_FACET_CONTRACTS: Final = {
    ReviewedConformanceFacet.CREATE: (
        "canonical-family-plan-creates-body-owned-content-bound-flatface-circle"
    ),
    ReviewedConformanceFacet.EDIT: (
        "native-circle-center-edit-preserves-flatface-support-owner-and-closed-profile"
    ),
    ReviewedConformanceFacet.RECOMPUTE: (
        "explicit-recompute-preserves-selection-state-shape-and-geometry-digests"
    ),
    ReviewedConformanceFacet.SAVE: "managed-fcstd-save-is-nonempty",
    ReviewedConformanceFacet.REOPEN: (
        "saved-base-and-flatface-sketch-reopen-with-exact-primary-digests"
    ),
    ReviewedConformanceFacet.NEGATIVE: ("tampered-plan-is-rejected-before-document-mutation"),
    ReviewedConformanceFacet.LATE_ROLLBACK: (
        "post-native-create-failure-restores-sequence-group-tip-and-visibility"
    ),
}


def _fail(path: str) -> None:
    raise CapabilityCatalogError(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)


def _canonical(value: object, *, maximum: int = 64 * 1024) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail("flatface_sketch_verification/canonical")
    if not raw or len(raw) > maximum:
        _fail("flatface_sketch_verification/canonical")
    return raw


def _sha(domain: bytes, value: bytes | str) -> str:
    raw = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(domain + raw).hexdigest()


_HARNESS_CONTRACT_SHA256: Final = _sha(
    _HARNESS_CONTRACT_DOMAIN,
    _canonical(
        {
            "schema_version": 1,
            "verifier": {
                "id": FLATFACE_SKETCH_VERIFIER_ID,
                "version": FLATFACE_SKETCH_VERIFIER_VERSION,
            },
            "execution": {
                "caller_callback_input": False,
                "caller_pass_input": False,
                "caller_result_input": False,
                "canonical_family_plan": True,
                "native_rule_execution": True,
                "same_process_managed_freecad": True,
                "documents_closed_between_host_cases": True,
                "receipt_persistence": False,
            },
            "facets": {facet.value: value for facet, value in _FACET_CONTRACTS.items()},
        }
    ),
)


def _build_case_manifest() -> ReviewedConformanceCaseManifest:
    operation = FLATFACE_SKETCH_FAMILY_MANIFEST.operations[0]
    cases = tuple(
        ReviewedConformanceCase(
            case_id=f"flatface_sketch.{operation.operation_id}.{facet.value}",
            operation_id=operation.operation_id,
            operation_specification_sha256=operation.specification_sha256,
            facet=facet,
            case_contract_sha256=_sha(
                _CASE_CONTRACT_DOMAIN,
                _canonical(
                    {
                        "facet": facet.value,
                        "facet_contract": _FACET_CONTRACTS[facet],
                        "family_manifest_sha256": (FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256),
                        "harness_contract_sha256": _HARNESS_CONTRACT_SHA256,
                        "operation_id": operation.operation_id,
                        "operation_specification_sha256": operation.specification_sha256,
                        "schema_version": REVIEWED_VERIFICATION_SCHEMA_VERSION,
                    }
                ),
            ),
        )
        for facet in ReviewedConformanceFacet
    )
    return verification._admit_reviewed_host_conformance_case_manifest(  # noqa: SLF001
        manifest=FLATFACE_SKETCH_FAMILY_MANIFEST,
        cases=cases,
    )


FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST: Final = _build_case_manifest()


def _semantic(term: object) -> FlatFaceSketchSemanticIdentity:
    return FlatFaceSketchSemanticIdentity(
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _flatface_plan() -> FlatFaceSketchBackendPlan:
    return FlatFaceSketchBackendPlan(
        source_artifact_id="artifact_flatface_gate",
        source_graph_id="graph_flatface_gate",
        source_graph_sha256="1" * 64,
        source_content_sha256="2" * 64,
        lowering_request_sha256="3" * 64,
        adapter_contract_sha256=(
            FREECAD_FLATFACE_SKETCH_ADAPTER_DESCRIPTOR.adapter_contract_sha256
        ),
        manifest_sha256=FLATFACE_SKETCH_FAMILY_MANIFEST.manifest_sha256,
        body_id="body_main",
        base_node_id="node_base",
        base_result_id="result_base",
        node_id="node_flatface_sketch",
        result_id="result_flatface_sketch",
        operation_identity=_semantic(FLATFACE_SKETCH_CREATE_OPERATION_TERM),
        ownership_identity=_semantic(FLATFACE_SKETCH_BODY_OWNERSHIP_TERM),
        selector_identity=_semantic(FLATFACE_SKETCH_SELECTOR_TERM),
        profile_identity=_semantic(FLATFACE_SKETCH_PROFILE_TERM),
    )


def _hole_plan() -> PartDesignResidualBackendPlan:
    return PartDesignResidualBackendPlan(
        source_artifact_id="artifact_flatface_gate",
        source_graph_id="graph_hole_gate",
        source_graph_sha256="4" * 64,
        source_content_sha256="5" * 64,
        lowering_request_sha256="6" * 64,
        adapter_contract_sha256=(PARTDESIGN_RESIDUAL_MANIFEST.adapter.adapter_contract_sha256),
        manifest_sha256=PARTDESIGN_RESIDUAL_MANIFEST.manifest_sha256,
        body_id="body_main",
        node_id="node_hole",
        result_id="result_hole",
        operation=PartDesignResidualOperation.HOLE,
        base=SemanticObjectSelection(node_id="node_base", result_id="result_base"),
        profile=SemanticObjectSelection(
            node_id="node_flatface_sketch",
            result_id="result_flatface_sketch",
        ),
        hole_extent=HoleExtent.THROUGH_ALL,
        diameter_mm=1.0,
    )


def _same_object_sequence(left: object, right: tuple[object, ...]) -> bool:
    try:
        values = tuple(left)
    except BaseException:
        return False
    return len(values) == len(right) and all(
        actual is expected for actual, expected in zip(values, right, strict=True)
    )


def _close_owned_documents(freecad: object, owned: dict[str, object]) -> None:
    try:
        open_documents = freecad.listDocuments()
        if type(open_documents) is not dict:
            raise TypeError
        for name, document in tuple(owned.items()):
            if open_documents.get(name) is document:
                freecad.closeDocument(name)
    except BaseException:
        _fail("flatface_sketch_verification/cleanup")


def _new_base(*, freecad: object, part: object, name: str) -> tuple[object, object, object]:
    try:
        document = freecad.newDocument(name)
        document.UndoMode = 1
        body = document.addObject("PartDesign::Body", "ReviewedBody")
        base = body.newObject("PartDesign::Feature", "ReviewedBase")
        base.Shape = part.makeBox(10.0, 10.0, 10.0)
        body.Tip = base
        base.Visibility = True
        document.recompute()
    except BaseException:
        _fail("flatface_sketch_verification/base")
    return document, body, base


def _primary_topology(body: object, base: object, sketch: object) -> dict[str, object]:
    try:
        support = tuple(sketch.AttachmentSupport)
        wires = tuple(sketch.Shape.Wires)
        topology = {
            "body_owned": _same_object_sequence(tuple(body.Group), (base, sketch)),
            "body_tip": body.Tip is sketch,
            "support_is_base": len(support) == 1 and support[0][0] is base,
            "map_mode": str(sketch.MapMode),
            "geometry_count": int(sketch.GeometryCount),
            "constraint_count": int(sketch.ConstraintCount),
            "wire_count": len(wires),
            "wire_closed": len(wires) == 1 and bool(wires[0].isClosed()),
            "open_vertex_count": len(tuple(sketch.OpenVertices)),
            "native_type_id": str(sketch.TypeId),
        }
    except (AttributeError, IndexError, TypeError, ValueError):
        _fail("flatface_sketch_verification/topology")
    if topology != {
        "body_owned": True,
        "body_tip": True,
        "support_is_base": True,
        "map_mode": "FlatFace",
        "geometry_count": 1,
        "constraint_count": 0,
        "wire_count": 1,
        "wire_closed": True,
        "open_vertex_count": 0,
        "native_type_id": "Sketcher::SketchObject",
    }:
        _fail("flatface_sketch_verification/topology")
    return topology


def _primary_digests(
    body: object,
    base: object,
    sketch: object,
    selection: object,
) -> dict[str, str]:
    try:
        return {
            "state_sha256": flatface_rules._state_sha256(  # noqa: SLF001
                body, base, sketch, selection
            ),
            "shape_sha256": flatface_rules._shape_sha256(sketch),  # noqa: SLF001
            "geometry_sha256": flatface_rules._geometry_sha256(sketch),  # noqa: SLF001
        }
    except FlatFaceSketchRuleError:
        raise
    except BaseException:
        _fail("flatface_sketch_verification/digests")


def _edited_geometry_sha256(sketch: object) -> str:
    try:
        item = tuple(sketch.Geometry)[0]
        facts = {
            "count": int(sketch.GeometryCount),
            "type_id": str(item.TypeId),
            "center": [float(item.Center.x), float(item.Center.y), float(item.Center.z)],
            "axis": [float(item.Axis.x), float(item.Axis.y), float(item.Axis.z)],
            "radius_mm": float(item.Radius),
            "construction": bool(sketch.getConstruction(0)),
            "constraint_count": int(sketch.ConstraintCount),
            "open_vertex_count": len(tuple(sketch.OpenVertices)),
        }
    except (AttributeError, IndexError, TypeError, ValueError, OverflowError):
        _fail("flatface_sketch_verification/edited_geometry")
    if (
        facts["count"] != 1
        or facts["type_id"] != "Part::GeomCircle"
        or facts["center"] != [2.0, 3.0, 0.0]
        or facts["axis"] != [0.0, 0.0, 1.0]
        or facts["radius_mm"] != 1.0
        or facts["construction"] is not False
        or facts["constraint_count"] != 0
        or facts["open_vertex_count"] != 0
    ):
        _fail("flatface_sketch_verification/edited_geometry")
    return _sha(b"vibecad-flatface-sketch-edited-geometry-v1\0", _canonical(facts))


def _apply_flatface(
    *, document: object, body: object, base: object, plan: FlatFaceSketchBackendPlan
) -> tuple[object, object]:
    receipt = apply_flatface_sketch_plan(
        plan.canonical_bytes,
        expected_content_sha256=hashlib.sha256(plan.canonical_bytes).hexdigest(),
        expected_plan_sha256=plan.plan_sha256,
        bindings=FlatFaceSketchExecutionBindings(
            document=document,
            body=body,
            base=base,
            body_id=plan.body_id,
            base_node_id=plan.base_node_id,
            base_result_id=plan.base_result_id,
        ),
    )
    sketch = document.getObject(receipt.object_name)
    if sketch is None:
        _fail("flatface_sketch_verification/create")
    return receipt, sketch


def _late_rollback(
    *, freecad: object, part: object, plan: FlatFaceSketchBackendPlan
) -> dict[str, object]:
    owned: dict[str, object] = {}
    try:
        document, body, base = _new_base(
            freecad=freecad,
            part=part,
            name="VerifyFlatFaceSketchLateRollback",
        )
        owned[document.Name] = document
        before_objects = tuple(document.Objects)
        before_group = tuple(body.Group)
        before_tip = body.Tip
        before_visibility = tuple(bool(item.Visibility) for item in before_objects)
        original_geometry_digest = flatface_rules._geometry_sha256  # noqa: SLF001
        late_validation_reached = False

        def fail_after_mutation(_sketch: object) -> str:
            nonlocal late_validation_reached
            late_validation_reached = True
            raise FlatFaceSketchRuleError(
                FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED,
                "/verification/injected-late-geometry-digest",
            )

        flatface_rules._geometry_sha256 = fail_after_mutation  # type: ignore[assignment] # noqa: SLF001
        try:
            _apply_flatface(document=document, body=body, base=base, plan=plan)
        except FlatFaceSketchRuleError:
            pass
        else:
            _fail("flatface_sketch_verification/late_rollback")
        finally:
            flatface_rules._geometry_sha256 = original_geometry_digest  # type: ignore[assignment] # noqa: SLF001
        if (
            not late_validation_reached
            or not _same_object_sequence(document.Objects, before_objects)
            or not _same_object_sequence(body.Group, before_group)
            or body.Tip is not before_tip
            or tuple(bool(item.Visibility) for item in tuple(document.Objects)) != before_visibility
            or bool(document.HasPendingTransaction)
        ):
            _fail("flatface_sketch_verification/late_rollback")
        return {
            "injection_point": "geometry-digest-after-native-create",
            "native_mutation_reached": True,
            "object_sequence_restored": True,
            "body_group_restored": True,
            "body_tip_restored": True,
            "visibility_restored": True,
            "pending_transaction": False,
        }
    finally:
        _close_owned_documents(freecad, owned)


def _case_observation(
    *,
    case: ReviewedConformanceCase,
    challenge_sha256: str,
    evidence: dict[str, object],
) -> bytes:
    operation = FLATFACE_SKETCH_FAMILY_MANIFEST.operations[0]
    if (
        type(case) is not ReviewedConformanceCase
        or case.operation_id != operation.operation_id
        or type(challenge_sha256) is not str
        or len(challenge_sha256) != 64
        or any(character not in "0123456789abcdef" for character in challenge_sha256)
        or type(evidence) is not dict
    ):
        _fail("flatface_sketch_verification/observation")
    body = {
        "case_sha256": case.case_sha256,
        "challenge_sha256": challenge_sha256,
        "evidence": evidence,
    }
    return _canonical(
        {
            "authority": "none",
            "case_contract_sha256": case.case_contract_sha256,
            "case_sha256": case.case_sha256,
            "challenge_sha256": challenge_sha256,
            "evidence": evidence,
            "facet": case.facet.value,
            "harness_contract_sha256": _HARNESS_CONTRACT_SHA256,
            "observation_schema": 1,
            "observation_sha256": _sha(_OBSERVATION_DOMAIN, _canonical(body)),
            "operation_id": case.operation_id,
        }
    )


class _FlatFaceSketchExecutor:
    __slots__ = ("_cache", "_freecad")

    def __init__(self, freecad: object) -> None:
        self._freecad = freecad
        self._cache: dict[ReviewedConformanceFacet, dict[str, object]] | None = None

    def __call__(self, case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
        operation = FLATFACE_SKETCH_FAMILY_MANIFEST.operations[0]
        if (
            type(case) is not ReviewedConformanceCase
            or case.operation_id != operation.operation_id
            or case not in FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST.cases
        ):
            _fail("flatface_sketch_verification/case")
        if self._cache is None:
            self._cache = self._run()
        evidence = self._cache.get(case.facet)
        if evidence is None:
            _fail("flatface_sketch_verification/case")
        return _case_observation(
            case=case,
            challenge_sha256=challenge_sha256,
            evidence=evidence,
        )

    def _run(self) -> dict[ReviewedConformanceFacet, dict[str, object]]:
        try:
            import Part  # type: ignore[import-not-found]  # noqa: PLC0415
        except (Exception, SystemExit) as error:
            raise RuntimeError("managed FreeCAD Part unavailable") from error

        freecad = self._freecad
        owned: dict[str, object] = {}
        outcomes: dict[ReviewedConformanceFacet, dict[str, object]] = {}
        plan = _flatface_plan()
        try:
            with tempfile.TemporaryDirectory(prefix="vibecad-flatface-sketch-") as temporary:
                document, body, base = _new_base(
                    freecad=freecad,
                    part=Part,
                    name="VerifyFlatFaceSketch",
                )
                owned[document.Name] = document

                before_negative = tuple(document.Objects)
                before_negative_group = tuple(body.Group)
                before_negative_tip = body.Tip
                before_negative_visibility = tuple(
                    bool(item.Visibility) for item in before_negative
                )
                try:
                    apply_flatface_sketch_plan(
                        plan.canonical_bytes + b" ",
                        expected_content_sha256=hashlib.sha256(plan.canonical_bytes).hexdigest(),
                        expected_plan_sha256=plan.plan_sha256,
                        bindings=FlatFaceSketchExecutionBindings(
                            document=document,
                            body=body,
                            base=base,
                            body_id=plan.body_id,
                            base_node_id=plan.base_node_id,
                            base_result_id=plan.base_result_id,
                        ),
                    )
                except FlatFaceSketchRuleError:
                    pass
                else:
                    _fail("flatface_sketch_verification/negative")
                if (
                    not _same_object_sequence(document.Objects, before_negative)
                    or not _same_object_sequence(body.Group, before_negative_group)
                    or body.Tip is not before_negative_tip
                    or tuple(bool(item.Visibility) for item in tuple(document.Objects))
                    != before_negative_visibility
                    or bool(document.HasPendingTransaction)
                ):
                    _fail("flatface_sketch_verification/negative")
                outcomes[ReviewedConformanceFacet.NEGATIVE] = {
                    "tamper": "append-noncanonical-byte",
                    "mutation_count": 0,
                    "body_group_preserved": True,
                    "body_tip_preserved": True,
                    "visibility_preserved": True,
                    "pending_transaction": False,
                }

                receipt, sketch = _apply_flatface(
                    document=document,
                    body=body,
                    base=base,
                    plan=plan,
                )
                topology = _primary_topology(body, base, sketch)
                digests = _primary_digests(body, base, sketch, receipt.selection)
                if digests != {
                    "state_sha256": receipt.state_sha256,
                    "shape_sha256": receipt.shape_sha256,
                    "geometry_sha256": receipt.geometry_sha256,
                }:
                    _fail("flatface_sketch_verification/create")
                outcomes[ReviewedConformanceFacet.CREATE] = {
                    "plan_sha256": plan.plan_sha256,
                    "native_receipt_sha256": receipt.receipt_sha256,
                    "selection_sha256": receipt.selection.geometric_signature_sha256,
                    "face_brep_sha256": receipt.selection.face_brep_sha256,
                    "base_brep_sha256": receipt.selection.base_brep_sha256,
                    "topology": topology,
                    "primary_digests": digests,
                }

                document.recompute()
                recomputed = _primary_digests(body, base, sketch, receipt.selection)
                if recomputed != digests or _primary_topology(body, base, sketch) != topology:
                    _fail("flatface_sketch_verification/recompute")
                outcomes[ReviewedConformanceFacet.RECOMPUTE] = {
                    "primary_digests": recomputed,
                    "topology_preserved": True,
                }

                path = Path(temporary) / "flatface-sketch.FCStd"
                document.saveAs(str(path))
                if not path.is_file() or path.stat().st_size <= 0:
                    _fail("flatface_sketch_verification/save")
                outcomes[ReviewedConformanceFacet.SAVE] = {
                    "format": "FCStd",
                    "nonempty": True,
                    "body_group_names": list(receipt.group_after_names),
                }
                freecad.closeDocument(document.Name)

                reopened = freecad.openDocument(str(path))
                owned[reopened.Name] = reopened
                reopened.recompute()
                reopened_body = reopened.getObject(receipt.body_name)
                reopened_base = reopened.getObject(receipt.base_name)
                reopened_sketch = reopened.getObject(receipt.object_name)
                if reopened_body is None or reopened_base is None or reopened_sketch is None:
                    _fail("flatface_sketch_verification/reopen")
                _face, _label, reopened_selection = flatface_rules.select_unique_zmax_planar_face(
                    reopened_base
                )
                reopened_topology = _primary_topology(reopened_body, reopened_base, reopened_sketch)
                reopened_digests = _primary_digests(
                    reopened_body,
                    reopened_base,
                    reopened_sketch,
                    reopened_selection,
                )
                if (
                    reopened_selection != receipt.selection
                    or reopened_topology != topology
                    or reopened_digests != digests
                ):
                    _fail("flatface_sketch_verification/reopen")
                outcomes[ReviewedConformanceFacet.REOPEN] = {
                    "selection_sha256": reopened_selection.geometric_signature_sha256,
                    "topology": reopened_topology,
                    "primary_digests": reopened_digests,
                }
                freecad.closeDocument(reopened.Name)

                edit_document, edit_body, edit_base = _new_base(
                    freecad=freecad,
                    part=Part,
                    name="VerifyFlatFaceSketchEdit",
                )
                owned[edit_document.Name] = edit_document
                edit_receipt, edit_sketch = _apply_flatface(
                    document=edit_document,
                    body=edit_body,
                    base=edit_base,
                    plan=plan,
                )
                before_edit_shape = flatface_rules._shape_sha256(edit_sketch)  # noqa: SLF001
                before_edit_geometry = edit_receipt.geometry_sha256
                edit_sketch.moveGeometry(0, 3, freecad.Vector(2.0, 3.0, 0.0))
                edit_document.recompute()
                after_edit_shape = flatface_rules._shape_sha256(edit_sketch)  # noqa: SLF001
                after_edit_geometry = _edited_geometry_sha256(edit_sketch)
                edit_topology = _primary_topology(edit_body, edit_base, edit_sketch)
                if (
                    after_edit_shape == before_edit_shape
                    or after_edit_geometry == before_edit_geometry
                ):
                    _fail("flatface_sketch_verification/edit")
                outcomes[ReviewedConformanceFacet.EDIT] = {
                    "strategy": "move-circle-center",
                    "before_shape_sha256": before_edit_shape,
                    "after_shape_sha256": after_edit_shape,
                    "before_geometry_sha256": before_edit_geometry,
                    "after_geometry_sha256": after_edit_geometry,
                    "topology": edit_topology,
                }
                freecad.closeDocument(edit_document.Name)

                outcomes[ReviewedConformanceFacet.LATE_ROLLBACK] = _late_rollback(
                    freecad=freecad,
                    part=Part,
                    plan=plan,
                )
        finally:
            _close_owned_documents(freecad, owned)
        if set(outcomes) != set(ReviewedConformanceFacet):
            _fail("flatface_sketch_verification/outcomes")
        return outcomes


def build_flatface_sketch_managed_verification(
    *, freecad: object
) -> tuple[ReviewedVerificationReceipt, FreeCadPromotionVerificationBinding]:
    """Run the exact one-operation/seven-facet managed verification matrix."""

    if not _VERIFICATION_LOCK.acquire(blocking=False):
        _fail("flatface_sketch_verification/concurrent_verification")
    try:
        host = build_managed_freecad_conformance_host(
            freecad=freecad,
            case_manifest=FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST,
            execute_case=_FlatFaceSketchExecutor(freecad),
            verifier_id=FLATFACE_SKETCH_VERIFIER_ID,
            verifier_version=FLATFACE_SKETCH_VERIFIER_VERSION,
        )
        receipt = build_reviewed_verification_receipt(
            manifest=FLATFACE_SKETCH_FAMILY_MANIFEST,
            case_manifest=FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST,
            host=host,
        )
        return receipt, build_promotion_verification_binding(receipt)
    finally:
        _VERIFICATION_LOCK.release()


def run_flatface_sketch_hole_managed_gate() -> dict[str, object]:
    """Run rollback then base -> FlatFace Circle Sketch -> through Hole once."""

    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415
        import Part  # type: ignore[import-not-found]  # noqa: PLC0415
    except (Exception, SystemExit) as error:
        raise RuntimeError("managed FreeCAD unavailable") from error

    version = tuple(FreeCAD.Version())
    document_name = "VibeCADFlatFaceSketchHoleGate"
    if document_name in FreeCAD.listDocuments():
        FreeCAD.closeDocument(document_name)
    document = FreeCAD.newDocument(document_name)
    rollback_verified = False
    chain_verified = False
    receipt = None
    hole_receipt = None
    try:
        document.UndoMode = 1
        body = document.addObject("PartDesign::Body", "ReviewedBody")
        base = body.newObject("PartDesign::Feature", "ReviewedBase")
        base.Shape = Part.makeBox(10.0, 10.0, 10.0)
        body.Tip = base
        base.Visibility = True
        document.recompute()
        before_objects = tuple(document.Objects)
        before_group = tuple(body.Group)
        before_tip = body.Tip
        before_visibility = tuple(bool(item.Visibility) for item in before_objects)

        flatface_plan = _flatface_plan()
        flatface_content_sha256 = hashlib.sha256(flatface_plan.canonical_bytes).hexdigest()
        flatface_bindings = FlatFaceSketchExecutionBindings(
            document=document,
            body=body,
            base=base,
            body_id=flatface_plan.body_id,
            base_node_id=flatface_plan.base_node_id,
            base_result_id=flatface_plan.base_result_id,
        )

        original_geometry_digest = flatface_rules._geometry_sha256  # noqa: SLF001

        def fail_after_mutation(_sketch: object) -> str:
            raise FlatFaceSketchRuleError(
                FlatFaceSketchRuleErrorCode.CONFORMANCE_FAILED,
                "/gate/late_failure",
            )

        flatface_rules._geometry_sha256 = fail_after_mutation  # type: ignore[assignment] # noqa: SLF001
        try:
            apply_flatface_sketch_plan(
                flatface_plan.canonical_bytes,
                expected_content_sha256=flatface_content_sha256,
                expected_plan_sha256=flatface_plan.plan_sha256,
                bindings=flatface_bindings,
            )
        except FlatFaceSketchRuleError as error:
            if error.path != "/gate/late_failure":
                raise
        else:
            raise RuntimeError("late rollback gate did not fail")
        finally:
            flatface_rules._geometry_sha256 = original_geometry_digest  # type: ignore[assignment] # noqa: SLF001

        rollback_verified = (
            tuple(document.Objects) == before_objects
            and tuple(body.Group) == before_group
            and body.Tip is before_tip
            and tuple(bool(item.Visibility) for item in tuple(document.Objects))
            == before_visibility
        )
        if not rollback_verified:
            raise RuntimeError("late rollback did not restore document state")

        receipt = apply_flatface_sketch_plan(
            flatface_plan.canonical_bytes,
            expected_content_sha256=flatface_content_sha256,
            expected_plan_sha256=flatface_plan.plan_sha256,
            bindings=flatface_bindings,
        )
        sketch = document.getObject(receipt.object_name)
        if (
            sketch is None
            or body.Tip is not sketch
            or sketch.MapMode != "FlatFace"
            or tuple(sketch.AttachmentSupport)[0][0] is not base
            or re.search(r"Face[1-9][0-9]*", repr(receipt.selection)) is not None
        ):
            raise RuntimeError("FlatFace Sketch conformance failed")

        hole_plan = _hole_plan()
        hole_receipt = apply_partdesign_residual_plan(
            hole_plan.canonical_bytes,
            expected_content_sha256=hashlib.sha256(hole_plan.canonical_bytes).hexdigest(),
            expected_plan_sha256=hole_plan.plan_sha256,
            bindings=PartDesignResidualExecutionBindings(
                document=document,
                body=body,
                body_id=hole_plan.body_id,
                base=AuthenticatedResidualObject(
                    object=base,
                    node_id=hole_plan.base.node_id,
                    result_id=hole_plan.base.result_id,
                ),
                profile=AuthenticatedResidualObject(
                    object=sketch,
                    node_id=hole_plan.profile.node_id,
                    result_id=hole_plan.profile.result_id,
                ),
            ),
        )
        hole = document.getObject(hole_receipt.object_name)
        chain_verified = (
            hole is not None
            and body.Tip is hole
            and hole.Profile[0] is sketch
            and hole.BaseFeature is base
            and hole.Shape.isValid()
            and len(tuple(hole.Shape.Solids)) == 1
            and float(hole.Shape.Volume) < float(base.Shape.Volume)
        )
        if not chain_verified:
            raise RuntimeError("Hole chain conformance failed")
        return {
            "verifier_id": FLATFACE_SKETCH_HOLE_VERIFIER_ID,
            "verifier_version": FLATFACE_SKETCH_HOLE_VERIFIER_VERSION,
            "freecad_version": list(version[:3]),
            "freecad_build_id": version[7],
            "rollback_verified": rollback_verified,
            "chain_verified": chain_verified,
            "flatface_plan_sha256": flatface_plan.plan_sha256,
            "flatface_receipt_sha256": receipt.receipt_sha256,
            "face_signature_sha256": receipt.selection.geometric_signature_sha256,
            "face_brep_sha256": receipt.selection.face_brep_sha256,
            "base_brep_sha256": receipt.selection.base_brep_sha256,
            "hole_plan_sha256": hole_plan.plan_sha256,
            "hole_receipt_sha256": hole_receipt.receipt_sha256,
        }
    finally:
        FreeCAD.closeDocument(document_name)
        if document_name in FreeCAD.listDocuments():
            raise RuntimeError("verification document cleanup failed")


__all__ = [
    "FLATFACE_SKETCH_REVIEWED_HOST_CASE_MANIFEST",
    "FLATFACE_SKETCH_HOLE_VERIFIER_ID",
    "FLATFACE_SKETCH_HOLE_VERIFIER_VERSION",
    "FLATFACE_SKETCH_VERIFIER_ID",
    "FLATFACE_SKETCH_VERIFIER_VERSION",
    "build_flatface_sketch_managed_verification",
    "run_flatface_sketch_hole_managed_gate",
]
