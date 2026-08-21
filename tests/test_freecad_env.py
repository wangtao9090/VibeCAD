import os
import sys

import pytest

from vibecad import freecad_env as fe


def test_prepare_adds_lib_to_syspath_unix(monkeypatch, tmp_path):
    monkeypatch.setattr(fe.sys, "platform", "linux")
    monkeypatch.setattr(fe.sys, "prefix", str(tmp_path))
    saved = list(sys.path)
    try:
        fe.prepare_freecad_import()
        assert str(tmp_path / "lib") in sys.path
    finally:
        sys.path[:] = saved


@pytest.mark.windows_contract
def test_prepare_adds_library_dirs_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(fe.sys, "platform", "win32")
    monkeypatch.setattr(fe.sys, "prefix", str(tmp_path))
    saved = list(sys.path)
    saved_environment = dict(os.environ)
    try:
        fe.prepare_freecad_import()
        assert str(tmp_path / "Library" / "bin") in sys.path
    finally:
        sys.path[:] = saved
        os.environ.clear()
        os.environ.update(saved_environment)


@pytest.mark.windows_contract
def test_prepare_retains_one_windows_dll_directory_handle(monkeypatch, tmp_path):
    monkeypatch.setattr(fe.sys, "platform", "win32")
    monkeypatch.setattr(fe.sys, "prefix", str(tmp_path))
    key = os.path.normcase(os.path.normpath(tmp_path / "Library" / "bin"))
    fe._WINDOWS_DLL_DIRECTORY_HANDLES.pop(key, None)
    handles: list[object] = []

    def add_dll_directory(path):
        assert os.path.normcase(os.path.normpath(path)) == key
        handle = object()
        handles.append(handle)
        return handle

    monkeypatch.setattr(fe.os, "add_dll_directory", add_dll_directory)
    saved = list(sys.path)
    saved_environment = dict(os.environ)
    try:
        fe.prepare_freecad_import()
        fe.prepare_freecad_import()
        assert handles == [fe._WINDOWS_DLL_DIRECTORY_HANDLES[key]]
    finally:
        fe._WINDOWS_DLL_DIRECTORY_HANDLES.pop(key, None)
        sys.path[:] = saved
        os.environ.clear()
        os.environ.update(saved_environment)


@pytest.mark.windows_contract
def test_windows_activation_matches_reviewed_conda_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(fe.sys, "platform", "win32")
    prefix = tmp_path / "managed env"
    proj = prefix / "Library" / "share" / "proj"
    proj.mkdir(parents=True)
    (proj / "copyright_and_licenses.csv").write_text("reviewed", encoding="utf-8")
    base = {
        "KEEP": "yes",
        "PATH": str(tmp_path / "ambient"),
        "PROJ_DATA": "attacker-proj",
        "SSL_CERT_FILE": "attacker-cert",
    }

    environment = fe.activate_windows_runtime_environment(base, prefix)

    assert base["PROJ_DATA"] == "attacker-proj"
    assert environment["KEEP"] == "yes"
    assert environment["CONDA_PREFIX"] == str(prefix)
    assert environment["CONDA_DEFAULT_ENV"] == prefix.name
    assert environment["CONDA_SHLVL"] == "1"
    assert environment["PROJ_DATA"] == str(proj)
    assert environment["PROJ_NETWORK"] == "OFF"
    assert environment["SSL_CERT_FILE"] == str(prefix / "Library" / "ssl" / "cacert.pem")
    assert environment["SSL_CERT_DIR"] == str(prefix / "Library" / "ssl" / "certs")
    assert environment["XML_CATALOG_FILES"] == (prefix / "etc" / "xml" / "catalog").as_uri()
    assert environment["PATH"].split(os.pathsep)[:6] == [
        str(prefix),
        str(prefix / "Library" / "mingw-w64" / "bin"),
        str(prefix / "Library" / "usr" / "bin"),
        str(prefix / "Library" / "bin"),
        str(prefix / "Scripts"),
        str(prefix / "bin"),
    ]


def test_silence_fd1_restores():
    with fe.silence_fd1():
        pass
    os.write(1, b"")  # fd1 usable again → no exception


def test_server_reexports_freecad_env():
    import vibecad.server as srv

    assert srv._prepare_freecad_import is fe.prepare_freecad_import
    assert srv._silence_fd1 is fe.silence_fd1
