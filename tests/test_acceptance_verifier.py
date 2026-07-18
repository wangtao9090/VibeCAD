"""Contract tests for pure deterministic acceptance verification."""

from __future__ import annotations

import ast
import builtins
import copy
import dataclasses
import gc
import inspect
import pickle
import re
import threading
import time
import weakref
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest

import vibecad.validation as validation_package
from vibecad.validation import (
    ArtifactObservation,
    CompiledAcceptance,
    ObservationSnapshot,
    ShapeObservation,
    ValidationError,
    ValidationErrorCode,
    VerificationReceipt,
    VerificationResult,
    compile_acceptance_spec,
    consume_verification_receipt,
    verify_acceptance,
)
from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    EvidenceKind,
    ExecutionEvidence,
    StepResult,
)
from vibecad.workflow.state import CriterionOutcome, CriterionVerdict, VerificationReport

REVISION = "revision_0123456789abcdef0123456789abcdef"
OTHER_REVISION = "revision_11111111111111111111111111111111"
MANIFEST = "2" * 64
OTHER_MANIFEST = "3" * 64
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
VERIFICATION_ID_RE = re.compile(r"^verification_[0-9a-f]{32}$")


def _shape(target: str = "body", **overrides: object) -> ShapeObservation:
    values: dict[str, object] = {
        "target": target,
        "volume_mm3": 100.0,
        "area_mm2": 70.0,
        "bbox_mm": (10.0, 5.0, 2.0),
        "center_of_mass_mm": (5.0, 2.5, 1.0),
        "valid_shape": True,
        "solid_count": 1,
    }
    values.update(overrides)
    return ShapeObservation(**values)  # type: ignore[arg-type]


def _artifact(target: str = "export", **overrides: object) -> ArtifactObservation:
    values: dict[str, object] = {
        "target": target,
        "exists": True,
        "non_empty": True,
        "format": "step",
    }
    values.update(overrides)
    return ArtifactObservation(**values)  # type: ignore[arg-type]


def _snapshot(
    *,
    revision: str = REVISION,
    shapes: tuple[ShapeObservation, ...] | None = None,
    artifacts: tuple[ArtifactObservation, ...] | None = None,
) -> ObservationSnapshot:
    return ObservationSnapshot(
        candidate_revision=revision,
        shapes=(_shape(),) if shapes is None else shapes,
        artifacts=(_artifact(),) if artifacts is None else artifacts,
    )


def _criterion(
    criterion_id: str,
    kind: AcceptanceKind,
    check: str,
    *,
    target: str | None,
    expected: object,
    tolerance: int | float | None = None,
    parameters: dict[str, object] | None = None,
    required: bool = True,
) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        id=criterion_id,
        kind=kind,
        check=check,
        target=target,
        expected=expected,
        tolerance=tolerance,
        parameters={} if parameters is None else parameters,
        required=required,
    )


def _volume(
    *,
    criterion_id: str = "volume",
    target: str | None = "body",
    expected: object = 100.0,
    tolerance: int | float | None = 0.0,
    required: bool = True,
    parameters: dict[str, object] | None = None,
) -> AcceptanceCriterion:
    return _criterion(
        criterion_id,
        AcceptanceKind.GEOMETRY,
        "volume",
        target=target,
        expected=expected,
        tolerance=tolerance,
        parameters={"unit": "mm^3"} if parameters is None else parameters,
        required=required,
    )


def _spec(*criteria: AcceptanceCriterion, acceptance_id: str = "acceptance-main") -> AcceptanceSpec:
    return AcceptanceSpec(id=acceptance_id, criteria=criteria)


def _compiled(*criteria: AcceptanceCriterion) -> CompiledAcceptance:
    return compile_acceptance_spec(_spec(*(criteria or (_volume(),))))


def _verify(
    compiled: CompiledAcceptance,
    snapshot: ObservationSnapshot | object | None = None,
    *,
    revision: str = REVISION,
    manifest: str = MANIFEST,
) -> VerificationResult:
    return verify_acceptance(
        compiled,
        _snapshot() if snapshot is None else snapshot,
        candidate_revision=revision,
        manifest_sha256=manifest,
    )


