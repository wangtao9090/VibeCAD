"""Tests for the backend-neutral internal CAD runtime boundary."""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import inspect
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest

from vibecad.execution.selectors import (
    EntityKind,
    Provenance,
    ProvenanceSource,
    SelectorV1,
    SemanticRole,
)
from vibecad.interaction import cad_runtime as cad_module
from vibecad.interaction.cad_runtime import (
    CAD_EXECUTE_PROGRAM_V1,
    CadAdapterAuthorityError,
    CadApproximationDecision,
    CadArtifactDeclaration,
    CadArtifactProfile,
    CadArtifactRole,
    CadDomainService,
    CadExtensionDecision,
    CadNativeDecision,
    CadRuntimeAdapter,
    CadRuntimeAdapterRegistry,
    CadRuntimeDescriptor,
    CadRuntimeExtension,
    CadRuntimeIdentity,
    CadRuntimeRouter,
    CadSelectorEnvelope,
    CadSemanticMappingDecision,
    CadUnsupportedDecision,
    DuplicateCadRuntimeError,
    NativeLocator,
    NonExecutableCadDecisionError,
    UnknownCadRuntimeError,
)
from vibecad.runtime.contracts import (
    RuntimeArtifact,
    RuntimeCapability,
    RuntimeDescriptor,
    RuntimeIdentity,
)

_OTHER_REQUEST = RuntimeCapability(name="authoring.loft", version=1)
_NATIVE_SELECTED = RuntimeCapability(name="provider.loft", version=1)
_EXTENSION_SELECTED = RuntimeCapability(name="provider.extension", version=1)


def _cad_identity(
    provider: str = "freecad",
    version: str = "1.1",
) -> CadRuntimeIdentity:
    return CadRuntimeIdentity(
        runtime=RuntimeIdentity(
            family="cad",
            provider=provider,
            version=version,
        )
    )


def _declarations(
    identity: CadRuntimeIdentity | None = None,
) -> tuple[CadArtifactDeclaration, ...]:
    identity = identity or _cad_identity()
    return (
        CadArtifactDeclaration(
            runtime=identity,
            role=CadArtifactRole.NATIVE_MODEL,
            kind="native_model",
            media_type="application/vnd.cad-native",
            version=1,
        ),
        CadArtifactDeclaration(
            runtime=identity,
            role=CadArtifactRole.EXCHANGE,
            kind="exchange_model",
            media_type="model/vnd.cad-exchange",
            version=1,
        ),
    )


def _artifact_profile(identity: CadRuntimeIdentity | None = None) -> CadArtifactProfile:
    identity = identity or _cad_identity()
    return CadArtifactProfile(runtime=identity, declarations=_declarations(identity))


def _descriptor(
    identity: CadRuntimeIdentity | None = None,
    *,
    capabilities: tuple[RuntimeCapability, ...] = (
        CAD_EXECUTE_PROGRAM_V1,
        _NATIVE_SELECTED,
        _EXTENSION_SELECTED,
    ),
    decisions: Iterable[object] = (),
) -> CadRuntimeDescriptor:
    identity = identity or _cad_identity()
    return CadRuntimeDescriptor(
        runtime_descriptor=RuntimeDescriptor(
            identity=identity.runtime,
            capabilities=capabilities,
        ),
        artifact_profile=_artifact_profile(identity),
        decisions=decisions,
    )


def _selector(revision: str = "revision_" + "2" * 32) -> SelectorV1:
    return SelectorV1(
        project_id="project_" + "1" * 32,
        revision_id=revision,
        entity_kind=EntityKind.OBJECT,
        object_id="object_" + "3" * 32,
        feature_id=None,
        object_type="Part::Feature",
        semantic_role=SemanticRole.PART,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="operation-1",
        ),
    )


class _Adapter:
    def __init__(self, descriptor: CadRuntimeDescriptor) -> None:
        self.runtime_descriptor = descriptor
        self.generation_lost = False
        self.terminate_calls = 0
        self.close_calls = 0

    def terminate_generation(self) -> None:
        self.terminate_calls += 1

    def close_generation(self) -> None:
        self.close_calls += 1


class _HostileIterable:
    def __iter__(self):
        raise RuntimeError("hostile iterable")


