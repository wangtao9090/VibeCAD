from __future__ import annotations

import dataclasses
import sys
from types import MappingProxyType, ModuleType, SimpleNamespace

import pytest

import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from tests.test_reviewed_intent_program import reviewed_box_program
from vibecad.execution.freecad_partdesign_primitive_reviewed_execution import (
    PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS,
    PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    REVIEWED_PART_PRIMITIVE_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedNativeExecutionResult,
)
from vibecad.execution.selectors import SemanticRole


def _context(document: object, source_count: int):
    return reviewed_execution._ReviewedFamilyExecutionContext(
        session=SimpleNamespace(doc=document),
        document=document,
        source_results=tuple(object() for _ in range(source_count)),
    )


def _solid(type_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        TypeId=type_id,
        Shape=SimpleNamespace(
            isNull=lambda: False,
            isValid=lambda: True,
            Solids=(object(),),
            Volume=1.0,
        ),
    )


def _variant_contracts(
    *,
    include_source_one: bool = True,
) -> tuple[reviewed_execution._ReviewedProductResultContract, ...]:
    operation = REVIEWED_PART_PRIMITIVE_ROUTES[0].operation
    source_zero = reviewed_execution._ReviewedProductResultContract(
        operation_id=operation.operation_id,
        result_kind=reviewed_execution._ReviewedProductResultKind.SOLID,
        owned_type_ids=(operation.native_type_id, "PartDesign::Body", "App::Origin"),
        semantic_roles=(
            SemanticRole.FEATURE,
            SemanticRole.PART,
            SemanticRole.SUPPORT,
        ),
        source_count=0,
    )
    source_one = reviewed_execution._ReviewedProductResultContract(
        operation_id=operation.operation_id,
        result_kind=reviewed_execution._ReviewedProductResultKind.SOLID,
        owned_type_ids=(operation.native_type_id,),
        semantic_roles=(SemanticRole.FEATURE,),
        source_count=1,
    )
    return (source_zero, source_one) if include_source_one else (source_zero,)


def _family(
    execute_plan: object,
    *,
    product_results: tuple[reviewed_execution._ReviewedProductResultContract, ...] | None = None,
):
    base = REVIEWED_PART_PRIMITIVE_ROUTES[0]
    return reviewed_execution._ReviewedIntentFamilyDescriptor(
        manifest=base.manifest,
        subject_type_term=base.subject_type_term,
        adapter_factory=base.family.adapter_factory,
        validate_plan=base.family.validate_plan,
        execute_plan=execute_plan,
        product_results=_variant_contracts() if product_results is None else product_results,
        minimum_sources=0,
        maximum_sources=1,
    )


