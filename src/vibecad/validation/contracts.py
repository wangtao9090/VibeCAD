"""Strict value contracts and process-local validation capabilities."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import weakref
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Self

from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    SCHEMA_VERSION,
    is_canonical_json_pointer,
    join_json_pointer,
)
from vibecad.workflow.state import CriterionVerdict, VerificationReport

_OBSERVATION_DOMAIN = b"vibecad-observation-snapshot-v1\0"
_REVISION_RE = re.compile(r"^revision_[0-9a-f]{32}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_IDENTIFIER_BYTES = 256
_MAX_ERROR_PATH_LENGTH = 256
_MAX_SHAPES = 128
_MAX_ARTIFACTS = 128
_MAX_OBSERVATION_FACTS = 2048
_MAX_SNAPSHOT_BYTES = 64 * 1024
_MAX_ACTIVE_COMPILED = 256
_MAX_ACTIVE_RECEIPTS = 256
_FORMATS = frozenset({"fcstd", "step"})
_RECEIPT_REPORT_DOMAIN = b"vibecad-verification-receipt-report-v1\0"


class ValidationErrorCode(StrEnum):
    """Stable machine-readable validation rejection reasons."""

    MISSING_FIELD = "missing_field"
    UNKNOWN_FIELD = "unknown_field"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"
    BUDGET_EXCEEDED = "budget_exceeded"
    EMPTY_SPEC = "empty_spec"
    DUPLICATE_CRITERION = "duplicate_criterion"
    DUPLICATE_TARGET = "duplicate_target"
    UNSUPPORTED_CHECK = "unsupported_check"
    AMBIGUOUS_TARGET = "ambiguous_target"
    INVALID_UNIT = "invalid_unit"
    INVALID_TOLERANCE = "invalid_tolerance"
    BINDING_MISMATCH = "binding_mismatch"
    FORGED_CAPABILITY = "forged_capability"
    REPLAYED_RECEIPT = "replayed_receipt"


_ERROR_MESSAGES = {
    ValidationErrorCode.MISSING_FIELD: "A required field is missing.",
    ValidationErrorCode.UNKNOWN_FIELD: "The field is not supported.",
    ValidationErrorCode.UNSUPPORTED_VERSION: "The schema version is not supported.",
    ValidationErrorCode.INVALID_TYPE: "The value has an invalid type.",
    ValidationErrorCode.INVALID_VALUE: "The value is invalid.",
    ValidationErrorCode.BUDGET_EXCEEDED: "The validation input exceeds its resource budget.",
    ValidationErrorCode.EMPTY_SPEC: (
        "The acceptance specification has no supported machine criterion."
    ),
    ValidationErrorCode.DUPLICATE_CRITERION: "Criterion identifiers must be unique.",
    ValidationErrorCode.DUPLICATE_TARGET: "Observation targets must be unique.",
    ValidationErrorCode.UNSUPPORTED_CHECK: "The required acceptance check is unsupported.",
    ValidationErrorCode.AMBIGUOUS_TARGET: "The acceptance target must be explicit.",
    ValidationErrorCode.INVALID_UNIT: "The acceptance unit is invalid.",
    ValidationErrorCode.INVALID_TOLERANCE: "The acceptance tolerance is invalid.",
    ValidationErrorCode.BINDING_MISMATCH: "The validation binding does not match.",
    ValidationErrorCode.FORGED_CAPABILITY: "The validation capability is not authentic.",
    ValidationErrorCode.REPLAYED_RECEIPT: "The verification receipt was already consumed.",
}


class ValidationError(ValueError):
    """A bounded error envelope that never reflects rejected values."""

    def __init__(self, code: ValidationErrorCode, path: str = "") -> None:
        if type(code) is not ValidationErrorCode:
            raise TypeError("code must be a ValidationErrorCode")
        if (
            type(path) is not str
            or len(path) > _MAX_ERROR_PATH_LENGTH
            or not is_canonical_json_pointer(path)
        ):
            raise ValueError("path must be a bounded canonical JSON Pointer")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.path = path
        self.message = _ERROR_MESSAGES[code]
        super().__init__(self.message)

    def to_mapping(self) -> dict[str, int | str]:
        """Return a fresh strict JSON-compatible error envelope."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "path": self.path,
            "message": self.message,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        """Parse only the canonical redacted error representation."""

        data = _fields(
            value,
            allowed={"schema_version", "code", "path", "message"},
            required={"schema_version", "code", "path", "message"},
        )
        _validate_schema(data["schema_version"], "/schema_version")
        code_value = data["code"]
        if type(code_value) is not str:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/code")
        code_by_value = {item.value: item for item in ValidationErrorCode}
        code = code_by_value.get(code_value)
        if code is None:
            _raise_validation(ValidationErrorCode.INVALID_VALUE, "/code")
        path = data["path"]
        if (
            type(path) is not str
            or len(path) > _MAX_ERROR_PATH_LENGTH
            or not is_canonical_json_pointer(path)
        ):
            _raise_validation(ValidationErrorCode.INVALID_VALUE, "/path")
        message = data["message"]
        if type(message) is not str:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/message")
        if message != _ERROR_MESSAGES[code]:
            _raise_validation(ValidationErrorCode.INVALID_VALUE, "/message")
        return cls(code, path)


