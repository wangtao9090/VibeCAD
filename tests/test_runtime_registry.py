"""Tests for deterministic registration of generic runtime descriptors."""

from __future__ import annotations

import subprocess
import sys

import pytest

from vibecad.runtime.contracts import (
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeIdentity,
)
from vibecad.runtime.registry import (
    DuplicateRuntimeError,
    RuntimeRegistry,
    UnknownRuntimeError,
)


def _descriptor(
    family: str,
    provider: str,
    version: str = "1.0",
    *,
    capabilities: tuple[str, ...] = ("execute",),
) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        identity=RuntimeIdentity(
            family=family,
            provider=provider,
            version=version,
        ),
        capabilities=tuple(RuntimeCapability(name=item, version=1) for item in capabilities),
    )


def test_registry_orders_descriptors_by_full_runtime_identity_independent_of_input_order():
    descriptors = (
        _descriptor("simulation", "calculix"),
        _descriptor("cad", "freecad", "1.1"),
        _descriptor("cad", "freecad", "1.0"),
        _descriptor("cad", "onshape"),
    )

    first = RuntimeRegistry(descriptors)
    second = RuntimeRegistry(reversed(descriptors))

    expected = (
        RuntimeIdentity(family="cad", provider="freecad", version="1.0"),
        RuntimeIdentity(family="cad", provider="freecad", version="1.1"),
        RuntimeIdentity(family="cad", provider="onshape", version="1.0"),
        RuntimeIdentity(family="simulation", provider="calculix", version="1.0"),
    )
    assert first.identities == second.identities == expected
    assert tuple(item.identity for item in first) == expected


def test_registry_lookup_and_capability_support_use_exact_identity_and_version():
    freecad = _descriptor(
        "cad",
        "freecad",
        "1.1",
        capabilities=("authoring.create_box", "inspection.measure"),
    )
    registry = RuntimeRegistry((freecad,))
    exact = RuntimeIdentity(family="cad", provider="freecad", version="1.1")

    assert registry.lookup(exact) is freecad
    assert registry.supports(
        exact,
        RuntimeCapability(name="authoring.create_box", version=1),
    )
    assert not registry.supports(
        exact,
        RuntimeCapability(name="authoring.create_box", version=2),
    )

    with pytest.raises(UnknownRuntimeError) as caught:
        registry.lookup(RuntimeIdentity(family="cad", provider="freecad", version="1.0"))
    assert caught.value.identity.version == "1.0"


def test_registry_rejects_duplicate_full_identity_without_silent_overwrite():
    first = _descriptor("cad", "freecad", "1.1", capabilities=("execute",))
    duplicate = _descriptor("cad", "freecad", "1.1", capabilities=("inspect",))

    with pytest.raises(DuplicateRuntimeError) as caught:
        RuntimeRegistry((first, duplicate))

    assert caught.value.identity == first.identity
    assert str(caught.value) == "duplicate runtime identity: cad/freecad@1.1"


def test_registry_snapshots_caller_owned_iterables():
    descriptors = [_descriptor("cad", "freecad")]
    registry = RuntimeRegistry(descriptors)
    descriptors.append(_descriptor("cad", "onshape"))

    assert len(registry) == 1
    assert registry.identities == (
        RuntimeIdentity(family="cad", provider="freecad", version="1.0"),
    )


def test_registry_has_an_explicit_finite_descriptor_bound():
    descriptors = tuple(_descriptor("cad", "provider", f"1.{index}") for index in range(257))

    with pytest.raises(ValueError, match="runtime descriptors"):
        RuntimeRegistry(descriptors)


def test_registry_stops_enumerating_an_endless_public_iterable():
    script = """
from vibecad.runtime.contracts import (
    RuntimeCapability, RuntimeDescriptor, RuntimeIdentity,
)
from vibecad.runtime.registry import RuntimeRegistry

descriptor = RuntimeDescriptor(
    identity=RuntimeIdentity(family="cad", provider="freecad", version="1.1"),
    capabilities=(RuntimeCapability(name="execute", version=1),),
)

def endless():
    while True:
        yield descriptor

try:
    RuntimeRegistry(endless())
except ValueError:
    print("bounded-runtime-registry: PASS")
else:
    raise RuntimeError("endless registry iterable was accepted")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bounded-runtime-registry: PASS"
