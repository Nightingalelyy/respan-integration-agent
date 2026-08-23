"""v0a entrypoint - prepare and validate a patch without GitHub mutation.

    export RESPAN_API_KEY=...            # the only secret needed (gateway handles the model)

    # v0a — just integrate + show the diff + emit a trace (no GitHub needed):
    respan-integration-agent run --repo https://github.com/acme/app --config config.json

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
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    # argparse reflects unknown argument values. Reject the removed plaintext token
    # option and every abbreviation the old parser accepted before parsing so a
    # credential is never copied into stderr.
    def is_legacy_token_option(item: str) -> bool:
        option = item.split("=", 1)[0]
        return (
            option.startswith("--") and len(option) > 2 and "--token".startswith(option)
        )

    if any(is_legacy_token_option(item) for item in raw_argv):
        print(json.dumps(_config_failure_evidence(), sort_keys=True), file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser(prog="respan-integration-agent")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="onboard a repo (v0a: diff + trace)")
    run.add_argument("--repo", help="repo URL (overrides config.repo_url)")
    run.add_argument(
        "--config", required=True, help="path to an OnboardingRequest JSON"
    )
    args = parser.parse_args(raw_argv)

    # v0a never needs these credentials and no descendant should inherit them.
    for credential_name in ("RESPAN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        os.environ.pop(credential_name, None)

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
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        print(json.dumps(_config_failure_evidence(), sort_keys=True), file=sys.stderr)
        return 2

    try:
        result = run_session(req, respan_api_key=respan_api_key)
    except PreflightError as exc:
        print(
            json.dumps(_preflight_failure_evidence(exc), sort_keys=True),
            file=sys.stderr,
        )
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
    print("\n--- diff (v0a; GitHub delivery is a separate post-acceptance step) ---")
    print(result.diff)
    print(f"\n{result.summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
