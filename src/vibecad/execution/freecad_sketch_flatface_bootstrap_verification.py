"""Private one-process FreeCAD gate for FlatFace Sketch -> Hole.

This verifier is deliberately not wired into release attestation.  It proves
the family seam, one real rollback, the native Hole consumer, and document
cleanup for the exact managed FreeCAD build.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

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
    "FLATFACE_SKETCH_HOLE_VERIFIER_ID",
    "FLATFACE_SKETCH_HOLE_VERIFIER_VERSION",
    "run_flatface_sketch_hole_managed_gate",
]
