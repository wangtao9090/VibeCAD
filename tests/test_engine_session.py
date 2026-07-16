import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vibecad.engine.session import Session
from vibecad.runtime import status

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


class FakeDoc:
    def __init__(self, calls):
        self.calls = calls
        self._objs = {}

    def openTransaction(self, label):
        self.calls.append(f"openTransaction:{label}")

    def commitTransaction(self):
        self.calls.append("commitTransaction")

    def abortTransaction(self):
        self.calls.append("abortTransaction")

    def getObject(self, name):
        return self._objs.get(name)


def test_session_starts_without_freecad():
    import sys
    s = Session()
    assert s.doc is None
    assert "FreeCAD" not in sys.modules  # 构造不 import FreeCAD


def test_transaction_calls_open_commit(monkeypatch):
    s = Session()
    calls = []
    monkeypatch.setattr(s, "_doc", FakeDoc(calls))
    with s._transaction("test"):
        calls.append("body")
    assert calls == ["openTransaction:test", "body", "commitTransaction"]


def test_transaction_aborts_on_exception(monkeypatch):
    s = Session()
    fake = FakeDoc([])
    monkeypatch.setattr(s, "_doc", fake)
    revision = s._revision_id
    with pytest.raises(ValueError):
        with s._transaction("t"):
            s._revision_id = "leaked-revision"
            raise ValueError("boom")
    assert "abortTransaction" in fake.calls
    assert "commitTransaction" not in fake.calls
    assert s._revision_id == revision


def test_revision_dirty_state_tracks_commit_and_undo(monkeypatch):
    s = Session()
    monkeypatch.setattr(s, "_doc", FakeDoc([]))
    s.mark_saved()
    assert s.is_dirty() is False
    with s._transaction("change"):
        pass
    assert s.is_dirty() is True
    s.restore_roots_for_undo()
    assert s.is_dirty() is False


def test_transaction_without_document_raises_runtime_error():
    """无活动文档时 _transaction 必须抛 RuntimeError（中文指导），
    不得让 NoneType.openTransaction 的 AttributeError 穿透到 server 层。"""
    s = Session()
    with pytest.raises(RuntimeError, match="无活动文档"):
        with s._transaction("x"):
            pass  # pragma: no cover - 不应进入事务体


def test_assert_valid_solid_raises_on_invalid():
    s = Session()

    class FakeShape:
        def isValid(self):
            return False

        def isNull(self):
            return False

        Volume = 100.0

    with pytest.raises(RuntimeError, match="几何断言"):
        s.assert_valid_solid(FakeShape())


def test_assert_valid_solid_null_shape_raises_runtime_error():
    """NULL shape 必须走 isNull 分支抛 RuntimeError——BRepCheck_Analyzer 对 NULL shape
    的 isValid() 会抛 Part.OCCError，若先调 isValid 就把原始 OCC 错误泄漏给 server 层。"""
    s = Session()

    class FakeNullShape:
        def isNull(self):
            return True

        def isValid(self):  # 模拟 OCCT：NULL shape 上 isValid 直接炸
            raise Exception("BRepCheck_Analyzer::Init() - NULL shape")

        Volume = 0.0

    with pytest.raises(RuntimeError, match="NULL"):
        s.assert_valid_solid(FakeNullShape())


def test_assert_valid_solid_raises_on_zero_volume():
    s = Session()

    class FakeShape:
        def isValid(self):
            return True

        def isNull(self):
            return False

        Volume = 0.0

    with pytest.raises(RuntimeError, match="体积为零"):
        s.assert_valid_solid(FakeShape())


def test_assert_valid_solid_ok():
    s = Session()

    class FakeShape:
        def isValid(self):
            return True

        def isNull(self):
            return False

        Volume = 1000.0

    s.assert_valid_solid(FakeShape())  # 不抛


def test_get_object_missing_raises(monkeypatch):
    s = Session()
    monkeypatch.setattr(s, "_doc", FakeDoc([]))
    with pytest.raises(KeyError):
        s.get_object("Nope")


class _LifecycleDoc:
    def __init__(self, name):
        self.Name = name
        self.UndoMode = 0


def test_replace_document_close_failure_restores_old_session(monkeypatch):
    s = Session()
    old, candidate = _LifecycleDoc("Old"), _LifecycleDoc("Candidate")
    s._doc = old
    s._result_roots = {"__single__": "OldRoot"}
    closed = []

    def close_owned(doc):
        if doc is old:
            raise RuntimeError("close failed")
        closed.append(doc)

    monkeypatch.setattr(s, "_close_owned_document", close_owned)
    with pytest.raises(RuntimeError, match="close failed"):
        s._replace_document(candidate, restore_state=False)

    assert s.doc is old
    assert s._result_roots == {"__single__": "OldRoot"}
    assert closed == [candidate]


def test_close_document_failure_preserves_session_state(monkeypatch):
    s = Session()
    old = _LifecycleDoc("Old")
    s._doc = old
    s._result_roots = {"__single__": "Root"}
    s._labels = {"__single__": {"faces": {}}}
    revision = s._revision_id
    monkeypatch.setattr(
        s, "_close_owned_document", lambda doc: (_ for _ in ()).throw(RuntimeError("busy")))

    with pytest.raises(RuntimeError, match="busy"):
        s.close_document()

    assert s.doc is old
    assert s._result_roots == {"__single__": "Root"}
    assert s._labels == {"__single__": {"faces": {}}}
    assert s._revision_id == revision


def test_load_document_cleanup_failure_does_not_mask_load_error(monkeypatch, tmp_path):
    source = tmp_path / "broken.FCStd"
    source.write_text("not a FreeCAD document", encoding="utf-8")
    candidate = _LifecycleDoc("Candidate")
    candidate.load = lambda path: (_ for _ in ()).throw(ValueError("invalid FCStd"))
    candidate.recompute = lambda: None
    s = Session()
    monkeypatch.setattr(s, "_ensure_freecad", lambda: None)
    monkeypatch.setitem(sys.modules, "FreeCAD", SimpleNamespace(newDocument=lambda: candidate))
    monkeypatch.setattr(
        s, "_close_owned_document", lambda doc: (_ for _ in ()).throw(RuntimeError("cleanup")))

    with pytest.raises(ValueError, match="invalid FCStd"):
        s.load_document(source)


@pytest.mark.slow
def test_session_open_close_checkpoint(runtime_env, tmp_path):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling\n"
        + f"s = Session(checkpoint_dir=Path({str(tmp_path)!r}))\n"
        + "s.open_document('t')\n"
        + "s.new_part('A')\n"
        + "a = modeling.add_box(s, 10, 10, 10)\n"
        + "s.new_part('B')\n"
        + "modeling.add_box(s, 20, 10, 10)\n"
        + "s.set_active_part('A')\n"
        + "p = s._checkpoint()\n"
        + "assert Path(p).exists()\n"
        + "loaded = Session()\n"
        + "loaded.load_document(p)\n"
        + "assert loaded.active_part == 'A'\n"
        + "assert loaded.get_result_object('A').Name == a['name']\n"
        + "assert loaded.is_dirty() is False\n"
        + "loaded.close_document()\n"
        + "s.close_document()\n"
        + "assert s.doc is None\n"
        + "print('LIFECYCLE_OK')\n"
    )
    r = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "LIFECYCLE_OK" in r.stdout
