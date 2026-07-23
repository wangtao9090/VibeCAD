"""Contract tests for isolated, exact-once CAD candidate sessions."""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import inspect
import pickle
import sys
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path

import pytest

import vibecad.execution as execution_package
import vibecad.execution.candidate as candidate_module
import vibecad.execution.revisions as revisions_module
from vibecad.execution.candidate import (
    ActiveCandidate,
    CadSnapshotPort,
    CandidateCommitResult,
    CandidateCommitStatus,
    CandidateCoordinator,
    CandidateError,
    CandidateErrorCode,
    CandidateReconcileResult,
    CandidateReconcileStatus,
    CandidateRollbackResult,
    CandidateRollbackStatus,
    CheckpointedCandidate,
    SealedCandidate,
    SessionBinding,
    SessionSlot,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    ProjectHead,
    ReconciliationStatus,
    RevisionRef,
    RevisionStoreError,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
)
from vibecad.validation import (
    CompiledAcceptance,
    ObservationSnapshot,
    ShapeObservation,
    ValidationError,
    VerificationReceipt,
    compile_acceptance_spec,
    consume_verification_receipt,
    verify_acceptance,
)
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
)
from vibecad.workflow.lease import (
    LeaseRootTrust,
    ProjectWriteLease,
    ResourceLeaseManager,
)

PROJECT_ID = "project_0123456789abcdef0123456789abcdef"
OTHER_PROJECT_ID = "project_11111111111111111111111111111111"
OTHER_REVISION = "revision_11111111111111111111111111111111"
TASK_ID = "task_0123456789abcdef0123456789abcdef"
OTHER_TASK_ID = "task_11111111111111111111111111111111"
MANIFEST = "a" * 64

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
EXPECTED_CANDIDATE_EXPORTS = (
    "CandidateErrorCode",
    "CandidateError",
    "CandidateCommitStatus",
    "CandidateRollbackStatus",
    "CandidateReconcileStatus",
    "CadSnapshotPort",
    "SessionBinding",
    "SessionSlot",
    "ActiveCandidate",
    "CheckpointedCandidate",
    "SealedCandidate",
    "CandidateCommitResult",
    "CandidateRollbackResult",
    "CandidateReconcileResult",
    "CandidateCoordinator",
)
EXPECTED_ERROR_CODES = {
    "INVALID_INPUT": "invalid_input",
    "INVALID_IDENTIFIER": "invalid_identifier",
    "INVALID_CANDIDATE": "invalid_candidate",
    "INVALID_TRANSITION": "invalid_transition",
    "INVALID_BINDING": "invalid_binding",
    "INVALID_LEASE": "invalid_lease",
    "SESSION_ALIAS": "session_alias",
    "TERMINAL_IN_PROGRESS": "terminal_in_progress",
    "ALREADY_TERMINAL": "already_terminal",
    "RECEIPT_REJECTED": "receipt_rejected",
    "CAD_FAILURE": "cad_failure",
    "RESOURCE_EXHAUSTED": "resource_exhausted",
    "STORE_FAILURE": "store_failure",
    "CONFLICT": "conflict",
    "CLEANUP_REQUIRED": "cleanup_required",
    "RECOVERY_REQUIRED": "recovery_required",
}
EXPECTED_VALUE_FIELDS = {
    "SessionBinding": ("project_id", "revision_id", "session"),
    "ActiveCandidate": ("project_id", "base_head", "binding", "model_path", "step_path"),
    "CheckpointedCandidate": (
        "project_id",
        "base_head",
        "binding",
        "model_path",
        "step_path",
    ),
    "SealedCandidate": ("project_id", "base_head", "revision", "binding"),
    "CandidateCommitResult": (
        "schema_version",
        "status",
        "head",
        "revision",
        "live_binding",
        "report",
        "head_committed",
        "slot_promoted",
        "cleanup_required",
        "recovery_required",
        "cleanup_binding",
    ),
    "CandidateRollbackResult": (
        "schema_version",
        "status",
        "head",
        "live_binding",
        "reconciliation",
        "head_committed",
        "slot_promoted",
        "cleanup_required",
        "recovery_required",
        "cleanup_binding",
    ),
    "CandidateReconcileResult": (
        "schema_version",
        "status",
        "head",
        "live_binding",
        "reconciliation",
        "head_committed",
        "slot_promoted",
        "cleanup_required",
        "recovery_required",
        "cleanup_binding",
    ),
}


class ExplosiveValue:
    def _explode(self, protocol: str):
        raise AssertionError(f"untrusted protocol executed: {protocol}")

    def __eq__(self, _other):
        return self._explode("__eq__")

    def __bool__(self):
        return self._explode("__bool__")

    def __iter__(self):
        return self._explode("__iter__")

    def __str__(self):
        return self._explode("__str__")

    def __fspath__(self):
        return self._explode("__fspath__")


@dataclasses.dataclass(eq=False, slots=True)
class FakeSession:
    name: str
    payload: bytes
    closed: bool = False

    def __eq__(self, _other):
        raise AssertionError("Session equality must never execute")

    def __bool__(self):
        raise AssertionError("Session truth testing must never execute")

    def __iter__(self):
        raise AssertionError("Session iteration must never execute")

    def __str__(self):
        raise AssertionError("Session string coercion must never execute")

    def __fspath__(self):
        raise AssertionError("Session path coercion must never execute")

    def __hash__(self):
        raise AssertionError("Session hashing must never execute")

    def __getattr__(self, name):
        raise AssertionError(f"Unknown Session attribute accessed: {name}")


class FakeCadSnapshotPort(CadSnapshotPort):
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object | None]] = []
        self.created: list[FakeSession] = []
        self.fail_create = False
        self.fail_load_number: int | None = None
        self.fail_checkpoint = False
        self.close_failures: set[int] = set()
        self.close_hooks: dict[int, object] = {}
        self.create_alias: object | None = None
        self.load_aliases: dict[int, object] = {}
        self._load_count = 0

    def create_empty(self, *, revision_id: str) -> object:
        self.calls.append(("create_empty", revision_id, None))
        if self.fail_create:
            raise RuntimeError("create failed")
        if self.create_alias is not None:
            return self.create_alias
        session = FakeSession(f"empty:{revision_id}", b"empty-fcstd")
        self.created.append(session)
        return session

    def load_fcstd(self, path: Path) -> object:
        self._load_count += 1
        self.calls.append(("load_fcstd", path, None))
        if self.fail_load_number == self._load_count:
            raise RuntimeError("load failed")
        if self._load_count in self.load_aliases:
            return self.load_aliases[self._load_count]
        session = FakeSession(f"load:{self._load_count}", path.read_bytes())
        self.created.append(session)
        return session

    def checkpoint_fcstd(self, session: object, path: Path) -> None:
        self.calls.append(("checkpoint_fcstd", session, path))
        if self.fail_checkpoint:
            raise RuntimeError("checkpoint failed")
        assert type(session) is FakeSession
        path.write_bytes(session.payload)

    def close(self, session: object) -> None:
        self.calls.append(("close", session, None))
        assert type(session) is FakeSession
        hook = self.close_hooks.get(id(session))
        if hook is not None:
            assert callable(hook)
            hook()
        session.closed = True
        if id(session) in self.close_failures:
            raise RuntimeError("close failed after side effect")

    def close_count(self, session: object) -> int:
        return sum(
            1 for name, target, _extra in self.calls if name == "close" and target is session
        )


@dataclasses.dataclass(slots=True)
class Rig:
    manager: ResourceLeaseManager
    store: LocalRevisionStore
    lease: object
    head: ProjectHead
    baseline: FakeSession
    baseline_binding: SessionBinding
    slot: SessionSlot
    port: FakeCadSnapshotPort
    coordinator: CandidateCoordinator


def _track_store_mutations(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {
        "begin_revision": 0,
        "seal_revision": 0,
        "commit_revision": 0,
        "rollback_revision": 0,
        "reconcile": 0,
        "slot_compare_and_set": 0,
    }
    for name in (
        "begin_revision",
        "seal_revision",
        "commit_revision",
        "rollback_revision",
        "reconcile",
    ):
        original = getattr(LocalRevisionStore, name)

        def counted(self, *args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(LocalRevisionStore, name, counted)
    original_cas = SessionSlot.compare_and_set

    def counted_cas(self, expected, replacement):
        calls["slot_compare_and_set"] += 1
        return original_cas(self, expected, replacement)

    monkeypatch.setattr(SessionSlot, "compare_and_set", counted_cas)
    return calls


def _reset_store_mutations(calls: dict[str, int]) -> None:
    for name in calls:
        calls[name] = 0


def _secure_root(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


@contextmanager
def _rig(tmp_path: Path, *, imported: bool = False, suffix: str = "main"):
    revisions_module._initialize_candidate_file_limit_runtime()
    root = tmp_path / suffix
    locks_root = _secure_root(root / "locks")
    revisions_root = _secure_root(root / "revisions")
    manager = ResourceLeaseManager(locks_root, trust=LeaseRootTrust.TRUSTED_LOCAL)
    store = LocalRevisionStore(
        revisions_root,
        manager,
        trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
    )
    lease = manager.acquire_project_write(PROJECT_ID)
    if imported:
        source = root / "base.FCStd"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"imported-base-fcstd")
        source_bytes = source.read_bytes()
        head = store.import_trusted_fcstd(
            PROJECT_ID,
            source,
            hashlib.sha256(source_bytes).hexdigest(),
            len(source_bytes),
            lease,
        )
        payload = b"imported-base-fcstd"
    else:
        head = store.initialize_empty_project(PROJECT_ID, lease)
        payload = b"baseline-empty"
    baseline = FakeSession("baseline", payload)
    baseline_binding = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=head.revision_id,
        session=baseline,
    )
    slot = SessionSlot(baseline_binding)
    port = FakeCadSnapshotPort()
    coordinator = CandidateCoordinator(
        store=store,
        snapshot_port=port,
        session_slot=slot,
    )
    rig = Rig(
        manager,
        store,
        lease,
        head,
        baseline,
        baseline_binding,
        slot,
        port,
        coordinator,
    )
    try:
        yield rig
    finally:
        if not lease.released:
            lease.release(owner_token=lease.owner_token)


def _begin(rig: Rig) -> ActiveCandidate:
    return rig.coordinator.begin(
        project_id=PROJECT_ID,
        expected_head=rig.head,
        lease=rig.lease,
    )


def _commit_rig_payload(
    rig: Rig,
    head: ProjectHead,
    *,
    model: bytes,
    step: bytes,
) -> tuple[RevisionRef, ProjectHead]:
    revision_id = rig.store.begin_revision(PROJECT_ID, head, rig.lease)
    rig.store.candidate_model_path(PROJECT_ID, revision_id, rig.lease).write_bytes(model)
    rig.store.candidate_artifact_path(
        PROJECT_ID,
        revision_id,
        "step",
        rig.lease,
    ).write_bytes(step)
    revision = rig.store.seal_revision(PROJECT_ID, revision_id, rig.lease)
    committed = rig.store.commit_revision(PROJECT_ID, head, revision_id, rig.lease)
    return (revision, committed)


def _install_seeded_history(rig: Rig) -> RevisionRef:
    source, source_head = _commit_rig_payload(
        rig,
        rig.head,
        model=b"historical-seeded-model",
        step=b"historical-seeded-step",
    )
    _current, current_head = _commit_rig_payload(
        rig,
        source_head,
        model=b"current-model",
        step=b"current-step",
    )
    current_session = FakeSession("current-baseline", b"current-model")
    current_binding = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=current_head.revision_id,
        session=current_session,
    )
    slot = SessionSlot(current_binding)
    port = FakeCadSnapshotPort()
    rig.head = current_head
    rig.baseline = current_session
    rig.baseline_binding = current_binding
    rig.slot = slot
    rig.port = port
    rig.coordinator = CandidateCoordinator(
        store=rig.store,
        snapshot_port=port,
        session_slot=slot,
    )
    return source


def test_pre_cas_reservation_is_replay_safe_and_enters_cad_only_when_activated(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="reservation-replay") as rig:
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        assert rig.port.calls == []
        assert (
            rig.coordinator.reserve_candidate(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                reservation_key=TASK_ID,
                lease=rig.lease,
            )
            == revision_id
        )
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.reserve_candidate(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                reservation_key=OTHER_TASK_ID,
                lease=rig.lease,
            )
        _assert_candidate_error(captured, CandidateErrorCode.CONFLICT)
        active = rig.coordinator.begin_reserved(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        assert type(active) is ActiveCandidate
        assert active.binding.revision_id == revision_id
        assert [call[0] for call in rig.port.calls] == ["create_empty"]


def test_pre_cas_reservation_capacity_error_is_exact_and_has_zero_cad_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="reservation-capacity") as rig:
        current = sum(
            path.stat(follow_symlinks=False).st_size
            for path in rig.store._root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        monkeypatch.setattr(
            revisions_module,
            "_MAX_STORE_BYTES",
            current + 2_151_677_952 - 1,
            raising=False,
        )
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.reserve_candidate(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                reservation_key=TASK_ID,
                lease=rig.lease,
            )
        _assert_candidate_error(captured, CandidateErrorCode.RESOURCE_EXHAUSTED)
        assert rig.port.calls == []


def test_cancel_pre_cas_reservation_returns_deterministic_cleanup_evidence(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="reservation-cancel") as rig:
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        result = rig.coordinator.cancel_reservation(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        assert type(result) is CandidateRollbackResult
        assert result.status is CandidateRollbackStatus.NOT_COMMITTED
        assert result.cleanup_required is False
        assert result.recovery_required is False
        assert result.head == rig.head
        assert result.reconciliation is not None
        assert result.reconciliation.status is ReconciliationStatus.NOT_COMMITTED
        assert rig.port.calls == []


def test_reserved_candidate_activation_does_not_repeat_capacity_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="reservation-post-cas-capacity") as rig:
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        with monkeypatch.context() as capacity:
            for name in (
                "_MAX_STORE_BYTES",
                "_MAX_PROJECTS",
                "_MAX_REVISIONS",
                "_MAX_CANDIDATES_AND_RESERVATIONS",
                "_MAX_ORDINARY_FILES",
            ):
                capacity.setattr(revisions_module, name, 0, raising=False)
            active = rig.coordinator.begin_reserved(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                revision_id=revision_id,
                reservation_key=TASK_ID,
                lease=rig.lease,
            )
            assert active.binding.revision_id == revision_id
            assert [call[0] for call in rig.port.calls] == ["create_empty"]
        rolled_back = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED
        assert [call[0] for call in rig.port.calls] == ["create_empty", "close"]
        assert tuple(rig.store._root.rglob("reservation.json")) == ()


def test_staging_journal_replay_advances_a_crashed_reserved_record_to_staged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="reservation-phase-replay") as rig:
        original_phase = revisions_module._set_reservation_phase
        failed = False

        def fail_first_staged_phase(*args, **kwargs):
            nonlocal failed
            if not failed and args[6] == "staged":
                failed = True
                return (None, RevisionStoreErrorCode.RECOVERY_REQUIRED)
            return original_phase(*args, **kwargs)

        with monkeypatch.context() as fault:
            fault.setattr(revisions_module, "_set_reservation_phase", fail_first_staged_phase)
            with pytest.raises(CandidateError) as first:
                rig.coordinator.reserve_candidate(
                    project_id=PROJECT_ID,
                    expected_head=rig.head,
                    reservation_key=TASK_ID,
                    lease=rig.lease,
                )
        _assert_candidate_error(first, CandidateErrorCode.RECOVERY_REQUIRED)
        journal = rig.store._root.rglob("journal.json")
        assert len(tuple(journal)) == 1
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        active = rig.coordinator.begin_reserved(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        assert active.binding.revision_id == revision_id
        rolled_back = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED


def test_new_session_is_closed_when_file_limit_restore_fails_after_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="file-limit-create-session-cleanup") as rig:
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        runtime = revisions_module._CandidateFileLimitRuntime
        monkeypatch.setattr(runtime, "_initialized_pid", revisions_module.os.getpid())
        monkeypatch.setattr(runtime, "_poisoned_pid", None)
        monkeypatch.setattr(
            revisions_module.signal,
            "getsignal",
            lambda _signal_number: revisions_module.signal.SIG_IGN,
        )
        monkeypatch.setattr(
            revisions_module.resource,
            "getrlimit",
            lambda _resource_number: (
                revisions_module.resource.RLIM_INFINITY,
                revisions_module.resource.RLIM_INFINITY,
            ),
        )
        calls = 0

        def fail_restore(_resource_number, _value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected restore failure")

        monkeypatch.setattr(revisions_module.resource, "setrlimit", fail_restore)
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.begin_reserved(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                revision_id=revision_id,
                reservation_key=TASK_ID,
                lease=rig.lease,
            )
        error = _assert_candidate_error(captured, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.recovery_required is True
        created = rig.port.created[-1]
        assert created.closed is True
        assert rig.port.close_count(created) == 1


def test_reservation_is_charged_across_store_restart_replays_and_releases_exactly(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="reservation-restart") as rig:
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        reservation_records = tuple(rig.store._root.rglob("reservation.json"))
        assert len(reservation_records) == 1
        fresh_store = LocalRevisionStore(
            rig.store._root,
            rig.manager,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        )
        fresh = CandidateCoordinator(
            store=fresh_store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        assert (
            fresh.reserve_candidate(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                reservation_key=TASK_ID,
                lease=rig.lease,
            )
            == revision_id
        )
        assert tuple(rig.store._root.rglob("reservation.json")) == reservation_records
        cancelled = fresh.cancel_reservation(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        assert cancelled.status is CandidateRollbackStatus.NOT_COMMITTED
        assert tuple(rig.store._root.rglob("reservation.json")) == ()
        replacement_id = fresh.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=OTHER_TASK_ID,
            lease=rig.lease,
        )
        assert replacement_id != revision_id
        fresh.cancel_reservation(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=replacement_id,
            reservation_key=OTHER_TASK_ID,
            lease=rig.lease,
        )


def test_begin_reserved_load_failure_cleans_reservation_before_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="reservation-prelineage-load") as rig:
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        original = LocalRevisionStore.load_revision

        def fail_baseline(store, project_id, loaded_revision_id):
            if store is rig.store and loaded_revision_id == rig.head.revision_id:
                raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
            return original(store, project_id, loaded_revision_id)

        with monkeypatch.context() as fault:
            fault.setattr(LocalRevisionStore, "load_revision", fail_baseline)
            with pytest.raises(CandidateError) as captured:
                rig.coordinator.begin_reserved(
                    project_id=PROJECT_ID,
                    expected_head=rig.head,
                    revision_id=revision_id,
                    reservation_key=TASK_ID,
                    lease=rig.lease,
                )
        _assert_candidate_error(captured, CandidateErrorCode.STORE_FAILURE)
        assert tuple(rig.store._root.rglob("reservation.json")) == ()
        assert not any(path.is_dir() for path in rig.store._root.rglob("candidates/*"))
        retry_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        rig.coordinator.cancel_reservation(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=retry_id,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )


def test_begin_reserved_baseline_drift_cleans_reservation_before_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="reservation-prelineage-drift") as rig:
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=TASK_ID,
            lease=rig.lease,
        )
        wrong = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=OTHER_REVISION,
            session=FakeSession("wrong-baseline", b"wrong"),
        )
        original = SessionSlot.current

        def drifted(slot):
            if slot is rig.slot:
                return wrong
            return original(slot)

        with monkeypatch.context() as drift:
            drift.setattr(SessionSlot, "current", drifted)
            with pytest.raises(CandidateError) as captured:
                rig.coordinator.begin_reserved(
                    project_id=PROJECT_ID,
                    expected_head=rig.head,
                    revision_id=revision_id,
                    reservation_key=TASK_ID,
                    lease=rig.lease,
                )
        _assert_candidate_error(captured, CandidateErrorCode.CONFLICT)
        assert tuple(rig.store._root.rglob("reservation.json")) == ()
        assert not any(path.is_dir() for path in rig.store._root.rglob("candidates/*"))
        assert rig.port.calls == []


