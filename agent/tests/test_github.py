from __future__ import annotations

import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from respan_integration_agent.delivery import (
    BACKEND_TRACE_EVIDENCE_SCHEMA,
    REQUIRED_VERIFICATION_CHECKS,
    DeliveryError,
    DeliveryIntegrityError,
    DeliveryJournal,
    DeliveryJournalError,
    DeliveryPhase,
    DeliveryRecoveryRequired,
    PreparedDelivery,
    RemoteDisposition,
    RepositoryTarget,
    VerificationReceipt,
)
from respan_integration_agent.github import (
    GITHUB_API_ORIGIN,
    GITHUB_API_VERSION,
    DirectGitHubHttpsTransport,
    GitCommandResult,
    GitHubHttpRequest,
    GitHubHttpResponse,
    GitHubReadiness,
    GitHubRestClient,
    SubprocessGitExecutor,
    _GitFailure,
    build_pull_request_body,
    build_pull_request_title,
    commit_and_push,
    open_pr,
)


REPOSITORY = "respan/v0-delivery-fixture"
CANONICAL_URL = f"https://github.com/{REPOSITORY}"
TOKEN = "ghs_TEST_ONLY_STATELESS_FORMAT_abcdefghijklmnopqrstuvwxyz"
BASE_SHA = "1" * 40
COMMIT_SHA = "2" * 40
AGENT_TRACE_ID = "1" * 32
TARGET_TRACE_ID = "2" * 32
PATCH = b"""diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-print("old")
+print("new")
"""


def _trace_url(trace_id: str) -> str:
    return f"https://platform.respan.ai/platform/traces?trace_unique_id={trace_id}"


def _target() -> RepositoryTarget:
    return RepositoryTarget(
        repo_url=CANONICAL_URL,
        base_ref="main",
        allowed_slug=REPOSITORY,
    )


def _prepared(
    *,
    target: RepositoryTarget | None = None,
    base_sha: str = BASE_SHA,
    patch: bytes = PATCH,
    changed_paths: tuple[str, ...] = ("app.py",),
) -> PreparedDelivery:
    return PreparedDelivery(
        target=target or _target(),
        base_sha=base_sha,
        patch=patch,
        changed_paths=changed_paths,
        product="tracing",
        config_fingerprint="a" * 64,
        agent_run_id="respan-v0b-agent-run",
        agent_trace_id=AGENT_TRACE_ID,
        agent_trace_url=_trace_url(AGENT_TRACE_ID),
    )


def _receipt(
    prepared: PreparedDelivery,
    *,
    backend_verified_at: datetime | None = None,
) -> VerificationReceipt:
    return VerificationReceipt.for_prepared(
        prepared,
        gateway_report_fingerprint="b" * 64,
        target_run_id="respan-v0b-target-run",
        target_trace_id=TARGET_TRACE_ID,
        target_trace_url=_trace_url(TARGET_TRACE_ID),
        backend_verified_at=backend_verified_at or datetime.now(timezone.utc),
        backend_evidence_schema=BACKEND_TRACE_EVIDENCE_SCHEMA,
        passed_checks=REQUIRED_VERIFICATION_CHECKS,
    )


def _pr_payload(
    prepared: PreparedDelivery,
    *,
    number: int = 17,
    sha: str = COMMIT_SHA,
    state: str = "open",
    merged_at: str | None = None,
    base_sha: str | None = None,
    delivery_fingerprint: str | None = None,
    receipt: VerificationReceipt | None = None,
) -> dict[str, object]:
    fingerprint = delivery_fingerprint or prepared.delivery_fingerprint
    return {
        "number": number,
        "html_url": f"{CANONICAL_URL}/pull/{number}",
        "title": build_pull_request_title(prepared),
        "state": state,
        "merged_at": merged_at,
        "body": (
            build_pull_request_body(
                prepared,
                receipt or _receipt(prepared),
                commit_sha=sha,
            )
            if fingerprint == prepared.delivery_fingerprint
            else f"fixture\n\n<!-- respan-delivery:{fingerprint} -->\n"
        ),
        "head": {
            "ref": prepared.branch,
            "sha": sha,
            "repo": {"full_name": REPOSITORY},
        },
        "base": {
            "ref": prepared.target.base_ref,
            "sha": base_sha or prepared.base_sha,
            "repo": {"full_name": REPOSITORY},
        },
    }


def _user_payload() -> dict[str, object]:
    return {"login": "respan-delivery-bot", "id": 101, "type": "Bot"}


def _repository_payload() -> dict[str, object]:
    return {
        "id": 202,
        "name": "v0-delivery-fixture",
        "full_name": REPOSITORY,
        "html_url": CANONICAL_URL,
        "private": False,
        "visibility": "public",
        "archived": False,
        "disabled": False,
        "fork": False,
        "owner": {"login": "respan"},
    }


def _ref_payload(
    prepared: PreparedDelivery, sha: str | None = None
) -> dict[str, object]:
    return {
        "ref": f"refs/heads/{prepared.target.base_ref}",
        "object": {"type": "commit", "sha": sha or prepared.base_sha},
    }


def _json_response(
    status: int,
    value: object,
    *,
    headers: Mapping[str, str] | None = None,
) -> GitHubHttpResponse:
    return GitHubHttpResponse(
        status,
        {"content-type": "application/json", **dict(headers or {})},
        json.dumps(value, separators=(",", ":")).encode(),
    )


class ScriptedTransport:
    def __init__(
        self,
        script: Sequence[
            GitHubHttpResponse
            | BaseException
            | Callable[[GitHubHttpRequest], GitHubHttpResponse]
        ],
    ) -> None:
        self.script = list(script)
        self.requests: list[GitHubHttpRequest] = []

    def request(self, request: GitHubHttpRequest) -> GitHubHttpResponse:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("unexpected request")
        action = self.script.pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request)
        return action


