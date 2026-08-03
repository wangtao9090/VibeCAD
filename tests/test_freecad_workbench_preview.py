from __future__ import annotations

import hashlib
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "freecad" / "VibeCAD"))

from vibecad_workbench.preview import (  # noqa: E402, I001
    PreviewBinding,
    PreviewCoordinator,
    PreviewError,
    _validate_closed_descriptor,
)


PROJECT_ID = "project_" + "1" * 32
TASK_ID = "task_" + "2" * 32
DRAFT_ID = "draft_" + "3" * 32
CHECKOUT_ID = "checkout_" + "4" * 32
GRANT_ID = "file_grant_" + "5" * 32
OPEN_KEY = "checkout_open_" + "6" * 32
DIGEST = "7" * 64
SIZE = 23
REVISION_ID = "revision_" + "8" * 32
MANIFEST_DIGEST = "9" * 64
BASE_REVISION_ID = "revision_" + "a" * 32
BASE_MANIFEST_DIGEST = "b" * 64


def _source(kind: str = "head") -> dict[str, object]:
    if kind == "head":
        return {"kind": "head", "project_id": PROJECT_ID}
    return {
        "kind": "draft",
        "task_id": TASK_ID,
        "draft_id": DRAFT_ID,
        "expected_generation": 9,
    }


def _descriptor(
    *,
    checkout_id: str = CHECKOUT_ID,
    source: dict[str, object] | None = None,
    dirty: bool = False,
    state: str = "open",
    source_liveness: str = "live",
    digest: str = DIGEST,
    size: int = SIZE,
) -> dict[str, object]:
    requested = _source() if source is None else source
    resolved_source = {
        "kind": requested["kind"],
        "project_id": PROJECT_ID,
        "revision_id": REVISION_ID,
        "manifest_sha256": MANIFEST_DIGEST,
        "model_sha256": digest,
        "size_bytes": size,
        "task_id": requested.get("task_id"),
        "draft_id": requested.get("draft_id"),
        "task_generation": requested.get("expected_generation"),
    }
    is_draft = requested["kind"] == "draft"
    return {
        "checkout_id": checkout_id,
        "open_key": OPEN_KEY,
        "state": state,
        "authoritative": False,
        "dirty": dirty,
        "source": resolved_source,
        "initial_model_sha256": digest,
        "current_model_sha256": digest,
        "current_size_bytes": size,
        "source_head": {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "generation": 1,
            "revision_id": BASE_REVISION_ID if is_draft else REVISION_ID,
            "manifest_sha256": BASE_MANIFEST_DIGEST if is_draft else MANIFEST_DIGEST,
        },
        "source_liveness": source_liveness,
    }


def _base_head_descriptor(
    *,
    checkout_id: str = CHECKOUT_ID,
    state: str = "open",
) -> dict[str, object]:
    descriptor = _descriptor(checkout_id=checkout_id, state=state)
    resolved = descriptor["source"]
    source_head = descriptor["source_head"]
    assert type(resolved) is dict
    assert type(source_head) is dict
    resolved["revision_id"] = BASE_REVISION_ID
    resolved["manifest_sha256"] = BASE_MANIFEST_DIGEST
    source_head["revision_id"] = BASE_REVISION_ID
    source_head["manifest_sha256"] = BASE_MANIFEST_DIGEST
    return descriptor


def _claim(
    local_path: Path,
    *,
    checkout_id: str = CHECKOUT_ID,
    digest: str = DIGEST,
    size: int = SIZE,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "grant_id": GRANT_ID,
        "checkout_id": checkout_id,
        "purpose": "open_managed_checkout",
        "local_path": str(local_path),
        "current_model_sha256": digest,
        "current_size_bytes": size,
    }


def _acquired(
    local_path: Path,
    *,
    source: dict[str, object] | None = None,
    descriptor: dict[str, object] | None = None,
    claim: dict[str, object] | None = None,
) -> dict[str, object]:
    canonical_source = _source() if source is None else source
    return {
        "source": canonical_source,
        "open_key": OPEN_KEY,
        "descriptor": (_descriptor(source=canonical_source) if descriptor is None else descriptor),
        "claim": _claim(local_path) if claim is None else claim,
    }


class _Client:
    def __init__(
        self,
        descriptor: dict[str, object],
        claim: dict[str, object],
        *,
        claim_error: bool = False,
        grant: dict[str, object] | None = None,
        close_response: dict[str, object] | None = None,
        close_error: bool = False,
    ) -> None:
        self.descriptor = descriptor
        self.claim = claim
        self.claim_error = claim_error
        self.grant = (
            {
                "schema_version": 1,
                "grant_id": GRANT_ID,
                "purpose": "open_managed_checkout",
                "expires_in_ms": 30_000,
            }
            if grant is None
            else grant
        )
        self.close_response = (
            _descriptor(state="closed") if close_response is None else close_response
        )
        self.close_error = close_error
        self.calls: list[tuple[str, object]] = []
        self.claimed = False

    def open_checkout(self, *, open_key: object, source: object) -> dict[str, object]:
        self.calls.append(("open_checkout", {"open_key": open_key, "source": source}))
        return self.descriptor | {"file_grant": self.grant}

    def claim_file_grant(self, *, grant_id: object) -> dict[str, object]:
        self.calls.append(("claim_file_grant", grant_id))
        if self.claimed:
            raise RuntimeError("grant reused")
        self.claimed = True
        if self.claim_error:
            raise RuntimeError("synthetic claim failure")
        return self.claim

    def close_checkout(self, *, checkout_id: object) -> dict[str, object]:
        self.calls.append(("close_checkout", checkout_id))
        if self.close_error:
            raise RuntimeError("synthetic close failure")
        return self.close_response


