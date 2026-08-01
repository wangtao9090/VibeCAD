from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.fixtures.freecad_workbench.fake_host import (
    FakeLocalAgentClient,
    force_cleanup_workbench,
    install_fake_pyside,
    make_fake_freecad,
    make_fake_freecad_gui,
    pump_main_events,
)
from vibecad.execution.selectors import (
    EntityKind,
    Provenance,
    ProvenanceSource,
    SelectorV1,
    encode_provenance_metadata,
    resolve_selector,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_ROOT = _REPO_ROOT / "freecad" / "VibeCAD"
_PROJECT = "project_" + "1" * 32
_REVISION = "revision_" + "4" * 32
_OBJECT = "object_" + "5" * 32
_FEATURE = "feature_" + "7" * 32


class _ManagedObject:
    def __init__(
        self,
        document: object,
        *,
        object_id: str = _OBJECT,
        feature_id: str | None = _FEATURE,
    ) -> None:
        self.Document = document
        self.VibeCADObjectId = object_id
        self.VibeCADFeatureId = "" if feature_id is None else feature_id
        self.VibeCADSemanticRole = "primitive"
        self.VibeCADProvenance = encode_provenance_metadata(
            Provenance(source=ProvenanceSource.MODEL, operation_id="box")
        )
        self.TypeId = "Part::Box"

    @property
    def Name(self) -> str:  # pragma: no cover - any access is a C04 defect
        raise AssertionError("selection capture must not use Name")

    @property
    def Label(self) -> str:  # pragma: no cover - any access is a C04 defect
        raise AssertionError("selection capture must not use Label")


def _clear_workbench_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(sys.modules):
        if name == "vibecad_workbench" or name.startswith("vibecad_workbench."):
            monkeypatch.delitem(sys.modules, name, raising=False)


def _load_selection(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(_ADDON_ROOT))
    _clear_workbench_modules(monkeypatch)
    return importlib.import_module("vibecad_workbench.selection")


def test_capture_feature_selector_is_canonical_and_round_trips_to_same_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _load_selection(monkeypatch)
    document = object()
    selected = _ManagedObject(document)

    captured = selection.capture_managed_selector(
        selected_object=selected,
        document_objects=(selected,),
        project_id=_PROJECT,
        revision_id=_REVISION,
        subelements=(),
    )

    mapping = json.loads(captured.text)
    assert captured.selector == SelectorV1.from_mapping(mapping)
    assert captured.selector.entity_kind is EntityKind.FEATURE
    assert captured.selector.feature_id == _FEATURE
    assert captured.text == json.dumps(
        captured.selector.to_mapping(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert (
        resolve_selector(
            captured.selector,
            (selected,),
            project_id=_PROJECT,
            revision_id=_REVISION,
        )
        is selected
    )


def test_capture_without_feature_metadata_yields_whole_object_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _load_selection(monkeypatch)
    document = object()
    selected = _ManagedObject(document, feature_id=None)

    captured = selection.capture_managed_selector(
        selected_object=selected,
        document_objects=[selected],
        project_id=_PROJECT,
        revision_id=_REVISION,
        subelements=(),
    )

    assert captured.selector.entity_kind is EntityKind.OBJECT
    assert captured.selector.feature_id is None


def test_external_identity_request_contains_only_raw_managed_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _load_selection(monkeypatch)
    document = object()
    first = _ManagedObject(document, object_id="object_" + "3" * 32, feature_id=None)
    selected = _ManagedObject(document)

    request = selection.selector_identity_request(
        selected_object=selected,
        document_objects=(first, selected),
        project_id=_PROJECT,
        revision_id=_REVISION,
        subelements=(),
    )

    assert request == {
        "schema_version": 1,
        "project_id": _PROJECT,
        "revision_id": _REVISION,
        "selected_index": 1,
        "objects": [
            {
                "object_id": "object_" + "3" * 32,
                "feature_id": "",
                "object_type": "Part::Box",
                "semantic_role": "primitive",
                "provenance": first.VibeCADProvenance,
            },
            {
                "object_id": _OBJECT,
                "feature_id": _FEATURE,
                "object_type": "Part::Box",
                "semantic_role": "primitive",
                "provenance": selected.VibeCADProvenance,
            },
        ],
    }


@pytest.mark.parametrize("subelement", ["Face1", "Edge2", "Vertex3"])
def test_capture_rejects_every_subelement(
    monkeypatch: pytest.MonkeyPatch,
    subelement: str,
) -> None:
    selection = _load_selection(monkeypatch)
    document = object()
    selected = _ManagedObject(document)

    with pytest.raises(selection.SelectionCaptureError, match="unsupported subelement"):
        selection.capture_managed_selector(
            selected_object=selected,
            document_objects=(selected,),
            project_id=_PROJECT,
            revision_id=_REVISION,
            subelements=(subelement,),
        )


def test_capture_rejects_object_not_identical_to_unique_document_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _load_selection(monkeypatch)
    document = object()
    selected = _ManagedObject(document)
    same_identity = _ManagedObject(document)

    with pytest.raises(selection.SelectionCaptureError, match="selected object mismatch"):
        selection.capture_managed_selector(
            selected_object=selected,
            document_objects=(same_identity,),
            project_id=_PROJECT,
            revision_id=_REVISION,
            subelements=(),
        )


def test_managed_host_captures_draft_binding_and_copies_exact_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad = make_fake_freecad()
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    monkeypatch.syspath_prepend(str(_ADDON_ROOT))
    _clear_workbench_modules(monkeypatch)
    gateway = importlib.import_module("vibecad_workbench.gateway")
    clients: list[FakeLocalAgentClient] = []
    original_gateway = gateway.KernelGateway

    def make_client() -> FakeLocalAgentClient:
        client = FakeLocalAgentClient()
        clients.append(client)
        return client

    monkeypatch.setattr(gateway, "KernelGateway", lambda: original_gateway(make_client))
    host = importlib.import_module("vibecad_workbench.host")
    host.activate_workbench()
    try:
        pump_main_events(
            lambda: (
                bool(freecad_gui.main_window.docks)
                and bool(freecad_gui.main_window.docks[0].task_selector.items)
            )
        )
        dock = freecad_gui.main_window.docks[0]
        dock.open_head_button.click()
        dock.open_draft_button.click()
        pump_main_events(lambda: len(freecad.documents) == 2)
        session = host._session
        assert session is not None
        preview = session.preview
        assert preview is not None
        draft_checkout = dock._preview_checkouts["draft"]
        draft_document = preview.binding_for_checkout(draft_checkout).document
        selected = _ManagedObject(draft_document)
        draft_document.Objects.append(selected)

        freecad_gui.Selection.setSelection(selected)

        assert dock.selector_status_label.text == "Selector ready"
        assert dock.copy_selector_button.enabled is True
        mapping = json.loads(dock.selector_value_label.text)
        assert mapping["project_id"] == _PROJECT
        assert mapping["revision_id"] == _REVISION
        assert mapping["entity_kind"] == "feature"
        assert mapping["object_id"] == _OBJECT
        assert mapping["feature_id"] == _FEATURE
        dock.copy_selector_button.click()
        assert fake_pyside.QtWidgets.QApplication.clipboard().text == (
            dock.selector_value_label.text
        )
        assert dock.selector_status_label.text == "Selector copied"
        assert len(freecad_gui.Selection.observers) == 1
    finally:
        force_cleanup_workbench(host, freecad)
    assert freecad_gui.Selection.observers == []


def test_managed_host_visibly_rejects_face_and_untracked_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad = make_fake_freecad()
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    monkeypatch.syspath_prepend(str(_ADDON_ROOT))
    _clear_workbench_modules(monkeypatch)
    gateway = importlib.import_module("vibecad_workbench.gateway")
    original_gateway = gateway.KernelGateway
    monkeypatch.setattr(
        gateway,
        "KernelGateway",
        lambda: original_gateway(FakeLocalAgentClient),
    )
    host = importlib.import_module("vibecad_workbench.host")
    host.activate_workbench()
    try:
        pump_main_events(
            lambda: (
                bool(freecad_gui.main_window.docks)
                and bool(freecad_gui.main_window.docks[0].project_selector.items)
            )
        )
        dock = freecad_gui.main_window.docks[0]
        dock.open_head_button.click()
        pump_main_events(lambda: len(freecad.documents) == 1)
        document = next(iter(freecad.documents.values()))
        selected = _ManagedObject(document, feature_id=None)
        document.Objects.append(selected)

        freecad_gui.Selection.setSelection(selected, subelements=("Face1",))
        assert dock.selector_status_label.text == "Selection unsupported"
        assert dock.copy_selector_button.enabled is False

        foreign = type("ForeignDocument", (), {"Objects": []})()
        foreign_object = _ManagedObject(foreign)
        foreign.Objects.append(foreign_object)
        freecad_gui.Selection.setSelection(foreign_object)
        assert dock.selector_status_label.text == "Selection unavailable"
        assert dock.selector_value_label.text == ""
        assert dock.copy_selector_button.enabled is False
    finally:
        force_cleanup_workbench(host, freecad)


def test_external_host_resolves_selector_on_existing_private_worker_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pyside = install_fake_pyside()
    monkeypatch.setitem(sys.modules, "PySide", fake_pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", fake_pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtWidgets", fake_pyside.QtWidgets)
    freecad = make_fake_freecad()
    freecad_gui = make_fake_freecad_gui()
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    monkeypatch.setitem(sys.modules, "FreeCADGui", freecad_gui)
    monkeypatch.syspath_prepend(str(_ADDON_ROOT))
    _clear_workbench_modules(monkeypatch)
    gateway = importlib.import_module("vibecad_workbench.gateway")
    clients: list[FakeLocalAgentClient] = []
    original_gateway = gateway.KernelGateway

    class _ExternalSelectorClient(FakeLocalAgentClient):
        def resolve_selector_request(self, request: dict[str, object]) -> dict[str, object]:
            self._record("resolve_selector", request)
            selected = request["objects"][request["selected_index"]]
            selector = {
                "schema_version": 1,
                "project_id": request["project_id"],
                "revision_id": request["revision_id"],
                "entity_kind": "feature",
                "object_id": selected["object_id"],
                "feature_id": selected["feature_id"],
                "object_type": selected["object_type"],
                "semantic_role": selected["semantic_role"],
                "provenance": json.loads(selected["provenance"]),
                "expected_cardinality": 1,
            }
            return {
                "schema_version": 1,
                "selector": selector,
                "text": json.dumps(
                    selector,
                    allow_nan=False,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            }

    def make_client() -> FakeLocalAgentClient:
        client = _ExternalSelectorClient()
        clients.append(client)
        return client

    monkeypatch.setattr(gateway, "KernelGateway", lambda: original_gateway(make_client))
    host = importlib.import_module("vibecad_workbench.host")

    def unavailable(**_kwargs: object) -> object:
        raise host.SelectionCaptureError("selector_backend_unavailable")

    monkeypatch.setattr(host, "capture_managed_selector", unavailable)
    host.activate_workbench()
    try:
        pump_main_events(
            lambda: (
                bool(freecad_gui.main_window.docks)
                and bool(freecad_gui.main_window.docks[0].task_selector.items)
            )
        )
        dock = freecad_gui.main_window.docks[0]
        dock.open_draft_button.click()
        pump_main_events(lambda: len(freecad.documents) == 1)
        session = host._session
        assert session is not None
        assert session.preview is not None
        checkout_id = dock._preview_checkouts["draft"]
        document = session.preview.binding_for_checkout(checkout_id).document
        selected = _ManagedObject(document)
        document.Objects.append(selected)

        freecad_gui.Selection.setSelection(selected)
        pump_main_events(lambda: dock.selector_status_label.text == "Selector ready")

        mapping = json.loads(dock.selector_value_label.text)
        assert mapping["project_id"] == _PROJECT
        assert mapping["revision_id"] == _REVISION
        assert mapping["object_id"] == _OBJECT
        assert mapping["feature_id"] == _FEATURE
        assert [name for name, _, _ in clients[0].calls].count("resolve_selector") == 1
        assert clients[0].created_thread_id != session.main_thread_id
    finally:
        force_cleanup_workbench(host, freecad)
