"""Trusted in-process CAD execution and sealed-observation boundary.

The executor binds only the four operations in the default ModelProgram
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
    CadSnapshotPort,
    CheckpointedCandidate,
    SealedCandidate,
)
from vibecad.execution.registry import ExecutionProfile
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
)
from vibecad.feedback.text import describe_shape as _describe_shape
from vibecad.freecad_env import silence_fd1 as _silence_fd1
from vibecad.tools.modeling import add_box as _add_box
from vibecad.tools.modify import modify_part as _modify_part
from vibecad.validation import (
    ArtifactObservation,
    EntityObservation,
    EntityParameterObservation,
    ObservationSnapshot,
    PreservationObservation,
    ShapeObservation,
    compare_entity_preservation,
)
from vibecad.workflow.contracts import ModelProgram
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


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateEvidence:
    """Trusted sealed observations and path-free durable artifact references."""

    snapshot: ObservationSnapshot
    artifacts: tuple[TaskArtifactRef, ...]

    def __post_init__(self) -> None:
        if type(self.snapshot) is not ObservationSnapshot:
            raise ExecutorError(ExecutorErrorCode.INVALID_INPUT)
        if type(self.artifacts) is not tuple or len(self.artifacts) != 2:
            raise ExecutorError(ExecutorErrorCode.INVALID_INPUT)
        if not all(type(item) is TaskArtifactRef for item in self.artifacts):
            raise ExecutorError(ExecutorErrorCode.INVALID_INPUT)
        if tuple(item.name for item in self.artifacts) != ("model.FCStd", "model.step"):
            raise ExecutorError(ExecutorErrorCode.INVALID_INPUT)
        if tuple(item.format for item in self.artifacts) != ("fcstd", "step"):
            raise ExecutorError(ExecutorErrorCode.INVALID_INPUT)
        if any(
            item.candidate_revision != self.snapshot.candidate_revision for item in self.artifacts
        ):
            raise ExecutorError(ExecutorErrorCode.INVALID_INPUT)


@dataclass(frozen=True, slots=True)
class _ArtifactSnapshot:
    sha256: str
    size_bytes: int


class _ArtifactReadFailure(Exception):
    """Private marker whose details never cross the executor boundary."""


class _ObservationFailure(Exception):
    """Private marker whose details never cross the executor boundary."""


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


def _shape_observation(session: object) -> ShapeObservation:
    try:
        shape = session.get_assembly_shape()
        volume = _finite_number(shape.Volume, nonnegative=True)
        area = _finite_number(shape.Area, nonnegative=True)
        bound_box = shape.BoundBox
        bbox = (
            _finite_number(bound_box.XLength, nonnegative=True),
            _finite_number(bound_box.YLength, nonnegative=True),
            _finite_number(bound_box.ZLength, nonnegative=True),
        )
        center = shape.CenterOfMass
        center_of_mass = (
            _finite_number(center.x, nonnegative=False),
            _finite_number(center.y, nonnegative=False),
            _finite_number(center.z, nonnegative=False),
        )
        valid_shape = shape.isValid()
        if type(valid_shape) is not bool:
            raise _ObservationFailure
        solid_count = len(shape.Solids)
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
    quaternion = tuple(
        _finite_number(component, nonnegative=False) for component in raw_quaternion
    )
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
            obj
            for obj in document_objects
            if getattr(obj, "TypeId", None) in _PARAMETER_FIELDS
        )
        if any(
            sum(current is obj for current, _ in pairs) != 1
            for obj in modelable_objects
        ) or any(
            not any(current is obj for obj in document_objects)
            for current, _ in pairs
        ):
            raise _ObservationFailure
        observations = tuple(
            sorted(
                (_entity_observation(obj, identity) for obj, identity in pairs),
                key=lambda item: item.object_id,
            )
        )
        if len({item.object_id for item in observations}) != len(observations):
            raise _ObservationFailure
        feature_ids = tuple(
            item.feature_id for item in observations if item.feature_id is not None
        )
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
            compare_entity_preservation(
                old, new, target=target
            )
            for target in targets
        )
    return tuple(sorted(comparisons, key=lambda item: item.target))


def _bound_selectors(value: object) -> tuple[SelectorV1, ...]:
    if type(value) is SelectorV1:
        return (value,)
    if type(value) is MappingProxyType:
        return tuple(
            selector for item in value.values() for selector in _bound_selectors(item)
        )
    if type(value) is tuple:
        return tuple(selector for item in value for selector in _bound_selectors(item))
    return ()


def _managed_add_box(session: object, **kwargs: object) -> object:
    """Run the fixed box primitive and attach managed identity before sealing."""

    result = _add_box(session, **kwargs)
    attach = getattr(session, "attach_object_identity", None)
    read_identity = getattr(session, "read_object_identity", None)
    if not callable(attach) or not callable(read_identity):
        raise RuntimeError("managed object identity is unavailable")
    try:
        if type(result) is not dict:
            raise ValueError
        name = result["name"]
        if type(name) is not str or not name:
            raise ValueError
        obj = session.get_object(name)
        object_type = obj.TypeId
        if type(object_type) is not str:
            raise ValueError
        identity = EntityIdentity(
            object_id=f"object_{secrets.token_hex(16)}",
            feature_id=f"feature_{secrets.token_hex(16)}",
            object_type=object_type,
            semantic_role=SemanticRole.PRIMITIVE,
            provenance=Provenance(
                source=ProvenanceSource.MODEL,
                operation_id=None,
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
        raise RuntimeError("managed object identity could not be attached") from None
    return result


def _describe_part(session: object) -> dict[str, object]:
    """Return execution feedback only; this value is never trusted evidence."""

    return _describe_shape(session.get_assembly_shape())


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


class InProcessCadExecutor(CadSnapshotPort):
    """Compose validated programs with isolated CAD candidate Sessions."""

    __slots__ = ("_store",)

    def __init__(self, *, store: LocalRevisionStore) -> None:
        if type(store) is not LocalRevisionStore:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        self._store = store

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
        """Execute one authentic program using the three fixed CAD bindings."""

        if type(candidate) is not ActiveCandidate:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        if type(program) is not ValidatedProgram:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            program.require_authentic()
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
            session = candidate.binding.session
            handlers = {
                "add_box": partial(_managed_add_box, session),
                "modify_part": partial(_modify_part, session),
                "describe_part": partial(_describe_part, session),
            }
        except ExecutorError:
            raise
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None
        try:
            return _execute_validated_program(
                program,
                handlers,
                execution_profile=ExecutionProfile.HEADLESS,
                revision=candidate.binding.revision_id,
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
            os.lstat(trusted_path)
        except FileNotFoundError:
            pass
        except OSError:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        else:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE)
        try:
            parent = os.lstat(trusted_path.parent)
            if not stat.S_ISDIR(parent.st_mode):
                raise _ArtifactReadFailure
            shape = candidate.binding.session.get_assembly_shape()
        except _ArtifactReadFailure:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            with _silence_fd1():
                shape.exportStep(str(trusted_path))
            _read_artifact(trusted_path, "step")
        except _ArtifactReadFailure:
            _remove_failed_artifact(trusted_path)
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        except Exception:
            _remove_failed_artifact(trusted_path)
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
