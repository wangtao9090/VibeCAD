from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import vibecad.application.artifacts as artifacts
import vibecad.application.project_create as project_create
import vibecad.execution.revisions as revisions
import vibecad.interaction.checkouts as checkouts
import vibecad.workflow.store as task_store
from vibecad.application.artifacts import ArtifactRequestPhase, ArtifactSourceKind
from vibecad.application.project_api import ProjectKind
from vibecad.execution.revisions import CommitJournalState
from vibecad.workflow.state import TaskStatus

CORPUS = Path(__file__).parent / "fixtures" / "durable_v1"
SOURCE_ANCHOR = "2cfbbc416d789491c1c532653b4e460c53dfac60"
INDEX_SHA256 = "b6cee09ee434b9e952e011534124b11f9f910b9d706570c3030a0c05c35cc432"
INDEX_SIZE = 7_921
TASK_ID = "task_77777777777777777777777777777777"
PROJECT_ID = "project_11111111111111111111111111111111"
BASE_REVISION_ID = "revision_11111111111111111111111111111111"
SEALED_REVISION_ID = "revision_22222222222222222222222222222222"
CHECKOUT_ID = "checkout_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
CREATE_KEY = "project_create_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SYNTHETIC_SOURCE_LOCATOR = "synthetic-fixture-source"
MODEL_BYTES = b"VIBECAD-DURABLE-V1-GOLDEN-FCSTD\n"
STEP_BYTES = (
    b"ISO-10303-21;\n"
    b"HEADER;\n"
    b"FILE_DESCRIPTION(('VIBECAD DURABLE V1 GOLDEN'),'2;1');\n"
    b"ENDSEC;\n"
    b"DATA;\n"
    b"ENDSEC;\n"
    b"END-ISO-10303-21;\n"
)

EXPECTED_FACTS = {
    "checkout_open_v1.json": (
        "managed_checkout_open",
        "legacy open fact",
        1,
        "canonical-json",
    ),
    "checkout_open_v2.json": (
        "managed_checkout_open",
        "current open fact",
        2,
        "canonical-json",
    ),
    "checkout_tombstone_v1.json": (
        "managed_checkout_tombstone",
        "legacy closed fact",
        1,
        "canonical-json",
    ),
    "checkout_tombstone_v2.json": (
        "managed_checkout_tombstone",
        "current closed fact",
        2,
        "canonical-json",
    ),
    "generation_zero_empty_head.json": (
        "project_head",
        "empty generation zero",
        1,
        "canonical-json",
    ),
    "generation_zero_empty_manifest.json": (
        "revision_manifest",
        "empty generation zero",
        1,
        "canonical-json",
    ),
    "generation_zero_import_head.json": (
        "project_head",
        "imported generation zero",
        1,
        "canonical-json",
    ),
    "generation_zero_import_manifest.json": (
        "revision_manifest",
        "imported generation zero",
        1,
        "canonical-json",
    ),
    "journal_committed.json": (
        "revision_commit_journal",
        "committed decision",
        1,
        "canonical-json",
    ),
    "journal_not_committed.json": (
        "revision_commit_journal",
        "not-committed decision",
        1,
        "canonical-json",
    ),
    "journal_prepared.json": (
        "revision_commit_journal",
        "prepared decision",
        1,
        "canonical-json",
    ),
    "journal_staging.json": (
        "revision_commit_journal",
        "staging decision",
        1,
        "canonical-json",
    ),
    "materialization_delivery.json": (
        "artifact_delivery_manifest",
        "draft delivery",
        1,
        "canonical-json",
    ),
    "materialization_request.json": (
        "artifact_materialization_request",
        "reserved draft export",
        1,
        "canonical-json",
    ),
    "model.FCStd": ("revision_payload", "native model bytes", None, "opaque-bytes"),
    "model.step": ("revision_payload", "STEP exchange bytes", None, "opaque-bytes"),
    "project_create_hmac_key.json": (
        "project_create_hmac_key",
        "synthetic HMAC authority",
        1,
        "canonical-json",
    ),
    "project_create_quarantine_receipt.json": (
        "project_create_quarantine_receipt",
        "stage quarantine",
        1,
        "canonical-json",
    ),
    "project_create_request.json": (
        "project_create_request",
        "published imported generation zero",
        1,
        "canonical-json",
    ),
    "reservation.json": (
        "revision_reservation",
        "seeded candidate staged",
        1,
        "canonical-json",
    ),
    "sealed_revision_head.json": (
        "project_head",
        "sealed FCStd/STEP revision",
        1,
        "canonical-json",
    ),
    "sealed_revision_manifest.json": (
        "revision_manifest",
        "sealed FCStd/STEP revision",
        1,
        "canonical-json",
    ),
    "seed_binding.json": (
        "revision_seed_binding",
        "historical source bound",
        1,
        "canonical-json",
    ),
    "seed_intent.json": (
        "revision_seed_intent",
        "historical source requested",
        1,
        "canonical-json",
    ),
    "task_active.json": ("task_run", "active execution", 1, "canonical-json"),
    "task_awaiting_review.json": (
        "task_run",
        "awaiting user review",
        1,
        "canonical-json",
    ),
    "task_cancelled.json": ("task_run", "terminal cancelled", 1, "canonical-json"),
    "task_failed.json": ("task_run", "terminal failed", 1, "canonical-json"),
    "task_rejected.json": ("task_run", "terminal rejected", 1, "canonical-json"),
    "task_succeeded.json": (
        "task_run",
        "terminal reviewed success",
        1,
        "canonical-json",
    ),
}
EXPECTED_MEMBERS = tuple(sorted(EXPECTED_FACTS))


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _duplicate_checked_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> dict[str, object]:
    value = json.loads(
        raw,
        object_pairs_hook=_duplicate_checked_object,
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON")),
    )
    assert type(value) is dict
    return value


