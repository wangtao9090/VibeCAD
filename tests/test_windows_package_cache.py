from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from vibecad.runtime import paths
from vibecad.runtime import windows_package_cache as cache

pytestmark = pytest.mark.windows_contract


class FakeCacheBackend:
    def __init__(self, parents: tuple[Path, ...]) -> None:
        self.parents = parents
        self.invalid: set[Path] = set()
        self.collisions: set[Path] = set()
        self.created: list[Path] = []

    def candidate_parents(self) -> tuple[Path, ...]:
        return self.parents

    def validate_parent(self, parent: Path) -> None:
        if parent in self.invalid:
            raise cache.PackageCacheError("invalid parent")
        cache._validate_real_directory(parent)

    def create_private_directory(self, path: Path) -> bool:
        if path in self.collisions or os.path.lexists(path):
            return False
        path.mkdir()
        self.created.append(path)
        return True

    def security_digest(self, path: Path) -> str:
        cache._validate_real_directory(path)
        return "a" * 64


def _stale_receipt(
    backend: FakeCacheBackend,
    parent: Path,
    record: Path,
) -> tuple[Path, tuple[int, int], dict[str, object]]:
    root, identity = cache._acquire_root(
        backend,
        len(str(parent / "a")),
        ("a",),
    )
    token = "b" * 64
    cache._write_token_marker(root, token)
    payload = {
        "schema": 1,
        "state": "active",
        "root": str(root),
        "device": identity[0],
        "inode": identity[1],
        "security_sha256": backend.security_digest(root),
        "token": token,
        "helper_pid": os.getpid(),
        "helper_created": 1,
    }
    cache._write_receipt(record, payload)
    return root, identity, payload


def test_recovery_record_is_outside_replaceable_runtime(monkeypatch, tmp_path) -> None:
    home = tmp_path / "VibeCAD"
    monkeypatch.setenv("VIBECAD_HOME", str(home))

    assert paths.package_cache_record() == home / ".package-cache-session.json"
    assert not paths.package_cache_record().is_relative_to(paths.runtime_root())
    assert not paths.package_cache_record().is_relative_to(paths.data_root())


def test_reviewed_flat_cache_path_budget_keeps_a_twenty_character_margin() -> None:
    longest = (
        cache._DEFAULT_MAXIMUM_ROOT_LENGTH
        + 1
        + cache._REVIEWED_MAXIMUM_RELATIVE_PATH
    )
    assert longest == 239
    assert cache._LEGACY_MAXIMUM_VISIBLE_PATH - longest == 20


def test_session_uses_root_as_package_cache_and_isolates_environment(tmp_path) -> None:
    parent = tmp_path / "u"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    base = {
        "Path": "system-path",
        "conda_prefix": "foreign-prefix",
        "MAMBA_ROOT_PREFIX": "foreign-root",
        "CondaRc": "foreign-rc",
        "PYTHONPATH": "foreign-python",
        "XDG_CACHE_HOME": "foreign-xdg",
        "HTTP_PROXY": "http://proxy.invalid",
    }

    with cache.package_cache_session(
        _backend=backend,
        _names=("a",),
        maximum_root_length=len(str(parent / "a")),
    ) as session:
        root = session.root
        assert session.packages == root
        assert session.temporary == root / "tmp"
        assert session.pip == root / "pip"
        assert session.xdg == root / "xdg"
        environment = session.child_environment(base)
        assert environment["CONDA_PKGS_DIRS"] == str(root)
        assert environment["TEMP"] == environment["TMP"] == environment["TMPDIR"]
        assert environment["PIP_CACHE_DIR"] == str(root / "pip")
        assert environment["XDG_CACHE_HOME"] == str(root / "xdg")
        assert environment["Path"] == "system-path"
        assert environment["HTTP_PROXY"] == "http://proxy.invalid"
        assert "CONDA_PREFIX" not in {name.upper() for name in environment}
        assert "MAMBA_ROOT_PREFIX" not in {name.upper() for name in environment}
        assert "CONDARC" not in {name.upper() for name in environment}
        assert "PYTHONPATH" not in {name.upper() for name in environment}
        assert base["conda_prefix"] == "foreign-prefix"

    assert not root.exists()


