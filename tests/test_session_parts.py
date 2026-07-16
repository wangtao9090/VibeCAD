# tests/test_session_parts.py
"""Session 多零件注册表：纯逻辑快测（fake，不碰 FreeCAD）。
铁律：_parts 为空（从未 new_part）时一切行为与 R7 完全一致。"""
import pytest

from vibecad.engine import naming
from vibecad.engine.session import Session

# ---- fakes ----


class _FakeObj:
    def __init__(self, name, type_id="Part::Box"):
        self.Name, self.TypeId = name, type_id


class _FakeShape:
    def __init__(self, volume=10.0):
        self.Volume = volume

    def isNull(self):
        return False

    def isValid(self):
        return True


class _FakeSolidObj(_FakeObj):
    def __init__(self, name, type_id="Part::Box", volume=10.0):
        super().__init__(name, type_id)
        self.Shape = _FakeShape(volume)


class _FakeDoc:
    def __init__(self):
        self.Objects = []
        self.log = []

    def openTransaction(self, label):
        self.log.append(("open", label))

    def commitTransaction(self):
        self.log.append(("commit",))

    def abortTransaction(self):
        self.log.append(("abort",))


class _StateDoc(_FakeDoc):
    def __init__(self):
        super().__init__()
        self.PropertiesList = []

    def addProperty(self, _type, name, _group, _doc):
        self.PropertiesList.append(name)


class _CommitFailDoc(_FakeDoc):
    def commitTransaction(self):
        super().commitTransaction()
        raise RuntimeError("commit failed")


class _FakePlacement:
    """fake 容器位姿：identity=False 时 toMatrix() 返回哨兵矩阵供 transformed 消费。"""

    def __init__(self, identity=True):
        self._identity = identity

    def isIdentity(self):
        return self._identity

    def toMatrix(self):
        return "FAKE_MATRIX"


class _FakeContainer:
    def __init__(self, placement=None):
        self.grouped = []
        self.Placement = placement or _FakePlacement()

    def addObject(self, obj):
        self.grouped.append(obj)


def _fake_part(objects=(), placement=None):
    return {"container": _FakeContainer(placement), "objects": set(objects)}


# ---- 注册表基本面（计划 Task 1 Step 1 基准）----


def test_single_part_mode_unchanged():
    s = Session()
    assert s.active_part is None          # 未进入多零件模式
    assert s.part_names() == []


def test_new_part_requires_document():
    s = Session()
    with pytest.raises(RuntimeError, match="无活动文档"):
        s.new_part("盖板")


def test_new_part_duplicate_rejected(monkeypatch):
    s = Session()
    s._doc = _FakeDoc()
    monkeypatch.setattr(s, "_require_doc", lambda: None, raising=False)
    monkeypatch.setattr(s, "_register_part_container", lambda name: object(), raising=False)
    s._register_first_part_if_needed = lambda: None
    s.new_part("盖板")
    with pytest.raises(ValueError, match="已存在"):
        s.new_part("盖板")


def test_set_active_unknown_part():
    s = Session()
    with pytest.raises(ValueError, match="不存在"):
        s.set_active_part("幽灵")


def test_set_active_current_part_is_idempotent_and_not_dirty():
    s = Session()
    s._doc = _FakeDoc()
    s._parts = {"A": _fake_part()}
    s._active_part = "A"
    s.mark_saved()
    revision = s._revision_id
    s.set_active_part("A")
    assert s._revision_id == revision
    assert s.is_dirty() is False


def test_new_part_rejects_empty_name(monkeypatch):
    s = Session()
    monkeypatch.setattr(s, "_require_doc", lambda: None, raising=False)
    with pytest.raises(ValueError, match="非空字符串"):
        s.new_part("")


def test_new_part_sets_active_and_order(monkeypatch):
    s = Session()
    s._doc = _FakeDoc()
    monkeypatch.setattr(s, "_require_doc", lambda: None, raising=False)
    monkeypatch.setattr(s, "_register_part_container", lambda name: _FakeContainer(),
                        raising=False)
    s._register_first_part_if_needed = lambda: None
    out = s.new_part("底板")
    assert out == {"part": "底板", "implicit_part": None}
    s.new_part("盖板")
    assert s.part_names() == ["底板", "盖板"]
    assert s.active_part == "盖板"
    s.set_active_part("底板")
    assert s.active_part == "底板"