def _member_bytes(name: str) -> bytes:
    return (CORPUS / name).read_bytes()


def _member_json(name: str) -> dict[str, object]:
    raw = _member_bytes(name)
    assert not raw.endswith(b"\n")
    value = _parse_json(raw)
    assert _canonical_json(value) == raw
    return value


def _checked_body(name: str, domain: bytes, maximum: int) -> tuple[dict[str, object], bytes]:
    raw = _member_bytes(name)
    body, error = revisions._parse_checked_record(raw, domain, maximum)
    assert error is None
    assert type(body) is dict
    assert revisions._checked_record_bytes(body, domain) == raw
    return body, raw


def _manifest(name: str):
    body, raw = _checked_body(
        name,
        revisions._MANIFEST_CHECKSUM_DOMAIN,
        revisions._MAX_MANIFEST_BYTES,
    )
    value, error = revisions._revision_from_manifest(body, raw)
    assert error is None
    assert value is not None
    return value


def _head(name: str):
    body, _raw = _checked_body(
        name,
        revisions._HEAD_CHECKSUM_DOMAIN,
        revisions._MAX_HEAD_BYTES,
    )
    value, error = revisions._head_from_record(body)
    assert error is None
    assert value is not None
    return value


def test_durable_v1_corpus_is_complete_indexed_and_byte_exact() -> None:
    index_raw = _member_bytes("index.json")
    assert len(index_raw) == INDEX_SIZE
    assert hashlib.sha256(index_raw).hexdigest() == INDEX_SHA256
    assert index_raw.endswith(b"\n") and not index_raw.endswith(b"\n\n")
    index = _parse_json(index_raw[:-1])
    assert _canonical_json(index) + b"\n" == index_raw
    assert set(index) == {
        "byte_rules",
        "corpus_id",
        "corpus_schema_version",
        "hash_algorithm",
        "member_count",
        "members",
        "scope_note",
        "source_anchor",
    }
    assert index["corpus_id"] == "vibecad-pre-mr1-durable-baseline"
    assert index["corpus_schema_version"] == 1
    assert index["hash_algorithm"] == "sha256"
    assert index["member_count"] == len(EXPECTED_MEMBERS) == 30
    assert index["source_anchor"] == SOURCE_ANCHOR
    assert index["scope_note"] == (
        "Record versions are family-local. Revision records remain strict v1; managed checkout "
        "v2 facts are part of this pre-MR1 baseline and do not imply Revision v2."
    )
    assert index["byte_rules"] == {
        "binary_members": "opaque bytes; no text normalization",
        "index": (
            "UTF-8 canonical JSON with sorted keys and compact separators, followed by exactly "
            "one LF"
        ),
        "json_members": (
            "UTF-8 canonical JSON with sorted keys and compact separators, with no trailing LF"
        ),
        "member_order": "members sorted lexicographically by relative name",
    }

    entries = index["members"]
    assert type(entries) is list
    assert tuple(entry["name"] for entry in entries) == EXPECTED_MEMBERS
    actual_paths = tuple(sorted(CORPUS.iterdir(), key=lambda item: item.name))
    expected_paths = tuple(sorted(("index.json", *EXPECTED_MEMBERS)))
    assert tuple(path.name for path in actual_paths) == expected_paths
    assert all(path.is_file() and not path.is_symlink() for path in actual_paths)
    for entry in entries:
        assert type(entry) is dict
        assert set(entry) == {
            "encoding",
            "family",
            "name",
            "scenario",
            "schema_version",
            "sha256",
            "size_bytes",
        }
        name = entry["name"]
        assert type(name) is str and "/" not in name and "\\" not in name
        family, scenario, schema_version, encoding = EXPECTED_FACTS[name]
        assert (
            entry["family"],
            entry["scenario"],
            entry["schema_version"],
            entry["encoding"],
        ) == (family, scenario, schema_version, encoding)
        raw = _member_bytes(name)
        assert entry["size_bytes"] == len(raw)
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
        if encoding == "canonical-json":
            assert _canonical_json(_parse_json(raw)) == raw


