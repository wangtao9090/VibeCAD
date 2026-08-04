"""Tests for the domain-neutral runtime value contracts."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from collections.abc import Mapping

import pytest

import vibecad.runtime.contracts as runtime_contracts
from vibecad.runtime.contracts import (
    RuntimeArtifact,
    RuntimeBudget,
    RuntimeCapability,
    RuntimeControlPort,
    RuntimeDescriptor,
    RuntimeDiagnostic,
    RuntimeEvidence,
    RuntimeIdentity,
    RuntimeInvocation,
    RuntimeLifecycleState,
    RuntimeProvenance,
    RuntimeResult,
    RuntimeResultPort,
    RuntimeStatus,
)


class _HostileMapping(Mapping):
    def __getitem__(self, key):
        raise RuntimeError("hostile mapping read")

    def __iter__(self):
        raise RuntimeError("hostile mapping iteration")

    def __len__(self):
        return 0


def _identity(
    *,
    family: str = "cad",
    provider: str = "freecad",
    version: str = "1.1.0",
) -> RuntimeIdentity:
    return RuntimeIdentity(family=family, provider=provider, version=version)


def _capability(
    name: str = "authoring.create_box",
    *,
    version: int = 1,
) -> RuntimeCapability:
    return RuntimeCapability(name=name, version=version)


def _artifact(
    artifact_id: str = "native-model",
    *,
    runtime: RuntimeIdentity | None = None,
) -> RuntimeArtifact:
    return RuntimeArtifact(
        artifact_id=artifact_id,
        kind="native_model",
        media_type="application/x-freecad-document",
        digest="a" * 64,
        runtime=runtime or _identity(),
        metadata={"sealed": True, "dimensions": [10, 20, 30]},
    )


def test_runtime_identity_descriptor_and_capabilities_are_immutable_and_sorted():
    identity = _identity()
    capabilities = [
        _capability("inspection.measure"),
        _capability(),
    ]
    descriptor = RuntimeDescriptor(
        identity=identity,
        capabilities=capabilities,
        execution_profiles=["offscreen", "headless"],
        metadata={"engine": {"channel": "managed"}},
    )
    capabilities.append(_capability("authoring.fillet"))

    assert identity.key == "cad/freecad@1.1.0"
    assert tuple(item.name for item in descriptor.capabilities) == (
        "authoring.create_box",
        "inspection.measure",
    )
    assert descriptor.execution_profiles == ("headless", "offscreen")
    assert descriptor.metadata["engine"]["channel"] == "managed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.version = "2.0"
    with pytest.raises(TypeError):
        descriptor.metadata["engine"]["channel"] = "system"


def test_descriptor_rejects_duplicate_capability_and_profile_declarations():
    with pytest.raises(ValueError, match="duplicate runtime capability"):
        RuntimeDescriptor(
            identity=_identity(),
            capabilities=(_capability(), _capability()),
        )

    with pytest.raises(ValueError, match="duplicate execution profile"):
        RuntimeDescriptor(
            identity=_identity(),
            capabilities=(_capability(),),
            execution_profiles=("headless", "headless"),
        )


def test_execution_profiles_use_the_same_closed_name_grammar_as_capabilities():
    with pytest.raises(ValueError, match="execution_profiles"):
        RuntimeDescriptor(
            identity=_identity(),
            execution_profiles=("Headless GUI",),
        )

    with pytest.raises(ValueError, match="execution_profile"):
        RuntimeInvocation(
            invocation_id="invocation-001",
            owner_id="task-kernel",
            task_id="task-001",
            runtime=_identity(),
            capability=_capability(),
            budget=RuntimeBudget(
                max_elapsed_ms=1,
                max_memory_bytes=1,
                max_output_bytes=1,
            ),
            deadline_ms=1,
            execution_profile="Headless GUI",
        )


def test_invocation_seals_inputs_and_carries_owner_task_revision_budget_and_deadline():
    payload = {"operation": {"dimensions": [10, 20, 30]}}
    input_artifacts = [_artifact()]
    invocation = RuntimeInvocation(
        invocation_id="invocation-001",
        owner_id="task-kernel",
        task_id="task_0123456789abcdef0123456789abcdef",
        runtime=_identity(),
        capability=_capability(),
        input_revision="revision_0123456789abcdef0123456789abcdef",
        input_artifacts=input_artifacts,
        payload=payload,
        budget=RuntimeBudget(
            max_elapsed_ms=30_000,
            max_memory_bytes=512 * 1024 * 1024,
            max_output_bytes=64 * 1024,
        ),
        deadline_ms=1_900_000_000_000,
        execution_profile="headless",
    )
    payload["operation"]["dimensions"][0] = 999
    input_artifacts.clear()

    assert invocation.owner_id == "task-kernel"
    assert invocation.task_id.startswith("task_")
    assert invocation.input_revision.startswith("revision_")
    assert invocation.input_artifacts[0].artifact_id == "native-model"
    assert invocation.payload["operation"]["dimensions"] == (10, 20, 30)
    assert invocation.budget.max_elapsed_ms == 30_000
    assert invocation.deadline_ms == 1_900_000_000_000
    with pytest.raises(TypeError):
        invocation.payload["new"] = "value"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("max_elapsed_ms", 0, "max_elapsed_ms"),
        ("max_elapsed_ms", True, "max_elapsed_ms"),
        ("max_memory_bytes", -1, "max_memory_bytes"),
        ("max_output_bytes", 0, "max_output_bytes"),
    ),
)
def test_runtime_budget_rejects_non_positive_or_boolean_values(field, value, message):
    values = {
        "max_elapsed_ms": 1,
        "max_memory_bytes": 1,
        "max_output_bytes": 1,
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        RuntimeBudget(**values)


@pytest.mark.parametrize("deadline", (0, -1, True, 1.5))
def test_invocation_rejects_invalid_absolute_deadline(deadline):
    with pytest.raises((TypeError, ValueError), match="deadline_ms"):
        RuntimeInvocation(
            invocation_id="invocation-001",
            owner_id="task-kernel",
            task_id="task-001",
            runtime=_identity(),
            capability=_capability(),
            budget=RuntimeBudget(
                max_elapsed_ms=1,
                max_memory_bytes=1,
                max_output_bytes=1,
            ),
            deadline_ms=deadline,
        )


def test_result_artifacts_provenance_diagnostics_and_evidence_are_deeply_immutable():
    artifact_metadata = {"format": {"native": True}}
    diagnostic_details = {"limits": [1, 2]}
    evidence_value = {"measured": [10.0, 20.0]}
    artifact = dataclasses.replace(_artifact(), metadata=artifact_metadata)
    provenance = RuntimeProvenance(
        runtime=_identity(),
        invocation_id="invocation-001",
        input_artifact_ids=["source-model"],
        details={"adapter": {"name": "freecad-worker"}},
    )
    diagnostic = RuntimeDiagnostic(
        code="runtime.warning",
        message="A bounded warning.",
        retryable=False,
        details=diagnostic_details,
    )
    evidence = RuntimeEvidence(
        kind="measurement",
        name="bounding_box",
        value=evidence_value,
    )
    result = RuntimeResult(
        invocation_id="invocation-001",
        runtime=_identity(),
        state=RuntimeLifecycleState.SUCCEEDED,
        artifacts=[artifact],
        provenance=provenance,
        diagnostics=[diagnostic],
        evidence=[evidence],
        output={"object_ids": ["object-1"]},
    )
    artifact_metadata["format"]["native"] = False
    diagnostic_details["limits"][0] = 999
    evidence_value["measured"][0] = 999

    assert result.artifacts[0].metadata["format"]["native"] is True
    assert result.diagnostics[0].details["limits"] == (1, 2)
    assert result.evidence[0].value["measured"] == (10.0, 20.0)
    assert result.output["object_ids"] == ("object-1",)
    assert result.provenance.input_artifact_ids == ("source-model",)
    with pytest.raises(TypeError):
        result.output["object_ids"] = ()


def test_runtime_result_requires_terminal_state_and_matching_provenance():
    with pytest.raises(ValueError, match="terminal"):
        RuntimeResult(
            invocation_id="invocation-001",
            runtime=_identity(),
            state=RuntimeLifecycleState.RUNNING,
        )

    with pytest.raises(ValueError, match="provenance invocation_id"):
        RuntimeResult(
            invocation_id="invocation-001",
            runtime=_identity(),
            state=RuntimeLifecycleState.FAILED,
            provenance=RuntimeProvenance(
                runtime=_identity(),
                invocation_id="invocation-other",
            ),
        )


def test_runtime_result_requires_matching_provenance_when_artifacts_are_present():
    with pytest.raises(ValueError, match="provenance is required"):
        RuntimeResult(
            invocation_id="invocation-001",
            runtime=_identity(),
            state=RuntimeLifecycleState.SUCCEEDED,
            artifacts=(_artifact(),),
        )

    with pytest.raises(ValueError, match="provenance runtime"):
        RuntimeResult(
            invocation_id="invocation-001",
            runtime=_identity(),
            state=RuntimeLifecycleState.SUCCEEDED,
            artifacts=(_artifact(),),
            provenance=RuntimeProvenance(
                runtime=_identity(provider="other"),
                invocation_id="invocation-001",
            ),
        )

    with pytest.raises(ValueError, match="artifact runtime"):
        RuntimeResult(
            invocation_id="invocation-001",
            runtime=_identity(),
            state=RuntimeLifecycleState.SUCCEEDED,
            artifacts=(_artifact(runtime=_identity(provider="other")),),
            provenance=RuntimeProvenance(
                runtime=_identity(),
                invocation_id="invocation-001",
            ),
        )


def test_mapping_roots_are_explicitly_rejected_under_optimized_python():
    script = """
