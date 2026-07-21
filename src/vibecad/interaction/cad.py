"""Nominal trusted CAD execution contract shared by application components.

The objects in this module are local Python capabilities, not wire values and
not model-controlled extension points.  The current in-process implementation
verifies only the headless profile; the two GUI profiles are represented
honestly as planned and unavailable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vibecad.execution.candidate import (
    ActiveCandidate,
    CadSnapshotPort,
    CheckpointedCandidate,
    SealedCandidate,
)
from vibecad.execution.registry import ExecutionProfile
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.validation import ObservationSnapshot
from vibecad.workflow.contracts import ModelProgram
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram
from vibecad.workflow.state import TaskArtifactRef

MAX_ADMITTED_RUNTIME_MS = 30_000
MAX_ADMITTED_CREATED_OBJECTS = 1
MAX_ADMITTED_RESULT_BYTES = 262_144

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CadCapabilityStatus(StrEnum):
    """Closed implementation status for one execution profile."""

    VERIFIED = "verified"
    PLANNED = "planned"


@dataclass(frozen=True, slots=True, kw_only=True)
class CadProfileCapability:
    """Static, immutable truth about one local CAD execution profile."""

    profile: ExecutionProfile
    status: CadCapabilityStatus
    available: bool
    requires_gui_main_thread: bool

    def __post_init__(self) -> None:
        if type(self.profile) is not ExecutionProfile:
            raise TypeError("profile must be an ExecutionProfile")
        if type(self.status) is not CadCapabilityStatus:
            raise TypeError("status must be a CadCapabilityStatus")
        if type(self.available) is not bool or type(self.requires_gui_main_thread) is not bool:
            raise TypeError("capability flags must be booleans")
        if self.status is CadCapabilityStatus.VERIFIED and not self.available:
            raise ValueError("a verified profile must be available")
        if self.status is CadCapabilityStatus.PLANNED and self.available:
            raise ValueError("a planned profile cannot be available")


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidatedImportEvidence:
    """Byte evidence for the normalized private FCStd staging artifact."""

    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if type(self.sha256) is not str or _SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if type(self.size_bytes) is not int or self.size_bytes <= 0:
            raise ValueError("size_bytes must be a positive integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateEvidence:
    """Trusted sealed observations and path-free durable artifact references."""

    snapshot: ObservationSnapshot
    artifacts: tuple[TaskArtifactRef, ...]

    def __post_init__(self) -> None:
        if type(self.snapshot) is not ObservationSnapshot:
            raise ValueError("snapshot must be an ObservationSnapshot")
        if type(self.artifacts) is not tuple or len(self.artifacts) != 2:
            raise ValueError("artifacts must contain the model and STEP references")
        if not all(type(item) is TaskArtifactRef for item in self.artifacts):
            raise ValueError("artifacts must be TaskArtifactRef values")
        if tuple(item.name for item in self.artifacts) != ("model.FCStd", "model.step"):
            raise ValueError("artifact names do not match the sealed CAD layout")
        if tuple(item.format for item in self.artifacts) != ("fcstd", "step"):
            raise ValueError("artifact formats do not match the sealed CAD layout")
        if any(
            item.candidate_revision != self.snapshot.candidate_revision for item in self.artifacts
        ):
            raise ValueError("artifact revisions do not match the sealed snapshot")


class CadExecutionPort(CadSnapshotPort):
    """Nominal local capability for the complete trusted CAD lifecycle."""

    @property
    def execution_profile(self) -> ExecutionProfile:
        raise NotImplementedError("execution_profile is not implemented")

    @property
    def capabilities(self) -> tuple[CadProfileCapability, ...]:
        raise NotImplementedError("capabilities is not implemented")

    def validate_import(self, path: Path) -> ValidatedImportEvidence:
        raise NotImplementedError("validate_import is not implemented")

    def validate_program(self, program: ModelProgram) -> ValidatedProgram:
        raise NotImplementedError("validate_program is not implemented")

    def execute_program(
        self,
        *,
        program: ValidatedProgram,
        candidate: ActiveCandidate,
    ) -> tuple[NormalizedToolOutcome, ...]:
        raise NotImplementedError("execute_program is not implemented")

    def export_step(
        self,
        *,
        candidate: CheckpointedCandidate,
        lease: ProjectWriteLease,
    ) -> None:
        raise NotImplementedError("export_step is not implemented")

    def collect_evidence(self, *, candidate: SealedCandidate) -> CandidateEvidence:
        raise NotImplementedError("collect_evidence is not implemented")


__all__ = (
    "MAX_ADMITTED_RUNTIME_MS",
    "MAX_ADMITTED_CREATED_OBJECTS",
    "MAX_ADMITTED_RESULT_BYTES",
    "CadCapabilityStatus",
    "CadProfileCapability",
    "ValidatedImportEvidence",
    "CandidateEvidence",
    "CadExecutionPort",
)