def test_cad_identity_wraps_only_an_exact_generic_cad_identity():
    identity = _cad_identity()

    assert identity.runtime.family == "cad"
    assert identity.key == "cad/freecad@1.1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.runtime = RuntimeIdentity(family="cad", provider="other", version="1")
    with pytest.raises(ValueError, match="family"):
        CadRuntimeIdentity(
            runtime=RuntimeIdentity(
                family="simulation",
                provider="solver",
                version="1",
            )
        )
    with pytest.raises(TypeError):
        CadRuntimeIdentity(runtime=object())


def test_extension_is_explicitly_runtime_qualified_and_namespaced():
    extension = CadRuntimeExtension(
        runtime=_cad_identity(),
        name="freecad.parametric_program",
        capability=_EXTENSION_SELECTED,
    )

    assert extension.runtime == _cad_identity()
    with pytest.raises(ValueError, match="namespace"):
        CadRuntimeExtension(
            runtime=_cad_identity(),
            name="other.parametric_program",
            capability=_EXTENSION_SELECTED,
        )


def test_all_five_decisions_are_distinct_frozen_sum_variants():
    identity = _cad_identity()
    extension = CadRuntimeExtension(
        runtime=identity,
        name="freecad.parametric_program",
        capability=_EXTENSION_SELECTED,
    )
    decisions = (
        CadNativeDecision(runtime=identity, requested=CAD_EXECUTE_PROGRAM_V1),
        CadSemanticMappingDecision(
            runtime=identity,
            requested=_OTHER_REQUEST,
            selected=_NATIVE_SELECTED,
            disclosure="Loft intent is mapped to the provider loft capability.",
        ),
        CadApproximationDecision(
            runtime=identity,
            requested=RuntimeCapability(name="authoring.surface", version=1),
            proposal="Use a disclosed faceted solid approximation.",
        ),
        CadUnsupportedDecision(
            runtime=identity,
            requested=RuntimeCapability(name="authoring.mesh", version=1),
            reason="The runtime does not declare mesh authoring.",
        ),
        CadExtensionDecision(extension=extension),
    )

    assert len({type(item) for item in decisions}) == 5
    assert decisions[0].selected == CAD_EXECUTE_PROGRAM_V1
    assert decisions[1].selected == _NATIVE_SELECTED
    assert decisions[2].executable is False
    assert decisions[3].executable is False
    assert decisions[4].selected == _EXTENSION_SELECTED
    assert decisions[4].requested is extension
    with pytest.raises(dataclasses.FrozenInstanceError):
        decisions[1].disclosure = "changed"


def test_decision_variants_reject_invalid_or_ambiguous_invariants():
    identity = _cad_identity()

    with pytest.raises(ValueError, match="different"):
        CadSemanticMappingDecision(
            runtime=identity,
            requested=_OTHER_REQUEST,
            selected=_OTHER_REQUEST,
            disclosure="Not actually a mapping.",
        )
    with pytest.raises(ValueError, match="disclosure"):
        CadSemanticMappingDecision(
            runtime=identity,
            requested=_OTHER_REQUEST,
            selected=_NATIVE_SELECTED,
            disclosure=" ",
        )
    with pytest.raises(ValueError, match="proposal"):
        CadApproximationDecision(
            runtime=identity,
            requested=_OTHER_REQUEST,
            proposal="",
        )
    with pytest.raises(ValueError, match="reason"):
        CadUnsupportedDecision(
            runtime=identity,
            requested=_OTHER_REQUEST,
            reason="",
        )


