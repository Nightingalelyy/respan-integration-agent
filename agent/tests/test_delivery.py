from __future__ import annotations

import json
import os
import stat
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from respan_integration_agent.delivery import (
    BACKEND_TRACE_EVIDENCE_SCHEMA,
    DELIVERY_JOURNAL_SCHEMA,
    DELIVERY_MANIFEST_SCHEMA,
    PREPARED_DELIVERY_SCHEMA,
    REQUIRED_VERIFICATION_CHECKS,
    VERIFICATION_RECEIPT_SCHEMA,
    DeliveryConfigurationError,
    DeliveryError,
    DeliveryIntegrityError,
    DeliveryJournal,
    DeliveryJournalError,
    DeliveryJournalRecord,
    DeliveryManifest,
    DeliveryPhase,
    DeliveryRecoveryRequired,
    PreparedDelivery,
    RemoteDisposition,
    RepositoryTarget,
    VerificationReceipt,
)


REPOSITORY = "respan/v0-delivery-fixture"
REPO_URL = f"https://github.com/{REPOSITORY}"
BASE_SHA = "1" * 40
COMMIT_SHA = "2" * 40
REMOTE_SHA = "3" * 40
CONFIG_FINGERPRINT = "a" * 64
GATEWAY_REPORT_FINGERPRINT = "b" * 64
AGENT_TRACE_ID = "1" * 32
TARGET_TRACE_ID = "2" * 32
PATCH = b"""diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print("old")
+print("new")
"""
VERIFIED_AT = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)


def trace_url(trace_id: str) -> str:
    return f"https://platform.respan.ai/platform/traces?trace_unique_id={trace_id}"


def make_target(**overrides: object) -> RepositoryTarget:
    values: dict[str, object] = {
        "repo_url": REPO_URL,
        "base_ref": "main",
        "allowed_slug": REPOSITORY,
    }
    values.update(overrides)
    return RepositoryTarget(**values)


def make_prepared(**overrides: object) -> PreparedDelivery:
    values: dict[str, object] = {
        "target": make_target(),
        "base_sha": BASE_SHA,
        "patch": PATCH,
        "changed_paths": ("app.py",),
        "product": "tracing",
        "config_fingerprint": CONFIG_FINGERPRINT,
        "agent_run_id": "respan-v0b-agent-run",
        "agent_trace_id": AGENT_TRACE_ID,
        "agent_trace_url": trace_url(AGENT_TRACE_ID),
    }
    values.update(overrides)
    return PreparedDelivery(**values)


def make_receipt(
    prepared: PreparedDelivery | None = None, **overrides: object
) -> VerificationReceipt:
    selected = prepared or make_prepared()
    values: dict[str, object] = {
        "gateway_report_fingerprint": GATEWAY_REPORT_FINGERPRINT,
        "target_run_id": "respan-v0b-target-run",
        "target_trace_id": TARGET_TRACE_ID,
        "target_trace_url": trace_url(TARGET_TRACE_ID),
        "backend_verified_at": VERIFIED_AT,
    }
    values.update(overrides)
    return VerificationReceipt.for_prepared(selected, **values)


def make_manifest(
    prepared: PreparedDelivery | None = None, **overrides: object
) -> DeliveryManifest:
    selected = prepared or make_prepared()
    values: dict[str, object] = {
        "target": selected.target,
        "base_sha": selected.base_sha,
        "branch": selected.branch,
        "commit_sha": COMMIT_SHA,
        "delivery_fingerprint": selected.delivery_fingerprint,
        "pr_number": 17,
        "pr_url": f"{REPO_URL}/pull/17",
        "branch_disposition": RemoteDisposition.created,
        "pr_disposition": RemoteDisposition.created,
    }
    values.update(overrides)
    return DeliveryManifest(**values)


def make_state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "delivery-state"
    state_dir.mkdir(mode=0o700)
    os.chmod(state_dir, 0o700)
    return state_dir


