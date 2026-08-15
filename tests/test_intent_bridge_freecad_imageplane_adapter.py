"""Focused contracts and one real FreeCAD batch for reviewed ImagePlane."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import stat
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
from vibecad.intent_bridge.freecad_imageplane_adapter import (
    FREECAD_IMAGEPLANE_ADAPTER_DESCRIPTOR,
    IMAGEPLANE_ARTIFACT_LOCATOR_TERM,
    IMAGEPLANE_ARTIFACT_ROLE_TERM,
    IMAGEPLANE_ARTIFACT_TYPE_TERM,
    IMAGEPLANE_CANONICAL_JSON_ENCODING_TERM,
    IMAGEPLANE_CONFIGURATION_ROLE_TERM,
    IMAGEPLANE_CONFIGURATION_TYPE_TERM,
    IMAGEPLANE_FAMILY_TERM,
    IMAGEPLANE_INTENT_ROLE_TERM,
    IMAGEPLANE_MANIFEST,
    IMAGEPLANE_OPERATION_TERM,
    IMAGEPLANE_PFG_TERMS,
    IMAGEPLANE_REQUEST_TERMS,
    IMAGEPLANE_RESULT_ROLE_TERM,
    IMAGEPLANE_RESULT_TYPE_TERM,
    IMAGEPLANE_STRUCTURE_TERM,
    FreeCADImagePlaneAdapter,
    build_imageplane_artifact_document,
    build_imageplane_capability_document,
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
    DesignParameterV2,
    FeatureBodyV2,
    FeatureGraphResultV2,
    FeatureInputPortV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureParameterBindingV2,
    FeatureReferenceBindingV2,
    FeatureResultV2,
    ParametricFeatureGraphError,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    TermTypedValueV2,
)
from vibecad.parametric.freecad_imageplane_rules import (
    IMAGEPLANE_ARTIFACT_SPECS,
    MAX_IMAGEPLANE_PLAN_BYTES,
    HostOwnedImageStager,
    ImagePlaneBackendPlan,
    ImagePlaneRuleError,
    decode_imageplane_backend_plan,
    validate_imageplane_artifact_payload,
)


def _sha(value: str | bytes) -> str:
    payload = value if type(value) is bytes else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _proof_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.imageplane-proof-test",
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


RULE = _proof_term("rule_imageplane_target", "rule.imageplane-reviewed")
PREDICATE = _proof_term("predicate_imageplane_target", "predicate.imageplane-reviewed")
PREMISE_ROLE = _proof_term(
    "role_imageplane_candidate",
    "proof-role.imageplane-candidate",
)
CONCLUSION_ROLE = _proof_term(
    "role_imageplane_validated",
    "proof-role.imageplane-validated",
)
STRUCTURE_BRIDGE = _as_bridge(IMAGEPLANE_STRUCTURE_TERM)


def _configuration(
    *,
    media_type: str = "image/png",
    x_size_mm: float = 80.0,
    y_size_mm: float = 60.0,
    position_mm: list[float] | None = None,
    axis: list[float] | None = None,
    angle_degrees: float = 0.0,
) -> dict[str, object]:
    return {
        "media_type": media_type,
        "x_size_mm": x_size_mm,
        "y_size_mm": y_size_mm,
        "placement": {
            "position_mm": [0.0, 0.0, 0.0] if position_mm is None else position_mm,
            "axis": [0.0, 0.0, 1.0] if axis is None else axis,
            "angle_degrees": angle_degrees,
        },
    }


def _graph(
    artifact_content_sha256: str,
    *,
    configuration: dict[str, object] | None = None,
    artifact_id: str = "artifact_imageplane_source",
    operation_definition: str | None = None,
    parameter_type_term_ref_id: str | None = None,
) -> ParametricFeatureGraphV2:
    terms = list(IMAGEPLANE_PFG_TERMS)
    if operation_definition is not None:
        index = terms.index(IMAGEPLANE_OPERATION_TERM)
        terms[index] = dataclasses.replace(
            IMAGEPLANE_OPERATION_TERM,
            term_definition_sha256=operation_definition,
        )
    parameter = DesignParameterV2(
        parameter_id="parameter_imageplane_configuration",
        name="Image plane configuration",
        semantic_role_term_ref_id=IMAGEPLANE_CONFIGURATION_ROLE_TERM.term_ref_id,
        value=TermTypedValueV2.from_value(
            value_id="value_imageplane_configuration",
            value_type_term_ref_id=(
                parameter_type_term_ref_id or IMAGEPLANE_CONFIGURATION_TYPE_TERM.term_ref_id
            ),
            encoding_term_ref_id=IMAGEPLANE_CANONICAL_JSON_ENCODING_TERM.term_ref_id,
            value=configuration or _configuration(),
        ),
    )
    reference = SemanticReferenceV2(
        reference_id=artifact_id,
        scope=SemanticReferenceScope.EXTERNAL,
        semantic_role_term_ref_id=IMAGEPLANE_ARTIFACT_ROLE_TERM.term_ref_id,
        value_type_term_ref_id=IMAGEPLANE_ARTIFACT_TYPE_TERM.term_ref_id,
        locator_term_ref_id=IMAGEPLANE_ARTIFACT_LOCATOR_TERM.term_ref_id,
        source_content_sha256=artifact_content_sha256,
    )
    target = FeatureNodeV2(
        node_id="node_imageplane",
        body_id="body_document",
        name="Authenticated reference image",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id=IMAGEPLANE_STRUCTURE_TERM.term_ref_id,
            family_term_ref_id=IMAGEPLANE_FAMILY_TERM.term_ref_id,
            operation_term_ref_id=IMAGEPLANE_OPERATION_TERM.term_ref_id,
            input_ports=(
                FeatureInputPortV2(
                    port_id="port_configuration",
                    semantic_role_term_ref_id=IMAGEPLANE_CONFIGURATION_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=IMAGEPLANE_CONFIGURATION_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
                FeatureInputPortV2(
                    port_id="port_image",
                    semantic_role_term_ref_id=IMAGEPLANE_ARTIFACT_ROLE_TERM.term_ref_id,
                    value_type_term_ref_id=IMAGEPLANE_ARTIFACT_TYPE_TERM.term_ref_id,
                    minimum_cardinality=1,
                    maximum_cardinality=1,
                    ordered=False,
                ),
            ),
            parameter_bindings=(
                FeatureParameterBindingV2(
                    binding_id="binding_configuration",
                    port_id="port_configuration",
                    parameter_id=parameter.parameter_id,
                ),
            ),
            references=(
                FeatureReferenceBindingV2(
                    binding_id="binding_image",
                    port_id="port_image",
                    reference_id=reference.reference_id,
                ),
            ),
        ),
        results=(
            FeatureResultV2(
                result_id="result_imageplane",
                semantic_role_term_ref_id=IMAGEPLANE_RESULT_ROLE_TERM.term_ref_id,
                value_type_term_ref_id=IMAGEPLANE_RESULT_TYPE_TERM.term_ref_id,
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph_imageplane",
        name="Reference image plane",
        terms=tuple(terms),
        bodies=(FeatureBodyV2(body_id="body_document", name="Document root"),),
        parameters=(parameter,),
        references=(reference,),
        nodes=(target,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection_imageplane",
                node_id=target.node_id,
                result_id="result_imageplane",
            ),
        ),
    )


def _intent_document(graph: ParametricFeatureGraphV2) -> tuple[DocumentRef, bytes]:
    payload = graph.canonical_bytes
    return (
        DocumentRef(
            artifact_id="artifact_imageplane_pfg",
            role_term_ref_id=IMAGEPLANE_INTENT_ROLE_TERM.term_ref_id,
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
        artifact_id="artifact_imageplane_pfg",
        selector_kind_term_ref_id=PFG_SELECTOR_FEATURE_NODE.term_ref_id,
        selector_id="node_imageplane",
    )


class _Evaluator:
    def __init__(self) -> None:
        def signature(role: BridgeTermRef) -> RuleEndpointSignature:
            return RuleEndpointSignature(
                selector_kind_term=PFG_SELECTOR_FEATURE_NODE,
                role_term=role,
                subject_type_term=STRUCTURE_BRIDGE,
            )

        self._descriptor = TrustedRuleEvaluatorDescriptor(
            evaluator_id="imageplane_target_evaluator",
            evaluator_version="1.0.0",
            evaluator_contract_sha256=_sha("imageplane-target-evaluator-v1"),
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
                "/imageplane_target",
            )


def _proof(policy: TrustedRulePolicy, document: DocumentRef) -> ProofBundle:
    return ProofBundle(
        terms=(
            RULE,
            PREDICATE,
            PREMISE_ROLE,
            CONCLUSION_ROLE,
            STRUCTURE_BRIDGE,
            IMAGEPLANE_INTENT_ROLE_TERM,
            PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
            PFG_SELECTOR_FEATURE_NODE,
        ),
        documents=(document,),
        assertions=(
            ProofAssertion(
                assertion_id="assertion_imageplane_target",
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
                producer_id="imageplane_test_compiler",
                producer_version="1.0.0",
                producer_contract_sha256=_sha("imageplane-test-compiler"),
                rule_catalog_sha256=policy.catalog_sha256,
            ),
            request_sha256=_sha("imageplane-upstream-request"),
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
    max_output_bytes: int = MAX_IMAGEPLANE_PLAN_BYTES,
) -> tuple[BackendLoweringRequest, _Reader, TrustedRulePolicy]:
    intent_document, intent_payload = _intent_document(graph)
    capability_document, capability_payload = build_imageplane_capability_document()
    policy = TrustedRulePolicy(evaluators=(_Evaluator(),))
    request = BackendLoweringRequest(
        adapter=FREECAD_IMAGEPLANE_ADAPTER_DESCRIPTOR,
        terms=(
            *IMAGEPLANE_REQUEST_TERMS,
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


def test_imageplane_lowering_is_exact_content_bound_and_authority_free() -> None:
    image = (
        Path(__file__).parent / "fixtures" / "guided_photo_v1" / "images" / "calibration_block.png"
    ).read_bytes()
    artifact = build_imageplane_artifact_document(image, media_type="image/png")
    request, reader, policy = _request(_graph(artifact.content_sha256))
    sink = _Sink()
    adapter = FreeCADImagePlaneAdapter(sink)

    result, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    repeated, repeated_receipt = _lower(adapter, request, reader, policy)

    assert len(IMAGEPLANE_MANIFEST.operations) == 1
    assert isinstance(plan, ImagePlaneBackendPlan)
    assert plan.artifact_id == artifact.artifact_id
    assert plan.artifact_content_sha256 == artifact.content_sha256
    assert plan.configuration == _configuration()
    assert receipt.operation.native_type_id == "Image::ImagePlane"
    assert result.supported_subjects == (_subject(),)
    assert result.plan_document == receipt.plan_document
    assert not adapter.executable and not adapter.grants_execution_authority
    assert not receipt.executable and not receipt.grants_execution_authority
    assert not plan.executable and not plan.grants_execution_authority
    assert payload == plan.canonical_bytes
    assert b"Image::ImagePlane" not in payload
    assert b"ImageFile" not in payload
    assert b'"path"' not in payload
    assert repeated == result and repeated_receipt == receipt and len(sink.items) == 1


def test_imageplane_graph_identity_configuration_and_paths_fail_closed() -> None:
    digest = _sha("artifact")
    cases = (
        _graph(digest, operation_definition="f" * 64),
        _graph(digest, configuration=_configuration(axis=[0.0, 0.0, 0.5])),
    )
    for graph in cases:
        request, reader, policy = _request(graph)
        sink = _Sink()
        with pytest.raises(IntentBridgeError) as caught:
            _lower(FreeCADImagePlaneAdapter(sink), request, reader, policy)
        assert caught.value.code is IntentBridgeErrorCode.AUTHORITY_VIOLATION
        assert sink.items == {}

    with pytest.raises(ParametricFeatureGraphError):
        _graph(digest, artifact_id="../../host/image.png")
    with pytest.raises(ParametricFeatureGraphError):
        _graph(
            digest,
            parameter_type_term_ref_id=IMAGEPLANE_ARTIFACT_TYPE_TERM.term_ref_id,
        )


def test_imageplane_plan_budget_tamper_and_artifact_contracts() -> None:
    image = (
        Path(__file__).parent / "fixtures" / "guided_photo_v1" / "images" / "calibration_block.png"
    ).read_bytes()
    artifact = build_imageplane_artifact_document(image, media_type="image/png")
    graph = _graph(artifact.content_sha256)
    request, reader, policy = _request(graph)
    adapter = FreeCADImagePlaneAdapter(_Sink())
    _, receipt = _lower(adapter, request, reader, policy)
    _, payload = adapter.read_plan(receipt)
    with pytest.raises(ImagePlaneRuleError):
        decode_imageplane_backend_plan(payload + b" ")

    exact_request, exact_reader, exact_policy = _request(
        graph,
        max_output_bytes=len(payload),
    )
    result, _ = _lower(
        FreeCADImagePlaneAdapter(_Sink()),
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
    with pytest.raises(IntentBridgeError) as caught:
        _lower(
            FreeCADImagePlaneAdapter(sink),
            small_request,
            small_reader,
            small_policy,
        )
    assert caught.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert sink.items == {}

    jpeg = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xd9"
    validate_imageplane_artifact_payload(jpeg, "image/jpeg")
    jpeg_document = build_imageplane_artifact_document(jpeg, media_type="image/jpeg")
    assert (
        jpeg_document.schema_term_ref_id
        == IMAGEPLANE_ARTIFACT_SPECS["image/jpeg"].schema_term_ref_id
    )
    with pytest.raises(ImagePlaneRuleError):
        validate_imageplane_artifact_payload(jpeg, "image/png")
    with pytest.raises(IntentBridgeError):
        build_imageplane_artifact_document(image + b"tamper", media_type="image/png")


def test_host_owned_image_stager_is_exact_private_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    payload = b"exact image source"
    stager = HostOwnedImageStager(root)
    lease = stager.stage_exact(
        payload,
        suffix=".png",
        expected_content_sha256=_sha(payload),
    )
    with lease:
        assert lease.path.read_bytes() == payload
        assert lease.path.parent.parent == root
        assert not lease.path.is_symlink()
        assert stat.S_IMODE(lease.path.stat().st_mode) == 0o600
    assert tuple(root.iterdir()) == ()

    alias = tmp_path / "staging-link"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ImagePlaneRuleError):
        HostOwnedImageStager(alias)
    with pytest.raises(ImagePlaneRuleError):
        stager.stage_exact(
            payload,
            suffix=".png",
            expected_content_sha256="0" * 64,
        )
    assert tuple(root.iterdir()) == ()


@pytest.mark.slow
def test_real_freecad_imageplane_create_edit_roundtrip_tamper_and_rollback(
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

    image_root = Path(__file__).parent / "fixtures" / "guided_photo_v1" / "images"
    inputs = (
        (
            image_root / "calibration_block.png",
            _configuration(
                x_size_mm=80.0,
                y_size_mm=60.0,
                position_mm=[1.0, 2.0, 3.0],
                angle_degrees=15.0,
            ),
        ),
        (
            image_root / "fan_spacer.png",
            _configuration(
                x_size_mm=96.0,
                y_size_mm=72.0,
                position_mm=[4.0, 5.0, 6.0],
                axis=[0.0, 1.0, 0.0],
                angle_degrees=25.0,
            ),
        ),
    )
    cases: list[dict[str, object]] = []
    for index, (image_path, configuration) in enumerate(inputs):
        image = image_path.read_bytes()
        artifact = build_imageplane_artifact_document(image, media_type="image/png")
        request, reader, policy = _request(
            _graph(
                artifact.content_sha256,
                configuration=configuration,
                artifact_id=artifact.artifact_id,
            )
        )
        adapter = FreeCADImagePlaneAdapter(_Sink())
        result, receipt = _lower(adapter, request, reader, policy)
        plan, payload = adapter.read_plan(receipt)
        plan_path = tmp_path / f"imageplane-{index}.json"
        plan_path.write_bytes(payload)
        cases.append(
            {
                "plan_path": str(plan_path),
                "plan_content_sha256": result.plan_document.content_sha256,
                "plan_sha256": result.plan_document.document_digest,
                "container_id": plan.container_id,
                "artifact_path": str(image_path),
                "artifact_document": artifact.to_mapping(),
                "adapter_contract_sha256": receipt.adapter.adapter_contract_sha256,
                "manifest_sha256": receipt.manifest_sha256,
                "operation_specification_sha256": (receipt.operation.specification_sha256),
                "configuration": configuration,
            }
        )

    asset_root = tmp_path / "document-assets"
    staging_root = tmp_path / "staging"
    checkpoint_root = tmp_path / "checkpoints"
    for root in (asset_root, staging_root, checkpoint_root):
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    source_root = Path(__file__).parents[1] / "src"
    model_path = checkpoint_root / "ImagePlane.FCStd"
    code = f"""
