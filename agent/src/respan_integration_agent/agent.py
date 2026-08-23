"""Deterministic Claude Agent SDK loop routed and traced through Respan."""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import OnboardingRequest, Product, TracingMode, VerificationProfile
from .patch import capture_worktree_patch
from .skill import ProvisionedSkill, provision_respan_skill


DEFAULT_RESPAN_BASE_URL = "https://api.respan.ai/api"
# Use the full model name accepted by Claude Code instead of the mutable
# ``sonnet`` alias so preflight and execution approve the same route.
DEFAULT_AGENT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_AGENT_MAX_TURNS = 40
DEFAULT_AGENT_MAX_BUDGET_USD = 1.0
DEFAULT_AGENT_TIMEOUT_SECONDS = 300.0


class AgentRunError(RuntimeError):
    """The agent did not reach a trustworthy successful terminal state."""


class TraceLifecycleError(RuntimeError):
    """The v0a dogfood trace could not be captured or flushed."""


def _validated_respan_base_url(value: str) -> str:
    """Keep the v0 credential on the reviewed public Respan endpoint."""
    normalized = value.strip().rstrip("/")
    if normalized != DEFAULT_RESPAN_BASE_URL:
        raise AgentRunError(
            "v0 supports only the official Respan API endpoint; "
            "enterprise/custom endpoints require an explicit allowlist design"
        )
    return normalized


@dataclass(frozen=True)
class AgentResult:
    summary: str
    changed_files: list[str]
    trace_id: str
    run_id: str
    session_id: str
    num_turns: int
    duration_ms: int
    total_cost_usd: float | None
    usage: dict[str, Any] | None
    # True means the local force-flush call returned without raising. The Respan
    # exporter does not surface backend acceptance; the smoke run verifies that
    # separately by querying the exact run_id/trace_id in the platform.
    telemetry_flushed: bool


def _build_prompt(
    req: OnboardingRequest,
    *,
    skill: ProvisionedSkill,
    run_id: str,
) -> str:
    """Turn the validated config into a narrow, testable instruction."""
    reference = (
        skill.tracing_reference
        if req.product in (Product.tracing, Product.both)
        else skill.gateway_reference
    )
    lines = [
        "Use the respan skill to onboard this repository. Invoke the skill first, then read",
        f"the pinned reference at {reference}.",
        "The reference is available through Read. Do not use the web or install anything.",
        "Follow the config exactly; do not ask questions. Make only the required edits, then stop.",
        "Do not commit or push; the harness captures and validates the patch.",
        f"Harness run id (non-secret): {run_id}",
        "",
    ]
    if req.product in (Product.tracing, Product.both) and req.tracing:
        tracing = req.tracing
        lines.append(f"TRACING: mode={tracing.mode.value}.")
        if tracing.mode is TracingMode.auto:
            lines.append(
                "  Auto: add only the core Respan SDK and one retained Respan initialization. "
                "No decorators and no framework instrumentor."
            )
        else:
            lines.append(
                f"  Full: framework_instrumentor={tracing.framework_instrumentor or 'none'}, "
                f"decorators={tracing.use_decorators}, workflows={tracing.workflows or 'agent-chosen'}."
            )
        if tracing.environment:
            lines.append(f"  Set Respan environment={tracing.environment!r}.")
        if tracing.service_name:
            lines.append(f"  Set Respan app_name={tracing.service_name!r}.")
        lines.append(
            f"  Endpoint={tracing.endpoint.value}. The SDK reads RESPAN_API_KEY from the target "
            "runtime environment; never hardcode or add an .env file."
        )
    if req.product in (Product.gateway, Product.both) and req.gateway:
        gateway = req.gateway
        routes = ", ".join(
            f"{route.operation.value}[provider={route.provider},model={route.model}]"
            for route in gateway.routes
        )
        fallbacks = ", ".join(
            f"{route.operation.value}[provider={route.provider},model={route.model}]"
            for route in gateway.fallback_routes
        )
        lines.append(
            f"GATEWAY: funding={gateway.funding.value}, "
            f"required_credit_usd={gateway.required_credit_usd:g}, "
            f"caching={gateway.enable_caching}."
        )
        lines.append(f"  Exact primary routes, in order: {routes}.")
        lines.append(f"  Exact fallback routes, in order: {fallbacks or 'none'}.")

    if (
        req.verification
        and req.verification.profile is VerificationProfile.python_openai_auto_smoke
    ):
        tracing = req.tracing
        assert tracing is not None
        lines += [
            "",
            "PINNED V0A SMOKE CONTRACT (the harness rejects any deviation):",
            f"- Keep openai==1.99.9 and add exactly respan-ai=={req.verification.respan_ai_version} and "
            f"opentelemetry-instrumentation-openai=={req.verification.openai_otel_version}. "
            "The OpenTelemetry package is the reviewed direct-OpenAI compatibility dependency for this pinned SDK.",
            "- In app.py, add exactly one `from respan import Respan`.",
            "- Before any OpenAI(...) construction, retain exactly one Respan instance.",
            f"- Pass app_name={tracing.service_name!r} and environment={tracing.environment!r}.",
            '- Pass metadata={"run_id": os.environ["RESPAN_EXAMPLE_RUN_ID"]} so the target trace is exactly queryable.',
            "- Preserve the existing main() body byte-for-byte.",
            "- In the existing __main__ guard, call main() inside try and call the retained instance's flush() in finally.",
            "- Change only app.py and requirements.txt. Add no files, decorators, manual instrumentor setup, framework instrumentors, gateway rewrites, or refactors.",
        ]

    lines += [
        "",
        "Return a concise factual summary of the two edits and verification limitations.",
    ]
    return "\n".join(lines)