def test_descriptor_plans_native_mapping_approximation_unsupported_and_extension():
    identity = _cad_identity()
    mapped_request = RuntimeCapability(name="authoring.mapped", version=1)
    approximate_request = RuntimeCapability(name="authoring.approximate", version=1)
    rejected_request = RuntimeCapability(name="authoring.rejected", version=1)
    extension_request = CadRuntimeExtension(
        runtime=identity,
        name="freecad.parametric_program",
        capability=_EXTENSION_SELECTED,
    )
    descriptor = _descriptor(
        identity,
        decisions=(
            CadSemanticMappingDecision(
                runtime=identity,
                requested=mapped_request,
                selected=_NATIVE_SELECTED,
                disclosure="Mapped through a declared provider capability.",
            ),
            CadApproximationDecision(
                runtime=identity,
                requested=approximate_request,
                proposal="Approximate only after explicit caller approval.",
            ),
            CadUnsupportedDecision(
                runtime=identity,
                requested=rejected_request,
                reason="Explicitly unsupported.",
            ),
            CadExtensionDecision(extension=extension_request),
        ),
    )

    assert type(descriptor.plan(CAD_EXECUTE_PROGRAM_V1)) is CadNativeDecision
    assert type(descriptor.plan(mapped_request)) is CadSemanticMappingDecision
    assert type(descriptor.plan(approximate_request)) is CadApproximationDecision
    assert type(descriptor.plan(rejected_request)) is CadUnsupportedDecision
    assert type(descriptor.plan(extension_request)) is CadExtensionDecision
    unknown_extension = CadRuntimeExtension(
        runtime=identity,
        name="freecad.unconfigured_extension",
        capability=_EXTENSION_SELECTED,
    )
    unknown = descriptor.plan(unknown_extension)
    assert type(unknown) is CadUnsupportedDecision
    assert unknown.requested is unknown_extension
    assert cad_module.CadCapabilityRequest.__value__ == (RuntimeCapability | CadRuntimeExtension)
    unknown = descriptor.plan(RuntimeCapability(name="authoring.unknown", version=1))
    assert type(unknown) is CadUnsupportedDecision
    assert unknown.reason == "capability_not_declared"


def test_descriptor_rejects_undeclared_native_mapping_and_extension_selection():
    identity = _cad_identity()
    undeclared = RuntimeCapability(name="provider.undeclared", version=1)

    with pytest.raises(ValueError, match="native"):
        _descriptor(
            identity,
            capabilities=(),
            decisions=(
                CadNativeDecision(
                    runtime=identity,
                    requested=CAD_EXECUTE_PROGRAM_V1,
                ),
            ),
        )
    with pytest.raises(ValueError, match="mapping"):
        _descriptor(
            identity,
            decisions=(
                CadSemanticMappingDecision(
                    runtime=identity,
                    requested=_OTHER_REQUEST,
                    selected=undeclared,
                    disclosure="Invalid undeclared selection.",
                ),
            ),
        )
    with pytest.raises(ValueError, match="extension"):
        _descriptor(
            identity,
            decisions=(
                CadExtensionDecision(
                    extension=CadRuntimeExtension(
                        runtime=identity,
                        name="freecad.undeclared",
                        capability=undeclared,
                    ),
                ),
            ),
        )


def test_extension_wrong_runtime_is_rejected_without_native_fallback():
    identity = _cad_identity()
    other = _cad_identity("othercad")
    descriptor = _descriptor(identity)

    with pytest.raises(ValueError, match="runtime"):
        descriptor.plan(
            CadRuntimeExtension(
                runtime=other,
                name="othercad.parametric_program",
                capability=_EXTENSION_SELECTED,
            ),
        )


def test_unsupported_extension_rule_rejects_cross_runtime_request_immediately():
    with pytest.raises(ValueError, match="runtime"):
        CadUnsupportedDecision(
            runtime=_cad_identity(),
            requested=CadRuntimeExtension(
                runtime=_cad_identity("othercad"),
                name="othercad.unavailable_extension",
                capability=_EXTENSION_SELECTED,
            ),
            reason="Extension belongs to a different runtime.",
        )


def test_artifact_profile_requires_one_native_and_unique_runtime_qualified_kinds():
    identity = _cad_identity()
    native = _declarations(identity)[0]
    exchange = _declarations(identity)[1]

    with pytest.raises(ValueError, match="exactly one native"):
        CadArtifactProfile(runtime=identity, declarations=(exchange,))
    with pytest.raises(ValueError, match="exactly one native"):
        CadArtifactProfile(runtime=identity, declarations=(native, native))
    with pytest.raises(ValueError, match="duplicate artifact kind"):
        CadArtifactProfile(
            runtime=identity,
            declarations=(
                native,
                dataclasses.replace(exchange, kind=native.kind),
            ),
        )
    with pytest.raises(ValueError, match="runtime"):
        CadArtifactProfile(
            runtime=identity,
            declarations=(dataclasses.replace(native, runtime=_cad_identity("othercad")),),
        )


