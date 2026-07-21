"""Pure compilation, verification, and one-shot receipt consumption."""

from __future__ import annotations

import hashlib

from vibecad.validation.checks import CompiledSpecData, compile_spec, evaluate_criterion
from vibecad.validation.contracts import (
    CompiledAcceptance,
    EntityObservation,
    ObservationSnapshot,
    PreservationObservation,
    ValidationError,
    ValidationErrorCode,
    VerificationReceipt,
    VerificationResult,
    _canonical_json_bytes,
    _CompiledBinding,
    _consume_receipt,
    _entity_observation_digest,
    _issue_compiled,
    _issue_receipt,
    _lookup_compiled,
    _missing_entity_digest,
    _prefix_nested_error,
    _raise_validation,
    _ReceiptBinding,
    _validate_bounded_text,
    _validate_digest,
    _validate_revision,
    _validated_snapshot,
)
from vibecad.workflow.contracts import AcceptanceSpec
from vibecad.workflow.state import CriterionOutcome, VerificationReport

_SPEC_DOMAIN = b"vibecad-compiled-acceptance-spec-v1\0"
_REPORT_DOMAIN = b"vibecad-verification-report-v1\0"
_MAX_PRESERVE_FIELDS = 128
_IDENTITY_FIELDS = (
    "feature_id",
    "object_id",
    "object_type",
    "provenance",
    "semantic_role",
)
_GEOMETRY_FIELDS = (
    "area_mm2",
    "bbox_mm",
    "center_of_mass_mm",
    "solid_count",
    "valid_shape",
    "volume_mm3",
)


def _trusted_entity(value: object, path: str) -> EntityObservation:
    if type(value) is not EntityObservation:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, path)
    invalid_mapping = False
    try:
        mapping = EntityObservation.to_mapping(value)
    except (AttributeError, TypeError, ValueError, RecursionError):
        invalid_mapping = True
        mapping = None
    if invalid_mapping:
        _raise_validation(ValidationErrorCode.INVALID_VALUE, path)
    assert mapping is not None
    caught = None
    try:
        rebuilt = EntityObservation.from_mapping(mapping)
    except ValidationError as error:
        caught = error
        rebuilt = None
    if caught is not None:
        raise _prefix_nested_error(caught, path)
    assert rebuilt is not None
    return rebuilt


def _preserve_fields(value: object) -> tuple[str, ...]:
    if type(value) is not tuple:
        _raise_validation(ValidationErrorCode.INVALID_TYPE, "/preserve")
    if len(value) > _MAX_PRESERVE_FIELDS:
        _raise_validation(ValidationErrorCode.BUDGET_EXCEEDED, "/preserve")
    fields = tuple(
        _validate_bounded_text(item, f"/preserve/{index}")
        for index, item in enumerate(value)
    )
    if len(fields) != len(set(fields)):
        _raise_validation(ValidationErrorCode.DUPLICATE_TARGET, "/preserve")
    return fields


def _changed_parameter_fields(
    before: EntityObservation,
    after: EntityObservation,
) -> list[str]:
    before_by_name = {item.name: item for item in before.parameters}
    after_by_name = {item.name: item for item in after.parameters}
    return [
        f"parameters.{name}"
        for name in sorted(set(before_by_name) | set(after_by_name))
        if before_by_name.get(name) != after_by_name.get(name)
    ]


def _entity_field_value(entity: EntityObservation, field: str) -> object:
    if field == "feature_id":
        return entity.feature_id
    if field == "object_id":
        return entity.object_id
    if field == "object_type":
        return entity.object_type
    if field == "provenance":
        return entity.provenance
    if field == "semantic_role":
        return entity.semantic_role
    if field == "placement":
        return entity.placement
    if field == "area_mm2":
        return entity.area_mm2
    if field == "bbox_mm":
        return entity.bbox_mm
    if field == "center_of_mass_mm":
        return entity.center_of_mass_mm
    if field == "solid_count":
        return entity.solid_count
    if field == "valid_shape":
        return entity.valid_shape
    if field == "volume_mm3":
        return entity.volume_mm3
    raise ValueError("unsupported entity observation field")