def _list_url(prepared: PreparedDelivery, page: int) -> str:
    query = urlencode(
        {
            "state": "all",
            "head": f"{prepared.target.owner}:{prepared.branch}",
            "base": prepared.target.base_ref,
            "per_page": "100",
            "page": str(page),
        }
    )
    return f"{GITHUB_API_ORIGIN}/repos/{REPOSITORY}/pulls?{query}"


def test_rest_create_uses_exact_headers_body_and_repr_hides_secrets() -> None:
    prepared = _prepared()
    receipt = _receipt(prepared)
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            _json_response(201, _pr_payload(prepared)),
        ]
    )
    client = GitHubRestClient(TOKEN, prepared.target, transport=transport)

    record, disposition = client.create_or_reconcile_pull_request(
        prepared, receipt, commit_sha=COMMIT_SHA
    )

    assert disposition is RemoteDisposition.created
    assert record.number == 17
    assert [request.method for request in transport.requests] == ["GET", "GET", "POST"]
    request = transport.requests[2]
    assert request.url == f"{GITHUB_API_ORIGIN}/repos/{REPOSITORY}/pulls"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["X-GitHub-Api-Version"] == GITHUB_API_VERSION
    payload = json.loads(request.body)
    assert payload["head"] == prepared.branch
    assert payload["base"] == "main"
    assert prepared.delivery_fingerprint in payload["body"]
    assert "Applied Respan integration" not in payload["body"]
    assert TOKEN not in repr(request)
    assert prepared.delivery_fingerprint not in repr(request)
    hostile_response = GitHubHttpResponse(
        401,
        {"x-hostile": TOKEN},
        TOKEN.encode(),
    )
    assert TOKEN not in repr(hostile_response)


def test_list_follows_only_exact_same_origin_next_page() -> None:
    prepared = _prepared()
    first = _json_response(
        200,
        [_pr_payload(prepared, number=16)],
        headers={"link": f'<{_list_url(prepared, 2)}>; rel="next"'},
    )
    transport = ScriptedTransport(
        [first, _json_response(200, [_pr_payload(prepared, number=17)])]
    )
    client = GitHubRestClient(TOKEN, prepared.target, transport=transport)

    records = client.list_exact_pull_requests(branch=prepared.branch)

    assert [record.number for record in records] == [16, 17]
    assert [request.url for request in transport.requests] == [
        _list_url(prepared, 1),
        _list_url(prepared, 2),
    ]
    query = parse_qs(urlsplit(transport.requests[0].url).query)
    assert query["head"] == [f"respan:{prepared.branch}"]
    assert query["base"] == ["main"]


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.example/repos/respan/v0-delivery-fixture/pulls?page=2",
        "http://api.github.com/repos/respan/v0-delivery-fixture/pulls?page=2",
        "https://token@api.github.com/repos/respan/v0-delivery-fixture/pulls?page=2",
        "https://api.github.com:443/repos/respan/v0-delivery-fixture/pulls?page=2",
    ],
)
def test_pagination_rejects_every_noncanonical_origin(next_url: str) -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [
            _json_response(
                200,
                [],
                headers={"link": f'<{next_url}>; rel="next"'},
            )
        ]
    )

    with pytest.raises(DeliveryIntegrityError):
        GitHubRestClient(
            TOKEN, prepared.target, transport=transport
        ).list_exact_pull_requests(branch=prepared.branch)

    assert len(transport.requests) == 1


def test_duplicate_pr_422_is_reconciled_without_second_post() -> None:
    prepared = _prepared()
    receipt = _receipt(prepared)
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            _json_response(422, {"message": "unsafe body must not surface"}),
            _json_response(200, [_pr_payload(prepared)]),
        ]
    )

    record, disposition = GitHubRestClient(
        TOKEN, prepared.target, transport=transport
    ).create_or_reconcile_pull_request(prepared, receipt, commit_sha=COMMIT_SHA)

    assert record.number == 17
    assert disposition is RemoteDisposition.reused
    assert sum(request.method == "POST" for request in transport.requests) == 1


def test_ambiguous_post_is_reconciled_and_never_posted_twice() -> None:
    prepared = _prepared()
    receipt = _receipt(prepared)
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            TimeoutError("secret response"),
            _json_response(200, [_pr_payload(prepared)]),
        ]
    )

    record, disposition = GitHubRestClient(
        TOKEN, prepared.target, transport=transport
    ).create_or_reconcile_pull_request(prepared, receipt, commit_sha=COMMIT_SHA)

    assert record.number == 17
    assert disposition is RemoteDisposition.reused
    assert sum(request.method == "POST" for request in transport.requests) == 1


def test_unresolved_ambiguous_post_returns_only_safe_recovery() -> None:
    prepared = _prepared()
    receipt = _receipt(prepared)
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            TimeoutError(TOKEN),
            _json_response(200, []),
        ]
    )

    with pytest.raises(DeliveryError) as caught:
        GitHubRestClient(
            TOKEN, prepared.target, transport=transport
        ).create_or_reconcile_pull_request(prepared, receipt, commit_sha=COMMIT_SHA)

    encoded = json.dumps(caught.value.as_dict(), sort_keys=True)
    assert caught.value.code == "G_RECOVERY_REQUIRED"
    assert caught.value.stage == "github_recovery"
    assert caught.value.recovery["branch"] == prepared.branch
    assert caught.value.recovery["commit_sha"] == COMMIT_SHA
    assert TOKEN not in encoded
    assert "response" not in encoded
    assert caught.value.__cause__ is None
    assert sum(request.method == "POST" for request in transport.requests) == 1