def test_revision_head_journal_reservation_and_seed_vectors_round_trip() -> None:
    empty_revision = _manifest("generation_zero_empty_manifest.json")
    imported_revision = _manifest("generation_zero_import_manifest.json")
    sealed_revision = _manifest("sealed_revision_manifest.json")
    empty_head = _head("generation_zero_empty_head.json")
    imported_head = _head("generation_zero_import_head.json")
    sealed_head = _head("sealed_revision_head.json")

    assert empty_revision.base_revision is None
    assert empty_revision.model is None and empty_revision.artifacts == ()
    assert empty_head.generation == 0
    assert empty_head.revision_id == empty_revision.id
    assert empty_head.manifest_sha256 == empty_revision.manifest_sha256
    assert imported_revision.base_revision is None
    assert imported_revision.model is not None
    assert imported_revision.artifacts == ()
    assert imported_head.generation == 0
    assert imported_head.revision_id == imported_revision.id == BASE_REVISION_ID
    assert imported_head.manifest_sha256 == imported_revision.manifest_sha256
    assert sealed_revision.base_revision == imported_revision.id
    assert sealed_revision.id == SEALED_REVISION_ID
    assert sealed_head.generation == imported_head.generation + 1
    assert sealed_head.revision_id == sealed_revision.id
    assert sealed_head.manifest_sha256 == sealed_revision.manifest_sha256
    assert sealed_revision.model is not None
    assert (sealed_revision.model.sha256, sealed_revision.model.size_bytes) == (
        hashlib.sha256(MODEL_BYTES).hexdigest(),
        len(MODEL_BYTES),
    )
    assert len(sealed_revision.artifacts) == 1
    assert (
        sealed_revision.artifacts[0].sha256,
        sealed_revision.artifacts[0].size_bytes,
    ) == (hashlib.sha256(STEP_BYTES).hexdigest(), len(STEP_BYTES))
    assert _member_bytes("model.FCStd") == MODEL_BYTES
    assert _member_bytes("model.step") == STEP_BYTES

    journals = {}
    for name in (
        "journal_staging.json",
        "journal_prepared.json",
        "journal_committed.json",
        "journal_not_committed.json",
    ):
        body, _raw = _checked_body(
            name,
            revisions._JOURNAL_CHECKSUM_DOMAIN,
            revisions._MAX_JOURNAL_BYTES,
        )
        journal, error = revisions._journal_from_record(body)
        assert error is None
        assert journal is not None
        journals[name] = journal
        assert journal.expected_head == imported_head
        assert journal.candidate_revision == sealed_revision.id
    assert journals["journal_staging.json"].state is CommitJournalState.STAGING
    assert journals["journal_staging.json"].manifest_sha256 is None
    assert journals["journal_prepared.json"].state is CommitJournalState.PREPARED
    assert journals["journal_committed.json"].state is CommitJournalState.COMMITTED
    assert journals["journal_not_committed.json"].state is CommitJournalState.NOT_COMMITTED
    assert journals["journal_prepared.json"].manifest_sha256 == sealed_revision.manifest_sha256
    assert journals["journal_committed.json"].manifest_sha256 == sealed_revision.manifest_sha256
    assert journals["journal_not_committed.json"].manifest_sha256 == imported_head.manifest_sha256

    reservation_body, _raw = _checked_body(
        "reservation.json",
        revisions._RESERVATION_CHECKSUM_DOMAIN,
        revisions._MAX_JOURNAL_BYTES,
    )
    reservation, error = revisions._parse_reservation_body(reservation_body)
    assert error is None
    assert reservation is not None
    intent_body, _raw = _checked_body(
        "seed_intent.json",
        revisions._SEED_INTENT_CHECKSUM_DOMAIN,
        revisions._MAX_JOURNAL_BYTES,
    )
    intent, error = revisions._seed_intent_from_body(intent_body)
    assert error is None
    assert intent is not None
    binding_body, _raw = _checked_body(
        "seed_binding.json",
        revisions._SEED_BINDING_CHECKSUM_DOMAIN,
        revisions._MAX_MANIFEST_BYTES,
    )
    binding, error = revisions._seed_binding_from_body(binding_body)
    assert error is None
    assert binding is not None
    assert reservation["kind"] == "candidate"
    assert reservation["state"] == "staged"
    assert reservation["ceiling_files"] == 9
    assert reservation["revision_temp"] is None
    for value in (intent, binding):
        assert value["candidate_revision"] == reservation["revision_id"]
        assert value["expected_head"] == reservation["expected_head"]
        assert value["key_sha256"] == reservation["key_sha256"]
    source = binding["source_revision"]
    assert source.id != binding["expected_head"].revision_id
    assert source.model is not None
    assert (source.model.sha256, source.artifacts[0].sha256) == (
        hashlib.sha256(MODEL_BYTES).hexdigest(),
        hashlib.sha256(STEP_BYTES).hexdigest(),
    )