def test_seeded_candidate_adoption_preserves_exact_payload_without_checkpoint(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="seeded-adoption") as rig:
        source = _install_seeded_history(rig)
        reservation_key = "revert:" + "d" * 64
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        active = rig.coordinator.begin_seeded_reserved(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            source_revision=source,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        assert active.model_path.read_bytes() == b"historical-seeded-model"
        assert active.step_path.read_bytes() == b"historical-seeded-step"
        exact_before = (active.model_path.read_bytes(), active.step_path.read_bytes())
        assert [call[0] for call in rig.port.calls] == ["load_fcstd"]

        checkpointed = rig.coordinator.adopt_materialized(
            candidate=active,
            source_revision=source,
            lease=rig.lease,
        )
        assert type(checkpointed) is CheckpointedCandidate
        assert checkpointed.binding.session.payload == b"historical-seeded-model"
        assert active.binding.session.closed is True
        assert (active.model_path.read_bytes(), active.step_path.read_bytes()) == exact_before
        assert [call[0] for call in rig.port.calls] == [
            "load_fcstd",
            "load_fcstd",
            "close",
        ]
        assert not any(call[0] == "checkpoint_fcstd" for call in rig.port.calls)

        sealed = rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        assert sealed.revision.base_revision == rig.head.revision_id
        assert sealed.revision.model is not None
        assert sealed.revision.model.sha256 == source.model.sha256
        assert sealed.revision.model.size_bytes == source.model.size_bytes
        assert sealed.revision.artifacts[0].sha256 == source.artifacts[0].sha256
        assert sealed.revision.artifacts[0].size_bytes == source.artifacts[0].size_bytes
        assert not any(call[0] == "checkpoint_fcstd" for call in rig.port.calls)
        rig.coordinator.rollback(candidate=sealed, lease=rig.lease)


def test_seeded_reservation_cancel_cleans_unbound_intent_and_quota_owner(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="seeded-reservation-cancel") as rig:
        reservation_key = "revert:" + "7" * 64
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        assert len(tuple(rig.store._root.rglob("seed-intent.json"))) == 1
        assert tuple(rig.store._root.rglob("seed-binding.json")) == ()

        result = rig.coordinator.cancel_reservation(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        assert result.status is CandidateRollbackStatus.NOT_COMMITTED
        assert tuple(rig.store._root.rglob("seed-intent.json")) == ()
        assert tuple(rig.store._root.rglob("seed-binding.json")) == ()
        assert tuple(rig.store._root.rglob("reservation.json")) == ()
        assert not any(path.is_dir() for path in rig.store._root.rglob("candidates/*"))


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("reload", CandidateErrorCode.CAD_FAILURE),
        ("alias", CandidateErrorCode.SESSION_ALIAS),
        ("close", CandidateErrorCode.CLEANUP_REQUIRED),
    ],
)
def test_seeded_adoption_failures_close_or_retain_with_exact_attention(
    tmp_path: Path,
    failure: str,
    expected_code: CandidateErrorCode,
) -> None:
    with _rig(tmp_path, suffix=f"seeded-adoption-{failure}") as rig:
        source = _install_seeded_history(rig)
        reservation_key = "revert:" + "e" * 64
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        active = rig.coordinator.begin_seeded_reserved(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            source_revision=source,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        if failure == "reload":
            rig.port.fail_load_number = 2
        elif failure == "alias":
            rig.port.load_aliases[2] = active.binding.session
        else:
            rig.port.close_failures.add(id(active.binding.session))

        with pytest.raises(CandidateError) as captured:
            rig.coordinator.adopt_materialized(
                candidate=active,
                source_revision=source,
                lease=rig.lease,
            )
        error = _assert_candidate_error(captured, expected_code)
        if failure == "close":
            assert error.cleanup_required is True
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert not any(call[0] == "checkpoint_fcstd" for call in rig.port.calls)
        assert tuple(rig.store._root.rglob("reservation.json")) == ()


def test_seeded_begin_and_adoption_reject_binding_and_payload_tamper(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="seeded-binding-tamper") as rig:
        source = _install_seeded_history(rig)
        reservation_key = "revert:" + "f" * 64
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.begin_seeded_reserved(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                revision_id=revision_id,
                source_revision=source,
                reservation_key="revert:" + "0" * 64,
                lease=rig.lease,
            )
        _assert_candidate_error(captured, CandidateErrorCode.CONFLICT)
        assert rig.port.calls == []

        active = rig.coordinator.begin_seeded_reserved(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            revision_id=revision_id,
            source_revision=source,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        active.model_path.write_bytes(b"tampered-after-load")
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.adopt_materialized(
                candidate=active,
                source_revision=source,
                lease=rig.lease,
            )
        _assert_candidate_error(captured, CandidateErrorCode.STORE_FAILURE)
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert tuple(rig.store._root.rglob("reservation.json")) == ()
        assert not any(call[0] == "checkpoint_fcstd" for call in rig.port.calls)


@pytest.mark.parametrize("phase", ["begin", "adopt", "seal"])
def test_seeded_source_corruption_remains_recovery_required(
    tmp_path: Path,
    phase: str,
) -> None:
    with _rig(tmp_path, suffix=f"seeded-source-corruption-{phase}") as rig:
        source = _install_seeded_history(rig)
        reservation_key = "revert:" + "4" * 64
        revision_id = rig.coordinator.reserve_candidate(
            project_id=PROJECT_ID,
            expected_head=rig.head,
            reservation_key=reservation_key,
            lease=rig.lease,
        )
        source_model = rig.store.revision_model_path(PROJECT_ID, source.id)
        active = None
        checkpointed = None
        if phase == "adopt" or phase == "seal":
            active = rig.coordinator.begin_seeded_reserved(
                project_id=PROJECT_ID,
                expected_head=rig.head,
                revision_id=revision_id,
                source_revision=source,
                reservation_key=reservation_key,
                lease=rig.lease,
            )
        if phase == "seal":
            checkpointed = rig.coordinator.adopt_materialized(
                candidate=active,
                source_revision=source,
                lease=rig.lease,
            )
        source_model.write_bytes(b"corrupt-source-after-binding")

        with pytest.raises(CandidateError) as captured:
            if phase == "begin":
                rig.coordinator.begin_seeded_reserved(
                    project_id=PROJECT_ID,
                    expected_head=rig.head,
                    revision_id=revision_id,
                    source_revision=source,
                    reservation_key=reservation_key,
                    lease=rig.lease,
                )
            elif phase == "adopt":
                assert type(active) is ActiveCandidate
                rig.coordinator.adopt_materialized(
                    candidate=active,
                    source_revision=source,
                    lease=rig.lease,
                )
            else:
                assert type(checkpointed) is CheckpointedCandidate
                rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        error = _assert_candidate_error(captured, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.recovery_required is True
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert not any(call[0] == "checkpoint_fcstd" for call in rig.port.calls)
        if phase == "begin":
            assert len(tuple(rig.store._root.rglob("reservation.json"))) == 1


def test_file_limit_restore_failure_remains_candidate_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="file-limit-restore") as rig:
        active = _begin(rig)
        runtime = revisions_module._CandidateFileLimitRuntime
        monkeypatch.setattr(runtime, "_initialized_pid", revisions_module.os.getpid())
        monkeypatch.setattr(runtime, "_poisoned_pid", None)
        monkeypatch.setattr(
            revisions_module.signal,
            "getsignal",
            lambda _signal_number: revisions_module.signal.SIG_IGN,
        )
        monkeypatch.setattr(
            revisions_module.resource,
            "getrlimit",
            lambda _resource_number: (
                revisions_module.resource.RLIM_INFINITY,
                revisions_module.resource.RLIM_INFINITY,
            ),
        )
        calls = 0

        def fail_restore(_resource_number, _value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected restore failure")

        monkeypatch.setattr(revisions_module.resource, "setrlimit", fail_restore)
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.checkpoint(candidate=active, lease=rig.lease)
        error = _assert_candidate_error(captured, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.recovery_required is True
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert tuple(rig.store._root.rglob("reservation.json")) == ()


def test_new_session_is_closed_when_file_limit_restore_fails_after_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="file-limit-load-session-cleanup") as rig:
        active = _begin(rig)
        runtime = revisions_module._CandidateFileLimitRuntime
        monkeypatch.setattr(runtime, "_initialized_pid", revisions_module.os.getpid())
        monkeypatch.setattr(runtime, "_poisoned_pid", None)
        monkeypatch.setattr(
            revisions_module.signal,
            "getsignal",
            lambda _signal_number: revisions_module.signal.SIG_IGN,
        )
        monkeypatch.setattr(
            revisions_module.resource,
            "getrlimit",
            lambda _resource_number: (
                revisions_module.resource.RLIM_INFINITY,
                revisions_module.resource.RLIM_INFINITY,
            ),
        )
        calls = 0

        def fail_load_restore(_resource_number, _value):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("injected load restore failure")

        monkeypatch.setattr(revisions_module.resource, "setrlimit", fail_load_restore)
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.checkpoint(candidate=active, lease=rig.lease)
        error = _assert_candidate_error(captured, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.recovery_required is True
        loaded = rig.port.created[-1]
        assert loaded.name.startswith("load:")
        assert loaded.closed is True
        assert rig.port.close_count(loaded) == 1


def test_writer_of_536870913_bytes_has_zero_revision_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="file-limit-exact-writer") as rig:
        active = _begin(rig)
        before_manifests = tuple(
            sorted(
                str(path.relative_to(rig.store._root))
                for path in rig.store._root.rglob("manifest.json")
            )
        )

        def write_one_past(_port, _session, path):
            with path.open("wb") as stream:
                stream.seek(536_870_912)
                stream.write(b"x")
                stream.flush()

        monkeypatch.setattr(FakeCadSnapshotPort, "checkpoint_fcstd", write_one_past)
        with pytest.raises(CandidateError) as captured:
            rig.coordinator.checkpoint(candidate=active, lease=rig.lease)
        error = _assert_candidate_error(captured, CandidateErrorCode.CAD_FAILURE)
        assert error.head_committed is False
        assert error.cleanup_required is False
        assert error.recovery_required is False
        assert rig.store.load_head(PROJECT_ID) == rig.head
        after_manifests = tuple(
            sorted(
                str(path.relative_to(rig.store._root))
                for path in rig.store._root.rglob("manifest.json")
            )
        )
        assert after_manifests == before_manifests
        assert tuple(rig.store._root.rglob("reservation.json")) == ()
        assert not any(path.is_dir() for path in rig.store._root.rglob("candidates/*"))


def _checkpoint(rig: Rig, active: ActiveCandidate) -> CheckpointedCandidate:
    return rig.coordinator.checkpoint(candidate=active, lease=rig.lease)


def _sealed(rig: Rig) -> SealedCandidate:
    active = _begin(rig)
    checkpointed = _checkpoint(rig, active)
    checkpointed.step_path.write_bytes(b"ISO-10303-21;END-ISO-10303-21;")
    return rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)


def _detached_review_revision(rig: Rig) -> RevisionRef:
    revision_id = rig.store.begin_revision(PROJECT_ID, rig.head, rig.lease)
    model_path = rig.store.candidate_model_path(PROJECT_ID, revision_id, rig.lease)
    step_path = rig.store.candidate_artifact_path(PROJECT_ID, revision_id, "step", rig.lease)
    model_path.write_bytes(b"review-draft-fcstd")
    step_path.write_bytes(b"ISO-10303-21;REVIEW-DRAFT;END-ISO-10303-21;")
    revision = rig.store.seal_revision(PROJECT_ID, revision_id, rig.lease)
    detached = rig.store.rollback_revision(PROJECT_ID, revision_id, rig.lease)
    assert detached.status is ReconciliationStatus.NOT_COMMITTED
    return revision


def _verification_for(
    revision_id: str,
    manifest_sha256: str,
    *,
    acceptance_id: str = "acceptance-main",
    compiled=None,
):
    criterion = AcceptanceCriterion(
        id="volume",
        kind=AcceptanceKind.GEOMETRY,
        check="volume",
        target="body",
        expected=100.0,
        tolerance=0.0,
        parameters={"unit": "mm^3"},
        required=True,
    )
    if compiled is None:
        compiled = compile_acceptance_spec(AcceptanceSpec(id=acceptance_id, criteria=(criterion,)))
    snapshot = ObservationSnapshot(
        candidate_revision=revision_id,
        shapes=(
            ShapeObservation(
                target="body",
                volume_mm3=100.0,
                area_mm2=50.0,
                bbox_mm=(10.0, 5.0, 2.0),
                center_of_mass_mm=(5.0, 2.5, 1.0),
                valid_shape=True,
                solid_count=1,
            ),
        ),
    )
    verification = verify_acceptance(
        compiled,
        snapshot,
        candidate_revision=revision_id,
        manifest_sha256=manifest_sha256,
    )
    assert verification.receipt is not None
    return compiled, snapshot, verification.receipt, verification.report


def _verification(sealed: SealedCandidate):
    return _verification_for(sealed.revision.id, sealed.revision.manifest_sha256)


def _commit(rig: Rig, sealed: SealedCandidate):
    compiled, snapshot, receipt, report = _verification(sealed)
    result = rig.coordinator.commit(
        candidate=sealed,
        receipt=receipt,
        compiled=compiled,
        snapshot=snapshot,
        lease=rig.lease,
    )
    return result, compiled, snapshot, receipt, report


def _candidate_receipt_consumer_name() -> str:
    matches = [
        name
        for name, value in vars(candidate_module).items()
        if name.startswith("_") and value is consume_verification_receipt
    ]
    assert len(matches) == 1
    return matches[0]


def _assert_candidate_error(
    caught: pytest.ExceptionInfo[CandidateError], code: CandidateErrorCode
) -> CandidateError:
    error = caught.value
    assert type(error) is CandidateError
    assert error.code is code
    assert type(error.schema_version) is int and error.schema_version == 1
    assert type(error.message) is str and error.message.isprintable()
    assert len(error.message.splitlines()) == 1
    assert type(error.head_committed) is bool
    assert type(error.cleanup_required) is bool
    assert type(error.recovery_required) is bool
    assert error.args == (error.message,)
    assert not hasattr(error, "to_mapping")
    return error


def test_public_surface_signatures_and_closed_enums() -> None:
    assert candidate_module.__all__ == EXPECTED_CANDIDATE_EXPORTS
    assert execution_package.__all__ == EXPECTED_EXECUTION_EXPORTS
    assert not any(hasattr(execution_package, name) for name in EXPECTED_CANDIDATE_EXPORTS)
    assert {item.name: item.value for item in CandidateErrorCode} == EXPECTED_ERROR_CODES
    assert {item.name: item.value for item in CandidateCommitStatus} == {
        "COMMITTED": "committed",
        "COMMITTED_CLEANUP_REQUIRED": "committed_cleanup_required",
        "COMMITTED_RECOVERY_REQUIRED": "committed_recovery_required",
    }
    assert {item.name: item.value for item in CandidateRollbackStatus} == {
        "NOT_COMMITTED": "not_committed",
        "CLEANUP_REQUIRED": "cleanup_required",
        "RECOVERY_REQUIRED": "recovery_required",
    }
    assert {item.name: item.value for item in CandidateReconcileStatus} == {
        "CLEAN": "clean",
        "COMMITTED": "committed",
        "NOT_COMMITTED": "not_committed",
        "CLEANUP_REQUIRED": "cleanup_required",
        "RECOVERY_REQUIRED": "recovery_required",
    }

    def public_surface_names(value: object) -> set[str]:
        return {name for name in dir(value) if name != "mro" and not name.startswith("_")}

    def public_callables(value: object) -> set[str]:
        return {name for name in public_surface_names(value) if callable(getattr(value, name))}

    assert "__getattr__" not in vars(candidate_module)
    assert "__dir__" not in vars(candidate_module)
    assert CandidateError.__slots__ == (
        "schema_version",
        "code",
        "message",
        "head_committed",
        "cleanup_required",
        "recovery_required",
    )
    error_signature = inspect.signature(CandidateError)
    assert tuple(error_signature.parameters) == (
        "code",
        "head_committed",
        "cleanup_required",
        "recovery_required",
    )
    assert error_signature.parameters["code"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert error_signature.parameters["code"].default is inspect.Signature.empty
    for name in ("head_committed", "cleanup_required", "recovery_required"):
        assert error_signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert error_signature.parameters[name].default is False
    assert inspect.get_annotations(CandidateError.__init__, eval_str=True) == {
        "code": CandidateErrorCode,
        "head_committed": bool,
        "cleanup_required": bool,
        "recovery_required": bool,
        "return": None,
    }
    assert tuple(inspect.signature(SessionSlot).parameters) == ("initial",)
    slot_initial = inspect.signature(SessionSlot).parameters["initial"]
    assert slot_initial.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert slot_initial.default is inspect.Signature.empty
    assert inspect.get_annotations(SessionSlot.__init__, eval_str=True) == {
        "initial": SessionBinding,
        "return": None,
    }
    assert tuple(inspect.signature(SessionSlot.current).parameters) == ("self",)
    slot_current_self = inspect.signature(SessionSlot.current).parameters["self"]
    assert slot_current_self.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert slot_current_self.default is inspect.Signature.empty
    assert inspect.get_annotations(SessionSlot.current, eval_str=True) == {
        "return": SessionBinding,
    }
    assert tuple(inspect.signature(SessionSlot.compare_and_set).parameters) == (
        "self",
        "expected",
        "replacement",
    )
    assert inspect.get_annotations(SessionSlot.compare_and_set, eval_str=True) == {
        "expected": SessionBinding,
        "replacement": SessionBinding,
        "return": bool,
    }
    slot_cas = inspect.signature(SessionSlot.compare_and_set)
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in slot_cas.parameters.values()
    )
    assert all(
        parameter.default is inspect.Signature.empty for parameter in slot_cas.parameters.values()
    )
    assert public_callables(SessionSlot) == {"compare_and_set", "current"}
    assert public_surface_names(SessionSlot) == {"compare_and_set", "current"}
    assert tuple(inspect.signature(CandidateCoordinator).parameters) == (
        "store",
        "snapshot_port",
        "session_slot",
    )
    for parameter in inspect.signature(CandidateCoordinator).parameters.values():
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Signature.empty
    assert inspect.get_annotations(CandidateCoordinator.__init__, eval_str=True) == {
        "store": LocalRevisionStore,
        "snapshot_port": CadSnapshotPort,
        "session_slot": SessionSlot,
        "return": None,
    }
    expected_methods = {
        "begin": ("self", "project_id", "expected_head", "lease"),
        "reserve_candidate": (
            "self",
            "project_id",
            "expected_head",
            "reservation_key",
            "lease",
        ),
        "begin_reserved": (
            "self",
            "project_id",
            "expected_head",
            "revision_id",
            "reservation_key",
            "lease",
        ),
        "begin_seeded_reserved": (
            "self",
            "project_id",
            "expected_head",
            "revision_id",
            "source_revision",
            "reservation_key",
            "lease",
        ),
        "cancel_reservation": (
            "self",
            "project_id",
            "expected_head",
            "revision_id",
            "reservation_key",
            "lease",
        ),
        "checkpoint": ("self", "candidate", "lease"),
        "adopt_materialized": ("self", "candidate", "source_revision", "lease"),
        "seal": ("self", "candidate", "lease"),
        "reopen_review": ("self", "project_id", "base_head", "revision", "lease"),
        "prepare_review": ("self", "candidate", "lease"),
        "discard_review": ("self", "candidate", "lease"),
        "publish_review": (
            "self",
            "candidate",
            "receipt",
            "compiled",
            "snapshot",
            "lease",
        ),
        "commit": ("self", "candidate", "receipt", "compiled", "snapshot", "lease"),
        "rollback": ("self", "candidate", "lease"),
        "reconcile": ("self", "project_id", "lease"),
    }
    assert public_callables(CandidateCoordinator) == set(expected_methods)
    assert public_surface_names(CandidateCoordinator) == set(expected_methods)
    coordinator_annotations = {
        "begin": {
            "project_id": str,
            "expected_head": ProjectHead,
            "lease": ProjectWriteLease,
            "return": ActiveCandidate,
        },
        "reserve_candidate": {
            "project_id": str,
            "expected_head": ProjectHead,
            "reservation_key": str,
            "lease": ProjectWriteLease,
            "return": str,
        },
        "begin_reserved": {
            "project_id": str,
            "expected_head": ProjectHead,
            "revision_id": str,
            "reservation_key": str,
            "lease": ProjectWriteLease,
            "return": ActiveCandidate,
        },
        "begin_seeded_reserved": {
            "project_id": str,
            "expected_head": ProjectHead,
            "revision_id": str,
            "source_revision": RevisionRef,
            "reservation_key": str,
            "lease": ProjectWriteLease,
            "return": ActiveCandidate,
        },
        "cancel_reservation": {
            "project_id": str,
            "expected_head": ProjectHead,
            "revision_id": str,
            "reservation_key": str,
            "lease": ProjectWriteLease,
            "return": CandidateRollbackResult,
        },
        "checkpoint": {
            "candidate": ActiveCandidate,
            "lease": ProjectWriteLease,
            "return": CheckpointedCandidate,
        },
        "adopt_materialized": {
            "candidate": ActiveCandidate,
            "source_revision": RevisionRef,
            "lease": ProjectWriteLease,
            "return": CheckpointedCandidate,
        },
        "seal": {
            "candidate": CheckpointedCandidate,
            "lease": ProjectWriteLease,
            "return": SealedCandidate,
        },
        "reopen_review": {
            "project_id": str,
            "base_head": ProjectHead,
            "revision": RevisionRef,
            "lease": ProjectWriteLease,
            "return": SealedCandidate,
        },
        "prepare_review": {
            "candidate": SealedCandidate,
            "lease": ProjectWriteLease,
            "return": SealedCandidate,
        },
        "discard_review": {
            "candidate": SealedCandidate,
            "lease": ProjectWriteLease,
            "return": None,
        },
        "publish_review": {
            "candidate": SealedCandidate,
            "receipt": VerificationReceipt,
            "compiled": CompiledAcceptance,
            "snapshot": ObservationSnapshot,
            "lease": ProjectWriteLease,
            "return": CandidateRollbackResult,
        },
        "commit": {
            "candidate": SealedCandidate,
            "receipt": VerificationReceipt,
            "compiled": CompiledAcceptance,
            "snapshot": ObservationSnapshot,
            "lease": ProjectWriteLease,
            "return": CandidateCommitResult,
        },
        "rollback": {
            "candidate": ActiveCandidate | CheckpointedCandidate | SealedCandidate,
            "lease": ProjectWriteLease,
            "return": CandidateRollbackResult,
        },
        "reconcile": {
            "project_id": str,
            "lease": ProjectWriteLease,
            "return": CandidateReconcileResult,
        },
    }
    for name, parameters in expected_methods.items():
        signature = inspect.signature(getattr(CandidateCoordinator, name))
        assert tuple(signature.parameters) == parameters
        assert signature.parameters["self"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in tuple(signature.parameters.values())[1:]
        )
        assert all(
            parameter.default is inspect.Signature.empty
            for parameter in signature.parameters.values()
        )
        assert (
            inspect.get_annotations(
                getattr(CandidateCoordinator, name),
                eval_str=True,
            )
            == coordinator_annotations[name]
        )

    expected_port = {
        "create_empty": ("self", "revision_id"),
        "load_fcstd": ("self", "path"),
        "checkpoint_fcstd": ("self", "session", "path"),
        "close": ("self", "session"),
    }
    assert public_callables(CadSnapshotPort) == set(expected_port)
    assert public_surface_names(CadSnapshotPort) == set(expected_port)
    port_annotations = {
        "create_empty": {"revision_id": str, "return": object},
        "load_fcstd": {"path": Path, "return": object},
        "checkpoint_fcstd": {
            "session": object,
            "path": Path,
            "return": None,
        },
        "close": {"session": object, "return": None},
    }
    for name, parameters in expected_port.items():
        signature = inspect.signature(getattr(CadSnapshotPort, name))
        assert tuple(signature.parameters) == parameters
        assert all(
            parameter.default is inspect.Signature.empty
            for parameter in signature.parameters.values()
        )
        assert (
            inspect.get_annotations(getattr(CadSnapshotPort, name), eval_str=True)
            == (port_annotations[name])
        )
        if name == "create_empty":
            assert signature.parameters["self"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            assert signature.parameters["revision_id"].kind is inspect.Parameter.KEYWORD_ONLY
        else:
            assert all(
                parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
                for parameter in signature.parameters.values()
            )

    for name, expected_fields in EXPECTED_VALUE_FIELDS.items():
        value_type = getattr(candidate_module, name)
        assert dataclasses.is_dataclass(value_type)
        assert tuple(field.name for field in fields(value_type)) == expected_fields
        assert "__dict__" not in value_type.__slots__
        assert public_callables(value_type) == set()
        assert public_surface_names(value_type) == set(expected_fields)


def test_value_schemas_are_frozen_identity_only_and_nonserializable() -> None:
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in inspect.signature(SessionBinding).parameters.values()
    )
    session = FakeSession("one", b"one")
    binding = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=OTHER_REVISION,
        session=session,
    )
    twin = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=OTHER_REVISION,
        session=session,
    )
    assert binding is not twin
    assert binding != twin
    assert tuple(field.name for field in fields(SessionBinding)) == (
        "project_id",
        "revision_id",
        "session",
    )
    with pytest.raises(FrozenInstanceError):
        binding.session = object()  # type: ignore[misc]
    for operation in (
        lambda: copy.copy(binding),
        lambda: copy.deepcopy(binding),
    ):
        try:
            copied = operation()
        except TypeError:
            pass
        else:
            assert type(copied) is SessionBinding
            assert copied is not binding
            assert copied != binding
    with pytest.raises(TypeError):
        pickle.dumps(binding)
    with pytest.raises(CandidateError) as caught:
        SessionBinding(project_id=PROJECT_ID, revision_id=OTHER_REVISION, session=None)
    _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
    with pytest.raises(CandidateError) as caught:
        SessionBinding(project_id=ExplosiveValue(), revision_id=OTHER_REVISION, session=session)
    _assert_candidate_error(caught, CandidateErrorCode.INVALID_IDENTIFIER)


def test_all_handles_and_results_are_keyword_only_identity_values_and_nonserializable(
    tmp_path: Path,
) -> None:
    values: list[object] = []
    with _rig(tmp_path, suffix="value-commit") as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        checkpointed.step_path.write_bytes(b"STEP")
        sealed = rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        commit_result, *_unused = _commit(rig, sealed)
        values.extend((active, checkpointed, sealed, commit_result))
    with _rig(tmp_path, suffix="value-rollback") as rig:
        active = _begin(rig)
        rollback_result = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        values.append(rollback_result)
    with _rig(tmp_path, suffix="value-reconcile") as rig:
        reconcile_result = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        values.append(reconcile_result)

    assert commit_result.schema_version == 1
    assert rollback_result.schema_version == 1
    assert reconcile_result.schema_version == 1
    assert (
        replace(
            commit_result,
            status=CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED,
            cleanup_required=True,
        ).cleanup_required
        is True
    )
    assert (
        replace(
            commit_result,
            status=CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED,
            slot_promoted=None,
            recovery_required=True,
        ).recovery_required
        is True
    )
    assert (
        replace(
            rollback_result,
            status=CandidateRollbackStatus.CLEANUP_REQUIRED,
            cleanup_required=True,
        ).cleanup_required
        is True
    )
    assert (
        replace(
            rollback_result,
            status=CandidateRollbackStatus.RECOVERY_REQUIRED,
            slot_promoted=None,
            recovery_required=True,
        ).recovery_required
        is True
    )
    assert (
        replace(
            reconcile_result,
            status=CandidateReconcileStatus.CLEANUP_REQUIRED,
            cleanup_required=True,
        ).cleanup_required
        is True
    )
    assert (
        replace(
            reconcile_result,
            status=CandidateReconcileStatus.RECOVERY_REQUIRED,
            slot_promoted=None,
            recovery_required=True,
        ).recovery_required
        is True
    )
    unreadable_rollback = replace(
        rollback_result,
        status=CandidateRollbackStatus.RECOVERY_REQUIRED,
        head=None,
        live_binding=None,
        reconciliation=None,
        slot_promoted=None,
        recovery_required=True,
    )
    assert unreadable_rollback.head is None
    unreadable_reconcile = replace(
        reconcile_result,
        status=CandidateReconcileStatus.RECOVERY_REQUIRED,
        head=None,
        live_binding=None,
        reconciliation=None,
        slot_promoted=None,
        recovery_required=True,
    )
    assert unreadable_reconcile.head is None

    with pytest.raises(ValueError):
        replace(commit_result, recovery_required=True)
    with pytest.raises(ValueError):
        replace(commit_result, cleanup_required=True)
    with pytest.raises(ValueError):
        replace(commit_result, cleanup_binding=commit_result.live_binding)
    with pytest.raises(ValueError):
        replace(rollback_result, head_committed=True)
    with pytest.raises(ValueError):
        replace(rollback_result, slot_promoted=True)
    with pytest.raises(ValueError):
        replace(reconcile_result, recovery_required=True)

    result_cleanup_statuses = (
        (commit_result, CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED),
        (rollback_result, CandidateRollbackStatus.CLEANUP_REQUIRED),
        (reconcile_result, CandidateReconcileStatus.CLEANUP_REQUIRED),
    )
    for result, cleanup_status in result_cleanup_statuses:
        with pytest.raises(ValueError):
            replace(
                result,
                status=cleanup_status,
                cleanup_required=True,
                recovery_required=True,
            )
        assert result.live_binding is not None
        live_alias = SessionBinding(
            project_id=result.live_binding.project_id,
            revision_id=result.live_binding.revision_id,
            session=result.live_binding.session,
        )
        with pytest.raises(ValueError):
            replace(
                result,
                status=cleanup_status,
                cleanup_required=True,
                cleanup_binding=live_alias,
            )
        detached_cleanup = SessionBinding(
            project_id=result.live_binding.project_id,
            revision_id=result.live_binding.revision_id,
            session=FakeSession("detached-cleanup", b"detached"),
        )
        with pytest.raises(ValueError):
            replace(result, cleanup_binding=detached_cleanup)
        foreign_cleanup = SessionBinding(
            project_id=OTHER_PROJECT_ID,
            revision_id=result.live_binding.revision_id,
            session=FakeSession("foreign-cleanup", b"foreign"),
        )
        with pytest.raises(ValueError):
            replace(
                result,
                status=cleanup_status,
                cleanup_required=True,
                cleanup_binding=foreign_cleanup,
            )

    wrong_commit_revision_cleanup = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=OTHER_REVISION,
        session=FakeSession("wrong-revision-cleanup", b"wrong-revision"),
    )
    with pytest.raises(ValueError):
        replace(
            commit_result,
            status=CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED,
            cleanup_required=True,
            cleanup_binding=wrong_commit_revision_cleanup,
        )

    for result in (commit_result, rollback_result, reconcile_result):
        with pytest.raises(ValueError):
            replace(result, status=result.status.value)
        with pytest.raises(ValueError):
            replace(result, schema_version=True)
        for flag in (
            "head_committed",
            "slot_promoted",
            "cleanup_required",
            "recovery_required",
        ):
            with pytest.raises(ValueError):
                replace(result, **{flag: 1})

    for invalid in (
        lambda: replace(commit_result, schema_version=999),
        lambda: replace(commit_result, status=CandidateReconcileStatus.COMMITTED),
        lambda: replace(
            commit_result,
            status=CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED,
        ),
        lambda: replace(
            commit_result,
            status=CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED,
        ),
        lambda: replace(commit_result, head=None),
        lambda: replace(commit_result, live_binding=None),
        lambda: replace(commit_result, head_committed=False),
        lambda: replace(commit_result, slot_promoted=False),
        lambda: replace(commit_result, slot_promoted=None),
        lambda: replace(commit_result, revision=None),
        lambda: replace(commit_result, report=None),
        lambda: replace(rollback_result, schema_version=999),
        lambda: replace(rollback_result, status=CandidateCommitStatus.COMMITTED),
        lambda: replace(
            rollback_result,
            status=CandidateRollbackStatus.CLEANUP_REQUIRED,
        ),
        lambda: replace(
            rollback_result,
            status=CandidateRollbackStatus.RECOVERY_REQUIRED,
        ),
        lambda: replace(rollback_result, head=None),
        lambda: replace(rollback_result, live_binding=None),
        lambda: replace(rollback_result, reconciliation=None),
        lambda: replace(rollback_result, head_committed=True),
        lambda: replace(rollback_result, slot_promoted=None),
        lambda: replace(reconcile_result, schema_version=999),
        lambda: replace(reconcile_result, status=CandidateCommitStatus.COMMITTED),
        lambda: replace(
            reconcile_result,
            status=CandidateReconcileStatus.CLEANUP_REQUIRED,
        ),
        lambda: replace(
            reconcile_result,
            status=CandidateReconcileStatus.RECOVERY_REQUIRED,
        ),
        lambda: replace(reconcile_result, head=None),
        lambda: replace(reconcile_result, live_binding=None),
        lambda: replace(reconcile_result, reconciliation=None),
        lambda: replace(reconcile_result, head_committed=False),
        lambda: replace(reconcile_result, slot_promoted=None),
        lambda: replace(sealed, revision=None),
        lambda: replace(sealed, base_head=None),
    ):
        with pytest.raises(ValueError):
            invalid()

    for value in values:
        value_type = type(value)
        signature = inspect.signature(value_type)
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in signature.parameters.values()
        )
        assert not hasattr(value, "__dict__")
        twin = replace(value)
        assert twin is not value
        assert twin != value
        first_field = fields(value)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(value, first_field, getattr(value, first_field))
        for operation in (
            lambda current=value: copy.copy(current),
            lambda current=value: copy.deepcopy(current),
        ):
            try:
                copied = operation()
            except TypeError:
                pass
            else:
                assert type(copied) is value_type
                assert copied is not value
                assert copied != value
        with pytest.raises(TypeError):
            pickle.dumps(value)


def test_candidate_errors_use_fixed_nonreflective_messages() -> None:
    messages: dict[CandidateErrorCode, str] = {}
    for code in CandidateErrorCode:
        metadata = {
            "head_committed": False,
            "cleanup_required": code is CandidateErrorCode.CLEANUP_REQUIRED,
            "recovery_required": code is CandidateErrorCode.RECOVERY_REQUIRED,
        }
        first = CandidateError(code, **metadata)
        second = CandidateError(code, **metadata)
        assert first.message == second.message
        assert "project_" not in first.message
        assert "revision_" not in first.message
        assert "/" not in first.message
        messages[code] = first.message
    assert len(set(messages.values())) == len(messages)

    for raw_code in ("invalid_input", None, ExplosiveValue()):
        with pytest.raises((TypeError, ValueError)):
            CandidateError(raw_code)  # type: ignore[arg-type]
    for field in ("head_committed", "cleanup_required", "recovery_required"):
        with pytest.raises((TypeError, ValueError)):
            CandidateError(CandidateErrorCode.INVALID_INPUT, **{field: 1})
    with pytest.raises((TypeError, ValueError)):
        CandidateError(CandidateErrorCode.CLEANUP_REQUIRED)
    with pytest.raises((TypeError, ValueError)):
        CandidateError(CandidateErrorCode.RECOVERY_REQUIRED)
    with pytest.raises((TypeError, ValueError)):
        CandidateError(
            CandidateErrorCode.CLEANUP_REQUIRED,
            cleanup_required=True,
            recovery_required=True,
        )


def test_session_slot_uses_identity_cas_and_rejects_aliases() -> None:
    baseline = FakeSession("baseline", b"base")
    initial = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=OTHER_REVISION,
        session=baseline,
    )
    slot = SessionSlot(initial)
    replacement_session = FakeSession("replacement", b"new")
    replacement = SessionBinding(
        project_id=PROJECT_ID,
        revision_id="revision_22222222222222222222222222222222",
        session=replacement_session,
    )
    equal_fields_not_identity = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=OTHER_REVISION,
        session=baseline,
    )
    assert slot.current() is initial
    assert slot.compare_and_set(equal_fields_not_identity, replacement) is False
    assert slot.current() is initial
    assert slot.compare_and_set(initial, replacement) is True
    assert slot.current() is replacement
    with pytest.raises(CandidateError) as caught:
        slot.compare_and_set(
            replacement,
            SessionBinding(
                project_id=PROJECT_ID,
                revision_id="revision_33333333333333333333333333333333",
                session=replacement_session,
            ),
        )
    _assert_candidate_error(caught, CandidateErrorCode.SESSION_ALIAS)
    with pytest.raises(CandidateError) as caught:
        slot.compare_and_set(
            replacement,
            SessionBinding(
                project_id=OTHER_PROJECT_ID,
                revision_id="revision_33333333333333333333333333333333",
                session=FakeSession("foreign", b"foreign"),
            ),
        )
    _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)


def test_session_slot_concurrent_cas_publishes_exactly_one_binding() -> None:
    initial = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=OTHER_REVISION,
        session=FakeSession("baseline", b"base"),
    )
    slot = SessionSlot(initial)
    replacements = tuple(
        SessionBinding(
            project_id=PROJECT_ID,
            revision_id=f"revision_{index:032x}",
            session=FakeSession(f"candidate-{index}", bytes([index])),
        )
        for index in range(2, 10)
    )
    barrier = threading.Barrier(len(replacements) + 1)
    outcomes: list[tuple[SessionBinding, bool]] = []

    def publish(replacement: SessionBinding) -> None:
        barrier.wait()
        outcomes.append((replacement, slot.compare_and_set(initial, replacement)))

    threads = [
        threading.Thread(target=publish, args=(replacement,)) for replacement in replacements
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    winners = [replacement for replacement, won in outcomes if won]
    assert len(winners) == 1
    assert slot.current() is winners[0]


def test_public_constructor_boundaries_reject_malformed_and_hostile_values(
    tmp_path: Path,
) -> None:
    session = FakeSession("constructor", b"constructor")
    for revision_id in (None, "revision_not-canonical", ExplosiveValue()):
        with pytest.raises(CandidateError) as caught:
            SessionBinding(
                project_id=PROJECT_ID,
                revision_id=revision_id,
                session=session,
            )
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_IDENTIFIER)

    with pytest.raises(CandidateError) as caught:
        SessionBinding(
            project_id="project_not-canonical",
            revision_id=OTHER_REVISION,
            session=session,
        )
    _assert_candidate_error(caught, CandidateErrorCode.INVALID_IDENTIFIER)

    with pytest.raises(CandidateError) as caught:
        SessionSlot(ExplosiveValue())
    _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)

    initial = SessionBinding(
        project_id=PROJECT_ID,
        revision_id=OTHER_REVISION,
        session=session,
    )
    replacement = SessionBinding(
        project_id=PROJECT_ID,
        revision_id="revision_22222222222222222222222222222222",
        session=FakeSession("replacement", b"replacement"),
    )
    slot = SessionSlot(initial)
    with pytest.raises(CandidateError) as caught:
        slot.compare_and_set(ExplosiveValue(), replacement)
    _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
    with pytest.raises(CandidateError) as caught:
        slot.compare_and_set(initial, ExplosiveValue())
    _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
    assert slot.current() is initial

    with _rig(tmp_path, suffix="constructor-boundaries") as rig:
        for kwargs in (
            {
                "store": ExplosiveValue(),
                "snapshot_port": rig.port,
                "session_slot": rig.slot,
            },
            {
                "store": rig.store,
                "snapshot_port": ExplosiveValue(),
                "session_slot": rig.slot,
            },
            {
                "store": rig.store,
                "snapshot_port": rig.port,
                "session_slot": ExplosiveValue(),
            },
        ):
            with pytest.raises(CandidateError) as caught:
                CandidateCoordinator(**kwargs)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_INPUT)

        class LocalRevisionStoreSubclass(LocalRevisionStore):
            pass

        subclass_store = LocalRevisionStoreSubclass(
            _secure_root(tmp_path / "constructor-store-subclass"),
            rig.manager,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        )
        with pytest.raises(CandidateError) as caught:
            CandidateCoordinator(
                store=subclass_store,
                snapshot_port=rig.port,
                session_slot=rig.slot,
            )
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_INPUT)

        try:

            class SessionSlotSubclass(SessionSlot):
                pass

        except TypeError:
            pass
        else:
            subclass_slot = SessionSlotSubclass(initial)
            with pytest.raises(CandidateError) as caught:
                CandidateCoordinator(
                    store=rig.store,
                    snapshot_port=rig.port,
                    session_slot=subclass_slot,
                )
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_INPUT)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("project_id", "project_not-canonical", CandidateErrorCode.INVALID_IDENTIFIER),
        ("project_id", ExplosiveValue(), CandidateErrorCode.INVALID_IDENTIFIER),
        ("expected_head", ExplosiveValue(), CandidateErrorCode.INVALID_INPUT),
        ("lease", ExplosiveValue(), CandidateErrorCode.INVALID_LEASE),
    ],
)
def test_begin_hostile_input_is_rejected_without_store_cad_or_slot_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    code: CandidateErrorCode,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"begin-hostile-{field}") as rig:
        kwargs = {
            "project_id": PROJECT_ID,
            "expected_head": rig.head,
            "lease": rig.lease,
        }
        kwargs[field] = value
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.begin(**kwargs)
        _assert_candidate_error(caught, code)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        assert rig.slot.current() is rig.baseline_binding
        assert rig.store.load_head(PROJECT_ID) == rig.head


