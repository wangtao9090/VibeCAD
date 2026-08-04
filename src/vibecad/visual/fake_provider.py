"""Deterministic in-memory visual provider used only by tests and demos.

Fixtures are selected exclusively by the validated visual ``input_digest``.
The provider never reads an image, opens a file, touches durable application
state, invokes a callback, or performs network I/O.  Its in-memory lifecycle
table is deliberately empty after construction, so a restarted instance can
only report an unrecognized invocation as ``UNKNOWN``; reconciliation never
replays ``start``.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

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
from vibecad.visual.provider import (
    VISUAL_PROVIDER_DESCRIPTOR,
    VISUAL_PROVIDER_IDENTITY,
    build_visual_provider_failure_result,
    build_visual_provider_success_result,
    visual_provider_input_digest,
)
from vibecad.visual.reconstruction import ReconstructionProposal, VisualObservation

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_INVOCATION_ID = re.compile(r"^visual_invocation_[0-9a-f]{32}$")
_MAX_FIXTURES = 128
_MAX_REASON_BYTES = 512


class FakeVisualOutcomeKind(StrEnum):
    """Closed deterministic outcomes supported by one fixture."""

    OBSERVATION = "observation"
    PROPOSAL = "proposal"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class FakeVisualProviderErrorCode(StrEnum):
    """Stable fail-closed errors without fixture or invocation disclosure."""

    INVALID_FIXTURES = "invalid_fixtures"
    MISSING_FIXTURE = "missing_fixture"
    CONFLICT = "conflict"
    INVALID_INVOCATION_ID = "invalid_invocation_id"
    IDENTITY_MISMATCH = "identity_mismatch"
    INVALID_REASON = "invalid_reason"


class FakeVisualProviderError(ValueError):
    """Bounded deterministic-provider error."""

    def __init__(self, code: FakeVisualProviderErrorCode) -> None:
        if type(code) is not FakeVisualProviderErrorCode:
            raise TypeError("code must be an exact FakeVisualProviderErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: FakeVisualProviderErrorCode) -> None:
    raise FakeVisualProviderError(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class FakeVisualFixture:
    """One immutable outcome, keyed externally only by visual input digest."""

    kind: FakeVisualOutcomeKind
    value: VisualObservation | ReconstructionProposal | None = None
    diagnostic: RuntimeDiagnostic | None = None

    def __post_init__(self) -> None:
        if type(self.kind) is not FakeVisualOutcomeKind:
            _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)
        if self.kind is FakeVisualOutcomeKind.OBSERVATION:
            valid = type(self.value) is VisualObservation and self.diagnostic is None
        elif self.kind is FakeVisualOutcomeKind.PROPOSAL:
            valid = type(self.value) is ReconstructionProposal and self.diagnostic is None
        elif self.kind is FakeVisualOutcomeKind.FAILURE:
            valid = self.value is None and type(self.diagnostic) is RuntimeDiagnostic
        else:
            valid = self.value is None and self.diagnostic is None
        if not valid:
            _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)


def _fixture_snapshot(value: object) -> Mapping[str, FakeVisualFixture]:
    if not isinstance(value, Mapping):
        _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)
    try:
        iterator = iter(value)
    except Exception:
        _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)
    fixtures: dict[str, FakeVisualFixture] = {}
    try:
        for index, key in enumerate(iterator):
            if index >= _MAX_FIXTURES:
                _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)
            if type(key) is not str or _DIGEST.fullmatch(key) is None or key in fixtures:
                _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)
            fixture = value[key]
            if type(fixture) is not FakeVisualFixture:
                _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)
            fixtures[key] = fixture
    except FakeVisualProviderError:
        raise
    except Exception:
        _fail(FakeVisualProviderErrorCode.INVALID_FIXTURES)
    return MappingProxyType(dict(sorted(fixtures.items())))


def _checked_invocation_id(value: object) -> str:
    if type(value) is not str or _INVOCATION_ID.fullmatch(value) is None:
        _fail(FakeVisualProviderErrorCode.INVALID_INVOCATION_ID)
    return value


def _checked_reason(value: object) -> str:
    if type(value) is not str:
        _fail(FakeVisualProviderErrorCode.INVALID_REASON)
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        _fail(FakeVisualProviderErrorCode.INVALID_REASON)
    if not raw or len(raw) > _MAX_REASON_BYTES or value.strip() != value:
        _fail(FakeVisualProviderErrorCode.INVALID_REASON)
    return value


def _unknown_status(invocation_id: str) -> RuntimeStatus:
    return RuntimeStatus(
        invocation_id=invocation_id,
        runtime=VISUAL_PROVIDER_IDENTITY,
        state=RuntimeLifecycleState.UNKNOWN,
    )


class DeterministicFakeVisualProvider:
    """Concrete local provider with no authority beyond generic runtime ports."""

    __slots__ = (
        "_execution_count",
        "_fixtures",
        "_input_digests",
        "_results",
        "_statuses",
    )

    def __init__(self, fixtures: Mapping[str, FakeVisualFixture]) -> None:
        self._fixtures = _fixture_snapshot(fixtures)
        self._input_digests: dict[str, str] = {}
        self._statuses: dict[str, RuntimeStatus] = {}
        self._results: dict[str, RuntimeResult | None] = {}
        self._execution_count = 0

    @property
    def runtime_descriptor(self):
        """Return the exact descriptor required by ``VisualProviderBinding``."""

        return VISUAL_PROVIDER_DESCRIPTOR

    @property
    def execution_count(self) -> int:
        """Return the number of distinct invocations dispatched to a fixture."""

        return self._execution_count

    @property
    def known_invocation_count(self) -> int:
        """Return the number of in-memory invocation records."""

        return len(self._input_digests)

    def start(self, invocation: RuntimeInvocation) -> RuntimeStatus:
        input_digest = visual_provider_input_digest(invocation)
        invocation_id = invocation.invocation_id
        previous_digest = self._input_digests.get(invocation_id)
        if previous_digest is not None:
            if not hmac.compare_digest(previous_digest, input_digest):
                _fail(FakeVisualProviderErrorCode.CONFLICT)
            return self._statuses[invocation_id]

        fixture = self._fixtures.get(input_digest)
        if fixture is None:
            _fail(FakeVisualProviderErrorCode.MISSING_FIXTURE)

        result: RuntimeResult | None
        if fixture.kind in {
            FakeVisualOutcomeKind.OBSERVATION,
            FakeVisualOutcomeKind.PROPOSAL,
        }:
            assert fixture.value is not None
            result = build_visual_provider_success_result(invocation, fixture.value)
            status = RuntimeStatus(
                invocation_id=invocation_id,
                runtime=VISUAL_PROVIDER_IDENTITY,
                state=RuntimeLifecycleState.SUCCEEDED,
            )
        elif fixture.kind is FakeVisualOutcomeKind.FAILURE:
            assert fixture.diagnostic is not None
            result = build_visual_provider_failure_result(invocation, fixture.diagnostic)
            status = RuntimeStatus(
                invocation_id=invocation_id,
                runtime=VISUAL_PROVIDER_IDENTITY,
                state=RuntimeLifecycleState.FAILED,
                diagnostics=result.diagnostics,
            )
        else:
            result = None
            status = _unknown_status(invocation_id)

        # Publish the complete immutable outcome only after all contract builders
        # have succeeded.  Duplicate starts therefore cannot observe partial state.
        self._input_digests[invocation_id] = input_digest
        self._results[invocation_id] = result
        self._statuses[invocation_id] = status
        self._execution_count += 1
        return status

    def get_status(self, invocation_id: str) -> RuntimeStatus:
        checked = _checked_invocation_id(invocation_id)
        return self._statuses.get(checked, _unknown_status(checked))

    def cancel(self, invocation_id: str, *, reason: str) -> RuntimeStatus:
        checked = _checked_invocation_id(invocation_id)
        _checked_reason(reason)
        # Every fixture finishes synchronously or has an explicitly unknown
        # outcome.  Neither case may be rewritten into a guessed cancellation.
        return self._statuses.get(checked, _unknown_status(checked))

    def reconcile(self, invocation_id: str) -> RuntimeStatus:
        checked = _checked_invocation_id(invocation_id)
        # In particular, fixture availability is not permission to replay start.
        return self._statuses.get(checked, _unknown_status(checked))

    def health(self, identity: RuntimeIdentity) -> RuntimeHealth:
        if type(identity) is not RuntimeIdentity or identity != VISUAL_PROVIDER_IDENTITY:
            _fail(FakeVisualProviderErrorCode.IDENTITY_MISMATCH)
        return RuntimeHealth(runtime=identity, state=RuntimeHealthState.HEALTHY)

    def get_result(self, invocation_id: str) -> RuntimeResult | None:
        checked = _checked_invocation_id(invocation_id)
        # A single dictionary read is deliberately the entire non-waiting path.
        return self._results.get(checked)
