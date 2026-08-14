"""Focused contracts and one real FreeCAD batch for authenticated file imports."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from vibecad.intent_bridge.contracts import (
    BackendLoweringRequest,
    BridgeBudget,
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    ProducerBinding,
    ProducerDescriptor,
    ProofAssertion,
    ProofBundle,
    ProofEndpoint,
    SubjectRef,
)
from vibecad.intent_bridge.freecad_part_file_import_adapter import (
    FREECAD_PART_FILE_IMPORT_ADAPTER_DESCRIPTOR,
    PART_FILE_IMPORT_ARTIFACT_LOCATOR_TERM,
    PART_FILE_IMPORT_ARTIFACT_ROLE_TERM,
    PART_FILE_IMPORT_FAMILY_TERM,
    PART_FILE_IMPORT_INTENT_ROLE_TERM,
    PART_FILE_IMPORT_MANIFEST,
    PART_FILE_IMPORT_OPERATION_TERMS,
    PART_FILE_IMPORT_PFG_TERMS,
    PART_FILE_IMPORT_REQUEST_TERMS,
    PART_FILE_IMPORT_RESULT_ROLE_TERM,
    PART_FILE_IMPORT_SHAPE_TYPE_TERM,
    PART_FILE_IMPORT_STRUCTURE_TERM,
    FreeCADPartFileImportAdapter,
    build_part_file_import_artifact_document,
    build_part_file_import_capability_document,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PFG_SELECTOR_FEATURE_NODE,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import TrustedCodecRegistry
from vibecad.intent_bridge.trusted_proof_policy import (
    RuleEndpointSignature,
    TrustedRuleEvaluation,
    TrustedRuleEvaluatorDescriptor,
    TrustedRulePolicy,
)
from vibecad.parametric.feature_graph_v2 import (
    FeatureBodyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphError,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
)
from vibecad.parametric.freecad_part_file_import_rules import (
    MAX_PART_FILE_IMPORT_PLAN_BYTES,
    PART_FILE_IMPORT_NATIVE_SPECS,
    HostOwnedImportStager,
    PartFileImportBackendPlan,
    PartFileImportOperation,
    PartFileImportRuleError,
    decode_part_file_import_backend_plan,
)


def _sha(value: str | bytes) -> str:
    payload = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.part-file-import-proof-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_sha(f"proof:{term_id}"),
    )


def _as_bridge(term) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


RULE = _proof_term("rule_part_file_import_target", "rule.part-file-import-reviewed")
PREDICATE = _proof_term(
    "predicate_part_file_import_target",
    "predicate.part-file-import-reviewed",
)
PREMISE_ROLE = _proof_term(
    "role_part_file_import_candidate",
    "proof-role.part-file-import-candidate",
)
CONCLUSION_ROLE = _proof_term(
    "role_part_file_import_validated",
    "proof-role.part-file-import-validated",
)
STRUCTURE_BRIDGE = _as_bridge(PART_FILE_IMPORT_STRUCTURE_TERM)


class _Evaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="part_file_import_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("part-file-import-target-evaluator-v1"),
            rule_term=RULE,
            predicate_term=PREDICATE,
            premises=(signature(PREMISE_ROLE),),
            conclusions=(signature(CONCLUSION_ROLE),),
        )

    @property
    def descriptor(self) -> TrustedRuleEvaluatorDescriptor:
        return self._descriptor

    def validate(self, evaluation: TrustedRuleEvaluation) -> None:
        if (
            len(evaluation.documents) != 1
            or evaluation.premises[0].subject != _subject()
            or evaluation.conclusions[0].subject != _subject()
        ):
            raise IntentBridgeError(
                IntentBridgeErrorCode.AUTHORITY_VIOLATION,
                "/part_file_import_target",
            )


def _operation_terms(operation: PartFileImportOperation):
    return next(item for item in PART_FILE_IMPORT_OPERATION_TERMS if item.operation is operation)


def _graph(
    operation: PartFileImportOperation,
    artifact_content_sha256: str,
    *,
    artifact_id: str = "artifact_part_file_import_source",
    artifact_value_operation: PartFileImportOperation | None = None,
    operation_definition: str | None = None,
) -> ParametricFeatureGraphV2:
    operation_terms = _operation_terms(operation)
    value_terms = _operation_terms(artifact_value_operation or operation)
    terms = list(PART_FILE_IMPORT_PFG_TERMS)
    if operation_definition is not None:
        index = terms.index(operation_terms.operation_term)
        terms[index] = dataclasses.replace(
            operation_terms.operation_term,
            term_definition_sha256=operation_definition,
        )
    reference = SemanticReferenceV2(
        reference_id=artifact_id,
        scope=SemanticReferenceScope.EXTERNAL,
        semantic_role_term_ref_id=PART_FILE_IMPORT_ARTIFACT_ROLE_TERM.term_ref_id,
        value_type_term_ref_id=value_terms.artifact_value_type_term.term_ref_id,
        locator_term_ref_id=PART_FILE_IMPORT_ARTIFACT_LOCATOR_TERM.term_ref_id,
        source_content_sha256=artifact_content_sha256,
    )
    target = FeatureNodeV2(
        node_id="node_target",
        body_id="body_part_document",
        name=f"Reviewed Part {operation.value} import",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=PART_FILE_IMPORT_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=PART_FILE_IMPORT_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=operation_terms.operation_term.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_artifact",
                    semantic_role_term_ref_id=PART_FILE_IMPORT_ARTIFACT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=value_terms.artifact_value_type_term.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            references=(
                FeatureReferenceBindingV2(
                    binding_id="binding_artifact",
                    port_id="port_artifact",
                    reference_id=reference.reference_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_target",
                semantic_role_term_ref_id=PART_FILE_IMPORT_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=PART_FILE_IMPORT_SHAPE_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id=f"graph_part_file_import_{operation.value}",
        name=f"Part file import {operation.value}",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="body_part_document", name="Part document"),),
        parameters=(),
        references=(reference,),
        nodes=(target,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_target",
                node_id=target.node_id,
                result_id="result_target",
            ),
        ),
    )


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_part_file_import_pfg",
            role_term_ref_id=PART_FILE_IMPORT_INTENT_ROLE_TERM.term_ref_id,
            schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
            document_id=graph.graph_id,
            document_digest=graph.graph_sha256,
            content_sha256=_sha(payload),
            size_bytes=len(payload),
            media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
        ),
        payload,
    )


def _subject() -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_part_file_import_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_target",
    )


def _proof(policy: TrustedRulePolicy, document: DocumentRef) -> ProofBundle:
    return ProofBundle(
        terms=(
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
            STRUCTURE_BRIDGE,
            PART_FILE_IMPORT_INTENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_part_file_import_target",
                predicate_term_ref_id=PREDICATE.term_ref_id,
                rule_term_ref_id=RULE.term_ref_id,
                premises=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=PREMISE_ROLE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
                conclusions=(
                    ProofEndpoint(
                        ordinal=0,
                        role_term_ref_id=CONCLUSION_ROLE.term_ref_id,
                        subject=_subject(),
                    ),
                ),
            ),
        ),
        producer=ProducerBinding(
            descriptor=ProducerDescriptor(
                producer_id="part_file_import_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("part-file-import-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("part-file-import-upstream-request"),
        ),
    )


class _Reader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        payload = self.payloads[document.artifact_id]
        if len(payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return payload


class _Sink:
    def __init__(self) -> None:
        self.items: dict[str, tuple[DocumentRef, bytes]] = {}

    def publish_exact(self, document: DocumentRef, payload: bytes) -> bytes:
        existing = self.items.get(document.artifact_id)
        if existing is not None and existing != (document, payload):
            raise RuntimeError("collision")
        staged = dict(self.items)
        staged[document.artifact_id] = (document, payload)
        self.items = staged
        return payload

    def read_exact(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        stored_document, payload = self.items[document.artifact_id]
        if stored_document != document or len(payload) > maximum_bytes:
            raise RuntimeError("bad read")
        return payload


def _request(
    graph: ParametricFeatureGraphV2,
    *,
    max_output_bytes: int = MAX_PART_FILE_IMPORT_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_part_file_import_capability_document()
    policy = TrustedRulePolicy(evaluators=(_Evaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_PART_FILE_IMPORT_ADAPTER_DESCRIPTOR,
        terms=(
            *PART_FILE_IMPORT_REQUEST_TERMS,
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
        ),
        documents=(intent_document, capability_document),
        intent_artifact_ids=(intent_document.artifact_id,),
        capability_artifact_ids=(capability_document.artifact_id,),
        proof_bundle=_proof(policy, intent_document),
        budget=BridgeBudget(
            max_input_bytes=len(intent_payload) + len(capability_payload),
            max_output_bytes=max_output_bytes,
            max_subject_lookups=1,
            max_rule_applications=1,
        ),
    )
    return (
        request,
        _Reader(
            {
                intent_document.artifact_id: intent_payload,
                capability_document.artifact_id: capability_payload,
            }
        ),
        policy,
    )


def _lower(adapter, request, reader, policy):
    return adapter.lower_with_receipt(
        request,
        artifacts=reader,
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        proof_policy=policy,
    )


def test_part_file_import_fast_batch_three_honest_semantics_share_g0() -> None:
    assert len(PART_FILE_IMPORT_MANIFEST.operations) == 3
    assert set(PART_FILE_IMPORT_NATIVE_SPECS) == set(PartFileImportOperation)
    assert all(
        item.native_type_id != "Part::CurveNet" for item in PART_FILE_IMPORT_MANIFEST.operations
    )
    capability_document, capability_payload = build_part_file_import_capability_document()
    assert capability_document.document_digest == PART_FILE_IMPORT_MANIFEST.manifest_sha256
    assert capability_payload == PART_FILE_IMPORT_MANIFEST.canonical_bytes

    for operation in PartFileImportOperation:
        artifact_payload = f"authenticated-{operation.value}-fixture".encode()
        artifact_document = build_part_file_import_artifact_document(
            operation,
            artifact_payload,
        )
        request, reader, policy = _request(_graph(operation, artifact_document.content_sha256))
        sink = _Sink()
        adapter = FreeCADPartFileImportAdapter(sink)
        result, receipt = _lower(adapter, request, reader, policy)
        decoded, payload = adapter.read_plan(receipt)
        repeated, repeated_receipt = _lower(adapter, request, reader, policy)

        assert isinstance(decoded, PartFileImportBackendPlan)
        assert decoded.operation is operation
        assert decoded.artifact_id == artifact_document.artifact_id
        assert decoded.artifact_content_sha256 == artifact_document.content_sha256
        assert decoded.artifact_media_type == artifact_document.media_type
        assert receipt.operation.native_type_id == PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id
        assert result.supported_subjects == (_subject(),)
        assert result.plan_document == receipt.plan_document
        assert adapter.executable is False and adapter.grants_execution_authority is False
        assert receipt.executable is False and receipt.grants_execution_authority is False
        assert decoded.executable is False and decoded.grants_execution_authority is False
        assert payload == decoded.canonical_bytes
        assert b"Part::" not in payload
        assert b"FileName" not in payload
        assert b'"path"' not in payload
        assert repeated == result and repeated_receipt == receipt and len(sink.items) == 1


def test_part_file_import_graph_drift_and_format_substitution_fail_closed() -> None:
    digest = _sha("artifact")
    cases = (
        _graph(
            PartFileImportOperation.BREP,
            digest,
            operation_definition="f" * 64,
        ),
        _graph(
            PartFileImportOperation.BREP,
            digest,
            artifact_value_operation=PartFileImportOperation.STEP,
        ),
    )
    for graph in cases:
        request, reader, policy = _request(graph)
        sink = _Sink()
        with pytest.raises(IntentBridgeError) as error:
            _lower(FreeCADPartFileImportAdapter(sink), request, reader, policy)
        assert error.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
        assert sink.items == {}

    with pytest.raises(ParametricFeatureGraphError):
        _graph(PartFileImportOperation.STEP, digest, artifact_id="../../host/path.step")


def test_part_file_import_plan_canonical_budget_and_tamper() -> None:
    graph = _graph(PartFileImportOperation.STEP, _sha("artifact"))
    request, reader, policy = _request(graph)
    adapter = FreeCADPartFileImportAdapter(_Sink())
    _, receipt = _lower(adapter, request, reader, policy)
    _, payload = adapter.read_plan(receipt)
    with pytest.raises(PartFileImportRuleError):
        decode_part_file_import_backend_plan(payload + b" ")

    exact_request, exact_reader, exact_policy = _request(
        graph,
        max_output_bytes=len(payload),
    )
    result, _ = _lower(
        FreeCADPartFileImportAdapter(_Sink()),
        exact_request,
        exact_reader,
        exact_policy,
    )
    assert result.plan_document.size_bytes == len(payload)

    small_request, small_reader, small_policy = _request(
        graph,
        max_output_bytes=len(payload) - 1,
    )
    sink = _Sink()
    with pytest.raises(IntentBridgeError) as error:
        _lower(
            FreeCADPartFileImportAdapter(sink),
            small_request,
            small_reader,
            small_policy,
        )
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert sink.items == {}


def test_host_owned_stager_is_bounded_exact_and_rejects_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    payload = b"exact staged payload"
    stager = HostOwnedImportStager(root)
    lease = stager.stage_exact(
        payload,
        suffix=".step",
        expected_content_sha256=_sha(payload),
    )
    with lease:
        assert lease.path.read_bytes() == payload
        assert lease.path.parent.parent == root
        assert not lease.path.is_symlink()
    assert tuple(root.iterdir()) == ()

    symlink = tmp_path / "root-link"
    symlink.symlink_to(root, target_is_directory=True)
    with pytest.raises(PartFileImportRuleError):
        HostOwnedImportStager(symlink)
    with pytest.raises(PartFileImportRuleError):
        stager.stage_exact(
            payload,
            suffix=".step",
            expected_content_sha256="0" * 64,
        )
    assert tuple(root.iterdir()) == ()


@pytest.mark.slow
def test_real_freecad_part_file_import_batch(tmp_path: Path) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD batch gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")

    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    export_code = f"""
