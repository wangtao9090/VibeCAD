"""Lazy, durable AgentApplication implementing the neutral task-service port."""

from __future__ import annotations

import os
import re
import secrets
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable

from vibecad.application.data import ApplicationDataLayout
from vibecad.application.project import (
    ProjectBootstrapResult,
    _default_cad_port_factory,
    bootstrap_import_project,
    recover_bootstrap_cleanup,
    verify_generation_zero,
)
from vibecad.application.task_api import (
    TaskServicePortErrorCode,
    TaskServicePortFailure,
)
from vibecad.execution.revisions import (
    LocalRevisionStore,
    RevisionStoreError,
    RevisionStoreErrorCode,
    RevisionStoreRootTrust,
)
from vibecad.interaction.checkouts import (
    CheckoutDescriptor,
    CheckoutStoreRootTrust,
    DraftCheckoutSource,
    HeadCheckoutSource,
    ManagedCheckoutStore,
)
from vibecad.workflow.catalog import (
    TaskCatalogError,
    TaskCatalogErrorCode,
    TaskCatalogService,
)
from vibecad.workflow.contracts import ModelProgram
from vibecad.workflow.lease import LeaseError, LeaseRootTrust, ResourceLeaseManager
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreRootTrust,
)

__all__ = ("AgentApplication",)

MAX_PROJECT_RUNTIMES = 4
_PROJECT_ID = re.compile(r"^project_[0-9a-f]{32}$")
_PROCESS_CAD_GATE = threading.Lock()


def _reset_process_cad_gate() -> None:
    global _PROCESS_CAD_GATE
    _PROCESS_CAD_GATE = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_cad_gate)

_CATALOG_PORT_ERRORS = {
    TaskCatalogErrorCode.INVALID_INPUT: TaskServicePortErrorCode.INVALID_INPUT,
    TaskCatalogErrorCode.UNSUPPORTED_REASONING_OWNER: (
        TaskServicePortErrorCode.UNSUPPORTED_REASONING_OWNER
    ),
    TaskCatalogErrorCode.INVALID_STATE: TaskServicePortErrorCode.INVALID_STATE,
    TaskCatalogErrorCode.NOT_FOUND: TaskServicePortErrorCode.NOT_FOUND,
    TaskCatalogErrorCode.CONFLICT: TaskServicePortErrorCode.CONFLICT,
    TaskCatalogErrorCode.STORE_FAILURE: TaskServicePortErrorCode.STORE_FAILURE,
}


def _new_project_id() -> str:
    return f"project_{secrets.token_hex(16)}"


def _default_runtime_factory(**kwargs):
    from vibecad.application.project import build_project_runtime

    return build_project_runtime(**kwargs)


def _close_runtime(runtime: object) -> bool:
    try:
        return runtime.close() is True
    except Exception:
        return False


