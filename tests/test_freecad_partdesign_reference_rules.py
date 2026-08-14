"""Focused codec and real-runtime gates for PartDesign reference rules."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from vibecad.parametric.freecad_partdesign_reference_rules import (
    MAX_REFERENCE_PLAN_BYTES,
    PartDesignReferenceKind,
    PartDesignReferencePlan,
    ReferenceExecutionBindings,
    ReferenceRuleError,
    ReferenceRuleErrorCode,
    decode_partdesign_reference_plan,
)


def _plan(
    kind: PartDesignReferenceKind = PartDesignReferenceKind.DATUM_PLANE,
    **changes: object,
) -> PartDesignReferencePlan:
    kind_slug = kind.value if type(kind) is PartDesignReferenceKind else str(kind)
    values = {
        "source_artifact_id": "artifact_pfg",
        "source_graph_id": "graph_reference",
        "source_graph_sha256": "1" * 64,
        "source_content_sha256": "2" * 64,
        "lowering_request_sha256": "3" * 64,
        "adapter_contract_sha256": "4" * 64,
        "body_id": "body_main",
        "node_id": f"node_{kind_slug}",
        "result_id": f"result_{kind_slug}",
        "support_reference_id": "reference_support",
        "support_reference_sha256": "5" * 64,
        "kind": kind,
    }
    values.update(changes)
    return PartDesignReferencePlan(**values)


def test_reference_family_roundtrips_canonically_without_execution_authority() -> None:
    digests = set()
    for kind in PartDesignReferenceKind:
        plan = _plan(kind)
        raw = plan.canonical_bytes
        decoded = decode_partdesign_reference_plan(
            raw,
            expected_content_sha256=hashlib.sha256(raw).hexdigest(),
            expected_plan_sha256=plan.plan_sha256,
        )
        assert decoded == plan
        assert decoded.executable is False
        assert decoded.grants_execution_authority is False
        assert json.loads(raw)["operation"] == {"kind": kind.value}
        assert len(raw) < MAX_REFERENCE_PLAN_BYTES
        digests.add(plan.plan_sha256)
    assert len(digests) == len(PartDesignReferenceKind)


def test_reference_plan_rejects_tamper_noncanonical_duplicate_and_unknown_kind() -> None:
    plan = _plan()
    raw = plan.canonical_bytes
    mapping = json.loads(raw)
    mapping["backend"]["engine_build_id"] = "f" * 64
    tampered = json.dumps(mapping, sort_keys=True, separators=(",", ":")).encode("ascii")
    noncanonical = json.dumps(json.loads(raw), indent=2, sort_keys=True).encode("ascii")
    duplicate = raw[:-1] + b',"authority":"none"}'
    unknown = json.loads(raw)
    unknown["operation"]["kind"] = "future_reference"
    unknown_raw = json.dumps(unknown, sort_keys=True, separators=(",", ":")).encode("ascii")

    for payload in (tampered, noncanonical, duplicate, unknown_raw):
        with pytest.raises(ReferenceRuleError) as error:
            decode_partdesign_reference_plan(payload)
        assert error.value.code is ReferenceRuleErrorCode.INTEGRITY_FAILURE
        assert len(str(error.value)) < 160

    with pytest.raises(ReferenceRuleError) as content_error:
        decode_partdesign_reference_plan(raw, expected_content_sha256="e" * 64)
    assert content_error.value.code is ReferenceRuleErrorCode.INTEGRITY_FAILURE
    with pytest.raises(ReferenceRuleError) as plan_error:
        decode_partdesign_reference_plan(raw, expected_plan_sha256="d" * 64)
    assert plan_error.value.code is ReferenceRuleErrorCode.INTEGRITY_FAILURE


def test_reference_plan_and_live_binding_identifiers_are_strictly_bounded() -> None:
    with pytest.raises(ReferenceRuleError):
        _plan(kind="datum_plane")
    with pytest.raises(ReferenceRuleError):
        _plan(node_id="../native")
    with pytest.raises(ReferenceRuleError):
        _plan(support_reference_sha256="not-a-digest")
    with pytest.raises(ReferenceRuleError):
        ReferenceExecutionBindings(
            document=object(),
            body=object(),
            support=object(),
            body_id="body_main",
            support_reference_id="reference_support",
            support_reference_sha256="5" * 64,
            support_subname="Face1\nInjected",
        )

    changed = dataclasses.replace(
        _plan(),
        support_reference_sha256="6" * 64,
    )
    assert changed.plan_sha256 != _plan().plan_sha256


def test_reference_plan_budget_is_checked_before_json_decode() -> None:
    with pytest.raises(ReferenceRuleError) as error:
        decode_partdesign_reference_plan(b"x" * (MAX_REFERENCE_PLAN_BYTES + 1))
    assert error.value.code is ReferenceRuleErrorCode.INVALID_INPUT


@pytest.mark.slow
def test_real_freecad_reference_family_create_edit_save_reopen_and_rollback(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIBECAD_RUN_INTEGRATION") != "1":
        pytest.skip("set VIBECAD_RUN_INTEGRATION=1 to run the real FreeCAD gate")
    from vibecad.runtime import paths as runtime_paths
    from vibecad.runtime import status as runtime_status

    runtime_python = runtime_paths.active_runtime_python()
    if not runtime_python.is_file() or not runtime_paths.ready_sentinel().is_file():
        pytest.fail("an existing ready managed FreeCAD runtime is required")
    if not runtime_status.engine_compatible(runtime_python):
        pytest.fail("the existing managed FreeCAD runtime does not match current engine pins")

    plans = {kind: _plan(kind) for kind in PartDesignReferenceKind}
    payload_paths: dict[PartDesignReferenceKind, Path] = {}
    for kind, plan in plans.items():
        path = tmp_path / f"{kind.value}.json"
        path.write_bytes(plan.canonical_bytes)
        payload_paths[kind] = path
    model_path = tmp_path / "references.FCStd"
    source_root = Path(__file__).parents[1] / "src"
    payload_mapping = {kind.value: str(path) for kind, path in payload_paths.items()}
    content_mapping = {
        kind.value: hashlib.sha256(plan.canonical_bytes).hexdigest() for kind, plan in plans.items()
    }
    plan_digest_mapping = {kind.value: plan.plan_sha256 for kind, plan in plans.items()}
    code = f"""
