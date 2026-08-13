"""Focused tests for the trusted SketchIntentGraph structural codec."""

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
from vibecad.intent_bridge.ports import (
    GraphCodec,
    TrustedCodecRegistry,
    resolve_subject,
    validate_documents,
)
from vibecad.intent_bridge.sketch_intent_graph_codec import (
    SKETCH_ANCHOR_SELECTOR_TERM_REF_ID,
    SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID,
    SKETCH_DOCUMENT_SELECTOR_TERM_REF_ID,
    SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID,
    SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    SKETCH_INTENT_GRAPH_SCHEMA_TERM,
    SKETCH_RESULT_SELECTOR_TERM_REF_ID,
    SKETCH_ROOT_SELECTOR_TERM_REF_ID,
    SKETCH_ROOT_SEMANTIC_TYPE_TERM,
    SKETCH_SELECTOR_KIND_TERMS,
    SketchIntentGraphCodec,
)
from vibecad.sketch.contracts import (
    MAX_SKETCH_INTENT_BYTES,
    SKETCH_INTENT_SCHEMA_VERSION,
    SketchAnchor,
    SketchConstraintNode,
    SketchGeometryNode,
    SketchIntentGraph,
    SketchResultPort,
    encode_sketch_intent_graph,
)
from vibecad.sketch.ontology import (
    SketchAnchorTargetKind,
    SketchOntologyTermRef,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _term(term_ref_id: str, term_id: str) -> SketchOntologyTermRef:
    return SketchOntologyTermRef(
        term_ref_id=term_ref_id,
        namespace="org.vibecad.sketch-test",
        vocabulary_version="1.0.0",
        term_id=term_id,
        term_definition_sha256=_digest(f"definition:{term_id}"),
    )


def _graph() -> SketchIntentGraph:
    geometry_type = _term("geometry.line", "geometry/line")
    constraint_type = _term("constraint.fixed", "constraint/fixed")
    anchor_role = _term("anchor.whole", "anchor/whole")
    result_type = _term("value.endpoint", "value/endpoint")
    return SketchIntentGraph(
        schema_version=SKETCH_INTENT_SCHEMA_VERSION,
        graph_id="graph.codec.1",
        sketch_id="sketch.codec.1",
        terms=(geometry_type, constraint_type, anchor_role, result_type),
        geometries=(
            SketchGeometryNode(
                geometry_id="geometry.line.1",
                geometry_term_ref_id=geometry_type.term_ref_id,
                anchor_ids=("anchor.line.whole",),
                result_ids=("result.endpoint.1",),
            ),
        ),
        anchors=(
            SketchAnchor(
                anchor_id="anchor.line.whole",
                target_kind=SketchAnchorTargetKind.GEOMETRY,
                target_id="geometry.line.1",
                role_term_ref_id=anchor_role.term_ref_id,
            ),
        ),
        constraints=(
            SketchConstraintNode(
                constraint_id="constraint.fixed.1",
                constraint_term_ref_id=constraint_type.term_ref_id,
                anchor_ids=("anchor.line.whole",),
            ),
        ),
        results=(
            SketchResultPort(
                result_id="result.endpoint.1",
                producer_id="geometry.line.1",
                port_id="endpoint",
                value_type_term_ref_id=result_type.term_ref_id,
            ),
        ),
    )


def _document(graph: SketchIntentGraph, payload: bytes) -> DocumentRef:
    return DocumentRef(
        artifact_id="artifact.sketch.1",
        role_term_ref_id="role.intent",
        schema_term_ref_id=SKETCH_INTENT_GRAPH_SCHEMA_TERM.term_ref_id,
        document_id=graph.graph_id,
        document_digest=graph.graph_sha256,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        media_type=SKETCH_INTENT_GRAPH_MEDIA_TYPE,
    )


def _subject(selector_kind: str, selector_id: str) -> SubjectRef:
    return SubjectRef(
        artifact_id="artifact.sketch.1",
        selector_kind_term_ref_id=selector_kind,
        selector_id=selector_id,
    )


def _bridge_term(reference: SketchOntologyTermRef) -> BridgeTermRef:
    return BridgeTermRef(**reference.to_mapping())


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


class _MemoryReader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = dict(payloads)
        self.reads: list[str] = []

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        self.reads.append(document.artifact_id)
        payload = self.payloads[document.artifact_id]
        if len(payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return payload


def test_descriptor_is_deterministic_structural_only_and_full_identity_keyed() -> None:
    codec = SketchIntentGraphCodec()
    duplicate = SketchIntentGraphCodec()

    assert isinstance(codec, GraphCodec)
    assert codec.descriptor == duplicate.descriptor
    assert codec.descriptor.schema_term is SKETCH_INTENT_GRAPH_SCHEMA_TERM
    assert not any(hasattr(codec, name) for name in ("compile", "execute", "lower"))
    assert len({item.semantic_identity for item in SKETCH_SELECTOR_KIND_TERMS}) == 6

    registry = TrustedCodecRegistry((codec,))
    local_alias = dataclasses.replace(
        SKETCH_INTENT_GRAPH_SCHEMA_TERM,
        term_ref_id="schema.sketch_alias",
    )
    wrong_definition = dataclasses.replace(
        local_alias,
        term_definition_sha256=_digest("wrong schema definition"),
    )
    assert registry.codec_for(local_alias) is codec
    assert registry.codec_for(wrong_definition) is None


def test_validates_canonical_bytes_graph_digest_and_raw_content_hash() -> None:
    graph = _graph()
    payload = encode_sketch_intent_graph(graph)
    document = _document(graph, payload)
    codec = SketchIntentGraphCodec()

    assert document.document_digest == graph.graph_sha256
    assert document.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert document.document_digest != document.content_sha256
    codec.validate_document(document, payload)

    for changed, code in (
        (
            dataclasses.replace(document, document_digest=_digest("wrong semantic digest")),
            IntentBridgeErrorCode.INTEGRITY_FAILURE,
        ),
        (
            dataclasses.replace(document, content_sha256=_digest("wrong content hash")),
            IntentBridgeErrorCode.INTEGRITY_FAILURE,
        ),
        (
            dataclasses.replace(document, document_id="graph.other"),
            IntentBridgeErrorCode.INTEGRITY_FAILURE,
        ),
        (
            dataclasses.replace(document, media_type="application/json"),
            IntentBridgeErrorCode.INTEGRITY_FAILURE,
        ),
        (
            dataclasses.replace(document, size_bytes=MAX_SKETCH_INTENT_BYTES + 1),
            IntentBridgeErrorCode.BUDGET_EXCEEDED,
        ),
    ):
        with pytest.raises(IntentBridgeError) as raised:
            codec.validate_document(changed, payload)
        assert raised.value.code is code
        assert len(raised.value.path.encode("utf-8")) <= 384


def test_resolves_minimal_complete_stable_subject_set_with_full_ontology_identity() -> None:
    graph = _graph()
    payload = encode_sketch_intent_graph(graph)
    document = _document(graph, payload)
    codec = SketchIntentGraphCodec()
    term_by_id = {item.term_ref_id: item for item in graph.terms}
    expected = {
        (SKETCH_DOCUMENT_SELECTOR_TERM_REF_ID, graph.graph_id): codec.descriptor.schema_term,
        (SKETCH_ROOT_SELECTOR_TERM_REF_ID, graph.sketch_id): SKETCH_ROOT_SEMANTIC_TYPE_TERM,
        (SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID, "geometry.line.1"): _bridge_term(
            term_by_id["geometry.line"]
        ),
        (SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID, "constraint.fixed.1"): _bridge_term(
            term_by_id["constraint.fixed"]
        ),
        (SKETCH_ANCHOR_SELECTOR_TERM_REF_ID, "anchor.line.whole"): _bridge_term(
            term_by_id["anchor.whole"]
        ),
        (SKETCH_RESULT_SELECTOR_TERM_REF_ID, "result.endpoint.1"): _bridge_term(
            term_by_id["value.endpoint"]
        ),
    }

    for (selector_kind, selector_id), semantic_type in expected.items():
        subject = _subject(selector_kind, selector_id)
        resolved = codec.resolve_subject(document, payload, subject)
        assert resolved is not None
        assert resolved.subject == subject
        assert resolved.semantic_type == semantic_type
        assert resolved.semantic_type.to_mapping() == semantic_type.to_mapping()


def test_unknown_dangling_wrong_kind_and_foreign_subjects_remain_inert() -> None:
    graph = _graph()
    payload = encode_sketch_intent_graph(graph)
    document = _document(graph, payload)
    codec = SketchIntentGraphCodec()

    inert = (
        _subject("selector.future", "geometry.line.1"),
        _subject(SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID, "constraint.fixed.1"),
        _subject(SKETCH_RESULT_SELECTOR_TERM_REF_ID, "result.missing"),
        dataclasses.replace(
            _subject(SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID, "geometry.line.1"),
            artifact_id="artifact.other",
        ),
    )
    for subject in inert:
        assert codec.resolve_subject(document, payload, subject) is None


def test_reserved_selector_ref_ids_depend_on_outer_full_identity_policy() -> None:
    """The codec owns ref-id routing; ProofBundle policy owns selector identity."""

    graph = _graph()
    payload = encode_sketch_intent_graph(graph)
    document = _document(graph, payload)
    codec = SketchIntentGraphCodec()
    subject = _subject(SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID, "geometry.line.1")

    assert codec.resolve_subject(document, payload, subject) is not None
    selector_term = next(
        item
        for item in SKETCH_SELECTOR_KIND_TERMS
        if item.term_ref_id == SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID
    )
    wrong_same_ref_id = dataclasses.replace(
        selector_term,
        term_definition_sha256=_digest("untrusted selector definition"),
    )
    assert wrong_same_ref_id.semantic_identity != selector_term.semantic_identity


def test_unknown_schema_is_inert_and_payload_is_not_read() -> None:
    graph = _graph()
    payload = encode_sketch_intent_graph(graph)
    document = _document(graph, payload)
    unknown_schema = BridgeTermRef(
        term_ref_id="schema.future",
        namespace="org.vibecad.future",
        vocabulary_version="2.0.0",
        term_id="schema/future-sketch",
        term_definition_sha256=_digest("future schema"),
    )
    unknown_payload = b"not interpreted"
    unknown_document = dataclasses.replace(
        document,
        artifact_id="artifact.future",
        schema_term_ref_id=unknown_schema.term_ref_id,
        document_id="future.document",
        document_digest=_digest("future semantic document"),
        content_sha256=hashlib.sha256(unknown_payload).hexdigest(),
        size_bytes=len(unknown_payload),
        media_type="application/octet-stream",
    )
    reader = _MemoryReader(
        {
            document.artifact_id: payload,
            unknown_document.artifact_id: unknown_payload,
        }
    )

    report = validate_documents(
        terms=(SKETCH_INTENT_GRAPH_SCHEMA_TERM, unknown_schema),
        documents=(unknown_document, document),
        reader=reader,
        codecs=TrustedCodecRegistry((SketchIntentGraphCodec(),)),
        maximum_total_bytes=MAX_SKETCH_INTENT_BYTES,
    )

    assert tuple(item.document.artifact_id for item in report.validated) == (document.artifact_id,)
    assert report.inert_artifact_ids == (unknown_document.artifact_id,)
    assert reader.reads == [document.artifact_id]


def test_port_level_resolution_revalidates_bound_document() -> None:
    graph = _graph()
    payload = encode_sketch_intent_graph(graph)
    document = _document(graph, payload)
    codec = SketchIntentGraphCodec()
    registry = TrustedCodecRegistry((codec,))
    reader = _MemoryReader({document.artifact_id: payload})
    report = validate_documents(
        terms=(SKETCH_INTENT_GRAPH_SCHEMA_TERM,),
        documents=(document,),
        reader=reader,
        codecs=registry,
        maximum_total_bytes=MAX_SKETCH_INTENT_BYTES,
    )
    subject = _subject(SKETCH_ANCHOR_SELECTOR_TERM_REF_ID, "anchor.line.whole")

    resolved = resolve_subject(
        subject,
        validated_documents=report.validated,
        codecs=registry,
    )
    assert resolved is not None
    assert resolved.semantic_type == _bridge_term(
        next(item for item in graph.terms if item.term_ref_id == "anchor.whole")
    )

    tampered = dataclasses.replace(
        report.validated[0],
        document=dataclasses.replace(document, document_digest=_digest("tampered")),
    )
    with pytest.raises(IntentBridgeError) as raised:
        resolve_subject(subject, validated_documents=(tampered,), codecs=registry)
    assert raised.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE


def test_payload_budget_classifies_n_and_n_plus_one_with_bounded_errors() -> None:
    graph = _graph()
    codec = SketchIntentGraphCodec()
    at_limit = b"x" * MAX_SKETCH_INTENT_BYTES
    over_limit = at_limit + b"x"

    with pytest.raises(IntentBridgeError) as at_limit_error:
        codec.validate_document(_document(graph, at_limit), at_limit)
    assert at_limit_error.value.code is IntentBridgeErrorCode.INTEGRITY_FAILURE

    with pytest.raises(IntentBridgeError) as over_limit_error:
        codec.validate_document(_document(graph, over_limit), over_limit)
    assert over_limit_error.value.code is IntentBridgeErrorCode.BUDGET_EXCEEDED
    assert len(over_limit_error.value.path.encode("utf-8")) <= 384


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    (
        ("noncanonical", IntentBridgeErrorCode.INTEGRITY_FAILURE),
        ("graph_digest", IntentBridgeErrorCode.INTEGRITY_FAILURE),
        ("dangling", IntentBridgeErrorCode.INTEGRITY_FAILURE),
        ("unsupported_schema", IntentBridgeErrorCode.UNSUPPORTED_VERSION),
    ),
)
def test_rejects_rehashed_structural_tampering_with_bounded_errors(
    mutation: str,
    expected_code: IntentBridgeErrorCode,
) -> None:
    graph = _graph()
    original = encode_sketch_intent_graph(graph)
    envelope = json.loads(original)
    if mutation == "noncanonical":
        payload = json.dumps(envelope, sort_keys=True, indent=2).encode("ascii")
    elif mutation == "graph_digest":
        envelope["graph_sha256"] = _digest("attacker graph digest")
        payload = _canonical(envelope)
    elif mutation == "dangling":
        envelope["anchors"][0]["target_id"] = "geometry.missing"
        payload = _canonical(envelope)
    else:
        envelope["schema_version"] = 2
        payload = _canonical(envelope)
    document = dataclasses.replace(
        _document(graph, original),
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )

    with pytest.raises(IntentBridgeError) as raised:
        SketchIntentGraphCodec().validate_document(document, payload)
    assert raised.value.code is expected_code
    assert len(raised.value.path.encode("utf-8")) <= 384
    assert "geometry.missing" not in str(raised.value)