def test_task_vectors_round_trip_and_bind_draft_report_artifacts() -> None:
    expected = {
        "task_active.json": (6, TaskStatus.EXECUTING),
        "task_awaiting_review.json": (10, TaskStatus.AWAITING_USER_REVIEW),
        "task_cancelled.json": (9, TaskStatus.CANCELLED),
        "task_failed.json": (8, TaskStatus.FAILED),
        "task_rejected.json": (11, TaskStatus.REJECTED),
        "task_succeeded.json": (12, TaskStatus.SUCCEEDED),
    }
    decoded = {}
    for name, (generation, status) in expected.items():
        raw = _member_bytes(name)
        stored = task_store._decode_record(raw, TASK_ID)
        assert stored.generation == generation
        assert stored.task_run.status is status
        assert task_store._encode_record(stored.task_run, stored.generation) == raw
        decoded[name] = stored.task_run

    sealed = _manifest("sealed_revision_manifest.json")
    imported_head = _head("generation_zero_import_head.json")
    for task in decoded.values():
        assert task.id == TASK_ID
        assert task.project_id == PROJECT_ID
        assert task.base_revision == imported_head.revision_id
        assert task.candidate_revision == sealed.id
        assert tuple(
            (item.id, item.name, item.format, item.sha256, item.size_bytes)
            for item in task.artifacts
        ) == tuple(
            (item.id, item.name, item.format, item.sha256, item.size_bytes)
            for item in (sealed.model, *sealed.artifacts)
        )

    for name in (
        "task_awaiting_review.json",
        "task_rejected.json",
        "task_succeeded.json",
    ):
        task = decoded[name]
        assert task.draft is not None
        assert len(task.verification_reports) == 1
        report = task.verification_reports[0]
        assert task.draft.task_id == task.id
        assert task.draft.project_id == task.project_id
        assert task.draft.base_revision == task.base_revision
        assert task.draft.revision_id == task.candidate_revision == sealed.id
        assert task.draft.manifest_sha256 == report.manifest_sha256
        assert task.draft.verification_id == report.id
        assert task.draft.acceptance_id == report.acceptance_id
        assert task.draft.observation_digest == report.observation_digest
        assert report.manifest_sha256 == sealed.manifest_sha256
        assert report.passed is True
        verdict = report.verdicts[0]
        expected_digests = {
            "model_sha256": hashlib.sha256(MODEL_BYTES).hexdigest(),
            "step_sha256": hashlib.sha256(STEP_BYTES).hexdigest(),
        }
        assert verdict.expected == verdict.observed
        assert dict(verdict.expected) == expected_digests
    assert decoded["task_succeeded.json"].committed_revision == sealed.id
    assert decoded["task_failed.json"].last_error is not None
    assert decoded["task_failed.json"].last_error.code == "synthetic_failure"
    assert decoded["task_rejected.json"].committed_revision is None
    assert tuple(item.event.value for item in decoded["task_cancelled.json"].transitions[-3:]) == (
        "request_cancel",
        "start_cancellation",
        "confirm_cancelled",
    )