def compare_entity_preservation(
    before: EntityObservation | None,
    after: EntityObservation | None,
    *,
    target: str,
    preserve: tuple[str, ...] = (),
) -> PreservationObservation:
    """Purely compare one base/reloaded entity preservation boundary.

    Identity is always invariant.  An empty ``preserve`` tuple compares every
    parameter, placement, and geometry fact and is therefore appropriate for
    non-target objects.  A non-empty tuple compares the named top-level facts;
    ``geometry`` and ``parameters`` expand their complete groups, and every
    other name is interpreted as a parameter name.
    """

    checked_target = _validate_bounded_text(target, "/target")
    checked_preserve = _preserve_fields(preserve)
    trusted_before = None if before is None else _trusted_entity(before, "/before")
    trusted_after = None if after is None else _trusted_entity(after, "/after")
    before_digest = (
        _missing_entity_digest(checked_target, "before")
        if trusted_before is None
        else _entity_observation_digest(trusted_before)
    )
    after_digest = (
        _missing_entity_digest(checked_target, "after")
        if trusted_after is None
        else _entity_observation_digest(trusted_after)
    )

    changed: list[str] = []
    if trusted_before is None:
        changed.append("before.missing")
    if trusted_after is None:
        changed.append("after.missing")
    if trusted_before is not None and trusted_after is not None:
        for field in _IDENTITY_FIELDS:
            if _entity_field_value(trusted_before, field) != _entity_field_value(
                trusted_after,
                field,
            ):
                changed.append(field)

        if not checked_preserve:
            if trusted_before.placement != trusted_after.placement:
                changed.append("placement")
            for field in _GEOMETRY_FIELDS:
                if _entity_field_value(trusted_before, field) != _entity_field_value(
                    trusted_after,
                    field,
                ):
                    changed.append(field)
            changed.extend(_changed_parameter_fields(trusted_before, trusted_after))
        else:
            before_parameters = {item.name: item for item in trusted_before.parameters}
            after_parameters = {item.name: item for item in trusted_after.parameters}
            selected: set[str] = set()
            for field in checked_preserve:
                if field in _IDENTITY_FIELDS:
                    continue
                if field == "placement":
                    selected.add(field)
                    continue
                if field == "geometry":
                    selected.update(_GEOMETRY_FIELDS)
                    continue
                if field == "parameters":
                    changed.extend(
                        _changed_parameter_fields(trusted_before, trusted_after)
                    )
                    continue
                if field in _GEOMETRY_FIELDS:
                    selected.add(field)
                    continue
                before_parameter = before_parameters.get(field)
                after_parameter = after_parameters.get(field)
                if before_parameter is None or after_parameter is None:
                    changed.append(f"parameters.{field}.missing")
                elif before_parameter != after_parameter:
                    changed.append(f"parameters.{field}")
            for field in sorted(selected):
                before_value = _entity_field_value(trusted_before, field)
                after_value = _entity_field_value(trusted_after, field)
                if before_value is None or after_value is None:
                    changed.append(f"{field}.missing")
                elif before_value != after_value:
                    changed.append(field)

    changed_fields = tuple(sorted(set(changed)))
    return PreservationObservation(
        target=checked_target,
        preserved=not changed_fields,
        before_digest=before_digest,
        after_digest=after_digest,
        changed_fields=changed_fields,
    )


def compile_acceptance_spec(spec: AcceptanceSpec) -> CompiledAcceptance:
    """Compile an exact specification into an authentic process-local capability."""

    compiled = compile_spec(spec)
    digest = hashlib.sha256(_SPEC_DOMAIN + compiled.canonical_bytes).hexdigest()
    return _issue_compiled(
        _CompiledBinding(
            acceptance_id=compiled.acceptance_id,
            spec_digest=digest,
            payload=compiled,
        )
    )


def _report_id(
    compiled: _CompiledBinding,
    *,
    candidate_revision: str,
    manifest_sha256: str,
    observation_digest: str,
    passed: bool,
    verdicts: tuple[object, ...],
) -> str:
    payload = {
        "schema_version": 1,
        "acceptance_id": compiled.acceptance_id,
        "spec_digest": compiled.spec_digest,
        "candidate_revision": candidate_revision,
        "manifest_sha256": manifest_sha256,
        "observation_digest": observation_digest,
        "passed": passed,
        "verdicts": [verdict.to_mapping() for verdict in verdicts],
    }
    digest = hashlib.sha256(_REPORT_DOMAIN + _canonical_json_bytes(payload)).hexdigest()
    return f"verification_{digest[:32]}"


def verify_acceptance(
    compiled: CompiledAcceptance,
    snapshot: ObservationSnapshot,
    *,
    candidate_revision: str,
    manifest_sha256: str,
) -> VerificationResult:
    """Verify a trusted immutable snapshot against a compiled specification."""

    binding = _lookup_compiled(compiled)
    if type(binding.payload) is not CompiledSpecData:
        _raise_validation(ValidationErrorCode.FORGED_CAPABILITY, "/compiled")
    trusted_snapshot = _validated_snapshot(snapshot)
    revision = _validate_revision(candidate_revision)
    manifest = _validate_digest(manifest_sha256, "/manifest_sha256")
    if revision != trusted_snapshot.candidate_revision:
        _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/candidate_revision")

    verdicts = tuple(
        evaluate_criterion(criterion, trusted_snapshot) for criterion in binding.payload.criteria
    )
    passed = all(
        not verdict.required or verdict.outcome is CriterionOutcome.PASS for verdict in verdicts
    )
    report = VerificationReport(
        id=_report_id(
            binding,
            candidate_revision=revision,
            manifest_sha256=manifest,
            observation_digest=trusted_snapshot.observation_digest,
            passed=passed,
            verdicts=verdicts,
        ),
        acceptance_id=binding.acceptance_id,
        candidate_revision=revision,
        manifest_sha256=manifest,
        observation_digest=trusted_snapshot.observation_digest,
        passed=passed,
        verdicts=verdicts,
    )
    receipt = None
    if passed:
        receipt = _issue_receipt(
            _ReceiptBinding(
                compiled=compiled,
                spec_digest=binding.spec_digest,
                acceptance_id=binding.acceptance_id,
                candidate_revision=revision,
                manifest_sha256=manifest,
                observation_digest=trusted_snapshot.observation_digest,
                report=report,
            )
        )
    return VerificationResult(report=report, receipt=receipt)


def consume_verification_receipt(
    receipt: VerificationReceipt,
    compiled: CompiledAcceptance,
    snapshot: ObservationSnapshot,
    *,
    candidate_revision: str,
    manifest_sha256: str,
) -> VerificationReport:
    """Atomically consume one correctly bound successful receipt."""

    trusted_snapshot = _validated_snapshot(snapshot)
    revision = _validate_revision(candidate_revision)
    manifest = _validate_digest(manifest_sha256, "/manifest_sha256")
    if revision != trusted_snapshot.candidate_revision:
        _raise_validation(ValidationErrorCode.BINDING_MISMATCH, "/candidate_revision")
    return _consume_receipt(
        receipt,
        compiled,
        candidate_revision=revision,
        manifest_sha256=manifest,
        observation_digest=trusted_snapshot.observation_digest,
    )
