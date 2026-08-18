from __future__ import annotations

import copy
import hashlib
import pickle

import pytest

from vibecad.execution.freecad_reviewed_artifact_inputs import (
    MAX_REVIEWED_ARTIFACT_BYTES,
    ReviewedArtifactCatalogRecord,
    ReviewedArtifactCatalogSnapshot,
    ReviewedArtifactInputError,
    ReviewedArtifactInputErrorCode,
    _ReviewedArtifactRunResolver,
)
from vibecad.intent_bridge.ports import read_verified_document

_PAYLOAD = b"authenticated STEP bytes"
_DIGEST = hashlib.sha256(_PAYLOAD).hexdigest()
_PNG = b"authenticated PNG bytes"
_PNG_DIGEST = hashlib.sha256(_PNG).hexdigest()


def _record(
    *,
    artifact_id: str = "artifact_step",
    content_sha256: str = _DIGEST,
    size_bytes: int = len(_PAYLOAD),
    operation_ids: tuple[str, ...] = ("step",),
    maximum_bytes: int = MAX_REVIEWED_ARTIFACT_BYTES,
) -> ReviewedArtifactCatalogRecord:
    return ReviewedArtifactCatalogRecord(
        artifact_id=artifact_id,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        media_type="model/step",
        role_term_ref_id="role_part_file_import_artifact",
        schema_term_ref_id="schema_part_step_artifact_v1",
        document_id=f"part_file_import_{content_sha256[:32]}",
        family_id="freecad_part_file_import",
        operation_ids=operation_ids,
        maximum_bytes=maximum_bytes,
    )


def _snapshot(
    records: tuple[ReviewedArtifactCatalogRecord, ...] | None = None,
) -> ReviewedArtifactCatalogSnapshot:
    return ReviewedArtifactCatalogSnapshot(
        task_id="task_exact",
        project_id="project_exact",
        base_revision="revision_exact",
        run_id="artifact_run_exact",
        records=(_record(),) if records is None else records,
    )


def _image_record() -> ReviewedArtifactCatalogRecord:
    return ReviewedArtifactCatalogRecord(
        artifact_id="artifact_image",
        content_sha256=_PNG_DIGEST,
        size_bytes=len(_PNG),
        media_type="image/png",
        role_term_ref_id="role_imageplane_artifact",
        schema_term_ref_id="schema_imageplane_png_artifact_v1",
        document_id=f"imageplane_{_PNG_DIGEST[:32]}",
        family_id="freecad_imageplane",
        operation_ids=("place_or_edit_image_plane",),
        maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
    )


class _Source:
    def __init__(self, payload: bytes = _PAYLOAD) -> None:
        self.payload = payload
        self.reads: list[tuple[ReviewedArtifactCatalogRecord, int]] = []
        self.close_count = 0

    def read(self, record: ReviewedArtifactCatalogRecord, maximum_bytes: int) -> bytes:
        self.reads.append((record, maximum_bytes))
        return self.payload

    def close(self) -> None:
        self.close_count += 1


class _StagerFactory:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.creates: list[tuple[ReviewedArtifactCatalogRecord, str, str]] = []
        self.close_count = 0
        self.stager = object()
        self.fail_close = fail_close

    def create(
        self,
        *,
        record: ReviewedArtifactCatalogRecord,
        family_id: str,
        operation_id: str,
    ) -> object:
        self.creates.append((record, family_id, operation_id))
        return self.stager

    def close(self) -> None:
        self.close_count += 1
        if self.fail_close:
            raise RuntimeError


def _resolver(
    *,
    snapshot: ReviewedArtifactCatalogSnapshot | None = None,
    source: _Source | None = None,
    stagers: _StagerFactory | None = None,
    token: object | None = None,
) -> tuple[_ReviewedArtifactRunResolver, object, _Source, _StagerFactory]:
    selected_snapshot = _snapshot() if snapshot is None else snapshot
    selected_source = _Source() if source is None else source
    selected_stagers = _StagerFactory() if stagers is None else stagers
    selected_token = object() if token is None else token
    return (
        _ReviewedArtifactRunResolver(
            snapshot=selected_snapshot,
            source=selected_source,
            stager_factory=selected_stagers,
            task_id="task_exact",
            project_id="project_exact",
            base_revision="revision_exact",
            run_id="artifact_run_exact",
            run_token=selected_token,
        ),
        selected_token,
        selected_source,
        selected_stagers,
    )


def _resolve(resolver: _ReviewedArtifactRunResolver, token: object, **changes: object):
    values: dict[str, object] = {
        "run_token": token,
        "family_id": "freecad_part_file_import",
        "operation_id": "step",
        "artifact_id": "artifact_step",
        "content_sha256": _DIGEST,
        "role_term_ref_id": "role_part_file_import_artifact",
        "schema_term_ref_id": "schema_part_step_artifact_v1",
        "media_type": "model/step",
        "maximum_bytes": MAX_REVIEWED_ARTIFACT_BYTES,
    }
    values.update(changes)
    return resolver.resolve(**values)


