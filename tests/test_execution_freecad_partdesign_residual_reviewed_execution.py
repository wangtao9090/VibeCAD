"""Focused product gates for the Reviewed PartDesign residual family."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

import vibecad.execution.freecad_partdesign_residual_reviewed_execution as residual_execution
import vibecad.execution.freecad_reviewed_intent_execution as reviewed_execution
from vibecad.execution.freecad_partdesign_residual_reviewed_execution import (
    PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS,
    PARTDESIGN_RESIDUAL_REQUIRED_SOURCE_ROLES,
    PARTDESIGN_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES,
    PartDesignResidualOwnershipClosure,
    PartDesignResidualProductKind,
    PartDesignResidualSourceRole,
    build_partdesign_residual_reviewed_family_descriptor,
    execute_partdesign_residual_reviewed_plan_with_sources,
    resolve_partdesign_residual_reviewed_operation,
)
from vibecad.execution.freecad_reviewed_intent_execution import (
    CURRENT_REVIEWED_INTENT_ROUTES,
    REVIEWED_PARTDESIGN_RESIDUAL_ROUTES,
    ReviewedIntentExecutionError,
    ReviewedIntentExecutionErrorCode,
    ReviewedIntentRoute,
    ReviewedNativeExecutionResult,
    _ReviewedProductResultKind,
)
from vibecad.execution.selectors import (
    EntityIdentity,
    Provenance,
    ProvenanceSource,
    SemanticRole,
)
from vibecad.intent_bridge.contracts import DocumentRef
from vibecad.intent_bridge.freecad_partdesign_residual_adapter import (
    PARTDESIGN_RESIDUAL_MANIFEST,
)
from vibecad.intent_bridge.freecad_sketch_intent_adapter import (
    REVIEWED_SKETCH_FAMILY_MANIFEST,
)
from vibecad.parametric.freecad_partdesign_residual_rules import (
    ExplicitPlacement,
    HoleExtent,
    PartDesignResidualBackendPlan,
    PartDesignResidualConformanceReceipt,
    PartDesignResidualOperation,
    RevolutionAxis,
    SemanticObjectSelection,
)


class _Wire:
    def isClosed(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True


class _Shape:
    def __init__(self, brep: str, *, solid: bool) -> None:
        self._brep = brep
        self.Solids = (object(),) if solid else ()
        self.Volume = 100.0 if solid else 0.0
        self.Wires = () if solid else (_Wire(),)

    def exportBrepToString(self) -> str:
        return self._brep

    def isNull(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return False

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True


class _Vector:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = x, y, z


class _Rotation:
    def __init__(self) -> None:
        self.Q = (0.0, 0.0, 0.0, 1.0)


class _Placement:
    def __init__(self) -> None:
        self.Base = _Vector(1.0, 2.0, 3.0)
        self.Rotation = _Rotation()


class _Body:
    TypeId = "PartDesign::Body"

    def __init__(self, document: _Document) -> None:
        self.Document = document
        self.Name = "Body"
        self.Group: tuple[_Object, ...] = ()
        self.Tip: _Object | None = None


class _Object:
    def __init__(
        self,
        document: _Document,
        body: _Body,
        *,
        name: str,
        type_id: str,
        brep: str,
        solid: bool,
    ) -> None:
        self.Document = document
        self._body = body
        self.Name = name
        self.TypeId = type_id
        self.Shape = _Shape(brep, solid=solid)
        self.State = ("Up-to-date",)
        self.OpenVertices: tuple[object, ...] = ()
        self.Placement = _Placement()
        self.Visibility = True

    def getParentGeoFeatureGroup(self) -> _Body:  # noqa: N802 - FreeCAD API spelling
        return self._body

    def isValid(self) -> bool:  # noqa: N802 - FreeCAD API spelling
        return True


class _Profile(_Object):
    def __init__(
        self,
        document: _Document,
        body: _Body,
        *,
        name: str = "Profile",
    ) -> None:
        super().__init__(
            document,
            body,
            name=name,
            type_id="Sketcher::SketchObject",
            brep="closed-profile",
            solid=False,
        )
        self.MapMode = "Support"
        self.AttachmentSupport: list[tuple[_Object, list[str]]] = []
        self.GeometryCount = 1
        self.Geometry = (SimpleNamespace(TypeId="Part::GeomCircle"),)

    def getConstruction(self, _index: int) -> bool:  # noqa: N802 - FreeCAD API spelling
        return False


class _Document:
    def __init__(self) -> None:
        self.Objects: tuple[object, ...] = ()

    def getObject(self, name: str) -> object | None:
        return next((item for item in self.Objects if getattr(item, "Name", None) == name), None)


class _Session:
    def __init__(self, document: _Document, identities: dict[object, EntityIdentity]) -> None:
        self.doc = document
        self._identities = identities

    def read_object_identity(self, item: object) -> EntityIdentity:
        return self._identities[item]


def _identity(index: int, type_id: str, role: SemanticRole) -> EntityIdentity:
    return EntityIdentity(
        object_id=f"object_{index:032x}",
        feature_id=f"feature_{index:032x}",
        object_type=type_id,
        semantic_role=role,
        provenance=Provenance(
            source=ProvenanceSource.MODEL,
            operation_id="apply_reviewed_intent",
        ),
    )


def _shape_sha256(item: _Object) -> str:
    return hashlib.sha256(item.Shape.exportBrepToString().encode("utf-8")).hexdigest()


def _route_for_native_type(native_type_id: str) -> ReviewedIntentRoute:
    return next(
        route
        for route in CURRENT_REVIEWED_INTENT_ROUTES
        if route.operation.native_type_id == native_type_id
    )


def _unregistered_sketch_route() -> ReviewedIntentRoute:
    route = object.__new__(ReviewedIntentRoute)
    operation = REVIEWED_SKETCH_FAMILY_MANIFEST.operations[0]
    object.__setattr__(route, "operation", operation)
    object.__setattr__(route, "manifest", REVIEWED_SKETCH_FAMILY_MANIFEST)
    return route


def _source_result(
    route: ReviewedIntentRoute,
    item: _Object,
    *,
    run_token: object,
    kind: _ReviewedProductResultKind,
) -> ReviewedNativeExecutionResult:
    value = object.__new__(ReviewedNativeExecutionResult)
    plan_sha256 = hashlib.sha256(f"plan:{item.Name}".encode()).hexdigest()
    receipt = SimpleNamespace(
        plan_sha256=plan_sha256,
        object_name=item.Name,
        receipt_sha256=hashlib.sha256(f"receipt:{item.Name}".encode()).hexdigest(),
        result_shape_sha256=_shape_sha256(item),
    )
    for name, field_value in (
        ("route", route),
        ("object", item),
        ("plan_sha256", plan_sha256),
        ("plan_content_sha256", hashlib.sha256(f"content:{item.Name}".encode()).hexdigest()),
        ("native_receipt", receipt),
        ("owned_objects", (item,)),
        ("result_kind", kind),
        ("semantic_roles", (SemanticRole.FEATURE,)),
        ("_retained_run_token", run_token),
    ):
        object.__setattr__(value, name, field_value)
    return value


def _plan(operation: PartDesignResidualOperation) -> PartDesignResidualBackendPlan:
    common = {
        "source_artifact_id": "artifact_residual_product",
        "source_graph_id": "graph_residual_product",
        "source_graph_sha256": "1" * 64,
        "source_content_sha256": "2" * 64,
        "lowering_request_sha256": "3" * 64,
        "adapter_contract_sha256": (PARTDESIGN_RESIDUAL_MANIFEST.adapter.adapter_contract_sha256),
        "manifest_sha256": PARTDESIGN_RESIDUAL_MANIFEST.manifest_sha256,
        "body_id": "body_main",
        "node_id": "node_target",
        "result_id": "result_target",
        "operation": operation,
    }
    if operation is PartDesignResidualOperation.HOLE:
        return PartDesignResidualBackendPlan(
            **common,
            base=SemanticObjectSelection(node_id="node_base", result_id="result_base"),
            profile=SemanticObjectSelection(
                node_id="node_profile",
                result_id="result_profile",
            ),
            hole_extent=HoleExtent.DIMENSION,
            diameter_mm=6.0,
            depth_mm=5.0,
        )
    if operation is PartDesignResidualOperation.REVOLUTION:
        return PartDesignResidualBackendPlan(
            **common,
            profile=SemanticObjectSelection(
                node_id="node_profile",
                result_id="result_profile",
            ),
            axis_reference_id="reference_axis",
            axis_result_id="result_axis",
            revolution_axis=RevolutionAxis.HORIZONTAL,
            angle_degrees=180.0,
        )
    return PartDesignResidualBackendPlan(
        **common,
        placement=ExplicitPlacement(
            position_mm=(1.0, 2.0, 3.0),
            axis=(0.0, 0.0, 1.0),
            angle_degrees=0.0,
        ),
    )


def _document_ref(plan: PartDesignResidualBackendPlan) -> DocumentRef:
    return DocumentRef(
        artifact_id="artifact_residual_plan",
        role_term_ref_id=PARTDESIGN_RESIDUAL_MANIFEST.plan_role_term.term_ref_id,
        schema_term_ref_id=PARTDESIGN_RESIDUAL_MANIFEST.plan_schema_term.term_ref_id,
        document_id="document_residual_plan",
        document_digest=plan.plan_sha256,
        content_sha256=hashlib.sha256(plan.canonical_bytes).hexdigest(),
        size_bytes=len(plan.canonical_bytes),
        media_type=PARTDESIGN_RESIDUAL_MANIFEST.plan_media_type,
    )


def _fixture(
    *,
    include_base: bool,
) -> tuple[
    _Document,
    _Body,
    _Object | None,
    _Profile,
    _Session,
]:
    document = _Document()
    body = _Body(document)
    base = (
        _Object(
            document,
            body,
            name="Base",
            type_id="PartDesign::AdditiveBox",
            brep="base-solid",
            solid=True,
        )
        if include_base
        else None
    )
    profile = _Profile(document, body)
    body.Group = (profile,) if base is None else (base, profile)
    body.Tip = profile
    document.Objects = (body, *body.Group)
    identities = {
        body: _identity(1, body.TypeId, SemanticRole.PART),
        profile: _identity(2, profile.TypeId, SemanticRole.FEATURE),
    }
    if base is not None:
        identities[base] = _identity(3, base.TypeId, SemanticRole.FEATURE)
    return document, body, base, profile, _Session(document, identities)


def _fake_native_apply(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: object,
) -> PartDesignResidualConformanceReceipt:
    plan = residual_execution.decode_partdesign_residual_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    document = bindings.document
    body = bindings.body
    prior_tip = body.Tip
    result = _Object(
        document,
        body,
        name=f"Result_{plan.operation.value}",
        type_id=PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS[plan.operation.value].native_type_id,
        brep=f"result-{plan.operation.value}",
        solid=plan.operation is not PartDesignResidualOperation.COORDINATE_SYSTEM,
    )
    if plan.operation is PartDesignResidualOperation.COORDINATE_SYSTEM:
        result.MapMode = "Deactivated"
        result.AttachmentSupport = []
        body.Tip = prior_tip
        before_volume = after_volume = None
    else:
        result.BaseFeature = None if bindings.base is None else bindings.base.object
        result.Profile = (bindings.profile.object, ())
        body.Tip = result
        before_volume = 100.0 if bindings.base is not None else None
        after_volume = 50.0
    body.Group = (*body.Group, result)
    document.Objects = (*document.Objects, result)
    return PartDesignResidualConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=result.Name,
        native_type_id=result.TypeId,
        before_volume_mm3=before_volume,
        after_volume_mm3=after_volume,
    )


def test_formal_manifest_verification_delta_is_exact_three_and_registered() -> None:
    assert PARTDESIGN_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES == (
        (
            "partdesign_residual.hole",
            "org.vibecad.parametric-mechanical/1.0.0/operation.remove-circular-hole@"
            "b6faa2d999694a2a173e87d139902e99335029dcb728949c4579a1fcbf30bbfa",
        ),
        (
            "partdesign_residual.revolution",
            "org.vibecad.parametric-mechanical/1.0.0/operation.additive-revolution-angle@"
            "ef6c311553d2f2fd9947dde2a573acad95fd10fe06b342c97841d55ee1cc22f8",
        ),
        (
            "partdesign_residual.coordinate_system",
            "org.vibecad.parametric-mechanical/1.0.0/operation.local-coordinate-system@"
            "396bfe5b9273a47cc65e47fbef2bec980b89cd2c2db34a7fb645b67c05e34905",
        ),
    )
    assert tuple(
        PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS[item.value].native_type_id
        for item in PartDesignResidualOperation
    ) == (
        "PartDesign::Hole",
        "PartDesign::Revolution",
        "PartDesign::CoordinateSystem",
    )
    assert CURRENT_REVIEWED_INTENT_ROUTES[93:96] == REVIEWED_PARTDESIGN_RESIDUAL_ROUTES
    assert tuple(route.operation_id for route in REVIEWED_PARTDESIGN_RESIDUAL_ROUTES) == tuple(
        item[0] for item in PARTDESIGN_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES
    )


def test_source_and_result_closures_are_exact_and_descriptor_is_not_registered() -> None:
    assert PARTDESIGN_RESIDUAL_REQUIRED_SOURCE_ROLES == {
        "hole": ((PartDesignResidualSourceRole.BASE, PartDesignResidualSourceRole.PROFILE),),
        "revolution": (
            (PartDesignResidualSourceRole.PROFILE,),
            (PartDesignResidualSourceRole.BASE, PartDesignResidualSourceRole.PROFILE),
        ),
        "coordinate_system": ((PartDesignResidualSourceRole.BODY_ANCHOR,),),
    }
    assert PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS["hole"].result_kind is (
        PartDesignResidualProductKind.SOLID
    )
    assert PARTDESIGN_RESIDUAL_PRODUCT_CONTRACTS["coordinate_system"].semantic_role is (
        SemanticRole.SUPPORT
    )
    before = CURRENT_REVIEWED_INTENT_ROUTES
    descriptor = build_partdesign_residual_reviewed_family_descriptor()
    assert descriptor.manifest is PARTDESIGN_RESIDUAL_MANIFEST
    assert tuple(item.source_count for item in descriptor.product_results) == (2, 1, 2, 1)
    assert descriptor.requires_same_run_sources is True
    assert CURRENT_REVIEWED_INTENT_ROUTES is before


def test_resolver_requires_complete_formal_identity() -> None:
    for operation_id, semantic_operation in PARTDESIGN_RESIDUAL_REVIEWED_PRODUCT_IDENTITIES:
        operation = resolve_partdesign_residual_reviewed_operation(
            operation_id,
            semantic_operation,
        )
        assert operation is not None
        assert operation_id.endswith(f".{operation.operation_id}")
    assert (
        resolve_partdesign_residual_reviewed_operation(
            "partdesign_residual.hole",
            "operation.remove-circular-hole",
        )
        is None
    )


def test_profile_only_revolution_consumes_retained_same_run_sketch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, body, _base, profile, session = _fixture(include_base=False)
    token = object()
    sketch_route = _unregistered_sketch_route()
    monkeypatch.setattr(
        reviewed_execution,
        "CURRENT_REVIEWED_INTENT_ROUTES",
        (*CURRENT_REVIEWED_INTENT_ROUTES, sketch_route),
    )
    source = _source_result(
        sketch_route,
        profile,
        run_token=token,
        kind=_ReviewedProductResultKind.VALID_SHAPE,
    )
    monkeypatch.setattr(residual_execution, "apply_partdesign_residual_plan", _fake_native_apply)
    plan = _plan(PartDesignResidualOperation.REVOLUTION)

    native = execute_partdesign_residual_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        _document_ref(plan),
        PARTDESIGN_RESIDUAL_MANIFEST.operations[2],
        (source,),
        session=session,
        run_token=token,
    )

    assert body.Tip is native.object
    assert native.object.TypeId == "PartDesign::Revolution"
    assert isinstance(native.receipt, PartDesignResidualOwnershipClosure)
    assert native.receipt.semantic_role is SemanticRole.FEATURE
    assert _shape_sha256(profile) == source.native_receipt.result_shape_sha256


def test_coordinate_system_uses_tip_anchor_and_preserves_tip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, body, _base, profile, session = _fixture(include_base=False)
    prior_tip = body.Tip
    token = object()
    sketch_route = _unregistered_sketch_route()
    monkeypatch.setattr(
        reviewed_execution,
        "CURRENT_REVIEWED_INTENT_ROUTES",
        (*CURRENT_REVIEWED_INTENT_ROUTES, sketch_route),
    )
    source = _source_result(
        sketch_route,
        profile,
        run_token=token,
        kind=_ReviewedProductResultKind.VALID_SHAPE,
    )
    monkeypatch.setattr(residual_execution, "apply_partdesign_residual_plan", _fake_native_apply)
    plan = _plan(PartDesignResidualOperation.COORDINATE_SYSTEM)

    native = execute_partdesign_residual_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        _document_ref(plan),
        PARTDESIGN_RESIDUAL_MANIFEST.operations[0],
        (source,),
        session=session,
        run_token=token,
    )

    assert body.Tip is prior_tip
    assert native.object.TypeId == "PartDesign::CoordinateSystem"
    assert native.receipt.semantic_role is SemanticRole.SUPPORT
    assert native.receipt.result_shape_sha256 is None


def test_xy_plane_hole_profile_fails_closed_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, body, base, profile, session = _fixture(include_base=True)
    assert base is not None
    token = object()
    sketch_route = _unregistered_sketch_route()
    base_route = _route_for_native_type("PartDesign::AdditiveBox")
    monkeypatch.setattr(
        reviewed_execution,
        "CURRENT_REVIEWED_INTENT_ROUTES",
        (*CURRENT_REVIEWED_INTENT_ROUTES, sketch_route),
    )
    sources = (
        _source_result(
            base_route,
            base,
            run_token=token,
            kind=_ReviewedProductResultKind.SOLID,
        ),
        _source_result(
            sketch_route,
            profile,
            run_token=token,
            kind=_ReviewedProductResultKind.VALID_SHAPE,
        ),
    )
    called = False

    def forbidden_apply(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("native mutation must remain unreachable")

    monkeypatch.setattr(residual_execution, "apply_partdesign_residual_plan", forbidden_apply)
    plan = _plan(PartDesignResidualOperation.HOLE)
    before = (document.Objects, body.Group, body.Tip)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_partdesign_residual_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes,
            _document_ref(plan),
            PARTDESIGN_RESIDUAL_MANIFEST.operations[1],
            sources,
            session=session,
            run_token=token,
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
    assert (document.Objects, body.Group, body.Tip) == before


def test_flat_face_hole_profile_is_exactly_bound_to_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, body, base, profile, session = _fixture(include_base=True)
    assert base is not None
    profile.MapMode = "FlatFace"
    profile.AttachmentSupport = [(base, ["Face1"])]
    token = object()
    sketch_route = _unregistered_sketch_route()
    base_route = _route_for_native_type("PartDesign::AdditiveBox")
    monkeypatch.setattr(
        reviewed_execution,
        "CURRENT_REVIEWED_INTENT_ROUTES",
        (*CURRENT_REVIEWED_INTENT_ROUTES, sketch_route),
    )
    sources = (
        _source_result(
            base_route,
            base,
            run_token=token,
            kind=_ReviewedProductResultKind.SOLID,
        ),
        _source_result(
            sketch_route,
            profile,
            run_token=token,
            kind=_ReviewedProductResultKind.VALID_SHAPE,
        ),
    )
    monkeypatch.setattr(residual_execution, "apply_partdesign_residual_plan", _fake_native_apply)
    plan = _plan(PartDesignResidualOperation.HOLE)

    native = execute_partdesign_residual_reviewed_plan_with_sources(
        document,
        plan,
        plan.canonical_bytes,
        _document_ref(plan),
        PARTDESIGN_RESIDUAL_MANIFEST.operations[1],
        sources,
        session=session,
        run_token=token,
    )

    assert body.Tip is native.object
    assert native.object.BaseFeature is base
    assert native.object.Profile[0] is profile
    assert native.receipt.source_shape_sha256s == tuple(
        item.native_receipt.result_shape_sha256 for item in sources
    )


def test_cross_run_source_is_rejected_before_native_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, _body, _base, profile, session = _fixture(include_base=False)
    retained_token = object()
    requested_token = object()
    sketch_route = _unregistered_sketch_route()
    monkeypatch.setattr(
        reviewed_execution,
        "CURRENT_REVIEWED_INTENT_ROUTES",
        (*CURRENT_REVIEWED_INTENT_ROUTES, sketch_route),
    )
    source = _source_result(
        sketch_route,
        profile,
        run_token=retained_token,
        kind=_ReviewedProductResultKind.VALID_SHAPE,
    )
    called = False

    def forbidden_apply(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("native mutation must remain unreachable")

    monkeypatch.setattr(residual_execution, "apply_partdesign_residual_plan", forbidden_apply)
    plan = _plan(PartDesignResidualOperation.REVOLUTION)

    with pytest.raises(ReviewedIntentExecutionError) as caught:
        execute_partdesign_residual_reviewed_plan_with_sources(
            document,
            plan,
            plan.canonical_bytes,
            _document_ref(plan),
            PARTDESIGN_RESIDUAL_MANIFEST.operations[2],
            (source,),
            session=session,
            run_token=requested_token,
        )

    assert caught.value.code is ReviewedIntentExecutionErrorCode.INTEGRITY_FAILURE
    assert called is False
