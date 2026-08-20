"""Trusted in-process CAD execution and observation boundary tests."""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import vibecad.execution.executor as executor_module
import vibecad.execution.freecad_part_profile_surface_reviewed_execution as profile_execution
from tests.test_execution_freecad_part_curve_reviewed_execution import (
    _reviewed_program as reviewed_curve_program,
)
from tests.test_execution_freecad_part_offset_projection_reviewed_execution import (
    _program as reviewed_offset_program,
)
from tests.test_execution_freecad_part_profile_surface_reviewed_execution import (
    _program as reviewed_profile_surface_program,
)
from tests.test_execution_freecad_partdesign_dressup_transform_reviewed_execution import (
    _lower as lower_reviewed_dressup,
)
from tests.test_execution_freecad_partdesign_dressup_transform_reviewed_execution import (
    _program as reviewed_dressup_program,
)
from tests.test_execution_freecad_partdesign_primitive_reviewed_execution import (
    _program as reviewed_partdesign_primitive_program,
)
from tests.test_intent_bridge_freecad_part_datum_adapter import _graph as _datum_graph
from tests.test_reviewed_intent_program import reviewed_box_program, reviewed_primitive_program
from tests.test_reviewed_part_csg_product import reviewed_csg_program
from vibecad import _file_compat
from vibecad.execution.candidate import (
    ActiveCandidate,
    CadSnapshotPort,
    CheckpointedCandidate,
    SealedCandidate,
    SessionBinding,
)
from vibecad.execution.executor import (
    CandidateEvidence,
    ExecutorError,
    ExecutorErrorCode,
    InProcessCadExecutor,
)
from vibecad.execution.freecad_part_offset_projection_reviewed_execution import (
    PART_OFFSET_RESULT_INVARIANTS,
    PartOffsetOwnershipClosure,
)
from vibecad.execution.freecad_part_profile_surface_reviewed_execution import (
    PART_PROFILE_SURFACE_RESULT_INVARIANTS,
    PartProfileSurfaceOwnershipClosure,
)
from vibecad.execution.freecad_partdesign_dressup_transform_reviewed_execution import (
    PartDesignDressupOwnershipClosure,
)
from vibecad.execution.freecad_partdesign_primitive_reviewed_execution import (
    PARTDESIGN_PRIMITIVE_RESULT_INVARIANTS,
    PartDesignPrimitiveOwnershipClosure,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    REVIEWED_PART_BOX_ROUTE,
    REVIEWED_PART_CSG_ROUTES,
    REVIEWED_PART_CURVE_ROUTES,
    REVIEWED_PART_DATUM_ROUTES,
    REVIEWED_PART_OFFSET_ROUTES,
    REVIEWED_PART_PROFILE_SURFACE_ROUTES,
    REVIEWED_PARTDESIGN_DRESSUP_ROUTES,
    REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
    _ReviewedFamilyExecutionContext,
    _ReviewedFamilyNativeExecution,
    lower_reviewed_intent,
)
from vibecad.execution.registry import (
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    RiskClass,
    ValueShape,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
    index_entity_identities,
)
from vibecad.intent_bridge.freecad_part_datum_adapter import PART_DATUM_MANIFEST
from vibecad.parametric import freecad_partdesign_dressup_transform_rules as dressup_rules
from vibecad.parametric.freecad_part_core_rules import (
    PART_CORE_NATIVE_SPECS,
    PartCoreConformanceReceipt,
    PartCoreOperation,
)
from vibecad.parametric.freecad_part_curve_rules import (
    PartCurveConformanceReceipt,
    PartCurveOperation,
    PartCurveShapeSignature,
)
from vibecad.parametric.freecad_part_datum_rules import (
    PART_DATUM_NATIVE_TYPE_IDS,
    PartDatumConformanceReceipt,
    PartDatumOperation,
)
from vibecad.parametric.freecad_part_offset_projection_rules import (
    PartOffsetConformanceReceipt,
    PartOffsetOperation,
)
from vibecad.parametric.freecad_part_profile_surface_rules import (
    PartProfileSurfaceConformanceReceipt,
    PartProfileSurfaceOperation,
)
from vibecad.parametric.freecad_partdesign_dressup_transform_rules import (
    MultiTransformParameters,
    PartDesignDressupTransformConformanceReceipt,
    PartDesignDressupTransformOperation,
)
from vibecad.parametric.freecad_partdesign_primitive_rules import (
    PartDesignPrimitiveConformanceReceipt,
    PartDesignPrimitiveOperation,
)
from vibecad.validation import ComponentBomMetadata
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram, validate_model_program
from vibecad.workflow.reviewed_intent import ReviewedIntentProgramV1
from vibecad.workflow.state import TaskArtifactRef

PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
BASE_REVISION = "revision_0123456789abcdef0123456789abcdef"
CANDIDATE_REVISION = "revision_11111111111111111111111111111111"
MODEL_ID = "artifact_0123456789abcdef0123456789abcdef"
STEP_ID = "artifact_11111111111111111111111111111111"
DIGEST = "a" * 64


class _FakeVector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = x
        self.y = y
        self.z = z


class _FakeBoundBox:
    def __init__(
        self,
        x: float = 12.0,
        y: float = 20.0,
        z: float = 30.0,
        *,
        center: tuple[float, float, float] | None = None,
    ) -> None:
        self.XLength = x
        self.YLength = y
        self.ZLength = z
        cx, cy, cz = center or (x / 2.0, y / 2.0, z / 2.0)
        self.XMin = cx - x / 2.0
        self.XMax = cx + x / 2.0
        self.YMin = cy - y / 2.0
        self.YMax = cy + y / 2.0
        self.ZMin = cz - z / 2.0
        self.ZMax = cz + z / 2.0

    def translate(self, x: float, y: float, z: float) -> None:
        self.XMin += x
        self.XMax += x
        self.YMin += y
        self.YMax += y
        self.ZMin += z
        self.ZMax += z


class _FakeShape:
    def __init__(
        self,
        *,
        export_error: BaseException | None = None,
        volume: float = 7200.0,
        area: float = 2400.0,
        shape_type: str = "Solid",
        vertex_count: int = 1,
        edge_count: int = 1,
        face_count: int = 1,
        solid_count: int = 1,
        wire_closed: bool = False,
        bbox: tuple[float, float, float] = (12.0, 20.0, 30.0),
        center: tuple[float, float, float] = (6.0, 10.0, 15.0),
        bbox_center: tuple[float, float, float] | None = None,
    ) -> None:
        self.Volume = volume
        self.Area = area
        self.BoundBox = _FakeBoundBox(*bbox, center=bbox_center or center)
        self.CenterOfMass = _FakeVector(*center)
        self.ShapeType = shape_type
        self.Vertexes = tuple(object() for _ in range(vertex_count))
        self.Edges = tuple(object() for _ in range(edge_count))
        self.Faces = tuple(object() for _ in range(face_count))
        self.Solids = tuple(object() for _ in range(solid_count))
        self.Wires = (
            (SimpleNamespace(isClosed=lambda: wire_closed),) if shape_type == "Wire" else ()
        )
        self.Length = max(bbox)
        self.export_error = export_error
        self.export_calls: list[str] = []

    def isValid(self) -> bool:
        return True

    def isNull(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return False

    def exportStep(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self.export_calls.append(path)
        if self.export_error is not None:
            raise self.export_error
        Path(path).write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")

    def exportBrepToString(self) -> str:  # noqa: N802 - FreeCAD API spelling
        return repr(
            (
                self.Volume,
                self.Area,
                self.BoundBox.XLength,
                self.BoundBox.YLength,
                self.BoundBox.ZLength,
                self.CenterOfMass.x,
                self.CenterOfMass.y,
                self.CenterOfMass.z,
                self.ShapeType,
                len(self.Edges),
                len(self.Faces),
                len(self.Solids),
            )
        )

    def transformed(self, matrix):
        x, y, z = matrix[:3]
        return _FakeShape(
            volume=self.Volume,
            area=self.Area,
            shape_type=self.ShapeType,
            edge_count=len(self.Edges),
            face_count=len(self.Faces),
            solid_count=len(self.Solids),
            bbox=(self.BoundBox.XLength, self.BoundBox.YLength, self.BoundBox.ZLength),
            center=(
                self.CenterOfMass.x + x,
                self.CenterOfMass.y + y,
                self.CenterOfMass.z + z,
            ),
        )

    def common(self, other):
        bounds = []
        for axis in ("X", "Y", "Z"):
            low = max(getattr(self.BoundBox, f"{axis}Min"), getattr(other.BoundBox, f"{axis}Min"))
            high = min(
                getattr(self.BoundBox, f"{axis}Max"),
                getattr(other.BoundBox, f"{axis}Max"),
            )
            bounds.append(max(0.0, high - low))
        return SimpleNamespace(Volume=math.prod(bounds))


class _FakeDocument:
    def __init__(self) -> None:
        self.recompute_calls = 0
        self.save_calls: list[str] = []
        self.Objects: tuple[object, ...] = ()
        self._transaction_objects: tuple[object, ...] | None = None
        self._recompute_observers: list[object] = []
        self.Name = "FakeDocument"

    def recompute(self) -> None:
        self.recompute_calls += 1
        for obj in self.Objects:
            for observer in tuple(self._recompute_observers):
                observer.slotRecomputedObject(obj)

    def isTouched(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return False

    def getObject(self, name: str):  # noqa: N802 - FreeCAD API spelling
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)

    def openTransaction(self, _label: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self._transaction_objects = tuple(self.Objects)

    def commitTransaction(self) -> None:  # noqa: N802 - FreeCAD API spelling
        self._transaction_objects = None

    def abortTransaction(self) -> None:  # noqa: N802 - FreeCAD API spelling
        if self._transaction_objects is not None:
            self.Objects = self._transaction_objects
        self._transaction_objects = None

    def removeObject(self, name: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self.Objects = tuple(item for item in self.Objects if getattr(item, "Name", None) != name)

    def saveCopy(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self.save_calls.append(path)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Document.xml", "<Document />")


class _FakeSession:
    freecad_version = (1, 1)

    def __init__(self, shape: _FakeShape | None = None) -> None:
        self.doc = _FakeDocument()
        self.shape = shape or _FakeShape()
        self.persist_calls = 0
        self.loaded: list[Path] = []
        self.opened: list[str] = []
        self.close_calls = 0
        self.shape_calls = 0
        self.identity_object = type("ManagedBox", (), {})()
        self.identity_object.Name = "Box"
        self.identity_object.TypeId = "Part::Box"
        self.identity_object.Length = 10.0
        self.identity_object.Width = 20.0
        self.identity_object.Height = 30.0
        self.identity_object.Placement = _FakePlacement(0.0)
        self.identity_object.Shape = _FakeShape()
        self.identity_object.State = []
        self.attached_identities: list[tuple[object, object]] = []
        self.result_object: object | None = None

    @contextlib.contextmanager
    def _transaction(
        self,
        _label: str,
        part: str | None = None,
        *,
        claim_new_objects: bool = True,
    ):
        del claim_new_objects
        objects_before = self.doc.Objects
        object_state_before = tuple(
            (
                obj,
                {
                    name: copy.deepcopy(getattr(obj, name))
                    for name in (
                        "Length",
                        "Width",
                        "Height",
                        "Radius",
                        "Radius1",
                        "Radius2",
                        "Placement",
                        "Shape",
                    )
                    if hasattr(obj, name)
                },
            )
            for obj in objects_before
        )
        identities_before = list(self.attached_identities)
        result_before = self.result_object
        result_by_part_before = dict(getattr(self, "_result_by_part", {}))
        parts_before = {
            name: {**info, "objects": set(info["objects"])}
            for name, info in getattr(self, "_parts", {}).items()
        }
        try:
            yield
        except BaseException:
            self.doc.Objects = objects_before
            for obj, values in object_state_before:
                for name, value in values.items():
                    setattr(obj, name, value)
            self.attached_identities = identities_before
            self.result_object = result_before
            if hasattr(self, "_result_by_part"):
                self._result_by_part = result_by_part_before
            if hasattr(self, "_parts"):
                self._parts = {
                    name: {**info, "objects": set(info["objects"])}
                    for name, info in parts_before.items()
                }
            raise

    def load_document(self, path: Path) -> object:
        self.loaded.append(path)
        return self.doc

    def open_document(self, name: str) -> object:
        self.opened.append(name)
        return self.doc

    def persist_state(self) -> None:
        self.persist_calls += 1

    def close_document(self) -> None:
        self.close_calls += 1

    def get_assembly_shape(self) -> _FakeShape:
        self.shape_calls += 1
        return self.shape

    def get_object(self, name: str) -> object:
        for obj in self.doc.Objects:
            if getattr(obj, "Name", None) == name:
                return obj
        if name == "Box":
            return self.identity_object
        raise KeyError(name)

    def attach_object_identity(self, obj: object, identity: object) -> object:
        obj.Document = self.doc  # type: ignore[attr-defined]
        obj.VibeCADObjectId = identity.object_id  # type: ignore[attr-defined]
        obj.VibeCADFeatureId = identity.feature_id or ""  # type: ignore[attr-defined]
        obj.VibeCADSemanticRole = identity.semantic_role.value  # type: ignore[attr-defined]
        obj.VibeCADProvenance = (  # type: ignore[attr-defined]
            '{"operation_id":"'
            + str(identity.provenance.operation_id)
            + '","source":"'
            + identity.provenance.source.value
            + '"}'
        )
        if not any(current is obj for current in self.doc.Objects):
            self.doc.Objects = (*self.doc.Objects, obj)
        self.attached_identities.append((obj, identity))
        return identity

    def read_object_identity(self, obj: object) -> object:
        for current, identity in reversed(self.attached_identities):
            if current is obj:
                return identity
        raise ValueError("identity missing")

    def list_object_identities(self) -> tuple[tuple[object, object], ...]:
        identities = index_entity_identities(self.doc.Objects)
        return tuple(zip(self.doc.Objects, identities, strict=True))

    def set_result_object(self, obj: object, part: str | None = None) -> None:
        del part
        self.result_object = obj

    def get_result_object(self, part: str | None = None) -> object:
        del part
        if self.result_object is None:
            raise RuntimeError("result object missing")
        return self.result_object

    def assert_valid_solid(self, shape: object) -> None:
        if not shape.isValid() or shape.Volume <= 0:  # type: ignore[attr-defined]
            raise RuntimeError("invalid fake solid")

    def owner_of(self, object_name: str) -> str | None:
        del object_name
        return None


class _FakeRotation:
    def __init__(self, q: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)) -> None:
        self.Q = q


class _FakePlacement:
    def __init__(
        self,
        x: float,
        y: float = 0.0,
        z: float = 0.0,
        *,
        q: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
    ) -> None:
        self.Base = _FakeVector(x, y, z)
        self.Rotation = _FakeRotation(q)

    def toMatrix(self):  # noqa: N802 - FreeCAD API spelling
        return (self.Base.x, self.Base.y, self.Base.z, *self.Rotation.Q)


class _FakeEntity:
    def __init__(self, suffix: str, *, x: float, length: float) -> None:
        self.VibeCADObjectId = f"object_{suffix * 32}"
        self.VibeCADFeatureId = f"feature_{suffix * 32}"
        self.VibeCADSemanticRole = "primitive"
        self.VibeCADProvenance = '{"operation_id":"box","source":"model"}'
        self.TypeId = "Part::Box"
        self.Length = length
        self.Width = 20.0
        self.Height = 30.0
        self.Placement = _FakePlacement(x)
        self.Shape = _FakeShape()


class _FakeComponentSession(_FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self._parts: dict[str, dict[str, object]] = {}
        self._result_by_part: dict[str, object] = {}
        self._bom_by_part: dict[str, ComponentBomMetadata] = {}

    def create_component(self, name: str, identity: object) -> dict[str, object]:
        container = SimpleNamespace(
            Name=f"VibePart{len(self._parts)}",
            Label=name,
            TypeId="App::Part",
            Placement=_FakePlacement(0.0),
        )
        self.attach_object_identity(container, identity)
        self._parts[name] = {"container": container, "objects": set()}
        return {"component": name, "object_id": identity.object_id}

    def list_component_identity_records(self):
        identities = {obj.Name: identity for obj, identity in self.list_object_identities()}
        records = []
        for part_name, info in self._parts.items():
            container = info["container"]
            members = tuple(
                sorted(
                    ((self.get_object(name), identities[name]) for name in info["objects"]),
                    key=lambda item: item[1].object_id,
                )
            )
            records.append((part_name, container, identities[container.Name], members))
        return tuple(sorted(records, key=lambda item: item[2].object_id))

    def owner_of(self, object_name: str) -> str | None:
        return next(
            (
                part_name
                for part_name, info in self._parts.items()
                if object_name in info["objects"]
            ),
            None,
        )

    def set_result_object(self, obj: object, part: str | None = None) -> None:
        if part is None:
            super().set_result_object(obj)
            return
        self._result_by_part[part] = obj

    def get_result_object(self, part: str | None = None) -> object:
        if part is None:
            return super().get_result_object()
        return self._result_by_part[part]

    def _claim_new_objects(self, before: set[str], part: str | None = None) -> None:
        assert part is not None
        self._parts[part]["objects"].update(  # type: ignore[attr-defined]
            obj.Name for obj in self.doc.Objects if obj.Name not in before
        )

    def get_result_shape(self, part_name: str):
        return self._result_by_part[part_name].Shape

    def assert_valid_solid(self, shape: object) -> None:
        if not shape.isValid() or shape.Volume <= 0:  # type: ignore[attr-defined]
            raise RuntimeError("invalid fake solid")

    def read_component_bom_metadata(self, part_name: str) -> ComponentBomMetadata | None:
        return self._bom_by_part.get(part_name)

    def set_component_bom_metadata(
        self,
        part_name: str,
        metadata: ComponentBomMetadata,
    ) -> ComponentBomMetadata:
        self._bom_by_part[part_name] = metadata
        return metadata

    def get_assembly_shape(self):
        shapes = [
            self.get_result_shape(part_name).transformed(info["container"].Placement.toMatrix())
            for part_name, info in self._parts.items()
            if info["objects"]
        ]
        if not shapes:
            raise RuntimeError("empty assembly")
        mins = [min(getattr(shape.BoundBox, f"{axis}Min") for shape in shapes) for axis in "XYZ"]
        maxs = [max(getattr(shape.BoundBox, f"{axis}Max") for shape in shapes) for axis in "XYZ"]
        volume = sum(shape.Volume for shape in shapes)
        center = tuple(
            sum(shape.Volume * getattr(shape.CenterOfMass, axis) for shape in shapes) / volume
            for axis in "xyz"
        )
        return _FakeShape(
            volume=volume,
            area=sum(shape.Area for shape in shapes),
            bbox=tuple(high - low for low, high in zip(mins, maxs, strict=True)),
            center=center,
            bbox_center=tuple((low + high) / 2 for low, high in zip(mins, maxs, strict=True)),
        )


def _fake_add_box(
    session: _FakeSession,
    *,
    length: float,
    width: float,
    height: float,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    part: str | None = None,
) -> object:
    obj = session.identity_object
    if any(current is obj for current in session.doc.Objects):
        obj = type("ManagedBox", (), {})()
        obj.Name = f"Box{len(session.doc.Objects):03d}"
        obj.TypeId = "Part::Box"
    obj.Length = length
    obj.Width = width
    obj.Height = height
    obj.Placement = _FakePlacement(*position)
    obj.Shape = _FakeShape(
        volume=length * width * height,
        area=2 * (length * width + length * height + width * height),
        bbox=(length, width, height),
        center=(
            position[0] + length / 2,
            position[1] + width / 2,
            position[2] + height / 2,
        ),
    )
    obj.State = []
    session.doc.Objects = (*session.doc.Objects, obj)
    if part is not None:
        session._parts[part]["objects"].add(obj.Name)  # type: ignore[attr-defined]
    session.set_result_object(obj, part=part)
    return object()


def _fake_add_cylinder(
    session: _FakeSession,
    *,
    radius: float,
    height: float,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    axis: str = "z",
    part: str | None = None,
) -> object:
    obj = type("ManagedCylinder", (), {})()
    obj.Name = "Cylinder"
    obj.TypeId = "Part::Cylinder"
    obj.Radius = radius
    obj.Height = height
    obj.Angle = 360.0
    sine = math.sin(math.pi / 4)
    rotations = {
        "x": (0.0, sine, 0.0, sine),
        "y": (-sine, 0.0, 0.0, sine),
        "z": (0.0, 0.0, 0.0, 1.0),
    }
    q = rotations[axis]
    obj.Placement = _FakePlacement(*position, q=q)
    if axis == "x":
        bbox = (height, 2 * radius, 2 * radius)
        center_offset = (height / 2, 0.0, 0.0)
    elif axis == "y":
        bbox = (2 * radius, height, 2 * radius)
        center_offset = (0.0, height / 2, 0.0)
    else:
        bbox = (2 * radius, 2 * radius, height)
        center_offset = (0.0, 0.0, height / 2)
    obj.Shape = _FakeShape(
        volume=math.pi * radius**2 * height,
        area=2 * math.pi * radius * (radius + height),
        bbox=bbox,
        center=tuple(
            origin + offset for origin, offset in zip(position, center_offset, strict=True)
        ),
    )
    obj.State = []
    session.doc.Objects = (*session.doc.Objects, obj)
    if part is not None:
        session._parts[part]["objects"].add(obj.Name)  # type: ignore[attr-defined]
    session.set_result_object(obj, part=part)
    return object()


def _axis_quaternion(axis: str) -> tuple[float, float, float, float]:
    sine = math.sin(math.pi / 4)
    return {
        "x": (0.0, sine, 0.0, sine),
        "y": (-sine, 0.0, 0.0, sine),
        "z": (0.0, 0.0, 0.0, 1.0),
    }[axis]


def _fake_add_cone(
    session: _FakeSession,
    *,
    radius1: float,
    height: float,
    radius2: float = 0.0,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    axis: str = "z",
    part: str | None = None,
) -> object:
    obj = type("ManagedCone", (), {})()
    obj.Name = "Cone"
    obj.TypeId = "Part::Cone"
    obj.Radius1 = radius1
    obj.Radius2 = radius2
    obj.Height = height
    obj.Angle = 360.0
    obj.Placement = _FakePlacement(*position, q=_axis_quaternion(axis))
    radius_sum = radius1**2 + radius1 * radius2 + radius2**2
    center_z = height * (radius1**2 + 2 * radius1 * radius2 + 3 * radius2**2) / (4 * radius_sum)
    center_offsets = {
        "x": (center_z, 0.0, 0.0),
        "y": (0.0, center_z, 0.0),
        "z": (0.0, 0.0, center_z),
    }
    diameter = 2 * max(radius1, radius2)
    bboxes = {
        "x": (height, diameter, diameter),
        "y": (diameter, height, diameter),
        "z": (diameter, diameter, height),
    }
    bbox_offsets = {
        "x": (height / 2, 0.0, 0.0),
        "y": (0.0, height / 2, 0.0),
        "z": (0.0, 0.0, height / 2),
    }
    center = tuple(
        origin + offset for origin, offset in zip(position, center_offsets[axis], strict=True)
    )
    bbox_center = tuple(
        origin + offset for origin, offset in zip(position, bbox_offsets[axis], strict=True)
    )
    slant = math.hypot(height, radius1 - radius2)
    obj.Shape = _FakeShape(
        volume=math.pi * height * radius_sum / 3,
        area=math.pi * (radius1**2 + radius2**2 + (radius1 + radius2) * slant),
        bbox=bboxes[axis],
        center=center,
        bbox_center=bbox_center,
    )
    session.doc.Objects = (*session.doc.Objects, obj)
    if part is not None:
        session._parts[part]["objects"].add(obj.Name)  # type: ignore[attr-defined]
    session.set_result_object(obj, part=part)
    return object()


def _fake_add_sphere(
    session: _FakeSession,
    *,
    radius: float,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    part: str | None = None,
) -> object:
    obj = type("ManagedSphere", (), {})()
    obj.Name = "Sphere"
    obj.TypeId = "Part::Sphere"
    obj.Radius = radius
    obj.Angle1 = -90.0
    obj.Angle2 = 90.0
    obj.Angle3 = 360.0
    obj.Placement = _FakePlacement(*position)
    obj.Shape = _FakeShape(
        volume=4 * math.pi * radius**3 / 3,
        area=4 * math.pi * radius**2,
        bbox=(2 * radius, 2 * radius, 2 * radius),
        center=position,
        bbox_center=position,
    )
    session.doc.Objects = (*session.doc.Objects, obj)
    if part is not None:
        session._parts[part]["objects"].add(obj.Name)  # type: ignore[attr-defined]
    session.set_result_object(obj, part=part)
    return object()


def _fake_add_torus(
    session: _FakeSession,
    *,
    radius1: float,
    radius2: float,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    axis: str = "z",
    part: str | None = None,
) -> object:
    obj = type("ManagedTorus", (), {})()
    obj.Name = "Torus"
    obj.TypeId = "Part::Torus"
    obj.Radius1 = radius1
    obj.Radius2 = radius2
    obj.Angle1 = -180.0
    obj.Angle2 = 180.0
    obj.Angle3 = 360.0
    obj.Placement = _FakePlacement(*position, q=_axis_quaternion(axis))
    diameter = 2 * (radius1 + radius2)
    thickness = 2 * radius2
    bboxes = {
        "x": (thickness, diameter, diameter),
        "y": (diameter, thickness, diameter),
        "z": (diameter, diameter, thickness),
    }
    obj.Shape = _FakeShape(
        volume=2 * math.pi**2 * radius1 * radius2**2,
        area=4 * math.pi**2 * radius1 * radius2,
        bbox=bboxes[axis],
        center=position,
        bbox_center=position,
    )
    session.doc.Objects = (*session.doc.Objects, obj)
    if part is not None:
        session._parts[part]["objects"].add(obj.Name)  # type: ignore[attr-defined]
    session.set_result_object(obj, part=part)
    return object()


def _fake_boolean(
    session: _FakeSession,
    *,
    base_name: str,
    tool_name: str,
    type_id: str,
) -> object:
    base = session.get_object(base_name)
    tool = session.get_object(tool_name)
    volume = {
        "Part::Cut": base.Shape.Volume - tool.Shape.Volume / 2,
        "Part::Fuse": base.Shape.Volume + tool.Shape.Volume / 2,
        "Part::Common": min(base.Shape.Volume, tool.Shape.Volume) / 2,
    }[type_id]
    obj = type("ManagedBoolean", (), {})()
    label = type_id.removeprefix("Part::")
    ordinal = sum(getattr(current, "TypeId", None) == type_id for current in session.doc.Objects)
    obj.Name = label if ordinal == 0 else f"{label}{ordinal:03d}"
    obj.TypeId = type_id
    obj.Base = base
    obj.Tool = tool
    obj.Placement = _FakePlacement(0.0)
    obj.Shape = _FakeShape(
        volume=volume,
        area=max(1.0, base.Shape.Area / 2),
        bbox=(10.0, 10.0, 10.0),
        center=(5.0, 5.0, 5.0),
    )
    obj.State = []
    session.doc.Objects = (*session.doc.Objects, obj)
    session.set_result_object(obj, part=session.owner_of(base_name))
    return object()


def _fake_boolean_cut(
    session: _FakeSession,
    *,
    base_name: str,
    tool_name: str,
) -> object:
    return _fake_boolean(
        session,
        base_name=base_name,
        tool_name=tool_name,
        type_id="Part::Cut",
    )


def _fake_boolean_fuse(
    session: _FakeSession,
    *,
    base_name: str,
    tool_name: str,
) -> object:
    return _fake_boolean(
        session,
        base_name=base_name,
        tool_name=tool_name,
        type_id="Part::Fuse",
    )


def _fake_boolean_common(
    session: _FakeSession,
    *,
    base_name: str,
    tool_name: str,
) -> object:
    return _fake_boolean(
        session,
        base_name=base_name,
        tool_name=tool_name,
        type_id="Part::Common",
    )


class _FakeRecomputeObserver:
    def __init__(self, document: _FakeDocument) -> None:
        self.document = document

    def __enter__(self) -> executor_module._RecomputeReceipt:
        receipt = executor_module._RecomputeReceipt(document=self.document, object_ids=set())
        self.document._recompute_observers.append(self)
        self.receipt = receipt
        return receipt

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.document._recompute_observers.remove(self)

    def slotRecomputedObject(self, obj: object) -> None:  # noqa: N802 - FreeCAD API
        self.receipt.object_ids.add(id(obj))


class _FakeMissingDescendantRecomputeObserver(_FakeRecomputeObserver):
    def slotRecomputedObject(self, obj: object) -> None:  # noqa: N802 - FreeCAD API
        if getattr(obj, "TypeId", None) not in {"Part::Cut", "Part::Fuse", "Part::Common"}:
            super().slotRecomputedObject(obj)


def _fake_modify_part(
    session: _FakeSession,
    *,
    name: str,
    parameter: str,
    value: float,
) -> object:
    obj = session.get_object(name)
    setattr(obj, parameter.capitalize(), value)
    position = (obj.Placement.Base.x, obj.Placement.Base.y, obj.Placement.Base.z)
    obj.Shape = _FakeShape(
        volume=obj.Length * obj.Width * obj.Height,
        area=2 * (obj.Length * obj.Width + obj.Length * obj.Height + obj.Width * obj.Height),
        bbox=(obj.Length, obj.Width, obj.Height),
        center=(
            position[0] + obj.Length / 2,
            position[1] + obj.Width / 2,
            position[2] + obj.Height / 2,
        ),
    )
    return object()


def _refresh_fake_boolean_descendants(session: _FakeSession, source: object) -> None:
    """Refresh the unique fake Boolean consumer chain after one operand edit."""

    current = source
    while True:
        matches = tuple(
            obj
            for obj in session.doc.Objects
            if getattr(obj, "TypeId", None) in {"Part::Cut", "Part::Fuse", "Part::Common"}
            and (getattr(obj, "Base", None) is current or getattr(obj, "Tool", None) is current)
        )
        if not matches:
            return
        assert len(matches) == 1
        current = matches[0]
        base = current.Base
        tool = current.Tool
        current.Shape = _FakeShape(
            volume={
                "Part::Cut": base.Shape.Volume - tool.Shape.Volume / 2,
                "Part::Fuse": base.Shape.Volume + tool.Shape.Volume / 2,
                "Part::Common": min(base.Shape.Volume, tool.Shape.Volume) / 2,
            }[current.TypeId],
            area=max(1.0, base.Shape.Area / 2),
            bbox=(
                base.Shape.BoundBox.XLength,
                base.Shape.BoundBox.YLength,
                base.Shape.BoundBox.ZLength,
            ),
            center=(
                base.Shape.CenterOfMass.x,
                base.Shape.CenterOfMass.y,
                base.Shape.CenterOfMass.z,
            ),
        )


def _fake_move_part(
    session: _FakeSession,
    *,
    name: str,
    position: tuple[float, float, float],
) -> object:
    obj = session.get_object(name)
    old = (obj.Placement.Base.x, obj.Placement.Base.y, obj.Placement.Base.z)
    obj.Placement = _FakePlacement(*position, q=obj.Placement.Rotation.Q)
    center = obj.Shape.CenterOfMass
    obj.Shape.BoundBox.translate(
        position[0] - old[0],
        position[1] - old[1],
        position[2] - old[2],
    )
    obj.Shape.CenterOfMass = _FakeVector(
        center.x + position[0] - old[0],
        center.y + position[1] - old[1],
        center.z + position[2] - old[2],
    )
    return object()


def _fake_rotate_part(
    session: _FakeSession,
    *,
    name: str,
    axis: str,
    angle: float,
) -> object:
    obj = session.get_object(name)
    delta = executor_module._axis_rotation(axis, angle)
    q = executor_module._quaternion_product(delta, obj.Placement.Rotation.Q)
    base = obj.Placement.Base
    bound_box = obj.Shape.BoundBox
    center = _FakeVector(
        (bound_box.XMin + bound_box.XMax) / 2.0,
        (bound_box.YMin + bound_box.YMax) / 2.0,
        (bound_box.ZMin + bound_box.ZMax) / 2.0,
    )
    radians = math.radians(angle)
    sine = math.sin(radians)
    cosine = math.cos(radians)
    offset = (base.x - center.x, base.y - center.y, base.z - center.z)
    if axis == "x":
        rotated = (
            offset[0],
            cosine * offset[1] - sine * offset[2],
            sine * offset[1] + cosine * offset[2],
        )
    elif axis == "y":
        rotated = (
            cosine * offset[0] + sine * offset[2],
            offset[1],
            -sine * offset[0] + cosine * offset[2],
        )
    else:
        rotated = (
            cosine * offset[0] - sine * offset[1],
            sine * offset[0] + cosine * offset[1],
            offset[2],
        )
    obj.Placement = _FakePlacement(
        center.x + rotated[0],
        center.y + rotated[1],
        center.z + rotated[2],
        q=q,
    )
    old_center = obj.Shape.CenterOfMass
    center_offset = (
        old_center.x - center.x,
        old_center.y - center.y,
        old_center.z - center.z,
    )
    if axis == "x":
        rotated_center = (
            center_offset[0],
            cosine * center_offset[1] - sine * center_offset[2],
            sine * center_offset[1] + cosine * center_offset[2],
        )
    elif axis == "y":
        rotated_center = (
            cosine * center_offset[0] + sine * center_offset[2],
            center_offset[1],
            -sine * center_offset[0] + cosine * center_offset[2],
        )
    else:
        rotated_center = (
            cosine * center_offset[0] - sine * center_offset[1],
            sine * center_offset[0] + cosine * center_offset[1],
            center_offset[2],
        )
    obj.Shape.CenterOfMass = _FakeVector(
        center.x + rotated_center[0],
        center.y + rotated_center[1],
        center.z + rotated_center[2],
    )
    return object()


def _store() -> LocalRevisionStore:
    return object.__new__(LocalRevisionStore)


def _reviewed_datum_program(operation: PartDatumOperation) -> ReviewedIntentProgramV1:
    graph = _datum_graph(operation)
    reviewed = next(
        item for item in PART_DATUM_MANIFEST.operations if item.operation_id == operation.value
    )
    namespace, version, term_id, digest = reviewed.semantic_term.semantic_identity
    return ReviewedIntentProgramV1(
        operation_id=f"{PART_DATUM_MANIFEST.family_id}.{operation.value}",
        semantic_operation=f"{namespace}/{version}/{term_id}@{digest}",
        intent_graph_sha256=graph.graph_sha256,
        intent_content_sha256=hashlib.sha256(graph.canonical_bytes).hexdigest(),
        intent_graph=graph,
    )


def _lease(*, project_id: str = PROJECT_ID, released: bool = False) -> ProjectWriteLease:
    lease = object.__new__(ProjectWriteLease)
    object.__setattr__(lease, "project_id", project_id)
    object.__setattr__(lease, "released", released)
    return lease


def _head() -> ProjectHead:
    return ProjectHead(
        project_id=PROJECT_ID,
        generation=1,
        revision_id=BASE_REVISION,
        manifest_sha256=DIGEST,
    )


def _active(session: object, root: Path) -> ActiveCandidate:
    return ActiveCandidate(
        project_id=PROJECT_ID,
        base_head=_head(),
        binding=SessionBinding(
            project_id=PROJECT_ID,
            revision_id=CANDIDATE_REVISION,
            session=session,
        ),
        model_path=root / "model.FCStd",
        step_path=root / "model.step",
    )


def _checkpointed(session: object, root: Path) -> CheckpointedCandidate:
    active = _active(session, root)
    return CheckpointedCandidate(
        project_id=active.project_id,
        base_head=active.base_head,
        binding=active.binding,
        model_path=active.model_path,
        step_path=active.step_path,
    )


def _prepare_empty_private_artifact(path: Path) -> None:
    if sys.platform == "win32":
        _file_compat.set_private_dacl(path.parent)
    path.touch(mode=0o600)
    path.chmod(0o600)
    if sys.platform == "win32":
        _file_compat.set_private_dacl(path)


def _artifact(path: Path, artifact_id: str, artifact_format: str) -> RevisionArtifactRef:
    import hashlib

    raw = path.read_bytes()
    return RevisionArtifactRef(
        id=artifact_id,
        name=path.name,
        format=artifact_format,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


def _sealed(session: object, model_path: Path, step_path: Path) -> SealedCandidate:
    model = _artifact(model_path, MODEL_ID, "fcstd")
    step = _artifact(step_path, STEP_ID, "step")
    revision = RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256=DIGEST,
        model=model,
        artifacts=(step,),
    )
    return SealedCandidate(
        project_id=PROJECT_ID,
        base_head=_head(),
        revision=revision,
        binding=SessionBinding(
            project_id=PROJECT_ID,
            revision_id=CANDIDATE_REVISION,
            session=session,
        ),
    )


def _write_artifacts(root: Path) -> tuple[Path, Path]:
    model = root / "model.FCStd"
    step = root / "model.step"
    with zipfile.ZipFile(model, "w") as archive:
        archive.writestr("Document.xml", "<Document />")
    step.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n#1=A;\nENDSEC;\nEND-ISO-10303-21;\n")
    return model, step


def _command(
    command_id: str,
    op: str,
    *,
    args: dict[str, object] | None = None,
    target: dict[str, object] | None = None,
    depends_on: tuple[str, ...] = (),
    preserve: tuple[str, ...] = (),
) -> ModelCommand:
    return ModelCommand(
        id=command_id,
        op=op,
        target={} if target is None else target,
        args={} if args is None else args,
        depends_on=depends_on,
        preserve=preserve,
        source=ValueSource.MODEL,
    )


@pytest.fixture(autouse=True)
def _fake_recompute_receipt(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    if request.node.get_closest_marker("slow") is None:
        monkeypatch.setattr(executor_module, "_DocumentRecomputeObserver", _FakeRecomputeObserver)


def _program() -> ModelProgram:
    return ModelProgram(
        task_id="task-executor",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length_mm": 10, "width_mm": 20, "height_mm": 30},
            ),
            _command(
                "modify",
                "modify_parameter",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"parameter": "length", "value_mm": 12},
                depends_on=("box",),
            ),
            _command("inspect", "inspect_model", depends_on=("modify",)),
        ),
        acceptance=AcceptanceSpec(id="acceptance-executor", criteria=()),
    )


def _six_operation_program() -> ModelProgram:
    return ModelProgram(
        task_id="task-executor-six",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={
                    "length_mm": 10,
                    "width_mm": 20,
                    "height_mm": 30,
                    "position_mm": (1, 2, 3),
                },
            ),
            _command(
                "cylinder",
                "create_cylinder",
                args={
                    "radius_mm": 4,
                    "height_mm": 18,
                    "position_mm": (50, 0, 0),
                    "axis": "x",
                },
            ),
            _command(
                "modify",
                "modify_parameter",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"parameter": "length", "value_mm": 12},
                depends_on=("box",),
            ),
            _command(
                "move",
                "move_part",
                target={"object": {"command_id": "cylinder", "slot": "object"}},
                args={"position_mm": (60, 5, 1)},
                depends_on=("cylinder",),
            ),
            _command(
                "rotate",
                "rotate_part",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"axis": "z", "angle_deg": 90},
                depends_on=("box", "modify"),
            ),
            _command(
                "inspect",
                "inspect_model",
                depends_on=("rotate", "move"),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-executor-six", criteria=()),
    )


def _part_native_primitives_program() -> ModelProgram:
    return ModelProgram(
        task_id="task-executor-part-primitives",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "cone",
                "create_cone",
                args={
                    "base_radius_mm": 6,
                    "top_radius_mm": 2,
                    "height_mm": 15,
                    "position_mm": (1, 2, 3),
                    "axis": "x",
                },
            ),
            _command(
                "sphere",
                "create_sphere",
                args={"radius_mm": 4, "position_mm": (30, 5, -2)},
                depends_on=("cone",),
            ),
            _command(
                "torus",
                "create_torus",
                args={
                    "major_radius_mm": 8,
                    "minor_radius_mm": 2,
                    "position_mm": (60, 0, 0),
                    "axis": "y",
                },
                depends_on=("sphere",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-part-primitives", criteria=()),
    )


def _part_boolean_program(operation: str) -> ModelProgram:
    return ModelProgram(
        task_id=f"task-executor-{operation}",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "base",
                "create_box",
                args={"length_mm": 20, "width_mm": 20, "height_mm": 20},
            ),
            _command(
                "tool",
                "create_box",
                args={
                    "length_mm": 10,
                    "width_mm": 10,
                    "height_mm": 10,
                    "position_mm": (5, 5, 5),
                },
                depends_on=("base",),
            ),
            _command(
                "boolean",
                operation,
                target={
                    "base": {"command_id": "base", "slot": "object"},
                    "tool": {"command_id": "tool", "slot": "object"},
                },
                depends_on=("base", "tool"),
            ),
        ),
        acceptance=AcceptanceSpec(id=f"acceptance-{operation}", criteria=()),
    )


