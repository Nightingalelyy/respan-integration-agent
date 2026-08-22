from __future__ import annotations

import base64
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from respan_integration_agent.agent import DEFAULT_AGENT_MODEL
from respan_integration_agent.platform import (
    BackendNotFoundError,
    BackendRateLimitError,
    BackendRedirectError,
    BackendSchemaError,
    BackendTransportError,
)
from respan_integration_agent.trace_gate import (
    AgentTraceExpectation,
    PollingPolicy,
    TargetTraceExpectation,
    TraceAmbiguityError,
    TraceContractError,
    TraceDeadlineExceeded,
    TraceSecretExposureError,
    poll_and_verify_smoke_traces,
)


AGENT_RUN = "respan-v0a-agent-test0001"
TARGET_RUN = "respan-v0a-target-test0001"
AGENT_TRACE = "a" * 32
TARGET_TRACE = "b" * 32
START = datetime(2026, 8, 22, 1, 0, 0, tzinfo=timezone.utc)
FINISH = datetime(2026, 8, 22, 1, 1, 0, tzinfo=timezone.utc)
SPAN_START = "2026-08-22T01:00:01Z"
SPAN_END = "2026-08-22T01:00:02Z"
CHECKOUT = Path("/private/tmp/respan-v0a-checkout")


def _metadata(run_id: str, service: str, environment: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "service.name": service,
        "respan.environment": environment,
    }


def _row(
    *,
    index: int,
    trace_id: str,
    span_id: str,
    parent_id: str = "",
    name: str,
    log_type: str,
    metadata: dict[str, Any],
    input_value: Any = "",
    output_value: Any = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost: float = 0.0,
    model: str = "",
) -> dict[str, Any]:
    record_id = f"{index:032x}"
    return {
        "id": record_id,
        "unique_id": record_id,
        "span_unique_id": span_id,
        "span_parent_id": parent_id,
        "trace_unique_id": trace_id,
        "span_name": name,
        "span_workflow_name": "",
        "log_type": log_type,
        "start_time": SPAN_START,
        "timestamp": SPAN_END,
        "end_time": SPAN_END,
        "latency": 1.0,
        "status": "success",
        "status_code": 200,
        "error": "",
        "error_code": "",
        "error_message": "",
        "blurred": False,
        "input": input_value,
        "output": output_value,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_request_tokens": total_tokens,
        "cost": cost,
        "model": model,
        "metadata": metadata,
        "children": [],
    }


def _agent_payload() -> dict[str, Any]:
    base_metadata = _metadata(AGENT_RUN, "respan-integration-agent", "onboarding")
    root = _row(
        index=1,
        trace_id=AGENT_TRACE,
        span_id="0000000000000001",
        name="workflow",
        log_type="workflow",
        metadata=base_metadata,
    )
    root["span_workflow_name"] = AGENT_RUN
    prompt = " ".join(
        [
            AGENT_RUN,
            "mode=auto",
            "respan-v0a-python-smoke",
            "smoke",
            "respan-ai==4.1.0",
            "opentelemetry-instrumentation-openai==0.62.3",
            "references/tracing.md",
            "Do not use the web or install anything.",
            "Change only app.py and requirements.txt.",
        ]
    )
    chat_metadata = {
        **base_metadata,
        "otel.scope.name": "opentelemetry.instrumentation.claude_agent_sdk",
        "otel.scope.version": "0.1.4",
        "gen_ai.system": "anthropic",
        "response_cost": "0.105432",
    }
    chat = _row(
        index=2,
        trace_id=AGENT_TRACE,
        span_id="0000000000000002",
        parent_id=root["span_unique_id"],
        name="agent.respan-integration-agent",
        log_type="chat",
        metadata=chat_metadata,
        input_value=json.dumps(prompt),
        output_value=json.dumps({"summary": "integrated"}),
        model=DEFAULT_AGENT_MODEL,
    )
    tool_specs = [
        (3, "Skill", {"skill": "respan", "args": ""}),
        (4, "Read", {"file_path": "/reviewed/references/tracing.md"}),
        (5, "Edit", {"file_path": str(CHECKOUT / "app.py")}),
        (6, "Edit", {"file_path": str(CHECKOUT / "requirements.txt")}),
    ]
    tools = []
    for index, tool_name, tool_input in tool_specs:
        metadata = {
            **base_metadata,
            "otel.scope.name": "opentelemetry.instrumentation.claude_agent_sdk",
            "otel.scope.version": "0.1.4",
            "gen_ai.tool.call.id": f"call-{index}",
            "gen_ai.tool.call.type": "function",
        }
        tools.append(
            _row(
                index=index,
                trace_id=AGENT_TRACE,
                span_id=f"{index:016x}",
                parent_id=chat["span_unique_id"],
                name=f"tool.{tool_name}",
                log_type="tool",
                metadata=metadata,
                input_value=json.dumps(tool_input),
                output_value=json.dumps({"ok": True}),
            )
        )
    chat["children"] = copy.deepcopy(tools)
    root["children"] = [copy.deepcopy(chat)]
    tree_rows = [root, chat, *tools]
    list_rows = copy.deepcopy(tree_rows)
    for row in list_rows:
        row.pop("children", None)
    listed_chat = next(row for row in list_rows if row["span_name"].startswith("agent."))
    listed_chat.update(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_request_tokens": 15,
        }
    )
    details = copy.deepcopy(list_rows)
    detail_chat = next(row for row in details if row["span_name"].startswith("agent."))
    detail_chat.update(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_request_tokens": 15,
            "cost": 0.0,
        }
    )
    trace = {
        "id": AGENT_TRACE,
        "trace_unique_id": AGENT_TRACE,
        "root_span_unique_id": root["span_unique_id"],
        "start_time": SPAN_START,
        "end_time": SPAN_END,
        "duration": 1.0,
        "span_count": len(tree_rows),
        "llm_call_count": 1,
        "total_cost": 0.0,
        "total_prompt_tokens": 10,
        "total_completion_tokens": 5,
        "total_tokens": 15,
        "error_count": 0,
        "name": AGENT_RUN,
        "environment": "prod",
        "trace_group_identifier": AGENT_RUN,
        "metadata": base_metadata,
        "span_tree": [copy.deepcopy(root)],
    }
    return {
        "marker": {key: copy.deepcopy(value) for key, value in trace.items() if key != "span_tree"},
        "trace": trace,
        "list": list_rows,
        "details": {row["id"]: row for row in details},
    }


