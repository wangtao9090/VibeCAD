"""Nominal trusted CAD execution contract shared by application components.

The objects in this module are local Python capabilities, not wire values and
not model-controlled extension points.  The current in-process implementation
verifies only the headless profile; the two GUI profiles are represented
honestly as planned and unavailable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
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
from vibecad.validation import BomObservation, ObservationSnapshot
from vibecad.workflow.contracts import ModelProgram
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram
from vibecad.workflow.state import TaskArtifactRef

MAX_ADMITTED_RUNTIME_MS = 30_000
# PM1 owns Body + Origin + seven helpers + outer Sketch + Pad and one
# Sketch/Pocket pair for each of its bounded 16 circles: 11 + 2 * 16.
MAX_ADMITTED_CREATED_OBJECTS = 43
MAX_ADMITTED_RESULT_BYTES = 262_144

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_PATTERN = re.compile(r"revision_[0-9a-f]{32}\Z")
_RELEASE_DRAWING_VIEWS = ("front", "right", "top", "isometric")
MAX_RELEASE_DRAWING_BYTES = 160_000


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
class ValidatedMaterializationEvidence:
    """Read-only byte evidence for one complete FCStd/STEP delivery pair."""

    fcstd_sha256: str
    fcstd_size_bytes: int
    step_sha256: str
    step_size_bytes: int

    def __post_init__(self) -> None:
        for digest in (self.fcstd_sha256, self.step_sha256):
            if type(digest) is not str or _SHA256_PATTERN.fullmatch(digest) is None:
                raise ValueError("artifact digest must be a lowercase SHA-256 value")
        for size in (self.fcstd_size_bytes, self.step_size_bytes):
            if type(size) is not int or size <= 0:
                raise ValueError("artifact size must be a positive integer")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReleaseCadEvidence:
    """Bounded PDF and BOM facts derived read-only from one immutable Revision."""

    revision_id: str
    bom: BomObservation
    drawing_pdf: bytes = field(repr=False)
    view_names: tuple[str, ...]
    balloon_items: tuple[tuple[int, str], ...]

    def __post_init__(self) -> None:
        if (
            type(self.revision_id) is not str
            or _REVISION_PATTERN.fullmatch(self.revision_id) is None
        ):
            raise ValueError("release evidence revision is invalid")
        if type(self.bom) is not BomObservation or not self.bom.complete or not self.bom.rows:
            raise ValueError("release evidence requires a complete BOM")
        if (
            type(self.drawing_pdf) is not bytes
            or not self.drawing_pdf.startswith(b"%PDF-")
            or b"%%EOF" not in self.drawing_pdf[-32:]
            or len(self.drawing_pdf) > MAX_RELEASE_DRAWING_BYTES
        ):
            raise ValueError("release drawing is invalid")
        if self.view_names != _RELEASE_DRAWING_VIEWS:
            raise ValueError("release drawing views are invalid")
        expected_items = tuple(
            (index, row.component_ids[0]) for index, row in enumerate(self.bom.rows, start=1)
        )
        if self.balloon_items != expected_items:
            raise ValueError("release drawing balloons do not match the BOM")

    @property
    def drawing_sha256(self) -> str:
        return hashlib.sha256(self.drawing_pdf).hexdigest()

    @property
    def drawing_size_bytes(self) -> int:
        return len(self.drawing_pdf)


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

    def revalidate_normalized_import(self, path: Path) -> ValidatedImportEvidence:
        """Revalidate one normalized private FCStd without modifying it."""

        raise NotImplementedError("revalidate_normalized_import is not implemented")

    def validate_materialization(
        self,
        *,
        fcstd: Path,
        step: Path,
    ) -> ValidatedMaterializationEvidence:
        raise NotImplementedError("validate_materialization is not implemented")

    def render_release(self, *, revision: object) -> ReleaseCadEvidence:
        raise NotImplementedError("render_release is not implemented")

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
    "ValidatedMaterializationEvidence",
    "ReleaseCadEvidence",
    "CandidateEvidence",
    "CadExecutionPort",
)
