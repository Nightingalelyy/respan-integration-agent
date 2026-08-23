from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.run_v0b_smoke as smoke  # noqa: E402
from respan_integration_agent.delivery import (  # noqa: E402
    BACKEND_TRACE_EVIDENCE_SCHEMA,
    REQUIRED_VERIFICATION_CHECKS,
    DeliveryConfigurationError,
    DeliveryError,
    DeliveryIntegrityError,
    DeliveryJournal,
    DeliveryManifest,
    DeliveryTransportError,
    PreparedDelivery,
    RemoteDisposition,
    RepositoryTarget,
    VerificationReceipt,
)
from respan_integration_agent.github import GitHubReadiness  # noqa: E402


REPOSITORY = "respan/v0-delivery-fixture"
CANONICAL_URL = f"https://github.com/{REPOSITORY}"
TOKEN = "github_pat_TEST_ONLY_v0b_smoke_secret_abcdefghijklmnopqrstuvwxyz"
BASE_SHA = "1" * 40
COMMIT_SHA = "2" * 40
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


def _trace_url(trace_id: str) -> str:
    return f"https://platform.respan.ai/platform/traces?trace_unique_id={trace_id}"


def _state_dir(tmp_path: Path) -> Path:
    state_dir = tmp_path / "v0b-state"
    state_dir.mkdir(mode=0o700)
    state_dir.chmod(0o700)
    return state_dir


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    state_dir: Path,
    *,
    repo_url: str = CANONICAL_URL,
    allowed_slug: str = REPOSITORY,
    base_ref: str = "main",
) -> None:
    monkeypatch.setenv("RESPAN_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-must-be-removed")
    monkeypatch.setenv("GH_TOKEN", "gh-token-must-be-removed")
    monkeypatch.setenv("RESPAN_V0B_REPOSITORY_URL", repo_url)
    monkeypatch.setenv("RESPAN_V0B_ALLOWED_REPOSITORY", allowed_slug)
    monkeypatch.setenv("RESPAN_V0B_BASE_REF", base_ref)
    monkeypatch.setenv("RESPAN_V0B_STATE_DIR", str(state_dir))


def _target(
    *,
    repo_url: str = CANONICAL_URL,
    allowed_slug: str = REPOSITORY,
    base_ref: str = "main",
) -> RepositoryTarget:
    return RepositoryTarget(
        repo_url=repo_url,
        base_ref=base_ref,
        allowed_slug=allowed_slug,
    )


def _prepared(
    *,
    target: RepositoryTarget | None = None,
    base_sha: str = BASE_SHA,
    patch: bytes = PATCH,
) -> PreparedDelivery:
    return PreparedDelivery(
        target=target or _target(),
        base_sha=base_sha,
        patch=patch,
        changed_paths=("app.py",),
        product="tracing",
        config_fingerprint="a" * 64,
        agent_run_id="respan-v0b-agent-smoke-test",
        agent_trace_id=AGENT_TRACE_ID,
        agent_trace_url=_trace_url(AGENT_TRACE_ID),
    )


def _receipt(prepared: PreparedDelivery) -> VerificationReceipt:
    return VerificationReceipt.for_prepared(
        prepared,
        gateway_report_fingerprint="b" * 64,
        target_run_id="respan-v0b-target-smoke-test",
        target_trace_id=TARGET_TRACE_ID,
        target_trace_url=_trace_url(TARGET_TRACE_ID),
        backend_verified_at=datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
        backend_evidence_schema=BACKEND_TRACE_EVIDENCE_SCHEMA,
        passed_checks=REQUIRED_VERIFICATION_CHECKS,
    )


def _accepted(
    prepared: PreparedDelivery | None,
    receipt: VerificationReceipt | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        prepared_delivery=prepared,
        verification_receipt=receipt,
        evidence={
            "schema_version": "respan-v0-smoke-evidence/v1",
            "verdict": "BACKEND_VERIFIED_PASS",
            "base_commit": prepared.base_sha if prepared is not None else BASE_SHA,
            "patch_sha256": (
                prepared.patch_sha256 if prepared is not None else "0" * 64
            ),
        },
    )


def _readiness(
    target: RepositoryTarget, *, base_sha: str = BASE_SHA
) -> GitHubReadiness:
    return GitHubReadiness(
        authenticated_login="respan-delivery-fixture",
        authenticated_user_id=101,
        repository=target.slug,
        base_ref=target.base_ref,
        base_sha=base_sha,
        publicly_readable=True,
    )


def _manifest(prepared: PreparedDelivery) -> DeliveryManifest:
    return DeliveryManifest(
        target=prepared.target,
        base_sha=prepared.base_sha,
        branch=prepared.branch,
        commit_sha=COMMIT_SHA,
        delivery_fingerprint=prepared.delivery_fingerprint,
        pr_number=17,
        pr_url=f"{prepared.target.canonical_url}/pull/17",
        branch_disposition=RemoteDisposition.created,
        pr_disposition=RemoteDisposition.created,
    )