def _target_payload() -> dict[str, Any]:
    metadata = {
        **_metadata(TARGET_RUN, "respan-v0a-python-smoke", "smoke"),
        "otel.scope.name": "opentelemetry.instrumentation.openai.v1",
        "otel.scope.version": "0.62.3",
        "gen_ai.openai.api_base": "https://api.respan.ai/api/",
    }
    messages = [
        {
            "role": "user",
            "content": f"Return exactly this text and nothing else: {TARGET_RUN}",
        }
    ]
    completion = {"role": "assistant", "content": TARGET_RUN, "finish_reason": "stop"}
    root = _row(
        index=101,
        trace_id=TARGET_TRACE,
        span_id="0000000000000065",
        name="llm.gpt-4o-mini",
        log_type="chat",
        metadata=metadata,
        input_value=json.dumps(messages),
        output_value=json.dumps(completion),
        prompt_tokens=8,
        completion_tokens=4,
        total_tokens=12,
        cost=0.000012,
        model="gpt-4o-mini",
    )
    root["provider_id"] = "openai"
    detail = copy.deepcopy(root)
    detail.pop("children", None)
    detail["prompt_messages"] = messages
    detail["completion_message"] = completion
    listed = copy.deepcopy(detail)
    trace = {
        "id": TARGET_TRACE,
        "trace_unique_id": TARGET_TRACE,
        "root_span_unique_id": root["span_unique_id"],
        "start_time": SPAN_START,
        "end_time": SPAN_END,
        "duration": 1.0,
        "span_count": 1,
        "llm_call_count": 1,
        "total_cost": 0.000012,
        "total_prompt_tokens": 8,
        "total_completion_tokens": 4,
        "total_tokens": 12,
        "error_count": 0,
        "name": "llm.gpt-4o-mini",
        "environment": "prod",
        "metadata": _metadata(TARGET_RUN, "respan-v0a-python-smoke", "smoke"),
        "span_tree": [root],
    }
    return {
        "marker": {key: copy.deepcopy(value) for key, value in trace.items() if key != "span_tree"},
        "trace": trace,
        "list": [listed],
        "details": {detail["id"]: detail},
    }


