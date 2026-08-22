"""Session orchestrator: validate → (prep) → clone → agent → PR.

This is the whole loop, and the place cost is capped for v0 (max turns/tokens) until the
gateway exposes an Anthropic-compatible endpoint the agent can route through.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from . import github
from .agent import DEFAULT_RESPAN_BASE_URL, run_agent
from .config import OnboardingRequest, Product
from .patch import capture_worktree_patch
from .sandbox import checkout
from .skill import bundled_skill_dir, validate_skill_source
from .verify import verify_integration


@dataclass
class SessionResult:
    summary: str
    trace_id: str
    trace_url: str
    run_id: str
    agent_session_id: str
    # Local flush completion only; backend acceptance is established by the
    # separate exact-marker platform verification in the v0a smoke run.
    telemetry_flushed: bool
    num_turns: int
    duration_ms: int
    total_cost_usd: float | None
    changed_files: list[str]
    diff: str
    # Retain the now-disposed checkout's absolute path so the deterministic
    # backend gate can prove every recorded Edit stayed inside that checkout.
    agent_checkout_root: Path
    pr: github.OpenedPR | None  # None in v0a (no token → no PR, just the diff)


def _preflight(req: OnboardingRequest, respan_api_key: str) -> None:
    """Fail fast on prep the onboarding depends on.

    Gateway onboarding is worthless if the account can't route a call, so verify funding
    (credits balance > 0, or BYOK keys present) BEFORE we clone or spend a token on the agent.
    """
    if not respan_api_key.strip():
        raise ValueError("RESPAN_API_KEY is empty")
    validate_skill_source(bundled_skill_dir())
    if req.product in (Product.gateway, Product.both):
        # TODO(v0): call the Respan API to confirm credits balance > 0 (or BYOK configured).
        # Raise a clear error the dashboard can surface ("add credits before onboarding").
        pass


def run_session(
    req: OnboardingRequest,
    *,
    respan_api_key: str,
    respan_base_url: str = DEFAULT_RESPAN_BASE_URL,
    github_token: str | None = None,
) -> SessionResult:
    _preflight(req, respan_api_key)
    branch = f"respan/onboard-{req.product.value}"
    title = f"Add Respan {req.product.value} instrumentation"
    with checkout(req.repo_url, req.base_branch, token=github_token) as workdir:
        # Claude's subprocess reports the real macOS `/private/var/...` path
        # even when tempfile supplied the `/var/...` symlink spelling.
        agent_checkout_root = workdir.resolve()
        result = run_agent(
            workdir,
            req,
            respan_api_key=respan_api_key,
            respan_base_url=respan_base_url,
        )
        captured = capture_worktree_patch(workdir)
        if captured.changed_files != result.changed_files:
            raise RuntimeError("worktree changed after the agent terminal result")
        verify_integration(
            workdir,
            req,
            captured.changed_files,
            captured.diff,
            respan_api_key=respan_api_key,
        )
        pr = None
        if github_token:  # v0b: deliver as a PR; v0a: just the diff
            github.commit_branch(workdir, branch, title)
            pr = github.open_pr(workdir, branch, title, result.summary, github_token)
    return SessionResult(
        summary=result.summary,
        trace_id=result.trace_id,
        trace_url=(
            "https://platform.respan.ai/platform/traces?"
            + urlencode({"trace_unique_id": result.trace_id})
        ),
        run_id=result.run_id,
        agent_session_id=result.session_id,
        telemetry_flushed=result.telemetry_flushed,
        num_turns=result.num_turns,
        duration_ms=result.duration_ms,
        total_cost_usd=result.total_cost_usd,
        changed_files=captured.changed_files,
        diff=captured.diff,
        agent_checkout_root=agent_checkout_root,
        pr=pr,
    )
