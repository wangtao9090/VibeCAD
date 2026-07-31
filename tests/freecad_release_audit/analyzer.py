"""Deterministic offline analyzer for one complete A16 v1 audit journal."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .journal import MAX_RUN_BYTES
from .registry import RULE_IDS, run_registered
from .rules import analyze_journal

_RULE_ID_SET = frozenset(RULE_IDS)


@dataclass(frozen=True, slots=True)
class Verdict:
    rule_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if any(rule_id not in _RULE_ID_SET for rule_id in self.rule_ids):
            raise ValueError("verdict contains an unknown rule ID")
        object.__setattr__(self, "rule_ids", tuple(sorted(set(self.rule_ids))))

    @property
    def ok(self) -> bool:
        return not self.rule_ids

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "verdict": "GO" if self.ok else "RED",
            "rule_ids": list(self.rule_ids),
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.as_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )


def analyze_bytes(raw: object) -> Verdict:
    try:
        document, journal_findings = analyze_journal(raw)
    except (KeyError, OverflowError, RecursionError, TypeError, ValueError):
        return Verdict(("A16-JRN-002",))
    if journal_findings or document is None:
        return Verdict(tuple(sorted(journal_findings)))
    try:
        return Verdict(run_registered(document))
    except (AttributeError, KeyError, OverflowError, RecursionError, TypeError, ValueError):
        return Verdict(("A16-JRN-001",))


def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 1:
        return 2
    try:
        path = Path(values[0])
        if path.stat().st_size > MAX_RUN_BYTES:
            raise ValueError("journal exceeds byte limit")
        with path.open("rb") as stream:
            raw = stream.read(MAX_RUN_BYTES + 1)
        if len(raw) > MAX_RUN_BYTES:
            raise ValueError("journal exceeds byte limit")
    except (OSError, ValueError):
        verdict = Verdict(("A16-JRN-002",))
    else:
        verdict = analyze_bytes(raw)
    sys.stdout.buffer.write(verdict.canonical_bytes())
    return 0 if verdict.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
