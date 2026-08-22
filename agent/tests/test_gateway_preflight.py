from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from typing import Any

import pytest

from respan_integration_agent import platform as platform_module
from respan_integration_agent.gateway_preflight import (
    GATEWAY_READINESS_SCHEMA,
    GATEWAY_READINESS_URL,
    OFFICIAL_RESPAN_API_BASE,
    PREFLIGHT_REPORT_SCHEMA,
    FundingRequirement,
    GatewayReadinessBackend,
    PreflightAuthenticationError,
    PreflightAuthorizationError,
    PreflightCheck,
    PreflightConfigurationError,
    PreflightDeadlineError,
    PreflightError,
    PreflightNotReadyError,
    PreflightPlan,
    PreflightRateLimitError,
    PreflightRedirectError,
    PreflightReport,
    PreflightRequestError,
    PreflightResponseTooLargeError,
    PreflightSchemaError,
    PreflightTransportError,
    RespanGatewayReadinessClient,
    RoutePurpose,
    RouteRequirement,
)


FAKE_API_KEY = "rk-test-gateway-readiness-key"
WALL_START = 1_787_429_600.0


class FakeClock:
    def __init__(self, *, monotonic: float = 100.0, wall: float = WALL_START) -> None:
        self.monotonic_value = monotonic
        self.wall_value = wall
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


