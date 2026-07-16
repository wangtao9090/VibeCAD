"""Provider-neutral workflow contract tests."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from copy import deepcopy

import pytest

from vibecad.workflow.contracts import (
    AcceptanceCriterion,
    AcceptanceKind,
    AcceptanceSpec,
    ErrorCategory,
    EvidenceKind,
    ExecutionEvidence,
    Intent,
    IntentAssumption,
    IntentKind,
    ModelCommand,
    ModelProgram,
    StepError,
    StepResult,
    ValueSource,
)
from vibecad.workflow.errors import (
    MAX_SAFE_JSON_INTEGER,
    SCHEMA_VERSION,
    ContractErrorCode,
    ContractValidationError,
)


def _contracts():
    assumption = IntentAssumption(
        id="assumption-1",
        statement="Preserve the existing hole axis",
        source=ValueSource.MODEL,
    )
    intent = Intent(
        id="intent-1",
        task_type=IntentKind.MODIFY,
        goal="Increase the mounting-hole diameter",
        input_project="project.fcstd",
        artifacts=("photo-front",),
        requirements={"diameter": 8, "unit": "mm", "finish": ["deburr"]},
        allowed_assumptions=(assumption,),
        unresolved_questions=("Confirm material",),
    )
    criterion = AcceptanceCriterion(
        id="criterion-1",
        kind=AcceptanceKind.GEOMETRY,
        check="diameter",
        target="HoleFeature001",
        expected=8,
        tolerance=0.01,
        parameters={"unit": "mm"},
    )
    acceptance = AcceptanceSpec(id="acceptance-1", criteria=(criterion,))
    command = ModelCommand(
        id="op-1",
        op="modify_parameter",
        target={"object": "HoleFeature001"},
        args={"parameter": "Diameter", "value": 8, "unit": "mm"},
        preserve=("center", "axis", "depth"),
        source=ValueSource.USER,
        depends_on=("op-0",),
    )
    program = ModelProgram(
        task_id="task-123",
        base_revision="rev-004",
        operations=(command,),
        acceptance=acceptance,
    )
    evidence = ExecutionEvidence(
        id="evidence-1",
        kind=EvidenceKind.FACT,
        name="measured_diameter",
        value={"value": 8.0, "unit": "mm"},
        operation_id="op-1",
        metadata={"method": "measure_distance"},
    )
    error = StepError(
        category=ErrorCategory.GEOMETRY,
        code="diameter_mismatch",
        message="Measured diameter is outside tolerance",
        retryable=True,
        needs_input=False,
        related_objects=("HoleFeature001",),
        diagnostic_artifacts=("diagnostic.json",),
        operation_id="op-1",
        details={"measured": 7.8},
    )
    success = StepResult(
        ok=True,
        value={"object": "HoleFeature001"},
        elapsed_ms=12.5,
        operation_id="op-1",
        revision="candidate-005",
        facts={"diameter": 8.0},
        artifacts=("model.fcstd",),
        warnings=("Preview only",),
        evidence=(evidence,),
    )
    failure = StepResult(
        ok=False,
        value=None,
        elapsed_ms=8,
        operation_id="op-1",
        error=error,
    )
    return (
        intent,
        criterion,
        acceptance,
        command,
        program,
        evidence,
        error,
        success,
        failure,
        assumption,
    )


def _assert_plain(value):
    if value is None or type(value) in {bool, int, float, str}:
        return
    if type(value) is list:
        for item in value:
            _assert_plain(item)
        return
    assert type(value) is dict
    assert all(type(key) is str for key in value)
    for item in value.values():
        _assert_plain(item)


@pytest.mark.parametrize("contract", _contracts())
def test_every_contract_round_trips_through_a_plain_mapping(contract):
    encoded = contract.to_mapping()

    _assert_plain(encoded)
    assert type(contract).from_mapping(encoded) == contract


def test_model_program_mapping_uses_the_architecture_vocabulary():
    program = _contracts()[4]

    encoded = program.to_mapping()

    assert set(encoded) == {
        "schema_version",
        "task_id",
        "base_revision",
        "operations",
        "acceptance",
    }
    assert set(encoded["operations"][0]) == {
        "schema_version",
        "id",
        "op",
        "target",
        "args",
        "preserve",
        "source",
        "depends_on",
    }


def test_contracts_copy_and_deep_freeze_caller_owned_values():
    requirements = {"dimensions": [10, {"unit": "mm"}]}
    intent = Intent(
        id="intent-1",
        task_type=IntentKind.CREATE,
        goal="Create a plate",
        requirements=requirements,
    )
    requirements["dimensions"][1]["unit"] = "inch"

    assert intent.to_mapping()["requirements"]["dimensions"][1]["unit"] == "mm"
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.goal = "mutated"
    with pytest.raises(TypeError):
        intent.requirements["new"] = "value"
    with pytest.raises(TypeError):
        intent.requirements["dimensions"][1]["unit"] = "inch"


@pytest.mark.parametrize("contract", _contracts())
def test_every_contract_rejects_an_unsupported_schema_version(contract):
    encoded = contract.to_mapping()
    encoded["schema_version"] = 2

    with pytest.raises(ContractValidationError) as caught:
        type(contract).from_mapping(encoded)

    assert caught.value.code is ContractErrorCode.UNSUPPORTED_VERSION
    assert caught.value.path == "/schema_version"


def test_huge_schema_versions_are_rejected_without_rendering_the_integer():
    huge = 10**5000

    intent = _contracts()[0].to_mapping()
    intent["schema_version"] = huge
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(intent)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == "/schema_version"
    assert caught.value.message == "schema_version is outside the safe integer range"

    error = ContractValidationError(
        ContractErrorCode.INVALID_VALUE, "/value", "bad value"
    ).to_mapping()
    error["schema_version"] = huge
    with pytest.raises(ContractValidationError) as caught:
        ContractValidationError.from_mapping(error)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == "/schema_version"
    assert caught.value.message == "schema_version is outside the safe integer range"

    with pytest.raises(ValueError) as caught:
        ContractValidationError(
            ContractErrorCode.INVALID_VALUE,
            "/value",
            "bad value",
            schema_version=huge,
        )
    assert str(caught.value) == "schema_version is outside the safe integer range"


def test_bool_is_not_accepted_as_an_integer_or_number():
    encoded = _contracts()[0].to_mapping()
    encoded["schema_version"] = True
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)
    assert caught.value.code is ContractErrorCode.INVALID_TYPE

    criterion = _contracts()[1].to_mapping()
    criterion["tolerance"] = False
    with pytest.raises(ContractValidationError) as caught:
        AcceptanceCriterion.from_mapping(criterion)
    assert caught.value.code is ContractErrorCode.INVALID_TYPE

    error = _contracts()[6].to_mapping()
    error["needs_input"] = 1
    with pytest.raises(ContractValidationError) as caught:
        StepError.from_mapping(error)
    assert caught.value.code is ContractErrorCode.INVALID_TYPE


@pytest.mark.parametrize(
    ("contract_type", "encoded", "field"),
    [
        (Intent, _contracts()[0].to_mapping(), "task_type"),
        (AcceptanceCriterion, _contracts()[1].to_mapping(), "kind"),
        (ModelCommand, _contracts()[3].to_mapping(), "source"),
        (ExecutionEvidence, _contracts()[5].to_mapping(), "kind"),
        (StepError, _contracts()[6].to_mapping(), "category"),
    ],
)
def test_invalid_enum_values_are_rejected(contract_type, encoded, field):
    encoded[field] = "not-a-supported-value"

    with pytest.raises(ContractValidationError) as caught:
        contract_type.from_mapping(encoded)

    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == f"/{field}"


def test_unknown_and_missing_fields_fail_closed_at_every_level():
    intent = _contracts()[0].to_mapping()
    intent["future_field"] = True
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(intent)
    assert caught.value.code is ContractErrorCode.UNKNOWN_FIELD
    assert caught.value.path == "/future_field"

    program = _contracts()[4].to_mapping()
    del program["task_id"]
    with pytest.raises(ContractValidationError) as caught:
        ModelProgram.from_mapping(program)
    assert caught.value.code is ContractErrorCode.MISSING_FIELD
    assert caught.value.path == "/task_id"

    nested = _contracts()[4].to_mapping()
    nested["operations"][0]["python"] = "import FreeCAD"
    with pytest.raises(ContractValidationError) as caught:
        ModelProgram.from_mapping(nested)
    assert caught.value.code is ContractErrorCode.UNKNOWN_FIELD
    assert caught.value.path == "/operations/0/python"

    nested_intent = _contracts()[0].to_mapping()
    nested_intent["allowed_assumptions"] = ["Preserve the axis"]
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(nested_intent)
    assert caught.value.code is ContractErrorCode.INVALID_TYPE
    assert caught.value.path == "/allowed_assumptions/0"


@pytest.mark.parametrize(
    ("contract_type", "encoded", "path"),
    [
        (AcceptanceSpec, {**_contracts()[2].to_mapping(), "criteria": [42]}, "/criteria/0"),
        (ModelProgram, {**_contracts()[4].to_mapping(), "operations": "op-1"}, "/operations"),
        (StepResult, {**_contracts()[7].to_mapping(), "evidence": ["fact"]}, "/evidence/0"),
    ],
)
def test_malformed_collections_and_nested_items_are_rejected(contract_type, encoded, path):
    with pytest.raises(ContractValidationError) as caught:
        contract_type.from_mapping(encoded)

    assert caught.value.code is ContractErrorCode.INVALID_TYPE
    assert caught.value.path == path


@pytest.mark.parametrize(
    ("contract_type", "encoded", "path"),
    [
        (Intent, {**_contracts()[0].to_mapping(), "id": "  "}, "/id"),
        (AcceptanceCriterion, {**_contracts()[1].to_mapping(), "id": ""}, "/id"),
        (AcceptanceSpec, {**_contracts()[2].to_mapping(), "id": "\t"}, "/id"),
        (ModelCommand, {**_contracts()[3].to_mapping(), "id": "\n"}, "/id"),
        (ModelProgram, {**_contracts()[4].to_mapping(), "task_id": ""}, "/task_id"),
    ],
)
def test_blank_identifiers_are_rejected(contract_type, encoded, path):
    with pytest.raises(ContractValidationError) as caught:
        contract_type.from_mapping(encoded)

    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == path


def test_json_payloads_reject_non_json_values_and_non_finite_numbers():
    encoded = _contracts()[0].to_mapping()
    encoded["requirements"] = {"unsafe": object()}
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)
    assert caught.value.path == "/requirements/unsafe"

    encoded["requirements"] = {"unsafe": float("nan")}
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE


def test_step_result_error_state_is_unambiguous():
    error = _contracts()[6]

    with pytest.raises(ContractValidationError, match="successful result"):
        StepResult(ok=True, value=None, elapsed_ms=1, error=error)
    with pytest.raises(ContractValidationError, match="failed result"):
        StepResult(ok=False, value=None, elapsed_ms=1)


def test_validation_error_has_a_stable_machine_readable_shape():
    encoded = _contracts()[0].to_mapping()
    encoded["schema_version"] = 99

    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)

    assert caught.value.to_mapping() == {
        "schema_version": SCHEMA_VERSION,
        "code": "unsupported_version",
        "path": "/schema_version",
        "message": "unsupported schema_version 99; expected 1",
    }


def test_validation_error_is_itself_a_versioned_strict_round_trip_contract():
    original = ContractValidationError(
        ContractErrorCode.INVALID_VALUE,
        "/requirements/a~1b~0c",
        "unsupported payload",
    )

    encoded = original.to_mapping()
    restored = ContractValidationError.from_mapping(encoded)

    assert encoded == {
        "schema_version": SCHEMA_VERSION,
        "code": "invalid_value",
        "path": "/requirements/a~1b~0c",
        "message": "unsupported payload",
    }
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.code is original.code
    assert restored.path == original.path
    assert restored.message == original.message
    assert ContractValidationError(ContractErrorCode.INVALID_TYPE, "", "root failure").path == ""


@pytest.mark.parametrize(
    ("mutate", "expected_code", "expected_path"),
    [
        (lambda item: item.update(future=True), ContractErrorCode.UNKNOWN_FIELD, "/future"),
        (lambda item: item.pop("code"), ContractErrorCode.MISSING_FIELD, "/code"),
        (
            lambda item: item.__setitem__("schema_version", 2),
            ContractErrorCode.UNSUPPORTED_VERSION,
            "/schema_version",
        ),
        (
            lambda item: item.__setitem__("schema_version", True),
            ContractErrorCode.INVALID_TYPE,
            "/schema_version",
        ),
        (
            lambda item: item.__setitem__("code", "future_code"),
            ContractErrorCode.INVALID_VALUE,
            "/code",
        ),
        (
            lambda item: item.__setitem__("code", 1),
            ContractErrorCode.INVALID_TYPE,
            "/code",
        ),
        (
            lambda item: item.__setitem__("path", "not-a-pointer"),
            ContractErrorCode.INVALID_VALUE,
            "/path",
        ),
        (
            lambda item: item.__setitem__("path", "/bad~2escape"),
            ContractErrorCode.INVALID_VALUE,
            "/path",
        ),
        (
            lambda item: item.__setitem__("path", 1),
            ContractErrorCode.INVALID_TYPE,
            "/path",
        ),
        (
            lambda item: item.__setitem__("message", 1),
            ContractErrorCode.INVALID_TYPE,
            "/message",
        ),
    ],
)
def test_validation_error_mapping_rejects_noncanonical_or_malformed_input(
    mutate, expected_code, expected_path
):
    encoded = ContractValidationError(
        ContractErrorCode.INVALID_VALUE, "/value", "bad value"
    ).to_mapping()
    mutate(encoded)

    with pytest.raises(ContractValidationError) as caught:
        ContractValidationError.from_mapping(encoded)

    assert caught.value.code is expected_code
    assert caught.value.path == expected_path


@pytest.mark.parametrize(
    ("key", "expected_path"),
    [
        ("", "/requirements/"),
        ("~", "/requirements/~0"),
        ("/", "/requirements/~1"),
        ("~1", "/requirements/~01"),
        ("a/b~c", "/requirements/a~1b~0c"),
    ],
)
def test_error_paths_are_collision_free_rfc6901_json_pointers(key, expected_path):
    encoded = _contracts()[0].to_mapping()
    encoded["requirements"] = {key: object()}

    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)

    assert caught.value.path == expected_path


def test_validation_error_string_quotes_and_escapes_control_characters():
    path = "/line\nbreak"
    message = "bad\r\nmessage\twith controls"

    rendered = str(ContractValidationError(ContractErrorCode.INVALID_VALUE, path, message))

    assert "\n" not in rendered
    assert "\r" not in rendered
    assert json.dumps(path) in rendered
    assert json.dumps(message) in rendered


def test_step_result_requires_and_canonically_emits_value_and_elapsed_ms():
    with pytest.raises(TypeError):
        StepResult(ok=True)  # type: ignore[call-arg]

    for field in ("value", "elapsed_ms"):
        encoded = _contracts()[7].to_mapping()
        del encoded[field]
        with pytest.raises(ContractValidationError) as caught:
            StepResult.from_mapping(encoded)
        assert caught.value.code is ContractErrorCode.MISSING_FIELD
        assert caught.value.path == f"/{field}"

    assert _contracts()[7].to_mapping()["value"] == {"object": "HoleFeature001"}
    assert _contracts()[7].to_mapping()["elapsed_ms"] == 12.5


def test_step_result_value_is_deeply_frozen_and_thawed():
    value = {"objects": [{"name": "Box"}]}
    result = StepResult(ok=True, value=value, elapsed_ms=0)
    value["objects"][0]["name"] = "Cylinder"

    assert result.to_mapping()["value"] == {"objects": [{"name": "Box"}]}
    with pytest.raises(TypeError):
        result.value["objects"][0]["name"] = "Cylinder"


@pytest.mark.parametrize("elapsed", [True, -1, float("nan"), float("inf"), float("-inf")])
def test_step_result_rejects_invalid_elapsed_ms(elapsed):
    encoded = _contracts()[7].to_mapping()
    encoded["elapsed_ms"] = elapsed

    with pytest.raises(ContractValidationError) as caught:
        StepResult.from_mapping(encoded)

    assert caught.value.path == "/elapsed_ms"
    assert caught.value.code in {ContractErrorCode.INVALID_TYPE, ContractErrorCode.INVALID_VALUE}


def test_step_error_requires_retry_and_diagnostic_policy_fields():
    with pytest.raises(TypeError):
        StepError(  # type: ignore[call-arg]
            category=ErrorCategory.RUNTIME,
            code="worker_failed",
            message="Worker failed",
        )

    for field in (
        "retryable",
        "needs_input",
        "related_objects",
        "diagnostic_artifacts",
    ):
        encoded = _contracts()[6].to_mapping()
        del encoded[field]
        with pytest.raises(ContractValidationError) as caught:
            StepError.from_mapping(encoded)
        assert caught.value.code is ContractErrorCode.MISSING_FIELD
        assert caught.value.path == f"/{field}"


def test_step_error_diagnostic_lists_are_strict_nonblank_string_lists():
    encoded = _contracts()[6].to_mapping()
    encoded["related_objects"] = "HoleFeature001"
    with pytest.raises(ContractValidationError) as caught:
        StepError.from_mapping(encoded)
    assert caught.value.code is ContractErrorCode.INVALID_TYPE
    assert caught.value.path == "/related_objects"

    encoded = _contracts()[6].to_mapping()
    encoded["diagnostic_artifacts"] = [""]
    with pytest.raises(ContractValidationError) as caught:
        StepError.from_mapping(encoded)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == "/diagnostic_artifacts/0"


@pytest.mark.parametrize("value", [MAX_SAFE_JSON_INTEGER, -MAX_SAFE_JSON_INTEGER])
def test_signed_ieee754_safe_integer_boundaries_are_accepted_everywhere(value):
    encoded = _contracts()[0].to_mapping()
    encoded["requirements"] = {"value": value}
    assert Intent.from_mapping(encoded).to_mapping()["requirements"] == {"value": value}

    criterion = _contracts()[1].to_mapping()
    criterion["tolerance"] = abs(value)
    assert AcceptanceCriterion.from_mapping(criterion).tolerance == abs(value)


@pytest.mark.parametrize("value", [MAX_SAFE_JSON_INTEGER + 1, -(MAX_SAFE_JSON_INTEGER + 1)])
def test_integers_outside_the_signed_ieee754_safe_range_are_rejected(value):
    encoded = _contracts()[0].to_mapping()
    encoded["requirements"] = {"value": value}
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == "/requirements/value"

    criterion = _contracts()[1].to_mapping()
    criterion["tolerance"] = abs(value)
    with pytest.raises(ContractValidationError) as caught:
        AcceptanceCriterion.from_mapping(criterion)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == "/tolerance"


def _nested_json(container_depth):
    value = 0
    for _ in range(container_depth - 1):
        value = [value]
    return {"root": value}


def test_json_container_depth_64_is_allowed_but_65_is_rejected():
    encoded = _contracts()[0].to_mapping()
    encoded["requirements"] = _nested_json(64)
    assert Intent.from_mapping(encoded).to_mapping()["requirements"] == _nested_json(64)

    encoded["requirements"] = _nested_json(65)
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path.startswith("/requirements/root/")


def test_json_cycle_is_rejected_but_shared_sibling_aliases_are_allowed():
    cycle = {}
    cycle["self"] = cycle
    encoded = _contracts()[0].to_mapping()
    encoded["requirements"] = {"cycle": cycle}
    with pytest.raises(ContractValidationError) as caught:
        Intent.from_mapping(encoded)
    assert caught.value.code is ContractErrorCode.INVALID_VALUE
    assert caught.value.path == "/requirements/cycle/self"

    shared = {"dimensions": [10, 20]}
    encoded["requirements"] = {"left": shared, "right": shared}
    parsed = Intent.from_mapping(encoded)
    assert parsed.to_mapping()["requirements"] == {
        "left": {"dimensions": [10, 20]},
        "right": {"dimensions": [10, 20]},
    }


def test_workflow_contracts_import_without_cad_mcp_or_model_sdks():
    code = """
import sys
import vibecad.workflow
banned = {'FreeCAD', 'Part', 'mcp', 'anthropic', 'openai'}
loaded = sorted(name for name in banned if name in sys.modules)
assert not loaded, loaded
print('workflow import boundary OK')
"""

    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "workflow import boundary OK" in result.stdout


def test_from_mapping_does_not_mutate_the_input():
    encoded = _contracts()[4].to_mapping()
    before = deepcopy(encoded)

    ModelProgram.from_mapping(encoded)

    assert encoded == before
