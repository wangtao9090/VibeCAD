"""Bounded conformance checks for admitted CAD runtime adapter snapshots."""

from __future__ import annotations

from dataclasses import dataclass

from vibecad.interaction.cad_runtime import (
    CadAdapterAuthorityError,
    CadExtensionDecision,
    CadNativeDecision,
    CadRuntimeAdapterRegistry,
    CadRuntimeDescriptor,
    CadRuntimeIdentity,
    CadRuntimeRouter,
    CadSelectorEnvelope,
    CadSemanticMappingDecision,
    NonExecutableCadDecisionError,
)
from vibecad.runtime.conformance import (
    ConformanceReport,
    _FindingCollector,
    _prepare_cases,
)
from vibecad.runtime.contracts import RuntimeArtifact, RuntimeCapability

_MAX_ARTIFACTS = 32
_EXECUTABLE_DECISIONS = (
    CadNativeDecision,
    CadSemanticMappingDecision,
    CadExtensionDecision,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CadRuntimeAdmissionCase:
    """One adapter instance to admit through the existing CAD registry."""

    case_id: str
    adapter: object


@dataclass(frozen=True, slots=True, kw_only=True)
class CadRuntimeConformanceCase:
    """One pure check over an already-admitted CAD registry snapshot."""

    case_id: str
    registry: CadRuntimeAdapterRegistry
    identity: CadRuntimeIdentity
    executable_request: object
    unsupported_request: object
    artifacts: tuple[RuntimeArtifact, ...]
    selector: object


def _snapshot_artifacts(
    artifacts: object,
    *,
    case_id: str,
    findings: _FindingCollector,
) -> tuple[object, ...]:
    try:
        if isinstance(artifacts, (str, bytes, bytearray)):
            raise TypeError
        iterator = iter(artifacts)  # type: ignore[arg-type]
    except Exception:
        findings.add(
            "cad_artifact_collection_invalid",
            case_id,
            "artifacts",
        )
        return ()

    snapshot: list[object] = []
    try:
        for index in range(_MAX_ARTIFACTS + 1):
            try:
                artifact = next(iterator)
            except StopIteration:
                return tuple(snapshot)
            if index == _MAX_ARTIFACTS:
                findings.add(
                    "cad_artifact_limit_exceeded",
                    case_id,
                    "artifacts",
                )
                return ()
            snapshot.append(artifact)
    except Exception:
        findings.add(
            "cad_artifact_collection_invalid",
            case_id,
            "artifacts",
        )
        return ()
    return ()


def _evaluate_artifacts(
    artifacts: object,
    descriptor: CadRuntimeDescriptor | None,
    *,
    case_id: str,
    findings: _FindingCollector,
) -> None:
    snapshot = _snapshot_artifacts(
        artifacts,
        case_id=case_id,
        findings=findings,
    )
    for artifact in snapshot:
        if type(artifact) is not RuntimeArtifact:
            findings.add("cad_artifact_invalid", case_id, "artifacts")
            continue
        if descriptor is None:
            continue
        if artifact.runtime != descriptor.identity.runtime:
            findings.add(
                "cad_artifact_runtime_mismatch",
                case_id,
                "artifacts",
            )
            continue
        declaration = next(
            (
                item
                for item in descriptor.artifact_profile.declarations
                if item.kind == artifact.kind
            ),
            None,
        )
        if declaration is None:
            findings.add(
                "cad_artifact_kind_undeclared",
                case_id,
                "artifacts",
            )
        elif artifact.media_type != declaration.media_type:
            findings.add(
                "cad_artifact_media_mismatch",
                case_id,
                "artifacts",
            )


def _evaluate_executable_request(
    case: CadRuntimeConformanceCase,
    descriptor: CadRuntimeDescriptor,
    *,
    case_id: str,
    findings: _FindingCollector,
) -> None:
    router = CadRuntimeRouter(case.registry)
    try:
        decision = router.plan(case.identity, case.executable_request)
    except Exception:
        findings.add(
            "cad_executable_rejected",
            case_id,
            "executable_request",
        )
        return
    if type(decision) not in _EXECUTABLE_DECISIONS or not decision.executable:
        findings.add(
            "cad_executable_rejected",
            case_id,
            "executable_request",
        )
        return
    selected = decision.selected
    if type(selected) is not RuntimeCapability or not descriptor.runtime_descriptor.supports(
        selected
    ):
        findings.add(
            "cad_undeclared_capability",
            case_id,
            "executable_request",
        )
        return
    if type(case.executable_request) is RuntimeCapability and (
        type(decision) is not CadNativeDecision or selected != case.executable_request
    ):
        findings.add(
            "cad_executable_not_exact",
            case_id,
            "executable_request",
        )
        return
    try:
        expected = case.registry.lookup(case.identity)
        routed = router.adapter_for(case.identity, case.executable_request)
    except Exception:
        findings.add(
            "cad_executable_rejected",
            case_id,
            "executable_request",
        )
        return
    if routed is not expected:
        findings.add(
            "cad_route_identity_mismatch",
            case_id,
            "executable_request",
        )


def _evaluate_unsupported_request(
    case: CadRuntimeConformanceCase,
    router: CadRuntimeRouter,
    *,
    case_id: str,
    findings: _FindingCollector,
) -> None:
    try:
        decision = router.plan(case.identity, case.unsupported_request)
    except Exception:
        findings.add(
            "cad_unsupported_request_invalid",
            case_id,
            "unsupported_request",
        )
        return
    if getattr(decision, "executable", True):
        findings.add(
            "cad_unsupported_accepted",
            case_id,
            "unsupported_request",
        )
        return
    try:
        routed = router.adapter_for(case.identity, case.unsupported_request)
    except NonExecutableCadDecisionError as exc:
        if exc.decision != decision:
            findings.add(
                "cad_unsupported_route_wrong_error",
                case_id,
                "unsupported_request",
            )
    except Exception:
        findings.add(
            "cad_unsupported_route_wrong_error",
            case_id,
            "unsupported_request",
        )
    else:
        del routed
        findings.add(
            "cad_unsupported_route_returned",
            case_id,
            "unsupported_request",
        )


def _evaluate_snapshot_case(
    case_id: str,
    case: CadRuntimeConformanceCase,
    findings: _FindingCollector,
) -> None:
    registry = case.registry
    identity = case.identity
    descriptor: CadRuntimeDescriptor | None = None
    if type(registry) is not CadRuntimeAdapterRegistry:
        findings.add("cad_registry_invalid", case_id, "registry")
    if type(identity) is not CadRuntimeIdentity:
        findings.add("cad_identity_invalid", case_id, "identity")
    elif type(registry) is CadRuntimeAdapterRegistry:
        if identity not in registry.identities:
            findings.add(
                "cad_registry_identity_mismatch",
                case_id,
                "identity",
            )
        else:
            descriptor = registry.descriptor(identity)

    if descriptor is not None:
        router = CadRuntimeRouter(case.registry)
        _evaluate_executable_request(
            case,
            descriptor,
            case_id=case_id,
            findings=findings,
        )
        _evaluate_unsupported_request(
            case,
            router,
            case_id=case_id,
            findings=findings,
        )
    _evaluate_artifacts(
        case.artifacts,
        descriptor,
        case_id=case_id,
        findings=findings,
    )
    if type(case.selector) is not CadSelectorEnvelope:
        findings.add(
            "cad_selector_envelope_required",
            case_id,
            "selector",
        )
    elif type(identity) is CadRuntimeIdentity and case.selector.runtime != identity:
        findings.add(
            "cad_selector_runtime_mismatch",
            case_id,
            "selector",
        )


def evaluate_cad_runtime_admission(cases: object) -> ConformanceReport:
    """Delegate each bounded adapter admission once to the existing registry."""

    prepared, initial = _prepare_cases(
        cases,
        case_type=CadRuntimeAdmissionCase,
        code_prefix="cad_",
    )
    findings = _FindingCollector("cad_finding_limit_exceeded")
    findings.extend(initial)
    for case_id, value in prepared:
        case: CadRuntimeAdmissionCase = value  # type: ignore[assignment]
        try:
            CadRuntimeAdapterRegistry((case.adapter,))
        except CadAdapterAuthorityError:
            findings.add(
                "cad_admission_authority",
                case_id,
                "adapter",
            )
        except Exception:
            findings.add(
                "cad_admission_failed",
                case_id,
                "adapter",
            )
    return findings.report()


def evaluate_cad_runtime_conformance(cases: object) -> ConformanceReport:
    """Evaluate already-admitted snapshots without provider metadata rereads."""

    prepared, initial = _prepare_cases(
        cases,
        case_type=CadRuntimeConformanceCase,
        code_prefix="cad_",
    )
    findings = _FindingCollector("cad_finding_limit_exceeded")
    findings.extend(initial)
    for case_id, value in prepared:
        _evaluate_snapshot_case(
            case_id,
            value,  # type: ignore[arg-type]
            findings,
        )
    return findings.report()