import os, sys
from pathlib import Path
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
import FreeCAD as App
import Part
root = Path({str(artifact_root)!r})
doc = App.newDocument('M3ArtifactExport')
source = doc.addObject('Part::Feature', 'Source')
source.Shape = Part.makeBox(10, 20, 30)
doc.recompute()
source.Shape.exportBrep(str(root / 'source.brep'))
Part.export([source], str(root / 'source.step'))
Part.export([source], str(root / 'source.iges'))
App.closeDocument(doc.Name)
"""
    subprocess.run([str(runtime_python), "-c", export_code], check=True)

    cases = []
    for operation in PartFileImportOperation:
        artifact_path = artifact_root / f"source.{operation.value}"
        artifact_payload = artifact_path.read_bytes()
        artifact_document = build_part_file_import_artifact_document(
            operation,
            artifact_payload,
        )
        request, reader, policy = _request(_graph(operation, artifact_document.content_sha256))
        adapter = FreeCADPartFileImportAdapter(_Sink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        plan_path = tmp_path / f"part-file-import-{operation.value}.json"
        plan_path.write_bytes(payload)
        cases.append(
            {
                "operation": operation.value,
                "type_id": PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id,
                "plan_path": str(plan_path),
                "plan_content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "body_id": plan.body_id,
                "artifact_path": str(artifact_path),
                "artifact_document": artifact_document.to_mapping(),
                "adapter_contract_sha256": receipt.adapter.adapter_contract_sha256,
                "manifest_sha256": receipt.manifest_sha256,
                "operation_specification_sha256": receipt.operation.specification_sha256,
            }
        )

    invalid_payload = b"not a STEP exchange file"
    invalid_document = build_part_file_import_artifact_document(
        PartFileImportOperation.STEP,
        invalid_payload,
        artifact_id="artifact_part_file_import_invalid",
    )
    invalid_request, invalid_reader, invalid_policy = _request(
        _graph(
            PartFileImportOperation.STEP,
            invalid_document.content_sha256,
            artifact_id=invalid_document.artifact_id,
        )
    )
    invalid_adapter = FreeCADPartFileImportAdapter(_Sink())
    invalid_result, invalid_receipt = _lower(
        invalid_adapter,
        invalid_request,
        invalid_reader,
        invalid_policy,
    )
    invalid_plan, invalid_plan_payload = invalid_adapter.read_plan(invalid_receipt)
    invalid_plan_path = tmp_path / "part-file-import-invalid.json"
    invalid_plan_path.write_bytes(invalid_plan_payload)
    invalid_artifact_path = artifact_root / "invalid.step"
    invalid_artifact_path.write_bytes(invalid_payload)

    staging_root = tmp_path / "staging"
    staging_root.mkdir(mode=0o700)
    staging_root.chmod(0o700)
    model_path = tmp_path / "part-file-imports.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    code = f"""
