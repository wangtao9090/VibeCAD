"""Strict Level-A entity selector contracts and pure resolver behavior."""

from __future__ import annotations

import copy
import dataclasses
from types import MappingProxyType

import pytest

from vibecad.execution.selectors import (
    EntityIdentity,
    EntityKind,
    Provenance,
    ProvenanceSource,
    SelectorError,
    SelectorErrorCode,
    SelectorV1,
    SemanticRole,
    encode_provenance_metadata,
    index_entity_identities,
    parse_entity_identity,
    resolve_selector,
)

PROJECT = "project_" + "1" * 32
OTHER_PROJECT = "project_" + "2" * 32
REVISION = "revision_" + "3" * 32
OTHER_REVISION = "revision_" + "4" * 32
OBJECT = "object_" + "5" * 32
OTHER_OBJECT = "object_" + "6" * 32
FEATURE = "feature_" + "7" * 32
OTHER_FEATURE = "feature_" + "8" * 32


def _provenance(**overrides: object) -> Provenance:
    values: dict[str, object] = {
        "source": ProvenanceSource.MODEL,
        "operation_id": "box",
    }
    values.update(overrides)
    return Provenance(**values)  # type: ignore[arg-type]


def _identity(**overrides: object) -> EntityIdentity:
    values: dict[str, object] = {
        "object_id": OBJECT,
        "feature_id": FEATURE,
        "object_type": "Part::Box",
        "semantic_role": SemanticRole.PRIMITIVE,
        "provenance": _provenance(),
    }
    values.update(overrides)
    return EntityIdentity(**values)  # type: ignore[arg-type]


def _selector(**overrides: object) -> SelectorV1:
    values: dict[str, object] = {
        "project_id": PROJECT,
        "revision_id": REVISION,
        "entity_kind": EntityKind.FEATURE,
        "object_id": OBJECT,
        "feature_id": FEATURE,
        "object_type": "Part::Box",
        "semantic_role": SemanticRole.PRIMITIVE,
        "provenance": _provenance(),
        "expected_cardinality": 1,
    }
    values.update(overrides)
    return SelectorV1(**values)  # type: ignore[arg-type]


class _ManagedObject:
    def __init__(
        self,
        *,
        object_id: str = OBJECT,
        feature_id: str | None = FEATURE,
        object_type: str = "Part::Box",
        semantic_role: str = "primitive",
        provenance: Provenance | None = None,
    ) -> None:
        self.VibeCADObjectId = object_id
        self.VibeCADFeatureId = "" if feature_id is None else feature_id
        self.VibeCADSemanticRole = semantic_role
        self.VibeCADProvenance = encode_provenance_metadata(provenance or _provenance())
        self.TypeId = object_type

    @property
    def Name(self):  # pragma: no cover - any access is a resolver defect
        raise AssertionError("resolver must not access Name")

    @property
    def Label(self):  # pragma: no cover - any access is a resolver defect
        raise AssertionError("resolver must not access Label")


def _assert_error(
    caught: pytest.ExceptionInfo[SelectorError],
    code: SelectorErrorCode,
    *,
    path: str | None = None,
) -> SelectorError:
    error = caught.value
    assert type(error) is SelectorError
    assert error.code is code
    if path is not None:
        assert error.path == path
    assert error.schema_version == 1
    assert error.message
    assert error.args == (error.message,)
    assert set(error.to_mapping()) == {"schema_version", "code", "path", "message"}
    assert SelectorError.from_mapping(error.to_mapping()).to_mapping() == error.to_mapping()
    return error


def test_closed_enums_and_error_codes_are_stable() -> None:
    assert {item.value for item in EntityKind} == {"object", "feature"}
    assert {item.value for item in SemanticRole} == {
        "part",
        "primitive",
        "feature",
        "support",
    }
    assert {item.value for item in ProvenanceSource} == {
        "user",
        "model",
        "system",
        "imported",
    }
    assert {item.value for item in SelectorErrorCode} == {
        "missing_field",
        "unknown_field",
        "unsupported_version",
        "invalid_type",
        "invalid_value",
        "invalid_error_record",
        "wrong_project",
        "stale_revision",
        "malformed_identity",
        "duplicate_identity",
        "zero_match",
        "multiple_matches",
        "invalid_input",
    }


