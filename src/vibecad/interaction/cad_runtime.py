"""Backend-neutral internal CAD runtime planning and selection contracts.

This module owns immutable CAD-domain values and deterministic adapter
selection only.  It does not execute lifecycle hooks, persist data, or expose
product tools.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from vibecad.execution.selectors import SelectorV1
from vibecad.runtime.contracts import (
    RuntimeArtifact,
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeIdentity,
)
from vibecad.runtime.registry import RuntimeRegistry

_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[_.-][a-z0-9]+)*$")
_MEDIA_TYPE = re.compile(r"^[a-z][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_MAX_TEXT_BYTES = 256
_MAX_CAD_COLLECTION_ITEMS = 1_024
_MAX_CAD_ADAPTERS = 256
_MAX_SAFE_INTEGER = 2**53 - 1
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_IDENTIFIER_TOKEN = re.compile(r"[A-Za-z0-9]+")
_FORBIDDEN_ADAPTER_AUTHORITY_TOKENS = frozenset(
    {
        "accept",
        "commit",
        "head",
        "reject",
        "review",
    }
)
_FORBIDDEN_ADAPTER_AUTHORITIES = frozenset(
    {
        "accept",
        "commit",
        "head",
        "lease",
        "public_tool",
        "reject",
        "review",
        "revision",
        "store",
        "task",
    }
)

CAD_EXECUTE_PROGRAM_V1 = RuntimeCapability(
    name="authoring.execute_program",
    version=1,
)


def _text(
    value: object,
    name: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    if (
        not value.strip()
        or value != value.strip()
        or not value.isprintable()
        or len(value.splitlines()) != 1
        or len(encoded) > _MAX_TEXT_BYTES
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        raise ValueError(f"{name} must be bounded printable single-line text")
    return value


def _positive_integer(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SAFE_INTEGER:
        raise ValueError(f"{name} must be a positive safe integer")
    return value


def _snapshot(
    values: object,
    *,
    name: str,
    limit: int,
) -> tuple[object, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an iterable")
    try:
        iterator = iter(values)  # type: ignore[arg-type]
    except Exception as exc:
        raise ValueError(f"{name} could not be enumerated") from exc
    result: list[object] = []
    for index in range(limit + 1):
        try:
            item = next(iterator)
        except StopIteration:
            return tuple(result)
        except Exception as exc:
            raise ValueError(f"{name} could not be enumerated") from exc
        if index == limit:
            raise ValueError(f"{name} exceeds the maximum of {limit} items")
        result.append(item)
    raise RuntimeError("unreachable bounded CAD collection snapshot")


def _runtime(value: object, name: str = "runtime") -> CadRuntimeIdentity:
    if type(value) is not CadRuntimeIdentity:
        raise TypeError(f"{name} must be a CadRuntimeIdentity")
    return value


def _capability(value: object, name: str) -> RuntimeCapability:
    if type(value) is not RuntimeCapability:
        raise TypeError(f"{name} must be a RuntimeCapability")
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class CadRuntimeIdentity:
    """Exact identity of one CAD-family runtime."""

    runtime: RuntimeIdentity

    def __post_init__(self) -> None:
        if type(self.runtime) is not RuntimeIdentity:
            raise TypeError("runtime must be a RuntimeIdentity")
        if self.runtime.family != "cad":
            raise ValueError("runtime family must be cad")

    @property
    def key(self) -> str:
        """Return the exact generic runtime key."""

        return self.runtime.key


@dataclass(frozen=True, slots=True, kw_only=True)
class CadRuntimeExtension:
    """Explicit runtime-qualified request for non-portable behavior."""

    runtime: CadRuntimeIdentity
    name: str
    capability: RuntimeCapability

    def __post_init__(self) -> None:
        runtime = _runtime(self.runtime)
        name = _text(self.name, "extension name", pattern=_NAME)
        if not name.startswith(f"{runtime.runtime.provider}."):
            raise ValueError("extension name must use the runtime provider namespace")
        object.__setattr__(self, "name", name)
        _capability(self.capability, "capability")


type CadCapabilityRequest = RuntimeCapability | CadRuntimeExtension


@dataclass(frozen=True, slots=True, kw_only=True)
class CadNativeDecision:
    """Execute the exact requested capability natively."""

    runtime: CadRuntimeIdentity
    requested: RuntimeCapability

    def __post_init__(self) -> None:
        _runtime(self.runtime)
        _capability(self.requested, "requested")

    @property
    def selected(self) -> RuntimeCapability:
        return self.requested

    @property
    def executable(self) -> bool:
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class CadSemanticMappingDecision:
    """Execute through a distinct declared capability with disclosure."""

    runtime: CadRuntimeIdentity
    requested: RuntimeCapability
    selected: RuntimeCapability
    disclosure: str

    def __post_init__(self) -> None:
        _runtime(self.runtime)
        requested = _capability(self.requested, "requested")
        selected = _capability(self.selected, "selected")
        if requested == selected:
            raise ValueError("semantic mapping selected capability must be different")
        object.__setattr__(
            self,
            "disclosure",
            _text(self.disclosure, "disclosure"),
        )

    @property
    def executable(self) -> bool:
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class CadApproximationDecision:
    """Non-executable approximation proposal requiring a later decision."""

    runtime: CadRuntimeIdentity
    requested: RuntimeCapability
    proposal: str

    def __post_init__(self) -> None:
        _runtime(self.runtime)
        _capability(self.requested, "requested")
        object.__setattr__(self, "proposal", _text(self.proposal, "proposal"))

    @property
    def executable(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class CadUnsupportedDecision:
    """Non-executable rejection produced before adapter selection."""

    runtime: CadRuntimeIdentity
    requested: CadCapabilityRequest
    reason: str

    def __post_init__(self) -> None:
        _runtime(self.runtime)
        if type(self.requested) is RuntimeCapability:
            _capability(self.requested, "requested")
        elif type(self.requested) is CadRuntimeExtension:
            _runtime(self.requested.runtime, "requested runtime")
            if self.requested.runtime != self.runtime:
                raise ValueError(
                    "unsupported extension request runtime must match decision runtime"
                )
        else:
            raise TypeError("requested must be a RuntimeCapability or CadRuntimeExtension")
        object.__setattr__(self, "reason", _text(self.reason, "reason"))

    @property
    def executable(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class CadExtensionDecision:
    """Execute one explicit runtime-qualified extension capability."""

    extension: CadRuntimeExtension

    def __post_init__(self) -> None:
        if type(self.extension) is not CadRuntimeExtension:
            raise TypeError("extension must be a CadRuntimeExtension")

    @property
    def requested(self) -> CadRuntimeExtension:
        return self.extension

    @property
    def runtime(self) -> CadRuntimeIdentity:
        return self.extension.runtime

    @property
    def selected(self) -> RuntimeCapability:
        return self.extension.capability

    @property
    def executable(self) -> bool:
        return True


type CadCapabilityDecision = (
    CadNativeDecision
    | CadSemanticMappingDecision
    | CadApproximationDecision
    | CadUnsupportedDecision
    | CadExtensionDecision
)

_DECISION_TYPES = (
    CadNativeDecision,
    CadSemanticMappingDecision,
    CadApproximationDecision,
    CadUnsupportedDecision,
    CadExtensionDecision,
)


class CadArtifactRole(StrEnum):
    """Closed semantic roles for runtime-qualified CAD artifacts."""

    NATIVE_MODEL = "native_model"
    EXCHANGE = "exchange"
    SEMANTIC_OBSERVATION = "semantic_observation"
    SELECTOR_MAPPING = "selector_mapping"
    PROVENANCE = "provenance"
    EVIDENCE = "evidence"


@dataclass(frozen=True, slots=True, kw_only=True)
class CadArtifactDeclaration:
    """Versioned role, kind and media declaration for one exact runtime."""

    runtime: CadRuntimeIdentity
    role: CadArtifactRole
    kind: str
    media_type: str
    version: int = 1

    def __post_init__(self) -> None:
        _runtime(self.runtime)
        if type(self.role) is not CadArtifactRole:
            raise TypeError("role must be a CadArtifactRole")
        object.__setattr__(self, "kind", _text(self.kind, "kind", pattern=_NAME))
        object.__setattr__(
            self,
            "media_type",
            _text(self.media_type, "media_type", pattern=_MEDIA_TYPE),
        )
        object.__setattr__(
            self,
            "version",
            _positive_integer(self.version, "version"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CadArtifactProfile:
    """Bounded immutable artifact declarations for one CAD runtime."""

    runtime: CadRuntimeIdentity
    declarations: tuple[CadArtifactDeclaration, ...]

    def __post_init__(self) -> None:
        runtime = _runtime(self.runtime)
        snapshot = _snapshot(
            self.declarations,
            name="artifact declarations",
            limit=_MAX_CAD_COLLECTION_ITEMS,
        )
        if any(type(item) is not CadArtifactDeclaration for item in snapshot):
            raise TypeError("artifact declarations must contain only CadArtifactDeclaration values")
        declarations: tuple[CadArtifactDeclaration, ...] = snapshot  # type: ignore[assignment]
        if any(item.runtime != runtime for item in declarations):
            raise ValueError("artifact declaration runtime must match profile runtime")
        native_count = sum(item.role is CadArtifactRole.NATIVE_MODEL for item in declarations)
        if native_count != 1:
            raise ValueError("artifact profile must contain exactly one native model")
        singleton_roles = (
            CadArtifactRole.SEMANTIC_OBSERVATION,
            CadArtifactRole.SELECTOR_MAPPING,
            CadArtifactRole.PROVENANCE,
        )
        for role in singleton_roles:
            if sum(item.role is role for item in declarations) > 1:
                raise ValueError(f"artifact profile may contain at most one {role.value}")
        kinds = tuple(item.kind for item in declarations)
        if len(kinds) != len(set(kinds)):
            raise ValueError("duplicate artifact kind")
        object.__setattr__(
            self,
            "declarations",
            tuple(sorted(declarations, key=lambda item: item.kind)),
        )

    def validate_artifact(self, artifact: RuntimeArtifact) -> CadArtifactDeclaration:
        """Validate one concrete artifact against exact runtime/kind/media."""

        if type(artifact) is not RuntimeArtifact:
            raise TypeError("artifact must be a RuntimeArtifact")
        if artifact.runtime != self.runtime.runtime:
            raise ValueError("artifact runtime must match profile runtime")
        declaration = next(
            (item for item in self.declarations if item.kind == artifact.kind),
            None,
        )
        if declaration is None:
            raise ValueError("artifact kind is not declared")
        if artifact.media_type != declaration.media_type:
            raise ValueError("artifact media type must match its declaration")
        return declaration


@dataclass(frozen=True, slots=True, kw_only=True)
class NativeLocator:
    """Ephemeral native locator qualified by runtime and source revision."""

    runtime: CadRuntimeIdentity
    revision_id: str
    scheme: str
    reference: str

    def __post_init__(self) -> None:
        _runtime(self.runtime)
        object.__setattr__(self, "revision_id", _text(self.revision_id, "revision_id"))
        object.__setattr__(
            self,
            "scheme",
            _text(self.scheme, "scheme", pattern=_NAME),
        )
        object.__setattr__(self, "reference", _text(self.reference, "reference"))


@dataclass(frozen=True, slots=True, kw_only=True)
class CadSelectorEnvelope:
    """Persistent semantic selector plus an optional ephemeral native locator."""

    runtime: CadRuntimeIdentity
    semantic: SelectorV1
    native: NativeLocator | None = None

    def __post_init__(self) -> None:
        runtime = _runtime(self.runtime)
        if type(self.semantic) is not SelectorV1:
            raise TypeError("semantic must be a SelectorV1")
        semantic = SelectorV1.from_mapping(self.semantic.to_mapping())
        if self.native is not None:
            if type(self.native) is not NativeLocator:
                raise TypeError("native must be a NativeLocator or null")
            if self.native.runtime != runtime:
                raise ValueError("native locator runtime must match envelope runtime")
            if self.native.revision_id != semantic.revision_id:
                raise ValueError("native locator revision must match semantic revision")
        object.__setattr__(self, "semantic", semantic)

    def without_native(self) -> CadSelectorEnvelope:
        """Drop ephemeral native identity while preserving semantic authority."""

        return CadSelectorEnvelope(runtime=self.runtime, semantic=self.semantic)


def _request_key(requested: CadCapabilityRequest) -> tuple[str, int, str]:
    if type(requested) is RuntimeCapability:
        return (requested.name, requested.version, "")
    if type(requested) is CadRuntimeExtension:
        return (
            requested.capability.name,
            requested.capability.version,
            requested.name,
        )
    raise TypeError("requested must be a RuntimeCapability or CadRuntimeExtension")


@dataclass(frozen=True, slots=True, kw_only=True)
class CadRuntimeDescriptor:
    """Generic runtime metadata plus CAD artifact and planning contracts."""

    runtime_descriptor: RuntimeDescriptor
    artifact_profile: CadArtifactProfile
    decisions: tuple[CadCapabilityDecision, ...] = ()

    def __post_init__(self) -> None:
        if type(self.runtime_descriptor) is not RuntimeDescriptor:
            raise TypeError("runtime_descriptor must be a RuntimeDescriptor")
        identity = CadRuntimeIdentity(runtime=self.runtime_descriptor.identity)
        if type(self.artifact_profile) is not CadArtifactProfile:
            raise TypeError("artifact_profile must be a CadArtifactProfile")
        if self.artifact_profile.runtime != identity:
            raise ValueError("artifact profile runtime must match runtime descriptor")
        snapshot = _snapshot(
            self.decisions,
            name="capability decisions",
            limit=_MAX_CAD_COLLECTION_ITEMS,
        )
        if any(type(item) not in _DECISION_TYPES for item in snapshot):
            raise TypeError("capability decisions contain an unsupported decision type")
        decisions: tuple[CadCapabilityDecision, ...] = snapshot  # type: ignore[assignment]
        requests: set[CadCapabilityRequest] = set()
        for decision in decisions:
            if decision.runtime != identity:
                raise ValueError("capability decision runtime must match descriptor runtime")
            if decision.requested in requests:
                raise ValueError("duplicate capability decision request")
            requests.add(decision.requested)
            if isinstance(decision, CadNativeDecision):
                if not self.runtime_descriptor.supports(decision.requested):
                    raise ValueError("native decision capability must be declared")
            elif isinstance(decision, CadSemanticMappingDecision):
                if not self.runtime_descriptor.supports(decision.selected):
                    raise ValueError("mapping selected capability must be declared")
            elif isinstance(decision, CadExtensionDecision):
                if not self.runtime_descriptor.supports(decision.selected):
                    raise ValueError("extension selected capability must be declared")
        object.__setattr__(
            self,
            "decisions",
            tuple(
                sorted(
                    decisions,
                    key=lambda item: _request_key(item.requested),
                )
            ),
        )

    @property
    def identity(self) -> CadRuntimeIdentity:
        """Return the exact CAD runtime identity."""

        return self.artifact_profile.runtime

    def plan(self, requested: CadCapabilityRequest) -> CadCapabilityDecision:
        """Return one deterministic capability decision without side effects."""

        if type(requested) is RuntimeCapability:
            requested = _capability(requested, "requested")
        elif type(requested) is CadRuntimeExtension:
            if requested.runtime != self.identity:
                raise ValueError("extension request runtime must match descriptor runtime")
        else:
            raise TypeError("requested must be a RuntimeCapability or CadRuntimeExtension")
        configured = next(
            (item for item in self.decisions if item.requested == requested),
            None,
        )
        if configured is not None:
            return configured
        if type(requested) is RuntimeCapability and self.runtime_descriptor.supports(requested):
            return CadNativeDecision(runtime=self.identity, requested=requested)
        return CadUnsupportedDecision(
            runtime=self.identity,
            requested=requested,
            reason="capability_not_declared",
        )


@runtime_checkable
class CadRuntimeAdapter(Protocol):
    """Structural compatibility surface selected by CAD routing."""

    @property
    def runtime_descriptor(self) -> CadRuntimeDescriptor: ...

    @property
    def generation_lost(self) -> bool: ...

    def terminate_generation(self) -> None: ...

    def close_generation(self) -> None: ...


class DuplicateCadRuntimeError(ValueError):
    """Raised when two adapters declare the same exact runtime identity."""

    def __init__(self, identity: CadRuntimeIdentity) -> None:
        self.identity = _runtime(identity, "identity")
        super().__init__(f"duplicate CAD runtime identity: {identity.key}")


class UnknownCadRuntimeError(LookupError):
    """Raised when an exact CAD runtime identity is not registered."""

    def __init__(self, identity: CadRuntimeIdentity) -> None:
        self.identity = _runtime(identity, "identity")
        super().__init__(f"unknown CAD runtime identity: {identity.key}")


class CadAdapterAuthorityError(ValueError):
    """Raised when a provider exposes authority outside the adapter surface."""

    def __init__(self, name: str, token: str | None = None) -> None:
        self.name = _text(name, "authority name")
        self.token = _text(
            name if token is None else token,
            "authority token",
            pattern=_NAME,
        )
        super().__init__(f"CAD runtime adapter exposes forbidden authority: {name}")


def _identifier_tokens(name: str) -> tuple[str, ...]:
    separated = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    separated = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", separated)
    return tuple(item.lower() for item in _IDENTIFIER_TOKEN.findall(separated))


def _public_adapter_names(adapter: object) -> tuple[str, ...]:
    names: set[str] = set()
    for cls in type(adapter).__mro__:
        try:
            namespace = type.__getattribute__(cls, "__dict__")
        except Exception as exc:
            raise ValueError("adapter class namespace could not be inspected") from exc
        for name in namespace:
            if type(name) is str and not name.startswith("_"):
                names.add(name)
    try:
        namespace = object.__getattribute__(adapter, "__dict__")
    except AttributeError:
        namespace = {}
    except Exception as exc:
        raise ValueError("adapter instance namespace could not be inspected") from exc
    if type(namespace) is not dict:
        raise ValueError("adapter instance namespace could not be inspected")
    for name in namespace:
        if type(name) is str and not name.startswith("_"):
            names.add(name)
    return tuple(sorted(names))


def _validate_adapter_authority(adapter: object) -> None:
    for name in _public_adapter_names(adapter):
        if name in _FORBIDDEN_ADAPTER_AUTHORITIES:
            raise CadAdapterAuthorityError(name)
        tokens = _identifier_tokens(name)
        forbidden = next(
            (token for token in tokens if token in _FORBIDDEN_ADAPTER_AUTHORITY_TOKENS),
            None,
        )
        if forbidden is not None:
            raise CadAdapterAuthorityError(name, forbidden)


def _validate_zero_argument_hook(adapter: object, name: str) -> None:
    try:
        hook = getattr(adapter, name)
        signature = inspect.signature(hook)
        signature.bind()
    except Exception as exc:
        raise TypeError(f"{name} must be callable with zero arguments") from exc


def _adapter_descriptor(adapter: object) -> CadRuntimeDescriptor:
    _validate_adapter_authority(adapter)
    if not isinstance(adapter, CadRuntimeAdapter):
        raise TypeError("adapter must implement CadRuntimeAdapter")
    try:
        descriptor = adapter.runtime_descriptor
        generation_lost = adapter.generation_lost
    except Exception as exc:
        raise ValueError("adapter metadata could not be read") from exc
    if type(descriptor) is not CadRuntimeDescriptor:
        raise TypeError("adapter runtime_descriptor must be a CadRuntimeDescriptor")
    if type(generation_lost) is not bool:
        raise TypeError("adapter generation_lost must be a boolean")
    _validate_zero_argument_hook(adapter, "terminate_generation")
    _validate_zero_argument_hook(adapter, "close_generation")
    return descriptor


class CadRuntimeAdapterRegistry:
    """Bounded deterministic exact-version CAD adapter registry."""

    __slots__ = (
        "_adapters",
        "_descriptor_lookup",
        "_descriptors",
        "_generic_registry",
        "_identities",
        "_lookup",
    )

    def __init__(self, adapters: Iterable[CadRuntimeAdapter] = ()) -> None:
        snapshot = _snapshot(
            adapters,
            name="CAD runtime adapters",
            limit=_MAX_CAD_ADAPTERS,
        )
        records: list[tuple[CadRuntimeIdentity, CadRuntimeDescriptor, CadRuntimeAdapter]] = []
        seen: set[CadRuntimeIdentity] = set()
        for adapter in snapshot:
            descriptor = _adapter_descriptor(adapter)
            identity = descriptor.identity
            if identity in seen:
                raise DuplicateCadRuntimeError(identity)
            seen.add(identity)
            records.append((identity, descriptor, adapter))  # type: ignore[arg-type]
        records.sort(
            key=lambda item: (
                item[0].runtime.family,
                item[0].runtime.provider,
                item[0].runtime.version,
            )
        )
        self._identities = tuple(item[0] for item in records)
        self._descriptors = tuple(item[1] for item in records)
        self._adapters = tuple(item[2] for item in records)
        self._lookup: Mapping[CadRuntimeIdentity, CadRuntimeAdapter] = MappingProxyType(
            {identity: adapter for identity, _, adapter in records}
        )
        self._descriptor_lookup: Mapping[
            CadRuntimeIdentity,
            CadRuntimeDescriptor,
        ] = MappingProxyType({identity: descriptor for identity, descriptor, _ in records})
        self._generic_registry = RuntimeRegistry(
            tuple(descriptor.runtime_descriptor for descriptor in self._descriptors)
        )

    @property
    def identities(self) -> tuple[CadRuntimeIdentity, ...]:
        return self._identities

    @property
    def adapters(self) -> tuple[CadRuntimeAdapter, ...]:
        return self._adapters

    @property
    def descriptors(self) -> tuple[CadRuntimeDescriptor, ...]:
        return self._descriptors

    def descriptor(self, identity: CadRuntimeIdentity) -> CadRuntimeDescriptor:
        identity = _runtime(identity, "identity")
        try:
            self._generic_registry.lookup(identity.runtime)
            return self._descriptor_lookup[identity]
        except KeyError as exc:
            raise UnknownCadRuntimeError(identity) from exc
        except LookupError as exc:
            raise UnknownCadRuntimeError(identity) from exc

    def lookup(self, identity: CadRuntimeIdentity) -> CadRuntimeAdapter:
        identity = _runtime(identity, "identity")
        try:
            self._generic_registry.lookup(identity.runtime)
            return self._lookup[identity]
        except KeyError as exc:
            raise UnknownCadRuntimeError(identity) from exc
        except LookupError as exc:
            raise UnknownCadRuntimeError(identity) from exc

    def __iter__(self) -> Iterator[CadRuntimeAdapter]:
        return iter(self._adapters)

    def __len__(self) -> int:
        return len(self._adapters)


class NonExecutableCadDecisionError(RuntimeError):
    """Raised before selection for approximation or unsupported decisions."""

    def __init__(
        self,
        decision: CadApproximationDecision | CadUnsupportedDecision,
    ) -> None:
        if type(decision) not in {CadApproximationDecision, CadUnsupportedDecision}:
            raise TypeError("decision must be non-executable")
        self.decision = decision
        super().__init__(f"CAD capability decision is not executable: {type(decision).__name__}")


@dataclass(frozen=True, slots=True)
class CadRuntimeRouter:
    """Pure capability planning and exact adapter selection."""

    registry: CadRuntimeAdapterRegistry

    def __post_init__(self) -> None:
        if type(self.registry) is not CadRuntimeAdapterRegistry:
            raise TypeError("registry must be a CadRuntimeAdapterRegistry")

    def plan(
        self,
        identity: CadRuntimeIdentity,
        requested: CadCapabilityRequest,
    ) -> CadCapabilityDecision:
        descriptor = self.registry.descriptor(identity)
        return descriptor.plan(requested)

    def adapter_for(
        self,
        identity: CadRuntimeIdentity,
        requested: CadCapabilityRequest,
    ) -> CadRuntimeAdapter:
        decision = self.plan(identity, requested)
        if type(decision) in {CadApproximationDecision, CadUnsupportedDecision}:
            raise NonExecutableCadDecisionError(decision)  # type: ignore[arg-type]
        return self.registry.lookup(identity)


@dataclass(frozen=True, slots=True)
class CadDomainService:
    """Narrow application-facing CAD planning and adapter selection service."""

    router: CadRuntimeRouter

    def __post_init__(self) -> None:
        if type(self.router) is not CadRuntimeRouter:
            raise TypeError("router must be a CadRuntimeRouter")

    def plan(
        self,
        identity: CadRuntimeIdentity,
        requested: CadCapabilityRequest,
    ) -> CadCapabilityDecision:
        return self.router.plan(identity, requested)

    def adapter_for(
        self,
        identity: CadRuntimeIdentity,
        requested: CadCapabilityRequest,
    ) -> CadRuntimeAdapter:
        return self.router.adapter_for(identity, requested)
