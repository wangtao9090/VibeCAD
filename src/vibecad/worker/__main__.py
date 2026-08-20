"""Managed-runtime entry point for the private FreeCAD Worker."""

from __future__ import annotations

import os
import re
import socket
import sys

from vibecad.worker.service import serve_worker


def _arguments(argv: list[str]) -> tuple[int, str]:
    if len(argv) != 5 or argv[1] != "--protocol-fd" or argv[3] != "--generation-id":
        raise ValueError("invalid Worker arguments")
    try:
        descriptor = int(argv[2], 10)
    except (TypeError, ValueError):
        raise ValueError("invalid Worker descriptor") from None
    generation = argv[4]
    valid_process_boundary = descriptor >= 3
    if sys.platform != "win32":
        valid_process_boundary = (
            valid_process_boundary
            and os.getpgrp() == os.getpid()
            and os.getsid(0) == os.getpid()
        )
    if not valid_process_boundary or re.fullmatch(
        r"worker_generation_[0-9a-f]{32}", generation
    ) is None:
        raise ValueError("invalid Worker generation")
    return descriptor, generation


def main() -> int:
    try:
        descriptor, generation = _arguments(sys.argv)
        if sys.platform == "win32":
            connection = socket.socket(fileno=descriptor)
            connection.set_inheritable(False)
        else:
            os.set_inheritable(descriptor, False)
            connection = socket.socket(fileno=descriptor)
    except (OSError, ValueError):
        return 2
    try:
        return serve_worker(connection, generation)
    except BaseException:
        return 3
    finally:
        try:
            connection.close()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
