"""Reviewed native rules for the bounded standalone Part core family.

The plan is backend-neutral until this module validates an exact static
operation specification.  Graph text never selects a ``TypeId`` or property;
those values exist only in :data:`PART_CORE_NATIVE_SPECS`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Self

from vibecad.parametric.freecad_partdesign_primitive_rules import (
    PartDesignPrimitiveOperation,
    PartDesignPrimitiveRuleError,
    PrimitiveParameterSet,
)
from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionRunner,
)

PART_CORE_PLAN_SCHEMA_VERSION: Final = 1
PART_CORE_PLAN_MEDIA_TYPE: Final = "application/vnd.vibecad.freecad-part-core-plan+json"
MAX_PART_CORE_PLAN_BYTES: Final = 256 * 1024
MAX_PART_CORE_SOURCES: Final = 16
PART_CORE_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
PART_CORE_RULE_ID: Final = "freecad.part-core.reviewed.v1"

_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-part-core.rule-contract.v1\0"
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-part-core.plan.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-part-core.receipt.v1\0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PartCoreOperation(StrEnum):
    BOX = "box"
    CONE = "cone"
    CYLINDER = "cylinder"
    ELLIPSOID = "ellipsoid"
    PRISM = "prism"
    SPHERE = "sphere"
    TORUS = "torus"
    WEDGE = "wedge"
    CUT = "cut"
    FUSE = "fuse"
    COMMON = "common"
    SECTION = "section"
    MULTI_FUSE = "multi_fuse"
    MULTI_COMMON = "multi_common"
    COMPOUND = "compound"
    MIRROR = "mirror"
    SCALE = "scale"
    REVERSE = "reverse"
    REFINE = "refine"


class PartCoreResultKind(StrEnum):
    SOLID = "solid"
    SECTION = "section"
    COMPOUND = "compound"


class PartCoreRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartCoreRuleError(ValueError):
    def __init__(self, code: PartCoreRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartCoreRuleErrorCode:
            raise TypeError("code must be a PartCoreRuleErrorCode")
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
        super().__init__(f"part core rule error ({code.value}) at {path}")


def _fail(code: PartCoreRuleErrorCode, path: str = "/") -> None:
    raise PartCoreRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    if not 1 <= size <= 128 or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str) -> float:
    if type(value) not in (int, float):
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result):
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    return 0.0 if result == 0.0 else result


def _canonical_json(value: object, *, maximum: int = MAX_PART_CORE_PLAN_BYTES) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(PartCoreRuleErrorCode.INVALID_INPUT)
    if not raw or len(raw) > maximum:
        _fail(PartCoreRuleErrorCode.INVALID_INPUT)
    return raw


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_PART_CORE_PLAN_BYTES:
        _fail(PartCoreRuleErrorCode.INVALID_INPUT)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE)
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail(PartCoreRuleErrorCode.INVALID_INPUT)
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE)
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True)
class PartCoreNativeSpec:
    operation: PartCoreOperation
    type_id: str
    native_operation: str
    property_names: tuple[str, ...]
    result_kind: PartCoreResultKind
    minimum_sources: int
    maximum_sources: int
    primitive_operation: PartDesignPrimitiveOperation | None = None


def _spec(
    operation: PartCoreOperation,
    type_id: str,
    native_operation: str,
    property_names: tuple[str, ...],
    result_kind: PartCoreResultKind,
    minimum_sources: int,
    maximum_sources: int,
    primitive_operation: PartDesignPrimitiveOperation | None = None,
) -> PartCoreNativeSpec:
    return PartCoreNativeSpec(
        operation,
        type_id,
        native_operation,
        property_names,
        result_kind,
        minimum_sources,
        maximum_sources,
        primitive_operation,
    )


PART_CORE_NATIVE_SPECS: Final = {
    PartCoreOperation.BOX: _spec(
        PartCoreOperation.BOX,
        "Part::Box",
        "create_box",
        ("Length", "Width", "Height", "Placement"),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_BOX,
    ),
    PartCoreOperation.CONE: _spec(
        PartCoreOperation.CONE,
        "Part::Cone",
        "create_cone",
        ("Radius1", "Radius2", "Height", "Angle", "Placement"),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_CONE,
    ),
    PartCoreOperation.CYLINDER: _spec(
        PartCoreOperation.CYLINDER,
        "Part::Cylinder",
        "create_cylinder",
        ("Radius", "Height", "Angle", "Placement"),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_CYLINDER,
    ),
    PartCoreOperation.ELLIPSOID: _spec(
        PartCoreOperation.ELLIPSOID,
        "Part::Ellipsoid",
        "create_ellipsoid",
        ("Radius1", "Radius2", "Radius3", "Angle1", "Angle2", "Angle3", "Placement"),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_ELLIPSOID,
    ),
    PartCoreOperation.PRISM: _spec(
        PartCoreOperation.PRISM,
        "Part::Prism",
        "create_prism",
        ("Polygon", "Circumradius", "Height", "FirstAngle", "SecondAngle", "Placement"),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_PRISM,
    ),
    PartCoreOperation.SPHERE: _spec(
        PartCoreOperation.SPHERE,
        "Part::Sphere",
        "create_sphere",
        ("Radius", "Angle1", "Angle2", "Angle3", "Placement"),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_SPHERE,
    ),
    PartCoreOperation.TORUS: _spec(
        PartCoreOperation.TORUS,
        "Part::Torus",
        "create_torus",
        ("Radius1", "Radius2", "Angle1", "Angle2", "Angle3", "Placement"),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_TORUS,
    ),
    PartCoreOperation.WEDGE: _spec(
        PartCoreOperation.WEDGE,
        "Part::Wedge",
        "create_wedge",
        (
            "Xmin",
            "Xmax",
            "X2min",
            "X2max",
            "Ymin",
            "Ymax",
            "Zmin",
            "Zmax",
            "Z2min",
            "Z2max",
            "Placement",
        ),
        PartCoreResultKind.SOLID,
        0,
        0,
        PartDesignPrimitiveOperation.ADDITIVE_WEDGE,
    ),
    PartCoreOperation.CUT: _spec(
        PartCoreOperation.CUT,
        "Part::Cut",
        "cut",
        ("Base", "Tool", "Refine"),
        PartCoreResultKind.SOLID,
        2,
        2,
    ),
    PartCoreOperation.FUSE: _spec(
        PartCoreOperation.FUSE,
        "Part::Fuse",
        "fuse",
        ("Base", "Tool", "Refine"),
        PartCoreResultKind.SOLID,
        2,
        2,
    ),
    PartCoreOperation.COMMON: _spec(
        PartCoreOperation.COMMON,
        "Part::Common",
        "common",
        ("Base", "Tool", "Refine"),
        PartCoreResultKind.SOLID,
        2,
        2,
    ),
    PartCoreOperation.SECTION: _spec(
        PartCoreOperation.SECTION,
        "Part::Section",
        "section",
        ("Base", "Tool", "Approximation", "Refine"),
        PartCoreResultKind.SECTION,
        2,
        2,
    ),
    PartCoreOperation.MULTI_FUSE: _spec(
        PartCoreOperation.MULTI_FUSE,
        "Part::MultiFuse",
        "multi_fuse",
        ("Shapes", "Refine"),
        PartCoreResultKind.SOLID,
        2,
        16,
    ),
    PartCoreOperation.MULTI_COMMON: _spec(
        PartCoreOperation.MULTI_COMMON,
        "Part::MultiCommon",
        "multi_common",
        ("Shapes", "Behavior", "Refine"),
        PartCoreResultKind.SOLID,
        2,
        16,
    ),
    PartCoreOperation.COMPOUND: _spec(
        PartCoreOperation.COMPOUND,
        "Part::Compound",
        "compound",
        ("Links",),
        PartCoreResultKind.COMPOUND,
        2,
        16,
    ),
    PartCoreOperation.MIRROR: _spec(
        PartCoreOperation.MIRROR,
        "Part::Mirroring",
        "mirror",
        ("Source", "Base", "Normal"),
        PartCoreResultKind.SOLID,
        1,
        1,
    ),
    PartCoreOperation.SCALE: _spec(
        PartCoreOperation.SCALE,
        "Part::Scale",
        "scale",
        ("Base", "Uniform", "UniformScale", "XScale", "YScale", "ZScale"),
        PartCoreResultKind.SOLID,
        1,
        1,
    ),
    PartCoreOperation.REVERSE: _spec(
        PartCoreOperation.REVERSE,
        "Part::Reverse",
        "reverse",
        ("Source",),
        PartCoreResultKind.SOLID,
        1,
        1,
    ),
    PartCoreOperation.REFINE: _spec(
        PartCoreOperation.REFINE,
        "Part::Refine",
        "refine",
        ("Source",),
        PartCoreResultKind.SOLID,
        1,
        1,
    ),
}


def _contract_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "engine": {"version": "1.1.0", "build_id": PART_CORE_FREECAD_ENGINE_BUILD_ID},
        "operations": [
            {
                "operation": spec.operation.value,
                "type_id": spec.type_id,
                "native_operation": spec.native_operation,
                "property_names": list(spec.property_names),
                "result_kind": spec.result_kind.value,
                "source_cardinality": [spec.minimum_sources, spec.maximum_sources],
                "primitive_operation": None
                if spec.primitive_operation is None
                else spec.primitive_operation.value,
            }
            for spec in PART_CORE_NATIVE_SPECS.values()
        ],
        "invariants": [
            "exact-build",
            "same-document-authenticated-sources",
            "static-native-map",
            "single-transaction",
            "exact-rollback",
            "valid-shape-and-operation-specific-effect",
        ],
    }


PART_CORE_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _canonical_json(_contract_mapping())
).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCoreSelection:
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/selection/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/selection/result_id"))

    def to_mapping(self) -> dict[str, str]:
        return {"node_id": self.node_id, "result_id": self.result_id}

    @classmethod
    def from_mapping(cls, value: object, path: str) -> Self:
        item = _exact_fields(value, {"node_id", "result_id"}, path)
        return cls(node_id=item["node_id"], result_id=item["result_id"])


_PRIMITIVE_BY_OPERATION: Final = {
    operation: spec.primitive_operation
    for operation, spec in PART_CORE_NATIVE_SPECS.items()
    if spec.primitive_operation is not None
}


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCoreParameterSet:
    operation: PartCoreOperation
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.operation) is not PartCoreOperation or type(self.canonical_bytes) is not bytes:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters")
        normalized = self._normalize(self.operation, _decode_mapping(self.canonical_bytes))
        raw = _canonical_json(normalized, maximum=32 * 1024)
        if not hmac.compare_digest(raw, self.canonical_bytes):
            _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/parameters")

    @classmethod
    def from_value(cls, operation: PartCoreOperation, value: object) -> Self:
        if type(operation) is not PartCoreOperation:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters/operation")
        normalized = cls._normalize(operation, value)
        return cls(
            operation=operation, canonical_bytes=_canonical_json(normalized, maximum=32 * 1024)
        )

    @staticmethod
    def _normalize(operation: PartCoreOperation, value: object) -> dict[str, object]:
        primitive = _PRIMITIVE_BY_OPERATION.get(operation)
        if primitive is not None:
            try:
                return PrimitiveParameterSet.from_value(primitive, value).to_value()
            except PartDesignPrimitiveRuleError:
                _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters")
        if operation is PartCoreOperation.MIRROR:
            item = _exact_fields(value, {"base_point_mm", "normal"}, "/parameters")
            base = item["base_point_mm"]
            normal = item["normal"]
            if (
                type(base) is not list
                or type(normal) is not list
                or len(base) != 3
                or len(normal) != 3
            ):
                _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters")
            checked_base = [
                _finite(part, f"/parameters/base_point_mm/{index}")
                for index, part in enumerate(base)
            ]
            checked_normal = [
                _finite(part, f"/parameters/normal/{index}") for index, part in enumerate(normal)
            ]
            if any(abs(part) > 1_000_000.0 for part in checked_base) or not math.isclose(
                math.sqrt(sum(part * part for part in checked_normal)),
                1.0,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters")
            return {"base_point_mm": checked_base, "normal": checked_normal}
        if operation is PartCoreOperation.SCALE:
            item = _exact_fields(value, {"scale_xyz"}, "/parameters")
            scales = item["scale_xyz"]
            if type(scales) is not list or len(scales) != 3:
                _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters/scale_xyz")
            checked = [
                _finite(part, f"/parameters/scale_xyz/{index}") for index, part in enumerate(scales)
            ]
            if any(not 0.001 <= part <= 1_000.0 for part in checked):
                _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters/scale_xyz")
            return {"scale_xyz": checked}
        return _exact_fields(value, set(), "/parameters")

    @property
    def value(self) -> dict[str, object]:
        return _decode_mapping(self.canonical_bytes)

    def to_mapping(self) -> dict[str, object]:
        return self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCoreBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    operation_specification_sha256: str
    body_id: str
    target: PartCoreSelection
    operation: PartCoreOperation
    sources: tuple[PartCoreSelection, ...]
    parameters: PartCoreParameterSet
    plan_sha256: str = field(init=False)
    canonical_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("source_artifact_id", "source_graph_id", "body_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "manifest_sha256",
            "operation_specification_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if (
            type(self.target) is not PartCoreSelection
            or type(self.operation) is not PartCoreOperation
        ):
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/target")
        if (
            type(self.parameters) is not PartCoreParameterSet
            or self.parameters.operation is not self.operation
        ):
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/parameters")
        sources = self.sources
        if type(sources) is not tuple or any(
            type(item) is not PartCoreSelection for item in sources
        ):
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/sources")
        spec = PART_CORE_NATIVE_SPECS[self.operation]
        if not spec.minimum_sources <= len(sources) <= spec.maximum_sources:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/sources")
        if len(set(sources)) != len(sources) or self.target in sources:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/sources")
        body = self.to_mapping(include_digest=False)
        raw_body = _canonical_json(body)
        plan_sha256 = hashlib.sha256(_PLAN_DIGEST_DOMAIN + raw_body).hexdigest()
        object.__setattr__(self, "plan_sha256", plan_sha256)
        object.__setattr__(
            self, "canonical_bytes", _canonical_json({**body, "plan_sha256": plan_sha256})
        )

    def to_mapping(self, *, include_digest: bool = True) -> dict[str, object]:
        result = {
            "schema_version": PART_CORE_PLAN_SCHEMA_VERSION,
            "authority": "none",
            "source": {
                "artifact_id": self.source_artifact_id,
                "graph_id": self.source_graph_id,
                "graph_sha256": self.source_graph_sha256,
                "content_sha256": self.source_content_sha256,
            },
            "lowering": {
                "request_sha256": self.lowering_request_sha256,
                "adapter_contract_sha256": self.adapter_contract_sha256,
                "manifest_sha256": self.manifest_sha256,
                "operation_specification_sha256": self.operation_specification_sha256,
            },
            "body_id": self.body_id,
            "target": self.target.to_mapping(),
            "operation": self.operation.value,
            "sources": [item.to_mapping() for item in self.sources],
            "parameters": self.parameters.to_mapping(),
        }
        if include_digest:
            result["plan_sha256"] = self.plan_sha256
        return result

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        root = _exact_fields(
            value,
            {
                "schema_version",
                "authority",
                "source",
                "lowering",
                "body_id",
                "target",
                "operation",
                "sources",
                "parameters",
                "plan_sha256",
            },
            "/",
        )
        if root["schema_version"] != PART_CORE_PLAN_SCHEMA_VERSION or root["authority"] != "none":
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/schema_version")
        try:
            operation = PartCoreOperation(root["operation"])
        except (TypeError, ValueError):
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/operation")
        source = _exact_fields(
            root["source"], {"artifact_id", "graph_id", "graph_sha256", "content_sha256"}, "/source"
        )
        lowering = _exact_fields(
            root["lowering"],
            {
                "request_sha256",
                "adapter_contract_sha256",
                "manifest_sha256",
                "operation_specification_sha256",
            },
            "/lowering",
        )
        raw_sources = root["sources"]
        if type(raw_sources) is not list or len(raw_sources) > MAX_PART_CORE_SOURCES:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/sources")
        result = cls(
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=lowering["request_sha256"],
            adapter_contract_sha256=lowering["adapter_contract_sha256"],
            manifest_sha256=lowering["manifest_sha256"],
            operation_specification_sha256=lowering["operation_specification_sha256"],
            body_id=root["body_id"],
            target=PartCoreSelection.from_mapping(root["target"], "/target"),
            operation=operation,
            sources=tuple(
                PartCoreSelection.from_mapping(item, f"/sources/{index}")
                for index, item in enumerate(raw_sources)
            ),
            parameters=PartCoreParameterSet.from_value(operation, root["parameters"]),
        )
        if not hmac.compare_digest(
            result.plan_sha256, _digest(root["plan_sha256"], "/plan_sha256")
        ):
            _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
        return result


def decode_part_core_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartCoreBackendPlan:
    value = _decode_mapping(raw)
    result = PartCoreBackendPlan.from_mapping(value)
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE)
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, _digest(expected_plan_sha256, "/expected_plan_sha256")
    ):
        _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedPartCoreObject:
    object: object
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/bindings/object")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCoreExecutionBindings:
    document: object
    body_id: str
    sources: tuple[AuthenticatedPartCoreObject, ...]

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/bindings/document")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        if (
            type(self.sources) is not tuple
            or len(self.sources) > MAX_PART_CORE_SOURCES
            or any(type(item) is not AuthenticatedPartCoreObject for item in self.sources)
        ):
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/bindings/sources")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartCoreConformanceReceipt:
    plan_sha256: str
    operation: PartCoreOperation
    object_name: str
    source_shape_sha256s: tuple[str, ...]
    result_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/receipt/plan_sha256"))
        if type(self.operation) is not PartCoreOperation:
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/receipt/operation")
        object.__setattr__(
            self, "object_name", _identifier(self.object_name, "/receipt/object_name")
        )
        if (
            type(self.source_shape_sha256s) is not tuple
            or len(self.source_shape_sha256s) > MAX_PART_CORE_SOURCES
            or any(
                type(item) is not str or _SHA256.fullmatch(item) is None
                for item in self.source_shape_sha256s
            )
        ):
            _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/receipt/sources")
        object.__setattr__(
            self, "result_shape_sha256", _digest(self.result_shape_sha256, "/receipt/result")
        )
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "source_shape_sha256s": list(self.source_shape_sha256s),
            "result_shape_sha256": self.result_shape_sha256,
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(
                _RECEIPT_DIGEST_DOMAIN + _canonical_json(body, maximum=32 * 1024)
            ).hexdigest(),
        )

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_signature(
    shape: object, path: str
) -> tuple[str, float, int, int, tuple[float, float, float], tuple[float, float, float]]:
    try:
        if shape is None or shape.isNull() or not shape.isValid():
            _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, path)
        volume = float(shape.Volume)
        solids = len(shape.Solids)
        edges = len(shape.Edges)
        center_value = shape.Solids[0].CenterOfMass if solids == 1 else shape.BoundBox.Center
        center = tuple(float(item) for item in (center_value.x, center_value.y, center_value.z))
        bbox = tuple(
            float(item)
            for item in (shape.BoundBox.XLength, shape.BoundBox.YLength, shape.BoundBox.ZLength)
        )
        raw = shape.exportBrepToString().encode("utf-8")
    except PartCoreRuleError:
        raise
    except (Exception, SystemExit, UnicodeError, OverflowError):
        _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, path)
    values = (volume, *center, *bbox)
    if any(not math.isfinite(item) for item in values):
        _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, path)
    return hashlib.sha256(raw).hexdigest(), volume, solids, edges, center, bbox


def _validate_bindings(plan: PartCoreBackendPlan, bindings: PartCoreExecutionBindings):
    if bindings.body_id != plan.body_id or len(bindings.sources) != len(plan.sources):
        _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/bindings")
    document = bindings.document
    try:
        if getattr(document, "UndoMode", 0) != 1 or bool(document.HasPendingTransaction):
            _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    except PartCoreRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    objects: list[object] = []
    signatures = []
    for index, (selection, authenticated) in enumerate(
        zip(plan.sources, bindings.sources, strict=True)
    ):
        if (
            selection.node_id != authenticated.node_id
            or selection.result_id != authenticated.result_id
        ):
            _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, f"/bindings/sources/{index}")
        obj = authenticated.object
        try:
            if obj.Document is not document or obj not in tuple(document.Objects):
                _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, f"/bindings/sources/{index}")
        except PartCoreRuleError:
            raise
        except (Exception, SystemExit):
            _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, f"/bindings/sources/{index}")
        if any(obj is existing for existing in objects):
            _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/bindings/sources")
        objects.append(obj)
        signatures.append(_shape_signature(obj.Shape, f"/bindings/sources/{index}/shape"))
    return document, tuple(objects), tuple(signatures)


_PRIMITIVE_PROPERTIES: Final = {
    PartCoreOperation.BOX: (
        ("Length", "size_x_mm"),
        ("Width", "size_y_mm"),
        ("Height", "size_z_mm"),
    ),
    PartCoreOperation.CONE: (
        ("Radius1", "base_radius_mm"),
        ("Radius2", "top_radius_mm"),
        ("Height", "height_mm"),
        ("Angle", "sweep_degrees"),
    ),
    PartCoreOperation.CYLINDER: (
        ("Radius", "radius_mm"),
        ("Height", "height_mm"),
        ("Angle", "sweep_degrees"),
    ),
    PartCoreOperation.ELLIPSOID: (
        ("Radius1", "radius_x_mm"),
        ("Radius2", "radius_y_mm"),
        ("Radius3", "radius_z_mm"),
        ("Angle1", "latitude_min_degrees"),
        ("Angle2", "latitude_max_degrees"),
        ("Angle3", "sweep_degrees"),
    ),
    PartCoreOperation.PRISM: (
        ("Polygon", "side_count"),
        ("Circumradius", "circumradius_mm"),
        ("Height", "height_mm"),
    ),
    PartCoreOperation.SPHERE: (
        ("Radius", "radius_mm"),
        ("Angle1", "latitude_min_degrees"),
        ("Angle2", "latitude_max_degrees"),
        ("Angle3", "sweep_degrees"),
    ),
    PartCoreOperation.TORUS: (
        ("Radius1", "major_radius_mm"),
        ("Radius2", "minor_radius_mm"),
        ("Angle1", "latitude_min_degrees"),
        ("Angle2", "latitude_max_degrees"),
        ("Angle3", "sweep_degrees"),
    ),
    PartCoreOperation.WEDGE: (
        ("Xmin", "x_min_mm"),
        ("Ymin", "y_min_mm"),
        ("Zmin", "z_min_mm"),
        ("X2min", "x_inner_min_mm"),
        ("Z2min", "z_inner_min_mm"),
        ("Xmax", "x_max_mm"),
        ("Ymax", "y_max_mm"),
        ("Zmax", "z_max_mm"),
        ("X2max", "x_inner_max_mm"),
        ("Z2max", "z_inner_max_mm"),
    ),
}


def _configure_result(
    FreeCAD: object, result: object, plan: PartCoreBackendPlan, sources: tuple[object, ...]
) -> None:
    value = plan.parameters.value
    if plan.operation in _PRIMITIVE_PROPERTIES:
        shape = value["shape"]
        placement = value["placement"]
        for native_name, semantic_name in _PRIMITIVE_PROPERTIES[plan.operation]:
            setattr(result, native_name, shape[semantic_name])
        if plan.operation is PartCoreOperation.PRISM:
            result.FirstAngle = 0.0
            result.SecondAngle = 0.0
        result.Placement = FreeCAD.Placement(
            FreeCAD.Vector(*placement["translation_mm"]),
            FreeCAD.Rotation(
                FreeCAD.Vector(*placement["rotation_axis"]), placement["rotation_degrees"]
            ),
        )
    elif plan.operation in (
        PartCoreOperation.CUT,
        PartCoreOperation.FUSE,
        PartCoreOperation.COMMON,
    ):
        result.Base, result.Tool, result.Refine = sources[0], sources[1], True
    elif plan.operation is PartCoreOperation.SECTION:
        result.Base, result.Tool, result.Approximation, result.Refine = (
            sources[0],
            sources[1],
            False,
            True,
        )
    elif plan.operation is PartCoreOperation.MULTI_FUSE:
        result.Shapes, result.Refine = list(sources), True
    elif plan.operation is PartCoreOperation.MULTI_COMMON:
        result.Shapes = list(sources)
        result.Behavior = "CommonOfAllShapes"
        result.Refine = True
    elif plan.operation is PartCoreOperation.COMPOUND:
        result.Links = list(sources)
    elif plan.operation is PartCoreOperation.MIRROR:
        result.Source = sources[0]
        result.Base = FreeCAD.Vector(*value["base_point_mm"])
        result.Normal = FreeCAD.Vector(*value["normal"])
    elif plan.operation is PartCoreOperation.SCALE:
        scales = value["scale_xyz"]
        result.Base = sources[0]
        uniform = math.isclose(scales[0], scales[1], rel_tol=0.0, abs_tol=1e-12) and math.isclose(
            scales[1], scales[2], rel_tol=0.0, abs_tol=1e-12
        )
        result.Uniform = uniform
        result.UniformScale = scales[0]
        result.XScale, result.YScale, result.ZScale = scales
    elif plan.operation in (PartCoreOperation.REVERSE, PartCoreOperation.REFINE):
        result.Source = sources[0]
    else:
        _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/operation")


def _vector_values(value: object, path: str) -> tuple[float, float, float]:
    try:
        result = (float(value.x), float(value.y), float(value.z))
    except (Exception, SystemExit, OverflowError):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, path)
    if any(not math.isfinite(item) for item in result):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, path)
    return result


def _placement_values(value: object, path: str) -> tuple[float, ...]:
    try:
        matrix = value.toMatrix()
        result = tuple(
            float(getattr(matrix, name))
            for name in (
                "A11",
                "A12",
                "A13",
                "A14",
                "A21",
                "A22",
                "A23",
                "A24",
                "A31",
                "A32",
                "A33",
                "A34",
                "A41",
                "A42",
                "A43",
                "A44",
            )
        )
    except (Exception, SystemExit, OverflowError):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, path)
    if any(not math.isfinite(item) for item in result):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, path)
    return result


def _assert_close(actual: object, expected: object, path: str) -> None:
    try:
        matches = math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError, OverflowError):
        matches = False
    if not matches:
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, path)


def _validate_native_readback(
    FreeCAD: object,
    result: object,
    plan: PartCoreBackendPlan,
    sources: tuple[object, ...],
) -> None:
    operation = plan.operation
    value = plan.parameters.value
    if operation in _PRIMITIVE_PROPERTIES:
        shape = value["shape"]
        for index, (native_name, semantic_name) in enumerate(_PRIMITIVE_PROPERTIES[operation]):
            actual = getattr(result, native_name)
            expected = shape[semantic_name]
            if semantic_name == "side_count":
                if int(actual) != expected:
                    _fail(
                        PartCoreRuleErrorCode.CONFORMANCE_FAILED,
                        f"/result/properties/{index}",
                    )
            else:
                _assert_close(actual, expected, f"/result/properties/{index}")
        placement = value["placement"]
        expected_placement = FreeCAD.Placement(
            FreeCAD.Vector(*placement["translation_mm"]),
            FreeCAD.Rotation(
                FreeCAD.Vector(*placement["rotation_axis"]),
                placement["rotation_degrees"],
            ),
        )
        for index, (actual, expected) in enumerate(
            zip(
                _placement_values(result.Placement, "/result/placement"),
                _placement_values(expected_placement, "/result/placement"),
                strict=True,
            )
        ):
            _assert_close(actual, expected, f"/result/placement/{index}")
        if operation is PartCoreOperation.PRISM:
            _assert_close(result.FirstAngle, 0.0, "/result/first_angle")
            _assert_close(result.SecondAngle, 0.0, "/result/second_angle")
        return
    if operation in (
        PartCoreOperation.CUT,
        PartCoreOperation.FUSE,
        PartCoreOperation.COMMON,
        PartCoreOperation.SECTION,
    ):
        if result.Base is not sources[0] or result.Tool is not sources[1]:
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/links")
        if not bool(result.Refine):
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/refine")
        if operation is PartCoreOperation.SECTION and bool(result.Approximation):
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/approximation")
        return
    if operation in (PartCoreOperation.MULTI_FUSE, PartCoreOperation.MULTI_COMMON):
        if tuple(result.Shapes) != sources or not bool(result.Refine):
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/links")
        if operation is PartCoreOperation.MULTI_COMMON and result.Behavior != "CommonOfAllShapes":
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/behavior")
        return
    if operation is PartCoreOperation.COMPOUND:
        if tuple(result.Links) != sources:
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/links")
        return
    if operation is PartCoreOperation.MIRROR:
        if result.Source is not sources[0]:
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/source")
        for index, (actual, expected) in enumerate(
            zip(
                _vector_values(result.Base, "/result/base"),
                value["base_point_mm"],
                strict=True,
            )
        ):
            _assert_close(actual, expected, f"/result/base/{index}")
        for index, (actual, expected) in enumerate(
            zip(
                _vector_values(result.Normal, "/result/normal"),
                value["normal"],
                strict=True,
            )
        ):
            _assert_close(actual, expected, f"/result/normal/{index}")
        return
    if operation is PartCoreOperation.SCALE:
        scales = value["scale_xyz"]
        expected_uniform = math.isclose(
            scales[0], scales[1], rel_tol=0.0, abs_tol=1e-12
        ) and math.isclose(scales[1], scales[2], rel_tol=0.0, abs_tol=1e-12)
        if result.Base is not sources[0] or bool(result.Uniform) is not expected_uniform:
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/scale")
        _assert_close(result.UniformScale, scales[0], "/result/uniform_scale")
        for index, native_name in enumerate(("XScale", "YScale", "ZScale")):
            _assert_close(
                getattr(result, native_name),
                scales[index],
                f"/result/scale/{index}",
            )
        return
    if operation in (PartCoreOperation.REVERSE, PartCoreOperation.REFINE):
        if result.Source is not sources[0]:
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/source")
        return
    _fail(PartCoreRuleErrorCode.INTEGRITY_FAILURE, "/operation")


def _validate_effect(
    plan: PartCoreBackendPlan, result: object, source_signatures: tuple[tuple, ...]
) -> tuple:
    signature = _shape_signature(result.Shape, "/result/shape")
    _sha, volume, solids, edges, center, _bbox = signature
    source_volumes = tuple(item[1] for item in source_signatures)
    epsilon = max(1e-8, max((abs(item) for item in source_volumes), default=1.0) * 1e-10)
    operation = plan.operation
    spec = PART_CORE_NATIVE_SPECS[operation]
    if spec.result_kind is PartCoreResultKind.SOLID and (solids != 1 or abs(volume) <= epsilon):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/solid")
    if spec.result_kind is PartCoreResultKind.SECTION and (
        solids != 0 or edges < 1 or abs(volume) > epsilon
    ):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/section")
    if spec.result_kind is PartCoreResultKind.COMPOUND:
        try:
            if result.Shape.ShapeType != "Compound" or len(result.Shape.childShapes()) != len(
                source_signatures
            ):
                _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/compound")
        except PartCoreRuleError:
            raise
        except Exception:
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/compound")
    if operation is PartCoreOperation.CUT and not volume < source_volumes[0] - epsilon:
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/effect")
    if operation is PartCoreOperation.FUSE and not volume > max(source_volumes) + epsilon:
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/effect")
    if operation is PartCoreOperation.COMMON and not 0.0 < volume < min(source_volumes) - epsilon:
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/effect")
    if operation is PartCoreOperation.MULTI_FUSE and not volume > max(source_volumes) + epsilon:
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/effect")
    if (
        operation is PartCoreOperation.MULTI_COMMON
        and not 0.0 < volume < min(source_volumes) - epsilon
    ):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/effect")
    if operation is PartCoreOperation.MIRROR:
        base, normal = plan.parameters.value["base_point_mm"], plan.parameters.value["normal"]
        source_center = source_signatures[0][4]
        displacement = sum(
            (source_center[index] - base[index]) * normal[index] for index in range(3)
        )
        expected = tuple(
            source_center[index] - 2.0 * displacement * normal[index] for index in range(3)
        )
        if not math.isclose(abs(volume), abs(source_volumes[0]), rel_tol=1e-9, abs_tol=1e-8) or any(
            not math.isclose(center[index], expected[index], rel_tol=0.0, abs_tol=1e-7)
            for index in range(3)
        ):
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/mirror")
    if operation is PartCoreOperation.SCALE:
        expected = abs(source_volumes[0]) * math.prod(plan.parameters.value["scale_xyz"])
        if not math.isclose(abs(volume), expected, rel_tol=1e-8, abs_tol=1e-7):
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/scale")
    if operation is PartCoreOperation.REVERSE and not math.isclose(
        volume, -source_volumes[0], rel_tol=1e-8, abs_tol=1e-7
    ):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/reverse")
    if operation is PartCoreOperation.REFINE and (
        not math.isclose(abs(volume), abs(source_volumes[0]), rel_tol=1e-8, abs_tol=1e-7)
        or not edges < source_signatures[0][3]
    ):
        _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result/refine")
    return signature


def apply_part_core_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartCoreExecutionBindings,
) -> PartCoreConformanceReceipt:
    """Apply one exact reviewed Part operation; this is the explicit authority seam."""

    if type(bindings) is not PartCoreExecutionBindings:
        _fail(PartCoreRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PART_CORE_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_part_core_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    document, sources, source_signatures = _validate_bindings(plan, bindings)
    spec = PART_CORE_NATIVE_SPECS[plan.operation]
    object_name = f"VcPart_{plan.operation.value}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_objects = tuple(document.Objects)
        before_visibilities = tuple(bool(item.Visibility) for item in before_objects)
    except PartCoreRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartCoreRuleErrorCode.PRECONDITION_FAILED, "/document")

    result_holder: list[tuple[object, tuple]] = []

    def snapshot() -> object:
        return before_objects, before_visibilities

    def apply() -> object:
        result = document.addObject(spec.type_id, object_name)
        _configure_result(FreeCAD, result, plan, sources)
        document.recompute()
        try:
            if (
                result.TypeId != spec.type_id
                or not result.isValid()
                or "Invalid" in tuple(result.State)
                or "Touched" in tuple(result.State)
            ):
                _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result")
        except PartCoreRuleError:
            raise
        except (Exception, SystemExit):
            _fail(PartCoreRuleErrorCode.CONFORMANCE_FAILED, "/result")
        _validate_native_readback(FreeCAD, result, plan, sources)
        signature = _validate_effect(plan, result, source_signatures)
        result_holder.append((result, signature))
        return result

    def rollback_matches(before: object) -> bool:
        expected_objects, expected_visibilities = before
        try:
            current = tuple(document.Objects)
            return (
                len(current) == len(expected_objects)
                and all(
                    left is right for left, right in zip(current, expected_objects, strict=True)
                )
                and tuple(bool(item.Visibility) for item in current) == expected_visibilities
                and document.getObject(object_name) is None
            )
        except BaseException:
            return False

    try:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD reviewed Part core",
            snapshot=snapshot,
            apply=apply,
            rollback_matches=rollback_matches,
        )
    except NativeTransactionError:
        _fail(PartCoreRuleErrorCode.TRANSACTION_FAILED, "/transaction")
    if len(result_holder) != 1:
        _fail(PartCoreRuleErrorCode.TRANSACTION_FAILED, "/transaction/result")
    result, result_signature = result_holder[0]
    return PartCoreConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=result.Name,
        source_shape_sha256s=tuple(item[0] for item in source_signatures),
        result_shape_sha256=result_signature[0],
    )


__all__ = [
    "MAX_PART_CORE_PLAN_BYTES",
    "PART_CORE_FREECAD_ENGINE_BUILD_ID",
    "PART_CORE_NATIVE_SPECS",
    "PART_CORE_PLAN_MEDIA_TYPE",
    "PART_CORE_RULE_CONTRACT_SHA256",
    "PART_CORE_RULE_ID",
    "AuthenticatedPartCoreObject",
    "PartCoreBackendPlan",
    "PartCoreConformanceReceipt",
    "PartCoreExecutionBindings",
    "PartCoreNativeSpec",
    "PartCoreOperation",
    "PartCoreParameterSet",
    "PartCoreResultKind",
    "PartCoreRuleError",
    "PartCoreRuleErrorCode",
    "PartCoreSelection",
    "apply_part_core_plan",
    "decode_part_core_backend_plan",
]
