from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from respan_integration_agent import GatewayOperation, GatewayRoute
from respan_integration_agent.agent import _build_prompt
from respan_integration_agent.config import GatewayConfig, OnboardingRequest


def _route(
    *,
    operation: str = "openai.chat.completions",
    provider: str = "openai",
    model: str = "gpt-4o-mini",
) -> dict[str, str]:
    return {
        "operation": operation,
        "provider": provider,
        "model": model,
    }


def _gateway(
    *,
    funding: str = "credits",
    required_credit_usd: float = 1.0,
    routes: list[dict[str, str]] | None = None,
    fallback_routes: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "funding": funding,
        "routes": routes if routes is not None else [_route()],
        "enable_caching": False,
        "fallback_routes": fallback_routes or [],
        "required_credit_usd": required_credit_usd,
    }


def _request(
    *,
    product: str = "gateway",
    gateway: dict[str, object] | None = None,
    tracing: dict[str, object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "repo_url": "/tmp/fixture",
        "product": product,
    }
    if gateway is not None:
        result["gateway"] = gateway
    if tracing is not None:
        result["tracing"] = tracing
    return result


def test_unknown_config_field_is_rejected():
    with pytest.raises(ValidationError):
        OnboardingRequest.model_validate(
            {
                "repo_url": "/tmp/fixture",
                "product": "tracing",
                "trcaing": {"mode": "auto"},
            }
        )


def test_gateway_contract_preserves_exact_ordered_routes():
    config = GatewayConfig.model_validate(
        _gateway(
            routes=[
                _route(),
                _route(
                    operation="anthropic.messages",
                    provider="anthropic",
                    model="claude-sonnet-4-5-20250929",
                ),
            ],
            fallback_routes=[
                _route(provider="azure-openai", model="openai/gpt-4.1-mini"),
                _route(
                    operation="anthropic.messages",
                    provider="anthropic",
                    model="claude-haiku-4-5-20251001",
                ),
            ],
        )
    )

    assert isinstance(config.routes, tuple)
    assert isinstance(config.fallback_routes, tuple)
    assert config.routes[0].identity == (
        GatewayOperation.openai_chat_completions,
        "openai",
        "gpt-4o-mini",
    )
    assert [route.model for route in config.fallback_routes] == [
        "openai/gpt-4.1-mini",
        "claude-haiku-4-5-20251001",
    ]
    with pytest.raises(ValidationError, match="frozen"):
        config.routes[0].model = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("old_field", "value"),
    [
        ("providers", ["openai"]),
        ("enable_fallbacks", True),
    ],
)
def test_ambiguous_legacy_gateway_fields_are_rejected(old_field, value):
    gateway = _gateway()
    gateway[old_field] = value

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GatewayConfig.model_validate(gateway)


def test_gateway_routes_must_be_nonempty():
    with pytest.raises(ValidationError, match="at least 1 item"):
        GatewayConfig.model_validate(_gateway(routes=[]))


def test_gateway_total_route_count_matches_preflight_capacity():
    routes = [_route(model=f"model-primary-{index}") for index in range(8)]
    fallbacks = [_route(model=f"model-fallback-{index}") for index in range(8)]

    with pytest.raises(ValidationError, match="at most 15 total target routes"):
        GatewayConfig.model_validate(
            _gateway(routes=routes, fallback_routes=fallbacks)
        )


@pytest.mark.parametrize(
    ("routes", "fallback_routes", "message"),
    [
        ([_route(), _route()], [], "primary routes must be unique"),
        (
            [_route()],
            [
                _route(provider="anthropic", model="claude-3-5-haiku"),
                _route(provider="anthropic", model="claude-3-5-haiku"),
            ],
            "fallback routes must be unique",
        ),
        ([_route()], [_route()], "primary and fallback routes must not overlap"),
    ],
)
def test_duplicate_route_identities_are_rejected(routes, fallback_routes, message):
    with pytest.raises(ValidationError, match=message):
        GatewayConfig.model_validate(
            _gateway(routes=routes, fallback_routes=fallback_routes)
        )


