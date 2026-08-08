"""Narrow read-only access to process-local provider coordinate evidence.

This composition is intentionally separate from :class:`VisualProviderBinding`.
It cannot start, retry, persist, adopt, or publish anything.  A provider restart
may make the evidence unavailable; callers must treat that as unknown rather
than invoking the model again implicitly.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import StrEnum

from vibecad.runtime.contracts import RuntimeInvocation, RuntimeResult
from vibecad.visual.evidence import BoundVisualEvidence
from vibecad.visual.provider import (
    VisualProviderBinding,
    VisualProviderExecutionReceipt,
    VisualProviderOutput,
    validate_visual_provider_result,
    visual_provider_input_digest,
)
from vibecad.visual.reconstruction import VisualObservation


class VisualEvidenceProviderErrorCode(StrEnum):
    INVALID_COMPOSITION = "invalid_composition"
    INVALID_INPUT = "invalid_input"
    RESULT_MISMATCH = "result_mismatch"
    READ_FAILURE = "read_failure"


class VisualEvidenceProviderError(ValueError):
    def __init__(self, code: VisualEvidenceProviderErrorCode) -> None:
        if type(code) is not VisualEvidenceProviderErrorCode:
            raise TypeError("code must be an exact VisualEvidenceProviderErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: VisualEvidenceProviderErrorCode) -> None:
    raise VisualEvidenceProviderError(code)


def _class_method(provider_type: type, name: str) -> object | None:
    for owner in type.__getattribute__(provider_type, "__mro__"):
        namespace = type.__getattribute__(owner, "__dict__")
        if name in namespace:
            return namespace[name]
    return None


def _validate_reader(provider: object) -> None:
    method = _class_method(type(provider), "get_bound_evidence")
    if method is None or not inspect.isfunction(method) or inspect.iscoroutinefunction(method):
        _fail(VisualEvidenceProviderErrorCode.INVALID_COMPOSITION)
    try:
        parameters = tuple(
            inspect.signature(method, follow_wrapped=False, eval_str=False).parameters.values()
        )
    except (TypeError, ValueError):
        _fail(VisualEvidenceProviderErrorCode.INVALID_COMPOSITION)
    if tuple((item.name, item.kind) for item in parameters) != (
        ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
        ("invocation_id", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ) or any(item.default is not inspect.Parameter.empty for item in parameters):
        _fail(VisualEvidenceProviderErrorCode.INVALID_COMPOSITION)


@dataclass(frozen=True, slots=True, kw_only=True)
class VisualEvidenceProviderBinding:
    """Validate and read one provider's optional process-local evidence."""

    provider_binding: VisualProviderBinding

    def __post_init__(self) -> None:
        if type(self.provider_binding) is not VisualProviderBinding:
            _fail(VisualEvidenceProviderErrorCode.INVALID_COMPOSITION)
        _validate_reader(self.provider_binding.provider)

    def retrieve(
        self,
        invocation: RuntimeInvocation,
        result: RuntimeResult,
    ) -> BoundVisualEvidence | None:
        if type(invocation) is not RuntimeInvocation or type(result) is not RuntimeResult:
            _fail(VisualEvidenceProviderErrorCode.INVALID_INPUT)
        try:
            visual_provider_input_digest(
                invocation,
                runtime_profile=self.provider_binding.runtime_profile,
            )
        except Exception:
            _fail(VisualEvidenceProviderErrorCode.INVALID_INPUT)
        try:
            checked = validate_visual_provider_result(
                invocation,
                result,
                runtime_profile=self.provider_binding.runtime_profile,
            )
        except Exception:
            _fail(VisualEvidenceProviderErrorCode.RESULT_MISMATCH)
        try:
            output = VisualProviderOutput.from_mapping(checked.output)
            observation = (
                output.value
                if type(output.value) is VisualObservation
                else output.value.observation
            )
            assert type(observation) is VisualObservation
            assert checked.provenance is not None
            receipt = VisualProviderExecutionReceipt.from_mapping(
                checked.provenance.details["execution"]
            )
        except (AssertionError, KeyError, TypeError, ValueError):
            _fail(VisualEvidenceProviderErrorCode.RESULT_MISMATCH)
        try:
            value = self.provider_binding.provider.get_bound_evidence(invocation.invocation_id)
        except Exception:
            _fail(VisualEvidenceProviderErrorCode.READ_FAILURE)
        if value is None:
            return None
        if type(value) is not BoundVisualEvidence:
            _fail(VisualEvidenceProviderErrorCode.RESULT_MISMATCH)
        if (
            value.reconstruction_id != observation.reconstruction_id
            or value.generation != observation.generation
            or value.image_set_id != observation.image_set_id
            or value.image_set_manifest_sha256 != observation.image_set_manifest_sha256
            or value.observation_id != observation.id
            or value.observation_digest != observation.digest
            or value.image_batch_manifest_sha256 != receipt.image_batch_sha256
        ):
            _fail(VisualEvidenceProviderErrorCode.RESULT_MISMATCH)
        return value


__all__ = [
    "VisualEvidenceProviderBinding",
    "VisualEvidenceProviderError",
    "VisualEvidenceProviderErrorCode",
]