from vibecad.runtime.contracts import (
    RuntimeArtifact, RuntimeBudget, RuntimeCapability, RuntimeDescriptor,
    RuntimeDiagnostic, RuntimeIdentity, RuntimeInvocation, RuntimeLifecycleState,
    RuntimeProvenance, RuntimeResult,
)

identity = RuntimeIdentity(family="cad", provider="freecad", version="1.1")
capability = RuntimeCapability(name="execute", version=1)
budget = RuntimeBudget(max_elapsed_ms=1, max_memory_bytes=1, max_output_bytes=1)

constructors = (
    lambda: RuntimeDescriptor(identity=identity, metadata=[]),
    lambda: RuntimeArtifact(
        artifact_id="model", kind="native_model",
        media_type="application/octet-stream", digest="a" * 64,
        runtime=identity, metadata=[],
    ),
    lambda: RuntimeInvocation(
        invocation_id="invocation-1", owner_id="owner", task_id="task-1",
        runtime=identity, capability=capability, budget=budget, deadline_ms=1,
        payload=[],
    ),
    lambda: RuntimeDiagnostic(code="runtime.error", message="error", details=[]),
    lambda: RuntimeProvenance(
        runtime=identity, invocation_id="invocation-1", details=[],
    ),
    lambda: RuntimeResult(
        invocation_id="invocation-1", runtime=identity,
        state=RuntimeLifecycleState.FAILED, output=[],
    ),
)
for constructor in constructors:
    try:
        constructor()
    except TypeError:
        continue
    raise RuntimeError("non-mapping root was accepted")
