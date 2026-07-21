"""Provider-neutral execution metadata for VibeCAD's Agent Core."""

from vibecad.execution.registry import (
    DEFAULT_OPERATION_REGISTRY,
    ExecutionProfile,
    FieldMetadata,
    OperationMetadata,
    OperationRegistry,
    RegistryError,
    RegistryErrorCode,
    ResourceBudget,
    ResultSlotMetadata,
    RiskClass,
    ValueShape,
)

__all__ = [
    "DEFAULT_OPERATION_REGISTRY",
    "ExecutionProfile",
    "FieldMetadata",
    "OperationMetadata",
    "OperationRegistry",
    "RegistryError",
    "RegistryErrorCode",
    "ResourceBudget",
    "ResultSlotMetadata",
    "RiskClass",
    "ValueShape",
]