def _raise_validation(code: ValidationErrorCode, path: str = "") -> None:
    raise ValidationError(code, path)


def _safe_field_path(parent: str, name: str) -> str:
    if (
        len(name) > 128
        or not name.isprintable()
        or len(name.splitlines()) != 1
        or len(parent) + len(name) + 1 > _MAX_ERROR_PATH_LENGTH
    ):
        name = "__unknown__"
    return join_json_pointer(parent, name)


def _fields(
    value: object,
    *,
    allowed: set[str],
    required: set[str],
    path: str = "",
) -> dict[str, Any]:
    if type(value) is not dict:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    keys = tuple(value)
    if not all(type(key) is str for key in keys):
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    unknown = sorted(key for key in keys if key not in allowed)
    if unknown:
        _raise_validation(
            ValidationErrorCode.UNKNOWN_FIELD,
            _safe_field_path(path, unknown[0]),
        )
    missing = sorted(required - set(keys))
    if missing:
        _raise_validation(
            ValidationErrorCode.MISSING_FIELD,
            join_json_pointer(path, missing[0]),
        )
    return dict(value)


def _validate_schema(value: object, path: str) -> int:
    if type(value) is not int:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    if value != SCHEMA_VERSION:
        _raise_validation(ValidationErrorCode.UNSUPPORTED_VERSION, path)
    return value


def _validate_bounded_text(value: object, path: str) -> str:
    if type(value) is not str:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    if not value.strip() or not value.isprintable() or len(value.splitlines()) != 1:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    encoded = None
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        pass
    if encoded is None:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    if len(encoded) > _MAX_IDENTIFIER_BYTES:
        _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED, path)
    return value


def _finite_number(
    value: object,
    path: str,
    *,
    nonnegative: bool = False,
    error_code: ValidationErrorCode = ValidationErrorCode.INVALID_VALUE,
) -> int | float:
    if type(value) not in {int, float}:
        _raise_validation(
            ValidationErrorCode.INVALID_TYPE
            if error_code is ValidationErrorCode.INVALID_VALUE
            else error_code,
            path,
        )
    if type(value) is int and abs(value) > MAX_SAFE_JSON_INTEGER:
        _raise_validation(error_code, path)
    if type(value) is float and not math.isfinite(value):
        _raise_validation(error_code, path)
    if nonnegative and value < 0:
        _raise_validation(error_code, path)
    return value


def _optional_number(
    value: object,
    path: str,
    *,
    nonnegative: bool,
) -> int | float | None:
    if value is None:
        return None
    return _finite_number(value, path, nonnegative=nonnegative)


