"""Execute authentic validated programs through explicitly injected handlers.

This module is the pure in-process execution boundary for Phase 1.  It does
not discover handlers, import CAD integrations, retry operations, or own a
candidate revision.  Callers bind synchronous semantic handlers explicitly.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic_ns as _monotonic_ns
from types import MappingProxyType
from typing import Any

from vibecad.execution.registry import (
    ExecutionProfile,
    ResourceBudget,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
    _matches_value_shape,
)
from vibecad.execution.results import (
    NormalizedToolOutcome,
    normalize_tool_exception,
    normalize_tool_result,
)
from vibecad.execution.selectors import SelectorV1
from vibecad.interaction.cad import (
    MAX_ADMITTED_CREATED_OBJECTS,
    MAX_ADMITTED_RESULT_BYTES,
    MAX_ADMITTED_RUNTIME_MS,
)
from vibecad.workflow.contracts import (
    ErrorCategory,
    EvidenceKind,
    ExecutionEvidence,
    ModelProgram,
    StepError,
    StepResult,
    ValueSource,
)
from vibecad.workflow.errors import SCHEMA_VERSION
from vibecad.workflow.program import (
    BoundCommand,
    BoundResultRef,
    ValidatedProgram,
)


class AdapterErrorCode(StrEnum):
    """Stable configuration failures raised before any handler executes."""

    INVALID_PROGRAM = "invalid_program"
    INVALID_HANDLERS = "invalid_handlers"
    INVALID_REVISION = "invalid_revision"
    MISSING_HANDLER = "missing_handler"
    NON_CALLABLE_HANDLER = "non_callable_handler"
    INVALID_EXECUTION_PROFILE = "invalid_execution_profile"
    UNSUPPORTED_EXECUTION_PROFILE = "unsupported_execution_profile"


_ERROR_MESSAGES = {
    AdapterErrorCode.INVALID_PROGRAM: "Validated program capability is invalid.",
    AdapterErrorCode.INVALID_HANDLERS: "Handler mapping could not be read.",
    AdapterErrorCode.INVALID_REVISION: "Candidate revision is invalid.",
    AdapterErrorCode.MISSING_HANDLER: "A required handler is missing.",
    AdapterErrorCode.NON_CALLABLE_HANDLER: "A required handler is not callable.",
    AdapterErrorCode.INVALID_EXECUTION_PROFILE: "Execution profile is invalid.",
    AdapterErrorCode.UNSUPPORTED_EXECUTION_PROFILE: (
        "An operation does not support the requested execution profile."
    ),
}


class AdapterError(ValueError):
    """A fixed, non-reflective adapter configuration error."""

    def __init__(self, code: AdapterErrorCode) -> None:
        if not isinstance(code, AdapterErrorCode):
            raise TypeError("code must be an AdapterErrorCode")
        self.schema_version = SCHEMA_VERSION
        self.code = code
        self.message = _ERROR_MESSAGES[code]
        super().__init__(f"execution adapter error ({code.value}): {self.message}")

    def to_mapping(self) -> dict[str, int | str]:
        """Return the fixed schema-v1 JSON-compatible error record."""

        return {
            "schema_version": self.schema_version,
            "code": self.code.value,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class _PlannedCommand:
    operation_id: str
    operation: str
    handler_name: str
    handler_kwargs: Mapping[str, Any]
    risk_class: RiskClass
    evidence_required: bool
    depends_on: tuple[str, ...]
    execution_profiles: tuple[ExecutionProfile, ...]
    minimum_freecad_version: tuple[int, int]
    maximum_freecad_version_exclusive: tuple[int, int]
    requires_gui_main_thread: bool
    resource_budget: ResourceBudget
    result_slots: tuple[ResultSlotMetadata, ...]
    handler: Callable[..., object]


@dataclass(frozen=True, slots=True)
class _CommandSnapshot:
    operation_id: str
    operation: str
    handler_name: str
    handler_kwargs: Mapping[str, Any]
    risk_class: RiskClass
    evidence_required: bool
    depends_on: tuple[str, ...]
    preserve: tuple[str, ...]
    source: ValueSource
    execution_profiles: tuple[ExecutionProfile, ...]
    minimum_freecad_version: tuple[int, int]
    maximum_freecad_version_exclusive: tuple[int, int]
    requires_gui_main_thread: bool
    resource_budget: ResourceBudget
    result_slots: tuple[ResultSlotMetadata, ...]


def _invalid_program() -> AdapterError:
    return AdapterError(AdapterErrorCode.INVALID_PROGRAM)


def _freeze_bound_value(value: object) -> object:
    """Copy the already-sealed JSON-like command value into adapter ownership."""

    if type(value) is BoundResultRef:
        if type(value.value_shape) is not ValueShape or not _matches_value_shape(
            MappingProxyType({"command_id": value.command_id, "slot": value.slot}),
            ValueShape.RESULT_REF,
        ):
            raise _invalid_program()
        return BoundResultRef(value.command_id, value.slot, value.value_shape)
    if type(value) is SelectorV1:
        try:
            return SelectorV1.from_mapping(value.to_mapping())
        except Exception:
            raise _invalid_program() from None
    if type(value) is MappingProxyType:
        try:
            keys = tuple(value)
            if any(type(key) is not str for key in keys) or len(set(keys)) != len(keys):
                raise _invalid_program()
            return MappingProxyType({key: _freeze_bound_value(value[key]) for key in keys})
        except AdapterError:
            raise
        except Exception:
            raise _invalid_program() from None
    if type(value) is tuple:
        return tuple(_freeze_bound_value(item) for item in value)
    if value is None or type(value) in {str, int, float, bool}:
        return value
    raise _invalid_program()


def _exactly_equal(left: object, right: object) -> bool:
    """Compare sealed adapter data without Python's cross-type coercions."""

    if type(left) is not type(right):
        return False
    if type(left) is MappingProxyType:
        assert type(right) is MappingProxyType
        try:
            left_keys = tuple(left)
            right_keys = tuple(right)
            return left_keys == right_keys and all(
                _exactly_equal(left[key], right[key]) for key in left_keys
            )
        except Exception:
            return False
    if type(left) is tuple:
        assert type(right) is tuple
        return len(left) == len(right) and all(
            _exactly_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if type(left) is BoundResultRef:
        assert type(right) is BoundResultRef
        return all(
            _exactly_equal(left_item, right_item)
            for left_item, right_item in zip(
                (left.command_id, left.slot, left.value_shape),
                (right.command_id, right.slot, right.value_shape),
                strict=True,
            )
        )
    if type(left) is SelectorV1:
        assert type(right) is SelectorV1
        try:
            checked_left = SelectorV1.from_mapping(left.to_mapping())
            checked_right = SelectorV1.from_mapping(right.to_mapping())
        except Exception:
            return False
        return checked_left == checked_right
    if type(left) is ResultSlotMetadata:
        assert type(right) is ResultSlotMetadata
        return all(
            _exactly_equal(left_item, right_item)
            for left_item, right_item in zip(
                (
                    left.name,
                    left.result_field,
                    left.value_shape,
                    left.enum_values,
                    left.allowed_units,
                ),
                (
                    right.name,
                    right.result_field,
                    right.value_shape,
                    right.enum_values,
                    right.allowed_units,
                ),
                strict=True,
            )
        )
    if type(left) is ResourceBudget:
        assert type(right) is ResourceBudget
        return all(
            _exactly_equal(left_item, right_item)
            for left_item, right_item in zip(
                (
                    left.max_runtime_ms,
                    left.max_created_objects,
                    left.max_result_bytes,
                ),
                (
                    right.max_runtime_ms,
                    right.max_created_objects,
                    right.max_result_bytes,
                ),
                strict=True,
            )
        )
    if type(left) is _CommandSnapshot:
        assert type(right) is _CommandSnapshot
        return all(
            _exactly_equal(left_item, right_item)
            for left_item, right_item in zip(
                (
                    left.operation_id,
                    left.operation,
                    left.handler_name,
                    left.handler_kwargs,
                    left.risk_class,
                    left.evidence_required,
                    left.depends_on,
                    left.preserve,
                    left.source,
                    left.execution_profiles,
                    left.minimum_freecad_version,
                    left.maximum_freecad_version_exclusive,
                    left.requires_gui_main_thread,
                    left.resource_budget,
                    left.result_slots,
                ),
                (
                    right.operation_id,
                    right.operation,
                    right.handler_name,
                    right.handler_kwargs,
                    right.risk_class,
                    right.evidence_required,
                    right.depends_on,
                    right.preserve,
                    right.source,
                    right.execution_profiles,
                    right.minimum_freecad_version,
                    right.maximum_freecad_version_exclusive,
                    right.requires_gui_main_thread,
                    right.resource_budget,
                    right.result_slots,
                ),
                strict=True,
            )
        )
    if isinstance(left, (ExecutionProfile, RiskClass, ValueShape, ValueSource)):
        return left is right
    if left is None or type(left) in {str, int, float, bool}:
        return left == right
    return False


def _snapshot_command(command: object) -> _CommandSnapshot:
    if type(command) is not BoundCommand:
        raise _invalid_program()
    try:
        operation_id = command.id
        operation = command.operation
        handler_name = command.handler_name
        handler_kwargs = command.handler_kwargs
        depends_on = command.depends_on
        preserve = command.preserve
        source = command.source
        risk_class = command.risk_class
        evidence_required = command.evidence_required
        execution_profiles = command.execution_profiles
        minimum_freecad_version = command.minimum_freecad_version
        maximum_freecad_version_exclusive = command.maximum_freecad_version_exclusive
        requires_gui_main_thread = command.requires_gui_main_thread
        resource_budget = command.resource_budget
        result_slots = command.result_slots
    except Exception:
        raise _invalid_program() from None

    if any(
        type(value) is not str or not value.strip()
        for value in (operation_id, operation, handler_name)
    ):
        raise _invalid_program()
    if type(handler_kwargs) is not MappingProxyType:
        raise _invalid_program()
    if (
        type(depends_on) is not tuple
        or not all(type(item) is str and bool(item.strip()) for item in depends_on)
        or len(set(depends_on)) != len(depends_on)
        or operation_id in depends_on
    ):
        raise _invalid_program()
    if type(preserve) is not tuple or not all(
        type(item) is str and bool(item.strip()) for item in preserve
    ):
        raise _invalid_program()
    if type(source) is not ValueSource or type(risk_class) is not RiskClass:
        raise _invalid_program()
    if type(evidence_required) is not bool:
        raise _invalid_program()
    if (
        type(execution_profiles) is not tuple
        or not execution_profiles
        or not all(type(item) is ExecutionProfile for item in execution_profiles)
        or len(set(execution_profiles)) != len(execution_profiles)
    ):
        raise _invalid_program()
    if (
        type(minimum_freecad_version) is not tuple
        or type(maximum_freecad_version_exclusive) is not tuple
        or len(minimum_freecad_version) != 2
        or len(maximum_freecad_version_exclusive) != 2
        or not all(type(item) is int for item in minimum_freecad_version)
        or not all(type(item) is int for item in maximum_freecad_version_exclusive)
        or minimum_freecad_version >= maximum_freecad_version_exclusive
        or type(requires_gui_main_thread) is not bool
        or type(resource_budget) is not ResourceBudget
    ):
        raise _invalid_program()
    if type(result_slots) is not tuple or not all(
        type(item) is ResultSlotMetadata for item in result_slots
    ):
        raise _invalid_program()
    try:
        frozen_budget = ResourceBudget(
            max_runtime_ms=resource_budget.max_runtime_ms,
            max_created_objects=resource_budget.max_created_objects,
            max_result_bytes=resource_budget.max_result_bytes,
        )
        frozen_result_slots = tuple(
            ResultSlotMetadata(
                item.name,
                item.result_field,
                item.value_shape,
                item.enum_values,
                item.allowed_units,
            )
            for item in result_slots
        )
    except Exception:
        raise _invalid_program() from None
    if len({item.name for item in frozen_result_slots}) != len(frozen_result_slots) or len(
        {item.result_field for item in frozen_result_slots}
    ) != len(frozen_result_slots):
        raise _invalid_program()

    frozen_kwargs = _freeze_bound_value(handler_kwargs)
    assert isinstance(frozen_kwargs, Mapping)
    return _CommandSnapshot(
        operation_id=operation_id,
        operation=operation,
        handler_name=handler_name,
        handler_kwargs=frozen_kwargs,
        risk_class=risk_class,
        evidence_required=evidence_required,
        depends_on=depends_on,
        preserve=preserve,
        source=source,
        execution_profiles=execution_profiles,
        minimum_freecad_version=minimum_freecad_version,
        maximum_freecad_version_exclusive=maximum_freecad_version_exclusive,
        requires_gui_main_thread=requires_gui_main_thread,
        resource_budget=frozen_budget,
        result_slots=frozen_result_slots,
    )


def _snapshot_program(program: object) -> tuple[_CommandSnapshot, ...]:
    if type(program) is not ValidatedProgram:
        raise _invalid_program()
    try:
        program.require_authentic()
        source_program = program.program
        commands = program.commands
    except Exception:
        raise _invalid_program() from None
    if type(source_program) is not ModelProgram or type(commands) is not tuple or not commands:
        raise _invalid_program()

    snapshots = tuple(_snapshot_command(command) for command in commands)
    identifiers = tuple(command.operation_id for command in snapshots)
    if len(set(identifiers)) != len(identifiers):
        raise _invalid_program()
    try:
        canonical = program._revalidate_source()
        canonical_snapshots = tuple(_snapshot_command(command) for command in canonical.commands)
    except AdapterError:
        raise
    except Exception:
        raise _invalid_program() from None
    if not _exactly_equal(snapshots, canonical_snapshots):
        raise _invalid_program()
    return snapshots


def _validated_revision(revision: object) -> str | None:
    try:
        probe = StepResult(
            ok=True,
            value=None,
            elapsed_ms=0,
            revision=revision,  # type: ignore[arg-type]
            error=None,
        )
    except Exception:
        raise AdapterError(AdapterErrorCode.INVALID_REVISION) from None
    return probe.revision


def _freeze_plan(
    commands: tuple[_CommandSnapshot, ...],
    handlers: object,
    execution_profile: object,
    freecad_version: object,
    gui_main_thread: object,
) -> tuple[_PlannedCommand, ...]:
    if type(execution_profile) is not ExecutionProfile:
        raise AdapterError(AdapterErrorCode.INVALID_EXECUTION_PROFILE)
    if any(execution_profile not in command.execution_profiles for command in commands):
        raise AdapterError(AdapterErrorCode.UNSUPPORTED_EXECUTION_PROFILE)
    if (
        type(freecad_version) is not tuple
        or len(freecad_version) != 2
        or not all(type(item) is int and item >= 0 for item in freecad_version)
        or type(gui_main_thread) is not bool
    ):
        raise AdapterError(AdapterErrorCode.INVALID_EXECUTION_PROFILE)
    if any(
        not (
            command.minimum_freecad_version
            <= freecad_version
            < command.maximum_freecad_version_exclusive
        )
        or (command.requires_gui_main_thread and not gui_main_thread)
        for command in commands
    ):
        raise AdapterError(AdapterErrorCode.UNSUPPORTED_EXECUTION_PROFILE)
    if any(
        command.resource_budget.max_runtime_ms > MAX_ADMITTED_RUNTIME_MS
        or command.resource_budget.max_created_objects > MAX_ADMITTED_CREATED_OBJECTS
        or command.resource_budget.max_result_bytes > MAX_ADMITTED_RESULT_BYTES
        for command in commands
    ):
        raise _invalid_program()
    _validate_result_references(commands)
    if not isinstance(handlers, Mapping):
        raise AdapterError(AdapterErrorCode.INVALID_HANDLERS)

    resolved: dict[str, Callable[..., object]] = {}
    plan: list[_PlannedCommand] = []
    for command in commands:
        handler = resolved.get(command.handler_name)
        if handler is None:
            try:
                candidate = handlers[command.handler_name]
            except KeyError:
                raise AdapterError(AdapterErrorCode.MISSING_HANDLER) from None
            except Exception:
                raise AdapterError(AdapterErrorCode.INVALID_HANDLERS) from None
            if not callable(candidate):
                raise AdapterError(AdapterErrorCode.NON_CALLABLE_HANDLER)
            handler = candidate
            resolved[command.handler_name] = handler
        plan.append(
            _PlannedCommand(
                operation_id=command.operation_id,
                operation=command.operation,
                handler_name=command.handler_name,
                handler_kwargs=command.handler_kwargs,
                risk_class=command.risk_class,
                evidence_required=command.evidence_required,
                depends_on=command.depends_on,
                execution_profiles=command.execution_profiles,
                minimum_freecad_version=command.minimum_freecad_version,
                maximum_freecad_version_exclusive=(command.maximum_freecad_version_exclusive),
                requires_gui_main_thread=command.requires_gui_main_thread,
                resource_budget=command.resource_budget,
                result_slots=command.result_slots,
                handler=handler,
            )
        )
    return tuple(plan)


def _bound_result_refs(value: object) -> tuple[BoundResultRef, ...]:
    if type(value) is BoundResultRef:
        return (value,)
    if type(value) is MappingProxyType:
        return tuple(ref for item in value.values() for ref in _bound_result_refs(item))
    if type(value) is tuple:
        return tuple(ref for item in value for ref in _bound_result_refs(item))
    return ()


def _validate_result_references(commands: tuple[_CommandSnapshot, ...]) -> None:
    closures: dict[str, frozenset[str]] = {}
    slots_by_command: dict[str, Mapping[str, ResultSlotMetadata]] = {}
    for command in commands:
        closure: set[str] = set()
        for dependency in command.depends_on:
            inherited = closures.get(dependency)
            if inherited is None:
                raise _invalid_program()
            closure.add(dependency)
            closure.update(inherited)
        closures[command.operation_id] = frozenset(closure)

        for reference in _bound_result_refs(command.handler_kwargs):
            if reference.command_id not in closure:
                raise _invalid_program()
            producer_slots = slots_by_command.get(reference.command_id)
            if producer_slots is None:
                raise _invalid_program()
            slot = producer_slots.get(reference.slot)
            if slot is None or slot.value_shape is not reference.value_shape:
                raise _invalid_program()

        slots_by_command[command.operation_id] = MappingProxyType(
            {slot.name: slot for slot in command.result_slots}
        )


def _facts(
    command: _PlannedCommand,
    execution_profile: ExecutionProfile,
) -> Mapping[str, object]:
    return {
        "operation": command.operation,
        "handler_name": command.handler_name,
        "risk_class": command.risk_class.value,
        "execution_profile": execution_profile.value,
    }


def _read_monotonic_ns() -> int | None:
    """Return one typed clock reading or mark an ordinary read failure."""

    try:
        value = _monotonic_ns()
    except Exception:
        return None
    return value if type(value) is int else None


def _elapsed_measurement(
    started: int | None,
    finished: int | None,
) -> tuple[int | float, bool]:
    if started is None or finished is None or finished < started:
        return 0, False
    try:
        return (finished - started) / 1_000_000, True
    except (ArithmeticError, OverflowError):
        return 0, False


def _with_execution_evidence(
    outcome: NormalizedToolOutcome,
    operation_id: str,
) -> NormalizedToolOutcome:
    result = outcome.result
    evidence = ExecutionEvidence(
        id=f"{operation_id}:execution",
        kind=EvidenceKind.OBSERVATION,
        name="execution_acknowledged",
        operation_id=operation_id,
        value={"result_ok": True},
    )
    enriched = StepResult(
        ok=result.ok,
        value=result.value,
        elapsed_ms=result.elapsed_ms,
        operation_id=result.operation_id,
        revision=result.revision,
        facts=result.facts,
        artifacts=result.artifacts,
        warnings=result.warnings,
        evidence=(evidence,),
        error=result.error,
    )
    return NormalizedToolOutcome(result=enriched, diagnostic=outcome.diagnostic)


def _resolve_bound_value(
    value: object,
    result_values: Mapping[str, Mapping[str, object]],
) -> object:
    if type(value) is BoundResultRef:
        try:
            return result_values[value.command_id][value.slot]
        except Exception:
            raise _invalid_program() from None
    if type(value) is MappingProxyType:
        return MappingProxyType(
            {key: _resolve_bound_value(item, result_values) for key, item in value.items()}
        )
    if type(value) is tuple:
        return tuple(_resolve_bound_value(item, result_values) for item in value)
    return value


def _resolve_handler_kwargs(
    values: Mapping[str, Any],
    result_values: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    return {key: _resolve_bound_value(value, result_values) for key, value in values.items()}


def _extract_result_slots(
    command: _PlannedCommand,
    outcome: NormalizedToolOutcome,
) -> Mapping[str, object] | None:
    if not command.result_slots:
        return MappingProxyType({})
    value = outcome.result.value
    if not isinstance(value, Mapping):
        return None
    extracted: dict[str, object] = {}
    try:
        for slot in command.result_slots:
            item = value[slot.result_field]
            if not _matches_value_shape(
                item,
                slot.value_shape,
                enum_values=slot.enum_values,
                allowed_units=slot.allowed_units,
            ):
                return None
            extracted[slot.name] = item
    except Exception:
        return None
    return MappingProxyType(extracted)


def _invalid_result_slot_outcome(
    outcome: NormalizedToolOutcome,
) -> NormalizedToolOutcome:
    result = outcome.result
    return normalize_tool_result(
        object(),
        operation_id=result.operation_id,
        elapsed_ms=result.elapsed_ms,
        revision=result.revision,
        facts=result.facts,
        artifacts=result.artifacts,
        warnings=result.warnings,
    )


def _object_count(counter: Callable[[], int] | None) -> int | None:
    if counter is None:
        return 0
    try:
        value = counter()
    except Exception:
        return None
    if type(value) is not int or value < 0:
        return None
    return value


def _canonical_result_size(outcome: NormalizedToolOutcome) -> int | None:
    try:
        encoded = json.dumps(
            outcome.result.to_mapping(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except Exception:
        return None
    return len(encoded)


def _budget_failure_outcome(outcome: NormalizedToolOutcome) -> NormalizedToolOutcome:
    result = outcome.result
    error = StepError(
        category=ErrorCategory.RUNTIME,
        code="resource_budget_exceeded",
        message="CAD execution exceeded its resource budget.",
        retryable=False,
        needs_input=False,
        related_objects=(),
        diagnostic_artifacts=(),
        operation_id=result.operation_id,
    )
    failed = StepResult(
        ok=False,
        value=None,
        elapsed_ms=result.elapsed_ms,
        operation_id=result.operation_id,
        revision=result.revision,
        facts=result.facts,
        artifacts=result.artifacts,
        warnings=result.warnings,
        evidence=result.evidence,
        error=error,
    )
    return NormalizedToolOutcome(result=failed)


def _apply_resource_budget(
    command: _PlannedCommand,
    outcome: NormalizedToolOutcome,
    *,
    objects_before: int,
    objects_after: int | None,
) -> NormalizedToolOutcome:
    result_bytes = _canonical_result_size(outcome)
    created_objects = None if objects_after is None else max(0, objects_after - objects_before)
    budget = command.resource_budget
    if (
        outcome.result.elapsed_ms > budget.max_runtime_ms
        or created_objects is None
        or created_objects > budget.max_created_objects
        or result_bytes is None
        or result_bytes > budget.max_result_bytes
    ):
        return _budget_failure_outcome(outcome)
    return outcome


def execute_validated_program(
    program: ValidatedProgram,
    handlers: Mapping[str, Callable[..., object]],
    *,
    execution_profile: ExecutionProfile,
    revision: str | None = None,
    freecad_version: tuple[int, int] = (1, 1),
    gui_main_thread: bool = False,
    object_count: Callable[[], int] | None = None,
) -> tuple[NormalizedToolOutcome, ...]:
    """Execute a sealed program once per command through a frozen handler plan."""

    commands = _snapshot_program(program)
    trusted_revision = _validated_revision(revision)
    if object_count is not None and not callable(object_count):
        raise _invalid_program()
    plan = _freeze_plan(
        commands,
        handlers,
        execution_profile,
        freecad_version,
        gui_main_thread,
    )

    outcomes: list[NormalizedToolOutcome] = []
    result_values: dict[str, Mapping[str, object]] = {}
    for command in plan:
        handler_kwargs = _resolve_handler_kwargs(command.handler_kwargs, result_values)
        objects_before = _object_count(object_count)
        if objects_before is None:
            raise _invalid_program()
        started = _read_monotonic_ns()
        if started is None:
            outcome = normalize_tool_result(
                {"ok": True},
                operation_id=command.operation_id,
                elapsed_ms=0,
                revision=trusted_revision,
                facts=_facts(command, execution_profile),
            )
            outcomes.append(_budget_failure_outcome(outcome))
            break
        try:
            raw = command.handler(**handler_kwargs)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                try:
                    _read_monotonic_ns()
                except BaseException:
                    pass
                raise exc
            finished = _read_monotonic_ns()
            elapsed_ms, runtime_measurement_valid = _elapsed_measurement(started, finished)
            outcome = normalize_tool_exception(
                exc,
                operation_id=command.operation_id,
                elapsed_ms=elapsed_ms,
                revision=trusted_revision,
                facts=_facts(command, execution_profile),
            )
        else:
            finished = _read_monotonic_ns()
            elapsed_ms, runtime_measurement_valid = _elapsed_measurement(started, finished)
            outcome = normalize_tool_result(
                raw,
                operation_id=command.operation_id,
                elapsed_ms=elapsed_ms,
                revision=trusted_revision,
                facts=_facts(command, execution_profile),
            )

        objects_after = _object_count(object_count)
        measurement_budget_failed = outcome.result.ok and not runtime_measurement_valid
        if measurement_budget_failed:
            outcome = _budget_failure_outcome(outcome)

        slots = None
        if outcome.result.ok:
            slots = _extract_result_slots(command, outcome)
            if slots is None:
                outcome = _invalid_result_slot_outcome(outcome)
        if outcome.result.ok and command.evidence_required:
            outcome = _with_execution_evidence(outcome, command.operation_id)
        if not measurement_budget_failed:
            outcome = _apply_resource_budget(
                command,
                outcome,
                objects_before=objects_before,
                objects_after=objects_after,
            )
        if outcome.result.ok and slots is not None:
            result_values[command.operation_id] = slots
        outcomes.append(outcome)
        if not outcome.result.ok:
            break

    return tuple(outcomes)


__all__ = [
    "AdapterError",
    "AdapterErrorCode",
    "execute_validated_program",
]
