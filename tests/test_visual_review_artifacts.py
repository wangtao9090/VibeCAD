"""Strict contract tests for persistent advisory visual-review records."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct

import pytest

from tests.test_visual_overlay_render import _plan_and_images
from vibecad.visual.overlay_render import render_evidence_overlay
from vibecad.visual.review_artifacts import (
    VisualReviewArtifact,
    VisualReviewArtifactError,
    VisualReviewArtifactErrorCode,
    VisualReviewAuthority,
    VisualReviewResource,
    decode_visual_review_artifact,
    encode_visual_review_artifact,
    parse_visual_review_resource_uri,
    visual_review_resource_uri,
)

_RECONSTRUCTION_ID = "reconstruction_" + "7" * 32


def _artifact(*, source_index: int = 0) -> VisualReviewArtifact:
    sizes = ((96, 64),) if source_index == 0 else ((96, 64), (80, 60))
    plan, image_set, raws = _plan_and_images(*sizes)
    overlay = render_evidence_overlay(
        plan=plan,
        image_set=image_set,
        source_index=source_index,
        normalized_png=raws[source_index],
    )
    return VisualReviewArtifact(
        reconstruction_id=_RECONSTRUCTION_ID,
        generation=3,
        overlay=overlay,
    )


def _replace_metadata(raw: bytes, metadata: bytes) -> bytes:
    size = int.from_bytes(raw[8:12], "big")
    png = raw[12 + size :]
    return raw[:8] + struct.pack(">I", len(metadata)) + metadata + png


def _metadata(raw: bytes) -> dict[str, object]:
    size = int.from_bytes(raw[8:12], "big")
    return json.loads(raw[12 : 12 + size])


def test_visual_review_record_round_trips_exactly_and_derives_uri() -> None:
    artifact = _artifact()

    raw = encode_visual_review_artifact(artifact)
    result = decode_visual_review_artifact(raw)

    assert result == artifact
    assert result.overlay.png_bytes == artifact.overlay.png_bytes
    assert encode_visual_review_artifact(result) == raw
    assert artifact.record_sha256 == hashlib.sha256(raw).hexdigest()
    assert artifact.resource_uri == (f"vibecad://visual-review/{artifact.observation_id}/0.png")
    assert artifact.resource_uri == visual_review_resource_uri(artifact.observation_id, 0)
    assert parse_visual_review_resource_uri(artifact.resource_uri) == (
        artifact.observation_id,
        0,
    )
    assert _metadata(raw)["authority"] == "advisory_only"


def test_record_digest_changes_with_generation_and_png() -> None:
    artifact = _artifact()
    later = dataclasses.replace(artifact, generation=4)
    other_source = _artifact(source_index=1)

    assert artifact.record_sha256 != later.record_sha256
    assert artifact.record_sha256 != other_source.record_sha256


@pytest.mark.parametrize(
    "uri",
    (
        "file:///secret.png",
        "vibecad://visual-review/visual_observation_" + "1" * 31 + "/0.png",
        "vibecad://visual-review/visual_observation_" + "1" * 32 + "/00.png",
        "vibecad://visual-review/visual_observation_" + "1" * 32 + "/16.png",
        "vibecad://visual-review/visual_observation_" + "1" * 32 + "/0.jpg",
    ),
)
def test_resource_uri_grammar_is_closed(uri: str) -> None:
    with pytest.raises(VisualReviewArtifactError) as caught:
        parse_visual_review_resource_uri(uri)
    assert caught.value.code is VisualReviewArtifactErrorCode.INVALID_INPUT


def test_resource_value_requires_exact_png_contract() -> None:
    artifact = _artifact()
    resource = VisualReviewResource(
        uri=artifact.resource_uri,
        data=artifact.overlay.png_bytes,
    )
    assert resource.media_type == "image/png"

    with pytest.raises(VisualReviewArtifactError) as wrong_media:
        dataclasses.replace(resource, media_type="image/jpeg")
    assert wrong_media.value.code is VisualReviewArtifactErrorCode.INVALID_INPUT

    with pytest.raises(VisualReviewArtifactError) as wrong_data:
        dataclasses.replace(resource, data=bytearray(resource.data))  # type: ignore[arg-type]
    assert wrong_data.value.code is VisualReviewArtifactErrorCode.INVALID_INPUT

    with pytest.raises(VisualReviewArtifactError) as non_png:
        dataclasses.replace(resource, data=b"not-a-png")
    assert non_png.value.code is VisualReviewArtifactErrorCode.INVALID_INPUT


def test_record_rejects_authority_schema_and_identity_forgery() -> None:
    artifact = _artifact()

    with pytest.raises(VisualReviewArtifactError) as authority:
        dataclasses.replace(artifact, authority="authoritative")  # type: ignore[arg-type]
    assert authority.value.code is VisualReviewArtifactErrorCode.AUTHORITY_VIOLATION

    with pytest.raises(VisualReviewArtifactError) as schema:
        dataclasses.replace(artifact, schema_version=2)
    assert schema.value.code is VisualReviewArtifactErrorCode.UNSUPPORTED_VERSION

    with pytest.raises(VisualReviewArtifactError) as identifier:
        dataclasses.replace(artifact, reconstruction_id="invalid")
    assert identifier.value.code is VisualReviewArtifactErrorCode.INVALID_INPUT

    with pytest.raises(VisualReviewArtifactError) as overlay_type:
        dataclasses.replace(artifact, overlay=object())  # type: ignore[arg-type]
    assert overlay_type.value.code is VisualReviewArtifactErrorCode.INVALID_INPUT


def test_decode_rejects_tampered_png_unknown_fields_and_noncanonical_json() -> None:
    raw = encode_visual_review_artifact(_artifact())

    tampered_png = raw[:-1] + bytes([raw[-1] ^ 1])
    with pytest.raises(VisualReviewArtifactError) as png:
        decode_visual_review_artifact(tampered_png)
    assert png.value.code is VisualReviewArtifactErrorCode.INTEGRITY_FAILURE

    malformed_png = b"\x89PNG\r\n\x1a\nnot-a-real-png"
    malformed_metadata = _metadata(raw)
    malformed_metadata["png_sha256"] = hashlib.sha256(malformed_png).hexdigest()
    malformed_metadata["png_size_bytes"] = len(malformed_png)
    malformed_record = _replace_metadata(
        raw,
        json.dumps(malformed_metadata, separators=(",", ":"), sort_keys=True).encode("ascii"),
    )
    metadata_size = int.from_bytes(malformed_record[8:12], "big")
    malformed_record = malformed_record[: 12 + metadata_size] + malformed_png
    with pytest.raises(VisualReviewArtifactError) as malformed:
        decode_visual_review_artifact(malformed_record)
    assert malformed.value.code is VisualReviewArtifactErrorCode.INTEGRITY_FAILURE

    metadata = _metadata(raw)
    metadata["unexpected"] = True
    unknown = json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(VisualReviewArtifactError) as unknown_field:
        decode_visual_review_artifact(_replace_metadata(raw, unknown))
    assert unknown_field.value.code is VisualReviewArtifactErrorCode.INTEGRITY_FAILURE

    canonical = json.dumps(_metadata(raw), separators=(",", ":"), sort_keys=True)
    noncanonical = (" " + canonical).encode("ascii")
    with pytest.raises(VisualReviewArtifactError) as whitespace:
        decode_visual_review_artifact(_replace_metadata(raw, noncanonical))
    assert whitespace.value.code is VisualReviewArtifactErrorCode.INTEGRITY_FAILURE


def test_decode_rejects_duplicate_keys_and_authority_escalation() -> None:
    raw = encode_visual_review_artifact(_artifact())
    canonical = json.dumps(_metadata(raw), separators=(",", ":"), sort_keys=True)
    duplicate = canonical[:-1] + ',"width":96}'
    with pytest.raises(VisualReviewArtifactError) as duplicate_key:
        decode_visual_review_artifact(_replace_metadata(raw, duplicate.encode("ascii")))
    assert duplicate_key.value.code is VisualReviewArtifactErrorCode.INTEGRITY_FAILURE

    metadata = _metadata(raw)
    metadata["authority"] = "cad_authority"
    escalated = json.dumps(metadata, separators=(",", ":"), sort_keys=True).encode("ascii")
    with pytest.raises(VisualReviewArtifactError) as authority:
        decode_visual_review_artifact(_replace_metadata(raw, escalated))
    assert authority.value.code is VisualReviewArtifactErrorCode.AUTHORITY_VIOLATION


def test_decode_rejects_truncation_bad_magic_and_nonbytes() -> None:
    raw = encode_visual_review_artifact(_artifact())
    cases = (raw[:11], b"BADMAGIC" + raw[8:], raw[:12])
    for value in cases:
        with pytest.raises(VisualReviewArtifactError):
            decode_visual_review_artifact(value)

    with pytest.raises(VisualReviewArtifactError) as wrong_type:
        decode_visual_review_artifact(bytearray(raw))
    assert wrong_type.value.code is VisualReviewArtifactErrorCode.INVALID_INPUT


def test_encode_enforces_metadata_budget_before_record_construction(monkeypatch) -> None:
    artifact = _artifact()
    monkeypatch.setattr(
        "vibecad.visual.review_artifacts.MAX_VISUAL_REVIEW_METADATA_BYTES",
        8,
    )
    with pytest.raises(VisualReviewArtifactError) as caught:
        encode_visual_review_artifact(artifact)
    assert caught.value.code is VisualReviewArtifactErrorCode.BUDGET_EXCEEDED


def test_visual_review_contract_has_no_task_cad_or_host_dependencies() -> None:
    import inspect

    import vibecad.visual.review_artifacts as module

    source = inspect.getsource(module)
    for forbidden in (
        "vibecad.tasks",
        "vibecad.application",
        "vibecad.interaction",
        "vibecad.cad",
        "vibecad.worker",
        "vibecad.mcp",
        "blender",
        "bpy",
    ):
        assert forbidden not in source


def test_authority_enum_has_only_advisory_value() -> None:
    assert tuple(VisualReviewAuthority) == (VisualReviewAuthority.ADVISORY_ONLY,)