def _part_boolean_edit_program(operation: str) -> ModelProgram:
    created = _part_boolean_program(operation).operations
    return ModelProgram(
        task_id=f"task-executor-{operation}-edit",
        base_revision=BASE_REVISION,
        operations=(
            *created,
            _command(
                "edit",
                "modify_parameter",
                target={"object": {"command_id": "base", "slot": "object"}},
                args={"parameter": "length", "value_mm": 22},
                depends_on=("base", "boolean"),
            ),
        ),
        acceptance=AcceptanceSpec(id=f"acceptance-{operation}-edit", criteria=()),
    )


def _component_boolean_program(*, include_empty_component: bool = False) -> ModelProgram:
    operations = (
        _command("component", "create_component", args={"name": "Bracket"}),
        _command(
            "base",
            "create_box",
            target={"component": {"command_id": "component", "slot": "component"}},
            args={"length_mm": 20, "width_mm": 10, "height_mm": 10},
            depends_on=("component",),
        ),
        _command(
            "tool",
            "create_box",
            target={"component": {"command_id": "component", "slot": "component"}},
            args={
                "length_mm": 10,
                "width_mm": 10,
                "height_mm": 10,
                "position_mm": (15, 0, 0),
            },
            depends_on=("base",),
        ),
        _command(
            "boolean",
            "boolean_fuse",
            target={
                "base": {"command_id": "base", "slot": "object"},
                "tool": {"command_id": "tool", "slot": "object"},
            },
            depends_on=("base", "tool"),
        ),
    )
    if include_empty_component:
        operations = (
            *operations,
            _command(
                "other",
                "create_component",
                args={"name": "Other"},
                depends_on=("boolean",),
            ),
        )
    return ModelProgram(
        task_id="task-executor-component-boolean",
        base_revision=BASE_REVISION,
        operations=operations,
        acceptance=AcceptanceSpec(id="acceptance-component-boolean", criteria=()),
    )