def test_provenance_identity_and_selector_round_trip_exactly() -> None:
    provenance = _provenance()
    assert provenance.to_mapping() == {"source": "model", "operation_id": "box"}
    assert Provenance.from_mapping(provenance.to_mapping()) == provenance
    assert encode_provenance_metadata(provenance) == (
        '{"operation_id":"box","source":"model"}'
    )

    identity = _identity()
    assert EntityIdentity.from_mapping(identity.to_mapping()) == identity
    selector = identity.to_selector(
        project_id=PROJECT,
        revision_id=REVISION,
        entity_kind=EntityKind.FEATURE,
    )
    assert selector == _selector()
    assert SelectorV1.from_mapping(selector.to_mapping()) == selector
    assert copy.copy(selector) is selector
    assert copy.deepcopy(selector) is selector
    with pytest.raises(dataclasses.FrozenInstanceError):
        selector.object_id = OTHER_OBJECT  # type: ignore[misc]


def test_mapping_contracts_accept_deeply_frozen_program_json() -> None:
    mapping = _selector().to_mapping()
    mapping["provenance"] = MappingProxyType(mapping["provenance"])
    frozen = MappingProxyType(mapping)

    assert SelectorV1.from_mapping(frozen) == _selector()


def test_identity_converts_to_object_selector_without_leaking_feature_id() -> None:
    selector = _identity().to_selector(
        project_id=PROJECT,
        revision_id=REVISION,
        entity_kind=EntityKind.OBJECT,
    )
    assert selector.entity_kind is EntityKind.OBJECT
    assert selector.feature_id is None
    assert selector.object_id == OBJECT


def test_feature_selector_requires_feature_identity() -> None:
    with pytest.raises(SelectorError) as caught:
        _identity(feature_id=None).to_selector(
            project_id=PROJECT,
            revision_id=REVISION,
            entity_kind=EntityKind.FEATURE,
        )
    _assert_error(caught, SelectorErrorCode.INVALID_VALUE, path="/feature_id")


@pytest.mark.parametrize("contract", ["provenance", "identity", "selector"])
@pytest.mark.parametrize("case", ["missing", "extra"])
def test_mapping_contracts_reject_missing_and_extra_fields(contract: str, case: str) -> None:
    values = {
        "provenance": _provenance().to_mapping(),
        "identity": _identity().to_mapping(),
        "selector": _selector().to_mapping(),
    }[contract]
    parser = {
        "provenance": Provenance.from_mapping,
        "identity": EntityIdentity.from_mapping,
        "selector": SelectorV1.from_mapping,
    }[contract]
    if case == "missing":
        removed = next(iter(values))
        del values[removed]
        expected = SelectorErrorCode.MISSING_FIELD
    else:
        values["Name"] = "Box"
        expected = SelectorErrorCode.UNKNOWN_FIELD
    with pytest.raises(SelectorError) as caught:
        parser(values)
    _assert_error(caught, expected)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("schema_version", True, SelectorErrorCode.INVALID_TYPE),
        ("schema_version", 2, SelectorErrorCode.UNSUPPORTED_VERSION),
        ("project_id", "project_" + "A" * 32, SelectorErrorCode.INVALID_VALUE),
        ("revision_id", "rev_" + "3" * 32, SelectorErrorCode.INVALID_VALUE),
        ("object_id", "object_" + "5" * 31, SelectorErrorCode.INVALID_VALUE),
        ("feature_id", "feature_" + "G" * 32, SelectorErrorCode.INVALID_VALUE),
        ("object_type", "Box", SelectorErrorCode.INVALID_VALUE),
        ("object_type", "Part::Box\nsecret", SelectorErrorCode.INVALID_VALUE),
        ("semantic_role", "result", SelectorErrorCode.INVALID_VALUE),
        ("expected_cardinality", True, SelectorErrorCode.INVALID_TYPE),
        ("expected_cardinality", 2, SelectorErrorCode.INVALID_VALUE),
    ],
)
def test_selector_rejects_noncanonical_fields(
    field: str,
    value: object,
    code: SelectorErrorCode,
) -> None:
    mapping = _selector().to_mapping()
    mapping[field] = value
    with pytest.raises(SelectorError) as caught:
        SelectorV1.from_mapping(mapping)
    _assert_error(caught, code, path=f"/{field}")