class FakeBackend:
    def __init__(self) -> None:
        self.payloads = {AGENT_RUN: _agent_payload(), TARGET_RUN: _target_payload()}
        self.trace_to_run = {AGENT_TRACE: AGENT_RUN, TARGET_TRACE: TARGET_RUN}
        self.marker_sequences: dict[str, list[Any]] = {}
        self.method_errors: dict[str, list[Exception]] = {}
        self.calls: list[tuple[str, str]] = []

    def _raise_queued(self, method: str) -> None:
        errors = self.method_errors.get(method, [])
        if errors:
            raise errors.pop(0)

    def list_traces_by_run_id(self, run_id, start_time, end_time):
        del start_time, end_time
        self.calls.append(("list_traces", run_id))
        self._raise_queued("list_traces")
        sequence = self.marker_sequences.get(run_id)
        if sequence:
            value = sequence.pop(0)
            if isinstance(value, Exception):
                raise value
            return copy.deepcopy(value)
        return [copy.deepcopy(self.payloads[run_id]["marker"])]

    def retrieve_trace(self, trace_id):
        self.calls.append(("retrieve_trace", trace_id))
        self._raise_queued("retrieve_trace")
        return copy.deepcopy(self.payloads[self.trace_to_run[trace_id]]["trace"])

    def list_spans_by_trace_id(self, trace_id, start_time, end_time):
        del start_time, end_time
        self.calls.append(("list_spans", trace_id))
        self._raise_queued("list_spans")
        return copy.deepcopy(self.payloads[self.trace_to_run[trace_id]]["list"])

    def retrieve_span(self, unique_id):
        self.calls.append(("retrieve_span", unique_id))
        self._raise_queued("retrieve_span")
        for payload in self.payloads.values():
            if unique_id in payload["details"]:
                return copy.deepcopy(payload["details"][unique_id])
        raise BackendNotFoundError("retrieve_span", status_code=404)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _expectations(*, checkout_root: Path | None = CHECKOUT):
    return (
        AgentTraceExpectation(
            run_id=AGENT_RUN,
            trace_id=AGENT_TRACE,
            smoke_started_at=START,
            smoke_finished_at=FINISH,
            sdk_cost_usd=0.105432,
            checkout_root=checkout_root,
        ),
        TargetTraceExpectation(
            run_id=TARGET_RUN,
            smoke_started_at=START,
            smoke_finished_at=FINISH,
        ),
    )


def test_agent_trace_expectation_uses_the_pinned_execution_model_by_default():
    agent, _ = _expectations()

    assert agent.model == DEFAULT_AGENT_MODEL


def _run_gate(
    backend: FakeBackend,
    *,
    policy: PollingPolicy | None = None,
    secret_values=(),
    checkout_root: Path | None = CHECKOUT,
):
    clock = FakeClock()
    agent, target = _expectations(checkout_root=checkout_root)
    report = poll_and_verify_smoke_traces(
        backend,
        agent=agent,
        target=target,
        secret_values=secret_values,
        policy=policy,
        clock=clock,
        sleeper=clock.sleep,
    )
    return report, clock


def test_golden_traces_converge_and_report_only_sanitized_evidence():
    backend = FakeBackend()

    report, clock = _run_gate(backend, secret_values=("sentinel-exact-secret",))

    assert report.passed
    assert report.attempts == 2
    assert clock.sleeps == [1.0]
    assert report.agent_trace_id == AGENT_TRACE
    assert report.target_trace_id == TARGET_TRACE
    assert set(report.warning_codes) == {
        "W_ENVIRONMENT_PROJECTION",
        "W_AGENT_AGGREGATE_COST",
        "W_AGENT_TREE_USAGE",
        "W_TARGET_SPAN_CONTRACT",
    }
    evidence = report.to_dict()
    assert evidence["schema_version"] == "respan-v0-backend-trace-gate/v1"
    assert evidence["passed"] is True
    encoded = json.dumps(evidence, sort_keys=True)
    assert "sentinel-exact-secret" not in encoded
    assert "prompt_messages" not in encoded
    assert "respan-v0a-checkout" not in encoded


def test_not_found_then_complete_requires_two_complete_stable_observations():
    backend = FakeBackend()
    backend.marker_sequences[AGENT_RUN] = [[], [backend.payloads[AGENT_RUN]["marker"]]]

    report, clock = _run_gate(backend)

    assert report.attempts == 3
    assert clock.sleeps == [1.0, 2.0]
    assert ("list_traces", TARGET_RUN) in backend.calls


def test_shared_deadline_uses_capped_backoff_without_oversleep():
    backend = FakeBackend()
    backend.marker_sequences[AGENT_RUN] = [[], [], [], []]
    clock = FakeClock()
    agent, target = _expectations()

    with pytest.raises(TraceDeadlineExceeded) as caught:
        poll_and_verify_smoke_traces(
            backend,
            agent=agent,
            target=target,
            policy=PollingPolicy(timeout_seconds=2.5),
            clock=clock,
            sleeper=clock.sleep,
        )

    assert clock.sleeps == [1.0, 1.5]
    assert caught.value.elapsed_seconds == 2.5
    assert caught.value.unmet_codes == ("T_MARKER_NOT_INDEXED",)


def test_hydration_stops_calling_backend_when_shared_deadline_expires():
    clock = FakeClock()

    class SlowDetailBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.detail_calls = 0

        def retrieve_span(self, unique_id):
            self.detail_calls += 1
            clock.value += 0.6
            return super().retrieve_span(unique_id)

    backend = SlowDetailBackend()
    agent, target = _expectations()
    with pytest.raises(TraceDeadlineExceeded) as caught:
        poll_and_verify_smoke_traces(
            backend,
            agent=agent,
            target=target,
            policy=PollingPolicy(timeout_seconds=1.5),
            clock=clock,
            sleeper=clock.sleep,
        )

    assert backend.detail_calls == 1
    assert caught.value.elapsed_seconds == 1.6
    assert caught.value.unmet_codes == ("T_DEADLINE_DURING_HYDRATION",)