def test_begin_empty_creates_isolated_session_and_store_owned_paths(tmp_path: Path) -> None:
    with _rig(tmp_path) as rig:
        active = _begin(rig)
        assert type(active) is ActiveCandidate
        assert active.project_id == PROJECT_ID
        assert active.base_head is rig.head
        assert active.binding.project_id == PROJECT_ID
        assert active.binding.revision_id.startswith("revision_")
        assert active.binding.session is not rig.baseline
        assert active.binding.session is rig.port.created[0]
        assert active.model_path == rig.store.candidate_model_path(
            PROJECT_ID, active.binding.revision_id, rig.lease
        )
        assert active.step_path == rig.store.candidate_artifact_path(
            PROJECT_ID, active.binding.revision_id, "step", rig.lease
        )
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.calls[0] == (
            "create_empty",
            active.binding.revision_id,
            None,
        )


def test_begin_imported_loads_candidate_copy_not_baseline(tmp_path: Path) -> None:
    with _rig(tmp_path, imported=True) as rig:
        active = _begin(rig)
        assert type(active.binding.session) is FakeSession
        assert active.binding.session is not rig.baseline
        assert active.binding.session.payload == b"imported-base-fcstd"
        assert rig.port.calls[0][0] == "load_fcstd"
        assert rig.port.calls[0][1] == active.model_path
        assert rig.slot.current() is rig.baseline_binding
        assert not rig.baseline.closed


@pytest.mark.parametrize(
    ("imported", "failure", "code"),
    [
        (False, "create", CandidateErrorCode.CAD_FAILURE),
        (False, "alias", CandidateErrorCode.SESSION_ALIAS),
        (True, "load", CandidateErrorCode.CAD_FAILURE),
        (True, "alias", CandidateErrorCode.SESSION_ALIAS),
    ],
)
def test_begin_faults_reconcile_staging_without_closing_baseline(
    tmp_path: Path,
    imported: bool,
    failure: str,
    code: CandidateErrorCode,
) -> None:
    with _rig(tmp_path, imported=imported, suffix=f"begin-{imported}-{failure}") as rig:
        if failure == "create":
            rig.port.fail_create = True
        elif failure == "load":
            rig.port.fail_load_number = 1
        elif imported:
            rig.port.load_aliases[1] = rig.baseline
        else:
            rig.port.create_alias = rig.baseline
        with pytest.raises(CandidateError) as caught:
            _begin(rig)
        error = _assert_candidate_error(caught, code)
        assert error.head_committed is False
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert not rig.baseline.closed
        reconciled = rig.store.reconcile(PROJECT_ID, rig.lease)
        assert reconciled.status is ReconciliationStatus.NOT_COMMITTED


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        ("not_committed", CandidateErrorCode.STORE_FAILURE),
        ("cleanup", CandidateErrorCode.CLEANUP_REQUIRED),
        ("raise", CandidateErrorCode.RECOVERY_REQUIRED),
    ],
)
def test_begin_revision_uncertainty_reconciles_once_and_reports_durable_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_code: CandidateErrorCode,
) -> None:
    with _rig(tmp_path, suffix=f"begin-revision-uncertain-{outcome}") as rig:
        original_begin = candidate_module._reserve_candidate_revision
        original_reconcile = LocalRevisionStore.reconcile
        reconcile_calls = 0
        rollback_calls = 0

        def begin_then_raise(store, project_id, expected_head, reservation_key, lease):
            original_begin(store, project_id, expected_head, reservation_key, lease)
            raise RevisionStoreError(
                RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=False,
            )

        def scripted_reconcile(self, project_id, lease):
            nonlocal reconcile_calls
            reconcile_calls += 1
            result = original_reconcile(self, project_id, lease)
            assert result.status is ReconciliationStatus.NOT_COMMITTED
            if outcome == "cleanup":
                return replace(result, status=ReconciliationStatus.CLEANUP_REQUIRED)
            if outcome == "raise":
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
            return result

        def forbidden_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            raise AssertionError("unknown revision id must reconcile, not roll back")

        monkeypatch.setattr(candidate_module, "_reserve_candidate_revision", begin_then_raise)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", scripted_reconcile)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
        with pytest.raises(CandidateError) as caught:
            _begin(rig)
        error = _assert_candidate_error(caught, expected_code)
        assert error.cleanup_required is (outcome == "cleanup")
        assert error.recovery_required is (outcome == "raise")
        assert reconcile_calls == 1
        assert rollback_calls == 0
        assert rig.port.calls == []
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        reconciled = original_reconcile(rig.store, PROJECT_ID, rig.lease)
        assert reconciled.status is ReconciliationStatus.NOT_COMMITTED


@pytest.mark.parametrize("path_kind", ["model", "artifact"])
@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        ("not_committed", CandidateErrorCode.STORE_FAILURE),
        ("cleanup", CandidateErrorCode.CLEANUP_REQUIRED),
        ("raise", CandidateErrorCode.RECOVERY_REQUIRED),
    ],
)
def test_begin_path_fault_rolls_back_once_and_reports_durable_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_kind: str,
    outcome: str,
    expected_code: CandidateErrorCode,
) -> None:
    with _rig(tmp_path, suffix=f"begin-{path_kind}-{outcome}") as rig:
        original_rollback = LocalRevisionStore.rollback_revision
        original_reconcile = LocalRevisionStore.reconcile
        rollback_calls = 0
        reconcile_calls = 0

        def fail_model_path(self, project_id, revision_id, lease):
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)

        def fail_artifact_path(self, project_id, revision_id, format, lease):
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)

        def scripted_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            result = original_rollback(self, project_id, revision_id, lease)
            assert result.status is ReconciliationStatus.NOT_COMMITTED
            if outcome == "cleanup":
                return replace(result, status=ReconciliationStatus.CLEANUP_REQUIRED)
            if outcome == "raise":
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
            return result

        def forbidden_reconcile(self, project_id, lease):
            nonlocal reconcile_calls
            reconcile_calls += 1
            raise AssertionError("known revision id must roll back, not reconcile")

        if path_kind == "model":
            monkeypatch.setattr(LocalRevisionStore, "candidate_model_path", fail_model_path)
        else:
            monkeypatch.setattr(
                LocalRevisionStore,
                "candidate_artifact_path",
                fail_artifact_path,
            )
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", scripted_rollback)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", forbidden_reconcile)
        with pytest.raises(CandidateError) as caught:
            _begin(rig)
        error = _assert_candidate_error(caught, expected_code)
        assert error.cleanup_required is (outcome == "cleanup")
        assert error.recovery_required is (outcome == "raise")
        assert rollback_calls == 1
        assert reconcile_calls == 0
        assert rig.port.calls == []
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        reconciled = original_reconcile(rig.store, PROJECT_ID, rig.lease)
        assert reconciled.status is ReconciliationStatus.NOT_COMMITTED


def test_begin_rejects_slot_or_head_conflict_before_cad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path) as rig:
        third = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=OTHER_REVISION,
            session=FakeSession("third", b"third"),
        )
        assert rig.slot.compare_and_set(rig.baseline_binding, third)
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            _begin(rig)
        _assert_candidate_error(caught, CandidateErrorCode.CONFLICT)
        assert not any(mutations.values())
        assert rig.port.calls == []
        assert rig.store.load_head(PROJECT_ID) == rig.head


def test_begin_rejects_stale_expected_head_before_store_or_cad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="begin-stale-expected-head") as rig:
        stale_head = ProjectHead(
            project_id=PROJECT_ID,
            generation=rig.head.generation + 1,
            revision_id=OTHER_REVISION,
            manifest_sha256=MANIFEST,
        )
        original_begin = LocalRevisionStore.begin_revision
        begin_calls = 0

        def counted_begin(self, project_id, expected_head, lease):
            nonlocal begin_calls
            begin_calls += 1
            return original_begin(self, project_id, expected_head, lease)

        monkeypatch.setattr(LocalRevisionStore, "begin_revision", counted_begin)
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.begin(
                project_id=PROJECT_ID,
                expected_head=stale_head,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.CONFLICT)
        assert begin_calls == 0
        assert not any(mutations.values())
        assert rig.port.calls == []
        assert rig.store.load_head(PROJECT_ID) == rig.head
        reconciled = rig.store.reconcile(PROJECT_ID, rig.lease)
        assert reconciled.status is ReconciliationStatus.CLEAN


