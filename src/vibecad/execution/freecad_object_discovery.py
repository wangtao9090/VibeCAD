"""Read-only collection of one already-instantiated FreeCAD object schema."""

from __future__ import annotations

import hashlib

from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
)
from vibecad.execution.freecad_object_schemas import (
    MAX_FREECAD_ENUM_VALUES_PER_PROPERTY,
    MAX_FREECAD_PROPERTIES_PER_OBJECT,
    MAX_FREECAD_PROPERTY_DOCUMENTATION_BYTES,
    MAX_FREECAD_PROPERTY_FLAGS,
    MAX_FREECAD_PROPERTY_TEXT_BYTES,
    FreeCadDocumentObjectSchema,
    FreeCadInstantiationMode,
    FreeCadPropertySchema,
)


def _fail(code: CapabilityCatalogErrorCode, path: str) -> None:
    raise CapabilityCatalogError(code, path)


def _bounded_text(value: object, path: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if size > MAX_FREECAD_PROPERTY_TEXT_BYTES or (not value and not allow_empty):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if value and (not value.isprintable() or len(value.splitlines()) != 1):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    return value


def _string_collection(
    value: object,
    path: str,
    *,
    maximum: int,
    allow_none: bool = False,
    allow_empty_items: bool = False,
) -> tuple[str, ...]:
    if value is None and allow_none:
        return ()
    if type(value) not in {list, tuple}:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if len(value) > maximum:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    result = tuple(
        _bounded_text(item, f"{path}/{index}", allow_empty=allow_empty_items)
        for index, item in enumerate(value)
    )
    if len(set(result)) != len(result):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    return result


def _status_flags(value: object, path: str) -> tuple[str, ...]:
    if type(value) not in {list, tuple}:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    if len(value) > MAX_FREECAD_PROPERTY_FLAGS:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, path)
    result: list[str] = []
    for index, item in enumerate(value):
        if type(item) is str:
            result.append(f"name:{_bounded_text(item, f'{path}/{index}')}")
        elif type(item) is int and 0 <= item <= 2**31 - 1:
            result.append(f"code:{item}")
        else:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, f"{path}/{index}")
    if len(set(result)) != len(result):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
    return tuple(result)


def collect_freecad_document_object_schema(
    *,
    native_object: object,
    instantiation_mode: FreeCadInstantiationMode,
) -> FreeCadDocumentObjectSchema:
    """Inspect the static property surface without reading values or mutating state."""

    if type(instantiation_mode) is not FreeCadInstantiationMode:
        _fail(CapabilityCatalogErrorCode.INVALID_INPUT, "instantiation_mode")
    try:
        native_type_id = native_object.TypeId
        property_names = native_object.PropertiesList
    except Exception:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "native_object")
    native_type_id = _bounded_text(native_type_id, "native_object/TypeId")
    if type(property_names) not in {list, tuple}:
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "native_object/PropertiesList")
    if len(property_names) > MAX_FREECAD_PROPERTIES_PER_OBJECT:
        _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, "native_object/PropertiesList")
    names = tuple(
        _bounded_text(item, f"native_object/PropertiesList/{index}")
        for index, item in enumerate(property_names)
    )
    if len(set(names)) != len(names):
        _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, "native_object/PropertiesList")
    properties: list[FreeCadPropertySchema] = []
    for index, name in enumerate(names):
        path = f"native_object/PropertiesList/{index}"
        try:
            property_type = native_object.getTypeIdOfProperty(name)
            group = native_object.getGroupOfProperty(name)
            editor_modes = native_object.getEditorMode(name)
            statuses = native_object.getPropertyStatus(name)
            enumerations = native_object.getEnumerationsOfProperty(name)
            documentation = native_object.getDocumentationOfProperty(name)
        except Exception:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, path)
        property_type = _bounded_text(property_type, f"{path}/type")
        group = _bounded_text(group, f"{path}/group", allow_empty=True)
        modes = _string_collection(
            editor_modes,
            f"{path}/editor_modes",
            maximum=MAX_FREECAD_PROPERTY_FLAGS,
        )
        flags = _status_flags(statuses, f"{path}/status_flags")
        enum_values = _string_collection(
            enumerations,
            f"{path}/enumeration_values",
            maximum=MAX_FREECAD_ENUM_VALUES_PER_PROPERTY,
            allow_none=True,
            allow_empty_items=True,
        )
        if type(documentation) is not str:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, f"{path}/documentation")
        try:
            documentation_bytes = documentation.encode("utf-8")
        except UnicodeError:
            _fail(CapabilityCatalogErrorCode.INTEGRITY_FAILURE, f"{path}/documentation")
        if len(documentation_bytes) > MAX_FREECAD_PROPERTY_DOCUMENTATION_BYTES:
            _fail(CapabilityCatalogErrorCode.BUDGET_EXCEEDED, f"{path}/documentation")
        properties.append(
            FreeCadPropertySchema(
                native_property_name=name,
                native_property_type_id=property_type,
                group_name=group,
                editor_modes=modes,
                status_flags=flags,
                enumeration_values=enum_values,
                documentation_sha256=hashlib.sha256(documentation_bytes).hexdigest(),
            )
        )
    return FreeCadDocumentObjectSchema(
        native_type_id=native_type_id,
        instantiation_mode=instantiation_mode,
        properties=tuple(properties),
    )


__all__ = ()
