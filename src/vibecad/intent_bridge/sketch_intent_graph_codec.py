"""Trusted structural codec for a canonical :class:`SketchIntentGraph`.

The codec is a private host-created bridge component.  It validates immutable
graph bytes and resolves stable graph subjects, but it never resolves an
ontology term to an implementation and never grants execution authority.

``SubjectRef`` currently carries only a selector-kind *reference id*, not the
selector term's complete semantic identity.  This codec therefore accepts only
the reserved reference ids below.  The surrounding ``ProofBundle`` term table
and trusted proof policy remain responsible for binding each reserved id to its
complete content-addressed selector identity.  Unknown or wrong-kind selectors
stay inert.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from types import MappingProxyType

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.ports import (
    GraphCodecDescriptor,
    ResolvedSubject,
)
from vibecad.sketch.contracts import (
    MAX_SKETCH_INTENT_BYTES,
    SketchIntentError,
    SketchIntentErrorCode,
    SketchIntentGraph,
    decode_sketch_intent_graph,
)
from vibecad.sketch.ontology import SketchOntologyTermRef

SKETCH_INTENT_GRAPH_MEDIA_TYPE = "application/vnd.vibecad.sketch-intent+json"

SKETCH_DOCUMENT_SELECTOR_TERM_REF_ID = "selector.sketch_document"
SKETCH_ROOT_SELECTOR_TERM_REF_ID = "selector.sketch_root"
SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID = "selector.sketch_geometry"
SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID = "selector.sketch_constraint"
SKETCH_ANCHOR_SELECTOR_TERM_REF_ID = "selector.sketch_anchor"
SKETCH_RESULT_SELECTOR_TERM_REF_ID = "selector.sketch_result"

_TERM_NAMESPACE = "vibecad.intent_bridge.sketch"
_TERM_VERSION = "1.0.0"
_TERM_DEFINITION_DOMAIN = b"vibecad.intent-bridge.sketch-codec-term.v1\0"
_CODEC_CONTRACT_DOMAIN = b"vibecad.intent-bridge.sketch-codec-contract.v1\0"


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _fixed_term(term_ref_id: str, term_id: str, *, category: str) -> BridgeTermRef:
    body = {
        "category": category,
        "namespace": _TERM_NAMESPACE,
        "term_id": term_id,
        "vocabulary_version": _TERM_VERSION,
    }
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=_TERM_NAMESPACE,
        vocabulary_version=_TERM_VERSION,
        term_id=term_id,
        term_definition_sha256=hashlib.sha256(
            _TERM_DEFINITION_DOMAIN + _canonical(body)
        ).hexdigest(),
    )


SKETCH_INTENT_GRAPH_SCHEMA_TERM = _fixed_term(
    "schema.sketch_intent_graph",
    "schema/sketch-intent-graph",
    category="document_schema",
)
SKETCH_ROOT_SEMANTIC_TYPE_TERM = _fixed_term(
    "type.sketch_root",
    "type/sketch-root",
    category="semantic_type",
)

SKETCH_DOCUMENT_SELECTOR_TERM = _fixed_term(
    SKETCH_DOCUMENT_SELECTOR_TERM_REF_ID,
    "selector/sketch-document",
    category="selector_kind",
)
SKETCH_ROOT_SELECTOR_TERM = _fixed_term(
    SKETCH_ROOT_SELECTOR_TERM_REF_ID,
    "selector/sketch-root",
    category="selector_kind",
)
SKETCH_GEOMETRY_SELECTOR_TERM = _fixed_term(
    SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID,
    "selector/sketch-geometry",
    category="selector_kind",
)
SKETCH_CONSTRAINT_SELECTOR_TERM = _fixed_term(
    SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID,
    "selector/sketch-constraint",
    category="selector_kind",
)
SKETCH_ANCHOR_SELECTOR_TERM = _fixed_term(
    SKETCH_ANCHOR_SELECTOR_TERM_REF_ID,
    "selector/sketch-anchor",
    category="selector_kind",
)
SKETCH_RESULT_SELECTOR_TERM = _fixed_term(
    SKETCH_RESULT_SELECTOR_TERM_REF_ID,
    "selector/sketch-result",
    category="selector_kind",
)
SKETCH_SELECTOR_KIND_TERMS = (
    SKETCH_ANCHOR_SELECTOR_TERM,
    SKETCH_CONSTRAINT_SELECTOR_TERM,
    SKETCH_DOCUMENT_SELECTOR_TERM,
    SKETCH_GEOMETRY_SELECTOR_TERM,
    SKETCH_RESULT_SELECTOR_TERM,
    SKETCH_ROOT_SELECTOR_TERM,
)

_CODEC_CONTRACT_SHA256 = hashlib.sha256(
    _CODEC_CONTRACT_DOMAIN
    + _canonical(
        {
            "media_type": SKETCH_INTENT_GRAPH_MEDIA_TYPE,
            "schema_term": SKETCH_INTENT_GRAPH_SCHEMA_TERM.to_mapping(),
            "selector_terms": [item.to_mapping() for item in SKETCH_SELECTOR_KIND_TERMS],
            "sketch_root_type": SKETCH_ROOT_SEMANTIC_TYPE_TERM.to_mapping(),
        }
    )
).hexdigest()

_DESCRIPTOR = GraphCodecDescriptor(
    codec_id="codec.sketch_intent_graph",
    codec_version="1.0.0",
    codec_contract_sha256=_CODEC_CONTRACT_SHA256,
    schema_term=SKETCH_INTENT_GRAPH_SCHEMA_TERM,
)


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _bridge_term(reference: SketchOntologyTermRef) -> BridgeTermRef:
    """Preserve the complete five-field sketch ontology identity."""

    try:
        return BridgeTermRef(**reference.to_mapping())
    except IntentBridgeError:
        raise
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/terms")


def _decode_graph(payload: bytes) -> SketchIntentGraph:
    try:
        return decode_sketch_intent_graph(payload)
    except SketchIntentError as error:
        if error.code is SketchIntentErrorCode.UNSUPPORTED_VERSION:
            _fail(IntentBridgeErrorCode.UNSUPPORTED_VERSION, "/document/schema_version")
        if error.code is SketchIntentErrorCode.BUDGET_EXCEEDED:
            _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/document/payload")
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/payload")
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/payload")


def _validated_graph(document: object, payload: object) -> SketchIntentGraph:
    if type(document) is not DocumentRef or type(payload) is not bytes:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/document")
    if len(payload) > MAX_SKETCH_INTENT_BYTES or document.size_bytes > MAX_SKETCH_INTENT_BYTES:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/document/size_bytes")
    if len(payload) != document.size_bytes:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/size_bytes")
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/content_sha256")
    if document.media_type != SKETCH_INTENT_GRAPH_MEDIA_TYPE:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/media_type")
    graph = _decode_graph(payload)
    if document.document_id != graph.graph_id:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/document_id")
    if not hmac.compare_digest(document.document_digest, graph.graph_sha256):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/document_digest")
    return graph


class SketchIntentGraphCodec:
    """Structural ``GraphCodec`` for the exact SketchIntentGraph v1 schema."""

    __slots__ = ()

    @property
    def descriptor(self) -> GraphCodecDescriptor:
        return _DESCRIPTOR

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        _validated_graph(document, payload)

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        graph = _validated_graph(document, payload)
        if type(subject) is not SubjectRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/subject")
        if subject.artifact_id != document.artifact_id:
            return None

        term_by_id = MappingProxyType({item.term_ref_id: item for item in graph.terms})
        semantic_type: BridgeTermRef | None = None
        selector_kind = subject.selector_kind_term_ref_id
        selector_id = subject.selector_id
        if selector_kind == SKETCH_DOCUMENT_SELECTOR_TERM_REF_ID:
            if selector_id == graph.graph_id:
                semantic_type = self.descriptor.schema_term
        elif selector_kind == SKETCH_ROOT_SELECTOR_TERM_REF_ID:
            if selector_id == graph.sketch_id:
                semantic_type = SKETCH_ROOT_SEMANTIC_TYPE_TERM
        elif selector_kind == SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID:
            node = next(
                (item for item in graph.geometries if item.geometry_id == selector_id),
                None,
            )
            if node is not None:
                semantic_type = _bridge_term(term_by_id[node.geometry_term_ref_id])
        elif selector_kind == SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID:
            node = next(
                (item for item in graph.constraints if item.constraint_id == selector_id),
                None,
            )
            if node is not None:
                semantic_type = _bridge_term(term_by_id[node.constraint_term_ref_id])
        elif selector_kind == SKETCH_ANCHOR_SELECTOR_TERM_REF_ID:
            anchor = next((item for item in graph.anchors if item.anchor_id == selector_id), None)
            if anchor is not None:
                semantic_type = _bridge_term(term_by_id[anchor.role_term_ref_id])
        elif selector_kind == SKETCH_RESULT_SELECTOR_TERM_REF_ID:
            result = next((item for item in graph.results if item.result_id == selector_id), None)
            if result is not None:
                semantic_type = _bridge_term(term_by_id[result.value_type_term_ref_id])

        if semantic_type is None:
            return None
        return ResolvedSubject(subject=subject, semantic_type=semantic_type)


__all__ = [
    "SKETCH_ANCHOR_SELECTOR_TERM",
    "SKETCH_ANCHOR_SELECTOR_TERM_REF_ID",
    "SKETCH_CONSTRAINT_SELECTOR_TERM",
    "SKETCH_CONSTRAINT_SELECTOR_TERM_REF_ID",
    "SKETCH_DOCUMENT_SELECTOR_TERM",
    "SKETCH_DOCUMENT_SELECTOR_TERM_REF_ID",
    "SKETCH_GEOMETRY_SELECTOR_TERM",
    "SKETCH_GEOMETRY_SELECTOR_TERM_REF_ID",
    "SKETCH_INTENT_GRAPH_MEDIA_TYPE",
    "SKETCH_INTENT_GRAPH_SCHEMA_TERM",
    "SKETCH_RESULT_SELECTOR_TERM",
    "SKETCH_RESULT_SELECTOR_TERM_REF_ID",
    "SKETCH_ROOT_SELECTOR_TERM",
    "SKETCH_ROOT_SELECTOR_TERM_REF_ID",
    "SKETCH_ROOT_SEMANTIC_TYPE_TERM",
    "SKETCH_SELECTOR_KIND_TERMS",
    "SketchIntentGraphCodec",
]
