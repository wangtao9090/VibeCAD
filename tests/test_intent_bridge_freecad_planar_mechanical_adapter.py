"""Focused lowering and one batched real-FreeCAD gate for PM1."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_intent_rules_planar_mechanical_v1 import (  # exact accepted PM1 fixture
    _document as _visual_document,
)
from tests.test_intent_rules_planar_mechanical_v1 import _graph as _visual_graph
from tests.test_intent_rules_planar_mechanical_v1 import _Reader as _VisualReader
from tests.test_intent_rules_planar_mechanical_v1 import _request as _compile_request
from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BridgeBudget,
    BridgeDisposition,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
)
from vibecad.intent_bridge.freecad_planar_mechanical_adapter import (
    FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
    PLANAR_REQUEST_TERMS,
    FreeCADPlanarMechanicalAdapter,
    build_planar_mechanical_capability_document,
)
from vibecad.intent_bridge.ports import IntentBackendAdapter, TrustedCodecRegistry
from vibecad.intent_bridge.sketch_intent_graph_codec import SketchIntentGraphCodec
from vibecad.intent_compiler.artifacts import InMemoryIntentArtifactPublisher
from vibecad.intent_rules.planar_mechanical_v1.catalog import (
    build_planar_mechanical_v1_stack,
)
from vibecad.parametric.freecad_planar_mechanical_rules import (
    PLANAR_MECHANICAL_RULE_CONTRACT_SHA256,
    PlanarMechanicalRuleError,
    decode_planar_mechanical_plan,
)


class _Artifacts:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        payload = self.payloads[document.artifact_id]
        assert len(payload) <= maximum_bytes
        return payload


class _MemoryPlanSink:
    def __init__(self, *, fail: bool = False, corrupt_readback: bool = False) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}
        self.fail = fail
        self.corrupt_readback = corrupt_readback

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        if self.fail:
            raise RuntimeError("injected plan publication failure")
        assert len(payload) == document.size_bytes
        assert hashlib.sha256(payload).hexdigest() == document.content_sha256
        current = self.items.get(document.artifact_id)
        if current is not None:
            assert current == (document, payload)
        else:
            self.items[document.artifact_id] = (document, payload)
        return payload + b" " if self.corrupt_readback else payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        stored, payload = self.items[document.artifact_id]
        assert stored == document and len(payload) <= maximum_bytes
        return payload


def _compiled(circle_count: int):
    graph = _visual_graph(circle_count)
    visual_document, visual_payload = _visual_document(graph)
    publisher = InMemoryIntentArtifactPublisher()
    stack = build_planar_mechanical_v1_stack(publisher=publisher)
    compile_request = _compile_request(stack.compiler, visual_document, len(visual_payload))
    result = stack.compiler.compile(
        compile_request,
        artifacts=_VisualReader(visual_payload),
        codecs=stack.codecs,
        proof_policy=stack.proof_policy,
    )
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.proof_bundle is not None
    payloads = {visual_document.artifact_id: visual_payload}
    for document in result.output_documents:
        payloads[document.artifact_id] = publisher.read(document, 512 * 1024)
    return result, stack, payloads


def _lowering_fixture(
    circle_count: int,
    *,
    max_output_bytes: int = 128 * 1024,
):
    compile_result, stack, payloads = _compiled(circle_count)
    capability_document, capability_payload = build_planar_mechanical_capability_document()
    payloads[capability_document.artifact_id] = capability_payload
    assert compile_result.proof_bundle is not None
    proof_bytes = sum(item.size_bytes for item in compile_result.proof_bundle.documents)
    request = BackendLoweringRequest(
        adapter=FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR,
        terms=PLANAR_REQUEST_TERMS,
        documents=(*compile_result.output_documents, capability_document),
        intent_artifact_ids=tuple(item.artifact_id for item in compile_result.output_documents),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=compile_result.proof_bundle,
        budget=BridgeBudget(
            max_input_bytes=proof_bytes + len(capability_payload),
            max_output_bytes=max_output_bytes,
            max_subject_lookups=6,
            max_rule_applications=2,
        ),
    )
    return request, _Artifacts(payloads), stack


def _lower(circle_count: int, *, sink: _MemoryPlanSink | None = None):
    request, reader, stack = _lowering_fixture(circle_count)
    sink = sink or _MemoryPlanSink()
    adapter = FreeCADPlanarMechanicalAdapter(sink)
    result, receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=stack.codecs,
        proof_policy=stack.proof_policy,
    )
    plan, payload = adapter.read_plan(receipt)
    return result, receipt, plan, payload, adapter, sink, request, reader, stack


@pytest.mark.parametrize("circle_count", [0, 1, 16])
def test_exact_pm1_outputs_lower_to_one_deterministic_authority_free_plan(
    circle_count: int,
) -> None:
    result, receipt, plan, payload, adapter, sink, request, reader, stack = _lower(circle_count)

    assert isinstance(adapter, IntentBackendAdapter)
    assert result.disposition is BridgeDisposition.COMPLETE
    assert result.plan_document is not None
    assert len(result.supported_subjects) == 2
    assert len(plan.circles) == circle_count
    assert plan.final_node_id == (
        "node.add" if circle_count == 0 else f"node.remove.{circle_count - 1:03d}"
    )
    assert plan.final_result_id == (
        "result.add.solid" if circle_count == 0 else f"result.remove.{circle_count - 1:03d}.solid"
    )
    assert plan.depth_mm == 8.0
    assert plan.expected_volume_mm3 > 0.0
    assert not plan.executable and not plan.grants_execution_authority
    assert not receipt.executable and not receipt.grants_execution_authority
    assert all(
        token not in payload for token in (b"PartDesign::", b"Sketcher::", b"TypeId", b"Profile")
    )
    assert b"pad_then_pocket_through_all" in payload

    repeated_result, repeated_receipt = adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=stack.codecs,
        proof_policy=stack.proof_policy,
    )
    assert repeated_result == result and repeated_receipt == receipt
    assert len(sink.items) == 1


def test_capability_codec_proof_and_source_tamper_are_rejected_before_publication() -> None:
    request, reader, stack = _lowering_fixture(1)
    capability_id = request.capability_artifact_ids[0]
    reader.payloads[capability_id] += b" "
    sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as capability_error:
        FreeCADPlanarMechanicalAdapter(sink).lower(
            request,
            artifacts=reader,
            codecs=stack.codecs,
            proof_policy=stack.proof_policy,
        )
    assert capability_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}

    request, reader, stack = _lowering_fixture(1)
    parametric_id = next(item for item in request.intent_artifact_ids if "parametric" in item)
    payload = reader.payloads[parametric_id]
    reader.payloads[parametric_id] = payload[:-1] + bytes([payload[-1] ^ 1])
    with pytest.raises(IntentBridgeError) as source_error:
        FreeCADPlanarMechanicalAdapter(sink).lower(
            request,
            artifacts=reader,
            codecs=stack.codecs,
            proof_policy=stack.proof_policy,
        )
    assert source_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
    assert sink.items == {}

    request, reader, stack = _lowering_fixture(1)
    with pytest.raises(IntentBridgeError) as codec_error:
        FreeCADPlanarMechanicalAdapter(sink).lower(
            request,
            artifacts=reader,
            codecs=TrustedCodecRegistry((SketchIntentGraphCodec(),)),
            proof_policy=stack.proof_policy,
        )
    assert codec_error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
    assert sink.items == {}


def test_atomic_sink_failure_non_exact_readback_and_n_plus_one_budget_leave_zero_plan() -> None:
    request, reader, stack = _lowering_fixture(1)
    rule_limited = dataclasses.replace(
        request,
        budget=dataclasses.replace(request.budget, max_rule_applications=1),
    )
    rule_limited_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as rule_budget_error:
        FreeCADPlanarMechanicalAdapter(rule_limited_sink).lower(
            rule_limited,
            artifacts=reader,
            codecs=stack.codecs,
            proof_policy=stack.proof_policy,
        )
    assert rule_budget_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert rule_limited_sink.items == {}

    failed = _MemoryPlanSink(fail=True)
    with pytest.raises(IntentBridgeError):
        FreeCADPlanarMechanicalAdapter(failed).lower(
            request,
            artifacts=reader,
            codecs=stack.codecs,
            proof_policy=stack.proof_policy,
        )
    assert failed.items == {}

    corrupt = _MemoryPlanSink(corrupt_readback=True)
    with pytest.raises(IntentBridgeError) as readback_error:
        FreeCADPlanarMechanicalAdapter(corrupt).lower(
            request,
            artifacts=reader,
            codecs=stack.codecs,
            proof_policy=stack.proof_policy,
        )
    assert readback_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    result, *_ = _lower(1)
    assert result.plan_document is not None
    size = result.plan_document.size_bytes
    exact_request, exact_reader, exact_stack = _lowering_fixture(1, max_output_bytes=size)
    exact_sink = _MemoryPlanSink()
    exact_result = FreeCADPlanarMechanicalAdapter(exact_sink).lower(
        exact_request,
        artifacts=exact_reader,
        codecs=exact_stack.codecs,
        proof_policy=exact_stack.proof_policy,
    )
    assert exact_result.plan_document is not None
    assert exact_result.plan_document.size_bytes == size

    small_request, small_reader, small_stack = _lowering_fixture(1, max_output_bytes=size - 1)
    small_sink = _MemoryPlanSink()
    with pytest.raises(IntentBridgeError) as budget_error:
        FreeCADPlanarMechanicalAdapter(small_sink).lower(
            small_request,
            artifacts=small_reader,
            codecs=small_stack.codecs,
            proof_policy=small_stack.proof_policy,
        )
    assert budget_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert small_sink.items == {}


def test_plan_decoder_is_canonical_bounded_and_content_bound() -> None:
    result, _receipt, plan, payload, *_ = _lower(1)
    assert result.plan_document is not None
    assert (
        decode_planar_mechanical_plan(
            payload,
            expected_content_sha256=result.plan_document.content_sha256,
            expected_plan_sha256=result.plan_document.document_digest,
        )
        == plan
    )
    with pytest.raises(PlanarMechanicalRuleError):
        decode_planar_mechanical_plan(payload + b" ")

    duplicate = payload.replace(b'"authority":"none"', b'"authority":"none","authority":"none"')
    with pytest.raises(PlanarMechanicalRuleError):
        decode_planar_mechanical_plan(duplicate)

    mapping = plan.to_mapping()
    mapping["geometry"]["depth_mm"] = 10**4000
    adversarial = json.dumps(
        mapping,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    with pytest.raises(PlanarMechanicalRuleError) as error:
        decode_planar_mechanical_plan(adversarial)
    assert len(str(error.value)) < 180

    circle = plan.circles[0]
    with pytest.raises(PlanarMechanicalRuleError):
        dataclasses.replace(
            plan,
            circles=(dataclasses.replace(circle, center_x_mm=10_000.0),),
        )


def test_rule_and_adapter_contracts_are_stable_content_digests() -> None:
    assert len(PLANAR_MECHANICAL_RULE_CONTRACT_SHA256) == 64
    assert len(FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256) == 64
    assert PLANAR_MECHANICAL_RULE_CONTRACT_SHA256 != (
        FREECAD_PLANAR_MECHANICAL_ADAPTER_DESCRIPTOR.adapter_contract_sha256
    )


@pytest.mark.slow
def test_real_freecad_batch_zero_one_sixteen_edit_save_reopen_and_rollback(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD batch gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")

    cases = []
    for circle_count in (0, 1, 16):
        result, _receipt, plan, payload, *_ = _lower(circle_count)
        assert result.plan_document is not None
        path = tmp_path / f"pm1-{circle_count}.json"
        path.write_bytes(payload)
        cases.append(
            {
                "circle_count": circle_count,
                "path": str(path),
                "content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
            }
        )
    model_path = tmp_path / "pm1-planar-mechanical.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import os, sys
sys.path.insert(0, {str(source_root)!r})
from vibecad.freecad_env import prepare_freecad_import
prepare_freecad_import()
from pathlib import Path
import FreeCAD
from vibecad.parametric.freecad_planar_mechanical_rules import (
    PlanarMechanicalExecutionBindings,
    PlanarMechanicalRuleError,
    apply_planar_mechanical_plan,
)

CASES = {cases!r}
MODEL = {str(model_path)!r}
document = FreeCAD.newDocument('PM1PlanarMechanicalBatch')
document.UndoMode = 1
persisted = []
for entry in CASES:
    payload = Path(entry['path']).read_bytes()
    receipt = apply_planar_mechanical_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=PlanarMechanicalExecutionBindings(document=document),
    )
    body = document.getObject(receipt.body_name)
    outer = document.getObject(receipt.outer_sketch_name)
    pad = document.getObject(receipt.pad_name)
    sketches = tuple(document.getObject(name) for name in receipt.circle_sketch_names)
    pockets = tuple(document.getObject(name) for name in receipt.pocket_names)
    assert body.TypeId == 'PartDesign::Body'
    assert outer.TypeId == 'Sketcher::SketchObject'
    assert pad.TypeId == 'PartDesign::Pad' and pad.Profile[0] is outer
    assert len(sketches) == len(pockets) == entry['circle_count']
    assert all(item.TypeId == 'PartDesign::Pocket' for item in pockets)
    assert all(item.Type == 'ThroughAll' for item in pockets)
    assert all(item in body.Group for item in (outer, pad, *sketches, *pockets))
    previous = pad
    for sketch, pocket in zip(sketches, pockets, strict=True):
        assert pocket.Profile[0] is sketch and pocket.BaseFeature is previous
        previous = pocket
    assert body.Tip is previous
    before_edit = float(body.Tip.Shape.Volume)
    if entry['circle_count'] == 1:
        sketches[0].setDatum(0, FreeCAD.Units.Quantity('4 mm'))
    else:
        pad.Length = 9.0 if entry['circle_count'] == 16 else 10.0
    document.recompute()
    after_edit = float(body.Tip.Shape.Volume)
    assert body.Tip.isValid() and len(body.Tip.Shape.Solids) == 1
    assert abs(after_edit - before_edit) > 1e-6
    persisted.append((
        receipt.body_name,
        receipt.outer_sketch_name,
        receipt.pad_name,
        receipt.circle_sketch_names,
        receipt.pocket_names,
        after_edit,
    ))

document.saveAs(MODEL)
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument(MODEL)
reopened.recompute()
for body_name, outer_name, pad_name, sketch_names, pocket_names, volume in persisted:
    body = reopened.getObject(body_name)
    outer = reopened.getObject(outer_name)
    pad = reopened.getObject(pad_name)
    sketches = tuple(reopened.getObject(name) for name in sketch_names)
    pockets = tuple(reopened.getObject(name) for name in pocket_names)
    assert body.TypeId == 'PartDesign::Body'
    assert pad.TypeId == 'PartDesign::Pad' and pad.Profile[0] is outer
    assert all(item.TypeId == 'PartDesign::Pocket' for item in pockets)
    assert all(item.Type == 'ThroughAll' for item in pockets)
    assert body.Tip is (pockets[-1] if pockets else pad)
    assert abs(float(body.Tip.Shape.Volume) - volume) < 1e-6
FreeCAD.closeDocument(reopened.Name)

# Tampered bytes and a mismatched digest both fail before creating any object.
invalid = FreeCAD.newDocument('PM1PlanarMechanicalInvalid')
invalid.UndoMode = 1
entry = CASES[1]
payload = Path(entry['path']).read_bytes()
for bad_payload, content_digest in (
    (payload + b' ', entry['content_sha256']),
    (payload, '0' * 64),
):
    before = tuple(invalid.Objects)
    try:
        apply_planar_mechanical_plan(
            bad_payload,
            expected_content_sha256=content_digest,
            expected_plan_sha256=entry['plan_sha256'],
            bindings=PlanarMechanicalExecutionBindings(document=invalid),
        )
    except PlanarMechanicalRuleError:
        pass
    else:
        raise AssertionError('tampered plan must fail')
    assert tuple(invalid.Objects) == before and not invalid.HasPendingTransaction
FreeCAD.closeDocument(invalid.Name)

# A real FreeCAD transaction is faulted after native objects exist; abort must
# restore the exact pre-transaction object list and pending-transaction state.
rollback = FreeCAD.newDocument('PM1PlanarMechanicalRollback')
rollback.UndoMode = 1
class FaultOnceDocument:
    def __init__(self, inner):
        self.inner = inner
        self.fired = False
    def __getattr__(self, name):
        return getattr(self.inner, name)
    def recompute(self):
        if not self.fired:
            self.fired = True
            raise RuntimeError('injected recompute failure')
        return self.inner.recompute()
before = tuple(rollback.Objects)
try:
    apply_planar_mechanical_plan(
        payload,
        expected_content_sha256=entry['content_sha256'],
        expected_plan_sha256=entry['plan_sha256'],
        bindings=PlanarMechanicalExecutionBindings(document=FaultOnceDocument(rollback)),
    )
except PlanarMechanicalRuleError:
    pass
else:
    raise AssertionError('injected native failure must fail')
assert tuple(rollback.Objects) == before and not rollback.HasPendingTransaction
FreeCAD.closeDocument(rollback.Name)
print('REAL_PM1_PLANAR_MECHANICAL_BATCH_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_PM1_PLANAR_MECHANICAL_BATCH_OK" in completed.stdout
