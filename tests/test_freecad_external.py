from __future__ import annotations

import json
import os
import plistlib
import sys
from pathlib import Path

import pytest

from vibecad import freecad_external


def _fake_app(root: Path, *, version: str = "1.1.3", pyside: str = "6.8.3") -> Path:
    app = (root / "FreeCAD.app").resolve()
    contents = app / "Contents"
    executable = contents / "MacOS" / "FreeCAD"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"pilot-freecad")
    executable.chmod(0o755)
    resources = contents / "Resources"
    (resources / "lib" / "python3.11" / "site-packages" / f"PySide6-{pyside}.dist-info").mkdir(
        parents=True
    )
    (resources / "lib" / "libpython3.11.dylib").write_bytes(b"python")
    (resources / "Ext" / "PySide").mkdir(parents=True)
    (resources / "Ext" / "PySide" / "__init__.py").write_text(
        "from PySide6 import __version__\n",
        encoding="utf-8",
    )
    metadata = (
        resources
        / "lib"
        / "python3.11"
        / "site-packages"
        / f"PySide6-{pyside}.dist-info"
        / "METADATA"
    )
    metadata.write_text(
        f"Metadata-Version: 2.1\nName: PySide6\nVersion: {pyside}\n",
        encoding="utf-8",
    )
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": "org.freecad.FreeCAD",
                "CFBundleExecutable": "FreeCAD",
                "CFBundleVersion": version,
                "CFBundleName": f"FreeCAD_{version}",
            },
            stream,
        )
    return app


def _addon_source() -> Path:
    return (Path(__file__).resolve().parent.parent / "freecad" / "VibeCAD").resolve()


def test_doctor_admits_only_exact_pilot_without_executing_app(tmp_path: Path) -> None:
    app = _fake_app(tmp_path)

    result = freecad_external.doctor(app)

    assert set(result["host"]) == {
        "app_path",
        "bundle_id",
        "freecad_version",
        "python_version",
        "pyside_version",
        "host_fingerprint",
    }
    fingerprint = result["host"]["host_fingerprint"]
    assert type(fingerprint) is str and len(fingerprint) == 64
    assert result == {
        "schema_version": 1,
        "compatible": True,
        "host": {
            "app_path": str(app),
            "bundle_id": "org.freecad.FreeCAD",
            "freecad_version": "1.1.3",
            "python_version": "3.11",
            "pyside_version": "6.8.3",
            "host_fingerprint": fingerprint,
        },
        "managed_fallback": "vibecad --freecad",
    }

    wrong = _fake_app(tmp_path / "wrong", version="1.2.0")
    with pytest.raises(freecad_external.ExternalFreeCADError, match="not admitted"):
        freecad_external.doctor(wrong)


def test_install_is_owned_idempotent_and_clean_uninstall_is_reversible(
    tmp_path: Path,
) -> None:
    app = _fake_app(tmp_path / "host")
    user_data = (tmp_path / "user-data").resolve()
    bridge_root = tmp_path / "managed-runtime"
    bridge_root.mkdir(mode=0o700)
    bridge_target = bridge_root / "python3.12"
    bridge_target.write_bytes(b"#!/bin/sh\nexit 0\n")
    bridge_target.chmod(0o700)
    bridge_python = bridge_root / "python"
    bridge_python.symlink_to(bridge_target.name)

    first = freecad_external.install_addon(
        app,
        user_data_root=user_data,
        packaged_addon=_addon_source(),
        bridge_python=bridge_python,
    )
    second = freecad_external.install_addon(
        app,
        user_data_root=user_data,
        packaged_addon=_addon_source(),
        bridge_python=bridge_python,
    )

    target = user_data / "Mod" / "VibeCAD"
    receipt = json.loads((target / ".vibecad-install.json").read_text(encoding="utf-8"))
    config = json.loads((target / "bridge.json").read_text(encoding="utf-8"))
    assert first["status"] == "installed"
    assert second == {**first, "status": "already_installed"}
    assert receipt["target"] == str(target)
    assert receipt["host_app"] == str(app)
    assert config["python_path"] == str(bridge_python)
    assert config["python_target"] == str(bridge_target)
    assert config["package_version"] == "0.7.0"
    assert not (target / "vibecad").exists()

    sibling = target.parent / "OtherAddon"
    sibling.mkdir()
    outcome = freecad_external.uninstall_addon(app, user_data_root=user_data)

    assert outcome["status"] == "uninstalled"
    assert not target.exists()
    assert sibling.is_dir()


def test_install_upgrades_an_owned_older_package_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app = _fake_app(tmp_path / "host")
    user_data = (tmp_path / "user-data").resolve()
    bridge_python = Path(sys.executable).resolve(strict=True)

    monkeypatch.setattr(freecad_external, "__version__", "0.6.1")
    first = freecad_external.install_addon(
        app,
        user_data_root=user_data,
        packaged_addon=_addon_source(),
        bridge_python=bridge_python,
    )
    monkeypatch.setattr(freecad_external, "__version__", "0.7.0")
    upgraded = freecad_external.install_addon(
        app,
        user_data_root=user_data,
        packaged_addon=_addon_source(),
        bridge_python=bridge_python,
    )

    target = user_data / "Mod" / "VibeCAD"
    receipt = json.loads((target / ".vibecad-install.json").read_text(encoding="utf-8"))
    config = json.loads((target / "bridge.json").read_text(encoding="utf-8"))
    assert first["status"] == "installed"
    assert upgraded["status"] == "upgraded"
    assert upgraded["receipt_id"] != first["receipt_id"]
    assert receipt["package_version"] == "0.7.0"
    assert config["package_version"] == "0.7.0"


def test_install_and_uninstall_refuse_foreign_or_mutated_tree(tmp_path: Path) -> None:
    app = _fake_app(tmp_path / "host")
    user_data = (tmp_path / "user-data").resolve()
    target = user_data / "Mod" / "VibeCAD"
    target.mkdir(parents=True)
    foreign = target / "foreign.txt"
    foreign.write_text("keep", encoding="utf-8")

    with pytest.raises(freecad_external.ExternalFreeCADError, match="foreign"):
        freecad_external.install_addon(
            app,
            user_data_root=user_data,
            packaged_addon=_addon_source(),
            bridge_python=Path(sys.executable).resolve(strict=True),
        )
    assert foreign.read_text(encoding="utf-8") == "keep"

    os.replace(target, target.parent / "ForeignVibeCAD")
    freecad_external.install_addon(
        app,
        user_data_root=user_data,
        packaged_addon=_addon_source(),
        bridge_python=Path(sys.executable).resolve(strict=True),
    )
    installed = user_data / "Mod" / "VibeCAD"
    installed.chmod(0o700)
    init = installed / "Init.py"
    init.chmod(0o600)
    init.write_text("mutated\n", encoding="utf-8")

    with pytest.raises(freecad_external.ExternalFreeCADError, match="mutated"):
        freecad_external.uninstall_addon(app, user_data_root=user_data)
    assert installed.is_dir()


def test_install_refuses_symlinked_user_data_ancestor_before_writing(tmp_path: Path) -> None:
    app = _fake_app(tmp_path / "host")
    durable = tmp_path / "durable"
    durable.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(durable, target_is_directory=True)

    with pytest.raises(freecad_external.ExternalFreeCADError, match="directory is unsafe"):
        freecad_external.install_addon(
            app,
            user_data_root=linked / "FreeCAD",
            packaged_addon=_addon_source(),
            bridge_python=Path(sys.executable).resolve(strict=True),
        )

    assert list(durable.iterdir()) == []