@pytest.mark.parametrize(
    "bad_created_response",
    [
        GitHubHttpResponse(201, {"content-type": "application/json"}, b"{"),
        GitHubHttpResponse(201, {"content-type": "text/html"}, b"created"),
    ],
)
def test_malformed_created_response_reconciles_without_second_post(
    bad_created_response: GitHubHttpResponse,
) -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            bad_created_response,
            _json_response(200, [_pr_payload(prepared)]),
        ]
    )

    record, disposition = GitHubRestClient(
        TOKEN,
        prepared.target,
        transport=transport,
    ).create_or_reconcile_pull_request(
        prepared,
        _receipt(prepared),
        commit_sha=COMMIT_SHA,
    )

    assert record.number == 17
    assert disposition is RemoteDisposition.reused
    assert sum(request.method == "POST" for request in transport.requests) == 1


@pytest.mark.parametrize("mismatch", ["base_sha", "fingerprint", "body"])
def test_ambiguous_create_retains_mismatched_pr_recovery_identity(
    mismatch: str,
) -> None:
    prepared = _prepared()
    payload = _pr_payload(prepared)
    if mismatch == "base_sha":
        payload["base"]["sha"] = "3" * 40
    elif mismatch == "fingerprint":
        payload = _pr_payload(prepared, delivery_fingerprint="3" * 64)
    else:
        payload["body"] = "edited after creation"
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            GitHubHttpResponse(201, {"content-type": "application/json"}, b"{"),
            _json_response(200, [payload]),
        ]
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=transport,
        ).create_or_reconcile_pull_request(
            prepared,
            _receipt(prepared),
            commit_sha=COMMIT_SHA,
        )

    assert caught.value.recovery["pr_number"] == 17
    assert caught.value.recovery["pr_url"] == f"{CANONICAL_URL}/pull/17"
    assert caught.value.code == (
        "G_BASE_MOVED" if mismatch == "base_sha" else "G_PR_CONFLICT"
    )
    assert sum(request.method == "POST" for request in transport.requests) == 1


@pytest.mark.parametrize("status", [200, 202])
def test_unexpected_successful_post_reconciles_without_second_post(status: int) -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            _json_response(status, {"unexpected": "success response"}),
            _json_response(200, [_pr_payload(prepared)]),
        ]
    )

    record, disposition = GitHubRestClient(
        TOKEN,
        prepared.target,
        transport=transport,
    ).create_or_reconcile_pull_request(
        prepared,
        _receipt(prepared),
        commit_sha=COMMIT_SHA,
    )

    assert record.number == 17
    assert disposition is RemoteDisposition.reused
    assert sum(request.method == "POST" for request in transport.requests) == 1


@pytest.mark.parametrize(
    ("state", "merged_at", "accepted"),
    [
        ("open", None, True),
        ("closed", "2026-08-23T00:00:00Z", True),
        ("closed", None, False),
    ],
)
def test_existing_exact_pr_reuse_policy(
    state: str, merged_at: str | None, accepted: bool
) -> None:
    prepared = _prepared()
    receipt = _receipt(prepared)
    transport = ScriptedTransport(
        [
            _json_response(
                200,
                [_pr_payload(prepared, state=state, merged_at=merged_at)],
            )
        ]
    )
    action = lambda: GitHubRestClient(  # noqa: E731
        TOKEN, prepared.target, transport=transport
    ).create_or_reconcile_pull_request(prepared, receipt, commit_sha=COMMIT_SHA)

    if accepted:
        _record, disposition = action()
        assert disposition is RemoteDisposition.reused
    else:
        with pytest.raises(DeliveryIntegrityError):
            action()
    assert all(request.method == "GET" for request in transport.requests)


def test_existing_branch_pr_with_other_sha_fails_without_post() -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [_json_response(200, [_pr_payload(prepared, sha="3" * 40)])]
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        GitHubRestClient(
            TOKEN, prepared.target, transport=transport
        ).create_or_reconcile_pull_request(
            prepared, _receipt(prepared), commit_sha=COMMIT_SHA
        )

    assert caught.value.stage == "github_head_collision"
    assert all(request.method == "GET" for request in transport.requests)


@pytest.mark.parametrize("field_name", ["title", "body"])
def test_existing_pr_requires_exact_generated_content(field_name: str) -> None:
    prepared = _prepared()
    payload = _pr_payload(prepared)
    payload[field_name] = f"operator-mutated-{field_name}"
    if field_name == "body":
        payload[field_name] += (
            f"\n\n<!-- respan-delivery:{prepared.delivery_fingerprint} -->\n"
        )
    transport = ScriptedTransport([_json_response(200, [payload])])

    with pytest.raises(DeliveryIntegrityError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=transport,
        ).create_or_reconcile_pull_request(
            prepared,
            _receipt(prepared),
            commit_sha=COMMIT_SHA,
        )

    assert caught.value.code == "G_PR_CONFLICT"
    assert caught.value.stage == "github_pr_content"
    assert caught.value.recovery["pr_number"] == 17
    assert all(request.method == "GET" for request in transport.requests)


@pytest.mark.parametrize(
    "bad_response",
    [
        GitHubHttpResponse(200, {"content-type": "text/html"}, b"[]"),
        GitHubHttpResponse(200, {"content-type": "application/json"}, b'{"x":1,"x":2}'),
        GitHubHttpResponse(200, {"content-type": "application/json"}, b"{}"),
        GitHubHttpResponse(302, {"location": "https://api.github.com/elsewhere"}, b""),
    ],
)
def test_list_rejects_content_schema_and_redirect_failures(
    bad_response: GitHubHttpResponse,
) -> None:
    prepared = _prepared()
    with pytest.raises(DeliveryError):
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=ScriptedTransport([bad_response]),
        ).list_exact_pull_requests(branch=prepared.branch)


def test_safe_get_retries_only_bounded_transient_failures() -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [
            TimeoutError("first read failed"),
            _json_response(503, {"message": "unavailable"}),
            _json_response(200, []),
        ]
    )

    records = GitHubRestClient(
        TOKEN,
        prepared.target,
        transport=transport,
        retry_delay_seconds=0,
    ).list_exact_pull_requests(branch=prepared.branch)

    assert records == ()
    assert len(transport.requests) == 3
    assert all(request.method == "GET" for request in transport.requests)