def test_shortest_valid_parent_is_selected_and_path_budget_is_enforced(tmp_path) -> None:
    too_long = tmp_path / ("x" * 30)
    short = tmp_path / "s"
    too_long.mkdir()
    short.mkdir()
    backend = FakeCacheBackend((too_long, short))
    budget = len(str(short / "z"))

    with cache.package_cache_session(
        _backend=backend,
        _names=("z",),
        maximum_root_length=budget,
    ) as session:
        assert session.root == short / "z"
        assert len(str(session.root)) <= budget


def test_existing_unknown_candidate_is_skipped_and_untouched(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    unknown = parent / "a"
    unknown.mkdir()
    marker = unknown / "keep.bin"
    marker.write_bytes(b"keep")
    backend = FakeCacheBackend((parent,))

    with cache.package_cache_session(
        _backend=backend,
        _names=("a", "b"),
        maximum_root_length=len(str(parent / "b")),
    ) as session:
        assert session.root == parent / "b"

    assert marker.read_bytes() == b"keep"
    assert unknown.is_dir()


def test_body_failure_still_removes_exact_cache(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))

    with pytest.raises(LookupError, match="body failed"):
        with cache.package_cache_session(
            _backend=backend,
            _names=("a",),
            maximum_root_length=len(str(parent / "a")),
        ) as session:
            root = session.root
            (root / "payload.bin").write_bytes(b"payload")
            raise LookupError("body failed")

    assert not root.exists()


def test_replacement_generation_is_not_removed(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    detached = parent / "detached"

    with pytest.raises(cache.PackageCacheError, match="generation identity changed"):
        with cache.package_cache_session(
            _backend=backend,
            _names=("a",),
            maximum_root_length=len(str(parent / "a")),
        ) as session:
            root = session.root
            root.rename(detached)
            root.mkdir()
            marker = root / "foreign.bin"
            marker.write_bytes(b"foreign")

    assert marker.read_bytes() == b"foreign"
    assert detached.is_dir()


def test_alias_parent_is_rejected_without_creation(monkeypatch, tmp_path) -> None:
    parent = tmp_path / "alias-parent"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    backend.invalid.add(parent)

    with pytest.raises(cache.PackageCacheError, match="no safe short physical"):
        with cache.package_cache_session(
            _backend=backend,
            _names=("a",),
            maximum_root_length=len(str(parent / "a")),
        ):
            pytest.fail("an invalid parent must not be used")

    assert backend.created == []


def test_no_candidate_within_budget_fails_without_writes(tmp_path) -> None:
    parent = tmp_path / "long-parent"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))

    with pytest.raises(cache.PackageCacheError, match="no safe short physical"):
        with cache.package_cache_session(
            _backend=backend,
            _names=("a",),
            maximum_root_length=8,
        ):
            pytest.fail("an over-budget cache must not be created")

    assert backend.created == []


@pytest.mark.skipif(sys.platform != "win32", reason="native cache contract is Windows-only")
def test_native_windows_cache_is_short_private_and_transient() -> None:
    with cache.package_cache_session() as session:
        root = session.root
        assert len(str(root)) <= cache._DEFAULT_MAXIMUM_ROOT_LENGTH
        assert root.is_dir()
        assert session.temporary.is_dir()
        assert session.pip.is_dir()
        assert session.xdg.is_dir()
        session.validate()

    assert not root.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native cache contract is Windows-only")
def test_native_non_elevated_windows_temp_candidate_can_be_created_and_removed(
    monkeypatch,
) -> None:
    backend = cache._WindowsCacheBackend()
    windows_temp = backend._windows / "Temp"
    monkeypatch.setattr(backend, "candidate_parents", lambda: (windows_temp,))

    with cache.package_cache_session(_backend=backend) as session:
        root = session.root
        assert root.parent == windows_temp
        assert len(str(root)) <= cache._DEFAULT_MAXIMUM_ROOT_LENGTH
        session.validate()

    assert not root.exists()


