"""Reviewed FreeCAD rules for bounded Part profile and surface operations.

The backend plan carries semantic operations, authenticated source identities,
and bounded values only. Native ``TypeId`` and property selection is owned by
the static table in this module. Plans and receipts never grant execution
authority; :func:`apply_part_profile_surface_plan` is an explicit trusted-host
boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Self

from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionRunner,
)

PART_PROFILE_SURFACE_PLAN_SCHEMA_VERSION: Final = 1
PART_PROFILE_SURFACE_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-part-profile-surface-plan+json"
)
MAX_PART_PROFILE_SURFACE_PLAN_BYTES: Final = 128 * 1024
MAX_PART_PROFILE_SURFACE_SOURCES: Final = 8
PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
PART_PROFILE_SURFACE_RULE_ID: Final = "freecad.part-profile-surface.reviewed.v1"

_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-part-profile-surface.rule-contract.v1\0"
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-part-profile-surface.plan.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-part-profile-surface.receipt.v1\0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SHAPE_BREP_BYTES = 4 * 1024 * 1024


class PartProfileSurfaceOperation(StrEnum):
    EXTRUSION = "extrusion"
    REVOLUTION = "revolution"
    LOFT = "loft"
    SWEEP = "sweep"
    RULED_SURFACE = "ruled_surface"
    FACE = "face"


class PartProfileSurfaceSourceRole(StrEnum):
    PROFILE = "profile"
    SPINE = "spine"
    CURVE = "curve"
    BOUNDARY = "boundary"


class PartProfileSurfaceResultKind(StrEnum):
    SOLID = "solid"
    SURFACE = "surface"


class PartProfileSurfaceRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartProfileSurfaceRuleError(ValueError):
    """Bounded stable failure at the reviewed native boundary."""

    def __init__(self, code: PartProfileSurfaceRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartProfileSurfaceRuleErrorCode:
            raise TypeError("code must be a PartProfileSurfaceRuleErrorCode")
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
        super().__init__(f"Part profile/surface rule error ({code.value}) at {path}")


def _fail(code: PartProfileSurfaceRuleErrorCode, path: str = "/") -> None:
    raise PartProfileSurfaceRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or len(value) > 128 or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    return value


def _finite(value: object, path: str) -> float:
    if type(value) not in {int, float}:
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    if not math.isfinite(result):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    return 0.0 if result == 0.0 else result


def _vector(value: object, path: str, *, normalized: bool = False) -> list[float]:
    if type(value) is not list or len(value) != 3:
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    result = [_finite(item, f"{path}/{index}") for index, item in enumerate(value)]
    if any(abs(item) > 1_000_000.0 for item in result):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    if normalized and not math.isclose(
        math.sqrt(sum(item * item for item in result)),
        1.0,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    return result


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object, *, maximum: int = MAX_PART_PROFILE_SURFACE_PLAN_BYTES) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT)
    if not payload or len(payload) > maximum:
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT)
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


def _decode_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_PART_PROFILE_SURFACE_PLAN_BYTES:
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE)
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT)
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE)
    return value


@dataclass(frozen=True, slots=True)
class ProfileSurfaceSourceRequirement:
    role: PartProfileSurfaceSourceRole
    minimum: int
    maximum: int
    ordered: bool


@dataclass(frozen=True, slots=True)
class NativeProfileSurfaceSpec:
    type_id: str
    object_prefix: str
    native_operation: str
    native_property_names: tuple[str, ...]
    source_requirements: tuple[ProfileSurfaceSourceRequirement, ...]
    result_kind: PartProfileSurfaceResultKind


def _requirement(
    role: PartProfileSurfaceSourceRole,
    minimum: int,
    maximum: int,
    *,
    ordered: bool,
) -> ProfileSurfaceSourceRequirement:
    return ProfileSurfaceSourceRequirement(role, minimum, maximum, ordered)


PART_PROFILE_SURFACE_NATIVE_SPECS: Final = MappingProxyType(
    {
        PartProfileSurfaceOperation.EXTRUSION: NativeProfileSurfaceSpec(
            "Part::Extrusion",
            "Extrusion",
            "extrude_closed_profile",
            (
                "Base",
                "Dir",
                "LengthFwd",
                "LengthRev",
                "Solid",
                "Reversed",
                "Symmetric",
                "TaperAngle",
                "TaperAngleRev",
                "DirMode",
                "FaceMakerClass",
                "FaceMakerMode",
            ),
            (_requirement(PartProfileSurfaceSourceRole.PROFILE, 1, 1, ordered=False),),
            PartProfileSurfaceResultKind.SOLID,
        ),
        PartProfileSurfaceOperation.REVOLUTION: NativeProfileSurfaceSpec(
            "Part::Revolution",
            "Revolution",
            "revolve_closed_profile",
            ("Source", "Base", "Axis", "Angle", "Solid", "Symmetric", "FaceMakerClass"),
            (_requirement(PartProfileSurfaceSourceRole.PROFILE, 1, 1, ordered=False),),
            PartProfileSurfaceResultKind.SOLID,
        ),
        PartProfileSurfaceOperation.LOFT: NativeProfileSurfaceSpec(
            "Part::Loft",
            "Loft",
            "loft_ordered_closed_profiles",
            ("Sections", "Solid", "Closed", "Ruled", "Linearize"),
            (_requirement(PartProfileSurfaceSourceRole.PROFILE, 2, 8, ordered=True),),
            PartProfileSurfaceResultKind.SOLID,
        ),
        PartProfileSurfaceOperation.SWEEP: NativeProfileSurfaceSpec(
            "Part::Sweep",
            "Sweep",
            "sweep_ordered_closed_profiles_along_single_edge_spine",
            ("Sections", "Spine", "Solid", "Frenet", "Transition", "Linearize"),
            (
                _requirement(PartProfileSurfaceSourceRole.PROFILE, 1, 4, ordered=True),
                _requirement(PartProfileSurfaceSourceRole.SPINE, 1, 1, ordered=False),
            ),
            PartProfileSurfaceResultKind.SOLID,
        ),
        PartProfileSurfaceOperation.RULED_SURFACE: NativeProfileSurfaceSpec(
            "Part::RuledSurface",
            "RuledSurface",
            "rule_between_ordered_single_edges",
            ("Curve1", "Curve2", "Orientation"),
            (_requirement(PartProfileSurfaceSourceRole.CURVE, 2, 2, ordered=True),),
            PartProfileSurfaceResultKind.SURFACE,
        ),
        PartProfileSurfaceOperation.FACE: NativeProfileSurfaceSpec(
            "Part::Face",
            "Face",
            "face_from_closed_planar_boundary",
            ("Sources", "FaceMakerClass"),
            (_requirement(PartProfileSurfaceSourceRole.BOUNDARY, 1, 1, ordered=False),),
            PartProfileSurfaceResultKind.SURFACE,
        ),
    }
)


def _contract_mapping() -> dict[str, object]:
    return {
        "engine": {
            "name": "FreeCAD",
            "version": "1.1.0",
            "build_id": PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID,
        },
        "operation_contracts": [
            {
                "operation": operation.value,
                "type_id": spec.type_id,
                "native_operation": spec.native_operation,
                "properties": list(spec.native_property_names),
                "result_kind": spec.result_kind.value,
                "source_requirements": [
                    {
                        "role": item.role.value,
                        "minimum": item.minimum,
                        "maximum": item.maximum,
                        "ordered": item.ordered,
                    }
                    for item in spec.source_requirements
                ],
            }
            for operation, spec in PART_PROFILE_SURFACE_NATIVE_SPECS.items()
        ],
        "fixed": {
            "solid_operations": ["extrusion", "revolution", "loft", "sweep"],
            "surface_operations": ["ruled_surface", "face"],
            "profiles": "closed-planar-wire",
            "spine_and_curve": "single-edge",
            "transaction": "exact-rollback",
        },
    }


PART_PROFILE_SURFACE_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _canonical_json(_contract_mapping())
).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartProfileSurfaceParameterSet:
    operation: PartProfileSurfaceOperation
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        if type(self.operation) is not PartProfileSurfaceOperation:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters/operation")
        if type(self.canonical_bytes) is not bytes:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters")
        normalized = self._normalize(self.operation, _decode_mapping(self.canonical_bytes))
        if not hmac.compare_digest(
            self.canonical_bytes,
            _canonical_json(normalized, maximum=16 * 1024),
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE, "/parameters")

    @classmethod
    def from_value(
        cls,
        operation: PartProfileSurfaceOperation,
        value: object,
    ) -> Self:
        normalized = cls._normalize(operation, value)
        return cls(
            operation=operation,
            canonical_bytes=_canonical_json(normalized, maximum=16 * 1024),
        )

    @staticmethod
    def _normalize(
        operation: PartProfileSurfaceOperation,
        value: object,
    ) -> dict[str, object]:
        if type(operation) is not PartProfileSurfaceOperation:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters/operation")
        if operation is PartProfileSurfaceOperation.EXTRUSION:
            item = _exact_fields(
                value,
                {"direction", "forward_length_mm", "reverse_length_mm"},
                "/parameters",
            )
            forward = _finite(item["forward_length_mm"], "/parameters/forward_length_mm")
            reverse = _finite(item["reverse_length_mm"], "/parameters/reverse_length_mm")
            if not 0.001 <= forward <= 100_000.0 or not 0.0 <= reverse <= 100_000.0:
                _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters/length")
            return {
                "direction": _vector(item["direction"], "/parameters/direction", normalized=True),
                "forward_length_mm": forward,
                "reverse_length_mm": reverse,
            }
        if operation is PartProfileSurfaceOperation.REVOLUTION:
            item = _exact_fields(
                value,
                {"axis_origin_mm", "axis_direction", "angle_degrees"},
                "/parameters",
            )
            angle = _finite(item["angle_degrees"], "/parameters/angle_degrees")
            if not 0.1 <= angle <= 360.0:
                _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters/angle_degrees")
            return {
                "axis_origin_mm": _vector(item["axis_origin_mm"], "/parameters/axis_origin_mm"),
                "axis_direction": _vector(
                    item["axis_direction"],
                    "/parameters/axis_direction",
                    normalized=True,
                ),
                "angle_degrees": angle,
            }
        if operation is PartProfileSurfaceOperation.LOFT:
            item = _exact_fields(value, {"ruled"}, "/parameters")
            if type(item["ruled"]) is not bool:
                _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters/ruled")
            return {"ruled": item["ruled"]}
        if operation is PartProfileSurfaceOperation.SWEEP:
            item = _exact_fields(value, {"frenet"}, "/parameters")
            if type(item["frenet"]) is not bool:
                _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters/frenet")
            return {"frenet": item["frenet"]}
        return _exact_fields(value, set(), "/parameters")

    @property
    def value(self) -> dict[str, object]:
        return _decode_mapping(self.canonical_bytes)

    def to_mapping(self) -> dict[str, object]:
        return self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class PartProfileSurfaceSelection:
    role: PartProfileSurfaceSourceRole
    node_id: str
    result_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if type(self.role) is not PartProfileSurfaceSourceRole:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/selection/role")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/selection/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/selection/result_id"))
        if (
            type(self.ordinal) is not int
            or not 0 <= self.ordinal < MAX_PART_PROFILE_SURFACE_SOURCES
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/selection/ordinal")

    def to_mapping(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "node_id": self.node_id,
            "result_id": self.result_id,
            "ordinal": self.ordinal,
        }

    @classmethod
    def from_mapping(cls, value: object, path: str) -> Self:
        item = _exact_fields(value, {"role", "node_id", "result_id", "ordinal"}, path)
        try:
            role = PartProfileSurfaceSourceRole(item["role"])
        except (TypeError, ValueError):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, f"{path}/role")
        return cls(
            role=role,
            node_id=item["node_id"],
            result_id=item["result_id"],
            ordinal=item["ordinal"],
        )


def _validate_selection_contract(
    operation: PartProfileSurfaceOperation,
    sources: tuple[PartProfileSurfaceSelection, ...],
) -> None:
    spec = PART_PROFILE_SURFACE_NATIVE_SPECS[operation]
    expected: list[tuple[PartProfileSurfaceSourceRole, int]] = []
    offset = 0
    for requirement in spec.source_requirements:
        group = tuple(item for item in sources if item.role is requirement.role)
        if not requirement.minimum <= len(group) <= requirement.maximum:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/sources")
        if tuple(item.ordinal for item in group) != tuple(range(len(group))):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/sources/ordinal")
        expected.extend((requirement.role, ordinal) for ordinal in range(len(group)))
        offset += len(group)
    if offset != len(sources) or tuple((item.role, item.ordinal) for item in sources) != tuple(
        expected
    ):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/sources/order")
    identities = tuple((item.node_id, item.result_id) for item in sources)
    if len(set(identities)) != len(identities):
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/sources/identity")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartProfileSurfaceBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    operation_specification_sha256: str
    body_id: str
    node_id: str
    result_id: str
    parameter_id: str
    value_id: str
    operation: PartProfileSurfaceOperation
    sources: tuple[PartProfileSurfaceSelection, ...]
    parameters: PartProfileSurfaceParameterSet
    schema_version: int = PART_PROFILE_SURFACE_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != PART_PROFILE_SURFACE_PLAN_SCHEMA_VERSION:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "body_id",
            "node_id",
            "result_id",
            "parameter_id",
            "value_id",
        ):
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
        if type(self.operation) is not PartProfileSurfaceOperation:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/operation")
        if type(self.sources) is not tuple or any(
            type(item) is not PartProfileSurfaceSelection for item in self.sources
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/sources")
        if len(self.sources) > MAX_PART_PROFILE_SURFACE_SOURCES:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/sources")
        _validate_selection_contract(self.operation, self.sources)
        if (
            type(self.parameters) is not PartProfileSurfaceParameterSet
            or self.parameters.operation is not self.operation
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/parameters")

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
                "engine_build_id": PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PART_PROFILE_SURFACE_RULE_ID,
                "rule_contract_sha256": PART_PROFILE_SURFACE_RULE_CONTRACT_SHA256,
            },
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
            "selection": {
                "body_id": self.body_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
                "parameter_id": self.parameter_id,
                "value_id": self.value_id,
            },
            "operation": self.operation.value,
            "sources": [item.to_mapping() for item in self.sources],
            "parameters": self.parameters.to_mapping(),
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        root = _exact_fields(
            value,
            {
                "schema_version",
                "authority",
                "backend",
                "rule",
                "source",
                "lowering",
                "selection",
                "operation",
                "sources",
                "parameters",
            },
            "/",
        )
        backend = _exact_fields(
            root["backend"], {"engine", "engine_version", "engine_build_id"}, "/backend"
        )
        rule = _exact_fields(root["rule"], {"rule_id", "rule_contract_sha256"}, "/rule")
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
        selection = _exact_fields(
            root["selection"],
            {"body_id", "node_id", "result_id", "parameter_id", "value_id"},
            "/selection",
        )
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": PART_PROFILE_SURFACE_RULE_ID,
                "rule_contract_sha256": PART_PROFILE_SURFACE_RULE_CONTRACT_SHA256,
            }
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        try:
            operation = PartProfileSurfaceOperation(root["operation"])
        except (TypeError, ValueError):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/operation")
        raw_sources = root["sources"]
        if type(raw_sources) is not list or len(raw_sources) > MAX_PART_PROFILE_SURFACE_SOURCES:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/sources")
        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=lowering["request_sha256"],
            adapter_contract_sha256=lowering["adapter_contract_sha256"],
            manifest_sha256=lowering["manifest_sha256"],
            operation_specification_sha256=lowering["operation_specification_sha256"],
            body_id=selection["body_id"],
            node_id=selection["node_id"],
            result_id=selection["result_id"],
            parameter_id=selection["parameter_id"],
            value_id=selection["value_id"],
            operation=operation,
            sources=tuple(
                PartProfileSurfaceSelection.from_mapping(item, f"/sources/{index}")
                for index, item in enumerate(raw_sources)
            ),
            parameters=PartProfileSurfaceParameterSet.from_value(operation, root["parameters"]),
        )


def decode_part_profile_surface_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartProfileSurfaceBackendPlan:
    result = PartProfileSurfaceBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE)
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256,
        _digest(expected_plan_sha256, "/expected_plan_sha256"),
    ):
        _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedPartProfileSurfaceObject:
    object: object
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/bindings/object")
        object.__setattr__(self, "node_id", _identifier(self.node_id, "/bindings/node_id"))
        object.__setattr__(self, "result_id", _identifier(self.result_id, "/bindings/result_id"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartProfileSurfaceExecutionBindings:
    document: object
    body_id: str
    sources: tuple[AuthenticatedPartProfileSurfaceObject, ...]
    expected_adapter_contract_sha256: str
    expected_manifest_sha256: str
    expected_operation_specification_sha256: str

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/bindings/document")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        if (
            type(self.sources) is not tuple
            or len(self.sources) > MAX_PART_PROFILE_SURFACE_SOURCES
            or any(type(item) is not AuthenticatedPartProfileSurfaceObject for item in self.sources)
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/bindings/sources")
        for name in (
            "expected_adapter_contract_sha256",
            "expected_manifest_sha256",
            "expected_operation_specification_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/bindings/{name}"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartProfileSurfaceConformanceReceipt:
    plan_sha256: str
    operation: PartProfileSurfaceOperation
    object_name: str
    source_shape_sha256s: tuple[str, ...]
    result_shape_sha256: str
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/receipt/plan"))
        if type(self.operation) is not PartProfileSurfaceOperation:
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/receipt/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/receipt/object"))
        if (
            type(self.source_shape_sha256s) is not tuple
            or len(self.source_shape_sha256s) > MAX_PART_PROFILE_SURFACE_SOURCES
            or any(
                type(item) is not str or _SHA256.fullmatch(item) is None
                for item in self.source_shape_sha256s
            )
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/receipt/sources")
        object.__setattr__(
            self,
            "result_shape_sha256",
            _digest(self.result_shape_sha256, "/receipt/result"),
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
) -> tuple[str, str, int, int, int, float, float, float]:
    try:
        if shape is None or shape.isNull() or not shape.isValid():
            _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, path)
        shape_type = str(shape.ShapeType)
        edges = len(shape.Edges)
        faces = len(shape.Faces)
        solids = len(shape.Solids)
        length = float(shape.Length)
        area = float(shape.Area)
        volume = float(shape.Volume)
        raw = shape.exportBrepToString().encode("utf-8")
    except PartProfileSurfaceRuleError:
        raise
    except (Exception, SystemExit, UnicodeError, OverflowError):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, path)
    if len(raw) > _MAX_SHAPE_BREP_BYTES or any(
        not math.isfinite(item) or item < 0.0 for item in (length, area, volume)
    ):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, path)
    return hashlib.sha256(raw).hexdigest(), shape_type, edges, faces, solids, length, area, volume


def _validate_source_shape(
    Part: object,
    shape: object,
    role: PartProfileSurfaceSourceRole,
    path: str,
) -> tuple[str, str, int, int, int, float, float, float]:
    try:
        shape_type = str(shape.ShapeType)
        edges = len(shape.Edges)
        faces = len(shape.Faces)
        solids = len(shape.Solids)
        length = float(shape.Length)
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, path)
    if role in {PartProfileSurfaceSourceRole.PROFILE, PartProfileSurfaceSourceRole.BOUNDARY}:
        try:
            wires = tuple(shape.Wires)
            planar_face = Part.Face(wires[0]) if len(wires) == 1 else None
            valid_profile = (
                shape_type == "Wire"
                and 1 <= edges <= 256
                and faces == 0
                and solids == 0
                and len(wires) == 1
                and wires[0].isClosed()
                and planar_face is not None
                and not planar_face.isNull()
                and planar_face.isValid()
                and float(planar_face.Area) > 1e-9
            )
        except (Exception, SystemExit, OverflowError):
            valid_profile = False
        if not valid_profile:
            _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, path)
    elif (
        role not in {PartProfileSurfaceSourceRole.SPINE, PartProfileSurfaceSourceRole.CURVE}
        or edges != 1
        or faces != 0
        or solids != 0
        or length <= 1e-9
    ):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, path)
    return _shape_signature(shape, path)


def _validate_bindings(
    Part: object,
    plan: PartProfileSurfaceBackendPlan,
    bindings: PartProfileSurfaceExecutionBindings,
) -> tuple[
    object, tuple[object, ...], tuple[tuple[str, str, int, int, int, float, float, float], ...]
]:
    if (
        bindings.body_id != plan.body_id
        or len(bindings.sources) != len(plan.sources)
        or not hmac.compare_digest(
            plan.adapter_contract_sha256,
            bindings.expected_adapter_contract_sha256,
        )
        or not hmac.compare_digest(plan.manifest_sha256, bindings.expected_manifest_sha256)
        or not hmac.compare_digest(
            plan.operation_specification_sha256,
            bindings.expected_operation_specification_sha256,
        )
    ):
        _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE, "/bindings")
    document = bindings.document
    try:
        if getattr(document, "UndoMode", 0) != 1 or bool(document.HasPendingTransaction):
            _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    except PartProfileSurfaceRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    objects: list[object] = []
    signatures = []
    for index, (selection, authenticated) in enumerate(
        zip(plan.sources, bindings.sources, strict=True)
    ):
        if (
            selection.node_id != authenticated.node_id
            or selection.result_id != authenticated.result_id
        ):
            _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE, f"/bindings/sources/{index}")
        item = authenticated.object
        try:
            if (
                item.Document is not document
                or item not in tuple(document.Objects)
                or not item.isValid()
                or tuple(item.State) != ("Up-to-date",)
            ):
                _fail(
                    PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED,
                    f"/bindings/sources/{index}",
                )
        except PartProfileSurfaceRuleError:
            raise
        except (Exception, SystemExit):
            _fail(
                PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED,
                f"/bindings/sources/{index}",
            )
        if any(item is existing for existing in objects):
            _fail(PartProfileSurfaceRuleErrorCode.INTEGRITY_FAILURE, "/bindings/sources")
        objects.append(item)
        signatures.append(
            _validate_source_shape(
                Part,
                item.Shape,
                selection.role,
                f"/bindings/sources/{index}/shape",
            )
        )
    return document, tuple(objects), tuple(signatures)


def _sources_by_role(
    plan: PartProfileSurfaceBackendPlan,
    sources: tuple[object, ...],
) -> dict[PartProfileSurfaceSourceRole, tuple[object, ...]]:
    return {
        role: tuple(
            source
            for selection, source in zip(plan.sources, sources, strict=True)
            if selection.role is role
        )
        for role in PartProfileSurfaceSourceRole
    }


def _configure_result(
    FreeCAD: object,
    result: object,
    plan: PartProfileSurfaceBackendPlan,
    sources: tuple[object, ...],
) -> None:
    grouped = _sources_by_role(plan, sources)
    value = plan.parameters.value
    operation = plan.operation
    if operation is PartProfileSurfaceOperation.EXTRUSION:
        result.Base = grouped[PartProfileSurfaceSourceRole.PROFILE][0]
        result.Dir = FreeCAD.Vector(*value["direction"])
        result.LengthFwd = value["forward_length_mm"]
        result.LengthRev = value["reverse_length_mm"]
        result.Solid = True
        result.Reversed = False
        result.Symmetric = False
        result.TaperAngle = 0.0
        result.TaperAngleRev = 0.0
        result.DirMode = "Custom"
        result.FaceMakerClass = "Part::FaceMakerBullseye"
        result.FaceMakerMode = "Bullseye"
    elif operation is PartProfileSurfaceOperation.REVOLUTION:
        result.Source = grouped[PartProfileSurfaceSourceRole.PROFILE][0]
        result.Base = FreeCAD.Vector(*value["axis_origin_mm"])
        result.Axis = FreeCAD.Vector(*value["axis_direction"])
        result.Angle = value["angle_degrees"]
        result.Solid = True
        result.Symmetric = False
        result.FaceMakerClass = "Part::FaceMakerBullseye"
    elif operation is PartProfileSurfaceOperation.LOFT:
        result.Sections = list(grouped[PartProfileSurfaceSourceRole.PROFILE])
        result.Solid = True
        result.Closed = False
        result.Ruled = value["ruled"]
        result.Linearize = False
    elif operation is PartProfileSurfaceOperation.SWEEP:
        result.Sections = list(grouped[PartProfileSurfaceSourceRole.PROFILE])
        result.Spine = (grouped[PartProfileSurfaceSourceRole.SPINE][0], ["Edge1"])
        result.Solid = True
        result.Frenet = value["frenet"]
        result.Transition = "Right corner"
        result.Linearize = False
    elif operation is PartProfileSurfaceOperation.RULED_SURFACE:
        curves = grouped[PartProfileSurfaceSourceRole.CURVE]
        result.Curve1 = (curves[0], ["Edge1"])
        result.Curve2 = (curves[1], ["Edge1"])
        result.Orientation = "Automatic"
    else:
        result.Sources = list(grouped[PartProfileSurfaceSourceRole.BOUNDARY])
        result.FaceMakerClass = "Part::FaceMakerBullseye"


def _vector_matches(actual: object, expected: object) -> bool:
    try:
        return all(
            math.isclose(float(actual[index]), float(expected[index]), rel_tol=0.0, abs_tol=1e-9)
            for index in range(3)
        )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        return False


def _link_sub_matches(actual: object, expected: object) -> bool:
    try:
        linked, subelements = actual
        return linked is expected and tuple(subelements) == ("Edge1",)
    except (Exception, SystemExit, TypeError, ValueError):
        return False


def _validate_native_readback(
    result: object,
    plan: PartProfileSurfaceBackendPlan,
    sources: tuple[object, ...],
) -> None:
    grouped = _sources_by_role(plan, sources)
    value = plan.parameters.value
    operation = plan.operation
    valid = False
    try:
        if operation is PartProfileSurfaceOperation.EXTRUSION:
            valid = (
                result.Base is grouped[PartProfileSurfaceSourceRole.PROFILE][0]
                and _vector_matches(result.Dir, value["direction"])
                and math.isclose(float(result.LengthFwd), value["forward_length_mm"], abs_tol=1e-9)
                and math.isclose(float(result.LengthRev), value["reverse_length_mm"], abs_tol=1e-9)
                and bool(result.Solid)
                and not bool(result.Reversed)
                and not bool(result.Symmetric)
                and math.isclose(float(result.TaperAngle), 0.0, abs_tol=1e-9)
                and math.isclose(float(result.TaperAngleRev), 0.0, abs_tol=1e-9)
                and str(result.DirMode) == "Custom"
                and str(result.FaceMakerClass) == "Part::FaceMakerBullseye"
                and str(result.FaceMakerMode) == "Bullseye"
            )
        elif operation is PartProfileSurfaceOperation.REVOLUTION:
            valid = (
                result.Source is grouped[PartProfileSurfaceSourceRole.PROFILE][0]
                and _vector_matches(result.Base, value["axis_origin_mm"])
                and _vector_matches(result.Axis, value["axis_direction"])
                and math.isclose(float(result.Angle), value["angle_degrees"], abs_tol=1e-9)
                and bool(result.Solid)
                and not bool(result.Symmetric)
                and str(result.FaceMakerClass) == "Part::FaceMakerBullseye"
            )
        elif operation is PartProfileSurfaceOperation.LOFT:
            valid = (
                tuple(result.Sections) == grouped[PartProfileSurfaceSourceRole.PROFILE]
                and bool(result.Solid)
                and not bool(result.Closed)
                and bool(result.Ruled) is value["ruled"]
                and not bool(result.Linearize)
            )
        elif operation is PartProfileSurfaceOperation.SWEEP:
            valid = (
                tuple(result.Sections) == grouped[PartProfileSurfaceSourceRole.PROFILE]
                and _link_sub_matches(
                    result.Spine,
                    grouped[PartProfileSurfaceSourceRole.SPINE][0],
                )
                and bool(result.Solid)
                and bool(result.Frenet) is value["frenet"]
                and str(result.Transition) == "Right corner"
                and not bool(result.Linearize)
            )
        elif operation is PartProfileSurfaceOperation.RULED_SURFACE:
            curves = grouped[PartProfileSurfaceSourceRole.CURVE]
            valid = (
                _link_sub_matches(result.Curve1, curves[0])
                and _link_sub_matches(result.Curve2, curves[1])
                and str(result.Orientation) == "Automatic"
            )
        else:
            valid = (
                tuple(result.Sources) == grouped[PartProfileSurfaceSourceRole.BOUNDARY]
                and str(result.FaceMakerClass) == "Part::FaceMakerBullseye"
            )
    except (Exception, SystemExit, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        _fail(PartProfileSurfaceRuleErrorCode.CONFORMANCE_FAILED, "/result/properties")
    try:
        out_list = tuple(result.OutList)
    except (Exception, SystemExit):
        _fail(PartProfileSurfaceRuleErrorCode.CONFORMANCE_FAILED, "/result/links")
    if len(out_list) != len(sources) or any(
        not any(source is linked for linked in out_list) for source in sources
    ):
        _fail(PartProfileSurfaceRuleErrorCode.CONFORMANCE_FAILED, "/result/links")


def _validate_result_shape(
    result: object,
    spec: NativeProfileSurfaceSpec,
) -> tuple[str, str, int, int, int, float, float, float]:
    try:
        if (
            result.TypeId != spec.type_id
            or not result.isValid()
            or tuple(result.State) != ("Up-to-date",)
        ):
            _fail(PartProfileSurfaceRuleErrorCode.CONFORMANCE_FAILED, "/result/state")
        signature = _shape_signature(result.Shape, "/result/shape")
    except PartProfileSurfaceRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartProfileSurfaceRuleErrorCode.CONFORMANCE_FAILED, "/result")
    _, shape_type, edges, faces, solids, length, area, volume = signature
    if spec.result_kind is PartProfileSurfaceResultKind.SOLID:
        valid = shape_type == "Solid" and solids == 1 and faces >= 1 and volume > 1e-9
    else:
        valid = shape_type == "Face" and solids == 0 and faces == 1 and area > 1e-9
    if not valid or edges < 1 or length <= 1e-9:
        _fail(PartProfileSurfaceRuleErrorCode.CONFORMANCE_FAILED, "/result/shape")
    return signature


def apply_part_profile_surface_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartProfileSurfaceExecutionBindings,
) -> PartProfileSurfaceConformanceReceipt:
    """Execute one exact plan at the explicit trusted-host authority seam."""

    if type(bindings) is not PartProfileSurfaceExecutionBindings:
        _fail(PartProfileSurfaceRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415
        import Part  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_part_profile_surface_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    document, sources, source_signatures = _validate_bindings(Part, plan, bindings)
    spec = PART_PROFILE_SURFACE_NATIVE_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_objects = tuple(document.Objects)
        before_visibility = tuple(
            (item, bool(item.Visibility)) for item in before_objects if hasattr(item, "Visibility")
        )
        before_source_digests = tuple(item[0] for item in source_signatures)
    except PartProfileSurfaceRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartProfileSurfaceRuleErrorCode.PRECONDITION_FAILED, "/document")

    holder: list[tuple[object, tuple[str, str, int, int, int, float, float, float]]] = []

    def snapshot() -> object:
        return before_objects, before_visibility, before_source_digests

    def create() -> object:
        result = document.addObject(spec.type_id, object_name)
        _configure_result(FreeCAD, result, plan, sources)
        document.recompute()
        _validate_native_readback(result, plan, sources)
        signature = _validate_result_shape(result, spec)
        holder.append((result, signature))
        return result

    def rollback_matches(before: object) -> bool:
        expected_objects, expected_visibility, expected_source_digests = before
        try:
            current = tuple(document.Objects)
            return (
                len(current) == len(expected_objects)
                and all(
                    left is right for left, right in zip(current, expected_objects, strict=True)
                )
                and all(bool(item.Visibility) is visible for item, visible in expected_visibility)
                and tuple(_shape_signature(item.Shape, "/rollback/source")[0] for item in sources)
                == expected_source_digests
                and document.getObject(object_name) is None
            )
        except BaseException:
            return False

    try:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD reviewed Part profile surface",
            snapshot=snapshot,
            apply=create,
            rollback_matches=rollback_matches,
        )
    except NativeTransactionError:
        _fail(PartProfileSurfaceRuleErrorCode.TRANSACTION_FAILED, "/transaction")
    if len(holder) != 1:
        _fail(PartProfileSurfaceRuleErrorCode.TRANSACTION_FAILED, "/transaction/result")
    result, result_signature = holder[0]
    return PartProfileSurfaceConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=result.Name,
        source_shape_sha256s=tuple(item[0] for item in source_signatures),
        result_shape_sha256=result_signature[0],
    )


__all__ = [
    "MAX_PART_PROFILE_SURFACE_PLAN_BYTES",
    "MAX_PART_PROFILE_SURFACE_SOURCES",
    "PART_PROFILE_SURFACE_FREECAD_ENGINE_BUILD_ID",
    "PART_PROFILE_SURFACE_NATIVE_SPECS",
    "PART_PROFILE_SURFACE_PLAN_MEDIA_TYPE",
    "PART_PROFILE_SURFACE_PLAN_SCHEMA_VERSION",
    "PART_PROFILE_SURFACE_RULE_CONTRACT_SHA256",
    "PART_PROFILE_SURFACE_RULE_ID",
    "AuthenticatedPartProfileSurfaceObject",
    "NativeProfileSurfaceSpec",
    "PartProfileSurfaceBackendPlan",
    "PartProfileSurfaceConformanceReceipt",
    "PartProfileSurfaceExecutionBindings",
    "PartProfileSurfaceOperation",
    "PartProfileSurfaceParameterSet",
    "PartProfileSurfaceResultKind",
    "PartProfileSurfaceRuleError",
    "PartProfileSurfaceRuleErrorCode",
    "PartProfileSurfaceSelection",
    "PartProfileSurfaceSourceRole",
    "ProfileSurfaceSourceRequirement",
    "apply_part_profile_surface_plan",
    "decode_part_profile_surface_backend_plan",
]