def test_exhausted_rate_limit_has_named_transient_error() -> None:
    prepared = _prepared()
    responses = [_json_response(429, {"message": "rate limited"}) for _ in range(3)]
    transport = ScriptedTransport(responses)

    with pytest.raises(DeliveryError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=transport,
            retry_delay_seconds=0,
        ).list_exact_pull_requests(branch=prepared.branch)

    assert caught.value.code == "G_RATE_LIMIT"
    assert caught.value.transient is True
    assert caught.value.attempts == 3
    assert len(transport.requests) == 3


def test_invalid_retry_after_has_named_schema_error() -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [
            _json_response(
                429,
                {"message": "rate limited"},
                headers={"retry-after": "not-a-number"},
            )
        ]
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=transport,
            retry_delay_seconds=0,
        ).list_exact_pull_requests(branch=prepared.branch)

    assert caught.value.code == "G_RESPONSE_SCHEMA"
    assert caught.value.stage == "github_retry_header"
    assert len(transport.requests) == 1


def test_safe_get_does_not_retry_ordinary_forbidden_response() -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [
            _json_response(403, {"message": "forbidden"}),
            _json_response(200, []),
        ]
    )

    with pytest.raises(DeliveryError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=transport,
            retry_delay_seconds=0,
        ).list_exact_pull_requests(branch=prepared.branch)

    assert caught.value.stage == "github_authorization"
    assert caught.value.code == "G_FORBIDDEN"
    assert len(transport.requests) == 1


def test_direct_transport_requires_verified_tls() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        DirectGitHubHttpsTransport(ssl_context=context)


def test_rest_rejects_a_response_after_the_shared_deadline() -> None:
    prepared = _prepared()
    moments = iter((0.0, 0.0, 31.0))
    transport = ScriptedTransport([_json_response(200, [])])
    client = GitHubRestClient(
        TOKEN,
        prepared.target,
        transport=transport,
        operation_timeout=30.0,
        monotonic=lambda: next(moments),
    )

    with pytest.raises(DeliveryError) as caught:
        client.list_exact_pull_requests(branch=prepared.branch)

    assert caught.value.code == "G_TRANSPORT"
    assert len(transport.requests) == 1


def test_subprocess_git_executor_stops_at_the_output_cap(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(_GitFailure):
        SubprocessGitExecutor().run(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * (2 * 1024 * 1024))",
            ],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            input_bytes=None,
            timeout=10.0,
        )

    assert time.monotonic() - started < 5.0


def test_subprocess_git_executor_kills_descendants_at_the_output_cap(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "child-ready"
    survived = tmp_path / "child-survived"
    child_code = (
        "import time\n"
        "from pathlib import Path\n"
        f"Path({str(ready)!r}).write_text('ready', encoding='utf-8')\n"
        "time.sleep(0.5)\n"
        f"Path({str(survived)!r}).write_text('survived', encoding='utf-8')\n"
    )
    parent_code = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"subprocess.Popen([{sys.executable!r}, '-c', {child_code!r}])\n"
        f"ready = Path({str(ready)!r})\n"
        "while not ready.exists():\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write('x' * (2 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )

    with pytest.raises(_GitFailure):
        SubprocessGitExecutor().run(
            [sys.executable, "-c", parent_code],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            input_bytes=None,
            timeout=10.0,
        )

    assert ready.is_file()
    time.sleep(0.8)
    assert not survived.exists()


def test_read_only_preflight_proves_auth_and_anonymous_identity() -> None:
    prepared = _prepared()
    authenticated = ScriptedTransport(
        [
            _json_response(200, _user_payload()),
            _json_response(200, _repository_payload()),
            _json_response(200, _ref_payload(prepared)),
        ]
    )
    public = ScriptedTransport(
        [
            _json_response(200, _repository_payload()),
            _json_response(200, _ref_payload(prepared)),
        ]
    )

    result = GitHubRestClient(
        TOKEN,
        prepared.target,
        transport=authenticated,
        public_transport=public,
    ).preflight(expected_base_sha=prepared.base_sha)

    assert result == GitHubReadiness(
        authenticated_login="respan-delivery-bot",
        authenticated_user_id=101,
        repository=REPOSITORY,
        base_ref="main",
        base_sha=prepared.base_sha,
        publicly_readable=True,
    )
    assert all(request.method == "GET" for request in authenticated.requests)
    assert all(
        request.headers["Authorization"] == f"Bearer {TOKEN}"
        for request in authenticated.requests
    )
    assert all(request.method == "GET" for request in public.requests)
    assert all("Authorization" not in request.headers for request in public.requests)
    assert not hasattr(result, "can_write")


def test_read_only_preflight_rejects_public_base_identity_mismatch() -> None:
    prepared = _prepared()
    authenticated = ScriptedTransport(
        [
            _json_response(200, _user_payload()),
            _json_response(200, _repository_payload()),
            _json_response(200, _ref_payload(prepared)),
        ]
    )
    public = ScriptedTransport(
        [
            _json_response(200, _repository_payload()),
            _json_response(200, _ref_payload(prepared, "3" * 40)),
        ]
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=authenticated,
            public_transport=public,
        ).preflight(expected_base_sha=prepared.base_sha)

    assert caught.value.stage == "base_changed"
    assert all("Authorization" not in request.headers for request in public.requests)


def test_read_only_preflight_stops_on_redacted_authentication_failure() -> None:
    prepared = _prepared()
    authenticated = ScriptedTransport(
        [_json_response(401, {"message": f"bad credential {TOKEN}"})]
    )
    public = ScriptedTransport([_json_response(200, _repository_payload())])

    with pytest.raises(DeliveryError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=authenticated,
            public_transport=public,
        ).preflight()

    assert caught.value.stage == "github_authentication"
    assert caught.value.code == "G_AUTH"
    assert TOKEN not in json.dumps(caught.value.as_dict(), sort_keys=True)
    assert caught.value.__cause__ is None
    assert public.requests == []


def test_moved_base_stops_before_the_only_pr_post() -> None:
    prepared = _prepared()
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared, "3" * 40)),
        ]
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=transport,
        ).create_or_reconcile_pull_request(
            prepared,
            _receipt(prepared),
            commit_sha=COMMIT_SHA,
        )

    assert caught.value.stage == "base_changed"
    assert caught.value.code == "G_BASE_MOVED"
    assert all(request.method == "GET" for request in transport.requests)