class _Document:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.FileName = ""
        self.Modified = False


class _Host:
    def __init__(
        self,
        *,
        reuse: bool = False,
        modified: bool = False,
        registry_name: str | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.reuse = reuse
        self.modified = modified
        self.registry_name = registry_name
        self.events = [] if events is None else events
        self.documents: dict[str, _Document] = {}
        self.opened_paths: list[str] = []
        self.close_failures = 0
        self._next_document_index: int | None = None

    def openDocument(self, local_path: str) -> _Document:
        self.events.append("document.open")
        self.opened_paths.append(local_path)
        if self.reuse and self.documents:
            return next(iter(self.documents.values()))
        if self._next_document_index is None:
            self._next_document_index = len(self.documents) + 1
        document = _Document(f"Preview{self._next_document_index}")
        self._next_document_index += 1
        document.FileName = local_path
        document.Modified = self.modified
        self.documents[self.registry_name or document.Name] = document
        return document

    def listDocuments(self) -> dict[str, _Document]:
        return dict(self.documents)

    def getDocument(self, name: str) -> _Document | None:
        return self.documents.get(name)

    def closeDocument(self, name: str) -> None:
        self.events.append("document.close")
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("synthetic document close failure")
        self.documents.pop(name, None)


class _UnboundTouched:
    def __get__(self, instance: object, _owner: type) -> object:
        if instance is None:
            return self

        def invalid_bound_call() -> bool:
            raise TypeError("synthetic FreeCAD bound isTouched failure")

        return invalid_bound_call

    def __call__(self, document: object) -> object:
        return document._touched  # type: ignore[attr-defined]


class _FreeCAD11Document:
    isTouched = _UnboundTouched()

    def __init__(self, name: str, touched: object) -> None:
        self.Name = name
        self.FileName = ""
        self._touched = touched


class _BrokenTouchedDocument(_Document):
    isTouched = None


class _BrokenTouchedHost(_Host):
    def openDocument(self, local_path: str) -> _BrokenTouchedDocument:
        self.events.append("document.open")
        self.opened_paths.append(local_path)
        document = _BrokenTouchedDocument("Preview1")
        document.FileName = local_path
        self.documents[document.Name] = document
        return document


class _FreeCAD11Host(_Host):
    def __init__(self, touched: object) -> None:
        super().__init__()
        self.touched = touched

    def openDocument(self, local_path: str) -> _FreeCAD11Document:
        self.events.append("document.open")
        self.opened_paths.append(local_path)
        document = _FreeCAD11Document("Preview1", self.touched)
        document.FileName = local_path
        self.documents[document.Name] = document
        return document


def test_acquire_opens_then_claims_on_the_same_client_once_and_detaches_mapping(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    client = _Client(_descriptor(), _claim(path))

    acquired = PreviewCoordinator.acquire(
        client,
        source=_source(),
        open_key=OPEN_KEY,
    )

    assert client.calls == [
        ("open_checkout", {"open_key": OPEN_KEY, "source": _source()}),
        ("claim_file_grant", GRANT_ID),
    ]
    assert type(acquired) is dict
    assert acquired == _acquired(path)
    assert all(type(value) in {dict, str, int, bool, type(None)} for value in acquired.values())


@pytest.mark.parametrize("failure", ("raise", "wrong-grant"))
def test_fail_acquire_closes_exact_orphan_checkout_once(
    tmp_path: Path,
    failure: str,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    claim = _claim(path)
    if failure == "wrong-grant":
        claim["grant_id"] = "file_grant_" + "a" * 32
    client = _Client(
        _descriptor(),
        claim,
        claim_error=failure == "raise",
    )

    with pytest.raises((PreviewError, RuntimeError)):
        PreviewCoordinator.acquire(client, source=_source(), open_key=OPEN_KEY)

    assert [call[0] for call in client.calls] == [
        "open_checkout",
        "claim_file_grant",
        "close_checkout",
    ]
    assert client.calls[-1] == ("close_checkout", CHECKOUT_ID)


@pytest.mark.parametrize(
    ("descriptor_change", "claim_change"),
    [
        ({"checkout_id": "checkout_" + "a" * 32}, {}),
        ({"current_model_sha256": "b" * 64}, {}),
        ({"current_size_bytes": SIZE + 1}, {}),
        ({}, {"checkout_id": "checkout_" + "c" * 32}),
        ({}, {"current_model_sha256": "d" * 64}),
        ({}, {"current_size_bytes": SIZE + 1}),
    ],
)
def test_open_rejects_checkout_digest_or_size_disagreement_before_host_access(
    tmp_path: Path,
    descriptor_change: dict[str, object],
    claim_change: dict[str, object],
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    descriptor = _descriptor() | descriptor_change
    claim = _claim(path) | claim_change
    host = _Host()

    with pytest.raises(PreviewError):
        PreviewCoordinator(host).open(_acquired(path, descriptor=descriptor, claim=claim))

    assert host.opened_paths == []


def test_open_passes_only_the_exact_absolute_claimed_path_to_freecad(
    tmp_path: Path,
) -> None:
    exact_path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    coordinator = PreviewCoordinator(host)

    binding = coordinator.open(_acquired(exact_path))

    assert type(binding) is PreviewBinding
    assert host.opened_paths == [str(exact_path)]
    assert binding.document is host.getDocument(binding.document_name)
    with pytest.raises(FrozenInstanceError):
        binding.document_name = "forged"

    relative = Path(CHECKOUT_ID) / "model.FCStd"
    with pytest.raises(PreviewError):
        coordinator.open(_acquired(relative, claim=_claim(relative)))
    assert host.opened_paths == [str(exact_path)]


def test_freecad_11_uses_unbound_is_touched_without_modified(
    tmp_path: Path,
) -> None:
    exact_path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _FreeCAD11Host(False)
    coordinator = PreviewCoordinator(host)

    binding = coordinator.open(_acquired(exact_path))

    with pytest.raises(TypeError, match="bound isTouched failure"):
        binding.document.isTouched()
    assert coordinator.validate_binding(CHECKOUT_ID, _descriptor()) is binding


@pytest.mark.parametrize("touched", (True, "not-a-bool"))
def test_freecad_11_touched_or_non_bool_fails_closed(
    tmp_path: Path,
    touched: object,
) -> None:
    exact_path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _FreeCAD11Host(touched)

    with pytest.raises(PreviewError):
        PreviewCoordinator(host).open(_acquired(exact_path))

    assert host.documents == {}
    assert host.events == ["document.open", "document.close"]


def test_present_but_non_callable_is_touched_does_not_fallback_to_modified(
    tmp_path: Path,
) -> None:
    exact_path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _BrokenTouchedHost()

    with pytest.raises(PreviewError):
        PreviewCoordinator(host).open(_acquired(exact_path))

    assert host.documents == {}
    assert host.events == ["document.open", "document.close"]


def test_draft_open_accepts_candidate_revision_distinct_from_base_head(
    tmp_path: Path,
) -> None:
    source = _source("draft")
    descriptor = _descriptor(source=source)
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()

    binding = PreviewCoordinator(host).open(_acquired(path, source=source, descriptor=descriptor))

    assert descriptor["source"]["revision_id"] == REVISION_ID
    assert descriptor["source"]["manifest_sha256"] == MANIFEST_DIGEST
    assert descriptor["source_head"]["revision_id"] == BASE_REVISION_ID
    assert descriptor["source_head"]["manifest_sha256"] == BASE_MANIFEST_DIGEST
    assert binding.document is host.getDocument(binding.document_name)
    assert host.opened_paths == [str(path)]


def test_head_and_draft_require_distinct_registered_document_identity(
    tmp_path: Path,
) -> None:
    host = _Host(reuse=True)
    coordinator = PreviewCoordinator(host)
    head_path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    coordinator.open(_acquired(head_path))
    draft_source = _source("draft")
    draft_checkout = "checkout_" + "d" * 32
    draft_path = (tmp_path / draft_checkout / "model.FCStd").resolve()
    draft_descriptor = _descriptor(
        checkout_id=draft_checkout,
        source=draft_source,
    )
    draft_claim = _claim(draft_path, checkout_id=draft_checkout)

    with pytest.raises(PreviewError):
        coordinator.open(
            _acquired(
                draft_path,
                source=draft_source,
                descriptor=draft_descriptor,
                claim=draft_claim,
            )
        )

    assert len(host.documents) == 1


def test_fail_new_invalid_document_is_rolled_back_but_reused_document_is_preserved(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    new_host = _Host(modified=True)

    with pytest.raises(PreviewError):
        PreviewCoordinator(new_host).open(_acquired(path))

    assert new_host.documents == {}
    assert new_host.events == ["document.open", "document.close"]

    reused_host = _Host(reuse=True, modified=True)
    existing = _Document("Existing")
    existing.Modified = True
    reused_host.documents[existing.Name] = existing

    with pytest.raises(PreviewError):
        PreviewCoordinator(reused_host).open(_acquired(path))

    assert reused_host.documents == {"Existing": existing}
    assert reused_host.events == ["document.open"]


def test_fix04_legacy_registry_name_drift_is_sticky_and_never_closed(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host(registry_name="RegistryName")
    coordinator = PreviewCoordinator(host)

    with pytest.raises(PreviewError) as raised:
        coordinator.open(_acquired(path))

    assert raised.value.recovery_required is True
    assert len(host.documents) == 1
    assert host.events == ["document.open"]
    assert coordinator.ready_checkout_ids() == ()


@pytest.mark.parametrize(
    "descriptor",
    [
        _descriptor(dirty=True),
        _descriptor(state="closed"),
        _descriptor(source_liveness="stale"),
        _descriptor(source_liveness="revoked"),
        _descriptor(source_liveness="recovery_required"),
    ],
)
def test_dirty_closed_stale_revoked_or_recovery_required_is_fail_closed(
    tmp_path: Path,
    descriptor: dict[str, object],
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path))

    assert coordinator.review_eligible(binding, descriptor) is False
    assert coordinator.review_eligible(binding, _descriptor()) is False


def test_modified_document_permanently_disables_the_open_cycle(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path))
    binding.document.Modified = True

    assert coordinator.review_eligible(binding, _descriptor()) is False


@pytest.mark.parametrize(
    "file_shape",
    ("regular", "symlink", "hardlink"),
)
def test_fix04_review_attestor_requires_exact_owned_single_link_regular_file(
    tmp_path: Path,
    file_shape: str,
) -> None:
    content = b"VibeCAD secure attestation model\n"
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / CHECKOUT_ID / "model.FCStd"
    path.parent.mkdir(parents=True)
    if file_shape == "regular":
        path.write_bytes(content)
    else:
        target = tmp_path / f"{file_shape}-target.FCStd"
        target.write_bytes(content)
        if file_shape == "symlink":
            path.symlink_to(target)
        else:
            path.hardlink_to(target)
    descriptor = _descriptor(
        digest=digest,
        size=len(content),
    )
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(
        _acquired(
            path,
            descriptor=descriptor,
            claim=_claim(
                path,
                digest=digest,
                size=len(content),
            ),
        )
    )
    cycle = coordinator._cycle
    assert cycle is not None

    if file_shape == "regular":
        assert coordinator.attest_review_binding(CHECKOUT_ID, descriptor) is binding
        assert cycle.poisoned is False
        return

    with pytest.raises(PreviewError):
        coordinator.attest_review_binding(CHECKOUT_ID, descriptor)
    assert cycle.poisoned is True
    assert coordinator.aggregate_review_eligible() is False


def test_fix04_review_attestor_missing_file_normalizes_and_poisons(
    tmp_path: Path,
) -> None:
    path = tmp_path / CHECKOUT_ID / "model.FCStd"
    descriptor = _descriptor()
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path, descriptor=descriptor))

    with pytest.raises(PreviewError) as raised:
        coordinator.attest_review_binding(CHECKOUT_ID, descriptor)

    assert isinstance(raised.value.primary_error, FileNotFoundError)
    assert binding.document.FileName == str(path.resolve())
    assert coordinator._cycle is not None
    assert coordinator._cycle.poisoned is True
    assert coordinator.aggregate_review_eligible() is False


@pytest.mark.parametrize(
    "drift",
    (
        "descriptor-extra",
        "resolved-project",
        "source-head-revision",
        "task-generation",
        "resolved-digest",
        "resolved-size",
    ),
)
def test_fail_exact_descriptor_and_cross_field_drift_before_freecad(
    tmp_path: Path,
    drift: str,
) -> None:
    requested = _source("draft") if drift == "task-generation" else _source()
    descriptor = _descriptor(source=requested)
    if drift == "descriptor-extra":
        descriptor["unexpected"] = True
    elif drift == "resolved-project":
        descriptor["source"]["project_id"] = "project_" + "a" * 32
    elif drift == "source-head-revision":
        descriptor["source_head"]["revision_id"] = "revision_" + "b" * 32
    elif drift == "task-generation":
        descriptor["source"]["task_generation"] = 10
    elif drift == "resolved-digest":
        descriptor["source"]["model_sha256"] = "c" * 64
    else:
        descriptor["source"]["size_bytes"] = SIZE + 1
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()

    with pytest.raises(PreviewError):
        PreviewCoordinator(host).open(_acquired(path, source=requested, descriptor=descriptor))

    assert host.opened_paths == []


def test_fail_refresh_immutable_descriptor_drift_is_sticky(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path))
    drifted = _descriptor()
    drifted["source_head"]["generation"] = 2

    assert coordinator.review_eligible(binding, drifted) is False
    assert coordinator.review_eligible(binding, _descriptor()) is False
    binding.document.Modified = False
    assert coordinator.review_eligible(binding, _descriptor()) is False


def test_c02_refresh_source_drift_retains_replacement_for_exact_cleanup(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path))
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    stale_source = _source() | {"project_id": "project_" + "e" * 32}
    replacement_checkout = "checkout_" + "c" * 32
    replacement_path = (tmp_path / replacement_checkout / "model.FCStd").resolve()
    replacement_descriptor = _descriptor(
        checkout_id=replacement_checkout,
        source=stale_source,
    )
    replacement_resolved = replacement_descriptor["source"]
    replacement_head = replacement_descriptor["source_head"]
    assert type(replacement_resolved) is dict
    assert type(replacement_head) is dict
    replacement_resolved["project_id"] = stale_source["project_id"]
    replacement_head["project_id"] = stale_source["project_id"]

    with pytest.raises(PreviewError):
        coordinator.refresh(
            binding,
            _acquired(
                replacement_path,
                source=stale_source,
                descriptor=replacement_descriptor,
                claim=_claim(
                    replacement_path,
                    checkout_id=replacement_checkout,
                ),
            ),
        )

    assert host.opened_paths == [str(path)]
    assert tuple(coordinator._owned) == (CHECKOUT_ID, replacement_checkout)
    assert coordinator._cycle is cycle
    assert coordinator._active_cycle_id() == cycle_id
    assert cycle.poisoned is True
    assert coordinator.binding_for_checkout(CHECKOUT_ID) is binding
    replacement = coordinator._owned[replacement_checkout]
    assert replacement.binding is None
    assert replacement.document is None
    assert replacement.document_name is None
    assert replacement.document_closed is True
    assert replacement.checkout_closed is False

    closed: list[str] = []
    client_closed: list[None] = []

    def close_checkout(checkout_id: str) -> dict[str, object]:
        closed.append(checkout_id)
        descriptor = coordinator._owned[checkout_id].descriptor
        assert type(descriptor) is dict
        response = deepcopy(descriptor)
        response["state"] = "closed"
        return response

    coordinator.close_all(
        close_checkout=close_checkout,
        close_client=lambda: client_closed.append(None),
    )
    coordinator.close_all(
        close_checkout=close_checkout,
        close_client=lambda: client_closed.append(None),
    )

    assert closed == [CHECKOUT_ID, replacement_checkout]
    assert client_closed == [None]
    assert host.events == ["document.open", "document.close"]
    assert coordinator.cleanup_complete() is True
    assert coordinator._cycle is cycle
    assert coordinator._active_cycle_id() == cycle_id
    assert cycle.poisoned is True


