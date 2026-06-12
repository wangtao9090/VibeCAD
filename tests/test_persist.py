"""save_view：落盘/序号递增/滚动清理/文件名消毒。不依赖 FreeCAD。"""
from vibecad.feedback import persist

PNG = b"\x89PNG\r\n\x1a\nfake"


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBECAD_HOME", str(tmp_path))


def test_save_view_writes_and_increments(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    p1 = persist.save_view(PNG, "Demo", "add_box")
    p2 = persist.save_view(PNG, "Demo", "add_hole")
    assert p1.endswith("001-add_box.png") and p2.endswith("002-add_hole.png")
    assert (tmp_path / "views" / "Demo" / "001-add_box.png").read_bytes() == PNG


def test_save_view_rolls_old_files(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    for i in range(25):
        persist.save_view(PNG, "Doc", f"t{i}")
    files = sorted((tmp_path / "views" / "Doc").glob("*.png"))
    assert len(files) == 20 and files[0].name.startswith("006-")


def test_save_view_sanitizes_names(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    p = persist.save_view(PNG, "a/b:c", "x y")
    assert "/views/a_b_c/001-x_y.png" in p.replace("\\", "/")
