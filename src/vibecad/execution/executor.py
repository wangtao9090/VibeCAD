"""Trusted in-process CAD execution and sealed-observation boundary.

The executor binds only the six operations in the default ModelProgram
registry.  It never accepts a handler mapping, output path, observation, or
retry policy from the program.  STEP export and verification evidence are
derived from coordinator-owned candidate capabilities and the immutable local
revision store.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import stat
import zipfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from types import MappingProxyType

from vibecad.engine.session import Session as _Session
from vibecad.execution.adapter import (
    AdapterError as _AdapterError,
)
from vibecad.execution.adapter import (
    execute_validated_program as _execute_validated_program,
)
from vibecad.execution.candidate import (
    ActiveCandidate,
    CheckpointedCandidate,
    SealedCandidate,
)
from vibecad.execution.registry import ExecutionProfile, ValueShape, _matches_value_shape
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SelectorV1,
    SemanticRole,
    index_entity_identities,
    resolve_selector,
)
from vibecad.freecad_env import silence_fd1 as _silence_fd1
from vibecad.interaction.cad import (
    CadCapabilityStatus,
    CadExecutionPort,
    CadProfileCapability,
    CandidateEvidence,
    ValidatedImportEvidence,
    ValidatedMaterializationEvidence,
)
from vibecad.tools.modeling import add_box as _add_box
from vibecad.tools.modeling import add_cylinder as _add_cylinder
from vibecad.tools.modify import modify_part as _modify_part
from vibecad.tools.transform import move_part as _move_part
from vibecad.tools.transform import rotate_part as _rotate_part
from vibecad.validation import (
    ArtifactObservation,
    EntityObservation,
    EntityParameterObservation,
    ObservationSnapshot,
    PreservationObservation,
    ShapeObservation,
    compare_entity_preservation,
)
from vibecad.workflow.contracts import ModelProgram, ValueSource
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram, validate_model_program
from vibecad.workflow.state import TaskArtifactRef

_MAX_ARTIFACT_BYTES = 536_870_912
_READ_CHUNK_BYTES = 1024 * 1024
_SIGNATURE_WINDOW_BYTES = 1024 * 1024
_MAX_ZIP_ENTRIES = 4096
_CHECKPOINT_NAME_ATTEMPTS = 8
_REVISION_PATTERN = re.compile(r"revision_[0-9a-f]{32}")


class ExecutorErrorCode(StrEnum):
    """Stable failures owned by the trusted executor boundary."""

    INVALID_INPUT = "invalid_input"
    INVALID_CANDIDATE = "invalid_candidate"
    INVALID_LEASE = "invalid_lease"
    CAD_FAILURE = "cad_failure"
    ARTIFACT_FAILURE = "artifact_failure"
    INTEGRITY_FAILURE = "integrity_failure"


_ERROR_MESSAGES = {
    ExecutorErrorCode.INVALID_INPUT: "The executor input is invalid.",
    ExecutorErrorCode.INVALID_CANDIDATE: "The candidate capability is invalid.",
    ExecutorErrorCode.INVALID_LEASE: "The project write lease is invalid.",
    ExecutorErrorCode.CAD_FAILURE: "The CAD operation failed.",
    ExecutorErrorCode.ARTIFACT_FAILURE: "The CAD artifact is invalid.",
    ExecutorErrorCode.INTEGRITY_FAILURE: "The candidate integrity check failed.",
}


class ExecutorError(ValueError):
    """Fixed, non-reflective executor failure."""

    __slots__ = ("code", "message", "schema_version")

    def __init__(self, code: ExecutorErrorCode) -> None:
        if type(code) is not ExecutorErrorCode:
            raise TypeError("code must be an ExecutorErrorCode")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        self.args = (self.message,)

    def to_mapping(self) -> dict[str, int | str]:
        """Return the fixed schema-v1 JSON-compatible error record."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class _ArtifactSnapshot:
    sha256: str
    size_bytes: int


class _ArtifactReadFailure(Exception):
    """Private marker whose details never cross the executor boundary."""


class _ObservationFailure(Exception):
    """Private marker whose details never cross the executor boundary."""


@dataclass(frozen=True, slots=True)
class _InvocationContext:
    operation_id: str
    operation: str
    preserve: tuple[str, ...]
    source: ValueSource


_PARAMETER_FIELDS = {
    "Part::Box": (
        ("height", "Height", "mm"),
        ("length", "Length", "mm"),
        ("width", "Width", "mm"),
    ),
    "Part::Cylinder": (
        ("angle", "Angle", "deg"),
        ("height", "Height", "mm"),
        ("radius", "Radius", "mm"),
    ),
}


def _fixed_error(code: ExecutorErrorCode) -> ExecutorError:
    return ExecutorError(code)


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _ordinary_owned_file(value: os.stat_result) -> bool:
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        return False
    if value.st_size <= 0 or value.st_size > _MAX_ARTIFACT_BYTES:
        return False
    try:
        return value.st_uid == os.geteuid()
    except AttributeError:
        return True


def _step_placeholder_identity(value: os.stat_result) -> tuple[int, ...] | None:
    """Return the fixed identity of one store-reserved STEP placeholder."""

    if (
        not stat.S_ISREG(value.st_mode)
        or stat.S_IMODE(value.st_mode) != 0o600
        or value.st_nlink != 1
        or value.st_size != 0
    ):
        return None
    try:
        owner = value.st_uid
        current_owner = os.geteuid()
    except AttributeError:
        return None
    if owner != current_owner:
        return None
    return (
        value.st_dev,
        value.st_ino,
        owner,
        value.st_mode,
        value.st_nlink,
    )


def _step_output_matches_placeholder(
    value: os.stat_result,
    placeholder_identity: tuple[int, ...],
) -> bool:
    try:
        owner = value.st_uid
    except AttributeError:
        return False
    return (
        stat.S_ISREG(value.st_mode)
        and 0 < value.st_size <= _MAX_ARTIFACT_BYTES
        and (
            value.st_dev,
            value.st_ino,
            owner,
            value.st_mode,
            value.st_nlink,
        )
        == placeholder_identity
    )


