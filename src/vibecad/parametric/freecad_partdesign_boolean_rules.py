"""Trusted native rule for the reviewed ``PartDesign::Boolean`` batch.

Plans carry only semantic operation and exact graph-result identities.  The
native TypeId, property names, and enumeration labels live exclusively in this
module.  Importing it never imports FreeCAD; native mutation is available only
through the explicit ``apply_partdesign_boolean_plan`` host action.
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

PARTDESIGN_BOOLEAN_PLAN_SCHEMA_VERSION: Final = 1
PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-partdesign-boolean-plan+json"
)
MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES: Final = 32 * 1024
PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID: Final = "PartDesign::Boolean"

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-boolean-plan.v1\0"
_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-partdesign-boolean-rule.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-partdesign-boolean-receipt.v1\0"


class PartDesignBooleanOperation(StrEnum):
    FUSE = "fuse"
    CUT = "cut"
    COMMON = "common"


_NATIVE_ENUM: Final = {
    PartDesignBooleanOperation.FUSE: "Fuse",
    PartDesignBooleanOperation.CUT: "Cut",
    PartDesignBooleanOperation.COMMON: "Common",
}

PARTDESIGN_BOOLEAN_RULE_ID: Final = "freecad.partdesign.boolean.v1"
_NATIVE_CONTRACT = {
    "engine": {
        "name": "FreeCAD",
        "version": "1.1.0",
        "build_id": PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID,
    },
    "type_id": PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID,
    "properties": {
        "base": "BaseFeature",
        "tools": "Group",
        "operation": "Type",
        "refine": "Refine",
    },
    "operations": [
        {"semantic_id": operation.value, "native_enum": native}
        for operation, native in _NATIVE_ENUM.items()
    ],
    "constraints": {
        "base": "exact-target-body-tip",
        "tools": "exactly-one-external-body-tip",
        "result": "valid-single-solid",
        "transaction": "rollback-exact-document-and-body-state",
    },
}
PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN
    + json.dumps(
        _NATIVE_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
).hexdigest()


class PartDesignBooleanRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"


class PartDesignBooleanRuleError(ValueError):
    def __init__(self, code: PartDesignBooleanRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartDesignBooleanRuleErrorCode:
            raise TypeError("code must be a PartDesignBooleanRuleErrorCode")
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isascii()
            or not path.isprintable()
            or len(path) > 192
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"PartDesign Boolean rule error ({code.value}) at {path}")


def _fail(code: PartDesignBooleanRuleErrorCode, path: str) -> None:
    raise PartDesignBooleanRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/")
    if not raw or len(raw) > MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES:
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/")
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
    if type(raw) is not bytes or not raw or len(raw) > MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES:
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/")
    except (UnicodeError, ValueError, RecursionError):
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/")
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/")
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class BooleanOperandSelection:
    body_id: str
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        for name in ("body_id", "node_id", "result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/selection/{name}"))

    def to_mapping(self) -> dict[str, str]:
        return {"body_id": self.body_id, "node_id": self.node_id, "result_id": self.result_id}

    @classmethod
    def from_mapping(cls, value: object, path: str) -> BooleanOperandSelection:
        item = _exact_fields(value, {"body_id", "node_id", "result_id"}, path)
        return cls(body_id=item["body_id"], node_id=item["node_id"], result_id=item["result_id"])


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignBooleanBackendPlan:
    """Canonical authority-free binding of one reviewed Boolean intent."""

    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation: PartDesignBooleanOperation
    base: BooleanOperandSelection
    tools: tuple[BooleanOperandSelection, ...]
    schema_version: int = PARTDESIGN_BOOLEAN_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in ("source_artifact_id", "source_graph_id", "body_id", "node_id", "result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.operation) is not PartDesignBooleanOperation:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/operation")
        if type(self.base) is not BooleanOperandSelection:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/selection/base")
        if (
            type(self.tools) is not tuple
            or len(self.tools) != 1
            or any(type(item) is not BooleanOperandSelection for item in self.tools)
        ):
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/selection/tools")
        operands = (self.base, *self.tools)
        if (
            self.base.body_id != self.body_id
            or any(item.body_id == self.body_id for item in self.tools)
            or len({item.body_id for item in operands}) != len(operands)
            or len({item.node_id for item in operands}) != len(operands)
            or len({item.result_id for item in operands}) != len(operands)
            or self.node_id in {item.node_id for item in operands}
            or self.result_id in {item.result_id for item in operands}
        ):
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/selection")

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
                "engine_build_id": PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID,
            },
            "rule": {
                "rule_id": PARTDESIGN_BOOLEAN_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
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
                "body_id": self.body_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
                "base": self.base.to_mapping(),
                "tools": [item.to_mapping() for item in self.tools],
            },
            "operation": {
                "id": self.operation.value,
                "refine": True,
                "ordered_tools": True,
                "single_solid": True,
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_mapping())

    @property
    def plan_sha256(self) -> str:
        return hashlib.sha256(_PLAN_DIGEST_DOMAIN + self.canonical_bytes).hexdigest()

    @classmethod
    def from_mapping(cls, value: object) -> PartDesignBooleanBackendPlan:
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
        rule = _exact_fields(root["rule"], {"rule_id", "rule_contract_sha256"}, "/rule")
        source = _exact_fields(
            root["source"], {"artifact_id", "graph_id", "graph_sha256", "content_sha256"}, "/source"
        )
        binding = _exact_fields(
            root["binding"], {"lowering_request_sha256", "adapter_contract_sha256"}, "/binding"
        )
        selection = _exact_fields(
            root["selection"], {"body_id", "node_id", "result_id", "base", "tools"}, "/selection"
        )
        operation = _exact_fields(
            root["operation"], {"id", "refine", "ordered_tools", "single_solid"}, "/operation"
        )
        try:
            operation_id = PartDesignBooleanOperation(operation["id"])
        except (TypeError, ValueError):
            _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/operation/id")
        if (
            root["authority"] != "none"
            or backend
            != {
                "engine": "FreeCAD",
                "engine_version": "1.1.0",
                "engine_build_id": PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID,
            }
            or rule
            != {
                "rule_id": PARTDESIGN_BOOLEAN_RULE_ID,
                "rule_contract_sha256": PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256,
            }
            or operation
            != {
                "id": operation_id.value,
                "refine": True,
                "ordered_tools": True,
                "single_solid": True,
            }
        ):
            _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/contract")
        tools = selection["tools"]
        if type(tools) is not list:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/selection/tools")
        return cls(
            schema_version=root["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=binding["lowering_request_sha256"],
            adapter_contract_sha256=binding["adapter_contract_sha256"],
            body_id=selection["body_id"],
            node_id=selection["node_id"],
            result_id=selection["result_id"],
            operation=operation_id,
            base=BooleanOperandSelection.from_mapping(selection["base"], "/selection/base"),
            tools=tuple(
                BooleanOperandSelection.from_mapping(item, "/selection/tools/item")
                for item in tools
            ),
        )


def decode_partdesign_boolean_backend_plan(
    raw: object,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartDesignBooleanBackendPlan:
    if expected_content_sha256 is not None:
        expected_content_sha256 = _digest(expected_content_sha256, "/expected_content_sha256")
    if expected_plan_sha256 is not None:
        expected_plan_sha256 = _digest(expected_plan_sha256, "/expected_plan_sha256")
    result = PartDesignBooleanBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/")
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_content_sha256
    ):
        _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256, expected_plan_sha256
    ):
        _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticatedBooleanOperand:
    """Host-authenticated semantic result and its exact owning Body."""

    object: object
    body: object
    body_id: str
    node_id: str
    result_id: str

    def __post_init__(self) -> None:
        if self.object is None or self.body is None:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/bindings/operand")
        for name in ("body_id", "node_id", "result_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/bindings/{name}"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignBooleanExecutionBindings:
    document: object
    target_body: object
    target_body_id: str
    base: AuthenticatedBooleanOperand
    tools: tuple[AuthenticatedBooleanOperand, ...]

    def __post_init__(self) -> None:
        if self.document is None or self.target_body is None:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/bindings")
        object.__setattr__(
            self,
            "target_body_id",
            _identifier(self.target_body_id, "/bindings/target_body_id"),
        )
        if type(self.base) is not AuthenticatedBooleanOperand:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/bindings/base")
        if (
            type(self.tools) is not tuple
            or len(self.tools) != 1
            or any(type(item) is not AuthenticatedBooleanOperand for item in self.tools)
        ):
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/bindings/tools")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartDesignBooleanConformanceReceipt:
    plan_sha256: str
    operation: PartDesignBooleanOperation
    object_name: str
    base_volume_mm3: float
    tool_volumes_mm3: tuple[float, ...]
    result_volume_mm3: float
    receipt_id: str = field(init=False)
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if type(self.operation) is not PartDesignBooleanOperation:
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/operation")
        object.__setattr__(self, "object_name", _identifier(self.object_name, "/object_name"))
        if (
            type(self.tool_volumes_mm3) is not tuple
            or not self.tool_volumes_mm3
            or any(type(item) not in {int, float} for item in self.tool_volumes_mm3)
        ):
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/receipt/tools")
        try:
            base = float(self.base_volume_mm3)
            tools = tuple(float(item) for item in self.tool_volumes_mm3)
            result = float(self.result_volume_mm3)
        except (TypeError, ValueError, OverflowError):
            _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/receipt/volumes")
        if not all(
            math.isfinite(item) and item > 0.0 for item in (base, *tools, result)
        ) or not _volume_conforms(self.operation, base, tools, result):
            _fail(PartDesignBooleanRuleErrorCode.CONFORMANCE_FAILED, "/receipt/volumes")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "base_volume_mm3": base,
            "tool_volumes_mm3": list(tools),
            "result_volume_mm3": result,
        }
        digest = hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        object.__setattr__(self, "base_volume_mm3", base)
        object.__setattr__(self, "tool_volumes_mm3", tools)
        object.__setattr__(self, "result_volume_mm3", result)
        object.__setattr__(self, "receipt_sha256", digest)
        object.__setattr__(self, "receipt_id", f"partdesign_boolean_{digest[:32]}")

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_volume(
    shape: object,
    path: str,
    *,
    code: PartDesignBooleanRuleErrorCode = PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED,
) -> float:
    try:
        if shape is None or shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            _fail(code, path)
        volume = float(shape.Volume)
    except PartDesignBooleanRuleError:
        raise
    except Exception:
        _fail(code, path)
    if not math.isfinite(volume) or volume <= 0.0:
        _fail(code, path)
    return volume


def _selection_matches(
    selection: BooleanOperandSelection,
    authenticated: AuthenticatedBooleanOperand,
) -> bool:
    return (
        selection.body_id == authenticated.body_id
        and selection.node_id == authenticated.node_id
        and selection.result_id == authenticated.result_id
    )


def _validate_bindings(
    plan: PartDesignBooleanBackendPlan,
    bindings: PartDesignBooleanExecutionBindings,
) -> tuple[float, tuple[float, ...]]:
    if (
        bindings.target_body_id != plan.body_id
        or not _selection_matches(plan.base, bindings.base)
        or len(bindings.tools) != len(plan.tools)
        or any(
            not _selection_matches(expected, actual)
            for expected, actual in zip(plan.tools, bindings.tools, strict=True)
        )
    ):
        _fail(PartDesignBooleanRuleErrorCode.INTEGRITY_FAILURE, "/bindings/identity")
    document, target_body = bindings.document, bindings.target_body
    base = bindings.base
    tools = bindings.tools
    try:
        if (
            getattr(document, "UndoMode", 0) != 1
            or bool(document.HasPendingTransaction)
            or target_body.Document is not document
            or target_body.TypeId != "PartDesign::Body"
            or base.body is not target_body
            or base.body_id != bindings.target_body_id
            or base.object.Document is not document
            or base.object not in tuple(target_body.Group)
            or target_body.Tip is not base.object
        ):
            _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/bindings/base")
        tool_bodies = tuple(item.body for item in tools)
        if (
            len({id(item) for item in tool_bodies}) != len(tool_bodies)
            or target_body in tool_bodies
        ):
            _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/bindings/tools")
        for index, tool in enumerate(tools):
            if (
                tool.body.Document is not document
                or tool.body.TypeId != "PartDesign::Body"
                or tool.object.Document is not document
                or tool.object not in tuple(tool.body.Group)
                or tool.body.Tip is not tool.object
            ):
                _fail(
                    PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED,
                    f"/bindings/tools/{index}",
                )
    except PartDesignBooleanRuleError:
        raise
    except Exception:
        _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/bindings")
    base_volume = _shape_volume(base.object.Shape, "/bindings/base/shape")
    tool_volumes = tuple(
        _shape_volume(item.object.Shape, f"/bindings/tools/{index}/shape")
        for index, item in enumerate(tools)
    )
    return base_volume, tool_volumes


def _volume_conforms(
    operation: PartDesignBooleanOperation,
    base: float,
    tools: tuple[float, ...],
    result: float,
) -> bool:
    epsilon = max(1e-8, max((base, *tools)) * 1e-10)
    if operation is PartDesignBooleanOperation.FUSE:
        return result > base + epsilon and result <= base + sum(tools) + epsilon
    if operation is PartDesignBooleanOperation.CUT:
        return epsilon < result < base - epsilon
    return epsilon < result < base - epsilon and result <= min(tools) + epsilon


def _document_snapshot(
    document: object,
    bodies: tuple[object, ...],
) -> tuple[
    tuple[object, ...],
    tuple[tuple[object, tuple[object, ...], object | None], ...],
    tuple[tuple[object, bool], ...],
]:
    try:
        objects = tuple(document.Objects)
        body_state = tuple((body, tuple(body.Group), body.Tip) for body in bodies)
        visibility = tuple(
            (item, bool(item.Visibility)) for item in objects if hasattr(item, "Visibility")
        )
    except Exception:
        _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/document/snapshot")
    return objects, body_state, visibility


def _snapshot_matches(
    document: object,
    snapshot: tuple[
        tuple[object, ...],
        tuple[tuple[object, tuple[object, ...], object | None], ...],
        tuple[tuple[object, bool], ...],
    ],
) -> bool:
    objects, bodies, visibility = snapshot
    try:
        actual_objects = tuple(document.Objects)
        return (
            len(actual_objects) == len(objects)
            and all(
                actual is expected for actual, expected in zip(actual_objects, objects, strict=True)
            )
            and all(tuple(body.Group) == group and body.Tip is tip for body, group, tip in bodies)
            and all(bool(item.Visibility) is visible for item, visible in visibility)
            and not bool(document.HasPendingTransaction)
        )
    except Exception:
        return False


def apply_partdesign_boolean_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartDesignBooleanExecutionBindings,
) -> PartDesignBooleanConformanceReceipt:
    """Execute one exact reviewed plan in a real FreeCAD transaction."""

    if type(bindings) is not PartDesignBooleanExecutionBindings:
        _fail(PartDesignBooleanRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: F401, PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_partdesign_boolean_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    base_volume, tool_volumes = _validate_bindings(plan, bindings)
    document, target_body = bindings.document, bindings.target_body
    base = bindings.base.object
    tool_bodies = tuple(item.body for item in bindings.tools)
    object_name = f"Boolean{_NATIVE_ENUM[plan.operation]}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_group = tuple(target_body.Group)
        snapshot = _document_snapshot(document, (target_body, *tool_bodies))
    except PartDesignBooleanRuleError:
        raise
    except Exception:
        _fail(PartDesignBooleanRuleErrorCode.PRECONDITION_FAILED, "/document")

    transaction_open = False
    try:
        document.openTransaction("VibeCAD trusted PartDesign Boolean")
        transaction_open = True
        feature = target_body.newObject(PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID, object_name)
        feature.Type = _NATIVE_ENUM[plan.operation]
        feature.Group = list(tool_bodies)
        feature.Refine = True
        document.recompute()
        result_volume = _shape_volume(
            feature.Shape,
            "/result/shape",
            code=PartDesignBooleanRuleErrorCode.CONFORMANCE_FAILED,
        )
        if (
            feature.TypeId != PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID
            or not feature.isValid()
            or tuple(feature.State) != ("Up-to-date",)
            or tuple(feature.getEnumerationsOfProperty("Type")) != tuple(_NATIVE_ENUM.values())
            or feature.getTypeIdOfProperty("BaseFeature") != "App::PropertyLink"
            or feature.getTypeIdOfProperty("Group") != "App::PropertyLinkList"
            or feature.getTypeIdOfProperty("Type") != "App::PropertyEnumeration"
            or feature.getTypeIdOfProperty("Refine") != "App::PropertyBool"
            or target_body.Tip is not feature
            or tuple(target_body.Group) != (*before_group, feature)
            or feature.BaseFeature is not base
            or str(feature.Type) != _NATIVE_ENUM[plan.operation]
            or tuple(feature.Group) != tool_bodies
            or not bool(feature.Refine)
            or not _volume_conforms(plan.operation, base_volume, tool_volumes, result_volume)
        ):
            _fail(PartDesignBooleanRuleErrorCode.CONFORMANCE_FAILED, "/result")
        document.commitTransaction()
        transaction_open = False
    except BaseException as error:
        if transaction_open:
            try:
                document.abortTransaction()
                document.recompute()
            except BaseException:
                _fail(PartDesignBooleanRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        if not _snapshot_matches(document, snapshot):
            _fail(PartDesignBooleanRuleErrorCode.TRANSACTION_FAILED, "/transaction/rollback")
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, PartDesignBooleanRuleError):
            raise error
        _fail(PartDesignBooleanRuleErrorCode.TRANSACTION_FAILED, "/transaction/apply")

    return PartDesignBooleanConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        base_volume_mm3=base_volume,
        tool_volumes_mm3=tool_volumes,
        result_volume_mm3=result_volume,
    )


__all__ = [
    "MAX_PARTDESIGN_BOOLEAN_PLAN_BYTES",
    "PARTDESIGN_BOOLEAN_FREECAD_ENGINE_BUILD_ID",
    "PARTDESIGN_BOOLEAN_NATIVE_TYPE_ID",
    "PARTDESIGN_BOOLEAN_PLAN_MEDIA_TYPE",
    "PARTDESIGN_BOOLEAN_PLAN_SCHEMA_VERSION",
    "PARTDESIGN_BOOLEAN_RULE_CONTRACT_SHA256",
    "PARTDESIGN_BOOLEAN_RULE_ID",
    "AuthenticatedBooleanOperand",
    "BooleanOperandSelection",
    "PartDesignBooleanBackendPlan",
    "PartDesignBooleanConformanceReceipt",
    "PartDesignBooleanExecutionBindings",
    "PartDesignBooleanOperation",
    "PartDesignBooleanRuleError",
    "PartDesignBooleanRuleErrorCode",
    "apply_partdesign_boolean_plan",
    "decode_partdesign_boolean_backend_plan",
]