def test_new_part_does_not_claim_container_internal_objects(monkeypatch):
    """App::Part 自动 Origin/轴/基准面不属于可建模对象，空零件必须保持 objects 空。"""
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    s._parts = {"A": _fake_part()}
    s._active_part = "A"

    def create_container(name):
        doc.Objects.extend([
            _FakeObj("VibePart001", "App::Part"),
            _FakeObj("Origin001", "App::Origin"),
            _FakeObj("X_Axis001", "App::Line"),
        ])
        return _FakeContainer()

    monkeypatch.setattr(s, "_register_part_container", create_container)
    s._register_first_part_if_needed = lambda: None
    s.new_part("B")
    assert s._parts["B"]["objects"] == set()


def test_new_part_undo_redo_restores_active_part(monkeypatch):
    """活动零件属于事务历史；redo 后不能误留在 undo 阶段的零件。"""
    s = Session()
    s._doc = _FakeDoc()
    monkeypatch.setattr(s, "_register_part_container", lambda name: _FakeContainer())
    s._register_first_part_if_needed = lambda: None
    s.new_part("A")
    s.new_part("B")
    assert s.active_part == "B"
    s.restore_roots_for_undo()
    assert s.active_part == "A"
    s.restore_roots_for_redo()
    assert s.active_part == "B"


def test_dirty_state_uses_full_session_snapshot_across_undo_redo():
    """revision 回拨不能掩盖 active_part/result_roots 与已保存状态的差异。"""
    s = Session()
    s._doc = _FakeDoc()
    s._parts = {"A": _fake_part(), "B": _fake_part()}
    s._active_part = "A"
    s.mark_saved()
    with s._transaction("geometry"):
        pass
    s.set_active_part("B")
    s.restore_roots_for_undo()
    assert s.active_part == "A"
    assert s.is_dirty() is False
    s.restore_roots_for_redo()
    assert s.active_part == "B"
    assert s.is_dirty() is True


def test_new_part_implicit_name_clash(monkeypatch):
    """单零件几何归入隐式 Part1 后，new_part("Part1") 撞名必须响亮拒绝。"""
    s = Session()
    s._doc = _FakeDoc()
    monkeypatch.setattr(s, "_require_doc", lambda: None, raising=False)
    monkeypatch.setattr(s, "_register_part_container", lambda name: _FakeContainer(),
                        raising=False)

    def fake_first():
        s._parts["Part1"] = _fake_part()
        return "Part1"

    s._register_first_part_if_needed = fake_first
    with pytest.raises(ValueError, match="已存在"):
        s.new_part("Part1")


def test_first_new_part_migrates_existing_objects(monkeypatch):
    """单零件模式造过几何后首次 new_part：既有对象归入隐式 Part1（容器+集合），
    单零件命名空间的标签快照随归属迁移（几何没变，不强迫重标注）。"""
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    doc.Objects += [_FakeSolidObj("Box"), _FakeSolidObj("Cut", type_id="Part::Cut")]
    s.set_labels({"A": {"surface": "Plane"}}, {})  # 单零件命名空间快照
    containers = []

    def fake_container(name):
        c = _FakeContainer()
        containers.append((name, c))
        return c

    monkeypatch.setattr(s, "_register_part_container", fake_container, raising=False)
    out = s.new_part("盖板")
    assert out == {"part": "盖板", "implicit_part": "Part1"}
    assert s.part_names() == ["Part1", "盖板"] and s.active_part == "盖板"
    assert s._parts["Part1"]["objects"] == {"Box", "Cut"}
    part1_container = dict(containers)["Part1"]
    assert {o.Name for o in part1_container.grouped} == {"Box", "Cut"}
    assert "__single__" not in s._labels and "Part1" in s._labels


def test_open_and_close_document_reset_parts():
    s = Session()
    s._parts = {"盖板": _fake_part()}
    s._active_part = "盖板"
    s.close_document()  # 无文档早退路径也必须清注册表（与 _labels 同款纪律）
    assert s.part_names() == [] and s.active_part is None


# ---- 差集法对象归属（_transaction 钩子）----


def test_transaction_claims_new_objects_into_active_part():
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    doc.Objects.append(_FakeObj("Box"))  # 事务前已存在 → 不被重复归属
    s._parts = {"盖板": _fake_part()}
    s._active_part = "盖板"
    with s._transaction("add_box"):
        doc.Objects.append(_FakeObj("Box001"))
    assert {o.Name for o in s._parts["盖板"]["container"].grouped} == {"Box001"}
    assert s._parts["盖板"]["objects"] == {"Box001"}
    assert doc.log[-1] == ("commit",)


def test_transaction_zero_overhead_in_single_part_mode():
    """_parts 空（单零件模式）：差集法不启动，事务日志与 R7 完全一致。"""
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    with s._transaction("add_box"):
        doc.Objects.append(_FakeObj("Box"))
    assert doc.log == [("open", "add_box"), ("commit",)]