def _safe_zip_names(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    try:
        entries = tuple(archive.infolist())
    except Exception:
        raise _ArtifactReadFailure from None
    if not entries or len(entries) > _MAX_ZIP_ENTRIES:
        raise _ArtifactReadFailure
    total_size = 0
    document_count = 0
    names: set[str] = set()
    for entry in entries:
        name = entry.filename
        if type(name) is not str or not name or "\x00" in name or name in names:
            raise _ArtifactReadFailure
        names.add(name)
        normalized = name.replace("\\", "/")
        parts = tuple(part for part in normalized.split("/") if part)
        if normalized.startswith("/") or ".." in parts or entry.flag_bits & 0x1:
            raise _ArtifactReadFailure
        if entry.file_size < 0 or entry.file_size > _MAX_ARTIFACT_BYTES:
            raise _ArtifactReadFailure
        total_size += entry.file_size
        if total_size > _MAX_ARTIFACT_BYTES:
            raise _ArtifactReadFailure
        if name == "Document.xml":
            document_count += 1
            if entry.file_size <= 0:
                raise _ArtifactReadFailure
    if document_count != 1:
        raise _ArtifactReadFailure
    return entries


def _validate_fcstd_fd(fd: int) -> None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        duplicate = os.dup(fd)
    except OSError:
        raise _ArtifactReadFailure from None
    try:
        with os.fdopen(duplicate, "rb", closefd=True) as stream:
            with zipfile.ZipFile(stream, "r") as archive:
                _safe_zip_names(archive)
                with archive.open("Document.xml", "r") as document:
                    if not document.read(1):
                        raise _ArtifactReadFailure
    except _ArtifactReadFailure:
        raise
    except Exception:
        raise _ArtifactReadFailure from None


def _validate_step_envelope(prefix: bytes, suffix: bytes, saw_nul: bool) -> None:
    if saw_nul:
        raise _ArtifactReadFailure
    leading = prefix.lstrip(b"\xef\xbb\xbf \t\r\n")
    trailing = suffix.rstrip(b" \t\r\n")
    if not leading.startswith(b"ISO-10303-21;"):
        raise _ArtifactReadFailure
    if b"DATA;" not in prefix or b"ENDSEC;" not in suffix:
        raise _ArtifactReadFailure
    if not trailing.endswith(b"END-ISO-10303-21;"):
        raise _ArtifactReadFailure


def _read_artifact(path: object, artifact_format: str) -> _ArtifactSnapshot:
    if not isinstance(path, Path) or artifact_format not in {"fcstd", "step"}:
        raise _ArtifactReadFailure
    try:
        before = os.lstat(path)
    except OSError:
        raise _ArtifactReadFailure from None
    if not _ordinary_owned_file(before):
        raise _ArtifactReadFailure
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError:
        raise _ArtifactReadFailure from None
    digest = hashlib.sha256()
    prefix = bytearray()
    suffix = bytearray()
    saw_nul = False
    try:
        opened = os.fstat(fd)
        if not _ordinary_owned_file(opened) or _stat_identity(opened) != _stat_identity(before):
            raise _ArtifactReadFailure
        remaining = opened.st_size
        while remaining:
            try:
                chunk = os.read(fd, min(_READ_CHUNK_BYTES, remaining))
            except OSError:
                raise _ArtifactReadFailure from None
            if not chunk:
                raise _ArtifactReadFailure
            remaining -= len(chunk)
            digest.update(chunk)
            saw_nul = saw_nul or b"\x00" in chunk
            if len(prefix) < _SIGNATURE_WINDOW_BYTES:
                prefix.extend(chunk[: _SIGNATURE_WINDOW_BYTES - len(prefix)])
            suffix.extend(chunk)
            if len(suffix) > _SIGNATURE_WINDOW_BYTES:
                del suffix[: len(suffix) - _SIGNATURE_WINDOW_BYTES]
        if artifact_format == "fcstd":
            _validate_fcstd_fd(fd)
        else:
            _validate_step_envelope(bytes(prefix), bytes(suffix), saw_nul)
        after = os.fstat(fd)
        if _stat_identity(after) != _stat_identity(opened):
            raise _ArtifactReadFailure
    finally:
        try:
            os.close(fd)
        except OSError:
            raise _ArtifactReadFailure from None
    try:
        closed = os.lstat(path)
    except OSError:
        raise _ArtifactReadFailure from None
    if _stat_identity(closed) != _stat_identity(before):
        raise _ArtifactReadFailure
    return _ArtifactSnapshot(sha256=digest.hexdigest(), size_bytes=before.st_size)


def _finite_number(value: object, *, nonnegative: bool) -> int | float:
    if type(value) not in {int, float} or type(value) is bool:
        raise _ObservationFailure
    if type(value) is float and not math.isfinite(value):
        raise _ObservationFailure
    if nonnegative and value < 0:
        raise _ObservationFailure
    return value


def _managed_assembly_shape(session: object) -> object:
    """Build the aggregate from the complete managed primitive inventory.

    Legacy/fake Session implementations without the managed identity authority retain their
    existing result-root shape.  A real managed Session never falls back once identities exist.
    """

    list_identities = getattr(session, "list_object_identities", None)
    if not callable(list_identities):
        return session.get_assembly_shape()  # type: ignore[attr-defined]
    pairs = tuple(list_identities())
    modelable = tuple(obj for obj, identity in pairs if identity.object_type in _PARAMETER_FIELDS)
    if not modelable:
        return session.get_assembly_shape()  # type: ignore[attr-defined]
    shapes = tuple(obj.Shape for obj in modelable)
    if len(shapes) == 1:
        return shapes[0]
    with _silence_fd1():
        import Part  # noqa: PLC0415

        return Part.makeCompound(list(shapes))


def _shape_center_of_mass(
    shape: object,
    solids: tuple[object, ...],
) -> tuple[int | float, int | float, int | float]:
    try:
        center = shape.CenterOfMass  # type: ignore[attr-defined]
    except AttributeError:
        weighted = [0.0, 0.0, 0.0]
        total_volume = 0.0
        try:
            for solid in solids:
                volume = _finite_number(solid.Volume, nonnegative=True)  # type: ignore[attr-defined]
                solid_center = solid.CenterOfMass  # type: ignore[attr-defined]
                components = (
                    _finite_number(solid_center.x, nonnegative=False),
                    _finite_number(solid_center.y, nonnegative=False),
                    _finite_number(solid_center.z, nonnegative=False),
                )
                total_volume += float(volume)
                for index, component in enumerate(components):
                    weighted[index] += float(volume) * float(component)
        except _ObservationFailure:
            raise
        except Exception:
            raise _ObservationFailure from None
        if (
            not math.isfinite(total_volume)
            or total_volume <= 0
            or not _same_geometry_number(
                total_volume,
                _finite_number(shape.Volume, nonnegative=True),  # type: ignore[attr-defined]
            )
        ):
            raise _ObservationFailure from None
        return (
            weighted[0] / total_volume,
            weighted[1] / total_volume,
            weighted[2] / total_volume,
        )
    except Exception:
        raise _ObservationFailure from None
    return (
        _finite_number(center.x, nonnegative=False),
        _finite_number(center.y, nonnegative=False),
        _finite_number(center.z, nonnegative=False),
    )


def _shape_observation(session: object) -> ShapeObservation:
    try:
        shape = _managed_assembly_shape(session)
        volume = _finite_number(shape.Volume, nonnegative=True)
        area = _finite_number(shape.Area, nonnegative=True)
        bound_box = shape.BoundBox
        bbox = (
            _finite_number(bound_box.XLength, nonnegative=True),
            _finite_number(bound_box.YLength, nonnegative=True),
            _finite_number(bound_box.ZLength, nonnegative=True),
        )
        solids = tuple(shape.Solids)
        center_of_mass = _shape_center_of_mass(shape, solids)
        valid_shape = shape.isValid()
        if type(valid_shape) is not bool:
            raise _ObservationFailure
        solid_count = len(solids)
        if type(solid_count) is not int or solid_count < 0:
            raise _ObservationFailure
        return ShapeObservation(
            target="body",
            volume_mm3=volume,
            area_mm2=area,
            bbox_mm=bbox,
            center_of_mass_mm=center_of_mass,
            valid_shape=valid_shape,
            solid_count=solid_count,
        )
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _quantity_value(value: object) -> int | float:
    if type(value) in {int, float}:
        return _finite_number(value, nonnegative=False)
    try:
        raw = value.Value  # type: ignore[attr-defined]
    except Exception:
        raise _ObservationFailure from None
    return _finite_number(raw, nonnegative=False)


def _canonical_placement(value: object) -> tuple[int | float, ...]:
    try:
        base = value.Base  # type: ignore[attr-defined]
        rotation = value.Rotation  # type: ignore[attr-defined]
        translation = (
            _finite_number(base.x, nonnegative=False),
            _finite_number(base.y, nonnegative=False),
            _finite_number(base.z, nonnegative=False),
        )
        raw_quaternion = tuple(rotation.Q)
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None
    if len(raw_quaternion) != 4:
        raise _ObservationFailure
    quaternion = tuple(_finite_number(component, nonnegative=False) for component in raw_quaternion)
    try:
        norm = math.sqrt(sum(component * component for component in quaternion))
    except (ArithmeticError, OverflowError):
        raise _ObservationFailure from None
    if not math.isfinite(norm) or norm <= 0:
        raise _ObservationFailure
    normalized = tuple(component / norm for component in quaternion)
    first_nonzero = next((component for component in normalized if component != 0), 0)
    if first_nonzero < 0:
        normalized = tuple(-component for component in normalized)
    return (*translation, *normalized)


def _entity_geometry(shape: object) -> dict[str, object]:
    try:
        null_check = getattr(shape, "isNull", None)
        if null_check is not None:
            if not callable(null_check):
                raise _ObservationFailure
            is_null = null_check()
            if type(is_null) is not bool:
                raise _ObservationFailure
            if is_null:
                return {
                    "volume_mm3": None,
                    "area_mm2": None,
                    "bbox_mm": None,
                    "center_of_mass_mm": None,
                    "valid_shape": None,
                    "solid_count": None,
                }
        bound_box = shape.BoundBox  # type: ignore[attr-defined]
        center = shape.CenterOfMass  # type: ignore[attr-defined]
        valid_shape = shape.isValid()  # type: ignore[attr-defined]
        if type(valid_shape) is not bool:
            raise _ObservationFailure
        solid_count = len(shape.Solids)  # type: ignore[attr-defined]
        if type(solid_count) is not int or solid_count < 0:
            raise _ObservationFailure
        return {
            "volume_mm3": _finite_number(shape.Volume, nonnegative=True),  # type: ignore[attr-defined]
            "area_mm2": _finite_number(shape.Area, nonnegative=True),  # type: ignore[attr-defined]
            "bbox_mm": (
                _finite_number(bound_box.XLength, nonnegative=True),
                _finite_number(bound_box.YLength, nonnegative=True),
                _finite_number(bound_box.ZLength, nonnegative=True),
            ),
            "center_of_mass_mm": (
                _finite_number(center.x, nonnegative=False),
                _finite_number(center.y, nonnegative=False),
                _finite_number(center.z, nonnegative=False),
            ),
            "valid_shape": valid_shape,
            "solid_count": solid_count,
        }
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _bound_box_center(shape: object) -> tuple[int | float, int | float, int | float]:
    """Return the live global center used by the fixed legacy rotation leaf."""

    try:
        bound_box = shape.BoundBox  # type: ignore[attr-defined]
        bounds = tuple(
            (
                _finite_number(getattr(bound_box, f"{axis}Min"), nonnegative=False),
                _finite_number(getattr(bound_box, f"{axis}Max"), nonnegative=False),
            )
            for axis in ("X", "Y", "Z")
        )
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None
    if any(high < low for low, high in bounds):
        raise _ObservationFailure
    return tuple((float(low) + float(high)) / 2.0 for low, high in bounds)  # type: ignore[return-value]


def _entity_observation(obj: object, identity: EntityIdentity) -> EntityObservation:
    try:
        parameters = tuple(
            EntityParameterObservation(
                name=name,
                value=_quantity_value(getattr(obj, property_name)),
                unit=unit,
            )
            for name, property_name, unit in _PARAMETER_FIELDS.get(identity.object_type, ())
        )
        placement = _canonical_placement(obj.Placement)  # type: ignore[attr-defined]
        shape = getattr(obj, "Shape", None)
        geometry = (
            {
                "volume_mm3": None,
                "area_mm2": None,
                "bbox_mm": None,
                "center_of_mass_mm": None,
                "valid_shape": None,
                "solid_count": None,
            }
            if shape is None
            else _entity_geometry(shape)
        )
        return EntityObservation(
            object_id=identity.object_id,
            feature_id=identity.feature_id,
            object_type=identity.object_type,
            semantic_role=identity.semantic_role.value,
            provenance=identity.provenance.to_mapping(),
            placement=placement,
            parameters=parameters,
            **geometry,
        )
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _entity_observations(session: object) -> tuple[EntityObservation, ...]:
    try:
        document_objects = tuple(session.doc.Objects)  # type: ignore[attr-defined]
        list_identities = getattr(session, "list_object_identities", None)
        if callable(list_identities):
            pairs = tuple(list_identities())
        else:
            identities = index_entity_identities(document_objects)
            pairs = tuple(zip(document_objects, identities, strict=True))
        modelable_objects = tuple(
            obj for obj in document_objects if getattr(obj, "TypeId", None) in _PARAMETER_FIELDS
        )
        if any(
            sum(current is obj for current, _ in pairs) != 1 for obj in modelable_objects
        ) or any(not any(current is obj for obj in document_objects) for current, _ in pairs):
            raise _ObservationFailure
        observations = tuple(
            sorted(
                (_entity_observation(obj, identity) for obj, identity in pairs),
                key=lambda item: item.object_id,
            )
        )
        if len({item.object_id for item in observations}) != len(observations):
            raise _ObservationFailure
        feature_ids = tuple(item.feature_id for item in observations if item.feature_id is not None)
        if len(set(feature_ids)) != len(feature_ids):
            raise _ObservationFailure
        return observations
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _reloaded_observations(
    path: Path,
    *,
    include_shape: bool,
) -> tuple[ShapeObservation | None, tuple[EntityObservation, ...]]:
    probe = None
    failed = False
    shape: ShapeObservation | None = None
    entities: tuple[EntityObservation, ...] = ()
    try:
        probe = _Session()
        probe.load_document(path)
        if include_shape:
            shape = _shape_observation(probe)
        entities = _entity_observations(probe)
    except Exception:
        failed = True
    finally:
        if probe is not None:
            try:
                probe.close_document()
            except Exception:
                failed = True
    if failed:
        raise _ObservationFailure
    return shape, entities


def _preservation_observations(
    before: tuple[EntityObservation, ...],
    after: tuple[EntityObservation, ...],
) -> tuple[PreservationObservation, ...]:
    before_by_id = {item.object_id: item for item in before}
    after_by_id = {item.object_id: item for item in after}
    comparisons = []
    for object_id in sorted(set(before_by_id) | set(after_by_id)):
        old = before_by_id.get(object_id)
        new = after_by_id.get(object_id)
        reference = old if old is not None else new
        assert reference is not None
        targets = (reference.object_id,) + (
            (reference.feature_id,) if reference.feature_id is not None else ()
        )
        comparisons.extend(
            compare_entity_preservation(old, new, target=target) for target in targets
        )
    return tuple(sorted(comparisons, key=lambda item: item.target))


def _bound_selectors(value: object) -> tuple[SelectorV1, ...]:
    if type(value) is SelectorV1:
        return (value,)
    if type(value) is MappingProxyType:
        return tuple(selector for item in value.values() for selector in _bound_selectors(item))
    if type(value) is tuple:
        return tuple(selector for item in value for selector in _bound_selectors(item))
    return ()


def _operation_failure() -> RuntimeError:
    return RuntimeError("managed operation invariant failed")


def _same_number(actual: object, expected: object) -> bool:
    if type(actual) not in {int, float} or type(expected) not in {int, float}:
        return False
    return math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-9)