@pytest.mark.parametrize(
    ("response_phase", "bad_field"),
    [
        ("existing", "base_sha"),
        ("existing", "fingerprint"),
        ("existing", "head_sha"),
        ("created", "base_sha"),
        ("created", "fingerprint"),
        ("created", "head_sha"),
    ],
)
def test_pr_results_require_exact_base_sha_and_delivery_fingerprint(
    response_phase: str,
    bad_field: str,
) -> None:
    prepared = _prepared()
    overrides: dict[str, str] = {}
    if bad_field == "base_sha":
        overrides["base_sha"] = "3" * 40
    elif bad_field == "head_sha":
        overrides["sha"] = "3" * 40
    else:
        overrides["delivery_fingerprint"] = "3" * 64
    payload = _pr_payload(prepared, **overrides)
    script: list[GitHubHttpResponse]
    if response_phase == "existing":
        script = [_json_response(200, [payload])]
    else:
        script = [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            _json_response(201, payload),
        ]
    transport = ScriptedTransport(script)

    with pytest.raises(DeliveryIntegrityError) as caught:
        GitHubRestClient(
            TOKEN,
            prepared.target,
            transport=transport,
        ).create_or_reconcile_pull_request(
            prepared,
            _receipt(prepared),
            commit_sha=COMMIT_SHA,
        )

    assert sum(request.method == "POST" for request in transport.requests) == (
        response_phase == "created"
    )
    assert caught.value.recovery["pr_number"] == 17
    assert caught.value.recovery["pr_url"] == f"{CANONICAL_URL}/pull/17"
    assert caught.value.recovery["remote_sha"] == (
        "3" * 40 if bad_field == "head_sha" else COMMIT_SHA
    )
    assert caught.value.code == (
        "G_BASE_MOVED" if bad_field == "base_sha" else "G_PR_CONFLICT"
    )


def test_pull_request_body_is_deterministic_bounded_and_evidence_only() -> None:
    prepared = _prepared()
    receipt = _receipt(prepared)

    first = build_pull_request_body(prepared, receipt, commit_sha=COMMIT_SHA)
    second = build_pull_request_body(prepared, receipt, commit_sha=COMMIT_SHA)

    assert first == second
    assert prepared.patch_sha256 in first
    assert prepared.agent_trace_url in first
    assert receipt.target_trace_url in first
    assert set(REQUIRED_VERIFICATION_CHECKS) <= {
        line.removeprefix("- [x] `").removesuffix("`")
        for line in first.splitlines()
        if line.startswith("- [x]")
    }
    assert f"Head: `{prepared.branch}` at `{COMMIT_SHA}`" in first
    assert "`app.py`" in first
    assert "does not add credits or provider credentials" in first
    assert "Review it before merging" in first
    assert len(first.encode()) < 64 * 1024


def _git(path: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["/usr/bin/git", *args],
        cwd=path,
        input=input_bytes,
        check=True,
        capture_output=True,
    ).stdout


def _local_prepared(tmp_path: Path) -> tuple[PreparedDelivery, Path]:
    source = tmp_path / "source"
    remote = tmp_path / "remote.git"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "fixture")
    _git(source, "config", "user.email", "fixture@example.com")
    (source / "app.py").write_text('print("old")\n', encoding="utf-8")
    _git(source, "add", "app.py")
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-08-23T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-08-23T00:00:00+00:00",
    }
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=source,
        env=env,
        check=True,
        capture_output=True,
    )
    base_sha = _git(source, "rev-parse", "HEAD").decode().strip()
    _git(tmp_path, "clone", "--bare", str(source), str(remote))
    (source / "app.py").write_text('print("new")\n', encoding="utf-8")
    patch = _git(
        source,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-renames",
        "HEAD",
    )
    return _prepared(base_sha=base_sha, patch=patch), remote


class LocalRemoteExecutor:
    """Exercise real Git while mapping the frozen GitHub URL to a local bare remote."""

    def __init__(self, remote: Path, *, lose_push_ack: bool = False) -> None:
        self.remote = str(remote)
        self.delegate = SubprocessGitExecutor()
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.lose_push_ack = lose_push_ack
        self.push_askpass: bytes | None = None
        self.local_config_after_push: bytes | None = None

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
    ) -> GitCommandResult:
        original = tuple(args)
        copied_env = dict(env)
        self.calls.append((original, copied_env))
        rewritten = [
            self.remote if value == CANONICAL_URL else value for value in original
        ]
        rewritten = [
            "protocol.allow=always" if value == "protocol.allow=never" else value
            for value in rewritten
        ]
        if "push" in original:
            self.push_askpass = Path(copied_env["GIT_ASKPASS"]).read_bytes()
        result = self.delegate.run(
            rewritten,
            cwd=cwd,
            env=env,
            input_bytes=input_bytes,
            timeout=timeout,
        )
        if "get-url" in original and result.returncode == 0:
            result = GitCommandResult(0, f"{CANONICAL_URL}\n".encode(), result.stderr)
        if self.lose_push_ack and "push" in original and result.returncode == 0:
            result = GitCommandResult(1, b"", b"lost acknowledgement")
        if "push" in original:
            self.local_config_after_push = _git(
                cwd,
                "config",
                "--local",
                "--list",
                "--show-origin",
            )
        return result