class FakeTransport:
    def __init__(self, responses: Iterable[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[platform_module._TransportRequest] = []

    def request(
        self, request: platform_module._TransportRequest
    ) -> platform_module._TransportResponse:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(request)
        return item


def orchestration_route(
    *,
    check_id: str = "orchestration",
    funding: FundingRequirement = FundingRequirement.any,
    credit: float = 10.0,
) -> RouteRequirement:
    return RouteRequirement(
        check_id=check_id,
        purpose=RoutePurpose.orchestration,
        operation="anthropic.messages",
        model="claude-sonnet-4-20250514",
        provider="anthropic",
        funding=funding,
        required_credit_usd=credit,
    )


def target_route(
    *,
    check_id: str = "target-primary-001",
    funding: FundingRequirement = FundingRequirement.byok,
    credit: float = 0.0,
) -> RouteRequirement:
    return RouteRequirement(
        check_id=check_id,
        purpose=RoutePurpose.target,
        operation="openai.chat.completions",
        model="gpt-5-mini",
        provider="openai",
        funding=funding,
        required_credit_usd=credit,
    )


def make_plan(*routes: RouteRequirement) -> PreflightPlan:
    selected = routes or (orchestration_route(), target_route())
    return PreflightPlan(
        respan_base_url=OFFICIAL_RESPAN_API_BASE,
        agent_model=selected[0].model,
        max_budget_usd=10.0,
        routes=tuple(selected),
    )


def check_payload(
    route: RouteRequirement,
    *,
    credential_source: str | None = None,
    resolved_model: str | None = None,
) -> dict[str, Any]:
    default_source = {
        FundingRequirement.credits: "managed",
        FundingRequirement.byok: "customer",
        FundingRequirement.any: "managed",
    }[route.funding]
    return {
        "check_id": route.check_id,
        "purpose": route.purpose.value,
        "operation": route.operation,
        "requested_model": route.model,
        "resolved_model": resolved_model
        or (
            route.model
            if route.purpose is RoutePurpose.orchestration
            else f"{route.provider}/{route.model}"
        ),
        "provider": route.provider,
        "credential_source": credential_source or default_source,
        "funding_requested": route.funding.value,
        "funding_satisfied": True,
        "required_credit_usd": float(route.required_credit_usd),
        "key_ready": True,
        "limits_ready": True,
        "route_ready": True,
        "status": "ready",
        "reason_codes": [],
    }


def ready_payload(plan: PreflightPlan) -> dict[str, Any]:
    return {
        "schema_version": GATEWAY_READINESS_SCHEMA,
        "ready": True,
        "checks": [check_payload(route) for route in plan.routes],
    }


def response(
    payload: Any = None,
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    raw: bytes | None = None,
) -> platform_module._TransportResponse:
    response_headers = {"content-type": "application/json; charset=utf-8"}
    response_headers.update(headers or {})
    body = raw if raw is not None else json.dumps(payload).encode("utf-8")
    return platform_module._TransportResponse(status, response_headers, body)


def make_client(
    *items: Any,
    clock: FakeClock | None = None,
    **kwargs: Any,
) -> tuple[RespanGatewayReadinessClient, FakeTransport, FakeClock]:
    test_clock = clock or FakeClock()
    transport = FakeTransport(items)
    client = RespanGatewayReadinessClient(
        FAKE_API_KEY,
        transport=transport,
        clock=test_clock.monotonic,
        wall_clock=test_clock.wall,
        sleeper=test_clock.sleep,
        **kwargs,
    )
    return client, transport, test_clock


def test_falsey_injected_transport_is_not_replaced() -> None:
    class FalseyTransport(FakeTransport):
        def __bool__(self) -> bool:
            return False

    plan = make_plan(orchestration_route())
    clock = FakeClock()
    transport = FalseyTransport([response(ready_payload(plan))])
    client = RespanGatewayReadinessClient(
        FAKE_API_KEY,
        transport=transport,
        clock=clock.monotonic,
        wall_clock=clock.wall,
        sleeper=clock.sleep,
    )

    assert client.check(plan).passed is True
    assert len(transport.requests) == 1


def test_enums_are_wire_values_and_public_records_are_frozen() -> None:
    assert [item.value for item in FundingRequirement] == ["any", "credits", "byok"]
    assert [item.value for item in RoutePurpose] == ["orchestration", "target"]

    route = orchestration_route()
    with pytest.raises(FrozenInstanceError):
        route.model = "other"  # type: ignore[misc]

    plan = make_plan(route)
    with pytest.raises(FrozenInstanceError):
        plan.agent_model = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"check_id": "Target Primary"}, "check_id"),
        ({"purpose": "orchestration"}, "purpose"),
        ({"operation": "responses.create"}, "operation"),
        ({"model": "https://model.example/key"}, "model"),
        ({"provider": "OpenAI"}, "provider"),
        ({"funding": "credits"}, "funding"),
        ({"required_credit_usd": True}, "required_credit_usd"),
        ({"required_credit_usd": float("nan")}, "required_credit_usd"),
        ({"required_credit_usd": -0.01}, "required_credit_usd"),
        ({"required_credit_usd": 10_001}, "required_credit_usd"),
    ],
)
def test_route_requirement_rejects_ambiguous_values(
    changes: dict[str, Any], message: str
) -> None:
    values: dict[str, Any] = {
        "check_id": "orchestration",
        "purpose": RoutePurpose.orchestration,
        "operation": "anthropic.messages",
        "model": "claude-sonnet-4-20250514",
        "provider": "anthropic",
        "funding": FundingRequirement.any,
        "required_credit_usd": 1.0,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        RouteRequirement(**values)


@pytest.mark.parametrize(
    "model",
    [
        "gpt/../secret",
        "gpt//secret",
        "https://model.example/name",
        "sk-" + "a" * 20,
        "ghp_" + "a" * 20,
        "m" * 129,
        "model/",
    ],
)
def test_model_validation_matches_gateway_config_safety(model: str) -> None:
    with pytest.raises(ValueError, match="model"):
        RouteRequirement(
            check_id="target-primary-001",
            purpose=RoutePurpose.target,
            operation="openai.chat.completions",
            model=model,
            provider="azure.openai",
            funding=FundingRequirement.byok,
            required_credit_usd=0.0,
        )


def test_provider_and_operation_validation_match_gateway_config() -> None:
    route = RouteRequirement(
        check_id="target-primary-001",
        purpose=RoutePurpose.target,
        operation="openai.chat.completions",
        model="gpt-4o",
        provider="azure.openai",
        funding=FundingRequirement.byok,
        required_credit_usd=0.0,
    )
    assert route.provider == "azure.openai"

    with pytest.raises(ValueError, match="provider"):
        RouteRequirement(**{**route.__dict__, "provider": "azure-"})
    with pytest.raises(ValueError, match="provider"):
        RouteRequirement(**{**route.__dict__, "provider": "rk-" + "a" * 20})
    with pytest.raises(ValueError, match="operation"):
        RouteRequirement(**{**route.__dict__, "operation": "responses.create"})
    with pytest.raises(ValueError, match="require provider"):
        RouteRequirement(
            **{
                **route.__dict__,
                "operation": "anthropic.messages",
                "provider": "openai",
            }
        )


def test_funding_credit_relationship_is_validated() -> None:
    with pytest.raises(ValueError, match="credits funding"):
        target_route(funding=FundingRequirement.credits, credit=0.0)
    with pytest.raises(ValueError, match="BYOK funding"):
        target_route(funding=FundingRequirement.byok, credit=1.0)


def test_plan_rejects_wrong_origin_shape_duplicates_and_model_mismatch() -> None:
    route = orchestration_route()
    with pytest.raises(ValueError, match="official Respan execution API base"):
        PreflightPlan("https://api.respan.ai", route.model, 10.0, (route,))
    with pytest.raises(ValueError, match="official Respan execution API base"):
        PreflightPlan("https://evil.example/api", route.model, 10.0, (route,))
    with pytest.raises(ValueError, match="tuple"):
        PreflightPlan(OFFICIAL_RESPAN_API_BASE, route.model, 10.0, [route])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one orchestration"):
        PreflightPlan(
            OFFICIAL_RESPAN_API_BASE,
            target_route().model,
            10.0,
            (target_route(),),
        )
    with pytest.raises(ValueError, match="unique check_id"):
        make_plan(route, target_route(check_id="orchestration"))
    with pytest.raises(ValueError, match="unique business requirements"):
        make_plan(
            route,
            target_route(
                check_id="target-primary-001",
                funding=FundingRequirement.credits,
                credit=1.0,
            ),
            target_route(
                check_id="target-fallback-001",
                funding=FundingRequirement.credits,
                credit=2.0,
            ),
        )
    with pytest.raises(ValueError, match="agent_model"):
        PreflightPlan(OFFICIAL_RESPAN_API_BASE, "other-model", 10.0, (route,))
    wrong_orchestration = RouteRequirement(
        check_id="orchestration",
        purpose=RoutePurpose.orchestration,
        operation="openai.chat.completions",
        model=route.model,
        provider="openai",
        funding=FundingRequirement.any,
        required_credit_usd=10.0,
    )
    with pytest.raises(ValueError, match="Anthropic messages"):
        make_plan(wrong_orchestration)
    with pytest.raises(ValueError, match="must equal"):
        PreflightPlan(OFFICIAL_RESPAN_API_BASE, route.model, 0.5, (route,))
    assert (
        make_plan(
            route,
            target_route(funding=FundingRequirement.credits, credit=100.0),
        )
        .routes[1]
        .required_credit_usd
        == 100.0
    )


def test_plan_to_dict_is_deterministic_and_safe() -> None:
    plan = make_plan()
    assert plan.to_dict() == {
        "respan_base_url": "https://api.respan.ai/api",
        "agent_model": "claude-sonnet-4-20250514",
        "max_budget_usd": 10.0,
        "routes": [route.to_dict() for route in plan.routes],
    }
    assert json.dumps(plan.to_dict(), sort_keys=True) == json.dumps(
        plan.to_dict(), sort_keys=True
    )


def test_plan_allows_at_most_fifteen_targets_plus_orchestration() -> None:
    targets = tuple(
        RouteRequirement(
            check_id=f"target-primary-{index:03d}",
            purpose=RoutePurpose.target,
            operation="openai.chat.completions",
            model=f"gpt-5-mini-{index}",
            provider="openai",
            funding=FundingRequirement.byok,
            required_credit_usd=0.0,
        )
        for index in range(1, 17)
    )
    assert len(make_plan(orchestration_route(), *targets[:15]).routes) == 16
    with pytest.raises(ValueError, match="between 1 and 16"):
        make_plan(orchestration_route(), *targets)


def test_success_uses_exact_endpoint_wire_body_headers_and_report_contract() -> None:
    plan = make_plan()
    client, transport, _ = make_client(response(ready_payload(plan)))

    report = client.check(plan)

    assert isinstance(client, GatewayReadinessBackend)
    assert report.passed is True
    assert report.approved_agent_model == "claude-sonnet-4-20250514"
    assert report.attempts == 1
    assert report.paid_canary_performed is False
    assert report.started_at.tzinfo is timezone.utc
    assert report.started_at <= report.finished_at
    assert [check.purpose for check in report.checks] == [
        RoutePurpose.orchestration,
        RoutePurpose.target,
    ]
    assert report.checks[0].check_id == "orchestration"
    assert report.checks[0].required_credit_usd == 10.0

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == GATEWAY_READINESS_URL
    assert request.connect_timeout + request.read_timeout <= 20.0
    assert request.max_response_bytes == 256 * 1024
    assert dict(request.headers) == {
        "Accept": "application/json",
        "Authorization": f"Bearer {FAKE_API_KEY}",
        "Connection": "close",
        "Content-Type": "application/json",
        "User-Agent": "respan-integration-agent/gateway-readiness-v1",
    }
    assert "Cookie" not in request.headers
    assert json.loads(request.body or b"") == {
        "schema_version": GATEWAY_READINESS_SCHEMA,
        "checks": [route.to_dict() for route in plan.routes],
    }
    assert "respan_base_url" not in json.loads(request.body or b"")
    assert "agent_model" not in json.loads(request.body or b"")
    assert "max_budget_usd" not in json.loads(request.body or b"")

    serialized = report.to_dict()
    assert serialized["schema_version"] == PREFLIGHT_REPORT_SCHEMA
    assert serialized["verdict"] == "PREFLIGHT_PASS"
    assert serialized["passed"] is True
    assert serialized["paid_canary_performed"] is False
    assert serialized["checks"][0]["check_id"] == "orchestration"
    assert serialized["checks"][0]["required_credit_usd"] == 10.0
    assert serialized["started_at"].endswith("Z")
    assert "key_ready" not in json.dumps(serialized)
    assert "reason_codes" not in json.dumps(serialized)
    assert FAKE_API_KEY not in json.dumps(serialized)


def test_response_order_is_normalized_to_plan_order() -> None:
    plan = make_plan()
    payload = ready_payload(plan)
    payload["checks"].reverse()
    client, _, _ = make_client(response(payload))

    report = client.check(plan)

    assert [check.purpose for check in report.checks] == [
        RoutePurpose.orchestration,
        RoutePurpose.target,
    ]


def test_orchestration_model_cannot_be_backend_substituted_but_target_alias_can() -> (
    None
):
    plan = make_plan()
    payload = ready_payload(plan)
    payload["checks"][0]["resolved_model"] = "claude-opus-4-1"
    client, _, _ = make_client(response(payload))
    with pytest.raises(PreflightSchemaError):
        client.check(plan)

    payload = ready_payload(plan)
    payload["checks"][1]["resolved_model"] = "openai/gpt-5-mini-2026-08-01"
    client, _, _ = make_client(response(payload))
    report = client.check(plan)
    assert report.approved_agent_model == plan.agent_model
    assert report.checks[1].resolved_model == "openai/gpt-5-mini-2026-08-01"


@pytest.mark.parametrize(
    ("funding", "source"),
    [
        (FundingRequirement.credits, "managed"),
        (FundingRequirement.byok, "customer"),
        (FundingRequirement.any, "managed"),
        (FundingRequirement.any, "customer"),
    ],
)
def test_funding_credential_semantics_accept_exact_supported_combinations(
    funding: FundingRequirement, source: str
) -> None:
    if funding is FundingRequirement.byok:
        route = target_route(funding=funding, credit=0.0)
        plan = make_plan(orchestration_route(), route)
        check_index = 1
    else:
        route = orchestration_route(funding=funding)
        plan = make_plan(route)
        check_index = 0
    payload = ready_payload(plan)
    payload["checks"][check_index]["credential_source"] = source
    client, _, _ = make_client(response(payload))

    assert client.check(plan).checks[check_index].credential_source == source


@pytest.mark.parametrize(
    ("funding", "source"),
    [
        (FundingRequirement.credits, "customer"),
        (FundingRequirement.credits, "default"),
        (FundingRequirement.byok, "managed"),
        (FundingRequirement.byok, "default"),
        (FundingRequirement.any, "default"),
    ],
)
def test_funding_credential_semantics_reject_mismatches(
    funding: FundingRequirement, source: str
) -> None:
    if funding is FundingRequirement.byok:
        route = target_route(funding=funding, credit=0.0)
        plan = make_plan(orchestration_route(), route)
        check_index = 1
    else:
        route = orchestration_route(funding=funding)
        plan = make_plan(route)
        check_index = 0
    payload = ready_payload(plan)
    payload["checks"][check_index]["credential_source"] = source
    client, _, _ = make_client(response(payload))

    with pytest.raises(PreflightSchemaError) as caught:
        client.check(plan)
    assert caught.value.code == "P_SCHEMA_UNSUPPORTED"


@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    [
        ("funding_satisfied", False, PreflightNotReadyError),
        ("key_ready", False, PreflightNotReadyError),
        ("limits_ready", False, PreflightNotReadyError),
        ("route_ready", False, PreflightNotReadyError),
        ("status", "not_ready", PreflightNotReadyError),
        ("reason_codes", ["provider.no_key"], PreflightNotReadyError),
        ("funding_satisfied", 1, PreflightSchemaError),
        ("key_ready", 1, PreflightSchemaError),
        ("limits_ready", None, PreflightSchemaError),
        ("route_ready", "true", PreflightSchemaError),
        ("status", "unknown", PreflightSchemaError),
        ("reason_codes", ["Unsafe reason with details"], PreflightSchemaError),
        ("resolved_model", "", PreflightSchemaError),
        ("resolved_model", "gpt//secret", PreflightSchemaError),
        ("resolved_model", "sk-" + "a" * 20, PreflightSchemaError),
        ("credential_source", "unknown", PreflightSchemaError),
    ],
)
def test_check_fails_closed_on_negative_or_ambiguous_core_fields(
    field: str, value: Any, error_type: type[PreflightError]
) -> None:
    plan = make_plan(orchestration_route())
    payload = ready_payload(plan)
    payload["checks"][0][field] = value
    client, transport, _ = make_client(response(payload))

    with pytest.raises(error_type):
        client.check(plan)
    assert len(transport.requests) == 1