@pytest.mark.parametrize("kind", ["foreign", "released"])
def test_begin_lease_authority_is_checked_before_any_cad(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"begin-lease-{kind}") as rig:
        other_manager = ResourceLeaseManager(
            _secure_root(tmp_path / f"begin-lease-{kind}-other-locks"),
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        supplied = other_manager.acquire_project_write(PROJECT_ID)
        if kind == "released":
            supplied.release(owner_token=supplied.owner_token)
        _reset_store_mutations(mutations)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.begin(
                    project_id=PROJECT_ID,
                    expected_head=rig.head,
                    lease=supplied,
                )
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert not any(mutations.values())
            assert rig.port.calls == []
            assert rig.store.load_head(PROJECT_ID) == rig.head
            assert rig.slot.current() is rig.baseline_binding
        finally:
            if not supplied.released:
                supplied.release(owner_token=supplied.owner_token)


def test_cross_coordinator_and_forged_handles_have_zero_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path) as rig:
        active = _begin(rig)
        other = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            other.checkpoint(candidate=active, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            other.rollback(candidate=active, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        checkpointed = _checkpoint(rig, active)
        rolled_back = rig.coordinator.rollback(candidate=checkpointed, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED


def test_forged_checkpointed_and_sealed_handles_authorize_no_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="forged-later-handles") as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        checkpointed.step_path.write_bytes(b"STEP")
        forged_checkpoint = replace(checkpointed)
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.seal(candidate=forged_checkpoint, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before

        sealed = rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        _reset_store_mutations(mutations)
        forged_sealed = replace(sealed)
        compiled, snapshot, receipt, report = _verification(sealed)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=forged_sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.rollback(candidate=forged_sealed, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        consumed = consume_verification_receipt(
            receipt,
            compiled,
            snapshot,
            candidate_revision=sealed.revision.id,
            manifest_sha256=sealed.revision.manifest_sha256,
        )
        assert consumed == report
        assert consumed is not report
        rig.coordinator.rollback(candidate=sealed, lease=rig.lease)

        forged = replace(active, binding=active.binding)
        _reset_store_mutations(mutations)
        before_after_rollback = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.checkpoint(candidate=forged, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before_after_rollback


def test_cross_coordinator_sealed_rejection_preserves_original_commit_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="cross-coordinator-sealed") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, report = _verification(sealed)
        other = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            other.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            other.rollback(candidate=sealed, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before

        committed = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert committed.status is CandidateCommitStatus.COMMITTED
        assert committed.report == report
        assert committed.report is not report


@pytest.mark.parametrize("stage", ["active", "checkpointed", "sealed"])
@pytest.mark.parametrize("copier", [copy.copy, copy.deepcopy], ids=["copy", "deepcopy"])
def test_copied_handle_is_either_rejected_by_copy_or_cannot_authorize_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    copier,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"copied-handle-{stage}-{copier.__name__}") as rig:
        active = _begin(rig)
        authentic: ActiveCandidate | CheckpointedCandidate | SealedCandidate = active
        if stage in {"checkpointed", "sealed"}:
            authentic = _checkpoint(rig, active)
        if stage == "sealed":
            assert type(authentic) is CheckpointedCandidate
            authentic.step_path.write_bytes(b"STEP")
            authentic = rig.coordinator.seal(candidate=authentic, lease=rig.lease)
        try:
            copied = copier(authentic)
        except TypeError:
            copied = None

        if copied is not None:
            assert copied is not authentic
            assert copied != authentic
            _reset_store_mutations(mutations)
            before = tuple(rig.port.calls)
            with pytest.raises(CandidateError) as caught:
                if stage == "active":
                    rig.coordinator.checkpoint(candidate=copied, lease=rig.lease)
                elif stage == "checkpointed":
                    rig.coordinator.seal(candidate=copied, lease=rig.lease)
                else:
                    rig.coordinator.rollback(candidate=copied, lease=rig.lease)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
            assert not any(mutations.values())
            assert tuple(rig.port.calls) == before
        recovered = rig.coordinator.rollback(candidate=authentic, lease=rig.lease)
        assert recovered.status is CandidateRollbackStatus.NOT_COMMITTED


@pytest.mark.parametrize("stage", ["active", "checkpointed"])
@pytest.mark.parametrize("kind", ["forged", "cross", "stale"])
def test_active_and_checkpointed_rollback_require_exact_live_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    kind: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"rollback-capability-{stage}-{kind}") as rig:
        active = _begin(rig)
        stage_handle: ActiveCandidate | CheckpointedCandidate = active
        if stage == "checkpointed":
            stage_handle = _checkpoint(rig, active)

        authentic: ActiveCandidate | CheckpointedCandidate | SealedCandidate = stage_handle
        supplied: ActiveCandidate | CheckpointedCandidate = stage_handle
        target = rig.coordinator
        if kind == "forged":
            supplied = replace(stage_handle)
        elif kind == "cross":
            target = CandidateCoordinator(
                store=rig.store,
                snapshot_port=rig.port,
                session_slot=rig.slot,
            )
        else:
            assert kind == "stale"
            if stage == "active":
                authentic = _checkpoint(rig, active)
            else:
                assert type(stage_handle) is CheckpointedCandidate
                stage_handle.step_path.write_bytes(b"STEP")
                authentic = rig.coordinator.seal(
                    candidate=stage_handle,
                    lease=rig.lease,
                )

        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            target.rollback(candidate=supplied, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding

        recovered = rig.coordinator.rollback(candidate=authentic, lease=rig.lease)
        assert recovered.status is CandidateRollbackStatus.NOT_COMMITTED


def test_checkpoint_saves_reloads_then_closes_active_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, imported=True, suffix="checkpoint-exact-snapshot") as rig:
        active = _begin(rig)
        active_session = active.binding.session
        baseline_revision = rig.store.load_revision(PROJECT_ID, rig.head.revision_id)
        baseline_model_path = rig.store.revision_model_path(
            PROJECT_ID,
            rig.head.revision_id,
        )
        baseline_model_bytes = baseline_model_path.read_bytes()
        assert type(active_session) is FakeSession
        active_session.payload = b"candidate-mutated-fcstd"
        checkpointed = _checkpoint(rig, active)
        checkpoint_session = checkpointed.binding.session
        assert type(checkpointed) is CheckpointedCandidate
        assert checkpoint_session is not active_session
        assert checkpoint_session is not rig.baseline
        assert checkpoint_session.payload == b"candidate-mutated-fcstd"
        checkpoint_index = next(
            index for index, call in enumerate(rig.port.calls) if call[0] == "checkpoint_fcstd"
        )
        assert rig.port.calls[checkpoint_index] == (
            "checkpoint_fcstd",
            active_session,
            active.model_path,
        )
        reload_index = next(
            index
            for index, call in enumerate(rig.port.calls)
            if index > checkpoint_index and call[0] == "load_fcstd"
        )
        assert rig.port.calls[reload_index] == ("load_fcstd", active.model_path, None)
        close_index = next(
            index
            for index, call in enumerate(rig.port.calls)
            if call[0] == "close" and call[1] is active_session
        )
        assert checkpoint_index < reload_index < close_index
        assert rig.port.close_count(active_session) == 1
        assert rig.port.close_count(checkpoint_session) == 0
        _reset_store_mutations(mutations)
        before_stale = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.checkpoint(candidate=active, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before_stale
        rolled_back = rig.coordinator.rollback(candidate=checkpointed, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.store.load_revision(PROJECT_ID, rig.head.revision_id) == baseline_revision
        assert baseline_model_path.read_bytes() == baseline_model_bytes


def test_successful_seal_rejects_stale_checkpointed_without_any_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="stale-checkpointed-after-seal") as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        checkpointed.step_path.write_bytes(b"STEP")
        sealed = rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(checkpointed.binding.session) == 1
        assert rig.port.close_count(sealed.binding.session) == 0

        rolled_back = rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED


def test_transition_retires_old_binding_before_close_hook_can_publish_it(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="transition-retirement") as rig:
        active = _begin(rig)
        active_rejections: list[CandidateErrorCode] = []

        def try_active_publish() -> None:
            try:
                rig.slot.compare_and_set(rig.baseline_binding, active.binding)
            except CandidateError as exc:
                active_rejections.append(exc.code)

        rig.port.close_hooks[id(active.binding.session)] = try_active_publish
        checkpointed = _checkpoint(rig, active)
        assert active_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert rig.slot.current() is rig.baseline_binding
        with pytest.raises(CandidateError) as caught:
            rig.slot.compare_and_set(rig.baseline_binding, active.binding)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)

        checkpoint_rejections: list[CandidateErrorCode] = []

        def try_checkpoint_publish() -> None:
            try:
                rig.slot.compare_and_set(rig.baseline_binding, checkpointed.binding)
            except CandidateError as exc:
                checkpoint_rejections.append(exc.code)

        checkpointed.step_path.write_bytes(b"STEP")
        rig.port.close_hooks[id(checkpointed.binding.session)] = try_checkpoint_publish
        rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        assert checkpoint_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert rig.slot.current() is rig.baseline_binding
        with pytest.raises(CandidateError) as caught:
            rig.slot.compare_and_set(rig.baseline_binding, checkpointed.binding)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)


@pytest.mark.parametrize("failure", ["checkpoint", "reload", "close"])
def test_checkpoint_fault_terminally_reconciles_without_touching_baseline(
    tmp_path: Path, failure: str
) -> None:
    with _rig(tmp_path, suffix=f"checkpoint-{failure}") as rig:
        active = _begin(rig)
        active_session = active.binding.session
        if failure == "checkpoint":
            rig.port.fail_checkpoint = True
        elif failure == "reload":
            rig.port.fail_load_number = 1
        else:
            rig.port.close_failures.add(id(active_session))
        with pytest.raises(CandidateError) as caught:
            _checkpoint(rig, active)
        error = caught.value
        assert error.head_committed is False
        if failure == "close":
            assert error.cleanup_required is True
        else:
            assert error.code is CandidateErrorCode.CAD_FAILURE
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert not rig.baseline.closed
        with pytest.raises(CandidateError) as duplicate:
            rig.coordinator.rollback(candidate=active, lease=rig.lease)
        _assert_candidate_error(duplicate, CandidateErrorCode.ALREADY_TERMINAL)
        with pytest.raises(CandidateError) as stale:
            rig.slot.compare_and_set(rig.baseline_binding, active.binding)
        _assert_candidate_error(stale, CandidateErrorCode.INVALID_BINDING)
        for session in rig.port.created:
            assert rig.port.close_count(session) == 1


@pytest.mark.parametrize("alias", ["active", "baseline"])
def test_checkpoint_reload_alias_is_rejected_and_every_owned_session_closes_once(
    tmp_path: Path, alias: str
) -> None:
    with _rig(tmp_path, suffix=f"checkpoint-alias-{alias}") as rig:
        active = _begin(rig)
        active_session = active.binding.session
        rig.port.load_aliases[1] = active_session if alias == "active" else rig.baseline
        with pytest.raises(CandidateError) as caught:
            _checkpoint(rig, active)
        _assert_candidate_error(caught, CandidateErrorCode.SESSION_ALIAS)
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(active_session) == 1
        assert rig.port.close_count(rig.baseline) == 0


@pytest.mark.parametrize(
    ("stage", "drift"),
    [
        ("checkpoint", "slot"),
        ("checkpoint", "head"),
        ("seal", "slot"),
        ("seal", "head"),
    ],
)
def test_checkpoint_and_seal_recheck_head_and_slot_before_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    drift: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"{stage}-{drift}-drift") as rig:
        active = _begin(rig)
        candidate: ActiveCandidate | CheckpointedCandidate = active
        if stage == "seal":
            candidate = _checkpoint(rig, active)
            candidate.step_path.write_bytes(b"STEP")
        before = tuple(rig.port.calls)
        third = None
        original_load = LocalRevisionStore.load_head
        if drift == "slot":
            third = SessionBinding(
                project_id=PROJECT_ID,
                revision_id=OTHER_REVISION,
                session=FakeSession("third", b"third"),
            )
            assert rig.slot.compare_and_set(rig.baseline_binding, third)
        else:
            changed = ProjectHead(
                project_id=PROJECT_ID,
                generation=rig.head.generation + 1,
                revision_id=OTHER_REVISION,
                manifest_sha256=MANIFEST,
            )

            def changed_head(self, project_id):
                return changed

            monkeypatch.setattr(LocalRevisionStore, "load_head", changed_head)
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            if stage == "checkpoint":
                assert type(candidate) is ActiveCandidate
                rig.coordinator.checkpoint(candidate=candidate, lease=rig.lease)
            else:
                assert type(candidate) is CheckpointedCandidate
                rig.coordinator.seal(candidate=candidate, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.CONFLICT)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        assert rig.port.close_count(candidate.binding.session) == 0
        if third is not None:
            assert rig.slot.compare_and_set(third, rig.baseline_binding)
        else:
            monkeypatch.setattr(LocalRevisionStore, "load_head", original_load)
        rolled_back = rig.coordinator.rollback(candidate=candidate, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED


@pytest.mark.parametrize("stage", ["checkpoint", "seal"])
def test_checkpoint_and_seal_reject_same_issuer_replacement_lease_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"{stage}-replacement-lease") as rig:
        active = _begin(rig)
        candidate: ActiveCandidate | CheckpointedCandidate = active
        if stage == "seal":
            candidate = _checkpoint(rig, active)
            candidate.step_path.write_bytes(b"STEP")
        before = tuple(rig.port.calls)
        rig.lease.release(owner_token=rig.lease.owner_token)
        replacement_lease = rig.manager.acquire_project_write(PROJECT_ID)
        _reset_store_mutations(mutations)
        try:
            with pytest.raises(CandidateError) as caught:
                if stage == "checkpoint":
                    assert type(candidate) is ActiveCandidate
                    rig.coordinator.checkpoint(
                        candidate=candidate,
                        lease=replacement_lease,
                    )
                else:
                    assert type(candidate) is CheckpointedCandidate
                    rig.coordinator.seal(
                        candidate=candidate,
                        lease=replacement_lease,
                    )
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert not any(mutations.values())
            assert tuple(rig.port.calls) == before
            assert rig.port.close_count(candidate.binding.session) == 0
            recovered = rig.coordinator.reconcile(
                project_id=PROJECT_ID,
                lease=replacement_lease,
            )
            assert recovered.status is CandidateReconcileStatus.NOT_COMMITTED
            assert rig.port.close_count(candidate.binding.session) == 1
        finally:
            replacement_lease.release(owner_token=replacement_lease.owner_token)


def test_seal_requires_step_and_terminally_rolls_back_missing_artifact(tmp_path: Path) -> None:
    with _rig(tmp_path) as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        checkpoint_session = checkpointed.binding.session
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        error = _assert_candidate_error(caught, CandidateErrorCode.STORE_FAILURE)
        assert error.head_committed is False
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(checkpoint_session) == 1
        reconciled = rig.store.reconcile(PROJECT_ID, rig.lease)
        assert reconciled.status is ReconciliationStatus.NOT_COMMITTED


def test_seal_publishes_then_reloads_immutable_model_before_closing_export_session(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path) as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        export_session = checkpointed.binding.session
        checkpointed.step_path.write_bytes(b"STEP")
        sealed = rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        immutable_session = sealed.binding.session
        assert type(sealed) is SealedCandidate
        assert immutable_session is not rig.baseline
        assert immutable_session is not active.binding.session
        assert immutable_session is not export_session
        assert immutable_session.payload == export_session.payload
        immutable_path = rig.store.revision_model_path(PROJECT_ID, sealed.revision.id)
        load_index = max(
            index
            for index, call in enumerate(rig.port.calls)
            if call[0] == "load_fcstd" and call[1] == immutable_path
        )
        close_index = next(
            index
            for index, call in enumerate(rig.port.calls)
            if call[0] == "close" and call[1] is export_session
        )
        assert load_index < close_index
        assert rig.port.close_count(export_session) == 1
        assert rig.port.close_count(immutable_session) == 0
        assert rig.store.load_head(PROJECT_ID) == rig.head


def test_seal_close_side_effect_failure_never_issues_sealed_capability(tmp_path: Path) -> None:
    with _rig(tmp_path) as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        export_session = checkpointed.binding.session
        checkpointed.step_path.write_bytes(b"STEP")
        rig.port.close_failures.add(id(export_session))
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        error = caught.value
        assert error.code is CandidateErrorCode.CLEANUP_REQUIRED
        assert error.head_committed is False
        assert error.cleanup_required is True
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(export_session) == 1
        for session in rig.port.created:
            assert rig.port.close_count(session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        with pytest.raises(CandidateError) as stale:
            rig.slot.compare_and_set(rig.baseline_binding, checkpointed.binding)
        _assert_candidate_error(stale, CandidateErrorCode.INVALID_BINDING)
        with pytest.raises(CandidateError) as duplicate:
            rig.coordinator.rollback(candidate=checkpointed, lease=rig.lease)
        _assert_candidate_error(duplicate, CandidateErrorCode.ALREADY_TERMINAL)


@pytest.mark.parametrize(
    "failure",
    ["load", "alias_export", "alias_baseline", "alias_retired_active"],
)
def test_seal_immutable_reload_fault_or_alias_closes_all_owned_sessions_once(
    tmp_path: Path, failure: str
) -> None:
    with _rig(tmp_path, suffix=f"seal-{failure}") as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        checkpointed.step_path.write_bytes(b"STEP")
        if failure == "load":
            rig.port.fail_load_number = 2
            expected = CandidateErrorCode.CAD_FAILURE
        elif failure == "alias_export":
            rig.port.load_aliases[2] = checkpointed.binding.session
            expected = CandidateErrorCode.SESSION_ALIAS
        elif failure == "alias_baseline":
            rig.port.load_aliases[2] = rig.baseline
            expected = CandidateErrorCode.SESSION_ALIAS
        else:
            assert failure == "alias_retired_active"
            rig.port.load_aliases[2] = active.binding.session
            expected = CandidateErrorCode.SESSION_ALIAS
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        _assert_candidate_error(caught, expected)
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(active.binding.session) == 1
        for session in rig.port.created:
            assert rig.port.close_count(session) == 1


@pytest.mark.parametrize("failure", ["durability", "cleanup"])
def test_seal_store_uncertainty_issues_no_handle_and_reconciles_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    with _rig(tmp_path, suffix=f"seal-store-{failure}") as rig:
        active = _begin(rig)
        checkpointed = _checkpoint(rig, active)
        checkpointed.step_path.write_bytes(b"STEP")
        original_seal = LocalRevisionStore.seal_revision
        original_rollback = LocalRevisionStore.rollback_revision
        original_reconcile = LocalRevisionStore.reconcile
        seal_calls = 0
        rollback_calls = 0
        reconcile_calls = 0

        def seal_then_raise(self, project_id, revision_id, lease):
            nonlocal seal_calls
            seal_calls += 1
            original_seal(self, project_id, revision_id, lease)
            if failure == "durability":
                raise RevisionStoreError(
                    RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                    head_committed=False,
                )
            raise RevisionStoreError(RevisionStoreErrorCode.CLEANUP_REQUIRED)

        def counted_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            return original_rollback(self, project_id, revision_id, lease)

        def counted_reconcile(self, project_id, lease):
            nonlocal reconcile_calls
            reconcile_calls += 1
            return original_reconcile(self, project_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "seal_revision", seal_then_raise)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", counted_rollback)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", counted_reconcile)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.seal(candidate=checkpointed, lease=rig.lease)
        error = _assert_candidate_error(caught, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.head_committed is False
        assert error.cleanup_required is False
        assert error.recovery_required is True
        assert seal_calls == 1
        assert rollback_calls + reconcile_calls == 1
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(checkpointed.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        assert sum(1 for call in rig.port.calls[len(before) :] if call[0] == "load_fcstd") == 0
        for session in rig.port.created:
            assert rig.port.close_count(session) == 1
        durable_calls = (rollback_calls, reconcile_calls)
        reconciled = original_reconcile(rig.store, PROJECT_ID, rig.lease)
        assert reconciled.status is ReconciliationStatus.NOT_COMMITTED
        assert (rollback_calls, reconcile_calls) == durable_calls


def test_publish_review_consumes_receipt_then_detaches_sealed_revision_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    consumer_name = _candidate_receipt_consumer_name()
    original_consume = getattr(candidate_module, consumer_name)
    original_rollback = LocalRevisionStore.rollback_revision

    def observed_consume(*args, **kwargs):
        events.append("consume")
        return original_consume(*args, **kwargs)

    def observed_rollback(self, project_id, revision_id, lease):
        events.append("rollback")
        return original_rollback(self, project_id, revision_id, lease)

    monkeypatch.setattr(candidate_module, consumer_name, observed_consume)
    monkeypatch.setattr(LocalRevisionStore, "rollback_revision", observed_rollback)
    with _rig(tmp_path, suffix="publish-review") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        result = rig.coordinator.publish_review(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )

        assert type(result) is CandidateRollbackResult
        assert result.status is CandidateRollbackStatus.NOT_COMMITTED
        assert result.head == rig.head
        assert result.head_committed is False
        assert result.live_binding is rig.baseline_binding
        assert events == ["consume", "rollback"]
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.store.load_revision(PROJECT_ID, sealed.revision.id) == sealed.revision
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0

        repeated = rig.coordinator.publish_review(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert repeated is result
        assert events == ["consume", "rollback"]
        with pytest.raises(ValidationError):
            consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )


def test_reopened_review_has_no_prepared_authority_until_explicit_prepare_then_commits(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="review-reopen-prepare-commit") as rig:
        revision = _detached_review_revision(rig)
        detached_truth = rig.store.reconcile(PROJECT_ID, rig.lease)
        assert detached_truth.status is ReconciliationStatus.NOT_COMMITTED

        reopened = rig.coordinator.reopen_review(
            project_id=PROJECT_ID,
            base_head=rig.head,
            revision=revision,
            lease=rig.lease,
        )

        assert type(reopened) is SealedCandidate
        assert reopened.base_head == rig.head
        assert reopened.revision == revision
        assert reopened.binding.revision_id == revision.id
        assert reopened.binding.session is not rig.baseline
        assert reopened.binding.session.payload == b"review-draft-fcstd"
        assert rig.slot.current() is rig.baseline_binding
        assert rig.store.reconcile(PROJECT_ID, rig.lease) == detached_truth

        compiled, snapshot, receipt, report = _verification_for(
            revision.id,
            revision.manifest_sha256,
        )
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=reopened,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert rig.store.reconcile(PROJECT_ID, rig.lease) == detached_truth
        assert rig.store.load_head(PROJECT_ID) == rig.head

        prepared = rig.coordinator.prepare_review(candidate=reopened, lease=rig.lease)
        assert type(prepared) is SealedCandidate
        assert prepared is not reopened
        assert prepared.revision == revision
        assert prepared.binding is reopened.binding
        result = rig.coordinator.commit(
            candidate=prepared,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )

        assert result.status is CandidateCommitStatus.COMMITTED
        assert result.report == report
        assert result.head.revision_id == revision.id
        assert result.head.manifest_sha256 == revision.manifest_sha256
        assert rig.store.load_head(PROJECT_ID) == result.head
        assert rig.slot.current() is prepared.binding


def test_discard_reopened_review_is_idempotent_close_only_and_never_mutates_head(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="review-discard") as rig:
        revision = _detached_review_revision(rig)
        detached_truth = rig.store.reconcile(PROJECT_ID, rig.lease)
        reopened = rig.coordinator.reopen_review(
            project_id=PROJECT_ID,
            base_head=rig.head,
            revision=revision,
            lease=rig.lease,
        )
        review_session = reopened.binding.session

        assert rig.coordinator.discard_review(candidate=reopened, lease=rig.lease) is None
        assert rig.coordinator.discard_review(candidate=reopened, lease=rig.lease) is None

        assert rig.port.close_count(review_session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.slot.current() is rig.baseline_binding
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.store.reconcile(PROJECT_ID, rig.lease) == detached_truth
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.prepare_review(candidate=reopened, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.ALREADY_TERMINAL)


def test_discard_reopened_review_close_failure_is_cleanup_required_and_not_retried(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="review-discard-cleanup") as rig:
        revision = _detached_review_revision(rig)
        detached_truth = rig.store.reconcile(PROJECT_ID, rig.lease)
        reopened = rig.coordinator.reopen_review(
            project_id=PROJECT_ID,
            base_head=rig.head,
            revision=revision,
            lease=rig.lease,
        )
        review_session = reopened.binding.session
        rig.port.close_failures.add(id(review_session))

        with pytest.raises(CandidateError) as caught:
            rig.coordinator.discard_review(candidate=reopened, lease=rig.lease)

        _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
        assert rig.port.close_count(review_session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.store.reconcile(PROJECT_ID, rig.lease) == detached_truth
        with pytest.raises(CandidateError) as repeated:
            rig.coordinator.discard_review(candidate=reopened, lease=rig.lease)
        _assert_candidate_error(repeated, CandidateErrorCode.ALREADY_TERMINAL)
        assert rig.port.close_count(review_session) == 1


def test_prepare_review_rejects_unrelated_staging_journal_without_mutation_and_closes(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="review-prepare-nonterminal") as rig:
        revision = _detached_review_revision(rig)
        reopened = rig.coordinator.reopen_review(
            project_id=PROJECT_ID,
            base_head=rig.head,
            revision=revision,
            lease=rig.lease,
        )
        review_session = reopened.binding.session
        unrelated_revision = rig.store.begin_revision(PROJECT_ID, rig.head, rig.lease)
        unrelated_model = rig.store.candidate_model_path(
            PROJECT_ID,
            unrelated_revision,
            rig.lease,
        )

        with pytest.raises(CandidateError) as caught:
            rig.coordinator.prepare_review(candidate=reopened, lease=rig.lease)

        _assert_candidate_error(caught, CandidateErrorCode.CONFLICT)
        assert (
            rig.store.candidate_model_path(
                PROJECT_ID,
                unrelated_revision,
                rig.lease,
            )
            == unrelated_model
        )
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.store.load_revision(PROJECT_ID, revision.id) == revision
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(review_session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        rig.store.rollback_revision(PROJECT_ID, unrelated_revision, rig.lease)


def test_prepare_review_durability_uncertainty_settles_and_closes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="review-prepare-durability-uncertain") as rig:
        revision = _detached_review_revision(rig)
        reopened = rig.coordinator.reopen_review(
            project_id=PROJECT_ID,
            base_head=rig.head,
            revision=revision,
            lease=rig.lease,
        )
        review_session = reopened.binding.session
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

        def fail_first_project_fsync(fd):
            nonlocal failed
            if (
                prepared_replaced
                and not failed
                and roles.get(fd) == revisions_module._project_key(PROJECT_ID)
            ):
                failed = True
                raise OSError("SECRET review prepare fsync")
            return original_fsync(fd)

        monkeypatch.setattr(revisions_module.os, "open", tracked_open)
        monkeypatch.setattr(revisions_module.os, "replace", tracked_replace)
        monkeypatch.setattr(revisions_module.os, "fsync", fail_first_project_fsync)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.prepare_review(candidate=reopened, lease=rig.lease)

        _assert_candidate_error(caught, CandidateErrorCode.STORE_FAILURE)
        assert prepared_replaced and failed
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.store.load_revision(PROJECT_ID, revision.id) == revision
        settled = rig.store.reconcile(PROJECT_ID, rig.lease)
        assert settled.status is ReconciliationStatus.NOT_COMMITTED
        assert settled.journal is not None
        assert settled.journal.candidate_revision == revision.id
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(review_session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        with pytest.raises(CandidateError) as repeated:
            rig.coordinator.prepare_review(candidate=reopened, lease=rig.lease)
        _assert_candidate_error(repeated, CandidateErrorCode.ALREADY_TERMINAL)
        assert rig.port.close_count(review_session) == 1


def test_reopen_review_rejects_stale_or_mismatched_durable_identity_before_cad_load(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="review-reopen-mismatch") as rig:
        revision = _detached_review_revision(rig)
        calls_before = tuple(rig.port.calls)
        for base_head, supplied_revision in (
            (replace(rig.head, generation=rig.head.generation + 1), revision),
            (rig.head, replace(revision, manifest_sha256="c" * 64)),
        ):
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.reopen_review(
                    project_id=PROJECT_ID,
                    base_head=base_head,
                    revision=supplied_revision,
                    lease=rig.lease,
                )
            _assert_candidate_error(caught, CandidateErrorCode.CONFLICT)
            assert tuple(rig.port.calls) == calls_before
            assert rig.store.load_head(PROJECT_ID) == rig.head
            assert rig.store.load_revision(PROJECT_ID, revision.id) == revision


def test_reopen_review_rejects_foreign_manager_lease_before_cad_or_store_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="review-reopen-foreign-lease") as rig:
        revision = _detached_review_revision(rig)
        other_manager = ResourceLeaseManager(
            _secure_root(tmp_path / "review-reopen-foreign-locks"),
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        foreign = other_manager.acquire_project_write(PROJECT_ID)
        calls_before = tuple(rig.port.calls)
        _reset_store_mutations(mutations)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.reopen_review(
                    project_id=PROJECT_ID,
                    base_head=rig.head,
                    revision=revision,
                    lease=foreign,
                )
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert not any(mutations.values())
            assert tuple(rig.port.calls) == calls_before
            assert rig.store.load_head(PROJECT_ID) == rig.head
            assert rig.store.load_revision(PROJECT_ID, revision.id) == revision
        finally:
            foreign.release(owner_token=foreign.owner_token)


def test_publish_review_foreign_lease_cannot_burn_receipt_before_store_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="publish-review-foreign-lease") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        other_manager = ResourceLeaseManager(
            _secure_root(tmp_path / "publish-review-foreign-locks"),
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        foreign = other_manager.acquire_project_write(PROJECT_ID)
        _reset_store_mutations(mutations)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.publish_review(
                    candidate=sealed,
                    receipt=receipt,
                    compiled=compiled,
                    snapshot=snapshot,
                    lease=foreign,
                )
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert not any(mutations.values())
            assert rig.store.load_head(PROJECT_ID) == rig.head
        finally:
            foreign.release(owner_token=foreign.owner_token)

        result = rig.coordinator.publish_review(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert result.status is CandidateRollbackStatus.NOT_COMMITTED


def test_review_reopen_is_restart_safe_session_isolated_and_process_local(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="review-restart") as rig:
        revision = _detached_review_revision(rig)
        detached_truth = rig.store.reconcile(PROJECT_ID, rig.lease)
        first = rig.coordinator.reopen_review(
            project_id=PROJECT_ID,
            base_head=rig.head,
            revision=revision,
            lease=rig.lease,
        )
        first_session = first.binding.session
        rig.coordinator.discard_review(candidate=first, lease=rig.lease)
        rig.lease.release(owner_token=rig.lease.owner_token)

        with rig.manager.acquire_project_write(PROJECT_ID) as restarted_lease:
            restarted_port = FakeCadSnapshotPort()
            restarted = CandidateCoordinator(
                store=rig.store,
                snapshot_port=restarted_port,
                session_slot=rig.slot,
            )
            second = restarted.reopen_review(
                project_id=PROJECT_ID,
                base_head=rig.head,
                revision=revision,
                lease=restarted_lease,
            )
            second_session = second.binding.session

            assert second_session is not first_session
            assert second_session is not rig.baseline
            assert second_session.payload == first_session.payload == b"review-draft-fcstd"
            assert rig.slot.current() is rig.baseline_binding
            with pytest.raises(CandidateError) as caught:
                restarted.prepare_review(candidate=first, lease=restarted_lease)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
            assert restarted.discard_review(candidate=second, lease=restarted_lease) is None
            assert restarted_port.close_count(second_session) == 1

        assert rig.port.close_count(first_session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.store.load_head(PROJECT_ID) == rig.head
        with rig.manager.acquire_project_write(PROJECT_ID) as inspection_lease:
            assert rig.store.reconcile(PROJECT_ID, inspection_lease) == detached_truth


def test_successful_commit_consumes_receipt_advances_head_and_transfers_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    consumer_name = _candidate_receipt_consumer_name()
    original_consume = getattr(candidate_module, consumer_name)
    original_commit = LocalRevisionStore.commit_revision

    def observed_consume(*args, **kwargs):
        events.append("consume")
        return original_consume(*args, **kwargs)

    def observed_commit(self, project_id, expected_head, revision_id, lease):
        events.append("commit")
        return original_commit(self, project_id, expected_head, revision_id, lease)

    monkeypatch.setattr(
        candidate_module,
        consumer_name,
        observed_consume,
    )
    monkeypatch.setattr(LocalRevisionStore, "commit_revision", observed_commit)
    with _rig(tmp_path) as rig:
        sealed = _sealed(rig)
        sealed_session = sealed.binding.session
        baseline_rejections: list[CandidateErrorCode] = []

        def try_republish_baseline() -> None:
            try:
                rig.slot.compare_and_set(rig.slot.current(), rig.baseline_binding)
            except CandidateError as exc:
                baseline_rejections.append(exc.code)

        rig.port.close_hooks[id(rig.baseline)] = try_republish_baseline
        result, compiled, snapshot, receipt, report = _commit(rig, sealed)
        assert events == ["consume", "commit"]
        assert type(result) is CandidateCommitResult
        assert result.status is CandidateCommitStatus.COMMITTED
        assert result.head_committed is True
        assert result.slot_promoted is True
        assert result.cleanup_required is False
        assert result.recovery_required is False
        assert result.cleanup_binding is None
        assert result.revision is sealed.revision
        assert result.report == report
        assert result.report is not report
        assert result.head == rig.store.load_head(PROJECT_ID)
        assert result.head.revision_id == sealed.revision.id
        assert result.live_binding is rig.slot.current()
        assert result.live_binding.session is sealed_session
        assert rig.port.close_count(sealed_session) == 0
        assert rig.port.close_count(rig.baseline) == 1
        assert rig.baseline.closed
        assert baseline_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert not sealed_session.closed
        with pytest.raises(CandidateError) as caught:
            rig.slot.compare_and_set(result.live_binding, rig.baseline_binding)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
        with pytest.raises(ValidationError):
            consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )
        with pytest.raises(ValueError):
            replace(result, head_committed=False)
        with pytest.raises(ValueError):
            replace(result, slot_promoted=False)


def test_duplicate_commit_is_cached_and_opposite_terminal_is_rejected(tmp_path: Path) -> None:
    with _rig(tmp_path) as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        first = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        second = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert second is first
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.ALREADY_TERMINAL)
        assert rig.port.close_count(rig.baseline) == 1
        assert rig.port.close_count(sealed.binding.session) == 0


@pytest.mark.parametrize("mode", ["cleanup", "recovery"])
def test_nonclean_commit_duplicate_replays_no_terminal_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    consume_calls = 0
    commit_calls = 0
    reconcile_calls = 0
    cas_calls = 0
    consumer_name = _candidate_receipt_consumer_name()
    original_consume = getattr(candidate_module, consumer_name)
    original_commit = LocalRevisionStore.commit_revision
    original_reconcile = LocalRevisionStore.reconcile
    original_cas = SessionSlot.compare_and_set

    def counted_consume(*args, **kwargs):
        nonlocal consume_calls
        consume_calls += 1
        return original_consume(*args, **kwargs)

    def counted_commit(self, project_id, expected_head, revision_id, lease):
        nonlocal commit_calls
        commit_calls += 1
        return original_commit(self, project_id, expected_head, revision_id, lease)

    def counted_reconcile(self, project_id, lease):
        nonlocal reconcile_calls
        reconcile_calls += 1
        return original_reconcile(self, project_id, lease)

    def scripted_cas(self, expected, replacement):
        nonlocal cas_calls
        cas_calls += 1
        if mode == "recovery":
            return False
        return original_cas(self, expected, replacement)

    monkeypatch.setattr(
        candidate_module,
        consumer_name,
        counted_consume,
    )
    monkeypatch.setattr(LocalRevisionStore, "commit_revision", counted_commit)
    monkeypatch.setattr(LocalRevisionStore, "reconcile", counted_reconcile)
    monkeypatch.setattr(SessionSlot, "compare_and_set", scripted_cas)
    with _rig(tmp_path, suffix=f"duplicate-nonclean-{mode}") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        if mode == "cleanup":
            rig.port.close_failures.add(id(rig.baseline))
        first = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert first.status is (
            CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED
            if mode == "cleanup"
            else CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        )
        counts = (consume_calls, commit_calls, reconcile_calls, cas_calls)
        before = tuple(rig.port.calls)
        try:
            duplicate = rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        except CandidateError as exc:
            assert exc.code is CandidateErrorCode.ALREADY_TERMINAL
        else:
            assert duplicate is first
        assert (consume_calls, commit_calls, reconcile_calls, cas_calls) == counts
        assert tuple(rig.port.calls) == before
        assert rig.store.load_head(PROJECT_ID).revision_id == sealed.revision.id
        if mode == "cleanup":
            assert rig.port.close_count(rig.baseline) == 1
            assert rig.port.close_count(sealed.binding.session) == 0
        else:
            assert rig.port.close_count(rig.baseline) == 0
            assert rig.port.close_count(sealed.binding.session) == 1


def test_replayed_receipt_fails_before_head_and_terminally_rolls_back(tmp_path: Path) -> None:
    with _rig(tmp_path) as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        consume_verification_receipt(
            receipt,
            compiled,
            snapshot,
            candidate_revision=sealed.revision.id,
            manifest_sha256=sealed.revision.manifest_sha256,
        )
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        error = _assert_candidate_error(caught, CandidateErrorCode.RECEIPT_REJECTED)
        assert error.head_committed is False
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0


def test_snapshot_mismatch_reaches_no_head_commit_without_burning_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path) as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, report = _verification(sealed)
        original_rollback = LocalRevisionStore.rollback_revision
        rollback_calls = 0

        def counted_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            return original_rollback(self, project_id, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", counted_rollback)
        wrong_snapshot = ObservationSnapshot(candidate_revision=OTHER_REVISION)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=wrong_snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.RECEIPT_REJECTED)
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rollback_calls == 1
        assert rig.port.close_count(sealed.binding.session) == 1
        with pytest.raises(CandidateError) as duplicate:
            rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        _assert_candidate_error(duplicate, CandidateErrorCode.ALREADY_TERMINAL)
        consumed = consume_verification_receipt(
            receipt,
            compiled,
            snapshot,
            candidate_revision=sealed.revision.id,
            manifest_sha256=sealed.revision.manifest_sha256,
        )
        assert consumed == report
        assert consumed is not report


@pytest.mark.parametrize(
    "kind",
    ["absent", "forged", "wrong_compiled", "wrong_revision", "wrong_manifest", "failed"],
)
def test_receipt_rejection_matrix_is_terminal_and_never_mutates_head(
    tmp_path: Path, kind: str
) -> None:
    with _rig(tmp_path, suffix=f"receipt-{kind}") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, correct_receipt, _report = _verification(sealed)
        receipt: object | None = correct_receipt
        supplied_compiled = compiled
        supplied_snapshot = snapshot
        original_binding = None
        if kind == "absent":
            receipt = None
        elif kind == "forged":
            receipt = object()
        elif kind == "wrong_compiled":
            other_compiled, _other_snapshot, _other_receipt, _other_report = _verification_for(
                sealed.revision.id,
                sealed.revision.manifest_sha256,
                acceptance_id="acceptance-other",
            )
            supplied_compiled = other_compiled
            original_binding = (
                correct_receipt,
                compiled,
                snapshot,
                sealed.revision.id,
                sealed.revision.manifest_sha256,
            )
        elif kind == "wrong_revision":
            other = _verification_for(OTHER_REVISION, MANIFEST, compiled=compiled)
            receipt = other[2]
            original_binding = (receipt, compiled, other[1], OTHER_REVISION, MANIFEST)
        elif kind == "wrong_manifest":
            other = _verification_for(sealed.revision.id, MANIFEST, compiled=compiled)
            receipt = other[2]
            original_binding = (
                receipt,
                compiled,
                other[1],
                sealed.revision.id,
                MANIFEST,
            )
        else:
            failed_snapshot = ObservationSnapshot(
                candidate_revision=sealed.revision.id,
                shapes=(
                    ShapeObservation(
                        target="body",
                        volume_mm3=1.0,
                        area_mm2=1.0,
                        bbox_mm=(1.0, 1.0, 1.0),
                        center_of_mass_mm=(0.5, 0.5, 0.5),
                        valid_shape=True,
                        solid_count=1,
                    ),
                ),
            )
            failed = verify_acceptance(
                compiled,
                failed_snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )
            assert failed.receipt is None
            receipt = failed.receipt
            supplied_snapshot = failed_snapshot

        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_during_receipt_cleanup() -> None:
            try:
                rig.slot.compare_and_set(rig.baseline_binding, sealed.binding)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(sealed.binding.session)] = try_publish_during_receipt_cleanup
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=supplied_compiled,
                snapshot=supplied_snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.RECEIPT_REJECTED)
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 1
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        with pytest.raises(CandidateError) as stale:
            rig.slot.compare_and_set(rig.baseline_binding, sealed.binding)
        _assert_candidate_error(stale, CandidateErrorCode.INVALID_BINDING)
        with pytest.raises(CandidateError) as duplicate:
            rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        _assert_candidate_error(duplicate, CandidateErrorCode.ALREADY_TERMINAL)
        if original_binding is not None:
            (
                original_receipt,
                original_compiled,
                original_snapshot,
                original_revision,
                original_manifest,
            ) = original_binding
            report = consume_verification_receipt(
                original_receipt,
                original_compiled,
                original_snapshot,
                candidate_revision=original_revision,
                manifest_sha256=original_manifest,
            )
            assert report.passed is True


@pytest.mark.parametrize("phase", ["begin", "checkpoint", "seal", "receipt"])
def test_each_accepted_pre_head_fault_performs_its_own_durable_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    with _rig(tmp_path, suffix=f"fault-reconcile-{phase}") as rig:
        candidate = None
        compiled = snapshot = None
        if phase == "begin":
            rig.port.fail_create = True
        else:
            active = _begin(rig)
            candidate = active
            if phase == "checkpoint":
                rig.port.fail_checkpoint = True
            else:
                checkpointed = _checkpoint(rig, active)
                candidate = checkpointed
                if phase == "receipt":
                    checkpointed.step_path.write_bytes(b"STEP")
                    sealed = rig.coordinator.seal(
                        candidate=checkpointed,
                        lease=rig.lease,
                    )
                    candidate = sealed
                    compiled, snapshot, _receipt, _report = _verification(sealed)
        original = LocalRevisionStore.reconcile
        original_rollback = LocalRevisionStore.rollback_revision
        reconcile_calls = 0
        rollback_calls = 0

        def counted(self, project_id, lease):
            nonlocal reconcile_calls
            reconcile_calls += 1
            return original(self, project_id, lease)

        def counted_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            return original_rollback(self, project_id, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "reconcile", counted)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", counted_rollback)
        with pytest.raises(CandidateError):
            if phase == "begin":
                _begin(rig)
            elif phase == "checkpoint":
                assert type(candidate) is ActiveCandidate
                rig.coordinator.checkpoint(candidate=candidate, lease=rig.lease)
            elif phase == "seal":
                assert type(candidate) is CheckpointedCandidate
                rig.coordinator.seal(candidate=candidate, lease=rig.lease)
            else:
                assert type(candidate) is SealedCandidate
                rig.coordinator.commit(
                    candidate=candidate,
                    receipt=object(),
                    compiled=compiled,
                    snapshot=snapshot,
                    lease=rig.lease,
                )
        assert reconcile_calls + rollback_calls == 1
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0


@pytest.mark.parametrize("failure", ["io_result", "generic", "false_committed_metadata"])
def test_real_store_pre_head_linearization_fault_uses_durable_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    with _rig(tmp_path, suffix=f"pre-head-{failure}") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        events: list[str] = []
        original_reconcile = LocalRevisionStore.reconcile
        rollback_calls = 0

        def fail_head_replace(project_fd, raw, token):
            events.append("head_replace")
            if failure == "io_result":
                return (RevisionStoreErrorCode.IO_ERROR, False)
            if failure == "generic":
                raise RuntimeError("generic failure before HEAD replacement")
            raise RevisionStoreError(
                RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=True,
            )

        def counted_reconcile(self, project_id, lease):
            events.append("reconcile")
            return original_reconcile(self, project_id, lease)

        def forbidden_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            raise AssertionError("commit ambiguity must use durable reconcile exactly once")

        monkeypatch.setattr(revisions_module, "_replace_head_record", fail_head_replace)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", counted_reconcile)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        error = _assert_candidate_error(caught, CandidateErrorCode.STORE_FAILURE)
        assert error.head_committed is False
        assert events == ["head_replace", "reconcile"]
        assert rollback_calls == 0
        assert rig.store.load_head(PROJECT_ID) == rig.head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 1
        with pytest.raises(ValidationError):
            consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )
        before_duplicate = tuple(events)
        with pytest.raises(CandidateError) as duplicate:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(duplicate, CandidateErrorCode.ALREADY_TERMINAL)
        with pytest.raises(CandidateError) as opposite:
            rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        _assert_candidate_error(opposite, CandidateErrorCode.ALREADY_TERMINAL)
        assert tuple(events) == before_duplicate


def test_not_committed_reconcile_with_unreadable_exact_head_requires_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="not-committed-head-unreadable") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        original_reconcile = LocalRevisionStore.reconcile
        original_load_head = LocalRevisionStore.load_head
        reconciled = False
        commit_calls = 0
        reconcile_calls = 0
        load_calls = 0
        rollback_calls = 0
        cas_calls = 0

        def fail_before_head(self, project_id, expected_head, revision_id, lease):
            nonlocal commit_calls
            commit_calls += 1
            raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)

        def reconcile_not_committed(self, project_id, lease):
            nonlocal reconciled, reconcile_calls
            reconcile_calls += 1
            result = original_reconcile(self, project_id, lease)
            assert result.status is ReconciliationStatus.NOT_COMMITTED
            reconciled = True
            return result

        def unreadable_exact_head(self, project_id):
            nonlocal load_calls
            load_calls += 1
            if reconciled:
                raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
            return original_load_head(self, project_id)

        def forbidden_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            raise AssertionError("unreadable exact HEAD must not trigger rollback")

        def forbidden_cas(self, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            raise AssertionError("unreadable exact HEAD must not trigger CAS")

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", fail_before_head)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", reconcile_not_committed)
        monkeypatch.setattr(LocalRevisionStore, "load_head", unreadable_exact_head)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        error = _assert_candidate_error(caught, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.recovery_required is True
        assert commit_calls == 1
        assert reconcile_calls == 1
        assert load_calls == 2
        assert rollback_calls == 0
        assert cas_calls == 0
        assert tuple(rig.port.calls) == before
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0
        assert original_load_head(rig.store, PROJECT_ID) == rig.head
        with pytest.raises(ValidationError):
            consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )
        counts = (commit_calls, reconcile_calls, load_calls, rollback_calls, cas_calls)
        with pytest.raises(CandidateError) as duplicate:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(duplicate, CandidateErrorCode.ALREADY_TERMINAL)
        assert (commit_calls, reconcile_calls, load_calls, rollback_calls, cas_calls) == counts


@pytest.mark.parametrize("claimed_head_committed", [True, False])
def test_post_head_durability_metadata_is_ignored_in_favor_of_durable_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claimed_head_committed: bool,
) -> None:
    with _rig(tmp_path, suffix=f"post-head-metadata-{claimed_head_committed}") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        baseline_rejections: list[CandidateErrorCode] = []

        def try_republish_baseline() -> None:
            try:
                rig.slot.compare_and_set(rig.slot.current(), rig.baseline_binding)
            except CandidateError as exc:
                baseline_rejections.append(exc.code)

        rig.port.close_hooks[id(rig.baseline)] = try_republish_baseline
        original = LocalRevisionStore.commit_revision
        original_reconcile = LocalRevisionStore.reconcile
        reconcile_calls = 0
        rollback_calls = 0

        def commit_then_raise(self, project_id, expected_head, revision_id, lease):
            original(self, project_id, expected_head, revision_id, lease)
            raise RevisionStoreError(
                RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=claimed_head_committed,
            )

        def counted_reconcile(self, project_id, lease):
            nonlocal reconcile_calls
            reconcile_calls += 1
            return original_reconcile(self, project_id, lease)

        def forbidden_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            raise AssertionError("post-HEAD ambiguity must never rollback")

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", commit_then_raise)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", counted_reconcile)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
        result = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert result.head_committed is True
        assert result.slot_promoted is True
        assert result.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert result.cleanup_required is False
        assert result.recovery_required is True
        assert result.cleanup_binding is None
        assert reconcile_calls == 1
        assert rollback_calls == 0
        assert rig.store.load_head(PROJECT_ID).revision_id == sealed.revision.id
        assert rig.slot.current().session is sealed.binding.session
        assert rig.port.close_count(rig.baseline) == 1
        assert rig.port.close_count(sealed.binding.session) == 0
        assert baseline_rejections == [CandidateErrorCode.INVALID_BINDING]