def test_refresh_replaces_binding_with_one_new_checkout_authority(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path))
    replacement_checkout = "checkout_" + "c" * 32
    replacement_path = (tmp_path / replacement_checkout / "model.FCStd").resolve()
    replacement_acquired = _acquired(
        replacement_path,
        descriptor=_descriptor(checkout_id=replacement_checkout),
        claim=_claim(
            replacement_path,
            checkout_id=replacement_checkout,
        ),
    )

    replacement = coordinator.refresh(binding, replacement_acquired)

    assert replacement is coordinator.binding_for_checkout(replacement_checkout)
    assert replacement is not binding
    assert dict(replacement.descriptor)["checkout_id"] == replacement_checkout
    assert host.opened_paths == [str(path), str(replacement_path)]
    assert host.events == ["document.open", "document.close", "document.open"]
    assert tuple(coordinator._owned) == (CHECKOUT_ID, replacement_checkout)
    assert coordinator._owned[CHECKOUT_ID].document_closed is True
    assert coordinator._owned[CHECKOUT_ID].checkout_closed is False
    assert coordinator._owned[replacement_checkout].binding is replacement
    assert sum(record.binding is replacement for record in coordinator._owned.values()) == 1


def test_cleanup_is_document_then_checkout_then_client_and_is_at_most_once(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host(events=events)
    coordinator = PreviewCoordinator(host)
    coordinator.open(_acquired(path))

    def close_checkout(checkout_id: str) -> dict[str, object]:
        events.append(f"checkout.close:{checkout_id}")
        return _descriptor(checkout_id=checkout_id, state="closed")

    coordinator.close_all(
        close_checkout=close_checkout,
        close_client=lambda: events.append("client.close"),
    )
    coordinator.close_all(
        close_checkout=close_checkout,
        close_client=lambda: events.append("client.close"),
    )

    assert events == [
        "document.open",
        "document.close",
        f"checkout.close:{CHECKOUT_ID}",
        "client.close",
    ]


@pytest.mark.parametrize(
    "malformation",
    ("grant-extra", "descriptor-extra", "nested-serialization"),
)
def test_fix02_raw_checkout_identity_precedes_nested_acquire_validation(
    tmp_path: Path,
    malformation: str,
) -> None:
    class _SerializedDict(dict[str, object]):
        pass

    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    descriptor = _descriptor()
    grant = {
        "schema_version": 1,
        "grant_id": GRANT_ID,
        "purpose": "open_managed_checkout",
        "expires_in_ms": 30_000,
    }
    if malformation == "grant-extra":
        grant["unexpected"] = None
    elif malformation == "descriptor-extra":
        descriptor["unexpected"] = None
    else:
        descriptor["source"] = _SerializedDict(descriptor["source"])
    client = _Client(descriptor, _claim(path), grant=grant)

    with pytest.raises(PreviewError):
        PreviewCoordinator.acquire(client, source=_source(), open_key=OPEN_KEY)

    assert client.calls[-1] == ("close_checkout", CHECKOUT_ID)
    assert [name for name, _payload in client.calls].count("close_checkout") == 1


@pytest.mark.parametrize("nested", ("source_head", "grant", "claim"))
@pytest.mark.parametrize("version", (True, 0, 2))
def test_fix02_nested_schema_versions_require_exact_integer_one(
    tmp_path: Path,
    nested: str,
    version: object,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    descriptor = _descriptor()
    claim = _claim(path)
    grant = {
        "schema_version": 1,
        "grant_id": GRANT_ID,
        "purpose": "open_managed_checkout",
        "expires_in_ms": 30_000,
    }
    if nested == "source_head":
        descriptor["source_head"]["schema_version"] = version
    elif nested == "grant":
        grant["schema_version"] = version
    else:
        claim["schema_version"] = version
    client = _Client(descriptor, claim, grant=grant)

    with pytest.raises(PreviewError):
        PreviewCoordinator.acquire(client, source=_source(), open_key=OPEN_KEY)

    assert client.calls[-1] == ("close_checkout", CHECKOUT_ID)


@pytest.mark.parametrize(
    "close_response",
    (
        _descriptor(),
        _descriptor(state="closed") | {"unexpected": None},
        _descriptor(
            checkout_id="checkout_" + "a" * 32,
            state="closed",
        ),
    ),
)
def test_fix02_acquire_cleanup_requires_exact_closed_acknowledgement(
    tmp_path: Path,
    close_response: dict[str, object],
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    claim = _claim(path)
    claim["grant_id"] = "file_grant_" + "a" * 32
    client = _Client(
        _descriptor(),
        claim,
        close_response=close_response,
    )

    with pytest.raises(PreviewError) as raised:
        PreviewCoordinator.acquire(client, source=_source(), open_key=OPEN_KEY)

    assert raised.value.recovery_required is True
    assert raised.value.cleanup_error is not None
    assert client.calls[-1] == ("close_checkout", CHECKOUT_ID)


def test_fix02_clean_preexisting_document_is_rejected_and_preserved(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host(reuse=True)
    existing = _Document("Existing")
    host.documents[existing.Name] = existing
    coordinator = PreviewCoordinator(host)

    with pytest.raises(PreviewError) as raised:
        coordinator.open(_acquired(path))

    assert raised.value.recovery_required is False
    assert host.documents == {"Existing": existing}
    assert host.events == ["document.open"]
    assert coordinator.ready_checkout_ids() == (CHECKOUT_ID,)


def test_fix02_accepts_exactly_one_new_document_without_mutating_prior_registry(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    existing = _Document("Existing")
    host.documents[existing.Name] = existing
    before = dict(host.documents)
    coordinator = PreviewCoordinator(host)

    binding = coordinator.open(_acquired(path))

    assert host.documents["Existing"] is existing
    assert all(host.documents[name] is document for name, document in before.items())
    assert set(host.documents) == {"Existing", binding.document_name}
    assert binding.document_name not in before
    assert host.documents[binding.document_name] is binding.document
    coordinator.close_documents()
    assert host.documents == before
    assert coordinator.ready_checkout_ids() == (CHECKOUT_ID,)


def test_fix04_legacy_register_then_raise_is_sticky_unknown_and_never_rolled_back(
    tmp_path: Path,
) -> None:
    class _RegisterThenRaiseHost(_Host):
        def openDocument(self, local_path: str) -> _Document:
            self.events.append("document.open")
            self.opened_paths.append(local_path)
            document = _Document("RegisteredBeforeRaise")
            self.documents[document.Name] = document
            raise RuntimeError("synthetic register-then-raise")

    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _RegisterThenRaiseHost()
    coordinator = PreviewCoordinator(host)

    with pytest.raises(PreviewError) as raised:
        coordinator.open(_acquired(path))

    assert raised.value.recovery_required is True
    assert set(host.documents) == {"RegisteredBeforeRaise"}
    assert host.events == ["document.open"]
    assert coordinator.ready_checkout_ids() == ()


@pytest.mark.parametrize("mutation", ("name-collision", "multiple-delta"))
def test_fix02_ambiguous_registry_mutation_retains_document_authority(
    tmp_path: Path,
    mutation: str,
) -> None:
    class _AmbiguousHost(_Host):
        def openDocument(self, local_path: str) -> _Document:
            self.events.append("document.open")
            self.opened_paths.append(local_path)
            if mutation == "name-collision":
                document = _Document("Existing")
                self.documents[document.Name] = document
                return document
            document = _Document("Preview")
            self.documents[document.Name] = document
            self.documents["UnexpectedSidecar"] = _Document("UnexpectedSidecar")
            return document

    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _AmbiguousHost()
    existing = _Document("Existing")
    host.documents[existing.Name] = existing
    coordinator = PreviewCoordinator(host)

    with pytest.raises(PreviewError) as raised:
        coordinator.open(_acquired(path))

    assert raised.value.recovery_required is True
    assert coordinator.ready_checkout_ids() == ()


def test_fix02_rollback_close_failure_blocks_checkout_until_document_retry(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host(modified=True)
    host.close_failures = 1
    coordinator = PreviewCoordinator(host)

    with pytest.raises(PreviewError) as raised:
        coordinator.open(_acquired(path))

    assert raised.value.recovery_required is False
    assert coordinator.ready_checkout_ids() == ()
    assert len(host.documents) == 1
    retained_registry = dict(host.documents)
    assert all(host.documents[name] is document for name, document in retained_registry.items())

    coordinator.close_documents()

    assert host.documents == {}
    assert host.events == [
        "document.open",
        "document.close",
        "document.close",
    ]
    assert coordinator.ready_checkout_ids() == (CHECKOUT_ID,)


def test_fix04_close_failure_with_exact_registry_identity_is_retryable(
    tmp_path: Path,
) -> None:
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host()
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path))
    retained_registry = dict(host.documents)
    host.close_failures = 1

    with pytest.raises(PreviewError) as raised:
        coordinator.discard_document(CHECKOUT_ID)

    assert raised.value.recovery_required is False
    assert set(host.documents) == set(retained_registry)
    assert all(host.documents[name] is document for name, document in retained_registry.items())
    assert coordinator.binding_for_checkout(CHECKOUT_ID) is binding
    assert coordinator.ready_checkout_ids() == ()

    coordinator.close_documents()

    assert host.documents == {}
    assert host.events == [
        "document.open",
        "document.close",
        "document.close",
    ]
    assert coordinator.ready_checkout_ids() == (CHECKOUT_ID,)


def test_fix04_close_failure_with_registry_drift_retains_sticky_authority(
    tmp_path: Path,
) -> None:
    class _DriftThenRaiseHost(_Host):
        def __init__(self) -> None:
            super().__init__()
            self.close_calls = 0
            self.replacement: _Document | None = None
            self.added: _Document | None = None

        def closeDocument(self, name: str) -> None:
            self.events.append("document.close")
            self.close_calls += 1
            self.replacement = _Document(name)
            self.added = _Document("UserAddedDuringClose")
            self.documents[name] = self.replacement
            self.documents[self.added.Name] = self.added
            raise RuntimeError("synthetic close with registry drift")

    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _DriftThenRaiseHost()
    existing = _Document("Existing")
    host.documents[existing.Name] = existing
    coordinator = PreviewCoordinator(host)
    binding = coordinator.open(_acquired(path))

    with pytest.raises(PreviewError) as raised:
        coordinator.discard_document(CHECKOUT_ID)

    assert raised.value.recovery_required is True
    assert coordinator.binding_for_checkout(CHECKOUT_ID) is binding
    assert coordinator.ready_checkout_ids() == ()
    assert host.close_calls == 1
    assert host.documents["Existing"] is existing
    assert host.documents[binding.document_name] is host.replacement
    assert host.documents["UserAddedDuringClose"] is host.added
    retained_registry = dict(host.documents)

    with pytest.raises(PreviewError) as retried:
        coordinator.close_documents()

    assert retried.value.recovery_required is True
    assert host.close_calls == 1
    assert set(host.documents) == set(retained_registry)
    assert all(host.documents[name] is document for name, document in retained_registry.items())
    assert coordinator.ready_checkout_ids() == ()


def test_fix03_review_requires_exactly_one_head_and_one_draft_binding(
    tmp_path: Path,
) -> None:
    host = _Host()
    coordinator = PreviewCoordinator(host)
    head_path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    coordinator.open(_acquired(head_path, descriptor=_base_head_descriptor()))

    assert coordinator.aggregate_review_eligible() is False

    draft_source = _source("draft")
    draft_checkout = "checkout_" + "d" * 32
    draft_path = (tmp_path / draft_checkout / "model.FCStd").resolve()
    draft_binding = coordinator.open(
        _acquired(
            draft_path,
            source=draft_source,
            descriptor=_descriptor(
                checkout_id=draft_checkout,
                source=draft_source,
            ),
            claim=_claim(draft_path, checkout_id=draft_checkout),
        )
    )
    assert coordinator.aggregate_review_eligible() is True

    duplicate_checkout = "checkout_" + "e" * 32
    duplicate_path = (tmp_path / duplicate_checkout / "model.FCStd").resolve()
    with pytest.raises(PreviewError):
        coordinator.open(
            _acquired(
                duplicate_path,
                descriptor=_descriptor(checkout_id=duplicate_checkout),
                claim=_claim(duplicate_path, checkout_id=duplicate_checkout),
            )
        )
    cycle = coordinator._cycle
    assert cycle is not None
    assert cycle.poisoned is True
    assert coordinator.aggregate_review_eligible() is False
    assert coordinator._owned[duplicate_checkout].binding is None
    assert coordinator._owned[duplicate_checkout].document is None
    assert coordinator._owned[duplicate_checkout].checkout_closed is False
    assert draft_binding.document is host.getDocument(draft_binding.document_name)


def test_fix03_inconsistent_list_get_registry_snapshot_rejects_ownership(
    tmp_path: Path,
) -> None:
    class _InconsistentRegistryHost(_Host):
        def __init__(self) -> None:
            super().__init__()
            self.user_document = _Document("UserDocument")
            self.imposter = _Document("UserDocument")
            self.documents[self.user_document.Name] = self.user_document
            self.closed_names: list[str] = []

        def getDocument(self, name: str) -> _Document | None:
            if name == self.user_document.Name:
                return self.imposter
            return super().getDocument(name)

        def closeDocument(self, name: str) -> None:
            self.closed_names.append(name)
            super().closeDocument(name)

    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _InconsistentRegistryHost()

    with pytest.raises(PreviewError) as raised:
        PreviewCoordinator(host).open(_acquired(path))

    assert raised.value.recovery_required is True
    assert host.documents["UserDocument"] is host.user_document
    assert "UserDocument" not in host.closed_names


@pytest.mark.parametrize("operation", ("rollback", "discard"))
def test_fix03_cleanup_requires_exact_registry_minus_target(
    tmp_path: Path,
    operation: str,
) -> None:
    class _CollateralMutationHost(_Host):
        def __init__(self, *, modified: bool) -> None:
            super().__init__(modified=modified)
            self.user_document = _Document("UserDocument")
            self.documents[self.user_document.Name] = self.user_document
            self.inject_collateral = operation == "rollback"
            self.closed_names: list[str] = []

        def closeDocument(self, name: str) -> None:
            self.closed_names.append(name)
            super().closeDocument(name)
            if self.inject_collateral:
                collateral = _Document("CollateralDocument")
                self.documents[collateral.Name] = collateral

    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _CollateralMutationHost(modified=operation == "rollback")
    coordinator = PreviewCoordinator(host)

    if operation == "rollback":
        with pytest.raises(PreviewError) as raised:
            coordinator.open(_acquired(path))
    else:
        binding = coordinator.open(_acquired(path))
        host.inject_collateral = True
        with pytest.raises(PreviewError) as raised:
            coordinator.discard_document(CHECKOUT_ID)
        assert binding.document_name in host.closed_names

    assert raised.value.recovery_required is True
    assert host.documents["UserDocument"] is host.user_document
    assert "UserDocument" not in host.closed_names
    assert host.closed_names == ["Preview2"]
    assert "CollateralDocument" in host.documents


def test_fix03_close_all_requires_exact_full_closed_descriptor(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _Host(events=events)
    coordinator = PreviewCoordinator(host)
    coordinator.open(_acquired(path))

    with pytest.raises(PreviewError) as raised:
        coordinator.close_all(
            close_checkout=lambda checkout_id: events.append(f"checkout.close:{checkout_id}"),
            close_client=lambda: events.append("client.close"),
        )

    assert raised.value.recovery_required is True
    assert events == [
        "document.open",
        "document.close",
        f"checkout.close:{CHECKOUT_ID}",
    ]
    assert coordinator.ready_checkout_ids() == (CHECKOUT_ID,)


def test_fix03_closed_ack_allows_mutable_drift_but_requires_immutable_identity() -> None:
    opened = _descriptor()
    closed = deepcopy(_descriptor(state="closed"))
    closed["dirty"] = True
    closed["current_model_sha256"] = "a" * 64
    closed["current_size_bytes"] = SIZE + 11
    closed["source_head"]["generation"] = 2
    closed["source_head"]["revision_id"] = "revision_" + "b" * 32
    closed["source_head"]["manifest_sha256"] = "c" * 64

    assert (
        _validate_closed_descriptor(
            closed,
            checkout_id=CHECKOUT_ID,
            source=_source(),
            open_key=OPEN_KEY,
            descriptor=opened,
        )
        == closed
    )

    wrong_checkout = deepcopy(closed)
    wrong_checkout["checkout_id"] = "checkout_" + "d" * 32
    wrong_open_key = deepcopy(closed)
    wrong_open_key["open_key"] = "checkout_open_" + "e" * 32
    wrong_source = deepcopy(closed)
    wrong_source["source"]["project_id"] = "project_" + "f" * 32
    wrong_source["source_head"]["project_id"] = "project_" + "f" * 32
    wrong_initial = deepcopy(closed)
    wrong_initial["initial_model_sha256"] = "0" * 64

    for malformed in (
        wrong_checkout,
        wrong_open_key,
        wrong_source,
        wrong_initial,
    ):
        with pytest.raises(PreviewError):
            _validate_closed_descriptor(
                malformed,
                checkout_id=CHECKOUT_ID,
                source=_source(),
                open_key=OPEN_KEY,
                descriptor=opened,
            )


def test_fix04_registry_new_name_alias_to_user_object_is_sticky_and_never_closed(
    tmp_path: Path,
) -> None:
    class _AliasingHost(_Host):
        def __init__(self) -> None:
            super().__init__()
            self.user_document = _Document("UserDocument")
            self.documents[self.user_document.Name] = self.user_document
            self.closed_names: list[str] = []

        def openDocument(self, local_path: str) -> _Document:
            self.events.append("document.open")
            self.opened_paths.append(local_path)
            self.documents["NewPreviewAlias"] = self.user_document
            return self.user_document

        def closeDocument(self, name: str) -> None:
            self.closed_names.append(name)
            super().closeDocument(name)

    path = (tmp_path / CHECKOUT_ID / "model.FCStd").resolve()
    host = _AliasingHost()
    coordinator = PreviewCoordinator(host)

    with pytest.raises(PreviewError) as raised:
        coordinator.open(_acquired(path))

    assert raised.value.recovery_required is True
    assert host.documents == {
        "UserDocument": host.user_document,
        "NewPreviewAlias": host.user_document,
    }
    assert host.closed_names == []
    assert host.events == ["document.open"]
    assert coordinator.ready_checkout_ids() == ()


def test_fix04_partial_cycle_replacement_stays_poisoned_until_full_exact_retirement(
    tmp_path: Path,
) -> None:
    host = _Host()
    coordinator = PreviewCoordinator(host)

    def open_binding(kind: str, digit: str) -> tuple[str, PreviewBinding]:
        source = _source(kind)
        checkout_id = "checkout_" + digit * 32
        path = (tmp_path / checkout_id / "model.FCStd").resolve()
        descriptor = (
            _base_head_descriptor(checkout_id=checkout_id)
            if kind == "head"
            else _descriptor(
                checkout_id=checkout_id,
                source=source,
            )
        )
        return checkout_id, coordinator.open(
            _acquired(
                path,
                source=source,
                descriptor=descriptor,
                claim=_claim(path, checkout_id=checkout_id),
            )
        )

    def retire(checkout_id: str, binding: PreviewBinding) -> None:
        coordinator.discard_document(checkout_id)
        source = dict(binding.source)
        descriptor = (
            _base_head_descriptor(checkout_id=checkout_id, state="closed")
            if source["kind"] == "head"
            else _descriptor(
                checkout_id=checkout_id,
                source=source,
                state="closed",
            )
        )
        coordinator.mark_checkout_closed(
            checkout_id,
            descriptor,
        )

    head_id, head = open_binding("head", "4")
    draft_id, draft = open_binding("draft", "d")
    cycle = coordinator._cycle
    cycle_id = coordinator._active_cycle_id()
    assert cycle is not None
    assert type(cycle_id) is int
    coordinator.poison_binding(head_id)
    retire(head_id, head)
    replacement_id, replacement = open_binding("head", "e")

    assert coordinator.review_eligible(replacement, dict(replacement.descriptor)) is True
    assert coordinator.review_eligible(draft, dict(draft.descriptor)) is True
    assert coordinator.aggregate_review_eligible() is False

    retire(draft_id, draft)
    retire(replacement_id, replacement)
    assert coordinator._retired_cycle_ready(cycle_id) is True

    successor_id = "checkout_" + "a" * 32
    successor_path = (tmp_path / successor_id / "model.FCStd").resolve()
    with pytest.raises(PreviewError):
        coordinator.open(
            _acquired(
                successor_path,
                descriptor=_base_head_descriptor(checkout_id=successor_id),
                claim=_claim(successor_path, checkout_id=successor_id),
            )
        )

    assert coordinator._cycle is cycle
    assert coordinator._active_cycle_id() == cycle_id
    assert cycle.poisoned is True
    assert tuple(coordinator._owned) == (
        head_id,
        draft_id,
        replacement_id,
        successor_id,
    )
    successor = coordinator._owned[successor_id]
    assert successor.binding is None
    assert successor.document is None
    assert successor.document_closed is True
    assert successor.checkout_closed is False
    assert coordinator.ready_checkout_ids() == (successor_id,)
    assert coordinator.aggregate_review_eligible() is False
