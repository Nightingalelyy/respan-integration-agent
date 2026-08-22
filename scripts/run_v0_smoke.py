#!/usr/bin/env python3
"""Run the trusted, credentialed v0a fixture and verify the generated target."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "agent" / "src"
FIXTURE = REPO_ROOT / "smoke" / "v0a-python"
RESPAN_AI_VERSION = "4.1.0"
OPENAI_OTEL_VERSION = "0.62.3"
AGENT_BASE_URL = "https://api.respan.ai/api"
TARGET_BASE_URL = "https://api.respan.ai/api"
TARGET_MODEL = "gpt-4o-mini"
PYPI_INDEX_URL = "https://pypi.org/simple"
BACKEND_GATE_TIMEOUT_SECONDS = 240.0
BACKEND_CONNECT_TIMEOUT_SECONDS = 10.0
BACKEND_READ_TIMEOUT_SECONDS = 30.0
EVIDENCE_SCHEMA_VERSION = "respan-v0-smoke-evidence/v1"

sys.path.insert(0, str(AGENT_SRC))

from respan_integration_agent.config import OnboardingRequest  # noqa: E402
from respan_integration_agent.platform import (  # noqa: E402
    BackendError,
    BackendRedirectError,
    BackendTransportError,
    RespanPlatformClient,
)
from respan_integration_agent.runner import run_session  # noqa: E402
from respan_integration_agent.trace_gate import (  # noqa: E402
    AgentTraceExpectation,
    PollingPolicy,
    TargetTraceExpectation,
    TraceDeadlineExceeded,
    TraceGateAvailabilityError,
    TraceGateError,
    poll_and_verify_smoke_traces,
)
from respan_integration_agent.verify import verify_integration  # noqa: E402


def _read_env_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip("\"'")
    return None


def _respan_api_key() -> str:
    existing = os.environ.get("RESPAN_API_KEY", "").strip()
    if existing:
        return existing
    for candidate in (REPO_ROOT / ".env", REPO_ROOT.parent / ".env"):
        value = _read_env_value(candidate, "RESPAN_API_KEY")
        if value:
            return value
    raise RuntimeError(
        "RESPAN_API_KEY is not set and was not found in a repository .env"
    )


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 180.0,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _init_fixture_repo(path: Path) -> str:
    _run(["git", "init", "-b", "main"], cwd=path)
    _run(["git", "config", "user.name", "respan-v0-smoke"], cwd=path)
    _run(["git", "config", "user.email", "smoke@respan.ai"], cwd=path)
    _run(["git", "add", "-A"], cwd=path)
    _run(["git", "commit", "-m", "baseline smoke fixture"], cwd=path)
    return _run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()


def _target_env(api_key: str, run_id: str) -> dict[str, str]:
    """Give the generated target only the runtime values its fixture permits."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
        "RESPAN_API_KEY": api_key,
        "RESPAN_EXAMPLE_RUN_ID": run_id,
        "RESPAN_BASE_URL": TARGET_BASE_URL,
        "RESPAN_SMOKE_MODEL": TARGET_MODEL,
    }