def _assert_error(caught: pytest.ExceptionInfo[ValidationError], code: ValidationErrorCode):
    error = caught.value
    assert type(error) is ValidationError
    assert error.code is code
    assert error.schema_version == 1
    assert type(error.path) is str
    assert error.path == "" or error.path.startswith("/")
    assert error.message
    assert error.message.isprintable()
    assert len(error.message) <= 256
    assert len(error.message.splitlines()) == 1
    assert error.args == (error.message,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert set(vars(error)) == {"schema_version", "code", "path", "message"}
    assert error.to_mapping() == {
        "schema_version": 1,
        "code": code.value,
        "path": error.path,
        "message": error.message,
    }
    return error


def test_public_surface_is_closed_and_function_signatures_have_no_evidence_escape_hatch() -> None:
    expected = {
        "ArtifactObservation",
        "CompiledAcceptance",
        "ObservationSnapshot",
        "ShapeObservation",
        "ValidationError",
        "ValidationErrorCode",
        "VerificationReceipt",
        "VerificationResult",
        "compile_acceptance_spec",
        "consume_verification_receipt",
        "verify_acceptance",
    }
    assert set(validation_package.__all__) == expected
    assert {name for name in expected if hasattr(validation_package, name)} == expected

    compile_signature = inspect.signature(compile_acceptance_spec)
    assert tuple(compile_signature.parameters) == ("spec",)
    verify_signature = inspect.signature(verify_acceptance)
    assert tuple(verify_signature.parameters) == (
        "compiled",
        "snapshot",
        "candidate_revision",
        "manifest_sha256",
    )
    assert verify_signature.parameters["candidate_revision"].kind is inspect.Parameter.KEYWORD_ONLY
    assert verify_signature.parameters["manifest_sha256"].kind is inspect.Parameter.KEYWORD_ONLY
    consume_signature = inspect.signature(consume_verification_receipt)
    assert tuple(consume_signature.parameters) == (
        "receipt",
        "compiled",
        "snapshot",
        "candidate_revision",
        "manifest_sha256",
    )
    for signature in (verify_signature, consume_signature):
        lowered = " ".join(signature.parameters).lower()
        assert "step" not in lowered
        assert "fact" not in lowered
        assert "evidence" not in lowered
        assert "acknowledged" not in lowered


def test_shape_observation_is_frozen_strict_and_round_trips() -> None:
    shape = _shape()
    assert shape.to_mapping() == {
        "schema_version": 1,
        "target": "body",
        "volume_mm3": 100.0,
        "area_mm2": 70.0,
        "bbox_mm": [10.0, 5.0, 2.0],
        "center_of_mass_mm": [5.0, 2.5, 1.0],
        "valid_shape": True,
        "solid_count": 1,
    }
    assert ShapeObservation.from_mapping(shape.to_mapping()) == shape
    with pytest.raises(FrozenInstanceError):
        shape.volume_mm3 = 200.0  # type: ignore[misc]
    with pytest.raises(ValidationError) as caught:
        ShapeObservation.from_mapping({**shape.to_mapping(), "extra": True})
    error = _assert_error(caught, ValidationErrorCode.UNKNOWN_FIELD)
    assert error.path == "/extra"
    with pytest.raises(ValidationError) as caught:
        ShapeObservation.from_mapping(MappingProxyType(shape.to_mapping()))
    _assert_error(caught, ValidationErrorCode.INVALID_TYPE)


@pytest.mark.parametrize(
    ("overrides", "path"),
    [
        ({"target": ""}, "/target"),
        ({"target": "x\nforged"}, "/target"),
        ({"target": "x" * 257}, "/target"),
        ({"volume_mm3": True}, "/volume_mm3"),
        ({"volume_mm3": -1}, "/volume_mm3"),
        ({"volume_mm3": float("inf")}, "/volume_mm3"),
        ({"area_mm2": float("nan")}, "/area_mm2"),
        ({"area_mm2": -0.1}, "/area_mm2"),
        ({"bbox_mm": (0, 0)}, "/bbox_mm"),
        ({"bbox_mm": (0, 0, 0, 1)}, "/bbox_mm"),
        ({"bbox_mm": (0, True, 1)}, "/bbox_mm/1"),
        ({"bbox_mm": (1, -0.1, 1)}, "/bbox_mm/1"),
        ({"center_of_mass_mm": (0, 1)}, "/center_of_mass_mm"),
        ({"valid_shape": 1}, "/valid_shape"),
        ({"solid_count": True}, "/solid_count"),
        ({"solid_count": -1}, "/solid_count"),
        ({"schema_version": 2}, "/schema_version"),
    ],
)
def test_shape_observation_rejects_noncanonical_values(
    overrides: dict[str, object], path: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        _shape(**overrides)
    error = caught.value
    assert type(error) is ValidationError
    assert error.path == path
    assert error.code in {
        ValidationErrorCode.INVALID_TYPE,
        ValidationErrorCode.INVALID_VALUE,
        ValidationErrorCode.BUDGET_EXCEEDED,
        ValidationErrorCode.UNSUPPORTED_VERSION,
    }


def test_observation_constructors_and_mapping_parsers_keep_tuple_json_boundaries_strict() -> None:
    assert _shape("界" * 85).target == "界" * 85
    with pytest.raises(ValidationError) as caught:
        _shape("界" * 86)
    _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)

    with pytest.raises(ValidationError) as caught:
        _shape(bbox_mm=[10.0, 5.0, 2.0])
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/bbox_mm"

    shape_mapping = _shape().to_mapping()
    shape_mapping["bbox_mm"] = (10.0, 5.0, 2.0)
    with pytest.raises(ValidationError) as caught:
        ShapeObservation.from_mapping(shape_mapping)
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/bbox_mm"

    with pytest.raises(ValidationError) as caught:
        ObservationSnapshot(
            candidate_revision=REVISION,
            shapes=[_shape()],  # type: ignore[arg-type]
            artifacts=(),
        )
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/shapes"

    snapshot_mapping = _snapshot().to_mapping()
    snapshot_mapping["shapes"] = tuple(snapshot_mapping["shapes"])
    with pytest.raises(ValidationError) as caught:
        ObservationSnapshot.from_mapping(snapshot_mapping)
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/shapes"


def test_artifact_observation_is_frozen_strict_and_absence_is_canonical() -> None:
    artifact = _artifact()
    assert artifact.to_mapping() == {
        "schema_version": 1,
        "target": "export",
        "exists": True,
        "non_empty": True,
        "format": "step",
    }
    assert ArtifactObservation.from_mapping(artifact.to_mapping()) == artifact
    absent = ArtifactObservation(target="export", exists=False)
    assert absent.non_empty is False
    assert absent.format is None
    assert ArtifactObservation.from_mapping(absent.to_mapping()) == absent
    with pytest.raises(FrozenInstanceError):
        artifact.exists = False  # type: ignore[misc]

    for overrides, path in (
        ({"exists": 1}, "/exists"),
        ({"non_empty": 1}, "/non_empty"),
        ({"format": "STEP"}, "/format"),
        ({"format": "stl"}, "/format"),
        ({"exists": False, "non_empty": True, "format": None}, "/non_empty"),
        ({"exists": False, "non_empty": False, "format": "step"}, "/format"),
    ):
        with pytest.raises(ValidationError) as caught:
            _artifact(**overrides)
        assert caught.value.path == path


def test_snapshot_is_canonical_bounded_and_digest_round_trips() -> None:
    snapshot = ObservationSnapshot(
        candidate_revision=REVISION,
        shapes=(_shape("a"), _shape("b")),
        artifacts=(_artifact("c"), _artifact("d", format="fcstd")),
    )
    assert DIGEST_RE.fullmatch(snapshot.observation_digest)
    mapping = snapshot.to_mapping()
    assert mapping["observation_digest"] == snapshot.observation_digest
    assert ObservationSnapshot.from_mapping(mapping) == snapshot
    reconstructed = ObservationSnapshot.from_mapping(mapping)
    assert reconstructed.observation_digest == snapshot.observation_digest
    with pytest.raises(FrozenInstanceError):
        snapshot.candidate_revision = OTHER_REVISION  # type: ignore[misc]

    tampered = dict(mapping)
    tampered["observation_digest"] = "0" * 64
    with pytest.raises(ValidationError) as caught:
        ObservationSnapshot.from_mapping(tampered)
    _assert_error(caught, ValidationErrorCode.BINDING_MISMATCH)


def test_snapshot_and_compiled_spec_digests_have_frozen_domain_separated_vectors() -> None:
    snapshot = _snapshot()
    assert snapshot.observation_digest == (
        "60859fd638aa8911c4a72365e80db95f9def3febf3d649bd68306bad75c3b2e8"
    )

    compiled = _compiled()
    assert compiled.acceptance_id == "acceptance-main"
    assert compiled.spec_digest == (
        "225120725a2324ec44712b18db5bd66b09f273a5655b8df1ad734aeaedd888f9"
    )
    assert compiled.spec_digest != snapshot.observation_digest

    changed_expected = _compiled(_volume(expected=99.5))
    assert changed_expected.spec_digest == (
        "ab21e99c43cb8e7494dbe016d6690d006ab49895108d6d95b50dafed2f4ac90c"
    )
    changed_tolerance = _compiled(_volume(tolerance=0.5))
    assert changed_tolerance.spec_digest == (
        "dc9bbfbeff7fc1fd40dccc7e9ecdb5430d92dca10ea98fd47748fa215fb11d5d"
    )
    changed_required = _compiled(_volume(required=False))
    assert changed_required.spec_digest == (
        "089876ff6dd7d5a138a53002753a273f78616c2545f84580f59796e8aa049505"
    )
    changed_target = _compiled(_volume(target="other"))
    assert changed_target.spec_digest == (
        "e2d17ba69f061263a4277baea0fb2d71b530aefdbb7193138b3add338b71b152"
    )

    area = _criterion(
        "area",
        AcceptanceKind.GEOMETRY,
        "area",
        target="body",
        expected=70.0,
        parameters={"unit": "mm^2"},
    )
    forward = _compiled(_volume(), area)
    reversed_order = _compiled(area, _volume())
    other_acceptance = compile_acceptance_spec(
        _spec(_volume(), area, acceptance_id="acceptance-other")
    )
    assert len({forward.spec_digest, reversed_order.spec_digest, other_acceptance.spec_digest}) == 3

    snapshot_variants = (
        _snapshot(revision=OTHER_REVISION),
        _snapshot(shapes=(_shape("other"),)),
        _snapshot(shapes=(_shape(volume_mm3=100.1),)),
        _snapshot(shapes=(_shape(area_mm2=70.1),)),
        _snapshot(shapes=(_shape(bbox_mm=(10.1, 5.0, 2.0)),)),
        _snapshot(shapes=(_shape(center_of_mass_mm=(5.1, 2.5, 1.0)),)),
        _snapshot(shapes=(_shape(valid_shape=False),)),
        _snapshot(shapes=(_shape(solid_count=2),)),
        _snapshot(artifacts=(_artifact("other"),)),
        _snapshot(artifacts=(ArtifactObservation(target="export", exists=False),)),
        _snapshot(artifacts=(_artifact(non_empty=False),)),
        _snapshot(artifacts=(_artifact(format="fcstd"),)),
    )
    assert all(item.observation_digest != snapshot.observation_digest for item in snapshot_variants)
    assert len({item.observation_digest for item in snapshot_variants}) == len(snapshot_variants)


@pytest.mark.parametrize(
    ("factory", "code", "path"),
    [
        (
            lambda: ObservationSnapshot(
                candidate_revision="revision-not-canonical", shapes=(), artifacts=()
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/candidate_revision",
        ),
        (
            lambda: ObservationSnapshot(
                candidate_revision=REVISION,
                shapes=(_shape("b"), _shape("a")),
                artifacts=(),
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/shapes",
        ),
        (
            lambda: ObservationSnapshot(
                candidate_revision=REVISION,
                shapes=(_shape("a"), _shape("a")),
                artifacts=(),
            ),
            ValidationErrorCode.DUPLICATE_TARGET,
            "/shapes",
        ),
        (
            lambda: ObservationSnapshot(
                candidate_revision=REVISION,
                shapes=(),
                artifacts=(_artifact("b"), _artifact("a")),
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/artifacts",
        ),
        (
            lambda: ObservationSnapshot(
                candidate_revision=REVISION,
                shapes=tuple(_shape(f"shape-{index:03d}") for index in range(129)),
                artifacts=(),
            ),
            ValidationErrorCode.BUDGET_EXCEEDED,
            "/shapes",
        ),
        (
            lambda: ObservationSnapshot(
                candidate_revision=REVISION,
                shapes=(),
                artifacts=(_artifact("a"), _artifact("a")),
            ),
            ValidationErrorCode.DUPLICATE_TARGET,
            "/artifacts",
        ),
        (
            lambda: ObservationSnapshot(
                candidate_revision=REVISION,
                shapes=(),
                artifacts=tuple(_artifact(f"artifact-{index:03d}") for index in range(129)),
            ),
            ValidationErrorCode.BUDGET_EXCEEDED,
            "/artifacts",
        ),
    ],
)
def test_snapshot_rejects_bad_identity_order_duplicates_and_budgets(
    factory, code: ValidationErrorCode, path: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        factory()
    error = _assert_error(caught, code)
    assert error.path == path


def test_snapshot_enforces_canonical_byte_and_total_fact_budgets() -> None:
    shapes = tuple(_shape(f"shape-{index:03d}") for index in range(128))
    artifacts = tuple(_artifact(f"artifact-{index:03d}") for index in range(128))
    maximum_fact_snapshot = ObservationSnapshot(
        candidate_revision=REVISION,
        shapes=shapes,
        artifacts=artifacts,
    )
    assert len(maximum_fact_snapshot.shapes) == 128
    assert len(maximum_fact_snapshot.artifacts) == 128

    long_shapes = tuple(_shape(f"{index:03d}-" + "s" * 252) for index in range(128))
    long_artifacts = tuple(_artifact(f"{index:03d}-" + "a" * 252) for index in range(128))
    with pytest.raises(ValidationError) as caught:
        ObservationSnapshot(
            candidate_revision=REVISION,
            shapes=long_shapes,
            artifacts=long_artifacts,
        )
    error = _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)
    assert error.path == ""


def test_compile_returns_an_opaque_authentic_capability() -> None:
    compiled = _compiled()
    assert type(compiled) is CompiledAcceptance
    assert repr(compiled) == "CompiledAcceptance(<opaque>)"
    assert compiled.acceptance_id == "acceptance-main"
    assert DIGEST_RE.fullmatch(compiled.spec_digest)
    with pytest.raises(TypeError):
        CompiledAcceptance()
    with pytest.raises(TypeError):
        type("DerivedCompiled", (CompiledAcceptance,), {})
    with pytest.raises(TypeError):
        compiled.anything = True  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        copy.copy(compiled)
    with pytest.raises(TypeError):
        copy.deepcopy(compiled)
    with pytest.raises((TypeError, pickle.PicklingError)):
        pickle.dumps(compiled)
    with pytest.raises(TypeError):
        compiled.__reduce__()
    with pytest.raises(TypeError):
        compiled.__reduce_ex__(5)
    with pytest.raises(TypeError):
        dataclasses.replace(compiled)
    with pytest.raises(TypeError):
        vars(compiled)
    assert not hasattr(compiled, "to_mapping")
    assert not hasattr(CompiledAcceptance, "from_mapping")


def test_compiled_capability_rejects_slot_tampering_and_a_stolen_seal() -> None:
    compiled = _compiled()
    slot_names = {
        slot
        for base in type(compiled).__mro__
        for slot in getattr(base, "__slots__", ())
        if slot != "__weakref__"
    }
    assert slot_names == {"_seal"}
    seal = object.__getattribute__(compiled, "_seal")

    forged = object.__new__(CompiledAcceptance)
    object.__setattr__(forged, "_seal", seal)
    with pytest.raises(ValidationError) as caught:
        _verify(forged)
    _assert_error(caught, ValidationErrorCode.FORGED_CAPABILITY)

    overwritten = _compiled()
    object.__setattr__(overwritten, "_seal", object())
    with pytest.raises(ValidationError) as caught:
        _verify(overwritten)
    _assert_error(caught, ValidationErrorCode.FORGED_CAPABILITY)

    deleted = _compiled()
    object.__delattr__(deleted, "_seal")
    with pytest.raises(ValidationError) as caught:
        _verify(deleted)
    _assert_error(caught, ValidationErrorCode.FORGED_CAPABILITY)


def test_compile_rejects_non_specs_empty_duplicates_and_over_budget() -> None:
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec({"id": "acceptance-main", "criteria": []})  # type: ignore[arg-type]
    _assert_error(caught, ValidationErrorCode.INVALID_TYPE)

    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec())
    _assert_error(caught, ValidationErrorCode.EMPTY_SPEC)

    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(_volume(), _volume()))
    _assert_error(caught, ValidationErrorCode.DUPLICATE_CRITERION)

    criteria = tuple(_volume(criterion_id=f"volume-{index:03d}") for index in range(129))
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(*criteria))
    _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)

    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(_volume(), acceptance_id="a" * 257))
    error = _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)
    assert error.path == "/id"

    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(_volume(criterion_id="c" * 257)))
    error = _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)
    assert error.path == "/criteria/0/id"

    oversized_unknowns = tuple(
        _criterion(
            f"unknown-{index:03d}",
            AcceptanceKind.VISUAL,
            "later",
            target="body",
            expected="x" * 4000,
            required=False,
        )
        for index in range(80)
    )
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(_volume(), *oversized_unknowns))
    error = _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)
    assert error.path == ""