import dataclasses, hashlib, json, os, sys, zipfile
from pathlib import Path
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
import FreeCAD as App
from vibecad.engine.document_assets import DocumentAssetWorkspace
from vibecad.engine.session import Session
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.parametric.freecad_imageplane_rules import (
    HostOwnedImageStager,
    ImagePlaneExecutionBindings,
    ImagePlaneRuleError,
    ImagePlaneRuleErrorCode,
    apply_imageplane_plan,
)

class Reader:
    def __init__(self, payload): self.payload = payload
    def read(self, document, maximum_bytes):
        if len(self.payload) > maximum_bytes: raise RuntimeError('over budget')
        return self.payload

def workspace_manifest(root):
    result = []
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root))
        if path.is_file():
            result.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            result.append((relative, 'directory'))
    return tuple(result)

cases = json.loads({json.dumps(cases)!r})
asset_root = Path({str(asset_root)!r})
staging_root = Path({str(staging_root)!r})
checkpoint_root = Path({str(checkpoint_root)!r})

def binding(document, workspace, case, artifact_document, payload):
    return ImagePlaneExecutionBindings(
        document=document,
        document_assets=workspace,
        artifact_document=artifact_document,
        artifacts=Reader(payload),
        stager=HostOwnedImageStager(staging_root),
        container_id=case['container_id'],
        expected_adapter_contract_sha256=case['adapter_contract_sha256'],
        expected_manifest_sha256=case['manifest_sha256'],
        expected_operation_specification_sha256=(
            case['operation_specification_sha256']
        ),
    )