def _nested_component_boolean_program() -> ModelProgram:
    operations = _component_boolean_program().operations
    return ModelProgram(
        task_id="task-executor-nested-component-boolean",
        base_revision=BASE_REVISION,
        operations=(
            *operations,
            _command(
                "second_tool",
                "create_box",
                target={"component": {"command_id": "component", "slot": "component"}},
                args={
                    "length_mm": 5,
                    "width_mm": 10,
                    "height_mm": 10,
                    "position_mm": (22, 0, 0),
                },
                depends_on=("boolean",),
            ),
            _command(
                "second_boolean",
                "boolean_fuse",
                target={
                    "base": {"command_id": "boolean", "slot": "object"},
                    "tool": {"command_id": "second_tool", "slot": "object"},
                },
                depends_on=("boolean", "second_tool"),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-nested-component-boolean", criteria=()),
    )


def _component_program(
    *,
    component_b_position: tuple[float, float, float] = (20, 0, 0),
) -> ModelProgram:
    return ModelProgram(
        task_id="task-executor-components",
        base_revision=BASE_REVISION,
        operations=(
            _command("component_a", "create_component", args={"name": "A"}),
            _command(
                "box_a",
                "create_box",
                target={"component": {"command_id": "component_a", "slot": "component"}},
                args={"length_mm": 10, "width_mm": 10, "height_mm": 10},
                depends_on=("component_a",),
            ),
            _command(
                "component_b",
                "create_component",
                args={"name": "B"},
                depends_on=("box_a",),
            ),
            _command(
                "box_b",
                "create_box",
                target={"component": {"command_id": "component_b", "slot": "component"}},
                args={"length_mm": 5, "width_mm": 10, "height_mm": 10},
                depends_on=("component_b",),
            ),
            _command(
                "place_b",
                "place_component",
                target={"component": {"command_id": "component_b", "slot": "component"}},
                args={
                    "position_mm": component_b_position,
                    "rotation_axis": "z",
                    "angle_deg": 0,
                },
                depends_on=("box_b",),
            ),
            _command("inspect", "inspect_model", depends_on=("place_b",)),
        ),
        acceptance=AcceptanceSpec(id="acceptance-executor-components", criteria=()),
    )


def _component_bom_program(
    *,
    component_b_length: float = 10,
    component_b_part_number: str = "BRACKET-001",
) -> ModelProgram:
    metadata = {
        "part_number": "BRACKET-001",
        "description": "Mounting bracket",
        "material": "Aluminum 6061",
        "density_kg_m3": 2700,
    }
    return ModelProgram(
        task_id="task-executor-component-bom",
        base_revision=BASE_REVISION,
        operations=(
            _command("component_a", "create_component", args={"name": "A"}),
            _command(
                "box_a",
                "create_box",
                target={"component": {"command_id": "component_a", "slot": "component"}},
                args={"length_mm": 10, "width_mm": 10, "height_mm": 10},
                depends_on=("component_a",),
            ),
            _command(
                "bom_a",
                "set_component_bom",
                target={"component": {"command_id": "component_a", "slot": "component"}},
                args=metadata,
                depends_on=("box_a",),
            ),
            _command(
                "component_b",
                "create_component",
                args={"name": "B"},
                depends_on=("bom_a",),
            ),
            _command(
                "box_b",
                "create_box",
                target={"component": {"command_id": "component_b", "slot": "component"}},
                args={
                    "length_mm": component_b_length,
                    "width_mm": 10,
                    "height_mm": 10,
                },
                depends_on=("component_b",),
            ),
            _command(
                "place_b",
                "place_component",
                target={"component": {"command_id": "component_b", "slot": "component"}},
                args={
                    "position_mm": (20, 0, 0),
                    "rotation_axis": "z",
                    "angle_deg": 0,
                },
                depends_on=("box_b",),
            ),
            _command(
                "bom_b",
                "set_component_bom",
                target={"component": {"command_id": "component_b", "slot": "component"}},
                args={**metadata, "part_number": component_b_part_number},
                depends_on=("place_b",),
            ),
            _command("inspect", "inspect_model", depends_on=("bom_b",)),
        ),
        acceptance=AcceptanceSpec(id="acceptance-executor-component-bom", criteria=()),
    )


def _install_store_paths(
    monkeypatch: pytest.MonkeyPatch,
    sealed: SealedCandidate,
    model_path: Path,
    step_path: Path,
) -> list[str]:
    calls: list[str] = []
    assert sealed.revision.model is not None
    base_revision = RevisionRef(
        id=BASE_REVISION,
        project_id=PROJECT_ID,
        base_revision=None,
        manifest_sha256=sealed.base_head.manifest_sha256,
        model=sealed.revision.model,
        artifacts=(),
    )

    live = sealed.binding.session

    def session_factory() -> _FakeSession:
        probe = _FakeSession(getattr(live, "shape", None))
        probe.doc.Objects = tuple(getattr(getattr(live, "doc", None), "Objects", ()))
        return probe

    def load_revision(self: LocalRevisionStore, project_id: str, revision_id: str) -> RevisionRef:
        del self
        assert project_id == PROJECT_ID
        if revision_id == CANDIDATE_REVISION:
            calls.append("load")
            return sealed.revision
        assert revision_id == BASE_REVISION
        calls.append("base_load")
        return base_revision

    def revision_model_path(self: LocalRevisionStore, project_id: str, revision_id: str) -> Path:
        del self
        assert project_id == PROJECT_ID
        if revision_id == CANDIDATE_REVISION:
            calls.append("model_path")
        else:
            assert revision_id == BASE_REVISION
            calls.append("base_model_path")
        return model_path

    def revision_artifact_path(
        self: LocalRevisionStore,
        project_id: str,
        revision_id: str,
        artifact_id: str,
    ) -> Path:
        del self
        calls.append("step_path")
        assert (project_id, revision_id, artifact_id) == (
            PROJECT_ID,
            CANDIDATE_REVISION,
            STEP_ID,
        )
        return step_path

    monkeypatch.setattr(LocalRevisionStore, "load_revision", load_revision)
    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", revision_model_path)
    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", revision_artifact_path)
    monkeypatch.setattr(executor_module, "_Session", session_factory)
    return calls


def test_public_contract_and_fixed_redacted_errors() -> None:
    assert executor_module.__all__ == [
        "ExecutorErrorCode",
        "ExecutorError",
        "CandidateEvidence",
        "InProcessCadExecutor",
    ]
    assert {item.value for item in ExecutorErrorCode} == {
        "invalid_input",
        "invalid_candidate",
        "invalid_lease",
        "cad_failure",
        "artifact_failure",
        "integrity_failure",
        "internal_failure",
    }
    for code in ExecutorErrorCode:
        error = ExecutorError(code)
        assert error.to_mapping() == {
            "schema_version": SCHEMA_VERSION,
            "code": code.value,
            "message": error.message,
        }
        assert "secret" not in str(error)
        json.dumps(error.to_mapping())
    with pytest.raises(TypeError):
        ExecutorError("secret")  # type: ignore[arg-type]


def test_internal_cleanup_failure_supersedes_recoverable_operation_failure() -> None:
    operation = ExecutorError(ExecutorErrorCode.CAD_FAILURE)
    cleanup = ExecutorError(ExecutorErrorCode.INTERNAL_FAILURE)

    assert executor_module._prefer_cleanup_failure(operation, cleanup) is cleanup
    assert executor_module._prefer_cleanup_failure(None, operation) is operation


def test_constructor_requires_exact_revision_store() -> None:
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=object())  # type: ignore[arg-type]
    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT


def test_executor_is_the_candidate_coordinator_snapshot_port() -> None:
    executor = InProcessCadExecutor(store=_store())
    assert isinstance(executor, CadSnapshotPort)
    assert issubclass(InProcessCadExecutor, CadSnapshotPort)


def test_validate_program_reuses_authentic_validator() -> None:
    validated = InProcessCadExecutor(store=_store()).validate_program(_program())
    assert type(validated) is ValidatedProgram
    validated.require_authentic()
    assert tuple(command.handler_name for command in validated.commands) == (
        "create_box",
        "modify_parameter",
        "inspect_model",
    )


def test_create_load_checkpoint_and_close_use_public_session_surface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    made: list[_FakeSession] = []

    def factory() -> _FakeSession:
        session = _FakeSession()
        made.append(session)
        return session

    monkeypatch.setattr(executor_module, "_Session", factory)
    executor = InProcessCadExecutor(store=_store())
    empty = executor.create_empty(revision_id=CANDIDATE_REVISION)
    source = tmp_path / "source.FCStd"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("Document.xml", "<Document />")
    loaded = executor.load_fcstd(source)
    checkpoint = tmp_path / "candidate.FCStd"
    executor.checkpoint_fcstd(loaded, checkpoint)
    executor.close(loaded)

    assert empty is made[0]
    assert made[0].opened == ["VibeCADCandidate_11111111111111111111111111111111"]
    assert loaded is made[1]
    assert made[1].loaded == [source]
    assert made[1].doc.recompute_calls == 1
    assert made[1].persist_calls == 1
    assert len(made[1].doc.save_calls) == 1
    fresh_checkpoint = Path(made[1].doc.save_calls[0])
    assert fresh_checkpoint != checkpoint
    assert fresh_checkpoint.parent == checkpoint.parent
    assert fresh_checkpoint.suffix.lower() == ".fcstd"
    assert not fresh_checkpoint.exists()
    assert checkpoint.is_file()
    if os.name == "posix":
        assert checkpoint.stat().st_mode & 0o777 == 0o600
    assert made[1].close_calls == 1


def test_long_windows_load_keeps_the_short_bridge_until_executor_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.FCStd"
    short = tmp_path / "bridge.FCStd"
    for path in (source, short):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Document.xml", "<Document />")
    session = _FakeSession()
    manager = SimpleNamespace()
    owner = executor_module._WindowsCadBridgeOwner(
        manager=manager,
        directory_capability=object(),
        source_directory_capability=object(),
        source_identity=object(),
        staged_identity=object(),
    )
    events: list[str] = []

    monkeypatch.setattr(executor_module, "_Session", lambda: session)
    monkeypatch.setattr(executor_module, "_windows_cad_path_needs_bridge", lambda _path: True)
    monkeypatch.setattr(
        executor_module,
        "_stage_windows_cad_input",
        lambda path: (short, owner) if path == source else pytest.fail("unexpected source"),
    )
    monkeypatch.setattr(
        executor_module._WindowsCadBridgeOwner,
        "validate_after_load",
        lambda self: events.append("validate"),
    )

    def close_bridge(self) -> None:
        events.append("bridge-close")
        self.closed = True

    monkeypatch.setattr(executor_module._WindowsCadBridgeOwner, "close", close_bridge)
    executor = InProcessCadExecutor(store=_store())

    loaded = executor.load_fcstd(source)

    assert loaded is session
    assert session.loaded == [short]
    assert events == ["validate"]
    assert getattr(session, executor_module._WINDOWS_CAD_BRIDGE_ATTRIBUTE) is owner

    executor.close(session)

    assert session.close_calls == 1
    assert events == ["validate", "bridge-close"]
    assert not hasattr(session, executor_module._WINDOWS_CAD_BRIDGE_ATTRIBUTE)


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path capabilities")
@pytest.mark.windows_contract
def test_windows_cad_bridge_accepts_verbatim_source_and_revalidates_private_file_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_directory = tmp_path / "source"
    bridge_parent = tmp_path / "bridge-parent"
    _file_compat.ensure_private_directory(source_directory, exclusive=True)
    _file_compat.ensure_private_directory(bridge_parent, exclusive=True)
    source = source_directory / "model.FCStd"
    payload_buffer = io.BytesIO()
    with zipfile.ZipFile(payload_buffer, "w") as archive:
        archive.writestr("Document.xml", "<Document />")
    payload = payload_buffer.getvalue()
    descriptor, _capability = _file_compat.open_private_file(
        source,
        create=True,
        read_write=True,
        exclusive=True,
    )
    try:
        assert os.write(descriptor, payload) == len(payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    monkeypatch.setenv("FREECAD_USER_TEMP", str(bridge_parent))

    verbatim_source = Path(_file_compat.windows_extended_path(source))
    assert os.fspath(verbatim_source).startswith("\\\\?\\")

    staged, owner = executor_module._stage_windows_cad_input(verbatim_source)
    staged_directory = staged.parent

    assert staged != source
    assert staged.read_bytes() == payload
    assert executor_module._windows_path_units(staged) < (
        executor_module._WINDOWS_CAD_LEGACY_PATH_BUDGET
    )
    owner.validate_after_load()
    assert source.read_bytes() == payload
    _file_compat.delete_windows_file(
        source,
        parent=owner.source_directory_capability,
        expected=owner.source_identity.capability,
    )
    owner.close()

    assert not source.exists()
    assert not staged_directory.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path capabilities")
@pytest.mark.windows_contract
def test_windows_checkpoint_uses_short_bridge_for_verbatim_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge_parent = tmp_path / "bridge-parent"
    _file_compat.ensure_private_directory(bridge_parent, exclusive=True)
    parent = tmp_path / ("destination-" + "a" * 72)
    parent_capability = _file_compat.ensure_private_directory(parent, exclusive=True)
    while executor_module._windows_path_units(parent / "model.FCStd") < 230:
        child = parent / ("nested-" + "b" * 72)
        parent_capability = _file_compat.ensure_private_directory(
            child,
            expected_parent=parent_capability,
            exclusive=True,
        )
        parent = child
    destination = parent / "model.FCStd"
    descriptor, _destination_capability = _file_compat.open_private_file(
        destination,
        create=True,
        read_write=True,
        exclusive=True,
        expected_parent=parent_capability,
    )
    os.close(descriptor)
    verbatim_destination = Path(_file_compat.windows_extended_path(destination))
    monkeypatch.setenv("FREECAD_USER_TEMP", str(bridge_parent))
    session = _FakeSession()

    InProcessCadExecutor(store=_store()).checkpoint_fcstd(
        session,
        verbatim_destination,
    )

    assert executor_module._read_artifact(verbatim_destination, "fcstd").size_bytes > 0
    assert len(session.doc.save_calls) == 1
    assert executor_module._windows_path_units(Path(session.doc.save_calls[0])) < (
        executor_module._WINDOWS_CAD_LEGACY_PATH_BUDGET
    )
    assert tuple(bridge_parent.iterdir()) == ()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path capabilities")
@pytest.mark.windows_contract
def test_windows_step_export_uses_short_bridge_for_verbatim_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bridge_parent = tmp_path / "bridge-parent"
    _file_compat.ensure_private_directory(bridge_parent, exclusive=True)
    parent = tmp_path / ("destination-" + "a" * 72)
    parent_capability = _file_compat.ensure_private_directory(parent, exclusive=True)
    while executor_module._windows_path_units(parent / "model.step") < 230:
        child = parent / ("nested-" + "b" * 72)
        parent_capability = _file_compat.ensure_private_directory(
            child,
            expected_parent=parent_capability,
            exclusive=True,
        )
        parent = child
    step_path = parent / "model.step"
    descriptor, _step_capability = _file_compat.open_private_file(
        step_path,
        create=True,
        read_write=True,
        exclusive=True,
        expected_parent=parent_capability,
    )
    os.close(descriptor)
    model_path = parent / "model.FCStd"
    verbatim_step = Path(_file_compat.windows_extended_path(step_path))
    verbatim_model = Path(_file_compat.windows_extended_path(model_path))
    monkeypatch.setenv("FREECAD_USER_TEMP", str(bridge_parent))
    session = _FakeSession()

    executor_module._export_session_step(
        session=session,
        model_path=verbatim_model,
        step_path=verbatim_step,
    )

    assert executor_module._read_artifact(verbatim_step, "step").size_bytes > 0
    assert len(session.shape.export_calls) == 1
    assert executor_module._windows_path_units(Path(session.shape.export_calls[0])) < (
        executor_module._WINDOWS_CAD_LEGACY_PATH_BUDGET
    )
    assert tuple(bridge_parent.iterdir()) == ()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path capabilities")
@pytest.mark.windows_contract
def test_windows_failed_checkpoint_cleanup_requires_exact_private_file_id(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "private"
    parent_capability = _file_compat.ensure_private_directory(parent, exclusive=True)
    partial = parent / ".vibecad-checkpoint-partial.FCStd"
    descriptor, _partial_capability = _file_compat.open_private_file(
        partial,
        create=True,
        read_write=True,
        exclusive=True,
        expected_parent=parent_capability,
    )
    try:
        assert os.write(descriptor, b"partial") == len(b"partial")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    executor_module._remove_failed_artifact(
        Path(_file_compat.windows_extended_path(partial)),
    )

    assert not partial.exists()


def test_create_empty_bootstraps_a_trusted_document_outside_the_model_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptySession:
        def __init__(self) -> None:
            self.close_calls = 0
            self.opened: list[str] = []

        def open_document(self, name: str) -> object:
            self.opened.append(name)
            return object()

        def close_document(self) -> None:
            self.close_calls += 1

    session = EmptySession()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)

    created = InProcessCadExecutor(store=_store()).create_empty(
        revision_id=CANDIDATE_REVISION,
    )

    assert created is session
    assert session.opened == [
        "VibeCADCandidate_11111111111111111111111111111111",
    ]
    assert session.close_calls == 0


def test_create_empty_closes_failed_bootstrap_and_redacts_runtime_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenSession:
        def __init__(self) -> None:
            self.close_calls = 0

        def open_document(self, name: str) -> object:
            del name
            raise RuntimeError("secret-bootstrap-detail")

        def close_document(self) -> None:
            self.close_calls += 1

    session = BrokenSession()
    monkeypatch.setattr(executor_module, "_Session", lambda: session)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).create_empty(
            revision_id=CANDIDATE_REVISION,
        )

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert "secret" not in str(caught.value)
    assert session.close_calls == 1


def test_create_empty_rejects_invalid_revision_before_constructing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def forbidden() -> object:
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr(executor_module, "_Session", forbidden)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).create_empty(revision_id="untrusted")

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert calls == 0


def test_checkpoint_rejects_silent_save_noop_and_preserves_existing_candidate(
    tmp_path: Path,
) -> None:
    class SilentDocument(_FakeDocument):
        def saveCopy(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
            self.save_calls.append(path)

    session = _FakeSession()
    session.doc = SilentDocument()
    checkpoint = tmp_path / "model.FCStd"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("Document.xml", "<Baseline />")
    baseline = checkpoint.read_bytes()

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert checkpoint.read_bytes() == baseline
    assert len(session.doc.save_calls) == 1
    fresh_checkpoint = Path(session.doc.save_calls[0])
    assert fresh_checkpoint != checkpoint
    assert fresh_checkpoint.parent == checkpoint.parent
    assert not fresh_checkpoint.exists()


def test_checkpoint_rejects_malformed_fresh_copy_and_preserves_existing_candidate(
    tmp_path: Path,
) -> None:
    class MalformedDocument(_FakeDocument):
        def saveCopy(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
            self.save_calls.append(path)
            Path(path).write_bytes(b"not-an-fcstd")

    session = _FakeSession()
    session.doc = MalformedDocument()
    checkpoint = tmp_path / "model.FCStd"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("Document.xml", "<Baseline />")
    baseline = checkpoint.read_bytes()

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert checkpoint.read_bytes() == baseline
    assert len(session.doc.save_calls) == 1
    assert not Path(session.doc.save_calls[0]).exists()


def test_checkpoint_replace_failure_preserves_existing_candidate_and_cleans_temp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    checkpoint = tmp_path / "model.FCStd"
    with zipfile.ZipFile(checkpoint, "w") as archive:
        archive.writestr("Document.xml", "<Baseline />")
    baseline = checkpoint.read_bytes()

    def reject_replace(source: Path, destination: Path, **_kwargs: object) -> None:
        assert source != checkpoint
        assert destination == checkpoint
        raise OSError("replace failed")

    if sys.platform == "win32":
        monkeypatch.setattr(
            executor_module._file_compat,
            "replace_windows_file",
            reject_replace,
        )
    else:
        monkeypatch.setattr(executor_module.os, "replace", reject_replace)
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert checkpoint.read_bytes() == baseline
    assert len(session.doc.save_calls) == 1
    assert not Path(session.doc.save_calls[0]).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows path capabilities")
@pytest.mark.windows_contract
def test_windows_short_checkpoint_publishes_by_exact_file_id(tmp_path: Path) -> None:
    _file_compat.protect_windows_path(tmp_path, directory=True)
    checkpoint = Path(os.path.abspath(tmp_path / "model.FCStd"))
    descriptor, before = _file_compat.open_private_file(
        checkpoint,
        create=True,
        read_write=True,
        exclusive=True,
        expected_parent=_file_compat.capture_windows_path(tmp_path, directory=True),
    )
    os.close(descriptor)
    session = _FakeSession()

    InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    after = _file_compat.capture_windows_path(checkpoint, directory=False)
    assert (after.volume, after.file_id) != (before.volume, before.file_id)
    assert _file_compat.validate_windows_path(after, directory=False) == checkpoint
    assert executor_module._read_artifact(checkpoint, "fcstd").size_bytes > 0
    assert len(session.doc.save_calls) == 1
    assert not Path(session.doc.save_calls[0]).exists()


def test_checkpoint_name_collisions_fail_closed_before_freecad_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token = "0" * 32
    collision = tmp_path / f".vibecad-checkpoint-{token}.FCStd"
    collision.write_bytes(b"owned-collision")
    checkpoint = tmp_path / "model.FCStd"
    session = _FakeSession()
    monkeypatch.setattr(executor_module.secrets, "token_hex", lambda size: token)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.doc.save_calls == []
    assert collision.read_bytes() == b"owned-collision"
    assert not checkpoint.exists()


@pytest.mark.parametrize("method", ["load_fcstd", "checkpoint_fcstd", "close"])
def test_session_port_exceptions_are_redacted(
    method: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    made: list[_FakeSession] = []

    class Broken(_FakeSession):
        def __init__(self) -> None:
            super().__init__()
            made.append(self)

        def load_document(self, path: Path) -> object:
            del path
            raise RuntimeError("secret-source-path")

        def persist_state(self) -> None:
            raise RuntimeError("secret-document")

        def close_document(self) -> None:
            self.close_calls += 1
            raise RuntimeError("secret-close")

    monkeypatch.setattr(executor_module, "_Session", Broken)
    executor = InProcessCadExecutor(store=_store())
    with pytest.raises(ExecutorError) as caught:
        if method == "load_fcstd":
            source = tmp_path / "source.FCStd"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("Document.xml", "<Document />")
            executor.load_fcstd(source)
        elif method == "checkpoint_fcstd":
            executor.checkpoint_fcstd(Broken(), tmp_path / "model.FCStd")
        else:
            executor.close(Broken())
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE
    assert "secret" not in str(caught.value)
    if method == "load_fcstd":
        assert len(made) == 1
        assert made[0].close_calls == 1


def test_execute_program_binds_fixed_handlers_once_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []

    def add_box(session: _FakeSession, **kwargs: object) -> object:
        calls.append(("add_box", (session, kwargs)))
        return _fake_add_box(session, **kwargs)  # type: ignore[arg-type]

    def modify_part(session: _FakeSession, **kwargs: object) -> object:
        calls.append(("modify_part", (session, kwargs)))
        return _fake_modify_part(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(executor_module, "_add_box", add_box)
    monkeypatch.setattr(executor_module, "_modify_part", modify_part)
    executor = InProcessCadExecutor(store=_store())
    validated = executor.validate_program(_program())
    session = _FakeSession()

    outcomes = executor.execute_program(
        program=validated,
        candidate=_active(session, tmp_path),
    )

    assert tuple(name for name, _ in calls) == (
        "add_box",
        "modify_part",
    )
    assert len(outcomes) == 3
    assert all(outcome.result.ok for outcome in outcomes)
    assert all(outcome.result.revision == CANDIDATE_REVISION for outcome in outcomes)
    assert all(
        payload[0] is session
        for _, payload in calls  # type: ignore[index]
    )
    assert calls[0][1][1] == {  # type: ignore[index]
        "length": 10,
        "width": 20,
        "height": 30,
    }
    assert calls[1][1][1] == {  # type: ignore[index]
        "name": "Box",
        "parameter": "length",
        "value": 12,
    }


def test_execute_program_applies_one_reviewed_box_and_adopts_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed = reviewed_box_program()

    def execute(session: _FakeSession, value: object) -> ReviewedNativeExecutionResult:
        assert value == reviewed
        obj = session.identity_object
        obj.Length = 10.0
        obj.Width = 8.0
        obj.Height = 6.0
        obj.Placement = _FakePlacement(0.0)
        obj.Shape = _FakeShape(
            volume=480.0,
            area=376.0,
            bbox=(10.0, 8.0, 6.0),
            center=(5.0, 4.0, 3.0),
        )
        session.doc.Objects = (*session.doc.Objects, obj)
        return ReviewedNativeExecutionResult(
            route=REVIEWED_PART_BOX_ROUTE,
            object=obj,
            plan_sha256="a" * 64,
            plan_content_sha256="b" * 64,
            native_receipt=PartCoreConformanceReceipt(
                plan_sha256="a" * 64,
                operation=PartCoreOperation.BOX,
                object_name=obj.Name,
                source_shape_sha256s=(),
                result_shape_sha256="c" * 64,
            ),
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    session = _FakeSession()
    program = ModelProgram(
        task_id="task-reviewed-box",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "reviewed_box",
                "apply_reviewed_intent",
                args={"intent": reviewed.to_mapping()},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reviewed-box", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert len(outcomes) == 1
    assert outcomes[0].result.ok is True
    result = outcomes[0].result.value
    identity = session.read_object_identity(session.identity_object)
    assert result["kind"] == "reviewed_intent_applied"
    assert result["reviewed_operation_id"] == reviewed.operation_id
    assert result["object_id"] == identity.object_id
    assert result["feature_id"] == identity.feature_id
    assert result["plan_sha256"] == "a" * 64
    assert session.result_object is session.identity_object


class _ManagedDatumFeature:
    def __init__(self, document: _FakeDocument, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.Placement = _FakePlacement(10.0, 20.0, 30.0)
        self.State = ("Up-to-date",)
        self.OriginFeatures: tuple[object, ...] = ()

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True


@pytest.mark.parametrize(
    ("operation", "owned_type_ids"),
    (
        (PartDatumOperation.DATUM_POINT, ("Part::DatumPoint",)),
        (
            PartDatumOperation.LOCAL_COORDINATE_SYSTEM,
            (
                "Part::LocalCoordinateSystem",
                "App::Line",
                "App::Line",
                "App::Line",
                "App::Plane",
                "App::Plane",
                "App::Plane",
                "App::Point",
            ),
        ),
    ),
)
def test_managed_reviewed_datum_adopts_closure_checkpoints_reopens_and_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: PartDatumOperation,
    owned_type_ids: tuple[str, ...],
) -> None:
    reviewed = _reviewed_datum_program(operation)
    route = next(
        item for item in REVIEWED_PART_DATUM_ROUTES if item.operation_id == reviewed.operation_id
    )

    def execute(session: _FakeSession, value: object) -> ReviewedNativeExecutionResult:
        assert value == reviewed
        primary_name = f"Managed_{operation.value}_{reviewed.intent_graph_sha256[:16]}"
        if any(getattr(item, "Name", None) == primary_name for item in session.doc.Objects):
            raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
        owned = tuple(
            _ManagedDatumFeature(
                session.doc,
                primary_name if index == 0 else f"{primary_name}_Helper{index}",
                type_id,
            )
            for index, type_id in enumerate(owned_type_ids)
        )
        owned[0].OriginFeatures = owned[1:]
        session.doc.Objects = (*session.doc.Objects, *owned)
        receipt = PartDatumConformanceReceipt(
            plan_sha256="d" * 64,
            operation=operation,
            object_name=primary_name,
            native_type_id=PART_DATUM_NATIVE_TYPE_IDS[operation],
            owned_object_names=tuple(item.Name for item in owned),
        )
        return ReviewedNativeExecutionResult(
            route=route,
            object=owned[0],
            plan_sha256="d" * 64,
            plan_content_sha256="e" * 64,
            native_receipt=receipt,
            owned_objects=owned,
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    program = validate_model_program(
        ModelProgram(
            task_id=f"task-managed-{operation.value}",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    f"reviewed_{operation.value}",
                    "apply_reviewed_intent",
                    args={"intent": reviewed.to_mapping()},
                ),
            ),
            acceptance=AcceptanceSpec(id="accept-managed-datum", criteria=()),
        )
    )
    executor = InProcessCadExecutor(store=_store())
    session = _FakeSession()

    outcomes = executor.execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert len(outcomes) == 1 and outcomes[0].result.ok is True
    public_result = outcomes[0].result.value
    identities = tuple(identity for _, identity in session.attached_identities)
    assert public_result["object_id"] == identities[0].object_id
    assert "owned_object_ids" not in public_result
    assert len(identities) == len(owned_type_ids)
    assert tuple(item.object_type for item in identities) == owned_type_ids
    assert all(item.semantic_role is SemanticRole.SUPPORT for item in identities)
    assert all(item.provenance.source is ProvenanceSource.MODEL for item in identities)
    assert session.result_object is None

    checkpoint = tmp_path / f"{operation.value}.FCStd"
    executor.checkpoint_fcstd(session, checkpoint)
    persisted_identities = tuple(
        EntityIdentity.from_mapping(identity.to_mapping()) for identity in identities
    )

    def reopen_factory() -> _FakeSession:
        reopened = _FakeSession()
        reopened.doc = copy.deepcopy(session.doc)
        reopened.attached_identities = list(
            zip(reopened.doc.Objects, persisted_identities, strict=True)
        )
        return reopened

    monkeypatch.setattr(executor_module, "_Session", reopen_factory)
    reopened = executor.load_fcstd(checkpoint)
    reopened_identities = tuple(identity for _, identity in reopened.list_object_identities())
    assert reopened_identities == persisted_identities
    before_duplicate = tuple(reopened.doc.Objects)

    duplicate = executor.execute_program(
        program=program,
        candidate=_active(reopened, tmp_path),
    )

    assert len(duplicate) == 1 and duplicate[0].result.ok is False
    assert tuple(reopened.doc.Objects) == before_duplicate
    assert tuple(identity for _, identity in reopened.list_object_identities()) == (
        persisted_identities
    )


@pytest.mark.parametrize("failure", ("missing_helper", "extra_helper", "tip_drift"))
def test_managed_reviewed_datum_validation_failure_restores_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    operation = (
        PartDatumOperation.DATUM_POINT
        if failure == "tip_drift"
        else PartDatumOperation.LOCAL_COORDINATE_SYSTEM
    )
    reviewed = _reviewed_datum_program(operation)

    class RollbackSession(_FakeSession):
        def list_object_identities(self) -> tuple[tuple[object, object], ...]:
            return tuple(self.attached_identities)

    session = RollbackSession()
    if failure == "tip_drift":
        body = type(
            "ExistingBody",
            (),
            {"TypeId": "PartDesign::Body", "Tip": object()},
        )()
        session.doc.Objects = (body,)

    def fail_after_mutation(session: _FakeSession, value: object) -> object:
        assert value == reviewed
        if failure == "tip_drift":
            session.doc.Objects[0].Tip = object()
        else:
            count = 7 if failure == "missing_helper" else 9
            created = tuple(
                _ManagedDatumFeature(
                    session.doc,
                    f"InvalidLCS_{index}",
                    "Part::LocalCoordinateSystem" if index == 0 else "App::Line",
                )
                for index in range(count)
            )
            session.doc.Objects = (*session.doc.Objects, *created)
        raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE)

    monkeypatch.setattr(
        executor_module,
        "_execute_reviewed_intent_native",
        fail_after_mutation,
    )
    program = validate_model_program(
        ModelProgram(
            task_id=f"task-managed-rollback-{failure}",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    f"reviewed_rollback_{failure}",
                    "apply_reviewed_intent",
                    args={"intent": reviewed.to_mapping()},
                ),
            ),
            acceptance=AcceptanceSpec(id="accept-managed-datum-rollback", criteria=()),
        )
    )
    before_objects = tuple(session.doc.Objects)
    before_tip = getattr(before_objects[0], "Tip", None) if before_objects else None

    outcome = InProcessCadExecutor(store=_store()).execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert len(outcome) == 1 and outcome[0].result.ok is False
    assert tuple(session.doc.Objects) == before_objects
    if before_objects:
        assert before_objects[0].Tip is before_tip
    assert session.attached_identities == []


class _ManagedPartDesignPrimitiveObject(_ManagedDatumFeature):
    def __init__(self, document: _FakeDocument, name: str, type_id: str) -> None:
        super().__init__(document, name, type_id)
        self.Group: tuple[object, ...] = ()
        self.OriginFeatures: tuple[object, ...] = ()
        self.BaseFeature: object | None = None


def _managed_partdesign_additive_result(
    session: _FakeSession,
    reviewed: ReviewedIntentProgramV1,
) -> ReviewedNativeExecutionResult:
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_PRIMITIVE_ROUTES
        if item.operation.operation_id == PartDesignPrimitiveOperation.ADDITIVE_BOX.value
    )
    primary_name = f"ManagedAdditiveBox_{reviewed.intent_graph_sha256[:16]}"
    if session.doc.getObject(primary_name) is not None:
        raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
    body = _ManagedPartDesignPrimitiveObject(
        session.doc,
        f"{primary_name}_Body",
        "PartDesign::Body",
    )
    origin = _ManagedPartDesignPrimitiveObject(
        session.doc,
        f"{primary_name}_Origin",
        "App::Origin",
    )
    helper_types = (
        "App::Line",
        "App::Line",
        "App::Line",
        "App::Plane",
        "App::Plane",
        "App::Plane",
        "App::Point",
    )
    helper_roles = (
        "X_Axis",
        "Y_Axis",
        "Z_Axis",
        "XY_Plane",
        "XZ_Plane",
        "YZ_Plane",
        "Origin",
    )
    helpers = tuple(
        _ManagedPartDesignPrimitiveObject(
            session.doc,
            f"{primary_name}_Helper{index}",
            type_id,
        )
        for index, type_id in enumerate(helper_types)
    )
    for helper, role in zip(helpers, helper_roles, strict=True):
        helper.Role = role
        helper.InList = (origin,)
    origin.OriginFeatures = helpers
    body.Origin = origin
    primary = _ManagedPartDesignPrimitiveObject(
        session.doc,
        primary_name,
        "PartDesign::AdditiveBox",
    )
    primary.Shape = _FakeShape(
        volume=480.0,
        area=376.0,
        bbox=(10.0, 8.0, 6.0),
        center=(5.0, 4.0, 3.0),
    )
    body.Group = (primary,)
    body.Tip = primary
    body_closure = (body, origin, *helpers)
    session.doc.Objects = (*session.doc.Objects, *body_closure, primary)
    plan_sha256 = "7" * 64
    receipt = PartDesignPrimitiveConformanceReceipt(
        plan_sha256=plan_sha256,
        operation=PartDesignPrimitiveOperation.ADDITIVE_BOX,
        object_name=primary.Name,
        before_volume_mm3=0.0,
        after_volume_mm3=480.0,
    )
    ownership = PartDesignPrimitiveOwnershipClosure(
        invariant=PARTDESIGN_PRIMITIVE_RESULT_INVARIANTS[PartDesignPrimitiveOperation.ADDITIVE_BOX],
        native_receipt=receipt,
        object=primary,
        body=body,
        base=None,
        body_closure=body_closure,
        created_body=True,
        base_shape_sha256=None,
        result_shape_sha256=hashlib.sha256(primary.Shape.exportBrepToString().encode()).hexdigest(),
    )
    return ReviewedNativeExecutionResult(
        route=route,
        object=primary,
        plan_sha256=plan_sha256,
        plan_content_sha256="8" * 64,
        native_receipt=ownership,
        owned_objects=(primary, *body_closure),
        _verified_execution_context=_ReviewedFamilyExecutionContext(
            session=session,
            document=session.doc,
            source_results=(),
        ),
    )


def test_managed_first_additive_adopts_ten_objects_and_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed = reviewed_partdesign_primitive_program(PartDesignPrimitiveOperation.ADDITIVE_BOX)

    def execute(session: _FakeSession, value: object) -> ReviewedNativeExecutionResult:
        assert value == reviewed
        return _managed_partdesign_additive_result(session, reviewed)

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    program = validate_model_program(
        ModelProgram(
            task_id="task-managed-partdesign-first-additive",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    "reviewed_additive_box",
                    "apply_reviewed_intent",
                    args={"intent": reviewed.to_mapping()},
                ),
            ),
            acceptance=AcceptanceSpec(id="accept-managed-partdesign-first-additive", criteria=()),
        )
    )
    executor = InProcessCadExecutor(store=_store())
    session = _FakeSession()

    outcomes = executor.execute_program(program=program, candidate=_active(session, tmp_path))

    assert len(outcomes) == 1 and outcomes[0].result.ok is True
    assert len(session.doc.Objects) == 10
    assert len(session.attached_identities) == 10
    assert session.result_object is session.doc.Objects[-1]
    identities = tuple(identity for _, identity in session.attached_identities)
    assert tuple(identity.semantic_role for identity in identities) == (
        SemanticRole.FEATURE,
        SemanticRole.PART,
        *(SemanticRole.SUPPORT,) * 8,
    )
    assert tuple(item.TypeId for item in session.doc.Objects) == (
        "PartDesign::Body",
        "App::Origin",
        "App::Line",
        "App::Line",
        "App::Line",
        "App::Plane",
        "App::Plane",
        "App::Plane",
        "App::Point",
        "PartDesign::AdditiveBox",
    )
    before_duplicate = tuple(session.doc.Objects)
    identities_before_duplicate = tuple(session.attached_identities)

    duplicate = executor.execute_program(program=program, candidate=_active(session, tmp_path))

    assert len(duplicate) == 1 and duplicate[0].result.ok is False
    assert tuple(session.doc.Objects) == before_duplicate
    assert tuple(session.attached_identities) == identities_before_duplicate