def test_catalog_is_canonical_and_binds_the_complete_run() -> None:
    alpha = _record(artifact_id="artifact_a", operation_ids=("step_b", "step_a"))
    beta = _record(artifact_id="artifact_b")
    first = _snapshot((beta, alpha))
    second = _snapshot(
        (
            _record(artifact_id="artifact_a", operation_ids=("step_a", "step_b")),
            beta,
        )
    )

    assert first.records == second.records
    assert first.catalog_sha256 == second.catalog_sha256
    assert first.canonical_bytes == second.canonical_bytes
    assert first.to_mapping()["catalog_sha256"] == first.catalog_sha256
    assert tuple(item.artifact_id for item in first.records) == ("artifact_a", "artifact_b")

    changed_run = ReviewedArtifactCatalogSnapshot(
        task_id=first.task_id,
        project_id=first.project_id,
        base_revision=first.base_revision,
        run_id="artifact_run_other",
        records=first.records,
    )
    assert changed_run.catalog_sha256 != first.catalog_sha256

    retained_by_caller = _record()
    isolated = _snapshot((retained_by_caller,))
    object.__setattr__(retained_by_caller, "artifact_id", "artifact_tampered")
    assert isolated.records[0].artifact_id == "artifact_step"


def test_catalog_rejects_duplicates_and_budget_overflow() -> None:
    with pytest.raises(ReviewedArtifactInputError) as duplicate_operation:
        _record(operation_ids=("step", "step"))
    assert duplicate_operation.value.code is ReviewedArtifactInputErrorCode.INVALID_INPUT

    entry = _record()
    with pytest.raises(ReviewedArtifactInputError) as duplicate_artifact:
        _snapshot((entry, entry))
    assert duplicate_artifact.value.code is ReviewedArtifactInputErrorCode.INVALID_INPUT

    oversized_records = tuple(
        _record(
            artifact_id=f"artifact_{index}",
            size_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
        )
        for index in range(17)
    )
    with pytest.raises(ReviewedArtifactInputError) as total_budget:
        _snapshot(oversized_records)
    assert total_budget.value.code is ReviewedArtifactInputErrorCode.BUDGET_EXCEEDED


def test_resolver_returns_one_exact_reader_and_stager_context() -> None:
    resolver, token, source, stagers = _resolver()

    resolution = _resolve(resolver, token)
    context = resolution.artifact_context
    document = context.artifact_document

    assert document.artifact_id == "artifact_step"
    assert document.document_id == f"part_file_import_{_DIGEST[:32]}"
    assert document.document_digest == _DIGEST
    assert document.content_sha256 == _DIGEST
    assert document.role_term_ref_id == "role_part_file_import_artifact"
    assert document.schema_term_ref_id == "schema_part_step_artifact_v1"
    assert document.media_type == "model/step"
    assert (
        read_verified_document(
            context.artifacts,
            document,
            maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
        )
        == _PAYLOAD
    )
    assert context.stager_factory.create() is stagers.stager
    assert source.reads == [(resolver._snapshot.records[0], MAX_REVIEWED_ARTIFACT_BYTES)]
    assert stagers.creates == [(resolver._snapshot.records[0], "freecad_part_file_import", "step")]


def test_same_foundation_resolves_the_imageplane_artifact_contract() -> None:
    snapshot = _snapshot((_image_record(),))
    resolver, token, _source, stagers = _resolver(
        snapshot=snapshot,
        source=_Source(_PNG),
    )

    context = _resolve(
        resolver,
        token,
        family_id="freecad_imageplane",
        operation_id="place_or_edit_image_plane",
        artifact_id="artifact_image",
        content_sha256=_PNG_DIGEST,
        role_term_ref_id="role_imageplane_artifact",
        schema_term_ref_id="schema_imageplane_png_artifact_v1",
        media_type="image/png",
    ).artifact_context

    assert context.artifact_document.document_id == f"imageplane_{_PNG_DIGEST[:32]}"
    assert (
        read_verified_document(
            context.artifacts,
            context.artifact_document,
            maximum_bytes=MAX_REVIEWED_ARTIFACT_BYTES,
        )
        == _PNG
    )
    assert context.stager_factory.create() is stagers.stager


@pytest.mark.parametrize(
    ("changes", "code"),
    (
        ({"artifact_id": "artifact_unknown"}, ReviewedArtifactInputErrorCode.UNKNOWN_ARTIFACT),
        ({"content_sha256": "f" * 64}, ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION),
        (
            {"role_term_ref_id": "role_wrong"},
            ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION,
        ),
        (
            {"schema_term_ref_id": "schema_wrong"},
            ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION,
        ),
        ({"media_type": "model/iges"}, ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION),
        ({"maximum_bytes": 1024}, ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION),
        ({"family_id": "freecad_imageplane"}, ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION),
        ({"operation_id": "brep"}, ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION),
    ),
)
def test_resolution_rejects_every_unbound_selector(
    changes: dict[str, object],
    code: ReviewedArtifactInputErrorCode,
) -> None:
    resolver, token, source, stagers = _resolver()

    with pytest.raises(ReviewedArtifactInputError) as failure:
        _resolve(resolver, token, **changes)

    assert failure.value.code is code
    assert source.reads == []
    assert stagers.creates == []


