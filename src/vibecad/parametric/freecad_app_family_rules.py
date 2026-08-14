"""Trusted FreeCAD rules for reviewed application document-object semantics.

The backend-neutral plan carries a bounded semantic configuration and, for
container/link operations, stable graph identities for one authenticated
related object.  It never carries a native type, native property name, object
name, Python, expression, or import path.  This module owns the static mapping
to FreeCAD 1.1.0 and executes it through the shared rollback-proven boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionRunner,
)

APP_FAMILY_PLAN_SCHEMA_VERSION: Final = 1
APP_FAMILY_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-app-family-plan+json"
MAX_APP_FAMILY_PLAN_BYTES: Final = 48 * 1024
APP_FAMILY_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
MAX_APP_TEXT_BYTES: Final = 4096
MAX_ANNOTATION_LINES: Final = 8
MAX_ANNOTATION_LINE_BYTES: Final = 512

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-app-family-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-app-family-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-app-family-receipt.v1\0"


class AppFamilyOperation(StrEnum):
    TEXT_ANNOTATION = "text_annotation"
    LEADER_ANNOTATION = "leader_annotation"
    DOCUMENT_GROUP = "document_group"
    OBJECT_LINK = "object_link"
    LINK_GROUP = "link_group"
    MATERIAL_DEFINITION = "material_definition"
    POSITIONED_PART = "positioned_part"
    PLACEMENT_REFERENCE = "placement_reference"
    TEXT_DOCUMENT = "text_document"
    SCALAR_VARIABLE_SET = "scalar_variable_set"


class AppFamilyRelationKind(StrEnum):
    NONE = "none"
    MEMBER = "member"
    LINK_TARGET = "link_target"


@dataclass(frozen=True, slots=True)
class _NativeAppSpec:
    type_id: str
    object_prefix: str
    properties: tuple[str, ...]
    relation_kind: AppFamilyRelationKind


_NATIVE_APP_SPECS: Final = {
    AppFamilyOperation.TEXT_ANNOTATION: _NativeAppSpec(
        "App::Annotation",
        "Annotation",
        ("LabelText", "Position"),
        AppFamilyRelationKind.NONE,
    ),
    AppFamilyOperation.LEADER_ANNOTATION: _NativeAppSpec(
        "App::AnnotationLabel",
        "AnnotationLabel",
        ("BasePosition", "LabelText", "TextPosition"),
        AppFamilyRelationKind.NONE,
    ),
    AppFamilyOperation.DOCUMENT_GROUP: _NativeAppSpec(
        "App::DocumentObjectGroup",
        "DocumentGroup",
        ("Group",),
        AppFamilyRelationKind.MEMBER,
    ),
    AppFamilyOperation.OBJECT_LINK: _NativeAppSpec(
        "App::Link",
        "ObjectLink",
        ("LinkPlacement", "LinkTransform", "LinkedObject", "Placement"),
        AppFamilyRelationKind.LINK_TARGET,
    ),
    AppFamilyOperation.LINK_GROUP: _NativeAppSpec(
        "App::LinkGroup",
        "LinkGroup",
        ("ElementList", "LinkMode", "Placement"),
        AppFamilyRelationKind.MEMBER,
    ),
    AppFamilyOperation.MATERIAL_DEFINITION: _NativeAppSpec(
        "App::MaterialObject",
        "Material",
        ("Material",),
        AppFamilyRelationKind.NONE,
    ),
    AppFamilyOperation.POSITIONED_PART: _NativeAppSpec(
        "App::Part",
        "Part",
        ("Group", "Origin", "Placement"),
        AppFamilyRelationKind.MEMBER,
    ),
    AppFamilyOperation.PLACEMENT_REFERENCE: _NativeAppSpec(
        "App::Placement",
        "PlacementReference",
        ("Placement",),
        AppFamilyRelationKind.NONE,
    ),
    AppFamilyOperation.TEXT_DOCUMENT: _NativeAppSpec(
        "App::TextDocument",
        "TextDocument",
        ("Text",),
        AppFamilyRelationKind.NONE,
    ),
    AppFamilyOperation.SCALAR_VARIABLE_SET: _NativeAppSpec(
        "App::VarSet",
        "VariableSet",
        ("Value",),
        AppFamilyRelationKind.NONE,
    ),
}

APP_FAMILY_NATIVE_TYPE_IDS: Final = {
    operation: spec.type_id for operation, spec in _NATIVE_APP_SPECS.items()
}
APP_FAMILY_NATIVE_PROPERTIES: Final = {
    operation: spec.properties for operation, spec in _NATIVE_APP_SPECS.items()
}
APP_FAMILY_RELATION_KINDS: Final = {
    operation: spec.relation_kind for operation, spec in _NATIVE_APP_SPECS.items()
}

APP_FAMILY_EXCLUDED_CANDIDATES: Final = {
    "App::LinkElement": "generated-helper-and-duplicate-single-link-semantics",
    "App::LocalCoordinateSystem": (
        "base-of-reviewed-Part::LocalCoordinateSystem-same-user-semantics"
    ),
}

APP_FAMILY_RULE_ID: Final = "freecad.app.document-object-family.v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{APP_FAMILY_FREECAD_ENGINE_BUILD_ID};"
    "ops=annotation:App::Annotation,leader:App::AnnotationLabel,"
    "group:App::DocumentObjectGroup,link:App::Link,link-group:App::LinkGroup,"
    "material:App::MaterialObject,part:App::Part,placement:App::Placement,"
    "text:App::TextDocument,var-set:App::VarSet;"
    "excluded=App::LinkElement:generated-helper,"
    "App::LocalCoordinateSystem:duplicate-reviewed-user-semantics;"
    "relations=authenticated-one-object,no-cycle;"
    "link=LinkTransform:true;part-helpers=Origin-plus-seven;"
    "text=bounded;material=Name,Description,Density;"
    "var-set=one-static-App::PropertyFloat-Value,no-expression;"
    "authority=no-python,no-expression,no-import-path;"
    "ownership=document-root;transaction=shared-rollback"
)
APP_FAMILY_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class AppFamilyRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CYCLE = "cycle"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class AppFamilyRuleError(ValueError):
    """Bounded failure from the reviewed application-object native boundary."""

    def __init__(self, code: AppFamilyRuleErrorCode, path: str = "/") -> None:
        if type(code) is not AppFamilyRuleErrorCode:
            raise TypeError("code must be an exact AppFamilyRuleErrorCode")
        try:
            size = len(path.encode("utf-8")) if type(path) is str else 0
        except UnicodeError:
            size = 385
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isprintable()
            or len(path.splitlines()) != 1
            or size > 384
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"App family rule error ({code.value}) at {path}")


def _fail(code: AppFamilyRuleErrorCode, path: str) -> None:
    raise AppFamilyRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if type(value) not in {int, float}:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    if (
        not math.isfinite(result)
        or (minimum is not None and result < minimum)
        or (maximum is not None and result > maximum)
    ):
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    return result


def _bounded_text(
    value: object,
    path: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    minimum = 0 if allow_empty else 1
    if not minimum <= size <= maximum or "\x00" in value:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object, *, maximum: int = MAX_APP_FAMILY_PLAN_BYTES) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/")
    if not payload or len(payload) > maximum:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/")
    return payload


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_json(raw: object, path: str, *, maximum: int) -> object:
    if type(raw) is not bytes or not raw or len(raw) > maximum:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, path)
    except (UnicodeError, ValueError, RecursionError):
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    if not hmac.compare_digest(raw, _canonical_json(value, maximum=maximum)):
        _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, path)
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    return value


def _vector(value: object, path: str) -> list[float]:
    if type(value) is not list or len(value) != 3:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    return [
        _finite(item, f"{path}/{index}", minimum=-1_000_000, maximum=1_000_000)
        for index, item in enumerate(value)
    ]


def _placement(value: object, path: str) -> dict[str, object]:
    fields = _exact_fields(value, {"position_mm", "axis", "angle_degrees"}, path)
    position = _vector(fields["position_mm"], f"{path}/position_mm")
    axis = _vector(fields["axis"], f"{path}/axis")
    norm = math.sqrt(sum(item * item for item in axis))
    if norm <= 1e-12 or abs(norm - 1.0) > 1e-9:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, f"{path}/axis")
    angle = _finite(
        fields["angle_degrees"],
        f"{path}/angle_degrees",
        minimum=-360,
        maximum=360,
    )
    return {"position_mm": position, "axis": axis, "angle_degrees": angle}


def _annotation_lines(value: object, path: str) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= MAX_ANNOTATION_LINES:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    lines = [
        _bounded_text(item, f"{path}/{index}", maximum=MAX_ANNOTATION_LINE_BYTES)
        for index, item in enumerate(value)
    ]
    if sum(len(item.encode("utf-8")) for item in lines) > MAX_APP_TEXT_BYTES:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, path)
    return lines


def _validated_configuration(
    operation: AppFamilyOperation,
    value: object,
) -> dict[str, object]:
    path = "/operation/configuration"
    if operation is AppFamilyOperation.TEXT_ANNOTATION:
        fields = _exact_fields(value, {"lines", "position_mm"}, path)
        return {
            "lines": _annotation_lines(fields["lines"], f"{path}/lines"),
            "position_mm": _vector(fields["position_mm"], f"{path}/position_mm"),
        }
    if operation is AppFamilyOperation.LEADER_ANNOTATION:
        fields = _exact_fields(value, {"lines", "base_position_mm", "text_position_mm"}, path)
        return {
            "lines": _annotation_lines(fields["lines"], f"{path}/lines"),
            "base_position_mm": _vector(fields["base_position_mm"], f"{path}/base_position_mm"),
            "text_position_mm": _vector(fields["text_position_mm"], f"{path}/text_position_mm"),
        }
    if operation is AppFamilyOperation.DOCUMENT_GROUP:
        _exact_fields(value, set(), path)
        return {}
    if operation in {
        AppFamilyOperation.OBJECT_LINK,
        AppFamilyOperation.LINK_GROUP,
        AppFamilyOperation.POSITIONED_PART,
        AppFamilyOperation.PLACEMENT_REFERENCE,
    }:
        fields = _exact_fields(value, {"placement"}, path)
        return {"placement": _placement(fields["placement"], f"{path}/placement")}
    if operation is AppFamilyOperation.MATERIAL_DEFINITION:
        fields = _exact_fields(value, {"name", "description", "density_kg_m3"}, path)
        return {
            "name": _bounded_text(fields["name"], f"{path}/name", maximum=128),
            "description": _bounded_text(
                fields["description"],
                f"{path}/description",
                maximum=512,
                allow_empty=True,
            ),
            "density_kg_m3": _finite(
                fields["density_kg_m3"],
                f"{path}/density_kg_m3",
                minimum=1e-12,
                maximum=1_000_000_000,
            ),
        }
    if operation is AppFamilyOperation.TEXT_DOCUMENT:
        fields = _exact_fields(value, {"text"}, path)
        return {"text": _bounded_text(fields["text"], f"{path}/text", maximum=MAX_APP_TEXT_BYTES)}
    if operation is AppFamilyOperation.SCALAR_VARIABLE_SET:
        fields = _exact_fields(value, {"value"}, path)
        return {"value": _finite(fields["value"], f"{path}/value", minimum=-1e12, maximum=1e12)}
    _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/operation/kind")


def encode_app_family_configuration(operation: AppFamilyOperation, value: object) -> bytes:
    """Canonicalize one operation-specific bounded semantic configuration."""

    if type(operation) is not AppFamilyOperation:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/operation/kind")
    return _canonical_json(_validated_configuration(operation, value), maximum=16 * 1024)


@dataclass(frozen=True, slots=True, kw_only=True)
class AppFamilyBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    container_id: str
    target_node_id: str
    target_result_id: str
    operation: AppFamilyOperation
    configuration_bytes: bytes
    related_node_id: str | None = None
    related_result_id: str | None = None
    schema_version: int = APP_FAMILY_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "container_id",
            "target_node_id",
            "target_result_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "manifest_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.operation) is not AppFamilyOperation:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/operation")
        relation_kind = APP_FAMILY_RELATION_KINDS[self.operation]
        if relation_kind is AppFamilyRelationKind.NONE:
            if self.related_node_id is not None or self.related_result_id is not None:
                _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/relation")
        else:
            if self.related_node_id is None or self.related_result_id is None:
                _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/relation")
            object.__setattr__(
                self,
                "related_node_id",
                _identifier(self.related_node_id, "/related_node_id"),
            )
            object.__setattr__(
                self,
                "related_result_id",
                _identifier(self.related_result_id, "/related_result_id"),
            )
        if type(self.configuration_bytes) is not bytes:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/configuration")
        config = _decode_json(self.configuration_bytes, "/configuration", maximum=16 * 1024)
        canonical = encode_app_family_configuration(self.operation, config)
        if not hmac.compare_digest(self.configuration_bytes, canonical):
            _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/configuration")

    @property
    def configuration(self) -> dict[str, object]:
        value = _decode_json(self.configuration_bytes, "/configuration", maximum=16 * 1024)
        if type(value) is not dict:
            _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/configuration")
        return value

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        relation = (
            None
            if self.related_node_id is None
            else {
                "node_id": self.related_node_id,
                "result_id": self.related_result_id,
                "kind": APP_FAMILY_RELATION_KINDS[self.operation].value,
            }
        )
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": APP_FAMILY_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": APP_FAMILY_RULE_ID,
                "rule_contract_sha256": APP_FAMILY_RULE_CONTRACT_SHA256,
                "manifest_sha256": self.manifest_sha256,
            },
            "source": {
                "artifact_id": self.source_artifact_id,
                "graph_id": self.source_graph_id,
                "graph_sha256": self.source_graph_sha256,
                "content_sha256": self.source_content_sha256,
            },
            "binding": {
                "lowering_request_sha256": self.lowering_request_sha256,
                "adapter_contract_sha256": self.adapter_contract_sha256,
            },
            "selection": {
                "container_id": self.container_id,
                "target_node_id": self.target_node_id,
                "target_result_id": self.target_result_id,
                "relation": relation,
            },
            "operation": {
                "kind": self.operation.value,
                "configuration": self.configuration,
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> AppFamilyBackendPlan:
        root = _exact_fields(
            value,
            {
                "schema_version",
                "authority",
                "backend",
                "rule",
                "source",
                "binding",
                "selection",
                "operation",
            },
            "/",
        )
        backend = _exact_fields(
            root["backend"], {"engine", "engine_version", "engine_build_id"}, "/backend"
        )
        rule = _exact_fields(
            root["rule"],
            {"rule_id", "rule_contract_sha256", "manifest_sha256"},
            "/rule",
        )
        source = _exact_fields(
            root["source"],
            {"artifact_id", "graph_id", "graph_sha256", "content_sha256"},
            "/source",
        )
        binding = _exact_fields(
            root["binding"],
            {"lowering_request_sha256", "adapter_contract_sha256"},
            "/binding",
        )
        selection = _exact_fields(
            root["selection"],
            {"container_id", "target_node_id", "target_result_id", "relation"},
            "/selection",
        )
        operation = _exact_fields(root["operation"], {"kind", "configuration"}, "/operation")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": APP_FAMILY_FREECAD_ENGINE_BUILD_ID,
            }
            or rule["rule_id"] != APP_FAMILY_RULE_ID
            or rule["rule_contract_sha256"] != APP_FAMILY_RULE_CONTRACT_SHA256
        ):
            _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        try:
            kind = AppFamilyOperation(operation["kind"])
        except (TypeError, ValueError):
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/operation/kind")
        relation = selection["relation"]
        related_node_id = None
        related_result_id = None
        if relation is not None:
            relation_fields = _exact_fields(
                relation, {"node_id", "result_id", "kind"}, "/selection/relation"
            )
            if relation_fields["kind"] != APP_FAMILY_RELATION_KINDS[kind].value:
                _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/selection/relation/kind")
            related_node_id = relation_fields["node_id"]
            related_result_id = relation_fields["result_id"]
        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=binding["lowering_request_sha256"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            manifest_sha256=rule["manifest_sha256"],
            container_id=selection["container_id"],
            target_node_id=selection["target_node_id"],
            target_result_id=selection["target_result_id"],
            operation=kind,
            configuration_bytes=encode_app_family_configuration(kind, operation["configuration"]),
            related_node_id=related_node_id,
            related_result_id=related_result_id,
        )


def decode_app_family_backend_plan(
    raw: bytes,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> AppFamilyBackendPlan:
    value = _decode_json(raw, "/", maximum=MAX_APP_FAMILY_PLAN_BYTES)
    plan = AppFamilyBackendPlan.from_mapping(value)
    if not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256,
        _digest(expected_plan_sha256, "/expected_plan_sha256"),
    ):
        _fail(AppFamilyRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


@dataclass(frozen=True, slots=True, kw_only=True)
class AppFamilyExecutionBindings:
    document: object
    container_id: str
    related_node_id: str | None = None
    related_result_id: str | None = None
    related_object: object | None = None

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/bindings/document")
        object.__setattr__(
            self, "container_id", _identifier(self.container_id, "/bindings/container_id")
        )
        values = (self.related_node_id, self.related_result_id, self.related_object)
        if any(item is None for item in values) and not all(item is None for item in values):
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/bindings/relation")
        if self.related_node_id is not None:
            object.__setattr__(
                self,
                "related_node_id",
                _identifier(self.related_node_id, "/bindings/related_node_id"),
            )
            object.__setattr__(
                self,
                "related_result_id",
                _identifier(self.related_result_id, "/bindings/related_result_id"),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class AppFamilyConformanceReceipt:
    plan_sha256: str
    operation: AppFamilyOperation
    object_name: str
    native_type_id: str
    owned_object_names: tuple[str, ...]
    related_object_name: str | None
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not AppFamilyOperation:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        if self.native_type_id != APP_FAMILY_NATIVE_TYPE_IDS[self.operation]:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/native_type_id")
        if (
            type(self.owned_object_names) is not tuple
            or not self.owned_object_names
            or len(self.owned_object_names) > 16
        ):
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/owned_object_names")
        checked = tuple(
            _identifier(item, f"/owned_object_names/{index}")
            for index, item in enumerate(self.owned_object_names)
        )
        expected_count = 9 if self.operation is AppFamilyOperation.POSITIONED_PART else 1
        if len(checked) != expected_count or checked[0] != self.object_name:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/owned_object_names")
        object.__setattr__(self, "owned_object_names", checked)
        expected_related = (
            APP_FAMILY_RELATION_KINDS[self.operation] is not AppFamilyRelationKind.NONE
        )
        if expected_related:
            object.__setattr__(
                self,
                "related_object_name",
                _identifier(self.related_object_name, "/related_object_name"),
            )
        elif self.related_object_name is not None:
            _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/related_object_name")
        body = {
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "native_type_id": self.native_type_id,
            "owned_object_names": list(self.owned_object_names),
            "related_object_name": self.related_object_name,
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest(),
        )

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _same_identity_sequence(left: object, right: tuple[object, ...]) -> bool:
    try:
        values = tuple(left)
    except Exception:
        return False
    return len(values) == len(right) and all(
        actual is expected for actual, expected in zip(values, right, strict=True)
    )


def _placement_signature(value: object) -> tuple[float, ...]:
    return (
        float(value.Base.x),
        float(value.Base.y),
        float(value.Base.z),
        *(float(item) for item in value.Rotation.Q),
    )


def _snapshot(document: object):
    objects = tuple(document.Objects)
    groups = tuple(
        (item, tuple(item.Group)) for item in objects if "Group" in tuple(item.PropertiesList)
    )
    visibility = tuple(
        (item, bool(item.Visibility))
        for item in objects
        if "Visibility" in tuple(item.PropertiesList)
    )
    placements = tuple(
        (item, _placement_signature(item.Placement))
        for item in objects
        if "Placement" in tuple(item.PropertiesList)
    )
    linked_objects = tuple(
        (item, item.LinkedObject)
        for item in objects
        if "LinkedObject" in tuple(item.PropertiesList)
    )
    element_lists = tuple(
        (item, tuple(item.ElementList))
        for item in objects
        if "ElementList" in tuple(item.PropertiesList)
    )
    return objects, groups, visibility, placements, linked_objects, element_lists


def _rollback_matches(document: object, before: object) -> bool:
    objects, groups, visibility, placements, linked_objects, element_lists = before
    if not _same_identity_sequence(document.Objects, objects):
        return False
    try:
        return (
            all(_same_identity_sequence(item.Group, members) for item, members in groups)
            and all(bool(item.Visibility) is value for item, value in visibility)
            and all(
                _placement_signature(item.Placement) == signature for item, signature in placements
            )
            and all(item.LinkedObject is target for item, target in linked_objects)
            and all(
                _same_identity_sequence(item.ElementList, members)
                for item, members in element_lists
            )
        )
    except Exception:
        return False


def _placement_matches(actual: object, expected: dict[str, object]) -> bool:
    try:
        position = tuple(float(item) for item in expected["position_mm"])
        axis = tuple(float(item) for item in expected["axis"])
        angle_degrees = float(expected["angle_degrees"])
        quaternion = tuple(float(item) for item in actual.Rotation.Q)
        half_angle = math.radians(angle_degrees) / 2.0
        sine = math.sin(half_angle)
        expected_q = (
            axis[0] * sine,
            axis[1] * sine,
            axis[2] * sine,
            math.cos(half_angle),
        )
        same = all(
            abs(left - right) <= 1e-9 for left, right in zip(quaternion, expected_q, strict=True)
        )
        negated = all(
            abs(left + right) <= 1e-9 for left, right in zip(quaternion, expected_q, strict=True)
        )
        return all(
            abs(actual_value - expected_value) <= 1e-9
            for actual_value, expected_value in zip(
                (float(actual.Base.x), float(actual.Base.y), float(actual.Base.z)),
                position,
                strict=True,
            )
        ) and (same or negated)
    except Exception:
        return False


def _freecad_placement(FreeCAD: object, config: dict[str, object]) -> object:
    placement = config["placement"]
    return FreeCAD.Placement(
        FreeCAD.Vector(*placement["position_mm"]),
        FreeCAD.Rotation(FreeCAD.Vector(*placement["axis"]), placement["angle_degrees"]),
    )


def _validate_root_ownership(feature: object) -> None:
    try:
        if feature.getParentGroup() is not None:
            _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
    except AppFamilyRuleError:
        raise
    except Exception:
        _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")


def _validate_no_cycle(feature: object, related: object | None) -> None:
    if related is None:
        return
    try:
        if related is feature or any(item is feature for item in related.OutListRecursive):
            _fail(AppFamilyRuleErrorCode.CYCLE, "/result/relation")
        if not any(item is related for item in feature.OutListRecursive):
            _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/relation")
    except AppFamilyRuleError:
        raise
    except Exception:
        _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/relation")


_PART_HELPERS: Final = (
    ("X_Axis", "App::Line"),
    ("Y_Axis", "App::Line"),
    ("Z_Axis", "App::Line"),
    ("XY_Plane", "App::Plane"),
    ("XZ_Plane", "App::Plane"),
    ("YZ_Plane", "App::Plane"),
    ("Origin", "App::Point"),
)


def _validate_part_helpers(feature: object, created: tuple[object, ...]) -> None:
    try:
        origin = feature.Origin
        helpers = tuple(origin.OriginFeatures)
        if (
            len(created) != 9
            or created[0] is not feature
            or created[1] is not origin
            or origin.TypeId != "App::Origin"
            or tuple(created[2:]) != helpers
            or tuple(origin.Group)
        ):
            _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/origin")
        for helper, (role, type_id) in zip(helpers, _PART_HELPERS, strict=True):
            if (
                helper.TypeId != type_id
                or helper.Role != role
                or helper.Document is not feature.Document
                or tuple(helper.InList) != (origin,)
                or not helper.isValid()
                or tuple(helper.State) != ("Up-to-date",)
            ):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/origin")
    except AppFamilyRuleError:
        raise
    except Exception:
        _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/origin")


def _material_mapping(config: dict[str, object]) -> dict[str, str]:
    density = float(config["density_kg_m3"])
    return {
        "Name": str(config["name"]),
        "Description": str(config["description"]),
        "Density": f"{density:.12g} kg/m^3",
    }


def _configure_feature(
    feature: object,
    operation: AppFamilyOperation,
    config: dict[str, object],
    related: object | None,
    FreeCAD: object,
) -> None:
    if operation is AppFamilyOperation.TEXT_ANNOTATION:
        feature.LabelText = list(config["lines"])
        feature.Position = FreeCAD.Vector(*config["position_mm"])
    elif operation is AppFamilyOperation.LEADER_ANNOTATION:
        feature.LabelText = list(config["lines"])
        feature.BasePosition = FreeCAD.Vector(*config["base_position_mm"])
        feature.TextPosition = FreeCAD.Vector(*config["text_position_mm"])
    elif operation is AppFamilyOperation.DOCUMENT_GROUP:
        feature.addObject(related)
    elif operation is AppFamilyOperation.OBJECT_LINK:
        feature.setLink(related)
        feature.LinkTransform = True
        feature.Placement = _freecad_placement(FreeCAD, config)
    elif operation is AppFamilyOperation.LINK_GROUP:
        feature.setLink([related])
        feature.Placement = _freecad_placement(FreeCAD, config)
    elif operation is AppFamilyOperation.MATERIAL_DEFINITION:
        feature.Material = _material_mapping(config)
    elif operation is AppFamilyOperation.POSITIONED_PART:
        feature.addObject(related)
        feature.Placement = _freecad_placement(FreeCAD, config)
    elif operation is AppFamilyOperation.PLACEMENT_REFERENCE:
        feature.Placement = _freecad_placement(FreeCAD, config)
    elif operation is AppFamilyOperation.TEXT_DOCUMENT:
        feature.Text = config["text"]
    elif operation is AppFamilyOperation.SCALAR_VARIABLE_SET:
        feature.addProperty("App::PropertyFloat", "Value", "Variables", "Reviewed bounded scalar")
        feature.Value = config["value"]
    else:
        _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/operation")


def _validate_feature(
    feature: object,
    operation: AppFamilyOperation,
    config: dict[str, object],
    related: object | None,
    created: tuple[object, ...],
) -> None:
    try:
        if tuple(feature.ExpressionEngine):
            _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/expression")
        if operation is AppFamilyOperation.TEXT_ANNOTATION:
            if tuple(feature.LabelText) != tuple(config["lines"]) or tuple(
                float(item) for item in feature.Position
            ) != tuple(config["position_mm"]):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/annotation")
        elif operation is AppFamilyOperation.LEADER_ANNOTATION:
            if (
                tuple(feature.LabelText) != tuple(config["lines"])
                or tuple(float(item) for item in feature.BasePosition)
                != tuple(config["base_position_mm"])
                or tuple(float(item) for item in feature.TextPosition)
                != tuple(config["text_position_mm"])
            ):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/annotation")
        elif operation is AppFamilyOperation.DOCUMENT_GROUP:
            if not _same_identity_sequence(feature.Group, (related,)):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/membership")
        elif operation is AppFamilyOperation.OBJECT_LINK:
            if (
                feature.LinkedObject is not related
                or not bool(feature.LinkTransform)
                or not _placement_matches(feature.Placement, config["placement"])
                or not _placement_matches(feature.LinkPlacement, config["placement"])
            ):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/link")
        elif operation is AppFamilyOperation.LINK_GROUP:
            if (
                not _same_identity_sequence(feature.ElementList, (related,))
                or feature.LinkMode != "None"
                or not _placement_matches(feature.Placement, config["placement"])
            ):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/link_group")
        elif operation is AppFamilyOperation.MATERIAL_DEFINITION:
            if dict(feature.Material) != _material_mapping(config):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/material")
        elif operation is AppFamilyOperation.POSITIONED_PART:
            if not _same_identity_sequence(feature.Group, (related,)) or not _placement_matches(
                feature.Placement, config["placement"]
            ):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/part")
            _validate_part_helpers(feature, created)
        elif operation is AppFamilyOperation.PLACEMENT_REFERENCE:
            if not _placement_matches(feature.Placement, config["placement"]):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/placement")
        elif operation is AppFamilyOperation.TEXT_DOCUMENT:
            if feature.Text != config["text"]:
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/text")
        elif operation is AppFamilyOperation.SCALAR_VARIABLE_SET:
            if (
                feature.getTypeIdOfProperty("Value") != "App::PropertyFloat"
                or feature.getGroupOfProperty("Value") != "Variables"
                or abs(float(feature.Value) - float(config["value"])) > 1e-12
            ):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/value")
    except AppFamilyRuleError:
        raise
    except Exception:
        _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result")
    _validate_no_cycle(feature, related)


def apply_app_family_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: AppFamilyExecutionBindings,
) -> AppFamilyConformanceReceipt:
    """Execute one exact reviewed application-object plan."""

    if type(bindings) is not AppFamilyExecutionBindings:
        _fail(AppFamilyRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != APP_FAMILY_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_app_family_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if bindings.container_id != plan.container_id:
        _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/bindings/container_id")
    relation_kind = APP_FAMILY_RELATION_KINDS[plan.operation]
    expected_relation = relation_kind is not AppFamilyRelationKind.NONE
    if expected_relation:
        if (
            bindings.related_node_id != plan.related_node_id
            or bindings.related_result_id != plan.related_result_id
            or bindings.related_object is None
        ):
            _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/bindings/relation")
    elif any(
        item is not None
        for item in (
            bindings.related_node_id,
            bindings.related_result_id,
            bindings.related_object,
        )
    ):
        _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/bindings/relation")
    document = bindings.document
    related = bindings.related_object
    spec = _NATIVE_APP_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None or bool(document.HasPendingTransaction):
            _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/document")
        before_objects = tuple(document.Objects)
        if related is not None and (
            related.Document is not document
            or not any(item is related for item in before_objects)
            or not related.isValid()
            or tuple(related.State) != ("Up-to-date",)
            or related.TypeId
            in {"App::Origin", "App::Line", "App::Plane", "App::Point", "App::LinkElement"}
        ):
            _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/relation/object")
    except AppFamilyRuleError:
        raise
    except Exception:
        _fail(AppFamilyRuleErrorCode.PRECONDITION_FAILED, "/document")

    feature: object | None = None
    created: tuple[object, ...] = ()
    config = plan.configuration

    def apply() -> object:
        nonlocal feature, created
        feature = document.addObject(spec.type_id, object_name)
        _configure_feature(feature, plan.operation, config, related, FreeCAD)
        document.recompute()
        after_objects = tuple(document.Objects)
        if not _same_identity_sequence(after_objects[: len(before_objects)], before_objects):
            _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
        created = after_objects[len(before_objects) :]
        expected_count = 9 if plan.operation is AppFamilyOperation.POSITIONED_PART else 1
        try:
            if (
                len(created) != expected_count
                or created[0] is not feature
                or document.getObject(object_name) is not feature
                or feature.TypeId != spec.type_id
                or feature.Document is not document
                or not feature.isValid()
                or tuple(feature.State) != ("Up-to-date",)
            ):
                _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result")
            _validate_root_ownership(feature)
            _validate_feature(feature, plan.operation, config, related, created)
        except AppFamilyRuleError:
            raise
        except Exception:
            _fail(AppFamilyRuleErrorCode.CONFORMANCE_FAILED, "/result")
        return feature

    try:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted App document-object family",
            snapshot=lambda: _snapshot(document),
            apply=apply,
            rollback_matches=lambda before: _rollback_matches(document, before),
        )
    except NativeTransactionError as error:
        _fail(AppFamilyRuleErrorCode.TRANSACTION_FAILED, error.path)
    if feature is None:
        _fail(AppFamilyRuleErrorCode.TRANSACTION_FAILED, "/result")
    return AppFamilyConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        native_type_id=spec.type_id,
        owned_object_names=tuple(item.Name for item in created),
        related_object_name=None if related is None else related.Name,
    )


__all__ = [
    "APP_FAMILY_EXCLUDED_CANDIDATES",
    "APP_FAMILY_FREECAD_ENGINE_BUILD_ID",
    "APP_FAMILY_NATIVE_PROPERTIES",
    "APP_FAMILY_NATIVE_TYPE_IDS",
    "APP_FAMILY_PLAN_MEDIA_TYPE",
    "APP_FAMILY_PLAN_SCHEMA_VERSION",
    "APP_FAMILY_RELATION_KINDS",
    "APP_FAMILY_RULE_CONTRACT_SHA256",
    "APP_FAMILY_RULE_ID",
    "MAX_ANNOTATION_LINES",
    "MAX_APP_FAMILY_PLAN_BYTES",
    "MAX_APP_TEXT_BYTES",
    "AppFamilyBackendPlan",
    "AppFamilyConformanceReceipt",
    "AppFamilyExecutionBindings",
    "AppFamilyOperation",
    "AppFamilyRelationKind",
    "AppFamilyRuleError",
    "AppFamilyRuleErrorCode",
    "apply_app_family_plan",
    "decode_app_family_backend_plan",
    "encode_app_family_configuration",
]