@pytest.mark.parametrize(
    ("kind", "feature_id"),
    [("object", FEATURE), ("feature", None)],
)
def test_entity_kind_and_feature_id_are_consistent(kind: str, feature_id: str | None) -> None:
    mapping = _selector().to_mapping()
    mapping["entity_kind"] = kind
    mapping["feature_id"] = feature_id
    with pytest.raises(SelectorError) as caught:
        SelectorV1.from_mapping(mapping)
    _assert_error(caught, SelectorErrorCode.INVALID_VALUE, path="/feature_id")


@pytest.mark.parametrize(
    "mapping",
    [
        {"source": "unknown", "operation_id": "box"},
        {"source": "model", "operation_id": ""},
        {"source": "model", "operation_id": "x\nsecret"},
        {"source": "model", "operation_id": 1},
    ],
)
def test_provenance_rejects_invalid_source_or_operation_id(mapping: dict[str, object]) -> None:
    with pytest.raises(SelectorError) as caught:
        Provenance.from_mapping(mapping)
    assert caught.value.code in {SelectorErrorCode.INVALID_TYPE, SelectorErrorCode.INVALID_VALUE}


def test_errors_are_fixed_and_do_not_reflect_rejected_values() -> None:
    hostile = "secret\nFORGED LOG LINE"
    mapping = _selector().to_mapping()
    mapping["object_type"] = hostile
    with pytest.raises(SelectorError) as caught:
        SelectorV1.from_mapping(mapping)
    error = _assert_error(caught, SelectorErrorCode.INVALID_VALUE)
    assert hostile not in str(error)
    assert hostile not in error.message
    assert hostile not in repr(error.to_mapping())


def test_hostile_unknown_field_uses_a_bounded_nonreflective_path() -> None:
    hostile = "secret\n" + "x" * 1000
    mapping = _selector().to_mapping()
    mapping[hostile] = 1
    with pytest.raises(SelectorError) as caught:
        SelectorV1.from_mapping(mapping)
    error = _assert_error(caught, SelectorErrorCode.UNKNOWN_FIELD, path="/__unknown__")
    assert hostile not in str(error)


def test_parse_entity_identity_uses_only_managed_metadata_and_actual_type() -> None:
    obj = _ManagedObject()
    assert parse_entity_identity(obj) == _identity()


@pytest.mark.parametrize(
    "attribute",
    [
        "VibeCADObjectId",
        "VibeCADFeatureId",
        "VibeCADSemanticRole",
        "VibeCADProvenance",
        "TypeId",
    ],
)
def test_parse_entity_identity_rejects_missing_required_metadata(attribute: str) -> None:
    obj = _ManagedObject()
    delattr(obj, attribute)
    with pytest.raises(SelectorError) as caught:
        parse_entity_identity(obj)
    _assert_error(caught, SelectorErrorCode.MALFORMED_IDENTITY)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("VibeCADObjectId", "Box"),
        ("VibeCADFeatureId", "feature_" + "Z" * 32),
        ("VibeCADSemanticRole", "result"),
        ("VibeCADProvenance", '{"source":"model", "operation_id":"box"}'),
        ("TypeId", "Box"),
    ],
)
def test_parse_entity_identity_rejects_malformed_metadata(attribute: str, value: object) -> None:
    obj = _ManagedObject()
    setattr(obj, attribute, value)
    with pytest.raises(SelectorError) as caught:
        parse_entity_identity(obj)
    _assert_error(caught, SelectorErrorCode.MALFORMED_IDENTITY)


def test_index_is_ordered_and_rejects_duplicate_object_or_feature_ids() -> None:
    first = _ManagedObject()
    second = _ManagedObject(
        object_id=OTHER_OBJECT,
        feature_id=OTHER_FEATURE,
        object_type="Part::Cylinder",
    )
    assert index_entity_identities((first, second)) == (
        _identity(),
        _identity(
            object_id=OTHER_OBJECT,
            feature_id=OTHER_FEATURE,
            object_type="Part::Cylinder",
        ),
    )

    for duplicate in (
        _ManagedObject(object_id=OBJECT, feature_id=OTHER_FEATURE),
        _ManagedObject(object_id=OTHER_OBJECT, feature_id=FEATURE),
    ):
        with pytest.raises(SelectorError) as caught:
            index_entity_identities((first, duplicate))
        _assert_error(caught, SelectorErrorCode.DUPLICATE_IDENTITY)