def _validate_terminal_result(result: Any | None) -> Any:
    if result is None:
        raise AgentRunError("Claude Agent SDK ended without a terminal ResultMessage")

    problems: list[str] = []
    if getattr(result, "is_error", True):
        problems.append("is_error=true")
    if getattr(result, "subtype", None) != "success":
        problems.append(f"subtype={getattr(result, 'subtype', None)!r}")
    terminal_reason = getattr(result, "terminal_reason", None)
    if terminal_reason not in (None, "completed"):
        problems.append(f"terminal_reason={terminal_reason!r}")
    if getattr(result, "permission_denials", None):
        problems.append("permission_denials present")
    if getattr(result, "errors", None):
        problems.append("errors present")
    if getattr(result, "api_error_status", None) is not None:
        problems.append(f"api_error_status={result.api_error_status}")
    if getattr(result, "deferred_tool_use", None) is not None:
        problems.append("deferred_tool_use present")
    summary = (getattr(result, "result", None) or "").strip()
    if not summary:
        problems.append("empty result summary")

    if problems:
        detail = (
            summary[:500]
            if summary
            else "; ".join(str(item) for item in getattr(result, "errors", []) or [])
        )
        suffix = f": {detail}" if detail else ""
        raise AgentRunError(
            f"Claude Agent SDK terminal result rejected ({', '.join(problems)}){suffix}"
        )
    return result


