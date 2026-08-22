import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scripts.run_v0_smoke as smoke  # noqa: E402
from respan_integration_agent.platform import (  # noqa: E402
    BackendRedirectError,
    BackendTransportError,
)
from respan_integration_agent.trace_gate import (  # noqa: E402
    TraceContractError,
    TraceDeadlineExceeded,
)
from scripts.run_v0_smoke import (  # noqa: E402
    PYPI_INDEX_URL,
    TARGET_BASE_URL,
    TARGET_MODEL,
    _pip_env,
    _target_env,
)


def test_target_environment_does_not_inherit_unrelated_secrets(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("RESPAN_BASE_URL", "https://attacker.invalid/api")
    monkeypatch.setenv("RESPAN_SMOKE_MODEL", "unexpected-model")

    env = _target_env("respan-secret", "respan-v0a-target-test")

    assert env["RESPAN_API_KEY"] == "respan-secret"
    assert env["RESPAN_EXAMPLE_RUN_ID"] == "respan-v0a-target-test"
    assert env["RESPAN_BASE_URL"] == TARGET_BASE_URL
    assert env["RESPAN_SMOKE_MODEL"] == TARGET_MODEL
    assert "DATABASE_URL" not in env
    assert "GITHUB_TOKEN" not in env


def test_pip_environment_does_not_inherit_credentials_or_index(monkeypatch):
    monkeypatch.setenv("RESPAN_API_KEY", "respan-secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://attacker.invalid/simple")
    monkeypatch.setenv("PIP_EXTRA_INDEX_URL", "https://attacker.invalid/extra")

    env = _pip_env()

    assert "RESPAN_API_KEY" not in env
    assert "PIP_INDEX_URL" not in env
    assert "PIP_EXTRA_INDEX_URL" not in env
    assert PYPI_INDEX_URL == "https://pypi.org/simple"


def test_backend_verification_locks_both_trace_expectations_and_secret(monkeypatch):
    captured = {}
    sentinel_report = object()

    def fake_client(api_key, **kwargs):
        captured["client_key"] = api_key
        captured["client_kwargs"] = kwargs
        return "backend"

    def fake_poll(backend, **kwargs):
        captured["backend"] = backend
        captured.update(kwargs)
        return sentinel_report

    monkeypatch.setattr(smoke, "RespanPlatformClient", fake_client)
    monkeypatch.setattr(smoke, "poll_and_verify_smoke_traces", fake_poll)
    started = datetime(2026, 8, 22, 1, 2, tzinfo=timezone.utc)
    finished = started + timedelta(minutes=2)
    checkout = Path("/private/tmp/respan-v0-checkout")
    result = SimpleNamespace(
        total_cost_usd=0.125,
        run_id="respan-v0a-agent-test",
        trace_id="a" * 32,
        agent_checkout_root=checkout,
        preflight=SimpleNamespace(approved_agent_model="claude-sonnet-4-20250514"),
    )

    report = smoke._verify_backend_traces(
        api_key="sentinel-secret",
        result=result,
        target_run_id="respan-v0a-target-test",
        smoke_started_at=started,
        smoke_finished_at=finished,
    )

    assert report is sentinel_report
    assert captured["client_key"] == "sentinel-secret"
    assert captured["client_kwargs"] == {
        "connect_timeout": smoke.BACKEND_CONNECT_TIMEOUT_SECONDS,
        "read_timeout": smoke.BACKEND_READ_TIMEOUT_SECONDS,
    }
    assert captured["backend"] == "backend"
    assert captured["secret_values"] == ("sentinel-secret",)
    assert captured["agent"].trace_id == "a" * 32
    assert captured["agent"].checkout_root == checkout
    assert captured["agent"].sdk_cost_usd == 0.125
    assert captured["agent"].model == "claude-sonnet-4-20250514"
    assert captured["target"].run_id == "respan-v0a-target-test"
    assert captured["target"].model == TARGET_MODEL
    assert captured["policy"].timeout_seconds == smoke.BACKEND_GATE_TIMEOUT_SECONDS


def test_backend_failure_evidence_is_versioned_payload_free_and_redacted():
    error = TraceDeadlineExceeded(
        "E_TRACE_DEADLINE_EXCEEDED",
        attempts=4,
        elapsed_seconds=120.0,
        unmet_codes=("T_NOT_READY",),
    )

    evidence = smoke._backend_failure_evidence(error)
    serialized = json.dumps(evidence, sort_keys=True)

    assert evidence["schema_version"] == smoke.EVIDENCE_SCHEMA_VERSION
    assert evidence["verdict"] == "BACKEND_VERIFICATION_FAILED"
    assert evidence["backend_error"] == {
        "code": "E_TRACE_DEADLINE_EXCEEDED",
        "attempts": 4,
        "elapsed_seconds": 120.0,
        "unmet_codes": ["T_NOT_READY"],
    }
    assert "payload" not in serialized.lower()


def test_target_gate_precedes_backend_and_never_echoes_captured_output(monkeypatch):
    called = False

    def fake_backend(**_kwargs):
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(smoke, "_verify_backend_traces", fake_backend)
    failed_target = subprocess.CompletedProcess(
        args=["python", "app.py"],
        returncode=0,
        stdout="sentinel-secret",
        stderr="",
    )
    now = datetime.now(timezone.utc)

    try:
        smoke._validate_target_then_backend(
            target=failed_target,
            api_key="sentinel-secret",
            result=object(),
            target_run_id="respan-v0a-target-test",
            smoke_started_at=now,
            smoke_finished_at=now,
        )
    except RuntimeError as error:
        assert "sentinel-secret" not in str(error)
    else:  # pragma: no cover - regression assertion
        raise AssertionError("malformed target output was accepted")
    assert called is False

    passed_target = subprocess.CompletedProcess(
        args=["python", "app.py"],
        returncode=0,
        stdout="SMOKE_OK\n",
        stderr="",
    )
    assert (
        smoke._validate_target_then_backend(
            target=passed_target,
            api_key="sentinel-secret",
            result=object(),
            target_run_id="respan-v0a-target-test",
            smoke_started_at=now,
            smoke_finished_at=now,
        )
        is not None
    )
    assert called is True


def test_contextual_failure_evidence_contains_known_trace_links_not_payloads():
    error = TraceContractError("E_TARGET_MODEL", role="target")

    evidence = smoke._backend_failure_evidence(
        error,
        agent_run_id="respan-v0a-agent-test",
        agent_trace_id="a" * 32,
        agent_trace_url=(
            "https://platform.respan.ai/platform/traces?trace_unique_id=" + "a" * 32
        ),
        target_run_id="respan-v0a-target-test",
    )

    assert evidence["agent_trace_id"] == "a" * 32
    assert evidence["agent_trace_url"].endswith("a" * 32)
    assert evidence["target_run_id"] == "respan-v0a-target-test"
    assert set(evidence) == {
        "schema_version",
        "verdict",
        "backend_error",
        "agent_run_id",
        "agent_trace_id",
        "agent_trace_url",
        "target_run_id",
    }


def test_entrypoint_maps_backend_availability_and_contract_failures(
    monkeypatch, capsys
):
    def raise_transport():
        raise BackendTransportError("retrieve_trace", status_code=503)

    monkeypatch.setattr(smoke, "main", raise_transport)
    assert smoke.entrypoint() == 3
    availability = json.loads(capsys.readouterr().err)
    assert availability["backend_error"]["code"] == "backend_transport_error"

    def raise_contract():
        raise TraceContractError("E_TARGET_MODEL", role="target")

    monkeypatch.setattr(smoke, "main", raise_contract)
    assert smoke.entrypoint() == 4
    contract = json.loads(capsys.readouterr().err)
    assert contract["backend_error"] == {
        "code": "E_TARGET_MODEL",
        "role": "target",
    }

    def raise_redirect():
        raise BackendRedirectError("pagination", status_code=302)

    monkeypatch.setattr(smoke, "main", raise_redirect)
    assert smoke.entrypoint() == 4


class _SmokePreflightReport:
    def __init__(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        passed: bool = True,
    ) -> None:
        self.started_at = started_at
        self.finished_at = finished_at
        self.passed = passed
        self.paid_canary_performed = False
        self.approved_agent_model = "claude-sonnet-pinned"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "respan-gateway-preflight/v1",
            "passed": self.passed,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "attempts": 1,
            "paid_canary_performed": self.paid_canary_performed,
            "checks": [
                {
                    "role": "orchestration",
                    "code": "P_ROUTE_READY",
                    "status": "pass" if self.passed else "fail",
                    "resolved_model": self.approved_agent_model,
                }
            ],
        }


