"""Reviewed FreeCAD rules for detached imports of authenticated CAD artifacts.

The graph and backend plan carry only a content-bound artifact reference.  Raw
artifact bytes are resolved at the trusted-host boundary through
``ArtifactReader`` and written to a private, bounded staging directory.  The
native importer is selected exclusively by the static table below.  Imported
topology is detached from ``FileName`` before the staging file is removed and
before the document transaction commits.

Neither a plan nor a receipt grants execution authority, and neither contains
the temporary host path or the imported bytes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Self

from vibecad.intent_bridge.contracts import DocumentRef, IntentBridgeError
from vibecad.intent_bridge.ports import ArtifactReader, read_verified_document
from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionRunner,
)

PART_FILE_IMPORT_PLAN_SCHEMA_VERSION: Final = 1
PART_FILE_IMPORT_PLAN_MEDIA_TYPE: Final = (
    "application/vnd.vibecad.freecad-part-file-import-plan+json"
)
MAX_PART_FILE_IMPORT_PLAN_BYTES: Final = 64 * 1024
MAX_PART_FILE_IMPORT_ARTIFACT_BYTES: Final = 4 * 1024 * 1024
MAX_IMPORTED_TOPOLOGY_ITEMS: Final = 100_000
PART_FILE_IMPORT_FREECAD_ENGINE_BUILD_ID: Final = "34a9716668b1ddeb55b914f1c5be644826bdbbbf"
PART_FILE_IMPORT_RULE_ID: Final = "freecad.part-file-import.reviewed.v1"

_RULE_CONTRACT_DOMAIN = b"vibecad.freecad-part-file-import.rule-contract.v1\0"
_PLAN_DIGEST_DOMAIN = b"vibecad.freecad-part-file-import.plan.v1\0"
_RECEIPT_DIGEST_DOMAIN = b"vibecad.freecad-part-file-import.receipt.v1\0"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SHAPE_BREP_BYTES = 16 * 1024 * 1024
_ALLOWED_SUFFIXES = frozenset({".brep", ".iges", ".step"})


class PartFileImportOperation(StrEnum):
    BREP = "brep"
    IGES = "iges"
    STEP = "step"


class PartFileImportRuleErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INTEGRITY_FAILURE = "integrity_failure"
    PRECONDITION_FAILED = "precondition_failed"
    CONFORMANCE_FAILED = "conformance_failed"
    TRANSACTION_FAILED = "transaction_failed"
    STAGING_FAILED = "staging_failed"


class PartFileImportRuleError(ValueError):
    """Bounded stable failure at the reviewed file-import boundary."""

    def __init__(self, code: PartFileImportRuleErrorCode, path: str = "/") -> None:
        if type(code) is not PartFileImportRuleErrorCode:
            raise TypeError("code must be a PartFileImportRuleErrorCode")
        try:
            size = len(path.encode("utf-8")) if type(path) is str else 0
        except UnicodeError:
            size = 385
        if (
            type(path) is not str
            or not path.startswith("/")
            or not path.isprintable()
            or len(path.splitlines()) != 1
            or size > 384
        ):
            path = "/"
        self.code = code
        self.path = path
        super().__init__(f"Part file import rule error ({code.value}) at {path}")


def _fail(code: PartFileImportRuleErrorCode, path: str = "/") -> None:
    raise PartFileImportRuleError(code, path)


def _identifier(value: object, path: str) -> str:
    if type(value) is not str or len(value) > 128 or _IDENTIFIER.fullmatch(value) is None:
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT, path)
    return value


def _digest(value: object, path: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT, path)
    return value


def _exact_fields(value: object, keys: set[str], path: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT, path)
    return value


def _canonical_json(value: object, *, maximum: int = MAX_PART_FILE_IMPORT_PLAN_BYTES) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT)
    if not payload or len(payload) > maximum:
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT)
    return payload


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError
        result[key] = value
    return result


def _decode_mapping(raw: object) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_PART_FILE_IMPORT_PLAN_BYTES:
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT)
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicates)
    except _DuplicateKeyError:
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE)
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT)
    if type(value) is not dict or not hmac.compare_digest(raw, _canonical_json(value)):
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE)
    return value


@dataclass(frozen=True, slots=True)
class NativePartFileImportSpec:
    type_id: str
    object_prefix: str
    native_operation: str
    native_property_names: tuple[str, ...]
    artifact_role_term_ref_id: str
    artifact_schema_term_ref_id: str
    artifact_value_type_term_ref_id: str
    artifact_media_type: str
    staging_suffix: str


_ARTIFACT_ROLE_TERM_REF_ID = "role_part_file_import_artifact"

PART_FILE_IMPORT_NATIVE_SPECS: Final = MappingProxyType(
    {
        PartFileImportOperation.BREP: NativePartFileImportSpec(
            "Part::ImportBrep",
            "ImportBrep",
            "import_authenticated_brep_snapshot",
            ("FileName",),
            _ARTIFACT_ROLE_TERM_REF_ID,
            "schema_part_brep_artifact_v1",
            "type_part_brep_artifact",
            "model/vnd.opencascade.brep",
            ".brep",
        ),
        PartFileImportOperation.IGES: NativePartFileImportSpec(
            "Part::ImportIges",
            "ImportIges",
            "import_authenticated_iges_snapshot",
            ("FileName",),
            _ARTIFACT_ROLE_TERM_REF_ID,
            "schema_part_iges_artifact_v1",
            "type_part_iges_artifact",
            "model/iges",
            ".iges",
        ),
        PartFileImportOperation.STEP: NativePartFileImportSpec(
            "Part::ImportStep",
            "ImportStep",
            "import_authenticated_step_snapshot",
            ("FileName",),
            _ARTIFACT_ROLE_TERM_REF_ID,
            "schema_part_step_artifact_v1",
            "type_part_step_artifact",
            "model/step",
            ".step",
        ),
    }
)


def _contract_mapping() -> dict[str, object]:
    return {
        "engine": {
            "name": "FreeCAD",
            "version": "1.1.0",
            "build_id": PART_FILE_IMPORT_FREECAD_ENGINE_BUILD_ID,
        },
        "operations": [
            {
                "operation": operation.value,
                "type_id": spec.type_id,
                "native_operation": spec.native_operation,
                "properties": list(spec.native_property_names),
                "artifact": {
                    "role_term_ref_id": spec.artifact_role_term_ref_id,
                    "schema_term_ref_id": spec.artifact_schema_term_ref_id,
                    "value_type_term_ref_id": spec.artifact_value_type_term_ref_id,
                    "media_type": spec.artifact_media_type,
                    "suffix": spec.staging_suffix,
                    "maximum_bytes": MAX_PART_FILE_IMPORT_ARTIFACT_BYTES,
                },
            }
            for operation, spec in PART_FILE_IMPORT_NATIVE_SPECS.items()
        ],
        "fixed": {
            "artifact_resolution": "ArtifactReader-exact-sha256",
            "staging": "host-owned-private-bounded-no-symlink",
            "native_selection": "static-reviewed-table",
            "result": "detached-topology-snapshot",
            "transaction": "exact-rollback",
        },
        "excluded_alias": {
            "type_id": "Part::CurveNet",
            "reason": "extension-router-for-brep-iges-step",
        },
    }


PART_FILE_IMPORT_RULE_CONTRACT_SHA256: Final = hashlib.sha256(
    _RULE_CONTRACT_DOMAIN + _canonical_json(_contract_mapping())
).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class PartFileImportBackendPlan:
    source_artifact_id: str
    source_graph_id: str
    source_graph_sha256: str
    source_content_sha256: str
    lowering_request_sha256: str
    adapter_contract_sha256: str
    manifest_sha256: str
    operation_specification_sha256: str
    body_id: str
    node_id: str
    result_id: str
    operation: PartFileImportOperation
    artifact_id: str
    artifact_content_sha256: str
    artifact_role_term_ref_id: str
    artifact_schema_term_ref_id: str
    artifact_value_type_term_ref_id: str
    artifact_media_type: str
    schema_version: int = PART_FILE_IMPORT_PLAN_SCHEMA_VERSION
    canonical_bytes: bytes = field(init=False, repr=False)
    plan_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PART_FILE_IMPORT_PLAN_SCHEMA_VERSION
        ):
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/schema_version")
        for name in (
            "source_artifact_id",
            "source_graph_id",
            "body_id",
            "node_id",
            "result_id",
            "artifact_id",
            "artifact_role_term_ref_id",
            "artifact_schema_term_ref_id",
            "artifact_value_type_term_ref_id",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/{name}"))
        for name in (
            "source_graph_sha256",
            "source_content_sha256",
            "lowering_request_sha256",
            "adapter_contract_sha256",
            "manifest_sha256",
            "operation_specification_sha256",
            "artifact_content_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/{name}"))
        if type(self.operation) is not PartFileImportOperation:
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/operation")
        spec = PART_FILE_IMPORT_NATIVE_SPECS[self.operation]
        if (
            self.artifact_role_term_ref_id != spec.artifact_role_term_ref_id
            or self.artifact_schema_term_ref_id != spec.artifact_schema_term_ref_id
            or self.artifact_value_type_term_ref_id != spec.artifact_value_type_term_ref_id
            or self.artifact_media_type != spec.artifact_media_type
        ):
            _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/artifact/contract")
        payload = _canonical_json(self.to_mapping())
        object.__setattr__(self, "canonical_bytes", payload)
        object.__setattr__(
            self,
            "plan_sha256",
            hashlib.sha256(_PLAN_DIGEST_DOMAIN + payload).hexdigest(),
        )

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority": "none",
            "source": {
                "artifact_id": self.source_artifact_id,
                "graph_id": self.source_graph_id,
                "graph_sha256": self.source_graph_sha256,
                "content_sha256": self.source_content_sha256,
            },
            "lowering_request_sha256": self.lowering_request_sha256,
            "adapter_contract_sha256": self.adapter_contract_sha256,
            "manifest_sha256": self.manifest_sha256,
            "operation_specification_sha256": self.operation_specification_sha256,
            "target": {
                "body_id": self.body_id,
                "node_id": self.node_id,
                "result_id": self.result_id,
            },
            "operation": self.operation.value,
            "artifact": {
                "artifact_id": self.artifact_id,
                "content_sha256": self.artifact_content_sha256,
                "role_term_ref_id": self.artifact_role_term_ref_id,
                "schema_term_ref_id": self.artifact_schema_term_ref_id,
                "value_type_term_ref_id": self.artifact_value_type_term_ref_id,
                "media_type": self.artifact_media_type,
            },
        }

    @classmethod
    def from_mapping(cls, value: object) -> Self:
        fields = _exact_fields(
            value,
            {
                "schema_version",
                "authority",
                "source",
                "lowering_request_sha256",
                "adapter_contract_sha256",
                "manifest_sha256",
                "operation_specification_sha256",
                "target",
                "operation",
                "artifact",
            },
            "/",
        )
        if fields["authority"] != "none":
            _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/authority")
        source = _exact_fields(
            fields["source"],
            {"artifact_id", "graph_id", "graph_sha256", "content_sha256"},
            "/source",
        )
        target = _exact_fields(fields["target"], {"body_id", "node_id", "result_id"}, "/target")
        artifact = _exact_fields(
            fields["artifact"],
            {
                "artifact_id",
                "content_sha256",
                "role_term_ref_id",
                "schema_term_ref_id",
                "value_type_term_ref_id",
                "media_type",
            },
            "/artifact",
        )
        try:
            operation = PartFileImportOperation(fields["operation"])
        except (TypeError, ValueError):
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/operation")
        return cls(
            schema_version=fields["schema_version"],
            source_artifact_id=source["artifact_id"],
            source_graph_id=source["graph_id"],
            source_graph_sha256=source["graph_sha256"],
            source_content_sha256=source["content_sha256"],
            lowering_request_sha256=fields["lowering_request_sha256"],
            adapter_contract_sha256=fields["adapter_contract_sha256"],
            manifest_sha256=fields["manifest_sha256"],
            operation_specification_sha256=fields["operation_specification_sha256"],
            body_id=target["body_id"],
            node_id=target["node_id"],
            result_id=target["result_id"],
            operation=operation,
            artifact_id=artifact["artifact_id"],
            artifact_content_sha256=artifact["content_sha256"],
            artifact_role_term_ref_id=artifact["role_term_ref_id"],
            artifact_schema_term_ref_id=artifact["schema_term_ref_id"],
            artifact_value_type_term_ref_id=artifact["value_type_term_ref_id"],
            artifact_media_type=artifact["media_type"],
        )


def decode_part_file_import_backend_plan(
    raw: bytes,
    *,
    expected_content_sha256: str | None = None,
    expected_plan_sha256: str | None = None,
) -> PartFileImportBackendPlan:
    result = PartFileImportBackendPlan.from_mapping(_decode_mapping(raw))
    if type(raw) is not bytes or not hmac.compare_digest(raw, result.canonical_bytes):
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE)
    if expected_content_sha256 is not None and not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(),
        _digest(expected_content_sha256, "/expected_content_sha256"),
    ):
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/content_sha256")
    if expected_plan_sha256 is not None and not hmac.compare_digest(
        result.plan_sha256,
        _digest(expected_plan_sha256, "/expected_plan_sha256"),
    ):
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/plan_sha256")
    return result


def _validate_staging_parent(path: object) -> tuple[Path, int, int]:
    if not isinstance(path, Path) or not path.is_absolute():
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/stager/root")
    try:
        info = path.lstat()
    except (OSError, ValueError, RuntimeError):
        _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/stager/root")
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/stager/root")
    return path, info.st_dev, info.st_ino


class _StagedImportLease:
    __slots__ = ("_active", "_directory", "_path")

    def __init__(self, directory: Path, path: Path) -> None:
        self._directory = directory
        self._path = path
        self._active = True

    @property
    def path(self) -> Path:
        if not self._active:
            _fail(PartFileImportRuleErrorCode.STAGING_FAILED, "/stager/lease")
        return self._path

    def verify(self) -> None:
        if not self._active:
            _fail(PartFileImportRuleErrorCode.STAGING_FAILED, "/stager/lease")
        try:
            info = self._path.lstat()
            directory_info = self._directory.lstat()
        except (OSError, ValueError, RuntimeError):
            _fail(PartFileImportRuleErrorCode.STAGING_FAILED, "/stager/lease")
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or stat.S_ISLNK(directory_info.st_mode)
            or not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.geteuid()
            or directory_info.st_mode & 0o077
            or self._path.parent != self._directory
        ):
            _fail(PartFileImportRuleErrorCode.STAGING_FAILED, "/stager/lease")

    def close(self) -> None:
        if not self._active:
            return
        self.verify()
        try:
            self._path.unlink()
            self._directory.rmdir()
        except OSError:
            _fail(PartFileImportRuleErrorCode.STAGING_FAILED, "/stager/cleanup")
        self._active = False
        if self._path.exists() or self._directory.exists():
            _fail(PartFileImportRuleErrorCode.STAGING_FAILED, "/stager/cleanup")

    def __enter__(self) -> Self:
        self.verify()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        del exc_type, exc, traceback
        if self._active:
            self.close()
        return False


class HostOwnedImportStager:
    """Create one private, single-file staging directory per import.

    The root is trusted host configuration, never graph or plan input.  The
    constructor records its inode and rejects symlink or shared-writable roots;
    every stage revalidates that identity before creating a 0700 child.
    """

    __slots__ = ("_device", "_inode", "_root")

    def __init__(self, root: Path) -> None:
        checked, device, inode = _validate_staging_parent(root)
        self._root = checked
        self._device = device
        self._inode = inode

    def stage_exact(
        self,
        payload: bytes,
        *,
        suffix: str,
        expected_content_sha256: str,
    ) -> _StagedImportLease:
        if (
            type(payload) is not bytes
            or not 1 <= len(payload) <= MAX_PART_FILE_IMPORT_ARTIFACT_BYTES
            or type(suffix) is not str
            or suffix not in _ALLOWED_SUFFIXES
        ):
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/stager/payload")
        expected = _digest(expected_content_sha256, "/stager/content_sha256")
        if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected):
            _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/stager/content_sha256")
        root, device, inode = _validate_staging_parent(self._root)
        if device != self._device or inode != self._inode:
            _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/stager/root")
        directory: Path | None = None
        staged: Path | None = None
        descriptor: int | None = None
        transferred = False
        try:
            directory = Path(tempfile.mkdtemp(prefix=".vibecad-import-", dir=root))
            os.chmod(directory, 0o700)
            staged = directory / f"artifact{suffix}"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(staged, flags, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short staging write")
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            lease = _StagedImportLease(directory, staged)
            lease.verify()
            transferred = True
            return lease
        except PartFileImportRuleError:
            raise
        except (OSError, ValueError, RuntimeError, SystemExit):
            _fail(PartFileImportRuleErrorCode.STAGING_FAILED, "/stager/write")
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not transferred and staged is not None and staged.exists():
                try:
                    staged.unlink()
                except OSError:
                    pass
            if not transferred and directory is not None and directory.exists():
                try:
                    directory.rmdir()
                except OSError:
                    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class PartFileImportExecutionBindings:
    document: object
    artifact_document: DocumentRef
    artifacts: ArtifactReader
    stager: HostOwnedImportStager
    body_id: str
    expected_adapter_contract_sha256: str
    expected_manifest_sha256: str
    expected_operation_specification_sha256: str

    def __post_init__(self) -> None:
        if self.document is None:
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/bindings/document")
        if type(self.artifact_document) is not DocumentRef or not isinstance(
            self.artifacts, ArtifactReader
        ):
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/bindings/artifact")
        if type(self.stager) is not HostOwnedImportStager:
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/bindings/stager")
        object.__setattr__(self, "body_id", _identifier(self.body_id, "/bindings/body_id"))
        for name in (
            "expected_adapter_contract_sha256",
            "expected_manifest_sha256",
            "expected_operation_specification_sha256",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), f"/bindings/{name}"))


@dataclass(frozen=True, slots=True, kw_only=True)
class PartFileImportConformanceReceipt:
    plan_sha256: str
    operation: PartFileImportOperation
    object_name: str
    artifact_id: str
    artifact_content_sha256: str
    artifact_size_bytes: int
    result_shape_type: str
    result_shape_sha256: str
    edge_count: int
    face_count: int
    solid_count: int
    receipt_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _digest(self.plan_sha256, "/receipt/plan"))
        if type(self.operation) is not PartFileImportOperation:
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/receipt/operation")
        for name in ("object_name", "artifact_id"):
            object.__setattr__(self, name, _identifier(getattr(self, name), f"/receipt/{name}"))
        object.__setattr__(
            self,
            "artifact_content_sha256",
            _digest(self.artifact_content_sha256, "/receipt/artifact_content_sha256"),
        )
        if (
            type(self.artifact_size_bytes) is not int
            or not 1 <= self.artifact_size_bytes <= MAX_PART_FILE_IMPORT_ARTIFACT_BYTES
            or type(self.result_shape_type) is not str
            or not 1 <= len(self.result_shape_type) <= 32
        ):
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/receipt/result")
        object.__setattr__(
            self,
            "result_shape_sha256",
            _digest(self.result_shape_sha256, "/receipt/result_shape_sha256"),
        )
        counts = (self.edge_count, self.face_count, self.solid_count)
        if any(
            type(item) is not int or not 0 <= item <= MAX_IMPORTED_TOPOLOGY_ITEMS for item in counts
        ):
            _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/receipt/counts")
        body = {
            "authority": "none",
            "plan_sha256": self.plan_sha256,
            "operation": self.operation.value,
            "object_name": self.object_name,
            "artifact": {
                "artifact_id": self.artifact_id,
                "content_sha256": self.artifact_content_sha256,
                "size_bytes": self.artifact_size_bytes,
            },
            "result": {
                "shape_type": self.result_shape_type,
                "shape_sha256": self.result_shape_sha256,
                "edge_count": self.edge_count,
                "face_count": self.face_count,
                "solid_count": self.solid_count,
            },
        }
        object.__setattr__(
            self,
            "receipt_sha256",
            hashlib.sha256(_RECEIPT_DIGEST_DOMAIN + _canonical_json(body)).hexdigest(),
        )

    @property
    def executable(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False


def _shape_signature(shape: object) -> tuple[str, str, int, int, int, int, float, float, float]:
    try:
        if shape is None or shape.isNull() or not shape.isValid():
            _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result/shape")
        shape_type = str(shape.ShapeType)
        vertices = len(shape.Vertexes)
        edges = len(shape.Edges)
        faces = len(shape.Faces)
        solids = len(shape.Solids)
        length = float(shape.Length)
        area = float(shape.Area)
        volume = float(shape.Volume)
        raw = shape.exportBrepToString().encode("utf-8")
    except PartFileImportRuleError:
        raise
    except (Exception, SystemExit, UnicodeError, OverflowError):
        _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result/shape")
    counts = (vertices, edges, faces, solids)
    measures = (length, area, volume)
    if (
        not 1 <= sum(counts) <= 4 * MAX_IMPORTED_TOPOLOGY_ITEMS
        or any(item < 0 or item > MAX_IMPORTED_TOPOLOGY_ITEMS for item in counts)
        or any(not math.isfinite(item) or item < 0.0 for item in measures)
        or not raw
        or len(raw) > _MAX_SHAPE_BREP_BYTES
    ):
        _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result/shape")
    return (
        hashlib.sha256(raw).hexdigest(),
        shape_type,
        vertices,
        edges,
        faces,
        solids,
        length,
        area,
        volume,
    )


def _validate_detached_import(
    result: object,
    *,
    expected_type_id: str,
    expected_signature: tuple[str, str, int, int, int, int, float, float, float] | None = None,
) -> tuple[str, str, int, int, int, int, float, float, float]:
    try:
        if (
            result.TypeId != expected_type_id
            or not result.isValid()
            or tuple(result.State) != ("Up-to-date",)
            or result.FileName != ""
        ):
            _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result/state")
        signature = _shape_signature(result.Shape)
    except PartFileImportRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result")
    if expected_signature is not None:
        same_topology_metrics = signature[1:6] == expected_signature[1:6] and all(
            math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-9)
            for actual, expected in zip(signature[6:], expected_signature[6:], strict=True)
        )
        if not same_topology_metrics:
            _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result/detached_shape")
    return signature


def _verified_artifact_payload(
    plan: PartFileImportBackendPlan,
    bindings: PartFileImportExecutionBindings,
) -> bytes:
    artifact = bindings.artifact_document
    spec = PART_FILE_IMPORT_NATIVE_SPECS[plan.operation]
    if (
        artifact.artifact_id != plan.artifact_id
        or not hmac.compare_digest(artifact.content_sha256, plan.artifact_content_sha256)
        or not hmac.compare_digest(artifact.document_digest, plan.artifact_content_sha256)
        or artifact.document_id != f"part_file_import_{plan.artifact_content_sha256[:32]}"
        or artifact.role_term_ref_id != spec.artifact_role_term_ref_id
        or artifact.schema_term_ref_id != spec.artifact_schema_term_ref_id
        or artifact.media_type != spec.artifact_media_type
        or artifact.size_bytes > MAX_PART_FILE_IMPORT_ARTIFACT_BYTES
    ):
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/bindings/artifact")
    try:
        return read_verified_document(
            bindings.artifacts,
            artifact,
            maximum_bytes=MAX_PART_FILE_IMPORT_ARTIFACT_BYTES,
        )
    except IntentBridgeError:
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/bindings/artifact/payload")
    except SystemExit:
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/bindings/artifact/payload")


def _validate_execution_bindings(
    plan: PartFileImportBackendPlan,
    bindings: PartFileImportExecutionBindings,
) -> tuple[object, bytes]:
    if (
        bindings.body_id != plan.body_id
        or not hmac.compare_digest(
            plan.adapter_contract_sha256,
            bindings.expected_adapter_contract_sha256,
        )
        or not hmac.compare_digest(plan.manifest_sha256, bindings.expected_manifest_sha256)
        or not hmac.compare_digest(
            plan.operation_specification_sha256,
            bindings.expected_operation_specification_sha256,
        )
    ):
        _fail(PartFileImportRuleErrorCode.INTEGRITY_FAILURE, "/bindings")
    document = bindings.document
    try:
        if getattr(document, "UndoMode", 0) != 1 or bool(document.HasPendingTransaction):
            _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    except PartFileImportRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/bindings/document")
    return document, _verified_artifact_payload(plan, bindings)


def apply_part_file_import_plan(
    raw: bytes,
    *,
    expected_content_sha256: str,
    expected_plan_sha256: str,
    bindings: PartFileImportExecutionBindings,
) -> PartFileImportConformanceReceipt:
    """Import one exact artifact snapshot at the explicit trusted-host seam."""

    if type(bindings) is not PartFileImportExecutionBindings:
        _fail(PartFileImportRuleErrorCode.INVALID_INPUT, "/bindings")
    try:
        import FreeCAD  # type: ignore[import-not-found]  # noqa: PLC0415

        version = tuple(FreeCAD.Version())
    except (Exception, SystemExit):
        _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/engine")
    if (
        version[:3] != ("1", "1", "0")
        or len(version) < 8
        or version[7] != PART_FILE_IMPORT_FREECAD_ENGINE_BUILD_ID
    ):
        _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/engine")
    plan = decode_part_file_import_backend_plan(
        raw,
        expected_content_sha256=expected_content_sha256,
        expected_plan_sha256=expected_plan_sha256,
    )
    document, artifact_payload = _validate_execution_bindings(plan, bindings)
    spec = PART_FILE_IMPORT_NATIVE_SPECS[plan.operation]
    object_name = f"{spec.object_prefix}_{plan.plan_sha256[:16]}"
    try:
        if document.getObject(object_name) is not None:
            _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/document/object_name")
        before_objects = tuple(document.Objects)
    except PartFileImportRuleError:
        raise
    except (Exception, SystemExit):
        _fail(PartFileImportRuleErrorCode.PRECONDITION_FAILED, "/document")

    holder: list[tuple[object, tuple[str, str, int, int, int, int, float, float, float]]] = []

    def snapshot() -> object:
        return before_objects

    def rollback_matches(before: object) -> bool:
        try:
            current = tuple(document.Objects)
            return (
                type(before) is tuple
                and len(current) == len(before)
                and all(left is right for left, right in zip(current, before, strict=True))
                and document.getObject(object_name) is None
            )
        except (Exception, SystemExit):
            return False

    with bindings.stager.stage_exact(
        artifact_payload,
        suffix=spec.staging_suffix,
        expected_content_sha256=plan.artifact_content_sha256,
    ) as lease:

        def create() -> object:
            result = document.addObject(spec.type_id, object_name)
            lease.verify()
            result.FileName = str(lease.path)
            document.recompute()
            try:
                if (
                    result.TypeId != spec.type_id
                    or not result.isValid()
                    or tuple(result.State) != ("Up-to-date",)
                    or result.FileName != str(lease.path)
                ):
                    _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result/import")
                imported_signature = _shape_signature(result.Shape)
                detached_shape = result.Shape.copy()
                result.FileName = ""
                result.Shape = detached_shape
                result.purgeTouched()
                document.recompute()
            except PartFileImportRuleError:
                raise
            except (Exception, SystemExit):
                _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result/import")
            signature = _validate_detached_import(
                result,
                expected_type_id=spec.type_id,
                expected_signature=imported_signature,
            )
            lease.close()
            document.recompute()
            signature = _validate_detached_import(
                result,
                expected_type_id=spec.type_id,
                expected_signature=signature,
            )
            holder.append((result, signature))
            return result

        try:
            result = NativeTransactionRunner().run(
                document,
                label=f"VibeCAD reviewed {spec.native_operation}",
                snapshot=snapshot,
                apply=create,
                rollback_matches=rollback_matches,
            )
        except NativeTransactionError:
            _fail(PartFileImportRuleErrorCode.TRANSACTION_FAILED, "/document/transaction")

    if len(holder) != 1 or holder[0][0] is not result:
        _fail(PartFileImportRuleErrorCode.CONFORMANCE_FAILED, "/result")
    signature = holder[0][1]
    return PartFileImportConformanceReceipt(
        plan_sha256=plan.plan_sha256,
        operation=plan.operation,
        object_name=object_name,
        artifact_id=plan.artifact_id,
        artifact_content_sha256=plan.artifact_content_sha256,
        artifact_size_bytes=len(artifact_payload),
        result_shape_type=signature[1],
        result_shape_sha256=signature[0],
        edge_count=signature[3],
        face_count=signature[4],
        solid_count=signature[5],
    )


__all__ = [
    "MAX_PART_FILE_IMPORT_ARTIFACT_BYTES",
    "MAX_PART_FILE_IMPORT_PLAN_BYTES",
    "PART_FILE_IMPORT_FREECAD_ENGINE_BUILD_ID",
    "PART_FILE_IMPORT_NATIVE_SPECS",
    "PART_FILE_IMPORT_PLAN_MEDIA_TYPE",
    "PART_FILE_IMPORT_RULE_CONTRACT_SHA256",
    "PART_FILE_IMPORT_RULE_ID",
    "HostOwnedImportStager",
    "NativePartFileImportSpec",
    "PartFileImportBackendPlan",
    "PartFileImportConformanceReceipt",
    "PartFileImportExecutionBindings",
    "PartFileImportOperation",
    "PartFileImportRuleError",
    "PartFileImportRuleErrorCode",
    "apply_part_file_import_plan",
    "decode_part_file_import_backend_plan",
]