def test_inventory_stops_after_one_call_crosses_shared_deadline():
    clock = FakeClock()

    class SlowInventoryBackend(FakeBackend):
        def list_traces_by_run_id(self, run_id, start_time, end_time):
            clock.value += 2.0
            return super().list_traces_by_run_id(run_id, start_time, end_time)

    backend = SlowInventoryBackend()
    agent, target = _expectations()
    with pytest.raises(TraceDeadlineExceeded) as caught:
        poll_and_verify_smoke_traces(
            backend,
            agent=agent,
            target=target,
            policy=PollingPolicy(timeout_seconds=1.5),
            clock=clock,
            sleeper=clock.sleep,
        )

    assert backend.calls == [("list_traces", AGENT_RUN)]
    assert caught.value.elapsed_seconds == 2.0
    assert caught.value.unmet_codes == ("T_DEADLINE_DURING_INVENTORY",)


def test_rate_limit_retry_after_is_bounded_and_respects_deadline():
    backend = FakeBackend()
    backend.method_errors["list_traces"] = [
        BackendRateLimitError("list_traces", retry_after=99.0)
    ]

    report, clock = _run_gate(backend)

    assert report.attempts == 3
    assert clock.sleeps == [5.0, 2.0]


@pytest.mark.parametrize(
    "error",
    [
        BackendTransportError("retrieve_trace"),
        BackendNotFoundError("retrieve_trace", status_code=404),
    ],
)
def test_transient_backend_failures_retry(error):
    backend = FakeBackend()
    backend.method_errors["retrieve_trace"] = [error]

    report, _ = _run_gate(backend)

    assert report.attempts == 3


def test_untrusted_redirect_fails_immediately_instead_of_retrying():
    backend = FakeBackend()
    backend.method_errors["list_traces"] = [BackendRedirectError("list_traces")]

    with pytest.raises(BackendRedirectError):
        _run_gate(backend)

    assert backend.calls == [("list_traces", AGENT_RUN)]


def test_exact_marker_ambiguity_fails_immediately():
    backend = FakeBackend()
    marker = backend.payloads[AGENT_RUN]["marker"]
    backend.marker_sequences[AGENT_RUN] = [[marker, marker]]

    with pytest.raises(TraceAmbiguityError, match="E_MARKER_NOT_UNIQUE"):
        _run_gate(backend)


def test_agent_marker_must_match_locked_trace_id():
    backend = FakeBackend()
    backend.payloads[AGENT_RUN]["marker"]["trace_unique_id"] = "c" * 32
    backend.payloads[AGENT_RUN]["marker"]["id"] = "c" * 32

    with pytest.raises(TraceAmbiguityError, match="E_AGENT_MARKER_TRACE_MISMATCH"):
        _run_gate(backend)


def test_all_zero_agent_and_discovered_target_trace_ids_are_rejected():
    with pytest.raises(ValueError, match="nonzero"):
        AgentTraceExpectation(
            run_id=AGENT_RUN,
            trace_id="0" * 32,
            smoke_started_at=START,
            smoke_finished_at=FINISH,
            sdk_cost_usd=0.1,
        )

    backend = FakeBackend()
    backend.payloads[TARGET_RUN]["marker"]["id"] = "0" * 32
    backend.payloads[TARGET_RUN]["marker"]["trace_unique_id"] = "0" * 32
    with pytest.raises(TraceContractError, match="E_INVALID_TRACE_ID"):
        _run_gate(backend)


def test_target_trace_id_is_locked_after_first_complete_observation():
    backend = FakeBackend()
    marker = backend.payloads[TARGET_RUN]["marker"]
    changed = copy.deepcopy(marker)
    changed["id"] = changed["trace_unique_id"] = "c" * 32
    backend.marker_sequences[TARGET_RUN] = [[marker], [changed]]

    with pytest.raises(TraceAmbiguityError, match="E_TARGET_MARKER_TRACE_CHANGED"):
        _run_gate(backend)


@pytest.mark.parametrize("view", ["tree", "list"])
def test_duplicate_record_ids_are_rejected(view):
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    if view == "tree":
        duplicate = copy.deepcopy(payload["trace"]["span_tree"][0]["children"][0]["children"][0])
        payload["trace"]["span_tree"][0]["children"][0]["children"].append(duplicate)
        payload["trace"]["span_count"] += 1
    else:
        payload["list"].append(copy.deepcopy(payload["list"][0]))
        payload["trace"]["span_count"] += 1

    with pytest.raises(TraceContractError, match="E_DUPLICATE"):
        _run_gate(backend)