def _install_route(
    monkeypatch: pytest.MonkeyPatch,
    family: object,
):
    route = dataclasses.replace(REVIEWED_PART_PRIMITIVE_ROUTES[0], family=family)
    monkeypatch.setattr(
        reviewed_execution,
        "_ROUTES_BY_IDENTITY",
        MappingProxyType({(route.operation_id, route.semantic_operation): route}),
    )
    monkeypatch.setattr(
        reviewed_execution,
        "require_reviewed_route_verified",
        lambda selected, *, freecad: None,
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", ModuleType("FreeCAD"))
    return route


def test_partdesign_primitive_contracts_fit_the_finite_shared_variant_seam() -> None:
    spec = PARTDESIGN_PRIMITIVE_REVIEWED_FAMILY_SPEC
    product_results = tuple(
        reviewed_execution._ReviewedProductResultContract(
            operation_id=contract.operation.value,
            result_kind=reviewed_execution._ReviewedProductResultKind.SOLID,
            owned_type_ids=variant.owned_type_ids,
            semantic_roles=variant.semantic_roles,
            source_count=variant.source_count,
        )
        for contract in PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS.values()
        for variant in contract.closure_variants
    )
    family = reviewed_execution._ReviewedIntentFamilyDescriptor(
        manifest=spec.manifest,
        subject_type_term=spec.subject_type_term,
        adapter_factory=spec.adapter_factory,
        validate_plan=spec.validate_plan,
        execute_plan=spec.execute_plan,
        product_results=product_results,
        minimum_sources=0,
        maximum_sources=1,
    )
    document = SimpleNamespace(Objects=())

    assert len(product_results) == 24
    for operation in spec.manifest.operations:
        contract = PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS[
            next(
                item
                for item in PARTDESIGN_PRIMITIVE_PRODUCT_CONTRACTS
                if item.value == operation.operation_id
            )
        ]
        with_one = family.product_result(operation, context=_context(document, 1))
        assert with_one.source_count == 1
        assert with_one.owned_type_ids == (operation.native_type_id,)
        if contract.minimum_sources == 0:
            without_source = family.product_result(operation, context=_context(document, 0))
            assert without_source.source_count == 0
            assert without_source.owned_type_ids == (
                operation.native_type_id,
                "PartDesign::Body",
                "App::Origin",
                "App::Line",
                "App::Line",
                "App::Line",
                "App::Plane",
                "App::Plane",
                "App::Plane",
                "App::Point",
            )
        else:
            with pytest.raises(ReviewedIntentExecutionError) as caught:
                family.product_result(operation, context=_context(document, 0))
            assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("invalid", ("duplicate", "mixed", "out_of_range"))
def test_family_descriptor_rejects_ambiguous_or_out_of_range_variants(
    invalid: str,
) -> None:
    variants = _variant_contracts()
    if invalid == "duplicate":
        product_results = (*variants, variants[0])
    elif invalid == "mixed":
        product_results = (
            *variants,
            dataclasses.replace(variants[0], source_count=None),
        )
    else:
        product_results = (dataclasses.replace(variants[0], source_count=2),)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        _family(lambda *_args: None, product_results=product_results)
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


@pytest.mark.parametrize("source_count", (0, 1))
def test_executor_accepts_owned_primary_first_when_document_add_order_differs(
    monkeypatch: pytest.MonkeyPatch,
    source_count: int,
) -> None:
    def execute_plan(document, _plan, _payload, plan_document, operation, context):
        assert len(context.source_results) == source_count
        primary = _solid(operation.native_type_id)
        if source_count == 0:
            body = SimpleNamespace(TypeId="PartDesign::Body")
            origin = SimpleNamespace(TypeId="App::Origin")
            document.Objects = (body, origin, primary)
            owned = (primary, body, origin)
        else:
            document.Objects = (primary,)
            owned = (primary,)
        return reviewed_execution._ReviewedFamilyNativeExecution(
            object=primary,
            receipt=SimpleNamespace(plan_sha256=plan_document.document_digest),
            owned_objects=owned,
        )

    family = _family(execute_plan)
    route = _install_route(monkeypatch, family)
    document = SimpleNamespace(Objects=())

    result = reviewed_execution.execute_reviewed_intent_native(
        SimpleNamespace(doc=document),
        reviewed_box_program(),
        source_results=tuple(object() for _ in range(source_count)),
    )

    assert result.route == route
    assert result.object is result.owned_objects[0]
    assert set(map(id, result.owned_objects)) == set(map(id, document.Objects))
    assert (
        result.semantic_roles
        == family.product_result(
            route.operation,
            context=_context(document, source_count),
        ).semantic_roles
    )


@pytest.mark.parametrize(
    "layout",
    ("variant_substitution", "extra", "missing", "duplicate", "wrong_identity"),
)
def test_executor_rejects_variant_substitution_and_inexact_added_identity_sets(
    monkeypatch: pytest.MonkeyPatch,
    layout: str,
) -> None:
    def execute_plan(document, _plan, _payload, plan_document, operation, _context):
        primary = _solid(operation.native_type_id)
        body = SimpleNamespace(TypeId="PartDesign::Body")
        origin = SimpleNamespace(TypeId="App::Origin")
        owned = (primary, body, origin)
        if layout == "variant_substitution":
            document.Objects = (primary,)
            owned = (primary,)
        elif layout == "extra":
            document.Objects = (body, origin, object(), primary)
        elif layout == "missing":
            document.Objects = (body, primary)
        elif layout == "duplicate":
            document.Objects = (body, origin, body)
        else:
            document.Objects = (body, SimpleNamespace(TypeId="App::Origin"), primary)
        return reviewed_execution._ReviewedFamilyNativeExecution(
            object=primary,
            receipt=SimpleNamespace(plan_sha256=plan_document.document_digest),
            owned_objects=owned,
        )

    family = _family(execute_plan)
    _install_route(monkeypatch, family)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        reviewed_execution.execute_reviewed_intent_native(
            SimpleNamespace(doc=SimpleNamespace(Objects=())),
            reviewed_box_program(),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE


def test_missing_source_count_variant_fails_before_family_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def execute_plan(*_args):
        nonlocal called
        called = True

    family = _family(execute_plan, product_results=_variant_contracts(include_source_one=False))
    _install_route(monkeypatch, family)
    document = SimpleNamespace(Objects=())

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        reviewed_execution.execute_reviewed_intent_native(
            SimpleNamespace(doc=document),
            reviewed_box_program(),
            source_results=(object(),),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert document.Objects == ()


def test_variant_result_cannot_be_constructed_without_verified_execution_context() -> None:
    family = _family(lambda *_args: None)
    route = dataclasses.replace(REVIEWED_PART_PRIMITIVE_ROUTES[0], family=family)
    primary = _solid(route.operation.native_type_id)
    body = SimpleNamespace(TypeId="PartDesign::Body")
    origin = SimpleNamespace(TypeId="App::Origin")

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        ReviewedNativeExecutionResult(
            route=route,
            object=primary,
            plan_sha256="a" * 64,
            plan_content_sha256="b" * 64,
            native_receipt=SimpleNamespace(plan_sha256="a" * 64),
            owned_objects=(primary, body, origin),
        )
    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
