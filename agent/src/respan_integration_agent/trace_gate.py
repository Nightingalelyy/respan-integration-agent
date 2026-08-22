"""Pure, bounded backend acceptance gate for the trusted v0a smoke run.

The platform adapter deliberately returns JSON-native dictionaries.  This module owns
the stricter, versioned meaning of those records: two exact-marker traces, complete and
stable cross-view inventories, safe span trees, and the agent/target semantic contracts.
It never includes backend payloads or credentials in reports or exceptions.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote, quote_plus

from .agent import DEFAULT_AGENT_MODEL
from .platform import (
    BackendNotFoundError,
    BackendRateLimitError,
    BackendRedirectError,
    BackendSchemaError,
    BackendTransportError,
    TraceBackend,
)


_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RECORD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_AGENT_RUN_ID_RE = re.compile(r"^respan-v0a-agent-[A-Za-z0-9._:-]{1,64}$")
_TARGET_RUN_ID_RE = re.compile(r"^respan-v0a-target-[A-Za-z0-9._:-]{1,64}$")
_OFFICIAL_TARGET_API_BASE = "https://api.respan.ai/api/"
_SUCCESS_STATUSES = frozenset({"success"})
_ALLOWED_AGENT_TOOLS = frozenset({"Skill", "Read", "Glob", "Grep", "Edit"})
_KNOWN_WARNING_CODES = frozenset(
    {
        "W_ENVIRONMENT_PROJECTION",
        "W_AGENT_AGGREGATE_COST",
        "W_AGENT_TREE_USAGE",
        "W_TARGET_SPAN_CONTRACT",
    }
)
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)https?://[^\s/:@]+:[^\s/@]+@"),
)


class TraceGateError(RuntimeError):
    """Base class for redacted gate failures."""

    def __init__(self, code: str, *, role: str | None = None) -> None:
        self.code = code
        self.role = role
        suffix = f" ({role})" if role else ""
        super().__init__(f"{code}{suffix}")


class TraceNotReady(TraceGateError):
    """Internal retry state for an absent or incomplete observation."""


class TraceContractError(TraceGateError):
    """A stable backend trace violates the reviewed v0a contract."""


class TraceAmbiguityError(TraceContractError):
    """An exact marker resolved ambiguously or to the wrong fixed trace."""


class TraceGateAvailabilityError(TraceGateError):
    """The backend did not become observable before the bounded deadline."""


class TraceDeadlineExceeded(TraceGateAvailabilityError):
    """The shared convergence deadline expired."""

    def __init__(
        self,
        code: str,
        *,
        attempts: int,
        elapsed_seconds: float,
        unmet_codes: Sequence[str],
    ) -> None:
        self.attempts = attempts
        self.elapsed_seconds = elapsed_seconds
        self.unmet_codes = tuple(sorted(set(unmet_codes)))
        super().__init__(code)


class TraceSecretExposureError(TraceContractError):
    """A trace contains an exact or high-confidence credential indicator."""


@dataclass(frozen=True)
class AgentTraceExpectation:
    run_id: str
    trace_id: str
    smoke_started_at: datetime
    smoke_finished_at: datetime
    sdk_cost_usd: float
    checkout_root: Path | None = None
    root_span_name: str = "workflow"
    service_name: str = "respan-integration-agent"
    environment: str = "onboarding"
    model: str = DEFAULT_AGENT_MODEL
    provider_system: str = "anthropic"
    target_service_name: str = "respan-v0a-python-smoke"
    target_environment: str = "smoke"
    respan_ai_version: str = "4.1.0"
    openai_otel_version: str = "0.62.3"
    instrumentation_scope_name: str = "opentelemetry.instrumentation.claude_agent_sdk"
    instrumentation_scope_version: str = "0.1.4"
    edited_paths: frozenset[str] = field(
        default_factory=lambda: frozenset({"app.py", "requirements.txt"})
    )

    def __post_init__(self) -> None:
        _validate_expectation_window(self.smoke_started_at, self.smoke_finished_at)
        if not _TRACE_ID_RE.fullmatch(self.trace_id) or int(self.trace_id, 16) == 0:
            raise ValueError("agent trace_id must be nonzero lowercase 32-hex")
        if not _AGENT_RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("agent run_id must use the v0a agent marker format")
        if not math.isfinite(self.sdk_cost_usd) or self.sdk_cost_usd <= 0:
            raise ValueError("agent expectation requires a run id and positive finite SDK cost")
        if self.checkout_root is not None and not self.checkout_root.is_absolute():
            raise ValueError("agent checkout_root must be absolute")


@dataclass(frozen=True)
class TargetTraceExpectation:
    run_id: str
    smoke_started_at: datetime
    smoke_finished_at: datetime
    span_name: str = "llm.gpt-4o-mini"
    model: str = "gpt-4o-mini"
    provider: str = "openai"
    service_name: str = "respan-v0a-python-smoke"
    environment: str = "smoke"
    instrumentation_scope_name: str = "opentelemetry.instrumentation.openai.v1"
    instrumentation_scope_version: str = "0.62.3"
    official_api_base: str = _OFFICIAL_TARGET_API_BASE

    def __post_init__(self) -> None:
        _validate_expectation_window(self.smoke_started_at, self.smoke_finished_at)
        if not _TARGET_RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("target run_id must use the v0a target marker format")
        if self.official_api_base != _OFFICIAL_TARGET_API_BASE:
            raise ValueError("target API base must remain pinned to the official Respan API")


@dataclass(frozen=True)
class PollingPolicy:
    timeout_seconds: float = 120.0
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0, 5.0)
    max_retry_after_seconds: float = 5.0
    required_stable_observations: int = 2
    clock_skew_seconds: float = 30.0
    cost_absolute_tolerance: float = 1e-9
    cost_relative_tolerance: float = 1e-6

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if not self.backoff_seconds or any(
            not math.isfinite(value) or value <= 0 for value in self.backoff_seconds
        ):
            raise ValueError("backoff_seconds must contain positive finite values")
        if self.max_retry_after_seconds <= 0:
            raise ValueError("max_retry_after_seconds must be positive")
        if self.required_stable_observations < 2:
            raise ValueError("at least two stable observations are required")
        if self.clock_skew_seconds < 0:
            raise ValueError("clock_skew_seconds cannot be negative")


@dataclass(frozen=True)
class SpanObservation:
    record_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    log_type: str
    status: str
    closed: bool
    has_required_input: bool
    has_required_output: bool


@dataclass(frozen=True)
class TraceObservation:
    role: Literal["agent", "target"]
    trace_id: str
    span_count: int
    spans: tuple[SpanObservation, ...]
    fingerprint: str


@dataclass(frozen=True)
class TraceCheck:
    code: str
    status: Literal["pass", "warn", "fail"]
    role: Literal["agent", "target", "shared"]


@dataclass(frozen=True)
class TraceGateReport:
    agent_trace_id: str
    target_trace_id: str
    attempts: int
    elapsed_seconds: float
    checks: tuple[TraceCheck, ...]

    @property
    def passed(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    @property
    def warning_codes(self) -> tuple[str, ...]:
        return tuple(sorted({check.code for check in self.checks if check.status == "warn"}))

    @property
    def agent_trace_url(self) -> str:
        return _trace_url(self.agent_trace_id)

    @property
    def target_trace_url(self) -> str:
        return _trace_url(self.target_trace_id)

    def to_dict(self) -> dict[str, Any]:
        """Return deterministic, payload-free evidence safe to print or archive."""

        checks = sorted(
            self.checks,
            key=lambda check: (check.role, check.code, check.status),
        )
        return {
            "schema_version": "respan-v0-backend-trace-gate/v1",
            "passed": self.passed,
            "agent_trace_id": self.agent_trace_id,
            "agent_trace_url": self.agent_trace_url,
            "target_trace_id": self.target_trace_id,
            "target_trace_url": self.target_trace_url,
            "attempts": self.attempts,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "checks": [
                {"code": check.code, "status": check.status, "role": check.role}
                for check in checks
            ],
            "warnings": sorted(self.warning_codes),
        }


@dataclass(frozen=True)
class _Inventory:
    observation: TraceObservation
    marker_row: Mapping[str, Any]
    trace: Mapping[str, Any]
    tree_rows: tuple[Mapping[str, Any], ...]
    list_rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class _HydratedTrace:
    inventory: _Inventory
    details: tuple[Mapping[str, Any], ...]

    @property
    def details_by_id(self) -> dict[str, Mapping[str, Any]]:
        return {_record_id(item): item for item in self.details}


def poll_and_verify_smoke_traces(
    backend: TraceBackend,
    *,
    agent: AgentTraceExpectation,
    target: TargetTraceExpectation,
    secret_values: Sequence[str] = (),
    policy: PollingPolicy | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> TraceGateReport:
    """Poll and validate both v0a traces under one monotonic deadline.

    Only redacted check codes and locked trace IDs escape this function.  Backend
    exceptions are either retried according to the plan or propagated when their class
    means retrying cannot make the request safe or valid.
    """

    resolved_policy = policy or PollingPolicy()
    if agent.run_id == target.run_id:
        raise ValueError("agent and target run ids must be distinct")
    secrets = _expanded_secrets(secret_values)
    started = clock()
    deadline = started + resolved_policy.timeout_seconds
    attempts = 0
    last_unmet = {"T_NOT_OBSERVED_AGENT", "T_NOT_OBSERVED_TARGET"}
    last_fingerprints: dict[str, str] = {}
    stable_counts = {"agent": 0, "target": 0}
    retry_after = 0.0
    locked_target_trace_id: str | None = None

    while True:
        now = clock()
        if now >= deadline:
            raise TraceDeadlineExceeded(
                "E_TRACE_DEADLINE_EXCEEDED",
                attempts=attempts,
                elapsed_seconds=max(0.0, now - started),
                unmet_codes=tuple(last_unmet),
            )
        attempts += 1
        inventories: dict[str, _Inventory] = {}
        cycle_unmet: set[str] = set()
        retry_after = 0.0
        specifications = (
            (
                "agent",
                agent.run_id,
                agent.trace_id,
                agent.smoke_started_at,
                agent.smoke_finished_at,
            ),
            (
                "target",
                target.run_id,
                locked_target_trace_id,
                target.smoke_started_at,
                target.smoke_finished_at,
            ),
        )
        for role, run_id, fixed_trace_id, start_time, end_time in specifications:
            try:
                inventory = _fetch_inventory(
                    backend,
                    role=role,
                    run_id=run_id,
                    fixed_trace_id=fixed_trace_id,
                    start_time=start_time,
                    end_time=end_time,
                    secrets=secrets,
                    clock=clock,
                    deadline=deadline,
                )
            except BackendRedirectError:
                raise
            except (
                TraceNotReady,
                BackendNotFoundError,
                BackendRateLimitError,
                BackendTransportError,
            ) as exc:
                code, requested_delay = _retry_directive(exc, resolved_policy)
                cycle_unmet.add(code)
                retry_after = max(retry_after, requested_delay)
                stable_counts[role] = 0
                last_fingerprints.pop(role, None)
                continue
            inventories[role] = inventory
            if role == "target" and locked_target_trace_id is None:
                locked_target_trace_id = inventory.observation.trace_id

        if len(inventories) == 2:
            agent_inventory = inventories["agent"]
            target_inventory = inventories["target"]
            if agent_inventory.observation.trace_id == target_inventory.observation.trace_id:
                raise TraceAmbiguityError("E_TRACE_IDS_NOT_DISTINCT", role="shared")

            for role, inventory in inventories.items():
                fingerprint = inventory.observation.fingerprint
                if last_fingerprints.get(role) == fingerprint:
                    stable_counts[role] += 1
                else:
                    last_fingerprints[role] = fingerprint
                    stable_counts[role] = 1

            if all(
                count >= resolved_policy.required_stable_observations
                for count in stable_counts.values()
            ):
                try:
                    agent_hydrated = _hydrate(
                        backend,
                        agent_inventory,
                        secrets=secrets,
                        clock=clock,
                        deadline=deadline,
                    )
                    target_hydrated = _hydrate(
                        backend,
                        target_inventory,
                        secrets=secrets,
                        clock=clock,
                        deadline=deadline,
                    )
                    checks = _validate_all(
                        agent_hydrated,
                        target_hydrated,
                        agent=agent,
                        target=target,
                        policy=resolved_policy,
                    )
                except BackendRedirectError:
                    raise
                except (
                    TraceNotReady,
                    BackendNotFoundError,
                    BackendRateLimitError,
                    BackendTransportError,
                ) as exc:
                    code, requested_delay = _retry_directive(exc, resolved_policy)
                    cycle_unmet.add(code)
                    retry_after = max(retry_after, requested_delay)
                else:
                    accepted_at = clock()
                    if accepted_at >= deadline:
                        raise TraceDeadlineExceeded(
                            "E_TRACE_DEADLINE_EXCEEDED",
                            attempts=attempts,
                            elapsed_seconds=max(0.0, accepted_at - started),
                            unmet_codes=("T_DEADLINE_BEFORE_ACCEPTANCE",),
                        )
                    elapsed = max(0.0, accepted_at - started)
                    return TraceGateReport(
                        agent_trace_id=agent_inventory.observation.trace_id,
                        target_trace_id=target_inventory.observation.trace_id,
                        attempts=attempts,
                        elapsed_seconds=elapsed,
                        checks=checks,
                    )
            cycle_unmet.update(
                {
                f"T_INVENTORY_NOT_STABLE_{role.upper()}"
                for role, count in stable_counts.items()
                if count < resolved_policy.required_stable_observations
                }
            )
        last_unmet = cycle_unmet or {"T_TRACE_OBSERVATION_INCOMPLETE"}

        now = clock()
        if now >= deadline:
            raise TraceDeadlineExceeded(
                "E_TRACE_DEADLINE_EXCEEDED",
                attempts=attempts,
                elapsed_seconds=max(0.0, now - started),
                unmet_codes=tuple(last_unmet),
            )
        scheduled = resolved_policy.backoff_seconds[
            min(attempts - 1, len(resolved_policy.backoff_seconds) - 1)
        ]
        delay = max(scheduled, retry_after)
        remaining = deadline - now
        sleeper(min(delay, remaining))


def _fetch_inventory(
    backend: TraceBackend,
    *,
    role: Literal["agent", "target"],
    run_id: str,
    fixed_trace_id: str | None,
    start_time: datetime,
    end_time: datetime,
    secrets: tuple[str, ...],
    clock: Callable[[], float],
    deadline: float,
) -> _Inventory:
    _ensure_deadline_remaining(clock, deadline, role=role, phase="INVENTORY")
    rows = backend.list_traces_by_run_id(run_id, start_time, end_time)
    _ensure_deadline_remaining(clock, deadline, role=role, phase="INVENTORY")
    _scan_for_secrets(rows, secrets=secrets, role=role)
    if not rows:
        raise TraceNotReady("T_MARKER_NOT_INDEXED", role=role)
    if len(rows) != 1:
        raise TraceAmbiguityError("E_MARKER_NOT_UNIQUE", role=role)
    marker_row = rows[0]
    trace_id = _trace_id(marker_row)
    if fixed_trace_id is not None and trace_id != fixed_trace_id:
        code = (
            "E_AGENT_MARKER_TRACE_MISMATCH"
            if role == "agent"
            else "E_TARGET_MARKER_TRACE_CHANGED"
        )
        raise TraceAmbiguityError(code, role=role)

    _ensure_deadline_remaining(clock, deadline, role=role, phase="INVENTORY")
    trace = backend.retrieve_trace(trace_id)
    _ensure_deadline_remaining(clock, deadline, role=role, phase="INVENTORY")
    list_rows = backend.list_spans_by_trace_id(trace_id, start_time, end_time)
    _ensure_deadline_remaining(clock, deadline, role=role, phase="INVENTORY")
    _scan_for_secrets((trace, list_rows), secrets=secrets, role=role)
    if not list_rows:
        raise TraceNotReady("T_SPAN_LIST_EMPTY", role=role)

    tree = trace.get("span_tree")
    if not isinstance(tree, list) or not tree:
        raise TraceNotReady("T_SPAN_TREE_EMPTY", role=role)
    tree_rows = _flatten_tree(tree, role=role)
    tree_ids = [_record_id(item) for item in tree_rows]
    list_ids = [_record_id(item) for item in list_rows]
    tree_span_ids = [_span_id(item) for item in tree_rows]
    list_span_ids = [_span_id(item) for item in list_rows]
    _require_unique(tree_ids, "E_DUPLICATE_TREE_RECORD_ID", role)
    _require_unique(list_ids, "E_DUPLICATE_LIST_RECORD_ID", role)

    span_count = _required_int(trace, "span_count", role=role)
    if span_count <= 0:
        raise TraceNotReady("T_TRACE_SPAN_COUNT_ZERO", role=role)
    if span_count != len(tree_rows) or span_count != len(list_rows):
        raise TraceNotReady("T_SPAN_COUNT_DISAGREEMENT", role=role)
    if set(tree_ids) != set(list_ids):
        raise TraceNotReady("T_TREE_LIST_ID_DISAGREEMENT", role=role)
    if set(tree_span_ids) != set(list_span_ids):
        raise TraceNotReady("T_TREE_LIST_SPAN_ID_DISAGREEMENT", role=role)
    if any(
        not row.get("start_time") or not row.get("end_time", row.get("timestamp"))
        for row in (*tree_rows, *list_rows)
    ):
        raise TraceNotReady("T_INVENTORY_SPAN_NOT_CLOSED", role=role)
    tree_by_record_id = {_record_id(item): item for item in tree_rows}
    for list_row in list_rows:
        tree_row = tree_by_record_id[_record_id(list_row)]
        if _span_id(list_row) != _span_id(tree_row):
            raise TraceContractError("E_TREE_LIST_SPAN_ID_DISAGREEMENT", role=role)
        if _parent_span_id(list_row) != _parent_span_id(tree_row):
            raise TraceContractError("E_TREE_LIST_PARENT_DISAGREEMENT", role=role)
    if _trace_id(trace) != trace_id or any(_trace_id(item) != trace_id for item in tree_rows):
        raise TraceContractError("E_TREE_TRACE_ID_MISMATCH", role=role)
    if any(_trace_id(item) != trace_id for item in list_rows):
        raise TraceContractError("E_LIST_TRACE_ID_MISMATCH", role=role)

    _validate_marker_metadata(marker_row, run_id, role=role, code="E_MARKER_ROW_METADATA")
    _validate_marker_metadata(trace, run_id, role=role, code="E_TRACE_MARKER_METADATA")
    root = _validate_hierarchy(tree_rows, role=role)
    _validate_tree_nesting(tree, role=role)
    if trace.get("root_span_unique_id") != _span_id(root):
        raise TraceContractError("E_ROOT_SPAN_ID_MISMATCH", role=role)
    _validate_marker_metadata(root, run_id, role=role, code="E_ROOT_MARKER_METADATA")

    observations: list[SpanObservation] = []
    for row in sorted(tree_rows, key=_record_id):
        start = row.get("start_time")
        end = row.get("end_time", row.get("timestamp"))
        observations.append(
            SpanObservation(
                record_id=_record_id(row),
                span_id=_span_id(row),
                parent_span_id=_parent_span_id(row),
                name=_required_str(row, "span_name", role=role),
                log_type=_required_str(row, "log_type", role=role),
                status=str(row.get("status", "")).strip().lower(),
                closed=bool(start and end),
                has_required_input=_has_content(_span_input(row)),
                has_required_output=_has_content(_span_output(row)),
            )
        )
    aggregate_fields = (
        span_count,
        trace.get("error_count"),
        trace.get("total_prompt_tokens"),
        trace.get("total_completion_tokens"),
        trace.get("total_tokens"),
        bool(trace.get("end_time")),
    )
    fingerprint_payload = (
        trace_id,
        aggregate_fields,
        tuple(
            (
                span.record_id,
                span.span_id,
                span.parent_span_id,
                span.name,
                span.log_type,
                span.status,
                span.closed,
                span.has_required_input,
                span.has_required_output,
            )
            for span in observations
        ),
        tuple(
            sorted(
                (
                    _record_id(row),
                    _span_id(row),
                    _parent_span_id(row),
                    str(row.get("span_name", "")),
                    str(row.get("log_type", "")),
                    str(row.get("status", "")).lower(),
                    bool(row.get("start_time"))
                    and bool(row.get("end_time", row.get("timestamp"))),
                    _has_content(_span_input(row)),
                    _has_content(_span_output(row)),
                )
                for row in list_rows
            )
        ),
    )
    fingerprint = hashlib.sha256(repr(fingerprint_payload).encode()).hexdigest()
    return _Inventory(
        observation=TraceObservation(
            role=role,
            trace_id=trace_id,
            span_count=span_count,
            spans=tuple(observations),
            fingerprint=fingerprint,
        ),
        marker_row=marker_row,
        trace=trace,
        tree_rows=tree_rows,
        list_rows=tuple(list_rows),
    )


def _hydrate(
    backend: TraceBackend,
    inventory: _Inventory,
    *,
    secrets: tuple[str, ...],
    clock: Callable[[], float],
    deadline: float,
) -> _HydratedTrace:
    role = inventory.observation.role
    details: list[Mapping[str, Any]] = []
    for row in inventory.list_rows:
        if clock() >= deadline:
            raise TraceNotReady("T_DEADLINE_DURING_HYDRATION", role=role)
        record_id = _record_id(row)
        detail = backend.retrieve_span(record_id)
        if clock() >= deadline:
            raise TraceNotReady("T_DEADLINE_DURING_HYDRATION", role=role)
        _scan_for_secrets(detail, secrets=secrets, role=role)
        if _record_id(detail) != record_id:
            raise TraceNotReady("T_DETAIL_RECORD_ID_MISMATCH", role=role)
        matching_list = next(
            item for item in inventory.list_rows if _record_id(item) == record_id
        )
        matching_tree = next(
            item for item in inventory.tree_rows if _record_id(item) == record_id
        )
        if not (
            _span_id(detail) == _span_id(matching_list) == _span_id(matching_tree)
        ):
            raise TraceNotReady("T_DETAIL_SPAN_ID_MISMATCH", role=role)
        if _parent_span_id(detail) != _parent_span_id(matching_tree):
            raise TraceNotReady("T_DETAIL_PARENT_ID_MISMATCH", role=role)
        if _parent_span_id(matching_list) != _parent_span_id(detail):
            raise TraceNotReady("T_DETAIL_PARENT_ID_MISMATCH", role=role)
        core_identities = {
            (
                item.get("span_name"),
                item.get("log_type"),
                item.get("status"),
                item.get("status_code"),
            )
            for item in (matching_tree, matching_list, detail)
        }
        if len(core_identities) != 1:
            raise TraceContractError("E_CROSS_VIEW_CORE_IDENTITY", role=role)
        models = {
            item.get("model")
            for item in (matching_tree, matching_list, detail)
            if item.get("model") not in (None, "")
        }
        if len(models) > 1:
            raise TraceContractError("E_CROSS_VIEW_MODEL", role=role)
        details.append(detail)
    detail_ids = [_record_id(item) for item in details]
    _require_unique(detail_ids, "E_DUPLICATE_DETAIL_RECORD_ID", role)
    expected = {_record_id(item) for item in inventory.tree_rows}
    if set(detail_ids) != expected:
        raise TraceNotReady("T_DETAIL_INVENTORY_DISAGREEMENT", role=role)
    trace_id = inventory.observation.trace_id
    if any(_trace_id(item) != trace_id for item in details):
        raise TraceContractError("E_DETAIL_TRACE_ID_MISMATCH", role=role)
    return _HydratedTrace(inventory=inventory, details=tuple(details))


def _validate_all(
    agent_trace: _HydratedTrace,
    target_trace: _HydratedTrace,
    *,
    agent: AgentTraceExpectation,
    target: TargetTraceExpectation,
    policy: PollingPolicy,
) -> tuple[TraceCheck, ...]:
    checks: list[TraceCheck] = []
    checks.extend(
        _validate_shared(
            agent_trace,
            expected_run_id=agent.run_id,
            started_at=agent.smoke_started_at,
            finished_at=agent.smoke_finished_at,
            expected_environment=agent.environment,
            policy=policy,
        )
    )
    checks.extend(
        _validate_shared(
            target_trace,
            expected_run_id=target.run_id,
            started_at=target.smoke_started_at,
            finished_at=target.smoke_finished_at,
            expected_environment=target.environment,
            policy=policy,
        )
    )
    checks.extend(_validate_agent(agent_trace, expected=agent, policy=policy))
    checks.extend(_validate_target(target_trace, expected=target, policy=policy))
    unknown = {
        item.code for item in checks if item.status == "warn"
    } - _KNOWN_WARNING_CODES
    if unknown:
        raise TraceContractError("E_UNREGISTERED_WARNING", role="shared")
    return tuple(checks)


def _validate_shared(
    hydrated: _HydratedTrace,
    *,
    expected_run_id: str,
    started_at: datetime,
    finished_at: datetime,
    expected_environment: str,
    policy: PollingPolicy,
) -> list[TraceCheck]:
    inventory = hydrated.inventory
    role = inventory.observation.role
    trace = inventory.trace
    if _required_int(trace, "error_count", role=role) != 0:
        raise TraceContractError("E_TRACE_ERROR_COUNT", role=role)
    if not trace.get("end_time"):
        raise TraceNotReady("T_TRACE_NOT_CLOSED", role=role)
    _validate_time_range(
        trace,
        started_at=started_at,
        finished_at=finished_at,
        skew_seconds=policy.clock_skew_seconds,
        role=role,
    )
    trace_duration = _required_number(trace, "duration", role=role)
    if trace_duration < 0:
        raise TraceContractError("E_NEGATIVE_TRACE_DURATION", role=role)

    tree_by_record = {_record_id(item): item for item in inventory.tree_rows}
    list_by_record = {_record_id(item): item for item in inventory.list_rows}
    for detail in hydrated.details:
        record_id = _record_id(detail)
        tree_row = tree_by_record[record_id]
        list_row = list_by_record[record_id]
        if detail.get("blurred") is not False:
            raise TraceContractError("E_BLURRED_SPAN_DETAIL", role=role)
        if any(
            "blurred" in item and item.get("blurred") is not False
            for item in (list_row, tree_row)
        ):
            raise TraceContractError("E_BLURRED_SPAN_DETAIL", role=role)
        for view in (detail, tree_row, list_row):
            _validate_time_range(
                view,
                started_at=started_at,
                finished_at=finished_at,
                skew_seconds=policy.clock_skew_seconds,
                role=role,
            )
            latency = view.get("latency")
            if (
                isinstance(latency, bool)
                or not isinstance(latency, (int, float))
                or latency < 0
            ):
                raise TraceContractError("E_INVALID_SPAN_DURATION", role=role)
            _validate_success(view, role=role)
        if "Span not properly closed" in _safe_json((detail, tree_row, list_row)):
            raise TraceContractError("E_IMPROPERLY_CLOSED_SPAN", role=role)
        _validate_marker_metadata(detail, expected_run_id, role=role, code="E_DETAIL_MARKER_METADATA")

    top_environment = str(trace.get("environment", "")).strip()
    checks = [TraceCheck("P_SHARED_TRACE_CONTRACT", "pass", role)]
    if top_environment != expected_environment:
        if top_environment == "prod":
            checks.append(TraceCheck("W_ENVIRONMENT_PROJECTION", "warn", role))
        else:
            raise TraceContractError("E_ENVIRONMENT_PROJECTION_UNKNOWN", role=role)
    return checks


def _validate_agent(
    hydrated: _HydratedTrace,
    *,
    expected: AgentTraceExpectation,
    policy: PollingPolicy,
) -> list[TraceCheck]:
    inventory = hydrated.inventory
    details = hydrated.details_by_id
    roots = [item for item in inventory.tree_rows if not _parent_span_id(item)]
    root = roots[0]
    if _required_str(root, "span_name", role="agent") != expected.root_span_name:
        raise TraceContractError("E_AGENT_ROOT_NAME", role="agent")
    if _required_str(root, "log_type", role="agent").lower() != "workflow":
        raise TraceContractError("E_AGENT_ROOT_TYPE", role="agent")
    workflow_name = str(root.get("span_workflow_name", "")).strip()
    trace_group = str(inventory.trace.get("trace_group_identifier", "")).strip()
    if workflow_name != expected.run_id or trace_group != expected.run_id:
        raise TraceContractError("E_AGENT_WORKFLOW_IDENTITY", role="agent")

    root_span_id = _span_id(root)
    direct_children = [
        item for item in inventory.tree_rows if _parent_span_id(item) == root_span_id
    ]
    agent_children = [
        item
        for item in direct_children
        if item.get("span_name") == "agent.respan-integration-agent"
        and str(item.get("log_type", "")).lower() == "chat"
    ]
    if len(agent_children) != 1 or len(direct_children) != 1:
        raise TraceContractError("E_AGENT_CHAT_SHAPE", role="agent")
    chat_tree = agent_children[0]
    chat = details[_record_id(chat_tree)]
    if not _has_content(_span_input(chat)) or not _has_content(_span_output(chat)):
        raise TraceNotReady("T_AGENT_CHAT_CONTENT_NOT_READY", role="agent")
    prompt = _content_text(_span_input(chat))
    required_prompt_fragments = (
        expected.run_id,
        "mode=auto",
        expected.target_service_name,
        expected.target_environment,
        f"respan-ai=={expected.respan_ai_version}",
        f"opentelemetry-instrumentation-openai=={expected.openai_otel_version}",
        "references/tracing.md",
        "do not use the web or install anything",
        "change only app.py and requirements.txt",
    )
    lowered_prompt = prompt.lower()
    if any(fragment.lower() not in lowered_prompt for fragment in required_prompt_fragments):
        raise TraceContractError("E_AGENT_PROMPT_CONTRACT", role="agent")

    chat_span_id = _span_id(chat_tree)
    tools = [
        item for item in inventory.tree_rows if _parent_span_id(item) == chat_span_id
    ]
    if not tools:
        raise TraceContractError("E_AGENT_TOOL_INVENTORY_EMPTY", role="agent")
    if len(tools) != len(inventory.tree_rows) - 2:
        raise TraceContractError("E_AGENT_NON_TOOL_DESCENDANT", role="agent")
    ordered_tools = sorted(tools, key=lambda item: _parse_datetime(item.get("start_time")))
    names: list[str] = []
    for item in ordered_tools:
        if str(item.get("log_type", "")).lower() != "tool":
            raise TraceContractError("E_AGENT_CHILD_NOT_TOOL", role="agent")
        name = _tool_name(_required_str(item, "span_name", role="agent"))
        if name not in _ALLOWED_AGENT_TOOLS:
            raise TraceContractError("E_AGENT_FORBIDDEN_TOOL", role="agent")
        detail = details[_record_id(item)]
        if not _has_content(_span_input(detail)) or not _has_content(_span_output(detail)):
            raise TraceNotReady("T_AGENT_TOOL_CONTENT_NOT_READY", role="agent")
        if not _parseable_nonempty(_span_input(detail)) or not _parseable_nonempty(_span_output(detail)):
            raise TraceContractError("E_AGENT_TOOL_CONTENT", role="agent")
        names.append(name)
    if names[0] != "Skill":
        raise TraceContractError("E_AGENT_SKILL_NOT_FIRST", role="agent")
    first_skill = details[_record_id(ordered_tools[0])]
    if _find_values(_semantic_value(_span_input(first_skill)), "skill") != ["respan"]:
        raise TraceContractError("E_AGENT_SKILL_INPUT", role="agent")

    edit_indexes = [index for index, name in enumerate(names) if name == "Edit"]
    tracing_reads = [
        index
        for index, (name, row) in enumerate(zip(names, ordered_tools, strict=True))
        if name == "Read"
        and _is_tracing_reference_read(_span_input(details[_record_id(row)]))
    ]
    if not edit_indexes or not tracing_reads or min(tracing_reads) >= min(edit_indexes):
        raise TraceContractError("E_AGENT_TOOL_ORDER", role="agent")

    edited: set[str] = set()
    for index in edit_indexes:
        detail = details[_record_id(ordered_tools[index])]
        paths = _extract_edit_paths(_semantic_value(_span_input(detail)))
        if len(paths) != 1:
            raise TraceContractError("E_AGENT_EDIT_PATH", role="agent")
        edited.add(_resolve_edit_path(paths[0], expected.checkout_root))
    if edited != set(expected.edited_paths):
        raise TraceContractError("E_AGENT_EDIT_SET", role="agent")

    prompt_tokens, completion_tokens, total_tokens = _token_triplet(chat, role="agent")
    if chat.get("model") in (None, ""):
        raise TraceNotReady("T_AGENT_MODEL_NOT_READY", role="agent")
    if _required_str(chat, "model", role="agent") != expected.model:
        raise TraceContractError("E_AGENT_MODEL", role="agent")
    listed_chat = next(
        item
        for item in inventory.list_rows
        if _record_id(item) == _record_id(chat_tree)
    )
    if _token_triplet(listed_chat, role="agent") != (
        prompt_tokens,
        completion_tokens,
        total_tokens,
    ):
        raise TraceContractError("E_AGENT_LIST_TOKENS", role="agent")
    if _required_int(inventory.trace, "llm_call_count", role="agent") != 1:
        raise TraceContractError("E_AGENT_LLM_CALL_COUNT", role="agent")
    if (
        _required_int(inventory.trace, "total_prompt_tokens", role="agent")
        != prompt_tokens
        or _required_int(inventory.trace, "total_completion_tokens", role="agent")
        != completion_tokens
        or _required_int(inventory.trace, "total_tokens", role="agent") != total_tokens
    ):
        raise TraceContractError("E_AGENT_AGGREGATE_TOKENS", role="agent")

    response_cost = _metadata_lookup(chat, "response_cost")
    if response_cost in (None, "", 0, "0", "0.0"):
        raise TraceNotReady("T_AGENT_COST_NOT_READY", role="agent")
    canonical_cost = _metadata_number(chat, "response_cost", role="agent")
    if canonical_cost <= 0 or not _cost_close(
        canonical_cost,
        expected.sdk_cost_usd,
        policy=policy,
    ):
        raise TraceContractError("E_AGENT_CANONICAL_COST", role="agent")
    if any(
        _required_number(item, "cost", role="agent") != 0
        for item in (chat_tree, listed_chat, chat)
    ):
        raise TraceContractError("E_AGENT_PROJECTED_COST", role="agent")
    aggregate_cost = _required_number(inventory.trace, "total_cost", role="agent")
    checks = [TraceCheck("P_AGENT_TRACE_CONTRACT", "pass", "agent")]
    if aggregate_cost == 0:
        checks.append(TraceCheck("W_AGENT_AGGREGATE_COST", "warn", "agent"))
    elif not _cost_close(aggregate_cost, canonical_cost, policy=policy):
        raise TraceContractError("E_AGENT_AGGREGATE_COST", role="agent")

    tree_prompt = _optional_int(chat_tree.get("prompt_tokens"))
    tree_completion = _optional_int(chat_tree.get("completion_tokens"))
    tree_total = _optional_int(chat_tree.get("total_request_tokens"))
    if (tree_prompt, tree_completion, tree_total) == (0, 0, 0):
        checks.append(TraceCheck("W_AGENT_TREE_USAGE", "warn", "agent"))
    elif (tree_prompt, tree_completion, tree_total) != (
        prompt_tokens,
        completion_tokens,
        total_tokens,
    ):
        raise TraceContractError("E_AGENT_TREE_USAGE", role="agent")

    _validate_canonical_metadata(
        chat,
        role="agent",
        service=expected.service_name,
        environment=expected.environment,
        scope_name=expected.instrumentation_scope_name,
        scope_version=expected.instrumentation_scope_version,
    )
    provider_system = _metadata_lookup(chat, "gen_ai.system")
    if provider_system in (None, ""):
        raise TraceNotReady("T_AGENT_PROVIDER_NOT_READY", role="agent")
    if provider_system != expected.provider_system:
        raise TraceContractError("E_AGENT_PROVIDER", role="agent")
    return checks


def _validate_target(
    hydrated: _HydratedTrace,
    *,
    expected: TargetTraceExpectation,
    policy: PollingPolicy,
) -> list[TraceCheck]:
    inventory = hydrated.inventory
    if inventory.observation.span_count != 1 or len(hydrated.details) != 1:
        raise TraceContractError("E_TARGET_SPAN_COUNT", role="target")
    tree = inventory.tree_rows[0]
    detail = hydrated.details[0]
    listed = inventory.list_rows[0]
    if detail.get("model") in (None, ""):
        raise TraceNotReady("T_TARGET_MODEL_NOT_READY", role="target")
    if _parent_span_id(tree):
        raise TraceContractError("E_TARGET_ROOT_PARENT", role="target")
    for item in (tree, listed, detail):
        if _required_str(item, "span_name", role="target") != expected.span_name:
            raise TraceContractError("E_TARGET_SPAN_NAME", role="target")
        if _required_str(item, "log_type", role="target").lower() != "chat":
            raise TraceContractError("E_TARGET_OPERATION", role="target")
        if _required_str(item, "model", role="target") != expected.model:
            raise TraceContractError("E_TARGET_MODEL", role="target")
    if detail.get("provider_id") in (None, ""):
        raise TraceNotReady("T_TARGET_PROVIDER_NOT_READY", role="target")
    provider = _required_str(detail, "provider_id", role="target").lower()
    if provider != expected.provider.lower():
        raise TraceContractError("E_TARGET_PROVIDER", role="target")

    if not _has_content(detail.get("input")):
        raise TraceNotReady("T_TARGET_INPUT_NOT_READY", role="target")
    messages = detail.get("prompt_messages")
    if messages is None:
        raise TraceNotReady("T_TARGET_PROMPT_MESSAGES_NOT_READY", role="target")
    if not isinstance(messages, list) or len(messages) != 1:
        raise TraceContractError("E_TARGET_INPUT_SHAPE", role="target")
    message = messages[0]
    if not isinstance(message, Mapping):
        raise TraceContractError("E_TARGET_INPUT_ROLE", role="target")
    if message.get("role") in (None, ""):
        raise TraceNotReady("T_TARGET_INPUT_ROLE_NOT_READY", role="target")
    if str(message.get("role")).lower() != "user":
        raise TraceContractError("E_TARGET_INPUT_ROLE", role="target")
    expected_request = f"Return exactly this text and nothing else: {expected.run_id}"
    if _content_text(message.get("content")).strip() != expected_request:
        raise TraceContractError("E_TARGET_INPUT_MARKER", role="target")

    if not _has_content(detail.get("output")):
        raise TraceNotReady("T_TARGET_OUTPUT_NOT_READY", role="target")
    completion = detail.get("completion_message")
    if completion is None:
        raise TraceNotReady("T_TARGET_COMPLETION_MESSAGE_NOT_READY", role="target")
    if isinstance(completion, Mapping):
        if completion.get("role") in (None, ""):
            raise TraceNotReady("T_TARGET_OUTPUT_ROLE_NOT_READY", role="target")
        if str(completion.get("role")).lower() != "assistant":
            raise TraceContractError("E_TARGET_OUTPUT_ROLE", role="target")
        if completion.get("finish_reason") in (None, ""):
            raise TraceNotReady("T_TARGET_FINISH_NOT_READY", role="target")
        if completion.get("finish_reason") != "stop":
            raise TraceContractError("E_TARGET_FINISH_REASON", role="target")
        completion = completion.get("content")
    if _content_text(completion).strip() != expected.run_id:
        raise TraceContractError("E_TARGET_OUTPUT_MARKER", role="target")

    prompt_tokens, completion_tokens, total_tokens = _token_triplet(detail, role="target")
    if _token_triplet(tree, role="target") != (
        prompt_tokens,
        completion_tokens,
        total_tokens,
    ) or _token_triplet(listed, role="target") != (
        prompt_tokens,
        completion_tokens,
        total_tokens,
    ):
        raise TraceContractError("E_TARGET_CROSS_VIEW_TOKENS", role="target")
    if (
        _required_int(inventory.trace, "total_prompt_tokens", role="target")
        != prompt_tokens
        or _required_int(inventory.trace, "total_completion_tokens", role="target")
        != completion_tokens
        or _required_int(inventory.trace, "total_tokens", role="target") != total_tokens
        or _required_int(inventory.trace, "llm_call_count", role="target") != 1
    ):
        raise TraceContractError("E_TARGET_AGGREGATE_TOKENS", role="target")

    detail_cost = _positive_cost(detail, role="target")
    list_cost = _positive_cost(listed, role="target")
    tree_cost = _positive_cost(tree, role="target")
    aggregate_cost = _positive_cost(inventory.trace, key="total_cost", role="target")
    if not all(
        _cost_close(detail_cost, value, policy=policy)
        for value in (list_cost, tree_cost, aggregate_cost)
    ):
        raise TraceContractError("E_TARGET_COST", role="target")

    _validate_canonical_metadata(
        detail,
        role="target",
        service=expected.service_name,
        environment=expected.environment,
        scope_name=expected.instrumentation_scope_name,
        scope_version=expected.instrumentation_scope_version,
    )
    api_base = _metadata_lookup(detail, "gen_ai.openai.api_base")
    if api_base in (None, ""):
        raise TraceNotReady("T_TARGET_API_ORIGIN_NOT_READY", role="target")
    if (
        not isinstance(api_base, str)
        or api_base.rstrip("/") != expected.official_api_base.rstrip("/")
    ):
        raise TraceContractError("E_TARGET_API_ORIGIN", role="target")
    return [
        TraceCheck("P_TARGET_TRACE_CONTRACT", "pass", "target"),
        TraceCheck("W_TARGET_SPAN_CONTRACT", "warn", "target"),
    ]


def _validate_hierarchy(
    rows: Sequence[Mapping[str, Any]],
    *,
    role: Literal["agent", "target"],
) -> Mapping[str, Any]:
    span_ids = [_span_id(item) for item in rows]
    _require_unique(span_ids, "E_DUPLICATE_SPAN_ID", role)
    by_span_id = dict(zip(span_ids, rows, strict=True))
    for row in rows:
        parent = _parent_span_id(row)
        if parent and parent not in by_span_id:
            raise TraceContractError("E_ORPHAN_SPAN", role=role)

    for span_id, row in by_span_id.items():
        seen = {span_id}
        parent = _parent_span_id(row)
        while parent:
            if parent in seen:
                raise TraceContractError("E_SPAN_CYCLE", role=role)
            seen.add(parent)
            parent_row = by_span_id.get(parent)
            if parent_row is None:
                raise TraceContractError("E_ORPHAN_SPAN", role=role)
            parent = _parent_span_id(parent_row)
    roots = [item for item in rows if not _parent_span_id(item)]
    if len(roots) != 1:
        raise TraceContractError("E_ROOT_COUNT", role=role)
    return roots[0]


def _validate_tree_nesting(
    roots: Sequence[Any],
    *,
    role: Literal["agent", "target"],
) -> None:
    def visit(value: Mapping[str, Any], expected_parent: str | None) -> None:
        if _parent_span_id(value) != expected_parent:
            raise TraceContractError("E_TREE_PARENT_DISAGREEMENT", role=role)
        children = value.get("children", [])
        assert isinstance(children, list)  # validated by _flatten_tree
        for child in children:
            assert isinstance(child, Mapping)  # validated by _flatten_tree
            visit(child, _span_id(value))

    for root in roots:
        assert isinstance(root, Mapping)  # validated by _flatten_tree
        visit(root, None)


def _flatten_tree(
    roots: Sequence[Any],
    *,
    role: Literal["agent", "target"],
) -> tuple[Mapping[str, Any], ...]:
    flattened: list[Mapping[str, Any]] = []

    def visit(value: Any) -> None:
        if not isinstance(value, Mapping):
            raise TraceContractError("E_TREE_NODE_SCHEMA", role=role)
        flattened.append(value)
        children = value.get("children", [])
        if not isinstance(children, list):
            raise TraceContractError("E_TREE_CHILDREN_SCHEMA", role=role)
        for child in children:
            visit(child)

    for root in roots:
        visit(root)
    return tuple(flattened)


def _validate_success(record: Mapping[str, Any], *, role: str) -> None:
    status = str(record.get("status", "")).strip().lower()
    status_code = record.get("status_code")
    if status not in _SUCCESS_STATUSES:
        raise TraceContractError("E_SPAN_STATUS", role=role)
    if isinstance(status_code, bool) or not isinstance(status_code, int) or not 200 <= status_code < 300:
        raise TraceContractError("E_SPAN_STATUS_CODE", role=role)
    for key in ("error", "error_code", "error_message"):
        if record.get(key) not in (None, "", [], {}):
            raise TraceContractError("E_SPAN_ERROR_FIELD", role=role)
    if record.get("warnings") not in (None, "", [], {}):
        raise TraceContractError("E_SPAN_UNREVIEWED_WARNING", role=role)


def _validate_time_range(
    record: Mapping[str, Any],
    *,
    started_at: datetime,
    finished_at: datetime,
    skew_seconds: float,
    role: str,
) -> None:
    start_value = record.get("start_time")
    end_value = record.get("end_time", record.get("timestamp"))
    if not start_value or not end_value:
        raise TraceNotReady("T_RECORD_NOT_CLOSED", role=role)
    start = _parse_datetime(start_value)
    end = _parse_datetime(end_value)
    lower = _as_utc(started_at) - timedelta(seconds=skew_seconds)
    upper = _as_utc(finished_at) + timedelta(seconds=skew_seconds)
    if end < start:
        raise TraceContractError("E_TIME_REVERSED", role=role)
    if start < lower or end > upper:
        raise TraceContractError("E_TIME_OUTSIDE_SMOKE_WINDOW", role=role)


def _validate_marker_metadata(
    record: Mapping[str, Any],
    run_id: str,
    *,
    role: str,
    code: str,
) -> None:
    value = _metadata_lookup(record, "run_id")
    if value != run_id:
        raise TraceContractError(code, role=role)


def _validate_canonical_metadata(
    record: Mapping[str, Any],
    *,
    role: str,
    service: str,
    environment: str,
    scope_name: str,
    scope_version: str,
) -> None:
    expected = {
        "service.name": service,
        "respan.environment": environment,
        "otel.scope.name": scope_name,
        "otel.scope.version": scope_version,
    }
    for key, value in expected.items():
        observed = _metadata_lookup(record, key)
        if observed in (None, ""):
            raise TraceNotReady("T_CANONICAL_METADATA_NOT_READY", role=role)
        if observed != value:
            raise TraceContractError("E_CANONICAL_METADATA", role=role)


def _trace_id(record: Mapping[str, Any]) -> str:
    value = record.get("trace_unique_id", record.get("id"))
    if (
        not isinstance(value, str)
        or not _TRACE_ID_RE.fullmatch(value)
        or int(value, 16) == 0
    ):
        raise TraceContractError("E_INVALID_TRACE_ID", role="shared")
    return value


def _record_id(record: Mapping[str, Any]) -> str:
    value = record.get("unique_id", record.get("id"))
    if not isinstance(value, str) or not _RECORD_ID_RE.fullmatch(value):
        raise TraceContractError("E_INVALID_RECORD_ID", role="shared")
    return value


def _span_id(record: Mapping[str, Any]) -> str:
    value = record.get("span_unique_id")
    if (
        not isinstance(value, str)
        or not _SPAN_ID_RE.fullmatch(value)
        or int(value, 16) == 0
    ):
        raise TraceContractError("E_INVALID_SPAN_ID", role="shared")
    return value


def _parent_span_id(record: Mapping[str, Any]) -> str | None:
    value = record.get("span_parent_id")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise TraceContractError("E_INVALID_PARENT_SPAN_ID", role="shared")
    return value


def _required_str(record: Mapping[str, Any], key: str, *, role: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TraceContractError("E_REQUIRED_STRING_FIELD", role=role)
    return value.strip()


def _required_int(record: Mapping[str, Any], key: str, *, role: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceContractError("E_REQUIRED_INTEGER_FIELD", role=role)
    return value


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _required_number(record: Mapping[str, Any], key: str, *, role: str) -> float:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TraceContractError("E_REQUIRED_NUMBER_FIELD", role=role)
    resolved = float(value)
    if not math.isfinite(resolved):
        raise TraceContractError("E_NONFINITE_NUMBER_FIELD", role=role)
    return resolved


def _metadata_number(record: Mapping[str, Any], key: str, *, role: str) -> float:
    value = _metadata_lookup(record, key)
    try:
        resolved = float(value)
    except (TypeError, ValueError):
        resolved = None
    if resolved is None:
        raise TraceContractError("E_METADATA_NUMBER", role=role)
    if not math.isfinite(resolved):
        raise TraceContractError("E_METADATA_NUMBER", role=role)
    return resolved


def _metadata_lookup(record: Mapping[str, Any], key: str) -> Any:
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    if key in metadata:
        return metadata[key]
    current: Any = metadata
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _span_input(record: Mapping[str, Any]) -> Any:
    value = record.get("input")
    if _has_content(value):
        return value
    return record.get("prompt_messages")


def _span_output(record: Mapping[str, Any]) -> Any:
    value = record.get("output")
    if _has_content(value):
        return value
    return record.get("completion_message", record.get("completion_messages"))


def _semantic_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            decoded = json.loads(stripped)
        except (json.JSONDecodeError, TypeError):
            return stripped
        return _semantic_value(decoded, depth=depth + 1)
    if isinstance(value, Mapping):
        return {str(key): _semantic_value(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_semantic_value(item, depth=depth + 1) for item in value]
    return value


def _has_content(value: Any) -> bool:
    semantic = _semantic_value(value)
    if semantic in (None, "", [], {}):
        return False
    if isinstance(semantic, str):
        return bool(semantic.strip())
    return True


def _parseable_nonempty(value: Any) -> bool:
    if not _has_content(value):
        return False
    if isinstance(value, str):
        try:
            json.loads(value)
        except json.JSONDecodeError:
            return False
    return True


def _content_text(value: Any) -> str:
    semantic = _semantic_value(value)
    if isinstance(semantic, str):
        return semantic
    return json.dumps(semantic, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _find_values(value: Any, key: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, Mapping):
        for current_key, current_value in value.items():
            if current_key == key:
                found.append(current_value)
            found.extend(_find_values(current_value, key))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_values(item, key))
    return found


def _extract_edit_paths(value: Any) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "filePath", "path"):
        for item in _find_values(value, key):
            if isinstance(item, str) and item.strip():
                paths.append(item.strip())
    return list(dict.fromkeys(paths))


def _is_tracing_reference_read(value: Any) -> bool:
    paths = _extract_edit_paths(_semantic_value(value))
    return len(paths) == 1 and PurePosixPath(paths[0]).parts[-2:] == (
        "references",
        "tracing.md",
    )


def _resolve_edit_path(raw_path: str, checkout_root: Path | None) -> str:
    path = PurePosixPath(raw_path)
    if any(part in ("", ".", "..") for part in path.parts):
        raise TraceContractError("E_AGENT_EDIT_PATH", role="agent")
    if checkout_root is not None and path.is_absolute():
        root = PurePosixPath(checkout_root.as_posix())
        if not path.is_relative_to(root):
            raise TraceContractError("E_AGENT_EDIT_OUTSIDE_CHECKOUT", role="agent")
        path = path.relative_to(root)
    elif checkout_root is None and path.is_absolute():
        # run_session intentionally destroys its private clone before returning.  The
        # retained patch gate proves the changed-file set separately, so the backend
        # evidence can safely normalize only a single reviewed filename suffix here.
        path = PurePosixPath(path.name)
    if not path.parts or len(path.parts) != 1:
        raise TraceContractError("E_AGENT_EDIT_PATH", role="agent")
    return path.as_posix()


def _tool_name(span_name: str) -> str:
    if not span_name.startswith("tool.") or span_name.count(".") != 1:
        raise TraceContractError("E_AGENT_TOOL_NAME", role="agent")
    return span_name.removeprefix("tool.")


def _token_triplet(record: Mapping[str, Any], *, role: str) -> tuple[int, int, int]:
    if any(
        record.get(key) in (None, 0)
        for key in ("prompt_tokens", "completion_tokens", "total_request_tokens")
    ):
        raise TraceNotReady("T_TOKEN_USAGE_NOT_READY", role=role)
    prompt = _required_int(record, "prompt_tokens", role=role)
    completion = _required_int(record, "completion_tokens", role=role)
    total = _required_int(record, "total_request_tokens", role=role)
    if prompt <= 0 or completion <= 0 or total != prompt + completion:
        raise TraceContractError("E_TOKEN_ACCOUNTING", role=role)
    return prompt, completion, total


def _positive_cost(
    record: Mapping[str, Any],
    *,
    role: str,
    key: str = "cost",
) -> float:
    if record.get(key) in (None, 0, 0.0):
        raise TraceNotReady(f"T_{role.upper()}_COST_NOT_READY", role=role)
    value = _required_number(record, key, role=role)
    if value <= 0:
        raise TraceContractError("E_NONPOSITIVE_COST", role=role)
    return value


def _cost_close(left: float, right: float, *, policy: PollingPolicy) -> bool:
    return math.isclose(
        left,
        right,
        rel_tol=policy.cost_relative_tolerance,
        abs_tol=policy.cost_absolute_tolerance,
    )


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TraceContractError("E_INVALID_TIMESTAMP", role="shared")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
    if parsed is None:
        raise TraceContractError("E_INVALID_TIMESTAMP", role="shared")
    if parsed.tzinfo is None:
        raise TraceContractError("E_NAIVE_TIMESTAMP", role="shared")
    return parsed.astimezone(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("smoke timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_expectation_window(started_at: datetime, finished_at: datetime) -> None:
    if _as_utc(finished_at) < _as_utc(started_at):
        raise ValueError("smoke_finished_at must not precede smoke_started_at")


def _require_unique(values: Sequence[str], code: str, role: str) -> None:
    if len(values) != len(set(values)):
        raise TraceContractError(code, role=role)


def _scan_for_secrets(value: Any, *, secrets: tuple[str, ...], role: str) -> None:
    texts = (_safe_json(value), _safe_json(_semantic_value(value)))
    if any(secret and secret in text for secret in secrets for text in texts):
        raise TraceSecretExposureError("E_EXACT_SECRET_EXPOSURE", role=role)
    if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS for text in texts):
        raise TraceSecretExposureError("E_CREDENTIAL_SHAPE_EXPOSURE", role=role)


def _expanded_secrets(secret_values: Sequence[str]) -> tuple[str, ...]:
    expanded: set[str] = set()
    for raw in secret_values:
        if not isinstance(raw, str):
            raise TypeError("secret_values must contain strings")
        if not raw:
            continue
        standard_base64 = base64.b64encode(raw.encode()).decode()
        urlsafe_base64 = base64.urlsafe_b64encode(raw.encode()).decode()
        utf16_units = [
            int.from_bytes(raw.encode("utf-16-be")[index : index + 2], "big")
            for index in range(0, len(raw.encode("utf-16-be")), 2)
        ]
        unicode_escaped = "".join(f"\\u{unit:04x}" for unit in utf16_units)
        unicode_escaped_upper = "".join(f"\\u{unit:04X}" for unit in utf16_units)
        expanded.update(
            {
                raw,
                quote(raw, safe=""),
                quote_plus(raw, safe=""),
                json.dumps(raw)[1:-1],
                standard_base64,
                standard_base64.rstrip("="),
                urlsafe_base64,
                urlsafe_base64.rstrip("="),
                unicode_escaped,
                unicode_escaped_upper,
                json.dumps(unicode_escaped)[1:-1],
                json.dumps(unicode_escaped_upper)[1:-1],
            }
        )
    return tuple(sorted(expanded, key=len, reverse=True))


def _safe_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        encoded = None
    if encoded is None:
        # Platform responses are JSON-native by contract.  Keep any unexpected type out
        # of the exception while still turning it into a stable schema failure.
        raise BackendSchemaError("trace_gate")
    return encoded


def _bounded_retry_after(value: Any, policy: PollingPolicy) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    if not math.isfinite(float(value)) or value <= 0:
        return 0.0
    return min(float(value), policy.max_retry_after_seconds)


def _ensure_deadline_remaining(
    clock: Callable[[], float],
    deadline: float,
    *,
    role: str,
    phase: str,
) -> None:
    if clock() >= deadline:
        raise TraceNotReady(f"T_DEADLINE_DURING_{phase}", role=role)


def _retry_directive(
    error: TraceNotReady
    | BackendNotFoundError
    | BackendRateLimitError
    | BackendTransportError,
    policy: PollingPolicy,
) -> tuple[str, float]:
    if isinstance(error, TraceNotReady):
        return error.code, 0.0
    if isinstance(error, BackendRateLimitError):
        return (
            "T_BACKEND_RATE_LIMITED",
            _bounded_retry_after(error.retry_after, policy),
        )
    if isinstance(error, BackendNotFoundError):
        return "T_BACKEND_RECORD_NOT_FOUND", 0.0
    return "T_BACKEND_TRANSPORT", 0.0


def _trace_url(trace_id: str) -> str:
    return f"https://platform.respan.ai/platform/traces?trace_unique_id={trace_id}"
