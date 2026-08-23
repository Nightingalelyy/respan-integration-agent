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
from dataclasses import dataclass
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
from respan_integration_agent.delivery import (  # noqa: E402
    BACKEND_TRACE_EVIDENCE_SCHEMA,
    REQUIRED_VERIFICATION_CHECKS,
    PreparedDelivery,
    RepositoryTarget,
    VerificationReceipt,
)
from respan_integration_agent.gateway_preflight import (  # noqa: E402
    PreflightDeadlineError,
    PreflightError,
    PreflightRateLimitError,
    PreflightTransportError,
)
from respan_integration_agent.platform import (  # noqa: E402
    BackendError,
    BackendRedirectError,
    BackendTransportError,
    RespanPlatformClient,
)
from respan_integration_agent.runner import SessionResult, run_session  # noqa: E402
from respan_integration_agent.sandbox import (  # noqa: E402
    _credential_free_git_env,
    checkout,
    checkout_head,
)
from respan_integration_agent.trace_gate import (  # noqa: E402
    AgentTraceExpectation,
    PollingPolicy,
    TargetTraceExpectation,
    TraceDeadlineExceeded,
    TraceGateAvailabilityError,
    TraceGateError,
    TraceGateReport,
    poll_and_verify_smoke_traces,
)
from respan_integration_agent.verify import verify_integration  # noqa: E402


@dataclass(frozen=True)
class AcceptedSmokeRun:
    """The immutable, payload-bounded result downstream delivery may consume."""

    request: OnboardingRequest
    result: SessionResult
    target_run_id: str
    target_trace_id: str
    target_trace_url: str
    backend_verified_at: datetime
    backend_evidence_schema: str
    passed_checks: tuple[str, ...]
    evidence: dict[str, object]
    prepared_delivery: PreparedDelivery | None
    verification_receipt: VerificationReceipt | None


class ContextualBackendFailure(RuntimeError):
    """Carry safe trace identifiers while keeping backend payloads out of errors."""

    def __init__(
        self,
        error: BackendError | TraceGateError,
        *,
        agent_run_id: str,
        agent_trace_id: str,
        agent_trace_url: str,
        target_run_id: str,
    ) -> None:
        self.error = error
        self.agent_run_id = agent_run_id
        self.agent_trace_id = agent_trace_id
        self.agent_trace_url = agent_trace_url
        self.target_run_id = target_run_id
        super().__init__("backend verification failed")

    def evidence(self) -> dict[str, object]:
        return _backend_failure_evidence(
            self.error,
            agent_run_id=self.agent_run_id,
            agent_trace_id=self.agent_trace_id,
            agent_trace_url=self.agent_trace_url,
            target_run_id=self.target_run_id,
        )


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


def _subprocess_env() -> dict[str, str]:
    """Allow only process mechanics; never forward credentials or user config."""

    return {
        "PATH": os.environ.get("PATH", ""),
        "TMPDIR": os.environ.get("TMPDIR", tempfile.gettempdir()),
    }


def _fingerprint_json(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise RuntimeError("accepted evidence could not be canonicalized") from None
    return hashlib.sha256(encoded).hexdigest()


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
        env=_subprocess_env() if env is None else env,
        input=input_text,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_home(path: Path) -> Path:
    home = path.parent / f".{path.name}-git-home"
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    return home


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "credential.helper=",
            *args,
        ],
        cwd=cwd,
        env=_credential_free_git_env(_git_home(cwd)),
        input_text=input_text,
    )


def _init_fixture_repo(path: Path) -> str:
    _run_git(["init", "-b", "main"], cwd=path)
    _run_git(["config", "user.name", "respan-v0-smoke"], cwd=path)
    _run_git(["config", "user.email", "smoke@respan.ai"], cwd=path)
    _run_git(["add", "-A"], cwd=path)
    _run_git(["commit", "-m", "baseline smoke fixture"], cwd=path)
    return _run_git(["rev-parse", "HEAD"], cwd=path).stdout.strip()


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
            model=result.preflight.approved_agent_model,
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


def _preflight_failure_evidence(error: PreflightError) -> dict[str, object]:
    """Return a versioned failure without reflecting HTTP or credential material."""

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "verdict": "GATEWAY_PREFLIGHT_FAILED",
        "preflight_error": error.as_dict(),
    }