def test_materialization_request_and_delivery_round_trip_and_cross_bind() -> None:
    request_raw = _member_bytes("materialization_request.json")
    request = artifacts._parse_record(request_raw)
    assert request.phase is ArtifactRequestPhase.RESERVED
    assert request.eligibility.source_kind is ArtifactSourceKind.DRAFT
    assert artifacts._record_envelope(request) == request_raw

    awaiting = task_store._decode_record(_member_bytes("task_awaiting_review.json"), TASK_ID)
    draft = awaiting.task_run.draft
    assert draft is not None
    sealed = _manifest("sealed_revision_manifest.json")
    eligibility = request.eligibility
    assert eligibility.task_id == awaiting.task_run.id
    assert eligibility.task_generation == awaiting.generation
    assert eligibility.project_id == awaiting.task_run.project_id
    assert eligibility.revision_id == draft.revision_id == sealed.id
    assert eligibility.manifest_sha256 == draft.manifest_sha256 == sealed.manifest_sha256
    assert eligibility.draft_id == draft.id
    assert eligibility.artifacts == (sealed.model, *sealed.artifacts)
    assert request.materialization_id == artifacts._materialization_id(eligibility)
    assert request.delivery_manifest_sha256 == artifacts._delivery_manifest_digest(eligibility)

    delivery_raw = _member_bytes("materialization_delivery.json")
    delivery = _member_json("materialization_delivery.json")
    assert set(delivery) == {"schema_version", "body", "body_sha256"}
    assert delivery["schema_version"] == 1
    assert delivery["body"] == artifacts._delivery_manifest_body(eligibility)
    assert delivery["body_sha256"] == request.delivery_manifest_sha256
    assert artifacts._canonical_json(delivery) == delivery_raw
    expected_digest = hashlib.sha256(
        artifacts._DELIVERY_MANIFEST_DOMAIN + artifacts._canonical_json(delivery["body"])
    ).hexdigest()
    assert expected_digest == delivery["body_sha256"]


def test_project_create_key_request_and_quarantine_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_record = _member_json("project_create_hmac_key.json")
    assert set(key_record) == {"schema_version", "key_hex", "key_id"}
    assert key_record["schema_version"] == 1
    key = bytes.fromhex(key_record["key_hex"])
    assert key == bytes(range(32))
    assert key_record["key_id"] == project_create._digest(project_create._KEY_ID_DOMAIN, key)

    request_raw = _member_bytes("project_create_request.json")
    with monkeypatch.context() as patch:
        patch.setattr(project_create.os, "geteuid", lambda: 501)
        record = project_create._record_from_bytes(
            request_raw,
            expected_name=project_create._request_name(CREATE_KEY),
        )
        assert project_create._record_bytes(record) == request_raw
        assert record.kind is ProjectKind.IMPORT_FCSTD
        assert record.phase == "PUBLISHED"
        assert record.outcome == "PUBLISHED"
        assert record.key_id == key_record["key_id"]
        assert record.intent_hmac == project_create._intent_digest(
            key,
            create_key=CREATE_KEY,
            kind=ProjectKind.IMPORT_FCSTD,
            source_path=None,
            source_locator=SYNTHETIC_SOURCE_LOCATOR,
        )
        assert record.generation_zero is not None
        imported = _manifest("generation_zero_import_manifest.json")
        imported_head = _head("generation_zero_import_head.json")
        assert record.generation_zero.revision == imported
        assert record.generation_zero.head == imported_head

        quarantine_json = _member_json("project_create_quarantine_receipt.json")
        body = quarantine_json["body"]
        binding = project_create._binding_from_mapping(
            body["binding"],
            project_create._STAGE_NAME,
            minimum_size=0,
        )
        assert binding is not None
        receipt_name = project_create._quarantine_receipt_name(binding)
        receipt_raw = _member_bytes("project_create_quarantine_receipt.json")
        receipt = project_create._quarantine_receipt_from_bytes(
            receipt_raw,
            expected_name=receipt_name,
        )
        assert project_create._quarantine_receipt_bytes(receipt) == receipt_raw
        assert receipt.original_name == f".stage.{record.intent_hmac[:32]}.FCStd"
        assert receipt.binding.sha256 == hashlib.sha256(MODEL_BYTES).hexdigest()
        assert receipt.binding.size == len(MODEL_BYTES)
        assert receipt.quarantine_name == project_create._quarantine_file_name(receipt.binding)


