"""Nominal CadExecutionPort and trusted import-normalization tests."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

import vibecad.execution.executor as executor_module
from vibecad.execution.candidate import CadSnapshotPort
from vibecad.execution.executor import (
    CandidateEvidence as ExecutorCandidateEvidence,
)
from vibecad.execution.executor import (
    ExecutorError,
    ExecutorErrorCode,
    InProcessCadExecutor,
)
from vibecad.execution.registry import ExecutionProfile
from vibecad.execution.revisions import LocalRevisionStore
from vibecad.interaction.cad import (
    MAX_ADMITTED_CREATED_OBJECTS,
    MAX_ADMITTED_RESULT_BYTES,
    MAX_ADMITTED_RUNTIME_MS,
    CadCapabilityStatus,
    CadExecutionPort,
    CadProfileCapability,
    CandidateEvidence,
    ValidatedImportEvidence,
)
from vibecad.validation import EntityObservation, EntityParameterObservation


def _store() -> LocalRevisionStore:
    return object.__new__(LocalRevisionStore)


def _fcstd(path: Path, document: str = "<Document />") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Document.xml", document)


def test_nominal_port_extends_snapshot_port_and_reports_only_headless_verified() -> None:
    executor = InProcessCadExecutor(store=_store())

    assert isinstance(executor, CadExecutionPort)
    assert isinstance(executor, CadSnapshotPort)
    assert issubclass(InProcessCadExecutor, CadExecutionPort)
    assert executor.execution_profile is ExecutionProfile.HEADLESS
    assert executor.capabilities == (
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
    assert ExecutorCandidateEvidence is CandidateEvidence
    assert (
        MAX_ADMITTED_RUNTIME_MS,
        MAX_ADMITTED_CREATED_OBJECTS,
        MAX_ADMITTED_RESULT_BYTES,
    ) == (30_000, 1, 262_144)


def test_validated_import_evidence_is_exact_and_immutable() -> None:
    evidence = ValidatedImportEvidence(sha256="a" * 64, size_bytes=1)
    assert evidence.sha256 == "a" * 64
    with pytest.raises((AttributeError, TypeError)):
        evidence.size_bytes = 2  # type: ignore[misc]
    with pytest.raises(ValueError):
        ValidatedImportEvidence(sha256="A" * 64, size_bytes=1)
    with pytest.raises(ValueError):
        ValidatedImportEvidence(sha256="a" * 64, size_bytes=True)  # type: ignore[arg-type]


def test_validate_import_normalizes_box_identity_checkpoints_and_reloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    saved_identities: list[object] = []
    made: list[ImportSession] = []

    class ImportObject:
        Name = "Box"
        TypeId = "Part::Box"
        geometry_marker = (10.0, 20.0, 30.0)

    class ImportDocument:
        def __init__(self, obj: ImportObject) -> None:
            self.Objects = (obj,)
            self.recompute_calls = 0
            self.transactions: list[str] = []
            self.commits = 0
            self.aborts = 0

        def recompute(self) -> None:
            self.recompute_calls += 1

        def openTransaction(self, name: str) -> None:  # noqa: N802
            self.transactions.append(name)

        def commitTransaction(self) -> None:  # noqa: N802
            self.commits += 1

        def abortTransaction(self) -> None:  # noqa: N802
            self.aborts += 1

        def saveCopy(self, path: str) -> None:  # noqa: N802
            _fcstd(Path(path))

    class ImportSession:
        freecad_version = (1, 1)

        def __init__(self) -> None:
            self.obj = ImportObject()
            self.doc = ImportDocument(self.obj)
            self.identities: list[object] = []
            self.close_calls = 0
            self.persist_calls = 0
            made.append(self)

        def load_document(self, path: Path) -> object:
            del path
            self.identities = list(saved_identities)
            return self.doc

        def list_object_identities(self) -> tuple[tuple[object, object], ...]:
            return tuple((self.obj, identity) for identity in self.identities)

        def attach_object_identity(self, obj: object, identity: object) -> object:
            assert obj is self.obj
            self.identities.append(identity)
            saved_identities[:] = self.identities
            return identity

        def read_object_identity(self, obj: object) -> object:
            assert obj is self.obj
            return self.identities[0]

        def persist_state(self) -> None:
            self.persist_calls += 1
            saved_identities[:] = self.identities

        def close_document(self) -> None:
            self.close_calls += 1

    def observations(session: ImportSession) -> tuple[object, ...]:
        return tuple(
            (
                identity.object_id,
                identity.feature_id,
                identity.object_type,
                session.obj.geometry_marker,
            )
            for _, identity in session.list_object_identities()
        )

    monkeypatch.setattr(executor_module, "_Session", ImportSession)
    monkeypatch.setattr(executor_module, "_entity_observations", observations)
    staging = tmp_path / "bootstrap.FCStd"
    _fcstd(staging)

    evidence = InProcessCadExecutor(store=_store()).validate_import(staging)

    assert type(evidence) is ValidatedImportEvidence
    raw = staging.read_bytes()
    assert evidence == ValidatedImportEvidence(
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )
    assert len(made) == 2
    assert made[0].doc.recompute_calls >= 1
    assert made[0].doc.transactions == ["VibeCAD Import Identity Normalization"]
    assert made[0].doc.commits == 1
    assert made[0].persist_calls == 1
    assert made[0].close_calls == 1
    assert made[1].doc.recompute_calls >= 1
    assert made[1].close_calls == 1
    identity = saved_identities[0]
    assert identity.object_id.startswith("object_")
    assert identity.feature_id.startswith("feature_")
    assert identity.object_type == "Part::Box"
    assert identity.provenance.source.value == "imported"
    assert identity.provenance.operation_id is None


def test_validate_import_rejects_empty_document_and_closes_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class EmptyDocument:
        Objects: tuple[object, ...] = ()

        def recompute(self) -> None:
            return None

    class EmptySession:
        def __init__(self) -> None:
            self.doc = EmptyDocument()
            self.close_calls = 0

        def load_document(self, path: Path) -> object:
            del path
            return self.doc

        def list_object_identities(self) -> tuple[object, ...]:
            return ()

        def close_document(self) -> None:
            self.close_calls += 1

    made = EmptySession()
    monkeypatch.setattr(executor_module, "_Session", lambda: made)
    staging = tmp_path / "empty.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert made.close_calls == 1


def test_validate_import_rejects_unobserved_app_featurepython_object(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ImportObject:
        TypeId = "Part::Box"

    class UnobservedObject:
        TypeId = "App::FeaturePython"

    class Document:
        Objects = (ImportObject(), UnobservedObject())

        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

        def list_object_identities(self) -> tuple[object, ...]:
            return ()

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )

    def observations(session: object) -> tuple[str, ...]:
        executor_module._import_objects(session)
        return ("box-observation",)

    monkeypatch.setattr(executor_module, "_normalize_import_identities", observations)
    monkeypatch.setattr(executor_module, "_validated_import_observations", observations)
    staging = tmp_path / "unobserved-app-object.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INVALID_INPUT
    assert len(closed) == 1


def test_validate_import_rejects_reload_identity_or_geometry_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: ("identity-a", "geometry-a"),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: ("identity-a", "geometry-b"),
    )
    staging = tmp_path / "drift.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert len(closed) == 2


def test_validate_import_accepts_bounded_occ_reload_noise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    def observed(center_y: float) -> tuple[EntityObservation, ...]:
        return (
            EntityObservation(
                object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                feature_id="feature_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                object_type="Part::Cylinder",
                semantic_role="primitive",
                provenance={"source": "imported", "operation_id": None},
                placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
                volume_mm3=62.83185307179586,
                area_mm2=87.96459430051421,
                bbox_mm=(4.0, 4.0, 5.0),
                center_of_mass_mm=(30.0, center_y, 2.5),
                valid_shape=True,
                solid_count=1,
            ),
        )

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: observed(4.376390559497447e-17),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: observed(6.91504145611255e-17),
    )
    staging = tmp_path / "occ-noise.FCStd"
    _fcstd(staging)

    evidence = InProcessCadExecutor(store=_store()).validate_import(staging)

    assert type(evidence) is ValidatedImportEvidence
    assert len(closed) == 2


def test_validate_import_rejects_direct_parameter_drift_within_geometry_tolerance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    def observed(length: float) -> tuple[EntityObservation, ...]:
        return (
            EntityObservation(
                object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                feature_id="feature_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                object_type="Part::Box",
                semantic_role="primitive",
                provenance={"source": "imported", "operation_id": None},
                placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
                parameters=(
                    EntityParameterObservation(
                        name="length",
                        value=length,
                        unit="mm",
                    ),
                ),
                volume_mm3=1.0,
                area_mm2=6.0,
                bbox_mm=(1.0, 1.0, 1.0),
                center_of_mass_mm=(0.5, 0.5, 0.5),
                valid_shape=True,
                solid_count=1,
            ),
        )

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: observed(1.0),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: observed(1.0000000005),
    )
    staging = tmp_path / "parameter-drift.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert len(closed) == 2


def test_validate_import_rejects_large_coordinate_translation_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    def observed(x: float) -> tuple[EntityObservation, ...]:
        return (
            EntityObservation(
                object_id="object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                feature_id="feature_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                object_type="Part::Box",
                semantic_role="primitive",
                provenance={"source": "imported", "operation_id": None},
                placement=(x, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
                volume_mm3=1.0,
                area_mm2=6.0,
                bbox_mm=(1.0, 1.0, 1.0),
                center_of_mass_mm=(x + 0.5, 0.5, 0.5),
                valid_shape=True,
                solid_count=1,
            ),
        )

    sessions = iter((Session(), Session()))
    closed: list[object] = []
    monkeypatch.setattr(InProcessCadExecutor, "load_fcstd", lambda self, path: next(sessions))
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "close",
        lambda self, session: closed.append(session),
    )
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: observed(1_000_000_000_000.0),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: observed(1_000_000_000_001.0),
    )
    staging = tmp_path / "large-placement-drift.FCStd"
    _fcstd(staging)

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert len(closed) == 2


def test_validate_import_rejects_staging_swap_during_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Document:
        def recompute(self) -> None:
            return None

    class Session:
        doc = Document()

    staging = tmp_path / "swap.FCStd"
    _fcstd(staging)
    sessions = iter((Session(), Session()))
    close_calls = 0

    def close(self: object, session: object) -> None:
        nonlocal close_calls
        del self, session
        close_calls += 1
        if close_calls == 2:
            _fcstd(staging, "<Swapped />")

    monkeypatch.setattr(
        InProcessCadExecutor,
        "load_fcstd",
        lambda self, path: next(sessions),
    )
    monkeypatch.setattr(
        InProcessCadExecutor,
        "checkpoint_fcstd",
        lambda self, session, path: None,
    )
    monkeypatch.setattr(InProcessCadExecutor, "close", close)
    monkeypatch.setattr(
        executor_module,
        "_normalize_import_identities",
        lambda session: ("identity", "geometry"),
    )
    monkeypatch.setattr(
        executor_module,
        "_validated_import_observations",
        lambda session: ("identity", "geometry"),
    )

    with pytest.raises(ExecutorError) as caught:
        InProcessCadExecutor(store=_store()).validate_import(staging)

    assert caught.value.code is ExecutorErrorCode.INTEGRITY_FAILURE
    assert close_calls == 2
