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
from vibecad.execution.results import NormalizedToolOutcome
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.feedback.text import describe_shape as _describe_shape
from vibecad.freecad_env import silence_fd1 as _silence_fd1
from vibecad.tools.modeling import add_box as _add_box
from vibecad.tools.modeling import new_document as _new_document
from vibecad.tools.modify import modify_part as _modify_part
from vibecad.validation import (
    ArtifactObservation,
    ObservationSnapshot,
    ShapeObservation,
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
        return validate_model_program(program)

    def create_empty(self, *, revision_id: str) -> object:
        """Create an isolated Session without opening a document."""

        if type(revision_id) is not str or _REVISION_PATTERN.fullmatch(revision_id) is None:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            return _Session()
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None

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
        """Execute one authentic program using the four fixed CAD bindings."""

        if type(candidate) is not ActiveCandidate:
            raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
        if type(program) is not ValidatedProgram:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            program.require_authentic()
            source = program.program
            if source.base_revision != candidate.base_head.revision_id:
                raise _fixed_error(ExecutorErrorCode.INVALID_CANDIDATE)
            session = candidate.binding.session
            handlers = {
                "new_document": partial(_new_document, session),
                "add_box": partial(_add_box, session),
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
            shape = _shape_observation(candidate.binding.session)
        except _ObservationFailure:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        snapshot = ObservationSnapshot(
            candidate_revision=durable.id,
            shapes=(shape,),
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