def test_generic_exception_after_real_head_commit_is_discovered_from_durable_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path) as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        original = LocalRevisionStore.commit_revision
        original_reconcile = LocalRevisionStore.reconcile
        reconcile_calls = 0
        rollback_calls = 0

        def commit_then_raise_generic(self, project_id, expected_head, revision_id, lease):
            original(self, project_id, expected_head, revision_id, lease)
            raise RuntimeError("generic exception after durable HEAD replacement")

        def counted_reconcile(self, project_id, lease):
            nonlocal reconcile_calls
            reconcile_calls += 1
            return original_reconcile(self, project_id, lease)

        def forbidden_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            raise AssertionError("post-HEAD generic ambiguity must never rollback")

        monkeypatch.setattr(
            LocalRevisionStore,
            "commit_revision",
            commit_then_raise_generic,
        )
        monkeypatch.setattr(LocalRevisionStore, "reconcile", counted_reconcile)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
        result = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert result.head_committed is True
        assert result.slot_promoted is True
        assert result.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert result.cleanup_required is False
        assert result.recovery_required is True
        assert result.cleanup_binding is None
        assert reconcile_calls == 1
        assert rollback_calls == 0
        assert rig.store.load_head(PROJECT_ID).revision_id == sealed.revision.id
        assert rig.slot.current().session is sealed.binding.session
        assert rig.port.close_count(rig.baseline) == 1
        assert rig.port.close_count(sealed.binding.session) == 0


def test_commit_reconcile_and_head_disagreement_fails_recovery_without_guessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="durable-disagreement") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        original_reconcile = LocalRevisionStore.reconcile
        original_load_head = LocalRevisionStore.load_head
        reconciled = False
        cas_calls = 0
        rollback_calls = 0

        def fail_before_head(self, project_id, expected_head, revision_id, lease):
            raise RevisionStoreError(RevisionStoreErrorCode.CONFLICT)

        def reconcile_old(self, project_id, lease):
            nonlocal reconciled
            result = original_reconcile(self, project_id, lease)
            assert result.status is ReconciliationStatus.NOT_COMMITTED
            reconciled = True
            return result

        contradictory_head = ProjectHead(
            project_id=PROJECT_ID,
            generation=rig.head.generation + 1,
            revision_id=sealed.revision.id,
            manifest_sha256=sealed.revision.manifest_sha256,
        )

        def load_contradictory(self, project_id):
            if reconciled:
                return contradictory_head
            return original_load_head(self, project_id)

        def forbidden_cas(self, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            raise AssertionError("durable disagreement must not CAS")

        def forbidden_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            raise AssertionError("durable disagreement must not rollback again")

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", fail_before_head)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", reconcile_old)
        monkeypatch.setattr(LocalRevisionStore, "load_head", load_contradictory)
        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        error = _assert_candidate_error(caught, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.recovery_required is True
        assert cas_calls == 0
        assert rollback_calls == 0
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0
        assert original_load_head(rig.store, PROJECT_ID) == rig.head
        with pytest.raises(ValidationError):
            consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )


def test_committed_reconcile_and_old_head_read_disagreement_never_cas_or_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="reverse-durable-disagreement") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        original_commit = LocalRevisionStore.commit_revision
        original_reconcile = LocalRevisionStore.reconcile
        original_load = LocalRevisionStore.load_head
        reconciled = False
        cas_calls = 0
        rollback_calls = 0

        def commit_then_raise(self, project_id, expected_head, revision_id, lease):
            original_commit(self, project_id, expected_head, revision_id, lease)
            raise RuntimeError("generic post-HEAD exception")

        def reconcile_committed(self, project_id, lease):
            nonlocal reconciled
            result = original_reconcile(self, project_id, lease)
            assert result.status is ReconciliationStatus.COMMITTED
            reconciled = True
            return result

        def stale_head(self, project_id):
            if reconciled:
                return rig.head
            return original_load(self, project_id)

        def forbidden_cas(self, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            raise AssertionError("disagreeing durable evidence must not CAS")

        def forbidden_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            raise AssertionError("committed durable evidence must never rollback")

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", commit_then_raise)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", reconcile_committed)
        monkeypatch.setattr(LocalRevisionStore, "load_head", stale_head)
        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
        result = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert result.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert result.head_committed is True
        assert result.slot_promoted is None
        assert result.recovery_required is True
        assert cas_calls == 0
        assert rollback_calls == 0
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0
        assert original_load(rig.store, PROJECT_ID).revision_id == sealed.revision.id


def test_commit_reconcile_unreadable_fails_recovery_without_guessing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit_calls = 0
    reconcile_calls = 0
    rollback_calls = 0
    cas_calls = 0

    def failed_commit(self, project_id, expected_head, revision_id, lease):
        nonlocal commit_calls
        commit_calls += 1
        raise RuntimeError("commit outcome is unreadable")

    def unreadable_reconcile(self, project_id, lease):
        nonlocal reconcile_calls
        reconcile_calls += 1
        raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)

    def forbidden_rollback(self, project_id, revision_id, lease):
        nonlocal rollback_calls
        rollback_calls += 1
        raise AssertionError("ambiguous commit must not rollback")

    def forbidden_cas(self, expected, replacement):
        nonlocal cas_calls
        cas_calls += 1
        raise AssertionError("unreadable durable truth must not CAS")

    monkeypatch.setattr(LocalRevisionStore, "commit_revision", failed_commit)
    monkeypatch.setattr(LocalRevisionStore, "reconcile", unreadable_reconcile)
    monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
    monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
    with _rig(tmp_path, suffix="commit-reconcile-unreadable") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        error = _assert_candidate_error(caught, CandidateErrorCode.RECOVERY_REQUIRED)
        assert error.recovery_required is True
        assert commit_calls == 1
        assert reconcile_calls == 1
        assert rollback_calls == 0
        assert cas_calls == 0
        assert tuple(rig.port.calls) == before
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0
        with pytest.raises(ValidationError):
            consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )
        counts = (commit_calls, reconcile_calls, rollback_calls, cas_calls)
        with pytest.raises(CandidateError) as duplicate:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(duplicate, CandidateErrorCode.ALREADY_TERMINAL)
        assert (commit_calls, reconcile_calls, rollback_calls, cas_calls) == counts


def test_post_head_exact_head_unreadable_returns_recovery_without_cas_or_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_commit = LocalRevisionStore.commit_revision
    original_reconcile = LocalRevisionStore.reconcile
    original_load = LocalRevisionStore.load_head
    reconciled = False
    commit_calls = 0
    reconcile_calls = 0
    rollback_calls = 0
    cas_calls = 0

    def commit_then_raise(self, project_id, expected_head, revision_id, lease):
        nonlocal commit_calls
        commit_calls += 1
        original_commit(self, project_id, expected_head, revision_id, lease)
        raise RuntimeError("generic exception after HEAD replacement")

    def counted_reconcile(self, project_id, lease):
        nonlocal reconcile_calls, reconciled
        reconcile_calls += 1
        result = original_reconcile(self, project_id, lease)
        assert result.status is ReconciliationStatus.COMMITTED
        reconciled = True
        return result

    def unreadable_head(self, project_id):
        if reconciled:
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        return original_load(self, project_id)

    def forbidden_rollback(self, project_id, revision_id, lease):
        nonlocal rollback_calls
        rollback_calls += 1
        raise AssertionError("post-HEAD ambiguity must not rollback")

    def forbidden_cas(self, expected, replacement):
        nonlocal cas_calls
        cas_calls += 1
        raise AssertionError("unreadable exact HEAD must not CAS")

    monkeypatch.setattr(LocalRevisionStore, "commit_revision", commit_then_raise)
    monkeypatch.setattr(LocalRevisionStore, "reconcile", counted_reconcile)
    monkeypatch.setattr(LocalRevisionStore, "load_head", unreadable_head)
    monkeypatch.setattr(LocalRevisionStore, "rollback_revision", forbidden_rollback)
    monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
    with _rig(tmp_path, suffix="post-head-read-unreadable") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        before = tuple(rig.port.calls)
        result = rig.coordinator.commit(
            candidate=sealed,
            receipt=receipt,
            compiled=compiled,
            snapshot=snapshot,
            lease=rig.lease,
        )
        assert result.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert result.head_committed is True
        assert result.slot_promoted is None
        assert result.cleanup_required is False
        assert result.recovery_required is True
        assert result.cleanup_binding is None
        assert result.head.revision_id == sealed.revision.id
        assert commit_calls == 1
        assert reconcile_calls == 1
        assert rollback_calls == 0
        assert cas_calls == 0
        assert tuple(rig.port.calls) == before
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0
        assert original_load(rig.store, PROJECT_ID).revision_id == sealed.revision.id
        with pytest.raises(ValidationError):
            consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )


def test_post_head_displaced_baseline_close_failure_stays_cleanup_not_rollback(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path) as rig:
        sealed = _sealed(rig)
        rig.port.close_failures.add(id(rig.baseline))
        result, *_unused = _commit(rig, sealed)
        assert result.status is CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED
        assert result.head_committed is True
        assert result.slot_promoted is True
        assert result.cleanup_required is True
        assert result.recovery_required is False
        assert result.cleanup_binding is None
        assert rig.store.load_head(PROJECT_ID).revision_id == sealed.revision.id
        assert rig.slot.current().session is sealed.binding.session
        assert rig.port.close_count(rig.baseline) == 1
        assert rig.port.close_count(sealed.binding.session) == 0
        calls_before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
        assert error.cleanup_required is True
        assert tuple(rig.port.calls) == calls_before
        assert rig.port.close_count(rig.baseline) == 1


@pytest.mark.parametrize(
    ("mode", "promoted", "baseline_closes", "sealed_closes", "cleanup_retained"),
    [
        ("false_baseline", False, 0, 1, False),
        ("false_promoted", True, 1, 0, False),
        ("raise_baseline", False, 0, 1, False),
        ("raise_promoted", True, 1, 0, False),
        ("true_without_publish", None, 0, 0, True),
        ("raise_third", None, 0, 0, True),
        ("false_third", None, 0, 0, True),
        ("true_third", None, 0, 0, True),
        ("current_unreadable", None, 0, 0, False),
        ("false_current_unreadable", None, 0, 0, False),
        ("raise_current_unreadable", None, 0, 0, False),
    ],
)
def test_post_head_cas_result_and_identity_readback_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    promoted: bool | None,
    baseline_closes: int,
    sealed_closes: int,
    cleanup_retained: bool,
) -> None:
    with _rig(tmp_path, suffix=f"cas-{mode}") as rig:
        sealed = _sealed(rig)
        sealed_session = sealed.binding.session
        original = SessionSlot.compare_and_set
        original_current = SessionSlot.current
        cas_calls = 0
        readback_unreadable = False
        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_candidate_during_close() -> None:
            try:
                original(rig.slot, rig.baseline_binding, sealed.binding)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        if mode in {"false_baseline", "raise_baseline"}:
            rig.port.close_hooks[id(sealed_session)] = try_publish_candidate_during_close

        def injected(slot, expected, replacement):
            nonlocal cas_calls, readback_unreadable
            cas_calls += 1
            if mode == "false_baseline":
                return False
            if mode == "false_promoted":
                assert original(slot, expected, replacement) is True
                return False
            if mode == "raise_baseline":
                raise RuntimeError("CAS failed before publish")
            if mode == "raise_promoted":
                assert original(slot, expected, replacement) is True
                raise RuntimeError("CAS failed after publish")
            if mode == "true_without_publish":
                return True
            if mode == "current_unreadable":
                assert original(slot, expected, replacement) is True
                readback_unreadable = True
                return True
            if mode == "false_current_unreadable":
                readback_unreadable = True
                return False
            if mode == "raise_current_unreadable":
                readback_unreadable = True
                raise RuntimeError("CAS and readback unavailable")
            third = SessionBinding(
                project_id=PROJECT_ID,
                revision_id=OTHER_REVISION,
                session=FakeSession("third", b"third"),
            )
            assert original(slot, expected, third) is True
            if mode == "raise_third":
                raise RuntimeError("CAS failed with divergent current")
            return mode == "true_third"

        def scripted_current(slot):
            if readback_unreadable:
                raise RuntimeError("slot readback unavailable")
            return original_current(slot)

        monkeypatch.setattr(SessionSlot, "compare_and_set", injected)
        monkeypatch.setattr(SessionSlot, "current", scripted_current)
        result, *_unused = _commit(rig, sealed)
        assert result.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert result.head_committed is True
        assert result.slot_promoted is promoted
        assert result.recovery_required is True
        assert result.cleanup_required is cleanup_retained
        assert result.cleanup_binding is (sealed.binding if cleanup_retained else None)
        assert cas_calls == 1
        assert rig.store.load_head(PROJECT_ID).revision_id == sealed.revision.id
        assert rig.port.close_count(rig.baseline) == baseline_closes
        assert rig.port.close_count(sealed_session) == sealed_closes
        assert retirement_rejections == (
            [CandidateErrorCode.INVALID_BINDING]
            if mode in {"false_baseline", "raise_baseline"}
            else []
        )
        if cleanup_retained:
            monkeypatch.setattr(SessionSlot, "compare_and_set", original)
            monkeypatch.setattr(SessionSlot, "current", original_current)
            current = rig.slot.current()
            with pytest.raises(CandidateError) as caught:
                rig.slot.compare_and_set(current, result.cleanup_binding)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
            assert rig.slot.current() is current
        if promoted is True:
            assert rig.slot.current().session is sealed_session
        elif promoted is False:
            assert rig.slot.current() is rig.baseline_binding
        elif mode in {
            "current_unreadable",
            "false_current_unreadable",
            "raise_current_unreadable",
        }:
            assert result.live_binding is None
        else:
            assert result.live_binding is rig.slot.current()


def test_post_head_unpromoted_candidate_close_failure_is_cleanup_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="post-head-candidate-close-failure") as rig:
        sealed = _sealed(rig)
        original_cas = SessionSlot.compare_and_set
        rig.port.close_failures.add(id(sealed.binding.session))
        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_candidate_during_failed_close() -> None:
            try:
                original_cas(rig.slot, rig.baseline_binding, sealed.binding)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(sealed.binding.session)] = try_publish_candidate_during_failed_close

        def refuse_promotion(slot, expected, replacement):
            return False

        monkeypatch.setattr(SessionSlot, "compare_and_set", refuse_promotion)
        result, *_unused = _commit(rig, sealed)
        assert result.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert result.head_committed is True
        assert result.slot_promoted is False
        assert result.cleanup_required is True
        assert result.recovery_required is True
        assert result.cleanup_binding is None
        assert result.live_binding is rig.baseline_binding
        assert rig.store.load_head(PROJECT_ID).revision_id == sealed.revision.id
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 1
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert rig.port.close_count(rig.baseline) == 0

        monkeypatch.setattr(SessionSlot, "compare_and_set", original_cas)
        calls_before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
        assert error.cleanup_required is True
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        assert tuple(rig.port.calls) == calls_before


def test_retained_commit_cleanup_binding_is_retired_and_reconciled_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="commit-retained-cleanup") as rig:
        sealed = _sealed(rig)
        original_cas = SessionSlot.compare_and_set

        def true_without_publish(slot, expected, replacement):
            return True

        monkeypatch.setattr(SessionSlot, "compare_and_set", true_without_publish)
        result, *_unused = _commit(rig, sealed)
        assert result.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert result.cleanup_required is True
        assert result.cleanup_binding is sealed.binding
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 0

        monkeypatch.setattr(SessionSlot, "compare_and_set", original_cas)
        with pytest.raises(CandidateError) as caught:
            rig.slot.compare_and_set(rig.baseline_binding, result.cleanup_binding)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
        assert rig.slot.current() is rig.baseline_binding

        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_during_cleanup_close() -> None:
            try:
                original_cas(rig.slot, rig.slot.current(), result.cleanup_binding)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(sealed.binding.session)] = try_publish_during_cleanup_close
        cas_calls = 0

        def counted_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            return original_cas(slot, expected, replacement)

        monkeypatch.setattr(SessionSlot, "compare_and_set", counted_cas)
        loads_before = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        recovered = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert recovered.status is CandidateReconcileStatus.COMMITTED
        assert recovered.slot_promoted is True
        assert rig.slot.current().revision_id == sealed.revision.id
        assert rig.slot.current().session is not sealed.binding.session
        assert rig.port.close_count(sealed.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 1
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert cas_calls == 1
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == (loads_before + 1)

        repeated = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert repeated.status is CandidateReconcileStatus.COMMITTED
        assert rig.port.close_count(sealed.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 1
        assert cas_calls == 1
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == (loads_before + 1)


def test_closed_candidate_binding_cannot_be_published_later_through_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="retired-binding") as rig:
        sealed = _sealed(rig)
        original = SessionSlot.compare_and_set

        def fail_candidate_cas(slot, expected, replacement):
            return False

        monkeypatch.setattr(SessionSlot, "compare_and_set", fail_candidate_cas)
        result, *_unused = _commit(rig, sealed)
        assert result.slot_promoted is False
        assert rig.port.close_count(sealed.binding.session) == 1
        monkeypatch.setattr(SessionSlot, "compare_and_set", original)
        with pytest.raises(CandidateError) as caught:
            rig.slot.compare_and_set(rig.baseline_binding, sealed.binding)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
        assert rig.slot.current() is rig.baseline_binding


@pytest.mark.parametrize("stage", ["active", "checkpointed", "sealed"])
def test_explicit_rollback_closes_only_candidate_sessions_once(tmp_path: Path, stage: str) -> None:
    with _rig(tmp_path, suffix=f"rollback-{stage}") as rig:
        active = _begin(rig)
        candidate: ActiveCandidate | CheckpointedCandidate | SealedCandidate = active
        owned = [active.binding.session]
        if stage in {"checkpointed", "sealed"}:
            checkpointed = _checkpoint(rig, active)
            candidate = checkpointed
            owned.append(checkpointed.binding.session)
        if stage == "sealed":
            assert type(candidate) is CheckpointedCandidate
            candidate.step_path.write_bytes(b"STEP")
            sealed = rig.coordinator.seal(candidate=candidate, lease=rig.lease)
            candidate = sealed
            owned.append(sealed.binding.session)
        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_during_rollback_close() -> None:
            try:
                rig.slot.compare_and_set(rig.baseline_binding, candidate.binding)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(candidate.binding.session)] = try_publish_during_rollback_close
        result = rig.coordinator.rollback(candidate=candidate, lease=rig.lease)
        assert type(result) is CandidateRollbackResult
        assert result.status is CandidateRollbackStatus.NOT_COMMITTED
        assert result.head_committed is False
        assert result.slot_promoted is False
        assert result.cleanup_required is False
        assert result.recovery_required is False
        assert result.head == rig.head
        assert result.live_binding is rig.baseline_binding
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        for session in owned:
            assert rig.port.close_count(session) == 1
        duplicate = rig.coordinator.rollback(candidate=candidate, lease=rig.lease)
        assert duplicate is result
        with pytest.raises(CandidateError) as caught:
            rig.slot.compare_and_set(rig.baseline_binding, candidate.binding)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)


def test_explicit_rollback_close_failure_is_cached_cleanup_required(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="rollback-close-failure") as rig:
        active = _begin(rig)
        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_during_failed_close() -> None:
            try:
                rig.slot.compare_and_set(rig.baseline_binding, active.binding)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(active.binding.session)] = try_publish_during_failed_close
        rig.port.close_failures.add(id(active.binding.session))
        result = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        assert result.status is CandidateRollbackStatus.CLEANUP_REQUIRED
        assert result.head_committed is False
        assert result.slot_promoted is False
        assert result.cleanup_required is True
        assert result.recovery_required is False
        assert result.cleanup_binding is None
        assert result.head == rig.head
        assert result.live_binding is rig.baseline_binding
        assert result.reconciliation.status is ReconciliationStatus.NOT_COMMITTED
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert rig.port.close_count(active.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0

        duplicate = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        assert duplicate is result
        assert rig.port.close_count(active.binding.session) == 1
        calls_before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
        assert error.cleanup_required is True
        assert tuple(rig.port.calls) == calls_before
        assert rig.port.close_count(active.binding.session) == 1


@pytest.mark.parametrize("failure", ["generic", "cleanup", "durability"])
def test_rollback_store_ambiguity_uses_one_durable_reconcile_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    with _rig(tmp_path, suffix=f"rollback-store-{failure}") as rig:
        active = _begin(rig)
        original_rollback = LocalRevisionStore.rollback_revision
        original_reconcile = LocalRevisionStore.reconcile
        rollback_calls = 0
        reconcile_calls = 0

        def rollback_then_raise(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            original_rollback(self, project_id, revision_id, lease)
            if failure == "generic":
                raise RuntimeError("generic exception after durable rollback")
            if failure == "cleanup":
                raise RevisionStoreError(RevisionStoreErrorCode.CLEANUP_REQUIRED)
            raise RevisionStoreError(
                RevisionStoreErrorCode.DURABILITY_UNCERTAIN,
                head_committed=True,
            )

        def counted_reconcile(self, project_id, lease):
            nonlocal reconcile_calls
            reconcile_calls += 1
            return original_reconcile(self, project_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", rollback_then_raise)
        monkeypatch.setattr(LocalRevisionStore, "reconcile", counted_reconcile)
        result = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        assert result.status is CandidateRollbackStatus.RECOVERY_REQUIRED
        assert result.head_committed is False
        assert result.slot_promoted is False
        assert result.cleanup_required is False
        assert result.recovery_required is True
        assert result.cleanup_binding is None
        assert result.head == rig.head
        assert result.live_binding is rig.baseline_binding
        assert result.reconciliation.status is ReconciliationStatus.NOT_COMMITTED
        assert rollback_calls == 1
        assert reconcile_calls == 1
        assert rig.port.close_count(active.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0

        duplicate = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        assert duplicate is result
        assert rollback_calls == 1
        assert reconcile_calls == 1
        assert rig.port.close_count(active.binding.session) == 1


def test_rollback_rejects_same_issuer_replacement_lease_before_store_or_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="rollback-replacement-lease") as rig:
        active = _begin(rig)
        original_rollback = LocalRevisionStore.rollback_revision
        rollback_calls = 0

        def counted_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            return original_rollback(self, project_id, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", counted_rollback)
        before = tuple(rig.port.calls)
        rig.lease.release(owner_token=rig.lease.owner_token)
        replacement_lease = rig.manager.acquire_project_write(PROJECT_ID)
        _reset_store_mutations(mutations)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.rollback(candidate=active, lease=replacement_lease)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert rollback_calls == 0
            assert not any(mutations.values())
            assert tuple(rig.port.calls) == before
            assert rig.port.close_count(active.binding.session) == 0

            recovered = rig.coordinator.reconcile(
                project_id=PROJECT_ID,
                lease=replacement_lease,
            )
            assert recovered.status is CandidateReconcileStatus.NOT_COMMITTED
            assert rollback_calls == 0
            assert rig.port.close_count(active.binding.session) == 1
        finally:
            replacement_lease.release(owner_token=replacement_lease.owner_token)


@pytest.mark.parametrize("stage", ["active", "checkpointed", "sealed"])
@pytest.mark.parametrize("drift", ["head", "slot"])
def test_rollback_rejects_unrelated_head_or_slot_drift_before_store_or_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    drift: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"rollback-{stage}-{drift}-drift") as rig:
        active = _begin(rig)
        candidate: ActiveCandidate | CheckpointedCandidate | SealedCandidate = active
        if stage in {"checkpointed", "sealed"}:
            candidate = _checkpoint(rig, active)
        if stage == "sealed":
            assert type(candidate) is CheckpointedCandidate
            candidate.step_path.write_bytes(b"ISO-10303-21;END-ISO-10303-21;")
            candidate = rig.coordinator.seal(candidate=candidate, lease=rig.lease)
        before = tuple(rig.port.calls)
        original_load = LocalRevisionStore.load_head
        third = None
        if drift == "head":
            changed = ProjectHead(
                project_id=PROJECT_ID,
                generation=rig.head.generation + 1,
                revision_id=OTHER_REVISION,
                manifest_sha256=MANIFEST,
            )

            def changed_head(self, project_id):
                return changed

            monkeypatch.setattr(LocalRevisionStore, "load_head", changed_head)
        else:
            third = SessionBinding(
                project_id=PROJECT_ID,
                revision_id=OTHER_REVISION,
                session=FakeSession("third", b"third"),
            )
            assert rig.slot.compare_and_set(rig.baseline_binding, third)

        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.rollback(candidate=candidate, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.CONFLICT)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        assert rig.port.close_count(candidate.binding.session) == 0
        assert rig.port.close_count(rig.baseline) == 0

        if third is not None:
            assert rig.slot.compare_and_set(third, rig.baseline_binding)
        else:
            monkeypatch.setattr(LocalRevisionStore, "load_head", original_load)
        recovered = rig.coordinator.rollback(candidate=candidate, lease=rig.lease)
        assert recovered.status is CandidateRollbackStatus.NOT_COMMITTED


@pytest.mark.parametrize("kind", ["foreign", "released"])
def test_rollback_rejects_foreign_or_released_captured_lease_without_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"rollback-invalid-lease-{kind}") as rig:
        active = _begin(rig)
        supplied = rig.lease
        other_lease = None
        if kind == "foreign":
            other_manager = ResourceLeaseManager(
                _secure_root(tmp_path / "rollback-invalid-lease-other-locks"),
                trust=LeaseRootTrust.TRUSTED_LOCAL,
            )
            other_lease = other_manager.acquire_project_write(PROJECT_ID)
            supplied = other_lease
        else:
            rig.lease.release(owner_token=rig.lease.owner_token)
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.rollback(candidate=active, lease=supplied)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert not any(mutations.values())
            assert tuple(rig.port.calls) == before
            assert rig.port.close_count(active.binding.session) == 0
            assert rig.slot.current() is rig.baseline_binding
        finally:
            if other_lease is not None:
                other_lease.release(owner_token=other_lease.owner_token)

        if kind == "foreign":
            recovered = rig.coordinator.rollback(candidate=active, lease=rig.lease)
            assert recovered.status is CandidateRollbackStatus.NOT_COMMITTED
        else:
            recovery_lease = rig.manager.acquire_project_write(PROJECT_ID)
            try:
                recovered = rig.coordinator.reconcile(
                    project_id=PROJECT_ID,
                    lease=recovery_lease,
                )
                assert recovered.status is CandidateReconcileStatus.NOT_COMMITTED
            finally:
                recovery_lease.release(owner_token=recovery_lease.owner_token)


def test_released_or_wrong_lease_is_rejected_before_cad_or_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path) as rig:
        active = _begin(rig)
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        other_manager = ResourceLeaseManager(
            _secure_root(tmp_path / "other-locks"),
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        other_lease = other_manager.acquire_project_write(PROJECT_ID)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.checkpoint(candidate=active, lease=other_lease)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert not any(mutations.values())
            assert tuple(rig.port.calls) == before
        finally:
            other_lease.release(owner_token=other_lease.owner_token)

        rig.lease.release(owner_token=rig.lease.owner_token)
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.checkpoint(candidate=active, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before


@pytest.mark.parametrize("precondition", ["wrong_lease", "slot_changed"])
def test_commit_preconditions_reject_before_receipt_or_head_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    precondition: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"commit-precondition-{precondition}") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, report = _verification(sealed)
        commit_calls = 0
        original_commit = LocalRevisionStore.commit_revision

        def counted_commit(self, project_id, expected_head, revision_id, lease):
            nonlocal commit_calls
            commit_calls += 1
            return original_commit(self, project_id, expected_head, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", counted_commit)
        used_lease = rig.lease
        other_lease = None
        third = None
        if precondition == "wrong_lease":
            other_manager = ResourceLeaseManager(
                _secure_root(tmp_path / "commit-precondition-other-locks"),
                trust=LeaseRootTrust.TRUSTED_LOCAL,
            )
            other_lease = other_manager.acquire_project_write(PROJECT_ID)
            used_lease = other_lease
            expected_code = CandidateErrorCode.INVALID_LEASE
        else:
            third = SessionBinding(
                project_id=PROJECT_ID,
                revision_id=OTHER_REVISION,
                session=FakeSession("third", b"third"),
            )
            assert rig.slot.compare_and_set(rig.baseline_binding, third)
            expected_code = CandidateErrorCode.CONFLICT
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.commit(
                    candidate=sealed,
                    receipt=receipt,
                    compiled=compiled,
                    snapshot=snapshot,
                    lease=used_lease,
                )
            _assert_candidate_error(caught, expected_code)
            assert commit_calls == 0
            assert not any(mutations.values())
            assert tuple(rig.port.calls) == before
            assert rig.store.load_head(PROJECT_ID) == rig.head
            assert rig.port.close_count(sealed.binding.session) == 0
            consumed = consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )
            assert consumed == report
            assert consumed is not report
        finally:
            if other_lease is not None:
                other_lease.release(owner_token=other_lease.owner_token)
        if third is not None:
            assert rig.slot.compare_and_set(third, rig.baseline_binding)
        rolled_back = rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED


def test_released_lease_commit_precondition_does_not_burn_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="commit-released-lease") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, report = _verification(sealed)
        commit_calls = 0

        def forbidden_commit(self, project_id, expected_head, revision_id, lease):
            nonlocal commit_calls
            commit_calls += 1
            raise AssertionError("commit must not run for a released lease")

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", forbidden_commit)
        rig.lease.release(owner_token=rig.lease.owner_token)
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
        assert commit_calls == 0
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        assert rig.store.load_head(PROJECT_ID) == rig.head
        recovery_lease = rig.manager.acquire_project_write(PROJECT_ID)
        _reset_store_mutations(mutations)
        with pytest.raises(CandidateError) as replacement_lease_error:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=recovery_lease,
            )
        _assert_candidate_error(
            replacement_lease_error,
            CandidateErrorCode.INVALID_LEASE,
        )
        assert commit_calls == 0
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        consumed = consume_verification_receipt(
            receipt,
            compiled,
            snapshot,
            candidate_revision=sealed.revision.id,
            manifest_sha256=sealed.revision.manifest_sha256,
        )
        assert consumed == report
        assert consumed is not report
        try:
            recovered = rig.coordinator.reconcile(
                project_id=PROJECT_ID,
                lease=recovery_lease,
            )
            assert recovered.status is CandidateReconcileStatus.NOT_COMMITTED
            assert rig.port.close_count(sealed.binding.session) == 1
        finally:
            recovery_lease.release(owner_token=recovery_lease.owner_token)