def test_root_false_is_not_ready_but_non_boolean_is_schema_error() -> None:
    plan = make_plan(orchestration_route())
    payload = ready_payload(plan)
    payload["ready"] = False
    client, _, _ = make_client(response(payload))
    with pytest.raises(PreflightNotReadyError) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {"code": "P_NOT_READY", "attempts": 1}

    payload["ready"] = 1
    client, _, _ = make_client(response(payload))
    with pytest.raises(PreflightSchemaError):
        client.check(plan)


def test_negative_response_is_fully_validated_before_not_ready_is_raised() -> None:
    plan = make_plan()
    payload = ready_payload(plan)
    payload["ready"] = False
    payload["checks"][0].update(
        {
            "status": "not_ready",
            "key_ready": False,
            "reason_codes": ["provider.no_key"],
        }
    )
    del payload["checks"][1]["route_ready"]
    client, _, _ = make_client(response(payload))
    with pytest.raises(PreflightSchemaError):
        client.check(plan)

    payload = ready_payload(plan)
    payload["ready"] = False
    payload["checks"][0].update(
        {
            "status": "not_ready",
            "key_ready": False,
            "reason_codes": ["provider.no_key"],
        }
    )
    payload["checks"][1].update(
        {
            "status": "not_ready",
            "limits_ready": False,
            "reason_codes": ["route.limit_exceeded"],
        }
    )
    client, _, _ = make_client(response(payload))
    with pytest.raises(PreflightNotReadyError) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {
        "code": "P_NOT_READY",
        "attempts": 1,
        "reason_codes": ["provider.no_key", "route.limit_exceeded"],
    }


