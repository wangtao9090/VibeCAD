"""Isolated CAD candidate lifecycle and exact-once revision coordination."""

from __future__ import annotations

import re as _re
import threading as _threading
import weakref as _weakref
from collections import OrderedDict as _OrderedDict
from dataclasses import dataclass as _dataclass
from enum import StrEnum as _StrEnum
from pathlib import Path as _Path

from vibecad.execution.revisions import LocalRevisionStore as _LocalRevisionStore
from vibecad.execution.revisions import ProjectHead as _ProjectHead
from vibecad.execution.revisions import ReconciliationResult as _ReconciliationResult
from vibecad.execution.revisions import ReconciliationStatus as _ReconciliationStatus
from vibecad.execution.revisions import RevisionRef as _RevisionRef
from vibecad.execution.revisions import RevisionStoreError as _RevisionStoreError
from vibecad.execution.revisions import RevisionStoreErrorCode as _RevisionStoreErrorCode
from vibecad.validation import CompiledAcceptance as _CompiledAcceptance
from vibecad.validation import ObservationSnapshot as _ObservationSnapshot
from vibecad.validation import ValidationError as _ValidationError
from vibecad.validation import VerificationReceipt as _VerificationReceipt
from vibecad.validation import consume_verification_receipt as _consume_verification_receipt
from vibecad.workflow.errors import SCHEMA_VERSION as _SCHEMA_VERSION
from vibecad.workflow.lease import ProjectWriteLease as _ProjectWriteLease
from vibecad.workflow.state import VerificationReport as _VerificationReport

