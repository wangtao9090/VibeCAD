from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

import vibecad.execution.freecad_part_file_import_reviewed_execution as import_execution
from tests.test_intent_bridge_freecad_part_file_import_adapter import (
    _graph,
    _lower,
    _request,
    _Sink,
)
from vibecad.execution.freecad_part_file_import_reviewed_execution import (
    PART_FILE_IMPORT_RESULT_INVARIANTS,
    PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC,
    PART_FILE_IMPORT_REVIEWED_PRODUCT_IDENTITIES,
    PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS,
    PART_FILE_IMPORT_SHARED_BLOCKERS,
    PART_FILE_IMPORT_SHARED_REGISTRATION_READY,
    PartFileImportArtifactAuthority,
    execute_part_file_import_reviewed_plan,
    execute_part_file_import_reviewed_plan_with_authority,
    part_file_import_reviewed_adapter_factory,
    resolve_part_file_import_reviewed_operation,
    validate_part_file_import_reviewed_plan,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PART_FILE_IMPORT_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    _ReviewedFamilyExecutionContext,
)
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_part_file_import_adapter import (
    PART_FILE_IMPORT_MANIFEST,
    FreeCADPartFileImportAdapter,
    build_part_file_import_artifact_document,
)
from vibecad.parametric.freecad_part_file_import_rules import (
    MAX_PART_FILE_IMPORT_ARTIFACT_BYTES,
    PART_FILE_IMPORT_NATIVE_SPECS,
    HostOwnedImportStager,
    PartFileImportConformanceReceipt,
    PartFileImportExecutionBindings,
    PartFileImportOperation,
)
from vibecad.validation.contracts import EntityObservation


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def read(self, document: DocumentRef, maximum_bytes: int) -> bytes:
        del document
        self.calls += 1
        if len(self.payload) > maximum_bytes:
            raise RuntimeError("over budget")
        return self.payload


class _Shape:
    def __init__(self, value: str = "reviewed-import-shape") -> None:
        self._value = value
        self.ShapeType = "Compound"
        self.Vertexes = [object()]
        self.Edges = [object()]
        self.Faces = [object()]
        self.Solids: list[object] = []
        self.Length = 1.0
        self.Area = 1.0
        self.Volume = 0.0

    def exportBrepToString(self) -> str:
        return self._value

    def isNull(self) -> bool:
        return False

    def isValid(self) -> bool:
        return True

    def mutate(self) -> None:
        self._value += "-stale"


