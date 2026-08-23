#!/usr/bin/env python3
"""Run protected v0b delivery after the exact v0a acceptance gates.

This entrypoint is intentionally separate from ``run_v0_smoke.py``. It accepts one
explicitly allowlisted disposable public GitHub fixture and cannot create any remote
object until the fresh target and backend trace-content checks have passed.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "agent" / "src"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_SRC))

from respan_integration_agent.delivery import (  # noqa: E402
    DeliveryConfigurationError,
    DeliveryError,
    DeliveryJournal,
    PreparedDelivery,
    RepositoryTarget,
    VerificationReceipt,
)
from respan_integration_agent.gateway_preflight import PreflightError  # noqa: E402
from respan_integration_agent.github import (  # noqa: E402
    GitHubReadiness,
    GitHubRestClient,
    open_pr,
)
from scripts.run_v0_smoke import (  # noqa: E402
    ContextualBackendFailure,
    _backend_exit_code,
    _preflight_exit_code,
    run_verified_smoke,
)


EVIDENCE_SCHEMA_VERSION = "respan-v0b-smoke-evidence/v1"
_UPSTREAM_DENYLIST = frozenset({"nightingalelyy/respan-integration-agent"})


@dataclass(frozen=True, slots=True)
class V0bSettings:
    target: RepositoryTarget
    state_dir: Path
    token: str = field(repr=False)


def _failure_evidence(error: DeliveryError) -> dict[str, object]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "verdict": "V0B_DELIVERY_FAILED",
        "delivery_error": error.as_dict(),
    }


def _runtime_failure_evidence() -> dict[str, object]:
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "verdict": "V0B_RUNTIME_FAILED",
        "runtime_error": {"code": "V0B_UNEXPECTED"},
    }


def _delivery_exit_code(error: DeliveryError) -> int:
    if error.code == "G_CONFIG" or error.stage in {
        "authentication",
        "github_authentication",
    }:
        return 2
    if (
        error.transient
        or error.code == "G_RECOVERY_REQUIRED"
        or error.stage
        in {
            "github_rate_limit",
            "github_transport",
            "github_recovery",
            "push_ambiguous",
        }
    ):
        return 3
    return 4


def _read_settings() -> V0bSettings:
    # Remove every common GitHub credential name before any descendant is started.
    token = os.environ.pop("RESPAN_GITHUB_TOKEN", "")
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("GH_TOKEN", None)
    repo_url = os.environ.get("RESPAN_V0B_REPOSITORY_URL", "")
    allowed_slug = os.environ.get("RESPAN_V0B_ALLOWED_REPOSITORY", "")
    base_ref = os.environ.get("RESPAN_V0B_BASE_REF", "main")
    raw_state_dir = os.environ.get("RESPAN_V0B_STATE_DIR", "")
    if not token or not repo_url or not allowed_slug or not raw_state_dir:
        raise DeliveryConfigurationError("v0b_configuration")
    try:
        target = RepositoryTarget(
            repo_url=repo_url,
            base_ref=base_ref,
            allowed_slug=allowed_slug,
        )
    except ValueError:
        raise DeliveryConfigurationError("v0b_configuration") from None
    if target.slug.casefold() in _UPSTREAM_DENYLIST:
        raise DeliveryConfigurationError("upstream_forbidden")
    state_dir = Path(raw_state_dir)
    # Validate the explicit durable directory before the read-only network check or
    # paid agent run. The throwaway fingerprint creates no journal file.
    try:
        DeliveryJournal(state_dir, "a" * 64)
    except DeliveryError:
        raise DeliveryConfigurationError("v0b_state_directory") from None
    return V0bSettings(target=target, state_dir=state_dir, token=token)


def _readiness_dict(readiness: GitHubReadiness) -> dict[str, object]:
    return {
        "authenticated_login": readiness.authenticated_login,
        "authenticated_user_id": readiness.authenticated_user_id,
        "repository": readiness.repository,
        "base_ref": readiness.base_ref,
        "base_sha": readiness.base_sha,
        "publicly_readable": readiness.publicly_readable,
        "write_permission_claimed": False,
    }


def run_delivery_smoke() -> dict[str, object]:
    """Return sanitized success evidence or raise a typed, redacted failure."""

    settings = _read_settings()
    client = GitHubRestClient(settings.token, settings.target)
    readiness = client.preflight()
    if (
        readiness.repository != settings.target.slug
        or readiness.base_ref != settings.target.base_ref
        or readiness.publicly_readable is not True
    ):
        raise DeliveryConfigurationError("github_readiness")

    accepted = run_verified_smoke(
        repo_url=settings.target.canonical_url,
        base_branch=settings.target.base_ref,
        expected_base_commit=readiness.base_sha,
        delivery_target=settings.target,
    )
    prepared = accepted.prepared_delivery
    receipt = accepted.verification_receipt
    if not isinstance(prepared, PreparedDelivery) or not isinstance(
        receipt, VerificationReceipt
    ):
        raise DeliveryConfigurationError("acceptance_contract")
    if prepared.base_sha != readiness.base_sha or prepared.target != settings.target:
        raise DeliveryConfigurationError("acceptance_identity")
    receipt.validate_for(prepared)

    journal = DeliveryJournal(settings.state_dir, prepared.delivery_fingerprint)
    manifest = open_pr(
        prepared,
        receipt,
        settings.token,
        journal,
        rest_client=client,
    )
    manifest.validate_for(prepared, receipt)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "verdict": "V0B_DELIVERED",
        "github_readiness": _readiness_dict(readiness),
        "acceptance": accepted.evidence,
        "prepared_delivery": prepared.safe_identity_dict(),
        "verification_receipt": receipt.to_dict(),
        "delivery": manifest.to_dict(),
    }


def main() -> int:
    try:
        evidence = run_delivery_smoke()
    except DeliveryError as error:
        print(json.dumps(_failure_evidence(error), sort_keys=True), file=sys.stderr)
        return _delivery_exit_code(error)
    except ContextualBackendFailure as failure:
        evidence = failure.evidence()
        evidence["schema_version"] = EVIDENCE_SCHEMA_VERSION
        print(json.dumps(evidence, sort_keys=True), file=sys.stderr)
        return _backend_exit_code(failure.error)
    except PreflightError as error:
        evidence = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "verdict": "GATEWAY_PREFLIGHT_FAILED",
            "preflight_error": error.as_dict(),
        }
        print(json.dumps(evidence, sort_keys=True), file=sys.stderr)
        return _preflight_exit_code(error)
    except Exception:
        print(json.dumps(_runtime_failure_evidence(), sort_keys=True), file=sys.stderr)
        return 4
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


def entrypoint() -> int:
    return main()


if __name__ == "__main__":
    raise SystemExit(entrypoint())
