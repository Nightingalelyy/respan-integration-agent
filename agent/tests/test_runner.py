from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from respan_integration_agent import cli, runner
from respan_integration_agent.agent import DEFAULT_AGENT_MODEL
from respan_integration_agent.config import (
    GatewayConfig,
    GatewayFunding,
    GatewayOperation,
    GatewayRoute,
    OnboardingRequest,
    Product,
    TracingConfig,
)
from respan_integration_agent.gateway_preflight import (
    FundingRequirement,
    PreflightAuthenticationError,
    PreflightCheck,
    PreflightError,
    PreflightPlan,
    PreflightReport,
    RoutePurpose,
)


UTC = timezone.utc
APPROVED_AGENT_MODEL = DEFAULT_AGENT_MODEL
APPROVED_AGENT_BUDGET_USD = 1.0
PRIMARY_TARGET_ROUTE = ("openai.chat.completions", "openai", "gpt-4o-mini")
FALLBACK_TARGET_ROUTES = (
    ("openai.chat.completions", "azure-openai", "openai/gpt-4.1-mini"),
    ("openai.chat.completions", "google", "gemini-flash-pinned"),
)


@dataclass(frozen=True)
class StubPreflightReport:
    """Small duck-typed report used to keep runner tests API-light."""

    passed: bool = True
    approved_agent_model: str = APPROVED_AGENT_MODEL
    approved_agent_max_budget_usd: float = APPROVED_AGENT_BUDGET_USD
    started_at: str = "2026-08-22T00:00:00+00:00"
    finished_at: str = "2026-08-22T00:00:01+00:00"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "respan-gateway-preflight/v1",
            "passed": self.passed,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "attempts": 1,
            "paid_canary_performed": False,
            "checks": [
                {
                    "role": "orchestration",
                    "code": "P_ROUTE_READY",
                    "status": "pass" if self.passed else "fail",
                    "resolved_model": self.approved_agent_model,
                }
            ],
        }


