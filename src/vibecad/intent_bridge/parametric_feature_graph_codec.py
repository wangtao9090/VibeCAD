"""Trusted structural codec for :class:`ParametricFeatureGraphV2` documents.

The codec validates canonical bytes plus both raw and graph-domain digests.  It
resolves only stable graph identifiers and returns content-bound semantic type
identities.  Unknown selectors remain inert.  This module performs no lowering,
execution, dynamic discovery, or authority grant.

The current GraphCodec seam carries only ``selector_kind_term_ref_id``.  This
codec therefore accepts the exact reserved selector ref-ids below; the matching
selector-kind semantic identities are closed separately by the ProofBundle
term table and its injected trusted policy.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from vibecad.intent_bridge.contracts import (
    BridgeTermRef,
    DocumentRef,
    IntentBridgeError,
    IntentBridgeErrorCode,
    SubjectRef,
)
from vibecad.intent_bridge.ports import GraphCodecDescriptor, ResolvedSubject
from vibecad.parametric.feature_graph_v2 import (
    MAX_PARAMETRIC_FEATURE_GRAPH_BYTES,
    GraphAuthority,
    ParametricFeatureGraphError,
    ParametricFeatureGraphErrorCode,
    ParametricFeatureGraphV2,
    SemanticTermRefV2,
    decode_parametric_feature_graph_v2,
)

PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.parametric-feature-graph-v2+json"
)

_ONTOLOGY_NAMESPACE = "org.vibecad.intent_bridge.pfg_v2"
_ONTOLOGY_VERSION = "1.0.0"
_ONTOLOGY_DEFINITION_DOMAIN = b"vibecad.intent-bridge.pfg-v2-ontology.v1\0"
_CODEC_CONTRACT_DOMAIN = b"vibecad.intent-bridge.pfg-v2-codec-contract.v1\0"


def _fixed_term(term_ref_id: str, term_id: str) -> BridgeTermRef:
    definition = hashlib.sha256(
        b"\0".join(
            (
                _ONTOLOGY_DEFINITION_DOMAIN,
                _ONTOLOGY_NAMESPACE.encode("ascii"),
                _ONTOLOGY_VERSION.encode("ascii"),
                term_id.encode("utf-8"),
            )
        )
    ).hexdigest()
    return BridgeTermRef(
        term_ref_id=term_ref_id,
        namespace=_ONTOLOGY_NAMESPACE,
        vocabulary_version=_ONTOLOGY_VERSION,
        term_id=term_id,
        term_definition_sha256=definition,
    )


PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM: Final = _fixed_term(
    "schema_parametric_feature_graph_v2",
    "document-schema.parametric-feature-graph-v2",
)

PFG_SELECTOR_DOCUMENT_ROOT: Final = _fixed_term(
    "selector_pfg_v2_document_root",
    "selector.parametric-feature-graph-v2.document-root",
)
PFG_SELECTOR_FEATURE_BODY: Final = _fixed_term(
    "selector_pfg_v2_feature_body",
    "selector.parametric-feature-graph-v2.feature-body",
)
PFG_SELECTOR_FEATURE_NODE: Final = _fixed_term(
    "selector_pfg_v2_feature_node",
    "selector.parametric-feature-graph-v2.feature-node",
)
PFG_SELECTOR_FEATURE_RESULT: Final = _fixed_term(
    "selector_pfg_v2_feature_result",
    "selector.parametric-feature-graph-v2.feature-result",
)
PFG_SELECTOR_DESIGN_PARAMETER: Final = _fixed_term(
    "selector_pfg_v2_design_parameter",
    "selector.parametric-feature-graph-v2.design-parameter",
)
PFG_SELECTOR_SEMANTIC_REFERENCE: Final = _fixed_term(
    "selector_pfg_v2_semantic_reference",
    "selector.parametric-feature-graph-v2.semantic-reference",
)
PFG_SELECTOR_GRAPH_RESULT: Final = _fixed_term(
    "selector_pfg_v2_graph_result",
    "selector.parametric-feature-graph-v2.graph-result",
)

PFG_TYPE_DOCUMENT_ROOT: Final = _fixed_term(
    "type_pfg_v2_document_root",
    "subject-type.parametric-feature-graph-v2.document-root",
)
PFG_TYPE_FEATURE_BODY: Final = _fixed_term(
    "type_pfg_v2_feature_body",
    "subject-type.parametric-feature-graph-v2.feature-body",
)

PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS: Final = (
    PFG_SELECTOR_DOCUMENT_ROOT,
    PFG_SELECTOR_FEATURE_BODY,
    PFG_SELECTOR_FEATURE_NODE,
    PFG_SELECTOR_FEATURE_RESULT,
    PFG_SELECTOR_DESIGN_PARAMETER,
    PFG_SELECTOR_SEMANTIC_REFERENCE,
    PFG_SELECTOR_GRAPH_RESULT,
)

_SELECTOR_DOCUMENT_ROOT = PFG_SELECTOR_DOCUMENT_ROOT.term_ref_id
_SELECTOR_FEATURE_BODY = PFG_SELECTOR_FEATURE_BODY.term_ref_id
_SELECTOR_FEATURE_NODE = PFG_SELECTOR_FEATURE_NODE.term_ref_id
_SELECTOR_FEATURE_RESULT = PFG_SELECTOR_FEATURE_RESULT.term_ref_id
_SELECTOR_DESIGN_PARAMETER = PFG_SELECTOR_DESIGN_PARAMETER.term_ref_id
_SELECTOR_SEMANTIC_REFERENCE = PFG_SELECTOR_SEMANTIC_REFERENCE.term_ref_id
_SELECTOR_GRAPH_RESULT = PFG_SELECTOR_GRAPH_RESULT.term_ref_id
_KNOWN_SELECTOR_IDS = frozenset(
    term.term_ref_id for term in PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS
)

_CODEC_CONTRACT_TERMS = (
    PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
    *PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS,
    PFG_TYPE_DOCUMENT_ROOT,
    PFG_TYPE_FEATURE_BODY,
)
_CODEC_CONTRACT_SHA256 = hashlib.sha256(
    b"\0".join(
        (
            _CODEC_CONTRACT_DOMAIN,
            PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE.encode("ascii"),
            str(MAX_PARAMETRIC_FEATURE_GRAPH_BYTES).encode("ascii"),
            b"canonical-bytes;raw-sha256;graph-sha256;graph-id;authority-free;unknown-inert",
            *(
                "|".join((term.term_ref_id, *term.semantic_identity)).encode("utf-8")
                for term in _CODEC_CONTRACT_TERMS
            ),
        )
    )
).hexdigest()


@dataclass(frozen=True, slots=True)
class _SubjectIndex:
    graph: ParametricFeatureGraphV2
    terms: MappingProxyType[str, SemanticTermRefV2]
    body_ids: frozenset[str]
    node_type_terms: MappingProxyType[str, str]
    result_type_terms: MappingProxyType[str, str]
    parameter_type_terms: MappingProxyType[str, str]
    reference_type_terms: MappingProxyType[str, str]
    graph_result_type_terms: MappingProxyType[str, str]


def _fail(code: IntentBridgeErrorCode, path: str) -> None:
    raise IntentBridgeError(code, path)


def _decode_document(document: DocumentRef, payload: bytes) -> ParametricFeatureGraphV2:
    if type(document) is not DocumentRef or type(payload) is not bytes:
        _fail(IntentBridgeErrorCode.INVALID_INPUT, "/document")
    if document.media_type != PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/media_type")
    if document.size_bytes > MAX_PARAMETRIC_FEATURE_GRAPH_BYTES:
        _fail(IntentBridgeErrorCode.BUDGET_EXCEEDED, "/document/size_bytes")
    if len(payload) != document.size_bytes:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/size_bytes")
    if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), document.content_sha256):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/content_sha256")
    try:
        graph = decode_parametric_feature_graph_v2(
            payload,
            expected_sha256=document.document_digest,
        )
    except ParametricFeatureGraphError as error:
        code = (
            IntentBridgeErrorCode.BUDGET_EXCEEDED
            if error.code is ParametricFeatureGraphErrorCode.BUDGET_EXCEEDED
            else IntentBridgeErrorCode.INTEGRITY_FAILURE
        )
        _fail(code, "/document/payload")
    except Exception:
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/payload")
    if (
        not hmac.compare_digest(graph.graph_id, document.document_id)
        or not hmac.compare_digest(graph.canonical_bytes, payload)
        or graph.authority is not GraphAuthority.TRUSTED_ADAPTER_REQUIRED
        or graph.executable
        or not graph.adapter_binding_required
    ):
        _fail(IntentBridgeErrorCode.INTEGRITY_FAILURE, "/document/document_id")
    return graph


def _index_graph(graph: ParametricFeatureGraphV2) -> _SubjectIndex:
    terms = {item.term_ref_id: item for item in graph.terms}
    result_types = {
        result.result_id: result.value_type_term_ref_id
        for node in graph.nodes
        for result in node.results
    }
    return _SubjectIndex(
        graph=graph,
        terms=MappingProxyType(terms),
        body_ids=frozenset(item.body_id for item in graph.bodies),
        node_type_terms=MappingProxyType(
            {item.node_id: item.intent.structural_kind_term_ref_id for item in graph.nodes}
        ),
        result_type_terms=MappingProxyType(result_types),
        parameter_type_terms=MappingProxyType(
            {item.parameter_id: item.value.value_type_term_ref_id for item in graph.parameters}
        ),
        reference_type_terms=MappingProxyType(
            {item.reference_id: item.value_type_term_ref_id for item in graph.references}
        ),
        graph_result_type_terms=MappingProxyType(
            {item.selection_id: result_types[item.result_id] for item in graph.graph_results}
        ),
    )


def _bridge_term(term: SemanticTermRefV2) -> BridgeTermRef:
    """Preserve the complete PFG ontology identity without normalization."""

    return BridgeTermRef(
        term_ref_id=term.term_ref_id,
        namespace=term.namespace,
        vocabulary_version=term.vocabulary_version,
        term_id=term.term_id,
        term_definition_sha256=term.term_definition_sha256,
    )


def _resolved(
    subject: SubjectRef,
    semantic_type: BridgeTermRef,
) -> ResolvedSubject:
    return ResolvedSubject(subject=subject, semantic_type=semantic_type)


class ParametricFeatureGraphV2Codec:
    """Trusted, immutable structural codec for one exact PFGv2 schema."""

    __slots__ = ("_descriptor",)

    def __init__(self) -> None:
        self._descriptor = GraphCodecDescriptor(
            codec_id="parametric_feature_graph_v2_codec",
            codec_version="1.0.0",
            codec_contract_sha256=_CODEC_CONTRACT_SHA256,
            schema_term=PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM,
        )

    @property
    def descriptor(self) -> GraphCodecDescriptor:
        return self._descriptor

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def validate_document(self, document: DocumentRef, payload: bytes) -> None:
        """Validate canonical bytes, graph identity, and both digest domains."""

        _decode_document(document, payload)

    def resolve_subject(
        self,
        document: DocumentRef,
        payload: bytes,
        subject: SubjectRef,
    ) -> ResolvedSubject | None:
        """Resolve a stable PFGv2 identifier; unknown or mismatched stays inert."""

        if type(document) is not DocumentRef or type(payload) is not bytes:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/document")
        if type(subject) is not SubjectRef:
            _fail(IntentBridgeErrorCode.INVALID_INPUT, "/subject")
        if (
            subject.artifact_id != document.artifact_id
            or subject.selector_kind_term_ref_id not in _KNOWN_SELECTOR_IDS
        ):
            return None
        index = _index_graph(_decode_document(document, payload))
        selector = subject.selector_kind_term_ref_id
        selector_id = subject.selector_id
        if selector == _SELECTOR_DOCUMENT_ROOT:
            return (
                _resolved(subject, PFG_TYPE_DOCUMENT_ROOT)
                if selector_id == index.graph.graph_id
                else None
            )
        if selector == _SELECTOR_FEATURE_BODY:
            return (
                _resolved(subject, PFG_TYPE_FEATURE_BODY) if selector_id in index.body_ids else None
            )
        if selector == _SELECTOR_FEATURE_NODE:
            term_id = index.node_type_terms.get(selector_id)
        elif selector == _SELECTOR_FEATURE_RESULT:
            term_id = index.result_type_terms.get(selector_id)
        elif selector == _SELECTOR_DESIGN_PARAMETER:
            term_id = index.parameter_type_terms.get(selector_id)
        elif selector == _SELECTOR_SEMANTIC_REFERENCE:
            term_id = index.reference_type_terms.get(selector_id)
        elif selector == _SELECTOR_GRAPH_RESULT:
            term_id = index.graph_result_type_terms.get(selector_id)
        else:  # pragma: no cover - protected by the complete selector set above
            return None
        if term_id is None:
            return None
        semantic_term = index.terms.get(term_id)
        return None if semantic_term is None else _resolved(subject, _bridge_term(semantic_term))


__all__ = [
    "PARAMETRIC_FEATURE_GRAPH_V2_MEDIA_TYPE",
    "PARAMETRIC_FEATURE_GRAPH_V2_SCHEMA_TERM",
    "PARAMETRIC_FEATURE_GRAPH_V2_SELECTOR_TERMS",
    "PFG_SELECTOR_DESIGN_PARAMETER",
    "PFG_SELECTOR_DOCUMENT_ROOT",
    "PFG_SELECTOR_FEATURE_BODY",
    "PFG_SELECTOR_FEATURE_NODE",
    "PFG_SELECTOR_FEATURE_RESULT",
    "PFG_SELECTOR_GRAPH_RESULT",
    "PFG_SELECTOR_SEMANTIC_REFERENCE",
    "PFG_TYPE_DOCUMENT_ROOT",
    "PFG_TYPE_FEATURE_BODY",
    "ParametricFeatureGraphV2Codec",
]