@pytest.mark.parametrize(
    ("field", "value", "code", "path"),
    [
        ("expected", float("nan"), ValidationErrorCode.INVALID_VALUE, "/criteria/0/expected"),
        ("expected", float("inf"), ValidationErrorCode.INVALID_VALUE, "/criteria/0/expected"),
        ("expected", 2**53, ValidationErrorCode.INVALID_VALUE, "/criteria/0/expected"),
        (
            "tolerance",
            True,
            ValidationErrorCode.INVALID_TOLERANCE,
            "/criteria/0/tolerance",
        ),
        (
            "tolerance",
            -0.1,
            ValidationErrorCode.INVALID_TOLERANCE,
            "/criteria/0/tolerance",
        ),
        (
            "tolerance",
            float("inf"),
            ValidationErrorCode.INVALID_TOLERANCE,
            "/criteria/0/tolerance",
        ),
    ],
)
def test_compile_revalidates_mutated_nonfinite_and_unsafe_numeric_contracts(
    field: str, value: object, code: ValidationErrorCode, path: str
) -> None:
    criterion = _volume()
    object.__setattr__(criterion, field, value)
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(criterion))
    error = _assert_error(caught, code)
    assert error.path == path


def test_compile_revalidates_vector_components_and_redacts_hostile_contract_text() -> None:
    vector = _criterion(
        "center",
        AcceptanceKind.GEOMETRY,
        "center_of_mass",
        target="body",
        expected=(5.0, 2.5, 1.0),
        parameters={"unit": "mm"},
    )
    object.__setattr__(vector, "expected", (5.0, True, 1.0))
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(vector))
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/criteria/0/expected/1"

    secret = "SECRET-MODEL-TEXT-" + "x" * 300
    hostile = _volume(target=secret)
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(hostile))
    error = _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)
    assert secret not in error.message
    assert "SECRET" not in str(error)

    hostile_check = _criterion(
        "unknown",
        AcceptanceKind.VISUAL,
        "later",
        target="body",
        expected=True,
    )
    object.__setattr__(hostile_check, "check", "SECRET\nFORGED-LOG-LINE")
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(hostile_check))
    error = _assert_error(caught, ValidationErrorCode.UNSUPPORTED_CHECK)
    assert "SECRET" not in error.message
    assert "FORGED" not in str(error)


def test_compile_redacts_and_escapes_unknown_parameter_keys() -> None:
    long_key = "SECRET-PARAMETER-" + "x" * 300
    for key in (long_key, "SECRET\nFORGED-LOG-LINE"):
        with pytest.raises(ValidationError) as caught:
            compile_acceptance_spec(_spec(_volume(parameters={"unit": "mm^3", key: True})))
        error = _assert_error(caught, ValidationErrorCode.INVALID_VALUE)
        assert error.path == "/criteria/0/parameters"
        assert "SECRET" not in error.message
        assert "FORGED" not in str(error)

    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(_volume(parameters={"unit": "mm^3", "a/b~c": True})))
    error = _assert_error(caught, ValidationErrorCode.INVALID_VALUE)
    assert error.path == "/criteria/0/parameters/a~1b~0c"

    reflected = _volume()
    object.__setattr__(reflected, "parameters", {1: True})
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(reflected))
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/criteria/0/parameters"