def test_negative_response_rejects_an_unbounded_aggregate_reason_set() -> None:
    plan = make_plan()
    payload = ready_payload(plan)
    payload["ready"] = False
    for check_index, check in enumerate(payload["checks"]):
        check.update(
            {
                "status": "not_ready",
                "route_ready": False,
                "reason_codes": [
                    f"route.group{check_index}.reason{reason_index}"
                    for reason_index in range(9)
                ],
            }
        )
    client, _, _ = make_client(response(payload))
    with pytest.raises(PreflightSchemaError):
        client.check(plan)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("check_id", "other-check"),
        ("check_id", "rk-" + "a" * 20),
        ("purpose", "target"),
        ("operation", "other.operation"),
        ("requested_model", "other-model"),
        ("provider", "openai"),
        ("funding_requested", "credits"),
        ("required_credit_usd", 9.99),
        ("required_credit_usd", True),
    ],
)
def test_exact_response_identity_is_required(field: str, value: Any) -> None:
    plan = make_plan(orchestration_route())
    payload = ready_payload(plan)
    payload["checks"][0][field] = value
    client, _, _ = make_client(response(payload))

    with pytest.raises(PreflightSchemaError):
        client.check(plan)


def test_missing_duplicate_extra_and_unknown_checks_are_rejected() -> None:
    plan = make_plan()
    variants: list[dict[str, Any]] = []

    missing = ready_payload(plan)
    missing["checks"].pop()
    variants.append(missing)

    duplicate = ready_payload(plan)
    duplicate["checks"][1] = deepcopy(duplicate["checks"][0])
    variants.append(duplicate)

    extra = ready_payload(plan)
    extra["checks"].append(deepcopy(extra["checks"][0]))
    variants.append(extra)

    unknown_field = ready_payload(plan)
    unknown_field["checks"][0]["backend_note"] = "ignored?"
    variants.append(unknown_field)

    missing_field = ready_payload(plan)
    del missing_field["checks"][0]["route_ready"]
    variants.append(missing_field)

    for payload in variants:
        client, _, _ = make_client(response(payload))
        with pytest.raises(PreflightSchemaError):
            client.check(plan)


