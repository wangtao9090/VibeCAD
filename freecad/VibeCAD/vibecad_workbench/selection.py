"""Strict managed-preview selection capture for the FreeCAD Workbench."""

from __future__ import annotations

import json
from dataclasses import dataclass

from vibecad.execution.selectors import (
    EntityKind,
    SelectorV1,
    parse_entity_identity,
    resolve_selector,
)

__all__ = (
    "CapturedSelector",
    "ManagedSelectionObserver",
    "SelectionCaptureError",
    "capture_managed_selector",
)

_ERROR_MESSAGES = {
    "invalid_selection": "invalid selection",
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
    selector: SelectorV1
    text: str


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