import os, sys
sys.path.insert(0, os.path.join(sys.prefix, 'lib'))
sys.path.insert(0, {str(source_root)!r})
from pathlib import Path
import FreeCAD, Part, PartDesign
from vibecad.parametric.freecad_partdesign_reference_rules import (
    PartDesignReferenceKind, ReferenceExecutionBindings, ReferenceRuleError,
    apply_partdesign_reference_plan,
)
payload_paths = {payload_mapping!r}
content_digests = {content_mapping!r}
plan_digests = {plan_digest_mapping!r}
document = FreeCAD.newDocument('ReferenceFamily')
document.UndoMode = 1
body = document.addObject('PartDesign::Body', 'Body')
base = body.newObject('PartDesign::Feature', 'Base')
base.Shape = Part.makeBox(20, 20, 10)
source_body = document.addObject('PartDesign::Body', 'SourceBody')
source = source_body.newObject('PartDesign::Feature', 'Source')
source.Shape = Part.makeCylinder(3, 10)
document.recompute()
initial_tip = body.Tip
subnames = {{
    'datum_plane': 'Face6',
    'datum_line': 'Edge10',
    'datum_point': 'Vertex7',
    'shape_binder': '',
    'subshape_binder': 'Face1',
}}
supports = {{
    'datum_plane': base,
    'datum_line': base,
    'datum_point': base,
    'shape_binder': source,
    'subshape_binder': source,
}}
receipts = {{}}
for kind_value in subnames:
    payload = Path(payload_paths[kind_value]).read_bytes()
    support = supports[kind_value]
    bindings = ReferenceExecutionBindings(
        document=document, body=body, support=support,
        body_id='body_main', support_reference_id='reference_support',
        support_reference_sha256={"5" * 64!r},
        support_subname=subnames[kind_value])
    receipt = apply_partdesign_reference_plan(
        payload, expected_content_sha256=content_digests[kind_value],
        expected_plan_sha256=plan_digests[kind_value], bindings=bindings)
    receipts[kind_value] = receipt
    result = document.getObject(receipt.object_name)
    assert result in body.Group and body.Tip is initial_tip
