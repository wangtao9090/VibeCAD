"""Focused tests for the trusted ParametricFeatureGraphV2 bridge codec."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.parametric_feature_graph_codec import (
    PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS,
    PFG_SELECTOR_DESIGN_PARAMETER,
    PFG_SELECTOR_DOCUMENT_ROOT,
    PFG_SELECTOR_FEATURE_BODY,
    PFG_SELECTOR_FEATURE_NODE,
    PFG_SELECTOR_FEATURE_RESULT,
    PFG_SELECTOR_GRAPH_RESULT,
    PFG_SELECTOR_SEMANTIC_REFERENCE,
    PFG_TYPE_DOCUMENT_ROOT,
    PFG_TYPE_FEATURE_BODY,
    ParametricFeatureGraphV2Codec,
)
from vibecad.intent_bridge.ports import (
    GraphCodec,
    TrustedCodecRegistry,
    resolve_subject,
    validate_documents,
)
from vibecad.parametric.feature_graph_v2 import (
    MAX_PARAMETRIC_FEATURE_GRAPH_BYTES,
    DesignParameterV2,
    FeatureBodyV2,
    FeatureGraphResultV2,
    FeatureIntentV2,
    FeatureNodeV2,
    FeatureResultV2,
    ParametricFeatureGraphV2,
    SemanticReferenceScope,
    SemanticReferenceV2,
    SemanticTermRefV2,
    TermTypedValueV2,
    encode_parametric_feature_graph_v2,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _pfg_term(term_ref_id: str, *, namespace: str = "vibecad.foundation") -> SemanticTermRefV2:
    return SemanticTermRefV2(
        term_ref_id=term_ref_id,
        namespace=namespace,
        vocabulary_version="2.0.0",
        term_id=f"foundation:{term_ref_id}",
        term_definition_sha256=_sha(f"term:{namespace}:{term_ref_id}"),
    )


def _graph(*, structural_namespace: str = "vibecad.foundation") -> ParametricFeatureGraphV2:
    term_ids = (
        "semantic.parameter",
        "type.scalar",
        "encoding.canonical-json",
        "reference-role.axis",
        "type.axis",
        "locator.origin-z-axis",
        "structure.feature",
        "family.primitive",
        "operation.primitive",
        "result-role.solid",
        "type.solid",
    )
    terms = tuple(
        _pfg_term(
            term_id,
            namespace=(
                structural_namespace if term_id == "structure.feature" else "vibecad.foundation"
            ),
        )
        for term_id in term_ids
    )
    parameter = DesignParameterV2(
        parameter_id="parameter.length",
        name="Length",
        semantic_role_term_ref_id="semantic.parameter",
        value=TermTypedValueV2.from_value(
            value_id="value.length",
            value_type_term_ref_id="type.scalar",
            encoding_term_ref_id="encoding.canonical-json",
            value=10,
        ),
    )
    reference = SemanticReferenceV2(
        reference_id="reference.axis",
        scope=SemanticReferenceScope.ORIGIN,
        semantic_role_term_ref_id="reference-role.axis",
        value_type_term_ref_id="type.axis",
        locator_term_ref_id="locator.origin-z-axis",
    )
    node = FeatureNodeV2(
        node_id="node.box",
        body_id="body.main",
        name="Box",
        intent=FeatureIntentV2(
            structural_kind_term_ref_id="structure.feature",
            family_term_ref_id="family.primitive",
            operation_term_ref_id="operation.primitive",
        ),
        results=(
            FeatureResultV2(
                result_id="result.box.solid",
                semantic_role_term_ref_id="result-role.solid",
                value_type_term_ref_id="type.solid",
            ),
        ),
    )
    return ParametricFeatureGraphV2(
        graph_id="graph.main",
        name="Main",
        terms=terms,
        bodies=(FeatureBodyV2(body_id="body.main", name="Main"),),
        parameters=(parameter,),
        references=(reference,),
        nodes=(node,),
        graph_results=(
            FeatureGraphResultV2(
                selection_id="selection.main",
                node_id="node.box",
                result_id="result.box.solid",
            ),
        ),
    )


def _document(
    graph: ParametricFeatureGraphV2,
    *,
    payload: bytes | None = None,
) -> DocumentRef:
    raw = encode_parametric_feature_graph_v2(graph) if payload is None else payload
    return DocumentRef(
        artifact_id="artifact_pfg",
        role_term_ref_id="role_intent",
        schema_term_ref_id=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_ref_id,
        document_id=graph.graph_id,
        document_digest=graph.graph_sha256,
        content_sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
        media_type=PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE,
    )


def _bridge_term(term_ref_id: str, *, term_id: str | None = None) -> BridgeTermRef:
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.codec_test",
        vocabulary_version="1.0.0",
        term_id=term_id or f"test.{term_ref_id}",
        term_definition_sha256=_sha(f"bridge:{term_id or term_ref_id}"),
    )


def _subject(selector: BridgeTermRef, selector_id: str) -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact_pfg",
        selector_kind_term_ref_id=selector.term_ref_id,
        selector_id=selector_id,
    )


class _MemoryReader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        self.reads += 1
        if len(self.payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return self.payload


def _validated_document(graph: ParametricFeatureGraphV2):
    raw = encode_parametric_feature_graph_v2(graph)
    document = _document(graph)
    role = _bridge_term("role_intent")
    report = validate_documents(
        terms=(role, PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM),
        documents=(document,),
        reader=_MemoryReader(raw),
        codecs=TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),)),
        maximum_total_bytes=MAX_PARAMETRIC_FEATURE_GRAPH_BYTES,
    )
    return document, report.validated


def _identity(term: SemanticTermRefV2 | BridgeTermRef) -> tuple[str, str, str, str]:
    return (
        term.namespace,
        term.vocabulary_version,
        term.term_id,
        term.term_definition_sha256,
    )


def test_codec_is_structural_graph_codec_with_content_bound_schema_and_no_authority():
    codec = ParametricFeatureGraphV2Codec()

    assert isinstance(codec, GraphCodec)
    assert codec.descriptor.schema_term == PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM
    assert (
        codec.descriptor.codec_contract_sha256
        == "47cb4a10f4079d926682d59663c65b76182c1117217156b9f377354097df46d8"
    )
    assert (
        PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM.term_definition_sha256
        == "5d710e1d056ae843f8ed8d1c29ecab505149400417176a73c872c184387cb654"
    )
    assert codec.executable is False
    assert codec.grants_execution_authority is False
    assert not hasattr(codec, "lower")
    assert not hasattr(codec, "execute")
    assert len(PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS) == 7
    assert len({item.semantic_identity for item in PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS}) == 7
    assert tuple(item.term_ref_id for item in PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS) == (
        "selector_pfg_v2_document_root",
        "selector_pfg_v2_feature_body",
        "selector_pfg_v2_feature_node",
        "selector_pfg_v2_feature_result",
        "selector_pfg_v2_design_parameter",
        "selector_pfg_v2_semantic_reference",
        "selector_pfg_v2_graph_result",
    )


def test_registry_selects_schema_by_complete_identity_and_unknown_schema_is_inert():
    codec = ParametricFeatureGraphV2Codec()
    registry = TrustedCodecRegistry((codec,))
    alias = dataclasses.replace(
        PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
        term_ref_id="local_schema_alias",
    )
    rebound = dataclasses.replace(alias, term_definition_sha256="f" * 64)

    assert registry.codec_for(alias) is codec
    assert registry.codec_for(rebound) is None

    graph = _graph()
    raw = encode_parametric_feature_graph_v2(graph)
    future_schema = _bridge_term("schema_future", term_id="document-schema.future-pfg")
    document = dataclasses.replace(_document(graph), schema_term_ref_id=future_schema.term_ref_id)
    reader = _MemoryReader(raw)
    report = validate_documents(
        terms=(_bridge_term("role_intent"), future_schema),
        documents=(document,),
        reader=reader,
        codecs=registry,
        maximum_total_bytes=MAX_PARAMETRIC_FEATURE_GRAPH_BYTES,
    )

    assert report.validated == ()
    assert report.inert_artifact_ids == (document.artifact_id,)
    assert reader.reads == 0


def test_codec_validates_canonical_bytes_graph_digest_content_hash_and_graph_id():
    graph = _graph()
    raw = encode_parametric_feature_graph_v2(graph)
    document = _document(graph)
    codec = ParametricFeatureGraphV2Codec()

    codec.validate_document(document, raw)

    cases = (
        dataclasses.replace(document, content_sha256="f" * 64),
        dataclasses.replace(document, document_digest="e" * 64),
        dataclasses.replace(document, document_id="graph.other"),
        dataclasses.replace(document, media_type="application/json"),
    )
    for invalid in cases:
        with pytest.raises(IntentBridgeError) as error:
            codec.validate_document(invalid, raw)
        assert error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE
        assert len(str(error.value)) < 160


def test_codec_rejects_noncanonical_and_semantically_substituted_payloads():
    graph = _graph()
    raw = encode_parametric_feature_graph_v2(graph)
    codec = ParametricFeatureGraphV2Codec()

    noncanonical = json.dumps(json.loads(raw), indent=1, sort_keys=True).encode()
    noncanonical_document = _document(graph, payload=noncanonical)
    with pytest.raises(IntentBridgeError) as canonical_error:
        codec.validate_document(noncanonical_document, noncanonical)
    assert canonical_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    replacement = dataclasses.replace(graph, name="Replacement")
    replacement_raw = encode_parametric_feature_graph_v2(replacement)
    substituted_document = dataclasses.replace(
        _document(graph),
        content_sha256=hashlib.sha256(replacement_raw).hexdigest(),
        size_bytes=len(replacement_raw),
    )
    with pytest.raises(IntentBridgeError) as semantic_error:
        codec.validate_document(substituted_document, replacement_raw)
    assert semantic_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_codec_enforces_native_graph_byte_budget_before_decode():
    graph = _graph()
    raw = b"x" * (MAX_PARAMETRIC_FEATURE_GRAPH_BYTES + 1)
    document = _document(graph, payload=raw)

    with pytest.raises(IntentBridgeError) as error:
        ParametricFeatureGraphV2Codec().validate_document(document, raw)
    assert error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert error.value.path == "/document/size_bytes"


@pytest.mark.parametrize(
    ("selector", "selector_id", "semantic_term"),
    (
        (PFG_SELECTOR_DOCUMENT_ROOT, "graph.main", PFG_TYPE_DOCUMENT_ROOT),
        (PFG_SELECTOR_FEATURE_BODY, "body.main", PFG_TYPE_FEATURE_BODY),
        (PFG_SELECTOR_FEATURE_NODE, "node.box", "structure.feature"),
        (PFG_SELECTOR_FEATURE_RESULT, "result.box.solid", "type.solid"),
        (PFG_SELECTOR_DESIGN_PARAMETER, "parameter.length", "type.scalar"),
        (PFG_SELECTOR_SEMANTIC_REFERENCE, "reference.axis", "type.axis"),
        (PFG_SELECTOR_GRAPH_RESULT, "selection.main", "type.solid"),
    ),
)
def test_codec_resolves_minimum_complete_stable_subject_set_with_full_identity(
    selector,
    selector_id,
    semantic_term,
):
    graph = _graph()
    document, validated = _validated_document(graph)
    registry = TrustedCodecRegistry((ParametricFeatureGraphV2Codec(),))
    subject = _subject(selector, selector_id)

    resolved = resolve_subject(
        subject,
        validated_documents=validated,
        codecs=registry,
    )

    assert resolved is not None
    assert resolved.subject == subject
    if type(semantic_term) is BridgeTermRef:
        assert resolved.semantic_type == semantic_term
    else:
        source = next(item for item in graph.terms if item.term_ref_id == semantic_term)
        assert _identity(resolved.semantic_type) == _identity(source)
        assert resolved.semantic_type.term_ref_id == source.term_ref_id
    assert document.document_digest == graph.graph_sha256


def test_unknown_dangling_cross_artifact_and_type_mismatched_selectors_are_inert():
    graph = _graph()
    raw = encode_parametric_feature_graph_v2(graph)
    document = _document(graph)
    codec = ParametricFeatureGraphV2Codec()
    selector_alias = dataclasses.replace(
        PFG_SELECTOR_FEATURE_NODE,
        term_ref_id="selector_pfg_v2_feature_node_alias",
    )
    subjects = (
        SubjectRef(
            artifact_id=document.artifact_id,
            selector_kind_term_ref_id="selector_future_kind",
            selector_id="node.box",
        ),
        _subject(selector_alias, "node.box"),
        _subject(PFG_SELECTOR_FEATURE_NODE, "node.missing"),
        _subject(PFG_SELECTOR_FEATURE_NODE, "result.box.solid"),
        dataclasses.replace(
            _subject(PFG_SELECTOR_FEATURE_NODE, "node.box"),
            artifact_id="artifact_other",
        ),
    )

    assert all(codec.resolve_subject(document, raw, subject) is None for subject in subjects)


def test_graph_term_identity_is_preserved_without_namespace_normalization():
    graph = _graph(structural_namespace="vendor-example")
    raw = encode_parametric_feature_graph_v2(graph)
    document = _document(graph)
    codec = ParametricFeatureGraphV2Codec()

    codec.validate_document(document, raw)
    resolved = codec.resolve_subject(
        document,
        raw,
        _subject(PFG_SELECTOR_FEATURE_NODE, "node.box"),
    )

    assert resolved is not None
    source = next(term for term in graph.terms if term.term_ref_id == "structure.feature")
    assert _identity(resolved.semantic_type) == _identity(source)
    assert resolved.semantic_type.namespace == "vendor-example"


def test_direct_codec_rejects_wrong_argument_types_with_bounded_bridge_error():
    graph = _graph()
    raw = encode_parametric_feature_graph_v2(graph)
    codec = ParametricFeatureGraphV2Codec()

    with pytest.raises(IntentBridgeError) as document_error:
        codec.validate_document(object(), raw)  # type: ignore[arg-type]
    assert document_error.value.code is IntentBridgeErrorCode.INVALID_INPUT

    with pytest.raises(IntentBridgeError) as subject_error:
        codec.resolve_subject(_document(graph), raw, object())  # type: ignore[arg-type]
    assert subject_error.value.code is IntentBridgeErrorCode.INVALID_INPUT

    with pytest.raises(IntentBridgeError) as payload_error:
        codec.resolve_subject(
            _document(graph),
            object(),  # type: ignore[arg-type]
            _subject(PFG_SELECTOR_FEATURE_NODE, "node.box"),
        )
    assert payload_error.value.code is IntentBridgeErrorCode.INVALID_INPUT