def test_partial_cross_view_inventory_retries_until_deadline():
    backend = FakeBackend()
    backend.payloads[AGENT_RUN]["list"].pop()
    policy = PollingPolicy(timeout_seconds=1.0)

    with pytest.raises(TraceDeadlineExceeded) as caught:
        _run_gate(backend, policy=policy)

    assert "T_SPAN_COUNT_DISAGREEMENT" in caught.value.unmet_codes


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("orphan", "E_ORPHAN_SPAN"),
        ("cycle", "E_SPAN_CYCLE"),
        ("multiple_root", "E_ROOT_COUNT"),
        ("root_id", "E_ROOT_SPAN_ID_MISMATCH"),
    ],
)
def test_invalid_hierarchy_is_rejected(mutation, code):
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    tree = payload["trace"]["span_tree"][0]
    if mutation == "orphan":
        tree["children"][0]["span_parent_id"] = "f" * 16
    elif mutation == "cycle":
        root_id = tree["span_unique_id"]
        chat = tree["children"][0]
        tree["span_parent_id"] = chat["span_unique_id"]
        chat["span_parent_id"] = root_id
    elif mutation == "multiple_root":
        tree["children"][0]["span_parent_id"] = ""
    else:
        payload["trace"]["root_span_unique_id"] = "f" * 16

    def sync_parents(row):
        listed = next(item for item in payload["list"] if item["id"] == row["id"])
        listed["span_parent_id"] = row["span_parent_id"]
        payload["details"][row["id"]]["span_parent_id"] = row["span_parent_id"]
        for child in row["children"]:
            sync_parents(child)

    sync_parents(tree)

    with pytest.raises(TraceContractError, match=code):
        _run_gate(backend)


@pytest.mark.parametrize("field", ["start_time", "end_time"])
def test_missing_closure_is_retryable_until_deadline(field):
    backend = FakeBackend()
    detail = next(iter(backend.payloads[AGENT_RUN]["details"].values()))
    detail[field] = ""

    with pytest.raises(TraceDeadlineExceeded) as caught:
        _run_gate(backend, policy=PollingPolicy(timeout_seconds=3.0))

    assert "T_RECORD_NOT_CLOSED" in caught.value.unmet_codes


def test_storage_content_not_ready_is_retried_and_can_converge():
    class EnrichingBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.chat_id = next(
                row["id"]
                for row in self.payloads[AGENT_RUN]["details"].values()
                if row["log_type"] == "chat"
            )
            self.chat_reads = 0

        def retrieve_span(self, unique_id):
            detail = super().retrieve_span(unique_id)
            if unique_id == self.chat_id:
                self.chat_reads += 1
                if self.chat_reads == 1:
                    detail["output"] = ""
            return detail

    backend = EnrichingBackend()

    report, clock = _run_gate(backend)

    assert report.passed
    assert report.attempts == 3
    assert clock.sleeps == [1.0, 2.0]


@pytest.mark.parametrize(
    ("role", "field", "code"),
    [
        ("agent", "tokens", "T_TOKEN_USAGE_NOT_READY"),
        ("agent", "cost", "T_AGENT_COST_NOT_READY"),
        ("agent", "metadata", "T_CANONICAL_METADATA_NOT_READY"),
        ("target", "input", "T_TARGET_INPUT_NOT_READY"),
        ("target", "output", "T_TARGET_OUTPUT_NOT_READY"),
        ("target", "cost", "T_TARGET_COST_NOT_READY"),
        ("target", "metadata", "T_CANONICAL_METADATA_NOT_READY"),
    ],
)
def test_missing_storage_enrichment_retries_to_deadline(role, field, code):
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN if role == "agent" else TARGET_RUN]
    detail = next(
        row
        for row in payload["details"].values()
        if row["log_type"] == "chat"
    )
    if field == "tokens":
        detail["prompt_tokens"] = 0
    elif field == "cost":
        if role == "agent":
            detail["metadata"]["response_cost"] = ""
        else:
            detail["cost"] = 0
    elif field == "metadata":
        detail["metadata"].pop("service.name")
    elif field == "input":
        detail["prompt_messages"] = None
        detail["input"] = ""
    else:
        detail["completion_message"] = None
        detail["output"] = ""

    with pytest.raises(TraceDeadlineExceeded) as caught:
        _run_gate(backend, policy=PollingPolicy(timeout_seconds=3.0))

    assert code in caught.value.unmet_codes


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("status", "error", "E_SPAN_STATUS"),
        ("status_code", 500, "E_SPAN_STATUS_CODE"),
        ("error_message", "failure", "E_SPAN_ERROR_FIELD"),
        ("blurred", True, "E_BLURRED_SPAN_DETAIL"),
        ("output", "Span not properly closed", "E_IMPROPERLY_CLOSED_SPAN"),
    ],
)
def test_failed_or_uninspectable_span_is_rejected(field, value, code):
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    detail = next(iter(payload["details"].values()))
    detail[field] = value
    if field in {"status", "status_code"}:
        listed = next(row for row in payload["list"] if row["id"] == detail["id"])
        listed[field] = value

        def find_tree(row):
            if row["id"] == detail["id"]:
                return row
            for child in row["children"]:
                found = find_tree(child)
                if found is not None:
                    return found
            return None

        tree = find_tree(payload["trace"]["span_tree"][0])
        assert tree is not None
        tree[field] = value

    with pytest.raises(TraceContractError, match=code):
        _run_gate(backend)