def make_record(
    prepared: PreparedDelivery,
    phase: DeliveryPhase,
    **overrides: object,
) -> DeliveryJournalRecord:
    values: dict[str, object] = {
        "phase": phase,
        "repository": prepared.target.slug,
        "base_ref": prepared.target.base_ref,
        "base_sha": prepared.base_sha,
        "branch": prepared.branch,
        "delivery_fingerprint": prepared.delivery_fingerprint,
    }
    if phase is not DeliveryPhase.prepared:
        values["commit_sha"] = COMMIT_SHA
    if phase in {
        DeliveryPhase.branch_pushed,
        DeliveryPhase.pr_observed,
        DeliveryPhase.pr_created,
        DeliveryPhase.completed,
    }:
        values["remote_sha"] = REMOTE_SHA
    if phase in {DeliveryPhase.pr_created, DeliveryPhase.completed}:
        values["pr_number"] = 17
        values["pr_url"] = f"{REPO_URL}/pull/17"
    values.update(overrides)
    return DeliveryJournalRecord(**values)


def test_repository_target_canonicalizes_only_the_exact_allowed_slug() -> None:
    target = make_target(repo_url=f"{REPO_URL}.git")

    assert target.owner == "respan"
    assert target.repo == "v0-delivery-fixture"
    assert target.slug == REPOSITORY
    assert target.canonical_url == REPO_URL
    assert target.base_ref == "main"

    with pytest.raises(FrozenInstanceError):
        target.base_ref = "other"


@pytest.mark.parametrize(
    "repo_url",
    [
        "http://github.com/respan/v0-delivery-fixture",
        "HTTPS://github.com/respan/v0-delivery-fixture",
        "https://GitHub.com/respan/v0-delivery-fixture",
        "https://github.com:443/respan/v0-delivery-fixture",
        "https://user@github.com/respan/v0-delivery-fixture",
        "https://github.com/respan/v0-delivery-fixture/",
        "https://github.com/respan/v0-delivery-fixture/extra",
        "https://github.com/respan%2fv0-delivery-fixture",
        "https://github.com/respan/v0-delivery-fixture?token=hidden",
        "https://github.com/respan/v0-delivery-fixture#fragment",
        "git@github.com:respan/v0-delivery-fixture.git",
        "/private/tmp/v0-delivery-fixture",
    ],
)
def test_repository_target_rejects_noncanonical_or_nonpublic_urls(
    repo_url: str,
) -> None:
    with pytest.raises(ValueError):
        make_target(repo_url=repo_url)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_slug", "other/v0-delivery-fixture"),
        ("allowed_slug", "Respan/v0-delivery-fixture"),
        ("base_ref", "refs/heads/main"),
        ("base_ref", "../main"),
        ("base_ref", "main.lock"),
        ("base_ref", "feature//unsafe"),
        ("base_ref", "feature@{one}"),
        ("base_ref", "feature name"),
        ("base_ref", "github_pat_" + "a" * 24),
    ],
)
def test_repository_target_rejects_mismatched_slug_and_unsafe_refs(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError):
        make_target(**{field: value})


def test_prepared_delivery_computes_stable_content_identity_and_hides_patch() -> None:
    sensitive_source = b"ordinary-sensitive-source-value"
    prepared = make_prepared(patch=PATCH + sensitive_source + b"\n")
    repeated = make_prepared(patch=PATCH + sensitive_source + b"\n")

    assert prepared.patch_sha256 == repeated.patch_sha256
    assert prepared.delivery_fingerprint == repeated.delivery_fingerprint
    assert prepared.branch == repeated.branch
    assert prepared.branch == (
        f"respan/onboard-tracing-{BASE_SHA[:8]}-{prepared.patch_sha256[:12]}"
    )
    assert sensitive_source.decode() not in repr(prepared)
    safe = prepared.safe_identity_dict()
    encoded = json.dumps(safe, sort_keys=True)
    assert safe["schema_version"] == PREPARED_DELIVERY_SCHEMA
    assert "patch" not in safe
    assert sensitive_source.decode() not in encoded
    assert safe["changed_paths"] == ["app.py"]

    with pytest.raises(FrozenInstanceError):
        prepared.branch = "other"


