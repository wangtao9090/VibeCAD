import os
import subprocess

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
    with pytest.raises(ValueError):
        with s._transaction("t"):
            raise ValueError("boom")
    assert "abortTransaction" in fake.calls
    assert "commitTransaction" not in fake.calls


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


@pytest.mark.slow
def test_session_open_close_checkpoint(runtime_env, tmp_path):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "from vibecad.engine.session import Session\n"
        + f"s = Session(checkpoint_dir=Path({str(tmp_path)!r}))\n"
        + "s.open_document('t')\n"
        + "assert s.doc is not None\n"
        + "p = s._checkpoint()\n"
        + "assert Path(p).exists()\n"
        + "s.close_document()\n"
        + "assert s.doc is None\n"
        + "print('LIFECYCLE_OK')\n"
    )
    r = subprocess.run([runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert "LIFECYCLE_OK" in r.stdout