def test_tree_blur_and_unreviewed_backend_warnings_are_rejected():
    backend = FakeBackend()
    backend.payloads[AGENT_RUN]["trace"]["span_tree"][0]["blurred"] = True
    with pytest.raises(TraceContractError, match="E_BLURRED_SPAN_DETAIL"):
        _run_gate(backend)

    backend = FakeBackend()
    next(iter(backend.payloads[AGENT_RUN]["details"].values()))["warnings"] = [
        "unreviewed-warning"
    ]
    with pytest.raises(TraceContractError, match="E_SPAN_UNREVIEWED_WARNING"):
        _run_gate(backend)

    backend = FakeBackend()
    next(iter(backend.payloads[AGENT_RUN]["details"].values())).pop("blurred")
    with pytest.raises(TraceContractError, match="E_BLURRED_SPAN_DETAIL"):
        _run_gate(backend)


def test_tree_list_and_detail_core_identity_must_agree():
    backend = FakeBackend()
    detail = next(
        row
        for row in backend.payloads[AGENT_RUN]["details"].values()
        if row["span_name"] == "tool.Read"
    )
    detail["span_name"] = "tool.Grep"

    with pytest.raises(TraceContractError, match="E_CROSS_VIEW_CORE_IDENTITY"):
        _run_gate(backend)


@pytest.mark.parametrize("view", ["tree", "list"])
def test_incomplete_tree_or_list_closure_retries_to_deadline(view):
    backend = FakeBackend()
    if view == "tree":
        backend.payloads[AGENT_RUN]["trace"]["span_tree"][0]["end_time"] = ""
    else:
        backend.payloads[AGENT_RUN]["list"][0]["end_time"] = ""

    with pytest.raises(TraceDeadlineExceeded) as caught:
        _run_gate(backend, policy=PollingPolicy(timeout_seconds=1.0))

    assert caught.value.unmet_codes == ("T_INVENTORY_SPAN_NOT_CLOSED",)


def test_agent_root_name_and_flat_list_tokens_are_strict():
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    root_id = payload["trace"]["span_tree"][0]["id"]
    payload["trace"]["span_tree"][0]["span_name"] = "changed-workflow"
    next(row for row in payload["list"] if row["id"] == root_id)["span_name"] = (
        "changed-workflow"
    )
    payload["details"][root_id]["span_name"] = "changed-workflow"
    with pytest.raises(TraceContractError, match="E_AGENT_ROOT_NAME"):
        _run_gate(backend)

    backend = FakeBackend()
    listed_chat = next(
        row
        for row in backend.payloads[AGENT_RUN]["list"]
        if row["log_type"] == "chat"
    )
    listed_chat["prompt_tokens"] += 1
    listed_chat["total_request_tokens"] += 1
    with pytest.raises(TraceContractError, match="E_AGENT_LIST_TOKENS"):
        _run_gate(backend)


def test_agent_model_provider_projected_cost_and_exact_success_are_strict():
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    chat = next(row for row in payload["details"].values() if row["log_type"] == "chat")
    chat["model"] = "other"
    with pytest.raises(TraceContractError, match="E_CROSS_VIEW_MODEL|E_AGENT_MODEL"):
        _run_gate(backend)

    backend = FakeBackend()
    chat = next(
        row
        for row in backend.payloads[AGENT_RUN]["details"].values()
        if row["log_type"] == "chat"
    )
    chat["metadata"]["gen_ai.system"] = "other"
    with pytest.raises(TraceContractError, match="E_AGENT_PROVIDER"):
        _run_gate(backend)

    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    for collection in (
        payload["trace"]["span_tree"][0]["children"],
        payload["list"],
        list(payload["details"].values()),
    ):
        chat = next(row for row in collection if row["log_type"] == "chat")
        chat["cost"] = 123.0
    with pytest.raises(TraceContractError, match="E_AGENT_PROJECTED_COST"):
        _run_gate(backend)

    backend = FakeBackend()
    detail = next(iter(backend.payloads[AGENT_RUN]["details"].values()))
    detail["status"] = "unset"
    with pytest.raises(TraceContractError, match="E_CROSS_VIEW_CORE_IDENTITY|E_SPAN_STATUS"):
        _run_gate(backend)


