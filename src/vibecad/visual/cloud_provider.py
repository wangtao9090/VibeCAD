"""Single-effect cloud visual provider over an injected transport.

This adapter owns no credentials, HTTP client, filesystem path, CAD, Task, or
revision authority.  A host supplies one read-only sealed-image reader and one
provider-specific transport.  Each durable invocation can cause at most one
transport call in a process; exceptions and uncertain outcomes become UNKNOWN
and reconciliation never replays the call.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from vibecad.runtime.contracts import (
    RuntimeDiagnostic,
    RuntimeHealth,
    RuntimeHealthState,
    RuntimeIdentity,
    RuntimeInvocation,
    RuntimeLifecycleState,
    RuntimeResult,
    RuntimeStatus,
)
from vibecad.visual.contracts import ImageSet
from vibecad.visual.evidence import (
    MAX_EVIDENCE_FEATURES,
    MAX_EVIDENCE_TOTAL_POINTS,
    BoundVisualEvidence,
    ProviderFeatureEvidence,
    bind_visual_evidence,
)
from vibecad.visual.provider import (
    VisualProviderExecutionReceipt,
    VisualProviderRuntimeProfile,
    build_visual_provider_failure_result,
    build_visual_provider_success_result,
    visual_provider_input_digest,
)
from vibecad.visual.provider_images import (
    ProviderImageBatch,
    VisualProviderCapabilityProfile,
    prepare_provider_image_batch,
)
from vibecad.visual.reconstruction import ReconstructionProposal, VisualObservation

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_INVOCATION_ID = re.compile(r"^visual_invocation_[0-9a-f]{32}$")
_REQUEST_DIGEST_DOMAIN = b"vibecad-cloud-visual-request-v1\0"
_MAX_REQUEST_MANIFEST_BYTES = 128 * 1024
_MAX_REASON_BYTES = 512
_TRANSPORT_SIGNATURE = (
    ("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("request", inspect.Parameter.POSITIONAL_OR_KEYWORD),
    ("timeout_ms", inspect.Parameter.KEYWORD_ONLY),
)


class CloudVisualProviderErrorCode(StrEnum):
    INVALID_COMPOSITION = "invalid_composition"
    INVALID_INVOCATION = "invalid_invocation"
    INVOCATION_CONFLICT = "invocation_conflict"
    INVALID_TRANSPORT = "invalid_transport"
    INVALID_REASON = "invalid_reason"


class CloudVisualProviderError(ValueError):
    def __init__(self, code: CloudVisualProviderErrorCode) -> None:
        if type(code) is not CloudVisualProviderErrorCode:
            raise TypeError("code must be an exact CloudVisualProviderErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: CloudVisualProviderErrorCode) -> None:
    raise CloudVisualProviderError(code)


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail(CloudVisualProviderErrorCode.INVALID_INVOCATION)
    if len(raw) > _MAX_REQUEST_MANIFEST_BYTES:
        _fail(CloudVisualProviderErrorCode.INVALID_INVOCATION)
    return raw


def _checked_invocation_id(value: object) -> str:
    if type(value) is not str or _INVOCATION_ID.fullmatch(value) is None:
        _fail(CloudVisualProviderErrorCode.INVALID_INVOCATION)
    return value


def _unknown(invocation_id: str, runtime: RuntimeIdentity) -> RuntimeStatus:
    return RuntimeStatus(
        invocation_id=invocation_id,
        runtime=runtime,
        state=RuntimeLifecycleState.UNKNOWN,
    )


class CloudVisualOutcomeKind(StrEnum):
    SUCCEEDED = "succeeded"
    DEFINITIVE_FAILURE = "definitive_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, kw_only=True)
class CloudVisualTransportOutcome:
    kind: CloudVisualOutcomeKind
    value: VisualObservation | ReconstructionProposal | None = None
    diagnostic: RuntimeDiagnostic | None = None
    execution_receipt: VisualProviderExecutionReceipt | None = None
    feature_evidence: tuple[ProviderFeatureEvidence, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not CloudVisualOutcomeKind:
            _fail(CloudVisualProviderErrorCode.INVALID_TRANSPORT)
        if type(self.feature_evidence) is not tuple:
            _fail(CloudVisualProviderErrorCode.INVALID_TRANSPORT)
        if len(self.feature_evidence) > MAX_EVIDENCE_FEATURES or any(
            type(item) is not ProviderFeatureEvidence for item in self.feature_evidence
        ):
            _fail(CloudVisualProviderErrorCode.INVALID_TRANSPORT)
        if sum(len(item.points) for item in self.feature_evidence) > MAX_EVIDENCE_TOTAL_POINTS:
            _fail(CloudVisualProviderErrorCode.INVALID_TRANSPORT)
        if self.kind is CloudVisualOutcomeKind.SUCCEEDED:
            valid = type(self.value) in {VisualObservation, ReconstructionProposal}
            valid = (
                valid
                and self.diagnostic is None
                and type(self.execution_receipt) is VisualProviderExecutionReceipt
            )
        elif self.kind is CloudVisualOutcomeKind.DEFINITIVE_FAILURE:
            valid = (
                self.value is None
                and type(self.diagnostic) is RuntimeDiagnostic
                and self.execution_receipt is None
                and not self.feature_evidence
            )
        else:
            valid = (
                self.value is None
                and self.diagnostic is None
                and self.execution_receipt is None
                and not self.feature_evidence
            )
        if not valid:
            _fail(CloudVisualProviderErrorCode.INVALID_TRANSPORT)


@dataclass(frozen=True, slots=True, kw_only=True)
class CloudVisualRequest:
    invocation: RuntimeInvocation
    input_sha256: str
    image_batch: ProviderImageBatch
    request_sha256: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.invocation) is not RuntimeInvocation
            or type(self.input_sha256) is not str
            or _DIGEST.fullmatch(self.input_sha256) is None
            or type(self.image_batch) is not ProviderImageBatch
        ):
            _fail(CloudVisualProviderErrorCode.INVALID_INVOCATION)
        body = {
            "invocation_id": self.invocation.invocation_id,
            "input_sha256": self.input_sha256,
            "image_batch_sha256": self.image_batch.manifest_sha256,
            "data_policy_profile": self.image_batch.profile.data_policy_profile,
            "detail": self.image_batch.profile.detail.value,
        }
        expected = hashlib.sha256(_REQUEST_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()
        if self.request_sha256:
            if (
                type(self.request_sha256) is not str
                or _DIGEST.fullmatch(self.request_sha256) is None
                or not hmac.compare_digest(self.request_sha256, expected)
            ):
                _fail(CloudVisualProviderErrorCode.INVALID_INVOCATION)
        object.__setattr__(self, "request_sha256", expected)

    @property
    def image_parts(self):
        """Return ephemeral derivative parts, including bytes, to the transport."""

        return self.image_batch.parts

    def to_manifest_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "invocation_id": self.invocation.invocation_id,
                "input_sha256": self.input_sha256,
                "image_batch_sha256": self.image_batch.manifest_sha256,
                "data_policy_profile": self.image_batch.profile.data_policy_profile,
                "detail": self.image_batch.profile.detail.value,
                "request_sha256": self.request_sha256,
            }
        )


class CloudVisualTransport(Protocol):
    def invoke(
        self,
        request: CloudVisualRequest,
        *,
        timeout_ms: int,
    ) -> CloudVisualTransportOutcome: ...


def _validate_transport(value: object) -> None:
    method = None
    for owner in type.__getattribute__(type(value), "__mro__"):
        namespace = type.__getattribute__(owner, "__dict__")
        if "invoke" in namespace:
            method = namespace["invoke"]
            break
    if method is None or not inspect.isfunction(method) or inspect.iscoroutinefunction(method):
        _fail(CloudVisualProviderErrorCode.INVALID_COMPOSITION)
    try:
        parameters = tuple(
            inspect.signature(method, follow_wrapped=False, eval_str=False).parameters.values()
        )
    except (TypeError, ValueError):
        _fail(CloudVisualProviderErrorCode.INVALID_COMPOSITION)
    if tuple((item.name, item.kind) for item in parameters) != _TRANSPORT_SIGNATURE or any(
        item.default is not inspect.Parameter.empty for item in parameters
    ):
        _fail(CloudVisualProviderErrorCode.INVALID_COMPOSITION)


class CloudVisualProvider:
    """Synchronous one-call adapter with restart-safe UNKNOWN semantics."""

    __slots__ = (
        "_image_profile",
        "_image_reader",
        "_bound_evidence",
        "_input_digests",
        "_results",
        "_runtime_profile",
        "_statuses",
        "_transport",
        "_transport_count",
    )

    def __init__(
        self,
        *,
        runtime_profile: VisualProviderRuntimeProfile,
        image_profile: VisualProviderCapabilityProfile,
        image_reader: object,
        transport: CloudVisualTransport,
    ) -> None:
        if (
            type(runtime_profile) is not VisualProviderRuntimeProfile
            or not runtime_profile.network
            or type(image_profile) is not VisualProviderCapabilityProfile
            or runtime_profile.identity.provider != image_profile.provider
            or runtime_profile.model != image_profile.model
            or runtime_profile.model_version != image_profile.model_version
            or not callable(image_reader)
        ):
            _fail(CloudVisualProviderErrorCode.INVALID_COMPOSITION)
        _validate_transport(transport)
        self._runtime_profile = runtime_profile
        self._image_profile = image_profile
        self._image_reader = image_reader
        self._transport = transport
        self._bound_evidence: dict[str, BoundVisualEvidence | None] = {}
        self._input_digests: dict[str, str] = {}
        self._statuses: dict[str, RuntimeStatus] = {}
        self._results: dict[str, RuntimeResult | None] = {}
        self._transport_count = 0

    @property
    def runtime_descriptor(self):
        return self._runtime_profile.descriptor

    @property
    def transport_count(self) -> int:
        return self._transport_count

    def _publish(
        self,
        invocation_id: str,
        input_digest: str,
        status: RuntimeStatus,
        result: RuntimeResult | None,
        bound_evidence: BoundVisualEvidence | None = None,
    ) -> RuntimeStatus:
        self._input_digests[invocation_id] = input_digest
        self._statuses[invocation_id] = status
        self._results[invocation_id] = result
        self._bound_evidence[invocation_id] = bound_evidence
        return status

    def _local_failure(
        self,
        invocation: RuntimeInvocation,
        input_digest: str,
    ) -> RuntimeStatus:
        diagnostic = RuntimeDiagnostic(
            code="provider.image_preparation_failed",
            message="Provider image preparation failed before transport.",
            retryable=False,
        )
        result = build_visual_provider_failure_result(
            invocation,
            diagnostic,
            runtime_profile=self._runtime_profile,
        )
        return self._publish(
            invocation.invocation_id,
            input_digest,
            RuntimeStatus(
                invocation_id=invocation.invocation_id,
                runtime=self._runtime_profile.identity,
                state=RuntimeLifecycleState.FAILED,
                diagnostics=result.diagnostics,
            ),
            result,
        )

    def start(self, invocation: RuntimeInvocation) -> RuntimeStatus:
        try:
            input_digest = visual_provider_input_digest(
                invocation,
                runtime_profile=self._runtime_profile,
            )
        except Exception:
            _fail(CloudVisualProviderErrorCode.INVALID_INVOCATION)
        invocation_id = invocation.invocation_id
        previous = self._input_digests.get(invocation_id)
        if previous is not None:
            if not hmac.compare_digest(previous, input_digest):
                _fail(CloudVisualProviderErrorCode.INVOCATION_CONFLICT)
            return self._statuses[invocation_id]

        payload = invocation.payload
        try:
            image_set, images = self._image_reader(
                payload["image_set_id"],
                payload["image_set_manifest_sha256"],
            )
            if type(image_set) is not ImageSet or type(images) is not tuple:
                raise TypeError
            batch = prepare_provider_image_batch(
                image_set=image_set,
                normalized_images=images,
                profile=self._image_profile,
                detail_crops=(),
            )
            request = CloudVisualRequest(
                invocation=invocation,
                input_sha256=input_digest,
                image_batch=batch,
            )
        except Exception:
            return self._local_failure(invocation, input_digest)

        self._transport_count += 1
        try:
            outcome = self._transport.invoke(
                request,
                timeout_ms=self._image_profile.transport_timeout_ms,
            )
        except Exception:
            outcome = CloudVisualTransportOutcome(kind=CloudVisualOutcomeKind.UNKNOWN)
        if type(outcome) is not CloudVisualTransportOutcome:
            outcome = CloudVisualTransportOutcome(kind=CloudVisualOutcomeKind.UNKNOWN)

        if outcome.kind is CloudVisualOutcomeKind.UNKNOWN:
            return self._publish(
                invocation_id,
                input_digest,
                _unknown(invocation_id, self._runtime_profile.identity),
                None,
            )
        try:
            if outcome.kind is CloudVisualOutcomeKind.DEFINITIVE_FAILURE:
                assert outcome.diagnostic is not None
                result = build_visual_provider_failure_result(
                    invocation,
                    outcome.diagnostic,
                    runtime_profile=self._runtime_profile,
                )
                state = RuntimeLifecycleState.FAILED
            else:
                assert outcome.value is not None
                assert outcome.execution_receipt is not None
                if not hmac.compare_digest(
                    outcome.execution_receipt.request_sha256,
                    request.request_sha256,
                ) or not hmac.compare_digest(
                    outcome.execution_receipt.image_batch_sha256,
                    batch.manifest_sha256,
                ):
                    raise ValueError
                observation = (
                    outcome.value
                    if type(outcome.value) is VisualObservation
                    else outcome.value.observation
                )
                bound_evidence = bind_visual_evidence(
                    observation=observation,
                    image_set=image_set,
                    image_batch=batch,
                    features=outcome.feature_evidence,
                )
                result = build_visual_provider_success_result(
                    invocation,
                    outcome.value,
                    runtime_profile=self._runtime_profile,
                    execution_receipt=outcome.execution_receipt,
                )
                state = RuntimeLifecycleState.SUCCEEDED
        except Exception:
            return self._publish(
                invocation_id,
                input_digest,
                _unknown(invocation_id, self._runtime_profile.identity),
                None,
            )
        return self._publish(
            invocation_id,
            input_digest,
            RuntimeStatus(
                invocation_id=invocation_id,
                runtime=self._runtime_profile.identity,
                state=state,
                diagnostics=result.diagnostics,
            ),
            result,
            bound_evidence if state is RuntimeLifecycleState.SUCCEEDED else None,
        )

    def get_status(self, invocation_id: str) -> RuntimeStatus:
        checked = _checked_invocation_id(invocation_id)
        return self._statuses.get(
            checked,
            _unknown(checked, self._runtime_profile.identity),
        )

    def cancel(self, invocation_id: str, *, reason: str) -> RuntimeStatus:
        checked = _checked_invocation_id(invocation_id)
        if (
            type(reason) is not str
            or not reason
            or reason.strip() != reason
            or len(reason.encode("utf-8")) > _MAX_REASON_BYTES
        ):
            _fail(CloudVisualProviderErrorCode.INVALID_REASON)
        return self._statuses.get(
            checked,
            _unknown(checked, self._runtime_profile.identity),
        )

    def reconcile(self, invocation_id: str) -> RuntimeStatus:
        checked = _checked_invocation_id(invocation_id)
        return self._statuses.get(
            checked,
            _unknown(checked, self._runtime_profile.identity),
        )

    def health(self, identity: RuntimeIdentity) -> RuntimeHealth:
        if type(identity) is not RuntimeIdentity or identity != self._runtime_profile.identity:
            _fail(CloudVisualProviderErrorCode.INVALID_INVOCATION)
        return RuntimeHealth(runtime=identity, state=RuntimeHealthState.UNKNOWN)

    def get_result(self, invocation_id: str) -> RuntimeResult | None:
        return self._results.get(_checked_invocation_id(invocation_id))

    def get_bound_evidence(self, invocation_id: str) -> BoundVisualEvidence | None:
        """Return process-local advisory evidence without replaying the provider."""

        return self._bound_evidence.get(_checked_invocation_id(invocation_id))


__all__ = [
    "CloudVisualOutcomeKind",
    "CloudVisualProvider",
    "CloudVisualProviderError",
    "CloudVisualProviderErrorCode",
    "CloudVisualRequest",
    "CloudVisualTransport",
    "CloudVisualTransportOutcome",
]
