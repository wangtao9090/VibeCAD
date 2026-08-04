"""Visual-only composition over the generic runtime lifecycle contracts.

This module admits one exact local deterministic provider contract.  It does
not implement a provider, read image bytes, touch durable storage, create a CAD
Task, register a CAD adapter, or perform network I/O.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from vibecad.runtime.contracts import (
    RuntimeArtifact,
    RuntimeBudget,
    RuntimeCapability,
    RuntimeControlPort,
    RuntimeDescriptor,
    RuntimeDiagnostic,
    RuntimeIdentity,
    RuntimeInvocation,
    RuntimeLifecycleState,
    RuntimeProvenance,
    RuntimeResult,
    RuntimeResultPort,
)
from vibecad.runtime.registry import RuntimeRegistry
from vibecad.visual.reconstruction import (
    ReconstructionProposal,
    VisualObservation,
    visual_invocation_identity,
)

VISUAL_PROVIDER_SCHEMA_VERSION = 1
VISUAL_PROVIDER_MODEL = "deterministic_visual_fixture"
VISUAL_PROVIDER_MODEL_VERSION = "1"
VISUAL_PROVIDER_EXECUTION_PROFILE = "local_only"
VISUAL_INTERNAL_CORRELATION_SEMANTICS = "visual_internal_not_cad_task"

VISUAL_PROVIDER_IDENTITY = RuntimeIdentity(
    family="visual",
    provider="deterministic_fake",
    version="1.0",
)
VISUAL_OBSERVE_V1 = RuntimeCapability(name="visual.observe", version=1)
VISUAL_PROVIDER_DESCRIPTOR = RuntimeDescriptor(
    identity=VISUAL_PROVIDER_IDENTITY,
    capabilities=(VISUAL_OBSERVE_V1,),
    execution_profiles=(VISUAL_PROVIDER_EXECUTION_PROFILE,),
    metadata={
        "model": VISUAL_PROVIDER_MODEL,
        "model_version": VISUAL_PROVIDER_MODEL_VERSION,
        "network": False,
        "correlation_semantics": VISUAL_INTERNAL_CORRELATION_SEMANTICS,
    },
)

_INPUT_DIGEST_DOMAIN = b"vibecad-visual-provider-input-v1\0"
_OUTPUT_DIGEST_DOMAIN = b"vibecad-visual-provider-output-v1\0"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_ANSWER_DIGESTS = 128
_MAX_CANONICAL_BYTES = 1024 * 1024
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_IDENTIFIER_TOKEN = re.compile(r"[A-Za-z0-9]+")
_FORBIDDEN_AUTHORITY_TOKENS = frozenset(
    {
        "accept",
        "commit",
        "head",
        "lease",
        "reject",
        "review",
        "revision",
        "store",
        "task",
    }
)
_FORBIDDEN_EXACT_AUTHORITIES = frozenset(
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
_PROVIDER_SIGNATURES = {
    "start": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "get_status": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "cancel": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("reason", inspect.Parameter.KEYWORD_ONLY),
    ),
    "reconcile": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "health": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("identity", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
    "get_result": (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ),
}


class VisualProviderErrorCode(StrEnum):
    """Stable fail-closed categories for visual runtime composition."""

    INVALID_PROVIDER = "invalid_provider"
    FORBIDDEN_AUTHORITY = "forbidden_authority"
    DESCRIPTOR_MISMATCH = "descriptor_mismatch"
    INVALID_INVOCATION = "invalid_invocation"
    INVALID_OUTPUT = "invalid_output"
    RESULT_MISMATCH = "result_mismatch"


class VisualProviderError(ValueError):
    """Bounded composition failure that does not reflect provider values."""

    def __init__(self, code: VisualProviderErrorCode, subject: str) -> None:
        if type(code) is not VisualProviderErrorCode:
            raise TypeError("code must be an exact VisualProviderErrorCode")
        if (
            type(subject) is not str
            or not subject
            or len(subject) > 64
            or re.fullmatch(r"[a-z0-9_.-]+", subject) is None
        ):
            raise ValueError("subject must be bounded contract text")
        self.code = code
        self.subject = subject
        super().__init__(code.value)


def _raise(code: VisualProviderErrorCode, subject: str) -> None:
    raise VisualProviderError(code, subject)


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        _raise(VisualProviderErrorCode.INVALID_OUTPUT, "canonical_json")
    if len(raw) > _MAX_CANONICAL_BYTES:
        _raise(VisualProviderErrorCode.INVALID_OUTPUT, "canonical_bytes")
    return raw


def _checked_digest(
    value: object,
    subject: str,
    *,
    code: VisualProviderErrorCode = VisualProviderErrorCode.INVALID_INVOCATION,
) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _raise(code, subject)
    return value


def _clarification_answer_digests(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_ANSWER_DIGESTS:
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "answer_digests")
    result = tuple(_checked_digest(item, "answer_digest") for item in value)
    if len(result) != len(set(result)):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "answer_digests")
    return tuple(sorted(result))


def visual_runtime_correlation_id(reconstruction_id: str, generation: int) -> str:
    """Return an explicit internal correlation value, never a CAD Task ID."""

    try:
        visual_invocation_identity(reconstruction_id, generation, "image_set_" + "0" * 32, "0" * 64)
    except (TypeError, ValueError):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "correlation")
    return f"visual-internal:{reconstruction_id}:generation-{generation}"


def _input_body(
    *,
    invocation_id: str,
    reconstruction_id: str,
    generation: int,
    image_set_id: str,
    image_set_manifest_sha256: str,
    clarification_answer_digests: tuple[str, ...],
    budget: RuntimeBudget,
    deadline_ms: int,
) -> dict[str, object]:
    correlation_id = visual_runtime_correlation_id(reconstruction_id, generation)
    return {
        "schema_version": VISUAL_PROVIDER_SCHEMA_VERSION,
        "runtime": {
            "family": VISUAL_PROVIDER_IDENTITY.family,
            "provider": VISUAL_PROVIDER_IDENTITY.provider,
            "version": VISUAL_PROVIDER_IDENTITY.version,
        },
        "capability": {
            "name": VISUAL_OBSERVE_V1.name,
            "version": VISUAL_OBSERVE_V1.version,
        },
        "execution_profile": VISUAL_PROVIDER_EXECUTION_PROFILE,
        "model": VISUAL_PROVIDER_MODEL,
        "model_version": VISUAL_PROVIDER_MODEL_VERSION,
        "network": False,
        "correlation_semantics": VISUAL_INTERNAL_CORRELATION_SEMANTICS,
        "correlation_id": correlation_id,
        "invocation_id": invocation_id,
        "reconstruction_id": reconstruction_id,
        "generation": generation,
        "image_set_id": image_set_id,
        "image_set_manifest_sha256": image_set_manifest_sha256,
        "clarification_answer_digests": clarification_answer_digests,
        "budget": {
            "max_elapsed_ms": budget.max_elapsed_ms,
            "max_memory_bytes": budget.max_memory_bytes,
            "max_output_bytes": budget.max_output_bytes,
        },
        "deadline_ms": deadline_ms,
    }


def _input_digest(body: Mapping[str, object]) -> str:
    return hashlib.sha256(_INPUT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()


def _image_set_artifact(image_set_id: str, manifest_sha256: str) -> RuntimeArtifact:
    return RuntimeArtifact(
        artifact_id=image_set_id,
        kind="visual_image_set_manifest",
        media_type="application/vnd.vibecad.image-set+json",
        digest=manifest_sha256,
        runtime=VISUAL_PROVIDER_IDENTITY,
        metadata={
            "schema_version": VISUAL_PROVIDER_SCHEMA_VERSION,
            "role": "sealed_image_set_manifest",
            "network": False,
        },
    )


def build_visual_provider_invocation(
    *,
    reconstruction_id: str,
    generation: int,
    image_set_id: str,
    image_set_manifest_sha256: str,
    clarification_answer_digests: tuple[str, ...] = (),
    budget: RuntimeBudget,
    deadline_ms: int,
) -> RuntimeInvocation:
    """Build the one strict local visual invocation for a reconstruction generation."""

    if type(budget) is not RuntimeBudget:
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "budget")
    answers = _clarification_answer_digests(clarification_answer_digests)
    manifest_digest = _checked_digest(image_set_manifest_sha256, "manifest_digest")
    try:
        invocation_id = visual_invocation_identity(
            reconstruction_id,
            generation,
            image_set_id,
            manifest_digest,
        )
    except (TypeError, ValueError):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "visual_identity")
    body = _input_body(
        invocation_id=invocation_id,
        reconstruction_id=reconstruction_id,
        generation=generation,
        image_set_id=image_set_id,
        image_set_manifest_sha256=manifest_digest,
        clarification_answer_digests=answers,
        budget=budget,
        deadline_ms=deadline_ms,
    )
    payload = body | {"input_digest": _input_digest(body)}
    try:
        return RuntimeInvocation(
            invocation_id=invocation_id,
            owner_id=reconstruction_id,
            task_id=visual_runtime_correlation_id(reconstruction_id, generation),
            runtime=VISUAL_PROVIDER_IDENTITY,
            capability=VISUAL_OBSERVE_V1,
            budget=budget,
            deadline_ms=deadline_ms,
            input_artifacts=(_image_set_artifact(image_set_id, manifest_digest),),
            payload=payload,
            execution_profile=VISUAL_PROVIDER_EXECUTION_PROFILE,
        )
    except (TypeError, ValueError):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "runtime_invocation")


def visual_provider_input_digest(value: object) -> str:
    """Validate one exact visual invocation and return its bound input digest."""

    if type(value) is not RuntimeInvocation:
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "invocation")
    invocation = value
    if (
        invocation.runtime != VISUAL_PROVIDER_IDENTITY
        or invocation.capability != VISUAL_OBSERVE_V1
        or invocation.execution_profile != VISUAL_PROVIDER_EXECUTION_PROFILE
        or invocation.input_revision is not None
        or len(invocation.input_artifacts) != 1
    ):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "runtime_binding")
    payload = invocation.payload
    expected_fields = {
        "schema_version",
        "runtime",
        "capability",
        "execution_profile",
        "model",
        "model_version",
        "network",
        "correlation_semantics",
        "correlation_id",
        "invocation_id",
        "reconstruction_id",
        "generation",
        "image_set_id",
        "image_set_manifest_sha256",
        "clarification_answer_digests",
        "budget",
        "deadline_ms",
        "input_digest",
    }
    if set(payload) != expected_fields:
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "payload_fields")
    reconstruction_id = payload["reconstruction_id"]
    generation = payload["generation"]
    image_set_id = payload["image_set_id"]
    manifest_digest = _checked_digest(payload["image_set_manifest_sha256"], "manifest_digest")
    answers = _clarification_answer_digests(payload["clarification_answer_digests"])
    try:
        expected_invocation_id = visual_invocation_identity(
            reconstruction_id,
            generation,
            image_set_id,
            manifest_digest,
        )
        correlation_id = visual_runtime_correlation_id(reconstruction_id, generation)
    except (TypeError, ValueError):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "visual_identity")
    if (
        invocation.invocation_id != expected_invocation_id
        or invocation.owner_id != reconstruction_id
        or invocation.task_id != correlation_id
    ):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "correlation")
    try:
        expected_artifact = _image_set_artifact(image_set_id, manifest_digest)
    except (TypeError, ValueError):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "input_artifact")
    if invocation.input_artifacts != (expected_artifact,):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "input_artifact")
    body = _input_body(
        invocation_id=expected_invocation_id,
        reconstruction_id=reconstruction_id,
        generation=generation,
        image_set_id=image_set_id,
        image_set_manifest_sha256=manifest_digest,
        clarification_answer_digests=answers,
        budget=invocation.budget,
        deadline_ms=invocation.deadline_ms,
    )
    supplied_digest = _checked_digest(payload["input_digest"], "input_digest")
    expected_digest = _input_digest(body)
    if not hmac.compare_digest(supplied_digest, expected_digest):
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "input_digest")
    if payload != body | {"input_digest": expected_digest}:
        _raise(VisualProviderErrorCode.INVALID_INVOCATION, "payload_binding")
    return expected_digest


class VisualProviderOutputKind(StrEnum):
    OBSERVATION = "observation"
    PROPOSAL = "proposal"


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualProviderOutput:
    """Strict successful provider output bound to one exact input digest."""

    input_digest: str
    value: VisualObservation | ReconstructionProposal
    output_digest: str = ""
    schema_version: int = VISUAL_PROVIDER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != VISUAL_PROVIDER_SCHEMA_VERSION:
            _raise(VisualProviderErrorCode.INVALID_OUTPUT, "schema_version")
        input_digest = _checked_digest(
            self.input_digest,
            "input_digest",
            code=VisualProviderErrorCode.INVALID_OUTPUT,
        )
        if type(self.value) not in {VisualObservation, ReconstructionProposal}:
            _raise(VisualProviderErrorCode.INVALID_OUTPUT, "value")
        object.__setattr__(self, "input_digest", input_digest)
        expected = hashlib.sha256(
            _OUTPUT_DIGEST_DOMAIN + _canonical_json(self._body_mapping())
        ).hexdigest()
        if self.output_digest:
            supplied = _checked_digest(
                self.output_digest,
                "output_digest",
                code=VisualProviderErrorCode.INVALID_OUTPUT,
            )
            if not hmac.compare_digest(supplied, expected):
                _raise(VisualProviderErrorCode.INVALID_OUTPUT, "output_digest")
        object.__setattr__(self, "output_digest", expected)

    @property
    def kind(self) -> VisualProviderOutputKind:
        if type(self.value) is VisualObservation:
            return VisualProviderOutputKind.OBSERVATION
        return VisualProviderOutputKind.PROPOSAL

    def _body_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "input_digest": self.input_digest,
            "value": self.value.to_mapping(),
        }

    def to_mapping(self) -> dict[str, object]:
        return self._body_mapping() | {"output_digest": self.output_digest}

    @classmethod
    def from_mapping(cls, value: object) -> VisualProviderOutput:
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version",
            "kind",
            "input_digest",
            "value",
            "output_digest",
        }:
            _raise(VisualProviderErrorCode.INVALID_OUTPUT, "output_fields")
        kind = value["kind"]
        nested = _thaw(value["value"])
        try:
            if kind == VisualProviderOutputKind.OBSERVATION.value:
                decoded: VisualObservation | ReconstructionProposal = (
                    VisualObservation.from_mapping(nested)
                )
            elif kind == VisualProviderOutputKind.PROPOSAL.value:
                decoded = ReconstructionProposal.from_mapping(nested)
            else:
                _raise(VisualProviderErrorCode.INVALID_OUTPUT, "output_kind")
            return cls(
                schema_version=value["schema_version"],
                input_digest=value["input_digest"],
                value=decoded,
                output_digest=value["output_digest"],
            )
        except VisualProviderError:
            raise
        except (TypeError, ValueError):
            _raise(VisualProviderErrorCode.INVALID_OUTPUT, "output_value")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw(item) for item in value]
    return value


def visual_provider_output_digest(value: object) -> str:
    """Validate a strict visual output envelope and return its digest."""

    if type(value) is not VisualProviderOutput:
        _raise(VisualProviderErrorCode.INVALID_OUTPUT, "output")
    return value.output_digest


def _provenance_details(input_digest: str, output_digest: str | None) -> dict[str, object]:
    return {
        "schema_version": VISUAL_PROVIDER_SCHEMA_VERSION,
        "input_digest": input_digest,
        "output_digest": output_digest,
        "model": VISUAL_PROVIDER_MODEL,
        "model_version": VISUAL_PROVIDER_MODEL_VERSION,
        "network": False,
        "correlation_semantics": VISUAL_INTERNAL_CORRELATION_SEMANTICS,
    }


def _provenance(
    invocation: RuntimeInvocation,
    input_digest: str,
    output_digest: str | None,
) -> RuntimeProvenance:
    return RuntimeProvenance(
        runtime=VISUAL_PROVIDER_IDENTITY,
        invocation_id=invocation.invocation_id,
        input_artifact_ids=tuple(item.artifact_id for item in invocation.input_artifacts),
        details=_provenance_details(input_digest, output_digest),
    )


def _output_artifact(output: VisualProviderOutput) -> RuntimeArtifact:
    if type(output.value) is VisualObservation:
        kind = "visual_observation"
        media_type = "application/vnd.vibecad.visual-observation+json"
    else:
        kind = "reconstruction_proposal"
        media_type = "application/vnd.vibecad.reconstruction-proposal+json"
    return RuntimeArtifact(
        artifact_id=output.value.id,
        kind=kind,
        media_type=media_type,
        digest=output.output_digest,
        runtime=VISUAL_PROVIDER_IDENTITY,
        metadata={
            "schema_version": VISUAL_PROVIDER_SCHEMA_VERSION,
            "semantic_digest": output.value.digest,
            "model": VISUAL_PROVIDER_MODEL,
            "model_version": VISUAL_PROVIDER_MODEL_VERSION,
            "network": False,
        },
    )


def _validate_output_correlation(
    invocation: RuntimeInvocation,
    value: VisualObservation | ReconstructionProposal,
) -> None:
    observation = value if type(value) is VisualObservation else value.observation
    payload = invocation.payload
    if (
        observation.invocation_id != invocation.invocation_id
        or observation.reconstruction_id != payload["reconstruction_id"]
        or observation.generation != payload["generation"]
        or observation.image_set_id != payload["image_set_id"]
        or observation.image_set_manifest_sha256 != payload["image_set_manifest_sha256"]
    ):
        _raise(VisualProviderErrorCode.RESULT_MISMATCH, "visual_correlation")
    if type(value) is ReconstructionProposal:
        allowed_answer_digests = frozenset(payload["clarification_answer_digests"])
        if any(
            answer.digest not in allowed_answer_digests for answer in value.clarification_answers
        ):
            _raise(VisualProviderErrorCode.RESULT_MISMATCH, "clarification_answers")


def build_visual_provider_success_result(
    invocation: RuntimeInvocation,
    value: VisualObservation | ReconstructionProposal,
) -> RuntimeResult:
    """Build the canonical successful generic result for one strict visual value."""

    input_digest = visual_provider_input_digest(invocation)
    if type(value) not in {VisualObservation, ReconstructionProposal}:
        _raise(VisualProviderErrorCode.INVALID_OUTPUT, "value")
    _validate_output_correlation(invocation, value)
    output = VisualProviderOutput(input_digest=input_digest, value=value)
    return RuntimeResult(
        invocation_id=invocation.invocation_id,
        runtime=VISUAL_PROVIDER_IDENTITY,
        state=RuntimeLifecycleState.SUCCEEDED,
        artifacts=(_output_artifact(output),),
        provenance=_provenance(invocation, input_digest, output.output_digest),
        output=output.to_mapping(),
    )


def validate_visual_provider_result(
    invocation: RuntimeInvocation,
    result: object,
) -> RuntimeResult:
    """Fail closed unless a terminal result is exactly bound to the invocation."""

    input_digest = visual_provider_input_digest(invocation)
    if type(result) is not RuntimeResult:
        _raise(VisualProviderErrorCode.RESULT_MISMATCH, "result")
    if (
        result.invocation_id != invocation.invocation_id
        or result.runtime != VISUAL_PROVIDER_IDENTITY
    ):
        _raise(VisualProviderErrorCode.RESULT_MISMATCH, "correlation")
    if result.state is RuntimeLifecycleState.SUCCEEDED:
        output = VisualProviderOutput.from_mapping(result.output)
        if output.input_digest != input_digest:
            _raise(VisualProviderErrorCode.RESULT_MISMATCH, "input_digest")
        expected = build_visual_provider_success_result(invocation, output.value)
        if result != expected:
            _raise(VisualProviderErrorCode.RESULT_MISMATCH, "success_result")
        return result
    if result.state not in {RuntimeLifecycleState.FAILED, RuntimeLifecycleState.CANCELLED}:
        _raise(VisualProviderErrorCode.RESULT_MISMATCH, "terminal_state")
    if (
        result.artifacts
        or result.output
        or result.evidence
        or result.provenance != _provenance(invocation, input_digest, None)
        or (result.state is RuntimeLifecycleState.FAILED and not result.diagnostics)
    ):
        _raise(VisualProviderErrorCode.RESULT_MISMATCH, "terminal_result")
    return result


class VisualProviderPort(RuntimeControlPort, RuntimeResultPort, Protocol):
    """Exact structural provider shape admitted by visual-only composition."""

    @property
    def runtime_descriptor(self) -> RuntimeDescriptor: ...


def _identifier_tokens(name: str) -> tuple[str, ...]:
    separated = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", name)
    separated = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", separated)
    return tuple(item.lower() for item in _IDENTIFIER_TOKEN.findall(separated))


def _provider_authority_names(provider: object) -> tuple[str, ...]:
    names: set[str] = set()
    try:
        classes = type.__getattribute__(type(provider), "__mro__")
        for owner in classes:
            namespace = type.__getattribute__(owner, "__dict__")
            names.update(
                name for name in namespace if type(name) is str and not name.startswith("__")
            )
        try:
            instance_namespace = object.__getattribute__(provider, "__dict__")
        except AttributeError:
            instance_namespace = {}
        if type(instance_namespace) is not dict:
            raise TypeError
        names.update(
            name for name in instance_namespace if type(name) is str and not name.startswith("__")
        )
    except Exception:
        _raise(VisualProviderErrorCode.INVALID_PROVIDER, "namespace")
    return tuple(sorted(names))


def _validate_provider_authority(provider: object) -> None:
    for name in _provider_authority_names(provider):
        tokens = _identifier_tokens(name)
        forbidden = next(
            (token for token in tokens if token in _FORBIDDEN_AUTHORITY_TOKENS),
            None,
        )
        public_tool = any(
            left == "public" and right == "tool"
            for left, right in zip(tokens, tokens[1:], strict=False)
        )
        if name in _FORBIDDEN_EXACT_AUTHORITIES or forbidden is not None or public_tool:
            _raise(VisualProviderErrorCode.FORBIDDEN_AUTHORITY, "provider_authority")


def _class_namespace_value(provider_type: type, name: str) -> object | None:
    for owner in type.__getattribute__(provider_type, "__mro__"):
        namespace = type.__getattribute__(owner, "__dict__")
        if name in namespace:
            return namespace[name]
    return None


def _validate_provider_shape(provider: object) -> None:
    provider_type = type(provider)
    descriptor_property = _class_namespace_value(provider_type, "runtime_descriptor")
    if type(descriptor_property) is not property:
        _raise(VisualProviderErrorCode.INVALID_PROVIDER, "runtime_descriptor")
    for name, expected in _PROVIDER_SIGNATURES.items():
        value = _class_namespace_value(provider_type, name)
        if value is None or not inspect.isfunction(value) or inspect.iscoroutinefunction(value):
            _raise(VisualProviderErrorCode.INVALID_PROVIDER, "provider_method")
        try:
            parameters = tuple(
                inspect.signature(value, follow_wrapped=False, eval_str=False).parameters.values()
            )
        except (TypeError, ValueError):
            _raise(VisualProviderErrorCode.INVALID_PROVIDER, "provider_method")
        actual = tuple((parameter.name, parameter.kind) for parameter in parameters)
        if actual != expected or any(
            parameter.default is not inspect.Parameter.empty for parameter in parameters
        ):
            _raise(VisualProviderErrorCode.INVALID_PROVIDER, "provider_method")


def _admit_provider(provider: object) -> tuple[RuntimeDescriptor, RuntimeRegistry]:
    # Authority is inspected statically before descriptor/property access.
    _validate_provider_authority(provider)
    _validate_provider_shape(provider)
    try:
        descriptor = object.__getattribute__(provider, "runtime_descriptor")
    except Exception:
        _raise(VisualProviderErrorCode.INVALID_PROVIDER, "runtime_descriptor")
    if type(descriptor) is not RuntimeDescriptor or descriptor != VISUAL_PROVIDER_DESCRIPTOR:
        _raise(VisualProviderErrorCode.DESCRIPTOR_MISMATCH, "runtime_descriptor")
    if descriptor.identity.family == "cad" or descriptor.metadata.get("network") is not False:
        _raise(VisualProviderErrorCode.DESCRIPTOR_MISMATCH, "runtime_descriptor")
    registry = RuntimeRegistry((descriptor,))
    if registry.lookup(VISUAL_PROVIDER_IDENTITY) != descriptor:
        _raise(VisualProviderErrorCode.DESCRIPTOR_MISMATCH, "runtime_registry")
    return descriptor, registry


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualProviderBinding:
    """One exact descriptor/control/result binding for local visual execution."""

    provider: VisualProviderPort
    descriptor: RuntimeDescriptor = field(init=False)
    registry: RuntimeRegistry = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        descriptor, registry = _admit_provider(self.provider)
        object.__setattr__(self, "descriptor", descriptor)
        object.__setattr__(self, "registry", registry)

    @property
    def control(self) -> RuntimeControlPort:
        return self.provider

    @property
    def results(self) -> RuntimeResultPort:
        return self.provider

    def retrieve_result(self, invocation: RuntimeInvocation) -> RuntimeResult | None:
        """Perform exactly one non-waiting result read and validate any result."""

        visual_provider_input_digest(invocation)
        result = self.provider.get_result(invocation.invocation_id)
        if result is None:
            return None
        return validate_visual_provider_result(invocation, result)


def build_visual_provider_failure_result(
    invocation: RuntimeInvocation,
    diagnostic: RuntimeDiagnostic,
) -> RuntimeResult:
    """Build a definitive failed receipt without a visual output artifact."""

    if type(diagnostic) is not RuntimeDiagnostic:
        _raise(VisualProviderErrorCode.INVALID_OUTPUT, "diagnostic")
    input_digest = visual_provider_input_digest(invocation)
    return RuntimeResult(
        invocation_id=invocation.invocation_id,
        runtime=VISUAL_PROVIDER_IDENTITY,
        state=RuntimeLifecycleState.FAILED,
        provenance=_provenance(invocation, input_digest, None),
        diagnostics=(diagnostic,),
    )