__all__ = (
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

_PROJECT_PATTERN = "project_[0-9a-f]{32}"
_REVISION_PATTERN = "revision_[0-9a-f]{32}"
_TERMINAL_REPLAY_LIMIT = 32


class CandidateErrorCode(_StrEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_IDENTIFIER = "invalid_identifier"
    INVALID_CANDIDATE = "invalid_candidate"
    INVALID_TRANSITION = "invalid_transition"
    INVALID_BINDING = "invalid_binding"
    INVALID_LEASE = "invalid_lease"
    SESSION_ALIAS = "session_alias"
    TERMINAL_IN_PROGRESS = "terminal_in_progress"
    ALREADY_TERMINAL = "already_terminal"
    RECEIPT_REJECTED = "receipt_rejected"
    CAD_FAILURE = "cad_failure"
    STORE_FAILURE = "store_failure"
    CONFLICT = "conflict"
    CLEANUP_REQUIRED = "cleanup_required"
    RECOVERY_REQUIRED = "recovery_required"


class CandidateCommitStatus(_StrEnum):
    COMMITTED = "committed"
    COMMITTED_CLEANUP_REQUIRED = "committed_cleanup_required"
    COMMITTED_RECOVERY_REQUIRED = "committed_recovery_required"


class CandidateRollbackStatus(_StrEnum):
    NOT_COMMITTED = "not_committed"
    CLEANUP_REQUIRED = "cleanup_required"
    RECOVERY_REQUIRED = "recovery_required"


class CandidateReconcileStatus(_StrEnum):
    CLEAN = "clean"
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"
    CLEANUP_REQUIRED = "cleanup_required"
    RECOVERY_REQUIRED = "recovery_required"


def _error_message(code):
    if code is CandidateErrorCode.INVALID_INPUT:
        return "The candidate input is invalid."
    if code is CandidateErrorCode.INVALID_IDENTIFIER:
        return "The candidate identifier is invalid."
    if code is CandidateErrorCode.INVALID_CANDIDATE:
        return "The candidate capability is invalid."
    if code is CandidateErrorCode.INVALID_TRANSITION:
        return "The candidate transition is invalid."
    if code is CandidateErrorCode.INVALID_BINDING:
        return "The session binding is invalid."
    if code is CandidateErrorCode.INVALID_LEASE:
        return "The project lease is invalid."
    if code is CandidateErrorCode.SESSION_ALIAS:
        return "The candidate session is not isolated."
    if code is CandidateErrorCode.TERMINAL_IN_PROGRESS:
        return "A terminal candidate operation is in progress."
    if code is CandidateErrorCode.ALREADY_TERMINAL:
        return "The candidate is already terminal."
    if code is CandidateErrorCode.RECEIPT_REJECTED:
        return "The verification receipt was rejected."
    if code is CandidateErrorCode.CAD_FAILURE:
        return "The CAD candidate operation failed."
    if code is CandidateErrorCode.STORE_FAILURE:
        return "The candidate revision operation failed."
    if code is CandidateErrorCode.CONFLICT:
        return "The candidate conflicts with current state."
    if code is CandidateErrorCode.CLEANUP_REQUIRED:
        return "Candidate cleanup requires attention."
    if code is CandidateErrorCode.RECOVERY_REQUIRED:
        return "Candidate recovery requires attention."
    raise TypeError("code must be a CandidateErrorCode")


class CandidateError(ValueError):
    __slots__ = (
        "schema_version",
        "code",
        "message",
        "head_committed",
        "cleanup_required",
        "recovery_required",
    )

    def __init__(
        self,
        code: CandidateErrorCode,
        *,
        head_committed: bool = False,
        cleanup_required: bool = False,
        recovery_required: bool = False,
    ) -> None:
        if type(code) is not CandidateErrorCode:
            raise TypeError("code must be a CandidateErrorCode")
        if type(head_committed) is not bool:
            raise TypeError("head_committed must be a bool")
        if type(cleanup_required) is not bool:
            raise TypeError("cleanup_required must be a bool")
        if type(recovery_required) is not bool:
            raise TypeError("recovery_required must be a bool")
        if code is CandidateErrorCode.CLEANUP_REQUIRED:
            if not cleanup_required or recovery_required:
                raise ValueError("cleanup flags do not match the error code")
        elif code is CandidateErrorCode.RECOVERY_REQUIRED:
            if cleanup_required or not recovery_required:
                raise ValueError("recovery flags do not match the error code")
        elif cleanup_required or recovery_required:
            raise ValueError("attention flags do not match the error code")
        self.schema_version = _SCHEMA_VERSION
        self.code = code
        self.message = _error_message(code)
        self.head_committed = head_committed
        self.cleanup_required = cleanup_required
        self.recovery_required = recovery_required
        self.args = (self.message,)


class CadSnapshotPort:
    def create_empty(self, *, revision_id: str) -> object:
        raise NotImplementedError("create_empty is not implemented")

    def load_fcstd(self, path: _Path) -> object:
        raise NotImplementedError("load_fcstd is not implemented")

    def checkpoint_fcstd(self, session: object, path: _Path) -> None:
        raise NotImplementedError("checkpoint_fcstd is not implemented")

    def close(self, session: object) -> None:
        raise NotImplementedError("close is not implemented")


def _canonical_identifier(value, pattern):
    return type(value) is str and _re.fullmatch(pattern, value) is not None


def _require_project(value):
    if not _canonical_identifier(value, _PROJECT_PATTERN):
        raise CandidateError(CandidateErrorCode.INVALID_IDENTIFIER)


def _require_revision(value):
    if not _canonical_identifier(value, _REVISION_PATTERN):
        raise CandidateError(CandidateErrorCode.INVALID_IDENTIFIER)


class _IdentityValue:
    __slots__ = ()

    def __copy__(self):
        raise TypeError("identity values cannot be copied")

    def __deepcopy__(self, memo):
        raise TypeError("identity values cannot be copied")

    def __reduce__(self):
        raise TypeError("identity values cannot be serialized")

    def __reduce_ex__(self, protocol):
        raise TypeError("identity values cannot be serialized")


@_dataclass(
    frozen=True,
    slots=True,
    kw_only=True,
    eq=False,
    repr=False,
    weakref_slot=True,
)
class SessionBinding(_IdentityValue):
    project_id: str
    revision_id: str
    session: object

    def __post_init__(self):
        _require_project(self.project_id)
        _require_revision(self.revision_id)
        if self.session is None:
            raise CandidateError(CandidateErrorCode.INVALID_BINDING)


def _validate_open_handle(project_id, base_head, binding, model_path, step_path):
    _require_project(project_id)
    if type(base_head) is not _ProjectHead:
        raise ValueError("base_head must be a ProjectHead")
    if type(binding) is not SessionBinding:
        raise ValueError("binding must be a SessionBinding")
    if not isinstance(model_path, _Path) or not isinstance(step_path, _Path):
        raise ValueError("candidate paths must be Paths")
    if base_head.project_id != project_id or binding.project_id != project_id:
        raise ValueError("candidate project bindings do not match")
    if binding.revision_id == base_head.revision_id or model_path == step_path:
        raise ValueError("candidate lineage is invalid")


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class ActiveCandidate(_IdentityValue):
    project_id: str
    base_head: _ProjectHead
    binding: SessionBinding
    model_path: _Path
    step_path: _Path

    def __post_init__(self):
        _validate_open_handle(
            self.project_id,
            self.base_head,
            self.binding,
            self.model_path,
            self.step_path,
        )


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class CheckpointedCandidate(_IdentityValue):
    project_id: str
    base_head: _ProjectHead
    binding: SessionBinding
    model_path: _Path
    step_path: _Path

    def __post_init__(self):
        _validate_open_handle(
            self.project_id,
            self.base_head,
            self.binding,
            self.model_path,
            self.step_path,
        )


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class SealedCandidate(_IdentityValue):
    project_id: str
    base_head: _ProjectHead
    revision: _RevisionRef
    binding: SessionBinding

    def __post_init__(self):
        _require_project(self.project_id)
        if type(self.base_head) is not _ProjectHead:
            raise ValueError("base_head must be a ProjectHead")
        if type(self.revision) is not _RevisionRef:
            raise ValueError("revision must be a RevisionRef")
        if type(self.binding) is not SessionBinding:
            raise ValueError("binding must be a SessionBinding")
        if (
            self.base_head.project_id != self.project_id
            or self.revision.project_id != self.project_id
            or self.binding.project_id != self.project_id
            or self.revision.base_revision != self.base_head.revision_id
            or self.binding.revision_id != self.revision.id
        ):
            raise ValueError("sealed candidate lineage is invalid")


def _exact_bool(value):
    return type(value) is bool


def _optional_bool(value):
    return value is None or type(value) is bool


def _validate_result_flags(
    schema_version,
    head_committed,
    slot_promoted,
    cleanup_required,
    recovery_required,
):
    if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
        raise ValueError("schema_version is invalid")
    if not _exact_bool(head_committed):
        raise ValueError("head_committed is invalid")
    if not _optional_bool(slot_promoted):
        raise ValueError("slot_promoted is invalid")
    if not _exact_bool(cleanup_required) or not _exact_bool(recovery_required):
        raise ValueError("result flags are invalid")


def _validate_cleanup_binding(live_binding, cleanup_binding, cleanup_required, project_id):
    if live_binding is not None and type(live_binding) is not SessionBinding:
        raise ValueError("live_binding is invalid")
    if cleanup_binding is not None and type(cleanup_binding) is not SessionBinding:
        raise ValueError("cleanup_binding is invalid")
    if cleanup_binding is not None:
        if not cleanup_required or cleanup_binding.project_id != project_id:
            raise ValueError("cleanup_binding is inconsistent")
        if live_binding is not None:
            if cleanup_binding.session is live_binding.session:
                raise ValueError("cleanup binding aliases the live Session")
    if live_binding is not None and live_binding.project_id != project_id:
        raise ValueError("live_binding has the wrong project")


def _status_flags(status, normal, cleanup, recovery, cleanup_required, recovery_required):
    if status is normal:
        return not cleanup_required and not recovery_required
    if status is cleanup:
        return cleanup_required and not recovery_required
    if status is recovery:
        return recovery_required
    return False


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class CandidateCommitResult(_IdentityValue):
    schema_version: int = _SCHEMA_VERSION
    status: CandidateCommitStatus
    head: _ProjectHead
    revision: _RevisionRef
    live_binding: SessionBinding | None
    report: _VerificationReport
    head_committed: bool
    slot_promoted: bool | None
    cleanup_required: bool
    recovery_required: bool
    cleanup_binding: SessionBinding | None

    def __post_init__(self):
        _validate_result_flags(
            self.schema_version,
            self.head_committed,
            self.slot_promoted,
            self.cleanup_required,
            self.recovery_required,
        )
        if type(self.status) is not CandidateCommitStatus:
            raise ValueError("status is invalid")
        if type(self.head) is not _ProjectHead or type(self.revision) is not _RevisionRef:
            raise ValueError("committed revision data is invalid")
        if type(self.report) is not _VerificationReport or not self.report.passed:
            raise ValueError("verification report is invalid")
        if not self.head_committed:
            raise ValueError("commit results require a committed HEAD")
        if not _status_flags(
            self.status,
            CandidateCommitStatus.COMMITTED,
            CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED,
            CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED,
            self.cleanup_required,
            self.recovery_required,
        ):
            raise ValueError("commit status flags are inconsistent")
        if self.status is CandidateCommitStatus.COMMITTED and self.slot_promoted is not True:
            raise ValueError("clean commit requires slot promotion")
        if (
            self.status is CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED
            and self.slot_promoted is not True
        ):
            raise ValueError("cleanup commit requires slot promotion")
        if self.live_binding is None and (
            not self.recovery_required or self.slot_promoted is not None
        ):
            raise ValueError("live binding is required")
        if (
            self.head.project_id != self.revision.project_id
            or self.head.revision_id != self.revision.id
            or self.head.manifest_sha256 != self.revision.manifest_sha256
            or self.report.candidate_revision != self.revision.id
            or self.report.manifest_sha256 != self.revision.manifest_sha256
        ):
            raise ValueError("commit result lineage is inconsistent")
        _validate_cleanup_binding(
            self.live_binding,
            self.cleanup_binding,
            self.cleanup_required,
            self.head.project_id,
        )
        if (
            self.cleanup_binding is not None
            and self.cleanup_binding.revision_id != self.revision.id
        ):
            raise ValueError("cleanup binding has the wrong revision")


def _validate_reconciliation_fields(head, live_binding, reconciliation, recovery_required):
    if head is not None and type(head) is not _ProjectHead:
        raise ValueError("head is invalid")
    if reconciliation is not None and type(reconciliation) is not _ReconciliationResult:
        raise ValueError("reconciliation is invalid")
    if not recovery_required and (head is None or live_binding is None or reconciliation is None):
        raise ValueError("durable result fields are required")
    if head is not None and reconciliation is not None:
        if head != reconciliation.head or head.project_id != reconciliation.project_id:
            raise ValueError("reconciliation lineage is inconsistent")


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class CandidateRollbackResult(_IdentityValue):
    schema_version: int = _SCHEMA_VERSION
    status: CandidateRollbackStatus
    head: _ProjectHead | None
    live_binding: SessionBinding | None
    reconciliation: _ReconciliationResult | None
    head_committed: bool
    slot_promoted: bool | None
    cleanup_required: bool
    recovery_required: bool
    cleanup_binding: SessionBinding | None

    def __post_init__(self):
        _validate_result_flags(
            self.schema_version,
            self.head_committed,
            self.slot_promoted,
            self.cleanup_required,
            self.recovery_required,
        )
        if type(self.status) is not CandidateRollbackStatus:
            raise ValueError("status is invalid")
        if not _status_flags(
            self.status,
            CandidateRollbackStatus.NOT_COMMITTED,
            CandidateRollbackStatus.CLEANUP_REQUIRED,
            CandidateRollbackStatus.RECOVERY_REQUIRED,
            self.cleanup_required,
            self.recovery_required,
        ):
            raise ValueError("rollback status flags are inconsistent")
        if self.status is not CandidateRollbackStatus.RECOVERY_REQUIRED:
            if self.head_committed or self.slot_promoted is not False:
                raise ValueError("rollback result flags are inconsistent")
        _validate_reconciliation_fields(
            self.head,
            self.live_binding,
            self.reconciliation,
            self.recovery_required,
        )
        project_id = None
        if self.head is not None:
            project_id = self.head.project_id
        elif self.live_binding is not None:
            project_id = self.live_binding.project_id
        elif self.reconciliation is not None:
            project_id = self.reconciliation.project_id
        if project_id is not None:
            _validate_cleanup_binding(
                self.live_binding,
                self.cleanup_binding,
                self.cleanup_required,
                project_id,
            )
        elif self.cleanup_binding is not None:
            raise ValueError("cleanup binding has no project anchor")


@_dataclass(frozen=True, slots=True, kw_only=True, eq=False, repr=False)
class CandidateReconcileResult(_IdentityValue):
    schema_version: int = _SCHEMA_VERSION
    status: CandidateReconcileStatus
    head: _ProjectHead | None
    live_binding: SessionBinding | None
    reconciliation: _ReconciliationResult | None
    head_committed: bool
    slot_promoted: bool | None
    cleanup_required: bool
    recovery_required: bool
    cleanup_binding: SessionBinding | None

    def __post_init__(self):
        _validate_result_flags(
            self.schema_version,
            self.head_committed,
            self.slot_promoted,
            self.cleanup_required,
            self.recovery_required,
        )
        if type(self.status) is not CandidateReconcileStatus:
            raise ValueError("status is invalid")
        if not _status_flags(
            self.status,
            CandidateReconcileStatus.CLEAN,
            CandidateReconcileStatus.CLEANUP_REQUIRED,
            CandidateReconcileStatus.RECOVERY_REQUIRED,
            self.cleanup_required,
            self.recovery_required,
        ) and not (
            self.status
            in {
                CandidateReconcileStatus.COMMITTED,
                CandidateReconcileStatus.NOT_COMMITTED,
            }
            and not self.cleanup_required
            and not self.recovery_required
        ):
            raise ValueError("reconcile status flags are inconsistent")
        if self.status in {CandidateReconcileStatus.CLEAN, CandidateReconcileStatus.COMMITTED}:
            if not self.head_committed or self.slot_promoted is not True:
                raise ValueError("committed reconcile flags are inconsistent")
        elif self.status is CandidateReconcileStatus.NOT_COMMITTED:
            if self.head_committed or self.slot_promoted is not False:
                raise ValueError("not-committed reconcile flags are inconsistent")
        _validate_reconciliation_fields(
            self.head,
            self.live_binding,
            self.reconciliation,
            self.recovery_required,
        )
        project_id = None
        if self.head is not None:
            project_id = self.head.project_id
        elif self.live_binding is not None:
            project_id = self.live_binding.project_id
        elif self.reconciliation is not None:
            project_id = self.reconciliation.project_id
        if project_id is not None:
            _validate_cleanup_binding(
                self.live_binding,
                self.cleanup_binding,
                self.cleanup_required,
                project_id,
            )
        elif self.cleanup_binding is not None:
            raise ValueError("cleanup binding has no project anchor")


class SessionSlot:
    __slots__ = ("_current", "_lock", "_retired")

    def __init__(self, initial: SessionBinding) -> None:
        if type(initial) is not SessionBinding:
            raise CandidateError(CandidateErrorCode.INVALID_BINDING)
        self._current = initial
        self._lock = _threading.RLock()
        self._retired = _weakref.WeakSet()

    def current(self) -> SessionBinding:
        with self._lock:
            return self._current

    def compare_and_set(
        self,
        expected: SessionBinding,
        replacement: SessionBinding,
    ) -> bool:
        with self._lock:
            if type(expected) is not SessionBinding or type(replacement) is not SessionBinding:
                raise CandidateError(CandidateErrorCode.INVALID_BINDING)
            if replacement.project_id != self._current.project_id:
                raise CandidateError(CandidateErrorCode.INVALID_BINDING)
            if replacement in self._retired:
                raise CandidateError(CandidateErrorCode.INVALID_BINDING)
            if replacement.session is self._current.session:
                raise CandidateError(CandidateErrorCode.SESSION_ALIAS)
            if self._current is not expected:
                return False
            self._current = replacement
            return True

    def _retire(self, binding):
        with self._lock:
            if type(binding) is not SessionBinding:
                return False
            if self._current is binding:
                return False
            if self._current.session is binding.session:
                return False
            self._retired.add(binding)
            return True


class CandidateCoordinator:
    __slots__ = (
        "_store",
        "_snapshot_port",
        "_session_slot",
        "_lock",
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
        "_terminal_order",
        "_retained",
        "_pending_baselines",
        "_attempted",
        "_unresolved",
        "_load_failures",
        "_close_failed",
        "_runtime_closed",
    )

    def __init__(
        self,
        *,
        store: _LocalRevisionStore,
        snapshot_port: CadSnapshotPort,
        session_slot: SessionSlot,
    ) -> None:
        if type(store) is not _LocalRevisionStore:
            raise CandidateError(CandidateErrorCode.INVALID_INPUT)
        if not isinstance(snapshot_port, CadSnapshotPort):
            raise CandidateError(CandidateErrorCode.INVALID_INPUT)
        if type(session_slot) is not SessionSlot:
            raise CandidateError(CandidateErrorCode.INVALID_INPUT)
        self._store = store
        self._snapshot_port = snapshot_port
        self._session_slot = session_slot
        self._lock = _threading.RLock()
        self._owners = dict()
        self._stages = dict()
        self._leases = dict()
        self._heads = dict()
        self._baselines = dict()
        self._paths = dict()
        self._handles = dict()
        self._bindings = dict()
        self._terminal_kinds = dict()
        self._terminal_results = dict()
        self._terminal_order = _OrderedDict()
        self._retained = dict()
        self._pending_baselines = dict()
        self._attempted = _weakref.WeakSet()
        self._unresolved = _weakref.WeakSet()
        self._load_failures = set()
        self._close_failed = False
        self._runtime_closed = False

    def _close_runtime(self, *, project_id: str) -> bool:
        """Close an idle project baseline without discarding recovery authority."""

        with self._lock:
            if self._runtime_closed:
                return True
            if self._close_failed:
                return False
            try:
                current = self._session_slot.current()
            except Exception:
                return False
            if current.project_id != project_id:
                return False
            if any(
                head.project_id == project_id and self._stages.get(token) != "terminal"
                for token, head in self._heads.items()
            ):
                return False
            if self._cleanup_retained(project_id, current):
                return False
            if self._retained.get(project_id, ()) or self._pending_baselines.get(project_id, ()):
                return False
            self._attempted.add(current)
            try:
                self._snapshot_port.close(current.session)
            except BaseException as error:
                self._unresolved.add(current)
                self._close_failed = True
                if isinstance(error, Exception):
                    return False
                raise
            self._runtime_closed = True
            self._purge_terminal_replay()
            return True

    def _evict_terminal(self, token):
        with self._lock:
            if self._stages.get(token) != "terminal":
                return
            handles = self._handles.pop(token, ())
            for handle in handles:
                if self._owners.get(handle) is token:
                    self._owners.pop(handle, None)
            self._stages.pop(token, None)
            self._leases.pop(token, None)
            self._heads.pop(token, None)
            self._baselines.pop(token, None)
            self._paths.pop(token, None)
            self._bindings.pop(token, None)
            self._terminal_kinds.pop(token, None)
            self._terminal_results.pop(token, None)
            self._terminal_order.pop(token, None)

    def _remember_terminal(self, token):
        with self._lock:
            if self._stages.get(token) != "terminal":
                return
            self._terminal_order.update({token: None})
            self._terminal_order.move_to_end(token)
            while len(self._terminal_order) > _TERMINAL_REPLAY_LIMIT:
                expired, _value = self._terminal_order.popitem(last=False)
                self._evict_terminal(expired)

    def _purge_terminal_replay(self):
        with self._lock:
            while self._terminal_order:
                token, _value = self._terminal_order.popitem(last=False)
                self._evict_terminal(token)

    def _start_lineage(self, project_id, base_head, baseline, lease, revision_id, paths):
        with self._lock:
            token = object()
            self._stages.update({token: "starting"})
            self._leases.update({token: lease})
            self._heads.update({token: base_head})
            self._baselines.update({token: baseline})
            self._paths.update({token: (paths, revision_id)})
            self._handles.update({token: ()})
            self._bindings.update({token: ()})
            self._terminal_kinds.update({token: None})
            self._terminal_results.update({token: None})
            return token

    def _issue(self, token, stage, handle, binding):
        with self._lock:
            handles = self._handles.get(token)
            bindings = self._bindings.get(token)
            if handles is None or bindings is None:
                raise CandidateError(CandidateErrorCode.INVALID_CANDIDATE)
            self._owners.update({handle: token})
            self._handles.update({token: handles + (handle,)})
            if binding not in bindings:
                self._bindings.update({token: bindings + (binding,)})
            self._stages.update({token: stage})

    def _add_binding(self, token, binding):
        with self._lock:
            bindings = self._bindings.get(token)
            if bindings is None:
                raise CandidateError(CandidateErrorCode.INVALID_CANDIDATE)
            if binding not in bindings:
                self._bindings.update({token: bindings + (binding,)})

    def _lookup(self, candidate, stage, operation):
        with self._lock:
            token = self._owners.get(candidate)
            if token is None:
                raise CandidateError(CandidateErrorCode.INVALID_CANDIDATE)
            current_stage = self._stages.get(token)
            if current_stage == "terminal":
                terminal_kind = self._terminal_kinds.get(token)
                terminal_result = self._terminal_results.get(token)
                if terminal_kind == operation and terminal_result is not None:
                    return (token, terminal_result)
                raise CandidateError(CandidateErrorCode.ALREADY_TERMINAL)
            handles = self._handles.get(token)
            if not handles or handles[-1] is not candidate or current_stage != stage:
                raise CandidateError(CandidateErrorCode.INVALID_CANDIDATE)
            return (token, None)

    def _finish(self, token, operation, result):
        with self._lock:
            self._stages.update({token: "terminal"})
            self._terminal_kinds.update({token: operation})
            self._terminal_results.update({token: result})
            self._remember_terminal(token)

    def _fail(self, token):
        with self._lock:
            self._stages.update({token: "terminal"})
            self._terminal_kinds.update({token: "failed"})
            self._terminal_results.update({token: None})
            self._remember_terminal(token)

    def _check_lease(self, token, lease):
        with self._lock:
            captured = self._leases.get(token)
            head = self._heads.get(token)
            if head is None or not self._store_lease_is_valid(head.project_id, lease):
                raise CandidateError(CandidateErrorCode.INVALID_LEASE)
            if (
                lease is not captured
                or lease.released is not False
                or lease.project_id != head.project_id
            ):
                raise CandidateError(CandidateErrorCode.INVALID_LEASE)

    def _store_lease_is_valid(self, project_id, lease):
        with self._lock:
            try:
                self._store.validate_project_write_lease(project_id, lease)
            except Exception:
                return False
            return True

    def _require_session_creation_allowed(self):
        with self._lock:
            if self._close_failed:
                raise CandidateError(
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            if self._runtime_closed:
                raise CandidateError(CandidateErrorCode.INVALID_TRANSITION)

    def _check_preconditions(self, token, lease):
        with self._lock:
            self._check_lease(token, lease)
            head = self._heads.get(token)
            baseline = self._baselines.get(token)
            if head is None or baseline is None:
                raise CandidateError(CandidateErrorCode.INVALID_CANDIDATE)
            try:
                current_head = self._store.load_head(head.project_id)
            except Exception:
                raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
            if current_head != head:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                current_binding = self._session_slot.current()
            except Exception:
                raise CandidateError(CandidateErrorCode.CONFLICT) from None
            if current_binding is not baseline:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            return (head, baseline)

    def _aliases_owned(self, token, binding):
        with self._lock:
            baseline = self._baselines.get(token)
            if baseline is not None:
                if binding.session is baseline.session:
                    return True
            bindings = self._bindings.get(token)
            if bindings is None:
                return True
            for owned in bindings:
                if binding.session is owned.session:
                    return True
            return False

    def _close_binding(self, binding):
        with self._lock:
            if binding in self._unresolved:
                return True
            if binding in self._attempted:
                return False
            try:
                retired = self._session_slot._retire(binding)
            except Exception:
                return False
            if not retired:
                return False
            self._attempted.add(binding)
            try:
                self._snapshot_port.close(binding.session)
            except BaseException as error:
                self._unresolved.add(binding)
                self._retain(binding.project_id, binding)
                self._close_failed = True
                if isinstance(error, Exception):
                    return True
                raise
            return False

    def _close_owned(self, token):
        with self._lock:
            bindings = self._bindings.get(token)
            failed = False
            if bindings is None:
                return failed
            for binding in bindings:
                if self._close_binding(binding):
                    failed = True
            return failed

    def _abort(self, token, lease, code, cleanup_required=False):
        with self._lock:
            head = self._heads.get(token)
            path_record = self._paths.get(token)
            reconciliation = None
            recovery_required = False
            if head is not None and path_record is not None:
                try:
                    reconciliation = self._store.rollback_revision(
                        head.project_id,
                        path_record[1],
                        lease,
                    )
                except Exception:
                    recovery_required = True
            if (
                reconciliation is not None
                and reconciliation.status is _ReconciliationStatus.COMMITTED
            ):
                recovery_required = True
            if (
                reconciliation is not None
                and reconciliation.status is _ReconciliationStatus.CLEANUP_REQUIRED
            ):
                cleanup_required = True
            cleanup_failed = self._close_owned(token)
            self._fail(token)
            if recovery_required:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if cleanup_required or cleanup_failed:
                return CandidateError(
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            return CandidateError(code)

    def _begin_reconcile_error(self, project_id, lease):
        with self._lock:
            try:
                reconciliation = self._store.reconcile(project_id, lease)
            except Exception:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if reconciliation.status is _ReconciliationStatus.COMMITTED:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if reconciliation.status is _ReconciliationStatus.CLEANUP_REQUIRED:
                return CandidateError(
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            return CandidateError(CandidateErrorCode.STORE_FAILURE)

    def _begin_rollback_error(self, project_id, revision_id, lease):
        with self._lock:
            try:
                reconciliation = self._store.rollback_revision(
                    project_id,
                    revision_id,
                    lease,
                )
            except Exception:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if reconciliation.status is _ReconciliationStatus.COMMITTED:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if reconciliation.status is _ReconciliationStatus.CLEANUP_REQUIRED:
                return CandidateError(
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            return CandidateError(CandidateErrorCode.STORE_FAILURE)

    def _retain(self, project_id, binding):
        with self._lock:
            retained = self._retained.get(project_id)
            if retained is None:
                retained = ()
            if binding not in retained:
                self._retained.update({project_id: retained + (binding,)})

    def _remember_baseline(self, project_id, binding):
        with self._lock:
            baselines = self._pending_baselines.get(project_id)
            if baselines is None:
                baselines = ()
            if binding not in baselines:
                self._pending_baselines.update({project_id: baselines + (binding,)})

    def _cleanup_retained_candidates(self, project_id, live_binding):
        with self._lock:
            failed = False
            retained = self._retained.get(project_id)
            if retained is None:
                retained = ()
            keep = ()
            for binding in retained:
                if binding.session is live_binding.session:
                    keep = keep + (binding,)
                elif self._close_binding(binding):
                    failed = True
                    keep = keep + (binding,)
            self._retained.update({project_id: keep})
            return failed

    def _cleanup_retained(self, project_id, live_binding):
        with self._lock:
            failed = self._cleanup_retained_candidates(project_id, live_binding)
            baselines = self._pending_baselines.get(project_id)
            if baselines is None:
                baselines = ()
            keep_baselines = ()
            for binding in baselines:
                if binding.session is live_binding.session:
                    keep_baselines = keep_baselines + (binding,)
                elif self._close_binding(binding):
                    failed = True
                    keep_baselines = keep_baselines + (binding,)
            self._pending_baselines.update({project_id: keep_baselines})
            return failed

    def _close_project_candidates(self, project_id):
        with self._lock:
            failed = False
            for token, head in self._heads.items():
                if head.project_id == project_id and self._close_owned(token):
                    failed = True
            retained = self._retained.get(project_id)
            if retained is None:
                retained = ()
            keep = ()
            for binding in retained:
                if self._close_binding(binding):
                    failed = True
                    keep = keep + (binding,)
            self._retained.update({project_id: keep})
            return failed

    def _settle_review_prepare_failure(self, token, project_id, lease, cleanup_hint):
        with self._lock:
            try:
                reconciliation = self._store.reconcile(project_id, lease)
            except Exception:
                reconciliation = None
            cleanup_failed = self._close_owned(token)
            head = self._heads.get(token)
            self._fail(token)
            if reconciliation is None or head is None:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if reconciliation.head != head:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if reconciliation.status is _ReconciliationStatus.COMMITTED:
                return CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                )
            if (
                cleanup_hint
                or cleanup_failed
                or reconciliation.status is _ReconciliationStatus.CLEANUP_REQUIRED
            ):
                return CandidateError(
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            if reconciliation.status in {
                _ReconciliationStatus.CLEAN,
                _ReconciliationStatus.NOT_COMMITTED,
            }:
                return CandidateError(CandidateErrorCode.STORE_FAILURE)
            return CandidateError(
                CandidateErrorCode.RECOVERY_REQUIRED,
                recovery_required=True,
            )

    def _commit_result(
        self,
        candidate,
        report,
        head,
        live_binding,
        slot_promoted,
        cleanup_required,
        recovery_required,
        cleanup_binding,
    ):
        with self._lock:
            if recovery_required:
                status = CandidateCommitStatus.COMMITTED_RECOVERY_REQUIRED
            elif cleanup_required:
                status = CandidateCommitStatus.COMMITTED_CLEANUP_REQUIRED
            else:
                status = CandidateCommitStatus.COMMITTED
            return CandidateCommitResult(
                status=status,
                head=head,
                revision=candidate.revision,
                live_binding=live_binding,
                report=report,
                head_committed=True,
                slot_promoted=slot_promoted,
                cleanup_required=cleanup_required,
                recovery_required=recovery_required,
                cleanup_binding=cleanup_binding,
            )

    def _post_commit(self, candidate, report, head, force_recovery, baseline):
        with self._lock:
            outcome = "raise"
            try:
                if self._session_slot.compare_and_set(baseline, candidate.binding):
                    outcome = "true"
                else:
                    outcome = "false"
            except Exception:
                outcome = "raise"
            try:
                current = self._session_slot.current()
            except Exception:
                self._retain(candidate.project_id, candidate.binding)
                self._remember_baseline(candidate.project_id, baseline)
                return self._commit_result(
                    candidate,
                    report,
                    head,
                    None,
                    None,
                    False,
                    True,
                    None,
                )
            if current is candidate.binding:
                cleanup_failed = self._close_binding(baseline)
                return self._commit_result(
                    candidate,
                    report,
                    head,
                    candidate.binding,
                    True,
                    cleanup_failed,
                    force_recovery or outcome != "true",
                    None,
                )
            if current is baseline and outcome != "true":
                cleanup_failed = self._close_binding(candidate.binding)
                return self._commit_result(
                    candidate,
                    report,
                    head,
                    baseline,
                    False,
                    cleanup_failed,
                    True,
                    None,
                )
            try:
                self._session_slot._retire(candidate.binding)
            except Exception:
                pass
            self._retain(candidate.project_id, candidate.binding)
            self._remember_baseline(candidate.project_id, baseline)
            return self._commit_result(
                candidate,
                report,
                head,
                current,
                None,
                True,
                True,
                candidate.binding,
            )

    def begin(
        self,
        *,
        project_id: str,
        expected_head: _ProjectHead,
        lease: _ProjectWriteLease,
    ) -> ActiveCandidate:
        with self._lock:
            _require_project(project_id)
            if type(expected_head) is not _ProjectHead or expected_head.project_id != project_id:
                raise CandidateError(CandidateErrorCode.INVALID_INPUT)
            if not self._store_lease_is_valid(project_id, lease):
                raise CandidateError(CandidateErrorCode.INVALID_LEASE)
            self._require_session_creation_allowed()
            try:
                durable_head = self._store.load_head(project_id)
            except Exception:
                raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
            if durable_head != expected_head:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                baseline = self._session_slot.current()
            except Exception:
                raise CandidateError(CandidateErrorCode.CONFLICT) from None
            if (
                baseline.project_id != project_id
                or baseline.revision_id != expected_head.revision_id
            ):
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                base_revision = self._store.load_revision(project_id, expected_head.revision_id)
            except Exception:
                raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
            try:
                revision_id = self._store.begin_revision(project_id, expected_head, lease)
            except _RevisionStoreError as error:
                if error.code is _RevisionStoreErrorCode.INVALID_LEASE:
                    raise CandidateError(CandidateErrorCode.INVALID_LEASE) from None
                if error.code is _RevisionStoreErrorCode.CONFLICT:
                    raise CandidateError(CandidateErrorCode.CONFLICT) from None
                raise self._begin_reconcile_error(project_id, lease) from None
            except Exception:
                raise self._begin_reconcile_error(project_id, lease) from None
            try:
                model_path = self._store.candidate_model_path(
                    project_id,
                    revision_id,
                    lease,
                )
                step_path = self._store.candidate_artifact_path(
                    project_id,
                    revision_id,
                    "step",
                    lease,
                )
            except Exception:
                raise self._begin_rollback_error(project_id, revision_id, lease) from None
            token = self._start_lineage(
                project_id,
                expected_head,
                baseline,
                lease,
                revision_id,
                (model_path, step_path),
            )
            try:
                if base_revision.model is None:
                    binding = SessionBinding(
                        project_id=project_id,
                        revision_id=revision_id,
                        session=self._snapshot_port.create_empty(revision_id=revision_id),
                    )
                else:
                    binding = SessionBinding(
                        project_id=project_id,
                        revision_id=revision_id,
                        session=self._snapshot_port.load_fcstd(model_path),
                    )
            except Exception:
                raise self._abort(token, lease, CandidateErrorCode.CAD_FAILURE) from None
            if binding.session is baseline.session:
                raise self._abort(token, lease, CandidateErrorCode.SESSION_ALIAS)
            active = ActiveCandidate(
                project_id=project_id,
                base_head=expected_head,
                binding=binding,
                model_path=model_path,
                step_path=step_path,
            )
            self._issue(token, "active", active, binding)
            return active

    def checkpoint(
        self,
        *,
        candidate: ActiveCandidate,
        lease: _ProjectWriteLease,
    ) -> CheckpointedCandidate:
        with self._lock:
            token, cached = self._lookup(candidate, "active", "checkpoint")
            if cached is not None:
                raise CandidateError(CandidateErrorCode.ALREADY_TERMINAL)
            self._check_preconditions(token, lease)
            self._require_session_creation_allowed()
            try:
                self._snapshot_port.checkpoint_fcstd(
                    candidate.binding.session,
                    candidate.model_path,
                )
            except Exception:
                raise self._abort(token, lease, CandidateErrorCode.CAD_FAILURE) from None
            try:
                replacement = SessionBinding(
                    project_id=candidate.project_id,
                    revision_id=candidate.binding.revision_id,
                    session=self._snapshot_port.load_fcstd(candidate.model_path),
                )
            except Exception:
                raise self._abort(token, lease, CandidateErrorCode.CAD_FAILURE) from None
            if self._aliases_owned(token, replacement):
                raise self._abort(token, lease, CandidateErrorCode.SESSION_ALIAS)
            self._add_binding(token, replacement)
            if self._close_binding(candidate.binding):
                raise self._abort(
                    token,
                    lease,
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            checkpointed = CheckpointedCandidate(
                project_id=candidate.project_id,
                base_head=candidate.base_head,
                binding=replacement,
                model_path=candidate.model_path,
                step_path=candidate.step_path,
            )
            self._issue(token, "checkpointed", checkpointed, replacement)
            return checkpointed

    def seal(
        self,
        *,
        candidate: CheckpointedCandidate,
        lease: _ProjectWriteLease,
    ) -> SealedCandidate:
        with self._lock:
            token, cached = self._lookup(candidate, "checkpointed", "seal")
            if cached is not None:
                raise CandidateError(CandidateErrorCode.ALREADY_TERMINAL)
            self._check_preconditions(token, lease)
            self._require_session_creation_allowed()
            try:
                revision = self._store.seal_revision(
                    candidate.project_id,
                    candidate.binding.revision_id,
                    lease,
                )
            except Exception:
                raise self._abort(token, lease, CandidateErrorCode.STORE_FAILURE) from None
            try:
                immutable_path = self._store.revision_model_path(
                    candidate.project_id,
                    revision.id,
                )
                replacement = SessionBinding(
                    project_id=candidate.project_id,
                    revision_id=revision.id,
                    session=self._snapshot_port.load_fcstd(immutable_path),
                )
            except Exception:
                raise self._abort(token, lease, CandidateErrorCode.CAD_FAILURE) from None
            if self._aliases_owned(token, replacement):
                raise self._abort(token, lease, CandidateErrorCode.SESSION_ALIAS)
            self._add_binding(token, replacement)
            if self._close_binding(candidate.binding):
                raise self._abort(
                    token,
                    lease,
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            sealed = SealedCandidate(
                project_id=candidate.project_id,
                base_head=candidate.base_head,
                revision=revision,
                binding=replacement,
            )
            self._issue(token, "sealed", sealed, replacement)
            return sealed

    def reopen_review(
        self,
        *,
        project_id: str,
        base_head: _ProjectHead,
        revision: _RevisionRef,
        lease: _ProjectWriteLease,
    ) -> SealedCandidate:
        with self._lock:
            _require_project(project_id)
            if type(base_head) is not _ProjectHead or base_head.project_id != project_id:
                raise CandidateError(CandidateErrorCode.INVALID_INPUT)
            if type(revision) is not _RevisionRef or revision.project_id != project_id:
                raise CandidateError(CandidateErrorCode.INVALID_INPUT)
            if revision.base_revision != base_head.revision_id:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            if not self._store_lease_is_valid(project_id, lease):
                raise CandidateError(CandidateErrorCode.INVALID_LEASE)
            self._require_session_creation_allowed()
            try:
                durable_head = self._store.load_head(project_id)
            except Exception:
                raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
            if durable_head != base_head:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                durable_revision = self._store.load_revision(project_id, revision.id)
            except Exception:
                raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
            if durable_revision != revision:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                baseline = self._session_slot.current()
            except Exception:
                raise CandidateError(CandidateErrorCode.CONFLICT) from None
            if baseline.project_id != project_id or baseline.revision_id != base_head.revision_id:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                immutable_path = self._store.revision_model_path(project_id, revision.id)
            except Exception:
                raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
            token = self._start_lineage(
                project_id,
                base_head,
                baseline,
                lease,
                revision.id,
                (immutable_path, immutable_path),
            )
            try:
                binding = SessionBinding(
                    project_id=project_id,
                    revision_id=revision.id,
                    session=self._snapshot_port.load_fcstd(immutable_path),
                )
            except Exception:
                self._fail(token)
                raise CandidateError(CandidateErrorCode.CAD_FAILURE) from None
            if self._aliases_owned(token, binding):
                self._fail(token)
                raise CandidateError(CandidateErrorCode.SESSION_ALIAS)
            reopened = SealedCandidate(
                project_id=project_id,
                base_head=base_head,
                revision=durable_revision,
                binding=binding,
            )
            self._issue(token, "review_open", reopened, binding)
            return reopened

    def prepare_review(
        self,
        *,
        candidate: SealedCandidate,
        lease: _ProjectWriteLease,
    ) -> SealedCandidate:
        with self._lock:
            token, cached = self._lookup(candidate, "review_open", "prepare_review")
            if cached is not None:
                raise CandidateError(CandidateErrorCode.ALREADY_TERMINAL)
            self._check_preconditions(token, lease)
            try:
                prepared_revision = self._store.prepare_revision(
                    candidate.project_id,
                    candidate.base_head,
                    candidate.revision.id,
                    candidate.revision.manifest_sha256,
                    lease,
                )
            except _RevisionStoreError as error:
                if error.code is _RevisionStoreErrorCode.INVALID_LEASE:
                    raise CandidateError(CandidateErrorCode.INVALID_LEASE) from None
                if error.code is _RevisionStoreErrorCode.CONFLICT:
                    cleanup_failed = self._close_owned(token)
                    self._fail(token)
                    if cleanup_failed:
                        raise CandidateError(
                            CandidateErrorCode.CLEANUP_REQUIRED,
                            cleanup_required=True,
                        ) from None
                    raise CandidateError(CandidateErrorCode.CONFLICT) from None
                raise self._settle_review_prepare_failure(
                    token,
                    candidate.project_id,
                    lease,
                    error.code is _RevisionStoreErrorCode.CLEANUP_REQUIRED,
                ) from None
            except Exception:
                raise self._settle_review_prepare_failure(
                    token,
                    candidate.project_id,
                    lease,
                    False,
                ) from None
            if prepared_revision != candidate.revision:
                raise self._settle_review_prepare_failure(
                    token,
                    candidate.project_id,
                    lease,
                    False,
                )
            prepared = SealedCandidate(
                project_id=candidate.project_id,
                base_head=candidate.base_head,
                revision=prepared_revision,
                binding=candidate.binding,
            )
            self._issue(token, "sealed", prepared, candidate.binding)
            return prepared

    def discard_review(
        self,
        *,
        candidate: SealedCandidate,
        lease: _ProjectWriteLease,
    ) -> None:
        with self._lock:
            token = self._owners.get(candidate)
            if (
                token is not None
                and self._stages.get(token) == "terminal"
                and self._terminal_kinds.get(token) == "discard_review"
            ):
                return None
            token, cached = self._lookup(candidate, "review_open", "discard_review")
            if cached is not None:
                raise CandidateError(CandidateErrorCode.ALREADY_TERMINAL)
            self._check_preconditions(token, lease)
            if self._close_owned(token):
                self._fail(token)
                raise CandidateError(
                    CandidateErrorCode.CLEANUP_REQUIRED,
                    cleanup_required=True,
                )
            self._finish(token, "discard_review", None)
            return None

    def publish_review(
        self,
        *,
        candidate: SealedCandidate,
        receipt: _VerificationReceipt,
        compiled: _CompiledAcceptance,
        snapshot: _ObservationSnapshot,
        lease: _ProjectWriteLease,
    ) -> CandidateRollbackResult:
        with self._lock:
            token, cached = self._lookup(candidate, "sealed", "publish_review")
            if cached is not None:
                return cached
            self._check_preconditions(token, lease)
            try:
                _consume_verification_receipt(
                    receipt,
                    compiled,
                    snapshot,
                    candidate_revision=candidate.revision.id,
                    manifest_sha256=candidate.revision.manifest_sha256,
                )
            except _ValidationError:
                raise self._abort(
                    token,
                    lease,
                    CandidateErrorCode.RECEIPT_REJECTED,
                ) from None
            except Exception:
                raise self._abort(
                    token,
                    lease,
                    CandidateErrorCode.RECEIPT_REJECTED,
                ) from None
            try:
                result = CandidateCoordinator.rollback(
                    self,
                    candidate=candidate,
                    lease=lease,
                )
            except CandidateError:
                self._close_owned(token)
                self._fail(token)
                raise
            self._terminal_kinds.update({token: "publish_review"})
            return result

    def commit(
        self,
        *,
        candidate: SealedCandidate,
        receipt: _VerificationReceipt,
        compiled: _CompiledAcceptance,
        snapshot: _ObservationSnapshot,
        lease: _ProjectWriteLease,
    ) -> CandidateCommitResult:
        with self._lock:
            token, cached = self._lookup(candidate, "sealed", "commit")
            if cached is not None:
                return cached
            head, baseline = self._check_preconditions(token, lease)
            try:
                report = _consume_verification_receipt(
                    receipt,
                    compiled,
                    snapshot,
                    candidate_revision=candidate.revision.id,
                    manifest_sha256=candidate.revision.manifest_sha256,
                )
            except _ValidationError:
                raise self._abort(
                    token,
                    lease,
                    CandidateErrorCode.RECEIPT_REJECTED,
                ) from None
            except Exception:
                raise self._abort(
                    token,
                    lease,
                    CandidateErrorCode.RECEIPT_REJECTED,
                ) from None
            try:
                committed_head = self._store.commit_revision(
                    candidate.project_id,
                    candidate.base_head,
                    candidate.revision.id,
                    lease,
                )
            except Exception:
                try:
                    reconciliation = self._store.reconcile(candidate.project_id, lease)
                except Exception:
                    self._fail(token)
                    raise CandidateError(
                        CandidateErrorCode.RECOVERY_REQUIRED,
                        recovery_required=True,
                    ) from None
                try:
                    exact_head = self._store.load_head(candidate.project_id)
                except Exception:
                    if reconciliation.status is _ReconciliationStatus.COMMITTED:
                        result = self._commit_result(
                            candidate,
                            report,
                            reconciliation.head,
                            baseline,
                            None,
                            False,
                            True,
                            None,
                        )
                        self._finish(token, "commit", result)
                        return result
                    self._fail(token)
                    raise CandidateError(
                        CandidateErrorCode.RECOVERY_REQUIRED,
                        recovery_required=True,
                    ) from None
                if reconciliation.status in {
                    _ReconciliationStatus.NOT_COMMITTED,
                    _ReconciliationStatus.CLEANUP_REQUIRED,
                }:
                    if exact_head == candidate.base_head:
                        self._close_owned(token)
                        self._fail(token)
                        raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
                    self._fail(token)
                    raise CandidateError(
                        CandidateErrorCode.RECOVERY_REQUIRED,
                        recovery_required=True,
                    ) from None
                if reconciliation.status is _ReconciliationStatus.COMMITTED:
                    if (
                        exact_head.revision_id == candidate.revision.id
                        and exact_head.manifest_sha256 == candidate.revision.manifest_sha256
                    ):
                        result = self._post_commit(
                            candidate,
                            report,
                            exact_head,
                            True,
                            baseline,
                        )
                        self._finish(token, "commit", result)
                        return result
                    result = self._commit_result(
                        candidate,
                        report,
                        reconciliation.head,
                        baseline,
                        None,
                        False,
                        True,
                        None,
                    )
                    self._finish(token, "commit", result)
                    return result
                self._fail(token)
                raise CandidateError(
                    CandidateErrorCode.RECOVERY_REQUIRED,
                    recovery_required=True,
                ) from None
            result = self._post_commit(
                candidate,
                report,
                committed_head,
                False,
                baseline,
            )
            self._finish(token, "commit", result)
            return result

    def rollback(
        self,
        *,
        candidate: ActiveCandidate | CheckpointedCandidate | SealedCandidate,
        lease: _ProjectWriteLease,
    ) -> CandidateRollbackResult:
        with self._lock:
            if type(candidate) not in {ActiveCandidate, CheckpointedCandidate, SealedCandidate}:
                raise CandidateError(CandidateErrorCode.INVALID_CANDIDATE)
            expected_stage = "active"
            if type(candidate) is CheckpointedCandidate:
                expected_stage = "checkpointed"
            elif type(candidate) is SealedCandidate:
                expected_stage = "sealed"
            token, cached = self._lookup(candidate, expected_stage, "rollback")
            if cached is not None:
                return cached
            self._check_lease(token, lease)
            head = self._heads.get(token)
            baseline = self._baselines.get(token)
            if head is None or baseline is None:
                raise CandidateError(CandidateErrorCode.INVALID_CANDIDATE)
            try:
                durable_head = self._store.load_head(head.project_id)
            except Exception:
                raise CandidateError(CandidateErrorCode.STORE_FAILURE) from None
            if durable_head != head:
                if (
                    type(candidate) is SealedCandidate
                    and durable_head.revision_id == candidate.revision.id
                    and durable_head.manifest_sha256 == candidate.revision.manifest_sha256
                ):
                    try:
                        reconciliation = self._store.reconcile(head.project_id, lease)
                    except Exception:
                        result = CandidateRollbackResult(
                            status=CandidateRollbackStatus.RECOVERY_REQUIRED,
                            head=None,
                            live_binding=None,
                            reconciliation=None,
                            head_committed=True,
                            slot_promoted=None,
                            cleanup_required=False,
                            recovery_required=True,
                            cleanup_binding=None,
                        )
                    else:
                        result = CandidateRollbackResult(
                            status=CandidateRollbackStatus.RECOVERY_REQUIRED,
                            head=reconciliation.head,
                            live_binding=baseline,
                            reconciliation=reconciliation,
                            head_committed=True,
                            slot_promoted=False,
                            cleanup_required=False,
                            recovery_required=True,
                            cleanup_binding=None,
                        )
                    self._finish(token, "rollback", result)
                    return result
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                current = self._session_slot.current()
            except Exception:
                raise CandidateError(CandidateErrorCode.CONFLICT) from None
            if current is not baseline:
                raise CandidateError(CandidateErrorCode.CONFLICT)
            try:
                reconciliation = self._store.rollback_revision(
                    head.project_id,
                    candidate.binding.revision_id,
                    lease,
                )
            except Exception:
                try:
                    reconciliation = self._store.reconcile(head.project_id, lease)
                except Exception:
                    result = CandidateRollbackResult(
                        status=CandidateRollbackStatus.RECOVERY_REQUIRED,
                        head=None,
                        live_binding=None,
                        reconciliation=None,
                        head_committed=False,
                        slot_promoted=None,
                        cleanup_required=False,
                        recovery_required=True,
                        cleanup_binding=None,
                    )
                    self._finish(token, "rollback", result)
                    return result
                if reconciliation.status is _ReconciliationStatus.COMMITTED:
                    result = CandidateRollbackResult(
                        status=CandidateRollbackStatus.RECOVERY_REQUIRED,
                        head=reconciliation.head,
                        live_binding=baseline,
                        reconciliation=reconciliation,
                        head_committed=True,
                        slot_promoted=False,
                        cleanup_required=False,
                        recovery_required=True,
                        cleanup_binding=None,
                    )
                    self._finish(token, "rollback", result)
                    return result
                cleanup_failed = self._close_owned(token)
                result = CandidateRollbackResult(
                    status=CandidateRollbackStatus.RECOVERY_REQUIRED,
                    head=reconciliation.head,
                    live_binding=baseline,
                    reconciliation=reconciliation,
                    head_committed=False,
                    slot_promoted=False,
                    cleanup_required=cleanup_failed,
                    recovery_required=True,
                    cleanup_binding=None,
                )
                self._finish(token, "rollback", result)
                return result
            if reconciliation.status is _ReconciliationStatus.COMMITTED:
                result = CandidateRollbackResult(
                    status=CandidateRollbackStatus.RECOVERY_REQUIRED,
                    head=reconciliation.head,
                    live_binding=baseline,
                    reconciliation=reconciliation,
                    head_committed=True,
                    slot_promoted=False,
                    cleanup_required=False,
                    recovery_required=True,
                    cleanup_binding=None,
                )
                self._finish(token, "rollback", result)
                return result
            cleanup_failed = self._close_owned(token)
            cleanup_required = (
                cleanup_failed or reconciliation.status is _ReconciliationStatus.CLEANUP_REQUIRED
            )
            status = CandidateRollbackStatus.NOT_COMMITTED
            if cleanup_required:
                status = CandidateRollbackStatus.CLEANUP_REQUIRED
            result = CandidateRollbackResult(
                status=status,
                head=reconciliation.head,
                live_binding=baseline,
                reconciliation=reconciliation,
                head_committed=False,
                slot_promoted=False,
                cleanup_required=cleanup_required,
                recovery_required=False,
                cleanup_binding=None,
            )
            self._finish(token, "rollback", result)
            return result

    def _reconcile_result(
        self,
        status,
        reconciliation,
        live_binding,
        head_committed,
        slot_promoted,
        cleanup_required,
        recovery_required,
        cleanup_binding,
    ):
        with self._lock:
            head = None
            if reconciliation is not None:
                head = reconciliation.head
            return CandidateReconcileResult(
                status=status,
                head=head,
                live_binding=live_binding,
                reconciliation=reconciliation,
                head_committed=head_committed,
                slot_promoted=slot_promoted,
                cleanup_required=cleanup_required,
                recovery_required=recovery_required,
                cleanup_binding=cleanup_binding,
            )

    def _post_reconcile(self, project_id, reconciliation, baseline, replacement):
        with self._lock:
            outcome = "raise"
            try:
                if self._session_slot.compare_and_set(baseline, replacement):
                    outcome = "true"
                else:
                    outcome = "false"
            except Exception:
                outcome = "raise"
            try:
                current = self._session_slot.current()
            except Exception:
                self._retain(project_id, replacement)
                self._remember_baseline(project_id, baseline)
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    None,
                    True,
                    None,
                    False,
                    True,
                    None,
                )
            if current is replacement:
                cleanup_failed = self._close_binding(baseline)
                if self._cleanup_retained(project_id, replacement):
                    cleanup_failed = True
                if outcome != "true":
                    return self._reconcile_result(
                        CandidateReconcileStatus.RECOVERY_REQUIRED,
                        reconciliation,
                        replacement,
                        True,
                        True,
                        cleanup_failed,
                        True,
                        None,
                    )
                if cleanup_failed:
                    return self._reconcile_result(
                        CandidateReconcileStatus.CLEANUP_REQUIRED,
                        reconciliation,
                        replacement,
                        True,
                        True,
                        True,
                        False,
                        None,
                    )
                return self._reconcile_result(
                    CandidateReconcileStatus.COMMITTED,
                    reconciliation,
                    replacement,
                    True,
                    True,
                    False,
                    False,
                    None,
                )
            if current is baseline and outcome != "true":
                cleanup_failed = self._close_binding(replacement)
                if self._cleanup_retained(project_id, baseline):
                    cleanup_failed = True
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    baseline,
                    True,
                    False,
                    cleanup_failed,
                    True,
                    None,
                )
            try:
                self._session_slot._retire(replacement)
            except Exception:
                pass
            self._retain(project_id, replacement)
            self._remember_baseline(project_id, baseline)
            return self._reconcile_result(
                CandidateReconcileStatus.RECOVERY_REQUIRED,
                reconciliation,
                current,
                True,
                None,
                True,
                True,
                replacement,
            )

    def reconcile(
        self,
        *,
        project_id: str,
        lease: _ProjectWriteLease,
    ) -> CandidateReconcileResult:
        with self._lock:
            if self._close_failed or self._runtime_closed:
                _require_project(project_id)
                if not self._store_lease_is_valid(project_id, lease):
                    raise CandidateError(CandidateErrorCode.INVALID_LEASE)
                self._require_session_creation_allowed()
            try:
                reconciliation = self._store.reconcile(project_id, lease)
            except _RevisionStoreError as error:
                if error.code is _RevisionStoreErrorCode.INVALID_LEASE:
                    raise CandidateError(CandidateErrorCode.INVALID_LEASE) from None
                return CandidateReconcileResult(
                    status=CandidateReconcileStatus.RECOVERY_REQUIRED,
                    head=None,
                    live_binding=None,
                    reconciliation=None,
                    head_committed=False,
                    slot_promoted=None,
                    cleanup_required=False,
                    recovery_required=True,
                    cleanup_binding=None,
                )
            except Exception:
                return CandidateReconcileResult(
                    status=CandidateReconcileStatus.RECOVERY_REQUIRED,
                    head=None,
                    live_binding=None,
                    reconciliation=None,
                    head_committed=False,
                    slot_promoted=None,
                    cleanup_required=False,
                    recovery_required=True,
                    cleanup_binding=None,
                )
            try:
                current = self._session_slot.current()
            except Exception:
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    None,
                    reconciliation.status is _ReconciliationStatus.COMMITTED,
                    None,
                    False,
                    True,
                    None,
                )
            if reconciliation.status is _ReconciliationStatus.CLEAN:
                if (
                    current.project_id == project_id
                    and current.revision_id == reconciliation.head.revision_id
                ):
                    return self._reconcile_result(
                        CandidateReconcileStatus.CLEAN,
                        reconciliation,
                        current,
                        True,
                        True,
                        False,
                        False,
                        None,
                    )
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    current,
                    False,
                    None,
                    False,
                    True,
                    None,
                )
            if reconciliation.status in {
                _ReconciliationStatus.NOT_COMMITTED,
                _ReconciliationStatus.CLEANUP_REQUIRED,
            }:
                cleanup_failed = self._close_project_candidates(project_id)
                cleanup_required = (
                    cleanup_failed
                    or reconciliation.status is _ReconciliationStatus.CLEANUP_REQUIRED
                )
                status = CandidateReconcileStatus.NOT_COMMITTED
                if cleanup_required:
                    status = CandidateReconcileStatus.CLEANUP_REQUIRED
                return self._reconcile_result(
                    status,
                    reconciliation,
                    current,
                    False,
                    False,
                    cleanup_required,
                    False,
                    None,
                )
            if reconciliation.status is not _ReconciliationStatus.COMMITTED:
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    current,
                    False,
                    None,
                    False,
                    True,
                    None,
                )
            if (
                current.project_id == project_id
                and current.revision_id == reconciliation.head.revision_id
            ):
                cleanup_failed = self._cleanup_retained(project_id, current)
                if cleanup_failed:
                    return self._reconcile_result(
                        CandidateReconcileStatus.CLEANUP_REQUIRED,
                        reconciliation,
                        current,
                        True,
                        True,
                        True,
                        False,
                        None,
                    )
                return self._reconcile_result(
                    CandidateReconcileStatus.COMMITTED,
                    reconciliation,
                    current,
                    True,
                    True,
                    False,
                    False,
                    None,
                )
            journal = reconciliation.journal
            if (
                journal is None
                or current.project_id != project_id
                or current.revision_id != journal.expected_head.revision_id
            ):
                cleanup_failed = self._cleanup_retained_candidates(project_id, current)
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    current,
                    True,
                    None,
                    cleanup_failed,
                    True,
                    None,
                )
            failure_key = (
                project_id,
                reconciliation.head.revision_id,
                current.revision_id,
            )
            if failure_key in self._load_failures:
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    current,
                    True,
                    None,
                    False,
                    True,
                    None,
                )
            try:
                immutable_path = self._store.revision_model_path(
                    project_id,
                    reconciliation.head.revision_id,
                )
                replacement = SessionBinding(
                    project_id=project_id,
                    revision_id=reconciliation.head.revision_id,
                    session=self._snapshot_port.load_fcstd(immutable_path),
                )
            except Exception:
                self._load_failures.add(failure_key)
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    current,
                    True,
                    None,
                    False,
                    True,
                    None,
                )
            if replacement.session is current.session:
                return self._reconcile_result(
                    CandidateReconcileStatus.RECOVERY_REQUIRED,
                    reconciliation,
                    current,
                    True,
                    None,
                    False,
                    True,
                    None,
                )
            return self._post_reconcile(
                project_id,
                reconciliation,
                current,
                replacement,
            )