@pytest.mark.parametrize(
    "role",
    (
        CadArtifactRole.SEMANTIC_OBSERVATION,
        CadArtifactRole.SELECTOR_MAPPING,
        CadArtifactRole.PROVENANCE,
    ),
)
def test_artifact_profile_rejects_duplicate_singleton_evidence_roles(role):
    identity = _cad_identity()
    native = _declarations(identity)[0]

    with pytest.raises(ValueError, match="at most one"):
        CadArtifactProfile(
            runtime=identity,
            declarations=(
                native,
                CadArtifactDeclaration(
                    runtime=identity,
                    role=role,
                    kind=f"{role.value}_one",
                    media_type="application/vnd.cad-evidence",
                ),
                CadArtifactDeclaration(
                    runtime=identity,
                    role=role,
                    kind=f"{role.value}_two",
                    media_type="application/vnd.cad-evidence",
                ),
            ),
        )


def test_artifact_profile_allows_multiple_exchange_and_evidence_declarations():
    identity = _cad_identity()
    native = _declarations(identity)[0]
    declarations = (
        native,
        *(
            CadArtifactDeclaration(
                runtime=identity,
                role=role,
                kind=f"{role.value}_{index}",
                media_type="application/vnd.cad-artifact",
            )
            for role in (CadArtifactRole.EXCHANGE, CadArtifactRole.EVIDENCE)
            for index in range(2)
        ),
    )

    profile = CadArtifactProfile(runtime=identity, declarations=declarations)

    assert len(profile.declarations) == 5


def test_artifact_roles_match_the_approved_runtime_qualified_evidence_model():
    assert tuple(CadArtifactRole) == (
        CadArtifactRole.NATIVE_MODEL,
        CadArtifactRole.EXCHANGE,
        CadArtifactRole.SEMANTIC_OBSERVATION,
        CadArtifactRole.SELECTOR_MAPPING,
        CadArtifactRole.PROVENANCE,
        CadArtifactRole.EVIDENCE,
    )


def test_artifact_profile_validates_concrete_runtime_kind_and_media_exactly():
    identity = _cad_identity()
    profile = _artifact_profile(identity)
    artifact = RuntimeArtifact(
        artifact_id="native-model",
        kind="native_model",
        media_type="application/vnd.cad-native",
        digest="a" * 64,
        runtime=identity.runtime,
    )

    assert profile.validate_artifact(artifact).role is CadArtifactRole.NATIVE_MODEL
    with pytest.raises(ValueError, match="runtime"):
        profile.validate_artifact(
            dataclasses.replace(artifact, runtime=_cad_identity("othercad").runtime)
        )
    with pytest.raises(ValueError, match="kind"):
        profile.validate_artifact(dataclasses.replace(artifact, kind="unknown_model"))
    with pytest.raises(ValueError, match="media"):
        profile.validate_artifact(
            dataclasses.replace(artifact, media_type="application/octet-stream")
        )


def test_selector_envelope_revalidates_semantic_authority_and_drops_native_safely():
    identity = _cad_identity()
    selector = _selector()
    locator = NativeLocator(
        runtime=identity,
        revision_id=selector.revision_id,
        scheme="subelement",
        reference="Face3",
    )
    envelope = CadSelectorEnvelope(
        runtime=identity,
        semantic=selector,
        native=locator,
    )
    semantic_only = envelope.without_native()

    assert envelope.semantic == selector
    assert envelope.semantic is not selector
    assert envelope.native.reference == "Face3"
    assert semantic_only.semantic == selector
    assert semantic_only.native is None


def test_selector_envelope_requires_semantic_and_exact_native_revision_and_runtime():
    identity = _cad_identity()
    selector = _selector()

    with pytest.raises(TypeError, match="semantic"):
        CadSelectorEnvelope(
            runtime=identity,
            semantic=None,
            native=NativeLocator(
                runtime=identity,
                revision_id=selector.revision_id,
                scheme="subelement",
                reference="Face3",
            ),
        )
    with pytest.raises(ValueError, match="revision"):
        CadSelectorEnvelope(
            runtime=identity,
            semantic=selector,
            native=NativeLocator(
                runtime=identity,
                revision_id="revision_" + "9" * 32,
                scheme="subelement",
                reference="Face3",
            ),
        )
    with pytest.raises(ValueError, match="runtime"):
        CadSelectorEnvelope(
            runtime=identity,
            semantic=selector,
            native=NativeLocator(
                runtime=_cad_identity("othercad"),
                revision_id=selector.revision_id,
                scheme="subelement",
                reference="Face3",
            ),
        )


