"""Canonical persistence contracts for organic derived-artifact generations."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

from vibecad.organic.contracts import (
    ORGANIC_SCHEMA_VERSION,
    AxisConvention,
    DerivedArtifact,
    DerivedArtifactKind,
    DerivedArtifactSet,
    MeshJobRequest,
    MeshMediaType,
    MeshOperation,
    MeshOperationKind,
    MeshOperationPlan,
    MeshProfile,
    MirrorAxis,
    SealedMeshSource,
)
from vibecad.organic.plan import mesh_operation_plan_digest

MAX_ORGANIC_MANIFEST_BYTES = 64 * 1024
MAX_ORGANIC_JSON_DEPTH = 16
MAX_ORGANIC_JSON_NODES = 2_048
MAX_ORGANIC_JSON_STRING_BYTES = 4_096
MAX_SAFE_JSON_INTEGER = 2**53 - 1

_BODY_DIGEST_DOMAIN = b"vibecad-organic-generation-manifest-v1\0"


class OrganicPersistenceError(ValueError):
    """Bounded persistence failure that does not reflect rejected bytes."""


@dataclass(frozen=True, slots=True, kw_only=True)
class OrganicGenerationManifest:
    request: MeshJobRequest
    result: DerivedArtifactSet
    body_sha256: str
    manifest_sha256: str
    schema_version: int = ORGANIC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.request) is not MeshJobRequest or type(self.result) is not DerivedArtifactSet:
            raise OrganicPersistenceError("invalid_manifest")
        if self.schema_version != ORGANIC_SCHEMA_VERSION:
            raise OrganicPersistenceError("unsupported_version")
        for value in (self.body_sha256, self.manifest_sha256):
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise OrganicPersistenceError("invalid_manifest")
        _validate_binding(self.request, self.result)


def _canonical_json(value: object, *, maximum: int = MAX_ORGANIC_MANIFEST_BYTES) -> bytes:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc
    if len(raw) > maximum:
        raise OrganicPersistenceError("manifest_too_large")
    return raw


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _parse_int(raw: str) -> int:
    value = int(raw)
    if not -MAX_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER:
        raise ValueError("unsafe integer")
    return value


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc


def _validate_json_tree(value: object, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_ORGANIC_JSON_NODES or depth > MAX_ORGANIC_JSON_DEPTH:
        raise OrganicPersistenceError("manifest_too_large")
    if type(value) is dict:
        if len(value) > 32:
            raise OrganicPersistenceError("manifest_too_large")
        for key, item in value.items():
            if type(key) is not str or _utf8_size(key) > 128:
                raise OrganicPersistenceError("invalid_manifest")
            _validate_json_tree(item, depth=depth + 1, nodes=nodes)
        return
    if type(value) is list:
        if len(value) > 128:
            raise OrganicPersistenceError("manifest_too_large")
        for item in value:
            _validate_json_tree(item, depth=depth + 1, nodes=nodes)
        return
    if type(value) is str:
        if _utf8_size(value) > MAX_ORGANIC_JSON_STRING_BYTES:
            raise OrganicPersistenceError("manifest_too_large")
        return
    if type(value) is int:
        if not -MAX_SAFE_JSON_INTEGER <= value <= MAX_SAFE_JSON_INTEGER:
            raise OrganicPersistenceError("invalid_manifest")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise OrganicPersistenceError("invalid_manifest")
        return
    if type(value) in {bool, type(None)}:
        return
    raise OrganicPersistenceError("invalid_manifest")


def _load_strict(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_ORGANIC_MANIFEST_BYTES:
        raise OrganicPersistenceError("manifest_too_large")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_int=_parse_int,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite")),
        )
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc
    if type(value) is not dict:
        raise OrganicPersistenceError("invalid_manifest")
    _validate_json_tree(value)
    if _canonical_json(value) != raw:
        raise OrganicPersistenceError("noncanonical_manifest")
    return value


def _exact_fields(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise OrganicPersistenceError("invalid_manifest")
    return value


def _operation_mapping(operation: MeshOperation) -> dict[str, object]:
    result: dict[str, object] = {
        "kind": operation.kind.value,
        "operation_id": operation.operation_id,
        "schema_version": operation.schema_version,
    }
    for name in ("distance_mm", "iterations", "target_triangles", "level"):
        value = getattr(operation, name)
        if value is not None:
            result[name] = value
    if operation.axis is not None:
        result["axis"] = operation.axis.value
    return result


def _request_mapping(request: MeshJobRequest) -> dict[str, object]:
    source = request.source
    plan = request.plan
    return {
        "generation": request.generation,
        "mesh_job_id": request.mesh_job_id,
        "plan": {
            "expected_boundary_loops": plan.expected_boundary_loops,
            "operations": [_operation_mapping(item) for item in plan.operations],
            "profile": plan.profile.value,
            "schema_version": plan.schema_version,
        },
        "schema_version": request.schema_version,
        "source": {
            "axis_convention": source.axis_convention.value,
            "byte_count": source.byte_count,
            "media_type": source.media_type.value,
            "millimeters_per_unit": source.millimeters_per_unit,
            "schema_version": source.schema_version,
            "sha256": source.sha256,
            "source_id": source.source_id,
            "triangle_count": source.triangle_count,
            "vertex_count": source.vertex_count,
        },
    }


def _result_mapping(result: DerivedArtifactSet) -> dict[str, object]:
    return {
        "artifacts": [
            {
                "artifact_id": artifact.artifact_id,
                "byte_count": artifact.byte_count,
                "kind": artifact.kind.value,
                "media_type": artifact.media_type,
                "schema_version": artifact.schema_version,
                "sha256": artifact.sha256,
            }
            for artifact in result.artifacts
        ],
        "generation": result.generation,
        "mesh_job_id": result.mesh_job_id,
        "plan_sha256": result.plan_sha256,
        "schema_version": result.schema_version,
        "source_sha256": result.source_sha256,
    }


def _body(request: MeshJobRequest, result: DerivedArtifactSet) -> dict[str, object]:
    _validate_binding(request, result)
    return {
        "authority": "derived_artifact_only",
        "correlation_semantics": "mesh_job_not_cad_task",
        "request": _request_mapping(request),
        "result": _result_mapping(result),
        "schema_version": ORGANIC_SCHEMA_VERSION,
    }


def _body_digest(body: object) -> str:
    return hashlib.sha256(_BODY_DIGEST_DOMAIN + _canonical_json(body)).hexdigest()


def _validate_binding(request: MeshJobRequest, result: DerivedArtifactSet) -> None:
    if (
        request.mesh_job_id != result.mesh_job_id
        or request.generation != result.generation
        or request.source.sha256 != result.source_sha256
        or mesh_operation_plan_digest(request.plan) != result.plan_sha256
        or request.correlation_semantics != "mesh_job_not_cad_task"
        or result.authority != "derived_artifact_only"
    ):
        raise OrganicPersistenceError("binding_mismatch")


def encode_organic_manifest(request: MeshJobRequest, result: DerivedArtifactSet) -> bytes:
    if type(request) is not MeshJobRequest or type(result) is not DerivedArtifactSet:
        raise OrganicPersistenceError("invalid_manifest")
    body = _body(request, result)
    envelope = {
        "body": body,
        "body_sha256": _body_digest(body),
        "schema_version": ORGANIC_SCHEMA_VERSION,
    }
    return _canonical_json(envelope)


def build_organic_manifest(
    request: MeshJobRequest,
    result: DerivedArtifactSet,
) -> OrganicGenerationManifest:
    raw = encode_organic_manifest(request, result)
    value = _load_strict(raw)
    return OrganicGenerationManifest(
        request=request,
        result=result,
        body_sha256=str(value["body_sha256"]),
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _decode_source(value: object) -> SealedMeshSource:
    fields = _exact_fields(
        value,
        {
            "axis_convention",
            "byte_count",
            "media_type",
            "millimeters_per_unit",
            "schema_version",
            "sha256",
            "source_id",
            "triangle_count",
            "vertex_count",
        },
    )
    try:
        return SealedMeshSource(
            source_id=fields["source_id"],  # type: ignore[arg-type]
            sha256=fields["sha256"],  # type: ignore[arg-type]
            media_type=MeshMediaType(fields["media_type"]),
            byte_count=fields["byte_count"],  # type: ignore[arg-type]
            vertex_count=fields["vertex_count"],  # type: ignore[arg-type]
            triangle_count=fields["triangle_count"],  # type: ignore[arg-type]
            millimeters_per_unit=fields["millimeters_per_unit"],  # type: ignore[arg-type]
            axis_convention=AxisConvention(fields["axis_convention"]),
            schema_version=fields["schema_version"],  # type: ignore[arg-type]
        )
    except (ValueError, TypeError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc


def _decode_operation(value: object) -> MeshOperation:
    if type(value) is not dict:
        raise OrganicPersistenceError("invalid_manifest")
    allowed = {
        "axis",
        "distance_mm",
        "iterations",
        "kind",
        "level",
        "operation_id",
        "schema_version",
        "target_triangles",
    }
    if not {"kind", "operation_id", "schema_version"}.issubset(value) or not set(value) <= allowed:
        raise OrganicPersistenceError("invalid_manifest")
    try:
        return MeshOperation(
            operation_id=value["operation_id"],  # type: ignore[arg-type]
            kind=MeshOperationKind(value["kind"]),
            distance_mm=value.get("distance_mm"),  # type: ignore[arg-type]
            iterations=value.get("iterations"),  # type: ignore[arg-type]
            target_triangles=value.get("target_triangles"),  # type: ignore[arg-type]
            axis=None if "axis" not in value else MirrorAxis(value["axis"]),
            level=value.get("level"),  # type: ignore[arg-type]
            schema_version=value["schema_version"],  # type: ignore[arg-type]
        )
    except (ValueError, TypeError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc


def _decode_plan(value: object) -> MeshOperationPlan:
    fields = _exact_fields(
        value,
        {"expected_boundary_loops", "operations", "profile", "schema_version"},
    )
    if type(fields["operations"]) is not list:
        raise OrganicPersistenceError("invalid_manifest")
    try:
        return MeshOperationPlan(
            profile=MeshProfile(fields["profile"]),
            operations=tuple(_decode_operation(item) for item in fields["operations"]),
            expected_boundary_loops=fields["expected_boundary_loops"],  # type: ignore[arg-type]
            schema_version=fields["schema_version"],  # type: ignore[arg-type]
        )
    except (ValueError, TypeError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc


def _decode_request(value: object) -> MeshJobRequest:
    fields = _exact_fields(
        value,
        {"generation", "mesh_job_id", "plan", "schema_version", "source"},
    )
    try:
        return MeshJobRequest(
            mesh_job_id=fields["mesh_job_id"],  # type: ignore[arg-type]
            generation=fields["generation"],  # type: ignore[arg-type]
            source=_decode_source(fields["source"]),
            plan=_decode_plan(fields["plan"]),
            schema_version=fields["schema_version"],  # type: ignore[arg-type]
        )
    except (ValueError, TypeError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc


def _decode_artifact(value: object) -> DerivedArtifact:
    fields = _exact_fields(
        value,
        {"artifact_id", "byte_count", "kind", "media_type", "schema_version", "sha256"},
    )
    try:
        return DerivedArtifact(
            artifact_id=fields["artifact_id"],  # type: ignore[arg-type]
            kind=DerivedArtifactKind(fields["kind"]),
            sha256=fields["sha256"],  # type: ignore[arg-type]
            byte_count=fields["byte_count"],  # type: ignore[arg-type]
            media_type=fields["media_type"],  # type: ignore[arg-type]
            schema_version=fields["schema_version"],  # type: ignore[arg-type]
        )
    except (ValueError, TypeError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc


def _decode_result(value: object) -> DerivedArtifactSet:
    fields = _exact_fields(
        value,
        {
            "artifacts",
            "generation",
            "mesh_job_id",
            "plan_sha256",
            "schema_version",
            "source_sha256",
        },
    )
    if type(fields["artifacts"]) is not list:
        raise OrganicPersistenceError("invalid_manifest")
    try:
        return DerivedArtifactSet(
            mesh_job_id=fields["mesh_job_id"],  # type: ignore[arg-type]
            generation=fields["generation"],  # type: ignore[arg-type]
            source_sha256=fields["source_sha256"],  # type: ignore[arg-type]
            plan_sha256=fields["plan_sha256"],  # type: ignore[arg-type]
            artifacts=tuple(_decode_artifact(item) for item in fields["artifacts"]),
            schema_version=fields["schema_version"],  # type: ignore[arg-type]
        )
    except (ValueError, TypeError) as exc:
        raise OrganicPersistenceError("invalid_manifest") from exc


def decode_organic_manifest(raw: object) -> OrganicGenerationManifest:
    value = _load_strict(raw)
    envelope = _exact_fields(value, {"body", "body_sha256", "schema_version"})
    if envelope["schema_version"] != ORGANIC_SCHEMA_VERSION:
        raise OrganicPersistenceError("unsupported_version")
    body = _exact_fields(
        envelope["body"],
        {"authority", "correlation_semantics", "request", "result", "schema_version"},
    )
    if (
        body["schema_version"] != ORGANIC_SCHEMA_VERSION
        or body["authority"] != "derived_artifact_only"
        or body["correlation_semantics"] != "mesh_job_not_cad_task"
        or type(envelope["body_sha256"]) is not str
        or _body_digest(body) != envelope["body_sha256"]
    ):
        raise OrganicPersistenceError("binding_mismatch")
    request = _decode_request(body["request"])
    result = _decode_result(body["result"])
    _validate_binding(request, result)
    if encode_organic_manifest(request, result) != raw:
        raise OrganicPersistenceError("noncanonical_manifest")
    return OrganicGenerationManifest(
        request=request,
        result=result,
        body_sha256=envelope["body_sha256"],
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
    )


__all__ = (
    "MAX_ORGANIC_MANIFEST_BYTES",
    "OrganicGenerationManifest",
    "OrganicPersistenceError",
    "build_organic_manifest",
    "decode_organic_manifest",
    "encode_organic_manifest",
)