def _vector(
    value: object,
    path: str,
    *,
    length: int,
    nonnegative: bool,
) -> tuple[int | float, ...] | None:
    if value is None:
        return None
    if type(value) is not tuple:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    if len(value) != length:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    result: list[int | float] = []
    for index, item in enumerate(value):
        result.append(
            _finite_number(
                item,
                join_json_pointer(path, str(index)),
                nonnegative=nonnegative,
            )
        )
    return tuple(result)


def _json_vector(value: object, path: str) -> tuple[object, ...] | None:
    if value is None:
        return None
    if type(value) is not list:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    return tuple(value)


def _validate_revision(value: object, path: str = "/candidate_revision") -> str:
    if type(value) is not str or _REVISION_RE.fullmatch(value) is None:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    return value


def _validate_digest(value: object, path: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    return value


def _canonical_json_bytes(value: object, path: str = "") -> bytes:
    rendered = None
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError):
        pass
    if rendered is None:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    return rendered.encode("ascii")


@dataclass(frozen=True, slots=True, kw_only=True)
class ShapeObservation:
    """Trusted scalar and vector facts for one shape target."""

    target: str
    volume_mm3: int | float | None = None
    area_mm2: int | float | None = None
    bbox_mm: tuple[int | float, ...] | None = None
    center_of_mass_mm: tuple[int | float, ...] | None = None
    valid_shape: bool | None = None
    solid_count: int | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schema_version", _validate_schema(self.schema_version, "/schema_version")
        )
        object.__setattr__(self, "target", _validate_bounded_text(self.target, "/target"))
        object.__setattr__(
            self,
            "volume_mm3",
            _optional_number(self.volume_mm3, "/volume_mm3", nonnegative=True),
        )
        object.__setattr__(
            self,
            "area_mm2",
            _optional_number(self.area_mm2, "/area_mm2", nonnegative=True),
        )
        object.__setattr__(
            self,
            "bbox_mm",
            _vector(self.bbox_mm, "/bbox_mm", length=3, nonnegative=True),
        )
        object.__setattr__(
            self,
            "center_of_mass_mm",
            _vector(
                self.center_of_mass_mm,
                "/center_of_mass_mm",
                length=3,
                nonnegative=False,
            ),
        )
        if self.valid_shape is not None and type(self.valid_shape) is not bool:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/valid_shape")
        if self.solid_count is not None:
            if type(self.solid_count) is not int:
                _raise_validation(ValidationErrorCode.INVALID_TYPE, "/solid_count")
            if self.solid_count < 0 or self.solid_count > MAX_SAFE_JSON_INTEGER:
                _raise_validation(ValidationErrorCode.INVALID_VALUE, "/solid_count")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "volume_mm3": self.volume_mm3,
            "area_mm2": self.area_mm2,
            "bbox_mm": list(self.bbox_mm) if self.bbox_mm is not None else None,
            "center_of_mass_mm": (
                list(self.center_of_mass_mm) if self.center_of_mass_mm is not None else None
            ),
            "valid_shape": self.valid_shape,
            "solid_count": self.solid_count,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        required = {
            "schema_version",
            "target",
            "volume_mm3",
            "area_mm2",
            "bbox_mm",
            "center_of_mass_mm",
            "valid_shape",
            "solid_count",
        }
        data = _fields(value, allowed=required, required=required)
        return cls(
            schema_version=data["schema_version"],
            target=data["target"],
            volume_mm3=data["volume_mm3"],
            area_mm2=data["area_mm2"],
            bbox_mm=_json_vector(data["bbox_mm"], "/bbox_mm"),
            center_of_mass_mm=_json_vector(data["center_of_mass_mm"], "/center_of_mass_mm"),
            valid_shape=data["valid_shape"],
            solid_count=data["solid_count"],
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactObservation:
    """Trusted existence, size-state, and format facts for one artifact target."""

    target: str
    exists: bool
    non_empty: bool | None = None
    format: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schema_version", _validate_schema(self.schema_version, "/schema_version")
        )
        object.__setattr__(self, "target", _validate_bounded_text(self.target, "/target"))
        if type(self.exists) is not bool:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/exists")
        if self.non_empty is not None and type(self.non_empty) is not bool:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/non_empty")
        if not self.exists:
            if self.non_empty is True:
                _raise_validation(ValidationErrorCode.INVALID_VALUE, "/non_empty")
            if self.format is not None:
                _raise_validation(ValidationErrorCode.INVALID_VALUE, "/format")
            object.__setattr__(self, "non_empty", False)
        if self.format is not None:
            if type(self.format) is not str:
                _raise_validation(ValidationErrorCode.INVALID_TYPE, "/format")
            if self.format not in _FORMATS:
                _raise_validation(ValidationErrorCode.INVALID_VALUE, "/format")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target": self.target,
            "exists": self.exists,
            "non_empty": self.non_empty,
            "format": self.format,
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        required = {"schema_version", "target", "exists", "non_empty", "format"}
        data = _fields(value, allowed=required, required=required)
        return cls(
            schema_version=data["schema_version"],
            target=data["target"],
            exists=data["exists"],
            non_empty=data["non_empty"],
            format=data["format"],
        )