def test_transaction_abort_does_not_claim():
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    s._parts = {"盖板": _fake_part()}
    s._active_part = "盖板"
    with pytest.raises(RuntimeError, match="boom"), s._transaction("add_box"):
        doc.Objects.append(_FakeObj("Box001"))
        raise RuntimeError("boom")
    assert s._parts["盖板"]["container"].grouped == []
    assert s._parts["盖板"]["objects"] == set()
    assert doc.log[-1] == ("abort",)


def test_transaction_claim_skips_containers():
    """事务中新建的 App::Part 容器不算几何对象，不得归入任何零件。"""
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    s._parts = {"盖板": _fake_part()}
    s._active_part = "盖板"
    with s._transaction("new_part"):
        doc.Objects.append(_FakeObj("VibePart001", type_id="App::Part"))
    assert s._parts["盖板"]["container"].grouped == []
    assert s._parts["盖板"]["objects"] == set()


# ---- get_result_object 的零件维度 ----


def test_get_result_object_single_mode_unchanged():
    """_parts 空：R7 原全文档逻辑（fallback 取最后一个有体积对象）。"""
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    doc.Objects += [_FakeSolidObj("BoxA"), _FakeSolidObj("BoxB")]
    assert s.get_result_object().Name == "BoxB"
    with pytest.raises(RuntimeError, match="无活动文档"):
        Session().get_result_object()


def test_single_part_mode_rejects_explicit_part_without_mutating_saved_state():
    s = Session()
    doc = _FakeDoc()
    box = _FakeSolidObj("Box")
    doc.Objects.append(box)
    s._doc = doc
    s.set_result_object(box)
    s.mark_saved()

    with pytest.raises(ValueError, match="单零件模式"):
        s.get_result_object("ghost")

    assert s._result_roots == {"__single__": "Box"}
    assert s.is_dirty() is False


def test_result_fallback_rejects_invalid_positive_volume_shape():
    class _InvalidShape(_FakeShape):
        def isValid(self):
            return False

    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    invalid = _FakeSolidObj("Broken", type_id="Part::Cut")
    invalid.Shape = _InvalidShape(volume=10)
    valid = _FakeSolidObj("Valid")
    doc.Objects += [valid, invalid]
    assert s.get_result_object() is valid


def test_explicit_result_root_overrides_legacy_type_heuristic():
    """旧 heuristic 会永远优先 Cut；显式 root 必须允许新建 Box 成为当前结果。"""
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    cut = _FakeSolidObj("Cut", type_id="Part::Cut")
    box = _FakeSolidObj("BoxAfterCut")
    doc.Objects += [cut, box]
    s.set_result_object(box)
    assert s.get_result_object() is box


def test_persisted_state_restores_explicit_root():
    doc = _StateDoc()
    box = _FakeSolidObj("Box")
    doc.Objects.append(box)
    writer = Session()
    writer._doc = doc
    writer.set_result_object(box)
    writer.persist_state()

    reader = Session()
    reader._doc = doc
    reader._restore_persisted_state()
    reader._stabilize_result_roots()
    reader.mark_saved()  # load_document 在状态恢复与 root 稳定后建立完整保存快照
    assert reader.get_result_object() is box
    assert reader.is_dirty() is False


def test_result_roots_are_namespaced_per_part():
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    a1, a2, b = (_FakeSolidObj("A1"), _FakeSolidObj("A2"), _FakeSolidObj("B"))
    doc.Objects += [a1, a2, b]
    s._parts = {"A": _fake_part({"A1", "A2"}), "B": _fake_part({"B"})}
    s._active_part = "A"
    s.set_result_object(a1, part="A")
    s.set_result_object(b, part="B")
    assert s.get_result_object("A") is a1
    assert s.get_result_object("B") is b


def test_transaction_abort_restores_explicit_root():
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    old, new = _FakeSolidObj("Old"), _FakeSolidObj("New")
    doc.Objects += [old, new]
    s.set_result_object(old)
    with pytest.raises(RuntimeError, match="boom"), s._transaction("failing"):
        s.set_result_object(new)
        raise RuntimeError("boom")
    assert s.get_result_object() is old


def test_get_result_object_scoped_to_part():
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    a, b = _FakeSolidObj("BoxA"), _FakeSolidObj("BoxB")
    cut = _FakeSolidObj("CutA", type_id="Part::Cut")
    doc.Objects += [a, cut, b]
    s._parts = {"底板": _fake_part({"BoxA", "CutA"}), "盖板": _fake_part({"BoxB"})}
    s._active_part = "盖板"
    assert s.get_result_object() is b            # 默认活动零件
    assert s.get_result_object(part="底板") is cut  # 结果类型表优先于 fallback（同款语义）
    assert s.get_result_shape(part="底板") is cut.Shape
    with pytest.raises(ValueError, match="不存在"):
        s.get_result_object(part="幽灵")