@pytest.mark.parametrize("view", ["tree", "list"])
def test_target_tree_and_list_tokens_must_match_canonical_detail(view):
    backend = FakeBackend()
    row = backend.payloads[TARGET_RUN]["trace"]["span_tree"][0]
    if view == "list":
        row = backend.payloads[TARGET_RUN]["list"][0]
    row["prompt_tokens"] = 99
    row["completion_tokens"] = 1
    row["total_request_tokens"] = 100

    with pytest.raises(TraceContractError, match="E_TARGET_CROSS_VIEW_TOKENS"):
        _run_gate(backend)


@pytest.mark.parametrize("tool_name", ["Bash", "WebFetch", "WebSearch", "Agent", "Write"])
def test_forbidden_agent_tools_are_rejected(tool_name):
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    tool = payload["trace"]["span_tree"][0]["children"][0]["children"][2]
    record_id = tool["id"]
    tool["span_name"] = f"tool.{tool_name}"
    next(row for row in payload["list"] if row["id"] == record_id)["span_name"] = f"tool.{tool_name}"
    payload["details"][record_id]["span_name"] = f"tool.{tool_name}"

    with pytest.raises(TraceContractError, match="E_AGENT_FORBIDDEN_TOOL"):
        _run_gate(backend)


def test_skill_and_tracing_read_order_is_strict():
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    tools = payload["trace"]["span_tree"][0]["children"][0]["children"]
    tools[0]["start_time"], tools[2]["start_time"] = (
        "2026-08-22T01:00:01.900Z",
        "2026-08-22T01:00:01.100Z",
    )

    with pytest.raises(TraceContractError, match="E_AGENT_SKILL_NOT_FIRST"):
        _run_gate(backend)


def test_tracing_reference_read_requires_exact_path_suffix():
    backend = FakeBackend()
    read = next(
        row
        for row in backend.payloads[AGENT_RUN]["details"].values()
        if row["span_name"] == "tool.Read"
    )
    read["input"] = json.dumps(
        {"file_path": "/reviewed/references/tracing.md.evil"}
    )

    with pytest.raises(TraceContractError, match="E_AGENT_TOOL_ORDER"):
        _run_gate(backend)


@pytest.mark.parametrize(
    "bad_path",
    ["/private/tmp/other/app.py", "/private/tmp/respan-v0a-checkout/sub/app.py", "../app.py"],
)
def test_edit_outside_exact_checkout_files_is_rejected(bad_path):
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    edit = payload["trace"]["span_tree"][0]["children"][0]["children"][2]
    record_id = edit["id"]
    payload["details"][record_id]["input"] = json.dumps({"file_path": bad_path})

    with pytest.raises(TraceContractError, match="E_AGENT_EDIT"):
        _run_gate(backend)


def test_destroyed_checkout_fallback_accepts_only_exact_filename_suffixes():
    backend = FakeBackend()

    report, _ = _run_gate(backend, checkout_root=None)

    assert report.passed


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("name", "E_TARGET_SPAN_NAME"),
        ("model", "E_TARGET_MODEL"),
        ("provider", "E_TARGET_PROVIDER"),
        ("input", "E_TARGET_INPUT_MARKER"),
        ("output", "E_TARGET_OUTPUT_MARKER"),
        ("service", "E_CANONICAL_METADATA"),
        ("environment", "E_CANONICAL_METADATA"),
        ("scope", "E_CANONICAL_METADATA"),
        ("endpoint", "E_TARGET_API_ORIGIN"),
        ("tokens", "E_TOKEN_ACCOUNTING"),
        ("cost", "E_TARGET_COST"),
    ],
)
def test_target_contract_mutations_are_rejected(mutation, code):
    backend = FakeBackend()
    payload = backend.payloads[TARGET_RUN]
    detail = next(iter(payload["details"].values()))
    if mutation == "name":
        for row in (payload["trace"]["span_tree"][0], payload["list"][0], detail):
            row["span_name"] = "openai.chat"
    elif mutation == "model":
        for row in (payload["trace"]["span_tree"][0], payload["list"][0], detail):
            row["model"] = "other"
    elif mutation == "provider":
        detail["provider_id"] = "other"
    elif mutation == "input":
        detail["prompt_messages"][0]["content"] = "no marker"
    elif mutation == "output":
        detail["completion_message"]["content"] = "wrong"
    elif mutation == "service":
        detail["metadata"]["service.name"] = "wrong"
    elif mutation == "environment":
        detail["metadata"]["respan.environment"] = "wrong"
    elif mutation == "scope":
        detail["metadata"]["otel.scope.version"] = "9.9.9"
    elif mutation == "endpoint":
        detail["metadata"]["gen_ai.openai.api_base"] = "https://evil.invalid/api/"
    elif mutation == "tokens":
        detail["total_request_tokens"] += 1
    else:
        detail["cost"] = 0.1

    with pytest.raises(TraceContractError, match=code):
        _run_gate(backend)


