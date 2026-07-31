"""Serial, deterministic registry for all A16-AUD-2 semantic rules."""

from __future__ import annotations

from collections.abc import Callable

from .rules import AuditDocument, check_evidence, check_flow

RULE_IDS = (
    "A16-ART-001",
    "A16-COR-001",
    "A16-ERR-001",
    "A16-FLW-001",
    "A16-HSH-001",
    "A16-IDN-001",
    "A16-IO-001",
    "A16-IPC-001",
    "A16-JRN-001",
    "A16-JRN-002",
    "A16-JRN-003",
    "A16-RES-001",
    "A16-RES-002",
    "A16-SUC-001",
    "A16-TIM-001",
    "A16-TRM-001",
)
_RULE_ID_SET = frozenset(RULE_IDS)

Checker = Callable[[AuditDocument], tuple[str, ...]]
CHECKERS: tuple[Checker, ...] = (check_flow, check_evidence)


def run_registered(document: AuditDocument) -> tuple[str, ...]:
    findings: set[str] = set()
    for checker in CHECKERS:
        packet = checker(document)
        if type(packet) is not tuple or any(rule_id not in _RULE_ID_SET for rule_id in packet):
            return ("A16-JRN-001",)
        findings.update(packet)
    return tuple(sorted(findings))
