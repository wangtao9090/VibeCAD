"""IPC, identity, resource, timing, hash, artifact, and I/O rules."""

from __future__ import annotations

from .flow import EXPECTED_BINDINGS, event_bindings
from .journal import AuditDocument

IPC_001 = "A16-IPC-001"
IDN_001 = "A16-IDN-001"
RES_001 = "A16-RES-001"
RES_002 = "A16-RES-002"
TIM_001 = "A16-TIM-001"
HSH_001 = "A16-HSH-001"
ART_001 = "A16-ART-001"
IO_001 = "A16-IO-001"

CORE_IDENTITIES = (
    "daemon_id",
    "daemon_pid",
    "gui_pid",
    "project_id",
    "task_id",
    "draft_id",
    "revision_id",
    "candidate_revision",
)
EXPECTED_LEDGER = (
    (1, 1, 1, 0),
    (2, 2, 2, 0),
    (2, 2, 2, 0),
    (2, 2, 2, 0),
    (2, 2, 2, 0),
    (2, 2, 2, 0),
    (2, 2, 2, 0),
    (0, 0, 0, 0),
)
LEDGER_FIELDS = (
    "checkout_count",
    "grant_count",
    "document_count",
    "pending_request_count",
)
EVENT_DIGEST_FIELDS = frozenset(("request_sha256", "callback_sha256"))
FINAL_EVENT_DIGEST_FIELDS = EVENT_DIGEST_FIELDS | {
    "result_sha256",
    "screenshot_sha256",
}
EXPECTED_AUTHORITIES = (
    ("checkout_head", "grant_head"),
    ("checkout_draft", "grant_draft"),
)


def _core(record: dict[str, object]) -> tuple[object, ...]:
    identities = record["identities"]
    return tuple(identities.get(name) for name in CORE_IDENTITIES)


def check_evidence(document: AuditDocument) -> tuple[str, ...]:
    events = document.events
    terminal = document.terminal
    assert terminal is not None
    bindings = event_bindings(document)
    findings: set[str] = set()
    if terminal["dropped_frames"] != 0 or any(
        record["counts"].get("dropped_frame_count") != 0 for record in events
    ):
        findings.add(IPC_001)
    expected_identity = _core(terminal)
    if (
        None in expected_identity
        or expected_identity[6] == expected_identity[7]
        or terminal["identities"].keys() != set(CORE_IDENTITIES)
        or len(events) != len(EXPECTED_BINDINGS)
        or any(
            (binding.emitter, binding.role) != (expected.emitter, expected.role)
            or None in _core(record)
            or _core(record) != expected_identity
            for record, binding, expected in zip(events, bindings, EXPECTED_BINDINGS, strict=True)
        )
    ):
        findings.add(IDN_001)
    authority_pairs: list[tuple[object, object]] = []
    authority_invalid = len(events) != 8
    for index, record in enumerate(events):
        identities = record["identities"]
        correlation = record["correlation"]
        identity_pair = (identities.get("checkout_id"), identities.get("grant_id"))
        correlation_pair = (correlation.get("checkout_id"), correlation.get("grant_id"))
        if index < 2:
            authority_pairs.append(identity_pair)
            if None in identity_pair or identity_pair != correlation_pair:
                authority_invalid = True
        elif identity_pair != (None, None) or correlation_pair != (None, None):
            authority_invalid = True
    if (
        authority_invalid
        or tuple(authority_pairs) != EXPECTED_AUTHORITIES
        or len({item for pair in authority_pairs for item in pair}) != 4
    ):
        findings.add(RES_001)
    ledger = tuple(tuple(record["counts"].get(name) for name in LEDGER_FIELDS) for record in events)
    if ledger != EXPECTED_LEDGER or any(value != 0 for value in terminal["resources"].values()):
        findings.add(RES_002)
    terminal_ns = terminal["monotonic_ns"]
    elapsed_ns = terminal["elapsed_ns"]
    start_ns = terminal_ns - elapsed_ns
    if (
        terminal_ns > terminal["deadline_ns"]
        or start_ns < 0
        or not events
        or start_ns > events[0]["monotonic_ns"]
        or any(record["monotonic_ns"] > terminal_ns for record in events)
    ):
        findings.add(TIM_001)
    hashes = terminal["hashes"]
    if (
        hashes["product_before_sha256"] != hashes["product_after_sha256"]
        or hashes["runtime_before_sha256"] != hashes["runtime_after_sha256"]
    ):
        findings.add(HSH_001)
    expected_digest_fields = (EVENT_DIGEST_FIELDS,) * 7 + (FINAL_EVENT_DIGEST_FIELDS,)
    artifact_shape_invalid = len(events) != len(expected_digest_fields) or any(
        set(record["digests"]) - EVENT_DIGEST_FIELDS != expected - EVENT_DIGEST_FIELDS
        for record, expected in zip(events, expected_digest_fields, strict=True)
    )
    final_digests = events[-1]["digests"] if events else {}
    if (
        terminal["outcome"] != "success"
        or terminal["error_code"] is not None
        or terminal["planned_exits"] != {"daemon": 0, "gui": 0}
        or terminal["observed_exits"] != {"daemon": 0, "gui": 0}
        or any(value is None for value in terminal["digests"].values())
        or artifact_shape_invalid
        or final_digests.get("result_sha256") != terminal["digests"]["result_sha256"]
        or final_digests.get("screenshot_sha256") != terminal["digests"]["screenshot_sha256"]
    ):
        findings.add(ART_001)
    if terminal["truncated_logs"] != 0 or any(
        record["counts"].get("truncated_log_count") != 0 for record in events
    ):
        findings.add(IO_001)
    return tuple(sorted(findings))