def test_managed_first_additive_rolls_back_ten_objects_after_late_adoption_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed = reviewed_partdesign_primitive_program(PartDesignPrimitiveOperation.ADDITIVE_BOX)

    class FailingAttachSession(_FakeSession):
        def attach_object_identity(self, obj: object, identity: object) -> object:
            if len(self.attached_identities) == 4:
                raise RuntimeError("bounded late adoption failure")
            return super().attach_object_identity(obj, identity)

    def execute(session: _FakeSession, value: object) -> ReviewedNativeExecutionResult:
        assert value == reviewed
        return _managed_partdesign_additive_result(session, reviewed)

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    program = validate_model_program(
        ModelProgram(
            task_id="task-managed-partdesign-first-additive-rollback",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    "reviewed_additive_box_rollback",
                    "apply_reviewed_intent",
                    args={"intent": reviewed.to_mapping()},
                ),
            ),
            acceptance=AcceptanceSpec(
                id="accept-managed-partdesign-first-additive-rollback",
                criteria=(),
            ),
        )
    )
    session = FailingAttachSession()

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert len(outcomes) == 1 and outcomes[0].result.ok is False
    assert session.doc.Objects == ()
    assert session.attached_identities == []
    assert session.result_object is None


class _ManagedMultiTransformFeature:
    def __init__(
        self,
        document: _FakeDocument,
        name: str,
        type_id: str,
        *,
        volume: float,
    ) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.Placement = _FakePlacement(0.0)
        self.State = ("Up-to-date",)
        self.Shape = _FakeShape(volume=volume)
        self.BaseFeature = None

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True


def test_managed_multitransform_adopts_full_closure_and_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed_base = reviewed_box_program()
    reviewed_multi = reviewed_dressup_program(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    _, _, _, plan, _ = lower_reviewed_dressup(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    assert type(plan.parameters) is MultiTransformParameters
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_DRESSUP_ROUTES
        if item.operation.operation_id == "multi_transform"
    )
    plan_document = route.manifest.plan_document(plan.canonical_bytes, plan.plan_sha256)

    def execute(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        if value == reviewed_base:
            assert source_results == ()
            base = _ManagedMultiTransformFeature(
                session.doc,
                "ReviewedBase",
                REVIEWED_PART_BOX_ROUTE.operation.native_type_id,
                volume=10.0,
            )
            base.Length = 5.0
            base.Width = 2.0
            base.Height = 1.0
            session.doc.Objects = (*session.doc.Objects, base)
            return ReviewedNativeExecutionResult(
                route=REVIEWED_PART_BOX_ROUTE,
                object=base,
                plan_sha256="a" * 64,
                plan_content_sha256="b" * 64,
                native_receipt=PartCoreConformanceReceipt(
                    plan_sha256="a" * 64,
                    operation=PartCoreOperation.BOX,
                    object_name=base.Name,
                    source_shape_sha256s=(),
                    result_shape_sha256=hashlib.sha256(
                        base.Shape.exportBrepToString().encode()
                    ).hexdigest(),
                ),
            )
        assert value == reviewed_multi
        assert len(source_results) == 1 and source_results[0].route is REVIEWED_PART_BOX_ROUTE
        primary_name = f"ManagedMulti_{reviewed_multi.intent_graph_sha256[:16]}"
        if session.doc.getObject(primary_name) is not None:
            raise ReviewedIntentExecutionError(ReviewedIntentExecutionErrorCode.EXECUTION_FAILED)
        type_ids = (
            route.operation.native_type_id,
            *(
                dressup_rules._NATIVE_STEP_SPECS[step.kind].type_id  # noqa: SLF001
                for step in plan.parameters.steps
            ),
        )
        owned = tuple(
            _ManagedMultiTransformFeature(
                session.doc,
                primary_name if index == 0 else f"{primary_name}_Child{index}",
                type_id,
                volume=15.0 if index == 0 else 1.0,
            )
            for index, type_id in enumerate(type_ids)
        )
        owned[0].BaseFeature = source_results[0].object
        session.doc.Objects = (*session.doc.Objects, *owned)
        native_receipt = PartDesignDressupTransformConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=PartDesignDressupTransformOperation.MULTI_TRANSFORM,
            object_names=tuple(item.Name for item in owned),
            before_volume_mm3=10.0,
            after_volume_mm3=15.0,
        )
        receipt = PartDesignDressupOwnershipClosure(
            native_receipt=native_receipt,
            body_id=plan.body_id,
            node_id=plan.node_id,
            result_id=plan.result_id,
            plan_content_sha256=plan_document.content_sha256,
            result_shape_sha256=hashlib.sha256(
                owned[0].Shape.exportBrepToString().encode()
            ).hexdigest(),
            native_type_id=route.operation.native_type_id,
        )
        native = _ReviewedFamilyNativeExecution(
            object=owned[0],
            receipt=receipt,
            owned_objects=owned,
        )
        resolution = route.family.resolve_dynamic_product_result(
            plan,
            plan_document,
            route.operation,
            native,
        )
        assert resolution is not None
        return ReviewedNativeExecutionResult(
            route=route,
            object=owned[0],
            plan_sha256=plan_document.document_digest,
            plan_content_sha256=plan_document.content_sha256,
            native_receipt=receipt,
            owned_objects=owned,
            _verified_execution_context=_ReviewedFamilyExecutionContext(
                session=session,
                document=session.doc,
                source_results=source_results,
            ),
            _verified_dynamic_resolution=resolution,
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    source_ref = ({"command_id": "source", "slot": "object"},)
    program = validate_model_program(
        ModelProgram(
            task_id="task-managed-multi-transform",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    "source",
                    "apply_reviewed_intent",
                    args={"intent": reviewed_base.to_mapping()},
                ),
                _command(
                    "multi",
                    "apply_reviewed_intent",
                    args={"intent": reviewed_multi.to_mapping(), "sources": source_ref},
                    depends_on=("source",),
                ),
                _command(
                    "multi_duplicate",
                    "apply_reviewed_intent",
                    args={"intent": reviewed_multi.to_mapping(), "sources": source_ref},
                    depends_on=("source",),
                ),
            ),
            acceptance=AcceptanceSpec(id="accept-managed-multi-transform", criteria=()),
        )
    )
    session = _FakeSession()

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert tuple(item.result.ok for item in outcomes) == (True, True, False)
    closure_identities = tuple(identity for _, identity in session.attached_identities[1:])
    assert tuple(item.semantic_role for item in closure_identities) == (
        SemanticRole.FEATURE,
        SemanticRole.SUPPORT,
        SemanticRole.SUPPORT,
    )
    assert tuple(item.object_type for item in closure_identities) == (
        "PartDesign::MultiTransform",
        "PartDesign::Scaled",
        "PartDesign::Mirrored",
    )
    assert all(item.provenance.operation_id == "multi" for item in closure_identities)
    assert session.result_object is session.doc.Objects[1]
    assert len(session.doc.Objects) == 4


def test_managed_multitransform_failure_rolls_back_full_closure_and_body_tip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed = reviewed_dressup_program(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    _, _, _, plan, _ = lower_reviewed_dressup(PartDesignDressupTransformOperation.MULTI_TRANSFORM)
    route = next(
        item
        for item in REVIEWED_PARTDESIGN_DRESSUP_ROUTES
        if item.operation.operation_id == "multi_transform"
    )
    plan_document = route.manifest.plan_document(plan.canonical_bytes, plan.plan_sha256)
    session = _FakeSession()
    old_tip = object()
    body = _ManagedDatumFeature(session.doc, "Body", "PartDesign::Body")
    body.Tip = old_tip
    session.attach_object_identity(
        body,
        EntityIdentity(
            object_id="object_" + "1" * 32,
            feature_id="feature_" + "2" * 32,
            object_type="PartDesign::Body",
            semantic_role=SemanticRole.SUPPORT,
            provenance=Provenance(
                source=ProvenanceSource.MODEL,
                operation_id="existing_body",
            ),
        ),
    )

    def fail_after_mutation(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...],
    ) -> ReviewedNativeExecutionResult:
        assert value == reviewed and len(source_results) == 1
        body.Tip = object()
        created = tuple(
            _ManagedMultiTransformFeature(
                session.doc,
                f"InvalidMulti{index}",
                type_id,
                volume=15.0 if index == 0 else 1.0,
            )
            for index, type_id in enumerate(
                (
                    "PartDesign::MultiTransform",
                    "PartDesign::Scaled",
                    "PartDesign::Mirrored",
                    "PartDesign::Scaled",
                )
            )
        )
        session.doc.Objects = (*session.doc.Objects, *created)
        native_receipt = PartDesignDressupTransformConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=PartDesignDressupTransformOperation.MULTI_TRANSFORM,
            object_names=tuple(item.Name for item in created),
            before_volume_mm3=10.0,
            after_volume_mm3=15.0,
        )
        receipt = PartDesignDressupOwnershipClosure(
            native_receipt=native_receipt,
            body_id=plan.body_id,
            node_id=plan.node_id,
            result_id=plan.result_id,
            plan_content_sha256=plan_document.content_sha256,
            result_shape_sha256=hashlib.sha256(
                created[0].Shape.exportBrepToString().encode()
            ).hexdigest(),
            native_type_id=route.operation.native_type_id,
        )
        native = _ReviewedFamilyNativeExecution(
            object=created[0],
            receipt=receipt,
            owned_objects=created,
        )
        route.family.resolve_dynamic_product_result(
            plan,
            plan_document,
            route.operation,
            native,
        )
        raise AssertionError("extra child must fail in the dynamic resolver")

    base_result: ReviewedNativeExecutionResult | None = None

    def execute(
        current: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        nonlocal base_result
        if value != reviewed:
            base = _ManagedMultiTransformFeature(
                current.doc,
                "RollbackBase",
                "Part::Box",
                volume=10.0,
            )
            base.Length = 5.0
            base.Width = 2.0
            base.Height = 1.0
            current.doc.Objects = (*current.doc.Objects, base)
            base_result = ReviewedNativeExecutionResult(
                route=REVIEWED_PART_BOX_ROUTE,
                object=base,
                plan_sha256="1" * 64,
                plan_content_sha256="2" * 64,
                native_receipt=PartCoreConformanceReceipt(
                    plan_sha256="1" * 64,
                    operation=PartCoreOperation.BOX,
                    object_name=base.Name,
                    source_shape_sha256s=(),
                    result_shape_sha256="3" * 64,
                ),
            )
            return base_result
        return fail_after_mutation(current, value, source_results=source_results)

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute)
    base = reviewed_box_program()
    program = validate_model_program(
        ModelProgram(
            task_id="task-managed-multi-rollback",
            base_revision=BASE_REVISION,
            operations=(
                _command(
                    "source",
                    "apply_reviewed_intent",
                    args={"intent": base.to_mapping()},
                ),
                _command(
                    "multi",
                    "apply_reviewed_intent",
                    args={
                        "intent": reviewed.to_mapping(),
                        "sources": ({"command_id": "source", "slot": "object"},),
                    },
                    depends_on=("source",),
                ),
            ),
            acceptance=AcceptanceSpec(id="accept-managed-multi-rollback", criteria=()),
        )
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=program,
        candidate=_active(session, tmp_path),
    )

    assert tuple(item.result.ok for item in outcomes) == (True, False)
    assert body.Tip is old_tip
    assert tuple(item.Name for item in session.doc.Objects) == ("Body", "RollbackBase")
    assert len(session.attached_identities) == 2
    assert base_result is not None