def test_resolve_object_and_feature_selectors_by_exact_identity() -> None:
    obj = _ManagedObject()
    assert resolve_selector(
        _selector(),
        (obj,),
        project_id=PROJECT,
        revision_id=REVISION,
    ) is obj
    object_selector = _identity().to_selector(
        project_id=PROJECT,
        revision_id=REVISION,
        entity_kind=EntityKind.OBJECT,
    )
    assert resolve_selector(
        object_selector,
        (obj,),
        project_id=PROJECT,
        revision_id=REVISION,
    ) is obj


class _TraversalBomb:
    def __init__(self) -> None:
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        raise AssertionError("object traversal must not occur")


@pytest.mark.parametrize(
    ("project_id", "revision_id", "code"),
    [
        (OTHER_PROJECT, REVISION, SelectorErrorCode.WRONG_PROJECT),
        (PROJECT, OTHER_REVISION, SelectorErrorCode.STALE_REVISION),
    ],
)
def test_authority_mismatch_is_rejected_before_object_traversal(
    project_id: str,
    revision_id: str,
    code: SelectorErrorCode,
) -> None:
    objects = _TraversalBomb()
    with pytest.raises(SelectorError) as caught:
        resolve_selector(
            _selector(),
            objects,
            project_id=project_id,
            revision_id=revision_id,
        )
    _assert_error(caught, code)
    assert objects.iterations == 0


@pytest.mark.parametrize(
    ("override", "path"),
    [
        ({"object_type": "Part::Cylinder"}, "/object_type"),
        ({"semantic_role": SemanticRole.SUPPORT}, "/semantic_role"),
        (
            {
                "provenance": Provenance(
                    source=ProvenanceSource.SYSTEM,
                    operation_id="box",
                )
            },
            "/provenance",
        ),
    ],
)
def test_type_role_and_provenance_mismatch_produce_zero_match(
    override: dict[str, object],
    path: str,
) -> None:
    with pytest.raises(SelectorError) as caught:
        resolve_selector(
            _selector(**override),
            (_ManagedObject(),),
            project_id=PROJECT,
            revision_id=REVISION,
        )
    _assert_error(caught, SelectorErrorCode.ZERO_MATCH, path=path)


def test_zero_multiple_and_unrelated_duplicate_identities_fail_closed() -> None:
    with pytest.raises(SelectorError) as zero:
        resolve_selector(
            _selector(),
            (),
            project_id=PROJECT,
            revision_id=REVISION,
        )
    _assert_error(zero, SelectorErrorCode.ZERO_MATCH)

    with pytest.raises(SelectorError) as multiple:
        resolve_selector(
            _selector(),
            (_ManagedObject(), _ManagedObject()),
            project_id=PROJECT,
            revision_id=REVISION,
        )
    _assert_error(multiple, SelectorErrorCode.MULTIPLE_MATCHES)

    matching = _ManagedObject()
    unrelated_a = _ManagedObject(
        object_id=OTHER_OBJECT,
        feature_id=OTHER_FEATURE,
        object_type="Part::Cylinder",
    )
    unrelated_b = _ManagedObject(
        object_id=OTHER_OBJECT,
        feature_id=None,
        object_type="App::Part",
        semantic_role="part",
        provenance=Provenance(source=ProvenanceSource.IMPORTED, operation_id=None),
    )
    with pytest.raises(SelectorError) as duplicate:
        resolve_selector(
            _selector(),
            (matching, unrelated_a, unrelated_b),
            project_id=PROJECT,
            revision_id=REVISION,
        )
    _assert_error(duplicate, SelectorErrorCode.DUPLICATE_IDENTITY)


def test_object_selector_does_not_fall_back_to_feature_name_label_or_order() -> None:
    wrong = _ManagedObject(object_id=OTHER_OBJECT, feature_id=OTHER_FEATURE)
    with pytest.raises(SelectorError) as caught:
        resolve_selector(
            _selector(),
            (wrong,),
            project_id=PROJECT,
            revision_id=REVISION,
        )
    _assert_error(caught, SelectorErrorCode.ZERO_MATCH)
