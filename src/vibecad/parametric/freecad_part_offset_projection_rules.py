"""Trusted FreeCAD rules for reviewed offset and projection semantics.

Plans carry bounded distances and authenticated graph identities only.  They
never carry native type/property names or topology labels.  The sole
``Face1``/``Edge1`` mapping is owned here by the reviewed rule and is permitted
only after the live support and projection shapes are proven to contain
exactly one face and one edge respectively.
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

PART_OFFSET_PLAN_SCHEMA_VERSION: Final = 1
PART_OFFSET_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-part-offset-projection-plan+json"
)
MAX_PART_OFFSET_PLAN_BYTES: Final = 48 * 1024
PART_OFFSET_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
MIN_OFFSET_MM: Final = 0.001
MAX_OFFSET_MM: Final = 100_000.0

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-part-offset-projection-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-part-offset-projection-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-part-offset-projection-receipt.v1\0"


class PartOffsetOperation(StrEnum):
    SOLID_OFFSET = "solid_offset"
    PLANAR_WIRE_OFFSET = "planar_wire_offset"
    EDGE_ON_FACE_PROJECTION = "edge_on_face_projection"


class PartOffsetSourceRole(StrEnum):
    SOLID_SOURCE = "solid_source"
    PLANAR_WIRE_SOURCE = "planar_wire_source"
    SUPPORT_FACE = "support_face"
    PROJECTION_EDGE = "projection_edge"


@dataclass(frozen=True, slots=True)
class _NativeOffsetSpec:
    type_id: str
    object_prefix: str
    properties: tuple[str, ...]
    source_roles: tuple[PartOffsetSourceRole, ...]


_NATIVE_OFFSET_SPECS: Final = {
    PartOffsetOperation.SOLID_OFFSET: _NativeOffsetSpec(
        "Part::Offset",
        "SolidOffset",
        (
            "Fill",
            "Intersection",
            "Join",
            "Mode",
            "SelfIntersection",
            "Shape",
            "Source",
            "Value",
        ),
        (PartOffsetSourceRole.SOLID_SOURCE,),
    ),
    PartOffsetOperation.PLANAR_WIRE_OFFSET: _NativeOffsetSpec(
        "Part::Offset2D",
        "PlanarWireOffset",
        (
            "Fill",
            "Intersection",
            "Join",
            "Mode",
            "SelfIntersection",
            "Shape",
            "Source",
            "Value",
        ),
        (PartOffsetSourceRole.PLANAR_WIRE_SOURCE,),
    ),
    PartOffsetOperation.EDGE_ON_FACE_PROJECTION: _NativeOffsetSpec(
        "Part::ProjectOnSurface",
        "EdgeOnFaceProjection",
        ("Direction", "Height", "Mode", "Offset", "Projection", "Shape", "SupportFace"),
        (PartOffsetSourceRole.SUPPORT_FACE, PartOffsetSourceRole.PROJECTION_EDGE),
    ),
}

PART_OFFSET_NATIVE_TYPE_IDS: Final = {
    operation: spec.type_id for operation, spec in _NATIVE_OFFSET_SPECS.items()
}
PART_OFFSET_NATIVE_PROPERTIES: Final = {
    operation: spec.properties for operation, spec in _NATIVE_OFFSET_SPECS.items()
}
PART_OFFSET_SOURCE_ROLES: Final = {
    operation: spec.source_roles for operation, spec in _NATIVE_OFFSET_SPECS.items()
}
PART_OFFSET_EXCLUDED_CANDIDATES: Final = {
    "Part::Spline": ("shape-only-storage-without-native-control-point-or-recompute-properties")
}

PART_OFFSET_RULE_ID: Final = "freecad.part.offset-projection-family.v1"
_NATIVE_CONTRACT = (
    f"engine=FreeCAD-1.1.0/{PART_OFFSET_FREECAD_ENGINE_BUILD_ID};"
    "ops=solid-offset:Part::Offset,planar-wire-offset:Part::Offset2D,"
    "edge-on-face-projection:Part::ProjectOnSurface;"
    "offset=single-solid-source,Skin,Arc,Fill:false,Intersection:false,"
    "SelfIntersection:false;offset2d=single-closed-planar-wire-source,Pipe,Arc,"
    "Fill:false,Intersection:false,SelfIntersection:false;"
    "projection=authenticated-single-face-support,authenticated-single-edge-source,"
    "Direction:0,0,-1,Mode:Edges,Offset:0,Height:0;"
    "topology-labels=trusted-rule-only-singleton-resolution;"
    "excluded=Part::Spline:shape-only-no-native-edit-recompute;"
    "ownership=document-root;transaction=shared-rollback"
)
PART_OFFSET_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _NATIVE_CONTRACT.encode("ascii")
).hexdigest()


class PartOffsetRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    SELECTION_FAILED = "selection_failed"
    CYCLE = "cycle"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartOffsetRuleError(ValueError):
    """Bounded failure from the reviewed native offset/projection boundary."""

    def __init__(self, code: PartOffsetRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartOffsetRuleErrorCode:
            raise TypeError("code must be an exact PartOffsetRuleErrorCode")
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
        super().__init__(f"Part offset rule error ({code.value}) at {path}")


def _fail(code: PartOffsetRuleErrorCode, path: str) -> None:
    raise PartOffsetRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str) -> float:
    if type(value) not in {int, float}:
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result):
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    return result


def _canonical_json(value: object, *, maximum: int = MAX_PART_OFFSET_PLAN_BYTES) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/")
    if not payload or len(payload) > maximum:
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/")
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
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, path)
    except (UnicodeError, ValueError, RecursionError):
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    if not hmac.compare_digest(raw, _canonical_json(value, maximum=maximum)):
        _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, path)
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, path)
    return value


def _validated_configuration(
    operation: PartOffsetOperation,
    value: object,
) -> dict[str, object]:
    path = "/operation/configuration"
    if operation in {
        PartOffsetOperation.SOLID_OFFSET,
        PartOffsetOperation.PLANAR_WIRE_OFFSET,
    }:
        fields = _exact_fields(value, {"distance_mm"}, path)
        distance = _finite(fields["distance_mm"], f"{path}/distance_mm")
        if not MIN_OFFSET_MM <= abs(distance) <= MAX_OFFSET_MM:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, f"{path}/distance_mm")
        return {"distance_mm": distance}
    if operation is PartOffsetOperation.EDGE_ON_FACE_PROJECTION:
        _exact_fields(value, set(), path)
        return {}
    _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/operation/kind")


def encode_part_offset_configuration(
    operation: PartOffsetOperation,
    value: object,
) -> bytes:
    """Canonicalize one bounded operation-specific configuration."""

    if type(operation) is not PartOffsetOperation:
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/operation/kind")
    return _canonical_json(_validated_configuration(operation, value), maximum=4096)


@dataclass(frozen=True, slots=True, kw_only=True)
class PartOffsetSelection:
    role: PartOffsetSourceRole
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if type(self.role) is not PartOffsetSourceRole:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/selection/role")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/selection/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/selection/result_id"))

    def to_mapping(self) -> dict[str, object]:
        return {"role": self.role.value, "node_id": self.node_id, "result_id": self.result_id}

    @classmethod
    def from_mapping(cls, value: object, path: str) -> PartOffsetSelection:
        fields = _exact_fields(value, {"role", "node_id", "result_id"}, path)
        try:
            role = PartOffsetSourceRole(fields["role"])
        except (TypeError, ValueError):
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, f"{path}/role")
        return cls(role=role, node_id=fields["node_id"], result_id=fields["result_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class PartOffsetBackendPlan:
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
    operation: PartOffsetOperation
    configuration_bytes: bytes
    sources: tuple[PartOffsetSelection, ...]
    schema_version: int = PART_OFFSET_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/schema_version")
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
        if type(self.operation) is not PartOffsetOperation:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/operation")
        if type(self.sources) is not tuple or any(
            type(item) is not PartOffsetSelection for item in self.sources
        ):
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/sources")
        expected_roles = PART_OFFSET_SOURCE_ROLES[self.operation]
        if (
            tuple(item.role for item in self.sources) != expected_roles
            or len({item.node_id for item in self.sources}) != len(self.sources)
            or any(item.node_id == self.target_node_id for item in self.sources)
        ):
            _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, "/sources")
        if type(self.configuration_bytes) is not bytes:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/configuration")
        config = _decode_json(self.configuration_bytes, "/configuration", maximum=4096)
        if not hmac.compare_digest(
            self.configuration_bytes,
            encode_part_offset_configuration(self.operation, config),
        ):
            _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, "/configuration")

    @property
    def configuration(self) -> dict[str, object]:
        value = _decode_json(self.configuration_bytes, "/configuration", maximum=4096)
        if type(value) is not dict:
            _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, "/configuration")
        return value

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "backend": {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PART_OFFSET_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PART_OFFSET_RULE_ID,
                "rule_contract_sha256": PART_OFFSET_RULE_CONTRACT_SHA256,
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
                "sources": [item.to_mapping() for item in self.sources],
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
    def from_mapping(cls, value: object) -> PartOffsetBackendPlan:
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
            root["rule"], {"rule_id", "rule_contract_sha256", "manifest_sha256"}, "/rule"
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
            {"container_id", "target_node_id", "target_result_id", "sources"},
            "/selection",
        )
        operation = _exact_fields(root["operation"], {"kind", "configuration"}, "/operation")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PART_OFFSET_FREECAD_ENGINE_BUILD_ID,
            }
            or rule["rule_id"] != PART_OFFSET_RULE_ID
            or rule["rule_contract_sha256"] != PART_OFFSET_RULE_CONTRACT_SHA256
        ):
            _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        try:
            kind = PartOffsetOperation(operation["kind"])
        except (TypeError, ValueError):
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/operation/kind")
        raw_sources = selection["sources"]
        if type(raw_sources) is not list or len(raw_sources) > 2:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/selection/sources")
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
            configuration_bytes=encode_part_offset_configuration(kind, operation["configuration"]),
            sources=tuple(
                PartOffsetSelection.from_mapping(item, f"/selection/sources/{index}")
                for index, item in enumerate(raw_sources)
            ),
        )


def decode_part_offset_backend_plan(
    raw: bytes,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartOffsetBackendPlan:
    value = _decode_json(raw, "/", maximum=MAX_PART_OFFSET_PLAN_BYTES)
    plan = PartOffsetBackendPlan.from_mapping(value)
    if not hmac.compare_digest(raw, plan.canonical_bytes):
        _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        plan.plan_sha256,
        _digest(expected_plan_sha256, "/expected_plan_sha256"),
    ):
        _fail(PartOffsetRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return plan


@dataclass(frozen=True, slots=True, kw_only=True)
class PartOffsetSourceBinding:
    role: PartOffsetSourceRole
    node_id: str
    result_id: str
    native_object: object

    def __post_init__(self) -> None:
        if type(self.role) is not PartOffsetSourceRole or self.native_object is None:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/bindings/sources")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartOffsetExecutionBindings:
    document: object
    container_id: str
    sources: tuple[PartOffsetSourceBinding, ...]

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/bindings/document")
        object.__setattr__(
            self, "container_id", _identifier(self.container_id, "/bindings/container_id")
        )
        if (
            type(self.sources) is not tuple
            or not 1 <= len(self.sources) <= 2
            or any(type(item) is not PartOffsetSourceBinding for item in self.sources)
        ):
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/bindings/sources")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartOffsetConformanceReceipt:
    plan_sha256: str
    operation: PartOffsetOperation
    object_name: str
    native_type_id: str
    source_object_names: tuple[str, ...]
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartOffsetOperation:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        if self.native_type_id != PART_OFFSET_NATIVE_TYPE_IDS[self.operation]:
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/native_type_id")
        expected_count = len(PART_OFFSET_SOURCE_ROLES[self.operation])
        if (
            type(self.source_object_names) is not tuple
            or len(self.source_object_names) != expected_count
        ):
            _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/source_object_names")
        checked = tuple(
            _identifier(item, f"/source_object_names/{index}")
            for index, item in enumerate(self.source_object_names)
        )
        object.__setattr__(self, "source_object_names", checked)
        body = {
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "native_type_id": self.native_type_id,
            "source_object_names": list(checked),
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


def _link_sub_signature(value: object) -> tuple[object, tuple[str, ...]] | None:
    if value is None:
        return None
    base, names = value
    return base, tuple(names)


def _link_sub_list_signature(value: object) -> tuple[tuple[object, tuple[str, ...]], ...]:
    return tuple((base, tuple(names)) for base, names in value)


def _link_sub_matches(
    current: object,
    expected: tuple[object, tuple[str, ...]] | None,
) -> bool:
    signature = _link_sub_signature(current)
    if signature is None or expected is None:
        return signature is expected
    return signature[0] is expected[0] and signature[1] == expected[1]


def _link_sub_list_matches(
    current: object,
    expected: tuple[tuple[object, tuple[str, ...]], ...],
) -> bool:
    signature = _link_sub_list_signature(current)
    return len(signature) == len(expected) and all(
        actual[0] is wanted[0] and actual[1] == wanted[1]
        for actual, wanted in zip(signature, expected, strict=True)
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
    sources = tuple(
        (item, item.Source) for item in objects if "Source" in tuple(item.PropertiesList)
    )
    support_faces = tuple(
        (item, _link_sub_signature(item.SupportFace))
        for item in objects
        if "SupportFace" in tuple(item.PropertiesList)
    )
    projections = tuple(
        (item, _link_sub_list_signature(item.Projection))
        for item in objects
        if "Projection" in tuple(item.PropertiesList)
    )
    return objects, groups, visibility, placements, sources, support_faces, projections


def _rollback_matches(document: object, before: object) -> bool:
    objects, groups, visibility, placements, sources, support_faces, projections = before
    if not _same_identity_sequence(document.Objects, objects):
        return False
    try:
        return (
            all(_same_identity_sequence(item.Group, members) for item, members in groups)
            and all(bool(item.Visibility) is value for item, value in visibility)
            and all(
                _placement_signature(item.Placement) == signature for item, signature in placements
            )
            and all(item.Source is source for item, source in sources)
            and all(
                _link_sub_matches(item.SupportFace, signature) for item, signature in support_faces
            )
            and all(
                _link_sub_list_matches(item.Projection, signature)
                for item, signature in projections
            )
        )
    except Exception:
        return False


def _validate_source_shape(role: PartOffsetSourceRole, source: object) -> None:
    try:
        shape = source.Shape
        common = not shape.isNull() and shape.isValid()
        if role is PartOffsetSourceRole.SOLID_SOURCE:
            accepted = common and shape.ShapeType == "Solid" and len(shape.Solids) == 1
        elif role is PartOffsetSourceRole.PLANAR_WIRE_SOURCE:
            accepted = (
                common
                and shape.ShapeType == "Wire"
                and len(shape.Wires) == 1
                and shape.isClosed()
                and shape.findPlane() is not None
            )
        elif role is PartOffsetSourceRole.SUPPORT_FACE:
            accepted = common and shape.ShapeType == "Face" and len(shape.Faces) == 1
        elif role is PartOffsetSourceRole.PROJECTION_EDGE:
            accepted = common and shape.ShapeType == "Edge" and len(shape.Edges) == 1
        else:
            accepted = False
        if not accepted:
            _fail(PartOffsetRuleErrorCode.SELECTION_FAILED, f"/sources/{role.value}")
    except PartOffsetRuleError:
        raise
    except Exception:
        _fail(PartOffsetRuleErrorCode.SELECTION_FAILED, f"/sources/{role.value}")


def _validate_root_ownership(feature: object) -> None:
    try:
        if feature.getParentGroup() is not None:
            _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")
    except PartOffsetRuleError:
        raise
    except Exception:
        _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result/ownership")


def _validate_no_cycle(feature: object, sources: tuple[object, ...]) -> None:
    try:
        if any(
            source is feature or any(item is feature for item in source.OutListRecursive)
            for source in sources
        ):
            _fail(PartOffsetRuleErrorCode.CYCLE, "/result/relation")
        if any(not any(item is source for item in feature.OutListRecursive) for source in sources):
            _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result/relation")
    except PartOffsetRuleError:
        raise
    except Exception:
        _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result/relation")


def _configure_feature(
    feature: object,
    operation: PartOffsetOperation,
    config: dict[str, object],
    sources: tuple[object, ...],
    FreeCAD: object,
) -> None:
    if operation is PartOffsetOperation.SOLID_OFFSET:
        feature.Source = sources[0]
        feature.Value = config["distance_mm"]
        feature.Mode = "Skin"
        feature.Join = "Arc"
        feature.Fill = False
        feature.Intersection = False
        feature.SelfIntersection = False
    elif operation is PartOffsetOperation.PLANAR_WIRE_OFFSET:
        feature.Source = sources[0]
        feature.Value = config["distance_mm"]
        feature.Mode = "Pipe"
        feature.Join = "Arc"
        feature.Fill = False
        feature.Intersection = False
        feature.SelfIntersection = False
    elif operation is PartOffsetOperation.EDGE_ON_FACE_PROJECTION:
        feature.SupportFace = (sources[0], ["Face1"])
        feature.Projection = [(sources[1], ["Edge1"])]
        feature.Direction = FreeCAD.Vector(0.0, 0.0, -1.0)
        feature.Mode = "Edges"
        feature.Offset = 0.0
        feature.Height = 0.0
    else:
        _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/operation")


def _validate_feature(
    feature: object,
    operation: PartOffsetOperation,
    config: dict[str, object],
    sources: tuple[object, ...],
) -> None:
    try:
        if tuple(feature.ExpressionEngine) or feature.Shape.isNull() or not feature.Shape.isValid():
            _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result")
        if operation is PartOffsetOperation.SOLID_OFFSET:
            if (
                feature.Source is not sources[0]
                or abs(float(feature.Value) - float(config["distance_mm"])) > 1e-9
                or feature.Mode != "Skin"
                or feature.Join != "Arc"
                or bool(feature.Fill)
                or bool(feature.Intersection)
                or bool(feature.SelfIntersection)
                or feature.Shape.ShapeType != "Solid"
            ):
                _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result/offset")
        elif operation is PartOffsetOperation.PLANAR_WIRE_OFFSET:
            if (
                feature.Source is not sources[0]
                or abs(float(feature.Value) - float(config["distance_mm"])) > 1e-9
                or feature.Mode != "Pipe"
                or feature.Join != "Arc"
                or bool(feature.Fill)
                or bool(feature.Intersection)
                or bool(feature.SelfIntersection)
                or feature.Shape.ShapeType != "Wire"
            ):
                _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result/offset2d")
        elif operation is PartOffsetOperation.EDGE_ON_FACE_PROJECTION:
            support, support_names = feature.SupportFace
            projection = tuple(feature.Projection)
            direction = tuple(float(item) for item in feature.Direction)
            if (
                support is not sources[0]
                or tuple(support_names) != ("Face1",)
                or len(projection) != 1
                or projection[0][0] is not sources[1]
                or tuple(projection[0][1]) != ("Edge1",)
                or direction != (0.0, 0.0, -1.0)
                or feature.Mode != "Edges"
                or abs(float(feature.Offset)) > 1e-12
                or abs(float(feature.Height)) > 1e-12
                or feature.Shape.ShapeType != "Compound"
            ):
                _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result/projection")
        else:
            _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/operation")
    except PartOffsetRuleError:
        raise
    except Exception:
        _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result")
    _validate_no_cycle(feature, sources)


def apply_part_offset_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartOffsetExecutionBindings,
) -> PartOffsetConformanceReceipt:
    """Execute one exact reviewed offset/projection plan."""

    if type(bindings) is not PartOffsetExecutionBindings:
        _fail(PartOffsetRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartOffsetRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PART_OFFSET_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartOffsetRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_part_offset_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    if bindings.container_id != plan.container_id or len(bindings.sources) != len(plan.sources):
        _fail(PartOffsetRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    for binding, selection in zip(bindings.sources, plan.sources, strict=True):
        if (
            binding.role is not selection.role
            or binding.node_id != selection.node_id
            or binding.result_id != selection.result_id
        ):
            _fail(PartOffsetRuleErrorCode.PRECONDITION_FAILED, "/bindings/sources")
    document = bindings.document
    sources = tuple(item.native_object for item in bindings.sources)
    spec = _NATIVE_OFFSET_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        before_objects = tuple(document.Objects)
        if (
            document.getObject(object_name) is not None
            or bool(document.HasPendingTransaction)
            or any(
                left is right
                for index, left in enumerate(sources)
                for right in sources[index + 1 :]
            )
        ):
            _fail(PartOffsetRuleErrorCode.PRECONDITION_FAILED, "/document")
        for binding in bindings.sources:
            source = binding.native_object
            if (
                source.Document is not document
                or not any(item is source for item in before_objects)
                or not source.isValid()
                or tuple(source.State) != ("Up-to-date",)
            ):
                _fail(PartOffsetRuleErrorCode.PRECONDITION_FAILED, "/sources")
            _validate_source_shape(binding.role, source)
    except PartOffsetRuleError:
        raise
    except Exception:
        _fail(PartOffsetRuleErrorCode.PRECONDITION_FAILED, "/document")

    feature: object | None = None
    config = plan.configuration

    def apply() -> object:
        nonlocal feature
        feature = document.addObject(spec.type_id, object_name)
        _configure_feature(feature, plan.operation, config, sources, FreeCAD)
        document.recompute()
        after_objects = tuple(document.Objects)
        try:
            if (
                len(after_objects) != len(before_objects) + 1
                or not _same_identity_sequence(after_objects[:-1], before_objects)
                or after_objects[-1] is not feature
                or document.getObject(object_name) is not feature
                or feature.TypeId != spec.type_id
                or feature.Document is not document
                or not feature.isValid()
                or tuple(feature.State) != ("Up-to-date",)
            ):
                _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result")
            _validate_root_ownership(feature)
            _validate_feature(feature, plan.operation, config, sources)
        except PartOffsetRuleError:
            raise
        except Exception:
            _fail(PartOffsetRuleErrorCode.CONFORMANCE_FAILED, "/result")
        return feature

    try:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted Part offset/projection family",
            snapshot=lambda: _snapshot(document),
            apply=apply,
            rollback_matches=lambda before: _rollback_matches(document, before),
        )
    except NativeTransactionError as error:
        _fail(PartOffsetRuleErrorCode.TRANSACTION_FAILED, error.path)
    if feature is None:
        _fail(PartOffsetRuleErrorCode.TRANSACTION_FAILED, "/result")
    return PartOffsetConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        native_type_id=spec.type_id,
        source_object_names=tuple(item.Name for item in sources),
    )


__all__ = [
    "MAX_OFFSET_MM",
    "MAX_PART_OFFSET_PLAN_BYTES",
    "MIN_OFFSET_MM",
    "PART_OFFSET_EXCLUDED_CANDIDATES",
    "PART_OFFSET_FREECAD_ENGINE_BUILD_ID",
    "PART_OFFSET_NATIVE_PROPERTIES",
    "PART_OFFSET_NATIVE_TYPE_IDS",
    "PART_OFFSET_PLAN_MEDIA_TYPE",
    "PART_OFFSET_PLAN_SCHEMA_VERSION",
    "PART_OFFSET_RULE_CONTRACT_SHA256",
    "PART_OFFSET_RULE_ID",
    "PART_OFFSET_SOURCE_ROLES",
    "PartOffsetBackendPlan",
    "PartOffsetConformanceReceipt",
    "PartOffsetExecutionBindings",
    "PartOffsetOperation",
    "PartOffsetRuleError",
    "PartOffsetRuleErrorCode",
    "PartOffsetSelection",
    "PartOffsetSourceBinding",
    "PartOffsetSourceRole",
    "apply_part_offset_plan",
    "decode_part_offset_backend_plan",
    "encode_part_offset_configuration",
]
