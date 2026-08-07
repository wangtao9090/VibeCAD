"""Authority-free contracts for Mesh/SubD derived artifacts.

These values deliberately do not contain CAD Task, Revision, draft, review, or
HEAD fields.  They describe one sealed mesh input and a bounded, deterministic
operation plan whose outputs remain derived artifacts.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

ORGANIC_SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_VERTICES = 500_000
MAX_SOURCE_TRIANGLES = 1_000_000
MAX_OPERATION_COUNT = 12
MAX_OUTPUT_TRIANGLES = 1_000_000
MAX_OUTPUT_ITEM_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SMOOTH_ITERATIONS = 50
MAX_SUBDIVISION_LEVEL = 3

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^mesh_job_[0-9a-f]{32}$")
_SOURCE_ID = re.compile(r"^mesh_input_[0-9a-f]{32}$")
_OPERATION_ID = re.compile(r"^mesh_op_[0-9a-f]{32}$")
_ARTIFACT_ID = re.compile(r"^derived_artifact_[0-9a-f]{32}$")
_TEXT_MAX_BYTES = 256


class OrganicContractErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    UNSUPPORTED_VERSION = "unsupported_version"
    AUTHORITY_VIOLATION = "authority_violation"


class OrganicContractError(ValueError):
    """Bounded error that does not reflect rejected input content."""

    def __init__(self, code: OrganicContractErrorCode, path: str = "") -> None:
        if type(code) is not OrganicContractErrorCode:
            raise TypeError("code must be an exact OrganicContractErrorCode")
        if type(path) is not str or len(path.encode("utf-8")) > 512:
            raise ValueError("path must be bounded text")
        self.code = code
        self.path = path
        super().__init__(code.value)


class MeshMediaType(StrEnum):
    PLY = "application/vnd.vibecad.mesh+ply"
    STL = "model/stl"


class MeshProfile(StrEnum):
    CLOSED_SURFACE_V1 = "closed_surface_v1"
    OPEN_SURFACE_V1 = "open_surface_v1"


class AxisConvention(StrEnum):
    Z_UP_RIGHT_HANDED = "z_up_right_handed"


class MirrorAxis(StrEnum):
    X = "x"
    Y = "y"
    Z = "z"


class MeshOperationKind(StrEnum):
    REMOVE_DUPLICATE_VERTICES = "remove_duplicate_vertices"
    REMOVE_DUPLICATE_TRIANGLES = "remove_duplicate_triangles"
    REMOVE_DEGENERATE_TRIANGLES = "remove_degenerate_triangles"
    REMOVE_UNREFERENCED_VERTICES = "remove_unreferenced_vertices"
    MERGE_CLOSE_VERTICES = "merge_close_vertices"
    ORIENT_NORMALS = "orient_normals"
    TAUBIN_SMOOTH = "taubin_smooth"
    QUADRIC_DECIMATE = "quadric_decimate"
    VERTEX_CLUSTER = "vertex_cluster"
    MIRROR = "mirror"
    VOXEL_REMESH = "voxel_remesh"
    SUBDIVISION_SURFACE = "subdivision_surface"


class DerivedArtifactKind(StrEnum):
    CONTROL_CAGE = "control_cage"
    EDITABLE_BLEND = "editable_blend"
    EVALUATED_GLB = "evaluated_glb"
    PREVIEW_PNG = "preview_png"
    VALIDATION_REPORT = "validation_report"


_ARTIFACT_MEDIA_TYPES = {
    DerivedArtifactKind.CONTROL_CAGE: MeshMediaType.PLY.value,
    DerivedArtifactKind.EDITABLE_BLEND: "application/x-blender",
    DerivedArtifactKind.EVALUATED_GLB: "model/gltf-binary",
    DerivedArtifactKind.PREVIEW_PNG: "image/png",
    DerivedArtifactKind.VALIDATION_REPORT: "application/json",
}


def _fail(code: OrganicContractErrorCode, path: str = "") -> None:
    raise OrganicContractError(code, path)


def _schema(value: object) -> int:
    if type(value) is not int or value != ORGANIC_SCHEMA_VERSION:
        _fail(OrganicContractErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    return value


def _identifier(value: object, pattern: re.Pattern[str], path: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(OrganicContractErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    return _identifier(value, _DIGEST, path)


def _positive_int(value: object, maximum: int, path: str) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        _fail(OrganicContractErrorCode.BUDGET_EXCEEDED, path)
    return value


def _positive_float(value: object, maximum: float, path: str) -> float:
    if type(value) not in {int, float}:
        _fail(OrganicContractErrorCode.INVALID_INPUT, path)
    result = float(value)
    if not math.isfinite(result) or not 0.0 < result <= maximum:
        _fail(OrganicContractErrorCode.BUDGET_EXCEEDED, path)
    return result


def _text(value: object, path: str) -> str:
    if type(value) is not str:
        _fail(OrganicContractErrorCode.INVALID_INPUT, path)
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        _fail(OrganicContractErrorCode.INVALID_INPUT, path)
    if (
        not raw
        or len(raw) > _TEXT_MAX_BYTES
        or value.strip() != value
        or not value.isprintable()
        or len(value.splitlines()) != 1
    ):
        _fail(OrganicContractErrorCode.INVALID_INPUT, path)
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SealedMeshSource:
    source_id: str
    sha256: str
    media_type: MeshMediaType
    byte_count: int
    vertex_count: int
    triangle_count: int
    millimeters_per_unit: int | float
    axis_convention: AxisConvention = AxisConvention.Z_UP_RIGHT_HANDED
    schema_version: int = ORGANIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(self, "source_id", _identifier(self.source_id, _SOURCE_ID, "/source_id"))
        object.__setattr__(self, "sha256", _digest(self.sha256, "/sha256"))
        if type(self.media_type) is not MeshMediaType:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/media_type")
        object.__setattr__(
            self, "byte_count", _positive_int(self.byte_count, MAX_SOURCE_BYTES, "/byte_count")
        )
        object.__setattr__(
            self,
            "vertex_count",
            _positive_int(self.vertex_count, MAX_SOURCE_VERTICES, "/vertex_count"),
        )
        object.__setattr__(
            self,
            "triangle_count",
            _positive_int(self.triangle_count, MAX_SOURCE_TRIANGLES, "/triangle_count"),
        )
        object.__setattr__(
            self,
            "millimeters_per_unit",
            _positive_float(self.millimeters_per_unit, 1_000_000.0, "/millimeters_per_unit"),
        )
        if type(self.axis_convention) is not AxisConvention:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/axis_convention")


@dataclass(frozen=True, slots=True, kw_only=True)
class MeshOperation:
    operation_id: str
    kind: MeshOperationKind
    distance_mm: int | float | None = None
    iterations: int | None = None
    target_triangles: int | None = None
    axis: MirrorAxis | None = None
    level: int | None = None
    schema_version: int = ORGANIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self,
            "operation_id",
            _identifier(self.operation_id, _OPERATION_ID, "/operation_id"),
        )
        if type(self.kind) is not MeshOperationKind:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/kind")

        supplied = {
            name
            for name in ("distance_mm", "iterations", "target_triangles", "axis", "level")
            if getattr(self, name) is not None
        }
        expected: set[str]
        if self.kind is MeshOperationKind.MERGE_CLOSE_VERTICES:
            expected = {"distance_mm"}
        elif self.kind is MeshOperationKind.TAUBIN_SMOOTH:
            expected = {"iterations"}
        elif self.kind in {
            MeshOperationKind.QUADRIC_DECIMATE,
            MeshOperationKind.VERTEX_CLUSTER,
        }:
            expected = {"target_triangles"}
        elif self.kind is MeshOperationKind.MIRROR:
            expected = {"axis"}
        elif self.kind is MeshOperationKind.VOXEL_REMESH:
            expected = {"distance_mm", "target_triangles"}
        elif self.kind is MeshOperationKind.SUBDIVISION_SURFACE:
            expected = {"level"}
        else:
            expected = set()
        if supplied != expected:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/parameters")

        if self.distance_mm is not None:
            object.__setattr__(
                self,
                "distance_mm",
                _positive_float(self.distance_mm, 1_000_000.0, "/distance_mm"),
            )
        if self.iterations is not None:
            object.__setattr__(
                self,
                "iterations",
                _positive_int(self.iterations, MAX_SMOOTH_ITERATIONS, "/iterations"),
            )
        if self.target_triangles is not None:
            object.__setattr__(
                self,
                "target_triangles",
                _positive_int(
                    self.target_triangles,
                    MAX_OUTPUT_TRIANGLES,
                    "/target_triangles",
                ),
            )
        if self.axis is not None and type(self.axis) is not MirrorAxis:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/axis")
        if self.level is not None:
            object.__setattr__(
                self,
                "level",
                _positive_int(self.level, MAX_SUBDIVISION_LEVEL, "/level"),
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class MeshOperationPlan:
    profile: MeshProfile
    operations: tuple[MeshOperation, ...]
    expected_boundary_loops: int
    schema_version: int = ORGANIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        if type(self.profile) is not MeshProfile:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/profile")
        if (
            not isinstance(self.operations, tuple)
            or not 0 < len(self.operations) <= MAX_OPERATION_COUNT
        ):
            _fail(OrganicContractErrorCode.BUDGET_EXCEEDED, "/operations")
        if any(type(operation) is not MeshOperation for operation in self.operations):
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/operations")
        if type(self.expected_boundary_loops) is not int or self.expected_boundary_loops < 0:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/expected_boundary_loops")
        if self.profile is MeshProfile.CLOSED_SURFACE_V1 and self.expected_boundary_loops != 0:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/expected_boundary_loops")
        if (
            self.profile is MeshProfile.OPEN_SURFACE_V1
            and not 1 <= self.expected_boundary_loops <= 128
        ):
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/expected_boundary_loops")


@dataclass(frozen=True, slots=True, kw_only=True)
class MeshJobRequest:
    mesh_job_id: str
    generation: int
    source: SealedMeshSource
    plan: MeshOperationPlan
    schema_version: int = ORGANIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self, "mesh_job_id", _identifier(self.mesh_job_id, _JOB_ID, "/mesh_job_id")
        )
        object.__setattr__(
            self, "generation", _positive_int(self.generation, 2**53 - 1, "/generation")
        )
        if type(self.source) is not SealedMeshSource:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/source")
        if type(self.plan) is not MeshOperationPlan:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/plan")

    @property
    def correlation_semantics(self) -> str:
        return "mesh_job_not_cad_task"


@dataclass(frozen=True, slots=True, kw_only=True)
class DerivedArtifact:
    artifact_id: str
    kind: DerivedArtifactKind
    sha256: str
    byte_count: int
    media_type: str
    schema_version: int = ORGANIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self,
            "artifact_id",
            _identifier(self.artifact_id, _ARTIFACT_ID, "/artifact_id"),
        )
        if type(self.kind) is not DerivedArtifactKind:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/kind")
        object.__setattr__(self, "sha256", _digest(self.sha256, "/sha256"))
        object.__setattr__(
            self,
            "byte_count",
            _positive_int(self.byte_count, MAX_OUTPUT_ITEM_BYTES, "/byte_count"),
        )
        object.__setattr__(self, "media_type", _text(self.media_type, "/media_type"))
        if self.media_type != _ARTIFACT_MEDIA_TYPES[self.kind]:
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/media_type")


@dataclass(frozen=True, slots=True, kw_only=True)
class DerivedArtifactSet:
    mesh_job_id: str
    generation: int
    source_sha256: str
    plan_sha256: str
    artifacts: tuple[DerivedArtifact, ...]
    schema_version: int = ORGANIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _schema(self.schema_version))
        object.__setattr__(
            self, "mesh_job_id", _identifier(self.mesh_job_id, _JOB_ID, "/mesh_job_id")
        )
        object.__setattr__(
            self, "generation", _positive_int(self.generation, 2**53 - 1, "/generation")
        )
        object.__setattr__(self, "source_sha256", _digest(self.source_sha256, "/source_sha256"))
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/plan_sha256"))
        if not isinstance(self.artifacts, tuple) or any(
            type(artifact) is not DerivedArtifact for artifact in self.artifacts
        ):
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/artifacts")
        expected = set(DerivedArtifactKind)
        kinds = {artifact.kind for artifact in self.artifacts}
        if kinds != expected or len(self.artifacts) != len(expected):
            _fail(OrganicContractErrorCode.INVALID_INPUT, "/artifacts")
        total = sum(artifact.byte_count for artifact in self.artifacts)
        if total > MAX_OUTPUT_TOTAL_BYTES:
            _fail(OrganicContractErrorCode.BUDGET_EXCEEDED, "/artifacts")
        ordered = tuple(sorted(self.artifacts, key=lambda item: item.kind.value))
        object.__setattr__(self, "artifacts", ordered)

    @property
    def authority(self) -> str:
        return "derived_artifact_only"