def _pip_env() -> dict[str, str]:
    """Install the target without forwarding credentials or package-index overrides."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
    }


def _verify_backend_traces(
    *,
    api_key: str,
    result,
    target_run_id: str,
    smoke_started_at: datetime,
    smoke_finished_at: datetime,
):
    """Require exact, content-level backend acceptance for both smoke traces."""
    if result.total_cost_usd is None or result.total_cost_usd <= 0:
        raise RuntimeError("agent SDK did not report a positive smoke cost")
    backend = RespanPlatformClient(
        api_key,
        connect_timeout=BACKEND_CONNECT_TIMEOUT_SECONDS,
        read_timeout=BACKEND_READ_TIMEOUT_SECONDS,
    )
    return poll_and_verify_smoke_traces(
        backend,
        agent=AgentTraceExpectation(
            run_id=result.run_id,
            trace_id=result.trace_id,
            smoke_started_at=smoke_started_at,
            smoke_finished_at=smoke_finished_at,
            sdk_cost_usd=result.total_cost_usd,
            checkout_root=result.agent_checkout_root,
            respan_ai_version=RESPAN_AI_VERSION,
            openai_otel_version=OPENAI_OTEL_VERSION,
        ),
        target=TargetTraceExpectation(
            run_id=target_run_id,
            smoke_started_at=smoke_started_at,
            smoke_finished_at=smoke_finished_at,
            model=TARGET_MODEL,
            instrumentation_scope_version=OPENAI_OTEL_VERSION,
        ),
        secret_values=(api_key,),
        policy=PollingPolicy(timeout_seconds=BACKEND_GATE_TIMEOUT_SECONDS),
    )


def _validate_target_process(
    target: subprocess.CompletedProcess[str], api_key: str
) -> None:
    """Reject target failures without reflecting captured output or credentials."""
    if api_key in target.stdout or api_key in target.stderr:
        raise RuntimeError("target process output exposed RESPAN_API_KEY")
    if target.stdout != "SMOKE_OK\n":
        raise RuntimeError("target stdout was not exactly SMOKE_OK")
    if "Traceback" in target.stderr:
        raise RuntimeError("target process emitted a traceback")


def _validate_target_then_backend(
    *,
    target: subprocess.CompletedProcess[str],
    api_key: str,
    result,
    target_run_id: str,
    smoke_started_at: datetime,
    smoke_finished_at: datetime,
):
    """Keep the backend gate strictly downstream of the real target gate."""
    _validate_target_process(target, api_key)
    return _verify_backend_traces(
        api_key=api_key,
        result=result,
        target_run_id=target_run_id,
        smoke_started_at=smoke_started_at,
        smoke_finished_at=smoke_finished_at,
    )


def _backend_failure_evidence(
    error: BackendError | TraceGateError,
    *,
    agent_run_id: str | None = None,
    agent_trace_id: str | None = None,
    agent_trace_url: str | None = None,
    target_run_id: str | None = None,
) -> dict[str, object]:
    """Serialize only stable backend failure codes; never payloads or credentials."""
    if isinstance(error, BackendError):
        detail: dict[str, object] = dict(error.as_dict())
    else:
        detail = {"code": error.code}
        if error.role is not None:
            detail["role"] = error.role
        if isinstance(error, TraceDeadlineExceeded):
            detail.update(
                {
                    "attempts": error.attempts,
                    "elapsed_seconds": round(error.elapsed_seconds, 6),
                    "unmet_codes": list(error.unmet_codes),
                }
            )
    evidence: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "verdict": "BACKEND_VERIFICATION_FAILED",
        "backend_error": detail,
    }
    for key, value in (
        ("agent_run_id", agent_run_id),
        ("agent_trace_id", agent_trace_id),
        ("agent_trace_url", agent_trace_url),
        ("target_run_id", target_run_id),
    ):
        if value is not None:
            evidence[key] = value
    return evidence


def _backend_exit_code(error: BackendError | TraceGateError) -> int:
    if isinstance(error, BackendRedirectError):
        return 4
    if isinstance(error, (TraceGateAvailabilityError, BackendTransportError)):
        return 3
    return 4


def main() -> int:
    api_key = _respan_api_key()
    suffix = uuid.uuid4().hex[:12]
    agent_run_id = f"respan-v0a-agent-{suffix}"
    target_run_id = f"respan-v0a-target-{suffix}"
    started_at = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(prefix="respan-v0a-correct-") as temp_root:
        root = Path(temp_root)
        source = root / "source"
        patched = root / "patched"
        shutil.copytree(FIXTURE, source)
        base_commit = _init_fixture_repo(source)

        request = OnboardingRequest.model_validate(
            {
                "repo_url": str(source),
                "base_branch": "main",
                "product": "tracing",
                "tracing": {
                    "mode": "auto",
                    "environment": "smoke",
                    "service_name": "respan-v0a-python-smoke",
                    "endpoint": "platform",
                },
                "verification": {
                    "profile": "python-openai-auto-smoke",
                    "respan_ai_version": RESPAN_AI_VERSION,
                    "openai_otel_version": OPENAI_OTEL_VERSION,
                },
            }
        )

        # A deterministic id makes the integration-agent trace exactly queryable.
        import respan_integration_agent.agent as agent_module

        original_run_agent = agent_module.run_agent

        def run_agent_with_id(*args, **kwargs):
            kwargs["run_id"] = agent_run_id
            return original_run_agent(*args, **kwargs)

        import respan_integration_agent.runner as runner_module

        runner_module.run_agent = run_agent_with_id
        try:
            result = run_session(
                request,
                respan_api_key=api_key,
                respan_base_url=AGENT_BASE_URL,
            )
        finally:
            runner_module.run_agent = original_run_agent

        shutil.copytree(FIXTURE, patched)
        _init_fixture_repo(patched)
        _run(["git", "apply", "--check", "-"], cwd=patched, input_text=result.diff)
        _run(["git", "apply", "-"], cwd=patched, input_text=result.diff)
        verify_integration(
            patched,
            request,
            result.changed_files,
            result.diff,
            respan_api_key=api_key,
        )

        venv = root / "venv"
        _run([sys.executable, "-m", "venv", str(venv)], cwd=root)
        python = venv / "bin" / "python"
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--isolated",
                "--no-input",
                "--no-cache-dir",
                "--index-url",
                PYPI_INDEX_URL,
                "--disable-pip-version-check",
                "-r",
                str(patched / "requirements.txt"),
            ],
            cwd=patched,
            env=_pip_env(),
            timeout=300.0,
        )
        _run([str(python), "-m", "pip", "check"], cwd=patched, env=_pip_env())

        target = _run(
            [str(python), "app.py"],
            cwd=patched,
            env=_target_env(api_key, target_run_id),
            timeout=60.0,
        )
        capture_finished_at = datetime.now(timezone.utc)
        try:
            backend_report = _validate_target_then_backend(
                target=target,
                api_key=api_key,
                result=result,
                target_run_id=target_run_id,
                smoke_started_at=started_at,
                smoke_finished_at=capture_finished_at,
            )
        except (TraceGateError, BackendError) as error:
            print(
                json.dumps(
                    _backend_failure_evidence(
                        error,
                        agent_run_id=result.run_id,
                        agent_trace_id=result.trace_id,
                        agent_trace_url=result.trace_url,
                        target_run_id=target_run_id,
                    ),
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return _backend_exit_code(error)
        if not backend_report.passed:  # defensive: hard gate failures normally raise
            raise RuntimeError("backend trace gate returned a non-passing report")
        verified_at = datetime.now(timezone.utc)
        evidence = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "verdict": "BACKEND_VERIFIED_PASS",
            "started_at": started_at.isoformat(),
            "capture_finished_at": capture_finished_at.isoformat(),
            "backend_verified_at": verified_at.isoformat(),
            "base_commit": base_commit,
            "patch_sha256": hashlib.sha256(result.diff.encode("utf-8")).hexdigest(),
            "agent_run_id": result.run_id,
            "agent_session_id": result.agent_session_id,
            "agent_turns": result.num_turns,
            "agent_cost_usd": result.total_cost_usd,
            "agent_base_url": AGENT_BASE_URL,
            "telemetry_flushed": result.telemetry_flushed,
            "changed_files": result.changed_files,
            "target_run_id": target_run_id,
            "target_stdout": target.stdout.strip(),
            "target_base_url": TARGET_BASE_URL,
            "target_model": TARGET_MODEL,
            "package_index": PYPI_INDEX_URL,
            "respan_ai_version": RESPAN_AI_VERSION,
            "openai_otel_version": OPENAI_OTEL_VERSION,
            "backend_gate_timeout_seconds": BACKEND_GATE_TIMEOUT_SECONDS,
            "backend_connect_timeout_seconds": BACKEND_CONNECT_TIMEOUT_SECONDS,
            "backend_read_timeout_seconds": BACKEND_READ_TIMEOUT_SECONDS,
            "backend_gate": backend_report.to_dict(),
        }
        print(json.dumps(evidence, indent=2, sort_keys=True))
        print("\n--- accepted patch ---")
        print(result.diff)
    return 0


def entrypoint() -> int:
    """Map backend availability and contract failures to stable CLI exit classes."""
    try:
        return main()
    except (TraceGateError, BackendError) as error:
        print(json.dumps(_backend_failure_evidence(error), sort_keys=True), file=sys.stderr)
        return _backend_exit_code(error)


if __name__ == "__main__":
    raise SystemExit(entrypoint())