print("optimized-root-rejection: PASS")
"""

    completed = subprocess.run(
        [sys.executable, "-O", "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "optimized-root-rejection: PASS"


def test_frozen_json_has_deterministic_width_node_and_utf8_byte_bounds():
    width = runtime_contracts._MAX_JSON_CONTAINER_ITEMS
    nodes = runtime_contracts._MAX_JSON_TOTAL_NODES
    string_bytes = runtime_contracts._MAX_JSON_STRING_BYTES

    with pytest.raises(ValueError, match="container items"):
        RuntimeEvidence(
            kind="fact",
            name="wide_sequence",
            value=[None] * (width + 1),
        )
    with pytest.raises(ValueError, match="container items"):
        RuntimeEvidence(
            kind="fact",
            name="wide_mapping",
            value={f"k{index}": None for index in range(width + 1)},
        )
    with pytest.raises(ValueError, match="total nodes"):
        RuntimeEvidence(
            kind="fact",
            name="many_nodes",
            value=[[None] * width for _ in range(nodes // width + 1)],
        )
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        RuntimeEvidence(
            kind="fact",
            name="huge_string",
            value="x" * 1_000_000,
        )
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        RuntimeEvidence(
            kind="fact",
            name="multibyte_string",
            value="界" * (string_bytes // 3 + 1),
        )
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        RuntimeEvidence(
            kind="fact",
            name="huge_key",
            value={"k" * (string_bytes + 1): None},
        )


def test_frozen_json_bounds_cumulative_logical_utf8_bytes_per_root():
    repeated = "x" * runtime_contracts._MAX_JSON_STRING_BYTES

    with pytest.raises(ValueError, match="cumulative UTF-8 bytes"):
        RuntimeEvidence(
            kind="fact",
            name="repeated_large_string",
            value=[repeated] * runtime_contracts._MAX_JSON_CONTAINER_ITEMS,
        )


def test_frozen_json_rejects_cycles_and_hostile_or_infinite_containers():
    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    cyclic_mapping: dict[str, object] = {}
    cyclic_mapping["self"] = cyclic_mapping

    for value in (
        cyclic_list,
        cyclic_mapping,
        _HostileMapping(),
    ):
        with pytest.raises(ValueError):
            RuntimeEvidence(kind="fact", name="hostile", value=value)


def test_frozen_json_stops_enumerating_an_infinite_sequence():
    script = """