def test_adapter_registry_is_exact_versioned_deterministic_and_duplicate_safe():
    first = _Adapter(_descriptor(_cad_identity("othercad", "2.0")))
    second = _Adapter(_descriptor(_cad_identity("freecad", "1.1")))
    reversed_registry = CadRuntimeAdapterRegistry((first, second))
    forward_registry = CadRuntimeAdapterRegistry((second, first))

    expected = (_cad_identity("freecad", "1.1"), _cad_identity("othercad", "2.0"))
    assert reversed_registry.identities == forward_registry.identities == expected
    assert reversed_registry.lookup(_cad_identity("freecad", "1.1")) is second
    with pytest.raises(UnknownCadRuntimeError):
        reversed_registry.lookup(_cad_identity("freecad", "1.0"))
    with pytest.raises(DuplicateCadRuntimeError):
        CadRuntimeAdapterRegistry((second, second))


def test_registry_freezes_descriptor_at_admission_and_router_never_rereads_provider():
    identity = _cad_identity()
    admitted = _descriptor(identity)
    replacement = _descriptor(
        identity,
        capabilities=(),
    )

    class MutableProvider(_Adapter):
        def __init__(self) -> None:
            self._descriptors = [admitted, replacement]
            self.descriptor_reads = 0
            self.generation_lost = False
            self.terminate_calls = 0
            self.close_calls = 0

        @property
        def runtime_descriptor(self):
            index = min(self.descriptor_reads, len(self._descriptors) - 1)
            self.descriptor_reads += 1
            return self._descriptors[index]

    adapter = MutableProvider()
    registry = CadRuntimeAdapterRegistry((adapter,))
    router = CadRuntimeRouter(registry)

    assert type(router.plan(identity, CAD_EXECUTE_PROGRAM_V1)) is CadNativeDecision
    assert router.adapter_for(identity, CAD_EXECUTE_PROGRAM_V1) is adapter
    assert adapter.descriptor_reads == 1


def test_two_reversed_fixture_identities_plan_independently():
    native = _Adapter(_descriptor(_cad_identity("freecad", "1.1")))
    mapped_identity = _cad_identity("othercad", "2.0")
    mapped = _Adapter(
        _descriptor(
            mapped_identity,
            decisions=(
                CadSemanticMappingDecision(
                    runtime=mapped_identity,
                    requested=CAD_EXECUTE_PROGRAM_V1,
                    selected=_NATIVE_SELECTED,
                    disclosure="Mapped independently for the other fixture.",
                ),
            ),
        )
    )
    router = CadRuntimeRouter(CadRuntimeAdapterRegistry((mapped, native)))

    assert type(router.plan(native.runtime_descriptor.identity, CAD_EXECUTE_PROGRAM_V1)) is (
        CadNativeDecision
    )
    assert type(router.plan(mapped_identity, CAD_EXECUTE_PROGRAM_V1)) is (
        CadSemanticMappingDecision
    )
    assert router.adapter_for(native.runtime_descriptor.identity, CAD_EXECUTE_PROGRAM_V1) is native
    assert router.adapter_for(mapped_identity, CAD_EXECUTE_PROGRAM_V1) is mapped


def test_approximation_and_unsupported_fail_before_any_adapter_hook_call():
    identity = _cad_identity()
    approximation = RuntimeCapability(name="authoring.approximation", version=1)
    unsupported = RuntimeCapability(name="authoring.unsupported", version=1)
    adapter = _Adapter(
        _descriptor(
            identity,
            decisions=(
                CadApproximationDecision(
                    runtime=identity,
                    requested=approximation,
                    proposal="Explicit proposal only.",
                ),
                CadUnsupportedDecision(
                    runtime=identity,
                    requested=unsupported,
                    reason="Explicit rejection.",
                ),
            ),
        )
    )
    service = CadDomainService(CadRuntimeRouter(CadRuntimeAdapterRegistry((adapter,))))

    for request in (
        approximation,
        unsupported,
        RuntimeCapability(name="authoring.unknown", version=1),
    ):
        with pytest.raises(NonExecutableCadDecisionError) as caught:
            service.adapter_for(identity, request)
        assert caught.value.decision.executable is False
    assert adapter.terminate_calls == adapter.close_calls == 0
    assert adapter.generation_lost is False


