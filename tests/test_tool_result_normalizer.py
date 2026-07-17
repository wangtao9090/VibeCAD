from __future__ import annotations

import json
import sys
from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from vibecad.execution.results import (
    NormalizedToolOutcome,
    ToolDiagnosticClass,
    ToolResultCode,
    normalize_tool_exception,
    normalize_tool_result,
)
from vibecad.workflow import (
    ErrorCategory,
    EvidenceKind,
    ExecutionEvidence,
    StepResult,
)


class _ExplodingKeys(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError("getitem must not be reached")

    def __iter__(self) -> Iterator[str]:
        raise ValueError("private mapping-key detail")

    def __len__(self) -> int:
        return 1


class _ExplodingValue(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("private mapping-value detail")

    def __iter__(self) -> Iterator[str]:
        return iter(("ok",))

    def __len__(self) -> int:
        return 1


class _DuplicateKeys(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        return True

    def __iter__(self) -> Iterator[str]:
        return iter(("ok", "ok"))

    def __len__(self) -> int:
        return 2


class _InterruptingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise AssertionError("getitem must not be reached")

    def __iter__(self) -> Iterator[str]:
        raise KeyboardInterrupt

    def __len__(self) -> int:
        return 1


class _OpaqueError(Exception):
    def __str__(self) -> str:
        raise AssertionError("exception text must not be read")

    def __repr__(self) -> str:
        raise AssertionError("exception representation must not be read")


class _PrivateEvidence:
    def __repr__(self) -> str:
        return "private-evidence-sentinel"


class _McpLikeAttachment:
    def __repr__(self) -> str:
        return "private-mcp-attachment"


def _mapping(outcome: NormalizedToolOutcome) -> dict[str, Any]:
    return outcome.result.to_mapping()


def _assert_fixed_failure(
    outcome: NormalizedToolOutcome,
    code: ToolResultCode,
    diagnostic: ToolDiagnosticClass,
) -> None:
    assert outcome.result.ok is False
    assert outcome.result.value is None
    assert outcome.result.error is not None
    assert outcome.result.error.category is ErrorCategory.RUNTIME
    assert outcome.result.error.code == code.value
    assert outcome.diagnostic is diagnostic


@pytest.mark.parametrize(
    "raw",
    [None, True, 7, 2.5, "ready", [1, {"nested": [2, None]}]],
)
def test_normalize_json_scalar_and_list_success(raw: object) -> None:
    outcome = normalize_tool_result(raw, operation_id="inspect", elapsed_ms=1)

    assert outcome.result.ok is True
    assert outcome.result.to_mapping()["value"] == raw
    assert outcome.diagnostic is None


def test_success_mapping_strips_only_transport_fields() -> None:
    outcome = normalize_tool_result(
        {"ok": True, "error": None, "name": "Box", "volume": 12.5},
        operation_id="create_box",
        elapsed_ms=2,
    )

    assert outcome.result.to_mapping()["value"] == {"name": "Box", "volume": 12.5}


def test_legacy_mapping_without_transport_status_is_success() -> None:
    raw = {
        "valid": True,
        "parts": {"empty_part": {"error": "local geometry observation"}},
    }

    outcome = normalize_tool_result(raw, operation_id="inspect_model", elapsed_ms=3)

    assert outcome.result.ok is True
    assert outcome.result.to_mapping()["value"] == raw


def test_success_result_deeply_freezes_raw_and_injected_values() -> None:
    raw = {"ok": True, "shape": {"sizes": [1, 2]}}
    facts = {"bbox": {"x": [10, 20]}}
    artifacts = ["part.step"]
    warnings = ["approximate"]
    evidence_value = {"checks": ["solid"]}
    evidence = ExecutionEvidence(
        id="ev-1",
        kind=EvidenceKind.FACT,
        name="geometry",
        value=evidence_value,
        operation_id="create_box",
    )

    outcome = normalize_tool_result(
        raw,
        operation_id="create_box",
        elapsed_ms=4,
        revision="rev-2",
        facts=facts,
        artifacts=artifacts,
        warnings=warnings,
        evidence=[evidence],
    )
    before = _mapping(outcome)

    raw["shape"]["sizes"].append(3)  # type: ignore[index,union-attr]
    facts["bbox"]["x"].append(30)  # type: ignore[index,union-attr]
    artifacts.append("later.stl")
    warnings.append("later")
    evidence_value["checks"].append("mutated")

    assert _mapping(outcome) == before
    assert before["operation_id"] == "create_box"
    assert before["revision"] == "rev-2"
    assert before["artifacts"] == ["part.step"]
    assert before["warnings"] == ["approximate"]
    assert before["evidence"][0]["name"] == "geometry"  # type: ignore[index]


def test_public_result_round_trips_as_schema_v1_json() -> None:
    outcome = normalize_tool_result(
        {"ok": True, "value": [1, 2]},
        operation_id="inspect_model",
        elapsed_ms=1.25,
        facts={"solid_count": 1},
        artifacts=("model.step",),
        warnings=("draft",),
    )

    encoded = json.loads(json.dumps(_mapping(outcome), allow_nan=False))

    assert StepResult.from_mapping(encoded).to_mapping() == encoded


def test_contract_valid_context_is_not_given_a_new_normalizer_length_policy() -> None:
    operation_id = "operation-" + ("x" * 300)
    revision = "revision-" + ("y" * 300)

    outcome = normalize_tool_result(
        {"ok": True},
        operation_id=operation_id,
        revision=revision,
        elapsed_ms=1,
    )

    assert outcome.result.operation_id == operation_id
    assert outcome.result.revision == revision


def test_wrapper_is_immutable_and_has_no_public_serialization() -> None:
    outcome = normalize_tool_result(1, operation_id="inspect", elapsed_ms=0)

    with pytest.raises(FrozenInstanceError):
        outcome.diagnostic = ToolDiagnosticClass.INVALID_RESULT  # type: ignore[misc]
    assert not hasattr(outcome, "to_mapping")
    assert "diagnostic" not in _mapping(outcome)


def test_public_and_local_enums_are_closed_and_stable() -> None:
    assert {item.value for item in ToolResultCode} == {
        "tool_reported_error",
        "invalid_tool_result",
        "contradictory_tool_result",
        "unexpected_tool_exception",
    }
    assert {item.value for item in ToolDiagnosticClass} == {
        "reported_error",
        "invalid_result",
        "contradictory_result",
        "timeout_exception",
        "value_exception",
        "runtime_exception",
        "other_exception",
    }


def test_ok_false_uses_fixed_generic_failure_when_metadata_is_absent() -> None:
    outcome = normalize_tool_result(
        {"ok": False, "phase": "failed"},
        operation_id="create_box",
        elapsed_ms=5,
    )

    _assert_fixed_failure(
        outcome,
        ToolResultCode.TOOL_REPORTED_ERROR,
        ToolDiagnosticClass.REPORTED_ERROR,
    )
    assert outcome.result.error is not None
    assert outcome.result.error.message == "CAD tool reported an error."
    assert outcome.result.error.details == {}


def test_safe_top_level_failure_metadata_is_bounded_and_typed() -> None:
    raw = {
        "ok": False,
        "code": "geometry_conflict",
        "message": "The selected objects do not intersect.",
        "retryable": True,
        "needs_input": False,
        "related_objects": ["Body", "Tool"],
        "diagnostic_artifacts": ["diagnostics/report.json"],
        "phase": "ignored-legacy-extension",
    }

    outcome = normalize_tool_result(raw, operation_id="boolean_cut", elapsed_ms=6)

    error = outcome.result.error
    assert error is not None
    assert error.code == ToolResultCode.TOOL_REPORTED_ERROR.value
    assert error.message == "The selected objects do not intersect."
    assert error.retryable is True
    assert error.needs_input is False
    assert error.related_objects == ("Body", "Tool")
    assert error.diagnostic_artifacts == ("diagnostics/report.json",)
    assert error.details == {"tool_code": "geometry_conflict"}
    assert outcome.diagnostic is ToolDiagnosticClass.REPORTED_ERROR


def test_safe_nested_failure_metadata_is_accepted_without_mutation_aliasing() -> None:
    related = ["Box"]
    artifacts = ["failure.json"]
    nested = {
        "code": "invalid_geometry",
        "message": "Geometry validation failed.",
        "retryable": False,
        "needs_input": True,
        "related_objects": related,
        "diagnostic_artifacts": artifacts,
    }
    outcome = normalize_tool_result(
        {"ok": False, "error": nested},
        operation_id="create_box",
        elapsed_ms=7,
    )
    before = _mapping(outcome)

    related.append("Later")
    artifacts.append("later.log")
    nested["message"] = "mutated"

    assert _mapping(outcome) == before
    assert outcome.result.error is not None
    assert outcome.result.error.needs_input is True


def test_safe_error_string_is_a_reported_failure_message() -> None:
    outcome = normalize_tool_result(
        {"error": "Document is not available."},
        operation_id="inspect_model",
        elapsed_ms=1,
    )

    assert outcome.result.error is not None
    assert outcome.result.error.message == "Document is not available."
    assert outcome.diagnostic is ToolDiagnosticClass.REPORTED_ERROR


@pytest.mark.parametrize("error", [False, 0, "", [], {}, object()])
def test_every_non_null_top_level_error_is_failure(error: object) -> None:
    outcome = normalize_tool_result(
        {"error": error, "valid": True},
        operation_id="inspect_model",
        elapsed_ms=1,
    )

    _assert_fixed_failure(
        outcome,
        ToolResultCode.TOOL_REPORTED_ERROR,
        ToolDiagnosticClass.REPORTED_ERROR,
    )


def test_failure_status_precedes_success_looking_or_non_json_fields() -> None:
    outcome = normalize_tool_result(
        {"ok": False, "name": "Box", "value": object(), "volume": float("nan")},
        operation_id="create_box",
        elapsed_ms=1,
    )

    _assert_fixed_failure(
        outcome,
        ToolResultCode.TOOL_REPORTED_ERROR,
        ToolDiagnosticClass.REPORTED_ERROR,
    )


@pytest.mark.parametrize("error", ["private detail", "", False, 0, [], {}, object()])
def test_ok_true_with_non_null_error_is_contradictory(error: object) -> None:
    outcome = normalize_tool_result(
        {"ok": True, "error": error, "name": "Box"},
        operation_id="create_box",
        elapsed_ms=1,
    )

    _assert_fixed_failure(
        outcome,
        ToolResultCode.CONTRADICTORY_TOOL_RESULT,
        ToolDiagnosticClass.CONTRADICTORY_RESULT,
    )
    serialized = json.dumps(_mapping(outcome))
    assert "private detail" not in serialized
    assert "Box" not in serialized


@pytest.mark.parametrize("status", [None, 0, 1, 0.0, "true", [], {}])
def test_non_boolean_ok_is_invalid_even_when_an_error_is_present(status: object) -> None:
    outcome = normalize_tool_result(
        {"ok": status, "error": "must not be reflected"},
        operation_id="create_box",
        elapsed_ms=1,
    )

    _assert_fixed_failure(
        outcome,
        ToolResultCode.INVALID_TOOL_RESULT,
        ToolDiagnosticClass.INVALID_RESULT,
    )
    assert "must not be reflected" not in json.dumps(_mapping(outcome))


@pytest.mark.parametrize(
    "metadata",
    [
        {"code": "line\nbreak"},
        {"code": "x" * 65},
        {"code": 7},
        {"message": "line\nbreak"},
        {"message": "x" * 257},
        {"message": object()},
        {"retryable": 1},
        {"needs_input": 0},
        {"related_objects": "Box"},
        {"related_objects": ["Box", "bad\nreference"]},
        {"diagnostic_artifacts": ["x" * 257]},
    ],
)
def test_unsafe_top_level_failure_metadata_falls_back_without_reflection(
    metadata: dict[str, object],
) -> None:
    secret = next(iter(metadata.values()))
    outcome = normalize_tool_result(
        {"ok": False, **metadata},
        operation_id="create_box",
        elapsed_ms=1,
    )

    error = outcome.result.error
    assert error is not None
    assert error.code == ToolResultCode.TOOL_REPORTED_ERROR.value
    assert error.message == "CAD tool reported an error."
    assert error.retryable is False
    assert error.needs_input is False
    assert error.related_objects == ()
    assert error.diagnostic_artifacts == ()
    assert error.details == {}
    if type(secret) is str:
        assert secret not in json.dumps(_mapping(outcome))


def test_unknown_nested_error_fields_force_generic_metadata() -> None:
    outcome = normalize_tool_result(
        {
            "ok": False,
            "error": {
                "message": "otherwise safe",
                "private": "must not be reflected",
            },
        },
        operation_id="create_box",
        elapsed_ms=1,
    )

    assert outcome.result.error is not None
    assert outcome.result.error.message == "CAD tool reported an error."
    serialized = json.dumps(_mapping(outcome))
    assert "otherwise safe" not in serialized
    assert "must not be reflected" not in serialized


@pytest.mark.parametrize(
    "raw",
    [
        object(),
        {1: "non-string-key"},
        _ExplodingKeys(),
        _ExplodingValue(),
        _DuplicateKeys(),
    ],
)
def test_hostile_or_non_json_tool_values_become_redacted_invalid_results(raw: object) -> None:
    outcome = normalize_tool_result(raw, operation_id="inspect", elapsed_ms=1)

    _assert_fixed_failure(
        outcome,
        ToolResultCode.INVALID_TOOL_RESULT,
        ToolDiagnosticClass.INVALID_RESULT,
    )
    serialized = json.dumps(_mapping(outcome))
    assert "private" not in serialized
    assert "Exploding" not in serialized
    assert "non-string-key" not in serialized


def test_cyclic_tool_values_become_invalid_without_aliasing() -> None:
    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    cyclic_mapping: dict[str, object] = {}
    cyclic_mapping["self"] = cyclic_mapping

    for raw in (cyclic_list, cyclic_mapping):
        outcome = normalize_tool_result(raw, operation_id="inspect", elapsed_ms=1)
        _assert_fixed_failure(
            outcome,
            ToolResultCode.INVALID_TOOL_RESULT,
            ToolDiagnosticClass.INVALID_RESULT,
        )


@pytest.mark.parametrize(
    "raw",
    [float("nan"), float("inf"), float("-inf"), {"value": float("nan")}],
)
def test_non_finite_raw_numbers_are_invalid(raw: object) -> None:
    outcome = normalize_tool_result(raw, operation_id="inspect", elapsed_ms=1)

    _assert_fixed_failure(
        outcome,
        ToolResultCode.INVALID_TOOL_RESULT,
        ToolDiagnosticClass.INVALID_RESULT,
    )


@pytest.mark.parametrize(
    "elapsed",
    [True, -1, float("nan"), float("inf"), float("-inf"), "1", 9_007_199_254_740_992],
)
def test_invalid_elapsed_values_fail_closed_with_safe_zero(elapsed: object) -> None:
    outcome = normalize_tool_result(
        {"ok": True, "private": "must not survive"},
        operation_id="inspect",
        elapsed_ms=elapsed,  # type: ignore[arg-type]
    )

    _assert_fixed_failure(
        outcome,
        ToolResultCode.INVALID_TOOL_RESULT,
        ToolDiagnosticClass.INVALID_RESULT,
    )
    assert outcome.result.elapsed_ms == 0
    assert "must not survive" not in json.dumps(_mapping(outcome))


@pytest.mark.parametrize(
    ("kwargs", "secret"),
    [
        ({"facts": {"bad": float("nan")}}, "NaN"),
        ({"artifacts": [7]}, "7"),
        ({"warnings": [""]}, ""),
        ({"evidence": [_PrivateEvidence()]}, "private-evidence-sentinel"),
    ],
)
def test_invalid_injected_context_becomes_a_fixed_invalid_result(
    kwargs: dict[str, object],
    secret: str,
) -> None:
    outcome = normalize_tool_result(
        {"ok": True, "name": "Box"},
        operation_id="create_box",
        elapsed_ms=1,
        **kwargs,  # type: ignore[arg-type]
    )

    _assert_fixed_failure(
        outcome,
        ToolResultCode.INVALID_TOOL_RESULT,
        ToolDiagnosticClass.INVALID_RESULT,
    )
    assert outcome.result.facts == {}
    assert outcome.result.artifacts == ()
    assert outcome.result.warnings == ()
    assert outcome.result.evidence == ()
    if secret:
        assert secret not in json.dumps(_mapping(outcome), allow_nan=False)


def _r4_valid_context() -> dict[str, object]:
    evidence = ExecutionEvidence(
        id="ev-r4",
        kind=EvidenceKind.ARTIFACT,
        name="post-operation-model",
        value={"checks": ["shape-return"]},
        operation_id="operation-r4",
    )
    return {
        "operation_id": "operation-" + ("x" * 300),
        "elapsed_ms": 8,
        "revision": "revision-r4",
        "facts": {"bbox": {"x": [10, 20]}},
        "artifacts": ["model-r4.step"],
        "warnings": ["raw shape requires normalization"],
        "evidence": [evidence],
    }


@pytest.mark.parametrize(
    "case",
    ["non_json", "cyclic", "mcp_like"],
)
def test_r4_context_boundary_preserves_valid_context_for_invalid_raw(case: str) -> None:
    if case == "non_json":
        raw: object = object()
    elif case == "cyclic":
        cyclic: list[object] = []
        cyclic.append(cyclic)
        raw = cyclic
    else:
        raw = [{"ok": True, "name": "Box"}, _McpLikeAttachment()]
    context = _r4_valid_context()

    outcome = normalize_tool_result(raw, **context)  # type: ignore[arg-type]
    mapping = _mapping(outcome)

    _assert_fixed_failure(
        outcome,
        ToolResultCode.INVALID_TOOL_RESULT,
        ToolDiagnosticClass.INVALID_RESULT,
    )
    assert mapping["operation_id"] == context["operation_id"]
    assert mapping["revision"] == "revision-r4"
    assert mapping["facts"] == {"bbox": {"x": [10, 20]}}
    assert mapping["artifacts"] == ["model-r4.step"]
    assert mapping["warnings"] == ["raw shape requires normalization"]
    assert mapping["evidence"][0]["id"] == "ev-r4"  # type: ignore[index]
    assert "private-mcp-attachment" not in json.dumps(mapping)


@pytest.mark.parametrize(
    "case",
    [
        "artifacts_string",
        "artifacts_mapping",
        "artifacts_set",
        "warnings_string",
        "warnings_mapping",
        "warnings_set",
        "evidence_iterator",
    ],
)
def test_r4_context_boundary_rejects_non_list_tuple_context(case: str) -> None:
    context = _r4_valid_context()
    if case == "artifacts_string":
        context["artifacts"] = "private-context-sentinel"
    elif case == "artifacts_mapping":
        context["artifacts"] = {"private-context-sentinel": "not-a-reference"}
    elif case == "artifacts_set":
        context["artifacts"] = {"private-context-sentinel"}
    elif case == "warnings_string":
        context["warnings"] = "private-context-sentinel"
    elif case == "warnings_mapping":
        context["warnings"] = {"private-context-sentinel": "not-a-warning"}
    elif case == "warnings_set":
        context["warnings"] = {"private-context-sentinel"}
    else:
        context["evidence"] = iter(context["evidence"])  # type: ignore[arg-type]

    outcome = normalize_tool_result(
        {"ok": True, "name": "Box"},
        **context,  # type: ignore[arg-type]
    )
    mapping = _mapping(outcome)

    _assert_fixed_failure(
        outcome,
        ToolResultCode.INVALID_TOOL_RESULT,
        ToolDiagnosticClass.INVALID_RESULT,
    )
    assert mapping["operation_id"] == context["operation_id"]
    assert mapping["revision"] == "revision-r4"
    assert mapping["facts"] == {}
    assert mapping["artifacts"] == []
    assert mapping["warnings"] == []
    assert mapping["evidence"] == []
    assert "private-context-sentinel" not in json.dumps(mapping)


@pytest.mark.parametrize(
    ("exception", "diagnostic"),
    [
        (TimeoutError("private timeout"), ToolDiagnosticClass.TIMEOUT_EXCEPTION),
        (ValueError("private value"), ToolDiagnosticClass.VALUE_EXCEPTION),
        (RuntimeError("private runtime"), ToolDiagnosticClass.RUNTIME_EXCEPTION),
        (KeyError("private other"), ToolDiagnosticClass.OTHER_EXCEPTION),
        (_OpaqueError(), ToolDiagnosticClass.OTHER_EXCEPTION),
    ],
)
def test_exceptions_are_classified_locally_and_fully_redacted_publicly(
    exception: Exception,
    diagnostic: ToolDiagnosticClass,
) -> None:
    outcome = normalize_tool_exception(
        exception,
        operation_id="create_box",
        elapsed_ms=2,
        facts={"attempt": 1},
        artifacts=("trace-id",),
        warnings=("operation failed",),
    )

    _assert_fixed_failure(outcome, ToolResultCode.UNEXPECTED_TOOL_EXCEPTION, diagnostic)
    mapping = _mapping(outcome)
    serialized = json.dumps(mapping)
    assert mapping["facts"] == {"attempt": 1}
    assert mapping["artifacts"] == ["trace-id"]
    assert mapping["warnings"] == ["operation failed"]
    assert type(exception).__name__ not in serialized
    assert "private" not in serialized
    assert diagnostic.value not in serialized


@pytest.mark.parametrize("exception", [KeyboardInterrupt(), SystemExit(2)])
def test_base_exceptions_are_re_raised_not_normalized(exception: BaseException) -> None:
    with pytest.raises(type(exception)) as caught:
        normalize_tool_exception(  # type: ignore[arg-type]
            exception,
            operation_id="create_box",
            elapsed_ms=1,
        )

    assert caught.value is exception


def test_non_exception_argument_is_rejected_without_reflection() -> None:
    with pytest.raises(TypeError, match="exc must be an Exception"):
        normalize_tool_exception(  # type: ignore[arg-type]
            object(),
            operation_id="create_box",
            elapsed_ms=1,
        )


def test_hostile_mapping_base_exception_is_not_swallowed() -> None:
    with pytest.raises(KeyboardInterrupt):
        normalize_tool_result(_InterruptingMapping(), operation_id="inspect", elapsed_ms=1)


def test_normalization_has_no_logging_or_cad_runtime_import_side_effect(caplog: Any) -> None:
    names = (
        "FreeCAD",
        "Part",
        "vibecad.server",
        "vibecad.engine.session",
        "vibecad.tools.modeling",
    )
    before = {name: sys.modules.get(name) for name in names}

    normalize_tool_result({"ok": True, "name": "Box"}, operation_id="create_box", elapsed_ms=1)
    normalize_tool_exception(RuntimeError("private"), operation_id="create_box", elapsed_ms=1)

    assert not caplog.records
    assert {name: sys.modules.get(name) for name in names} == before
