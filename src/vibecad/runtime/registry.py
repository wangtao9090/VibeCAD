"""Deterministic in-process registry for generic runtime descriptors."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType

from vibecad.runtime.contracts import (
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeIdentity,
)

_MAX_RUNTIME_DESCRIPTORS = 256


def _snapshot_descriptors(
    descriptors: Iterable[RuntimeDescriptor],
) -> tuple[RuntimeDescriptor, ...]:
    try:
        iterator = iter(descriptors)
    except Exception as exc:
        raise ValueError("runtime descriptors could not be enumerated") from exc
    snapshot: list[object] = []
    for index in range(_MAX_RUNTIME_DESCRIPTORS + 1):
        try:
            descriptor = next(iterator)
        except StopIteration:
            break
        except Exception as exc:
            raise ValueError("runtime descriptors could not be enumerated") from exc
        if index == _MAX_RUNTIME_DESCRIPTORS:
            raise ValueError("runtime descriptors exceed the maximum of 256")
        snapshot.append(descriptor)
    if any(type(item) is not RuntimeDescriptor for item in snapshot):
        raise TypeError("descriptors must contain only RuntimeDescriptor values")
    return tuple(snapshot)  # type: ignore[return-value]


class DuplicateRuntimeError(ValueError):
    """Raised when registration would silently replace a runtime identity."""

    def __init__(self, identity: RuntimeIdentity) -> None:
        if type(identity) is not RuntimeIdentity:
            raise TypeError("identity must be a RuntimeIdentity")
        self.identity = identity
        super().__init__(f"duplicate runtime identity: {identity.key}")


class UnknownRuntimeError(LookupError):
    """Raised when an exact versioned runtime identity is not registered."""

    def __init__(self, identity: RuntimeIdentity) -> None:
        if type(identity) is not RuntimeIdentity:
            raise TypeError("identity must be a RuntimeIdentity")
        self.identity = identity
        super().__init__(f"unknown runtime identity: {identity.key}")


class RuntimeRegistry:
    """Immutable deterministic registry keyed by full runtime identity."""

    __slots__ = ("_descriptors", "_identities", "_lookup")

    def __init__(self, descriptors: Iterable[RuntimeDescriptor] = ()) -> None:
        snapshot = _snapshot_descriptors(descriptors)

        by_identity: dict[RuntimeIdentity, RuntimeDescriptor] = {}
        for descriptor in snapshot:
            if descriptor.identity in by_identity:
                raise DuplicateRuntimeError(descriptor.identity)
            by_identity[descriptor.identity] = descriptor

        identities = tuple(
            sorted(
                by_identity,
                key=lambda item: (item.family, item.provider, item.version),
            )
        )
        self._identities = identities
        self._descriptors = tuple(by_identity[identity] for identity in identities)
        self._lookup: Mapping[RuntimeIdentity, RuntimeDescriptor] = MappingProxyType(
            {identity: by_identity[identity] for identity in identities}
        )

    @property
    def identities(self) -> tuple[RuntimeIdentity, ...]:
        """Return identities in stable family/provider/version order."""

        return self._identities

    @property
    def descriptors(self) -> tuple[RuntimeDescriptor, ...]:
        """Return the immutable ordered descriptor snapshot."""

        return self._descriptors

    def lookup(self, identity: RuntimeIdentity) -> RuntimeDescriptor:
        """Resolve one exact identity without version fallback."""

        if type(identity) is not RuntimeIdentity:
            raise TypeError("identity must be a RuntimeIdentity")
        try:
            return self._lookup[identity]
        except KeyError as exc:
            raise UnknownRuntimeError(identity) from exc

    def supports(
        self,
        identity: RuntimeIdentity,
        capability: RuntimeCapability,
    ) -> bool:
        """Return whether an exact registered runtime declares a capability."""

        return self.lookup(identity).supports(capability)

    def __iter__(self) -> Iterator[RuntimeDescriptor]:
        return iter(self._descriptors)

    def __len__(self) -> int:
        return len(self._descriptors)
