"""The onboarding config contract.

This is what the questionnaire produces and what the agent implements against — so the
agent never has to guess mid-run. A pre-scan of the repo pre-fills the defaults
(detected language, LLM libraries, frameworks) so the user confirms rather than fills.

Mirrors the SDK's own "Auto vs Full" tracing decision and the gateway credits/BYOK prep.
"""

from __future__ import annotations

from enum import Enum
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Product(str, Enum):
    tracing = "tracing"
    gateway = "gateway"
    both = "both"


# ── Tracing ──────────────────────────────────────────────────────────────────
class TracingMode(str, Enum):
    #: Just `Respan()` — every LLM call captured as a flat span. No decorators,
    #: no framework instrumentor, even if a framework is detected.
    auto = "auto"
    #: Explicit framework instrumentor and/or @workflow/@task decorators.
    full = "full"


class Endpoint(str, Enum):
    platform = "platform"  # https://api.respan.ai
    enterprise = "enterprise"


class TracingConfig(StrictModel):
    mode: TracingMode = TracingMode.auto
    #: Full only — add @workflow/@task decorators for nested structure.
    use_decorators: bool = False
    #: Full only — framework instrumentor to use (auto-detected; e.g.
    #: "respan-instrumentation-langchain"). None = direct-SDK auto only.
    framework_instrumentor: Optional[str] = None
    #: Full + decorators — which workflows to wrap (function names). Empty = agent decides.
    workflows: list[str] = Field(default_factory=list)
    environment: Optional[str] = None  # e.g. "production"
    service_name: Optional[str] = None
    endpoint: Endpoint = Endpoint.platform


# ── Gateway ──────────────────────────────────────────────────────────────────
class GatewayFunding(str, Enum):
    #: Managed provider keys — user adds credits to their Respan account.
    credits = "credits"
    #: Bring your own provider key(s); the gateway proxies them.
    byok = "byok"


class GatewayOperation(str, Enum):
    """Completion operations the v0 target-gateway path can configure exactly."""

    openai_chat_completions = "openai.chat.completions"
    anthropic_messages = "anthropic.messages"


_GATEWAY_PROVIDER_PATTERN = r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
_GATEWAY_MODEL_PATTERN = (
    r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,126}[A-Za-z0-9])?$"
)
_CREDENTIAL_LIKE_ROUTE_VALUE = re.compile(
    r"(?i)^(?:(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})$"
)


class GatewayRoute(StrictModel):
    """One exact provider/model route, with no credentials or free-form URL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: GatewayOperation
    provider: str = Field(
        min_length=1,
        max_length=64,
        pattern=_GATEWAY_PROVIDER_PATTERN,
    )
    model: str = Field(
        min_length=1,
        max_length=128,
        pattern=_GATEWAY_MODEL_PATTERN,
    )

    @model_validator(mode="after")
    def _validate_route(self) -> "GatewayRoute":
        if self.operation is GatewayOperation.anthropic_messages and (
            self.provider != "anthropic"
        ):
            raise ValueError(
                "anthropic.messages routes require provider='anthropic'"
            )
        if ".." in self.model or "//" in self.model or "://" in self.model:
            raise ValueError("gateway model must be a safe model identifier")
        if _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(self.provider) or (
            _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(self.model)
        ):
            raise ValueError("gateway routes must not contain credential-like values")
        return self

    @property
    def identity(self) -> tuple[GatewayOperation, str, str]:
        return (self.operation, self.provider, self.model)


class GatewayConfig(StrictModel):
    """Exact target routes and the funding readiness required before onboarding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    funding: GatewayFunding
    routes: tuple[GatewayRoute, ...] = Field(min_length=1, max_length=15)
    enable_caching: bool = False
    fallback_routes: tuple[GatewayRoute, ...] = Field(
        default=(),
        max_length=14,
    )
    # Explicit for both modes: credits require a positive bounded reserve, while
    # BYOK uses the provider account and therefore requires exactly zero Respan credits.
    required_credit_usd: float = Field(
        ge=0.0,
        le=100.0,
        allow_inf_nan=False,
        strict=True,
    )

    @model_validator(mode="after")
    def _validate_gateway_contract(self) -> "GatewayConfig":
        if self.funding is GatewayFunding.credits and self.required_credit_usd <= 0:
            raise ValueError("credits funding requires required_credit_usd > 0")
        if self.funding is GatewayFunding.byok and self.required_credit_usd != 0:
            raise ValueError("BYOK funding requires required_credit_usd = 0")
        if len(self.routes) + len(self.fallback_routes) > 15:
            raise ValueError("gateway supports at most 15 total target routes in v0")

        primary_identities = [route.identity for route in self.routes]
        fallback_identities = [route.identity for route in self.fallback_routes]
        if len(primary_identities) != len(set(primary_identities)):
            raise ValueError("gateway primary routes must be unique")
        if len(fallback_identities) != len(set(fallback_identities)):
            raise ValueError("gateway fallback routes must be unique")
        if set(primary_identities) & set(fallback_identities):
            raise ValueError("gateway primary and fallback routes must not overlap")

        primary_operations = {route.operation for route in self.routes}
        if any(
            route.operation not in primary_operations for route in self.fallback_routes
        ):
            raise ValueError(
                "each fallback operation must match a configured primary operation"
            )
        return self


class VerificationProfile(str, Enum):
    python_openai_auto_smoke = "python-openai-auto-smoke"


class VerificationConfig(StrictModel):
    """Optional deterministic gate used by the trusted v0a smoke fixture."""

    profile: VerificationProfile
    respan_ai_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    openai_otel_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


# ── The request ──────────────────────────────────────────────────────────────
class OnboardingRequest(StrictModel):
    repo_url: str
    base_branch: str = "main"
    product: Product
    tracing: Optional[TracingConfig] = None
    gateway: Optional[GatewayConfig] = None
    verification: Optional[VerificationConfig] = None

    @model_validator(mode="after")
    def _require_matching_sections(self) -> "OnboardingRequest":
        needs_tracing = self.product in (Product.tracing, Product.both)
        needs_gateway = self.product in (Product.gateway, Product.both)
        if not needs_tracing and self.tracing is not None:
            raise ValueError("tracing config is not allowed for gateway-only onboarding")
        if not needs_gateway and self.gateway is not None:
            raise ValueError("gateway config is not allowed for tracing-only onboarding")
        if needs_tracing and self.tracing is None:
            self.tracing = TracingConfig()  # sensible default = Auto
        if needs_gateway and self.gateway is None:
            raise ValueError(
                "gateway onboarding requires a GatewayConfig (funding is a required prep step)"
            )
        if self.verification is not None:
            is_python_auto_smoke = (
                self.verification.profile
                is VerificationProfile.python_openai_auto_smoke
            )
            if is_python_auto_smoke and not (
                self.product is Product.tracing
                and self.tracing is not None
                and self.tracing.mode is TracingMode.auto
            ):
                raise ValueError(
                    "python-openai-auto-smoke verification requires product=tracing and mode=auto"
                )
        return self