import dataclasses, hashlib, json, os, sys, zipfile
from pathlib import Path
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
import FreeCAD as App
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.parametric.freecad_part_file_import_rules import (
    HostOwnedImportStager,
    PartFileImportExecutionBindings,
    PartFileImportRuleError,
    apply_part_file_import_plan,
)

class Reader:
    def __init__(self, payload): self.payload = payload
    def read(self, document, maximum_bytes):
        if len(self.payload) > maximum_bytes: raise RuntimeError('over budget')
        return self.payload

cases = json.loads({json.dumps(cases)!r})
staging_root = Path({str(staging_root)!r})
doc = App.newDocument('M3PartFileImports')
doc.UndoMode = 1
receipts = []
for case in cases:
    raw_plan = Path(case['plan_path']).read_bytes()
    raw_artifact = Path(case['artifact_path']).read_bytes()
    artifact_document = DocumentRef.from_mapping(case['artifact_document'])
    binding = PartFileImportExecutionBindings(
        document=doc,
        artifact_document=artifact_document,
        artifacts=Reader(raw_artifact),
        stager=HostOwnedImportStager(staging_root),
        body_id=case['body_id'],
        expected_adapter_contract_sha256=case['adapter_contract_sha256'],
        expected_manifest_sha256=case['manifest_sha256'],
        expected_operation_specification_sha256=case['operation_specification_sha256'],
    )
    receipt = apply_part_file_import_plan(
        raw_plan,
        expected_content_sha256=case['plan_content_sha256'],
        expected_plan_sha256=case['plan_sha256'],
        bindings=binding,
    )
    result = doc.getObject(receipt.object_name)
    assert result is not None and result.TypeId == case['type_id']
    assert result.FileName == '' and result.Shape.isValid() and not result.Shape.isNull()
    assert receipt.artifact_content_sha256 == hashlib.sha256(raw_artifact).hexdigest()
    assert {str(staging_root)!r} not in repr(receipt)
    assert tuple(staging_root.iterdir()) == ()
    receipts.append(receipt)