def test_adapter_protocol_is_narrow_structural_and_rejects_authority_providers():
    descriptor = _descriptor()
    adapter = _Adapter(descriptor)

    assert isinstance(adapter, CadRuntimeAdapter)
    protocol_names = {
        name
        for name, value in vars(CadRuntimeAdapter).items()
        if not name.startswith("__") and (callable(value) or isinstance(value, property))
    }
    assert protocol_names == {
        "runtime_descriptor",
        "generation_lost",
        "terminate_generation",
        "close_generation",
    }

    class AuthorityAdapter(_Adapter):
        def commit(self):
            raise AssertionError("must never be called")

    with pytest.raises(CadAdapterAuthorityError, match="commit"):
        CadRuntimeAdapterRegistry((AuthorityAdapter(descriptor),))


@pytest.mark.parametrize(
    ("public_name", "token"),
    (
        ("commit_revision", "commit"),
        ("advance_head", "head"),
        ("accept_draft", "accept"),
        ("reject_task", "reject"),
        ("review_task", "review"),
        ("commitRevision", "commit"),
        ("advanceHead", "head"),
        ("acceptDraft", "accept"),
        ("rejectTask", "reject"),
        ("reviewTask", "review"),
    ),
)
def test_adapter_registry_rejects_authority_tokens_in_public_class_names(
    public_name,
    token,
):
    authority_type = type(
        "AuthorityProvider",
        (_Adapter,),
        {public_name: lambda self: None},
    )

    with pytest.raises(CadAdapterAuthorityError) as caught:
        CadRuntimeAdapterRegistry((authority_type(_descriptor()),))

    assert caught.value.name == public_name
    assert caught.value.token == token


def test_adapter_registry_rejects_authority_tokens_in_public_instance_names():
    adapter = _Adapter(_descriptor())
    adapter.commitRevision = lambda: None

    with pytest.raises(CadAdapterAuthorityError) as caught:
        CadRuntimeAdapterRegistry((adapter,))

    assert caught.value.name == "commitRevision"
    assert caught.value.token == "commit"


def test_adapter_registry_allows_ordinary_compatibility_and_private_capabilities():
    class TrustedCompatibilityAdapter(_Adapter):
        def __init__(self, descriptor):
            super().__init__(descriptor)
            self._store = object()
            self._lease = object()

        def open_revision(self):
            return None

    adapter = TrustedCompatibilityAdapter(_descriptor())
    registry = CadRuntimeAdapterRegistry((adapter,))

    assert registry.lookup(_cad_identity()) is adapter
    assert adapter.terminate_calls == adapter.close_calls == 0


@pytest.mark.parametrize(
    ("hook_name", "signature_kind"),
    (
        ("terminate_generation", "required_positional"),
        ("terminate_generation", "required_keyword"),
        ("close_generation", "required_positional"),
        ("close_generation", "required_keyword"),
    ),
)
def test_adapter_registry_rejects_hooks_that_cannot_bind_zero_arguments(
    hook_name,
    signature_kind,
):
    if signature_kind == "required_positional":

        def invalid_hook(self, required):
            raise AssertionError("must never be called")

    else:

        def invalid_hook(self, *, required):
            raise AssertionError("must never be called")

    adapter_type = type("InvalidHookAdapter", (_Adapter,), {hook_name: invalid_hook})

    with pytest.raises(TypeError, match=hook_name):
        CadRuntimeAdapterRegistry((adapter_type(_descriptor()),))


def test_adapter_registry_accepts_zero_bindable_hook_forms_without_calling_them():
    class ZeroBindableAdapter(_Adapter):
        def terminate_generation(self, optional=None):
            self.terminate_calls += 1

        def close_generation(self, *args, **kwargs):
            self.close_calls += 1

    adapter = ZeroBindableAdapter(_descriptor())
    registry = CadRuntimeAdapterRegistry((adapter,))

    assert registry.lookup(_cad_identity()) is adapter
    assert adapter.terminate_calls == adapter.close_calls == 0