def test_root_schema_and_additive_fields_are_rejected() -> None:
    plan = make_plan(orchestration_route())
    variants = []
    wrong_schema = ready_payload(plan)
    wrong_schema["schema_version"] = "respan.gateway-readiness/v2"
    variants.append(wrong_schema)
    extra = ready_payload(plan)
    extra["message"] = "ready"
    variants.append(extra)
    missing = ready_payload(plan)
    del missing["checks"]
    variants.append(missing)

    for payload in variants:
        client, _, _ = make_client(response(payload))
        with pytest.raises(PreflightSchemaError):
            client.check(plan)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"schema_version":"respan.gateway-readiness/v1",'
        b'"schema_version":"duplicate","ready":true,"checks":[]}',
        b'{"schema_version":"respan.gateway-readiness/v1","ready":NaN,"checks":[]}',
        b"\xff",
    ],
)
def test_malformed_duplicate_and_non_object_json_are_rejected(raw: bytes) -> None:
    plan = make_plan(orchestration_route())
    client, transport, _ = make_client(response(raw=raw))

    with pytest.raises(PreflightSchemaError):
        client.check(plan)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"content-type": "text/html"},
        {"content-type": "application/problem+json"},
        {},
    ],
)
def test_response_requires_application_json_content_type(
    headers: dict[str, str],
) -> None:
    plan = make_plan(orchestration_route())
    payload = ready_payload(plan)
    raw_response = platform_module._TransportResponse(
        200,
        headers,
        json.dumps(payload).encode("utf-8"),
    )
    client, _, _ = make_client(raw_response)
    with pytest.raises(PreflightSchemaError):
        client.check(plan)


