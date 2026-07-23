"""Authenticated, single-instance local Task Kernel."""

from vibecad.daemon.client import LocalKernelClient
from vibecad.daemon.facade import ALLOWED_APPLICATION_OPERATIONS, LocalKernelFacade
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
    "DAEMON_DIRECTORY_NAME",
    "DAEMON_ENDPOINT_NAME",
    "DAEMON_RECEIPT_NAME",
    "DAEMON_SECRET_NAME",
    "DaemonEndpointBinding",
    "DaemonError",
    "DaemonErrorCode",
    "DaemonFileBinding",
    "DaemonReceipt",
    "LocalKernelClient",
    "LocalKernelDaemon",
    "LocalKernelFacade",
    "LocalKernelState",
    "daemon_run_root",
    "run_daemon",
)