@pytest.mark.parametrize("source_interface", ("legacy_pair", "ordered_collection"))
def test_execute_program_resolves_two_prior_reviewed_outputs_for_csg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_interface: str,
) -> None:
    reviewed_box = reviewed_box_program()
    reviewed_cut = reviewed_csg_program(PartCoreOperation.CUT)
    primitive_results: list[ReviewedNativeExecutionResult] = []
    dependency_sources: list[tuple[ReviewedNativeExecutionResult, ...]] = []

    def execute_primitive(
        session: _FakeSession,
        value: object,
    ) -> ReviewedNativeExecutionResult:
        assert value == reviewed_box
        index = len(primitive_results)
        obj = type("ManagedReviewedBox", (), {})()
        obj.Name = f"ReviewedBox{index}"
        obj.TypeId = "Part::Box"
        obj.Length = 10.0
        obj.Width = 8.0
        obj.Height = 6.0
        obj.Placement = _FakePlacement(float(index) * 2.0)
        obj.Shape = _FakeShape(
            volume=480.0,
            area=376.0,
            bbox=(10.0, 8.0, 6.0),
            center=(5.0 + float(index) * 2.0, 4.0, 3.0),
        )
        obj.State = []
        session.doc.Objects = (*session.doc.Objects, obj)
        plan_sha256 = f"{index + 1:x}" * 64
        result = ReviewedNativeExecutionResult(
            route=REVIEWED_PART_BOX_ROUTE,
            object=obj,
            plan_sha256=plan_sha256,
            plan_content_sha256=f"{index + 3:x}" * 64,
            native_receipt=PartCoreConformanceReceipt(
                plan_sha256=plan_sha256,
                operation=PartCoreOperation.BOX,
                object_name=obj.Name,
                source_shape_sha256s=(),
                result_shape_sha256=hashlib.sha256(
                    obj.Shape.exportBrepToString().encode()
                ).hexdigest(),
            ),
        )
        primitive_results.append(result)
        return result

    def execute_csg(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...],
    ) -> ReviewedNativeExecutionResult:
        assert value == reviewed_cut
        assert source_results == tuple(primitive_results)
        dependency_sources.append(source_results)
        obj = type("ManagedReviewedCut", (), {})()
        obj.Name = "ReviewedCut"
        obj.TypeId = "Part::Cut"
        obj.Base = source_results[0].object
        obj.Tool = source_results[1].object
        obj.Refine = True
        obj.Placement = _FakePlacement(0.0)
        obj.Shape = _FakeShape(
            volume=240.0,
            area=300.0,
            bbox=(8.0, 8.0, 6.0),
            center=(4.0, 4.0, 3.0),
        )
        obj.State = []
        session.doc.Objects = (*session.doc.Objects, obj)
        return ReviewedNativeExecutionResult(
            route=REVIEWED_PART_CSG_ROUTES[0],
            object=obj,
            plan_sha256="5" * 64,
            plan_content_sha256="6" * 64,
            native_receipt=PartCoreConformanceReceipt(
                plan_sha256="5" * 64,
                operation=PartCoreOperation.CUT,
                object_name=obj.Name,
                source_shape_sha256s=tuple(
                    item.native_receipt.result_shape_sha256 for item in source_results
                ),
                result_shape_sha256=hashlib.sha256(
                    obj.Shape.exportBrepToString().encode()
                ).hexdigest(),
            ),
        )

    def execute_reviewed(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        if value == reviewed_box:
            assert source_results == ()
            return execute_primitive(session, value)
        return execute_csg(session, value, source_results=source_results)

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_reviewed)
    session = _FakeSession()
    dependent_args = {"intent": reviewed_cut.to_mapping()}
    if source_interface == "legacy_pair":
        dependent_args.update(
            {
                "source_a": {"command_id": "source_a", "slot": "object"},
                "source_b": {"command_id": "source_b", "slot": "object"},
            }
        )
    else:
        dependent_args["sources"] = (
            {"command_id": "source_a", "slot": "object"},
            {"command_id": "source_b", "slot": "object"},
        )
    program = ModelProgram(
        task_id="task-reviewed-csg",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "source_a",
                "apply_reviewed_intent",
                args={"intent": reviewed_box.to_mapping()},
            ),
            _command(
                "source_b",
                "apply_reviewed_intent",
                args={"intent": reviewed_box.to_mapping()},
            ),
            _command(
                "csg_cut",
                "apply_reviewed_intent",
                args=dependent_args,
                depends_on=("source_a", "source_b"),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reviewed-csg", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert len(outcomes) == 3
    assert all(item.result.ok for item in outcomes)
    assert len(dependency_sources) == 1
    assert outcomes[-1].result.value["reviewed_operation_id"] == "freecad_part_core.cut"
    source_identities = tuple(
        session.read_object_identity(item.object) for item in dependency_sources[0]
    )
    assert tuple(item.provenance.operation_id for item in source_identities) == (
        "source_a",
        "source_b",
    )
    result_identity = session.read_object_identity(session.result_object)
    assert result_identity.semantic_role.value == "feature"
    assert result_identity.provenance.operation_id == "csg_cut"


@pytest.mark.parametrize("source_interface", ("legacy_pair", "ordered_collection"))
def test_reviewed_csg_rejects_non_reviewed_result_slots_before_dependency_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_interface: str,
) -> None:
    reviewed_cut = reviewed_csg_program(PartCoreOperation.CUT)
    dependency_called = False

    def add_box(session: _FakeSession, **kwargs: object) -> object:
        return _fake_add_box(session, **kwargs)  # type: ignore[arg-type]

    def execute_csg(*_args: object, **_kwargs: object) -> ReviewedNativeExecutionResult:
        nonlocal dependency_called
        dependency_called = True
        raise AssertionError("dependency leaf must stay inert")

    monkeypatch.setattr(executor_module, "_add_box", add_box)
    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_csg)
    session = _FakeSession()
    dependent_args = {"intent": reviewed_cut.to_mapping()}
    if source_interface == "legacy_pair":
        dependent_args.update(
            {
                "source_a": {"command_id": "plain_a", "slot": "object"},
                "source_b": {"command_id": "plain_b", "slot": "object"},
            }
        )
    else:
        dependent_args["sources"] = (
            {"command_id": "plain_a", "slot": "object"},
            {"command_id": "plain_b", "slot": "object"},
        )
    program = ModelProgram(
        task_id="task-reviewed-csg-wrong-slots",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "plain_a",
                "create_box",
                args={"length_mm": 10, "width_mm": 8, "height_mm": 6},
            ),
            _command(
                "plain_b",
                "create_box",
                args={"length_mm": 8, "width_mm": 6, "height_mm": 4},
            ),
            _command(
                "csg_cut",
                "apply_reviewed_intent",
                args=dependent_args,
                depends_on=("plain_a", "plain_b"),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reviewed-csg-wrong-slots", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True, False]
    assert dependency_called is False
    assert len(session.doc.Objects) == 2


def test_reviewed_csg_rejects_incomplete_source_pair_without_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed_box = reviewed_box_program()
    reviewed_cut = reviewed_csg_program(PartCoreOperation.CUT)
    dependency_called = False

    def execute_primitive(
        session: _FakeSession,
        _value: object,
    ) -> ReviewedNativeExecutionResult:
        obj = session.identity_object
        obj.Shape = _FakeShape(volume=480.0, area=376.0, bbox=(10.0, 8.0, 6.0))
        session.doc.Objects = (*session.doc.Objects, obj)
        return ReviewedNativeExecutionResult(
            route=REVIEWED_PART_BOX_ROUTE,
            object=obj,
            plan_sha256="7" * 64,
            plan_content_sha256="8" * 64,
            native_receipt=PartCoreConformanceReceipt(
                plan_sha256="7" * 64,
                operation=PartCoreOperation.BOX,
                object_name=obj.Name,
                source_shape_sha256s=(),
                result_shape_sha256=hashlib.sha256(
                    obj.Shape.exportBrepToString().encode()
                ).hexdigest(),
            ),
        )

    def execute_csg(*_args: object, **_kwargs: object) -> ReviewedNativeExecutionResult:
        nonlocal dependency_called
        dependency_called = True
        raise AssertionError("dependency leaf must stay inert")

    def execute_reviewed(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        if value == reviewed_box:
            assert source_results == ()
            return execute_primitive(session, value)
        return execute_csg(session, value, source_results=source_results)

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_reviewed)
    session = _FakeSession()
    program = ModelProgram(
        task_id="task-reviewed-csg-incomplete",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "source_a",
                "apply_reviewed_intent",
                args={"intent": reviewed_box.to_mapping()},
            ),
            _command(
                "csg_cut",
                "apply_reviewed_intent",
                args={
                    "intent": reviewed_cut.to_mapping(),
                    "source_a": {"command_id": "source_a", "slot": "object"},
                },
                depends_on=("source_a",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reviewed-csg-incomplete", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, False]
    assert dependency_called is False
    assert len(session.doc.Objects) == 1


def test_execute_program_adopts_reviewed_face_as_managed_non_solid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed_box = reviewed_box_program()
    reviewed_face = reviewed_profile_surface_program(PartProfileSurfaceOperation.FACE)
    source_result: ReviewedNativeExecutionResult | None = None

    def execute_reviewed(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        nonlocal source_result
        if value == reviewed_box:
            assert source_results == ()
            obj = session.identity_object
            obj.Shape = _FakeShape(volume=480.0, area=376.0, bbox=(10.0, 8.0, 6.0))
            session.doc.Objects = (*session.doc.Objects, obj)
            source_result = ReviewedNativeExecutionResult(
                route=REVIEWED_PART_BOX_ROUTE,
                object=obj,
                plan_sha256="1" * 64,
                plan_content_sha256="2" * 64,
                native_receipt=PartCoreConformanceReceipt(
                    plan_sha256="1" * 64,
                    operation=PartCoreOperation.BOX,
                    object_name=obj.Name,
                    source_shape_sha256s=(),
                    result_shape_sha256=hashlib.sha256(
                        obj.Shape.exportBrepToString().encode()
                    ).hexdigest(),
                ),
            )
            return source_result
        assert value == reviewed_face
        assert source_results == (source_result,)
        obj = type("ManagedReviewedFace", (), {})()
        obj.Name = "ReviewedFace"
        obj.TypeId = REVIEWED_PART_PROFILE_SURFACE_ROUTES[-1].operation.native_type_id
        obj.Placement = _FakePlacement(0.0)
        obj.Shape = _FakeShape(
            volume=0.0,
            area=64.0,
            shape_type="Face",
            edge_count=4,
            face_count=1,
            solid_count=0,
            bbox=(8.0, 8.0, 0.0),
            center=(4.0, 4.0, 0.0),
        )
        obj.State = ("Up-to-date",)
        obj.isValid = lambda: True
        session.doc.Objects = (*session.doc.Objects, obj)
        receipt = PartProfileSurfaceConformanceReceipt(
            plan_sha256="3" * 64,
            operation=PartProfileSurfaceOperation.FACE,
            object_name=obj.Name,
            source_shape_sha256s=(source_results[0].native_receipt.result_shape_sha256,),
            result_shape_sha256=hashlib.sha256(obj.Shape.exportBrepToString().encode()).hexdigest(),
        )
        return ReviewedNativeExecutionResult(
            route=REVIEWED_PART_PROFILE_SURFACE_ROUTES[-1],
            object=obj,
            plan_sha256="3" * 64,
            plan_content_sha256="4" * 64,
            native_receipt=PartProfileSurfaceOwnershipClosure(
                invariant=PART_PROFILE_SURFACE_RESULT_INVARIANTS[PartProfileSurfaceOperation.FACE],
                native_receipt=receipt,
            ),
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_reviewed)
    session = _FakeSession()
    program = ModelProgram(
        task_id="task-reviewed-face",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "boundary",
                "apply_reviewed_intent",
                args={"intent": reviewed_box.to_mapping()},
            ),
            _command(
                "face",
                "apply_reviewed_intent",
                args={
                    "intent": reviewed_face.to_mapping(),
                    "source_a": {"command_id": "boundary", "slot": "object"},
                },
                depends_on=("boundary",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reviewed-face", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True]
    face_result = outcomes[-1].result.value
    assert face_result["reviewed_operation_id"].endswith(".face")
    identity = next(
        identity
        for _, identity in session.attached_identities
        if identity.object_id == face_result["object_id"]
    )
    observation = face_result["after"]
    assert identity.semantic_role.value == "feature"
    assert observation["solid_count"] == 0
    assert observation["area_mm2"] == 64.0
    assert observation["volume_mm3"] == 0.0
    assert session.result_object is source_result.object


def test_three_managed_reviewed_wires_loft_in_order_from_same_run_side_table(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed_profile = reviewed_curve_program(PartCurveOperation.REGULAR_POLYGON)
    reviewed_loft = reviewed_profile_surface_program(
        PartProfileSurfaceOperation.LOFT,
        source_count=3,
    )
    curve_route = next(
        route
        for route in REVIEWED_PART_CURVE_ROUTES
        if route.operation.operation_id == PartCurveOperation.REGULAR_POLYGON.value
    )
    produced_profiles: list[ReviewedNativeExecutionResult] = []
    resolved_profile_names: tuple[str, ...] | None = None
    bound_profile_names: tuple[str, ...] | None = None

    def execute_reviewed(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        if value == reviewed_profile:
            assert source_results == ()
            index = len(produced_profiles)
            obj = type("ManagedReviewedProfile", (), {})()
            obj.Name = f"ReviewedProfile{index}"
            obj.TypeId = curve_route.operation.native_type_id
            obj.Placement = _FakePlacement(0.0, 0.0, float(index) * 5.0)
            obj.Shape = _FakeShape(
                volume=0.0,
                area=0.0,
                shape_type="Wire",
                vertex_count=3,
                edge_count=3,
                face_count=0,
                solid_count=0,
                wire_closed=True,
                bbox=(6.0, 6.0, 0.0),
                center=(0.0, 0.0, float(index) * 5.0),
            )
            obj.State = ()
            obj.Document = session.doc
            obj.isValid = lambda: True
            session.doc.Objects = (*session.doc.Objects, obj)
            plan_sha256 = hashlib.sha256(f"profile-plan:{index}".encode()).hexdigest()
            result = ReviewedNativeExecutionResult(
                route=curve_route,
                object=obj,
                plan_sha256=plan_sha256,
                plan_content_sha256=hashlib.sha256(f"profile-content:{index}".encode()).hexdigest(),
                native_receipt=PartCurveConformanceReceipt(
                    plan_sha256=plan_sha256,
                    operation=PartCurveOperation.REGULAR_POLYGON,
                    object_name=obj.Name,
                    shape=PartCurveShapeSignature(
                        shape_type="Wire",
                        vertex_count=len(obj.Shape.Vertexes),
                        edge_count=len(obj.Shape.Edges),
                        face_count=len(obj.Shape.Faces),
                        length_mm=obj.Shape.Length,
                        area_mm2=obj.Shape.Area,
                    ),
                ),
            )
            produced_profiles.append(result)
            return result
        assert value == reviewed_loft
        nonlocal resolved_profile_names
        resolved_profile_names = tuple(item.object.Name for item in source_results)
        lowered = lower_reviewed_intent(value)
        native = profile_execution.execute_part_profile_surface_reviewed_plan(
            session.doc,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            lowered.route.operation,
            _ReviewedFamilyExecutionContext(
                session=session,
                document=session.doc,
                source_results=source_results,
            ),
        )
        return ReviewedNativeExecutionResult(
            route=lowered.route,
            object=native.object,
            plan_sha256=lowered.result.plan_document.document_digest,
            plan_content_sha256=lowered.result.plan_document.content_sha256,
            native_receipt=native.receipt,
        )

    def apply_loft(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: object,
    ) -> PartProfileSurfaceConformanceReceipt:
        nonlocal bound_profile_names
        assert raw
        assert len(expected_content_sha256) == len(expected_plan_sha256) == 64
        bound_profile_names = tuple(item.object.Name for item in bindings.sources)
        obj = type("ManagedReviewedLoft", (), {})()
        obj.Name = "ReviewedLoft"
        obj.TypeId = REVIEWED_PART_PROFILE_SURFACE_ROUTES[2].operation.native_type_id
        obj.Placement = _FakePlacement(0.0)
        obj.Shape = _FakeShape(
            volume=240.0,
            area=180.0,
            shape_type="Solid",
            vertex_count=6,
            edge_count=9,
            face_count=5,
            solid_count=1,
            bbox=(6.0, 6.0, 10.0),
            center=(0.0, 0.0, 5.0),
        )
        obj.State = ("Up-to-date",)
        obj.Document = bindings.document
        obj.isValid = lambda: True
        bindings.document.Objects = (*bindings.document.Objects, obj)
        return PartProfileSurfaceConformanceReceipt(
            plan_sha256=expected_plan_sha256,
            operation=PartProfileSurfaceOperation.LOFT,
            object_name=obj.Name,
            source_shape_sha256s=tuple(
                hashlib.sha256(item.object.Shape.exportBrepToString().encode()).hexdigest()
                for item in bindings.sources
            ),
            result_shape_sha256=hashlib.sha256(obj.Shape.exportBrepToString().encode()).hexdigest(),
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_reviewed)
    monkeypatch.setattr(profile_execution, "apply_part_profile_surface_plan", apply_loft)
    session = _FakeSession()
    source_ids = ("profile_0", "profile_1", "profile_2")
    requested_order = ("profile_2", "profile_0", "profile_1")
    program = ModelProgram(
        task_id="task-reviewed-three-profile-loft",
        base_revision=BASE_REVISION,
        operations=(
            *(
                _command(
                    source_id,
                    "apply_reviewed_intent",
                    args={"intent": reviewed_profile.to_mapping()},
                )
                for source_id in source_ids
            ),
            _command(
                "loft",
                "apply_reviewed_intent",
                args={
                    "intent": reviewed_loft.to_mapping(),
                    "sources": tuple(
                        {"command_id": source_id, "slot": "object"} for source_id in requested_order
                    ),
                },
                depends_on=source_ids,
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reviewed-three-profile-loft", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    expected_order = (
        "ReviewedProfile2",
        "ReviewedProfile0",
        "ReviewedProfile1",
    )
    assert [item.result.ok for item in outcomes] == [True, True, True, True]
    assert resolved_profile_names == bound_profile_names == expected_order
    assert len(produced_profiles) == 3
    assert len(session.doc.Objects) == len(session.attached_identities) == 4
    assert session.result_object is session.doc.Objects[-1]
    assert session.result_object.TypeId == "Part::Loft"
    identities = tuple(identity for _, identity in session.attached_identities)
    assert all(identity.semantic_role.value == "primitive" for identity in identities[:3])
    assert identities[-1].semantic_role.value == "feature"


def test_two_reviewed_primitives_cannot_masquerade_as_ordered_loft_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reviewed_box = reviewed_box_program()
    reviewed_loft = reviewed_profile_surface_program(PartProfileSurfaceOperation.LOFT)
    source_results: list[ReviewedNativeExecutionResult] = []
    native_called = False

    def execute_reviewed(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        if value == reviewed_box:
            assert source_results == ()
            index = len(source_results_holder)
            obj = type("ManagedReviewedSolid", (), {})()
            obj.Name = f"ReviewedSolid{index}"
            obj.TypeId = "Part::Box"
            obj.Length = 10.0
            obj.Width = 8.0
            obj.Height = 6.0
            obj.Placement = _FakePlacement(float(index))
            obj.Shape = _FakeShape(volume=480.0, area=376.0, bbox=(10.0, 8.0, 6.0))
            obj.State = []
            session.doc.Objects = (*session.doc.Objects, obj)
            digest = f"{index + 5:x}" * 64
            result = ReviewedNativeExecutionResult(
                route=REVIEWED_PART_BOX_ROUTE,
                object=obj,
                plan_sha256=digest,
                plan_content_sha256=f"{index + 7:x}" * 64,
                native_receipt=PartCoreConformanceReceipt(
                    plan_sha256=digest,
                    operation=PartCoreOperation.BOX,
                    object_name=obj.Name,
                    source_shape_sha256s=(),
                    result_shape_sha256=hashlib.sha256(
                        obj.Shape.exportBrepToString().encode()
                    ).hexdigest(),
                ),
            )
            source_results_holder.append(result)
            return result
        assert value == reviewed_loft
        assert source_results == tuple(source_results_holder)
        lowered = lower_reviewed_intent(value)
        return profile_execution.execute_part_profile_surface_reviewed_plan(
            session.doc,
            lowered.plan,
            lowered.payload,
            lowered.result.plan_document,
            lowered.route.operation,
            _ReviewedFamilyExecutionContext(
                session=session,
                document=session.doc,
                source_results=source_results,
            ),
        )

    source_results_holder = source_results

    def apply(*args: object, **kwargs: object) -> object:
        nonlocal native_called
        del args, kwargs
        native_called = True
        raise AssertionError("solid primitives must not reach profile native apply")

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_reviewed)
    monkeypatch.setattr(profile_execution, "apply_part_profile_surface_plan", apply)
    session = _FakeSession()
    program = ModelProgram(
        task_id="task-reviewed-loft-source-rejection",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "solid_a",
                "apply_reviewed_intent",
                args={"intent": reviewed_box.to_mapping()},
            ),
            _command(
                "solid_b",
                "apply_reviewed_intent",
                args={"intent": reviewed_box.to_mapping()},
            ),
            _command(
                "loft",
                "apply_reviewed_intent",
                args={
                    "intent": reviewed_loft.to_mapping(),
                    "sources": (
                        {"command_id": "solid_a", "slot": "object"},
                        {"command_id": "solid_b", "slot": "object"},
                    ),
                },
                depends_on=("solid_a", "solid_b"),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reviewed-loft-rejection", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True, False]
    assert native_called is False
    assert len(session.doc.Objects) == 2


@pytest.mark.parametrize(
    ("operation", "shape_type", "source_count"),
    (
        (PartOffsetOperation.PLANAR_WIRE_OFFSET, "Wire", 1),
        (PartOffsetOperation.EDGE_ON_FACE_PROJECTION, "Compound", 2),
    ),
)
def test_execute_program_adopts_reviewed_offset_non_solids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: PartOffsetOperation,
    shape_type: str,
    source_count: int,
) -> None:
    reviewed_box = reviewed_box_program()
    reviewed_offset = reviewed_offset_program(operation)
    primitive_results: list[ReviewedNativeExecutionResult] = []

    def execute_reviewed(
        session: _FakeSession,
        value: object,
        *,
        source_results: tuple[ReviewedNativeExecutionResult, ...] = (),
    ) -> ReviewedNativeExecutionResult:
        if value == reviewed_box:
            assert source_results == ()
            index = len(primitive_results)
            obj = type("ManagedReviewedOffsetSource", (), {})()
            obj.Name = f"ReviewedOffsetSource{index}"
            obj.TypeId = "Part::Box"
            obj.Length = 10.0
            obj.Width = 8.0
            obj.Height = 6.0
            obj.Placement = _FakePlacement(float(index))
            obj.Shape = _FakeShape(volume=480.0, area=376.0, bbox=(10.0, 8.0, 6.0))
            obj.State = []
            session.doc.Objects = (*session.doc.Objects, obj)
            plan_sha256 = f"{index + 1:x}" * 64
            result = ReviewedNativeExecutionResult(
                route=REVIEWED_PART_BOX_ROUTE,
                object=obj,
                plan_sha256=plan_sha256,
                plan_content_sha256=f"{index + 3:x}" * 64,
                native_receipt=PartCoreConformanceReceipt(
                    plan_sha256=plan_sha256,
                    operation=PartCoreOperation.BOX,
                    object_name=obj.Name,
                    source_shape_sha256s=(),
                    result_shape_sha256=hashlib.sha256(
                        obj.Shape.exportBrepToString().encode()
                    ).hexdigest(),
                ),
            )
            primitive_results.append(result)
            return result

        assert value == reviewed_offset
        assert source_results == tuple(primitive_results)
        route = next(
            item
            for item in REVIEWED_PART_OFFSET_ROUTES
            if item.operation.operation_id == operation.value
        )
        obj = type("ManagedReviewedOffsetResult", (), {})()
        obj.Name = f"Reviewed{shape_type}Offset"
        obj.TypeId = route.operation.native_type_id
        obj.Placement = _FakePlacement(0.0)
        obj.Shape = _FakeShape(
            volume=0.0,
            area=64.0,
            shape_type=shape_type,
            edge_count=1,
            face_count=0,
            solid_count=0,
            bbox=(8.0, 8.0, 1.0),
            center=(4.0, 4.0, 0.5),
        )
        obj.State = ("Up-to-date",)
        obj.isValid = lambda: True
        session.doc.Objects = (*session.doc.Objects, obj)
        plan_sha256 = "9" * 64
        source_shape_sha256s = tuple(
            item.native_receipt.result_shape_sha256 for item in source_results
        )
        receipt = PartOffsetConformanceReceipt(
            plan_sha256=plan_sha256,
            operation=operation,
            object_name=obj.Name,
            native_type_id=obj.TypeId,
            source_object_names=tuple(item.object.Name for item in source_results),
        )
        return ReviewedNativeExecutionResult(
            route=route,
            object=obj,
            plan_sha256=plan_sha256,
            plan_content_sha256="a" * 64,
            native_receipt=PartOffsetOwnershipClosure(
                invariant=PART_OFFSET_RESULT_INVARIANTS[operation],
                native_receipt=receipt,
                source_shape_sha256s=source_shape_sha256s,
                result_shape_sha256=hashlib.sha256(
                    obj.Shape.exportBrepToString().encode()
                ).hexdigest(),
            ),
        )

    monkeypatch.setattr(executor_module, "_execute_reviewed_intent_native", execute_reviewed)
    source_commands = tuple(
        _command(
            f"source_{index}",
            "apply_reviewed_intent",
            args={"intent": reviewed_box.to_mapping()},
        )
        for index in range(source_count)
    )
    offset_args: dict[str, object] = {
        "intent": reviewed_offset.to_mapping(),
        "sources": tuple(
            {"command_id": f"source_{index}", "slot": "object"} for index in range(source_count)
        ),
    }
    program = ModelProgram(
        task_id=f"task-reviewed-{operation.value}",
        base_revision=BASE_REVISION,
        operations=(
            *source_commands,
            _command(
                "offset",
                "apply_reviewed_intent",
                args=offset_args,
                depends_on=tuple(f"source_{index}" for index in range(source_count)),
            ),
        ),
        acceptance=AcceptanceSpec(id=f"acceptance-{operation.value}", criteria=()),
    )
    session = _FakeSession()

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )

    assert all(item.result.ok for item in outcomes)
    observation = outcomes[-1].result.value["after"]
    result_object, identity = next(
        (item, item_identity)
        for item, item_identity in session.attached_identities
        if item_identity.object_id == observation["object_id"]
    )
    assert identity.semantic_role.value == "feature"
    assert result_object.Shape.ShapeType == shape_type
    assert observation["solid_count"] == 0
    assert observation["volume_mm3"] == 0.0


def _run_real_freecad_script(
    runtime_python: Path,
    code: str,
    *,
    tmp_path: Path,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    """Run a large real-engine probe without exceeding CreateProcess limits."""

    args = [str(runtime_python), "-c", code]
    environment: dict[str, str] | None = None
    script_capability = None
    freecad_temp_capability = None
    freecad_temp: Path | None = None
    if sys.platform == "win32":
        script_root = tmp_path / "windows-real-freecad-script"
        root_capability = _file_compat.ensure_private_directory(
            script_root,
            exclusive=True,
        )
        script = script_root / "probe.py"
        descriptor, script_capability = _file_compat.open_private_file(
            script,
            create=True,
            read_write=True,
            exclusive=True,
            expected_parent=root_capability,
        )
        try:
            payload = code.encode("utf-8")
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _file_compat.validate_windows_path(script_capability, directory=False)
        freecad_temp = script_root / "freecad-temp"
        freecad_temp_capability = _file_compat.ensure_private_directory(
            freecad_temp,
            expected_parent=root_capability,
            exclusive=True,
        )
        environment = dict(os.environ)
        environment["FREECAD_USER_TEMP"] = str(freecad_temp)
        args = [str(runtime_python), "-B", str(script)]

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if script_capability is not None:
        _file_compat.validate_windows_path(script_capability, directory=False)
        assert freecad_temp is not None
        assert freecad_temp_capability is not None
        _file_compat.validate_windows_path(freecad_temp_capability, directory=True)
        assert tuple(freecad_temp.iterdir()) == ()
    return result


@pytest.mark.slow
def test_real_freecad_reviewed_primitives_execute_checkpoint_reopen_and_reject_duplicate(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")
    source_root = Path(__file__).parents[1] / "src"
    operations = (
        PartCoreOperation.BOX,
        PartCoreOperation.CONE,
        PartCoreOperation.CYLINDER,
        PartCoreOperation.ELLIPSOID,
        PartCoreOperation.PRISM,
        PartCoreOperation.SPHERE,
        PartCoreOperation.TORUS,
        PartCoreOperation.WEDGE,
    )
    reviewed_mappings = tuple(
        reviewed_primitive_program(operation).to_mapping() for operation in operations
    )
    expected_types = tuple(PART_CORE_NATIVE_SPECS[operation].type_id for operation in operations)
    code = (
        f"import sys; sys.path.insert(0, {str(source_root)!r})\n"
        + "import os\n"
        + "from pathlib import Path\n"
        + "from vibecad.execution.candidate import ActiveCandidate, SessionBinding\n"
        + "from vibecad.execution.executor import "
        + "InProcessCadExecutor, _entity_observations, _same_import_observations\n"
        + "from vibecad.execution.revisions import LocalRevisionStore, ProjectHead\n"
        + "from vibecad.workflow.contracts import "
        + "AcceptanceSpec, ModelCommand, ModelProgram, ValueSource\n"
        + f"root = Path({str(tmp_path)!r})\n"
        + "native_root = root / 'freecad-native-cache'\n"
        + "if os.name == 'nt':\n"
        + "    from vibecad._file_compat import ensure_private_directory\n"
        + "    ensure_private_directory(native_root, exclusive=True)\n"
        + "else:\n"
        + "    native_root.mkdir(mode=0o700)\n"
        + "os.environ['FREECAD_USER_TEMP'] = str(native_root)\n"
        + f"reviewed_mappings = {reviewed_mappings!r}\n"
        + f"expected_types = {expected_types!r}\n"
        + f"project_id = {PROJECT_ID!r}\n"
        + f"base_revision = {BASE_REVISION!r}\n"
        + f"candidate_revision = {CANDIDATE_REVISION!r}\n"
        + "commands = tuple(ModelCommand(id=f'reviewed_{index}', "
        + "op='apply_reviewed_intent', target={}, args={'intent': mapping}, "
        + "depends_on=(), preserve=(), source=ValueSource.MODEL) "
        + "for index, mapping in enumerate(reviewed_mappings))\n"
        + "program = ModelProgram(task_id='task-real-reviewed-primitives', "
        + "base_revision=base_revision, operations=commands, "
        + "acceptance=AcceptanceSpec(id='accept-real-reviewed-primitives', criteria=()))\n"
        + "store = object.__new__(LocalRevisionStore)\n"
        + "executor = InProcessCadExecutor(store=store)\n"
        + "session = executor.create_empty(revision_id=candidate_revision)\n"
        + "loaded = None\n"
        + "try:\n"
        + "    head = ProjectHead(project_id=project_id, generation=0, "
        + "revision_id=base_revision, manifest_sha256='a' * 64)\n"
        + "    candidate = ActiveCandidate(project_id=project_id, base_head=head, "
        + "binding=SessionBinding(project_id=project_id, revision_id=candidate_revision, "
        + "session=session), model_path=root / 'model.FCStd', "
        + "step_path=root / 'model.step')\n"
        + "    validated = executor.validate_program(program)\n"
        + "    outcomes = executor.execute_program(program=validated, candidate=candidate)\n"
        + "    assert len(outcomes) == len(reviewed_mappings), "
        + "tuple(item.result.to_mapping() for item in outcomes)\n"
        + "    assert all(item.result.ok for item in outcomes)\n"
        + "    assert all(item.result.value['kind'] == 'reviewed_intent_applied' "
        + "for item in outcomes)\n"
        + "    entities = _entity_observations(session)\n"
        + "    assert len(entities) == len(expected_types)\n"
        + "    assert {item.object_type for item in entities} == set(expected_types)\n"
        + "    assert all(item.valid_shape and item.solid_count == 1 "
        + "and item.volume_mm3 is not None and item.volume_mm3 > 0 for item in entities), "
        + "tuple(item.to_mapping() for item in entities)\n"
        + "    assert session.get_result_object().Name.startswith('VcPart_wedge_')\n"
        + "    before_duplicate = tuple(session.doc.Objects)\n"
        + "    duplicate = executor.execute_program(program=validated, candidate=candidate)\n"
        + "    assert len(duplicate) == 1 and not duplicate[0].result.ok\n"
        + "    assert tuple(session.doc.Objects) == before_duplicate\n"
        + "    executor.checkpoint_fcstd(session, root / 'model.FCStd')\n"
        + "    loaded = executor.load_fcstd(root / 'model.FCStd')\n"
        + "    reloaded = _entity_observations(loaded)\n"
        + "    assert _same_import_observations(reloaded, entities)\n"
        + "    assert loaded.get_result_object().Name == session.get_result_object().Name\n"
        + "    print('REAL_REVIEWED_PRIMITIVES_OK')\n"
        + "finally:\n"
        + "    if loaded is not None:\n"
        + "        loaded.close_document()\n"
        + "    session.close_document()\n"
        + "assert tuple(native_root.iterdir()) == ()\n"
    )
    result = _run_real_freecad_script(
        runtime_python,
        code,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "REAL_REVIEWED_PRIMITIVES_OK" in result.stdout


def test_execute_program_supplies_trusted_profile_version_and_object_counter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def adapter(program: object, handlers: object, **kwargs: object) -> tuple[object, ...]:
        captured.update(kwargs)
        captured["program"] = program
        captured["handlers"] = handlers
        return ()

    monkeypatch.setattr(executor_module, "_execute_validated_program", adapter)
    executor = InProcessCadExecutor(store=_store())
    program = executor.validate_program(_program())
    session = _FakeSession()

    assert (
        executor.execute_program(
            program=program,
            candidate=_active(session, tmp_path),
        )
        == ()
    )
    assert captured["execution_profile"] is executor.execution_profile
    assert captured["freecad_version"] == (1, 1)
    assert captured["gui_main_thread"] is False
    counter = captured["object_count"]
    assert callable(counter)
    assert counter() == 0
    session.doc.Objects = (object(),)
    assert counter() == 1


def test_execute_program_rejects_runtime_version_before_any_cad_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0

    def add_box(session: object, **kwargs: object) -> object:
        nonlocal calls
        del session, kwargs
        calls += 1
        return object()

    monkeypatch.setattr(executor_module, "_add_box", add_box)
    executor = InProcessCadExecutor(store=_store())
    session = _FakeSession()
    session.freecad_version = (2, 0)

    with pytest.raises(ExecutorError) as caught:
        executor.execute_program(
            program=executor.validate_program(_program()),
            candidate=_active(session, tmp_path),
        )

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert calls == 0
    assert session.doc.Objects == ()


def test_execute_program_runs_all_six_managed_operations_with_fixed_traces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_add_cylinder", _fake_add_cylinder)
    monkeypatch.setattr(executor_module, "_modify_part", _fake_modify_part)
    monkeypatch.setattr(executor_module, "_move_part", _fake_move_part)
    monkeypatch.setattr(executor_module, "_rotate_part", _fake_rotate_part)
    monkeypatch.setattr(
        executor_module,
        "_managed_assembly_shape",
        lambda session: session.shape,
    )
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_six_operation_program()),
        candidate=_active(session, tmp_path),
    )

    assert tuple(outcome.result.operation_id for outcome in outcomes) == (
        "box",
        "cylinder",
        "modify",
        "move",
        "rotate",
        "inspect",
    )
    assert all(outcome.result.ok for outcome in outcomes)
    values = [outcome.result.value for outcome in outcomes]
    assert [value["operation"] for value in values] == [  # type: ignore[index]
        "create_box",
        "create_cylinder",
        "modify_parameter",
        "move_part",
        "rotate_part",
        "inspect_model",
    ]
    identities = [identity for _, identity in session.attached_identities]
    assert [identity.provenance.operation_id for identity in identities] == [
        "box",
        "cylinder",
    ]
    assert len({identity.object_id for identity in identities}) == 2
    assert values[0]["object_id"] == identities[0].object_id  # type: ignore[index]
    assert values[1]["object_id"] == identities[1].object_id  # type: ignore[index]


def test_execute_program_creates_native_cone_sphere_and_torus_with_live_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_cone", _fake_add_cone)
    monkeypatch.setattr(executor_module, "_add_sphere", _fake_add_sphere)
    monkeypatch.setattr(executor_module, "_add_torus", _fake_add_torus)
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_part_native_primitives_program()),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, True]
    assert [outcome.result.operation_id for outcome in outcomes] == ["cone", "sphere", "torus"]
    values = [outcome.result.value for outcome in outcomes]
    assert [value["operation"] for value in values] == [  # type: ignore[index]
        "create_cone",
        "create_sphere",
        "create_torus",
    ]
    assert [value["after"]["object_type"] for value in values] == [  # type: ignore[index]
        "Part::Cone",
        "Part::Sphere",
        "Part::Torus",
    ]
    identities = [identity for _, identity in session.attached_identities]
    assert [identity.provenance.operation_id for identity in identities] == [
        "cone",
        "sphere",
        "torus",
    ]
    assert len({identity.object_id for identity in identities}) == 3


@pytest.mark.parametrize(
    ("operation", "leaf", "object_type", "relation"),
    (
        ("boolean_cut", _fake_boolean_cut, "Part::Cut", "cut"),
        ("boolean_fuse", _fake_boolean_fuse, "Part::Fuse", "fuse"),
        ("boolean_common", _fake_boolean_common, "Part::Common", "common"),
    ),
)
def test_execute_program_creates_managed_native_boolean_feature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    leaf: object,
    object_type: str,
    relation: str,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, f"_{operation}_uncommitted", leaf)
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_part_boolean_program(operation)),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, True]
    value = outcomes[-1].result.value
    assert value["kind"] == "boolean_created"  # type: ignore[index]
    assert value["operation"] == operation  # type: ignore[index]
    assert value["after"]["object_type"] == object_type  # type: ignore[index]
    assert value["after"]["semantic_role"] == "feature"  # type: ignore[index]
    parameters = {
        item["name"]: item["value"]
        for item in value["after"]["parameters"]  # type: ignore[index]
    }
    assert parameters == {
        "base_object_id": value["base_object_id"],  # type: ignore[index]
        "operation": relation,
        "tool_object_id": value["tool_object_id"],  # type: ignore[index]
    }
    assert session.result_object is session.doc.Objects[-1]


def test_managed_boolean_rolls_back_creation_when_identity_attachment_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(
        executor_module,
        "_boolean_cut_uncommitted",
        _fake_boolean_cut,
    )
    session = _FakeSession()
    original_attach = session.attach_object_identity

    def attach(obj: object, identity: object) -> object:
        if getattr(obj, "TypeId", None) == "Part::Cut":
            raise RuntimeError("private identity fault")
        return original_attach(obj, identity)

    session.attach_object_identity = attach  # type: ignore[method-assign]
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_part_boolean_program("boolean_cut")),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, False]
    assert [obj.TypeId for obj in session.doc.Objects] == ["Part::Box", "Part::Box"]
    assert session.result_object is session.doc.Objects[-1]
    assert all(identity.object_type != "Part::Cut" for _, identity in session.attached_identities)


def test_managed_boolean_rolls_back_identity_owner_and_root_after_late_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(
        executor_module,
        "_boolean_cut_uncommitted",
        _fake_boolean_cut,
    )
    session = _FakeSession()
    original_attach = session.attach_object_identity

    def attach_then_corrupt(obj: object, identity: object) -> object:
        attached = original_attach(obj, identity)
        if getattr(obj, "TypeId", None) == "Part::Cut":
            obj.Shape = _FakeShape(volume=0.0)  # type: ignore[attr-defined]
        return attached

    session.attach_object_identity = attach_then_corrupt  # type: ignore[method-assign]
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(_part_boolean_program("boolean_cut")),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, False]
    assert [obj.TypeId for obj in session.doc.Objects] == ["Part::Box", "Part::Box"]
    assert session.result_object is session.doc.Objects[-1]
    assert all(identity.object_type != "Part::Cut" for _, identity in session.attached_identities)


@pytest.mark.parametrize(
    ("operation", "leaf"),
    (
        ("boolean_cut", _fake_boolean_cut),
        ("boolean_fuse", _fake_boolean_fuse),
        ("boolean_common", _fake_boolean_common),
    ),
)
def test_managed_boolean_operand_edit_propagates_to_each_native_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    leaf: object,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, f"_{operation}_uncommitted", leaf)

    def modify(session: _FakeSession, **kwargs: object) -> object:
        kwargs.pop("result_name", None)
        result = _fake_modify_part(session, **kwargs)  # type: ignore[arg-type]
        _refresh_fake_boolean_descendants(session, session.get_object(str(kwargs["name"])))
        session.doc.recompute()
        return result

    monkeypatch.setattr(executor_module, "_modify_part_uncommitted", modify)
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_part_boolean_edit_program(operation)),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, True, True]
    assert session.doc.Objects[-1].Shape.BoundBox.XLength == 22