@pytest.mark.parametrize(
    "leak",
    [
        "sentinel-exact-secret!",
        "sentinel-exact-secret%21",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "sk-proj-abcdefghijklmnopqrstuv",
        "ghp_abcdefghijklmnopqrstuvwxyz1234",
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN PRIVATE KEY-----",
        "eyJabcdefghijklmnop.abcdefghijklmnop.abcdefghijklmnop",
        "https://user:password@example.invalid/path",
    ],
)
def test_exact_encoded_and_shaped_credentials_fail_without_echo(leak):
    backend = FakeBackend()
    detail = next(iter(backend.payloads[AGENT_RUN]["details"].values()))
    detail["output"] = leak

    with pytest.raises(TraceSecretExposureError) as caught:
        _run_gate(backend, secret_values=("sentinel-exact-secret!",))

    assert leak not in str(caught.value)


def test_json_unicode_escaped_exact_secret_is_detected_after_decoding():
    backend = FakeBackend()
    secret = "sentinel-unicode-secret"
    escaped = '"' + "".join(f"\\u{ord(char):04x}" for char in secret) + '"'
    next(iter(backend.payloads[AGENT_RUN]["details"].values()))["output"] = escaped

    with pytest.raises(TraceSecretExposureError) as caught:
        _run_gate(backend, secret_values=(secret,))

    _assert_empty_exception_chain(caught.value)
    assert secret not in str(caught.value)


def test_double_encoded_json_unicode_escaped_secret_is_detected():
    backend = FakeBackend()
    secret = "sentinel-double-unicode-secret"
    escaped = "".join(f"\\u{ord(char):04x}" for char in secret)
    next(iter(backend.payloads[AGENT_RUN]["details"].values()))["output"] = (
        json.dumps(escaped)
    )

    with pytest.raises(TraceSecretExposureError):
        _run_gate(backend, secret_values=(secret,))


def test_unpadded_base64_exact_secret_is_detected():
    backend = FakeBackend()
    secret = "sentinel-base64-secret"
    encoded = base64.urlsafe_b64encode(secret.encode()).decode().rstrip("=")
    next(iter(backend.payloads[AGENT_RUN]["details"].values()))["output"] = encoded

    with pytest.raises(TraceSecretExposureError):
        _run_gate(backend, secret_values=(secret,))


@pytest.mark.parametrize("mutation", ["missing_role", "missing_completion"])
def test_target_completion_cannot_assume_assistant_shape(mutation):
    backend = FakeBackend()
    detail = next(iter(backend.payloads[TARGET_RUN]["details"].values()))
    if mutation == "missing_role":
        detail["completion_message"].pop("role")
        expected = "T_TARGET_OUTPUT_ROLE_NOT_READY"
    else:
        detail["completion_message"] = None
        detail["output"] = TARGET_RUN
        expected = "T_TARGET_COMPLETION_MESSAGE_NOT_READY"

    with pytest.raises(TraceDeadlineExceeded) as caught:
        _run_gate(backend, policy=PollingPolicy(timeout_seconds=3.0))

    assert expected in caught.value.unmet_codes


def test_generic_high_entropy_identifiers_are_allowed():
    backend = FakeBackend()
    detail = next(iter(backend.payloads[AGENT_RUN]["details"].values()))
    detail["metadata"]["signature"] = "z" * 80

    report, _ = _run_gate(backend)

    assert report.passed


def _assert_empty_exception_chain(error: BaseException) -> None:
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize("mutation", ["metadata_number", "timestamp", "edit_path"])
def test_contract_failures_do_not_retain_secret_bearing_exception_context(mutation):
    backend = FakeBackend()
    payload = backend.payloads[AGENT_RUN]
    sentinel = "sentinel-chain-secret"
    if mutation == "metadata_number":
        chat = next(
            row for row in payload["details"].values() if row["log_type"] == "chat"
        )
        chat["metadata"]["response_cost"] = sentinel
    elif mutation == "timestamp":
        next(iter(payload["details"].values()))["start_time"] = sentinel
    else:
        edit = next(
            row for row in payload["details"].values() if row["span_name"] == "tool.Edit"
        )
        edit["input"] = json.dumps({"file_path": f"/outside/{sentinel}/app.py"})

    with pytest.raises(TraceContractError) as caught:
        _run_gate(backend)

    _assert_empty_exception_chain(caught.value)
    assert sentinel not in str(caught.value)


def test_non_json_backend_value_has_no_secret_bearing_exception_context():
    backend = FakeBackend()
    backend.payloads[AGENT_RUN]["marker"]["unsafe"] = {object()}

    with pytest.raises(BackendSchemaError) as caught:
        _run_gate(backend)

    _assert_empty_exception_chain(caught.value)