def _available_command_names(server_info: dict[str, Any] | None) -> set[str]:
    names: set[str] = set()
    for item in (server_info or {}).get("commands", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lstrip("/")
        if name:
            names.add(name)
    return names


async def _run(
    workdir: Path,
    req: OnboardingRequest,
    respan_api_key: str,
    respan_base_url: str,
    model: str,
    max_turns: int,
    max_budget_usd: float,
    timeout_seconds: float,
    skill: ProvisionedSkill,
    run_id: str,
) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

    options = ClaudeAgentOptions(
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        cwd=str(workdir),
        permission_mode="acceptEdits",
        tools=["Skill", "Read", "Glob", "Grep", "Edit"],
        disallowed_tools=["Bash", "WebFetch", "WebSearch", "Agent"],
        skills=["respan"],
        setting_sources=["user"],
        strict_mcp_config=True,
        mcp_servers={},
        plugins=[],
        add_dirs=[skill.skill_dir],
        # The SDK's bundled Claude CLI 2.1.238 does not load user-directory
        # skills even though they remain invokable once discovered. Keep the
        # temp user config, explicit setting source, empty MCP/plugins, and
        # restricted tools as the isolation boundary, then verify discovery
        # before sending the first model turn.
        extra_args={"no-session-persistence": None},
        env={
            "CLAUDE_CONFIG_DIR": str(skill.config_dir),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_AGENT_SDK_CLIENT_APP": "respan-integration-agent/0.0.1",
            "RESPAN_API_KEY": "",
            "OPENAI_API_KEY": "",
            "GITHUB_TOKEN": "",
            "GH_TOKEN": "",
            "RESPAN_GITHUB_TOKEN": "",
            "ANTHROPIC_API_KEY": respan_api_key,
            "ANTHROPIC_AUTH_TOKEN": respan_api_key,
            "ANTHROPIC_BASE_URL": f"{respan_base_url}/anthropic/",
        },
    )

    client = ClaudeSDKClient(options=options)
    terminal_results: list[Any] = []
    try:
        async with asyncio.timeout(timeout_seconds):
            await client.connect()
            commands = _available_command_names(await client.get_server_info())
            if "respan" not in commands:
                raise AgentRunError(
                    "isolated Claude configuration did not discover the pinned respan skill"
                )

            await client.query(_build_prompt(req, skill=skill, run_id=run_id))
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    terminal_results.append(message)
    except TimeoutError as exc:
        raise AgentRunError(
            f"Claude Agent SDK exceeded {timeout_seconds:.0f}s timeout"
        ) from exc
    finally:
        await client.disconnect()

    if len(terminal_results) != 1:
        raise AgentRunError(
            f"expected exactly one terminal ResultMessage, got {len(terminal_results)}"
        )
    return _validate_terminal_result(terminal_results[0])


def run_agent(
    workdir: Path,
    req: OnboardingRequest,
    *,
    respan_api_key: str,
    respan_base_url: str = DEFAULT_RESPAN_BASE_URL,
    model: str = DEFAULT_AGENT_MODEL,
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    max_budget_usd: float = DEFAULT_AGENT_MAX_BUDGET_USD,
    timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
    run_id: str | None = None,
) -> AgentResult:
    """Run one isolated, traced turn and complete its local telemetry flush."""
    from respan import Respan
    from respan_instrumentation_claude_agent_sdk import ClaudeAgentSDKInstrumentor

    resolved_run_id = run_id or f"respan-v0a-agent-{uuid.uuid4().hex[:16]}"
    resolved_base_url = _validated_respan_base_url(respan_base_url)
    trace_id: str | None = None
    terminal_result: Any | None = None
    telemetry_flushed = False

    with provision_respan_skill() as skill:
        instrumentor = ClaudeAgentSDKInstrumentor(
            agent_name="respan-integration-agent",
            capture_content=True,
        )
        respan = Respan(
            instrumentations=[instrumentor],
            api_key=respan_api_key,
            base_url=resolved_base_url,
            app_name="respan-integration-agent",
            environment="onboarding",
            metadata={"run_id": resolved_run_id},
            auto_flush="off",
        )
        try:
            if not getattr(instrumentor, "_is_instrumented", False):
                raise TraceLifecycleError(
                    "Claude Agent SDK instrumentation did not activate"
                )
            trace_client = respan.telemetry.get_client()
            with trace_client.start_span(resolved_run_id, kind="workflow") as root_span:
                if root_span is None:
                    raise TraceLifecycleError(
                        "Respan did not create the v0a session root span"
                    )
                span_context = root_span.get_span_context()
                if not span_context.is_valid:
                    raise TraceLifecycleError(
                        "Respan returned an invalid root span context"
                    )
                trace_id = format(span_context.trace_id, "032x")
                terminal_result = asyncio.run(
                    _run(
                        workdir,
                        req,
                        respan_api_key,
                        resolved_base_url,
                        model,
                        max_turns,
                        max_budget_usd,
                        timeout_seconds,
                        skill,
                        resolved_run_id,
                    )
                )
        finally:
            active_exception = sys.exc_info()[1]
            try:
                respan.flush()
                # Respan 4.1.0 discards force_flush()'s return value and its
                # exporter converts HTTP failures to SpanExportResult.FAILURE.
                # This means only that the local flush call returned.
                telemetry_flushed = True
            except Exception as exc:
                if active_exception is not None:
                    active_exception.add_note(
                        f"Respan flush also failed: {type(exc).__name__}: {exc}"
                    )
                else:
                    raise TraceLifecycleError(
                        f"Respan flush failed: {type(exc).__name__}: {exc}"
                    ) from exc
            finally:
                respan.shutdown()

    if terminal_result is None or trace_id is None or not telemetry_flushed:
        raise AgentRunError(
            "agent finished without a complete terminal result and local telemetry flush"
        )

    captured = capture_worktree_patch(workdir)
    return AgentResult(
        summary=terminal_result.result.strip(),
        changed_files=captured.changed_files,
        trace_id=trace_id,
        run_id=resolved_run_id,
        session_id=terminal_result.session_id,
        num_turns=terminal_result.num_turns,
        duration_ms=terminal_result.duration_ms,
        total_cost_usd=terminal_result.total_cost_usd,
        usage=terminal_result.usage,
        telemetry_flushed=telemetry_flushed,
    )