def apply_case(document, workspace, case, artifact_document=None, payload=None):
    raw_plan = Path(case['plan_path']).read_bytes()
    raw_artifact = (
        Path(case['artifact_path']).read_bytes() if payload is None else payload
    )
    artifact_document = (
        DocumentRef.from_mapping(case['artifact_document'])
        if artifact_document is None else artifact_document
    )
    return apply_imageplane_plan(
        raw_plan,
        expected_content_sha256=case['plan_content_sha256'],
        expected_plan_sha256=case['plan_sha256'],
        bindings=binding(
            document,
            workspace,
            case,
            artifact_document,
            raw_artifact,
        ),
    )

# Session owns TransientDir before assignment and preserves included bytes in FCStd.
session = Session(checkpoint_dir=checkpoint_root, document_asset_root=asset_root)
session.open_document('ImagePlane')
first = apply_case(session.doc, session._document_assets, cases[0])
assert first.disposition == 'created'
feature = session.doc.getObject(first.object_name)
assert feature is not None and feature.TypeId == 'Image::ImagePlane'
assert feature.XSize == 80.0 and feature.YSize == 60.0
retained = Path(feature.ImageFile)
assert retained.parent == Path(session.doc.TransientDir)
assert retained.name == first.retained_alias
assert hashlib.sha256(retained.read_bytes()).hexdigest() == first.artifact_content_sha256
assert str(asset_root) not in repr(first) and str(staging_root) not in repr(first)
assert tuple(staging_root.iterdir()) == ()
checkpoint = session._checkpoint()
with zipfile.ZipFile(checkpoint) as archive:
    assert first.retained_alias in archive.namelist()
    assert hashlib.sha256(archive.read(first.retained_alias)).hexdigest() == (
        first.artifact_content_sha256
    )