class _RawCheckoutRoot:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def read_file_at(self, _root_fd: int, _name: str, *, maximum: int):
        assert len(self.raw) <= maximum
        return self.raw, object()

    def verify_file_entry(
        self,
        _root_fd: int,
        _name: str,
        *,
        expected: object,
        maximum: int,
    ) -> None:
        assert expected is not None
        assert len(self.raw) <= maximum


def test_checkout_legacy_and_current_open_tombstone_facts_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codec = object.__new__(checkouts.ManagedCheckoutStore)
    sealed = _manifest("sealed_revision_manifest.json")
    sealed_head = _head("sealed_revision_head.json")
    with monkeypatch.context() as patch:
        patch.setattr(checkouts.os, "geteuid", lambda: 501)
        legacy_open_raw = _member_bytes("checkout_open_v1.json")
        legacy_open = codec._decode_open(legacy_open_raw)
        current_open_raw = _member_bytes("checkout_open_v2.json")
        current_open = codec._decode_open(current_open_raw)
        assert legacy_open.checkout_id == current_open.checkout_id == CHECKOUT_ID
        assert legacy_open.source == current_open.source
        assert legacy_open.source_head is None and legacy_open.source_binding is None
        assert current_open.source_head == sealed_head
        assert current_open.source_binding is not None
        assert current_open.source_binding.size == len(MODEL_BYTES)
        assert codec._encode_open(current_open) == current_open_raw
        for name, raw, domain in (
            ("checkout_open_v1.json", legacy_open_raw, checkouts._RECORD_DOMAIN),
            ("checkout_open_v2.json", current_open_raw, checkouts._RECORD_DOMAIN_V2),
        ):
            value = _member_json(name)
            checksum = value.pop("checksum")
            assert checksum == hashlib.sha256(domain + checkouts._canonical(value)).hexdigest()
            assert checkouts._encode_record(value, domain) == raw

        tombstone_name = f"closed_{CHECKOUT_ID}.json"
        closed = {}
        for fixture, domain in (
            ("checkout_tombstone_v1.json", checkouts._TOMBSTONE_DOMAIN),
            ("checkout_tombstone_v2.json", checkouts._TOMBSTONE_DOMAIN_V2),
        ):
            raw = _member_bytes(fixture)
            codec._root = _RawCheckoutRoot(raw)
            record = codec._load_tombstone_name(0, tombstone_name)
            closed[fixture] = record
            value = _member_json(fixture)
            checksum = value.pop("checksum")
            assert checksum == hashlib.sha256(domain + checkouts._canonical(value)).hexdigest()
            assert checkouts._encode_record(value, domain) == raw
            assert record.descriptor.checkout_id == CHECKOUT_ID
            assert record.descriptor.source.revision_id == sealed.id
            assert record.descriptor.current_model_sha256 == hashlib.sha256(MODEL_BYTES).hexdigest()
            assert record.descriptor.current_size_bytes == len(MODEL_BYTES)
        assert closed["checkout_tombstone_v1.json"].descriptor.source_head is None
        assert closed["checkout_tombstone_v1.json"].descriptor.source_binding is None
        assert closed["checkout_tombstone_v2.json"].descriptor.source_head == sealed_head
        assert closed["checkout_tombstone_v2.json"].descriptor.source_binding is not None


def test_corpus_test_has_no_mutation_switch() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    forbidden = (
        "arg" + "parse",
        "os." + "environ",
        "sys." + "argv",
        "--" + "update",
        "--" + "generate",
        "update_" + "golden",
    )
    assert all(token not in source for token in forbidden)