# Raw-content tamper and media substitution are rejected before mutation.
first = cases[0]
first_document = DocumentRef.from_mapping(first['artifact_document'])
before = tuple(doc.Objects)
for artifact_document, payload in (
    (first_document, Path(first['artifact_path']).read_bytes() + b'tamper'),
    (
        dataclasses.replace(first_document, media_type='model/step'),
        Path(first['artifact_path']).read_bytes(),
    ),
):
    binding = PartFileImportExecutionBindings(
        document=doc,
        artifact_document=artifact_document,
        artifacts=Reader(payload),
        stager=HostOwnedImportStager(staging_root),
        body_id=first['body_id'],
        expected_adapter_contract_sha256=first['adapter_contract_sha256'],
        expected_manifest_sha256=first['manifest_sha256'],
        expected_operation_specification_sha256=first['operation_specification_sha256'],
    )
    try:
        apply_part_file_import_plan(
            Path(first['plan_path']).read_bytes(),
            expected_content_sha256=first['plan_content_sha256'],
            expected_plan_sha256=first['plan_sha256'],
            bindings=binding,
        )
        raise AssertionError('tampered artifact accepted')
    except PartFileImportRuleError:
        pass
    assert tuple(doc.Objects) == before and tuple(staging_root.iterdir()) == ()

