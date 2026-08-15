"""Focused public seam tests for managed FreeCAD runtime capabilities."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import vibecad.execution.freecad_capability_runtime_v2 as runtime_capabilities
import vibecad.server as server
from vibecad.execution.capabilities import (
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilitySupportStatus,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadCapabilitySemanticKind,
)

_DIGEST = "a" * 64
_BINDING_DIGEST = "b" * 64
_QUERY_DIGEST = "c" * 64
_PAGE_DIGEST = "d" * 64


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _binding_mapping() -> dict[str, object]:
    return {
        "backend": {
            "backend_id": "freecad",
            "backend_version": [1, 1, 0],
            "build_fingerprint_sha256": _DIGEST,
            "discovery_profile": "headless",
            "platform_id": "macos.arm64",
        },
        "binding_sha256": _BINDING_DIGEST,
        "compiler_catalog_sha256": _DIGEST,
        "discovery_manifest_sha256": _DIGEST,
        "discovery_snapshot_sha256": _DIGEST,
        "extra_formal_catalog_sha256": [],
        "intent_catalog_sha256": _DIGEST,
        "native_type_count": 1,
        "operation_catalog_sha256": _DIGEST,
        "projection_catalog_sha256": _DIGEST,
        "projection_manifest_sha256": _DIGEST,
        "promotion_pack_sha256": [],
        "schema_version": 1,
    }


def _page_mapping() -> dict[str, object]:
    return {
        "entries": [
            {
                "active_descriptor_sha256": _DIGEST,
                "active_status": "discovered",
                "capability_id": "freecad.native.Part.Box",
                "declaring_module": "Part",
                "inheritance_family_native_type_id": "Part::Feature",
                "layers": {
                    "discovered": {
                        "catalog_sha256": _DIGEST,
                        "descriptor_sha256": _DIGEST,
                        "promotion_pack_sha256": None,
                        "status": "discovered",
                    },
                    "representable": None,
                    "executable": None,
                    "verified": None,
                },
                "native_type_id": "Part::Box",
                "parent_native_type_id": "Part::Feature",
                "semantic_kind": "document_object",
            }
        ],
        "next_cursor": None,
        "offset": 0,
        "page_sha256": _PAGE_DIGEST,
        "page_size": 1,
        "query_sha256": _QUERY_DIGEST,
        "runtime_binding_sha256": _BINDING_DIGEST,
        "schema_version": 1,
        "total_matches": 1,
    }


class _Slot:
    def __init__(self, runtime: object, calls: list[str]) -> None:
        self.runtime = runtime
        self.calls = calls

    def get(self) -> object:
        self.calls.append("compose")
        return self.runtime


def _install_fake_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    query_error: CapabilityCatalogError | None = None,
) -> tuple[object, list[str], list[dict[str, object]]]:
    calls: list[str] = []
    query_calls: list[dict[str, object]] = []
    runtime = SimpleNamespace(binding=object())
    page = object()

    monkeypatch.setattr(
        server._installer,
        "is_ready",
        lambda: (calls.append("guard"), True)[1],
    )
    monkeypatch.setattr(server, "_in_conda_runtime", lambda: True)
    monkeypatch.setattr(
        server,
        "_enter_application_effect",
        lambda: (calls.append("effect"), True)[1],
    )
    monkeypatch.setattr(server, "_runtime_capability_slot", _Slot(runtime, calls))

    def query(value: object, **kwargs: object) -> object:
        assert value is runtime
        calls.append("query")
        query_calls.append(kwargs)
        if query_error is not None:
            raise query_error
        return page

    monkeypatch.setattr(runtime_capabilities, "query_freecad_capability_runtime_v2", query)
    monkeypatch.setattr(
        runtime_capabilities,
        "encode_freecad_capability_runtime_binding_v2",
        lambda value: _canonical(_binding_mapping()),
    )
    monkeypatch.setattr(
        runtime_capabilities,
        "encode_freecad_capability_query_page_v2",
        lambda value: _canonical(_page_mapping()),
    )
    return runtime, calls, query_calls


def test_public_query_guards_then_composes_and_returns_canonical_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, calls, query_calls = _install_fake_runtime(monkeypatch)

    class ClosedApplicationSlot:
        def get(self) -> object:
            raise AssertionError("runtime capability queries must not open the Task Kernel")

    monkeypatch.setattr(server, "_application_slot", ClosedApplicationSlot())
    result = anyio.run(
        server._handle_call_tool,
        "query_freecad_runtime_capabilities",
        {
            "schema_version": 1,
            "module": "Part",
            "semantic_kind": "document_object",
            "minimum_status": "representable",
            "limit": 1,
        },
    )

    assert calls == ["guard", "effect", "compose", "query"]
    assert query_calls == [
        {
            "module": "Part",
            "semantic_kind": FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT,
            "minimum_status": CapabilitySupportStatus.REPRESENTABLE,
            "page_size": 1,
            "cursor": None,
        }
    ]
    assert result.isError is False
    assert result.structuredContent == {
        "schema_version": 1,
        "ok": True,
        "result": {
            "schema_version": 1,
            "runtime_binding": _binding_mapping(),
            "query_page": _page_mapping(),
        },
        "error": None,
    }


def test_runtime_guard_and_schema_rejection_never_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(server._installer, "is_ready", lambda: False)
    monkeypatch.setattr(
        server,
        "_enter_application_effect",
        lambda: calls.append("effect") or False,
    )
    monkeypatch.setattr(
        server,
        "_runtime_capability_slot",
        _Slot(object(), calls),
    )
    assert (
        server._schema_failure(
            "query_freecad_runtime_capabilities",
            {"schema_version": 1, "limit": 128, "cursor": None},
        )
        is None
    )

    unavailable = anyio.run(
        server._handle_call_tool,
        "query_freecad_runtime_capabilities",
        {"schema_version": 1},
    )
    invalid = anyio.run(
        server._handle_call_tool,
        "query_freecad_runtime_capabilities",
        {"schema_version": 1, "limit": 129},
    )
    invalid_module = anyio.run(
        server._handle_call_tool,
        "query_freecad_runtime_capabilities",
        {"schema_version": 1, "module": "Part..Design"},
    )

    assert unavailable.isError is True
    assert unavailable.structuredContent["error"]["code"] == "runtime_unavailable"
    assert invalid.isError is True
    assert invalid.structuredContent["error"] == {
        "schema_version": 1,
        "code": "invalid_value",
        "path": "/limit",
        "message": "A request value is invalid.",
    }
    assert invalid_module.isError is True
    assert invalid_module.structuredContent["error"]["code"] == "invalid_value"
    assert invalid_module.structuredContent["error"]["path"] == "/module"
    assert calls == []


@pytest.mark.parametrize(
    ("catalog_code", "catalog_path", "public_code", "public_path"),
    (
        (CapabilityCatalogErrorCode.UNKNOWN_REFERENCE, "module", "invalid_input", "/module"),
        (
            CapabilityCatalogErrorCode.INTEGRITY_FAILURE,
            "cursor/runtime_binding_sha256",
            "integrity_failure",
            "/cursor",
        ),
        (
            CapabilityCatalogErrorCode.BUDGET_EXCEEDED,
            "cursor",
            "budget_exceeded",
            "/cursor",
        ),
    ),
)
def test_capability_query_errors_have_closed_public_mapping(
    monkeypatch: pytest.MonkeyPatch,
    catalog_code: CapabilityCatalogErrorCode,
    catalog_path: str,
    public_code: str,
    public_path: str,
) -> None:
    _install_fake_runtime(
        monkeypatch,
        query_error=CapabilityCatalogError(catalog_code, catalog_path),
    )

    result = anyio.run(
        server._handle_call_tool,
        "query_freecad_runtime_capabilities",
        {"schema_version": 1, "module": "Part", "cursor": "abc"},
    )

    assert result.isError is True
    assert result.structuredContent["error"]["code"] == public_code
    assert result.structuredContent["error"]["path"] == public_path


def test_runtime_capability_slot_is_single_flight_cached_and_retryable() -> None:
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    calls = 0
    runtime = object()

    def compose() -> object:
        nonlocal calls
        with lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return runtime

    slot = server._RuntimeCapabilitySlot(compose)
    results: list[object] = []
    failures: list[BaseException] = []

    def read() -> None:
        try:
            results.append(slot.get())
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=5)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert failures == []
    assert len(results) == 8 and all(item is runtime for item in results)
    assert calls == 1
    assert slot.get() is runtime and calls == 1

    attempts = 0

    def recover() -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first composition fails")
        return runtime

    retryable = server._RuntimeCapabilitySlot(recover)
    with pytest.raises(RuntimeError, match="composition failed"):
        retryable.get()
    assert retryable.get() is runtime
    assert attempts == 2


def test_public_verified_query_composes_without_importing_or_running_live_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecad.execution.freecad_current_managed_verification as live_verifier

    calls: list[object] = []
    freecad = object()
    runtime = SimpleNamespace(binding=object())
    page = object()

    monkeypatch.setattr(
        live_verifier,
        "build_current_managed_freecad_reviewed_verification_set_for_maintainers",
        lambda **kwargs: pytest.fail("public query must not run the maintainer verifier"),
    )
    monkeypatch.setattr(
        server,
        "_prepare_freecad_import",
        lambda: calls.append("prepare"),
    )

    def import_module(name: str) -> object:
        calls.append(("import", name))
        if name == "FreeCAD":
            return freecad
        if name == "vibecad.execution.freecad_capability_runtime_v2":
            return runtime_capabilities
        raise AssertionError(f"unexpected runtime import: {name}")

    def compose(**kwargs: object) -> object:
        calls.append(("compose", kwargs))
        assert kwargs == {"freecad": freecad}
        return runtime

    def query(value: object, **kwargs: object) -> object:
        calls.append(("query", kwargs))
        assert value is runtime
        assert kwargs["minimum_status"] is CapabilitySupportStatus.VERIFIED
        return page

    monkeypatch.setattr(server.importlib, "import_module", import_module)
    monkeypatch.setattr(
        runtime_capabilities,
        "compose_managed_freecad_capability_runtime_v2",
        compose,
    )
    monkeypatch.setattr(runtime_capabilities, "query_freecad_capability_runtime_v2", query)
    monkeypatch.setattr(
        runtime_capabilities,
        "encode_freecad_capability_runtime_binding_v2",
        lambda value: _canonical(_binding_mapping()),
    )
    monkeypatch.setattr(
        runtime_capabilities,
        "encode_freecad_capability_query_page_v2",
        lambda value: _canonical(_page_mapping()),
    )
    monkeypatch.setattr(server._installer, "is_ready", lambda: True)
    monkeypatch.setattr(server, "_in_conda_runtime", lambda: True)
    monkeypatch.setattr(server, "_enter_application_effect", lambda: True)
    monkeypatch.setattr(
        server,
        "_runtime_capability_slot",
        server._RuntimeCapabilitySlot(server._compose_freecad_runtime_capabilities),
    )

    result = anyio.run(
        server._handle_call_tool,
        "query_freecad_runtime_capabilities",
        {"schema_version": 1, "minimum_status": "verified"},
    )

    assert result.isError is False
    assert calls == [
        "prepare",
        ("import", "FreeCAD"),
        ("import", "vibecad.execution.freecad_capability_runtime_v2"),
        ("compose", {"freecad": freecad}),
        (
            "query",
            {
                "module": None,
                "semantic_kind": None,
                "minimum_status": CapabilitySupportStatus.VERIFIED,
                "page_size": 64,
                "cursor": None,
            },
        ),
    ]


def test_tool_manifest_stays_bounded_and_bootstrap_does_not_import_freecad() -> None:
    listed = anyio.run(server._handle_list_tools)
    raw = json.dumps(
        listed.model_dump(mode="json", by_alias=True, exclude_none=True),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert len(listed.tools) == 39
    assert listed.tools[5].name == "query_freecad_runtime_capabilities"
    assert len(raw) <= 32_768
    assert "FreeCAD" not in sys.modules


@pytest.mark.slow
def test_real_managed_runtime_traverses_public_pages_without_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # noqa: PLC0415

    assert FreeCAD.GuiUp == 0
    assert FreeCAD.listDocuments() == {}
    assert "FreeCADGui" not in sys.modules

    monkeypatch.setattr(server._installer, "is_ready", lambda: True)
    monkeypatch.setattr(server, "_in_conda_runtime", lambda: True)
    monkeypatch.setattr(server, "_application_effect_entered", threading.Event())
    monkeypatch.setattr(
        server,
        "_runtime_capability_slot",
        server._RuntimeCapabilitySlot(server._compose_freecad_runtime_capabilities),
    )

    class ClosedApplicationSlot:
        def get(self) -> object:
            raise AssertionError("the runtime query must not open the Task Kernel")

    monkeypatch.setattr(server, "_application_slot", ClosedApplicationSlot())

    native_ids: list[str] = []
    cursor: str | None = None
    binding_sha256: str | None = None
    query_sha256: str | None = None
    while True:
        request: dict[str, object] = {
            "schema_version": 1,
            "module": "Part",
            "minimum_status": "discovered",
            "limit": 17,
        }
        if cursor is not None:
            request["cursor"] = cursor
        response = anyio.run(
            server._handle_call_tool,
            "query_freecad_runtime_capabilities",
            request,
        )
        assert response.isError is False
        result = response.structuredContent["result"]
        binding = result["runtime_binding"]
        page = result["query_page"]
        binding_sha256 = binding_sha256 or binding["binding_sha256"]
        query_sha256 = query_sha256 or page["query_sha256"]
        assert binding["binding_sha256"] == binding_sha256
        assert page["runtime_binding_sha256"] == binding_sha256
        assert page["query_sha256"] == query_sha256
        native_ids.extend(entry["native_type_id"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert len(native_ids) > 17
    assert native_ids == sorted(native_ids)
    assert len(native_ids) == len(set(native_ids))
    assert "Part::Box" in native_ids
    assert FreeCAD.listDocuments() == {}
    assert FreeCAD.GuiUp == 0
    assert "FreeCADGui" not in sys.modules
