import pytest

from vibecad.engine.session import Session


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