# Invalid but exactly authenticated STEP reaches native creation, then rolls back.
invalid_document = DocumentRef.from_mapping({invalid_document.to_mapping()!r})
invalid_binding = PartFileImportExecutionBindings(
    document=doc,
    artifact_document=invalid_document,
    artifacts=Reader(Path({str(invalid_artifact_path)!r}).read_bytes()),
    stager=HostOwnedImportStager(staging_root),
    body_id={invalid_plan.body_id!r},
    expected_adapter_contract_sha256={invalid_receipt.adapter.adapter_contract_sha256!r},
    expected_manifest_sha256={invalid_receipt.manifest_sha256!r},
    expected_operation_specification_sha256={invalid_receipt.operation.specification_sha256!r},
)
before = tuple(doc.Objects)
try:
    apply_part_file_import_plan(
        Path({str(invalid_plan_path)!r}).read_bytes(),
        expected_content_sha256={invalid_result.plan_document.content_sha256!r},
        expected_plan_sha256={invalid_result.plan_document.document_digest!r},
        bindings=invalid_binding,
    )
    raise AssertionError('invalid STEP accepted')
except PartFileImportRuleError:
    pass
assert tuple(doc.Objects) == before and tuple(staging_root.iterdir()) == ()

doc.recompute()
doc.saveAs({str(model_path)!r})
App.closeDocument(doc.Name)
with zipfile.ZipFile({str(model_path)!r}) as archive:
    saved = b''.join(archive.read(name) for name in archive.namelist())
assert str(staging_root).encode() not in saved
reopened = App.openDocument({str(model_path)!r})
assert len(reopened.Objects) == 3
for case, receipt in zip(cases, receipts, strict=True):
    result = reopened.getObject(receipt.object_name)
    assert result.TypeId == case['type_id'] and result.FileName == ''
    assert result.Shape.isValid() and not result.Shape.isNull()
    assert str(result.Shape.ShapeType) == receipt.result_shape_type
    assert len(result.Shape.Edges) == receipt.edge_count
    assert len(result.Shape.Faces) == receipt.face_count
    assert len(result.Shape.Solids) == receipt.solid_count
App.closeDocument(reopened.Name)
"""
    subprocess.run([str(runtime_python), "-c", code], check=True)
    assert model_path.is_file()
    assert tuple(staging_root.iterdir()) == ()
