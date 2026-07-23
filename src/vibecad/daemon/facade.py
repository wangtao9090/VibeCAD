"""Closed, code-installed facade for the local Task Kernel."""

from __future__ import annotations

from vibecad.interaction.checkouts import (
    CheckoutError,
    CheckoutErrorCode,
    DraftCheckoutSource,
    HeadCheckoutSource,
)
from vibecad.interaction.protocol_v2 import (
    V2_VERSION,
    StaticV2Dispatcher,
    V2ErrorCode,
    V2ProtocolError,
)

ALLOWED_APPLICATION_OPERATIONS = frozenset(
    {
        "accept_draft",
        "cancel_task",
        "compare_revisions",
        "create_box",
        "create_cylinder",
        "create_project",
        "create_task",
        "export_task_artifacts",
        "get_artifact_manifest",
        "get_capabilities",
        "get_project",
        "get_task",
        "get_task_events",
        "inspect_model",
        "list_projects",
        "list_revisions",
        "list_tasks",
        "modify_parameter",
        "move_part",
        "reject_draft",
        "resume_task",
        "revert_project",
        "rotate_part",
        "submit_model_program",
    }
)

_DIRECT_OPERATIONS = frozenset(
    {
        "create_box",
        "create_cylinder",
        "inspect_model",
        "modify_parameter",
        "move_part",
        "rotate_part",
    }
)


def _checkout_failure(error: CheckoutError) -> V2ProtocolError:
    if error.code is CheckoutErrorCode.INVALID_INPUT:
        return V2ProtocolError(V2ErrorCode.INVALID_REQUEST)
    if error.code is CheckoutErrorCode.RESOURCE_EXHAUSTED:
        return V2ProtocolError(V2ErrorCode.RESOURCE_EXHAUSTED)
    return V2ProtocolError(V2ErrorCode.UNAVAILABLE)


class LocalKernelFacade:
    """Literal application routing; wire strings never select Python attributes."""

    __slots__ = ("_application", "_daemon_id", "_dispatcher")

    def __init__(self, application: object, *, daemon_id: str) -> None:
        if application is None or type(daemon_id) is not str:
            raise TypeError("invalid local kernel facade")
        self._application = application
        self._daemon_id = daemon_id
        self._dispatcher = StaticV2Dispatcher(
            kernel_ping=self._kernel_ping,
            application_call=self._application_call,
            checkout_open=self._checkout_open,
            checkout_get=self._checkout_get,
            checkout_close=self._checkout_close,
            allowed_application_operations=ALLOWED_APPLICATION_OPERATIONS,
        )

    @property
    def dispatcher(self) -> StaticV2Dispatcher:
        return self._dispatcher

    def _kernel_ping(self, _params: dict[str, object]) -> dict[str, object]:
        return {
            "schema_version": 1,
            "daemon_id": self._daemon_id,
            "status": "ready",
            "protocol": {"major": V2_VERSION[0], "minor": V2_VERSION[1]},
        }

    def _application_call(self, params: dict[str, object]) -> dict[str, object]:
        operation = params["operation"]
        request = params["request"]
        if operation == "create_project":
            if request.get("kind") == "import_fcstd" or request.get("source_path") is not None:
                raise V2ProtocolError(V2ErrorCode.UNAVAILABLE)
            return self._application.create_project_request(request)
        if operation == "get_project":
            return self._application.get_project_request(request)
        if operation == "list_projects":
            return self._application.list_projects_request(request)
        if operation == "list_revisions":
            return self._application.list_revisions_request(request)
        if operation == "compare_revisions":
            return self._application.compare_revisions_request(request)
        if operation == "revert_project":
            return self._application.revert_project_request(request)
        if operation == "create_task":
            return self._application.create_task_request(request)
        if operation == "list_tasks":
            return self._application.list_tasks_request(request)
        if operation == "get_task":
            return self._application.get_task_request(request)
        if operation == "get_task_events":
            return self._application.get_task_events_request(request)
        if operation == "submit_model_program":
            return self._application.submit_model_program_request(request)
        if operation == "resume_task":
            return self._application.resume_task_request(request)
        if operation == "cancel_task":
            return self._application.cancel_task_request(request)
        if operation == "accept_draft":
            return self._application.accept_draft_request(request)
        if operation == "reject_draft":
            return self._application.reject_draft_request(request)
        if operation == "get_artifact_manifest":
            return self._application.get_artifact_manifest_request(request)
        if operation == "export_task_artifacts":
            return self._application.export_task_artifacts_request(request)
        if operation == "get_capabilities":
            return self._application.get_capabilities_request(request)
        if operation in _DIRECT_OPERATIONS:
            return self._application.invoke_direct_operation_request(operation, request)
        raise V2ProtocolError(V2ErrorCode.UNKNOWN_METHOD)

    @staticmethod
    def _source(value: dict[str, object]) -> HeadCheckoutSource | DraftCheckoutSource:
        try:
            if value["kind"] == "head":
                return HeadCheckoutSource(project_id=value["project_id"])
            return DraftCheckoutSource(
                task_id=value["task_id"],
                draft_id=value["draft_id"],
                expected_generation=value["expected_generation"],
            )
        except (CheckoutError, KeyError, TypeError, ValueError):
            raise V2ProtocolError(V2ErrorCode.INVALID_REQUEST) from None

    @staticmethod
    def _descriptor(value: object) -> dict[str, object]:
        try:
            result = value.to_local_mapping()
        except (AttributeError, CheckoutError, TypeError, ValueError):
            raise V2ProtocolError(V2ErrorCode.INTERNAL_ERROR) from None
        if type(result) is not dict:
            raise V2ProtocolError(V2ErrorCode.INTERNAL_ERROR)
        return result

    def _checkout_open(self, params: dict[str, object]) -> dict[str, object]:
        try:
            descriptor = self._application.open_checkout(
                open_key=params["open_key"],
                source=self._source(params["source"]),
            )
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        return self._descriptor(descriptor)

    def _checkout_get(self, params: dict[str, object]) -> dict[str, object]:
        try:
            descriptor = self._application.get_checkout(
                checkout_id=params["checkout_id"],
            )
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        return self._descriptor(descriptor)

    def _checkout_close(self, params: dict[str, object]) -> dict[str, object]:
        try:
            descriptor = self._application.close_checkout(
                checkout_id=params["checkout_id"],
            )
        except CheckoutError as error:
            raise _checkout_failure(error) from None
        return self._descriptor(descriptor)


__all__ = ("ALLOWED_APPLICATION_OPERATIONS", "LocalKernelFacade")