class _Result:
    def __init__(self, document: _Document, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.TypeId = type_id
        self.FileName = ""
        self.State = ("Up-to-date",)
        self.Shape = _Shape()

    def isValid(self) -> bool:
        return True


class _Document:
    def __init__(self) -> None:
        self.Objects: list[_Result] = []
        # Deliberately tempting, untrusted fallbacks.  The family callback must
        # not inspect any of them when the context authority is absent.
        self.FileName = "/tmp/untrusted.step"
        self.Label = "artifact_part_file_import_source"
        self.artifact_store = {"artifact_part_file_import_source": b"untrusted"}

    def getObject(self, name: str) -> _Result | None:
        return next((item for item in self.Objects if item.Name == name), None)


def _lowered(
    operation: PartFileImportOperation,
    artifact_payload: bytes,
):
    artifact_document = build_part_file_import_artifact_document(operation, artifact_payload)
    request, reader, policy = _request(_graph(operation, artifact_document.content_sha256))
    adapter = FreeCADPartFileImportAdapter(_Sink())
    _, receipt = _lower(adapter, request, reader, policy)
    plan, payload = adapter.read_plan(receipt)
    return plan, payload, receipt, artifact_document


def _stager(tmp_path: Path) -> HostOwnedImportStager:
    root = tmp_path / "staging"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    return HostOwnedImportStager(root)


def _install_fake_apply(
    monkeypatch: pytest.MonkeyPatch,
    document: _Document,
    artifact_size: int,
) -> list[PartFileImportExecutionBindings]:
    calls: list[PartFileImportExecutionBindings] = []

    def apply(
        raw: bytes,
        *,
        expected_content_sha256: str,
        expected_plan_sha256: str,
        bindings: PartFileImportExecutionBindings,
    ) -> PartFileImportConformanceReceipt:
        del expected_content_sha256
        assert type(raw) is bytes
        assert bindings.document is document
        calls.append(bindings)
        plan = import_execution.decode_part_file_import_backend_plan(
            raw,
            expected_plan_sha256=expected_plan_sha256,
        )
        name = f"Import_{plan.operation.value}"
        result = _Result(document, name, PART_FILE_IMPORT_NATIVE_SPECS[plan.operation].type_id)
        document.Objects.append(result)
        shape_digest = hashlib.sha256(result.Shape.exportBrepToString().encode("utf-8")).hexdigest()
        return PartFileImportConformanceReceipt(
            plan_sha256=plan.plan_sha256,
            operation=plan.operation,
            object_name=name,
            artifact_id=plan.artifact_id,
            artifact_content_sha256=plan.artifact_content_sha256,
            artifact_size_bytes=artifact_size,
            result_shape_type=result.Shape.ShapeType,
            result_shape_sha256=shape_digest,
            edge_count=len(result.Shape.Edges),
            face_count=len(result.Shape.Faces),
            solid_count=len(result.Shape.Solids),
        )

    monkeypatch.setattr(import_execution, "apply_part_file_import_plan", apply)
    return calls


def test_part_file_import_family_slice_freezes_three_exact_routes_and_blockers() -> None:
    assert PART_FILE_IMPORT_REVIEWED_PRODUCT_OPERATIONS == tuple(PartFileImportOperation)
    assert len(PART_FILE_IMPORT_REVIEWED_PRODUCT_IDENTITIES) == 3
    assert PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.manifest is PART_FILE_IMPORT_MANIFEST
    assert PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.operation_ids == ("brep", "iges", "step")
    assert PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.result_invariants is (
        PART_FILE_IMPORT_RESULT_INVARIANTS
    )
    assert PART_FILE_IMPORT_SHARED_REGISTRATION_READY is True
    assert PART_FILE_IMPORT_REVIEWED_FAMILY_SPEC.shared_registration_ready is True
    assert PART_FILE_IMPORT_SHARED_BLOCKERS == ()
    assert len(CURRENT_REVIEWED_INTENT_ROUTES) == 96
    assert CURRENT_REVIEWED_INTENT_ROUTES[78:81] == REVIEWED_PART_FILE_IMPORT_ROUTES
    assert tuple(route.operation.operation_id for route in REVIEWED_PART_FILE_IMPORT_ROUTES) == (
        "brep",
        "iges",
        "step",
    )
    adapter = part_file_import_reviewed_adapter_factory(_Sink())
    assert type(adapter) is FreeCADPartFileImportAdapter
    for operation in PartFileImportOperation:
        reviewed = next(
            item
            for item in PART_FILE_IMPORT_MANIFEST.operations
            if item.operation_id == operation.value
        )
        namespace, version, term_id, digest = reviewed.semantic_term.semantic_identity
        semantic = f"{namespace}/{version}/{term_id}@{digest}"
        route = next(
            item
            for item in REVIEWED_PART_FILE_IMPORT_ROUTES
            if item.operation.operation_id == operation.value
        )
        assert route.semantic_operation == semantic
        assert route.family.formal_semantic_binding.value == "full_identity"
        assert route.family.artifact_requirement_for(route.operation) is not None
        assert route.family.product_execution_mode(route.operation).value == "create"
        assert (
            resolve_part_file_import_reviewed_operation(
                f"{PART_FILE_IMPORT_MANIFEST.family_id}.{operation.value}",
                semantic,
            )
            is reviewed
        )
        assert PART_FILE_IMPORT_RESULT_INVARIANTS[operation].native_type_id == (
            PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id
        )
        assert PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id.startswith("Part::Import")
    assert (
        resolve_part_file_import_reviewed_operation("freecad_part_file_import.step", "step") is None
    )


@pytest.mark.parametrize("operation", tuple(PartFileImportOperation))
def test_part_file_import_plan_binding_is_exact(
    operation: PartFileImportOperation,
) -> None:
    plan, _, receipt, _ = _lowered(operation, f"artifact-{operation.value}".encode())
    validate_part_file_import_reviewed_plan(plan, receipt, receipt.operation)

    other = next(item for item in PART_FILE_IMPORT_MANIFEST.operations if item != receipt.operation)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        validate_part_file_import_reviewed_plan(plan, receipt, other)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE

    tampered_receipt = dataclasses.replace(
        receipt,
        plan_document=dataclasses.replace(
            receipt.plan_document,
            media_type="application/octet-stream",
        ),
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        validate_part_file_import_reviewed_plan(plan, tampered_receipt, receipt.operation)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


def test_shared_callback_rejects_missing_artifact_authority_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, payload, receipt, _ = _lowered(PartFileImportOperation.STEP, b"exact-step")
    document = _Document()
    called = False

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal called
        called = True
        raise AssertionError("native rule must remain unreachable")

    monkeypatch.setattr(import_execution, "apply_part_file_import_plan", forbidden)
    context = _ReviewedFamilyExecutionContext(
        session=object(),
        document=document,
        source_results=(),
    )
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_file_import_reviewed_plan(
            document,
            plan,
            payload,
            receipt.plan_document,
            receipt.operation,
            context,
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert document.Objects == [] and called is False


@pytest.mark.parametrize("operation", tuple(PartFileImportOperation))
def test_internal_authority_executes_and_closes_owned_valid_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: PartFileImportOperation,
) -> None:
    artifact_payload = f"authenticated-{operation.value}".encode()
    plan, payload, receipt, artifact_document = _lowered(operation, artifact_payload)
    document = _Document()
    calls = _install_fake_apply(monkeypatch, document, len(artifact_payload))
    reader = _Reader(artifact_payload)
    authority = PartFileImportArtifactAuthority(
        artifact_document=artifact_document,
        artifacts=reader,
        stager=_stager(tmp_path),
    )

    execution = execute_part_file_import_reviewed_plan_with_authority(
        document,
        plan,
        payload,
        receipt.plan_document,
        receipt.operation,
        authority,
    )

    assert execution.object is document.Objects[0]
    assert execution.object.TypeId == PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id
    assert len(calls) == 1 and calls[0].artifact_document == artifact_document
    assert calls[0].artifacts is reader and calls[0].stager is authority.stager
    assert reader.calls == 1
    assert execution.receipt.artifact_id == artifact_document.artifact_id
    assert execution.receipt.artifact_content_sha256 == artifact_document.content_sha256
    observation = EntityObservation(
        object_id="object_" + "a" * 32,
        feature_id="feature_" + "b" * 32,
        object_type=execution.object.TypeId,
        semantic_role="feature",
        provenance={"source": "model", "operation_id": "apply_reviewed_intent"},
        placement=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0),
        area_mm2=1.0,
        bbox_mm=(1.0, 1.0, 1.0),
        center_of_mass_mm=(0.0, 0.0, 0.0),
        valid_shape=True,
        solid_count=0,
    )
    execution.receipt.validate_adoption(document, execution.object, observation)
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execution.receipt.validate_adopted_observation(
            dataclasses.replace(observation, solid_count=1)
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    execution.object.Shape.mutate()
    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execution.receipt.validate_native_result(document, execution.object)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("failure", ("media", "digest", "payload", "oversize"))
def test_artifact_tamper_rejected_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    artifact_payload = b"authenticated-brep"
    plan, payload, receipt, artifact_document = _lowered(
        PartFileImportOperation.BREP,
        artifact_payload,
    )
    reader_payload = artifact_payload
    if failure == "media":
        artifact_document = dataclasses.replace(artifact_document, media_type="model/step")
    elif failure == "digest":
        artifact_document = dataclasses.replace(artifact_document, document_digest="0" * 64)
    elif failure == "payload":
        reader_payload += b"-tamper"
    else:
        artifact_document = dataclasses.replace(
            artifact_document,
            size_bytes=MAX_PART_FILE_IMPORT_ARTIFACT_BYTES + 1,
        )
    document = _Document()
    native_calls = _install_fake_apply(monkeypatch, document, len(artifact_payload))
    reader = _Reader(reader_payload)
    authority = PartFileImportArtifactAuthority(
        artifact_document=artifact_document,
        artifacts=reader,
        stager=_stager(tmp_path),
    )

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_part_file_import_reviewed_plan_with_authority(
            document,
            plan,
            payload,
            receipt.plan_document,
            receipt.operation,
            authority,
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert native_calls == [] and document.Objects == []
    if failure != "payload":
        assert reader.calls == 0
