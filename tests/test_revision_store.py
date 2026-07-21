from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import re
import socket
import stat
import threading
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

import vibecad.execution as execution_package
import vibecad.execution.revisions as revisions_module
from vibecad.execution.revisions import (
    CommitJournal,
    CommitJournalState,
    LocalRevisionStore,
    ProjectHead,
    ReconciliationResult,
    ReconciliationStatus,
    RevisionArtifactRef,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
)
from vibecad.workflow.lease import (
    LeaseError,
    LeaseErrorCode,
    LeaseRootTrust,
    ProjectWriteLease,
    ResourceLeaseManager,
)

SCHEMA_VERSION = 1
PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
OTHER_PROJECT_ID = "project_11111111111111111111111111111111"
REVISION_A = "revision_0123456789abcdef0123456789abcdef"
REVISION_B = "revision_11111111111111111111111111111111"
REVISION_C = "revision_22222222222222222222222222222222"
ARTIFACT_MODEL = "artifact_0123456789abcdef0123456789abcdef"
ARTIFACT_STEP = "artifact_11111111111111111111111111111111"
TRANSACTION_ID = "transaction_0123456789abcdef0123456789abcdef"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class ExplosiveInput:
    def __init__(self) -> None:
        self.protocol_calls: list[str] = []

    def _explode(self, protocol: str):
        self.protocol_calls.append(protocol)
        raise AssertionError("untrusted input protocol must not execute")

    def __fspath__(self) -> str:
        return self._explode("__fspath__")

    def __iter__(self):
        return self._explode("__iter__")

    def __eq__(self, other):
        return self._explode("__eq__")

    def __getitem__(self, key):
        return self._explode("__getitem__")

    def __contains__(self, item):
        return self._explode("__contains__")

    def __bool__(self) -> bool:
        return self._explode("__bool__")

    def __len__(self) -> int:
        return self._explode("__len__")

    def __hash__(self) -> int:
        return self._explode("__hash__")

    def __format__(self, format_spec: str) -> str:
        return self._explode("__format__")

    def __str__(self) -> str:
        return self._explode("__str__")

    def __bytes__(self) -> bytes:
        return self._explode("__bytes__")

    def __index__(self) -> int:
        return self._explode("__index__")

    def __int__(self) -> int:
        return self._explode("__int__")


PROJECT_PATH_DOMAIN = b"vibecad-revision-project-path-v1\0"
REVISION_PATH_DOMAIN = b"vibecad-revision-content-path-v1\0"
CANDIDATE_PATH_DOMAIN = b"vibecad-revision-candidate-path-v1\0"
MANIFEST_CHECKSUM_DOMAIN = b"vibecad-revision-manifest-v1\0"
HEAD_CHECKSUM_DOMAIN = b"vibecad-project-head-v1\0"
JOURNAL_CHECKSUM_DOMAIN = b"vibecad-commit-journal-v1\0"

EXPECTED_EXECUTION_EXPORTS = [
    "DEFAULT_OPERATION_REGISTRY",
    "EntityIdentity",
    "EntityKind",
    "ExecutionProfile",
    "FieldMetadata",
    "OperationMetadata",
    "OperationRegistry",
    "Provenance",
    "ProvenanceSource",
    "RegistryError",
    "RegistryErrorCode",
    "ResourceBudget",
    "ResultSlotMetadata",
    "RiskClass",
    "SelectorError",
    "SelectorErrorCode",
    "SelectorV1",
    "SemanticRole",
    "ValueShape",
    "encode_provenance_metadata",
    "index_entity_identities",
    "parse_entity_identity",
    "resolve_selector",
]
EXPECTED_REVISION_EXPORTS = (
    "CommitJournal",
    "CommitJournalState",
    "LocalRevisionStore",
    "ProjectHead",
    "ReconciliationResult",
    "ReconciliationStatus",
    "RevisionArtifactRef",
    "RevisionRef",
    "RevisionStoreError",
    "RevisionStoreErrorCode",
    "RevisionStoreRootTrust",
)
EXPECTED_STORE_METHODS = {
    "begin_revision": ("self", "project_id", "expected_head", "lease"),
    "candidate_artifact_path": ("self", "project_id", "revision_id", "format", "lease"),
    "candidate_model_path": ("self", "project_id", "revision_id", "lease"),
    "commit_revision": ("self", "project_id", "expected_head", "revision_id", "lease"),
    "import_trusted_fcstd": (
        "self",
        "project_id",
        "source",
        "expected_sha256",
        "expected_size",
        "lease",
    ),
    "initialize_empty_project": ("self", "project_id", "lease"),
    "load_head": ("self", "project_id"),
    "load_revision": ("self", "project_id", "revision_id"),
    "prepare_revision": (
        "self",
        "project_id",
        "expected_head",
        "revision_id",
        "manifest_sha256",
        "lease",
    ),
    "reconcile": ("self", "project_id", "lease"),
    "revision_artifact_path": ("self", "project_id", "revision_id", "artifact_id"),
    "revision_model_path": ("self", "project_id", "revision_id"),
    "rollback_revision": ("self", "project_id", "revision_id", "lease"),
    "seal_revision": ("self", "project_id", "revision_id", "lease"),
    "validate_project_write_lease": ("self", "project_id", "lease"),
}
EXPECTED_VALUE_FIELDS = {
    "CommitJournal": (
        "schema_version",
        "id",
        "project_id",
        "expected_head",
        "candidate_revision",
        "manifest_sha256",
        "state",
    ),
    "ProjectHead": (
        "schema_version",
        "project_id",
        "generation",
        "revision_id",
        "manifest_sha256",
    ),
    "ReconciliationResult": (
        "schema_version",
        "project_id",
        "status",
        "head",
        "journal",
    ),
    "RevisionArtifactRef": (
        "schema_version",
        "id",
        "name",
        "format",
        "sha256",
        "size_bytes",
    ),
    "RevisionRef": (
        "schema_version",
        "id",
        "project_id",
        "base_revision",
        "manifest_sha256",
        "model",
        "artifacts",
    ),
}
EXPECTED_ENUM_MEMBERS = {
    "CommitJournalState": {
        "STAGING": "staging",
        "PREPARED": "prepared",
        "COMMITTED": "committed",
        "NOT_COMMITTED": "not_committed",
    },
    "ReconciliationStatus": {
        "CLEAN": "clean",
        "COMMITTED": "committed",
        "NOT_COMMITTED": "not_committed",
        "CLEANUP_REQUIRED": "cleanup_required",
    },
    "RevisionStoreErrorCode": {
        "INVALID_IDENTIFIER": "invalid_identifier",
        "INVALID_INPUT": "invalid_input",
        "NOT_FOUND": "not_found",
        "ALREADY_EXISTS": "already_exists",
        "CONFLICT": "conflict",
        "CORRUPT_RECORD": "corrupt_record",
        "CORRUPT_CONTENT": "corrupt_content",
        "BUDGET_EXCEEDED": "budget_exceeded",
        "UNSAFE_STORE": "unsafe_store",
        "INVALID_LEASE": "invalid_lease",
        "IO_ERROR": "io_error",
        "DURABILITY_UNCERTAIN": "durability_uncertain",
        "RECOVERY_REQUIRED": "recovery_required",
        "CLEANUP_REQUIRED": "cleanup_required",
    },
    "RevisionStoreRootTrust": {"TRUSTED_LOCAL": "trusted_local"},
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _checked_record(body: dict[str, object], domain: bytes) -> bytes:
    checksum = hashlib.sha256(domain + _canonical(body)).hexdigest()
    return _canonical({**body, "checksum": checksum})


def _write_checked_record(path: Path, body: dict[str, object], domain: bytes) -> bytes:
    raw = _checked_record(body, domain)
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return raw


def _assert_closed_error(
    error: RevisionStoreError,
    code: RevisionStoreErrorCode,
    *,
    head_committed: bool | None = None,
) -> None:
    assert error.code is code
    assert str(error) == error.message
    assert error.args == (error.message,)
    assert len(error.message) <= 128
    assert "SECRET" not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    if head_committed is None:
        assert not hasattr(error, "head_committed")
    else:
        assert error.head_committed is head_committed


def _path_key(domain: bytes, identifier: str) -> str:
    return hashlib.sha256(domain + identifier.encode("utf-8")).hexdigest()


def _project_dir(root: Path, project_id: str = PROJECT_ID) -> Path:
    return root / _path_key(PROJECT_PATH_DOMAIN, project_id)


def _revision_dir(root: Path, revision_id: str, project_id: str = PROJECT_ID) -> Path:
    return (
        _project_dir(root, project_id) / "revisions" / _path_key(REVISION_PATH_DOMAIN, revision_id)
    )


def _candidate_dir(root: Path, revision_id: str, project_id: str = PROJECT_ID) -> Path:
    return (
        _project_dir(root, project_id)
        / "candidates"
        / _path_key(CANDIDATE_PATH_DOMAIN, revision_id)
    )


def _rewrite_candidate_lineage(
    root: Path,
    revision_id: str,
    base_revision: str,
    state: CommitJournalState,
) -> str:
    manifest_path = _revision_dir(root, revision_id) / "manifest.json"
    manifest_body = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_body.pop("checksum")
    manifest_body["base_revision"] = base_revision
    manifest_raw = _write_checked_record(
        manifest_path,
        manifest_body,
        MANIFEST_CHECKSUM_DOMAIN,
    )
    manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
    journal_path = _project_dir(root) / "journal.json"
    journal_body = json.loads(journal_path.read_text(encoding="utf-8"))
    journal_body.pop("checksum")
    journal_body["manifest_sha256"] = manifest_digest
    journal_body["state"] = state.value
    _write_checked_record(journal_path, journal_body, JOURNAL_CHECKSUM_DOMAIN)
    return manifest_digest


def _write_test_head(
    root: Path,
    old_head: ProjectHead,
    revision_id: str,
    manifest_digest: str,
) -> ProjectHead:
    head = ProjectHead(
        project_id=old_head.project_id,
        generation=old_head.generation + 1,
        revision_id=revision_id,
        manifest_sha256=manifest_digest,
    )
    _write_checked_record(
        _project_dir(root) / "HEAD.json",
        head.to_mapping(),
        HEAD_CHECKSUM_DOMAIN,
    )
    return head


def _artifact(
    *,
    identifier: str = ARTIFACT_STEP,
    name: str = "model.step",
    fmt: str = "step",
    digest: str = DIGEST_B,
    size: int = 11,
) -> RevisionArtifactRef:
    return RevisionArtifactRef(
        id=identifier,
        name=name,
        format=fmt,
        sha256=digest,
        size_bytes=size,
    )


def _model() -> RevisionArtifactRef:
    return _artifact(
        identifier=ARTIFACT_MODEL,
        name="model.FCStd",
        fmt="fcstd",
        digest=DIGEST_A,
        size=13,
    )


def _revision(
    *,
    identifier: str = REVISION_B,
    project_id: str = PROJECT_ID,
    base_revision: str | None = REVISION_A,
    manifest_sha256: str = DIGEST_A,
    model: RevisionArtifactRef | None = None,
    artifacts: tuple[RevisionArtifactRef, ...] | None = None,
) -> RevisionRef:
    if model is None:
        model = _model()
    if artifacts is None:
        artifacts = (_artifact(),)
    return RevisionRef(
        id=identifier,
        project_id=project_id,
        base_revision=base_revision,
        manifest_sha256=manifest_sha256,
        model=model,
        artifacts=artifacts,
    )


def _head(
    *,
    project_id: str = PROJECT_ID,
    generation: int = 1,
    revision_id: str = REVISION_B,
    manifest_sha256: str = DIGEST_A,
) -> ProjectHead:
    return ProjectHead(
        project_id=project_id,
        generation=generation,
        revision_id=revision_id,
        manifest_sha256=manifest_sha256,
    )


def _journal(
    *,
    state: CommitJournalState = CommitJournalState.PREPARED,
    manifest_sha256: str | None = DIGEST_B,
) -> CommitJournal:
    return CommitJournal(
        id=TRANSACTION_ID,
        project_id=PROJECT_ID,
        expected_head=_head(
            generation=0,
            revision_id=REVISION_A,
            manifest_sha256=DIGEST_A,
        ),
        candidate_revision=REVISION_B,
        manifest_sha256=manifest_sha256,
        state=state,
    )


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    store_root = tmp_path / "revision-store"
    lock_root = tmp_path / "revision-locks"
    store_root.mkdir(mode=0o700)
    lock_root.mkdir(mode=0o700)
    os.chmod(store_root, 0o700)
    os.chmod(lock_root, 0o700)
    return store_root, lock_root


@pytest.fixture
def store_parts(
    roots: tuple[Path, Path],
) -> tuple[LocalRevisionStore, ResourceLeaseManager, Path]:
    store_root, lock_root = roots
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    store = LocalRevisionStore(
        store_root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    return store, manager, store_root


def _initialize_empty(
    store: LocalRevisionStore,
    manager: ResourceLeaseManager,
    project_id: str = PROJECT_ID,
) -> ProjectHead:
    with manager.acquire_project_write(project_id) as lease:
        return store.initialize_empty_project(project_id, lease)


def _initialize_imported(
    store: LocalRevisionStore,
    manager: ResourceLeaseManager,
    source: Path,
    project_id: str = PROJECT_ID,
) -> ProjectHead:
    raw = source.read_bytes()
    with manager.acquire_project_write(project_id) as lease:
        return store.import_trusted_fcstd(
            project_id,
            source,
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            lease,
        )


def _import_trusted(
    store: LocalRevisionStore,
    project_id: str,
    source: str | Path,
    lease: ProjectWriteLease,
) -> ProjectHead:
    raw = Path(source).read_bytes()
    return store.import_trusted_fcstd(
        project_id,
        source,
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        lease,
    )


def _begin_and_fill(
    store: LocalRevisionStore,
    lease: ProjectWriteLease,
    head: ProjectHead,
    *,
    model: bytes = b"changed-fcstd",
    step: bytes = b"ISO-10303-21;STEP;ENDSEC;",
) -> str:
    revision_id = store.begin_revision(PROJECT_ID, head, lease)
    model_path = store.candidate_model_path(PROJECT_ID, revision_id, lease)
    step_path = store.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
    model_path.write_bytes(model)
    step_path.write_bytes(step)
    return revision_id


def _all_tree_bytes(root: Path) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path.relative_to(root))] = path.read_bytes()
    return result


def test_public_surface_is_direct_module_only_and_exact():
    assert revisions_module.__all__ == EXPECTED_REVISION_EXPORTS
    assert execution_package.__all__ == EXPECTED_EXECUTION_EXPORTS
    for name in EXPECTED_REVISION_EXPORTS:
        assert getattr(revisions_module, name) is globals()[name]
        assert name not in execution_package.__all__


def test_local_revision_store_method_surface_and_signatures_are_exact():
    public = {name for name in dir(LocalRevisionStore) if not name.startswith("_")}
    assert public == set(EXPECTED_STORE_METHODS)
    constructor = inspect.signature(LocalRevisionStore)
    assert tuple(constructor.parameters) == ("root", "lease_manager", "trust")
    assert constructor.parameters["trust"].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(
        parameter.default is inspect.Parameter.empty
        for parameter in constructor.parameters.values()
    )
    for name, expected_parameters in EXPECTED_STORE_METHODS.items():
        signature = inspect.signature(getattr(LocalRevisionStore, name))
        assert tuple(signature.parameters) == expected_parameters
        assert all(
            parameter.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
            for parameter in signature.parameters.values()
        )
        assert all(
            parameter.default is inspect.Parameter.empty
            for parameter in signature.parameters.values()
        )
        assert signature.return_annotation is inspect.Signature.empty


def test_public_value_types_are_frozen_slotted_keyword_only_and_exact():
    values = (
        _artifact(),
        _revision(),
        _head(),
        _journal(),
        ReconciliationResult(
            project_id=PROJECT_ID,
            status=ReconciliationStatus.COMMITTED,
            head=_head(manifest_sha256=DIGEST_B),
            journal=replace(_journal(), state=CommitJournalState.COMMITTED),
        ),
    )
    for value in values:
        expected_fields = EXPECTED_VALUE_FIELDS[type(value).__name__]
        assert tuple(field.name for field in fields(value)) == expected_fields
        constructor = inspect.signature(type(value))
        assert tuple(constructor.parameters) == expected_fields
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in constructor.parameters.values()
        )
        for name, parameter in constructor.parameters.items():
            if name == "schema_version":
                assert parameter.default == SCHEMA_VERSION
            else:
                assert parameter.default is inspect.Parameter.empty
        with pytest.raises((FrozenInstanceError, AttributeError)):
            value.schema_version = 2
        with pytest.raises((FrozenInstanceError, AttributeError)):
            value.extra = True
        assert "__dict__" not in dir(value)
        assert type(type(value).from_mapping(value.to_mapping())) is type(value)
        assert type(value).from_mapping(value.to_mapping()) == value
        assert type(value).from_mapping(value.to_mapping()) is not value
    with pytest.raises(TypeError):
        RevisionArtifactRef(ARTIFACT_STEP, "model.step", "step", DIGEST_A, 1)


def test_public_enum_members_names_and_values_are_exact_and_closed():
    enums = (
        CommitJournalState,
        ReconciliationStatus,
        RevisionStoreErrorCode,
        RevisionStoreRootTrust,
    )
    for enum_type in enums:
        assert {
            name: member.value for name, member in enum_type.__members__.items()
        } == EXPECTED_ENUM_MEMBERS[enum_type.__name__]


@pytest.mark.parametrize(
    ("factory", "mapping"),
    [
        (_artifact, _artifact().to_mapping()),
        (_revision, _revision().to_mapping()),
        (_head, _head().to_mapping()),
        (_journal, _journal().to_mapping()),
        (
            lambda: ReconciliationResult(
                project_id=PROJECT_ID,
                status=ReconciliationStatus.CLEAN,
                head=_head(),
                journal=None,
            ),
            ReconciliationResult(
                project_id=PROJECT_ID,
                status=ReconciliationStatus.CLEAN,
                head=_head(),
                journal=None,
            ).to_mapping(),
        ),
    ],
)
def test_mappings_are_fresh_strict_json_values(factory, mapping):
    value = factory()
    first = value.to_mapping()
    second = value.to_mapping()
    assert first == mapping == second
    assert first is not second
    first["schema_version"] = 99
    assert value.to_mapping() == mapping
    parser = type(value).from_mapping
    for malformed in (None, [], (), object(), type("DictProxy", (dict,), {})(mapping)):
        with pytest.raises(RevisionStoreError) as captured:
            parser(malformed)
        assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT
    for changed in (
        {**mapping, "extra": True},
        {key: item for key, item in mapping.items() if key != "schema_version"},
        {**mapping, "schema_version": True},
        {**mapping, "schema_version": 2},
    ):
        with pytest.raises(RevisionStoreError) as captured:
            parser(changed)
        assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT


def test_nested_mappings_are_defensive_and_require_exact_containers():
    revision = _revision()
    mapping = revision.to_mapping()
    assert type(mapping["model"]) is dict
    assert type(mapping["artifacts"]) is list
    parsed = RevisionRef.from_mapping(mapping)
    mapping["model"]["sha256"] = "c" * 64
    mapping["artifacts"][0]["sha256"] = "d" * 64
    assert parsed == revision
    for artifacts in (tuple(revision.to_mapping()["artifacts"]), {"0": _artifact().to_mapping()}):
        changed = revision.to_mapping()
        changed["artifacts"] = artifacts
        with pytest.raises(RevisionStoreError) as captured:
            RevisionRef.from_mapping(changed)
        assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "identifier",
    [
        "",
        "project_bad",
        "PROJECT_0123456789abcdef0123456789abcdef",
        "project_0123456789ABCDEF0123456789abcdef",
        "project_0123456789abcdef0123456789abcde",
        "project_0123456789abcdef0123456789abcdef0",
        "../project_0123456789abcdef0123456789abcdef",
        "project_0123456789abcdef/0123456789abcdef",
        "project_0123456789abcdef\\0123456789abcdef",
        "project_０123456789abcdef0123456789abcdef",
        "project_0123456789abcdef0123456789abcde\n",
    ],
)
def test_project_identifiers_are_canonical(identifier):
    with pytest.raises(RevisionStoreError) as captured:
        ProjectHead(
            project_id=identifier,
            generation=0,
            revision_id=REVISION_A,
            manifest_sha256=DIGEST_A,
        )
    assert captured.value.code is RevisionStoreErrorCode.INVALID_IDENTIFIER


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "artifact_bad"),
        ("name", "../model.step"),
        ("name", "dir/model.step"),
        ("name", "dir\\model.step"),
        ("name", "."),
        ("name", ".."),
        ("name", "model.step\n"),
        ("format", "stl"),
        ("sha256", "A" * 64),
        ("sha256", "a" * 63),
        ("size_bytes", True),
        ("size_bytes", 0),
        ("size_bytes", -1),
        ("size_bytes", 2**53),
    ],
)
def test_artifact_contract_rejects_invalid_values(field, value):
    fields = {
        "id": ARTIFACT_STEP,
        "name": "model.step",
        "format": "step",
        "sha256": DIGEST_A,
        "size_bytes": 1,
    }
    fields[field] = value
    with pytest.raises(RevisionStoreError) as captured:
        RevisionArtifactRef(**fields)
    expected = RevisionStoreErrorCode.INVALID_INPUT
    if field == "id":
        expected = RevisionStoreErrorCode.INVALID_IDENTIFIER
    elif field == "size_bytes" and value == 2**53:
        expected = RevisionStoreErrorCode.BUDGET_EXCEEDED
    assert captured.value.code is expected


def test_revision_contract_freezes_empty_imported_and_candidate_invariants():
    empty = RevisionRef(
        id=REVISION_A,
        project_id=PROJECT_ID,
        base_revision=None,
        manifest_sha256=DIGEST_A,
        model=None,
        artifacts=(),
    )
    imported = replace(empty, model=_model())
    candidate = _revision()
    assert empty.model is None and empty.artifacts == ()
    assert imported.model == _model() and imported.artifacts == ()
    assert candidate.base_revision == REVISION_A
    assert candidate.model == _model()
    assert candidate.artifacts == (_artifact(),)
    invalid = []
    for source, changes in (
        (empty, {"artifacts": [_artifact().to_mapping()]}),
        (empty, {"base_revision": REVISION_C}),
        (imported, {"artifacts": [_artifact().to_mapping()]}),
        (candidate, {"model": None}),
        (candidate, {"artifacts": []}),
        (
            candidate,
            {
                "artifacts": [
                    _artifact().to_mapping(),
                    _artifact(identifier=ARTIFACT_MODEL).to_mapping(),
                ]
            },
        ),
        (candidate, {"model": _artifact().to_mapping()}),
        (candidate, {"artifacts": [_artifact(name="other.step").to_mapping()]}),
    ):
        mapping = source.to_mapping()
        mapping.update(changes)
        invalid.append(mapping)
    for mapping in invalid:
        with pytest.raises(RevisionStoreError) as captured:
            RevisionRef.from_mapping(mapping)
        assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT


def test_revision_contract_rejects_self_based_lineage_direct_and_from_mapping():
    revision = _revision()
    with pytest.raises(RevisionStoreError) as captured:
        replace(revision, base_revision=revision.id)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_INPUT)
    mapping = revision.to_mapping()
    mapping["base_revision"] = revision.id
    with pytest.raises(RevisionStoreError) as captured:
        RevisionRef.from_mapping(mapping)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_INPUT)


def test_head_journal_and_reconciliation_cross_field_invariants():
    head = _head()
    assert head.generation == 1
    for changes in (
        {"generation": True},
        {"generation": -1},
        {"generation": 2**53},
        {"revision_id": REVISION_A, "manifest_sha256": None},
    ):
        fields = head.to_mapping()
        fields.update(changes)
        with pytest.raises(RevisionStoreError) as captured:
            ProjectHead.from_mapping(fields)
        assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT

    staging = _journal(state=CommitJournalState.STAGING, manifest_sha256=None)
    prepared = _journal()
    committed = replace(prepared, state=CommitJournalState.COMMITTED)
    not_committed = replace(prepared, state=CommitJournalState.NOT_COMMITTED)
    assert staging.manifest_sha256 is None
    assert prepared.manifest_sha256 == DIGEST_B
    assert committed.state is CommitJournalState.COMMITTED
    assert not_committed.state is CommitJournalState.NOT_COMMITTED
    with pytest.raises(RevisionStoreError) as captured:
        replace(staging, manifest_sha256=DIGEST_A)
    assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT
    with pytest.raises(RevisionStoreError) as captured:
        replace(prepared, manifest_sha256=None)
    assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT
    unknown_state = prepared.to_mapping()
    unknown_state["state"] = "future_state"
    with pytest.raises(RevisionStoreError) as captured:
        CommitJournal.from_mapping(unknown_state)
    assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT

    clean = ReconciliationResult(
        project_id=PROJECT_ID,
        status=ReconciliationStatus.CLEAN,
        head=head,
        journal=None,
    )
    assert ReconciliationResult.from_mapping(clean.to_mapping()) == clean
    for status, result_head, journal in (
        (
            ReconciliationStatus.COMMITTED,
            _head(manifest_sha256=DIGEST_B),
            committed,
        ),
        (
            ReconciliationStatus.NOT_COMMITTED,
            _head(generation=0, revision_id=REVISION_A, manifest_sha256=DIGEST_A),
            not_committed,
        ),
        (
            ReconciliationStatus.CLEANUP_REQUIRED,
            _head(generation=0, revision_id=REVISION_A, manifest_sha256=DIGEST_A),
            not_committed,
        ),
    ):
        result = ReconciliationResult(
            project_id=PROJECT_ID,
            status=status,
            head=result_head,
            journal=journal,
        )
        assert ReconciliationResult.from_mapping(result.to_mapping()) == result
    with pytest.raises(RevisionStoreError) as captured:
        replace(clean, journal=committed)
    assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT
    unknown_status = clean.to_mapping()
    unknown_status["status"] = "future_status"
    with pytest.raises(RevisionStoreError) as captured:
        ReconciliationResult.from_mapping(unknown_status)
    assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT


@pytest.mark.parametrize("generation", [0, 2])
def test_committed_reconciliation_requires_exact_single_generation_advance(generation: int):
    committed = replace(_journal(), state=CommitJournalState.COMMITTED)
    head = _head(generation=generation, manifest_sha256=DIGEST_B)
    with pytest.raises(RevisionStoreError) as captured:
        ReconciliationResult(
            project_id=PROJECT_ID,
            status=ReconciliationStatus.COMMITTED,
            head=head,
            journal=committed,
        )
    _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_INPUT)
    valid = ReconciliationResult(
        project_id=PROJECT_ID,
        status=ReconciliationStatus.COMMITTED,
        head=_head(generation=1, manifest_sha256=DIGEST_B),
        journal=committed,
    ).to_mapping()
    valid["head"]["generation"] = generation
    with pytest.raises(RevisionStoreError) as captured:
        ReconciliationResult.from_mapping(valid)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_INPUT)


def test_error_codes_messages_and_uncertainty_metadata_are_closed_and_redacted():
    expected_codes = {
        "invalid_identifier",
        "invalid_input",
        "not_found",
        "already_exists",
        "conflict",
        "corrupt_record",
        "corrupt_content",
        "budget_exceeded",
        "unsafe_store",
        "invalid_lease",
        "io_error",
        "durability_uncertain",
        "recovery_required",
        "cleanup_required",
    }
    assert {item.value for item in RevisionStoreErrorCode} == expected_codes
    for code in RevisionStoreErrorCode:
        if code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN:
            error = RevisionStoreError(code, head_committed=True)
            assert error.head_committed is True
        else:
            error = RevisionStoreError(code)
            assert not hasattr(error, "head_committed")
        assert error.code is code
        assert error.message
        assert str(error) == error.message
        assert len(str(error)) <= 128
        assert "SECRET" not in str(error)
    with pytest.raises(ValueError):
        RevisionStoreError(RevisionStoreErrorCode.DURABILITY_UNCERTAIN)
    with pytest.raises(ValueError):
        RevisionStoreError(RevisionStoreErrorCode.IO_ERROR, head_committed=False)


def test_root_constructor_is_explicit_exact_and_side_effect_free(roots):
    store_root, lock_root = roots
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    before = tuple(store_root.iterdir())
    store = LocalRevisionStore(
        store_root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    assert type(store) is LocalRevisionStore
    assert tuple(store_root.iterdir()) == before == ()
    with pytest.raises(RevisionStoreError) as captured:
        LocalRevisionStore(store_root, manager, trust="trusted_local")
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    with pytest.raises(TypeError):
        LocalRevisionStore(store_root, object(), trust=RevisionStoreRootTrust.TRUSTED_LOCAL)


def test_root_rejects_hostile_input_before_any_implicit_protocol(roots):
    _store_root, lock_root = roots
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    hostile = ExplosiveInput()
    with pytest.raises(RevisionStoreError) as captured:
        LocalRevisionStore(
            hostile,
            manager,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        )
    _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)
    assert hostile.protocol_calls == []


def test_exact_string_root_and_trusted_source_are_accepted(roots, tmp_path: Path):
    store_root, lock_root = roots
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    string_root = str(store_root)
    source = tmp_path / "string-source.FCStd"
    source_bytes = b"trusted string source"
    source.write_bytes(source_bytes)
    string_source = str(source)
    assert type(string_root) is str
    assert type(string_source) is str
    store = LocalRevisionStore(
        string_root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    with manager.acquire_project_write(PROJECT_ID) as lease:
        head = _import_trusted(store, PROJECT_ID, string_source, lease)
    assert store.revision_model_path(PROJECT_ID, head.revision_id).read_bytes() == source_bytes


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777, 0o600])
def test_root_requires_private_owned_directory(roots, mode):
    store_root, lock_root = roots
    os.chmod(store_root, mode)
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    with pytest.raises(RevisionStoreError) as captured:
        LocalRevisionStore(
            store_root,
            manager,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        )
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE


def test_missing_or_symlink_root_is_rejected(tmp_path: Path):
    lock_root = tmp_path / "locks"
    lock_root.mkdir(mode=0o700)
    os.chmod(lock_root, 0o700)
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    missing = tmp_path / "missing"
    with pytest.raises(RevisionStoreError) as captured:
        LocalRevisionStore(missing, manager, trust=RevisionStoreRootTrust.TRUSTED_LOCAL)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RevisionStoreError) as captured:
        LocalRevisionStore(link, manager, trust=RevisionStoreRootTrust.TRUSTED_LOCAL)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE


def test_empty_project_is_a_real_pathless_initial_revision(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    assert head.project_id == PROJECT_ID
    assert head.generation == 0
    assert re.fullmatch(r"revision_[0-9a-f]{32}", head.revision_id)
    assert re.fullmatch(r"[0-9a-f]{64}", head.manifest_sha256)
    assert store.load_head(PROJECT_ID) == head

    revision = store.load_revision(PROJECT_ID, head.revision_id)
    assert revision.id == head.revision_id
    assert revision.project_id == PROJECT_ID
    assert revision.base_revision is None
    assert revision.manifest_sha256 == head.manifest_sha256
    assert revision.model is None
    assert revision.artifacts == ()
    with pytest.raises(RevisionStoreError) as captured:
        store.revision_model_path(PROJECT_ID, head.revision_id)
    assert captured.value.code is RevisionStoreErrorCode.NOT_FOUND

    project_dir = _project_dir(root)
    revision_dir = _revision_dir(root, head.revision_id)
    assert project_dir.is_dir()
    assert revision_dir.is_dir()
    assert PROJECT_ID not in project_dir.name
    assert head.revision_id not in revision_dir.name
    assert stat.S_IMODE(project_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(revision_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((project_dir / "HEAD.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((revision_dir / "manifest.json").stat().st_mode) == 0o600


def test_empty_initialization_is_exclusive_and_loads_are_strict(store_parts):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            store.initialize_empty_project(PROJECT_ID, lease)
        assert captured.value.code is RevisionStoreErrorCode.ALREADY_EXISTS
    assert store.load_head(PROJECT_ID) == head
    for invalid in ("project_bad", PROJECT_ID.upper(), True, None):
        with pytest.raises(RevisionStoreError) as captured:
            store.load_head(invalid)
        assert captured.value.code is RevisionStoreErrorCode.INVALID_IDENTIFIER
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(OTHER_PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.NOT_FOUND


def test_trusted_fcstd_import_copies_bytes_and_never_persists_source_path(
    store_parts, tmp_path: Path
):
    store, manager, root = store_parts
    source_dir = tmp_path / "SECRET-SOURCE-DIRECTORY"
    source_dir.mkdir()
    source = source_dir / "SECRET-original-name.FCStd"
    original = b"PK\x03\x04FCStd deterministic bytes"
    source.write_bytes(original)
    head = _initialize_imported(store, manager, source)
    revision = store.load_revision(PROJECT_ID, head.revision_id)
    assert head.generation == 0
    assert revision.base_revision is None
    assert revision.artifacts == ()
    assert revision.model is not None
    assert revision.model.name == "model.FCStd"
    assert revision.model.format == "fcstd"
    assert revision.model.sha256 == hashlib.sha256(original).hexdigest()
    assert revision.model.size_bytes == len(original)
    model_path = store.revision_model_path(PROJECT_ID, head.revision_id)
    assert model_path.read_bytes() == original
    assert model_path.name == "model.FCStd"
    assert stat.S_IMODE(model_path.stat().st_mode) == 0o600

    source.write_bytes(b"changed after import")
    assert model_path.read_bytes() == original
    durable = b"".join(path.read_bytes() for path in _project_dir(root).rglob("*.json"))
    for secret in (str(source), str(source_dir), source.name, source_dir.name):
        assert secret.encode("utf-8") not in durable
        assert secret not in repr(head)
        assert secret not in repr(revision)


def test_literal_import_manifest_and_head_vectors_are_canonical(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    source = tmp_path / "base.FCStd"
    source.write_bytes(b"abc")
    monkeypatch.setattr(revisions_module, "_new_revision_id", lambda: REVISION_A)
    monkeypatch.setattr(revisions_module, "_new_artifact_id", lambda: ARTIFACT_MODEL)
    head = _initialize_imported(store, manager, source)

    model_mapping = {
        "schema_version": 1,
        "id": ARTIFACT_MODEL,
        "name": "model.FCStd",
        "format": "fcstd",
        "sha256": hashlib.sha256(b"abc").hexdigest(),
        "size_bytes": 3,
    }
    manifest_body = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "revision_id": REVISION_A,
        "base_revision": None,
        "model": model_mapping,
        "artifacts": [],
    }
    expected_manifest = _checked_record(manifest_body, MANIFEST_CHECKSUM_DOMAIN)
    assert hashlib.sha256(expected_manifest).hexdigest() == (
        "22466245c595a208848a39134952b4e8fd5d569a1c6fe461a590e3b3c6371094"
    )
    manifest_path = _revision_dir(root, REVISION_A) / "manifest.json"
    assert manifest_path.read_bytes() == expected_manifest
    assert head.manifest_sha256 == hashlib.sha256(expected_manifest).hexdigest()

    head_body = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "generation": 0,
        "revision_id": REVISION_A,
        "manifest_sha256": head.manifest_sha256,
    }
    expected_head = _checked_record(head_body, HEAD_CHECKSUM_DOMAIN)
    assert hashlib.sha256(expected_head).hexdigest() == (
        "ba8d05ab835d68b2c15b42c1f549de4c26762e2f75341e2b396195d47e9bd9c5"
    )
    assert (_project_dir(root) / "HEAD.json").read_bytes() == expected_head


def test_literal_staging_prepared_committed_and_not_committed_journal_vectors(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    revision_ids = iter((REVISION_A, REVISION_B))
    artifact_ids = iter((ARTIFACT_MODEL, ARTIFACT_STEP))
    monkeypatch.setattr(revisions_module, "_new_revision_id", lambda: next(revision_ids))
    monkeypatch.setattr(revisions_module, "_new_transaction_id", lambda: TRANSACTION_ID)
    monkeypatch.setattr(revisions_module, "_new_artifact_id", lambda: next(artifact_ids))
    head = _initialize_empty(store, manager)
    journal_path = _project_dir(root) / "journal.json"

    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        assert revision_id == REVISION_B
        staging = CommitJournal(
            id=TRANSACTION_ID,
            project_id=PROJECT_ID,
            expected_head=head,
            candidate_revision=REVISION_B,
            manifest_sha256=None,
            state=CommitJournalState.STAGING,
        )
        expected_staging = _checked_record(staging.to_mapping(), JOURNAL_CHECKSUM_DOMAIN)
        assert journal_path.read_bytes() == expected_staging
        assert hashlib.sha256(expected_staging).hexdigest() == (
            "6a3c3bdaab2789dd50ef0b073d9c9ab3f762337475dd2b695bf7352bd4339421"
        )

        model_path = store.candidate_model_path(PROJECT_ID, revision_id, lease)
        step_path = store.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        model_path.write_bytes(b"model")
        step_path.write_bytes(b"step")
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        prepared = replace(
            staging,
            manifest_sha256=sealed.manifest_sha256,
            state=CommitJournalState.PREPARED,
        )
        expected_prepared = _checked_record(prepared.to_mapping(), JOURNAL_CHECKSUM_DOMAIN)
        assert journal_path.read_bytes() == expected_prepared

        committed_head = store.commit_revision(PROJECT_ID, head, revision_id, lease)
        committed = replace(prepared, state=CommitJournalState.COMMITTED)
        expected_committed = _checked_record(committed.to_mapping(), JOURNAL_CHECKSUM_DOMAIN)
        assert journal_path.read_bytes() == expected_committed
        expected_head = _checked_record(committed_head.to_mapping(), HEAD_CHECKSUM_DOMAIN)
        assert (_project_dir(root) / "HEAD.json").read_bytes() == expected_head
        assert expected_staging != expected_prepared != expected_committed

    other_store_root = root.parent / "other-store"
    other_lock_root = root.parent / "other-locks-for-journal"
    other_store_root.mkdir(mode=0o700)
    other_lock_root.mkdir(mode=0o700)
    os.chmod(other_store_root, 0o700)
    os.chmod(other_lock_root, 0o700)
    other_manager = ResourceLeaseManager(other_lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    other_store = LocalRevisionStore(
        other_store_root,
        other_manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    monkeypatch.setattr(revisions_module, "_new_revision_id", lambda: REVISION_C)
    other_head = _initialize_empty(other_store, other_manager)
    monkeypatch.setattr(revisions_module, "_new_revision_id", lambda: REVISION_B)
    monkeypatch.setattr(revisions_module, "_new_transaction_id", lambda: TRANSACTION_ID)
    with other_manager.acquire_project_write(PROJECT_ID) as lease:
        other_revision = other_store.begin_revision(PROJECT_ID, other_head, lease)
        rolled_back = other_store.rollback_revision(PROJECT_ID, other_revision, lease)
        assert rolled_back.journal is not None
        expected_not_committed = _checked_record(
            rolled_back.journal.to_mapping(), JOURNAL_CHECKSUM_DOMAIN
        )
        assert (_project_dir(other_store_root) / "journal.json").read_bytes() == (
            expected_not_committed
        )


@pytest.mark.parametrize(
    "mutation",
    [
        b"{}",
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1.0}',
        b'{"schema_version":NaN}',
        b'{"z":1,"a":2}',
        b"{",
        b"\xff",
    ],
)
def test_reconcile_rejects_malformed_noncanonical_and_checksum_bad_journals(
    store_parts, mutation: bytes
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        store.begin_revision(PROJECT_ID, head, lease)
        journal_path = _project_dir(root) / "journal.json"
        journal_path.write_bytes(mutation)
        os.chmod(journal_path, 0o600)
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)


def test_reconcile_rejects_foreign_committed_old_third_head_and_missing_content(
    store_parts,
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    project_dir = _project_dir(root)
    journal_path = project_dir / "journal.json"
    head_path = project_dir / "HEAD.json"
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        raw = json.loads(journal_path.read_text(encoding="utf-8"))
        body = {key: value for key, value in raw.items() if key != "checksum"}

        foreign = {**body, "project_id": OTHER_PROJECT_ID}
        _write_checked_record(journal_path, foreign, JOURNAL_CHECKSUM_DOMAIN)
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)

        committed_old = {**body, "state": CommitJournalState.COMMITTED.value}
        _write_checked_record(journal_path, committed_old, JOURNAL_CHECKSUM_DOMAIN)
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)

        _write_checked_record(journal_path, body, JOURNAL_CHECKSUM_DOMAIN)
        third_head = {
            **head.to_mapping(),
            "generation": head.generation + 2,
        }
        _write_checked_record(head_path, third_head, HEAD_CHECKSUM_DOMAIN)
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)

        _write_checked_record(head_path, head.to_mapping(), HEAD_CHECKSUM_DOMAIN)
        sealed_dir = _revision_dir(root, sealed.id)
        hidden = sealed_dir.with_name(sealed_dir.name + ".missing")
        sealed_dir.rename(hidden)
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)


def test_complete_sealed_orphan_without_journal_is_never_adopted(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        journal_path = _project_dir(root) / "journal.json"
        journal_path.unlink()
        result = store.reconcile(PROJECT_ID, lease)
        assert result.status is ReconciliationStatus.CLEAN
        assert result.head == head
        assert result.journal is None
        assert store.load_revision(PROJECT_ID, revision_id) == sealed
        assert store.load_head(PROJECT_ID) == head


def test_begin_seal_commit_and_readback_complete_lifecycle(store_parts, tmp_path: Path):
    store, manager, root = store_parts
    source = tmp_path / "base.FCStd"
    source.write_bytes(b"base-fcstd")
    base_head = _initialize_imported(store, manager, source)
    before = _all_tree_bytes(_revision_dir(root, base_head.revision_id))

    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, base_head, lease)
        assert re.fullmatch(r"revision_[0-9a-f]{32}", revision_id)
        assert revision_id != base_head.revision_id
        candidate_model = store.candidate_model_path(PROJECT_ID, revision_id, lease)
        candidate_step = store.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        assert candidate_model.read_bytes() == b"base-fcstd"
        assert candidate_step.name == "model.step"
        assert not candidate_step.exists()
        candidate_model.write_bytes(b"changed-fcstd")
        candidate_step.write_bytes(b"ISO-10303-21;STEP;ENDSEC;")

        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        assert sealed.id == revision_id
        assert sealed.base_revision == base_head.revision_id
        assert sealed.model is not None
        assert sealed.model.name == "model.FCStd"
        assert sealed.model.sha256 == hashlib.sha256(b"changed-fcstd").hexdigest()
        assert len(sealed.artifacts) == 1
        assert sealed.artifacts[0].name == "model.step"
        assert sealed.artifacts[0].format == "step"
        assert (
            sealed.artifacts[0].sha256 == hashlib.sha256(b"ISO-10303-21;STEP;ENDSEC;").hexdigest()
        )
        assert not candidate_model.exists()
        assert not candidate_step.exists()
        assert store.load_head(PROJECT_ID) == base_head
        assert store.load_revision(PROJECT_ID, revision_id) == sealed

        committed = store.commit_revision(PROJECT_ID, base_head, revision_id, lease)
        assert committed.generation == base_head.generation + 1
        assert committed.revision_id == revision_id
        assert committed.manifest_sha256 == sealed.manifest_sha256

    assert store.load_head(PROJECT_ID) == committed
    assert store.load_revision(PROJECT_ID, revision_id) == sealed
    assert store.revision_model_path(PROJECT_ID, revision_id).read_bytes() == b"changed-fcstd"
    assert (
        store.revision_artifact_path(PROJECT_ID, revision_id, sealed.artifacts[0].id).read_bytes()
        == b"ISO-10303-21;STEP;ENDSEC;"
    )
    assert _all_tree_bytes(_revision_dir(root, base_head.revision_id)) == before


def test_begin_from_empty_uses_controlled_missing_model_path(store_parts):
    store, manager, _root = store_parts
    base_head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, base_head, lease)
        model_path = store.candidate_model_path(PROJECT_ID, revision_id, lease)
        step_path = store.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        assert not model_path.exists()
        assert not step_path.exists()
        model_path.write_bytes(b"first model")
        step_path.write_bytes(b"first step")
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        committed = store.commit_revision(PROJECT_ID, base_head, revision_id, lease)
    assert sealed.base_revision == base_head.revision_id
    assert committed.revision_id == sealed.id


def test_terminal_committed_journal_reconciles_idempotently_and_next_begin_consumes_it(
    store_parts,
):
    store, manager, _root = store_parts
    base_head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, base_head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        committed = store.commit_revision(PROJECT_ID, base_head, revision_id, lease)
        first = store.reconcile(PROJECT_ID, lease)
        second = store.reconcile(PROJECT_ID, lease)
        assert first == second
        assert first.status is ReconciliationStatus.COMMITTED
        assert first.head == committed
        assert first.journal is not None
        assert first.journal.state is CommitJournalState.COMMITTED

        next_revision = store.begin_revision(PROJECT_ID, committed, lease)
        assert next_revision not in {revision_id, sealed.base_revision}
        next_journal = store.reconcile(PROJECT_ID, lease)
        assert next_journal.status is ReconciliationStatus.NOT_COMMITTED
        assert next_journal.head == committed


def test_staging_rollback_is_not_committed_and_removes_writable_candidate(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        model_path = store.candidate_model_path(PROJECT_ID, revision_id, lease)
        model_path.write_bytes(b"partial")
        result = store.rollback_revision(PROJECT_ID, revision_id, lease)
        assert result.status is ReconciliationStatus.NOT_COMMITTED
        assert result.head == head
        assert result.journal is not None
        assert result.journal.state is CommitJournalState.NOT_COMMITTED
        assert not _candidate_dir(root, revision_id).exists()
        assert store.reconcile(PROJECT_ID, lease) == result
    assert store.load_head(PROJECT_ID) == head
    with pytest.raises(RevisionStoreError) as captured:
        store.load_revision(PROJECT_ID, revision_id)
    assert captured.value.code is RevisionStoreErrorCode.NOT_FOUND


def test_prepared_rollback_never_advances_head_or_mutates_old_revision(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    before = _all_tree_bytes(_revision_dir(root, head.revision_id))
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        result = store.rollback_revision(PROJECT_ID, revision_id, lease)
        assert result.status is ReconciliationStatus.NOT_COMMITTED
        assert result.head == head
        assert result.journal is not None
        assert result.journal.manifest_sha256 == sealed.manifest_sha256
        assert store.reconcile(PROJECT_ID, lease) == result
    assert store.load_head(PROJECT_ID) == head
    assert store.load_revision(PROJECT_ID, revision_id) == sealed
    assert _all_tree_bytes(_revision_dir(root, head.revision_id)) == before


def test_terminal_sealed_revision_can_be_reprepared_with_new_transaction_and_committed(
    store_parts,
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        detached = store.rollback_revision(PROJECT_ID, revision_id, lease)
        assert detached.status is ReconciliationStatus.NOT_COMMITTED
        assert detached.journal is not None
        assert detached.journal.state is CommitJournalState.NOT_COMMITTED
        terminal_transaction = detached.journal.id
        immutable_before = _all_tree_bytes(_revision_dir(root, revision_id))

        prepared = store.prepare_revision(
            PROJECT_ID,
            head,
            revision_id,
            sealed.manifest_sha256,
            lease,
        )

        assert prepared == sealed
        assert prepared is not sealed
        assert store.load_revision(PROJECT_ID, revision_id) == sealed
        assert _all_tree_bytes(_revision_dir(root, revision_id)) == immutable_before
        assert store.load_head(PROJECT_ID) == head
        journal_mapping = json.loads(
            (_project_dir(root) / "journal.json").read_text(encoding="utf-8")
        )
        assert journal_mapping["state"] == CommitJournalState.PREPARED.value
        assert journal_mapping["id"] != terminal_transaction
        assert journal_mapping["project_id"] == PROJECT_ID
        assert journal_mapping["expected_head"] == head.to_mapping()
        assert journal_mapping["candidate_revision"] == sealed.id
        assert journal_mapping["manifest_sha256"] == sealed.manifest_sha256

        committed = store.commit_revision(PROJECT_ID, head, revision_id, lease)

    assert committed.generation == head.generation + 1
    assert committed.revision_id == sealed.id
    assert committed.manifest_sha256 == sealed.manifest_sha256
    assert store.load_head(PROJECT_ID) == committed
    assert store.load_revision(PROJECT_ID, revision_id) == sealed


@pytest.mark.parametrize("failure", ["directory_fsync", "project_fd_close", "root_fd_close"])
def test_reprepare_durability_uncertainty_converges_to_terminal_not_committed(
    store_parts,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        store.rollback_revision(PROJECT_ID, revision_id, lease)
        immutable_before = _all_tree_bytes(_revision_dir(root, revision_id))
        original_open = revisions_module.os.open
        original_fsync = revisions_module.os.fsync
        original_replace = revisions_module.os.replace
        original_close_project = revisions_module._close_project_fds
        original_close_fd = revisions_module._close_fd
        roles: dict[int, str] = {}
        prepared_replaced = False
        failed = False

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                fd = original_open(path, flags, mode)
            else:
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
            roles[fd] = str(path)
            return fd

        def tracked_replace(src, dst, *args, **kwargs):
            nonlocal prepared_replaced
            result = original_replace(src, dst, *args, **kwargs)
            if dst == "journal.json":
                prepared_replaced = True
            return result

        def targeted_fsync(fd):
            nonlocal failed
            if (
                failure == "directory_fsync"
                and prepared_replaced
                and not failed
                and roles.get(fd) == _project_dir(root).name
            ):
                failed = True
                raise OSError("SECRET review prepare directory fsync")
            return original_fsync(fd)

        def targeted_close_project(project_open):
            nonlocal failed
            close_failed = original_close_project(project_open)
            if failure == "project_fd_close" and prepared_replaced and not failed:
                failed = True
                return True
            return close_failed

        def targeted_close_fd(fd):
            nonlocal failed
            close_failed = original_close_fd(fd)
            if (
                failure == "root_fd_close"
                and prepared_replaced
                and not failed
                and roles.get(fd) == root.name
            ):
                failed = True
                return True
            return close_failed

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
        monkeypatch.setattr(revisions_module.os, "fsync", targeted_fsync)
        monkeypatch.setattr(revisions_module, "_close_project_fds", targeted_close_project)
        monkeypatch.setattr(revisions_module, "_close_fd", targeted_close_fd)
        with pytest.raises(RevisionStoreError) as captured:
            store.prepare_revision(
                PROJECT_ID,
                head,
                revision_id,
                sealed.manifest_sha256,
                lease,
            )
        _assert_closed_error(
            captured.value,
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=False,
        )
        assert prepared_replaced and failed
        assert store.load_head(PROJECT_ID) == head
        assert store.load_revision(PROJECT_ID, revision_id) == sealed
        assert _all_tree_bytes(_revision_dir(root, revision_id)) == immutable_before
        reconciled = store.reconcile(PROJECT_ID, lease)
        assert reconciled.status is ReconciliationStatus.NOT_COMMITTED
        assert reconciled.head == head
        assert reconciled.journal is not None
        assert reconciled.journal.candidate_revision == revision_id
        assert reconciled.journal.state is CommitJournalState.NOT_COMMITTED


@pytest.mark.parametrize(
    "mismatch",
    ["base_generation", "base_revision", "base_manifest", "draft_manifest"],
)
def test_prepare_existing_revision_rejects_stale_full_head_and_manifest_without_mutation(
    store_parts,
    mismatch: str,
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        store.rollback_revision(PROJECT_ID, revision_id, lease)
        supplied_head = head
        supplied_manifest = sealed.manifest_sha256
        if mismatch == "base_generation":
            supplied_head = replace(head, generation=head.generation + 1)
        elif mismatch == "base_revision":
            supplied_head = replace(head, revision_id=REVISION_C)
        elif mismatch == "base_manifest":
            supplied_head = replace(head, manifest_sha256="c" * 64)
        else:
            supplied_manifest = "c" * 64
        before = _all_tree_bytes(root)

        with pytest.raises(RevisionStoreError) as captured:
            store.prepare_revision(
                PROJECT_ID,
                supplied_head,
                revision_id,
                supplied_manifest,
                lease,
            )

        _assert_closed_error(captured.value, RevisionStoreErrorCode.CONFLICT)
        assert _all_tree_bytes(root) == before
        assert store.load_head(PROJECT_ID) == head
        assert store.load_revision(PROJECT_ID, revision_id) == sealed


def test_prepare_existing_revision_rejects_unrelated_nonterminal_journal_without_mutation(
    store_parts,
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        store.rollback_revision(PROJECT_ID, revision_id, lease)
        unrelated_revision = store.begin_revision(PROJECT_ID, head, lease)
        before = _all_tree_bytes(root)

        with pytest.raises(RevisionStoreError) as captured:
            store.prepare_revision(
                PROJECT_ID,
                head,
                revision_id,
                sealed.manifest_sha256,
                lease,
            )

        _assert_closed_error(captured.value, RevisionStoreErrorCode.CONFLICT)
        assert _all_tree_bytes(root) == before
        assert store.load_head(PROJECT_ID) == head
        assert store.load_revision(PROJECT_ID, revision_id) == sealed
        store.rollback_revision(PROJECT_ID, unrelated_revision, lease)


def test_prepare_existing_revision_rejects_terminal_journal_not_matching_current_head(
    store_parts,
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        detached = store.rollback_revision(PROJECT_ID, revision_id, lease)
        assert detached.journal is not None
        journal_path = _project_dir(root) / "journal.json"
        body = detached.journal.to_mapping()
        body["expected_head"] = replace(
            head,
            generation=head.generation + 1,
        ).to_mapping()
        _write_checked_record(journal_path, body, JOURNAL_CHECKSUM_DOMAIN)
        before = _all_tree_bytes(root)

        with pytest.raises(RevisionStoreError) as captured:
            store.prepare_revision(
                PROJECT_ID,
                head,
                revision_id,
                sealed.manifest_sha256,
                lease,
            )

        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)
        assert _all_tree_bytes(root) == before
        assert store.load_head(PROJECT_ID) == head
        assert store.load_revision(PROJECT_ID, revision_id) == sealed


def test_prepare_existing_revision_requires_exact_live_project_lease_without_mutation(
    store_parts,
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        store.rollback_revision(PROJECT_ID, revision_id, lease)
    assert lease.released
    before = _all_tree_bytes(root)

    with pytest.raises(RevisionStoreError) as captured:
        store.prepare_revision(
            PROJECT_ID,
            head,
            revision_id,
            sealed.manifest_sha256,
            lease,
        )

    _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_LEASE)
    assert _all_tree_bytes(root) == before
    assert store.load_head(PROJECT_ID) == head
    assert store.load_revision(PROJECT_ID, revision_id) == sealed


def test_stale_head_and_wrong_candidate_are_conflicts(store_parts):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    stale = replace(head, generation=head.generation + 1)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            store.begin_revision(PROJECT_ID, stale, lease)
        assert captured.value.code is RevisionStoreErrorCode.CONFLICT
        revision_id = _begin_and_fill(store, lease, head)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, REVISION_C, lease)
        assert captured.value.code is RevisionStoreErrorCode.CONFLICT
        store.seal_revision(PROJECT_ID, revision_id, lease)
        with pytest.raises(RevisionStoreError) as captured:
            store.commit_revision(PROJECT_ID, stale, revision_id, lease)
        assert captured.value.code is RevisionStoreErrorCode.CONFLICT
    assert store.load_head(PROJECT_ID) == head


def test_all_mutations_require_exact_live_matching_lease(store_parts, roots):
    store, manager, _root = store_parts
    _store_root, lock_root = roots
    other_lock_root = lock_root.parent / "other-locks"
    other_lock_root.mkdir(mode=0o700)
    os.chmod(other_lock_root, 0o700)
    other_manager = ResourceLeaseManager(other_lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    project_lease = manager.acquire_project_write(PROJECT_ID)
    wrong_project = manager.acquire_project_write(OTHER_PROJECT_ID)
    foreign = other_manager.acquire_project_write(PROJECT_ID)
    try:
        for lease in (wrong_project, foreign, object(), None):
            with pytest.raises(RevisionStoreError) as captured:
                store.initialize_empty_project(PROJECT_ID, lease)
            assert captured.value.code is RevisionStoreErrorCode.INVALID_LEASE
        project_lease.release(owner_token=project_lease.owner_token)
        with pytest.raises(RevisionStoreError) as captured:
            store.initialize_empty_project(PROJECT_ID, project_lease)
        assert captured.value.code is RevisionStoreErrorCode.INVALID_LEASE
    finally:
        if not wrong_project.released:
            wrong_project.release(owner_token=wrong_project.owner_token)
        if not foreign.released:
            foreign.release(owner_token=foreign.owner_token)


def test_every_existing_project_mutation_rejects_all_invalid_lease_classes(
    store_parts, roots, tmp_path: Path
):
    store, manager, _root = store_parts
    _store_root, lock_root = roots
    head = _initialize_empty(store, manager)
    source = tmp_path / "base.FCStd"
    source.write_bytes(b"base")
    foreign_root = lock_root.parent / "foreign-existing-locks"
    foreign_root.mkdir(mode=0o700)
    os.chmod(foreign_root, 0o700)
    foreign_manager = ResourceLeaseManager(foreign_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    wrong = manager.acquire_project_write(OTHER_PROJECT_ID)
    foreign = foreign_manager.acquire_project_write(PROJECT_ID)
    released = manager.acquire_project_write(PROJECT_ID)
    released.release(owner_token=released.owner_token)
    try:
        for invalid in (wrong, foreign, released, object(), None):
            calls = (
                lambda invalid=invalid: store.initialize_empty_project(PROJECT_ID, invalid),
                lambda invalid=invalid: _import_trusted(store, PROJECT_ID, source, invalid),
                lambda invalid=invalid: store.begin_revision(PROJECT_ID, head, invalid),
                lambda invalid=invalid: store.candidate_model_path(PROJECT_ID, REVISION_B, invalid),
                lambda invalid=invalid: store.candidate_artifact_path(
                    PROJECT_ID, REVISION_B, "step", invalid
                ),
                lambda invalid=invalid: store.seal_revision(PROJECT_ID, REVISION_B, invalid),
                lambda invalid=invalid: store.commit_revision(
                    PROJECT_ID, head, REVISION_B, invalid
                ),
                lambda invalid=invalid: store.rollback_revision(PROJECT_ID, REVISION_B, invalid),
                lambda invalid=invalid: store.reconcile(PROJECT_ID, invalid),
            )
            for call in calls:
                with pytest.raises(RevisionStoreError) as captured:
                    call()
                _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_LEASE)
    finally:
        wrong.release(owner_token=wrong.owner_token)
        foreign.release(owner_token=foreign.owner_token)


def test_every_mutation_rejects_an_inherited_process_context_before_storage_access(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    source = tmp_path / "inherited-process.FCStd"
    source.write_bytes(b"unchanged source")
    before = _all_tree_bytes(root)
    creator_pid = os.getpid()
    lease = manager.acquire_project_write(PROJECT_ID)
    calls = (
        lambda: store.initialize_empty_project(PROJECT_ID, lease),
        lambda: _import_trusted(store, PROJECT_ID, source, lease),
        lambda: store.begin_revision(PROJECT_ID, head, lease),
        lambda: store.candidate_model_path(PROJECT_ID, REVISION_B, lease),
        lambda: store.candidate_artifact_path(PROJECT_ID, REVISION_B, "step", lease),
        lambda: store.seal_revision(PROJECT_ID, REVISION_B, lease),
        lambda: store.commit_revision(PROJECT_ID, head, REVISION_B, lease),
        lambda: store.rollback_revision(PROJECT_ID, REVISION_B, lease),
        lambda: store.reconcile(PROJECT_ID, lease),
    )

    def fail_storage_open(*_args, **_kwargs):
        raise AssertionError("storage access occurred before the process check")

    try:
        with monkeypatch.context() as changed_process:
            changed_process.setattr(revisions_module.os, "getpid", lambda: creator_pid + 1)
            changed_process.setattr(revisions_module.os, "open", fail_storage_open)
            for call in calls:
                with pytest.raises(RevisionStoreError) as captured:
                    call()
                _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_LEASE)
    finally:
        lease.release(owner_token=lease.owner_token)
    assert _all_tree_bytes(root) == before


def test_store_binds_process_identity_at_construction_before_first_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store_root_a = tmp_path / "store-a"
    lock_root_a = tmp_path / "locks-a"
    store_root_b = tmp_path / "store-b"
    lock_root_b = tmp_path / "locks-b"
    for root in (store_root_a, lock_root_a, store_root_b, lock_root_b):
        root.mkdir(mode=0o700)
        os.chmod(root, 0o700)

    real_pid = os.getpid()
    pid_a = real_pid + 1000
    pid_b = real_pid + 2000
    current_pid_value = [pid_a]

    def current_pid():
        return current_pid_value[0]

    def fail_storage_open(*_args, **_kwargs):
        raise AssertionError("storage access occurred before the process check")

    monkeypatch.setattr(revisions_module.os, "getpid", current_pid)
    manager_a = ResourceLeaseManager(lock_root_a, trust=LeaseRootTrust.TRUSTED_LOCAL)
    store_a = LocalRevisionStore(
        store_root_a,
        manager_a,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    lease_a = manager_a.acquire_project_write(PROJECT_ID)
    lease_b = None
    try:
        current_pid_value[0] = pid_b
        with monkeypatch.context() as blocked_storage:
            blocked_storage.setattr(revisions_module.os, "open", fail_storage_open)
            with pytest.raises(RevisionStoreError) as captured:
                store_a.initialize_empty_project(PROJECT_ID, lease_a)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_LEASE)
        assert _all_tree_bytes(store_root_a) == {}

        manager_b = ResourceLeaseManager(lock_root_b, trust=LeaseRootTrust.TRUSTED_LOCAL)
        store_b = LocalRevisionStore(
            store_root_b,
            manager_b,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        )
        lease_b = manager_b.acquire_project_write(PROJECT_ID)
        head = store_b.initialize_empty_project(PROJECT_ID, lease_b)
        assert head.generation == 0
        assert store_b.load_head(PROJECT_ID) == head
    finally:
        if lease_b is not None and not lease_b.released:
            current_pid_value[0] = pid_b
            lease_b.release(owner_token=lease_b.owner_token)
        current_pid_value[0] = pid_a
        lease_a.release(owner_token=lease_a.owner_token)


@pytest.mark.parametrize(
    ("factory", "field", "invalid"),
    [
        (_revision, "id", "revision_bad"),
        (_revision, "base_revision", "revision_bad"),
        (_journal, "id", "transaction_bad"),
        (_journal, "candidate_revision", "revision_bad"),
    ],
)
def test_revision_and_transaction_identifiers_are_canonical(factory, field, invalid):
    mapping = factory().to_mapping()
    mapping[field] = invalid
    with pytest.raises(RevisionStoreError) as captured:
        type(factory()).from_mapping(mapping)
    assert captured.value.code is RevisionStoreErrorCode.INVALID_IDENTIFIER


def test_store_methods_reject_noncanonical_project_revision_and_artifact_ids(store_parts):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    read_calls = (
        lambda: store.load_head("project_bad"),
        lambda: store.load_revision(PROJECT_ID, "revision_bad"),
        lambda: store.load_revision("project_bad", head.revision_id),
        lambda: store.revision_model_path(PROJECT_ID, "revision_bad"),
        lambda: store.revision_model_path("project_bad", head.revision_id),
        lambda: store.revision_artifact_path(PROJECT_ID, head.revision_id, "artifact_bad"),
        lambda: store.revision_artifact_path("project_bad", head.revision_id, ARTIFACT_STEP),
    )
    for call in read_calls:
        with pytest.raises(RevisionStoreError) as captured:
            call()
        assert captured.value.code is RevisionStoreErrorCode.INVALID_IDENTIFIER
    with manager.acquire_project_write(PROJECT_ID) as lease:
        mutation_calls = (
            lambda: store.begin_revision("project_bad", head, lease),
            lambda: store.candidate_model_path(PROJECT_ID, "revision_bad", lease),
            lambda: store.candidate_artifact_path(PROJECT_ID, "revision_bad", "step", lease),
            lambda: store.seal_revision(PROJECT_ID, "revision_bad", lease),
            lambda: store.commit_revision(PROJECT_ID, head, "revision_bad", lease),
            lambda: store.rollback_revision(PROJECT_ID, "revision_bad", lease),
        )
        for call in mutation_calls:
            with pytest.raises(RevisionStoreError) as captured:
                call()
            assert captured.value.code is RevisionStoreErrorCode.INVALID_IDENTIFIER


def test_store_does_not_reacquire_nonreentrant_project_lease(
    store_parts, monkeypatch, tmp_path: Path
):
    store, manager, _root = store_parts
    original = manager.acquire_project_write
    calls: list[str] = []
    source = tmp_path / "other-base.FCStd"
    source.write_bytes(b"other base")

    def tracked(self, project_id):
        calls.append(project_id)
        return original(project_id)

    lease = original(PROJECT_ID)
    other_lease = original(OTHER_PROJECT_ID)
    try:
        monkeypatch.setattr(ResourceLeaseManager, "acquire_project_write", tracked)
        head = store.initialize_empty_project(PROJECT_ID, lease)
        revision_id = _begin_and_fill(store, lease, head)
        store.candidate_model_path(PROJECT_ID, revision_id, lease)
        store.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        committed = store.commit_revision(PROJECT_ID, head, revision_id, lease)
        store.reconcile(PROJECT_ID, lease)
        rollback_id = store.begin_revision(PROJECT_ID, committed, lease)
        store.rollback_revision(PROJECT_ID, rollback_id, lease)
        _import_trusted(store, OTHER_PROJECT_ID, source, other_lease)
    finally:
        lease.release(owner_token=lease.owner_token)
        other_lease.release(owner_token=other_lease.owner_token)
    assert calls == []


def test_candidate_paths_are_fixed_store_owned_and_require_staging_authority(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        model = store.candidate_model_path(PROJECT_ID, revision_id, lease)
        step = store.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        assert model == _candidate_dir(root, revision_id) / "model.FCStd"
        assert step == _candidate_dir(root, revision_id) / "model.step"
        assert PROJECT_ID not in str(model.relative_to(root))
        assert revision_id not in str(model.relative_to(root))
        for fmt in ("stl", "gltf", "fcstd", "STEP", "../step", True):
            with pytest.raises(RevisionStoreError) as captured:
                store.candidate_artifact_path(PROJECT_ID, revision_id, fmt, lease)
            assert captured.value.code is RevisionStoreErrorCode.INVALID_INPUT
        store.rollback_revision(PROJECT_ID, revision_id, lease)
        with pytest.raises(RevisionStoreError) as captured:
            store.candidate_model_path(PROJECT_ID, revision_id, lease)
        assert captured.value.code is RevisionStoreErrorCode.CONFLICT


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("missing", RevisionStoreErrorCode.NOT_FOUND),
        ("empty", RevisionStoreErrorCode.INVALID_INPUT),
        ("directory", RevisionStoreErrorCode.INVALID_INPUT),
        ("symlink", RevisionStoreErrorCode.INVALID_INPUT),
        ("hardlink", RevisionStoreErrorCode.INVALID_INPUT),
        ("fifo", RevisionStoreErrorCode.INVALID_INPUT),
        ("socket", RevisionStoreErrorCode.INVALID_INPUT),
    ],
)
def test_import_rejects_unsafe_or_empty_sources(store_parts, tmp_path: Path, kind, expected):
    store, manager, _root = store_parts
    source = tmp_path / "SECRET-source.FCStd"
    if kind == "empty":
        source.write_bytes(b"")
    elif kind == "directory":
        source.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target.FCStd"
        target.write_bytes(b"target")
        source.symlink_to(target)
    elif kind == "hardlink":
        target = tmp_path / "target.FCStd"
        target.write_bytes(b"target")
        os.link(target, source)
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "socket":
        source = tmp_path.parent / "s"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(source))
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            store.import_trusted_fcstd(PROJECT_ID, source, DIGEST_A, 1, lease)
    _assert_closed_error(captured.value, expected)


def test_import_rejects_hostile_input_before_any_implicit_protocol(store_parts):
    store, manager, root = store_parts
    hostile = ExplosiveInput()
    before = _all_tree_bytes(root)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            store.import_trusted_fcstd(PROJECT_ID, hostile, DIGEST_A, 1, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.INVALID_INPUT)
    assert hostile.protocol_calls == []
    assert _all_tree_bytes(root) == before


def test_import_opens_regular_source_nofollow_cloexec_nonblocking_and_read_only(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    source = tmp_path / "base.FCStd"
    source.write_bytes(b"trusted model")
    original_open = revisions_module.os.open
    observed: list[tuple[int, int | None]] = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if str(path) == str(source):
            observed.append((flags, dir_fd))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        _import_trusted(store, PROJECT_ID, source, lease)
    assert len(observed) == 1
    flags, _dir_fd = observed[0]
    assert flags & os.O_NOFOLLOW
    assert flags & os.O_CLOEXEC
    assert flags & os.O_NONBLOCK
    assert not flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR)


def test_imported_model_is_fsynced_before_atomic_project_publication(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    source = tmp_path / "base.FCStd"
    source.write_bytes(b"trusted imported model")
    original_open = revisions_module.os.open
    original_fsync = revisions_module.os.fsync
    original_rename = revisions_module.os.rename
    roles: dict[int, str] = {}
    events: list[tuple[str, str]] = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        roles[fd] = str(path)
        return fd

    def tracked_fsync(fd):
        events.append(("fsync", roles.get(fd, "<unknown>")))
        return original_fsync(fd)

    def tracked_rename(src, dst, *args, **kwargs):
        events.append(("rename", str(dst)))
        return original_rename(src, dst, *args, **kwargs)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "fsync", tracked_fsync)
        monkeypatch.setattr(revisions_module.os, "rename", tracked_rename)
        _import_trusted(store, PROJECT_ID, source, lease)
    publication = events.index(("rename", _project_dir(root).name))
    assert ("fsync", "model.FCStd") in events[:publication]
    assert ("fsync", "manifest.json") in events[:publication]
    assert ("fsync", "HEAD.json") in events[:publication]
    assert ("fsync", root.name) in events[publication + 1 :]


def test_imported_model_fsync_failure_prevents_project_publication(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    source = tmp_path / "base.FCStd"
    source.write_bytes(b"trusted imported model")
    original_open = revisions_module.os.open
    original_fsync = revisions_module.os.fsync
    roles: dict[int, str] = {}
    failed = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        roles[fd] = str(path)
        return fd

    def fail_imported_model_fsync(fd):
        nonlocal failed
        if roles.get(fd) == "model.FCStd" and not failed:
            failed = True
            raise OSError("SECRET imported model fsync")
        return original_fsync(fd)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_imported_model_fsync)
        with pytest.raises(RevisionStoreError) as captured:
            _import_trusted(store, PROJECT_ID, source, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
    assert failed
    assert tuple(root.iterdir()) == ()


def test_import_size_budget_is_checked_before_project_publication(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    source = tmp_path / "oversized.FCStd"
    source.write_bytes(b"1234")
    monkeypatch.setattr(revisions_module, "_MAX_FILE_BYTES", 3)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            _import_trusted(store, PROJECT_ID, source, lease)
    assert captured.value.code is RevisionStoreErrorCode.BUDGET_EXCEEDED
    assert tuple(root.iterdir()) == ()


def test_import_detects_source_mutation_during_bounded_copy(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    source = tmp_path / "mutable.FCStd"
    source.write_bytes(b"a" * 131072)
    original_read = revisions_module.os.read
    changed = False

    def mutating_read(fd, count):
        nonlocal changed
        data = original_read(fd, count)
        if data and not changed:
            changed = True
            source.write_bytes(b"b" * 131072)
        return data

    monkeypatch.setattr(revisions_module.os, "read", mutating_read)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            _import_trusted(store, PROJECT_ID, source, lease)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT


def test_import_requires_exact_prevalidated_digest_and_size_before_publication(
    store_parts, tmp_path: Path
):
    store, manager, root = store_parts
    source = tmp_path / "normalized.FCStd"
    validated = b"validated normalized model"
    source.write_bytes(validated)
    expected_sha256 = hashlib.sha256(validated).hexdigest()
    expected_size = len(validated)

    # Simulate replacement after the CAD port validated the staging file but
    # before the revision store reopens it for its authoritative copy.
    source.write_bytes(b"swapped after semantic validation")
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            store.import_trusted_fcstd(
                PROJECT_ID,
                source,
                expected_sha256,
                expected_size,
                lease,
            )

    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT
    assert tuple(root.iterdir()) == ()


def test_copy_reads_are_bounded_to_the_frozen_chunk_and_exact_size_boundary_passes(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    source = tmp_path / "chunked.FCStd"
    source.write_bytes(b"x" * 65537)
    original_read = revisions_module.os.read
    requests: list[int] = []

    def tracked_read(fd, count):
        requests.append(count)
        return original_read(fd, count)

    monkeypatch.setattr(revisions_module, "_MAX_FILE_BYTES", 65537)
    monkeypatch.setattr(revisions_module.os, "read", tracked_read)
    head = _initialize_imported(store, manager, source)
    assert store.load_revision(PROJECT_ID, head.revision_id).model is not None
    assert requests
    assert max(requests) <= 65536


@pytest.mark.parametrize(
    "mutation",
    [
        b"{}",
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1.0}',
        b'{"schema_version":NaN}',
        b'{"z":1,"a":2}',
        b"{",
        b"\xff",
    ],
)
def test_manifest_parser_rejects_malformed_duplicate_float_and_noncanonical_records(
    store_parts, mutation: bytes
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    manifest = _revision_dir(root, head.revision_id) / "manifest.json"
    manifest.write_bytes(mutation)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_revision(PROJECT_ID, head.revision_id)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.CORRUPT_RECORD)


@pytest.mark.parametrize("target_name", ["model.FCStd", "model.step"])
@pytest.mark.parametrize("kind", ["missing", "empty", "directory", "symlink", "hardlink"])
def test_seal_rejects_missing_empty_and_unsafe_candidate_files(
    store_parts, tmp_path: Path, target_name: str, kind: str
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        model = store.candidate_model_path(PROJECT_ID, revision_id, lease)
        step = store.candidate_artifact_path(PROJECT_ID, revision_id, "step", lease)
        model.write_bytes(b"model")
        step.write_bytes(b"step")
        target = model if target_name == "model.FCStd" else step
        target.unlink()
        if kind == "empty":
            target.write_bytes(b"")
        elif kind == "directory":
            target.mkdir()
        elif kind == "symlink":
            outside = tmp_path / f"outside-{target_name}"
            outside.write_bytes(b"outside")
            target.symlink_to(outside)
        elif kind == "hardlink":
            outside = tmp_path / f"outside-{target_name}"
            outside.write_bytes(b"outside")
            os.link(outside, target)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        expected = (
            RevisionStoreErrorCode.NOT_FOUND
            if kind == "missing"
            else RevisionStoreErrorCode.INVALID_INPUT
        )
        assert captured.value.code is expected
        assert store.load_head(PROJECT_ID) == head


def test_seal_enforces_individual_and_aggregate_size_budgets(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head, model=b"123", step=b"456")
        monkeypatch.setattr(revisions_module, "_MAX_FILE_BYTES", 2)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        assert captured.value.code is RevisionStoreErrorCode.BUDGET_EXCEEDED

    with manager.acquire_project_write(PROJECT_ID) as lease:
        store.rollback_revision(PROJECT_ID, revision_id, lease)
        revision_id = _begin_and_fill(store, lease, head, model=b"123", step=b"456")
        monkeypatch.setattr(revisions_module, "_MAX_FILE_BYTES", 10)
        monkeypatch.setattr(revisions_module, "_MAX_REVISION_BYTES", 5)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        assert captured.value.code is RevisionStoreErrorCode.BUDGET_EXCEEDED


def test_sealed_revision_isolated_from_later_candidate_path_recreation(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(
            store,
            lease,
            head,
            model=b"sealed model",
            step=b"sealed step",
        )
        candidate_dir = _candidate_dir(root, revision_id)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        candidate_dir.mkdir(mode=0o700)
        (candidate_dir / "model.FCStd").write_bytes(b"forged replacement")
        (candidate_dir / "model.step").write_bytes(b"forged replacement")
        assert store.load_revision(PROJECT_ID, revision_id) == sealed
        assert store.revision_model_path(PROJECT_ID, revision_id).read_bytes() == b"sealed model"


def test_load_revision_detects_model_and_artifact_content_corruption(store_parts):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
    model_path = store.revision_model_path(PROJECT_ID, revision_id)
    model_path.write_bytes(b"tampered")
    with pytest.raises(RevisionStoreError) as captured:
        store.load_revision(PROJECT_ID, revision_id)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT

    model_path.write_bytes(b"changed-fcstd")
    artifact_path = store.revision_artifact_path(PROJECT_ID, revision_id, sealed.artifacts[0].id)
    artifact_path.write_bytes(b"tampered")
    with pytest.raises(RevisionStoreError) as captured:
        store.load_revision(PROJECT_ID, revision_id)
        assert captured.value.code is RevisionStoreErrorCode.CORRUPT_CONTENT


@pytest.mark.parametrize("operation", ["load", "begin"])
def test_model_integrity_and_base_copy_stream_without_whole_file_record_reads(
    store_parts,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
):
    store, manager, _root = store_parts
    source = tmp_path / "streamed.FCStd"
    source.write_bytes(b"streamed-base" * 8192)
    head = _initialize_imported(store, manager, source)
    original_read_bounded = revisions_module._read_bounded_file

    def reject_whole_content_read(parent_fd, name, root_device, maximum, missing_code):
        if name == "model.FCStd" or name == "model.step":
            raise AssertionError("CAD content must use bounded streaming I/O")
        return original_read_bounded(parent_fd, name, root_device, maximum, missing_code)

    monkeypatch.setattr(revisions_module, "_read_bounded_file", reject_whole_content_read)
    if operation == "load":
        assert store.load_revision(PROJECT_ID, head.revision_id).model is not None
    else:
        with manager.acquire_project_write(PROJECT_ID) as lease:
            revision_id = store.begin_revision(PROJECT_ID, head, lease)
            assert (
                store.candidate_model_path(PROJECT_ID, revision_id, lease).read_bytes()
                == source.read_bytes()
            )
            store.rollback_revision(PROJECT_ID, revision_id, lease)


@pytest.mark.parametrize(
    "mutation",
    [
        b"{}",
        b'{"schema_version":1,"schema_version":1}',
        b'{"schema_version":1.0}',
        b'{"schema_version":NaN}',
        b'{"z":1,"a":2}',
        b"{",
        b"\xff",
    ],
)
def test_head_parser_rejects_malformed_duplicate_float_noncanonical_and_truncated_records(
    store_parts, mutation: bytes
):
    store, manager, root = store_parts
    _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    head_path.write_bytes(mutation)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_RECORD


def test_manifest_checksum_digest_and_head_binding_are_independent(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    manifest_path = _revision_dir(root, head.revision_id) / "manifest.json"
    decoded = json.loads(manifest_path.read_text())
    decoded["checksum"] = "0" * 64
    manifest_path.write_bytes(_canonical(decoded))
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_RECORD


def test_destination_symlink_hardlink_directory_and_mode_attacks_fail_closed(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    original = head_path.read_bytes()

    head_path.unlink()
    outside = root.parent / "outside.json"
    outside.write_bytes(original)
    head_path.symlink_to(outside)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    head_path.unlink()

    head_path.mkdir(mode=0o700)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    head_path.rmdir()

    head_path.write_bytes(original)
    os.chmod(head_path, 0o600)

    extra_link = root.parent / "HEAD-hardlink.json"
    os.link(head_path, extra_link)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    extra_link.unlink()
    os.chmod(head_path, 0o644)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE
    os.chmod(head_path, 0o600)
    assert store.load_head(PROJECT_ID) == head


def test_root_identity_replacement_is_rejected_after_construction(roots):
    store_root, lock_root = roots
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    store = LocalRevisionStore(
        store_root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    moved = store_root.with_name("old-store")
    store_root.rename(moved)
    store_root.mkdir(mode=0o700)
    os.chmod(store_root, 0o700)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            store.initialize_empty_project(PROJECT_ID, lease)
    assert captured.value.code is RevisionStoreErrorCode.UNSAFE_STORE


def test_preexisting_project_symlink_cannot_redirect_initialization(store_parts, tmp_path: Path):
    store, manager, root = store_parts
    outside = tmp_path / "outside-project"
    outside.mkdir(mode=0o700)
    os.chmod(outside, 0o700)
    project_entry = _project_dir(root)
    project_entry.symlink_to(outside, target_is_directory=True)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            store.initialize_empty_project(PROJECT_ID, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)
    assert tuple(outside.iterdir()) == ()


@pytest.mark.parametrize(("entry", "mode"), [("revisions", 0o755), ("candidates", 0o755)])
def test_project_child_directory_mode_is_revalidated_on_every_operation(
    store_parts, entry: str, mode: int
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    os.chmod(_project_dir(root) / entry, mode)
    if entry == "revisions":
        with pytest.raises(RevisionStoreError) as captured:
            store.load_head(PROJECT_ID)
    else:
        with manager.acquire_project_write(PROJECT_ID) as lease:
            with pytest.raises(RevisionStoreError) as captured:
                store.begin_revision(PROJECT_ID, head, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)


@pytest.mark.parametrize("level", ["project", "revision"])
def test_project_and_revision_directory_modes_are_owner_only(store_parts, level: str):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    target = _project_dir(root)
    if level == "revision":
        target = _revision_dir(root, head.revision_id)
    os.chmod(target, 0o755)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)


def test_revisions_directory_symlink_is_never_followed(store_parts, tmp_path: Path):
    store, manager, root = store_parts
    _initialize_empty(store, manager)
    revisions = _project_dir(root) / "revisions"
    hidden = _project_dir(root) / "revisions-hidden"
    revisions.rename(hidden)
    outside = tmp_path / "outside-revisions"
    outside.mkdir()
    revisions.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)


def test_candidate_directory_symlink_and_journal_symlink_fail_closed(store_parts, tmp_path: Path):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        candidate = _candidate_dir(root, revision_id)
        candidate.rmdir()
        outside = tmp_path / "outside-candidate"
        outside.mkdir()
        candidate.symlink_to(outside, target_is_directory=True)
        with pytest.raises(RevisionStoreError) as captured:
            store.candidate_model_path(PROJECT_ID, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)

        candidate.unlink()
        candidate.mkdir(mode=0o700)
        journal = _project_dir(root) / "journal.json"
        raw = journal.read_bytes()
        journal.unlink()
        outside_journal = tmp_path / "outside-journal.json"
        outside_journal.write_bytes(raw)
        journal.symlink_to(outside_journal)
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)


@pytest.mark.parametrize("attack", ["hardlink", "mode", "directory"])
def test_journal_hardlink_mode_and_directory_attacks_require_recovery(
    store_parts, tmp_path, attack
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        store.begin_revision(PROJECT_ID, head, lease)
        journal = _project_dir(root) / "journal.json"
        if attack == "mode":
            os.chmod(journal, 0o644)
        elif attack == "directory":
            journal.unlink()
            journal.mkdir(mode=0o700)
        else:
            os.link(journal, tmp_path / "journal-hardlink.json")
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)


@pytest.mark.parametrize("entry", ["manifest", "model", "artifact"])
@pytest.mark.parametrize("attack", ["symlink", "hardlink", "mode"])
def test_immutable_revision_entries_reject_link_and_permission_attacks(
    store_parts, tmp_path: Path, entry: str, attack: str
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
    revision_dir = _revision_dir(root, revision_id)
    if entry == "manifest":
        target = revision_dir / "manifest.json"
    elif entry == "model":
        target = revision_dir / "model.FCStd"
    else:
        target = store.revision_artifact_path(PROJECT_ID, revision_id, sealed.artifacts[0].id)
    original = target.read_bytes()
    if attack == "mode":
        os.chmod(target, 0o644)
    else:
        target.unlink()
        outside = tmp_path / f"outside-{entry}"
        outside.write_bytes(original)
        if attack == "symlink":
            target.symlink_to(outside)
        else:
            os.link(outside, target)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_revision(PROJECT_ID, revision_id)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)


def test_partial_writes_are_completed_and_zero_write_fails_without_publication(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    original_write = revisions_module.os.write
    partials = 0

    def partial_write(fd, data):
        nonlocal partials
        if len(data) > 1:
            partials += 1
            data = data[: max(1, len(data) // 2)]
        return original_write(fd, data)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "write", partial_write)
        head = store.initialize_empty_project(PROJECT_ID, lease)
    assert partials > 0
    assert store.load_head(PROJECT_ID) == head

    other_root = root.parent / "zero-write-store"
    other_root.mkdir(mode=0o700)
    os.chmod(other_root, 0o700)
    other_store = LocalRevisionStore(
        other_root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    monkeypatch.setattr(revisions_module.os, "write", lambda _fd, _data: 0)
    with manager.acquire_project_write(OTHER_PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            other_store.initialize_empty_project(OTHER_PROJECT_ID, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
    assert tuple(other_root.iterdir()) == ()


def test_created_entries_use_exclusive_nofollow_cloexec_dirfd_and_private_modes(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    original_open = revisions_module.os.open
    original_mkdir = revisions_module.os.mkdir
    created_files: list[tuple[int, int, int | None]] = []
    created_dirs: list[tuple[int, int | None]] = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if flags & os.O_CREAT:
            created_files.append((flags, mode, dir_fd))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    def tracked_mkdir(path, mode=0o777, *, dir_fd=None):
        created_dirs.append((mode, dir_fd))
        if dir_fd is None:
            return original_mkdir(path, mode)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "mkdir", tracked_mkdir)
        store.initialize_empty_project(PROJECT_ID, lease)
    assert created_files
    for flags, mode, dir_fd in created_files:
        assert flags & os.O_EXCL
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_CLOEXEC
        assert mode == 0o600
        assert type(dir_fd) is int
    assert created_dirs
    assert all(mode == 0o700 and type(dir_fd) is int for mode, dir_fd in created_dirs)


def test_atomic_rename_and_replace_are_dirfd_relative(store_parts, monkeypatch):
    store, manager, _root = store_parts
    original_rename = revisions_module.os.rename
    original_replace = revisions_module.os.replace
    rename_calls: list[tuple[object, object]] = []
    replace_calls: list[tuple[object, object]] = []

    def tracked_rename(src, dst, *args, **kwargs):
        rename_calls.append((kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd")))
        return original_rename(src, dst, *args, **kwargs)

    def tracked_replace(src, dst, *args, **kwargs):
        replace_calls.append((kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd")))
        return original_replace(src, dst, *args, **kwargs)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "rename", tracked_rename)
        monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
        head = store.initialize_empty_project(PROJECT_ID, lease)
        revision_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        store.commit_revision(PROJECT_ID, head, revision_id, lease)
    assert rename_calls
    assert replace_calls
    assert all(type(source) is int and type(target) is int for source, target in rename_calls)
    assert all(type(source) is int and type(target) is int for source, target in replace_calls)


def test_begin_fsyncs_staging_journal_and_project_directory_before_return(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    original_open = revisions_module.os.open
    original_fsync = revisions_module.os.fsync
    roles: dict[int, str] = {}
    events: list[tuple[str, str]] = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        roles[fd] = str(path)
        return fd

    def tracked_fsync(fd):
        events.append(("fsync", roles.get(fd, "<unknown>")))
        return original_fsync(fd)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "fsync", tracked_fsync)
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        journal_sync = next(
            index
            for index, event in enumerate(events)
            if event[0] == "fsync"
            and (event[1] == "journal.json" or event[1].startswith(".journal.json."))
        )
        project_sync = next(
            index
            for index, event in enumerate(events[journal_sync + 1 :], journal_sync + 1)
            if event == ("fsync", _project_dir(root).name)
        )
        assert journal_sync < project_sync < len(events)
        store.rollback_revision(PROJECT_ID, revision_id, lease)


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_staging_journal_fsync_failures_never_advance_head(
    store_parts, monkeypatch: pytest.MonkeyPatch, failure: str
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    before = _all_tree_bytes(_revision_dir(root, head.revision_id))
    original_open = revisions_module.os.open
    original_fsync = revisions_module.os.fsync
    roles: dict[int, str] = {}
    journal_synced = False
    failed = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        roles[fd] = str(path)
        return fd

    def targeted_fsync(fd):
        nonlocal failed, journal_synced
        role = roles.get(fd, "")
        is_journal = role == "journal.json" or role.startswith(".journal.json.")
        if failure == "file" and is_journal and not failed:
            failed = True
            raise OSError("SECRET staging journal file fsync")
        if (
            failure == "directory"
            and journal_synced
            and role == _project_dir(root).name
            and not failed
        ):
            failed = True
            raise OSError("SECRET staging journal directory fsync")
        result = original_fsync(fd)
        if is_journal:
            journal_synced = True
        return result

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "fsync", targeted_fsync)
        with pytest.raises(RevisionStoreError) as captured:
            store.begin_revision(PROJECT_ID, head, lease)
        if failure == "file":
            _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
        else:
            _assert_closed_error(
                captured.value,
                RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=False,
            )
        assert failed
        monkeypatch.setattr(revisions_module.os, "fsync", original_fsync)
        assert store.load_head(PROJECT_ID) == head
    assert _all_tree_bytes(_revision_dir(root, head.revision_id)) == before


def test_full_lifecycle_fsyncs_each_file_before_publish_and_each_containing_directory(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    original_open = revisions_module.os.open
    original_fsync = revisions_module.os.fsync
    original_rename = revisions_module.os.rename
    original_replace = revisions_module.os.replace
    original_rmdir = revisions_module.os.rmdir
    roles: dict[int, str] = {}
    events: list[tuple[str, str]] = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        roles[fd] = str(path)
        return fd

    def tracked_fsync(fd):
        events.append(("fsync", roles.get(fd, "<unknown>")))
        return original_fsync(fd)

    def tracked_rename(src, dst, *args, **kwargs):
        events.append(("rename", str(dst)))
        return original_rename(src, dst, *args, **kwargs)

    def tracked_replace(src, dst, *args, **kwargs):
        events.append(("replace", str(dst)))
        return original_replace(src, dst, *args, **kwargs)

    def tracked_rmdir(path, *args, **kwargs):
        events.append(("rmdir", str(path)))
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(revisions_module.os, "open", tracked_open)
    monkeypatch.setattr(revisions_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(revisions_module.os, "rename", tracked_rename)
    monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
    monkeypatch.setattr(revisions_module.os, "rmdir", tracked_rmdir)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        head = store.initialize_empty_project(PROJECT_ID, lease)
        revision_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        store.commit_revision(PROJECT_ID, head, revision_id, lease)

    revision_publish = next(
        index
        for index, event in enumerate(events)
        if event == ("rename", _path_key(REVISION_PATH_DOMAIN, revision_id))
    )
    before_revision = events[:revision_publish]
    assert ("fsync", "model.FCStd") in before_revision
    assert ("fsync", "model.step") in before_revision
    assert ("fsync", "manifest.json") in before_revision
    assert ("fsync", "revisions") in events[revision_publish + 1 :]

    all_journal_replaces = [
        index for index, event in enumerate(events) if event == ("replace", "journal.json")
    ]
    assert len(all_journal_replaces) >= 2
    journal_replaces = all_journal_replaces[-2:]
    head_replace = events.index(("replace", "HEAD.json"))
    journal_temp_syncs: list[int] = []
    journal_directory_syncs: list[int] = []
    for position, replace_index in enumerate(journal_replaces):
        lower = revision_publish if position == 0 else head_replace
        upper = head_replace if position == 0 else len(events)
        temp_sync = next(
            index
            for index in range(lower + 1, replace_index)
            if events[index][0] == "fsync" and events[index][1].startswith(".journal.json.")
        )
        directory_sync = next(
            index
            for index in range(replace_index + 1, upper)
            if events[index] == ("fsync", _project_dir(root).name)
        )
        assert lower < temp_sync < replace_index < directory_sync < upper
        journal_temp_syncs.append(temp_sync)
        journal_directory_syncs.append(directory_sync)

    head_temp_sync = next(
        index
        for index in range(journal_directory_syncs[0] + 1, head_replace)
        if events[index][0] == "fsync" and events[index][1].startswith(".HEAD.json.")
    )
    head_directory_sync = next(
        index
        for index in range(head_replace + 1, journal_temp_syncs[1])
        if events[index] == ("fsync", _project_dir(root).name)
    )
    assert (
        journal_directory_syncs[0]
        < head_temp_sync
        < head_replace
        < head_directory_sync
        < journal_temp_syncs[1]
    )
    project_publish = events.index(("rename", _project_dir(root).name))
    assert ("fsync", root.name) in events[project_publish + 1 :]
    if any(event[0] == "rmdir" for event in events):
        last_rmdir = max(index for index, event in enumerate(events) if event[0] == "rmdir")
        assert ("fsync", "candidates") in events[last_rmdir + 1 :]


@pytest.mark.parametrize(
    "target",
    ["model.FCStd", "model.step", "manifest.json", "prepared_journal"],
)
def test_each_prepublication_file_fsync_failure_is_not_committed(
    store_parts, monkeypatch: pytest.MonkeyPatch, target: str
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    before = _all_tree_bytes(_revision_dir(root, head.revision_id))
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        original_open = revisions_module.os.open
        original_fsync = revisions_module.os.fsync
        roles: dict[int, str] = {}
        failed = False

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                fd = original_open(path, flags, mode)
            else:
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
            roles[fd] = str(path)
            return fd

        def targeted_fsync(fd):
            nonlocal failed
            role = roles.get(fd, "")
            matches = role == target or (
                target == "prepared_journal" and role.startswith(".journal.json.")
            )
            if matches and not failed:
                failed = True
                raise OSError("SECRET targeted prepublication fsync")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "fsync", targeted_fsync)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
        assert failed
        assert store.load_head(PROJECT_ID) == head
    assert _all_tree_bytes(_revision_dir(root, head.revision_id)) == before


def test_revision_directory_fsync_uncertainty_is_pre_head(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        original_open = revisions_module.os.open
        original_fsync = revisions_module.os.fsync
        original_rename = revisions_module.os.rename
        roles: dict[int, str] = {}
        published = False

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                fd = original_open(path, flags, mode)
            else:
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
            roles[fd] = str(path)
            return fd

        def tracked_rename(src, dst, *args, **kwargs):
            nonlocal published
            result = original_rename(src, dst, *args, **kwargs)
            if dst == _path_key(REVISION_PATH_DOMAIN, revision_id):
                published = True
            return result

        def fail_revisions_dir(fd):
            if published and roles.get(fd) == "revisions":
                raise OSError("SECRET revisions dir fsync")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "rename", tracked_rename)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_revisions_dir)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        _assert_closed_error(
            captured.value,
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=False,
        )
        assert store.load_head(PROJECT_ID) == head


def test_prepared_journal_directory_fsync_uncertainty_is_not_committed(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        original_open = revisions_module.os.open
        original_fsync = revisions_module.os.fsync
        original_replace = revisions_module.os.replace
        roles: dict[int, str] = {}
        prepared_replaced = False
        failed = False

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                fd = original_open(path, flags, mode)
            else:
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
            roles[fd] = str(path)
            return fd

        def tracked_replace(src, dst, *args, **kwargs):
            nonlocal prepared_replaced
            result = original_replace(src, dst, *args, **kwargs)
            if dst == "journal.json":
                prepared_replaced = True
            return result

        def fail_project_dir_after_prepared(fd):
            nonlocal failed
            if prepared_replaced and not failed and roles.get(fd) == _project_dir(root).name:
                failed = True
                raise OSError("SECRET prepared journal directory fsync")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_project_dir_after_prepared)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        _assert_closed_error(
            captured.value,
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=False,
        )
        assert failed
        monkeypatch.setattr(revisions_module.os, "fsync", original_fsync)
        assert store.load_head(PROJECT_ID) == head
        result = store.reconcile(PROJECT_ID, lease)
        assert result.status is ReconciliationStatus.NOT_COMMITTED
        assert result.head == head


def test_head_and_post_head_journal_file_fsync_failures_classify_linearization(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        original_open = revisions_module.os.open
        original_fsync = revisions_module.os.fsync
        roles: dict[int, str] = {}

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                fd = original_open(path, flags, mode)
            else:
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
            roles[fd] = str(path)
            return fd

        def fail_head_temp(fd):
            if roles.get(fd, "").startswith(".HEAD.json."):
                raise OSError("SECRET head temp fsync")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_head_temp)
        with pytest.raises(RevisionStoreError) as captured:
            store.commit_revision(PROJECT_ID, head, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
        assert store.load_head(PROJECT_ID) == head

        monkeypatch.setattr(revisions_module.os, "fsync", original_fsync)
        original_replace = revisions_module.os.replace
        head_replaced = False

        def tracked_replace(src, dst, *args, **kwargs):
            nonlocal head_replaced
            result = original_replace(src, dst, *args, **kwargs)
            if dst == "HEAD.json":
                head_replaced = True
            return result

        def fail_committed_journal_temp(fd):
            if head_replaced and roles.get(fd, "").startswith(".journal.json."):
                raise OSError("SECRET committed journal fsync")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_committed_journal_temp)
        with pytest.raises(RevisionStoreError) as captured:
            store.commit_revision(PROJECT_ID, head, revision_id, lease)
        _assert_closed_error(
            captured.value,
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )
        assert store.load_head(PROJECT_ID).revision_id == revision_id


def test_close_failure_before_revision_publish_is_io_error_and_preserves_head(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        original_open = revisions_module.os.open
        original_close = revisions_module.os.close
        roles: dict[int, str] = {}
        failed = False

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                fd = original_open(path, flags, mode)
            else:
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
            roles[fd] = str(path)
            return fd

        def fail_manifest_close(fd):
            nonlocal failed
            result = original_close(fd)
            if roles.get(fd) == "manifest.json" and not failed:
                failed = True
                raise OSError("SECRET close")
            return result

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "close", fail_manifest_close)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
        assert failed
        assert store.load_head(PROJECT_ID) == head


def test_read_error_is_redacted_and_does_not_change_durable_bytes(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    before = _all_tree_bytes(root)
    original_read = revisions_module.os.read

    def fail_read(_fd, _count):
        raise OSError("SECRET read errno and path")

    monkeypatch.setattr(revisions_module.os, "read", fail_read)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
    assert _all_tree_bytes(root) == before
    monkeypatch.setattr(revisions_module.os, "read", original_read)
    assert store.load_head(PROJECT_ID) == head


def test_prepublication_fsync_failure_cleans_owned_temporary_project(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts

    def fail_fsync(_fd):
        raise OSError("SECRET fsync")

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "fsync", fail_fsync)
        with pytest.raises(RevisionStoreError) as captured:
            store.initialize_empty_project(PROJECT_ID, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
    assert tuple(root.iterdir()) == ()


def test_post_project_publication_fsync_failure_is_durability_uncertain(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    project_name = _project_dir(root).name
    original_rename = revisions_module.os.rename
    original_fsync = revisions_module.os.fsync
    published = False

    def tracked_rename(src, dst, *args, **kwargs):
        nonlocal published
        result = original_rename(src, dst, *args, **kwargs)
        if dst == project_name:
            published = True
        return result

    def fail_after_publication(fd):
        if published:
            raise OSError("SECRET publication fsync")
        return original_fsync(fd)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "rename", tracked_rename)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_after_publication)
        with pytest.raises(RevisionStoreError) as captured:
            store.initialize_empty_project(PROJECT_ID, lease)
    _assert_closed_error(
        captured.value,
        RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
        head_committed=True,
    )
    monkeypatch.setattr(revisions_module.os, "fsync", original_fsync)
    assert store.load_head(PROJECT_ID).generation == 0


def test_revision_publish_and_prepared_journal_failures_remain_not_committed(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    before = _all_tree_bytes(_revision_dir(root, head.revision_id))
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        destination = _path_key(REVISION_PATH_DOMAIN, revision_id)
        original_rename = revisions_module.os.rename

        def fail_revision_publish(src, dst, *args, **kwargs):
            if dst == destination:
                raise OSError("SECRET revision rename")
            return original_rename(src, dst, *args, **kwargs)

        monkeypatch.setattr(revisions_module.os, "rename", fail_revision_publish)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
        assert store.load_head(PROJECT_ID) == head
        monkeypatch.setattr(revisions_module.os, "rename", original_rename)
        result = store.reconcile(PROJECT_ID, lease)
        assert result.status is ReconciliationStatus.NOT_COMMITTED
    assert _all_tree_bytes(_revision_dir(root, head.revision_id)) == before


def test_prepared_journal_replace_failure_preserves_old_head_and_orphan(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        original_replace = revisions_module.os.replace

        def fail_prepared_journal(src, dst, *args, **kwargs):
            if dst == "journal.json":
                raise OSError("SECRET prepared journal")
            return original_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(revisions_module.os, "replace", fail_prepared_journal)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
        monkeypatch.setattr(revisions_module.os, "replace", original_replace)
        assert store.load_head(PROJECT_ID) == head
        result = store.reconcile(PROJECT_ID, lease)
        assert result.status is ReconciliationStatus.NOT_COMMITTED
        assert store.load_revision(PROJECT_ID, revision_id).id == revision_id


def test_candidate_cleanup_failure_is_explicit_and_never_advances_head(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        store.candidate_model_path(PROJECT_ID, revision_id, lease).write_bytes(b"partial")
        original_unlink = revisions_module.os.unlink

        def fail_candidate_unlink(path, *args, **kwargs):
            if path == "model.FCStd":
                raise OSError("SECRET cleanup path")
            return original_unlink(path, *args, **kwargs)

        monkeypatch.setattr(revisions_module.os, "unlink", fail_candidate_unlink)
        result = store.rollback_revision(PROJECT_ID, revision_id, lease)
        assert result.status is ReconciliationStatus.CLEANUP_REQUIRED
        assert result.head == head
        assert result.journal is not None
        assert result.journal.state is CommitJournalState.NOT_COMMITTED
    assert store.load_head(PROJECT_ID) == head


@pytest.mark.parametrize("failure", ["rmdir", "candidates_fsync"])
def test_candidate_directory_cleanup_failures_are_explicit_and_never_advance_head(
    store_parts, monkeypatch: pytest.MonkeyPatch, failure: str
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        candidate_name = _candidate_dir(root, revision_id).name
        original_open = revisions_module.os.open
        original_rmdir = revisions_module.os.rmdir
        original_fsync = revisions_module.os.fsync
        roles: dict[int, str] = {}
        removed = False

        def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is None:
                fd = original_open(path, flags, mode)
            else:
                fd = original_open(path, flags, mode, dir_fd=dir_fd)
            roles[fd] = str(path)
            return fd

        def targeted_rmdir(path, *args, **kwargs):
            nonlocal removed
            if failure == "rmdir" and path == candidate_name:
                raise OSError("SECRET candidate rmdir")
            result = original_rmdir(path, *args, **kwargs)
            if path == candidate_name:
                removed = True
            return result

        def targeted_fsync(fd):
            if failure == "candidates_fsync" and removed and roles.get(fd) == "candidates":
                raise OSError("SECRET candidates directory fsync")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "rmdir", targeted_rmdir)
        monkeypatch.setattr(revisions_module.os, "fsync", targeted_fsync)
        result = store.rollback_revision(PROJECT_ID, revision_id, lease)
        assert result.status is ReconciliationStatus.CLEANUP_REQUIRED
        assert result.head == head
        assert result.journal is not None
        assert result.journal.state is CommitJournalState.NOT_COMMITTED
    assert store.load_head(PROJECT_ID) == head


def test_failure_before_head_replace_keeps_old_head_and_reconciles_not_committed(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    old_bytes = _all_tree_bytes(_revision_dir(root, head.revision_id))
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        original_replace = revisions_module.os.replace

        def fail_head_replace(src, dst, *args, **kwargs):
            if dst == "HEAD.json":
                raise OSError("SECRET native path")
            return original_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(revisions_module.os, "replace", fail_head_replace)
        with pytest.raises(RevisionStoreError) as captured:
            store.commit_revision(PROJECT_ID, head, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
        monkeypatch.setattr(revisions_module.os, "replace", original_replace)
        assert store.load_head(PROJECT_ID) == head
        result = store.reconcile(PROJECT_ID, lease)
        assert result.status is ReconciliationStatus.NOT_COMMITTED
        assert result.head == head
    assert _all_tree_bytes(_revision_dir(root, head.revision_id)) == old_bytes


def test_failure_after_head_replace_never_rolls_back_and_is_durability_uncertain(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
        original_replace = revisions_module.os.replace
        original_fsync = revisions_module.os.fsync
        head_replaced = False
        failed = False

        def tracked_replace(src, dst, *args, **kwargs):
            nonlocal head_replaced
            result = original_replace(src, dst, *args, **kwargs)
            if dst == "HEAD.json":
                head_replaced = True
            return result

        def fail_first_post_head_fsync(fd):
            nonlocal failed
            if head_replaced and not failed:
                failed = True
                raise OSError("SECRET fsync detail")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_first_post_head_fsync)
        with pytest.raises(RevisionStoreError) as captured:
            store.commit_revision(PROJECT_ID, head, revision_id, lease)
        _assert_closed_error(
            captured.value,
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )
        monkeypatch.setattr(revisions_module.os, "fsync", original_fsync)
        new_head = store.load_head(PROJECT_ID)
        assert new_head.revision_id == revision_id
        assert new_head.manifest_sha256 == sealed.manifest_sha256
        reconciled = store.reconcile(PROJECT_ID, lease)
        assert reconciled.status is ReconciliationStatus.COMMITTED
        assert reconciled.head == new_head


def test_failure_recording_committed_journal_leaves_prepared_evidence_for_reconcile(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        original_replace = revisions_module.os.replace
        head_seen = False

        def fail_committed_journal(src, dst, *args, **kwargs):
            nonlocal head_seen
            if dst == "HEAD.json":
                result = original_replace(src, dst, *args, **kwargs)
                head_seen = True
                return result
            if head_seen and dst == "journal.json":
                raise OSError("SECRET journal detail")
            return original_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(revisions_module.os, "replace", fail_committed_journal)
        with pytest.raises(RevisionStoreError) as captured:
            store.commit_revision(PROJECT_ID, head, revision_id, lease)
        _assert_closed_error(
            captured.value,
            RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
            head_committed=True,
        )
        assert store.load_head(PROJECT_ID).revision_id == revision_id
        monkeypatch.setattr(revisions_module.os, "replace", original_replace)
        result = store.reconcile(PROJECT_ID, lease)
        assert result.status is ReconciliationStatus.COMMITTED
        assert result.journal is not None
        assert result.journal.state is CommitJournalState.COMMITTED


def test_corrupt_head_or_manifest_never_allows_reconcile_to_guess(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        head_path = _project_dir(root) / "HEAD.json"
        head_path.write_bytes(b"{}")
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        assert captured.value.code is RevisionStoreErrorCode.RECOVERY_REQUIRED


def test_commit_and_reconcile_reject_candidate_with_wrong_base_lineage(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        _rewrite_candidate_lineage(
            root,
            revision_id,
            REVISION_C,
            CommitJournalState.PREPARED,
        )
        before_head = head_path.read_bytes()
        with pytest.raises(RevisionStoreError) as captured:
            store.commit_revision(PROJECT_ID, head, revision_id, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.CORRUPT_RECORD)
        assert head_path.read_bytes() == before_head
        assert store.load_head(PROJECT_ID) == head
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)


@pytest.mark.parametrize(
    "journal_state",
    [CommitJournalState.PREPARED, CommitJournalState.COMMITTED],
)
def test_reconcile_rejects_new_head_with_wrong_candidate_base(
    store_parts,
    journal_state: CommitJournalState,
):
    store, manager, root = store_parts
    old_head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, old_head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        digest = _rewrite_candidate_lineage(root, revision_id, REVISION_C, journal_state)
        _write_test_head(root, old_head, revision_id, digest)
        with pytest.raises(RevisionStoreError) as captured:
            store.reconcile(PROJECT_ID, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)


def test_begin_never_consumes_terminal_wrong_lineage_evidence(store_parts):
    store, manager, root = store_parts
    old_head = _initialize_empty(store, manager)
    journal_path = _project_dir(root) / "journal.json"
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, old_head)
        store.seal_revision(PROJECT_ID, revision_id, lease)
        digest = _rewrite_candidate_lineage(
            root,
            revision_id,
            REVISION_C,
            CommitJournalState.COMMITTED,
        )
        new_head = _write_test_head(root, old_head, revision_id, digest)
        before_journal = journal_path.read_bytes()
        with pytest.raises(RevisionStoreError) as captured:
            store.begin_revision(PROJECT_ID, new_head, lease)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)
        assert journal_path.read_bytes() == before_journal


def test_root_component_close_fault_attempts_each_owned_fd_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "nested" / "store"
    root.mkdir(parents=True, mode=0o700)
    os.chmod(root.parent, 0o700)
    os.chmod(root, 0o700)
    lock_root = tmp_path / "locks"
    lock_root.mkdir(mode=0o700)
    os.chmod(lock_root, 0o700)
    manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    original_open = revisions_module.os.open
    original_close = revisions_module.os.close
    opened: set[int] = set()
    attempted: dict[int, int] = {}
    closed: set[int] = set()
    injected = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.add(fd)
        return fd

    def close_fault(fd):
        nonlocal injected
        attempted[fd] = attempted.get(fd, 0) + 1
        result = original_close(fd)
        closed.add(fd)
        if not injected:
            injected = True
            raise OSError("SECRET root traversal close")
        return result

    monkeypatch.setattr(revisions_module.os, "open", tracked_open)
    monkeypatch.setattr(revisions_module.os, "close", close_fault)
    try:
        with pytest.raises(RevisionStoreError) as captured:
            LocalRevisionStore(root, manager, trust=RevisionStoreRootTrust.TRUSTED_LOCAL)
        _assert_closed_error(captured.value, RevisionStoreErrorCode.UNSAFE_STORE)
        assert injected
        assert opened <= attempted.keys()
        assert all(attempted[fd] == 1 for fd in opened)
        assert opened <= closed
    finally:
        for fd in opened - closed:
            try:
                original_close(fd)
            except OSError:
                pass


def test_external_source_parent_close_fault_attempts_each_owned_fd_once(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    source_parent = tmp_path / "nested" / "source"
    source_parent.mkdir(parents=True)
    source = source_parent / "base.FCStd"
    source.write_bytes(b"trusted")
    original_open = revisions_module.os.open
    original_close = revisions_module.os.close
    opened: set[int] = set()
    attempted: dict[int, int] = {}
    closed: set[int] = set()
    injected = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        opened.add(fd)
        return fd

    def close_fault(fd):
        nonlocal injected
        attempted[fd] = attempted.get(fd, 0) + 1
        result = original_close(fd)
        closed.add(fd)
        if not injected:
            injected = True
            raise OSError("SECRET source traversal close")
        return result

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "close", close_fault)
        try:
            with pytest.raises(RevisionStoreError) as captured:
                _import_trusted(store, PROJECT_ID, source, lease)
            _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
            assert injected
            assert opened <= attempted.keys()
            assert all(attempted[fd] == 1 for fd in opened)
            assert opened <= closed
        finally:
            for fd in opened - closed:
                try:
                    original_close(fd)
                except OSError:
                    pass


def test_import_attempts_source_and_parent_close_after_first_close_fault(
    store_parts, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store, manager, _root = store_parts
    source = tmp_path / "base.FCStd"
    source.write_bytes(b"trusted")
    original_open = revisions_module.os.open
    original_close = revisions_module.os.close
    source_fd = None
    source_parent_fd = None
    last_directory_fd = None
    attempted: list[int] = []

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal last_directory_fd, source_fd, source_parent_fd
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_DIRECTORY:
            last_directory_fd = fd
        if str(path) == str(source) and dir_fd is None:
            source_fd = fd
            source_parent_fd = last_directory_fd
        return fd

    def fail_source_closes(fd):
        attempted.append(fd)
        result = original_close(fd)
        if fd == source_fd or fd == source_parent_fd:
            raise OSError("SECRET imported source close")
        return result

    with manager.acquire_project_write(PROJECT_ID) as lease:
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "close", fail_source_closes)
        with pytest.raises(RevisionStoreError) as captured:
            _import_trusted(store, PROJECT_ID, source, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
    assert source_fd is not None and source_parent_fd is not None
    assert source_fd in attempted
    assert source_parent_fd in attempted


def test_candidate_authority_close_failure_is_explicit(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    original_open = revisions_module.os.open
    original_close = revisions_module.os.close
    candidate_fds: set[int] = set()
    candidate_name = None
    failed = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == candidate_name:
            candidate_fds.add(fd)
        return fd

    def fail_candidates_close(fd):
        nonlocal failed
        result = original_close(fd)
        if fd in candidate_fds and not failed:
            failed = True
            raise OSError("SECRET candidate authority close")
        return result

    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        candidate_name = _candidate_dir(root, revision_id).name
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "close", fail_candidates_close)
        with pytest.raises(RevisionStoreError) as captured:
            store.candidate_model_path(PROJECT_ID, revision_id, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.IO_ERROR)
    assert failed


@pytest.mark.parametrize("failure", ["stat", "unlink", "rmdir", "candidates_fsync", "close"])
def test_seal_candidate_cleanup_failure_is_never_reported_as_success(
    store_parts, monkeypatch: pytest.MonkeyPatch, failure: str
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    original_open = revisions_module.os.open
    original_close = revisions_module.os.close
    original_replace = revisions_module.os.replace
    original_stat = revisions_module.os.stat
    original_unlink = revisions_module.os.unlink
    original_rmdir = revisions_module.os.rmdir
    original_fsync = revisions_module.os.fsync
    roles: dict[int, str] = {}
    candidate_name = None
    candidate_close_count = 0
    candidate_removed = False
    prepared_published = False
    failed = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        roles[fd] = str(path)
        return fd

    def targeted_unlink(path, *args, **kwargs):
        nonlocal failed
        if failure == "unlink" and path == "model.FCStd" and not failed:
            failed = True
            raise OSError("SECRET seal cleanup unlink")
        return original_unlink(path, *args, **kwargs)

    def targeted_stat(path, *args, **kwargs):
        nonlocal failed
        if failure == "stat" and prepared_published and path == "model.FCStd" and not failed:
            failed = True
            raise PermissionError("SECRET seal cleanup stat")
        return original_stat(path, *args, **kwargs)

    def tracked_replace(source, destination, *args, **kwargs):
        nonlocal prepared_published
        result = original_replace(source, destination, *args, **kwargs)
        if destination == "journal.json":
            prepared_published = True
        return result

    def targeted_rmdir(path, *args, **kwargs):
        nonlocal candidate_removed, failed
        if failure == "rmdir" and path == candidate_name and not failed:
            failed = True
            raise OSError("SECRET seal cleanup rmdir")
        result = original_rmdir(path, *args, **kwargs)
        if path == candidate_name:
            candidate_removed = True
        return result

    def targeted_fsync(fd):
        nonlocal failed
        if (
            failure == "candidates_fsync"
            and candidate_removed
            and roles.get(fd) == "candidates"
            and not failed
        ):
            failed = True
            raise OSError("SECRET seal candidates fsync")
        return original_fsync(fd)

    def targeted_close(fd):
        nonlocal candidate_close_count, failed
        result = original_close(fd)
        if roles.get(fd) == candidate_name:
            candidate_close_count += 1
        if failure == "close" and candidate_close_count == 2 and not failed:
            failed = True
            raise OSError("SECRET seal candidate close")
        return result

    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = _begin_and_fill(store, lease, head)
        candidate_name = _candidate_dir(root, revision_id).name
        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
        monkeypatch.setattr(revisions_module.os, "stat", targeted_stat)
        monkeypatch.setattr(revisions_module.os, "unlink", targeted_unlink)
        monkeypatch.setattr(revisions_module.os, "rmdir", targeted_rmdir)
        monkeypatch.setattr(revisions_module.os, "fsync", targeted_fsync)
        monkeypatch.setattr(revisions_module.os, "close", targeted_close)
        with pytest.raises(RevisionStoreError) as captured:
            store.seal_revision(PROJECT_ID, revision_id, lease)
    _assert_closed_error(
        captured.value,
        RevisionStoreErrorCode.CLEANUP_REQUIRED,
    )
    assert failed
    assert store.load_head(PROJECT_ID) == head


@pytest.mark.parametrize("operation", ["reconcile", "begin"])
def test_journal_stat_error_is_recovery_evidence_and_is_never_overwritten(
    store_parts, monkeypatch: pytest.MonkeyPatch, operation: str
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        store.begin_revision(PROJECT_ID, head, lease)
    original_stat = revisions_module.os.stat
    journal = _project_dir(root) / "journal.json"
    before = journal.read_bytes()
    injected = False

    def fail_journal_stat(path, *args, **kwargs):
        nonlocal injected
        if path == "journal.json":
            injected = True
            raise PermissionError("SECRET journal stat")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(revisions_module.os, "stat", fail_journal_stat)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        with pytest.raises(RevisionStoreError) as captured:
            if operation == "reconcile":
                store.reconcile(PROJECT_ID, lease)
            else:
                store.begin_revision(PROJECT_ID, head, lease)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.RECOVERY_REQUIRED)
    assert injected
    assert journal.read_bytes() == before


def test_head_inode_change_between_stat_and_open_retries_to_a_stable_record(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    replacement = head_path.with_name("HEAD.swap")
    replacement.write_bytes(head_path.read_bytes())
    os.chmod(replacement, 0o600)
    original_open = revisions_module.os.open
    replaced = False
    head_open_count = 0

    def replace_before_head_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal head_open_count, replaced
        if path == "HEAD.json":
            head_open_count += 1
            if not replaced:
                replaced = True
                replacement.replace(head_path)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(revisions_module.os, "open", replace_before_head_open)
    assert store.load_head(PROJECT_ID) == head
    assert replaced
    assert head_open_count == 2


def test_head_replaced_after_open_is_not_rejected_as_unsafe(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    replacement = head_path.with_name("HEAD.after-open")
    replacement.write_bytes(head_path.read_bytes())
    os.chmod(replacement, 0o600)
    original_open = revisions_module.os.open
    original_fstat = revisions_module.os.fstat
    head_fd = None
    replaced = False

    def track_head_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal head_fd
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "HEAD.json" and head_fd is None:
            head_fd = fd
        return fd

    def replace_before_head_fstat(fd):
        nonlocal replaced
        if fd == head_fd and not replaced:
            replaced = True
            replacement.replace(head_path)
        return original_fstat(fd)

    monkeypatch.setattr(revisions_module.os, "open", track_head_open)
    monkeypatch.setattr(revisions_module.os, "fstat", replace_before_head_fstat)
    assert store.load_head(PROJECT_ID) == head
    assert head_fd is not None
    assert replaced


def test_head_replaced_after_initial_fstat_remains_complete_old_or_new(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    replacement = head_path.with_name("HEAD.after-fstat")
    replacement.write_bytes(head_path.read_bytes())
    os.chmod(replacement, 0o600)
    original_open = revisions_module.os.open
    original_fstat = revisions_module.os.fstat
    head_fd = None
    replaced = False

    def track_head_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal head_fd
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "HEAD.json" and head_fd is None:
            head_fd = fd
        return fd

    def replace_after_initial_head_fstat(fd):
        nonlocal replaced
        result = original_fstat(fd)
        if fd == head_fd and not replaced:
            replaced = True
            replacement.replace(head_path)
        return result

    monkeypatch.setattr(revisions_module.os, "open", track_head_open)
    monkeypatch.setattr(revisions_module.os, "fstat", replace_after_initial_head_fstat)
    assert store.load_head(PROJECT_ID) == head
    assert head_fd is not None
    assert replaced


def test_head_path_stat_unlinked_by_atomic_replace_retries(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    replacement = head_path.with_name("HEAD.during-stat")
    replacement.write_bytes(head_path.read_bytes())
    os.chmod(replacement, 0o600)
    original_open = revisions_module.os.open
    original_close = revisions_module.os.close
    original_fstat = revisions_module.os.fstat
    original_stat = revisions_module.os.stat
    injected = False
    observed_unlinked = False

    def stat_unlinked_old_head(path, *args, **kwargs):
        nonlocal injected, observed_unlinked
        if path == "HEAD.json" and not injected:
            injected = True
            held_fd = original_open(
                path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=kwargs["dir_fd"],
            )
            try:
                replacement.replace(head_path)
                result = original_fstat(held_fd)
                observed_unlinked = result.st_nlink == 0
                return result
            finally:
                original_close(held_fd)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(revisions_module.os, "stat", stat_unlinked_old_head)
    assert store.load_head(PROJECT_ID) == head
    assert injected
    assert observed_unlinked


def test_candidate_only_without_journal_and_complete_orphan_never_become_head(store_parts):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        journal_path = _project_dir(root) / "journal.json"
        journal_path.unlink()
        project_fd = os.open(_project_dir(root), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(project_fd)
        finally:
            os.close(project_fd)
        assert store.reconcile(PROJECT_ID, lease).status is ReconciliationStatus.CLEAN
        assert store.load_head(PROJECT_ID) == head
        assert _candidate_dir(root, revision_id).exists()


def test_concurrent_atomic_readers_observe_only_complete_old_or_new_heads(store_parts):
    store, manager, _root = store_parts
    old = _initialize_empty(store, manager)
    observed: list[ProjectHead] = []
    errors: list[BaseException] = []
    stop = threading.Event()
    started = threading.Event()
    saw_new = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                item = store.load_head(PROJECT_ID)
                observed.append(item)
                started.set()
                if item != old:
                    saw_new.set()
        except BaseException as exc:
            errors.append(exc)
            started.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert started.wait(timeout=5)
        with manager.acquire_project_write(PROJECT_ID) as lease:
            revision_id = _begin_and_fill(store, lease, old)
            sealed = store.seal_revision(PROJECT_ID, revision_id, lease)
            new = store.commit_revision(PROJECT_ID, old, revision_id, lease)
        assert saw_new.wait(timeout=5)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert observed
    assert set(observed) <= {old, new}
    assert any(item == old for item in observed)
    assert any(item == new for item in observed)
    assert new.revision_id == sealed.id


def test_same_project_second_writer_is_contended_and_stale_writer_conflicts(store_parts, roots):
    store, manager, _root = store_parts
    _store_root, lock_root = roots
    second_manager = ResourceLeaseManager(lock_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    head = _initialize_empty(store, manager)
    acquisition_errors: list[BaseException] = []

    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)

        def contend():
            try:
                contender = second_manager.acquire_project_write(PROJECT_ID)
            except BaseException as exc:
                acquisition_errors.append(exc)
            else:
                acquisition_errors.append(
                    AssertionError("second same-project writer unexpectedly acquired lease")
                )
                contender.release(owner_token=contender.owner_token)

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert len(acquisition_errors) == 1
        assert type(acquisition_errors[0]) is LeaseError
        assert acquisition_errors[0].code is LeaseErrorCode.CONTENDED
        store.rollback_revision(PROJECT_ID, revision_id, lease)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        current_id = _begin_and_fill(store, lease, head)
        store.seal_revision(PROJECT_ID, current_id, lease)
        committed = store.commit_revision(PROJECT_ID, head, current_id, lease)
    stale_errors: list[BaseException] = []

    def stale_writer():
        try:
            with manager.acquire_project_write(PROJECT_ID) as stale_lease:
                store.begin_revision(PROJECT_ID, head, stale_lease)
        except BaseException as exc:
            stale_errors.append(exc)

    thread = threading.Thread(target=stale_writer)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(stale_errors) == 1
    assert type(stale_errors[0]) is RevisionStoreError
    assert stale_errors[0].code is RevisionStoreErrorCode.CONFLICT
    assert store.load_head(PROJECT_ID) == committed


def test_different_projects_can_begin_independently_under_distinct_live_leases(store_parts):
    store, manager, _root = store_parts
    first_head = _initialize_empty(store, manager)
    second_head = _initialize_empty(store, manager, OTHER_PROJECT_ID)
    first_lease = manager.acquire_project_write(PROJECT_ID)
    second_lease = manager.acquire_project_write(OTHER_PROJECT_ID)
    barrier = threading.Barrier(3)
    results: dict[str, str] = {}
    errors: list[BaseException] = []

    def begin(project_id, head, lease):
        try:
            barrier.wait(timeout=5)
            results[project_id] = store.begin_revision(project_id, head, lease)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(
        target=begin,
        args=(PROJECT_ID, first_head, first_lease),
        daemon=True,
    )
    second = threading.Thread(
        target=begin,
        args=(OTHER_PROJECT_ID, second_head, second_lease),
        daemon=True,
    )
    started_threads: list[threading.Thread] = []
    try:
        first.start()
        started_threads.append(first)
        second.start()
        started_threads.append(second)
        barrier.wait(timeout=5)
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive() and not second.is_alive()
        assert errors == []
        assert set(results) == {PROJECT_ID, OTHER_PROJECT_ID}
        assert results[PROJECT_ID] != results[OTHER_PROJECT_ID]
        store.rollback_revision(PROJECT_ID, results[PROJECT_ID], first_lease)
        store.rollback_revision(OTHER_PROJECT_ID, results[OTHER_PROJECT_ID], second_lease)
    finally:
        barrier.abort()
        for thread in started_threads:
            thread.join(timeout=5)
        try:
            assert all(not thread.is_alive() for thread in started_threads)
        finally:
            if not first_lease.released:
                first_lease.release(owner_token=first_lease.owner_token)
            if not second_lease.released:
                second_lease.release(owner_token=second_lease.owner_token)


def test_record_size_depth_node_and_string_budgets_are_enforced(store_parts, monkeypatch):
    store, manager, root = store_parts
    _initialize_empty(store, manager)
    head_path = _project_dir(root) / "HEAD.json"
    original = head_path.read_bytes()
    monkeypatch.setattr(revisions_module, "_MAX_HEAD_BYTES", len(original))
    assert store.load_head(PROJECT_ID).project_id == PROJECT_ID
    monkeypatch.setattr(revisions_module, "_MAX_HEAD_BYTES", len(original) - 1)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.BUDGET_EXCEEDED

    monkeypatch.setattr(revisions_module, "_MAX_HEAD_BYTES", 16384)
    deep = b"[" * 65 + b"]" * 65
    head_path.write_bytes(deep)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_RECORD


def test_manifest_journal_node_and_string_resource_budgets_are_independent(
    store_parts, monkeypatch: pytest.MonkeyPatch
):
    store, manager, root = store_parts
    head = _initialize_empty(store, manager)
    manifest = _revision_dir(root, head.revision_id) / "manifest.json"
    manifest_size = manifest.stat().st_size
    monkeypatch.setattr(revisions_module, "_MAX_MANIFEST_BYTES", manifest_size)
    assert store.load_revision(PROJECT_ID, head.revision_id).id == head.revision_id
    monkeypatch.setattr(revisions_module, "_MAX_MANIFEST_BYTES", manifest_size - 1)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_revision(PROJECT_ID, head.revision_id)
    assert captured.value.code is RevisionStoreErrorCode.BUDGET_EXCEEDED
    monkeypatch.setattr(revisions_module, "_MAX_MANIFEST_BYTES", 262144)

    with manager.acquire_project_write(PROJECT_ID) as lease:
        revision_id = store.begin_revision(PROJECT_ID, head, lease)
        journal = _project_dir(root) / "journal.json"
        journal_size = journal.stat().st_size
        monkeypatch.setattr(revisions_module, "_MAX_JOURNAL_BYTES", journal_size)
        assert (
            store.candidate_model_path(PROJECT_ID, revision_id, lease)
            == _candidate_dir(root, revision_id) / "model.FCStd"
        )
        monkeypatch.setattr(revisions_module, "_MAX_JOURNAL_BYTES", journal_size - 1)
        with pytest.raises(RevisionStoreError) as captured:
            store.candidate_model_path(PROJECT_ID, revision_id, lease)
        assert captured.value.code is RevisionStoreErrorCode.RECOVERY_REQUIRED
        monkeypatch.setattr(revisions_module, "_MAX_JOURNAL_BYTES", 32768)
        assert revision_id

    monkeypatch.setattr(revisions_module, "_MAX_JSON_NODES", 3)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_RECORD
    monkeypatch.setattr(revisions_module, "_MAX_JSON_NODES", 4096)
    monkeypatch.setattr(revisions_module, "_MAX_JSON_STRING_BYTES", 3)
    with pytest.raises(RevisionStoreError) as captured:
        store.load_head(PROJECT_ID)
    assert captured.value.code is RevisionStoreErrorCode.CORRUPT_RECORD


def test_json_depth_node_and_utf8_string_limits_accept_exact_and_reject_one_over():
    assert revisions_module._MAX_JSON_DEPTH == 64
    assert revisions_module._MAX_JSON_NODES == 4096
    assert revisions_module._MAX_JSON_STRING_BYTES == 4096

    exact_depth = b"[" * 64 + b"0" + b"]" * 64
    over_depth = b"[" * 65 + b"0" + b"]" * 65
    assert revisions_module._json_depth_is_safe(exact_depth)
    assert not revisions_module._json_depth_is_safe(over_depth)

    revisions_module._validate_json_resources([None] * 4095)
    with pytest.raises(RevisionStoreError) as captured:
        revisions_module._validate_json_resources([None] * 4096)
    _assert_closed_error(captured.value, RevisionStoreErrorCode.CORRUPT_RECORD)

    revisions_module._validate_json_resources("é" * 2048)
    with pytest.raises(RevisionStoreError) as captured:
        revisions_module._validate_json_resources("é" * 2048 + "x")
    _assert_closed_error(captured.value, RevisionStoreErrorCode.CORRUPT_RECORD)


def test_import_and_execution_modules_remain_free_of_forbidden_dependencies_and_dynamic_io():
    source_path = Path(inspect.getsourcefile(revisions_module) or "")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    allowed_os_calls = {
        "close",
        "fchmod",
        "fstat",
        "fsync",
        "get_inheritable",
        "geteuid",
        "getpid",
        "mkdir",
        "open",
        "read",
        "rename",
        "replace",
        "rmdir",
        "stat",
        "unlink",
        "write",
    }
    allowed_os_attributes = allowed_os_calls | {
        "O_CLOEXEC",
        "O_CREAT",
        "O_DIRECTORY",
        "O_EXCL",
        "O_NOFOLLOW",
        "O_NONBLOCK",
        "O_RDONLY",
        "O_RDWR",
        "O_TRUNC",
        "O_WRONLY",
    }
    allowed_module_symbols = {
        "__future__": {"annotations"},
        "dataclasses": {"dataclass"},
        "enum": {"StrEnum"},
        "hashlib": {"sha256"},
        "json": {"JSONDecodeError", "dumps", "loads"},
        "os": allowed_os_attributes,
        "pathlib": {"Path"},
        "re": {"fullmatch"},
        "secrets": {"token_hex"},
        "stat": {"S_IMODE", "S_ISDIR", "S_ISREG"},
        "vibecad.workflow.errors": {"MAX_SAFE_JSON_INTEGER"},
        "vibecad.workflow.lease": {"ProjectWriteLease", "ResourceLeaseManager"},
    }
    allowed_module_calls = {
        "__future__": set(),
        "dataclasses": {"dataclass"},
        "enum": set(),
        "hashlib": {"sha256"},
        "json": {"dumps", "loads"},
        "os": allowed_os_calls,
        "pathlib": {"Path"},
        "re": {"fullmatch"},
        "secrets": {"token_hex"},
        "stat": {"S_IMODE", "S_ISDIR", "S_ISREG"},
        "vibecad.workflow.errors": set(),
        "vibecad.workflow.lease": set(),
    }
    allowed_builtin_calls = {
        "TypeError",
        "ValueError",
        "bytes",
        "str",
        "type",
    }
    protected_builtin_names = allowed_builtin_calls | {
        "OSError",
        "UnicodeDecodeError",
        "dict",
        "len",
        "list",
        "staticmethod",
        "tuple",
    }
    allowed_non_module_attribute_calls = {
        "hexdigest",
        "update",
    }
    local_callables = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    imports: set[str] = set()
    module_names: dict[str, str] = {}
    imported_names: dict[str, tuple[str, str]] = {}
    imported_bound_names: set[str] = set()
    binding_counts: dict[str, int] = {}
    future_annotation_imports = 0

    def record_binding(name: str | None) -> None:
        if name is not None:
            binding_counts[name] = binding_counts.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
                assert alias.name in allowed_module_symbols
                assert alias.name != "__future__"
                if "." in alias.name:
                    assert alias.asname is not None
                bound_name = alias.asname or alias.name
                record_binding(bound_name)
                module_names[bound_name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            assert node.level == 0
            imports.add(node.module)
            assert node.module in allowed_module_symbols
            if node.module == "__future__":
                assert len(node.names) == 1
                assert node.names[0].name == "annotations"
                assert node.names[0].asname is None
                future_annotation_imports += 1
            for alias in node.names:
                assert alias.name in allowed_module_symbols[node.module]
                assert node.module != "os" or alias.name != "getpid"
                bound_name = alias.asname or alias.name
                record_binding(bound_name)
                imported_bound_names.add(bound_name)
                imported_names[bound_name] = (node.module, alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            record_binding(node.id)
        elif isinstance(node, ast.arg):
            record_binding(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            record_binding(node.name)
        elif isinstance(node, ast.ExceptHandler):
            record_binding(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            record_binding(node.name)
        elif isinstance(node, ast.MatchMapping):
            record_binding(node.rest)
        elif type(node).__name__ in {"ParamSpec", "TypeVar", "TypeVarTuple"}:
            record_binding(node.name)
    assert imports <= set(allowed_module_symbols)
    assert future_annotation_imports == 1
    assert {
        "vibecad.workflow.errors",
        "vibecad.workflow.lease",
    } <= imports
    for fixed_name in set(module_names) | imported_bound_names | local_callables:
        assert binding_counts[fixed_name] == 1
    assert not set(binding_counts) & protected_builtin_names

    def is_approved_sha256_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Name):
            return imported_names.get(node.func.id) == ("hashlib", "sha256")
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            return module_names.get(node.func.value.id) == "hashlib" and node.func.attr == "sha256"
        return False

    def approved_module_call(function: ast.AST) -> tuple[str, str] | None:
        if isinstance(function, ast.Name):
            return imported_names.get(function.id)
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            module_name = module_names.get(function.value.id)
            if module_name is not None:
                return module_name, function.attr
        return None

    def is_approved_handler_type(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            if node.id in {"OSError", "UnicodeDecodeError"}:
                return True
            return imported_names.get(node.id) == ("json", "JSONDecodeError")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return module_names.get(node.value.id) == "json" and node.attr == "JSONDecodeError"
        return False

    hash_state_names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and is_approved_sha256_call(node.value)
        ):
            hash_state_names.add(node.targets[0].id)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and is_approved_sha256_call(node.value)
        ):
            hash_state_names.add(node.target.id)
    for hash_state_name in hash_state_names:
        assert binding_counts[hash_state_name] == 1
    forbidden_names = {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "__builtins__",
        "getattr",
        "globals",
        "locals",
        "setattr",
        "delattr",
        "callable",
        "vars",
        "hasattr",
        "input",
        "breakpoint",
        "BaseException",
        "Exception",
        "len",
        "tuple",
    }
    forbidden_attributes = {
        "__bases__",
        "__builtins__",
        "__class__",
        "__code__",
        "__dict__",
        "__getattr__",
        "__getattribute__",
        "__globals__",
        "__mro__",
        "__subclasses__",
        "__traceback__",
        "_getframe",
        "_accessor",
        "_acquire_validated",
        "_adapter",
        "_creator_pid",
        "_ensure_process",
        "_fd",
        "_flavour",
        "_raw_open",
        "_registry_key",
        "_root_identity",
        "_root_parts",
        "_scandir",
        "acquire",
        "acquire_project_write",
        "chmod",
        "cr_frame",
        "cwd",
        "exists",
        "expanduser",
        "f_back",
        "f_builtins",
        "f_globals",
        "f_locals",
        "glob",
        "gi_frame",
        "group",
        "hardlink_to",
        "home",
        "is_block_device",
        "is_char_device",
        "is_dir",
        "is_fifo",
        "is_file",
        "is_junction",
        "is_mount",
        "is_socket",
        "is_symlink",
        "iterdir",
        "lchmod",
        "link_to",
        "listdir",
        "lstat",
        "mkdir",
        "open",
        "owner",
        "parser",
        "read_bytes",
        "readlink",
        "read_text",
        "rename",
        "replace",
        "release",
        "resolve",
        "rglob",
        "rmdir",
        "samefile",
        "scandir",
        "stat",
        "symlink_to",
        "tb_frame",
        "touch",
        "unlink",
        "walk",
        "write_bytes",
        "write_text",
        "fileno",
        "flush",
        "read",
        "write",
        "system",
        "popen",
    }
    assert not imported_bound_names & forbidden_names
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def is_executable_function_body_call(node: ast.Call) -> bool:
        child: ast.AST = node
        while child in parents:
            parent = parents[child]
            if isinstance(parent, ast.ClassDef):
                return False
            if isinstance(parent, ast.FunctionDef):
                return child in parent.body
            if isinstance(parent, ast.AsyncFunctionDef):
                return False
            child = parent
        return False

    def is_static_literal(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.Tuple):
            return all(is_static_literal(item) for item in node.elts)
        return False

    def is_exact_loop_type_guard(statement: ast.AST, name: str) -> bool:
        if not isinstance(statement, ast.If) or statement.orelse:
            return False
        if not statement.body or not isinstance(statement.body[-1], (ast.Raise, ast.Return)):
            return False
        comparison = statement.test
        if not isinstance(comparison, ast.Compare):
            return False
        if len(comparison.ops) != 1 or not isinstance(comparison.ops[0], ast.IsNot):
            return False
        if len(comparison.comparators) != 1:
            return False
        left = comparison.left
        if not (
            isinstance(left, ast.Call)
            and isinstance(left.func, ast.Name)
            and left.func.id == "type"
            and len(left.args) == 1
            and not left.keywords
            and isinstance(left.args[0], ast.Name)
            and left.args[0].id == name
        ):
            return False
        expected = comparison.comparators[0]
        if isinstance(expected, ast.Name):
            return expected.id in {"bytes", "dict", "list", "str"}
        return (
            isinstance(expected, ast.Call)
            and isinstance(expected.func, ast.Name)
            and expected.func.id == "type"
            and len(expected.args) == 1
            and not expected.keywords
            and isinstance(expected.args[0], ast.Tuple)
            and expected.args[0].elts == []
        )

    def function_binding_count(function: ast.FunctionDef, name: str) -> int:
        count = 0
        for item in ast.walk(function):
            if isinstance(item, ast.arg) and item.arg == name:
                count += 1
            elif isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
                if item.id == name:
                    count += 1
            elif isinstance(item, ast.ExceptHandler) and item.name == name:
                count += 1
        return count

    def loop_has_exact_guard(loop: ast.For) -> bool:
        iterator = loop.iter
        if not isinstance(iterator, ast.Name):
            return False
        child: ast.AST = loop
        function = None
        while child in parents:
            parent = parents[child]
            if isinstance(parent, ast.FunctionDef):
                function = parent
                break
            child = parent
        if function is None or function_binding_count(function, iterator.id) != 1:
            return False
        top_level = child
        loop_index = function.body.index(top_level)
        for statement in function.body[:loop_index]:
            if is_exact_loop_type_guard(statement, iterator.id):
                return True
        return False

    public_value_classes = {
        "CommitJournal",
        "ProjectHead",
        "ReconciliationResult",
        "RevisionArtifactRef",
        "RevisionRef",
    }
    expected_class_methods = {
        "CommitJournal": {"__post_init__", "from_mapping", "to_mapping"},
        "CommitJournalState": set(),
        "LocalRevisionStore": {"__init__", *EXPECTED_STORE_METHODS},
        "ProjectHead": {"__post_init__", "from_mapping", "to_mapping"},
        "ReconciliationResult": {"__post_init__", "from_mapping", "to_mapping"},
        "ReconciliationStatus": set(),
        "RevisionArtifactRef": {"__post_init__", "from_mapping", "to_mapping"},
        "RevisionRef": {"__post_init__", "from_mapping", "to_mapping"},
        "RevisionStoreError": {"__init__"},
        "RevisionStoreErrorCode": set(),
        "RevisionStoreRootTrust": set(),
    }
    source_classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    assert set(source_classes) == set(expected_class_methods)
    for class_name, class_node in source_classes.items():
        methods = {
            statement.name
            for statement in class_node.body
            if isinstance(statement, ast.FunctionDef)
        }
        assert methods == expected_class_methods[class_name]
        declared_fields = tuple(
            statement.target.id
            for statement in class_node.body
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
        )
        if class_name in EXPECTED_VALUE_FIELDS:
            assert declared_fields == EXPECTED_VALUE_FIELDS[class_name]
            for statement in class_node.body:
                if isinstance(statement, ast.AnnAssign):
                    if statement.target.id == "schema_version":
                        assert isinstance(statement.value, ast.Constant)
                        assert type(statement.value.value) is int
                        assert statement.value.value == SCHEMA_VERSION
                    else:
                        assert statement.value is None
        else:
            assert declared_fields == ()
        class_assignments = {
            statement.targets[0].id: statement.value.value
            for statement in class_node.body
            if isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Constant)
        }
        if class_name in EXPECTED_ENUM_MEMBERS:
            assert class_assignments == EXPECTED_ENUM_MEMBERS[class_name]
            assert all(
                isinstance(statement, (ast.Assign, ast.Expr, ast.Pass))
                for statement in class_node.body
            )
        else:
            for statement in class_node.body:
                if isinstance(statement, ast.Assign):
                    assert statement.targets[0].id == "__slots__"

    for statement in tree.body:
        assert isinstance(
            statement,
            (
                ast.AnnAssign,
                ast.Assign,
                ast.ClassDef,
                ast.Expr,
                ast.FunctionDef,
                ast.Import,
                ast.ImportFrom,
            ),
        )
        if isinstance(statement, ast.Assign):
            assert len(statement.targets) == 1
            assert isinstance(statement.targets[0], ast.Name)
            assert is_static_literal(statement.value)
        elif isinstance(statement, ast.AnnAssign):
            assert isinstance(statement.target, ast.Name)
            assert statement.value is None or is_static_literal(statement.value)
        elif isinstance(statement, ast.Expr):
            assert isinstance(statement.value, ast.Constant)
            assert type(statement.value.value) is str

    for class_node in source_classes.values():
        for statement in class_node.body:
            assert isinstance(
                statement,
                (ast.AnnAssign, ast.Assign, ast.Expr, ast.FunctionDef, ast.Pass),
            )
            if isinstance(statement, ast.Assign):
                assert len(statement.targets) == 1
                assert isinstance(statement.targets[0], ast.Name)
                assert is_static_literal(statement.value)
            elif isinstance(statement, ast.AnnAssign):
                assert isinstance(statement.target, ast.Name)
                assert statement.value is None or is_static_literal(statement.value)
            elif isinstance(statement, ast.Expr):
                assert isinstance(statement.value, ast.Constant)
                assert type(statement.value.value) is str
    for node in ast.walk(tree):
        assert not isinstance(
            node,
            (
                ast.AsyncFor,
                ast.AsyncFunctionDef,
                ast.AsyncWith,
                ast.Await,
                ast.Delete,
                ast.DictComp,
                ast.FormattedValue,
                ast.GeneratorExp,
                ast.JoinedStr,
                ast.Lambda,
                ast.ListComp,
                ast.Match,
                ast.SetComp,
                ast.Starred,
                ast.Yield,
                ast.YieldFrom,
            ),
        )
        if isinstance(node, ast.arguments):
            assert node.vararg is None
            assert node.kwarg is None
            for default in node.defaults:
                assert is_static_literal(default)
            for default in node.kw_defaults:
                assert default is None or is_static_literal(default)
        if isinstance(node, (ast.Tuple, ast.List)):
            assert not isinstance(node.ctx, (ast.Store, ast.Del))
        if isinstance(node, ast.For):
            assert isinstance(node.target, ast.Name)
            assert loop_has_exact_guard(node)
        if isinstance(node, ast.Dict):
            assert all(key is not None for key in node.keys)
        if isinstance(node, ast.Call):
            assert all(keyword.arg is not None for keyword in node.keywords)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            parent = parents[node]
            if isinstance(node, ast.ClassDef):
                assert isinstance(parent, ast.Module)
            else:
                assert isinstance(parent, (ast.Module, ast.ClassDef))
                if node.name.startswith("__") and node.name.endswith("__"):
                    assert isinstance(parent, ast.ClassDef)
                    assert node.name in expected_class_methods[parent.name]
            assert len(node.decorator_list) <= 1
            for decorator in node.decorator_list:
                if isinstance(node, ast.ClassDef):
                    assert node.name in public_value_classes
                    assert isinstance(decorator, ast.Call)
                    if isinstance(decorator.func, ast.Name):
                        assert imported_names.get(decorator.func.id) == (
                            "dataclasses",
                            "dataclass",
                        )
                    elif isinstance(decorator.func, ast.Attribute):
                        assert isinstance(decorator.func.value, ast.Name)
                        assert module_names.get(decorator.func.value.id) == "dataclasses"
                        assert decorator.func.attr == "dataclass"
                    else:
                        raise AssertionError("unsupported decorator target")
                    assert decorator.args == []
                    options = {
                        keyword.arg: keyword.value.value
                        for keyword in decorator.keywords
                        if keyword.arg is not None and isinstance(keyword.value, ast.Constant)
                    }
                    assert options == {"frozen": True, "kw_only": True, "slots": True}
                    assert len(decorator.keywords) == len(options)
                    assert all(keyword.value.value is True for keyword in decorator.keywords)
                else:
                    parent = parents[node]
                    assert isinstance(parent, ast.ClassDef)
                    assert parent.name in public_value_classes
                    assert node.name == "from_mapping"
                    assert isinstance(decorator, ast.Name)
                    assert decorator.id == "staticmethod"
        if isinstance(node, ast.ClassDef):
            assert node.keywords == []
            for base in node.bases:
                if isinstance(base, ast.Name):
                    assert base.id == "ValueError" or imported_names.get(base.id) == (
                        "enum",
                        "StrEnum",
                    )
                elif isinstance(base, ast.Attribute):
                    assert isinstance(base.value, ast.Name)
                    assert module_names.get(base.value.id) == "enum"
                    assert base.attr == "StrEnum"
                else:
                    raise AssertionError("unsupported class base")
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names
            if node.id in module_names:
                parent = parents[node]
                assert isinstance(parent, ast.Attribute) and parent.value is node
        if isinstance(node, ast.Attribute):
            assert not (node.attr.startswith("__") and node.attr.endswith("__"))
            if isinstance(node.value, ast.Name) and node.value.id in module_names:
                module_name = module_names[node.value.id]
                assert node.attr in allowed_module_symbols[module_name]
                assert isinstance(node.ctx, ast.Load)
            else:
                assert node.attr not in forbidden_attributes
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in imported_names:
                module_name, symbol_name = imported_names[node.func.id]
                assert symbol_name in allowed_module_calls[module_name]
            else:
                assert node.func.id in allowed_builtin_calls | local_callables
                if node.func.id in allowed_builtin_calls:
                    assert node.keywords == []
                    if node.func.id in {"TypeError", "ValueError"}:
                        assert len(node.args) == 1
                        assert isinstance(node.args[0], ast.Constant)
                        assert type(node.args[0].value) is str
                    elif node.func.id in {"bytes", "str"}:
                        assert len(node.args) == 2
                        assert isinstance(node.args[1], ast.Constant)
                        assert node.args[1].value == "utf-8"
                    elif node.func.id == "type":
                        assert len(node.args) == 1
        if isinstance(node, ast.Call):
            assert isinstance(node.func, (ast.Name, ast.Attribute))
            module_call = approved_module_call(node.func)
            if module_call == ("dataclasses", "dataclass"):
                parent = parents[node]
                assert isinstance(parent, ast.ClassDef)
                assert node in parent.decorator_list
            else:
                assert is_executable_function_body_call(node)
            if module_call == ("hashlib", "sha256"):
                assert len(node.args) <= 1
                assert node.keywords == []
            elif module_call == ("json", "dumps"):
                assert len(node.args) == 1
                assert all(keyword.arg is not None for keyword in node.keywords)
                json_options = {keyword.arg: keyword.value for keyword in node.keywords}
                assert len(node.keywords) == 4
                assert set(json_options) == {
                    "allow_nan",
                    "ensure_ascii",
                    "separators",
                    "sort_keys",
                }
                assert isinstance(json_options["allow_nan"], ast.Constant)
                assert json_options["allow_nan"].value is False
                assert isinstance(json_options["ensure_ascii"], ast.Constant)
                assert json_options["ensure_ascii"].value is False
                assert isinstance(json_options["sort_keys"], ast.Constant)
                assert json_options["sort_keys"].value is True
                separators = json_options["separators"]
                assert isinstance(separators, ast.Tuple)
                assert all(isinstance(item, ast.Constant) for item in separators.elts)
                assert [item.value for item in separators.elts] == [",", ":"]
            elif module_call == ("json", "loads"):
                assert len(node.args) == 1
                assert len(node.keywords) == 1
                hook = node.keywords[0]
                assert hook.arg == "object_pairs_hook"
                assert isinstance(hook.value, ast.Name)
                assert hook.value.id == "_json_object_pairs"
                assert hook.value.id in local_callables
            elif module_call == ("pathlib", "Path"):
                assert len(node.args) == 1
                assert node.keywords == []
            elif module_call == ("re", "fullmatch"):
                assert len(node.args) == 2
                assert node.keywords == []
            elif module_call == ("secrets", "token_hex"):
                assert len(node.args) == 1
                assert node.keywords == []
                assert isinstance(node.args[0], ast.Constant)
                assert type(node.args[0].value) is int
                assert node.args[0].value == 16
            elif module_call is not None and module_call[0] == "stat":
                assert len(node.args) == 1
                assert node.keywords == []
            elif module_call in {("os", "geteuid"), ("os", "getpid")}:
                assert node.args == []
                assert node.keywords == []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name) and node.func.value.id in module_names:
                module_name = module_names[node.func.value.id]
                assert node.func.attr in allowed_module_calls[module_name]
            else:
                assert node.func.attr in allowed_non_module_attribute_calls
                if node.func.attr == "update":
                    assert isinstance(node.func.value, ast.Name)
                    assert node.func.value.id in hash_state_names
                elif node.func.attr == "hexdigest":
                    assert (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id in hash_state_names
                    ) or is_approved_sha256_call(node.func.value)
        assert not isinstance(node, ast.With)
        if isinstance(node, ast.ExceptHandler):
            assert node.type is not None
            if isinstance(node.type, ast.Tuple):
                assert node.type.elts
                assert all(is_approved_handler_type(item) for item in node.type.elts)
            else:
                assert is_approved_handler_type(node.type)


def test_source_contains_no_freecad_mcp_model_network_or_ambient_path_surface():
    source = Path(inspect.getsourcefile(revisions_module) or "").read_text(encoding="utf-8")
    lowered = source.lower()
    for token in (
        "freecad",
        "mcp",
        "anthropic",
        "openai",
        "requests",
        "urllib",
        "socket",
        "subprocess",
        "tempfile",
        "shutil",
        "vibecad.tools",
        "vibecad.server",
        "vibecad.engine",
        "vibecad.validation",
        "taskrun",
        "mark_saved",
    ):
        assert token not in lowered

    tree = ast.parse(source)
    forbidden_private_session_attributes = {
        "_revision_id",
        "_saved_revision_id",
        "_replace_document",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_private_session_attributes