def _prefix_nested_error(error: ValidationError, parent: str) -> ValidationError:
    path = f"{parent}{error.path}" if error.path else parent
    if len(path) > _MAX_ERROR_PATH_LENGTH:
        path = parent
    return ValidationError(error.code, path)


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationSnapshot:
    """Canonical immutable facts for one sealed candidate revision."""

    candidate_revision: str
    shapes: tuple[ShapeObservation, ...] = ()
    artifacts: tuple[ArtifactObservation, ...] = ()
    schema_version: int = SCHEMA_VERSION
    observation_digest: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "schema_version", _validate_schema(self.schema_version, "/schema_version")
        )
        object.__setattr__(
            self,
            "candidate_revision",
            _validate_revision(self.candidate_revision),
        )
        if type(self.shapes) is not tuple:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/shapes")
        if len(self.shapes) > _MAX_SHAPES:
            _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED, "/shapes")
        for index, shape in enumerate(self.shapes):
            if type(shape) is not ShapeObservation:
                _raise_validation(
                    ValidationErrorCode.INVALID_TYPE,
                    join_json_pointer("/shapes", str(index)),
                )
        if type(self.artifacts) is not tuple:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/artifacts")
        if len(self.artifacts) > _MAX_ARTIFACTS:
            _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED, "/artifacts")
        for index, artifact in enumerate(self.artifacts):
            if type(artifact) is not ArtifactObservation:
                _raise_validation(
                    ValidationErrorCode.INVALID_TYPE,
                    join_json_pointer("/artifacts", str(index)),
                )

        shape_targets = tuple(item.target for item in self.shapes)
        if len(shape_targets) != len(set(shape_targets)):
            _raise_validation(ValidationErrorCode.DUPLICATE_TARGET, "/shapes")
        if shape_targets != tuple(sorted(shape_targets)):
            _raise_validation(ValidationErrorCode.INVALID_VALUE, "/shapes")
        artifact_targets = tuple(item.target for item in self.artifacts)
        if len(artifact_targets) != len(set(artifact_targets)):
            _raise_validation(ValidationErrorCode.DUPLICATE_TARGET, "/artifacts")
        if artifact_targets != tuple(sorted(artifact_targets)):
            _raise_validation(ValidationErrorCode.INVALID_VALUE, "/artifacts")

        facts = 0
        for shape in self.shapes:
            facts += int(shape.volume_mm3 is not None)
            facts += int(shape.area_mm2 is not None)
            facts += len(shape.bbox_mm) if shape.bbox_mm is not None else 0
            facts += len(shape.center_of_mass_mm) if shape.center_of_mass_mm is not None else 0
            facts += int(shape.valid_shape is not None)
            facts += int(shape.solid_count is not None)
        for artifact in self.artifacts:
            facts += 1
            facts += int(artifact.non_empty is not None)
            facts += int(artifact.format is not None)
        if facts > _MAX_OBSERVATION_FACTS:
            _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED)

        canonical = _canonical_json_bytes(self._digest_mapping())
        if len(canonical) > _MAX_SNAPSHOT_BYTES:
            _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED)
        digest = hashlib.sha256(_OBSERVATION_DOMAIN + canonical).hexdigest()
        object.__setattr__(self, "observation_digest", digest)

    def _digest_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "candidate_revision": self.candidate_revision,
            "shapes": [item.to_mapping() for item in self.shapes],
            "artifacts": [item.to_mapping() for item in self.artifacts],
        }

    def to_mapping(self) -> dict[str, object]:
        result = self._digest_mapping()
        result["observation_digest"] = self.observation_digest
        return result

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        required = {
            "schema_version",
            "candidate_revision",
            "shapes",
            "artifacts",
            "observation_digest",
        }
        data = _fields(value, allowed=required, required=required)
        if type(data["shapes"]) is not list:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/shapes")
        shapes: list[ShapeObservation] = []
        for index, raw_shape in enumerate(data["shapes"]):
            caught = None
            try:
                parsed = ShapeObservation.from_mapping(raw_shape)
            except ValidationError as error:
                caught = error
                parsed = None
            if caught is not None:
                raise _prefix_nested_error(caught, join_json_pointer("/shapes", str(index)))
            assert parsed is not None
            shapes.append(parsed)
        if type(data["artifacts"]) is not list:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, "/artifacts")
        artifacts: list[ArtifactObservation] = []
        for index, raw_artifact in enumerate(data["artifacts"]):
            caught = None
            try:
                parsed_artifact = ArtifactObservation.from_mapping(raw_artifact)
            except ValidationError as error:
                caught = error
                parsed_artifact = None
            if caught is not None:
                raise _prefix_nested_error(
                    caught,
                    join_json_pointer("/artifacts", str(index)),
                )
            assert parsed_artifact is not None
            artifacts.append(parsed_artifact)
        supplied_digest = _validate_digest(data["observation_digest"], "/observation_digest")
        snapshot = cls(
            schema_version=data["schema_version"],
            candidate_revision=data["candidate_revision"],
            shapes=tuple(shapes),
            artifacts=tuple(artifacts),
        )
        if supplied_digest != snapshot.observation_digest:
            _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/observation_digest")
        return snapshot