class AgentApplication:
    """Process-owned composition root; CAD dependencies remain lazy."""

    __slots__ = (
        "_cad_gate",
        "_catalog",
        "_cad_port_factory",
        "_checkouts",
        "_closed",
        "_creator_pid",
        "_layout",
        "_lease_manager",
        "_revision_store",
        "_runtime_factory",
        "_runtimes",
        "_task_store",
    )

    def __init__(
        self,
        *,
        layout: ApplicationDataLayout,
        lease_manager: ResourceLeaseManager,
        task_store: TaskRunStore,
        revision_store: LocalRevisionStore,
        runtime_factory: Callable[..., object],
        cad_port_factory: Callable[..., object],
    ) -> None:
        if not (
            sys.platform == "darwin"
            and type(layout) is ApplicationDataLayout
            and type(lease_manager) is ResourceLeaseManager
            and type(task_store) is TaskRunStore
            and type(revision_store) is LocalRevisionStore
            and callable(runtime_factory)
            and callable(cad_port_factory)
            and getattr(task_store, "_lease_manager", None) is lease_manager
            and getattr(revision_store, "_lease_manager", None) is lease_manager
            and getattr(lease_manager, "_root_parts", None) == layout.locks.parts
            and getattr(task_store, "_root_parts", None) == layout.tasks.parts
            and getattr(revision_store, "_root", None) == layout.projects
        ):
            raise TypeError("invalid AgentApplication composition")
        self._layout = layout
        self._lease_manager = lease_manager
        self._task_store = task_store
        self._revision_store = revision_store
        self._catalog = TaskCatalogService(
            task_store=task_store,
            revision_store=revision_store,
        )
        self._checkouts = ManagedCheckoutStore(
            layout.checkouts,
            layout.locks,
            revision_store,
            task_store,
            trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
        )
        self._runtime_factory = runtime_factory
        self._cad_port_factory = cad_port_factory
        self._runtimes: OrderedDict[str, object] = OrderedDict()
        self._cad_gate = _PROCESS_CAD_GATE
        self._creator_pid = os.getpid()
        self._closed = False

    @classmethod
    def open(
        cls,
        *,
        data_root: object,
        runtime_factory: Callable[..., object] = _default_runtime_factory,
        cad_port_factory: Callable[..., object] = _default_cad_port_factory,
    ) -> AgentApplication:
        layout = ApplicationDataLayout.open(data_root)
        recover_bootstrap_cleanup(layout.bootstrap)
        leases = ResourceLeaseManager(
            layout.locks,
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        tasks = TaskRunStore(
            layout.tasks,
            leases,
            trust=TaskStoreRootTrust.TRUSTED_LOCAL,
        )
        revisions = LocalRevisionStore(
            layout.projects,
            leases,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        )
        return cls(
            layout=layout,
            lease_manager=leases,
            task_store=tasks,
            revision_store=revisions,
            runtime_factory=runtime_factory,
            cad_port_factory=cad_port_factory,
        )

    @staticmethod
    def execution_capabilities() -> dict[str, str | bool]:
        return {
            "headless": ("verified" if sys.platform == "darwin" else "unsupported_platform"),
            "offscreen_gui": "planned_unavailable",
            "interactive_gui": "planned_unavailable",
            "daemon": False,
            "authenticated_transport": False,
            "ipc_server": False,
        }

    def _ensure_live(self) -> None:
        if self._closed or os.getpid() != self._creator_pid:
            raise RuntimeError("AgentApplication is not live in this process")

    @staticmethod
    def _catalog_failure(error: TaskCatalogError) -> TaskServicePortFailure:
        return TaskServicePortFailure(code=_CATALOG_PORT_ERRORS[error.code])

    def bootstrap_empty(self) -> ProjectBootstrapResult:
        self._ensure_live()
        selected = _new_project_id()
        if type(selected) is not str or _PROJECT_ID.fullmatch(selected) is None:
            raise ValueError("invalid project id")
        lease = None
        result = None
        release_cleanup_required = False
        try:
            lease = self._lease_manager.acquire_project_write(selected)
            try:
                self._revision_store.initialize_empty_project(selected, lease)
            except RevisionStoreError as error:
                if error.code is RevisionStoreErrorCode.ALREADY_EXISTS:
                    raise ValueError("project bootstrap conflict") from None
                elif (
                    error.code is RevisionStoreErrorCode.DURABILITY_UNCERTAIN
                    and getattr(error, "head_committed", False) is True
                ):
                    pass
                else:
                    raise
            result = verify_generation_zero(self._revision_store, selected, None)
        finally:
            if lease is not None:
                try:
                    lease.release(owner_token=lease.owner_token)
                except Exception:
                    if (
                        type(result) is not ProjectBootstrapResult
                        or type(getattr(lease, "released", None)) is not bool
                        or lease.released is not True
                    ):
                        raise
                    release_cleanup_required = True
        if type(result) is not ProjectBootstrapResult:
            raise RevisionStoreError(RevisionStoreErrorCode.IO_ERROR)
        if release_cleanup_required:
            return ProjectBootstrapResult(
                head=result.head,
                revision=result.revision,
                cleanup_required=True,
            )
        return result

    def bootstrap_import(
        self,
        *,
        source: object,
    ) -> ProjectBootstrapResult:
        self._ensure_live()
        selected = _new_project_id()
        if type(selected) is not str or _PROJECT_ID.fullmatch(selected) is None:
            raise ValueError("invalid project id")
        with self._cad_gate:
            self._ensure_live()
            return bootstrap_import_project(
                project_id=selected,
                source=source,
                bootstrap_root=self._layout.bootstrap,
                revision_store=self._revision_store,
                lease_manager=self._lease_manager,
                cad_port_factory=self._cad_port_factory,
            )

    def create_task(
        self,
        *,
        task_id: str,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
    ) -> StoredTaskRun | TaskServicePortFailure:
        self._ensure_live()
        try:
            return self._catalog.create_task(
                task_id=task_id,
                project_id=project_id,
                reasoning_owner=reasoning_owner,
                review_policy=review_policy,
            )
        except TaskCatalogError as error:
            return self._catalog_failure(error)

    def get_task(self, *, task_id: str) -> StoredTaskRun | TaskServicePortFailure:
        self._ensure_live()
        try:
            return self._catalog.get_task(task_id=task_id)
        except TaskCatalogError as error:
            return self._catalog_failure(error)

    def reject_draft(
        self,
        *,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> StoredTaskRun | TaskServicePortFailure:
        self._ensure_live()
        try:
            return self._catalog.reject_draft(
                task_id=task_id,
                draft_id=draft_id,
                expected_generation=expected_generation,
            )
        except TaskCatalogError as error:
            return self._catalog_failure(error)

    def open_checkout(
        self,
        *,
        open_key: str,
        source: HeadCheckoutSource | DraftCheckoutSource,
    ) -> CheckoutDescriptor:
        self._ensure_live()
        return self._checkouts.open(open_key, source)

    def get_checkout(self, *, checkout_id: str) -> CheckoutDescriptor:
        self._ensure_live()
        return self._checkouts.get(checkout_id)

    def close_checkout(self, *, checkout_id: str) -> CheckoutDescriptor:
        self._ensure_live()
        return self._checkouts.close(checkout_id)

    def submit_model_program(
        self,
        *,
        task_id: str,
        expected_generation: int,
        program: ModelProgram,
    ) -> StoredTaskRun | TaskServicePortFailure:
        return self._cad_method(
            "submit_model_program",
            task_id=task_id,
            expected_generation=expected_generation,
            program=program,
        )

    def continue_task(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun | TaskServicePortFailure:
        return self._cad_method(
            "continue_task",
            task_id=task_id,
            expected_generation=expected_generation,
        )

    def reconcile_task(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun | TaskServicePortFailure:
        return self._cad_method(
            "reconcile_task",
            task_id=task_id,
            expected_generation=expected_generation,
        )

    def accept_draft(
        self,
        *,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> StoredTaskRun | TaskServicePortFailure:
        return self._cad_method(
            "accept_draft",
            task_id=task_id,
            draft_id=draft_id,
            expected_generation=expected_generation,
        )

    def _cad_method(self, method: str, **kwargs):
        self._ensure_live()
        try:
            stored = self._catalog.get_task(task_id=kwargs["task_id"])
        except TaskCatalogError as error:
            return self._catalog_failure(error)
        with self._cad_gate:
            self._ensure_live()
            runtime = self._runtime_for(stored.task_run.project_id)
            if type(runtime) is TaskServicePortFailure:
                return runtime
            try:
                result = getattr(runtime.service, method)(**kwargs)
            except Exception as error:
                from vibecad.workflow.service import (
                    TaskServiceError,
                    TaskServiceErrorCode,
                )

                if type(error) is not TaskServiceError:
                    raise
                mapping = {
                    TaskServiceErrorCode.INVALID_INPUT: (TaskServicePortErrorCode.INVALID_INPUT),
                    TaskServiceErrorCode.UNSUPPORTED_REASONING_OWNER: (
                        TaskServicePortErrorCode.UNSUPPORTED_REASONING_OWNER
                    ),
                    TaskServiceErrorCode.INVALID_STATE: (TaskServicePortErrorCode.INVALID_STATE),
                    TaskServiceErrorCode.NOT_FOUND: TaskServicePortErrorCode.NOT_FOUND,
                    TaskServiceErrorCode.CONFLICT: TaskServicePortErrorCode.CONFLICT,
                    TaskServiceErrorCode.STORE_FAILURE: (TaskServicePortErrorCode.STORE_FAILURE),
                    TaskServiceErrorCode.LEASE_UNAVAILABLE: (
                        TaskServicePortErrorCode.LEASE_UNAVAILABLE
                    ),
                    TaskServiceErrorCode.RECOVERY_REQUIRED: (
                        TaskServicePortErrorCode.RECOVERY_REQUIRED
                    ),
                }
                result = TaskServicePortFailure(code=mapping[error.code])
            if bool(getattr(runtime, "stale", False)):
                try:
                    closed = runtime.close()
                except Exception:
                    closed = False
                if closed is True and self._runtimes.get(stored.task_run.project_id) is runtime:
                    del self._runtimes[stored.task_run.project_id]
            return result

    def _runtime_for(self, project_id: str):
        try:
            lease = self._lease_manager.acquire_project_write(project_id)
        except LeaseError:
            return TaskServicePortFailure(code=TaskServicePortErrorCode.LEASE_UNAVAILABLE)
        result = None
        created = None
        try:
            head = self._revision_store.load_head(project_id)
            cached = self._runtimes.get(project_id)
            if cached is not None and cached.head == head:
                self._runtimes.move_to_end(project_id)
                result = cached
            elif cached is not None:
                if not _close_runtime(cached):
                    result = TaskServicePortFailure(
                        code=TaskServicePortErrorCode.RESOURCE_EXHAUSTED
                    )
                else:
                    del self._runtimes[project_id]
            while result is None and len(self._runtimes) >= MAX_PROJECT_RUNTIMES:
                evicted = False
                for key, candidate in tuple(self._runtimes.items()):
                    if not _close_runtime(candidate):
                        continue
                    del self._runtimes[key]
                    evicted = True
                    break
                if not evicted:
                    result = TaskServicePortFailure(
                        code=TaskServicePortErrorCode.RESOURCE_EXHAUSTED
                    )
            if result is None:
                created = self._runtime_factory(
                    project_id=project_id,
                    head=head,
                    task_store=self._task_store,
                    revision_store=self._revision_store,
                    lease_manager=self._lease_manager,
                )
                self._runtimes[project_id] = created
                result = created
        finally:
            release_failed = False
            try:
                lease.release(owner_token=lease.owner_token)
            except Exception:
                release_failed = True
        if release_failed:
            if created is not None and self._runtimes.get(project_id) is created:
                try:
                    closed = _close_runtime(created)
                except Exception:  # pragma: no cover - defensive against corruption
                    closed = False
                if closed and self._runtimes.get(project_id) is created:
                    del self._runtimes[project_id]
            return TaskServicePortFailure(code=TaskServicePortErrorCode.LEASE_UNAVAILABLE)
        return result

    def close(self) -> None:
        if self._closed:
            return
        if os.getpid() != self._creator_pid:
            raise RuntimeError("AgentApplication belongs to another process")
        with self._cad_gate:
            if self._closed:
                return
            for project_id, runtime in tuple(self._runtimes.items()):
                if _close_runtime(runtime) and self._runtimes.get(project_id) is runtime:
                    del self._runtimes[project_id]
            self._closed = True