expected_types = {{
    'datum_plane': 'PartDesign::Plane',
    'datum_line': 'PartDesign::Line',
    'datum_point': 'PartDesign::Point',
    'shape_binder': 'PartDesign::ShapeBinder',
    'subshape_binder': 'PartDesign::SubShapeBinder',
}}
for key, receipt in receipts.items():
    assert document.getObject(receipt.object_name).TypeId == expected_types[key]
plane = document.getObject(receipts['datum_plane'].object_name)
binder = document.getObject(receipts['shape_binder'].object_name)
before_plane_z = float(plane.Placement.Base.z)
before_binder_volume = float(binder.Shape.Volume)
base.Shape = Part.makeBox(20, 20, 15)
source.Shape = Part.makeCylinder(3, 12)
document.recompute()
assert float(plane.Placement.Base.z) > before_plane_z + 4.9
assert float(binder.Shape.Volume) > before_binder_volume + 1.0
document.saveAs({str(model_path)!r})
names = {{key: receipt.object_name for key, receipt in receipts.items()}}
FreeCAD.closeDocument(document.Name)
reopened = FreeCAD.openDocument({str(model_path)!r})
reopened.recompute()
reopened_body = reopened.getObject('Body')
assert reopened_body.Tip.Name == 'Base'
for key, name in names.items():
    result = reopened.getObject(name)
    assert result.TypeId == expected_types[key] and result.isValid()
assert reopened.getObject(names['datum_plane']).AttachmentSupport[0][0].Name == 'Base'
assert tuple(reopened.getObject(names['datum_plane']).AttachmentSupport[0][1]) == ('Face6',)
assert reopened.getObject(names['shape_binder']).Support[0][0].Name == 'Source'
assert tuple(reopened.getObject(names['subshape_binder']).Support[0][1]) == ('Face1',)
assert abs(float(reopened.getObject(names['shape_binder']).Shape.Volume)
           - float(reopened.getObject('Source').Shape.Volume)) < 1e-7
FreeCAD.closeDocument(reopened.Name)

bad = FreeCAD.newDocument('ReferenceRollback')
bad.UndoMode = 1
bad_body = bad.addObject('PartDesign::Body', 'Body')
bad_base = bad_body.newObject('PartDesign::Feature', 'Base')
bad_base.Shape = Part.makeBox(5, 5, 5)
bad_source_body = bad.addObject('PartDesign::Body', 'SourceBody')
bad_source = bad_source_body.newObject('PartDesign::Feature', 'Source')
bad_source.Shape = Part.makeCylinder(3, 10)
bad.recompute()
before_names = tuple(item.Name for item in bad.Objects)
before_group = tuple(item.Name for item in bad_body.Group)
before_tip = bad_body.Tip
bad_payload = Path(payload_paths['datum_plane']).read_bytes()
bad_bindings = ReferenceExecutionBindings(
    document=bad, body=bad_body, support=bad_source,
    body_id='body_main', support_reference_id='reference_support',
    support_reference_sha256={"5" * 64!r}, support_subname='Face1')
try:
    apply_partdesign_reference_plan(
        bad_payload, expected_content_sha256=content_digests['datum_plane'],
        expected_plan_sha256=plan_digests['datum_plane'], bindings=bad_bindings)
except ReferenceRuleError:
    pass
else:
    raise AssertionError('curved FlatFace support must fail closed')
assert tuple(item.Name for item in bad.Objects) == before_names
assert tuple(item.Name for item in bad_body.Group) == before_group
assert bad_body.Tip is before_tip
FreeCAD.closeDocument(bad.Name)
print('REAL_REFERENCE_FAMILY_OK')
"""
    completed = subprocess.run(
        [str(runtime_python), "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert "REAL_REFERENCE_FAMILY_OK" in completed.stdout