def test_get_result_object_empty_part_raises():
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    doc.Objects.append(_FakeSolidObj("BoxA"))  # 属于别的零件
    s._parts = {"底板": _fake_part({"BoxA"}), "盖板": _fake_part()}
    s._active_part = "盖板"
    with pytest.raises(RuntimeError, match="盖板"):
        s.get_result_object()


# ---- 标签注册表的零件命名空间 ----


class _Vec:
    def __init__(self, x, y, z):
        self.x, self.y, self.z = x, y, z


class _Plane:
    def __init__(self):
        self.Axis = _Vec(0, 0, 1)


class _Face:
    def __init__(self, area, center=(0, 0, 0)):
        self.Surface = _Plane()
        self.Area = area
        self.CenterOfMass = _Vec(*center)


class _FacesShape:
    def __init__(self, faces):
        self.Faces = faces


def test_labels_namespaced_per_part(monkeypatch):
    """同标签 "A" 在不同零件命名空间下解析到各自指纹，互不串扰。"""
    s = Session()
    face_bottom, face_cover = _Face(100.0), _Face(200.0)
    s._parts = {"底板": _fake_part(), "盖板": _fake_part()}
    s._active_part = "底板"
    s.set_labels({"A": naming.face_fingerprint(face_bottom)}, {})  # 默认归活动零件 底板
    s.set_labels({"A": naming.face_fingerprint(face_cover)}, {}, part="盖板")
    shapes = {"底板": _FacesShape([face_bottom]), "盖板": _FacesShape([face_cover])}
    monkeypatch.setattr(s, "get_result_shape",
                        lambda part=None: shapes[part or s._active_part])
    assert s.resolve_face("A") == 0               # 活动零件命名空间
    assert s.resolve_face("A", part="盖板") == 0   # 显式零件命名空间
    # 串扰检查：盖板指纹（面积 200）解析底板面集（面积 100）必须过期
    shapes["盖板"] = _FacesShape([face_bottom])
    with pytest.raises(naming.LabelExpiredError):
        s.resolve_face("A", part="盖板")


def test_labels_shown_accumulates_within_part_namespace():
    """shown 累积逻辑按命名空间隔离：同零件同注册表累积，跨零件不互相污染。"""
    s = Session()
    s._parts = {"底板": _fake_part(), "盖板": _fake_part()}
    s._active_part = "底板"
    freg = {"A": {"surface": "Plane"}}
    s.set_labels(freg, {}, shown={"A"})
    s.set_labels(freg, {}, shown=set())            # 同零件同注册表 → shown 仍含 A
    assert "A" in s._labels["底板"]["shown"]
    s.set_labels(freg, {}, shown=set(), part="盖板")  # 盖板首存 → 不继承底板的 shown
    assert s._labels["盖板"]["shown"] == set()


def test_resolve_unknown_label_in_part_namespace():
    s = Session()
    s._parts = {"盖板": _fake_part()}
    s._active_part = "盖板"
    s.set_labels({"A": {"surface": "Plane"}}, {}, part="盖板")
    with pytest.raises(naming.LabelExpiredError, match="未知边标签"):
        s.resolve_edge("E1")  # 该命名空间没有边标签
    with pytest.raises(naming.LabelExpiredError, match="未知面标签"):
        s.resolve_face("Z")   # 该命名空间没有 Z 标签


# ---- _claim_new_objects 两阶段（终审 M-1）----


def test_claim_new_objects_two_phase_all_or_nothing():
    """addObject 中途失败 → objects 集合不得半更新：abort 后 FreeCAD 重用对象
    Name（真机取证），残名会让下个事务的新对象被误判为已归属。"""
    s = Session()
    s._doc = _FakeDoc()
    s._doc.Objects = [_FakeObj("Old"), _FakeObj("New1"), _FakeObj("New2")]

    class _BoomContainer(_FakeContainer):
        def addObject(self, obj):
            if obj.Name == "New2":
                raise RuntimeError("container boom")
            super().addObject(obj)

    part = {"container": _BoomContainer(), "objects": {"Old"}}
    s._parts = {"A": part}
    s._active_part = "A"
    with pytest.raises(RuntimeError, match="boom"):
        s._claim_new_objects(before={"Old"})
    # 集合零污染（New1 虽已入容器，但容器 Group 随事务 abort 回滚）
    assert part["objects"] == {"Old"}


