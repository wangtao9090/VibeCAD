"""Private transaction primitive shared by reviewed FreeCAD rule families.

Family-specific code owns all topology, selection, and native property rules.
This module owns only the transaction lifecycle and exact rollback proof.  It
does not import FreeCAD, choose a TypeId, or expose an execution entry point.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

ResultT = TypeVar("ResultT")


class NativeTransactionErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    PRECONDITION_FAILED = "precondition_failed"
    TRANSACTION_FAILED = "transaction_failed"
    ROLLBACK_FAILED = "rollback_failed"


class NativeTransactionError(ValueError):
    """Bounded stable failure from the shared native transaction lifecycle."""

    def __init__(self, code: NativeTransactionErrorCode, path: str = "/") -> None:
        if type(code) is not NativeTransactionErrorCode:
            raise TypeError("code must be a NativeTransactionErrorCode")
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
        super().__init__(f"native transaction error ({code.value}) at {path}")


def _fail(code: NativeTransactionErrorCode, path: str) -> None:
    raise NativeTransactionError(code, path)


def _pending(document: object) -> bool:
    try:
        value = document.HasPendingTransaction
    except (Exception, SystemExit):
        _fail(NativeTransactionErrorCode.PRECONDITION_FAILED, "/document/transaction")
    if type(value) is not bool:
        _fail(NativeTransactionErrorCode.PRECONDITION_FAILED, "/document/transaction")
    return value


def _callable_member(document: object, name: str) -> Callable[..., object]:
    try:
        member = getattr(document, name)
    except (Exception, SystemExit):
        _fail(NativeTransactionErrorCode.PRECONDITION_FAILED, f"/document/{name}")
    if not callable(member):
        _fail(NativeTransactionErrorCode.PRECONDITION_FAILED, f"/document/{name}")
    return member


class NativeTransactionRunner:
    """Run trusted family work in one transaction with exact rollback proof.

    ``snapshot``, ``apply``, and ``rollback_matches`` are constructor-owned
    trusted callables.  ``apply`` may perform family-specific validation and
    recomputes, but it must not open or commit another transaction.  Any normal
    failure or ``SystemExit`` is converted to a bounded error after rollback.
    ``KeyboardInterrupt`` propagates only after exact rollback is proven.
    """

    __slots__ = ()

    def run(
        self,
        document: object,
        *,
        label: str,
        snapshot: Callable[[], object],
        apply: Callable[[], ResultT],
        rollback_matches: Callable[[object], bool],
    ) -> ResultT:
        if document is None or any(
            not callable(item) for item in (snapshot, apply, rollback_matches)
        ):
            _fail(NativeTransactionErrorCode.INVALID_INPUT, "/transaction")
        if type(label) is not str:
            _fail(NativeTransactionErrorCode.INVALID_INPUT, "/label")
        try:
            label_size = len(label.encode("utf-8"))
        except UnicodeError:
            label_size = 129
        if not 1 <= label_size <= 128 or not label.isprintable() or len(label.splitlines()) != 1:
            _fail(NativeTransactionErrorCode.INVALID_INPUT, "/label")
        open_transaction = _callable_member(document, "openTransaction")
        commit_transaction = _callable_member(document, "commitTransaction")
        abort_transaction = _callable_member(document, "abortTransaction")
        recompute = _callable_member(document, "recompute")
        if _pending(document):
            _fail(NativeTransactionErrorCode.PRECONDITION_FAILED, "/document/transaction")
        try:
            before = snapshot()
        except KeyboardInterrupt:
            raise
        except (Exception, SystemExit):
            _fail(NativeTransactionErrorCode.PRECONDITION_FAILED, "/document/snapshot")

        transaction_open = False
        try:
            open_transaction(label)
            transaction_open = True
            result = apply()
            commit_transaction()
            transaction_open = False
            if _pending(document):
                _fail(NativeTransactionErrorCode.TRANSACTION_FAILED, "/document/commit")
            return result
        except BaseException as error:
            rollback_ok = True
            try:
                if transaction_open:
                    abort_transaction()
                recompute()
                rollback_ok = rollback_matches(before) is True and not _pending(document)
            except BaseException:
                rollback_ok = False
            if not rollback_ok:
                raise NativeTransactionError(
                    NativeTransactionErrorCode.ROLLBACK_FAILED,
                    "/document/rollback",
                ) from None
            if isinstance(error, KeyboardInterrupt):
                raise
            if isinstance(error, NativeTransactionError):
                raise
            raise NativeTransactionError(
                NativeTransactionErrorCode.TRANSACTION_FAILED,
                "/document/transaction",
            ) from None


__all__ = [
    "NativeTransactionError",
    "NativeTransactionErrorCode",
    "NativeTransactionRunner",
]