def test_prepared_fingerprint_binds_config_and_paths_but_branch_binds_content() -> None:
    baseline = make_prepared()
    other_config = make_prepared(config_fingerprint="c" * 64)
    other_patch = make_prepared(patch=PATCH.replace(b"new", b"newer"))

    assert baseline.delivery_fingerprint != other_config.delivery_fingerprint
    assert baseline.branch == other_config.branch
    assert baseline.delivery_fingerprint != other_patch.delivery_fingerprint
    assert baseline.branch != other_patch.branch


def test_prepared_delivery_allows_reviewable_dotfiles_but_not_git_or_workflows() -> (
    None
):
    prepared = make_prepared(changed_paths=(".gitignore", "app.py"))
    assert prepared.changed_paths == (".gitignore", "app.py")

    for unsafe_path in (".git/config", ".github/workflows/publish.yml"):
        with pytest.raises(ValueError):
            make_prepared(changed_paths=(unsafe_path,))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_sha", "A" * 40),
        ("base_sha", "0" * 40),
        ("patch", "not-bytes"),
        ("patch", b""),
        ("patch", b"not a git diff\n"),
        ("changed_paths", ["app.py"]),
        ("changed_paths", ("z.py", "a.py")),
        ("changed_paths", ("app.py", "app.py")),
        ("changed_paths", ("../app.py",)),
        ("changed_paths", (".github/workflows/publish.yml",)),
        ("product", "evals"),
        ("config_fingerprint", "a" * 63),
        ("agent_run_id", "bad run"),
        ("agent_trace_id", "1" * 31),
        ("agent_trace_url", "https://attacker.invalid/trace"),
    ],
)
def test_prepared_delivery_rejects_noncanonical_input(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        make_prepared(**{field: value})


def test_prepared_delivery_rejects_common_credential_shapes_in_patch() -> None:
    token = b"github_pat_11AA00_exampletokenvalue1234567890"
    with pytest.raises(ValueError, match="credential-like"):
        make_prepared(patch=PATCH + token + b"\n")


def test_verification_receipt_is_closed_bound_evidence() -> None:
    prepared = make_prepared()
    receipt = make_receipt(prepared)

    receipt.validate_for(prepared)
    evidence = receipt.to_dict()
    assert evidence["schema_version"] == VERIFICATION_RECEIPT_SCHEMA
    assert evidence["delivery_fingerprint"] == prepared.delivery_fingerprint
    assert evidence["patch_sha256"] == prepared.patch_sha256
    assert evidence["passed_checks"] == list(REQUIRED_VERIFICATION_CHECKS)
    assert evidence["backend_evidence_schema"] == BACKEND_TRACE_EVIDENCE_SCHEMA
    assert evidence["backend_verified_at"].endswith("+00:00")

    with pytest.raises(FrozenInstanceError):
        receipt.delivery_fingerprint = "c" * 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gateway_report_fingerprint", "b" * 63),
        ("target_run_id", "respan-v0b-agent-run"),
        ("target_trace_id", AGENT_TRACE_ID),
        ("target_trace_url", "https://platform.respan.ai/platform/traces"),
        ("backend_verified_at", datetime(2026, 8, 23, 12, 0)),
        (
            "backend_verified_at",
            datetime(2026, 8, 23, 12, 0, tzinfo=timezone(timedelta(hours=1))),
        ),
        ("backend_evidence_schema", "future-schema/v2"),
        ("passed_checks", REQUIRED_VERIFICATION_CHECKS[:-1]),
        ("passed_checks", tuple(reversed(REQUIRED_VERIFICATION_CHECKS))),
        ("passed_checks", list(REQUIRED_VERIFICATION_CHECKS)),
    ],
)
def test_verification_receipt_rejects_incomplete_or_malformed_evidence(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        make_receipt(**{field: value})


def test_receipt_rejects_another_prepared_delivery_without_disclosing_identity() -> (
    None
):
    receipt = make_receipt()
    other = make_prepared(patch=PATCH.replace(b"new", b"different"))

    with pytest.raises(DeliveryIntegrityError) as caught:
        receipt.validate_for(other)

    rendered = repr(caught.value)
    assert caught.value.as_dict() == {
        "code": "G_INTEGRITY",
        "stage": "receipt_validation",
        "transient": False,
    }
    assert other.delivery_fingerprint not in rendered


def test_delivery_manifest_is_safe_exact_completed_evidence() -> None:
    prepared = make_prepared()
    receipt = make_receipt(prepared)
    manifest = make_manifest(prepared)

    manifest.validate_for(prepared, receipt)
    evidence = manifest.to_dict()
    assert evidence == {
        "schema_version": DELIVERY_MANIFEST_SCHEMA,
        "repository": REPOSITORY,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "branch": prepared.branch,
        "commit_sha": COMMIT_SHA,
        "delivery_fingerprint": prepared.delivery_fingerprint,
        "branch_disposition": "created",
        "pr_disposition": "created",
        "pr_number": 17,
        "pr_url": f"{REPO_URL}/pull/17",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_sha", "f" * 39),
        ("branch", "../unsafe"),
        ("commit_sha", "2" * 39),
        ("delivery_fingerprint", "a" * 63),
        ("pr_number", True),
        ("pr_number", 0),
        ("pr_url", "https://github.com/other/repo/pull/17"),
        ("branch_disposition", "created"),
        ("pr_disposition", "reused"),
    ],
)
def test_delivery_manifest_rejects_unsafe_or_coerced_fields(
    field: str, value: object
) -> None:
    with pytest.raises(ValueError):
        make_manifest(**{field: value})