@pytest.mark.parametrize(
    ("status", "error_type", "code"),
    [
        (301, PreflightRedirectError, "P_REDIRECT"),
        (307, PreflightRedirectError, "P_REDIRECT"),
        (401, PreflightAuthenticationError, "P_AUTH_INVALID"),
        (403, PreflightAuthorizationError, "P_AUTH_FORBIDDEN"),
        (400, PreflightRequestError, "P_REQUEST_REJECTED"),
        (404, PreflightRequestError, "P_REQUEST_REJECTED"),
        (408, PreflightRequestError, "P_REQUEST_REJECTED"),
        (501, PreflightRequestError, "P_REQUEST_REJECTED"),
    ],
)
def test_non_retriable_statuses_are_immediate_and_typed(
    status: int, error_type: type[PreflightError], code: str
) -> None:
    plan = make_plan(orchestration_route())
    client, transport, _ = make_client(response({}, status=status))

    with pytest.raises(error_type) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {
        "code": code,
        "status_code": status,
        "attempts": 1,
    }
    assert len(transport.requests) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_only_documented_statuses_retry_then_succeed(status: int) -> None:
    plan = make_plan(orchestration_route())
    headers = {"retry-after": "1.5"} if status == 429 else None
    client, transport, clock = make_client(
        response({}, status=status, headers=headers),
        response(ready_payload(plan)),
    )

    report = client.check(plan)

    assert report.attempts == 2
    assert all(check.attempts == 2 for check in report.checks)
    assert len(transport.requests) == 2
    assert clock.sleeps == ([1.5] if status == 429 else [0.25])


def test_network_errors_retry_but_unknown_injected_exceptions_do_not() -> None:
    plan = make_plan(orchestration_route())
    client, transport, clock = make_client(
        TimeoutError("secret response body"),
        OSError("network"),
        response(ready_payload(plan)),
    )
    report = client.check(plan)
    assert report.attempts == 3
    assert clock.sleeps == [0.25, 0.5]
    assert len(transport.requests) == 3

    client, transport, clock = make_client(
        ValueError("not a network exception"),
        response(ready_payload(plan)),
    )
    with pytest.raises(PreflightTransportError):
        client.check(plan)
    assert len(transport.requests) == 1
    assert clock.sleeps == []


def test_injected_sleeper_failure_is_redacted() -> None:
    plan = make_plan(orchestration_route())
    clock = FakeClock()
    transport = FakeTransport([response({}, status=503)])

    def hostile_sleeper(_seconds: float) -> None:
        raise ValueError("secret-response-body")

    client = RespanGatewayReadinessClient(
        FAKE_API_KEY,
        transport=transport,
        clock=clock.monotonic,
        wall_clock=clock.wall,
        sleeper=hostile_sleeper,
    )
    with pytest.raises(PreflightTransportError) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {"code": "P_TRANSPORT", "attempts": 1}
    assert "secret-response-body" not in repr(caught.value)


def test_retry_after_is_bounded_and_exhaustion_remains_rate_limit_error() -> None:
    plan = make_plan(orchestration_route())
    client, transport, clock = make_client(
        response({}, status=429, headers={"retry-after": "600"}),
        response({}, status=429, headers={"retry-after": "600"}),
        response({}, status=429, headers={"retry-after": "600"}),
    )

    with pytest.raises(PreflightRateLimitError) as caught:
        client.check(plan)

    assert caught.value.as_dict() == {
        "code": "P_RATE_LIMITED",
        "status_code": 429,
        "attempts": 3,
        "retry_after_seconds": 5.0,
    }
    assert clock.sleeps == [5.0, 5.0]
    assert len(transport.requests) == 3