def _preflight_exit_code(error: PreflightError) -> int:
    if isinstance(
        error,
        (PreflightDeadlineError, PreflightRateLimitError, PreflightTransportError),
    ):
        return 3
    return 4


def _validate_preflight_timing(result, smoke_started_at: datetime) -> None:
    """Prove the sanitized readiness pass happened before agent execution."""

    report = result.preflight
    if not report.passed or report.paid_canary_performed:
        raise RuntimeError("gateway preflight did not return a non-paid pass")
    if report.started_at < smoke_started_at:
        raise RuntimeError("gateway preflight timestamp predates the smoke run")
    if report.finished_at < report.started_at:
        raise RuntimeError("gateway preflight timestamps are reversed")
    if report.finished_at > result.agent_started_at:
        raise RuntimeError("agent execution began before gateway preflight completed")


def run_verified_smoke(
    *,
    repo_url: str | None = None,
    base_branch: str = "main",
    expected_base_commit: str | None = None,
    delivery_target: RepositoryTarget | None = None,
) -> AcceptedSmokeRun:
    """Run through backend acceptance without performing any GitHub mutation."""

    if delivery_target is not None:
        if (
            not isinstance(delivery_target, RepositoryTarget)
            or repo_url != delivery_target.canonical_url
            or base_branch != delivery_target.base_ref
            or expected_base_commit is None
        ):
            raise RuntimeError("v0b delivery target does not match the smoke source")
    # The v0b wrapper retains its token only in a local variable. Defensively make
    # every smoke path credential-free even when this function is called directly.
    for credential_name in ("RESPAN_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        os.environ.pop(credential_name, None)

    api_key = _respan_api_key()
    suffix = uuid.uuid4().hex[:12]
    agent_run_id = f"respan-v0a-agent-{suffix}"
    target_run_id = f"respan-v0a-target-{suffix}"
    started_at = datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(prefix="respan-v0a-correct-") as temp_root:
        root = Path(temp_root)
        source = root / "source"
        patched = root / "patched"
        source_base_commit: str | None = None
        if repo_url is None:
            shutil.copytree(FIXTURE, source)
            source_base_commit = _init_fixture_repo(source)
            resolved_repo_url = str(source)
        else:
            resolved_repo_url = repo_url

        request = OnboardingRequest.model_validate(
            {
                "repo_url": resolved_repo_url,
                "base_branch": base_branch,
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
                expected_base_commit=(
                    source_base_commit
                    if source_base_commit is not None
                    else expected_base_commit
                ),
            )
        finally:
            runner_module.run_agent = original_run_agent
        _validate_preflight_timing(result, started_at)
        if source_base_commit is not None and result.base_commit != source_base_commit:
            raise RuntimeError("session base commit did not match the prepared fixture")

        # Replay from the same exact base commit accepted by the runner. Initializing
        # a second independent fixture commit would not bind target evidence to the
        # delivery parent even when the files happened to match.
        with checkout(resolved_repo_url, base_branch) as replay:
            if checkout_head(replay) != result.base_commit:
                raise RuntimeError("replay checkout base moved after agent preparation")
            shutil.copytree(replay, patched)
        _run_git(["apply", "--check", "-"], cwd=patched, input_text=result.diff)
        _run_git(["apply", "-"], cwd=patched, input_text=result.diff)
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
            raise ContextualBackendFailure(
                error,
                agent_run_id=result.run_id,
                agent_trace_id=result.trace_id,
                agent_trace_url=result.trace_url,
                target_run_id=target_run_id,
            ) from None
        if not isinstance(backend_report, TraceGateReport):
            raise RuntimeError("backend trace gate returned an invalid report type")
        if not backend_report.passed:  # defensive: hard gate failures normally raise
            raise RuntimeError("backend trace gate returned a non-passing report")
        if (
            backend_report.agent_trace_id != result.trace_id
            or backend_report.agent_trace_url != result.trace_url
        ):
            raise RuntimeError("backend trace gate agent identity did not match")
        if not backend_report.checks or any(
            check.status == "fail" for check in backend_report.checks
        ):
            raise RuntimeError("backend trace gate checks were incomplete")
        for required_role in ("agent", "target"):
            if not any(
                check.role == required_role and check.status == "pass"
                for check in backend_report.checks
            ):
                raise RuntimeError("backend trace gate role acceptance was incomplete")
        verified_at = datetime.now(timezone.utc)
        backend_evidence = backend_report.to_dict()
        backend_schema = backend_evidence.get("schema_version")
        target_trace_id = backend_evidence.get("target_trace_id")
        target_trace_url = backend_evidence.get("target_trace_url")
        if (
            backend_evidence.get("passed") is not True
            or backend_schema != BACKEND_TRACE_EVIDENCE_SCHEMA
        ):
            raise RuntimeError("backend trace gate evidence schema is invalid")
        if (
            not isinstance(target_trace_id, str)
            or len(target_trace_id) != 32
            or any(character not in "0123456789abcdef" for character in target_trace_id)
            or int(target_trace_id, 16) == 0
        ):
            raise RuntimeError("backend trace gate target identity is invalid")
        if target_trace_url != (
            "https://platform.respan.ai/platform/traces?"
            f"trace_unique_id={target_trace_id}"
        ):
            raise RuntimeError("backend trace gate target link is invalid")
        passed_checks = REQUIRED_VERIFICATION_CHECKS
        prepared_delivery: PreparedDelivery | None = None
        verification_receipt: VerificationReceipt | None = None
        if delivery_target is not None:
            prepared_delivery = PreparedDelivery(
                target=delivery_target,
                base_sha=result.base_commit,
                patch=result.diff.encode("utf-8"),
                changed_paths=tuple(sorted(result.changed_files)),
                product=request.product.value,
                config_fingerprint=_fingerprint_json(request.model_dump(mode="json")),
                agent_run_id=result.run_id,
                agent_trace_id=result.trace_id,
                agent_trace_url=result.trace_url,
            )
            verification_receipt = VerificationReceipt.for_prepared(
                prepared_delivery,
                gateway_report_fingerprint=_fingerprint_json(
                    result.preflight.to_dict()
                ),
                target_run_id=target_run_id,
                target_trace_id=target_trace_id,
                target_trace_url=target_trace_url,
                backend_verified_at=verified_at,
                backend_evidence_schema=backend_schema,
                passed_checks=passed_checks,
            )
            verification_receipt.validate_for(prepared_delivery)
        evidence = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "verdict": "BACKEND_VERIFIED_PASS",
            "started_at": started_at.isoformat(),
            "capture_finished_at": capture_finished_at.isoformat(),
            "backend_verified_at": verified_at.isoformat(),
            "base_commit": result.base_commit,
            "patch_sha256": hashlib.sha256(result.diff.encode("utf-8")).hexdigest(),
            "agent_run_id": result.run_id,
            "agent_session_id": result.agent_session_id,
            "agent_turns": result.num_turns,
            "agent_cost_usd": result.total_cost_usd,
            "agent_base_url": AGENT_BASE_URL,
            "agent_started_at": result.agent_started_at.isoformat(),
            "telemetry_flushed": result.telemetry_flushed,
            "changed_files": result.changed_files,
            "gateway_preflight": result.preflight.to_dict(),
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
            "backend_gate": backend_evidence,
        }
        return AcceptedSmokeRun(
            request=request,
            result=result,
            target_run_id=target_run_id,
            target_trace_id=target_trace_id,
            target_trace_url=target_trace_url,
            backend_verified_at=verified_at,
            backend_evidence_schema=backend_schema,
            passed_checks=passed_checks,
            evidence=evidence,
            prepared_delivery=prepared_delivery,
            verification_receipt=verification_receipt,
        )


def main() -> int:
    try:
        accepted = run_verified_smoke()
    except ContextualBackendFailure as failure:
        print(json.dumps(failure.evidence(), sort_keys=True), file=sys.stderr)
        return _backend_exit_code(failure.error)
    print(json.dumps(accepted.evidence, indent=2, sort_keys=True))
    print("\n--- accepted patch ---")
    print(accepted.result.diff)
    return 0


def entrypoint() -> int:
    """Map backend availability and contract failures to stable CLI exit classes."""
    try:
        return main()
    except PreflightError as error:
        print(
            json.dumps(_preflight_failure_evidence(error), sort_keys=True),
            file=sys.stderr,
        )
        return _preflight_exit_code(error)
    except (TraceGateError, BackendError) as error:
        print(
            json.dumps(_backend_failure_evidence(error), sort_keys=True),
            file=sys.stderr,
        )
        return _backend_exit_code(error)


if __name__ == "__main__":
    raise SystemExit(entrypoint())