def test_compile_translates_cyclic_reflection_tampering_to_stable_errors() -> None:
    cyclic_parameters: dict[str, object] = {}
    cyclic_parameters["self"] = cyclic_parameters
    supported = _volume()
    object.__setattr__(supported, "parameters", cyclic_parameters)
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(supported))
    _assert_error(caught, ValidationErrorCode.INVALID_VALUE)

    cyclic_expected: list[object] = []
    cyclic_expected.append(cyclic_expected)
    optional_unknown = _criterion(
        "unknown",
        AcceptanceKind.VISUAL,
        "later",
        target="body",
        expected=True,
        required=False,
    )
    object.__setattr__(optional_unknown, "expected", cyclic_expected)
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(_volume(), optional_unknown))
    _assert_error(caught, ValidationErrorCode.INVALID_VALUE)


@pytest.mark.parametrize(
    ("owner", "field", "value", "code", "path"),
    [
        ("spec", "schema_version", True, ValidationErrorCode.INVALID_TYPE, "/schema_version"),
        (
            "criterion",
            "schema_version",
            True,
            ValidationErrorCode.INVALID_TYPE,
            "/criteria/0/schema_version",
        ),
        (
            "criterion",
            "kind",
            "geometry",
            ValidationErrorCode.INVALID_TYPE,
            "/criteria/0/kind",
        ),
        (
            "criterion",
            "check",
            1,
            ValidationErrorCode.INVALID_TYPE,
            "/criteria/0/check",
        ),
    ],
)
def test_compile_revalidates_exact_schema_kind_and_check_types(
    owner: str,
    field: str,
    value: object,
    code: ValidationErrorCode,
    path: str,
) -> None:
    criterion = _volume()
    spec = _spec(criterion)
    object.__setattr__(spec if owner == "spec" else criterion, field, value)
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(spec)
    error = _assert_error(caught, code)
    assert error.path == path


def test_compile_requires_a_supported_machine_check_but_preserves_optional_unknowns() -> None:
    required_unknown = _criterion(
        "unknown",
        AcceptanceKind.VISUAL,
        "looks_right",
        target="body",
        expected=True,
    )
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(required_unknown))
    _assert_error(caught, ValidationErrorCode.UNSUPPORTED_CHECK)

    optional_unknown = _criterion(
        "unknown",
        AcceptanceKind.VISUAL,
        "looks_right",
        target="body",
        expected=True,
        required=False,
    )
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(optional_unknown))
    _assert_error(caught, ValidationErrorCode.EMPTY_SPEC)

    result = _verify(_compiled(_volume(), optional_unknown))
    assert [verdict.criterion_id for verdict in result.report.verdicts] == ["volume", "unknown"]
    assert result.report.verdicts[1].outcome is CriterionOutcome.UNSUPPORTED
    assert result.report.verdicts[1].required is False
    assert result.report.passed is True
    assert type(result.receipt) is VerificationReceipt


