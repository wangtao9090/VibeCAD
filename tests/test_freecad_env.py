import sys

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


def test_prepare_adds_library_dirs_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(fe.sys, "platform", "win32")
    monkeypatch.setattr(fe.sys, "prefix", str(tmp_path))
    saved = list(sys.path)
    try:
        fe.prepare_freecad_import()
        assert str(tmp_path / "Library" / "bin") in sys.path
    finally:
        sys.path[:] = saved


def test_silence_fd1_restores():
    import os
    with fe.silence_fd1():
        pass
    os.write(1, b"")  # fd1 usable again → no exception


def test_server_reexports_freecad_env():
    import vibecad.server as srv
    assert srv._prepare_freecad_import is fe.prepare_freecad_import
    assert srv._silence_fd1 is fe.silence_fd1