def test_fallback_operation_must_have_a_primary_route():
    with pytest.raises(ValidationError, match="match a configured primary operation"):
        GatewayConfig.model_validate(
            _gateway(
                fallback_routes=[
                    _route(
                        operation="anthropic.messages",
                        provider="anthropic",
                        model="claude-haiku-4-5-20251001",
                    )
                ]
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", ""),
        ("provider", "Open AI"),
        ("provider", "https://provider.invalid"),
        ("model", ""),
        ("model", "../secret"),
        ("model", "https://model.invalid"),
        ("model", "sk-abcdefghijklmnopqrstuvwxyz123456"),
    ],
)
def test_gateway_route_rejects_unsafe_identifiers(field, value):
    route = _route()
    route[field] = value
    with pytest.raises(ValidationError):
        GatewayRoute.model_validate(route)


def test_gateway_operation_is_closed_and_anthropic_provider_is_exact():
    with pytest.raises(ValidationError):
        GatewayRoute.model_validate(_route(operation="openai.responses"))
    with pytest.raises(ValidationError, match="require provider='anthropic'"):
        GatewayRoute.model_validate(
            _route(operation="anthropic.messages", provider="openai")
        )


@pytest.mark.parametrize("amount", [0.0, -0.01, 100.01, float("inf"), float("nan")])
def test_credits_require_a_bounded_positive_reserve(amount):
    with pytest.raises(ValidationError):
        GatewayConfig.model_validate(_gateway(required_credit_usd=amount))


def test_byok_requires_zero_respan_credit_reserve():
    config = GatewayConfig.model_validate(
        _gateway(funding="byok", required_credit_usd=0.0)
    )
    assert config.required_credit_usd == 0

    with pytest.raises(ValidationError, match="BYOK funding requires"):
        GatewayConfig.model_validate(
            _gateway(funding="byok", required_credit_usd=0.01)
        )


def test_required_credit_reserve_is_explicit():
    gateway = _gateway()
    del gateway["required_credit_usd"]
    with pytest.raises(ValidationError, match="Field required"):
        GatewayConfig.model_validate(gateway)


def test_product_matrix_rejects_missing_or_irrelevant_sections():
    tracing = OnboardingRequest.model_validate(_request(product="tracing"))
    assert tracing.tracing is not None
    assert tracing.gateway is None

    gateway = OnboardingRequest.model_validate(
        _request(product="gateway", gateway=_gateway())
    )
    assert gateway.tracing is None
    assert gateway.gateway is not None

    both = OnboardingRequest.model_validate(
        _request(product="both", gateway=_gateway())
    )
    assert both.tracing is not None
    assert both.gateway is not None

    with pytest.raises(ValidationError, match="requires a GatewayConfig"):
        OnboardingRequest.model_validate(_request(product="gateway"))
    with pytest.raises(ValidationError, match="requires a GatewayConfig"):
        OnboardingRequest.model_validate(_request(product="both"))
    with pytest.raises(ValidationError, match="not allowed for tracing-only"):
        OnboardingRequest.model_validate(
            _request(product="tracing", gateway=_gateway())
        )
    with pytest.raises(ValidationError, match="not allowed for gateway-only"):
        OnboardingRequest.model_validate(
            _request(product="gateway", gateway=_gateway(), tracing={"mode": "auto"})
        )


def test_gateway_prompt_renders_only_exact_sanitized_contract():
    req = OnboardingRequest.model_validate(
        _request(
            gateway=_gateway(
                routes=[_route()],
                fallback_routes=[
                    _route(provider="anthropic", model="claude-3-5-haiku")
                ],
            )
        )
    )
    skill = SimpleNamespace(
        tracing_reference=Path("/skill/references/tracing.md"),
        gateway_reference=Path("/skill/references/gateway.md"),
    )

    prompt = _build_prompt(req, skill=skill, run_id="respan-v0-gateway-test")

    assert "funding=credits, required_credit_usd=1, caching=False" in prompt
    assert (
        "Exact primary routes, in order: "
        "openai.chat.completions[provider=openai,model=gpt-4o-mini]" in prompt
    )
    assert (
        "Exact fallback routes, in order: "
        "openai.chat.completions[provider=anthropic,model=claude-3-5-haiku]"
        in prompt
    )
    assert "providers=" not in prompt
    assert "enable_fallbacks" not in prompt


def test_smoke_profile_requires_tracing_auto():
    with pytest.raises(ValidationError, match="requires product=tracing and mode=auto"):
        OnboardingRequest.model_validate(
            {
                **_request(product="gateway", gateway=_gateway()),
                "verification": {
                    "profile": "python-openai-auto-smoke",
                    "respan_ai_version": "4.1.0",
                    "openai_otel_version": "0.62.3",
                },
            }
        )