def test_changed_head_is_rejected_before_receipt_consumption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="commit-head-changed") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, report = _verification(sealed)
        original_load = LocalRevisionStore.load_head
        changed_head = ProjectHead(
            project_id=PROJECT_ID,
            generation=rig.head.generation + 1,
            revision_id=OTHER_REVISION,
            manifest_sha256=MANIFEST,
        )
        commit_calls = 0

        def changed(self, project_id):
            return changed_head

        def forbidden_commit(self, project_id, expected_head, revision_id, lease):
            nonlocal commit_calls
            commit_calls += 1
            raise AssertionError("changed HEAD must reject before commit")

        monkeypatch.setattr(LocalRevisionStore, "load_head", changed)
        monkeypatch.setattr(LocalRevisionStore, "commit_revision", forbidden_commit)
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.commit(
                candidate=sealed,
                receipt=receipt,
                compiled=compiled,
                snapshot=snapshot,
                lease=rig.lease,
            )
        _assert_candidate_error(caught, CandidateErrorCode.CONFLICT)
        assert commit_calls == 0
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == before
        assert rig.port.close_count(sealed.binding.session) == 0
        consumed = consume_verification_receipt(
            receipt,
            compiled,
            snapshot,
            candidate_revision=sealed.revision.id,
            manifest_sha256=sealed.revision.manifest_sha256,
        )
        assert consumed == report
        assert consumed is not report
        monkeypatch.setattr(LocalRevisionStore, "load_head", original_load)
        assert rig.store.load_head(PROJECT_ID) == rig.head
        rolled_back = rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        assert rolled_back.status is CandidateRollbackStatus.NOT_COMMITTED


def test_concurrent_duplicate_commit_performs_terminal_side_effects_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path) as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        original = LocalRevisionStore.commit_revision
        commit_calls = 0

        def counted(self, project_id, expected_head, revision_id, lease):
            nonlocal commit_calls
            commit_calls += 1
            return original(self, project_id, expected_head, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", counted)
        barrier = threading.Barrier(3)
        outcomes: list[object] = []
        unexpected: list[BaseException] = []

        def worker() -> None:
            barrier.wait()
            try:
                outcomes.append(
                    rig.coordinator.commit(
                        candidate=sealed,
                        receipt=receipt,
                        compiled=compiled,
                        snapshot=snapshot,
                        lease=rig.lease,
                    )
                )
            except CandidateError as exc:
                outcomes.append(exc)
            except BaseException as exc:
                unexpected.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert unexpected == []
        assert len(outcomes) == 2
        results = [item for item in outcomes if type(item) is CandidateCommitResult]
        errors = [item for item in outcomes if type(item) is CandidateError]
        assert len(results) in {1, 2}
        if len(results) == 2:
            assert results[0] is results[1]
        assert len(errors) == 2 - len(results)
        assert all(
            error.code
            in {
                CandidateErrorCode.ALREADY_TERMINAL,
                CandidateErrorCode.TERMINAL_IN_PROGRESS,
            }
            for error in errors
        )
        assert commit_calls == 1
        assert rig.port.close_count(rig.baseline) == 1


def test_concurrent_commit_and_rollback_reserve_one_terminal_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="commit-vs-rollback") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, _report = _verification(sealed)
        original_commit = LocalRevisionStore.commit_revision
        original_rollback = LocalRevisionStore.rollback_revision
        commit_calls = 0
        rollback_calls = 0

        def counted_commit(self, project_id, expected_head, revision_id, lease):
            nonlocal commit_calls
            commit_calls += 1
            return original_commit(self, project_id, expected_head, revision_id, lease)

        def counted_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            return original_rollback(self, project_id, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", counted_commit)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", counted_rollback)
        barrier = threading.Barrier(3)
        outcomes: list[object] = []

        def commit_worker() -> None:
            barrier.wait()
            try:
                outcomes.append(
                    rig.coordinator.commit(
                        candidate=sealed,
                        receipt=receipt,
                        compiled=compiled,
                        snapshot=snapshot,
                        lease=rig.lease,
                    )
                )
            except CandidateError as exc:
                outcomes.append(exc)

        def rollback_worker() -> None:
            barrier.wait()
            try:
                outcomes.append(rig.coordinator.rollback(candidate=sealed, lease=rig.lease))
            except CandidateError as exc:
                outcomes.append(exc)

        threads = [
            threading.Thread(target=commit_worker),
            threading.Thread(target=rollback_worker),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert len(outcomes) == 2
        terminal_results = [
            item
            for item in outcomes
            if type(item) in {CandidateCommitResult, CandidateRollbackResult}
        ]
        errors = [item for item in outcomes if type(item) is CandidateError]
        assert len(terminal_results) == 1
        assert len(errors) == 1
        assert errors[0].code in {
            CandidateErrorCode.ALREADY_TERMINAL,
            CandidateErrorCode.TERMINAL_IN_PROGRESS,
        }
        assert commit_calls + rollback_calls == 1
        if type(terminal_results[0]) is CandidateCommitResult:
            assert rig.port.close_count(rig.baseline) == 1
            assert rig.port.close_count(sealed.binding.session) == 0
        else:
            assert rig.port.close_count(rig.baseline) == 0
            assert rig.port.close_count(sealed.binding.session) == 1


def test_concurrent_duplicate_rollback_is_cached_and_closes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="rollback-vs-rollback") as rig:
        sealed = _sealed(rig)
        original = LocalRevisionStore.rollback_revision
        rollback_calls = 0

        def counted(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            return original(self, project_id, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", counted)
        barrier = threading.Barrier(3)
        outcomes: list[object] = []
        unexpected: list[BaseException] = []

        def worker() -> None:
            barrier.wait()
            try:
                outcomes.append(rig.coordinator.rollback(candidate=sealed, lease=rig.lease))
            except CandidateError as exc:
                outcomes.append(exc)
            except BaseException as exc:
                unexpected.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert unexpected == []
        assert len(outcomes) == 2
        results = [item for item in outcomes if type(item) is CandidateRollbackResult]
        errors = [item for item in outcomes if type(item) is CandidateError]
        assert len(results) in {1, 2}
        if len(results) == 2:
            assert results[0] is results[1]
        assert len(errors) == 2 - len(results)
        assert all(
            error.code
            in {
                CandidateErrorCode.ALREADY_TERMINAL,
                CandidateErrorCode.TERMINAL_IN_PROGRESS,
            }
            for error in errors
        )
        assert rollback_calls == 1
        assert rig.port.close_count(sealed.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0


@pytest.mark.parametrize("winner", ["commit", "rollback"])
def test_terminal_winner_is_blocked_inside_first_store_side_effect_while_loser_enters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    winner: str,
) -> None:
    with _rig(tmp_path, suffix=f"forced-terminal-race-{winner}") as rig:
        sealed = _sealed(rig)
        compiled, snapshot, receipt, report = _verification(sealed)
        original_commit = LocalRevisionStore.commit_revision
        original_rollback = LocalRevisionStore.rollback_revision
        winner_entered = threading.Event()
        release_winner = threading.Event()
        loser_entered_coordinator = threading.Event()
        commit_calls = 0
        rollback_calls = 0
        outcomes: list[object] = []

        def entry_trace(target_code):
            def trace(frame, event, _arg):
                if event == "call" and frame.f_code is target_code:
                    loser_entered_coordinator.set()
                    return None
                return trace

            return trace

        def blocked_commit(self, project_id, expected_head, revision_id, lease):
            nonlocal commit_calls
            commit_calls += 1
            if winner == "commit":
                winner_entered.set()
                assert release_winner.wait(timeout=5)
            return original_commit(self, project_id, expected_head, revision_id, lease)

        def blocked_rollback(self, project_id, revision_id, lease):
            nonlocal rollback_calls
            rollback_calls += 1
            if winner == "rollback":
                winner_entered.set()
                assert release_winner.wait(timeout=5)
            return original_rollback(self, project_id, revision_id, lease)

        monkeypatch.setattr(LocalRevisionStore, "commit_revision", blocked_commit)
        monkeypatch.setattr(LocalRevisionStore, "rollback_revision", blocked_rollback)

        def run_commit(*, loser: bool) -> None:
            if loser:
                sys.settrace(entry_trace(CandidateCoordinator.commit.__code__))
            try:
                outcomes.append(
                    rig.coordinator.commit(
                        candidate=sealed,
                        receipt=receipt,
                        compiled=compiled,
                        snapshot=snapshot,
                        lease=rig.lease,
                    )
                )
            except CandidateError as exc:
                outcomes.append(exc)
            finally:
                if loser:
                    sys.settrace(None)

        def run_rollback(*, loser: bool) -> None:
            if loser:
                sys.settrace(entry_trace(CandidateCoordinator.rollback.__code__))
            try:
                outcomes.append(rig.coordinator.rollback(candidate=sealed, lease=rig.lease))
            except CandidateError as exc:
                outcomes.append(exc)
            finally:
                if loser:
                    sys.settrace(None)

        winner_target = run_commit if winner == "commit" else run_rollback
        loser_target = run_rollback if winner == "commit" else run_commit
        first = threading.Thread(target=winner_target, kwargs={"loser": False})
        first.start()
        assert winner_entered.wait(timeout=5)
        second = threading.Thread(target=loser_target, kwargs={"loser": True})
        second.start()
        assert loser_entered_coordinator.wait(timeout=5)
        release_winner.set()
        for thread in (first, second):
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert len(outcomes) == 2
        errors = [item for item in outcomes if type(item) is CandidateError]
        assert len(errors) == 1
        assert errors[0].code in {
            CandidateErrorCode.ALREADY_TERMINAL,
            CandidateErrorCode.TERMINAL_IN_PROGRESS,
        }
        assert commit_calls + rollback_calls == 1
        if winner == "commit":
            assert commit_calls == 1
            assert rig.port.close_count(rig.baseline) == 1
            with pytest.raises(ValidationError):
                consume_verification_receipt(
                    receipt,
                    compiled,
                    snapshot,
                    candidate_revision=sealed.revision.id,
                    manifest_sha256=sealed.revision.manifest_sha256,
                )
        else:
            assert rollback_calls == 1
            assert rig.port.close_count(sealed.binding.session) == 1
            consumed = consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=sealed.revision.id,
                manifest_sha256=sealed.revision.manifest_sha256,
            )
            assert consumed == report
            assert consumed is not report


@pytest.mark.parametrize("kind", ["foreign", "released"])
def test_reconcile_uses_store_as_first_lease_authority_without_cad_or_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"reconcile-invalid-lease-{kind}") as rig:
        active = _begin(rig)
        supplied = rig.lease
        other_lease = None
        if kind == "foreign":
            other_manager = ResourceLeaseManager(
                _secure_root(tmp_path / "reconcile-invalid-lease-other-locks"),
                trust=LeaseRootTrust.TRUSTED_LOCAL,
            )
            other_lease = other_manager.acquire_project_write(PROJECT_ID)
            supplied = other_lease
        else:
            rig.lease.release(owner_token=rig.lease.owner_token)
        _reset_store_mutations(mutations)
        before = tuple(rig.port.calls)
        try:
            with pytest.raises(CandidateError) as caught:
                rig.coordinator.reconcile(project_id=PROJECT_ID, lease=supplied)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_LEASE)
            assert mutations["reconcile"] == 1
            assert sum(mutations.values()) == 1
            assert tuple(rig.port.calls) == before
            assert rig.port.close_count(active.binding.session) == 0
            assert rig.slot.current() is rig.baseline_binding
        finally:
            if other_lease is not None:
                other_lease.release(owner_token=other_lease.owner_token)

        if kind == "foreign":
            recovered = rig.coordinator.reconcile(
                project_id=PROJECT_ID,
                lease=rig.lease,
            )
            assert recovered.status is CandidateReconcileStatus.NOT_COMMITTED
        else:
            recovery_lease = rig.manager.acquire_project_write(PROJECT_ID)
            try:
                recovered = rig.coordinator.reconcile(
                    project_id=PROJECT_ID,
                    lease=recovery_lease,
                )
                assert recovered.status is CandidateReconcileStatus.NOT_COMMITTED
            finally:
                recovery_lease.release(owner_token=recovery_lease.owner_token)


def test_committed_reconcile_orders_durable_truth_before_load_and_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="reconcile-first-order") as rig:
        sealed = _sealed(rig)
        rig.store.commit_revision(
            PROJECT_ID,
            rig.head,
            sealed.revision.id,
            rig.lease,
        )
        events: list[str] = []
        original_reconcile = LocalRevisionStore.reconcile
        original_load = FakeCadSnapshotPort.load_fcstd
        original_cas = SessionSlot.compare_and_set
        original_current = SessionSlot.current

        def observed_reconcile(self, project_id, lease):
            events.append("reconcile")
            return original_reconcile(self, project_id, lease)

        def observed_load(self, path):
            events.append("load")
            return original_load(self, path)

        def observed_current(self):
            events.append("current")
            return original_current(self)

        def observed_cas(self, expected, replacement):
            events.append("cas")
            return original_cas(self, expected, replacement)

        monkeypatch.setattr(LocalRevisionStore, "reconcile", observed_reconcile)
        monkeypatch.setattr(FakeCadSnapshotPort, "load_fcstd", observed_load)
        monkeypatch.setattr(SessionSlot, "current", observed_current)
        monkeypatch.setattr(SessionSlot, "compare_and_set", observed_cas)
        fresh = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        result = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert result.status is CandidateReconcileStatus.COMMITTED
        assert events == ["reconcile", "current", "load", "cas", "current"]


def test_reconcile_durable_fault_precedes_and_forbids_cad_cas_or_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="reconcile-first-fault") as rig:
        sealed = _sealed(rig)
        rig.store.commit_revision(
            PROJECT_ID,
            rig.head,
            sealed.revision.id,
            rig.lease,
        )
        events: list[str] = []

        def failed_reconcile(self, project_id, lease):
            events.append("reconcile")
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)

        def forbidden_load(self, path):
            events.append("load")
            raise AssertionError("CAD load must not precede durable reconciliation")

        def forbidden_cas(self, expected, replacement):
            events.append("cas")
            raise AssertionError("CAS must not precede durable reconciliation")

        def forbidden_current(self):
            events.append("current")
            raise AssertionError("slot read must not precede durable reconciliation")

        monkeypatch.setattr(LocalRevisionStore, "reconcile", failed_reconcile)
        monkeypatch.setattr(FakeCadSnapshotPort, "load_fcstd", forbidden_load)
        monkeypatch.setattr(SessionSlot, "current", forbidden_current)
        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        fresh = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        before = tuple(rig.port.calls)
        result = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert result.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert result.recovery_required is True
        assert events == ["reconcile"]
        assert tuple(rig.port.calls) == before
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0


def test_reconcile_clean_not_committed_and_committed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="clean") as clean:
        result = clean.coordinator.reconcile(project_id=PROJECT_ID, lease=clean.lease)
        assert type(result) is CandidateReconcileResult
        assert result.status is CandidateReconcileStatus.CLEAN
        assert result.head == clean.head
        assert result.live_binding is clean.baseline_binding
        assert result.head_committed is True
        assert result.slot_promoted is True
        repeated = clean.coordinator.reconcile(project_id=PROJECT_ID, lease=clean.lease)
        assert repeated.status is CandidateReconcileStatus.CLEAN
        assert repeated.live_binding is clean.baseline_binding
        assert clean.port.calls == []

    with _rig(tmp_path, suffix="not-committed") as pending:
        active = _begin(pending)
        result = pending.coordinator.reconcile(project_id=PROJECT_ID, lease=pending.lease)
        assert result.status is CandidateReconcileStatus.NOT_COMMITTED
        assert result.head == pending.head
        assert result.live_binding is pending.baseline_binding
        assert result.head_committed is False
        assert result.slot_promoted is False
        assert pending.port.close_count(active.binding.session) == 1
        repeated = pending.coordinator.reconcile(project_id=PROJECT_ID, lease=pending.lease)
        assert repeated.status in {
            CandidateReconcileStatus.CLEAN,
            CandidateReconcileStatus.NOT_COMMITTED,
        }
        assert pending.port.close_count(active.binding.session) == 1

    with _rig(tmp_path, suffix="committed") as committed:
        sealed = _sealed(committed)
        new_head = committed.store.commit_revision(
            PROJECT_ID,
            committed.head,
            sealed.revision.id,
            committed.lease,
        )
        original_cas = SessionSlot.compare_and_set
        cas_calls = 0

        def counted_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            return original_cas(slot, expected, replacement)

        monkeypatch.setattr(SessionSlot, "compare_and_set", counted_cas)
        fresh = CandidateCoordinator(
            store=committed.store,
            snapshot_port=committed.port,
            session_slot=committed.slot,
        )
        result = fresh.reconcile(project_id=PROJECT_ID, lease=committed.lease)
        assert result.status is CandidateReconcileStatus.COMMITTED
        assert result.head == new_head
        assert result.head_committed is True
        assert result.slot_promoted is True
        assert result.live_binding is committed.slot.current()
        assert result.live_binding.revision_id == new_head.revision_id
        assert result.live_binding.session is not sealed.binding.session
        assert committed.port.close_count(committed.baseline) == 1
        assert committed.port.close_count(sealed.binding.session) == 0
        assert cas_calls == 1
        load_calls = sum(1 for call in committed.port.calls if call[0] == "load_fcstd")
        repeated = fresh.reconcile(project_id=PROJECT_ID, lease=committed.lease)
        assert repeated.status is CandidateReconcileStatus.COMMITTED
        assert repeated.live_binding is committed.slot.current()
        assert sum(1 for call in committed.port.calls if call[0] == "load_fcstd") == load_calls
        assert cas_calls == 1
        assert committed.port.close_count(committed.baseline) == 1
        assert committed.port.close_count(sealed.binding.session) == 0


def test_reconcile_clean_slot_divergence_fails_closed(tmp_path: Path) -> None:
    with _rig(tmp_path) as rig:
        third = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=OTHER_REVISION,
            session=FakeSession("third", b"third"),
        )
        assert rig.slot.compare_and_set(rig.baseline_binding, third)
        result = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert result.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert result.recovery_required is True
        assert result.live_binding is third
        assert rig.port.close_count(third.session) == 0
        assert rig.port.close_count(rig.baseline) == 0


def test_not_committed_reconcile_repeats_without_load_cas_or_double_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="reconcile-not-committed-idempotent") as rig:
        active = _begin(rig)
        load_calls = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        cas_calls = 0

        def forbidden_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            raise AssertionError("not-committed reconcile must not CAS")

        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        first = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        second = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert first.status is CandidateReconcileStatus.NOT_COMMITTED
        assert second.status in {
            CandidateReconcileStatus.CLEAN,
            CandidateReconcileStatus.NOT_COMMITTED,
        }
        assert cas_calls == 0
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == load_calls
        assert rig.port.close_count(active.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0


@pytest.mark.parametrize("mode", ["cleanup", "recovery"])
def test_reconcile_maps_store_cleanup_or_recovery_without_guessing_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    with _rig(tmp_path, suffix=f"reconcile-store-{mode}") as rig:
        active = _begin(rig)
        original = LocalRevisionStore.reconcile
        cas_calls = 0

        def scripted_reconcile(self, project_id, lease):
            if mode == "recovery":
                raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)
            result = original(self, project_id, lease)
            assert result.status is ReconciliationStatus.NOT_COMMITTED
            return replace(result, status=ReconciliationStatus.CLEANUP_REQUIRED)

        def forbidden_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            raise AssertionError("cleanup/recovery durable state must not CAS")

        monkeypatch.setattr(LocalRevisionStore, "reconcile", scripted_reconcile)
        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        result = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert cas_calls == 0
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0
        if mode == "cleanup":
            assert result.status is CandidateReconcileStatus.CLEANUP_REQUIRED
            assert result.cleanup_required is True
            assert rig.port.close_count(active.binding.session) == 1
        else:
            assert result.status is CandidateReconcileStatus.RECOVERY_REQUIRED
            assert result.recovery_required is True
            assert rig.port.close_count(active.binding.session) == 0
        repeated = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert repeated.status is result.status
        assert cas_calls == 0
        if mode == "cleanup":
            assert rig.port.close_count(active.binding.session) == 1
        else:
            assert rig.port.close_count(active.binding.session) == 0


def test_commit_cas_failure_is_recovered_by_later_idempotent_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _rig(tmp_path, suffix="commit-cas-then-reconcile") as rig:
        sealed = _sealed(rig)
        original_cas = SessionSlot.compare_and_set
        cas_calls = 0

        def fail_first_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            return False

        monkeypatch.setattr(SessionSlot, "compare_and_set", fail_first_cas)
        committed, *_unused = _commit(rig, sealed)
        assert committed.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        assert committed.slot_promoted is False
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(sealed.binding.session) == 1

        def counted_real_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            return original_cas(slot, expected, replacement)

        monkeypatch.setattr(SessionSlot, "compare_and_set", counted_real_cas)
        recovered = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert recovered.status is CandidateReconcileStatus.COMMITTED
        assert recovered.slot_promoted is True
        assert rig.slot.current().revision_id == sealed.revision.id
        assert rig.slot.current().session is not sealed.binding.session
        assert rig.port.close_count(rig.baseline) == 1
        assert cas_calls == 2
        load_calls = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        repeated = rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert repeated.status is CandidateReconcileStatus.COMMITTED
        assert cas_calls == 2
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == load_calls


@pytest.mark.parametrize(
    (
        "mode",
        "cas_expected",
        "baseline_closes",
        "replacement_closes",
        "cleanup_retained",
    ),
    [
        ("load_failure", 0, 0, 0, False),
        ("load_alias_baseline", 0, 0, 0, False),
        ("cas_false", 1, 0, 1, False),
        ("cas_raise_baseline", 1, 0, 1, False),
        ("cas_false_promoted", 1, 1, 0, False),
        ("cas_raise_promoted", 1, 1, 0, False),
        ("cas_true_without_publish", 1, 0, 0, True),
        ("cas_raise_third", 1, 0, 0, True),
        ("cas_false_third", 1, 0, 0, True),
        ("cas_true_third", 1, 0, 0, True),
        ("cas_readback_unreadable", 1, 0, 0, False),
        ("cas_false_unreadable", 1, 0, 0, False),
        ("cas_raise_unreadable", 1, 0, 0, False),
    ],
)
def test_fresh_committed_reconcile_uses_full_load_cas_readback_ownership_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    cas_expected: int,
    baseline_closes: int,
    replacement_closes: int,
    cleanup_retained: bool,
) -> None:
    with _rig(tmp_path, suffix=f"reconcile-matrix-{mode}") as rig:
        sealed = _sealed(rig)
        rig.store.commit_revision(
            PROJECT_ID,
            rig.head,
            sealed.revision.id,
            rig.lease,
        )
        fresh = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        original_cas = SessionSlot.compare_and_set
        original_current = SessionSlot.current
        cas_calls = 0
        cas_finished = False
        if mode == "load_failure":
            rig.port.fail_load_number = 3
        elif mode == "load_alias_baseline":
            rig.port.load_aliases[3] = rig.baseline

        def scripted_cas(slot, expected, replacement):
            nonlocal cas_calls, cas_finished
            cas_calls += 1
            if mode == "cas_false":
                cas_finished = True
                return False
            if mode == "cas_raise_baseline":
                cas_finished = True
                raise RuntimeError("reconcile CAS failed before publish")
            if mode == "cas_false_promoted":
                assert original_cas(slot, expected, replacement) is True
                cas_finished = True
                return False
            if mode == "cas_raise_promoted":
                assert original_cas(slot, expected, replacement) is True
                cas_finished = True
                raise RuntimeError("reconcile CAS failed after publish")
            if mode == "cas_true_without_publish":
                cas_finished = True
                return True
            if mode in {"cas_raise_third", "cas_false_third", "cas_true_third"}:
                third = SessionBinding(
                    project_id=PROJECT_ID,
                    revision_id=OTHER_REVISION,
                    session=FakeSession("third", b"third"),
                )
                assert original_cas(slot, expected, third) is True
                cas_finished = True
                if mode == "cas_raise_third":
                    raise RuntimeError("reconcile CAS left divergent binding")
                return mode == "cas_true_third"
            if mode == "cas_readback_unreadable":
                assert original_cas(slot, expected, replacement) is True
                cas_finished = True
                return True
            if mode == "cas_false_unreadable":
                cas_finished = True
                return False
            assert mode == "cas_raise_unreadable"
            cas_finished = True
            raise RuntimeError("reconcile CAS and readback unavailable")

        def scripted_current(slot):
            if (
                mode
                in {
                    "cas_readback_unreadable",
                    "cas_false_unreadable",
                    "cas_raise_unreadable",
                }
                and cas_finished
            ):
                raise RuntimeError("reconcile slot readback unavailable")
            return original_current(slot)

        monkeypatch.setattr(SessionSlot, "compare_and_set", scripted_cas)
        monkeypatch.setattr(SessionSlot, "current", scripted_current)
        created_before = len(rig.port.created)
        result = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert result.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert result.head_committed is True
        assert result.recovery_required is True
        assert result.cleanup_required is cleanup_retained
        assert cas_calls == cas_expected
        assert rig.port.close_count(rig.baseline) == baseline_closes
        assert rig.port.close_count(sealed.binding.session) == 0
        if len(rig.port.created) == created_before:
            assert mode in {"load_failure", "load_alias_baseline"}
        else:
            replacement = rig.port.created[-1]
            assert rig.port.close_count(replacement) == replacement_closes
            if cleanup_retained:
                assert type(result.cleanup_binding) is SessionBinding
                assert result.cleanup_binding.project_id == PROJECT_ID
                assert result.cleanup_binding.revision_id == sealed.revision.id
                assert result.cleanup_binding.session is replacement
            else:
                assert result.cleanup_binding is None
        if len(rig.port.created) == created_before:
            assert result.cleanup_binding is None
        elif cleanup_retained:
            monkeypatch.setattr(SessionSlot, "compare_and_set", original_cas)
            monkeypatch.setattr(SessionSlot, "current", original_current)
            current = rig.slot.current()
            with pytest.raises(CandidateError) as caught:
                rig.slot.compare_and_set(current, result.cleanup_binding)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
            assert rig.slot.current() is current
        assert rig.store.load_head(PROJECT_ID).revision_id == sealed.revision.id


@pytest.mark.parametrize("origin", ["commit", "fresh"])
@pytest.mark.parametrize("mode", ["promoted", "false_baseline", "raise_baseline"])
def test_unreadable_cas_ownership_converges_once_without_session_accumulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    mode: str,
) -> None:
    with _rig(tmp_path, suffix=f"unreadable-ownership-{origin}-{mode}") as rig:
        sealed = _sealed(rig)
        coordinator = rig.coordinator
        if origin == "fresh":
            rig.store.commit_revision(
                PROJECT_ID,
                rig.head,
                sealed.revision.id,
                rig.lease,
            )
            coordinator = CandidateCoordinator(
                store=rig.store,
                snapshot_port=rig.port,
                session_slot=rig.slot,
            )

        original_cas = SessionSlot.compare_and_set
        original_current = SessionSlot.current
        readback_unreadable = False
        cas_calls = 0
        replacements: list[SessionBinding] = []

        def unreadable_cas(slot, expected, replacement):
            nonlocal cas_calls, readback_unreadable
            cas_calls += 1
            replacements.append(replacement)
            if mode == "promoted":
                assert original_cas(slot, expected, replacement) is True
                readback_unreadable = True
                return True
            readback_unreadable = True
            if mode == "false_baseline":
                return False
            raise RuntimeError("CAS outcome and slot readback are unavailable")

        def unreadable_current(slot):
            if readback_unreadable:
                raise RuntimeError("slot readback unavailable")
            return original_current(slot)

        monkeypatch.setattr(SessionSlot, "compare_and_set", unreadable_cas)
        monkeypatch.setattr(SessionSlot, "current", unreadable_current)
        if origin == "commit":
            first, *_unused = _commit(rig, sealed)
            assert first.status is CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
        else:
            first = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
            assert first.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert first.cleanup_required is False
        assert first.recovery_required is True
        assert first.live_binding is None
        assert len(replacements) == 1
        unknown = replacements[0]
        unknown_session = unknown.session
        assert rig.port.close_count(unknown_session) == 0
        assert rig.port.close_count(rig.baseline) == 0

        monkeypatch.setattr(SessionSlot, "compare_and_set", original_cas)
        monkeypatch.setattr(SessionSlot, "current", original_current)
        loads_before = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        created_before = len(rig.port.created)
        cas_before = cas_calls
        recovered = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert recovered.status is CandidateReconcileStatus.COMMITTED
        assert recovered.slot_promoted is True
        assert rig.slot.current().revision_id == sealed.revision.id
        assert rig.port.close_count(rig.baseline) == 1
        if origin == "fresh":
            assert rig.port.close_count(sealed.binding.session) == 0
        if mode == "promoted":
            assert rig.slot.current() is unknown
            assert rig.port.close_count(unknown_session) == 0
            assert len(rig.port.created) == created_before
            assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == loads_before
            assert cas_calls == cas_before
        else:
            assert rig.slot.current().session is not unknown_session
            assert rig.port.close_count(unknown_session) == 1
            assert len(rig.port.created) == created_before + 1
            assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == (
                loads_before + 1
            )

        stable_counts = (
            len(rig.port.created),
            len(rig.port.calls),
            rig.port.close_count(unknown_session),
            rig.port.close_count(rig.baseline),
        )
        repeated = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert repeated.status is CandidateReconcileStatus.COMMITTED
        assert (
            len(rig.port.created),
            len(rig.port.calls),
            rig.port.close_count(unknown_session),
            rig.port.close_count(rig.baseline),
        ) == stable_counts