def test_manifest_validation_binds_target_base_branch_and_receipt() -> None:
    prepared = make_prepared()
    receipt = make_receipt(prepared)
    wrong = make_manifest(prepared, branch="respan/another-safe-branch")

    with pytest.raises(DeliveryIntegrityError):
        wrong.validate_for(prepared, receipt)


def test_delivery_errors_expose_only_stable_safe_fields() -> None:
    prepared = make_prepared()
    token = "github_pat_11AA00_exampletokenvalue1234567890"
    error = DeliveryRecoveryRequired(
        "pr_create",
        status_code=503,
        attempts=3,
        recovery={
            "repository": REPOSITORY,
            "base_ref": "main",
            "base_sha": BASE_SHA,
            "branch": prepared.branch,
            "commit_sha": COMMIT_SHA,
            "remote_sha": REMOTE_SHA,
            "delivery_fingerprint": prepared.delivery_fingerprint,
            "phase": "branch_pushed",
        },
    )

    assert error.as_dict() == {
        "code": "G_RECOVERY_REQUIRED",
        "stage": "pr_create",
        "transient": True,
        "status_code": 503,
        "attempts": 3,
        "recovery": {
            "base_ref": "main",
            "base_sha": BASE_SHA,
            "branch": prepared.branch,
            "commit_sha": COMMIT_SHA,
            "delivery_fingerprint": prepared.delivery_fingerprint,
            "phase": "branch_pushed",
            "remote_sha": REMOTE_SHA,
            "repository": REPOSITORY,
        },
    }
    serialized = json.dumps(error.as_dict(), sort_keys=True) + repr(error)
    assert token not in serialized
    assert "response_body" not in serialized

    with pytest.raises(ValueError):
        DeliveryError(token)
    with pytest.raises(ValueError):
        DeliveryConfigurationError("config", recovery={"body": token})