session.close_document()
assert tuple(asset_root.iterdir()) == ()

# Load receives a fresh TransientDir before PropertyFileIncluded decode.
session = Session(checkpoint_dir=checkpoint_root, document_asset_root=asset_root)
session.load_document(checkpoint)
feature = session.doc.getObject(first.object_name)
assert feature is not None and Path(feature.ImageFile).parent == Path(session.doc.TransientDir)
assert hashlib.sha256(Path(feature.ImageFile).read_bytes()).hexdigest() == (
    first.artifact_content_sha256
)

# Raw plan, payload and media drift all fail before mutation or source staging.
case = cases[0]
artifact_document = DocumentRef.from_mapping(case['artifact_document'])
raw_payload = Path(case['artifact_path']).read_bytes()
before_objects = tuple(session.doc.Objects)
before_workspace = workspace_manifest(Path(session.doc.TransientDir))
for raw_plan, document, payload in (
    (Path(case['plan_path']).read_bytes() + b' ', artifact_document, raw_payload),
    (Path(case['plan_path']).read_bytes(), artifact_document, raw_payload + b'tamper'),
    (
        Path(case['plan_path']).read_bytes(),
        dataclasses.replace(artifact_document, media_type='image/jpeg'),
        raw_payload,
    ),
):
    try:
        apply_imageplane_plan(
            raw_plan,
            expected_content_sha256=case['plan_content_sha256'],
            expected_plan_sha256=case['plan_sha256'],
            bindings=binding(
                session.doc,
                session._document_assets,
                case,
                document,
                payload,
            ),
        )
        raise AssertionError('tampered ImagePlane input accepted')
    except ImagePlaneRuleError:
        pass
    assert tuple(session.doc.Objects) == before_objects
    assert workspace_manifest(Path(session.doc.TransientDir)) == before_workspace
    assert tuple(staging_root.iterdir()) == ()