def test_constructor_and_resolve_reject_cross_binding_and_cross_run() -> None:
    snapshot = _snapshot()
    with pytest.raises(ReviewedArtifactInputError) as wrong_task:
        _ReviewedArtifactRunResolver(
            snapshot=snapshot,
            source=_Source(),
            stager_factory=_StagerFactory(),
            task_id="task_other",
            project_id=snapshot.project_id,
            base_revision=snapshot.base_revision,
            run_id=snapshot.run_id,
            run_token=object(),
        )
    assert wrong_task.value.code is ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION

    resolver, _token, source, stagers = _resolver(snapshot=snapshot)
    with pytest.raises(ReviewedArtifactInputError) as wrong_run:
        _resolve(resolver, object())
    assert wrong_run.value.code is ReviewedArtifactInputErrorCode.AUTHORITY_VIOLATION
    assert source.reads == []
    assert stagers.creates == []


def test_catalog_and_payload_tamper_fail_closed() -> None:
    snapshot = _snapshot()
    resolver, token, source, stagers = _resolver(snapshot=snapshot)
    resolution = _resolve(resolver, token)
    object.__setattr__(snapshot.records[0], "content_sha256", "f" * 64)

    with pytest.raises(ReviewedArtifactInputError) as catalog_tamper:
        resolution.artifact_context.artifacts.read(
            resolution.artifact_context.artifact_document,
            MAX_REVIEWED_ARTIFACT_BYTES,
        )
    assert catalog_tamper.value.code is ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE
    with pytest.raises(ReviewedArtifactInputError) as stager_tamper:
        resolution.artifact_context.stager_factory.create()
    assert stager_tamper.value.code is ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE
    assert source.reads == []
    assert stagers.creates == []

    bad_source = _Source(b"different bytes")
    clean_resolver, clean_token, _, _ = _resolver(source=bad_source)
    clean_context = _resolve(clean_resolver, clean_token).artifact_context
    with pytest.raises(ReviewedArtifactInputError) as payload_tamper:
        clean_context.artifacts.read(
            clean_context.artifact_document,
            MAX_REVIEWED_ARTIFACT_BYTES,
        )
    assert payload_tamper.value.code is ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE


def test_resolver_rejects_a_coherently_replaced_snapshot_after_binding() -> None:
    snapshot = _snapshot()
    resolver, token, source, stagers = _resolver(snapshot=snapshot)
    context = _resolve(resolver, token).artifact_context
    replacement = _snapshot(
        (
            _record(
                content_sha256="f" * 64,
                size_bytes=len(_PAYLOAD),
            ),
        )
    )
    object.__setattr__(snapshot, "records", replacement.records)
    object.__setattr__(snapshot, "canonical_bytes", replacement.canonical_bytes)
    object.__setattr__(snapshot, "catalog_sha256", replacement.catalog_sha256)

    with pytest.raises(ReviewedArtifactInputError) as failure:
        context.artifacts.read(context.artifact_document, MAX_REVIEWED_ARTIFACT_BYTES)

    assert failure.value.code is ReviewedArtifactInputErrorCode.INTEGRITY_FAILURE
    assert source.reads == []
    assert stagers.creates == []


def test_resolver_and_derived_capabilities_are_noncopyable_and_nonserializable() -> None:
    resolver, token, _, _ = _resolver()
    resolution = _resolve(resolver, token)
    capabilities = (
        resolver,
        resolution,
        resolution.artifact_context,
        resolution.artifact_context.artifacts,
        resolution.artifact_context.stager_factory,
    )

    for capability in capabilities:
        with pytest.raises(TypeError):
            copy.copy(capability)
        with pytest.raises(TypeError):
            copy.deepcopy(capability)
        with pytest.raises(TypeError):
            pickle.dumps(capability)


def test_close_is_idempotent_and_revokes_every_derived_capability() -> None:
    resolver, token, source, stagers = _resolver()
    context = _resolve(resolver, token).artifact_context

    resolver.close()
    resolver.close()

    assert source.close_count == 1
    assert stagers.close_count == 1
    for action in (
        lambda: _resolve(resolver, token),
        lambda: context.artifacts.read(
            context.artifact_document,
            MAX_REVIEWED_ARTIFACT_BYTES,
        ),
        context.stager_factory.create,
    ):
        with pytest.raises(ReviewedArtifactInputError) as closed:
            action()
        assert closed.value.code is ReviewedArtifactInputErrorCode.CLOSED


def test_cleanup_failure_still_revokes_the_resolver_and_closes_other_capabilities() -> None:
    stagers = _StagerFactory(fail_close=True)
    resolver, token, source, _ = _resolver(stagers=stagers)

    with pytest.raises(ReviewedArtifactInputError) as failure:
        resolver.close()

    assert failure.value.code is ReviewedArtifactInputErrorCode.CLEANUP_FAILED
    assert source.close_count == 1
    assert stagers.close_count == 1
    with pytest.raises(ReviewedArtifactInputError) as closed:
        _resolve(resolver, token)
    assert closed.value.code is ReviewedArtifactInputErrorCode.CLOSED
