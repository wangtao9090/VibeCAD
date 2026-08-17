"""Trusted in-process CAD execution and sealed-observation boundary.

The executor binds only operations in the default ModelProgram registry.  It
never accepts a handler mapping, output path, observation, or
retry policy from the program.  STEP export and verification evidence are
derived from coordinator-owned candidate capabilities and the immutable local
revision store.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import secrets
import stat
import zipfile
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import MappingProxyType

from vibecad.engine.session import Session as _Session
from vibecad.engine.session import SessionLifecycleError as _SessionLifecycleError
from vibecad.execution.adapter import (
    AdapterError as _AdapterError,
)
from vibecad.execution.adapter import (
    _prepare_validated_program_execution as _prepare_validated_program_execution,
)
from vibecad.execution.adapter import (
    execute_validated_program as _execute_validated_program,
)
from vibecad.execution.candidate import (
    ActiveCandidate,
    CheckpointedCandidate,
    SealedCandidate,
)
from vibecad.execution.errors import ExecutorError, ExecutorErrorCode
from vibecad.execution.freecad_reviewed_intent_execution import (
    ReviewedNativeExecutionResult,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    execute_reviewed_intent_native as _execute_reviewed_intent_native,
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
    ReleaseCadEvidence,
    ValidatedImportEvidence,
    ValidatedMaterializationEvidence,
)
from vibecad.parametric.compiler import (
    compile_parametric_design as _compile_parametric_design,
)
from vibecad.parametric.compiler import (
    modify_parametric_parameter as _modify_parametric_parameter,
)
from vibecad.parametric.compiler import (
    parametric_entity_facts,
)
from vibecad.parametric.compiler import (
    stabilize_parametric_session as _stabilize_parametric_session,
)
from vibecad.parametric.contracts import ParametricDesignIR
from vibecad.tools.modeling import (
    _boolean_common_uncommitted,
    _boolean_cut_uncommitted,
    _boolean_fuse_uncommitted,
)
from vibecad.tools.modeling import add_box as _add_box
from vibecad.tools.modeling import add_cone as _add_cone
from vibecad.tools.modeling import add_cylinder as _add_cylinder
from vibecad.tools.modeling import add_sphere as _add_sphere
from vibecad.tools.modeling import add_torus as _add_torus
from vibecad.tools.modify import _modify_part_uncommitted
from vibecad.tools.modify import modify_part as _modify_part
from vibecad.tools.transform import _move_part_uncommitted, _rotate_part_uncommitted
from vibecad.tools.transform import move_part as _move_part
from vibecad.tools.transform import rotate_part as _rotate_part
from vibecad.validation import (
    ArtifactObservation,
    BomConflictObservation,
    BomObservation,
    BomRowObservation,
    ComponentBomMetadata,
    ComponentObservation,
    EntityObservation,
    EntityParameterObservation,
    InterferenceObservation,
    ObservationSnapshot,
    PreservationObservation,
    ShapeObservation,
    compare_entity_preservation,
)
from vibecad.workflow.contracts import ModelProgram, ValueSource
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram, validate_model_program
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1
from vibecad.workflow.state import TaskArtifactRef

_MAX_ARTIFACT_BYTES = 536_870_912
_READ_CHUNK_BYTES = 1024 * 1024
_SIGNATURE_WINDOW_BYTES = 1024 * 1024
_MAX_ZIP_ENTRIES = 4096
_CHECKPOINT_NAME_ATTEMPTS = 8
_REVISION_PATTERN = re.compile(r"revision_[0-9a-f]{32}")


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
    "Part::Cone": (
        ("angle", "Angle", "deg"),
        ("base_radius", "Radius1", "mm"),
        ("height", "Height", "mm"),
        ("top_radius", "Radius2", "mm"),
    ),
    "Part::Sphere": (
        ("angle1", "Angle1", "deg"),
        ("angle2", "Angle2", "deg"),
        ("angle3", "Angle3", "deg"),
        ("radius", "Radius", "mm"),
    ),
    "Part::Torus": (
        ("angle1", "Angle1", "deg"),
        ("angle2", "Angle2", "deg"),
        ("angle3", "Angle3", "deg"),
        ("major_radius", "Radius1", "mm"),
        ("minor_radius", "Radius2", "mm"),
    ),
}
_BOOLEAN_OPERATIONS = {
    "Part::Common": "common",
    "Part::Cut": "cut",
    "Part::Fuse": "fuse",
}
_NATIVE_ENTITY_TYPES = frozenset((*_PARAMETER_FIELDS, *_BOOLEAN_OPERATIONS))


def _native_dependency_graph(
    pairs: tuple[tuple[object, EntityIdentity], ...],
) -> tuple[
    dict[str, tuple[object, EntityIdentity]],
    dict[str, tuple[str, str]],
    frozenset[str],
]:
    """Validate one bounded native Part forest and return its exact links.

    Boolean operands may themselves be prior boolean roots, but every native
    object has at most one consumer.  This keeps the document a forest rather
    than a shared-expression DAG whose overlapping roots would be ambiguous in
    an assembly observation or STEP export.
    """

    modelable = tuple(
        (obj, identity) for obj, identity in pairs if identity.object_type in _NATIVE_ENTITY_TYPES
    )
    by_name: dict[str, tuple[object, EntityIdentity]] = {}
    for obj, identity in modelable:
        name = getattr(obj, "Name", None)
        is_boolean = identity.object_type in _BOOLEAN_OPERATIONS
        key = name if type(name) is str and name else identity.object_id
        expected_role = SemanticRole.FEATURE if is_boolean else SemanticRole.PRIMITIVE
        if (
            (is_boolean and (type(name) is not str or not name))
            or key in by_name
            or identity.semantic_role is not expected_role
            or (is_boolean and identity.feature_id is None)
        ):
            raise _ObservationFailure
        by_name[key] = (obj, identity)

    dependencies: dict[str, tuple[str, str]] = {}
    consumers: dict[str, str] = {}
    for name, (obj, identity) in by_name.items():
        if identity.object_type not in _BOOLEAN_OPERATIONS:
            continue
        operand_names = tuple(
            getattr(getattr(obj, relation, None), "Name", None) for relation in ("Base", "Tool")
        )
        if (
            any(type(item) is not str or item not in by_name for item in operand_names)
            or len(set(operand_names)) != 2
        ):
            raise _ObservationFailure
        base_name, tool_name = operand_names
        assert type(base_name) is str and type(tool_name) is str
        if by_name[base_name][0] is not getattr(obj, "Base", None) or by_name[tool_name][
            0
        ] is not getattr(obj, "Tool", None):
            raise _ObservationFailure
        for operand_name in (base_name, tool_name):
            if operand_name in consumers:
                raise _ObservationFailure
            consumers[operand_name] = name
        dependencies[name] = (base_name, tool_name)

    resolved = {name for name in by_name if name not in dependencies}
    pending = set(dependencies)
    while pending:
        ready = tuple(
            name for name in sorted(pending) if set(dependencies[name]).issubset(resolved)
        )
        if not ready:
            raise _ObservationFailure
        resolved.update(ready)
        pending.difference_update(ready)
    if resolved != set(by_name):
        raise _ObservationFailure
    return by_name, dependencies, frozenset(consumers)


def _native_boolean_descendants(
    dependencies: dict[str, tuple[str, str]],
    *,
    target_name: str,
) -> tuple[str, ...]:
    """Return the unique ordered consumer chain for one native operand."""

    consumer_by_operand = {
        operand: feature_name
        for feature_name, operands in dependencies.items()
        for operand in operands
    }
    descendants: list[str] = []
    current = target_name
    while current in consumer_by_operand:
        current = consumer_by_operand[current]
        descendants.append(current)
    return tuple(descendants)


def _require_recomputed_boolean_descendants(
    by_name: dict[str, tuple[object, EntityIdentity]],
    descendant_names: tuple[str, ...],
    recomputed_objects: frozenset[int],
    target: object,
) -> frozenset[str]:
    """Consume one executor-created dependency-execution challenge.

    A legitimate operand edit can leave the final BRep mathematically
    unchanged (for example, moving one solid wholly inside a fused base).  The
    old aggregate-metric test therefore rejected correct edits.  Freshness is
    instead proved by the reviewed dependency links, a short-lived FreeCAD
    document-observer receipt, and the post-recompute ``State`` ledger.  The
    leaf's full recompute must emit an event for the exact target and every
    authenticated descendant, leave neither ``Touched`` nor ``Invalid``
    behind, and leave the whole document clean.  The returned object-id set is
    consumed by the observation validator so an incomplete receipt cannot
    silently skip one descendant.
    """

    if id(target) not in recomputed_objects:
        raise _operation_failure()
    recomputed: set[str] = set()
    for name in descendant_names:
        try:
            obj, identity = by_name[name]
            state = tuple(obj.State)
        except Exception:
            raise _operation_failure() from None
        if (
            any(type(item) is not str for item in state)
            or id(obj) not in recomputed_objects
            or "Touched" in state
            or "Invalid" in state
        ):
            raise _operation_failure()
        recomputed.add(identity.object_id)
    if len(recomputed) != len(descendant_names):
        raise _operation_failure()
    try:
        document = by_name[descendant_names[0]][0].Document
        touched = document.isTouched()
    except Exception:
        raise _operation_failure() from None
    if type(touched) is not bool or touched:
        raise _operation_failure()
    return frozenset(recomputed)


@dataclass(slots=True)
class _RecomputeReceipt:
    document: object
    object_ids: set[int]

    def slotRecomputedObject(self, obj: object) -> None:  # noqa: N802 - FreeCAD API
        if getattr(obj, "Document", None) is self.document:
            self.object_ids.add(id(obj))


class _DocumentRecomputeObserver:
    """Short-lived FreeCAD observer proving which objects this leaf recomputed."""

    def __init__(self, document: object) -> None:
        name = getattr(document, "Name", None)
        if type(name) is not str or not name:
            raise _operation_failure()
        self._receipt = _RecomputeReceipt(document=document, object_ids=set())
        self._freecad: object | None = None

    def __enter__(self) -> _RecomputeReceipt:
        try:
            with _silence_fd1():
                import FreeCAD  # noqa: PLC0415

                FreeCAD.addDocumentObserver(self._receipt)
        except Exception:
            raise _operation_failure() from None
        self._freecad = FreeCAD
        return self._receipt

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        try:
            assert self._freecad is not None
            self._freecad.removeDocumentObserver(self._receipt)  # type: ignore[attr-defined]
        except Exception:
            raise _operation_failure() from None


def _validated_explicit_component_roots(
    session: object,
    *,
    pairs: tuple[tuple[object, EntityIdentity], ...] | None = None,
    records: tuple[tuple[object, ...], ...] | None = None,
    require_complete: bool = True,
) -> dict[str, object]:
    """Authenticate the reviewed native-Part component result profile.

    Native Part operands are hidden by their Boolean result in FreeCAD, but
    remain document members.  A persisted or externally changed component
    registry must therefore keep every operand and its consuming feature under
    the same explicit component, and a component result may never point back
    to a consumed operand.  Entity-level observation permits multiple
    unconsumed roots while a later Boolean is being assembled.  Component and
    export boundaries set ``require_complete`` and require exactly one root.

    FreeCAD ``OutList`` is deliberately not authority here: it mixes consuming
    geometry links with attachment, support, containment, and arbitrary dynamic
    properties.  The only consuming edges accepted by this profile are the
    reviewed native Boolean ``Base``/``Tool`` links validated by
    :func:`_native_dependency_graph`.  A new object family must add an explicit
    semantic profile before it can pass component delivery; unknown families
    fail closed instead of being hidden behind a plausible result shape.
    """

    list_records = getattr(session, "list_component_identity_records", None)
    if not callable(list_records):
        return {}
    try:
        component_records = tuple(list_records()) if records is None else records
        if not component_records:
            return {}
        list_identities = getattr(session, "list_object_identities", None)
        get_result_object = getattr(session, "get_result_object", None)
        if pairs is None and (not callable(list_identities) or not callable(get_result_object)):
            # Compatibility-only observer fixtures do not expose managed identity
            # or result-root authority.  Production Session always exposes both.
            return {
                part_name: session.get_result_shape(part_name)  # type: ignore[attr-defined]
                for part_name, _container, _identity, members in component_records
                if members
            }
        identified = tuple(list_identities()) if pairs is None else pairs
        by_name, dependencies, consumed = _native_dependency_graph(identified)
        owner_by_name: dict[str, str] = {}
        member_names_by_part: dict[str, frozenset[str]] = {}
        ordered_parts: list[str] = []
        for record in component_records:
            if len(record) != 4:
                raise _ObservationFailure
            part_name, _container, _identity, members = record
            if type(part_name) is not str or not part_name or type(members) is not tuple:
                raise _ObservationFailure
            member_names = tuple(getattr(obj, "Name", None) for obj, _identity in members)
            if (
                any(type(name) is not str or not name for name in member_names)
                or len(member_names) != len(set(member_names))
                or part_name in member_names_by_part
            ):
                raise _ObservationFailure
            ordered_parts.append(part_name)
            member_names_by_part[part_name] = frozenset(member_names)
            for name in member_names:
                assert type(name) is str
                if name in owner_by_name:
                    raise _ObservationFailure
                owner_by_name[name] = part_name

        # Once explicit components exist, every managed native object must have
        # exactly one owner.  Otherwise a cross-component Boolean can be counted
        # once through its result and again through a leaked operand.
        if set(by_name) - set(owner_by_name):
            raise _ObservationFailure
        for feature_name, operands in dependencies.items():
            owners = {
                owner_by_name.get(feature_name),
                *(owner_by_name.get(name) for name in operands),
            }
            if None in owners or len(owners) != 1:
                raise _ObservationFailure

        roots: dict[str, object] = {}
        for part_name in ordered_parts:
            member_names = member_names_by_part[part_name]
            if not member_names:
                continue
            root = get_result_object(part_name)
            root_name = getattr(root, "Name", None)
            if (
                type(root_name) is not str
                or root_name not in member_names
                or root_name not in by_name
                or by_name[root_name][0] is not root
                or root_name in consumed
            ):
                raise _ObservationFailure
            if require_complete:
                native_members = member_names & set(by_name)
                native_roots = native_members - consumed
                if native_members != set(member_names) or native_roots != {root_name}:
                    raise _ObservationFailure
            roots[part_name] = root.Shape
        return roots
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _fixed_error(code: ExecutorErrorCode) -> ExecutorError:
    return ExecutorError(code)


def _prefer_cleanup_failure(
    current: ExecutorError | None,
    cleanup: ExecutorError,
) -> ExecutorError:
    """Never let a recoverable operation error hide a fatal lifecycle failure."""

    if current is None or cleanup.code is ExecutorErrorCode.INTERNAL_FAILURE:
        return cleanup
    return current


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

    list_components = getattr(session, "list_component_identity_records", None)
    if callable(list_components):
        records = tuple(list_components())
        if records:
            _validated_explicit_component_roots(session, records=records)
            return session.get_assembly_shape()  # type: ignore[attr-defined]
    list_identities = getattr(session, "list_object_identities", None)
    if not callable(list_identities):
        return session.get_assembly_shape()  # type: ignore[attr-defined]
    pairs = tuple(list_identities())
    by_name, _dependencies, consumed = _native_dependency_graph(pairs)
    if not by_name:
        return session.get_assembly_shape()  # type: ignore[attr-defined]
    roots = tuple(obj for name, (obj, _identity) in by_name.items() if name not in consumed)
    if not roots:
        raise _ObservationFailure
    shapes = tuple(obj.Shape for obj in roots)
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
        _stabilize_parametric_session(session)
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
        valid_shape = shape.isValid()  # type: ignore[attr-defined]
        if type(valid_shape) is not bool:
            raise _ObservationFailure
        solids = tuple(shape.Solids)  # type: ignore[attr-defined]
        solid_count = len(solids)
        if type(solid_count) is not int or solid_count < 0:
            raise _ObservationFailure
        center_of_mass = _shape_center_of_mass(shape, solids)
        return {
            "volume_mm3": _finite_number(shape.Volume, nonnegative=True),  # type: ignore[attr-defined]
            "area_mm2": _finite_number(shape.Area, nonnegative=True),  # type: ignore[attr-defined]
            "bbox_mm": (
                _finite_number(bound_box.XLength, nonnegative=True),
                _finite_number(bound_box.YLength, nonnegative=True),
                _finite_number(bound_box.ZLength, nonnegative=True),
            ),
            "center_of_mass_mm": center_of_mass,
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


def _entity_observation(
    obj: object,
    identity: EntityIdentity,
    *,
    identities_by_name: dict[str, EntityIdentity] | None = None,
) -> EntityObservation:
    try:
        standard_parameters = tuple(
            EntityParameterObservation(
                name=name,
                value=_quantity_value(getattr(obj, property_name)),
                unit=unit,
            )
            for name, property_name, unit in _PARAMETER_FIELDS.get(identity.object_type, ())
        )
        relation_parameters: tuple[EntityParameterObservation, ...] = ()
        if identity.object_type in _BOOLEAN_OPERATIONS:
            if identities_by_name is None or identity.semantic_role is not SemanticRole.FEATURE:
                raise _ObservationFailure
            base_name = getattr(getattr(obj, "Base", None), "Name", None)
            tool_name = getattr(getattr(obj, "Tool", None), "Name", None)
            base_identity = identities_by_name.get(base_name) if type(base_name) is str else None
            tool_identity = identities_by_name.get(tool_name) if type(tool_name) is str else None
            if (
                type(base_identity) is not EntityIdentity
                or type(tool_identity) is not EntityIdentity
                or base_identity == tool_identity
            ):
                raise _ObservationFailure
            relation_parameters = (
                EntityParameterObservation(
                    name="base_object_id",
                    value=base_identity.object_id,
                ),
                EntityParameterObservation(
                    name="operation",
                    value=_BOOLEAN_OPERATIONS[identity.object_type],
                ),
                EntityParameterObservation(
                    name="tool_object_id",
                    value=tool_identity.object_id,
                ),
            )
        parametric_parameters = tuple(
            EntityParameterObservation(name=fact.name, value=fact.value, unit=fact.unit)
            for fact in parametric_entity_facts(obj)
        )
        parameters = tuple(
            sorted(
                (*standard_parameters, *relation_parameters, *parametric_parameters),
                key=lambda item: item.name,
            )
        )
        placement = _canonical_placement(obj.Placement)  # type: ignore[attr-defined]
        # ``App::Part`` starts exposing an aggregate Shape after its first member is
        # added.  That aggregate belongs to the component-observation contract, not
        # to the container's identity observation; treating it as entity geometry
        # makes the same component change shape merely because membership changed.
        shape = None if identity.object_type == "App::Part" else getattr(obj, "Shape", None)
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
        _stabilize_parametric_session(session)
        document_objects = tuple(session.doc.Objects)  # type: ignore[attr-defined]
        list_identities = getattr(session, "list_object_identities", None)
        if callable(list_identities):
            pairs = tuple(list_identities())
        else:
            identities = index_entity_identities(document_objects)
            pairs = tuple(zip(document_objects, identities, strict=True))
        modelable_objects = tuple(
            obj for obj in document_objects if getattr(obj, "TypeId", None) in _NATIVE_ENTITY_TYPES
        )
        if any(
            sum(current is obj for current, _ in pairs) != 1 for obj in modelable_objects
        ) or any(not any(current is obj for obj in document_objects) for current, _ in pairs):
            raise _ObservationFailure
        named_identities = tuple(
            (name, identity)
            for obj, identity in pairs
            if type(name := getattr(obj, "Name", None)) is str and name
        )
        identities_by_name = dict(named_identities)
        if len(identities_by_name) != len(named_identities):
            raise _ObservationFailure
        _native_dependency_graph(pairs)
        _validated_explicit_component_roots(
            session,
            pairs=pairs,
            require_complete=False,
        )
        observations = tuple(
            sorted(
                (
                    _entity_observation(
                        obj,
                        identity,
                        identities_by_name=identities_by_name,
                    )
                    for obj, identity in pairs
                ),
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


def _component_observations(session: object) -> tuple[ComponentObservation, ...]:
    """Observe strict explicit components in global assembly coordinates."""

    list_records = getattr(session, "list_component_identity_records", None)
    if not callable(list_records):
        return ()
    read_bom = getattr(session, "read_component_bom_metadata", None)
    try:
        records = tuple(list_records())
        roots = _validated_explicit_component_roots(session, records=records)
        observations = []
        for part_name, container, identity, members in records:
            local_shape = roots[part_name]
            shape = local_shape.transformed(container.Placement.toMatrix())
            observations.append(
                ComponentObservation(
                    component_id=identity.object_id,
                    object_type=identity.object_type,
                    provenance=identity.provenance.to_mapping(),
                    placement=_canonical_placement(container.Placement),
                    member_object_ids=tuple(
                        member_identity.object_id for _, member_identity in members
                    ),
                    bom=(None if not callable(read_bom) else read_bom(part_name)),
                    **_entity_geometry(shape),
                )
            )
        ordered = tuple(sorted(observations, key=lambda item: item.component_id))
        if len({item.component_id for item in ordered}) != len(ordered):
            raise _ObservationFailure
        return ordered
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _interference_observations(session: object) -> tuple[InterferenceObservation, ...]:
    """Compute the complete ordered pairwise common-volume matrix."""

    list_records = getattr(session, "list_component_identity_records", None)
    if not callable(list_records):
        return ()
    try:
        records = tuple(list_records())
        roots = _validated_explicit_component_roots(session, records=records)
        global_shapes = {
            identity.object_id: roots[part_name].transformed(container.Placement.toMatrix())
            for part_name, container, identity, _members in records
        }
        observations = []
        with _silence_fd1():
            for left in range(len(records)):
                for right in range(left + 1, len(records)):
                    left_id = records[left][2].object_id
                    right_id = records[right][2].object_id
                    volume = _finite_number(
                        global_shapes[left_id].common(global_shapes[right_id]).Volume,
                        nonnegative=True,
                    )
                    observations.append(
                        InterferenceObservation(
                            component_a_id=left_id,
                            component_b_id=right_id,
                            common_volume_mm3=volume,
                            interfering=volume > 1e-6,
                        )
                    )
        return tuple(observations)
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _component_geometry_digest(
    session: object,
    part_name: str,
    members: tuple[tuple[object, EntityIdentity], ...],
) -> str:
    try:
        local_geometry = _entity_geometry(session.get_result_shape(part_name))  # type: ignore[attr-defined]
        member_facts = []
        identities_by_name = {obj.Name: identity for obj, identity in members}
        for obj, identity in members:
            observation = _entity_observation(
                obj,
                identity,
                identities_by_name=identities_by_name,
            )
            member_facts.append(
                {
                    "object_type": observation.object_type,
                    "semantic_role": observation.semantic_role,
                    "placement": list(observation.placement),
                    "parameters": [item.to_mapping() for item in observation.parameters],
                    "volume_mm3": observation.volume_mm3,
                    "area_mm2": observation.area_mm2,
                    "bbox_mm": (None if observation.bbox_mm is None else list(observation.bbox_mm)),
                    "center_of_mass_mm": (
                        None
                        if observation.center_of_mass_mm is None
                        else list(observation.center_of_mass_mm)
                    ),
                    "valid_shape": observation.valid_shape,
                    "solid_count": observation.solid_count,
                }
            )
        member_facts.sort(
            key=lambda value: json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "local_geometry": {
                key: list(value) if type(value) is tuple else value
                for key, value in local_geometry.items()
            },
            "members": member_facts,
        }
        raw = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(b"vibecad-component-geometry-v1\0" + raw).hexdigest()
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _bom_observation(
    session: object,
    components: tuple[ComponentObservation, ...] | None = None,
) -> BomObservation | None:
    list_records = getattr(session, "list_component_identity_records", None)
    if not callable(list_records):
        return None
    try:
        records = tuple(list_records())
        if not records:
            return None
        observed = _component_observations(session) if components is None else components
        by_id = {item.component_id: item for item in observed}
        claims: dict[str, list[tuple[ComponentObservation, str]]] = {}
        missing = []
        for part_name, _container, identity, members in records:
            component = by_id.get(identity.object_id)
            if component is None:
                raise _ObservationFailure
            metadata = component.bom
            if metadata is None:
                missing.append(component.component_id)
                continue
            digest = _component_geometry_digest(session, part_name, members)
            claims.setdefault(metadata.part_number, []).append((component, digest))

        rows = []
        conflicts = []
        for part_number in sorted(claims):
            group = claims[part_number]
            first_component, first_digest = group[0]
            first_metadata = first_component.bom
            assert first_metadata is not None
            consistent = all(
                component.bom == first_metadata and digest == first_digest
                for component, digest in group
            )
            component_ids = tuple(sorted(component.component_id for component, _ in group))
            if not consistent:
                conflicts.append(
                    BomConflictObservation(
                        part_number=part_number,
                        component_ids=component_ids,
                    )
                )
                continue
            volume = first_component.volume_mm3
            if volume is None or volume <= 0:
                raise _ObservationFailure
            unit_mass = float(volume) * float(first_metadata.density_kg_m3) * 1e-9
            rows.append(
                BomRowObservation(
                    part_number=part_number,
                    description=first_metadata.description,
                    material=first_metadata.material,
                    density_kg_m3=first_metadata.density_kg_m3,
                    quantity=len(component_ids),
                    unit_mass_kg=unit_mass,
                    total_mass_kg=unit_mass * len(component_ids),
                    component_ids=component_ids,
                    geometry_digest=first_digest,
                )
            )
        total_quantity = sum(item.quantity for item in rows)
        total_mass = sum(float(item.total_mass_kg) for item in rows)
        return BomObservation(
            component_count=len(records),
            rows=tuple(rows),
            missing_component_ids=tuple(sorted(missing)),
            conflicts=tuple(conflicts),
            total_quantity=total_quantity,
            total_mass_kg=total_mass,
            complete=not missing and not conflicts and total_quantity == len(records),
        )
    except _ObservationFailure:
        raise
    except Exception:
        raise _ObservationFailure from None


def _bom_csv(bom: BomObservation | None) -> str | None:
    if bom is None or not bom.complete:
        return None
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(
        (
            "part_number",
            "description",
            "material",
            "density_kg_m3",
            "quantity",
            "unit_mass_kg",
            "total_mass_kg",
            "component_ids",
            "geometry_digest",
        )
    )
    for row in bom.rows:
        writer.writerow(
            (
                row.part_number,
                row.description,
                row.material,
                row.density_kg_m3,
                row.quantity,
                row.unit_mass_kg,
                row.total_mass_kg,
                ";".join(row.component_ids),
                row.geometry_digest,
            )
        )
    return stream.getvalue()


def _reloaded_observations(
    path: Path,
    *,
    include_shape: bool,
) -> tuple[
    ShapeObservation | None,
    tuple[EntityObservation, ...],
    tuple[ComponentObservation, ...],
    tuple[InterferenceObservation, ...],
    BomObservation | None,
]:
    probe = None
    failed = False
    shape: ShapeObservation | None = None
    entities: tuple[EntityObservation, ...] = ()
    components: tuple[ComponentObservation, ...] = ()
    interferences: tuple[InterferenceObservation, ...] = ()
    bom: BomObservation | None = None
    try:
        probe = _Session()
        probe.load_document(path)
        if include_shape:
            shape = _shape_observation(probe)
        entities = _entity_observations(probe)
        components = _component_observations(probe)
        interferences = _interference_observations(probe)
        bom = _bom_observation(probe, components)
    except _SessionLifecycleError:
        raise
    except Exception:
        failed = True
    finally:
        if probe is not None:
            try:
                probe.close_document()
            except _SessionLifecycleError:
                raise
            except Exception:
                failed = True
    if failed:
        raise _ObservationFailure
    return shape, entities, components, interferences, bom


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


def _same_shape_observation(actual: object, expected: object) -> bool:
    """Compare save/reload geometry without treating OCC float noise as drift."""

    return (
        type(actual) is ShapeObservation
        and type(expected) is ShapeObservation
        and actual.schema_version == expected.schema_version
        and actual.target == expected.target
        and _same_geometry_number(actual.volume_mm3, expected.volume_mm3)
        and _same_geometry_number(actual.area_mm2, expected.area_mm2)
        and _same_geometry_vector(actual.bbox_mm, expected.bbox_mm)
        and _same_geometry_vector(actual.center_of_mass_mm, expected.center_of_mass_mm)
        and actual.valid_shape is expected.valid_shape
        and actual.solid_count == expected.solid_count
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
    project_id: str,
    revision_id: str,
    component: object | None = None,
    **kwargs: object,
) -> dict[str, object]:
    """Create one primitive, bind identity, and rebuild the result from live facts."""

    if context.preserve:
        raise _operation_failure()
    before = _entity_observations(session)
    part_name: str | None = None
    component_identity: EntityIdentity | None = None
    if component is not None:
        _container, component_identity, part_name = _resolve_component_target(
            session,
            component,
            project_id=project_id,
            revision_id=revision_id,
        )
    try:
        document_before = tuple(session.doc.Objects)  # type: ignore[attr-defined]
    except Exception:
        raise _operation_failure() from None
    if part_name is None:
        leaf(session, **kwargs)
    else:
        leaf(session, part=part_name, **kwargs)
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
        if part_name is not None and session.owner_of(obj.Name) != part_name:  # type: ignore[attr-defined]
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
    elif context.operation == "create_cone":
        expected_parameters = {
            "angle": 360.0,
            "base_radius": kwargs.get("radius1"),
            "height": kwargs.get("height"),
            "top_radius": kwargs.get("radius2", 0.0),
        }
        radius1 = expected_parameters["base_radius"]
        radius2 = expected_parameters["top_radius"]
        height = expected_parameters["height"]
        if any(type(item) not in {int, float} for item in (radius1, radius2, height)):
            raise _operation_failure()
        base_radius = float(radius1)
        top_radius = float(radius2)
        cone_height = float(height)
        radius_sum = base_radius**2 + base_radius * top_radius + top_radius**2
        if base_radius <= 0 or top_radius < 0 or cone_height <= 0 or radius_sum <= 0:
            raise _operation_failure()
        expected_volume = math.pi * cone_height * radius_sum / 3.0
        slant = math.hypot(cone_height, base_radius - top_radius)
        expected_area = math.pi * (
            base_radius**2 + top_radius**2 + (base_radius + top_radius) * slant
        )
        local_center_z = (
            cone_height
            * (base_radius**2 + 2.0 * base_radius * top_radius + 3.0 * top_radius**2)
            / (4.0 * radius_sum)
        )
        cone_axis = kwargs.get("axis", "z")
        diameter = 2.0 * max(base_radius, top_radius)
        if cone_axis == "x":
            expected_rotation = _axis_rotation("y", 90.0)
            expected_bbox = (cone_height, diameter, diameter)
        elif cone_axis == "y":
            expected_rotation = _axis_rotation("x", -90.0)
            expected_bbox = (diameter, cone_height, diameter)
        elif cone_axis == "z":
            expected_rotation = (0.0, 0.0, 0.0, 1.0)
            expected_bbox = (diameter, diameter, cone_height)
        else:
            raise _operation_failure()
        center_offset = _rotate_vector(expected_rotation, (0.0, 0.0, local_center_z))
        expected_center = tuple(
            float(origin) + offset
            for origin, offset in zip(expected_position, center_offset, strict=True)
        )
    elif context.operation == "create_sphere":
        expected_parameters = {
            "angle1": -90.0,
            "angle2": 90.0,
            "angle3": 360.0,
            "radius": kwargs.get("radius"),
        }
        radius = expected_parameters["radius"]
        if type(radius) not in {int, float} or float(radius) <= 0:
            raise _operation_failure()
        sphere_radius = float(radius)
        expected_volume = 4.0 * math.pi * sphere_radius**3 / 3.0
        expected_area = 4.0 * math.pi * sphere_radius**2
        expected_bbox = (2.0 * sphere_radius,) * 3
        expected_center = tuple(float(component) for component in expected_position)
        expected_rotation = (0.0, 0.0, 0.0, 1.0)
    elif context.operation == "create_torus":
        expected_parameters = {
            "angle1": -180.0,
            "angle2": 180.0,
            "angle3": 360.0,
            "major_radius": kwargs.get("radius1"),
            "minor_radius": kwargs.get("radius2"),
        }
        radius1 = expected_parameters["major_radius"]
        radius2 = expected_parameters["minor_radius"]
        if type(radius1) not in {int, float} or type(radius2) not in {int, float}:
            raise _operation_failure()
        major_radius = float(radius1)
        minor_radius = float(radius2)
        if major_radius <= minor_radius or minor_radius <= 0:
            raise _operation_failure()
        expected_volume = 2.0 * math.pi**2 * major_radius * minor_radius**2
        expected_area = 4.0 * math.pi**2 * major_radius * minor_radius
        torus_axis = kwargs.get("axis", "z")
        if torus_axis == "x":
            expected_rotation = _axis_rotation("y", 90.0)
        elif torus_axis == "y":
            expected_rotation = _axis_rotation("x", -90.0)
        elif torus_axis == "z":
            expected_rotation = (0.0, 0.0, 0.0, 1.0)
        else:
            raise _operation_failure()
        # OCC's torus triangulation can conservatively enlarge the reported X/Y
        # bounding box.  Volume, area, center, native parameters and one-solid
        # validity remain the exact creation contract.
        expected_bbox = None
        expected_center = tuple(float(component) for component in expected_position)
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
        or (expected_bbox is not None and not _same_geometry_vector(created.bbox_mm, expected_bbox))
        or not _same_geometry_vector(created.center_of_mass_mm, expected_center)
    ):
        raise _operation_failure()
    result = {
        "schema_version": SCHEMA_VERSION,
        "kind": "entity_created",
        "operation": context.operation,
        "object_id": created.object_id,
        "feature_id": created.feature_id,
        "after": created.to_mapping(),
        "preservation": [item.to_mapping() for item in comparisons],
    }
    if component_identity is not None:
        result["component_id"] = component_identity.object_id
    return result


def _managed_boolean(
    session: object,
    context: _InvocationContext,
    *,
    leaf: Callable[..., object],
    expected_type: str,
    project_id: str,
    revision_id: str,
    base: object,
    tool: object,
) -> dict[str, object]:
    """Create one native Part boolean and seal its operand dependency links."""

    if context.preserve or expected_type not in _BOOLEAN_OPERATIONS:
        raise _operation_failure()
    before = _entity_observations(session)
    before_by_id = _observation_map(before)
    base_obj, base_identity = _resolve_entity_target(
        session,
        base,
        project_id=project_id,
        revision_id=revision_id,
    )
    tool_obj, tool_identity = _resolve_entity_target(
        session,
        tool,
        project_id=project_id,
        revision_id=revision_id,
    )
    try:
        pairs = _identified_pairs(session)
        by_name, _dependencies, consumed = _native_dependency_graph(pairs)
        base_name = base_obj.Name
        tool_name = tool_obj.Name
        if (
            base_obj is tool_obj
            or base_identity == tool_identity
            or base_name not in by_name
            or tool_name not in by_name
            or by_name[base_name] != (base_obj, base_identity)
            or by_name[tool_name] != (tool_obj, tool_identity)
            or base_name in consumed
            or tool_name in consumed
        ):
            raise ValueError
        owner_of = getattr(session, "owner_of", None)
        if not callable(owner_of):
            raise ValueError
        base_owner = owner_of(base_name)
        tool_owner = owner_of(tool_name)
        if base_owner != tool_owner:
            raise ValueError
        document_before = tuple(session.doc.Objects)  # type: ignore[attr-defined]
    except Exception:
        raise _operation_failure() from None

    attach = getattr(session, "attach_object_identity", None)
    read_identity = getattr(session, "read_object_identity", None)
    get_result = getattr(session, "get_result_object", None)
    transaction = getattr(session, "_transaction", None)
    claim_new_objects = getattr(session, "_claim_new_objects", None)
    if (
        not callable(attach)
        or not callable(read_identity)
        or not callable(get_result)
        or not callable(transaction)
        or (base_owner is not None and not callable(claim_new_objects))
    ):
        raise _operation_failure()
    try:
        with transaction(
            f"VibeCAD {context.operation}",
            part=base_owner,
            claim_new_objects=False,
        ):
            leaf(session, base_name=base_name, tool_name=tool_name)
            document_after = tuple(session.doc.Objects)  # type: ignore[attr-defined]
            if any(
                not any(current is obj for current in document_after) for obj in document_before
            ):
                raise ValueError
            added = tuple(
                obj
                for obj in document_after
                if not any(obj is current for current in document_before)
            )
            result_candidates = tuple(
                obj for obj in added if getattr(obj, "TypeId", None) == expected_type
            )
            if len(result_candidates) != 1:
                raise ValueError
            result_obj = result_candidates[0]
            managed_added = tuple(
                obj
                for obj in added
                if getattr(obj, "TypeId", None) in _NATIVE_ENTITY_TYPES
                or any(
                    getattr(obj, property_name, None)
                    for property_name in (
                        "VibeCADObjectId",
                        "VibeCADFeatureId",
                        "VibeCADSemanticRole",
                        "VibeCADProvenance",
                    )
                )
            )
            if managed_added != (result_obj,):
                raise ValueError
            if (
                result_obj.TypeId != expected_type
                or result_obj.Base is not base_obj
                or result_obj.Tool is not tool_obj
            ):
                raise ValueError
            identity = EntityIdentity(
                object_id=f"object_{secrets.token_hex(16)}",
                feature_id=f"feature_{secrets.token_hex(16)}",
                object_type=expected_type,
                semantic_role=SemanticRole.FEATURE,
                provenance=Provenance(
                    source=ProvenanceSource(context.source.value),
                    operation_id=context.operation_id,
                ),
            )
            if attach(result_obj, identity) != identity or read_identity(result_obj) != identity:
                raise ValueError
            if base_owner is not None:
                claim_new_objects(
                    {obj.Name for obj in document_before},
                    part=base_owner,
                )
                if owner_of(result_obj.Name) != base_owner:
                    raise ValueError
            if get_result(base_owner) is not result_obj:
                raise ValueError
            after = _entity_observations(session)
            after_by_id = _observation_map(after)
            if set(before_by_id) - set(after_by_id) or set(after_by_id) - set(before_by_id) != {
                identity.object_id
            }:
                raise ValueError
            comparisons = _require_non_target_preservation(
                before_by_id,
                {key: value for key, value in after_by_id.items() if key != identity.object_id},
                target=None,
            )
            created = after_by_id[identity.object_id]
            parameters = {item.name: item.value for item in created.parameters}
            expected_parameters = {
                "base_object_id": base_identity.object_id,
                "operation": _BOOLEAN_OPERATIONS[expected_type],
                "tool_object_id": tool_identity.object_id,
            }
            base_observation = before_by_id.get(base_identity.object_id)
            tool_observation = before_by_id.get(tool_identity.object_id)
            if (
                created.object_type != expected_type
                or created.feature_id != identity.feature_id
                or parameters != expected_parameters
                or base_observation is None
                or tool_observation is None
                or base_observation.volume_mm3 is None
                or tool_observation.volume_mm3 is None
                or created.valid_shape is not True
                or created.solid_count != 1
                or created.volume_mm3 is None
                or created.volume_mm3 <= 0
                or created.area_mm2 is None
                or created.area_mm2 <= 0
                or created.bbox_mm is None
                or any(component <= 0 for component in created.bbox_mm)
                or created.center_of_mass_mm is None
            ):
                raise ValueError
            result_volume = float(created.volume_mm3)
            base_volume = float(base_observation.volume_mm3)
            tool_volume = float(tool_observation.volume_mm3)
            tolerance = max(base_volume, tool_volume, 1.0) * 1e-7
            operation = _BOOLEAN_OPERATIONS[expected_type]
            if operation == "cut" and not result_volume < base_volume - tolerance:
                raise ValueError
            if operation == "fuse" and not (
                max(base_volume, tool_volume) - tolerance
                <= result_volume
                <= base_volume + tool_volume + tolerance
            ):
                raise ValueError
            if operation == "common" and not (
                0 < result_volume <= min(base_volume, tool_volume) + tolerance
            ):
                raise ValueError
    except Exception:
        raise _operation_failure() from None
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "boolean_created",
        "operation": context.operation,
        "object_id": created.object_id,
        "feature_id": created.feature_id,
        "base_object_id": base_identity.object_id,
        "tool_object_id": tool_identity.object_id,
        "after": created.to_mapping(),
        "preservation": [item.to_mapping() for item in comparisons],
    }


def _managed_create_parametric_design(
    session: object,
    context: _InvocationContext,
    *,
    design: object,
) -> dict[str, object]:
    """Compile and atomically adopt one complete editable parametric design."""

    if context.preserve:
        raise _operation_failure()
    try:
        checked = ParametricDesignIR.from_mapping(design)
    except Exception:
        raise _operation_failure() from None

    before = _entity_observations(session)
    before_by_id = _observation_map(before)
    adopted: tuple[object, EntityIdentity, tuple[EntityIdentity, ...]] | None = None
    provenance = Provenance(
        source=ProvenanceSource(context.source.value),
        operation_id=context.operation_id,
    )

    def adopt(compiled: object) -> None:
        nonlocal adopted
        if adopted is not None:
            raise ValueError
        try:
            compiled_features = tuple(compiled.features)  # type: ignore[attr-defined]
            compiled_treatments = tuple(getattr(compiled, "edge_treatments", ()))
            compiled_entities = compiled_features + compiled_treatments
            compiled_result = getattr(compiled, "result_object", compiled.body)  # type: ignore[attr-defined]
            if (
                compiled.design_id != checked.id  # type: ignore[attr-defined]
                or compiled.design_digest != checked.digest  # type: ignore[attr-defined]
                or getattr(compiled.body, "TypeId", None) != "PartDesign::Body"  # type: ignore[attr-defined]
                or tuple(item.feature_id for item in compiled_features)
                != tuple(item.id for item in checked.features)
                or tuple(item.feature_id for item in compiled_treatments)
                != tuple(item.id for item in checked.edge_treatments)
                or len(tuple(compiled.sketches)) != len(checked.sketches)  # type: ignore[attr-defined]
                or (
                    compiled_result
                    is not (
                        compiled_treatments[-1].object if compiled_treatments else compiled.body  # type: ignore[attr-defined]
                    )
                )
            ):
                raise ValueError
            attach = session.attach_object_identity  # type: ignore[attr-defined]
            read = session.read_object_identity  # type: ignore[attr-defined]
            set_result = session.set_result_object  # type: ignore[attr-defined]
            if not all(callable(item) for item in (attach, read, set_result)):
                raise ValueError
            body_identity = EntityIdentity(
                object_id=f"object_{secrets.token_hex(16)}",
                feature_id=None,
                object_type="PartDesign::Body",
                semantic_role=SemanticRole.PART,
                provenance=provenance,
            )
            feature_identities = tuple(
                EntityIdentity(
                    object_id=f"object_{secrets.token_hex(16)}",
                    feature_id=f"feature_{secrets.token_hex(16)}",
                    object_type=feature.object.TypeId,
                    semantic_role=SemanticRole.FEATURE,
                    provenance=provenance,
                )
                for feature in compiled_entities
            )
            bindings = (
                ((compiled.body, body_identity),)  # type: ignore[attr-defined]
                + tuple(
                    (item.object, identity)
                    for item, identity in zip(
                        compiled_entities,
                        feature_identities,
                        strict=True,
                    )
                )
            )
            for obj, identity in bindings:
                if attach(obj, identity) != identity or read(obj) != identity:
                    raise ValueError
            set_result(compiled_result)
            adopted = compiled, body_identity, feature_identities
        except Exception:
            raise ValueError from None

    try:
        compiled = _compile_parametric_design(session, checked, adopt=adopt)
    except Exception:
        raise _operation_failure() from None
    if adopted is None or adopted[0] is not compiled:
        raise _operation_failure()
    _, body_identity, feature_identities = adopted

    after = _entity_observations(session)
    after_by_id = _observation_map(after)
    new_ids = {body_identity.object_id, *(item.object_id for item in feature_identities)}
    if set(before_by_id) - set(after_by_id) or set(after_by_id) - set(before_by_id) != new_ids:
        raise _operation_failure()
    comparisons = _require_non_target_preservation(
        before_by_id,
        {key: value for key, value in after_by_id.items() if key not in new_ids},
        target=None,
    )

    body = after_by_id[body_identity.object_id]
    body_parameters = {item.name: item.value for item in body.parameters}
    if (
        body.feature_id is not None
        or body.object_type != "PartDesign::Body"
        or body.semantic_role != SemanticRole.PART.value
        or body.provenance != provenance.to_mapping()
        or body_parameters.get("parametric.design_ir_digest") != checked.digest
        or body_parameters.get("parametric.feature_count") != len(checked.features)
        or body_parameters.get("parametric.edge_treatment_count")
        != (len(checked.edge_treatments) if checked.edge_treatments else None)
        or body_parameters.get("parametric.sketch_count") != len(checked.sketches)
        or body.valid_shape is not True
        or body.solid_count != 1
        or body.volume_mm3 is None
        or body.volume_mm3 <= 0
    ):
        raise _operation_failure()

    feature_observations = tuple(after_by_id[item.object_id] for item in feature_identities)
    feature_identity_count = len(checked.features)
    for index, (feature, identity, observation) in enumerate(
        zip(
            checked.features,
            feature_identities[:feature_identity_count],
            feature_observations[:feature_identity_count],
            strict=True,
        )
    ):
        parameters = {item.name: item.value for item in observation.parameters}
        if (
            observation.feature_id != identity.feature_id
            or observation.object_type != identity.object_type
            or observation.semantic_role != SemanticRole.FEATURE.value
            or observation.provenance != provenance.to_mapping()
            or parameters.get("parametric.design_ir_digest") != checked.digest
            or parameters.get("parametric.feature.index") != index
            or parameters.get("parametric.feature.kind") != feature.kind.value
            or parameters.get("parametric.shape_valid") is not True
            or parameters.get("parametric.solid_count") != 1
            or observation.valid_shape is not True
            or observation.solid_count != 1
            or observation.volume_mm3 is None
            or observation.volume_mm3 <= 0
        ):
            raise _operation_failure()

    for index, (treatment, identity, observation) in enumerate(
        zip(
            checked.edge_treatments,
            feature_identities[feature_identity_count:],
            feature_observations[feature_identity_count:],
            strict=True,
        )
    ):
        parameters = {item.name: item.value for item in observation.parameters}
        if (
            observation.feature_id != identity.feature_id
            or observation.object_type != identity.object_type
            or observation.semantic_role != SemanticRole.FEATURE.value
            or observation.provenance != provenance.to_mapping()
            or parameters.get("parametric.design_ir_digest") != checked.digest
            or parameters.get("parametric.edge_treatment.index") != index
            or parameters.get("parametric.edge_treatment.kind") != treatment.kind.value
            or parameters.get("parametric.edge_treatment.edge_count") != len(treatment.targets)
            or parameters.get("parametric.shape_valid") is not True
            or parameters.get("parametric.solid_count") != 1
            or observation.valid_shape is not True
            or observation.solid_count != 1
            or observation.volume_mm3 is None
            or observation.volume_mm3 <= 0
        ):
            raise _operation_failure()

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "parametric_design_created",
        "operation": context.operation,
        "design_id": checked.id,
        "design_digest": checked.digest,
        "object_id": body_identity.object_id,
        "tip_object_id": feature_identities[-1].object_id,
        "feature_object_ids": [item.object_id for item in feature_identities],
        "feature_ids": [item.feature_id for item in feature_identities],
        "after": body.to_mapping(),
        "features": [item.to_mapping() for item in feature_observations],
        "preservation": [item.to_mapping() for item in comparisons],
    }


def _managed_apply_reviewed_intent(
    session: object,
    context: _InvocationContext,
    *,
    intent: object,
) -> dict[str, object]:
    """Execute and adopt one statically routed Reviewed intent."""

    if context.preserve:
        raise _operation_failure()
    try:
        checked = ReviewedIntentProgramV1.from_mapping(intent)
        attach = session.attach_object_identity  # type: ignore[attr-defined]
        read_identity = session.read_object_identity  # type: ignore[attr-defined]
        set_result = session.set_result_object  # type: ignore[attr-defined]
        get_result = session.get_result_object  # type: ignore[attr-defined]
        transaction = session._transaction  # type: ignore[attr-defined]
        if not all(
            callable(item) for item in (attach, read_identity, set_result, get_result, transaction)
        ):
            raise ValueError
        before = _entity_observations(session)
        before_by_id = _observation_map(before)
        document_before = tuple(session.doc.Objects)  # type: ignore[attr-defined]
    except Exception:
        raise _operation_failure() from None

    try:
        executed = _execute_reviewed_intent_native(session, checked)
        if type(executed) is not ReviewedNativeExecutionResult:
            raise ValueError
        obj = executed.object
        document_after = tuple(session.doc.Objects)  # type: ignore[attr-defined]
        added = tuple(
            item
            for item in document_after
            if not any(item is existing for existing in document_before)
        )
        if (
            len(document_after) != len(document_before) + 1
            or len(added) != 1
            or added[0] is not obj
            or getattr(obj, "TypeId", None) != executed.route.operation.native_type_id
        ):
            raise ValueError
        provenance = Provenance(
            source=ProvenanceSource(context.source.value),
            operation_id=context.operation_id,
        )
        identity = EntityIdentity(
            object_id=f"object_{secrets.token_hex(16)}",
            feature_id=f"feature_{secrets.token_hex(16)}",
            object_type=executed.route.operation.native_type_id,
            semantic_role=SemanticRole.PRIMITIVE,
            provenance=provenance,
        )
        with transaction(
            "VibeCAD adopt reviewed intent",
            claim_new_objects=False,
        ):
            if attach(obj, identity) != identity or read_identity(obj) != identity:
                raise ValueError
            set_result(obj)
            if get_result() is not obj:
                raise ValueError
            after = _entity_observations(session)
            after_by_id = _observation_map(after)
            if set(after_by_id) != {*before_by_id, identity.object_id}:
                raise ValueError
            comparisons = _require_non_target_preservation(
                before_by_id,
                {key: value for key, value in after_by_id.items() if key != identity.object_id},
                target=None,
            )
            created = after_by_id[identity.object_id]
            if (
                created.feature_id != identity.feature_id
                or created.object_type != identity.object_type
                or created.semantic_role != SemanticRole.PRIMITIVE.value
                or created.provenance != provenance.to_mapping()
                or created.valid_shape is not True
                or created.solid_count != 1
                or created.volume_mm3 is None
                or created.volume_mm3 <= 0
            ):
                raise ValueError
    except Exception:
        raise _operation_failure() from None

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "reviewed_intent_applied",
        "operation": context.operation,
        "reviewed_operation_id": executed.route.operation_id,
        "semantic_operation": executed.route.semantic_operation,
        "object_id": identity.object_id,
        "feature_id": identity.feature_id,
        "plan_sha256": executed.plan_sha256,
        "plan_content_sha256": executed.plan_content_sha256,
        "native_receipt_sha256": executed.native_receipt.receipt_sha256,
        "after": created.to_mapping(),
        "preservation": [item.to_mapping() for item in comparisons],
    }


def _same_parametric_entity_envelope(
    before: EntityObservation,
    after: EntityObservation,
    *,
    mutable_parameters: Mapping[str, tuple[float, str]],
) -> bool:
    if (
        before.schema_version != after.schema_version
        or before.object_id != after.object_id
        or before.feature_id != after.feature_id
        or before.object_type != after.object_type
        or before.semantic_role != after.semantic_role
        or before.provenance != after.provenance
        or not _same_vector(before.placement[:3], after.placement[:3])
        or not _same_rotation(before.placement[3:], after.placement[3:])
        or before.valid_shape is not True
        or after.valid_shape is not True
        or before.solid_count != 1
        or after.solid_count != 1
        or len(before.parameters) != len(after.parameters)
    ):
        return False
    before_parameters = {item.name: item for item in before.parameters}
    after_parameters = {item.name: item for item in after.parameters}
    if set(before_parameters) != set(after_parameters):
        return False
    for name, old in before_parameters.items():
        new = after_parameters[name]
        mutable = mutable_parameters.get(name)
        if mutable is None:
            if not _same_import_parameter(old, new):
                return False
            continue
        expected, unit = mutable
        if (
            old.schema_version != new.schema_version
            or old.name != new.name
            or old.unit != new.unit
            or new.unit != unit
            or type(new.value) not in {int, float}
            or not _same_number(new.value, expected)
        ):
            return False
    return True


def _same_entity_geometry(left: EntityObservation, right: EntityObservation) -> bool:
    return (
        _same_optional_geometry_number(left.volume_mm3, right.volume_mm3)
        and _same_optional_geometry_number(left.area_mm2, right.area_mm2)
        and _same_optional_geometry_vector(left.bbox_mm, right.bbox_mm)
        and _same_optional_geometry_vector(left.center_of_mass_mm, right.center_of_mass_mm)
        and left.valid_shape is right.valid_shape
        and left.solid_count == right.solid_count
    )


def _managed_modify_parametric_parameter(
    session: object,
    context: _InvocationContext,
    *,
    project_id: str,
    revision_id: str,
    target: object,
    design: object,
    parameter_id: object,
    value: object,
) -> dict[str, object]:
    """Edit one public IR parameter while preserving identity and Task authority."""

    if context.preserve:
        raise _operation_failure()
    if type(target) is not SelectorV1:
        raise _operation_failure()
    try:
        checked = ParametricDesignIR.from_mapping(design)
    except Exception:
        raise _operation_failure() from None
    before = _entity_observations(session)
    before_by_id = _observation_map(before)
    obj, identity = _resolve_entity_target(
        session,
        target,
        project_id=project_id,
        revision_id=revision_id,
    )
    old_body = before_by_id.get(identity.object_id)
    if (
        old_body is None
        or identity.feature_id is not None
        or identity.object_type != "PartDesign::Body"
        or identity.semantic_role is not SemanticRole.PART
    ):
        raise _operation_failure()

    feature_bindings: dict[int, tuple[str, ...]] = {}
    for index, feature in enumerate(checked.features):
        names = tuple(
            sorted(
                name for name, bound_id in feature.parameters.items() if bound_id == parameter_id
            )
        )
        if names:
            feature_bindings[index] = names
    treatment_bindings: dict[int, tuple[str, ...]] = {}
    for treatment_index, treatment in enumerate(checked.edge_treatments):
        names: list[str] = []
        for target_index, treatment_target in enumerate(treatment.targets):
            if treatment_target.start_parameter_id == parameter_id:
                names.append(f"parametric.edge_treatment.target.{target_index}.start")
            if treatment_target.end_parameter_id == parameter_id:
                names.append(f"parametric.edge_treatment.target.{target_index}.end")
        if names:
            treatment_bindings[treatment_index] = tuple(sorted(names))

    try:
        raw_result_roots = session._result_roots  # type: ignore[attr-defined]
        if type(raw_result_roots) is not dict or any(
            type(key) is not str or type(item) is not str for key, item in raw_result_roots.items()
        ):
            raise TypeError
        result_roots_before = dict(raw_result_roots)
    except Exception:
        raise _operation_failure() from None

    verified: dict[str, object] = {}

    def verify(edit: object) -> None:
        try:
            if dict(session._result_roots) != result_roots_before:  # type: ignore[attr-defined]
                raise _operation_failure()
        except ExecutorError:
            raise
        except Exception:
            raise _operation_failure() from None

        after = _entity_observations(session)
        after_by_id = _observation_map(after)
        if set(after_by_id) != set(before_by_id):
            raise _operation_failure()
        new_body = after_by_id.get(identity.object_id)
        if new_body is None:
            raise _operation_failure()
        try:
            after_value = edit.after_value  # type: ignore[attr-defined]
            edit_unit = edit.unit  # type: ignore[attr-defined]
        except Exception:
            raise _operation_failure() from None
        if type(after_value) not in {int, float} or type(edit_unit) is not str:
            raise _operation_failure()

        parametric_ids: set[str] = set()
        feature_object_ids: dict[int, str] = {}
        treatment_object_ids: dict[int, str] = {}
        for object_id, old in before_by_id.items():
            old_parameters = {item.name: item.value for item in old.parameters}
            if old_parameters.get("parametric.design_ir_digest") != checked.digest:
                if not _same_import_observation(old, after_by_id[object_id]):
                    raise _operation_failure()
                continue
            parametric_ids.add(object_id)
            mutable_parameters: dict[str, tuple[float, str]] = {}
            if old.semantic_role == SemanticRole.FEATURE.value:
                feature_index = old_parameters.get("parametric.feature.index")
                treatment_index = old_parameters.get("parametric.edge_treatment.index")
                if type(feature_index) is int and treatment_index is None:
                    if feature_index in feature_object_ids:
                        raise _operation_failure()
                    feature_object_ids[feature_index] = object_id
                    mutable_parameters = {
                        f"parametric.feature.parameter.{name}": (after_value, edit_unit)
                        for name in feature_bindings.get(feature_index, ())
                    }
                elif type(treatment_index) is int and feature_index is None:
                    if treatment_index in treatment_object_ids:
                        raise _operation_failure()
                    treatment_object_ids[treatment_index] = object_id
                    mutable_parameters = {
                        name: (after_value, edit_unit)
                        for name in treatment_bindings.get(treatment_index, ())
                    }
                else:
                    raise _operation_failure()
            if not _same_parametric_entity_envelope(
                old,
                after_by_id[object_id],
                mutable_parameters=mutable_parameters,
            ):
                raise _operation_failure()
        if (
            len(parametric_ids) != len(checked.features) + len(checked.edge_treatments) + 1
            or set(feature_object_ids) != set(range(len(checked.features)))
            or set(treatment_object_ids) != set(range(len(checked.edge_treatments)))
        ):
            raise _operation_failure()

        ordered_feature_object_ids = tuple(
            feature_object_ids[index] for index in range(len(checked.features))
        ) + tuple(treatment_object_ids[index] for index in range(len(checked.edge_treatments)))
        affected_feature_object_ids = tuple(
            object_id
            for object_id in ordered_feature_object_ids
            if not _same_entity_geometry(before_by_id[object_id], after_by_id[object_id])
        )
        if not affected_feature_object_ids:
            raise _operation_failure()
        verified.update(
            {
                "edit": edit,
                "new_body": new_body,
                "affected_feature_object_ids": affected_feature_object_ids,
            }
        )

    try:
        edit = _modify_parametric_parameter(
            session,
            checked,
            body=obj,
            parameter_id=parameter_id,
            value=value,
            verify=verify,
        )
    except Exception:
        raise _operation_failure() from None
    if (
        edit.body is not obj
        or edit.design_id != checked.id
        or edit.design_digest != checked.digest
        or verified.get("edit") is not edit
    ):
        raise _operation_failure()
    new_body = verified.get("new_body")
    affected_feature_object_ids = verified.get("affected_feature_object_ids")
    if type(new_body) is not EntityObservation or type(affected_feature_object_ids) is not tuple:
        raise _operation_failure()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "parametric_parameter_modified",
        "operation": context.operation,
        "design_id": edit.design_id,
        "design_digest": edit.design_digest,
        "object_id": identity.object_id,
        "parameter_id": edit.parameter_id,
        "parameter_name": edit.parameter_name,
        "unit": edit.unit,
        "before_value": edit.before_value,
        "after_value": edit.after_value,
        "consumer_ids": list(edit.consumer_ids),
        "affected_feature_object_ids": list(affected_feature_object_ids),
        "before": old_body.to_mapping(),
        "after": new_body.to_mapping(),
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


def _resolve_component_target(
    session: object,
    target: object,
    *,
    project_id: str,
    revision_id: str,
) -> tuple[object, EntityIdentity, str]:
    obj, identity = _resolve_entity_target(
        session,
        target,
        project_id=project_id,
        revision_id=revision_id,
    )
    if (
        identity.object_type != "App::Part"
        or identity.semantic_role is not SemanticRole.PART
        or identity.feature_id is not None
    ):
        raise _operation_failure()
    list_records = getattr(session, "list_component_identity_records", None)
    if not callable(list_records):
        raise _operation_failure()
    try:
        matches = tuple(
            (part_name, container, component_identity)
            for part_name, container, component_identity, _members in list_records()
            if container is obj and component_identity == identity
        )
    except Exception:
        raise _operation_failure() from None
    if len(matches) != 1:
        raise _operation_failure()
    return obj, identity, matches[0][0]


def _parameter_value(observation: EntityObservation, name: str) -> int | float:
    try:
        parameter = next(item for item in observation.parameters if item.name == name)
    except StopIteration:
        raise _operation_failure() from None
    if type(parameter.value) not in {int, float}:
        raise _operation_failure()
    return parameter.value


def _validated_managed_mutation_result(
    *,
    context: _InvocationContext,
    identity: EntityIdentity,
    old: EntityObservation,
    before_by_id: dict[str, EntityObservation],
    after: tuple[EntityObservation, ...],
    descendant_ids: frozenset[str],
    descendant_before: dict[str, EntityObservation],
    recomputed_descendant_ids: frozenset[str],
    rotation_pivot: tuple[int | float, int | float, int | float] | None,
    parameter: str | None,
    value: object,
    position: object,
    leaf_kwargs: dict[str, object],
) -> dict[str, object]:
    """Validate one mutation and every authenticated native Boolean descendant."""

    after_by_id = _observation_map(after)
    new = after_by_id.get(identity.object_id)
    if new is None or set(before_by_id) != set(after_by_id):
        raise _operation_failure()
    comparisons: list[PreservationObservation] = []
    for object_id in sorted(before_by_id):
        if object_id == identity.object_id:
            continue
        if object_id in descendant_ids:
            descendant = after_by_id[object_id]
            previous = descendant_before[object_id]
            if (
                object_id not in recomputed_descendant_ids
                or descendant.valid_shape is not True
                or descendant.solid_count != 1
                or descendant.volume_mm3 is None
                or descendant.volume_mm3 <= 0
                or descendant.area_mm2 is None
                or descendant.area_mm2 <= 0
                or descendant.bbox_mm is None
                or descendant.center_of_mass_mm is None
                or previous.volume_mm3 is None
                or previous.area_mm2 is None
                or previous.bbox_mm is None
                or previous.center_of_mass_mm is None
            ):
                raise _operation_failure()
            comparisons.append(
                _require_preserved(
                    before_by_id[object_id],
                    descendant,
                    target=object_id,
                    preserve=("placement", "parameters"),
                )
            )
        else:
            comparisons.append(
                _require_preserved(
                    before_by_id[object_id],
                    after_by_id[object_id],
                    target=object_id,
                )
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
    try:
        by_name, dependencies, _consumed = _native_dependency_graph(_identified_pairs(session))
        target_name = obj.Name
        if target_name not in by_name or by_name[target_name] != (obj, identity):
            raise ValueError
        descendant_names = _native_boolean_descendants(
            dependencies,
            target_name=target_name,
        )
        descendant_ids = frozenset(by_name[name][1].object_id for name in descendant_names)
        descendant_before = {
            by_name[name][1].object_id: before_by_id[by_name[name][1].object_id]
            for name in descendant_names
        }
    except Exception:
        raise _operation_failure() from None
    rotation_pivot: tuple[int | float, int | float, int | float] | None = None
    if context.operation == "rotate_part":
        try:
            rotation_pivot = _bound_box_center(obj.Shape)
        except _ObservationFailure:
            raise _operation_failure() from None
    set_result = getattr(session, "set_result_object", None)
    if not callable(set_result):
        raise _operation_failure()
    owner_of = getattr(session, "owner_of", None)
    if not callable(owner_of):
        raise _operation_failure()
    try:
        owner = owner_of(target_name)
        result_obj = by_name[descendant_names[-1]][0] if descendant_names else obj
    except Exception:
        raise _operation_failure() from None
    transaction = getattr(session, "_transaction", None)
    use_outer_transaction = bool(descendant_names)
    if use_outer_transaction and not callable(transaction):
        raise _operation_failure()
    try:
        if use_outer_transaction:
            with transaction(
                f"VibeCAD {context.operation}",
                part=owner,
                claim_new_objects=False,
            ):
                set_result(result_obj, part=owner)
                with _DocumentRecomputeObserver(session.doc) as receipt:  # type: ignore[attr-defined]
                    if context.operation == "modify_parameter":
                        _modify_part_uncommitted(
                            session,
                            name=obj.Name,
                            parameter=leaf_kwargs["parameter"],
                            value=leaf_kwargs["value"],
                            result_name=result_obj.Name,
                        )
                    elif context.operation == "move_part":
                        _move_part_uncommitted(
                            session,
                            name=obj.Name,
                            position=leaf_kwargs["position"],
                            result_name=result_obj.Name,
                        )
                    elif context.operation == "rotate_part":
                        _rotate_part_uncommitted(
                            session,
                            name=obj.Name,
                            axis=leaf_kwargs["axis"],
                            angle=leaf_kwargs["angle"],
                            result_name=result_obj.Name,
                        )
                    else:
                        raise ValueError
                recomputed_descendant_ids = _require_recomputed_boolean_descendants(
                    by_name,
                    descendant_names,
                    frozenset(receipt.object_ids),
                    obj,
                )
                after = _entity_observations(session)
                result = _validated_managed_mutation_result(
                    context=context,
                    identity=identity,
                    old=old,
                    before_by_id=before_by_id,
                    after=after,
                    descendant_ids=descendant_ids,
                    descendant_before=descendant_before,
                    recomputed_descendant_ids=recomputed_descendant_ids,
                    rotation_pivot=rotation_pivot,
                    parameter=parameter,
                    value=value,
                    position=position,
                    leaf_kwargs=leaf_kwargs,
                )
        else:
            set_result(result_obj, part=owner)
            leaf(session, name=obj.Name, **leaf_kwargs)
            after = _entity_observations(session)
            result = _validated_managed_mutation_result(
                context=context,
                identity=identity,
                old=old,
                before_by_id=before_by_id,
                after=after,
                descendant_ids=descendant_ids,
                descendant_before=descendant_before,
                recomputed_descendant_ids=frozenset(),
                rotation_pivot=rotation_pivot,
                parameter=parameter,
                value=value,
                position=position,
                leaf_kwargs=leaf_kwargs,
            )
    except Exception:
        raise _operation_failure() from None
    return result


def _managed_inspect(
    session: object,
    context: _InvocationContext,
    *,
    revision_id: str,
) -> dict[str, object]:
    if context.preserve:
        raise _operation_failure()
    before = _entity_observations(session)
    components_before = _component_observations(session)
    interferences_before = _interference_observations(session)
    bom_before = _bom_observation(session, components_before)
    shape = _shape_observation(session)
    after = _entity_observations(session)
    components_after = _component_observations(session)
    interferences_after = _interference_observations(session)
    bom_after = _bom_observation(session, components_after)
    if (
        before != after
        or components_before != components_after
        or interferences_before != interferences_after
        or bom_before != bom_after
    ):
        raise _operation_failure()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "model_inspection",
        "operation": context.operation,
        "shape": shape.to_mapping(),
        "entities": [item.to_mapping() for item in after],
        "components": [item.to_mapping() for item in components_after],
        "interferences": [item.to_mapping() for item in interferences_after],
        "bom_revision_id": revision_id,
        "bom": None if bom_after is None else bom_after.to_mapping(),
        "bom_csv": _bom_csv(bom_after),
    }


def _managed_create_component(
    session: object,
    context: _InvocationContext,
    *,
    name: object,
) -> dict[str, object]:
    if context.preserve or type(name) is not str:
        raise _operation_failure()
    before = _entity_observations(session)
    before_by_id = _observation_map(before)
    create = getattr(session, "create_component", None)
    if not callable(create):
        raise _operation_failure()
    identity = EntityIdentity(
        object_id=f"object_{secrets.token_hex(16)}",
        feature_id=None,
        object_type="App::Part",
        semantic_role=SemanticRole.PART,
        provenance=Provenance(
            source=ProvenanceSource(context.source.value),
            operation_id=context.operation_id,
        ),
    )
    try:
        result = create(name, identity)
    except Exception:
        raise _operation_failure() from None
    if result != {"component": name, "object_id": identity.object_id}:
        raise _operation_failure()
    after = _entity_observations(session)
    after_by_id = _observation_map(after)
    if set(after_by_id) - set(before_by_id) != {identity.object_id}:
        raise _operation_failure()
    comparisons = _require_non_target_preservation(
        before_by_id,
        {key: value for key, value in after_by_id.items() if key != identity.object_id},
        target=None,
    )
    created = after_by_id[identity.object_id]
    if (
        created.feature_id is not None
        or created.object_type != "App::Part"
        or created.semantic_role != "part"
        or created.provenance != identity.provenance.to_mapping()
        or any(
            value is not None
            for value in (
                created.volume_mm3,
                created.area_mm2,
                created.bbox_mm,
                created.center_of_mass_mm,
                created.valid_shape,
                created.solid_count,
            )
        )
    ):
        raise _operation_failure()
    records = session.list_component_identity_records()  # type: ignore[attr-defined]
    if not any(
        part_name == name and component_identity == identity and not members
        for part_name, _container, component_identity, members in records
    ):
        raise _operation_failure()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "component_created",
        "operation": context.operation,
        "component_id": identity.object_id,
        "name": name,
        "after": created.to_mapping(),
        "preservation": [item.to_mapping() for item in comparisons],
    }


def _set_absolute_component_placement(
    session: object,
    *,
    part_name: str,
    position: object,
    rotation_axis: object,
    angle: object,
) -> tuple[int | float, ...]:
    if (
        type(position) is not tuple
        or len(position) != 3
        or type(rotation_axis) is not str
        or rotation_axis not in {"x", "y", "z"}
        or type(angle) not in {int, float}
        or not math.isfinite(angle)
        or not -360.0 < float(angle) < 360.0
    ):
        raise _operation_failure()
    axes = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
    try:
        with session._transaction(  # type: ignore[attr-defined]
            "place_component",
            claim_new_objects=False,
        ):
            with _silence_fd1():
                import FreeCAD  # noqa: PLC0415

                container = session._parts[part_name]["container"]  # type: ignore[attr-defined]
                placement = FreeCAD.Placement(
                    FreeCAD.Vector(*position),
                    FreeCAD.Rotation(FreeCAD.Vector(*axes[rotation_axis]), float(angle)),
                )
                expected = _canonical_placement(placement)
                if _canonical_placement(container.Placement) == expected:
                    raise ValueError
                container.Placement = placement
                session.doc.recompute()  # type: ignore[attr-defined]
                for component_name, info in session._parts.items():  # type: ignore[attr-defined]
                    if info["objects"]:
                        session.assert_valid_solid(  # type: ignore[attr-defined]
                            session.get_result_shape(component_name)  # type: ignore[attr-defined]
                        )
                interferences = _interference_observations(session)
                if any(item.interfering for item in interferences):
                    raise ValueError
        return expected
    except ExecutorError:
        raise
    except Exception:
        raise _operation_failure() from None


def _managed_set_component_bom(
    session: object,
    context: _InvocationContext,
    *,
    project_id: str,
    revision_id: str,
    bom_revision_id: str,
    target: object,
    part_number: object,
    description: object,
    material: object,
    density: object,
) -> dict[str, object]:
    if context.preserve:
        raise _operation_failure()
    try:
        metadata = ComponentBomMetadata(
            part_number=part_number,  # type: ignore[arg-type]
            description=description,  # type: ignore[arg-type]
            material=material,  # type: ignore[arg-type]
            density_kg_m3=density,  # type: ignore[arg-type]
        )
    except Exception:
        raise _operation_failure() from None
    before_entities = _entity_observations(session)
    before_components = _component_observations(session)
    before_by_id = {item.component_id: item for item in before_components}
    before_interferences = _interference_observations(session)
    _container, identity, part_name = _resolve_component_target(
        session,
        target,
        project_id=project_id,
        revision_id=revision_id,
    )
    old_component = before_by_id.get(identity.object_id)
    write_metadata = getattr(session, "set_component_bom_metadata", None)
    if old_component is None or old_component.bom == metadata or not callable(write_metadata):
        raise _operation_failure()
    try:
        observed_metadata = write_metadata(part_name, metadata)
    except Exception:
        raise _operation_failure() from None
    if observed_metadata != metadata:
        raise _operation_failure()
    after_entities = _entity_observations(session)
    after_components = _component_observations(session)
    after_by_id = {item.component_id: item for item in after_components}
    after_interferences = _interference_observations(session)
    new_component = after_by_id.get(identity.object_id)
    if (
        before_entities != after_entities
        or set(before_by_id) != set(after_by_id)
        or before_interferences != after_interferences
        or new_component != replace(old_component, bom=metadata)
        or any(
            before_by_id[component_id] != after_by_id[component_id]
            for component_id in before_by_id
            if component_id != identity.object_id
        )
    ):
        raise _operation_failure()
    bom = _bom_observation(session, after_components)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "component_bom_set",
        "operation": context.operation,
        "component_id": identity.object_id,
        "before": None if old_component.bom is None else old_component.bom.to_mapping(),
        "after": metadata.to_mapping(),
        "bom_revision_id": bom_revision_id,
        "bom": None if bom is None else bom.to_mapping(),
        "bom_csv": _bom_csv(bom),
    }


def _managed_place_component(
    session: object,
    context: _InvocationContext,
    *,
    project_id: str,
    revision_id: str,
    target: object,
    position: object,
    rotation_axis: object,
    angle: object,
) -> dict[str, object]:
    if context.preserve:
        raise _operation_failure()
    before_entities = _entity_observations(session)
    before_by_id = _observation_map(before_entities)
    before_components = {item.component_id: item for item in _component_observations(session)}
    _container, identity, part_name = _resolve_component_target(
        session,
        target,
        project_id=project_id,
        revision_id=revision_id,
    )
    old_component = before_components.get(identity.object_id)
    if old_component is None:
        raise _operation_failure()
    expected_placement = _set_absolute_component_placement(
        session,
        part_name=part_name,
        position=position,
        rotation_axis=rotation_axis,
        angle=angle,
    )
    after_entities = _entity_observations(session)
    after_by_id = _observation_map(after_entities)
    comparisons = _require_non_target_preservation(
        before_by_id,
        after_by_id,
        target=identity.object_id,
    )
    new_entity = after_by_id.get(identity.object_id)
    if new_entity is None or new_entity.placement != expected_placement:
        raise _operation_failure()
    after_components = {item.component_id: item for item in _component_observations(session)}
    new_component = after_components.get(identity.object_id)
    if new_component is None or new_component.placement != expected_placement:
        raise _operation_failure()
    if set(before_components) != set(after_components) or any(
        before_components[component_id] != after_components[component_id]
        for component_id in before_components
        if component_id != identity.object_id
    ):
        raise _operation_failure()
    if (
        old_component.component_id != new_component.component_id
        or old_component.object_type != new_component.object_type
        or old_component.provenance != new_component.provenance
        or old_component.member_object_ids != new_component.member_object_ids
        or not _same_geometry_number(
            old_component.volume_mm3,
            new_component.volume_mm3,
        )
        or not _same_geometry_number(old_component.area_mm2, new_component.area_mm2)
        or old_component.valid_shape is not new_component.valid_shape
        or old_component.solid_count != new_component.solid_count
    ):
        raise _operation_failure()
    interferences = _interference_observations(session)
    if any(item.interfering for item in interferences):
        raise _operation_failure()
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "component_placed",
        "operation": context.operation,
        "component_id": identity.object_id,
        "before": old_component.to_mapping(),
        "after": new_component.to_mapping(),
        "interferences": [item.to_mapping() for item in interferences],
        "preservation": [item.to_mapping() for item in comparisons],
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


def _export_session_step(
    *,
    session: object,
    model_path: Path,
    step_path: Path,
) -> None:
    """Export one session to an already reserved empty STEP placeholder."""

    if (
        session is None
        or type(model_path) is not type(Path())
        or type(step_path) is not type(Path())
        or model_path.name != "model.FCStd"
        or step_path.name != "model.step"
        or model_path.parent != step_path.parent
    ):
        raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
    try:
        existing = os.lstat(step_path)
    except (FileNotFoundError, OSError):
        raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
    placeholder_identity = _step_placeholder_identity(existing)
    if placeholder_identity is None:
        raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE)
    try:
        parent = os.lstat(step_path.parent)
        if not stat.S_ISDIR(parent.st_mode):
            raise _ArtifactReadFailure
        _stabilize_parametric_session(session)
        shape = _managed_assembly_shape(session)
    except _ArtifactReadFailure:
        raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
    except Exception:
        raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
    try:
        with _silence_fd1():
            shape.exportStep(str(step_path))
        after_export = os.lstat(step_path)
        if not _step_output_matches_placeholder(
            after_export,
            placeholder_identity,
        ):
            raise _ArtifactReadFailure
        _read_artifact(step_path, "step")
        after_read = os.lstat(step_path)
        if not _step_output_matches_placeholder(
            after_read,
            placeholder_identity,
        ):
            raise _ArtifactReadFailure
    except _ArtifactReadFailure:
        raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
    except Exception:
        raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None


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
                failed = _prefer_cleanup_failure(failed, close_error)
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
                failed = _prefer_cleanup_failure(failed, close_error)
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

        cad_failure: ExecutorError | None = None
        try:
            try:
                session.load_document(path)
                session.doc.recompute()
                _validated_import_observations(session)
            except ExecutorError as error:
                # A read-only recovery probe may expose two distinct states through
                # INVALID_INPUT: an unsupported source envelope, or an interrupted
                # identity-normalization pass over otherwise supported primitives.
                # Only the former is a terminal caller error.  The latter remains a
                # CAD/recovery failure so a crash cannot be mislabeled as bad input.
                code = ExecutorErrorCode.CAD_FAILURE
                if type(error) is ExecutorError and error.code is ExecutorErrorCode.INVALID_INPUT:
                    unsupported_envelope = False
                    try:
                        objects = tuple(session.doc.Objects)
                        unsupported_envelope = not objects or any(
                            not _supported_import_object_type(obj) for obj in objects
                        )
                    except BaseException:
                        # Failure to inspect the envelope is an internal CAD fault,
                        # not evidence that the caller supplied unsupported input.
                        unsupported_envelope = False
                    if unsupported_envelope:
                        code = ExecutorErrorCode.INVALID_INPUT
                cad_failure = _fixed_error(code)
            except _SessionLifecycleError:
                raise
            except BaseException:
                cad_failure = _fixed_error(ExecutorErrorCode.CAD_FAILURE)
        finally:
            try:
                session.close_document()
            except _SessionLifecycleError:
                raise
            except BaseException:
                cad_failure = _fixed_error(ExecutorErrorCode.CAD_FAILURE)
        if cad_failure is not None:
            raise cad_failure

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
                failed = _prefer_cleanup_failure(failed, close_error)
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

    def render_release(self, *, revision: object) -> ReleaseCadEvidence:
        """Derive a bounded assembly drawing and BOM without mutating a Revision."""

        if type(revision) is not RevisionRef:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT)
        try:
            path = self._store.revision_model_path(revision.project_id, revision.id)
            identity_before = _stat_identity(os.lstat(path))
            artifact_before = _read_artifact(path, "fcstd")
        except Exception:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if (
            artifact_before.sha256 != revision.model.sha256
            or artifact_before.size_bytes != revision.model.size_bytes
        ):
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        session = self.load_fcstd(path)
        evidence = None
        failed: ExecutorError | None = None
        try:
            try:
                session.doc.recompute()
                components = _component_observations(session)
                bom = _bom_observation(session, components)
                if bom is None or not bom.complete:
                    raise _ObservationFailure
                from vibecad.feedback.release_drawing import (  # noqa: PLC0415
                    DRAWING_VIEWS,
                    render_assembly_drawing,
                )

                drawing, balloon_items = render_assembly_drawing(
                    session,
                    bom=bom,
                    project_id=revision.project_id,
                    revision_id=revision.id,
                )
                evidence = ReleaseCadEvidence(
                    revision_id=revision.id,
                    bom=bom,
                    drawing_pdf=drawing,
                    view_names=DRAWING_VIEWS,
                    balloon_items=balloon_items,
                )
            except Exception:
                raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        except ExecutorError as error:
            failed = error
        finally:
            try:
                self.close(session)
            except ExecutorError as close_error:
                failed = _prefer_cleanup_failure(failed, close_error)
        if failed is not None:
            raise failed
        try:
            identity_after = _stat_identity(os.lstat(path))
            artifact_after = _read_artifact(path, "fcstd")
        except Exception:
            raise _fixed_error(ExecutorErrorCode.ARTIFACT_FAILURE) from None
        if identity_after != identity_before or artifact_after != artifact_before:
            raise _fixed_error(ExecutorErrorCode.INTEGRITY_FAILURE)
        assert evidence is not None
        return evidence

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
        except _SessionLifecycleError:
            raise _fixed_error(ExecutorErrorCode.INTERNAL_FAILURE) from None
        except Exception:
            try:
                session.close_document()
            except _SessionLifecycleError:
                raise _fixed_error(ExecutorErrorCode.INTERNAL_FAILURE) from None
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
            _entity_observations(session)
        except _SessionLifecycleError:
            raise _fixed_error(ExecutorErrorCode.INTERNAL_FAILURE) from None
        except Exception:
            try:
                session.close_document()
            except _SessionLifecycleError:
                raise _fixed_error(ExecutorErrorCode.INTERNAL_FAILURE) from None
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
            _stabilize_parametric_session(session)
            _entity_observations(session)
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
        except _SessionLifecycleError:
            raise _fixed_error(ExecutorErrorCode.INTERNAL_FAILURE) from None
        except Exception:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None

    def execute_program(
        self,
        *,
        program: ValidatedProgram,
        candidate: ActiveCandidate,
    ) -> tuple[NormalizedToolOutcome, ...]:
        """Execute one authentic program using the six fixed CAD bindings."""

        (
            prepared,
            handlers,
            revision_id,
            freecad_version,
            object_count,
        ) = self._prepare_program_invocation(
            program=program,
            candidate=candidate,
        )
        try:
            return _execute_validated_program(
                prepared,
                handlers,
                execution_profile=self.execution_profile,
                revision=revision_id,
                freecad_version=freecad_version,
                gui_main_thread=False,
                object_count=object_count,
            )
        except _AdapterError:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None

    def _prepare_program_execution(
        self,
        *,
        program: ValidatedProgram,
        candidate: ActiveCandidate,
    ):
        """Build the private command cursor used by in-process and Worker CAD."""

        (
            prepared,
            handlers,
            revision_id,
            freecad_version,
            object_count,
        ) = self._prepare_program_invocation(
            program=program,
            candidate=candidate,
        )
        try:
            return _prepare_validated_program_execution(
                prepared,
                handlers,
                execution_profile=self.execution_profile,
                revision=revision_id,
                freecad_version=freecad_version,
                gui_main_thread=False,
                object_count=object_count,
            )
        except _AdapterError:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None

    def _prepare_program_invocation(
        self,
        *,
        program: ValidatedProgram,
        candidate: ActiveCandidate,
    ) -> tuple[
        ValidatedProgram,
        dict[str, Callable[..., object]],
        str,
        tuple[int, int],
        Callable[[], int],
    ]:
        """Resolve one authentic program to fixed executor-owned arguments."""

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
            fixed_leaves = (
                _add_box,
                _add_cone,
                _add_cylinder,
                _add_sphere,
                _add_torus,
                _boolean_common_uncommitted,
                _boolean_cut_uncommitted,
                _boolean_fuse_uncommitted,
                _modify_part,
                _move_part,
                _rotate_part,
                _set_absolute_component_placement,
                _compile_parametric_design,
                _modify_parametric_parameter,
                _execute_reviewed_intent_native,
            )
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
            candidate_revision_id = candidate.binding.revision_id
            handlers = {
                "create_box": _queued_handler(
                    contexts.get("create_box", deque()),
                    partial(
                        _managed_create,
                        session,
                        leaf=_add_box,
                        expected_type="Part::Box",
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "create_cylinder": _queued_handler(
                    contexts.get("create_cylinder", deque()),
                    partial(
                        _managed_create,
                        session,
                        leaf=_add_cylinder,
                        expected_type="Part::Cylinder",
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "create_cone": _queued_handler(
                    contexts.get("create_cone", deque()),
                    partial(
                        _managed_create,
                        session,
                        leaf=_add_cone,
                        expected_type="Part::Cone",
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "create_sphere": _queued_handler(
                    contexts.get("create_sphere", deque()),
                    partial(
                        _managed_create,
                        session,
                        leaf=_add_sphere,
                        expected_type="Part::Sphere",
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "create_torus": _queued_handler(
                    contexts.get("create_torus", deque()),
                    partial(
                        _managed_create,
                        session,
                        leaf=_add_torus,
                        expected_type="Part::Torus",
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "boolean_cut": _queued_handler(
                    contexts.get("boolean_cut", deque()),
                    partial(
                        _managed_boolean,
                        session,
                        leaf=_boolean_cut_uncommitted,
                        expected_type="Part::Cut",
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "boolean_fuse": _queued_handler(
                    contexts.get("boolean_fuse", deque()),
                    partial(
                        _managed_boolean,
                        session,
                        leaf=_boolean_fuse_uncommitted,
                        expected_type="Part::Fuse",
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "boolean_common": _queued_handler(
                    contexts.get("boolean_common", deque()),
                    partial(
                        _managed_boolean,
                        session,
                        leaf=_boolean_common_uncommitted,
                        expected_type="Part::Common",
                        project_id=project_id,
                        revision_id=revision_id,
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
                    partial(
                        _managed_inspect,
                        session,
                        revision_id=candidate_revision_id,
                    ),
                ),
                "create_component": _queued_handler(
                    contexts.get("create_component", deque()),
                    partial(_managed_create_component, session),
                ),
                "set_component_bom": _queued_handler(
                    contexts.get("set_component_bom", deque()),
                    partial(
                        _managed_set_component_bom,
                        session,
                        project_id=project_id,
                        revision_id=revision_id,
                        bom_revision_id=candidate_revision_id,
                    ),
                ),
                "place_component": _queued_handler(
                    contexts.get("place_component", deque()),
                    partial(
                        _managed_place_component,
                        session,
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "create_parametric_design": _queued_handler(
                    contexts.get("create_parametric_design", deque()),
                    partial(_managed_create_parametric_design, session),
                ),
                "modify_parametric_parameter": _queued_handler(
                    contexts.get("modify_parametric_parameter", deque()),
                    partial(
                        _managed_modify_parametric_parameter,
                        session,
                        project_id=project_id,
                        revision_id=revision_id,
                    ),
                ),
                "apply_reviewed_intent": _queued_handler(
                    contexts.get("apply_reviewed_intent", deque()),
                    partial(_managed_apply_reviewed_intent, session),
                ),
            }
        except ExecutorError:
            raise
        except Exception:
            raise _fixed_error(ExecutorErrorCode.INVALID_INPUT) from None
        freecad_version = _session_freecad_version(session)
        return (
            program,
            handlers,
            candidate.binding.revision_id,
            freecad_version,
            partial(_document_object_count, session),
        )

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
        _export_session_step(
            session=candidate.binding.session,
            model_path=candidate.model_path,
            step_path=trusted_path,
        )

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
            live_components = _component_observations(candidate.binding.session)
            live_interferences = _interference_observations(candidate.binding.session)
            live_bom = _bom_observation(candidate.binding.session, live_components)
        except _ObservationFailure:
            raise _fixed_error(ExecutorErrorCode.CAD_FAILURE) from None
        try:
            sealed_shape, entities, components, interferences, bom = _reloaded_observations(
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
        if (
            sealed_shape is None
            or not _same_shape_observation(sealed_shape, live_shape)
            or not _same_import_observations(entities, live_entities)
            or components != live_components
            or interferences != live_interferences
            or bom != live_bom
        ):
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
                (
                    _,
                    before_entities,
                    _before_components,
                    _before_interferences,
                    _before_bom,
                ) = _reloaded_observations(
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
            components=components,
            interferences=interferences,
            bom=bom,
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
