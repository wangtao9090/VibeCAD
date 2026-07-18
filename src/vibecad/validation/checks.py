"""Closed deterministic acceptance-check compilation and evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from vibecad.validation.contracts import (
    ArtifactObservation,
    ObservationSnapshot,
    ShapeObservation,
    ValidationErrorCode,
    _canonical_json_bytes,
    _finite_number,
    _raise_validation,
    _validate_bounded_text,
)
from vibecad.workflow.contracts import AcceptanceCriterion, AcceptanceKind, AcceptanceSpec
from vibecad.workflow.state import CriterionOutcome, CriterionVerdict

_MAX_CRITERIA = 128
_MAX_SPEC_BYTES = 256 * 1024
_FORMATS = frozenset({"fcstd", "step"})
_SUPPORTED = frozenset(
    {
        (AcceptanceKind.GEOMETRY, "volume"),
        (AcceptanceKind.GEOMETRY, "area"),
        (AcceptanceKind.GEOMETRY, "bbox"),
        (AcceptanceKind.GEOMETRY, "center_of_mass"),
        (AcceptanceKind.TOPOLOGY, "valid_shape"),
        (AcceptanceKind.TOPOLOGY, "solid_count"),
        (AcceptanceKind.ARTIFACT, "exists"),
        (AcceptanceKind.ARTIFACT, "non_empty"),
        (AcceptanceKind.ARTIFACT, "format"),
    }
)


@dataclass(frozen=True, slots=True)
class CompiledCriterion:
    criterion_id: str
    required: bool
    supported: bool
    family: str
    check: str
    target: str | None
    expected: object
    tolerance: int | float | None


@dataclass(frozen=True, slots=True)
class CompiledSpecData:
    acceptance_id: str
    criteria: tuple[CompiledCriterion, ...]
    canonical_bytes: bytes


def _criterion_path(index: int, field: str) -> str:
    return f"/criteria/{index}/{field}"


def _safe_contract_mapping(
    value: AcceptanceCriterion | AcceptanceSpec,
    path: str,
) -> dict[str, object]:
    failed = False
    try:
        mapping = value.to_mapping()
    except (AttributeError, TypeError, ValueError, RecursionError):
        failed = True
        mapping = None
    if failed or type(mapping) is not dict:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    return mapping


def _parameters(criterion: AcceptanceCriterion, index: int) -> dict[str, object]:
    mapping = _safe_contract_mapping(criterion, f"/criteria/{index}")
    parameters = mapping["parameters"]
    if type(parameters) is not dict:
        _raise_validation(
            ValidationErrorCode.INVALID_TYPE,
            _criterion_path(index, "parameters"),
        )
    if not all(type(key) is str for key in parameters):
        _raise_validation(
            ValidationErrorCode.INVALID_TYPE,
            _criterion_path(index, "parameters"),
        )
    return parameters


def _parameter_path(index: int, name: str) -> str:
    parent = _criterion_path(index, "parameters")
    if not name.isprintable() or len(name.splitlines()) != 1:
        return parent
    escaped = name.replace("~", "~0").replace("/", "~1")
    path = f"{parent}/{escaped}"
    return path if len(path) <= 256 else parent


def _unit_parameters(
    criterion: AcceptanceCriterion,
    index: int,
    expected_unit: str,
) -> None:
    parameters = _parameters(criterion, index)
    extras = sorted(key for key in parameters if key != "unit")
    if extras:
        _raise_validation(
            ValidationErrorCode.INVALID_VALUE,
            _parameter_path(index, extras[0]),
        )
    unit = parameters.get("unit")
    if type(unit) is not str or unit != expected_unit:
        _raise_validation(
            ValidationErrorCode.INVALID_UNIT,
            _criterion_path(index, "parameters/unit"),
        )


def _empty_parameters(criterion: AcceptanceCriterion, index: int) -> None:
    parameters = _parameters(criterion, index)
    if parameters:
        name = sorted(parameters)[0]
        _raise_validation(
            ValidationErrorCode.INVALID_VALUE,
            _parameter_path(index, name),
        )


def _numeric_tolerance(criterion: AcceptanceCriterion, index: int) -> int | float:
    if criterion.tolerance is None:
        return 0
    return _finite_number(
        criterion.tolerance,
        _criterion_path(index, "tolerance"),
        nonnegative=True,
        error_code=ValidationErrorCode.INVALID_TOLERANCE,
    )


def _forbid_tolerance(criterion: AcceptanceCriterion, index: int) -> None:
    if criterion.tolerance is not None:
        _raise_validation(
            ValidationErrorCode.INVALID_TOLERANCE,
            _criterion_path(index, "tolerance"),
        )


def _numeric_expected(
    criterion: AcceptanceCriterion,
    index: int,
    *,
    nonnegative: bool,
) -> int | float:
    return _finite_number(
        criterion.expected,
        _criterion_path(index, "expected"),
        nonnegative=nonnegative,
    )


def _vector_expected(
    criterion: AcceptanceCriterion,
    index: int,
    *,
    nonnegative: bool,
) -> tuple[int | float, ...]:
    path = _criterion_path(index, "expected")
    if type(criterion.expected) is not tuple:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    if len(criterion.expected) != 3:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    result: list[int | float] = []
    for component, value in enumerate(criterion.expected):
        result.append(
            _finite_number(
                value,
                f"{path}/{component}",
                nonnegative=nonnegative,
            )
        )
    return tuple(result)


def _compile_supported(
    criterion: AcceptanceCriterion,
    index: int,
) -> CompiledCriterion:
    target_path = _criterion_path(index, "target")
    if criterion.target is None:
        _raise_validation(ValidationErrorCode.AMBIGUOUS_TARGET, target_path)
    target = _validate_bounded_text(criterion.target, target_path)
    expected_path = _criterion_path(index, "expected")
    key = (criterion.kind, criterion.check)

    if key == (AcceptanceKind.GEOMETRY, "volume"):
        _unit_parameters(criterion, index, "mm^3")
        expected = _numeric_expected(criterion, index, nonnegative=True)
        tolerance = _numeric_tolerance(criterion, index)
        family = "shape"
    elif key == (AcceptanceKind.GEOMETRY, "area"):
        _unit_parameters(criterion, index, "mm^2")
        expected = _numeric_expected(criterion, index, nonnegative=True)
        tolerance = _numeric_tolerance(criterion, index)
        family = "shape"
    elif key == (AcceptanceKind.GEOMETRY, "bbox"):
        _unit_parameters(criterion, index, "mm")
        expected = _vector_expected(criterion, index, nonnegative=True)
        tolerance = _numeric_tolerance(criterion, index)
        family = "shape"
    elif key == (AcceptanceKind.GEOMETRY, "center_of_mass"):
        _unit_parameters(criterion, index, "mm")
        expected = _vector_expected(criterion, index, nonnegative=False)
        tolerance = _numeric_tolerance(criterion, index)
        family = "shape"
    elif key == (AcceptanceKind.TOPOLOGY, "valid_shape"):
        _empty_parameters(criterion, index)
        _forbid_tolerance(criterion, index)
        if type(criterion.expected) is not bool:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, expected_path)
        expected = criterion.expected
        tolerance = None
        family = "shape"
    elif key == (AcceptanceKind.TOPOLOGY, "solid_count"):
        _empty_parameters(criterion, index)
        _forbid_tolerance(criterion, index)
        if type(criterion.expected) is not int:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, expected_path)
        expected = _finite_number(criterion.expected, expected_path, nonnegative=True)
        tolerance = None
        family = "shape"
    elif key in {
        (AcceptanceKind.ARTIFACT, "exists"),
        (AcceptanceKind.ARTIFACT, "non_empty"),
    }:
        _empty_parameters(criterion, index)
        _forbid_tolerance(criterion, index)
        if type(criterion.expected) is not bool:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, expected_path)
        expected = criterion.expected
        tolerance = None
        family = "artifact"
    else:
        _empty_parameters(criterion, index)
        _forbid_tolerance(criterion, index)
        if type(criterion.expected) is not str:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, expected_path)
        if criterion.expected not in _FORMATS:
            _raise_validation(ValidationErrorCode.INVALID_VALUE, expected_path)
        expected = criterion.expected
        tolerance = None
        family = "artifact"

    return CompiledCriterion(
        criterion_id=criterion.id,
        required=criterion.required,
        supported=True,
        family=family,
        check=criterion.check,
        target=target,
        expected=expected,
        tolerance=tolerance,
    )


def compile_spec(spec: AcceptanceSpec) -> CompiledSpecData:
    """Validate and canonicalize one exact acceptance specification."""

    if type(spec) is not AcceptanceSpec:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, "/spec")
    if type(spec.schema_version) is not int:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, "/schema_version")
    if spec.schema_version != 1:
        _raise_validation(ValidationErrorCode.UNSUPPORTED_VERSION, "/schema_version")
    acceptance_id = _validate_bounded_text(spec.id, "/id")
    if type(spec.criteria) is not tuple:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, "/criteria")
    if not spec.criteria:
        _raise_validation(ValidationErrorCode.EMPTY_SPEC, "/criteria")
    if len(spec.criteria) > _MAX_CRITERIA:
        _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED, "/criteria")
    for index, criterion in enumerate(spec.criteria):
        if type(criterion) is not AcceptanceCriterion:
            _raise_validation(
                ValidationErrorCode.INVALID_TYPE,
                f"/criteria/{index}",
            )
        if type(criterion.schema_version) is not int:
            _raise_validation(
                ValidationErrorCode.INVALID_TYPE,
                _criterion_path(index, "schema_version"),
            )
        if criterion.schema_version != 1:
            _raise_validation(
                ValidationErrorCode.UNSUPPORTED_VERSION,
                _criterion_path(index, "schema_version"),
            )
    ids: list[str] = []
    for index, criterion in enumerate(spec.criteria):
        ids.append(_validate_bounded_text(criterion.id, _criterion_path(index, "id")))
    if len(ids) != len(set(ids)):
        _raise_validation(ValidationErrorCode.DUPLICATE_CRITERION, "/criteria")

    compiled: list[CompiledCriterion] = []
    supported_count = 0
    for index, criterion in enumerate(spec.criteria):
        if type(criterion.kind) is not AcceptanceKind:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, _criterion_path(index, "kind"))
        if type(criterion.check) is not str:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, _criterion_path(index, "check"))
        if type(criterion.required) is not bool:
            _raise_validation(ValidationErrorCode.INVALID_TYPE, _criterion_path(index, "required"))
        if (criterion.kind, criterion.check) not in _SUPPORTED:
            if criterion.required:
                _raise_validation(
                    ValidationErrorCode.UNSUPPORTED_CHECK,
                    _criterion_path(index, "check"),
                )
            compiled.append(
                CompiledCriterion(
                    criterion_id=criterion.id,
                    required=False,
                    supported=False,
                    family="unsupported",
                    check=criterion.check,
                    target=criterion.target,
                    expected=criterion.expected,
                    tolerance=None,
                )
            )
            continue
        compiled.append(_compile_supported(criterion, index))
        supported_count += 1
    if supported_count == 0:
        _raise_validation(ValidationErrorCode.EMPTY_SPEC, "/criteria")

    canonical = _canonical_json_bytes(_safe_contract_mapping(spec, "/spec"))
    if len(canonical) > _MAX_SPEC_BYTES:
        _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED)
    return CompiledSpecData(
        acceptance_id=acceptance_id,
        criteria=tuple(compiled),
        canonical_bytes=canonical,
    )


def _shape_value(shape: ShapeObservation, check: str) -> object:
    if check == "volume":
        return shape.volume_mm3
    if check == "area":
        return shape.area_mm2
    if check == "bbox":
        return shape.bbox_mm
    if check == "center_of_mass":
        return shape.center_of_mass_mm
    if check == "valid_shape":
        return shape.valid_shape
    return shape.solid_count


def _artifact_value(artifact: ArtifactObservation, check: str) -> object:
    if check == "exists":
        return artifact.exists
    if check == "non_empty":
        return artifact.non_empty
    return artifact.format


def _unsupported_verdict(criterion: CompiledCriterion, *, missing: bool) -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id=criterion.criterion_id,
        required=criterion.required,
        outcome=CriterionOutcome.UNSUPPORTED,
        expected=criterion.expected,
        observed=None,
        delta=None,
        tolerance=None,
        evidence=(),
        message=(
            "The required observation is unavailable."
            if missing
            else "The acceptance criterion is unsupported."
        ),
    )


def evaluate_criterion(
    criterion: CompiledCriterion,
    snapshot: ObservationSnapshot,
) -> CriterionVerdict:
    """Evaluate one compiled descriptor without dynamic dispatch."""

    if not criterion.supported:
        return _unsupported_verdict(criterion, missing=False)
    observed = None
    evidence = ""
    if criterion.family == "shape":
        for index, shape in enumerate(snapshot.shapes):
            if shape.target == criterion.target:
                observed = _shape_value(shape, criterion.check)
                evidence = f"/shapes/{index}/{_shape_field(criterion.check)}"
                break
    else:
        for index, artifact in enumerate(snapshot.artifacts):
            if artifact.target == criterion.target:
                observed = _artifact_value(artifact, criterion.check)
                evidence = f"/artifacts/{index}/{criterion.check}"
                break
    if observed is None:
        return _unsupported_verdict(criterion, missing=True)

    delta: int | float | tuple[int | float, ...] | None = None
    tolerance: int | float | tuple[int | float, ...] | None = None
    if criterion.check in {"volume", "area"}:
        assert type(observed) in {int, float}
        assert type(criterion.expected) in {int, float}
        assert criterion.tolerance is not None
        delta = observed - criterion.expected
        tolerance = criterion.tolerance
        passed = abs(delta) <= criterion.tolerance
    elif criterion.check in {"bbox", "center_of_mass"}:
        assert type(observed) is tuple
        assert type(criterion.expected) is tuple
        assert criterion.tolerance is not None
        delta = tuple(observed[index] - criterion.expected[index] for index in range(len(observed)))
        tolerance = (criterion.tolerance,) * len(observed)
        passed = all(abs(component) <= criterion.tolerance for component in delta)
    else:
        passed = observed == criterion.expected
    outcome = CriterionOutcome.PASS if passed else CriterionOutcome.FAIL
    return CriterionVerdict(
        criterion_id=criterion.criterion_id,
        required=criterion.required,
        outcome=outcome,
        expected=criterion.expected,
        observed=observed,
        delta=delta,
        tolerance=tolerance,
        evidence=(evidence,),
        message=(
            "The acceptance criterion passed." if passed else "The acceptance criterion failed."
        ),
    )


def _shape_field(check: str) -> str:
    if check == "volume":
        return "volume_mm3"
    if check == "area":
        return "area_mm2"
    if check == "bbox":
        return "bbox_mm"
    if check == "center_of_mass":
        return "center_of_mass_mm"
    return check
