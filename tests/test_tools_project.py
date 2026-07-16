import contextlib
import os
import subprocess
from pathlib import Path

import pytest

from vibecad.runtime import status
from vibecad.tools import project

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "src")


class _SaveDoc:
    Name = "Demo"
    Objects = []
    UndoCount = 2
    RedoCount = 0
    PropertiesList = []

    def recompute(self):
        return None

    def saveCopy(self, path):
        Path(path).write_bytes(b"FCStd")


class _ProjectSession:
    def __init__(self):
        self.doc = _SaveDoc()
        self._result_roots = {"__single__": "Box"}
        self.active_part = None
        self.saved = False
        self.loaded = None
        self.dirty = False

    def _require_doc(self):
        if self.doc is None:
            raise RuntimeError("无活动文档")

    def part_names(self):
        return []

    def persist_state(self):
        self.persisted = True

    def mark_saved(self):
        self.saved = True

    def is_dirty(self):
        return self.dirty

    def load_document(self, path):
        self.loaded = Path(path)
        return self.doc


def test_save_project_adds_fcstd_and_marks_saved(tmp_path):
    session = _ProjectSession()
    out = project.save_project(session, str(tmp_path / "demo"))
    assert out["ok"] is True
    assert out["path"].endswith("demo.FCStd")
    assert Path(out["path"]).read_bytes() == b"FCStd"
    assert session.persisted is True and session.saved is True


def test_save_project_refuses_overwrite(tmp_path):
    target = tmp_path / "demo.FCStd"
    target.write_bytes(b"old")
    with pytest.raises(ValueError, match="已存在"):
        project.save_project(_ProjectSession(), str(target), overwrite=False)
    assert target.read_bytes() == b"old"


def test_save_project_failure_preserves_existing_file(tmp_path):
    target = tmp_path / "demo.FCStd"
    target.write_bytes(b"VALID_OLD_FILE")
    session = _ProjectSession()

    def broken_save(path):
        Path(path).write_bytes(b"BROKEN")
        raise OSError("disk full")

    session.doc.saveCopy = broken_save
    with pytest.raises(OSError, match="disk full"):
        project.save_project(session, str(target))
    assert target.read_bytes() == b"VALID_OLD_FILE"
    assert not list(tmp_path.glob(".demo.*.FCStd"))
    assert session.saved is False


def test_open_project_protects_unsaved_session(tmp_path):
    source = tmp_path / "demo.FCStd"
    source.write_bytes(b"FCStd")
    session = _ProjectSession()
    session.dirty = True
    with pytest.raises(ValueError, match="未保存"):
        project.open_project(session, str(source))
    assert session.loaded is None
    out = project.open_project(session, str(source), discard_unsaved=True)
    assert out["ok"] is True and session.loaded == source


def test_project_path_requires_fcstd_for_open(tmp_path):
    with pytest.raises(ValueError, match="FCStd"):
        project.open_project(_ProjectSession(), str(tmp_path / "demo.step"))


class _HistoryDoc:
    Objects = []
    UndoCount = 1
    RedoCount = 0
    UndoNames = ["add_box"]
    RedoNames = []

    def undo(self):
        self.UndoCount, self.RedoCount = 0, 1

    def redo(self):
        self.UndoCount, self.RedoCount = 1, 0

    def recompute(self):
        if getattr(self, "fail_recompute", False):
            raise RuntimeError("recompute failed")
        return None


class _HistorySession(_ProjectSession):
    def __init__(self):
        super().__init__()
        self.doc = _HistoryDoc()
        self.calls = []

    def restore_roots_for_undo(self):
        self.calls.append("undo_roots")

    def restore_roots_for_redo(self):
        self.calls.append("redo_roots")

    def refresh_model_state(self, *, allow_root_fallback=True):
        self.calls.append(("refresh", allow_root_fallback))


def test_undo_and_redo_sync_session_state():
    session = _HistorySession()
    out = project.undo(session)
    assert out["operation"] == "add_box"
    assert session.calls == ["undo_roots", ("refresh", False)]
    session.doc.RedoNames = ["add_box"]
    out = project.redo(session)
    assert out["operation"] == "add_box"
    assert session.calls[-2:] == ["redo_roots", ("refresh", False)]


def test_undo_empty_history_is_loud():
    session = _HistorySession()
    session.doc.UndoCount = 0
    with pytest.raises(ValueError, match="没有可撤销"):
        project.undo(session)


def test_undo_syncs_session_even_when_recompute_fails():
    session = _HistorySession()
    session.doc.fail_recompute = True
    with pytest.raises(RuntimeError, match="历史已同步"):
        project.undo(session)
    assert session.doc.UndoCount == 0 and session.doc.RedoCount == 1
    assert session.calls == ["undo_roots", ("refresh", False)]


def test_redo_syncs_session_even_when_recompute_fails():
    session = _HistorySession()
    session.doc.UndoCount, session.doc.RedoCount = 0, 1
    session.doc.fail_recompute = True
    with pytest.raises(RuntimeError, match="历史已同步"):
        project.redo(session)
    assert session.doc.UndoCount == 1 and session.doc.RedoCount == 0
    assert session.calls == ["redo_roots", ("refresh", False)]


class _DeleteObj:
    def __init__(self, name, dependents=(), type_id="Part::Feature"):
        self.Name = name
        self.InList = list(dependents)
        self.TypeId = type_id