def test_persistent_reconcile_load_failure_is_bounded_and_later_converges_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="persistent-reconcile-load-failure") as rig:
        sealed = _sealed(rig)
        rig.store.commit_revision(
            PROJECT_ID,
            rig.head,
            sealed.revision.id,
            rig.lease,
        )
        fresh = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        original_load = FakeCadSnapshotPort.load_fcstd
        original_cas = SessionSlot.compare_and_set
        load_attempts = 0
        cas_calls = 0

        def failed_load(self, path):
            nonlocal load_attempts
            load_attempts += 1
            raise RuntimeError("persistent immutable load failure")

        def forbidden_cas(self, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            raise AssertionError("failed immutable load must not CAS")

        monkeypatch.setattr(FakeCadSnapshotPort, "load_fcstd", failed_load)
        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        created_before = len(rig.port.created)
        calls_before = tuple(rig.port.calls)
        first = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        second = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert first.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert second.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert load_attempts == 1
        assert cas_calls == 0
        assert len(rig.port.created) == created_before
        assert tuple(rig.port.calls) == calls_before
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0

        monkeypatch.setattr(FakeCadSnapshotPort, "load_fcstd", original_load)
        monkeypatch.setattr(SessionSlot, "compare_and_set", original_cas)
        recovered_coordinator = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        recovered = recovered_coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert recovered.status is CandidateReconcileStatus.COMMITTED
        assert rig.slot.current().revision_id == sealed.revision.id
        assert len(rig.port.created) == created_before + 1
        assert rig.port.close_count(rig.baseline) == 1
        stable_calls = tuple(rig.port.calls)
        repeated = recovered_coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert repeated.status is CandidateReconcileStatus.COMMITTED
        assert tuple(rig.port.calls) == stable_calls


def test_fresh_retained_cleanup_binding_is_retired_and_reconciled_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _rig(tmp_path, suffix="fresh-retained-cleanup") as rig:
        sealed = _sealed(rig)
        rig.store.commit_revision(
            PROJECT_ID,
            rig.head,
            sealed.revision.id,
            rig.lease,
        )
        fresh = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        original_cas = SessionSlot.compare_and_set

        def true_without_publish(slot, expected, replacement):
            return True

        monkeypatch.setattr(SessionSlot, "compare_and_set", true_without_publish)
        first = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert first.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert first.cleanup_required is True
        assert type(first.cleanup_binding) is SessionBinding
        retained = first.cleanup_binding
        retained_session = retained.session
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(retained_session) == 0

        monkeypatch.setattr(SessionSlot, "compare_and_set", original_cas)
        with pytest.raises(CandidateError) as caught:
            rig.slot.compare_and_set(rig.baseline_binding, retained)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_BINDING)
        assert rig.slot.current() is rig.baseline_binding

        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_during_cleanup_close() -> None:
            try:
                original_cas(rig.slot, rig.slot.current(), retained)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(retained_session)] = try_publish_during_cleanup_close
        cas_calls = 0

        def counted_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            return original_cas(slot, expected, replacement)

        monkeypatch.setattr(SessionSlot, "compare_and_set", counted_cas)
        loads_before = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        recovered = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert recovered.status is CandidateReconcileStatus.COMMITTED
        assert recovered.slot_promoted is True
        assert rig.slot.current().revision_id == sealed.revision.id
        assert rig.slot.current().session is not retained_session
        assert rig.port.close_count(retained_session) == 1
        assert rig.port.close_count(rig.baseline) == 1
        assert rig.port.close_count(sealed.binding.session) == 0
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert cas_calls == 1
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == (loads_before + 1)

        repeated = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert repeated.status is CandidateReconcileStatus.COMMITTED
        assert rig.port.close_count(retained_session) == 1
        assert rig.port.close_count(rig.baseline) == 1
        assert cas_calls == 1
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == (loads_before + 1)


def test_runtime_close_failure_before_side_effect_is_sticky(tmp_path: Path) -> None:
    with _rig(tmp_path, suffix="runtime-close-sticky") as rig:

        def fail_before_close() -> None:
            raise RuntimeError("close failed before side effect")

        rig.port.close_hooks[id(rig.baseline)] = fail_before_close

        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        assert rig.baseline.closed is False
        assert rig.port.close_count(rig.baseline) == 1

        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        assert rig.baseline.closed is False
        assert rig.port.close_count(rig.baseline) == 1


def test_runtime_baseline_keyboard_interrupt_is_sticky_before_propagation(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="runtime-close-keyboard-interrupt") as rig:

        def interrupt_before_close() -> None:
            raise KeyboardInterrupt

        rig.port.close_hooks[id(rig.baseline)] = interrupt_before_close
        with pytest.raises(KeyboardInterrupt):
            rig.coordinator._close_runtime(project_id=PROJECT_ID)

        assert rig.baseline.closed is False
        assert rig.baseline_binding in rig.coordinator._attempted
        assert rig.baseline_binding in rig.coordinator._unresolved
        assert rig.coordinator._close_failed is True
        assert rig.port.close_count(rig.baseline) == 1
        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        assert rig.port.close_count(rig.baseline) == 1

        with pytest.raises(CandidateError) as caught:
            _begin(rig)
        error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
        assert error.cleanup_required is True
        assert rig.port.close_count(rig.baseline) == 1


def test_owned_binding_system_exit_is_retained_and_never_reclosed(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="owned-close-system-exit") as rig:
        active = _begin(rig)

        def exit_before_close() -> None:
            raise SystemExit(91)

        rig.port.close_hooks[id(active.binding.session)] = exit_before_close
        with pytest.raises(SystemExit) as caught:
            rig.coordinator._close_binding(active.binding)
        assert caught.value.code == 91

        assert active.binding.session.closed is False
        assert active.binding in rig.coordinator._attempted
        assert active.binding in rig.coordinator._unresolved
        assert active.binding in rig.coordinator._retained[PROJECT_ID]
        assert rig.coordinator._close_failed is True
        assert rig.port.close_count(active.binding.session) == 1
        assert rig.coordinator._close_binding(active.binding) is True
        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        assert rig.port.close_count(active.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0

        with pytest.raises(CandidateError) as blocked:
            _begin(rig)
        error = _assert_candidate_error(blocked, CandidateErrorCode.CLEANUP_REQUIRED)
        assert error.cleanup_required is True
        assert rig.port.close_count(active.binding.session) == 1


@pytest.mark.parametrize("owner", ["retained", "pending_baseline"])
def test_owned_cleanup_close_failure_stays_unresolved_and_blocks_runtime_eviction(
    tmp_path: Path,
    owner: str,
) -> None:
    with _rig(tmp_path, suffix=f"{owner}-close-sticky") as rig:
        cleanup_binding = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=OTHER_REVISION,
            session=FakeSession(owner, owner.encode()),
        )

        def fail_before_close() -> None:
            raise RuntimeError("close failed before side effect")

        collection = rig.coordinator._retained
        if owner == "retained":
            rig.coordinator._retain(PROJECT_ID, cleanup_binding)
        else:
            rig.coordinator._remember_baseline(PROJECT_ID, cleanup_binding)
            collection = rig.coordinator._pending_baselines
        rig.port.close_hooks[id(cleanup_binding.session)] = fail_before_close

        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        assert cleanup_binding.session.closed is False
        assert rig.baseline.closed is False
        assert rig.port.close_count(cleanup_binding.session) == 1
        assert cleanup_binding in collection[PROJECT_ID]

        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        assert cleanup_binding.session.closed is False
        assert rig.baseline.closed is False
        assert rig.port.close_count(cleanup_binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0
        assert cleanup_binding in collection[PROJECT_ID]


def test_terminal_owned_close_failure_is_sticky_before_runtime_eviction(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="terminal-owned-close-sticky") as rig:
        active = _begin(rig)

        def fail_before_close() -> None:
            raise RuntimeError("close failed before side effect")

        rig.port.close_hooks[id(active.binding.session)] = fail_before_close
        result = rig.coordinator.rollback(candidate=active, lease=rig.lease)
        assert result.status is CandidateRollbackStatus.CLEANUP_REQUIRED
        assert active.binding.session.closed is False
        assert rig.port.close_count(active.binding.session) == 1

        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        assert active.binding.session.closed is False
        assert rig.baseline.closed is False
        assert rig.port.close_count(active.binding.session) == 1
        assert rig.port.close_count(rig.baseline) == 0


@pytest.mark.parametrize("entry", ["begin", "reopen_review", "reconcile"])
def test_close_poisoned_root_entry_rejects_without_store_or_cad_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"close-poisoned-{entry}") as rig:
        revision = None
        if entry == "reopen_review":
            revision = _detached_review_revision(rig)
        elif entry == "reconcile":
            revision_id = rig.store.begin_revision(PROJECT_ID, rig.head, rig.lease)
            model_path = rig.store.candidate_model_path(PROJECT_ID, revision_id, rig.lease)
            step_path = rig.store.candidate_artifact_path(
                PROJECT_ID,
                revision_id,
                "step",
                rig.lease,
            )
            model_path.write_bytes(b"committed-fcstd")
            step_path.write_bytes(b"ISO-10303-21;COMMITTED;END-ISO-10303-21;")
            sealed = rig.store.seal_revision(PROJECT_ID, revision_id, rig.lease)
            rig.store.commit_revision(PROJECT_ID, rig.head, sealed.id, rig.lease)

        def fail_before_close() -> None:
            raise RuntimeError("close failed before side effect")

        rig.port.close_hooks[id(rig.baseline)] = fail_before_close
        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is False
        calls_before = tuple(rig.port.calls)
        _reset_store_mutations(mutations)

        for _attempt in range(2):
            with pytest.raises(CandidateError) as caught:
                if entry == "begin":
                    _begin(rig)
                elif entry == "reopen_review":
                    assert revision is not None
                    rig.coordinator.reopen_review(
                        project_id=PROJECT_ID,
                        base_head=rig.head,
                        revision=revision,
                        lease=rig.lease,
                    )
                else:
                    rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
            error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
            assert error.cleanup_required is True
            assert error.recovery_required is False

        assert not any(mutations.values())
        assert tuple(rig.port.calls) == calls_before
        assert rig.port.close_count(rig.baseline) == 1


@pytest.mark.parametrize("entry", ["checkpoint", "seal"])
def test_close_poisoned_candidate_transition_cannot_load_another_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"close-poisoned-{entry}") as rig:
        candidate = _begin(rig)
        if entry == "seal":
            candidate = _checkpoint(rig, candidate)
            candidate.step_path.write_bytes(b"ISO-10303-21;END-ISO-10303-21;")
        poison = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=OTHER_REVISION,
            session=FakeSession("poison", b"poison"),
        )

        def fail_before_close() -> None:
            raise RuntimeError("close failed before side effect")

        rig.port.close_hooks[id(poison.session)] = fail_before_close
        assert rig.coordinator._close_binding(poison) is True
        calls_before = tuple(rig.port.calls)
        _reset_store_mutations(mutations)

        for _attempt in range(2):
            with pytest.raises(CandidateError) as caught:
                if entry == "checkpoint":
                    rig.coordinator.checkpoint(candidate=candidate, lease=rig.lease)
                else:
                    rig.coordinator.seal(candidate=candidate, lease=rig.lease)
            error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
            assert error.cleanup_required is True
            assert error.recovery_required is False

        assert not any(mutations.values())
        assert tuple(rig.port.calls) == calls_before
        assert rig.port.close_count(poison.session) == 1


def test_terminal_replay_is_bounded_and_evicts_the_oldest_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 32
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix="bounded-terminal-replay") as rig:
        candidates = []
        results = []
        for _index in range(limit + 1):
            candidate = _begin(rig)
            result = rig.coordinator.rollback(candidate=candidate, lease=rig.lease)
            candidates.append(candidate)
            results.append(result)

        lineage_fields = (
            "_stages",
            "_leases",
            "_heads",
            "_baselines",
            "_paths",
            "_handles",
            "_bindings",
            "_terminal_kinds",
            "_terminal_results",
        )
        assert all(len(getattr(rig.coordinator, name)) == limit for name in lineage_fields)
        assert len(rig.coordinator._owners) == limit
        assert candidate_module._TERMINAL_REPLAY_LIMIT == limit

        _reset_store_mutations(mutations)
        calls_before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.rollback(candidate=candidates[0], lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert rig.coordinator.rollback(candidate=candidates[1], lease=rig.lease) is results[1]
        assert rig.coordinator.rollback(candidate=candidates[-1], lease=rig.lease) is results[-1]
        assert not any(mutations.values())
        assert tuple(rig.port.calls) == calls_before


def test_terminal_replay_eviction_never_reclaims_a_nonterminal_capability(
    tmp_path: Path,
) -> None:
    limit = 32
    with _rig(tmp_path, suffix="bounded-terminal-active") as rig:
        revision = _detached_review_revision(rig)
        protected = rig.coordinator.reopen_review(
            project_id=PROJECT_ID,
            base_head=rig.head,
            revision=revision,
            lease=rig.lease,
        )
        completed = []
        for _index in range(limit + 1):
            candidate = rig.coordinator.reopen_review(
                project_id=PROJECT_ID,
                base_head=rig.head,
                revision=revision,
                lease=rig.lease,
            )
            rig.coordinator.discard_review(candidate=candidate, lease=rig.lease)
            completed.append(candidate)

        assert len(rig.coordinator._stages) == limit + 1
        assert rig.coordinator._owners.get(protected) is not None
        calls_before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.discard_review(candidate=completed[0], lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert rig.coordinator.discard_review(candidate=completed[-1], lease=rig.lease) is None
        assert tuple(rig.port.calls) == calls_before

        protected_session = protected.binding.session
        assert rig.coordinator.discard_review(candidate=protected, lease=rig.lease) is None
        assert rig.port.close_count(protected_session) == 1
        assert len(rig.coordinator._stages) == limit


def test_failed_lineage_rejection_is_cached_then_safely_evicted(tmp_path: Path) -> None:
    limit = 32
    with _rig(tmp_path, suffix="bounded-terminal-failed") as rig:
        failed = _begin(rig)
        rig.port.fail_checkpoint = True
        with pytest.raises(CandidateError) as caught:
            _checkpoint(rig, failed)
        _assert_candidate_error(caught, CandidateErrorCode.CAD_FAILURE)
        rig.port.fail_checkpoint = False

        with pytest.raises(CandidateError) as caught:
            rig.coordinator.rollback(candidate=failed, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.ALREADY_TERMINAL)

        for _index in range(limit):
            candidate = _begin(rig)
            rig.coordinator.rollback(candidate=candidate, lease=rig.lease)

        calls_before = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.rollback(candidate=failed, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert tuple(rig.port.calls) == calls_before
        assert len(rig.coordinator._stages) == limit


def test_successful_runtime_close_purges_terminal_replay_authority(tmp_path: Path) -> None:
    with _rig(tmp_path, suffix="runtime-close-purges-terminal") as rig:
        oldest = None
        for _index in range(3):
            candidate = _begin(rig)
            rig.coordinator.rollback(candidate=candidate, lease=rig.lease)
            if oldest is None:
                oldest = candidate

        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is True
        assert rig.baseline.closed is True
        lineage_fields = (
            "_owners",
            "_stages",
            "_leases",
            "_heads",
            "_baselines",
            "_paths",
            "_handles",
            "_bindings",
            "_terminal_kinds",
            "_terminal_results",
        )
        assert all(not getattr(rig.coordinator, name) for name in lineage_fields)

        calls_before = tuple(rig.port.calls)
        assert oldest is not None
        with pytest.raises(CandidateError) as caught:
            rig.coordinator.rollback(candidate=oldest, lease=rig.lease)
        _assert_candidate_error(caught, CandidateErrorCode.INVALID_CANDIDATE)
        assert tuple(rig.port.calls) == calls_before


@pytest.mark.parametrize("entry", ["begin", "reopen_review", "reconcile"])
def test_closed_runtime_root_entry_is_stably_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    mutations = _track_store_mutations(monkeypatch)
    with _rig(tmp_path, suffix=f"runtime-closed-{entry}") as rig:
        revision = _detached_review_revision(rig) if entry == "reopen_review" else None
        assert rig.coordinator._close_runtime(project_id=PROJECT_ID) is True
        calls_before = tuple(rig.port.calls)
        _reset_store_mutations(mutations)

        for _attempt in range(2):
            with pytest.raises(CandidateError) as caught:
                if entry == "begin":
                    _begin(rig)
                elif entry == "reopen_review":
                    assert revision is not None
                    rig.coordinator.reopen_review(
                        project_id=PROJECT_ID,
                        base_head=rig.head,
                        revision=revision,
                        lease=rig.lease,
                    )
                else:
                    rig.coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
            _assert_candidate_error(caught, CandidateErrorCode.INVALID_TRANSITION)

        assert not any(mutations.values())
        assert tuple(rig.port.calls) == calls_before
        assert rig.port.close_count(rig.baseline) == 1


@pytest.mark.parametrize("origin", ["commit", "fresh"])
def test_retained_cleanup_waits_for_authoritative_reconcile_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
) -> None:
    with _rig(tmp_path, suffix=f"retained-reconcile-first-{origin}") as rig:
        sealed = _sealed(rig)
        original_cas = SessionSlot.compare_and_set
        original_reconcile = LocalRevisionStore.reconcile

        def true_without_publish(slot, expected, replacement):
            return True

        if origin == "commit":
            monkeypatch.setattr(SessionSlot, "compare_and_set", true_without_publish)
            first, *_unused = _commit(rig, sealed)
            coordinator = rig.coordinator
        else:
            rig.store.commit_revision(
                PROJECT_ID,
                rig.head,
                sealed.revision.id,
                rig.lease,
            )
            coordinator = CandidateCoordinator(
                store=rig.store,
                snapshot_port=rig.port,
                session_slot=rig.slot,
            )
            monkeypatch.setattr(SessionSlot, "compare_and_set", true_without_publish)
            first = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)

        assert first.cleanup_required is True
        assert type(first.cleanup_binding) is SessionBinding
        retained = first.cleanup_binding
        retained_session = retained.session
        assert rig.port.close_count(retained_session) == 0

        events: list[str] = []

        def failed_reconcile(self, project_id, lease):
            events.append("reconcile")
            raise RevisionStoreError(RevisionStoreErrorCode.RECOVERY_REQUIRED)

        def forbidden_cas(slot, expected, replacement):
            events.append("cas")
            raise AssertionError("durable reconcile failure must forbid CAS")

        monkeypatch.setattr(LocalRevisionStore, "reconcile", failed_reconcile)
        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        before = tuple(rig.port.calls)
        result = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert result.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert result.recovery_required is True
        assert events == ["reconcile"]
        assert tuple(rig.port.calls) == before
        assert rig.port.close_count(retained_session) == 0
        assert rig.port.close_count(rig.baseline) == 0
        assert rig.port.close_count(sealed.binding.session) == 0
        monkeypatch.setattr(LocalRevisionStore, "reconcile", original_reconcile)
        monkeypatch.setattr(SessionSlot, "compare_and_set", original_cas)
        rig.port.close_failures.add(id(retained_session))
        loads_before = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        recovered = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert recovered.status is CandidateReconcileStatus.CLEANUP_REQUIRED
        assert recovered.head_committed is True
        assert recovered.slot_promoted is True
        assert recovered.cleanup_required is True
        assert recovered.recovery_required is False
        assert rig.slot.current().revision_id == sealed.revision.id
        assert rig.slot.current().session is not retained_session
        assert rig.port.close_count(retained_session) == 1
        assert rig.port.close_count(rig.baseline) == 1
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == (loads_before + 1)

        stable_calls = tuple(rig.port.calls)
        with pytest.raises(CandidateError) as caught:
            coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
        assert error.cleanup_required is True
        assert rig.port.close_count(retained_session) == 1
        assert rig.port.close_count(rig.baseline) == 1
        assert tuple(rig.port.calls) == stable_calls


@pytest.mark.parametrize("origin", ["commit", "fresh"])
@pytest.mark.parametrize("close_failure", [False, True])
def test_retained_cleanup_is_bounded_when_live_slot_is_third_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    close_failure: bool,
) -> None:
    with _rig(tmp_path, suffix=f"retained-third-{origin}-{close_failure}") as rig:
        sealed = _sealed(rig)
        original_cas = SessionSlot.compare_and_set
        third = SessionBinding(
            project_id=PROJECT_ID,
            revision_id=OTHER_REVISION,
            session=FakeSession("third", b"third"),
        )

        def publish_third(slot, expected, replacement):
            assert original_cas(slot, expected, third) is True
            return False

        if origin == "commit":
            monkeypatch.setattr(SessionSlot, "compare_and_set", publish_third)
            first, *_unused = _commit(rig, sealed)
            coordinator = rig.coordinator
        else:
            rig.store.commit_revision(
                PROJECT_ID,
                rig.head,
                sealed.revision.id,
                rig.lease,
            )
            coordinator = CandidateCoordinator(
                store=rig.store,
                snapshot_port=rig.port,
                session_slot=rig.slot,
            )
            monkeypatch.setattr(SessionSlot, "compare_and_set", publish_third)
            first = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)

        expected_status = (
            CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
            if origin == "commit"
            else CandidateReconcileStatus.RECOVERY_REQUIRED
        )
        assert first.status is expected_status
        assert first.cleanup_required is True
        assert type(first.cleanup_binding) is SessionBinding
        retained = first.cleanup_binding
        retained_session = retained.session
        assert rig.slot.current() is third
        assert rig.port.close_count(retained_session) == 0

        retirement_rejections: list[CandidateErrorCode] = []

        def try_publish_during_cleanup_close() -> None:
            try:
                original_cas(rig.slot, third, retained)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(retained_session)] = try_publish_during_cleanup_close
        cas_calls = 0

        def forbidden_cas(slot, expected, replacement):
            nonlocal cas_calls
            cas_calls += 1
            raise AssertionError("a divergent third binding must not be replaced")

        monkeypatch.setattr(SessionSlot, "compare_and_set", forbidden_cas)
        if close_failure:
            rig.port.close_failures.add(id(retained_session))
        loads_before = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        recovered = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert recovered.status is CandidateReconcileStatus.RECOVERY_REQUIRED
        assert recovered.live_binding is third
        assert recovered.cleanup_required is close_failure
        assert recovered.recovery_required is True
        assert recovered.cleanup_binding is None
        assert rig.slot.current() is third
        assert rig.port.close_count(retained_session) == 1
        assert rig.port.close_count(third.session) == 0
        assert rig.port.close_count(rig.baseline) == 0
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        assert cas_calls == 0
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == loads_before

        if close_failure:
            with pytest.raises(CandidateError) as caught:
                coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
            error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
            assert error.cleanup_required is True
        else:
            repeated = coordinator.reconcile(project_id=PROJECT_ID, lease=rig.lease)
            assert repeated.status is CandidateReconcileStatus.RECOVERY_REQUIRED
            assert repeated.live_binding is third
        assert rig.port.close_count(retained_session) == 1
        assert rig.port.close_count(third.session) == 0
        assert rig.port.close_count(rig.baseline) == 0
        assert cas_calls == 0
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == loads_before


def test_fresh_committed_reconcile_baseline_close_failure_is_not_retried(
    tmp_path: Path,
) -> None:
    with _rig(tmp_path, suffix="reconcile-baseline-close-failure") as rig:
        sealed = _sealed(rig)
        rig.store.commit_revision(
            PROJECT_ID,
            rig.head,
            sealed.revision.id,
            rig.lease,
        )
        fresh = CandidateCoordinator(
            store=rig.store,
            snapshot_port=rig.port,
            session_slot=rig.slot,
        )
        retirement_rejections: list[CandidateErrorCode] = []

        def try_republish_baseline() -> None:
            try:
                rig.slot.compare_and_set(rig.slot.current(), rig.baseline_binding)
            except CandidateError as exc:
                retirement_rejections.append(exc.code)

        rig.port.close_hooks[id(rig.baseline)] = try_republish_baseline
        rig.port.close_failures.add(id(rig.baseline))
        result = fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        assert result.status is CandidateReconcileStatus.CLEANUP_REQUIRED
        assert result.head_committed is True
        assert result.slot_promoted is True
        assert result.cleanup_required is True
        assert result.recovery_required is False
        assert rig.slot.current().revision_id == sealed.revision.id
        assert rig.port.close_count(rig.baseline) == 1
        assert retirement_rejections == [CandidateErrorCode.INVALID_BINDING]
        load_calls = sum(1 for call in rig.port.calls if call[0] == "load_fcstd")
        with pytest.raises(CandidateError) as caught:
            fresh.reconcile(project_id=PROJECT_ID, lease=rig.lease)
        error = _assert_candidate_error(caught, CandidateErrorCode.CLEANUP_REQUIRED)
        assert error.cleanup_required is True
        assert rig.port.close_count(rig.baseline) == 1
        assert sum(1 for call in rig.port.calls if call[0] == "load_fcstd") == load_calls


def test_rollback_discovers_advanced_head_and_never_rolls_it_back(tmp_path: Path) -> None:
    with _rig(tmp_path, suffix="rollback-after-head") as rig:
        sealed = _sealed(rig)
        new_head = rig.store.commit_revision(
            PROJECT_ID,
            rig.head,
            sealed.revision.id,
            rig.lease,
        )
        result = rig.coordinator.rollback(candidate=sealed, lease=rig.lease)
        assert result.status is CandidateRollbackStatus.RECOVERY_REQUIRED
        assert result.head_committed is True
        assert result.recovery_required is True
        assert result.head == new_head
        assert rig.store.load_head(PROJECT_ID) == new_head
        assert rig.slot.current() is rig.baseline_binding
        assert rig.port.close_count(rig.baseline) == 0