def test_journal_requires_an_explicit_owned_mode_0700_directory(
    tmp_path: Path,
) -> None:
    prepared = make_prepared()
    missing = tmp_path / "missing"
    with pytest.raises(DeliveryJournalError):
        DeliveryJournal(missing, prepared.delivery_fingerprint)

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    os.chmod(unsafe, 0o755)
    with pytest.raises(DeliveryJournalError):
        DeliveryJournal(unsafe, prepared.delivery_fingerprint)

    safe = make_state_dir(tmp_path)
    symlink = tmp_path / "state-link"
    symlink.symlink_to(safe, target_is_directory=True)
    with pytest.raises(DeliveryJournalError):
        DeliveryJournal(symlink, prepared.delivery_fingerprint)

    with pytest.raises(DeliveryJournalError):
        DeliveryJournal(Path("relative-state"), prepared.delivery_fingerprint)


def test_journal_atomically_round_trips_source_free_mode_0600_records(
    tmp_path: Path,
) -> None:
    prepared = make_prepared()
    state_dir = make_state_dir(tmp_path)
    journal = DeliveryJournal(state_dir, prepared.delivery_fingerprint)
    assert journal.load() is None

    initial = DeliveryJournalRecord.for_prepared(prepared)
    journal.record(initial)
    assert journal.load() == initial
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600

    committed = make_record(prepared, DeliveryPhase.committed)
    journal.record(committed)
    pushed = make_record(prepared, DeliveryPhase.branch_pushed)
    journal.record(pushed)
    completed = make_record(prepared, DeliveryPhase.completed)
    journal.record(completed)
    assert journal.load() == completed

    raw = journal.path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["schema_version"] == DELIVERY_JOURNAL_SCHEMA
    assert payload["phase"] == "completed"
    assert payload["pr_number"] == 17
    assert not {"patch", "token", "body", "request", "response"} & set(payload)
    assert prepared.patch.decode("utf-8") not in raw
    assert not list(state_dir.glob("*.tmp"))
    lock_files = list(state_dir.glob(".*.lock"))
    assert len(lock_files) == 1
    assert stat.S_IMODE(lock_files[0].stat().st_mode) == 0o600


def test_journal_first_record_and_transitions_are_monotonic_and_immutable(
    tmp_path: Path,
) -> None:
    prepared = make_prepared()
    journal = DeliveryJournal(make_state_dir(tmp_path), prepared.delivery_fingerprint)

    with pytest.raises(DeliveryJournalError):
        journal.record(make_record(prepared, DeliveryPhase.committed))

    journal.record(DeliveryJournalRecord.for_prepared(prepared))
    journal.record(make_record(prepared, DeliveryPhase.committed))

    with pytest.raises(DeliveryJournalError):
        journal.record(DeliveryJournalRecord.for_prepared(prepared))
    with pytest.raises(DeliveryJournalError):
        journal.record(
            make_record(
                prepared,
                DeliveryPhase.branch_pushed,
                repository="respan/another-fixture",
            )
        )
    with pytest.raises(DeliveryJournalError):
        journal.record(
            make_record(
                prepared,
                DeliveryPhase.branch_pushed,
                commit_sha="4" * 40,
            )
        )

    assert journal.load() == make_record(prepared, DeliveryPhase.committed)


def test_journal_preserves_a_later_observed_divergent_pr_head(tmp_path: Path) -> None:
    prepared = make_prepared()
    journal = DeliveryJournal(make_state_dir(tmp_path), prepared.delivery_fingerprint)
    journal.record(DeliveryJournalRecord.for_prepared(prepared))
    journal.record(make_record(prepared, DeliveryPhase.committed))
    journal.record(
        make_record(
            prepared,
            DeliveryPhase.branch_pushed,
            remote_sha=COMMIT_SHA,
        )
    )
    collision = make_record(
        prepared,
        DeliveryPhase.pr_observed,
        remote_sha=REMOTE_SHA,
        pr_number=17,
        pr_url=f"{REPO_URL}/pull/17",
    )
    journal.record(collision)

    assert journal.load() == collision
    with pytest.raises(DeliveryJournalError):
        journal.record(
            make_record(
                prepared,
                DeliveryPhase.completed,
                remote_sha=COMMIT_SHA,
            )
        )