def test_invalid_retry_after_uses_bounded_backoff() -> None:
    plan = make_plan(orchestration_route())
    client, _, clock = make_client(
        response({}, status=429, headers={"retry-after": "not-a-date"}),
        response(ready_payload(plan)),
    )

    assert client.check(plan).attempts == 2
    assert clock.sleeps == [0.25]


def test_retriable_server_error_exhaustion_is_transport_error() -> None:
    plan = make_plan(orchestration_route())
    client, transport, clock = make_client(
        response({}, status=503),
        response({}, status=503),
        response({}, status=503),
    )

    with pytest.raises(PreflightTransportError) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {
        "code": "P_TRANSPORT",
        "status_code": 503,
        "attempts": 3,
    }
    assert len(transport.requests) == 3
    assert clock.sleeps == [0.25, 0.5]


def test_shared_deadline_bounds_attempt_timeouts_and_stops_retry() -> None:
    plan = make_plan(orchestration_route())
    clock = FakeClock()

    def slow_failure(request: platform_module._TransportRequest) -> Any:
        assert request.connect_timeout + request.read_timeout <= 1.0
        clock.advance(0.9)
        raise TimeoutError

    client, transport, _ = make_client(
        slow_failure,
        response(ready_payload(plan)),
        clock=clock,
        deadline_seconds=1.0,
    )

    with pytest.raises(PreflightDeadlineError) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {"code": "P_TIMEOUT", "attempts": 1}
    assert len(transport.requests) == 1
    assert clock.sleeps == []


def test_transport_success_after_shared_deadline_is_timeout() -> None:
    plan = make_plan(orchestration_route())
    clock = FakeClock()

    def late_success(
        _request: platform_module._TransportRequest,
    ) -> platform_module._TransportResponse:
        clock.advance(1.01)
        return response(ready_payload(plan))

    client, transport, _ = make_client(
        late_success,
        clock=clock,
        max_attempts=1,
        deadline_seconds=1.0,
    )
    with pytest.raises(PreflightDeadlineError) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {"code": "P_TIMEOUT", "attempts": 1}
    assert len(transport.requests) == 1