def test_managed_boolean_operand_edit_rejects_stale_descendant_geometry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(
        executor_module,
        "_boolean_cut_uncommitted",
        _fake_boolean_cut,
    )

    def modify_without_recompute(session: _FakeSession, **kwargs: object) -> object:
        kwargs.pop("result_name", None)
        return _fake_modify_part(session, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        executor_module,
        "_modify_part_uncommitted",
        modify_without_recompute,
    )
    monkeypatch.setattr(
        executor_module,
        "_DocumentRecomputeObserver",
        _FakeMissingDescendantRecomputeObserver,
    )
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_part_boolean_edit_program("boolean_cut")),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, True, False]
    assert session.doc.Objects[0].Length == 20
    assert session.doc.Objects[-1].Shape.Volume == 7500


def test_boolean_recompute_receipt_rejects_missing_target_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_boolean_cut_uncommitted", _fake_boolean_cut)

    class DescendantOnly(_FakeRecomputeObserver):
        def slotRecomputedObject(self, obj: object) -> None:  # noqa: N802 - FreeCAD API
            if getattr(obj, "TypeId", None) == "Part::Cut":
                super().slotRecomputedObject(obj)

    monkeypatch.setattr(executor_module, "_DocumentRecomputeObserver", DescendantOnly)

    def modify(session: _FakeSession, **kwargs: object) -> object:
        kwargs.pop("result_name", None)
        result = _fake_modify_part(session, **kwargs)  # type: ignore[arg-type]
        _refresh_fake_boolean_descendants(session, session.get_object(str(kwargs["name"])))
        session.doc.recompute()
        return result

    monkeypatch.setattr(executor_module, "_modify_part_uncommitted", modify)
    session = _FakeSession()
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(_part_boolean_edit_program("boolean_cut")),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True, True, True, False]
    assert session.doc.Objects[0].Length == 20


@pytest.mark.parametrize(
    ("operation", "arguments", "private_leaf"),
    (
        ("move_part", {"position_mm": (1, 0, 0)}, "_move_part_uncommitted"),
        ("rotate_part", {"axis": "z", "angle_deg": 90}, "_rotate_part_uncommitted"),
    ),
)
def test_managed_boolean_operand_transform_propagates_inside_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    arguments: dict[str, object],
    private_leaf: str,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_boolean_cut_uncommitted", _fake_boolean_cut)

    def transform(session: _FakeSession, **kwargs: object) -> object:
        kwargs.pop("result_name", None)
        target = session.get_object(str(kwargs["name"]))
        result = (
            _fake_move_part(session, **kwargs)  # type: ignore[arg-type]
            if operation == "move_part"
            else _fake_rotate_part(session, **kwargs)  # type: ignore[arg-type]
        )
        _refresh_fake_boolean_descendants(session, target)
        session.doc.recompute()
        return result

    monkeypatch.setattr(executor_module, private_leaf, transform)
    created = _part_boolean_program("boolean_cut").operations
    program = ModelProgram(
        task_id=f"task-executor-boolean-{operation}",
        base_revision=BASE_REVISION,
        operations=(
            *created,
            _command(
                "transform",
                operation,
                target={"object": {"command_id": "base", "slot": "object"}},
                args=arguments,
                depends_on=("base", "boolean"),
            ),
        ),
        acceptance=AcceptanceSpec(id=f"acceptance-boolean-{operation}", criteria=()),
    )
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())
    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, True, True]


def test_managed_boolean_remains_owned_by_its_explicit_component(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_boolean_fuse_uncommitted", _fake_boolean_fuse)
    program = _component_boolean_program()
    session = _FakeComponentSession()
    executor = InProcessCadExecutor(store=_store())
    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, True, True]
    result = session.doc.Objects[-1]
    assert session.owner_of(result.Name) == "Bracket"
    assert session.get_result_object("Bracket") is result
    records = session.list_component_identity_records()
    assert sorted(identity.object_type for _obj, identity in records[0][3]) == [
        "Part::Box",
        "Part::Box",
        "Part::Fuse",
    ]


def test_component_boolean_can_add_another_tool_and_close_a_nested_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_boolean_fuse_uncommitted", _fake_boolean_fuse)
    session = _FakeComponentSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_nested_component_boolean_program()),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True] * 6
    records = session.list_component_identity_records()
    assert len(records[0][3]) == 5
    assert sum(identity.object_type == "Part::Fuse" for _obj, identity in records[0][3]) == 2
    assert executor_module._component_observations(session)[0].solid_count == 1


def test_component_delivery_rejects_an_unconsumed_second_solid_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    program = ModelProgram(
        task_id="task-executor-incomplete-component",
        base_revision=BASE_REVISION,
        operations=(
            _command("component", "create_component", args={"name": "Bracket"}),
            _command(
                "first",
                "create_box",
                target={"component": {"command_id": "component", "slot": "component"}},
                args={"length_mm": 20, "width_mm": 10, "height_mm": 10},
                depends_on=("component",),
            ),
            _command(
                "second",
                "create_box",
                target={"component": {"command_id": "component", "slot": "component"}},
                args={
                    "length_mm": 5,
                    "width_mm": 5,
                    "height_mm": 5,
                    "position_mm": (30, 0, 0),
                },
                depends_on=("first",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-incomplete-component", criteria=()),
    )
    session = _FakeComponentSession()
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(session, tmp_path),
    )
    assert all(outcome.result.ok for outcome in outcomes)

    with pytest.raises(executor_module._ObservationFailure):
        executor_module._component_observations(session)
    with pytest.raises(executor_module._ObservationFailure):
        executor_module._managed_assembly_shape(session)


def test_component_boolean_observation_rejects_consumed_result_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_boolean_fuse_uncommitted", _fake_boolean_fuse)
    session = _FakeComponentSession()
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(_component_boolean_program()),
        candidate=_active(session, tmp_path),
    )
    assert all(outcome.result.ok for outcome in outcomes)
    base = next(obj for obj in session.doc.Objects if obj.TypeId == "Part::Box")
    session._result_by_part["Bracket"] = base

    with pytest.raises(executor_module._ObservationFailure):
        executor_module._component_observations(session)
    with pytest.raises(executor_module._ObservationFailure):
        executor_module._managed_assembly_shape(session)


def test_component_boolean_observation_rejects_cross_component_operand(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_boolean_fuse_uncommitted", _fake_boolean_fuse)
    session = _FakeComponentSession()
    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(
            _component_boolean_program(include_empty_component=True),
        ),
        candidate=_active(session, tmp_path),
    )
    assert all(outcome.result.ok for outcome in outcomes)
    tool = next(
        obj
        for obj in session.doc.Objects
        if obj.TypeId == "Part::Box" and obj.Placement.Base.x == 15
    )
    session._parts["Bracket"]["objects"].remove(tool.Name)  # type: ignore[attr-defined]
    session._parts["Other"]["objects"].add(tool.Name)  # type: ignore[attr-defined]
    session._result_by_part["Other"] = tool

    with pytest.raises(executor_module._ObservationFailure):
        executor_module._entity_observations(session)
    with pytest.raises(executor_module._ObservationFailure):
        executor_module._component_observations(session)


def test_managed_boolean_rejects_reusing_an_already_consumed_operand(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    monkeypatch.setattr(executor_module, "_boolean_cut_uncommitted", _fake_boolean_cut)
    program = ModelProgram(
        task_id="task-executor-reused-boolean-operand",
        base_revision=BASE_REVISION,
        operations=(
            *_part_boolean_program("boolean_cut").operations,
            _command(
                "reuse",
                "boolean_cut",
                target={
                    "base": {"command_id": "base", "slot": "object"},
                    "tool": {"command_id": "boolean", "slot": "object"},
                },
                depends_on=("base", "boolean"),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-reused-boolean-operand", criteria=()),
    )
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True, True, False]
    assert outcomes[-1].result.error is not None
    assert outcomes[-1].result.error.category.value == "runtime"


def test_execute_program_builds_places_and_inspects_explicit_components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)

    def place(
        session: _FakeComponentSession,
        *,
        part_name: str,
        position: tuple[float, float, float],
        rotation_axis: str,
        angle: float,
    ) -> tuple[float, ...]:
        quaternion = executor_module._axis_rotation(rotation_axis, angle)
        session._parts[part_name]["container"].Placement = _FakePlacement(
            *position,
            q=quaternion,
        )
        return (*position, *quaternion)

    monkeypatch.setattr(executor_module, "_set_absolute_component_placement", place)
    session = _FakeComponentSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_component_program()),
        candidate=_active(session, tmp_path),
    )

    assert [item.result.ok for item in outcomes] == [True] * 6
    component_ids = [outcomes[index].result.value["component_id"] for index in (0, 2)]
    assert len(set(component_ids)) == 2
    assert outcomes[1].result.value["component_id"] == component_ids[0]
    assert outcomes[3].result.value["component_id"] == component_ids[1]
    assert outcomes[4].result.value["after"]["placement"][:3] == (20, 0, 0)
    inspection = outcomes[5].result.value
    assert len(inspection["components"]) == 2
    assert len(inspection["interferences"]) == 1
    observed = inspection["interferences"][0]
    assert observed["component_a_id"] == min(component_ids)
    assert observed["component_b_id"] == max(component_ids)
    assert observed["common_volume_mm3"] == 0.0
    assert observed["interfering"] is False
    assert inspection["bom"]["schema_version"] == SCHEMA_VERSION
    assert inspection["bom"]["component_count"] == 2
    assert inspection["bom"]["rows"] == ()
    assert inspection["bom"]["missing_component_ids"] == tuple(sorted(component_ids))
    assert inspection["bom"]["conflicts"] == ()
    assert inspection["bom"]["total_quantity"] == 0
    assert inspection["bom"]["total_mass_kg"] == 0
    assert inspection["bom"]["complete"] is False
    assert inspection["bom_csv"] is None


def test_component_bom_groups_equal_geometry_and_emits_revision_bound_csv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)

    def place(session, *, part_name, position, rotation_axis, angle):
        quaternion = executor_module._axis_rotation(rotation_axis, angle)
        session._parts[part_name]["container"].Placement = _FakePlacement(
            *position,
            q=quaternion,
        )
        return (*position, *quaternion)

    monkeypatch.setattr(executor_module, "_set_absolute_component_placement", place)
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_component_bom_program()),
        candidate=_active(_FakeComponentSession(), tmp_path),
    )

    assert len(outcomes) == 8
    assert all(item.result.ok for item in outcomes)
    first_bom = outcomes[2].result.value
    assert first_bom["kind"] == "component_bom_set"
    assert first_bom["bom"]["complete"] is True
    inspection = outcomes[-1].result.value
    assert inspection["bom_revision_id"] == CANDIDATE_REVISION
    bom = inspection["bom"]
    assert bom["complete"] is True
    assert bom["missing_component_ids"] == ()
    assert bom["conflicts"] == ()
    assert bom["total_quantity"] == 2
    assert bom["total_mass_kg"] == pytest.approx(0.0054)
    assert len(bom["rows"]) == 1
    row = bom["rows"][0]
    assert row["part_number"] == "BRACKET-001"
    assert row["quantity"] == 2
    assert row["unit_mass_kg"] == pytest.approx(0.0027)
    assert len(row["component_ids"]) == 2
    assert inspection["bom_csv"] == outcomes[6].result.value["bom_csv"]
    assert inspection["bom_csv"].splitlines()[0] == (
        "part_number,description,material,density_kg_m3,quantity,unit_mass_kg,"
        "total_mass_kg,component_ids,geometry_digest"
    )
    assert (
        inspection["bom_csv"]
        .splitlines()[1]
        .startswith("BRACKET-001,Mounting bracket,Aluminum 6061,2700,2,")
    )


def test_component_bom_reports_same_part_number_geometry_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)

    def place(session, *, part_name, position, rotation_axis, angle):
        quaternion = executor_module._axis_rotation(rotation_axis, angle)
        session._parts[part_name]["container"].Placement = _FakePlacement(
            *position,
            q=quaternion,
        )
        return (*position, *quaternion)

    monkeypatch.setattr(executor_module, "_set_absolute_component_placement", place)
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_component_bom_program(component_b_length=5)),
        candidate=_active(_FakeComponentSession(), tmp_path),
    )

    assert all(item.result.ok for item in outcomes)
    bom = outcomes[-1].result.value["bom"]
    assert bom["complete"] is False
    assert bom["rows"] == ()
    assert bom["missing_component_ids"] == ()
    assert bom["total_quantity"] == 0
    assert bom["total_mass_kg"] == 0
    assert len(bom["conflicts"]) == 1
    conflict = bom["conflicts"][0]
    assert conflict["schema_version"] == SCHEMA_VERSION
    assert conflict["part_number"] == "BRACKET-001"
    assert conflict["component_ids"] == tuple(
        sorted(
            (
                outcomes[0].result.value["component_id"],
                outcomes[3].result.value["component_id"],
            )
        )
    )
    assert outcomes[-1].result.value["bom_csv"] is None


def test_place_component_fails_closed_when_global_shapes_interfere(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)

    def place(session, *, part_name, position, rotation_axis, angle):
        quaternion = executor_module._axis_rotation(rotation_axis, angle)
        session._parts[part_name]["container"].Placement = _FakePlacement(
            *position,
            q=quaternion,
        )
        return (*position, *quaternion)

    monkeypatch.setattr(executor_module, "_set_absolute_component_placement", place)
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_component_program(component_b_position=(2, 0, 0))),
        candidate=_active(_FakeComponentSession(), tmp_path),
    )

    assert [item.result.ok for item in outcomes[:4]] == [True] * 4
    assert len(outcomes) == 5
    assert outcomes[4].result.ok is False