class RecordingReadinessBackend:
    """Planned injected seam: one authoritative ``check(plan)`` read."""

    def __init__(
        self,
        report: PreflightReport | StubPreflightReport | None = None,
        *,
        error: BaseException | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.report = report
        self.error = error
        self.events = events
        self.plans: list[Any] = []

    def check(self, plan: PreflightPlan) -> PreflightReport | StubPreflightReport:
        self.plans.append(plan)
        if self.events is not None:
            self.events.append("preflight")
        if self.error is not None:
            raise self.error
        if self.report is None:
            self.report = _passing_report(plan)
        return self.report


def _route(operation: str, provider: str, model: str) -> GatewayRoute:
    return GatewayRoute(
        operation=GatewayOperation(operation),
        provider=provider,
        model=model,
    )


def _request(product: Product) -> OnboardingRequest:
    gateway = None
    tracing = None
    if product in (Product.gateway, Product.both):
        gateway = GatewayConfig(
            funding=GatewayFunding.credits,
            routes=(_route(*PRIMARY_TARGET_ROUTE),),
            fallback_routes=tuple(_route(*route) for route in FALLBACK_TARGET_ROUTES),
            required_credit_usd=0.25,
            enable_caching=False,
        )
    if product in (Product.tracing, Product.both):
        tracing = TracingConfig()
    return OnboardingRequest(
        repo_url="/private/tmp/trusted-fixture",
        base_branch="main",
        product=product,
        tracing=tracing,
        gateway=gateway,
        verification=None,
    )


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _plan_checks(plan: Any) -> tuple[Any, ...]:
    """Single adapter for the planned immutable plan representation."""
    checks = getattr(plan, "checks", None)
    if checks is None:
        checks = getattr(plan, "requirements", None)
    if checks is None:
        checks = getattr(plan, "routes", None)
    assert checks is not None, "PreflightPlan must expose routes/checks/requirements"
    return tuple(checks)


def _check_route(check: Any) -> Any:
    return getattr(check, "route", check)


def _routes_for_role(plan: Any, role: str) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for check in _plan_checks(plan):
        check_role = getattr(check, "role", getattr(check, "purpose", ""))
        if _value(check_role) != role:
            continue
        route = _check_route(check)
        found.append(
            (
                _value(getattr(route, "operation")),
                _value(getattr(route, "provider")),
                str(getattr(route, "model")),
            )
        )
    return found


def _agent_budget_from_plan(plan: Any) -> float:
    for name in (
        "max_budget_usd",
        "agent_max_budget_usd",
        "max_agent_budget_usd",
        "orchestration_max_budget_usd",
    ):
        if hasattr(plan, name):
            return float(getattr(plan, name))
    orchestration = getattr(plan, "orchestration", None)
    if orchestration is not None and hasattr(orchestration, "max_budget_usd"):
        return float(orchestration.max_budget_usd)
    raise AssertionError("PreflightPlan must retain the approved agent budget")


def _passing_report(plan: PreflightPlan) -> PreflightReport:
    started_at = datetime.now(UTC)
    checks = []
    for route in plan.routes:
        checks.append(
            PreflightCheck(
                check_id=route.check_id,
                purpose=route.purpose,
                operation=route.operation,
                requested_model=route.model,
                resolved_model=(
                    APPROVED_AGENT_MODEL
                    if route.purpose is RoutePurpose.orchestration
                    else route.model
                ),
                provider=route.provider,
                funding=route.funding,
                required_credit_usd=route.required_credit_usd,
                credential_source=(
                    "managed"
                    if route.funding is FundingRequirement.credits
                    else "customer"
                    if route.funding is FundingRequirement.byok
                    else "managed"
                ),
                attempts=1,
            )
        )
    return PreflightReport(
        started_at=started_at,
        finished_at=started_at,
        checks=tuple(checks),
        attempts=1,
        paid_canary_performed=False,
    )


def _agent_result() -> SimpleNamespace:
    return SimpleNamespace(
        summary="applied deterministic integration",
        trace_id="a" * 32,
        run_id="respan-v0a-agent-runner-test",
        session_id="session-runner-test",
        telemetry_flushed=True,
        num_turns=2,
        duration_ms=250,
        total_cost_usd=0.1,
        changed_files=["app.py"],
    )


def _install_happy_runner_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str] | None = None,
    captured_agent_kwargs: dict[str, Any] | None = None,
) -> None:
    @contextmanager
    def fake_checkout(*_args: Any, **_kwargs: Any):
        if events is not None:
            events.append("checkout")
        yield Path("/private/tmp/respan-runner-test-checkout")

    def fake_agent(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        if events is not None:
            events.append("run_agent")
        if captured_agent_kwargs is not None:
            captured_agent_kwargs.update(kwargs)
        return _agent_result()

    monkeypatch.setattr(runner, "checkout", fake_checkout)
    monkeypatch.setattr(runner, "checkout_head", lambda *_args: "c" * 40)
    monkeypatch.setattr(runner, "run_agent", fake_agent)
    monkeypatch.setattr(
        runner,
        "capture_worktree_patch",
        lambda *_args, **_kwargs: SimpleNamespace(
            changed_files=["app.py"], diff="diff --git a/app.py b/app.py\n"
        ),
    )
    monkeypatch.setattr(runner, "verify_integration", lambda *_args, **_kwargs: None)


def test_preflight_precedes_checkout_and_agent_and_approved_values_are_used(
    monkeypatch,
):
    events: list[str] = []
    agent_kwargs: dict[str, Any] = {}
    backend = RecordingReadinessBackend(events=events)
    _install_happy_runner_fakes(
        monkeypatch, events=events, captured_agent_kwargs=agent_kwargs
    )

    result = runner.run_session(
        _request(Product.tracing),
        respan_api_key="sentinel-secret",
        gateway_readiness_backend=backend,
    )

    assert events[:3] == ["preflight", "checkout", "run_agent"]
    assert len(backend.plans) == 1
    assert agent_kwargs["model"] == APPROVED_AGENT_MODEL
    assert agent_kwargs["max_budget_usd"] == APPROVED_AGENT_BUDGET_USD
    assert agent_kwargs["max_budget_usd"] == _agent_budget_from_plan(backend.plans[0])
    assert result.preflight is backend.report
    assert result.preflight.passed is True
    assert result.agent_started_at.tzinfo is UTC
    assert result.preflight.finished_at <= result.agent_started_at
    assert result.base_commit == "c" * 40

    serialized = json.dumps(result.preflight.to_dict(), sort_keys=True)
    assert "sentinel-secret" not in serialized
    assert "response_body" not in serialized
    assert "authorization" not in serialized.lower()


def test_falsey_injected_backend_is_used_without_constructing_a_real_client(
    monkeypatch,
):
    class FalseyBackend(RecordingReadinessBackend):
        def __bool__(self) -> bool:
            return False

    backend = FalseyBackend()
    _install_happy_runner_fakes(monkeypatch)

    def forbidden_real_client(*_args: Any, **_kwargs: Any):
        raise AssertionError("falsey injected backend was replaced")

    monkeypatch.setattr(runner, "RespanGatewayReadinessClient", forbidden_real_client)

    result = runner.run_session(
        _request(Product.tracing),
        respan_api_key="sentinel-secret",
        gateway_readiness_backend=backend,
    )

    assert result.preflight is backend.report
    assert len(backend.plans) == 1


@pytest.mark.parametrize("product", list(Product))
def test_plan_checks_orchestration_for_every_product_and_target_only_when_requested(
    monkeypatch, product
):
    backend = RecordingReadinessBackend()
    _install_happy_runner_fakes(monkeypatch)

    runner.run_session(
        _request(product),
        respan_api_key="sentinel-secret",
        gateway_readiness_backend=backend,
    )

    assert len(backend.plans) == 1
    plan = backend.plans[0]
    assert plan.routes[0].check_id == "orchestration"
    orchestration = _routes_for_role(plan, "orchestration")
    assert len(orchestration) == 1

    target = _routes_for_role(plan, "target")
    if product is Product.tracing:
        assert target == []
    else:
        assert [route.check_id for route in plan.routes[1:]] == [
            "target-primary-001",
            "target-fallback-001",
            "target-fallback-002",
        ]
        assert target == [
            PRIMARY_TARGET_ROUTE,
            *FALLBACK_TARGET_ROUTES,
        ]


@pytest.mark.parametrize(
    "backend",
    [
        RecordingReadinessBackend(
            error=PreflightAuthenticationError(status_code=401, attempts=1)
        ),
        RecordingReadinessBackend(report=StubPreflightReport(passed=False)),
    ],
    ids=["typed-failure", "nonpassing-report"],
)
def test_every_preflight_failure_stops_checkout_and_agent(monkeypatch, backend):
    calls = {"checkout": 0, "agent": 0}

    @contextmanager
    def forbidden_checkout(*_args: Any, **_kwargs: Any):
        calls["checkout"] += 1
        yield Path("/private/tmp/should-not-exist")

    monkeypatch.setattr(runner, "checkout", forbidden_checkout)
    monkeypatch.setattr(
        runner,
        "run_agent",
        lambda *_args, **_kwargs: calls.__setitem__("agent", calls["agent"] + 1),
    )
    with pytest.raises(PreflightError):
        runner.run_session(
            _request(Product.both),
            respan_api_key="sentinel-secret",
            gateway_readiness_backend=backend,
        )

    assert len(backend.plans) == 1, "failure must come from the readiness check"
    assert calls == {"checkout": 0, "agent": 0}


def test_run_session_rejects_legacy_github_delivery_before_preflight(monkeypatch):
    monkeypatch.setattr(
        runner,
        "_preflight",
        lambda *_args, **_kwargs: pytest.fail("legacy delivery reached preflight"),
    )
    with pytest.raises(runner.SessionDeliveryDisabledError) as raised:
        runner.run_session(
            _request(Product.tracing),
            respan_api_key="sentinel-secret",
            github_token="github-sentinel-secret",
        )

    assert "github-sentinel-secret" not in str(raised.value)


def test_expected_base_mismatch_stops_before_agent(monkeypatch):
    events: list[str] = []
    backend = RecordingReadinessBackend(events=events)
    _install_happy_runner_fakes(monkeypatch, events=events)
    monkeypatch.setattr(runner, "checkout_head", lambda *_args: "c" * 40)
    monkeypatch.setattr(
        runner,
        "run_agent",
        lambda *_args, **_kwargs: pytest.fail("moved base reached the agent"),
    )

    with pytest.raises(runner.SessionBaseChangedError):
        runner.run_session(
            _request(Product.tracing),
            respan_api_key="sentinel-secret",
            expected_base_commit="d" * 40,
            gateway_readiness_backend=backend,
        )

    assert events == ["preflight", "checkout"]


def test_head_change_after_agent_is_rejected(monkeypatch):
    events: list[str] = []
    backend = RecordingReadinessBackend(events=events)
    _install_happy_runner_fakes(monkeypatch, events=events)
    heads = iter(("c" * 40, "d" * 40))
    monkeypatch.setattr(runner, "checkout_head", lambda *_args: next(heads))

    with pytest.raises(RuntimeError, match="HEAD changed"):
        runner.run_session(
            _request(Product.tracing),
            respan_api_key="sentinel-secret",
            gateway_readiness_backend=backend,
        )

    assert events == ["preflight", "checkout", "run_agent"]


@pytest.mark.parametrize("mutation", ["model", "future_timestamp"])
def test_malformed_injected_report_is_rejected_before_checkout(monkeypatch, mutation):
    calls = {"checkout": 0}

    class MalformedBackend:
        def check(self, plan: PreflightPlan) -> PreflightReport:
            report = _passing_report(plan)
            if mutation == "model":
                object.__setattr__(report.checks[0], "resolved_model", "other-model")
            else:
                future = datetime.now(UTC) + timedelta(hours=1)
                object.__setattr__(report, "started_at", future)
                object.__setattr__(report, "finished_at", future)
            return report

    @contextmanager
    def forbidden_checkout(*_args: Any, **_kwargs: Any):
        calls["checkout"] += 1
        yield Path("/private/tmp/should-not-exist")

    monkeypatch.setattr(runner, "checkout", forbidden_checkout)

    with pytest.raises(PreflightError):
        runner.run_session(
            _request(Product.tracing),
            respan_api_key="sentinel-secret",
            gateway_readiness_backend=MalformedBackend(),
        )

    assert calls["checkout"] == 0


def test_cli_serializes_only_stable_preflight_error_fields(
    monkeypatch, tmp_path, capsys
):
    class HostileStringPreflightError(PreflightAuthenticationError):
        def __str__(self) -> str:
            return (
                "sentinel-secret raw-response-body "
                "Authorization: Bearer sentinel-secret"
            )

    error = HostileStringPreflightError(status_code=401, attempts=1)
    safe_payload = cli._preflight_failure_evidence(error)

    def fail_session(*_args: Any, **_kwargs: Any) -> None:
        raise error

    config_path = tmp_path / "request.json"
    config_path.write_text(
        json.dumps(
            {
                "repo_url": "/private/tmp/trusted-fixture",
                "product": "tracing",
                "tracing": {"mode": "auto"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RESPAN_API_KEY", "sentinel-secret")
    monkeypatch.setattr(cli, "run_session", fail_session)

    exit_code = cli.main(["run", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert exit_code != 0
    assert captured.out == ""
    assert json.loads(captured.err) == safe_payload
    assert "sentinel-secret" not in captured.err
    assert "raw-response-body" not in captured.err
    assert "Authorization" not in captured.err


def test_cli_config_failure_never_reflects_hostile_route_input(
    monkeypatch, tmp_path, capsys
):
    hostile_value = "sk-" + "hostilecredentialfragment" * 2
    config_path = tmp_path / "hostile-request.json"
    config_path.write_text(
        json.dumps(
            {
                "repo_url": "/private/tmp/trusted-fixture",
                "product": "gateway",
                "gateway": {
                    "funding": "byok",
                    "routes": [
                        {
                            "operation": "openai.chat.completions",
                            "provider": "openai",
                            "model": hostile_value,
                        }
                    ],
                    "required_credit_usd": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RESPAN_API_KEY", "sentinel-secret")
    monkeypatch.setattr(
        cli,
        "run_session",
        lambda *_args, **_kwargs: pytest.fail("invalid config reached run_session"),
    )

    exit_code = cli.main(["run", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == cli._config_failure_evidence()
    assert hostile_value not in captured.err
    assert str(config_path) not in captured.err


@pytest.mark.parametrize(
    "token_args",
    [
        ("--token", "github-sentinel-secret"),
        ("--tok", "github-sentinel-secret"),
        ("--t=github-sentinel-secret",),
    ],
)
def test_cli_rejects_removed_token_option_without_reflecting_value(
    monkeypatch, capsys, token_args
):
    hostile_token = "github-sentinel-secret"
    monkeypatch.setattr(
        cli,
        "run_session",
        lambda *_args, **_kwargs: pytest.fail("token option reached run_session"),
    )

    exit_code = cli.main(["run", "--config", "/not/read", *token_args])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ""
    assert json.loads(captured.err) == cli._config_failure_evidence()
    assert hostile_token not in captured.err


def test_cli_scrubs_all_ambient_github_credentials_before_session(
    monkeypatch, tmp_path, capsys
):
    config_path = tmp_path / "request.json"
    config_path.write_text(
        json.dumps(
            {
                "repo_url": "/private/tmp/trusted-fixture",
                "product": "tracing",
                "tracing": {"mode": "auto"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RESPAN_API_KEY", "sentinel-respan-secret")
    for name in ("RESPAN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        monkeypatch.setenv(name, "sentinel-github-secret")

    def stopped_session(*_args: Any, **_kwargs: Any) -> None:
        for name in ("RESPAN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
            assert name not in os.environ
        raise PreflightAuthenticationError(status_code=401, attempts=1)

    monkeypatch.setattr(cli, "run_session", stopped_session)

    assert cli.main(["run", "--config", str(config_path)]) != 0
    assert "sentinel-github-secret" not in capsys.readouterr().err


def test_cli_unexpected_runtime_failure_never_reflects_exception_text(
    monkeypatch, tmp_path, capsys
):
    hostile_value = "sentinel-secret raw-response Authorization: Bearer sentinel-secret"
    config_path = tmp_path / "request.json"
    config_path.write_text(
        json.dumps(
            {
                "repo_url": "/private/tmp/trusted-fixture",
                "product": "tracing",
                "tracing": {"mode": "auto"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RESPAN_API_KEY", "sentinel-secret")

    def fail_session(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(hostile_value)

    monkeypatch.setattr(cli, "run_session", fail_session)

    exit_code = cli.main(["run", "--config", str(config_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert json.loads(captured.err) == cli._runtime_failure_evidence()
    assert hostile_value not in captured.err
    assert "sentinel-secret" not in captured.err
