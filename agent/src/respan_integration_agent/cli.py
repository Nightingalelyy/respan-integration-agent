"""v0 entrypoint — run a session locally, before the platform UI exists.

    export RESPAN_API_KEY=...            # the only secret needed (gateway handles the model)

    # v0a — just integrate + show the diff + emit a trace (no GitHub needed):
    respan-integration-agent run --repo https://github.com/acme/app --config config.json

    # v0b — also open a PR:
    respan-integration-agent run --repo ... --config config.json --token $GH_TOKEN

`config.json` is an OnboardingRequest (see config.py), e.g.:

    {"repo_url": "https://github.com/acme/app", "product": "tracing", "tracing": {"mode": "auto"}}
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from pydantic import ValidationError

from .config import OnboardingRequest
from .gateway_preflight import (
    PreflightDeadlineError,
    PreflightError,
    PreflightRateLimitError,
    PreflightTransportError,
)
from .runner import run_session


PREFLIGHT_ERROR_SCHEMA_VERSION = "respan-integration-agent-preflight-error/v1"
CONFIG_ERROR_SCHEMA_VERSION = "respan-integration-agent-config-error/v1"
RUNTIME_ERROR_SCHEMA_VERSION = "respan-integration-agent-runtime-error/v1"


def _config_failure_evidence() -> dict[str, object]:
    """Reject hostile config without reflecting paths, values, or validation input."""

    return {
        "schema_version": CONFIG_ERROR_SCHEMA_VERSION,
        "verdict": "CONFIG_FAILED",
        "config_error": {"code": "C_CONFIG_INVALID"},
    }


def _runtime_failure_evidence() -> dict[str, object]:
    """Return a stable fallback for unexpected failures without exception text."""

    return {
        "schema_version": RUNTIME_ERROR_SCHEMA_VERSION,
        "verdict": "RUNTIME_FAILED",
        "runtime_error": {"code": "R_UNEXPECTED"},
    }


def _preflight_failure_evidence(error: PreflightError) -> dict[str, object]:
    """Serialize only the stable fields exposed by a safe preflight error."""

    return {
        "schema_version": PREFLIGHT_ERROR_SCHEMA_VERSION,
        "verdict": "PREFLIGHT_FAILED",
        "preflight_error": error.as_dict(),
    }


def _preflight_exit_code(error: PreflightError) -> int:
    if isinstance(
        error,
        (PreflightDeadlineError, PreflightRateLimitError, PreflightTransportError),
    ):
        return 3
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="respan-integration-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="onboard a repo (v0a: diff+trace, v0b: +PR)")
    run.add_argument("--repo", help="repo URL (overrides config.repo_url)")
    run.add_argument(
        "--config", required=True, help="path to an OnboardingRequest JSON"
    )
    run.add_argument(
        "--token", help="GitHub token (PR scope) — omit for v0a (diff only)"
    )

    args = parser.parse_args(argv)

    respan_api_key = os.environ.get("RESPAN_API_KEY")
    if not respan_api_key:
        print(
            "error: set RESPAN_API_KEY (the gateway handles the model — no Anthropic key needed)",
            file=sys.stderr,
        )
        return 2

    try:
        with open(args.config, encoding="utf-8") as config_file:
            data = json.load(config_file)
        if not isinstance(data, dict):
            raise ValueError("config root must be an object")
        if args.repo:
            data["repo_url"] = args.repo
        req = OnboardingRequest.model_validate(data)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        print(json.dumps(_config_failure_evidence(), sort_keys=True), file=sys.stderr)
        return 2

    try:
        result = run_session(
            req, respan_api_key=respan_api_key, github_token=args.token
        )
    except PreflightError as exc:
        print(json.dumps(_preflight_failure_evidence(exc), sort_keys=True), file=sys.stderr)
        return _preflight_exit_code(exc)
    except Exception:
        print(json.dumps(_runtime_failure_evidence(), sort_keys=True), file=sys.stderr)
        return 1

    print(
        f"\nchanged {len(result.changed_files)} file(s): {', '.join(result.changed_files)}"
    )
    print(f"trace:  {result.trace_url}")
    print(f"trace id: {result.trace_id}")
    print(f"run id:   {result.run_id}")
    print(
        f"preflight: pass attempts={result.preflight.attempts} "
        f"finished_at={result.preflight.finished_at.isoformat()}"
    )
    print(
        f"agent:    session={result.agent_session_id} turns={result.num_turns} "
        f"duration_ms={result.duration_ms} cost_usd={result.total_cost_usd}"
    )
    if result.pr:
        print(f"PR:     {result.pr.url}")
    else:
        print("\n--- diff (v0a; pass --token to open a PR) ---")
        print(result.diff)
    print(f"\n{result.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
