"""Focused lifecycle gates for the private reviewed FreeCAD transaction runner."""

from __future__ import annotations

import pytest

from vibecad.parametric.freecad_reviewed_transaction import (
    NativeTransactionError,
    NativeTransactionErrorCode,
    NativeTransactionRunner,
)


class _Document:
    def __init__(
        self,
        *,
        pending: bool = False,
        commit_fails: bool = False,
        abort_restores: bool = True,
    ) -> None:
        self.HasPendingTransaction = pending
        self.commit_fails = commit_fails
        self.abort_restores = abort_restores
        self.values = ["base"]
        self._before: list[str] | None = None
        self.labels: list[str] = []
        self.recomputes = 0

    def openTransaction(self, label: str) -> None:
        self.labels.append(label)
        self._before = list(self.values)
        self.HasPendingTransaction = True

    def commitTransaction(self) -> None:
        if self.commit_fails:
            raise RuntimeError("host detail")
        self.HasPendingTransaction = False
        self._before = None

    def abortTransaction(self) -> None:
        if self.abort_restores and self._before is not None:
            self.values = self._before
        self.HasPendingTransaction = False
        self._before = None

    def recompute(self) -> None:
        self.recomputes += 1


class _OpaquePendingDocument(_Document):
    """Match FreeCAD 1.1.0, whose flag stays false inside an open transaction."""

    def openTransaction(self, label: str) -> None:
        self.labels.append(label)
        self._before = list(self.values)


def _snapshot(document: _Document) -> tuple[str, ...]:
    return tuple(document.values)


def _matches(document: _Document, before: object) -> bool:
    return tuple(document.values) == before


def test_native_transaction_commits_without_family_specific_branching() -> None:
    document = _OpaquePendingDocument()

    def apply() -> str:
        document.values.append("created")
        return "receipt"

    result = NativeTransactionRunner().run(
        document,
        label="VibeCAD trusted test family",
        snapshot=lambda: _snapshot(document),
        apply=apply,
        rollback_matches=lambda before: _matches(document, before),
    )
    assert result == "receipt"
    assert document.values == ["base", "created"]
    assert document.labels == ["VibeCAD trusted test family"]
    assert document.HasPendingTransaction is False


@pytest.mark.parametrize("failure", [RuntimeError("detail"), SystemExit("detail")])
def test_native_transaction_failure_is_bounded_and_exactly_rolled_back(failure) -> None:
    document = _Document()

    def apply() -> None:
        document.values.append("partial")
        raise failure

    with pytest.raises(NativeTransactionError) as error:
        NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted failing family",
            snapshot=lambda: _snapshot(document),
            apply=apply,
            rollback_matches=lambda before: _matches(document, before),
        )
    assert error.value.code is NativeTransactionErrorCode.TRANSACTION_FAILED
    assert "detail" not in str(error.value)
    assert document.values == ["base"]
    assert document.HasPendingTransaction is False
    assert document.recomputes == 1


def test_keyboard_interrupt_propagates_only_after_exact_rollback() -> None:
    document = _OpaquePendingDocument()

    def apply() -> None:
        document.values.append("partial")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        NativeTransactionRunner().run(
            document,
            label="VibeCAD trusted cancelled family",
            snapshot=lambda: _snapshot(document),
            apply=apply,
            rollback_matches=lambda before: _matches(document, before),
        )
    assert document.values == ["base"]
    assert document.HasPendingTransaction is False


def test_pending_commit_failure_and_rollback_mismatch_fail_closed() -> None:
    pending = _Document(pending=True)
    with pytest.raises(NativeTransactionError) as precondition:
        NativeTransactionRunner().run(
            pending,
            label="VibeCAD trusted family",
            snapshot=lambda: _snapshot(pending),
            apply=lambda: None,
            rollback_matches=lambda before: _matches(pending, before),
        )
    assert precondition.value.code is NativeTransactionErrorCode.PRECONDITION_FAILED

    commit_failure = _Document(commit_fails=True)

    def apply_commit_failure() -> None:
        commit_failure.values.append("partial")

    with pytest.raises(NativeTransactionError) as commit:
        NativeTransactionRunner().run(
            commit_failure,
            label="VibeCAD trusted family",
            snapshot=lambda: _snapshot(commit_failure),
            apply=apply_commit_failure,
            rollback_matches=lambda before: _matches(commit_failure, before),
        )
    assert commit.value.code is NativeTransactionErrorCode.TRANSACTION_FAILED
    assert commit_failure.values == ["base"]

    mismatch = _Document(abort_restores=False)

    def apply_mismatch() -> None:
        mismatch.values.append("partial")
        raise RuntimeError("detail")

    with pytest.raises(NativeTransactionError) as rollback:
        NativeTransactionRunner().run(
            mismatch,
            label="VibeCAD trusted family",
            snapshot=lambda: _snapshot(mismatch),
            apply=apply_mismatch,
            rollback_matches=lambda before: _matches(mismatch, before),
        )
    assert rollback.value.code is NativeTransactionErrorCode.ROLLBACK_FAILED
    assert mismatch.values == ["base", "partial"]
