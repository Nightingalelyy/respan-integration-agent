"""Fail-closed client for the Respan gateway-readiness contract.

This module intentionally does not perform a model inference.  It asks the
official Respan API to evaluate the same route, credential, funding, and limit
decisions that a gateway request would use.  Only a completely ready response
is returned to callers; malformed or negative responses become safe, typed
errors.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from typing import Callable, ClassVar, Protocol, runtime_checkable

from . import platform as _platform


OFFICIAL_RESPAN_API_ORIGIN = "https://api.respan.ai"
OFFICIAL_RESPAN_API_BASE = f"{OFFICIAL_RESPAN_API_ORIGIN}/api"
GATEWAY_READINESS_URL = f"{OFFICIAL_RESPAN_API_ORIGIN}/api/gateway/readiness/"
GATEWAY_READINESS_SCHEMA = "respan.gateway-readiness/v1"
PREFLIGHT_REPORT_SCHEMA = "respan-integration-agent-preflight/v1"

_MAX_ATTEMPTS = 3
_MAX_DEADLINE_SECONDS = 20.0
_MAX_RETRY_AFTER_SECONDS = 5.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_RETRIABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_OPERATION_VALUES = frozenset({"openai.chat.completions", "anthropic.messages"})
_MODEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,126}[A-Za-z0-9])?$")
_PROVIDER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_API_KEY_RE = re.compile(r"^[\x21-\x7e]{8,4096}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_CREDENTIAL_LIKE_ROUTE_VALUE = re.compile(
    r"^(?:(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})$",
    re.IGNORECASE,
)


class FundingRequirement(str, Enum):
    """How a route is required to be funded."""

    any = "any"
    credits = "credits"
    byok = "byok"


class RoutePurpose(str, Enum):
    """The role a gateway route plays in the integration agent."""

    orchestration = "orchestration"
    target = "target"


def _require_enum(value: object, enum_type: type[Enum], field: str) -> None:
    if not isinstance(value, enum_type):
        raise ValueError(f"{field} must be a {enum_type.__name__}")


def _require_matching_string(
    value: object, pattern: re.Pattern[str], field: str
) -> None:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field} has an invalid value")


def _require_check_id(value: object) -> None:
    _require_matching_string(value, _CHECK_ID_RE, "check_id")
    if _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(value) is not None:
        raise ValueError("check_id has an invalid value")


def _require_model(value: object, field: str) -> None:
    _require_matching_string(value, _MODEL_RE, field)
    if (
        "://" in value
        or ".." in value
        or "//" in value
        or _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(value) is not None
    ):
        raise ValueError(f"{field} has an invalid value")


def _require_provider(value: object, field: str) -> None:
    _require_matching_string(value, _PROVIDER_RE, field)
    if _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(value) is not None:
        raise ValueError(f"{field} has an invalid value")


def _require_operation(value: object, field: str) -> None:
    if not isinstance(value, str) or value not in _OPERATION_VALUES:
        raise ValueError(f"{field} has an invalid value")


def _require_money(value: object, field: str, *, positive: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    numeric = float(value)
    minimum = 0.0 if not positive else 0.000001
    if not math.isfinite(numeric) or numeric < minimum or numeric > 10_000.0:
        raise ValueError(f"{field} is outside the supported range")


def _require_funding_credit(
    funding: FundingRequirement,
    required_credit_usd: float,
) -> None:
    credit = float(required_credit_usd)
    if funding is FundingRequirement.credits and credit <= 0:
        raise ValueError("credits funding requires required_credit_usd > 0")
    if funding is FundingRequirement.byok and credit != 0:
        raise ValueError("BYOK funding requires required_credit_usd = 0")


@dataclass(frozen=True)
class RouteRequirement:
    """One route whose backend readiness must be established."""

    check_id: str
    purpose: RoutePurpose
    operation: str
    model: str
    provider: str
    funding: FundingRequirement
    required_credit_usd: float

    def __post_init__(self) -> None:
        _require_check_id(self.check_id)
        _require_enum(self.purpose, RoutePurpose, "purpose")
        _require_operation(self.operation, "operation")
        _require_model(self.model, "model")
        _require_provider(self.provider, "provider")
        if self.operation == "anthropic.messages" and self.provider != "anthropic":
            raise ValueError("anthropic.messages routes require provider='anthropic'")
        _require_enum(self.funding, FundingRequirement, "funding")
        _require_money(self.required_credit_usd, "required_credit_usd")
        _require_funding_credit(self.funding, self.required_credit_usd)

    @property
    def identity(self) -> tuple[str, str, str, str, str, str, float]:
        """Return the complete identity the backend must echo."""

        return (
            self.check_id,
            self.purpose.value,
            self.operation,
            self.model,
            self.provider,
            self.funding.value,
            float(self.required_credit_usd),
        )

    @property
    def business_identity(self) -> tuple[str, str, str, str, str]:
        """Return the route identity independently of its local check ID."""

        return self.identity[1:-1]

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "purpose": self.purpose.value,
            "operation": self.operation,
            "model": self.model,
            "provider": self.provider,
            "funding": self.funding.value,
            "required_credit_usd": float(self.required_credit_usd),
        }


@dataclass(frozen=True)
class PreflightPlan:
    """A complete, immutable gateway-readiness request."""

    respan_base_url: str
    agent_model: str
    max_budget_usd: float
    routes: tuple[RouteRequirement, ...]

    def __post_init__(self) -> None:
        if self.respan_base_url != OFFICIAL_RESPAN_API_BASE:
            raise ValueError(
                "respan_base_url must be the official Respan execution API base"
            )
        _require_model(self.agent_model, "agent_model")
        _require_money(self.max_budget_usd, "max_budget_usd", positive=True)
        if not isinstance(self.routes, tuple):
            raise ValueError("routes must be a tuple")
        if not 1 <= len(self.routes) <= 16:
            raise ValueError("routes must contain between 1 and 16 requirements")
        if any(not isinstance(route, RouteRequirement) for route in self.routes):
            raise ValueError("routes must contain RouteRequirement values")
        identities = [route.identity for route in self.routes]
        business_identities = [route.business_identity for route in self.routes]
        check_ids = [route.check_id for route in self.routes]
        if len(set(check_ids)) != len(check_ids):
            raise ValueError("routes must have unique check_id values")
        if len(set(identities)) != len(identities):
            raise ValueError("routes must have unique response identities")
        if len(set(business_identities)) != len(business_identities):
            raise ValueError("routes must have unique business requirements")
        orchestration = [
            route
            for route in self.routes
            if route.purpose is RoutePurpose.orchestration
        ]
        if len(orchestration) != 1:
            raise ValueError(
                "routes must contain exactly one orchestration requirement"
            )
        if (
            orchestration[0].operation != "anthropic.messages"
            or orchestration[0].provider != "anthropic"
        ):
            raise ValueError("orchestration must use the Anthropic messages route")
        if orchestration[0].model != self.agent_model:
            raise ValueError("agent_model must match the orchestration route model")
        if float(orchestration[0].required_credit_usd) != float(self.max_budget_usd):
            raise ValueError(
                "orchestration credit requirement must equal max_budget_usd"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "respan_base_url": self.respan_base_url,
            "agent_model": self.agent_model,
            "max_budget_usd": float(self.max_budget_usd),
            "routes": [route.to_dict() for route in self.routes],
        }


@dataclass(frozen=True)
class PreflightCheck:
    """A backend-approved route, reduced to safe result fields."""

    check_id: str
    purpose: RoutePurpose
    operation: str
    requested_model: str
    resolved_model: str
    provider: str
    funding: FundingRequirement
    required_credit_usd: float
    credential_source: str
    attempts: int

    def __post_init__(self) -> None:
        _require_check_id(self.check_id)
        _require_enum(self.purpose, RoutePurpose, "purpose")
        _require_operation(self.operation, "operation")
        _require_model(self.requested_model, "requested_model")
        _require_model(self.resolved_model, "resolved_model")
        _require_provider(self.provider, "provider")
        if self.operation == "anthropic.messages" and self.provider != "anthropic":
            raise ValueError("anthropic.messages routes require provider='anthropic'")
        _require_enum(self.funding, FundingRequirement, "funding")
        _require_money(self.required_credit_usd, "required_credit_usd")
        _require_funding_credit(self.funding, self.required_credit_usd)
        if (
            self.purpose is RoutePurpose.orchestration
            and self.resolved_model != self.requested_model
        ):
            raise ValueError("orchestration resolved_model must equal requested_model")
        if self.credential_source not in {"managed", "customer"}:
            raise ValueError("credential_source has an invalid value")
        expected_sources = {
            FundingRequirement.credits: {"managed"},
            FundingRequirement.byok: {"customer"},
            FundingRequirement.any: {"managed", "customer"},
        }[self.funding]
        if self.credential_source not in expected_sources:
            raise ValueError("credential_source does not satisfy funding")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise ValueError("attempts must be an integer")
        if not 1 <= self.attempts <= _MAX_ATTEMPTS:
            raise ValueError("attempts is outside the supported range")

    def to_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "purpose": self.purpose.value,
            "operation": self.operation,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "provider": self.provider,
            "funding": self.funding.value,
            "required_credit_usd": float(self.required_credit_usd),
            "credential_source": self.credential_source,
            "attempts": self.attempts,
        }


@dataclass(frozen=True)
class PreflightReport:
    """A fully successful readiness evaluation."""

    started_at: datetime
    finished_at: datetime
    checks: tuple[PreflightCheck, ...]
    attempts: int
    paid_canary_performed: bool = False

    def __post_init__(self) -> None:
        started = _validate_timestamp(self.started_at, "started_at")
        finished = _validate_timestamp(self.finished_at, "finished_at")
        if finished < started:
            raise ValueError("finished_at cannot precede started_at")
        if not isinstance(self.checks, tuple) or not self.checks:
            raise ValueError("checks must be a non-empty tuple")
        if any(not isinstance(check, PreflightCheck) for check in self.checks):
            raise ValueError("checks must contain PreflightCheck values")
        if (
            sum(check.purpose is RoutePurpose.orchestration for check in self.checks)
            != 1
        ):
            raise ValueError("checks must contain exactly one orchestration check")
        identities = {
            (
                check.check_id,
                check.purpose,
                check.operation,
                check.requested_model,
                check.provider,
                check.funding,
                float(check.required_credit_usd),
            )
            for check in self.checks
        }
        if len(identities) != len(self.checks):
            raise ValueError("checks must have unique identities")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise ValueError("attempts must be an integer")
        if not 1 <= self.attempts <= _MAX_ATTEMPTS:
            raise ValueError("attempts is outside the supported range")
        if any(check.attempts != self.attempts for check in self.checks):
            raise ValueError("check attempts must match report attempts")
        if type(self.paid_canary_performed) is not bool:  # noqa: E721
            raise ValueError("paid_canary_performed must be a boolean")
        if self.paid_canary_performed:
            raise ValueError("the readiness endpoint must not perform a paid canary")

    @property
    def passed(self) -> bool:
        return bool(self.checks) and not self.paid_canary_performed

    @property
    def approved_agent_model(self) -> str:
        return next(
            check.requested_model
            for check in self.checks
            if check.purpose is RoutePurpose.orchestration
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": PREFLIGHT_REPORT_SCHEMA,
            "verdict": "PREFLIGHT_PASS",
            "started_at": _format_utc_datetime(self.started_at),
            "finished_at": _format_utc_datetime(self.finished_at),
            "passed": self.passed,
            "approved_agent_model": self.approved_agent_model,
            "checks": [check.to_dict() for check in self.checks],
            "attempts": self.attempts,
            "paid_canary_performed": self.paid_canary_performed,
        }


def _validate_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be an aware UTC datetime")
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError(f"{field} must be an aware UTC datetime")
    return value


class PreflightError(RuntimeError):
    """Base class for safe readiness failures."""

    code: ClassVar[str] = "P_PREFLIGHT_ERROR"

    def __init__(
        self,
        *,
        status_code: int | None = None,
        attempts: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        if status_code is not None and (
            type(status_code) is not int or not 100 <= status_code <= 599
        ):
            raise ValueError("status_code must be an HTTP status")
        if attempts is not None and (
            type(attempts) is not int or not 0 <= attempts <= _MAX_ATTEMPTS
        ):
            raise ValueError("attempts is outside the supported range")
        if retry_after_seconds is not None and (
            isinstance(retry_after_seconds, bool)
            or not isinstance(retry_after_seconds, (int, float))
            or not math.isfinite(float(retry_after_seconds))
            or not 0 <= float(retry_after_seconds) <= _MAX_RETRY_AFTER_SECONDS
        ):
            raise ValueError("retry_after_seconds is outside the supported range")
        super().__init__(self.code)
        self.status_code = status_code
        self.attempts = attempts
        self.retry_after_seconds = (
            float(retry_after_seconds) if retry_after_seconds is not None else None
        )

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code}
        if type(self.status_code) is int and 100 <= self.status_code <= 599:
            result["status_code"] = self.status_code
        if type(self.attempts) is int and 0 <= self.attempts <= _MAX_ATTEMPTS:
            result["attempts"] = self.attempts
        if (
            isinstance(self.retry_after_seconds, (int, float))
            and not isinstance(self.retry_after_seconds, bool)
            and math.isfinite(float(self.retry_after_seconds))
            and 0 <= float(self.retry_after_seconds) <= _MAX_RETRY_AFTER_SECONDS
        ):
            result["retry_after_seconds"] = float(self.retry_after_seconds)
        return result


class PreflightConfigurationError(PreflightError):
    code = "P_CONFIG_INVALID"


class PreflightAuthenticationError(PreflightError):
    code = "P_AUTH_INVALID"


class PreflightAuthorizationError(PreflightError):
    code = "P_AUTH_FORBIDDEN"


class PreflightRedirectError(PreflightError):
    code = "P_REDIRECT"


class PreflightRateLimitError(PreflightError):
    code = "P_RATE_LIMITED"


class PreflightTransportError(PreflightError):
    code = "P_TRANSPORT"


class PreflightDeadlineError(PreflightError):
    code = "P_TIMEOUT"


class PreflightResponseTooLargeError(PreflightError):
    code = "P_RESPONSE_TOO_LARGE"


class PreflightRequestError(PreflightError):
    code = "P_REQUEST_REJECTED"


class PreflightSchemaError(PreflightError):
    code = "P_SCHEMA_UNSUPPORTED"


class PreflightNotReadyError(PreflightError):
    code = "P_NOT_READY"

    def __init__(
        self,
        *,
        reason_codes: tuple[str, ...] = (),
        attempts: int | None = None,
    ) -> None:
        if (
            not isinstance(reason_codes, tuple)
            or len(reason_codes) > 16
            or any(
                not isinstance(reason, str)
                or _REASON_RE.fullmatch(reason) is None
                or _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(reason) is not None
                for reason in reason_codes
            )
            or len(set(reason_codes)) != len(reason_codes)
        ):
            raise ValueError("reason_codes must contain unique safe codes")
        super().__init__(attempts=attempts)
        self.reason_codes = reason_codes

    def as_dict(self) -> dict[str, object]:
        result = super().as_dict()
        if (
            isinstance(self.reason_codes, tuple)
            and 0 < len(self.reason_codes) <= 16
            and all(
                isinstance(reason, str)
                and _REASON_RE.fullmatch(reason) is not None
                and _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(reason) is None
                for reason in self.reason_codes
            )
            and len(set(self.reason_codes)) == len(self.reason_codes)
        ):
            result["reason_codes"] = list(self.reason_codes)
        return result


@runtime_checkable
class GatewayReadinessBackend(Protocol):
    """Protocol used by runner code to substitute an offline readiness backend."""

    def check(self, plan: PreflightPlan) -> PreflightReport:
        """Return a report only when every requested route is ready."""


class RespanGatewayReadinessClient:
    """Bounded, direct-HTTPS client for the official readiness endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        transport: _platform._Transport | None = None,
        connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout_seconds: float = _DEFAULT_READ_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        max_attempts: int = _MAX_ATTEMPTS,
        deadline_seconds: float = _MAX_DEADLINE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(api_key, str) or _API_KEY_RE.fullmatch(api_key) is None:
            raise PreflightConfigurationError()
        if any(character.isspace() for character in api_key):
            raise PreflightConfigurationError()
        self._validate_positive_timeout(connect_timeout_seconds)
        self._validate_positive_timeout(read_timeout_seconds)
        if isinstance(max_response_bytes, bool) or not isinstance(
            max_response_bytes, int
        ):
            raise PreflightConfigurationError()
        if not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES:
            raise PreflightConfigurationError()
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise PreflightConfigurationError()
        if not 1 <= max_attempts <= _MAX_ATTEMPTS:
            raise PreflightConfigurationError()
        self._validate_positive_timeout(deadline_seconds)
        if float(deadline_seconds) > _MAX_DEADLINE_SECONDS:
            raise PreflightConfigurationError()
        if not all(callable(value) for value in (clock, wall_clock, sleeper)):
            raise PreflightConfigurationError()

        self._api_key = api_key
        self._transport = (
            transport if transport is not None else _platform._HttpsTransport()
        )
        self._connect_timeout_seconds = float(connect_timeout_seconds)
        self._read_timeout_seconds = float(read_timeout_seconds)
        self._max_response_bytes = max_response_bytes
        self._max_attempts = max_attempts
        self._deadline_seconds = float(deadline_seconds)
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleeper = sleeper

    @staticmethod
    def _validate_positive_timeout(value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PreflightConfigurationError()
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise PreflightConfigurationError()

    def check(self, plan: PreflightPlan) -> PreflightReport:
        if not isinstance(plan, PreflightPlan):
            raise PreflightConfigurationError()

        started_wall = self._wall_clock()
        started = self._clock()
        deadline = started + self._deadline_seconds
        request_body = _platform._encode_json(
            {
                "schema_version": GATEWAY_READINESS_SCHEMA,
                "checks": [route.to_dict() for route in plan.routes],
            },
            "gateway_readiness",
        )

        for attempt in range(1, self._max_attempts + 1):
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise PreflightDeadlineError(attempts=attempt - 1)
            request = self._request(request_body, remaining)
            try:
                response = self._transport.request(request)
            except _platform.BackendTransportError as error:
                if self._clock() >= deadline:
                    raise PreflightDeadlineError(attempts=attempt) from None
                if error.operation == "http_response":
                    raise PreflightResponseTooLargeError(attempts=attempt) from None
                if attempt == self._max_attempts:
                    raise PreflightTransportError(attempts=attempt) from None
                if not self._retry_sleep(self._backoff(attempt), deadline, attempt):
                    raise PreflightDeadlineError(attempts=attempt) from None
                continue
            except (OSError, TimeoutError, ConnectionError):
                if self._clock() >= deadline:
                    raise PreflightDeadlineError(attempts=attempt) from None
                if attempt == self._max_attempts:
                    raise PreflightTransportError(attempts=attempt) from None
                if not self._retry_sleep(self._backoff(attempt), deadline, attempt):
                    raise PreflightDeadlineError(attempts=attempt) from None
                continue
            except Exception:  # unknown injected failures are never retried
                if self._clock() >= deadline:
                    raise PreflightDeadlineError(attempts=attempt) from None
                raise PreflightTransportError(attempts=attempt) from None

            if self._clock() >= deadline:
                raise PreflightDeadlineError(attempts=attempt)

            if (
                not isinstance(response, _platform._TransportResponse)
                or type(response.status) is not int
                or not isinstance(response.headers, Mapping)
                or not isinstance(response.body, bytes)
            ):
                raise PreflightTransportError(attempts=attempt)
            try:
                normalized_headers = {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
                if len(normalized_headers) != len(response.headers):
                    raise ValueError
            except Exception:
                raise PreflightTransportError(attempts=attempt) from None
            if len(response.body) > self._max_response_bytes:
                raise PreflightResponseTooLargeError(
                    status_code=response.status,
                    attempts=attempt,
                )

            status = response.status
            if 300 <= status <= 399:
                raise PreflightRedirectError(status_code=status, attempts=attempt)
            if status == 401:
                raise PreflightAuthenticationError(status_code=status, attempts=attempt)
            if status == 403:
                raise PreflightAuthorizationError(status_code=status, attempts=attempt)
            if status == 200:
                payload = self._decode_response(
                    response.body, normalized_headers, attempt
                )
                checks = self._validate_ready_payload(payload, plan, attempt)
                if self._clock() >= deadline:
                    raise PreflightDeadlineError(attempts=attempt)
                finished_wall = started_wall + max(
                    0.0, self._wall_clock() - started_wall
                )
                return PreflightReport(
                    started_at=_utc_datetime(started_wall),
                    finished_at=_utc_datetime(finished_wall),
                    checks=checks,
                    attempts=attempt,
                )

            retry_after = None
            if status == 429:
                retry_after = _bounded_retry_after(
                    normalized_headers.get("retry-after"),
                    now=self._wall_clock(),
                )
            if status in _RETRIABLE_STATUSES:
                if attempt == self._max_attempts:
                    if status == 429:
                        raise PreflightRateLimitError(
                            status_code=status,
                            attempts=attempt,
                            retry_after_seconds=retry_after,
                        )
                    raise PreflightTransportError(status_code=status, attempts=attempt)
                delay = (
                    retry_after if retry_after is not None else self._backoff(attempt)
                )
                if not self._retry_sleep(delay, deadline, attempt):
                    if status == 429:
                        raise PreflightRateLimitError(
                            status_code=status,
                            attempts=attempt,
                            retry_after_seconds=retry_after,
                        )
                    raise PreflightDeadlineError(attempts=attempt)
                continue

            raise PreflightRequestError(status_code=status, attempts=attempt)

        raise AssertionError("unreachable")

    def _request(self, body: bytes, remaining: float) -> _platform._TransportRequest:
        connect_timeout = min(self._connect_timeout_seconds, remaining / 2.0)
        read_timeout = min(self._read_timeout_seconds, remaining - connect_timeout)
        if connect_timeout <= 0 or read_timeout <= 0:
            raise PreflightDeadlineError()
        return _platform._TransportRequest(
            url=GATEWAY_READINESS_URL,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Connection": "close",
                "Content-Type": "application/json",
                "User-Agent": "respan-integration-agent/gateway-readiness-v1",
            },
            body=body,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_response_bytes=self._max_response_bytes,
        )

    def _decode_response(
        self,
        body: bytes,
        headers: Mapping[str, str],
        attempt: int,
    ) -> dict[str, object]:
        content_type = headers.get("content-type")
        if (
            content_type is None
            or content_type.split(";", 1)[0].strip().lower() != "application/json"
        ):
            raise PreflightSchemaError(status_code=200, attempts=attempt)
        try:
            return _platform._decode_json_object(body, "gateway_readiness")
        except Exception:
            raise PreflightSchemaError(status_code=200, attempts=attempt) from None

    def _validate_ready_payload(
        self,
        payload: dict[str, object],
        plan: PreflightPlan,
        attempt: int,
    ) -> tuple[PreflightCheck, ...]:
        expected_root = {"schema_version", "ready", "checks"}
        if set(payload) != expected_root:
            raise PreflightSchemaError(status_code=200, attempts=attempt)
        if payload["schema_version"] != GATEWAY_READINESS_SCHEMA:
            raise PreflightSchemaError(status_code=200, attempts=attempt)
        ready = payload["ready"]
        if type(ready) is not bool:  # noqa: E721
            raise PreflightSchemaError(status_code=200, attempts=attempt)

        raw_checks = payload["checks"]
        if not isinstance(raw_checks, list):
            raise PreflightSchemaError(status_code=200, attempts=attempt)
        if len(raw_checks) != len(plan.routes):
            raise PreflightSchemaError(status_code=200, attempts=attempt)

        by_identity: dict[
            tuple[str, str, str, str, str, str, float], dict[str, object]
        ] = {}
        for raw_check in raw_checks:
            if not isinstance(raw_check, dict):
                raise PreflightSchemaError(status_code=200, attempts=attempt)
            identity = _response_identity(raw_check, attempt)
            if identity in by_identity:
                raise PreflightSchemaError(status_code=200, attempts=attempt)
            by_identity[identity] = raw_check
        if set(by_identity) != {route.identity for route in plan.routes}:
            raise PreflightSchemaError(status_code=200, attempts=attempt)

        checks: list[PreflightCheck] = []
        all_checks_ready = True
        negative_reason_codes: list[str] = []
        for route in plan.routes:
            raw_check = by_identity[route.identity]
            check_reasons = _validate_reasons(raw_check["reason_codes"], attempt)
            check_status = raw_check["status"]
            funding_satisfied = raw_check["funding_satisfied"]
            flags = (
                raw_check["key_ready"],
                raw_check["limits_ready"],
                raw_check["route_ready"],
            )
            if check_status not in {"ready", "not_ready"}:
                raise PreflightSchemaError(status_code=200, attempts=attempt)
            if type(funding_satisfied) is not bool or any(  # noqa: E721
                type(flag) is not bool
                for flag in flags  # noqa: E721
            ):
                raise PreflightSchemaError(status_code=200, attempts=attempt)

            resolved_model = raw_check["resolved_model"]
            credential_source = raw_check["credential_source"]
            try:
                _require_model(resolved_model, "resolved_model")
            except ValueError:
                raise PreflightSchemaError(status_code=200, attempts=attempt) from None
            if (
                route.purpose is RoutePurpose.orchestration
                and resolved_model != route.model
            ):
                raise PreflightSchemaError(status_code=200, attempts=attempt)
            if not isinstance(credential_source, str):
                raise PreflightSchemaError(status_code=200, attempts=attempt)
            allowed_sources = {
                FundingRequirement.credits: {"managed"},
                FundingRequirement.byok: {"customer"},
                FundingRequirement.any: {"managed", "customer"},
            }[route.funding]
            if credential_source not in allowed_sources:
                raise PreflightSchemaError(status_code=200, attempts=attempt)

            check_is_ready = (
                check_status == "ready"
                and funding_satisfied
                and flags == (True, True, True)
                and not check_reasons
            )
            if not check_is_ready:
                all_checks_ready = False
                negative_reason_codes.extend(check_reasons)

            checks.append(
                PreflightCheck(
                    check_id=route.check_id,
                    purpose=route.purpose,
                    operation=route.operation,
                    requested_model=route.model,
                    resolved_model=resolved_model,
                    provider=route.provider,
                    funding=route.funding,
                    required_credit_usd=route.required_credit_usd,
                    credential_source=credential_source,
                    attempts=attempt,
                )
            )
        if not ready or not all_checks_ready:
            unique_reasons = tuple(dict.fromkeys(negative_reason_codes))
            if len(unique_reasons) > 16:
                raise PreflightSchemaError(status_code=200, attempts=attempt)
            raise PreflightNotReadyError(
                reason_codes=unique_reasons,
                attempts=attempt,
            )
        return tuple(checks)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.25 * (2 ** (attempt - 1)), 1.0)

    def _retry_sleep(self, delay: float, deadline: float, attempt: int) -> bool:
        bounded_delay = max(0.0, min(float(delay), _MAX_RETRY_AFTER_SECONDS))
        if bounded_delay >= deadline - self._clock():
            return False
        try:
            self._sleeper(bounded_delay)
        except Exception:
            raise PreflightTransportError(attempts=attempt) from None
        return self._clock() < deadline