def _validated_snapshot(value: object) -> ObservationSnapshot:
    if type(value) is not ObservationSnapshot:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, "/snapshot")
    incomplete = False
    try:
        revision = value.candidate_revision
        shapes = value.shapes
        artifacts = value.artifacts
        schema_version = value.schema_version
        supplied_digest = value.observation_digest
    except AttributeError:
        incomplete = True
        revision = None
        shapes = ()
        artifacts = ()
        schema_version = SCHEMA_VERSION
        supplied_digest = None
    if incomplete:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, "/snapshot")
    caught = None
    try:
        rebuilt = ObservationSnapshot(
            candidate_revision=revision,
            shapes=shapes,
            artifacts=artifacts,
            schema_version=schema_version,
        )
    except ValidationError as error:
        caught = error
        rebuilt = None
    if caught is not None:
        raise ValidationError(caught.code, caught.path)
    assert rebuilt is not None
    if supplied_digest != rebuilt.observation_digest:
        _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/observation_digest")
    return rebuilt


class _OpaqueCapability:
    __slots__ = ("_seal", "__weakref__")

    def __new__(cls, *_args, **_kwargs):
        raise TypeError("validation capabilities cannot be constructed directly")

    def __setattr__(self, _name, _value) -> None:
        raise TypeError("validation capabilities are immutable")

    def __delattr__(self, _name) -> None:
        raise TypeError("validation capabilities are immutable")

    def __copy__(self):
        raise TypeError("validation capabilities cannot be copied")

    def __deepcopy__(self, _memo):
        raise TypeError("validation capabilities cannot be copied")

    def __reduce__(self):
        raise TypeError("validation capabilities cannot be serialized")

    def __reduce_ex__(self, _protocol):
        raise TypeError("validation capabilities cannot be serialized")


