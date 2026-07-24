"""Lazy, durable AgentApplication implementing the neutral task-service port."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import sys
import threading
import time
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
    _initialize_candidate_file_limit_runtime,
)
from vibecad.interaction.checkouts import (
    CheckoutDescriptor,
    CheckoutFileSnapshot,
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
from vibecad.workflow.state import ReasoningOwner, ReviewPolicy, TaskEvent, TaskStatus
from vibecad.workflow.store import (
    StoredTaskRun,
    TaskRunStore,
    TaskStoreRootTrust,
)

__all__ = ("AgentApplication",)

MAX_PROJECT_RUNTIMES = 4
CAD_CANCELLATION_DRAIN_SECONDS = 5.0
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
    TaskCatalogErrorCode.RESOURCE_EXHAUSTED: TaskServicePortErrorCode.RESOURCE_EXHAUSTED,
    TaskCatalogErrorCode.RECOVERY_REQUIRED: TaskServicePortErrorCode.RECOVERY_REQUIRED,
}


def _new_project_id() -> str:
    return f"project_{secrets.token_hex(16)}"


def _has_cancellation_request(task: object) -> bool:
    return any(
        record.event
        in {
            TaskEvent.REQUEST_CANCEL,
            TaskEvent.REQUEST_ACTIVE_CANCEL,
        }
        for record in getattr(task, "transitions", ())
    )


def _default_runtime_factory(**kwargs):
    from vibecad.application.project import build_project_runtime

    return build_project_runtime(**kwargs)


def _close_runtime(runtime: object) -> bool:
    try:
        return runtime.close() is True
    except Exception:
        return False


def _require_current_layout(layout: ApplicationDataLayout) -> None:
    for path in (
        layout.root,
        layout.locks,
        layout.tasks,
        layout.projects,
        layout.bootstrap,
        layout.checkouts,
        layout.artifacts,
    ):
        layout.require_current(path)


def _close_application_candidate(application: AgentApplication) -> None:
    try:
        application.close()
    except Exception:
        pass


class _DescriptorProjectCreatePort:
    __slots__ = ("_descriptor", "_identity", "_locator", "_service", "_source")

    def __init__(
        self,
        *,
        service: object,
        descriptor: int,
        locator: str,
        identity: dict[str, object],
    ) -> None:
        self._service = service
        self._descriptor = descriptor
        self._locator = locator
        self._identity = dict(identity)
        self._source = f"/descriptor/{locator}.FCStd"

    @property
    def source(self) -> str:
        return self._source

    def create_project(
        self,
        *,
        create_key: str,
        kind: object,
        source_path: str | None,
    ):
        from vibecad.application.project_api import (
            ProjectKind,
            ProjectServicePortErrorCode,
            ProjectServicePortFailure,
        )

        if kind is not ProjectKind.IMPORT_FCSTD or source_path != self._source:
            return ProjectServicePortFailure(code=ProjectServicePortErrorCode.INVALID_INPUT)
        return self._service.create_project_from_descriptor(
            create_key=create_key,
            source_fd=self._descriptor,
            source_locator=self._locator,
            source_identity=self._identity,
        )


class AgentApplication:
    """Process-owned composition root; CAD dependencies remain lazy."""

    __slots__ = (
        "_artifact_api",
        "_artifact_authority",
        "_artifact_service",
        "_artifact_store",
        "_cad_execution_port",
        "_cad_admission_condition",
        "_cad_fence_required",
        "_cad_fence_task_id",
        "_cad_gate",
        "_cad_task_admissions",
        "_cad_validation_port",
        "_cancellation_reconcile_lock",
        "_catalog",
        "_cad_port_factory",
        "_checkouts",
        "_closed",
        "_close_lock",
        "_component_lock",
        "_creator_pid",
        "_direct_api",
        "_generation_epoch",
        "_generation_lock",
        "_layout",
        "_lease_manager",
        "_project_api",
        "_project_service",
        "_revision_store",
        "_runtime_factory",
        "_runtimes",
        "_task_api",
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
        try:
            lock_identity = layout.identity_for(layout.locks)
            task_identity = layout.identity_for(layout.tasks)
            project_identity = layout.identity_for(layout.projects)
            checkout_identity = layout.identity_for(layout.checkouts)
        except Exception:
            raise TypeError("invalid AgentApplication composition") from None
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
            and getattr(lease_manager, "_root_identity", None) == lock_identity
            and getattr(task_store, "_root_parts", None) == layout.tasks.parts
            and getattr(task_store, "_root_identity", None) == task_identity
            and getattr(revision_store, "_root", None) == layout.projects
            and getattr(revision_store, "_identity", None) == project_identity
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
        checkouts = ManagedCheckoutStore(
            layout.checkouts,
            layout.locks,
            revision_store,
            task_store,
            trust=CheckoutStoreRootTrust.TRUSTED_LOCAL,
        )
        if not (
            getattr(getattr(checkouts, "_root", None), "identity", None) == checkout_identity
            and getattr(getattr(checkouts, "_lock_root", None), "identity", None) == lock_identity
        ):
            raise TypeError("invalid AgentApplication composition")
        self._checkouts = checkouts
        self._runtime_factory = runtime_factory
        self._cad_port_factory = cad_port_factory
        self._runtimes: OrderedDict[str, object] = OrderedDict()
        self._cad_gate = _PROCESS_CAD_GATE
        self._cad_task_admissions: dict[str, int] = {}
        self._generation_epoch = 0
        self._generation_lock = threading.Lock()
        self._cad_admission_condition = threading.Condition(self._generation_lock)
        self._cad_fence_required = False
        self._cad_fence_task_id = None
        self._cancellation_reconcile_lock = threading.Lock()
        self._component_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._creator_pid = os.getpid()
        self._closed = False
        self._task_api = None
        self._direct_api = None
        self._project_service = None
        self._project_api = None
        self._cad_execution_port = None
        self._cad_validation_port = None
        self._artifact_store = None
        self._artifact_authority = None
        self._artifact_service = None
        self._artifact_api = None

    @classmethod
    def from_captured_layout(
        cls,
        *,
        layout,
        lease_manager,
        runtime_factory: Callable[..., object] = _default_runtime_factory,
        cad_port_factory: Callable[..., object] = _default_cad_port_factory,
    ) -> AgentApplication:
        if not (
            type(layout) is ApplicationDataLayout
            and type(lease_manager) is ResourceLeaseManager
            and callable(runtime_factory)
            and callable(cad_port_factory)
        ):
            raise TypeError("invalid AgentApplication composition")
        try:
            lock_identity = layout.identity_for(layout.locks)
        except Exception:
            raise TypeError("invalid AgentApplication composition") from None
        if not (
            getattr(lease_manager, "_creator_pid", None) == os.getpid()
            and getattr(lease_manager, "_root_parts", None) == layout.locks.parts
            and getattr(lease_manager, "_root_identity", None) == lock_identity
        ):
            raise TypeError("invalid AgentApplication composition")

        _initialize_candidate_file_limit_runtime()
        _require_current_layout(layout)
        recover_bootstrap_cleanup(layout.bootstrap)
        _require_current_layout(layout)
        tasks = TaskRunStore(
            layout.tasks,
            lease_manager,
            trust=TaskStoreRootTrust.TRUSTED_LOCAL,
        )
        if not (
            getattr(tasks, "_lease_manager", None) is lease_manager
            and getattr(tasks, "_root_parts", None) == layout.tasks.parts
            and getattr(tasks, "_root_identity", None) == layout.identity_for(layout.tasks)
        ):
            raise TypeError("invalid AgentApplication composition")
        revisions = LocalRevisionStore(
            layout.projects,
            lease_manager,
            trust=RevisionStoreRootTrust.TRUSTED_LOCAL,
        )
        if not (
            getattr(revisions, "_lease_manager", None) is lease_manager
            and getattr(revisions, "_root", None) == layout.projects
            and getattr(revisions, "_identity", None) == layout.identity_for(layout.projects)
        ):
            raise TypeError("invalid AgentApplication composition")
        application = None
        try:
            application = cls(
                layout=layout,
                lease_manager=lease_manager,
                task_store=tasks,
                revision_store=revisions,
                runtime_factory=runtime_factory,
                cad_port_factory=cad_port_factory,
            )
            if not (
                type(application) is cls
                and application._layout is layout
                and application._lease_manager is lease_manager
                and application._task_store is tasks
                and application._revision_store is revisions
                and application._runtime_factory is runtime_factory
                and application._cad_port_factory is cad_port_factory
            ):
                raise TypeError("invalid AgentApplication composition")
            _require_current_layout(layout)
        except BaseException:
            if application is not None:
                _close_application_candidate(application)
            raise
        return application

    @classmethod
    def open(
        cls,
        *,
        data_root: object,
        runtime_factory: Callable[..., object] = _default_runtime_factory,
        cad_port_factory: Callable[..., object] = _default_cad_port_factory,
    ) -> AgentApplication:
        layout = ApplicationDataLayout.open(data_root)
        leases = ResourceLeaseManager(
            layout.locks,
            trust=LeaseRootTrust.TRUSTED_LOCAL,
        )
        return cls.from_captured_layout(
            layout=layout,
            lease_manager=leases,
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

    def _task_api_for_request(self):
        self._ensure_live()
        api = self._task_api
        if api is not None:
            return api
        with self._component_lock:
            self._ensure_live()
            api = self._task_api
            if api is None:
                from vibecad.application.task_api import TaskApi

                api = TaskApi(port=self)
                self._task_api = api
            return api

    def _direct_api_for_request(self):
        self._ensure_live()
        api = self._direct_api
        if api is not None:
            return api
        with self._component_lock:
            self._ensure_live()
            api = self._direct_api
            if api is None:
                from vibecad.application.public_surface import DirectOperationApi

                api = DirectOperationApi(port=self)
                self._direct_api = api
            return api

    def _cad_validation_port_locked(self):
        port = self._cad_validation_port
        if port is not None:
            return port

        from vibecad.interaction.cad import CadExecutionPort

        class _LazyGatedCadExecutionPort(CadExecutionPort):
            __slots__ = ("_application",)

            def __init__(self, application: AgentApplication) -> None:
                self._application = application

            def validate_import(self, path):
                return self._application._invoke_validation_cad("validate_import", path)

            def revalidate_normalized_import(self, path):
                return self._application._invoke_validation_cad(
                    "revalidate_normalized_import",
                    path,
                )

            def validate_materialization(self, *, fcstd, step):
                return self._application._invoke_validation_cad(
                    "validate_materialization",
                    fcstd=fcstd,
                    step=step,
                )

        port = _LazyGatedCadExecutionPort(self)
        self._cad_validation_port = port
        return port

    def _cad_validation_port_for_request(self):
        self._ensure_live()
        port = self._cad_validation_port
        if port is not None:
            return port
        with self._component_lock:
            self._ensure_live()
            return self._cad_validation_port_locked()

    def _validation_cad_factory(self, *, revision_store: object):
        self._ensure_live()
        if revision_store is not self._revision_store:
            raise TypeError("invalid CAD validation composition")
        port = self._cad_validation_port
        if port is None:
            return self._cad_validation_port_for_request()
        return port

    def _invoke_validation_cad(self, method: str, *args, **kwargs):
        self._ensure_live()
        with self._generation_lock:
            generation_epoch = self._generation_epoch
        with self._cad_gate:
            self._ensure_live()
            port = self._cad_execution_port_under_gate(
                expected_generation_epoch=generation_epoch,
            )
            if port is None:
                from vibecad.execution.errors import ExecutorError, ExecutorErrorCode

                raise ExecutorError(ExecutorErrorCode.CAD_FAILURE)
            try:
                return getattr(port, method)(*args, **kwargs)
            finally:
                self._retire_lost_generation(port)

    def _cad_execution_port_under_gate(
        self,
        *,
        expected_generation_epoch: int | None = None,
    ):
        port = getattr(self, "_cad_execution_port", None)
        current_epoch = getattr(self, "_generation_epoch", 0)
        if getattr(self, "_cad_fence_required", False) or (
            expected_generation_epoch is not None and current_epoch != expected_generation_epoch
        ):
            return None
        if port is not None:
            return port
        from vibecad.interaction.cad import CadExecutionPort

        factory = getattr(self, "_cad_port_factory", None)
        if factory is None:
            return None
        generation_lock = getattr(self, "_generation_lock", None)
        lock = generation_lock if generation_lock is not None else contextlib.nullcontext()
        with lock:
            current_epoch = getattr(self, "_generation_epoch", 0)
            if getattr(self, "_cad_fence_required", False) or (
                expected_generation_epoch is not None and current_epoch != expected_generation_epoch
            ):
                return None
            port = getattr(self, "_cad_execution_port", None)
            if port is not None:
                return port
            candidate = factory(revision_store=self._revision_store)
            if not isinstance(candidate, CadExecutionPort):
                raise TypeError("CAD factory returned an invalid execution port")
            self._cad_execution_port = candidate
            return candidate

    def _project_api_for_request(self):
        self._ensure_live()
        api = self._project_api
        if api is not None:
            return api
        with self._component_lock:
            self._ensure_live()
            api = self._project_api
            if api is None:
                from vibecad.application.project_api import ProjectApi

                api = ProjectApi(port=self)
                self._project_api = api
            return api

    def _project_bundle_for_request(self):
        self._ensure_live()
        api = self._project_api
        service = self._project_service
        if api is not None and service is not None:
            return api, service
        with self._component_lock:
            self._ensure_live()
            api = self._project_api
            service = self._project_service
            if api is None or service is None:
                from vibecad.application.project_api import ProjectApi
                from vibecad.application.project_create import DurableProjectService

                self._cad_validation_port_locked()
                candidate_service = DurableProjectService(
                    bootstrap_root=self._layout.bootstrap,
                    data_root=self._layout.root,
                    expected_bootstrap_identity=self._layout.identity_for(self._layout.bootstrap),
                    expected_data_identity=self._layout.identity_for(self._layout.root),
                    revision_store=self._revision_store,
                    lease_manager=self._lease_manager,
                    cad_port_factory=self._validation_cad_factory,
                )
                candidate_api = api if api is not None else ProjectApi(port=self)
                self._project_service = candidate_service
                self._project_api = candidate_api
                service = candidate_service
                api = candidate_api
            return api, service

    def _artifact_api_for_manifest_request(self):
        self._ensure_live()
        api = self._artifact_api
        if api is not None:
            return api
        with self._component_lock:
            self._ensure_live()
            api = self._artifact_api
            if api is None:
                from vibecad.application.artifacts import ArtifactApi

                api = ArtifactApi(port=self)
                self._artifact_api = api
            return api

    def _artifact_bundle_for_request(self):
        self._ensure_live()
        api = self._artifact_api
        service = self._artifact_service
        store = self._artifact_store
        if api is not None and service is not None and store is not None:
            return api, service, store
        with self._component_lock:
            self._ensure_live()
            api = self._artifact_api
            service = self._artifact_service
            store = self._artifact_store
            if api is None or service is None or store is None:
                from vibecad.application.artifacts import (
                    ArtifactApi,
                    ArtifactMaterializationService,
                    ArtifactStore,
                )

                cad = self._cad_validation_port_locked()
                authority = self._artifact_authority_locked()
                candidate_store = None
                try:
                    candidate_store = ArtifactStore(
                        root=self._layout.artifacts,
                        expected_root_identity=self._layout.identity_for(self._layout.artifacts),
                    )
                    service = ArtifactMaterializationService(
                        store=candidate_store,
                        authority=authority,
                        cad=cad,
                    )
                    api = ArtifactApi(port=self)
                except BaseException as error:
                    if candidate_store is not None:
                        try:
                            candidate_store.close()
                        except BaseException as close_error:
                            raise close_error from error
                    raise
                self._artifact_store = candidate_store
                self._artifact_service = service
                self._artifact_api = api
                store = candidate_store
            return api, service, store

    def _artifact_authority_locked(self):
        self._ensure_live()
        authority = self._artifact_authority
        if authority is None:
            from vibecad.application.artifacts import LocalArtifactAuthority

            authority = LocalArtifactAuthority(
                task_store=self._task_store,
                revision_store=self._revision_store,
                lease_manager=self._lease_manager,
            )
            self._artifact_authority = authority
        return authority

    def _artifact_authority_for_transition(self):
        self._ensure_live()
        authority = self._artifact_authority
        if authority is not None:
            return authority
        with self._component_lock:
            self._ensure_live()
            return self._artifact_authority_locked()

    @staticmethod
    def _artifact_gate_failure(error: object) -> TaskServicePortFailure:
        from vibecad.application.artifacts import (
            ArtifactDependencyError,
            ArtifactDependencyErrorCode,
        )

        if type(error) is not ArtifactDependencyError:
            return TaskServicePortFailure(code=TaskServicePortErrorCode.STORE_FAILURE)
        mapping = {
            ArtifactDependencyErrorCode.NOT_FOUND: TaskServicePortErrorCode.NOT_FOUND,
            ArtifactDependencyErrorCode.INVALID_STATE: TaskServicePortErrorCode.INVALID_STATE,
            ArtifactDependencyErrorCode.CONFLICT: TaskServicePortErrorCode.CONFLICT,
            ArtifactDependencyErrorCode.LEASE_UNAVAILABLE: (
                TaskServicePortErrorCode.LEASE_UNAVAILABLE
            ),
            ArtifactDependencyErrorCode.RESOURCE_EXHAUSTED: (
                TaskServicePortErrorCode.RESOURCE_EXHAUSTED
            ),
            ArtifactDependencyErrorCode.INTEGRITY_FAILURE: (TaskServicePortErrorCode.STORE_FAILURE),
            ArtifactDependencyErrorCode.CAD_FAILURE: TaskServicePortErrorCode.STORE_FAILURE,
            ArtifactDependencyErrorCode.STORE_FAILURE: TaskServicePortErrorCode.STORE_FAILURE,
            ArtifactDependencyErrorCode.RECOVERY_REQUIRED: (
                TaskServicePortErrorCode.RECOVERY_REQUIRED
            ),
            ArtifactDependencyErrorCode.RUNTIME_UNAVAILABLE: (
                TaskServicePortErrorCode.STORE_FAILURE
            ),
            ArtifactDependencyErrorCode.INTERNAL_ERROR: TaskServicePortErrorCode.STORE_FAILURE,
        }
        return TaskServicePortFailure(code=mapping[error.code])

    def _review_transition(self, *, task_id: str, body):
        self._ensure_live()
        try:
            authority = self._artifact_authority_for_transition()
            gate = authority.acquire_export_gate(task_id=task_id)
        except Exception as error:
            return self._artifact_gate_failure(error)

        entered = False
        body_completed = False
        try:
            with gate:
                entered = True
                self._ensure_live()
                result = body()
                body_completed = True
        except Exception as error:
            from vibecad.application.artifacts import ArtifactDependencyError

            if type(error) is ArtifactDependencyError:
                if entered:
                    return TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
                return self._artifact_gate_failure(error)
            if body_completed:
                return TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
            if not entered:
                return TaskServicePortFailure(code=TaskServicePortErrorCode.STORE_FAILURE)
            raise
        return result

    def create_project_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api, _ = self._project_bundle_for_request()
        self._ensure_live()
        return api.create_project(request)

    def import_project_descriptor_request(
        self,
        request: object,
        *,
        source_fd: object,
        locator: object,
    ) -> dict[str, object]:
        self._ensure_live()
        _, service = self._project_bundle_for_request()
        self._ensure_live()
        if (
            type(source_fd) is not int
            or source_fd < 0
            or type(locator) is not dict
            or type(locator.get("digest")) is not str
        ):
            candidate = request
            proxy = _DescriptorProjectCreatePort(
                service=service,
                descriptor=-1,
                locator="0" * 64,
                identity={},
            )
        else:
            proxy = _DescriptorProjectCreatePort(
                service=service,
                descriptor=source_fd,
                locator=locator["digest"],
                identity=locator,
            )
            candidate = request
        if type(candidate) is dict:
            candidate = dict(candidate)
            candidate.setdefault("source_path", proxy.source)
        from vibecad.application.project_api import ProjectApi

        return ProjectApi(port=proxy).create_project(candidate)

    def get_project_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api, _ = self._project_bundle_for_request()
        self._ensure_live()
        return api.get_project(request)

    def list_projects_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._project_api_for_request()
        self._ensure_live()
        return api.list_projects(request)

    def list_revisions_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._project_api_for_request()
        self._ensure_live()
        return api.list_revisions(request)

    def compare_revisions_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._project_api_for_request()
        self._ensure_live()
        return api.compare_revisions(request)

    def revert_project_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.revert_project(request)

    def create_task_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.create_task(request)

    def list_tasks_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.list_tasks(request)

    def get_task_events_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.get_task_events(request)

    def get_task_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.get_task(request)

    def submit_model_program_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.submit_model_program(request)

    def resume_task_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.resume_task(request)

    def cancel_task_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.cancel_task(request)

    def accept_draft_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.accept_draft(request)

    def reject_draft_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.reject_draft(request)

    def get_capabilities_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._task_api_for_request()
        self._ensure_live()
        return api.get_capabilities(request)

    def invoke_direct_operation_request(
        self,
        operation: object,
        request: object,
    ) -> dict[str, object]:
        self._ensure_live()
        api = self._direct_api_for_request()
        self._ensure_live()
        return api.invoke(operation, request)

    def export_task_artifacts_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api, _, _ = self._artifact_bundle_for_request()
        self._ensure_live()
        return api.export_task_artifacts(request)

    def get_artifact_manifest_request(self, request: object) -> dict[str, object]:
        self._ensure_live()
        api = self._artifact_api_for_manifest_request()
        self._ensure_live()
        return api.get_artifact_manifest(request)

    def read_artifact_resource(self, uri: object):
        self._ensure_live()
        _, _, store = self._artifact_bundle_for_request()
        self._ensure_live()
        return store.read_resource(uri)

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
        cad = self._cad_validation_port_for_request()
        self._ensure_live()
        return bootstrap_import_project(
            project_id=selected,
            source=source,
            bootstrap_root=self._layout.bootstrap,
            revision_store=self._revision_store,
            lease_manager=self._lease_manager,
            cad_port_factory=lambda **_kwargs: cad,
        )

    def create_project(
        self,
        *,
        create_key: str,
        kind: object,
        source_path: str | None,
    ):
        self._ensure_live()
        _, service = self._project_bundle_for_request()
        self._ensure_live()
        return service.create_project(
            create_key=create_key,
            kind=kind,
            source_path=source_path,
        )

    def get_project(self, *, project_id: str):
        self._ensure_live()
        _, service = self._project_bundle_for_request()
        self._ensure_live()
        return service.get_project(project_id=project_id)

    @staticmethod
    def _revision_discovery_failure(error):
        from vibecad.application.project_api import (
            ProjectServicePortErrorCode,
            ProjectServicePortFailure,
        )

        mapping = {
            "invalid_input": ProjectServicePortErrorCode.INVALID_INPUT,
            "not_found": ProjectServicePortErrorCode.NOT_FOUND,
            "conflict": ProjectServicePortErrorCode.CONFLICT,
            "resource_exhausted": (ProjectServicePortErrorCode.RESOURCE_EXHAUSTED),
            "integrity_failure": (ProjectServicePortErrorCode.INTEGRITY_FAILURE),
            "store_failure": ProjectServicePortErrorCode.STORE_FAILURE,
            "recovery_required": (ProjectServicePortErrorCode.RECOVERY_REQUIRED),
        }
        code = getattr(getattr(error, "code", None), "value", None)
        return ProjectServicePortFailure(
            code=mapping.get(code, ProjectServicePortErrorCode.INTERNAL_ERROR)
        )

    def list_projects(self, *, limit: int, cursor: str | None):
        self._ensure_live()
        from vibecad.application.revision_discovery import (
            RevisionDiscoveryError,
            RevisionDiscoveryService,
        )

        try:
            return RevisionDiscoveryService(store=self._revision_store).list_projects(
                limit=limit,
                cursor=cursor,
            )
        except RevisionDiscoveryError as error:
            return self._revision_discovery_failure(error)

    def list_revisions(
        self,
        *,
        project_id: str,
        limit: int,
        cursor: str | None,
    ):
        self._ensure_live()
        from vibecad.application.revision_discovery import (
            RevisionDiscoveryError,
            RevisionDiscoveryService,
        )

        try:
            return RevisionDiscoveryService(store=self._revision_store).list_revisions(
                project_id=project_id,
                limit=limit,
                cursor=cursor,
            )
        except RevisionDiscoveryError as error:
            return self._revision_discovery_failure(error)

    def compare_revisions(
        self,
        *,
        project_id: str,
        from_revision: str,
        to_revision: str,
    ):
        self._ensure_live()
        from vibecad.application.revision_compare import (
            RevisionCompareError,
            RevisionCompareService,
        )

        try:
            return RevisionCompareService(store=self._revision_store).compare_revisions(
                project_id=project_id,
                from_revision=from_revision,
                to_revision=to_revision,
            )
        except RevisionCompareError as error:
            return self._revision_discovery_failure(error)

    def revert_project(
        self,
        *,
        revert_key: str,
        project_id: str,
        source_revision: str,
        expected_head: str,
    ) -> StoredTaskRun | TaskServicePortFailure:
        """Replay without CAD when possible, otherwise enter the project runtime."""

        self._ensure_live()
        from vibecad.workflow.revert import (
            RevertProgramError,
            revert_task_identity,
        )

        try:
            task_id, _creation_digest = revert_task_identity(revert_key)
        except RevertProgramError:
            return TaskServicePortFailure(code=TaskServicePortErrorCode.INVALID_INPUT)
        existing = self._revert_replay(
            task_id=task_id,
            revert_key=revert_key,
            project_id=project_id,
            source_revision=source_revision,
            expected_head=expected_head,
        )
        if existing is not None:
            return existing
        prepared, created_here = self._prepare_revert_task(
            revert_key=revert_key,
            project_id=project_id,
            source_revision=source_revision,
            expected_head=expected_head,
        )
        if (
            type(prepared) is not StoredTaskRun
            or prepared.task_run.status is not TaskStatus.PROGRAM_READY
        ):
            return prepared
        if not created_here:
            return prepared

        with self._cad_gate:
            self._ensure_live()
            with self._cad_task_admission(task_id) as generation_epoch:
                port = None
                try:
                    current = self._revert_replay(
                        task_id=task_id,
                        revert_key=revert_key,
                        project_id=project_id,
                        source_revision=source_revision,
                        expected_head=expected_head,
                    )
                    if (
                        type(current) is not StoredTaskRun
                        or current.task_run.status is not TaskStatus.PROGRAM_READY
                    ):
                        return current
                    try:
                        runtime = self._runtime_for(
                            project_id,
                            expected_generation_epoch=generation_epoch,
                        )
                    except Exception as error:
                        from vibecad.execution.errors import ExecutorError

                        if type(error) is ExecutorError:
                            cancellation = self._await_durable_cancellation(
                                task_id,
                                minimum_generation=0,
                            )
                            if cancellation is not None:
                                return cancellation
                            return TaskServicePortFailure(
                                code=TaskServicePortErrorCode.RECOVERY_REQUIRED
                            )
                        raise
                    port = getattr(self, "_cad_execution_port", None)
                    if type(runtime) is TaskServicePortFailure:
                        cancellation = self._await_durable_cancellation(
                            task_id,
                            minimum_generation=0,
                        )
                        return cancellation if cancellation is not None else runtime
                    try:
                        result = runtime.service.revert_project(
                            revert_key=revert_key,
                            project_id=project_id,
                            source_revision=source_revision,
                            expected_head=expected_head,
                        )
                    except Exception as error:
                        result = self._task_service_failure(error)
                    if type(result) is TaskServicePortFailure:
                        cancellation = self._await_durable_cancellation(
                            task_id,
                            minimum_generation=0,
                        )
                        if cancellation is not None:
                            result = cancellation
                    if bool(getattr(runtime, "stale", False)):
                        try:
                            closed = runtime.close()
                        except Exception:
                            closed = False
                        if closed is True:
                            with self._generation_lock:
                                if self._runtimes.get(project_id) is runtime:
                                    del self._runtimes[project_id]
                    return result
                finally:
                    if port is None:
                        port = getattr(self, "_cad_execution_port", None)
                    if port is not None:
                        self._retire_lost_generation(port, task_id=task_id)

    def _prepare_revert_task(
        self,
        *,
        revert_key: str,
        project_id: str,
        source_revision: str,
        expected_head: str,
    ) -> tuple[StoredTaskRun | TaskServicePortFailure, bool]:
        from vibecad.workflow.revert import (
            RevertProgramError,
            RevertProgramErrorCode,
            build_revert_binding,
            parse_bound_revert_task,
        )
        from vibecad.workflow.service import (
            TaskServiceError,
            _load_revert_source_from_store,
        )

        try:
            with self._lease_manager.acquire_project_write(project_id):
                head = self._revision_store.load_head(project_id)
                if head.revision_id != expected_head:
                    return (
                        TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT),
                        False,
                    )
                source = _load_revert_source_from_store(
                    self._revision_store,
                    project_id=project_id,
                    source_revision=source_revision,
                    head=head,
                )
                binding = build_revert_binding(
                    revert_key=revert_key,
                    project_id=project_id,
                    source_revision=source,
                    expected_head=head,
                )
                stored, created_here = self._catalog.create_revert_task_with_disposition(
                    revert_key=revert_key,
                    project_id=project_id,
                    source_revision=source,
                    expected_head=head,
                )
                if parse_bound_revert_task(stored) != binding:
                    return (
                        TaskServicePortFailure(code=TaskServicePortErrorCode.CONFLICT),
                        False,
                    )
                return stored, created_here
        except LeaseError:
            return (
                TaskServicePortFailure(code=TaskServicePortErrorCode.LEASE_UNAVAILABLE),
                False,
            )
        except TaskCatalogError as error:
            return self._catalog_failure(error), False
        except RevertProgramError as error:
            return (
                TaskServicePortFailure(
                    code=(
                        TaskServicePortErrorCode.CONFLICT
                        if error.code is RevertProgramErrorCode.CONFLICT
                        else TaskServicePortErrorCode.INVALID_INPUT
                    )
                ),
                False,
            )
        except TaskServiceError as error:
            return self._task_service_failure(error), False
        except Exception:
            return (
                TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED),
                False,
            )

    def _revert_replay(
        self,
        *,
        task_id: str,
        revert_key: str,
        project_id: str,
        source_revision: str,
        expected_head: str,
    ) -> StoredTaskRun | TaskServicePortFailure | None:
        from vibecad.workflow.revert import (
            RevertProgramError,
            RevertProgramErrorCode,
            require_matching_revert_task,
        )

        try:
            existing = self._catalog.get_task(task_id=task_id)
        except TaskCatalogError as error:
            if error.code is not TaskCatalogErrorCode.NOT_FOUND:
                return self._catalog_failure(error)
            return None
        try:
            require_matching_revert_task(
                existing,
                revert_key=revert_key,
                project_id=project_id,
                source_revision=source_revision,
                expected_head=expected_head,
            )
        except RevertProgramError as error:
            code = (
                TaskServicePortErrorCode.CONFLICT
                if error.code is RevertProgramErrorCode.CONFLICT
                else TaskServicePortErrorCode.INVALID_INPUT
            )
            return TaskServicePortFailure(code=code)
        return existing

    def get_artifact_manifest(self, *, request: object):
        self._ensure_live()
        from vibecad.application.artifact_manifest import (
            ArtifactManifestError,
            ArtifactManifestService,
        )
        from vibecad.application.artifacts import (
            ArtifactManifestRequest,
            ArtifactServiceErrorCode,
            ArtifactServicePortFailure,
        )

        if type(request) is not ArtifactManifestRequest:
            return ArtifactServicePortFailure(code=ArtifactServiceErrorCode.INVALID_INPUT)
        try:
            service = ArtifactManifestService(
                task_store=self._task_store,
                revision_store=self._revision_store,
                artifact_root=self._layout.artifacts,
                expected_artifact_root_identity=self._layout.identity_for(self._layout.artifacts),
            )
            return service.get_artifact_manifest(
                task_id=request.task_id,
                expected_generation=request.expected_generation,
                revision_id=request.revision_id,
                draft_id=request.draft_id,
            )
        except ArtifactManifestError as error:
            try:
                code = ArtifactServiceErrorCode(error.code.value)
            except (AttributeError, ValueError):
                code = ArtifactServiceErrorCode.INTERNAL_ERROR
            return ArtifactServicePortFailure(code=code)

    def export_task_artifacts(self, *, request: object):
        self._ensure_live()
        _, service, _ = self._artifact_bundle_for_request()
        self._ensure_live()
        return service.export_task_artifacts(request=request)

    def create_task(
        self,
        *,
        project_id: str,
        reasoning_owner: ReasoningOwner,
        review_policy: ReviewPolicy,
        task_id: str | None = None,
        create_key: str | None = None,
    ) -> StoredTaskRun | TaskServicePortFailure:
        self._ensure_live()
        try:
            return self._catalog.create_task(
                project_id=project_id,
                reasoning_owner=reasoning_owner,
                review_policy=review_policy,
                task_id=task_id,
                create_key=create_key,
            )
        except TaskCatalogError as error:
            return self._catalog_failure(error)

    def get_task(self, *, task_id: str) -> StoredTaskRun | TaskServicePortFailure:
        self._ensure_live()
        try:
            return self._catalog.get_task(task_id=task_id)
        except TaskCatalogError as error:
            return self._catalog_failure(error)

    def list_tasks(
        self,
        *,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object] | TaskServicePortFailure:
        self._ensure_live()
        from vibecad.application.discovery import (
            TaskDiscoveryError,
            TaskDiscoveryErrorCode,
            TaskDiscoveryService,
        )

        try:
            return TaskDiscoveryService(catalog=self._catalog).list_tasks(
                limit=limit,
                cursor=cursor,
            )
        except TaskDiscoveryError as error:
            mapping = {
                TaskDiscoveryErrorCode.INVALID_INPUT: TaskServicePortErrorCode.INVALID_INPUT,
                TaskDiscoveryErrorCode.NOT_FOUND: TaskServicePortErrorCode.NOT_FOUND,
                TaskDiscoveryErrorCode.CONFLICT: TaskServicePortErrorCode.CONFLICT,
                TaskDiscoveryErrorCode.RESOURCE_EXHAUSTED: (
                    TaskServicePortErrorCode.RESOURCE_EXHAUSTED
                ),
                TaskDiscoveryErrorCode.STORE_FAILURE: TaskServicePortErrorCode.STORE_FAILURE,
            }
            return TaskServicePortFailure(code=mapping[error.code])

    def get_task_events(
        self,
        *,
        task_id: str,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object] | TaskServicePortFailure:
        self._ensure_live()
        from vibecad.application.discovery import (
            TaskDiscoveryError,
            TaskDiscoveryErrorCode,
            TaskDiscoveryService,
        )

        try:
            return TaskDiscoveryService(catalog=self._catalog).get_task_events(
                task_id=task_id,
                limit=limit,
                cursor=cursor,
            )
        except TaskDiscoveryError as error:
            mapping = {
                TaskDiscoveryErrorCode.INVALID_INPUT: TaskServicePortErrorCode.INVALID_INPUT,
                TaskDiscoveryErrorCode.NOT_FOUND: TaskServicePortErrorCode.NOT_FOUND,
                TaskDiscoveryErrorCode.CONFLICT: TaskServicePortErrorCode.CONFLICT,
                TaskDiscoveryErrorCode.RESOURCE_EXHAUSTED: (
                    TaskServicePortErrorCode.RESOURCE_EXHAUSTED
                ),
                TaskDiscoveryErrorCode.STORE_FAILURE: TaskServicePortErrorCode.STORE_FAILURE,
            }
            return TaskServicePortFailure(code=mapping[error.code])

    def reject_draft(
        self,
        *,
        task_id: str,
        draft_id: str,
        expected_generation: int,
    ) -> StoredTaskRun | TaskServicePortFailure:
        def reject():
            try:
                return self._catalog.reject_draft(
                    task_id=task_id,
                    draft_id=draft_id,
                    expected_generation=expected_generation,
                )
            except TaskCatalogError as error:
                return self._catalog_failure(error)

        return self._review_transition(task_id=task_id, body=reject)

    @contextlib.contextmanager
    def _cad_task_admission(self, task_id: str):
        with self._generation_lock:
            generation_epoch = self._generation_epoch
            admissions = getattr(self, "_cad_task_admissions", None)
            if admissions is None:
                admissions = {}
                self._cad_task_admissions = admissions
            admissions[task_id] = admissions.get(task_id, 0) + 1
        try:
            yield generation_epoch
        finally:
            condition = getattr(self, "_cad_admission_condition", None)
            lock = condition if condition is not None else self._generation_lock
            with lock:
                admissions = getattr(self, "_cad_task_admissions", {})
                admitted = admissions.get(task_id, 0)
                if admitted <= 1:
                    admissions.pop(task_id, None)
                else:
                    admissions[task_id] = admitted - 1
                if condition is not None:
                    condition.notify_all()

    def _wait_for_cad_task_drain(self, task_id: str) -> bool:
        condition = getattr(self, "_cad_admission_condition", None)
        if condition is None:
            with self._cad_gate:
                return getattr(self, "_cad_task_admissions", {}).get(task_id, 0) == 0
        deadline = time.monotonic() + CAD_CANCELLATION_DRAIN_SECONDS
        with condition:
            while self._cad_task_admissions.get(task_id, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                condition.wait(timeout=remaining)
        return True

    def _cancel_reservation_presence(self, stored: StoredTaskRun):
        from vibecad.execution.revisions import (
            CandidateReservationPresence,
            CandidateReservationPresenceStatus,
        )
        from vibecad.workflow.revert import parse_bound_revert_task

        task = stored.task_run
        binding = parse_bound_revert_task(task)
        reservation_key = binding.reservation_key if binding is not None else task.id
        try:
            result = self._revision_store.probe_candidate_reservation(
                task.project_id,
                task.base_revision,
                reservation_key,
            )
        except Exception:
            return CandidateReservationPresenceStatus.AMBIGUOUS
        if type(result) is not CandidateReservationPresence:
            return CandidateReservationPresenceStatus.AMBIGUOUS
        return result.status

    @staticmethod
    def _cancellation_reservation_key(stored: StoredTaskRun) -> str:
        from vibecad.workflow.revert import parse_bound_revert_task

        binding = parse_bound_revert_task(stored.task_run)
        if binding is not None:
            return binding.reservation_key
        return stored.task_run.id

    def _start_orphan_cancellation(
        self,
        stored: StoredTaskRun,
    ) -> StoredTaskRun | None:
        from vibecad.execution.revisions import (
            CandidateReservationPresence,
            CandidateReservationPresenceStatus,
        )

        task = stored.task_run
        reservation_key = self._cancellation_reservation_key(stored)
        lease = None
        try:
            lease = self._lease_manager.acquire_project_write(task.project_id)
            presence = self._revision_store.probe_candidate_reservation(
                task.project_id,
                task.base_revision,
                reservation_key,
            )
            if not (
                type(presence) is CandidateReservationPresence
                and presence.status is CandidateReservationPresenceStatus.EXACT_PRE_CANDIDATE
            ):
                return None
            return self._catalog.start_cancellation(
                task_id=task.id,
                expected_generation=stored.generation,
            )
        except (LeaseError, TaskCatalogError):
            return None
        except Exception:
            return None
        finally:
            if lease is not None:
                try:
                    lease.release(owner_token=lease.owner_token)
                except Exception:
                    pass

    def cancel_task(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun | TaskServicePortFailure:
        self._ensure_live()
        orphan_candidate = False
        fence_proven = False
        with self._generation_lock:
            admitted = self._cad_task_admissions.get(task_id, 0) > 0
            active = admitted
            try:
                stored = self._catalog.get_task(task_id=task_id)
            except TaskCatalogError:
                stored = None
            if (
                not active
                and type(stored) is StoredTaskRun
                and stored.generation == expected_generation
                and not _has_cancellation_request(stored.task_run)
            ):
                from vibecad.execution.revisions import (
                    CandidateReservationPresenceStatus,
                )

                presence = self._cancel_reservation_presence(stored)
                orphan_candidate = (
                    presence is CandidateReservationPresenceStatus.EXACT_PRE_CANDIDATE
                )
                active = presence is not CandidateReservationPresenceStatus.ABSENT
            try:
                requested = self._catalog.cancel_task(
                    task_id=task_id,
                    expected_generation=expected_generation,
                    active=active,
                )
            except TaskCatalogError as error:
                requested = None
                if error.code in {
                    TaskCatalogErrorCode.CONFLICT,
                    TaskCatalogErrorCode.STORE_FAILURE,
                }:
                    requested = self._await_durable_cancellation(
                        task_id,
                        minimum_generation=expected_generation,
                    )
                if requested is None:
                    return self._catalog_failure(error)
            if requested.task_run.status is TaskStatus.CANCEL_REQUESTED:
                if not orphan_candidate and any(
                    record.event is TaskEvent.REQUEST_ACTIVE_CANCEL
                    for record in requested.task_run.transitions
                ):
                    from vibecad.execution.revisions import (
                        CandidateReservationPresenceStatus,
                    )

                    orphan_candidate = (
                        self._cancel_reservation_presence(requested)
                        is CandidateReservationPresenceStatus.EXACT_PRE_CANDIDATE
                    )
                retrying_fence = self._cad_fence_required and self._cad_fence_task_id == task_id
                if admitted or retrying_fence:
                    if not self._fence_cad_generation_locked(task_id=task_id):
                        return TaskServicePortFailure(
                            code=TaskServicePortErrorCode.RECOVERY_REQUIRED
                        )
                    fence_proven = True
        current = requested
        if current.task_run.status is TaskStatus.CANCEL_REQUESTED:
            if fence_proven:
                try:
                    current = self._catalog.start_cancellation(
                        task_id=task_id,
                        expected_generation=current.generation,
                    )
                except TaskCatalogError as error:
                    replayed = self._await_durable_cancellation(
                        task_id,
                        minimum_generation=current.generation,
                    )
                    if replayed is None:
                        return self._catalog_failure(error)
                    current = replayed
            elif orphan_candidate:
                orphan_started = self._start_orphan_cancellation(current)
                if orphan_started is not None:
                    current = orphan_started
        if admitted and not self._wait_for_cad_task_drain(task_id):
            return TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
        if current.task_run.status in {
            TaskStatus.CANCELLED,
            TaskStatus.SUCCEEDED,
        }:
            if admitted:
                return self._reconcile_cancellation_store_only(
                    task_id=task_id,
                    expected_generation=current.generation,
                )
            return current
        if current.task_run.status not in {
            TaskStatus.CANCELLING,
            TaskStatus.RECOVERY_REQUIRED,
            TaskStatus.CLEANUP_REQUIRED,
        }:
            return requested
        return self._reconcile_cancellation_store_only(
            task_id=task_id,
            expected_generation=current.generation,
        )

    def _fence_cad_generation(self) -> bool:
        """Fence one active CAD generation without waiting for the CAD gate."""

        with self._generation_lock:
            return self._fence_cad_generation_locked()

    def _fence_cad_generation_locked(self, *, task_id: str | None = None) -> bool:
        if (
            self._cad_fence_required
            and self._cad_fence_task_id is not None
            and self._cad_fence_task_id != task_id
        ):
            return False
        port = self._cad_execution_port
        if port is None:
            if self._cad_fence_required:
                return False
            self._cad_fence_required = False
            self._cad_fence_task_id = None
            self._generation_epoch += 1
            self._runtimes.clear()
            return True
        terminate = getattr(port, "terminate_generation", None)
        if not callable(terminate):
            return False
        try:
            terminate()
        except Exception:
            self._cad_fence_required = True
            self._cad_fence_task_id = task_id
            self._generation_epoch += 1
            self._runtimes.clear()
            return False
        if self._cad_execution_port is port:
            self._cad_execution_port = None
        self._cad_fence_required = False
        self._cad_fence_task_id = None
        self._generation_epoch += 1
        self._runtimes.clear()
        return True

    def _retire_lost_generation(
        self,
        port: object,
        *,
        task_id: str | None = None,
    ) -> None:
        try:
            lost = getattr(port, "generation_lost", False) is True
        except Exception:
            lost = True
        if not lost:
            return
        with self._generation_lock:
            if self._cad_execution_port is port:
                if self._cad_fence_required:
                    return
                self._fence_cad_generation_locked(task_id=task_id)

    def _reconcile_cancellation_store_only(
        self,
        *,
        task_id: str,
        expected_generation: int,
    ) -> StoredTaskRun | TaskServicePortFailure:
        from vibecad.workflow.service import (
            TaskService,
            TaskServiceError,
        )

        with self._cancellation_reconcile_lock:
            try:
                return TaskService.reconcile_cancellation(
                    task_store=self._task_store,
                    revision_store=self._revision_store,
                    lease_manager=self._lease_manager,
                    task_id=task_id,
                    expected_generation=expected_generation,
                )
            except Exception as error:
                if type(error) is TaskServiceError:
                    cancellation = self._durable_cancellation_for(task_id)
                    if cancellation is not None and cancellation.generation >= expected_generation:
                        return cancellation
                return self._task_service_failure(error)

    def _durable_cancellation_for(self, task_id: str) -> StoredTaskRun | None:
        try:
            stored = self._catalog.get_task(task_id=task_id)
        except TaskCatalogError:
            return None
        if _has_cancellation_request(stored.task_run):
            return stored
        return None

    def _await_durable_cancellation(
        self,
        task_id: str,
        *,
        minimum_generation: int,
    ) -> StoredTaskRun | None:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            cancellation = self._durable_cancellation_for(task_id)
            if cancellation is not None and cancellation.generation >= minimum_generation:
                return cancellation
            time.sleep(0.002)
        return None

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

    def capture_checkout_file(self, *, checkout_id: str) -> CheckoutFileSnapshot:
        self._ensure_live()
        return self._checkouts.capture_live_file(checkout_id)

    def require_same_checkout_file(
        self,
        snapshot: CheckoutFileSnapshot,
    ) -> CheckoutFileSnapshot:
        self._ensure_live()
        return self._checkouts.require_same_live_file(snapshot)

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
        self._ensure_live()
        try:
            stored = self._catalog.get_task(task_id=task_id)
        except TaskCatalogError as error:
            return self._catalog_failure(error)
        if stored.generation == expected_generation and _has_cancellation_request(stored.task_run):
            return self._reconcile_cancellation_store_only(
                task_id=task_id,
                expected_generation=expected_generation,
            )
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
        return self._review_transition(
            task_id=task_id,
            body=lambda: self._cad_method(
                "accept_draft",
                task_id=task_id,
                draft_id=draft_id,
                expected_generation=expected_generation,
            ),
        )

    @staticmethod
    def _task_service_failure(error: Exception) -> TaskServicePortFailure:
        from vibecad.workflow.service import (
            TaskServiceError,
            TaskServiceErrorCode,
        )

        if type(error) is not TaskServiceError:
            raise error
        mapping = {
            TaskServiceErrorCode.INVALID_INPUT: TaskServicePortErrorCode.INVALID_INPUT,
            TaskServiceErrorCode.UNSUPPORTED_REASONING_OWNER: (
                TaskServicePortErrorCode.UNSUPPORTED_REASONING_OWNER
            ),
            TaskServiceErrorCode.INVALID_STATE: TaskServicePortErrorCode.INVALID_STATE,
            TaskServiceErrorCode.NOT_FOUND: TaskServicePortErrorCode.NOT_FOUND,
            TaskServiceErrorCode.CONFLICT: TaskServicePortErrorCode.CONFLICT,
            TaskServiceErrorCode.STORE_FAILURE: TaskServicePortErrorCode.STORE_FAILURE,
            TaskServiceErrorCode.LEASE_UNAVAILABLE: TaskServicePortErrorCode.LEASE_UNAVAILABLE,
            TaskServiceErrorCode.RESOURCE_EXHAUSTED: (TaskServicePortErrorCode.RESOURCE_EXHAUSTED),
            TaskServiceErrorCode.RECOVERY_REQUIRED: TaskServicePortErrorCode.RECOVERY_REQUIRED,
        }
        return TaskServicePortFailure(code=mapping[error.code])

    def _cad_method(self, method: str, **kwargs):
        self._ensure_live()
        task_id = kwargs["task_id"]
        try:
            stored = self._catalog.load_expected(
                task_id,
                kwargs["expected_generation"],
            )
        except TaskCatalogError as error:
            return self._catalog_failure(error)
        if _has_cancellation_request(stored.task_run):
            return TaskServicePortFailure(code=TaskServicePortErrorCode.INVALID_STATE)
        with self._cad_gate:
            self._ensure_live()
            with self._cad_task_admission(task_id) as generation_epoch:
                port = None
                try:
                    try:
                        stored = self._catalog.load_expected(
                            task_id,
                            kwargs["expected_generation"],
                        )
                    except TaskCatalogError as error:
                        return self._catalog_failure(error)
                    if _has_cancellation_request(stored.task_run):
                        return TaskServicePortFailure(code=TaskServicePortErrorCode.INVALID_STATE)
                    try:
                        runtime = self._runtime_for(
                            stored.task_run.project_id,
                            expected_generation_epoch=generation_epoch,
                        )
                    except Exception as error:
                        from vibecad.execution.errors import ExecutorError

                        if type(error) is ExecutorError:
                            cancellation = self._await_durable_cancellation(
                                task_id,
                                minimum_generation=kwargs["expected_generation"],
                            )
                            if cancellation is not None:
                                return cancellation
                            return TaskServicePortFailure(
                                code=TaskServicePortErrorCode.RECOVERY_REQUIRED
                            )
                        raise
                    port = getattr(self, "_cad_execution_port", None)
                    if type(runtime) is TaskServicePortFailure:
                        cancellation = self._await_durable_cancellation(
                            task_id,
                            minimum_generation=kwargs["expected_generation"],
                        )
                        return cancellation if cancellation is not None else runtime
                    try:
                        result = getattr(runtime.service, method)(**kwargs)
                    except Exception as error:
                        result = self._task_service_failure(error)
                    if type(result) is TaskServicePortFailure:
                        cancellation = self._await_durable_cancellation(
                            task_id,
                            minimum_generation=kwargs["expected_generation"],
                        )
                        if cancellation is not None:
                            result = cancellation
                    if bool(getattr(runtime, "stale", False)):
                        try:
                            closed = runtime.close()
                        except Exception:
                            closed = False
                        if closed is True:
                            with self._generation_lock:
                                if self._runtimes.get(stored.task_run.project_id) is runtime:
                                    del self._runtimes[stored.task_run.project_id]
                    return result
                finally:
                    if port is None:
                        port = getattr(self, "_cad_execution_port", None)
                    if port is not None:
                        self._retire_lost_generation(port, task_id=task_id)

    def _runtime_for(
        self,
        project_id: str,
        *,
        expected_generation_epoch: int | None = None,
    ):
        try:
            lease = self._lease_manager.acquire_project_write(project_id)
        except LeaseError:
            return TaskServicePortFailure(code=TaskServicePortErrorCode.LEASE_UNAVAILABLE)
        result = None
        created = None
        try:
            head = self._revision_store.load_head(project_id)
            with self._generation_lock:
                if (
                    expected_generation_epoch is not None
                    and self._generation_epoch != expected_generation_epoch
                ):
                    result = TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
                cached = self._runtimes.get(project_id)
            if result is None and cached is not None and cached.head == head:
                with self._generation_lock:
                    if self._runtimes.get(project_id) is cached:
                        self._runtimes.move_to_end(project_id)
                        result = cached
            elif result is None and cached is not None:
                if not _close_runtime(cached):
                    result = TaskServicePortFailure(
                        code=TaskServicePortErrorCode.RESOURCE_EXHAUSTED
                    )
                else:
                    with self._generation_lock:
                        if self._runtimes.get(project_id) is cached:
                            del self._runtimes[project_id]
            while result is None:
                with self._generation_lock:
                    if len(self._runtimes) < MAX_PROJECT_RUNTIMES:
                        break
                    runtime_snapshot = tuple(self._runtimes.items())
                evicted = False
                for key, candidate in runtime_snapshot:
                    if not _close_runtime(candidate):
                        continue
                    with self._generation_lock:
                        if self._runtimes.get(key) is candidate:
                            del self._runtimes[key]
                            evicted = True
                            break
                        if len(self._runtimes) < MAX_PROJECT_RUNTIMES:
                            evicted = True
                            break
                if not evicted:
                    result = TaskServicePortFailure(
                        code=TaskServicePortErrorCode.RESOURCE_EXHAUSTED
                    )
            if result is None:
                cad_port = self._cad_execution_port_under_gate(
                    expected_generation_epoch=expected_generation_epoch,
                )
                if cad_port is None and getattr(self, "_cad_port_factory", None) is not None:
                    result = TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
            if result is None:
                created = self._runtime_factory(
                    project_id=project_id,
                    head=head,
                    task_store=self._task_store,
                    revision_store=self._revision_store,
                    lease_manager=self._lease_manager,
                    cad_port=cad_port,
                )
                with self._generation_lock:
                    generation_is_current = (
                        (
                            expected_generation_epoch is None
                            or self._generation_epoch == expected_generation_epoch
                        )
                        and self._cad_execution_port is cad_port
                        and getattr(cad_port, "generation_lost", False) is not True
                    )
                    if generation_is_current:
                        self._runtimes[project_id] = created
                        result = created
                if result is None:
                    _close_runtime(created)
                    result = TaskServicePortFailure(code=TaskServicePortErrorCode.RECOVERY_REQUIRED)
        finally:
            release_failed = False
            try:
                lease.release(owner_token=lease.owner_token)
            except Exception:
                release_failed = True
        if release_failed:
            with self._generation_lock:
                created_is_current = (
                    created is not None and self._runtimes.get(project_id) is created
                )
            if created_is_current:
                try:
                    closed = _close_runtime(created)
                except Exception:  # pragma: no cover - defensive against corruption
                    closed = False
                if closed:
                    with self._generation_lock:
                        if self._runtimes.get(project_id) is created:
                            del self._runtimes[project_id]
            return TaskServicePortFailure(code=TaskServicePortErrorCode.LEASE_UNAVAILABLE)
        return result

    def close(self) -> None:
        if os.getpid() != self._creator_pid:
            raise RuntimeError("AgentApplication belongs to another process")
        with self._close_lock:
            if self._closed:
                return
            with self._component_lock:
                close_error = None
                with self._cad_gate:
                    self._closed = True
                    with self._generation_lock:
                        runtime_snapshot = tuple(self._runtimes.items())
                    for project_id, runtime in runtime_snapshot:
                        if _close_runtime(runtime):
                            with self._generation_lock:
                                if self._runtimes.get(project_id) is runtime:
                                    del self._runtimes[project_id]
                    with self._generation_lock:
                        port = getattr(self, "_cad_execution_port", None)
                        self._cad_execution_port = None
                        self._generation_epoch += 1
                    close_generation = getattr(port, "close_generation", None)
                    if callable(close_generation):
                        try:
                            close_generation()
                        except Exception as error:
                            close_error = error
                store = self._artifact_store
                if store is not None:
                    try:
                        store.close()
                    except Exception as error:
                        if close_error is None:
                            close_error = error
                if close_error is not None:
                    raise close_error