_CHECK_KEYS = {
    "check_id",
    "purpose",
    "operation",
    "requested_model",
    "resolved_model",
    "provider",
    "credential_source",
    "funding_requested",
    "funding_satisfied",
    "required_credit_usd",
    "status",
    "key_ready",
    "limits_ready",
    "route_ready",
    "reason_codes",
}


def _response_identity(
    raw_check: dict[str, object],
    attempt: int,
) -> tuple[str, str, str, str, str, str, float]:
    if set(raw_check) != _CHECK_KEYS:
        raise PreflightSchemaError(status_code=200, attempts=attempt)
    check_id = raw_check["check_id"]
    purpose = raw_check["purpose"]
    operation = raw_check["operation"]
    requested_model = raw_check["requested_model"]
    provider = raw_check["provider"]
    funding = raw_check["funding_requested"]
    required_credit_usd = raw_check["required_credit_usd"]
    if purpose not in {item.value for item in RoutePurpose}:
        raise PreflightSchemaError(status_code=200, attempts=attempt)
    if funding not in {item.value for item in FundingRequirement}:
        raise PreflightSchemaError(status_code=200, attempts=attempt)
    try:
        _require_check_id(check_id)
        _require_operation(operation, "operation")
        _require_model(requested_model, "requested_model")
        _require_provider(provider, "provider")
        if operation == "anthropic.messages" and provider != "anthropic":
            raise ValueError
        _require_money(required_credit_usd, "required_credit_usd")
    except ValueError:
        raise PreflightSchemaError(status_code=200, attempts=attempt) from None
    return (
        check_id,
        purpose,
        operation,
        requested_model,
        provider,
        funding,
        float(required_credit_usd),
    )