class _DeleteDoc:
    UndoCount = 0
    RedoCount = 0

    def __init__(self, objects):
        self.Objects = list(objects)

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self):
        return None


class _DeleteSession:
    def __init__(self, objects):
        self.doc = _DeleteDoc(objects)
        self._parts = {}
        self._result_roots = {}
        self.active_part = None
        self.refresh_fallback = None

    def _require_doc(self):
        return None

    def get_object(self, name):
        obj = self.doc.getObject(name)
        if obj is None:
            raise KeyError(name)
        return obj

    def owner_of(self, name):
        return None

    def _result_key(self, owner):
        return "__single__"

    @contextlib.contextmanager
    def _transaction(self, label, part=None):
        yield

    def refresh_model_state(self, *, allow_root_fallback=True):
        self.refresh_fallback = allow_root_fallback

    def part_names(self):
        return []


def test_delete_requires_explicit_cascade_for_dependents():
    downstream = _DeleteObj("Cut")
    base = _DeleteObj("Box", [downstream])
    session = _DeleteSession([base, downstream])
    with pytest.raises(ValueError, match="cascade=true"):
        project.delete_object(session, "Box")
    out = project.delete_object(session, "Box", cascade=True)
    assert out["deleted"] == ["Cut", "Box"]
    assert session.doc.Objects == []
    assert session.refresh_fallback is False


@pytest.mark.slow
def test_save_open_roundtrip_restores_explicit_root(runtime_env, tmp_path):
    target = tmp_path / "roundtrip.FCStd"
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling, project\n"
        + f"target = {str(target)!r}\n"
        + "s = Session(); modeling.new_document(s, 'RoundTrip')\n"
        + "modeling.add_box(s, 10, 10, 10)\n"
        + "last = modeling.add_cylinder(s, 2, 8, position=(20,0,0))\n"
        + "project.save_project(s, target)\n"
        + "loaded = Session(); opened = project.open_project(loaded, target)\n"
        + "assert opened['result_roots']['__single__'] == last['name'], opened\n"
        + "assert loaded.get_result_object().Name == last['name']\n"
        + "assert loaded.doc.UndoCount == 0\n"
        + "print('ROUNDTRIP_OK')\n"
    )
    proc = subprocess.run(
        [runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "ROUNDTRIP_OK" in proc.stdout


@pytest.mark.slow
def test_multipart_save_open_and_placed_measure_real(runtime_env, tmp_path):
    target = tmp_path / "assembly.FCStd"
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import assembly, measure, modeling, project\n"
        + f"target = {str(target)!r}\n"
        + "s = Session(); modeling.new_document(s, 'AssemblyRoundTrip')\n"
        + "s.new_part('A'); a = modeling.add_box(s, 10, 10, 10)\n"
        + "s.new_part('B'); b = modeling.add_box(s, 10, 10, 10)\n"
        + "assembly.place_part(s, 'B', position=(20, 0, 0))\n"
        + "distance = measure.measure(s, kind='distance', first=a['name'], second=b['name'])\n"
        + "assert abs(distance['distance_mm'] - 10) < 1e-6, distance\n"
        + "summary = measure.measure(s)\n"
        + "assert abs(summary['volume_mm3'] - 2000) < 1e-6, summary\n"
        + "assert abs(summary['bbox_mm']['size'][0] - 30) < 1e-6, summary\n"
        + "s.set_active_part('A'); expected_roots = dict(s._result_roots)\n"
        + "project.save_project(s, target)\n"
        + "loaded = Session(); opened = project.open_project(loaded, target)\n"
        + "assert opened['active_part'] == 'A', opened\n"
        + "assert opened['result_roots'] == expected_roots, (opened, expected_roots)\n"
        + "again = measure.measure(loaded, kind='distance', first=a['name'], second=b['name'])\n"
        + "assert abs(again['distance_mm'] - 10) < 1e-6, again\n"
        + "print('ASSEMBLY_ROUNDTRIP_MEASURE_OK')\n"
    )
    proc = subprocess.run(
        [runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "ASSEMBLY_ROUNDTRIP_MEASURE_OK" in proc.stdout


@pytest.mark.slow
def test_delete_current_root_undo_redo_real(runtime_env):
    code = (
        status._PREP
        + f"import sys; sys.path.insert(0, {_SRC!r})\n"
        + "from vibecad.engine.session import Session\n"
        + "from vibecad.tools import modeling, project\n"
        + "s = Session(); modeling.new_document(s, 'DeleteHistory')\n"
        + "base = modeling.add_box(s, 10, 10, 10)\n"
        + "tool = modeling.add_cylinder(s, 2, 20, position=(5,5,-5))\n"
        + "cut = modeling.boolean_cut(s, base['name'], tool['name'])\n"
        + "deleted = project.delete_object(s, cut['name'])\n"
        + "assert deleted['result_roots']['__single__'] == base['name'], deleted\n"
        + "assert s.doc.getObject(cut['name']) is None\n"
        + "undone = project.undo(s)\n"
        + "assert undone['result_roots']['__single__'] == cut['name'], undone\n"
        + "assert s.doc.getObject(cut['name']) is not None\n"
        + "redone = project.redo(s)\n"
        + "assert redone['result_roots']['__single__'] == base['name'], redone\n"
        + "assert s.doc.getObject(cut['name']) is None\n"
        + "print('DELETE_HISTORY_OK')\n"
    )
    proc = subprocess.run(
        [runtime_env, "-c", code], capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "DELETE_HISTORY_OK" in proc.stdout