# Stable graph/node binding edits the same object and replaces the active asset.
second = apply_case(session.doc, session._document_assets, cases[1])
assert second.disposition == 'updated' and second.object_name == first.object_name
assert len(session.doc.Objects) == 1
feature = session.doc.getObject(second.object_name)
assert feature.XSize == 96.0 and feature.YSize == 72.0
assert Path(feature.ImageFile).name == second.retained_alias
assert hashlib.sha256(Path(feature.ImageFile).read_bytes()).hexdigest() == (
    second.artifact_content_sha256
)
checkpoint = session._checkpoint()
session.close_document()
assert tuple(asset_root.iterdir()) == ()

session = Session(checkpoint_dir=checkpoint_root, document_asset_root=asset_root)
session.load_document(checkpoint)
feature = session.doc.getObject(second.object_name)
assert feature is not None and feature.XSize == 96.0 and feature.YSize == 72.0
assert Path(feature.ImageFile).name == second.retained_alias
assert hashlib.sha256(Path(feature.ImageFile).read_bytes()).hexdigest() == (
    second.artifact_content_sha256
)
session.close_document()
assert tuple(asset_root.iterdir()) == ()

# A late native fault after assigning replacement bytes restores the old object,
# configuration and exact document workspace manifest.
class FaultDocument:
    def __init__(self, inner):
        object.__setattr__(self, 'inner', inner)
        object.__setattr__(self, 'fail_once', False)
    def __getattr__(self, name): return getattr(self.inner, name)
    def __setattr__(self, name, value):
        if name in {{'inner', 'fail_once'}}:
            object.__setattr__(self, name, value)
        else:
            setattr(self.inner, name, value)
    def recompute(self):
        result = self.inner.recompute()
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError('late recompute fault')
        return result