class MoveBaseAfterBranchObservationExecutor(LocalRemoteExecutor):
    def __init__(self, remote: Path, *, branch: str, moved_sha: str) -> None:
        super().__init__(remote)
        self.branch = branch
        self.moved_sha = moved_sha
        self.moved = False

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
    ) -> GitCommandResult:
        result = super().run(
            args,
            cwd=cwd,
            env=env,
            input_bytes=input_bytes,
            timeout=timeout,
        )
        if (
            not self.moved
            and "ls-remote" in args
            and args[-1] == f"refs/heads/{self.branch}"
            and result.returncode == 0
        ):
            _git(
                Path(self.remote),
                "update-ref",
                "refs/heads/main",
                self.moved_sha,
            )
            self.moved = True
        return result


class CreateBranchImmediatelyBeforePushExecutor(LocalRemoteExecutor):
    def __init__(self, remote: Path, *, branch: str, base_sha: str) -> None:
        super().__init__(remote)
        self.branch = branch
        self.base_sha = base_sha
        self.created = False

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
    ) -> GitCommandResult:
        if not self.created and "push" in args:
            _git(
                Path(self.remote),
                "update-ref",
                f"refs/heads/{self.branch}",
                self.base_sha,
            )
            self.created = True
        return super().run(
            args,
            cwd=cwd,
            env=env,
            input_bytes=input_bytes,
            timeout=timeout,
        )


def _journal(tmp_path: Path, prepared: PreparedDelivery) -> DeliveryJournal:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    return DeliveryJournal(state, prepared.delivery_fingerprint)


def test_commit_and_push_requires_matching_receipt_before_git_or_journal(
    tmp_path: Path,
) -> None:
    prepared, _remote = _local_prepared(tmp_path)
    other_patch = prepared.patch.replace(b'print("new")', b'print("other")')
    other_prepared = _prepared(patch=other_patch)
    journal = _journal(tmp_path, prepared)

    class ForbiddenExecutor:
        def run(self, *args, **kwargs):  # pragma: no cover - regression sentinel
            raise AssertionError("mismatched receipt reached Git")

    with pytest.raises(DeliveryIntegrityError) as caught:
        commit_and_push(
            prepared,
            _receipt(other_prepared),
            TOKEN,
            journal,
            git_executor=ForbiddenExecutor(),
        )

    assert caught.value.stage == "receipt_validation"
    assert journal.load() is None


def test_future_receipt_blocks_branch_before_git_or_journal(tmp_path: Path) -> None:
    prepared, _remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    future = _receipt(
        prepared,
        backend_verified_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )

    class ForbiddenExecutor:
        def run(self, *args, **kwargs):  # pragma: no cover - regression sentinel
            raise AssertionError("future receipt reached Git")

    with pytest.raises(DeliveryIntegrityError) as caught:
        commit_and_push(
            prepared,
            future,
            TOKEN,
            journal,
            git_executor=ForbiddenExecutor(),
        )

    assert caught.value.stage == "receipt_time"
    assert journal.load() is None