def test_source_boundary_has_no_session_private_cad_network_or_serialization_escape() -> None:
    source_path = Path(candidate_module.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    imported_modules: set[str] = set()
    plain_imports: list[tuple[str, str | None]] = []
    from_imports: list[tuple[str | None, str, str | None, int]] = []
    module_aliases: dict[str, str] = {}
    relative_imports = 0
    attributes: set[str] = set()
    calls: set[str] = set()
    attribute_calls: set[str] = set()
    names: set[str] = set()
    subscript_calls = 0
    dynamic_calls = 0
    definitions: set[str] = set()
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            imported_modules.update(alias.name for alias in node.names)
            for alias in node.names:
                plain_imports.append((alias.name, alias.asname))
                module_aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative_imports += 1
            if node.module:
                imported_roots.add(node.module.split(".", 1)[0])
                imported_modules.add(node.module)
            for alias in node.names:
                from_imports.append((node.module, alias.name, alias.asname, node.level))
        elif isinstance(node, ast.Attribute):
            attributes.add(node.attr)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                calls.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                attribute_calls.add(node.func.attr)
            elif isinstance(node.func, ast.Subscript):
                subscript_calls += 1
            else:
                dynamic_calls += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.add(node.name)
        if isinstance(node, ast.Name):
            names.add(node.id)
    assert imported_roots.isdisjoint(
        {
            "FreeCAD",
            "Part",
            "aiohttp",
            "httpx",
            "importlib",
            "requests",
            "socket",
            "subprocess",
            "urllib",
        }
    )
    allowed_plain_imports = {
        "os",
        "re",
        "resource",
        "secrets",
        "signal",
        "threading",
        "weakref",
    }
    assert len(set(plain_imports)) == len(plain_imports)
    plain_import_modules = [module for module, _alias in plain_imports]
    assert len(plain_import_modules) == len(set(plain_import_modules))
    for module, alias in plain_imports:
        assert module in allowed_plain_imports
        assert alias is not None and alias.startswith("_") and not alias.startswith("__")
    assert sum(1 for module, _alias in plain_imports if module == "threading") == 1
    assert sum(1 for module, _alias in plain_imports if module == "weakref") == 1
    allowed_from_imports = {
        "__future__": {"annotations"},
        "collections": {"OrderedDict"},
        "dataclasses": {"dataclass"},
        "enum": {"StrEnum"},
        "pathlib": {"Path"},
        "vibecad.execution.revisions": {
            "LocalRevisionStore",
            "ProjectHead",
            "ReconciliationResult",
            "ReconciliationStatus",
            "RevisionRef",
            "RevisionStoreError",
            "RevisionStoreErrorCode",
            "_candidate_file_limit",
            "_reserve_candidate_revision",
            "_validate_candidate_reservation",
        },
        "vibecad.validation": {
            "CompiledAcceptance",
            "ObservationSnapshot",
            "ValidationError",
            "VerificationReceipt",
            "consume_verification_receipt",
        },
        "vibecad.workflow.errors": {"MAX_SAFE_JSON_INTEGER", "SCHEMA_VERSION"},
        "vibecad.workflow.lease": {"ProjectWriteLease"},
        "vibecad.workflow.state": {"VerificationReport"},
    }
    assert relative_imports == 0
    assert imported_modules <= allowed_plain_imports | set(allowed_from_imports)
    for module, name, alias, level in from_imports:
        assert level == 0
        assert module in allowed_from_imports
        assert name in allowed_from_imports[module]
        private_revision_boundary = module == "vibecad.execution.revisions" and name in {
            "_candidate_file_limit",
            "_reserve_candidate_revision",
            "_validate_candidate_reservation",
        }
        assert name != "*" and (not name.startswith("_") or private_revision_boundary)
        if module == "__future__":
            assert alias is None
        else:
            assert alias is not None and alias.startswith("_") and not alias.startswith("__")
            assert alias != name or private_revision_boundary
    assert len(set(from_imports)) == len(from_imports)
    imported_from_pairs = [(module, name) for module, name, _alias, _level in from_imports]
    assert len(imported_from_pairs) == len(set(imported_from_pairs))
    assert (
        sum(
            1
            for module, name, alias, level in from_imports
            if module == "__future__" and name == "annotations" and alias is None and level == 0
        )
        == 1
    )

    imported_alias_list = [alias for _module, alias in plain_imports] + [
        alias for _module, _name, alias, _level in from_imports if alias is not None
    ]
    assert len(imported_alias_list) == len(set(imported_alias_list))
    imported_aliases = set(imported_alias_list)
    top_level_callable_list = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert len(top_level_callable_list) == len(set(top_level_callable_list))
    top_level_callables = set(top_level_callable_list)
    assert imported_aliases.isdisjoint(top_level_callables)
    protected_builtin_names = {
        "Exception",
        "NotImplementedError",
        "TypeError",
        "ValueError",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "id",
        "int",
        "isinstance",
        "len",
        "list",
        "min",
        "object",
        "range",
        "set",
        "str",
        "super",
        "tuple",
        "type",
        "zip",
    }
    protected_names = imported_aliases | top_level_callables | protected_builtin_names
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            assert node.id not in protected_names
        elif isinstance(node, ast.arg):
            assert node.arg not in protected_names
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            raise AssertionError("global and nonlocal state are forbidden")
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            assert node.name not in protected_names
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name is not None:
            assert node.name not in protected_names
        elif isinstance(node, ast.MatchMapping) and node.rest is not None:
            assert node.rest not in protected_names
        elif (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node not in tree.body
        ):
            assert node.name not in protected_names

    dynamic_surface_names = {
        "__delattr__",
        "__dir__",
        "__getattr__",
        "__getattribute__",
        "__setattr__",
    }
    implicit_protocol_definitions = {
        "__bool__",
        "__bytes__",
        "__contains__",
        "__format__",
        "__fspath__",
        "__getitem__",
        "__iter__",
        "__len__",
        "__repr__",
        "__str__",
    }
    assert definitions.isdisjoint(dynamic_surface_names | implicit_protocol_definitions)
    assert not any(isinstance(node, ast.Lambda) for node in ast.walk(tree))
    assert not any(
        isinstance(node, (ast.NamedExpr, ast.Match, ast.Yield, ast.YieldFrom))
        for node in ast.walk(tree)
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            assert not node.keywords

    def immutable_literal(node: ast.AST) -> bool:
        return isinstance(node, ast.Constant) or (
            isinstance(node, ast.Tuple) and all(immutable_literal(item) for item in node.elts)
        )

    module_constant_names: set[str] = set()
    allowed_module_statements = (
        ast.Import,
        ast.ImportFrom,
        ast.Assign,
        ast.ClassDef,
        ast.FunctionDef,
    )
    for statement in tree.body:
        assert isinstance(statement, allowed_module_statements) or (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and type(statement.value.value) is str
        )
        if isinstance(statement, ast.Assign):
            assert len(statement.targets) == 1
            target = statement.targets[0]
            assert isinstance(target, ast.Name)
            assert target.id == "__all__" or target.id.startswith("_")
            assert immutable_literal(statement.value)
            module_constant_names.add(target.id)

    imported_constant_aliases = {
        alias
        for module, name, alias, _level in from_imports
        if module == "vibecad.workflow.errors"
        and name in {"MAX_SAFE_JSON_INTEGER", "SCHEMA_VERSION"}
        and alias is not None
    }
    dataclass_aliases = {
        alias
        for module, name, alias, _level in from_imports
        if module == "dataclasses" and name == "dataclass" and alias is not None
    }
    strenum_aliases = {
        alias
        for module, name, alias, _level in from_imports
        if module == "enum" and name == "StrEnum" and alias is not None
    }

    def safe_class_value(node: ast.AST) -> bool:
        if immutable_literal(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in module_constant_names | imported_constant_aliases
        return False

    top_level_private_classes = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.startswith("_")
    }
    allowed_class_statements = (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.Pass)
    for class_node in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        assert len(class_node.bases) <= 1
        for base in class_node.bases:
            assert isinstance(base, ast.Name)
            assert (
                base.id
                in {
                    "Exception",
                    "ValueError",
                    "object",
                }
                | strenum_aliases
                | top_level_private_classes
            )
        for decorator in class_node.decorator_list:
            decorator_name = decorator.func if isinstance(decorator, ast.Call) else decorator
            assert isinstance(decorator_name, ast.Name)
            assert decorator_name.id in dataclass_aliases
            if isinstance(decorator, ast.Call):
                assert decorator.args == []
                assert all(immutable_literal(keyword.value) for keyword in decorator.keywords)
        for statement in class_node.body:
            assert isinstance(statement, allowed_class_statements) or (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and type(statement.value.value) is str
            )
            if isinstance(statement, ast.Assign):
                assert len(statement.targets) == 1
                assert isinstance(statement.targets[0], ast.Name)
                assert statement.targets[0].id not in dynamic_surface_names
                if statement.targets[0].id.startswith("__"):
                    assert statement.targets[0].id == "__slots__"
                assert safe_class_value(statement.value)
            elif isinstance(statement, ast.AnnAssign):
                assert isinstance(statement.target, ast.Name)
                assert statement.target.id not in dynamic_surface_names
                assert not statement.target.id.startswith("__")
                assert statement.value is None or safe_class_value(statement.value)
            elif isinstance(statement, ast.AugAssign):
                raise AssertionError("mutable class-level state is forbidden")
            if isinstance(statement, ast.FunctionDef):
                if statement.name.startswith("__") and statement.name.endswith("__"):
                    assert statement.name in {
                        "__copy__",
                        "__deepcopy__",
                        "__enter__",
                        "__exit__",
                        "__init__",
                        "__post_init__",
                        "__reduce__",
                        "__reduce_ex__",
                    }
                    if statement.name == "__init__":
                        assert class_node.name in {
                            "CandidateError",
                            "_CandidateFileLimit",
                            "SessionSlot",
                            "CandidateCoordinator",
                        }
                    elif statement.name == "__post_init__":
                        assert class_node.name in set(EXPECTED_VALUE_FIELDS)
                    else:
                        assert class_node.name.startswith("_")

    identity_value_names = set(EXPECTED_VALUE_FIELDS)
    for class_node in (
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name in identity_value_names
    ):
        dataclass_decorators = [
            decorator
            for decorator in class_node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id in dataclass_aliases
        ]
        assert len(dataclass_decorators) == 1
        options = {keyword.arg: keyword.value for keyword in dataclass_decorators[0].keywords}
        for option in ("eq", "repr"):
            value = options.get(option)
            assert isinstance(value, ast.Constant) and value.value is False
        for option in ("frozen", "kw_only", "slots"):
            value = options.get(option)
            assert isinstance(value, ast.Constant) and value.value is True
        if class_node.name == "SessionBinding":
            value = options.get("weakref_slot")
            assert isinstance(value, ast.Constant) and value.value is True
        assert not any(
            isinstance(statement, ast.FunctionDef)
            and statement.name in {"__format__", "__repr__", "__str__"}
            for statement in class_node.body
        )

    for function_node in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        assert not isinstance(function_node, ast.AsyncFunctionDef)
        assert function_node.decorator_list == []
        assert isinstance(parents[function_node], (ast.Module, ast.ClassDef))
        defaults = list(function_node.args.defaults) + [
            default for default in function_node.args.kw_defaults if default is not None
        ]
        assert all(immutable_literal(default) for default in defaults)

    allowed_module_attributes = {
        "os": {"getpid"},
        "re": {"fullmatch"},
        "resource": {"RLIMIT_FSIZE", "RLIM_INFINITY", "getrlimit", "setrlimit"},
        "secrets": {"token_hex"},
        "signal": {"SIGXFSZ", "SIG_IGN", "getsignal", "signal"},
        "threading": {"RLock", "current_thread", "get_ident", "main_thread"},
        "weakref": {"WeakSet"},
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = module_aliases.get(node.value.id)
            if module is not None:
                assert node.attr in allowed_module_attributes[module]
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in module_aliases
        ):
            parent = parents[node]
            assert isinstance(parent, ast.Attribute)
            assert parent.value is node

    def enclosing_class_name(node: ast.AST) -> str | None:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, ast.ClassDef):
                return current.name
            current = parents.get(current)
        return None

    def enclosing_function_node(node: ast.AST) -> ast.FunctionDef | None:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, ast.FunctionDef):
                return current
            current = parents.get(current)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            if isinstance(node.value, ast.Name) and node.value.id == "_CandidateFileLimitRuntime":
                assert node.attr in {"_initialized_pid", "_poisoned_pid"}
                continue
            assert isinstance(node.value, ast.Name) and node.value.id == "self"
            owner = enclosing_class_name(node)
            assert owner in {
                "_CandidateFileLimit",
                "CandidateError",
                "SessionSlot",
                "CandidateCoordinator",
            }
            if owner == "CandidateError":
                assert node.attr in {
                    "args",
                    "schema_version",
                    "code",
                    "message",
                    "head_committed",
                    "cleanup_required",
                    "recovery_required",
                }
            else:
                assert node.attr.startswith("_") and not node.attr.startswith("__")
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            raise AssertionError("subscript state mutation is forbidden")

    top_level_classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def class_method(class_name: str, method_name: str) -> ast.FunctionDef:
        matches = [
            statement
            for statement in top_level_classes[class_name].body
            if isinstance(statement, ast.FunctionDef) and statement.name == method_name
        ]
        assert len(matches) == 1
        return matches[0]

    def constructor_field(class_name: str, parameter_name: str) -> str:
        initializer = class_method(class_name, "__init__")
        matches: list[str] = []
        assert not any(
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id == parameter_name
            for node in ast.walk(initializer)
        )
        for node in initializer.body:
            target = None
            value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                value = node.value
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and isinstance(value, ast.Name)
                and value.id == parameter_name
            ):
                matches.append(target.attr)
        assert len(matches) == 1
        assert matches[0].startswith("_") and not matches[0].startswith("__")
        return matches[0]

    coordinator_boundary_fields = {
        "store": constructor_field("CandidateCoordinator", "store"),
        "snapshot_port": constructor_field("CandidateCoordinator", "snapshot_port"),
        "session_slot": constructor_field("CandidateCoordinator", "session_slot"),
    }
    assert len(set(coordinator_boundary_fields.values())) == 3
    slot_current_field = constructor_field("SessionSlot", "initial")

    def attribute_stores(class_name: str, attribute: str) -> list[ast.Attribute]:
        return [
            node
            for node in ast.walk(top_level_classes[class_name])
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and node.attr == attribute
        ]

    for field_name in coordinator_boundary_fields.values():
        assert len(attribute_stores("CandidateCoordinator", field_name)) == 1

    threading_alias = next(alias for module, alias in plain_imports if module == "threading")

    def lock_field(class_name: str) -> str:
        initializer = class_method(class_name, "__init__")
        matches: list[str] = []
        for node in ast.walk(initializer):
            target = None
            value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                value = node.value
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "RLock"
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id == threading_alias
                and value.args == []
                and value.keywords == []
            ):
                matches.append(target.attr)
        assert len(matches) == 1
        assert matches[0].startswith("_") and not matches[0].startswith("__")
        assert len(attribute_stores(class_name, matches[0])) == 1
        return matches[0]

    slot_lock_field = lock_field("SessionSlot")
    coordinator_lock_field = lock_field("CandidateCoordinator")
    assert coordinator_lock_field not in coordinator_boundary_fields.values()

    def method_lock_blocks(method: ast.FunctionDef, field_name: str) -> list[ast.With]:
        return [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.With)
            and len(node.items) == 1
            and node.items[0].optional_vars is None
            and isinstance(node.items[0].context_expr, ast.Attribute)
            and node.items[0].context_expr.attr == field_name
            and isinstance(node.items[0].context_expr.value, ast.Name)
            and node.items[0].context_expr.value.id == "self"
        ]

    def node_is_within(node: ast.AST, container: ast.AST) -> bool:
        current = parents.get(node)
        while current is not None:
            if current is container:
                return True
            current = parents.get(current)
        return False

    def assert_method_uses_one_lock(class_name: str, method_name: str, field_name: str) -> None:
        method = class_method(class_name, method_name)
        assert len(method_lock_blocks(method, field_name)) == 1

    for locked_method in ("current", "compare_and_set", "_retire"):
        assert_method_uses_one_lock("SessionSlot", locked_method, slot_lock_field)
    for locked_method in (
        "begin",
        "reserve_candidate",
        "begin_reserved",
        "begin_seeded_reserved",
        "cancel_reservation",
        "checkpoint",
        "adopt_materialized",
        "seal",
        "reopen_review",
        "prepare_review",
        "discard_review",
        "publish_review",
        "commit",
        "rollback",
        "reconcile",
    ):
        assert_method_uses_one_lock("CandidateCoordinator", locked_method, coordinator_lock_field)

    ordered_dict_aliases = {
        alias
        for module, name, alias, _level in from_imports
        if module == "collections" and name == "OrderedDict" and alias is not None
    }

    def is_collection_constructor(node: ast.AST) -> bool:
        if isinstance(node, (ast.Dict, ast.List, ast.Set)):
            return True
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in {"dict", "list", "set"} | ordered_dict_aliases
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            return (
                module_aliases.get(node.func.value.id) == "weakref" and node.func.attr == "WeakSet"
            )
        return False

    collection_fields: dict[str, set[str]] = {
        "SessionSlot": set(),
        "CandidateCoordinator": set(),
    }
    slot_weak_retirement_fields: list[str] = []
    for owner in ("SessionSlot", "CandidateCoordinator"):
        initializer = class_method(owner, "__init__")
        for node in ast.walk(initializer):
            target = None
            value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                target = node.target
                value = node.value
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and is_collection_constructor(value)
            ):
                collection_fields[owner].add(target.attr)
                assert target.attr.startswith("_") and not target.attr.startswith("__")
                assert len(attribute_stores(owner, target.attr)) == 1
                if (
                    owner == "SessionSlot"
                    and isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and isinstance(value.func.value, ast.Name)
                    and module_aliases.get(value.func.value.id) == "weakref"
                    and value.func.attr == "WeakSet"
                    and value.args == []
                    and value.keywords == []
                ):
                    slot_weak_retirement_fields.append(target.attr)

    assert len(slot_weak_retirement_fields) == 1
    slot_weak_retirement_field = slot_weak_retirement_fields[0]
    assert collection_fields["SessionSlot"] == {slot_weak_retirement_field}
    assert {
        node.attr
        for node in ast.walk(top_level_classes["SessionSlot"])
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Store)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    } <= {slot_current_field, slot_lock_field, slot_weak_retirement_field}

    def slot_collection_calls(operation: str) -> list[ast.Call]:
        return [
            node
            for node in ast.walk(top_level_classes["SessionSlot"])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == operation
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.value.attr == slot_weak_retirement_field
        ]

    assert slot_collection_calls("add")
    retired_membership_checks = [
        node
        for node in ast.walk(top_level_classes["SessionSlot"])
        if isinstance(node, ast.Compare)
        and any(isinstance(operator, (ast.In, ast.NotIn)) for operator in node.ops)
        and any(
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "self"
            and value.attr == slot_weak_retirement_field
            for value in (node.left, *node.comparators)
        )
    ]
    assert retired_membership_checks

    def assert_self_state_is_locked(class_name: str, field_name: str) -> None:
        for method in (
            statement
            for statement in top_level_classes[class_name].body
            if isinstance(statement, ast.FunctionDef) and statement.name != "__init__"
        ):
            locked_blocks = method_lock_blocks(method, field_name)
            state_attributes = [
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ]
            lock_contexts = {block.items[0].context_expr for block in locked_blocks}
            for attribute in state_attributes:
                if attribute.attr == field_name:
                    assert attribute in lock_contexts
                else:
                    assert len(locked_blocks) == 1
                    assert node_is_within(attribute, locked_blocks[0])

    assert_self_state_is_locked("SessionSlot", slot_lock_field)
    assert_self_state_is_locked("CandidateCoordinator", coordinator_lock_field)

    safe_builtin_calls = protected_builtin_names - {"Exception"}
    trusted_callable_imports = {
        ("collections", "OrderedDict"),
        ("dataclasses", "dataclass"),
        ("vibecad.execution.revisions", "_candidate_file_limit"),
        ("vibecad.execution.revisions", "_reserve_candidate_revision"),
        ("vibecad.execution.revisions", "_validate_candidate_reservation"),
        ("vibecad.validation", "consume_verification_receipt"),
    }
    trusted_import_calls = {
        alias
        for module, name, alias, _level in from_imports
        if (module, name) in trusted_callable_imports and alias is not None
    }
    store_calls = {
        "begin_revision",
        "candidate_artifact_path",
        "candidate_model_path",
        "commit_revision",
        "load_head",
        "load_revision",
        "prepare_revision",
        "reconcile",
        "revision_model_path",
        "rollback_revision",
        "seed_candidate_from_revision",
        "seal_revision",
        "validate_candidate_payload",
        "validate_project_write_lease",
    }
    snapshot_port_calls = {"checkpoint_fcstd", "close", "create_empty", "load_fcstd"}
    session_slot_calls = {"compare_and_set", "current", "_retire"}
    boundary_receivers = {
        **{name: coordinator_boundary_fields["store"] for name in store_calls},
        **{name: coordinator_boundary_fields["snapshot_port"] for name in snapshot_port_calls},
        **{name: coordinator_boundary_fields["session_slot"] for name in session_slot_calls},
    }
    coordinator_public_methods = (
        "begin",
        "reserve_candidate",
        "begin_reserved",
        "begin_seeded_reserved",
        "cancel_reservation",
        "checkpoint",
        "adopt_materialized",
        "seal",
        "reopen_review",
        "prepare_review",
        "discard_review",
        "publish_review",
        "commit",
        "rollback",
        "reconcile",
    )
    coordinator_session_helpers = {
        "_create_empty_session",
        "_load_fcstd_session",
    }
    for method_name in coordinator_public_methods:
        method = class_method("CandidateCoordinator", method_name)
        parameter_names = {argument.arg for argument in method.args.args + method.args.kwonlyargs}
        for node in ast.walk(method):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                assert node.id not in parameter_names

    store_path_calls = {
        "candidate_artifact_path",
        "candidate_model_path",
        "revision_model_path",
    }

    def direct_store_path_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in store_path_calls
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"
            and node.func.value.attr == coordinator_boundary_fields["store"]
        )

    trusted_path_names: set[tuple[ast.FunctionDef, str]] = set()
    changed = True
    while changed:
        changed = False
        for function_node in (
            node
            for node in ast.walk(top_level_classes["CandidateCoordinator"])
            if isinstance(node, ast.FunctionDef)
        ):
            for node in ast.walk(function_node):
                target = None
                value = None
                if isinstance(node, ast.Assign) and len(node.targets) == 1:
                    target = node.targets[0]
                    value = node.value
                elif isinstance(node, ast.AnnAssign):
                    target = node.target
                    value = node.value
                if not isinstance(target, ast.Name):
                    continue
                trusted_value = direct_store_path_call(value) or (
                    isinstance(value, ast.Name) and (function_node, value.id) in trusted_path_names
                )
                key = (function_node, target.id)
                if trusted_value and key not in trusted_path_names:
                    trusted_path_names.add(key)
                    changed = True
    for function_node, name in trusted_path_names:
        assert (
            sum(
                1
                for node in ast.walk(function_node)
                if isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id == name
            )
            == 1
        )

    def call_argument(node: ast.Call, position: int, name: str) -> ast.AST:
        positional = node.args[position] if len(node.args) > position else None
        keywords = [keyword.value for keyword in node.keywords if keyword.arg == name]
        assert not (positional is not None and keywords)
        assert len(keywords) <= 1
        result = positional if positional is not None else (keywords[0] if keywords else None)
        assert result is not None
        return result

    def trusted_load_path(node: ast.AST, function_node: ast.FunctionDef) -> bool:
        if (
            function_node
            is class_method(
                "CandidateCoordinator",
                "_load_fcstd_session",
            )
            and isinstance(node, ast.Name)
            and node.id == "path"
        ):
            return True
        if direct_store_path_call(node):
            return True
        if isinstance(node, ast.Name):
            return (function_node, node.id) in trusted_path_names
        return (
            function_node.name in {"checkpoint", "adopt_materialized"}
            and isinstance(node, ast.Attribute)
            and node.attr == "model_path"
            and isinstance(node.value, ast.Name)
            and node.value.id == "candidate"
        )

    collection_calls = {
        "add",
        "append",
        "clear",
        "discard",
        "get",
        "items",
        "keys",
        "move_to_end",
        "pop",
        "popitem",
        "remove",
        "setdefault",
        "update",
        "values",
    }
    for node in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if isinstance(node.func, ast.Name):
            assert (
                node.func.id in top_level_callables
                or node.func.id in safe_builtin_calls
                or node.func.id in trusted_import_calls
            )
            if node.func.id == "type":
                assert len(node.args) == 1 and node.keywords == []
            continue
        assert isinstance(node.func, ast.Attribute)
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in module_aliases:
            module = module_aliases[receiver.id]
            assert node.func.attr in allowed_module_attributes[module]
            continue
        if node.func.attr in boundary_receivers:
            assert enclosing_class_name(node) == "CandidateCoordinator"
            assert isinstance(receiver, ast.Attribute)
            assert receiver.attr == boundary_receivers[node.func.attr]
            assert isinstance(receiver.value, ast.Name) and receiver.value.id == "self"
            if node.func.attr == "checkpoint_fcstd":
                function_node = enclosing_function_node(node)
                assert function_node is class_method("CandidateCoordinator", "checkpoint")
                assert len(node.args) + len(node.keywords) == 2
                session_argument = call_argument(node, 0, "session")
                assert isinstance(session_argument, ast.Attribute)
                assert session_argument.attr == "session"
                binding_argument = session_argument.value
                assert isinstance(binding_argument, ast.Attribute)
                assert binding_argument.attr == "binding"
                assert isinstance(binding_argument.value, ast.Name)
                assert binding_argument.value.id == "candidate"
                path_argument = call_argument(node, 1, "path")
                assert trusted_load_path(path_argument, function_node)
            elif node.func.attr == "create_empty":
                function_node = enclosing_function_node(node)
                assert function_node is class_method(
                    "CandidateCoordinator",
                    "_create_empty_session",
                )
            elif node.func.attr == "load_fcstd":
                function_node = enclosing_function_node(node)
                assert function_node is class_method(
                    "CandidateCoordinator",
                    "_load_fcstd_session",
                )
                assert len(node.args) + len(node.keywords) == 1
                path_argument = call_argument(node, 0, "path")
                assert trusted_load_path(path_argument, function_node)
            continue
        if node.func.attr in collection_calls:
            owner = enclosing_class_name(node)
            assert owner in {"SessionSlot", "CandidateCoordinator"}
            assert isinstance(receiver, ast.Attribute)
            assert receiver.attr in collection_fields[owner]
            assert isinstance(receiver.value, ast.Name) and receiver.value.id == "self"
            continue
        if node.func.attr == "rollback":
            assert enclosing_class_name(node) == "CandidateCoordinator"
            assert enclosing_function_node(node) is class_method(
                "CandidateCoordinator",
                "publish_review",
            )
            assert isinstance(receiver, ast.Name)
            assert receiver.id == "CandidateCoordinator"
            assert len(node.args) == 1
            assert isinstance(node.args[0], ast.Name) and node.args[0].id == "self"
            continue
        if enclosing_class_name(node) == "_CandidateFileLimit" and node.func.attr in {
            "acquire",
            "release",
        }:
            assert isinstance(receiver, (ast.Attribute, ast.Name))
            continue
        if node.func.attr in {"begin_reserved", "reserve_candidate"}:
            function_node = enclosing_function_node(node)
            if node.func.attr == "reserve_candidate":
                assert function_node is class_method(
                    "CandidateCoordinator",
                    "begin",
                )
            else:
                assert function_node in {
                    class_method("CandidateCoordinator", "begin"),
                    class_method("CandidateCoordinator", "begin_seeded_reserved"),
                }
            assert isinstance(receiver, ast.Name) and receiver.id == "self"
            continue
        if node.func.attr in coordinator_session_helpers:
            function_node = enclosing_function_node(node)
            assert function_node is not None
            assert function_node.name in coordinator_public_methods
            assert isinstance(receiver, ast.Name) and receiver.id == "self"
            if node.func.attr == "_load_fcstd_session":
                assert len(node.args) + len(node.keywords) == 1
                path_argument = call_argument(node, 0, "path")
                assert trusted_load_path(path_argument, function_node)
            continue
        assert node.func.attr.startswith("_") and not node.func.attr.startswith("__")
        assert enclosing_class_name(node) in {"SessionSlot", "CandidateCoordinator"}
        assert isinstance(receiver, ast.Name) and receiver.id == "self"

    session_names = {
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg) and node.arg == "session"
    }

    def expression_mentions_session(node: ast.AST | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Name) and node.id in session_names:
            return True
        if isinstance(node, ast.Attribute) and node.attr == "session":
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"SessionBinding", "id", "isinstance", "type"}
        ):
            return False
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"create_empty", "load_fcstd"}
        ):
            return True
        return any(expression_mentions_session(child) for child in ast.iter_child_nodes(node))

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            value = None
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            if not expression_mentions_session(value):
                continue
            assert len(targets) == 1 and isinstance(targets[0], ast.Name)
            target = targets[0]
            if target.id not in session_names:
                session_names.add(target.id)
                changed = True

    safe_session_call_names = {"SessionBinding", "id", "isinstance", "type"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not any(
                keyword.arg is None and expression_mentions_session(keyword.value)
                for keyword in node.keywords
            )
            supplied = list(node.args) + [keyword.value for keyword in node.keywords]
            if any(expression_mentions_session(value) for value in supplied):
                if isinstance(node.func, ast.Name):
                    assert node.func.id in safe_session_call_names
                else:
                    assert isinstance(node.func, ast.Attribute)
                    assert node.func.attr in {"checkpoint_fcstd", "close"}
                    receiver = node.func.value
                    assert isinstance(receiver, ast.Attribute)
                    assert receiver.attr == coordinator_boundary_fields["snapshot_port"]
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(expression_mentions_session(value) for value in operands):
                assert all(isinstance(operator, (ast.Is, ast.IsNot)) for operator in node.ops)
        elif isinstance(node, ast.JoinedStr):
            assert not expression_mentions_session(node)
        elif isinstance(node, ast.Attribute):
            assert not expression_mentions_session(node.value)
        elif isinstance(node, ast.With):
            assert all(not expression_mentions_session(item.context_expr) for item in node.items)
        elif isinstance(node, (ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Subscript, ast.Starred)):
            assert not expression_mentions_session(node)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            assert not expression_mentions_session(node.iter)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            assert all(
                not expression_mentions_session(generator.iter) for generator in node.generators
            )
        elif isinstance(node, ast.Dict):
            assert all(not expression_mentions_session(key) for key in node.keys)
        elif isinstance(node, ast.Set):
            assert all(not expression_mentions_session(value) for value in node.elts)
        elif isinstance(node, ast.Return):
            if expression_mentions_session(node.value):
                function_node = enclosing_function_node(node)
                assert function_node is not None
                assert function_node.name in coordinator_session_helpers

    def safe_session_truth_test(node: ast.AST) -> bool:
        if not expression_mentions_session(node):
            return True
        if isinstance(node, ast.Compare):
            return all(isinstance(operator, (ast.Is, ast.IsNot)) for operator in node.ops)
        if isinstance(node, ast.BoolOp):
            return all(safe_session_truth_test(value) for value in node.values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id in {"id", "isinstance", "type"}
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            assert safe_session_truth_test(node.test)
    assert attributes.isdisjoint(
        {
            "_active_part",
            "_checkpoint",
            "_checkpoint_dir",
            "_doc",
            "_labels",
            "_loaded",
            "_parts",
            "_redo_active_parts",
            "_redo_result_roots",
            "_redo_revisions",
            "_replace_document",
            "_result_roots",
            "_revision_id",
            "_saved_active_part",
            "_saved_revision_id",
            "_saved_result_roots",
            "_undo_active_parts",
            "_undo_result_roots",
            "_undo_revisions",
            "__dict__",
            "__base__",
            "__bases__",
            "__annotations__",
            "__class__",
            "__closure__",
            "__code__",
            "__func__",
            "__getattribute__",
            "__globals__",
            "__loader__",
            "__mro__",
            "__self__",
            "__spec__",
            "__subclasses__",
            "active_part",
            "assert_valid_solid",
            "clear_result_object",
            "close_document",
            "doc",
            "get_assembly_shape",
            "get_object",
            "get_result_object",
            "get_result_shape",
            "is_dirty",
            "load_document",
            "mark_saved",
            "new_part",
            "open_document",
            "owner_of",
            "part_names",
            "persist_state",
            "refresh_model_state",
            "resolve_edge",
            "resolve_face",
            "restore_roots_for_redo",
            "restore_roots_for_undo",
            "set_active_part",
            "set_labels",
            "set_result_object",
            "closed",
            "name",
            "payload",
            "import_module",
            "create_module",
            "exec_module",
            "find_spec",
            "load_module",
            "loader",
            "spec",
            "chmod",
            "copy",
            "copy_into",
            "hardlink_to",
            "lchmod",
            "link_to",
            "mkdir",
            "move",
            "move_into",
            "open",
            "popen",
            "rename",
            "replace",
            "rmdir",
            "saveAs",
            "saveCopy",
            "system",
            "symlink_to",
            "touch",
            "unlink",
            "write_bytes",
            "write",
            "write_text",
        }
    )
    assert not any(attribute.startswith("__") for attribute in attributes)
    assert calls.isdisjoint(
        {
            "__import__",
            "compile",
            "eval",
            "exec",
            "getattr",
            "globals",
            "locals",
            "open",
            "setattr",
            "vars",
        }
    )
    assert names.isdisjoint(
        {
            "__annotations__",
            "__builtins__",
            "__import__",
            "compile",
            "eval",
            "exec",
            "getattr",
            "globals",
            "locals",
            "open",
            "setattr",
            "vars",
        }
    )
    assert subscript_calls == 0
    assert dynamic_calls == 0
    assert definitions.isdisjoint({"from_mapping", "to_mapping"})
    assert {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    } == set(EXPECTED_CANDIDATE_EXPORTS)
    assert {
        name
        for name, value in vars(candidate_module).items()
        if not name.startswith("_") and callable(value)
    } == set(EXPECTED_CANDIDATE_EXPORTS)
