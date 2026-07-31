from __future__ import annotations

import hashlib
import io
import json
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import analyzer as analyzer_module
from .analyzer import Verdict, analyze_bytes
from .journal import MAX_RUN_BYTES, AuditJournal, canonical_line
from .registry import RULE_IDS
from .rules import journal as journal_rules

FIXTURE_ROOT = Path(__file__).with_name("fixtures") / "v1"
PASS_PATH = FIXTURE_ROOT / "pass" / "full-flow.jsonl"
OVERLAY_FIELDS = {"schema_version", "type", "rule_id", "changes"}
CHANGE_FIELDS = {"action", "line", "path", "value"}
FIXTURE_RULES = {
    "evidence/art-001-missing-screenshot.jsonl": "A16-ART-001",
    "evidence/hsh-001-product-drift.jsonl": "A16-HSH-001",
    "evidence/idn-001-daemon-drift.jsonl": "A16-IDN-001",
    "evidence/io-001-truncated-log.jsonl": "A16-IO-001",
    "evidence/ipc-001-dropped-frame.jsonl": "A16-IPC-001",
    "evidence/res-001-duplicate-authority.jsonl": "A16-RES-001",
    "evidence/res-002-terminal-ledger.jsonl": "A16-RES-002",
    "evidence/tim-001-deadline.jsonl": "A16-TIM-001",
    "flow/cor-001-orphan-callback.jsonl": "A16-COR-001",
    "flow/err-001-late-error.jsonl": "A16-ERR-001",
    "flow/flw-001-phase-order.jsonl": "A16-FLW-001",
    "flow/suc-001-success-kind.jsonl": "A16-SUC-001",
    "journal/jrn-001-extra-field.jsonl": "A16-JRN-001",
    "journal/jrn-002-noncanonical.jsonl": "A16-JRN-002",
    "journal/jrn-003-sequence-gap.jsonl": "A16-JRN-003",
    "journal/trm-001-prefix.jsonl": "A16-TRM-001",
}


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _overlay_paths() -> tuple[Path, ...]:
    return tuple(sorted(path for path in FIXTURE_ROOT.rglob("*.jsonl") if path != PASS_PATH))


