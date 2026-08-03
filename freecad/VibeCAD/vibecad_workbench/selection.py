"""Strict managed-preview selection capture for the FreeCAD Workbench."""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = (
    "CapturedSelector",
    "ManagedSelectionObserver",
    "SelectionCaptureError",
    "capture_managed_selector",
    "selector_identity_request",
)

_ERROR_MESSAGES = {
    "invalid_selection": "invalid selection",
    "selector_backend_unavailable": "selector backend unavailable",
    "unsupported_subelement": "unsupported subelement",
    "selected_object_mismatch": "selected object mismatch",
}


class SelectionCaptureError(ValueError):
    """Fixed local rejection that never reflects untrusted selection data."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _ERROR_MESSAGES:
            raise TypeError("invalid selection capture error code")
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


@dataclass(frozen=True, slots=True)
class CapturedSelector:
    selector: object
    text: str


def selector_identity_request(
    *,
    selected_object: object,
    document_objects: object,
    project_id: str,
    revision_id: str,
    subelements: tuple[str, ...],
) -> dict[str, object]:
    """Detach raw managed identity fields for resolution by the managed backend."""

    if (
        type(project_id) is not str
        or type(revision_id) is not str
        or type(subelements) is not tuple
        or any(type(item) is not str for item in subelements)
        or type(document_objects) not in {list, tuple}
        or not 0 < len(document_objects) <= 1000
    ):
        raise SelectionCaptureError("invalid_selection")
    if subelements:
        raise SelectionCaptureError("unsupported_subelement")
    records: list[dict[str, object]] = []
    selected_indices: list[int] = []
    try:
        for index, item in enumerate(document_objects):
            if item is selected_object:
                selected_indices.append(index)
            object_id = item.VibeCADObjectId
            feature_id = item.VibeCADFeatureId
            object_type = item.TypeId
            semantic_role = item.VibeCADSemanticRole
            provenance = item.VibeCADProvenance
            if (
                type(object_id) is not str
                or (feature_id is not None and type(feature_id) is not str)
                or type(object_type) is not str
                or type(semantic_role) is not str
                or type(provenance) is not str
            ):
                raise TypeError
            records.append(
                {
                    "object_id": object_id,
                    "feature_id": feature_id,
                    "object_type": object_type,
                    "semantic_role": semantic_role,
                    "provenance": provenance,
                }
            )
    except Exception:
        raise SelectionCaptureError("invalid_selection") from None
    if len(selected_indices) != 1:
        raise SelectionCaptureError("selected_object_mismatch")
    return {
        "schema_version": 1,
        "project_id": project_id,
        "revision_id": revision_id,
        "selected_index": selected_indices[0],
        "objects": records,
    }


def capture_managed_selector(
    *,
    selected_object: object,
    document_objects: object,
    project_id: str,
    revision_id: str,
    subelements: tuple[str, ...],
) -> CapturedSelector:
    """Build and uniquely re-resolve one revision-bound Level-A selector."""

    if type(subelements) is not tuple or any(type(item) is not str for item in subelements):
        raise SelectionCaptureError("invalid_selection")
    if subelements:
        raise SelectionCaptureError("unsupported_subelement")
    try:
        from vibecad.execution.selectors import (
            EntityKind,
            parse_entity_identity,
            resolve_selector,
        )
    except ImportError:
        raise SelectionCaptureError("selector_backend_unavailable") from None
    identity = parse_entity_identity(selected_object)
    kind = EntityKind.FEATURE if identity.feature_id is not None else EntityKind.OBJECT
    selector = identity.to_selector(
        project_id=project_id,
        revision_id=revision_id,
        entity_kind=kind,
    )
    resolved = resolve_selector(
        selector,
        document_objects,
        project_id=project_id,
        revision_id=revision_id,
    )
    if resolved is not selected_object:
        raise SelectionCaptureError("selected_object_mismatch")
    text = json.dumps(
        selector.to_mapping(),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return CapturedSelector(selector=selector, text=text)


class ManagedSelectionObserver:
    """Adapt FreeCAD's Selection API without using object names as identity."""

    __slots__ = ("_selection", "_capture", "_clear", "_reject", "_attached")

    def __init__(
        self,
        selection: object,
        *,
        capture: object,
        clear: object,
        reject: object,
    ) -> None:
        if not callable(capture) or not callable(clear) or not callable(reject):
            raise TypeError("invalid selection observer callbacks")
        self._selection = selection
        self._capture = capture
        self._clear = clear
        self._reject = reject
        self._attached = False

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self) -> None:
        add_observer = getattr(self._selection, "addObserver", None)
        if self._attached or not callable(add_observer):
            raise RuntimeError("selection observer cannot be attached")
        add_observer(self)
        self._attached = True

    def detach(self) -> None:
        if not self._attached:
            return
        remove_observer = getattr(self._selection, "removeObserver", None)
        if not callable(remove_observer):
            raise RuntimeError("selection observer cannot be detached")
        remove_observer(self)
        self._attached = False

    def matches(
        self,
        selected_object: object,
        document: object,
        subelements: tuple[str, ...],
    ) -> bool:
        try:
            get_selection = getattr(self._selection, "getSelectionEx", None)
            if not callable(get_selection):
                return False
            records = get_selection()
            if type(records) is not list or len(records) != 1:
                return False
            record = records[0]
            raw_subelements = record.SubElementNames
            return (
                record.Object is selected_object
                and selected_object.Document is document
                and type(raw_subelements) in {list, tuple}
                and tuple(raw_subelements) == subelements
            )
        except Exception:
            return False

    def _refresh(self) -> None:
        try:
            get_selection = getattr(self._selection, "getSelectionEx", None)
            if not callable(get_selection):
                raise TypeError
            records = get_selection()
            if type(records) is not list:
                raise TypeError
            if not records:
                self._clear()
                return
            if len(records) != 1:
                self._reject()
                return
            record = records[0]
            selected_object = record.Object
            document = selected_object.Document
            raw_subelements = record.SubElementNames
            if type(raw_subelements) not in (list, tuple) or any(
                type(item) is not str for item in raw_subelements
            ):
                raise TypeError
            self._capture(
                selected_object,
                document,
                tuple(raw_subelements),
            )
        except Exception:
            self._reject()

    def addSelection(self, *_args: object) -> None:
        self._refresh()

    def removeSelection(self, *_args: object) -> None:
        self._refresh()

    def setSelection(self, *_args: object) -> None:
        self._refresh()

    def clearSelection(self, *_args: object) -> None:
        self._clear()