class CompiledAcceptance(_OpaqueCapability):
    """Opaque authentic result of fail-closed acceptance compilation."""

    __slots__ = ()

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("CompiledAcceptance cannot be subclassed")

    @property
    def acceptance_id(self) -> str:
        return _lookup_compiled(self).acceptance_id

    @property
    def spec_digest(self) -> str:
        return _lookup_compiled(self).spec_digest

    def __repr__(self) -> str:
        return "CompiledAcceptance(<opaque>)"


class VerificationReceipt(_OpaqueCapability):
    """Opaque one-shot authority for one successful deterministic report."""

    __slots__ = ()

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("VerificationReceipt cannot be subclassed")

    def __repr__(self) -> str:
        return "VerificationReceipt(<opaque>)"


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationResult:
    """A durable report plus a receipt only when required criteria pass."""

    report: VerificationReport
    receipt: VerificationReceipt | None

    def __post_init__(self) -> None:
        if type(self.report) is not VerificationReport:
            raise TypeError("report must be a VerificationReport")
        if self.report.passed:
            if type(self.receipt) is not VerificationReceipt:
                raise TypeError("a passing report requires a VerificationReceipt")
        elif self.receipt is not None:
            raise TypeError("a failing report cannot contain a VerificationReceipt")


@dataclass(frozen=True, slots=True)
class _CompiledBinding:
    acceptance_id: str
    spec_digest: str
    payload: object


@dataclass(frozen=True, slots=True)
class _ReceiptBinding:
    compiled: CompiledAcceptance
    spec_digest: str
    acceptance_id: str
    candidate_revision: str
    manifest_sha256: str
    observation_digest: str
    report: VerificationReport


@dataclass(slots=True)
class _ReceiptRecord:
    binding: _ReceiptBinding
    report_digest: str
    trusted_report: VerificationReport
    consumed: bool = False


_PROCESS_SEAL = object()
_REGISTRY_LOCK = threading.RLock()
_COMPILED_REGISTRY: dict[
    int,
    tuple[weakref.ReferenceType[CompiledAcceptance], _CompiledBinding],
] = {}
_RECEIPT_REGISTRY: dict[
    int,
    tuple[weakref.ReferenceType[VerificationReceipt], _ReceiptRecord],
] = {}


def _issue_compiled(binding: _CompiledBinding) -> CompiledAcceptance:
    if type(binding) is not _CompiledBinding:
        raise TypeError("binding must be a _CompiledBinding")
    with _REGISTRY_LOCK:
        if len(_COMPILED_REGISTRY) >= _MAX_ACTIVE_COMPILED:
            _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED, "/compiled")
        capability = object.__new__(CompiledAcceptance)
        object.__setattr__(capability, "_seal", _PROCESS_SEAL)
        identity = id(capability)

        def cleanup(reference) -> None:
            with _REGISTRY_LOCK:
                current = _COMPILED_REGISTRY.get(identity)
                if current is not None and current[0] is reference:
                    del _COMPILED_REGISTRY[identity]

        reference = weakref.ref(capability, cleanup)
        _COMPILED_REGISTRY[identity] = (reference, binding)
    return capability


def _lookup_compiled(value: object) -> _CompiledBinding:
    if type(value) is not CompiledAcceptance:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, "/compiled")
    missing_seal = False
    try:
        seal = value._seal
    except AttributeError:
        missing_seal = True
        seal = None
    if missing_seal or seal is not _PROCESS_SEAL:
        _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/compiled")
    with _REGISTRY_LOCK:
        record = _COMPILED_REGISTRY.get(id(value))
        if record is None:
            _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/compiled")
        reference = record[0]
        if reference() is not value:
            _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/compiled")
        return record[1]


