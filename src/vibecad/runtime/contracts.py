"""Domain-neutral contracts for invoking and controlling external runtimes.

The contracts in this module describe immutable in-process values only.  They
do not select a domain adapter, own a clock, persist state, or grant revision,
review, commit, or public-tool authority.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

type JsonScalar = None | bool | int | float | str
type FrozenJson = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[_.-][a-z0-9]+)*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
_MEDIA_TYPE = re.compile(r"^[a-z][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_TEXT_LENGTH = 256
_MAX_JSON_DEPTH = 32
_MAX_JSON_CONTAINER_ITEMS = 1_024
_MAX_JSON_TOTAL_NODES = 8_192
_MAX_JSON_STRING_BYTES = 65_536
_MAX_JSON_TOTAL_STRING_BYTES = 1_048_576
_MAX_CONTRACT_COLLECTION_ITEMS = 1_024
_MAX_SAFE_INTEGER = 2**53 - 1


def _text(value: object, name: str, *, pattern: re.Pattern[str] | None = None) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or value != value.strip()
        or len(value) > _MAX_TEXT_LENGTH
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise ValueError(f"{name} must be bounded printable single-line text")
    return value


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} must be a positive safe integer")
    return value


@dataclass(slots=True)
class _FreezeBudget:
    nodes: int = 0
    string_bytes: int = 0
    active_containers: set[int] = field(default_factory=set)

    def consume(self, name: str) -> None:
        self.nodes += 1
        if self.nodes > _MAX_JSON_TOTAL_NODES:
            raise ValueError(f"{name} exceeds the maximum total nodes")

    def consume_string_bytes(self, count: int, name: str) -> None:
        self.string_bytes += count
        if self.string_bytes > _MAX_JSON_TOTAL_STRING_BYTES:
            raise ValueError(f"{name} exceeds the maximum cumulative UTF-8 bytes")


def _json_string(value: str, name: str, budget: _FreezeBudget) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} contains invalid UTF-8 text") from exc
    if len(encoded) > _MAX_JSON_STRING_BYTES:
        raise ValueError(f"{name} exceeds the maximum UTF-8 bytes")
    budget.consume_string_bytes(len(encoded), name)
    return value


def _enter_container(value: object, *, name: str, budget: _FreezeBudget) -> int:
    identity = id(value)
    if identity in budget.active_containers:
        raise ValueError(f"{name} contains a container cycle")
    budget.active_containers.add(identity)
    return identity


def _freeze_mapping(
    value: Mapping[object, object],
    *,
    name: str,
    depth: int,
    budget: _FreezeBudget,
) -> Mapping[str, FrozenJson]:
    identity = _enter_container(value, name=name, budget=budget)
    frozen: dict[str, FrozenJson] = {}
    try:
        try:
            iterator = iter(value)
        except Exception as exc:
            raise ValueError(f"{name} could not be enumerated") from exc
        try:
            for index, key in enumerate(iterator):
                if index >= _MAX_JSON_CONTAINER_ITEMS:
                    raise ValueError(f"{name} exceeds the maximum container items")
                budget.consume(name)
                if type(key) is not str:
                    raise TypeError(f"{name} must have string keys")
                _json_string(key, name, budget)
                if key in frozen:
                    raise ValueError(f"{name} must have unique string keys")
                try:
                    item = value[key]
                except Exception as exc:
                    raise ValueError(f"{name} could not be snapshotted") from exc
                frozen[key] = _freeze_json(
                    item,
                    name=name,
                    depth=depth + 1,
                    budget=budget,
                )
        except (TypeError, ValueError):
            raise
        except Exception as exc:
            raise ValueError(f"{name} could not be enumerated") from exc
        return MappingProxyType({key: frozen[key] for key in sorted(frozen)})
    finally:
        budget.active_containers.remove(identity)


def _freeze_sequence(
    value: Sequence[object],
    *,
    name: str,
    depth: int,
    budget: _FreezeBudget,
) -> tuple[FrozenJson, ...]:
    identity = _enter_container(value, name=name, budget=budget)
    frozen: list[FrozenJson] = []
    try:
        try:
            iterator = iter(value)
        except Exception as exc:
            raise ValueError(f"{name} could not be enumerated") from exc
        try:
            for index, item in enumerate(iterator):
                if index >= _MAX_JSON_CONTAINER_ITEMS:
                    raise ValueError(f"{name} exceeds the maximum container items")
                frozen.append(
                    _freeze_json(
                        item,
                        name=name,
                        depth=depth + 1,
                        budget=budget,
                    )
                )
        except (TypeError, ValueError):
            raise
        except Exception as exc:
            raise ValueError(f"{name} could not be enumerated") from exc
        return tuple(frozen)
    finally:
        budget.active_containers.remove(identity)


def _freeze_json(
    value: object,
    *,
    name: str,
    depth: int = 0,
    budget: _FreezeBudget | None = None,
) -> FrozenJson:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{name} exceeds the maximum container depth")
    if budget is None:
        budget = _FreezeBudget()
    budget.consume(name)
    if value is None or type(value) is bool:
        return value  # type: ignore[return-value]
    if type(value) is str:
        return _json_string(value, name, budget)
    if type(value) is int:
        if abs(value) > _MAX_SAFE_INTEGER:
            raise ValueError(f"{name} contains an unsafe integer")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        return _freeze_mapping(value, name=name, depth=depth, budget=budget)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _freeze_sequence(value, name=name, depth=depth, budget=budget)
    raise TypeError(f"{name} must contain only JSON-compatible values")


def _freeze_mapping_root(value: object, *, name: str) -> Mapping[str, FrozenJson]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    frozen = _freeze_json(value, name=name)
    if not isinstance(frozen, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return frozen


def _bounded_collection_snapshot(
    values: object,
    *,
    name: str,
) -> tuple[object, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values,
        Sequence,
    ):
        raise TypeError(f"{name} must be a sequence")
    try:
        iterator = iter(values)
    except Exception as exc:
        raise ValueError(f"{name} could not be enumerated") from exc
    result: list[object] = []
    for index in range(_MAX_CONTRACT_COLLECTION_ITEMS + 1):
        try:
            item = next(iterator)
        except StopIteration:
            return tuple(result)
        except Exception as exc:
            raise ValueError(f"{name} could not be enumerated") from exc
        if index == _MAX_CONTRACT_COLLECTION_ITEMS:
            raise ValueError(f"{name} exceeds the maximum contract collection items")
        result.append(item)
    raise RuntimeError("unreachable bounded collection snapshot")


def _typed_tuple[ItemT](
    values: object,
    item_type: type[ItemT],
    name: str,
) -> tuple[ItemT, ...]:
    result = _bounded_collection_snapshot(values, name=name)
    if any(type(item) is not item_type for item in result):
        raise TypeError(f"{name} must contain only {item_type.__name__} values")
    return result  # type: ignore[return-value]


def _text_tuple(
    values: object,
    name: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    snapshot = _bounded_collection_snapshot(values, name=name)
    return tuple(_text(item, name, pattern=pattern) for item in snapshot)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeIdentity:
    """Stable identity of one versioned runtime implementation."""

    family: str
    provider: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", _text(self.family, "family", pattern=_NAME))
        object.__setattr__(
            self,
            "provider",
            _text(self.provider, "provider", pattern=_NAME),
        )
        object.__setattr__(
            self,
            "version",
            _text(self.version, "version", pattern=_VERSION),
        )

    @property
    def key(self) -> str:
        """Return the deterministic full identity key."""

        return f"{self.family}/{self.provider}@{self.version}"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeCapability:
    """One versioned capability declared by a runtime."""

    name: str
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "name", pattern=_NAME))
        object.__setattr__(
            self,
            "version",
            _positive_integer(self.version, "version"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeDescriptor:
    """Immutable runtime discovery record."""

    identity: RuntimeIdentity
    capabilities: tuple[RuntimeCapability, ...] = ()
    execution_profiles: tuple[str, ...] = ()
    metadata: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.identity) is not RuntimeIdentity:
            raise TypeError("identity must be a RuntimeIdentity")
        capabilities = _typed_tuple(
            self.capabilities,
            RuntimeCapability,
            "capabilities",
        )
        capability_keys = tuple((item.name, item.version) for item in capabilities)
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("duplicate runtime capability")
        profiles = _text_tuple(
            self.execution_profiles,
            "execution_profiles",
            pattern=_NAME,
        )
        if len(profiles) != len(set(profiles)):
            raise ValueError("duplicate execution profile")
        frozen_metadata = _freeze_mapping_root(self.metadata, name="metadata")
        object.__setattr__(
            self,
            "capabilities",
            tuple(sorted(capabilities, key=lambda item: (item.name, item.version))),
        )
        object.__setattr__(self, "execution_profiles", tuple(sorted(profiles)))
        object.__setattr__(self, "metadata", frozen_metadata)

    def supports(self, capability: RuntimeCapability) -> bool:
        """Return whether the exact capability version was declared."""

        if type(capability) is not RuntimeCapability:
            raise TypeError("capability must be a RuntimeCapability")
        return capability in self.capabilities


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeBudget:
    """Caller-owned upper bounds for one runtime invocation."""

    max_elapsed_ms: int
    max_memory_bytes: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        for name in ("max_elapsed_ms", "max_memory_bytes", "max_output_bytes"):
            object.__setattr__(
                self,
                name,
                _positive_integer(getattr(self, name), name),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeArtifact:
    """Sealed runtime-qualified artifact reference and metadata."""

    artifact_id: str
    kind: str
    media_type: str
    digest: str
    runtime: RuntimeIdentity
    metadata: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _text(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "kind", _text(self.kind, "kind", pattern=_NAME))
        object.__setattr__(
            self,
            "media_type",
            _text(self.media_type, "media_type", pattern=_MEDIA_TYPE),
        )
        object.__setattr__(
            self,
            "digest",
            _text(self.digest, "digest", pattern=_SHA256),
        )
        if type(self.runtime) is not RuntimeIdentity:
            raise TypeError("runtime must be a RuntimeIdentity")
        metadata = _freeze_mapping_root(self.metadata, name="metadata")
        object.__setattr__(self, "metadata", metadata)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeInvocation:
    """Immutable request envelope owned and correlated by the application."""

    invocation_id: str
    owner_id: str
    task_id: str
    runtime: RuntimeIdentity
    capability: RuntimeCapability
    budget: RuntimeBudget
    deadline_ms: int
    input_revision: str | None = None
    input_artifacts: tuple[RuntimeArtifact, ...] = ()
    payload: Mapping[str, FrozenJson] = field(default_factory=dict)
    execution_profile: str | None = None

    def __post_init__(self) -> None:
        for name in ("invocation_id", "owner_id", "task_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if type(self.runtime) is not RuntimeIdentity:
            raise TypeError("runtime must be a RuntimeIdentity")
        if type(self.capability) is not RuntimeCapability:
            raise TypeError("capability must be a RuntimeCapability")
        if type(self.budget) is not RuntimeBudget:
            raise TypeError("budget must be a RuntimeBudget")
        object.__setattr__(
            self,
            "deadline_ms",
            _positive_integer(self.deadline_ms, "deadline_ms"),
        )
        if self.input_revision is not None:
            object.__setattr__(
                self,
                "input_revision",
                _text(self.input_revision, "input_revision"),
            )
        artifacts = _typed_tuple(
            self.input_artifacts,
            RuntimeArtifact,
            "input_artifacts",
        )
        if len({item.artifact_id for item in artifacts}) != len(artifacts):
            raise ValueError("duplicate input artifact_id")
        payload = _freeze_mapping_root(self.payload, name="payload")
        if self.execution_profile is not None:
            object.__setattr__(
                self,
                "execution_profile",
                _text(self.execution_profile, "execution_profile", pattern=_NAME),
            )
        object.__setattr__(
            self,
            "input_artifacts",
            tuple(sorted(artifacts, key=lambda item: item.artifact_id)),
        )
        object.__setattr__(self, "payload", payload)


class RuntimeLifecycleState(StrEnum):
    """Closed lifecycle states reported by runtime control ports."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        """Return whether no more runtime execution is expected."""

        return self in {self.SUCCEEDED, self.FAILED, self.CANCELLED}


class RuntimeHealthState(StrEnum):
    """Closed runtime health states independent of invocation lifecycle."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeDiagnostic:
    """One immutable runtime diagnostic without domain semantics."""

    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _text(self.code, "code", pattern=_NAME))
        object.__setattr__(self, "message", _text(self.message, "message"))
        if type(self.retryable) is not bool:
            raise TypeError("retryable must be a boolean")
        details = _freeze_mapping_root(self.details, name="details")
        object.__setattr__(self, "details", details)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeHealth:
    """Immutable health snapshot for one exact runtime identity."""

    runtime: RuntimeIdentity
    state: RuntimeHealthState
    diagnostics: tuple[RuntimeDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        if type(self.runtime) is not RuntimeIdentity:
            raise TypeError("runtime must be a RuntimeIdentity")
        if type(self.state) is not RuntimeHealthState:
            raise TypeError("state must be a RuntimeHealthState")
        object.__setattr__(
            self,
            "diagnostics",
            _typed_tuple(self.diagnostics, RuntimeDiagnostic, "diagnostics"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeEvidence:
    """Runtime-produced evidence that remains subject to domain verification."""

    kind: str
    name: str
    value: FrozenJson

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _text(self.kind, "kind", pattern=_NAME))
        object.__setattr__(self, "name", _text(self.name, "name", pattern=_NAME))
        object.__setattr__(
            self,
            "value",
            _freeze_json(self.value, name="value"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeProvenance:
    """Immutable attribution for one runtime execution."""

    runtime: RuntimeIdentity
    invocation_id: str
    input_artifact_ids: tuple[str, ...] = ()
    details: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.runtime) is not RuntimeIdentity:
            raise TypeError("runtime must be a RuntimeIdentity")
        object.__setattr__(
            self,
            "invocation_id",
            _text(self.invocation_id, "invocation_id"),
        )
        artifact_ids = _text_tuple(self.input_artifact_ids, "input_artifact_ids")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("duplicate input artifact_id in provenance")
        details = _freeze_mapping_root(self.details, name="details")
        object.__setattr__(
            self,
            "input_artifact_ids",
            tuple(sorted(artifact_ids)),
        )
        object.__setattr__(self, "details", details)


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeStatus:
    """Immutable status snapshot returned by lifecycle control."""

    invocation_id: str
    runtime: RuntimeIdentity
    state: RuntimeLifecycleState
    diagnostics: tuple[RuntimeDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "invocation_id",
            _text(self.invocation_id, "invocation_id"),
        )
        if type(self.runtime) is not RuntimeIdentity:
            raise TypeError("runtime must be a RuntimeIdentity")
        if type(self.state) is not RuntimeLifecycleState:
            raise TypeError("state must be a RuntimeLifecycleState")
        object.__setattr__(
            self,
            "diagnostics",
            _typed_tuple(self.diagnostics, RuntimeDiagnostic, "diagnostics"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeResult:
    """Terminal immutable result envelope returned by a runtime adapter."""

    invocation_id: str
    runtime: RuntimeIdentity
    state: RuntimeLifecycleState
    artifacts: tuple[RuntimeArtifact, ...] = ()
    provenance: RuntimeProvenance | None = None
    diagnostics: tuple[RuntimeDiagnostic, ...] = ()
    evidence: tuple[RuntimeEvidence, ...] = ()
    output: Mapping[str, FrozenJson] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "invocation_id",
            _text(self.invocation_id, "invocation_id"),
        )
        if type(self.runtime) is not RuntimeIdentity:
            raise TypeError("runtime must be a RuntimeIdentity")
        if type(self.state) is not RuntimeLifecycleState:
            raise TypeError("state must be a RuntimeLifecycleState")
        if not self.state.is_terminal:
            raise ValueError("runtime result state must be terminal")
        artifacts = _typed_tuple(self.artifacts, RuntimeArtifact, "artifacts")
        if len({item.artifact_id for item in artifacts}) != len(artifacts):
            raise ValueError("duplicate result artifact_id")
        if any(item.runtime != self.runtime for item in artifacts):
            raise ValueError("result artifact runtime must match result runtime")
        if artifacts and self.provenance is None:
            raise ValueError("provenance is required when result artifacts are present")
        if self.provenance is not None:
            if type(self.provenance) is not RuntimeProvenance:
                raise TypeError("provenance must be a RuntimeProvenance or null")
            if self.provenance.invocation_id != self.invocation_id:
                raise ValueError("provenance invocation_id must match result")
            if self.provenance.runtime != self.runtime:
                raise ValueError("provenance runtime must match result runtime")
        diagnostics = _typed_tuple(
            self.diagnostics,
            RuntimeDiagnostic,
            "diagnostics",
        )
        evidence = _typed_tuple(self.evidence, RuntimeEvidence, "evidence")
        output = _freeze_mapping_root(self.output, name="output")
        object.__setattr__(
            self,
            "artifacts",
            tuple(sorted(artifacts, key=lambda item: item.artifact_id)),
        )
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "output", output)


class RuntimeControlPort(Protocol):
    """Application-owned lifecycle control authority for one runtime adapter."""

    def start(self, invocation: RuntimeInvocation) -> RuntimeStatus:
        """Start one immutable invocation and return its initial status."""

        ...

    def get_status(self, invocation_id: str) -> RuntimeStatus:
        """Return a current status snapshot without changing lifecycle state."""

        ...

    def cancel(self, invocation_id: str, *, reason: str) -> RuntimeStatus:
        """Request cancellation and return the resulting status snapshot."""

        ...

    def reconcile(self, invocation_id: str) -> RuntimeStatus:
        """Reconcile uncertain process state with the adapter's runtime."""

        ...

    def health(self, identity: RuntimeIdentity) -> RuntimeHealth:
        """Return runtime health without starting or mutating an invocation."""

        ...


class RuntimeResultPort(Protocol):
    """Read an available terminal result without lifecycle-control authority."""

    def get_result(self, invocation_id: str) -> RuntimeResult | None:
        """Return one terminal result, or ``None`` without waiting or mutation."""

        ...
