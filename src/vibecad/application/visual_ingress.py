"""Strict host-edge ingress for descriptor-bound visual image sets."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
import sys
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from vibecad._file_compat import (
    WindowsPathCapability,
    capture_windows_fd,
    open_private_file,
    validate_windows_path,
    windows_extended_path,
)
from vibecad._file_compat import (
    pread as portable_pread,
)
from vibecad.visual.contracts import (
    MAX_IMAGE_SET_ITEMS,
    MAX_IMAGE_SET_RECORD_BYTES,
    MAX_IMAGE_SET_SOURCE_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
    VISUAL_SCHEMA_VERSION,
    CalibrationEvidence,
    DimensionHint,
    ProcessingAuthorization,
    VisualContractError,
    VisualContractErrorCode,
)
from vibecad.visual.inputs import (
    DescriptorSource,
    ImageIngress,
    SealImageSetRequest,
    VisualInputStoreError,
    VisualInputStoreErrorCode,
    bind_visual_input_locator,
)

_STAGING_LOCATOR_DOMAIN = b"vibecad-visual-staging-locator-v1\0"
_STAGING_LOCATOR_FIELDS = {
    "schema_version",
    "dev",
    "ino",
    "mode",
    "uid",
    "nlink",
    "mtime_ns",
    "ctime_ns",
    "source_locators",
    "source_sha256",
    "digest",
}
_SOURCE_LOCATOR_FIELDS = {
    "schema_version",
    "dev",
    "ino",
    "mode",
    "uid",
    "nlink",
    "size",
    "mtime_ns",
    "ctime_ns",
    "digest",
}
_REQUEST_FIELDS = {
    "schema_version",
    "create_key",
    "inputs",
    "unit",
    "dimension_hints",
    "calibration_evidence",
    "same_object",
    "same_state",
    "same_scale",
    "processing_authorization",
}
_INPUT_FIELDS = {
    "schema_version",
    "view_role",
    "calibration_status",
    "declared_mime",
}


class VisualIngressErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTEGRITY_FAILURE = "integrity_failure"


class VisualIngressError(RuntimeError):
    """Bounded host-ingress failure that never reflects local metadata."""

    __slots__ = ("code",)

    def __init__(self, code: VisualIngressErrorCode) -> None:
        if type(code) is not VisualIngressErrorCode:
            raise TypeError("code must be an exact VisualIngressErrorCode")
        self.code = code
        super().__init__(code.value)


def _raise(code: VisualIngressErrorCode) -> None:
    raise VisualIngressError(code)


def _exact(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    return dict(value)


def _sequence(value: object, maximum: int) -> tuple[object, ...]:
    if type(value) is not list or len(value) > maximum:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    return tuple(value)


def _canonical_json(value: object) -> bytes:
    try:
        raw = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    if len(raw) > MAX_IMAGE_SET_RECORD_BYTES:
        _raise(VisualIngressErrorCode.BUDGET_EXCEEDED)
    return raw


def _domain_failure(error: BaseException) -> VisualIngressError:
    if type(error) is VisualContractError and error.code is VisualContractErrorCode.BUDGET_EXCEEDED:
        return VisualIngressError(VisualIngressErrorCode.BUDGET_EXCEEDED)
    if (
        type(error) is VisualInputStoreError
        and error.code is VisualInputStoreErrorCode.BUDGET_EXCEEDED
    ):
        return VisualIngressError(VisualIngressErrorCode.BUDGET_EXCEEDED)
    return VisualIngressError(VisualIngressErrorCode.INVALID_INPUT)


def parse_seal_image_set_request(value: object) -> SealImageSetRequest:
    """Decode one exact JSON-style request into the sealed domain contract."""

    try:
        data = _exact(value, _REQUEST_FIELDS)
        inputs = tuple(
            ImageIngress(**_exact(item, _INPUT_FIELDS))
            for item in _sequence(data["inputs"], MAX_IMAGE_SET_ITEMS)
        )
        hints = tuple(
            DimensionHint.from_mapping(item) for item in _sequence(data["dimension_hints"], 32)
        )
        evidence = tuple(
            CalibrationEvidence.from_mapping(item)
            for item in _sequence(data["calibration_evidence"], MAX_IMAGE_SET_ITEMS * 2)
        )
        request = SealImageSetRequest(
            schema_version=data["schema_version"],
            create_key=data["create_key"],
            inputs=inputs,
            unit=data["unit"],
            dimension_hints=hints,
            calibration_evidence=evidence,
            same_object=data["same_object"],
            same_state=data["same_state"],
            same_scale=data["same_scale"],
            processing_authorization=ProcessingAuthorization(data["processing_authorization"]),
        )
        _canonical_json(request.to_mapping())
        return request
    except VisualIngressError:
        raise
    except (TypeError, ValueError, VisualContractError, VisualInputStoreError) as error:
        raise _domain_failure(error) from None


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_mtime_ns,
        _stable_ctime_ns(value),
    )


def _source_identity(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        _stable_ctime_ns(value),
    )


def _stable_ctime_ns(value: os.stat_result) -> int:
    if sys.platform == "win32":
        return int(getattr(value, "st_birthtime_ns", value.st_ctime_ns))
    return int(value.st_ctime_ns)


def _wire_identity(value: int, *, width: int) -> int | str:
    if sys.platform != "win32":
        return value
    if type(value) is not int or value < 0 or value >= 1 << (width * 4):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    return f"{value:0{width}x}"


def _parse_wire_identity(value: object, *, width: int) -> int:
    if sys.platform != "win32":
        if type(value) is not int:
            _raise(VisualIngressErrorCode.INVALID_INPUT)
        return value
    if type(value) is not str or len(value) != width or re.fullmatch(r"[0-9a-f]+", value) is None:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    return int(value, 16)


def _is_reparse_point(value: os.stat_result) -> bool:
    return bool(int(getattr(value, "st_file_attributes", 0) or 0) & 0x400)


def _safe_directory(value: os.stat_result) -> bool:
    if sys.platform == "win32":
        # Owner and access control are verified from the native directory
        # handle by open_visual_staging; CRT stat has no SID/DACL authority.
        return (
            stat.S_ISDIR(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and not _is_reparse_point(value)
            and 1 <= value.st_nlink <= 2 + MAX_IMAGE_SET_ITEMS
        )
    return (
        stat.S_ISDIR(value.st_mode)
        and value.st_uid == os.geteuid()
        and 1 <= value.st_nlink <= 2 + MAX_IMAGE_SET_ITEMS
        and stat.S_IMODE(value.st_mode) == 0o700
    )


def _safe_source(value: os.stat_result) -> bool:
    if sys.platform == "win32":
        # The corresponding native file capability proves current-owner SID,
        # protected DACL, single link, and a non-reparse File ID.
        return (
            stat.S_ISREG(value.st_mode)
            and not stat.S_ISLNK(value.st_mode)
            and not _is_reparse_point(value)
            and value.st_nlink == 1
            and 0 < value.st_size <= MAX_IMAGE_SOURCE_BYTES
        )
    return (
        stat.S_ISREG(value.st_mode)
        and value.st_uid == os.geteuid()
        and value.st_nlink == 1
        and stat.S_IMODE(value.st_mode) == 0o600
        and 0 < value.st_size <= MAX_IMAGE_SOURCE_BYTES
    )


def _digest(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    return value


def _source_locator_identity(value: object) -> tuple[int, int, int, int, int, int, int, int]:
    data = _exact(value, _SOURCE_LOCATOR_FIELDS)
    for name in ("schema_version", "mode", "uid", "nlink", "size"):
        if type(data[name]) is not int:
            _raise(VisualIngressErrorCode.INVALID_INPUT)
    dev = _parse_wire_identity(data["dev"], width=16)
    ino = _parse_wire_identity(data["ino"], width=32)
    if type(data["schema_version"]) is not int or data["schema_version"] != VISUAL_SCHEMA_VERSION:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    for name in ("mtime_ns", "ctime_ns"):
        if type(data[name]) is not str or not data[name].isdigit() or len(data[name]) > 32:
            _raise(VisualIngressErrorCode.INVALID_INPUT)
    if (
        type(data["digest"]) is not str
        or len(data["digest"]) != 64
        or any(character not in "0123456789abcdef" for character in data["digest"])
    ):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    synthetic = os.stat_result(
        (
            data["mode"],
            ino,
            dev,
            data["nlink"],
            data["uid"],
            0,
            data["size"],
            0,
            0,
            0,
        )
    )
    if not _safe_source(synthetic):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    return (
        dev,
        ino,
        data["mode"],
        data["uid"],
        data["nlink"],
        data["size"],
        int(data["mtime_ns"]),
        int(data["ctime_ns"]),
    )


def bind_visual_staging_locator(
    request: object,
    directory: object,
    sources: object,
    source_sha256: object,
) -> dict[str, object]:
    """Bind one exact private staging directory and its fixed source entries."""

    if type(request) is not SealImageSetRequest or type(directory) is not os.stat_result:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes)):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    source_stats = tuple(sources)
    if type(source_sha256) not in {list, tuple}:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    source_digests = tuple(_digest(item) for item in source_sha256)
    if len(source_stats) != len(request.inputs) or not 1 <= len(source_stats) <= 4:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    if len(source_digests) != len(source_stats):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    if not _safe_directory(directory) or any(
        type(item) is not os.stat_result or not _safe_source(item) for item in source_stats
    ):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    identities = tuple(_source_identity(item) for item in source_stats)
    if len({identity[:2] for identity in identities}) != len(identities):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    if sum(identity[5] for identity in identities) > MAX_IMAGE_SET_SOURCE_BYTES:
        _raise(VisualIngressErrorCode.BUDGET_EXCEEDED)
    source_locators = [
        bind_visual_input_locator(request, index, source)
        for index, source in enumerate(source_stats)
    ]
    body = {
        "schema_version": VISUAL_SCHEMA_VERSION,
        "dev": _wire_identity(directory.st_dev, width=16),
        "ino": _wire_identity(directory.st_ino, width=32),
        "mode": directory.st_mode,
        "uid": directory.st_uid,
        "nlink": directory.st_nlink,
        "mtime_ns": str(directory.st_mtime_ns),
        "ctime_ns": str(_stable_ctime_ns(directory)),
        "source_locators": source_locators,
        "source_sha256": list(source_digests),
    }
    digest = hashlib.sha256(
        _STAGING_LOCATOR_DOMAIN
        + _canonical_json({"request": request.to_mapping(), "identity": body})
    ).hexdigest()
    return body | {"digest": digest}


class OpenedVisualStaging:
    """Identity-pinned source descriptors opened from one staging-directory FD."""

    __slots__ = (
        "_directory_fd",
        "_directory_capability",
        "_directory_identity",
        "_expected_names",
        "_source_fds",
        "_source_capabilities",
        "_source_identities",
        "_source_sha256",
        "sources",
    )

    def __init__(
        self,
        *,
        directory_fd: int,
        directory_capability: WindowsPathCapability | None,
        directory_identity: tuple[int, int, int, int, int, int, int],
        expected_names: tuple[str, ...],
        source_fds: tuple[int, ...],
        source_capabilities: tuple[WindowsPathCapability, ...],
        source_identities: tuple[tuple[int, int, int, int, int, int, int, int], ...],
        source_sha256: tuple[str, ...],
        sources: tuple[DescriptorSource, ...],
    ) -> None:
        self._directory_fd = directory_fd
        self._directory_capability = directory_capability
        self._directory_identity = directory_identity
        self._expected_names = expected_names
        self._source_fds = source_fds
        self._source_capabilities = source_capabilities
        self._source_identities = source_identities
        self._source_sha256 = source_sha256
        self.sources = sources

    def verify(self) -> None:
        if sys.platform == "win32":
            self._verify_windows()
            return
        try:
            directory = os.fstat(self._directory_fd)
            names = os.listdir(self._directory_fd)
        except OSError:
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        if (
            not _safe_directory(directory)
            or _directory_identity(directory) != self._directory_identity
            or len(names) != len(self._expected_names)
            or set(names) != set(self._expected_names)
        ):
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        observed: list[tuple[int, int, int, int, int, int, int, int]] = []
        try:
            for name, descriptor, expected, expected_sha256 in zip(
                self._expected_names,
                self._source_fds,
                self._source_identities,
                self._source_sha256,
                strict=True,
            ):
                entry = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
                opened = os.fstat(descriptor)
                if (
                    not _safe_source(entry)
                    or not _safe_source(opened)
                    or _source_identity(entry) != expected
                    or _source_identity(opened) != expected
                ):
                    _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
                digest = hashlib.sha256()
                remaining = expected[5]
                offset = 0
                while remaining:
                    chunk = os.pread(descriptor, min(64 * 1024, remaining), offset)
                    if not chunk:
                        _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
                    digest.update(chunk)
                    remaining -= len(chunk)
                    offset += len(chunk)
                if (
                    not hmac.compare_digest(digest.hexdigest(), expected_sha256)
                    or _source_identity(os.fstat(descriptor)) != expected
                ):
                    _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
                observed.append(expected)
        except OSError:
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        if len({identity[:2] for identity in observed}) != len(observed):
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        if sum(identity[5] for identity in observed) > MAX_IMAGE_SET_SOURCE_BYTES:
            _raise(VisualIngressErrorCode.BUDGET_EXCEEDED)

    def _verify_windows(self) -> None:
        directory_capability = self._directory_capability
        if type(directory_capability) is not WindowsPathCapability or len(
            self._source_capabilities
        ) != len(self._source_fds):
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        try:
            pinned_directory = capture_windows_fd(
                self._directory_fd,
                directory=True,
                generation_token=directory_capability.generation_token,
            )
            directory = os.fstat(self._directory_fd)
            validate_windows_path(directory_capability, directory=True)
            names = tuple(
                entry.name for entry in os.scandir(windows_extended_path(directory_capability.path))
            )
        except (OSError, TypeError, ValueError):
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        if (
            pinned_directory != directory_capability
            or not _safe_directory(directory)
            or _directory_identity(directory) != self._directory_identity
            or len(names) != len(self._expected_names)
            or set(names) != set(self._expected_names)
        ):
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)

        observed_file_ids: set[tuple[int, int]] = set()
        try:
            for name, descriptor, capability, expected, expected_sha256 in zip(
                self._expected_names,
                self._source_fds,
                self._source_capabilities,
                self._source_identities,
                self._source_sha256,
                strict=True,
            ):
                pinned = capture_windows_fd(
                    descriptor,
                    directory=False,
                    generation_token=capability.generation_token,
                )
                validate_windows_path(capability, directory=False)
                current = os.fstat(descriptor)
                if (
                    pinned != capability
                    or os.path.normcase(os.fspath(os.path.dirname(capability.path)))
                    != os.path.normcase(directory_capability.path)
                    or os.path.basename(capability.path) != name
                    or not _safe_source(current)
                    or _source_identity(current) != expected
                ):
                    _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
                native_identity = (capability.volume, capability.file_id)
                if native_identity in observed_file_ids:
                    _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
                observed_file_ids.add(native_identity)
                digest = hashlib.sha256()
                remaining = expected[5]
                offset = 0
                while remaining:
                    chunk = portable_pread(
                        descriptor,
                        min(64 * 1024, remaining),
                        offset,
                    )
                    if not chunk or len(chunk) > remaining:
                        _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
                    digest.update(chunk)
                    remaining -= len(chunk)
                    offset += len(chunk)
                after = os.fstat(descriptor)
                after_capability = capture_windows_fd(
                    descriptor,
                    directory=False,
                    generation_token=capability.generation_token,
                )
                if (
                    after_capability != capability
                    or _source_identity(after) != expected
                    or not hmac.compare_digest(digest.hexdigest(), expected_sha256)
                ):
                    _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        except VisualIngressError:
            raise
        except (OSError, TypeError, ValueError):
            _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
        if sum(identity[5] for identity in self._source_identities) > MAX_IMAGE_SET_SOURCE_BYTES:
            _raise(VisualIngressErrorCode.BUDGET_EXCEEDED)

    def close(self) -> None:
        for descriptor in reversed(self._source_fds):
            with contextlib.suppress(OSError):
                os.close(descriptor)


def open_visual_staging(
    request: object,
    directory_fd: object,
    locator: object,
) -> OpenedVisualStaging:
    """Open only fixed source names from one received staging-directory FD."""

    if (
        type(request) is not SealImageSetRequest
        or type(directory_fd) is not int
        or directory_fd < 0
    ):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    data = _exact(locator, _STAGING_LOCATOR_FIELDS)
    for name in ("schema_version", "mode", "uid", "nlink"):
        if type(data[name]) is not int:
            _raise(VisualIngressErrorCode.INVALID_INPUT)
    directory_dev = _parse_wire_identity(data["dev"], width=16)
    directory_ino = _parse_wire_identity(data["ino"], width=32)
    if type(data["schema_version"]) is not int or data["schema_version"] != VISUAL_SCHEMA_VERSION:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    for name in ("mtime_ns", "ctime_ns"):
        if type(data[name]) is not str or not data[name].isdigit() or len(data[name]) > 32:
            _raise(VisualIngressErrorCode.INVALID_INPUT)
    if (
        type(data["digest"]) is not str
        or len(data["digest"]) != 64
        or any(character not in "0123456789abcdef" for character in data["digest"])
    ):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    raw_locators = _sequence(data["source_locators"], MAX_IMAGE_SET_ITEMS)
    raw_digests = _sequence(data["source_sha256"], MAX_IMAGE_SET_ITEMS)
    if len(raw_locators) != len(request.inputs):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    source_digests = tuple(_digest(item) for item in raw_digests)
    if len(source_digests) != len(raw_locators):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    source_identities = tuple(_source_locator_identity(item) for item in raw_locators)
    body = {name: data[name] for name in _STAGING_LOCATOR_FIELDS if name != "digest"}
    expected_digest = hashlib.sha256(
        _STAGING_LOCATOR_DOMAIN
        + _canonical_json({"request": request.to_mapping(), "identity": body})
    ).hexdigest()
    if not hmac.compare_digest(data["digest"], expected_digest):
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    try:
        directory = os.fstat(directory_fd)
    except OSError:
        _raise(VisualIngressErrorCode.INVALID_INPUT)
    expected_directory = (
        directory_dev,
        directory_ino,
        data["mode"],
        data["uid"],
        data["nlink"],
        int(data["mtime_ns"]),
        int(data["ctime_ns"]),
    )
    if not _safe_directory(directory) or _directory_identity(directory) != expected_directory:
        _raise(VisualIngressErrorCode.INVALID_INPUT)

    names = tuple(f"source_{index}" for index in range(len(request.inputs)))
    source_fds: list[int] = []
    source_capabilities: list[WindowsPathCapability] = []
    try:
        directory_capability = None
        if sys.platform == "win32":
            directory_capability = capture_windows_fd(directory_fd, directory=True)
            validate_windows_path(directory_capability, directory=True)
            observed_names = [
                entry.name for entry in os.scandir(windows_extended_path(directory_capability.path))
            ]
        else:
            observed_names = os.listdir(directory_fd)
        if set(observed_names) != set(names) or len(observed_names) != len(names):
            _raise(VisualIngressErrorCode.INVALID_INPUT)
        for name, expected in zip(names, source_identities, strict=True):
            if sys.platform == "win32":
                descriptor, capability = open_private_file(
                    Path(directory_capability.path) / name,
                    create=False,
                    read_write=False,
                    expected_parent=directory_capability,
                )
                source_capabilities.append(capability)
            else:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                descriptor = os.open(name, flags, dir_fd=directory_fd)
            source_fds.append(descriptor)
            current = os.fstat(descriptor)
            if not _safe_source(current) or _source_identity(current) != expected:
                _raise(VisualIngressErrorCode.INVALID_INPUT)
        if sys.platform == "win32":
            native_identities = {
                (capability.volume, capability.file_id) for capability in source_capabilities
            }
            unique = len(native_identities) == len(source_capabilities)
        else:
            unique = len({identity[:2] for identity in source_identities}) == len(source_identities)
        if not unique:
            _raise(VisualIngressErrorCode.INVALID_INPUT)
        opened = OpenedVisualStaging(
            directory_fd=directory_fd,
            directory_capability=directory_capability,
            directory_identity=expected_directory,
            expected_names=names,
            source_fds=tuple(source_fds),
            source_capabilities=tuple(source_capabilities),
            source_identities=source_identities,
            source_sha256=source_digests,
            sources=tuple(
                DescriptorSource(fd=descriptor, locator=source_locator)
                for descriptor, source_locator in zip(
                    source_fds,
                    raw_locators,
                    strict=True,
                )
            ),
        )
        opened.verify()
        return opened
    except VisualIngressError:
        for descriptor in reversed(source_fds):
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise
    except (OSError, VisualInputStoreError):
        for descriptor in reversed(source_fds):
            with contextlib.suppress(OSError):
                os.close(descriptor)
        _raise(VisualIngressErrorCode.INVALID_INPUT)


def validate_seal_result(value: object) -> dict[str, object]:
    """Validate the only result shape exposed to host adapters."""

    data = _exact(
        value,
        {"schema_version", "image_set_id", "image_set_manifest_sha256"},
    )
    if type(data["schema_version"]) is not int or data["schema_version"] != VISUAL_SCHEMA_VERSION:
        _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
    image_set_id = data["image_set_id"]
    digest = data["image_set_manifest_sha256"]
    if (
        type(image_set_id) is not str
        or len(image_set_id) != len("image_set_") + 32
        or not image_set_id.startswith("image_set_")
        or any(character not in "0123456789abcdef" for character in image_set_id[10:])
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        _raise(VisualIngressErrorCode.INTEGRITY_FAILURE)
    return data


__all__ = (
    "OpenedVisualStaging",
    "VisualIngressError",
    "VisualIngressErrorCode",
    "bind_visual_staging_locator",
    "open_visual_staging",
    "parse_seal_image_set_request",
    "validate_seal_result",
)