@pytest.mark.parametrize(
    "phase",
    [
        DeliveryPhase.committed,
        DeliveryPhase.branch_pushed,
        DeliveryPhase.pr_created,
        DeliveryPhase.completed,
    ],
)
def test_journal_record_requires_identifiers_for_each_phase(
    phase: DeliveryPhase,
) -> None:
    prepared = make_prepared()
    values: dict[str, object] = {
        "phase": phase,
        "repository": REPOSITORY,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "branch": prepared.branch,
        "delivery_fingerprint": prepared.delivery_fingerprint,
    }
    with pytest.raises(ValueError):
        DeliveryJournalRecord(**values)


def test_journal_load_rejects_corruption_extra_fields_and_unsafe_permissions(
    tmp_path: Path,
) -> None:
    prepared = make_prepared()
    state_dir = make_state_dir(tmp_path)
    journal = DeliveryJournal(state_dir, prepared.delivery_fingerprint)
    token = "github_pat_11AA00_exampletokenvalue1234567890"

    journal.path.write_text(
        json.dumps(
            {
                **DeliveryJournalRecord.for_prepared(prepared).to_dict(),
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(journal.path, 0o600)
    with pytest.raises(DeliveryJournalError) as caught:
        journal.load()
    assert token not in repr(caught.value)
    assert caught.value.__cause__ is None

    journal.path.write_text("{not-json:" + token, encoding="utf-8")
    os.chmod(journal.path, 0o600)
    with pytest.raises(DeliveryJournalError) as caught:
        journal.load()
    assert token not in repr(caught.value)
    assert caught.value.__cause__ is None

    journal.path.write_text(
        json.dumps(DeliveryJournalRecord.for_prepared(prepared).to_dict()),
        encoding="utf-8",
    )
    os.chmod(journal.path, 0o644)
    with pytest.raises(DeliveryJournalError):
        journal.load()


def test_journal_rejects_symlink_and_oversized_snapshot(tmp_path: Path) -> None:
    prepared = make_prepared()
    state_dir = make_state_dir(tmp_path)
    journal = DeliveryJournal(state_dir, prepared.delivery_fingerprint)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    os.chmod(outside, 0o600)
    journal.path.symlink_to(outside)
    with pytest.raises(DeliveryJournalError):
        journal.load()

    journal.path.unlink()
    journal.path.write_bytes(b"x" * (64 * 1024 + 1))
    os.chmod(journal.path, 0o600)
    with pytest.raises(DeliveryJournalError):
        journal.load()


def test_journal_rejects_wrong_record_type_and_fingerprint(tmp_path: Path) -> None:
    prepared = make_prepared()
    journal = DeliveryJournal(make_state_dir(tmp_path), prepared.delivery_fingerprint)
    with pytest.raises(DeliveryJournalError):
        journal.record({})

    other = make_prepared(patch=PATCH.replace(b"new", b"other"))
    with pytest.raises(DeliveryJournalError):
        journal.record(DeliveryJournalRecord.for_prepared(other))


def test_journal_replace_failure_preserves_last_snapshot_and_redacts_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import respan_integration_agent.delivery as delivery_module

    prepared = make_prepared()
    state_dir = make_state_dir(tmp_path)
    journal = DeliveryJournal(state_dir, prepared.delivery_fingerprint)
    initial = DeliveryJournalRecord.for_prepared(prepared)
    journal.record(initial)
    secret = "github_pat_11AA00_exampletokenvalue1234567890"

    def failed_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError(secret)

    monkeypatch.setattr(delivery_module.os, "replace", failed_replace)
    with pytest.raises(DeliveryJournalError) as caught:
        journal.record(make_record(prepared, DeliveryPhase.committed))

    assert secret not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert journal.load() == initial
    assert not list(state_dir.glob("*.tmp"))