def _validate_reasons(value: object, attempt: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 16:
        raise PreflightSchemaError(status_code=200, attempts=attempt)
    result: list[str] = []
    for reason in value:
        if (
            not isinstance(reason, str)
            or _REASON_RE.fullmatch(reason) is None
            or _CREDENTIAL_LIKE_ROUTE_VALUE.fullmatch(reason) is not None
        ):
            raise PreflightSchemaError(status_code=200, attempts=attempt)
        result.append(reason)
    if len(set(result)) != len(result):
        raise PreflightSchemaError(status_code=200, attempts=attempt)
    return tuple(result)


def _bounded_retry_after(value: str | None, *, now: float) -> float | None:
    if value is None:
        return None
    candidate = value.strip()
    try:
        seconds = float(candidate)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = parsed.timestamp() - now
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


def _utc_datetime(timestamp: float) -> datetime:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise PreflightConfigurationError() from None


def _format_utc_datetime(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "FundingRequirement",
    "GATEWAY_READINESS_SCHEMA",
    "GATEWAY_READINESS_URL",
    "GatewayReadinessBackend",
    "OFFICIAL_RESPAN_API_BASE",
    "OFFICIAL_RESPAN_API_ORIGIN",
    "PREFLIGHT_REPORT_SCHEMA",
    "PreflightAuthenticationError",
    "PreflightAuthorizationError",
    "PreflightCheck",
    "PreflightConfigurationError",
    "PreflightDeadlineError",
    "PreflightError",
    "PreflightNotReadyError",
    "PreflightPlan",
    "PreflightRateLimitError",
    "PreflightRedirectError",
    "PreflightReport",
    "PreflightRequestError",
    "PreflightResponseTooLargeError",
    "PreflightSchemaError",
    "PreflightTransportError",
    "RespanGatewayReadinessClient",
    "RoutePurpose",
    "RouteRequirement",
]