def test_old_receipt_reconciles_existing_pr_without_mutation() -> None:
    prepared = _prepared()
    old_receipt = _receipt(
        prepared,
        backend_verified_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    transport = ScriptedTransport(
        [_json_response(200, [_pr_payload(prepared, receipt=old_receipt)])]
    )

    record, disposition = GitHubRestClient(
        TOKEN,
        prepared.target,
        transport=transport,
    ).create_or_reconcile_pull_request(
        prepared,
        old_receipt,
        commit_sha=COMMIT_SHA,
    )

    assert record.number == 17
    assert disposition is RemoteDisposition.reused
    assert [request.method for request in transport.requests] == ["GET"]


def test_oversized_pr_body_is_rejected_before_git_or_journal(tmp_path: Path) -> None:
    changed_paths = tuple(f"dir/{index:03d}-{'a' * 400}.py" for index in range(200))
    prepared = _prepared(changed_paths=changed_paths)
    journal = _journal(tmp_path, prepared)

    class ForbiddenExecutor:
        def run(self, *args, **kwargs):  # pragma: no cover - regression sentinel
            raise AssertionError("oversized PR body reached Git")

    with pytest.raises(DeliveryIntegrityError) as caught:
        commit_and_push(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            git_executor=ForbiddenExecutor(),
        )

    assert caught.value.stage == "pull_request_body"
    assert journal.load() is None


def test_real_git_commit_push_is_exact_redacted_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    first_executor = LocalRemoteExecutor(remote)
    monkeypatch.setenv("PATH", str(tmp_path / "hostile-path"))

    first = commit_and_push(
        prepared, _receipt(prepared), TOKEN, journal, git_executor=first_executor
    )

    assert first.disposition is RemoteDisposition.created
    assert (
        _git(remote, "rev-parse", prepared.branch).decode().strip() == first.commit_sha
    )
    assert (
        _git(remote, "rev-parse", f"{first.commit_sha}^").decode().strip()
        == prepared.base_sha
    )
    assert _git(remote, "show", f"{first.commit_sha}:app.py") == b'print("new")\n'
    assert journal.load().phase is DeliveryPhase.branch_pushed
    push_calls = [call for call in first_executor.calls if "push" in call[0]]
    clone_calls = [call for call in first_executor.calls if "clone" in call[0]]
    assert len(push_calls) == 1
    assert len(clone_calls) == 1
    assert clone_calls[0][0].count("clone") == 1
    assert all(
        call_args[0] == "/usr/bin/git" for call_args, _env in first_executor.calls
    )
    assert all(
        call_env["PATH"] == "/usr/bin:/bin" for _args, call_env in first_executor.calls
    )
    assert "http.followRedirects=false" in clone_calls[0][0]
    assert "http.sslVerify=true" in clone_calls[0][0]
    assert push_calls[0][1]["RESPAN_GITHUB_TOKEN"] == TOKEN
    assert first_executor.push_askpass is not None
    assert TOKEN.encode() not in first_executor.push_askpass
    assert first_executor.local_config_after_push is not None
    assert TOKEN.encode() not in first_executor.local_config_after_push
    assert TOKEN.encode() not in _git(
        remote, "show", "-s", "--format=%B", first.commit_sha
    )
    for args, env in first_executor.calls:
        assert TOKEN not in " ".join(args)
        if "push" not in args:
            assert "RESPAN_GITHUB_TOKEN" not in env
        assert "--force" not in args
        lease_args = [arg for arg in args if arg.startswith("--force-with-lease")]
        if "push" in args:
            assert lease_args == [f"--force-with-lease=refs/heads/{prepared.branch}:"]
        else:
            assert lease_args == []

    second_executor = LocalRemoteExecutor(remote)
    second = commit_and_push(
        prepared, _receipt(prepared), TOKEN, journal, git_executor=second_executor
    )

    assert second.commit_sha == first.commit_sha
    assert second.disposition is RemoteDisposition.reused
    assert not any("push" in args for args, _env in second_executor.calls)


def test_reused_branch_gets_final_base_recheck_before_return(tmp_path: Path) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    first = commit_and_push(
        prepared,
        _receipt(prepared),
        TOKEN,
        journal,
        git_executor=LocalRemoteExecutor(remote),
    )
    executor = MoveBaseAfterBranchObservationExecutor(
        remote,
        branch=prepared.branch,
        moved_sha=first.commit_sha,
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        commit_and_push(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            git_executor=executor,
        )

    assert caught.value.stage == "base_changed"
    assert caught.value.recovery["phase"] == DeliveryPhase.branch_observed.value
    assert not any("push" in args for args, _env in executor.calls)


def test_successful_push_with_lost_ack_is_reconciled_once(tmp_path: Path) -> None:
    prepared, remote = _local_prepared(tmp_path)
    executor = LocalRemoteExecutor(remote, lose_push_ack=True)
    journal = _journal(tmp_path, prepared)

    result = commit_and_push(
        prepared,
        _receipt(prepared),
        TOKEN,
        journal,
        git_executor=executor,
    )

    assert result.disposition is RemoteDisposition.reused
    assert journal.load().phase is DeliveryPhase.branch_observed
    assert sum("push" in args for args, _env in executor.calls) == 1
    assert (
        _git(remote, "rev-parse", prepared.branch).decode().strip() == result.commit_sha
    )


def test_open_pr_journals_pushed_branch_but_posts_nothing_if_rest_base_moved(
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared, "3" * 40)),
        ]
    )
    client = GitHubRestClient(TOKEN, prepared.target, transport=transport)

    with pytest.raises(DeliveryIntegrityError) as caught:
        open_pr(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            rest_client=client,
            git_executor=LocalRemoteExecutor(remote),
        )

    assert caught.value.stage == "base_changed"
    assert _git(remote, "rev-parse", prepared.branch)
    assert journal.load().phase is DeliveryPhase.branch_pushed
    assert all(request.method == "GET" for request in transport.requests)


def test_open_pr_journals_pr_identity_if_base_moves_during_create(
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)

    def moved_base_created_response(_request: GitHubHttpRequest) -> GitHubHttpResponse:
        commit_sha = _git(remote, "rev-parse", prepared.branch).decode().strip()
        return _json_response(
            201,
            _pr_payload(prepared, sha=commit_sha, base_sha="3" * 40),
        )

    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            moved_base_created_response,
        ]
    )
    client = GitHubRestClient(TOKEN, prepared.target, transport=transport)

    with pytest.raises(DeliveryIntegrityError) as caught:
        open_pr(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            rest_client=client,
            git_executor=LocalRemoteExecutor(remote),
        )

    assert caught.value.code == "G_BASE_MOVED"
    assert caught.value.stage == "base_changed"
    assert sum(request.method == "POST" for request in transport.requests) == 1
    recorded = journal.load()
    assert recorded is not None
    assert recorded.phase is DeliveryPhase.pr_created
    assert recorded.pr_number == 17
    assert recorded.pr_url == f"{prepared.target.canonical_url}/pull/17"
    assert recorded.commit_sha == recorded.remote_sha


def test_open_pr_journals_observed_head_if_created_response_diverges(
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    divergent_sha = "3" * 40
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            _json_response(201, _pr_payload(prepared, sha=divergent_sha)),
        ]
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        open_pr(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            rest_client=GitHubRestClient(
                TOKEN,
                prepared.target,
                transport=transport,
            ),
            git_executor=LocalRemoteExecutor(remote),
        )

    assert caught.value.stage == "github_head_collision"
    assert caught.value.recovery["remote_sha"] == divergent_sha
    recorded = journal.load()
    assert recorded is not None
    assert recorded.phase is DeliveryPhase.pr_created
    assert recorded.pr_number == 17
    assert recorded.remote_sha == divergent_sha


