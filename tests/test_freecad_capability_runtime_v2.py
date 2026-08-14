"""Focused tests for managed FreeCAD runtime capability composition."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import vibecad.execution.freecad_capability_runtime_v2 as runtime_capabilities
import vibecad.execution.freecad_discovery_runtime_v2 as runtime_discovery
from vibecad.execution.capabilities import (
    CapabilityBackend,
    CapabilityCatalogError,
    CapabilityCatalogErrorCode,
    CapabilityCatalogSegment,
    CapabilityDescriptor,
    CapabilityExecutionProfile,
    CapabilityKind,
    CapabilityLifecycleStage,
    CapabilityRiskClass,
    CapabilitySupportStatus,
    CapabilityTermRef,
)
from vibecad.execution.freecad_builtin_intent_capabilities import (
    current_freecad_intent_capability_specs,
)
from vibecad.execution.freecad_capabilities import (
    FreeCadNativeTypeCategory,
    FreeCadRegisteredType,
    freecad_type_capability_id,
)
from vibecad.execution.freecad_capability_projection_v2 import (
    FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
    MAX_FREECAD_CAPABILITY_PROMOTION_PACKS,
    FreeCadCapabilityPromotionEntry,
    FreeCadCapabilityPromotionPack,
    FreeCadCapabilitySemanticKind,
)
from vibecad.execution.freecad_capability_runtime_v2 import (
    MAX_FREECAD_CAPABILITY_QUERY_PAGE_SIZE,
    compose_managed_freecad_capability_runtime_v2,
    encode_freecad_capability_query_page_v2,
    encode_freecad_capability_runtime_binding_v2,
    query_freecad_capability_runtime_v2,
)
from vibecad.execution.freecad_discovery_runtime_v2 import (
    FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
)
from vibecad.execution.freecad_discovery_v2 import (
    FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
    FreeCadDiscoverySnapshotV2,
    FreeCadPagedCapabilityCatalog,
    build_paged_freecad_type_catalog,
)
from vibecad.execution.operation_capabilities import operation_capability_id
from vibecad.parametric.compiler import (
    _EDGE_TREATMENT_TYPE_IDS,
    _FEATURE_TYPE_IDS,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _registered(
    native_type_id: str,
    module: str,
    category: FreeCadNativeTypeCategory,
    parent: str | None = None,
) -> FreeCadRegisteredType:
    return FreeCadRegisteredType(
        native_type_id=native_type_id,
        declaring_module=module,
        parent_native_type_id=parent,
        category=category,
    )


def _discovery() -> FreeCadPagedCapabilityCatalog:
    compiler_type_ids = tuple(
        sorted(
            {
                *_FEATURE_TYPE_IDS.values(),
                *_EDGE_TREATMENT_TYPE_IDS.values(),
                *(item.native_type_id for item in current_freecad_intent_capability_specs()),
            }
        )
    )
    registered_types = (
        _registered(
            "Base::BaseClass",
            "Base",
            FreeCadNativeTypeCategory.NATIVE_TYPE,
        ),
        _registered(
            "App::DocumentObject",
            "App",
            FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
            "Base::BaseClass",
        ),
        _registered(
            "App::Property",
            "App",
            FreeCadNativeTypeCategory.PROPERTY_TYPE,
            "Base::BaseClass",
        ),
        _registered(
            "App::Extension",
            "App",
            FreeCadNativeTypeCategory.EXTENSION_TYPE,
            "Base::BaseClass",
        ),
        *(
            _registered(
                native_type_id,
                native_type_id.split("::", 1)[0],
                FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
                "App::DocumentObject",
            )
            for native_type_id in compiler_type_ids
        ),
        _registered(
            "PartDesign::TestRepresentableFeature",
            "PartDesign",
            FreeCadNativeTypeCategory.DOCUMENT_OBJECT,
            "App::DocumentObject",
        ),
    )
    snapshot = FreeCadDiscoverySnapshotV2(
        schema_version=FREECAD_DISCOVERY_V2_SCHEMA_VERSION,
        backend_version=(1, 1, 0),
        build_fingerprint_sha256=_sha("managed-freecad-build"),
        platform_id="macos.arm64",
        probe_profile=CapabilityExecutionProfile.HEADLESS,
        probe_modules=FREECAD_DISCOVERY_V2_ALLOWED_MODULES,
        registered_types=registered_types,
    )
    return build_paged_freecad_type_catalog(snapshot, max_descriptors_per_page=4)


def _term(label: str) -> CapabilityTermRef:
    term_id = f"semantic/{label}"
    return CapabilityTermRef(
        term_ref_id=f"semantic.test.{label}",
        namespace="vcad.test.runtime-capability",
        vocabulary_version="1.0",
        term_id=term_id,
        term_definition_sha256=_sha(term_id),
    )


def _extra_catalog(
    backend: CapabilityBackend,
    label: str,
) -> CapabilityCatalogSegment:
    module_term = _term("extra-module")
    operation_term = _term("extra-operation")
    module_id = "vibecad.module.test.extra"
    capability_id = f"vibecad.operation.test.extra.{label}"
    return CapabilityCatalogSegment(
        schema_version=1,
        segment_id=f"test.extra.{label}",
        backend=backend,
        discovery_receipt_sha256=_sha(f"extra-{label}"),
        discovery_algorithm_id="vcad.test.extra-catalog",
        discovery_algorithm_version="1.0",
        terms=(module_term, operation_term),
        descriptors=(
            CapabilityDescriptor(
                capability_id=module_id,
                kind=CapabilityKind.MODULE,
                native_identifier="vibecad.test.extra",
                declaring_module_id=module_id,
                status=CapabilitySupportStatus.REPRESENTABLE,
                risk_class=CapabilityRiskClass.READ_ONLY,
                semantic_term_ref_ids=(module_term.term_ref_id,),
            ),
            CapabilityDescriptor(
                capability_id=capability_id,
                kind=CapabilityKind.OPERATION,
                native_identifier=f"vibecad.extra.{label}",
                declaring_module_id=module_id,
                status=CapabilitySupportStatus.EXECUTABLE,
                risk_class=CapabilityRiskClass.READ_ONLY,
                semantic_term_ref_ids=(operation_term.term_ref_id,),
                execution_profiles=(CapabilityExecutionProfile.HEADLESS,),
                lifecycle_stages=(CapabilityLifecycleStage.INSPECT,),
            ),
        ),
    )


def _promotion_pack(
    discovery: FreeCadPagedCapabilityCatalog,
) -> FreeCadCapabilityPromotionPack:
    term = _term("synthetic-representable")
    return FreeCadCapabilityPromotionPack(
        schema_version=FREECAD_CAPABILITY_PROMOTION_PACK_SCHEMA_VERSION,
        pack_id="test.synthetic.representable",
        lane_id="test.partdesign",
        adapter_id="vcad.test.partdesign",
        adapter_version="1.0",
        adapter_contract_sha256=_sha("partdesign-adapter"),
        discovery_snapshot_sha256=discovery.snapshot.snapshot_sha256,
        discovery_manifest_sha256=discovery.manifest.manifest_sha256,
        backend=discovery.snapshot.backend,
        terms=(term,),
        entries=(
            FreeCadCapabilityPromotionEntry(
                native_type_id="PartDesign::TestRepresentableFeature",
                semantic_kind=FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT,
                target_status=CapabilitySupportStatus.REPRESENTABLE,
                risk_class=CapabilityRiskClass.MUTATING,
                semantic_term_ref_ids=(term.term_ref_id,),
            ),
        ),
    )


def _install_fake_collector(
    monkeypatch: pytest.MonkeyPatch,
    discovery: FreeCadPagedCapabilityCatalog,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def collect(**kwargs: object) -> FreeCadPagedCapabilityCatalog:
        calls.append(kwargs)
        return discovery

    monkeypatch.setattr(
        runtime_capabilities,
        "collect_managed_freecad_discovery_v2",
        collect,
    )
    return calls


def _error_code(call) -> CapabilityCatalogErrorCode:
    with pytest.raises(CapabilityCatalogError) as failure:
        call()
    return failure.value.code


def test_composes_builtin_extra_and_promotion_catalogs_with_exact_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery()
    calls = _install_fake_collector(monkeypatch, discovery)
    alpha = _extra_catalog(discovery.snapshot.backend, "alpha")
    beta = _extra_catalog(discovery.snapshot.backend, "beta")
    pack = _promotion_pack(discovery)
    importer = lambda name: name  # noqa: E731 - identity is never called by the fake collector
    forward = compose_managed_freecad_capability_runtime_v2(
        freecad=object(),
        module_importer=importer,
        extra_formal_catalogs=(alpha, beta),
        promotion_packs=(pack,),
    )
    reverse = compose_managed_freecad_capability_runtime_v2(
        freecad=object(),
        module_importer=importer,
        extra_formal_catalogs=(beta, alpha),
        promotion_packs=(pack,),
    )

    assert len(calls) == 2
    assert calls[0]["probe_modules"] == FREECAD_DISCOVERY_V2_ALLOWED_MODULES
    assert calls[0]["module_importer"] is importer
    assert forward.binding.binding_sha256 == reverse.binding.binding_sha256
    assert encode_freecad_capability_runtime_binding_v2(forward.binding) == (
        encode_freecad_capability_runtime_binding_v2(reverse.binding)
    )
    assert forward.binding.backend == discovery.snapshot.backend
    assert forward.binding.discovery_snapshot_sha256 == discovery.snapshot.snapshot_sha256
    assert forward.binding.discovery_manifest_sha256 == discovery.manifest.manifest_sha256
    assert forward.binding.projection_manifest_sha256 == (
        forward.projection.manifest.manifest_sha256
    )
    assert forward.binding.extra_formal_catalog_sha256 == tuple(
        sorted((alpha.catalog_sha256, beta.catalog_sha256))
    )
    assert len(forward.binding.promotion_pack_sha256) == 7
    assert pack.pack_sha256 in forward.binding.promotion_pack_sha256
    assert forward.binding.intent_catalog_sha256 == forward.intent_catalog.catalog_sha256
    assert len(forward.projection.manifest.formal_bindings) == 48
    assert {
        binding.formal_capability_id for binding in forward.projection.manifest.formal_bindings
    }.issuperset(spec.capability_id for spec in current_freecad_intent_capability_specs())
    assert (
        forward.projection.index.lookup(operation_capability_id("create_box")).status
        is CapabilitySupportStatus.EXECUTABLE
    )
    assert (
        forward.projection.index.lookup("vibecad.operation.test.extra.alpha").status
        is CapabilitySupportStatus.EXECUTABLE
    )
    assert (
        forward.projection.index.lookup(
            freecad_type_capability_id("PartDesign::TestRepresentableFeature")
        ).status
        is CapabilitySupportStatus.REPRESENTABLE
    )
    assert {
        forward.projection.index.lookup(freecad_type_capability_id(spec.native_type_id)).status
        for spec in current_freecad_intent_capability_specs()
    } == {CapabilitySupportStatus.EXECUTABLE}


def test_query_filters_and_n_plus_one_pages_are_stable_and_content_addressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery()
    _install_fake_collector(monkeypatch, discovery)
    runtime = compose_managed_freecad_capability_runtime_v2(
        freecad=object(),
        promotion_packs=(_promotion_pack(discovery),),
    )
    first = query_freecad_capability_runtime_v2(runtime, module="App", page_size=2)
    repeated = query_freecad_capability_runtime_v2(runtime, module="App", page_size=2)
    second = query_freecad_capability_runtime_v2(
        runtime,
        module="App",
        page_size=2,
        cursor=first.next_cursor,
    )
    exact = query_freecad_capability_runtime_v2(runtime, module="App", page_size=3)

    assert first.total_matches == exact.total_matches == 3
    assert [item.native_type_id for item in first.entries] == [
        "App::DocumentObject",
        "App::Extension",
    ]
    assert [item.native_type_id for item in second.entries] == ["App::Property"]
    assert first.offset == 0 and second.offset == 2
    assert first.next_cursor is not None and second.next_cursor is None
    assert exact.next_cursor is None
    assert first.page_sha256 == repeated.page_sha256
    assert first.next_cursor == repeated.next_cursor
    encoded = json.loads(encode_freecad_capability_query_page_v2(first))
    assert encoded["page_sha256"] == first.page_sha256
    representable = query_freecad_capability_runtime_v2(
        runtime,
        semantic_kind=FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT,
        minimum_status=CapabilitySupportStatus.REPRESENTABLE,
    )
    executable = query_freecad_capability_runtime_v2(
        runtime,
        minimum_status=CapabilitySupportStatus.EXECUTABLE,
    )
    verified = query_freecad_capability_runtime_v2(
        runtime,
        minimum_status=CapabilitySupportStatus.VERIFIED,
    )
    assert representable.total_matches == 38
    assert "PartDesign::Pad" in {item.native_type_id for item in representable.entries}
    assert executable.total_matches == len(current_freecad_intent_capability_specs()) == 37
    assert verified.total_matches == 0
    assert verified.entries == () and verified.next_cursor is None


def test_cursor_tamper_query_drift_runtime_drift_and_unknown_module_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery()
    _install_fake_collector(monkeypatch, discovery)
    runtime = compose_managed_freecad_capability_runtime_v2(freecad=object())
    drifted = compose_managed_freecad_capability_runtime_v2(
        freecad=object(),
        extra_formal_catalogs=(_extra_catalog(discovery.snapshot.backend, "drift"),),
    )
    first = query_freecad_capability_runtime_v2(runtime, module="App", page_size=2)
    assert first.next_cursor is not None
    raw = base64.urlsafe_b64decode(
        first.next_cursor.encode("ascii") + b"=" * (-len(first.next_cursor.encode("ascii")) % 4)
    )
    item = json.loads(raw)
    item["offset"] = 1
    tampered = (
        base64.urlsafe_b64encode(
            json.dumps(item, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
                "ascii"
            )
        )
        .decode("ascii")
        .rstrip("=")
    )

    assert (
        _error_code(
            lambda: query_freecad_capability_runtime_v2(
                runtime,
                module="App",
                page_size=2,
                cursor=tampered,
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _error_code(
            lambda: query_freecad_capability_runtime_v2(
                runtime,
                module="App",
                page_size=1,
                cursor=first.next_cursor,
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _error_code(
            lambda: query_freecad_capability_runtime_v2(
                runtime,
                semantic_kind=FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT,
                page_size=2,
                cursor=first.next_cursor,
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _error_code(
            lambda: query_freecad_capability_runtime_v2(
                drifted,
                module="App",
                page_size=2,
                cursor=first.next_cursor,
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _error_code(lambda: query_freecad_capability_runtime_v2(runtime, module="Unknown"))
        is CapabilityCatalogErrorCode.UNKNOWN_REFERENCE
    )
    assert (
        _error_code(lambda: query_freecad_capability_runtime_v2(runtime, cursor="not-a-cursor"))
        is CapabilityCatalogErrorCode.INVALID_INPUT
    )


def test_runtime_and_query_inputs_are_bounded_and_cross_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _discovery()
    _install_fake_collector(monkeypatch, discovery)
    runtime = compose_managed_freecad_capability_runtime_v2(freecad=object())
    drifted_backend = dataclasses.replace(
        discovery.snapshot.backend,
        build_fingerprint_sha256=_sha("other-build"),
    )
    wrong_catalog = _extra_catalog(drifted_backend, "wrong-build")

    assert (
        _error_code(
            lambda: compose_managed_freecad_capability_runtime_v2(
                freecad=object(),
                extra_formal_catalogs=(wrong_catalog,),
            )
        )
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )
    assert (
        _error_code(
            lambda: compose_managed_freecad_capability_runtime_v2(
                freecad=object(),
                extra_formal_catalogs=[],  # type: ignore[arg-type]
            )
        )
        is CapabilityCatalogErrorCode.INVALID_INPUT
    )
    assert (
        _error_code(
            lambda: compose_managed_freecad_capability_runtime_v2(
                freecad=object(),
                promotion_packs=(_promotion_pack(discovery),)
                * MAX_FREECAD_CAPABILITY_PROMOTION_PACKS,
            )
        )
        is CapabilityCatalogErrorCode.BUDGET_EXCEEDED
    )
    assert (
        _error_code(
            lambda: query_freecad_capability_runtime_v2(
                runtime,
                page_size=MAX_FREECAD_CAPABILITY_QUERY_PAGE_SIZE + 1,
            )
        )
        is CapabilityCatalogErrorCode.INVALID_INPUT
    )
    assert (
        _error_code(
            lambda: query_freecad_capability_runtime_v2(
                runtime,
                minimum_status="executable",  # type: ignore[arg-type]
            )
        )
        is CapabilityCatalogErrorCode.INVALID_INPUT
    )
    tampered_binding = dataclasses.replace(
        runtime.binding,
        projection_catalog_sha256=_sha("tampered-projection"),
    )
    assert (
        _error_code(lambda: dataclasses.replace(runtime, binding=tampered_binding))
        is CapabilityCatalogErrorCode.INTEGRITY_FAILURE
    )


@pytest.mark.slow
def test_real_managed_freecad_runtime_composes_449_and_pages_with_extra_catalog() -> None:
    python_raw = os.environ.get("VIBECAD_MANAGED_FREECAD_PYTHON")
    if not python_raw:
        pytest.skip("managed FreeCAD Python was not requested")
    if Path(python_raw).resolve() != Path(sys.executable).resolve():
        pytest.fail("the test must run inside the requested managed FreeCAD Python")

    from vibecad.freecad_env import prepare_freecad_import

    prepare_freecad_import()
    import FreeCAD  # noqa: PLC0415

    before_documents = FreeCAD.listDocuments()
    before_gui_up = FreeCAD.GuiUp
    before_gui_module = "FreeCADGui" in sys.modules
    version = tuple(FreeCAD.Version())
    backend = CapabilityBackend(
        backend_id="freecad",
        backend_version=tuple(int(item) for item in version[:3]),
        build_fingerprint_sha256=runtime_discovery._build_fingerprint(version),
        platform_id=runtime_discovery._platform_id(),
        discovery_profile=CapabilityExecutionProfile.HEADLESS,
    )
    extra = _extra_catalog(backend, "real-probe")
    runtime = compose_managed_freecad_capability_runtime_v2(
        freecad=FreeCAD,
        extra_formal_catalogs=(extra,),
    )

    part_ids: list[str] = []
    cursor = None
    while True:
        page = query_freecad_capability_runtime_v2(
            runtime,
            module="Part",
            page_size=25,
            cursor=cursor,
        )
        part_ids.extend(item.native_type_id for item in page.entries)
        cursor = page.next_cursor
        if cursor is None:
            break
    document_ids: list[str] = []
    cursor = None
    while True:
        page = query_freecad_capability_runtime_v2(
            runtime,
            semantic_kind=FreeCadCapabilitySemanticKind.DOCUMENT_OBJECT,
            page_size=128,
            cursor=cursor,
        )
        document_ids.extend(item.native_type_id for item in page.entries)
        cursor = page.next_cursor
        if cursor is None:
            break

    assert before_documents == FreeCAD.listDocuments() == {}
    assert before_gui_up == FreeCAD.GuiUp == 0
    assert before_gui_module is False
    assert "FreeCADGui" not in sys.modules
    assert runtime.binding.native_type_count == 449
    assert len(runtime.projection.manifest.entries) == 449
    assert len(runtime.projection.manifest.formal_bindings) == 48
    assert len(runtime.binding.promotion_pack_sha256) == 6
    assert len(part_ids) == len(set(part_ids)) == 141
    assert part_ids == sorted(part_ids)
    assert len(document_ids) == len(set(document_ids)) == 175
    assert runtime.binding.extra_formal_catalog_sha256 == (extra.catalog_sha256,)
    assert (
        runtime.projection.index.lookup("vibecad.operation.test.extra.real-probe").status
        is CapabilitySupportStatus.EXECUTABLE
    )
    executable = query_freecad_capability_runtime_v2(
        runtime,
        minimum_status=CapabilitySupportStatus.EXECUTABLE,
    )
    assert executable.total_matches == 37
    assert {item.native_type_id for item in executable.entries} == {
        spec.native_type_id for spec in current_freecad_intent_capability_specs()
    }
    assert (
        query_freecad_capability_runtime_v2(
            runtime,
            minimum_status=CapabilitySupportStatus.VERIFIED,
        ).total_matches
        == 0
    )
