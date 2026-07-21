import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from vibecad.engine.session import Session
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SelectorError,
    SemanticRole,
)
from vibecad.runtime import status

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


@pytest.fixture(scope="session")
def existing_managed_runtime_python():
    """Use an already-ready managed runtime; this fixture never invokes the installer."""
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    from vibecad.runtime import paths

    python = paths.active_runtime_python()
    if not python.is_file() or not paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not status.engine_compatible(python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")
    return str(python)


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


class _IdentityObject:
    def __init__(self, name, *, type_id="Part::Box"):
        self.Name = name
        self.TypeId = type_id
        self.PropertiesList = []
        self.add_calls = []
        self._property_types = {}
        self._editor_modes = {}
        self._property_status = {}

    def addProperty(
        self,
        type_id,
        name,
        group="",
        doc="",
        attr=0,
        read_only=False,
        hidden=False,
        locked=False,
    ):
        if name in self.PropertiesList:
            raise RuntimeError("duplicate property")
        self.add_calls.append(
            (type_id, name, group, doc, attr, read_only, hidden, locked)
        )
        self.PropertiesList.append(name)
        self._property_types[name] = type_id
        self._editor_modes[name] = [
            value
            for enabled, value in ((read_only, "ReadOnly"), (hidden, "Hidden"))
            if enabled
        ]
        self._property_status[name] = ["LockDynamic"] if locked else []
        setattr(self, name, "")
        return self

    def getTypeIdOfProperty(self, name):
        return self._property_types[name]

    def getEditorMode(self, name):
        return list(self._editor_modes[name])

    def getPropertyStatus(self, name):
        return list(self._property_status[name])


class _IdentityDoc(FakeDoc):
    def __init__(self, *objects):
        super().__init__([])
        self.Objects = list(objects)
        self._objs = {obj.Name: obj for obj in objects}


_OBJECT_A = "object_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_OBJECT_B = "object_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_FEATURE_A = "feature_11111111111111111111111111111111"
_FEATURE_B = "feature_22222222222222222222222222222222"


def _identity_session(*objects):
    session = Session()
    session._doc = _IdentityDoc(*objects)
    return session


def _identity(
    *,
    object_id=_OBJECT_A,
    feature_id=_FEATURE_A,
    object_type="Part::Box",
    source=ProvenanceSource.MODEL,
    operation_id="box",
):
    return EntityIdentity(
        object_id=object_id,
        feature_id=feature_id,
        object_type=object_type,
        semantic_role=SemanticRole.PRIMITIVE,
        provenance=Provenance(source=source, operation_id=operation_id),
    )


def _attach_a(session, obj):
    return session.attach_object_identity(obj, _identity())


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


def test_attach_read_and_list_object_identity_uses_locked_persistent_properties():
    obj = _IdentityObject("Target")
    session = _identity_session(obj)
    identity = _identity()

    attached = _attach_a(session, obj)

    assert attached == identity
    assert [call[1] for call in obj.add_calls] == [
        "VibeCADObjectId",
        "VibeCADFeatureId",
        "VibeCADSemanticRole",
        "VibeCADProvenance",
    ]
    assert all(
        call[0] == "App::PropertyString"
        and call[2] == "VibeCAD"
        and call[4:] == (0, True, True, True)
        for call in obj.add_calls
    )
    assert session.read_object_identity(obj) == attached
    assert session.list_object_identities() == ((obj, attached),)

    # Exact retry is idempotent and never recreates or overwrites authority.
    assert _attach_a(session, obj) == attached
    assert len(obj.add_calls) == 4
    with pytest.raises(ValueError, match="已附加"):
        session.attach_object_identity(obj, _identity(object_id=_OBJECT_B))
    assert obj.VibeCADObjectId == _OBJECT_A


def test_attach_object_identity_rejects_wrong_contract_or_type_before_mutation():
    obj = _IdentityObject("Target")
    session = _identity_session(obj)

    with pytest.raises(TypeError, match="EntityIdentity"):
        session.attach_object_identity(obj, object())
    with pytest.raises(ValueError, match="object_type"):
        session.attach_object_identity(obj, _identity(object_type="Part::Cylinder"))

    assert obj.PropertiesList == []
    assert obj.add_calls == []


def test_attach_object_identity_persists_nullable_feature_id_as_empty_string():
    obj = _IdentityObject("Imported")
    session = _identity_session(obj)
    identity = _identity(feature_id=None, source=ProvenanceSource.IMPORTED)

    assert session.attach_object_identity(obj, identity) == identity
    assert obj.VibeCADFeatureId == ""
    assert session.read_object_identity(obj).feature_id is None


def test_read_object_identity_rejects_missing_partial_wrong_type_or_flags():
    missing = _IdentityObject("Missing")
    session = _identity_session(missing)
    with pytest.raises(ValueError, match="未附加"):
        session.read_object_identity(missing)

    partial = _IdentityObject("Partial")
    partial.addProperty("App::PropertyString", "VibeCADObjectId")
    partial.VibeCADObjectId = _OBJECT_A
    session = _identity_session(partial)
    with pytest.raises(ValueError, match="不完整"):
        session.read_object_identity(partial)

    wrong_type = _IdentityObject("WrongType")
    session = _identity_session(wrong_type)
    _attach_a(session, wrong_type)
    wrong_type._property_types["VibeCADObjectId"] = "App::PropertyInteger"
    with pytest.raises(ValueError, match="PropertyString"):
        session.read_object_identity(wrong_type)

    wrong_flags = _IdentityObject("WrongFlags")
    session = _identity_session(wrong_flags)
    _attach_a(session, wrong_flags)
    wrong_flags._editor_modes["VibeCADFeatureId"] = []
    with pytest.raises(ValueError, match="flags"):
        session.read_object_identity(wrong_flags)


def test_list_object_identities_rejects_malformed_and_duplicate_authority_without_cache():
    first = _IdentityObject("First")
    second = _IdentityObject("Second")
    session = _identity_session(first, second)
    _attach_a(session, first)
    session.attach_object_identity(
        second,
        _identity(
            object_id=_OBJECT_B,
            feature_id=_FEATURE_B,
            source=ProvenanceSource.IMPORTED,
            operation_id="import",
        ),
    )
    assert [identity.object_id for _, identity in session.list_object_identities()] == [
        _OBJECT_A,
        _OBJECT_B,
    ]

    # Direct property mutation is observed on the next read; Session owns no identity cache.
    second.VibeCADObjectId = _OBJECT_A
    with pytest.raises(SelectorError):
        session.list_object_identities()

    second.VibeCADObjectId = _OBJECT_B
    second.VibeCADProvenance = '{"source": "not-canonical"}'
    with pytest.raises(SelectorError):
        session.list_object_identities()


def test_identity_read_rejects_object_not_owned_by_current_document():
    current = _IdentityObject("Target")
    stale_proxy = _IdentityObject("Target")
    session = _identity_session(current)

    with pytest.raises(ValueError, match="当前文档"):
        _attach_a(session, stale_proxy)
    assert stale_proxy.PropertiesList == []


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


@pytest.mark.slow
def test_object_identity_survives_recompute_checkpoint_close_and_load(
    existing_managed_runtime_python,
    tmp_path,
):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from pathlib import Path\n"
        + "import FreeCAD\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.execution.selectors import (\n"
        + "    EntityIdentity, EntityKind, Provenance, ProvenanceSource, resolve_selector,\n"
        + "    SelectorError, SelectorErrorCode, SemanticRole,\n"
        + ")\n"
        + f"root = Path({str(tmp_path)!r})\n"
        + "a_identity = EntityIdentity(\n"
        + f"    object_id={_OBJECT_A!r}, feature_id={_FEATURE_A!r},\n"
        + "    object_type='Part::Box', semantic_role=SemanticRole.PRIMITIVE,\n"
        + "    provenance=Provenance(source=ProvenanceSource.MODEL, operation_id='box_a'),\n"
        + ")\n"
        + "b_identity = EntityIdentity(\n"
        + f"    object_id={_OBJECT_B!r}, feature_id={_FEATURE_B!r},\n"
        + "    object_type='Part::Box', semantic_role=SemanticRole.PRIMITIVE,\n"
        + "    provenance=Provenance(source=ProvenanceSource.MODEL, operation_id='box_b'),\n"
        + ")\n"
        + "s = Session(checkpoint_dir=root)\n"
        + "loaded = None\n"
        + "try:\n"
        + "    s.open_document('IdentityPersistence')\n"
        + "    with s._transaction('seed identities'):\n"
        + "        a = s.doc.addObject('Part::Box', 'TargetBox')\n"
        + "        a.Length, a.Width, a.Height = 10, 20, 30\n"
        + "        b = s.doc.addObject('Part::Box', 'ControlBox')\n"
        + "        b.Length, b.Width, b.Height = 7, 11, 13\n"
        + "        b.Placement.Base = FreeCAD.Vector(100, 0, 0)\n"
        + "        assert s.attach_object_identity(a, a_identity) == a_identity\n"
        + "        assert s.attach_object_identity(b, b_identity) == b_identity\n"
        + "        s.doc.recompute()\n"
        + "    before = tuple((obj.Name, identity) for obj, identity in "
        + "s.list_object_identities())\n"
        + "    assert before == (('TargetBox', a_identity), ('ControlBox', b_identity))\n"
        + "    project_id = 'project_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n"
        + "    revision_id = 'revision_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'\n"
        + "    selector = a_identity.to_selector(project_id=project_id, "
        + "revision_id=revision_id, entity_kind=EntityKind.FEATURE)\n"
        + "    target = resolve_selector(selector, (a, b), project_id=project_id, "
        + "revision_id=revision_id)\n"
        + "    assert target is a\n"
        + "    try:\n"
        + "        resolve_selector(selector, (a, b), project_id=project_id, "
        + "revision_id='revision_cccccccccccccccccccccccccccccccc')\n"
        + "    except SelectorError as error:\n"
        + "        assert error.code is SelectorErrorCode.STALE_REVISION\n"
        + "    else:\n"
        + "        raise AssertionError('stale selector was accepted')\n"
        + "    try:\n"
        + "        resolve_selector(selector, (b,), project_id=project_id, "
        + "revision_id=revision_id)\n"
        + "    except SelectorError as error:\n"
        + "        assert error.code is SelectorErrorCode.ZERO_MATCH\n"
        + "    else:\n"
        + "        raise AssertionError('zero-hit selector was accepted')\n"
        + "    with s._transaction('modify target only'):\n"
        + "        target.Length = 12\n"
        + "        s.doc.recompute()\n"
        + "    assert abs(a.Shape.Volume - 7200.0) < 1e-7\n"
        + "    assert abs(b.Shape.Volume - 1001.0) < 1e-7\n"
        + "    assert s.read_object_identity(a) == a_identity\n"
        + "    assert s.read_object_identity(b) == b_identity\n"
        + "    checkpoint = s._checkpoint()\n"
        + "    assert checkpoint.is_file() and checkpoint.stat().st_size > 0\n"
        + "    s.close_document()\n"
        + "    loaded = Session()\n"
        + "    loaded.load_document(checkpoint)\n"
        + "    after = loaded.list_object_identities()\n"
        + "    assert tuple(identity for _, identity in after) == (a_identity, b_identity)\n"
        + "    la, lb = (obj for obj, _ in after)\n"
        + "    assert la.Name == 'TargetBox' and lb.Name == 'ControlBox'\n"
        + "    assert abs(float(la.Length) - 12.0) < 1e-9\n"
        + "    assert abs(la.Shape.Volume - 7200.0) < 1e-7\n"
        + "    assert abs(float(lb.Length) - 7.0) < 1e-9\n"
        + "    assert abs(lb.Shape.Volume - 1001.0) < 1e-7\n"
        + "    assert abs(lb.Placement.Base.x - 100.0) < 1e-9\n"
        + "    assert resolve_selector(selector, (la, lb), project_id=project_id, "
        + "revision_id=revision_id) is la\n"
        + "    for obj, _ in after:\n"
        + "        for prop in ('VibeCADObjectId', 'VibeCADFeatureId', "
        + "'VibeCADSemanticRole', 'VibeCADProvenance'):\n"
        + "            assert obj.getTypeIdOfProperty(prop) == 'App::PropertyString'\n"
        + "            assert {'ReadOnly', 'Hidden'} <= set(obj.getEditorMode(prop))\n"
        + "            assert 'LockDynamic' in set(obj.getPropertyStatus(prop))\n"
        + "    loaded.doc.recompute()\n"
        + "    assert tuple(identity for _, identity in loaded.list_object_identities()) == "
        + "(a_identity, b_identity)\n"
        + "    duplicate = loaded.doc.copyObject(la, False)\n"
        + "    assert duplicate.VibeCADObjectId == a_identity.object_id\n"
        + "    loaded.doc.recompute()\n"
        + "    try:\n"
        + "        loaded.list_object_identities()\n"
        + "    except SelectorError:\n"
        + "        pass\n"
        + "    else:\n"
        + "        raise AssertionError('duplicate object identity was accepted')\n"
        + "    missing = loaded.doc.addObject('Part::Box', 'MissingIdentity')\n"
        + "    try:\n"
        + "        loaded.read_object_identity(missing)\n"
        + "    except ValueError:\n"
        + "        pass\n"
        + "    else:\n"
        + "        raise AssertionError('missing object identity was accepted')\n"
        + "    print('IDENTITY_PERSISTENCE_OK')\n"
        + "finally:\n"
        + "    if loaded is not None and loaded.doc is not None:\n"
        + "        loaded.close_document()\n"
        + "    if s.doc is not None:\n"
        + "        s.close_document()\n"
    )
    result = subprocess.run(
        [existing_managed_runtime_python, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert "IDENTITY_PERSISTENCE_OK" in result.stdout
