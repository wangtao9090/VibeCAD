"""Authenticated, single-instance local Task Kernel."""

from vibecad.daemon.adapters import (
    LocalAgentClient,
    LocalAgentClientError,
    LocalAgentClientErrorCode,
)
from vibecad.daemon.bootstrap import (
    DAEMON_BOOTSTRAP_POLL_SECONDS,
    DAEMON_BOOTSTRAP_TIMEOUT_SECONDS,
    DAEMON_RETIRE_TIMEOUT_SECONDS,
    connect_or_start_local_kernel,
    retire_local_kernel,
)
from vibecad.daemon.client import LocalKernelClient
from vibecad.daemon.facade import (
    ALLOWED_APPLICATION_OPERATIONS,
    KERNEL_API_EPOCH,
    KERNEL_API_NAME,
    KERNEL_BUILD_ID,
    LocalKernelFacade,
)
from vibecad.daemon.service import LocalKernelDaemon, LocalKernelState, run_daemon
from vibecad.daemon.state import (
    DAEMON_AUTHORITY,
    DAEMON_DIRECTORY_NAME,
    DAEMON_ENDPOINT_NAME,
    DAEMON_RECEIPT_NAME,
    DAEMON_SECRET_NAME,
    DaemonEndpointBinding,
    DaemonError,
    DaemonErrorCode,
    DaemonFileBinding,
    DaemonReceipt,
    daemon_run_root,
)

__all__ = (
    "ALLOWED_APPLICATION_OPERATIONS",
    "DAEMON_AUTHORITY",
    "DAEMON_BOOTSTRAP_POLL_SECONDS",
    "DAEMON_BOOTSTRAP_TIMEOUT_SECONDS",
    "DAEMON_DIRECTORY_NAME",
    "DAEMON_ENDPOINT_NAME",
    "DAEMON_RECEIPT_NAME",
    "DAEMON_RETIRE_TIMEOUT_SECONDS",
    "DAEMON_SECRET_NAME",
    "DaemonEndpointBinding",
    "DaemonError",
    "DaemonErrorCode",
    "DaemonFileBinding",
    "DaemonReceipt",
    "LocalAgentClient",
    "LocalAgentClientError",
    "LocalAgentClientErrorCode",
    "KERNEL_API_EPOCH",
    "KERNEL_API_NAME",
    "KERNEL_BUILD_ID",
    "LocalKernelClient",
    "LocalKernelDaemon",
    "LocalKernelFacade",
    "LocalKernelState",
    "daemon_run_root",
    "connect_or_start_local_kernel",
    "retire_local_kernel",
    "run_daemon",
)