@pytest.mark.parametrize(
    ("criterion", "code", "path"),
    [
        (_volume(target=None), ValidationErrorCode.AMBIGUOUS_TARGET, "/criteria/0/target"),
        (
            _volume(parameters={}),
            ValidationErrorCode.INVALID_UNIT,
            "/criteria/0/parameters/unit",
        ),
        (
            _volume(parameters={"unit": "cm^3"}),
            ValidationErrorCode.INVALID_UNIT,
            "/criteria/0/parameters/unit",
        ),
        (
            _volume(parameters={"unit": "mm^3", "relative": True}),
            ValidationErrorCode.INVALID_VALUE,
            "/criteria/0/parameters/relative",
        ),
        (
            _volume(expected=True),
            ValidationErrorCode.INVALID_TYPE,
            "/criteria/0/expected",
        ),
        (
            _criterion(
                "bbox",
                AcceptanceKind.GEOMETRY,
                "bbox",
                target="body",
                expected=(1, 1),
                parameters={"unit": "mm"},
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/criteria/0/expected",
        ),
        (
            _criterion(
                "valid",
                AcceptanceKind.TOPOLOGY,
                "valid_shape",
                target="body",
                expected=1,
            ),
            ValidationErrorCode.INVALID_TYPE,
            "/criteria/0/expected",
        ),
        (
            _criterion(
                "center",
                AcceptanceKind.GEOMETRY,
                "center_of_mass",
                target="body",
                expected=(0, 0, 0, 0),
                parameters={"unit": "mm"},
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/criteria/0/expected",
        ),
        (
            _criterion(
                "valid",
                AcceptanceKind.TOPOLOGY,
                "valid_shape",
                target="body",
                expected=True,
                parameters={"unit": "bool"},
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/criteria/0/parameters/unit",
        ),
        (
            _criterion(
                "solids",
                AcceptanceKind.TOPOLOGY,
                "solid_count",
                target="body",
                expected=1.0,
            ),
            ValidationErrorCode.INVALID_TYPE,
            "/criteria/0/expected",
        ),
        (
            _criterion(
                "exists",
                AcceptanceKind.ARTIFACT,
                "exists",
                target="export",
                expected=True,
                tolerance=0,
            ),
            ValidationErrorCode.INVALID_TOLERANCE,
            "/criteria/0/tolerance",
        ),
        (
            _criterion(
                "format",
                AcceptanceKind.ARTIFACT,
                "format",
                target="export",
                expected="stl",
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/criteria/0/expected",
        ),
        (
            _criterion(
                "format",
                AcceptanceKind.ARTIFACT,
                "format",
                target="export",
                expected="step",
                parameters={"casefold": True},
            ),
            ValidationErrorCode.INVALID_VALUE,
            "/criteria/0/parameters/casefold",
        ),
    ],
)
def test_compile_rejects_ambiguous_or_malformed_supported_checks(
    criterion: AcceptanceCriterion, code: ValidationErrorCode, path: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        compile_acceptance_spec(_spec(criterion))
    error = _assert_error(caught, code)
    assert error.path == path


def _all_supported_spec() -> AcceptanceSpec:
    return _spec(
        _volume(tolerance=0.5),
        _criterion(
            "area",
            AcceptanceKind.GEOMETRY,
            "area",
            target="body",
            expected=70.0,
            tolerance=0.25,
            parameters={"unit": "mm^2"},
        ),
        _criterion(
            "bbox",
            AcceptanceKind.GEOMETRY,
            "bbox",
            target="body",
            expected=(10.0, 5.0, 2.0),
            tolerance=0.1,
            parameters={"unit": "mm"},
        ),
        _criterion(
            "center",
            AcceptanceKind.GEOMETRY,
            "center_of_mass",
            target="body",
            expected=(5.0, 2.5, 1.0),
            tolerance=0.1,
            parameters={"unit": "mm"},
        ),
        _criterion("valid", AcceptanceKind.TOPOLOGY, "valid_shape", target="body", expected=True),
        _criterion("solids", AcceptanceKind.TOPOLOGY, "solid_count", target="body", expected=1),
        _criterion("exists", AcceptanceKind.ARTIFACT, "exists", target="export", expected=True),
        _criterion(
            "non-empty",
            AcceptanceKind.ARTIFACT,
            "non_empty",
            target="export",
            expected=True,
        ),
        _criterion("format", AcceptanceKind.ARTIFACT, "format", target="export", expected="step"),
    )


def test_every_allowlisted_check_passes_and_emits_ordered_durable_evidence() -> None:
    result = _verify(compile_acceptance_spec(_all_supported_spec()))
    assert type(result) is VerificationResult
    assert type(result.report) is VerificationReport
    assert type(result.receipt) is VerificationReceipt
    assert result.report.passed is True
    assert result.report.acceptance_id == "acceptance-main"
    assert result.report.candidate_revision == REVISION
    assert result.report.manifest_sha256 == MANIFEST
    assert result.report.observation_digest == _snapshot().observation_digest
    assert VERIFICATION_ID_RE.fullmatch(result.report.id)
    assert [verdict.criterion_id for verdict in result.report.verdicts] == [
        "volume",
        "area",
        "bbox",
        "center",
        "valid",
        "solids",
        "exists",
        "non-empty",
        "format",
    ]
    assert all(type(verdict) is CriterionVerdict for verdict in result.report.verdicts)
    assert all(verdict.outcome is CriterionOutcome.PASS for verdict in result.report.verdicts)
    assert [verdict.evidence for verdict in result.report.verdicts] == [
        ("/shapes/0/volume_mm3",),
        ("/shapes/0/area_mm2",),
        ("/shapes/0/bbox_mm",),
        ("/shapes/0/center_of_mass_mm",),
        ("/shapes/0/valid_shape",),
        ("/shapes/0/solid_count",),
        ("/artifacts/0/exists",),
        ("/artifacts/0/non_empty",),
        ("/artifacts/0/format",),
    ]
    bbox = result.report.verdicts[2]
    assert bbox.delta == (0.0, 0.0, 0.0)
    assert bbox.tolerance == (0.1,) * 3
    center = result.report.verdicts[3]
    assert center.tolerance == (0.1,) * 3


@pytest.mark.parametrize(
    ("criterion", "snapshot", "expected_delta", "expected_tolerance"),
    [
        (_volume(expected=99.5, tolerance=0.5), _snapshot(), 0.5, 0.5),
        (
            _criterion(
                "area",
                AcceptanceKind.GEOMETRY,
                "area",
                target="body",
                expected=69.75,
                tolerance=0.25,
                parameters={"unit": "mm^2"},
            ),
            _snapshot(),
            0.25,
            0.25,
        ),
        (
            _criterion(
                "bbox",
                AcceptanceKind.GEOMETRY,
                "bbox",
                target="body",
                expected=(9.9, 5, 2),
                tolerance=0.1,
                parameters={"unit": "mm"},
            ),
            _snapshot(),
            (pytest.approx(0.1), 0.0, 0.0),
            (0.1,) * 3,
        ),
        (
            _criterion(
                "center",
                AcceptanceKind.GEOMETRY,
                "center_of_mass",
                target="body",
                expected=(5.1, 2.5, 1.0),
                tolerance=0.1,
                parameters={"unit": "mm"},
            ),
            _snapshot(),
            (pytest.approx(-0.1), 0.0, 0.0),
            (0.1,) * 3,
        ),
    ],
)
def test_numeric_tolerance_boundary_is_inclusive(
    criterion: AcceptanceCriterion,
    snapshot: ObservationSnapshot,
    expected_delta: object,
    expected_tolerance: object,
) -> None:
    verdict = _verify(_compiled(criterion), snapshot).report.verdicts[0]
    assert verdict.outcome is CriterionOutcome.PASS
    if type(expected_delta) is tuple:
        assert len(verdict.delta) == len(expected_delta)  # type: ignore[arg-type]
        for actual, expected in zip(verdict.delta, expected_delta, strict=True):  # type: ignore[arg-type]
            assert actual == expected
    else:
        assert verdict.delta == expected_delta
    assert verdict.tolerance == expected_tolerance


def test_vector_tolerance_is_componentwise_not_euclidean() -> None:
    criterion = _criterion(
        "bbox",
        AcceptanceKind.GEOMETRY,
        "bbox",
        target="body",
        expected=(9.925, 4.925, 1.925),
        tolerance=0.1,
        parameters={"unit": "mm"},
    )
    verdict = _verify(_compiled(criterion)).report.verdicts[0]
    assert verdict.outcome is CriterionOutcome.PASS
    assert verdict.delta == pytest.approx((0.075, 0.075, 0.075))


def test_boolean_and_count_checks_do_not_use_truthiness() -> None:
    criteria = (
        _criterion("valid", AcceptanceKind.TOPOLOGY, "valid_shape", target="body", expected=False),
        _criterion("solids", AcceptanceKind.TOPOLOGY, "solid_count", target="body", expected=0),
        _criterion("exists", AcceptanceKind.ARTIFACT, "exists", target="export", expected=False),
        _criterion(
            "non-empty",
            AcceptanceKind.ARTIFACT,
            "non_empty",
            target="export",
            expected=False,
        ),
    )
    snapshot = _snapshot(
        shapes=(_shape(valid_shape=False, solid_count=0),),
        artifacts=(ArtifactObservation(target="export", exists=False),),
    )
    result = _verify(_compiled(*criteria), snapshot)
    assert [item.outcome for item in result.report.verdicts] == [CriterionOutcome.PASS] * 4
    assert type(result.receipt) is VerificationReceipt


@pytest.mark.parametrize(
    ("criterion", "snapshot", "observed"),
    [
        (_volume(expected=99.4, tolerance=0.5), _snapshot(), 100.0),
        (
            _criterion(
                "area",
                AcceptanceKind.GEOMETRY,
                "area",
                target="body",
                expected=69.4,
                tolerance=0.5,
                parameters={"unit": "mm^2"},
            ),
            _snapshot(),
            70.0,
        ),
        (
            _criterion(
                "bbox",
                AcceptanceKind.GEOMETRY,
                "bbox",
                target="body",
                expected=(9.8, 5, 2),
                tolerance=0.1,
                parameters={"unit": "mm"},
            ),
            _snapshot(),
            (10.0, 5.0, 2.0),
        ),
        (
            _criterion(
                "center",
                AcceptanceKind.GEOMETRY,
                "center_of_mass",
                target="body",
                expected=(5.0, 2.5, 1.11),
                tolerance=0.1,
                parameters={"unit": "mm"},
            ),
            _snapshot(),
            (5.0, 2.5, 1.0),
        ),
        (
            _criterion(
                "valid", AcceptanceKind.TOPOLOGY, "valid_shape", target="body", expected=False
            ),
            _snapshot(),
            True,
        ),
        (
            _criterion("solids", AcceptanceKind.TOPOLOGY, "solid_count", target="body", expected=2),
            _snapshot(),
            1,
        ),
        (
            _criterion(
                "exists",
                AcceptanceKind.ARTIFACT,
                "exists",
                target="export",
                expected=False,
            ),
            _snapshot(),
            True,
        ),
        (
            _criterion(
                "non-empty",
                AcceptanceKind.ARTIFACT,
                "non_empty",
                target="export",
                expected=False,
            ),
            _snapshot(),
            True,
        ),
        (
            _criterion(
                "format",
                AcceptanceKind.ARTIFACT,
                "format",
                target="export",
                expected="fcstd",
            ),
            _snapshot(),
            "step",
        ),
    ],
)
def test_known_mismatches_fail_and_never_issue_receipts(
    criterion: AcceptanceCriterion, snapshot: ObservationSnapshot, observed: object
) -> None:
    result = _verify(_compiled(criterion), snapshot)
    verdict = result.report.verdicts[0]
    assert verdict.outcome is CriterionOutcome.FAIL
    assert verdict.observed == observed
    assert result.report.passed is False
    assert result.receipt is None


@pytest.mark.parametrize(
    "criterion",
    [
        _volume(expected=99.499, tolerance=0.5),
        _criterion(
            "area",
            AcceptanceKind.GEOMETRY,
            "area",
            target="body",
            expected=70.251,
            tolerance=0.25,
            parameters={"unit": "mm^2"},
        ),
        _criterion(
            "bbox",
            AcceptanceKind.GEOMETRY,
            "bbox",
            target="body",
            expected=(10.0, 4.899, 2.0),
            tolerance=0.1,
            parameters={"unit": "mm"},
        ),
        _criterion(
            "center",
            AcceptanceKind.GEOMETRY,
            "center_of_mass",
            target="body",
            expected=(5.0, 2.5, 0.899),
            tolerance=0.1,
            parameters={"unit": "mm"},
        ),
    ],
)
def test_every_numeric_check_fails_just_beyond_its_tolerance(
    criterion: AcceptanceCriterion,
) -> None:
    result = _verify(_compiled(criterion))
    assert result.report.verdicts[0].outcome is CriterionOutcome.FAIL
    assert result.receipt is None


@pytest.mark.parametrize(
    ("criterion", "snapshot"),
    [
        (_volume(target="missing"), _snapshot()),
        (_volume(), _snapshot(shapes=(_shape(volume_mm3=None),))),
        (
            _criterion(
                "area",
                AcceptanceKind.GEOMETRY,
                "area",
                target="body",
                expected=70.0,
                parameters={"unit": "mm^2"},
            ),
            _snapshot(shapes=(_shape(area_mm2=None),)),
        ),
        (
            _criterion(
                "bbox",
                AcceptanceKind.GEOMETRY,
                "bbox",
                target="body",
                expected=(10.0, 5.0, 2.0),
                parameters={"unit": "mm"},
            ),
            _snapshot(shapes=(_shape(bbox_mm=None),)),
        ),
        (
            _criterion(
                "center",
                AcceptanceKind.GEOMETRY,
                "center_of_mass",
                target="body",
                expected=(5.0, 2.5, 1.0),
                parameters={"unit": "mm"},
            ),
            _snapshot(shapes=(_shape(center_of_mass_mm=None),)),
        ),
        (
            _criterion(
                "valid", AcceptanceKind.TOPOLOGY, "valid_shape", target="body", expected=True
            ),
            _snapshot(shapes=(_shape(valid_shape=None),)),
        ),
        (
            _criterion("solids", AcceptanceKind.TOPOLOGY, "solid_count", target="body", expected=1),
            _snapshot(shapes=(_shape(solid_count=None),)),
        ),
        (
            _criterion(
                "exists", AcceptanceKind.ARTIFACT, "exists", target="missing", expected=True
            ),
            _snapshot(),
        ),
        (
            _criterion(
                "non-empty",
                AcceptanceKind.ARTIFACT,
                "non_empty",
                target="export",
                expected=True,
            ),
            _snapshot(artifacts=(_artifact(non_empty=None),)),
        ),
        (
            _criterion(
                "format", AcceptanceKind.ARTIFACT, "format", target="export", expected="step"
            ),
            _snapshot(artifacts=(ArtifactObservation(target="export", exists=False),)),
        ),
    ],
)
def test_required_missing_targets_or_facts_are_unsupported_and_fail(
    criterion: AcceptanceCriterion, snapshot: ObservationSnapshot
) -> None:
    result = _verify(_compiled(criterion), snapshot)
    verdict = result.report.verdicts[0]
    assert verdict.outcome is CriterionOutcome.UNSUPPORTED
    assert verdict.evidence == ()
    assert result.report.passed is False
    assert result.receipt is None


def test_optional_missing_fact_is_diagnostic_while_known_optional_mismatch_stays_fail() -> None:
    optional_missing = _volume(criterion_id="optional-missing", target="missing", required=False)
    optional_mismatch = _criterion(
        "optional-format",
        AcceptanceKind.ARTIFACT,
        "format",
        target="export",
        expected="fcstd",
        required=False,
    )
    result = _verify(_compiled(_volume(), optional_missing, optional_mismatch))
    assert [item.outcome for item in result.report.verdicts] == [
        CriterionOutcome.PASS,
        CriterionOutcome.UNSUPPORTED,
        CriterionOutcome.FAIL,
    ]
    assert result.report.passed is True
    assert type(result.receipt) is VerificationReceipt


def test_report_is_deterministic_but_each_success_receipt_has_distinct_identity() -> None:
    compiled = _compiled()
    snapshot = _snapshot()
    first = _verify(compiled, snapshot)
    second = _verify(compiled, snapshot)
    assert first.report == second.report
    assert first.report.to_mapping() == second.report.to_mapping()
    assert first.receipt is not second.receipt
    assert type(first.receipt) is VerificationReceipt
    assert type(second.receipt) is VerificationReceipt

    changed_manifest = _verify(compiled, snapshot, manifest=OTHER_MANIFEST)
    assert changed_manifest.report.id != first.report.id
    changed_snapshot = _snapshot(shapes=(_shape(volume_mm3=100.25),))
    changed = _verify(compiled, changed_snapshot)
    assert changed.report.id != first.report.id
    assert changed.report.observation_digest != first.report.observation_digest

    changed_expected = _verify(_compiled(_volume(expected=99.9)), snapshot)
    changed_tolerance = _verify(_compiled(_volume(tolerance=0.5)), snapshot)
    changed_required = _verify(_compiled(_volume(required=False)), snapshot)
    area = _criterion(
        "area",
        AcceptanceKind.GEOMETRY,
        "area",
        target="body",
        expected=70.0,
        parameters={"unit": "mm^2"},
    )
    forward = _verify(_compiled(_volume(), area), snapshot)
    reversed_order = _verify(_compiled(area, _volume()), snapshot)
    assert (
        len(
            {
                first.report.id,
                changed_expected.report.id,
                changed_tolerance.report.id,
                changed_required.report.id,
                forward.report.id,
                reversed_order.report.id,
            }
        )
        == 6
    )


def test_verify_rejects_binding_mismatches_bad_manifests_and_non_snapshots() -> None:
    compiled = _compiled()
    snapshot = _snapshot()
    with pytest.raises(ValidationError) as caught:
        _verify(compiled, snapshot, revision=OTHER_REVISION)
    _assert_error(caught, ValidationErrorCode.BINDING_MISMATCH)
    with pytest.raises(ValidationError) as caught:
        _verify(compiled, snapshot, manifest="ABC")
    _assert_error(caught, ValidationErrorCode.INVALID_VALUE)

    class SnapshotSubclass(ObservationSnapshot):
        pass

    class SnapshotProxy:
        candidate_revision = snapshot.candidate_revision
        shapes = snapshot.shapes
        artifacts = snapshot.artifacts
        observation_digest = snapshot.observation_digest

    subclass = SnapshotSubclass(
        candidate_revision=REVISION,
        shapes=snapshot.shapes,
        artifacts=snapshot.artifacts,
    )
    for impostor in (
        snapshot.to_mapping(),
        MappingProxyType(snapshot.to_mapping()),
        subclass,
        SnapshotProxy(),
        object(),
    ):
        with pytest.raises(ValidationError) as caught:
            _verify(compiled, impostor)
        _assert_error(caught, ValidationErrorCode.INVALID_TYPE)


def test_verify_revalidates_exact_snapshot_integrity_after_object_level_tampering() -> None:
    compiled = _compiled()
    digest_tampered = _snapshot()
    object.__setattr__(digest_tampered, "observation_digest", "0" * 64)
    with pytest.raises(ValidationError) as caught:
        _verify(compiled, digest_tampered)
    _assert_error(caught, ValidationErrorCode.BINDING_MISMATCH)

    fact_tampered = _snapshot()
    object.__setattr__(fact_tampered.shapes[0], "volume_mm3", 99.0)
    with pytest.raises(ValidationError) as caught:
        _verify(compiled, fact_tampered)
    _assert_error(caught, ValidationErrorCode.BINDING_MISMATCH)

    incomplete = object.__new__(ObservationSnapshot)
    with pytest.raises(ValidationError) as caught:
        _verify(compiled, incomplete)
    _assert_error(caught, ValidationErrorCode.INVALID_VALUE)


def test_snapshot_rejects_observation_subclasses() -> None:
    class ShapeSubclass(ShapeObservation):
        pass

    class ArtifactSubclass(ArtifactObservation):
        pass

    shape = ShapeSubclass(
        target="body",
        volume_mm3=100.0,
        area_mm2=70.0,
        bbox_mm=(10.0, 5.0, 2.0),
        center_of_mass_mm=(5.0, 2.5, 1.0),
        valid_shape=True,
        solid_count=1,
    )
    artifact = ArtifactSubclass(**_artifact().to_mapping())
    with pytest.raises(ValidationError) as caught:
        ObservationSnapshot(candidate_revision=REVISION, shapes=(shape,), artifacts=())
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/shapes/0"
    with pytest.raises(ValidationError) as caught:
        ObservationSnapshot(candidate_revision=REVISION, shapes=(), artifacts=(artifact,))
    error = _assert_error(caught, ValidationErrorCode.INVALID_TYPE)
    assert error.path == "/artifacts/0"


def test_step_results_and_execution_acknowledgements_cannot_satisfy_acceptance() -> None:
    evidence = ExecutionEvidence(
        id="evidence-1",
        kind=EvidenceKind.ASSERTION,
        name="execution_acknowledged",
        value=True,
    )
    result = StepResult(
        ok=True,
        value={"volume": 100.0},
        elapsed_ms=1,
        revision=REVISION,
        facts={"volume_mm3": 100.0, "valid_shape": True},
        evidence=(evidence,),
    )
    compiled = _compiled()
    for impostor in (result, result.to_mapping(), result.facts, result.evidence):
        with pytest.raises(ValidationError) as caught:
            _verify(compiled, impostor)
        _assert_error(caught, ValidationErrorCode.INVALID_TYPE)


def test_receipt_is_opaque_bound_and_consumed_exactly_once() -> None:
    compiled = _compiled()
    snapshot = _snapshot()
    result = _verify(compiled, snapshot)
    receipt = result.receipt
    assert type(receipt) is VerificationReceipt
    assert repr(receipt) == "VerificationReceipt(<opaque>)"
    with pytest.raises(TypeError):
        VerificationReceipt()
    with pytest.raises(TypeError):
        type("DerivedReceipt", (VerificationReceipt,), {})
    with pytest.raises(TypeError):
        receipt.anything = True  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        copy.copy(receipt)
    with pytest.raises(TypeError):
        copy.deepcopy(receipt)
    with pytest.raises((TypeError, pickle.PicklingError)):
        pickle.dumps(receipt)
    with pytest.raises(TypeError):
        receipt.__reduce__()
    with pytest.raises(TypeError):
        receipt.__reduce_ex__(5)
    with pytest.raises(TypeError):
        dataclasses.replace(receipt)
    with pytest.raises(TypeError):
        vars(receipt)
    assert not hasattr(receipt, "to_mapping")
    assert not hasattr(VerificationReceipt, "from_mapping")

    duplicate_compiled = _compiled()
    changed_compiled = _compiled(_volume(expected=99.9))
    assert changed_compiled.acceptance_id == compiled.acceptance_id
    assert changed_compiled.spec_digest != compiled.spec_digest
    for args in (
        (duplicate_compiled, snapshot, REVISION, MANIFEST),
        (changed_compiled, snapshot, REVISION, MANIFEST),
        (compiled, snapshot, OTHER_REVISION, MANIFEST),
        (compiled, snapshot, REVISION, OTHER_MANIFEST),
        (
            compiled,
            _snapshot(shapes=(_shape(volume_mm3=99.9),)),
            REVISION,
            MANIFEST,
        ),
    ):
        wrong_compiled, wrong_snapshot, wrong_revision, wrong_manifest = args
        with pytest.raises(ValidationError) as caught:
            consume_verification_receipt(
                receipt,
                wrong_compiled,
                wrong_snapshot,
                candidate_revision=wrong_revision,
                manifest_sha256=wrong_manifest,
            )
        _assert_error(caught, ValidationErrorCode.BINDING_MISMATCH)

    consumed = consume_verification_receipt(
        receipt,
        compiled,
        snapshot,
        candidate_revision=REVISION,
        manifest_sha256=MANIFEST,
    )
    assert consumed == result.report
    assert consumed is not result.report
    with pytest.raises(ValidationError) as caught:
        consume_verification_receipt(
            receipt,
            compiled,
            snapshot,
            candidate_revision=REVISION,
            manifest_sha256=MANIFEST,
        )
    _assert_error(caught, ValidationErrorCode.REPLAYED_RECEIPT)


def test_receipt_rejects_slot_tampering_and_equal_report_substitution() -> None:
    compiled = _compiled()
    snapshot = _snapshot()
    first = _verify(compiled, snapshot)
    second = _verify(compiled, snapshot)
    assert first.report == second.report
    assert first.report is not second.report
    assert type(first.receipt) is VerificationReceipt
    assert type(second.receipt) is VerificationReceipt

    slot_names = {
        slot
        for base in VerificationReceipt.__mro__
        for slot in getattr(base, "__slots__", ())
        if slot != "__weakref__"
    }
    assert slot_names == {"_seal"}
    seal = object.__getattribute__(first.receipt, "_seal")
    forged = object.__new__(VerificationReceipt)
    object.__setattr__(forged, "_seal", seal)
    with pytest.raises(ValidationError) as caught:
        consume_verification_receipt(
            forged,
            compiled,
            snapshot,
            candidate_revision=REVISION,
            manifest_sha256=MANIFEST,
        )
    _assert_error(caught, ValidationErrorCode.FORGED_CAPABILITY)

    object.__setattr__(first.receipt, "_seal", object())
    with pytest.raises(ValidationError) as caught:
        consume_verification_receipt(
            first.receipt,
            compiled,
            snapshot,
            candidate_revision=REVISION,
            manifest_sha256=MANIFEST,
        )
    _assert_error(caught, ValidationErrorCode.FORGED_CAPABILITY)

    consumed = consume_verification_receipt(
        second.receipt,
        compiled,
        snapshot,
        candidate_revision=REVISION,
        manifest_sha256=MANIFEST,
    )
    assert consumed == second.report
    assert consumed is not second.report


def test_consumed_report_is_an_issuer_owned_copy_not_the_public_alias() -> None:
    compiled = _compiled()
    snapshot = _snapshot()
    result = _verify(compiled, snapshot)
    assert type(result.receipt) is VerificationReceipt
    consumed = consume_verification_receipt(
        result.receipt,
        compiled,
        snapshot,
        candidate_revision=REVISION,
        manifest_sha256=MANIFEST,
    )
    canonical = consumed.to_mapping()
    assert consumed == result.report
    assert consumed is not result.report
    assert consumed.verdicts[0] is not result.report.verdicts[0]

    object.__setattr__(result.report, "id", "verification_ffffffffffffffffffffffffffffffff")
    object.__setattr__(result.report.verdicts[0], "outcome", CriterionOutcome.FAIL)
    assert consumed.to_mapping() == canonical
    assert consumed.id != result.report.id
    assert consumed.verdicts[0].outcome is CriterionOutcome.PASS


def test_receipt_binds_the_entire_immutable_report_value() -> None:
    def mutate_id(report: VerificationReport) -> None:
        object.__setattr__(report, "id", "verification_ffffffffffffffffffffffffffffffff")

    def mutate_verdict(report: VerificationReport) -> None:
        object.__setattr__(report.verdicts[0], "outcome", CriterionOutcome.FAIL)

    def mutate_verdict_tuple(report: VerificationReport) -> None:
        object.__setattr__(report, "verdicts", ())

    for mutation in (mutate_id, mutate_verdict, mutate_verdict_tuple):
        compiled = _compiled()
        snapshot = _snapshot()
        result = _verify(compiled, snapshot)
        assert type(result.receipt) is VerificationReceipt
        mutation(result.report)
        with pytest.raises(ValidationError) as caught:
            consume_verification_receipt(
                result.receipt,
                compiled,
                snapshot,
                candidate_revision=REVISION,
                manifest_sha256=MANIFEST,
            )
        _assert_error(caught, ValidationErrorCode.BINDING_MISMATCH)


def test_object_new_forgery_and_cross_capability_use_fail_closed() -> None:
    forged_compiled = object.__new__(CompiledAcceptance)
    with pytest.raises(ValidationError) as caught:
        _verify(forged_compiled)
    _assert_error(caught, ValidationErrorCode.FORGED_CAPABILITY)

    compiled = _compiled()
    snapshot = _snapshot()
    _verify(compiled, snapshot)
    forged_receipt = object.__new__(VerificationReceipt)
    with pytest.raises(ValidationError) as caught:
        consume_verification_receipt(
            forged_receipt,
            compiled,
            snapshot,
            candidate_revision=REVISION,
            manifest_sha256=MANIFEST,
        )
    _assert_error(caught, ValidationErrorCode.FORGED_CAPABILITY)


def test_receipt_consumption_is_atomic_under_concurrency() -> None:
    compiled = _compiled()
    snapshot = _snapshot()
    result = _verify(compiled, snapshot)
    receipt = result.receipt
    assert type(receipt) is VerificationReceipt
    barrier = threading.Barrier(8)
    outcomes: list[object] = []
    outcomes_lock = threading.Lock()

    def consume() -> None:
        try:
            barrier.wait(timeout=5)
            outcome: object = consume_verification_receipt(
                receipt,
                compiled,
                snapshot,
                candidate_revision=REVISION,
                manifest_sha256=MANIFEST,
            )
        except ValidationError as exc:
            outcome = exc.code
        except BaseException as exc:
            outcome = exc
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=consume) for _ in range(8)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
        assert not thread.is_alive()

    reports = [outcome for outcome in outcomes if type(outcome) is VerificationReport]
    assert len(reports) == 1
    assert reports[0] == result.report
    assert reports[0] is not result.report
    assert outcomes.count(ValidationErrorCode.REPLAYED_RECEIPT) == 7


def test_capability_registries_do_not_keep_dead_capabilities_alive() -> None:
    compiled = _compiled()
    compiled_ref = weakref.ref(compiled)
    del compiled
    gc.collect()
    assert compiled_ref() is None

    compiled = _compiled()
    snapshot = _snapshot()
    result = _verify(compiled, snapshot)
    receipt = result.receipt
    assert type(receipt) is VerificationReceipt
    consume_verification_receipt(
        receipt,
        compiled,
        snapshot,
        candidate_revision=REVISION,
        manifest_sha256=MANIFEST,
    )
    receipt_ref = weakref.ref(receipt)
    compiled_ref = weakref.ref(compiled)
    del result
    del receipt
    del compiled
    gc.collect()
    assert receipt_ref() is None
    assert compiled_ref() is None


def test_capability_registries_have_explicit_live_object_budgets_and_recover_capacity() -> None:
    compiled_values = [_compiled() for _ in range(256)]
    with pytest.raises(ValidationError) as caught:
        _compiled()
    error = _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)
    assert error.path == "/compiled"
    compiled_values.clear()
    gc.collect()
    recovered = _compiled()
    assert type(recovered) is CompiledAcceptance

    snapshot = _snapshot()
    receipts = []
    for _index in range(256):
        result = _verify(recovered, snapshot)
        assert type(result.receipt) is VerificationReceipt
        receipts.append(result.receipt)
    with pytest.raises(ValidationError) as caught:
        _verify(recovered, snapshot)
    error = _assert_error(caught, ValidationErrorCode.BUDGET_EXCEEDED)
    assert error.path == "/receipt"
    receipts.clear()
    del result
    gc.collect()
    assert type(_verify(recovered, snapshot).receipt) is VerificationReceipt


def test_failed_verification_has_no_receipt_to_authorize() -> None:
    result = _verify(_compiled(_volume(expected=99.0)))
    assert result.report.passed is False
    assert result.receipt is None
    with pytest.raises(ValidationError) as caught:
        consume_verification_receipt(
            None,  # type: ignore[arg-type]
            _compiled(),
            _snapshot(),
            candidate_revision=REVISION,
            manifest_sha256=MANIFEST,
        )
    _assert_error(caught, ValidationErrorCode.INVALID_TYPE)


def test_validation_error_mapping_is_strict_and_redacted() -> None:
    with pytest.raises(ValidationError) as caught:
        ValidationError.from_mapping(
            {
                "schema_version": 1,
                "code": ValidationErrorCode.INVALID_VALUE.value,
                "path": "/target",
                "message": "The value is invalid.",
                "attacker": "forged",
            }
        )
    error = _assert_error(caught, ValidationErrorCode.UNKNOWN_FIELD)
    assert error.path == "/attacker"

    reconstructed = ValidationError.from_mapping(
        {
            "schema_version": 1,
            "code": ValidationErrorCode.INVALID_VALUE.value,
            "path": "/target",
            "message": "The value is invalid.",
        }
    )
    assert type(reconstructed) is ValidationError
    assert reconstructed.code is ValidationErrorCode.INVALID_VALUE
    assert reconstructed.path == "/target"
    assert reconstructed.message == "The value is invalid."

    for hostile_message in (
        "SECRET\nFORGED-LOG-LINE",
        "S" * 257,
        "attacker-selected but printable",
    ):
        with pytest.raises(ValidationError) as caught:
            ValidationError.from_mapping(
                {
                    "schema_version": 1,
                    "code": ValidationErrorCode.INVALID_VALUE.value,
                    "path": "/target",
                    "message": hostile_message,
                }
            )
        error = _assert_error(caught, ValidationErrorCode.INVALID_VALUE)
        assert error.path == "/message"
        assert hostile_message not in error.message
        assert "SECRET" not in str(error)


def test_validation_modules_are_pure_closed_and_do_not_reference_execution_evidence() -> None:
    validation_root = Path(validation_package.__file__).parent
    source_paths = tuple(sorted(validation_root.glob("*.py")))
    assert {path.name for path in source_paths} == {
        "__init__.py",
        "checks.py",
        "contracts.py",
        "engine.py",
    }
    allowed_direct_imports = {
        "__init__.py": set(),
        "contracts.py": {"hashlib", "json", "math", "re", "threading", "weakref"},
        "checks.py": set(),
        "engine.py": {"hashlib"},
    }
    allowed_from_imports = {
        "__init__.py": {
            "vibecad.validation.contracts": {
                "ArtifactObservation",
                "CompiledAcceptance",
                "ObservationSnapshot",
                "ShapeObservation",
                "ValidationError",
                "ValidationErrorCode",
                "VerificationReceipt",
                "VerificationResult",
            },
            "vibecad.validation.engine": {
                "compile_acceptance_spec",
                "consume_verification_receipt",
                "verify_acceptance",
            },
        },
        "contracts.py": {
            "__future__": {"annotations"},
            "dataclasses": {"dataclass", "field"},
            "enum": {"StrEnum"},
            "typing": {"Any", "Self"},
            "vibecad.workflow.errors": {
                "MAX_SAFE_JSON_INTEGER",
                "SCHEMA_VERSION",
                "is_canonical_json_pointer",
                "join_json_pointer",
            },
            "vibecad.workflow.state": {"CriterionVerdict", "VerificationReport"},
        },
        "checks.py": {
            "__future__": {"annotations"},
            "dataclasses": {"dataclass"},
            "vibecad.validation.contracts": {
                "ArtifactObservation",
                "ObservationSnapshot",
                "ShapeObservation",
                "ValidationErrorCode",
                "_canonical_json_bytes",
                "_finite_number",
                "_raise_validation",
                "_validate_bounded_text",
            },
            "vibecad.workflow.contracts": {
                "AcceptanceCriterion",
                "AcceptanceKind",
                "AcceptanceSpec",
            },
            "vibecad.workflow.state": {"CriterionOutcome", "CriterionVerdict"},
        },
        "engine.py": {
            "__future__": {"annotations"},
            "vibecad.validation.checks": {
                "CompiledSpecData",
                "compile_spec",
                "evaluate_criterion",
            },
            "vibecad.validation.contracts": {
                "CompiledAcceptance",
                "ObservationSnapshot",
                "ValidationErrorCode",
                "VerificationReceipt",
                "VerificationResult",
                "_CompiledBinding",
                "_ReceiptBinding",
                "_canonical_json_bytes",
                "_consume_receipt",
                "_issue_compiled",
                "_issue_receipt",
                "_lookup_compiled",
                "_raise_validation",
                "_validate_digest",
                "_validate_revision",
                "_validated_snapshot",
            },
            "vibecad.workflow.contracts": {"AcceptanceSpec"},
            "vibecad.workflow.state": {"CriterionOutcome", "VerificationReport"},
        },
    }
    forbidden_names = {
        "ExecutionEvidence",
        "StepResult",
        "execution_acknowledged",
    }
    forbidden_calls = {
        "__import__",
        "callable",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "hasattr",
        "locals",
        "open",
        "setattr",
        "vars",
    }
    forbidden_attributes = {
        "__bases__",
        "__class__",
        "__code__",
        "__dict__",
        "__func__",
        "__globals__",
        "__mro__",
        "__subclasses__",
        "__traceback__",
    }

    def is_broad_exception(node: ast.expr | None) -> bool:
        if node is None:
            return True
        if isinstance(node, ast.Name):
            return node.id in {"BaseException", "Exception"}
        if isinstance(node, ast.Tuple):
            return any(is_broad_exception(item) for item in node.elts)
        return False

    for path in source_paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.asname is None for alias in node.names)
                assert {alias.name for alias in node.names} <= allowed_direct_imports[path.name]
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.level == 0
                assert node.module in allowed_from_imports[path.name]
                assert all(alias.asname is None and alias.name != "*" for alias in node.names)
                assert {alias.name for alias in node.names} <= allowed_from_imports[path.name][
                    node.module
                ]
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in forbidden_names
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_attributes
            if isinstance(node, ast.Call):
                assert not isinstance(node.func, (ast.Call, ast.Lambda, ast.Subscript))
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in forbidden_calls
                if isinstance(node.func, ast.Attribute) and node.func.attr.startswith("__"):
                    allowed_dunder_call = (
                        isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "object"
                        and node.func.attr in {"__delattr__", "__new__", "__setattr__"}
                    ) or (
                        isinstance(node.func.value, ast.Call)
                        and isinstance(node.func.value.func, ast.Name)
                        and node.func.value.func.id == "super"
                        and not node.func.value.args
                        and not node.func.value.keywords
                        and node.func.attr == "__init__"
                    )
                    assert allowed_dunder_call
            if isinstance(node, ast.ExceptHandler):
                assert not is_broad_exception(node.type)


def test_compile_and_verify_do_not_reach_ambient_dynamic_builtins(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("ambient dynamic capability was used")

    with monkeypatch.context() as patcher:
        patcher.setattr(builtins, "open", forbidden)
        patcher.setattr(builtins, "eval", forbidden)
        patcher.setattr(builtins, "exec", forbidden)
        patcher.setattr(builtins, "__import__", forbidden)
        compiled = _compiled()
        result = _verify(compiled)
    assert result.report.passed is True