def _overlay(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    value = json.loads(raw)
    allowed = OVERLAY_FIELDS | {"reseal"}
    assert type(value) is dict and set(value) <= allowed and OVERLAY_FIELDS <= set(value)
    assert value["schema_version"] == 1 and value["type"] == "fixture_overlay"
    assert value["rule_id"] in RULE_IDS and _canonical(value) == raw
    assert type(value["changes"]) is list and value["changes"]
    for change in value["changes"]:
        assert type(change) is dict and set(change) == CHANGE_FIELDS
        assert change["action"] in {"set", "noncanonical"}
        assert type(change["line"]) is int and 0 <= change["line"] <= 8
        assert type(change["path"]) is list
        assert all(type(part) is str for part in change["path"])
    return value


def _assign(record: dict[str, object], path: list[str], value: object) -> None:
    target = record
    for part in path[:-1]:
        child = target[part]
        assert type(child) is dict
        target = child
    target[path[-1]] = value


def _records() -> list[dict[str, object]]:
    return [json.loads(line) for line in PASS_PATH.read_bytes().split(b"\n") if line]


def _seal(records: list[dict[str, object]]) -> bytes:
    lines = [canonical_line(record) for record in records]
    records[-1]["prefix_sha256"] = hashlib.sha256(b"".join(lines[:-1])).hexdigest()
    lines[-1] = canonical_line(records[-1])
    return b"".join(lines)


def build_negative(overlay: dict[str, object]) -> bytes:
    records = _records()
    noncanonical: set[int] = set()
    for change in overlay["changes"]:
        line = change["line"]
        if change["action"] == "noncanonical":
            noncanonical.add(line)
        else:
            _assign(records[line], change["path"], change["value"])
    lines = [canonical_line(record)[:-1] for record in records]
    for line in noncanonical:
        lines[line] = lines[line].replace(b'{"correlation":', b'{ "correlation":', 1)
    if overlay.get("reseal", True):
        records[-1]["prefix_sha256"] = hashlib.sha256(
            b"".join(line + b"\n" for line in lines[:-1])
        ).hexdigest()
        lines[-1] = canonical_line(records[-1])[:-1]
    return b"\n".join(lines) + b"\n"


def test_pass_fixture_is_go_and_output_is_byte_deterministic() -> None:
    raw = PASS_PATH.read_bytes()
    first = analyze_bytes(raw)
    second = analyze_bytes(raw)
    assert first.ok and first.rule_ids == ()
    assert first == second
    assert first.canonical_bytes() == b'{"rule_ids":[],"schema_version":1,"verdict":"GO"}\n'


def test_aud1_writer_output_is_a_producer_derived_go() -> None:
    source = _records()
    timestamps = iter(record["monotonic_ns"] for record in source)
    stream = io.BytesIO()
    journal = AuditJournal(
        stream,
        run_id=source[0]["run_id"],
        clock=lambda: next(timestamps),
    )
    for record in source[:-1]:
        journal.append_event(
            emitter=record["emitter"],
            role=record["role"],
            phase=record["phase"],
            event=record["event"],
            outcome=record["outcome"],
            error_code=record["error_code"],
            correlation=record["correlation"],
            identities=record["identities"],
            digests=record["digests"],
            counts=record["counts"],
        )
    terminal = source[-1]
    journal.append_terminal(
        outcome=terminal["outcome"],
        error_code=terminal["error_code"],
        identities=terminal["identities"],
        elapsed_ns=terminal["elapsed_ns"],
        deadline_ns=terminal["deadline_ns"],
        planned_exits=terminal["planned_exits"],
        observed_exits=terminal["observed_exits"],
        hashes=terminal["hashes"],
        digests=terminal["digests"],
        resources=terminal["resources"],
        dropped_frames=terminal["dropped_frames"],
        truncated_logs=terminal["truncated_logs"],
    )
    assert stream.getvalue() == PASS_PATH.read_bytes()
    assert analyze_bytes(stream.getvalue()).rule_ids == ()


@pytest.mark.parametrize("path", _overlay_paths(), ids=lambda path: path.stem)
def test_each_committed_negative_isolates_one_rule(path: Path) -> None:
    overlay = _overlay(path)
    verdict = analyze_bytes(build_negative(overlay))
    assert not verdict.ok
    assert verdict.rule_ids == (overlay["rule_id"],)
    assert verdict.as_dict()["verdict"] == "RED"


def test_corpus_has_exactly_one_overlay_per_rule_and_is_bounded() -> None:
    paths = _overlay_paths()
    overlays = tuple(_overlay(path) for path in paths)
    assert len(paths) == len(RULE_IDS) == 16
    assert tuple(sorted(value["rule_id"] for value in overlays)) == RULE_IDS
    assert {
        str(path.relative_to(FIXTURE_ROOT)): _overlay(path)["rule_id"] for path in paths
    } == FIXTURE_RULES
    assert sum(path.stat().st_size for path in (PASS_PATH, *paths)) <= 1_048_576


def test_nonterminal_overlays_recompute_terminal_prefix() -> None:
    for path in _overlay_paths():
        overlay = _overlay(path)
        if overlay.get("reseal", True):
            raw = build_negative(overlay)
            lines = raw[:-1].split(b"\n")
            terminal = json.loads(lines[-1])
            assert (
                terminal["prefix_sha256"]
                == hashlib.sha256(b"".join(line + b"\n" for line in lines[:-1])).hexdigest()
            )


def test_multiple_findings_are_sorted_and_go_requires_an_empty_set() -> None:
    overlay = {
        "changes": [
            {"action": "set", "line": 2, "path": ["event"], "value": "projects"},
            {
                "action": "set",
                "line": 8,
                "path": ["hashes", "product_after_sha256"],
                "value": "b" * 64,
            },
        ]
    }
    verdict = analyze_bytes(build_negative(overlay))
    assert verdict.rule_ids == ("A16-HSH-001", "A16-SUC-001")
    assert not verdict.ok
    assert Verdict(("A16-SUC-001", "A16-HSH-001")).rule_ids == verdict.rule_ids
    with pytest.raises(ValueError, match="unknown rule ID"):
        Verdict(("A16-UNKNOWN-001",))


def test_crlf_is_not_reinterpreted_as_lf_framing() -> None:
    records = _records()
    lines = [canonical_line(record)[:-1] for record in records]
    raw_prefix = b"\r\n".join(lines[:-1]) + b"\r\n"
    records[-1]["prefix_sha256"] = hashlib.sha256(raw_prefix).hexdigest()
    crlf = raw_prefix + canonical_line(records[-1])[:-1] + b"\r\n"
    assert analyze_bytes(crlf).rule_ids == ("A16-JRN-002",)


def test_callback_permutation_and_deleted_operation_fail_correlation() -> None:
    records = _records()
    records[2]["correlation"]["callback_id"] = 4
    records[3]["correlation"]["callback_id"] = 3
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-COR-001",)

    records = _records()
    del records[4]["correlation"]["operation"]
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-COR-001",)


def test_digest_shape_and_terminal_artifact_binding_are_closed() -> None:
    records = _records()
    del records[3]["digests"]["callback_sha256"]
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-COR-001",)

    records = _records()
    records[3]["digests"]["callback_sha256"] = ""
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-JRN-001",)

    records = _records()
    records[3]["digests"]["callback_sha256"] = "c" * 64
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-COR-001",)

    records = _records()
    records[7]["digests"]["result_sha256"] = "b" * 64
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-ART-001",)


@pytest.mark.parametrize("event_index", range(8))
def test_each_event_requires_a_serialized_digest_mapping(event_index: int) -> None:
    records = _records()
    records[event_index]["digests"] = None
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-JRN-001",)


def test_cli_emits_canonical_schema_red_for_null_event_digests(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    records = _records()
    records[0]["digests"] = None
    path = tmp_path / "null-digests.jsonl"
    path.write_bytes(_seal(records))

    assert analyzer_module.main([str(path)]) == 1
    assert capsysbinary.readouterr().out == Verdict(("A16-JRN-001",)).canonical_bytes()


def test_analyzer_backstops_attribute_errors_as_schema_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_attribute_error(_document: object) -> tuple[str, ...]:
        raise AttributeError("malformed mapping reached a semantic rule")

    monkeypatch.setattr(analyzer_module, "run_registered", raise_attribute_error)
    assert analyze_bytes(PASS_PATH.read_bytes()).rule_ids == ("A16-JRN-001",)


def test_authority_location_full_resource_ledger_and_deadline_are_closed() -> None:
    records = _records()
    records[2]["correlation"]["checkout_id"] = "checkout_late"
    records[2]["identities"]["checkout_id"] = "checkout_late"
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-RES-001",)

    records = _records()
    for field in ("checkout_id", "grant_id"):
        records[0]["correlation"][field], records[1]["correlation"][field] = (
            records[1]["correlation"][field],
            records[0]["correlation"][field],
        )
        records[0]["identities"][field], records[1]["identities"][field] = (
            records[1]["identities"][field],
            records[0]["identities"][field],
        )
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-RES-001",)

    records = _records()
    records[-1]["resources"]["active_sockets"] = 1
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-RES-002",)

    records = _records()
    records[0]["counts"]["document_count"] = 2
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-RES-002",)

    records = _records()
    records[-1]["monotonic_ns"] = records[-1]["deadline_ns"] + 1
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-TIM-001",)

    records = _records()
    records[-1]["elapsed_ns"] = 1
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-TIM-001",)


def test_emitter_role_and_core_identity_relationships_are_closed() -> None:
    records = _records()
    records[5]["role"] = "controller"
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-IDN-001",)

    records = _records()
    records[-1]["identities"]["candidate_revision"] = records[-1]["identities"]["revision_id"]
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-IDN-001",)


def test_terminal_dropped_frames_uses_ipc_not_io_taxonomy() -> None:
    records = _records()
    records[-1]["dropped_frames"] = 1
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-IPC-001",)


def test_parser_is_total_for_wrong_types_and_recursive_json() -> None:
    assert analyze_bytes(None).rule_ids == ("A16-JRN-002",)
    nested = b'{"value":' + b"[" * 2_000 + b"0" + b"]" * 2_000 + b"}\n"
    assert analyze_bytes(nested).rule_ids == ("A16-JRN-002",)


@pytest.mark.parametrize("schema_version", [True, False, 1.0])
@pytest.mark.parametrize("line_indexes", [(0,), (8,), tuple(range(9))])
def test_schema_versions_require_exact_json_integers(
    schema_version: object, line_indexes: tuple[int, ...]
) -> None:
    records = _records()
    for index in line_indexes:
        records[index]["schema_version"] = schema_version
    assert analyze_bytes(_seal(records)).rule_ids == ("A16-JRN-001",)


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_non_utf8_json_encodings_cannot_desynchronize_nesting_scan(encoding: str) -> None:
    deep = '{"marker":"\u0122","value":' + "[" * 33 + "0" + "]" * 33 + "}\n"
    assert analyze_bytes(deep.encode(encoding)).rule_ids == ("A16-JRN-002",)


def test_utf8_nesting_scan_handles_multibyte_quotes_and_escaped_structure() -> None:
    deep = ('{"marker":"\u0122","value":' + "[" * 33 + "0" + "]" * 33 + "}\n").encode()
    assert analyze_bytes(deep).rule_ids == ("A16-JRN-002",)
    escaped = json.dumps(
        {"marker": '\\"[{]}\\\\\u0122'},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert journal_rules._json_nesting_is_bounded(escaped)  # noqa: SLF001
    assert journal_rules._json_nesting_is_bounded("[" * 32 + "]" * 32)  # noqa: SLF001
    assert not journal_rules._json_nesting_is_bounded("[" * 33 + "]" * 33)  # noqa: SLF001


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf{}\n",
        b'{"value":"\xef\xbb\xbf"}\n',
        b'{"value":"\x00"}\n',
        b'{"value":"\xff"}\n',
    ],
)
def test_bom_nul_and_non_utf8_are_rejected(raw: bytes) -> None:
    assert analyze_bytes(raw).rule_ids == ("A16-JRN-002",)


def test_excess_record_count_is_rejected_before_json_or_split_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"{}\n" * 1_000_000

    def forbidden_loads(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("record-count rejection must precede JSON decoding")

    monkeypatch.setattr(journal_rules.json, "loads", forbidden_loads)
    assert len(raw) < MAX_RUN_BYTES
    tracemalloc.start()
    try:
        assert analyze_bytes(raw).rule_ids == ("A16-JRN-002",)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_048_576


def test_unknown_wide_payload_is_schema_red_without_being_retained() -> None:
    raw = _canonical({"type": "event", "wide": [0] * 3_000})
    assert len(raw) <= 16_384
    assert analyze_bytes(raw).rule_ids == ("A16-JRN-001",)


def test_duplicate_keys_and_concatenated_runs_fail_closed() -> None:
    raw = PASS_PATH.read_bytes()
    duplicate = raw.replace(b'{"correlation":', b'{"schema_version":1,"correlation":', 1)
    assert analyze_bytes(duplicate).rule_ids == ("A16-JRN-002",)
    assert not analyze_bytes(raw + raw).ok


def test_cli_rejects_oversized_stat_before_open(monkeypatch: pytest.MonkeyPatch) -> None:
    class OversizedPath:
        def __init__(self, _value: object) -> None:
            pass

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=MAX_RUN_BYTES + 1)

        def open(self, _mode: str) -> object:
            raise AssertionError("oversized input must not be opened")

    output = io.BytesIO()
    monkeypatch.setattr(analyzer_module, "Path", OversizedPath)
    monkeypatch.setattr(analyzer_module.sys, "stdout", SimpleNamespace(buffer=output))
    assert analyzer_module.main(["oversized.jsonl"]) == 1
    assert output.getvalue() == Verdict(("A16-JRN-002",)).canonical_bytes()