def _patch_readiness_client(
    monkeypatch: pytest.MonkeyPatch,
    target: RepositoryTarget,
    events: list[str],
) -> None:
    class FakeGitHubRestClient:
        def __init__(self, token: str, actual_target: RepositoryTarget) -> None:
            assert token == TOKEN
            assert actual_target == target

        def preflight(self) -> GitHubReadiness:
            events.append("preflight")
            return _readiness(target)

    monkeypatch.setattr(smoke, "GitHubRestClient", FakeGitHubRestClient)


def test_settings_hard_deny_the_upstream_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = _state_dir(tmp_path)
    upstream = "nightingalelyy/respan-integration-agent"
    _configure(
        monkeypatch,
        state_dir,
        repo_url=f"https://github.com/{upstream}",
        allowed_slug=upstream,
    )

    with pytest.raises(DeliveryConfigurationError) as raised:
        smoke._read_settings()

    assert raised.value.stage == "upstream_forbidden"


def test_settings_hide_the_token_and_remove_all_github_credentials_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, _state_dir(tmp_path))

    settings = smoke._read_settings()

    assert settings.token == TOKEN
    assert TOKEN not in repr(settings)
    assert "token=" not in repr(settings)
    for name in ("RESPAN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        assert name not in os.environ


@pytest.mark.parametrize(
    ("invalid_kind", "expected_stage"),
    [
        ("missing-token", "v0b_configuration"),
        ("invalid-target", "v0b_configuration"),
        ("target-allowlist-mismatch", "v0b_configuration"),
        ("invalid-state-dir", "v0b_state_directory"),
    ],
)
def test_target_and_configuration_validation_precede_client_and_smoke(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invalid_kind: str,
    expected_stage: str,
) -> None:
    state_dir = _state_dir(tmp_path)
    _configure(monkeypatch, state_dir)
    if invalid_kind == "missing-token":
        monkeypatch.delenv("RESPAN_GITHUB_TOKEN")
    elif invalid_kind == "invalid-target":
        monkeypatch.setenv(
            "RESPAN_V0B_REPOSITORY_URL",
            "https://example.invalid/respan/v0-delivery-fixture",
        )
    elif invalid_kind == "target-allowlist-mismatch":
        monkeypatch.setenv(
            "RESPAN_V0B_ALLOWED_REPOSITORY",
            "respan/a-different-fixture",
        )
    else:
        state_dir.chmod(0o755)

    calls: list[str] = []

    def forbidden_client(*_args, **_kwargs):
        calls.append("client")
        raise AssertionError("GitHub client constructed before validation")

    def forbidden_smoke(*_args, **_kwargs):
        calls.append("smoke")
        raise AssertionError("smoke started before validation")

    monkeypatch.setattr(smoke, "GitHubRestClient", forbidden_client)
    monkeypatch.setattr(smoke, "run_verified_smoke", forbidden_smoke)

    with pytest.raises(DeliveryConfigurationError) as raised:
        smoke.run_delivery_smoke()

    assert raised.value.stage == expected_stage
    assert calls == []


def test_success_orders_preflight_then_verified_smoke_then_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_dir = _state_dir(tmp_path)
    _configure(monkeypatch, state_dir)
    target = _target()
    prepared = _prepared(target=target)
    receipt = _receipt(prepared)
    accepted = _accepted(prepared, receipt)
    manifest = _manifest(prepared)
    events: list[str] = []
    client_holder: dict[str, object] = {}

    class FakeGitHubRestClient:
        def __init__(self, token: str, actual_target: RepositoryTarget) -> None:
            assert token == TOKEN
            assert actual_target == target
            client_holder["client"] = self

        def preflight(self) -> GitHubReadiness:
            events.append("preflight")
            for name in ("RESPAN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
                assert name not in os.environ
            return _readiness(target)

    def fake_verified_smoke(**kwargs) -> SimpleNamespace:
        events.append("verified-smoke")
        assert kwargs == {
            "repo_url": target.canonical_url,
            "base_branch": target.base_ref,
            "expected_base_commit": BASE_SHA,
            "delivery_target": target,
        }
        for name in ("RESPAN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
            assert name not in os.environ
        return accepted

    def fake_open_pr(
        actual_prepared: PreparedDelivery,
        actual_receipt: VerificationReceipt,
        token: str,
        journal: DeliveryJournal,
        *,
        rest_client: object,
    ) -> DeliveryManifest:
        events.append("open-pr")
        assert actual_prepared is prepared
        assert actual_receipt is receipt
        assert token == TOKEN
        assert rest_client is client_holder["client"]
        assert journal.state_dir == state_dir
        assert journal.delivery_fingerprint == prepared.delivery_fingerprint
        assert journal.load() is None
        return manifest

    monkeypatch.setattr(smoke, "GitHubRestClient", FakeGitHubRestClient)
    monkeypatch.setattr(smoke, "run_verified_smoke", fake_verified_smoke)
    monkeypatch.setattr(smoke, "open_pr", fake_open_pr)

    evidence = smoke.run_delivery_smoke()

    assert events == ["preflight", "verified-smoke", "open-pr"]
    assert evidence["schema_version"] == smoke.EVIDENCE_SCHEMA_VERSION
    assert evidence["verdict"] == "V0B_DELIVERED"
    assert evidence["github_readiness"] == {
        "authenticated_login": "respan-delivery-fixture",
        "authenticated_user_id": 101,
        "repository": REPOSITORY,
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "publicly_readable": True,
        "write_permission_claimed": False,
    }
    assert evidence["prepared_delivery"] == prepared.safe_identity_dict()
    assert evidence["verification_receipt"] == receipt.to_dict()
    assert evidence["delivery"] == manifest.to_dict()
    serialized = json.dumps(evidence, sort_keys=True)
    assert TOKEN not in serialized
    assert "diff --git" not in serialized
    assert "authorization" not in serialized.casefold()
    assert "request_body" not in serialized
    assert "response_body" not in serialized


def test_smoke_failure_never_reaches_open_pr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, _state_dir(tmp_path))
    target = _target()
    events: list[str] = []
    _patch_readiness_client(monkeypatch, target, events)

    def failed_smoke(**_kwargs):
        events.append("smoke")
        raise RuntimeError("target runtime gate failed")

    def forbidden_open_pr(*_args, **_kwargs):
        events.append("open-pr")
        raise AssertionError("delivery ran after smoke failure")

    monkeypatch.setattr(smoke, "run_verified_smoke", failed_smoke)
    monkeypatch.setattr(smoke, "open_pr", forbidden_open_pr)

    with pytest.raises(RuntimeError, match="target runtime gate failed"):
        smoke.run_delivery_smoke()

    assert events == ["preflight", "smoke"]


@pytest.mark.parametrize(
    ("artifact_kind", "error_type", "expected_stage"),
    [
        ("missing", DeliveryConfigurationError, "acceptance_contract"),
        ("wrong-base", DeliveryConfigurationError, "acceptance_identity"),
        ("wrong-receipt", DeliveryIntegrityError, "receipt_validation"),
    ],
)
def test_missing_or_mismatched_acceptance_never_reaches_open_pr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artifact_kind: str,
    error_type: type[DeliveryError],
    expected_stage: str,
) -> None:
    _configure(monkeypatch, _state_dir(tmp_path))
    target = _target()
    prepared = _prepared(target=target)
    if artifact_kind == "missing":
        accepted = _accepted(None, None)
    elif artifact_kind == "wrong-base":
        wrong_base = _prepared(target=target, base_sha="3" * 40)
        accepted = _accepted(wrong_base, _receipt(wrong_base))
    else:
        other_patch = PATCH.replace(b'print("new")', b'print("other")')
        other_prepared = _prepared(target=target, patch=other_patch)
        accepted = _accepted(prepared, _receipt(other_prepared))

    events: list[str] = []
    _patch_readiness_client(monkeypatch, target, events)

    def fake_smoke(**_kwargs):
        events.append("smoke")
        return accepted

    def forbidden_open_pr(*_args, **_kwargs):
        events.append("open-pr")
        raise AssertionError("delivery ran with invalid acceptance")

    monkeypatch.setattr(smoke, "run_verified_smoke", fake_smoke)
    monkeypatch.setattr(smoke, "open_pr", forbidden_open_pr)

    with pytest.raises(error_type) as raised:
        smoke.run_delivery_smoke()

    assert raised.value.stage == expected_stage
    assert events == ["preflight", "smoke"]


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_evidence"),
    [
        (
            DeliveryTransportError("github_transport", status_code=503, attempts=3),
            3,
            {
                "schema_version": smoke.EVIDENCE_SCHEMA_VERSION,
                "verdict": "V0B_DELIVERY_FAILED",
                "delivery_error": {
                    "code": "G_TRANSPORT",
                    "stage": "github_transport",
                    "transient": True,
                    "status_code": 503,
                    "attempts": 3,
                },
            },
        ),
        (
            RuntimeError(TOKEN),
            4,
            {
                "schema_version": smoke.EVIDENCE_SCHEMA_VERSION,
                "verdict": "V0B_RUNTIME_FAILED",
                "runtime_error": {"code": "V0B_UNEXPECTED"},
            },
        ),
    ],
    ids=["typed-delivery-error", "unexpected-error"],
)
def test_cli_failure_output_is_stable_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_code: int,
    expected_evidence: dict[str, object],
) -> None:
    def fail() -> dict[str, object]:
        raise error

    monkeypatch.setattr(smoke, "run_delivery_smoke", fail)

    assert smoke.entrypoint() == expected_code
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == json.dumps(expected_evidence, sort_keys=True) + "\n"
    assert TOKEN not in captured.err
    assert "traceback" not in captured.err.casefold()