def _issue_receipt(binding: _ReceiptBinding) -> VerificationReceipt:
    if type(binding) is not _ReceiptBinding:
        raise TypeError("binding must be a _ReceiptBinding")
    report_digest = _report_digest(binding.report)
    trusted_report = _clone_report(binding.report)
    with _REGISTRY_LOCK:
        if len(_RECEIPT_REGISTRY) >= _MAX_ACTIVE_RECEIPTS:
            _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED, "/receipt")
        receipt = object.__new__(VerificationReceipt)
        object.__setattr__(receipt, "_seal", _PROCESS_SEAL)
        identity = id(receipt)

        def cleanup(reference) -> None:
            with _REGISTRY_LOCK:
                current = _RECEIPT_REGISTRY.get(identity)
                if current is not None and current[0] is reference:
                    del _RECEIPT_REGISTRY[identity]

        reference = weakref.ref(receipt, cleanup)
        _RECEIPT_REGISTRY[identity] = (
            reference,
            _ReceiptRecord(binding, report_digest, trusted_report),
        )
    return receipt


def _report_digest(report: object) -> str:
    failed = False
    try:
        mapping = report.to_mapping()
        canonical = _canonical_json_bytes(mapping)
    except (AttributeError, TypeError, ValueError, RecursionError):
        failed = True
        canonical = None
    if failed or canonical is None:
        _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/receipt")
    return hashlib.sha256(_RECEIPT_REPORT_DOMAIN + canonical).hexdigest()


def _clone_report(report: VerificationReport) -> VerificationReport:
    verdicts = tuple(
        CriterionVerdict(
            criterion_id=verdict.criterion_id,
            required=verdict.required,
            message=verdict.message,
            outcome=verdict.outcome,
            expected=verdict.expected,
            observed=verdict.observed,
            delta=verdict.delta,
            tolerance=verdict.tolerance,
            evidence=verdict.evidence,
            schema_version=verdict.schema_version,
        )
        for verdict in report.verdicts
    )
    return VerificationReport(
        id=report.id,
        acceptance_id=report.acceptance_id,
        candidate_revision=report.candidate_revision,
        manifest_sha256=report.manifest_sha256,
        observation_digest=report.observation_digest,
        passed=report.passed,
        verdicts=verdicts,
        schema_version=report.schema_version,
    )


def _lookup_receipt_record(value: object) -> _ReceiptRecord:
    if type(value) is not VerificationReceipt:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, "/receipt")
    missing_seal = False
    try:
        seal = value._seal
    except AttributeError:
        missing_seal = True
        seal = None
    if missing_seal or seal is not _PROCESS_SEAL:
        _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/receipt")
    record = _RECEIPT_REGISTRY.get(id(value))
    if record is None:
        _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/receipt")
    reference = record[0]
    if reference() is not value:
        _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/receipt")
    return record[1]


def _consume_receipt(
    receipt: object,
    compiled: object,
    *,
    candidate_revision: str,
    manifest_sha256: str,
    observation_digest: str,
) -> VerificationReport:
    compiled_binding = _lookup_compiled(compiled)
    with _REGISTRY_LOCK:
        record = _lookup_receipt_record(receipt)
        if record.consumed:
            _raise_validation(ValidationErrorCode.REPLAYED_RECEIPT, "/receipt")
        binding = record.binding
        report = binding.report
        current_report_digest = _report_digest(report)
        matches = (
            binding.compiled is compiled
            and binding.spec_digest == compiled_binding.spec_digest
            and binding.acceptance_id == compiled_binding.acceptance_id
            and binding.candidate_revision == candidate_revision
            and binding.manifest_sha256 == manifest_sha256
            and binding.observation_digest == observation_digest
            and report.passed
            and report.acceptance_id == binding.acceptance_id
            and report.candidate_revision == candidate_revision
            and report.manifest_sha256 == manifest_sha256
            and report.observation_digest == observation_digest
            and current_report_digest == record.report_digest
        )
        if not matches:
            _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/receipt")
        record.consumed = True
        return record.trusted_report