@pytest.mark.parametrize("target", ("declarations", "decisions", "adapters"))
def test_public_cad_iterables_are_bounded_without_trusting_length_hints(target):
    script = f"""
from collections.abc import Sequence
from vibecad.interaction.cad_runtime import (
    CAD_EXECUTE_PROGRAM_V1, CadArtifactDeclaration, CadArtifactProfile,
    CadArtifactRole, CadNativeDecision, CadRuntimeAdapterRegistry,
    CadRuntimeDescriptor, CadRuntimeIdentity,
)
from vibecad.runtime.contracts import RuntimeCapability, RuntimeDescriptor, RuntimeIdentity

class Infinite(Sequence):
    def __init__(self, value):
        self.value = value
    def __getitem__(self, index):
        return self.value
    def __len__(self):
        return 1

identity = CadRuntimeIdentity(
    runtime=RuntimeIdentity(family="cad", provider="freecad", version="1.1")
)
declaration = CadArtifactDeclaration(
    runtime=identity, role=CadArtifactRole.NATIVE_MODEL, kind="native_model",
    media_type="application/vnd.cad-native", version=1,
)
profile = CadArtifactProfile(runtime=identity, declarations=(declaration,))
generic = RuntimeDescriptor(
    identity=identity.runtime,
    capabilities=(CAD_EXECUTE_PROGRAM_V1,),
)
decision = CadNativeDecision(runtime=identity, requested=CAD_EXECUTE_PROGRAM_V1)
descriptor = CadRuntimeDescriptor(
    runtime_descriptor=generic, artifact_profile=profile, decisions=(),
)
class Adapter:
    runtime_descriptor = descriptor
    generation_lost = False
    def terminate_generation(self): pass
    def close_generation(self): pass
adapter = Adapter()

target = {target!r}
try:
    if target == "declarations":
        CadArtifactProfile(runtime=identity, declarations=Infinite(declaration))
    elif target == "decisions":
        CadRuntimeDescriptor(
            runtime_descriptor=generic,
            artifact_profile=profile,
            decisions=Infinite(decision),
        )
    else:
        CadRuntimeAdapterRegistry(Infinite(adapter))
except ValueError:
    print("bounded-cad-iterable: PASS")
else:
    raise RuntimeError("endless CAD iterable was accepted")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=2.5,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bounded-cad-iterable: PASS"


def test_hostile_cad_iterables_fail_closed():
    with pytest.raises(ValueError, match="enumerated"):
        CadArtifactProfile(runtime=_cad_identity(), declarations=_HostileIterable())
    with pytest.raises(ValueError, match="enumerated"):
        _descriptor(decisions=_HostileIterable())
    with pytest.raises(ValueError, match="enumerated"):
        CadRuntimeAdapterRegistry(_HostileIterable())


def test_internal_execute_program_capability_is_exact_and_not_a_public_projection():
    assert CAD_EXECUTE_PROGRAM_V1 == RuntimeCapability(
        name="authoring.execute_program",
        version=1,
    )
    assert not hasattr(cad_module, "public_tool_specs")


def test_cad_runtime_imports_and_adapter_authority_surface_are_pure():
    source_path = Path(cad_module.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")

    internal_imports = {name for name in imports if name.startswith("vibecad.")}
    assert internal_imports == {
        "vibecad.execution.selectors",
        "vibecad.runtime.contracts",
        "vibecad.runtime.registry",
    }
    assert "assert " not in source_path.read_text(encoding="utf-8")
    forbidden = {
        "store",
        "lease",
        "task",
        "revision",
        "review",
        "accept",
        "reject",
        "commit",
        "head",
        "public_tool",
    }
    protocol_source = inspect.getsource(CadRuntimeAdapter).lower()
    assert not any(name in protocol_source for name in forbidden)


def test_interaction_package_initializer_remains_byte_identical():
    path = Path(cad_module.__file__).with_name("__init__.py")

    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "f1e9b6e50b2042c09dff60d024a6fbf53ee09f2507b6b66dfa0423de9ae776a5"
    )