def test_claim_new_objects_success_registers_all():
    """全部成功 → 一次性入册（容器对象 App::Part 排除在外）。"""
    s = Session()
    s._doc = _FakeDoc()
    s._doc.Objects = [_FakeObj("Old"), _FakeObj("New1"),
                      _FakeObj("C0", type_id="App::Part"), _FakeObj("New2")]
    part = _fake_part({"Old"})
    s._parts = {"A": part}
    s._active_part = "A"
    s._claim_new_objects(before={"Old"})
    assert part["objects"] == {"Old", "New1", "New2"}


def test_transaction_can_claim_new_objects_for_non_active_owner():
    s = Session()
    doc = _FakeDoc()
    s._doc = doc
    s._parts = {"A": _fake_part(), "B": _fake_part()}
    s._active_part = "B"
    with s._transaction("boolean", part="A"):
        doc.Objects.append(_FakeObj("Fuse"))
    assert s._parts["A"]["objects"] == {"Fuse"}
    assert s._parts["B"]["objects"] == set()


def test_commit_failure_restores_roots_revision_and_part_membership():
    s = Session()
    doc = _CommitFailDoc()
    old, new = _FakeSolidObj("Old"), _FakeSolidObj("New")
    doc.Objects.append(old)
    s._doc = doc
    s._parts = {"A": _fake_part({"Old"})}
    s._active_part = "A"
    s.set_result_object(old, part="A")
    revision = s._revision_id

    with pytest.raises(RuntimeError, match="commit failed"):
        with s._transaction("failing-commit", part="A"):
            doc.Objects.append(new)
            s.set_result_object(new, part="A")
            s._revision_id = "leaked-revision"

    assert s._result_roots == {"A": "Old"}
    assert s._revision_id == revision
    assert s._parts["A"]["objects"] == {"Old"}
    assert ("abort",) in doc.log
    assert s._undo_result_roots == []


# ---- owner_of：对象 → 零件反查（终审 C-D 守卫锚定的基石）----


def test_owner_of_reverse_lookup():
    s = Session()
    s._parts = {"底板": _fake_part({"Box", "HoleTool"}), "盖板": _fake_part({"Box001"})}
    assert s.owner_of("HoleTool") == "底板"
    assert s.owner_of("Box001") == "盖板"
    assert s.owner_of("Ghost") is None  # 未归属对象（装配状态异常，调用方拒绝）


def test_owner_of_single_part_mode_returns_none():
    s = Session()  # _parts 空
    assert s.owner_of("Box") is None


# ---- BUG-1 回归：装配模式 resolve 必须在全局坐标系匹配（标注指纹同款坐标系）----


def test_resolve_matches_in_global_frame_when_placement_nonidentity(monkeypatch):
    """黑盒复现根因：align 后容器 Placement 非单位，重新标注的指纹是全局坐标
    （get_assembly_shape 的 compound 面），在局部 Faces 上永远命中 0——
    修复后 resolve 匹配 transformed 后的全局 shape，立即可用新标签二次 align。"""
    s = Session()
    local_face = _Face(100.0, center=(0, 0, 0))     # 局部面（面心在原点）
    global_face = _Face(100.0, center=(0, 0, 10))   # 容器位姿应用后的全局面（z+10）

    class _TransformableShape:
        Faces = [local_face]

        def transformed(self, matrix):
            assert matrix == "FAKE_MATRIX"  # 必须消费容器 Placement.toMatrix()
            return _FacesShape([global_face])

    s._parts = {"盖板": _fake_part(placement=_FakePlacement(identity=False))}
    s._active_part = "盖板"
    s.set_labels({"A": naming.face_fingerprint(global_face)}, {})  # 标注=全局指纹
    monkeypatch.setattr(s, "get_result_shape",
                        lambda part=None: _TransformableShape())
    assert s.resolve_face("A") == 0  # 修复前：局部面集命中 0 → LabelExpiredError


def test_resolve_skips_transform_when_placement_identity(monkeypatch):
    """容器 Placement 为单位时不做 transformed（局部即全局，与修复前行为等价）。"""
    s = Session()
    face = _Face(100.0)

    class _NoTransformShape:
        Faces = [face]

        def transformed(self, matrix):
            raise AssertionError("identity Placement 不应触发 transformed")

    s._parts = {"盖板": _fake_part()}  # 默认单位 Placement
    s._active_part = "盖板"
    s.set_labels({"A": naming.face_fingerprint(face)}, {})
    monkeypatch.setattr(s, "get_result_shape",
                        lambda part=None: _NoTransformShape())
    assert s.resolve_face("A") == 0
