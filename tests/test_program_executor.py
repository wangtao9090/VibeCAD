"""Trusted in-process CAD execution and observation boundary tests."""

from __future__ import annotations

import json
import math
import os
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import vibecad.execution.executor as executor_module
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
from vibecad.execution.selectors import index_entity_identities
from vibecad.workflow.contracts import AcceptanceSpec, ModelCommand, ModelProgram, ValueSource
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.lease import ProjectWriteLease
from vibecad.workflow.program import ValidatedProgram, validate_model_program
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
        bbox: tuple[float, float, float] = (12.0, 20.0, 30.0),
        center: tuple[float, float, float] = (6.0, 10.0, 15.0),
        bbox_center: tuple[float, float, float] | None = None,
    ) -> None:
        self.Volume = volume
        self.Area = area
        self.BoundBox = _FakeBoundBox(*bbox, center=bbox_center or center)
        self.CenterOfMass = _FakeVector(*center)
        self.Solids = (object(),)
        self.export_error = export_error
        self.export_calls: list[str] = []

    def isValid(self) -> bool:
        return True

    def exportStep(self, path: str) -> None:  # noqa: N802 - FreeCAD API spelling
        self.export_calls.append(path)
        if self.export_error is not None:
            raise self.export_error
        Path(path).write_bytes(b"ISO-10303-21;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n")


class _FakeDocument:
    def __init__(self) -> None:
        self.recompute_calls = 0
        self.save_calls: list[str] = []
        self.Objects: tuple[object, ...] = ()

    def recompute(self) -> None:
        self.recompute_calls += 1

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
        self.attached_identities: list[tuple[object, object]] = []
        self.result_object: object | None = None

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

    def set_result_object(self, obj: object) -> None:
        self.result_object = obj


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


def _fake_add_box(
    session: _FakeSession,
    *,
    length: float,
    width: float,
    height: float,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
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
    session.doc.Objects = (*session.doc.Objects, obj)
    session.set_result_object(obj)
    return object()


def _fake_add_cylinder(
    session: _FakeSession,
    *,
    radius: float,
    height: float,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    axis: str = "z",
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
    session.doc.Objects = (*session.doc.Objects, obj)
    session.set_result_object(obj)
    return object()


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

    def reject_replace(source: Path, destination: Path) -> None:
        assert source != checkpoint
        assert destination == checkpoint
        raise OSError("replace failed")

    monkeypatch.setattr(executor_module.os, "replace", reject_replace)
    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).checkpoint_fcstd(session, checkpoint)

    assert caught.value.code is ExecutorErrorCode.ARTIFACT_FAILURE
    assert checkpoint.read_bytes() == baseline
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
        @staticmethod
        def get_assembly_shape() -> object:
            return CompoundWithoutCenter()

    observation = executor_module._shape_observation(CompoundSession())

    assert observation.center_of_mass_mm == (20.0, 2.0, 4.0)
    assert observation.solid_count == 2


def test_derived_geometry_tolerance_accepts_roundoff_but_rejects_material_error() -> None:
    reference = 11_650_984.713_924_531
    assert executor_module._same_geometry_number(reference, reference + 1.862_645e-9)
    assert not executor_module._same_geometry_number(reference, reference + 0.1)


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