def _same_geometry_number(actual: object, expected: object) -> bool:
    if type(actual) not in {int, float} or type(expected) not in {int, float}:
        return False
    return math.isclose(float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9)


def _same_vector(actual: tuple[int | float, ...], expected: object) -> bool:
    return (
        type(expected) is tuple
        and len(actual) == len(expected)
        and all(
            _same_number(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    )


def _same_geometry_vector(actual: tuple[int | float, ...], expected: object) -> bool:
    return (
        type(expected) is tuple
        and len(actual) == len(expected)
        and all(
            _same_geometry_number(actual_item, expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    )


def _quaternion_product(
    left: tuple[int | float, ...],
    right: tuple[int | float, ...],
) -> tuple[float, float, float, float]:
    lx, ly, lz, lw = (float(item) for item in left)
    rx, ry, rz, rw = (float(item) for item in right)
    return (
        lw * rx + lx * rw + ly * rz - lz * ry,
        lw * ry - lx * rz + ly * rw + lz * rx,
        lw * rz + lx * ry - ly * rx + lz * rw,
        lw * rw - lx * rx - ly * ry - lz * rz,
    )


def _rotate_vector(
    quaternion: tuple[int | float, ...],
    vector: tuple[int | float, ...],
) -> tuple[float, float, float]:
    qx, qy, qz, qw = (float(item) for item in quaternion)
    vx, vy, vz = (float(item) for item in vector)
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)
    return (
        vx + qw * tx + qy * tz - qz * ty,
        vy + qw * ty + qz * tx - qx * tz,
        vz + qw * tz + qx * ty - qy * tx,
    )


def _same_rotation(
    actual: tuple[int | float, ...],
    expected: tuple[int | float, ...],
) -> bool:
    try:
        dot = sum(
            float(actual_item) * float(expected_item)
            for actual_item, expected_item in zip(actual, expected, strict=True)
        )
    except (TypeError, ValueError):
        return False
    return math.isclose(abs(dot), 1.0, rel_tol=0.0, abs_tol=1e-9)


def _same_optional_geometry_number(actual: object, expected: object) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return _same_geometry_number(actual, expected)


def _same_optional_geometry_vector(actual: object, expected: object) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return type(actual) is tuple and _same_geometry_vector(actual, expected)


def _same_import_parameter(
    actual: EntityParameterObservation,
    expected: EntityParameterObservation,
) -> bool:
    return (
        type(actual) is EntityParameterObservation
        and type(expected) is EntityParameterObservation
        and actual.schema_version == expected.schema_version
        and actual.name == expected.name
        and type(actual.value) is type(expected.value)
        and actual.value == expected.value
        and actual.unit == expected.unit
    )


def _same_import_observation(
    actual: EntityObservation,
    expected: EntityObservation,
) -> bool:
    """Compare one save/reload boundary without treating OCC float noise as drift."""

    return (
        type(actual) is EntityObservation
        and type(expected) is EntityObservation
        and actual.object_id == expected.object_id
        and actual.feature_id == expected.feature_id
        and actual.object_type == expected.object_type
        and actual.semantic_role == expected.semantic_role
        and actual.provenance == expected.provenance
        and len(actual.placement) == len(expected.placement) == 7
        and _same_vector(actual.placement[:3], expected.placement[:3])
        and _same_rotation(actual.placement[3:], expected.placement[3:])
        and len(actual.parameters) == len(expected.parameters)
        and all(
            _same_import_parameter(left, right)
            for left, right in zip(actual.parameters, expected.parameters, strict=True)
        )
        and _same_optional_geometry_number(actual.volume_mm3, expected.volume_mm3)
        and _same_optional_geometry_number(actual.area_mm2, expected.area_mm2)
        and _same_optional_geometry_vector(actual.bbox_mm, expected.bbox_mm)
        and _same_optional_geometry_vector(
            actual.center_of_mass_mm,
            expected.center_of_mass_mm,
        )
        and actual.valid_shape is expected.valid_shape
        and actual.solid_count == expected.solid_count
    )


def _same_import_observations(actual: object, expected: object) -> bool:
    """Keep non-contract test doubles exact while comparing real observations semantically."""

    if not (
        type(actual) is tuple
        and type(expected) is tuple
        and len(actual) == len(expected)
        and all(type(item) is EntityObservation for item in (*actual, *expected))
    ):
        return actual == expected
    return all(
        _same_import_observation(left, right) for left, right in zip(actual, expected, strict=True)
    )


def _axis_rotation(axis: object, angle: object) -> tuple[float, float, float, float]:
    if type(axis) is not str or axis not in {"x", "y", "z"}:
        raise _operation_failure()
    if type(angle) not in {int, float}:
        raise _operation_failure()
    half_angle = math.radians(float(angle)) / 2.0
    sine = math.sin(half_angle)
    components = {
        "x": (sine, 0.0, 0.0),
        "y": (0.0, sine, 0.0),
        "z": (0.0, 0.0, sine),
    }[axis]
    return (*components, math.cos(half_angle))


def _observation_map(
    observations: tuple[EntityObservation, ...],
) -> dict[str, EntityObservation]:
    result = {item.object_id: item for item in observations}
    if len(result) != len(observations):
        raise _operation_failure()
    return result


def _identified_pairs(session: object) -> tuple[tuple[object, EntityIdentity], ...]:
    try:
        objects = tuple(session.doc.Objects)  # type: ignore[attr-defined]
        list_identities = getattr(session, "list_object_identities", None)
        if callable(list_identities):
            raw_pairs = tuple(list_identities())
        else:
            identities = index_entity_identities(objects)
            raw_pairs = tuple(zip(objects, identities, strict=True))
        if not all(type(identity) is EntityIdentity for _, identity in raw_pairs):
            raise ValueError
        return tuple((obj, identity) for obj, identity in raw_pairs)
    except Exception:
        raise _operation_failure() from None


def _require_preserved(
    before: EntityObservation | None,
    after: EntityObservation | None,
    *,
    target: str,
    preserve: tuple[str, ...] = (),
) -> PreservationObservation:
    try:
        comparison = compare_entity_preservation(
            before,
            after,
            target=target,
            preserve=preserve,
        )
    except Exception:
        raise _operation_failure() from None
    if not comparison.preserved:
        raise _operation_failure()
    return comparison


def _require_non_target_preservation(
    before: dict[str, EntityObservation],
    after: dict[str, EntityObservation],
    *,
    target: str | None,
) -> list[PreservationObservation]:
    if set(before) != set(after):
        raise _operation_failure()
    comparisons: list[PreservationObservation] = []
    for object_id in sorted(before):
        if object_id == target:
            continue
        comparisons.append(
            _require_preserved(
                before[object_id],
                after[object_id],
                target=object_id,
            )
        )
    return comparisons


def _managed_create(
    session: object,
    context: _InvocationContext,
    *,
    leaf: Callable[..., object],
    expected_type: str,
    **kwargs: object,
) -> dict[str, object]:
    """Create one primitive, bind identity, and rebuild the result from live facts."""

    if context.preserve:
        raise _operation_failure()
    before = _entity_observations(session)
    try:
        document_before = tuple(session.doc.Objects)  # type: ignore[attr-defined]
    except Exception:
        raise _operation_failure() from None
    leaf(session, **kwargs)
    attach = getattr(session, "attach_object_identity", None)
    read_identity = getattr(session, "read_object_identity", None)
    if not callable(attach) or not callable(read_identity):
        raise _operation_failure()
    try:
        document_after = tuple(session.doc.Objects)  # type: ignore[attr-defined]
        if any(not any(current is obj for current in document_after) for obj in document_before):
            raise ValueError
        added = tuple(
            obj for obj in document_after if not any(obj is current for current in document_before)
        )
        if len(added) != 1 or len(document_after) != len(document_before) + 1:
            raise ValueError
        obj = added[0]
        object_type = obj.TypeId
        if object_type != expected_type:
            raise ValueError
        identity = EntityIdentity(
            object_id=f"object_{secrets.token_hex(16)}",
            feature_id=f"feature_{secrets.token_hex(16)}",
            object_type=object_type,
            semantic_role=SemanticRole.PRIMITIVE,
            provenance=Provenance(
                source=ProvenanceSource(context.source.value),
                operation_id=context.operation_id,
            ),
        )
        attached = attach(obj, identity)
        observed = read_identity(obj)
        if (
            type(attached) is not EntityIdentity
            or attached != identity
            or type(observed) is not EntityIdentity
            or observed != identity
        ):
            raise ValueError
    except Exception:
        raise _operation_failure() from None

    after = _entity_observations(session)
    before_by_id = _observation_map(before)
    after_by_id = _observation_map(after)
    if set(before_by_id) - set(after_by_id) or set(after_by_id) - set(before_by_id) != {
        identity.object_id
    }:
        raise _operation_failure()
    comparisons = _require_non_target_preservation(
        before_by_id,
        {key: value for key, value in after_by_id.items() if key != identity.object_id},
        target=None,
    )
    created = after_by_id[identity.object_id]
    if created.object_type != expected_type or created.feature_id != identity.feature_id:
        raise _operation_failure()
    parameters = {item.name: item.value for item in created.parameters}
    expected_position = kwargs.get("position", (0.0, 0.0, 0.0))
    if type(expected_position) is not tuple or len(expected_position) != 3:
        raise _operation_failure()
    if context.operation == "create_box":
        expected_parameters = {
            "length": kwargs.get("length"),
            "width": kwargs.get("width"),
            "height": kwargs.get("height"),
        }
        expected_rotation = (0.0, 0.0, 0.0, 1.0)
        length, width, height = (
            expected_parameters["length"],
            expected_parameters["width"],
            expected_parameters["height"],
        )
        if any(type(item) not in {int, float} for item in (length, width, height)):
            raise _operation_failure()
        expected_volume = float(length) * float(width) * float(height)
        expected_area = 2.0 * (
            float(length) * float(width)
            + float(length) * float(height)
            + float(width) * float(height)
        )
        expected_bbox = (length, width, height)
        expected_center = tuple(
            float(origin) + float(dimension) / 2.0
            for origin, dimension in zip(
                expected_position,
                expected_bbox,
                strict=True,
            )
        )
    elif context.operation == "create_cylinder":
        expected_parameters = {
            "radius": kwargs.get("radius"),
            "height": kwargs.get("height"),
            "angle": 360.0,
        }
        radius, height = expected_parameters["radius"], expected_parameters["height"]
        if type(radius) not in {int, float} or type(height) not in {int, float}:
            raise _operation_failure()
        expected_volume = math.pi * float(radius) ** 2 * float(height)
        expected_area = 2.0 * math.pi * float(radius) * (float(radius) + float(height))
        cylinder_axis = kwargs.get("axis", "z")
        if cylinder_axis == "x":
            expected_rotation = _axis_rotation("y", 90.0)
            expected_bbox = (height, 2.0 * float(radius), 2.0 * float(radius))
            center_offset = (float(height) / 2.0, 0.0, 0.0)
        elif cylinder_axis == "y":
            expected_rotation = _axis_rotation("x", -90.0)
            expected_bbox = (2.0 * float(radius), height, 2.0 * float(radius))
            center_offset = (0.0, float(height) / 2.0, 0.0)
        elif cylinder_axis == "z":
            expected_rotation = (0.0, 0.0, 0.0, 1.0)
            expected_bbox = (2.0 * float(radius), 2.0 * float(radius), height)
            center_offset = (0.0, 0.0, float(height) / 2.0)
        else:
            raise _operation_failure()
        expected_center = tuple(
            float(origin) + offset
            for origin, offset in zip(expected_position, center_offset, strict=True)
        )
    else:
        raise _operation_failure()
    if set(parameters) != set(expected_parameters) or any(
        not _same_number(parameters[name], expected)
        for name, expected in expected_parameters.items()
    ):
        raise _operation_failure()
    if not _same_vector(created.placement[:3], expected_position) or not _same_rotation(
        created.placement[3:],
        expected_rotation,
    ):
        raise _operation_failure()
    if (
        created.valid_shape is not True
        or created.solid_count != 1
        or created.volume_mm3 is None
        or created.volume_mm3 <= 0
        or created.area_mm2 is None
        or created.area_mm2 <= 0
        or created.bbox_mm is None
        or any(component <= 0 for component in created.bbox_mm)
        or created.center_of_mass_mm is None
        or not _same_geometry_number(created.volume_mm3, expected_volume)
        or not _same_geometry_number(created.area_mm2, expected_area)
        or not _same_geometry_vector(created.bbox_mm, expected_bbox)
        or not _same_geometry_vector(created.center_of_mass_mm, expected_center)
    ):
        raise _operation_failure()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "entity_created",
        "operation": context.operation,
        "object_id": created.object_id,
        "feature_id": created.feature_id,
        "after": created.to_mapping(),
        "preservation": [item.to_mapping() for item in comparisons],
    }


def _resolve_entity_target(
    session: object,
    target: object,
    *,
    project_id: str,
    revision_id: str,
) -> tuple[object, EntityIdentity]:
    pairs = _identified_pairs(session)
    objects = tuple(obj for obj, _ in pairs)
    try:
        if type(target) is SelectorV1:
            obj = resolve_selector(
                target,
                objects,
                project_id=project_id,
                revision_id=revision_id,
            )
        elif _matches_value_shape(target, ValueShape.OBJECT_ID):
            matches = tuple(obj for obj, identity in pairs if identity.object_id == target)
            if len(matches) != 1:
                raise ValueError
            obj = matches[0]
        else:
            raise ValueError
        identity = next(identity for current, identity in pairs if current is obj)
        if identity.object_id != getattr(target, "object_id", identity.object_id):
            raise ValueError
        return obj, identity
    except Exception:
        raise _operation_failure() from None


def _parameter_value(observation: EntityObservation, name: str) -> int | float:
    try:
        parameter = next(item for item in observation.parameters if item.name == name)
    except StopIteration:
        raise _operation_failure() from None
    if type(parameter.value) not in {int, float}:
        raise _operation_failure()
    return parameter.value


def _managed_mutation(
    session: object,
    context: _InvocationContext,
    *,
    project_id: str,
    revision_id: str,
    target: object,
    leaf: Callable[..., object],
    leaf_kwargs: dict[str, object],
    parameter: str | None = None,
    value: object = None,
    position: object = None,
) -> dict[str, object]:
    before = _entity_observations(session)
    before_by_id = _observation_map(before)
    obj, identity = _resolve_entity_target(
        session,
        target,
        project_id=project_id,
        revision_id=revision_id,
    )
    old = before_by_id.get(identity.object_id)
    if old is None:
        raise _operation_failure()
    rotation_pivot: tuple[int | float, int | float, int | float] | None = None
    if context.operation == "rotate_part":
        try:
            rotation_pivot = _bound_box_center(obj.Shape)
        except _ObservationFailure:
            raise _operation_failure() from None
    set_result = getattr(session, "set_result_object", None)
    if not callable(set_result):
        raise _operation_failure()
    set_result(obj)
    leaf(session, name=obj.Name, **leaf_kwargs)

    after = _entity_observations(session)
    after_by_id = _observation_map(after)
    new = after_by_id.get(identity.object_id)
    if new is None:
        raise _operation_failure()
    comparisons = _require_non_target_preservation(
        before_by_id,
        after_by_id,
        target=identity.object_id,
    )
    parameter_names = {item.name for item in old.parameters}
    fixed: set[str]
    if context.operation == "modify_parameter":
        if type(parameter) is not str or type(value) not in {int, float}:
            raise _operation_failure()
        fixed = {"placement", *(parameter_names - {parameter})}
        actual = _parameter_value(new, parameter)
        if not math.isclose(float(actual), float(value), rel_tol=0.0, abs_tol=1e-9):
            raise _operation_failure()
    elif context.operation == "move_part":
        if type(position) is not tuple or len(position) != 3:
            raise _operation_failure()
        fixed = {
            *parameter_names,
            "solid_count",
            "valid_shape",
        }
        if (
            any(
                not math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-9)
                for actual, expected in zip(new.placement[:3], position, strict=True)
            )
            or new.placement[3:] != old.placement[3:]
        ):
            raise _operation_failure()
    elif context.operation == "rotate_part":
        fixed = {
            *parameter_names,
            "solid_count",
            "valid_shape",
        }
        axis = leaf_kwargs.get("axis")
        angle = leaf_kwargs.get("angle")
        delta_rotation = _axis_rotation(axis, angle)
        expected_rotation = _quaternion_product(delta_rotation, old.placement[3:])
        pivot = rotation_pivot
        old_center = old.center_of_mass_mm
        if pivot is None or old_center is None:
            raise _operation_failure()
        rotated_offset = _rotate_vector(
            delta_rotation,
            tuple(
                float(origin) - float(center)
                for origin, center in zip(old.placement[:3], pivot, strict=True)
            ),
        )
        expected_translation = tuple(
            float(center) + offset for center, offset in zip(pivot, rotated_offset, strict=True)
        )
        rotated_center_offset = _rotate_vector(
            delta_rotation,
            tuple(
                float(center_of_mass) - float(center)
                for center_of_mass, center in zip(old_center, pivot, strict=True)
            ),
        )
        expected_center = tuple(
            float(center) + offset
            for center, offset in zip(pivot, rotated_center_offset, strict=True)
        )
        if (
            new.placement == old.placement
            or not _same_rotation(new.placement[3:], expected_rotation)
            or not _same_geometry_vector(new.placement[:3], expected_translation)
            or new.center_of_mass_mm is None
            or not _same_geometry_vector(new.center_of_mass_mm, expected_center)
        ):
            raise _operation_failure()
    else:
        raise _operation_failure()
    if context.operation in {"move_part", "rotate_part"} and (
        not _same_geometry_number(old.volume_mm3, new.volume_mm3)
        or not _same_geometry_number(old.area_mm2, new.area_mm2)
    ):
        raise _operation_failure()
    requested_fields = fixed | set(context.preserve)
    for tolerant_field, old_value, new_value in (
        ("volume_mm3", old.volume_mm3, new.volume_mm3),
        ("area_mm2", old.area_mm2, new.area_mm2),
    ):
        if tolerant_field in requested_fields:
            if not _same_geometry_number(old_value, new_value):
                raise _operation_failure()
            requested_fields.remove(tolerant_field)
    requested = tuple(sorted(requested_fields))
    comparisons.append(
        _require_preserved(
            old,
            new,
            target=identity.object_id,
            preserve=requested,
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "entity_modified",
        "operation": context.operation,
        "object_id": new.object_id,
        "feature_id": new.feature_id,
        "before": old.to_mapping(),
        "after": new.to_mapping(),
        "preservation": [item.to_mapping() for item in comparisons],
    }


def _managed_inspect(
    session: object,
    context: _InvocationContext,
) -> dict[str, object]:
    if context.preserve:
        raise _operation_failure()
    before = _entity_observations(session)
    shape = _shape_observation(session)
    after = _entity_observations(session)
    if before != after:
        raise _operation_failure()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "model_inspection",
        "operation": context.operation,
        "shape": shape.to_mapping(),
        "entities": [item.to_mapping() for item in after],
    }


def _queued_handler(
    contexts: deque[_InvocationContext],
    callback: Callable[..., object],
) -> Callable[..., object]:
    def invoke(**kwargs: object) -> object:
        try:
            context = contexts.popleft()
        except IndexError:
            raise _operation_failure() from None
        return callback(context, **kwargs)

    return invoke


def _managed_modify_parameter(
    session: object,
    context: _InvocationContext,
    *,
    project_id: str,
    revision_id: str,
    target: object,
    parameter: str,
    value: object,
) -> dict[str, object]:
    return _managed_mutation(
        session,
        context,
        project_id=project_id,
        revision_id=revision_id,
        target=target,
        leaf=_modify_part,
        leaf_kwargs={"parameter": parameter, "value": value},
        parameter=parameter,
        value=value,
    )


def _managed_move_part(
    session: object,
    context: _InvocationContext,
    *,
    project_id: str,
    revision_id: str,
    target: object,
    position: object,
) -> dict[str, object]:
    return _managed_mutation(
        session,
        context,
        project_id=project_id,
        revision_id=revision_id,
        target=target,
        leaf=_move_part,
        leaf_kwargs={"position": position},
        position=position,
    )


def _managed_rotate_part(
    session: object,
    context: _InvocationContext,
    *,
    project_id: str,
    revision_id: str,
    target: object,
    axis: str,
    angle: object,
) -> dict[str, object]:
    return _managed_mutation(
        session,
        context,
        project_id=project_id,
        revision_id=revision_id,
        target=target,
        leaf=_rotate_part,
        leaf_kwargs={"axis": axis, "angle": angle},
    )


def _artifact_matches(actual: _ArtifactSnapshot, expected: RevisionArtifactRef) -> bool:
    return actual.sha256 == expected.sha256 and actual.size_bytes == expected.size_bytes


def _remove_failed_artifact(path: Path) -> None:
    """Remove only an executor-owned ordinary partial file, never a link."""

    try:
        current = os.lstat(path)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            return
        try:
            if current.st_uid != os.geteuid():
                return
        except AttributeError:
            pass
        if current.st_size > _MAX_ARTIFACT_BYTES:
            return
        os.unlink(path)
    except OSError:
        pass


def _fresh_checkpoint_path(path: Path) -> Path:
    """Reserve a name FreeCAD has never seen without creating the leaf first."""

    for _ in range(_CHECKPOINT_NAME_ATTEMPTS):
        candidate = path.with_name(f".vibecad-checkpoint-{secrets.token_hex(16)}.FCStd")
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            return candidate
        except OSError:
            raise _ArtifactReadFailure from None
    raise _ArtifactReadFailure


def _require_revision_layout(revision: object) -> tuple[RevisionArtifactRef, RevisionArtifactRef]:
    if type(revision) is not RevisionRef or type(revision.model) is not RevisionArtifactRef:
        raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
    if type(revision.artifacts) is not tuple or len(revision.artifacts) != 1:
        raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
    step = revision.artifacts[0]
    if type(step) is not RevisionArtifactRef:
        raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
    if (revision.model.name, revision.model.format) != ("model.FCStd", "fcstd"):
        raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
    if (step.name, step.format) != ("model.step", "step"):
        raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
    return revision.model, step


_IN_PROCESS_CAPABILITIES = (
    CadProfileCapability(
        profile=ExecutionProfile.HEADLESS,
        status=CadCapabilityStatus.VERIFIED,
        available=True,
        requires_gui_main_thread=False,
    ),
    CadProfileCapability(
        profile=ExecutionProfile.OFFSCREEN_GUI,
        status=CadCapabilityStatus.PLANNED,
        available=False,
        requires_gui_main_thread=True,
    ),
    CadProfileCapability(
        profile=ExecutionProfile.INTERACTIVE_GUI,
        status=CadCapabilityStatus.PLANNED,
        available=False,
        requires_gui_main_thread=True,
    ),
)


def _document_object_count(session: object) -> int:
    try:
        objects = tuple(session.doc.Objects)  # type: ignore[attr-defined]
    except Exception:
        raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
    return len(objects)


def _session_freecad_version(session: object) -> tuple[int, int]:
    """Read the active engine version after the Session has loaded FreeCAD."""

    raw = getattr(session, "freecad_version", None)
    if raw is None:
        try:
            session._ensure_freecad()  # type: ignore[attr-defined]
            import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

            raw = tuple(FreeCAD.Version()[:2])
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
    if type(raw) not in {tuple, list} or len(raw) != 2:
        raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)

    def component(value: object) -> int:
        if type(value) is int and value >= 0:
            return value
        if type(value) is str and value.isascii() and value.isdigit():
            return int(value)
        raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)

    return component(raw[0]), component(raw[1])


