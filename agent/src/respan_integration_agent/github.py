"""Fail-closed GitHub delivery for an already accepted onboarding patch.

Only this module may create the content-addressed remote branch and pull request.
The agent never receives the GitHub credential, and both mutations require a
``VerificationReceipt`` matching an immutable ``PreparedDelivery``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import selectors
import signal
import ssl
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from .delivery import (
    DeliveryAuthenticationError,
    DeliveryAuthorizationError,
    DeliveryBranchCollisionError,
    DeliveryError,
    DeliveryIntegrityError,
    DeliveryJournal,
    DeliveryJournalError,
    DeliveryJournalRecord,
    DeliveryManifest,
    DeliveryPhase,
    DeliveryPullRequestConflictError,
    DeliveryRateLimitError,
    DeliveryRecoveryRequired,
    DeliveryRepositoryNotFoundError,
    DeliveryResponseSchemaError,
    DeliveryTransportError,
    PreparedDelivery,
    RemoteDisposition,
    RepositoryTarget,
    VerificationReceipt,
)


GITHUB_API_ORIGIN = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
_USER_AGENT = "respan-integration-agent/0.0.1"
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_READ_TIMEOUT_SECONDS = 15.0
_DEFAULT_GIT_TIMEOUT_SECONDS = 60.0
_DEFAULT_HTTP_OPERATION_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_GIT_OUTPUT_BYTES = 1024 * 1024
_PAGE_SIZE = 100
_MAX_PAGES = 20
_MAX_RECEIPT_CLOCK_SKEW = timedelta(minutes=1)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,4096}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECK_RE = re.compile(r"^[a-z][a-z0-9-]{0,95}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_DELIVERY_MARKER_RE = re.compile(r"<!-- respan-delivery:([0-9a-f]{64}) -->")
_GITHUB_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
_RETRIABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
_PHASE_RANK = {phase: index for index, phase in enumerate(DeliveryPhase)}


class _TransportFailure(Exception):
    """Internal marker that never retains request material."""


class _GitFailure(Exception):
    """Internal marker that never retains subprocess material."""


def _error(
    stage: str,
    *,
    status_code: int | None = None,
    attempts: int | None = None,
    recovery: Mapping[str, object] | None = None,
) -> DeliveryError:
    error_type: type[DeliveryError]
    if stage in {"authentication", "github_authentication"}:
        error_type = DeliveryAuthenticationError
    elif stage == "github_authorization":
        error_type = DeliveryAuthorizationError
    elif stage == "github_not_found":
        error_type = DeliveryRepositoryNotFoundError
    elif stage == "github_rate_limit":
        error_type = DeliveryRateLimitError
    elif stage in {"git_transport", "github_transport"}:
        error_type = DeliveryTransportError
    elif stage in {
        "github_content_type",
        "github_pagination",
        "github_redirect",
        "github_request",
        "github_retry_header",
        "github_schema",
    }:
        error_type = DeliveryResponseSchemaError
    elif stage in {"github_conflict", "github_validation"}:
        error_type = DeliveryPullRequestConflictError
    elif stage == "branch_collision":
        error_type = DeliveryBranchCollisionError
    elif stage == "push_ambiguous":
        error_type = DeliveryRecoveryRequired
    else:
        error_type = DeliveryError
    return error_type(
        stage,
        status_code=status_code,
        attempts=attempts,
        recovery=dict(recovery) if recovery is not None else None,
    )


def _recovery_required(
    prepared: PreparedDelivery,
    *,
    commit_sha: str,
) -> DeliveryRecoveryRequired:
    return DeliveryRecoveryRequired(
        "github_recovery",
        attempts=1,
        recovery=_recovery(
            prepared,
            phase=DeliveryPhase.branch_pushed,
            commit_sha=commit_sha,
            remote_sha=commit_sha,
        ),
    )


def _recovery(
    prepared: PreparedDelivery,
    *,
    phase: DeliveryPhase | None = None,
    commit_sha: str | None = None,
    remote_sha: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "repository": prepared.target.slug,
        "base_ref": prepared.target.base_ref,
        "base_sha": prepared.base_sha,
        "branch": prepared.branch,
        "delivery_fingerprint": prepared.delivery_fingerprint,
    }
    for key, value in (
        ("phase", phase.value if phase is not None else None),
        ("commit_sha", commit_sha),
        ("remote_sha", remote_sha),
        ("pr_number", pr_number),
        ("pr_url", pr_url),
    ):
        if value is not None:
            result[key] = value
    return result


def _require_sha(value: object) -> str:
    if (
        not isinstance(value, str)
        or _SHA_RE.fullmatch(value) is None
        or int(value, 16) == 0
    ):
        raise DeliveryIntegrityError("git_identity")
    return value


def _require_branch(value: object) -> str:
    if (
        not isinstance(value, str)
        or _BRANCH_RE.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or value.endswith((".", "/", ".lock"))
        or value.startswith(("-", "/", "."))
        or "@{" in value
    ):
        raise DeliveryIntegrityError("branch_identity")
    return value


def _require_token(value: object) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
        raise _error("authentication")
    return value


def _validate_receipt(
    prepared: PreparedDelivery,
    receipt: VerificationReceipt,
) -> None:
    if not isinstance(receipt, VerificationReceipt):
        raise DeliveryIntegrityError("delivery_contract")
    receipt.validate_for(prepared)
    now = datetime.now(timezone.utc)
    # Exact accepted evidence remains valid for deterministic reconciliation as long
    # as its frozen base still exists. Expiring it would make recovery impossible
    # after a lost response; every mutation boundary independently rechecks the base.
    if receipt.backend_verified_at > now + _MAX_RECEIPT_CLOCK_SKEW:
        raise DeliveryIntegrityError("receipt_time")


def _positive_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 120:
        raise ValueError("timeout must be between 0 and 120 seconds")
    return result


def _validate_api_url(value: object, *, expected_path: str | None = None):
    if not isinstance(value, str):
        raise _TransportFailure from None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        raise _TransportFailure from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or parsed.netloc != "api.github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or (expected_path is not None and parsed.path != expected_path)
    ):
        raise _TransportFailure from None
    return parsed


@dataclass(frozen=True, slots=True)
class GitHubHttpRequest:
    """Bounded request whose credential and JSON body stay out of ``repr``."""

    method: str
    url: str
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    read_timeout: float = _DEFAULT_READ_TIMEOUT_SECONDS
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES
    overall_timeout: float = _DEFAULT_HTTP_OPERATION_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class GitHubHttpResponse:
    status: int
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)


@runtime_checkable
class GitHubHttpTransport(Protocol):
    def request(self, request: GitHubHttpRequest) -> GitHubHttpResponse: ...


class DirectGitHubHttpsTransport:
    """Verified direct TLS to api.github.com, without proxies or redirects."""

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        context = ssl_context or ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("GitHub HTTPS transport requires verified TLS")
        self._ssl_context = context

    def request(self, request: GitHubHttpRequest) -> GitHubHttpResponse:
        parsed = _validate_api_url(request.url)
        try:
            overall_timeout = _positive_timeout(request.overall_timeout)
            connect_timeout = _positive_timeout(request.connect_timeout)
            read_timeout = _positive_timeout(request.read_timeout)
        except ValueError:
            raise _TransportFailure from None
        deadline = time.monotonic() + overall_timeout

        def bounded_timeout(configured: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransportFailure from None
            return min(configured, remaining)

        request_target = urlunsplit(("", "", parsed.path, parsed.query, ""))
        connection = http.client.HTTPSConnection(
            "api.github.com",
            443,
            timeout=bounded_timeout(connect_timeout),
            context=self._ssl_context,
        )
        result: GitHubHttpResponse | None = None
        failed = False
        try:
            connection.connect()
            if connection.sock is None:  # pragma: no cover - stdlib invariant
                raise OSError
            connection.sock.settimeout(bounded_timeout(read_timeout))
            connection.request(
                request.method,
                request_target,
                body=request.body,
                headers=dict(request.headers),
            )
            connection.sock.settimeout(bounded_timeout(read_timeout))
            response = connection.getresponse()
            declared = response.getheader("Content-Length")
            if declared is not None:
                try:
                    declared_length = int(declared)
                except ValueError:
                    raise _TransportFailure from None
                if not 0 <= declared_length <= request.max_response_bytes:
                    raise _TransportFailure from None
            chunks: list[bytes] = []
            received = 0
            while received <= request.max_response_bytes:
                connection.sock.settimeout(bounded_timeout(read_timeout))
                chunk = response.read1(
                    min(64 * 1024, request.max_response_bytes + 1 - received)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
            if received > request.max_response_bytes:
                raise _TransportFailure from None
            bounded_timeout(read_timeout)
            body = b"".join(chunks)
            headers = {
                str(key).lower(): str(value) for key, value in response.getheaders()
            }
            result = GitHubHttpResponse(response.status, headers, body)
        except _TransportFailure:
            raise
        except Exception:
            failed = True
        finally:
            try:
                connection.close()
            except Exception:
                failed = True
        if failed or result is None:
            raise _TransportFailure from None
        return result


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    number: int
    url: str
    title: str = field(repr=False)
    body: str = field(repr=False)
    state: str
    branch: str
    base_ref: str
    head_sha: str
    base_sha: str
    delivery_fingerprint: str | None
    merged: bool


@dataclass(frozen=True, slots=True)
class GitHubReadiness:
    """Read-only identity evidence; it deliberately makes no write claim."""

    authenticated_login: str
    authenticated_user_id: int
    repository: str
    base_ref: str
    base_sha: str
    publicly_readable: bool


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _decode_json(body: bytes, *, expect_list: bool) -> object:
    if not body:
        raise DeliveryIntegrityError("github_schema")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeError, ValueError, TypeError):
        raise DeliveryIntegrityError("github_schema") from None
    if expect_list != isinstance(value, list):
        raise DeliveryIntegrityError("github_schema")
    if not expect_list and not isinstance(value, dict):
        raise DeliveryIntegrityError("github_schema")
    return value


def _encode_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError):  # pragma: no cover - statically bounded payloads
        raise DeliveryIntegrityError("github_schema") from None


def _headers(value: Mapping[str, str]) -> dict[str, str]:
    try:
        return {str(key).lower(): str(item) for key, item in value.items()}
    except Exception:
        raise _error("github_transport") from None


def _parse_pr(
    value: object,
    *,
    target: RepositoryTarget,
    branch: str,
    base_ref: str,
    expected_base_sha: str | None = None,
    expected_delivery_fingerprint: str | None = None,
) -> PullRequestRecord:
    if not isinstance(value, dict):
        raise DeliveryIntegrityError("github_schema")
    number = value.get("number")
    url = value.get("html_url")
    state = value.get("state")
    merged_at = value.get("merged_at")
    title = value.get("title")
    body = value.get("body")
    head = value.get("head")
    base = value.get("base")
    if (
        type(number) is not int
        or number <= 0
        or not isinstance(url, str)
        or state not in {"open", "closed"}
        or not isinstance(title, str)
        or not 1 <= len(title) <= 256
        or any(ord(character) < 32 or ord(character) == 127 for character in title)
        or (
            merged_at is not None
            and (
                not isinstance(merged_at, str)
                or _GITHUB_TIMESTAMP_RE.fullmatch(merged_at) is None
            )
        )
        or (state == "open" and merged_at is not None)
        or not isinstance(body, str)
        or len(body.encode("utf-8")) > 64 * 1024
        or not isinstance(head, dict)
        or not isinstance(base, dict)
    ):
        raise DeliveryIntegrityError("github_schema")
    head_repo = head.get("repo")
    base_repo = base.get("repo")
    head_sha = head.get("sha")
    base_sha = base.get("sha")
    markers = _DELIVERY_MARKER_RE.findall(body)
    expected_url = f"https://github.com/{target.slug}/pull/{number}"
    if (
        url != expected_url
        or head.get("ref") != branch
        or base.get("ref") != base_ref
        or not isinstance(head_sha, str)
        or _SHA_RE.fullmatch(head_sha) is None
        or int(head_sha, 16) == 0
        or not isinstance(base_sha, str)
        or _SHA_RE.fullmatch(base_sha) is None
        or int(base_sha, 16) == 0
        or not isinstance(head_repo, dict)
        or not isinstance(base_repo, dict)
        or not isinstance(head_repo.get("full_name"), str)
        or not isinstance(base_repo.get("full_name"), str)
        or head_repo["full_name"].casefold() != target.slug.casefold()
        or base_repo["full_name"].casefold() != target.slug.casefold()
        or (expected_base_sha is not None and base_sha != expected_base_sha)
        or (
            expected_delivery_fingerprint is not None
            and (len(markers) != 1 or markers[0] != expected_delivery_fingerprint)
        )
    ):
        raise DeliveryIntegrityError("github_schema")
    return PullRequestRecord(
        number,
        url,
        title,
        body,
        state,
        branch,
        base_ref,
        head_sha,
        base_sha,
        markers[0] if len(markers) == 1 else None,
        state == "closed" and merged_at is not None,
    )


def _parse_authenticated_user(value: object) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise DeliveryIntegrityError("github_schema")
    login = value.get("login")
    user_id = value.get("id")
    account_type = value.get("type")
    if (
        not isinstance(login, str)
        or _LOGIN_RE.fullmatch(login) is None
        or type(user_id) is not int
        or user_id <= 0
        or account_type not in {"User", "Bot"}
    ):
        raise DeliveryIntegrityError("github_schema")
    return login, user_id


def _parse_repository(value: object, target: RepositoryTarget) -> int:
    if not isinstance(value, dict):
        raise DeliveryIntegrityError("github_schema")
    repository_id = value.get("id")
    owner = value.get("owner")
    if (
        type(repository_id) is not int
        or repository_id <= 0
        or value.get("name") != target.repo
        or value.get("full_name") != target.slug
        or value.get("html_url") != target.canonical_url
        or value.get("private") is not False
        or value.get("visibility") != "public"
        or value.get("archived") is not False
        or value.get("disabled") is not False
        or value.get("fork") is not False
        or not isinstance(owner, dict)
        or owner.get("login") != target.owner
    ):
        raise DeliveryIntegrityError("github_schema")
    return repository_id


def _parse_base_ref(value: object, target: RepositoryTarget) -> str:
    if not isinstance(value, dict):
        raise DeliveryIntegrityError("github_schema")
    git_object = value.get("object")
    if (
        value.get("ref") != f"refs/heads/{target.base_ref}"
        or not isinstance(git_object, dict)
        or git_object.get("type") != "commit"
    ):
        raise DeliveryIntegrityError("github_schema")
    return _require_sha(git_object.get("sha"))


def _next_link(
    header: str | None,
    *,
    expected_path: str,
    fixed_query: Mapping[str, str],
    expected_page: int,
) -> str | None:
    if not header:
        return None
    next_urls: list[str] = []
    for part in header.split(","):
        sections = [section.strip() for section in part.split(";")]
        if (
            len(sections) < 2
            or not sections[0].startswith("<")
            or not sections[0].endswith(">")
        ):
            raise DeliveryIntegrityError("github_pagination")
        relations = [
            section[5:-1]
            for section in sections[1:]
            if section.startswith('rel="') and section.endswith('"')
        ]
        if "next" in relations:
            next_urls.append(sections[0][1:-1])
    if len(next_urls) > 1:
        raise DeliveryIntegrityError("github_pagination")
    if not next_urls:
        return None
    try:
        parsed = _validate_api_url(next_urls[0], expected_path=expected_path)
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    except (ValueError, _TransportFailure):
        raise DeliveryIntegrityError("github_pagination") from None
    expected_keys = {*fixed_query, "page"}
    if set(query) != expected_keys or query.get("page") != [str(expected_page)]:
        raise DeliveryIntegrityError("github_pagination")
    if any(query.get(key) != [expected] for key, expected in fixed_query.items()):
        raise DeliveryIntegrityError("github_pagination")
    return (
        f"{GITHUB_API_ORIGIN}{expected_path}?"
        f"{urlencode({**fixed_query, 'page': str(expected_page)})}"
    )


class GitHubRestClient:
    """Strict injectable adapter for one same-repository pull request."""

    def __init__(
        self,
        token: str,
        target: RepositoryTarget,
        *,
        transport: GitHubHttpTransport | None = None,
        public_transport: GitHubHttpTransport | None = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
        read_timeout: float = _DEFAULT_READ_TIMEOUT_SECONDS,
        operation_timeout: float = _DEFAULT_HTTP_OPERATION_TIMEOUT_SECONDS,
        max_get_attempts: int = 3,
        retry_delay_seconds: float = 0.25,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._token = _require_token(token)
        if not isinstance(target, RepositoryTarget):
            raise DeliveryIntegrityError("repository_target")
        self._target = target
        self._transport = (
            transport if transport is not None else DirectGitHubHttpsTransport()
        )
        self._public_transport = (
            public_transport if public_transport is not None else self._transport
        )
        self._connect_timeout = _positive_timeout(connect_timeout)
        self._read_timeout = _positive_timeout(read_timeout)
        self._operation_timeout = _positive_timeout(operation_timeout)
        if type(max_get_attempts) is not int or not 1 <= max_get_attempts <= 3:
            raise ValueError("max_get_attempts must be between one and three")
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, (int, float))
            or not math.isfinite(float(retry_delay_seconds))
            or not 0 <= float(retry_delay_seconds) <= 2
        ):
            raise ValueError("retry_delay_seconds is outside the supported range")
        if not callable(sleep) or not callable(monotonic):
            raise ValueError("sleep and monotonic must be callable")
        self._max_get_attempts = max_get_attempts
        self._retry_delay_seconds = float(retry_delay_seconds)
        self._sleep = sleep
        self._monotonic = monotonic
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= _MAX_RESPONSE_BYTES
        ):
            raise ValueError("max_response_bytes is outside the supported range")
        self._max_response_bytes = max_response_bytes

    @property
    def target(self) -> RepositoryTarget:
        return self._target

    def preflight(
        self,
        *,
        expected_base_sha: str | None = None,
    ) -> GitHubReadiness:
        """Prove auth identity and public repo/base readability using GETs only.

        This intentionally does not infer push or pull-request permission.  The
        unauthenticated calls use a distinct injectable seam and carry no
        ``Authorization`` header.
        """

        locked_expected = (
            _require_sha(expected_base_sha) if expected_base_sha is not None else None
        )
        user = self._get_json(
            "github_user",
            f"{GITHUB_API_ORIGIN}/user",
            authenticated=True,
        )
        login, user_id = _parse_authenticated_user(user)
        repository_url = f"{GITHUB_API_ORIGIN}{self._repository_path()}"
        authenticated_repository = self._get_json(
            "github_repository",
            repository_url,
            authenticated=True,
        )
        authenticated_repository_id = _parse_repository(
            authenticated_repository, self._target
        )
        authenticated_base = self.resolve_base_sha(authenticated=True)

        public_repository = self._get_json(
            "github_public_repository",
            repository_url,
            authenticated=False,
        )
        public_repository_id = _parse_repository(public_repository, self._target)
        public_base = self.resolve_base_sha(authenticated=False)
        if (
            public_repository_id != authenticated_repository_id
            or public_base != authenticated_base
            or (locked_expected is not None and authenticated_base != locked_expected)
        ):
            raise DeliveryIntegrityError("base_changed")
        return GitHubReadiness(
            authenticated_login=login,
            authenticated_user_id=user_id,
            repository=self._target.slug,
            base_ref=self._target.base_ref,
            base_sha=authenticated_base,
            publicly_readable=True,
        )

    def resolve_base_sha(self, *, authenticated: bool = True) -> str:
        """Resolve the exact base ref through a strict read-only REST call."""

        value = self._get_json(
            "github_base",
            f"{GITHUB_API_ORIGIN}{self._base_ref_path()}",
            authenticated=authenticated,
        )
        return _parse_base_ref(value, self._target)

    def _repository_path(self) -> str:
        return (
            f"/repos/{quote(self._target.owner, safe='')}/"
            f"{quote(self._target.repo, safe='')}"
        )

    def _base_ref_path(self) -> str:
        return (
            f"{self._repository_path()}/git/ref/heads/"
            f"{quote(self._target.base_ref, safe='')}"
        )

    def _get_json(
        self,
        operation: str,
        url: str,
        *,
        authenticated: bool,
    ) -> dict[str, object]:
        response = self._request(
            operation,
            "GET",
            url,
            None,
            authenticated=authenticated,
        )
        if response.status != 200:
            self._raise_status(operation, response)
        self._require_json(response)
        value = _decode_json(response.body, expect_list=False)
        assert isinstance(value, dict)
        return value

    def list_exact_pull_requests(
        self,
        *,
        branch: str,
        base_ref: str | None = None,
        base_sha: str | None = None,
        delivery_fingerprint: str | None = None,
    ) -> tuple[PullRequestRecord, ...]:
        locked_branch = _require_branch(branch)
        locked_base = _require_branch(
            base_ref if base_ref is not None else self._target.base_ref
        )
        locked_base_sha = _require_sha(base_sha) if base_sha is not None else None
        if delivery_fingerprint is not None and (
            not isinstance(delivery_fingerprint, str)
            or _FINGERPRINT_RE.fullmatch(delivery_fingerprint) is None
        ):
            raise DeliveryIntegrityError("delivery_fingerprint")
        path = f"{self._repository_path()}/pulls"
        fixed_query = {
            "state": "all",
            "head": f"{self._target.owner}:{locked_branch}",
            "base": locked_base,
            "per_page": str(_PAGE_SIZE),
        }
        page = 1
        url = (
            f"{GITHUB_API_ORIGIN}{path}?{urlencode({**fixed_query, 'page': str(page)})}"
        )
        seen_urls: set[str] = set()
        seen_numbers: set[int] = set()
        records: list[PullRequestRecord] = []
        while True:
            if page > _MAX_PAGES or url in seen_urls:
                raise DeliveryIntegrityError("github_pagination")
            seen_urls.add(url)
            response = self._request("list_prs", "GET", url, None)
            if response.status != 200:
                self._raise_status("list_prs", response)
            self._require_json(response)
            payload = _decode_json(response.body, expect_list=True)
            assert isinstance(payload, list)
            for value in payload:
                record = _parse_pr(
                    value,
                    target=self._target,
                    branch=locked_branch,
                    base_ref=locked_base,
                    expected_base_sha=locked_base_sha,
                    expected_delivery_fingerprint=delivery_fingerprint,
                )
                if record.number in seen_numbers:
                    raise DeliveryIntegrityError("github_schema")
                seen_numbers.add(record.number)
                records.append(record)
            page += 1
            url = _next_link(
                _headers(response.headers).get("link"),
                expected_path=path,
                fixed_query=fixed_query,
                expected_page=page,
            )
            if url is None:
                return tuple(records)

    def create_or_reconcile_pull_request(
        self,
        prepared: PreparedDelivery,
        receipt: VerificationReceipt,
        *,
        commit_sha: str,
    ) -> tuple[PullRequestRecord, RemoteDisposition]:
        _validate_receipt(prepared, receipt)
        if prepared.target != self._target:
            raise DeliveryIntegrityError("repository_target")
        locked_commit = _require_sha(commit_sha)
        existing = self._one_existing(prepared, receipt, locked_commit)
        if existing is not None:
            return existing, RemoteDisposition.reused

        # Keep this immediately before the sole POST.  A branch may already have
        # been pushed, but a moved base must never result in a pull request.
        if self.resolve_base_sha(authenticated=True) != prepared.base_sha:
            raise DeliveryIntegrityError(
                "base_changed",
                recovery=_recovery(
                    prepared,
                    phase=DeliveryPhase.branch_pushed,
                    commit_sha=locked_commit,
                    remote_sha=locked_commit,
                ),
            )

        path = f"{self._repository_path()}/pulls"
        pr_title = build_pull_request_title(prepared)
        pr_body = build_pull_request_body(
            prepared,
            receipt,
            commit_sha=locked_commit,
        )
        body = _encode_json(
            {
                "base": prepared.target.base_ref,
                "body": pr_body,
                "draft": False,
                "head": prepared.branch,
                "maintainer_can_modify": True,
                "title": pr_title,
            }
        )
        failure: DeliveryError | None = None
        try:
            response = self._request(
                "create_pr", "POST", f"{GITHUB_API_ORIGIN}{path}", body
            )
            if response.status == 201:
                try:
                    self._require_json(response)
                    record = _parse_pr(
                        _decode_json(response.body, expect_list=False),
                        target=self._target,
                        branch=prepared.branch,
                        base_ref=prepared.target.base_ref,
                    )
                except DeliveryError:
                    # The POST may have committed even when its 201 response is
                    # truncated or malformed. Treat the result as ambiguous and
                    # recover by lookup; never repeat the POST.
                    raise _error(
                        "github_transport",
                        status_code=201,
                        attempts=1,
                    ) from None
                if record.head_sha != locked_commit:
                    raise DeliveryIntegrityError(
                        "github_head_collision",
                        recovery=_recovery(
                            prepared,
                            phase=DeliveryPhase.pr_created,
                            commit_sha=locked_commit,
                            remote_sha=record.head_sha,
                            pr_number=record.number,
                            pr_url=record.url,
                        ),
                    )
                if record.base_sha != prepared.base_sha:
                    # GitHub does not expose a conditional create-by-base-SHA. A
                    # base move in the final GET-to-POST interval can therefore
                    # create a PR before the response reveals the race. Return its
                    # exact identity so the outer journal can durably record it.
                    raise DeliveryIntegrityError(
                        "base_changed",
                        recovery=_recovery(
                            prepared,
                            phase=DeliveryPhase.pr_created,
                            commit_sha=locked_commit,
                            remote_sha=record.head_sha,
                            pr_number=record.number,
                            pr_url=record.url,
                        ),
                    )
                if (
                    record.delivery_fingerprint != prepared.delivery_fingerprint
                    or record.title != pr_title
                    or record.body != pr_body
                ):
                    raise DeliveryIntegrityError(
                        "github_pr_content",
                        recovery=_recovery(
                            prepared,
                            phase=DeliveryPhase.pr_created,
                            commit_sha=locked_commit,
                            remote_sha=record.head_sha,
                            pr_number=record.number,
                            pr_url=record.url,
                        ),
                    )
                if record.state != "open":
                    raise DeliveryIntegrityError(
                        "github_schema",
                        recovery=_recovery(
                            prepared,
                            phase=DeliveryPhase.pr_created,
                            commit_sha=locked_commit,
                            remote_sha=record.head_sha,
                            pr_number=record.number,
                            pr_url=record.url,
                        ),
                    )
                return record, RemoteDisposition.created
            if 200 <= response.status <= 299:
                # A successful-but-unsupported response can still mean the POST
                # committed. Reconcile by exact lookup and never issue another POST.
                raise _error(
                    "github_transport",
                    status_code=response.status,
                    attempts=1,
                )
            self._raise_status("create_pr", response)
        except DeliveryError as error:
            failure = error

        assert failure is not None
        reconcile = (
            failure.status_code == 422
            or failure.status_code in _RETRIABLE_HTTP_STATUSES
            or failure.stage == "github_transport"
        )
        if not reconcile:
            raise failure from None
        try:
            existing = self._one_existing(prepared, receipt, locked_commit)
        except DeliveryError as reconciliation_error:
            if (
                "pr_number" in reconciliation_error.recovery
                and "pr_url" in reconciliation_error.recovery
            ):
                raise reconciliation_error from None
            raise _recovery_required(
                prepared,
                commit_sha=locked_commit,
            ) from None
        if existing is not None:
            return existing, RemoteDisposition.reused
        if failure.stage == "github_transport":
            raise _recovery_required(
                prepared,
                commit_sha=locked_commit,
            ) from None
        raise failure from None

    def _one_existing(
        self,
        prepared: PreparedDelivery,
        receipt: VerificationReceipt,
        commit_sha: str,
    ) -> PullRequestRecord | None:
        records = self.list_exact_pull_requests(
            branch=prepared.branch,
            base_ref=prepared.target.base_ref,
        )
        if len(records) > 1:
            raise DeliveryIntegrityError(
                "github_duplicate_pr",
                recovery=_recovery(
                    prepared,
                    phase=DeliveryPhase.branch_pushed,
                    commit_sha=commit_sha,
                    remote_sha=commit_sha,
                ),
            )
        if not records:
            return None
        record = records[0]
        if record.head_sha != commit_sha:
            raise DeliveryIntegrityError(
                "github_head_collision",
                recovery=_recovery(
                    prepared,
                    phase=DeliveryPhase.pr_observed,
                    commit_sha=commit_sha,
                    remote_sha=record.head_sha,
                    pr_number=record.number,
                    pr_url=record.url,
                ),
            )
        if record.base_sha != prepared.base_sha:
            raise DeliveryIntegrityError(
                "base_changed",
                recovery=_recovery(
                    prepared,
                    phase=DeliveryPhase.pr_observed,
                    commit_sha=commit_sha,
                    remote_sha=record.head_sha,
                    pr_number=record.number,
                    pr_url=record.url,
                ),
            )
        if record.delivery_fingerprint != prepared.delivery_fingerprint:
            raise DeliveryIntegrityError(
                "github_pr_content",
                recovery=_recovery(
                    prepared,
                    phase=DeliveryPhase.pr_observed,
                    commit_sha=commit_sha,
                    remote_sha=record.head_sha,
                    pr_number=record.number,
                    pr_url=record.url,
                ),
            )
        if record.state == "closed" and not record.merged:
            raise DeliveryIntegrityError(
                "github_closed_pr",
                recovery=_recovery(
                    prepared,
                    phase=DeliveryPhase.pr_observed,
                    commit_sha=commit_sha,
                    remote_sha=commit_sha,
                    pr_number=record.number,
                    pr_url=record.url,
                ),
            )
        expected_title = build_pull_request_title(prepared)
        expected_body = build_pull_request_body(
            prepared,
            receipt,
            commit_sha=commit_sha,
        )
        if record.title != expected_title or record.body != expected_body:
            raise DeliveryIntegrityError(
                "github_pr_content",
                recovery=_recovery(
                    prepared,
                    phase=DeliveryPhase.pr_observed,
                    commit_sha=commit_sha,
                    remote_sha=commit_sha,
                    pr_number=record.number,
                    pr_url=record.url,
                ),
            )
        return record

    def _request(
        self,
        operation: str,
        method: str,
        url: str,
        body: bytes | None,
        *,
        authenticated: bool = True,
    ) -> GitHubHttpResponse:
        if method not in {"GET", "POST"} or (method == "GET" and body is not None):
            raise DeliveryIntegrityError("github_request")
        try:
            _validate_api_url(url)
        except _TransportFailure:
            raise _error("github_transport") from None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            deadline = self._monotonic() + self._operation_timeout
        except Exception:
            raise _error("github_transport", attempts=1) from None
        max_attempts = self._max_get_attempts if method == "GET" else 1
        transport = self._transport if authenticated else self._public_transport
        for attempt in range(1, max_attempts + 1):
            try:
                remaining = deadline - self._monotonic()
            except Exception:
                raise _error("github_transport", attempts=attempt) from None
            if remaining <= 0:
                raise _error("github_transport", attempts=attempt)
            request = GitHubHttpRequest(
                method,
                url,
                headers,
                body,
                min(self._connect_timeout, remaining),
                min(self._read_timeout, remaining),
                self._max_response_bytes,
                remaining,
            )
            try:
                response = transport.request(request)
            except Exception:
                if attempt == max_attempts:
                    raise _error("github_transport", attempts=attempt) from None
                self._wait_before_retry(deadline, attempt, None)
                continue
            try:
                if deadline - self._monotonic() <= 0:
                    raise _error("github_transport", attempts=attempt)
            except DeliveryError:
                raise
            except Exception:
                raise _error("github_transport", attempts=attempt) from None
            if (
                not isinstance(response, GitHubHttpResponse)
                or type(response.status) is not int
                or not 100 <= response.status <= 599
                or not isinstance(response.headers, Mapping)
                or not isinstance(response.body, bytes)
                or len(response.body) > self._max_response_bytes
            ):
                raise _error("github_transport", attempts=attempt)
            if method == "GET" and self._is_retriable_get_response(response):
                if attempt < max_attempts:
                    self._wait_before_retry(deadline, attempt, response)
                    continue
                headers = _headers(response.headers)
                rate_limited = response.status == 429 or (
                    response.status == 403
                    and (
                        headers.get("x-ratelimit-remaining") == "0"
                        or "retry-after" in headers
                    )
                )
                raise _error(
                    "github_rate_limit" if rate_limited else "github_transport",
                    status_code=response.status,
                    attempts=attempt,
                )
            return response
        raise _error("github_transport", attempts=max_attempts)  # pragma: no cover

    @staticmethod
    def _is_retriable_get_response(response: GitHubHttpResponse) -> bool:
        if response.status in _RETRIABLE_HTTP_STATUSES:
            return True
        if response.status != 403:
            return False
        headers = _headers(response.headers)
        return headers.get("x-ratelimit-remaining") == "0" or "retry-after" in headers

    def _wait_before_retry(
        self,
        deadline: float,
        attempt: int,
        response: GitHubHttpResponse | None,
    ) -> None:
        delay = self._retry_delay_seconds * (2 ** (attempt - 1))
        if response is not None:
            retry_after = _headers(response.headers).get("retry-after")
            if retry_after is not None:
                if not retry_after.isascii() or not retry_after.isdecimal():
                    raise DeliveryIntegrityError("github_retry_header")
                delay = float(int(retry_after))
        try:
            remaining = deadline - self._monotonic()
            if remaining <= 0 or delay >= remaining:
                raise _error("github_transport", attempts=attempt)
            self._sleep(delay)
        except DeliveryError:
            raise
        except Exception:
            raise _error("github_transport", attempts=attempt) from None

    @staticmethod
    def _require_json(response: GitHubHttpResponse) -> None:
        content_type = _headers(response.headers).get("content-type")
        if content_type is None or not content_type.lower().startswith(
            "application/json"
        ):
            raise DeliveryIntegrityError(
                "github_content_type", status_code=response.status
            )

    @staticmethod
    def _raise_status(operation: str, response: GitHubHttpResponse) -> None:
        status = response.status
        headers = _headers(response.headers)
        if 300 <= status <= 399:
            raise _error("github_redirect", status_code=status)
        if status == 401:
            raise _error("github_authentication", status_code=status)
        if status == 403:
            stage = (
                "github_rate_limit"
                if headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in headers
                else "github_authorization"
            )
            raise _error(stage, status_code=status)
        if status == 404:
            raise _error("github_not_found", status_code=status)
        if status == 409:
            raise _error("github_conflict", status_code=status)
        if status == 422:
            raise _error("github_validation", status_code=status)
        if status == 429:
            raise _error("github_rate_limit", status_code=status, attempts=1)
        if status in _RETRIABLE_HTTP_STATUSES or 500 <= status <= 599:
            raise _error("github_transport", status_code=status, attempts=1)
        raise _error(operation, status_code=status)


def build_pull_request_title(prepared: PreparedDelivery) -> str:
    if not isinstance(prepared, PreparedDelivery):
        raise DeliveryIntegrityError("prepared_delivery")
    return f"Add Respan {prepared.product} instrumentation"


def build_pull_request_body(
    prepared: PreparedDelivery,
    receipt: VerificationReceipt,
    *,
    commit_sha: str,
) -> str:
    """Build a bounded body solely from validated, non-model evidence."""

    _validate_receipt(prepared, receipt)
    locked_commit = _require_sha(commit_sha)
    checks = tuple(receipt.passed_checks)
    if not checks or any(
        not isinstance(check, str) or _CHECK_RE.fullmatch(check) is None
        for check in checks
    ):
        raise DeliveryIntegrityError("verification_checks")
    check_lines = "\n".join(f"- [x] `{check}`" for check in checks)
    changed_lines = "\n".join(
        f"- `{changed_path}`" for changed_path in prepared.changed_paths
    )
    body = (
        "## Respan onboarding\n\n"
        "This pull request was generated by the Respan integration agent from the "
        "exact patch accepted by the v0 delivery gate. Review it before merging.\n\n"
        f"- Repository: `{prepared.target.slug}`\n"
        f"- Product: `{prepared.product}`\n"
        f"- Base: `{prepared.target.base_ref}` at `{prepared.base_sha}`\n"
        f"- Head: `{prepared.branch}` at `{locked_commit}`\n"
        f"- Patch SHA-256: `{prepared.patch_sha256}`\n"
        f"- Changed files: `{len(prepared.changed_paths)}`\n"
        f"- Agent trace: [open trace]({prepared.agent_trace_url})\n"
        f"- Target trace: [open trace]({receipt.target_trace_url})\n\n"
        "### Changed files\n\n"
        f"{changed_lines}\n\n"
        "### Verified gates\n\n"
        f"{check_lines}\n\n"
        "### Operator checklist\n\n"
        "- [ ] Keep the verified gateway funding route (credits or BYOK) configured; "
        "this change does not add credits or provider credentials.\n"
        "- [ ] Configure `RESPAN_API_KEY` in the target deployment.\n"
        "- [ ] Deploy the accepted change and inspect the first production trace.\n\n"
        f"<!-- respan-delivery:{prepared.delivery_fingerprint} -->\n"
    )
    if len(body.encode("utf-8")) > 64 * 1024:
        raise DeliveryIntegrityError("pull_request_body")
    return body


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


@runtime_checkable
class GitExecutor(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
    ) -> GitCommandResult: ...


class SubprocessGitExecutor:
    """Subprocess seam that converts every failure into a payload-free marker."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_bytes: bytes | None,
        timeout: float,
    ) -> GitCommandResult:
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        stdout = bytearray()
        stderr = bytearray()
        try:
            with tempfile.TemporaryFile() as stdin_file:
                if input_bytes is not None:
                    stdin_file.write(input_bytes)
                    stdin_file.seek(0)
                process = subprocess.Popen(
                    list(args),
                    cwd=cwd,
                    env=dict(env),
                    stdin=stdin_file if input_bytes is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                if process.stdout is None or process.stderr is None:
                    raise _GitFailure
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ, stdout)
                selector.register(process.stderr, selectors.EVENT_READ, stderr)
                deadline = time.monotonic() + timeout
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _GitFailure
                    events = selector.select(remaining)
                    if not events:
                        raise _GitFailure
                    for key, _mask in events:
                        chunk = os.read(key.fd, 64 * 1024)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        output = key.data
                        if len(output) + len(chunk) > _MAX_GIT_OUTPUT_BYTES:
                            raise _GitFailure
                        output.extend(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _GitFailure
                returncode = process.wait(timeout=remaining)
        except Exception:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    if process.poll() is None:
                        process.kill()
                try:
                    process.wait(timeout=1.0)
                except Exception:
                    pass
            raise _GitFailure from None
        finally:
            if selector is not None:
                selector.close()
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
        return GitCommandResult(returncode, bytes(stdout), bytes(stderr))


@dataclass(frozen=True, slots=True)
class PushedCommit:
    commit_sha: str
    disposition: RemoteDisposition


def _base_git_env(home: Path) -> dict[str, str]:
    """Use no credential, proxy, Git config, or application secret by default."""

    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _git_args(*args: str) -> tuple[str, ...]:
    return (
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "tag.gpgSign=false",
        "-c",
        "credential.helper=",
        "-c",
        "http.followRedirects=false",
        "-c",
        "http.sslVerify=true",
        "-c",
        "http.proxy=",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        *args,
    )


def _run_git(
    executor: GitExecutor,
    args: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    input_bytes: bytes | None = None,
    timeout: float,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> GitCommandResult:
    try:
        result = executor.run(
            args,
            cwd=cwd,
            env=env,
            input_bytes=input_bytes,
            timeout=timeout,
        )
    except Exception:
        raise _GitFailure from None
    if (
        not isinstance(result, GitCommandResult)
        or type(result.returncode) is not int
        or not isinstance(result.stdout, bytes)
        or not isinstance(result.stderr, bytes)
        or result.returncode not in allowed_returncodes
    ):
        raise _GitFailure from None
    return result


def _single_line(result: GitCommandResult) -> str:
    try:
        value = result.stdout.decode("utf-8").strip()
    except UnicodeError:
        raise _GitFailure from None
    if not value or "\n" in value or "\r" in value:
        raise _GitFailure from None
    return value


def _remote_ref(
    executor: GitExecutor,
    *,
    repo_root: Path,
    env: Mapping[str, str],
    remote_url: str,
    ref: str,
    timeout: float,
) -> str | None:
    result = _run_git(
        executor,
        _git_args("ls-remote", "--exit-code", "--refs", remote_url, ref),
        cwd=repo_root,
        env=env,
        timeout=timeout,
        allowed_returncodes=frozenset({0, 2}),
    )
    if result.returncode == 2:
        if result.stdout:
            raise _GitFailure from None
        return None
    try:
        rows = result.stdout.decode("utf-8").splitlines()
    except UnicodeError:
        raise _GitFailure from None
    if len(rows) != 1:
        raise _GitFailure from None
    fields = rows[0].split("\t")
    if len(fields) != 2 or fields[1] != ref or _SHA_RE.fullmatch(fields[0]) is None:
        raise _GitFailure from None
    return fields[0]


def _validate_changed_paths(prepared: PreparedDelivery) -> None:
    for raw_path in prepared.changed_paths:
        path = PurePosixPath(raw_path)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            raise DeliveryIntegrityError("changed_paths")
        if (
            len(path.parts) >= 2
            and path.parts[0].lower() == ".github"
            and path.parts[1].lower() == "workflows"
        ):
            raise DeliveryIntegrityError("workflow_change")


def _status_paths(stdout: bytes) -> tuple[str, ...]:
    entries = [entry for entry in stdout.split(b"\0") if entry]
    paths: list[str] = []
    try:
        for entry in entries:
            if len(entry) < 4 or entry[2:3] != b" " or entry[:2] == b"!!":
                raise _GitFailure from None
            paths.append(entry[3:].decode("utf-8"))
    except UnicodeError:
        raise _GitFailure from None
    return tuple(sorted(paths))


def _commit_message(prepared: PreparedDelivery) -> bytes:
    return (
        f"{build_pull_request_title(prepared)}\n\n"
        f"Respan-Delivery-Fingerprint: {prepared.delivery_fingerprint}\n"
        f"Respan-Patch-SHA256: {prepared.patch_sha256}\n"
        f"Respan-Agent-Run-ID: {prepared.agent_run_id}\n"
        f"Respan-Agent-Trace-ID: {prepared.agent_trace_id}\n"
        f"Respan-Product: {prepared.product}\n"
    ).encode("utf-8")


def _write_askpass(path: Path) -> None:
    payload = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
        "  *Password*) printf '%s\\n' \"$RESPAN_GITHUB_TOKEN\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o700)
        os.fchmod(descriptor, 0o700)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                raise OSError
            written += count
        os.fsync(descriptor)
    except OSError:
        raise _GitFailure from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _record(
    journal: DeliveryJournal,
    prepared: PreparedDelivery,
    phase: DeliveryPhase,
    *,
    commit_sha: str | None = None,
    remote_sha: str | None = None,
    pr_number: int | None = None,
    pr_url: str | None = None,
) -> None:
    """Record a monotonic phase, allowing exact replay of later durable state."""

    candidate = DeliveryJournalRecord(
        phase=phase,
        repository=prepared.target.slug,
        base_ref=prepared.target.base_ref,
        base_sha=prepared.base_sha,
        branch=prepared.branch,
        delivery_fingerprint=prepared.delivery_fingerprint,
        commit_sha=commit_sha,
        remote_sha=remote_sha,
        pr_number=pr_number,
        pr_url=pr_url,
    )
    previous = journal.load()
    if previous is None or _PHASE_RANK[previous.phase] < _PHASE_RANK[phase]:
        journal.record(candidate)
        return
    identity = (
        "repository",
        "base_ref",
        "base_sha",
        "branch",
        "delivery_fingerprint",
    )
    if any(getattr(previous, key) != getattr(candidate, key) for key in identity):
        raise DeliveryIntegrityError("journal_identity")
    for key in ("commit_sha", "remote_sha", "pr_number", "pr_url"):
        value = getattr(candidate, key)
        if value is not None and getattr(previous, key) != value:
            raise DeliveryIntegrityError("journal_identity")


def _record_pr_error_recovery(
    journal: DeliveryJournal,
    prepared: PreparedDelivery,
    error: DeliveryError,
    *,
    commit_sha: str,
) -> None:
    """Persist a PR identity observed in a typed error before re-raising it."""

    recovery = error.recovery
    phase_value = recovery.get("phase")
    if phase_value not in {
        DeliveryPhase.pr_observed.value,
        DeliveryPhase.pr_created.value,
    }:
        return
    expected_identity = {
        "repository": prepared.target.slug,
        "base_ref": prepared.target.base_ref,
        "base_sha": prepared.base_sha,
        "branch": prepared.branch,
        "delivery_fingerprint": prepared.delivery_fingerprint,
        "commit_sha": commit_sha,
    }
    if any(recovery.get(key) != value for key, value in expected_identity.items()):
        raise DeliveryIntegrityError("recovery_identity")
    pr_number = recovery.get("pr_number")
    pr_url = recovery.get("pr_url")
    if (
        type(pr_number) is not int
        or not isinstance(pr_url, str)
        or not isinstance(recovery.get("remote_sha"), str)
    ):
        raise DeliveryIntegrityError("recovery_identity")
    _require_sha(recovery["remote_sha"])
    _record_confirmed_remote(
        journal,
        prepared,
        DeliveryPhase(phase_value),
        commit_sha=commit_sha,
        remote_sha=recovery["remote_sha"],
        pr_number=pr_number,
        pr_url=pr_url,
    )


def _record_confirmed_remote(
    journal: DeliveryJournal,
    prepared: PreparedDelivery,
    phase: DeliveryPhase,
    *,
    commit_sha: str,
    remote_sha: str,
    pr_number: int | None = None,
    pr_url: str | None = None,
) -> None:
    """Turn a post-mutation journal failure into complete recovery evidence."""

    try:
        _record(
            journal,
            prepared,
            phase,
            commit_sha=commit_sha,
            remote_sha=remote_sha,
            pr_number=pr_number,
            pr_url=pr_url,
        )
    except DeliveryJournalError:
        raise DeliveryRecoveryRequired(
            "journal_recovery",
            recovery=_recovery(
                prepared,
                phase=phase,
                commit_sha=commit_sha,
                remote_sha=remote_sha,
                pr_number=pr_number,
                pr_url=pr_url,
            ),
        ) from None


def commit_and_push(
    prepared: PreparedDelivery,
    receipt: VerificationReceipt,
    token: str,
    journal: DeliveryJournal,
    *,
    git_executor: GitExecutor | None = None,
    git_timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
) -> PushedCommit:
    """Rebuild, commit, and publish only the accepted patch from a fresh clone."""

    if not isinstance(prepared, PreparedDelivery) or not isinstance(
        receipt, VerificationReceipt
    ):
        raise DeliveryIntegrityError("delivery_contract")
    _validate_receipt(prepared, receipt)
    if not isinstance(journal, DeliveryJournal):
        raise DeliveryIntegrityError("delivery_contract")
    locked_token = _require_token(token)
    locked_base = _require_sha(prepared.base_sha)
    locked_branch = _require_branch(prepared.branch)
    if _FINGERPRINT_RE.fullmatch(prepared.delivery_fingerprint) is None:
        raise DeliveryIntegrityError("delivery_fingerprint")
    if hashlib.sha256(prepared.patch).hexdigest() != prepared.patch_sha256:
        raise DeliveryIntegrityError("patch_identity")
    _validate_changed_paths(prepared)
    # The deterministic commit SHA is not available until the exact tree is rebuilt,
    # but every Git SHA has the same rendered width. Validate PR text and its bound
    # before journal or Git work using the already validated base SHA as a placeholder.
    build_pull_request_title(prepared)
    build_pull_request_body(prepared, receipt, commit_sha=prepared.base_sha)
    timeout = _positive_timeout(git_timeout)
    executor = git_executor if git_executor is not None else SubprocessGitExecutor()
    remote_url = prepared.target.canonical_url
    base_ref = f"refs/heads/{prepared.target.base_ref}"
    branch_ref = f"refs/heads/{locked_branch}"
    _record(journal, prepared, DeliveryPhase.prepared)

    with tempfile.TemporaryDirectory(prefix="respan-v0b-delivery-") as temp_root:
        root = Path(temp_root)
        repo_root = root / "repo"
        home = root / "home"
        home.mkdir(mode=0o700)
        env = _base_git_env(home)
        try:
            _run_git(
                executor,
                _git_args(
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--no-tags",
                    "--branch",
                    prepared.target.base_ref,
                    "--",
                    remote_url,
                    str(repo_root),
                ),
                cwd=root,
                env=env,
                timeout=timeout,
            )
            origin = _single_line(
                _run_git(
                    executor,
                    _git_args("remote", "get-url", "origin"),
                    cwd=repo_root,
                    env=env,
                    timeout=timeout,
                )
            )
            checkout_sha = _single_line(
                _run_git(
                    executor,
                    _git_args("rev-parse", "HEAD^{commit}"),
                    cwd=repo_root,
                    env=env,
                    timeout=timeout,
                )
            )
            if origin != remote_url or checkout_sha != locked_base:
                raise DeliveryIntegrityError(
                    "base_changed", recovery=_recovery(prepared)
                )
            clean = _run_git(
                executor,
                _git_args("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                cwd=repo_root,
                env=env,
                timeout=timeout,
            )
            if clean.stdout:
                raise DeliveryIntegrityError("fresh_clone")

            for check_only in (True, False):
                apply_args = ["apply", "--index"]
                if check_only:
                    apply_args.append("--check")
                apply_args += ["--whitespace=error-all", "-"]
                _run_git(
                    executor,
                    _git_args(*apply_args),
                    cwd=repo_root,
                    env=env,
                    input_bytes=prepared.patch,
                    timeout=timeout,
                )
            status = _run_git(
                executor,
                _git_args("status", "--porcelain=v1", "-z", "--untracked-files=all"),
                cwd=repo_root,
                env=env,
                timeout=timeout,
            )
            if _status_paths(status.stdout) != prepared.changed_paths:
                raise DeliveryIntegrityError("late_worktree_change")
            reconstructed = _run_git(
                executor,
                _git_args(
                    "diff",
                    "--cached",
                    "--binary",
                    "--full-index",
                    "--no-ext-diff",
                    "--no-renames",
                    locked_base,
                ),
                cwd=repo_root,
                env=env,
                timeout=timeout,
            ).stdout
            if hashlib.sha256(reconstructed).hexdigest() != prepared.patch_sha256:
                raise DeliveryIntegrityError("patch_identity")

            tree_sha = _require_sha(
                _single_line(
                    _run_git(
                        executor,
                        _git_args("write-tree"),
                        cwd=repo_root,
                        env=env,
                        timeout=timeout,
                    )
                )
            )
            base_date = _single_line(
                _run_git(
                    executor,
                    _git_args("show", "-s", "--format=%aI", locked_base),
                    cwd=repo_root,
                    env=env,
                    timeout=timeout,
                )
            )
            commit_env = {
                **env,
                "GIT_AUTHOR_NAME": "respan-integration-agent",
                "GIT_AUTHOR_EMAIL": "agent@respan.ai",
                "GIT_COMMITTER_NAME": "respan-integration-agent",
                "GIT_COMMITTER_EMAIL": "agent@respan.ai",
                "GIT_AUTHOR_DATE": base_date,
                "GIT_COMMITTER_DATE": base_date,
            }
            commit_sha = _require_sha(
                _single_line(
                    _run_git(
                        executor,
                        _git_args(
                            "commit-tree", tree_sha, "-p", locked_base, "-F", "-"
                        ),
                        cwd=repo_root,
                        env=commit_env,
                        input_bytes=_commit_message(prepared),
                        timeout=timeout,
                    )
                )
            )
            _record(
                journal,
                prepared,
                DeliveryPhase.committed,
                commit_sha=commit_sha,
            )

            observed_base = _remote_ref(
                executor,
                repo_root=repo_root,
                env=env,
                remote_url=remote_url,
                ref=base_ref,
                timeout=timeout,
            )
            if observed_base != locked_base:
                raise DeliveryIntegrityError(
                    "base_changed",
                    recovery=_recovery(
                        prepared,
                        phase=DeliveryPhase.committed,
                        commit_sha=commit_sha,
                    ),
                )
            observed_branch = _remote_ref(
                executor,
                repo_root=repo_root,
                env=env,
                remote_url=remote_url,
                ref=branch_ref,
                timeout=timeout,
            )
            if observed_branch is not None:
                if observed_branch != commit_sha:
                    raise DeliveryIntegrityError(
                        "branch_collision",
                        recovery=_recovery(
                            prepared,
                            phase=DeliveryPhase.branch_observed,
                            commit_sha=commit_sha,
                            remote_sha=observed_branch,
                        ),
                    )
                _record_confirmed_remote(
                    journal,
                    prepared,
                    DeliveryPhase.branch_observed,
                    commit_sha=commit_sha,
                    remote_sha=commit_sha,
                )
                final_base = _remote_ref(
                    executor,
                    repo_root=repo_root,
                    env=env,
                    remote_url=remote_url,
                    ref=base_ref,
                    timeout=timeout,
                )
                if final_base != locked_base:
                    raise DeliveryIntegrityError(
                        "base_changed",
                        recovery=_recovery(
                            prepared,
                            phase=DeliveryPhase.branch_observed,
                            commit_sha=commit_sha,
                            remote_sha=commit_sha,
                        ),
                    )
                return PushedCommit(commit_sha, RemoteDisposition.reused)

            askpass = root / "askpass.sh"
            _write_askpass(askpass)
            push_env = {
                **env,
                "GIT_ASKPASS": str(askpass),
                "GIT_ASKPASS_REQUIRE": "force",
                "RESPAN_GITHUB_TOKEN": locked_token,
            }
            push_failed = False
            try:
                _run_git(
                    executor,
                    _git_args(
                        "push",
                        "--porcelain",
                        "--no-verify",
                        f"--force-with-lease={branch_ref}:",
                        remote_url,
                        f"{commit_sha}:{branch_ref}",
                    ),
                    cwd=repo_root,
                    env=push_env,
                    timeout=timeout,
                )
            except _GitFailure:
                push_failed = True
            finally:
                push_env.pop("RESPAN_GITHUB_TOKEN", None)
                try:
                    askpass.unlink(missing_ok=True)
                except OSError:
                    pass

            remote_branch = _remote_ref(
                executor,
                repo_root=repo_root,
                env=env,
                remote_url=remote_url,
                ref=branch_ref,
                timeout=timeout,
            )
            if remote_branch != commit_sha:
                recovery = _recovery(
                    prepared,
                    phase=DeliveryPhase.committed,
                    commit_sha=commit_sha,
                    remote_sha=remote_branch,
                )
                if remote_branch is not None:
                    raise DeliveryIntegrityError(
                        "branch_collision",
                        attempts=1,
                        recovery=recovery,
                    )
                raise _error(
                    "push_ambiguous" if push_failed else "branch_collision",
                    attempts=1,
                    recovery=recovery,
                )
            branch_disposition = (
                RemoteDisposition.reused if push_failed else RemoteDisposition.created
            )
            branch_phase = (
                DeliveryPhase.branch_observed
                if push_failed
                else DeliveryPhase.branch_pushed
            )
            _record_confirmed_remote(
                journal,
                prepared,
                branch_phase,
                commit_sha=commit_sha,
                remote_sha=commit_sha,
            )
            final_base = _remote_ref(
                executor,
                repo_root=repo_root,
                env=env,
                remote_url=remote_url,
                ref=base_ref,
                timeout=timeout,
            )
            if final_base != locked_base:
                raise DeliveryIntegrityError(
                    "base_changed",
                    recovery=_recovery(
                        prepared,
                        phase=branch_phase,
                        commit_sha=commit_sha,
                        remote_sha=commit_sha,
                    ),
                )
            return PushedCommit(commit_sha, branch_disposition)
        except DeliveryError:
            raise
        except Exception:
            raise _error("git_transport", attempts=1) from None


def open_pr(
    prepared: PreparedDelivery,
    receipt: VerificationReceipt,
    token: str,
    journal: DeliveryJournal,
    *,
    rest_client: GitHubRestClient | None = None,
    git_executor: GitExecutor | None = None,
    git_timeout: float = _DEFAULT_GIT_TIMEOUT_SECONDS,
) -> DeliveryManifest:
    """Publish one exact branch and PR after receipt validation, with replay reuse."""

    _validate_receipt(prepared, receipt)
    pushed = commit_and_push(
        prepared,
        receipt,
        token,
        journal,
        git_executor=git_executor,
        git_timeout=git_timeout,
    )
    client = (
        rest_client
        if rest_client is not None
        else GitHubRestClient(token, prepared.target)
    )
    try:
        pr, pr_disposition = client.create_or_reconcile_pull_request(
            prepared,
            receipt,
            commit_sha=pushed.commit_sha,
        )
    except DeliveryError as error:
        _record_pr_error_recovery(
            journal,
            prepared,
            error,
            commit_sha=pushed.commit_sha,
        )
        raise
    pr_phase = (
        DeliveryPhase.pr_created
        if pr_disposition is RemoteDisposition.created
        else DeliveryPhase.pr_observed
    )
    _record_confirmed_remote(
        journal,
        prepared,
        pr_phase,
        commit_sha=pushed.commit_sha,
        remote_sha=pushed.commit_sha,
        pr_number=pr.number,
        pr_url=pr.url,
    )
    manifest = DeliveryManifest(
        target=prepared.target,
        base_sha=prepared.base_sha,
        branch=prepared.branch,
        commit_sha=pushed.commit_sha,
        delivery_fingerprint=prepared.delivery_fingerprint,
        pr_number=pr.number,
        pr_url=pr.url,
        branch_disposition=pushed.disposition,
        pr_disposition=pr_disposition,
    )
    manifest.validate_for(prepared, receipt)
    _record_confirmed_remote(
        journal,
        prepared,
        DeliveryPhase.completed,
        commit_sha=pushed.commit_sha,
        remote_sha=pushed.commit_sha,
        pr_number=pr.number,
        pr_url=pr.url,
    )
    return manifest


__all__ = [
    "DirectGitHubHttpsTransport",
    "GITHUB_API_ORIGIN",
    "GITHUB_API_VERSION",
    "GitCommandResult",
    "GitExecutor",
    "GitHubHttpRequest",
    "GitHubHttpResponse",
    "GitHubHttpTransport",
    "GitHubReadiness",
    "GitHubRestClient",
    "PullRequestRecord",
    "PushedCommit",
    "SubprocessGitExecutor",
    "build_pull_request_body",
    "build_pull_request_title",
    "commit_and_push",
    "open_pr",
]