def test_open_pr_preserves_head_collision_and_journals_pr_identity(
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    divergent_sha = "3" * 40
    transport = ScriptedTransport(
        [_json_response(200, [_pr_payload(prepared, sha=divergent_sha)])]
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        open_pr(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            rest_client=GitHubRestClient(
                TOKEN,
                prepared.target,
                transport=transport,
            ),
            git_executor=LocalRemoteExecutor(remote),
        )

    assert caught.value.stage == "github_head_collision"
    assert caught.value.recovery["remote_sha"] == divergent_sha
    recorded = journal.load()
    assert recorded is not None
    assert recorded.phase is DeliveryPhase.pr_observed
    assert recorded.pr_number == 17
    assert recorded.remote_sha == divergent_sha


def test_existing_content_addressed_branch_collision_never_pushes(
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    _git(remote, "update-ref", f"refs/heads/{prepared.branch}", prepared.base_sha)
    executor = LocalRemoteExecutor(remote)

    with pytest.raises(DeliveryIntegrityError) as caught:
        commit_and_push(
            prepared,
            _receipt(prepared),
            TOKEN,
            _journal(tmp_path, prepared),
            git_executor=executor,
        )

    assert caught.value.stage == "branch_collision"
    assert caught.value.code == "G_BRANCH_COLLISION"
    assert not any("push" in args for args, _env in executor.calls)


def test_concurrent_branch_creation_is_never_fast_forwarded(tmp_path: Path) -> None:
    prepared, remote = _local_prepared(tmp_path)
    executor = CreateBranchImmediatelyBeforePushExecutor(
        remote,
        branch=prepared.branch,
        base_sha=prepared.base_sha,
    )

    with pytest.raises(DeliveryIntegrityError) as caught:
        commit_and_push(
            prepared,
            _receipt(prepared),
            TOKEN,
            _journal(tmp_path, prepared),
            git_executor=executor,
        )

    assert caught.value.code == "G_BRANCH_COLLISION"
    assert caught.value.stage == "branch_collision"
    assert executor.created is True
    assert (
        _git(remote, "rev-parse", prepared.branch).decode().strip() == prepared.base_sha
    )
    assert sum("push" in args for args, _env in executor.calls) == 1


def test_journal_failure_after_push_returns_complete_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    original_record = DeliveryJournal.record

    def fail_after_push(self, record):
        if record.phase is DeliveryPhase.branch_pushed:
            raise DeliveryJournalError("journal_record")
        return original_record(self, record)

    monkeypatch.setattr(DeliveryJournal, "record", fail_after_push)

    with pytest.raises(DeliveryRecoveryRequired) as caught:
        commit_and_push(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            git_executor=LocalRemoteExecutor(remote),
        )

    remote_sha = _git(remote, "rev-parse", prepared.branch).decode().strip()
    assert caught.value.stage == "journal_recovery"
    assert caught.value.recovery["phase"] == DeliveryPhase.branch_pushed.value
    assert caught.value.recovery["commit_sha"] == remote_sha
    assert caught.value.recovery["remote_sha"] == remote_sha


def test_journal_failure_after_pr_returns_complete_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prepared, remote = _local_prepared(tmp_path)
    journal = _journal(tmp_path, prepared)
    original_record = DeliveryJournal.record

    def fail_after_pr(self, record):
        if record.phase is DeliveryPhase.pr_created:
            raise DeliveryJournalError("journal_record")
        return original_record(self, record)

    def created_response(_request: GitHubHttpRequest) -> GitHubHttpResponse:
        commit_sha = _git(remote, "rev-parse", prepared.branch).decode().strip()
        return _json_response(201, _pr_payload(prepared, sha=commit_sha))

    monkeypatch.setattr(DeliveryJournal, "record", fail_after_pr)
    transport = ScriptedTransport(
        [
            _json_response(200, []),
            _json_response(200, _ref_payload(prepared)),
            created_response,
        ]
    )

    with pytest.raises(DeliveryRecoveryRequired) as caught:
        open_pr(
            prepared,
            _receipt(prepared),
            TOKEN,
            journal,
            rest_client=GitHubRestClient(
                TOKEN,
                prepared.target,
                transport=transport,
            ),
            git_executor=LocalRemoteExecutor(remote),
        )

    assert caught.value.stage == "journal_recovery"
    assert caught.value.recovery["phase"] == DeliveryPhase.pr_created.value
    assert caught.value.recovery["pr_number"] == 17
    assert caught.value.recovery["pr_url"] == f"{CANONICAL_URL}/pull/17"


def test_workflow_patch_is_rejected_before_git_or_token_use(tmp_path: Path) -> None:
    patch = b"""diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
new file mode 100644
index 0000000..8b13789
--- /dev/null
+++ b/.github/workflows/ci.yml
@@ -0,0 +1 @@
+name: ci
"""
    with pytest.raises(ValueError):
        _prepared(
            patch=patch,
            changed_paths=(".github/workflows/ci.yml",),
        )

    # Preserve a defense-in-depth regression test at the Git boundary even though
    # the immutable preparation contract already rejects this path.
    prepared = _prepared()
    receipt = _receipt(prepared)
    object.__setattr__(prepared, "patch", patch)
    object.__setattr__(
        prepared,
        "patch_sha256",
        hashlib.sha256(patch).hexdigest(),
    )
    object.__setattr__(prepared, "changed_paths", (".github/workflows/ci.yml",))
    object.__setattr__(receipt, "patch_sha256", prepared.patch_sha256)
    object.__setattr__(receipt, "changed_paths", prepared.changed_paths)

    class ForbiddenExecutor:
        def run(self, *args, **kwargs):  # pragma: no cover - regression sentinel
            raise AssertionError("unsafe patch reached Git")

    with pytest.raises(DeliveryIntegrityError) as caught:
        commit_and_push(
            prepared,
            receipt,
            TOKEN,
            _journal(tmp_path, prepared),
            git_executor=ForbiddenExecutor(),
        )

    assert caught.value.stage == "workflow_change"
    assert caught.value.code == "G_PATCH_MISMATCH"


def test_git_failure_evidence_never_contains_token_or_subprocess_output(
    tmp_path: Path,
) -> None:
    prepared, _remote = _local_prepared(tmp_path)

    class HostileExecutor:
        def run(self, *args, **kwargs):
            return GitCommandResult(
                1, TOKEN.encode(), f"Authorization: {TOKEN}".encode()
            )

    with pytest.raises(DeliveryError) as caught:
        commit_and_push(
            prepared,
            _receipt(prepared),
            TOKEN,
            _journal(tmp_path, prepared),
            git_executor=HostileExecutor(),
        )

    serialized = json.dumps(caught.value.as_dict(), sort_keys=True)
    assert TOKEN not in serialized
    assert "Authorization" not in serialized
    assert caught.value.__cause__ is None
    assert caught.value.code == "G_TRANSPORT"