def _supported_import_object_type(obj: object) -> bool:
    object_type = getattr(obj, "TypeId", None)
    return type(object_type) is str and object_type in _PARAMETER_FIELDS


def _import_objects(
    session: object,
) -> tuple[tuple[object, ...], dict[int, EntityIdentity]]:
    """Validate the supported import envelope and snapshot existing identities."""

    try:
        objects = tuple(session.doc.Objects)  # type: ignore[attr-defined]
        pairs = tuple(session.list_object_identities())  # type: ignore[attr-defined]
    except Exception:
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None
    supported = tuple(obj for obj in objects if getattr(obj, "TypeId", None) in _PARAMETER_FIELDS)
    if not supported:
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
    if any(not _supported_import_object_type(obj) for obj in objects):
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)

    identities: dict[int, EntityIdentity] = {}
    for pair in pairs:
        if type(pair) is not tuple or len(pair) != 2:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        obj, identity = pair
        if (
            type(identity) is not EntityIdentity
            or not any(obj is current for current in supported)
            or identity.object_type != getattr(obj, "TypeId", None)
            or identity.feature_id is None
            or id(obj) in identities
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        identities[id(obj)] = identity
    return supported, identities


def _normalize_import_identities(session: object) -> tuple[EntityObservation, ...]:
    """Preserve complete identities and attach UUIDs only to untagged primitives."""

    supported, identities = _import_objects(session)
    missing = tuple(obj for obj in supported if id(obj) not in identities)
    if missing:
        try:
            document = session.doc  # type: ignore[attr-defined]
            attach = session.attach_object_identity  # type: ignore[attr-defined]
            read = session.read_object_identity  # type: ignore[attr-defined]
            document.openTransaction("VibeCAD Import Identity Normalization")
            try:
                for obj in missing:
                    identity = EntityIdentity(
                        object_id=f"object_{secrets.token_hex(16)}",
                        feature_id=f"feature_{secrets.token_hex(16)}",
                        object_type=obj.TypeId,
                        semantic_role=SemanticRole.PRIMITIVE,
                        provenance=Provenance(
                            source=ProvenanceSource.IMPORTED,
                            operation_id=None,
                        ),
                    )
                    if attach(obj, identity) != identity or read(obj) != identity:
                        raise ValueError
                document.commitTransaction()
            except BaseException:
                try:
                    document.abortTransaction()
                except Exception:
                    pass
                raise
            document.recompute()
        except ExecutorError:
            raise
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None
    return _validated_import_observations(session, expected_objects=supported)


def _validated_import_observations(
    session: object,
    *,
    expected_objects: tuple[object, ...] | None = None,
) -> tuple[EntityObservation, ...]:
    """Read a fully normalized import without attaching or repairing identities."""

    supported_after, identities_after = _import_objects(session)
    if (
        (expected_objects is not None and len(supported_after) != len(expected_objects))
        or len(identities_after) != len(supported_after)
        or (
            expected_objects is not None
            and any(
                not any(after is before for after in supported_after) for before in expected_objects
            )
        )
    ):
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
    try:
        observations = _entity_observations(session)
    except _ObservationFailure:
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None
    if len(observations) != len(supported_after):
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
    return observations


class InProcessCadExecutor(CadExecutionPort):
    """Compose validated programs with isolated CAD candidate Sessions."""

    __slots__ = ("_store",)

    def __init__(self, *, store: LocalRevisionStore) -> None:
        if type(store) is not LocalRevisionStore:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        self._store = store

    @property
    def execution_profile(self) -> ExecutionProfile:
        """Return the only execution profile verified by this implementation."""

        return ExecutionProfile.HEADLESS

    @property
    def capabilities(self) -> tuple[CadProfileCapability, ...]:
        """Return immutable truthful profile capabilities without probing FreeCAD."""

        return _IN_PROCESS_CAPABILITIES

    def validate_program(self, program: ModelProgram) -> ValidatedProgram:
        """Validate a raw ModelProgram before any project or CAD mutation."""

        if type(program) is not ModelProgram:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        validated = validate_model_program(program)
        if any(
            ExecutionProfile.HEADLESS not in command.execution_profiles
            for command in validated.commands
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        return validated

    def validate_import(self, path: Path) -> ValidatedImportEvidence:
        """Normalize and seal one private Box/Cylinder FCStd staging file."""

        if not isinstance(path, Path) or path.suffix.lower() != ".fcstd":
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        session = self.load_fcstd(path)
        normalized: tuple[EntityObservation, ...] | None = None
        failed: ExecutorError | None = None
        try:
            try:
                session.doc.recompute()
            except Exception:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
            normalized = _normalize_import_identities(session)
            self.checkpoint_fcstd(session, path)
        except ExecutorError as error:
            failed = error
        finally:
            try:
                self.close(session)
            except ExecutorError as close_error:
                if failed is None:
                    failed = close_error
        if failed is not None:
            raise failed
        assert normalized is not None
        try:
            normalized_artifact = _read_artifact(path, "fcstd")
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None

        probe = self.load_fcstd(path)
        reloaded: tuple[EntityObservation, ...] | None = None
        failed = None
        try:
            try:
                probe.doc.recompute()
            except Exception:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
            reloaded = _validated_import_observations(probe)
        except ExecutorError as error:
            failed = error
        finally:
            try:
                self.close(probe)
            except ExecutorError as close_error:
                if failed is None:
                    failed = close_error
        if failed is not None:
            raise failed
        if not _same_import_observations(reloaded, normalized):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            artifact = _read_artifact(path, "fcstd")
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if artifact != normalized_artifact:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        return ValidatedImportEvidence(
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
        )

    def revalidate_normalized_import(self, path: Path) -> ValidatedImportEvidence:
        """Read-only revalidation of one descriptor-pinned normalized import.

        The caller supplies only a fixed relative basename while holding its
        parent directory capability.  This boundary never repairs identities or
        invokes any persistence API: it hashes the artifact around one CAD
        load/recompute/observation cycle and rejects all intervening drift.
        """

        if (
            type(path) is not type(Path())
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name != path.parts[0]
            or path.suffix != ".FCStd"
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)

        try:
            before_identity = _stat_identity(os.lstat(path))
            before = _read_artifact(path, "fcstd")
        except BaseException:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None

        try:
            session = _Session()
        except BaseException:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None

        cad_failed = False
        try:
            try:
                session.load_document(path)
                session.doc.recompute()
                _validated_import_observations(session)
            except BaseException:
                cad_failed = True
        finally:
            try:
                session.close_document()
            except BaseException:
                cad_failed = True
        if cad_failed:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE)

        try:
            after_identity = _stat_identity(os.lstat(path))
        except BaseException:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if after_identity != before_identity:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            after = _read_artifact(path, "fcstd")
        except BaseException:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if after != before:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        return ValidatedImportEvidence(
            sha256=after.sha256,
            size_bytes=after.size_bytes,
        )

    def validate_materialization(
        self,
        *,
        fcstd: Path,
        step: Path,
    ) -> ValidatedMaterializationEvidence:
        """Reload and validate one immutable delivery pair without modifying it."""

        if (
            not isinstance(fcstd, Path)
            or not isinstance(step, Path)
            or fcstd.name != "model.FCStd"
            or step.name != "model.step"
            or fcstd.parent != step.parent
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            fcstd_identity = _stat_identity(os.lstat(fcstd))
            step_identity = _stat_identity(os.lstat(step))
            fcstd_before = _read_artifact(fcstd, "fcstd")
            step_before = _read_artifact(step, "step")
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        except OSError:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None

        session = self.load_fcstd(fcstd)
        failed: ExecutorError | None = None
        try:
            try:
                session.doc.recompute()
                _shape_observation(session)
                _entity_observations(session)
            except Exception:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        except ExecutorError as error:
            failed = error
        finally:
            try:
                self.close(session)
            except ExecutorError as close_error:
                if failed is None:
                    failed = close_error
        if failed is not None:
            raise failed

        try:
            identity_changed = (
                _stat_identity(os.lstat(fcstd)) != fcstd_identity
                or _stat_identity(os.lstat(step)) != step_identity
            )
        except OSError:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if identity_changed:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            fcstd_after = _read_artifact(fcstd, "fcstd")
            step_after = _read_artifact(step, "step")
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if fcstd_after != fcstd_before or step_after != step_before:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        return ValidatedMaterializationEvidence(
            fcstd_sha256=fcstd_after.sha256,
            fcstd_size_bytes=fcstd_after.size_bytes,
            step_sha256=step_after.sha256,
            step_size_bytes=step_after.size_bytes,
        )

    def create_empty(self, *, revision_id: str) -> object:
        """Create an isolated Session and trusted revision-owned document."""

        if type(revision_id) is not str or _REVISION_PATTERN.fullmatch(revision_id) is None:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            session = _Session()
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            suffix = revision_id.removeprefix("revision_")
            session.open_document(f"VibeCADCandidate_{suffix}")
        except Exception:
            try:
                session.close_document()
            except Exception:
                pass
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        return session

    def load_fcstd(self, path: Path) -> object:
        """Load one validated FCStd into a newly owned Session."""

        try:
            _read_artifact(path, "fcstd")
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        try:
            session = _Session()
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            session.load_document(path)
        except Exception:
            try:
                session.close_document()
            except Exception:
                pass
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        return session

    def checkpoint_fcstd(self, session: object, path: Path) -> None:
        """Checkpoint one Session through public persistence and document APIs."""

        if session is None or not isinstance(path, Path) or path.suffix.lower() != ".fcstd":
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            document = session.doc
            document.recompute()
            session.persist_state()
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            temporary = _fresh_checkpoint_path(path)
        except Exception:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        try:
            try:
                with _silence_fd1():
                    document.saveCopy(str(temporary))
            except Exception:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
            try:
                saved = _read_artifact(temporary, "fcstd")
                os.chmod(temporary, 0o600)
                if _read_artifact(temporary, "fcstd") != saved:
                    raise _ArtifactReadFailure
                os.replace(temporary, path)
                temporary = None
                if _read_artifact(path, "fcstd") != saved:
                    raise _ArtifactReadFailure
            except (_ArtifactReadFailure, OSError):
                raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        finally:
            if temporary is not None:
                _remove_failed_artifact(temporary)

    def close(self, session: object) -> None:
        """Close one owned Session exactly once."""

        if session is None:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            session.close_document()
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None

    def execute_program(
        self,
        *,
        program: ValidatedProgram,
        candidate: ActiveCandidate,
    ) -> tuple[NormalizedToolOutcome, ...]:
        """Execute one authentic program using the six fixed CAD bindings."""

        if type(candidate) is not ActiveCandidate:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        if type(program) is not ValidatedProgram:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            program.require_authentic()
            program = self.validate_program(program.program)
            source = program.program
            if source.base_revision != candidate.base_head.revision_id:
                raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
            selectors = tuple(
                selector
                for command in program.commands
                for selector in _bound_selectors(command.handler_kwargs)
            )
            if any(
                selector.project_id != candidate.project_id
                or selector.revision_id != candidate.base_head.revision_id
                for selector in selectors
            ):
                raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
            fixed_leaves = (_add_box, _add_cylinder, _modify_part, _move_part, _rotate_part)
            if not all(callable(item) for item in fixed_leaves):
                raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
            session = candidate.binding.session
            contexts: dict[str, deque[_InvocationContext]] = {}
            for command in program.commands:
                contexts.setdefault(command.handler_name, deque()).append(
                    _InvocationContext(
                        operation_id=command.id,
                        operation=command.operation,
                        preserve=command.preserve,
                        source=command.source,
                    )
                )
            project_id = candidate.project_id
            revision_id = candidate.base_head.revision_id
            handlers = {
                "create_box": _queued_handler(
                    contexts.get("create_box", deque()),
                    partial(
                        _managed_create,
                        session,
                        leaf=_add_box,
                        expected_type="Part::Box",
                    ),
                ),
                "create_cylinder": _queued_handler(
                    contexts.get("create_cylinder", deque()),
                    partial(
                        _managed_create,
                        session,
                        leaf=_add_cylinder,
                        expected_type="Part::Cylinder",
                    ),
                ),
                "modify_parameter": _queued_handler(
                    contexts.get("modify_parameter", deque()),
                    partial(
                        _managed_modify_parameter,
                        session,
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "move_part": _queued_handler(
                    contexts.get("move_part", deque()),
                    partial(
                        _managed_move_part,
                        session,
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "rotate_part": _queued_handler(
                    contexts.get("rotate_part", deque()),
                    partial(
                        _managed_rotate_part,
                        session,
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "inspect_model": _queued_handler(
                    contexts.get("inspect_model", deque()),
                    partial(_managed_inspect, session),
                ),
            }
        except ExecutorError:
            raise
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None
        try:
            freecad_version = _session_freecad_version(session)
            return _execute_validated_program(
                program,
                handlers,
                execution_profile=self.execution_profile,
                revision=candidate.binding.revision_id,
                freecad_version=freecad_version,
                gui_main_thread=False,
                object_count=partial(_document_object_count, session),
            )
        except _AdapterError:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None

    def export_step(
        self,
        *,
        candidate: CheckpointedCandidate,
        lease: ProjectWriteLease,
    ) -> None:
        """Export STEP once to the store-derived candidate artifact path."""

        if type(candidate) is not CheckpointedCandidate:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        if (
            type(lease) is not ProjectWriteLease
            or lease.project_id != candidate.project_id
            or lease.released is not False
        ):
            raise _fixed_error(ExecutorErrorCode.INVALID_LEASE)
        try:
            trusted_path = self._store.candidate_artifact_path(
                candidate.project_id,
                candidate.binding.revision_id,
                "step",
                lease,
            )
        except RevisionStoreError as error:
            code = (
                ExecutorErrorCode.INVALID_LEASE
                if error.code is RevisionStoreErrorCode.INVALID_LEASE
                else ExecutorErrorCode.INTEGRITY_FAILURE
            )
            raise _fixed_error(code) from None
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if (
            not isinstance(trusted_path, Path)
            or trusted_path != candidate.step_path
            or trusted_path.name != "model.step"
            or candidate.model_path.name != "model.FCStd"
            or trusted_path.parent != candidate.model_path.parent
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            existing = os.lstat(trusted_path)
        except FileNotFoundError:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        except OSError:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        placeholder_identity = _step_placeholder_identity(existing)
        if placeholder_identity is None:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE)
        try:
            parent = os.lstat(trusted_path.parent)
            if not stat.S_ISDIR(parent.st_mode):
                raise _ArtifactReadFailure
            shape = _managed_assembly_shape(candidate.binding.session)
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            with _silence_fd1():
                shape.exportStep(str(trusted_path))
            after_export = os.lstat(trusted_path)
            if not _step_output_matches_placeholder(
                after_export,
                placeholder_identity,
            ):
                raise _ArtifactReadFailure
            _read_artifact(trusted_path, "step")
            after_read = os.lstat(trusted_path)
            if not _step_output_matches_placeholder(
                after_read,
                placeholder_identity,
            ):
                raise _ArtifactReadFailure
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        except Exception:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None

    def collect_evidence(self, *, candidate: SealedCandidate) -> CandidateEvidence:
        """Collect immutable artifact facts and direct sealed geometry facts."""

        if type(candidate) is not SealedCandidate:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        try:
            durable = self._store.load_revision(
                candidate.project_id,
                candidate.revision.id,
            )
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if durable != candidate.revision:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        model_ref, step_ref = _require_revision_layout(durable)
        try:
            model_path = self._store.revision_model_path(candidate.project_id, durable.id)
            step_path = self._store.revision_artifact_path(
                candidate.project_id,
                durable.id,
                step_ref.id,
            )
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        try:
            model_actual = _read_artifact(model_path, "fcstd")
            step_actual = _read_artifact(step_path, "step")
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if not _artifact_matches(model_actual, model_ref) or not _artifact_matches(
            step_actual,
            step_ref,
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            confirmed = self._store.load_revision(candidate.project_id, durable.id)
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if confirmed != durable or confirmed != candidate.revision:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            live_shape = _shape_observation(candidate.binding.session)
            live_entities = _entity_observations(candidate.binding.session)
        except _ObservationFailure:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            sealed_shape, entities = _reloaded_observations(
                model_path,
                include_shape=True,
            )
        except _ObservationFailure:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            model_after_reload = _read_artifact(model_path, "fcstd")
            step_after_reload = _read_artifact(step_path, "step")
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if (
            model_after_reload != model_actual
            or step_after_reload != step_actual
            or not _artifact_matches(model_after_reload, model_ref)
            or not _artifact_matches(step_after_reload, step_ref)
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        if sealed_shape is None or sealed_shape != live_shape or entities != live_entities:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)

        try:
            base = self._store.load_revision(
                candidate.project_id,
                candidate.base_head.revision_id,
            )
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if (
            type(base) is not RevisionRef
            or base.id != candidate.base_head.revision_id
            or base.project_id != candidate.project_id
            or base.manifest_sha256 != candidate.base_head.manifest_sha256
            or durable.base_revision != base.id
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        before_entities: tuple[EntityObservation, ...] = ()
        base_path: Path | None = None
        base_actual: _ArtifactSnapshot | None = None
        if base.model is not None:
            if (base.model.name, base.model.format) != ("model.FCStd", "fcstd"):
                raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
            try:
                base_path = self._store.revision_model_path(candidate.project_id, base.id)
                base_actual = _read_artifact(base_path, "fcstd")
            except _ArtifactReadFailure:
                raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
            except Exception:
                raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
            if not _artifact_matches(base_actual, base.model):
                raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
            try:
                _, before_entities = _reloaded_observations(
                    base_path,
                    include_shape=False,
                )
            except _ObservationFailure:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
            try:
                base_after_reload = _read_artifact(base_path, "fcstd")
            except _ArtifactReadFailure:
                raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
            if base_after_reload != base_actual or not _artifact_matches(
                base_after_reload,
                base.model,
            ):
                raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            final_model = _read_artifact(model_path, "fcstd")
            final_step = _read_artifact(step_path, "step")
            final_durable = self._store.load_revision(candidate.project_id, durable.id)
            final_base = self._store.load_revision(candidate.project_id, base.id)
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        if (
            final_model != model_actual
            or final_step != step_actual
            or not _artifact_matches(final_model, model_ref)
            or not _artifact_matches(final_step, step_ref)
            or final_durable != durable
            or final_base != base
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        if base.model is not None:
            assert base_path is not None and base_actual is not None
            try:
                final_base_model = _read_artifact(base_path, "fcstd")
            except _ArtifactReadFailure:
                raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
            if final_base_model != base_actual or not _artifact_matches(
                final_base_model,
                base.model,
            ):
                raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        try:
            preservations = _preservation_observations(before_entities, entities)
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE) from None
        snapshot = ObservationSnapshot(
            candidate_revision=durable.id,
            shapes=(sealed_shape,),
            artifacts=(
                ArtifactObservation(
                    target="export",
                    exists=True,
                    non_empty=True,
                    format="step",
                ),
                ArtifactObservation(
                    target="model",
                    exists=True,
                    non_empty=True,
                    format="fcstd",
                ),
            ),
            entities=entities,
            preservations=preservations,
        )
        artifacts = (
            TaskArtifactRef(
                id=model_ref.id,
                name=model_ref.name,
                format=model_ref.format,
                sha256=model_ref.sha256,
                size_bytes=model_ref.size_bytes,
                candidate_revision=durable.id,
            ),
            TaskArtifactRef(
                id=step_ref.id,
                name=step_ref.name,
                format=step_ref.format,
                sha256=step_ref.sha256,
                size_bytes=step_ref.size_bytes,
                candidate_revision=durable.id,
            ),
        )
        return CandidateEvidence(snapshot=snapshot, artifacts=artifacts)


__all__ = [
    "ExecutorErrorCode",
    "ExecutorError",
    "CandidateEvidence",
    "InProcessCadExecutor",
]
