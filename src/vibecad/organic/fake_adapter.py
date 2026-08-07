"""Deterministic in-memory Mesh/SubD adapter used only by tests."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from vibecad.organic.contracts import (
    MAX_OUTPUT_ITEM_BYTES,
    DerivedArtifact,
    DerivedArtifactKind,
    DerivedArtifactSet,
    MeshJobRequest,
)
from vibecad.organic.plan import validate_mesh_operation_plan
from vibecad.organic.validation import MeshValidationReport, MeshValidationStatus


class FakeOrganicAdapterErrorCode(StrEnum):
    INVALID_FIXTURE = "invalid_fixture"
    MISSING_FIXTURE = "missing_fixture"
    SOURCE_MISMATCH = "source_mismatch"
    VALIDATION_NOT_PASSED = "validation_not_passed"


class FakeOrganicAdapterError(ValueError):
    def __init__(self, code: FakeOrganicAdapterErrorCode) -> None:
        if type(code) is not FakeOrganicAdapterErrorCode:
            raise TypeError("code must be an exact FakeOrganicAdapterErrorCode")
        self.code = code
        super().__init__(code.value)


def _fail(code: FakeOrganicAdapterErrorCode) -> None:
    raise FakeOrganicAdapterError(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class FakeArtifactPayload:
    kind: DerivedArtifactKind
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        if type(self.kind) is not DerivedArtifactKind:
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
        if type(self.media_type) is not str or not self.media_type:
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
        if type(self.content) is not bytes or not 0 < len(self.content) <= MAX_OUTPUT_ITEM_BYTES:
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)


@dataclass(frozen=True, slots=True, kw_only=True)
class FakeOrganicFixture:
    source_sha256: str
    plan_sha256: str
    validation: MeshValidationReport
    artifacts: tuple[FakeArtifactPayload, ...]

    def __post_init__(self) -> None:
        for value in (self.source_sha256, self.plan_sha256):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
        if type(self.validation) is not MeshValidationReport:
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
        if not isinstance(self.artifacts, tuple) or any(
            type(artifact) is not FakeArtifactPayload for artifact in self.artifacts
        ):
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
        expected = set(DerivedArtifactKind)
        if {artifact.kind for artifact in self.artifacts} != expected or len(self.artifacts) != len(
            expected
        ):
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)


def _fixture_snapshot(value: object) -> Mapping[str, FakeOrganicFixture]:
    if not isinstance(value, Mapping):
        _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
    try:
        snapshot = dict(value)
    except Exception:
        _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
    if len(snapshot) > 128:
        _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
    for key, fixture in snapshot.items():
        if type(key) is not str or type(fixture) is not FakeOrganicFixture:
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
        if key != fixture.plan_sha256:
            _fail(FakeOrganicAdapterErrorCode.INVALID_FIXTURE)
    return MappingProxyType(dict(sorted(snapshot.items())))


class DeterministicFakeOrganicAdapter:
    """Create metadata-only derived results without filesystem or CAD authority."""

    __slots__ = ("_execution_count", "_fixtures")

    def __init__(self, fixtures: Mapping[str, FakeOrganicFixture]) -> None:
        self._fixtures = _fixture_snapshot(fixtures)
        self._execution_count = 0

    @property
    def execution_count(self) -> int:
        return self._execution_count

    def execute(self, request: MeshJobRequest) -> DerivedArtifactSet:
        if type(request) is not MeshJobRequest:
            raise TypeError("request must be an exact MeshJobRequest")
        summary = validate_mesh_operation_plan(request.source, request.plan)
        fixture = self._fixtures.get(summary.plan_sha256)
        if fixture is None:
            _fail(FakeOrganicAdapterErrorCode.MISSING_FIXTURE)
        if fixture.source_sha256 != request.source.sha256:
            _fail(FakeOrganicAdapterErrorCode.SOURCE_MISMATCH)
        if (
            fixture.validation.status is not MeshValidationStatus.PASS
            or fixture.validation.profile is not request.plan.profile
            or fixture.validation.boundary_loop_count != request.plan.expected_boundary_loops
        ):
            _fail(FakeOrganicAdapterErrorCode.VALIDATION_NOT_PASSED)

        self._execution_count += 1
        artifacts: list[DerivedArtifact] = []
        for payload in fixture.artifacts:
            digest = hashlib.sha256(payload.content).hexdigest()
            seed = (
                request.mesh_job_id
                + ":"
                + str(request.generation)
                + ":"
                + payload.kind.value
                + ":"
                + digest
            ).encode("ascii")
            artifact_id = (
                "derived_artifact_"
                + hashlib.sha256(b"vibecad-derived-artifact-id-v1\0" + seed).hexdigest()[:32]
            )
            artifacts.append(
                DerivedArtifact(
                    artifact_id=artifact_id,
                    kind=payload.kind,
                    sha256=digest,
                    byte_count=len(payload.content),
                    media_type=payload.media_type,
                )
            )
        return DerivedArtifactSet(
            mesh_job_id=request.mesh_job_id,
            generation=request.generation,
            source_sha256=request.source.sha256,
            plan_sha256=summary.plan_sha256,
            artifacts=tuple(artifacts),
        )