def test_transport_failure_after_shared_deadline_on_final_attempt_is_timeout() -> None:
    plan = make_plan(orchestration_route())
    clock = FakeClock()

    def late_failure(_request: platform_module._TransportRequest) -> Any:
        clock.advance(1.01)
        raise TimeoutError

    client, transport, _ = make_client(
        late_failure,
        clock=clock,
        max_attempts=1,
        deadline_seconds=1.0,
    )
    with pytest.raises(PreflightDeadlineError) as caught:
        client.check(plan)
    assert caught.value.as_dict() == {"code": "P_TIMEOUT", "attempts": 1}
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"connect_timeout_seconds": 0},
        {"read_timeout_seconds": float("inf")},
        {"max_response_bytes": 0},
        {"max_response_bytes": 1024 * 1024 + 1},
        {"max_attempts": 0},
        {"max_attempts": 4},
        {"deadline_seconds": 0},
        {"deadline_seconds": 20.01},
    ],
)
def test_client_rejects_unbounded_or_invalid_limits(kwargs: dict[str, Any]) -> None:
    with pytest.raises(PreflightConfigurationError) as caught:
        RespanGatewayReadinessClient(FAKE_API_KEY, **kwargs)
    assert caught.value.as_dict() == {"code": "P_CONFIG_INVALID"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status_code": True},
        {"status_code": 99},
        {"status_code": 600},
        {"status_code": "https://secret.example"},
        {"attempts": True},
        {"attempts": -1},
        {"attempts": 4},
        {"attempts": "secret-response-body"},
        {"retry_after_seconds": True},
        {"retry_after_seconds": float("nan")},
        {"retry_after_seconds": -0.1},
        {"retry_after_seconds": 5.01},
        {"retry_after_seconds": "https://secret.example"},
    ],
)
def test_exported_error_constructor_rejects_hostile_metadata(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        PreflightRateLimitError(**kwargs)


@pytest.mark.parametrize(
    "reason_codes",
    [
        ["provider.no_key"],
        ("provider.no_key", "provider.no_key"),
        ("https://secret.example",),
        ("Unsafe body text",),
        ("rk-" + "a" * 20,),
        tuple(f"provider.reason-{index}" for index in range(17)),
        (["unhashable"],),
    ],
)
def test_not_ready_error_rejects_hostile_reason_codes(reason_codes: Any) -> None:
    with pytest.raises(ValueError):
        PreflightNotReadyError(reason_codes=reason_codes)


def test_valid_error_metadata_is_bounded_and_safe_to_serialize() -> None:
    error = PreflightRateLimitError(
        status_code=429,
        attempts=3,
        retry_after_seconds=5,
    )
    assert error.as_dict() == {
        "code": "P_RATE_LIMITED",
        "status_code": 429,
        "attempts": 3,
        "retry_after_seconds": 5.0,
    }
    not_ready = PreflightNotReadyError(
        reason_codes=("provider.no_key",),
        attempts=1,
    )
    assert not_ready.as_dict()["reason_codes"] == ["provider.no_key"]

    error.status_code = "https://secret.example"  # type: ignore[assignment]
    error.attempts = "secret-response-body"  # type: ignore[assignment]
    error.retry_after_seconds = float("nan")
    assert error.as_dict() == {"code": "P_RATE_LIMITED"}
    not_ready.reason_codes = ("rk-" + "a" * 20,)
    assert "reason_codes" not in not_ready.as_dict()


@pytest.mark.parametrize(
    "api_key",
    ["", "short", "contains whitespace", "line\nbreak", "\x00invalid-key"],
)
def test_api_key_validation_is_safe(api_key: str) -> None:
    with pytest.raises(PreflightConfigurationError) as caught:
        RespanGatewayReadinessClient(api_key)
    if api_key:
        assert api_key not in repr(caught.value)
        assert api_key not in json.dumps(caught.value.as_dict())


def test_response_size_and_malformed_transport_values_fail_closed() -> None:
    plan = make_plan(orchestration_route())
    oversized = platform_module._TransportResponse(
        200,
        {"content-type": "application/json"},
        b"x" * 65,
    )
    client, transport, _ = make_client(oversized, max_response_bytes=64)
    with pytest.raises(PreflightResponseTooLargeError) as caught:
        client.check(plan)
    assert caught.value.code == "P_RESPONSE_TOO_LARGE"
    assert len(transport.requests) == 1

    client, transport, _ = make_client(
        platform_module.BackendTransportError("http_response"),
        response(ready_payload(plan)),
    )
    with pytest.raises(PreflightResponseTooLargeError):
        client.check(plan)
    assert len(transport.requests) == 1

    for malformed in [
        object(),
        platform_module._TransportResponse(True, {}, b"{}"),
        platform_module._TransportResponse(200, {1: "bad"}, b"{}"),  # type: ignore[dict-item]
        platform_module._TransportResponse(200, {"x": 1}, b"{}"),  # type: ignore[dict-item]
        platform_module._TransportResponse(200, {}, "{}"),  # type: ignore[arg-type]
    ]:
        client, _, _ = make_client(malformed)
        with pytest.raises(PreflightTransportError):
            client.check(plan)


def test_error_serialization_never_contains_key_body_or_urls() -> None:
    plan = make_plan(orchestration_route())
    secret_body = "secret-response-body-with-token"
    client, _, _ = make_client(response(raw=secret_body.encode("utf-8"), status=400))
    with pytest.raises(PreflightRequestError) as caught:
        client.check(plan)

    serialized = json.dumps(caught.value.as_dict())
    rendered = repr(caught.value)
    for forbidden in (FAKE_API_KEY, secret_body, GATEWAY_READINESS_URL, "https://"):
        assert forbidden not in serialized
        assert forbidden not in rendered


def test_manual_check_and_report_enforce_funding_time_and_attempt_invariants() -> None:
    with pytest.raises(ValueError, match="does not satisfy funding"):
        PreflightCheck(
            check_id="orchestration",
            purpose=RoutePurpose.orchestration,
            operation="anthropic.messages",
            requested_model="model-a",
            resolved_model="model-a",
            provider="anthropic",
            funding=FundingRequirement.credits,
            required_credit_usd=1.0,
            credential_source="customer",
            attempts=1,
        )

    check = PreflightCheck(
        check_id="orchestration",
        purpose=RoutePurpose.orchestration,
        operation="anthropic.messages",
        requested_model="model-a",
        resolved_model="model-a",
        provider="anthropic",
        funding=FundingRequirement.credits,
        required_credit_usd=1.0,
        credential_source="managed",
        attempts=1,
    )
    start = datetime(2026, 8, 23, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="cannot precede"):
        PreflightReport(start, datetime(2026, 8, 22, tzinfo=timezone.utc), (check,), 1)
    with pytest.raises(ValueError, match="aware UTC"):
        PreflightReport(start.replace(tzinfo=None), start, (check,), 1)
    with pytest.raises(ValueError, match="attempts"):
        PreflightReport(start, start, (check,), 2)
    with pytest.raises(ValueError, match="paid canary"):
        PreflightReport(start, start, (check,), 1, paid_canary_performed=True)