@pytest.mark.slow
def test_real_freecad_component_program_checkpoints_reloads_and_exports_step(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")
    code = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r})\n"
        + "from pathlib import Path\n"
        + "from vibecad.execution.candidate import ActiveCandidate, SessionBinding\n"
        + "from vibecad.execution.executor import (\n"
        + "    InProcessCadExecutor, _component_observations, "
        + "_export_session_step, _interference_observations, _shape_observation,\n"
        + ")\n"
        + "from vibecad.execution.revisions import LocalRevisionStore, ProjectHead\n"
        + "from vibecad.workflow.contracts import (\n"
        + "    AcceptanceSpec, ModelCommand, ModelProgram, ValueSource,\n"
        + ")\n"
        + f"root = Path({str(tmp_path)!r})\n"
        + f"project_id = {PROJECT_ID!r}\n"
        + f"base_revision = {BASE_REVISION!r}\n"
        + f"candidate_revision = {CANDIDATE_REVISION!r}\n"
        + "def command(identifier, operation, *, target=None, args=None, depends=()):\n"
        + "    return ModelCommand(\n"
        + "        id=identifier, op=operation, target=target or {}, args=args or {},\n"
        + "        depends_on=depends, preserve=(), source=ValueSource.MODEL,\n"
        + "    )\n"
        + "program = ModelProgram(\n"
        + "    task_id='task-real-components', base_revision=base_revision, operations=(\n"
        + "        command('component_a', 'create_component', args={'name': 'A'}),\n"
        + "        command('box_a', 'create_box',\n"
        + "            target={'component': {'command_id': 'component_a', "
        + "'slot': 'component'}},\n"
        + "            args={'length_mm': 10, 'width_mm': 10, 'height_mm': 10},\n"
        + "            depends=('component_a',)),\n"
        + "        command('component_b', 'create_component', args={'name': 'B'},\n"
        + "            depends=('box_a',)),\n"
        + "        command('box_b', 'create_box',\n"
        + "            target={'component': {'command_id': 'component_b', "
        + "'slot': 'component'}},\n"
        + "            args={'length_mm': 5, 'width_mm': 10, 'height_mm': 10},\n"
        + "            depends=('component_b',)),\n"
        + "        command('place_b', 'place_component',\n"
        + "            target={'component': {'command_id': 'component_b', "
        + "'slot': 'component'}},\n"
        + "            args={'position_mm': [20, 0, 0], 'rotation_axis': 'z', "
        + "'angle_deg': 0},\n"
        + "            depends=('box_b',)),\n"
        + "        command('inspect', 'inspect_model', depends=('place_b',)),\n"
        + "    ), acceptance=AcceptanceSpec(id='accept-real-components', criteria=()),\n"
        + ")\n"
        + "store = object.__new__(LocalRevisionStore)\n"
        + "executor = InProcessCadExecutor(store=store)\n"
        + "session = executor.create_empty(revision_id=candidate_revision)\n"
        + "loaded = None\n"
        + "try:\n"
        + "    head = ProjectHead(project_id=project_id, generation=0,\n"
        + "        revision_id=base_revision, manifest_sha256='a' * 64)\n"
        + "    candidate = ActiveCandidate(project_id=project_id, base_head=head,\n"
        + "        binding=SessionBinding(project_id=project_id,\n"
        + "            revision_id=candidate_revision, session=session),\n"
        + "        model_path=root / 'model.FCStd', step_path=root / 'model.step')\n"
        + "    outcomes = executor.execute_program(\n"
        + "        program=executor.validate_program(program), candidate=candidate)\n"
        + "    assert len(outcomes) == 6 and all(item.result.ok for item in outcomes), "
        + "[item.result.to_mapping() for item in outcomes]\n"
        + "    components = _component_observations(session)\n"
        + "    interferences = _interference_observations(session)\n"
        + "    shape = _shape_observation(session)\n"
        + "    assert len(components) == 2 and len(interferences) == 1\n"
        + "    assert interferences[0].interfering is False\n"
        + "    assert abs(shape.volume_mm3 - 1500.0) < 1e-7\n"
        + "    assert abs(shape.bbox_mm[0] - 25.0) < 1e-7\n"
        + "    executor.checkpoint_fcstd(session, root / 'model.FCStd')\n"
        + "    step_path = root / 'model.step'\n"
        + "    if sys.platform == 'win32':\n"
        + "        from vibecad._file_compat import open_private_file\n"
        + "        descriptor, _ = open_private_file(step_path, exclusive=True)\n"
        + "        import os\n"
        + "        os.close(descriptor)\n"
        + "    else:\n"
        + "        step_path.touch(mode=0o600)\n"
        + "    _export_session_step(session=session, model_path=root / 'model.FCStd',\n"
        + "        step_path=step_path)\n"
        + "    assert step_path.stat().st_size > 0\n"
        + "    loaded = executor.load_fcstd(root / 'model.FCStd')\n"
        + "    assert _component_observations(loaded) == components\n"
        + "    assert _interference_observations(loaded) == interferences\n"
        + "    assert _shape_observation(loaded) == shape\n"
        + "    print('REAL_COMPONENT_PROGRAM_OK')\n"
        + "finally:\n"
        + "    if loaded is not None:\n"
        + "        loaded.close_document()\n"
        + "    session.close_document()\n"
    )
    result = _run_real_freecad_script(
        runtime_python,
        code,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "REAL_COMPONENT_PROGRAM_OK" in result.stdout


@pytest.mark.slow
def test_real_freecad_native_booleans_edit_reopen_and_export_step(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")
    code = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r})\n"
        + "from pathlib import Path\n"
        + "from vibecad.execution.candidate import ActiveCandidate, SessionBinding\n"
        + "from vibecad.execution.executor import (\n"
        + "    InProcessCadExecutor, _entity_observations, _export_session_step, "
        + "_same_import_observations, _shape_observation,\n"
        + ")\n"
        + "from vibecad.execution.revisions import LocalRevisionStore, ProjectHead\n"
        + "from vibecad.workflow.contracts import (\n"
        + "    AcceptanceSpec, ModelCommand, ModelProgram, ValueSource,\n"
        + ")\n"
        + f"root = Path({str(tmp_path)!r})\n"
        + f"project_id = {PROJECT_ID!r}\n"
        + f"base_revision = {BASE_REVISION!r}\n"
        + f"candidate_revision = {CANDIDATE_REVISION!r}\n"
        + "def command(identifier, operation, *, target=None, args=None, depends=()):\n"
        + "    return ModelCommand(\n"
        + "        id=identifier, op=operation, target=target or {}, args=args or {},\n"
        + "        depends_on=depends, preserve=(), source=ValueSource.MODEL,\n"
        + "    )\n"
        + "def ref(identifier):\n"
        + "    return {'command_id': identifier, 'slot': 'object'}\n"
        + "program = ModelProgram(\n"
        + "    task_id='task-real-native-booleans', base_revision=base_revision, operations=(\n"
        + "        command('cut_base', 'create_box',\n"
        + "            args={'length_mm': 20, 'width_mm': 20, 'height_mm': 20}),\n"
        + "        command('cut_tool', 'create_cylinder',\n"
        + "            args={'radius_mm': 3, 'height_mm': 20,\n"
        + "                'position_mm': [10, 10, 0], 'axis': 'z'},\n"
        + "            depends=('cut_base',)),\n"
        + "        command('cut', 'boolean_cut',\n"
        + "            target={'base': ref('cut_base'), 'tool': ref('cut_tool')},\n"
        + "            depends=('cut_base', 'cut_tool')),\n"
        + "        command('edit_cut_tool', 'modify_parameter',\n"
        + "            target={'object': ref('cut_tool')},\n"
        + "            args={'parameter': 'radius', 'value_mm': 4},\n"
        + "            depends=('cut_tool', 'cut')),\n"
        + "        command('fuse_base', 'create_box',\n"
        + "            args={'length_mm': 10, 'width_mm': 6, 'height_mm': 10,\n"
        + "                'position_mm': [40, 0, 0]}, depends=('edit_cut_tool',)),\n"
        + "        command('fuse_tool', 'create_box',\n"
        + "            args={'length_mm': 10, 'width_mm': 10, 'height_mm': 10,\n"
        + "                'position_mm': [45, 0, 0]}, depends=('fuse_base',)),\n"
        + "        command('fuse', 'boolean_fuse',\n"
        + "            target={'base': ref('fuse_base'), 'tool': ref('fuse_tool')},\n"
        + "            depends=('fuse_base', 'fuse_tool')),\n"
        + "        command('edit_fuse_base', 'modify_parameter',\n"
        + "            target={'object': ref('fuse_base')},\n"
        + "            args={'parameter': 'length', 'value_mm': 16},\n"
        + "            depends=('fuse_base', 'fuse')),\n"
        + "        command('rotate_fuse_base', 'rotate_part',\n"
        + "            target={'object': ref('fuse_base')},\n"
        + "            args={'axis': 'z', 'angle_deg': 90},\n"
        + "            depends=('edit_fuse_base', 'fuse')),\n"
        + "        command('common_base', 'create_box',\n"
        + "            args={'length_mm': 10, 'width_mm': 10, 'height_mm': 10,\n"
        + "                'position_mm': [80, 0, 0]}, depends=('rotate_fuse_base',)),\n"
        + "        command('common_tool', 'create_box',\n"
        + "            args={'length_mm': 10, 'width_mm': 10, 'height_mm': 10,\n"
        + "                'position_mm': [85, 0, 0]}, depends=('common_base',)),\n"
        + "        command('common', 'boolean_common',\n"
        + "            target={'base': ref('common_base'), 'tool': ref('common_tool')},\n"
        + "            depends=('common_base', 'common_tool')),\n"
        + "        command('edit_common_base', 'modify_parameter',\n"
        + "            target={'object': ref('common_base')},\n"
        + "            args={'parameter': 'length', 'value_mm': 12},\n"
        + "            depends=('common_base', 'common')),\n"
        + "        command('move_common_base', 'move_part',\n"
        + "            target={'object': ref('common_base')},\n"
        + "            args={'position_mm': [79, 0, 0]},\n"
        + "            depends=('edit_common_base', 'common')),\n"
        + "        command('contained_base', 'create_box',\n"
        + "            args={'length_mm': 20, 'width_mm': 20, 'height_mm': 20,\n"
        + "                'position_mm': [120, 0, 0]}, depends=('move_common_base',)),\n"
        + "        command('contained_tool', 'create_box',\n"
        + "            args={'length_mm': 5, 'width_mm': 5, 'height_mm': 5,\n"
        + "                'position_mm': [122, 2, 2]}, depends=('contained_base',)),\n"
        + "        command('contained_fuse', 'boolean_fuse',\n"
        + "            target={'base': ref('contained_base'), 'tool': ref('contained_tool')},\n"
        + "            depends=('contained_base', 'contained_tool')),\n"
        + "        command('move_contained_tool', 'move_part',\n"
        + "            target={'object': ref('contained_tool')},\n"
        + "            args={'position_mm': [130, 2, 2]},\n"
        + "            depends=('contained_tool', 'contained_fuse')),\n"
        + "        command('inspect', 'inspect_model', depends=('move_contained_tool',)),\n"
        + "    ), acceptance=AcceptanceSpec(id='accept-real-booleans', criteria=()),\n"
        + ")\n"
        + "store = object.__new__(LocalRevisionStore)\n"
        + "executor = InProcessCadExecutor(store=store)\n"
        + "session = executor.create_empty(revision_id=candidate_revision)\n"
        + "loaded = None\n"
        + "try:\n"
        + "    head = ProjectHead(project_id=project_id, generation=0,\n"
        + "        revision_id=base_revision, manifest_sha256='a' * 64)\n"
        + "    candidate = ActiveCandidate(project_id=project_id, base_head=head,\n"
        + "        binding=SessionBinding(project_id=project_id,\n"
        + "            revision_id=candidate_revision, session=session),\n"
        + "        model_path=root / 'model.FCStd', step_path=root / 'model.step')\n"
        + "    outcomes = executor.execute_program(\n"
        + "        program=executor.validate_program(program), candidate=candidate)\n"
        + "    assert len(outcomes) == 19 and all(item.result.ok for item in outcomes), "
        + "[item.result.to_mapping() for item in outcomes]\n"
        + "    observed = _entity_observations(session)\n"
        + "    by_operation = {item.provenance['operation_id']: item for item in observed}\n"
        + "    assert by_operation['cut'].object_type == 'Part::Cut'\n"
        + "    assert by_operation['fuse'].object_type == 'Part::Fuse'\n"
        + "    assert by_operation['common'].object_type == 'Part::Common'\n"
        + "    assert by_operation['cut'].semantic_role == 'feature'\n"
        + "    assert dict((p.name, p.value) for p in by_operation['cut'].parameters) == {\n"
        + "        'base_object_id': by_operation['cut_base'].object_id,\n"
        + "        'operation': 'cut',\n"
        + "        'tool_object_id': by_operation['cut_tool'].object_id,\n"
        + "    }\n"
        + "    assert dict((p.name, p.value) for p in by_operation['cut_tool'].parameters) "
        + "['radius'] == 4\n"
        + "    assert dict((p.name, p.value) for p in by_operation['fuse_base'].parameters) "
        + "['length'] == 16\n"
        + "    assert dict((p.name, p.value) for p in by_operation['common_base'].parameters) "
        + "['length'] == 12\n"
        + "    assert by_operation['fuse_base'].placement[3:] != (0, 0, 0, 1)\n"
        + "    assert by_operation['common_base'].placement[:3] == (79, 0, 0)\n"
        + "    assert by_operation['contained_tool'].placement[:3] == (130, 2, 2)\n"
        + "    assert abs(by_operation['contained_fuse'].volume_mm3 - 8000) <= 1e-7\n"
        + "    assert by_operation['cut'].volume_mm3 < 8000\n"
        + "    shape_before = _shape_observation(session)\n"
        + "    assert shape_before.solid_count == 4, shape_before.to_mapping()\n"
        + "    executor.checkpoint_fcstd(session, root / 'model.FCStd')\n"
        + "    step_path = root / 'model.step'\n"
        + "    if sys.platform == 'win32':\n"
        + "        from vibecad._file_compat import open_private_file\n"
        + "        descriptor, _ = open_private_file(step_path, exclusive=True)\n"
        + "        import os\n"
        + "        os.close(descriptor)\n"
        + "    else:\n"
        + "        step_path.touch(mode=0o600)\n"
        + "    _export_session_step(session=session, model_path=root / 'model.FCStd',\n"
        + "        step_path=step_path)\n"
        + "    assert step_path.stat().st_size > 0\n"
        + "    loaded = executor.load_fcstd(root / 'model.FCStd')\n"
        + "    loaded_observed = _entity_observations(loaded)\n"
        + "    assert [item.object_id for item in loaded_observed] == "
        + "[item.object_id for item in observed]\n"
        + "    assert all((item.object_id, item.feature_id, item.object_type, "
        + "item.semantic_role, dict(item.provenance)) == "
        + "(loaded_item.object_id, loaded_item.feature_id, loaded_item.object_type, "
        + "loaded_item.semantic_role, dict(loaded_item.provenance)) "
        + "for item, loaded_item in zip(observed, loaded_observed))\n"
        + "    assert all(item.parameters == loaded_item.parameters "
        + "for item, loaded_item in zip(observed, loaded_observed))\n"
        + "    if sys.platform == 'win32':\n"
        + "        assert _same_import_observations(loaded_observed, observed)\n"
        + "    else:\n"
        + "        assert all(item.placement == loaded_item.placement "
        + "for item, loaded_item in zip(observed, loaded_observed))\n"
        + "    assert all(item.valid_shape == loaded_item.valid_shape "
        + "and item.solid_count == loaded_item.solid_count "
        + "for item, loaded_item in zip(observed, loaded_observed))\n"
        + "    assert all(abs(item.volume_mm3 - loaded_item.volume_mm3) <= 1e-7 "
        + "and abs(item.area_mm2 - loaded_item.area_mm2) <= 1e-7 "
        + "and all(abs(a-b) <= 1e-7 for a,b in zip(item.bbox_mm, loaded_item.bbox_mm)) "
        + "and all(abs(a-b) <= 1e-7 for a,b in zip(item.center_of_mass_mm, "
        + "loaded_item.center_of_mass_mm)) for item, loaded_item in "
        + "zip(observed, loaded_observed))\n"
        + "    loaded_shape = _shape_observation(loaded)\n"
        + "    assert loaded_shape.target == shape_before.target\n"
        + "    assert loaded_shape.valid_shape == shape_before.valid_shape\n"
        + "    assert loaded_shape.solid_count == shape_before.solid_count\n"
        + "    assert abs(loaded_shape.volume_mm3 - shape_before.volume_mm3) <= 1e-7\n"
        + "    assert abs(loaded_shape.area_mm2 - shape_before.area_mm2) <= 1e-7\n"
        + "    assert all(abs(a-b) <= 1e-7 for a,b in "
        + "zip(loaded_shape.bbox_mm, shape_before.bbox_mm))\n"
        + "    assert all(abs(a-b) <= 1e-7 for a,b in "
        + "zip(loaded_shape.center_of_mass_mm, shape_before.center_of_mass_mm))\n"
        + "    print('REAL_NATIVE_BOOLEANS_OK')\n"
        + "finally:\n"
        + "    if loaded is not None:\n"
        + "        loaded.close_document()\n"
        + "    session.close_document()\n"
    )
    result = _run_real_freecad_script(
        runtime_python,
        code,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "REAL_NATIVE_BOOLEANS_OK" in result.stdout


@pytest.mark.slow
def test_real_freecad_component_boolean_owner_root_reopen_and_export_step(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")
    code = (
        f"import sys; sys.path.insert(0, {str(Path(__file__).parents[1] / 'src')!r})\n"
        + "from pathlib import Path\n"
        + "from vibecad.execution.candidate import ActiveCandidate, SessionBinding\n"
        + "from vibecad.execution.executor import (InProcessCadExecutor, "
        + "_component_observations, _entity_observations, _export_session_step)\n"
        + "from vibecad.execution.revisions import LocalRevisionStore, ProjectHead\n"
        + "from vibecad.workflow.contracts import (AcceptanceSpec, ModelCommand, "
        + "ModelProgram, ValueSource)\n"
        + f"root = Path({str(tmp_path)!r})\n"
        + f"project_id = {PROJECT_ID!r}\n"
        + f"base_revision = {BASE_REVISION!r}\n"
        + f"candidate_revision = {CANDIDATE_REVISION!r}\n"
        + "def command(identifier, operation, *, target=None, args=None, depends=()):\n"
        + "    return ModelCommand(id=identifier, op=operation, target=target or {}, "
        + "args=args or {}, depends_on=depends, preserve=(), source=ValueSource.MODEL)\n"
        + "def ref(identifier): return {'command_id': identifier, 'slot': 'object'}\n"
        + "program = ModelProgram(task_id='task-real-component-boolean', "
        + "base_revision=base_revision, operations=(\n"
        + "    command('component', 'create_component', args={'name': 'Bracket'}),\n"
        + "    command('base', 'create_box', target={'component': {'command_id': "
        + "'component', 'slot': 'component'}}, args={'length_mm': 20, 'width_mm': 10, "
        + "'height_mm': 10}, depends=('component',)),\n"
        + "    command('tool', 'create_box', target={'component': {'command_id': "
        + "'component', 'slot': 'component'}}, args={'length_mm': 10, 'width_mm': 10, "
        + "'height_mm': 10, 'position_mm': [15, 0, 0]}, depends=('base',)),\n"
        + "    command('fuse', 'boolean_fuse', target={'base': ref('base'), "
        + "'tool': ref('tool')}, depends=('base', 'tool')),\n"
        + "    command('second_tool', 'create_box', target={'component': {'command_id': "
        + "'component', 'slot': 'component'}}, args={'length_mm': 5, 'width_mm': 10, "
        + "'height_mm': 10, 'position_mm': [22, 0, 0]}, depends=('fuse',)),\n"
        + "    command('second_fuse', 'boolean_fuse', target={'base': ref('fuse'), "
        + "'tool': ref('second_tool')}, depends=('fuse', 'second_tool')),\n"
        + "    command('inspect', 'inspect_model', depends=('second_fuse',)),\n"
        + "), acceptance=AcceptanceSpec(id='accept-real-component-boolean', criteria=()))\n"
        + "executor = InProcessCadExecutor(store=object.__new__(LocalRevisionStore))\n"
        + "session = executor.create_empty(revision_id=candidate_revision); loaded = None\n"
        + "try:\n"
        + "    head = ProjectHead(project_id=project_id, generation=0, "
        + "revision_id=base_revision, manifest_sha256='a' * 64)\n"
        + "    candidate = ActiveCandidate(project_id=project_id, base_head=head, "
        + "binding=SessionBinding(project_id=project_id, revision_id=candidate_revision, "
        + "session=session), model_path=root/'model.FCStd', step_path=root/'model.step')\n"
        + "    outcomes = executor.execute_program(program=executor.validate_program(program), "
        + "candidate=candidate)\n"
        + "    assert len(outcomes) == 7 and all(item.result.ok for item in outcomes), "
        + "[item.result.to_mapping() for item in outcomes]\n"
        + "    observed = _entity_observations(session); components = "
        + "_component_observations(session)\n"
        + "    fuses = [item for item in observed if item.object_type == 'Part::Fuse']\n"
        + "    assert len(fuses) == 2 and all(item.semantic_role == 'feature' for item in fuses)\n"
        + "    assert len(components) == 1 and components[0].member_object_ids == "
        + "tuple(sorted(item.object_id for item in observed if item.object_type != 'App::Part'))\n"
        + "    executor.checkpoint_fcstd(session, root/'model.FCStd')\n"
        + "    step_path = root / 'model.step'\n"
        + "    if sys.platform == 'win32':\n"
        + "        from vibecad._file_compat import open_private_file\n"
        + "        descriptor, _ = open_private_file(step_path, exclusive=True)\n"
        + "        import os\n"
        + "        os.close(descriptor)\n"
        + "    else:\n"
        + "        step_path.touch(mode=0o600)\n"
        + "    _export_session_step(session=session, model_path=root/'model.FCStd', "
        + "step_path=step_path)\n"
        + "    loaded = executor.load_fcstd(root/'model.FCStd')\n"
        + "    assert [item.object_id for item in _entity_observations(loaded)] == "
        + "[item.object_id for item in observed]\n"
        + "    assert _component_observations(loaded)[0].member_object_ids == "
        + "components[0].member_object_ids\n"
        + "    print('REAL_COMPONENT_BOOLEAN_OK')\n"
        + "finally:\n"
        + "    if loaded is not None: loaded.close_document()\n"
        + "    session.close_document()\n"
    )
    result = _run_real_freecad_script(
        runtime_python,
        code,
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert "REAL_COMPONENT_BOOLEAN_OK" in result.stdout


def test_rotate_rejects_requested_quaternion_with_wrong_translation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)

    def rotate_with_extra_translation(
        session: _FakeSession,
        *,
        name: str,
        axis: str,
        angle: float,
    ) -> object:
        result = _fake_rotate_part(
            session,
            name=name,
            axis=axis,
            angle=angle,
        )
        obj = session.get_object(name)
        obj.Placement.Base.x += 1.0
        return result

    monkeypatch.setattr(
        executor_module,
        "_rotate_part",
        rotate_with_extra_translation,
    )
    program = ModelProgram(
        task_id="task-rotate-wrong-translation",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length_mm": 10, "width_mm": 20, "height_mm": 30},
            ),
            _command(
                "rotate",
                "rotate_part",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"axis": "z", "angle_deg": 90},
                depends_on=("box",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-rotate-translation", criteria=()),
    )
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(_FakeSession(), tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, False]


def test_rotate_uses_live_bound_box_center_for_partial_cylinder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_rotate_part", _fake_rotate_part)
    object_id = "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    operation_id = "import-partial-cylinder"
    cylinder = type("ManagedPartialCylinder", (), {})()
    cylinder.Name = "Cylinder"
    cylinder.TypeId = "Part::Cylinder"
    cylinder.Radius = 10.0
    cylinder.Height = 6.0
    cylinder.Angle = 180.0
    cylinder.Placement = _FakePlacement(0.0)
    cylinder.Shape = _FakeShape(
        volume=300 * math.pi,
        area=100 * math.pi + 60 * math.pi + 120,
        bbox=(20.0, 10.0, 6.0),
        center=(0.0, 40 / (3 * math.pi), 3.0),
        bbox_center=(0.0, 5.0, 3.0),
    )
    cylinder.VibeCADObjectId = object_id
    cylinder.VibeCADFeatureId = ""
    cylinder.VibeCADSemanticRole = "primitive"
    cylinder.VibeCADProvenance = '{"operation_id":"import-partial-cylinder","source":"imported"}'
    session = _FakeSession()
    session.doc.Objects = (cylinder,)
    selector = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "revision_id": BASE_REVISION,
        "entity_kind": "object",
        "object_id": object_id,
        "feature_id": None,
        "object_type": "Part::Cylinder",
        "semantic_role": "primitive",
        "provenance": {"source": "imported", "operation_id": operation_id},
        "expected_cardinality": 1,
    }
    program = ModelProgram(
        task_id="task-rotate-partial-cylinder",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "rotate",
                "rotate_part",
                target={"object": selector},
                args={"axis": "z", "angle_deg": 90},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-partial-cylinder", criteria=()),
    )

    executor = InProcessCadExecutor(store=_store())
    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True]
    rotated = outcomes[0].result.value["after"]
    assert rotated["placement"][:3] == pytest.approx([5.0, 5.0, 0.0])
    assert rotated["center_of_mass_mm"] == pytest.approx([5.0 - 4 * 10 / (3 * math.pi), 5.0, 3.0])


def test_managed_aggregate_compounds_every_identified_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    entities = (
        _FakeEntity("a", x=0.0, length=12.0),
        _FakeEntity("b", x=100.0, length=7.0),
    )
    session.doc.Objects = entities
    aggregate = _FakeShape(volume=11_400.0, bbox=(107.0, 20.0, 30.0))
    calls: list[list[object]] = []
    part = ModuleType("Part")

    def make_compound(shapes: list[object]) -> object:
        calls.append(shapes)
        return aggregate

    part.makeCompound = make_compound  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Part", part)

    observed = executor_module._managed_assembly_shape(session)

    assert observed is aggregate
    assert calls == [[entities[0].Shape, entities[1].Shape]]
    assert session.shape_calls == 0


def test_explicit_component_observation_uses_container_global_placement() -> None:
    global_shape = _FakeShape(
        volume=600.0,
        bbox=(10.0, 10.0, 6.0),
        center=(30.0, 5.0, 3.0),
    )

    class LocalShape:
        def transformed(self, matrix):
            assert matrix == "component-matrix"
            return global_shape

    placement = _FakePlacement(25.0)
    placement.toMatrix = lambda: "component-matrix"  # type: ignore[attr-defined]
    container = SimpleNamespace(Name="VibePart", Placement=placement)
    provenance = SimpleNamespace(
        to_mapping=lambda: {"source": "model", "operation_id": "component-a"}
    )
    component_identity = SimpleNamespace(
        object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        object_type="App::Part",
        provenance=provenance,
    )
    member_identity = SimpleNamespace(object_id="object_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")

    class ComponentSession:
        assembly = _FakeShape(volume=600.0)

        @staticmethod
        def list_component_identity_records():
            return (
                (
                    "Housing",
                    container,
                    component_identity,
                    ((object(), member_identity),),
                ),
            )

        @staticmethod
        def get_result_shape(part_name):
            assert part_name == "Housing"
            return LocalShape()

        @classmethod
        def get_assembly_shape(cls):
            return cls.assembly

    observations = executor_module._component_observations(ComponentSession())

    assert len(observations) == 1
    assert observations[0].component_id == component_identity.object_id
    assert observations[0].member_object_ids == (member_identity.object_id,)
    assert observations[0].placement == (25.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    assert observations[0].volume_mm3 == 600.0
    assert observations[0].center_of_mass_mm == (30.0, 5.0, 3.0)
    assert executor_module._managed_assembly_shape(ComponentSession()) is ComponentSession.assembly


def test_component_entity_observation_ignores_app_part_aggregate_shape() -> None:
    container = SimpleNamespace(
        TypeId="App::Part",
        Placement=_FakePlacement(0.0),
        Shape=object(),
    )
    identity = SimpleNamespace(
        object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        feature_id=None,
        object_type="App::Part",
        semantic_role=SimpleNamespace(value="part"),
        provenance=SimpleNamespace(
            to_mapping=lambda: {"source": "model", "operation_id": "component-a"}
        ),
    )

    observation = executor_module._entity_observation(container, identity)

    assert observation.object_type == "App::Part"
    assert observation.placement == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    assert observation.volume_mm3 is None
    assert observation.area_mm2 is None
    assert observation.bbox_mm is None
    assert observation.center_of_mass_mm is None
    assert observation.valid_shape is None
    assert observation.solid_count is None


def test_compound_observation_derives_volume_weighted_center_of_mass() -> None:
    class Solid:
        def __init__(self, volume: float, center: tuple[float, float, float]) -> None:
            self.Volume = volume
            self.CenterOfMass = _FakeVector(*center)

    class CompoundWithoutCenter:
        Volume = 30.0
        Area = 40.0
        BoundBox = _FakeBoundBox(30.0, 2.0, 2.0)
        Solids = (
            Solid(10.0, (0.0, 0.0, 0.0)),
            Solid(20.0, (30.0, 3.0, 6.0)),
        )

        @staticmethod
        def isValid() -> bool:  # noqa: N802 - FreeCAD API spelling
            return True

    class CompoundSession:
        doc = SimpleNamespace(Objects=())

        @staticmethod
        def get_assembly_shape() -> object:
            return CompoundWithoutCenter()

    observation = executor_module._shape_observation(CompoundSession())
    entity_geometry = executor_module._entity_geometry(CompoundWithoutCenter())

    assert observation.center_of_mass_mm == (20.0, 2.0, 4.0)
    assert observation.solid_count == 2
    assert entity_geometry["center_of_mass_mm"] == (20.0, 2.0, 4.0)
    assert entity_geometry["solid_count"] == 2


def test_zero_volume_valid_shape_uses_bound_box_center_when_mass_is_undefined() -> None:
    class WireShape:
        Volume = 0.0
        Area = 0.0
        Solids = ()
        BoundBox = SimpleNamespace(
            XMin=-5.0,
            XMax=15.0,
            YMin=-2.0,
            YMax=4.0,
            ZMin=0.0,
            ZMax=0.0,
            XLength=20.0,
            YLength=6.0,
            ZLength=0.0,
        )

        @property
        def CenterOfMass(self):  # noqa: N802 - native spelling
            raise RuntimeError("undefined for a wire")

        def isNull(self) -> bool:  # noqa: N802 - native spelling
            return False

        def isValid(self) -> bool:  # noqa: N802 - native spelling
            return True

    assert executor_module._entity_geometry(WireShape())["center_of_mass_mm"] == (
        5.0,
        1.0,
        0.0,
    )


def test_derived_geometry_tolerance_accepts_roundoff_but_rejects_material_error() -> None:
    reference = 11_650_984.713_924_531
    assert executor_module._same_geometry_number(reference, reference + 1.862_645e-9)
    assert not executor_module._same_geometry_number(reference, reference + 0.1)

    shape = executor_module.ShapeObservation(
        target="body",
        volume_mm3=reference,
        area_mm2=100.0,
        bbox_mm=(10.0, 20.0, 30.0),
        center_of_mass_mm=(5.0, 10.0, 15.0),
        valid_shape=True,
        solid_count=1,
    )
    roundoff = dataclasses.replace(
        shape,
        volume_mm3=reference + 1.862_645e-9,
        center_of_mass_mm=(5.0 + 1e-12, 10.0, 15.0),
    )
    material_error = dataclasses.replace(shape, volume_mm3=reference + 0.1)

    assert executor_module._same_shape_observation(shape, roundoff)
    assert not executor_module._same_shape_observation(shape, material_error)


def test_managed_create_attaches_fresh_typed_identity_before_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        executor_module,
        "_add_box",
        _fake_add_box,
    )
    program = ModelProgram(
        task_id="task-managed-create",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length_mm": 10, "width_mm": 20, "height_mm": 30},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-managed-create", criteria=()),
    )
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(session, tmp_path),
    )

    assert outcomes[0].result.ok is True
    assert len(session.attached_identities) == 1
    obj, identity = session.attached_identities[0]
    assert obj is session.identity_object
    assert identity.object_id.startswith("object_")
    assert identity.feature_id.startswith("feature_")
    assert identity.object_type == "Part::Box"
    assert identity.semantic_role.value == "primitive"
    assert identity.provenance.to_mapping() == {
        "source": "model",
        "operation_id": "box",
    }