class _SmokeBackendReport:
    passed = True

    @staticmethod
    def to_dict() -> dict[str, object]:
        return {
            "schema_version": "respan-v0-backend-trace-gate/v1",
            "passed": True,
            "agent_trace_id": "a" * 32,
            "target_trace_id": "b" * 32,
            "checks": [],
        }


def _smoke_session_result(
    preflight: _SmokePreflightReport,
    *,
    agent_started_at: datetime,
) -> SimpleNamespace:
    return SimpleNamespace(
        summary="applied deterministic integration",
        trace_id="a" * 32,
        trace_url=(
            "https://platform.respan.ai/platform/traces?trace_unique_id=" + "a" * 32
        ),
        run_id="respan-v0a-agent-smoke-test",
        agent_session_id="session-smoke-test",
        telemetry_flushed=True,
        num_turns=2,
        duration_ms=250,
        total_cost_usd=0.1,
        changed_files=["app.py", "requirements.txt"],
        diff="diff --git a/app.py b/app.py\n",
        agent_checkout_root=Path("/private/tmp/respan-smoke-agent-checkout"),
        preflight=preflight,
        agent_started_at=agent_started_at,
        pr=None,
    )


def test_smoke_success_evidence_includes_preflight_and_proves_timestamp_order(
    monkeypatch, capsys
):
    base = datetime(2026, 8, 23, 2, 0, tzinfo=timezone.utc)
    preflight = _SmokePreflightReport(
        started_at=base + timedelta(seconds=1),
        finished_at=base + timedelta(seconds=2),
    )
    result = _smoke_session_result(
        preflight, agent_started_at=base + timedelta(seconds=3)
    )
    events: list[str] = []

    class FrozenDatetime:
        calls = 0

        @classmethod
        def now(cls, tz=None):
            value = base + timedelta(seconds=cls.calls * 10)
            cls.calls += 1
            return value

    def fake_run_session(*_args, **_kwargs):
        events.append("session")
        return result

    def fake_run(args, **_kwargs):
        if args[-1] == "app.py":
            events.append("target")
            return subprocess.CompletedProcess(args, 0, "SMOKE_OK\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_backend(**_kwargs):
        events.append("backend")
        return _SmokeBackendReport()

    monkeypatch.setattr(smoke, "datetime", FrozenDatetime)
    monkeypatch.setattr(smoke, "_respan_api_key", lambda: "sentinel-secret")
    monkeypatch.setattr(smoke, "_init_fixture_repo", lambda _path: "c" * 40)
    monkeypatch.setattr(smoke, "run_session", fake_run_session)
    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(smoke, "verify_integration", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smoke, "_validate_target_then_backend", fake_backend)

    assert smoke.main() == 0
    stdout = capsys.readouterr().out
    evidence = json.loads(stdout.split("\n\n--- accepted patch ---", 1)[0])

    assert events == ["session", "target", "backend"]
    assert evidence["gateway_preflight"] == preflight.to_dict()
    assert evidence["gateway_preflight"]["passed"] is True
    assert evidence["gateway_preflight"]["paid_canary_performed"] is False
    assert evidence["agent_started_at"] == result.agent_started_at.isoformat()
    assert (
        datetime.fromisoformat(evidence["gateway_preflight"]["finished_at"])
        <= datetime.fromisoformat(evidence["agent_started_at"])
        <= datetime.fromisoformat(evidence["capture_finished_at"])
    )
    serialized = json.dumps(evidence, sort_keys=True)
    assert "sentinel-secret" not in serialized
    assert "response_body" not in serialized


@pytest.mark.parametrize(
    ("passed", "preflight_finished_offset"),
    [(False, 2), (True, 4)],
    ids=["nonpassing-report", "preflight-finishes-after-agent-start"],
)
def test_smoke_rejects_bad_preflight_before_target_or_backend(
    monkeypatch, passed, preflight_finished_offset
):
    base = datetime(2026, 8, 23, 3, 0, tzinfo=timezone.utc)
    preflight = _SmokePreflightReport(
        started_at=base + timedelta(seconds=1),
        finished_at=base + timedelta(seconds=preflight_finished_offset),
        passed=passed,
    )
    result = _smoke_session_result(
        preflight, agent_started_at=base + timedelta(seconds=3)
    )
    calls = {"session": 0, "target": 0, "backend": 0}

    class FrozenDatetime:
        calls = 0

        @classmethod
        def now(cls, tz=None):
            value = base + timedelta(seconds=cls.calls * 10)
            cls.calls += 1
            return value

    def fake_run_session(*_args, **_kwargs):
        calls["session"] += 1
        return result

    def fake_run(args, **_kwargs):
        if args[-1] == "app.py":
            calls["target"] += 1
            return subprocess.CompletedProcess(args, 0, "SMOKE_OK\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_backend(**_kwargs):
        calls["backend"] += 1
        return _SmokeBackendReport()

    monkeypatch.setattr(smoke, "datetime", FrozenDatetime)
    monkeypatch.setattr(smoke, "_respan_api_key", lambda: "sentinel-secret")
    monkeypatch.setattr(smoke, "_init_fixture_repo", lambda _path: "c" * 40)
    monkeypatch.setattr(smoke, "run_session", fake_run_session)
    monkeypatch.setattr(smoke, "_run", fake_run)
    monkeypatch.setattr(smoke, "verify_integration", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(smoke, "_validate_target_then_backend", fake_backend)

    raised: BaseException | None = None
    return_code: int | None = None
    try:
        return_code = smoke.main()
    except BaseException as error:  # production may raise a typed preflight error
        raised = error

    assert calls["session"] == 1
    assert raised is not None or return_code not in (None, 0)
    assert calls["target"] == 0
    assert calls["backend"] == 0
