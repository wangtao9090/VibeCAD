"""Read-only object-property collector tests."""

from __future__ import annotations

import hashlib

import pytest

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_object_discovery import (
    collect_freecad_document_object_schema,
)
from vibecad.execution.freecad_object_schemas import (
    MAX_FREECAD_PROPERTIES_PER_OBJECT,
    FreeCadInstantiationMode,
)


class _Object:
    TypeId = "Part::Box"
    PropertiesList = ["Width", "MapMode", "Label2"]

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _record(self, method: str, name: str, value):
        self.calls.append((method, name))
        return value

    def getTypeIdOfProperty(self, name: str):
        return self._record(
            "getTypeIdOfProperty",
            name,
            "App::PropertyEnumeration" if name == "MapMode" else "App::PropertyString",
        )

    def getGroupOfProperty(self, name: str):
        return self._record("getGroupOfProperty", name, "Attachment" if name == "MapMode" else "")

    def getEditorMode(self, name: str):
        return self._record("getEditorMode", name, ["Hidden"] if name == "Label2" else [])

    def getPropertyStatus(self, name: str):
        return self._record(
            "getPropertyStatus",
            name,
            ["Output", 26] if name == "Label2" else [],
        )

    def getEnumerationsOfProperty(self, name: str):
        return self._record(
            "getEnumerationsOfProperty",
            name,
            ["Deactivated", "FlatFace"] if name == "MapMode" else None,
        )

    def getDocumentationOfProperty(self, name: str):
        return self._record("getDocumentationOfProperty", name, f"Documentation for {name}")

    def addProperty(self, *_args):  # pragma: no cover - fails if collector mutates
        raise AssertionError("collector must not mutate object")

    def removeProperty(self, *_args):  # pragma: no cover - fails if collector mutates
        raise AssertionError("collector must not mutate object")


def test_collector_reads_complete_static_property_surface_only() -> None:
    native_object = _Object()
    schema = collect_freecad_document_object_schema(
        native_object=native_object,
        instantiation_mode=FreeCadInstantiationMode.TYPE_INSTANCE,
    )
    assert schema.native_type_id == "Part::Box"
    assert [item.native_property_name for item in schema.properties] == [
        "Label2",
        "MapMode",
        "Width",
    ]
    by_name = {item.native_property_name: item for item in schema.properties}
    assert by_name["MapMode"].enumeration_values == ("Deactivated", "FlatFace")
    assert by_name["Label2"].editor_modes == ("Hidden",)
    assert by_name["Label2"].status_flags == ("code:26", "name:Output")
    assert by_name["Width"].group_name == ""
    assert (
        by_name["Width"].documentation_sha256
        == hashlib.sha256(b"Documentation for Width").hexdigest()
    )
    assert len(native_object.calls) == len(native_object.PropertiesList) * 6
    assert {name for name, _property in native_object.calls} == {
        "getDocumentationOfProperty",
        "getEditorMode",
        "getEnumerationsOfProperty",
        "getGroupOfProperty",
        "getPropertyStatus",
        "getTypeIdOfProperty",
    }


class _ExplosiveObject(_Object):
    def getTypeIdOfProperty(self, name: str):
        raise RuntimeError(f"sensitive runtime detail {name}")


def test_collector_translates_property_api_failure_without_reflection() -> None:
    with pytest.raises(CapabilityCatalogError) as failure:
        collect_freecad_document_object_schema(
            native_object=_ExplosiveObject(),
            instantiation_mode=FreeCadInstantiationMode.TYPE_INSTANCE,
        )
    assert failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    assert str(failure.value) == "integrity_failure"
    assert "sensitive" not in str(failure.value)


class _OversizedObject(_Object):
    PropertiesList = [f"Property{index}" for index in range(MAX_FREECAD_PROPERTIES_PER_OBJECT + 1)]


def test_collector_rejects_property_budget_before_method_calls() -> None:
    native_object = _OversizedObject()
    with pytest.raises(CapabilityCatalogError) as failure:
        collect_freecad_document_object_schema(
            native_object=native_object,
            instantiation_mode=FreeCadInstantiationMode.TYPE_INSTANCE,
        )
    assert failure.value.code is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    assert not native_object.calls


class _BadStatusObject(_Object):
    def getPropertyStatus(self, name: str):
        return [True]


def test_collector_rejects_bool_as_integer_status_code() -> None:
    with pytest.raises(CapabilityCatalogError) as failure:
        collect_freecad_document_object_schema(
            native_object=_BadStatusObject(),
            instantiation_mode=FreeCadInstantiationMode.DOCUMENT_OBJECT,
        )
    assert failure.value.code is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