def test_repeated_create_handler_keeps_each_authenticated_command_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(executor_module, "_add_box", _fake_add_box)
    program = ModelProgram(
        task_id="task-repeated-create",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box_a",
                "create_box",
                args={"length_mm": 2, "width_mm": 3, "height_mm": 4},
            ),
            _command(
                "box_b",
                "create_box",
                args={
                    "length_mm": 5,
                    "width_mm": 6,
                    "height_mm": 7,
                    "position_mm": (20, 0, 0),
                },
                depends_on=("box_a",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-repeated-create", criteria=()),
    )
    session = _FakeSession()
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(session, tmp_path),
    )

    assert [outcome.result.ok for outcome in outcomes] == [True, True]
    assert [identity.provenance.operation_id for _, identity in session.attached_identities] == [
        "box_a",
        "box_b",
    ]
    assert [
        outcome.result.value["object_id"]  # type: ignore[index]
        for outcome in outcomes
    ] == [identity.object_id for _, identity in session.attached_identities]


def test_managed_create_fails_closed_when_identity_authority_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class UnmanagedSession(_FakeSession):
        attach_object_identity = None

    monkeypatch.setattr(
        executor_module,
        "_add_box",
        _fake_add_box,
    )
    executor = InProcessCadExecutor(store=_store())
    program = ModelProgram(
        task_id="task-unmanaged-create",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length_mm": 10, "width_mm": 20, "height_mm": 30},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-unmanaged-create", criteria=()),
    )

    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(UnmanagedSession(), tmp_path),
    )

    assert len(outcomes) == 1
    assert outcomes[0].result.ok is False
    assert "identity" not in json.dumps(outcomes[0].result.to_mapping()).lower()


def test_managed_create_rejects_callable_noop_identity_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class NoOpIdentitySession(_FakeSession):
        def attach_object_identity(self, obj: object, identity: object) -> object:
            del obj
            return identity

    monkeypatch.setattr(
        executor_module,
        "_add_box",
        _fake_add_box,
    )
    executor = InProcessCadExecutor(store=_store())
    program = ModelProgram(
        task_id="task-noop-identity",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length_mm": 10, "width_mm": 20, "height_mm": 30},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-noop-identity", criteria=()),
    )

    outcomes = executor.execute_program(
        program=executor.validate_program(program),
        candidate=_active(NoOpIdentitySession(), tmp_path),
    )

    assert outcomes[0].result.ok is False


def test_execute_preflights_all_fixed_handlers_before_first_cad_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = 0

    def forbidden(*args: object, **kwargs: object) -> object:
        nonlocal calls
        del args, kwargs
        calls += 1
        return {"ok": True}

    monkeypatch.setattr(executor_module, "_add_box", forbidden)
    monkeypatch.setattr(executor_module, "_add_cylinder", forbidden)
    monkeypatch.setattr(executor_module, "_modify_part", forbidden)
    monkeypatch.setattr(executor_module, "_move_part", forbidden)
    monkeypatch.setattr(executor_module, "_rotate_part", None)
    executor = InProcessCadExecutor(store=_store())

    with pytest.raises(ExecutorError) as caught:
        executor.execute_program(
            program=executor.validate_program(_program()),
            candidate=_active(_FakeSession(), tmp_path),
        )

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert calls == 0


def test_execute_program_stops_on_first_failure_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = {"box": 0}

    def box(session: object, **kwargs: object) -> object:
        del session, kwargs
        calls["box"] += 1
        raise RuntimeError("secret-cad-detail")

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("execution did not stop")

    monkeypatch.setattr(executor_module, "_add_box", box)
    monkeypatch.setattr(executor_module, "_add_cylinder", forbidden)
    monkeypatch.setattr(executor_module, "_modify_part", forbidden)
    monkeypatch.setattr(executor_module, "_move_part", forbidden)
    monkeypatch.setattr(executor_module, "_rotate_part", forbidden)
    executor = InProcessCadExecutor(store=_store())

    outcomes = executor.execute_program(
        program=executor.validate_program(_program()),
        candidate=_active(_FakeSession(), tmp_path),
    )

    assert calls == {"box": 1}
    assert len(outcomes) == 1
    assert outcomes[-1].result.ok is False
    assert "secret" not in json.dumps(outcomes[-1].result.to_mapping())


def test_created_entity_preservation_is_enforced_between_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        executor_module,
        "_add_box",
        _fake_add_box,
    )

    monkeypatch.setattr(executor_module, "_modify_part", _fake_modify_part)
    program = ModelProgram(
        task_id="task-command-preservation",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "box",
                "create_box",
                args={"length_mm": 10, "width_mm": 20, "height_mm": 30},
            ),
            _command(
                "modify",
                "modify_parameter",
                target={"object": {"command_id": "box", "slot": "object"}},
                args={"parameter": "length", "value_mm": 12},
                depends_on=("box",),
                preserve=("length",),
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-command-preservation", criteria=()),
    )

    outcomes = InProcessCadExecutor(store=_store()).execute_program(
        program=validate_model_program(program),
        candidate=_active(_FakeSession(), tmp_path),
    )

    assert tuple(outcome.result.ok for outcome in outcomes) == (True, False)


@pytest.mark.parametrize("candidate", [object(), None])
def test_execute_rejects_non_active_candidate_before_handlers(candidate: object) -> None:
    executor = InProcessCadExecutor(store=_store())
    with pytest.raises(ExecutorError) as caught:
        executor.execute_program(
            program=executor.validate_program(_program()),
            candidate=candidate,  # type: ignore[arg-type]
        )
    assert caught.value.code is ExecutorErrorCode.INVALID_CANDIDATE


def test_execute_rejects_selector_project_before_session_traversal(tmp_path: Path) -> None:
    class SessionTraversalBomb:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"session traversal occurred: {name}")

    selector = {
        "schema_version": 1,
        "project_id": "project_ffffffffffffffffffffffffffffffff",
        "revision_id": BASE_REVISION,
        "entity_kind": "feature",
        "object_id": "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "feature_id": "feature_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "object_type": "Part::Box",
        "semantic_role": "primitive",
        "provenance": {"source": "model", "operation_id": "box"},
        "expected_cardinality": 1,
    }
    program = ModelProgram(
        task_id="task-wrong-project-selector",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "select",
                "modify_parameter",
                target={"object": selector},
                args={"parameter": "length", "value_mm": 12},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-wrong-project-selector", criteria=()),
    )
    validated = validate_model_program(program)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).execute_program(
            program=validated,
            candidate=_active(SessionTraversalBomb(), tmp_path),
        )

    assert caught.value.code is ExecutorErrorCode.INVALID_CANDIDATE


def test_execute_rejects_custom_registry_authority_before_session_traversal(
    tmp_path: Path,
) -> None:
    class SessionTraversalBomb:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"session traversal occurred: {name}")

    registry = OperationRegistry(
        (
            OperationMetadata(
                operation="modify_parameter",
                handler_name="modify_parameter",
                risk_class=RiskClass.READ_ONLY,
                evidence_required=False,
                target_fields=(FieldMetadata("object", "target", ValueShape.OBJECT_ID),),
                argument_fields=(
                    FieldMetadata(
                        "parameter",
                        "parameter",
                        ValueShape.ENUM,
                        enum_values=("length",),
                    ),
                    FieldMetadata("value_mm", "value", ValueShape.POSITIVE_NUMBER),
                ),
            ),
        )
    )
    program = ModelProgram(
        task_id="task-custom-registry-bypass",
        base_revision=BASE_REVISION,
        operations=(
            _command(
                "modify",
                "modify_parameter",
                target={"object": "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                args={"parameter": "length", "value_mm": 12},
            ),
        ),
        acceptance=AcceptanceSpec(id="acceptance-custom-registry-bypass", criteria=()),
    )
    validated = validate_model_program(program, registry=registry)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).execute_program(
            program=validated,
            candidate=_active(SessionTraversalBomb(), tmp_path),
        )

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT


def test_controlled_step_export_uses_only_store_derived_exact_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shape = _FakeShape()
    candidate = _checkpointed(_FakeSession(shape), tmp_path)
    _prepare_empty_private_artifact(candidate.step_path)

    def candidate_artifact_path(
        self: LocalRevisionStore,
        project_id: str,
        revision_id: str,
        artifact_format: str,
        lease: ProjectWriteLease,
    ) -> Path:
        del self
        assert (project_id, revision_id, artifact_format) == (
            PROJECT_ID,
            CANDIDATE_REVISION,
            "step",
        )
        assert lease.project_id == PROJECT_ID
        return candidate.step_path

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", candidate_artifact_path)
    InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())

    assert shape.export_calls == [str(candidate.step_path)]
    assert candidate.step_path.read_bytes().startswith(b"ISO-10303-21;")


def test_step_export_rejects_forged_path_before_shape_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    candidate = _checkpointed(session, tmp_path)

    def wrong_path(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        return tmp_path / "other.step"

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", wrong_path)
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())
    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert session.shape_calls == 0
    assert session.shape.export_calls == []


@pytest.mark.parametrize("entry_kind", ["symlink", "directory", "hardlink"])
def test_step_export_never_overwrites_unsafe_existing_entry(
    entry_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    candidate = _checkpointed(session, tmp_path)
    outside = tmp_path / "outside.step"
    outside.write_bytes(b"outside-sentinel")
    if entry_kind == "symlink":
        candidate.step_path.symlink_to(outside)
    elif entry_kind == "directory":
        candidate.step_path.mkdir()
    else:
        os.link(outside, candidate.step_path)
    monkeypatch.setattr(
        LocalRevisionStore,
        "candidate_artifact_path",
        lambda *args, **kwargs: candidate.step_path,
    )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert session.shape_calls == 0
    assert session.shape.export_calls == []
    assert outside.read_bytes() == b"outside-sentinel"


def test_wrong_candidate_stage_is_rejected_before_store_or_cad(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    active = _active(session, tmp_path)
    checkpointed = _checkpointed(session, tmp_path)
    sealed = _sealed(session, model_path, step_path)

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("store must not be touched for the wrong stage")

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", forbidden)
    monkeypatch.setattr(LocalRevisionStore, "load_revision", forbidden)
    executor = InProcessCadExecutor(store=_store())

    for wrong_export in (active, sealed):
        with pytest.raises(ExecutorError) as caught:
            executor.export_step(candidate=wrong_export, lease=_lease())  # type: ignore[arg-type]
        assert caught.value.code is ExecutorErrorCode.INVALID_CANDIDATE
    for wrong_collect in (active, checkpointed):
        with pytest.raises(ExecutorError) as caught:
            executor.collect_evidence(candidate=wrong_collect)  # type: ignore[arg-type]
        assert caught.value.code is ExecutorErrorCode.INVALID_CANDIDATE
    assert session.shape_calls == 0


@pytest.mark.parametrize(
    "lease",
    [_lease(project_id="project_22222222222222222222222222222222"), _lease(released=True)],
)
def test_step_export_rejects_wrong_or_released_lease_before_store(
    lease: ProjectWriteLease,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        LocalRevisionStore,
        "candidate_artifact_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("store touched")),
    )
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(
            candidate=_checkpointed(_FakeSession(), tmp_path),
            lease=lease,
        )
    assert caught.value.code is ExecutorErrorCode.INVALID_LEASE


def test_store_rejected_lease_maps_to_invalid_lease_before_shape_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = _FakeSession()
    candidate = _checkpointed(session, tmp_path)

    def reject_lease(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        raise RevisionStoreError(RevisionStoreErrorCode.INVALID_LEASE)

    monkeypatch.setattr(LocalRevisionStore, "candidate_artifact_path", reject_lease)
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())

    assert caught.value.code is ExecutorErrorCode.INVALID_LEASE
    assert session.shape_calls == 0
    assert session.shape.export_calls == []


def test_step_export_failure_is_redacted_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shape = _FakeShape(export_error=RuntimeError("secret-export-path"))
    candidate = _checkpointed(_FakeSession(shape), tmp_path)
    _prepare_empty_private_artifact(candidate.step_path)
    monkeypatch.setattr(
        LocalRevisionStore,
        "candidate_artifact_path",
        lambda *args, **kwargs: candidate.step_path,
    )
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).export_step(candidate=candidate, lease=_lease())
    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert shape.export_calls == [str(candidate.step_path)]
    assert "secret" not in str(caught.value)


def test_collect_evidence_is_geometry_owned_and_manifest_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    calls = _install_store_paths(monkeypatch, sealed, model_path, step_path)

    evidence = InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert type(evidence) is CandidateEvidence
    assert evidence.snapshot.candidate_revision == CANDIDATE_REVISION
    assert len(evidence.snapshot.shapes) == 1
    shape = evidence.snapshot.shapes[0]
    assert shape.target == "body"
    assert shape.volume_mm3 == 7200.0
    assert shape.area_mm2 == 2400.0
    assert shape.bbox_mm == (12.0, 20.0, 30.0)
    assert shape.center_of_mass_mm == (6.0, 10.0, 15.0)
    assert shape.valid_shape is True
    assert shape.solid_count == 1
    assert tuple(item.target for item in evidence.snapshot.artifacts) == ("export", "model")
    assert tuple(item.format for item in evidence.snapshot.artifacts) == ("step", "fcstd")
    assert all(item.exists is True for item in evidence.snapshot.artifacts)
    assert all(item.non_empty is True for item in evidence.snapshot.artifacts)
    assert tuple(item.id for item in evidence.artifacts) == (MODEL_ID, STEP_ID)
    assert tuple(item.name for item in evidence.artifacts) == ("model.FCStd", "model.step")
    assert tuple(item.format for item in evidence.artifacts) == ("fcstd", "step")
    assert tuple(item.sha256 for item in evidence.artifacts) == (
        sealed.revision.model.sha256,
        sealed.revision.artifacts[0].sha256,
    )
    assert tuple(item.size_bytes for item in evidence.artifacts) == (
        sealed.revision.model.size_bytes,
        sealed.revision.artifacts[0].size_bytes,
    )
    assert all(item.candidate_revision == CANDIDATE_REVISION for item in evidence.artifacts)
    assert calls == [
        "load",
        "model_path",
        "step_path",
        "load",
        "base_load",
        "base_model_path",
        "load",
        "base_load",
    ]


def test_collect_evidence_is_per_object_and_reload_bound(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    entities = (
        _FakeEntity("a", x=0.0, length=12.0),
        _FakeEntity("b", x=100.0, length=7.0),
    )
    live = _FakeSession()
    live.doc.Objects = entities
    probe = _FakeSession()
    probe.doc.Objects = entities
    base_probe = _FakeSession()
    base_probe.doc.Objects = entities
    sealed = _sealed(live, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    probes = iter((probe, base_probe))
    monkeypatch.setattr(executor_module, "_Session", lambda: next(probes))
    monkeypatch.setattr(
        executor_module,
        "_managed_assembly_shape",
        lambda session: session.shape,
    )

    evidence = InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert probe.loaded == [model_path]
    assert probe.close_calls == 1
    assert tuple(item.object_id for item in evidence.snapshot.entities) == (
        "object_" + "a" * 32,
        "object_" + "b" * 32,
    )


def test_collect_evidence_compares_base_and_sealed_entities_for_preservation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    after = (
        _FakeEntity("a", x=0.0, length=12.0),
        _FakeEntity("b", x=100.0, length=7.0),
    )
    before = (
        _FakeEntity("a", x=0.0, length=10.0),
        _FakeEntity("b", x=100.0, length=7.0),
    )
    live = _FakeSession()
    live.doc.Objects = after
    candidate_probe = _FakeSession()
    candidate_probe.doc.Objects = after
    base_probe = _FakeSession()
    base_probe.doc.Objects = before
    sealed = _sealed(live, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    probes = iter((candidate_probe, base_probe))
    monkeypatch.setattr(executor_module, "_Session", lambda: next(probes))
    monkeypatch.setattr(
        executor_module,
        "_managed_assembly_shape",
        lambda session: session.shape,
    )

    snapshot = InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed).snapshot

    by_target = {item.target: item for item in snapshot.preservations}
    assert set(by_target) == {
        "object_" + "a" * 32,
        "object_" + "b" * 32,
        "feature_" + "a" * 32,
        "feature_" + "b" * 32,
    }
    for target in ("object_" + "a" * 32, "feature_" + "a" * 32):
        assert by_target[target].preserved is False
        assert by_target[target].changed_fields == ("parameters.length",)
    for target in ("object_" + "b" * 32, "feature_" + "b" * 32):
        assert by_target[target].preserved is True
        assert by_target[target].changed_fields == ()
    assert candidate_probe.close_calls == 1
    assert base_probe.close_calls == 1


def test_collect_evidence_rejects_live_vs_reloaded_entity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    live = _FakeSession()
    live.doc.Objects = (_FakeEntity("a", x=0.0, length=12.0),)
    probe = _FakeSession()
    probe.doc.Objects = (_FakeEntity("a", x=0.0, length=11.0),)
    sealed = _sealed(live, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    monkeypatch.setattr(executor_module, "_Session", lambda: probe)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert probe.close_calls == 1


def test_collect_evidence_rejects_partially_managed_modelable_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class PartialIdentitySession(_FakeSession):
        def list_object_identities(self) -> tuple[object, ...]:
            return ()

    model_path, step_path = _write_artifacts(tmp_path)
    live = PartialIdentitySession()
    live.doc.Objects = (type("UntaggedBox", (), {"TypeId": "Part::Box"})(),)
    sealed = _sealed(live, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE


def test_collect_evidence_rechecks_fcstd_after_freecad_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class MutatingProbe(_FakeSession):
        def load_document(self, path: Path) -> object:
            loaded = super().load_document(path)
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Document.xml", "<Document changed='true' />")
            return loaded

    model_path, step_path = _write_artifacts(tmp_path)
    live = _FakeSession()
    probe = MutatingProbe()
    sealed = _sealed(live, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    monkeypatch.setattr(executor_module, "_Session", lambda: probe)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert probe.loaded == [model_path]
    assert probe.close_calls == 1


def test_collect_evidence_rechecks_step_after_observation_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)

    class StepMutatingProbe(_FakeSession):
        def load_document(self, path: Path) -> object:
            loaded = super().load_document(path)
            step_path.write_bytes(b"ISO-10303-21;\nDATA;\n#2=B;\nENDSEC;\nEND-ISO-10303-21;\n")
            return loaded

    live = _FakeSession()
    probe = StepMutatingProbe()
    sealed = _sealed(live, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    monkeypatch.setattr(executor_module, "_Session", lambda: probe)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert probe.close_calls == 1


def test_geometry_observation_copies_independent_non_box_facts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    shape = _FakeShape()
    shape.Volume = 321.25
    shape.Area = 777.5
    shape.BoundBox = _FakeBoundBox()
    shape.BoundBox.XLength = 3.5
    shape.BoundBox.YLength = 40.25
    shape.BoundBox.ZLength = 9.75
    shape.CenterOfMass = _FakeVector(-4.5, 8.25, 112.0)
    shape.Solids = (object(), object())
    shape.isValid = lambda: False  # type: ignore[method-assign]
    sealed = _sealed(_FakeSession(shape), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    observed = (
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed).snapshot.shapes[0]
    )

    assert observed.volume_mm3 == 321.25
    assert observed.area_mm2 == 777.5
    assert observed.bbox_mm == (3.5, 40.25, 9.75)
    assert observed.center_of_mass_mm == (-4.5, 8.25, 112.0)
    assert observed.valid_shape is False
    assert observed.solid_count == 2


def test_untrusted_inspect_result_cannot_supply_acceptance_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    monkeypatch.setattr(
        executor_module,
        "_add_box",
        _fake_add_box,
    )
    monkeypatch.setattr(executor_module, "_modify_part", _fake_modify_part)
    executor = InProcessCadExecutor(store=_store())
    outcomes = executor.execute_program(
        program=executor.validate_program(_program()),
        candidate=_active(session, tmp_path),
    )
    assert outcomes[-1].result.ok is True

    evidence = executor.collect_evidence(candidate=sealed)

    observed = evidence.snapshot.shapes[0]
    assert observed.volume_mm3 == 7200.0
    assert observed.bbox_mm == (12.0, 20.0, 30.0)
    assert observed.valid_shape is True
    assert observed.solid_count == 1


@pytest.mark.parametrize("boundary", ["load", "model_path", "shape"])
def test_evidence_boundary_exceptions_are_fixed_and_redacted(
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)

    class SecretSession(_FakeSession):
        def get_assembly_shape(self) -> _FakeShape:
            self.shape_calls += 1
            raise RuntimeError("secret-geometry-detail")

    session = SecretSession() if boundary == "shape" else _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    if boundary == "load":
        monkeypatch.setattr(
            LocalRevisionStore,
            "load_revision",
            lambda *args: (_ for _ in ()).throw(RuntimeError("secret-store-record")),
        )
    else:
        _install_store_paths(monkeypatch, sealed, model_path, step_path)
        if boundary == "model_path":
            monkeypatch.setattr(
                LocalRevisionStore,
                "revision_model_path",
                lambda *args: (_ for _ in ()).throw(RuntimeError("secret-store-path")),
            )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    expected = (
        ExecutorErrorCode.CAD_FAILURE
        if boundary == "shape"
        else ExecutorErrorCode.INTEGRITY_FAILURE
    )
    assert caught.value.code is expected
    assert "secret" not in str(caught.value)
    assert "secret" not in json.dumps(caught.value.to_mapping())


@pytest.mark.parametrize(
    ("corrupt", "expected"),
    [
        ("fcstd", ExecutorErrorCode.ARTIFACT_FAILURE),
        ("step", ExecutorErrorCode.ARTIFACT_FAILURE),
        ("hash", ExecutorErrorCode.INTEGRITY_FAILURE),
    ],
)
def test_collect_evidence_rejects_format_or_hash_corruption(
    corrupt: str,
    expected: ExecutorErrorCode,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    if corrupt == "fcstd":
        model_path.write_bytes(b"not-a-FreeCAD-document")
    elif corrupt == "step":
        step_path.write_bytes(b"not-a-step-file")
    else:
        original = step_path.read_bytes()
        mutated = original.replace(b"#1=A;", b"#1=B;")
        assert len(mutated) == len(original)
        step_path.write_bytes(mutated)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    assert caught.value.code is expected


@pytest.mark.parametrize("bad_format", ["fcstd_without_document", "step_without_trailer"])
def test_format_detection_is_not_magic_or_prefix_only(
    bad_format: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    if bad_format == "fcstd_without_document":
        with zipfile.ZipFile(model_path, "w") as archive:
            archive.writestr("Other.xml", "<Other />")
    else:
        step_path.write_bytes(b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\n")
    sealed = _sealed(_FakeSession(), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE


def test_actual_artifact_mutation_after_manifest_read_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    monkeypatch.setattr(LocalRevisionStore, "load_revision", lambda *args: sealed.revision)
    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", lambda *args: model_path)

    def mutate_then_return(*args: object) -> Path:
        del args
        original = step_path.read_bytes()
        mutated = original.replace(b"#1=A;", b"#1=B;")
        assert len(mutated) == len(original)
        step_path.write_bytes(mutated)
        return step_path

    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", mutate_then_return)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE


def test_first_durable_revision_mismatch_rejects_before_paths_or_geometry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    session = _FakeSession()
    sealed = _sealed(session, model_path, step_path)
    mismatched = RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256="b" * 64,
        model=sealed.revision.model,
        artifacts=sealed.revision.artifacts,
    )
    monkeypatch.setattr(LocalRevisionStore, "load_revision", lambda *args: mismatched)

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("paths must not be resolved after a manifest mismatch")

    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", forbidden)
    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", forbidden)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert session.shape_calls == 0


def test_collect_evidence_detects_revision_mutation_between_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    mutated = RevisionRef(
        id=CANDIDATE_REVISION,
        project_id=PROJECT_ID,
        base_revision=BASE_REVISION,
        manifest_sha256="b" * 64,
        model=sealed.revision.model,
        artifacts=sealed.revision.artifacts,
    )
    values = iter((sealed.revision, mutated))
    monkeypatch.setattr(LocalRevisionStore, "load_revision", lambda *args: next(values))
    monkeypatch.setattr(LocalRevisionStore, "revision_model_path", lambda *args: model_path)
    monkeypatch.setattr(LocalRevisionStore, "revision_artifact_path", lambda *args: step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("field", ["Volume", "Area", "BoundBox", "CenterOfMass", "Solids"])
def test_collect_evidence_rejects_malformed_or_nonfinite_shape_facts(
    field: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    shape = _FakeShape()
    if field in {"Volume", "Area"}:
        setattr(shape, field, math.inf)
    elif field == "BoundBox":
        shape.BoundBox = object()
    elif field == "CenterOfMass":
        shape.CenterOfMass = _FakeVector(math.nan, 0, 0)
    else:
        shape.Solids = object()
    sealed = _sealed(_FakeSession(shape), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    assert caught.value.code is ExecutorErrorCode.CAD_FAILURE


def test_candidate_evidence_is_immutable_and_validates_exact_types(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model_path, step_path = _write_artifacts(tmp_path)
    sealed = _sealed(_FakeSession(), model_path, step_path)
    _install_store_paths(monkeypatch, sealed, model_path, step_path)
    evidence = InProcessCadExecutor(store=_store()).collect_evidence(candidate=sealed)
    with pytest.raises((AttributeError, TypeError)):
        evidence.artifacts = ()  # type: ignore[misc]
    with pytest.raises(ValueError):
        CandidateEvidence(snapshot=object(), artifacts=())  # type: ignore[arg-type]
    wrong_revision = TaskArtifactRef(
        id=MODEL_ID,
        name="model.FCStd",
        format="fcstd",
        sha256=evidence.artifacts[0].sha256,
        size_bytes=evidence.artifacts[0].size_bytes,
        candidate_revision="revision_22222222222222222222222222222222",
    )
    with pytest.raises(ValueError):
        CandidateEvidence(snapshot=evidence.snapshot, artifacts=(wrong_revision,))


def test_executor_has_no_configurable_handler_or_path_surface() -> None:
    executor = InProcessCadExecutor(store=_store())
    assert not hasattr(executor, "handlers")
    assert not hasattr(executor, "registry")
    assert not hasattr(executor, "output_dir")
    assert not hasattr(executor, "retry")
