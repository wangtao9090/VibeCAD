"""Real FreeCAD embedded-interpreter probe for the local Task Kernel."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import stat
import sys
from pathlib import Path

_RESULT_PREFIX = "VIBECAD_BOOTSTRAP_PROBE="


def _resolved(value: object) -> str:
    return str(Path(os.fspath(value)).resolve())


def _matches_path(value: object, expected: Path) -> bool:
    try:
        return Path(os.fspath(value)).resolve() == expected
    except (OSError, TypeError, ValueError):
        return False


def _run() -> tuple[dict[str, object], int]:
    probe_source = Path(__file__).resolve()
    expected_repo = probe_source.parents[3]
    expected_probe = (
        expected_repo / "tests" / "fixtures" / "freecad_workbench" / "bootstrap_probe.py"
    ).resolve()
    repo_source = (expected_repo / "src").resolve()
    expected_vibecad = (repo_source / "vibecad" / "__init__.py").resolve()
    expected_bootstrap = (repo_source / "vibecad" / "daemon" / "bootstrap.py").resolve()
    preloaded = sorted(
        name for name in sys.modules if name == "vibecad" or name.startswith("vibecad.")
    )
    result: dict[str, object] = {
        "child_pid": os.getpid(),
        "probe_source": str(probe_source),
        "repo_source": str(repo_source),
        "sys_executable": _resolved(sys.executable),
        "sys_prefix": _resolved(sys.prefix),
        "vibecad_preloaded": preloaded,
    }
    client = None
    run_root = None
    try:
        if probe_source != expected_probe:
            raise RuntimeError("bootstrap probe source identity mismatch")
        for label, source in (
            ("vibecad", expected_vibecad),
            ("daemon bootstrap", expected_bootstrap),
        ):
            source_info = source.lstat()
            if not stat.S_ISREG(source_info.st_mode):
                raise RuntimeError(f"{label} repository source is not regular")
        if preloaded:
            raise RuntimeError("vibecad was preloaded before probe")

        result["repo_source_occurrences_before"] = sum(
            _matches_path(entry, repo_source) for entry in sys.path
        )
        sys.path[:] = [entry for entry in sys.path if not _matches_path(entry, repo_source)]
        sys.path.insert(0, str(repo_source))
        importlib.invalidate_caches()
        result.update(
            {
                "repo_source_occurrences_after": sum(
                    _matches_path(entry, repo_source) for entry in sys.path
                ),
                "sys_path_zero": _resolved(sys.path[0]),
            }
        )

        import vibecad
        from vibecad.daemon import bootstrap
        from vibecad.daemon.adapters import LocalAgentClient
        from vibecad.daemon.state import (
            DAEMON_ENDPOINT_NAME,
            daemon_run_root,
            read_boot_state,
        )
        from vibecad.runtime import paths

        vibecad_source = _resolved(vibecad.__file__)
        bootstrap_source = _resolved(inspect.getsourcefile(bootstrap))
        result.update(
            {
                "bootstrap_source": bootstrap_source,
                "vibecad_source": vibecad_source,
            }
        )
        if vibecad_source != str(expected_vibecad):
            raise RuntimeError("vibecad source identity mismatch")
        if bootstrap_source != str(expected_bootstrap):
            raise RuntimeError("daemon bootstrap source identity mismatch")

        run_root = daemon_run_root(paths.data_root())
        result.update(
            {
                "cold_run_root_absent": not os.path.lexists(run_root),
                "run_root": str(run_root),
                "socket": str(run_root / DAEMON_ENDPOINT_NAME),
            }
        )
        if not result["cold_run_root_absent"]:
            raise RuntimeError("isolated daemon run root was not cold")

        client = LocalAgentClient.open()
        daemon_id = client.daemon_id
        ping = client.ping()
        state = read_boot_state(run_root)
        if state.receipt.daemon_id != daemon_id:
            raise RuntimeError("published daemon identity mismatch")
        if ping.get("daemon_id") != daemon_id:
            raise RuntimeError("ping daemon identity mismatch")
        result.update(
            {
                "daemon_id": daemon_id,
                "daemon_pid": state.receipt.pid,
                "run_root": str(run_root),
                "run_root_identity": [
                    state.receipt.run_root_dev,
                    state.receipt.run_root_ino,
                ],
                "status": "ok",
            }
        )
        return result, 0
    except BaseException as error:
        if run_root is not None:
            try:
                state = read_boot_state(run_root)
                result.update(
                    {
                        "daemon_id": state.receipt.daemon_id,
                        "daemon_pid": state.receipt.pid,
                        "run_root_identity": [
                            state.receipt.run_root_dev,
                            state.receipt.run_root_ino,
                        ],
                    }
                )
            except BaseException:
                pass
        result.update(
            {
                "error": f"{type(error).__name__}: {error}",
                "status": "error",
            }
        )
        return result, 1
    finally:
        if client is not None:
            client.close()
            result["client_closed"] = True


_result, _exit_code = _run()
print(_RESULT_PREFIX + json.dumps(_result, allow_nan=False, sort_keys=True), flush=True)
raise SystemExit(_exit_code)