from collections.abc import Sequence
from vibecad.runtime.contracts import RuntimeEvidence

class InfiniteSequence(Sequence):
    def __getitem__(self, index):
        return "item"

    def __len__(self):
        return 1

try:
    RuntimeEvidence(kind="fact", name="infinite", value=InfiniteSequence())
except ValueError:
    print("bounded-infinite-sequence: PASS")
else:
    raise RuntimeError("infinite sequence was accepted")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bounded-infinite-sequence: PASS"


@pytest.mark.parametrize("field", ("capabilities", "execution_profiles"))
def test_contract_collection_fields_stop_enumerating_an_infinite_sequence(field):
    script = f"""
from collections.abc import Sequence
from vibecad.runtime.contracts import (
    RuntimeCapability, RuntimeDescriptor, RuntimeIdentity,
)

class InfiniteSequence(Sequence):
    def __init__(self, value):
        self.value = value

    def __getitem__(self, index):
        return self.value

    def __len__(self):
        return 1

identity = RuntimeIdentity(family="cad", provider="freecad", version="1.1")
field = {field!r}
value = (
    RuntimeCapability(name="execute", version=1)
    if field == "capabilities"
    else "headless"
)
try:
    RuntimeDescriptor(identity=identity, **{{field: InfiniteSequence(value)}})
except ValueError:
    print("bounded-contract-sequence: PASS")
else:
    raise RuntimeError("infinite contract sequence was accepted")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bounded-contract-sequence: PASS"


def test_contract_collection_fields_have_one_explicit_finite_item_bound():
    capabilities = tuple(
        RuntimeCapability(name=f"capability.c{index}", version=1) for index in range(1_025)
    )
    profiles = tuple(f"profile_{index}" for index in range(1_025))

    with pytest.raises(ValueError, match="contract collection items"):
        RuntimeDescriptor(identity=_identity(), capabilities=capabilities)
    with pytest.raises(ValueError, match="contract collection items"):
        RuntimeDescriptor(identity=_identity(), execution_profiles=profiles)


def test_frozen_json_accepts_a_normal_nested_value_at_one_shared_root():
    evidence = RuntimeEvidence(
        kind="fact",
        name="normal",
        value={
            "dimensions": [10, 20, 30],
            "units": {"length": "毫米", "angle": "deg"},
            "valid": True,
        },
    )

    assert evidence.value["dimensions"] == (10, 20, 30)
    assert evidence.value["units"]["length"] == "毫米"


def test_lifecycle_control_is_an_abstract_structural_authority():
    calls: list[tuple[str, str]] = []
    health_state = runtime_contracts.RuntimeHealthState.HEALTHY

    class Control:
        def start(self, invocation: RuntimeInvocation) -> RuntimeStatus:
            calls.append(("start", invocation.invocation_id))
            return RuntimeStatus(
                invocation_id=invocation.invocation_id,
                runtime=invocation.runtime,
                state=RuntimeLifecycleState.PENDING,
            )

        def get_status(self, invocation_id: str) -> RuntimeStatus:
            calls.append(("status", invocation_id))
            return RuntimeStatus(
                invocation_id=invocation_id,
                runtime=_identity(),
                state=RuntimeLifecycleState.RUNNING,
            )

        def cancel(self, invocation_id: str, *, reason: str) -> RuntimeStatus:
            calls.append(("cancel", reason))
            return RuntimeStatus(
                invocation_id=invocation_id,
                runtime=_identity(),
                state=RuntimeLifecycleState.CANCELLED,
            )

        def reconcile(self, invocation_id: str) -> RuntimeStatus:
            calls.append(("reconcile", invocation_id))
            return self.get_status(invocation_id)

        def health(self, identity: RuntimeIdentity):
            calls.append(("health", identity.key))
            return runtime_contracts.RuntimeHealth(
                runtime=identity,
                state=health_state,
            )

    control: RuntimeControlPort = Control()
    invocation = RuntimeInvocation(
        invocation_id="invocation-001",
        owner_id="task-kernel",
        task_id="task-001",
        runtime=_identity(),
        capability=_capability(),
        budget=RuntimeBudget(
            max_elapsed_ms=1,
            max_memory_bytes=1,
            max_output_bytes=1,
        ),
        deadline_ms=1,
    )

    assert control.start(invocation).state is RuntimeLifecycleState.PENDING
    assert control.get_status("invocation-001").state is RuntimeLifecycleState.RUNNING
    assert control.cancel("invocation-001", reason="user-requested").state.is_terminal
    assert control.reconcile("invocation-001").invocation_id == "invocation-001"
    assert control.health(_identity()).state is health_state
    assert calls == [
        ("start", "invocation-001"),
        ("status", "invocation-001"),
        ("cancel", "user-requested"),
        ("reconcile", "invocation-001"),
        ("status", "invocation-001"),
        ("health", "cad/freecad@1.1.0"),
    ]


def test_result_retrieval_is_a_separate_structural_authority():
    expected = RuntimeResult(
        invocation_id="invocation-001",
        runtime=_identity(),
        state=RuntimeLifecycleState.SUCCEEDED,
        output={"proposal": "ready"},
    )

    class Results:
        def get_result(self, invocation_id: str) -> RuntimeResult | None:
            assert invocation_id == expected.invocation_id
            return expected

    results: RuntimeResultPort = Results()

    assert results.get_result("invocation-001") is expected

    class PendingResults:
        def get_result(self, invocation_id: str) -> RuntimeResult | None:
            assert invocation_id == "invocation-pending"
            return None

    pending: RuntimeResultPort = PendingResults()
    assert pending.get_result("invocation-pending") is None