inner = App.newDocument('ImagePlaneRollback')
inner.UndoMode = 1
fault = FaultDocument(inner)
workspace = DocumentAssetWorkspace(asset_root)
workspace.attach(fault)
original = apply_case(fault, workspace, cases[0])
feature = inner.getObject(original.object_name)
before_objects = tuple(inner.Objects)
before_configuration = (
    float(feature.XSize),
    float(feature.YSize),
    tuple(feature.Placement.Base),
    tuple(feature.Placement.Rotation.Q),
    str(feature.ImageFile),
)
before_workspace = workspace_manifest(Path(fault.TransientDir))
fault.fail_once = True
try:
    apply_case(fault, workspace, cases[1])
    raise AssertionError('late fault accepted')
except ImagePlaneRuleError as error:
    assert error.code is ImagePlaneRuleErrorCode.TRANSACTION_FAILED
assert tuple(inner.Objects) == before_objects
feature = inner.getObject(original.object_name)
assert (
    float(feature.XSize),
    float(feature.YSize),
    tuple(feature.Placement.Base),
    tuple(feature.Placement.Rotation.Q),
    str(feature.ImageFile),
) == before_configuration
assert workspace_manifest(Path(fault.TransientDir)) == before_workspace
assert tuple(staging_root.iterdir()) == () and not inner.HasPendingTransaction
App.closeDocument(inner.Name)
workspace.release_after_close(fault)
assert tuple(asset_root.iterdir()) == ()
"""
    subprocess.run([str(runtime_python), "-c", code], check=True)
    assert model_path.is_file()
    assert tuple(asset_root.iterdir()) == ()
    assert tuple(staging_root.iterdir()) == ()
