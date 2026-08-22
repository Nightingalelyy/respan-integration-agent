import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


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
