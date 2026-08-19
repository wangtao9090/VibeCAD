"""Focused authority, canonical-codec, and budget tests for the Groove rule."""

from __future__ import annotations

import dataclasses
import hashlib
import json

import pytest

from vibecad.parametric.freecad_partdesign_sketch_rules import (
    MAX_GROOVE_PLAN_BYTES,
    GrooveBackendPlan,
    GrooveRuleError,
    GrooveRuleErrorCode,
    decode_groove_backend_plan,
)


def _plan(**changes: object) -> GrooveBackendPlan:
    values = {
        "source_artifact_id": "artifact_pfg",
        "source_graph_id": "graph_main",
        "source_graph_sha256": "1" * 64,
        "source_content_sha256": "2" * 64,
        "lowering_request_sha256": "3" * 64,
        "adapter_contract_sha256": "4" * 64,
        "body_id": "body_main",
        "node_id": "node_groove",
        "result_id": "result_groove",
        "base_node_id": "node_base",
        "base_result_id": "result_base",
        "profile_node_id": "node_profile",
        "profile_result_id": "result_profile",
        "axis_reference_id": "reference_axis",
        "axis_result_id": "result_axis",
        "angle_degrees": 360.0,
        "reversed": False,
    }
    values.update(changes)
    return GrooveBackendPlan(**values)


def test_plan_is_canonical_content_bound_and_has_no_execution_authority() -> None:
    plan = _plan()
    raw = plan.canonical_bytes

    decoded = decode_groove_backend_plan(
        raw,
        expected_content_sha256=hashlib.sha256(raw).hexdigest(),
        expected_plan_sha256=plan.plan_sha256,
    )

    assert decoded == plan
    assert decoded.executable is False
    assert decoded.grants_execution_authority is False
    assert len(raw) < MAX_GROOVE_PLAN_BYTES
    assert json.loads(raw)["operation"] == {
        "allow_multi_face": False,
        "angle2_degrees": 0.0,
        "angle_degrees": 360.0,
        "axis_locator": "V_Axis",
        "midplane": False,
        "refine": True,
        "reversed": False,
        "type": "Angle",
    }


def test_plan_rejects_noncanonical_tamper_duplicate_keys_and_wrong_digests() -> None:
    plan = _plan()
    raw = plan.canonical_bytes
    noncanonical = json.dumps(json.loads(raw), indent=2, sort_keys=True).encode("ascii")
    duplicate = raw[:-1] + b',"authority":"none"}'

    for payload, code in (
        (noncanonical, GrooveRuleErrorCode.INTEGRITY_FAILURE),
        (duplicate, GrooveRuleErrorCode.INTEGRITY_FAILURE),
    ):
        with pytest.raises(GrooveRuleError) as error:
            decode_groove_backend_plan(payload)
        assert error.value.code is code
        assert len(str(error.value)) < 160

    with pytest.raises(GrooveRuleError) as content_error:
        decode_groove_backend_plan(raw, expected_content_sha256="f" * 64)
    assert content_error.value.code is GrooveRuleErrorCode.INTEGRITY_FAILURE
    with pytest.raises(GrooveRuleError) as plan_error:
        decode_groove_backend_plan(raw, expected_plan_sha256="e" * 64)
    assert plan_error.value.code is GrooveRuleErrorCode.INTEGRITY_FAILURE

    mapping = json.loads(raw)
    mapping["operation"]["type"] = "ThroughAll"
    through_all = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("ascii")
    with pytest.raises(GrooveRuleError) as mode_error:
        decode_groove_backend_plan(through_all)
    assert mode_error.value.code is GrooveRuleErrorCode.INTEGRITY_FAILURE


def test_plan_angle_reversed_and_native_contract_are_strictly_bounded() -> None:
    for angle in (0, -1, 360.0001, float("inf"), float("nan"), True):
        with pytest.raises(GrooveRuleError) as error:
            _plan(angle_degrees=angle)
        assert error.value.code is GrooveRuleErrorCode.INVALID_INPUT

    with pytest.raises(GrooveRuleError):
        _plan(reversed=1)
    with pytest.raises(GrooveRuleError):
        _plan(node_id="node_base")

    changed = dataclasses.replace(_plan(), angle_degrees=180.0, reversed=True)
    assert changed.plan_sha256 != _plan().plan_sha256
    assert decode_groove_backend_plan(changed.canonical_bytes) == changed


def test_plan_native_budget_is_checked_before_json_decode() -> None:
    with pytest.raises(GrooveRuleError) as error:
        decode_groove_backend_plan(b"x" * (MAX_GROOVE_PLAN_BYTES + 1))
    assert error.value.code is GrooveRuleErrorCode.INVALID_INPUT
