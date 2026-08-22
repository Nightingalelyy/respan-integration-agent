"""Session orchestrator: validate → (prep) → clone → agent → PR.

This is the whole loop, and the place cost is capped for v0 (max turns/tokens) until the
gateway exposes an Anthropic-compatible endpoint the agent can route through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from . import github
from .agent import (
    DEFAULT_AGENT_MAX_BUDGET_USD,
    DEFAULT_AGENT_MODEL,
    DEFAULT_RESPAN_BASE_URL,
    run_agent,
)
from .config import OnboardingRequest, Product
from .gateway_preflight import (
    FundingRequirement,
    GatewayReadinessBackend,
    PreflightConfigurationError,
    PreflightError,
    PreflightPlan,
    PreflightReport,
    PreflightSchemaError,
    PreflightTransportError,
    RespanGatewayReadinessClient,
    RoutePurpose,
    RouteRequirement,
)
from .patch import capture_worktree_patch
from .sandbox import checkout
from .skill import bundled_skill_dir, validate_skill_source
from .verify import verify_integration


_ORCHESTRATION_OPERATION = "anthropic.messages"
_ORCHESTRATION_PROVIDER = "anthropic"


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
    preflight: PreflightReport
    agent_started_at: datetime
    pr: github.OpenedPR | None  # None in v0a (no token → no PR, just the diff)


@dataclass(frozen=True)
class _PreflightOutcome:
    plan: PreflightPlan
    report: PreflightReport


def _build_preflight_plan(
    req: OnboardingRequest,
    *,
    respan_base_url: str,
    agent_model: str,
    max_budget_usd: float,
) -> PreflightPlan:
    """Freeze the exact routes that preflight approves and execution consumes."""

    if not isinstance(respan_base_url, str):
        raise PreflightConfigurationError()
    normalized_base_url = respan_base_url.strip().rstrip("/")
    if normalized_base_url != DEFAULT_RESPAN_BASE_URL:
        raise PreflightConfigurationError()

    try:
        routes = [
            RouteRequirement(
                check_id="orchestration",
                purpose=RoutePurpose.orchestration,
                operation=_ORCHESTRATION_OPERATION,
                model=agent_model,
                provider=_ORCHESTRATION_PROVIDER,
                funding=FundingRequirement.any,
                required_credit_usd=max_budget_usd,
            )
        ]
        if req.product in (Product.gateway, Product.both):
            gateway = req.gateway
            if gateway is None:  # strict request validation normally rejects this
                raise PreflightConfigurationError()
            funding = FundingRequirement(gateway.funding.value)
            for index, route in enumerate(gateway.routes, start=1):
                routes.append(
                    RouteRequirement(
                        check_id=f"target-primary-{index:03d}",
                        purpose=RoutePurpose.target,
                        operation=route.operation.value,
                        model=route.model,
                        provider=route.provider,
                        funding=funding,
                        required_credit_usd=gateway.required_credit_usd,
                    )
                )
            for index, route in enumerate(gateway.fallback_routes, start=1):
                routes.append(
                    RouteRequirement(
                        check_id=f"target-fallback-{index:03d}",
                        purpose=RoutePurpose.target,
                        operation=route.operation.value,
                        model=route.model,
                        provider=route.provider,
                        funding=funding,
                        required_credit_usd=gateway.required_credit_usd,
                    )
                )
        return PreflightPlan(
            respan_base_url=normalized_base_url,
            agent_model=agent_model,
            max_budget_usd=max_budget_usd,
            routes=tuple(routes),
        )
    except PreflightError:
        raise
    except (TypeError, ValueError):
        raise PreflightConfigurationError() from None


def _validate_preflight_report(
    plan: PreflightPlan, report: object
) -> PreflightReport:
    """Keep injected readiness backends behind the same fail-closed boundary."""

    if not isinstance(report, PreflightReport) or not report.passed:
        raise PreflightSchemaError()
    expected = tuple(
        (
            route.check_id,
            route.purpose,
            route.operation,
            route.model,
            route.provider,
            route.funding,
            float(route.required_credit_usd),
        )
        for route in plan.routes
    )
    observed = tuple(
        (
            check.check_id,
            check.purpose,
            check.operation,
            check.requested_model,
            check.provider,
            check.funding,
            float(check.required_credit_usd),
        )
        for check in report.checks
    )
    if observed != expected:
        raise PreflightSchemaError()
    if report.approved_agent_model != plan.agent_model:
        raise PreflightSchemaError()
    orchestration_check = next(
        check
        for check in report.checks
        if check.purpose is RoutePurpose.orchestration
    )
    if orchestration_check.resolved_model != plan.agent_model:
        raise PreflightSchemaError()
    return report


def _preflight(
    req: OnboardingRequest,
    respan_api_key: str,
    *,
    respan_base_url: str = DEFAULT_RESPAN_BASE_URL,
    agent_model: str = DEFAULT_AGENT_MODEL,
    max_budget_usd: float = DEFAULT_AGENT_MAX_BUDGET_USD,
    gateway_readiness_backend: GatewayReadinessBackend | None = None,
) -> _PreflightOutcome:
    """Fail fast on every gateway dependency before checkout or model execution.

    Every onboarding product uses the gateway for orchestration. Gateway/both requests
    additionally require every exact target and fallback route to be ready using the
    selected funding source.
    """
    if not isinstance(respan_api_key, str) or not respan_api_key.strip():
        raise PreflightConfigurationError()
    validate_skill_source(bundled_skill_dir())
    plan = _build_preflight_plan(
        req,
        respan_base_url=respan_base_url,
        agent_model=agent_model,
        max_budget_usd=max_budget_usd,
    )
    backend = (
        gateway_readiness_backend
        if gateway_readiness_backend is not None
        else RespanGatewayReadinessClient(respan_api_key)
    )
    readiness_started_at = datetime.now(timezone.utc)
    try:
        report = backend.check(plan)
    except PreflightError:
        raise
    except Exception:
        raise PreflightTransportError() from None
    readiness_finished_at = datetime.now(timezone.utc)
    validated_report = _validate_preflight_report(plan, report)
    if not (
        readiness_started_at
        <= validated_report.started_at
        <= validated_report.finished_at
        <= readiness_finished_at
    ):
        raise PreflightSchemaError()
    return _PreflightOutcome(plan, validated_report)


def run_session(
    req: OnboardingRequest,
    *,
    respan_api_key: str,
    respan_base_url: str = DEFAULT_RESPAN_BASE_URL,
    github_token: str | None = None,
    gateway_readiness_backend: GatewayReadinessBackend | None = None,
) -> SessionResult:
    preflight = _preflight(
        req,
        respan_api_key,
        respan_base_url=respan_base_url,
        gateway_readiness_backend=gateway_readiness_backend,
    )
    preflight_returned_at = datetime.now(timezone.utc)
    if preflight.report.finished_at > preflight_returned_at:
        raise PreflightSchemaError()
    branch = f"respan/onboard-{req.product.value}"
    title = f"Add Respan {req.product.value} instrumentation"
    with checkout(req.repo_url, req.base_branch, token=github_token) as workdir:
        # Claude's subprocess reports the real macOS `/private/var/...` path
        # even when tempfile supplied the `/var/...` symlink spelling.
        agent_checkout_root = workdir.resolve()
        agent_started_at = datetime.now(timezone.utc)
        result = run_agent(
            workdir,
            req,
            respan_api_key=respan_api_key,
            respan_base_url=preflight.plan.respan_base_url,
            model=preflight.plan.agent_model,
            max_budget_usd=preflight.plan.max_budget_usd,
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
        preflight=preflight.report,
        agent_started_at=agent_started_at,
        pr=pr,
    )