def test_dead_helper_receipt_recovers_exact_cache(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    root, _identity, _payload = _stale_receipt(backend, parent, record)

    assert cache.recover_stale_package_cache(
        record_path=record,
        maximum_root_length=len(str(root)),
        _backend=backend,
        _process_matches_fn=lambda *_args: False,
    )

    assert not root.exists()
    assert not record.exists()


def test_missing_exact_root_clears_only_matching_receipt(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    root, identity, _payload = _stale_receipt(backend, parent, record)
    cache._remove_owned_root(root, identity)

    assert cache.recover_stale_package_cache(
        record_path=record,
        maximum_root_length=len(str(root)),
        _backend=backend,
        _process_matches_fn=lambda *_args: False,
    )
    assert not record.exists()


def test_live_helper_receipt_is_not_deleted(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    root, _identity, _payload = _stale_receipt(backend, parent, record)

    with pytest.raises(cache.PackageCacheError, match="still active"):
        cache.recover_stale_package_cache(
            record_path=record,
            maximum_root_length=len(str(root)),
            _backend=backend,
            _process_matches_fn=lambda *_args: True,
        )

    assert root.is_dir()
    assert record.is_file()

    with pytest.raises(cache.PackageCacheError, match="liveness is indeterminate"):
        cache.recover_stale_package_cache(
            record_path=record,
            maximum_root_length=len(str(root)),
            _backend=backend,
            _process_matches_fn=lambda *_args: None,
        )

    assert root.is_dir()
    assert record.is_file()


def test_unknown_helper_liveness_fails_closed(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    root, _identity, _payload = _stale_receipt(backend, parent, record)

    def unknown(*_args):
        raise cache.PackageCacheError("process query unavailable")

    with pytest.raises(cache.PackageCacheError, match="process query unavailable"):
        cache.recover_stale_package_cache(
            record_path=record,
            maximum_root_length=len(str(root)),
            _backend=backend,
            _process_matches_fn=unknown,
        )

    assert root.is_dir()
    assert record.is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 liveness contract")
@pytest.mark.parametrize("failure", ["open", "times"])
def test_native_process_query_errors_are_not_reported_as_death(
    monkeypatch,
    failure,
) -> None:
    class Function:
        def __init__(self, call):
            self.call = call
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.call(*args)

    class Kernel:
        def __init__(self):
            def open_process(*_args):
                if failure == "open":
                    cache.ctypes.set_last_error(cache._ERROR_ACCESS_DENIED)
                    return 0
                return 1

            def get_times(*_args):
                cache.ctypes.set_last_error(cache._ERROR_ACCESS_DENIED)
                return 0

            self.OpenProcess = Function(open_process)
            self.CloseHandle = Function(lambda *_args: 1)
            self.GetProcessTimes = Function(get_times)

    monkeypatch.setattr(cache.ctypes, "WinDLL", lambda *_args, **_kwargs: Kernel())

    with pytest.raises(cache.PackageCacheError, match="query failed|times are unavailable"):
        cache._process_creation_time(1234)


def test_cleaning_receipt_recovers_after_partial_token_deletion(
    monkeypatch,
    tmp_path,
) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    root, _identity, _payload = _stale_receipt(backend, parent, record)
    (root / "writer.bin").write_bytes(b"partial")
    original_remove = cache._remove_owned_root
    attempts = 0

    def interrupt_once(path, identity):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            assert cache._read_receipt(record)["state"] == "cleaning"
            (path / cache._TOKEN_MARKER).unlink()
            raise cache._CleanupRetryable("simulated sharing violation")
        original_remove(path, identity)

    monkeypatch.setattr(cache, "_remove_owned_root", interrupt_once)

    assert cache.recover_stale_package_cache(
        record_path=record,
        maximum_root_length=len(str(root)),
        _backend=backend,
        _process_matches_fn=lambda *_args: False,
    )

    assert attempts == 2
    assert not root.exists()
    assert not record.exists()


def test_recovery_converges_transition_file_left_by_hard_exit(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    root, _identity, payload = _stale_receipt(backend, parent, record)
    transition = record.with_name(
        f"{record.name}.{payload['token']}.transition.tmp"
    )
    script = """
import os
import sys
from pathlib import Path

from vibecad.runtime import windows_package_cache as cache

record = Path(sys.argv[1])
active = cache._read_receipt(record)
cleaning = dict(active)
cleaning["state"] = "cleaning"
cache.os.replace = lambda *_args: os._exit(79)
cache._replace_matching_receipt(record, active, cleaning)
"""

    crashed = subprocess.run(
        [sys.executable, "-c", script, str(record)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert crashed.returncode == 79, crashed.stderr
    assert transition.is_file()
    assert cache._read_receipt(record)["state"] == "active"
    assert cache.recover_stale_package_cache(
        record_path=record,
        maximum_root_length=len(str(root)),
        _backend=backend,
        _process_matches_fn=lambda *_args: False,
    )
    assert not transition.exists()
    assert not root.exists()
    assert not record.exists()


def test_recovery_never_deletes_replacement_generation(tmp_path) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    root, _identity, _payload = _stale_receipt(backend, parent, record)
    detached = parent / "detached"
    root.rename(detached)
    root.mkdir()
    foreign = root / "foreign.bin"
    foreign.write_bytes(b"foreign")

    with pytest.raises(cache.PackageCacheError, match="identity changed"):
        cache.recover_stale_package_cache(
            record_path=record,
            maximum_root_length=len(str(root)),
            _backend=backend,
            _process_matches_fn=lambda *_args: False,
        )

    assert foreign.read_bytes() == b"foreign"
    assert detached.is_dir()
    assert record.is_file()


def test_malformed_recovery_record_fails_closed(tmp_path) -> None:
    record = tmp_path / "record.json"
    record.write_text("{}", encoding="utf-8")
    backend = FakeCacheBackend((tmp_path,))

    with pytest.raises(cache.PackageCacheError, match="invalid shape"):
        cache.recover_stale_package_cache(
            record_path=record,
            _backend=backend,
            _process_matches_fn=lambda *_args: False,
        )

    assert record.read_text(encoding="utf-8") == "{}"


def test_receipt_publication_never_replaces_a_racing_generation(
    monkeypatch,
    tmp_path,
) -> None:
    record = tmp_path / "record.json"
    record.write_bytes(b"foreign-generation")
    payload = {
        "schema": 1,
        "state": "active",
        "root": str(tmp_path / "root"),
        "device": 1,
        "inode": 1,
        "security_sha256": "a" * 64,
        "token": "b" * 64,
        "helper_pid": 2,
        "helper_created": 3,
    }
    original_lexists = cache.os.path.lexists
    monkeypatch.setattr(
        cache.os.path,
        "lexists",
        lambda path: False if Path(path) == record else original_lexists(path),
    )

    with pytest.raises(cache.PackageCacheError, match="could not be published"):
        cache._write_receipt(record, payload)

    assert record.read_bytes() == b"foreign-generation"


@pytest.mark.skipif(sys.platform != "win32", reason="native helper contract is Windows-only")
def test_helper_removes_exact_root_when_receipt_publication_fails(
    monkeypatch,
    tmp_path,
) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))
    record = tmp_path / "record.json"
    record.write_bytes(b"foreign-generation")
    root = parent / "a"
    monkeypatch.setattr(cache, "_WindowsCacheBackend", lambda: backend)
    monkeypatch.setattr(cache, "_candidate_names", lambda: ("a",))

    assert cache._cleanup_helper(record, len(str(root))) == 1

    assert not root.exists()
    assert record.read_bytes() == b"foreign-generation"


def test_body_failure_preserves_primary_error_when_cleanup_also_fails(
    monkeypatch,
    tmp_path,
) -> None:
    parent = tmp_path / "p"
    parent.mkdir()
    backend = FakeCacheBackend((parent,))

    def fail_cleanup(*_args):
        raise cache.PackageCacheError("simulated cleanup failure")

    monkeypatch.setattr(cache, "_remove_owned_root", fail_cleanup)
    with pytest.raises(LookupError, match="body failure") as caught:
        with cache.package_cache_session(
            _backend=backend,
            _names=("a",),
            maximum_root_length=len(str(parent / "a")),
        ):
            raise LookupError("body failure")

    assert any("simulated cleanup failure" in note for note in caught.value.__notes__)


@pytest.mark.skipif(sys.platform != "win32", reason="native helper contract is Windows-only")
def test_native_helper_ignores_hostile_python_environment(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("VIBECAD_HOME", str(home))
    monkeypatch.setenv("PyThOnHoMe", str(tmp_path / "hostile-home"))
    monkeypatch.setenv("pYtHoNpAtH", str(tmp_path / "hostile-path"))

    with cache.package_cache_session() as session:
        root = session.root
        session.validate()

    assert "-I" in cache._native_helper_command(home / "record.json", 40)
    assert not root.exists()
    assert not paths.package_cache_record().exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native helper contract is Windows-only")
def test_native_helper_cleans_after_hard_parent_exit(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    environment = dict(os.environ)
    environment["VIBECAD_HOME"] = str(home)
    code = (
        "import os\n"
        "from vibecad.runtime.windows_package_cache import package_cache_session\n"
        "manager = package_cache_session()\n"
        "session = manager.__enter__()\n"
        "print('CRASH_ROOT=' + str(session.root), flush=True)\n"
        "os._exit(17)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-B", "-c", code],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 17, completed.stdout
    root_line = next(
        line for line in completed.stdout.splitlines() if line.startswith("CRASH_ROOT=")
    )
    root = Path(root_line.removeprefix("CRASH_ROOT="))
    record = home / ".package-cache-session.json"
    deadline = time.monotonic() + 15
    while (root.exists() or record.exists()) and time.monotonic() < deadline:
        time.sleep(0.05)

    assert not root.exists()
    assert not record.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="native helper contract is Windows-only")
def test_native_helper_retries_partial_cleanup_while_writer_is_locked(tmp_path) -> None:
    home = tmp_path / "home"
    environment = dict(os.environ)
    environment["VIBECAD_HOME"] = str(home)
    child_code = (
        "import ctypes,time,sys\n"
        "path=sys.argv[1]\n"
        "k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
        "f=k.CreateFileW\n"
        "f.argtypes=(ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p)\n"
        "f.restype=ctypes.c_void_p\n"
        "h=f(path,0xC0000000,0,None,2,0x80,None)\n"
        "assert h not in (None,ctypes.c_void_p(-1).value),ctypes.get_last_error()\n"
        "print('LOCKED',flush=True)\n"
        "time.sleep(2)\n"
        "k.CloseHandle(ctypes.c_void_p(h))\n"
    )
    parent_code = (
        "import os,subprocess,sys\n"
        "from vibecad.runtime.windows_package_cache import package_cache_session\n"
        "manager=package_cache_session()\n"
        "session=manager.__enter__()\n"
        "locked=session.root/'locked.bin'\n"
        f"child_code={child_code!r}\n"
        "child=subprocess.Popen([sys.executable,'-B','-c',child_code,str(locked)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)\n"
        "assert child.stdout is not None and child.stderr is not None\n"
        "line=child.stdout.readline().strip()\n"
        "assert line=='LOCKED',(line,child.stderr.read())\n"
        "child.stdout.close()\n"
        "print('CRASH_ROOT='+str(session.root),flush=True)\n"
        "os._exit(17)\n"
    )

    completed = subprocess.run(
        [sys.executable, "-B", "-c", parent_code],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 17, completed.stdout
    root = Path(
        next(
            line.removeprefix("CRASH_ROOT=")
            for line in completed.stdout.splitlines()
            if line.startswith("CRASH_ROOT=")
        )
    )
    record = home / ".package-cache-session.json"
    deadline = time.monotonic() + 15
    while (root.exists() or record.exists()) and time.monotonic() < deadline:
        time.sleep(0.05)

    assert not root.exists()
    assert not record.exists()
