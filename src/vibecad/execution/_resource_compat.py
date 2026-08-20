"""Portable, fail-closed access to POSIX file-size resource limits."""

from __future__ import annotations

try:
    import resource as _resource
except ModuleNotFoundError:  # Windows has no stdlib resource module.
    _resource = None


if _resource is None:
    RLIMIT_FSIZE = 0
    RLIM_INFINITY = -1
else:
    RLIMIT_FSIZE = _resource.RLIMIT_FSIZE
    RLIM_INFINITY = _resource.RLIM_INFINITY


def getrlimit(resource_number: int) -> tuple[int, int]:
    """Return a native resource limit or fail closed when unavailable."""

    if _resource is None:
        raise OSError("file-size resource limits are unavailable on this platform")
    return _resource.getrlimit(resource_number)


def setrlimit(resource_number: int, limits: tuple[int, int]) -> None:
    """Set a native resource limit or fail closed when unavailable."""

    if _resource is None:
        raise OSError("file-size resource limits are unavailable on this platform")
    _resource.setrlimit(resource_number, limits)
