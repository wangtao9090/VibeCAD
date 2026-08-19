"""Managed REVIEWED_HOST verification for Wave D reviewed FreeCAD families.

The module owns the exact fixtures, canonical backend plans, 49-case matrix,
and same-process executor for three separately reviewed family manifests:
authenticated Part file imports, Part offset/projection, and ImagePlane.  Its
only runtime entry point accepts the already authenticated ``FreeCAD`` module.
Callers cannot submit observations or claimed results, and nothing here
persists evidence or changes a capability status.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

from vibecad.engine.document_assets import DocumentAssetWorkspace
from vibecad.execution.freecad_capability_projection_v2 import (
    FreeCadPromotionVerificationBinding,
)
from vibecad.execution.freecad_reviewed_verification import (
    MAX_REVIEWED_OBSERVATION_BYTES,
    REQUIRED_REVIEWED_CONFORMANCE_FACETS,
    ReviewedConformanceCase,
    ReviewedConformanceCaseManifest,
    ReviewedConformanceFacet,
    ReviewedVerificationReceipt,
    _admit_reviewed_host_conformance_case_manifest,
    build_managed_freecad_conformance_host,
    build_promotion_verification_binding,
    build_reviewed_verification_receipt,
)
from vibecad.intent_bridge.freecad_imageplane_adapter import (
    IMAGEPLANE_MANIFEST,
    build_imageplane_artifact_document,
)
from vibecad.intent_bridge.freecad_part_file_import_adapter import (
    PART_FILE_IMPORT_MANIFEST,
    build_part_file_import_artifact_document,
)
from vibecad.intent_bridge.freecad_part_offset_projection_adapter import (
    PART_OFFSET_MANIFEST,
)
from vibecad.intent_bridge.reviewed_family_engine import FamilyBatchManifest
from vibecad.parametric.freecad_imageplane_rules import (
    IMAGEPLANE_ARTIFACT_SPECS,
    HostOwnedImageStager,
    ImagePlaneBackendPlan,
    ImagePlaneExecutionBindings,
    ImagePlaneRuleError,
    apply_imageplane_plan,
    encode_imageplane_configuration,
)
from vibecad.parametric.freecad_part_file_import_rules import (
    PART_FILE_IMPORT_NATIVE_SPECS,
    HostOwnedImportStager,
    PartFileImportBackendPlan,
    PartFileImportExecutionBindings,
    PartFileImportOperation,
    PartFileImportRuleError,
    apply_part_file_import_plan,
)
from vibecad.parametric.freecad_part_offset_projection_rules import (
    PART_OFFSET_NATIVE_TYPE_IDS,
    PART_OFFSET_SOURCE_ROLES,
    PartOffsetBackendPlan,
    PartOffsetExecutionBindings,
    PartOffsetOperation,
    PartOffsetRuleError,
    PartOffsetSelection,
    PartOffsetSourceBinding,
    PartOffsetSourceRole,
    apply_part_offset_plan,
    encode_part_offset_configuration,
)

WAVE_D_VERIFIER_ID: Final = "vcad.managed.freecad.reviewed-wave-d"
WAVE_D_VERIFIER_VERSION: Final = "1.0.0"
WAVE_D_CASE_SCHEMA_VERSION: Final = 1
_FIXTURE_CONTRACT_VERSION: Final = "1.0.0"
_CASE_CONTRACT_DOMAIN = b"vibecad.reviewed-freecad.wave-d.case-contract.v1\0"
_PACK_CONTRACT_DOMAIN = b"vibecad.reviewed-freecad.wave-d.pack-contract.v1\0"
_FIXTURE_BUNDLE_DOMAIN = b"vibecad.reviewed-freecad.wave-d.fixture-bundle.v1\0"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _content_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: object, *, maximum: int = MAX_REVIEWED_OBSERVATION_BYTES) -> bytes:
    raw = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if not raw or len(raw) > maximum:
        raise RuntimeError("Wave D verification value exceeds its fixed budget")
    return raw


def _require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _operation_spec(manifest: FamilyBatchManifest, operation_id: str):
    matches = tuple(item for item in manifest.operations if item.operation_id == operation_id)
    if len(matches) != 1:
        raise RuntimeError("Wave D operation manifest closure is invalid")
    return matches[0]


def _common_fields(manifest: FamilyBatchManifest, operation_id: str, label: str) -> dict[str, str]:
    stem = f"wave-d:{manifest.family_id}:{operation_id}:{label}"
    return {
        "source_artifact_id": f"artifact_verify_{manifest.family_id}_{operation_id}_{label}",
        "source_graph_id": f"graph_verify_{manifest.family_id}_{operation_id}",
        "source_graph_sha256": _sha(stem + ":graph"),
        "source_content_sha256": _sha(stem + ":source"),
        "lowering_request_sha256": _sha(stem + ":request"),
        "adapter_contract_sha256": manifest.adapter.adapter_contract_sha256,
        "manifest_sha256": manifest.manifest_sha256,
    }


def _inflate_fixture(encoded: str, expected_sha256: str) -> bytes:
    try:
        payload = zlib.decompress(base64.b85decode(encoded.encode("ascii")))
    except (ValueError, zlib.error) as error:
        raise RuntimeError("Wave D import fixture encoding is invalid") from error
    if _content_sha256(payload) != expected_sha256:
        raise RuntimeError("Wave D import fixture digest drift")
    return payload


# Small, valid line-edge exports produced once by the pinned FreeCAD 1.1.0
# runtime.  They are embedded so every case and plan is content-bound before
# native execution; no ambient file path or exporter timestamp enters a case.
_BREP_PAYLOAD: Final = _inflate_fixture(
    "c-o~<!D_=W486av&~a@Su$sfpUEJHs#u9oRTIyzj5C<pe=-*FH1EXxLhuKDc(tF6yvX8H>d$U14<-KwFhjb3eHhH$OTPl3#8(*Di&-Zj!E@&r1ki^O$r@F6wizi>opr9uPgRT}|r(|yC_*=mLZE(9GvN4?8`p}cP!0BU3#C^H(;+u2j+tjLN#?NgLYH6#nnWtfpkKrh#i|_}BVj6T$DfSNR67zuRi5LOHhV@|i&GN7G_5hlAwWj9O=<JbI3PhDL2CGy{$y&n_SVid<I2dQu",
    "e4fc7fefb4e3e496a2a78fb7ded2a323a62bd618e37fd0755e98d4c6aabb6a4b",
)
_IGES_PAYLOAD: Final = _inflate_fixture(
    "c-pOyO$x#=5QX<SMGi2AnWTT>s<gFrQ7qI8hy*EUmG=H%tjWS^v5Jqt%inx?8P5Ns8V3;rP*iNMy5&h+CvlqbO`g^K+FcI1?=Kw>#S%yr>*l1}{aK6VsC#$?<_<`~GwxnL;|V1}VFctmvp~s!1W1rYSpoqHBy(CwlmSNM3k-bpOhLB55C{bTkhwOUP_Z}Vt6zF(fgT%HL}>TPI?i>c)b(%#9Q*izDLJ5IhGkl32f5i7zqZLLm$0@;@`ror!)5iH@S}DPcmv;BeRl",
    "72bc5a511353226de1e6165d64f4b04a05f868de7cab72b680e9ffc25d68ab2f",
)
_STEP_PAYLOAD: Final = _inflate_fixture(
    "c-oCsZEvGE5dNNDVTqLtCxS3}?MC~-3r=c@H-XdLenN=QYjj<x61sb*)BX1wo0pPqDm96fka_0u%roOLPopWZXZCFBkj><lQI92?$(H*p^H}O8JQh3(bscX{W$9861@)nP!iI^R4Z%hS4Qar196gs6a%l=MhSJCUx$MvYww>kFUQfwF*vrog`}2H_&4Co?^a+C3=#MvSg2m|hR2Sv>T%XX&x&hK2Y~p?OS5^P2hImr<mIV}|rh5?uk>GbK9li_Ezlb0ksg#jJ=J3?M|HB4&v(QVKyP0@Y(9J|6=nJB8>~oh&8L~KuMC3+3kZpO$l{ENRHRY+wn`2$&Pw3p_)geC}un8IA{H3XX)Xni{nH|dW@y`mIj&0kf=D=o}^M>AyLczWZsRb_TQ;80FlcNVn_>~9!D2vCuIu@{3OWEsEo@M^p3`o;k8naQ5S;FFkr7RR`BVC)D8nGpM4rs{A+q+_nR|Gl+X|bs-Bye#RsN+5@30#`mjl?dmlWwCVX!q}kR}#E>1?yLe-kkF4{FFCs)e&iR{Rj?r(6zO!s<s{l)Qc<wa7_Fvr&90cdgz1GbAD<-_lIM3lm%}&It`>hv4LR6qJW76%If>~+1F?vdd$nd@q}#?I4P~VZ)>8$B?JnyFl3IJGgE}B2fy>w$zq?nEC7dW=Be<B18W9ru8^{wT$%`IbPBy1@la){+m@+5N(v(T!-G)VVb|79I{W-LJ5ly&Pp}wgn7R7|4p+)Vm%y?G$rb>65J2Jz(gwT&h(pYnN*x4CvODVUnJ%fN#WB?wcxxugZVAh@29u?>k<H`tuw!4tQVmGfACf{A`wkL~lC~RcDS|wg_B$GMjt=<<z4Kl@$R$h`L?0=7*%w3W%?vcz`O#PEXZz4)iJ-uDpV3r~HK}rk0~uF_fd(iSzhfo<f)U^>U_rsqJ6mG6hS~OagS770%{<iq_3PL?BFT<m-l0)M*gz_+xk}4t;ZEGDFmwaSd%|lU{xDF=+n-jzeV;>dr2x$8{1?jq(gRLF$TAx4V8(ZpJ;^82N~i}y_x{T({v$99jRBuVKKCwZ6@~1svDN{j={{j_lKYgp^g07{37gt0!<6buy$(lt9Ta7j7sbmdFaF7X=1(sr%%-BQ9$wBcK~Qf}<HH$Gs)YGeK=i}pf~Q@S+_zH*o1fswtj4z%d{6nsh5kR-W(BM",
    "c8d041ec9fdd41adb5c0e5b6fec29750127fd7c2a5af0551c59986bc7e4bbb21",
)
_FILE_IMPORT_PAYLOADS: Final = MappingProxyType(
    {
        PartFileImportOperation.BREP: _BREP_PAYLOAD,
        PartFileImportOperation.IGES: _IGES_PAYLOAD,
        PartFileImportOperation.STEP: _STEP_PAYLOAD,
    }
)
_FILE_IMPORT_INVALID_PAYLOADS: Final = MappingProxyType(
    {
        operation: f"not-a-valid-{operation.value}-exchange-artifact".encode("ascii")
        for operation in PartFileImportOperation
    }
)

# A valid one-pixel PNG; the rule still validates its full chunk/CRC structure.
_IMAGE_PAYLOAD: Final = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _file_import_plan(
    operation: PartFileImportOperation,
    payload: bytes,
    *,
    label: str,
) -> PartFileImportBackendPlan:
    spec = PART_FILE_IMPORT_NATIVE_SPECS[operation]
    operation_spec = _operation_spec(PART_FILE_IMPORT_MANIFEST, operation.value)
    artifact_id = f"artifact_verify_file_{operation.value}_{label}"
    return PartFileImportBackendPlan(
        **_common_fields(PART_FILE_IMPORT_MANIFEST, operation.value, label),
        operation_specification_sha256=operation_spec.specification_sha256,
        body_id="document_root",
        node_id=f"node_{operation.value}_{label}",
        result_id=f"result_{operation.value}_{label}",
        operation=operation,
        artifact_id=artifact_id,
        artifact_content_sha256=_content_sha256(payload),
        artifact_role_term_ref_id=spec.artifact_role_term_ref_id,
        artifact_schema_term_ref_id=spec.artifact_schema_term_ref_id,
        artifact_value_type_term_ref_id=spec.artifact_value_type_term_ref_id,
        artifact_media_type=spec.artifact_media_type,
    )


def _offset_plan(operation: PartOffsetOperation) -> PartOffsetBackendPlan:
    configuration = (
        {} if operation is PartOffsetOperation.EDGE_ON_FACE_PROJECTION else {"distance_mm": 2.0}
    )
    return PartOffsetBackendPlan(
        **_common_fields(PART_OFFSET_MANIFEST, operation.value, "primary"),
        container_id="document_root",
        target_node_id=f"node_{operation.value}",
        target_result_id=f"result_{operation.value}",
        operation=operation,
        configuration_bytes=encode_part_offset_configuration(operation, configuration),
        sources=tuple(
            PartOffsetSelection(
                role=role,
                node_id=f"node_source_{role.value}",
                result_id=f"result_source_{role.value}",
            )
            for role in PART_OFFSET_SOURCE_ROLES[operation]
        ),
    )


_IMAGE_CONFIG_CREATE: Final = {
    "media_type": "image/png",
    "x_size_mm": 80.0,
    "y_size_mm": 60.0,
    "placement": {
        "position_mm": [1.0, 2.0, 3.0],
        "axis": [0.0, 0.0, 1.0],
        "angle_degrees": 15.0,
    },
}
_IMAGE_CONFIG_EDIT: Final = {
    "media_type": "image/png",
    "x_size_mm": 96.0,
    "y_size_mm": 72.0,
    "placement": {
        "position_mm": [4.0, 5.0, 6.0],
        "axis": [0.0, 1.0, 0.0],
        "angle_degrees": 25.0,
    },
}


def _image_plan(configuration: dict[str, object], *, label: str) -> ImagePlaneBackendPlan:
    operation = IMAGEPLANE_MANIFEST.operations[0]
    artifact = build_imageplane_artifact_document(
        _IMAGE_PAYLOAD,
        media_type="image/png",
        artifact_id="artifact_verify_imageplane",
    )
    artifact_spec = IMAGEPLANE_ARTIFACT_SPECS["image/png"]
    common = _common_fields(
        IMAGEPLANE_MANIFEST,
        operation.operation_id,
        label,
    )
    # Preserve stable graph/node identity across the reviewed edit plan.
    common["source_graph_id"] = "graph_verify_freecad_imageplane_stable"
    return ImagePlaneBackendPlan(
        **common,
        operation_specification_sha256=operation.specification_sha256,
        container_id="document_root",
        node_id="node_imageplane_stable",
        result_id="result_imageplane_stable",
        artifact_id=artifact.artifact_id,
        artifact_content_sha256=artifact.content_sha256,
        artifact_schema_term_ref_id=artifact_spec.schema_term_ref_id,
        artifact_media_type=artifact.media_type,
        configuration_bytes=encode_imageplane_configuration(configuration),
    )


_FILE_PRIMARY_PLANS: Final = MappingProxyType(
    {
        operation: _file_import_plan(
            operation,
            _FILE_IMPORT_PAYLOADS[operation],
            label="primary",
        )
        for operation in PartFileImportOperation
    }
)
_FILE_LATE_PLANS: Final = MappingProxyType(
    {
        operation: _file_import_plan(
            operation,
            _FILE_IMPORT_INVALID_PAYLOADS[operation],
            label="late",
        )
        for operation in PartFileImportOperation
    }
)
_OFFSET_PLANS: Final = MappingProxyType(
    {operation: _offset_plan(operation) for operation in PartOffsetOperation}
)
_IMAGE_CREATE_PLAN: Final = _image_plan(_IMAGE_CONFIG_CREATE, label="create")
_IMAGE_EDIT_PLAN: Final = _image_plan(_IMAGE_CONFIG_EDIT, label="edit")

WAVE_D_FAMILY_MANIFESTS: Final = (
    PART_FILE_IMPORT_MANIFEST,
    PART_OFFSET_MANIFEST,
    IMAGEPLANE_MANIFEST,
)


def _fixture_bundle_mapping(manifest: FamilyBatchManifest, operation_id: str) -> dict[str, object]:
    if manifest is PART_FILE_IMPORT_MANIFEST:
        operation = PartFileImportOperation(operation_id)
        primary = _FILE_PRIMARY_PLANS[operation]
        late = _FILE_LATE_PLANS[operation]
        return {
            "artifact_content_sha256": _content_sha256(_FILE_IMPORT_PAYLOADS[operation]),
            "artifact_size_bytes": len(_FILE_IMPORT_PAYLOADS[operation]),
            "fixture": "embedded-pinned-line-edge-exchange",
            "late_artifact_content_sha256": _content_sha256(
                _FILE_IMPORT_INVALID_PAYLOADS[operation]
            ),
            "late_plan_sha256": late.plan_sha256,
            "primary_plan_sha256": primary.plan_sha256,
        }
    if manifest is PART_OFFSET_MANIFEST:
        operation = PartOffsetOperation(operation_id)
        return {
            "fixture": "native-authenticated-source-shapes-v1",
            "plan_sha256": _OFFSET_PLANS[operation].plan_sha256,
            "source_roles": [item.value for item in PART_OFFSET_SOURCE_ROLES[operation]],
        }
    if manifest is IMAGEPLANE_MANIFEST:
        return {
            "artifact_content_sha256": _content_sha256(_IMAGE_PAYLOAD),
            "artifact_size_bytes": len(_IMAGE_PAYLOAD),
            "create_plan_sha256": _IMAGE_CREATE_PLAN.plan_sha256,
            "edit_plan_sha256": _IMAGE_EDIT_PLAN.plan_sha256,
            "fixture": "embedded-valid-png-content-addressed-workspace",
        }
    raise RuntimeError("unreviewed Wave D family")


_FIXTURE_BUNDLE_SHA256: Final = MappingProxyType(
    {
        (manifest.family_id, operation.operation_id): hashlib.sha256(
            _FIXTURE_BUNDLE_DOMAIN
            + _canonical(
                {
                    "contract_version": _FIXTURE_CONTRACT_VERSION,
                    "family_manifest_sha256": manifest.manifest_sha256,
                    "operation_specification_sha256": operation.specification_sha256,
                    "fixture": _fixture_bundle_mapping(manifest, operation.operation_id),
                },
                maximum=16 * 1024,
            )
        ).hexdigest()
        for manifest in WAVE_D_FAMILY_MANIFESTS
        for operation in manifest.operations
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WaveDReviewedCaseDescriptor:
    family_id: str
    family_manifest_sha256: str
    operation_id: str
    operation_specification_sha256: str
    native_type_id: str
    facet: ReviewedConformanceFacet
    fixture_contract_version: str
    fixture_bundle_sha256: str
    case_contract_sha256: str = field(init=False)
    case: ReviewedConformanceCase = field(init=False, repr=False)

    def __post_init__(self) -> None:
        matches = tuple(
            item for item in WAVE_D_FAMILY_MANIFESTS if item.family_id == self.family_id
        )
        if len(matches) != 1:
            raise ValueError("unknown Wave D family")
        manifest = matches[0]
        operation = _operation_spec(manifest, self.operation_id)
        expected_bundle = _FIXTURE_BUNDLE_SHA256.get((self.family_id, self.operation_id))
        if (
            manifest.manifest_sha256 != self.family_manifest_sha256
            or operation.specification_sha256 != self.operation_specification_sha256
            or operation.native_type_id != self.native_type_id
            or type(self.facet) is not ReviewedConformanceFacet
            or self.fixture_contract_version != _FIXTURE_CONTRACT_VERSION
            or expected_bundle != self.fixture_bundle_sha256
        ):
            raise ValueError("Wave D descriptor does not close over its reviewed contract")
        body = {
            "family_id": self.family_id,
            "family_manifest_sha256": self.family_manifest_sha256,
            "facet": self.facet.value,
            "fixture_bundle_sha256": self.fixture_bundle_sha256,
            "fixture_contract_version": self.fixture_contract_version,
            "native_type_id": self.native_type_id,
            "operation_id": self.operation_id,
            "operation_specification_sha256": self.operation_specification_sha256,
            "schema_version": WAVE_D_CASE_SCHEMA_VERSION,
        }
        contract_sha256 = hashlib.sha256(
            _CASE_CONTRACT_DOMAIN + _canonical(body, maximum=8 * 1024)
        ).hexdigest()
        object.__setattr__(self, "case_contract_sha256", contract_sha256)
        object.__setattr__(
            self,
            "case",
            ReviewedConformanceCase(
                case_id=f"case.wave-d.{contract_sha256[:32]}",
                operation_id=self.operation_id,
                operation_specification_sha256=self.operation_specification_sha256,
                facet=self.facet,
                case_contract_sha256=contract_sha256,
            ),
        )


def _build_descriptors() -> tuple[WaveDReviewedCaseDescriptor, ...]:
    return tuple(
        WaveDReviewedCaseDescriptor(
            family_id=manifest.family_id,
            family_manifest_sha256=manifest.manifest_sha256,
            operation_id=operation.operation_id,
            operation_specification_sha256=operation.specification_sha256,
            native_type_id=operation.native_type_id,
            facet=facet,
            fixture_contract_version=_FIXTURE_CONTRACT_VERSION,
            fixture_bundle_sha256=_FIXTURE_BUNDLE_SHA256[
                (manifest.family_id, operation.operation_id)
            ],
        )
        for manifest in WAVE_D_FAMILY_MANIFESTS
        for operation in manifest.operations
        for facet in REQUIRED_REVIEWED_CONFORMANCE_FACETS
    )


WAVE_D_REVIEWED_CASE_DESCRIPTORS: Final = _build_descriptors()
WAVE_D_VERIFIER_CONTRACT_SHA256: Final = hashlib.sha256(
    _PACK_CONTRACT_DOMAIN
    + _canonical(
        [
            {
                "case_contract_sha256": item.case_contract_sha256,
                "case_sha256": item.case.case_sha256,
                "family_id": item.family_id,
                "fixture_bundle_sha256": item.fixture_bundle_sha256,
            }
            for item in WAVE_D_REVIEWED_CASE_DESCRIPTORS
        ],
        maximum=256 * 1024,
    )
).hexdigest()


def _case_manifest_for(manifest: FamilyBatchManifest) -> ReviewedConformanceCaseManifest:
    return _admit_reviewed_host_conformance_case_manifest(
        manifest=manifest,
        cases=tuple(
            item.case
            for item in WAVE_D_REVIEWED_CASE_DESCRIPTORS
            if item.family_id == manifest.family_id
        ),
    )


WAVE_D_REVIEWED_CASE_MANIFESTS: Final = tuple(
    _case_manifest_for(manifest) for manifest in WAVE_D_FAMILY_MANIFESTS
)


def _shape_facts(shape: object) -> dict[str, object]:
    box = shape.BoundBox
    return {
        "area": round(float(shape.Area), 9),
        "bounds": [
            round(float(box.XMin), 9),
            round(float(box.YMin), 9),
            round(float(box.ZMin), 9),
            round(float(box.XLength), 9),
            round(float(box.YLength), 9),
            round(float(box.ZLength), 9),
        ],
        "edges": len(tuple(shape.Edges)),
        "faces": len(tuple(shape.Faces)),
        "shape_type": str(shape.ShapeType),
        "solids": len(tuple(shape.Solids)),
        "valid": bool(shape.isValid()) and not bool(shape.isNull()),
        "volume": round(float(shape.Volume), 9),
    }


def _document_snapshot(document: object) -> tuple[object, ...]:
    objects = tuple(document.Objects)
    return (
        objects,
        tuple(
            (item, tuple(item.Group)) for item in objects if "Group" in tuple(item.PropertiesList)
        ),
        tuple(
            (item, bool(item.Visibility))
            for item in objects
            if "Visibility" in tuple(item.PropertiesList)
        ),
        tuple(
            (item, _shape_facts(item.Shape))
            for item in objects
            if "Shape" in tuple(item.PropertiesList) and not item.Shape.isNull()
        ),
        bool(document.HasPendingTransaction),
    )


def _same_document_snapshot(document: object, before: tuple[object, ...]) -> bool:
    objects, groups, visibility, shapes, pending = before
    return (
        tuple(document.Objects) == objects
        and all(tuple(item.Group) == members for item, members in groups)
        and all(bool(item.Visibility) is value for item, value in visibility)
        and all(_shape_facts(item.Shape) == value for item, value in shapes)
        and bool(document.HasPendingTransaction) is pending
    )


def _workspace_manifest(root: Path) -> tuple[tuple[str, int, str], ...]:
    return tuple(
        (
            str(path.relative_to(root)),
            path.stat().st_size,
            _content_sha256(path.read_bytes()),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    )


def _observation(
    descriptor: WaveDReviewedCaseDescriptor,
    challenge_sha256: str,
    facts: dict[str, object],
) -> bytes:
    return _canonical(
        {
            "case_contract_sha256": descriptor.case_contract_sha256,
            "case_sha256": descriptor.case.case_sha256,
            "challenge_sha256": challenge_sha256,
            "direct_observation": facts,
            "evidence": "managed_freecad_same_process",
            "family_id": descriptor.family_id,
            "facet": descriptor.facet.value,
            "fixture_bundle_sha256": descriptor.fixture_bundle_sha256,
            "operation_id": descriptor.operation_id,
            "schema_version": WAVE_D_CASE_SCHEMA_VERSION,
            "verifier_contract_sha256": WAVE_D_VERIFIER_CONTRACT_SHA256,
        }
    )


class _ExactArtifactReader:
    __slots__ = ("_payload",)

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, document: object, maximum_bytes: int) -> bytes:
        del document
        if type(maximum_bytes) is not int or len(self._payload) > maximum_bytes:
            raise RuntimeError("Wave D exact artifact exceeds the requested budget")
        return self._payload


def _apply_file_import(
    plan: PartFileImportBackendPlan,
    payload: bytes,
    document: object,
    staging_root: Path,
):
    artifact = build_part_file_import_artifact_document(
        plan.operation,
        payload,
        artifact_id=plan.artifact_id,
    )
    raw = plan.canonical_bytes
    return apply_part_file_import_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=PartFileImportExecutionBindings(
            document=document,
            artifact_document=artifact,
            artifacts=_ExactArtifactReader(payload),
            stager=HostOwnedImportStager(staging_root),
            body_id=plan.body_id,
            expected_adapter_contract_sha256=plan.adapter_contract_sha256,
            expected_manifest_sha256=plan.manifest_sha256,
            expected_operation_specification_sha256=(plan.operation_specification_sha256),
        ),
    )


def _execute_file_import(
    freecad: object,
    descriptor: WaveDReviewedCaseDescriptor,
    temporary_root: Path,
) -> dict[str, object]:
    operation = PartFileImportOperation(descriptor.operation_id)
    primary = _FILE_PRIMARY_PLANS[operation]
    payload = _FILE_IMPORT_PAYLOADS[operation]
    staging_root = temporary_root / "staging"
    asset_root = temporary_root / "document-assets"
    for root in (staging_root, asset_root):
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    document = freecad.newDocument(f"VerifyImport_{operation.value}_{primary.plan_sha256[:8]}")
    document.UndoMode = 1
    workspace = DocumentAssetWorkspace(asset_root)
    workspace.attach_fresh_document(document)
    if descriptor.facet is ReviewedConformanceFacet.NEGATIVE:
        before = _document_snapshot(document)
        artifact = build_part_file_import_artifact_document(
            operation,
            payload,
            artifact_id=primary.artifact_id,
        )
        raw = primary.canonical_bytes
        try:
            apply_part_file_import_plan(
                raw + b" ",
                expected_content_sha256=_content_sha256(raw),
                expected_plan_sha256=primary.plan_sha256,
                bindings=PartFileImportExecutionBindings(
                    document=document,
                    artifact_document=artifact,
                    artifacts=_ExactArtifactReader(payload),
                    stager=HostOwnedImportStager(staging_root),
                    body_id=primary.body_id,
                    expected_adapter_contract_sha256=primary.adapter_contract_sha256,
                    expected_manifest_sha256=primary.manifest_sha256,
                    expected_operation_specification_sha256=(
                        primary.operation_specification_sha256
                    ),
                ),
            )
        except PartFileImportRuleError as error:
            _require(_same_document_snapshot(document, before), "import reject mutated document")
            _require(not tuple(staging_root.iterdir()), "import reject leaked staged files")
            facts = {
                "error_code": error.code.value,
                "error_path": error.path,
                "rollback_exact": True,
                "staging_empty": True,
            }
            return _close_workspace_with_facts(
                freecad,
                workspace,
                document,
                asset_root,
                facts,
            )
        raise RuntimeError("tampered import plan was accepted")
    if descriptor.facet is ReviewedConformanceFacet.LATE_ROLLBACK:
        late = _FILE_LATE_PLANS[operation]
        late_payload = _FILE_IMPORT_INVALID_PAYLOADS[operation]
        before = _document_snapshot(document)
        try:
            _apply_file_import(late, late_payload, document, staging_root)
        except PartFileImportRuleError as error:
            _require(
                _same_document_snapshot(document, before),
                "invalid exact import did not restore the document",
            )
            _require(not tuple(staging_root.iterdir()), "import rollback leaked staged files")
            facts = {
                "error_code": error.code.value,
                "error_path": error.path,
                "late_native_failure": True,
                "rollback_exact": True,
                "staging_empty": True,
            }
            return _close_workspace_with_facts(
                freecad,
                workspace,
                document,
                asset_root,
                facts,
            )
        raise RuntimeError("invalid exact import artifact was accepted")

    receipt = _apply_file_import(primary, payload, document, staging_root)
    feature = document.getObject(receipt.object_name)
    expected_type = PART_FILE_IMPORT_NATIVE_SPECS[operation].type_id
    _require(
        feature is not None
        and feature.TypeId == expected_type
        and feature.FileName == ""
        and feature.isValid()
        and not feature.Shape.isNull(),
        "import create readback failed",
    )
    _require(not tuple(staging_root.iterdir()), "import create leaked staged files")
    if descriptor.facet is ReviewedConformanceFacet.CREATE:
        facts = {
            "artifact_content_sha256": receipt.artifact_content_sha256,
            "native_type_id": feature.TypeId,
            "object_name": feature.Name,
            "receipt_sha256": receipt.receipt_sha256,
            "shape": _shape_facts(feature.Shape),
            "staging_empty": True,
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    if descriptor.facet is ReviewedConformanceFacet.EDIT:
        before = tuple(float(item) for item in feature.Placement.Base)
        feature.Placement = freecad.Placement(
            freecad.Vector(4.0, 5.0, 6.0),
            freecad.Rotation(freecad.Vector(0.0, 0.0, 1.0), 20.0),
        )
        document.recompute()
        after = tuple(round(float(item), 9) for item in feature.Placement.Base)
        _require(after == (4.0, 5.0, 6.0) and after != before, "import placement edit failed")
        facts = {
            "native_type_id": feature.TypeId,
            "placement_before": list(before),
            "placement_after": list(after),
            "shape_valid": feature.Shape.isValid(),
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    if descriptor.facet is ReviewedConformanceFacet.RECOMPUTE:
        before = _shape_facts(feature.Shape)
        document.recompute()
        after = _shape_facts(feature.Shape)
        _require(before == after and feature.FileName == "", "detached import recompute drift")
        facts = {
            "detached_recompute_stable": True,
            "native_type_id": feature.TypeId,
            "shape": after,
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    save_path = temporary_root / f"{descriptor.case.case_sha256}.FCStd"
    document.saveAs(str(save_path))
    size = save_path.stat().st_size
    _require(size > 0, "import save produced an empty FCStd")
    with zipfile.ZipFile(save_path) as archive:
        saved = b"".join(archive.read(name) for name in archive.namelist())
    _require(str(staging_root).encode() not in saved, "import staging path leaked into FCStd")
    if descriptor.facet is ReviewedConformanceFacet.SAVE:
        facts = {
            "format": "FCStd",
            "native_type_id": feature.TypeId,
            "nonempty": True,
            "saved": True,
            "staging_path_absent": True,
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    _require(descriptor.facet is ReviewedConformanceFacet.REOPEN, "unexpected import facet")
    object_name = feature.Name
    expected_shape = _shape_facts(feature.Shape)
    _close_workspace_document(freecad, workspace, document)
    reopened = freecad.newDocument()
    reopened_workspace = DocumentAssetWorkspace(asset_root)
    reopened_workspace.attach_fresh_document(reopened)
    reopened.load(str(save_path))
    reopened.recompute()
    reopened_feature = reopened.getObject(object_name)
    _require(
        reopened_feature is not None
        and reopened_feature.TypeId == expected_type
        and reopened_feature.FileName == ""
        and reopened_feature.isValid()
        and _shape_facts(reopened_feature.Shape) == expected_shape,
        "import reopen readback failed",
    )
    facts = {
        "format": "FCStd",
        "native_type_id": reopened_feature.TypeId,
        "nonempty": True,
        "reopened": True,
        "shape": expected_shape,
    }
    return _close_workspace_with_facts(
        freecad,
        reopened_workspace,
        reopened,
        asset_root,
        facts,
    )


def _offset_sources(
    freecad: object,
    part: object,
    document: object,
    operation: PartOffsetOperation,
) -> dict[PartOffsetSourceRole, object]:
    if operation is PartOffsetOperation.SOLID_OFFSET:
        source = document.addObject("Part::Box", "SolidSource")
        source.Length = 20.0
        source.Width = 10.0
        source.Height = 5.0
        return {PartOffsetSourceRole.SOLID_SOURCE: source}
    if operation is PartOffsetOperation.PLANAR_WIRE_OFFSET:
        source = document.addObject("Part::Feature", "WireSource")
        source.Shape = part.makePolygon(
            [
                freecad.Vector(0.0, 0.0, 0.0),
                freecad.Vector(20.0, 0.0, 0.0),
                freecad.Vector(20.0, 10.0, 0.0),
                freecad.Vector(0.0, 10.0, 0.0),
                freecad.Vector(0.0, 0.0, 0.0),
            ]
        )
        return {PartOffsetSourceRole.PLANAR_WIRE_SOURCE: source}
    support = document.addObject("Part::Feature", "SupportSource")
    support.Shape = part.makePlane(20.0, 20.0)
    edge = document.addObject("Part::Feature", "ProjectionSource")
    edge.Shape = part.makeLine(
        freecad.Vector(2.0, 2.0, 10.0),
        freecad.Vector(15.0, 2.0, 10.0),
    )
    return {
        PartOffsetSourceRole.SUPPORT_FACE: support,
        PartOffsetSourceRole.PROJECTION_EDGE: edge,
    }


def _offset_bindings(
    plan: PartOffsetBackendPlan,
    document: object,
    sources: dict[PartOffsetSourceRole, object],
) -> PartOffsetExecutionBindings:
    return PartOffsetExecutionBindings(
        document=document,
        container_id=plan.container_id,
        sources=tuple(
            PartOffsetSourceBinding(
                role=selection.role,
                node_id=selection.node_id,
                result_id=selection.result_id,
                native_object=sources[selection.role],
            )
            for selection in plan.sources
        ),
    )


def _apply_offset(
    plan: PartOffsetBackendPlan,
    bindings: PartOffsetExecutionBindings,
):
    raw = plan.canonical_bytes
    return apply_part_offset_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=bindings,
    )


def _change_offset_source(
    freecad: object,
    part: object,
    operation: PartOffsetOperation,
    sources: dict[PartOffsetSourceRole, object],
    *,
    extent: float,
) -> None:
    if operation is PartOffsetOperation.SOLID_OFFSET:
        sources[PartOffsetSourceRole.SOLID_SOURCE].Length = extent
    elif operation is PartOffsetOperation.PLANAR_WIRE_OFFSET:
        sources[PartOffsetSourceRole.PLANAR_WIRE_SOURCE].Shape = part.makePolygon(
            [
                freecad.Vector(0.0, 0.0, 0.0),
                freecad.Vector(extent, 0.0, 0.0),
                freecad.Vector(extent, 10.0, 0.0),
                freecad.Vector(0.0, 10.0, 0.0),
                freecad.Vector(0.0, 0.0, 0.0),
            ]
        )
    else:
        sources[PartOffsetSourceRole.PROJECTION_EDGE].Shape = part.makeLine(
            freecad.Vector(2.0, 2.0, 10.0),
            freecad.Vector(extent, 2.0, 10.0),
        )


def _execute_offset(
    freecad: object,
    part: object,
    descriptor: WaveDReviewedCaseDescriptor,
    temporary_root: Path,
) -> dict[str, object]:
    operation = PartOffsetOperation(descriptor.operation_id)
    plan = _OFFSET_PLANS[operation]
    asset_root = temporary_root / "document-assets"
    asset_root.mkdir(mode=0o700)
    asset_root.chmod(0o700)
    document = freecad.newDocument(f"VerifyOffset_{operation.value}_{plan.plan_sha256[:8]}")
    document.UndoMode = 1
    workspace = DocumentAssetWorkspace(asset_root)
    workspace.attach_fresh_document(document)
    sources = _offset_sources(freecad, part, document, operation)
    document.recompute()
    bindings = _offset_bindings(plan, document, sources)
    raw = plan.canonical_bytes
    if descriptor.facet is ReviewedConformanceFacet.NEGATIVE:
        before = _document_snapshot(document)
        try:
            apply_part_offset_plan(
                raw + b" ",
                expected_content_sha256=_content_sha256(raw),
                expected_plan_sha256=plan.plan_sha256,
                bindings=bindings,
            )
        except PartOffsetRuleError as error:
            _require(_same_document_snapshot(document, before), "offset reject mutated document")
            facts = {
                "error_code": error.code.value,
                "error_path": error.path,
                "rollback_exact": True,
            }
            return _close_workspace_with_facts(
                freecad,
                workspace,
                document,
                asset_root,
                facts,
            )
        raise RuntimeError("tampered offset plan was accepted")
    if descriptor.facet is ReviewedConformanceFacet.LATE_ROLLBACK:
        group = document.addObject("App::DocumentObjectGroup", "GuardGroup")
        expected_type = PART_OFFSET_NATIVE_TYPE_IDS[operation]

        class LateOwnershipObserver:
            fired = False

            def slotCreatedObject(self, item: object) -> None:
                if not self.fired and item.TypeId == expected_type:
                    self.fired = True
                    group.addObject(item)

        observer = LateOwnershipObserver()
        freecad.addDocumentObserver(observer)
        before = _document_snapshot(document)
        try:
            try:
                _apply_offset(plan, bindings)
            except PartOffsetRuleError as error:
                _require(observer.fired, "offset sabotage observer did not fire")
                _require(
                    _same_document_snapshot(document, before),
                    "offset late failure did not restore exact snapshot",
                )
                facts = {
                    "error_code": error.code.value,
                    "error_path": error.path,
                    "rollback_exact": True,
                    "sabotage_observed": True,
                }
                return _close_workspace_with_facts(
                    freecad,
                    workspace,
                    document,
                    asset_root,
                    facts,
                )
            raise RuntimeError("offset late ownership sabotage was accepted")
        finally:
            freecad.removeDocumentObserver(observer)

    receipt = _apply_offset(plan, bindings)
    feature = document.getObject(receipt.object_name)
    expected_type = PART_OFFSET_NATIVE_TYPE_IDS[operation]
    _require(
        feature is not None
        and feature.TypeId == expected_type
        and feature.isValid()
        and not feature.Shape.isNull(),
        "offset create readback failed",
    )
    if descriptor.facet is ReviewedConformanceFacet.CREATE:
        facts = {
            "native_type_id": feature.TypeId,
            "object_name": feature.Name,
            "receipt_sha256": receipt.receipt_sha256,
            "shape": _shape_facts(feature.Shape),
            "source_count": len(receipt.source_object_names),
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    if descriptor.facet is ReviewedConformanceFacet.EDIT:
        before = _shape_facts(feature.Shape)
        if operation in {
            PartOffsetOperation.SOLID_OFFSET,
            PartOffsetOperation.PLANAR_WIRE_OFFSET,
        }:
            feature.Value = 1.0
            edited = ["Value", 1.0]
        else:
            _change_offset_source(freecad, part, operation, sources, extent=18.0)
            edited = ["ProjectionSource", 18.0]
        document.recompute()
        after = _shape_facts(feature.Shape)
        _require(after != before and feature.isValid(), "offset edit did not propagate")
        facts = {
            "edited": edited,
            "native_type_id": feature.TypeId,
            "shape_after": after,
            "shape_before": before,
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    if descriptor.facet is ReviewedConformanceFacet.RECOMPUTE:
        before = _shape_facts(feature.Shape)
        _change_offset_source(freecad, part, operation, sources, extent=30.0)
        document.recompute()
        after = _shape_facts(feature.Shape)
        _require(after != before and feature.isValid(), "offset source recompute did not propagate")
        facts = {
            "native_type_id": feature.TypeId,
            "propagated": True,
            "shape_after": after,
            "shape_before": before,
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    save_path = temporary_root / f"{descriptor.case.case_sha256}.FCStd"
    document.saveAs(str(save_path))
    size = save_path.stat().st_size
    _require(size > 0, "offset save produced an empty FCStd")
    if descriptor.facet is ReviewedConformanceFacet.SAVE:
        facts = {
            "format": "FCStd",
            "native_type_id": feature.TypeId,
            "nonempty": True,
            "saved": True,
        }
        return _close_workspace_with_facts(
            freecad,
            workspace,
            document,
            asset_root,
            facts,
        )
    _require(descriptor.facet is ReviewedConformanceFacet.REOPEN, "unexpected offset facet")
    object_name = feature.Name
    expected_shape = _shape_facts(feature.Shape)
    expected_sources = tuple(item.Name for item in sources.values())
    _close_workspace_document(freecad, workspace, document)
    reopened = freecad.newDocument()
    reopened_workspace = DocumentAssetWorkspace(asset_root)
    reopened_workspace.attach_fresh_document(reopened)
    reopened.load(str(save_path))
    reopened.recompute()
    reopened_feature = reopened.getObject(object_name)
    _require(
        reopened_feature is not None
        and reopened_feature.TypeId == expected_type
        and reopened_feature.isValid()
        and _shape_facts(reopened_feature.Shape) == expected_shape
        and tuple(item.Name for item in reopened_feature.OutList) == expected_sources,
        "offset reopen readback failed",
    )
    facts = {
        "format": "FCStd",
        "native_type_id": reopened_feature.TypeId,
        "nonempty": True,
        "reopened": True,
        "shape": expected_shape,
    }
    return _close_workspace_with_facts(
        freecad,
        reopened_workspace,
        reopened,
        asset_root,
        facts,
    )


def _image_artifact_document():
    return build_imageplane_artifact_document(
        _IMAGE_PAYLOAD,
        media_type="image/png",
        artifact_id=_IMAGE_CREATE_PLAN.artifact_id,
    )


def _image_bindings(
    plan: ImagePlaneBackendPlan,
    document: object,
    workspace: DocumentAssetWorkspace,
    staging_root: Path,
) -> ImagePlaneExecutionBindings:
    return ImagePlaneExecutionBindings(
        document=document,
        document_assets=workspace,
        artifact_document=_image_artifact_document(),
        artifacts=_ExactArtifactReader(_IMAGE_PAYLOAD),
        stager=HostOwnedImageStager(staging_root),
        container_id=plan.container_id,
        expected_adapter_contract_sha256=plan.adapter_contract_sha256,
        expected_manifest_sha256=plan.manifest_sha256,
        expected_operation_specification_sha256=plan.operation_specification_sha256,
    )


def _apply_image(
    plan: ImagePlaneBackendPlan,
    document: object,
    workspace: DocumentAssetWorkspace,
    staging_root: Path,
):
    raw = plan.canonical_bytes
    return apply_imageplane_plan(
        raw,
        expected_content_sha256=_content_sha256(raw),
        expected_plan_sha256=plan.plan_sha256,
        bindings=_image_bindings(plan, document, workspace, staging_root),
    )


def _image_state(feature: object) -> dict[str, object]:
    image_path = Path(str(feature.ImageFile))
    return {
        "image_content_sha256": _content_sha256(image_path.read_bytes()),
        "image_name": image_path.name,
        "placement": [
            *[round(float(item), 9) for item in feature.Placement.Base],
            *[round(float(item), 9) for item in feature.Placement.Rotation.Q],
        ],
        "state": list(feature.State),
        "type_id": feature.TypeId,
        "x_size_mm": round(float(feature.XSize), 9),
        "y_size_mm": round(float(feature.YSize), 9),
    }


def _close_workspace_document(
    freecad: object,
    workspace: DocumentAssetWorkspace,
    document: object,
    *,
    native_document: object | None = None,
) -> None:
    native = document if native_document is None else native_document
    name = document.Name
    if name in freecad.listDocuments():
        if freecad.getDocument(name) is not native:
            raise RuntimeError("ImagePlane document registry identity drifted")
        workspace.require_attached(document)
        freecad.closeDocument(name)
    workspace.release_after_close(document)


def _close_workspace_with_facts(
    freecad: object,
    workspace: DocumentAssetWorkspace,
    document: object,
    asset_root: Path,
    facts: dict[str, object],
) -> dict[str, object]:
    """Close one exact owned document before returning stable case evidence."""

    _close_workspace_document(freecad, workspace, document)
    _require(not tuple(asset_root.iterdir()), "Wave D document workspace leaked")
    return facts


def _execute_imageplane(
    freecad: object,
    descriptor: WaveDReviewedCaseDescriptor,
    temporary_root: Path,
) -> dict[str, object]:
    asset_root = temporary_root / "document-assets"
    staging_root = temporary_root / "staging"
    for root in (asset_root, staging_root):
        root.mkdir(mode=0o700)
        root.chmod(0o700)
    document = freecad.newDocument(f"VerifyImage_{descriptor.case.case_sha256[:8]}")
    document.UndoMode = 1
    workspace = DocumentAssetWorkspace(asset_root)
    workspace.attach_fresh_document(document)
    if descriptor.facet is ReviewedConformanceFacet.NEGATIVE:
        before = _document_snapshot(document)
        before_workspace = _workspace_manifest(Path(document.TransientDir))
        raw = _IMAGE_CREATE_PLAN.canonical_bytes
        try:
            apply_imageplane_plan(
                raw + b" ",
                expected_content_sha256=_content_sha256(raw),
                expected_plan_sha256=_IMAGE_CREATE_PLAN.plan_sha256,
                bindings=_image_bindings(_IMAGE_CREATE_PLAN, document, workspace, staging_root),
            )
        except ImagePlaneRuleError as error:
            _require(_same_document_snapshot(document, before), "ImagePlane reject mutated doc")
            _require(
                _workspace_manifest(Path(document.TransientDir)) == before_workspace,
                "ImagePlane reject mutated workspace",
            )
            _require(not tuple(staging_root.iterdir()), "ImagePlane reject leaked staging")
            facts = {
                "error_code": error.code.value,
                "error_path": error.path,
                "rollback_exact": True,
                "workspace_exact": True,
            }
            _close_workspace_document(freecad, workspace, document)
            _require(not tuple(asset_root.iterdir()), "ImagePlane reject leaked workspace")
            return facts
        raise RuntimeError("tampered ImagePlane plan was accepted")
    if descriptor.facet is ReviewedConformanceFacet.LATE_ROLLBACK:

        class FaultDocument:
            def __init__(self, inner: object) -> None:
                object.__setattr__(self, "inner", inner)
                object.__setattr__(self, "fail_once", False)

            def __getattr__(self, name: str):
                return getattr(self.inner, name)

            def __setattr__(self, name: str, value: object) -> None:
                if name in {"inner", "fail_once"}:
                    object.__setattr__(self, name, value)
                else:
                    setattr(self.inner, name, value)

            def recompute(self):
                result = self.inner.recompute()
                if self.fail_once:
                    self.fail_once = False
                    raise RuntimeError("Wave D late ImagePlane recompute fault")
                return result

        _close_workspace_document(freecad, workspace, document)
        inner = freecad.newDocument(f"VerifyImageRollback_{descriptor.case.case_sha256[:8]}")
        inner.UndoMode = 1
        fault = FaultDocument(inner)
        workspace = DocumentAssetWorkspace(asset_root)
        workspace.attach_fresh_document(fault)
        initial = _apply_image(_IMAGE_CREATE_PLAN, fault, workspace, staging_root)
        feature = inner.getObject(initial.object_name)
        before_objects = tuple(inner.Objects)
        before_state = _image_state(feature)
        before_workspace = _workspace_manifest(Path(fault.TransientDir))
        fault.fail_once = True
        try:
            _apply_image(_IMAGE_EDIT_PLAN, fault, workspace, staging_root)
        except ImagePlaneRuleError as error:
            _require(tuple(inner.Objects) == before_objects, "ImagePlane rollback object drift")
            _require(_image_state(feature) == before_state, "ImagePlane rollback state drift")
            _require(
                _workspace_manifest(Path(fault.TransientDir)) == before_workspace,
                "ImagePlane rollback workspace drift",
            )
            _require(not tuple(staging_root.iterdir()), "ImagePlane rollback leaked staging")
            _close_workspace_document(
                freecad,
                workspace,
                fault,
                native_document=inner,
            )
            _require(not tuple(asset_root.iterdir()), "ImagePlane rollback leaked workspace")
            return {
                "error_code": error.code.value,
                "error_path": error.path,
                "late_native_failure": True,
                "rollback_exact": True,
                "workspace_exact": True,
            }
        raise RuntimeError("late ImagePlane recompute fault was accepted")

    receipt = _apply_image(_IMAGE_CREATE_PLAN, document, workspace, staging_root)
    feature = document.getObject(receipt.object_name)
    _require(
        feature is not None
        and feature.TypeId == "Image::ImagePlane"
        and feature.isValid()
        and _content_sha256(Path(feature.ImageFile).read_bytes())
        == _content_sha256(_IMAGE_PAYLOAD),
        "ImagePlane create readback failed",
    )
    _require(not tuple(staging_root.iterdir()), "ImagePlane create leaked staging")
    if descriptor.facet is ReviewedConformanceFacet.CREATE:
        facts = {
            "disposition": receipt.disposition,
            "object_name": feature.Name,
            "receipt_sha256": receipt.receipt_sha256,
            "retained_alias": receipt.retained_alias,
            "state": _image_state(feature),
        }
        _close_workspace_document(freecad, workspace, document)
        _require(not tuple(asset_root.iterdir()), "ImagePlane create leaked workspace")
        return facts
    if descriptor.facet is ReviewedConformanceFacet.EDIT:
        before_name = feature.Name
        edited = _apply_image(_IMAGE_EDIT_PLAN, document, workspace, staging_root)
        edited_feature = document.getObject(edited.object_name)
        state = _image_state(edited_feature)
        _require(
            edited.disposition == "updated"
            and edited_feature is feature
            and edited_feature.Name == before_name
            and state["x_size_mm"] == 96.0
            and state["y_size_mm"] == 72.0,
            "ImagePlane reviewed edit failed",
        )
        facts = {
            "disposition": edited.disposition,
            "same_object": True,
            "state": state,
        }
        _close_workspace_document(freecad, workspace, document)
        _require(not tuple(asset_root.iterdir()), "ImagePlane edit leaked workspace")
        return facts
    if descriptor.facet is ReviewedConformanceFacet.RECOMPUTE:
        before = _image_state(feature)
        document.recompute()
        after = _image_state(feature)
        _require(before == after, "ImagePlane recompute drift")
        facts = {"recompute_stable": True, "state": after}
        _close_workspace_document(freecad, workspace, document)
        _require(not tuple(asset_root.iterdir()), "ImagePlane recompute leaked workspace")
        return facts
    save_path = temporary_root / f"{descriptor.case.case_sha256}.FCStd"
    document.saveAs(str(save_path))
    size = save_path.stat().st_size
    _require(size > 0, "ImagePlane save produced an empty FCStd")
    with zipfile.ZipFile(save_path) as archive:
        names = tuple(archive.namelist())
        _require(receipt.retained_alias in names, "ImagePlane retained file missing from FCStd")
        _require(
            _content_sha256(archive.read(receipt.retained_alias))
            == receipt.artifact_content_sha256,
            "ImagePlane FCStd retained bytes drifted",
        )
    if descriptor.facet is ReviewedConformanceFacet.SAVE:
        facts = {
            "embedded_alias": receipt.retained_alias,
            "format": "FCStd",
            "native_type_id": feature.TypeId,
            "nonempty": True,
            "saved": True,
        }
        _close_workspace_document(freecad, workspace, document)
        _require(not tuple(asset_root.iterdir()), "ImagePlane save leaked workspace")
        return facts
    _require(descriptor.facet is ReviewedConformanceFacet.REOPEN, "unexpected ImagePlane facet")
    object_name = feature.Name
    expected = _image_state(feature)
    _close_workspace_document(freecad, workspace, document)
    reopened = freecad.newDocument()
    reopened.UndoMode = 1
    reopened_workspace = DocumentAssetWorkspace(asset_root)
    reopened_workspace.attach_fresh_document(reopened)
    reopened.load(str(save_path))
    reopened.recompute()
    reopened_feature = reopened.getObject(object_name)
    _require(
        reopened_feature is not None
        and _image_state(reopened_feature) == expected
        and Path(reopened_feature.ImageFile).parent == Path(reopened.TransientDir),
        "ImagePlane reopen readback failed",
    )
    facts = {
        "format": "FCStd",
        "native_type_id": reopened_feature.TypeId,
        "nonempty": True,
        "reopened": True,
        "state": expected,
    }
    _close_workspace_document(freecad, reopened_workspace, reopened)
    _require(not tuple(asset_root.iterdir()), "ImagePlane reopen leaked workspace")
    return facts


_DESCRIPTOR_BY_CASE_SHA256: Final = MappingProxyType(
    {item.case.case_sha256: item for item in WAVE_D_REVIEWED_CASE_DESCRIPTORS}
)


def _execute_wave_d_case(
    freecad: object,
    case: ReviewedConformanceCase,
    challenge_sha256: str,
) -> bytes:
    descriptor = _DESCRIPTOR_BY_CASE_SHA256.get(case.case_sha256)
    if descriptor is None or descriptor.case != case:
        raise RuntimeError("unreviewed Wave D verification case")
    with tempfile.TemporaryDirectory(prefix="vibecad-wave-d-") as temporary:
        temporary_root = Path(temporary)
        if descriptor.family_id == PART_FILE_IMPORT_MANIFEST.family_id:
            facts = _execute_file_import(freecad, descriptor, temporary_root)
        elif descriptor.family_id == PART_OFFSET_MANIFEST.family_id:
            import Part  # type: ignore[import-not-found]  # noqa: PLC0415

            facts = _execute_offset(freecad, Part, descriptor, temporary_root)
        elif descriptor.family_id == IMAGEPLANE_MANIFEST.family_id:
            facts = _execute_imageplane(freecad, descriptor, temporary_root)
        else:
            raise RuntimeError("unreviewed Wave D verification family")
        return _observation(descriptor, challenge_sha256, facts)


@dataclass(frozen=True, slots=True, kw_only=True)
class WaveDManagedVerificationBatch:
    receipts: tuple[ReviewedVerificationReceipt, ...]
    promotion_bindings: tuple[FreeCadPromotionVerificationBinding, ...]

    def __post_init__(self) -> None:
        if (
            type(self.receipts) is not tuple
            or type(self.promotion_bindings) is not tuple
            or len(self.receipts) != len(WAVE_D_FAMILY_MANIFESTS)
            or len(self.promotion_bindings) != len(self.receipts)
            or any(type(item) is not ReviewedVerificationReceipt for item in self.receipts)
            or any(
                type(item) is not FreeCadPromotionVerificationBinding
                for item in self.promotion_bindings
            )
            or any(
                binding.test_receipt_sha256 != receipt.test_receipt_sha256
                or binding.test_contract_sha256 != receipt.test_contract_sha256
                for receipt, binding in zip(
                    self.receipts,
                    self.promotion_bindings,
                    strict=True,
                )
            )
        ):
            raise ValueError("Wave D managed verification batch is not exactly bound")

    @property
    def grants_execution_authority(self) -> bool:
        return False


def build_managed_freecad_wave_d_verification(
    *,
    freecad: object,
) -> WaveDManagedVerificationBatch:
    """Execute all 49 cases, then return three receipts and their bindings."""

    receipts = []
    for manifest, case_manifest in zip(
        WAVE_D_FAMILY_MANIFESTS,
        WAVE_D_REVIEWED_CASE_MANIFESTS,
        strict=True,
    ):

        def execute(case: ReviewedConformanceCase, challenge_sha256: str) -> bytes:
            return _execute_wave_d_case(freecad, case, challenge_sha256)

        host = build_managed_freecad_conformance_host(
            freecad=freecad,
            case_manifest=case_manifest,
            execute_case=execute,
            verifier_id=f"{WAVE_D_VERIFIER_ID}.{WAVE_D_VERIFIER_CONTRACT_SHA256[:16]}",
            verifier_version=WAVE_D_VERIFIER_VERSION,
        )
        receipts.append(
            build_reviewed_verification_receipt(
                manifest=manifest,
                case_manifest=case_manifest,
                host=host,
            )
        )
    receipt_tuple = tuple(receipts)
    return WaveDManagedVerificationBatch(
        receipts=receipt_tuple,
        promotion_bindings=tuple(
            build_promotion_verification_binding(item) for item in receipt_tuple
        ),
    )


__all__ = (
    "WAVE_D_FAMILY_MANIFESTS",
    "WAVE_D_REVIEWED_CASE_DESCRIPTORS",
    "WAVE_D_REVIEWED_CASE_MANIFESTS",
    "WAVE_D_VERIFIER_CONTRACT_SHA256",
    "WAVE_D_VERIFIER_ID",
    "WAVE_D_VERIFIER_VERSION",
    "WaveDManagedVerificationBatch",
    "WaveDReviewedCaseDescriptor",
    "build_managed_freecad_wave_d_verification",
)
