"""Portable, fail-closed access to the POSIX file-size signal policy."""

from __future__ import annotations

import signal as _signal

SIG_IGN = _signal.SIG_IGN
SIGXFSZ = getattr(_signal, "SIGXFSZ", None)


def getsignal(signal_number: int | None):
    """Return a native signal handler or fail closed when SIGXFSZ is absent."""

    if signal_number is None:
        raise OSError("file-size signals are unavailable on this platform")
    return _signal.getsignal(signal_number)


def signal(signal_number: int | None, handler):
    """Install a native signal handler or fail closed when SIGXFSZ is absent."""

    if signal_number is None:
        raise OSError("file-size signals are unavailable on this platform")
    return _signal.signal(signal_number, handler)
